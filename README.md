# wnyreplyvoicebot: `snow_plow_dispatch_bot`

An AI phone agent for a Western New York snow plow service. Customers call in, and the bot
books plow or salting visits, checks on a truck, cancels jobs, or transfers the call to a
human dispatcher. It is built on Twilio Voice, FastAPI and Claude.

See [`agent-spec.md`](agent-spec.md) for behavior, architecture and acceptance criteria.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in ANTHROPIC_API_KEY and the Twilio values
set -a && source .env && set +a
uvicorn snow_plow_dispatch_bot.app:create_app --factory --host 0.0.0.0 --port 8000
```

Then in the Twilio console, for your phone number:

- **A call comes in** → Webhook `POST https://<your-host>/voice/incoming`
- **Call status changes** → `POST https://<your-host>/voice/status`

For local testing, expose port 8000 with a tunnel such as ngrok and set `PUBLIC_BASE_URL` to
the tunnel URL so signature validation matches. You can also set
`TWILIO_VALIDATE_SIGNATURE=false`, but only locally.

## Dispatcher API

```bash
curl -H "Authorization: Bearer $DISPATCH_API_TOKEN" http://localhost:8000/dispatch/queue
curl -X POST -H "Authorization: Bearer $DISPATCH_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"status": "en_route"}' http://localhost:8000/dispatch/requests/1/status
```

## Configuration

All settings come from environment variables. See [`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `ANTHROPIC_EFFORT` | `low` | Raise it if booking accuracy suffers; each step up adds latency |
| `ANTHROPIC_TIMEOUT_SECONDS` | `8` | Twilio drops webhooks after 15 s |
| `DISPATCHER_PHONE` | none | Transfers are off until this is set; the bot promises a callback instead |
| `TRUCK_COUNT`, `MINUTES_PER_JOB` | `4`, `25` | Drive the ETA estimate |
| `DISPATCH_API_TOKEN` | none | The dispatch API returns 503 until this is set |

## Tests

```bash
pytest
```

The tests replace the Claude client with a scripted fake, so they need no API key or network.

## Layout

```
snow_plow_dispatch_bot/
  app.py           FastAPI app: Twilio webhooks + dispatch API
  agent.py         System prompt and the Claude tool loop for one caller turn
  tools.py         Tool schemas, validation and handlers
  db.py            SQLite: customers, service_requests, call_sessions
  service_area.py  Erie/Niagara county towns and ZIP check
  sms.py           Twilio SMS confirmations
  config.py        Environment-driven settings
tests/
```
