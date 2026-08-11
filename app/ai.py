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
    RESEND_DEPOSIT = "RESEND_DEPOSIT"  # customer wants an M-Pesa deposit prompt resent/retry/STK push
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
    service_names: list[str] = Field(default_factory=list, description="List of catalog service names mentioned if multiple (e.g. ['Haircut', 'Hair Coloring'])")
    product_name: str | None = Field(default=None, description="Catalog product name mentioned")
    quantity: int | None = Field(default=None, description="Quantity of product or item if mentioned")
    date_text: str | None = Field(default=None, description="Date phrase mentioned in THIS message (e.g. tomorrow, Friday)")
    time_text: str | None = Field(default=None, description="Time phrase mentioned in THIS message (e.g. 10am, morning, at 2)")
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

{{"type": "ASK_INFO|LIST_SERVICES|LIST_PRODUCTS|BOOK_SERVICE|BUY_PRODUCT|CHECK_STATUS|CANCEL_BOOKING|CANCEL_ORDER|RESCHEDULE_BOOKING|RESEND_DEPOSIT|CONFIRM_ACTION|CANCEL_ACTION|OUT_OF_SCOPE|OFF_TOPIC", \
"conversation_act": "REQUEST|QUESTION|ACKNOWLEDGEMENT|CLOSING|CORRECTION|UNCERTAIN_ATTENDANCE|COMPLAINT|HUMAN_REQUEST|PROPOSAL|UNCLEAR", \
"authority_route": "NORMAL|OWNER_AUTHORITY_REQUIRED|OFF_TOPIC|UNCLEAR", \
"entities": {{"service_name": null, "service_names": [], "product_name": null, "quantity": null, "date_text": null, "time_text": null, "fulfillment_type": null, "delivery_address": null, "payment_phone": null}}, \
"reply_text": "<natural, friendly reply to send the customer, used for ASK_INFO, LIST_SERVICES, LIST_PRODUCTS, CHECK_STATUS, OFF_TOPIC, and general chat>"}}

