from datetime import datetime, timedelta

from app import ai
from app.conversation_decision import DecisionFacts, PrimaryAction, StatePolicy, TurnDecision
from app.workflows import customer as customer_mod
from app.models import Product, Service


async def _add_haircut(session, business, price=800, duration=45):
    service = Service(business_id=business.id, name="Haircut", price=price, duration_minutes=duration)
    session.add(service)
    await session.flush()
    return service


async def _add_manicure(session, business, price=1200, duration=60):
    service = Service(business_id=business.id, name="Manicure", price=price, duration_minutes=duration)
    session.add(service)
    await session.flush()
    return service


async def _add_product(session, business, name="Edge Control", price=500, stock=3):
    product = Product(business_id=business.id, name=name, price=price, stock_qty=stock)
    session.add(product)
    await session.flush()
    return product


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
    hours_reply = await customer_mod.handle_inbound_message(session, business, phone, "what are your hours", "cb-secret")
    assert "hours" in hours_reply.lower() or "open" in hours_reply.lower()

    final_reply = await customer_mod.handle_inbound_message(session, business, phone, "2pm", "cb-secret")
    assert future["day_name"] in final_reply
    assert f"{future['slot'].hour:02d}" in final_reply or str(future['slot'].hour) in final_reply


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

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.QUESTION,
                authority_route=ai.AuthorityRoute.NORMAL,
                entities={},
                reply_text="Here are our listed services: Haircut (KES 800). We currently offer these services!",
            )
        ],
    )

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

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.QUESTION,
                authority_route=ai.AuthorityRoute.NORMAL,
                entities={},
                reply_text="We don't currently list coiled braids, but we offer Braids for KES 2,500!",
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session,
        business,
        "254711116668",
        "Do you offer coiled braids?",
        "cb-secret",
    )

    assert "coiled braids" in reply.lower()
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


async def test_booking_entities_override_false_owner_escalation(session, business, monkeypatch, sent_messages):
    await _add_manicure(session, business)
    future = _future_booking_entities(days_ahead=10, hour=14)
    message = f"Can I come for manicure {future['date_text']} at 2pm"

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.OUT_OF_SCOPE,
                conversation_act=ai.ConversationAct.COMPLAINT,
                authority_route=ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED,
                entities={},
                reply_text="I'm really sorry to hear that! I've passed this directly to the team.",
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business, "254711116673", message, "cb-secret"
    )

    assert "Manicure" in reply
    assert "YES" in reply or "yes" in reply.lower()
    assert "sorry to hear" not in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert not any(message in t for t in owner_msgs)


async def test_off_topic_misclassified_as_cancel_preserves_pending_state(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    future = _future_booking_entities(days_ahead=12, hour=14)

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
            ai.Intent(type=ai.IntentType.CANCEL_ACTION, entities={}, reply_text="No problem, cancelled."),
        ],
    )

    phone = "254711116674"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "what is the weather", "cb-secret")

    assert "still saved" in reply.lower()

    from app import repositories as repo
    import json

    state_row = await repo.get_conversation_state(session, business.id, phone)
    state = json.loads(state_row.state_json)
    assert state["stage"] == customer_mod.STAGE_CONFIRMING
    assert state["pending"].get("type") == "booking"
    assert state["pending"].get("service_name") == "Haircut"


async def test_pending_state_has_strict_form_metadata(session, business, monkeypatch, sent_messages):
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

    phone = "254711116675"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut Thursday", "cb-secret")

    from app import repositories as repo
    import json

    state_row = await repo.get_conversation_state(session, business.id, phone)
    state = json.loads(state_row.state_json)
    pending = state["pending"]
    assert pending["state_version"] == 2
    assert "time_text" in pending["missing_fields"]
    assert "service_id" in pending["locked_fields"]
    assert pending["last_prompt"] == "ask_time"


