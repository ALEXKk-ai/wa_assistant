from datetime import datetime, timedelta

from app import ai
from app import repositories as repo
from app.models import Booking, BookingStatus, Order, OrderStatus, PaymentStatus, Service
from app.workflows import customer as customer_mod


def _mock_extract_intent(monkeypatch, responses):
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(ai, "extract_intent", _fake)
    return calls


async def _make_confirmed_booking(session, business, phone, start, duration=45, price=800, paid=True):
    service = Service(business_id=business.id, name="Haircut", price=price, duration_minutes=duration)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, business.id, phone)
    booking = await repo.create_booking(
        session, business.id, customer.id, service.id, start, start + timedelta(minutes=duration), 160
    )
    if paid:
        payment = await repo.create_payment(session, business.id, f"idem-{booking.id}", 160)
        payment.status = PaymentStatus.COMPLETED
        booking.payment_id = payment.id
        booking.status = BookingStatus.CONFIRMED
    await session.flush()
    return booking, service, customer


async def test_cancel_single_booking_prompts_confirmation(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=3)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200001", start)

    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={})])

    reply = await customer_mod.handle_inbound_message(
        session, business, "254700200001", "cancel my haircut", "cb-secret"
    )
    assert "Reply YES" in reply
    assert "Haircut" in reply


async def test_cancel_confirmed_booking_releases_slot_and_notifies_owner(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=3)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200002", start)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={}),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )

    phone = "254700200002"
    await customer_mod.handle_inbound_message(session, business, phone, "cancel my haircut", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")

    assert "has been cancelled" in reply
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.CANCELLED

    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any("Booking cancelled: Haircut" in t and phone in t and "Deposit paid: KES 160" in t for t in owner_msgs)

    other_customer = await repo.get_or_create_customer(session, business.id, "254700299999")
    new_booking = await repo.create_booking(
        session, business.id, other_customer.id, service.id, start, start + timedelta(minutes=45), 160
    )
    assert new_booking.id != booking.id


async def test_cancel_pending_deposit_booking_no_deposit_note(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=2)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200003", start, paid=False)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={}),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )
    phone = "254700200003"
    await customer_mod.handle_inbound_message(session, business, phone, "cancel my haircut", "cb-secret")
    await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")

    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any("Booking cancelled: Haircut" in t and "Deposit paid" not in t for t in owner_msgs)


async def test_cancel_already_cancelled_booking_is_a_no_op_message(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=2)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200004", start)
    booking.status = BookingStatus.CANCELLED
    await session.flush()

    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, business, "254700200004", "cancel my haircut", "cb-secret"
    )
    assert "don't have any upcoming bookings" in reply


async def test_cancel_with_no_bookings_says_so(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, business, "254700200005", "cancel my booking", "cb-secret"
    )
    assert "don't have any upcoming bookings" in reply


async def test_backing_out_of_cancel_does_not_cancel(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=3)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200006", start)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={}),
            ai.Intent(type=ai.IntentType.CANCEL_ACTION, entities={}),
        ],
    )
    phone = "254700200006"
    await customer_mod.handle_inbound_message(session, business, phone, "cancel my haircut", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "actually no, keep it", "cb-secret")

    assert "won't cancel" in reply.lower()
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.CONFIRMED
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert not any("Booking cancelled" in t for t in owner_msgs)


async def test_cancel_with_multiple_bookings_asks_which_one(session, business, monkeypatch, sent_messages):
    phone = "254700200007"
    start1 = datetime.now() + timedelta(days=2)
    start2 = datetime.now() + timedelta(days=4)
    service = Service(business_id=business.id, name="Haircut", price=800, duration_minutes=30)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, business.id, phone)
    await repo.create_booking(session, business.id, customer.id, service.id, start1, start1 + timedelta(minutes=30), 160)
    await repo.create_booking(session, business.id, customer.id, service.id, start2, start2 + timedelta(minutes=30), 160)

    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={})])
    reply = await customer_mod.handle_inbound_message(session, business, phone, "cancel a booking", "cb-secret")
    assert "1." in reply and "2." in reply
    assert "Which booking" in reply


async def test_numeric_selection_picks_the_right_booking_to_cancel(session, business, monkeypatch, sent_messages):
    phone = "254700200008"
    start1 = datetime.now() + timedelta(days=2)
    start2 = datetime.now() + timedelta(days=4)
    service = Service(business_id=business.id, name="Haircut", price=800, duration_minutes=30)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, business.id, phone)
    b1 = await repo.create_booking(session, business.id, customer.id, service.id, start1, start1 + timedelta(minutes=30), 160)
    b2 = await repo.create_booking(session, business.id, customer.id, service.id, start2, start2 + timedelta(minutes=30), 160)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={}),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )
    await customer_mod.handle_inbound_message(session, business, phone, "cancel a booking", "cb-secret")

    reply = await customer_mod.handle_inbound_message(session, business, phone, "2", "cb-secret")
    assert f"{start2:%d %b}" in reply

    confirm_reply = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")
    assert "cancelled" in confirm_reply.lower()

    refreshed_b1 = await session.get(Booking, b1.id)
    refreshed_b2 = await session.get(Booking, b2.id)
    assert refreshed_b1.status != BookingStatus.CANCELLED
    assert refreshed_b2.status == BookingStatus.CANCELLED


