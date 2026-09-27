"""Tools Claude can call during a phone conversation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .business import BusinessProfile
from .service_area import check_service_area, normalize_town
from .sms import Notifier, normalize_phone
from .storage import (
    CALL_TYPES,
    CONTACT_METHODS,
    CONTRACT_TYPES,
    PRIORITIES,
    PROPERTY_TYPES,
    SERVICE_TYPES,
    Database,
)

log = logging.getLogger(__name__)

TRANSFER_REASONS = (
    "safety_hazard",
    "angry_or_cancelling",
    "large_commercial",
    "owner_requested",
    "not_understood",
    "caller_request",
)
CALL_OUTCOMES = ("lead_captured", "ticket_logged", "info_only", "out_of_area", "wrong_number", "sales_call", "other")


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _str(description: str | None = None) -> dict[str, Any]:
    return {"type": "string", **({"description": description} if description else {})}


def _enum(values: tuple[str, ...], description: str | None = None) -> dict[str, Any]:
    return {"type": "string", "enum": list(values), **({"description": description} if description else {})}


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "lookup_customer",
        "Pull an existing customer's record by the number they are calling from and, if given, "
        "the service address they said. Use when the caller says they are already a customer.",
        {"address": _nullable(_str("Street address the caller gave, e.g. '42 Elmwood Ave'."))},
    ),
    _tool(
        "check_service_area",
        "Check whether a town or ZIP/postal code is inside the service area. Call as soon as a "
        "new caller gives their address or postal code.",
        {"town": _nullable(_str()), "zip_code": _nullable(_str())},
    ),
    _tool(
        "send_booking_link",
        "Text the caller this business's online booking link. Offer it to callers who want to "
        "book right away; never read a link out loud.",
        {},
    ),
    _tool(
        "create_ticket",
        "Log the call for the owner and send the caller an SMS confirmation. Call once you have "
        "the required details: new_lead needs name, property type, address, contract type, "
        "callback number and preferred contact; emergency needs address and callback number; "
        "complaint and existing_customer need address and what they need. Use null for anything "
        "the caller did not give.",
        {
            "call_type": _enum(CALL_TYPES),
            "priority": _enum(PRIORITIES, "'priority_one' only for emergencies."),
            "caller_name": _nullable(_str()),
            "callback_number": _nullable(_str("Only if different from the number they are calling from.")),
            "preferred_contact": _nullable(_enum(CONTACT_METHODS)),
            "property_type": _nullable(_enum(PROPERTY_TYPES)),
            "service_type": _nullable(_enum(SERVICE_TYPES)),
            "contract_type": _nullable(_enum(CONTRACT_TYPES)),
            "address": _nullable(_str()),
            "town": _nullable(_str()),
            "zip_code": _nullable(_str()),
            "access_notes": _nullable(_str("Cars to move, narrow entrance, obstacles under the snow.")),
            "details": _nullable(_str("What the caller needs, the complaint, or the hazard, in one or two sentences.")),
        },
    ),
    _tool(
        "transfer_call",
        "Transfer the call to a person right away. Safety hazards go to the on-call line; "
        "everything else goes to the owner. Create an emergency ticket first when there is a "
        "safety hazard and you have the address and callback number.",
        {"reason": _enum(TRANSFER_REASONS), "details": _str("One sentence for the person picking up.")},
    ),
    _tool(
        "end_call",
        "Hang up after your goodbye, once the caller has nothing else they need.",
        {"outcome": _enum(CALL_OUTCOMES), "summary": _str("One-sentence summary for the call log.")},
    ),
]


# -- input validation ---------------------------------------------------------
# Strict tool use constrains the schema; these models also bound lengths and
# keep the executor safe if strict mode is ever turned off.

class _LookupInput(BaseModel):
    address: str | None = Field(default=None, max_length=200)


class _Empty(BaseModel):
    model_config = {"extra": "forbid"}


class _AreaInput(BaseModel):
    town: str | None = Field(default=None, max_length=80)
    zip_code: str | None = Field(default=None, max_length=12)


class _TicketInput(BaseModel):
    call_type: Literal["new_lead", "existing_customer", "complaint", "emergency"]
    priority: Literal["normal", "priority_one"]
    caller_name: str | None = Field(default=None, max_length=120)
    callback_number: str | None = Field(default=None, max_length=30)
    preferred_contact: Literal["call", "text", "email"] | None = None
    property_type: Literal["residential", "commercial"] | None = None
    service_type: Literal["driveway_plowing", "lot_plowing", "sidewalk_clearing", "deicing"] | None = None
    contract_type: Literal["seasonal", "one_time", "undecided"] | None = None
    address: str | None = Field(default=None, max_length=200)
    town: str | None = Field(default=None, max_length=80)
    zip_code: str | None = Field(default=None, max_length=12)
    access_notes: str | None = Field(default=None, max_length=500)
    details: str | None = Field(default=None, max_length=500)


class _TransferInput(BaseModel):
    reason: Literal[TRANSFER_REASONS]  # type: ignore[valid-type]
    details: str = Field(max_length=300)


class _EndInput(BaseModel):
    outcome: Literal[CALL_OUTCOMES]  # type: ignore[valid-type]
    summary: str = Field(max_length=500)


_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "lookup_customer": _LookupInput,
    "check_service_area": _AreaInput,
    "send_booking_link": _Empty,
    "create_ticket": _TicketInput,
    "transfer_call": _TransferInput,
    "end_call": _EndInput,
}

_REQUIRED_BY_CALL_TYPE = {
    "new_lead": ("caller_name", "property_type", "address", "contract_type", "preferred_contact"),
    "emergency": ("address",),
    "complaint": ("address", "details"),
    "existing_customer": ("address", "details"),
}


# -- execution ----------------------------------------------------------------

@dataclass
class CallControl:
    """What the voice layer should do after Claude's reply is spoken."""

    transfer_to: str | None = None
    transfer_reason: str | None = None
    hang_up: bool = False
    outcome: str | None = None
    summary: str | None = None

    @property
    def ends_turn_taking(self) -> bool:
        return self.hang_up or self.transfer_to is not None


