"""Reconcile loaded billing data with AWS's own totals (WBS IN-6, PRD FR-ING-07).

After an export version loads, the billed (unblended) totals in ``cost_daily`` for that
billing period are compared, overall and per account, with Cost Explorer's
``UnblendedCost`` for the same days: the whole month once it is closed, otherwise up to the
last day the CUR covers. A difference over 1% (and at least USD 1, so tiny accounts do not
trip it) is a mismatch: the Overview shows a "being reconciled" banner and an internal
alert is logged. One ``GetCostAndUsage`` call per check (USD 0.01).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.aws import session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.connectors.validation import CE_CALL_COST_USD
from cloudoptics.ingestion.explorer import ExplorerError, amount, pages
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7

log = logging.getLogger(__name__)

TOLERANCE = 0.01
MIN_DIFFERENCE_USD = 1.0

Status = Literal["ok", "mismatch", "failed", "skipped"]


@dataclass
class Comparison:
    cur_total: float
    reference: float
    accounts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def difference_pct(self) -> float | None:
        if self.reference == 0:
            return None if self.cur_total == 0 else 100.0
        return round((self.cur_total - self.reference) / abs(self.reference) * 100, 4)

    @property
    def status(self) -> Literal["ok", "mismatch"]:
        return "mismatch" if _differs(self.cur_total, self.reference) or self.accounts else "ok"


def _differs(cur: float, reference: float) -> bool:
    gap = abs(cur - reference)
    return gap >= MIN_DIFFERENCE_USD and gap > abs(reference) * TOLERANCE


def explorer_totals(ce: Any, start: date, end: date) -> dict[str, float]:
    """Cost Explorer's unblended cost per linked account for ``[start, end)``."""
    results, _ = pages(
        ce,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
    )
    totals: dict[str, float] = {}
    for result in results:
        for group in result.get("Groups", []):
            account = group["Keys"][0]
            totals[account] = totals.get(account, 0.0) + amount(group["Metrics"], "UnblendedCost")
    return totals


def compare(cur: dict[str, float], reference: dict[str, float]) -> Comparison:
    result = Comparison(cur_total=sum(cur.values()), reference=sum(reference.values()))
    for account in sorted(set(cur) | set(reference)):
        ours, theirs = cur.get(account, 0.0), reference.get(account, 0.0)
        if _differs(ours, theirs):
            result.accounts.append(
                {"accountId": account, "cur": round(ours, 2), "reference": round(theirs, 2)}
            )
    return result


async def reconcile_period(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
) -> Status:
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        rows = (
            await session.execute(
                text(
                    "SELECT account_id, sum(billed_cost_usd) AS billed, max(usage_date) AS last, "
                    "max(data_version) AS version FROM cost_daily WHERE connection_id = :c "
                    "AND billing_period = :p GROUP BY account_id"
                ),
                {"c": connection_id, "p": billing_period},
            )
        ).all()
    if not rows:
        return "skipped"
    month_end = (billing_period + timedelta(days=32)).replace(day=1)
    through = min(max(r.last for r in rows), month_end - timedelta(days=1))
    cur = {r.account_id: float(r.billed) for r in rows}
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)

    def _reference() -> dict[str, float]:
        aws = get_session_factory().assume(
            conn.role_arn, external_id, session_name(tenant_id, "reconcile")
        )
        return explorer_totals(aws.client("ce"), billing_period, through + timedelta(days=1))

    comparison: Comparison | None = None
    error: str | None = None
    try:
        comparison = compare(cur, await run_in_threadpool(_reference))
    except ClientError as exc:
        error = str(exc.response.get("Error", {}).get("Code", "ClientError"))
    except (BotoCoreError, ExplorerError, KeyError, ValueError) as exc:
        error = str(exc) if isinstance(exc, ExplorerError) else type(exc).__name__

    status: Status = comparison.status if comparison else "failed"
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            text(
                "INSERT INTO cost_reconciliation (tenant_id, connection_id, billing_period, "
                "export_version, through_date, status, cur_total_usd, reference_usd, "
                "difference_pct, accounts, error_code, checked_at) VALUES (:t, :c, :p, :v, "
                ":through, :status, :cur, :ref, :pct, CAST(:accounts AS jsonb), :code, now()) "
                "ON CONFLICT (connection_id, billing_period) DO UPDATE SET "
                "export_version = EXCLUDED.export_version, through_date = EXCLUDED.through_date, "
                "status = EXCLUDED.status, cur_total_usd = EXCLUDED.cur_total_usd, "
                "reference_usd = EXCLUDED.reference_usd, "
                "difference_pct = EXCLUDED.difference_pct, accounts = EXCLUDED.accounts, "
                "error_code = EXCLUDED.error_code, checked_at = now()"
            ),
            {
                "t": tenant_id,
                "c": connection_id,
                "p": billing_period,
                "v": max(r.version for r in rows),
                "through": through,
                "status": status,
                "cur": round(comparison.cur_total, 6) if comparison else None,
                "ref": round(comparison.reference, 6) if comparison else None,
                "pct": comparison.difference_pct if comparison else None,
                "accounts": json.dumps(comparison.accounts if comparison else []),
                "code": error,
            },
        )
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at, finished_at, error_code, est_customer_api_cost_usd) VALUES (:id, "
                ":t, :c, 'cost.reconcile', CAST(:scope AS jsonb), :status, now(), now(), "
                ":code, :cost)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps({"billing_period": billing_period.isoformat()}),
                "status": "failed" if error else "succeeded",
                "code": error,
                "cost": CE_CALL_COST_USD,
            },
        )
    if status == "mismatch" and comparison:
        # Internal alert (FR-ING-07) until alert routing exists (WBS OPS-7).
        log.warning(
            "reconciliation mismatch tenant=%s connection=%s period=%s cur=%.2f reference=%.2f "
            "accounts=%d",
            tenant_id,
            connection_id,
            billing_period,
            comparison.cur_total,
            comparison.reference,
            len(comparison.accounts),
        )
    return status
