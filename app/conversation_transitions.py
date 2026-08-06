"""Explicit transition descriptions for conversation state changes.

The workflow handlers still perform the domain work, but all saved stage
changes can be classified here for logging, tests, and future migration to a
fully formal state machine.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StateTransition:
    from_stage: str
    to_stage: str
    from_type: str | None
    to_type: str | None
    kind: str


def describe_transition(old_stage: str, old_pending: dict, new_stage: str, new_pending: dict) -> StateTransition:
    old_type = (old_pending or {}).get("type")
    new_type = (new_pending or {}).get("type")
    if new_stage == old_stage and old_type == new_type:
        kind = "preserve"
    elif new_stage == "idle" and not new_pending:
        kind = "clear"
    elif new_stage == "confirming":
        kind = "ready_to_confirm"
    elif new_stage.startswith("collecting"):
        kind = "collect_fields"
    elif new_stage.startswith("selecting"):
        kind = "select_record"
    else:
        kind = "transition"
    return StateTransition(
        from_stage=old_stage,
        to_stage=new_stage,
        from_type=old_type,
        to_type=new_type,
        kind=kind,
    )
