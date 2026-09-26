"""Western New York service area: Erie and Niagara county municipalities."""

from __future__ import annotations

import re

SERVICE_TOWNS = frozenset(
    {
        "alden", "amherst", "aurora", "blasdell", "boston", "buffalo", "cheektowaga",
        "clarence", "depew", "east aurora", "eden", "elma", "evans", "grand island",
        "hamburg", "kenmore", "lackawanna", "lancaster", "lewiston", "lockport",
        "marilla", "niagara falls", "north tonawanda", "orchard park", "sloan",
        "snyder", "tonawanda", "wales", "west seneca", "wheatfield", "williamsville",
        "youngstown",
    }
)

# 140xx-142xx covers Buffalo and the surrounding Erie/Niagara county suburbs.
_ZIP_RE = re.compile(r"^14[0-2]\d\d$")


def normalize_town(town: str) -> str:
    town = re.sub(r"\s+", " ", town.strip().lower())
    for prefix in ("town of ", "city of ", "village of "):
        if town.startswith(prefix):
            town = town[len(prefix):]
    return town.removesuffix(", ny").removesuffix(" ny").strip()


def check_service_area(town: str, zip_code: str | None = None) -> dict:
    normalized = normalize_town(town) if town else ""
    zip_ok = bool(zip_code and _ZIP_RE.match(zip_code.strip()))
    town_ok = normalized in SERVICE_TOWNS
    return {
        "town": normalized,
        "zip_code": zip_code,
        "in_service_area": town_ok or zip_ok,
        "matched_on": "town" if town_ok else ("zip_code" if zip_ok else None),
    }
