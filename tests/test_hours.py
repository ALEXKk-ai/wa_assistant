from datetime import datetime

from app import hours as hours_mod


def test_parse_simple_weekday_range():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00")
    assert hours["mon"] == {"open": "09:00", "close": "18:00"}
    assert hours["fri"] == {"open": "09:00", "close": "18:00"}
    assert hours["sat"] is None
    assert hours["sun"] is None


def test_parse_multiple_segments():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00, Sat 10:00-14:00")
    assert hours["sat"] == {"open": "10:00", "close": "14:00"}
    assert hours["sun"] is None


def test_parse_single_day():
    hours = hours_mod.parse_hours_spec("Sun 12:00-16:00")
    assert hours["sun"] == {"open": "12:00", "close": "16:00"}
    assert hours["mon"] is None


def test_empty_spec_means_no_restriction():
    hours = hours_mod.parse_hours_spec("")
    assert all(v is None for v in hours.values())
    ok, msg = hours_mod.is_within_hours(hours, datetime(2026, 8, 24, 3, 0), datetime(2026, 8, 24, 4, 0))
    assert ok is True
    assert msg == ""


def test_bad_day_raises():
    import pytest

    with pytest.raises(hours_mod.HoursParseError):
        hours_mod.parse_hours_spec("Notaday 09:00-18:00")


def test_bad_time_raises():
    import pytest

    with pytest.raises(hours_mod.HoursParseError):
        hours_mod.parse_hours_spec("Mon 25:00-18:00")


def test_malformed_segment_raises():
    import pytest

    with pytest.raises(hours_mod.HoursParseError):
        hours_mod.parse_hours_spec("Mon-Fri")


def test_slot_within_hours_accepted():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00")
    # 2026-08-24 is a Monday
    start = datetime(2026, 8, 24, 10, 0)
    end = datetime(2026, 8, 24, 10, 45)
    ok, msg = hours_mod.is_within_hours(hours, start, end)
    assert ok is True


def test_slot_before_opening_rejected():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00")
    start = datetime(2026, 8, 24, 7, 0)
    end = datetime(2026, 8, 24, 7, 45)
    ok, msg = hours_mod.is_within_hours(hours, start, end)
    assert ok is False
    assert "outside our hours" in msg


def test_slot_on_closed_day_rejected():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00")
    # 2026-08-30 is a Sunday
    start = datetime(2026, 8, 30, 10, 0)
    end = datetime(2026, 8, 30, 10, 45)
    ok, msg = hours_mod.is_within_hours(hours, start, end)
    assert ok is False
    assert "closed on Sunday" in msg


def test_slot_ending_after_closing_rejected():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00")
    start = datetime(2026, 8, 24, 17, 45)
    end = datetime(2026, 8, 24, 18, 30)  # runs 30 min past close
    ok, msg = hours_mod.is_within_hours(hours, start, end)
    assert ok is False
    assert "past closing time" in msg


def test_format_hours_shows_all_days():
    hours = hours_mod.parse_hours_spec("Mon-Fri 09:00-18:00, Sat 10:00-14:00")
    text = hours_mod.format_hours(hours)
    assert "Monday: 09:00-18:00" in text
    assert "Saturday: 10:00-14:00" in text
    assert "Sunday: closed" in text


def test_format_unset_hours():
    hours = {d: None for d in hours_mod.DAYS}
    assert "no restrictions" in hours_mod.format_hours(hours)
