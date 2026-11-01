"""Download scheduler: a daily time window (optionally per weekday) during which the queue runs.

Pure functions over Settings + a datetime so they are trivial to test; the manager asks
`is_open` before dispatching queued tasks and `next_change` to show "starts at 23:00".
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .settings import Settings

ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)  # Monday .. Sunday, like datetime.weekday()


def parse_hhmm(value: str) -> time | None:
    try:
        h, _, m = value.strip().partition(":")
        return time(int(h), int(m or 0))
    except ValueError:
        return None


def _days(settings: Settings) -> tuple[int, ...]:
    days = tuple(d for d in settings.schedule_days if 0 <= d <= 6)
    return days or ALL_DAYS


def window_bounds(settings: Settings) -> tuple[time, time] | None:
    start = parse_hhmm(settings.schedule_start)
    stop = parse_hhmm(settings.schedule_stop)
    if start is None or stop is None:
        return None
    return start, stop


def is_open(settings: Settings, now: datetime) -> bool:
    """True when queued downloads may run at `now`.

    The window starts at `schedule_start` on each enabled weekday and ends at `schedule_stop`;
    when stop <= start it runs past midnight (23:00 -> 07:00 means the following morning still
    belongs to the day the window started on). Equal start and stop means the whole day.
    """
    if not settings.schedule_enabled:
        return True
    bounds = window_bounds(settings)
    if bounds is None:
        return True
    start, stop = bounds
    days = _days(settings)
    t = now.time().replace(second=0, microsecond=0)
    if start == stop:
        return now.weekday() in days
    if start < stop:
        return now.weekday() in days and start <= t < stop
    # Crosses midnight.
    if t >= start:
        return now.weekday() in days
    if t < stop:
        yesterday = (now - timedelta(days=1)).weekday()
        return yesterday in days
    return False


def next_change(settings: Settings, now: datetime) -> datetime | None:
    """The next moment `is_open` flips, or None when the schedule is off / never changes."""
    if not settings.schedule_enabled or window_bounds(settings) is None:
        return None
    state = is_open(settings, now)
    probe = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # The window is defined to the minute, so a minute walk over one week is exact and cheap enough.
    for _ in range(7 * 24 * 60 + 1):
        if is_open(settings, probe) != state:
            return probe
        probe += timedelta(minutes=1)
    return None


def describe(settings: Settings, now: datetime) -> str:
    """Short status text for the UI: '' when running normally, otherwise e.g. 'starts at 23:00'."""
    if not settings.schedule_enabled:
        return ""
    change = next_change(settings, now)
    if change is None:
        return ""
    when = change.strftime("%H:%M") if change.date() == now.date() else change.strftime("%a %H:%M")
    return f"stops at {when}" if is_open(settings, now) else f"starts at {when}"
