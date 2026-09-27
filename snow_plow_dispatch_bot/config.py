"""Service-wide configuration from environment variables.

Business-specific settings (name, service area, prices, alert phone, ...) live
in the database: see business.py.
"""

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
    # e.g. postgresql+psycopg://postgres.<ref>:<password>@aws-0-us-east-1.pooler.supabase.com:5432/postgres
    database_url: str = "sqlite:///snow_plow_dispatch.db"
    auto_create_schema: bool = False  # dev/test with SQLite only; production uses migrations/

    # Claude
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "low"  # voice is latency-sensitive
    anthropic_timeout_seconds: float = 8.0
    anthropic_max_retries: int = 1
    max_agent_steps: int = 5

    # Twilio (one account serves every business's number)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    validate_twilio_signature: bool = True
    public_base_url: str = ""  # e.g. https://voice.wnyreply.com, used for signature checks
    tts_voice: str = "Polly.Joanna-Neural"
    silence_timeout_seconds: int = 5
    max_no_input_prompts: int = 2
    send_sms: bool = True

    default_callback_timeframe: str = "30 minutes"
    dispatch_api_token: str = ""

    @property
    def twilio_rest_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)


def load_settings() -> Settings:
    d = Settings
    return Settings(
        database_url=os.environ.get("DATABASE_URL", d.database_url),
        auto_create_schema=_bool("AUTO_CREATE_SCHEMA", d.auto_create_schema),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", d.anthropic_model),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", d.anthropic_effort),
        anthropic_timeout_seconds=float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", d.anthropic_timeout_seconds)),
        anthropic_max_retries=_int("ANTHROPIC_MAX_RETRIES", d.anthropic_max_retries),
        max_agent_steps=_int("MAX_AGENT_STEPS", d.max_agent_steps),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        validate_twilio_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        tts_voice=os.environ.get("TTS_VOICE", d.tts_voice),
        silence_timeout_seconds=_int("SILENCE_TIMEOUT_SECONDS", d.silence_timeout_seconds),
        max_no_input_prompts=_int("MAX_NO_INPUT_PROMPTS", d.max_no_input_prompts),
        send_sms=_bool("SEND_SMS", True),
        default_callback_timeframe=os.environ.get("DEFAULT_CALLBACK_TIMEFRAME", d.default_callback_timeframe),
        dispatch_api_token=os.environ.get("DISPATCH_API_TOKEN", ""),
    )
