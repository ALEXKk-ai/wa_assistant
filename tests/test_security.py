import hashlib
import hmac

from app.config import get_settings
from app.security import decrypt_secret, encrypt_secret, verify_whatsapp_signature


def test_encrypt_decrypt_roundtrip():
    plaintext = "super-secret-whatsapp-token"
    ciphertext = encrypt_secret(plaintext)
    assert ciphertext != plaintext
    assert decrypt_secret(ciphertext) == plaintext


def test_empty_secret_roundtrips_to_empty():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_valid_signature_is_accepted():
    settings = get_settings()
    body = b'{"hello": "world"}'
    digest = hmac.new(settings.whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_whatsapp_signature(body, f"sha256={digest}") is True


def test_tampered_body_is_rejected():
    settings = get_settings()
    body = b'{"hello": "world"}'
    digest = hmac.new(settings.whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()
    tampered_body = b'{"hello": "world!"}'
    assert verify_whatsapp_signature(tampered_body, f"sha256={digest}") is False


def test_missing_signature_header_is_rejected():
    assert verify_whatsapp_signature(b"anything", None) is False


def test_malformed_signature_header_is_rejected():
    assert verify_whatsapp_signature(b"anything", "not-the-right-format") is False


def test_normalize_phone_number():
    from app.security import normalize_phone_number
    assert normalize_phone_number("0712345678") == "254712345678"
    assert normalize_phone_number("0112345678") == "254112345678"
    assert normalize_phone_number("+254712345678") == "254712345678"
    assert normalize_phone_number("254712345678") == "254712345678"
    assert normalize_phone_number("") == ""
