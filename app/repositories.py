"""Data access layer.

Design decision: every business-scoped function takes business_id as an
explicit, required, first-class argument, and filters on it directly in the
query - no context object, no implicit resolution. At v1 scale (a handful of
businesses you provision yourself), the highest-value tenant-isolation
safeguard is that this is impossible to get wrong by omission: business_id
has no default value anywhere in this file, so a call site that forgets it
is a TypeError at call time, not a silent bug at runtime. Grep this file for
"business_id: int" (no Optional, no default) as a standing check.

If/when this grows to self-serve multi-tenant onboarding at meaningful
scale, revisit centralizing this further (see the earlier TenantContext
discussion) - the cost/benefit tips the other way once you're not the one
provisioning every tenant by hand.
"""
import enum
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditEvent,
    Booking,
    BookingStatus,
    Business,
    ConversationState,
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Product,
    Service,
)


class BookingConflictError(RuntimeError):
    """Raised when a requested slot is already booked for this service."""


# Statuses that actually hold a slot. A REJECTED booking (owner declined a
# manual-confirmation request) was previously NOT excluded from the overlap
# check below - meaning a rejected booking permanently blocked its slot
# forever, the same bug class as never releasing a cancelled one. Both are
# now treated identically: neither blocks the slot.
_NON_BLOCKING_BOOKING_STATUSES = (BookingStatus.CANCELLED, BookingStatus.REJECTED)

# A PENDING_DEPOSIT booking that hasn't been paid within this many minutes is
# treated as abandoned and no longer blocks the slot.  The reconciliation job
# also marks these as CANCELLED so they don't linger in the DB indefinitely.
_PENDING_DEPOSIT_TIMEOUT_MINUTES = 30


async def get_business_by_phone_number_id(
    session: AsyncSession, whatsapp_phone_number_id: str
) -> Business | None:
    """The one lookup that is NOT business_id-scoped, by necessity - it's how
    we determine which business_id an inbound webhook call belongs to in the
    first place, from data Meta gives us (their phone_number_id), not from
    anything the caller supplies."""
    result = await session.execute(
        select(Business).where(Business.whatsapp_phone_number_id == whatsapp_phone_number_id)
    )
    return result.scalar_one_or_none()


async def get_business(session: AsyncSession, business_id: int) -> Business | None:
    return await session.get(Business, business_id)


async def list_services(session: AsyncSession, business_id: int) -> list[Service]:
    result = await session.execute(
        select(Service).where(Service.business_id == business_id, Service.active.is_(True))
    )
    return list(result.scalars().all())


async def get_service_for_business(session: AsyncSession, business_id: int, service_id: int) -> Service | None:
    """Unfiltered by active - a booking can reference a service the owner
    later deactivated, and cancel/reschedule/status still need to look it up."""
    result = await session.execute(
        select(Service).where(Service.id == service_id, Service.business_id == business_id)
    )
    return result.scalar_one_or_none()


async def list_products(session: AsyncSession, business_id: int) -> list[Product]:
    result = await session.execute(
        select(Product).where(Product.business_id == business_id, Product.active.is_(True))
    )
    return list(result.scalars().all())


async def get_product_for_business(session: AsyncSession, business_id: int, product_id: int) -> Product | None:
    result = await session.execute(
        select(Product).where(Product.id == product_id, Product.business_id == business_id)
    )
    return result.scalar_one_or_none()


async def get_or_create_customer(
    session: AsyncSession, business_id: int, phone_number: str, name: str | None = None
) -> Customer:
    result = await session.execute(
        select(Customer).where(
            Customer.business_id == business_id, Customer.phone_number == phone_number
        )
    )
    customer = result.scalar_one_or_none()
    if customer is None:
        customer = Customer(business_id=business_id, phone_number=phone_number, name=name)
        session.add(customer)
        await session.flush()
    elif name and customer.name != name:
        customer.name = name
        await session.flush()
    return customer


async def get_conversation_state(
    session: AsyncSession, business_id: int, customer_phone: str
) -> ConversationState | None:
    result = await session.execute(
        select(ConversationState).where(
            ConversationState.business_id == business_id,
            ConversationState.customer_phone == customer_phone,
        )
    )
    return result.scalar_one_or_none()


