import secrets
from urllib.parse import parse_qs, urlparse

from fake_aws import FakeClient, client_error, healthy_clients
from httpx import AsyncClient


async def workspace(client: AsyncClient, login) -> tuple[str, dict[str, str]]:
    headers = await login()
    r = await client.post("/v1/tenants", json={"name": "W", "countryCode": "KE"}, headers=headers)
    return r.json()["id"], headers


async def test_setup_offers_the_template_download_until_templates_are_hosted(
    client: AsyncClient, login
) -> None:
    tenant, headers = await workspace(client, login)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": "101010101010"}, headers=headers
    )
    body = r.json()
    assert body["quickCreateUrl"] is None
    assert body["templateDownloadUrl"].endswith("/infra/cloudformation/billing-role.yaml")
    assert body["resourceTemplateDownloadUrl"].endswith("/infra/cloudformation/resource-role.yaml")


async def test_create_connection_returns_a_quick_create_link(
    client: AsyncClient, login, quick_create
) -> None:
    tenant, headers = await workspace(client, login)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections",
        json={"awsAccountId": "111122223333", "accountType": "management"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["connection"]["status"] == "pending"
    assert body["connection"]["roleArn"] == (
        "arn:aws:iam::111122223333:role/CloudopticsBillingReadOnly"
    )
    assert len(body["externalId"]) >= 32
    url = urlparse(body["quickCreateUrl"])
    assert url.hostname == "us-east-1.console.aws.amazon.com"
    params = parse_qs(url.fragment.split("?", 1)[1])
    assert params["param_ExternalId"] == [body["externalId"]]
    assert params["param_CreateCurExport"] == ["true"]
    assert params["templateURL"][0].endswith("billing-role.yaml")

    again = await client.get(
        f"/v1/tenants/{tenant}/connections/{body['connection']['id']}/setup", headers=headers
    )
    assert again.json()["externalId"] == body["externalId"]


async def test_external_id_is_encrypted_at_rest(client: AsyncClient, login, owner_sql) -> None:
    tenant, headers = await workspace(client, login)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": "222233334444"}, headers=headers
    )
    external_id = r.json()["externalId"]
    [(stored,)] = owner_sql(
        "SELECT external_id_enc FROM cloud_connection WHERE id = %s", r.json()["connection"]["id"]
    )
    assert external_id.encode() not in bytes(stored)


async def test_an_aws_account_can_join_only_one_workspace(
    client: AsyncClient, login, fake_aws, owner_sql
) -> None:
    a, ha = await workspace(client, login)
    b, hb = await workspace(client, login)
    body = {"awsAccountId": "333344445555"}
    # Claiming an account proves nothing, so it blocks nobody and reveals nothing.
    ca = (await client.post(f"/v1/tenants/{a}/connections", json=body, headers=ha)).json()
    cb = (await client.post(f"/v1/tenants/{b}/connections", json=body, headers=hb)).json()
    # A second claim inside the same workspace is still refused.
    r = await client.post(f"/v1/tenants/{a}/connections", json=body, headers=ha)
    assert r.status_code == 409 and "this workspace" in r.json()["title"]

    # The first workspace to assume the role in the account (it has the stack) keeps it.
    va = f"/v1/tenants/{a}/connections/{ca['connection']['id']}/validate"
    assert (await client.post(va, headers=ha)).json()["status"] == "healthy"
    vb = f"/v1/tenants/{b}/connections/{cb['connection']['id']}/validate"
    conn = (await client.post(vb, headers=hb)).json()
    assert conn["status"] == "failed"
    [check] = conn["permissionChecks"]
    assert "another Cloudoptics workspace" in check["message"]
    verified = owner_sql(
        "SELECT tenant_id::text FROM cloud_connection WHERE aws_account_id = '333344445555' "
        "AND account_verified_at IS NOT NULL"
    )
    assert verified == [(a,)]
    # Once the first disconnects, the account is free for the other.
    await client.delete(f"/v1/tenants/{a}/connections/{ca['connection']['id']}", headers=ha)
    assert (await client.post(vb, headers=hb)).json()["status"] == "healthy"


async def test_validate_marks_the_connection_healthy(client: AsyncClient, login, fake_aws) -> None:
    tenant, headers = await workspace(client, login)
    created = (
        await client.post(
            f"/v1/tenants/{tenant}/connections",
            json={"awsAccountId": "123456789012", "accountType": "management"},
            headers=headers,
        )
    ).json()
    cid = created["connection"]["id"]
    r = await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
    assert r.status_code == 200, r.text
    conn = r.json()
    assert conn["status"] == "healthy"
    assert conn["curBucket"] == "cloudoptics-cur-123456789012"
    assert [c["id"] for c in conn["permissionChecks"]][:2] == ["assume_role", "cost_explorer"]

    role_arn, external_id, name = fake_aws.calls[-1]
    assert role_arn.endswith(":role/CloudopticsBillingReadOnly")
    assert external_id == created["externalId"]
    assert name.startswith("cloudoptics-") and name.endswith("-validate")

    log = (await client.get(f"/v1/tenants/{tenant}/audit-log", headers=headers)).json()["entries"]
    assert log[0]["action"] == "connection.status_changed"
    assert log[0]["after"] == {"status": "healthy"}


async def test_validate_reports_a_failed_assume(client: AsyncClient, login, fake_aws) -> None:
    fake_aws.assume_error = client_error("AccessDenied", "AssumeRole")
    tenant, headers = await workspace(client, login)
    cid = (
        await client.post(
            f"/v1/tenants/{tenant}/connections",
            json={"awsAccountId": "444455556666"},
            headers=headers,
        )
    ).json()["connection"]["id"]
    conn = (
        await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
    ).json()
    assert conn["status"] == "failed"
    assert conn["lastError"] == {"code": "AccessDenied", "check": "assume_role"}


