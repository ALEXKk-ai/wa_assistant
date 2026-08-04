"""Owner-facing notifications and commands.

The owner interacts with the bot by messaging the SAME business WhatsApp
number the bot uses for customers (that's the only number Meta lets this
business send/receive from) - so inbound messages from
business.owner_whatsapp_number are routed here instead of the customer
workflow (see app/engine.py:handle_whatsapp_webhook).

Supported owner commands (case-insensitive, first word is the command):
  TAKEOVER <customer_phone>          - pause the bot for this customer;
                                        their messages get forwarded to you
                                        instead of auto-replied to
  RELEASE <customer_phone>           - hand the conversation back to the bot
  REPLY <customer_phone> <message>   - send <message> to that customer as
                                        the business (works anytime, mainly
                                        useful during a takeover)
  CONFIRM B<id> | CONFIRM O<id>      - manually confirm a booking/order that
                                        is awaiting your confirmation
  REJECT B<id> | REJECT O<id>        - manually reject one instead

This module only parses commands and sends messages - it does not touch the
database. app/engine.py:handle_owner_command executes the actual state
changes, to avoid a circular import (engine already imports this module for
notifications).
"""
from dataclasses import dataclass

from app.logging_conf import get_logger, log_extra
from app.models import Business
from app.whatsapp import send_business_message

logger = get_logger(__name__)

HELP_TEXT = (
    "Commands:\n"
    "TAKEOVER <phone> - pause the bot for a customer\n"
    "RELEASE <phone> - hand the conversation back to the bot\n"
    "REPLY <phone> <message> - message a customer directly\n"
    "CONFIRM B<id> / CONFIRM O<id> - confirm a booking/order\n"
    "REJECT B<id> / REJECT O<id> - reject a booking/order"
)


@dataclass
class OwnerCommand:
    name: str  # "TAKEOVER" | "RELEASE" | "REPLY" | "CONFIRM" | "REJECT" | "DECLINE" | "UNKNOWN"
    args: list[str]


def parse_owner_command(text: str) -> OwnerCommand:
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return OwnerCommand(name="UNKNOWN", args=[])
    command = parts[0].upper()
    if command in {"TAKEOVER", "RELEASE", "REPLY", "CONFIRM", "REJECT", "DECLINE"}:
        return OwnerCommand(name=command, args=parts[1:])
    return OwnerCommand(name="UNKNOWN", args=parts)


def _format_customer_label(customer_phone: str, customer_name: str | None = None) -> str:
    if customer_name and customer_name.strip():
        return f"{customer_name.strip()} ({customer_phone})"
    return customer_phone


async def notify_owner_new_booking_request(
    business: Business,
    booking_id: int,
    service_name: str,
    slot_text: str,
    customer_phone: str,
    deposit_amount: float = 0.0,
    customer_name: str | None = None,
) -> None:
    manual = business.confirmation_mode.value == "manual"
    if deposit_amount > 0:
        status_note = "Awaiting their deposit - you'll get another message once it's paid."
    elif manual:
        status_note = f"No deposit required.\nReply CONFIRM B{booking_id} to accept, DECLINE B{booking_id} to ask for another time, or REJECT B{booking_id} to cancel."
    else:
        status_note = "No deposit required (Auto-confirmed)."

    cust_label = _format_customer_label(customer_phone, customer_name)
    text = (
        f"New booking request (ref B{booking_id}): {service_name} on {slot_text}\n"
        f"Customer: {cust_label}\n"
        f"{status_note}"
    )
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of new booking request",
        extra=log_extra(business_id=business.id, booking_id=booking_id),
    )


async def notify_owner_new_order_request(
    business: Business,
    order_id: int,
    summary: str,
    customer_phone: str,
    deposit_amount: float = 0.0,
    customer_name: str | None = None,
) -> None:
    manual = business.confirmation_mode.value == "manual"
    if deposit_amount > 0:
        status_note = "Awaiting their deposit - you'll get another message once it's paid."
    elif manual:
        status_note = f"No deposit required.\nReply CONFIRM O{order_id} to accept, or REJECT O{order_id} to decline."
    else:
        status_note = "No deposit required (Auto-confirmed)."

    cust_label = _format_customer_label(customer_phone, customer_name)
    text = (
        f"New order request (ref O{order_id}): {summary}\n"
        f"Customer: {cust_label}\n"
        f"{status_note}"
    )
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of new order request",
        extra=log_extra(business_id=business.id, order_id=order_id),
    )


async def notify_owner_deposit_paid(
    business: Business,
    ref: str,
    customer_phone: str,
    amount: float,
    description: str,
    needs_manual_confirmation: bool,
    customer_name: str | None = None,
) -> None:
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = f"Deposit received (ref {ref}): KES {amount} from {cust_label}.\n{description}"
    if needs_manual_confirmation:
        text += f"\n\nReply CONFIRM {ref} to accept, DECLINE {ref} to ask for another time, or REJECT {ref} to cancel."
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of deposit",
        extra=log_extra(
            business_id=business.id, ref=ref, amount=amount, manual=needs_manual_confirmation
        ),
    )


