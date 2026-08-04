"""Script to test live Daraja M-Pesa Sandbox STK Push requests.

Usage:
    python -m scripts.test_live_mpesa_sandbox <phone_number> <amount>

Example:
    python -m scripts.test_live_mpesa_sandbox 254708374149 100
"""
import asyncio
import os
import sys

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Business, BusinessType, ConfirmationMode
from app.payments import initiate_deposit
from app.security import encrypt_secret


async def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else "254708374149"
    amount = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0

    print(f"--- Testing Live M-Pesa STK Push ---")
    print(f"Target Phone: {phone}")
    print(f"Amount: KES {amount}")

    await init_db()
    async with SessionLocal() as session:
        result = await session.execute(select(Business).where(Business.name == "bloom salon"))
        business = result.scalars().first()
        if not business:
            business = Business(
                name="bloom salon",
                business_type=BusinessType.SERVICES,
                whatsapp_phone_number_id="1263634996831686",
                whatsapp_token_encrypted=encrypt_secret("dev-token"),
                owner_whatsapp_number="254103890536",
                mpesa_shortcode="174379",
                mpesa_passkey_encrypted=encrypt_secret("bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"),
                mpesa_consumer_key_encrypted=encrypt_secret("Z7UdM0bHvqVRV6WICRT6oLXzgtCMDbsWxLbTUn2drcZIPsWu"),
                mpesa_consumer_secret_encrypted=encrypt_secret("odG2TYC4nMRrz9LixCDdU07BuLf5nNApolrmSDeUs32sZUpAFGVom1PJPcAIDKOE"),
                deposit_percentage=20,
                confirmation_mode=ConfirmationMode.AUTOMATIC,
            )
            session.add(business)
            await session.flush()
        else:
            business.mpesa_consumer_key_encrypted = encrypt_secret("Z7UdM0bHvqVRV6WICRT6oLXzgtCMDbsWxLbTUn2drcZIPsWu")
            business.mpesa_consumer_secret_encrypted = encrypt_secret("odG2TYC4nMRrz9LixCDdU07BuLf5nNApolrmSDeUs32sZUpAFGVom1PJPcAIDKOE")
            business.mpesa_passkey_encrypted = encrypt_secret("bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919")
            await session.flush()

        try:
            payment = await initiate_deposit(
                session, business, phone, amount, callback_path_secret="mpesa_callback_secret_12345"
            )
            print(f"\n[OK] Payment row created id={payment.id}, status={payment.status.value}")
            if payment.checkout_request_id:
                print(f"[OK] STK Push Sent! CheckoutRequestID: {payment.checkout_request_id}")
                print(f"MerchantRequestID: {payment.merchant_request_id}")
            else:
                print("[INFO] STK Push initiated (pending checkout request ID assignment)")
        except Exception as err:
            print(f"\n[ERROR] Error during STK Push: {err}")


if __name__ == "__main__":
    asyncio.run(main())
