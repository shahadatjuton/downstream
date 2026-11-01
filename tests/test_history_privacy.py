"""downloads.json must not expose browser cookies or Authorization headers in clear text, and the
engine must log what it does."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from idm.engine import DownloadManager, Settings, Status

from .test_engine import LocalServer


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


def test_sensitive_headers_are_protected_on_disk_and_restored(server, tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False)
    state = tmp_path / "state.json"
    headers = {"Cookie": "session=topsecret; theme=dark", "Referer": "https://example.com/page"}
    m1 = DownloadManager(settings, state_path=state)
    m1.start()
    try:
        task = m1.add(server.url("/file"), headers=headers, start=False).result(10)
    finally:
        m1.stop()

    raw = state.read_text("utf-8")
    assert "topsecret" not in raw
    assert "https://example.com/page" in raw  # only cookies / Authorization are protected
    stored = json.loads(raw)["tasks"][0]["headers"]["Cookie"]
    assert stored.startswith(("dpapi:", "b64:"))

    m2 = DownloadManager(settings, state_path=state)
    m2.start()
    try:
        again = m2.snapshot()[0]
        assert again.id == task.id
        assert again.headers["Cookie"] == headers["Cookie"]
        assert again.headers["Referer"] == headers["Referer"]
        # And the restored download still works with the restored headers.
        m2.start_task(again.id).result(5)
        assert m2.wait(again.id, 60).status == Status.COMPLETED
    finally:
        m2.stop()


def test_engine_logs_lifecycle(server, tmp_path: Path, caplog):
    caplog.set_level(logging.INFO, logger="idm.engine")
    m = DownloadManager(
        Settings(download_dir=str(tmp_path / "dl"), use_categories=False), state_path=tmp_path / "s.json"
    )
    m.start()
    try:
        task = m.add(server.url("/file")).result(10)
        assert m.wait(task.id, 60).status == Status.COMPLETED
    finally:
        m.stop()
    text = caplog.text
    assert "engine starting" in text
    assert "added file 'payload.bin'" in text
    assert "completed 'payload.bin'" in text
    assert "engine stopping" in text
