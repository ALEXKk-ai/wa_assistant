from app import payments
from app import repositories as repo
from app.models import PaymentStatus


def _stk_callback_body(checkout_request_id: str, receipt: str = "ABC123XYZ") -> dict:
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "mr-1",
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": 0,
                "ResultDesc": "Success",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 100},
                        {"Name": "MpesaReceiptNumber", "Value": receipt},
                    ]
                },
            }
        }
    }


async def test_duplicate_callback_only_applies_once(session, business):
    payment = await repo.create_payment(session, business.id, "idem-key-1", 100)
    await repo.attach_checkout_request_id(session, payment.id, "ws_CO_1", "mr-1")

    body = _stk_callback_body("ws_CO_1")

    first = await payments.handle_callback(session, body)
    assert first.status == PaymentStatus.COMPLETED
    assert first.mpesa_receipt == "ABC123XYZ"
    first_processed_at = first.processed_at

    # Same callback delivered again (Safaricom retries are common).
    second = await payments.handle_callback(session, body)
    assert second.status == PaymentStatus.COMPLETED
    assert second.processed_at == first_processed_at  # untouched - proves no reprocessing

    # A tampered replay with a different receipt must NOT overwrite the
    # original result once already terminal.
    tampered = _stk_callback_body("ws_CO_1", receipt="FAKE999")
    third = await payments.handle_callback(session, tampered)
    assert third.mpesa_receipt == "ABC123XYZ"  # unchanged


async def test_callback_for_unknown_checkout_request_id_is_ignored(session, business):
    body = _stk_callback_body("does-not-exist")
    result = await payments.handle_callback(session, body)
    assert result is None


async def test_failed_payment_is_recorded_and_not_reprocessed(session, business):
    payment = await repo.create_payment(session, business.id, "idem-key-2", 100)
    await repo.attach_checkout_request_id(session, payment.id, "ws_CO_2", "mr-2")

    failed_body = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "mr-2",
                "CheckoutRequestID": "ws_CO_2",
                "ResultCode": 1032,
                "ResultDesc": "Request cancelled by user",
            }
        }
    }
    result = await payments.handle_callback(session, failed_body)
    assert result.status == PaymentStatus.FAILED

    # A late "success" callback for an already-FAILED payment must not flip it.
    late_success = _stk_callback_body("ws_CO_2")
    result2 = await payments.handle_callback(session, late_success)
    assert result2.status == PaymentStatus.FAILED
