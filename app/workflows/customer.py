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
from app.conversation_state import normalize_pending_form
from app.conversation_transitions import describe_transition
from app.conversation_turn import ConversationTurnProcessor, TurnContext
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
from app.response_generation import ValidatedResponseContract, render_validated_response
from app.whatsapp import send_business_message
from app.workflows import owner as owner_workflow

logger = get_logger(__name__)
turn_processor = ConversationTurnProcessor()

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
_SIMPLE_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|good\s+morning|good\s+afternoon|good\s+evening)\s*[!.]?\s*$",
    re.IGNORECASE,
)
_PAYMENT_STATUS_RE = re.compile(
    r"\b(paid|pay|payment|deposit|m-?pesa|mpesa|stk)\b",
    re.IGNORECASE,
)
_STOCK_QUESTION_RE = re.compile(r"\b(stock|in\s+stock|available|have|sell|restock|re-?stock)\b", re.IGNORECASE)
_RESTOCK_NOTIFY_RE = re.compile(r"\b(restock|re-?stock|when\s+you\s+get|notify|whatsapp\s+me|message\s+me)\b", re.IGNORECASE)
_TIME_WITH_MERIDIEM_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<period>a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)
_TIME_WITH_DAYPART_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?:at\s+|in\s+the\s+)?"
    r"(?P<period>morning|afternoon|evening|night)\b",
    re.IGNORECASE,
)
_TIME_24H_RE = re.compile(r"^\s*(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*$")
_BARE_TIME_RE = re.compile(r"\bat\s+(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\b|^\s*(?P<hour_standalone>\d{1,2})(?::(?P<minute_standalone>[0-5]\d))?\s*$", re.IGNORECASE)
_CORRECTION_TIME_RE = re.compile(
    r"\b(?:change|move|make|set|switch)\b.{0,30}?"
    r"(?:time|it|appointment|booking)?\s*(?:to|at)?\s*"
    r"(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*$",
    re.IGNORECASE,
)
AMBIGUOUS_12_TIME = "__AMBIGUOUS_12__"


def _infer_bare_time_text(message_text: str, pending: dict, business: Business) -> str | None:
    match = _BARE_TIME_RE.search(message_text)
    if not match:
        return None

    hour_str = match.group("hour") or match.group("hour_standalone")
    minute_str = match.group("minute") or match.group("minute_standalone")
    hour = int(hour_str)
    minute = int(minute_str or 0)
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
            json.dumps({"stage": new_stage, "pending": normalize_pending_form(new_stage, new_pending), "history": history}),
        )
        return reply_text

    history.append({"role": "customer", "text": message_text})

    catalog = await _build_catalog_summary(session, business)
    hours = json.loads(business.hours_json or "{}")
    extra_info = business.extra_info_text or ""
    if business.deposit_percentage and business.deposit_percentage > 0:
        dep_info = f"A {business.deposit_percentage:.0f}% deposit via M-Pesa is required for bookings to secure your slot."
    else:
        dep_info = "No deposit is required for bookings; customers pay when they arrive."
    extra_info = f"{dep_info} {extra_info}".strip() if extra_info else dep_info

    extracted_date_text = _extract_date_text(message_text, pending)
    extracted_time_text = _extract_time_text(message_text, pending, business)
    processed = await turn_processor.process(
        TurnContext(
            business=business,
            message_text=message_text,
            stage=stage,
            pending=pending,
            history=history[:-1],
            catalog=catalog,
            business_hours_text=hours_mod.format_hours(hours),
            business_address=business.address_text or "not listed",
            business_extra_info=extra_info or "none",
            fulfillment_policy=getattr(business.fulfillment_mode, "value", "both"),
            date_text_signal=extracted_date_text,
            time_text_signal=extracted_time_text,
            stage_confirming=STAGE_CONFIRMING,
            active_detail_stages=_ACTIVE_DETAIL_STAGES,
        )
    )
    intent = processed.intent

    pre_routed = None
    if not processed.skip_pre_route:
        pre_routed = await _pre_route_conversation_act(
            session, business, customer, customer_phone, message_text, intent, stage, pending
        )
    if pre_routed is not None:
        reply_text, new_stage, new_pending = pre_routed
    else:
        reply_text, new_stage, new_pending = await _dispatch(
            session, business, customer, customer_phone, message_text, intent, stage, pending, mpesa_callback_secret, history
        )

    history.append({"role": "bot", "text": reply_text})
    history = history[-MAX_HISTORY_ENTRIES:]
    saved_pending = normalize_pending_form(new_stage, new_pending)
    transition = describe_transition(stage, pending, new_stage, saved_pending)
    logger.info(
        "Conversation turn completed",
        extra=log_extra(
            business_id=business.id,
            stage_before=stage,
            stage_after=new_stage,
            pending_type_before=pending.get("type"),
            pending_type_after=saved_pending.get("type"),
            transition=transition.kind,
            final_action=processed.decision.primary_action.value,
            policy_reason=processed.policy_reason,
            reply_preview=reply_text[:120],
        ),
    )

    await repo.set_conversation_state(
        session,
        business.id,
        customer_phone,
        json.dumps({"stage": new_stage, "pending": saved_pending, "history": history}),
    )
    return reply_text


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


