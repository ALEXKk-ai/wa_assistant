"""Top-level routing.

Two webhook entry points, mirroring main.py:
  - handle_whatsapp_webhook: inbound WhatsApp message -> resolve business ->
    routes to EITHER the owner-command handler (if the sender is the
    business's registered owner number) OR the customer workflow - and
    within the customer path, checks for an active human takeover first.
  - handle_mpesa_callback: M-Pesa deposit result -> idempotent payment
    update -> auto-confirm or move to "awaiting owner confirmation"
    depending on the business's confirmation_mode, then notify everyone.
"""
import json
import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import payments as payments_module
from app import repositories as repo
from app.logging_conf import get_logger, log_extra
from app.models import (
    Booking,
    BookingStatus,
    Business,
    ConfirmationMode,
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    ProcessedMessage,
    Product,
)
from app.security import normalize_phone_number
from app.whatsapp import send_business_message
from app.workflows import customer as customer_workflow
from app.workflows import owner as owner_workflow

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Webhook message deduplication (two-tier)
# ---------------------------------------------------------------------------
# Meta occasionally delivers the same webhook payload 2-3 times (network
# retries, edge-to-origin replays). Without dedup, a duplicate delivery can
# create duplicate orders, send duplicate replies, or double-advance
# conversation state.
#
# Tier 1 (fast path): in-memory dict with 5-min TTL. Catches rapid-fire
#   duplicates within the same process without a DB round-trip.
# Tier 2 (durable): INSERT into processed_messages with ON CONFLICT DO
#   NOTHING. Survives container restarts, works across multiple workers.
# ---------------------------------------------------------------------------

_MESSAGE_DEDUP_TTL_SECONDS = 300  # 5 minutes
_MESSAGE_DEDUP_PURGE_EVERY = 100  # purge stale entries every N inserts


class _MessageDedup:
    """Thread-safe-ish (asyncio single-thread) dedup cache with TTL."""

    def __init__(self, ttl: float = _MESSAGE_DEDUP_TTL_SECONDS, purge_every: int = _MESSAGE_DEDUP_PURGE_EVERY):
        self._seen: dict[str, float] = {}  # message_id -> timestamp
        self._ttl = ttl
        self._purge_every = purge_every
        self._insert_count = 0

    def is_duplicate(self, message_id: str) -> bool:
        """Return True if this message_id was already seen within the TTL window."""
        now = time.monotonic()
        seen_at = self._seen.get(message_id)
        if seen_at is not None and (now - seen_at) < self._ttl:
            return True
        self._seen[message_id] = now
        self._insert_count += 1
        if self._insert_count >= self._purge_every:
            self._purge(now)
        return False

    def _purge(self, now: float) -> None:
        stale = [mid for mid, ts in self._seen.items() if (now - ts) >= self._ttl]
        for mid in stale:
            del self._seen[mid]
        self._insert_count = 0


_message_dedup = _MessageDedup()


async def _db_is_duplicate(session: AsyncSession, message_id: str) -> bool:
    """Insert into processed_messages; return True if already present.

    Uses INSERT ... ON CONFLICT DO NOTHING (Postgres) or INSERT OR IGNORE
    (SQLite).  If rowcount == 0 the row already existed → duplicate.
    """
    from app.config import get_settings
    url = get_settings().database_url
    if "postgresql" in url or "postgres" in url:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(ProcessedMessage).values(message_id=message_id).on_conflict_do_nothing(index_elements=["message_id"])
    else:
        # SQLite fallback for local dev / tests
        stmt = ProcessedMessage.__table__.insert().prefix_with("OR IGNORE").values(message_id=message_id)
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount == 0