async def forward_customer_message(
    business: Business, customer_phone: str, text: str, customer_name: str | None = None
) -> None:
    """Used while a conversation is under owner takeover - the bot doesn't
    auto-reply, it just relays what the customer said so the owner can
    respond with REPLY <phone> <message>."""
    cust_label = _format_customer_label(customer_phone, customer_name)
    forwarded = f"[{cust_label}]: {text}\n\n(Reply with: REPLY {customer_phone} <your message>)"
    await send_business_message(business, business.owner_whatsapp_number, forwarded)


async def notify_owner_unanswered_question(
    business: Business, customer_phone: str, question_text: str, customer_name: str | None = None
) -> None:
    """Sent when the bot classifies a message as OUT_OF_SCOPE - something it
    isn't grounded to answer from the catalog/business info (a proposal, a
    custom-price negotiation, a complaint needing a human call). The bot
    tells the customer it's passing this along rather than improvising an
    answer; this is the message that actually gets it to the owner."""
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = (
        f"A customer asked something the bot couldn't answer from your catalog/info "
        f"(ref: {cust_label}):\n\"{question_text}\"\n\n"
        f"Reply with: REPLY {customer_phone} <your message> to answer them directly."
    )
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of out-of-scope question",
        extra=log_extra(business_id=business.id, customer_phone=customer_phone),
    )


async def notify_owner_booking_cancelled(
    business: Business,
    customer_phone: str,
    service_name: str,
    slot_start,
    previous_status: str,
    deposit_amount: float | None,
    customer_name: str | None = None,
) -> None:
    """Sent immediately after a booking is successfully cancelled by the
    customer - a separate outbound message every time, never inferred from
    the customer-facing reply alone."""
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = f"Booking cancelled: {service_name} on {slot_start:%d %b %Y at %H:%M}. Customer: {cust_label}."
    if deposit_amount:
        text += f" Deposit paid: KES {deposit_amount} (manual refund may be required)."
    text += f" Previous status: {previous_status}."
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of booking cancellation",
        extra=log_extra(business_id=business.id, customer_phone=customer_phone, previous_status=previous_status),
    )


async def notify_owner_order_cancelled(
    business: Business,
    customer_phone: str,
    order_summary: str,
    previous_status: str,
    deposit_amount: float | None,
    customer_name: str | None = None,
) -> None:
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = f"Order cancelled: {order_summary}. Customer: {cust_label}."
    if deposit_amount:
        text += f" Deposit paid: KES {deposit_amount} (manual refund may be required)."
    text += f" Previous status: {previous_status}."
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of order cancellation",
        extra=log_extra(business_id=business.id, customer_phone=customer_phone, previous_status=previous_status),
    )


async def notify_owner_booking_rescheduled(
    business: Business,
    customer_phone: str,
    service_name: str,
    old_slot_start,
    new_slot_start,
    customer_name: str | None = None,
) -> None:
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = (
        f"Booking rescheduled: {service_name} for {cust_label}. "
        f"Was: {old_slot_start:%d %b %Y %H:%M} -> Now: {new_slot_start:%d %b %Y %H:%M}."
    )
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of booking reschedule",
        extra=log_extra(business_id=business.id, customer_phone=customer_phone),
    )


async def notify_owner_booking_time_change_request(
    business: Business,
    booking_id: int,
    service_name: str,
    slot_text: str,
    customer_phone: str,
    *,
    context: str,
    customer_name: str | None = None,
) -> None:
    """Owner must CONFIRM/REJECT a new time on an existing booking (manual mode).

    context: "initial_retry" (soft-reject on first booking) | "reschedule"
    """
    if context == "reschedule":
        intro = f"Reschedule request (ref B{booking_id}): {service_name} to {slot_text}"
    else:
        intro = f"New time requested (ref B{booking_id}): {service_name} on {slot_text}"
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = (
        f"{intro}\nCustomer: {cust_label}\n"
        f"Reply CONFIRM B{booking_id} to accept, or REJECT B{booking_id} if that time won't work."
    )
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of booking time change request",
        extra=log_extra(business_id=business.id, booking_id=booking_id, context=context),
    )


async def notify_owner_reschedule_pending(
    business: Business,
    booking_id: int,
    service_name: str,
    customer_phone: str,
    old_slot_start,
    new_slot_start,
    customer_name: str | None = None,
) -> None:
    cust_label = _format_customer_label(customer_phone, customer_name)
    text = (
        f"Reschedule request (ref B{booking_id}): {service_name} for {cust_label}.\n"
        f"Currently: {old_slot_start:%d %b %Y at %H:%M}\n"
        f"Requested: {new_slot_start:%d %b %Y at %H:%M}\n\n"
        f"Reply CONFIRM B{booking_id} to accept, or REJECT B{booking_id} to decline."
    )
    await send_business_message(business, business.owner_whatsapp_number, text)
    logger.info(
        "Owner notified of pending reschedule",
        extra=log_extra(business_id=business.id, booking_id=booking_id),
    )


async def send_ack(business: Business, text: str) -> None:
    await send_business_message(business, business.owner_whatsapp_number, text)


async def send_help(business: Business) -> None:
    await send_ack(business, HELP_TEXT)