_OWNER_AUTHORITY_ACTS = {
    ai.ConversationAct.COMPLAINT,
    ai.ConversationAct.HUMAN_REQUEST,
    ai.ConversationAct.PROPOSAL,
}

_EXPLICIT_HUMAN_REQUEST_RE = re.compile(
    r"\b(discount|negotiate|cheaper|refund|complaint|bad|horrible|unhappy|manager|management|human|person|owner|admin|talk|speak|proposal|partnership|commercial|arrangement|supply|bulk|b2b|collaborat|bring\s+my\s+own|after\s+hours)\b",
    re.IGNORECASE,
)


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
    # If customer is in STAGE_CONFIRMING and providing a phone number or confirmation reply,
    # do NOT allow loose social acts (e.g. UNCERTAIN_ATTENDANCE) to hijack or reset the state.
    digits_in_msg = "".join(c for c in message_text if c.isdigit())
    is_confirming_input = (
        stage == STAGE_CONFIRMING
        and pending
        and (
            intent.type in (ai.IntentType.CONFIRM_ACTION, ai.IntentType.RESEND_DEPOSIT)
            or len(digits_in_msg) >= 9
            or message_text.strip().lower() in ("yes", "confirm", "ok", "sure", "yep", "yeah", "proceed")
        )
    )
    if is_confirming_input:
        return None

    act = intent.conversation_act

    if (
        intent.authority_route == ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED
        or act in _OWNER_AUTHORITY_ACTS
        or bool(_EXPLICIT_HUMAN_REQUEST_RE.search(message_text))
    ):
        await owner_workflow.notify_owner_unanswered_question(
            business, customer_phone, message_text, customer_name=customer.name
        )
        is_complaint = (
            act == ai.ConversationAct.COMPLAINT
            or bool(re.search(r"\b(complaint|bad|horrible|unhappy|late|delay|ruined|disappointed|terrible|worst)\b", message_text, re.IGNORECASE))
        )
        if is_complaint:
            reply = "I'm really sorry to hear that! I've passed this directly to the team so they can look into it, and someone will get back to you shortly."
        else:
            reply = "I've passed this to the team. They'll get back to you soon."
        return reply, stage, pending

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
    if "today" in lowered:
        return "today"
    if "tonight" in lowered:
        return "today"
    if any(w in lowered for w in ("this morning", "this afternoon", "this evening")):
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
    lowered = text.lower()

    if "noon" in lowered:
        return "12:00"
    if "midnight" in lowered:
        return "00:00"

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

    match = _CORRECTION_TIME_RE.search(text)
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if hour == 12:
            return AMBIGUOUS_12_TIME
        if 1 <= hour <= 7:
            hour += 12
        return f"{hour:02d}:{minute:02d}"

    bare = _infer_bare_time_text(text, pending, business)
    if bare is not None:
        return bare

    if "morning" in lowered:
        return "09:00"
    if "afternoon" in lowered:
        return "14:00"
    if "evening" in lowered:
        return "17:00"

    return None


