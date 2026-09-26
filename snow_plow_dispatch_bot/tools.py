"""Tools Claude can call during a phone conversation.

Every tool is scoped to the caller's own phone number: the model never passes
a phone number, so a caller can only see or change their own requests.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .db import OPEN_STATUSES, PRIORITIES, SERVICE_TYPES, Database
from .service_area import check_service_area, normalize_town
from .sms import SmsSender

log = logging.getLogger(__name__)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


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
        "lookup_caller_account",
        "Look up the account and recent plow requests tied to the phone number the caller "
        "is calling from. Call this near the start of every call.",
        {},
    ),
    _tool(
        "check_service_area",
        "Check whether a town or ZIP code is inside the plowing service area "
        "(Erie and Niagara counties, NY). Call before creating a request for a new address.",
        {
            "town": {"type": "string", "description": "Town, city, or village name, e.g. 'Cheektowaga'."},
            "zip_code": _nullable({"type": "string", "description": "5-digit ZIP code if the caller gave one."}),
        },
    ),
    _tool(
        "create_service_request",
        "Create a plow or salting request and add it to the dispatch queue. Only call after "
        "reading the address and service back to the caller and hearing them confirm it.",
        {
            "customer_name": {"type": "string", "description": "Caller's full name."},
            "service_address": {"type": "string", "description": "Street address to plow, e.g. '123 Main St'."},
            "town": {"type": "string"},
            "zip_code": _nullable({"type": "string"}),
            "service_type": {"type": "string", "enum": list(SERVICE_TYPES)},
            "priority": {
                "type": "string",
                "enum": list(PRIORITIES),
                "description": "'urgent' only when someone must get out soon for work shifts, "
                "medical appointments, or a business opening; otherwise 'standard'.",
            },
            "notes": _nullable({
                "type": "string",
                "description": "Anything the driver needs: gate codes, parked cars, where to pile snow.",
            }),
        },
    ),
    _tool(
        "get_request_status",
        "Get the status and estimated arrival for one of the caller's plow requests. Pass "
        "null for request_id to get their most recent open request.",
        {"request_id": _nullable({"type": "integer"})},
    ),
    _tool(
        "cancel_service_request",
        "Cancel one of the caller's open plow requests after they confirm they want to cancel it.",
        {"request_id": {"type": "integer"}},
    ),
    _tool(
        "transfer_to_dispatcher",
        "Transfer the call to a human dispatcher. Use when the caller asks for a person, has "
        "a billing or damage complaint, or needs something these tools cannot do.",
        {"reason": {"type": "string"}},
    ),
    _tool(
        "end_call",
        "Hang up after saying goodbye, once the caller has nothing else they need.",
        {"summary": {"type": "string", "description": "One-sentence summary of the call for the log."}},
    ),
]


# -- input validation ---------------------------------------------------------
# Strict tool use already constrains the schema; these models also guard
# length/format, and keep the executor safe if strict mode is turned off.

class _Empty(BaseModel):
    model_config = {"extra": "forbid"}


class _ServiceAreaInput(BaseModel):
    town: str = Field(max_length=80)
    zip_code: str | None = Field(default=None, max_length=10)


class _CreateInput(BaseModel):
    customer_name: str = Field(min_length=1, max_length=120)
    service_address: str = Field(min_length=3, max_length=200)
    town: str = Field(min_length=1, max_length=80)
    zip_code: str | None = Field(default=None, max_length=10)
    service_type: Literal["driveway", "parking_lot", "sidewalk", "salting"]
    priority: Literal["standard", "urgent"]
    notes: str | None = Field(default=None, max_length=500)


class _StatusInput(BaseModel):
    request_id: int | None = None


class _CancelInput(BaseModel):
    request_id: int


class _TransferInput(BaseModel):
    reason: str = Field(max_length=300)


class _EndInput(BaseModel):
    summary: str = Field(max_length=500)


_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "lookup_caller_account": _Empty,
    "check_service_area": _ServiceAreaInput,
    "create_service_request": _CreateInput,
    "get_request_status": _StatusInput,
    "cancel_service_request": _CancelInput,
    "transfer_to_dispatcher": _TransferInput,
    "end_call": _EndInput,
}


# -- execution ----------------------------------------------------------------

@dataclass
class CallControl:
    """Side effects a tool asks the voice layer to perform after Claude's reply is spoken."""

    transfer: bool = False
    transfer_reason: str | None = None
    hang_up: bool = False
    summary: str | None = None


@dataclass
class ToolContext:
    db: Database
    settings: Settings
    sms: SmsSender
    call_sid: str
    caller_phone: str
    control: CallControl = field(default_factory=CallControl)


class ToolError(Exception):
    """An error message that is safe to show the model."""


def estimate_eta_minutes(db: Database, settings: Settings, request_id: int) -> int | None:
    """Rough ETA: jobs ahead of this one, spread across the trucks on the road."""
    queue = db.open_queue()
    position = next((i for i, r in enumerate(queue) if r["id"] == request_id), None)
    if position is None:
        return None
    trucks = max(settings.truck_count, 1)
    rounds = math.floor(position / trucks) + 1
    return rounds * settings.minutes_per_job


