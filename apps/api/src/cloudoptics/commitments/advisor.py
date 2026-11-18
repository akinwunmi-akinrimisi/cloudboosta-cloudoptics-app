"""R8 Savings Plan advisory: the arithmetic (PRD US-4.5, FR-ANA-10). Pure functions.

**Our simulation.** The baseline is the Savings-Plan-eligible on-demand spend (EC2 compute,
Fargate, Lambda duration; not Spot, not already covered) from the CUR, as an hourly rate,
with the savings of open R4-R7 recommendations taken off first so a commitment is never
sized for usage another recommendation is about to remove.

Cloudoptics keeps **daily** CUR aggregates, not hourly ones, so the hourly series is each
day's spend / 24 (the day's average hour). That hides the troughs inside a day (nights,
weekends inside the day boundary), so it overstates the hourly p10-p20. To stay
conservative we take the low end of the PRD's band: the target is 60% of the p10 of those
daily-average hours (the band shown runs up to 80% of the p20). A commitment at or under
the quietest hours is used in full, so the saving holds even if usage dips.

**Options.** For each instrument (Compute Savings Plan, EC2 Reserved Instance) x term
(1, 3 years) x payment (No, Partial, All Upfront) the target is priced at a typical
discount; where AWS returned its own recommendation for a 1-year Compute Savings Plan the
discount AWS estimated for this account replaces the typical one. AWS's own options
(30- and 60-day lookbacks) are shown next to ours.

**Break-even** is the month in which the on-demand cost avoided has paid back the upfront
payment: ``upfront / (on-demand equivalent per month - recurring per month)``; 0 when
there is no upfront payment (it saves from the first hour).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

HOURS_PER_MONTH = 730
HOURS_PER_YEAR = 8760
BASELINE_DAYS = 60
MIN_DAYS = 14
TARGET_SHARE = (0.6, 0.8)  # of the p10 and the p20 of the hourly baseline
PERCENTILES = (10, 20)
LADDER_FIRST = 0.25  # of the target (PRD: 20-35%)
LADDER_STEP = 0.10  # of the target, each month after
FX_STRESS = (0.2, 0.4)  # local currency weakening: the dollar costs 20% / 40% more
RETURN_WINDOW_DAYS = 7
MIN_COMMITMENT = 0.001  # AWS sells hourly commitments in steps of USD 0.001
MIN_SAVINGS_USD_MONTH = 5.0

Instrument = Literal["savings_plan", "reserved_instance"]
Payment = Literal["no_upfront", "partial_upfront", "all_upfront"]
Status = Literal["no_data", "not_enough_data", "no_purchase", "ok"]
INSTRUMENTS: tuple[Instrument, ...] = ("savings_plan", "reserved_instance")
PAYMENT_OPTIONS: tuple[Payment, ...] = ("no_upfront", "partial_upfront", "all_upfront")
UPFRONT_SHARE: dict[Payment, float] = {
    "no_upfront": 0.0,
    "partial_upfront": 0.5,
    "all_upfront": 1.0,
}
AWS_PAYMENT: dict[str, Payment] = {"NO_UPFRONT": "no_upfront", "ALL_UPFRONT": "all_upfront"}

# Typical discounts against on-demand for Linux, shared-tenancy compute (us-east-1 public
# prices, averaged over the m/c/r families, November 2026). Compute Savings Plans; Standard
# EC2 Reserved Instances (which fix the instance family and region).
TYPICAL_DISCOUNT: dict[tuple[Instrument, int, Payment], float] = {
    ("savings_plan", 1, "no_upfront"): 0.28,
    ("savings_plan", 1, "partial_upfront"): 0.31,
    ("savings_plan", 1, "all_upfront"): 0.33,
    ("savings_plan", 3, "no_upfront"): 0.46,
    ("savings_plan", 3, "partial_upfront"): 0.50,
    ("savings_plan", 3, "all_upfront"): 0.52,
    ("reserved_instance", 1, "no_upfront"): 0.36,
    ("reserved_instance", 1, "partial_upfront"): 0.39,
    ("reserved_instance", 1, "all_upfront"): 0.40,
    ("reserved_instance", 3, "no_upfront"): 0.54,
    ("reserved_instance", 3, "partial_upfront"): 0.59,
    ("reserved_instance", 3, "all_upfront"): 0.61,
}


def percentile(values: list[float], p: float) -> float:
    """Linear interpolation between the closest ranks (NumPy's default)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _floor(value: float, step: float = MIN_COMMITMENT) -> float:
    return round(math.floor(round(value / step, 6)) * step, 3)


@dataclass(frozen=True)
class Baseline:
    days: int
    netted_usd_month: float
    average_hourly_usd: float
    p10_hourly_usd: float
    p20_hourly_usd: float
    # On-demand cost per hour the commitment should cover (ours), and the PRD's band.
    target_hourly_usd: float
    band_low_hourly_usd: float
    band_high_hourly_usd: float


def baseline(daily_usd: list[float], netted_usd_month: float) -> Baseline:
    """``daily_usd``: eligible on-demand spend per day, oldest first (at most 60 used)."""
    days = daily_usd[-BASELINE_DAYS:]
    netted_hour = max(0.0, netted_usd_month) / HOURS_PER_MONTH
    hourly = [max(0.0, d / 24 - netted_hour) for d in days]
    p10, p20 = (percentile(hourly, p) for p in PERCENTILES)
    low, high = TARGET_SHARE[0] * p10, TARGET_SHARE[1] * p20
    return Baseline(
        days=len(days),
        netted_usd_month=round(max(0.0, netted_usd_month), 2),
        average_hourly_usd=round(sum(hourly) / len(hourly), 4) if hourly else 0.0,
        p10_hourly_usd=round(p10, 4),
        p20_hourly_usd=round(p20, 4),
        target_hourly_usd=round(low, 4),
        band_low_hourly_usd=round(low, 4),
        band_high_hourly_usd=round(high, 4),
    )


@dataclass(frozen=True)
class Option:
    source: Literal["ours", "aws"]
    instrument: Instrument
    term_years: int
    payment: Payment
    lookback_days: int | None
    discount_percent: float
    # The commitment (at commitment rates) and the on-demand spend it covers, per hour.
    hourly_commitment_usd: float
    covered_on_demand_hourly_usd: float
    monthly_savings_usd: float
    upfront_usd: float
    monthly_recurring_usd: float
    total_commitment_usd: float
    break_even_months: float
    discount_from_aws: bool = False


def option(
    instrument: Instrument,
    term_years: int,
    payment: Payment,
    covered_hourly: float,
    discount: float,
    *,
    source: Literal["ours", "aws"] = "ours",
    lookback_days: int | None = None,
    from_aws: bool = False,
    hourly_commitment: float | None = None,
    upfront: float | None = None,
    monthly_savings: float | None = None,
) -> Option:
    commitment = (
        _floor(covered_hourly * (1 - discount)) if hourly_commitment is None else hourly_commitment
    )
    covered = commitment / (1 - discount) if discount < 1 else covered_hourly
    term_months = 12 * term_years
    total = commitment * HOURS_PER_YEAR * term_years
    paid_upfront = total * UPFRONT_SHARE[payment] if upfront is None else upfront
    recurring = max(0.0, total - paid_upfront) / term_months
    on_demand_month = covered * HOURS_PER_MONTH
    saving = on_demand_month - total / term_months if monthly_savings is None else monthly_savings
    gain = on_demand_month - recurring
    if paid_upfront <= 0:
        break_even = 0.0
    elif gain <= 0:
        break_even = float(term_months)
    else:
        break_even = min(float(term_months), paid_upfront / gain)
    return Option(
        source=source,
        instrument=instrument,
        term_years=term_years,
        payment=payment,
        lookback_days=lookback_days,
        discount_percent=round(discount * 100, 1),
        hourly_commitment_usd=round(commitment, 3),
        covered_on_demand_hourly_usd=round(covered, 4),
        monthly_savings_usd=round(saving, 2),
        upfront_usd=round(paid_upfront, 2),
        monthly_recurring_usd=round(recurring, 2),
        total_commitment_usd=round(total, 2),
        break_even_months=round(break_even, 1),
        discount_from_aws=from_aws,
    )


def aws_discounts(recommendations: list[dict[str, object]]) -> dict[Payment, float]:
    """AWS's estimated discount for a 1-year Compute Savings Plan, per payment option
    (the longest lookback wins)."""
    found: dict[Payment, tuple[int, float]] = {}
    for r in recommendations:
        payment = AWS_PAYMENT.get(str(r.get("paymentOption")))
        pct = float(str(r.get("estimatedSavingsPercent") or 0))
        hourly = float(str(r.get("hourlyCommitmentUsd") or 0))
        lookback = int(str(r.get("lookbackDays") or 0))
        if payment is None or hourly <= 0 or not 0 < pct < 100:
            continue
        if payment not in found or lookback > found[payment][0]:
            found[payment] = (lookback, pct / 100)
    return {k: v[1] for k, v in found.items()}


def matrix(target_hourly: float, recommendations: list[dict[str, object]]) -> list[Option]:
    """Ours for every instrument x term x payment, then AWS's own options."""
    calibrated = aws_discounts(recommendations)
    options = []
    for instrument in INSTRUMENTS:
        for term in (1, 3):
            for payment in PAYMENT_OPTIONS:
                aws = instrument == "savings_plan" and term == 1 and payment in calibrated
                typical = TYPICAL_DISCOUNT[(instrument, term, payment)]
                discount = calibrated[payment] if aws else typical
                options.append(
                    option(instrument, term, payment, target_hourly, discount, from_aws=aws)
                )
    for r in sorted(
        recommendations,
        key=lambda r: (int(str(r.get("lookbackDays") or 0)), str(r.get("paymentOption"))),
    ):
        aws_payment = AWS_PAYMENT.get(str(r.get("paymentOption")))
        hourly = float(str(r.get("hourlyCommitmentUsd") or 0))
        pct = float(str(r.get("estimatedSavingsPercent") or 0)) / 100
        if aws_payment is None or hourly <= 0 or not 0 <= pct < 1:
            continue
        options.append(
            option(
                "savings_plan",
                1,
                aws_payment,
                0.0,
                pct,
                source="aws",
                lookback_days=int(str(r.get("lookbackDays") or 0)) or None,
                hourly_commitment=hourly,
                upfront=float(str(r.get("upfrontUsd") or 0)),
                monthly_savings=float(str(r.get("estimatedMonthlySavingsUsd") or 0)),
            )
        )
    return options


def default_option(options: list[Option]) -> Option | None:
    """PRD FR-ANA-10: 1-year, No Upfront Compute Savings Plan (no large dollar payment up
    front, the shortest commitment, any instance family or region)."""
    for o in options:
        if (o.source, o.instrument, o.term_years, o.payment) == (
            "ours",
            "savings_plan",
            1,
            "no_upfront",
        ):
            return o
    return None


@dataclass(frozen=True)
class LadderStep:
    month: int
    buy_on: date
    buy_hourly_usd: float
    total_hourly_usd: float
    share_percent: int


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    return date(day.year + index // 12, index % 12 + 1, 1)


def ladder(target_commitment: float, start: date) -> list[LadderStep]:
    """Buy 25% of the target now, then about 10% more each month (first of the month)."""
    if target_commitment < MIN_COMMITMENT:
        return []
    steps: list[LadderStep] = []
    share, held, month = LADDER_FIRST, 0.0, 0
    while held < target_commitment - 1e-9:
        total = target_commitment if share >= 1 - 1e-9 else _floor(target_commitment * share)
        buy = round(total - held, 3)
        if buy >= MIN_COMMITMENT:
            steps.append(
                LadderStep(
                    month=month,
                    buy_on=start if month == 0 else _add_months(start, month),
                    buy_hourly_usd=buy,
                    total_hourly_usd=round(total, 3),
                    share_percent=round(min(share, 1.0) * 100),
                )
            )
            held = total
            month += 1
        share = min(1.0, share + LADDER_STEP)
    return steps


@dataclass(frozen=True)
class FxScenario:
    weakening_percent: int
    rate: float
    monthly_local: float
    upfront_local: float
    total_local: float


@dataclass(frozen=True)
class FxView:
    currency: str
    rate: float
    rate_date: date
    estimated: bool
    scenarios: list[FxScenario] = field(default_factory=list)


def fx_view(o: Option, currency: str, rate: float, rate_date: date, estimated: bool) -> FxView:
    """The option's cost in local currency now and if the currency weakens 20% / 40%
    (each US dollar then costs 20% / 40% more local currency)."""
    monthly = o.total_commitment_usd / (12 * o.term_years) if o.term_years else 0.0
    scenarios = []
    for weakening in (0.0, *FX_STRESS):
        r = rate * (1 + weakening)
        scenarios.append(
            FxScenario(
                weakening_percent=round(weakening * 100),
                rate=round(r, 6),
                monthly_local=round(monthly * r, 2),
                upfront_local=round(o.upfront_usd * r, 2),
                total_local=round(o.total_commitment_usd * r, 2),
            )
        )
    return FxView(currency, rate, rate_date, estimated, scenarios)


def decline_tolerance_percent(b: Baseline, o: Option) -> float | None:
    """How far eligible usage can fall from its 60-day average before the commitment is
    partly unused (US-4.5 "risk of usage decline")."""
    if b.average_hourly_usd <= 0:
        return None
    return round(max(0.0, 1 - o.covered_on_demand_hourly_usd / b.average_hourly_usd) * 100, 1)


def target_coverage_percent(
    b: Baseline, o: Option, covered_usd: float | None, on_demand_usd: float | None, days: int
) -> float | None:
    """Savings Plan coverage after buying ``o``, from Cost Explorer's coverage over
    ``days`` (spend covered / (covered + on-demand)), or from our baseline alone."""
    if covered_usd is None or on_demand_usd is None or days <= 0:
        if b.average_hourly_usd <= 0:
            return None
        return round(min(100.0, o.covered_on_demand_hourly_usd / b.average_hourly_usd * 100), 1)
    hours = days * 24
    covered = covered_usd / hours + o.hourly_commitment_usd
    on_demand = max(0.0, on_demand_usd / hours - o.covered_on_demand_hourly_usd)
    total = covered + on_demand
    return round(covered / total * 100, 1) if total > 0 else None


def status(b: Baseline, default: Option | None) -> Status:
    if b.days == 0 or b.average_hourly_usd <= 0:
        return "no_data"
    if b.days < MIN_DAYS:
        return "not_enough_data"
    if (
        default is None
        or default.hourly_commitment_usd < MIN_COMMITMENT
        or default.monthly_savings_usd < MIN_SAVINGS_USD_MONTH
    ):
        return "no_purchase"
    return "ok"
