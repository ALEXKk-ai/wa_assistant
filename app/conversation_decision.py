"""Structured turn decisions for customer conversations.

The LLM can still classify natural language, but the rest of the app should
reason about one primary action per turn.  This module is intentionally small:
it translates the existing ai.Intent shape into a clearer decision object that
the policy layer can validate before any workflow mutates state.
"""
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app import ai


class PrimaryAction(str, Enum):
    START_BOOKING = "START_BOOKING"
    CONTINUE_BOOKING = "CONTINUE_BOOKING"
    CHANGE_BOOKING_FIELD = "CHANGE_BOOKING_FIELD"
    CONFIRM_PENDING_ACTION = "CONFIRM_PENDING_ACTION"
    CANCEL_PENDING_ACTION = "CANCEL_PENDING_ACTION"

    START_ORDER = "START_ORDER"
    CONTINUE_ORDER = "CONTINUE_ORDER"

    ASK_BUSINESS_INFO = "ASK_BUSINESS_INFO"
    ASK_CATALOG = "ASK_CATALOG"
    ASK_STOCK = "ASK_STOCK"

    CHECK_STATUS = "CHECK_STATUS"
    START_CANCEL_BOOKING = "START_CANCEL_BOOKING"
    START_CANCEL_ORDER = "START_CANCEL_ORDER"
    START_RESCHEDULE_BOOKING = "START_RESCHEDULE_BOOKING"
    RESEND_DEPOSIT_PROMPT = "RESEND_DEPOSIT_PROMPT"

    ESCALATE_TO_OWNER = "ESCALATE_TO_OWNER"
    OFF_TOPIC_BOUNDARY = "OFF_TOPIC_BOUNDARY"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"
    SOCIAL_REPLY = "SOCIAL_REPLY"
    FALLBACK = "FALLBACK"


class SecondaryAction(str, Enum):
    ANSWER_SERVICE_AVAILABILITY = "ANSWER_SERVICE_AVAILABILITY"
    ANSWER_PRODUCT_AVAILABILITY = "ANSWER_PRODUCT_AVAILABILITY"
    ANSWER_PRICE = "ANSWER_PRICE"
    ANSWER_HOURS = "ANSWER_HOURS"
    ANSWER_LOCATION = "ANSWER_LOCATION"
    ANSWER_PAYMENT_METHODS = "ANSWER_PAYMENT_METHODS"
    NOTIFY_OWNER = "NOTIFY_OWNER"
    PRESERVE_PENDING_CONTEXT = "PRESERVE_PENDING_CONTEXT"


class StatePolicy(str, Enum):
    PRESERVE = "preserve"
    UPDATE_PENDING = "update_pending"
    CLEAR_PENDING = "clear_pending"
    ASK_BEFORE_REPLACING = "ask_before_replacing"


@dataclass
class DecisionFacts:
    service_name: str | None = None
    service_names: list[str] = field(default_factory=list)
    product_name: str | None = None
    quantity: int | None = None
    date_text: str | None = None
    time_text: str | None = None
    payment_phone: str | None = None
    complaint: bool = False
    cancel_signal: bool = False
    off_topic: bool = False


@dataclass
class TurnDecision:
    primary_action: PrimaryAction
    facts: DecisionFacts = field(default_factory=DecisionFacts)
    secondary_actions: list[SecondaryAction] = field(default_factory=list)
    state_policy: StatePolicy = StatePolicy.PRESERVE
    needs_owner: bool = False
    confidence: float = 1.0
    reason: str = ""


class DecisionFactsSchema(BaseModel):
    service_name: str | None = None
    service_names: list[str] = Field(default_factory=list)
    product_name: str | None = None
    quantity: int | None = None
    date_text: str | None = None
    time_text: str | None = None
    payment_phone: str | None = None
    complaint: bool = False
    cancel_signal: bool = False
    off_topic: bool = False


