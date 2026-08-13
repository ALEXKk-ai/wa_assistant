"""Policy guardrails for one-turn conversation decisions.

The LLM proposes a direction; this module decides whether that direction is
allowed for the current state.  It does not touch the database and it does not
generate final business replies.  Its main job is preventing broad labels
like COMPLAINT, OUT_OF_SCOPE, OFF_TOPIC, or CANCEL_ACTION from beating clear
booking/order evidence or destructively clearing pending state.
"""
from dataclasses import dataclass
import re

from app import ai
from app.conversation_decision import (
    PrimaryAction,
    SecondaryAction,
    StatePolicy,
    TurnDecision,
    decision_from_intent,
)
from app.models import BusinessType


_EXPLICIT_CANCEL_RE = re.compile(
    r"\b(cancel\s+(this|that|request|action)|never\s*mind|nevermind|stop|start\s+over|forget\s+it|abort)\b",
    re.IGNORECASE,
)
_OFF_TOPIC_RE = re.compile(
    r"\b(weather|recipe|football|news|politics|write\s+code|python|javascript|homework|movie)\b",
    re.IGNORECASE,
)
_COMPLAINT_RE = re.compile(
    r"\b(complaint|bad|horrible|unhappy|late|delay|ruined|disappointed|terrible|worst|stupid|useless)\b",
    re.IGNORECASE,
)
_BOOKING_PHRASE_RE = re.compile(
    r"\b(book|booking|appointment|reserve|come|visit|schedule|slot)\b",
    re.IGNORECASE,
)
_ORDER_PHRASE_RE = re.compile(
    r"\b(buy|order|purchase|deliver|pickup|stock|in\s+stock|available)\b",
    re.IGNORECASE,
)
_BUY_PHRASE_RE = re.compile(r"\b(buy|order|purchase|get\s+me|send|deliver|pickup)\b", re.IGNORECASE)
_STOCK_ONLY_RE = re.compile(r"\b(stock|in\s+stock|restock|re-?stock|do\s+you\s+have|available)\b", re.IGNORECASE)
_CORRECTION_RE = re.compile(
    r"\b(change|move|make|set|switch|actually|instead|i\s+meant|no,\s*i\s+meant)\b",
    re.IGNORECASE,
)


@dataclass
class PolicyResult:
    intent: ai.Intent
    decision: TurnDecision
    skip_pre_route: bool = False
    reason: str = ""


def _clone_intent(intent: ai.Intent, *, type_: ai.IntentType | None = None, reply_text: str | None = None) -> ai.Intent:
    return ai.Intent(
        type=type_ or intent.type,
        entities=dict(intent.entities or {}),
        reply_text=intent.reply_text if reply_text is None else reply_text,
        conversation_act=intent.conversation_act,
        authority_route=intent.authority_route,
    )


def _catalog_mentioned(message_text: str, catalog_names: list[str]) -> bool:
    lowered = message_text.lower()
    return any(name and name.lower() in lowered for name in catalog_names)


def _matched_catalog_name(message_text: str, catalog_names: list[str]) -> str | None:
    lowered = message_text.lower()
    matches = [name for name in catalog_names if name and name.lower() in lowered]
    if not matches:
        return None
    return max(matches, key=len)


def _has_fact_value(value) -> bool:
    return value not in (None, "", [])


def _with_extracted_facts(
    intent: ai.Intent,
    *,
    type_: ai.IntentType,
    matched_name: str | None,
    business_type: BusinessType,
    date_text: str | None,
    time_text: str | None,
) -> ai.Intent:
    fixed = _clone_intent(intent, type_=type_)
    entities = dict(fixed.entities or {})
    if matched_name:
        if business_type == BusinessType.SERVICES and not entities.get("service_name"):
            entities["service_name"] = matched_name
        if business_type == BusinessType.GOODS and not entities.get("product_name"):
            entities["product_name"] = matched_name
    if date_text and not entities.get("date_text"):
        entities["date_text"] = date_text
    if time_text and not entities.get("time_text"):
        entities["time_text"] = time_text
    fixed.entities = entities
    return fixed


