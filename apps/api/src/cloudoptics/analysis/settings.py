"""Tenant rule settings (PRD FR-REC-08, WBS AN-17): rules turned off, the tag that hides a
resource from every rule, and the idle thresholds. Missing values take the defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_IGNORE_TAG = "cloudoptics:ignore"

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
]


@dataclass(frozen=True)
class RuleSettings:
    disabled_rules: frozenset[str] = field(default_factory=frozenset)
    ignore_tag: str = DEFAULT_IGNORE_TAG
    idle_cpu_percent: float = 5.0
    idle_days: int = 14

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> RuleSettings:
        data = data or {}
        default = cls()
        return cls(
            disabled_rules=frozenset(r for r in data.get("disabledRules", []) if r in RULE_IDS),
            ignore_tag=str(data.get("ignoreTag") or default.ignore_tag),
            idle_cpu_percent=float(data.get("idleCpuPercent", default.idle_cpu_percent)),
            idle_days=int(data.get("idleDays", default.idle_days)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "disabledRules": [r for r in RULE_IDS if r in self.disabled_rules],
            "ignoreTag": self.ignore_tag,
            "idleCpuPercent": self.idle_cpu_percent,
            "idleDays": self.idle_days,
        }

    def ignores(self, tags: dict[str, str]) -> bool:
        wanted = self.ignore_tag.lower()
        return any(k.lower() == wanted for k in tags)


async def load_rule_settings(session: AsyncSession) -> RuleSettings:
    """Inside a tenant-scoped transaction."""
    data = (await session.execute(text("SELECT settings FROM rule_setting"))).scalar_one_or_none()
    return RuleSettings.from_json(data)
