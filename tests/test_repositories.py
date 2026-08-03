from datetime import datetime, timedelta

import pytest

from app import repositories as repo
from app.models import Business, BusinessType, Service
from app.repositories import BookingConflictError


async def test_services_scoped_to_business(session, business, goods_business):
    """Two businesses' catalogs never leak into each other's listing."""
    session.add(Service(business_id=business.id, name="Haircut", price=500, duration_minutes=30))
    session.add(Service(business_id=goods_business.id, name="Should not appear", price=1, duration_minutes=1))
    await session.flush()

    services = await repo.list_services(session, business.id)
    assert [s.name for s in services] == ["Haircut"]


async def test_get_or_create_customer_is_scoped_per_business(session, business, goods_business):
    c1 = await repo.get_or_create_customer(session, business.id, "254711111111")
    c2 = await repo.get_or_create_customer(session, goods_business.id, "254711111111")
    # Same phone number, different businesses -> different customer rows.
    assert c1.id != c2.id
    assert c1.business_id == business.id
    assert c2.business_id == goods_business.id


async def test_double_booking_same_slot_is_rejected(session, business):
    service = Service(business_id=business.id, name="Massage", price=2000, duration_minutes=60)
    session.add(service)
    await session.flush()
    customer_a = await repo.get_or_create_customer(session, business.id, "254722222222")
    customer_b = await repo.get_or_create_customer(session, business.id, "254733333333")

    start = datetime(2026, 8, 25, 14, 0)
    end = start + timedelta(minutes=60)

    await repo.create_booking(session, business.id, customer_a.id, service.id, start, end, 400)

    with pytest.raises(BookingConflictError):
        await repo.create_booking(session, business.id, customer_b.id, service.id, start, end, 400)


async def test_overlapping_slot_is_also_rejected(session, business):
    service = Service(business_id=business.id, name="Massage", price=2000, duration_minutes=60)
    session.add(service)
    await session.flush()
    customer = await repo.get_or_create_customer(session, business.id, "254722222222")

    start = datetime(2026, 8, 25, 14, 0)
    end = start + timedelta(minutes=60)
    await repo.create_booking(session, business.id, customer.id, service.id, start, end, 400)

    # Overlapping (not identical) slot should also be rejected.
    overlap_start = datetime(2026, 8, 25, 14, 30)
    overlap_end = overlap_start + timedelta(minutes=60)
    with pytest.raises(BookingConflictError):
        await repo.create_booking(
            session, business.id, customer.id, service.id, overlap_start, overlap_end, 400
        )
