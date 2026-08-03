"""Simulate a customer chatting with demo shops using the real LLM (OpenRouter).

Run:
    python -m scripts.seed_demo_shops   # once
    python -m scripts.simulate_customer

Optional:
    python -m scripts.simulate_customer --shop services
    python -m scripts.simulate_customer --shop goods
    python -m scripts.simulate_customer --interactive
"""
import argparse
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from app import repositories as repo
from app.db import get_session, init_db
from app.workflows import customer as customer_mod

SERVICES_PHONE_ID = "demo-services-shop"
GOODS_PHONE_ID = "demo-goods-shop"
CUSTOMER_PHONE = "254712345678"
MPESA_SECRET = "local-test-secret"


def _future_date_label(days_ahead: int = 12) -> str:
    d = datetime.now() + timedelta(days=days_ahead)
    return d.strftime("%A %d %B")


async def _chat(session, business, customer_phone: str, messages: list[str]) -> None:
    print(f"\n{'=' * 60}")
    print(f"Shop: {business.name} ({business.business_type.value}, {business.confirmation_mode.value})")
    print(f"Customer: {customer_phone}")
    print("=" * 60)

    owner_msgs: list[str] = []

    async def _fake_send(biz, to, text):
        if to == business.owner_whatsapp_number:
            owner_msgs.append(text)
            print(f"\n  [OWNER] {text[:200]}{'...' if len(text) > 200 else ''}")
        else:
            print(f"\n  [sent to {to}] {text[:120]}{'...' if len(text) > 120 else ''}")

    deposit_log: list[float] = []

    async def _fake_deposit(session_, biz, phone, amount, secret):
        deposit_log.append(amount)

        class _P:
            id = len(deposit_log)

        print(f"\n  [M-Pesa STK] KES {amount} -> {phone}")
        return _P()

    with patch("app.workflows.customer.send_business_message", _fake_send), patch(
        "app.workflows.owner.send_business_message", _fake_send
    ), patch("app.whatsapp.send_business_message", _fake_send), patch(
        "app.workflows.customer.payments.initiate_deposit", _fake_deposit
    ):
        for i, msg in enumerate(messages, start=1):
            print(f"\nYou: {msg}")
            reply = await customer_mod.handle_inbound_message(
                session, business, customer_phone, msg, MPESA_SECRET
            )
            print(f"Bot: {reply}")

    if deposit_log:
        print(f"\n  Deposits triggered this session: {len(deposit_log)} (KES {deposit_log})")
    if owner_msgs:
        print(f"  Owner notifications: {len(owner_msgs)}")


async def run_scripted(shop: str) -> None:
    await init_db()
    future_day = _future_date_label()

    async with get_session() as session:
        if shop in ("services", "both"):
            business = await repo.get_business_by_phone_number_id(session, SERVICES_PHONE_ID)
            if business is None:
                print("Services shop not found. Run: python -m scripts.seed_demo_shops")
                return
            await _chat(
                session,
                business,
                CUSTOMER_PHONE,
                [
                    "Hi! What services do you offer and how much is a haircut?",
                    f"I'd like to book a haircut on {future_day} at 2pm",
                    "yes",
                ],
            )

        if shop in ("goods", "both"):
            business = await repo.get_business_by_phone_number_id(session, GOODS_PHONE_ID)
            if business is None:
                print("Goods shop not found. Run: python -m scripts.seed_demo_shops")
                return
            await _chat(
                session,
                business,
                "254798765432",
                [
                    "What products do you sell?",
                    "I want to buy 1 Blue Dress (M)",
                    "yes",
                ],
            )


async def run_interactive(shop: str) -> None:
    await init_db()
    async with get_session() as session:
        phone_id = SERVICES_PHONE_ID if shop == "services" else GOODS_PHONE_ID
        business = await repo.get_business_by_phone_number_id(session, phone_id)
        if business is None:
            print("Shop not found. Run: python -m scripts.seed_demo_shops")
            return

        print(f"\nInteractive chat with {business.name}. Type 'quit' to exit.\n")

        async def _fake_send(biz, to, text):
            tag = "OWNER" if to == business.owner_whatsapp_number else to
            print(f"  [{tag}] {text}")

        async def _fake_deposit(session_, biz, phone, amount, secret):
            class _P:
                id = 1

            print(f"  [M-Pesa STK] KES {amount}")
            return _P()

        with patch("app.workflows.customer.send_business_message", _fake_send), patch(
            "app.workflows.customer.payments.initiate_deposit", _fake_deposit
        ):
            while True:
                msg = input("You: ").strip()
                if not msg or msg.lower() in {"quit", "exit", "q"}:
                    break
                reply = await customer_mod.handle_inbound_message(
                    session, business, CUSTOMER_PHONE, msg, MPESA_SECRET
                )
                print(f"Bot: {reply}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate customer chats with demo shops.")
    parser.add_argument(
        "--shop",
        choices=["services", "goods", "both"],
        default="both",
        help="Which demo shop to talk to (default: both, scripted scenario)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Type messages yourself instead of running the scripted demo",
    )
    args = parser.parse_args()

    if args.interactive:
        shop = "services" if args.shop == "both" else args.shop
        asyncio.run(run_interactive(shop))
    else:
        asyncio.run(run_scripted(args.shop))


if __name__ == "__main__":
    main()
