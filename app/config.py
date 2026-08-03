"""Application configuration.

All values are loaded from environment variables (see .env.example). Nothing
secret is ever hardcoded here. The single master key (WA_MASTER_KEY) is used
only to encrypt/decrypt per-business credentials at rest (app/security.py) -
it is not itself a business credential.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str = "sqlite+aiosqlite:///./wa_assistant.db"

    # Security
    # Fernet key used to encrypt per-business secrets (WhatsApp token, M-Pesa
    # credentials) before they are stored in the database. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    wa_master_key: str

    # Meta WhatsApp Cloud API
    whatsapp_api_base: str = "https://graph.facebook.com/v20.0"
    # App secret used to verify inbound webhook signatures (X-Hub-Signature-256).
    # This is the Meta App secret, not a per-business token.
    whatsapp_app_secret: str
    whatsapp_webhook_verify_token: str

    # M-Pesa (Daraja)
    mpesa_base_url: str = "https://sandbox.safaricom.co.ke"
    # The public base URL of THIS application, used to build the M-Pesa
    # callback URL.  Must be set to the externally-reachable address of your
    # deployment (e.g. "https://mybot.example.com").  Deliberately has no
    # default so a missing value is a startup error, not a silent mis-route.
    app_base_url: str
    # Shared secret appended to the callback URL path so the M-Pesa callback
    # endpoint isn't guessable / can't be hit blind. Not a substitute for
    # proper network-level protections, but a real, cheap layer of defense.
    mpesa_callback_secret: str

    # LLM
    llm_provider: str = "gemini"  # "gemini" | "llama" | "openrouter"
    llm_api_key: str = ""
    llm_api_base: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 8.0
    llm_max_retries: int = 2

    # Ops
    log_level: str = "INFO"
    environment: str = "development"  # "development" | "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
