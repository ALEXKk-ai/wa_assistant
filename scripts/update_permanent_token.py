import asyncio
import os
import sys

from app.db import get_session, init_db
from app.repositories import get_business_by_phone_number_id
from app.security import encrypt_secret


async def main():
    token = os.environ.get("WHATSAPP_PERMANENT_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or "100000000000000"

    if len(sys.argv) > 1:
        token = sys.argv[1]
    if len(sys.argv) > 2:
        phone_id = sys.argv[2]

    if not token:
        print("Usage: python -m scripts.update_permanent_token <WHATSAPP_TOKEN> [PHONE_NUMBER_ID]")
        print("Or set environment variable WHATSAPP_PERMANENT_TOKEN before running.")
        sys.exit(1)

    await init_db()
    async with get_session() as session:
        biz = await get_business_by_phone_number_id(session, phone_id)
        if not biz:
            print(f"Business with phone_number_id '{phone_id}' not found.")
            return

        print(f"Found Business ID {biz.id}: '{biz.name}'")
        biz.whatsapp_token_encrypted = encrypt_secret(token)
        await session.flush()
        print("Successfully updated and Fernet-encrypted permanent access token!")


if __name__ == "__main__":
    asyncio.run(main())
