from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from typing import Any

import pytest
import sqlalchemy as sa
from anthropic.types.beta import BetaMessage

from snow_plow_dispatch_bot.agent import DispatchAgent
from snow_plow_dispatch_bot.app import create_app
from snow_plow_dispatch_bot.config import Settings
from snow_plow_dispatch_bot.business import BusinessProfile
from snow_plow_dispatch_bot.service_area import WNY_TOWNS, WNY_ZIP_PREFIXES
from snow_plow_dispatch_bot.sms import Notifier
from snow_plow_dispatch_bot.storage import Database, businesses, voice_settings

_ids = itertools.count(1)


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool_use(name: str, input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": name, "input": input}


def message(*content: dict[str, Any], stop_reason: str | None = None) -> BetaMessage:
    if stop_reason is None:
        stop_reason = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
    return BetaMessage.model_validate({
        "id": f"msg_{next(_ids):04d}",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    })


@dataclass
class FakeMessages:
    script: list[BetaMessage | Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> BetaMessage:
        # Snapshot messages: the agent keeps appending to the same list.
        kwargs = {**kwargs, "messages": [dict(m) for m in kwargs["messages"]]}
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()

        class _Beta:
            pass

        self.beta = _Beta()
        self.beta.messages = self.messages


class RecordingSms:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.from_numbers: list[str] = []

    def to(self, number: str) -> list[str]:
        return [body for to, body in self.sent if to == number]

    def send(self, to: str, body: str, from_: str) -> bool:
        self.sent.append((to, body))
        self.from_numbers.append(from_)
        return True


BIZ = "+17165550100"
OTHER_BIZ = "+15855550100"
OWNER = "+17165550199"
ON_CALL = "+17165550188"
BOOKING_LINK = "https://book.example.com/wny-test"

_VOICE_TABLES = "voice_tickets, voice_customers, voice_calls, voice_settings, businesses"


def seed_businesses(db: Database) -> None:
    with db.engine.begin() as c:
        for row in [
            {"twilio_number": BIZ, "business_name": "WNY Test Plowing", "service_area": "Buffalo and its suburbs",
             "booking_link": BOOKING_LINK, "contract_type": "seasonal", "active": True, "alert_phone": OWNER,
             "price_bands": "Residential driveway, seasonal: $450-750\nResidential per-push: $45-75"},
            {"twilio_number": OTHER_BIZ, "business_name": "Rochester Snow Co", "service_area": "Rochester",
             "booking_link": "", "price_bands": "", "active": True, "alert_phone": "+15855550199"},
        ]:
            c.execute(businesses.insert().values(**row))
        c.execute(voice_settings.insert().values(
            twilio_number=BIZ, on_call_phone=ON_CALL, callback_timeframe="30 minutes",
            owner_names="Mike,Mike Kowalski", service_towns=",".join(WNY_TOWNS),
            service_zip_prefixes=",".join(WNY_ZIP_PREFIXES),
        ))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        validate_twilio_signature=False,
        dispatch_api_token="secret",
    )


@pytest.fixture
def db() -> Database:
    """SQLite by default; set TEST_DATABASE_URL to run against Postgres with migrations applied."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        database = Database.from_url(url)
        with database.engine.begin() as c:
            c.execute(sa.text(f"TRUNCATE {_VOICE_TABLES} RESTART IDENTITY CASCADE"))
    else:
        database = Database.from_url("sqlite://")
        database.create_schema()
    seed_businesses(database)
    yield database
    database.engine.dispose()


@pytest.fixture
def business(db, settings) -> BusinessProfile:
    return BusinessProfile.from_row(db.get_business(BIZ), settings)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def sms() -> RecordingSms:
    return RecordingSms()


@pytest.fixture
def notifier(business, sms) -> Notifier:
    return Notifier(sms, business.twilio_number, business.alert_phone)


@pytest.fixture
def app(settings, db, fake_client, sms):
    agent = DispatchAgent(settings, client=fake_client)
    return create_app(settings=settings, db=db, agent=agent, sms=sms)
