"""Bearer-token authentication.

Login, sign-up and MFA are handled by the OIDC provider (ADR-006); the API only verifies
access tokens. ``dev`` mode verifies HS256 tokens minted by ``/v1/dev/token`` so the app
runs locally and in Codespaces without a provider; settings refuse it in staging/prod.

WorkOS AuthKit access tokens are RS256 JWTs signed with keys published at
``/sso/jwks/<client_id>``. They carry ``sub`` and ``client_id`` but not the email by
default, so the verifier reads the email from the WorkOS API (cached per user) unless a JWT
template adds it. The issuer appears as both ``https://api.workos.com`` and
``https://api.workos.com/user_management/<client_id>``; both are accepted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import httpx
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


@dataclass
class WorkOSVerifier:
    client_id: str
    api_key: str
    api_base: str = "https://api.workos.com"
    http: httpx.AsyncClient | None = None
    _profiles: dict[str, tuple[str, str | None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._jwks = jwt.PyJWKClient(
            f"{self.api_base}/sso/jwks/{self.client_id}", cache_keys=True, lifespan=3600
        )
        base = self.api_base.rstrip("/")
        self._issuers = {base, f"{base}/", f"{base}/user_management/{self.client_id}"}

    def _decode(self, token: str) -> dict[str, Any]:
        key = self._jwks.get_signing_key_from_jwt(token)
        payload: dict[str, Any] = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            options={"require": ["exp", "sub", "iss"], "verify_aud": False},
        )
        if payload["iss"] not in self._issuers:
            raise jwt.InvalidIssuerError("unexpected issuer")
        if payload.get("client_id", self.client_id) != self.client_id:
            raise jwt.InvalidTokenError("token was issued to another client")
        return payload

    async def _profile(self, user_id: str) -> tuple[str, str | None]:
        if user_id not in self._profiles:
            if not self.api_key:
                raise jwt.InvalidTokenError("token has no email and no WorkOS API key is set")
            client = self.http or httpx.AsyncClient(timeout=10)
            try:
                res = await client.get(
                    f"{self.api_base}/user_management/users/{user_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            finally:
                if self.http is None:
                    await client.aclose()
            if res.status_code != 200:
                raise jwt.InvalidTokenError("could not read the user from WorkOS")
            user = res.json()
            name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
            self._profiles[user_id] = (str(user["email"]), name or None)
        return self._profiles[user_id]

    async def verify(self, token: str) -> Claims:
        payload = await run_in_threadpool(self._decode, token)
        email, name = payload.get("email"), payload.get("name")
        if not email:
            email, name = await self._profile(str(payload["sub"]))
        return Claims(subject=f"workos|{payload['sub']}", email=str(email), name=name)


def build_verifier(settings: Settings) -> TokenVerifier:
    if settings.auth_mode == "oidc" and settings.workos_client_id:
        return WorkOSVerifier(
            settings.workos_client_id, settings.workos_api_key, settings.workos_api_base
        )
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
    settings = get_settings()
    if settings.auth_mode == "oidc" and not settings.oidc_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign-in is not configured yet")
    try:
        return await get_verifier().verify(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
