"""Tenant rule settings (PRD FR-REC-08, WBS AN-17): rules turned off, the tag that hides a
resource from every rule, the idle thresholds, what counts as non-production and the office
hours non-production servers are scheduled for (R7). Missing values take the defaults.

The non-production pattern is a regular expression the tenant writes, run in the shared
worker on text from the tenant's own AWS account (tags, names). It runs on the ``regex``
engine with a time limit on every match (``MATCH_TIMEOUT``), so a pattern that backtracks
badly cannot hold the worker: it is refused when saved (``pattern_problem`` times it on
strings built to make it backtrack) and, if one still times out on real text, it stops
matching for the rest of the process (``NonprodMatcher``)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import regex
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_IGNORE_TAG = "cloudoptics:ignore"
# R7 (PRD FR-ANA-07): a tag value under one of these keys, or a name, matching the pattern
# marks a resource as non-production. Matched case-insensitively.
DEFAULT_NONPROD_PATTERN = (
    r"(^|[^a-z0-9])(dev|development|test|testing|staging|stage|qa|sandbox|uat)([^a-z]|$)"
)
DEFAULT_NONPROD_TAG_KEYS = ("env", "environment", "stage")
DEFAULT_SCHEDULE_START = "07:00"
DEFAULT_SCHEDULE_END = "20:00"
MAX_PATTERN_LENGTH = 200
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# A quantified group that is itself quantified, e.g. (a+)+: can take exponential time.
NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
#: Text the pattern is matched against is cut to this length.
MAX_TEXT = 256
#: Seconds one match may take. A sensible pattern takes microseconds on 256 characters.
MATCH_TIMEOUT = 0.1
SLOW_PATTERN = "The pattern can take too long to match, which would hold up the analysis"

log = logging.getLogger(__name__)


def _compile(pattern: str) -> Any:
    return regex.compile(pattern, regex.IGNORECASE | regex.VERSION0)


def _probes(pattern: str) -> list[str]:
    """Texts that make a backtracking pattern slow: long runs of each character and each
    word the pattern names (and of common characters), ended by characters that make most
    matches fail. A heuristic: the time limit on every match is what bounds the cost."""
    units = dict.fromkeys(c for c in pattern if c.isprintable() and not c.isspace())
    units.update(dict.fromkeys("a0-_ ."))
    units.update(dict.fromkeys(re.findall(r"[A-Za-z0-9]{2,}", pattern)))
    return [
        (unit * (MAX_TEXT // len(unit)))[: MAX_TEXT - 1] + end
        for unit in units
        for end in ("\x00", "!")
    ]


def pattern_problem(pattern: str) -> str | None:
    """Why a non-production pattern cannot be used, or None when it can."""
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        return f"The pattern must be 1 to {MAX_PATTERN_LENGTH} characters"
    if NESTED_QUANTIFIER.search(pattern):
        return "The pattern repeats a repeated group, which can be very slow"
    try:
        re.compile(pattern)
        compiled = _compile(pattern)
    except (re.error, regex.error) as exc:
        return f"The pattern is not a valid regular expression: {exc}"
    try:
        for probe in _probes(pattern):
            compiled.search(probe, timeout=MATCH_TIMEOUT)
    except TimeoutError:
        return SLOW_PATTERN
    return None


class NonprodMatcher:
    """The tenant's pattern, matched with a time limit. A match that runs out of time counts
    as no match, and the pattern then matches nothing more in this process, so one slow
    match is the most a stored pattern can cost a worker."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self._compiled = _compile(pattern)
        self.timed_out = False

    def search(self, value: str) -> bool:
        if self.timed_out:
            return False
        try:
            # concurrent: the GIL is released while matching, so other threads run.
            found = self._compiled.search(value[:MAX_TEXT], timeout=MATCH_TIMEOUT, concurrent=True)
        except TimeoutError:
            self.timed_out = True
            log.warning("Non-production pattern timed out; it is skipped from now on")
            return False
        return found is not None


