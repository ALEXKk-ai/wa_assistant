"""Customer conversation workflow.

Handles what v1+ needs, per business type:
  - answering questions about the business (hours, catalog, prices)
  - booking a service (services businesses) or buying a product (goods businesses)
  - collecting a deposit via M-Pesa STK Push before confirming
  - cancelling or rescheduling an existing booking/order
  - checking status of upcoming bookings/orders

Conversation model: every turn is classified fresh by the LLM (app/ai.py),
which is given the last few turns of history, the business's operating
hours, AND whatever's already been collected for an in-progress
booking/order/cancellation/reschedule ("pending"). The LLM's job each turn
is narrow: report ONLY what's new in *this* message (a date, a time, a
quantity) - the code does all the combining, deterministically, by only
overwriting a pending field when something new comes in for it. This is
what lets "I want a haircut Thursday" be followed later by just "2pm"
without the day being re-asked or lost, and lets an unrelated question
mid-flow get answered without breaking the pending state.

State per (business, customer_phone) is a JSON blob:
{
  "stage": "idle" | "collecting_booking" | "collecting_order" | "confirming"
           | "selecting_booking" | "selecting_order" | "collecting_reschedule",
  "pending": {"type": "booking"|"order"|"cancel_booking"|"cancel_order"
              |"reschedule_booking", ...fields specific to that type},
  "history": [{"role": "customer"|"bot", "text": "..."}, ...]  # capped
}

CANCEL_ACTION vs CANCEL_BOOKING/CANCEL_ORDER - these are deliberately kept
distinct (see the "Rules for classification" in app/ai.py's prompt):
CANCEL_ACTION means "stop what we're doing right now" (abandon an
in-progress draft or back out of a pending confirmation); CANCEL_BOOKING/
CANCEL_ORDER mean "undo something that was already fully created". Mixing
these up was a real bug in an earlier version of this file - CANCEL_ACTION
used to be the only "cancel"-shaped intent and its handler wiped the
entire pending state unconditionally, which would have been confusing if
reused for cancelling a real, already-existing booking.
"""
import json
import re
from datetime import datetime, timedelta

from dateutil import parser as dateutil_parser
from sqlalchemy.ext.asyncio import AsyncSession

