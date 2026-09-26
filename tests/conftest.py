from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from snow_plow_dispatch_bot.agent import DispatchAgent
from snow_plow_dispatch_bot.app import create_app
from snow_plow_dispatch_bot.config import Settings
from snow_plow_dispatch_bot.db import Database
from snow_plow_dispatch_bot.sms import Notifier

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

    def to(self, number: str) -> list[str]:
        return [body for to, body in self.sent if to == number]

    def send(self, to: str, body: str) -> bool:
        self.sent.append((to, body))
        return True


OWNER = "+17165550199"
ON_CALL = "+17165550188"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_path=":memory:",
        validate_twilio_signature=False,
        dispatch_api_token="secret",
        owner_phone=OWNER,
        on_call_phone=ON_CALL,
        twilio_from_number="+17165550100",
        callback_timeframe="30 minutes",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    return Database(settings.database_path)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def sms() -> RecordingSms:
    return RecordingSms()


@pytest.fixture
def notifier(settings, sms) -> Notifier:
    return Notifier(settings, sms)


@pytest.fixture
def app(settings, db, fake_client, sms):
    agent = DispatchAgent(settings, client=fake_client)
    return create_app(settings=settings, db=db, agent=agent, sms=sms)