class TurnDecisionSchema(BaseModel):
    reasoning: str = Field(
        default="",
        description="Step-by-step reasoning analysis of customer message, intent, and history before classifying",
    )
    primary_action: PrimaryAction
    secondary_actions: list[SecondaryAction] = Field(default_factory=list)
    facts: DecisionFactsSchema = Field(default_factory=DecisionFactsSchema)
    state_policy: StatePolicy = StatePolicy.PRESERVE
    needs_owner: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("secondary_actions", mode="before")
    @classmethod
    def clean_secondary_actions(cls, v):
        if not isinstance(v, list):
            return []
        cleaned = []
        valid_map = {
            "ASK_BUSINESS_INFO": SecondaryAction.ANSWER_LOCATION,
            "ASK_CATALOG": SecondaryAction.ANSWER_SERVICE_AVAILABILITY,
            "ASK_STOCK": SecondaryAction.ANSWER_PRODUCT_AVAILABILITY,
            "ESCALATE_TO_OWNER": SecondaryAction.NOTIFY_OWNER,
        }
        for item in v:
            if isinstance(item, str):
                if item in SecondaryAction.__members__:
                    cleaned.append(SecondaryAction[item])
                elif item in valid_map:
                    cleaned.append(valid_map[item])
            elif isinstance(item, SecondaryAction):
                cleaned.append(item)
        return cleaned


def decision_from_schema(schema: TurnDecisionSchema) -> TurnDecision:
    facts = DecisionFacts(**schema.facts.model_dump())
    return TurnDecision(
        primary_action=schema.primary_action,
        facts=facts,
        secondary_actions=list(schema.secondary_actions),
        state_policy=schema.state_policy,
        needs_owner=schema.needs_owner,
        confidence=schema.confidence,
        reason=schema.reasoning or schema.reason,
    )


def intent_from_decision(decision: TurnDecision) -> ai.Intent:
    facts = decision.facts
    entities = {
        "service_name": facts.service_name,
        "service_names": facts.service_names,
        "product_name": facts.product_name,
        "quantity": facts.quantity,
        "date_text": facts.date_text,
        "time_text": facts.time_text,
        "payment_phone": facts.payment_phone,
    }
    entities = {k: v for k, v in entities.items() if v not in (None, "", [])}
    action_map = {
        PrimaryAction.START_BOOKING: ai.IntentType.BOOK_SERVICE,
        PrimaryAction.CONTINUE_BOOKING: ai.IntentType.BOOK_SERVICE,
        PrimaryAction.CHANGE_BOOKING_FIELD: ai.IntentType.BOOK_SERVICE,
        PrimaryAction.CONFIRM_PENDING_ACTION: ai.IntentType.CONFIRM_ACTION,
        PrimaryAction.CANCEL_PENDING_ACTION: ai.IntentType.CANCEL_ACTION,
        PrimaryAction.START_ORDER: ai.IntentType.BUY_PRODUCT,
        PrimaryAction.CONTINUE_ORDER: ai.IntentType.BUY_PRODUCT,
        PrimaryAction.ASK_BUSINESS_INFO: ai.IntentType.ASK_INFO,
        PrimaryAction.ASK_CATALOG: ai.IntentType.LIST_PRODUCTS if facts.product_name else ai.IntentType.LIST_SERVICES,
        PrimaryAction.ASK_STOCK: ai.IntentType.ASK_INFO,
        PrimaryAction.CHECK_STATUS: ai.IntentType.CHECK_STATUS,
        PrimaryAction.START_CANCEL_BOOKING: ai.IntentType.CANCEL_BOOKING,
        PrimaryAction.START_CANCEL_ORDER: ai.IntentType.CANCEL_ORDER,
        PrimaryAction.START_RESCHEDULE_BOOKING: ai.IntentType.RESCHEDULE_BOOKING,
        PrimaryAction.RESEND_DEPOSIT_PROMPT: ai.IntentType.RESEND_DEPOSIT,
        PrimaryAction.ESCALATE_TO_OWNER: ai.IntentType.OUT_OF_SCOPE,
        PrimaryAction.OFF_TOPIC_BOUNDARY: ai.IntentType.OFF_TOPIC,
        PrimaryAction.ASK_CLARIFICATION: ai.IntentType.ASK_INFO,
        PrimaryAction.SOCIAL_REPLY: ai.IntentType.ASK_INFO,
        PrimaryAction.FALLBACK: ai.IntentType.FALLBACK,
    }
    conversation_act = ai.ConversationAct.COMPLAINT if facts.complaint else ai.ConversationAct.REQUEST
    authority_route = ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED if decision.needs_owner else ai.AuthorityRoute.NORMAL
    return ai.Intent(
        type=action_map.get(decision.primary_action, ai.IntentType.FALLBACK),
        entities=entities,
        conversation_act=conversation_act,
        authority_route=authority_route,
        reply_text="",
    )


