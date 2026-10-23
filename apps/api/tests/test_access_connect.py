"""Connecting with the single stack: one-click link, the stack's callback, moving over."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

from fake_aws import healthy_clients
from httpx import AsyncClient
from test_connections import workspace


def account() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(12))


def link_params(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {k: v[0] for k, v in parse_qs(parsed.fragment.split("?", 1)[1]).items()}


def callback_body(params: dict[str, str], account: str, **extra: Any) -> dict[str, Any]:
    return {
        "requestType": "Create",
        "ref": params["param_ConnectionRef"],
        "tenantId": params["param_TenantId"],
        "externalId": params["param_ExternalId"],
        "accountId": account,
        "management": True,
        "curBucket": f"cloudoptics-cur-{account}",
        "curPrefix": "cloudoptics",
        "stackName": "cloudoptics-access",
        "templateVersion": "2.0.0",
        **extra,
    }


async def new_link(client: AsyncClient, tenant: str, headers: dict[str, str]) -> dict[str, Any]:
    r = await client.post(f"/v1/tenants/{tenant}/connections/intents", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_one_click_link_needs_no_account_id(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    tenant, headers = await workspace(client, login)
    setup = await new_link(client, tenant, headers)
    assert setup["intent"]["status"] == "waiting"
    params = link_params(setup["quickCreateUrl"])
    assert params["stackName"] == "cloudoptics-access"
    assert params["templateURL"].endswith("/cloudoptics-access.yaml")
    assert params["param_CallbackUrl"].endswith("/api/v1/cfn/callback")
    assert params["param_ConnectionRef"] == f"i:{setup['intent']['id']}"
    assert params["param_TenantId"] == tenant
    assert len(params["param_ExternalId"]) >= 32


async def test_the_stack_reports_its_account_and_the_connection_is_checked(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    setup = await new_link(client, tenant, headers)
    params = link_params(setup["quickCreateUrl"])
    fake_aws.clients = healthy_clients(acct)

    r = await client.post("/v1/cfn/callback", json=callback_body(params, acct))
    assert r.status_code == 200, r.text
    connection_id = r.json()["connectionId"]

    intent = await client.get(
        f"/v1/tenants/{tenant}/connections/intents/{setup['intent']['id']}", headers=headers
    )
    assert intent.json() == {
        "id": setup["intent"]["id"],
        "status": "connected",
        "connectionId": connection_id,
        "error": None,
    }
    conn = (await client.get(f"/v1/tenants/{tenant}/connections", headers=headers)).json()
    [c] = conn
    assert c["awsAccountId"] == acct
    assert c["accountType"] == "management"
    assert c["stackName"] == "cloudoptics-access"
    assert c["stackVersion"] == "2.0.0"
    assert c["status"] == "healthy"  # validated straight away (inline jobs in tests)
    assert fake_aws.calls[-1][0] == f"arn:aws:iam::{acct}:role/CloudopticsBillingReader"

    # The setup step may call again (an update): same connection, no duplicate.
    again = await client.post(
        "/v1/cfn/callback", json=callback_body(params, acct, requestType="Update")
    )
    assert again.json()["connectionId"] == connection_id
    assert len((await client.get(f"/v1/tenants/{tenant}/connections", headers=headers)).json()) == 1


async def test_a_wrong_external_id_is_refused(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    params = link_params((await new_link(client, tenant, headers))["quickCreateUrl"])
    r = await client.post("/v1/cfn/callback", json=callback_body(params, acct, externalId="y" * 43))
    assert r.status_code == 404
    other_tenant, _ = await workspace(client, login)
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(params, acct, tenantId=other_tenant)
    )
    assert r.status_code == 404


async def test_an_account_already_connected_is_explained_on_the_waiting_page(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    first = link_params((await new_link(client, tenant, headers))["quickCreateUrl"])
    fake_aws.clients = healthy_clients(acct)
    assert (
        await client.post("/v1/cfn/callback", json=callback_body(first, acct))
    ).status_code == 200

    second = await new_link(client, tenant, headers)
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(link_params(second["quickCreateUrl"]), acct)
    )
    assert r.status_code == 409
    assert "already connected" in r.json()["title"]
    intent = await client.get(
        f"/v1/tenants/{tenant}/connections/intents/{second['intent']['id']}", headers=headers
    )
    assert intent.json()["status"] == "waiting"
    assert "already connected" in intent.json()["error"]


async def test_an_existing_connection_moves_to_the_single_stack_and_keeps_its_id(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    created = await client.post(
        f"/v1/tenants/{tenant}/connections",
        json={"awsAccountId": acct, "accountType": "management"},
        headers=headers,
    )
    connection_id = created.json()["connection"]["id"]
    assert created.json()["updateAvailable"] is True
    move = link_params(created.json()["accessQuickCreateUrl"])
    assert move["param_ConnectionRef"] == f"c:{connection_id}"
    assert move["param_ExternalId"] == created.json()["externalId"]  # same trust, same history

    fake_aws.clients = healthy_clients(acct)
    wrong = await client.post("/v1/cfn/callback", json=callback_body(move, "999988887777"))
    assert wrong.status_code == 409
    assert f"Sign in to {acct}" in wrong.json()["title"]

    r = await client.post("/v1/cfn/callback", json=callback_body(move, acct))
    assert r.json()["connectionId"] == connection_id
    setup = await client.get(
        f"/v1/tenants/{tenant}/connections/{connection_id}/setup", headers=headers
    )
    assert setup.json()["connection"]["stackName"] == "cloudoptics-access"
    assert setup.json()["updateAvailable"] is False

    log = await client.get(f"/v1/tenants/{tenant}/audit-log", headers=headers)
    assert "connection.stack_changed" in [e["action"] for e in log.json()["entries"]]


async def test_deleting_the_stack_disconnects_the_account(
    client: AsyncClient, login, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, headers = await workspace(client, login)
    params = link_params((await new_link(client, tenant, headers))["quickCreateUrl"])
    fake_aws.clients = healthy_clients(acct)
    await client.post("/v1/cfn/callback", json=callback_body(params, acct))
    r = await client.post(
        "/v1/cfn/callback", json=callback_body(params, acct, requestType="Delete")
    )
    assert r.json()["status"] == "disconnected"
    [c] = (await client.get(f"/v1/tenants/{tenant}/connections", headers=headers)).json()
    assert c["status"] == "disconnected"

    # Creating the stack again from the same link brings it back.
    await client.post("/v1/cfn/callback", json=callback_body(params, acct))
    [c] = (await client.get(f"/v1/tenants/{tenant}/connections", headers=headers)).json()
    assert c["status"] == "healthy"
