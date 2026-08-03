"""Meta WhatsApp Cloud API client.

The one bug this module exists to prevent from ever recurring: an earlier
version of this system sent outbound messages from one global phone number
instead of each business's own. send_business_message() below takes the
resolved Business object (not a bare phone_number_id string) specifically so
the caller is forced to have looked up the right business first - you can't
call this function without a real Business row in hand.
"""
import httpx

from app.config import get_settings
from app.logging_conf import get_logger, log_extra
from app.models import Business
from app.security import decrypt_secret

logger = get_logger(__name__)


async def send_business_message(business: Business, to_phone_number: str, text: str) -> None:
    settings = get_settings()
    token = decrypt_secret(business.whatsapp_token_encrypted)
    url = f"{settings.whatsapp_api_base}/{business.whatsapp_phone_number_id}/messages"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to_phone_number,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Don't let a WhatsApp API outage crash the request handler -
            # log it loudly so it's visible in ops, but the webhook caller
            # (Meta) still gets a clean 200 so it doesn't retry-storm us.
            logger.error(
                "Failed to send WhatsApp message",
                extra=log_extra(
                    business_id=business.id, to=to_phone_number, error=str(exc)
                ),
            )
