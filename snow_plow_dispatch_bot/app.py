"""FastAPI app: Twilio voice webhooks plus a small dispatcher API."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from .agent import DispatchAgent, TurnResult, greeting
from .config import Settings, load_settings
from .db import ALL_STATUSES, Database
from .service_area import SERVICE_TOWNS
from .sms import SmsSender, build_sms_sender
from .tools import ToolContext, estimate_eta_minutes

log = logging.getLogger(__name__)

# Biases Twilio speech recognition toward local place names.
SPEECH_HINTS = ",".join(sorted(t.title() for t in SERVICE_TOWNS)) + ",driveway,parking lot,sidewalk,salting"


class StatusUpdate(BaseModel):
    status: str


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    agent: DispatchAgent | None = None,
    sms: SmsSender | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings.database_path)
    sms = sms or build_sms_sender(settings)
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
            signature = request.headers.get("X-Twilio-Signature", "")
            if not validator.validate(url, form, signature):
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
            speech_timeout="auto",
            speech_model="phone_call",
            language="en-US",
            hints=SPEECH_HINTS,
            action_on_empty_result=False,
        )
        if prompt:
            say(gather, prompt)
        resp.append(gather)
        # Reached only when the caller says nothing.
        resp.redirect("/voice/no-input", method="POST")

    def render_turn(turn: TurnResult) -> VoiceResponse:
        resp = VoiceResponse()
        if turn.control.transfer and settings.dispatcher_phone:
            say(resp, turn.speech)
            resp.dial(settings.dispatcher_phone, caller_id=settings.twilio_from_number or None)
        elif turn.control.hang_up:
            say(resp, turn.speech)
            resp.hangup()
        else:
            listen(resp, turn.speech)
        return resp

    # -- voice webhooks -------------------------------------------------------

    @app.post("/voice/incoming")
    async def voice_incoming(form: dict[str, str] = Depends(twilio_form)) -> Response:
        db.get_or_create_session(form["CallSid"], form.get("From", ""))
        resp = VoiceResponse()
        listen(resp, greeting(settings))
        return twiml(resp)

    @app.post("/voice/respond")
    def voice_respond(form: dict[str, str] = Depends(twilio_form)) -> Response:
        session = db.get_or_create_session(form["CallSid"], form.get("From", ""))
        speech = form.get("SpeechResult", "").strip()
        if not speech:
            return voice_no_input(form)
        session.no_input_count = 0
        ctx = ToolContext(db=db, settings=settings, sms=sms, call_sid=session.call_sid,
                          caller_phone=session.caller_phone)
        turn = agent.respond(session, speech, ctx)
        if turn.control.hang_up or turn.control.transfer:
            session.status = "transferred" if turn.control.transfer else "completed"
        db.save_session(session)
        return twiml(render_turn(turn))

    @app.post("/voice/no-input")
    def voice_no_input(form: dict[str, str] = Depends(twilio_form)) -> Response:
        session = db.get_or_create_session(form["CallSid"], form.get("From", ""))
        session.no_input_count += 1
        db.save_session(session)
        resp = VoiceResponse()
        if session.no_input_count > settings.max_no_input_prompts:
            say(resp, "I didn't hear anything, so I'll hang up now. Please call back anytime.")
            resp.hangup()
        else:
            listen(resp, "Sorry, I didn't catch that. How can I help with your snow service?")
        return twiml(resp)

    @app.post("/voice/status")
    async def voice_status(form: dict[str, str] = Depends(twilio_form)) -> Response:
        """Twilio call status callback; closes out the session when the call ends."""
        if form.get("CallStatus") in {"completed", "busy", "failed", "no-answer", "canceled"}:
            session = db.get_or_create_session(form["CallSid"], form.get("From", ""))
            if session.status == "active":
                session.status = "completed"
                db.save_session(session)
        return Response(status_code=204)

    # -- dispatcher API -------------------------------------------------------

    def require_dispatcher(authorization: str = Header(default="")) -> None:
        token = settings.dispatch_api_token
        if not token:
            raise HTTPException(503, "DISPATCH_API_TOKEN is not configured")
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "invalid token")

    @app.get("/dispatch/queue", dependencies=[Depends(require_dispatcher)])
    def dispatch_queue() -> list[dict[str, Any]]:
        queue = db.open_queue()
        for r in queue:
            r["eta_minutes"] = estimate_eta_minutes(db, settings, r["id"])
        return queue

    @app.post("/dispatch/requests/{request_id}/status", dependencies=[Depends(require_dispatcher)])
    def dispatch_update(request_id: int, body: StatusUpdate) -> dict[str, Any]:
        if body.status not in ALL_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(ALL_STATUSES)}")
        if not db.get_request(request_id):
            raise HTTPException(404, "request not found")
        updated = db.update_request_status(request_id, body.status)
        if body.status == "en_route":
            sms.send(updated["customer_phone"],
                     f"{settings.company_name}: a plow is on the way to {updated['service_address']}.")
        return updated

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
