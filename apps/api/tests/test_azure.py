"""The Azure plug-in (ADR-020) against the fake Azure (tests/fake_azure.py): credentials,
connect and validation per permission, ownership, FOCUS ingestion with native currency,
reconciliation, the snapshot, inventory, metrics, Advisor and the AZ rules."""

from __future__ import annotations

import gzip
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fake_azure import (
    CLIENT_ID,
    CONTAINER,
    DIRECTORY,
    FOLDER,
    STORAGE,
    SUBSCRIPTION,
    VM_WEB,
    FakeAzure,
    focus_rows,
    install,
    new_certificate,
)
from httpx import AsyncClient
from test_data_deletion import workspace

from cloudoptics import providers
from cloudoptics.analysis.settings import RULE_IDS
from cloudoptics.providers.azure import billing, inventory
from cloudoptics.providers.azure import connection as azconn
from cloudoptics.providers.azure.credentials import (
    AzureAuthError,
    AzureTokens,
    CertificateAssertion,
)

Sql = Callable[..., list[tuple[Any, ...]]]
TODAY = datetime.now(UTC).date()
THIS_MONTH = TODAY.replace(day=1)
LAST_MONTH = (THIS_MONTH - timedelta(days=1)).replace(day=1)
THROUGH = max(THIS_MONTH, TODAY - timedelta(days=1))


@pytest.fixture
def azure() -> Iterator[FakeAzure]:
    fake, undo = install()
    yield fake
    undo()


def body(sub: str = SUBSCRIPTION, **overrides: Any) -> dict[str, Any]:
    return {
        "directoryId": DIRECTORY,
        "scopeType": "subscription",
        "subscriptionIds": [sub],
        "storageAccount": STORAGE,
        "container": CONTAINER,
        "exportPath": FOLDER,
        **overrides,
    }


