from datetime import datetime, timedelta

from app import ai
from app.models import Service
from app.workflows import customer as customer_mod


def _next_monday_str() -> str:
    now = datetime.now()
    days_ahead = (0 - now.weekday()) % 7
    days_ahead = days_ahead or 7
    target = now + timedelta(days=days_ahead)
    return target.strftime("%d %B %Y")


def _mock_extract_intent(monkeypatch, responses):
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(ai, "extract_intent", _fake)
    return calls


async def test_slot_inside_hours_is_accepted(session, business_with_hours, monkeypatch, sent_messages):
    service = Service(business_id=business_with_hours.id, name="Facial", price=1000, duration_minutes=30)
    session.add(service)
    await session.flush()

    monday_str = _next_monday_str()
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Facial", "date_text": monday_str, "time_text": "10:00"},
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business_with_hours, "254700111222", f"facial {monday_str} 10am", "cb-secret"
    )
    assert "Reply YES" in reply
    assert "outside our hours" not in reply


async def test_slot_before_opening_is_rejected(session, business_with_hours, monkeypatch, sent_messages):
    service = Service(business_id=business_with_hours.id, name="Facial", price=1000, duration_minutes=30)
    session.add(service)
    await session.flush()

    monday_str = _next_monday_str()
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Facial", "date_text": monday_str, "time_text": "06:00"},
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business_with_hours, "254700111333", f"facial {monday_str} 6am", "cb-secret"
    )
    assert "outside our hours" in reply


async def test_slot_on_closed_day_is_rejected(session, business_with_hours, monkeypatch, sent_messages):
    service = Service(business_id=business_with_hours.id, name="Facial", price=1000, duration_minutes=30)
    session.add(service)
    await session.flush()

    now = datetime.now()
    days_ahead = (6 - now.weekday()) % 7 or 7  # next Sunday
    sunday = now + timedelta(days=days_ahead)
    sunday_str = sunday.strftime("%d %B %Y")

    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Facial", "date_text": sunday_str, "time_text": "10:00"},
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business_with_hours, "254700111444", f"facial {sunday_str} 10am", "cb-secret"
    )
    assert "closed on Sunday" in reply


async def test_slot_ending_after_closing_is_rejected(session, business_with_hours, monkeypatch, sent_messages):
    # Hours are Mon-Fri 09:00-18:00; a 60-minute service starting at 17:45
    # would end at 18:45, past closing.
    service = Service(business_id=business_with_hours.id, name="Massage", price=2000, duration_minutes=60)
    session.add(service)
    await session.flush()

    monday_str = _next_monday_str()
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Massage", "date_text": monday_str, "time_text": "17:45"},
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business_with_hours, "254700111555", f"massage {monday_str} 5:45pm", "cb-secret"
    )
    assert "past closing time" in reply


async def test_business_without_hours_accepts_any_time(session, business, monkeypatch, sent_messages):
    """The `business` fixture has no hours set (default '{}') - migration
    behavior means no restriction at all, including very early hours."""
    service = Service(business_id=business.id, name="Haircut", price=800, duration_minutes=30)
    session.add(service)
    await session.flush()

    monday_str = _next_monday_str()
    _mock_extract_intent(
        monkeypatch,
        [
            ai.Intent(
                type=ai.IntentType.BOOK_SERVICE,
                entities={"service_name": "Haircut", "date_text": monday_str, "time_text": "03:00"},
            )
        ],
    )

    reply = await customer_mod.handle_inbound_message(
        session, business, "254700111666", f"haircut {monday_str} 3am", "cb-secret"
    )
    assert "Reply YES" in reply