Rules for classification:
- type MUST BE EXACTLY ONE OF: ASK_INFO, LIST_SERVICES, LIST_PRODUCTS, BOOK_SERVICE, BUY_PRODUCT, CHECK_STATUS, CANCEL_BOOKING, CANCEL_ORDER, RESCHEDULE_BOOKING, RESEND_DEPOSIT, CONFIRM_ACTION, CANCEL_ACTION, OUT_OF_SCOPE, OFF_TOPIC. Do not use COMPLAINT as type; use OUT_OF_SCOPE for complaints and set conversation_act to COMPLAINT.
- BOOK_SERVICE / BUY_PRODUCT: Use when the customer explicitly expresses intent to reserve, book, purchase, or schedule an appointment/order (e.g. "I want to book a haircut", "reserve manicure tomorrow at 2pm", "I'd like to buy shampoo"), OR gives a date/time/quantity for an in-progress booking/order. ALSO use BOOK_SERVICE when the customer asks about availability on a specific day AND expresses intent to visit or book (e.g. "do you open tomorrow so I can come for a haircut", "are you available Saturday for braiding?") — extract the date into date_text and the service into service_name.
- service_name / service_names: Extract ALL explicitly requested service nouns (e.g. ["Haircut", "Manicure"]), whether listed in the Catalog below or NOT. Set service_names as a list of all requested service nouns, and set service_name to the primary one. Do NOT drop or filter out unlisted service nouns (like "manicure", "pedicure", "facial") during extraction. Do NOT extract adjectives ("quick", "fresh"), general chatter, amenities ("parking", "wifi"), or past experiences. If an unlisted service is requested, set service_name to exactly what the customer said — do NOT substitute or map it to a catalog service.
- product_name MUST be one of the exact names from the Catalog below, or null. Same rules as service_name.
- fulfillment_type: "delivery" or "pickup" if the customer specifies wanting delivery vs store pickup in THIS message, else null.
- delivery_address: street address / location / landmark if customer provides a delivery address in THIS message, else null.
- date_text: ONLY the date/day part mentioned in THIS message (e.g. "Thursday", "tomorrow", "today", "25th August"). Do not repeat dates from earlier turns.
- time_text: ONLY the time-of-day part mentioned in THIS message (e.g. "2pm", "14:00", "4:00pm", "at 2").
- quantity: a plain integer if THIS message states a quantity, else null.
- payment_phone: phone number if customer provides an M-Pesa payment line in THIS message (e.g. "0712345678", "0706832905", "pay via 0711223344"), else null.
- CONFIRM_ACTION: Customer agrees to proceed with an in-progress action ("yes", "confirm", "go ahead") OR provides a phone number while in STAGE_CONFIRMING. If Currently collecting shows an in-progress booking or payment confirmation, any 10-digit/12-digit phone number MUST be classified as CONFIRM_ACTION with payment_phone populated, NOT as UNCERTAIN_ATTENDANCE or ASK_INFO.
- CANCEL_ACTION: Customer wants to abandon an in-progress draft action ("nevermind", "stop").
- conversation_act captures what the message is doing socially: thanks/bye = ACKNOWLEDGEMENT/CLOSING, "I might not make it tomorrow" = UNCERTAIN_ATTENDANCE, "no, I meant 3pm" = CORRECTION, complaints = COMPLAINT, requests for a person = HUMAN_REQUEST, business partnership/wholesale/sponsorship proposals = PROPOSAL.
- authority_route should be OWNER_AUTHORITY_REQUIRED only when the customer asks for something requiring explicit owner/manager decision (proposal, partnership, complaint, custom discount negotiation, refund exception, explicit request for human manager/owner). Keep general business questions, deposit inquiries, operating hours, location, amenities, and catalog availability questions as NORMAL.
- OUT_OF_SCOPE: Use for complaints, frustrations, insults, business inquiries that require explicit human owner escalation (custom price negotiation, manager requests, custom policy exceptions).
- OFF_TOPIC: Use for completely irrelevant, non-business queries (e.g. writing code, weather, recipes, general trivia). Provide a polite assistant boundary in reply_text (e.g. "I'm the virtual assistant for {business_name}! I can only assist with our listed services, bookings, products, and operating hours..."). Do NOT notify the shop owner for OFF_TOPIC.
- CHECK_STATUS: Use ONLY when the customer asks about their own existing bookings or orders (e.g. "what's my booking status?", "do I have any upcoming appointments?", "check my order"). Do NOT use CHECK_STATUS when the customer asks about business availability or operating hours (e.g. "do you open tomorrow?", "are you open on Saturday?") — use ASK_INFO or BOOK_SERVICE instead.
- ASK_INFO / LIST_SERVICES / LIST_PRODUCTS: Use when the customer is asking a question about services, products, availability, operating hours, location, deposit policy, or pricing (e.g. "do you offer manicure?", "do you have acrylic nails?", "what services do you have?", "are you open tomorrow?", "what time do you close?", "are you free tomorrow at 11?"). Check the injected Catalog below:
  * Phrases starting with "are you free", "do you have availability", "is X open" are availability inquiries (ASK_INFO), NOT booking requests, unless the customer explicitly states "I want to book", "reserve", or "schedule".
  * Answer the customer's question directly and naturally using ONLY the provided business profile facts.
  * STRICT UNIVERSAL NO-SUGGESTION RULE: Answer ONLY what was asked. DO NOT suggest, propose, encourage, recommend, pitch, or invite ANY next steps, bookings, products, services, operating hours, website links, or follow-up actions unless the customer EXPLICITLY asked for them. Stop speaking immediately after giving the direct answer.
  * If the requested item is NOT listed in the Catalog (e.g. acrylic nails, massages), state warmly in reply_text that it is not currently offered at {business_name}, and list the available catalog services.
  * For questions about operating hours, reproduce the exact hours from Operating hours below — do NOT paraphrase, summarize, or rewrite them. Use the exact day names and time ranges as provided.
  * If Live slot info is present in Extra Info & FAQs, use it to state clearly whether the requested time or date is free or already booked.

