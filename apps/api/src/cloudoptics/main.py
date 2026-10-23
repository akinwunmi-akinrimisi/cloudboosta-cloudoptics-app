"""API process entry point."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloudoptics import __version__
from cloudoptics.connectors import routes as connector_routes
from cloudoptics.fx import routes as fx_routes
from cloudoptics.identity import dev as dev_routes
from cloudoptics.identity import routes as identity_routes
from cloudoptics.ingestion import routes as ingestion_routes
from cloudoptics.platform.db import get_engine
from cloudoptics.platform.settings import get_settings

PROBLEM_JSON = "application/problem+json"


def _problem(request: Request, status: int, title: str, **extra: object) -> JSONResponse:
    """RFC 9457 problem details (docs/09 §3.4)."""
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON,
        content={
            "type": "about:blank",
            "title": title,
            "status": status,
            "instance": request.url.path,
            **extra,
        },
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Cloudoptics API",
        version=__version__,
        docs_url="/docs" if settings.environment != "production" else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["authorization", "content-type"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_problem(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        response = _problem(request, exc.status_code, str(exc.detail))
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return _problem(request, 422, "Request validation failed", errors=errors)

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["platform"])
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: the app can reach PostgreSQL as its runtime role."""
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:  # any failure means not ready; details go to logs, not the response
            return _problem(request, 503, "Database unavailable")
        return JSONResponse({"status": "ready", "environment": settings.environment})

    app.include_router(identity_routes.router)
    app.include_router(connector_routes.router)
    app.include_router(connector_routes.accounts_router)
    app.include_router(ingestion_routes.router)
    app.include_router(fx_routes.router)
    if settings.auth_mode == "dev":
        app.include_router(dev_routes.router)
    return app


app = create_app()
