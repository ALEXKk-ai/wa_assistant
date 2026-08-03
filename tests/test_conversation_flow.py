from datetime import datetime, timedelta

from app import ai
from app.workflows import customer as customer_mod
from app.models import Service


async def _add_haircut(session, business, price=800, duration=45):
    service = Service(business_id=business.id, name="Haircut", price=price, duration_minutes=duration)
    session.add(service)
    await session.flush()
    return service


def _mock_extract_intent(monkeypatch, responses):
    """responses: list of ai.Intent, returned in order, one per call."""
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(ai, "extract_intent", _fake)
    return calls


async def test_service_and_date_given_together_only_asks_for_time(session, business, monkeypatch, sent_messages):
    """'I want a haircut on Thursday' should ask ONLY for the time, not
    re-ask for date and time as if nothing was said."""
    await _add_haircut(session, business)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "Thursday", "time_text": None},
            )
        ],
    )

    async def _fake_initiate_deposit(*a, **k):
        raise AssertionError("should not initiate payment yet")

    monkeypatch.setattr(customer_mod.payments, "initiate_deposit", _fake_initiate_deposit)

    reply = await customer_mod.handle_inbound_message(
        session, business, "254711112222", "I want to come for a haircut on Thursday", "cb-secret"
    )

    assert "what time" in reply.lower()
    assert "Thursday" in reply
    assert "what date and time" not in reply.lower()


def _future_booking_entities(days_ahead=14, hour=14, minute=0):
    """Date/time labels guaranteed to be in the future for slot validation."""
    slot = datetime.now() + timedelta(days=days_ahead)
    slot = slot.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return {
        "date_text": slot.strftime("%d %B %Y"),
        "time_text": f"{hour}:{minute:02d}",
        "day_name": slot.strftime("%A"),
        "slot": slot,
    }


async def test_time_only_reply_is_remembered_with_earlier_date(session, business, monkeypatch, sent_messages):
    """Simulates the full two-turn exchange: 'haircut Thursday' then just
    '2pm' - the bot must combine both without the date being re-asked or lost."""
    await _add_haircut(session, business)
    future = _future_booking_entities()

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": future["date_text"], "time_text": None},
            ),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": None, "date_text": None, "time_text": future["time_text"]},
            ),
        ],
    )

    phone = "254711113333"
    first_reply = await customer_mod.handle_inbound_message(
        session, business, phone, "I want to come for a haircut on Thursday", "cb-secret"
    )
    assert "what time" in first_reply.lower()

    second_reply = await customer_mod.handle_inbound_message(session, business, phone, "2pm", "cb-secret")

    assert future["day_name"] in second_reply
    assert f"{future['slot'].hour:02d}" in second_reply or str(future["slot"].hour) in second_reply
    assert "YES" in second_reply or "yes" in second_reply.lower()


async def test_confirming_after_slot_filled_creates_booking(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    business.mpesa_shortcode = "174379"
    future = _future_booking_entities()

    initiate_calls = []

    async def _fake_initiate_deposit(session_, business_, phone, amount, secret, payment_phone=None):
        initiate_calls.append((phone, amount))

        class _FakePayment:
            id = 999

        return _FakePayment()

    monkeypatch.setattr(customer_mod.payments, "initiate_deposit", _fake_initiate_deposit)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "service_name": "Haircut",
                    "date_text": future["date_text"],
                    "time_text": future["time_text"],
                },
            ),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )

    phone = "254711114444"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut thursday 2pm", "cb-secret")
    final_reply = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")

    assert "Haircut" in final_reply and "Booked" in final_reply
    assert len(initiate_calls) == 1
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any("New booking request" in t for t in owner_msgs)


async def test_unrelated_question_mid_booking_does_not_break_pending_state(session, business, monkeypatch, sent_messages):
    """Asking 'what are your hours' mid-booking should get answered on its
    own terms, without losing the day already given for the booking."""
    await _add_haircut(session, business)
    future = _future_booking_entities()

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": future["date_text"], "time_text": None},
            ),
            ai.Intent(type=ai.IntentType.ASK_INFO, entities={}, reply_text="We're open 9am-6pm every day."),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": None, "date_text": None, "time_text": future["time_text"]},
            ),
        ],
    )

    phone = "254711115555"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut thursday", "cb-secret")
    hours_reply = await customer_mod.handle_inbound_message(session, business, phone, "what are your hours?", "cb-secret")
    assert hours_reply == "We're open 9am-6pm every day."

    final_reply = await customer_mod.handle_inbound_message(session, business, phone, "2pm", "cb-secret")
    assert future["day_name"] in final_reply
    assert f"{future['slot'].hour:02d}" in final_reply or str(future["slot"].hour) in final_reply


async def test_out_of_scope_question_is_forwarded_to_owner_not_improvised(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(
        monkeypatch,
        [ai.Intent(type=ai.IntentType.OUT_OF_SCOPE, entities={}, reply_text="")],
    )

    phone = "254711116666"
    question = "Hi, I run a hotel chain and want to discuss a bulk supply partnership"
    reply = await customer_mod.handle_inbound_message(session, business, phone, question, "cb-secret")

    assert "passed it along" in reply.lower() or "team" in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any(question in t for t in owner_msgs), "the actual customer question must reach the owner"


async def test_cancel_clears_pending_state(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "Thursday", "time_text": "2pm"},
            ),
            ai.Intent(type=ai.IntentType.CANCEL_ACTION, entities={}),
        ],
    )

    phone = "254711117777"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut thursday 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "actually never mind", "cb-secret")

    assert "cancelled" in reply.lower()

    from app import repositories as repo
    import json

    state_row = await repo.get_conversation_state(session, business.id, phone)
    state = json.loads(state_row.state_json)
    assert state["stage"] == customer_mod.STAGE_IDLE
    assert state["pending"] == {}


async def test_custom_payment_phone_stk_push(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    business.mpesa_shortcode = "174379"

    stk_calls = []

    async def _fake_initiate_deposit(session_, business_, phone, amount, secret, payment_phone=None):
        stk_calls.append((phone, payment_phone, amount))

        class _FakePayment:
            id = 888

        return _FakePayment()

    monkeypatch.setattr(customer_mod.payments, "initiate_deposit", _fake_initiate_deposit)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "service_name": "Haircut",
                    "date_text": "18 August 2026",
                    "time_text": "14:00",
                    "payment_phone": "0712345678",
                },
            ),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )

    phone = "254711119999"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 18 Aug 2pm pay via 0712345678", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")

    assert "0712345678" in reply
    assert len(stk_calls) == 1
    assert stk_calls[0][1] == "0712345678"
