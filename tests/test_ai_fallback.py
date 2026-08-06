import pytest

from app import ai


async def test_llm_timeout_degrades_to_fallback(monkeypatch):
    async def _always_times_out(*args, **kwargs):
        raise TimeoutError("simulated LLM timeout")

    monkeypatch.setattr(ai, "_call_llm", _always_times_out)
    settings = ai.get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "")

    intent = await ai.extract_intent(
        customer_message="hi do you have space tomorrow",
        business_name="Test Salon",
        business_type="services",
        catalog=[],
    )

    assert intent.type == ai.IntentType.FALLBACK
    assert "team" in intent.reply_text.lower() or "shortly" in intent.reply_text.lower()


async def test_malformed_json_response_degrades_to_fallback(monkeypatch):
    async def _returns_garbage(*args, **kwargs):
        return "this is not json at all"

    monkeypatch.setattr(ai, "_call_llm", _returns_garbage)
    settings = ai.get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "")

    intent = await ai.extract_intent(
        customer_message="hello",
        business_name="Test Salon",
        business_type="services",
        catalog=[],
    )

    assert intent.type == ai.IntentType.FALLBACK


async def test_unknown_intent_type_degrades_to_fallback(monkeypatch):
    async def _returns_unknown_type(*args, **kwargs):
        return '{"type": "DO_SOMETHING_WEIRD", "entities": {}, "reply_text": "ok"}'

    monkeypatch.setattr(ai, "_call_llm", _returns_unknown_type)
    settings = ai.get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "")

    intent = await ai.extract_intent(
        customer_message="hello",
        business_name="Test Salon",
        business_type="services",
        catalog=[],
    )

    assert intent.type == ai.IntentType.FALLBACK


async def test_successful_response_is_parsed_correctly(monkeypatch):
    async def _returns_valid(*args, **kwargs):
        return '{"type": "LIST_SERVICES", "conversation_act": "QUESTION", "authority_route": "NORMAL", "entities": {}, "reply_text": "here you go"}'

    monkeypatch.setattr(ai, "_call_llm", _returns_valid)

    intent = await ai.extract_intent(
        customer_message="what do you offer",
        business_name="Test Salon",
        business_type="services",
        catalog=[],
    )

    assert intent.type == ai.IntentType.LIST_SERVICES
    assert intent.conversation_act == ai.ConversationAct.QUESTION
    assert intent.authority_route == ai.AuthorityRoute.NORMAL
    assert intent.reply_text == "here you go"


async def test_native_turn_decision_is_parsed(monkeypatch):
    async def _returns_decision(*args, **kwargs):
        return """
        {
          "primary_action": "START_BOOKING",
          "secondary_actions": ["ANSWER_SERVICE_AVAILABILITY"],
          "facts": {
            "service_name": "Haircut",
            "service_names": [],
            "product_name": null,
            "quantity": null,
            "date_text": "tomorrow",
            "time_text": "2pm",
            "payment_phone": null,
            "complaint": false,
            "cancel_signal": false,
            "off_topic": false
          },
          "state_policy": "update_pending",
          "needs_owner": false,
          "confidence": 0.93,
          "reason": "customer wants to book"
        }
        """

    monkeypatch.setattr(ai, "_call_llm", _returns_decision)

    intent, decision = await ai.extract_turn_decision(
        customer_message="book haircut tomorrow at 2pm",
        business_name="Test Salon",
        business_type="services",
        catalog=[{"name": "Haircut", "price": 800, "duration_minutes": 45}],
    )

    assert intent.type == ai.IntentType.BOOK_SERVICE
    assert intent.entities["service_name"] == "Haircut"
    assert intent.entities["time_text"] == "2pm"
    assert decision.primary_action.value == "START_BOOKING"
    assert decision.confidence == 0.93