async def test_degraded_when_a_permission_is_missing(client: AsyncClient, login, fake_aws) -> None:
    clients = healthy_clients("555566667777")
    clients["savingsplans"] = FakeClient(
        describe_savings_plans=client_error("AccessDeniedException")
    )
    fake_aws.clients = clients
    tenant, headers = await workspace(client, login)
    cid = (
        await client.post(
            f"/v1/tenants/{tenant}/connections",
            json={"awsAccountId": "555566667777"},
            headers=headers,
        )
    ).json()["connection"]["id"]
    conn = (
        await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
    ).json()
    assert conn["status"] == "degraded"
    assert conn["lastError"]["check"] == "savings_plans"


async def test_viewers_cannot_manage_connections(client: AsyncClient, login, owner_sql) -> None:
    tenant, _admin = await workspace(client, login)
    viewer = await login("conn-viewer@example.com")
    await client.get("/v1/me", headers=viewer)
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) "
        "SELECT %s, id, 'viewer' FROM app_user WHERE email = %s",
        tenant,
        "conn-viewer@example.com",
    )
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": "666677778888"}, headers=viewer
    )
    assert r.status_code == 403
    assert (
        await client.get(f"/v1/tenants/{tenant}/connections", headers=viewer)
    ).status_code == 200


async def test_disconnect(client: AsyncClient, login) -> None:
    tenant, headers = await workspace(client, login)
    cid = (
        await client.post(
            f"/v1/tenants/{tenant}/connections",
            json={"awsAccountId": "777788889999"},
            headers=headers,
        )
    ).json()["connection"]["id"]
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    assert r.status_code == 204
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "disconnected"


async def test_reconnect_reuses_the_connection_with_a_new_external_id(
    client: AsyncClient, login, fake_aws, owner_sql
) -> None:
    tenant, headers = await workspace(client, login)
    account = "".join(secrets.choice("0123456789") for _ in range(12))
    base = f"/v1/tenants/{tenant}/connections"
    first = (await client.post(base, json={"awsAccountId": account}, headers=headers)).json()
    cid = first["connection"]["id"]
    fake_aws.clients = healthy_clients(account)
    await client.post(f"{base}/{cid}/validate", headers=headers)

    # Still connected: connecting it again is refused with a pointer to the fix.
    again = await client.post(base, json={"awsAccountId": account}, headers=headers)
    assert again.status_code == 409 and "Set up again" in again.json()["title"]

    assert (await client.delete(f"{base}/{cid}", headers=headers)).status_code == 204
    r = await client.post(
        base, json={"awsAccountId": account, "accountType": "standalone"}, headers=headers
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["connection"]["id"] == cid
    assert body["connection"]["status"] == "pending"
    assert body["connection"]["accountType"] == "standalone"
    assert body["connection"]["permissionChecks"] == []
    assert body["externalId"] != first["externalId"]
    assert owner_sql("SELECT health FROM cloud_connection WHERE id = %s", cid) == [(None,)]
    setup = (await client.get(f"{base}/{cid}/setup", headers=headers)).json()
    assert setup["externalId"] == body["externalId"]
    actions = [
        e["action"]
        for e in (await client.get(f"/v1/tenants/{tenant}/audit-log", headers=headers)).json()[
            "entries"
        ]
    ]
    assert actions[:2] == ["connection.reconnected", "connection.disconnected"]


async def test_rejects_bad_account_ids(client: AsyncClient, login) -> None:
    tenant, headers = await workspace(client, login)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": "12345"}, headers=headers
    )
    assert r.status_code == 422


async def test_queue_mode_defers_a_job_that_the_worker_completes(
    client: AsyncClient, login, fake_aws, owner_sql
) -> None:
    from cloudoptics.connectors.jobs import validate_connection
    from cloudoptics.platform.jobs import get_job_app
    from cloudoptics.platform.settings import get_settings

    settings = get_settings()
    get_job_app.cache_clear()
    tenant, headers = await workspace(client, login)
    cid = (
        await client.post(
            f"/v1/tenants/{tenant}/connections",
            json={"awsAccountId": "888899990000"},
            headers=headers,
        )
    ).json()["connection"]["id"]
    settings.jobs_mode = "queue"
    try:
        r = await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
        assert r.status_code == 202
        assert r.json() == {"connectionId": cid, "status": "validating"}
        # A second request while one is queued is accepted without a duplicate job.
        again = await client.post(
            f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers
        )
        assert again.status_code == 202
    finally:
        settings.jobs_mode = "inline"
    jobs = owner_sql(
        "SELECT task_name, args->>'connection_id' FROM procrastinate_jobs WHERE status = 'todo'"
        " AND args->>'connection_id' = %s",
        cid,
    )
    assert jobs == [("connectors:connection.validate", cid)]

    status = await validate_connection(tenant_id=tenant, connection_id=cid)
    assert status == "healthy"
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "healthy"
    # A healthy connection queues provisional Cost Explorer costs, its first billing ingest
    # (it has an export) and account discovery (which skips non-management accounts).
    queued = owner_sql(
        "SELECT task_name FROM procrastinate_jobs WHERE status = 'todo'"
        " AND args->>'connection_id' = %s ORDER BY id",
        cid,
    )
    assert queued[-3:] == [
        ("ingestion:ce.snapshot",),
        ("ingestion:cur.ingest",),
        ("connectors:accounts.discover",),
    ]
