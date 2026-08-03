# WA Business Assistant (v1)

A WhatsApp bot for small businesses (services or goods) that answers questions
about the business, takes bookings or orders, and collects a deposit via
M-Pesa before confirming.

This is v1-scoped deliberately: **no self-serve onboarding**. You provision
each business yourself with the `scripts/manage.py` CLI. That single
simplification is what makes everything else in here tractable to build
well instead of half-building a much bigger system.

## What's actually in here

- FastAPI app (`app/main.py`) with two webhook endpoints: WhatsApp inbound
  messages, M-Pesa deposit callbacks.
- SQLite (async, via SQLAlchemy 2.0) - right choice at "a handful of
  businesses you provision by hand" scale. See "Scaling beyond v1" below for
  exactly what changes when that's no longer true.
- Per-business encrypted credentials (WhatsApp token, M-Pesa keys) - Fernet
  authenticated encryption, single master key from the environment, never
  in the database or git.
- **Idempotent M-Pesa deposit handling.** This is the one piece of real
  engineering rigor I did not let the scope-down cut: a duplicated,
  replayed, or out-of-order M-Pesa callback can only ever produce one
  effect on a payment, never a double-charge or double-confirmation. See
  `app/payments.py` and `tests/test_payments_idempotency.py`.
- A scheduled reconciliation job that catches payments stuck "pending" past
  15 minutes (e.g. the callback never arrived) and queries M-Pesa directly
  instead of leaving a customer's deposit in limbo forever.
- LLM intent extraction with timeout + retry + a safe conversational
  fallback ("let me get the team") if the LLM is slow or returns garbage -
  a broken LLM call degrades a conversation, it never crashes it.
- Structured JSON logging with a correlation ID that follows one inbound
  message through the whole call path, so debugging a live report doesn't
  mean reproducing it locally first.
- Double-booking prevention on the booking flow (exact-slot and overlap
  checks, tested with a concurrent-style test).
- Owner is notified **twice**: immediately when a customer requests a
  booking/order (before payment), and again when the deposit lands.
- Per-business **confirmation mode** - automatic (deposit confirms
  instantly) or manual (owner must `CONFIRM`/`REJECT` via WhatsApp).
- **Takeover mechanism** - the owner can pause the bot for a specific
  customer and message them directly through it, then hand control back.
- **Business hours** - operator-set per business, enforced in the booking/
  reschedule flow and used by the AI for accurate hours Q&A.
- **Cancel and reschedule** via plain WhatsApp messages, with owner
  notifications on every successful cancel/reschedule and a real slot-
  release guarantee (see "Cancel and reschedule" below).
- **CHECK_STATUS** gives a real summary of upcoming bookings/orders,
  not a placeholder message.
- A real, if intentionally small, test suite - tenant scoping, booking
  conflicts, payment idempotency (including a simulated tampered-replay
  attempt), LLM fallback behavior, and encryption/signature round-trips.
  Run it yourself: `pytest -q`.

## Conversation model (non-linear, with memory)

Earlier versions of this bot used a rigid two-step script per booking/order
(ask for the service, then always ask for a slot next, full stop). That
breaks the moment someone says "I want a haircut on Thursday" in one message
- it would ask for the date and time as if nothing was said. This is fixed:

- Every turn is classified fresh by the LLM (`app/ai.py`), which is shown
  the last few turns of conversation AND whatever's already been collected
  for an in-progress booking/order ("pending state").
- The LLM's job each turn is narrow and reliable: report ONLY what's new in
  *this* message (a date mention, a time mention, a quantity) - not to
  recombine everything said so far. The code (`app/workflows/customer.py`)
  does the combining, deterministically, by only overwriting a pending
  field when the LLM actually reports something new for it. This is why
  "I want a haircut Thursday" followed later by just "2pm" works: the date
  is stored the moment it's mentioned and is never touched again until
  either a booking is confirmed or the customer explicitly changes it.