async def test_complaint_while_pending_escalates_but_preserves_booking(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    future = _future_booking_entities(days_ahead=11, hour=14)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": future["date_text"], "time_text": future["time_text"]},
            ),
            ai.Intent(
                type=ai.IntentType.OUT_OF_SCOPE,
                conversation_act=ai.ConversationAct.COMPLAINT,
                authority_route=ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED,
                entities={},
            ),
        ],
    )

    phone = "254711116676"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "stupid", "cb-secret")

    assert "sorry" in reply.lower() or "team" in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert any("stupid" in t for t in owner_msgs)

    from app import repositories as repo
    import json

    state_row = await repo.get_conversation_state(session, business.id, phone)
    state = json.loads(state_row.state_json)
    assert state["stage"] == customer_mod.STAGE_CONFIRMING
    assert state["pending"].get("type") == "booking"


async def test_change_time_to_12_asks_noon_or_midnight(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    future = _future_booking_entities(days_ahead=13, hour=14)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": future["date_text"], "time_text": future["time_text"]},
            ),
            ai.Intent(type=ai.IntentType.ASK_INFO, entities={}, reply_text="Sure."),
        ],
    )

    phone = "254711116677"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "change the time to 12", "cb-secret")

    assert "noon" in reply.lower()
    assert "midnight" in reply.lower()


async def test_stock_restock_request_answers_and_notifies_owner(session, goods_business, monkeypatch, sent_messages):
    await _add_product(session, goods_business, name="Edge Control", price=450, stock=0)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                entities={"product_name": "Edge Control"},
                reply_text="I don't know.",
            )
        ],
    )

    phone = "254711116678"
    msg = "Do you have edge control in stock? If not WhatsApp me when you restock"
    reply = await customer_mod.handle_inbound_message(session, goods_business, phone, msg, "cb-secret")

    assert "out of stock" in reply.lower()
    assert "restock" in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == goods_business.owner_whatsapp_number]
    assert any(msg in t for t in owner_msgs)


async def test_multi_intent_catalog_question_can_start_booking(session, business, monkeypatch, sent_messages):
    await _add_manicure(session, business)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                entities={},
                reply_text="Yes, we offer manicure.",
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business, "254711116679", "Do you have manicure and can I book tomorrow?", "cb-secret"
    )

    assert "Manicure" in reply
    assert "what time" in reply.lower()


async def test_correction_changes_only_date_and_preserves_time(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    future = _future_booking_entities(days_ahead=14, hour=14)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "18 August 2026", "time_text": "14:00"},
            ),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"date_text": "18 August 2026"},
            ),
        ],
    )

    phone = "254711116680"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "make it 18 August 2026", "cb-secret")

    assert "Haircut" in reply
    assert "14:00" in reply or "2" in reply
    assert "YES" in reply or "yes" in reply.lower()


async def test_vague_change_request_asks_which_field(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)
    future = _future_booking_entities(days_ahead=15, hour=14)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": future["date_text"], "time_text": future["time_text"]},
            ),
            ai.Intent(type=ai.IntentType.ASK_INFO, entities={}, reply_text="Sure."),
        ],
    )

    phone = "254711116681"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "I want to change it", "cb-secret")

    assert "service" in reply.lower()
    assert "date" in reply.lower()
    assert "time" in reply.lower()


async def test_low_confidence_destructive_decision_asks_clarification(session, business, monkeypatch, sent_messages):
    async def _fake_turn_decision(*args, **kwargs):
        return (
            ai.Intent(type=ai.IntentType.CANCEL_ACTION, entities={}, reply_text="cancelled"),
            TurnDecision(
                primary_action=PrimaryAction.CANCEL_PENDING_ACTION,
                facts=DecisionFacts(cancel_signal=True),
                state_policy=StatePolicy.CLEAR_PENDING,
                confidence=0.2,
                reason="uncertain cancel",
            ),
        )

    monkeypatch.setattr(ai, "extract_turn_decision", _fake_turn_decision)

    reply = await customer_mod.handle_inbound_message(
        session, business, "254711116682", "maybe do the thing", "cb-secret"
    )

    assert "clarify" in reply.lower()


async def test_cancel_clears_pending_state(session, business, monkeypatch, sent_messages):
    await _add_haircut(session, business)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "18 August 2026", "time_text": "2pm"},
            ),
            ai.Intent(type=ai.IntentType.CANCEL_ACTION, entities={}),
        ],
    )

    phone = "254711117777"
    await customer_mod.handle_inbound_message(session, business, phone, "haircut 18 Aug 2pm", "cb-secret")
    reply = await customer_mod.handle_inbound_message(session, business, phone, "cancel this request", "cb-secret")

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
            ai.Intent(type=ai.IntentType.RESEND_DEPOSIT, entities={}),
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


