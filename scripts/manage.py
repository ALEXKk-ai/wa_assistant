"""Operator CLI for provisioning businesses and their catalogs.

You said sign-up is your job, not the bot's - this is that job, made fast.
No conversation flow, no wizard state machine: just direct, scriptable
commands you run when you onboard a real business.

Usage examples:

    python -m scripts.manage create-business \\
        --name "Jane's Salon" --type services \\
        --whatsapp-phone-number-id 109876543210 \\
        --whatsapp-token "EAAG..." \\
        --owner-whatsapp-number 254712345678 \\
        --mpesa-shortcode 174379 \\
        --mpesa-passkey "bfb279f9aa9bdbcf..." \\
        --mpesa-consumer-key "xxx" --mpesa-consumer-secret "yyy" \\
        --deposit-percentage 20

    python -m scripts.manage add-service --business-id 1 \\
        --name "Haircut" --price 800 --duration-minutes 45

    python -m scripts.manage add-product --business-id 2 \\
        --name "Blue Dress (M)" --price 2500 --stock 10

    python -m scripts.manage list-businesses
"""
import argparse
import asyncio
import json

from app import hours as hours_mod
from app.db import get_session, init_db
from app.models import Business, BusinessType, ConfirmationMode, FulfillmentMode, Product, Service
from app.security import encrypt_secret


async def create_business(args) -> None:
    await init_db()
    try:
        hours = hours_mod.parse_hours_spec(args.hours or "")
    except hours_mod.HoursParseError as exc:
        print(f"Invalid --hours value: {exc}")
        return
    async with get_session() as session:
        business = Business(
            name=args.name,
            business_type=BusinessType(args.type),
            whatsapp_phone_number_id=args.whatsapp_phone_number_id,
            whatsapp_token_encrypted=encrypt_secret(args.whatsapp_token),
            owner_whatsapp_number=args.owner_whatsapp_number,
            mpesa_shortcode=args.mpesa_shortcode or "",
            mpesa_passkey_encrypted=encrypt_secret(args.mpesa_passkey or ""),
            mpesa_consumer_key_encrypted=encrypt_secret(args.mpesa_consumer_key or ""),
            mpesa_consumer_secret_encrypted=encrypt_secret(args.mpesa_consumer_secret or ""),
            deposit_percentage=args.deposit_percentage,
            deposit_flat_amount=args.deposit_flat_amount,
            confirmation_mode=ConfirmationMode(args.confirmation_mode),
            fulfillment_mode=FulfillmentMode(getattr(args, "fulfillment_mode", "both") or "both"),
            hours_json=json.dumps(hours),
            timezone=getattr(args, "timezone", "Africa/Nairobi"),
            address_text=getattr(args, "address", None),
            extra_info_text=getattr(args, "extra_info", None),
        )
        session.add(business)
        await session.flush()
        print(f"Created business id={business.id} name={business.name!r}")
        print(f"Hours: {hours_mod.format_hours(hours)}")


async def update_business_hours(args) -> None:
    try:
        hours = hours_mod.parse_hours_spec(args.hours)
    except hours_mod.HoursParseError as exc:
        print(f"Invalid --hours value: {exc}")
        return
    async with get_session() as session:
        business = await session.get(Business, args.business_id)
        if business is None:
            print(f"No business with id={args.business_id}")
            return
        business.hours_json = json.dumps(hours)
        if args.timezone:
            business.timezone = args.timezone
        await session.flush()
        print(f"Updated hours for business_id={args.business_id}")
        print(f"Hours: {hours_mod.format_hours(hours)}")


async def update_business_info(args) -> None:
    async with get_session() as session:
        business = await session.get(Business, args.business_id)
        if business is None:
            print(f"No business with id={args.business_id}")
            return
        if args.address is not None:
            business.address_text = args.address
        if args.extra_info is not None:
            business.extra_info_text = args.extra_info
        if args.fulfillment_mode is not None:
            business.fulfillment_mode = FulfillmentMode(args.fulfillment_mode)
        await session.flush()
        print(f"Updated info for business_id={args.business_id}")
        if business.address_text:
            print(f"  Address: {business.address_text}")
        if business.extra_info_text:
            print(f"  Extra Info: {business.extra_info_text}")
        print(f"  Fulfillment Mode: {business.fulfillment_mode.value}")


