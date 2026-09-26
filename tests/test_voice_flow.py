import anthropic
import httpx
from fastapi.testclient import TestClient

from conftest import message, text, tool_use

CALLER = "+17165551234"


def post(client, path, **form):
    form.setdefault("CallSid", "CA123")
    form.setdefault("From", CALLER)
    return client.post(path, data=form)


def test_incoming_call_greets_and_listens(app):
    client = TestClient(app)
    r = post(client, "/voice/incoming")
    assert r.status_code == 200
    body = r.text
    assert "<Gather" in body and 'action="/voice/respond"' in body
    assert "Thanks for calling" in body
    assert '<Redirect method="POST">/voice/no-input</Redirect>' in body


def test_full_booking_conversation(app, fake_client, db, sms):
    client = TestClient(app)
    post(client, "/voice/incoming")

    # Turn 1: caller asks for a plow; Claude looks up the account, then asks for the address.
    fake_client.messages.script += [
        message(tool_use("lookup_caller_account", {})),
        message(text("I can help with that. What's the street address?")),
    ]
    r = post(client, "/voice/respond", SpeechResult="I need my driveway plowed")
    assert "What's the street address?" in r.text and "<Gather" in r.text

    first_call = fake_client.messages.calls[0]
    assert first_call["model"] == "claude-opus-5"
    assert first_call["fallbacks"] == "default"
    assert first_call["output_config"] == {"effort": "low"}
    first_user = first_call["messages"][0]["content"]
    assert "<call_context>" in first_user[0]["text"] and "1234" in first_user[0]["text"]
    assert first_user[1]["text"] == "Caller: I need my driveway plowed"

    # Turn 2: caller confirms; Claude books it and reads back the number.
    fake_client.messages.script += [
        message(tool_use("create_service_request", {
            "customer_name": "Pat Kowalski", "service_address": "42 Elmwood Ave", "town": "Buffalo",
            "zip_code": None, "service_type": "driveway", "priority": "standard", "notes": None,
        })),
        message(text("You're all set. Request number one, about twenty-five minutes.")),
    ]
    r = post(client, "/voice/respond", SpeechResult="Yes that's right")
    assert "Request number one" in r.text
    assert db.get_request(1)["status"] == "queued"
    assert len(sms.sent) == 1

    # History persisted across webhooks and is append-only.
    history = fake_client.messages.calls[-1]["messages"]
    assert history[:4] == fake_client.messages.calls[1]["messages"] + [history[3]]

    # Turn 3: caller is done; Claude says goodbye and ends the call.
    fake_client.messages.script += [
        message(text("Thanks for calling, stay warm!"), tool_use("end_call", {"summary": "booked #1"})),
    ]
    r = post(client, "/voice/respond", SpeechResult="That's all, thanks")
    assert "stay warm" in r.text and "<Hangup />" in r.text
    assert db.get_or_create_session("CA123", CALLER).status == "completed"


def test_transfer_dials_dispatcher(app, fake_client):
    client = TestClient(app)
    fake_client.messages.script += [
        message(text("Let me connect you with a dispatcher."),
                tool_use("transfer_to_dispatcher", {"reason": "billing question"})),
    ]
    r = post(client, "/voice/respond", SpeechResult="I have a question about my bill")
    assert "<Dial" in r.text and "+17165550199" in r.text


def test_api_error_falls_back_to_dispatcher(app, fake_client):
    client = TestClient(app)
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_client.messages.script.append(anthropic.APIConnectionError(request=req))
    r = post(client, "/voice/respond", SpeechResult="hello")
    assert "having trouble" in r.text and "<Dial" in r.text


def test_refusal_is_not_spoken(app, fake_client, db):
    client = TestClient(app)
    fake_client.messages.script.append(message(stop_reason="refusal"))
    r = post(client, "/voice/respond", SpeechResult="hello")
    assert "having trouble" in r.text
    session = db.get_or_create_session("CA123", CALLER)
    assert session.messages[-1]["role"] == "assistant"


def test_no_input_reprompts_then_hangs_up(app, settings):
    client = TestClient(app)
    for _ in range(settings.max_no_input_prompts):
        r = post(client, "/voice/no-input")
        assert "didn't catch that" in r.text and "<Gather" in r.text
    r = post(client, "/voice/no-input")
    assert "<Hangup />" in r.text


def test_signature_validation_rejects_forged_requests(settings, db, fake_client, sms):
    from dataclasses import replace

    from snow_plow_dispatch_bot.agent import DispatchAgent
    from snow_plow_dispatch_bot.app import create_app

    s = replace(settings, validate_twilio_signature=True, twilio_auth_token="tok",
                public_base_url="https://plow.example.com")
    app = create_app(settings=s, db=db, agent=DispatchAgent(s, client=fake_client), sms=sms)
    client = TestClient(app)
    assert post(client, "/voice/incoming").status_code == 403

    from twilio.request_validator import RequestValidator

    form = {"CallSid": "CA9", "From": CALLER}
    sig = RequestValidator("tok").compute_signature("https://plow.example.com/voice/incoming", form)
    r = client.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200


def test_dispatch_api(app, db, sms):
    client = TestClient(app)
    assert client.get("/dispatch/queue").status_code == 401
    auth = {"Authorization": "Bearer secret"}

    customer = db.upsert_customer(CALLER, "Pat", "42 Elmwood Ave", "Buffalo", None)
    db.create_request(customer["id"], None, "42 Elmwood Ave", "Buffalo", None, "driveway", "standard", None)

    queue = client.get("/dispatch/queue", headers=auth).json()
    assert [r["id"] for r in queue] == [1] and queue[0]["eta_minutes"] == 25

    r = client.post("/dispatch/requests/1/status", json={"status": "en_route"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "en_route"
    assert "on the way" in sms.sent[-1][1]

    assert client.post("/dispatch/requests/1/status", json={"status": "bogus"}, headers=auth).status_code == 422
    assert client.post("/dispatch/requests/99/status", json={"status": "completed"}, headers=auth).status_code == 404
