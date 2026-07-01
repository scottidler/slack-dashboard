from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from slack_dashboard.config import WorkWindowConfig
from slack_dashboard.worktime import business_hours_between

PT = ZoneInfo("America/Los_Angeles")


def _epoch(year: int, month: int, day: int, hour: int, minute: int = 0, tz: ZoneInfo = PT) -> float:
    """Epoch seconds for a local wall-clock time in the given timezone."""
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


def _cfg(**overrides: object) -> WorkWindowConfig:
    return WorkWindowConfig(**overrides)  # type: ignore[arg-type]


def test_intra_day_span_full_window() -> None:
    # A weekday (Wed 2026-06-24) 6am -> 6pm is the full 12-hour window.
    work = _cfg()
    start = _epoch(2026, 6, 24, 6)
    end = _epoch(2026, 6, 24, 18)
    assert business_hours_between(start, end, work) == pytest.approx(12.0)


def test_intra_day_partial_window() -> None:
    # Wed 1pm -> 8pm: the 6pm-8pm tail is outside the window, so 5 working hours.
    work = _cfg()
    start = _epoch(2026, 6, 24, 13)
    end = _epoch(2026, 6, 24, 20)
    assert business_hours_between(start, end, work) == pytest.approx(5.0)


def test_span_entirely_before_window() -> None:
    # Wed 2am -> 5am: entirely before 6am, contributes nothing.
    work = _cfg()
    start = _epoch(2026, 6, 24, 2)
    end = _epoch(2026, 6, 24, 5)
    assert business_hours_between(start, end, work) == pytest.approx(0.0)


def test_span_entirely_after_window() -> None:
    # Wed 7pm -> 11pm: entirely after 6pm, contributes nothing.
    work = _cfg()
    start = _epoch(2026, 6, 24, 19)
    end = _epoch(2026, 6, 24, 23)
    assert business_hours_between(start, end, work) == pytest.approx(0.0)


def test_overnight_frozen() -> None:
    # Wed 5pm -> Thu 7am: only 5pm-6pm Wed (1h) + 6am-7am Thu (1h) count; night is frozen.
    work = _cfg()
    start = _epoch(2026, 6, 24, 17)
    end = _epoch(2026, 6, 25, 7)
    assert business_hours_between(start, end, work) == pytest.approx(2.0)


def test_weekend_frozen() -> None:
    # Sat 2026-06-27 noon -> Sun 2026-06-28 noon: neither is a work day -> 0.
    work = _cfg()
    start = _epoch(2026, 6, 27, 12)
    end = _epoch(2026, 6, 28, 12)
    assert business_hours_between(start, end, work) == pytest.approx(0.0)


def test_friday_afternoon_to_monday_morning_weekend_frozen() -> None:
    # Fri 2026-06-26 4pm -> Mon 2026-06-29 9am: Fri 4-6pm (2h) + Mon 6-9am (3h) = 5 work-hrs;
    # the whole weekend contributes 0. This is the doc's "weekend frozen" worked example.
    work = _cfg()
    start = _epoch(2026, 6, 26, 16)
    end = _epoch(2026, 6, 29, 9)
    assert business_hours_between(start, end, work) == pytest.approx(5.0)


def test_multi_day_span() -> None:
    # Mon 2026-06-22 noon -> Wed 2026-06-24 noon: Mon 12-6pm (6h) + full Tue (12h) +
    # Wed 6am-noon (6h) = 24 working hours.
    work = _cfg()
    start = _epoch(2026, 6, 22, 12)
    end = _epoch(2026, 6, 24, 12)
    assert business_hours_between(start, end, work) == pytest.approx(24.0)


def test_end_before_start_returns_zero() -> None:
    work = _cfg()
    start = _epoch(2026, 6, 24, 12)
    end = _epoch(2026, 6, 24, 10)
    assert business_hours_between(start, end, work) == 0.0


def test_end_equals_start_returns_zero() -> None:
    work = _cfg()
    start = _epoch(2026, 6, 24, 12)
    assert business_hours_between(start, start, work) == 0.0


def test_reply_after_now_never_negative() -> None:
    # A reply timestamped after `now` (clock skew): start > end -> 0.0, never negative.
    work = _cfg()
    now = _epoch(2026, 6, 24, 12)
    reply = _epoch(2026, 6, 24, 13)
    assert business_hours_between(reply, now, work) == 0.0


