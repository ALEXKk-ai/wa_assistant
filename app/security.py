"""Security primitives: at-rest encryption for per-business secrets, and
webhook authenticity checks.

Design decisions (and why):

- Per-business credentials (WhatsApp access token, M-Pesa consumer secret,
  passkey) are encrypted with Fernet (AES-128-CBC + HMAC, authenticated
  encryption) before they ever touch the database. The single master key
  lives only in the environment (WA_MASTER_KEY), never in the DB, never in
  git. Rotating a business's credential doesn't require touching any other
  business's row.

- WhatsApp webhook payloads are verified against Meta's X-Hub-Signature-256
  header using the app secret, with a constant-time comparison, so a
  forged webhook call can't inject fake messages/orders.

- M-Pesa's callback mechanism has no built-in signature; Safaricom calls
  a URL you register. We mitigate with a per-deployment shared secret
  embedded in the callback path (see config.mpesa_callback_secret) plus
  strict idempotency handling in payments.py, so even a replayed or
  spoofed call can only ever be processed once and only against a real,
  pending payment record - it can't fabricate a new one.
"""
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class DecryptionError(RuntimeError):
    """Raised when a stored secret cannot be decrypted (wrong/rotated key)."""


def _fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.wa_master_key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a per-business credential for storage. Returns a str safe to
    store in a TEXT column."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored credential. Raises DecryptionError rather than
    leaking a stack trace with key material context."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Stored secret could not be decrypted - master key may have "
            "changed, or data is corrupted."
        ) from exc


def verify_whatsapp_signature(payload_body: bytes, signature_header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header for an inbound webhook call.

    signature_header looks like "sha256=<hex digest>". Returns False (never
    raises) on any malformed input so callers can uniformly reject.
    """
    settings = get_settings()
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    provided_digest = signature_header.split("=", 1)[1]
    expected_digest = hmac.new(
        settings.whatsapp_app_secret.encode(),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided_digest, expected_digest)


def verify_mpesa_callback_secret(path_secret: str) -> bool:
    """Constant-time check of the shared secret embedded in the M-Pesa
    callback URL path against the configured value."""
    settings = get_settings()
    return hmac.compare_digest(path_secret, settings.mpesa_callback_secret)
