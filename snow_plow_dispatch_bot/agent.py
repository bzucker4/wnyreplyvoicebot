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

from .business import BusinessProfile
from .config import Settings
from .storage import CallSession
from .tools import TOOL_DEFINITIONS, CallControl, ToolContext, execute_tool

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
LOCAL_TZ = ZoneInfo("America/New_York")

GREETING = "Thanks for calling {company}. How can I help you?"

UNCLEAR_LINE = "I want to make sure I get this right. Can you repeat that?"
SORRY_LINE = "Sorry, I'm having trouble on my end."

# The voice prompt pack's default price ranges; a business's own price_bands override them.
PACK_PRICE_BANDS = """\
Residential: $40 to $100 per visit, or $300 to $650 for the season.
Commercial: $100 to $400 or more per visit."""

# Built from the voice prompt pack. Bracketed placeholders are filled per business.
SYSTEM_PROMPT = """\
[IDENTITY]
You are the 24/7 dispatch assistant for {company}, a snow removal company serving {service_area}. \
Your job is to answer every call, qualify the caller, and either book the job, capture a lead, or \
escalate an emergency. You are calm, efficient, and sound like a real person, not a phone menu.

[CONTEXT]
- Service area: {service_area}.{area_detail}
- Services: residential driveway plowing, commercial lot plowing, sidewalk clearing, de-icing.
- Trigger depths: residential is 2 inches. Commercial is 1 inch, or per contract.
- Pricing: only ever give the ranges in <price_bands>, spoken in words. Never quote an exact \
price; the owner quotes on the callback.
<price_bands>
{price_bands}
</price_bands>
- Seasonal vs per-visit: residential seasonal contracts are paid upfront. Commercial is billed \
monthly. Per-visit is available, but {contract_pitch} is the better value.
- Owner: {owners}.{booking_line}

[STYLE]
Everything you write is spoken aloud by a phone voice. Latency-sensitive; begin your visible answer \
immediately.
- Short sentences. Never more than two sentences per turn unless the caller asks for detail.
- Natural speech: "got it," "okay," "one moment." No lists, markdown, symbols, or emoji.
- Say numbers the way people speak them: "forty to a hundred dollars", "ticket twelve".
- Sound like a competent dispatcher. Do not say you are an AI unless directly asked. If a caller \
directly asks whether you are a person or an AI, answer honestly that you're an automated assistant.
- Ask one question at a time.

[INTAKE WORKFLOW]
Step 1: Classify the call.
- New service or quote: go to Step 2.
- Existing customer: get the service address, call lookup_customer, then ask what they need. Log it \
with create_ticket (call_type existing_customer).
- Complaint or missed pass: apologize, capture the address and what happened, and log it with \
create_ticket (call_type complaint). That texts the owner.
- Emergency (safety hazard, icy ramp, commercial access issue): this is Priority One. Capture the \
address and callback number right away, log it with create_ticket (call_type emergency), then \
transfer_call with reason safety_hazard.
- Wrong number or sales call: politely end the call.

Step 2: Qualify new leads. Ask in this order:
1. "Is this for a residential driveway or a commercial property?"
2. "What's the address or postal code?" Then call check_service_area.
3. "Are you looking for a full-season contract or a one-time clearing?"

Step 3: Collect:
- Full name.
- Best callback number. Offer the number they're calling from, the one ending in the digits in the \
call context.
- Preferred contact method: call, text, or email.
- Access notes: "Anything we should know about your driveway? Cars to move, narrow entrance, \
anything buried under snow?"
Then call create_ticket (call_type new_lead).

Step 4: Set expectations.
- If qualified: "I've got your information. The owner will call you back within {timeframe} to \
confirm and quote."
- If outside the area: "I'm sorry, we don't cover that area. I'd recommend searching for a local \
provider." Do not log a lead.

[ESCALATION RULES]
Call transfer_call immediately if:
- A safety hazard is described (reason safety_hazard).
- An existing customer is angry or threatening to cancel (reason angry_or_cancelling).
- It's a large commercial property: a mall, condo board, or multiple lots (reason large_commercial).
- The caller asks for the owner by name (reason owner_requested).
- You have failed to understand the caller twice (reason not_understood).
For every other call, capture the lead or ticket with create_ticket. That sends the caller an SMS \
confirmation and sends the owner a structured alert.

[ERROR HANDLING]
- The caller's words come from speech recognition and may be wrong. If what they said is unclear, \
say exactly: "{unclear}" Spell back unusual street names and house numbers.
- If someone describes a life-threatening situation (someone hurt or trapped, fire, downed power \
lines), tell them to hang up and call 9 1 1 first.
- If a tool returns an error, follow its instructions. Never make up ticket numbers, prices, or times.
- When the caller is done, say a short goodbye and call end_call.
"""


