"""Per-business configuration: a `businesses` row plus optional `voice_settings`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings


def _csv(raw: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (raw or "").split(",") if p.strip())


@dataclass(frozen=True)
class BusinessProfile:
    twilio_number: str
    name: str
    service_area: str
    price_bands: str
    booking_link: str | None
    default_contract_type: str | None
    alert_phone: str | None
    on_call_phone: str | None
    callback_timeframe: str
    owner_names: tuple[str, ...]
    service_towns: tuple[str, ...]
    service_zip_prefixes: tuple[str, ...]
    accepting_calls: bool

    @property
    def has_structured_area(self) -> bool:
        return bool(self.service_towns or self.service_zip_prefixes)

    @property
    def emergency_phone(self) -> str | None:
        return self.on_call_phone or self.alert_phone

    @classmethod
    def from_row(cls, row: dict[str, Any], settings: Settings) -> "BusinessProfile":
        voice_enabled = row.get("voice_enabled")
        return cls(
            twilio_number=row["twilio_number"],
            name=row["business_name"],
            service_area=row["service_area"],
            price_bands=(row.get("price_bands") or "").strip(),
            booking_link=row.get("booking_link") or None,
            default_contract_type=row.get("contract_type"),
            alert_phone=row.get("alert_phone") or None,
            on_call_phone=row.get("on_call_phone") or None,
            callback_timeframe=row.get("callback_timeframe") or settings.default_callback_timeframe,
            owner_names=_csv(row.get("owner_names")),
            service_towns=_csv(row.get("service_towns")),
            service_zip_prefixes=_csv(row.get("service_zip_prefixes")),
            # No voice_settings row means voice is on for any active business.
            accepting_calls=bool(row["active"]) and voice_enabled is not False,
        )
