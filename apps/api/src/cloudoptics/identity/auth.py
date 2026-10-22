"""Bearer-token authentication.

Login, sign-up and MFA are handled by the OIDC provider (ADR-006); the API only verifies
access tokens. ``dev`` mode verifies HS256 tokens minted by ``/v1/dev/token`` so the app
runs locally and in Codespaces without a provider; settings refuse it in staging/prod.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import jwt
from fastapi import HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from cloudoptics.platform.settings import Settings, get_settings

DEV_ISSUER = "cloudoptics-dev"


@dataclass(frozen=True)
class Claims:
    subject: str
    email: str
    name: str | None


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Claims: ...


def _claims(payload: dict[str, Any]) -> Claims:
    email = payload.get("email")
    if not payload.get("sub") or not email:
        raise jwt.InvalidTokenError("token must carry sub and email")
    return Claims(subject=str(payload["sub"]), email=str(email), name=payload.get("name"))


class DevVerifier:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def verify(self, token: str) -> Claims:
        payload = jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            issuer=DEV_ISSUER,
            audience="cloudoptics-api",
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
        return _claims(payload)

    def mint(self, email: str, name: str | None = None, ttl_seconds: int = 8 * 3600) -> str:
        now = int(time.time())
        payload = {
            "iss": DEV_ISSUER,
            "aud": "cloudoptics-api",
            "sub": f"dev|{email.lower()}",
            "email": email.lower(),
            "name": name,
            "iat": now,
            "exp": now + ttl_seconds,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")


class OidcVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def _verify_sync(self, token: str) -> Claims:
        key = self._jwks.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            issuer=self._issuer,
            audience=self._audience,
            options={"require": ["exp", "sub", "iss"]},
        )
        return _claims(payload)

    async def verify(self, token: str) -> Claims:
        return await run_in_threadpool(self._verify_sync, token)


def build_verifier(settings: Settings) -> TokenVerifier:
    if settings.auth_mode == "oidc":
        return OidcVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    return DevVerifier(settings.dev_auth_secret)


@lru_cache
def get_verifier() -> TokenVerifier:
    return build_verifier(get_settings())


async def bearer_claims(request: Request) -> Claims:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await get_verifier().verify(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
