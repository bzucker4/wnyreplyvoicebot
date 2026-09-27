"""Service area checks against a business's town list and ZIP prefixes."""

from __future__ import annotations

import re
from typing import Iterable

# Handy seed values for a Buffalo-area business (Erie and Niagara counties).
WNY_TOWNS: tuple[str, ...] = (
    "alden", "amherst", "aurora", "blasdell", "boston", "buffalo", "cheektowaga",
    "clarence", "depew", "east aurora", "eden", "elma", "evans", "grand island",
    "hamburg", "kenmore", "lackawanna", "lancaster", "lewiston", "lockport",
    "marilla", "niagara falls", "north tonawanda", "orchard park", "sloan",
    "snyder", "tonawanda", "wales", "west seneca", "wheatfield", "williamsville",
    "youngstown",
)
WNY_ZIP_PREFIXES: tuple[str, ...] = ("140", "141", "142")


def normalize_town(town: str) -> str:
    town = re.sub(r"\s+", " ", town.strip().lower())
    for prefix in ("town of ", "city of ", "village of "):
        if town.startswith(prefix):
            town = town[len(prefix):]
    return town.removesuffix(", ny").removesuffix(" ny").strip()


def check_service_area(
    town: str | None,
    zip_code: str | None,
    towns: Iterable[str],
    zip_prefixes: Iterable[str],
) -> dict:
    normalized = normalize_town(town) if town else ""
    zip_clean = re.sub(r"\D", "", zip_code or "")
    zip_ok = len(zip_clean) == 5 and any(zip_clean.startswith(p) for p in zip_prefixes)
    town_ok = normalized in {normalize_town(t) for t in towns}
    return {
        "town": normalized or None,
        "zip_code": zip_clean or None,
        "in_service_area": town_ok or zip_ok,
        "matched_on": "town" if town_ok else ("zip_code" if zip_ok else None),
    }