def _infer_bare_time_text(message_text: str, pending: dict, business: Business) -> str | None:
    match = _BARE_TIME_RE.match(message_text)
    if not match:
        return None

    hour = int(match.group("hour") or match.group("hour_standalone"))
    minute = int(match.group("minute") or match.group("minute_standalone") or 0)
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


def _time_needs_clarification(message_text: str, time_text: str | None) -> bool:
    if time_text == AMBIGUOUS_12_TIME:
        return True
    if not time_text:
        return False
    lowered = message_text.lower()
    if "12" not in str(time_text) and not re.search(r"\b12\b", lowered):
        return False
    has_disambiguator = any(w in lowered for w in ("noon", "midday", "midnight", "am", "pm", "morning", "afternoon", "evening", "night"))
    is_correction = bool(re.search(r"\b(change|move|make|set|switch)\b", lowered))
    return is_correction and not has_disambiguator


def _is_valid_kenyan_phone(digits: str) -> bool:
    if not digits:
        return False
    if digits.startswith("0") and len(digits) == 10:
        return True
    if digits.startswith("254") and len(digits) == 12:
        return True
    if (digits.startswith("7") or digits.startswith("1")) and len(digits) == 9:
        return True
    return False


async def _dispatch(
    session, business, customer, customer_phone, message_text, intent, stage, pending, mpesa_callback_secret, history=None
) -> tuple[str, str, dict]:
    """Returns (reply_text, new_stage, new_pending)."""

    if business.business_type == BusinessType.GOODS and intent.type in (ai.IntentType.ASK_INFO, ai.IntentType.LIST_PRODUCTS) and _STOCK_QUESTION_RE.search(message_text):
        reply = await _answer_stock_request(session, business, customer_phone, message_text, customer_name=customer.name)
        return reply, stage, pending

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
        if intent.type in (ai.IntentType.LIST_PRODUCTS, ai.IntentType.LIST_SERVICES):
            reply = intent.reply_text or await _list_services_text(session, business)
            return reply, stage, pending
        if intent.type in (ai.IntentType.BOOK_SERVICE, ai.IntentType.BUY_PRODUCT):
            if pending.get("type") == "reschedule_booking":
                return await _advance_reschedule(session, business, pending, intent.entities, message_text=message_text)
            if pending.get("type") == "booking_time_retry":
                return await _advance_booking_time_retry(session, business, pending, intent.entities, message_text=message_text)
            entities = dict(intent.entities)
            if not entities.get("service_name"):
                entities["service_name"] = entities.get("product_name")
            return await _advance_booking(session, business, pending, entities, message_text=message_text, history=history)

    if business.business_type == BusinessType.GOODS:
        if intent.type == ai.IntentType.LIST_SERVICES:
            hdr = f"We don't offer service appointments, but we sell quality products! Here's what {business.name} has available:"
            reply = await _list_products_text(session, business, header=hdr)
            return reply, stage, pending
        if intent.type == ai.IntentType.LIST_PRODUCTS:
            reply = await _list_products_text(session, business)
            return reply, stage, pending
        if intent.type in (ai.IntentType.BOOK_SERVICE, ai.IntentType.BUY_PRODUCT):
            entities = dict(intent.entities)
            if not entities.get("product_name"):
                entities["product_name"] = entities.get("service_name")
            return await _advance_order(session, business, pending, entities)

    if intent.type == ai.IntentType.LIST_SERVICES:
        reply = await _list_services_text(session, business)
        return reply, stage, pending
    if intent.type == ai.IntentType.LIST_PRODUCTS:
        reply = await _list_products_text(session, business)
        return reply, stage, pending
    if intent.type == ai.IntentType.CHECK_STATUS:
        status_text = await _check_status_text(session, business, customer)
        # Fallback: if no bookings/orders AND message looks like a business-
        # availability question (mentions a date/day or a service name),
        # re-route to booking or hours reply instead of showing empty status.
        if status_text.startswith("You don't have any upcoming"):
            lowered = message_text.lower()
            has_date = _extract_date_text(message_text, pending) is not None
            has_avail = any(w in lowered for w in ("open", "available", "close", "hour"))
            catalog_names = [item.get("name", "").lower() for item in await _build_catalog_summary(session, business)]
            has_service = any(n and n in lowered for n in catalog_names)
            if has_date and has_service:
                # Customer asked "do you open tomorrow so I can come for a haircut"
                # → treat as BOOK_SERVICE
                return await _advance_booking(
                    session, business, pending, intent.entities,
                    message_text=message_text, history=history,
                )
            if has_date or has_avail:
                # Customer asked "do you open tomorrow?" → answer with DB hours
                hours = json.loads(business.hours_json or "{}")
                return f"Our hours are: {hours_mod.format_hours(hours)}", stage, pending
        return status_text, stage, pending
    if intent.type == ai.IntentType.ASK_INFO:
        # For hours-related questions, ALWAYS use DB-sourced hours instead of
        # trusting the LLM's reply_text which can paraphrase incorrectly
        # (e.g. "Monday to Sunday (closed on Sundays)").
        lowered = message_text.lower()
        is_hours_question = any(w in lowered for w in (
            "hour", "open", "close", "closed", "closing", "available",
        ))
        # Also catch day-specific questions like "do you open tomorrow"
        has_day_ref = _extract_date_text(message_text, pending) is not None
        if is_hours_question or has_day_ref:
            hours = json.loads(business.hours_json or "{}")
            # If asking about a specific day, answer specifically
            if has_day_ref:
                date_text = _extract_date_text(message_text, pending)
                parsed = _parse_date_text(date_text) if date_text else None
                if parsed is not None:
                    from app.hours import DAYS, DAY_NAMES
                    day_key = DAYS[parsed.weekday()]
                    day_info = hours.get(day_key)
                    day_name = DAY_NAMES[day_key]
                    if day_info is None:
                        reply = f"Sorry, we're closed on {day_name}s. Our hours are: {hours_mod.format_hours(hours)}"
                    else:
                        reply = f"Yes, we're open on {day_name} from {day_info['open']} to {day_info['close']}!"
                    return reply, stage, pending
            return f"Our hours are: {hours_mod.format_hours(hours)}", stage, pending
        if intent.reply_text:
            return intent.reply_text, stage, pending
        return await _grounded_info_reply(
            session, business, customer_phone, message_text
        ), stage, pending

    if intent.type == ai.IntentType.BOOK_SERVICE:
        if pending.get("type") == "reschedule_booking":
            return await _advance_reschedule(session, business, pending, intent.entities, message_text=message_text)
        if pending.get("type") == "booking_time_retry":
            return await _advance_booking_time_retry(session, business, pending, intent.entities, message_text=message_text)
        return await _advance_booking(session, business, pending, intent.entities, message_text=message_text, history=history)

    if intent.type == ai.IntentType.BUY_PRODUCT:
        return await _advance_order(session, business, pending, intent.entities)

    if intent.type == ai.IntentType.RESEND_DEPOSIT:
        return await _start_resend_deposit(session, business, customer, intent)

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
                if not _is_valid_kenyan_phone(digits_in_msg):
                    return (
                        "That phone number looks invalid — please reply with a valid 10-digit M-Pesa phone number (e.g. 0712345678) or reply YES to use your main number.",
                        STAGE_CONFIRMING,
                        pending,
                    )
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
        business, customer_phone, message_text, customer_name=None
    )
    return (
        "I don't have that information listed here, so I've passed your question "
        "to the team and they'll get back to you soon."
    )


