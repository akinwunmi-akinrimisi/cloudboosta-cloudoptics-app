"""Shared API building blocks: camelCase JSON models and request context."""

from __future__ import annotations

import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from cloudoptics.audit.log import Actor


class ApiModel(BaseModel):
    """JSON uses camelCase (docs/09 §3.1); Python uses snake_case."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


def user_actor(request: Request, user_id: uuid.UUID) -> Actor:
    return Actor(
        type="user",
        id=user_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
