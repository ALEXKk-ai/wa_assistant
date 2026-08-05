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


async def test_other_services_question_gets_compact_catalog_not_owner_escalation(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)

    async def _fail_extract_intent(*args, **kwargs):
        raise AssertionError("other-services question should be answered from the catalog before the LLM")

    monkeypatch.setattr(ai, "extract_intent", _fail_extract_intent)

    phone = "254711116667"
    question = "Which other services do you offer apart from the ones listed?"
    reply = await customer_mod.handle_inbound_message(session, business, phone, question, "cb-secret")

    assert "haircut" in reply.lower()
    assert "currently offer" in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert owner_msgs == []


async def test_specific_unlisted_service_variant_is_not_assumed_available(session, business, monkeypatch, sent_messages):
    from app.models import Service

    session.add(Service(business_id=business.id, name="Braids", price=2500, duration_minutes=120))
    await session.flush()

    async def _fail_extract_intent(*args, **kwargs):
        raise AssertionError("availability question should be answered from the catalog before the LLM")

    monkeypatch.setattr(ai, "extract_intent", _fail_extract_intent)

    reply = await customer_mod.handle_inbound_message(
        session,
        business,
        "254711116668",
        "Do you offer coiled braids?",
        "cb-secret",
    )

    assert "don't currently list coiled braids" in reply.lower()
    assert sent_messages == []


async def test_acknowledgement_gets_short_social_reply(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.ACKNOWLEDGEMENT,
                entities={},
                reply_text="",
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business, "254711116669", "thank you", "cb-secret"
    )

    assert reply == "You're welcome."
    assert sent_messages == []


async def test_uncertain_attendance_asks_cancel_or_reschedule_without_acting(session, business, monkeypatch, sent_messages):
    from app import repositories as repo

    service = await _add_haircut(session, business)
    customer = await repo.get_or_create_customer(session, business.id, "254711116670")
    slot = datetime.now() + timedelta(days=1)
    slot = slot.replace(hour=14, minute=0, second=0, microsecond=0)
    booking = await repo.create_booking(
        session,
        business.id,
        customer.id,
        service.id,
        slot,
        slot + timedelta(minutes=service.duration_minutes),
        100,
    )

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.UNCERTAIN_ATTENDANCE,
                entities={"date_text": "tomorrow"},
                reply_text="",
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business, customer.phone_number, "I don't think I'll make it tomorrow", "cb-secret"
    )

    assert "cancel or reschedule" in reply.lower()
    assert booking.status.value == "pending_deposit"
    assert sent_messages == []


async def test_owner_authority_route_escalates_even_with_ai_reply_text(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.PROPOSAL,
                authority_route=ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED,
                entities={},
                reply_text="Sure, we can partner with you.",
            )
        ],
    )

    phone = "254711116671"
    question = "I want to discuss a new commercial arrangement with your shop"
    reply = await customer_mod.handle_inbound_message(session, business, phone, question, "cb-secret")

    assert "passed" in reply.lower() or "team" in reply.lower()
    assert "partner with you" not in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any(question in t for t in owner_msgs)


async def test_owner_authority_route_does_not_require_regex_keyword(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.HUMAN_REQUEST,
                authority_route=ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED,
                entities={},
                reply_text="I can make that happen.",
            )
        ],
    )

    phone = "254711116672"
    question = "Could someone review my situation from yesterday?"
    reply = await customer_mod.handle_inbound_message(session, business, phone, question, "cb-secret")

    assert "passed" in reply.lower() or "team" in reply.lower()
    assert "make that happen" not in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any(question in t for t in owner_msgs)


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


async def test_resend_prompt_asks_confirmation(session, business, monkeypatch):
    await _add_haircut(session, business)
    business.mpesa_shortcode = "174379"

    stk_calls = []

    async def _fake_initiate_deposit(session_, business_, phone, amount, secret, payment_phone=None):
        stk_calls.append((phone, payment_phone, amount))

        class _FakePayment:
            id = 777

        return _FakePayment()

    monkeypatch.setattr(customer_mod.payments, "initiate_deposit", _fake_initiate_deposit)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "18 August 2026", "time_text": "14:00"},
            ),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )

    phone = "254722000111"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 18 Aug 2pm", "cb-secret")
    await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")
    assert len(stk_calls) == 1

    # Customer asks for resend
    reply1 = await customer_mod.handle_inbound_message(session, business, phone, "resend prompt", "cb-secret")
    assert "Would you like me to send" in reply1
    assert "254722000111" in reply1

    # Customer confirms YES
    reply2 = await customer_mod.handle_inbound_message(session, business, phone, "yes", "cb-secret")
    assert "sent a new M-Pesa prompt" in reply2
    assert len(stk_calls) == 2


async def test_non_text_message_handling(session, business, sent_messages):
    from app import engine
    # Audio/voice note
    res_voice = await engine.handle_non_text_message(session, business, "254711223344", "audio")
    assert "can't listen to voice notes" in res_voice.lower()

    # Image photo
    res_image = await engine.handle_non_text_message(session, business, "254711223344", "image")
    assert "forwarded it to the shop owner" in res_image.lower()
    assert any("sent a photo" in text for to, text in sent_messages if to == business.owner_whatsapp_number)
