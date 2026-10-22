"""API process entry point.

Sprint 0 skeleton: health endpoints and RFC 9457 problem+json errors. Routers for each
module are added from Sprint 1 (WBS API-1).
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloudoptics import __version__
from cloudoptics.platform.settings import get_settings

PROBLEM_JSON = "application/problem+json"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Cloudoptics API",
        version=__version__,
        docs_url="/docs" if settings.environment != "production" else None,
    )

    @app.exception_handler(StarletteHTTPException)
    async def problem_response(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            media_type=PROBLEM_JSON,
            content={
                "type": "about:blank",
                "title": str(exc.detail),
                "status": exc.status_code,
                "instance": request.url.path,
            },
        )

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["platform"])
    async def readyz() -> dict[str, str]:
        """Readiness: dependency checks (database, queue) are added in Sprint 1."""
        return {"status": "ready", "environment": settings.environment}

    return app


app = create_app()
