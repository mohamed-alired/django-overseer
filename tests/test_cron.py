from datetime import UTC, datetime

import pytest

from overseer.scheduling.cron import CronError, parse

UTC = UTC


def at(*args):
    return datetime(*args, tzinfo=UTC)


class TestParse:
    def test_every_five_minutes(self):
        c = parse("*/5 * * * *")
        assert c.minutes == frozenset(range(0, 60, 5))
        assert c.next_after(at(2026, 1, 1, 12, 3)) == at(2026, 1, 1, 12, 5)
        assert c.next_after(at(2026, 1, 1, 12, 5)) == at(2026, 1, 1, 12, 10)

    def test_lists_ranges_steps_names(self):
        c = parse("0,30 9-17/2 * jan-mar mon-fri")
        assert c.minutes == frozenset({0, 30})
        assert c.hours == frozenset({9, 11, 13, 15, 17})
        assert c.months == frozenset({1, 2, 3})
        assert c.weekdays == frozenset({1, 2, 3, 4, 5})
        # Sat 3 Jan 2026 -> Mon 5 Jan 09:00
        assert c.next_after(at(2026, 1, 3, 10, 0)) == at(2026, 1, 5, 9, 0)

    def test_aliases_and_sunday_seven(self):
        assert parse("@daily").next_after(at(2026, 1, 1, 5, 0)) == at(2026, 1, 2, 0, 0)
        assert parse("0 0 * * 7").weekdays == frozenset({0})

    def test_day_or_weekday_semantics(self):
        # Both restricted: fires on the 1st OR on Mondays (classic cron behaviour).
        c = parse("0 0 1 * mon")
        assert c.next_after(at(2026, 1, 1, 0, 0)) == at(2026, 1, 5, 0, 0)  # Monday
        assert c.next_after(at(2026, 1, 30, 0, 0)) == at(2026, 2, 1, 0, 0)  # the 1st

    def test_month_end_and_leap(self):
        assert parse("0 0 31 * *").next_after(at(2026, 2, 1)) == at(2026, 3, 31)
        assert parse("0 0 29 2 *").next_after(at(2026, 1, 1)) == at(2028, 2, 29)

    def test_timezone(self):
        c = parse("0 9 * * *")
        nxt = c.next_after(at(2026, 6, 1, 12, 0), tz="Europe/Berlin")
        assert nxt == at(2026, 6, 2, 7, 0)  # 09:00 CEST == 07:00 UTC

    @pytest.mark.parametrize(
        "bad",
        [
            "* * * *",
            "60 * * * *",
            "* 24 * * *",
            "* * 0 * *",
            "* * * 13 *",
            "* * * * 8",
            "a * * * *",
            "5-1 * * * *",
            "*/0 * * * *",
            ",* * * * *",
        ],
    )
    def test_invalid(self, bad):
        with pytest.raises(CronError):
            parse(bad)

    def test_naive_datetime_rejected(self):
        with pytest.raises(CronError):
            parse("* * * * *").next_after(datetime(2026, 1, 1))

    def test_never_fires(self):
        with pytest.raises(CronError):
            parse("0 0 30 2 *").next_after(at(2026, 1, 1))
