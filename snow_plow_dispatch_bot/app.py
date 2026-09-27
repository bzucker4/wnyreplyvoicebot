"""FastAPI app: Twilio voice webhooks (one service, many businesses) plus an admin API."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from .agent import DispatchAgent, TurnResult, greeting
from .business import BusinessProfile
from .config import Settings, load_settings
from .sms import Notifier, SmsSender, build_sms_sender, normalize_phone
from .storage import (
    PROPERTY_TYPES,
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    SESSION_DROPPED,
    SESSION_TRANSFERRED,
    TICKET_STATUSES,
    CallSession,
    Database,
)
from .tools import ToolContext

log = logging.getLogger(__name__)

STILL_THERE = "Are you still there?"
NOT_TAKING_CALLS = "Sorry, this number isn't currently taking calls. Please try again later."
ENDED_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
SPEECH_HINT_WORDS = "driveway,parking lot,sidewalk,de-icing,seasonal,one-time,commercial,residential"


def _business_param(raw: str | None) -> str | None:
    """?business=+17165550100 arrives as ' 17165550100' unless the '+' is URL-encoded."""
    return normalize_phone(raw) or raw if raw else None


class TicketStatusUpdate(BaseModel):
    status: str


class CustomerIn(BaseModel):
    business_number: str = Field(description="The business's Twilio number (businesses.twilio_number)")
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=3, max_length=200)
    phone: str | None = None
    town: str | None = None
    zip_code: str | None = None
    property_type: str | None = None
    plan: str | None = Field(default=None, description="e.g. seasonal or per_visit")
    notes: str | None = None


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    agent: DispatchAgent | None = None,
    sms: SmsSender | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if db is None:
        db = Database.from_url(settings.database_url)
        if settings.auto_create_schema:
            db.create_schema()
    sender = sms or build_sms_sender(settings)
    agent = agent or DispatchAgent(settings)
    validator = RequestValidator(settings.twilio_auth_token) if settings.twilio_auth_token else None

    app = FastAPI(title="snow_plow_dispatch_bot")

    # -- helpers --------------------------------------------------------------

    async def twilio_form(request: Request) -> dict[str, str]:
        form = {k: str(v) for k, v in (await request.form()).items()}
        if settings.validate_twilio_signature:
            if validator is None:
                raise HTTPException(500, "TWILIO_AUTH_TOKEN is required when signature validation is on")
            url = str(request.url)
            if settings.public_base_url:
                url = settings.public_base_url + request.url.path
                if request.url.query:
                    url += "?" + request.url.query
            if not validator.validate(url, form, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(403, "invalid Twilio signature")
        return form

    def load_business(number: str) -> BusinessProfile | None:
        row = db.get_business(number)
        return BusinessProfile.from_row(row, settings) if row else None

    def notifier_for(business: BusinessProfile) -> Notifier:
        return Notifier(sender, business.twilio_number, business.alert_phone)

    def session_for(form: dict[str, str]) -> CallSession:
        return db.get_or_create_session(form["CallSid"], form.get("To", ""), form.get("From", ""))

    def twiml(resp: VoiceResponse) -> Response:
        return Response(content=str(resp), media_type="application/xml")

    def say(resp: VoiceResponse | Gather, text: str) -> None:
        resp.say(text, voice=settings.tts_voice)

    def not_taking_calls() -> Response:
        resp = VoiceResponse()
        say(resp, NOT_TAKING_CALLS)
        resp.hangup()
        return twiml(resp)

    def listen(resp: VoiceResponse, prompt: str | None, business: BusinessProfile) -> None:
        # Biases speech recognition toward the business's towns and service words.
        hints = ",".join([t.title() for t in business.service_towns] + [SPEECH_HINT_WORDS])
        gather = Gather(
            input="speech",
            action="/voice/respond",
            method="POST",
            timeout=settings.silence_timeout_seconds,
            speech_timeout="auto",
            speech_model="phone_call",
            language="en-US",
            hints=hints,
            action_on_empty_result=False,
        )
        if prompt:
            say(gather, prompt)
        resp.append(gather)
        # Reached only after the caller has been silent for the gather timeout.
        resp.redirect("/voice/no-input", method="POST")

    def render_turn(turn: TurnResult, business: BusinessProfile) -> VoiceResponse:
        resp = VoiceResponse()
        if turn.control.transfer_to:
            say(resp, turn.speech)
            resp.dial(turn.control.transfer_to, action="/voice/dial-result", method="POST",
                      timeout=25, caller_id=business.twilio_number)
        elif turn.control.hang_up:
            say(resp, turn.speech)
            resp.hangup()
        else:
            listen(resp, turn.speech, business)
        return resp

    # -- voice webhooks -------------------------------------------------------

    @app.post("/voice/incoming")
    def voice_incoming(form: dict[str, str] = Depends(twilio_form)) -> Response:
        business = load_business(form.get("To", ""))
        if not business or not business.accepting_calls:
            return not_taking_calls()
        session_for(form)
        resp = VoiceResponse()
        listen(resp, greeting(business), business)
        return twiml(resp)

    @app.post("/voice/respond")
    def voice_respond(form: dict[str, str] = Depends(twilio_form)) -> Response:
        business = load_business(form.get("To", ""))
        if not business or not business.accepting_calls:
            return not_taking_calls()
        session = session_for(form)
        speech = form.get("SpeechResult", "").strip()
        if not speech:
            return voice_no_input(form)
        session.no_input_count = 0
        ctx = ToolContext(db=db, business=business, notifier=notifier_for(business),
                          call_sid=session.call_sid, caller_phone=session.caller_phone)
        turn = agent.respond(session, speech, ctx)
        if turn.control.transfer_to:
            session.status = SESSION_TRANSFERRED
        elif turn.control.hang_up:
            session.status = SESSION_COMPLETED
        db.save_session(session)
        return twiml(render_turn(turn, business))

    @app.post("/voice/no-input")
    def voice_no_input(form: dict[str, str] = Depends(twilio_form)) -> Response:
        business = load_business(form.get("To", ""))
        if not business or not business.accepting_calls:
            return not_taking_calls()
        session = session_for(form)
        session.no_input_count += 1
        db.save_session(session)
        resp = VoiceResponse()
        if session.no_input_count > settings.max_no_input_prompts:
            # Left "active" so the status callback treats it as a dropped call and texts them.
            say(resp, "I'll let you go. We'll text you so you can reach us.")
            resp.hangup()
        else:
            listen(resp, STILL_THERE, business)
        return twiml(resp)

    @app.post("/voice/dial-result")
    def voice_dial_result(form: dict[str, str] = Depends(twilio_form)) -> Response:
        """After a transfer: if nobody picked up, promise a callback instead of dead air."""
        resp = VoiceResponse()
        business = load_business(form.get("To", ""))
        if business and form.get("DialCallStatus") not in {"completed", "answered"}:
            notifier_for(business).alert_owner(
                f"MISSED TRANSFER from {form.get('From', '?')}. Please call them back.")
            say(resp, f"Sorry, nobody could pick up. The owner will call you back within {business.callback_timeframe}.")
        resp.hangup()
        return twiml(resp)

    @app.post("/voice/status")
    def voice_status(form: dict[str, str] = Depends(twilio_form)) -> Response:
        """Call status callback. A call that ends while still active was dropped: text the caller."""
        if form.get("CallStatus") not in ENDED_CALL_STATUSES:
            return Response(status_code=204)
        business = load_business(form.get("To", ""))
        if not business:
            return Response(status_code=204)
        session = session_for(form)
        if session.status != SESSION_ACTIVE:
            return Response(status_code=204)
        session.status = SESSION_DROPPED
        db.save_session(session)
        if db.tickets_for_call(session.call_sid):
            return Response(status_code=204)  # caller already got a confirmation text
        notifier = notifier_for(business)
        notifier.text(
            session.caller_phone,
            f"Thanks for calling {business.name}. We got your number and will call you back shortly.",
        )
        notifier.alert_owner(f"DROPPED CALL from {session.caller_phone or 'unknown number'}. No details captured; please call back.")
        return Response(status_code=204)

    # -- admin API (WNYReply operator) ----------------------------------------

    def require_token(authorization: str = Header(default="")) -> None:
        token = settings.dispatch_api_token
        if not token:
            raise HTTPException(503, "DISPATCH_API_TOKEN is not configured")
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "invalid token")

    @app.get("/dispatch/tickets", dependencies=[Depends(require_token)])
    def list_tickets(business: str | None = None) -> list[dict[str, Any]]:
        """Open tickets, Priority One first. Filter with ?business=<twilio number>."""
        return db.open_tickets(_business_param(business))

    @app.post("/dispatch/tickets/{ticket_id}/status", dependencies=[Depends(require_token)])
    def update_ticket(ticket_id: int, body: TicketStatusUpdate) -> dict[str, Any]:
        if body.status not in TICKET_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(TICKET_STATUSES)}")
        if not db.get_ticket(ticket_id):
            raise HTTPException(404, "ticket not found")
        return db.update_ticket_status(ticket_id, body.status)

    @app.get("/dispatch/customers", dependencies=[Depends(require_token)])
    def list_customers(business: str | None = None) -> list[dict[str, Any]]:
        return db.list_customers(_business_param(business))

    @app.post("/dispatch/customers", status_code=201, dependencies=[Depends(require_token)])
    def add_customer(body: CustomerIn) -> dict[str, Any]:
        if body.property_type and body.property_type not in PROPERTY_TYPES:
            raise HTTPException(422, f"property_type must be one of {', '.join(PROPERTY_TYPES)}")
        if not db.get_business(body.business_number):
            raise HTTPException(404, "unknown business_number")
        data = body.model_dump(exclude={"business_number", "name", "address"})
        data["phone"] = normalize_phone(body.phone) or body.phone
        return db.add_customer(body.business_number, body.name, body.address, **data)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
