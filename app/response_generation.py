"""Grounded response generation.

This is the response layer boundary: callers pass only validated facts and a
required next step.  The function currently renders deterministic WhatsApp
copy for payment/booking-critical turns, which is safer than asking a model to
free-write those promises.  A future LLM response stylist can plug in here as
long as it obeys the same contract.
"""
from dataclasses import dataclass, field


@dataclass
class ValidatedResponseContract:
    purpose: str
    allowed_facts: dict[str, str]
    required_next_step: str
    forbidden_claims: list[str] = field(default_factory=list)


def render_validated_response(contract: ValidatedResponseContract) -> str:
    if contract.purpose == "booking_summary":
        facts = contract.allowed_facts
        return (
            f"Here's what I have: {facts['service']} on {facts['slot']} "
            f"({facts['price']}, {facts['duration']})"
            f"{facts['deposit_text']}, or let me know if you'd like to change anything."
        )
    if contract.purpose == "order_summary":
        facts = contract.allowed_facts
        return (
            f"Here's what I have: {facts['summary']}"
            f"{facts['deposit_text']}, or let me know if you'd like to change anything."
        )
    return contract.required_next_step