@dataclass
class ToolContext:
    db: Database
    business: BusinessProfile
    notifier: Notifier
    call_sid: str
    caller_phone: str
    control: CallControl = field(default_factory=CallControl)


class ToolError(Exception):
    """An error message that is safe to show the model."""


def _label(value: str | None) -> str:
    return value.replace("_", " ") if value else "?"


def format_owner_alert(t: dict[str, Any]) -> str:
    """Structured owner alert: name, address, service type, urgency, and how to reach them."""
    head = {
        "new_lead": "NEW LEAD",
        "existing_customer": "CUSTOMER REQUEST",
        "complaint": "COMPLAINT",
        "emergency": "PRIORITY ONE EMERGENCY",
    }[t["call_type"]]
    address = ", ".join(p for p in (t["address"], t["town"], t["zip_code"]) if p) or "?"
    service = " / ".join(_label(v) for v in (t["property_type"], t["service_type"], t["contract_type"]) if v) or "?"
    lines = [
        f"{head} #{t['id']}",
        f"Name: {t['caller_name'] or '?'}",
        f"Address: {address}",
        f"Service: {service}",
        f"Urgency: {'PRIORITY ONE' if t['priority'] == 'priority_one' else 'normal'}",
        f"Callback: {t['callback_number'] or t['caller_phone'] or '?'} (prefers {t['preferred_contact'] or '?'})",
    ]
    if t["details"]:
        lines.append(f"Details: {t['details']}")
    if t["access_notes"]:
        lines.append(f"Access: {t['access_notes']}")
    return "\n".join(lines)


def _lookup_customer(ctx: ToolContext, args: _LookupInput) -> dict[str, Any]:
    b = ctx.business.twilio_number
    matches = ctx.db.find_customers(b, phone=normalize_phone(ctx.caller_phone), address=args.address)
    if not matches:
        return {"found": False, "message": "No record for this number or address. Treat as a new caller or ask them to spell the address."}
    c = matches[0]
    caller_verified = normalize_phone(c["phone"]) == normalize_phone(ctx.caller_phone)
    record: dict[str, Any] = {
        "found": True,
        "matched_on": "phone" if caller_verified else "address",
        "customer_id": c["id"],
        "property_type": c["property_type"],
        "plan": c["plan"],
        "open_tickets": len(ctx.db.open_tickets(b, customer_id=c["id"], caller_phone=ctx.caller_phone)),
    }
    if caller_verified:
        # Only reveal account details to the phone number on the account.
        record.update(name=c["name"], address=c["address"], town=c["town"], notes=c["notes"])
    else:
        record["message"] = "Calling from a different number than the one on file: do not read back account details; ask for their name."
    return record


def _area(ctx: ToolContext, town: str | None, zip_code: str | None) -> dict[str, Any]:
    b = ctx.business
    if not b.has_structured_area:
        return {
            "in_service_area": None,
            "service_area": b.service_area,
            "message": "No town list on file. Decide from the service area description; "
            "if you can't tell, capture the lead and let the owner confirm.",
        }
    return check_service_area(town, zip_code, b.service_towns, b.service_zip_prefixes)


def _check_service_area(ctx: ToolContext, args: _AreaInput) -> dict[str, Any]:
    if not args.town and not args.zip_code:
        raise ToolError("Provide a town or a ZIP code.")
    return _area(ctx, args.town, args.zip_code)


