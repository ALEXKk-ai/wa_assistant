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

from sqlalchemy import select
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
    Product,
)
from app.whatsapp import send_business_message
from app.workflows import customer as customer_workflow
from app.workflows import owner as owner_workflow

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Webhook message deduplication
# ---------------------------------------------------------------------------
# Meta occasionally delivers the same webhook payload 2-3 times (network
# retries, edge-to-origin replays). Without dedup, a duplicate delivery can
# create duplicate orders, send duplicate replies, or double-advance
# conversation state.  We track each Meta message ID in an in-memory dict
# with a 5-minute TTL and skip any ID we've already processed.
#
# Why in-memory instead of the DB?  At v1 scale (single process, one or a
# handful of businesses) an in-memory dict is simpler, faster, and avoids
# adding a table + migration.  If this ever moves to multi-worker, swap this
# for a Redis SETNX with TTL.
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


async def handle_whatsapp_webhook(
    session: AsyncSession, payload: dict, mpesa_callback_secret: str
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
        if message.get("type") != "text":
            return  # v1 only handles text; media/voice is a v2 concern
        message_id = message.get("id", "")
        sender_phone = message["from"]
        text = message["text"]["body"]
        contacts = change.get("contacts") or []
        sender_name = contacts[0].get("profile", {}).get("name") if contacts else None
    except (KeyError, IndexError):
        logger.warning("Unrecognized WhatsApp webhook payload shape", extra=log_extra(payload=payload))
        return

    # --- Deduplication guard ---
    if message_id and _message_dedup.is_duplicate(message_id):
        logger.info(
            "Duplicate webhook message skipped",
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

    if sender_phone == business.owner_whatsapp_number:
        await handle_owner_command(session, business, text)
        return

    if await _is_under_takeover(session, business.id, sender_phone):
        await owner_workflow.forward_customer_message(business, sender_phone, text, customer_name=sender_name)
        return

    reply_text = await customer_workflow.handle_inbound_message(
        session, business, sender_phone, text, mpesa_callback_secret, customer_name=sender_name
    )
    await send_business_message(business, sender_phone, reply_text)


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
        customer_phone = command.args[0]
        await _set_takeover(session, business.id, customer_phone, True)
        await owner_workflow.send_ack(
            business, f"Bot paused for {customer_phone}. Use REPLY {customer_phone} <message> to talk to them."
        )

    elif command.name == "RELEASE":
        if not command.args:
            await owner_workflow.send_ack(business, "Usage: RELEASE <customer_phone>")
            return
        customer_phone = command.args[0]
        await _set_takeover(session, business.id, customer_phone, False)
        await owner_workflow.send_ack(business, f"Bot resumed for {customer_phone}.")

    elif command.name == "REPLY":
        if len(command.args) < 2:
            await owner_workflow.send_ack(business, "Usage: REPLY <customer_phone> <message>")
            return
        customer_phone, message = command.args[0], command.args[1]
        await send_business_message(business, customer_phone, message)

    elif command.name == "CONFIRM":
        await _handle_confirm_reject(session, business, command.args, approve=True)

    elif command.name == "REJECT":
        await _handle_confirm_reject(session, business, command.args, approve=False)

    else:
        await owner_workflow.send_help(business)


async def _handle_confirm_reject(
    session: AsyncSession, business: Business, args: list[str], approve: bool
) -> None:
    if not args:
        await owner_workflow.send_ack(business, "Usage: CONFIRM B<id> or CONFIRM O<id> (or REJECT)")
        return
    ref = args[0].strip().upper()
    if len(ref) < 2 or ref[0] not in ("B", "O") or not ref[1:].isdigit():
        await owner_workflow.send_ack(business, f"Didn't recognize '{ref}' - expected e.g. B12 or O5.")
        return

    entity_id = int(ref[1:])
    if ref[0] == "B":
        booking = await repo.get_booking_for_business(session, business.id, entity_id)
        if booking is None:
            await owner_workflow.send_ack(business, f"No booking {ref} found.")
            return
        await _handle_booking_confirm_reject(session, business, booking, ref, approve)
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
    session: AsyncSession, business: Business, booking: Booking, ref: str, approve: bool
) -> None:
    manual = business.confirmation_mode == ConfirmationMode.MANUAL
    customer = await session.get(Customer, booking.customer_id)
    service = await repo.get_service_for_business(session, business.id, booking.service_id)
    service_name = service.name if service else "your booking"

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

    # Reject
    deposit_paid = await _booking_deposit_completed(session, booking)
    if manual and deposit_paid:
        if customer:
            await send_business_message(
                business,
                customer.phone_number,
                f"Your {service_name} booking couldn't be confirmed for "
                f"{booking.slot_start:%d %b %Y at %H:%M}. Please choose another date and time - "
                "your deposit is still on this booking.",
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
        await send_business_message(
            business,
            customer.phone_number,
            "Unfortunately your booking couldn't be confirmed - your deposit will be refunded. "
            "Sorry for the inconvenience.",
        )
    await owner_workflow.send_ack(business, f"{ref} rejected.")


async def _is_under_takeover(session: AsyncSession, business_id: int, customer_phone: str) -> bool:
    state_row = await repo.get_conversation_state(session, business_id, customer_phone)
    if state_row is None:
        return False
    state = json.loads(state_row.state_json)
    return bool(state.get("human_takeover", False))


async def _set_takeover(session: AsyncSession, business_id: int, customer_phone: str, value: bool) -> None:
    state_row = await repo.get_conversation_state(session, business_id, customer_phone)
    state = json.loads(state_row.state_json) if state_row else {"stage": "idle"}
    state["human_takeover"] = value
    await repo.set_conversation_state(session, business_id, customer_phone, json.dumps(state))


async def _find_booking_by_payment(session: AsyncSession, payment_id: int) -> Booking | None:
    result = await session.execute(select(Booking).where(Booking.payment_id == payment_id))
    return result.scalar_one_or_none()


async def _find_order_by_payment(session: AsyncSession, payment_id: int) -> Order | None:
    result = await session.execute(select(Order).where(Order.payment_id == payment_id))
    return result.scalar_one_or_none()


async def _reduce_stock_for_order(session: AsyncSession, order: Order) -> None:
    await repo.reduce_stock_for_order(session, order)
