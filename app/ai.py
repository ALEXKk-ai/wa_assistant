"""LLM-based intent extraction.

Two things changed from the original version to support non-linear,
multi-turn conversations:

1. extract_intent() now takes conversation_history and pending - the LLM
   sees what's already been collected (e.g. "service=Haircut, slot_text=
   'Thursday'") and the last few turns, so it can both (a) merge new
   information into what's already known instead of ignoring it, and
   (b) correctly interpret a short reply like "2pm" as filling in the
   missing time for an in-progress booking, rather than misclassifying it.

2. A genuine OUT_OF_SCOPE intent exists now, separate from FALLBACK.
   FALLBACK is a code-level safety net for technical failures (timeout,
   malformed JSON) and is never something the LLM is asked to choose.
   OUT_OF_SCOPE is a real classification the LLM makes when the message
   is something the catalog/business info can't ground an answer to (a
   partnership proposal, a complaint needing a human judgment call, a
   custom price negotiation) - the bot should not improvise an answer to
   these from general knowledge, it should say so and forward it.

The one rule this module still enforces: a slow or broken LLM must never
crash or silently stall a conversation - every call is wrapped in a
timeout + bounded retry, and any failure degrades to FALLBACK_INTENT.
"""
import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum

import httpx
import instructor
from groq import AsyncGroq
from pydantic import BaseModel, Field

from app.config import get_settings
from app.logging_conf import get_logger, log_extra

logger = get_logger(__name__)


class IntentType(str, Enum):
    ASK_INFO = "ASK_INFO"  # hours, location, general Q&A answerable from the catalog/business info
    LIST_SERVICES = "LIST_SERVICES"
    LIST_PRODUCTS = "LIST_PRODUCTS"
    BOOK_SERVICE = "BOOK_SERVICE"
    BUY_PRODUCT = "BUY_PRODUCT"
    CHECK_STATUS = "CHECK_STATUS"
    CANCEL_BOOKING = "CANCEL_BOOKING"  # customer wants to cancel an existing, already-created booking
    CANCEL_ORDER = "CANCEL_ORDER"  # same, for an existing order
    RESCHEDULE_BOOKING = "RESCHEDULE_BOOKING"  # customer wants to move an existing booking to a new time
    CONFIRM_ACTION = "CONFIRM_ACTION"  # customer is saying yes/go ahead to a pending booking or order
    CANCEL_ACTION = "CANCEL_ACTION"  # customer wants to abandon what's currently being collected/confirmed
    OUT_OF_SCOPE = "OUT_OF_SCOPE"  # business inquiry missing from catalog - needs human owner escalation
    OFF_TOPIC = "OFF_TOPIC"  # completely irrelevant queries (coding, general knowledge, weather) - handled by bot boundary
    FALLBACK = "FALLBACK"  # LLM call itself failed - code-level only, never an LLM classification


class ConversationAct(str, Enum):
    REQUEST = "REQUEST"
    QUESTION = "QUESTION"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    CLOSING = "CLOSING"
    CORRECTION = "CORRECTION"
    UNCERTAIN_ATTENDANCE = "UNCERTAIN_ATTENDANCE"
    COMPLAINT = "COMPLAINT"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    PROPOSAL = "PROPOSAL"
    UNCLEAR = "UNCLEAR"


class AuthorityRoute(str, Enum):
    NORMAL = "NORMAL"
    OWNER_AUTHORITY_REQUIRED = "OWNER_AUTHORITY_REQUIRED"
    OFF_TOPIC = "OFF_TOPIC"
    UNCLEAR = "UNCLEAR"


@dataclass
class Intent:
    type: IntentType
    entities: dict = field(default_factory=dict)
    reply_text: str = ""
    conversation_act: ConversationAct = ConversationAct.REQUEST
    authority_route: AuthorityRoute = AuthorityRoute.NORMAL


