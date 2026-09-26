"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    company_name: str = "WNY Snow Plow Dispatch"
    database_path: str = "snow_plow_dispatch.db"

    # Claude
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "low"  # voice is latency-sensitive
    anthropic_timeout_seconds: float = 8.0
    anthropic_max_retries: int = 1
    max_agent_steps: int = 5

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    validate_twilio_signature: bool = True
    public_base_url: str = ""  # e.g. https://plowbot.example.com, used for signature checks
    tts_voice: str = "Polly.Joanna-Neural"
    dispatcher_phone: str = ""
    send_sms_confirmations: bool = True
    max_no_input_prompts: int = 2

    # Dispatch operations
    truck_count: int = 4
    minutes_per_job: int = 25
    dispatch_api_token: str = ""

    @property
    def twilio_rest_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)


def load_settings() -> Settings:
    return Settings(
        company_name=os.environ.get("COMPANY_NAME", Settings.company_name),
        database_path=os.environ.get("DATABASE_PATH", Settings.database_path),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", Settings.anthropic_model),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", Settings.anthropic_effort),
        anthropic_timeout_seconds=float(
            os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", Settings.anthropic_timeout_seconds)
        ),
        anthropic_max_retries=_int("ANTHROPIC_MAX_RETRIES", Settings.anthropic_max_retries),
        max_agent_steps=_int("MAX_AGENT_STEPS", Settings.max_agent_steps),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER", ""),
        validate_twilio_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        tts_voice=os.environ.get("TTS_VOICE", Settings.tts_voice),
        dispatcher_phone=os.environ.get("DISPATCHER_PHONE", ""),
        send_sms_confirmations=_bool("SEND_SMS_CONFIRMATIONS", True),
        max_no_input_prompts=_int("MAX_NO_INPUT_PROMPTS", Settings.max_no_input_prompts),
        truck_count=_int("TRUCK_COUNT", Settings.truck_count),
        minutes_per_job=_int("MINUTES_PER_JOB", Settings.minutes_per_job),
        dispatch_api_token=os.environ.get("DISPATCH_API_TOKEN", ""),
    )