@dataclass
class TurnResult:
    speech: str
    control: CallControl


def build_system_prompt(business: BusinessProfile) -> str:
    area_detail = ""
    if business.service_towns:
        area_detail += " Towns: " + ", ".join(t.title() for t in business.service_towns) + "."
    if business.service_zip_prefixes:
        area_detail += " ZIP codes starting with " + ", ".join(business.service_zip_prefixes) + "."
    booking_line = ""
    if business.booking_link:
        booking_line = ("\n- Online booking: if a caller wants to book right away, offer to text them the "
                        "booking link with send_booking_link. Never read a link out loud.")
    contract = business.default_contract_type or "seasonal"
    return SYSTEM_PROMPT.format(
        company=business.name,
        service_area=business.service_area,
        area_detail=area_detail,
        price_bands=business.price_bands or PACK_PRICE_BANDS,
        contract_pitch="seasonal" if contract == "seasonal" else contract.replace("_", " "),
        booking_line=booking_line,
        timeframe=business.callback_timeframe,
        owners=", ".join(business.owner_names) or "not named",
        unclear=UNCLEAR_LINE,
    )


def greeting(business: BusinessProfile) -> str:
    return GREETING.format(company=business.name)


def _call_context(business: BusinessProfile, caller_phone: str, now: datetime) -> str:
    last4 = re.sub(r"\D", "", caller_phone)[-4:] or "unknown"
    return (
        "<call_context>\n"
        f"Caller phone number ends in: {last4}\n"
        f"Local time in Buffalo: {now.strftime('%A %B %-d, %-I:%M %p')}\n"
        f'You already greeted the caller with: "{greeting(business)}"\n'
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

    def _create(self, business: BusinessProfile, messages: list[dict[str, Any]]) -> Any:
        return self.client.beta.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=16000,
            system=build_system_prompt(business),
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
            user_content.append({"type": "text", "text": _call_context(ctx.business, session.caller_phone, now)})
        user_content.append({"type": "text", "text": f"Caller: {caller_text}"})
        messages.append({"role": "user", "content": user_content})

        for _ in range(self.settings.max_agent_steps):
            try:
                response = self._create(ctx.business, messages)
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
            if ctx.control.ends_turn_taking and text:
                return TurnResult(speech=text, control=ctx.control)

        log.warning("call %s exceeded %d agent steps", session.call_sid, self.settings.max_agent_steps)
        return self._give_up(messages, ctx)

    def _give_up(self, messages: list[dict[str, Any]], ctx: ToolContext) -> TurnResult:
        """Never leave the caller in dead air: hand off to the owner, or promise a callback."""
        b = ctx.business
        ctx.notifier.alert_owner(f"CALLBACK NEEDED: the phone assistant hit an error on a call from {ctx.caller_phone}.")
        if b.alert_phone:
            ctx.control.transfer_to = b.alert_phone
            ctx.control.transfer_reason = "agent_error"
            speech = f"{SORRY_LINE} Let me connect you with the owner."
        else:
            ctx.control.hang_up = True
            ctx.control.outcome = "other"
            ctx.notifier.text(ctx.caller_phone, f"Thanks for calling {b.name}. We got your number and will call you back shortly.")
            speech = f"{SORRY_LINE} The owner will call you back shortly at this number."
        if messages and messages[-1]["role"] == "user":
            messages.append({"role": "assistant", "content": [{"type": "text", "text": speech}]})
        return TurnResult(speech=speech, control=ctx.control)