async def set_conversation_state(
    session: AsyncSession, business_id: int, customer_phone: str, state_json: str
) -> ConversationState:
    state = await get_conversation_state(session, business_id, customer_phone)
    if state is None:
        state = ConversationState(
            business_id=business_id, customer_phone=customer_phone, state_json=state_json
        )
        session.add(state)
    else:
        state.state_json = state_json
    await session.flush()
    return state


async def create_booking(
    session: AsyncSession,
    business_id: int,
    customer_id: int,
    service_id: int,
    slot_start: datetime,
    slot_end: datetime,
    deposit_amount: float,
    *,
    skip_conflict_check: bool = False,
) -> Booking:
    """Creates a booking, optionally guarding against double-booking the same slot.

    skip_conflict_check=True for manual-confirmation businesses where the owner
    adjudicates capacity (multiple requests for the same slot are allowed).
    """
    if not skip_conflict_check:
        if await _slot_has_conflict(
            session, business_id, service_id, slot_start, slot_end
        ):
            raise BookingConflictError("This slot is already booked.")

    booking = Booking(
        business_id=business_id,
        customer_id=customer_id,
        service_id=service_id,
        slot_start=slot_start,
        slot_end=slot_end,
        deposit_amount=deposit_amount,
        status=BookingStatus.PENDING_DEPOSIT,
    )
    session.add(booking)
    await session.flush()
    await record_audit_event(
        session,
        business_id=business_id,
        entity_type="booking",
        entity_id=booking.id,
        actor="customer",
        action="CREATE",
        new_status=booking.status.value if isinstance(booking.status, enum.Enum) else str(booking.status),
    )
    return booking


async def create_order(
    session: AsyncSession,
    business_id: int,
    customer_id: int,
    items_json: str,
    total_amount: float,
    deposit_amount: float,
) -> Order:
    order = Order(
        business_id=business_id,
        customer_id=customer_id,
        items_json=items_json,
        total_amount=total_amount,
        deposit_amount=deposit_amount,
        status=OrderStatus.PENDING_DEPOSIT,
    )
    session.add(order)
    await session.flush()
    await record_audit_event(
        session,
        business_id=business_id,
        entity_type="order",
        entity_id=order.id,
        actor="customer",
        action="CREATE",
        new_status=order.status.value if isinstance(order.status, enum.Enum) else str(order.status),
    )
    return order


async def create_payment(
    session: AsyncSession,
    business_id: int,
    idempotency_key: str,
    amount: float,
) -> Payment:
    payment = Payment(
        business_id=business_id,
        idempotency_key=idempotency_key,
        amount=amount,
        status=PaymentStatus.PENDING,
    )
    session.add(payment)
    await session.flush()
    return payment


async def attach_checkout_request_id(
    session: AsyncSession, payment_id: int, checkout_request_id: str, merchant_request_id: str
) -> None:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        return
    payment.checkout_request_id = checkout_request_id
    payment.merchant_request_id = merchant_request_id
    await session.flush()


async def get_payment_by_checkout_request_id(
    session: AsyncSession, checkout_request_id: str
) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.checkout_request_id == checkout_request_id)
    )
    return result.scalar_one_or_none()


async def get_booking_for_business(
    session: AsyncSession, business_id: int, booking_id: int
) -> Booking | None:
    """Scoped lookup used when the OWNER references a booking by id (e.g.
    "CONFIRM B12") - business_id here comes from the authenticated owner's
    own business, not from anything the owner's message supplies, so an
    owner can never reference another business's booking even by guessing
    an id."""
    result = await session.execute(
        select(Booking).where(Booking.id == booking_id, Booking.business_id == business_id)
    )
    return result.scalar_one_or_none()


async def get_order_for_business(
    session: AsyncSession, business_id: int, order_id: int
) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.id == order_id, Order.business_id == business_id)
    )
    return result.scalar_one_or_none()


