"""Business operating-hours: parsing, formatting, and slot validation.

Storage: hours are kept on Business.hours_json as a JSON blob keyed by
3-letter lowercase weekday (mon..sun), each value either null (closed that
day) or {"open": "HH:MM", "close": "HH:MM"} in 24h time.

Migration behavior: an empty/missing hours blob ("{}" or all-null) means
"no restriction" - this is deliberate so businesses provisioned before this
feature existed keep working exactly as before until the operator sets
hours via `update-business-hours`. See README.
"""
import re
from datetime import datetime, time

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


class HoursParseError(ValueError):
    pass


def parse_hours_spec(spec: str) -> dict:
    """Parses a human-friendly spec, e.g. "Mon-Fri 09:00-18:00, Sat 10:00-14:00".
    Days not mentioned are closed. Raises HoursParseError on bad input so the
    CLI can show a clear message instead of silently storing garbage."""
    hours = {d: None for d in DAYS}
    if not spec or not spec.strip():
        return hours
    for segment in (s.strip() for s in spec.split(",") if s.strip()):
        parts = segment.split()
        if len(parts) != 2:
            raise HoursParseError(f"Couldn't parse '{segment}' - expected e.g. 'Mon-Fri 09:00-18:00'")
        day_part, time_part = parts
        if "-" not in time_part:
            raise HoursParseError(f"Couldn't parse time range '{time_part}' - expected e.g. '09:00-18:00'")
        open_str, close_str = time_part.split("-", 1)
        _validate_time(open_str)
        _validate_time(close_str)
        for day in _expand_day_range(day_part):
            hours[day] = {"open": open_str, "close": close_str}
    return hours


def _validate_time(value: str) -> None:
    if not _TIME_RE.match(value):
        raise HoursParseError(f"Invalid time '{value}' - expected 24h HH:MM")
    h, m = value.split(":")
    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise HoursParseError(f"Invalid time '{value}'")


def _expand_day_range(day_part: str) -> list[str]:
    if "-" in day_part:
        start_raw, end_raw = day_part.split("-", 1)
        start, end = _normalize_day(start_raw), _normalize_day(end_raw)
        start_i, end_i = DAYS.index(start), DAYS.index(end)
        if start_i <= end_i:
            return DAYS[start_i:end_i + 1]
        return DAYS[start_i:] + DAYS[:end_i + 1]  # e.g. Fri-Mon wraps the week
    return [_normalize_day(day_part)]


def _normalize_day(raw: str) -> str:
    key = raw.strip().lower()[:3]
    if key not in DAYS:
        raise HoursParseError(f"Unrecognized day '{raw}'")
    return key


def format_hours(hours: dict | None) -> str:
    if not hours or all(v is None for v in hours.values()):
        return "Hours not set - no restrictions on booking times."
    lines = []
    for day in DAYS:
        info = hours.get(day)
        lines.append(f"{DAY_NAMES[day]}: closed" if info is None else f"{DAY_NAMES[day]}: {info['open']}-{info['close']}")
    return "; ".join(lines)


def is_within_hours(hours: dict | None, slot_start: datetime, slot_end: datetime) -> tuple[bool, str]:
    """Returns (ok, message) - message explains the rejection when ok is
    False, and is empty when ok is True. An empty/unset hours blob always
    passes (see migration note in the module docstring)."""
    if not hours or all(v is None for v in hours.values()):
        return True, ""

    day_key = DAYS[slot_start.weekday()]
    info = hours.get(day_key)
    if info is None:
        return False, f"We're closed on {DAY_NAMES[day_key]}s. Hours: {format_hours(hours)}"

    open_t = _parse_hhmm(info["open"])
    close_t = _parse_hhmm(info["close"])
    if not (open_t <= slot_start.time() < close_t):
        return False, (
            f"That's outside our hours on {DAY_NAMES[day_key]} "
            f"({info['open']}-{info['close']}). What other time works?"
        )
    if slot_end.time() > close_t or slot_end.date() != slot_start.date():
        return False, (
            f"That would run past closing time on {DAY_NAMES[day_key]} "
            f"({info['open']}-{info['close']}). Would an earlier time work?"
        )
    return True, ""


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))
