import asyncio
from app.db import init_db, get_session
from app.repositories import get_business_by_phone_number_id
from app.security import encrypt_secret

PERMANENT_TOKEN = "EAGJZBt6wwOQUBSAFS7sfdvw3pZBtjQZBjFUdDezgKSMSxVZAsfSTQDk2TMqbFvXIjcspqqi1zlNL2ak8jeBK3AW90QCQmO71Kz5tEkKzvPGuE8qtjuceNTQGdVZBsZCKBPckwNWQtYONI2SyQiwnC46Hx1fIZCaHH3eN0Ph6iCXI54VIGFaj1ckBBvVxhqnkbi8rQZDZD"
PHONE_NUMBER_ID = "1263634996831686"

async def main():
    await init_db()
    async with get_session() as session:
        biz = await get_business_by_phone_number_id(session, PHONE_NUMBER_ID)
        if not biz:
            print(f"Business with phone_number_id '{PHONE_NUMBER_ID}' not found.")
            return
        
        print(f"Found Business ID {biz.id}: '{biz.name}'")
        biz.whatsapp_token_encrypted = encrypt_secret(PERMANENT_TOKEN)
        await session.flush()
        print("Successfully updated and Fernet-encrypted permanent access token!")

if __name__ == "__main__":
    asyncio.run(main())
