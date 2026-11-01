"""History store: round trip, pruning of old finished entries, and the manager's history limit."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from idm.engine import DownloadManager, DownloadTask, Settings, Status
from idm.engine.history import HistoryStore, prune_completed

from .test_engine import LocalServer


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


def _task(name: str, status: Status, completed_at: float | None = None) -> DownloadTask:
    return DownloadTask(url=f"https://x/{name}", filename=name, dest_dir="d", status=status, completed_at=completed_at)


def test_store_round_trip_and_interrupted_ids(tmp_path: Path):
    store = HistoryStore(tmp_path / "h.json")
    running = _task("a", Status.DOWNLOADING)
    queued = _task("b", Status.QUEUED)
    done = _task("c", Status.COMPLETED, time.time())
    done.headers = {"Cookie": "k=v", "Referer": "r"}
    store.save([running, queued, done])
    tasks, interrupted = store.load()
    assert [t.filename for t in tasks] == ["a", "b", "c"]
    assert set(interrupted) == {running.id, queued.id}
    assert tasks[0].status == Status.PAUSED  # a download killed mid-flight comes back paused
    assert tasks[2].headers == {"Cookie": "k=v", "Referer": "r"}
    assert "k=v" not in (tmp_path / "h.json").read_text("utf-8")


def test_store_tolerates_missing_or_corrupt_file(tmp_path: Path):
    assert HistoryStore(tmp_path / "missing.json").load() == ([], [])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", "utf-8")
    assert HistoryStore(bad).load() == ([], [])
    half = tmp_path / "half.json"
    half.write_text('{"tasks": [{"url": "u", "filename": "ok", "dest_dir": "d"}, {"nonsense": 1}]}', "utf-8")
    tasks, _ = HistoryStore(half).load()
    assert [t.filename for t in tasks] == ["ok"]


def test_prune_completed_drops_oldest_finished_only():
    tasks = {}
    for i in range(5):
        t = _task(f"done{i}", Status.COMPLETED, completed_at=1000.0 + i)
        tasks[t.id] = t
    active = _task("active", Status.DOWNLOADING)
    paused = _task("paused", Status.PAUSED)
    tasks[active.id] = active
    tasks[paused.id] = paused

    assert prune_completed(tasks, 0) == []  # 0 = unlimited
    assert prune_completed(tasks, 10) == []
    removed = prune_completed(tasks, 2)
    assert [t.filename for t in removed] == ["done0", "done1", "done2"]
    assert {t.filename for t in tasks.values()} == {"done3", "done4", "active", "paused"}


def test_manager_enforces_history_limit(server, tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False, history_limit=2)
    removed: list[str] = []
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.on_event = lambda name, task: removed.append(task.filename) if name == "removed" else None
    m.start()
    try:
        for i in range(4):
            t = m.add(server.url("/file"), filename=f"f{i}.bin").result(10)
            assert m.wait(t.id, 60).status == Status.COMPLETED
        names = sorted(t.filename for t in m.snapshot())
        assert names == ["f2.bin", "f3.bin"]
        assert removed == ["f0.bin", "f1.bin"]
        for i in range(4):
            assert (tmp_path / "dl" / f"f{i}.bin").exists()  # pruning never deletes files
    finally:
        m.stop()

    # Lowering the limit applies on the next start as well.
    m2 = DownloadManager(
        Settings(download_dir=str(tmp_path / "dl"), history_limit=1), state_path=tmp_path / "state.json"
    )
    m2.start()
    try:
        assert [t.filename for t in m2.snapshot()] == ["f3.bin"]
    finally:
        m2.stop()