from app import ai, payments
from app import hours as hours_mod
from app import repositories as repo
from app.logging_conf import get_logger, log_extra
from app.models import (
    Booking,
    BookingStatus,
    Business,
    BusinessType,
    ConfirmationMode,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from app.repositories import BookingConflictError
from app.whatsapp import send_business_message
from app.workflows import owner as owner_workflow

logger = get_logger(__name__)

STAGE_IDLE = "idle"
STAGE_COLLECTING_BOOKING = "collecting_booking"
STAGE_COLLECTING_ORDER = "collecting_order"
STAGE_CONFIRMING = "confirming"
STAGE_SELECTING_BOOKING = "selecting_booking"
STAGE_SELECTING_ORDER = "selecting_order"
STAGE_COLLECTING_RESCHEDULE = "collecting_reschedule"
STAGE_COLLECTING_TIME_RETRY = "collecting_time_retry"

MAX_HISTORY_ENTRIES = 10  # ~5 exchanges - bounds prompt size/cost

_ACTIVE_DETAIL_STAGES = {
    STAGE_COLLECTING_BOOKING,
    STAGE_COLLECTING_ORDER,
    STAGE_COLLECTING_RESCHEDULE,
    STAGE_COLLECTING_TIME_RETRY,
}
_ACTIVE_DETAIL_TYPES = {"booking", "order", "reschedule_booking", "booking_time_retry"}

_CODE_REQUEST_RE = re.compile(
    r"\b(write|generate|create|show|give|make)\b.*\b(code|script|program|python|javascript|java|html)\b"
    r"|\bpython\s+code\b|\bjavascript\s+code\b",
    re.IGNORECASE,
)
_OFFER_RE = re.compile(
    r"\b(?:do\s+you|can\s+i|can\s+we)\s+(?:offer|have|do|get|provide)\s+(?P<item>.+?)[?.!]*$",
    re.IGNORECASE,
)
_AVAILABLE_RE = re.compile(
    r"\b(?:is|are)\s+(?P<item>.+?)\s+(?:available|offered)[?.!]*$",
    re.IGNORECASE,
)
_PRICE_ITEM_RE = re.compile(
    r"\b(?:cost\s+of|price\s+of|how\s+much\s+(?:for|is|does)\s+|(?:what\s+is\s+the\s+)?price\s+of\s+)(?P<item>.+?)[?.!]*$",
    re.IGNORECASE,
)
_SIMPLE_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|good\s+morning|good\s+afternoon|good\s+evening)\s*[!.]?\s*$",
    re.IGNORECASE,
)
_PAYMENT_STATUS_RE = re.compile(
    r"\b(paid|pay|payment|deposit|m-?pesa|mpesa|stk)\b",
    re.IGNORECASE,
)
_UNGROUNDED_INFO_RE = re.compile(
    r"\b(bring|own|policy|refund|discount|negotiate|custom|proposal|partnership|collaborat|sponsor|complaint|manager|human|owner)\b",
    re.IGNORECASE,
)
_UNLISTED_CATALOG_RE = re.compile(
    r"\b(?:apart from|besides|other than|outside of|not listed|not on (?:the )?list|unlisted|any other|which other|what other)\b"
    r".*\b(?:services?|products?|items?|goods)\b"
    r"|\b(?:services?|products?|items?|goods)\b"
    r".*\b(?:apart from|besides|other than|outside of|not listed|not on (?:the )?list|unlisted|else)\b",
    re.IGNORECASE,
)
_TIME_WITH_MERIDIEM_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<period>a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)
_TIME_WITH_DAYPART_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?:at\s+|in\s+the\s+)?"
    r"(?P<period>morning|afternoon|evening|night)\b",
    re.IGNORECASE,
)
_TIME_24H_RE = re.compile(r"^\s*(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*$")
_BARE_TIME_RE = re.compile(r"^\s*(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*$")
_DATE_WORDS = {
    "today",
    "tomorrow",
    "tommorrow",
    "tonight",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


def _is_manual(business: Business) -> bool:
    return business.confirmation_mode == ConfirmationMode.MANUAL


async def handle_inbound_message(
    session: AsyncSession,
    business: Business,
    customer_phone: str,
    message_text: str,
    mpesa_callback_secret: str,
    customer_name: str | None = None,
) -> str:
    customer = await repo.get_or_create_customer(session, business.id, customer_phone, name=customer_name)
    state_row = await repo.get_conversation_state(session, business.id, customer_phone)
    state = (
        json.loads(state_row.state_json)
        if state_row
        else {"stage": STAGE_IDLE, "pending": {}, "history": []}
    )
    stage = state.get("stage", STAGE_IDLE)
    pending = state.get("pending") or {}
    history = state.get("history") or []

    # A numbered pick from a "which booking?" list is handled directly,
    # deterministically, without invoking the LLM at all - it's a purely
    # mechanical step (index into a list we just showed the customer) and
    # forcing it through classification would only add ambiguity risk for
    # zero benefit.
    if stage in (STAGE_SELECTING_BOOKING, STAGE_SELECTING_ORDER) and message_text.strip().isdigit():
        reply_text, new_stage, new_pending = await _handle_selection(
            session, business, stage, pending, int(message_text.strip())
        )
        history.append({"role": "customer", "text": message_text})
        history.append({"role": "bot", "text": reply_text})
        history = history[-MAX_HISTORY_ENTRIES:]
        await repo.set_conversation_state(
            session,
            business.id,
            customer_phone,
            json.dumps({"stage": new_stage, "pending": new_pending, "history": history}),
        )
        return reply_text

    history.append({"role": "customer", "text": message_text})

    direct_reply = _direct_greeting_reply(business, message_text, stage, pending)
    if direct_reply is None:
        direct_reply = await _direct_payment_status_reply(
            session, business, customer, message_text, mpesa_callback_secret=mpesa_callback_secret
        )

    if direct_reply is None:
        direct_reply = await _direct_pending_booking_reference_reply(session, business, customer, message_text)
    if direct_reply is None:
        direct_reply = await _direct_catalog_availability_reply(session, business, message_text)
    if direct_reply is None:
        direct_reply = _direct_location_reply(business, message_text)
    if direct_reply is None:
        direct_reply = _direct_hours_reply(business, message_text)
    if direct_reply is None:
        direct_reply = _direct_payment_methods_reply(business, message_text)

    if direct_reply is not None:
        if isinstance(direct_reply, tuple):
            reply_text, new_stage, new_pending = direct_reply
        else:
            reply_text, new_stage, new_pending = direct_reply, stage, pending
    else:
        intent = _deterministic_intent(message_text, stage, pending, business)
        if intent is None:
            catalog = await _build_catalog_summary(session, business)
            hours = json.loads(business.hours_json or "{}")
            extra_info = business.extra_info_text or ""
            if business.deposit_percentage and business.deposit_percentage > 0:
                dep_info = f"A {business.deposit_percentage:.0f}% deposit via M-Pesa is required for bookings to secure your slot."
            else:
                dep_info = "No deposit is required for bookings; customers pay when they arrive."
            extra_info = f"{dep_info} {extra_info}".strip() if extra_info else dep_info
            intent = await ai.extract_intent(
                customer_message=message_text,
                business_name=business.name,
                business_type=business.business_type.value,
                catalog=catalog,
                conversation_history=history[:-1],
                pending=pending,
                business_hours_text=hours_mod.format_hours(hours),
                business_address=business.address_text or "not listed",
                business_extra_info=extra_info or "none",
                fulfillment_policy=getattr(business.fulfillment_mode, "value", "both"),
            )
        else:
            logger.info(
                "Intent classified deterministically",
                extra=log_extra(business_id=business.id, intent=intent.type.value, stage=stage),
            )
        logger.info(
            "Intent classified",
            extra=log_extra(business_id=business.id, intent=intent.type.value, stage=stage),
        )
        pre_routed = await _pre_route_conversation_act(
            session, business, customer, customer_phone, message_text, intent, stage, pending
        )
        if pre_routed is not None:
            reply_text, new_stage, new_pending = pre_routed
        else:
            reply_text, new_stage, new_pending = await _dispatch(
                session, business, customer, customer_phone, message_text, intent, stage, pending, mpesa_callback_secret
            )

    history.append({"role": "bot", "text": reply_text})
    history = history[-MAX_HISTORY_ENTRIES:]

    await repo.set_conversation_state(
        session,
        business.id,
        customer_phone,
        json.dumps({"stage": new_stage, "pending": new_pending, "history": history}),
    )
    return reply_text


def _direct_greeting_reply(
    business: Business, message_text: str, stage: str, pending: dict
) -> str | None:
    if stage != STAGE_IDLE or pending:
        return None
    if not _SIMPLE_GREETING_RE.match(message_text):
        return None
    if business.business_type == BusinessType.SERVICES:
        return (
            f"Hello! Welcome to {business.name}. Would you like to book an appointment "
            "or ask something about the studio?"
        )
    return (
        f"Hello! Welcome to {business.name}. Would you like to place an order "
        "or ask something about the shop?"
    )


async def _direct_payment_status_reply(
    session: AsyncSession, business: Business, customer, message_text: str, mpesa_callback_secret: str = ""
) -> str | tuple[str, str, dict] | None:
    lowered = message_text.lower()
    is_status_query = bool(_PAYMENT_STATUS_RE.search(message_text))
    wants_resend = any(
        phrase in lowered
        for phrase in ("resend", "send again", "prompt again", "retry", "didnt get", "didn't get", "another prompt", "new prompt")
    )
    if not is_status_query and not wants_resend:
        return None
    if any(phrase in lowered for phrase in ("do you", "how much", "what is", "is there", "require", "required", "need")):
        return None
    if not any(word in lowered for word in ("paid", "payment", "deposit", "mpesa", "m-pesa", "stk", "resend", "prompt", "retry", "again")):
        return None

    digits = "".join(c for c in message_text if c.isdigit())
    custom_phone = digits if len(digits) >= 9 else None

    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    pending_bookings = [b for b in bookings if b.status == BookingStatus.PENDING_DEPOSIT]
    if pending_bookings:
        booking = pending_bookings[0]
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        service_name = service.name if service else "your booking"

        if wants_resend:
            target = custom_phone or customer.phone_number
            resend_pending = {
                "type": "resend_deposit",
                "booking_id": booking.id,
                "deposit_amount": float(booking.deposit_amount),
                "item_name": service_name,
                "payment_phone": custom_phone,
            }
            reply = (
                f"Sure! Would you like me to send the M-Pesa prompt for KES {_fmt_price(booking.deposit_amount)} ({service_name}) to {target}?\n"
                f"Reply YES to proceed, or reply with a different M-Pesa number (e.g. 0712345678)."
            )
            return reply, STAGE_CONFIRMING, resend_pending

        return (
            f"Thanks - I can see your {service_name} on {booking.slot_start:%d %b at %H:%M} "
            "is still waiting for the M-Pesa confirmation. Once it comes through, "
            "I'll update you here automatically. (Reply 'RESEND' if you need a new prompt)."
        )

    orders = await repo.list_upcoming_orders_for_customer(session, business.id, customer.id)
    pending_orders = [o for o in orders if o.status == OrderStatus.PENDING_DEPOSIT]
    if pending_orders:
        summary = await _order_summary_text(session, business, pending_orders[0])
        order = pending_orders[0]

        if wants_resend:
            target = custom_phone or customer.phone_number
            resend_pending = {
                "type": "resend_deposit",
                "order_id": order.id,
                "deposit_amount": float(order.deposit_amount),
                "item_name": summary,
                "payment_phone": custom_phone,
            }
            reply = (
                f"Sure! Would you like me to send the M-Pesa prompt for KES {_fmt_price(order.deposit_amount)} ({summary}) to {target}?\n"
                f"Reply YES to proceed, or reply with a different M-Pesa number (e.g. 0712345678)."
            )
            return reply, STAGE_CONFIRMING, resend_pending

        return (
            f"Thanks - I can see your order ({summary}) is still waiting for the "
            "M-Pesa confirmation. Once it comes through, I'll update you here automatically. (Reply 'RESEND' if you need a new prompt)."
        )

    return (
        "Thanks for letting us know. I don't see a booking or order currently waiting "
        "for deposit on this chat, so the team may need to check manually."
    )


async def _direct_reschedule_request_transition(
    session: AsyncSession, business: Business, customer, message_text: str, stage: str = STAGE_IDLE
) -> tuple[str, str, dict] | None:
    if stage != STAGE_IDLE:
        return None
    lowered = message_text.lower()
    if "reschedule" not in lowered and "move" not in lowered:
        return None
    if business.business_type != BusinessType.SERVICES:
        return "Rescheduling isn't available for orders - please contact us directly.", STAGE_IDLE, {}

    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    candidates = [
        b for b in bookings
        if b.status in (
            BookingStatus.CONFIRMED,
            BookingStatus.PENDING_DEPOSIT,
            BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION,
        )
    ]
    if not candidates:
        return "You don't have any upcoming bookings to reschedule.", STAGE_IDLE, {}

    selected = await _select_booking_from_message(session, business, candidates, message_text)
    target_text = _reschedule_target_fragment(message_text)
    entities = _extract_active_detail_entities(target_text, {"type": "reschedule_booking"}, business)

    if selected is None:
        if len(candidates) == 1:
            selected = candidates[0]
        else:
            lines = ["Which booking would you like to reschedule?"]
            ids = []
            for i, booking in enumerate(candidates, start=1):
                service = await repo.get_service_for_business(session, business.id, booking.service_id)
                service_name = service.name if service else "a service"
                lines.append(f"{i}. {service_name} on {booking.slot_start:%d %b at %H:%M}")
                ids.append(booking.id)
            pending = {"purpose": "reschedule_booking", "candidates": ids}
            return "\n".join(lines), STAGE_SELECTING_BOOKING, pending

    service = await repo.get_service_for_business(session, business.id, selected.service_id)
    service_name = service.name if service else "your service"
    pending = {"type": "reschedule_booking", "booking_id": selected.id}
    pending.update(entities)
    reply, new_stage, new_pending = await _advance_reschedule(session, business, pending, entities)
    if reply == "What date and time would you like to move it to?":
        reply = (
            f"Okay - rescheduling your {service_name} "
            f"(currently {selected.slot_start:%d %b at %H:%M}). "
            "What date and time would you like instead?"
        )
    return reply, new_stage, new_pending


def _reschedule_target_fragment(message_text: str) -> str:
    lowered = message_text.lower()
    for marker in (" reschedule to ", " move to ", " moved to ", " to "):
        index = lowered.rfind(marker)
        if index != -1:
            return message_text[index + len(marker):].strip()
    return message_text


async def _select_booking_from_message(
    session: AsyncSession, business: Business, bookings: list[Booking], message_text: str
) -> Booking | None:
    lowered = message_text.lower()
    date_hint = _extract_date_text(message_text)
    parsed_hint = _parse_date_text(date_hint) if date_hint else None
    service_names_by_booking: dict[int, str] = {}
    any_service_hint = False
    for booking in bookings:
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        if service is None:
            continue
        service_names_by_booking[booking.id] = service.name.lower()
        if service.name.lower() in lowered:
            any_service_hint = True

    matches = []
    for booking in bookings:
        service_name = service_names_by_booking.get(booking.id)
        service_matches = service_name is not None and service_name in lowered
        date_matches = parsed_hint is not None and booking.slot_start.date() == parsed_hint.date()
        if service_matches and (date_matches or parsed_hint is None):
            matches.append(booking)
        elif date_matches and not any_service_hint:
            matches.append(booking)
    return matches[0] if len(matches) == 1 else None


async def _direct_pending_booking_reference_reply(
    session: AsyncSession, business: Business, customer, message_text: str
) -> str | None:
    """If the customer loosely refers to an already-created pending booking
    ("the Friday haircut"), don't let the LLM turn that into a new booking."""
    lowered = message_text.lower()
    if any(word in lowered for word in ("book", "booking", "appointment", "schedule", "reschedule", "cancel")):
        return None

    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    pending_bookings = [b for b in bookings if b.status == BookingStatus.PENDING_DEPOSIT]
    for booking in pending_bookings:
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        if service is None:
            continue
        if service.name.lower() not in lowered:
            continue
        if _extract_date_text(message_text) is None and not _looks_like_weekday_reference(lowered):
            continue
        return (
            f"Your {service.name} booking on {booking.slot_start:%d %b at %H:%M} is still "
            "pending deposit confirmation. If you've paid, I'll update you here once "
            "M-Pesa confirms it."
        )
    return None


def _looks_like_weekday_reference(lowered_text: str) -> bool:
    return any(
        word in lowered_text
        for word in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "fraiday",
            "saturday",
            "sunday",
            "tomorrow",
            "tommorrow",
            "today",
        )
    )


def _direct_location_reply(business: Business, message_text: str) -> str | None:
    lowered = message_text.lower()
    if any(phrase in lowered for phrase in ("where located", "where are you", "where is your", "your address", "your location", "how do i get there", "directions to")):
        if business.address_text:
            return f"We are located at: {business.address_text}. Let us know if you'd like to book an appointment or ask about our services!"
        return f"We are located at {business.name}! Let us know if you'd like to book an appointment or ask about our services."
    return None


