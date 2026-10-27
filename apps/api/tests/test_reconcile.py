"""Reconciliation of CUR totals against Cost Explorer."""

import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fake_aws import FakeClient, FakeSessionFactory, client_error, healthy_clients
from httpx import AsyncClient
from test_ingestion import SEPT, connected, ingest, summary

from cloudoptics.connectors import service as connectors
from cloudoptics.ingestion import reconcile, service
from cloudoptics.ingestion.source import LocalExportSource
from cur_fixture import generate, write_export


class FakeCostExplorer:
    def __init__(self, totals: dict[str, float]) -> None:
        self.totals = totals
        self.requests: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": [account],
                            "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"}},
                        }
                        for account, v in self.totals.items()
                    ]
                }
            ]
        }


@pytest.fixture
def bucket(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "bucket"
    root.mkdir()
    service.set_source_factory(lambda conn, tenant_id: LocalExportSource(root))
    yield root
    service.set_source_factory(None)


@pytest.fixture
def aws():  # type: ignore[no-untyped-def]
    factory = FakeSessionFactory(healthy_clients())
    connectors.set_session_factory(factory)
    yield factory
    connectors.set_session_factory(None)


def test_compare_tolerates_one_percent_and_small_gaps() -> None:
    ok = reconcile.compare({"a": 1000.0, "b": 0.5}, {"a": 1009.0, "b": 0.0})
    assert ok.status == "ok"
    assert ok.difference_pct == pytest.approx(-0.8417, abs=1e-3)

    off = reconcile.compare({"a": 1000.0, "b": 50.0}, {"a": 1000.0, "b": 40.0})
    assert off.status == "mismatch"
    assert off.accounts == [{"accountId": "b", "cur": 50.0, "reference": 40.0}]
    assert reconcile.compare({}, {}).difference_pct is None


async def test_reconcile_after_ingest(client: AsyncClient, login, owner_sql, bucket, aws) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    tid, cid = uuid.UUID(tenant), uuid.UUID(connection)
    totals = {
        str(a).strip(): float(v)
        for a, v in owner_sql(
            "SELECT account_id, sum(billed_cost_usd) FROM cost_daily WHERE connection_id = %s "
            "GROUP BY account_id",
            connection,
        )
    }

    ce = FakeCostExplorer(totals)
    aws.clients["ce"] = ce  # type: ignore[assignment]
    assert await reconcile.reconcile_period(tid, cid, date(2026, 9, 1)) == "ok"
    assert ce.requests[-1]["TimePeriod"] == {"Start": "2026-09-01", "End": "2026-10-01"}
    assert aws.calls[-1][2].endswith("-reconcile")
    body = await summary(client, tenant, headers, "2026-10-02")
    assert body["reconciliation"]["status"] == "ok"
    assert body["reconciliation"]["month"] == "2026-09"

    # Cost Explorer reports 5% more for one account: a mismatch, shown on the summary.
    account = next(iter(totals))
    ce.totals = {**totals, account: totals[account] * 1.05 + 2}
    assert await reconcile.reconcile_period(tid, cid, date(2026, 9, 1)) == "mismatch"
    rec = (await summary(client, tenant, headers, "2026-10-02"))["reconciliation"]
    assert rec["status"] == "mismatch"
    assert rec["accountsMismatched"] == 1
    assert rec["differencePct"] < 0

    rows = owner_sql(
        "SELECT status, est_customer_api_cost_usd FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'cost.reconcile'",
        connection,
    )
    assert [s for s, _ in rows] == ["succeeded", "succeeded"]

    aws.clients["ce"] = FakeClient(get_cost_and_usage=client_error("AccessDeniedException"))
    assert await reconcile.reconcile_period(tid, cid, date(2026, 9, 1)) == "failed"
    assert owner_sql(
        "SELECT status, error_code FROM cost_reconciliation WHERE connection_id = %s", connection
    ) == [("failed", "AccessDeniedException")]
    # A failed check does not hide data; the summary just reports it.
    assert (await summary(client, tenant, headers, "2026-10-02"))["reconciliation"][
        "status"
    ] == "failed"

    assert await reconcile.reconcile_period(tid, cid, date(2026, 8, 1)) == "skipped"


async def test_ingest_job_queues_reconciliation(
    client: AsyncClient, login, owner_sql, bucket, monkeypatch
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.ingestion import jobs

    tenant, connection, _ = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    queued: list[dict[str, str]] = []

    async def fake_defer(app, name, lock, **kwargs):  # type: ignore[no-untyped-def]
        queued.append({"name": name, "lock": lock, **kwargs})

    from cloudoptics.anomalies import jobs as anomaly_jobs
    from cloudoptics.budgets import jobs as budget_jobs

    monkeypatch.setattr(jobs, "defer_once", fake_defer)
    monkeypatch.setattr(budget_jobs, "defer_once", fake_defer)
    monkeypatch.setattr(anomaly_jobs, "defer_once", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await jobs.ingest(tenant, connection) == "succeeded"
    assert queued == [
        {
            "name": jobs.RECONCILE_TASK,
            "lock": f"reconcile:{connection}:2026-09",
            "tenant_id": tenant,
            "connection_id": connection,
            "billing_period": "2026-09-01",
        },
        # New bills: the workspace's budgets and anomalies are checked straight away.
        {"name": budget_jobs.EVALUATE_TASK, "lock": f"budgets:{tenant}", "tenant_id": tenant},
        {"name": anomaly_jobs.DETECT_TASK, "lock": f"anomalies:{tenant}", "tenant_id": tenant},
    ]
