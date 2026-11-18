"""Read-only AWS calls for commitments (PRD FR-ANA-10, FR-CON-07 "Savings Plans: daily").

With the payer's billing role (``ce:Get*``, ``savingsplans:Describe*``):

- ``savingsplans:DescribeSavingsPlans``: the Savings Plans the payer holds (free).
- Cost Explorer, eight calls a refresh at USD 0.01 each, paid by the customer:
  ``GetSavingsPlansPurchaseRecommendation`` for a 1-year Compute Savings Plan, No Upfront
  and All Upfront, over 30 and 60 days (four calls); ``GetSavingsPlansCoverage`` and
  ``GetSavingsPlansUtilizationDetails`` (per plan, which also covers plans bought in member
  accounts); ``GetReservationUtilization`` per reservation and ``GetReservationCoverage``.

With each account's resource role (``ec2:DescribeReservedInstances``, ``rds:Describe*``),
per region: the EC2 and RDS reservations (free).

Every function here is synchronous and takes boto3 clients, so tests use fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

LOOKBACKS = {"THIRTY_DAYS": 30, "SIXTY_DAYS": 60}
PAYMENTS = ("NO_UPFRONT", "ALL_UPFRONT")
WINDOW_DAYS = 30
SP_STATES = ["payment-pending", "queued", "active"]
RI_STATES = ["payment-pending", "active"]


def code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _attr(attributes: dict[str, Any], *names: str) -> Any:
    """Cost Explorer's attribute names vary in case between APIs."""
    lowered = {k.lower(): v for k, v in attributes.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


@dataclass
class Commitment:
    kind: str  # savings_plan | ec2_reserved | rds_reserved
    commitment_id: str
    description: str
    account_id: str | None = None
    region_id: str | None = None
    plan_type: str | None = None
    term_months: int | None = None
    payment_option: str | None = None
    state: str | None = None
    quantity: int | None = None
    hourly_commitment_usd: float | None = None
    upfront_usd: float | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    utilisation_pct: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _months(seconds: Any) -> int | None:
    value = _float(seconds)
    return None if value is None else round(value / (365 * 86400) * 12)


def savings_plans(client: Any, account_id: str) -> list[Commitment]:
    """``DescribeSavingsPlans`` in the payer, paged."""
    found: list[Commitment] = []
    token: str | None = None
    while True:
        page = client.describe_savings_plans(
            states=SP_STATES, **({"nextToken": token} if token else {})
        )
        for sp in page.get("savingsPlans", []):
            plan_type = sp.get("savingsPlanType")
            family = sp.get("ec2InstanceFamily")
            found.append(
                Commitment(
                    kind="savings_plan",
                    commitment_id=str(sp.get("savingsPlanArn") or sp.get("savingsPlanId")),
                    description=" ".join(
                        p for p in (plan_type, "Savings Plan", family, sp.get("region")) if p
                    ),
                    account_id=account_id,
                    region_id=sp.get("region") or None,
                    plan_type=plan_type,
                    term_months=_months(sp.get("termDurationInSeconds")),
                    payment_option=sp.get("paymentOption"),
                    state=sp.get("state"),
                    hourly_commitment_usd=_float(sp.get("commitment")),
                    upfront_usd=_float(sp.get("upfrontPaymentAmount")),
                    starts_at=_time(sp.get("start")),
                    ends_at=_time(sp.get("end")),
                    details={"savingsPlanId": sp.get("savingsPlanId")},
                )
            )
        token = page.get("nextToken")
        if not token:
            return found


def _hourly_recurring(ri: dict[str, Any]) -> float:
    return sum(
        float(c.get("Amount") or 0)
        for c in ri.get("RecurringCharges") or []
        if c.get("Frequency") == "Hourly"
    )


def ec2_reservations(ec2: Any, account_id: str, region: str) -> list[Commitment]:
    page = ec2.describe_reserved_instances(Filters=[{"Name": "state", "Values": RI_STATES}])
    found = []
    for ri in page.get("ReservedInstances", []):
        count = int(ri.get("InstanceCount") or 1)
        found.append(
            Commitment(
                kind="ec2_reserved",
                commitment_id=str(ri["ReservedInstancesId"]),
                description=f"{count} x {ri.get('InstanceType')} "
                f"{ri.get('ProductDescription') or ''}".strip(),
                account_id=account_id,
                region_id=region,
                plan_type=ri.get("OfferingClass"),
                term_months=_months(ri.get("Duration")),
                payment_option=ri.get("OfferingType"),
                state=ri.get("State"),
                quantity=count,
                hourly_commitment_usd=round(
                    (_hourly_recurring(ri) + float(ri.get("UsagePrice") or 0)) * count, 6
                ),
                upfront_usd=_float(ri.get("FixedPrice")),
                starts_at=_time(ri.get("Start")),
                ends_at=_time(ri.get("End")),
                details={"instanceType": ri.get("InstanceType"), "scope": ri.get("Scope")},
            )
        )
    return found


def rds_reservations(rds: Any, account_id: str, region: str) -> list[Commitment]:
    found = []
    marker: str | None = None
    while True:
        page = rds.describe_reserved_db_instances(**({"Marker": marker} if marker else {}))
        for ri in page.get("ReservedDBInstances", []):
            if ri.get("State") not in (*RI_STATES, None):
                continue
            start = _time(ri.get("StartTime"))
            duration = _float(ri.get("Duration"))
            count = int(ri.get("DBInstanceCount") or 1)
            found.append(
                Commitment(
                    kind="rds_reserved",
                    commitment_id=str(ri["ReservedDBInstanceId"]),
                    description=f"{count} x {ri.get('DBInstanceClass')} "
                    f"{ri.get('ProductDescription') or ''}".strip()
                    + (" Multi-AZ" if ri.get("MultiAZ") else ""),
                    account_id=account_id,
                    region_id=region,
                    term_months=_months(duration),
                    payment_option=ri.get("OfferingType"),
                    state=ri.get("State"),
                    quantity=count,
                    hourly_commitment_usd=round(
                        (_hourly_recurring(ri) + float(ri.get("UsagePrice") or 0)) * count, 6
                    ),
                    upfront_usd=_float(ri.get("FixedPrice")),
                    starts_at=start,
                    ends_at=start + timedelta(seconds=duration) if start and duration else None,
                    details={
                        "instanceClass": ri.get("DBInstanceClass"),
                        "leaseId": ri.get("LeaseId"),
                    },
                )
            )
        marker = page.get("Marker")
        if not marker:
            return found


@dataclass
class ExplorerData:
    """What Cost Explorer said about commitments, for ``commitment_snapshot``."""

    recommendations: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    utilisation: dict[str, Any] = field(default_factory=dict)
    # Utilisation per Savings Plan ARN and per reservation (lease) ID, in percent.
    by_commitment: dict[str, float] = field(default_factory=dict)
    # Savings Plans Cost Explorer reports that DescribeSavingsPlans did not list (bought
    # in a member account).
    other_plans: list[Commitment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    calls: int = 0


def _recommendation(page: dict[str, Any], payment: str, lookback: str) -> dict[str, Any]:
    rec = page.get("SavingsPlansPurchaseRecommendation") or {}
    summary = rec.get("SavingsPlansPurchaseRecommendationSummary") or {}
    details = rec.get("SavingsPlansPurchaseRecommendationDetails") or []
    upfront = sum(_float(d.get("UpfrontCost")) or 0.0 for d in details)
    return {
        "paymentOption": payment,
        "lookbackDays": LOOKBACKS[lookback],
        "termYears": 1,
        "hourlyCommitmentUsd": _float(summary.get("HourlyCommitmentToPurchase")) or 0.0,
        "estimatedSavingsPercent": _float(summary.get("EstimatedSavingsPercentage")) or 0.0,
        "estimatedMonthlySavingsUsd": _float(summary.get("EstimatedMonthlySavingsAmount")) or 0.0,
        "currentOnDemandSpendUsd": _float(summary.get("CurrentOnDemandSpend")),
        "upfrontUsd": round(upfront, 2),
        "generatedAt": (page.get("Metadata") or {}).get("GenerationTimestamp"),
    }


def _safe(data: ExplorerData, name: str, call: Callable[[], None]) -> None:
    try:
        call()
    except (ClientError, BotoCoreError, KeyError, ValueError) as exc:
        data.errors.append(f"{name}: {code(exc)}")


def explorer(ce: Any, today: date, known_plans: set[str]) -> ExplorerData:
    """Cost Explorer's view of commitments over the last ``WINDOW_DAYS`` full days.

    A failing call is recorded in ``errors`` and the rest still run (partial-failure
    tolerance, docs/11c I4)."""
    data = ExplorerData()
    period = {
        "Start": (today - timedelta(days=WINDOW_DAYS)).isoformat(),
        "End": today.isoformat(),
    }

    for lookback in LOOKBACKS:
        for payment in PAYMENTS:

            def _rec(payment: str = payment, lookback: str = lookback) -> None:
                data.calls += 1
                page = ce.get_savings_plans_purchase_recommendation(
                    SavingsPlansType="COMPUTE_SP",
                    TermInYears="ONE_YEAR",
                    PaymentOption=payment,
                    LookbackPeriodInDays=lookback,
                    AccountScope="PAYER",
                )
                data.recommendations.append(_recommendation(page, payment, lookback))

            _safe(data, f"recommendation {payment} {lookback}", _rec)

    def _coverage() -> None:
        data.calls += 1
        page = ce.get_savings_plans_coverage(TimePeriod=period, Granularity="MONTHLY")
        covered = on_demand = 0.0
        for item in page.get("SavingsPlansCoverages", []):
            cov = item.get("Coverage") or {}
            covered += _float(cov.get("SpendCoveredBySavingsPlans")) or 0.0
            on_demand += _float(cov.get("OnDemandCost")) or 0.0
        total = covered + on_demand
        data.coverage["savingsPlans"] = {
            "coveredUsd": round(covered, 2),
            "onDemandUsd": round(on_demand, 2),
            "percent": round(covered / total * 100, 1) if total > 0 else None,
            "days": WINDOW_DAYS,
        }

    def _utilisation() -> None:
        token: str | None = None
        total: dict[str, Any] = {}
        while True:
            data.calls += 1
            page = ce.get_savings_plans_utilization_details(
                TimePeriod=period, **({"NextToken": token} if token else {})
            )
            for item in page.get("SavingsPlansUtilizationDetails", []):
                arn = str(item.get("SavingsPlanArn") or "")
                pct = _float((item.get("Utilization") or {}).get("UtilizationPercentage"))
                if arn and pct is not None:
                    data.by_commitment[arn] = pct
                if arn and arn not in known_plans:
                    attrs = item.get("Attributes") or {}
                    hourly = _float(_attr(attrs, "RecurringHourlyFee", "HourlyCommitment"))
                    data.other_plans.append(
                        Commitment(
                            kind="savings_plan",
                            commitment_id=arn,
                            description=" ".join(
                                str(p)
                                for p in (_attr(attrs, "SavingsPlansType"), "Savings Plan")
                                if p
                            ),
                            account_id=arn.split(":")[4] if arn.count(":") >= 5 else None,
                            plan_type=_attr(attrs, "SavingsPlansType"),
                            payment_option=_attr(attrs, "PaymentOption"),
                            hourly_commitment_usd=hourly,
                            starts_at=_time(_attr(attrs, "StartDateTime", "StartDate")),
                            ends_at=_time(_attr(attrs, "EndDateTime", "EndDate")),
                            utilisation_pct=pct,
                        )
                    )
            total = page.get("Total") or total
            token = page.get("NextToken")
            if not token:
                break
        util = total.get("Utilization") or {}
        data.utilisation["savingsPlans"] = {
            "percent": _float(util.get("UtilizationPercentage")),
            "unusedUsd": _float(util.get("UnusedCommitment")),
            "netSavingsUsd": _float((total.get("Savings") or {}).get("NetSavings")),
            "days": WINDOW_DAYS,
        }

    def _ri_utilisation() -> None:
        data.calls += 1
        page = ce.get_reservation_utilization(
            TimePeriod=period, GroupBy=[{"Type": "DIMENSION", "Key": "SUBSCRIPTION_ID"}]
        )
        for slot in page.get("UtilizationsByTime", []):
            for group in slot.get("Groups", []):
                attrs = group.get("Attributes") or {}
                pct = _float((group.get("Utilization") or {}).get("UtilizationPercentage"))
                lease = _attr(attrs, "leaseId", "reservationId")
                for key in (lease, group.get("Value")):
                    if key and pct is not None:
                        data.by_commitment[str(key)] = pct
        util = page.get("Total") or {}
        data.utilisation["reservations"] = {
            "percent": _float(util.get("UtilizationPercentage")),
            "days": WINDOW_DAYS,
        }

    def _ri_coverage() -> None:
        data.calls += 1
        page = ce.get_reservation_coverage(TimePeriod=period)
        hours = ((page.get("Total") or {}).get("CoverageHours")) or {}
        data.coverage["reservations"] = {
            "percent": _float(hours.get("CoverageHoursPercentage")),
            "days": WINDOW_DAYS,
        }

    _safe(data, "savings plans coverage", _coverage)
    _safe(data, "savings plans utilisation", _utilisation)
    _safe(data, "reservation utilisation", _ri_utilisation)
    _safe(data, "reservation coverage", _ri_coverage)
    return data


def regions(aws: Any) -> list[str]:
    from cloudoptics.connectors.aws import STACK_REGION

    ec2 = aws.client("ec2", STACK_REGION)
    return sorted(r["RegionName"] for r in ec2.describe_regions().get("Regions", []))
