import json
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.models import Business
from scripts import manage


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _patch_session(monkeypatch, session):
    """scripts/manage.py imported get_session by name, so it must be
    patched on the scripts.manage module itself, not app.db."""
    import scripts.manage as manage_mod

    @asynccontextmanager
    async def _fake_get_session():
        yield session

    async def _noop_init_db():
        return None

    monkeypatch.setattr(manage_mod, "get_session", _fake_get_session)
    monkeypatch.setattr(manage_mod, "init_db", _noop_init_db)


def _create_business_args(**overrides) -> _Args:
    base = dict(
        name="CLI Salon",
        type="services",
        whatsapp_phone_number_id="cli-1",
        whatsapp_token="tok",
        owner_whatsapp_number="254700000010",
        mpesa_shortcode=None,
        mpesa_passkey=None,
        mpesa_consumer_key=None,
        mpesa_consumer_secret=None,
        deposit_percentage=None,
        deposit_flat_amount=None,
        confirmation_mode="automatic",
        hours="",
        timezone="Africa/Nairobi",
    )
    base.update(overrides)
    return _Args(**base)


async def test_create_business_stores_parsed_hours(session, monkeypatch):
    _patch_session(monkeypatch, session)
    args = _create_business_args(hours="Mon-Fri 09:00-18:00")

    await manage.create_business(args)

    result = await session.execute(select(Business).where(Business.whatsapp_phone_number_id == "cli-1"))
    business = result.scalar_one()
    hours = json.loads(business.hours_json)
    assert hours["mon"] == {"open": "09:00", "close": "18:00"}
    assert hours["sun"] is None


async def test_create_business_without_hours_means_no_restriction(session, monkeypatch):
    _patch_session(monkeypatch, session)
    args = _create_business_args(whatsapp_phone_number_id="cli-no-hours", hours="")

    await manage.create_business(args)

    result = await session.execute(select(Business).where(Business.whatsapp_phone_number_id == "cli-no-hours"))
    business = result.scalar_one()
    hours = json.loads(business.hours_json)
    assert all(v is None for v in hours.values())


async def test_create_business_with_bad_hours_does_not_create(session, monkeypatch, capsys):
    _patch_session(monkeypatch, session)
    args = _create_business_args(whatsapp_phone_number_id="cli-bad", hours="Notaday 09:00-18:00")

    await manage.create_business(args)
    captured = capsys.readouterr()
    assert "Invalid --hours" in captured.out

    result = await session.execute(select(Business).where(Business.whatsapp_phone_number_id == "cli-bad"))
    assert result.scalar_one_or_none() is None


async def test_update_business_hours(session, monkeypatch, business):
    _patch_session(monkeypatch, session)
    args = _Args(business_id=business.id, hours="Sat-Sun 10:00-14:00", timezone=None)

    await manage.update_business_hours(args)

    refreshed = await session.get(Business, business.id)
    hours = json.loads(refreshed.hours_json)
    assert hours["sat"] == {"open": "10:00", "close": "14:00"}
    assert hours["sun"] == {"open": "10:00", "close": "14:00"}
    assert hours["mon"] is None


async def test_update_business_hours_bad_input_leaves_business_unchanged(session, monkeypatch, business, capsys):
    _patch_session(monkeypatch, session)
    original_hours = business.hours_json
    args = _Args(business_id=business.id, hours="Whoops 09:00-18:00", timezone=None)

    await manage.update_business_hours(args)
    captured = capsys.readouterr()
    assert "Invalid --hours" in captured.out

    refreshed = await session.get(Business, business.id)
    assert refreshed.hours_json == original_hours


async def test_update_business_hours_unknown_business_id(session, monkeypatch, capsys):
    _patch_session(monkeypatch, session)
    args = _Args(business_id=999999, hours="Mon 09:00-18:00", timezone=None)

    await manage.update_business_hours(args)
    captured = capsys.readouterr()
    assert "No business with id" in captured.out