async def connect(
    client: AsyncClient, tenant: str, headers: dict, sub: str, **overrides: Any
) -> dict:
    r = await client.post(
        f"/v1/tenants/{tenant}/providers/azure/connections",
        json=body(sub, **overrides),
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


def ready(
    fake: FakeAzure, code: str, *, months: tuple[date, ...] = (LAST_MONTH, THIS_MONTH)
) -> None:
    """Everything the customer does: roles, the tag, the export and its runs."""
    fake.grant_all()
    fake.add_storage(code)
    fake.add_export()
    fake.add_resources(datetime.now(UTC))
    for month in months:
        end = (month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        fake.deliver(month, min(end, THROUGH))


async def validate(client: AsyncClient, tenant: str, headers: dict, cid: str) -> dict:
    r = await client.post(
        f"/v1/tenants/{tenant}/providers/azure/connections/{cid}/validate", headers=headers
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


def checks(conn: dict) -> dict[str, str]:
    return {c["id"]: c["status"] for c in conn["permissionChecks"]}


# --- registry and credentials -------------------------------------------------------------


def test_azure_is_registered_with_its_capabilities_and_rules() -> None:
    azure = providers.get("azure")
    assert azure.account_noun == "subscription" and azure.billing_currencies is None
    assert azure.valid_account_id(SUBSCRIPTION) and azure.valid_account_id("12345678")
    assert not azure.valid_account_id("bad id with spaces")
    for name in ("connect", "credentials", "accounts", "billing_source", "reconcile",
                 "inventory", "metrics", "native_recs"):  # fmt: skip
        assert azure.capability(name) is not None, name
    assert azure.prices is None and azure.commitments is None
    ids = [r for s in azure.rules for r in s.rule_ids]
    assert len(ids) >= 5 and set(ids) <= set(RULE_IDS)
    assert {s.provider for s in azure.rules} == {"azure"}


async def test_assertion_is_accepted_and_tokens_are_cached() -> None:
    key, cert = new_certificate()
    fake = FakeAzure(certificate=cert)
    tokens = AzureTokens(
        CLIENT_ID, CertificateAssertion(key, cert), fake.authority,
        httpx.AsyncClient(transport=fake.transport()),
    )  # fmt: skip
    first = await tokens.token(DIRECTORY, "https://management.azure.com")
    assert first == f"tok|{DIRECTORY}|https://management.azure.com"
    assert await tokens.token(DIRECTORY, "https://management.azure.com") == first
    assert fake.tokens_issued == 1
    await tokens.token(DIRECTORY, "https://storage.azure.com")
    assert fake.tokens_issued == 2
    tokens.forget(DIRECTORY)
    await tokens.token(DIRECTORY, "https://storage.azure.com")
    assert fake.tokens_issued == 3


async def test_token_errors_say_what_is_wrong() -> None:
    key, cert = new_certificate()
    other_key, other_cert = new_certificate()
    fake = FakeAzure(certificate=cert, directories={DIRECTORY: False})
    http = httpx.AsyncClient(transport=fake.transport())

    def tokens(source: Any, client_id: str = CLIENT_ID) -> AzureTokens:
        return AzureTokens(client_id, source, fake.authority, http)

    with pytest.raises(AzureAuthError) as no_consent:
        await tokens(CertificateAssertion(key, cert)).token(DIRECTORY, "https://x")
    assert no_consent.value.kind == "consent" and no_consent.value.code == "700016"
    assert "Trace ID" not in str(no_consent.value)
    with pytest.raises(AzureAuthError) as unknown:
        await tokens(CertificateAssertion(key, cert)).token(str(uuid.uuid4()), "https://x")
    assert unknown.value.kind == "directory"
    with pytest.raises(AzureAuthError) as wrong_cert:
        await tokens(CertificateAssertion(other_key, other_cert)).token(DIRECTORY, "https://x")
    assert wrong_cert.value.kind == "credential"
    with pytest.raises(AzureAuthError) as missing:
        await tokens(None).token(DIRECTORY, "https://x")
    assert missing.value.code == "not_configured"


def test_certificate_and_key_must_match() -> None:
    from cryptography.hazmat.primitives import serialization

    key, cert = new_certificate()
    other, _ = new_certificate()
    pem_cert = cert.public_bytes(serialization.Encoding.PEM).decode()

    def pem(k: Any) -> str:
        return k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    loaded = CertificateAssertion.from_pem(pem_cert, pem(key))
    assert loaded.not_after > datetime.now(UTC).timestamp()
    import base64

    assert (
        CertificateAssertion.from_pem(
            base64.b64encode(pem_cert.encode()).decode(),
            base64.b64encode(pem(key).encode()).decode(),
        ).thumbprint
        == loaded.thumbprint
    )
    with pytest.raises(ValueError, match="does not match"):
        CertificateAssertion.from_pem(pem_cert, pem(other))


# --- connect ------------------------------------------------------------------------------


async def test_create_returns_the_set_up_steps(client: AsyncClient, login, azure) -> None:
    tenant, headers = await workspace(client, login, "Azure Setup Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    conn = setup["connection"]
    assert conn["provider"] == "azure" and conn["status"] == "pending"
    assert conn["scope"] == f"/subscriptions/{azure.subscription}"
    assert setup["clientId"] == CLIENT_ID
    assert setup["consentUrl"].startswith(
        f"https://login.microsoftonline.com/{DIRECTORY}/adminconsent?"
    )
    assert f"state={conn['id']}" in setup["consentUrl"]
    code = setup["ownershipCode"]
    assert code.startswith("co-") and code in setup["roleAssignmentCommand"]
    for role in ("Reader", "Cost Management Reader", "Storage Blob Data Reader"):
        assert role in setup["roleAssignmentCommand"]
    assert "az tag update" in setup["roleAssignmentCommand"]
    assert any(code in s for s in setup["portalRoleSteps"])
    assert any("FOCUS" in s for s in setup["exportSteps"])
    # The same steps again, with the same code.
    again = await client.get(
        f"/v1/tenants/{tenant}/providers/azure/connections/{conn['id']}", headers=headers
    )
    assert again.status_code == 200 and again.json()["ownershipCode"] == code
    # It is in the generic list, as an Azure connection.
    listed = await client.get(f"/v1/tenants/{tenant}/connections", headers=headers)
    row = next(c for c in listed.json() if c["id"] == conn["id"])
    assert row["provider"] == "azure" and row["awsAccountId"] == azure.subscription
    # Twice: refused.
    dup = await client.post(
        f"/v1/tenants/{tenant}/providers/azure/connections",
        json=body(azure.subscription),
        headers=headers,
    )
    assert dup.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"directoryId": "not-a-guid"},
        {"subscriptionIds": []},
        {"subscriptionIds": [SUBSCRIPTION, str(uuid.uuid4())]},
        {"subscriptionIds": ["nope"]},
        {"scopeType": "billing_account", "subscriptionIds": []},
        {"storageAccount": "Has-Caps"},
        {"container": "a--b"},
        {"exportPath": "../up"},
    ],
)
async def test_create_rejects_bad_input(client: AsyncClient, login, azure, overrides) -> None:
    tenant, headers = await workspace(client, login, "Azure Input Ltd")
    r = await client.post(
        f"/v1/tenants/{tenant}/providers/azure/connections",
        json=body(azure.subscription, **overrides),
        headers=headers,
    )
    assert r.status_code == 422, r.text


async def test_only_admins_connect(client: AsyncClient, login, owner_sql: Sql, azure) -> None:
    from test_data_deletion import member

    tenant, _ = await workspace(client, login, "Azure Roles Ltd")
    viewer = await member(client, login, owner_sql, tenant, "viewer")
    r = await client.post(
        f"/v1/tenants/{tenant}/providers/azure/connections",
        json=body(azure.subscription),
        headers=viewer,
    )
    assert r.status_code == 403


# --- validation, one permission at a time -------------------------------------------------


async def test_each_missing_permission_is_its_own_check(client: AsyncClient, login, azure) -> None:
    tenant, headers = await workspace(client, login, "Azure Checks Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid, code = setup["connection"]["id"], setup["ownershipCode"]

    azure.directories[DIRECTORY] = False
    conn = await validate(client, tenant, headers, cid)
    assert conn["status"] == "failed" and checks(conn) == {"consent": "error"}
    assert "Global Administrator" in conn["permissionChecks"][0]["hint"]

    azure.directories[DIRECTORY] = True
    conn = await validate(client, tenant, headers, cid)
    assert conn["status"] == "failed"
    assert checks(conn)["reader"] == "error" and checks(conn)["cost_reader"] == "error"
    assert checks(conn)["export"] == "skipped" and checks(conn)["ownership"] == "skipped"

    ready(azure, "co-wrong", months=())
    conn = await validate(client, tenant, headers, cid)
    assert conn["status"] == "failed" and checks(conn)["ownership"] == "error"
    assert checks(conn)["export"] == "ok" and checks(conn)["blob_reader"] == "ok"
    assert checks(conn)["export_data"] == "warning"

    azure.storage[STORAGE].tags["cloudoptics-verify"] = code
    azure.roles = {r for r in azure.roles if r[1] != "Storage Blob Data Reader"}
    conn = await validate(client, tenant, headers, cid)
    assert conn["status"] == "degraded"
    assert checks(conn)["ownership"] == "ok" and checks(conn)["blob_reader"] == "error"

    azure.grant_all()
    conn = await validate(client, tenant, headers, cid)
    assert conn["status"] == "healthy" and checks(conn)["export_data"] == "warning"


async def test_an_export_into_another_container_or_folder(
    client: AsyncClient, login, azure
) -> None:
    tenant, headers = await workspace(client, login, "Azure Export Ltd")
    setup = await connect(client, tenant, headers, azure.subscription, exportPath="elsewhere")
    ready(azure, setup["ownershipCode"], months=())
    conn = await validate(client, tenant, headers, setup["connection"]["id"])
    assert checks(conn)["export"] == "warning" and conn["status"] == "healthy"
    other = await connect(client, tenant, headers, str(uuid.uuid4()), container="other")
    conn = await validate(client, tenant, headers, other["connection"]["id"])
    assert checks(conn)["export"] in ("error", "skipped") and conn["status"] == "failed"


async def test_a_second_workspace_cannot_take_a_verified_subscription(
    client: AsyncClient, login, azure
) -> None:
    a, a_headers = await workspace(client, login, "Azure Owner Ltd")
    b, b_headers = await workspace(client, login, "Azure Other Ltd")
    first = await connect(client, a, a_headers, azure.subscription)
    ready(azure, first["ownershipCode"])
    assert (await validate(client, a, a_headers, first["connection"]["id"]))["status"] == "healthy"

    # B names A's subscription and storage: the app can read them, but B's code is not there.
    second = await connect(client, b, b_headers, azure.subscription)
    conn = await validate(client, b, b_headers, second["connection"]["id"])
    assert conn["status"] == "failed" and checks(conn)["ownership"] == "error"
    r = await client.get(f"/v1/tenants/{b}/costs/summary", headers=b_headers)
    assert r.status_code == 200 and not r.json().get("monthToDateUsd")
    # Even with B's code on the account (say, set by mistake), A keeps it.
    azure.storage[STORAGE].tags["cloudoptics-verify"] = second["ownershipCode"]
    conn = await validate(client, b, b_headers, second["connection"]["id"])
    assert conn["status"] == "failed"
    assert "another Cloudoptics workspace" in next(
        c["message"] for c in conn["permissionChecks"] if c["id"] == "ownership"
    )


# --- the whole journey --------------------------------------------------------------------


async def test_connect_loads_costs_resources_and_recommendations(
    client: AsyncClient, login, owner_sql: Sql, azure
) -> None:
    tenant, headers = await workspace(client, login, "Azure Journey Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid = setup["connection"]["id"]
    ready(azure, setup["ownershipCode"])
    conn = await validate(client, tenant, headers, cid)
    assert conn["status"] == "healthy", json.dumps(conn["permissionChecks"])

    rows = owner_sql(
        "SELECT provider, billing_currency, count(*), sum(billed_cost), sum(billed_cost_usd) "
        "FROM cost_daily WHERE connection_id = %s GROUP BY 1, 2",
        cid,
    )
    assert len(rows) == 1
    prov, ccy, n, native, usd = rows[0]
    assert prov == "azure" and ccy == "EUR" and n > 0
    # No staff EUR rate: Microsoft's own rate from the export (1.08 USD per EUR).
    assert float(usd) == pytest.approx(float(native) * 1.08, rel=1e-4)
    batches = owner_sql(
        "SELECT status, checks FROM ingest_batch WHERE connection_id = %s ORDER BY billing_period",
        cid,
    )
    assert [b[0] for b in batches] == ["succeeded", "succeeded"]
    fx_check = next(c for c in batches[0][1] if c["id"] == "fx")
    assert fx_check["status"] == "warning"
    credits = owner_sql(
        "SELECT sum(billed_cost) FROM cost_daily WHERE connection_id = %s "
        "AND charge_category = 'Credit'",
        cid,
    )
    assert float(credits[0][0]) == pytest.approx(-10.0)
    accounts = owner_sql(
        "SELECT provider, provider_account_id, name FROM cloud_account WHERE connection_id = %s",
        cid,
    )
    assert accounts == [("azure", azure.subscription, "Production")]
    recon = owner_sql(
        "SELECT status FROM cost_reconciliation WHERE connection_id = %s ORDER BY billing_period",
        cid,
    )
    assert [r[0] for r in recon] == ["ok", "ok"]
    resources = dict(
        owner_sql(
            "SELECT name, state FROM resource WHERE connection_id = %s AND provider = 'azure'", cid
        )
    )
    assert resources["vm-stopped"] == "stopped" and resources["disk-loose"] == "Unattached"
    assert resources["pip-idle"] == "unassociated"
    web = owner_sql(
        "SELECT config FROM resource WHERE connection_id = %s AND name = 'vm-web'", cid
    )[0][0]
    assert web["advisor"][0]["targetSku"] == "Standard_D2s_v5" and web["monthlyCostUsd"] > 0
    metrics = owner_sql(
        "SELECT count(*) FROM resource_metric m JOIN resource r ON r.id = m.resource_id "
        "WHERE r.connection_id = %s AND m.metric = 'cpu_max'",
        cid,
    )
    assert metrics[0][0] >= 14

    recs = owner_sql(
        "SELECT rule_id, fingerprint, fix, savings_basis FROM recommendation "
        "WHERE provider = 'azure' AND tenant_id = %s",
        tenant,
    )
    by_rule = {r[0]: r for r in recs}
    assert set(by_rule) == {
        "AZ1_unattached_disk", "AZ2_idle_public_ip", "AZ3_stopped_vm", "AZ4_old_snapshot",
        "AZ5_underused_vm",
    }  # fmt: skip
    assert by_rule["AZ1_unattached_disk"][1].startswith(
        f"azure:AZ1_unattached_disk:{azure.subscription}:"
    )
    assert all(r[3] == "effective_rate" for r in recs)
    assert "az vm resize" in by_rule["AZ5_underused_vm"][2]["steps"][0]
    assert "az vm deallocate" in by_rule["AZ3_stopped_vm"][2]["steps"][0]

    # The Overview's numbers come from the same tables.
    r = await client.get(f"/v1/tenants/{tenant}/costs/summary", headers=headers)
    assert r.status_code == 200, r.text
    listed = await client.get(f"/v1/tenants/{tenant}/recommendations", headers=headers)
    assert listed.status_code == 200
    assert any(i["provider"] == "azure" for i in listed.json())


async def test_ingest_is_idempotent_and_a_new_run_replaces_the_month(
    client: AsyncClient, login, owner_sql: Sql, azure
) -> None:
    tenant, headers = await workspace(client, login, "Azure Runs Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid = setup["connection"]["id"]
    ready(azure, setup["ownershipCode"], months=(LAST_MONTH,))
    await validate(client, tenant, headers, cid)

    def total() -> float:
        return float(
            owner_sql("SELECT sum(billed_cost) FROM cost_daily WHERE connection_id = %s", cid)[0][0]
        )

    before = total()
    again = await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))
    assert again.status == "up_to_date" and total() == before

    # A restated month (new run, one day less, gzipped CSV): replaces, never adds.
    end = (LAST_MONTH + timedelta(days=32)).replace(day=1) - timedelta(days=2)
    azure.deliver(LAST_MONTH, end, fmt="csv.gz", submitted=datetime.now(UTC) + timedelta(hours=1))
    result = await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))
    assert result.status == "succeeded" and result.loaded_periods == [LAST_MONTH]
    assert 0 < total() < before
    statuses = owner_sql(
        "SELECT status FROM ingest_batch WHERE connection_id = %s ORDER BY created_at", cid
    )
    assert [s[0] for s in statuses] == ["superseded", "succeeded"]

    # A run that fails a check is recorded; the previous one stays live.
    kept = total()
    azure.deliver(
        LAST_MONTH, end, manifest_rows=1, submitted=datetime.now(UTC) + timedelta(hours=2)
    )
    result = await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))
    assert result.status == "failed" and result.rejected and total() == kept

    # Reload reads the newest run again even though it was seen (the failing one: kept out).
    reloaded = await providers.get("azure").billing_source.reload(
        uuid.UUID(tenant), uuid.UUID(cid), LAST_MONTH
    )
    assert reloaded.status == "failed" and [p.outcome for p in reloaded.periods] == ["failed"]
    assert total() == kept

    # The shared Reload bills route goes through the plug-in too.
    azure.deliver(LAST_MONTH, end, submitted=datetime.now(UTC) + timedelta(hours=3))
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/{cid}/reload",
        json={"period": f"{LAST_MONTH:%Y-%m}"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded" and total() == pytest.approx(kept)
    missing = await providers.get("azure").billing_source.reload(
        uuid.UUID(tenant), uuid.UUID(cid), date(2020, 1, 1)
    )
    assert [p.outcome for p in missing.periods] == ["not_in_bucket"]


async def test_a_staff_rate_wins_and_no_rate_blocks_the_month(
    client: AsyncClient, login, owner_sql: Sql, azure
) -> None:
    tenant, headers = await workspace(client, login, "Azure FX Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid = setup["connection"]["id"]
    ready(azure, setup["ownershipCode"], months=())
    await validate(client, tenant, headers, cid)
    staff = owner_sql("SELECT id FROM app_user LIMIT 1")[0][0]
    # A staff rate for every day of the month: 0.8 GBP per USD. fx_rate is shared by every
    # workspace, so these rows are removed at the end or later tests read this rate.
    rate_ids: list[uuid.UUID] = []
    day = LAST_MONTH
    while day < THIS_MONTH:
        rate_ids.append(uuid.uuid4())
        owner_sql(
            "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
            "VALUES (%s, 'GBP', %s, 'official', 0.8, 'manual', %s)",
            rate_ids[-1],
            day,
            staff,
        )
        day += timedelta(days=1)
    try:
        await _staff_rate_then_blocked(client, tenant, headers, cid, owner_sql, azure)
    finally:
        for rid in rate_ids:
            owner_sql("DELETE FROM fx_rate WHERE id = %s", rid)


async def _staff_rate_then_blocked(
    client: AsyncClient, tenant: str, headers: dict[str, str], cid: str, owner_sql: Sql, azure
) -> None:
    gbp = [
        dict(r, BillingCurrency="GBP")
        for r in focus_rows(LAST_MONTH, LAST_MONTH + timedelta(days=4), sub=azure.subscription)
    ]
    azure.deliver(LAST_MONTH, LAST_MONTH + timedelta(days=4), rows=gbp)
    await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))
    native, usd = owner_sql(
        "SELECT sum(billed_cost), sum(billed_cost_usd) FROM cost_daily WHERE connection_id = %s",
        cid,
    )[0]
    assert float(usd) == pytest.approx(float(native) / 0.8, rel=1e-4)

    # Another currency with neither a staff rate nor Microsoft's USD amounts: blocked.
    bare = [
        {k: v for k, v in dict(r, BillingCurrency="KES").items() if not k.endswith("InUsd")}
        for r in focus_rows(THIS_MONTH, THROUGH, sub=azure.subscription)
    ]
    azure.deliver(THIS_MONTH, THROUGH, rows=bare)
    result = await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))
    assert result.rejected
    failed = owner_sql(
        "SELECT checks FROM ingest_batch WHERE connection_id = %s AND status = 'failed'", cid
    )[0][0]
    assert next(c for c in failed if c["id"] == "fx")["status"] == "error"