async def test_multi_service_booking_aggregates_totals(session, business, monkeypatch):
    from app import models
    await _add_haircut(session, business)
    # Add second service: Hair Coloring (KES 4000, 90 min)
    color_svc = models.Service(
        business_id=business.id,
        name="Hair Coloring",
        price=4000.0,
        duration_minutes=90,
    )
    session.add(color_svc)
    await session.commit()

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "service_names": ["Haircut", "Hair Coloring"],
                    "date_text": "18 August 2026",
                    "time_text": "14:00",
                },
            ),
        ],
    )

    phone = "254799000111"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "haircut and hair coloring 18 Aug at 2", "cb-secret")
    assert "Haircut & Hair Coloring" in reply
    assert "4,800" in reply
    assert "2h 15m" in reply


async def test_phone_number_input_in_confirming_stage_does_not_wipe_state(session, business, monkeypatch):
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
                entities={"service_name": "Haircut", "date_text": "18 August 2026", "time_text": "14:00"},
            ),
            ai.Intent(
                type=ai.IntentType.CONFIRM_ACTION,
                entities={"payment_phone": "0706832905"},
                conversation_act=ai.ConversationAct.UNCERTAIN_ATTENDANCE,  # LLM misclassifies act
            ),
        ],
    )

    phone = "254700112233"
    r1 = await customer_mod.handle_inbound_message(session, business, phone, "haircut Friday 2pm", "cb-secret")
    assert "Haircut" in r1

    # Customer replies with valid phone number while in STAGE_CONFIRMING
    r2 = await customer_mod.handle_inbound_message(session, business, phone, "0706832905", "cb-secret")
    assert "sent" in r2.lower() or "prompt" in r2.lower()
    assert len(stk_calls) == 1
    assert stk_calls[0][1] == "0706832905"


async def test_closed_day_clears_pending_date_text(session, business, monkeypatch):
    import json
    from app import repositories as repo
    await _add_haircut(session, business)
    # Set hours: closed on Sundays
    hours = {
        "mon": {"open": "09:00", "close": "18:00"},
        "tue": {"open": "09:00", "close": "18:00"},
        "wed": {"open": "09:00", "close": "18:00"},
        "thu": {"open": "09:00", "close": "18:00"},
        "fri": {"open": "09:00", "close": "18:00"},
        "sat": {"open": "09:00", "close": "18:00"},
        "sun": None,
    }
    business.hours_json = json.dumps(hours)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "Sunday"},
            ),
        ],
    )

    phone = "254700998877"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "haircut on Sunday", "cb-secret")
    assert "closed on Sundays" in reply.lower() or "closed" in reply.lower()

    state_row = await repo.get_conversation_state(session, business.id, phone)
    state = json.loads(state_row.state_json)
    assert state["pending"].get("date_text") is None


async def test_unlisted_service_does_not_fallback_to_history(session, business, monkeypatch):
    import json
    from app import repositories as repo
    await _add_haircut(session, business)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Manicure", "date_text": "tomorrow"},
            ),
        ],
    )

    phone = "254700998899"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "I want to come for manicure tomorrow", "cb-secret")
    assert "don't offer 'Manicure'" in reply or "don't offer" in reply.lower()
    assert "Haircut" not in reply or "Here are the services we offer:" in reply

    state_row = await repo.get_conversation_state(session, business.id, phone)
    state = json.loads(state_row.state_json)
    assert state["stage"] == customer_mod.STAGE_IDLE
    assert state["pending"] == {}


async def test_ask_info_with_date_does_not_trigger_hours_without_hours_keyword(session, business, monkeypatch):
    from app import ai
    from app.workflows import customer as customer_mod

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                entities={"date_text": "tomorrow"},
                reply_text="We have a full team available for custom requests tomorrow!",
            ),
        ],
    )

    phone = "254700887766"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "I want to come for manicure, pedicure and toes cleaning tomorrow", "cb-secret")
    assert "Yes, we're open on" not in reply
    assert "We have a full team available" in reply


