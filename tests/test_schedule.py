"""Scheduler: pure window logic plus the manager holding and releasing the queue."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

from idm.engine import DownloadManager, Settings, Status
from idm.engine import schedule as sched

from .test_engine import DIGEST, LocalServer, sha256_of


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


def _s(**kw) -> Settings:
    base = dict(schedule_enabled=True, schedule_start="23:00", schedule_stop="07:00")
    base.update(kw)
    return Settings(**base)


def test_window_disabled_is_always_open():
    s = Settings(schedule_enabled=False, schedule_start="23:00", schedule_stop="07:00")
    assert sched.is_open(s, datetime(2026, 11, 2, 12, 0))
    assert sched.describe(s, datetime(2026, 11, 2, 12, 0)) == ""


def test_window_same_day():
    s = _s(schedule_start="09:00", schedule_stop="17:00")
    assert not sched.is_open(s, datetime(2026, 11, 2, 8, 59))
    assert sched.is_open(s, datetime(2026, 11, 2, 9, 0))
    assert sched.is_open(s, datetime(2026, 11, 2, 16, 59))
    assert not sched.is_open(s, datetime(2026, 11, 2, 17, 0))


def test_window_crossing_midnight_and_weekdays():
    # Monday 2026-11-02; window 23:00 -> 07:00 on Mon only.
    s = _s(schedule_days=[0])
    assert not sched.is_open(s, datetime(2026, 11, 2, 22, 59))
    assert sched.is_open(s, datetime(2026, 11, 2, 23, 0))
    assert sched.is_open(s, datetime(2026, 11, 3, 6, 59))  # Tuesday morning belongs to Monday's window
    assert not sched.is_open(s, datetime(2026, 11, 3, 7, 0))
    assert not sched.is_open(s, datetime(2026, 11, 3, 23, 30))  # Tuesday night: not an enabled day


def test_equal_times_mean_whole_day_and_bad_times_fall_open():
    s = _s(schedule_start="00:00", schedule_stop="00:00", schedule_days=[5, 6])
    assert sched.is_open(s, datetime(2026, 11, 7, 12, 0))  # Saturday
    assert not sched.is_open(s, datetime(2026, 11, 4, 12, 0))  # Wednesday
    assert sched.is_open(_s(schedule_start="nope"), datetime(2026, 11, 4, 12, 0))


def test_next_change_and_describe():
    s = _s(schedule_start="23:00", schedule_stop="07:00")
    now = datetime(2026, 11, 2, 12, 0)
    assert sched.next_change(s, now) == datetime(2026, 11, 2, 23, 0)
    assert sched.describe(s, now) == "starts at 23:00"
    night = datetime(2026, 11, 2, 23, 30)
    assert sched.next_change(s, night) == datetime(2026, 11, 3, 7, 0)
    assert sched.describe(s, night) == "stops at Tue 07:00"


def test_manager_holds_queue_until_window_opens(server: LocalServer, tmp_path: Path):
    settings = Settings(
        download_dir=str(tmp_path / "dl"),
        use_categories=False,
        schedule_enabled=True,
        schedule_start="23:00",
        schedule_stop="07:00",
    )
    fake = [datetime(2026, 11, 2, 12, 0).timestamp()]  # noon: window closed
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.clock = lambda: fake[0]
    m.schedule_poll = 0.05
    m.start()
    try:
        task = m.add(server.url("/file")).result(10)
        time.sleep(0.5)
        assert task.status == Status.QUEUED and task.downloaded == 0
        assert m.schedule_note() == "starts at 23:00"

        fake[0] = datetime(2026, 11, 2, 23, 1).timestamp()  # window opens
        done = m.wait(task.id, 60)
        assert done.status == Status.COMPLETED, done.error
        assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST
    finally:
        m.stop()


def test_manager_pauses_running_download_when_window_closes_and_requeues(server: LocalServer, tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False, connections_per_download=2)
    fake = [datetime(2026, 11, 2, 23, 30).timestamp()]
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.clock = lambda: fake[0]
    m.schedule_poll = 0.05
    m.start()
    try:
        server.delay = 0.03
        task = m.add(server.url("/file")).result(10)
        deadline = time.time() + 20
        while task.downloaded < 256 * 1024 and time.time() < deadline:
            time.sleep(0.05)
        assert task.status == Status.DOWNLOADING

        # Turn the scheduler on with a window that is closed right now.
        m.update_settings(
            Settings(
                download_dir=str(tmp_path / "dl"),
                use_categories=False,
                connections_per_download=2,
                schedule_enabled=True,
                schedule_start="23:00",
                schedule_stop="07:00",
            )
        ).result(10)
        fake[0] = datetime(2026, 11, 3, 7, 5).timestamp()  # past the stop time
        deadline = time.time() + 10
        while task.status == Status.DOWNLOADING and time.time() < deadline:
            time.sleep(0.05)
        assert task.status == Status.QUEUED  # held by the scheduler, not paused by the user
        held_at = task.downloaded
        time.sleep(0.3)
        assert task.downloaded == held_at

        server.delay = 0.0
        fake[0] = datetime(2026, 11, 3, 23, 0).timestamp()
        done = m.wait(task.id, 60)
        assert done.status == Status.COMPLETED, done.error
        assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST
    finally:
        m.stop()
