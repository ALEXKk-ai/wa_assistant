from datetime import datetime, timedelta

from app import ai
from app import engine
from app import repositories as repo
from app.models import Booking, BookingStatus, PaymentStatus, Service


def _stk_success_body(checkout_request_id: str) -> dict:
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "mr-1",
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": 0,
                "ResultDesc": "Success",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 400},
                        {"Name": "MpesaReceiptNumber", "Value": "REC123"},
                    ]
                },
            }
        }
    }


async def _make_business_lookup(session):
    async def _lookup(business_id):
        return await repo.get_business(session, business_id)

    return _lookup


async def test_paid_booking_awaits_owner_confirmation_for_manual_business(session, manual_business, sent_messages):
    service = Service(business_id=manual_business.id, name="Facial", price=2000, duration_minutes=45)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, manual_business.id, "254766666666")
    start = datetime(2026, 9, 1, 10, 0)
    booking = await repo.create_booking(
        session, manual_business.id, customer.id, service.id, start, start + timedelta(minutes=45), 400
    )
    payment = await repo.create_payment(session, manual_business.id, "idem-1", 400)
    await repo.attach_checkout_request_id(session, payment.id, "ws_CO_manual_1", "mr-1")
    booking.payment_id = payment.id
    await session.flush()

    lookup = await _make_business_lookup(session)
    await engine.handle_mpesa_callback(session, _stk_success_body("ws_CO_manual_1"), lookup)

    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.AWAITING_OWNER_CONFIRMATION

    owner_texts = [text for to, text in sent_messages if to == manual_business.owner_whatsapp_number]
    assert any("CONFIRM B" in t for t in owner_texts)


async def test_owner_confirm_finalizes_booking(session, manual_business, sent_messages):
    service = Service(business_id=manual_business.id, name="Facial", price=2000, duration_minutes=45)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, manual_business.id, "254777777777")
    start = datetime(2026, 9, 2, 10, 0)
    booking = await repo.create_booking(
        session, manual_business.id, customer.id, service.id, start, start + timedelta(minutes=45), 400
    )
    payment = await repo.create_payment(session, manual_business.id, "idem-2", 400)
    await repo.attach_checkout_request_id(session, payment.id, "ws_CO_manual_2", "mr-2")
    booking.payment_id = payment.id
    await session.flush()

    lookup = await _make_business_lookup(session)
    await engine.handle_mpesa_callback(session, _stk_success_body("ws_CO_manual_2"), lookup)

    await engine.handle_owner_command(session, manual_business, f"CONFIRM B{booking.id}")

    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.CONFIRMED
    assert any(to == "254777777777" and "confirmed" in text.lower() for to, text in sent_messages)


async def test_owner_reject_soft_reject_keeps_booking_and_prompts_new_time(session, manual_business, sent_messages):
    service = Service(business_id=manual_business.id, name="Facial", price=2000, duration_minutes=45)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, manual_business.id, "254788888888")
    start = datetime(2026, 9, 3, 10, 0)
    booking = await repo.create_booking(
        session, manual_business.id, customer.id, service.id, start, start + timedelta(minutes=45), 400
    )
    payment = await repo.create_payment(session, manual_business.id, "idem-3", 400)
    payment.status = PaymentStatus.COMPLETED
    await repo.attach_checkout_request_id(session, payment.id, "ws_CO_manual_3", "mr-3")
    booking.payment_id = payment.id
    booking.status = BookingStatus.AWAITING_OWNER_CONFIRMATION
    await session.flush()

    await engine.handle_owner_command(session, manual_business, f"DECLINE B{booking.id}")

    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.AWAITING_OWNER_CONFIRMATION
    customer_msgs = [text for to, text in sent_messages if to == "254788888888"]
    assert any("another date and time" in t.lower() for t in customer_msgs)
    assert any("deposit is still" in t.lower() for t in customer_msgs)
    assert not any("refunded" in t.lower() for t in customer_msgs)


async def test_confirming_wrong_status_booking_is_a_no_op_with_explanation(session, business, sent_messages):
    """business (not manual_business) uses AUTOMATIC mode - a booking there
    never enters AWAITING_OWNER_CONFIRMATION, so CONFIRM should explain that
    rather than silently doing nothing or crashing."""
    service = Service(business_id=business.id, name="Haircut", price=800, duration_minutes=30)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, business.id, "254799999999")
    start = datetime(2026, 9, 4, 10, 0)
    booking = await repo.create_booking(
        session, business.id, customer.id, service.id, start, start + timedelta(minutes=30), 160
    )
    await session.flush()

    await engine.handle_owner_command(session, business, f"CONFIRM B{booking.id}")

    owner_texts = [text for to, text in sent_messages if to == business.owner_whatsapp_number]
    assert any("isn't awaiting confirmation" in t for t in owner_texts)


async def test_confirm_with_unknown_ref_is_handled_gracefully(session, business, sent_messages):
    await engine.handle_owner_command(session, business, "CONFIRM B99999")
    owner_texts = [text for to, text in sent_messages if to == business.owner_whatsapp_number]
    assert any("No booking" in t for t in owner_texts)


