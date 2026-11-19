"""Every route with ``{tenant_id}`` in its path requires membership of that workspace.

The routes come from the app's own route table, so a new workspace route is covered the
day it is added. Each one must depend on ``require_role`` (ViewerAccess / MemberAccess /
AdminAccess), and a signed-in user who is not a member (they have a workspace of their own)
gets the same 404 as for a workspace that does not exist (multi-cloud plan, Phase 0).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from httpx import AsyncClient

# (method, path) -> why the route may skip the membership check. None today: the CloudFormation
# callback (HMAC-signed, /v1/connections/callback) and the staff routes (/v1/admin/...) carry
# no {tenant_id} in their paths.
EXEMPT: dict[tuple[str, str], str] = {}

SAMPLE = {"month": "2026-10"}


def _calls(dependant: Dependant) -> Iterator[Any]:
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def _requires_role(dependant: Dependant) -> bool:
    return any(
        getattr(call, "__qualname__", "") == "require_role.<locals>.dependency"
        for call in _calls(dependant)
    )


def tenant_routes(app) -> list[tuple[str, str, Dependant]]:  # type: ignore[no-untyped-def]
    found = []
    for route in iter_route_contexts(app.routes):
        path = route.path or ""
        if "/tenants/{" not in path and "{tenant_id}" not in path:
            continue
        for method in sorted(route.methods or ()):
            if method != "HEAD":
                found.append((method, path, route.dependant))
    return found


def _fill(path: str, tenant: str) -> str:
    out = path.replace("{tenant_id}", tenant)
    while "{" in out:
        start = out.index("{")
        name = out[start + 1 : out.index("}", start)]
        out = out.replace("{" + name + "}", SAMPLE.get(name, str(uuid.uuid4())), 1)
    return out


def test_the_route_table_is_found(app) -> None:  # type: ignore[no-untyped-def]
    routes = tenant_routes(app)
    assert len(routes) > 60
    # Workspace routes name the workspace {tenant_id}: the access dependency reads that name.
    assert [p for _, p, _ in routes if "/tenants/{" in p and "/tenants/{tenant_id}" not in p] == []
    assert set(EXEMPT) - {(m, p) for m, p, _ in routes} == set(), "stale EXEMPT entry"
    # The dependency check tells routes apart: /v1/me needs a user, not a membership.
    [me] = [r for r in iter_route_contexts(app.routes) if r.path == "/v1/me" and "GET" in r.methods]
    assert not _requires_role(me.dependant)


def test_every_workspace_route_depends_on_require_role(app) -> None:  # type: ignore[no-untyped-def]
    missing = [
        f"{m} {p}"
        for m, p, dependant in tenant_routes(app)
        if (m, p) not in EXEMPT and not _requires_role(dependant)
    ]
    assert missing == [], "use ViewerAccess / MemberAccess / AdminAccess"


async def test_non_members_get_404_on_every_workspace_route(
    app,  # type: ignore[no-untyped-def]
    client: AsyncClient,
    login,
) -> None:
    owner = await login()
    r = await client.post(
        "/v1/tenants", json={"name": "Private", "countryCode": "NG"}, headers=owner
    )
    tenant = r.json()["id"]
    outsider = await login()
    r = await client.post(
        "/v1/tenants", json={"name": "Other", "countryCode": "KE"}, headers=outsider
    )
    assert r.status_code == 201

    wrong = []
    for method, path, _ in tenant_routes(app):
        if (method, path) in EXEMPT:
            continue
        for target in (tenant, str(uuid.uuid4())):
            r = await client.request(method, _fill(path, target), headers=outsider, json={})
            if r.status_code != 404:
                wrong.append(f"{method} {path}: {r.status_code}")
    assert wrong == []
    # Nothing was changed by the attempts: the workspace still has its one member and name.
    members = await client.get(f"/v1/tenants/{tenant}/members", headers=owner)
    assert len(members.json()) == 1
    assert (await client.get(f"/v1/tenants/{tenant}", headers=owner)).json()["name"] == "Private"