def _direct_hours_reply(business: Business, message_text: str) -> str | None:
    lowered = message_text.lower()
    if any(phrase in lowered for phrase in ("operating hours", "working hours", "opening hours", "open on", "what time do you open", "what time do you close")):
        hours = json.loads(business.hours_json or "{}")
        formatted = hours_mod.format_hours(hours)
        return f"Our operating hours are:\n{formatted}\n\nLet us know when you'd like to visit!"
    return None


def _direct_payment_methods_reply(business: Business, message_text: str) -> str | None:
    lowered = message_text.lower()
    if any(phrase in lowered for phrase in ("accept mpesa", "accept m-pesa", "take mpesa", "take m-pesa", "pay cash", "take cash", "accept cash", "payment methods", "how do i pay")):
        return "We accept M-Pesa for deposit payments and M-Pesa or cash on arrival!"
    return None


async def _direct_catalog_availability_reply(
    session: AsyncSession, business: Business, message_text: str
) -> str | None:
    """Answer obvious "do you offer X?" questions without asking the LLM to
    choose between services/products. This keeps a services shop from falling
    into the goods/product-list response when the requested service is absent."""
    if _UNLISTED_CATALOG_RE.search(message_text):
        return await _compact_catalog_text(session, business)

    item = _extract_catalog_item_question(message_text)
    if not item:
        return None

    if business.business_type == BusinessType.SERVICES:
        services = await repo.list_services(session, business.id)
        match = _find_named_item(item, [s.name for s in services])
        if match:
            service = next(s for s in services if s.name == match)
            return (
                f"Yes, we offer {service.name} for KES {service.price} "
                f"({service.duration_minutes} min). Would you like to book it?"
            )
        return (
            f"We don't currently list {item}. "
            f"{await _list_services_text(session, business)}"
        )

    products = await repo.list_products(session, business.id)
    match = _find_named_item(item, [p.name for p in products])
    if match:
        product = next(p for p in products if p.name == match)
        if product.stock_qty <= 0:
            return f"We do list {product.name}, but it's currently out of stock."
        return f"Yes, we have {product.name} for KES {product.price}. How many would you like?"
    return (
        f"We don't currently list {item}. "
        f"{await _list_products_text(session, business)}"
    )


def _extract_catalog_item_question(message_text: str) -> str | None:
    text = message_text.strip()
    lowered = text.lower()
    if "what" in lowered and ("offer" in lowered or "have" in lowered or "available" in lowered):
        return None

    match = _OFFER_RE.search(text) or _AVAILABLE_RE.search(text) or _PRICE_ITEM_RE.search(text)
    if not match:
        return None
    item = match.group("item").strip(" ?.!,")
    noise = ("a ", "an ", "the ")
    for prefix in noise:
        if item.lower().startswith(prefix):
            item = item[len(prefix):]
            break
    return item or None


def _find_named_item(requested: str, names: list[str]) -> str | None:
    requested_lower = requested.strip().lower()
    for name in names:
        if name.strip().lower() == requested_lower:
            return name
    return None


def _deterministic_intent(
    message_text: str, stage: str, pending: dict, business: Business
) -> ai.Intent | None:
    if _CODE_REQUEST_RE.search(message_text):
        return ai.Intent(
            type=ai.IntentType.OFF_TOPIC,
            entities={},
            reply_text=f"I'm the virtual assistant for {business.name}! I can only assist with our listed services, products, bookings, and operating hours.",
        )
    if _UNGROUNDED_INFO_RE.search(message_text):
        return ai.Intent(
            type=ai.IntentType.OUT_OF_SCOPE,
            entities={},
            conversation_act=ai.ConversationAct.HUMAN_REQUEST,
            authority_route=ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED,
        )

    if stage not in _ACTIVE_DETAIL_STAGES or pending.get("type") not in _ACTIVE_DETAIL_TYPES:
        return None

    entities = _extract_active_detail_entities(message_text, pending, business)
    if not entities:
        return None

    ptype = pending.get("type")
    if ptype == "order":
        return ai.Intent(type=ai.IntentType.BUY_PRODUCT, entities=entities)

    if ptype in ("booking", "reschedule_booking", "booking_time_retry"):
        if "date_text" not in entities or "time_text" not in entities:
            return None

    return ai.Intent(type=ai.IntentType.BOOK_SERVICE, entities=entities)


def _extract_active_detail_entities(
    message_text: str, pending: dict, business: Business
) -> dict:
    entities: dict = {}
    ptype = pending.get("type")
    if ptype == "order":
        lowered = message_text.strip().lower()
        if lowered in ("delivery", "deliver"):
            entities["fulfillment_type"] = "delivery"
        elif lowered in ("pickup", "pick up", "store pickup"):
            entities["fulfillment_type"] = "pickup"
        elif lowered.isdigit():
            entities["quantity"] = int(lowered)
        else:
            if pending.get("fulfillment_type") == "delivery" and not pending.get("delivery_address"):
                entities["delivery_address"] = message_text.strip()
    else:
        date_text = _extract_date_text(message_text, pending)
        time_text = _extract_time_text(message_text, pending, business)
        if date_text:
            entities["date_text"] = date_text
        if time_text:
            entities["time_text"] = time_text
    return entities


_OWNER_AUTHORITY_ACTS = {
    ai.ConversationAct.COMPLAINT,
    ai.ConversationAct.HUMAN_REQUEST,
    ai.ConversationAct.PROPOSAL,
}


async def _pre_route_conversation_act(
    session: AsyncSession,
    business: Business,
    customer,
    customer_phone: str,
    message_text: str,
    intent: ai.Intent,
    stage: str,
    pending: dict,
) -> tuple[str, str, dict] | None:
    act = intent.conversation_act

    if (
        intent.authority_route == ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED
        or act in _OWNER_AUTHORITY_ACTS
    ):
        if _extract_catalog_item_question(message_text) is not None:
            direct_reply = await _direct_catalog_availability_reply(session, business, message_text)
            if direct_reply is not None:
                return direct_reply, stage, pending

        await owner_workflow.notify_owner_unanswered_question(
            business, customer_phone, message_text, customer_name=customer.name
        )
        return "I've passed this to the team. They'll get back to you soon.", stage, pending

    if intent.authority_route == ai.AuthorityRoute.UNCLEAR or act == ai.ConversationAct.UNCLEAR:
        return "Could you clarify what you'd like help with?", stage, pending

    if act == ai.ConversationAct.UNCERTAIN_ATTENDANCE:
        return await _uncertain_attendance_reply(session, business, customer, message_text, intent)

    if act in (ai.ConversationAct.ACKNOWLEDGEMENT, ai.ConversationAct.CLOSING):
        if intent.type in (ai.IntentType.CONFIRM_ACTION, ai.IntentType.CANCEL_ACTION):
            return None
        if stage != STAGE_IDLE or pending:
            return "No problem. Reply YES to confirm, or tell me what to change.", stage, pending
        if act == ai.ConversationAct.CLOSING:
            return "No problem. Message us anytime.", STAGE_IDLE, {}
        return "You're welcome.", STAGE_IDLE, {}

    return None


async def _uncertain_attendance_reply(
    session: AsyncSession,
    business: Business,
    customer,
    message_text: str,
    intent: ai.Intent,
) -> tuple[str, str, dict]:
    if business.business_type != BusinessType.SERVICES:
        return "Do you have an order you'd like help with?", STAGE_IDLE, {}

    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    date_text = (intent.entities or {}).get("date_text") or _extract_date_text(message_text)
    parsed_date = _parse_date_text(date_text) if date_text else None
    if parsed_date is not None:
        bookings = [b for b in bookings if b.slot_start.date() == parsed_date.date()]

    if not bookings:
        return "Do you have a booking you'd like to cancel or reschedule?", STAGE_IDLE, {}

    if len(bookings) == 1:
        booking = bookings[0]
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        service_name = service.name if service else "your booking"
        return (
            f"Would you like to cancel or reschedule your {service_name} on "
            f"{booking.slot_start:%d %b at %H:%M}?",
            STAGE_IDLE,
            {},
        )

    lines = ["Which booking do you mean? You can say cancel or reschedule."]
    for i, booking in enumerate(bookings, start=1):
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        service_name = service.name if service else "a service"
        lines.append(f"{i}. {service_name} on {booking.slot_start:%d %b at %H:%M}")
    return "\n".join(lines), STAGE_IDLE, {}


def _extract_date_text(message_text: str, pending: dict | None = None) -> str | None:
    text = message_text.strip()
    lowered = text.lower()
    if "same day" in lowered:
        if pending and pending.get("date_text"):
            return pending["date_text"]
        return None
    if "fraiday" in lowered:
        return "friday"
    if "tommorrow" in lowered:
        return "tomorrow"
    if "tomorrow" in lowered:
        return "tomorrow"
    if "tonight" in lowered:
        return "today"
    weekday_match = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
        lowered,
    )
    if weekday_match:
        return weekday_match.group(1)
    explicit_date = re.search(
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        lowered,
    )
    if explicit_date:
        return explicit_date.group(0)
    month_first = re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
        r"\d{1,2}(?:st|nd|rd|th)?\b",
        lowered,
    )
    if month_first:
        return month_first.group(0)
    return None


def _extract_time_text(message_text: str, pending: dict, business: Business) -> str | None:
    text = message_text.strip()

    match = _TIME_WITH_MERIDIEM_RE.search(text)
    if match:
        period = match.group("period").lower().replace(".", "")
        return f"{int(match.group('hour'))}:{match.group('minute') or '00'}{period}"

    match = _TIME_WITH_DAYPART_RE.search(text)
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        period = match.group("period").lower()
        if period == "morning":
            hour = 0 if hour == 12 else hour
        elif period in {"afternoon", "evening", "night"} and hour < 12:
            hour += 12
        return f"{hour:02d}:{minute:02d}"

    match = _TIME_24H_RE.match(text)
    if match:
        return f"{int(match.group('hour')):02d}:{match.group('minute')}"

    return _infer_bare_time_text(text, pending, business)