def _find_named_item(message_text: str, names: list[str]) -> str | None:
    lowered = message_text.lower()
    matches = [name for name in names if name and name.lower() in lowered]
    if not matches:
        return None
    return max(matches, key=len)


async def _answer_stock_request(
    session: AsyncSession,
    business: Business,
    customer_phone: str,
    message_text: str,
    customer_name: str | None = None,
) -> str:
    products = await repo.list_products(session, business.id)
    if not products:
        await owner_workflow.notify_owner_unanswered_question(
            business, customer_phone, message_text, customer_name=customer_name
        )
        return (
            "I don't have product stock information listed here right now, so I've passed this to the team "
            "and they'll get back to you soon."
        )

    names = [p.name for p in products]
    matched_name = _find_named_item(message_text, names)
    restock_request = bool(_RESTOCK_NOTIFY_RE.search(message_text))
    if matched_name:
        product = next(p for p in products if p.name == matched_name)
        stock_text = (
            f"Yes, {product.name} is in stock at KES {_fmt_price(product.price)}."
            if product.stock_qty > 0
            else f"{product.name} is currently out of stock."
        )
        if restock_request:
            await owner_workflow.notify_owner_unanswered_question(
                business, customer_phone, message_text, customer_name=customer_name
            )
            stock_text += " I've passed your restock follow-up request to the team."
        return stock_text

    listed = ", ".join(names)
    if restock_request:
        await owner_workflow.notify_owner_unanswered_question(
            business, customer_phone, message_text, customer_name=customer_name
        )
        return (
            f"I don't see that product in the listed catalog. Current products: {listed}. "
            "I've passed your restock/request note to the team."
        )
    return f"I don't see that product in the listed catalog. Current products: {listed}."


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