def _format_eta(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    if minutes < 60:
        return f"about {minutes} minutes"
    hours = round(minutes / 60 * 2) / 2
    return f"about {hours:g} hours"


def _public_request(db: Database, settings: Settings, r: dict[str, Any]) -> dict[str, Any]:
    out = {
        "request_id": r["id"],
        "service_address": r["service_address"],
        "town": r["town"],
        "service_type": r["service_type"],
        "priority": r["priority"],
        "status": r["status"],
        "created_at": r["created_at"],
    }
    if r["status"] in OPEN_STATUSES:
        out["estimated_arrival"] = _format_eta(estimate_eta_minutes(db, settings, r["id"]))
    return out


def _owned_request(ctx: ToolContext, request_id: int) -> dict[str, Any]:
    r = ctx.db.get_request(request_id)
    if not r or r["customer_phone"] != ctx.caller_phone:
        raise ToolError(f"No request #{request_id} found for this caller's phone number.")
    return r


def _lookup_caller_account(ctx: ToolContext, _: _Empty) -> dict[str, Any]:
    customer = ctx.db.get_customer_by_phone(ctx.caller_phone)
    if not customer:
        return {"existing_customer": False}
    requests = ctx.db.list_requests_for_phone(ctx.caller_phone)
    return {
        "existing_customer": True,
        "name": customer["name"],
        "default_address": customer["default_address"],
        "town": customer["town"],
        "recent_requests": [_public_request(ctx.db, ctx.settings, r) for r in requests[:3]],
    }


def _check_service_area(ctx: ToolContext, args: _ServiceAreaInput) -> dict[str, Any]:
    return check_service_area(args.town, args.zip_code)


def _create_service_request(ctx: ToolContext, args: _CreateInput) -> dict[str, Any]:
    area = check_service_area(args.town, args.zip_code)
    if not area["in_service_area"]:
        raise ToolError(
            f"{args.town} is outside the service area (Erie and Niagara counties). "
            "Do not create the request; offer to transfer to a dispatcher instead."
        )
    town = normalize_town(args.town).title()
    customer = ctx.db.upsert_customer(
        ctx.caller_phone, args.customer_name.strip(), args.service_address.strip(), town, args.zip_code
    )
    request = ctx.db.create_request(
        customer_id=customer["id"],
        call_sid=ctx.call_sid,
        service_address=args.service_address.strip(),
        town=town,
        zip_code=args.zip_code,
        service_type=args.service_type,
        priority=args.priority,
        notes=args.notes,
    )
    eta = _format_eta(estimate_eta_minutes(ctx.db, ctx.settings, request["id"]))
    sms_sent = ctx.sms.send(
        ctx.caller_phone,
        f"{ctx.settings.company_name}: request #{request['id']} received for "
        f"{request['service_address']}, {town} ({args.service_type.replace('_', ' ')}). "
        f"Estimated arrival {eta or 'to be confirmed'}. Reply or call back to change it.",
    )
    return {
        "request_id": request["id"],
        "status": request["status"],
        "estimated_arrival": eta,
        "sms_confirmation_sent": sms_sent,
    }


def _get_request_status(ctx: ToolContext, args: _StatusInput) -> dict[str, Any]:
    if args.request_id is not None:
        return _public_request(ctx.db, ctx.settings, _owned_request(ctx, args.request_id))
    open_requests = ctx.db.list_requests_for_phone(ctx.caller_phone, open_only=True)
    if not open_requests:
        return {"open_request": None, "message": "This caller has no open requests."}
    return _public_request(ctx.db, ctx.settings, open_requests[0])


def _cancel_service_request(ctx: ToolContext, args: _CancelInput) -> dict[str, Any]:
    r = _owned_request(ctx, args.request_id)
    if r["status"] not in OPEN_STATUSES:
        raise ToolError(f"Request #{r['id']} is already {r['status']} and cannot be cancelled.")
    if r["status"] == "en_route":
        raise ToolError(
            f"A truck is already on the way to request #{r['id']}; transfer to a dispatcher to cancel."
        )
    updated = ctx.db.update_request_status(r["id"], "cancelled")
    return {"request_id": updated["id"], "status": updated["status"]}


def _transfer_to_dispatcher(ctx: ToolContext, args: _TransferInput) -> dict[str, Any]:
    if not ctx.settings.dispatcher_phone:
        return {
            "transferred": False,
            "message": "No dispatcher line is staffed right now. Tell the caller a dispatcher "
            "will call them back at this number, then end the call.",
        }
    ctx.control.transfer = True
    ctx.control.transfer_reason = args.reason
    return {"transferred": True, "message": "Tell the caller you are connecting them now."}


def _end_call(ctx: ToolContext, args: _EndInput) -> dict[str, Any]:
    ctx.control.hang_up = True
    ctx.control.summary = args.summary
    return {"ok": True, "message": "Say a short goodbye; the call will end after you speak."}


_HANDLERS = {
    "lookup_caller_account": _lookup_caller_account,
    "check_service_area": _check_service_area,
    "create_service_request": _create_service_request,
    "get_request_status": _get_request_status,
    "cancel_service_request": _cancel_service_request,
    "transfer_to_dispatcher": _transfer_to_dispatcher,
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
        return json.dumps({"error": "Internal error; apologize and offer a dispatcher."}), True