def _infer_bare_time_text(message_text: str, pending: dict, business: Business) -> str | None:
    match = _BARE_TIME_RE.match(message_text)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if hour > 23:
        return None
    if hour == 0 or hour > 12:
        return f"{hour:02d}:{minute:02d}"

    date_part = _parse_date_text(pending.get("date_text") or "")
    hours = json.loads(business.hours_json or "{}")
    if date_part is not None and hours and not all(v is None for v in hours.values()):
        candidates = [hour]
        if hour < 12:
            candidates.append(hour + 12)
        valid = []
        for candidate_hour in candidates:
            slot_start = date_part.replace(hour=candidate_hour, minute=minute)
            slot_end = slot_start + timedelta(minutes=1)
            ok, _ = hours_mod.is_within_hours(hours, slot_start, slot_end)
            if ok:
                valid.append(candidate_hour)
        if len(valid) == 1:
            return f"{valid[0]:02d}:{minute:02d}"

    # Human shorthand in booking contexts usually means daytime business
    # hours: "5" after a date prompt is much more likely to be 5pm than 5am.
    if 1 <= hour <= 7:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


async def _dispatch(
    session, business, customer, customer_phone, message_text, intent, stage, pending, mpesa_callback_secret
) -> tuple[str, str, dict]:
    """Returns (reply_text, new_stage, new_pending)."""

    if intent.type == ai.IntentType.OFF_TOPIC:
        reply = (
            intent.reply_text
            or f"I'm the virtual assistant for {business.name}! I can only assist with our listed services, products, bookings, and operating hours. How can I help you with your visit today?"
        )
        return reply, stage, pending

    if intent.type == ai.IntentType.OUT_OF_SCOPE:
        await owner_workflow.notify_owner_unanswered_question(
            business, customer_phone, message_text, customer_name=customer.name
        )
        reply = (
            intent.reply_text
            or "That's a great question for the team directly - I've passed it along and they'll get back to you soon."
        )
        return reply, stage, pending  # topic switch: leave any in-progress flow untouched

    if business.business_type == BusinessType.SERVICES:
        if intent.type == ai.IntentType.LIST_PRODUCTS:
            hdr = f"We don't sell physical products, but here are the services {business.name} offers:"
            reply = await _list_services_text(session, business, header=hdr)
            return reply, stage, pending
        if intent.type == ai.IntentType.LIST_SERVICES:
            reply = await _list_services_text(session, business)
            return reply, stage, pending
        if intent.type == ai.IntentType.BUY_PRODUCT:
            entities = dict(intent.entities)
            if entities.get("product_name") and not entities.get("service_name"):
                entities["service_name"] = entities["product_name"]
            return await _advance_booking(session, business, pending, entities)

    if business.business_type == BusinessType.GOODS:
        if intent.type == ai.IntentType.LIST_SERVICES:
            hdr = f"We don't offer service appointments, but we sell quality products! Here's what {business.name} has available:"
            reply = await _list_products_text(session, business, header=hdr)
            return reply, stage, pending
        if intent.type == ai.IntentType.LIST_PRODUCTS:
            reply = await _list_products_text(session, business)
            return reply, stage, pending
        if intent.type == ai.IntentType.BOOK_SERVICE:
            entities = dict(intent.entities)
            if entities.get("service_name") and not entities.get("product_name"):
                entities["product_name"] = entities["service_name"]
            return await _advance_order(session, business, pending, entities)

    if intent.type == ai.IntentType.LIST_SERVICES:
        reply = await _list_services_text(session, business)
        return reply, stage, pending
    if intent.type == ai.IntentType.LIST_PRODUCTS:
        reply = await _list_products_text(session, business)
        return reply, stage, pending
    if intent.type == ai.IntentType.CHECK_STATUS:
        return await _check_status_text(session, business, customer), stage, pending
    if intent.type == ai.IntentType.ASK_INFO:
        if intent.reply_text:
            return intent.reply_text, stage, pending
        return await _grounded_info_reply(
            session, business, customer_phone, message_text
        ), stage, pending

    if intent.type == ai.IntentType.BOOK_SERVICE:
        if pending.get("type") == "reschedule_booking":
            return await _advance_reschedule(session, business, pending, intent.entities)
        if pending.get("type") == "booking_time_retry":
            return await _advance_booking_time_retry(session, business, pending, intent.entities)
        return await _advance_booking(session, business, pending, intent.entities)

    if intent.type == ai.IntentType.BUY_PRODUCT:
        return await _advance_order(session, business, pending, intent.entities)

    if intent.type == ai.IntentType.CANCEL_BOOKING:
        return await _start_cancel_booking(session, business, customer)

    if intent.type == ai.IntentType.CANCEL_ORDER:
        return await _start_cancel_order(session, business, customer)

    if intent.type == ai.IntentType.RESCHEDULE_BOOKING:
        if business.business_type != BusinessType.SERVICES:
            return "Rescheduling isn't available for orders - please contact us directly.", STAGE_IDLE, {}
        return await _start_reschedule_booking(session, business, customer)

    digits_in_msg = "".join(c for c in message_text if c.isdigit())
    if intent.type == ai.IntentType.CONFIRM_ACTION or (stage == STAGE_CONFIRMING and len(digits_in_msg) >= 9):
        if stage == STAGE_CONFIRMING and pending:
            if len(digits_in_msg) >= 9:
                pending["payment_phone"] = digits_in_msg
            reply = await _finalize_pending_action(
                session, business, customer, customer_phone, pending, mpesa_callback_secret
            )
            return reply, STAGE_IDLE, {}
        return intent.reply_text or "Sure - what would you like to confirm?", stage, pending

    if intent.type == ai.IntentType.CANCEL_ACTION:
        if pending.get("type") in ("cancel_booking", "cancel_order"):
            return "Okay, I won't cancel it after all.", STAGE_IDLE, {}
        if pending:
            return "No problem, I've cancelled that request - let me know if you'd like to start over.", STAGE_IDLE, {}
        reply = intent.reply_text or "No problem at all! Feel free to reach out whenever you need anything. Have a great day!"
        return reply, STAGE_IDLE, {}

    # FALLBACK or anything unrecognized
    return intent.reply_text or ai.FALLBACK_INTENT.reply_text, stage, pending


async def _handle_selection(session, business, stage, pending, index: int) -> tuple[str, str, dict]:
    candidates = pending.get("candidates", [])
    if not (1 <= index <= len(candidates)):
        return (
            "Please reply with one of the numbers listed, or tell me if you'd like to do something else.",
            stage,
            pending,
        )
    selected_id = candidates[index - 1]
    purpose = pending.get("purpose")

    if purpose == "cancel_booking":
        booking = await repo.get_booking_for_business(session, business.id, selected_id)
        if booking is None:
            return "Sorry, I couldn't find that one anymore.", STAGE_IDLE, {}
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        service_name = service.name if service else "your service"
        new_pending = {"type": "cancel_booking", "booking_id": selected_id}
        reply = f"Reply YES to cancel your {service_name} on {booking.slot_start:%d %b at %H:%M}."
        return reply, STAGE_CONFIRMING, new_pending

    if purpose == "reschedule_booking":
        booking = await repo.get_booking_for_business(session, business.id, selected_id)
        if booking is None:
            return "Sorry, I couldn't find that one anymore.", STAGE_IDLE, {}
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        service_name = service.name if service else "your service"
        new_pending = {"type": "reschedule_booking", "booking_id": selected_id}
        reply = (
            f"Okay - rescheduling your {service_name} (currently {booking.slot_start:%d %b at %H:%M}). "
            "What date and time would you like instead?"
        )
        return reply, STAGE_COLLECTING_RESCHEDULE, new_pending

    if purpose == "cancel_order":
        order = await repo.get_order_for_business(session, business.id, selected_id)
        if order is None:
            return "Sorry, I couldn't find that one anymore.", STAGE_IDLE, {}
        new_pending = {"type": "cancel_order", "order_id": selected_id}
        reply = f"Reply YES to cancel order O{order.id}."
        return reply, STAGE_CONFIRMING, new_pending

    return "Sorry, something went wrong - let's start over.", STAGE_IDLE, {}


async def _build_catalog_summary(session: AsyncSession, business: Business) -> list[dict]:
    if business.business_type == BusinessType.SERVICES:
        services = await repo.list_services(session, business.id)
        return [
            {"name": s.name, "price": float(s.price), "duration_minutes": s.duration_minutes}
            for s in services
        ]
    products = await repo.list_products(session, business.id)
    return [{"name": p.name, "price": float(p.price), "in_stock": p.stock_qty > 0} for p in products]


def _fmt_price(amount: float) -> str:
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


async def _list_services_text(session: AsyncSession, business: Business, header: str | None = None) -> str:
    services = await repo.list_services(session, business.id)
    if not services:
        return f"{business.name} hasn't listed any services yet - please check back soon."
    intro = header or f"Here's what {business.name} offers:"
    lines = [intro]
    for i, s in enumerate(services, start=1):
        lines.append(f"- {s.name}: KES {_fmt_price(s.price)} ({s.duration_minutes} min)")
    lines.append("\nJust tell me which one and when you'd like to come in.")
    return "\n".join(lines)