class ExtractedEntitiesSchema(BaseModel):
    service_name: str | None = Field(default=None, description="Catalog service name mentioned (e.g. Haircut, Braiding)")
    product_name: str | None = Field(default=None, description="Catalog product name mentioned")
    quantity: int | None = Field(default=None, description="Quantity of product or item if mentioned")
    date_text: str | None = Field(default=None, description="Date phrase mentioned in THIS message (e.g. tomorrow, Friday)")
    time_text: str | None = Field(default=None, description="Time phrase mentioned in THIS message (e.g. 10am, morning)")
    fulfillment_type: str | None = Field(default=None, description="'delivery' or 'pickup' if mentioned")
    delivery_address: str | None = Field(default=None, description="Street address or landmark if mentioned")
    payment_phone: str | None = Field(default=None, description="M-Pesa payment phone number if provided")


class StructuredIntentResponse(BaseModel):
    type: IntentType
    entities: ExtractedEntitiesSchema = Field(default_factory=ExtractedEntitiesSchema)
    conversation_act: ConversationAct = ConversationAct.REQUEST
    authority_route: AuthorityRoute = AuthorityRoute.NORMAL
    reply_text: str = Field(default="", description="Natural polite response to send customer for Q&A or info questions")


FALLBACK_INTENT = Intent(
    type=IntentType.FALLBACK,
    entities={},
    reply_text=(
        "Sorry, I didn't quite catch that. Let me get the team to help you "
        "directly - someone will be with you shortly."
    ),
)

