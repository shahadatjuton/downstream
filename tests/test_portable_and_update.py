"""Portable mode data folder resolution, and the in-app updater helpers plus its download-then-run flow."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from idm.engine import DownloadManager, Settings
from idm.engine import settings as settings_mod
from idm.gui import dialogs

from .test_engine import LocalServer

# ------------------------------------------------------------------ portable mode


def test_data_dir_precedence(tmp_path: Path, monkeypatch):
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "IDM Clone.exe"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("IDM_CLONE_DATA_DIR", raising=False)

    # Installed: %APPDATA%\idm-clone
    assert settings_mod.portable_root() is None
    assert settings_mod.resolve_app_data_dir() == tmp_path / "roaming" / "idm-clone"

    # Portable marker next to the exe wins over APPDATA.
    (exe_dir / "portable.txt").write_text("", "utf-8")
    assert settings_mod.portable_root() == exe_dir
    assert settings_mod.resolve_app_data_dir() == exe_dir / "data"

    # Explicit override wins over everything.
    monkeypatch.setenv("IDM_CLONE_DATA_DIR", str(tmp_path / "custom"))
    assert settings_mod.resolve_app_data_dir() == tmp_path / "custom"


def test_source_checkout_is_not_portable_by_default(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    root = settings_mod.install_root()
    assert (root / "pyproject.toml").exists()  # points at the checkout when not frozen
    if not (root / "portable.txt").exists():
        assert settings_mod.portable_root() is None


# ------------------------------------------------------------------ updater helpers


def test_pick_installer_asset():
    assets = [
        {"name": "IDM Clone.exe", "browser_download_url": "https://x/portable.exe"},
        {"name": "idm-clone-extension.zip", "browser_download_url": "https://x/ext.zip"},
        {"name": "IDM-Clone-Setup-0.5.0.exe", "browser_download_url": "https://x/setup.exe"},
    ]
    assert dialogs.pick_installer_asset(assets) == "https://x/setup.exe"
    assert dialogs.pick_installer_asset(assets[:2]) is None
    assert dialogs.pick_installer_asset(None) is None


def test_is_installed_build(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Programs" / "IDM Clone" / "IDM Clone.exe"))
    assert dialogs.is_installed_build() is True
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Downloads" / "IDM Clone.exe"))
    assert dialogs.is_installed_build() is False
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert dialogs.is_installed_build() is False


def test_installer_command_is_silent_and_restarts():
    cmd = dialogs.installer_command(Path("C:/x/IDM-Clone-Setup-1.exe"))
    assert cmd[0].endswith("IDM-Clone-Setup-1.exe")
    assert "/SILENT" in cmd and "/CLOSEAPPLICATIONS" in cmd and "/RESTARTAPPLICATIONS" in cmd


# ------------------------------------------------------------------ GUI flow


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


def test_begin_app_update_downloads_then_runs_installer_and_quits(server, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from idm.gui import main_window as mw
    from idm.gui.theme import apply_theme

    from .test_gui import wait_until

    app = QApplication.instance() or QApplication([])
    settings = Settings(download_dir=str(tmp_path / "dl"), first_run_done=True, check_updates=False)
    mgr = DownloadManager(settings, state_path=tmp_path / "state.json")
    mgr.start()
    theme = apply_theme(app)
    win = mw.MainWindow(mgr, theme)
    launched: list[list[str]] = []
    quit_called: list[bool] = []
    monkeypatch.setattr(mw.subprocess, "Popen", lambda cmd, **kw: launched.append(list(cmd)))
    monkeypatch.setattr(win, "_quit", lambda: quit_called.append(True))
    try:
        win.begin_app_update(server.url("/file"), "9.9.9")
        wait_until(app, lambda: bool(launched), 60, "installer launch")
        cmd = launched[0]
        assert cmd[0].endswith("IDM-Clone-Setup-9.9.9.exe")
        assert Path(cmd[0]).exists()
        assert "updates" in Path(cmd[0]).parts
        assert "/SILENT" in cmd
        assert quit_called == [True]
    finally:
        win._timer.stop()
        mgr.on_event = None
        mgr.stop()
        win.close()