# --- export parsing and mapping (no database) ---------------------------------------------


def manifest(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "exportConfig": {"type": "FocusCost"},
        "runInfo": {"runId": "run-1", "startDate": "2026-10-01T00:00:00", "submittedTime": "x"},
        "blobs": [{"blobName": "f/e/20261001-20261031/run-1/part_0_0001.parquet"}],
        "dataRowCount": 3,
    }
    data.update(overrides)
    return json.dumps(data)


def test_manifests_are_checked() -> None:
    run = billing.parse_manifest("f/e/x/run-1/manifest.json", manifest(), "f/")
    assert run.billing_period == date(2026, 10, 1) and run.num_rows == 3
    for bad, match in (
        ({"exportConfig": {"type": "ActualCost"}}, "not FOCUS"),
        ({"runInfo": {"runId": "../x", "startDate": "2026-10-01"}}, "run ID"),
        ({"blobs": [{"blobName": "other/x.parquet"}]}, "outside"),
        ({"blobs": [{"blobName": "f/../../x.parquet"}]}, "outside"),
    ):
        with pytest.raises(billing.ManifestError, match=match):
            billing.parse_manifest("m", manifest(**bad), "f/")
    with pytest.raises(billing.ManifestError):
        billing.parse_manifest("m", "{nope", "f/")
    runs = [
        billing.ExportRun("a", date(2026, 10, 1), "a", "2026-10-02", (), None),
        billing.ExportRun("b", date(2026, 10, 1), "b", "2026-10-03", (), None),
        billing.ExportRun("c", date(2026, 9, 1), "c", "2026-09-30", (), None),
    ]
    assert [r.run_id for r in billing.latest_runs(runs)] == ["c", "b"]