@lru_cache(maxsize=32)
def nonprod_matcher(pattern: str) -> NonprodMatcher:
    return NonprodMatcher(pattern)


def window_problem(start: str, end: str) -> str | None:
    if not HHMM.match(start) or not HHMM.match(end):
        return "Times must be HH:MM, from 00:00 to 23:59"
    if start >= end:
        return "The schedule must end after it starts"
    return None


# Every rule a tenant can turn off, in the order the Settings page shows them.
RULE_IDS = [
    "R1_unattached_ebs",
    "R1_old_snapshot",
    "R2_idle_eip",
    "R2_idle_lb",
    "R3_gp2_gp3",
    "R4_idle_ec2",
    "R4_idle_rds",
    "R5_rightsize_ec2",
    "R6_newer_generation",
    "R6_graviton",
    "R7_schedule_ec2",
    "R7_schedule_rds",
    "R6b_extended_support",
    "R10_idle_nat",
    "R8_savings_plan",
    # DigitalOcean (providers/digitalocean/rules.py, ADR-021).
    "DO1_unattached_volume",
    "DO2_unassigned_reserved_ip",
    "DO3_old_snapshot",
    "DO4_idle_droplet",
    "DO5_idle_load_balancer",
    # Azure (providers/azure/rules.py, ADR-020)
    "AZ1_unattached_disk",
    "AZ2_idle_public_ip",
    "AZ3_stopped_vm",
    "AZ4_old_snapshot",
    "AZ5_underused_vm",
]


@dataclass(frozen=True)
class RuleSettings:
    disabled_rules: frozenset[str] = field(default_factory=frozenset)
    ignore_tag: str = DEFAULT_IGNORE_TAG
    idle_cpu_percent: float = 5.0
    idle_days: int = 14
    nonprod_pattern: str = DEFAULT_NONPROD_PATTERN
    nonprod_tag_keys: tuple[str, ...] = DEFAULT_NONPROD_TAG_KEYS
    schedule_start: str = DEFAULT_SCHEDULE_START
    schedule_end: str = DEFAULT_SCHEDULE_END

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> RuleSettings:
        data = data or {}
        default = cls()
        pattern = str(data.get("nonprodPattern") or default.nonprod_pattern)
        keys = tuple(str(k).strip() for k in data.get("nonprodTagKeys") or [] if str(k).strip())
        start = str(data.get("scheduleStart") or default.schedule_start)
        end = str(data.get("scheduleEnd") or default.schedule_end)
        if window_problem(start, end):
            start, end = default.schedule_start, default.schedule_end
        return cls(
            disabled_rules=frozenset(r for r in data.get("disabledRules", []) if r in RULE_IDS),
            ignore_tag=str(data.get("ignoreTag") or default.ignore_tag),
            idle_cpu_percent=float(data.get("idleCpuPercent", default.idle_cpu_percent)),
            idle_days=int(data.get("idleDays", default.idle_days)),
            nonprod_pattern=default.nonprod_pattern if pattern_problem(pattern) else pattern,
            nonprod_tag_keys=keys or default.nonprod_tag_keys,
            schedule_start=start,
            schedule_end=end,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "disabledRules": [r for r in RULE_IDS if r in self.disabled_rules],
            "ignoreTag": self.ignore_tag,
            "idleCpuPercent": self.idle_cpu_percent,
            "idleDays": self.idle_days,
            "nonprodPattern": self.nonprod_pattern,
            "nonprodTagKeys": list(self.nonprod_tag_keys),
            "scheduleStart": self.schedule_start,
            "scheduleEnd": self.schedule_end,
        }

    def ignores(self, tags: dict[str, str]) -> bool:
        wanted = self.ignore_tag.lower()
        return any(k.lower() == wanted for k in tags)


async def load_rule_settings(session: AsyncSession) -> RuleSettings:
    """Inside a tenant-scoped transaction."""
    data = (await session.execute(text("SELECT settings FROM rule_setting"))).scalar_one_or_none()
    return RuleSettings.from_json(data)
