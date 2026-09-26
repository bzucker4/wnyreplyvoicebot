"""FastAPI app: Twilio voice webhooks plus a small owner/dispatch API."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from .agent import DispatchAgent, TurnResult, greeting
from .config import Settings, load_settings
from .db import (
    PROPERTY_TYPES,
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    SESSION_DROPPED,
    SESSION_TRANSFERRED,
    TICKET_STATUSES,
    Database,
)
from .sms import Notifier, SmsSender, build_sms_sender, normalize_phone
from .tools import ToolContext

log = logging.getLogger(__name__)

STILL_THERE = "Are you still there?"
ENDED_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


class TicketStatusUpdate(BaseModel):
    status: str


class CustomerIn(BaseModel):
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
    db = db or Database(settings.database_path)
    notifier = Notifier(settings, sms or build_sms_sender(settings))
    agent = agent or DispatchAgent(settings)
    validator = RequestValidator(settings.twilio_auth_token) if settings.twilio_auth_token else None
    # Biases Twilio speech recognition toward local place names and service words.
    speech_hints = ",".join(t.title() for t in settings.service_towns) + (
        ",driveway,parking lot,sidewalk,de-icing,seasonal,one-time,commercial,residential"
    )

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

    def twiml(resp: VoiceResponse) -> Response:
        return Response(content=str(resp), media_type="application/xml")

    def say(resp: VoiceResponse | Gather, text: str) -> None:
        resp.say(text, voice=settings.tts_voice)

    def listen(resp: VoiceResponse, prompt: str | None) -> None:
        gather = Gather(
            input="speech",
            action="/voice/respond",
            method="POST",
            timeout=settings.silence_timeout_seconds,
            speech_timeout="auto",
            speech_model="phone_call",
            language="en-US",
            hints=speech_hints,
            action_on_empty_result=False,
        )
        if prompt:
            say(gather, prompt)
        resp.append(gather)
        # Reached only after the caller has been silent for the gather timeout.
        resp.redirect("/voice/no-input", method="POST")

    def render_turn(turn: TurnResult) -> VoiceResponse:
        resp = VoiceResponse()
        if turn.control.transfer_to:
            say(resp, turn.speech)
            resp.dial(turn.control.transfer_to, action="/voice/dial-result", method="POST",
                      timeout=25, caller_id=settings.twilio_from_number or None)
        elif turn.control.hang_up:
            say(resp, turn.speech)
            resp.hangup()
        else:
            listen(resp, turn.speech)
        return resp

    def session_for(form: dict[str, str]):
        return db.get_or_create_session(form["CallSid"], form.get("From", ""))

    # -- voice webhooks -------------------------------------------------------

    @app.post("/voice/incoming")
    def voice_incoming(form: dict[str, str] = Depends(twilio_form)) -> Response:
        session_for(form)
        resp = VoiceResponse()
        listen(resp, greeting(settings))
        return twiml(resp)

    @app.post("/voice/respond")
    def voice_respond(form: dict[str, str] = Depends(twilio_form)) -> Response:
        session = session_for(form)
        speech = form.get("SpeechResult", "").strip()
        if not speech:
            return voice_no_input(form)
        session.no_input_count = 0
        ctx = ToolContext(db=db, settings=settings, notifier=notifier, call_sid=session.call_sid,
                          caller_phone=session.caller_phone)
        turn = agent.respond(session, speech, ctx)
        if turn.control.transfer_to:
            session.status = SESSION_TRANSFERRED
        elif turn.control.hang_up:
            session.status = SESSION_COMPLETED
        db.save_session(session)
        return twiml(render_turn(turn))

    @app.post("/voice/no-input")
    def voice_no_input(form: dict[str, str] = Depends(twilio_form)) -> Response:
        session = session_for(form)
        session.no_input_count += 1
        db.save_session(session)
        resp = VoiceResponse()
        if session.no_input_count > settings.max_no_input_prompts:
            # Left "active" so the status callback treats it as a dropped call and texts them.
            say(resp, "I'll let you go. We'll text you so you can reach us.")
            resp.hangup()
        else:
            listen(resp, STILL_THERE)
        return twiml(resp)

    @app.post("/voice/dial-result")
    def voice_dial_result(form: dict[str, str] = Depends(twilio_form)) -> Response:
        """After a transfer: if nobody picked up, promise a callback instead of dead air."""
        resp = VoiceResponse()
        if form.get("DialCallStatus") not in {"completed", "answered"}:
            notifier.alert_owner(f"MISSED TRANSFER from {form.get('From', '?')}. Please call them back.")
            say(resp, f"Sorry, nobody could pick up. The owner will call you back within {settings.callback_timeframe}.")
        resp.hangup()
        return twiml(resp)

    @app.post("/voice/status")
    def voice_status(form: dict[str, str] = Depends(twilio_form)) -> Response:
        """Call status callback. A call that ends while still active was dropped: text the caller."""
        if form.get("CallStatus") not in ENDED_CALL_STATUSES:
            return Response(status_code=204)
        session = session_for(form)
        if session.status != SESSION_ACTIVE:
            return Response(status_code=204)
        session.status = SESSION_DROPPED
        db.save_session(session)
        if db.tickets_for_call(session.call_sid):
            return Response(status_code=204)  # caller already got a confirmation text
        notifier.text(
            session.caller_phone,
            f"Thanks for calling {settings.company_name}. We got your number and will call you back shortly.",
        )
        notifier.alert_owner(f"DROPPED CALL from {session.caller_phone or 'unknown number'}. No details captured; please call back.")
        return Response(status_code=204)

    # -- owner / dispatch API -------------------------------------------------

    def require_token(authorization: str = Header(default="")) -> None:
        token = settings.dispatch_api_token
        if not token:
            raise HTTPException(503, "DISPATCH_API_TOKEN is not configured")
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "invalid token")

    @app.get("/dispatch/tickets", dependencies=[Depends(require_token)])
    def list_tickets() -> list[dict[str, Any]]:
        """Open tickets, Priority One first."""
        return db.open_tickets()

    @app.post("/dispatch/tickets/{ticket_id}/status", dependencies=[Depends(require_token)])
    def update_ticket(ticket_id: int, body: TicketStatusUpdate) -> dict[str, Any]:
        if body.status not in TICKET_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(TICKET_STATUSES)}")
        if not db.get_ticket(ticket_id):
            raise HTTPException(404, "ticket not found")
        return db.update_ticket_status(ticket_id, body.status)

    @app.get("/dispatch/customers", dependencies=[Depends(require_token)])
    def list_customers() -> list[dict[str, Any]]:
        return db.list_customers()

    @app.post("/dispatch/customers", status_code=201, dependencies=[Depends(require_token)])
    def add_customer(body: CustomerIn) -> dict[str, Any]:
        if body.property_type and body.property_type not in PROPERTY_TYPES:
            raise HTTPException(422, f"property_type must be one of {', '.join(PROPERTY_TYPES)}")
        data = body.model_dump()
        data["phone"] = normalize_phone(body.phone) or body.phone
        return db.add_customer(**data)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
