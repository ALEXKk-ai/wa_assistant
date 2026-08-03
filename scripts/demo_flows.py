"""Terminal demo for manual vs automatic booking flows (no WhatsApp/M-Pesa network).

Run from project root:
    python -m scripts.demo_flows

Uses an in-memory DB and stubs outbound messages so you can see the flow in the terminal.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import ai
from app import engine
from app import repositories as repo
from app.models import (
    Base,
    BookingStatus,
    Business,
    BusinessType,
    ConfirmationMode,
    PaymentStatus,
    Service,
)
from app.workflows import customer as customer_mod


def _intent_factory(responses):
    calls = {"i": 0}

    async def _fake(*a, **k):
        r = responses[calls["i"]]
        calls["i"] += 1
        return r

    return _fake


async def _run_demo() -> None:
    sent: list[tuple[str, str]] = []

    async def _fake_send(business, to, text):
        sent.append((to, text))
        print(f"  -> [{to}] {text[:120]}{'...' if len(text) > 120 else ''}")

    engine_db = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine_db.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine_db, expire_on_commit=False, class_=AsyncSession)

    async with factory() as session:
        with patch("app.workflows.customer.send_business_message", _fake_send), patch(
            "app.engine.send_business_message", _fake_send
        ), patch("app.workflows.owner.send_business_message", _fake_send):

            # --- Manual business ---
            manual = Business(
                name="Demo Salon (Manual)",
                business_type=BusinessType.SERVICES,
                whatsapp_phone_number_id="manual-demo",
                owner_whatsapp_number="254700000001",
                deposit_percentage=20,
                confirmation_mode=ConfirmationMode.MANUAL,
            )
            auto = Business(
                name="Demo Salon (Auto)",
                business_type=BusinessType.SERVICES,
                whatsapp_phone_number_id="auto-demo",
                owner_whatsapp_number="254700000002",
                deposit_percentage=20,
                confirmation_mode=ConfirmationMode.AUTOMATIC,
            )
            session.add_all([manual, auto])
            await session.flush()

            for biz in (manual, auto):
                session.add(
                    Service(
                        business_id=biz.id,
                        name="Haircut",
                        price=800,
                        duration_minutes=45,
                    )
                )
            await session.flush()

            slot = datetime.now() + timedelta(days=10)
            slot = slot.replace(hour=14, minute=0, second=0, microsecond=0)
            slot2 = slot + timedelta(days=1)
            date_text = slot.strftime("%d %B %Y")
            date_text2 = slot2.strftime("%d %B %Y")

            print("\n=== 1. MANUAL: two customers, same slot (both allowed) ===\n")
            intents_a = [
                ai.Intent(
                    type=ai.IntentType.BOOK_SERVICE,
                    entities={"service_name": "Haircut", "date_text": date_text, "time_text": "14:00"},
                ),
                ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
            ]
            intents_b = list(intents_a)

            deposit_count = {"n": 0}

            async def _fake_deposit(session_, business_, phone, amount, secret):
                deposit_count["n"] += 1

                class _P:
                    id = deposit_count["n"]

                return _P()

            with patch.object(customer_mod.payments, "initiate_deposit", _fake_deposit):

                with patch.object(ai, "extract_intent", _intent_factory(intents_a)):
                    print("Customer A: book + YES")
                    await customer_mod.handle_inbound_message(
                        session, manual, "254711111111", "haircut", "secret"
                    )
                    await customer_mod.handle_inbound_message(
                        session, manual, "254711111111", "yes", "secret"
                    )

                sent.clear()
                with patch.object(ai, "extract_intent", _intent_factory(intents_b)):
                    print("\nCustomer B: same slot + YES (manual = no block)")
                    await customer_mod.handle_inbound_message(
                        session, manual, "254722222222", "haircut", "secret"
                    )
                    await customer_mod.handle_inbound_message(
                        session, manual, "254722222222", "yes", "secret"
                    )

            print(f"\n  Deposits initiated: {deposit_count['n']} (expect 2)\n")

            print("=== 2. MANUAL: owner soft-reject -> new time, no 2nd deposit ===\n")
            c = await repo.get_or_create_customer(session, manual.id, "254733333333")
            svc = (await repo.list_services(session, manual.id))[0]
            b = await repo.create_booking(
                session,
                manual.id,
                c.id,
                svc.id,
                slot,
                slot + timedelta(minutes=45),
                160,
                skip_conflict_check=True,
            )
            pay = await repo.create_payment(session, manual.id, "demo-pay", 160)
            pay.status = PaymentStatus.COMPLETED
            b.payment_id = pay.id
            b.status = BookingStatus.AWAITING_OWNER_CONFIRMATION
            await session.flush()

            sent.clear()
            print("Owner: REJECT B{id}".format(id=b.id))
            await engine.handle_owner_command(session, manual, f"REJECT B{b.id}")

            deposit_count["n"] = 0
            retry_intents = [
                ai.Intent(
                    type=ai.IntentType.BOOK_SERVICE,
                    entities={"date_text": date_text2, "time_text": "14:00"},
                ),
                ai.Intent(type=ai.IntentType.CONFIRM_ACTION, entities={}),
            ]
            with patch.object(ai, "extract_intent", _intent_factory(retry_intents)):
                print("\nCustomer: new time + YES")
                await customer_mod.handle_inbound_message(
                    session, manual, "254733333333", "new time", "secret"
                )
                await customer_mod.handle_inbound_message(
                    session, manual, "254733333333", "yes", "secret"
                )
            print(f"\n  Second deposits after retry: {deposit_count['n']} (expect 0)\n")

            print("=== 3. AUTO: conflict blocks second booking on same slot ===\n")
            ca = await repo.get_or_create_customer(session, auto.id, "254744444444")
            cb = await repo.get_or_create_customer(session, auto.id, "254755555555")
            svc_a = (await repo.list_services(session, auto.id))[0]
            await repo.create_booking(
                session,
                auto.id,
                ca.id,
                svc_a.id,
                slot,
                slot + timedelta(minutes=45),
                160,
            )
            sent.clear()
            with patch.object(ai, "extract_intent", _intent_factory(intents_b)):
                print("Customer B tries same slot + YES")
                await customer_mod.handle_inbound_message(
                    session, auto, "254755555555", "book", "secret"
                )
                reply = await customer_mod.handle_inbound_message(
                    session, auto, "254755555555", "yes", "secret"
                )
            print(f"\n  Bot reply: {reply}\n")

            print("=== Done. Run `python -m pytest -q` for full automated tests. ===\n")

    await engine_db.dispose()


if __name__ == "__main__":
    asyncio.run(_run_demo())
