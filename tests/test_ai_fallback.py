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
        return '{"type": "LIST_SERVICES", "entities": {}, "reply_text": "here you go"}'

    monkeypatch.setattr(ai, "_call_llm", _returns_valid)

    intent = await ai.extract_intent(
        customer_message="what do you offer",
        business_name="Test Salon",
        business_type="services",
        catalog=[],
    )

    assert intent.type == ai.IntentType.LIST_SERVICES
    assert intent.reply_text == "here you go"