async def list_upcoming_bookings_for_customer(
    session: AsyncSession, business_id: int, customer_id: int
) -> list[Booking]:
    """'Upcoming' = not cancelled/rejected and not already in the past.
    Used by the cancel/reschedule flow to find what a customer can act on,
    and by CHECK_STATUS to summarize what's coming up."""
    result = await session.execute(
        select(Booking)
        .where(
            Booking.business_id == business_id,
            Booking.customer_id == customer_id,
            Booking.status.notin_(_NON_BLOCKING_BOOKING_STATUSES),
            Booking.slot_start >= datetime.now(),
        )
        .order_by(Booking.slot_start)
    )
    return list(result.scalars().all())


_NON_BLOCKING_ORDER_STATUSES = (OrderStatus.CANCELLED, OrderStatus.REJECTED)


async def list_upcoming_orders_for_customer(
    session: AsyncSession, business_id: int, customer_id: int
) -> list[Order]:
    """Orders don't have a slot/date - "upcoming" here means "still active",
    most recent first."""
    result = await session.execute(
        select(Order)
        .where(
            Order.business_id == business_id,
            Order.customer_id == customer_id,
            Order.status.notin_(_NON_BLOCKING_ORDER_STATUSES),
        )
        .order_by(Order.created_at.desc())
    )
    return list(result.scalars().all())


async def reschedule_booking(
    session: AsyncSession,
    business_id: int,
    booking_id: int,
    new_slot_start: datetime,
    new_slot_end: datetime,
    *,
    skip_conflict_check: bool = False,
) -> Booking:
    """Updates an existing booking's slot in place - the old slot is freed
    automatically the moment this commits.

    skip_conflict_check=True for manual mode where the owner decides capacity.
    """
    booking = await get_booking_for_business(session, business_id, booking_id)
    if booking is None:
        raise ValueError(f"No booking {booking_id} for business {business_id}")

    if not skip_conflict_check:
        if await _slot_has_conflict(
            session,
            business_id,
            booking.service_id,
            new_slot_start,
            new_slot_end,
            exclude_booking_id=booking_id,
        ):
            raise BookingConflictError("The new slot is already booked.")

    booking.slot_start = new_slot_start
    booking.slot_end = new_slot_end
    booking.proposed_slot_start = None
    booking.proposed_slot_end = None
    await session.flush()
    return booking


async def update_booking_slot(
    session: AsyncSession,
    business_id: int,
    booking_id: int,
    slot_start: datetime,
    slot_end: datetime,
) -> Booking:
    """Change slot on an existing booking with no overlap check (manual owner flow)."""
    booking = await get_booking_for_business(session, business_id, booking_id)
    if booking is None:
        raise ValueError(f"No booking {booking_id} for business {business_id}")
    booking.slot_start = slot_start
    booking.slot_end = slot_end
    await session.flush()
    return booking


async def set_proposed_reschedule(
    session: AsyncSession,
    business_id: int,
    booking_id: int,
    proposed_start: datetime,
    proposed_end: datetime,
) -> Booking:
    """Store a pending reschedule; current slot stays until owner CONFIRMs."""
    booking = await get_booking_for_business(session, business_id, booking_id)
    if booking is None:
        raise ValueError(f"No booking {booking_id} for business {business_id}")
    booking.proposed_slot_start = proposed_start
    booking.proposed_slot_end = proposed_end
    booking.status = BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION
    await session.flush()
    return booking


async def apply_proposed_reschedule(
    session: AsyncSession, business_id: int, booking_id: int
) -> Booking:
    booking = await get_booking_for_business(session, business_id, booking_id)
    if booking is None:
        raise ValueError(f"No booking {booking_id} for business {business_id}")
    if booking.proposed_slot_start is None or booking.proposed_slot_end is None:
        raise ValueError(f"Booking {booking_id} has no proposed reschedule")
    booking.slot_start = booking.proposed_slot_start
    booking.slot_end = booking.proposed_slot_end
    booking.proposed_slot_start = None
    booking.proposed_slot_end = None
    booking.status = BookingStatus.CONFIRMED
    await session.flush()
    return booking