def _secondary_info_addendum(message_text: str, business: Business) -> str:
    """Scan for common secondary questions in a multi-part message and build
    a brief addendum so the customer doesn't have to ask again."""
    lowered = message_text.lower()
    parts: list[str] = []

    # Location / address question
    if any(w in lowered for w in ("where", "location", "address", "directions", "find", "located")):
        if business.address_text:
            parts.append(f"📍 We're located at: {business.address_text}")

    # M-Pesa / payment method question
    if any(w in lowered for w in ("m-pesa", "mpesa", "payment method", "pay with", "take m-pesa", "accept m-pesa")):
        if business.mpesa_shortcode:
            parts.append("💳 Yes, we accept M-Pesa payments!")
        else:
            parts.append("💳 Payment is collected at the shop.")

    # Deposit question (only if not already covered by the booking confirmation)
    if any(w in lowered for w in ("deposit", "upfront", "pay first", "pay before")):
        if business.deposit_percentage and business.deposit_percentage > 0:
            parts.append(f"💰 A {business.deposit_percentage:.0f}% deposit is required to secure your slot.")

    if not parts:
        return ""
    return "\n\n" + "\n".join(parts)


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


def _infer_service_from_history(history: list[dict], services) -> object | None:
    """Scan recent bot messages for catalog service names that were discussed.
    Returns the matching service if exactly one is found in the last few turns."""
    if not history:
        return None
    service_names_lower = {s.name.strip().lower(): s for s in services}
    # Look at the last 4 bot messages (most recent context)
    recent_bot_msgs = [t for t in history if t.get("role") == "bot"][-4:]
    found = set()
    for turn in recent_bot_msgs:
        text = (turn.get("text") or "").lower()
        for name_lower, svc in service_names_lower.items():
            if name_lower in text:
                found.add(svc.id)
    if len(found) == 1:
        svc_id = found.pop()
        return next((s for s in services if s.id == svc_id), None)
    return None


