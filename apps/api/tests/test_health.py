from httpx import ASGITransport, AsyncClient

from cloudoptics.main import PROBLEM_JSON, create_app


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def test_healthz() -> None:
    async with client() as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readyz() -> None:
    async with client() as c:
        response = await c.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_unknown_route_returns_problem_json() -> None:
    async with client() as c:
        response = await c.get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 404
    assert body["instance"] == "/does-not-exist"
