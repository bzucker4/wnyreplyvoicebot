"""The Claude-driven conversation loop for one caller utterance.

Twilio calls us once per thing the caller says, so the conversation history is
persisted between webhooks (see db.CallSession). That is why this module runs
its own tool loop instead of the SDK tool runner: each turn has to resume from
stored history and stop with a spoken reply.

History is append-only: earlier turns are never edited or removed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from .config import Settings
from .db import CallSession
from .tools import TOOL_DEFINITIONS, CallControl, ToolContext, execute_tool

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
LOCAL_TZ = ZoneInfo("America/New_York")

GREETING = "Thanks for calling {company}. I can schedule a plow, check on a truck, or change a request. How can I help?"

SORRY_LINE = "Sorry, I'm having trouble on my end."

SYSTEM_PROMPT = """\
You are the phone agent for {company}, a snow plowing and salting service in Western New York \
(Buffalo, and Erie and Niagara counties). Callers reach you by phone during and after storms.

Everything you write is read aloud by text-to-speech. Latency-sensitive; begin your visible answer \
immediately.
- Reply in one to three short, plain sentences. No lists, markdown, symbols, emoji, or URLs.
- Ask for one piece of information at a time.
- Say numbers the way people speak them: "request two forty-one", "about forty-five minutes".
- The caller's words come from speech recognition and may contain errors. When a street name, \
house number, or town sounds unusual, spell it back or ask them to repeat it.

What you can do:
- Schedule a plow or salting visit (driveway, parking lot, sidewalk, or salting only).
- Tell a caller the status and estimated arrival of their request.
- Cancel a request that has not been picked up by a truck.
- Transfer to a human dispatcher.

How to handle a call:
1. Call lookup_caller_account first so you can greet returning customers by name and reuse their \
address when they confirm it is the same one.
2. For a new request, collect: name, street address, town, service type, and anything the driver \
needs to know. Check the town with check_service_area. Ask whether it is urgent (for example they \
must leave for a hospital shift); default to standard.
3. Before calling create_service_request, read the address and service back and wait for a yes.
4. After creating it, give the request number and estimated arrival, and mention the text \
confirmation if one was sent.
5. When the caller is done, say goodbye and call end_call.

Rules:
- Estimated arrival times are estimates. Never promise an exact time.
- Do not quote prices or discuss billing, contracts, or property damage; transfer to a dispatcher.
- If someone describes a medical emergency, a person trapped, a fire, or downed power lines, tell \
them to hang up and call 9 1 1 right away.
- You can only see and change requests for the phone number the caller is calling from. If they \
ask about another number's request, transfer to a dispatcher.
- If a tool returns an error, explain simply and offer a dispatcher rather than guessing.
- Stay on the topic of snow service. Politely decline anything else.
"""


@dataclass
class TurnResult:
    speech: str
    control: CallControl


def build_system_prompt(settings: Settings) -> str:
    return SYSTEM_PROMPT.format(company=settings.company_name)


def greeting(settings: Settings) -> str:
    return GREETING.format(company=settings.company_name)


def _call_context(settings: Settings, caller_phone: str, now: datetime) -> str:
    last4 = re.sub(r"\D", "", caller_phone)[-4:] or "unknown"
    return (
        "<call_context>\n"
        f"Caller phone number ends in: {last4}\n"
        f"Local time in Buffalo: {now.strftime('%A %B %-d, %-I:%M %p')}\n"
        f'You already greeted the caller with: "{greeting(settings)}"\n'
        "</call_context>"
    )


def _dump_blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Serialize response blocks unchanged (including thinking signatures) for storage."""
    return [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in content]


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_#`>|~]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class DispatchAgent:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        self.client = client or anthropic.Anthropic(
            timeout=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
        )
        self.system_prompt = build_system_prompt(settings)

    def _create(self, messages: list[dict[str, Any]]) -> Any:
        return self.client.beta.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=16000,
            system=self.system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.anthropic_effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    def respond(
        self,
        session: CallSession,
        caller_text: str,
        ctx: ToolContext,
        now: datetime | None = None,
    ) -> TurnResult:
        """Append the caller's words to the session, run Claude until it has a reply, return it."""
        messages = session.messages
        user_content: list[dict[str, Any]] = []
        if not messages:
            now = now or datetime.now(LOCAL_TZ)
            user_content.append({"type": "text", "text": _call_context(self.settings, session.caller_phone, now)})
        user_content.append({"type": "text", "text": f"Caller: {caller_text}"})
        messages.append({"role": "user", "content": user_content})

        for _ in range(self.settings.max_agent_steps):
            try:
                response = self._create(messages)
            except anthropic.APIError:
                log.exception("Claude request failed for call %s", session.call_sid)
                return self._give_up(messages, ctx)

            if response.stop_reason == "refusal":
                log.warning("refusal on call %s: %s", session.call_sid, response.stop_details)
                return self._give_up(messages, ctx)

            messages.append({"role": "assistant", "content": _dump_blocks(response.content)})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text = clean_for_speech(" ".join(b.text for b in response.content if b.type == "text"))

            if not tool_uses:
                if not text:
                    return self._give_up(messages, ctx)
                return TurnResult(speech=text, control=ctx.control)

            if response.stop_reason == "max_tokens":
                # A truncated tool call must not run; close it out and bail.
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": t.id, "is_error": True,
                     "content": "Response was truncated; tool not run."}
                    for t in tool_uses
                ]})
                return self._give_up(messages, ctx)

            results = []
            for t in tool_uses:
                output, is_error = execute_tool(ctx, t.name, t.input)
                log.info("call %s tool %s error=%s", session.call_sid, t.name, is_error)
                results.append({"type": "tool_result", "tool_use_id": t.id, "content": output,
                                 "is_error": is_error})
            messages.append({"role": "user", "content": results})

            # Claude usually says something before transferring or hanging up;
            # if it did, don't spend another round trip.
            if (ctx.control.transfer or ctx.control.hang_up) and text:
                return TurnResult(speech=text, control=ctx.control)

        log.warning("call %s exceeded %d agent steps", session.call_sid, self.settings.max_agent_steps)
        return self._give_up(messages, ctx)

    def _give_up(self, messages: list[dict[str, Any]], ctx: ToolContext) -> TurnResult:
        if ctx.settings.dispatcher_phone:
            ctx.control.transfer = True
            ctx.control.transfer_reason = "agent error"
            speech = f"{SORRY_LINE} Let me connect you with a dispatcher."
        else:
            ctx.control.hang_up = True
            speech = f"{SORRY_LINE} Please call back in a few minutes, or text this number and we'll follow up."
        if messages and messages[-1]["role"] == "user":
            messages.append({"role": "assistant", "content": [{"type": "text", "text": speech}]})
        return TurnResult(speech=speech, control=ctx.control)
