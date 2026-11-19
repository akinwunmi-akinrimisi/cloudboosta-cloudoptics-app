"""The stack callback cannot act on its word alone (security audit run 1, leads 1-4 and 10).

The External ID, the callback's only proof, is readable in the customer's account and stays
known to admins who leave, so: links expire and can be withdrawn, a link stops working when
its maker is no longer an admin, a reported delete disconnects only once the role is gone,
new values replace known-good ones only once their role works, and disconnect is final for
work that was already queued.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from fake_aws import FakeSession, FakeSessionFactory, client_error, healthy_clients
from httpx import AsyncClient
from test_access_connect import account, callback_body, link_params, new_link
from test_connections import workspace

from cloudoptics.connectors import aws

DENIED = client_error("AccessDenied", "AssumeRole")


class PickyFactory(FakeSessionFactory):
    """Refuses some roles or External IDs, the way STS does for a role that is gone."""

    def __init__(self, clients: dict[str, Any]) -> None:
        super().__init__(clients)
        self.denied: set[str] = set()

    def assume(self, role_arn: str, external_id: str, name: str) -> FakeSession:
        self.calls.append((role_arn, external_id, name))
        if self.assume_error or role_arn in self.denied or external_id in self.denied:
            raise self.assume_error or DENIED
        return FakeSession(self.clients)


@pytest.fixture
def picky():  # type: ignore[no-untyped-def]
    from cloudoptics.connectors import service

    def make(acct: str) -> PickyFactory:
        factory = PickyFactory(healthy_clients(acct))
        service.set_session_factory(factory)
        return factory

    yield make
    service.set_session_factory(None)


async def connected(
    client: AsyncClient, login, acct: str
) -> tuple[str, dict[str, str], dict[str, str], str]:
    """A workspace whose account connected through a one-click link (validated inline)."""
    tenant, headers = await workspace(client, login)
    params = link_params((await new_link(client, tenant, headers))["quickCreateUrl"])
    r = await client.post("/v1/cfn/callback", json=callback_body(params, acct))
    assert r.status_code == 200, r.text
    return tenant, headers, params, r.json()["connectionId"]


async def actions(client: AsyncClient, tenant: str, headers: dict[str, str]) -> list[str]:
    log = await client.get(f"/v1/tenants/{tenant}/audit-log", headers=headers)
    return [e["action"] for e in log.json()["entries"]]


class QueueMode:
    """Jobs go to the queue instead of running inline, as on staging and production."""

    def __enter__(self) -> None:
        from cloudoptics.platform.jobs import get_job_app
        from cloudoptics.platform.settings import get_settings

        get_job_app.cache_clear()
        get_settings().jobs_mode = "queue"

    def __exit__(self, *_: object) -> None:
        from cloudoptics.platform.settings import get_settings

        get_settings().jobs_mode = "inline"


def jobs_for(owner_sql, connection_id: str) -> list[tuple[Any, ...]]:
    return owner_sql(
        "SELECT task_name, args, scheduled_at > now() + interval '4 minutes' "
        "FROM procrastinate_jobs WHERE status = 'todo' AND args->>'connection_id' = %s "
        "ORDER BY id",
        connection_id,
    )


# Lead 3: links expire, can be withdrawn and die with their maker's admin role -------------


async def test_an_expired_link_connects_nothing(
    client: AsyncClient, login, quick_create, fake_aws, owner_sql
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    setup = await new_link(client, tenant, headers)
    intent_id = setup["intent"]["id"]
    [(days,)] = owner_sql(
        "SELECT extract(day FROM expires_at - created_at)::int FROM connection_intent "
        "WHERE id = %s",
        intent_id,
    )
    assert days == 7
    owner_sql(
        "UPDATE connection_intent SET expires_at = now() - interval '1 second' WHERE id = %s",
        intent_id,
    )
    params = link_params(setup["quickCreateUrl"])
    r = await client.post("/v1/cfn/callback", json=callback_body(params, acct))
    assert r.status_code == 410
    assert "expired" in r.json()["title"]
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", tenant) == [(0,)]
    intent = await client.get(
        f"/v1/tenants/{tenant}/connections/intents/{intent_id}", headers=headers
    )
    assert intent.json()["status"] == "waiting" and "expired" in intent.json()["error"]


async def test_an_admin_can_list_and_withdraw_unused_links(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    unused = await new_link(client, tenant, headers)
    used = await new_link(client, tenant, headers)
    fake_aws.clients = healthy_clients(acct)
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(link_params(used["quickCreateUrl"]), acct)
    )
    assert r.status_code == 200

    base = f"/v1/tenants/{tenant}/connections/intents"
    listed = {i["id"]: i for i in (await client.get(base, headers=headers)).json()}
    assert listed[unused["intent"]["id"]]["status"] == "waiting"
    assert listed[used["intent"]["id"]]["status"] == "connected"

    # A link that already connected an account is disconnected, not withdrawn.
    assert (await client.delete(f"{base}/{used['intent']['id']}", headers=headers)).status_code == (
        409
    )
    assert (
        await client.delete(f"{base}/{unused['intent']['id']}", headers=headers)
    ).status_code == 204
    listed = {i["id"]: i for i in (await client.get(base, headers=headers)).json()}
    assert listed[unused["intent"]["id"]]["status"] == "revoked"
    assert listed[unused["intent"]["id"]]["revokedAt"] is not None
    assert (await client.delete(f"{base}/{uuid.uuid4()}", headers=headers)).status_code == 404

    r = await client.post(
        "/v1/cfn/callback",
        json=callback_body(link_params(unused["quickCreateUrl"]), account()),
    )
    assert r.status_code == 410 and "withdrawn" in r.json()["title"]
    assert "connection.intent_revoked" in await actions(client, tenant, headers)


async def test_a_link_stops_working_when_its_maker_is_no_longer_an_admin(
    client: AsyncClient, login, quick_create, fake_aws, owner_sql
) -> None:
    tenant, headers = await workspace(client, login)
    demoted = await new_link(client, tenant, headers)
    removed = await new_link(client, tenant, headers)
    [(maker,)] = owner_sql(
        "SELECT created_by::text FROM connection_intent WHERE id = %s", demoted["intent"]["id"]
    )

    owner_sql(
        "UPDATE membership SET role = 'viewer' WHERE tenant_id = %s AND user_id = %s",
        tenant,
        maker,
    )
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(link_params(demoted["quickCreateUrl"]), account())
    )
    assert r.status_code == 403 and "no longer an admin" in r.json()["title"]

    owner_sql("DELETE FROM membership WHERE tenant_id = %s AND user_id = %s", tenant, maker)
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(link_params(removed["quickCreateUrl"]), account())
    )
    assert r.status_code == 403
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", tenant) == [(0,)]


async def test_a_used_link_keeps_working_for_its_own_stack_after_it_expires(
    client: AsyncClient, login, quick_create, fake_aws, owner_sql
) -> None:
    """The deployed stack sends the same link on every update: expiry is for unused links."""
    acct = account()
    fake_aws.clients = healthy_clients(acct)
    tenant, _headers, params, cid = await connected(client, login, acct)
    owner_sql(
        "UPDATE connection_intent SET expires_at = now() - interval '1 day' WHERE tenant_id = %s",
        tenant,
    )
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(params, acct, requestType="Update")
    )
    assert r.status_code == 200 and r.json() == {"connectionId": cid, "status": "accepted"}


# Leads 1 and 2: a reported delete disconnects only once the role is gone ------------------


async def test_a_delete_report_is_ignored_while_the_role_still_works(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    fake_aws.clients = healthy_clients(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(params, acct, requestType="Delete")
    )
    assert r.json() == {"connectionId": cid, "status": "kept"}
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "healthy"
    assert fake_aws.calls[-1][2].endswith("-verify")
    assert "connection.delete_ignored" in await actions(client, tenant, headers)
    assert "connection.disconnected" not in await actions(client, tenant, headers)


async def test_a_queued_delete_check_disconnects_only_when_the_role_is_gone(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    from cloudoptics.connectors.jobs import REMOVAL_TASK, verify_removed

    acct = account()
    factory = picky(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    with QueueMode():
        r = await client.post(
            "/v1/cfn/callback", json=callback_body(params, acct, requestType="Delete")
        )
    assert r.json() == {"connectionId": cid, "status": "verifying"}
    [(task, args, later)] = jobs_for(owner_sql, cid)
    assert task == REMOVAL_TASK and later is True  # the roles outlive the setup step
    assert args["attempt"] == 1
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "healthy"

    # First check: the role still works, so it is checked once more later.
    owner_sql("DELETE FROM procrastinate_jobs WHERE args->>'connection_id' = %s", cid)
    with QueueMode():
        assert await verify_removed(**args) == "kept"
    [(_, again, later)] = jobs_for(owner_sql, cid)
    assert again["attempt"] == 2 and later is True
    assert "connection.delete_ignored" not in await actions(client, tenant, headers)

    # Second check: still works, so the report is ignored and the audit log says so.
    with QueueMode():
        assert await verify_removed(**again) == "kept"
    assert "connection.delete_ignored" in await actions(client, tenant, headers)

    # The role is really gone: now it disconnects.
    factory.assume_error = DENIED
    with QueueMode():
        assert await verify_removed(**args) == "disconnected"
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "disconnected"


async def test_a_delete_check_queued_before_a_reconnect_leaves_the_new_connection_alone(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    from cloudoptics.connectors.jobs import verify_removed

    acct = account()
    factory = picky(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    with QueueMode():
        await client.post(
            "/v1/cfn/callback", json=callback_body(params, acct, requestType="Delete")
        )
    [(_, args, _)] = jobs_for(owner_sql, cid)
    base = f"/v1/tenants/{tenant}/connections"
    await client.delete(f"{base}/{cid}", headers=headers)
    r = await client.post(base, json={"awsAccountId": acct}, headers=headers)
    assert r.json()["connection"]["id"] == cid  # reconnected, with a new External ID
    factory.assume_error = DENIED
    assert await verify_removed(**args) == "skipped"
    conn = (await client.get(f"{base}/{cid}", headers=headers)).json()
    assert conn["status"] == "pending"


async def test_an_unknown_aws_error_leaves_the_connection_and_is_retried(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    from cloudoptics.connectors.jobs import RemovalCheckError, verify_removed

    acct = account()
    factory = picky(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    with QueueMode():
        await client.post(
            "/v1/cfn/callback", json=callback_body(params, acct, requestType="Delete")
        )
    [(_, args, _)] = jobs_for(owner_sql, cid)
    factory.assume_error = client_error("Throttling", "AssumeRole")
    with pytest.raises(RemovalCheckError):
        await verify_removed(**args)
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "healthy"


# Lead 2: new values replace known-good ones only once their role works --------------------


async def test_a_reported_stack_change_is_kept_back_when_its_role_does_not_work(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    acct = account()
    factory = picky(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    other_role = aws.billing_role_arn(acct, "x-other")
    assert other_role.endswith("/CloudopticsBillingReadOnly")
    factory.denied.add(other_role)
    forged = callback_body(
        params, acct, requestType="Update", stackName="x-other", management=False,
        curBucket="someone-elses-bucket",
    )  # fmt: skip
    r = await client.post("/v1/cfn/callback", json=forged)
    assert r.json()["status"] == "accepted"

    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["stackName"] == "cloudoptics-access"
    assert conn["roleArn"] == f"arn:aws:iam::{acct}:role/CloudopticsBillingReader"
    assert conn["accountType"] == "management"
    assert conn["curBucket"] == f"cloudoptics-cur-{acct}"
    assert conn["status"] == "healthy"  # the known-good values were checked again
    assert owner_sql("SELECT pending_stack FROM cloud_connection WHERE id = %s", cid) == [(None,)]
    log = await actions(client, tenant, headers)
    assert "connection.stack_change_rejected" in log
    assert log.count("connection.stack_changed") == 0


async def test_a_queued_stack_change_waits_for_its_check(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    from cloudoptics.connectors.jobs import validate_connection

    acct = account()
    picky(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    newer = callback_body(params, acct, requestType="Update", templateVersion="9.9.9")
    with QueueMode():
        r = await client.post("/v1/cfn/callback", json=newer)
    assert r.json()["status"] == "accepted"
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["stackVersion"] == aws.ACCESS_TEMPLATE_VERSION  # not yet
    [(task, _, _)] = jobs_for(owner_sql, cid)
    assert task == "connectors:connection.validate"

    assert await validate_connection(tenant, cid) == "healthy"
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["stackVersion"] == "9.9.9"  # the role worked, so the report was applied


async def test_a_callback_does_not_bring_a_disconnected_connection_back(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    fake_aws.clients = healthy_clients(acct)
    tenant, headers, params, cid = await connected(client, login, acct)
    await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    calls = len(fake_aws.calls)
    for kind in ("Create", "Update"):
        r = await client.post(
            "/v1/cfn/callback", json=callback_body(params, acct, requestType=kind)
        )
        assert r.status_code == 409 and "disconnected" in r.json()["title"]
    conn = (await client.get(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)).json()
    assert conn["status"] == "disconnected"
    assert len(fake_aws.calls) == calls
    assert "connection.callback_refused" in await actions(client, tenant, headers)


# Lead 4: disconnect is final for work already queued ---------------------------------------


async def test_a_queued_validation_does_not_undo_a_disconnect(
    client: AsyncClient, login, quick_create, fake_aws, owner_sql
) -> None:
    from cloudoptics.connectors.jobs import check_connection, validate_connection

    acct = account()
    fake_aws.clients = healthy_clients(acct)
    tenant, headers, _, cid = await connected(client, login, acct)
    base = f"/v1/tenants/{tenant}/connections/{cid}"
    with QueueMode():
        assert (await client.post(f"{base}/validate", headers=headers)).status_code == 202
    assert (await client.delete(base, headers=headers)).status_code == 204
    owner_sql("DELETE FROM procrastinate_jobs WHERE args->>'connection_id' = %s", cid)
    calls = len(fake_aws.calls)

    assert await validate_connection(tenant, cid) == "disconnected"
    assert await check_connection(tenant, cid) == "disconnected"
    assert len(fake_aws.calls) == calls  # the customer's role was not assumed
    assert owner_sql("SELECT status, health FROM cloud_connection WHERE id = %s", cid) == [
        ("disconnected", None)
    ]
    assert jobs_for(owner_sql, cid) == []  # no loads or discovery queued
    # And an admin cannot "check" it back to life: Reconnect is the way.
    r = await client.post(f"{base}/validate", headers=headers)
    assert r.status_code == 409 and "Reconnect" in r.json()["title"]


async def test_a_reconciliation_queued_before_a_disconnect_does_not_call_aws(
    client: AsyncClient, login, quick_create, fake_aws, owner_sql
) -> None:
    from cloudoptics.ingestion import reconcile

    acct = account()
    fake_aws.clients = healthy_clients(acct)
    tenant, headers, _, cid = await connected(client, login, acct)
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "provider_account_id, service_code, service_name, service_category, region_id, usage_type, "
        "charge_category, billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) "
        "VALUES (%s, %s, '2026-09-03', '2026-09-01', %s, 'AmazonEC2', 'Amazon EC2', 'Compute', "
        "'us-east-1', 'BoxUsage', 'Usage', 10, 10, 10, 'v1')",
        tenant,
        cid,
        acct,
    )
    await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    calls = len(fake_aws.calls)
    status = await reconcile.reconcile_period(uuid.UUID(tenant), uuid.UUID(cid), date(2026, 9, 1))
    assert status == "skipped"
    assert len(fake_aws.calls) == calls


# Lead 10: a reservation exists only with fresh proof ----------------------------------------


async def test_disconnect_and_reconnect_release_the_account_until_proven_again(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    acct = account()
    factory = picky(acct)
    a, ha, _, ca = await connected(client, login, acct)

    def verified(cid: str) -> bool:
        [(at,)] = owner_sql("SELECT account_verified_at FROM cloud_connection WHERE id = %s", cid)
        return at is not None

    assert verified(ca)
    await client.delete(f"/v1/tenants/{a}/connections/{ca}", headers=ha)
    assert not verified(ca)

    # A reconnects without a working stack (its new External ID is refused) ...
    r = await client.post(f"/v1/tenants/{a}/connections", json={"awsAccountId": acct}, headers=ha)
    factory.denied.add(r.json()["externalId"])
    assert not verified(ca)
    v = await client.post(f"/v1/tenants/{a}/connections/{ca}/validate", headers=ha)
    assert v.json()["status"] == "failed"
    assert not verified(ca)

    # ... so the workspace that really has the stack can connect the account.
    b, hb = await workspace(client, login)
    cb = (
        await client.post(f"/v1/tenants/{b}/connections", json={"awsAccountId": acct}, headers=hb)
    ).json()["connection"]["id"]
    v = await client.post(f"/v1/tenants/{b}/connections/{cb}/validate", headers=hb)
    assert v.json()["status"] == "healthy"
    assert verified(cb)


async def test_a_new_link_over_a_disconnected_account_is_not_a_reservation(
    client: AsyncClient, login, quick_create, owner_sql, picky
) -> None:
    acct = account()
    factory = picky(acct)
    tenant, headers, _, cid = await connected(client, login, acct)
    await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    fresh = link_params((await new_link(client, tenant, headers))["quickCreateUrl"])
    factory.denied.add(fresh["param_ExternalId"])
    r = await client.post("/v1/cfn/callback", json=callback_body(fresh, acct))
    assert r.json()["connectionId"] == cid
    assert owner_sql(
        "SELECT status, account_verified_at FROM cloud_connection WHERE id = %s", cid
    ) == [("failed", None)]