async def purge_old_processed_messages(session: AsyncSession, older_than_hours: int = 48) -> int:
    """Delete processed_messages rows older than the threshold.

    Called by the reconciliation scheduler to keep the table small.
    Returns the number of rows deleted.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    result = await session.execute(
        delete(ProcessedMessage).where(ProcessedMessage.created_at < cutoff)
    )
    await session.flush()
    return result.rowcount

async def handle_whatsapp_webhook(
    session: AsyncSession, payload: dict, mpesa_callback_secret: str, timing: dict | None = None
) -> None:
    """Parses a Meta WhatsApp Cloud API webhook payload. Meta's payload
    shape wraps messages several levels deep; we only handle the single
    text-message case needed for v1 and ignore anything else (delivery
    receipts, media messages, etc.) rather than erroring on them."""
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        phone_number_id = change["metadata"]["phone_number_id"]
        messages = change.get("messages")
        if not messages:
            return  # status update / receipt, not a customer message
        message = messages[0]
        msg_type = message.get("type", "text")
        message_id = message.get("id", "")
        sender_phone = message["from"]
        text = message["text"]["body"] if msg_type == "text" else f"[{msg_type.upper()} ATTACHMENT]"
        contacts = change.get("contacts") or []
        sender_name = contacts[0].get("profile", {}).get("name") if contacts else None
    except (KeyError, IndexError):
        logger.warning("Unrecognized WhatsApp webhook payload shape", extra=log_extra(payload=payload))
        return

    # --- Deduplication guard (two-tier) ---
    if message_id:
        # Tier 1: fast in-memory check (same process, sub-second retries)
        if _message_dedup.is_duplicate(message_id):
            logger.info(
                "Duplicate webhook message skipped (in-memory)",
                extra=log_extra(message_id=message_id, sender=sender_phone),
            )
            return
        # Tier 2: durable DB check (survives restarts, multi-worker safe)
        if await _db_is_duplicate(session, message_id):
            logger.info(
                "Duplicate webhook message skipped (database)",
                extra=log_extra(message_id=message_id, sender=sender_phone),
            )
            return

    business = await repo.get_business_by_phone_number_id(session, phone_number_id)
    if business is None:
        logger.warning(
            "Webhook for unknown phone_number_id - ignoring",
            extra=log_extra(phone_number_id=phone_number_id),
        )
        return

    if normalize_phone_number(sender_phone) == normalize_phone_number(business.owner_whatsapp_number):
        if msg_type == "text":
            await handle_owner_command(session, business, text)
        return

    if await _is_under_takeover(session, business.id, sender_phone):
        await owner_workflow.forward_customer_message(business, sender_phone, text, customer_name=sender_name)
        return

    if msg_type != "text":
        reply_text = await handle_non_text_message(session, business, sender_phone, msg_type, customer_name=sender_name)
        if reply_text:
            await send_business_message(business, sender_phone, reply_text)
        return

    reply_text = await customer_workflow.handle_inbound_message(
        session, business, sender_phone, text, mpesa_callback_secret, customer_name=sender_name, timing=timing
    )
    await send_business_message(business, sender_phone, reply_text)


async def handle_non_text_message(
    session: AsyncSession, business: Business, sender_phone: str, msg_type: str, customer_name: str | None = None
) -> str | None:
    if msg_type in ("audio", "voice"):
        return (
            "Hello! 👋 I can't listen to voice notes right now. "
            "Please type out your request as a text message so I can assist you!"
        )
    elif msg_type == "sticker":
        return "Thanks for the sticker! 😊 How can I help you today? Feel free to type your request or booking time."
    elif msg_type == "image":
        await owner_workflow.notify_owner_media_received(
            business, sender_phone, media_type="photo", customer_name=customer_name
        )
        return (
            "Thanks for sharing the photo! I've forwarded it to the shop owner, "
            "and they will reply to you shortly."
        )
    elif msg_type in ("document", "video", "location"):
        await owner_workflow.notify_owner_media_received(
            business, sender_phone, media_type=msg_type, customer_name=customer_name
        )
        return (
            f"Thanks! I've received your {msg_type} and forwarded it to the team. "
            "They will get back to you shortly."
        )
    else:
        return (
            "Hello! 👋 I work best with text messages. Please type out your request so I can help you!"
        )


async def process_payment_completion_side_effects(session: AsyncSession, payment, business_lookup) -> None:
    if payment is None or payment.status != PaymentStatus.COMPLETED:
        return
    business = await business_lookup(payment.business_id)
    if business is None:
        return

    booking = await _find_booking_by_payment(session, payment.id)
    order = None if booking is not None else await _find_order_by_payment(session, payment.id)

    if booking is not None and booking.status == BookingStatus.PENDING_DEPOSIT:
        await _process_paid_booking(session, business, booking)
    elif order is not None and order.status == OrderStatus.PENDING_DEPOSIT:
        await _process_paid_order(session, business, order)


async def handle_mpesa_callback(session: AsyncSession, callback_body: dict, business_lookup) -> None:
    payment = await payments_module.handle_callback(session, callback_body)
    await process_payment_completion_side_effects(session, payment, business_lookup)


async def _process_paid_booking(session: AsyncSession, business: Business, booking: Booking) -> None:
    customer = await session.get(Customer, booking.customer_id)
    manual = business.confirmation_mode == ConfirmationMode.MANUAL
    ref = f"B{booking.id}"

    if manual:
        booking.status = BookingStatus.AWAITING_OWNER_CONFIRMATION
        await session.flush()
        await repo.record_audit_event(
            session,
            business_id=business.id,
            entity_type="booking",
            entity_id=booking.id,
            actor="system",
            action="DEPOSIT_PAID",
            previous_status="pending_deposit",
            new_status=booking.status.value,
        )
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                "Deposit received! Your booking is awaiting final confirmation from the "
                "business - we'll message you shortly.",
            )
    else:
        booking.status = BookingStatus.CONFIRMED
        await session.flush()
        await repo.record_audit_event(
            session,
            business_id=business.id,
            entity_type="booking",
            entity_id=booking.id,
            actor="system",
            action="DEPOSIT_PAID",
            previous_status="pending_deposit",
            new_status=booking.status.value,
        )
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                f"Deposit received - your booking on {booking.slot_start:%d %b %Y at %H:%M} is confirmed!",
            )

    await owner_workflow.notify_owner_deposit_paid(
        business,
        ref,
        customer.phone_number if customer else "unknown",
        float(booking.deposit_amount),
        f"Booking on {booking.slot_start:%d %b %Y at %H:%M}.",
        needs_manual_confirmation=manual,
    )


async def _process_paid_order(session: AsyncSession, business: Business, order: Order) -> None:
    customer = await session.get(Customer, order.customer_id)
    manual = business.confirmation_mode == ConfirmationMode.MANUAL
    ref = f"O{order.id}"

    if manual:
        order.status = OrderStatus.AWAITING_OWNER_CONFIRMATION
        await session.flush()
        await repo.record_audit_event(
            session,
            business_id=business.id,
            entity_type="order",
            entity_id=order.id,
            actor="system",
            action="DEPOSIT_PAID",
            previous_status="pending_deposit",
            new_status=order.status.value,
        )
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                "Deposit received! Your order is awaiting final confirmation from the "
                "business - we'll message you shortly.",
            )
    else:
        order.status = OrderStatus.CONFIRMED
        await session.flush()
        await repo.record_audit_event(
            session,
            business_id=business.id,
            entity_type="order",
            entity_id=order.id,
            actor="system",
            action="DEPOSIT_PAID",
            previous_status="pending_deposit",
            new_status=order.status.value,
        )
        await _reduce_stock_for_order(session, order)
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                "Deposit received - your order is confirmed! We'll be in touch about delivery/pickup.",
            )

    await owner_workflow.notify_owner_deposit_paid(
        business,
        ref,
        customer.phone_number if customer else "unknown",
        float(order.deposit_amount),
        "Order.",
        needs_manual_confirmation=manual,
    )


async def handle_owner_command(session: AsyncSession, business: Business, text: str) -> None:
    """Executes a command from the business owner. Parsing lives in
    owner.py; the actual state changes happen here since they need
    repo/session access owner.py deliberately doesn't have (keeps that
    module free of DB concerns and easy to unit test)."""
    command = owner_workflow.parse_owner_command(text)

    if command.name == "TAKEOVER":
        if not command.args:
            await owner_workflow.send_ack(business, "Usage: TAKEOVER <customer_phone>")
            return
        customer_phone = normalize_phone_number(command.args[0])
        await _set_takeover(session, business.id, customer_phone, True)
        await owner_workflow.send_ack(
            business, f"Bot paused for {customer_phone}. Use REPLY {customer_phone} <message> to talk to them."
        )

    elif command.name == "RELEASE":
        if not command.args:
            await owner_workflow.send_ack(business, "Usage: RELEASE <customer_phone>")
            return
        customer_phone = normalize_phone_number(command.args[0])
        await _set_takeover(session, business.id, customer_phone, False)
        await owner_workflow.send_ack(business, f"Bot resumed for {customer_phone}.")

    elif command.name == "REPLY":
        if len(command.args) < 2:
            await owner_workflow.send_ack(business, "Usage: REPLY <customer_phone> <message>")
            return
        customer_phone, message = normalize_phone_number(command.args[0]), command.args[1]
        await send_business_message(business, customer_phone, message)

    elif command.name == "CONFIRM":
        await _handle_confirm_reject(session, business, command.args, action_type="CONFIRM")

    elif command.name == "DECLINE":
        await _handle_confirm_reject(session, business, command.args, action_type="DECLINE")

    elif command.name == "REJECT":
        await _handle_confirm_reject(session, business, command.args, action_type="REJECT")

    else:
        await owner_workflow.send_help(business)


async def _handle_confirm_reject(
    session: AsyncSession, business: Business, args: list[str], action_type: str = "CONFIRM"
) -> None:
    if not args:
        await owner_workflow.send_ack(business, "Usage: CONFIRM B<id>, DECLINE B<id>, or REJECT B<id>")
        return
    ref = args[0].strip().upper()
    if len(ref) < 2 or ref[0] not in ("B", "O") or not ref[1:].isdigit():
        await owner_workflow.send_ack(business, f"Didn't recognize '{ref}' - expected e.g. B12 or O5.")
        return

    entity_id = int(ref[1:])
    approve = (action_type == "CONFIRM")
    if ref[0] == "B":
        booking = await repo.get_booking_for_business(session, business.id, entity_id)
        if booking is None:
            await owner_workflow.send_ack(business, f"No booking {ref} found.")
            return
        await _handle_booking_confirm_reject(session, business, booking, ref, action_type=action_type)
        return

    else:  # "O"
        order = await repo.get_order_for_business(session, business.id, entity_id)
        if order is None:
            await owner_workflow.send_ack(business, f"No order {ref} found.")
            return
        if order.status != OrderStatus.AWAITING_OWNER_CONFIRMATION:
            await owner_workflow.send_ack(
                business, f"Order {ref} isn't awaiting confirmation (status: {order.status.value})."
            )
            return
        prev_status = order.status.value
        order.status = OrderStatus.CONFIRMED if approve else OrderStatus.REJECTED
        await session.flush()
        await repo.record_audit_event(
            session,
            business_id=business.id,
            entity_type="order",
            entity_id=order.id,
            actor="owner",
            action="CONFIRM" if approve else "REJECT",
            previous_status=prev_status,
            new_status=order.status.value,
        )
        if approve:
            await _reduce_stock_for_order(session, order)
        customer = await session.get(Customer, order.customer_id)
        if customer:
            msg = (
                "Your order has been confirmed! We'll be in touch about delivery/pickup."
                if approve
                else "Unfortunately your order couldn't be confirmed - your deposit will be refunded. Sorry for the inconvenience."
            )
            await send_business_message(business, customer.phone_number, msg)
        await owner_workflow.send_ack(business, f"{ref} {'confirmed' if approve else 'rejected'}.")


async def _booking_deposit_completed(session: AsyncSession, booking: Booking) -> bool:
    if not booking.payment_id:
        return False
    payment = await session.get(Payment, booking.payment_id)
    return payment is not None and payment.status == PaymentStatus.COMPLETED


async def _handle_booking_confirm_reject(
    session: AsyncSession, business: Business, booking: Booking, ref: str, action_type: str = "CONFIRM"
) -> None:
    manual = business.confirmation_mode == ConfirmationMode.MANUAL
    customer = await session.get(Customer, booking.customer_id)
    service = await repo.get_service_for_business(session, business.id, booking.service_id)
    service_name = service.name if service else "your booking"

    approve = (action_type == "CONFIRM")

    if booking.status == BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION:
        if booking.proposed_slot_start is None:
            await owner_workflow.send_ack(business, f"Booking {ref} has no pending reschedule.")
            return
        proposed_start = booking.proposed_slot_start
        old_start = booking.slot_start
        if approve:
            await repo.apply_proposed_reschedule(session, business.id, booking.id)
            if customer:
                await send_business_message(
                    business,
                    customer.phone_number,
                    f"Your {service_name} has been moved to {booking.slot_start:%d %b %Y at %H:%M} "
                    f"(was {old_start:%d %b %Y at %H:%M}).",
                )
            await owner_workflow.notify_owner_booking_rescheduled(
                business,
                customer.phone_number if customer else "unknown",
                service_name,
                old_start,
                booking.slot_start,
            )
            await owner_workflow.send_ack(business, f"{ref} reschedule confirmed.")
        else:
            await repo.clear_proposed_reschedule(session, business.id, booking.id)
            if customer:
                await send_business_message(
                    business,
                    customer.phone_number,
                    f"That new time ({proposed_start:%d %b %Y at %H:%M}) didn't work for your "
                    f"{service_name}. Please choose another date and time - your booking on "
                    f"{old_start:%d %b %Y at %H:%M} is still held.",
                )
                await customer_workflow.seed_reschedule_retry(
                    session, business, customer.phone_number, booking.id
                )
            await owner_workflow.send_ack(business, f"{ref} reschedule declined.")
        return

    if booking.status != BookingStatus.AWAITING_OWNER_CONFIRMATION:
        await owner_workflow.send_ack(
            business, f"Booking {ref} isn't awaiting confirmation (status: {booking.status.value})."
        )
        return

    prev_status = booking.status.value
    if approve:
        booking.status = BookingStatus.CONFIRMED
        await session.flush()
        await repo.record_audit_event(
            session,
            business_id=business.id,
            entity_type="booking",
            entity_id=booking.id,
            actor="owner",
            action="CONFIRM",
            previous_status=prev_status,
            new_status=booking.status.value,
        )
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                f"Your {service_name} booking on {booking.slot_start:%d %b %Y at %H:%M} has been confirmed!",
            )
        await owner_workflow.send_ack(business, f"{ref} confirmed.")
        return

    deposit_paid = await _booking_deposit_completed(session, booking)

    # DECLINE or (REJECT on manual mode with paid deposit) -> Soft Reject (Ask customer for new time)
    if action_type == "DECLINE" or (manual and deposit_paid and action_type != "REJECT"):
        dep_note = " - your deposit is still on this booking." if deposit_paid else "."
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                f"Your {service_name} booking couldn't be confirmed for "
                f"{booking.slot_start:%d %b %Y at %H:%M}. Please choose another date and time{dep_note}",
            )
            await customer_workflow.seed_booking_time_retry(
                session,
                business,
                customer.phone_number,
                booking.id,
                booking.service_id,
                service_name,
            )
        await owner_workflow.send_ack(
            business, f"{ref} time declined - customer asked to pick another slot."
        )
        return

    booking.status = BookingStatus.REJECTED
    await session.flush()
    await repo.record_audit_event(
        session,
        business_id=business.id,
        entity_type="booking",
        entity_id=booking.id,
        actor="owner",
        action="REJECT",
        previous_status=prev_status,
        new_status=booking.status.value,
    )
    if customer:
        if deposit_paid:
            msg = (
                "Unfortunately your booking couldn't be confirmed - your deposit will be refunded. "
                "Sorry for the inconvenience."
            )
        else:
            msg = "Unfortunately your booking couldn't be confirmed. Sorry for the inconvenience."
        await send_business_message(business, customer.phone_number, msg)
    await owner_workflow.send_ack(business, f"{ref} rejected.")


async def _is_under_takeover(session: AsyncSession, business_id: int, customer_phone: str) -> bool:
    norm_phone = normalize_phone_number(customer_phone)
    state_row = await repo.get_conversation_state(session, business_id, norm_phone)
    if state_row is None:
        return False
    state = json.loads(state_row.state_json)
    return bool(state.get("human_takeover", False))


async def _set_takeover(session: AsyncSession, business_id: int, customer_phone: str, value: bool) -> None:
    norm_phone = normalize_phone_number(customer_phone)
    state_row = await repo.get_conversation_state(session, business_id, norm_phone)
    state = json.loads(state_row.state_json) if state_row else {"stage": "idle"}
    state["human_takeover"] = value
    await repo.set_conversation_state(session, business_id, norm_phone, json.dumps(state))


async def _find_booking_by_payment(session: AsyncSession, payment_id: int) -> Booking | None:
    result = await session.execute(select(Booking).where(Booking.payment_id == payment_id))
    return result.scalar_one_or_none()


async def _find_order_by_payment(session: AsyncSession, payment_id: int) -> Order | None:
    result = await session.execute(select(Order).where(Order.payment_id == payment_id))
    return result.scalar_one_or_none()


async def _reduce_stock_for_order(session: AsyncSession, order: Order) -> None:
    await repo.reduce_stock_for_order(session, order)