def decision_from_intent(intent: ai.Intent) -> TurnDecision:
    entities = intent.entities or {}
    facts = DecisionFacts(
        service_name=entities.get("service_name"),
        service_names=list(entities.get("service_names") or []),
        product_name=entities.get("product_name"),
        quantity=entities.get("quantity"),
        date_text=entities.get("date_text"),
        time_text=entities.get("time_text"),
        payment_phone=entities.get("payment_phone"),
        complaint=intent.conversation_act == ai.ConversationAct.COMPLAINT,
        off_topic=intent.type == ai.IntentType.OFF_TOPIC,
    )

    mapping = {
        ai.IntentType.ASK_INFO: PrimaryAction.ASK_BUSINESS_INFO,
        ai.IntentType.LIST_SERVICES: PrimaryAction.ASK_CATALOG,
        ai.IntentType.LIST_PRODUCTS: PrimaryAction.ASK_CATALOG,
        ai.IntentType.BOOK_SERVICE: PrimaryAction.START_BOOKING,
        ai.IntentType.BUY_PRODUCT: PrimaryAction.START_ORDER,
        ai.IntentType.CHECK_STATUS: PrimaryAction.CHECK_STATUS,
        ai.IntentType.CANCEL_BOOKING: PrimaryAction.START_CANCEL_BOOKING,
        ai.IntentType.CANCEL_ORDER: PrimaryAction.START_CANCEL_ORDER,
        ai.IntentType.RESCHEDULE_BOOKING: PrimaryAction.START_RESCHEDULE_BOOKING,
        ai.IntentType.RESEND_DEPOSIT: PrimaryAction.RESEND_DEPOSIT_PROMPT,
        ai.IntentType.CONFIRM_ACTION: PrimaryAction.CONFIRM_PENDING_ACTION,
        ai.IntentType.CANCEL_ACTION: PrimaryAction.CANCEL_PENDING_ACTION,
        ai.IntentType.OUT_OF_SCOPE: PrimaryAction.ESCALATE_TO_OWNER,
        ai.IntentType.OFF_TOPIC: PrimaryAction.OFF_TOPIC_BOUNDARY,
        ai.IntentType.FALLBACK: PrimaryAction.FALLBACK,
    }
    primary = mapping.get(intent.type, PrimaryAction.FALLBACK)

    if primary in (PrimaryAction.START_BOOKING, PrimaryAction.START_ORDER):
        state_policy = StatePolicy.UPDATE_PENDING
    elif primary == PrimaryAction.CANCEL_PENDING_ACTION:
        state_policy = StatePolicy.CLEAR_PENDING
    else:
        state_policy = StatePolicy.PRESERVE

    needs_owner = (
        primary == PrimaryAction.ESCALATE_TO_OWNER
        or intent.authority_route == ai.AuthorityRoute.OWNER_AUTHORITY_REQUIRED
        or intent.conversation_act
        in {ai.ConversationAct.COMPLAINT, ai.ConversationAct.HUMAN_REQUEST, ai.ConversationAct.PROPOSAL}
    )

    return TurnDecision(
        primary_action=primary,
        facts=facts,
        state_policy=state_policy,
        needs_owner=needs_owner,
        reason=f"from intent {intent.type.value}",
    )
