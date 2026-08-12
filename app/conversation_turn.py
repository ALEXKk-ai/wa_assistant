"""Conversation turn processor.

This module centralizes the non-DB parts of one customer turn:
classification, strict decision adaptation, policy validation, and diagnostic
logging.  Domain actions still live in app.workflows.customer so the proven
booking/order/payment handlers remain in place.
"""
from dataclasses import dataclass

from app import ai
from app.conversation_decision import PrimaryAction, StatePolicy
from app.conversation_decision import TurnDecision
from app.conversation_policy import PolicyResult, apply_turn_policy
from app.logging_conf import get_logger, log_extra
from app.models import Business, BusinessType

logger = get_logger(__name__)


@dataclass
class TurnContext:
    business: Business
    message_text: str
    stage: str
    pending: dict
    history: list[dict]
    catalog: list[dict]
    business_hours_text: str
    business_address: str
    business_extra_info: str
    fulfillment_policy: str
    date_text_signal: str | None
    time_text_signal: str | None
    stage_confirming: str
    active_detail_stages: set[str]


@dataclass
class ProcessedTurn:
    intent: ai.Intent
    decision: TurnDecision
    skip_pre_route: bool
    policy_reason: str


class ConversationTurnProcessor:
    async def process(self, context: TurnContext) -> ProcessedTurn:
        intent, decision = await ai.extract_turn_decision(
            customer_message=context.message_text,
            business_name=context.business.name,
            business_type=context.business.business_type.value,
            catalog=context.catalog,
            conversation_history=context.history,
            pending=context.pending,
            business_hours_text=context.business_hours_text,
            business_address=context.business_address,
            business_extra_info=context.business_extra_info,
            fulfillment_policy=context.fulfillment_policy,
            stage=context.stage,
        )

        intent = self._fallback_booking_recovery(intent, context)
        intent, decision = self._repair_low_confidence(intent, decision, context)
        logger.info(
            "Intent classified",
            extra=log_extra(
                business_id=context.business.id,
                intent=intent.type.value,
                stage=context.stage,
                decision=decision.primary_action.value,
                reasoning=decision.reason,
            ),
        )

        policy = self._apply_policy(intent, context)
        if policy.intent is not intent or policy.skip_pre_route:
            logger.info(
                "Conversation policy applied",
                extra=log_extra(
                    business_id=context.business.id,
                    stage=context.stage,
                    original_intent=intent.type.value,
                    final_intent=policy.intent.type.value,
                    action=policy.decision.primary_action.value,
                    reason=policy.reason,
                ),
            )
        return ProcessedTurn(
            intent=policy.intent,
            decision=policy.decision,
            skip_pre_route=policy.skip_pre_route,
            policy_reason=policy.reason,
        )

    def _apply_policy(self, intent: ai.Intent, context: TurnContext) -> PolicyResult:
        return apply_turn_policy(
            intent=intent,
            message_text=context.message_text,
            business_type=context.business.business_type,
            stage=context.stage,
            pending=context.pending,
            catalog_names=[item.get("name", "") for item in context.catalog],
            date_text_signal=context.date_text_signal,
            time_text_signal=context.time_text_signal,
            stage_confirming=context.stage_confirming,
            active_detail_stages=context.active_detail_stages,
        )

    def _fallback_booking_recovery(self, intent: ai.Intent, context: TurnContext) -> ai.Intent:
        if intent.type != ai.IntentType.FALLBACK:
            return intent
        lowered = context.message_text.lower()
        if context.stage != "idle" or not context.business.business_type == BusinessType.SERVICES:
            return intent
        if not any(word in lowered for word in ("book", "appointment", "reserve")):
            return intent
        if any(
            word in lowered
            for word in ("cancel", "reschedule", "status", "check", "my booking", "my bookings", "existing booking")
        ):
            return intent
        entities = {}
        if context.date_text_signal:
            entities["date_text"] = context.date_text_signal
        if context.time_text_signal:
            entities["time_text"] = context.time_text_signal
        return ai.Intent(type=ai.IntentType.BOOK_SERVICE, entities=entities)

    def _repair_low_confidence(
        self, intent: ai.Intent, decision: TurnDecision, context: TurnContext
    ) -> tuple[ai.Intent, TurnDecision]:
        destructive = {
            PrimaryAction.CANCEL_PENDING_ACTION,
            PrimaryAction.START_CANCEL_BOOKING,
            PrimaryAction.START_CANCEL_ORDER,
            PrimaryAction.START_RESCHEDULE_BOOKING,
            PrimaryAction.RESEND_DEPOSIT_PROMPT,
            PrimaryAction.CONFIRM_PENDING_ACTION,
        }
        if decision.confidence >= 0.55 or decision.primary_action not in destructive:
            return intent, decision
        repaired = ai.Intent(
            type=ai.IntentType.ASK_INFO,
            entities=dict(intent.entities or {}),
            reply_text="Could you clarify what you'd like me to do?",
        )
        decision.primary_action = PrimaryAction.ASK_CLARIFICATION
        decision.state_policy = StatePolicy.PRESERVE
        decision.reason = f"low-confidence destructive action repaired: {decision.reason}"
        logger.info(
            "Low-confidence conversation action repaired",
            extra=log_extra(
                business_id=context.business.id,
                original_action=decision.primary_action.value,
                confidence=decision.confidence,
            ),
        )
        return repaired, decision
