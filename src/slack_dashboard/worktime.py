import logging
from datetime import UTC, datetime, time, timedelta

from slack_dashboard.config import WorkWindowConfig

logger = logging.getLogger(__name__)


def business_hours_between(start_ts: float, end_ts: float, work: WorkWindowConfig) -> float:
    """Fractional working hours in ``[start_ts, end_ts]``.

    Counts only the daily ``[start_hour, end_hour)`` window on ``work_days``, in
    ``work.timezone``. Nights and weekends contribute zero. Pure and DST-correct via
    ``zoneinfo``.

    DST discipline: we iterate LOCAL calendar dates in ``work.timezone``, build each day's
    ``[start_hour, end_hour)`` window as aware local datetimes, intersect it with the span,
    and convert each interval's endpoints to epoch seconds BEFORE subtracting. We never
    subtract two aware datetimes across a 23/25-hour DST day, so the fold/gap on a DST day
    contributes its true wall-clock duration. The 6am-6pm default window avoids the 2am
    ambiguous instant, so cross-day spans are the only DST risk and they are handled here.

    Args:
        start_ts: span start, float epoch seconds (UTC).
        end_ts: span end, float epoch seconds (UTC).
        work: the working-hours band (timezone, start/end hour, work days).

    Returns:
        Fractional working hours in the span, always ``>= 0.0``. Returns ``0.0`` when
        ``end_ts <= start_ts`` (clock skew, or a reply timestamped after ``now``), never
        negative.
    """
    logger.debug(
        "business_hours_between: start_ts=%s end_ts=%s timezone=%s "
        "start_hour=%s end_hour=%s work_days=%s",
        start_ts,
        end_ts,
        work.timezone,
        work.start_hour,
        work.end_hour,
        work.work_days,
    )
    if end_ts <= start_ts:
        logger.debug("business_hours_between: end_ts <= start_ts, returning 0.0")
        return 0.0

    tz = work.tzinfo()
    workdays = work.work_weekdays()
    start_dt = datetime.fromtimestamp(start_ts, tz=UTC).astimezone(tz)
    end_dt = datetime.fromtimestamp(end_ts, tz=UTC).astimezone(tz)

    total_seconds = 0.0
    day = start_dt.date()
    last_day = end_dt.date()
    while day <= last_day:
        if day.weekday() in workdays:
            # Build the day's local working window as aware datetimes in work.timezone.
            window_open = datetime.combine(day, time(hour=work.start_hour), tzinfo=tz)
            window_close = datetime.combine(day, time(hour=work.end_hour), tzinfo=tz)
            # Intersect [window_open, window_close) with [start_dt, end_dt].
            lo = max(window_open, start_dt)
            hi = min(window_close, end_dt)
            if hi > lo:
                # Convert to epoch BEFORE subtracting so a DST fold/gap on this day counts
                # its true wall-clock duration, not a naive local-hour difference.
                total_seconds += hi.timestamp() - lo.timestamp()
        day += timedelta(days=1)

    hours = total_seconds / 3600.0
    logger.debug("business_hours_between: work_hours=%s", hours)
    return hours