def _send_booking_link(ctx: ToolContext, _: _Empty) -> dict[str, Any]:
    link = ctx.business.booking_link
    if not link:
        raise ToolError("This business has no booking link. Capture the lead instead.")
    sent = ctx.notifier.text(ctx.caller_phone, f"{ctx.business.name}: book your snow service here: {link}")
    if not sent:
        raise ToolError("Couldn't text this caller (no textable caller ID). Capture the lead instead.")
    return {"sent": True, "message": "Tell the caller the link is on its way by text."}


def _create_ticket(ctx: ToolContext, args: _TicketInput) -> dict[str, Any]:
    missing = [f for f in _REQUIRED_BY_CALL_TYPE[args.call_type] if not getattr(args, f)]
    if missing:
        raise ToolError(f"Missing {', '.join(missing)}. Ask the caller for it before logging this call.")
    if args.call_type == "new_lead" and ctx.business.has_structured_area:
        if not args.town and not args.zip_code:
            raise ToolError("Missing town or ZIP code. Ask for it so the service area can be checked.")
        if _area(ctx, args.town, args.zip_code)["in_service_area"] is False:
            raise ToolError("Outside the service area. Do not log a lead; tell the caller we don't cover that area.")

    callback = normalize_phone(args.callback_number) or args.callback_number or ctx.caller_phone
    customer_id = None
    if args.call_type in ("existing_customer", "complaint", "emergency"):
        matches = ctx.db.find_customers(ctx.business.twilio_number, phone=normalize_phone(ctx.caller_phone),
                                        address=args.address)
        customer_id = matches[0]["id"] if matches else None

    ticket = ctx.db.create_ticket(
        ctx.business.twilio_number,
        call_sid=ctx.call_sid,
        call_type=args.call_type,
        priority="priority_one" if args.call_type == "emergency" else args.priority,
        customer_id=customer_id,
        caller_phone=ctx.caller_phone,
        caller_name=args.caller_name,
        callback_number=callback,
        preferred_contact=args.preferred_contact,
        property_type=args.property_type,
        service_type=args.service_type,
        contract_type=args.contract_type,
        address=args.address,
        town=normalize_town(args.town).title() if args.town else None,
        zip_code=args.zip_code,
        access_notes=args.access_notes,
        details=args.details,
    )

    b = ctx.business
    owner_alerted = ctx.notifier.alert_owner(format_owner_alert(ticket))
    confirm_to = callback if normalize_phone(callback) else ctx.caller_phone
    caller_texted = ctx.notifier.text(
        confirm_to,
        f"Thanks for calling {b.name}. We've got your request (#{ticket['id']}) and the "
        f"owner will call you back within {b.callback_timeframe}.",
    )
    return {
        "ticket_id": ticket["id"],
        "priority": ticket["priority"],
        "owner_alerted": owner_alerted,
        "caller_sms_sent": caller_texted,
        "callback_timeframe": b.callback_timeframe,
    }


def _transfer_call(ctx: ToolContext, args: _TransferInput) -> dict[str, Any]:
    b = ctx.business
    target = b.emergency_phone if args.reason == "safety_hazard" else b.alert_phone
    ctx.notifier.alert_owner(
        f"{'PRIORITY ONE ' if args.reason == 'safety_hazard' else ''}TRANSFER ({_label(args.reason)}) "
        f"from {ctx.caller_phone}: {args.details}"
    )
    if not target:
        return {
            "transferred": False,
            "message": f"Nobody can take a live transfer right now. The owner has been texted. Tell the "
            f"caller the owner will call them back within {b.callback_timeframe}, then end the call.",
        }
    ctx.control.transfer_to = target
    ctx.control.transfer_reason = args.reason
    return {"transferred": True, "message": "Tell the caller you're connecting them now, in one short sentence."}


def _end_call(ctx: ToolContext, args: _EndInput) -> dict[str, Any]:
    ctx.control.hang_up = True
    ctx.control.outcome = args.outcome
    ctx.control.summary = args.summary
    return {"ok": True, "message": "Say a short goodbye; the call ends after you speak."}


_HANDLERS = {
    "lookup_customer": _lookup_customer,
    "check_service_area": _check_service_area,
    "send_booking_link": _send_booking_link,
    "create_ticket": _create_ticket,
    "transfer_call": _transfer_call,
    "end_call": _end_call,
}


def execute_tool(ctx: ToolContext, name: str, raw_input: Any) -> tuple[str, bool]:
    """Run one tool call. Returns (result_json, is_error)."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool {name!r}"}), True
    try:
        args = _INPUT_MODELS[name].model_validate(raw_input or {})
        return json.dumps(handler(ctx, args)), False
    except ValidationError as e:
        return json.dumps({"error": "Invalid input", "details": e.errors(include_url=False)}, default=str), True
    except ToolError as e:
        return json.dumps({"error": str(e)}), True
    except Exception:
        log.exception("tool %s failed", name)
        return json.dumps({"error": "Internal error. Take the caller's number and say the owner will call back."}), True
