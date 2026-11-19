"""The AWS rules, in the order the engine has always run them (R1-R7, R6b, R10), then R8."""

from __future__ import annotations

from cloudoptics.analysis import billing, compute, rules, schedule
from cloudoptics.analysis.registry import RuleSpec, check_unique

#: R8 sizes a Savings Plan for the payer from the bills, after the other rules have run
#: (``commitments.service.r8_candidates``); listed here for its metadata.
R8 = RuleSpec(
    id="R8",
    provider="aws",
    rule_ids=("R8_savings_plan",),
    resource_types=frozenset({"savings_plan_scope"}),
    version=1,
)

AWS_RULES: tuple[RuleSpec, ...] = check_unique(
    (*rules.RULE_SPECS, *compute.RULE_SPECS, *schedule.RULE_SPECS, *billing.RULE_SPECS, R8)
)
