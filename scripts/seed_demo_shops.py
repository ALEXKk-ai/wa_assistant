"""Create demo service + goods shops for local testing.

Run:
    python -m scripts.seed_demo_shops
"""
import asyncio
import json

from sqlalchemy import select

from app import hours as hours_mod
from app.db import get_session, init_db
from app.models import Business, BusinessType, ConfirmationMode, Product, Service
from app.security import encrypt_secret

SERVICES_PHONE_ID = "demo-services-shop"
GOODS_PHONE_ID = "demo-goods-shop"
DEV_TOKEN = "dev-whatsapp-token-not-for-production"


async def seed() -> None:
    await init_db()
    hours = hours_mod.parse_hours_spec("Mon-Sat 09:00-18:00")

    async with get_session() as session:
        existing = {
            b.whatsapp_phone_number_id: b
            for b in (await session.execute(select(Business))).scalars().all()
        }

        if SERVICES_PHONE_ID not in existing:
            salon = Business(
                name="Luna Hair Studio",
                business_type=BusinessType.SERVICES,
                whatsapp_phone_number_id=SERVICES_PHONE_ID,
                whatsapp_token_encrypted=encrypt_secret(DEV_TOKEN),
                owner_whatsapp_number="254700100001",
                mpesa_shortcode="174379",
                mpesa_passkey_encrypted=encrypt_secret("dev-passkey"),
                mpesa_consumer_key_encrypted=encrypt_secret("dev-key"),
                mpesa_consumer_secret_encrypted=encrypt_secret("dev-secret"),
                deposit_percentage=20,
                confirmation_mode=ConfirmationMode.MANUAL,
                hours_json=json.dumps(hours),
                timezone="Africa/Nairobi",
            )
            session.add(salon)
            await session.flush()
            session.add_all(
                [
                    Service(business_id=salon.id, name="Haircut", price=800, duration_minutes=45),
                    Service(business_id=salon.id, name="Manicure", price=600, duration_minutes=30),
                    Service(business_id=salon.id, name="Braiding", price=2500, duration_minutes=120),
                ]
            )
            print(f"Created services shop id={salon.id} ({salon.name})")
        else:
            print(f"Services shop already exists id={existing[SERVICES_PHONE_ID].id}")

        if GOODS_PHONE_ID not in existing:
            boutique = Business(
                name="Nairobi Style Boutique",
                business_type=BusinessType.GOODS,
                whatsapp_phone_number_id=GOODS_PHONE_ID,
                whatsapp_token_encrypted=encrypt_secret(DEV_TOKEN),
                owner_whatsapp_number="254700100002",
                mpesa_shortcode="174379",
                mpesa_passkey_encrypted=encrypt_secret("dev-passkey"),
                mpesa_consumer_key_encrypted=encrypt_secret("dev-key"),
                mpesa_consumer_secret_encrypted=encrypt_secret("dev-secret"),
                deposit_percentage=20,
                confirmation_mode=ConfirmationMode.AUTOMATIC,
                hours_json=json.dumps(hours),
                timezone="Africa/Nairobi",
            )
            session.add(boutique)
            await session.flush()
            session.add_all(
                [
                    Product(business_id=boutique.id, name="Blue Dress (M)", price=2500, stock_qty=8),
                    Product(business_id=boutique.id, name="Leather Handbag", price=4500, stock_qty=5),
                    Product(business_id=boutique.id, name="Sneakers (42)", price=3200, stock_qty=12),
                ]
            )
            print(f"Created goods shop id={boutique.id} ({boutique.name})")
        else:
            print(f"Goods shop already exists id={existing[GOODS_PHONE_ID].id}")

        META_PROD_PHONE_ID = "1263634996831686"
        PERM_TOKEN = "EAGJZBt6wwOQUBSAFS7sfdvw3pZBtjQZBjFUdDezgKSMSxVZAsfSTQDk2TMqbFvXIjcspqqi1zlNL2ak8jeBK3AW90QCQmO71Kz5tEkKzvPGuE8qtjuceNTQGdVZBsZCKBPckwNWQtYONI2SyQiwnC46Hx1fIZCaHH3eN0Ph6iCXI54VIGFaj1ckBBvVxhqnkbi8rQZDZD"
        
        meta_shop = existing.get(META_PROD_PHONE_ID)
        if not meta_shop:
            meta_shop = Business(
                name="Luna Hair Studio",
                business_type=BusinessType.SERVICES,
                whatsapp_phone_number_id=META_PROD_PHONE_ID,
                whatsapp_token_encrypted=encrypt_secret(PERM_TOKEN),
                owner_whatsapp_number="254103890536",
                mpesa_shortcode="174379",
                mpesa_passkey_encrypted=encrypt_secret("dev-passkey"),
                mpesa_consumer_key_encrypted=encrypt_secret("dev-key"),
                mpesa_consumer_secret_encrypted=encrypt_secret("dev-secret"),
                deposit_percentage=20,
                confirmation_mode=ConfirmationMode.AUTOMATIC,
                hours_json=json.dumps(hours),
                timezone="Africa/Nairobi",
            )
            session.add(meta_shop)
            await session.flush()
            print(f"Created Meta production shop id={meta_shop.id} ({meta_shop.name})")
        else:
            meta_shop.whatsapp_token_encrypted = encrypt_secret(PERM_TOKEN)
            await session.flush()
            print(f"Updated Meta production shop id={meta_shop.id} ({meta_shop.name}) token")

        # Ensure services exist for meta_shop
        existing_srvs = (await session.execute(select(Service).where(Service.business_id == meta_shop.id))).scalars().all()
        if not existing_srvs:
            session.add_all(
                [
                    Service(business_id=meta_shop.id, name="Haircut", price=1500, duration_minutes=45),
                    Service(business_id=meta_shop.id, name="Hair Coloring", price=4000, duration_minutes=90),
                    Service(business_id=meta_shop.id, name="Braiding", price=2500, duration_minutes=120),
                ]
            )
            await session.flush()
            print(f"Added default services for shop id={meta_shop.id}")

    print("\nShops ready. Run: python -m scripts.simulate_customer")


if __name__ == "__main__":
    asyncio.run(seed())
