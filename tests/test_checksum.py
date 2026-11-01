"""Checksum verification: parsing, verify-before-rename, and verifying a finished file later."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from idm.engine import DownloadManager, Settings, Status
from idm.engine.download import hash_file, parse_checksum

from .test_engine import DIGEST, PAYLOAD, LocalServer

MD5 = hashlib.md5(PAYLOAD).hexdigest()  # noqa: S324 (test fixture)


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


@pytest.fixture
def manager(tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=4, use_categories=False)
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    yield m
    m.stop()


def test_parse_checksum_forms():
    assert parse_checksum("") is None
    assert parse_checksum("  ") is None
    assert parse_checksum(DIGEST.upper()) == ("sha256", DIGEST)
    assert parse_checksum(f"sha256:{DIGEST}") == ("sha256", DIGEST)
    assert parse_checksum(f"SHA-256: {DIGEST}") == ("sha256", DIGEST)
    assert parse_checksum(f"{DIGEST}  payload.bin") == ("sha256", DIGEST)  # sha256sum output line
    assert parse_checksum(MD5) == ("md5", MD5)
    assert parse_checksum("a" * 40) == ("sha1", "a" * 40)
    assert parse_checksum("a" * 128) == ("sha512", "a" * 128)
    for bad in ("xyz", "12345", f"md5:{DIGEST}", "sha256:zz" + "0" * 62):
        with pytest.raises(ValueError):
            parse_checksum(bad)


def test_hash_file(tmp_path: Path):
    p = tmp_path / "x.bin"
    p.write_bytes(PAYLOAD)
    assert hash_file(p, "sha256") == DIGEST
    assert hash_file(p, "md5") == MD5


def test_matching_checksum_completes_and_is_recorded(server, manager, tmp_path):
    task = manager.add(server.url("/file"), checksum=f"{DIGEST}  payload.bin").result(10)
    assert task.checksum == f"sha256:{DIGEST}"
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert done.checksum_result == "ok"
    assert done.note == "checksum verified"
    assert (tmp_path / "dl" / "payload.bin").exists()


def test_mismatch_fails_and_keeps_part_file(server, manager, tmp_path):
    wrong = "0" * 64
    task = manager.add(server.url("/file"), checksum=wrong).result(10)
    done = manager.wait(task.id, 60)
    assert done.status == Status.ERROR
    assert done.checksum_result == "mismatch"
    assert "checksum mismatch" in (done.error or "")
    assert (tmp_path / "dl" / "payload.bin.part").exists()
    assert not (tmp_path / "dl" / "payload.bin").exists()


def test_md5_also_works(server, manager):
    task = manager.add(server.url("/file"), checksum=MD5).result(10)
    assert manager.wait(task.id, 60).checksum_result == "ok"


def test_invalid_checksum_is_rejected_before_adding(server, manager):
    with pytest.raises(ValueError):
        manager.add(server.url("/file"), checksum="not-hex")
    assert manager.snapshot() == []


def test_verify_finished_download_later(server, manager):
    events: list[str] = []
    manager.on_event = lambda name, task: events.append(name)
    task = manager.add(server.url("/file")).result(10)
    assert manager.wait(task.id, 60).status == Status.COMPLETED
    assert task.checksum == ""

    assert manager.verify_task(task.id, DIGEST).result(30) is True
    assert task.checksum_result == "ok" and task.checksum == f"sha256:{DIGEST}"
    assert "verified" in events

    assert manager.verify_task(task.id, "f" * 32).result(30) is False
    assert task.checksum_result == "mismatch"
    assert "mismatch" in task.note

    with pytest.raises(ValueError):
        manager.verify_task(task.id, "")
    unfinished = manager.add(server.url("/file"), start=False, filename="later.bin").result(10)
    with pytest.raises(ValueError):
        manager.verify_task(unfinished.id, DIGEST).result(10)