- Because classification runs fresh every turn (not "we're mid-flow, so
  force-parse whatever they say as a date"), an unrelated question asked
  mid-booking ("what are your hours?") gets answered on its own terms
  without losing the day already given - tested explicitly in
  `tests/test_conversation_flow.py`.
- Once a service + date + time (or a product + quantity) are fully known,
  the bot shows an explicit summary and asks for "YES" before creating
  anything or charging a deposit - that confirmation step is the "release"
  point: pending state is only cleared after a real confirm or cancel, not
  automatically.
- `CANCEL_ACTION` lets a customer back out or change their mind at any
  point before confirming, clearing the pending state cleanly.

**Questions the bot can't answer from the catalog** - a partnership
proposal, a bulk/wholesale inquiry, a complaint needing a judgment call, a
custom price negotiation - are classified as `OUT_OF_SCOPE`, a distinct
intent from the technical-failure `FALLBACK`. The bot does **not** improvise
an answer to these from general knowledge; it tells the customer it's
passing the question along, and forwards the customer's actual message to
the owner via WhatsApp. This is deliberately a real classification decision
the LLM makes each turn, not a keyword filter - see the "Rules for
classification" section of the prompt in `app/ai.py` for exactly what
triggers it.

**Known limitation**: date/time parsing (`_parse_date_text`,
`_combine_date_and_time` in `customer.py`) uses `dateutil`'s fuzzy parser,
which handles common phrasings well ("Thursday", "25 August", "2pm",
"14:30") but isn't bulletproof against very unusual phrasing - if it can't
parse a date or time, it says so and asks the customer to rephrase rather
than guessing. Worth watching in early usage and tightening with real
examples if it turns out to trip up often.

## Business hours

Set at provisioning time or updated later:

```bash
python -m scripts.manage create-business ... --hours "Mon-Fri 09:00-18:00, Sat 10:00-14:00"
python -m scripts.manage update-business-hours --business-id 1 --hours "Mon-Sun 08:00-20:00"
```

Format: comma-separated `<day-or-range> <open>-<close>` segments, 24h time
(`Mon-Fri 09:00-18:00`, `Sat 10:00-14:00`). Days not mentioned are closed.
Pass an empty string to `--hours` (or omit it on `create-business`) for **no
restriction at all** - this is also the automatic behavior for any business
provisioned before this feature existed, so nothing breaks on upgrade.

Hours are used in two places:
- **AI Q&A** - "are you open Sunday?", "what time do you close?" are
  answered from the real stored hours, never guessed (see the "Rules for
  classification" in `app/ai.py`'s prompt).
- **Booking/reschedule validation** - a requested slot outside hours, on a
  closed day, or ending after closing time (start + service duration) is
  rejected with a specific message before any deposit is requested. Slots
  in the past are rejected too, separately from the hours check.

Timezone is stored (`--timezone`, default `Africa/Nairobi`) for record/
display purposes only - see the comment on `Business.timezone` in
`app/models.py` for why datetimes stay naive/local rather than doing full
timezone-aware conversion (avoids a much bigger change v1 doesn't need).

## Cancel and reschedule

Customers can cancel or reschedule an existing booking/order via plain
WhatsApp messages - "cancel my appointment", "can I move my haircut to
Friday" - no menu, no special syntax. If they have exactly one active
booking/order, the bot acts on it directly (with a YES/NO confirmation
step); if they have more than one, it lists them numbered and asks which
one, and a bare numeric reply picks it without needing another LLM call.

**v1 explicitly does not handle refunds.** Cancelling a booking/order that
already had a deposit paid does not touch M-Pesa at all - the owner's
cancellation notification says a deposit was paid so they know a manual
refund may be needed, and that's the extent of it.

**Slot release**, worth being explicit about since it's easy to get subtly
wrong: cancelling a booking sets its status to `CANCELLED`, and
rescheduling moves the same row to a new `slot_start`/`slot_end` - either
way, the slot becomes bookable again the instant that commits, because the
conflict check in `repo.create_booking`/`repo.reschedule_booking` excludes
both `CANCELLED` and `REJECTED` bookings. That second exclusion was a real
latent bug fixed as part of this change: earlier code only excluded
`CANCELLED`, meaning a booking the owner rejected (manual-confirmation
mode) permanently blocked its own slot forever. Rescheduling also excludes
the booking's own id from its conflict check, so moving a booking doesn't
spuriously conflict with itself.

**CANCEL_ACTION vs CANCEL_BOOKING/CANCEL_ORDER** are deliberately distinct
intents (see `app/ai.py`'s prompt and the module docstring in
`customer.py`): `CANCEL_ACTION` means "stop what we're doing right now"
(abandon an in-progress draft, or back out of a YES/NO prompt) and only
ever touches conversation state, never a real DB record. `CANCEL_BOOKING`/
`CANCEL_ORDER` mean "undo something that was already fully created."
Collapsing these into one intent was a real ambiguity in an earlier
version of this bot, flagged and fixed here.

Owner notifications are sent on every successful cancel and reschedule -
same recipient and send path as deposit confirmations
(`business.owner_whatsapp_number` via `send_business_message`), and always
as a separate outbound message, never inferred from what the customer was
told. See `app/workflows/owner.py`'s `notify_owner_booking_cancelled`,
`notify_owner_order_cancelled`, and `notify_owner_booking_rescheduled`.

## CHECK_STATUS

Replaced the earlier "coming soon" stub - a customer asking about their
booking/order status now gets a real, short summary of their upcoming
bookings and active orders (service/product, date, and current status).

## Owner commands (new)

The owner controls the bot by messaging the **same business WhatsApp number**
customers message (that's the only number this business can send/receive
from). Send any of these from `owner_whatsapp_number`:

| Command | Effect |
|---|---|
| `TAKEOVER <customer_phone>` | Pauses the bot for that customer. Their messages get forwarded to you instead of auto-answered. |
| `RELEASE <customer_phone>` | Hands the conversation back to the bot. |
| `REPLY <customer_phone> <message>` | Sends `<message>` to that customer directly, as the business. Mainly used during a takeover. |
| `CONFIRM B<id>` / `CONFIRM O<id>` | Manually confirms a booking/order that's awaiting your confirmation (manual-mode businesses only). |
| `REJECT B<id>` / `REJECT O<id>` | Rejects it instead - the customer is told their deposit will be refunded. |

Anything else sent from the owner number gets a short command reference back,
rather than being silently ignored or routed into the customer bot logic.

**Confirmation mode** is set per business at provisioning time:
`--confirmation-mode automatic` (default - deposit paid = confirmed instantly)
or `--confirmation-mode manual` (deposit paid = booking/order sits at
"awaiting your confirmation" until you `CONFIRM`/`REJECT` it). The owner is
notified **twice** either way: once the moment a customer requests a
booking/order (before any money moves), and again once the deposit lands.

**Takeover** doesn't require manual confirmation mode - it works for either
setting, and is meant for "this conversation needs a human, right now"
moments, separate from the confirm/reject decision on a specific
booking/order.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values, see comments in the file
```

Generate the two secrets `.env.example` asks for:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # WA_MASTER_KEY
python -c "import secrets; print(secrets.token_urlsafe(24))"                                  # MPESA_CALLBACK_SECRET
```

Provision your first business:
```bash
python -m scripts.manage create-business \
  --name "Jane's Salon" --type services \
  --whatsapp-phone-number-id <from Meta app dashboard> \
  --whatsapp-token <permanent or long-lived token> \
  --owner-whatsapp-number 2547XXXXXXXX \
  --mpesa-shortcode <till/paybill> --mpesa-passkey <daraja passkey> \
  --mpesa-consumer-key <daraja key> --mpesa-consumer-secret <daraja secret> \
  --deposit-percentage 20

python -m scripts.manage add-service --business-id 1 --name "Haircut" --price 800 --duration-minutes 45
```

Run it:
```bash
uvicorn app.main:app --reload
```

Or containerized:
```bash
docker build -t wa-assistant .
docker run -p 8000:8000 --env-file .env -v $(pwd)/data:/app/data wa-assistant
```

Point Meta's webhook at `https://<your-domain>/webhook/whatsapp` and M-Pesa's
callback at `https://<your-domain>/webhook/mpesa/<your MPESA_CALLBACK_SECRET>`
(the STK Push code already builds this URL for you - just make sure your
`.env` values are right before you go live).

## What I deliberately left out of v1, and why

These aren't oversights - they were cut in an earlier round of this
conversation once the requirement became "you provision businesses, keep it
simple," and I'd stand by all of them at this scale:

- **Postgres** - SQLite handles a handful of businesses fine. The models
  avoid SQLite-specific types on purpose, so this is a connection string +
  Alembic setup later, not a rewrite.
- **The `TenantContext` structural-isolation layer** we discussed and
  prototyped earlier - overkill when you're the one provisioning every
  business by hand. Instead, every repository function requires
  `business_id` as an explicit, non-optional argument (see the module
  docstring in `app/repositories.py`) - a call site that forgets it is a
  `TypeError`, not a silent bug. That's the right amount of rigor for this
  scale.
- **Rate limiting, CI/CD, containerized multi-replica deploy** - build these
  against real traffic patterns once you have them, not against guesses.
- **Owner catalog self-service** - you edit the catalog via
  `scripts/manage.py` (or direct DB access) for now. Owner commands over
  WhatsApp (see above) cover takeover, confirm/reject, and cancel/
  reschedule notifications, not catalog edits.
- **Automated M-Pesa refunds on cancel** - deliberately out of scope (see
  the Cancel/Reschedule section above) - the owner is told a deposit was
  paid and handles the refund manually.

## Scaling beyond v1 (when you actually need to)

- **SQLite → Postgres**: swap `DATABASE_URL`, add Alembic, done - nothing in
  `app/repositories.py` or `app/models.py` assumes SQLite.
- **Concurrent booking correctness under real load**: the current
  overlap-check-then-insert in `create_booking` is safe under SQLite's
  single-writer model. Under Postgres with real concurrent traffic, add a
  `SELECT ... FOR UPDATE` on the service or a proper exclusion constraint -
  flagged explicitly in the code comment where it matters.
- **Self-serve onboarding**: if you ever want businesses to sign themselves
  up instead of you provisioning them, that's the point to revisit
  structural tenant isolation (the `TenantContext` approach) - the
  cost/benefit flips once you're not the one reviewing every new tenant by
  hand.

## Known gaps worth knowing about before you rely on this for real money

- M-Pesa has no callback signature by default - this app mitigates with a
  shared secret in the callback URL path (`MPESA_CALLBACK_SECRET`), which is
  a real but not bulletproof defense. If you want stronger guarantees,
  restrict the callback endpoint at the network/firewall level to
  Safaricom's published IP ranges as well.
- The LLM intent classifier is doing real work matching free-text service/
  product names against your catalog (exact match, then substring fallback).
  For a handful of clearly-named items this works well; if a business has
  many similarly-named services, watch for mismatches in early usage and
  tighten the matching logic then, with real examples in hand rather than
  hypothetical ones.
- `_query_stk_status` and the STK push flow are written against the
  documented Daraja API shape but have not been run against a live
  Safaricom sandbox/production account - test the payment flow end-to-end
  with real (sandbox) credentials before taking real deposits.
