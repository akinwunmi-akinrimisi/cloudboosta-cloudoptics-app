"""Provisional costs from Cost Explorer, and the daily and breakdown cost queries."""

import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fake_aws import FakeClient, FakeSessionFactory, client_error, healthy_clients
from httpx import AsyncClient
from test_ingestion import SEPT, connected, ingest, summary

from cloudoptics.connectors import service as connectors
from cloudoptics.ingestion import explorer, service
from cloudoptics.ingestion.source import LocalExportSource
from cur_fixture import generate, write_export


@pytest.fixture
def bucket(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "bucket"
    root.mkdir()
    service.set_source_factory(lambda conn, tenant_id: LocalExportSource(root))
    yield root
    service.set_source_factory(None)


class FakeCostExplorer:
    """Answers the daily record-type query (in two pages) and the monthly service query."""

    def __init__(self, unit: str = "USD") -> None:
        self.unit = unit
        self.requests: list[dict[str, Any]] = []

    def _metrics(self, amortised: float, unblended: float | None = None) -> dict[str, Any]:
        return {
            "AmortizedCost": {"Amount": str(amortised), "Unit": self.unit},
            "UnblendedCost": {"Amount": str(unblended or amortised), "Unit": self.unit},
        }

    def get_cost_and_usage(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if request["Granularity"] == "MONTHLY":
            return {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                                "Metrics": self._metrics(60),
                            },
                            {
                                "Keys": ["Amazon Simple Storage Service"],
                                "Metrics": self._metrics(25),
                            },
                            {"Keys": ["Tax"], "Metrics": self._metrics(5)},
                            {
                                "Keys": ["AWS Key Management Service"],
                                "Metrics": self._metrics(0.001),
                            },
                        ]
                    }
                ]
            }
        start = request["TimePeriod"]["Start"]
        if "NextPageToken" not in request:
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": start},
                        "Groups": [
                            {"Keys": ["Usage"], "Metrics": self._metrics(40, 45)},
                            {"Keys": ["Credit"], "Metrics": self._metrics(-2)},
                        ],
                    }
                ],
                "NextPageToken": "page-2",
            }
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": start[:8] + "02"},
                    "Groups": [
                        {"Keys": ["Usage"], "Metrics": self._metrics(50)},
                        {"Keys": ["Tax"], "Metrics": self._metrics(2)},
                    ],
                }
            ]
        }


def test_fetch_snapshot_sums_pages_and_splits_credits() -> None:
    ce = FakeCostExplorer()
    snap = explorer.fetch_snapshot(ce, date(2026, 10, 3))
    assert snap is not None
    assert snap.through_date == date(2026, 10, 2)
    assert snap.effective == pytest.approx(90)
    assert snap.billed == pytest.approx(95)
    assert snap.credits == pytest.approx(-2)
    assert snap.daily == {date(2026, 10, 1): 38, date(2026, 10, 2): 52}
    assert set(snap.by_service) == {
        "Amazon Elastic Compute Cloud - Compute",
        "Amazon Simple Storage Service",
        "Tax",
    }
    assert snap.calls == 3
    assert ce.requests[0]["TimePeriod"] == {"Start": "2026-10-01", "End": "2026-10-03"}


def test_fetch_snapshot_waits_for_a_full_day_and_refuses_other_currencies() -> None:
    assert explorer.fetch_snapshot(FakeCostExplorer(), date(2026, 10, 1)) is None
    with pytest.raises(explorer.ExplorerError, match="unsupported_currency:EUR"):
        explorer.fetch_snapshot(FakeCostExplorer(unit="EUR"), date(2026, 10, 3))


@pytest.fixture
def cost_explorer():  # type: ignore[no-untyped-def]
    ce = FakeCostExplorer()
    clients = healthy_clients()
    clients["ce"] = ce  # type: ignore[assignment]
    factory = FakeSessionFactory(clients)
    connectors.set_session_factory(factory)
    yield factory
    connectors.set_session_factory(None)