async def test_info_intent_during_active_booking_is_not_overridden_to_booking(session, business, monkeypatch):
    import json
    from app import repositories as repo

    # Set state in active booking stage
    phone = "254712345678"
    await repo.set_conversation_state(
        session,
        business.id,
        phone,
        json.dumps({
            "stage": customer_mod.STAGE_COLLECTING_BOOKING,
            "pending": {"type": "booking", "service_name": "Haircut"},
            "history": [],
        }),
    )

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                entities={"date_text": "Sunday"},
                reply_text="We are closed on Sundays. Our operating hours are Mon-Sat 09:00 - 18:00.",
            ),
        ],
    )

    reply = await customer_mod.handle_inbound_message(session, business, phone, "What time do you close on Sunday?", "cb-secret")
    assert "closed on Sundays" in reply or "hours" in reply.lower()
    assert "Haircut on Saturday" not in reply


async def test_multipart_message_appends_secondary_location_addendum(session, business, monkeypatch):
    business.address_text = "123 Main Street"
    await session.commit()
    await _add_haircut(session, business)
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut"},
            ),
        ],
    )

    phone = "254700112233"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "Where are you located and I'd like to book a haircut", "cb-secret")
    assert "Haircut" in reply
    assert "What date and time" in reply or "what time" in reply.lower()
    assert "We're located at" in reply or "located" in reply.lower()


async def test_social_reply_greeting_returns_welcome_without_owner_alert(session, business, monkeypatch, sent_messages):
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.REQUEST,
            ),
        ],
    )

    phone = "254799887766"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "Hello", "cb-secret")
    assert "Hello" in reply or "Welcome" in reply
    assert "don't have that information" not in reply
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert len(owner_msgs) == 0


async def test_grounded_info_reply_uses_ai_for_custom_faq(session, business, monkeypatch, sent_messages):
    business.extra_info_text = "Free street parking is available behind the salon building."
    await session.commit()

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.ASK_INFO,
                conversation_act=ai.ConversationAct.QUESTION,
                reply_text="Free street parking is available behind the salon building.",
            ),
        ],
    )

    phone = "254788990011"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "Is parking available?", "cb-secret")
    assert "parking is available" in reply.lower()
    owner_msgs = [t for to, t in sent_messages if to == business.owner_whatsapp_number]
    assert len(owner_msgs) == 0


async def test_mixed_service_booking_includes_unlisted_disclaimer_note(session, business, monkeypatch):
    await _add_haircut(session, business)
    from datetime import datetime, timedelta
    start = datetime(2026, 10, 20, 14, 0)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "service_name": "Haircut",
                    "service_names": ["Haircut", "Manicure"],
                    "date_text": start.strftime("%d %B %Y"),
                    "time_text": "14:00",
                },
            ),
        ],
    )

    phone = "254711998877"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "I'd like haircut and manicure on 20 October 2026 at 2pm", "cb-secret")
    assert "Haircut" in reply
    assert "Note: We don't currently offer Manicure" in reply


async def test_resend_deposit_yes_confirmation_triggers_mpesa_payment(session, business, monkeypatch):
    from app import repositories as repo, models
    await _add_haircut(session, business)
    from datetime import datetime, timedelta
    start = datetime(2026, 10, 20, 14, 0)
    end = start + timedelta(hours=1)
    booking = await repo.create_booking(
        session,
        business_id=business.id,
        customer_id=1,
        service_id=1,
        slot_start=start,
        slot_end=end,
        deposit_amount=500.0,
    )
    booking.status = models.BookingStatus.PENDING_DEPOSIT
    customer = await repo.get_or_create_customer(session, business.id, "254700001122")
    booking.customer_id = customer.id
    await session.commit()

    phone = "254700001122"
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(type=ai.IntentType.RESEND_DEPOSIT, entities={}),
            ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
        ],
    )

    # 1. Ask for deposit resend
    r1 = await customer_mod.handle_inbound_message(session, business, phone, "Send another prompt", "cb-secret")
    assert "Reply YES to proceed" in r1

    # 2. Reply YES to confirm resend
    r2 = await customer_mod.handle_inbound_message(session, business, phone, "Yes", "cb-secret")
    assert "M-Pesa prompt" in r2 and "Check your phone" in r2