def apply_turn_policy(
    *,
    intent: ai.Intent,
    message_text: str,
    business_type: BusinessType,
    stage: str,
    pending: dict,
    catalog_names: list[str],
    date_text_signal: str | None,
    time_text_signal: str | None,
    stage_confirming: str,
    active_detail_stages: set[str],
) -> PolicyResult:
    decision = decision_from_intent(intent)
    lowered = message_text.lower()
    complaint_signal = bool(_COMPLAINT_RE.search(message_text))
    off_topic_signal = bool(_OFF_TOPIC_RE.search(message_text))
    explicit_cancel = bool(_EXPLICIT_CANCEL_RE.search(message_text))
    catalog_signal = _catalog_mentioned(message_text, catalog_names)
    matched_name = _matched_catalog_name(message_text, catalog_names)
    has_date_signal = date_text_signal is not None or _has_fact_value(decision.facts.date_text)
    has_time_signal = time_text_signal is not None or _has_fact_value(decision.facts.time_text)
    booking_phrase = bool(_BOOKING_PHRASE_RE.search(message_text))
    order_phrase = bool(_ORDER_PHRASE_RE.search(message_text))
    buy_phrase = bool(_BUY_PHRASE_RE.search(message_text))
    stock_only = bool(_STOCK_ONLY_RE.search(message_text)) and not buy_phrase
    service_fact = _has_fact_value(decision.facts.service_name) or bool(decision.facts.service_names)
    product_fact = _has_fact_value(decision.facts.product_name)
    detail_fact = service_fact or product_fact or has_date_signal or has_time_signal
    correction_signal = bool(_CORRECTION_RE.search(message_text))

    decision.facts.complaint = decision.facts.complaint or complaint_signal
    decision.facts.cancel_signal = explicit_cancel
    decision.facts.off_topic = off_topic_signal or decision.facts.off_topic

    # A pending confirmation has its own deterministic confirmation/phone guard
    # in customer.py.  Do not let social pre-routing intercept it.
    if stage == stage_confirming and intent.type == ai.IntentType.CONFIRM_ACTION:
        decision.primary_action = PrimaryAction.CONFIRM_PENDING_ACTION
        decision.state_policy = StatePolicy.PRESERVE
        return PolicyResult(intent=intent, decision=decision, skip_pre_route=True, reason="confirming input")

    if intent.type == ai.IntentType.RESCHEDULE_BOOKING and pending and pending.get("type") in ("booking", "reschedule_booking", "booking_time_retry"):
        fixed = _with_extracted_facts(
            intent,
            type_=ai.IntentType.BOOK_SERVICE,
            matched_name=matched_name,
            business_type=business_type,
            date_text=date_text_signal,
            time_text=time_text_signal,
        )
        decision.primary_action = PrimaryAction.CHANGE_BOOKING_FIELD
        decision.state_policy = StatePolicy.UPDATE_PENDING
        return PolicyResult(
            intent=fixed,
            decision=decision,
            skip_pre_route=True,
            reason="reschedule intent remapped to draft booking update while pending draft exists",
        )

    if intent.type in (ai.IntentType.BOOK_SERVICE, ai.IntentType.BUY_PRODUCT) and not complaint_signal:
        decision.state_policy = StatePolicy.UPDATE_PENDING
        return PolicyResult(
            intent=intent,
            decision=decision,
            skip_pre_route=True,
            reason="normal commerce flow skips social pre-route",
        )

    if stage == stage_confirming and pending and correction_signal and detail_fact and not complaint_signal:
        if pending.get("type") in ("booking", "reschedule_booking", "booking_time_retry"):
            fixed = _with_extracted_facts(
                intent,
                type_=ai.IntentType.BOOK_SERVICE,
                matched_name=matched_name,
                business_type=business_type,
                date_text=date_text_signal,
                time_text=time_text_signal,
            )
            decision.primary_action = PrimaryAction.CHANGE_BOOKING_FIELD
            decision.state_policy = StatePolicy.UPDATE_PENDING
            return PolicyResult(
                intent=fixed,
                decision=decision,
                skip_pre_route=True,
                reason="booking correction while confirming",
            )
        if pending.get("type") == "order":
            fixed = _with_extracted_facts(
                intent,
                type_=ai.IntentType.BUY_PRODUCT,
                matched_name=matched_name,
                business_type=business_type,
                date_text=date_text_signal,
                time_text=time_text_signal,
            )
            decision.primary_action = PrimaryAction.CHANGE_BOOKING_FIELD
            decision.state_policy = StatePolicy.UPDATE_PENDING
            return PolicyResult(
                intent=fixed,
                decision=decision,
                skip_pre_route=True,
                reason="order correction while confirming",
            )

    if (
        stage == stage_confirming
        and pending
        and pending.get("type") not in ("cancel_booking", "cancel_order")
        and correction_signal
        and not detail_fact
        and not complaint_signal
    ):
        guarded = _clone_intent(
            intent,
            type_=ai.IntentType.ASK_INFO,
            reply_text="Sure - would you like to change the service, date, time, or payment phone number?",
        )
        decision.primary_action = PrimaryAction.ASK_CLARIFICATION
        decision.state_policy = StatePolicy.PRESERVE
        return PolicyResult(
            intent=guarded,
            decision=decision,
            skip_pre_route=True,
            reason="vague correction asks which field",
        )

    if intent.type == ai.IntentType.CANCEL_ACTION and not pending and explicit_cancel:
        target_intent = ai.IntentType.CANCEL_BOOKING if business_type == BusinessType.SERVICES else ai.IntentType.CANCEL_ORDER
        target_action = PrimaryAction.START_CANCEL_BOOKING if business_type == BusinessType.SERVICES else PrimaryAction.START_CANCEL_ORDER
        guarded = _clone_intent(intent, type_=target_intent)
        decision.primary_action = target_action
        decision.state_policy = StatePolicy.PRESERVE
        return PolicyResult(
            intent=guarded,
            decision=decision,
            skip_pre_route=True,
            reason="idle cancellation intent remapped to database cancellation check",
        )

    # Only explicit draft-cancel language may clear pending state.  Off-topic
    # chatter or vague model uncertainty should preserve the active request.
    if (
        intent.type == ai.IntentType.CANCEL_ACTION
        and pending
        and pending.get("type") not in ("cancel_booking", "cancel_order")
        and not explicit_cancel
    ):
        reply = (
            f"I can help with {', '.join(catalog_names) if catalog_names else 'the business'} here. "
            "Your current request is still saved - tell me what you'd like to change, or say 'cancel this request' to start over."
        )
        guarded = _clone_intent(intent, type_=ai.IntentType.OFF_TOPIC, reply_text=reply)
        decision.primary_action = PrimaryAction.OFF_TOPIC_BOUNDARY
        decision.secondary_actions.append(SecondaryAction.PRESERVE_PENDING_CONTEXT)
        decision.state_policy = StatePolicy.PRESERVE
        return PolicyResult(
            intent=guarded,
            decision=decision,
            skip_pre_route=True,
            reason="blocked non-explicit pending cancellation",
        )

    # Clear booking/order evidence should override false escalation or technical fallbacks (OUT_OF_SCOPE/FALLBACK)
    # ONLY if explicit booking/buy phrases ("want to book", "reserve", "schedule", "buy") exist.
    # DO NOT hijack ASK_INFO / ASK_BUSINESS_INFO availability queries ("is Tuesday 11am available") into forced booking drafts!
    is_price_or_deposit_question = any(w in lowered for w in ("how much", "price", "cost", "deposit", "fee", "how about"))
    booking_signal = (catalog_signal or service_fact) and (booking_phrase or (has_date_signal and has_time_signal and "want" in lowered)) and not is_price_or_deposit_question
    order_signal = (catalog_signal or product_fact) and (buy_phrase or (business_type == BusinessType.GOODS and order_phrase and not stock_only)) and not is_price_or_deposit_question
    can_override_escalation = not complaint_signal and intent.type in {
        ai.IntentType.OUT_OF_SCOPE,
        ai.IntentType.OFF_TOPIC,
        ai.IntentType.FALLBACK,
    }
    if business_type == BusinessType.SERVICES and booking_signal and can_override_escalation:
        fixed = _with_extracted_facts(
            intent,
            type_=ai.IntentType.BOOK_SERVICE,
            matched_name=matched_name,
            business_type=business_type,
            date_text=date_text_signal,
            time_text=time_text_signal,
        )
        decision.primary_action = PrimaryAction.START_BOOKING
        decision.secondary_actions.append(SecondaryAction.ANSWER_SERVICE_AVAILABILITY)
        decision.state_policy = StatePolicy.UPDATE_PENDING
        decision.needs_owner = False
        return PolicyResult(
            intent=fixed,
            decision=decision,
            skip_pre_route=True,
            reason="booking evidence overrode broad classification",
        )

    if business_type == BusinessType.GOODS and order_signal and can_override_escalation:
        fixed = _with_extracted_facts(
            intent,
            type_=ai.IntentType.BUY_PRODUCT,
            matched_name=matched_name,
            business_type=business_type,
            date_text=date_text_signal,
            time_text=time_text_signal,
        )
        decision.primary_action = PrimaryAction.START_ORDER
        decision.secondary_actions.append(SecondaryAction.ANSWER_PRODUCT_AVAILABILITY)
        decision.state_policy = StatePolicy.UPDATE_PENDING
        decision.needs_owner = False
        return PolicyResult(
            intent=fixed,
            decision=decision,
            skip_pre_route=True,
            reason="order evidence overrode broad classification",
        )
    # should continue that flow instead of being intercepted as unclear or
    # owner-required.  Complaints still escalate, but state is preserved.
    info_or_status_intent = intent.type in {
        ai.IntentType.ASK_INFO,
        ai.IntentType.LIST_SERVICES,
        ai.IntentType.LIST_PRODUCTS,
        ai.IntentType.CHECK_STATUS,
        ai.IntentType.OUT_OF_SCOPE,
        ai.IntentType.OFF_TOPIC,
    }
    if stage in active_detail_stages and detail_fact and not complaint_signal and not info_or_status_intent:
        if pending.get("type") in ("booking", "reschedule_booking", "booking_time_retry"):
            fixed_type = ai.IntentType.BOOK_SERVICE
            primary = PrimaryAction.CONTINUE_BOOKING
        elif pending.get("type") == "order":
            fixed_type = ai.IntentType.BUY_PRODUCT
            primary = PrimaryAction.CONTINUE_ORDER
        else:
            fixed_type = intent.type
            primary = decision.primary_action
        if fixed_type != intent.type:
            fixed = _clone_intent(intent, type_=fixed_type)
            decision.primary_action = primary
            decision.state_policy = StatePolicy.UPDATE_PENDING
            return PolicyResult(
                intent=fixed,
                decision=decision,
                skip_pre_route=True,
                reason="active detail flow continued from extracted facts",
            )

    if off_topic_signal and pending:
        reply = (
            "I can only help with this business's services, products, bookings, orders, and hours here. "
            "Your current request is still saved."
        )
        fixed = _clone_intent(intent, type_=ai.IntentType.OFF_TOPIC, reply_text=reply)
        decision.primary_action = PrimaryAction.OFF_TOPIC_BOUNDARY
        decision.secondary_actions.append(SecondaryAction.PRESERVE_PENDING_CONTEXT)
        decision.state_policy = StatePolicy.PRESERVE
        return PolicyResult(intent=fixed, decision=decision, skip_pre_route=True, reason="off-topic preserves pending")

    return PolicyResult(intent=intent, decision=decision)
