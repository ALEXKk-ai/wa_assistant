import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath("."))
os.environ.setdefault("WA_MASTER_KEY", "zH7l3QqQ9r6yV3m1n8p2s5u8x0A2C4E6G8I0K2M4O6Q=")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-app-secret")
os.environ.setdefault("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("MPESA_CALLBACK_SECRET", "test-callback-secret")
os.environ.setdefault("APP_BASE_URL", "http://localhost:8000")

from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import models, repositories as repo, hours as hours_mod
from app.workflows import customer as customer_mod

async def main():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    hours_dict = hours_mod.parse_hours_spec("Mon-Sat 09:00-18:00")

    async with session_factory() as session:
        business = models.Business(
            name="Bloom Salon",
            business_type=models.BusinessType.SERVICES,
            whatsapp_phone_number_id="test_phone_id",
            owner_whatsapp_number="254700112233",
            confirmation_mode=models.ConfirmationMode.AUTOMATIC,
            hours_json=json.dumps(hours_dict),
        )
        session.add(business)
        await session.commit()
        await session.refresh(business)

        haircut = models.Service(
            business_id=business.id,
            name="Haircut",
            price=1500.0,
            duration_minutes=45,
        )
        session.add(haircut)
        await session.commit()

        # Create an OCCUPIED booking tomorrow at 11:00
        now = datetime.now()
        tomorrow_11 = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
        
        customer1 = await repo.get_or_create_customer(session, business.id, "254700999888", "Existing Client")
        await repo.create_booking(
            session, business.id, customer1.id, haircut.id, tomorrow_11, tomorrow_11 + timedelta(minutes=45), 0.0
        )
        await session.commit()

        customer_phone = "254712345678"
        
        for msg in ["Is 11am open tomorrow?", "What times are booked tomorrow?"]:
            print(f"\n--- Customer Message (Slot Occupied): '{msg}' ---")
            reply = await customer_mod.handle_inbound_message(
                session, business, customer_phone, msg, "test-callback-secret"
            )
            print(f"--- Bot Response ---\n{reply}\n")

if __name__ == "__main__":
    asyncio.run(main())
