import json

import pytest

from snow_plow_dispatch_bot.service_area import check_service_area
from snow_plow_dispatch_bot.tools import TOOL_DEFINITIONS, ToolContext, execute_tool

CALLER = "+17165551234"


@pytest.fixture
def ctx(db, settings, sms):
    return ToolContext(db=db, settings=settings, sms=sms, call_sid="CA1", caller_phone=CALLER)


def run(ctx, name, args):
    out, is_error = execute_tool(ctx, name, args)
    return json.loads(out), is_error


def create(ctx, **overrides):
    args = {
        "customer_name": "Pat Kowalski",
        "service_address": "42 Elmwood Ave",
        "town": "Buffalo",
        "zip_code": "14222",
        "service_type": "driveway",
        "priority": "standard",
        "notes": None,
    }
    args.update(overrides)
    return run(ctx, "create_service_request", args)


def test_tool_schemas_are_strict_and_fully_required():
    for tool in TOOL_DEFINITIONS:
        schema = tool["input_schema"]
        assert tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize(
    "town,zip_code,expected",
    [
        ("Cheektowaga", None, True),
        ("Town of Amherst", None, True),
        ("north tonawanda, NY", None, True),
        ("Rochester", "14604", False),
        ("Somewhere", "14221", True),
        ("Syracuse", None, False),
    ],
)
def test_service_area(town, zip_code, expected):
    assert check_service_area(town, zip_code)["in_service_area"] is expected


def test_lookup_unknown_caller(ctx):
    out, err = run(ctx, "lookup_caller_account", {})
    assert not err and out == {"existing_customer": False}


def test_create_request_saves_customer_and_texts_confirmation(ctx, sms):
    out, err = create(ctx)
    assert not err
    assert out["request_id"] == 1
    assert out["status"] == "queued"
    assert out["estimated_arrival"] == "about 25 minutes"
    assert out["sms_confirmation_sent"] is True
    assert sms.sent[0][0] == CALLER and "#1" in sms.sent[0][1]

    account, _ = run(ctx, "lookup_caller_account", {})
    assert account["existing_customer"] and account["name"] == "Pat Kowalski"
    assert account["recent_requests"][0]["request_id"] == 1


def test_create_request_outside_area_is_rejected(ctx):
    out, err = create(ctx, town="Rochester", zip_code="14604")
    assert err and "outside the service area" in out["error"]


def test_invalid_input_returns_error(ctx):
    out, err = create(ctx, service_type="roof")
    assert err and out["error"] == "Invalid input"


def test_urgent_requests_jump_the_queue(ctx, settings):
    for i in range(settings.truck_count):
        create(ctx, service_address=f"{i} Main St")
    urgent, _ = create(ctx, service_address="9 Oak St", priority="urgent")
    last, _ = create(ctx, service_address="10 Main St")
    assert urgent["estimated_arrival"] == "about 25 minutes"
    assert last["estimated_arrival"] == "about 50 minutes"


def test_status_and_cancel_are_scoped_to_caller(ctx, db, settings, sms):
    create(ctx)
    other = ToolContext(db=db, settings=settings, sms=sms, call_sid="CA2", caller_phone="+17165559999")

    out, err = run(other, "get_request_status", {"request_id": 1})
    assert err and "No request #1" in out["error"]
    out, err = run(other, "cancel_service_request", {"request_id": 1})
    assert err

    out, err = run(ctx, "get_request_status", {"request_id": None})
    assert not err and out["request_id"] == 1
    out, err = run(ctx, "cancel_service_request", {"request_id": 1})
    assert not err and out["status"] == "cancelled"
    out, err = run(ctx, "cancel_service_request", {"request_id": 1})
    assert err and "already cancelled" in out["error"]


def test_cannot_cancel_en_route(ctx, db):
    create(ctx)
    db.update_request_status(1, "en_route")
    out, err = run(ctx, "cancel_service_request", {"request_id": 1})
    assert err and "on the way" in out["error"]


def test_transfer_and_end_call_set_control(ctx):
    run(ctx, "transfer_to_dispatcher", {"reason": "billing"})
    assert ctx.control.transfer and ctx.control.transfer_reason == "billing"
    run(ctx, "end_call", {"summary": "done"})
    assert ctx.control.hang_up


def test_transfer_without_dispatcher_line(db, sms):
    from snow_plow_dispatch_bot.config import Settings

    ctx = ToolContext(db=db, settings=Settings(dispatcher_phone=""), sms=sms, call_sid="CA1",
                      caller_phone=CALLER)
    out, _ = run(ctx, "transfer_to_dispatcher", {"reason": "x"})
    assert out["transferred"] is False and not ctx.control.transfer
