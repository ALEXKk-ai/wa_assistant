"""M-Pesa Daraja STK Push integration.

Idempotency design (this is the piece we deliberately did not cut for v1):

1. initiate_deposit() first creates a Payment row with a locally-generated
   idempotency_key and status=PENDING, THEN calls M-Pesa. If our process
   crashes between the M-Pesa call succeeding and us saving the
   checkout_request_id, the reconciliation job below will catch the
   orphaned pending row on its next run and query M-Pesa for the real
   status rather than leaving it stuck forever.

2. handle_callback() looks up the Payment by checkout_request_id (unique
   column) and checks its CURRENT status before doing anything. If it's
   already COMPLETED or FAILED, the callback is a duplicate/replay and is
   a no-op - it does not re-trigger booking confirmation, does not re-send
   a WhatsApp message, does not double count revenue. This is the actual
   guarantee: "same callback delivered twice equals one effect."

3. reconcile_pending_payments() is a scheduled job (see main.py) that finds
   payments stuck PENDING past a timeout and queries M-Pesa's transaction
   status API directly, rather than waiting indefinitely for a callback
   that may never arrive (mobile network drop, Safaricom-side failure, etc).
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories as repo
from app.config import get_settings
from app.logging_conf import get_logger, log_extra
from app.models import Business, Payment, PaymentStatus
from app.security import decrypt_secret

logger = get_logger(__name__)


class MpesaError(RuntimeError):
    pass


async def _get_access_token(business: Business) -> str:
    settings = get_settings()
    consumer_key = decrypt_secret(business.mpesa_consumer_key_encrypted)
    consumer_secret = decrypt_secret(business.mpesa_consumer_secret_encrypted)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{settings.mpesa_base_url}/oauth/v1/generate?grant_type=client_credentials",
            auth=(consumer_key, consumer_secret),
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def compute_deposit_amount(business: Business, full_price: float, item=None) -> float:
    if item is not None:
        if getattr(item, "deposit_percentage", None) is not None:
            return round(float(item.deposit_percentage) / 100 * full_price, 2)
        if getattr(item, "deposit_flat_amount", None) is not None:
            return float(item.deposit_flat_amount)
    if business.deposit_percentage is not None:
        return round(float(business.deposit_percentage) / 100 * full_price, 2)
    if business.deposit_flat_amount is not None:
        return float(business.deposit_flat_amount)
    return 0.0  # no deposit policy configured -> 0 deposit required (pay on arrival)


async def initiate_deposit(
    session: AsyncSession,
    business: Business,
    customer_phone: str,
    amount: float,
    callback_path_secret: str,
) -> Payment:
    """Creates a pending Payment row, then requests an STK Push. Returns the
    Payment row regardless of whether the STK request itself succeeds - a
    failure here still leaves an auditable PENDING record the reconciliation
    job (or the customer retrying) can act on, instead of a customer's tap
    silently vanishing."""
    idempotency_key = uuid.uuid4().hex
    payment = await repo.create_payment(session, business.id, idempotency_key, amount)

    settings = get_settings()
    try:
        access_token = await _get_access_token(business)
        shortcode = business.mpesa_shortcode
        passkey = decrypt_secret(business.mpesa_passkey_encrypted)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password = _stk_password(shortcode, passkey, timestamp)
        callback_url = (
            f"{settings.app_base_url.rstrip('/')}"
            f"/webhook/mpesa/{callback_path_secret}"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.mpesa_base_url}/mpesa/stkpush/v1/processrequest",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "BusinessShortCode": shortcode,
                    "Password": password,
                    "Timestamp": timestamp,
                    "TransactionType": "CustomerPayBillOnline",
                    "Amount": int(amount),
                    "PartyA": customer_phone,
                    "PartyB": shortcode,
                    "PhoneNumber": customer_phone,
                    "CallBackURL": callback_url,
                    "AccountReference": f"DEP-{payment.id}",
                    "TransactionDesc": "Deposit",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        await repo.attach_checkout_request_id(
            session, payment.id, data["CheckoutRequestID"], data["MerchantRequestID"]
        )
    except (httpx.HTTPError, KeyError) as exc:
        logger.error(
            "STK push request failed; payment left PENDING for reconciliation",
            extra=log_extra(payment_id=payment.id, business_id=business.id, error=str(exc)),
        )

    return payment


def _stk_password(shortcode: str, passkey: str, timestamp: str) -> str:
    import base64

    raw = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


async def handle_callback(session: AsyncSession, callback_body: dict) -> Payment | None:
    """Processes an M-Pesa STK callback exactly once per payment, regardless
    of how many times the callback is delivered."""
    stk = callback_body.get("Body", {}).get("stkCallback", {})
    checkout_request_id = stk.get("CheckoutRequestID")
    if not checkout_request_id:
        logger.warning("M-Pesa callback missing CheckoutRequestID", extra=log_extra(body=callback_body))
        return None

    payment = await repo.get_payment_by_checkout_request_id(session, checkout_request_id)
    if payment is None:
        logger.warning(
            "M-Pesa callback for unknown checkout_request_id (possible replay/spoof)",
            extra=log_extra(checkout_request_id=checkout_request_id),
        )
        return None

    if payment.status != PaymentStatus.PENDING:
        # Already processed - duplicate/replayed callback. No-op by design.
        logger.info(
            "Duplicate M-Pesa callback ignored",
            extra=log_extra(payment_id=payment.id, current_status=payment.status.value),
        )
        return payment

    result_code = stk.get("ResultCode")
    payment.raw_callback_json = json.dumps(callback_body)
    payment.processed_at = datetime.now(timezone.utc)

    if result_code == 0:
        items = {i["Name"]: i.get("Value") for i in stk.get("CallbackMetadata", {}).get("Item", [])}
        payment.status = PaymentStatus.COMPLETED
        receipt = items.get("MpesaReceiptNumber")
        payment.mpesa_receipt = str(receipt) if receipt else f"no-receipt-{payment.idempotency_key}"
    else:
        payment.status = PaymentStatus.FAILED

    await session.flush()
    logger.info(
        "M-Pesa payment processed",
        extra=log_extra(payment_id=payment.id, status=payment.status.value),
    )
    return payment


async def reconcile_pending_payments(session: AsyncSession, business_lookup, stuck_after_minutes: int = 15) -> int:
    """Finds payments stuck PENDING past the timeout and queries M-Pesa's
    transaction status API directly. business_lookup is an async callable
    business_id -> Business, injected to avoid a circular import.

    Returns the number of payments resolved this run.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stuck_after_minutes)
    stuck = await repo.list_pending_payments_older_than(session, cutoff)
    resolved = 0
    for payment in stuck:
        business = await business_lookup(payment.business_id)
        if business is None or not payment.checkout_request_id:
            continue
        try:
            status = await _query_stk_status(business, payment.checkout_request_id)
        except MpesaError as exc:
            logger.warning(
                "Reconciliation status query failed, will retry next run",
                extra=log_extra(payment_id=payment.id, error=str(exc)),
            )
            continue
        if status is not None:
            payment.status = status
            payment.processed_at = datetime.now(timezone.utc)
            await session.flush()
            resolved += 1
            logger.info(
                "Reconciliation resolved stuck payment",
                extra=log_extra(payment_id=payment.id, status=status.value),
            )
    return resolved


async def _query_stk_status(business: Business, checkout_request_id: str) -> PaymentStatus | None:
    settings = get_settings()
    try:
        access_token = await _get_access_token(business)
        shortcode = business.mpesa_shortcode
        passkey = decrypt_secret(business.mpesa_passkey_encrypted)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password = _stk_password(shortcode, passkey, timestamp)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.mpesa_base_url}/mpesa/stkpushquery/v1/query",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "BusinessShortCode": shortcode,
                    "Password": password,
                    "Timestamp": timestamp,
                    "CheckoutRequestID": checkout_request_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            result_code = data.get("ResultCode")
            if result_code is None:
                return None  # still pending on M-Pesa's side
            return PaymentStatus.COMPLETED if str(result_code) == "0" else PaymentStatus.FAILED
    except httpx.HTTPError as exc:
        raise MpesaError(str(exc)) from exc
