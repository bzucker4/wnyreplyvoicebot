# wnyreplyvoicebot: `snow_plow_dispatch_bot`

A 24/7 AI phone dispatcher for a snow removal company. It answers every call and qualifies the
caller. Then it captures a lead for the owner, logs a request or complaint, or escalates an
emergency to a person. The owner gets a structured SMS alert and the caller gets a text
confirmation. It is built on Twilio Voice, FastAPI and Claude.

Behavior follows the voice prompt pack. See [`agent-spec.md`](agent-spec.md) for the call
flows, escalation rules and acceptance criteria.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in company details, phone numbers, ANTHROPIC_API_KEY, Twilio
set -a && source .env && set +a
uvicorn snow_plow_dispatch_bot.app:create_app --factory --host 0.0.0.0 --port 8000
```

Then in the Twilio console, for your phone number:

- **A call comes in** → Webhook `POST https://<your-host>/voice/incoming`
- **Call status changes** → `POST https://<your-host>/voice/status` (needed for the dropped-call SMS)

For local testing, expose port 8000 with a tunnel such as ngrok and set `PUBLIC_BASE_URL` to
the tunnel URL so signature validation matches.

## Key settings

| Variable | What it does |
|---|---|
| `COMPANY_NAME`, `SERVICE_AREA`, `SERVICE_TOWNS`, `SERVICE_ZIP_PREFIXES`, `CALLBACK_TIMEFRAME`, `OWNER_NAMES` | Fill the prompt pack's placeholders |
| `OWNER_PHONE` | Receives SMS alerts, and transfers for everything except emergencies |
| `ON_CALL_PHONE` | Receives Priority One (safety hazard) transfers; falls back to `OWNER_PHONE` |
| `SILENCE_TIMEOUT_SECONDS` | Seconds of silence before "Are you still there?" (default 5) |
| `ANTHROPIC_EFFORT` | `low` by default for phone latency. Raising it adds latency, and Twilio waits at most 15 s for a reply |

The full list is in [`.env.example`](.env.example).

## Owner API

```bash
AUTH="Authorization: Bearer $DISPATCH_API_TOKEN"
curl -H "$AUTH" http://localhost:8000/dispatch/tickets
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"status": "contacted"}' http://localhost:8000/dispatch/tickets/1/status
# Load existing customers so the bot can recognize them
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"name": "Pat Kowalski", "address": "42 Elmwood Ave", "phone": "716-555-1234", "property_type": "residential", "plan": "seasonal"}' \
     http://localhost:8000/dispatch/customers
```

## Tests

```bash
pytest
```

The tests replace the Claude client with a scripted fake, so they need no API key or network.

## Layout

```
snow_plow_dispatch_bot/
  agent.py         Prompt pack (system prompt) and the Claude tool loop for one caller turn
  tools.py         Tool schemas, validation, handlers, owner alert format
  app.py           Twilio webhooks + owner API
  db.py            SQLite: customers, tickets, call_sessions
  sms.py           Caller confirmations and owner alerts
  service_area.py  Town / ZIP checks
  config.py        Environment-driven settings
tests/
```
