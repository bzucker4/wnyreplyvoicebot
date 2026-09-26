import json

import pytest
from conftest import ON_CALL, OWNER

from snow_plow_dispatch_bot.config import Settings
from snow_plow_dispatch_bot.service_area import check_service_area
from snow_plow_dispatch_bot.sms import Notifier, normalize_phone
from snow_plow_dispatch_bot.tools import TOOL_DEFINITIONS, ToolContext, execute_tool, format_owner_alert

CALLER = "+17165551234"


@pytest.fixture
def ctx(db, settings, notifier):
    return ToolContext(db=db, settings=settings, notifier=notifier, call_sid="CA1", caller_phone=CALLER)


def run(ctx, name, args):
    out, is_error = execute_tool(ctx, name, args)
    return json.loads(out), is_error


def ticket_args(**overrides):
    args = {
        "call_type": "new_lead",
        "priority": "normal",
        "caller_name": "Pat Kowalski",
        "callback_number": None,
        "preferred_contact": "text",
        "property_type": "residential",
        "service_type": "driveway_plowing",
        "contract_type": "seasonal",
        "address": "42 Elmwood Ave",
        "town": "Buffalo",
        "zip_code": "14222",
        "access_notes": "Narrow entrance, car on the left",
        "details": None,
    }
    args.update(overrides)
    return args


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
        (None, "14221", True),
        ("Syracuse", None, False),
    ],
)
def test_service_area(town, zip_code, expected):
    assert check_service_area(town, zip_code)["in_service_area"] is expected


def test_service_area_is_configurable(db, sms):
    s = Settings(service_towns=("Rochester",), service_zip_prefixes=("146",))
    ctx = ToolContext(db=db, settings=s, notifier=Notifier(s, sms), call_sid="CA1", caller_phone=CALLER)
    assert run(ctx, "check_service_area", {"town": "rochester", "zip_code": None})[0]["in_service_area"]
    assert not run(ctx, "check_service_area", {"town": "Buffalo", "zip_code": "14222"})[0]["in_service_area"]


def test_normalize_phone():
    assert normalize_phone("(716) 555-1234") == "+17165551234"
    assert normalize_phone("anonymous") is None
    assert normalize_phone(None) is None


def test_new_lead_texts_caller_and_alerts_owner(ctx, sms, db):
    out, err = run(ctx, "create_ticket", ticket_args())
    assert not err
    assert out["ticket_id"] == 1 and out["callback_timeframe"] == "30 minutes"
    assert out["owner_alerted"] and out["caller_sms_sent"]

    (alert,) = sms.to(OWNER)
    assert alert.startswith("NEW LEAD #1")
    for expected in ("Pat Kowalski", "42 Elmwood Ave, Buffalo, 14222", "residential / driveway plowing / seasonal",
                     "Urgency: normal", "prefers text", "Narrow entrance"):
        assert expected in alert
    (confirmation,) = sms.to(CALLER)
    assert "within 30 minutes" in confirmation
    assert db.get_ticket(1)["callback_number"] == CALLER


def test_new_lead_requires_qualifying_details(ctx, sms):
    out, err = run(ctx, "create_ticket", ticket_args(contract_type=None, caller_name=None))
    assert err and "caller_name" in out["error"] and "contract_type" in out["error"]
    assert sms.sent == []


def test_new_lead_outside_area_is_not_logged(ctx, db):
    out, err = run(ctx, "create_ticket", ticket_args(town="Rochester", zip_code="14604"))
    assert err and "don't cover that area" in out["error"]
    assert db.open_tickets() == []


def test_callback_number_override(ctx, sms, db):
    run(ctx, "create_ticket", ticket_args(callback_number="716-555-7777"))
    assert db.get_ticket(1)["callback_number"] == "+17165557777"
    assert sms.to("+17165557777")


def test_emergency_is_priority_one_and_sorted_first(ctx, db, sms):
    run(ctx, "create_ticket", ticket_args())
    out, err = run(ctx, "create_ticket", ticket_args(
        call_type="emergency", priority="normal", caller_name=None, property_type="commercial",
        contract_type=None, preferred_contact=None, details="Icy loading ramp, trucks can't get in"))
    assert not err and out["priority"] == "priority_one"
    assert [t["id"] for t in db.open_tickets()] == [2, 1]
    assert sms.to(OWNER)[-1].startswith("PRIORITY ONE EMERGENCY #2")


def test_complaint_links_existing_customer(ctx, db, sms):
    db.add_customer("Pat Kowalski", "42 Elmwood Avenue", phone=CALLER, plan="seasonal")
    out, err = run(ctx, "create_ticket", ticket_args(
        call_type="complaint", details="Plow skipped my driveway this morning"))
    assert not err
    assert db.get_ticket(out["ticket_id"])["customer_id"] == 1
    assert sms.to(OWNER)[-1].startswith("COMPLAINT")


def test_lookup_customer_hides_details_from_other_numbers(ctx, db, settings, notifier):
    db.add_customer("Pat Kowalski", "42 Elmwood Avenue", phone=CALLER, town="Buffalo", plan="seasonal")

    out, _ = run(ctx, "lookup_customer", {"address": None})
    assert out["found"] and out["matched_on"] == "phone" and out["name"] == "Pat Kowalski"

    other = ToolContext(db=db, settings=settings, notifier=notifier, call_sid="CA2", caller_phone="+17165559999")
    out, _ = run(other, "lookup_customer", {"address": "42 elmwood ave."})
    assert out["found"] and out["matched_on"] == "address"
    assert "name" not in out and "address" not in out

    out, _ = run(other, "lookup_customer", {"address": "7 Nowhere St"})
    assert out["found"] is False


def test_safety_transfer_goes_to_on_call(ctx, sms):
    out, _ = run(ctx, "transfer_call", {"reason": "safety_hazard", "details": "icy ramp"})
    assert out["transferred"] and ctx.control.transfer_to == ON_CALL
    assert sms.to(OWNER)[0].startswith("PRIORITY ONE TRANSFER")


def test_other_transfers_go_to_owner(ctx):
    run(ctx, "transfer_call", {"reason": "owner_requested", "details": "asked for Mike"})
    assert ctx.control.transfer_to == OWNER


def test_transfer_without_numbers_promises_callback(db, sms):
    s = Settings()
    ctx = ToolContext(db=db, settings=s, notifier=Notifier(s, sms), call_sid="CA1", caller_phone=CALLER)
    out, _ = run(ctx, "transfer_call", {"reason": "large_commercial", "details": "mall"})
    assert out["transferred"] is False and ctx.control.transfer_to is None


def test_end_call(ctx):
    run(ctx, "end_call", {"outcome": "wrong_number", "summary": "wrong number"})
    assert ctx.control.hang_up and ctx.control.outcome == "wrong_number"


def test_invalid_input_returns_error(ctx):
    out, err = run(ctx, "create_ticket", ticket_args(service_type="roof_raking"))
    assert err and out["error"] == "Invalid input"


def test_owner_alert_handles_sparse_ticket():
    alert = format_owner_alert({
        "id": 3, "call_type": "emergency", "priority": "priority_one", "caller_name": None,
        "address": "1 Main St", "town": None, "zip_code": None, "property_type": None,
        "service_type": None, "contract_type": None, "callback_number": None,
        "caller_phone": CALLER, "preferred_contact": None, "details": "Ice", "access_notes": None,
    })
    assert "Name: ?" in alert and f"Callback: {CALLER}" in alert
