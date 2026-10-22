"""Development-only token endpoint, mounted only when ``auth_mode == "dev"``.

Lets the web app, Codespaces and tests sign in as any email without an OIDC provider.
Settings refuse dev auth in staging and production.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import EmailStr, Field

from cloudoptics.identity.auth import DevVerifier
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/dev", tags=["dev"])


class DevTokenRequest(ApiModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=120)


class DevToken(ApiModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - token scheme, not a secret


@router.post("/token", response_model=DevToken, response_model_by_alias=True)
async def dev_token(body: DevTokenRequest) -> DevToken:
    verifier = DevVerifier(get_settings().dev_auth_secret)
    return DevToken(access_token=verifier.mint(str(body.email), body.name))
