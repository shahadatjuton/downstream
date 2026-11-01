"""HTTP authentication: 401 detection, Basic auth downloads, the encrypted credential store, and the
login row in the Add dialog."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from idm.engine import AuthRequiredError, DownloadManager, Settings, Status
from idm.engine import secrets as secrets_mod
from idm.engine.secrets import CredentialStore

from .test_engine import DIGEST, LocalServer, sha256_of


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


def test_protect_roundtrip_and_is_not_plaintext():
    token = secrets_mod.protect("p@ss wörd")
    assert "p@ss" not in token
    assert secrets_mod.unprotect(token) == "p@ss wörd"
    if sys.platform == "win32":
        assert token.startswith("dpapi:")


def test_credential_store_persists_and_matches_by_host(tmp_path: Path):
    store = CredentialStore(tmp_path / "cred.json")
    store.set("https://files.example.com:8443/a/b.zip", "alice", "secret")
    assert store.get("https://files.example.com/other").username == "alice"
    assert store.get("files.example.com").password == "secret"
    assert store.get("https://elsewhere.example.com/") is None
    raw = json.loads((tmp_path / "cred.json").read_text("utf-8"))
    assert "secret" not in json.dumps(raw)
    again = CredentialStore(tmp_path / "cred.json")
    assert again.get("files.example.com").password == "secret"
    again.remove("files.example.com")
    assert CredentialStore(tmp_path / "cred.json").hosts() == []


def test_probe_reports_401_then_succeeds_with_login(server, manager):
    with pytest.raises(AuthRequiredError) as info:
        manager.probe(server.url("/auth")).result(10)
    assert info.value.realm == "Members"
    pr = manager.probe(server.url("/auth"), username="alice", password="secret").result(10)
    assert pr.size and pr.resumable and pr.filename == "payload.bin"


def test_download_with_remembered_login_and_reuse(server, manager, tmp_path):
    task = manager.add(server.url("/auth"), username="alice", password="secret").result(10)
    assert "Authorization" not in task.headers  # saved logins live in the store, not in downloads.json
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST

    # Second download from the same host: no credentials given, the store supplies them.
    task2 = manager.add(server.url("/auth"), filename="again.bin").result(10)
    done2 = manager.wait(task2.id, 60)
    assert done2.status == Status.COMPLETED, done2.error
    assert sha256_of(tmp_path / "dl" / "again.bin") == DIGEST
    assert manager.credentials.hosts() == ["127.0.0.1"]

    # Forgetting it brings the 401 back.
    manager.forget_credential("127.0.0.1")
    with pytest.raises(AuthRequiredError):
        manager.probe(server.url("/auth")).result(10)


def test_one_off_login_is_not_stored(server, manager, tmp_path):
    task = manager.add(
        server.url("/auth"), username="alice", password="secret", remember_login=False, filename="once.bin"
    ).result(10)
    assert task.headers.get("Authorization", "").startswith("Basic ")
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert manager.credentials.hosts() == []


def test_add_dialog_shows_login_row_and_downloads(server, tmp_path):
    from PySide6.QtWidgets import QApplication

    from idm.gui.add_dialog import AddDownloadDialog
    from idm.gui.theme import apply_theme

    from .test_gui import wait_until

    app = QApplication.instance() or QApplication([])
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False, first_run_done=True)
    mgr = DownloadManager(settings, state_path=tmp_path / "state.json")
    mgr.start()
    try:
        theme = apply_theme(app)
        dlg = AddDownloadDialog(mgr, theme, server.url("/auth"))
        dlg.show()
        wait_until(app, lambda: dlg.needs_auth and dlg.auth_row.isVisible(), 10, "login row")
        assert not dlg.start_btn.isEnabled()
        dlg.user_edit.setText("alice")
        dlg.pass_edit.setText("secret")
        dlg._probe_with_login()
        wait_until(app, lambda: dlg.start_btn.isEnabled(), 10, "probe with login")
        assert dlg.auth_row.isVisible()  # the row stays so the user can see what was used
        futures = dlg.submit()
        task = futures[0].result(10)
        deadline = time.time() + 60
        while task.status not in (Status.COMPLETED, Status.ERROR) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.05)
        assert task.status == Status.COMPLETED, task.error
        assert mgr.credentials.get("127.0.0.1").username == "alice"
        dlg.close()
    finally:
        mgr.stop()
