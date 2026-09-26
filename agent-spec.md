# Agent spec: `snow_plow_dispatch_bot`

**Mode:** Greenfield
**Repository:** `wnyreplyvoicebot`
**Status:** v0.2, built from the voice prompt pack

## Purpose

A 24/7 phone dispatch assistant for a snow removal company. It answers every call and
qualifies the caller. Then it does one of three things: captures a lead for the owner to
quote, logs a request or complaint from an existing customer, or escalates an emergency to a
person. It should sound like a competent dispatcher, not a phone menu.

The conversation behavior comes from the voice prompt pack. Its text lives in `SYSTEM_PROMPT`
in `snow_plow_dispatch_bot/agent.py`. The pack's placeholders are filled from settings:

| Placeholder | Setting | Default |
|---|---|---|
| `[COMPANY NAME]` | `COMPANY_NAME` | WNY Snow Plow Dispatch |
| `[SERVICE AREA]` | `SERVICE_AREA` | Buffalo and the Erie and Niagara county suburbs |
| `[ZIP CODES / TOWNS]` | `SERVICE_TOWNS`, `SERVICE_ZIP_PREFIXES` | Erie/Niagara towns; `140`, `141`, `142` |
| `[TIMEFRAME]` | `CALLBACK_TIMEFRAME` | 30 minutes |
| Owner name(s) callers may ask for | `OWNER_NAMES` | none |

## Business context (from the prompt pack)

- **Services:** residential driveway plowing, commercial lot plowing, sidewalk clearing, de-icing.
- **Trigger depths:** 2 inches for residential. For commercial, 1 inch or whatever the contract says.
- **Pricing:** the bot gives ranges only and never an exact price. Residential is $40–$100
  per visit or $300–$650 for the season. Commercial is $100–$400+ per visit.
- **Billing:** residential seasonal contracts are paid upfront and commercial is billed
  monthly. Per-visit service is available, but seasonal is the better value.

## Call handling

| Call type | What the bot does | Tools |
|---|---|---|
| New service or quote | Qualifies in this order: residential or commercial, address or postal code (service area check), full season or one time. Then collects name, callback number, preferred contact method and access notes. Tells the caller the owner will call back within `[TIMEFRAME]` | `check_service_area` → `create_ticket(new_lead)` |
| Outside the area | "Sorry, we don't cover that area…" No lead is logged | `check_service_area` |
| Existing customer | Gets the address, pulls the record, asks what they need, logs it | `lookup_customer` → `create_ticket(existing_customer)` |
| Complaint or missed pass | Apologizes, captures the address and what happened; the owner gets an SMS | `create_ticket(complaint)` |
| Emergency (safety hazard, icy ramp, commercial access) | Priority One. Captures the address and callback number, then transfers to on-call | `create_ticket(emergency)` → `transfer_call(safety_hazard)` |
| Wrong number or sales call | Ends the call politely | `end_call` |

**Immediate transfers.** The bot transfers right away for:
- a safety hazard, which goes to `ON_CALL_PHONE`, or `OWNER_PHONE` if that isn't set;
- an angry customer, or one threatening to cancel;
- a large commercial property: a mall, a condo board, or several lots;
- a caller asking for the owner by name;
- two failed attempts to understand the caller.

All of these go to `OWNER_PHONE` except safety hazards. Before every transfer the owner gets
an SMS with the reason. If the transfer isn't answered, the caller is told the owner will call
back within the timeframe and the owner is texted.

**Notifications.** Every ticket texts a confirmation to the caller. It also sends the owner a
structured alert with the ticket type and number, name, address, service
(property / service / contract), urgency, callback number and preferred contact, and any
details or access notes.

## Style

- Replies are at most two short sentences unless the caller asks for detail.
- Uses natural phrases: "got it," "okay," "one moment."
- Doesn't volunteer that it's an AI. If a caller directly asks, it answers honestly.
- The caller's words come from speech recognition, so unusual addresses are spelled back.
  When the bot didn't understand, it says "I want to make sure I get this right. Can you repeat that?"
- Life-threatening situations (injury, someone trapped, fire, downed lines) get "call 911
  first" before anything else.

## Error handling

| Situation | Behavior |
|---|---|
| Silence for 5 s (`SILENCE_TIMEOUT_SECONDS`) | "Are you still there?" After `MAX_NO_INPUT_PROMPTS`, the bot hangs up |
| Call drops or ends before the bot finishes | The status callback texts the caller: "Thanks for calling [COMPANY]. We got your number and will call you back shortly." The owner gets a DROPPED CALL alert. Skipped if a ticket (and its confirmation text) already went out |
| Claude API error, refusal, truncated tool call, step limit | Apologizes, texts the owner CALLBACK NEEDED, and transfers to the owner. If no owner line is set, promises a callback and texts the caller |
| Blocked or anonymous caller ID | No SMS is attempted to the caller |

## Privacy

`lookup_customer` reveals account details (name, address, notes) only when the caller is
calling from the phone number on the account. When the match is by address from a different
number, the model is told an account exists, but not whose it is.

## Architecture

```
Caller ──PSTN──▶ Twilio ──webhooks──▶ FastAPI (/voice/*)
                                        │
                                        ├─▶ DispatchAgent ──▶ Claude Messages API (strict tools)
                                        │        └─▶ tools.py ──▶ SQLite (customers, tickets, call_sessions)
                                        │                     └─▶ Twilio SMS (caller confirmations, owner alerts)
                                        ▼
                               TwiML <Gather>/<Say>/<Dial>/<Hangup>
```

- **Speech:** Twilio `<Gather input="speech">` recognizes speech, with hints for town and
  service words, and `<Say>` speaks with a Polly neural voice. Each caller utterance is one
  webhook, and the history for each call is stored and replayed append-only.
- **Model:** `claude-opus-5` with adaptive thinking at `effort: low` for phone latency. It
  sends `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), and the system
  prompt and tools are cached.

## Interfaces

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /voice/incoming` | Twilio (A call comes in) | Greet and listen |
| `POST /voice/respond` | Twilio | One caller utterance in, one reply out |
| `POST /voice/no-input` | Twilio | "Are you still there?" / hang up |
| `POST /voice/dial-result` | Twilio | Handle an unanswered transfer |
| `POST /voice/status` | Twilio (Call status changes) | Detect dropped calls and send the SMS |
| `GET /dispatch/tickets` | Owner (Bearer token) | Open tickets, Priority One first |
| `POST /dispatch/tickets/{id}/status` | Owner | `new` / `contacted` / `scheduled` / `closed` |
| `GET` / `POST /dispatch/customers` | Owner | List customers and add them, so existing customers can be recognized |
| `GET /healthz` | Monitoring | Liveness |

## Acceptance criteria

- [x] A new-lead call collects every qualifying field in the pack's order, logs a lead, texts
  the caller, and sends the owner a structured alert.
- [x] Addresses outside the service area are turned away without logging a lead.
- [x] Emergencies are Priority One, sorted first, and transferred to on-call.
- [x] Escalations go to the right person, and an unanswered transfer falls back to a promised callback.
- [x] Silence gets "Are you still there?" after 5 seconds.
- [x] Dropped calls text the caller once.
- [x] Model or API failures never leave dead air.
- [x] Account details are only read back to the account's own phone number.
- [x] Forged Twilio webhooks are rejected, and the owner API needs a token.

## Future work

- Stream audio over Twilio Media Streams for lower latency and barge-in.
- Import customers from a CSV or a CRM.
- Owner dashboard, and email alerts for callers who prefer email.
- Evals built from real call transcripts: field capture accuracy and escalation precision/recall.