def test_tags_and_subscriptions_are_read_in_every_form() -> None:
    assert billing.parse_tags('{"env": "prod"}') == [("env", "prod")]
    assert billing.parse_tags('"env": "dev","n": 1') == [("env", "dev"), ("n", "1")]
    assert billing.parse_tags("") is None and billing.parse_tags("not json") is None
    assert billing.subscription_of(f"/subscriptions/{SUBSCRIPTION.upper()}") == SUBSCRIPTION
    assert billing.subscription_of(None) is None


def test_mapping_keeps_native_amounts_and_maps_categories(tmp_path) -> None:
    rows = focus_rows(date(2026, 10, 1), date(2026, 10, 3))
    rows[0]["ChargeCategory"] = "Weird"
    rows[1]["BilledCost"] = -1.0
    from fake_azure import encode

    path = tmp_path / "part.csv.gz"
    path.write_bytes(encode(rows, "csv.gz"))
    run = billing.ExportRun("m", date(2026, 10, 1), "r", "s", ("part.csv",), len(rows))
    loaded = billing.aggregate(run, [("part.csv", path, "0" * 64, path.stat().st_size)])
    assert loaded.ok and loaded.currency == "EUR"
    ids = {c.id: c.status for c in loaded.checks}
    assert ids["charge_categories"] == "warning" and ids["negative_costs"] == "warning"
    assert {r.charge_category for r in loaded.rows} <= {"Usage", "Credit", "Adjustment"}
    assert sum(float(r.billed_cost) for r in loaded.rows) == pytest.approx(
        sum(r["BilledCost"] for r in rows)
    )
    assert any(r.resource_id == VM_WEB.lower() for r in loaded.resource_rows)
    assert loaded.subscriptions == {SUBSCRIPTION: "Production"}
    # Two currencies in one export: blocked.
    mixed = [*rows, dict(rows[0], BillingCurrency="USD")]
    path.write_bytes(encode(mixed, "parquet"))
    blocked = billing.aggregate(
        billing.ExportRun("m", date(2026, 10, 1), "r", "s", ("p.parquet",), None),
        [("p.parquet", path, "0" * 64, 1)],
    )
    assert not blocked.ok and blocked.rows == []
    # A required column missing: blocked by the schema check.
    path.write_bytes(
        encode([{k: v for k, v in r.items() if k != "BilledCost"} for r in rows], "parquet")
    )
    schema = billing.aggregate(
        billing.ExportRun("m", date(2026, 10, 1), "r", "s", ("p.parquet",), None),
        [("p.parquet", path, "0" * 64, 1)],
    )
    assert {c.id: c.status for c in schema.checks}["schema"] == "error"
    assert gzip  # imported for the CSV fixture's sake


