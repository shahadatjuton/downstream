"""Per-download speed limits and changing the connection count while a download runs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from idm.engine import DownloadManager, DownloadTask, Settings, Status

from .test_engine import DIGEST, FILE_SIZE, TestServer, sha256_of


@pytest.fixture
def server():
    s = TestServer().start()
    yield s
    s.stop()


@pytest.fixture
def manager(tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=4, use_categories=False)
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    yield m
    m.stop()


def test_task_speed_limit_is_persisted_and_overrides_global():
    t = DownloadTask(url="u", filename="f", dest_dir="d", speed_limit=4096)
    again = DownloadTask.from_dict(t.to_dict())
    assert again.speed_limit == 4096
    legacy = DownloadTask.from_dict({"url": "u", "filename": "f", "dest_dir": "d"})
    assert legacy.speed_limit == 0


def test_per_task_limit_slows_a_download(server, manager, tmp_path):
    limit = 2 * 1024 * 1024  # 2 MB/s on a ~6.3 MB file: at least ~3 s instead of a loopback blink
    task = manager.add(server.url("/file"), start=False).result(10)
    manager.set_task_limit(task.id, limit).result(5)
    t0 = time.monotonic()
    manager.start_task(task.id).result(5)
    done = manager.wait(task.id, 60)
    elapsed = time.monotonic() - t0
    assert done.status == Status.COMPLETED, done.error
    assert elapsed >= FILE_SIZE / limit * 0.8, f"finished in {elapsed:.2f}s despite the limit"
    assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST


def test_raising_connections_mid_download_adds_segments(server, manager, tmp_path):
    server.delay = 0.02
    task = manager.add(server.url("/file"), connections=1).result(10)
    deadline = time.time() + 20
    while task.downloaded < 128 * 1024 and time.time() < deadline:
        time.sleep(0.05)
    assert len(task.segments) == 1  # a single connection never splits
    manager.set_task_connections(task.id, 4).result(5)
    deadline = time.time() + 20
    while len(task.segments) < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert len(task.segments) >= 3, "new workers should have split the remaining range"
    server.delay = 0.0
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert done.connections == 4
    assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST


def test_lowering_connections_mid_download_still_completes(server, manager, tmp_path):
    server.delay = 0.02
    task = manager.add(server.url("/file"), connections=6).result(10)
    deadline = time.time() + 20
    while task.downloaded < 128 * 1024 and time.time() < deadline:
        time.sleep(0.05)
    manager.set_task_connections(task.id, 1).result(5)
    server.delay = 0.0
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST
