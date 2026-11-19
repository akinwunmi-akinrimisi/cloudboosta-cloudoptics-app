"""POST /v1/site/visits: the public site's first-party beacon (SITE-15, docs/14 §8.4).

Public and unauthenticated, limited per client address (``rate_limit_site_per_minute``).
The body is small JSON, sent as ``text/plain`` so a browser needs no preflight; anything over
``MAX_BODY`` bytes is refused before it is parsed.

What is stored is anonymous by construction (``site_event``, migration 0038): the event
kind, the page path, the referring host, the campaign fields, the CTA name and the hour.
The client's address is used only for the rate limit (hashed there, never stored) and the
user agent only to drop crawlers; neither is written anywhere. Requests that ask not to be
tracked (``DNT: 1`` or ``Sec-GPC: 1``), crawlers and paths outside the public site
(``/demo``, ``/app``, ``/api``) are answered the same way (204) and not stored.
"""

from __future__ import annotations

import random
import re
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError, field_validator
from sqlalchemy import text

from cloudoptics.analytics.funnel import clean_path, clean_text
from cloudoptics.identity.deps import Session
from cloudoptics.platform.api import ApiModel, ApiProblem
from cloudoptics.platform.ratelimit import per_ip
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/site", tags=["analytics"])

MAX_BODY = 2048
_EXPIRE_SHARE = 0.01
NOT_TRACKED_PREFIXES = ("/demo", "/app", "/api")
_BOT = re.compile(
    r"bot|crawl|spider|slurp|preview|headless|lighthouse|pagespeed|facebookexternalhit|"
    r"embedly|curl|wget|python-|httpx|go-http|okhttp|java/|monitor|uptime",
    re.IGNORECASE,
)
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")

INSERT_VISIT = text(
    "INSERT INTO site_event (kind, path, landing, referrer_host, cta, utm_source, utm_medium, "
    "utm_campaign, utm_content, utm_term, ref) VALUES (:kind, :path, :landing, :referrer, "
    ":cta, :utm_source, :utm_medium, :utm_campaign, :utm_content, :utm_term, :ref)"
)
ANALYTICS_SCOPE = text("SELECT set_config('app.analytics_read', 'on', true)")
EXPIRE_VISITS = text("DELETE FROM site_event WHERE occurred_at < now() - interval '400 days'")

Campaign = Field(default=None, max_length=100)


class SiteVisit(ApiModel):
    """One beacon: a page view, or a click on a call to action."""

    kind: Literal["page_view", "cta_click"]
    path: str = Field(min_length=1, max_length=200, pattern=r"^/[A-Za-z0-9\-._~/%]*$")
    landing: bool = False
    referrer_host: str | None = Field(default=None, max_length=253)
    cta: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_-]+$")
    utm_source: str | None = Campaign
    utm_medium: str | None = Campaign
    utm_campaign: str | None = Campaign
    utm_content: str | None = Campaign
    utm_term: str | None = Campaign
    ref: str | None = Campaign

    @field_validator("referrer_host")
    @classmethod
    def _host(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        value = value.lower().rstrip(".")
        if not _HOST.match(value):
            raise ValueError("must be a host name")
        return value


def _not_tracked(request: Request, visit: SiteVisit) -> bool:
    headers = request.headers
    if headers.get("dnt") == "1" or headers.get("sec-gpc") == "1":
        return True
    if _BOT.search(headers.get("user-agent", "")) or not headers.get("user-agent"):
        return True
    path = visit.path.rstrip("/") or "/"
    return any(path == p or path.startswith(f"{p}/") for p in NOT_TRACKED_PREFIXES)


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise ApiProblem(status.HTTP_413_CONTENT_TOO_LARGE, "too_large", "Request too large")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise ApiProblem(status.HTTP_413_CONTENT_TOO_LARGE, "too_large", "Request too large")
    return body


@router.post(
    "/visits",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(per_ip("site", lambda: get_settings().rate_limit_site_per_minute))],
)
async def record_visit(request: Request, session: Session) -> Response:
    """Store one anonymous page view or click. Always 204 when the body is valid."""
    body = await _read_body(request)
    try:
        visit = SiteVisit.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**e, "loc": ("body", *e["loc"])} for e in exc.errors()]
        ) from None
    if not _not_tracked(request, visit):
        path = clean_path(visit.path)
        if path:
            await session.execute(
                INSERT_VISIT,
                {
                    "kind": visit.kind,
                    "path": path.rstrip("/") or "/",
                    "landing": visit.landing and visit.kind == "page_view",
                    "referrer": visit.referrer_host,
                    "cta": visit.cta if visit.kind == "cta_click" else None,
                    "utm_source": clean_text(visit.utm_source, 100, lower=True),
                    "utm_medium": clean_text(visit.utm_medium, 100, lower=True),
                    "utm_campaign": clean_text(visit.utm_campaign, 100),
                    "utm_content": clean_text(visit.utm_content, 100),
                    "utm_term": clean_text(visit.utm_term, 100),
                    "ref": clean_text(visit.ref, 100, lower=True),
                },
            )
        if random.random() < _EXPIRE_SHARE:  # noqa: S311 - load spreading, not security
            await session.execute(ANALYTICS_SCOPE)
            await session.execute(EXPIRE_VISITS)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
