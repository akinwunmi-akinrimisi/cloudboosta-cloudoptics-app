"""Read-side recommendation figures for other modules (the weekly digest)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

OPEN = ["new", "assigned", "in_progress"]


@dataclass(frozen=True)
class TopRecommendation:
    id: uuid.UUID
    title: str
    savings_usd_month: float
    risk_level: str
    effort: str
    new: bool  # first found in the window


@dataclass(frozen=True)
class RecommendationWeek:
    open_count: int
    open_savings_usd_month: float
    new_count: int
    done_count: int
    done_savings_usd_month: float
    top: list[TopRecommendation]


async def week(
    session: AsyncSession, start: datetime, end: datetime, limit: int = 5
) -> RecommendationWeek:
    """Open recommendations (the best ``limit``), those first found and those marked done
    or verified by someone between ``start`` and ``end``. Inside a tenant-scoped
    transaction."""
    params = {"open": OPEN, "start": start, "end": end}
    totals = (
        await session.execute(
            text(
                "SELECT count(*) AS n, coalesce(sum(savings_usd_month), 0) AS savings, "
                "count(*) FILTER (WHERE first_detected_at >= :start "
                "AND first_detected_at < :end) AS new FROM recommendation "
                "WHERE status = ANY(:open) AND primary_of IS NULL"
            ),
            params,
        )
    ).one()
    done = (
        await session.execute(
            text(
                "SELECT count(*) AS n, coalesce(sum(r.savings_usd_month), 0) AS savings "
                "FROM recommendation r WHERE r.status IN ('done', 'verified') "
                "AND EXISTS (SELECT 1 FROM recommendation_event e "
                "WHERE e.recommendation_id = r.id AND e.kind = 'status_changed' "
                "AND e.to_status IN ('done', 'verified') AND e.created_at >= :start "
                "AND e.created_at < :end)"
            ),
            params,
        )
    ).one()
    top = await session.execute(
        text(
            "SELECT id, title, savings_usd_month, risk_level, effort, "
            "first_detected_at >= :start AND first_detected_at < :end AS new "
            "FROM recommendation WHERE status = ANY(:open) AND primary_of IS NULL "
            "ORDER BY priority_score DESC, savings_usd_month DESC LIMIT :limit"
        ),
        {**params, "limit": limit},
    )
    return RecommendationWeek(
        open_count=totals.n,
        open_savings_usd_month=round(float(totals.savings), 2),
        new_count=totals.new,
        done_count=done.n,
        done_savings_usd_month=round(float(done.savings), 2),
        top=[
            TopRecommendation(
                r.id, r.title, round(float(r.savings_usd_month), 2), r.risk_level, r.effort, r.new
            )
            for r in top
        ],
    )