async def _advance_booking(
    session: AsyncSession,
    business: Business,
    pending: dict,
    entities: dict,
    message_text: str = "",
    history: list[dict] | None = None,
):
    old_date_text = pending.get("date_text")
    old_time_text = pending.get("time_text")
    pending = _merge_entities(pending, entities, ["service_name", "date_text", "time_text", "payment_phone"])
    pending["type"] = "booking"
    if pending.get("date_text") != old_date_text or pending.get("time_text") != old_time_text:
        pending.pop("slot_start_iso", None)

    services = await repo.list_services(session, business.id)

    # Support multi-service extraction (e.g. ["Haircut", "Hair Coloring"])
    raw_service_names = entities.get("service_names") or []
    if isinstance(raw_service_names, str):
        raw_service_names = [raw_service_names]
    if pending.get("service_name") and pending["service_name"] not in raw_service_names:
        raw_service_names.insert(0, pending["service_name"])

    matched_services = []
    for s_name in raw_service_names:
        if not s_name:
            continue
        name_clean = s_name.strip().lower()
        match_svc = next((s for s in services if s.name.strip().lower() == name_clean or name_clean in s.name.lower() or s.name.lower() in name_clean), None)
        if match_svc and match_svc not in matched_services:
            matched_services.append(match_svc)

    if matched_services:
        pending["service_ids"] = [s.id for s in matched_services]
        pending["service_names"] = [s.name for s in matched_services]
        pending["service_id"] = matched_services[0].id
        pending["service_name"] = " & ".join(s.name for s in matched_services)

    services_list = []
    if pending.get("service_ids"):
        for sid in pending["service_ids"]:
            svc_item = next((s for s in services if s.id == sid), None)
            if svc_item and svc_item not in services_list:
                services_list.append(svc_item)

    service = services_list[0] if services_list else None
    if service is None and pending.get("service_id"):
        service = next((s for s in services if s.id == pending["service_id"]), None)
    if service is None and pending.get("service_name"):
        name = pending["service_name"].strip().lower()
        service = next((s for s in services if s.name.strip().lower() == name or name in s.name.lower() or s.name.lower() in name), None)
        if service is not None:
            pending["service_id"] = service.id

    if service is None:
        for s in services:
            if s.name.lower() in (message_text or "").lower():
                service = s
                pending["service_id"] = s.id
                pending["service_name"] = s.name
                break

    if service is None and not pending.get("service_name"):
        service = _infer_service_from_history(history or [], services)
        if service is not None:
            pending["service_id"] = service.id
            pending["service_name"] = service.name

    if not services_list and service is not None:
        services_list = [service]

    if not services_list:
        if pending.get("service_name"):
            bad_name = pending["service_name"]
            reply = f"We don't offer '{bad_name.title()}' at {business.name}. " + await _list_services_text(session, business, header="Here are the services we offer:")
            return reply, STAGE_IDLE, {}
        else:
            reply = "Sure - which service would you like to book?"
        return reply, STAGE_COLLECTING_BOOKING, pending

    total_price = sum(float(s.price) for s in services_list)
    total_duration = sum(s.duration_minutes for s in services_list)
    combined_name = " & ".join(s.name for s in services_list)

    date_text = pending.get("date_text")
    time_text = pending.get("time_text")

    if _time_needs_clarification(message_text, time_text):
        pending["time_text"] = None
        pending.pop("slot_start_iso", None)
        return (
            "Did you mean 12:00 noon or midnight? Please reply with '12 noon' or 'midnight'.",
            STAGE_COLLECTING_BOOKING,
            pending,
        )

    if date_text and not time_text:
        inferred_time = _extract_time_text(message_text, pending, business) or _extract_time_text(date_text, pending, business)
        if inferred_time:
            time_text = inferred_time
            pending["time_text"] = time_text

    if time_text and not date_text:
        inferred_date = _extract_date_text(message_text, pending) or _extract_date_text(time_text, pending)
        if inferred_date:
            date_text = inferred_date
            pending["date_text"] = date_text

    if not date_text and not time_text:
        dur_str = f"{total_duration // 60}h {total_duration % 60}m" if total_duration >= 60 else f"{total_duration} min"
        return (
            f"Great choice - {combined_name} (KES {_fmt_price(total_price)}, {dur_str}). "
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
            f"Got it, {combined_name} on {parsed_date:%A %d %b} - what time works for you?",
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

    slot_end = slot_start + timedelta(minutes=total_duration)
    error = _validate_slot(business, slot_start, slot_end)
    if error:
        pending["time_text"] = None
        return error, STAGE_COLLECTING_BOOKING, pending

    deposit_amount = sum(payments.compute_deposit_amount(business, float(s.price), item=s) for s in services_list)
    pending["slot_start_iso"] = slot_start.isoformat()
    if deposit_amount > 0 and business.mpesa_shortcode:
        phone_hint = f" to {pending['payment_phone']}" if pending.get("payment_phone") else ""
        deposit_text = f", KES {_fmt_price(deposit_amount)} deposit.\nReply YES to send the M-Pesa prompt{phone_hint}, or reply with a different M-Pesa number (e.g. 0712345678)"
    else:
        deposit_text = ".\nNo upfront deposit required — payment will be collected upon arrival. Reply YES to confirm"

    dur_str = f"{total_duration // 60}h {total_duration % 60}m" if total_duration >= 60 else f"{total_duration} min"
    reply = render_validated_response(
        ValidatedResponseContract(
            purpose="booking_summary",
            allowed_facts={
                "service": combined_name,
                "slot": f"{slot_start:%A %d %b at %H:%M}",
                "price": f"KES {_fmt_price(total_price)}",
                "duration": dur_str,
                "deposit_text": deposit_text,
            },
            required_next_step="Ask the customer to reply YES to confirm.",
            forbidden_claims=["Do not say payment is complete.", "Do not say the owner confirmed."],
        )
    )
    addendum = _secondary_info_addendum(message_text, business)
    if addendum:
        reply += addendum
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
    reply = render_validated_response(
        ValidatedResponseContract(
            purpose="order_summary",
            allowed_facts={
                "summary": f"{quantity} x {product.name} (KES {_fmt_price(total)} total) - {fulfillment_str}",
                "deposit_text": deposit_text,
            },
            required_next_step="Ask the customer to reply YES to confirm.",
            forbidden_claims=["Do not say payment is complete.", "Do not reduce stock yet."],
        )
    )
    return reply, STAGE_CONFIRMING, pending


async def _advance_reschedule(
    session: AsyncSession, business: Business, pending: dict, entities: dict, message_text: str = ""
):
    old_date_text = pending.get("date_text")
    old_time_text = pending.get("time_text")
    pending = _merge_entities(pending, entities, ["date_text", "time_text"])
    pending["type"] = "reschedule_booking"
    if pending.get("date_text") != old_date_text or pending.get("time_text") != old_time_text:
        pending.pop("new_slot_start_iso", None)

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

    if _time_needs_clarification(message_text, time_text):
        pending["time_text"] = None
        pending.pop("new_slot_start_iso", None)
        return (
            "Did you mean 12:00 noon or midnight? Please reply with '12 noon' or 'midnight'.",
            STAGE_COLLECTING_RESCHEDULE,
            pending,
        )

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
    session: AsyncSession, business: Business, pending: dict, entities: dict, message_text: str = ""
):
    """Collect a new date/time for an existing booking after owner soft-rejected."""
    old_date_text = pending.get("date_text")
    old_time_text = pending.get("time_text")
    pending = _merge_entities(pending, entities, ["date_text", "time_text"])
    pending["type"] = "booking_time_retry"
    if pending.get("date_text") != old_date_text or pending.get("time_text") != old_time_text:
        pending.pop("slot_start_iso", None)

    service = await repo.get_service_for_business(session, business.id, pending.get("service_id"))
    if service is None:
        return "Something went wrong - please start your booking again.", STAGE_IDLE, {}

    date_text = pending.get("date_text")
    time_text = pending.get("time_text")

    if _time_needs_clarification(message_text, time_text):
        pending["time_text"] = None
        pending.pop("slot_start_iso", None)
        return (
            "Did you mean 12:00 noon or midnight? Please reply with '12 noon' or 'midnight'.",
            STAGE_COLLECTING_TIME_RETRY,
            pending,
        )

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