# --- rules (no database) ------------------------------------------------------------------


def resource(kind: str, state: str | None, **config: Any) -> Any:
    from cloudoptics.analysis.rules import Resource

    now = datetime.now(UTC)
    return Resource(
        id=uuid.uuid4(), account_id=SUBSCRIPTION, region_id="westeurope", resource_type=kind,
        resource_id=f"/subscriptions/{SUBSCRIPTION}/x/{uuid.uuid4().hex[:6]}", name="r",
        state=state, config=config, tags={"env": "dev"}, state_since=now - timedelta(days=40),
        first_seen_at=now - timedelta(days=40), provider="azure",
    )  # fmt: skip


def test_rules_price_from_the_bill_or_list_prices() -> None:
    from cloudoptics.analysis.pricing import PriceBook
    from cloudoptics.analysis.rules import Context
    from cloudoptics.providers.azure import rules

    ctx = Context(now=datetime.now(UTC), prices=PriceBook({}))
    disk = resource("azure_disk", "Unattached", sizeGiB=100, sku="Premium_LRS")
    billed = resource(
        "azure_disk", "Unattached", sizeGiB=100, sku="Premium_LRS", monthlyCostUsd=9.5
    )
    attached = resource("azure_disk", "Attached", sizeGiB=100)
    found = list(rules.unattached_disks(ctx, [disk, billed, attached]))
    assert [c.cost_now for c in found] == [pytest.approx(15.0), 9.5]
    assert found[0].basis == "list_price" and found[0].confidence == "low"
    assert found[1].basis == "effective_rate" and found[1].confidence == "high"
    assert found[1].fix["rollback"] and "az disk delete" in found[1].fix["steps"][1]

    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    live_disk = resource("azure_disk", "Attached")
    snaps = [
        resource("azure_snapshot", "Succeeded", sizeGiB=10, timeCreated=old),
        resource("azure_snapshot", "Succeeded", sizeGiB=10, timeCreated=old,
                 sourceResourceId=live_disk.resource_id),
        resource("azure_snapshot", "Succeeded", sizeGiB=10,
                 timeCreated=datetime.now(UTC).isoformat()),
    ]  # fmt: skip
    assert len(list(rules.old_snapshots(ctx, [live_disk, *snaps]))) == 1

    ip = list(rules.idle_public_ips(ctx, [resource("azure_public_ip", "unassociated"),
                                          resource("azure_public_ip", "associated")]))  # fmt: skip
    assert len(ip) == 1 and ip[0].reversibility == "irreversible"
    stopped = list(rules.stopped_not_deallocated(
        ctx, [resource("azure_vm", "stopped", monthlyCostUsd=50.0),
              resource("azure_vm", "deallocated", monthlyCostUsd=50.0),
              resource("azure_vm", "stopped")]))  # fmt: skip
    assert len(stopped) == 1 and stopped[0].savings == 50.0

    idle_vm = resource("azure_vm", "running", monthlyCostUsd=80.0, vmSize="Standard_D2s_v5")
    busy_vm = resource("azure_vm", "running", monthlyCostUsd=80.0, vmSize="Standard_D2s_v5")
    day = datetime.now(UTC).date()
    ctx.usage = {
        idle_vm.id: {"cpu_max": {day - timedelta(days=i): [2.0] * 24 for i in range(15)}},
        busy_vm.id: {"cpu_max": {day - timedelta(days=i): [70.0] * 24 for i in range(15)}},
    }
    advice = [{"savingsMonthly": 70.0, "targetSku": "Standard_D2s_v5"}]
    advised = resource(
        "azure_vm", "running", monthlyCostUsd=140.0, vmSize="Standard_D4s_v5", advisor=advice
    )
    found = {c.resource.id: c for c in rules.underused_vms(ctx, [idle_vm, busy_vm, advised])}
    assert set(found) == {idle_vm.id, advised.id}
    assert found[idle_vm.id].type == "stop_idle_instance" and found[idle_vm.id].savings == 80.0
    assert found[advised.id].savings == pytest.approx(70.0)
    assert "Standard_D2s_v5" in found[advised.id].fix["steps"][0]


