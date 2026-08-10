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


def test_parse_next_week_weekday():
    now = datetime.now()
    result_this = _parse_date_text("this Thursday")
    result_next = _parse_date_text("Thursday next week")
    assert result_this is not None
    assert result_next is not None
    assert (result_next.date() - result_this.date()).days == 7


def test_combine_with_bare_digit_time():
    combined_11 = _combine_date_and_time("25 December 2026", "11")
    assert combined_11 is not None
    assert combined_11.hour == 11

    combined_2 = _combine_date_and_time("25 December 2026", "2")
    assert combined_2 is not None
    assert combined_2.hour == 14


def test_parse_tomorrow_phrases():
    now = datetime.now()
    tmr = _parse_date_text("tomorrow")
    tmr_slang = _parse_date_text("tmrw")
    day_after = _parse_date_text("day after tomorrow")
    assert tmr is not None
    assert (tmr.date() - now.date()).days == 1
    assert tmr_slang is not None
    assert (tmr_slang.date() - now.date()).days == 1
    assert day_after is not None
    assert (day_after.date() - now.date()).days == 2