async def test_manual_confirmation_mode_slot_query_injects_manual_policy(session, business, monkeypatch):
    from app import models
    business.confirmation_mode = models.ConfirmationMode.MANUAL
    await session.commit()

    captured_list = []
    async def _fake_extract(**kwargs):
        captured_list.append(kwargs)
        if len(captured_list) == 1:
            return ai.Intent(type=ai.IntentType.ASK_INFO, reply_text="")
        return ai.Intent(type=ai.IntentType.ASK_INFO, reply_text="Submit your preferred time and our team will confirm!")

    monkeypatch.setattr(ai, "extract_intent", _fake_extract)

    phone = "254711882233"
    reply = await customer_mod.handle_inbound_message(session, business, phone, "Are you free tomorrow at 2pm?", "cb-secret")
    assert "Submit your preferred time" in reply
    grounded_info = captured_list[-1].get("business_extra_info", "")
    assert "Do NOT claim or guarantee that specific time slots are 100% free" in grounded_info


async def test_multi_service_booking_memory_retains_all_services_on_time_reply(session, business, monkeypatch):
    from app import models
    await _add_haircut(session, business)
    # Add Braiding service (2500, 120 min)
    braiding = models.Service(
        business_id=business.id,
        name="Braiding",
        price=2500.0,
        duration_minutes=120,
    )
    session.add(braiding)
    await session.commit()

    from datetime import datetime
    start = datetime(2026, 10, 20, 11, 0)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "service_name": "Haircut",
                    "service_names": ["Haircut", "Braiding"],
                    "date_text": "20 October 2026",
                    "time_text": None,
                },
            ),
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={
                    "service_name": None,
                    "service_names": [],
                    "date_text": None,
                    "time_text": "11",
                },
            ),
        ],
    )

    phone = "254711334455"
    # 1. Ask for Haircut & Braiding
    r1 = await customer_mod.handle_inbound_message(session, business, phone, "I want to book haircut and braiding tomorrow", "cb-secret")
    assert "Haircut & Braiding" in r1

    # 2. Reply with time '11'
    r2 = await customer_mod.handle_inbound_message(session, business, phone, "11", "cb-secret")
    assert "Haircut & Braiding" in r2
    assert "KES 3,300" in r2


async def test_confirming_stage_phone_with_extra_words_and_resend_deposit_fallback(session, business, monkeypatch):
    business.deposit_percentage = 20.0
    business.mpesa_shortcode = "174379"
    await _add_haircut(session, business)
    stk_calls = []

    async def mock_initiate(session, biz, phone, deposit, cb_secret, payment_phone=None):
        stk_calls.append((phone, payment_phone, deposit))
        from app.models import Payment, PaymentStatus
        from decimal import Decimal
        p = Payment(
            business_id=biz.id,
            idempotency_key="idemp_123",
            checkout_request_id="ws_123",
            amount=Decimal(str(deposit)),
            status=PaymentStatus.PENDING,
        )
        session.add(p)
        await session.flush()
        return p

    monkeypatch.setattr("app.payments.initiate_deposit", mock_initiate)

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": "28 October 2026", "time_text": "11:00"},
            ),
            ai.Intent(
                type=ai.IntentType.CONFIRM_ACTION,
                entities={"payment_phone": "0706832905"},
            ),
            ai.Intent(
                type=ai.IntentType.RESEND_DEPOSIT,
                entities={"payment_phone": "0706832905"},
            ),
        ],
    )

    phone = "254700998877"
    # 1. Book Haircut for 28 Oct at 11:00 -> STAGE_CONFIRMING
    r1 = await customer_mod.handle_inbound_message(session, business, phone, "I want to book haircut tomorrow at 11", "cb-secret")
    assert "Haircut" in r1
    assert "Reply YES" in r1

    # 2. Reply with invalid phone + date string ("0706832905 28th")
    r2 = await customer_mod.handle_inbound_message(session, business, phone, "0706832905 28th", "cb-secret")
    assert "invalid" in r2.lower()

    # 3. Correct to valid phone "0706832905", even if LLM misclassifies intent as RESEND_DEPOSIT
    r3 = await customer_mod.handle_inbound_message(session, business, phone, "0706832905", "cb-secret")
    assert "sent" in r3.lower() or "prompt" in r3.lower()
    assert len(stk_calls) == 1
    assert stk_calls[0][1] == "0706832905"