async def test_manual_mode_allows_overlapping_slot_requests(session, manual_business, monkeypatch, sent_messages):
    service = Service(business_id=manual_business.id, name="Haircut", price=800, duration_minutes=45)
    session.add(service)
    await session.flush()
    start = datetime.now() + timedelta(days=5)
    start = start.replace(hour=10, minute=0, second=0, microsecond=0)

    c1 = await repo.get_or_create_customer(session, manual_business.id, "254711111111")
    c2 = await repo.get_or_create_customer(session, manual_business.id, "254722222222")
    await repo.create_booking(
        session, manual_business.id, c1.id, service.id, start, start + timedelta(minutes=45), 160,
        skip_conflict_check=True,
    )
    b2 = await repo.create_booking(
        session, manual_business.id, c2.id, service.id, start, start + timedelta(minutes=45), 160,
        skip_conflict_check=True,
    )
    assert b2.id is not None


async def test_manual_soft_reject_time_retry_without_second_deposit(session, manual_business, monkeypatch, sent_messages):
    from app.workflows import customer as customer_mod

    service = Service(business_id=manual_business.id, name="Haircut", price=800, duration_minutes=45)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, manual_business.id, "254733333333")
    start = datetime.now() + timedelta(days=7)
    start = start.replace(hour=10, minute=0, second=0, microsecond=0)
    new_start = start + timedelta(days=1)

    booking = await repo.create_booking(
        session, manual_business.id, customer.id, service.id, start, start + timedelta(minutes=45), 160,
        skip_conflict_check=True,
    )
    payment = await repo.create_payment(session, manual_business.id, "idem-retry", 160)
    payment.status = PaymentStatus.COMPLETED
    booking.payment_id = payment.id
    booking.status = BookingStatus.AWAITING_OWNER_CONFIRMATION
    await session.flush()

    await engine.handle_owner_command(session, manual_business, f"DECLINE B{booking.id}")

    deposit_calls = []

    async def _fake_deposit(*a, **k):
        deposit_calls.append(1)
        raise AssertionError("should not charge deposit again")

    monkeypatch.setattr(customer_mod.payments, "initiate_deposit", _fake_deposit)

    calls = {"n": 0}

    async def _fake_intent(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            return ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "date_text": new_start.strftime("%d %B %Y"),
                    "time_text": "10:00",
                },
            )
        return ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={})

    monkeypatch.setattr(ai, "extract_intent", _fake_intent)

    phone = "254733333333"
    await customer_mod.handle_inbound_message(session, manual_business, phone, "10am next day", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, manual_business, phone, "yes", "cb-secret")

    assert not deposit_calls
    assert "deposit is still" in reply.lower() or "confirmation" in reply.lower()
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.slot_start.date() == new_start.date()
    assert refreshed.status == BookingStatus.AWAITING_OWNER_CONFIRMATION


async def test_manual_reschedule_awaits_owner_confirmation(session, manual_business, monkeypatch, sent_messages):
    from app.workflows import customer as customer_mod

    start = datetime.now() + timedelta(days=4)
    start = start.replace(hour=11, minute=0, second=0, microsecond=0)
    new_start = start + timedelta(days=2)

    booking, service, customer = await _make_confirmed_booking_manual(
        session, manual_business, "254744444444", start
    )

    _mock = _mock_extract_intent_helper(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.RESCHEDULE_BOOKING, entities={}),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"date_text": new_start.strftime("%d %B %Y"), "time_text": "11:00"},
            ),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )

    phone = "254744444444"
    await customer_mod.handle_inbound_message(session, manual_business, phone, "reschedule", "cb-secret")
    await customer_mod.handle_inbound_message(session, manual_business, phone, "new time", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, manual_business, phone, "yes", "cb-secret")

    assert "sent to the team" in reply.lower() or "confirmation" in reply.lower()
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.slot_start == start
    assert refreshed.proposed_slot_start is not None
    assert refreshed.status == BookingStatus.AWAITING_RESCHEDULE_CONFIRMATION

    await engine.handle_owner_command(session, manual_business, f"CONFIRM B{booking.id}")
    refreshed = await session.get(Booking, booking.id)
    assert refreshed.status == BookingStatus.CONFIRMED
    assert refreshed.slot_start.date() == new_start.date()


async def _make_confirmed_booking_manual(session, business, phone, start, duration=45):
    service = Service(business_id=business.id, name="Haircut", price=800, duration_minutes=duration)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, business.id, phone)
    booking = await repo.create_booking(
        session, business.id, customer.id, service.id, start, start + timedelta(minutes=duration), 160,
        skip_conflict_check=True,
    )
    payment = await repo.create_payment(session, business.id, f"idem-{booking.id}", 160)
    payment.status = PaymentStatus.COMPLETED
    booking.payment_id = payment.id
    booking.status = BookingStatus.CONFIRMED
    await session.flush()
    return booking, service, customer


def _mock_extract_intent_helper(monkeypatch, responses):
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(ai, "extract_intent", _fake)
    return calls