async def clear_proposed_reschedule(
    session: AsyncSession, business_id: int, booking_id: int
) -> Booking:
    booking = await get_booking_for_business(session, business_id, booking_id)
    if booking is None:
        raise ValueError(f"No booking {booking_id} for business {business_id}")
    booking.proposed_slot_start = None
    booking.proposed_slot_end = None
    if booking.status == BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION:
        booking.status = BookingStatus.CONFIRMED
    await session.flush()
    return booking


async def _slot_has_conflict(
    session: AsyncSession,
    business_id: int,
    service_id: int,
    slot_start: datetime,
    slot_end: datetime,
    *,
    exclude_booking_id: int | None = None,
) -> bool:
    from sqlalchemy import and_, func, not_, or_

    # A booking blocks the slot unless it's cancelled/rejected, or it's a
    # PENDING_DEPOSIT booking that's been sitting unpaid past the timeout
    # (the customer likely abandoned the M-Pesa prompt).
    #
    # The staleness check uses func.now() (SQL-side CURRENT_TIMESTAMP) so
    # the comparison is in the same timezone as the server_default on
    # created_at — no Python-side datetime.now() mismatch.
    stale_pending = and_(
        Booking.status == BookingStatus.PENDING_DEPOSIT,
        Booking.created_at < func.now() - timedelta(minutes=_PENDING_DEPOSIT_TIMEOUT_MINUTES),
    )

    query = select(Booking).where(
        Booking.business_id == business_id,
        Booking.service_id == service_id,
        Booking.status.notin_(_NON_BLOCKING_BOOKING_STATUSES),
        not_(stale_pending),
        Booking.slot_start < slot_end,
        Booking.slot_end > slot_start,
    )
    if exclude_booking_id is not None:
        query = query.where(Booking.id != exclude_booking_id)
    result = await session.execute(query)
    return result.first() is not None


async def list_pending_payments_older_than(
    session: AsyncSession, cutoff: datetime
) -> list[Payment]:
    """Used by the reconciliation job (app/payments.py) to find stuck
    payments across ALL businesses - intentionally not business_id scoped,
    since it's an internal ops job, never a customer-facing code path."""
    if cutoff.tzinfo is not None:
        cutoff = cutoff.replace(tzinfo=None)
    result = await session.execute(
        select(Payment).where(
            Payment.status == PaymentStatus.PENDING, Payment.created_at < cutoff
        )
    )
    return list(result.scalars().all())


async def expire_stale_pending_deposit_bookings(
    session: AsyncSession, timeout_minutes: int = _PENDING_DEPOSIT_TIMEOUT_MINUTES
) -> int:
    """Cancels bookings that have been stuck in PENDING_DEPOSIT past the timeout.
    Called by the reconciliation job alongside stale-payment resolution so
    abandoned deposits don't permanently block slots.

    Returns the number of bookings expired this run."""
    from sqlalchemy import func

    result = await session.execute(
        select(Booking).where(
            Booking.status == BookingStatus.PENDING_DEPOSIT,
            Booking.created_at < func.now() - timedelta(minutes=timeout_minutes),
        )
    )
    stale = list(result.scalars().all())
    expired_count = 0
    for booking in stale:
        if booking.payment_id:
            payment = await session.get(Payment, booking.payment_id)
            if payment is not None and payment.status == PaymentStatus.COMPLETED:
                continue  # Payment was completed - guard against accidental cancellation!
        booking.status = BookingStatus.CANCELLED
        expired_count += 1
    if expired_count:
        await session.flush()
    return expired_count


async def reduce_stock_for_order(session: AsyncSession, order: Order) -> None:
    import json
    items = json.loads(order.items_json)
    for item in items:
        product = await session.get(Product, item["product_id"])
        if product is not None:
            from sqlalchemy import case, update

            stmt = (
                update(Product)
                .where(Product.id == product.id)
                .values(
                    stock_qty=case(
                        (Product.stock_qty >= item["qty"], Product.stock_qty - item["qty"]),
                        else_=0,
                    )
                )
            )
            await session.execute(stmt)
    await session.flush()


async def record_audit_event(
    session: AsyncSession,
    business_id: int,
    entity_type: str,
    entity_id: int,
    actor: str,
    action: str,
    new_status: str,
    previous_status: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        business_id=business_id,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
    )
    session.add(event)
    await session.flush()
    return event