async def _list_products_text(session: AsyncSession, business: Business, header: str | None = None) -> str:
    products = await repo.list_products(session, business.id)
    if not products:
        return f"{business.name} hasn't listed any products yet - please check back soon."
    intro = header or f"Here's what {business.name} has available:"
    lines = [intro]
    for i, p in enumerate(products, start=1):
        stock_note = "" if p.stock_qty > 0 else " (out of stock)"
        lines.append(f"- {p.name}: KES {_fmt_price(p.price)}{stock_note}")
    lines.append("\nJust tell me which one and how many you'd like.")
    return "\n".join(lines)


async def _compact_catalog_text(session: AsyncSession, business: Business) -> str:
    if business.business_type == BusinessType.SERVICES:
        services = await repo.list_services(session, business.id)
        if not services:
            return "We don't have any services listed right now."
        names = ", ".join(s.name for s in services)
        return f"These are the services we currently offer: {names}."

    products = await repo.list_products(session, business.id)
    if not products:
        return "We don't have any products listed right now."
    names = ", ".join(p.name for p in products)
    return f"These are the products we currently have listed: {names}."


async def _grounded_info_reply(
    session: AsyncSession, business: Business, customer_phone: str, message_text: str
) -> str:
    lowered = message_text.lower()

    if any(word in lowered for word in ("where", "located", "location", "address", "directions", "find")):
        if business.address_text:
            reply = f"We are located at: {business.address_text}."
            if business.extra_info_text:
                reply += f"\n\nAdditional Info: {business.extra_info_text}"
            return reply

    if any(word in lowered for word in ("hour", "open", "close", "closed", "closing")):
        hours = json.loads(business.hours_json or "{}")
        return f"Our hours are: {hours_mod.format_hours(hours)}"

    if any(word in lowered for word in ("service", "offer", "available", "do you do")):
        if business.business_type == BusinessType.SERVICES:
            return await _list_services_text(session, business)
        hdr = f"We don't offer service appointments, but we sell quality products! Here's what {business.name} has available:"
        return await _list_products_text(session, business, header=hdr)

    if any(word in lowered for word in ("product", "goods", "sell", "stock")):
        if business.business_type == BusinessType.GOODS:
            return await _list_products_text(session, business)
        hdr = f"We don't sell physical products, but here are the services {business.name} offers:"
        return await _list_services_text(session, business, header=hdr)

    if any(word in lowered for word in ("price", "cost", "how much")):
        if business.business_type == BusinessType.SERVICES:
            services = await repo.list_services(session, business.id)
            match = _find_named_item(message_text, [s.name for s in services])
            if match:
                service = next(s for s in services if s.name == match)
                return f"{service.name} is KES {service.price} ({service.duration_minutes} min)."
        else:
            products = await repo.list_products(session, business.id)
            match = _find_named_item(message_text, [p.name for p in products])
            if match:
                product = next(p for p in products if p.name == match)
                stock_note = "currently in stock" if product.stock_qty > 0 else "currently out of stock"
                return f"{product.name} is KES {product.price} and is {stock_note}."

    await owner_workflow.notify_owner_unanswered_question(
        business, customer_phone, message_text, customer_name=customer.name
    )
    return (
        "I don't have that information listed here, so I've passed your question "
        "to the team and they'll get back to you soon."
    )


async def _check_status_text(session: AsyncSession, business: Business, customer) -> str:
    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    orders = await repo.list_upcoming_orders_for_customer(session, business.id, customer.id)

    if not bookings and not orders:
        return "You don't have any upcoming bookings or orders right now."

    lines = []
    if bookings:
        lines.append("Upcoming bookings:")
        for b in bookings:
            service = await repo.get_service_for_business(session, business.id, b.service_id)
            name = service.name if service else "a service"
            lines.append(f"- {name} on {b.slot_start:%d %b at %H:%M} ({b.status.value.replace('_', ' ')})")
    if orders:
        lines.append("Recent orders:")
        for o in orders:
            summary = await _order_summary_text(session, business, o)
            lines.append(f"- {summary} ({o.status.value.replace('_', ' ')})")
    return "\n".join(lines)


def _merge_entities(pending: dict, entities: dict, keys: list[str]) -> dict:
    """Only overwrite a field if the LLM actually returned something for it
    this turn - preserves anything already known that wasn't repeated."""
    merged = dict(pending)
    for key in keys:
        value = entities.get(key)
        if value not in (None, "", []):
            merged[key] = value
    return merged


def _validate_slot(business: Business, slot_start: datetime, slot_end: datetime) -> str | None:
    """Returns an error message if the slot is invalid (already in the
    past, or outside business hours), else None. Used both when first
    quoting a slot back to the customer AND again right before actually
    confirming/rescheduling (defense in depth - hours don't change often,
    but "is this still in the future" can, if enough time passes between
    the quote and the customer's YES)."""
    if slot_start < datetime.now():
        return "That time's already passed - what other time works for you?"
    hours = json.loads(business.hours_json or "{}")
    ok, msg = hours_mod.is_within_hours(hours, slot_start, slot_end)
    return None if ok else msg


async def _advance_booking(session: AsyncSession, business: Business, pending: dict, entities: dict):
    pending = _merge_entities(pending, entities, ["service_name", "date_text", "time_text", "payment_phone"])
    pending["type"] = "booking"

    services = await repo.list_services(session, business.id)
    service = None
    if pending.get("service_id"):
        service = next((s for s in services if s.id == pending["service_id"]), None)
    if service is None and pending.get("service_name"):
        name = pending["service_name"].strip().lower()
        service = next((s for s in services if s.name.strip().lower() == name), None)
        if service is None:
            service = next((s for s in services if name in s.name.lower()), None)
        if service is not None:
            pending["service_id"] = service.id

    if service is None:
        if pending.get("service_name"):
            reply = "I couldn't find that service. " + await _list_services_text(session, business)
        else:
            reply = "Sure - which service would you like to book?"
        return reply, STAGE_COLLECTING_BOOKING, pending

    date_text = pending.get("date_text")
    time_text = pending.get("time_text")

    if not date_text and not time_text:
        return (
            f"Great choice - {service.name} (KES {service.price}, {service.duration_minutes} min). "
            "What date and time would you like to come in?",
            STAGE_COLLECTING_BOOKING,
            pending,
        )

    if date_text and not time_text:
        parsed_date = _parse_date_text(date_text)
        if parsed_date is None:
            pending["date_text"] = None
            return (
                "Sorry, I couldn't quite work out that date - could you say it a bit "
                "more plainly, e.g. 'Thursday' or '25 August'?",
                STAGE_COLLECTING_BOOKING,
                pending,
            )
        return (
            f"Got it, {parsed_date:%A %d %b} - what time works for you?",
            STAGE_COLLECTING_BOOKING,
            pending,
        )

    if time_text and not date_text:
        return (
            "And what date would that be?",
            STAGE_COLLECTING_BOOKING,
            pending,
        )

    slot_start = _combine_date_and_time(date_text, time_text)
    if slot_start is None:
        pending["date_text"] = None
        pending["time_text"] = None
        return (
            "Sorry, I couldn't quite work out that date/time - could you say it a bit "
            "more plainly, e.g. 'Thursday at 2pm'?",
            STAGE_COLLECTING_BOOKING,
            pending,
        )

    slot_end = slot_start + timedelta(minutes=service.duration_minutes)
    error = _validate_slot(business, slot_start, slot_end)
    if error:
        pending["time_text"] = None
        return error, STAGE_COLLECTING_BOOKING, pending

    deposit_amount = payments.compute_deposit_amount(business, float(service.price), item=service)
    pending["slot_start_iso"] = slot_start.isoformat()
    if deposit_amount > 0 and business.mpesa_shortcode:
        phone_hint = f" to {pending['payment_phone']}" if pending.get("payment_phone") else ""
        deposit_text = f", KES {_fmt_price(deposit_amount)} deposit.\nReply YES to send the M-Pesa prompt{phone_hint}, or reply with a different M-Pesa number (e.g. 0712345678)"
    else:
        deposit_text = ".\nNo upfront deposit required — payment will be collected upon arrival. Reply YES to confirm"

    reply = (
        f"Here's what I have: {service.name} on {slot_start:%A %d %b at %H:%M} "
        f"(KES {_fmt_price(service.price)}, {service.duration_minutes} min)"
        f"{deposit_text}, or let me know if you'd like to change anything."
    )
    return reply, STAGE_CONFIRMING, pending