_SYSTEM_PROMPT = """You are a helpful, conversational WhatsApp assistant for {business_name}. \
Given the business's catalog, operating hours, recent conversation history, and what's currently \
in progress, analyze the customer's message and respond ONLY with JSON matching this schema, no markdown:

{{"type": "ASK_INFO|LIST_SERVICES|LIST_PRODUCTS|BOOK_SERVICE|BUY_PRODUCT|CHECK_STATUS|CANCEL_BOOKING|CANCEL_ORDER|RESCHEDULE_BOOKING|CONFIRM_ACTION|CANCEL_ACTION|OUT_OF_SCOPE|OFF_TOPIC", \
"conversation_act": "REQUEST|QUESTION|ACKNOWLEDGEMENT|CLOSING|CORRECTION|UNCERTAIN_ATTENDANCE|COMPLAINT|HUMAN_REQUEST|PROPOSAL|UNCLEAR", \
"authority_route": "NORMAL|OWNER_AUTHORITY_REQUIRED|OFF_TOPIC|UNCLEAR", \
"entities": {{"service_name": null, "product_name": null, "quantity": null, "date_text": null, "time_text": null, "fulfillment_type": null, "delivery_address": null, "payment_phone": null}}, \
"reply_text": "<natural, friendly reply to send the customer, used for ASK_INFO, LIST_SERVICES, LIST_PRODUCTS, CHECK_STATUS, OFF_TOPIC, and general chat>"}}

Rules for classification:
- type MUST BE EXACTLY ONE OF: ASK_INFO, LIST_SERVICES, LIST_PRODUCTS, BOOK_SERVICE, BUY_PRODUCT, CHECK_STATUS, CANCEL_BOOKING, CANCEL_ORDER, RESCHEDULE_BOOKING, CONFIRM_ACTION, CANCEL_ACTION, OUT_OF_SCOPE, OFF_TOPIC. Do not use COMPLAINT as type; use OUT_OF_SCOPE for complaints and set conversation_act to COMPLAINT.
- BOOK_SERVICE / BUY_PRODUCT: Use when the customer explicitly expresses intent to reserve, book, purchase, or schedule an appointment/order (e.g. "I want to book a haircut", "reserve manicure tomorrow at 2pm", "I'd like to buy shampoo"), OR gives a date/time/quantity for an in-progress booking/order.
- service_name MUST be one of the exact names from the Catalog below, or null. NEVER invent or guess a service name from general knowledge (e.g. do NOT output "Pedicure", "Manicure", "Massage" etc. unless they appear in the Catalog). If the customer mentions a service not in the Catalog, set service_name to null and use type ASK_INFO. If the customer is confirming a booking discussed in the Recent conversation (e.g. "yes book it", "let's do tomorrow at 11am"), set service_name to the catalog service name from the conversation context.
- product_name MUST be one of the exact names from the Catalog below, or null. Same rules as service_name.
- fulfillment_type: "delivery" or "pickup" if the customer specifies wanting delivery vs store pickup in THIS message, else null.
- delivery_address: street address / location / landmark if customer provides a delivery address in THIS message, else null.
- date_text: ONLY the date/day part mentioned in THIS message (e.g. "Thursday", "tomorrow", "today", "25th August"). Do not repeat dates from earlier turns.
- time_text: ONLY the time-of-day part mentioned in THIS message (e.g. "2pm", "14:00", "4:00pm"). Do not repeat times from earlier.
- quantity: a plain integer if THIS message states a quantity, else null.
- payment_phone: phone number if customer provides an M-Pesa payment line in THIS message (e.g. "0712345678", "pay via 0711223344"), else null.
- CANCEL_BOOKING / CANCEL_ORDER: Customer wants to cancel an existing, already-made booking/order.
- RESCHEDULE_BOOKING: Customer wants to move an existing booking to a new time.
- CHECK_STATUS: ONLY use when the customer explicitly asks to see/list their bookings or orders (e.g. "what are my bookings?", "do I have anything upcoming?", "show my orders"). Do NOT use CHECK_STATUS for questions ABOUT a booking process (e.g. "how long until you confirm?", "where are you located?", "what happens if payment fails?") — those are ASK_INFO.
- CONFIRM_ACTION: Customer agrees to proceed with an in-progress action ("yes", "confirm", "go ahead").
- CANCEL_ACTION: Customer wants to abandon an in-progress draft action ("nevermind", "stop").
- conversation_act captures what the message is doing socially: thanks/bye = ACKNOWLEDGEMENT/CLOSING, "I might not make it tomorrow" = UNCERTAIN_ATTENDANCE, "no, I meant 3pm" = CORRECTION, complaints = COMPLAINT, requests for a person = HUMAN_REQUEST, business partnership/wholesale/sponsorship proposals = PROPOSAL.
- authority_route should be OWNER_AUTHORITY_REQUIRED only when the customer asks for something requiring explicit owner/manager decision (proposal, partnership, complaint, custom discount negotiation, refund exception, explicit request for human manager/owner). Keep general business questions, deposit inquiries, operating hours, location, amenities, and catalog availability questions as NORMAL.
- OUT_OF_SCOPE: Use for complaints, frustrations, insults, business inquiries that require explicit human owner escalation (custom price negotiation, manager requests, custom policy exceptions).
- OFF_TOPIC: Use for completely irrelevant, non-business queries (e.g. writing code, weather, recipes, general trivia). Provide a polite assistant boundary in reply_text (e.g. "I'm the virtual assistant for {business_name}! I can only assist with our listed services, bookings, products, and operating hours..."). Do NOT notify the shop owner for OFF_TOPIC.
- ASK_INFO / LIST_SERVICES / LIST_PRODUCTS: Use when the customer is asking a question about services, products, availability, operating hours, location, deposit policy, or pricing (e.g. "do you offer manicure?", "do you have acrylic nails?", "what services do you have?"). Check the injected Catalog below:
  * If the requested item is listed in the Catalog, state that it is available along with its price and duration in reply_text, and ask if they'd like to book it.
  * If the requested item is NOT listed in the Catalog (e.g. acrylic nails, massages), state warmly in reply_text that it is not currently offered at {business_name}, and mention the available catalog services.

Business name: {business_name}
Business type: {business_type}
Fulfillment Policy: {fulfillment_policy}
Address & Location: {business_address}
Extra Info & FAQs: {business_extra_info}
Catalog: {catalog}
Operating hours: {business_hours_text}

Currently collecting: {pending_summary}

Recent conversation:
{history_text}
"""


def _format_history(conversation_history: list[dict]) -> str:
    if not conversation_history:
        return "(none yet)"
    lines = []
    for turn in conversation_history[-8:]:
        role = "Customer" if turn.get("role") == "customer" else "Business"
        lines.append(f"{role}: {turn.get('text', '')}")
    return "\n".join(lines)


def _format_pending(pending: dict | None) -> str:
    if not pending:
        return "(nothing in progress)"
    parts = [f"{k}={v}" for k, v in pending.items() if v not in (None, "", {})]
    return ", ".join(parts) if parts else "(nothing in progress)"


