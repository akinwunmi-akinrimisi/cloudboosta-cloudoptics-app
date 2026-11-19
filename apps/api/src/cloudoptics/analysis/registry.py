"""Rule registry (ADR-018): every savings rule with its metadata.

A rule is a function over the tenant's resources of one provider (``RuleFn``) plus what the
rest of the product needs to know about it: the provider it belongs to, the resource types
it reads, the rule IDs its candidates carry (stored on each recommendation, used by the
per-rule settings) and its version. Rule modules declare ``RULE_SPECS``; each provider
plug-in lists its own (``providers/<name>/rules.py``) and the engine runs them through
``engine_rules``, each over that provider's resources only.

Scoring, deduplication, the recommendation workflow, verification and realised savings stay
shared: they read ``Candidate`` and the stored row, not the provider.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloudoptics.analysis.rules import Candidate, Context, Resource

RuleFn = Callable[["Context", list["Resource"]], Iterator["Candidate"]]


@dataclass(frozen=True)
class RuleSpec:
    """One rule. ``id`` names the rule family (the R-number of PRD FR-ANA); ``rule_ids``
    are the ``rule_id`` values its candidates carry (some rules emit two variants)."""

    id: str
    provider: str
    rule_ids: tuple[str, ...]
    resource_types: frozenset[str]
    version: int
    # None: the rule is evaluated outside the engine's loop (R8 sizes the payer's
    # commitments from the bills, after the other rules; commitments/service.py).
    evaluate: RuleFn | None = None


class DuplicateRuleError(ValueError):
    """Two rules claim the same rule ID (recommendations are keyed on it)."""


def check_unique(specs: tuple[RuleSpec, ...]) -> tuple[RuleSpec, ...]:
    seen: dict[str, str] = {}
    for spec in specs:
        for rule_id in spec.rule_ids:
            if rule_id in seen:
                raise DuplicateRuleError(f"{rule_id} is in {seen[rule_id]} and {spec.id}")
            seen[rule_id] = spec.id
    return specs


def all_rules() -> tuple[RuleSpec, ...]:
    """Every registered provider's rules, in each provider's own order."""
    from cloudoptics.providers import registered

    return check_unique(tuple(spec for p in registered() for spec in p.rules))


def engine_rules() -> tuple[RuleSpec, ...]:
    """The rules the engine runs in its loop, in order."""
    return tuple(spec for spec in all_rules() if spec.evaluate is not None)


def rules_for(provider: str) -> tuple[RuleSpec, ...]:
    return tuple(spec for spec in all_rules() if spec.provider == provider)


def spec_for(rule_id: str) -> RuleSpec | None:
    """The rule a stored ``rule_id`` came from (None for a retired rule)."""
    return next((s for s in all_rules() if rule_id in s.rule_ids), None)
