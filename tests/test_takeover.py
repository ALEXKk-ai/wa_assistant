from app import engine


def _wa_payload(phone_number_id: str, sender: str, text: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "messages": [{"type": "text", "from": sender, "text": {"body": text}}],
                        }
                    }
                ]
            }
        ]
    }


async def test_takeover_flag_round_trips(session, business):
    assert await engine._is_under_takeover(session, business.id, "254711111111") is False
    await engine._set_takeover(session, business.id, "254711111111", True)
    assert await engine._is_under_takeover(session, business.id, "254711111111") is True
    await engine._set_takeover(session, business.id, "254711111111", False)
    assert await engine._is_under_takeover(session, business.id, "254711111111") is False


async def test_takeover_phone_normalization(session, business):
    await engine._set_takeover(session, business.id, "0711111111", True)
    assert await engine._is_under_takeover(session, business.id, "254711111111") is True
    assert await engine._is_under_takeover(session, business.id, "+254711111111") is True


async def test_owner_takeover_command_pauses_bot_for_customer(session, business, sent_messages):
    payload = _wa_payload(business.whatsapp_phone_number_id, business.owner_whatsapp_number, "TAKEOVER 254722222222")
    await engine.handle_whatsapp_webhook(session, payload, "cb-secret")

    assert await engine._is_under_takeover(session, business.id, "254722222222") is True
    # Owner gets an acknowledgement.
    assert any(to == business.owner_whatsapp_number for to, _ in sent_messages)


async def test_customer_message_during_takeover_is_forwarded_not_auto_replied(session, business, sent_messages, monkeypatch):
    await engine._set_takeover(session, business.id, "254733333333", True)

    # Even though the LLM would normally handle this, under takeover the
    # customer workflow must never be invoked - patch it to blow up if called.
    async def _should_not_be_called(*args, **kwargs):
        raise AssertionError("customer workflow should not run during takeover")

    import app.workflows.customer as customer_mod

    monkeypatch.setattr(customer_mod, "handle_inbound_message", _should_not_be_called)

    payload = _wa_payload(business.whatsapp_phone_number_id, "254733333333", "hi, is my order ready?")
    await engine.handle_whatsapp_webhook(session, payload, "cb-secret")

    # Message was forwarded to the owner, not auto-replied to the customer.
    assert any(to == business.owner_whatsapp_number and "hi, is my order ready?" in text for to, text in sent_messages)
    assert not any(to == "254733333333" for to, _ in sent_messages)


async def test_owner_release_resumes_bot(session, business, sent_messages):
    await engine._set_takeover(session, business.id, "254744444444", True)
    payload = _wa_payload(business.whatsapp_phone_number_id, business.owner_whatsapp_number, "RELEASE 254744444444")
    await engine.handle_whatsapp_webhook(session, payload, "cb-secret")
    assert await engine._is_under_takeover(session, business.id, "254744444444") is False


async def test_owner_reply_sends_direct_message_to_customer(session, business, sent_messages):
    payload = _wa_payload(
        business.whatsapp_phone_number_id,
        business.owner_whatsapp_number,
        "REPLY 254755555555 We'll have that ready by 5pm",
    )
    await engine.handle_whatsapp_webhook(session, payload, "cb-secret")
    assert ("254755555555", "We'll have that ready by 5pm") in sent_messages


async def test_unrecognized_owner_command_gets_help_text(session, business, sent_messages):
    payload = _wa_payload(business.whatsapp_phone_number_id, business.owner_whatsapp_number, "what's up")
    await engine.handle_whatsapp_webhook(session, payload, "cb-secret")
    owner_texts = [text for to, text in sent_messages if to == business.owner_whatsapp_number]
    assert any("Commands:" in t for t in owner_texts)