async def _start_resend_deposit(
    session: AsyncSession, business: Business, customer, intent: ai.Intent
) -> tuple[str, str, dict]:
    custom_phone = (intent.entities or {}).get("payment_phone")
    target_phone = custom_phone or customer.phone_number

    bookings = await repo.list_upcoming_bookings_for_customer(session, business.id, customer.id)
    pending_bookings = [b for b in bookings if b.status == BookingStatus.PENDING_DEPOSIT]
    if pending_bookings:
        booking = pending_bookings[0]
        service = await repo.get_service_for_business(session, business.id, booking.service_id)
        service_name = service.name if service else "your booking"
        resend_pending = {
            "type": "resend_deposit",
            "booking_id": booking.id,
            "deposit_amount": float(booking.deposit_amount),
            "item_name": service_name,
            "payment_phone": custom_phone,
        }
        reply = (
            f"Sure! Would you like me to send the M-Pesa prompt for KES {_fmt_price(booking.deposit_amount)} ({service_name}) to {target_phone}?\n"
            f"Reply YES to proceed, or reply with a different M-Pesa number (e.g. 0712345678)."
        )
        return reply, STAGE_CONFIRMING, resend_pending

    orders = await repo.list_upcoming_orders_for_customer(session, business.id, customer.id)
    pending_orders = [o for o in orders if o.status == OrderStatus.PENDING_DEPOSIT]
    if pending_orders:
        order = pending_orders[0]
        summary = await _order_summary_text(session, business, order)
        resend_pending = {
            "type": "resend_deposit",
            "order_id": order.id,
            "deposit_amount": float(order.deposit_amount),
            "item_name": summary,
            "payment_phone": custom_phone,
        }
        reply = (
            f"Sure! Would you like me to send the M-Pesa prompt for KES {_fmt_price(order.deposit_amount)} ({summary}) to {target_phone}?\n"
            f"Reply YES to proceed, or reply with a different M-Pesa number (e.g. 0712345678)."
        )
        return reply, STAGE_CONFIRMING, resend_pending

    return (
        "I don't see an upcoming booking or order waiting for a deposit on this chat right now. Would you like to make a new booking?",
        STAGE_IDLE,
        {},
    )

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
    if any(w in lowered for w in ("today", "tonight", "this morning", "this afternoon", "this evening")):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
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
    lowered = (time_text or "").lower().strip()
    
    if any(c.isdigit() for c in lowered):
        try:
            time_part = dateutil_parser.parse(lowered, default=datetime(2000, 1, 1, 0, 0), fuzzy=True)
            hour = time_part.hour
            minute = time_part.minute
            if 1 <= hour <= 7 and "am" not in lowered and "morning" not in lowered:
                hour += 12
            return date_part.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, OverflowError):
            pass

    if "morning" in lowered:
        return date_part.replace(hour=9, minute=0, second=0, microsecond=0)
    if "afternoon" in lowered:
        return date_part.replace(hour=14, minute=0, second=0, microsecond=0)
    if "evening" in lowered:
        return date_part.replace(hour=17, minute=0, second=0, microsecond=0)
    try:
        time_part = dateutil_parser.parse(time_text, default=datetime(2000, 1, 1, 0, 0), fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return date_part.replace(hour=time_part.hour, minute=time_part.minute, second=0, microsecond=0)