def test_inventory_records_and_scope() -> None:
    fake = FakeAzure(certificate=new_certificate()[1], subscription=SUBSCRIPTION)
    fake.add_resources()
    costs = {VM_WEB.lower(): 12.5}
    records = [inventory.to_record(r, costs) for r in fake.resources]
    web = next(r for r in records if r and r["name"] == "vm-web")
    assert web["state"] == "running" and web["config"]["monthlyCostUsd"] == 12.5
    assert web["rid"] == VM_WEB.lower() and web["account"] == SUBSCRIPTION
    assert inventory.to_record({"type": "microsoft.web/sites"}, {}) is None
    sub_scope = azconn.AzureSettings(
        uuid.uuid4(), uuid.uuid4(), "healthy", DIRECTORY, "billing_account", "123",
        (SUBSCRIPTION, "aaaaaaaa-0000-0000-0000-000000000000"), STORAGE, CONTAINER, "", None,
        None, True, "123",
    )  # fmt: skip
    # A billing account scans only the listed subscriptions its bill names.
    assert inventory.scan_subscriptions(sub_scope, [SUBSCRIPTION]) == [SUBSCRIPTION]
    hours = inventory.hourly_values(
        [{"timeStamp": "2026-10-01T05:00:00Z", "maximum": 7.0}], "maximum"
    )
    assert hours[date(2026, 10, 1)][5] == 7.0


