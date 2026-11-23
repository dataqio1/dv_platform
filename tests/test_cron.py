"""Minimal cron matcher."""

from __future__ import annotations

from datetime import datetime

import pytest

from dvp.core.cron import is_due


def test_every_minute():
    assert is_due("* * * * *", datetime(2026, 10, 8, 3, 17))


def test_daily_at_7am():
    assert is_due("0 7 * * *", datetime(2026, 10, 8, 7, 0))
    assert not is_due("0 7 * * *", datetime(2026, 10, 8, 7, 1))
    assert not is_due("0 7 * * *", datetime(2026, 10, 8, 8, 0))


def test_step_and_list():
    assert is_due("*/15 * * * *", datetime(2026, 10, 8, 9, 30))
    assert not is_due("*/15 * * * *", datetime(2026, 10, 8, 9, 31))
    assert is_due("0 9,17 * * *", datetime(2026, 10, 8, 17, 0))


def test_weekly_on_sunday():
    # 2026-10-11 is a Sunday
    assert is_due("0 8 * * 0", datetime(2026, 10, 11, 8, 0))
    assert is_due("0 8 * * 7", datetime(2026, 10, 11, 8, 0))  # 7 also = Sunday
    assert not is_due("0 8 * * 0", datetime(2026, 10, 8, 8, 0))  # a Thursday


def test_range():
    assert is_due("0 9-17 * * 1-5", datetime(2026, 10, 8, 13, 0))  # Thu 1pm
    assert not is_due("0 9-17 * * 1-5", datetime(2026, 10, 11, 13, 0))  # Sunday


def test_malformed_raises():
    with pytest.raises(ValueError):
        is_due("* * *", datetime(2026, 10, 8, 0, 0))