async def _advance_order(session: AsyncSession, business: Business, pending: dict, entities: dict):
    pending = _merge_entities(pending, entities, ["product_name", "quantity", "fulfillment_type", "delivery_address", "payment_phone"])
    pending["type"] = "order"

    products = await repo.list_products(session, business.id)
    product = None
    if pending.get("product_id"):
        product = next((p for p in products if p.id == pending["product_id"]), None)
    if product is None and pending.get("product_name"):
        name = pending["product_name"].strip().lower()
        product = next((p for p in products if p.name.strip().lower() == name), None)
        if product is None:
            product = next((p for p in products if name in p.name.lower()), None)
        if product is not None:
            pending["product_id"] = product.id

    if product is None:
        if pending.get("product_name"):
            reply = "I couldn't find that product. " + await _list_products_text(session, business)
        else:
            reply = "Sure - which product would you like?"
        return reply, STAGE_COLLECTING_ORDER, pending

    if product.stock_qty <= 0:
        return f"Sorry, {product.name} is currently out of stock.", STAGE_IDLE, {}

    quantity = pending.get("quantity")
    if not quantity:
        return f"How many {product.name} would you like? (KES {product.price} each)", STAGE_COLLECTING_ORDER, pending

    try:
        quantity = int(quantity)
        assert quantity > 0
    except (ValueError, AssertionError, TypeError):
        pending["quantity"] = None
        return "Please give me a number for the quantity, e.g. 2", STAGE_COLLECTING_ORDER, pending

    if quantity > product.stock_qty:
        pending["quantity"] = None
        return f"Only {product.stock_qty} of {product.name} left in stock - how many would you like?", STAGE_COLLECTING_ORDER, pending

    pending["quantity"] = quantity

    # Enforce Business Fulfillment Policy
    f_mode = getattr(business, "fulfillment_mode", None)
    f_mode_val = f_mode.value if f_mode else "both"

    if f_mode_val == "pickup_only":
        pending["fulfillment_type"] = "pickup"
    elif f_mode_val == "delivery_only":
        pending["fulfillment_type"] = "delivery"

    fulfillment_type = pending.get("fulfillment_type")
    if f_mode_val == "both" and fulfillment_type not in ("delivery", "pickup"):
        return (
            f"Would you prefer Delivery or Store Pickup for your order of {quantity} x {product.name}?",
            STAGE_COLLECTING_ORDER,
            pending,
        )

    if pending.get("fulfillment_type") == "delivery" and not pending.get("delivery_address"):
        return (
            f"Please provide your delivery address or landmark for the delivery of {quantity} x {product.name}.",
            STAGE_COLLECTING_ORDER,
            pending,
        )

    total = float(product.price) * quantity
    deposit_amount = payments.compute_deposit_amount(business, total, item=product)
    if deposit_amount > 0 and business.mpesa_shortcode:
        phone_hint = f" to {pending['payment_phone']}" if pending.get("payment_phone") else ""
        deposit_text = f", KES {_fmt_price(deposit_amount)} deposit.\nReply YES to send the M-Pesa prompt{phone_hint}, or reply with a different M-Pesa number (e.g. 0712345678)"
    else:
        deposit_text = ".\nNo upfront deposit required — payment will be collected upon delivery/pickup. Reply YES to confirm"

    if pending.get("fulfillment_type") == "delivery":
        fulfillment_str = f"Delivery to {pending.get('delivery_address')}"
    else:
        loc = business.address_text or "our store"
        fulfillment_str = f"Store Pickup at {loc}"

    reply = (
        f"Here's what I have: {quantity} x {product.name} (KES {_fmt_price(total)} total) — {fulfillment_str}"
        f"{deposit_text}, or let me know if you'd like to change anything."
    )
    return reply, STAGE_CONFIRMING, pending


async def _advance_reschedule(session: AsyncSession, business: Business, pending: dict, entities: dict):
    pending = _merge_entities(pending, entities, ["date_text", "time_text"])
    pending["type"] = "reschedule_booking"

    booking = await repo.get_booking_for_business(session, business.id, pending.get("booking_id"))
    if booking is None or booking.status not in (
        BookingStatus.CONFIRMED,
        BookingStatus.PENDING_DEPOSIT,
        BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION,
    ):
        return "That booking can't be rescheduled right now.", STAGE_IDLE, {}
    service = await repo.get_service_for_business(session, business.id, booking.service_id)
    if service is None:
        return "Something went wrong finding that booking's service.", STAGE_IDLE, {}

    date_text = pending.get("date_text")
    time_text = pending.get("time_text")

    if not date_text and not time_text:
        return "What date and time would you like to move it to?", STAGE_COLLECTING_RESCHEDULE, pending

    if date_text and not time_text:
        parsed_date = _parse_date_text(date_text)
        if parsed_date is None:
            pending["date_text"] = None
            return (
                "Sorry, I couldn't quite work out that date - could you rephrase, e.g. 'Thursday'?",
                STAGE_COLLECTING_RESCHEDULE,
                pending,
            )
        return f"Got it, {parsed_date:%A %d %b} - what time?", STAGE_COLLECTING_RESCHEDULE, pending

    if time_text and not date_text:
        return "And what date would that be?", STAGE_COLLECTING_RESCHEDULE, pending

    new_slot_start = _combine_date_and_time(date_text, time_text)
    if new_slot_start is None:
        pending["date_text"] = None
        pending["time_text"] = None
        return (
            "Sorry, I couldn't quite work out that date/time - could you say it more "
            "plainly, e.g. 'Thursday at 2pm'?",
            STAGE_COLLECTING_RESCHEDULE,
            pending,
        )

    new_slot_end = new_slot_start + timedelta(minutes=service.duration_minutes)
    error = _validate_slot(business, new_slot_start, new_slot_end)
    if error:
        pending["time_text"] = None
        return error, STAGE_COLLECTING_RESCHEDULE, pending

    pending["new_slot_start_iso"] = new_slot_start.isoformat()
    old_display = f"{booking.slot_start:%d %b %Y at %H:%M}"
    new_display = f"{new_slot_start:%d %b %Y at %H:%M}"
    reply = f"Move your {service.name} from {old_display} to {new_display}? Reply YES to confirm."
    return reply, STAGE_CONFIRMING, pending


async def _advance_booking_time_retry(
    session: AsyncSession, business: Business, pending: dict, entities: dict
):
    """Collect a new date/time for an existing booking after owner soft-rejected."""
    pending = _merge_entities(pending, entities, ["date_text", "time_text"])
    pending["type"] = "booking_time_retry"

    service = await repo.get_service_for_business(session, business.id, pending.get("service_id"))
    if service is None:
        return "Something went wrong - please start your booking again.", STAGE_IDLE, {}

    date_text = pending.get("date_text")
    time_text = pending.get("time_text")

    if not date_text and not time_text:
        return "What date and time would you like instead?", STAGE_COLLECTING_TIME_RETRY, pending

    if date_text and not time_text:
        parsed_date = _parse_date_text(date_text)
        if parsed_date is None:
            pending["date_text"] = None
            return (
                "Sorry, I couldn't quite work out that date - could you say it a bit "
                "more plainly, e.g. 'Thursday' or '25 August'?",
                STAGE_COLLECTING_TIME_RETRY,
                pending,
            )
        return (
            f"Got it, {parsed_date:%A %d %b} - what time works for you?",
            STAGE_COLLECTING_TIME_RETRY,
            pending,
        )

    if time_text and not date_text:
        return "And what date would that be?", STAGE_COLLECTING_TIME_RETRY, pending

    slot_start = _combine_date_and_time(date_text, time_text)
    if slot_start is None:
        pending["date_text"] = None
        pending["time_text"] = None
        return (
            "Sorry, I couldn't quite work out that date/time - could you say it a bit "
            "more plainly, e.g. 'Thursday at 2pm'?",
            STAGE_COLLECTING_TIME_RETRY,
            pending,
        )

    slot_end = slot_start + timedelta(minutes=service.duration_minutes)
    error = _validate_slot(business, slot_start, slot_end)
    if error:
        pending["time_text"] = None
        return error, STAGE_COLLECTING_TIME_RETRY, pending

    pending["slot_start_iso"] = slot_start.isoformat()
    service_label = pending.get("service_name") or service.name
    reply = (
        f"Move your {service_label} to {slot_start:%A %d %b at %H:%M}? "
        "Reply YES to send this to the team for confirmation."
    )
    return reply, STAGE_CONFIRMING, pending


async def _start_cancel_booking(session: AsyncSession, business: Business, customer):
    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    if not bookings:
        return "You don't have any upcoming bookings to cancel.", STAGE_IDLE, {}

    if len(bookings) == 1:
        b = bookings[0]
        service = await repo.get_service_for_business(session, business.id, b.service_id)
        service_name = service.name if service else "your service"
        pending = {"type": "cancel_booking", "booking_id": b.id}
        return f"Reply YES to cancel your {service_name} on {b.slot_start:%d %b at %H:%M}.", STAGE_CONFIRMING, pending

    lines = ["Which booking would you like to cancel?"]
    candidates = []
    for i, b in enumerate(bookings, start=1):
        service = await repo.get_service_for_business(session, business.id, b.service_id)
        service_name = service.name if service else "a service"
        lines.append(f"{i}. {service_name} on {b.slot_start:%d %b at %H:%M}")
        candidates.append(b.id)
    pending = {"purpose": "cancel_booking", "candidates": candidates}
    return "\n".join(lines), STAGE_SELECTING_BOOKING, pending


async def _start_reschedule_booking(session: AsyncSession, business: Business, customer):
    all_bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    bookings = [
        b for b in all_bookings
        if b.status in (
            BookingStatus.CONFIRMED,
            BookingStatus.PENDING_DEPOSIT,
            BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION,
        )
    ]
    if not bookings:
        return "You don't have any upcoming bookings to reschedule.", STAGE_IDLE, {}

    if len(bookings) == 1:
        b = bookings[0]
        service = await repo.get_service_for_business(session, business.id, b.service_id)
        service_name = service.name if service else "your service"
        pending = {"type": "reschedule_booking", "booking_id": b.id}
        reply = (
            f"Okay - rescheduling your {service_name} (currently {b.slot_start:%d %b at %H:%M}). "
            "What date and time would you like instead?"
        )
        return reply, STAGE_COLLECTING_RESCHEDULE, pending

    lines = ["Which booking would you like to reschedule?"]
    candidates = []
    for i, b in enumerate(bookings, start=1):
        service = await repo.get_service_for_business(session, business.id, b.service_id)
        service_name = service.name if service else "a service"
        lines.append(f"{i}. {service_name} on {b.slot_start:%d %b at %H:%M}")
        candidates.append(b.id)
    pending = {"purpose": "reschedule_booking", "candidates": candidates}
    return "\n".join(lines), STAGE_SELECTING_BOOKING, pending