async def extract_intent(
    customer_message: str,
    business_name: str,
    business_type: str,
    catalog: list[dict],
    conversation_history: list[dict] | None = None,
    pending: dict | None = None,
    business_hours_text: str = "not set - no restrictions",
    business_address: str = "not listed",
    business_extra_info: str = "none",
    fulfillment_policy: str = "both (delivery or store pickup)",
) -> Intent:
    settings = get_settings()
    prompt = _SYSTEM_PROMPT.format(
        business_name=business_name,
        business_type=business_type,
        fulfillment_policy=fulfillment_policy,
        business_address=business_address or "not listed",
        business_extra_info=business_extra_info or "none",
        catalog=catalog,
        business_hours_text=business_hours_text,
        pending_summary=_format_pending(pending),
        history_text=_format_history(conversation_history or []),
    )

    last_error: Exception | None = None
    is_mocked_llm = getattr(_call_llm, "__name__", "") != "_call_llm" or _call_llm.__module__ != __name__
    for attempt in range(settings.llm_max_retries + 1):
        try:
            groq_key = settings.llm_api_key if (settings.llm_api_key and settings.llm_api_key.startswith("gsk_")) else None
            if groq_key and not is_mocked_llm:
                client = instructor.patch(AsyncGroq(api_key=groq_key))
                structured = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    response_model=StructuredIntentResponse,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": customer_message},
                    ],
                    max_retries=settings.llm_max_retries,
                )
                return Intent(
                    type=structured.type,
                    entities={k: v for k, v in structured.entities.model_dump().items() if v is not None},
                    reply_text=structured.reply_text or "",
                    conversation_act=structured.conversation_act,
                    authority_route=structured.authority_route,
                )

            raw = await _call_llm(prompt, customer_message, settings)
            return _parse_intent(raw)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            last_error = exc
            logger.warning(
                "LLM call failed, attempt %s/%s",
                attempt + 1,
                settings.llm_max_retries + 1,
                extra=log_extra(error=str(exc)),
            )
            if attempt < settings.llm_max_retries:
                await asyncio.sleep(1.0 * (attempt + 1))

    if settings.gemini_api_key and settings.llm_provider != "gemini":
        try:
            logger.info("Primary LLM failed; trying secondary Gemini backup LLM")
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
            headers = {"x-goog-api-key": settings.gemini_api_key}
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={"contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + customer_message}]}]},
                )
                resp.raise_for_status()
                data = resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_intent(raw)
        except Exception as exc:
            logger.warning("Secondary Gemini LLM fallback also failed", extra=log_extra(error=str(exc)))

    logger.error("LLM calls exhausted retries, falling back", extra=log_extra(error=str(last_error)))
    return FALLBACK_INTENT


async def _call_llm(system_prompt: str, user_message: str, settings) -> str:
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        if settings.llm_provider == "gemini":
            url = settings.llm_api_base.split("?")[0]
            headers = {"x-goog-api-key": settings.llm_api_key}
            resp = await client.post(
                url,
                headers=headers,
                json={
                    "contents": [
                        {"role": "user", "parts": [{"text": system_prompt + "\n\n" + user_message}]}
                    ]
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            # OpenAI-compatible chat completion (OpenRouter, self-hosted Llama, etc.)
            base = settings.llm_api_base.rstrip("/")
            url = base if base.endswith("chat/completions") else f"{base}/chat/completions"
            payload: dict = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            }
            if settings.llm_model:
                payload["model"] = settings.llm_model
            headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
            if "openrouter.ai" in url:
                headers["HTTP-Referer"] = "https://localhost"
                headers["X-Title"] = "WA Business Assistant"
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


def _parse_intent(raw: str) -> Intent:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(cleaned)  # raises on malformed JSON -> caught by caller -> fallback
    intent_type = IntentType(parsed["type"])  # raises on unknown type -> fallback
    return Intent(
        type=intent_type,
        entities=parsed.get("entities", {}) or {},
        reply_text=parsed.get("reply_text", "") or "",
        conversation_act=ConversationAct(parsed.get("conversation_act") or ConversationAct.REQUEST.value),
        authority_route=AuthorityRoute(parsed.get("authority_route") or AuthorityRoute.NORMAL.value),
    )