def test_spring_forward_dst_boundary() -> None:
    # Spring-forward 2026: clocks jump 2am -> 3am on Sun 2026-03-08 (a 23-hour day, but a
    # weekend so it contributes 0). Span Fri 2026-03-06 5pm -> Mon 2026-03-09 7am crosses it:
    # Fri 5-6pm (1h) + Mon 6-7am (1h) = 2 work-hrs; the DST weekend is frozen regardless.
    work = _cfg()
    start = _epoch(2026, 3, 6, 17)
    end = _epoch(2026, 3, 9, 7)
    assert business_hours_between(start, end, work) == pytest.approx(2.0)


def test_spring_forward_within_workday_window() -> None:
    # The 6am-6pm window never contains the 2am spring-forward gap, so a workday window on a
    # spring-forward-adjacent Monday is exactly 12 wall-clock hours. Mon 2026-03-09 6am-6pm.
    work = _cfg()
    start = _epoch(2026, 3, 9, 6)
    end = _epoch(2026, 3, 9, 18)
    assert business_hours_between(start, end, work) == pytest.approx(12.0)


def test_fall_back_dst_boundary() -> None:
    # Fall-back 2026: clocks repeat 1am -> 1am on Sun 2026-11-01 (a 25-hour day, weekend).
    # Span Fri 2026-10-30 5pm -> Mon 2026-11-02 7am: Fri 5-6pm (1h) + Mon 6-7am (1h) = 2h;
    # the extra fall-back hour lands in the frozen weekend, so it does not inflate the count.
    work = _cfg()
    start = _epoch(2026, 10, 30, 17)
    end = _epoch(2026, 11, 2, 7)
    assert business_hours_between(start, end, work) == pytest.approx(2.0)


def test_custom_window_and_days() -> None:
    # 9am-5pm Mon/Wed only. Tue is excluded. Mon 2026-06-22 8am -> Wed 2026-06-24 6pm:
    # Mon 9am-5pm (8h) + Wed 9am-5pm (8h); Tue skipped = 16 working hours.
    work = _cfg(start_hour=9, end_hour=17, work_days=["mon", "wed"])
    start = _epoch(2026, 6, 22, 8)
    end = _epoch(2026, 6, 24, 18)
    assert business_hours_between(start, end, work) == pytest.approx(16.0)


def test_fractional_hours() -> None:
    # Wed 6:30am -> 7:15am: 45 minutes = 0.75 working hours.
    work = _cfg()
    start = _epoch(2026, 6, 24, 6, 30)
    end = _epoch(2026, 6, 24, 7, 15)
    assert business_hours_between(start, end, work) == pytest.approx(0.75)


# --- WorkWindowConfig validation ---


def test_config_defaults() -> None:
    work = WorkWindowConfig()
    assert work.timezone == "America/Los_Angeles"
    assert work.start_hour == 6
    assert work.end_hour == 18
    assert work.work_days == ["mon", "tue", "wed", "thu", "fri"]
    assert work.work_weekdays() == {0, 1, 2, 3, 4}


def test_config_kebab_aliases() -> None:
    work = WorkWindowConfig.model_validate(
        {
            "timezone": "America/New_York",
            "start-hour": 8,
            "end-hour": 16,
            "work-days": ["tue", "thu"],
        }
    )
    assert work.start_hour == 8
    assert work.end_hour == 16
    assert work.work_weekdays() == {1, 3}


def test_config_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end-hour"):
        WorkWindowConfig(start_hour=18, end_hour=6)


def test_config_rejects_end_equals_start() -> None:
    with pytest.raises(ValueError, match="end-hour"):
        WorkWindowConfig(start_hour=9, end_hour=9)


def test_config_rejects_empty_work_days() -> None:
    with pytest.raises(ValueError, match="work-days must not be empty"):
        WorkWindowConfig(work_days=[])


def test_config_rejects_unknown_work_day() -> None:
    with pytest.raises(ValueError, match="unknown day tokens"):
        WorkWindowConfig(work_days=["mon", "funday"])


def test_config_rejects_unresolvable_timezone() -> None:
    with pytest.raises(ValueError, match="not resolvable"):
        WorkWindowConfig(timezone="Mars/Olympus_Mons")


def test_config_case_insensitive_days() -> None:
    work = WorkWindowConfig(work_days=["MON", "Tue", "wed"])
    assert work.work_weekdays() == {0, 1, 2}


def test_config_nested_under_heat() -> None:
    from slack_dashboard.config import HeatConfig

    heat = HeatConfig.model_validate({"work-window": {"start-hour": 7, "end-hour": 19}})
    assert heat.work_window.start_hour == 7
    assert heat.work_window.end_hour == 19
    # Default when omitted.
    assert HeatConfig().work_window.start_hour == 6
