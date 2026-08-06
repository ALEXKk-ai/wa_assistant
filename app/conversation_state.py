"""Helpers for stricter conversation pending-state metadata.

ConversationState.state_json remains a JSON blob for compatibility, but new
pending records carry form-like metadata so handlers can reason about missing
fields, locked fields, and the last prompt without letting the LLM freely
overwrite the whole object.
"""
from copy import deepcopy


BOOKING_FIELDS = ("service_id", "service_name", "date_text", "time_text", "slot_start_iso")
ORDER_FIELDS = ("product_id", "product_name", "quantity", "fulfillment_type", "delivery_address")


def normalize_pending_form(stage: str, pending: dict | None) -> dict:
    normalized = deepcopy(pending or {})
    ptype = normalized.get("type")
    if not ptype:
        return normalized

    fields = BOOKING_FIELDS if ptype in ("booking", "reschedule_booking", "booking_time_retry") else ORDER_FIELDS
    missing = [field for field in fields if field in _required_fields(ptype, normalized) and not normalized.get(field)]
    locked = list(normalized.get("locked_fields") or [])
    for field in fields:
        if normalized.get(field) and field not in locked:
            locked.append(field)

    normalized["state_version"] = 2
    normalized["form_fields"] = {field: normalized.get(field) for field in fields if field in normalized}
    normalized["missing_fields"] = missing
    normalized["locked_fields"] = locked
    normalized["last_prompt"] = _infer_last_prompt(stage, normalized)
    return normalized


def _required_fields(ptype: str, pending: dict) -> tuple[str, ...]:
    if ptype == "booking":
        return ("service_id", "date_text", "time_text")
    if ptype in ("reschedule_booking", "booking_time_retry"):
        return ("date_text", "time_text")
    if ptype == "order":
        if pending.get("fulfillment_type") == "delivery":
            return ("product_id", "quantity", "fulfillment_type", "delivery_address")
        return ("product_id", "quantity", "fulfillment_type")
    return ()


def _infer_last_prompt(stage: str, pending: dict) -> str | None:
    if pending.get("slot_start_iso") or pending.get("new_slot_start_iso"):
        return "confirm"
    missing = pending.get("missing_fields") or []
    if "service_id" in missing:
        return "ask_service"
    if "product_id" in missing:
        return "ask_product"
    if "date_text" in missing:
        return "ask_date"
    if "time_text" in missing:
        return "ask_time"
    if "quantity" in missing:
        return "ask_quantity"
    if "fulfillment_type" in missing:
        return "ask_fulfillment"
    if "delivery_address" in missing:
        return "ask_delivery_address"
    return stage
