"""Create a local SQLite database with one demo business, for trying the bot without Supabase.

    python scripts/seed_local_business.py --db sqlite:///local.db --number +17165550100 \
        --alert-phone +17165550199

Production reads `businesses` from Supabase; manage voice_settings there with SQL.
"""

from __future__ import annotations

import argparse

from snow_plow_dispatch_bot.service_area import WNY_TOWNS, WNY_ZIP_PREFIXES
from snow_plow_dispatch_bot.storage import Database, businesses, voice_settings


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="sqlite:///local.db")
    p.add_argument("--number", required=True, help="Twilio number callers dial, E.164")
    p.add_argument("--name", default="WNY Demo Plowing")
    p.add_argument("--alert-phone", required=True)
    p.add_argument("--on-call-phone")
    p.add_argument("--booking-link", default="https://example.com/book")
    args = p.parse_args()

    if not args.db.startswith("sqlite"):
        raise SystemExit("Refusing to seed a non-SQLite database; use SQL against Supabase instead.")
    db = Database.from_url(args.db)
    db.create_schema()
    with db.engine.begin() as c:
        c.execute(businesses.insert().values(
            twilio_number=args.number, business_name=args.name,
            service_area="Buffalo and the Erie and Niagara county suburbs",
            booking_link=args.booking_link, contract_type="seasonal", active=True,
            alert_phone=args.alert_phone,
            price_bands="Residential driveway, seasonal: $450-750\nResidential driveway, per-push: $45-75",
        ))
        c.execute(voice_settings.insert().values(
            twilio_number=args.number, on_call_phone=args.on_call_phone, callback_timeframe="30 minutes",
            service_towns=",".join(WNY_TOWNS), service_zip_prefixes=",".join(WNY_ZIP_PREFIXES),
        ))
    print(f"Seeded {args.name} ({args.number}) into {args.db}")


if __name__ == "__main__":
    main()