CRITICAL — NEVER FABRICATE OR ASSUME:
- Your ONLY source of truth is the Business name, Catalog, Operating hours, Address & Location, Extra Info & FAQs, and Fulfillment Policy provided below. NEVER state, promise, imply, or invent ANY fact, capability, amenity, policy, or feature that is not EXPLICITLY written in these fields.
- NEVER state or claim that you are "unable to check availability real-time", "cannot view schedule", or "cannot check real-time in this chat". You have direct live database access via the Business profile and Live slot info above.
- If the customer asks about something NOT covered by the business profile below (e.g. parking, refund policy, payment methods beyond M-Pesa, home service, kids policy, group discounts, Wi-Fi, accessibility, gift cards, loyalty programs, specific product ingredients, insurance acceptance), you MUST say: "I don't have that information right now — let me check with the team and get back to you!" and set authority_route to OWNER_AUTHORITY_REQUIRED so the owner is notified.
- NEVER say "yes" or confirm availability of any service, product, feature, or policy you cannot directly verify from the data below. When in doubt, escalate — do not guess.
- Prices, durations, and deposit amounts MUST exactly match what is listed in the Catalog. Never round, estimate, or paraphrase amounts.

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


_DECISION_SYSTEM_PROMPT = """You are the strict decision router for {business_name}'s WhatsApp assistant.
Return ONLY JSON matching this schema, no markdown:

{{"reasoning": "<Answer these 3 questions: 1) Is the customer trying to DO something (book/order/cancel/change) or KNOW something (hours/prices/services/location)? 2) How many intents are in this message? 3) Which is primary and which are secondary?>",
"primary_action": "START_BOOKING|CONTINUE_BOOKING|CHANGE_BOOKING_FIELD|CONFIRM_PENDING_ACTION|CANCEL_PENDING_ACTION|START_ORDER|CONTINUE_ORDER|ASK_BUSINESS_INFO|ASK_CATALOG|ASK_STOCK|CHECK_STATUS|START_CANCEL_BOOKING|START_CANCEL_ORDER|START_RESCHEDULE_BOOKING|RESEND_DEPOSIT_PROMPT|ESCALATE_TO_OWNER|OFF_TOPIC_BOUNDARY|ASK_CLARIFICATION|SOCIAL_REPLY|FALLBACK",
"secondary_actions": ["ANSWER_SERVICE_AVAILABILITY|ANSWER_PRODUCT_AVAILABILITY|ANSWER_PRICE|ANSWER_HOURS|NOTIFY_OWNER|PRESERVE_PENDING_CONTEXT"],
"facts": {{"service_name": null, "service_names": [], "product_name": null, "quantity": null, "date_text": null, "time_text": null, "payment_phone": null, "complaint": false, "cancel_signal": false, "off_topic": false}},
"state_policy": "preserve|update_pending|clear_pending|ask_before_replacing",
"needs_owner": false,
"confidence": 0.0 }}

Rules:
- ALWAYS write the 'reasoning' field FIRST. Answer the 3 diagnostic questions before picking primary_action.
- Choose exactly one primary_action. Use secondary_actions for safe side effects or response enrichment.
- secondary_actions MUST ONLY contain: ANSWER_SERVICE_AVAILABILITY, ANSWER_PRODUCT_AVAILABILITY, ANSWER_PRICE, ANSWER_HOURS, NOTIFY_OWNER, PRESERVE_PENDING_CONTEXT. NEVER put PrimaryAction names (such as ESCALATE_TO_OWNER or ASK_CLARIFICATION) in secondary_actions.
- For services, service_name/service_names must be exact names from Catalog. For goods, product_name must be an exact catalog name. If unlisted service is requested, extract date_text/time_text but do NOT substitute catalog names into service_name.
- Booking/order facts are facts from THIS message only. Extract date_text and time_text exactly as spoken when present. Do not copy date/time/quantity from history unless the customer restates it.
- If a customer gives service + date/time or a correction to an active booking, prefer START_BOOKING/CONTINUE_BOOKING/CHANGE_BOOKING_FIELD over escalation unless there are actual complaint/owner-authority words.
- Questions starting with "are you free", "do you have availability", "is X open" are availability inquiries (ASK_BUSINESS_INFO / ASK_INFO), NOT booking requests (START_BOOKING), unless the customer explicitly states "I want to book", "reserve", or "schedule".
- OFF_TOPIC_BOUNDARY preserves pending state. CANCEL_PENDING_ACTION requires explicit cancel/stop/nevermind/start over language.
- ASK_STOCK is for "do you have X", "is X in stock", or restock notification requests. If a restock notification is requested, include NOTIFY_OWNER.
- ESCALATE_TO_OWNER is only for explicit complaints, refund exceptions, human/manager requests, proposals, or unavailable policy facts.
- Response wording is NOT your job. Do not invent a customer reply. Only route the turn.

Strict boundaries (NEVER violate):
- OFF_TOPIC_BOUNDARY is ONLY for non-business chatter (weather, coding, recipes, sports). Questions about unlisted services, variations, or anything related to the business are NEVER off-topic.
- UNCLEAR/ASK_CLARIFICATION is ONLY for complete gibberish. If the message mentions any service, product, price, time, date, or business topic, it is NOT unclear.
- complaint=true is ONLY for explicit dissatisfaction ("terrible", "ruined", "worst"). Price/discount questions are NEVER complaints.
- Greetings ("hi", "hello", "good morning") with no other content are SOCIAL_REPLY, even mid-booking.

Few-shot examples:

Customer: "Do you do knotless braids?" (active Haircut booking in memory)
{{"reasoning": "1) KNOW - asking if a service exists. 2) Single intent. 3) Primary: catalog inquiry.", "primary_action": "ASK_CATALOG", "secondary_actions": ["ANSWER_SERVICE_AVAILABILITY", "PRESERVE_PENDING_CONTEXT"], "facts": {{"service_name": "Knotless Braids"}}, "state_policy": "preserve", "needs_owner": false, "confidence": 0.92}}

Customer: "What time do you close on Sunday?" (active Haircut booking in memory)
{{"reasoning": "1) KNOW - asking about operating hours. 2) Single intent. 3) Primary: hours question.", "primary_action": "ASK_BUSINESS_INFO", "secondary_actions": ["ANSWER_HOURS", "PRESERVE_PENDING_CONTEXT"], "facts": {{"date_text": "Sunday"}}, "state_policy": "preserve", "needs_owner": false, "confidence": 0.95}}

Customer: "Where are you located and I'd like to book a haircut tomorrow"
{{"reasoning": "1) Both KNOW (location) and DO (booking). 2) Two intents. 3) Primary: booking action. Secondary: location question.", "primary_action": "START_BOOKING", "secondary_actions": ["PRESERVE_PENDING_CONTEXT"], "facts": {{"service_name": "Haircut", "date_text": "tomorrow"}}, "state_policy": "update_pending", "needs_owner": false, "confidence": 0.93}}

Customer: "Can I get a discount?"
{{"reasoning": "1) KNOW - asking about pricing/deals. 2) Single intent. 3) Primary: price inquiry. No dissatisfaction expressed.", "primary_action": "ASK_BUSINESS_INFO", "secondary_actions": ["ANSWER_PRICE"], "facts": {{}}, "state_policy": "preserve", "needs_owner": false, "confidence": 0.90}}

Customer: "Hello" (active booking in memory)
{{"reasoning": "1) Neither action nor question - simple greeting. 2) Single intent. 3) Primary: social reply.", "primary_action": "SOCIAL_REPLY", "secondary_actions": ["PRESERVE_PENDING_CONTEXT"], "facts": {{}}, "state_policy": "preserve", "needs_owner": false, "confidence": 0.98}}

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


async def extract_turn_decision(
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
):
    """Return the new strict decision schema, falling back to legacy intent."""
    from app.conversation_decision import (
        TurnDecisionSchema,
        decision_from_intent,
        decision_from_schema,
        intent_from_decision,
    )

    # Unit tests and some callers monkeypatch extract_intent; honor that path
    # so the workflow can be tested deterministically without a network call.
    is_mocked_intent = getattr(extract_intent, "__name__", "") != "extract_intent" or extract_intent.__module__ != __name__
    if not is_mocked_intent:
        settings = get_settings()
        prompt = _DECISION_SYSTEM_PROMPT.format(
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
        try:
            is_mocked_llm = getattr(_call_llm, "__name__", "") != "_call_llm" or _call_llm.__module__ != __name__
            gemini_key = settings.gemini_api_key or (settings.llm_api_key if settings.llm_provider == "gemini" else None)
            groq_key = settings.llm_api_key if (settings.llm_api_key and settings.llm_api_key.startswith("gsk_")) else None

            # Primary: Gemini
            if (gemini_key or settings.llm_provider == "gemini") and not is_mocked_llm:
                try:
                    raw = await _call_llm(prompt, customer_message, settings)
                    decision = decision_from_schema(TurnDecisionSchema.model_validate_json(_clean_json(raw)))
                    return intent_from_decision(decision), decision
                except Exception as exc:
                    logger.warning("Gemini primary turn decision failed; attempting Groq fallback", extra=log_extra(error=str(exc)))

            # Fallback: Groq with llama-3.3-70b-versatile
            if groq_key and not is_mocked_llm:
                client = instructor.patch(AsyncGroq(api_key=groq_key))
                structured = await client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    response_model=TurnDecisionSchema,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": customer_message},
                    ],
                    max_retries=settings.llm_max_retries,
                )
                decision = decision_from_schema(structured)
                return intent_from_decision(decision), decision

            raw = await _call_llm(prompt, customer_message, settings)
            decision = decision_from_schema(TurnDecisionSchema.model_validate_json(_clean_json(raw)))
            return intent_from_decision(decision), decision
        except Exception as exc:  # noqa: BLE001
            logger.warning("Native turn decision failed; falling back to legacy intent", extra=log_extra(error=str(exc)))

    intent = await extract_intent(
        customer_message=customer_message,
        business_name=business_name,
        business_type=business_type,
        catalog=catalog,
        conversation_history=conversation_history,
        pending=pending,
        business_hours_text=business_hours_text,
        business_address=business_address,
        business_extra_info=business_extra_info,
        fulfillment_policy=fulfillment_policy,
    )
    return intent, decision_from_intent(intent)


async def _call_llm(system_prompt: str, user_message: str, settings) -> str:
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        if settings.llm_provider == "gemini" or settings.gemini_api_key:
            api_key = settings.gemini_api_key or settings.llm_api_key
            url = settings.llm_api_base.split("?")[0] if settings.llm_api_base else "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
            if "key=" not in url and api_key:
                url = f"{url}?key={api_key}"
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["x-goog-api-key"] = api_key
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
            else:
                payload["model"] = "llama-3.3-70b-versatile"
            headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
            if "openrouter.ai" in url:
                headers["HTTP-Referer"] = "https://localhost"
                headers["X-Title"] = "WA Business Assistant"
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


def _parse_intent(raw: str) -> Intent:
    cleaned = _clean_json(raw)
    parsed = json.loads(cleaned)  # raises on malformed JSON -> caught by caller -> fallback
    intent_type = IntentType(parsed["type"])  # raises on unknown type -> fallback
    return Intent(
        type=intent_type,
        entities=parsed.get("entities", {}) or {},
        reply_text=parsed.get("reply_text", "") or "",
        conversation_act=ConversationAct(parsed.get("conversation_act") or ConversationAct.REQUEST.value),
        authority_route=AuthorityRoute(parsed.get("authority_route") or AuthorityRoute.NORMAL.value),
    )


def _clean_json(raw: str) -> str:
    return raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
