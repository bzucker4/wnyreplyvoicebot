# wnyreplyvoicebot: `snow_plow_dispatch_bot`

A 24/7 AI phone dispatcher for WNYReply's snow removal clients. One deployment serves every
client business. Each call is matched to its business by the Twilio number dialled, using
the same `businesses` table as the WNYReply SMS bot. The bot qualifies the caller. Then it
captures a lead, texts a booking link, logs a request or complaint, or escalates an
emergency to a person. The owner gets a structured SMS alert and the caller gets a text
confirmation, both from the business's own number.

Behavior follows the voice prompt pack. See [`agent-spec.md`](agent-spec.md) for the call
flows, per-business settings and escalation rules.

## Setup (Supabase)

1. Apply the migration to the `wny-snow-lead-bot` project:
   `migrations/20260927000000_create_voice_bot_tables.sql`. It only adds `voice_*` tables
   and doesn't touch `businesses`.
2. Optionally, give a business voice-specific settings:
   ```sql
   insert into voice_settings (twilio_number, on_call_phone, callback_timeframe, owner_names,
                               service_towns, service_zip_prefixes)
   values ('+15855550100', '+15855550188', '30 minutes', 'Mike',
           'Buffalo,Amherst,Cheektowaga', '140,141,142');
   ```
   With no row, an active business still takes calls: the model judges the service area from
   `service_area`, and the callback timeframe comes from `DEFAULT_CALLBACK_TIMEFRAME`.
3. Configure and run:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   cp .env.example .env   # DATABASE_URL, ANTHROPIC_API_KEY, Twilio credentials
   set -a && source .env && set +a
   uvicorn snow_plow_dispatch_bot.app:create_app --factory --host 0.0.0.0 --port 8000
   ```
4. For each business's Twilio number, in the Twilio console:
   - **A call comes in** → `POST https://<your-host>/voice/incoming`
   - **Call status changes** → `POST https://<your-host>/voice/status` (needed for the dropped-call SMS)

## Local dev without Supabase

```bash
python scripts/seed_local_business.py --db sqlite:///local.db --number +17165550100 --alert-phone +17165550199
DATABASE_URL=sqlite:///local.db TWILIO_VALIDATE_SIGNATURE=false \
  uvicorn snow_plow_dispatch_bot.app:create_app --factory --port 8000
```

## Operator API

```bash
AUTH="Authorization: Bearer $DISPATCH_API_TOKEN"
curl -H "$AUTH" "http://localhost:8000/dispatch/tickets?business=%2B17165550100"
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"status": "contacted"}' http://localhost:8000/dispatch/tickets/1/status
# Load a client's existing customers so the bot recognizes them
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"business_number": "+17165550100", "name": "Pat Kowalski", "address": "42 Elmwood Ave", "phone": "716-555-1234", "plan": "seasonal"}' \
     http://localhost:8000/dispatch/customers
```

## Tests

```bash
pytest                                   # SQLite, no network or API key needed
TEST_DATABASE_URL=postgresql+psycopg://... pytest   # against Postgres with the migration applied
```

The Postgres database needs `tests/sql/businesses_mirror.sql` and then the migration applied
first. The tests truncate every `voice_*` table and `businesses`, so never point
`TEST_DATABASE_URL` at the live project.

## Layout

```
snow_plow_dispatch_bot/
  agent.py         Prompt pack (system prompt, filled per business) and the Claude tool loop
  tools.py         Tool schemas, validation, handlers, owner alert format
  app.py           Twilio webhooks + operator API
  business.py      BusinessProfile: businesses row + voice_settings
  storage.py       SQLAlchemy tables and queries (Supabase Postgres / SQLite)
  sms.py           Texts from the business's number
  service_area.py  Town / ZIP checks
  config.py        Service-wide settings from the environment
migrations/        SQL for the Supabase project
scripts/           Local seeding
tests/
```
