"""Keep every test away from the real %APPDATA%\\idm-clone (settings, history, ipc.json, ffmpeg)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path: Path, monkeypatch):
    data = tmp_path / "appdata" / "idm-clone"
    data.mkdir(parents=True)
    import idm.engine.ipc as ipc_mod
    import idm.engine.media as media_mod
    import idm.engine.settings as settings_mod

    monkeypatch.setattr(settings_mod, "app_data_dir", lambda: data)
    monkeypatch.setattr(ipc_mod, "app_data_dir", lambda: data)
    monkeypatch.setattr(media_mod, "managed_ffmpeg_dir", lambda: data / "ffmpeg")
    monkeypatch.setenv("APPDATA", str(data.parent))
    return data