async def add_service(args) -> None:
    async with get_session() as session:
        service = Service(
            business_id=args.business_id,
            name=args.name,
            price=args.price,
            duration_minutes=args.duration_minutes,
        )
        session.add(service)
        await session.flush()
        print(f"Added service id={service.id} to business_id={args.business_id}")


async def add_product(args) -> None:
    async with get_session() as session:
        product = Product(
            business_id=args.business_id,
            name=args.name,
            price=args.price,
            stock_qty=args.stock,
        )
        session.add(product)
        await session.flush()
        print(f"Added product id={product.id} to business_id={args.business_id}")


async def list_businesses(args) -> None:
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(select(Business))
        for b in result.scalars().all():
            print(f"id={b.id}  name={b.name!r}  type={b.business_type.value}  "
                  f"whatsapp_phone_number_id={b.whatsapp_phone_number_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision businesses and catalogs.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-business")
    p.add_argument("--name", required=True)
    p.add_argument("--type", required=True, choices=["services", "goods"])
    p.add_argument("--whatsapp-phone-number-id", required=True)
    p.add_argument("--whatsapp-token", required=True)
    p.add_argument("--owner-whatsapp-number", required=True)
    p.add_argument("--mpesa-shortcode")
    p.add_argument("--mpesa-passkey")
    p.add_argument("--mpesa-consumer-key")
    p.add_argument("--mpesa-consumer-secret")
    p.add_argument("--deposit-percentage", type=float, default=None)
    p.add_argument("--deposit-flat-amount", type=float, default=None)
    p.add_argument(
        "--confirmation-mode",
        choices=["automatic", "manual"],
        default="automatic",
        help="automatic: deposit paid = booking/order confirmed instantly. "
        "manual: deposit paid = awaits your CONFIRM/REJECT reply on WhatsApp.",
    )
    p.add_argument(
        "--fulfillment-mode",
        choices=["delivery_only", "pickup_only", "both"],
        default="both",
        help="For Goods businesses: delivery_only, pickup_only, or both (default: both).",
    )
    p.add_argument("--hours", default="", help="Operating hours, e.g. 'Mon-Fri 09:00-18:00, Sat 10:00-14:00'. Days not mentioned are closed. Omit entirely for no restriction.")
    p.add_argument("--timezone", default="Africa/Nairobi", help="For reference/display only - see app/hours.py.")
    p.add_argument("--address", default=None, help="Physical address, landmark, or location directions.")
    p.add_argument("--extra-info", default=None, help="Custom FAQs, policies, parking notes, payment notes, etc.")
    p.set_defaults(func=create_business)

    p = sub.add_parser("update-business-hours")
    p.add_argument("--business-id", type=int, required=True)
    p.add_argument(
        "--hours",
        required=True,
        help="Operating hours, e.g. 'Mon-Fri 09:00-18:00, Sat 10:00-14:00'. "
        "Pass an empty string ('') to remove all restrictions.",
    )
    p.add_argument("--timezone", default=None, help="Optionally update the stored timezone too.")
    p.set_defaults(func=update_business_hours)

    p = sub.add_parser("update-business-info")
    p.add_argument("--business-id", type=int, required=True)
    p.add_argument("--address", default=None, help="Physical address, landmark, or location directions.")
    p.add_argument("--extra-info", default=None, help="Custom FAQs, policies, parking notes, payment notes, etc.")
    p.add_argument(
        "--fulfillment-mode",
        choices=["delivery_only", "pickup_only", "both"],
        default=None,
        help="Update fulfillment mode: delivery_only, pickup_only, or both.",
    )
    p.set_defaults(func=update_business_info)

    p = sub.add_parser("add-service")
    p.add_argument("--business-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--duration-minutes", type=int, default=60)
    p.set_defaults(func=add_service)

    p = sub.add_parser("add-product")
    p.add_argument("--business-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--stock", type=int, required=True)
    p.set_defaults(func=add_product)

    p = sub.add_parser("list-businesses")
    p.set_defaults(func=list_businesses)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