async def test_reschedule_success_path(session, business, monkeypatch, sent_messages):
    start = datetime(2026, 10, 15, 14, 0)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200010", start)
    new_start = start + timedelta(days=1)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.RESCHEDULE_BOOKING, entities={}),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"date_text": new_start.strftime("%d %B %Y"), "time_text": f"{new_start.hour}:00"},
            ),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )
    phone = "254700200010"
    await customer_mod.handle_inbound_message(session, business, phone, "reschedule my haircut", "cb-secret")
    await customer_mod.handle_inbound_message(session, business, phone, "move it", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")

    assert "Done" in reply
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.slot_start.date() == new_start.date()
    assert refreshed.status == BookingStatus.CONFIRMED

    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any("Booking rescheduled: Haircut" in t and phone in t for t in owner_msgs)


async def test_reschedule_conflict_with_existing_booking(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=3)
    start = start.replace(hour=14, minute=0, second=0, microsecond=0)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700200011", start)

    other_customer = await repo.get_or_create_customer(session, business.id, "254700299998")
    blocking_start = start + timedelta(days=1)
    blocking_start = blocking_start.replace(hour=14, minute=0, second=0, microsecond=0)
    other_booking = await repo.create_booking(
        session, business.id, other_customer.id, service.id, blocking_start, blocking_start + timedelta(minutes=45), 160
    )
    other_booking.status = BookingStatus.CONFIRMED
    await session.flush()

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.RESCHEDULE_BOOKING, entities={}),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "date_text": blocking_start.strftime("%d %B %Y"),
                    "time_text": "14:00",
                },
            ),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )
    phone = "254700200011"
    await customer_mod.handle_inbound_message(session, business, phone, "reschedule my haircut", "cb-secret")
    await customer_mod.handle_inbound_message(session, business, phone, "move it", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")

    assert "already booked" in reply.lower() or "someone else" in reply.lower()
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.slot_start == start


async def test_reschedule_respects_business_hours(session, business_with_hours, monkeypatch, sent_messages):
    now = datetime.now()
    days_ahead = (0 - now.weekday()) % 7 or 7
    monday = now + timedelta(days=days_ahead)
    start = monday.replace(hour=10, minute=0, second=0, microsecond=0)

    booking, service, customer = await _make_confirmed_booking(session, business_with_hours, "254700200012", start)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.RESCHEDULE_BOOKING, entities={}),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"date_text": monday.strftime("%d %B %Y"), "time_text": "06:00"},
            ),
        ],
    )
    phone = "254700200012"
    await customer_mod.handle_inbound_message(session, business_with_hours, phone, "reschedule my haircut", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business_with_hours, phone, "6am same day", "cb-secret")

    assert "outside our hours" in reply
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.slot_start == start


async def test_reschedule_not_available_for_orders(session, goods_business, monkeypatch, sent_messages):
    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.RESCHEDULE_BOOKING, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, goods_business, "254700200013", "can I reschedule my order", "cb-secret"
    )
    assert "not available for orders" in reply.lower() or "contact us" in reply.lower()


async def test_customer_cannot_cancel_another_customers_booking(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=3)
    booking, service, customer_a = await _make_confirmed_booking(session, business, "254700300001", start)

    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, business, "254700300002", "cancel my booking", "cb-secret"
    )
    assert "don't have any upcoming bookings" in reply

    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.CONFIRMED


async def test_cancel_scoped_to_business_even_with_same_customer_phone(session, business, goods_business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=3)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700300003", start)

    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CANCEL_BOOKING, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, goods_business, "254700300003", "cancel my booking", "cb-secret"
    )
    assert "don't have any upcoming bookings" in reply
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.CONFIRMED


async def test_cancel_order_success_and_owner_notified(session, goods_business, monkeypatch, sent_messages):
    from app.models import Product
    import json as jsonlib

    product = Product(business_id=goods_business.id, name="Blue Dress", price=2000, stock_qty=5)
    session.add(product)
    await session.flush()
    customer = await repo.get_or_create_customer(session, goods_business.id, "254700400001")

    items_json = jsonlib.dumps([{"product_id": product.id, "qty": 2, "unit_price": 2000}])
    order = await repo.create_order(session, goods_business.id, customer.id, items_json, 4000, 400)
    payment = await repo.create_payment(session, goods_business.id, "idem-order-1", 400)
    payment.status = PaymentStatus.COMPLETED
    order.payment_id = payment.id
    order.status = OrderStatus.CONFIRMED
    await session.flush()

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.CANCEL_ORDER, entities={}),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )
    phone = "254700400001"
    await customer_mod.handle_inbound_message(session, goods_business, phone, "cancel my order", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, goods_business, phone, "yes", "cb-secret")

    assert "cancelled" in reply.lower()
    refreshed = await session.get(Order, order.id)
    assert refreshed.status == OrderStatus.CANCELLED

    owner_msgs = [t for to, t in sent_messages if to == goods_business.owner_whatsapp_number]
    assert any("Order cancelled" in t and phone in t and "Deposit paid: KES 400" in t for t in owner_msgs)


async def test_check_status_lists_upcoming_bookings_and_orders(session, business, monkeypatch, sent_messages):
    start = datetime.now() + timedelta(days=2)
    booking, service, customer = await _make_confirmed_booking(session, business, "254700500001", start)

    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CHECK_STATUS, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, business, "254700500001", "what's the status of my booking", "cb-secret"
    )
    assert "Haircut" in reply
    assert "confirmed" in reply.lower()


async def test_check_status_with_nothing_upcoming(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(monkeypatch, [ai.Intent(type=ai.IntentType.CHECK_STATUS, entities={})])
    reply = await customer_mod.handle_inbound_message(
        session, business, "254700500002", "any updates on my order?", "cb-secret"
    )
    assert "don't have any upcoming" in reply