async def _start_cancel_order(session: AsyncSession, business: Business, customer):
    orders = await repo.list_upcoming_orders_for_customer(session, business.id, customer.id)
    if not orders:
        return "You don't have any active orders to cancel.", STAGE_IDLE, {}

    if len(orders) == 1:
        o = orders[0]
        summary = await _order_summary_text(session, business, o)
        pending = {"type": "cancel_order", "order_id": o.id}
        return f"Reply YES to cancel order O{o.id} ({summary}).", STAGE_CONFIRMING, pending

    lines = ["Which order would you like to cancel?"]
    candidates = []
    for i, o in enumerate(orders, start=1):
        summary = await _order_summary_text(session, business, o)
        lines.append(f"{i}. O{o.id}: {summary}")
        candidates.append(o.id)
    pending = {"purpose": "cancel_order", "candidates": candidates}
    return "\n".join(lines), STAGE_SELECTING_ORDER, pending


async def _order_summary_text(session: AsyncSession, business: Business, order) -> str:
    items = json.loads(order.items_json)
    parts = []
    for item in items:
        product = await repo.get_product_for_business(session, business.id, item["product_id"])
        name = product.name if product else "item"
        parts.append(f"{item['qty']} x {name}")
    return ", ".join(parts) if parts else f"Order O{order.id}"


async def _finalize_pending_action(
    session: AsyncSession, business: Business, customer, customer_phone: str, pending: dict, mpesa_callback_secret: str
) -> str:
    ptype = pending.get("type")
    if ptype == "booking":
        return await _finalize_booking(session, business, customer, customer_phone, pending, mpesa_callback_secret)
    if ptype == "order":
        return await _finalize_order(session, business, customer, customer_phone, pending, mpesa_callback_secret)
    if ptype == "cancel_booking":
        return await _finalize_cancel_booking(session, business, customer_phone, pending)
    if ptype == "cancel_order":
        return await _finalize_cancel_order(session, business, customer_phone, pending)
    if ptype == "reschedule_booking":
        return await _finalize_reschedule_booking(session, business, customer_phone, pending)
    if ptype == "booking_time_retry":
        return await _finalize_booking_time_retry(session, business, customer_phone, pending)
    if ptype == "resend_deposit":
        return await _finalize_resend_deposit(session, business, customer, customer_phone, pending, mpesa_callback_secret)
    return "Sorry, I lost track of what we were confirming - could you start again?"


async def _finalize_resend_deposit(
    session: AsyncSession, business: Business, customer, customer_phone: str, pending: dict, mpesa_callback_secret: str
) -> str:
    target_phone = pending.get("payment_phone") or customer_phone
    amount = float(pending.get("deposit_amount", 0))
    item_name = pending.get("item_name", "your request")

    payment = await payments.initiate_deposit(
        session, business, customer_phone, amount, mpesa_callback_secret, payment_phone=target_phone
    )
    if pending.get("booking_id"):
        booking = await repo.get_booking_for_business(session, business.id, pending["booking_id"])
        if booking:
            booking.payment_id = payment.id
            await session.flush()
    elif pending.get("order_id"):
        order = await repo.get_order_for_business(session, business.id, pending["order_id"])
        if order:
            order.payment_id = payment.id
            await session.flush()

    target_msg = f" (sent to {target_phone})" if target_phone != customer_phone else ""
    return (
        f"I've sent a new M-Pesa prompt for KES {_fmt_price(amount)} ({item_name}). "
        f"Check your phone{target_msg} and enter your PIN to confirm!"
    )


async def _finalize_booking(session, business, customer, customer_phone, pending, mpesa_callback_secret) -> str:
    service = await repo.get_service_for_business(session, business.id, pending.get("service_id"))
    if service is None or not pending.get("slot_start_iso"):
        return "Something went wrong with your booking - please start again by naming the service."

    slot_start = datetime.fromisoformat(pending["slot_start_iso"])
    slot_end = slot_start + timedelta(minutes=service.duration_minutes)

    # Defense in depth: re-validate right before creating, in case enough
    # time passed since the quote that the slot is now in the past (hours
    # changing between quote and confirm is rarer, but cheap to re-check too).
    error = _validate_slot(business, slot_start, slot_end)
    if error:
        return error + " Please tell me a new date and time."

    deposit_amount = payments.compute_deposit_amount(business, float(service.price), item=service)
    skip_conflict = _is_manual(business)

    try:
        booking = await repo.create_booking(
            session,
            business.id,
            customer.id,
            service.id,
            slot_start,
            slot_end,
            deposit_amount,
            skip_conflict_check=skip_conflict,
        )
    except BookingConflictError:
        return "That slot just got booked by someone else - what other time works for you?"

    await owner_workflow.notify_owner_new_booking_request(
        business,
        booking.id,
        service.name,
        f"{slot_start:%d %b %Y at %H:%M}",
        customer_phone,
        deposit_amount=deposit_amount,
        customer_name=customer.name,
    )

    if deposit_amount <= 0 or not business.mpesa_shortcode:
        if _is_manual(business):
            booking.status = BookingStatus.AWAITING_OWNER_CONFIRMATION
            await session.flush()
            return (
                f"Booked {service.name} on {slot_start:%d %b %Y at %H:%M}! "
                f"Your request has been sent to the team for confirmation. "
                f"Payment of KES {_fmt_price(service.price)} will be collected upon arrival."
            )
        booking.status = BookingStatus.CONFIRMED
        await session.flush()
        return (
            f"Booked! Your appointment for {service.name} on {slot_start:%d %b %Y at %H:%M} "
            f"is confirmed. Payment of KES {_fmt_price(service.price)} will be collected upon arrival."
        )

    payment = await payments.initiate_deposit(
        session, business, customer_phone, deposit_amount, mpesa_callback_secret, payment_phone=pending.get("payment_phone")
    )
    booking.payment_id = payment.id
    await session.flush()

    target_msg = f" (sent to {pending.get('payment_phone')})" if pending.get("payment_phone") else ""
    return (
        f"Booked {service.name} on {slot_start:%d %b %Y at %H:%M}, pending a KES "
        f"{_fmt_price(deposit_amount)} deposit. Check your phone{target_msg} for the M-Pesa prompt to confirm."
    )


async def _finalize_booking_time_retry(
    session: AsyncSession, business: Business, customer_phone: str, pending: dict
) -> str:
    """Customer picked a new time after owner soft-rejected (manual mode).

    Same booking, deposit already paid - no second STK push."""
    booking = await repo.get_booking_for_business(session, business.id, pending.get("booking_id"))
    if booking is None or booking.status != BookingStatus.AWAITING_OWNER_CONFIRMATION:
        return "I couldn't find that booking anymore - please start again."

    service = await repo.get_service_for_business(session, business.id, booking.service_id)
    if service is None or not pending.get("slot_start_iso"):
        return "Something went wrong - please tell me the new date and time again."

    slot_start = datetime.fromisoformat(pending["slot_start_iso"])
    slot_end = slot_start + timedelta(minutes=service.duration_minutes)
    error = _validate_slot(business, slot_start, slot_end)
    if error:
        return error + " Please tell me a new date and time."

    await repo.update_booking_slot(session, business.id, booking.id, slot_start, slot_end)
    await owner_workflow.notify_owner_booking_time_change_request(
        business,
        booking.id,
        service.name,
        f"{slot_start:%d %b %Y at %H:%M}",
        customer_phone,
        context="initial_retry",
        customer_name=booking.customer.name if getattr(booking, "customer", None) else None,
    )
    return (
        f"Thanks - I've sent your new time ({slot_start:%d %b %Y at %H:%M}) to the team "
        "for confirmation. Your deposit is still on this booking - we'll message you shortly."
    )


async def _finalize_order(session, business, customer, customer_phone, pending, mpesa_callback_secret) -> str:
    product = await repo.get_product_for_business(session, business.id, pending.get("product_id"))
    quantity = pending.get("quantity")
    if product is None or not quantity:
        return "Something went wrong with your order - please start again by naming the product."
    if quantity > product.stock_qty:
        return f"Only {product.stock_qty} of {product.name} left now - how many would you like instead?"

    total = float(product.price) * quantity
    deposit_amount = payments.compute_deposit_amount(business, total, item=product)
    items_json = json.dumps([{"product_id": product.id, "qty": quantity, "unit_price": float(product.price)}])

    if pending.get("fulfillment_type") == "delivery":
        fulfillment_summary = f"[Fulfillment: Delivery to {pending.get('delivery_address')}]"
        cust_fulfillment = f"Delivery to {pending.get('delivery_address')}"
    else:
        loc = business.address_text or "our store"
        fulfillment_summary = f"[Fulfillment: Store Pickup at {loc}]"
        cust_fulfillment = f"Store Pickup at {loc}"

    order_summary = f"{quantity} x {product.name} (KES {_fmt_price(total)}) {fulfillment_summary}"

    order = await repo.create_order(session, business.id, customer.id, items_json, total, deposit_amount)
    await owner_workflow.notify_owner_new_order_request(
        business,
        order.id,
        order_summary,
        customer_phone,
        deposit_amount=deposit_amount,
        customer_name=customer.name,
    )

    if deposit_amount <= 0 or not business.mpesa_shortcode:
        await repo.reduce_stock_for_order(session, order)
        if _is_manual(business):
            order.status = OrderStatus.AWAITING_OWNER_CONFIRMATION
            await session.flush()
            return (
                f"Order placed: {quantity} x {product.name} (Total KES {_fmt_price(total)}) — {cust_fulfillment}! "
                f"Your order has been sent to the team for confirmation. "
                f"Payment will be collected upon delivery/pickup."
            )
        order.status = OrderStatus.CONFIRMED
        await session.flush()
        return (
            f"Order confirmed: {quantity} x {product.name} (Total KES {_fmt_price(total)}) — {cust_fulfillment}! "
            f"Your items will be prepared. Payment will be collected upon delivery/pickup."
        )

    payment = await payments.initiate_deposit(
        session, business, customer_phone, deposit_amount, mpesa_callback_secret, payment_phone=pending.get("payment_phone")
    )
    order.payment_id = payment.id
    await session.flush()

    target_msg = f" (sent to {pending.get('payment_phone')})" if pending.get("payment_phone") else ""
    return (
        f"Order placed: {quantity} x {product.name} (KES {_fmt_price(total)} total), pending a KES "
        f"{_fmt_price(deposit_amount)} deposit. Check your phone{target_msg} for the M-Pesa prompt to confirm."
    )


