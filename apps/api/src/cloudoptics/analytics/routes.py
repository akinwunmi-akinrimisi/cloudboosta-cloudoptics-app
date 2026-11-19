"""/v1/admin/analytics: the funnel and site counts for Cloudoptics staff (SITE-15).

Staff only (platform-admin allowlist, verified email) with an MFA session, like the support
access tool. Everything returned is a count; no workspace, user or visitor is named. How to
read the numbers: docs/analytics.md.

Funnel cohorts: a workspace belongs to the window it was *created* in, and its later steps
count for that window whenever they happened. Sign-ups and site visits count in the window
they happened in. The source of a workspace is its first-touch ``utm_source``, else ``ref``
(the referring site the public site passes on), else ``direct``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import TextClause, text

from cloudoptics.analytics.funnel import STEPS
from cloudoptics.identity.deps import (
    CurrentUser,
    Session,
    User,
    has_mfa,
    is_platform_admin,
    mfa_required_problem,
)
from cloudoptics.platform.api import ApiModel

router = APIRouter(prefix="/v1/admin/analytics", tags=["analytics"])

ANALYTICS_SCOPE = text("SELECT set_config('app.analytics_read', 'on', true)")

# Literal statements only; the window is always bound.
COHORT_STEPS = text(
    "WITH cohort AS (SELECT tenant_id, coalesce(source, ref, 'direct') AS source "
    "FROM funnel_event WHERE event = 'workspace_created' "
    "AND occurred_at >= :since AND occurred_at < :until) "
    "SELECT c.source, f.event, count(*) AS n FROM cohort c "
    "JOIN funnel_event f ON f.tenant_id = c.tenant_id GROUP BY c.source, f.event"
)
SIGNUPS = text(
    "SELECT count(*) AS signups, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM funnel_event w "
    "WHERE w.event = 'workspace_created' AND w.user_id = s.user_id)) AS with_workspace "
    "FROM funnel_event s WHERE s.event = 'signup_completed' "
    "AND s.occurred_at >= :since AND s.occurred_at < :until"
)
SITE_BY_SOURCE = text(
    "SELECT coalesce(utm_source, ref, referrer_host, 'direct') AS source, "
    "count(*) FILTER (WHERE kind = 'page_view' AND landing) AS landings, "
    "count(*) FILTER (WHERE kind = 'cta_click' AND cta = 'start') AS start_clicks "
    "FROM site_event WHERE occurred_at >= :since AND occurred_at < :until GROUP BY 1"
)
SITE_TOTALS = text(
    "SELECT count(*) FILTER (WHERE kind = 'page_view') AS page_views, "
    "count(*) FILTER (WHERE kind = 'page_view' AND landing) AS landings, "
    "count(*) FILTER (WHERE kind = 'cta_click' AND cta = 'start') AS start_clicks "
    "FROM site_event WHERE occurred_at >= :since AND occurred_at < :until"
)
TOP_PAGES = text(
    "SELECT path AS name, count(*) AS n FROM site_event WHERE kind = 'page_view' "
    "AND occurred_at >= :since AND occurred_at < :until GROUP BY path ORDER BY n DESC, path "
    "LIMIT 50"
)
TOP_REFERRERS = text(
    "SELECT referrer_host AS name, count(*) AS n FROM site_event WHERE kind = 'page_view' "
    "AND landing AND referrer_host IS NOT NULL AND occurred_at >= :since "
    "AND occurred_at < :until GROUP BY referrer_host ORDER BY n DESC, referrer_host LIMIT 50"
)
TOP_CAMPAIGNS = text(
    "SELECT concat_ws(' / ', utm_source, utm_medium, utm_campaign) AS name, count(*) AS n "
    "FROM site_event WHERE kind = 'page_view' AND landing AND utm_source IS NOT NULL "
    "AND occurred_at >= :since AND occurred_at < :until GROUP BY 1 ORDER BY n DESC, 1 LIMIT 50"
)
TOP_CTAS = text(
    "SELECT concat(cta, ' on ', path) AS name, count(*) AS n FROM site_event "
    "WHERE kind = 'cta_click' AND occurred_at >= :since AND occurred_at < :until "
    "GROUP BY cta, path ORDER BY n DESC, 1 LIMIT 50"
)


async def analytics_staff(user: User, session: Session) -> CurrentUser:
    """Cloudoptics staff with a multi-factor session; opens the analytics read scope."""
    if not is_platform_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cloudoptics staff only")
    if not await has_mfa(user):
        raise mfa_required_problem()
    await session.execute(ANALYTICS_SCOPE)
    return user


Staff = Annotated[CurrentUser, Depends(analytics_staff)]


class SiteTotals(ApiModel):
    page_views: int
    landings: int
    start_clicks: int


class SourceRow(ApiModel):
    source: str
    landings: int
    start_clicks: int
    # Workspaces created in the window from this source, and how many reached each step.
    steps: dict[str, int]


class Funnel(ApiModel):
    since: date
    until: date
    site: SiteTotals
    signups: int
    signups_with_workspace: int
    # Workspaces created in the window, and how many of them reached each later step.
    steps: dict[str, int]
    by_source: list[SourceRow]


class Count(ApiModel):
    name: str
    count: int


class SiteReport(ApiModel):
    since: date
    until: date
    pages: list[Count]
    referrers: list[Count]
    campaigns: list[Count]
    ctas: list[Count]


def _window(since: date | None, until: date | None) -> dict[str, datetime]:
    end = until or datetime.now(UTC).date()
    start = since or end - timedelta(days=29)
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "since must not be after until")
    if (end - start).days > 400:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "At most 400 days at a time")
    return {
        "since": datetime.combine(start, time.min, UTC),
        "until": datetime.combine(end + timedelta(days=1), time.min, UTC),
    }


Since = Annotated[date | None, Query(description="First day (UTC), default 29 days before until")]
Until = Annotated[date | None, Query(description="Last day (UTC), inclusive; default today")]


@router.get("/funnel", response_model=Funnel, response_model_by_alias=True)
async def funnel(_: Staff, session: Session, since: Since = None, until: Until = None) -> Funnel:
    window = _window(since, until)
    tenant_steps = [s for s in STEPS if s != "signup_completed"]
    sources: dict[str, SourceRow] = {}

    def row(source: str) -> SourceRow:
        if source not in sources:
            sources[source] = SourceRow(
                source=source, landings=0, start_clicks=0, steps=dict.fromkeys(tenant_steps, 0)
            )
        return sources[source]

    for r in await session.execute(SITE_BY_SOURCE, window):
        if r.landings or r.start_clicks:
            entry = row(r.source)
            entry.landings, entry.start_clicks = r.landings, r.start_clicks
    totals = dict.fromkeys(tenant_steps, 0)
    for r in await session.execute(COHORT_STEPS, window):
        row(r.source).steps[r.event] = r.n
        totals[r.event] += r.n
    site = (await session.execute(SITE_TOTALS, window)).one()
    signups = (await session.execute(SIGNUPS, window)).one()
    return Funnel(
        since=window["since"].date(),
        until=(window["until"] - timedelta(days=1)).date(),
        site=SiteTotals.model_validate(site._mapping),
        signups=signups.signups,
        signups_with_workspace=signups.with_workspace,
        steps=totals,
        by_source=sorted(
            sources.values(), key=lambda s: (-s.steps["workspace_created"], -s.landings, s.source)
        ),
    )


@router.get("/site", response_model=SiteReport, response_model_by_alias=True)
async def site_report(
    _: Staff, session: Session, since: Since = None, until: Until = None
) -> SiteReport:
    window = _window(since, until)

    async def counts(query: TextClause) -> list[Count]:
        return [Count(name=r.name, count=r.n) for r in await session.execute(query, window)]

    return SiteReport(
        since=window["since"].date(),
        until=(window["until"] - timedelta(days=1)).date(),
        pages=await counts(TOP_PAGES),
        referrers=await counts(TOP_REFERRERS),
        campaigns=await counts(TOP_CAMPAIGNS),
        ctas=await counts(TOP_CTAS),
    )
