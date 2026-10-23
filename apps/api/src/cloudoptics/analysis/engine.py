"""Analysis run for one tenant: rules over the inventory, scored and stored (docs/09 §3.3).

Scoring follows PRD FR-REC-03/04: risk points add up to a score (Low < 35, Medium 35-65,
High > 65); priority = monthly savings x confidence x risk x effort weights. Several
candidates for one resource are deduplicated (FR-REC-06): the highest priority is the
primary and the rest point at it with ``primary_of`` (an alternative, not extra savings).

Rows are keyed by a fingerprint (rule, account, region, resource), so a re-run updates a
recommendation in place and keeps its status. An open recommendation no rule produces any
more (the resource changed or went away) becomes ``resolved_externally``; a resolved one
that comes back is reopened.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analysis.pricing import load_price_book
from cloudoptics.analysis.rules import RULES, Candidate, Context, Resource, parse_time
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7

ENGINE_VERSION = "2026.10.1"
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}
RISK_WEIGHT = {"low": 1.0, "medium": 0.6, "high": 0.3}
EFFORT_WEIGHT = {"S": 1.0, "M": 0.8, "L": 0.6}
OPEN = ("new", "assigned", "in_progress")
REOPENABLE = ("resolved_externally", "expired")


def risk_level(score: int) -> str:
    if score > 65:
        return "high"
    return "medium" if score >= 35 else "low"


@dataclass
class Scored:
    candidate: Candidate
    risk_score: int
    risk_level: str
    priority: float

    @property
    def fingerprint(self) -> str:
        r = self.candidate.resource
        return f"{self.candidate.rule_id}:{r.account_id}:{r.region_id}:{r.resource_id}"


def score(c: Candidate) -> Scored:
    points = min(100, sum(int(f["points"]) for f in c.risk_factors))
    level = risk_level(points)
    priority = (
        max(0.0, c.savings)
        * CONFIDENCE_WEIGHT[c.confidence]
        * RISK_WEIGHT[level]
        * EFFORT_WEIGHT[c.effort]
    )
    return Scored(c, points, level, round(priority, 6))


async def _load(session: AsyncSession) -> list[Resource]:
    rows = await session.execute(
        text(
            "SELECT id, account_id, region_id, resource_type, resource_id, name, state, config, "
            "tags, state_since, first_seen_at, arn FROM resource WHERE deleted_at IS NULL"
        )
    )
    return [
        Resource(
            id=r.id,
            account_id=r.account_id.strip(),
            region_id=r.region_id,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            name=r.name,
            state=r.state,
            config=r.config or {},
            tags=r.tags or {},
            state_since=r.state_since,
            first_seen_at=r.first_seen_at,
            arn=r.arn,
        )
        for r in rows
    ]


def build_context(resources: list[Resource], ctx: Context) -> Context:
    for r in resources:
        key = (r.account_id, r.region_id)
        if r.resource_type == "ebs_volume":
            ctx.live_volumes.add((r.account_id, r.region_id, r.resource_id))
        elif r.resource_type == "ec2_instance":
            ctx.instances.setdefault(key, {})[r.resource_id] = r
        elif r.resource_type == "ebs_snapshot" and r.state == "completed":
            volume = r.config.get("volumeId")
            started = parse_time(r.config.get("startTime"))
            if volume and started:
                per_region = ctx.snapshots.setdefault(key, {})
                if volume not in per_region or per_region[volume] < started:
                    per_region[volume] = started
    return ctx


def evaluate(ctx: Context, resources: list[Resource]) -> list[Scored]:
    build_context(resources, ctx)
    return [score(c) for rule in RULES for c in rule(ctx, resources)]


def _row(s: Scored, generation: int) -> dict[str, Any]:
    c = s.candidate
    return {
        "fingerprint": s.fingerprint,
        "type": c.type,
        "rule_id": c.rule_id,
        "rule_version": c.rule_version,
        "rule_config": json.dumps(c.rule_config),
        "generation": generation,
        "resource_ref": c.resource.id,
        "account_id": c.resource.account_id,
        "region_id": c.resource.region_id,
        "resource_id": c.resource.resource_id,
        "title": c.title,
        "current_config": json.dumps(c.current_config, default=str),
        "recommended_config": json.dumps(c.recommended_config, default=str),
        "cost_now": round(c.cost_now, 6),
        "cost_after": round(c.cost_after, 6),
        "basis": c.basis,
        "effort": c.effort,
        "reversibility": c.reversibility,
        "restart": c.restart_required,
        "risk_score": s.risk_score,
        "risk_level": s.risk_level,
        "risk_factors": json.dumps(c.risk_factors),
        "confidence": c.confidence,
        "confidence_notes": json.dumps(c.confidence_notes),
        "priority": s.priority,
        "evidence": json.dumps(c.evidence, default=str),
        "fix": json.dumps(c.fix),
    }


UPSERT = text(
    "INSERT INTO recommendation (id, tenant_id, fingerprint, type, rule_id, rule_version, "
    "rule_config, generation_id, resource_ref, account_id, region_id, resource_id, title, "
    "current_config, recommended_config, cost_now_usd_month, cost_after_usd_month, "
    "savings_basis, effort, reversibility, restart_required, risk_score, risk_level, "
    "risk_factors, confidence, confidence_notes, priority_score, evidence, fix, "
    "engine_version) VALUES (:id, :t, :fingerprint, :type, :rule_id, :rule_version, "
    "CAST(:rule_config AS jsonb), :generation, :resource_ref, :account_id, :region_id, "
    ":resource_id, :title, CAST(:current_config AS jsonb), CAST(:recommended_config AS jsonb), "
    ":cost_now, :cost_after, :basis, :effort, :reversibility, :restart, :risk_score, "
    ":risk_level, CAST(:risk_factors AS jsonb), :confidence, CAST(:confidence_notes AS jsonb), "
    ":priority, CAST(:evidence AS jsonb), CAST(:fix AS jsonb), :engine) "
    "ON CONFLICT (tenant_id, fingerprint) DO UPDATE SET type = EXCLUDED.type, "
    "rule_version = EXCLUDED.rule_version, rule_config = EXCLUDED.rule_config, "
    "generation_id = EXCLUDED.generation_id, resource_ref = EXCLUDED.resource_ref, "
    "title = EXCLUDED.title, current_config = EXCLUDED.current_config, "
    "recommended_config = EXCLUDED.recommended_config, "
    "cost_now_usd_month = EXCLUDED.cost_now_usd_month, "
    "cost_after_usd_month = EXCLUDED.cost_after_usd_month, "
    "savings_basis = EXCLUDED.savings_basis, effort = EXCLUDED.effort, "
    "reversibility = EXCLUDED.reversibility, restart_required = EXCLUDED.restart_required, "
    "risk_score = EXCLUDED.risk_score, risk_level = EXCLUDED.risk_level, "
    "risk_factors = EXCLUDED.risk_factors, confidence = EXCLUDED.confidence, "
    "confidence_notes = EXCLUDED.confidence_notes, priority_score = EXCLUDED.priority_score, "
    "evidence = EXCLUDED.evidence, fix = EXCLUDED.fix, engine_version = EXCLUDED.engine_version, "
    "last_evaluated_at = now(), status = CASE WHEN recommendation.status IN "
    "('resolved_externally', 'expired') THEN 'new' ELSE recommendation.status END "
    "RETURNING id"
)


async def store(
    session: AsyncSession, tenant_id: uuid.UUID, scored: list[Scored], generation: int
) -> dict[str, int]:
    ids: dict[str, uuid.UUID] = {}
    for s in scored:
        params = {**_row(s, generation), "id": uuid7(), "t": tenant_id, "engine": ENGINE_VERSION}
        ids[s.fingerprint] = (await session.execute(UPSERT, params)).scalar_one()

    by_resource: dict[uuid.UUID, list[Scored]] = defaultdict(list)
    for s in scored:
        by_resource[s.candidate.resource.id].append(s)
    for group in by_resource.values():
        group.sort(key=lambda s: s.priority, reverse=True)
        primary = ids[group[0].fingerprint]
        await session.execute(
            text("UPDATE recommendation SET primary_of = NULL WHERE id = :p"), {"p": primary}
        )
        if len(group) > 1:
            await session.execute(
                text("UPDATE recommendation SET primary_of = :p WHERE id = ANY(:ids)"),
                {"p": primary, "ids": [ids[s.fingerprint] for s in group[1:]]},
            )

    resolved = await session.execute(
        text(
            "UPDATE recommendation SET status = 'resolved_externally', last_evaluated_at = now() "
            "WHERE generation_id < :g AND status = ANY(:open) RETURNING id"
        ),
        {"g": generation, "open": list(OPEN)},
    )
    return {"recommendations": len(scored), "resolved": len(resolved.all())}


async def run_analysis(tenant_id: uuid.UUID, now: datetime | None = None) -> dict[str, int]:
    """Evaluate every rule for one tenant and store the results."""
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        resources = await _load(session)
        ctx = Context(now=now or datetime.now(UTC), prices=await load_price_book(session))
        scored = evaluate(ctx, resources)
        return await store(session, tenant_id, scored, int(time.time() * 1000))