async def _finalize_cancel_booking(session: AsyncSession, business: Business, customer_phone: str, pending: dict) -> str:
    booking = await repo.get_booking_for_business(session, business.id, pending.get("booking_id"))
    if booking is None:
        return "I couldn't find that booking anymore."
    if booking.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED):
        return "That booking is already cancelled."

    if booking.proposed_slot_start is not None:
        booking.proposed_slot_start = None
        booking.proposed_slot_end = None
        if booking.status == BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION:
            booking.status = BookingStatus.CONFIRMED

    previous_status = booking.status
    service = await repo.get_service_for_business(session, business.id, booking.service_id)
    service_name = service.name if service else "your booking"

    # v1 explicitly does not handle refunds - if a deposit was already
    # paid, the owner is told so (per notification content) so they can
    # sort a manual refund out themselves; no M-Pesa reversal API call
    # happens here.
    deposit_amount = None
    if booking.payment_id:
        payment = await session.get(Payment, booking.payment_id)
        if payment and payment.status == PaymentStatus.COMPLETED:
            deposit_amount = float(booking.deposit_amount)

    # Setting CANCELLED is the entire "slot release" mechanism - the
    # overlap check in repo.create_booking (and reschedule_booking)
    # excludes CANCELLED/REJECTED bookings, so this slot becomes bookable
    # again the instant this commits. No separate release step needed.
    booking.status = BookingStatus.CANCELLED
    await session.flush()
    await repo.record_audit_event(
        session,
        business_id=business.id,
        entity_type="booking",
        entity_id=booking.id,
        actor="customer",
        action="CANCEL",
        previous_status=previous_status.value if hasattr(previous_status, "value") else str(previous_status),
        new_status="cancelled",
    )

    await owner_workflow.notify_owner_booking_cancelled(
        business,
        customer_phone,
        service_name,
        booking.slot_start,
        previous_status.value,
        deposit_amount,
        customer_name=booking.customer.name if getattr(booking, "customer", None) else None,
    )
    return f"Your {service_name} booking on {booking.slot_start:%d %b at %H:%M} has been cancelled."


async def _finalize_cancel_order(session: AsyncSession, business: Business, customer_phone: str, pending: dict) -> str:
    order = await repo.get_order_for_business(session, business.id, pending.get("order_id"))
    if order is None:
        return "I couldn't find that order anymore."
    if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
        return "That order is already cancelled."

    previous_status = order.status
    summary = await _order_summary_text(session, business, order)

    deposit_amount = None
    if order.payment_id:
        payment = await session.get(Payment, order.payment_id)
        if payment and payment.status == PaymentStatus.COMPLETED:
            deposit_amount = float(order.deposit_amount)

    order.status = OrderStatus.CANCELLED
    await session.flush()
    await repo.record_audit_event(
        session,
        business_id=business.id,
        entity_type="order",
        entity_id=order.id,
        actor="customer",
        action="CANCEL",
        previous_status=previous_status.value if hasattr(previous_status, "value") else str(previous_status),
        new_status="cancelled",
    )

    await owner_workflow.notify_owner_order_cancelled(
        business,
        customer_phone,
        summary,
        previous_status.value,
        deposit_amount,
        customer_name=order.customer.name if getattr(order, "customer", None) else None,
    )
    return f"Your order ({summary}) has been cancelled."


async def _finalize_reschedule_booking(session: AsyncSession, business: Business, customer_phone: str, pending: dict) -> str:
    booking = await repo.get_booking_for_business(session, business.id, pending.get("booking_id"))
    if booking is None or booking.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED):
        return "That booking isn't active anymore."
    if booking.status not in (
        BookingStatus.CONFIRMED,
        BookingStatus.PENDING_DEPOSIT,
        BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION,
    ):
        return "That booking can't be rescheduled right now - please contact us directly."
    if not pending.get("new_slot_start_iso"):
        return "Something went wrong - please tell me the new date and time again."

    service = await repo.get_service_for_business(session, business.id, booking.service_id)
    if service is None:
        return "Something went wrong finding that booking's service."

    new_start = datetime.fromisoformat(pending["new_slot_start_iso"])
    new_end = new_start + timedelta(minutes=service.duration_minutes)

    error = _validate_slot(business, new_start, new_end)
    if error:
        return error + " Please tell me a new date and time."

    old_start = booking.slot_start

    if booking.status == BookingStatus.PENDING_DEPOSIT:
        try:
            await repo.reschedule_booking(
                session,
                business.id,
                booking.id,
                new_start,
                new_end,
                skip_conflict_check=_is_manual(business),
            )
        except BookingConflictError:
            return "That time is already booked - what other time works for you?"
        await owner_workflow.notify_owner_booking_rescheduled(
            business,
            customer_phone,
            service.name,
            old_start,
            new_start,
            customer_name=booking.customer.name if getattr(booking, "customer", None) else None,
        )
        return (
            f"Done - your pending {service.name} booking is now on "
            f"{new_start:%d %b %Y at %H:%M} (was {old_start:%d %b %Y at %H:%M}). "
            "Your deposit confirmation is still pending; I'll update you once M-Pesa confirms it."
        )

    if _is_manual(business):
        await repo.set_proposed_reschedule(session, business.id, booking.id, new_start, new_end)
        await owner_workflow.notify_owner_reschedule_pending(
            business,
            booking.id,
            service.name,
            customer_phone,
            old_start,
            new_start,
            customer_name=booking.customer.name if getattr(booking, "customer", None) else None,
        )
        return (
            f"Your request to move your {service.name} to {new_start:%d %b %Y at %H:%M} "
            f"(from {old_start:%d %b %Y at %H:%M}) has been sent to the team for confirmation."
        )

    try:
        await repo.reschedule_booking(
            session, business.id, booking.id, new_start, new_end, skip_conflict_check=False
        )
    except BookingConflictError:
        return "That time is already booked - what other time works for you?"

    await owner_workflow.notify_owner_booking_rescheduled(
        business,
        customer_phone,
        service.name,
        old_start,
        new_start,
        customer_name=booking.customer.name if getattr(booking, "customer", None) else None,
    )
    return f"Done - your {service.name} is now on {new_start:%d %b %Y at %H:%M} (was {old_start:%d %b %Y at %H:%M})."


async def seed_booking_time_retry(
    session: AsyncSession,
    business: Business,
    customer_phone: str,
    booking_id: int,
    service_id: int,
    service_name: str,
) -> None:
    """After owner soft-rejects a time (manual mode), prompt customer for a new slot."""
    pending = {
        "type": "booking_time_retry",
        "booking_id": booking_id,
        "service_id": service_id,
        "service_name": service_name,
    }
    await repo.set_conversation_state(
        session,
        business.id,
        customer_phone,
        json.dumps({"stage": STAGE_COLLECTING_TIME_RETRY, "pending": pending, "history": []}),
    )


async def seed_reschedule_retry(
    session: AsyncSession,
    business: Business,
    customer_phone: str,
    booking_id: int,
) -> None:
    """After owner soft-rejects a reschedule (manual mode), collect a new time."""
    pending = {"type": "reschedule_booking", "booking_id": booking_id}
    await repo.set_conversation_state(
        session,
        business.id,
        customer_phone,
        json.dumps({"stage": STAGE_COLLECTING_RESCHEDULE, "pending": pending, "history": []}),
    )


# dateutil parsing is a reasonable v1 approach for common phrasings
# ("Thursday", "25 August", "2pm", "14:00") - documented as a known
# limitation in the README for anything more unusual.

_MAX_WEEK_ROLLS = 2  # see _parse_date_text - bounds how far a bare weekday
# mention can roll forward, so an explicit PAST date (e.g. "1 January
# 2020") gets rejected as unparseable rather than silently reinterpreted
# as some arbitrary future date after enough +7-day rolls.


def _parse_date_text(date_text: str) -> datetime | None:
    """Parses just the date part, rolling a bare weekday/relative mention
    ("Thursday") forward to the next future occurrence. Capped at
    _MAX_WEEK_ROLLS rolls - a genuinely past explicit date (not just "last
    week's Thursday") will hit the cap and come back as None (unparseable)
    rather than being silently walked forward into the far future."""
    now = datetime.now()
    lowered = date_text.lower()
    if "day after tomorrow" in lowered:
        return (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "tomorrow" in lowered or "tmr" in lowered or "tmrw" in lowered:
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    has_next_week = "next week" in lowered
    try:
        parsed = dateutil_parser.parse(date_text, default=now.replace(microsecond=0), fuzzy=True)
    except (ValueError, OverflowError):
        return None
    parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    rolls = 0
    while parsed.date() < now.date():
        if rolls >= _MAX_WEEK_ROLLS:
            return None
        parsed += timedelta(days=7)
        rolls += 1
    if has_next_week and any(w in lowered for w in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
        if (parsed.date() - now.date()).days < 7:
            parsed += timedelta(days=7)
    return parsed


def _combine_date_and_time(date_text: str, time_text: str) -> datetime | None:
    date_part = _parse_date_text(date_text)
    if date_part is None:
        return None
    try:
        # Parse the time fragment on its own against a neutral default so we
        # only pull out the hour/minute, then apply those to the resolved date.
        time_part = dateutil_parser.parse(time_text, default=datetime(2000, 1, 1, 0, 0), fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return date_part.replace(hour=time_part.hour, minute=time_part.minute, second=0, microsecond=0)
