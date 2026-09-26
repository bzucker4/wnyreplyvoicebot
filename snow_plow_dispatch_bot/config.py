"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .service_area import DEFAULT_SERVICE_TOWNS, DEFAULT_ZIP_PREFIXES


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # Business ([COMPANY NAME], [SERVICE AREA], [ZIP CODES / TOWNS], [TIMEFRAME])
    company_name: str = "WNY Snow Plow Dispatch"
    service_area_description: str = "Buffalo and the Erie and Niagara county suburbs"
    service_towns: tuple[str, ...] = DEFAULT_SERVICE_TOWNS
    service_zip_prefixes: tuple[str, ...] = DEFAULT_ZIP_PREFIXES
    callback_timeframe: str = "30 minutes"
    owner_names: tuple[str, ...] = ()
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
    silence_timeout_seconds: int = 5
    max_no_input_prompts: int = 2

    # People the bot hands off to
    owner_phone: str = ""  # receives SMS alerts; transfers for non-emergency escalations
    on_call_phone: str = ""  # emergency (Priority One) transfers; falls back to owner_phone
    send_sms: bool = True

    dispatch_api_token: str = ""

    @property
    def twilio_rest_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def emergency_phone(self) -> str:
        return self.on_call_phone or self.owner_phone


def load_settings() -> Settings:
    d = Settings
    return Settings(
        company_name=os.environ.get("COMPANY_NAME", d.company_name),
        service_area_description=os.environ.get("SERVICE_AREA", d.service_area_description),
        service_towns=_list("SERVICE_TOWNS", d.service_towns),
        service_zip_prefixes=_list("SERVICE_ZIP_PREFIXES", d.service_zip_prefixes),
        callback_timeframe=os.environ.get("CALLBACK_TIMEFRAME", d.callback_timeframe),
        owner_names=_list("OWNER_NAMES", d.owner_names),
        database_path=os.environ.get("DATABASE_PATH", d.database_path),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", d.anthropic_model),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", d.anthropic_effort),
        anthropic_timeout_seconds=float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", d.anthropic_timeout_seconds)),
        anthropic_max_retries=_int("ANTHROPIC_MAX_RETRIES", d.anthropic_max_retries),
        max_agent_steps=_int("MAX_AGENT_STEPS", d.max_agent_steps),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER", ""),
        validate_twilio_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        tts_voice=os.environ.get("TTS_VOICE", d.tts_voice),
        silence_timeout_seconds=_int("SILENCE_TIMEOUT_SECONDS", d.silence_timeout_seconds),
        max_no_input_prompts=_int("MAX_NO_INPUT_PROMPTS", d.max_no_input_prompts),
        owner_phone=os.environ.get("OWNER_PHONE", ""),
        on_call_phone=os.environ.get("ON_CALL_PHONE", ""),
        send_sms=_bool("SEND_SMS", True),
        dispatch_api_token=os.environ.get("DISPATCH_API_TOKEN", ""),
    )
