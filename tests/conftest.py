import os

os.environ.setdefault("WA_MASTER_KEY", "zH7l3QqQ9r6yV3m1n8p2s5u8x0A2C4E6G8I0K2M4O6Q=")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-app-secret")
os.environ.setdefault("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("MPESA_CALLBACK_SECRET", "test-callback-secret")
os.environ.setdefault("APP_BASE_URL", "http://localhost:8000")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base, Business, BusinessType, ConfirmationMode


@pytest_asyncio.fixture
async def session():
    """A fresh in-memory SQLite DB per test, single shared connection via
    StaticPool (required for :memory: with async SQLite - a new connection
    would otherwise mean a brand new empty database)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def business(session: AsyncSession) -> Business:
    b = Business(
        name="Test Salon",
        business_type=BusinessType.SERVICES,
        whatsapp_phone_number_id="1234567890",
        owner_whatsapp_number="254700000000",
        deposit_percentage=20,
    )
    session.add(b)
    await session.flush()
    return b


@pytest_asyncio.fixture
def sent_messages(monkeypatch):
    """Stubs every module's imported reference to send_business_message so
    tests never hit the real network, and records (to, text) pairs so tests
    can assert on what would have been sent."""
    import app.engine as engine_mod
    import app.whatsapp as whatsapp_mod
    import app.workflows.customer as customer_mod
    import app.workflows.owner as owner_mod

    records: list[tuple[str, str]] = []

    async def _fake_send(business, to, text):
        records.append((to, text))

    monkeypatch.setattr(whatsapp_mod, "send_business_message", _fake_send)
    monkeypatch.setattr(engine_mod, "send_business_message", _fake_send)
    monkeypatch.setattr(customer_mod, "send_business_message", _fake_send)
    monkeypatch.setattr(owner_mod, "send_business_message", _fake_send)

    return records


@pytest_asyncio.fixture
async def goods_business(session: AsyncSession) -> Business:
    b = Business(
        name="Test Boutique",
        business_type=BusinessType.GOODS,
        whatsapp_phone_number_id="0987654321",
        owner_whatsapp_number="254700000001",
        deposit_flat_amount=100,
    )
    session.add(b)
    await session.flush()
    return b


@pytest_asyncio.fixture
async def manual_business(session: AsyncSession) -> Business:
    b = Business(
        name="Manual Confirm Spa",
        business_type=BusinessType.SERVICES,
        whatsapp_phone_number_id="1111111111",
        owner_whatsapp_number="254700000002",
        deposit_percentage=20,
        confirmation_mode=ConfirmationMode.MANUAL,
    )
    session.add(b)
    await session.flush()
    return b


@pytest_asyncio.fixture
async def business_with_hours(session: AsyncSession) -> Business:
    import json

    from app import hours as hours_mod

    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00, Sat 10:00-14:00")
    b = Business(
        name="Hours Salon",
        business_type=BusinessType.SERVICES,
        whatsapp_phone_number_id="2222222222",
        owner_whatsapp_number="254700000003",
        deposit_percentage=20,
        hours_json=json.dumps(hours),
    )
    session.add(b)
    await session.flush()
    return b
