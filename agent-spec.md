# Agent spec: `snow_plow_dispatch_bot`

**Mode:** Greenfield
**Repository:** `wnyreplyvoicebot`
**Status:** v0.1, first working version

## Purpose

A phone agent for a snow plowing and salting service in Western New York (Buffalo, Erie and
Niagara counties). During a storm the phones flood with the same few requests: "plow my
driveway", "where's my truck", "cancel, my neighbor did it". The bot answers every call right
away, handles those requests on its own, and hands anything else to a human dispatcher.

## Users

| Who | How they interact |
|---|---|
| Residential and commercial customers | Call the business number and talk to the bot |
| Dispatchers | Take transferred calls; read the queue and update job status through the dispatch API |

## Capabilities

1. **Book service.** Collect name, street address, town, service type (`driveway`,
   `parking_lot`, `sidewalk`, `salting`), priority (`standard` / `urgent`) and driver notes.
   Check the service area, read the details back, create the request only after the caller
   confirms, and text a confirmation.
2. **Check status.** Give the status and estimated arrival of the caller's requests.
3. **Cancel.** Cancel a queued or assigned request. A request that is `en_route` goes to a dispatcher.
4. **Transfer.** Connect the caller to `DISPATCHER_PHONE` when they ask for a person, raise
   billing, pricing or damage, or need something the tools can't do. If no dispatcher line is
   set, promise a callback instead.
5. **Recognize returning callers.** Look them up by caller ID and offer their saved address.

## Out of scope for v0.1

- Quoting prices, taking payments, contracts.
- Route optimization and truck GPS. The ETA is a queue-position heuristic.
- Outbound calls. Outbound SMS covers booking confirmations and "on the way" texts.
- Languages other than English.

## Conversation rules

- Output is spoken by TTS: one to three short sentences, no markdown or lists, one question at a time.
- Speech recognition is imperfect: spell back or re-ask for unusual street names, house numbers and towns.
- Always confirm before creating a request.
- ETAs are estimates; never promise an exact time.
- Emergencies (medical, someone trapped, fire, downed lines): tell the caller to hang up and dial 911.
- **Privacy:** tools are scoped to the calling phone number. The model never supplies a phone
  number, so a caller can't read or change someone else's request.
- Off-topic requests are politely declined.

## Architecture

```
Caller ──PSTN──▶ Twilio ──webhook──▶ FastAPI (/voice/*)
                                      │
                                      ├─▶ DispatchAgent ──▶ Claude Messages API (tools)
                                      │        │
                                      │        └─▶ tools.py ──▶ SQLite (customers, requests, call_sessions)
                                      │                     └─▶ Twilio SMS
                                      ▼
                              TwiML <Say>/<Gather>/<Dial>/<Hangup>
```

- **Speech in and out:** Twilio `<Gather input="speech">` does the speech-to-text (with
  place-name hints) and `<Say>` with a Polly neural voice does the speech. Each caller
  utterance is one webhook.
- **State:** the conversation history for each call is stored in `call_sessions` and replayed
  on every turn. It is append-only and never edited.
- **Model:** `claude-opus-5` with adaptive thinking at `effort: low` for phone-call latency. It
  sends `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) so a policy decline is
  retried server-side, and caches the stable system prompt and tools.
- **Tools** (strict schemas, also checked with Pydantic):
  `lookup_caller_account`, `check_service_area`, `create_service_request`,
  `get_request_status`, `cancel_service_request`, `transfer_to_dispatcher`, `end_call`.
- **Failure handling:** API errors, refusals, truncated tool calls and exceeding
  `MAX_AGENT_STEPS` all end the same way. The bot apologizes and transfers the call, or asks
  the caller to call back if no dispatcher line is set. A caller never hears silence or an
  error tone.
- **ETA:** `(floor(queue_position / TRUCK_COUNT) + 1) × MINUTES_PER_JOB`, with urgent jobs
  ordered ahead of standard ones.

## Interfaces

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /voice/incoming` | Twilio | Greet the caller and start listening |
| `POST /voice/respond` | Twilio | Handle one utterance and reply |
| `POST /voice/no-input` | Twilio | Re-prompt; hang up after `MAX_NO_INPUT_PROMPTS` |
| `POST /voice/status` | Twilio | Status callback; close out the session |
| `GET /dispatch/queue` | Dispatcher (Bearer token) | Open jobs in dispatch order, with ETAs |
| `POST /dispatch/requests/{id}/status` | Dispatcher (Bearer token) | `queued` / `assigned` / `en_route` / `completed` / `cancelled`; `en_route` texts the customer |
| `GET /healthz` | Monitoring | Liveness |

Twilio webhooks are checked with `X-Twilio-Signature`. Behind a proxy, set `PUBLIC_BASE_URL`.

## Acceptance criteria

- [x] A caller can book a driveway plow end to end, gets a request number and ETA, and receives an SMS.
- [x] A request is never created for an address outside Erie or Niagara county.
- [x] A caller can't see or cancel a request placed from another phone number.
- [x] Urgent requests are queued ahead of standard ones.
- [x] Model or API failures lead to a transfer or a polite hang-up, never dead air.
- [x] Silence re-prompts, then hangs up.
- [x] Forged Twilio webhooks are rejected.
- [x] The dispatch API needs a token; setting `en_route` texts the customer.

## Future work

- Stream audio over Twilio Media Streams for lower latency and barge-in.
- A dispatcher web dashboard in place of the raw API.
- Real routing and truck-position ETAs.
- Evals built from recorded transcripts: booking accuracy, address capture and transfer precision.
