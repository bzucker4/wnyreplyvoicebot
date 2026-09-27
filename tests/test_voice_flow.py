import anthropic
import httpx
from conftest import BIZ, BOOKING_LINK, ON_CALL, OTHER_BIZ, OWNER, message, text, tool_use
from fastapi.testclient import TestClient

from snow_plow_dispatch_bot.agent import PACK_PRICE_BANDS, UNCLEAR_LINE, build_system_prompt
from snow_plow_dispatch_bot.business import BusinessProfile
from snow_plow_dispatch_bot.storage import businesses

CALLER = "+17165551234"


def post(client, path, **form):
    form.setdefault("CallSid", "CA123")
    form.setdefault("From", CALLER)
    form.setdefault("To", BIZ)
    return client.post(path, data=form)


def session(db):
    return db.get_or_create_session("CA123", BIZ, CALLER)


def test_system_prompt_is_filled_per_business(business, db, settings):
    prompt = build_system_prompt(business)
    assert "dispatch assistant for WNY Test Plowing" in prompt
    assert "serving Buffalo and its suburbs" in prompt
    assert "within 30 minutes to confirm and quote" in prompt
    assert "Cheektowaga" in prompt and "140, 141, 142" in prompt
    assert "Residential driveway, seasonal: $450-750" in prompt and PACK_PRICE_BANDS not in prompt
    assert "send_booking_link" in prompt and "Mike Kowalski" in prompt
    assert UNCLEAR_LINE in prompt
    assert "{" not in prompt

    other = build_system_prompt(BusinessProfile.from_row(db.get_business(OTHER_BIZ), settings))
    assert "Rochester Snow Co" in other and PACK_PRICE_BANDS in other
    assert "send_booking_link" not in other and "Towns:" not in other
    assert f"within {settings.default_callback_timeframe}" in other


def test_unknown_or_inactive_number_is_not_taking_calls(app, db):
    client = TestClient(app)
    r = post(client, "/voice/incoming", To="+19995550000")
    assert "isn't currently taking calls" in r.text and "<Hangup />" in r.text

    with db.engine.begin() as c:
        c.execute(businesses.update().where(businesses.c.twilio_number == BIZ).values(active=False))
    r = post(client, "/voice/incoming")
    assert "isn't currently taking calls" in r.text


def test_incoming_call_greets_and_listens_with_5s_silence_timeout(app):
    client = TestClient(app)
    r = post(client, "/voice/incoming")
    assert r.status_code == 200
    assert "<Gather" in r.text and 'action="/voice/respond"' in r.text and 'timeout="5"' in r.text
    assert "Thanks for calling WNY Test Plowing" in r.text
    assert '<Redirect method="POST">/voice/no-input</Redirect>' in r.text


def test_new_lead_conversation(app, fake_client, db, sms):
    client = TestClient(app)
    post(client, "/voice/incoming")

    fake_client.messages.script += [
        message(text("Sure. Is this for a residential driveway or a commercial property?")),
    ]
    r = post(client, "/voice/respond", SpeechResult="Hi, I need a quote for plowing")
    assert "residential driveway or a commercial property" in r.text and "<Gather" in r.text

    first_call = fake_client.messages.calls[0]
    assert first_call["model"] == "claude-opus-5"
    assert first_call["fallbacks"] == "default"
    assert first_call["output_config"] == {"effort": "low"}
    context = first_call["messages"][0]["content"][0]["text"]
    assert "<call_context>" in context and "1234" in context

    fake_client.messages.script += [
        message(tool_use("create_ticket", {
            "call_type": "new_lead", "priority": "normal", "caller_name": "Pat Kowalski",
            "callback_number": None, "preferred_contact": "call", "property_type": "residential",
            "service_type": "driveway_plowing", "contract_type": "seasonal", "address": "42 Elmwood Ave",
            "town": "Buffalo", "zip_code": None, "access_notes": None, "details": None,
        })),
        message(text("I've got your information. The owner will call you back within 30 minutes to confirm and quote.")),
    ]
    r = post(client, "/voice/respond", SpeechResult="Nothing in the way, calling is best")
    assert "within 30 minutes" in r.text
    assert db.get_ticket(1)["call_type"] == "new_lead"
    assert sms.to(OWNER) and sms.to(CALLER)

    # History persists across webhooks and is append-only.
    history = fake_client.messages.calls[-1]["messages"]
    assert history[:3] == fake_client.messages.calls[1]["messages"]

    fake_client.messages.script += [
        message(text("Thanks for calling, stay warm."), tool_use("end_call", {"outcome": "lead_captured", "summary": "lead"})),
    ]
    r = post(client, "/voice/respond", SpeechResult="That's it")
    assert "stay warm" in r.text and "<Hangup />" in r.text

    # Normal hang-up after end_call: no dropped-call text.
    sent_before = len(sms.sent)
    post(client, "/voice/status", CallStatus="completed")
    assert len(sms.sent) == sent_before


def test_emergency_transfers_to_on_call(app, fake_client):
    client = TestClient(app)
    fake_client.messages.script += [
        message(text("Okay, connecting you to our on-call driver now."),
                tool_use("transfer_call", {"reason": "safety_hazard", "details": "icy ramp"})),
    ]
    r = post(client, "/voice/respond", SpeechResult="Our loading ramp is a sheet of ice")
    assert "<Dial" in r.text and ON_CALL in r.text and 'action="/voice/dial-result"' in r.text