async def get(client: AsyncClient, path: str, headers: dict[str, str], **params: Any) -> Any:
    r = await client.get(path, params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_provisional_costs_until_the_first_report(
    client: AsyncClient, login, owner_sql, bucket, cost_explorer
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    tid, cid = uuid.UUID(tenant), uuid.UUID(connection)
    today = date(2026, 9, 3)
    assert await explorer.refresh_snapshot(tid, cid, today) == "succeeded"
    assert cost_explorer.calls[-1][2].endswith("-ce")
    # A second refresh within six hours makes no Cost Explorer call.
    assert await explorer.refresh_snapshot(tid, cid, today) == "fresh"

    body = await summary(client, tenant, headers, "2026-09-03")
    assert body["status"] == "provisional"
    assert body["source"] == "cost_explorer"
    assert body["dataThrough"] == "2026-09-02"
    assert body["monthToDate"] == {"effective": 90.0, "billed": 95.0, "credits": -2.0}
    assert body["forecast"]["daysCovered"] == 2
    assert body["forecast"]["amount"] == pytest.approx(90 / 2 * 30)

    trend = await get(client, f"/v1/tenants/{tenant}/costs/daily", headers, today="2026-09-03")
    assert trend == {
        "source": "cost_explorer",
        "currency": "USD",
        "points": [{"date": "2026-09-01", "amount": 38.0}, {"date": "2026-09-02", "amount": 52.0}],
    }
    services = await get(
        client, f"/v1/tenants/{tenant}/costs/breakdown", headers, today="2026-09-03"
    )
    assert services["source"] == "cost_explorer"
    assert services["total"] == 85.0
    assert [i["label"] for i in services["items"]] == [
        "Amazon Elastic Compute Cloud - Compute",
        "Amazon Simple Storage Service",
    ]
    accounts = await get(
        client,
        f"/v1/tenants/{tenant}/costs/breakdown",
        headers,
        today="2026-09-03",
        dimension="account",
    )
    assert accounts["items"] == []

    runs = owner_sql(
        "SELECT status, est_customer_api_cost_usd FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'ce.snapshot'",
        connection,
    )
    assert [(s, float(c)) for s, c in runs] == [("succeeded", 0.03)]

    # Once the month's CUR is loaded, it replaces the provisional figures and no more calls.
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    assert (await summary(client, tenant, headers, "2026-09-03"))["source"] == "cur"
    assert await explorer.refresh_snapshot(tid, cid, today, force=True) == "cur_loaded"


async def test_snapshot_failures_are_recorded(
    client: AsyncClient, login, owner_sql, cost_explorer
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    cost_explorer.clients["ce"] = FakeClient(
        get_cost_and_usage=client_error("AccessDeniedException")
    )
    status = await explorer.refresh_snapshot(
        uuid.UUID(tenant), uuid.UUID(connection), date(2026, 9, 3)
    )
    assert status == "failed"
    assert owner_sql(
        "SELECT status, error_code FROM sync_run WHERE connection_id = %s AND job_type = "
        "'ce.snapshot'",
        connection,
    ) == [("failed", "AccessDeniedException")]
    assert (await summary(client, tenant, headers, "2026-09-03"))["status"] == "waiting"


async def test_daily_and_breakdown_from_the_report(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)

    trend = await get(
        client, f"/v1/tenants/{tenant}/costs/daily", headers, today="2026-10-05", days=14
    )
    assert trend["source"] == "cur"
    assert len(trend["points"]) == 14
    assert trend["points"][-1]["date"] == "2026-09-30"
    assert all(p["amount"] != 0 for p in trend["points"])

    for dimension in ("service", "account", "region"):
        body = await get(
            client,
            f"/v1/tenants/{tenant}/costs/breakdown",
            headers,
            today="2026-09-30",
            dimension=dimension,
        )
        assert body["source"] == "cur"
        assert body["items"], dimension
        assert sum(i["amount"] for i in body["items"]) == pytest.approx(body["total"], abs=0.05)
        assert sum(i["share"] for i in body["items"]) == pytest.approx(1, abs=0.01)
    labels = [i["label"] for i in body["items"]]  # region
    assert labels
    accounts = await get(
        client,
        f"/v1/tenants/{tenant}/costs/breakdown",
        headers,
        today="2026-09-30",
        dimension="account",
    )
    assert "demo-dev" in [i["label"] for i in accounts["items"]]

    empty = await get(client, f"/v1/tenants/{tenant}/costs/daily", headers, today="2026-08-01")
    assert empty == {"source": None, "currency": "USD", "points": []}


async def test_daily_poll_queues_a_snapshot_per_usable_connection(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.ingestion import jobs

    _, healthy, _ = await connected(client, login, owner_sql)
    _, no_export, _ = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET cur_bucket = NULL WHERE id = %s", no_export)
    _, gone, _ = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET status = 'disconnected' WHERE id = %s", gone)

    queued: list[str] = []

    async def fake_defer(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append(connection_id)

    monkeypatch.setattr(jobs, "defer_snapshot", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    count = await jobs.poll_explorer(0)
    assert {healthy, no_export} <= set(queued)
    assert gone not in queued
    assert count == len(queued)
