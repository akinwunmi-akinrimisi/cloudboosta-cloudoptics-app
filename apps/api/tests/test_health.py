from httpx import ASGITransport, AsyncClient

from cloudoptics.main import PROBLEM_JSON, create_app


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def test_healthz() -> None:
    async with client() as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readyz_checks_the_database(app) -> None:  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_readyz_reports_an_unreachable_database(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import create_async_engine

    from cloudoptics import main

    dead = create_async_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")
    monkeypatch.setattr(main, "get_engine", lambda: dead)
    async with client() as c:
        response = await c.get("/readyz")
    assert response.status_code == 503
    assert response.json()["title"] == "Database unavailable"


async def test_unknown_route_returns_problem_json() -> None:
    async with client() as c:
        response = await c.get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 404
    assert body["instance"] == "/does-not-exist"