def test_unanswered_transfer_promises_callback(app, sms):
    client = TestClient(app)
    r = post(client, "/voice/dial-result", DialCallStatus="no-answer")
    assert "call you back within 30 minutes" in r.text and "<Hangup />" in r.text
    assert sms.to(OWNER)[0].startswith("MISSED TRANSFER")


def test_api_error_hands_off_to_owner(app, fake_client, sms):
    client = TestClient(app)
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_client.messages.script.append(anthropic.APIConnectionError(request=req))
    r = post(client, "/voice/respond", SpeechResult="hello")
    assert "having trouble" in r.text and "<Dial" in r.text and OWNER in r.text
    assert sms.to(OWNER)[0].startswith("CALLBACK NEEDED")


def test_refusal_is_not_spoken(app, fake_client, db):
    client = TestClient(app)
    fake_client.messages.script.append(message(stop_reason="refusal"))
    r = post(client, "/voice/respond", SpeechResult="hello")
    assert "having trouble" in r.text
    assert session(db).messages[-1]["role"] == "assistant"


def test_silence_asks_are_you_still_there_then_hangs_up_and_texts(app, settings, sms):
    client = TestClient(app)
    post(client, "/voice/incoming")
    for _ in range(settings.max_no_input_prompts):
        r = post(client, "/voice/no-input")
        assert "Are you still there?" in r.text and "<Gather" in r.text
    r = post(client, "/voice/no-input")
    assert "<Hangup />" in r.text

    post(client, "/voice/status", CallStatus="completed")
    (drop_sms,) = sms.to(CALLER)
    assert drop_sms == "Thanks for calling WNY Test Plowing. We got your number and will call you back shortly."
    assert sms.to(OWNER)[0].startswith("DROPPED CALL")


def test_dropped_call_after_ticket_does_not_double_text(app, fake_client, db, sms):
    client = TestClient(app)
    fake_client.messages.script += [
        message(tool_use("create_ticket", {
            "call_type": "complaint", "priority": "normal", "caller_name": None, "callback_number": None,
            "preferred_contact": None, "property_type": None, "service_type": None, "contract_type": None,
            "address": "42 Elmwood Ave", "town": None, "zip_code": None, "access_notes": None,
            "details": "Missed pass this morning",
        })),
        message(text("I'm sorry about that. I've passed it to the owner.")),
    ]
    post(client, "/voice/respond", SpeechResult="You missed my driveway at 42 Elmwood")
    texts_before = len(sms.to(CALLER))
    post(client, "/voice/status", CallStatus="completed")
    assert len(sms.to(CALLER)) == texts_before
    assert session(db).status == "dropped"


def test_signature_validation_rejects_forged_requests(settings, db, fake_client, sms):
    from dataclasses import replace

    from twilio.request_validator import RequestValidator

    from snow_plow_dispatch_bot.agent import DispatchAgent
    from snow_plow_dispatch_bot.app import create_app

    s = replace(settings, validate_twilio_signature=True, twilio_auth_token="tok",
                public_base_url="https://plow.example.com")
    app = create_app(settings=s, db=db, agent=DispatchAgent(s, client=fake_client), sms=sms)
    client = TestClient(app)
    assert post(client, "/voice/incoming").status_code == 403

    form = {"CallSid": "CA9", "From": CALLER, "To": BIZ}
    sig = RequestValidator("tok").compute_signature("https://plow.example.com/voice/incoming", form)
    assert client.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200


def test_dispatch_api(app, db):
    client = TestClient(app)
    assert client.get("/dispatch/tickets").status_code == 401
    auth = {"Authorization": "Bearer secret"}

    r = client.post("/dispatch/customers", headers=auth, json={
        "business_number": BIZ, "name": "Pat Kowalski", "address": "42 Elmwood Ave", "phone": "(716) 555-1234",
        "property_type": "residential", "plan": "seasonal"})
    assert r.status_code == 201 and r.json()["phone"] == CALLER
    assert len(client.get("/dispatch/customers", headers=auth).json()) == 1

    bad = client.post("/dispatch/customers", headers=auth, json={
        "business_number": "+19995550000", "name": "X", "address": "1 Main St"})
    assert bad.status_code == 404

    db.create_ticket(BIZ, call_type="new_lead", priority="normal", caller_phone=CALLER)
    db.create_ticket(OTHER_BIZ, call_type="new_lead", priority="normal", caller_phone=CALLER)
    assert [t["id"] for t in client.get("/dispatch/tickets", headers=auth).json()] == [1, 2]
    assert [t["id"] for t in client.get(f"/dispatch/tickets?business={BIZ}", headers=auth).json()] == [1]

    r = client.post("/dispatch/tickets/1/status", json={"status": "closed"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "closed"
    assert [t["id"] for t in client.get("/dispatch/tickets", headers=auth).json()] == [2]
    assert client.post("/dispatch/tickets/1/status", json={"status": "bogus"}, headers=auth).status_code == 422
    assert client.post("/dispatch/tickets/99/status", json={"status": "closed"}, headers=auth).status_code == 404
