"""Sending email through Postmark (docs/11c I6).

``get_mailer()`` returns the Postmark sender when a server token is configured, otherwise
one that only logs, so local runs and tests never send. Tests swap in a fake with
``set_mailer``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

POSTMARK_URL = "https://api.postmarkapp.com/email"


@dataclass(frozen=True)
class Email:
    to: list[str]
    subject: str
    text: str
    html: str
    tag: str


class EmailError(Exception):
    pass


class Mailer(Protocol):
    async def send(self, email: Email) -> None: ...


class LogMailer:
    """No token configured: write the email to the log instead of sending it."""

    async def send(self, email: Email) -> None:
        log.info(
            "email not sent (no Postmark token): %s to %d recipients", email.tag, len(email.to)
        )


class PostmarkMailer:
    def __init__(self, token: str, sender: str, client: httpx.AsyncClient | None = None) -> None:
        self.token = token
        self.sender = sender
        self.client = client

    async def send(self, email: Email) -> None:
        body = {
            "From": self.sender,
            "To": ", ".join(email.to),
            "Subject": email.subject,
            "TextBody": email.text,
            "HtmlBody": email.html,
            "Tag": email.tag,
            "MessageStream": "outbound",
        }
        headers = {"Accept": "application/json", "X-Postmark-Server-Token": self.token}
        try:
            if self.client is not None:
                response = await self.client.post(POSTMARK_URL, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(POSTMARK_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EmailError(f"postmark_unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            try:
                detail = response.json()
                message = f"{detail.get('ErrorCode')}: {detail.get('Message')}"
            except ValueError:
                message = response.text[:200]
            raise EmailError(f"postmark_{response.status_code}: {message}")


_mailer: Mailer | None = None


def get_mailer() -> Mailer:
    if _mailer is not None:
        return _mailer
    settings = get_settings()
    if settings.postmark_server_token:
        return PostmarkMailer(settings.postmark_server_token, settings.email_from)
    return LogMailer()


def set_mailer(mailer: Mailer | None) -> None:
    """Swap the mailer (tests use fakes)."""
    global _mailer
    _mailer = mailer
