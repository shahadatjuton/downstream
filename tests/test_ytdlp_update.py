"""yt-dlp self-update: version parsing, archive validation, activation in a fresh interpreter, and
the manager's check/download flow against the local server."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from idm.engine import DownloadManager, Settings
from idm.engine import ytdlp_update as upd

from .test_engine import LocalServer

ROOT = Path(__file__).resolve().parents[1]


def fake_zip(version: str, broken: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        init = "raise ImportError('broken on purpose')\n" if broken else "from .version import __version__\n"
        zf.writestr("yt_dlp/__init__.py", init)
        zf.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
        zf.writestr("__main__.py", "")
    return buf.getvalue()


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


def test_version_parsing_and_comparison():
    assert upd.parse_version("2026.10.26") == (2026, 10, 26)
    assert upd.is_newer("2026.10.26.1", "2026.10.26")
    assert upd.is_newer("2027.01.01", "2026.12.31")
    assert not upd.is_newer("2026.10.26", "2026.10.26")
    assert not upd.is_newer("", "2026.10.26")


def test_validate_zip(tmp_path: Path):
    good = tmp_path / "good.zip"
    good.write_bytes(fake_zip("2099.1.1"))
    assert upd.validate_zip(good) == "2099.1.1"
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        upd.validate_zip(bad)
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(ValueError):
        upd.validate_zip(empty)


def _run_activation(tmp_path: Path, archive: bytes) -> list[str]:
    """Activate a managed archive in a fresh interpreter so the test process keeps its real yt-dlp."""
    appdata = tmp_path / "appdata"
    managed = appdata / "idm-clone" / "yt-dlp"
    managed.mkdir(parents=True)
    (managed / upd.ZIP_NAME).write_bytes(archive)
    code = (
        "from idm.engine import ytdlp_update as u\n"
        "print(u.activate_managed())\n"
        "import yt_dlp.version as v\n"
        "print(v.__version__)\n"
        "print(u.managed_zip().exists())\n"
    )
    env = dict(os.environ, APPDATA=str(appdata), PYTHONPATH=str(ROOT / "src"))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_activate_managed_prefers_downloaded_copy(tmp_path: Path):
    assert _run_activation(tmp_path, fake_zip("2099.1.1")) == ["2099.1.1", "2099.1.1", "True"]


def test_activate_managed_falls_back_when_archive_is_broken(tmp_path: Path):
    import yt_dlp.version

    activated, version, still_there = _run_activation(tmp_path, fake_zip("2099.1.1", broken=True))
    assert activated == "None"
    assert version == yt_dlp.version.__version__  # bundled copy stays in charge
    assert still_there == "False"  # the unusable archive was removed


def test_manager_checks_and_downloads_update(server: LocalServer, tmp_path: Path, monkeypatch):
    import yt_dlp.version

    monkeypatch.setattr(upd, "LATEST_URL", server.url("/ytdlp/latest"))
    monkeypatch.setattr(upd, "DOWNLOAD_URL", server.url("/ytdlp/dl"))
    server.ytdlp_tag = "2099.1.1"
    server.ytdlp_zip = fake_zip("2099.1.1")

    events: list[tuple[str, str]] = []
    m = DownloadManager(Settings(download_dir=str(tmp_path / "dl")), state_path=tmp_path / "state.json")
    m.on_event = lambda name, task: events.append((name, task.filename))
    m.start()
    try:
        assert m.ytdlp_version()[0] == yt_dlp.version.__version__
        got = m.update_ytdlp(activate=False).result(30)
        assert got == "2099.1.1"
        assert upd.managed_zip().exists()
        assert upd.validate_zip(upd.managed_zip()) == "2099.1.1"
        assert ("ytdlp-updated", "2099.1.1") in events

        # Already current: nothing is downloaded again.
        server.ytdlp_tag = yt_dlp.version.__version__
        upd.managed_zip().unlink()
        assert m.update_ytdlp(activate=False).result(30) is None
        assert not upd.managed_zip().exists()
        assert events[-1][0] == "ytdlp-current"

        # force=True downloads even when current.
        assert m.update_ytdlp(force=True, activate=False).result(30) == "2099.1.1"
    finally:
        m.stop()