async def test_jobs_dispatch_to_the_azure_plug_in(client: AsyncClient, login, azure) -> None:
    from cloudoptics.ingestion import jobs as ingestion_jobs
    from cloudoptics.inventory import jobs as inventory_jobs

    tenant, headers = await workspace(client, login, "Azure Jobs Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid = setup["connection"]["id"]
    # Not validated yet: every Azure job skips it.
    assert await ingestion_jobs.ingest(tenant, cid) == "skipped"
    assert await inventory_jobs.scan(tenant, cid) == "skipped"
    assert await ingestion_jobs.snapshot(tenant, cid) == "skipped"
    # The generic "Check again" validates through the plug-in too.
    ready(azure, setup["ownershipCode"], months=())
    r = await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"


async def test_snapshot_gives_provisional_month_to_date_costs(
    client: AsyncClient, login, owner_sql: Sql, azure
) -> None:
    from cloudoptics.providers.azure import reconcile

    tenant, headers = await workspace(client, login, "Azure Snapshot Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid = setup["connection"]["id"]
    ready(azure, setup["ownershipCode"], months=())
    await validate(client, tenant, headers, cid)
    azure.totals[f"{TODAY:%Y-%m}"] = {azure.subscription: (100.0, 108.0)}
    assert await reconcile.refresh_snapshot(uuid.UUID(tenant), uuid.UUID(cid)) == "succeeded"
    row = owner_sql(
        "SELECT provider, billing_currency, billed_cost, billed_cost_usd FROM cost_snapshot_ce "
        "WHERE connection_id = %s",
        cid,
    )[0]
    assert row[0] == "azure" and row[1] == "EUR"
    assert float(row[2]) == 100.0 and float(row[3]) == 108.0


async def test_consent_page_reads_nothing(client: AsyncClient) -> None:
    ok = await client.get("/v1/azure/consent?admin_consent=True&tenant=x&state=y")
    assert ok.status_code == 200 and "Cloudoptics is approved" in ok.text
    assert ok.headers["cache-control"] == "no-store"
    refused = await client.get("/v1/azure/consent?error=access_denied")
    assert "not given" in refused.text


# --- documented behaviour (ADR-020, Sources) ----------------------------------------------


async def test_throttling_is_retried_and_next_links_are_followed(monkeypatch) -> None:
    """429 waits as Retry-After / the Cost Management QPU header says; nextLink is followed
    as given (it carries its own api-version)."""
    from cloudoptics.providers.azure import client

    key, cert = new_certificate()
    fake = FakeAzure(certificate=cert)
    seen: list[str] = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return fake.handler(request)
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if "page2" not in str(request.url):
            link = "https://management.azure.com/x?api-version=2026-10-01&$skiptoken=page2"
            return httpx.Response(200, json={"value": [{"n": 1}], "nextLink": link})
        return httpx.Response(200, json={"value": [{"n": 2}]})

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(client.asyncio, "sleep", sleep)
    client.set_transport(httpx.MockTransport(handler), CertificateAssertion(key, cert))
    try:
        from cloudoptics.platform.settings import get_settings

        monkeypatch.setattr(get_settings(), "azure_client_id", CLIENT_ID)
        api = client.AzureApi(DIRECTORY)
        items = await api.arm_list("/x", api_version="2026-10-01")
    finally:
        client.set_transport(None)
    assert items == [{"n": 1}, {"n": 2}] and waits == [7.0]
    assert seen[-1].count("api-version") == 1
    response = httpx.Response(429, headers={client.QPU_RETRY_AFTER: "3"})
    assert client.retry_after(response, 0) == 3.0
    assert client.retry_after(httpx.Response(429), 1) == 4.0


def test_rows_without_a_subscription_and_focus_1_0_dates(tmp_path) -> None:
    """Microsoft leaves SubAccountId empty for unused reservations, savings plans and rounding
    (they count against the connection); FOCUS 1.0 dates have no seconds (1.0r2 adds them)."""
    from fake_azure import encode

    rows = focus_rows(date(2026, 10, 1), date(2026, 10, 2))
    rows[0]["SubAccountId"] = ""
    for r in rows:
        r["ChargePeriodStart"] = r["ChargePeriodStart"].replace(":00Z", "Z")
    path = tmp_path / "part.csv"
    path.write_bytes(encode(rows, "csv"))
    run = billing.ExportRun("m", date(2026, 10, 1), "r", "s", ("part.csv",), len(rows))
    loaded = billing.aggregate(run, [("part.csv", path, "0" * 64, 1)], "the-connection")
    assert loaded.ok, loaded.checks
    assert {r.account_id for r in loaded.rows} == {"the-connection", SUBSCRIPTION}
    assert {r.usage_date for r in loaded.rows} >= {date(2026, 10, 1), date(2026, 10, 2)}
    assert "the-connection" not in loaded.subscriptions


async def test_reconciliation_waits_out_cost_management_throttling(
    client: AsyncClient, login, owner_sql: Sql, azure, monkeypatch
) -> None:
    from cloudoptics.providers.azure import client as azure_client
    from cloudoptics.providers.azure import reconcile

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(azure_client.asyncio, "sleep", no_wait)
    tenant, headers = await workspace(client, login, "Azure Throttle Ltd")
    setup = await connect(client, tenant, headers, azure.subscription)
    cid = setup["connection"]["id"]
    ready(azure, setup["ownershipCode"], months=(LAST_MONTH,))
    await validate(client, tenant, headers, cid)
    azure.throttle_queries = 2
    status = await reconcile.reconcile_period(uuid.UUID(tenant), uuid.UUID(cid), LAST_MONTH)
    assert status == "ok" and azure.throttle_queries == 0


async def test_disconnect_drops_the_code_and_connecting_again_issues_a_new_one(
    client: AsyncClient, login, owner_sql: Sql, azure
) -> None:
    tenant, headers = await workspace(client, login, "Azure Again Ltd")
    first = await connect(client, tenant, headers, azure.subscription)
    cid = first["connection"]["id"]
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    assert r.status_code == 204
    assert owner_sql("SELECT external_id_enc FROM cloud_connection WHERE id = %s", cid) == [(None,)]
    gone = await client.get(
        f"/v1/tenants/{tenant}/providers/azure/connections/{cid}", headers=headers
    )
    assert gone.status_code == 409
    again = await connect(client, tenant, headers, azure.subscription)
    assert again["connection"]["id"] == cid and again["connection"]["status"] == "pending"
    assert again["ownershipCode"] != first["ownershipCode"]
