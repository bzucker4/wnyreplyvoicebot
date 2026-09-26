"""Outbound SMS confirmations via the Twilio REST API."""

from __future__ import annotations

import logging
from typing import Protocol

from .config import Settings

log = logging.getLogger(__name__)


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> bool: ...


class NullSmsSender:
    """Used when Twilio REST credentials are not configured (local dev, tests)."""

    def send(self, to: str, body: str) -> bool:
        log.info("SMS disabled; would send to %s: %s", to, body)
        return False


class TwilioSmsSender:
    def __init__(self, settings: Settings) -> None:
        from twilio.rest import Client

        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._from = settings.twilio_from_number

    def send(self, to: str, body: str) -> bool:
        try:
            self._client.messages.create(to=to, from_=self._from, body=body)
            return True
        except Exception:  # SMS is best-effort; never fail the call over it
            log.exception("failed to send SMS to %s", to)
            return False


def build_sms_sender(settings: Settings) -> SmsSender:
    if settings.send_sms_confirmations and settings.twilio_rest_enabled:
        return TwilioSmsSender(settings)
    return NullSmsSender()
