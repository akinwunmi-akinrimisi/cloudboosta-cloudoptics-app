"""/v1/tenants/{t}/costs/forecast and /costs/changes (PRD US-3.2, FR-ANA-09/12, docs/09 §3.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import Field

from cloudoptics.fx.routes import Quote
from cloudoptics.identity.deps import Session, ViewerAccess
from cloudoptics.ingestion import queries
from cloudoptics.ingestion.routes import Forecast, MonthParam, chosen_month
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.conditional import ConditionalGet
from cloudoptics.trends import changes, forecast

router = APIRouter(prefix="/v1/tenants/{tenant_id}/costs", tags=["costs"])


class MonthEnd(ApiModel):
    month: str  # YYYY-MM
    forecast: Forecast
    low: float
    high: float


class MonthAhead(ApiModel):
    month: str  # YYYY-MM
    amount: float
    low: float
    high: float
    #: ``trend``: a straight line through the last 90 days; ``run_rate``: under 28 days of
    #: bills, so the last 7 days' average carries on.
    method: Literal["trend", "run_rate"]


class FxSplit(ApiModel):
    """Last month against the month before in the workspace currency, each day converted at
    its own rate: the change is ``usageEffect`` + ``fxEffect`` (null without rates)."""

    month: str
    previous_month: str
    usd: float
    previous_usd: float
    local: float | None
    previous_local: float | None
    rate: float | None = Field(description="Month average, local per USD")
    previous_rate: float | None
    usage_effect: float | None
    fx_effect: float | None


class Outlook(ApiModel):
    """USD forecasts. ``quote`` is today's rate for the workspace currency (null: USD only);
    a scenario multiplies it by (1 + movement)."""

    status: Literal["no_data", "ready"]
    currency: str
    basis: Literal["official", "market"]
    data_through: date | None
    quote: Quote | None
    month_end: MonthEnd | None
    months: list[MonthAhead]
    #: Once-a-month charges (AWS Support, fees) included in each month ahead.
    monthly_charges_usd: float
    #: Tax as a share of the rest over the trend window, added to each month ahead.
    tax_share: float
    trend_days: int
    decomposition: FxSplit | None


def _month(day: date) -> str:
    return f"{day:%Y-%m}"


@router.get("/forecast", response_model=Outlook)
async def get_forecast(
    access: ViewerAccess, session: Session, cond: ConditionalGet, today: date | None = None
) -> Outlook:
    """This month's forecast, the next three months with a range, today's rate and last
    month's usage and exchange-rate effects. ``today`` (UTC) pins the date for tests."""
    return await cond.cached(lambda: _outlook(access.tenant_id, session, today))


async def _outlook(tenant_id: uuid.UUID, session: Session, today: date | None) -> Outlook:
    o = await forecast.outlook(session, tenant_id, today or datetime.now(UTC).date())
    d = o.decomposition
    return Outlook(
        status=o.status,
        currency=o.currency,
        basis=o.basis,
        data_through=o.data_through,
        quote=Quote.model_validate(o.quote) if o.quote else None,
        month_end=MonthEnd(
            month=_month(o.month_end_month),
            forecast=Forecast.model_validate(o.month_end),
            low=o.month_end_range[0],
            high=o.month_end_range[1],
        )
        if o.month_end and o.month_end_month and o.month_end_range
        else None,
        months=[
            MonthAhead(
                month=_month(m.month), amount=m.amount, low=m.low, high=m.high, method=m.method
            )
            for m in o.months
        ],
        monthly_charges_usd=o.monthly_charges_usd,
        tax_share=o.tax_share,
        trend_days=o.trend_days,
        decomposition=None
        if d is None
        else FxSplit(
            month=_month(d.month),
            previous_month=_month(d.previous_month),
            usd=d.usd,
            previous_usd=d.previous_usd,
            local=d.local,
            previous_local=d.previous_local,
            rate=d.rate,
            previous_rate=d.previous_rate,
            usage_effect=d.usage_effect,
            fx_effect=d.fx_effect,
        ),
    )


class Contributor(ApiModel):
    service_code: str
    service_name: str
    region: str | None
    usd: float
    previous_usd: float
    change_usd: float
    change_percent: float | None  # null: nothing the month before
    usage_type: str | None
    usage_type_change_usd: float
    #: The sentence in USD; the page words it in the viewer's currency.
    sentence: str


class Period(ApiModel):
    first: date = Field(alias="from")
    last: date = Field(alias="to")


class Changes(ApiModel):
    """Month to date against the same days last month (usage and purchases); for an ended
    month, the whole month against the whole month before."""

    status: Literal["no_data", "ready"]
    month: str | None
    current: Period | None
    previous: Period | None
    usd: float
    previous_usd: float
    change_usd: float
    rises: list[Contributor]
    falls: list[Contributor]


def _usd(amount: float) -> str:
    return f"USD {amount:,.2f}"


def _contributor(c: changes.Contributor) -> Contributor:
    return Contributor(
        service_code=c.service_code,
        service_name=c.service_name,
        region=c.region,
        usd=c.usd,
        previous_usd=c.previous_usd,
        change_usd=c.change_usd,
        change_percent=c.change_percent,
        usage_type=c.usage_type,
        usage_type_change_usd=c.usage_type_change_usd,
        sentence=changes.sentence(c, _usd),
    )


@router.get("/changes", response_model=Changes)
async def get_changes(
    access: ViewerAccess,
    session: Session,
    cond: ConditionalGet,
    today: date | None = None,
    limit: int = Query(changes.TOP, ge=1, le=10),
    month: MonthParam = None,
) -> Changes:
    """The biggest rises and falls by service and region (``limit`` each way). ``month``
    (YYYY-MM, an ended month): that whole month against the whole month before; by
    default the month to date against the same days last month."""
    day = today or datetime.now(UTC).date()
    past = queries.past_month(chosen_month(month, day), day)
    return await cond.cached(lambda: _changes(session, day, limit, past))


async def _changes(session: Session, day: date, limit: int, past: date | None) -> Changes:
    w = await changes.what_changed(session, day, limit, month=past)
    if w is None:
        return Changes(
            status="no_data", month=None, current=None, previous=None, usd=0.0,
            previous_usd=0.0, change_usd=0.0, rises=[], falls=[],
        )  # fmt: skip
    return Changes(
        status="ready",
        month=_month(w.month),
        current=Period(first=w.current[0], last=w.current[1]),
        previous=Period(first=w.previous[0], last=w.previous[1]),
        usd=w.usd,
        previous_usd=w.previous_usd,
        change_usd=w.change_usd,
        rises=[_contributor(c) for c in w.rises],
        falls=[_contributor(c) for c in w.falls],
    )
