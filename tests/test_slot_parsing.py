from datetime import datetime, timedelta

from app.workflows.customer import _combine_date_and_time, _parse_date_text


def test_parse_weekday_rolls_forward_to_future():
    result = _parse_date_text("Thursday")
    assert result is not None
    now = datetime.now()
    assert result.date() >= now.date()
    assert result.strftime("%A") == "Thursday"


def test_parse_explicit_date():
    result = _parse_date_text("25 December 2026")
    assert result is not None
    assert (result.year, result.month, result.day) == (2026, 12, 25)


def test_parse_garbage_date_returns_none():
    assert _parse_date_text("blah blah not a date") is None


def test_combine_date_and_time():
    combined = _combine_date_and_time("25 December 2026", "2pm")
    assert combined is not None
    assert (combined.year, combined.month, combined.day) == (2026, 12, 25)
    assert combined.hour == 14


def test_combine_with_24h_time():
    combined = _combine_date_and_time("25 December 2026", "14:30")
    assert combined.hour == 14
    assert combined.minute == 30


def test_combine_with_bad_time_returns_none():
    assert _combine_date_and_time("25 December 2026", "not a time at all") is None
