"""Outbound SMS: caller confirmations and owner alerts, via the Twilio REST API."""

from __future__ import annotations

import logging
import re
from typing import Protocol

from .config import Settings

log = logging.getLogger(__name__)

_E164_US = re.compile(r"^\+1\d{10}$")


def normalize_phone(raw: str | None) -> str | None:
    """Best-effort US number to E.164; None if it doesn't look like a real number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    candidate = "+" + digits
    return candidate if _E164_US.match(candidate) else None


class SmsSender(Protocol):
    def send(self, to: str, body: str, from_: str) -> bool: ...


class NullSmsSender:
    """Used when Twilio REST credentials are not configured (local dev, tests)."""

    def send(self, to: str, body: str, from_: str) -> bool:
        log.info("SMS disabled; would send from %s to %s: %s", from_, to, body)
        return False


class TwilioSmsSender:
    def __init__(self, settings: Settings) -> None:
        from twilio.rest import Client

        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def send(self, to: str, body: str, from_: str) -> bool:
        try:
            self._client.messages.create(to=to, from_=from_, body=body)
            return True
        except Exception:  # SMS is best-effort; never fail the call over it
            log.exception("failed to send SMS to %s", to)
            return False


class Notifier:
    """Texts from one business's Twilio number; skips blocked/anonymous caller IDs."""

    def __init__(self, sender: SmsSender, from_number: str, alert_phone: str | None) -> None:
        self.sender = sender
        self.from_number = from_number
        self.alert_phone = alert_phone

    def text(self, to: str | None, body: str) -> bool:
        number = normalize_phone(to)
        if not number:
            return False
        return self.sender.send(number, body, self.from_number)

    def alert_owner(self, body: str) -> bool:
        return self.text(self.alert_phone, body)


def build_sms_sender(settings: Settings) -> SmsSender:
    if settings.send_sms and settings.twilio_rest_enabled:
        return TwilioSmsSender(settings)
    return NullSmsSender()
