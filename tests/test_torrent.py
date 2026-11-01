"""BitTorrent support, tested with a second libtorrent session seeding on loopback."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from idm.engine import DownloadManager, Settings, Status, is_magnet, looks_like_torrent_file
from idm.engine.torrent import describe_torrent_file, magnet_name

lt = pytest.importorskip("libtorrent")

PAYLOAD = os.urandom(3 * 1024 * 1024 + 999)


def _quiet_session():
    from idm.engine.torrent import _free_port

    # A fixed, currently free port: with port 0 libtorrent's TCP and uTP sockets end up on different
    # numbers and the first (uTP) connection attempt from the downloader times out after 15 s.
    return lt.session(
        {
            "listen_interfaces": f"127.0.0.1:{_free_port()}",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "alert_mask": 0,
        }
    )


class Seeder:
    """Builds a single-file torrent from PAYLOAD and seeds it from a private session."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "seed"
        self.dir.mkdir(parents=True)
        (self.dir / "payload.bin").write_bytes(PAYLOAD)
        fs = lt.file_storage()
        lt.add_files(fs, str(self.dir / "payload.bin"))
        ct = lt.create_torrent(fs, piece_size=16 * 1024)
        lt.set_piece_hashes(ct, str(self.dir))
        self.torrent_bytes = lt.bencode(ct.generate())
        self.info = lt.torrent_info(lt.bdecode(self.torrent_bytes))
        self.session = _quiet_session()
        self.port = self.session.listen_port()
        params = lt.add_torrent_params()
        params.ti = self.info
        params.save_path = str(self.dir)
        params.flags |= lt.torrent_flags.seed_mode
        self.handle = self.session.add_torrent(params)
        deadline = time.monotonic() + 10
        while not self.handle.status().is_seeding and time.monotonic() < deadline:
            time.sleep(0.1)
        # The listen socket opens a moment after the session exists; a connection attempt that
        # arrives before that hangs until libtorrent's 15 s connect timeout.
        time.sleep(1.0)

    @property
    def magnet(self) -> str:
        return lt.make_magnet_uri(self.info) + f"&x.pe=127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.session.pause()


@pytest.fixture
def seeder(tmp_path: Path):
    s = Seeder(tmp_path)
    yield s
    s.stop()


@pytest.fixture
def manager(tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=True, torrent_port=0, torrent_trackers=[])
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    yield m
    m.stop()


def test_link_detection_helpers():
    assert is_magnet("magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Ubuntu")
    assert not is_magnet("https://example.com/magnet:?xt=urn:btih:abc")
    assert looks_like_torrent_file("https://x.org/file.torrent?dl=1")
    assert looks_like_torrent_file("blob", "application/x-bittorrent; charset=binary")
    assert not looks_like_torrent_file("https://x.org/file.zip", "application/zip")
    assert magnet_name("magnet:?xt=urn:btih:" + "a" * 40 + "&dn=My%20Show") == "My Show"
    assert magnet_name("magnet:?xt=urn:btih:" + "b" * 40) == "b" * 16


def test_describe_torrent_file_parses_locally(seeder):
    desc = describe_torrent_file(seeder.torrent_bytes)
    assert desc["name"] == "payload.bin"
    assert desc["size"] == len(PAYLOAD)
    assert desc["files"] == [("payload.bin", len(PAYLOAD))]
    with pytest.raises(Exception, match="not a valid"):
        describe_torrent_file(b"definitely not bencoded")


def test_magnet_download_completes_with_piece_map(manager, seeder, tmp_path, isolated_app_data):
    task = manager.add_torrent(seeder.magnet).result(10)
    assert task.kind == "torrent"
    assert task.dest_dir == str(tmp_path / "dl" / "Torrents")  # category folder for torrents
    done = manager.wait(task.id, 90)
    assert done.status == Status.COMPLETED, done.error
    assert done.filename == "payload.bin" and done.size == len(PAYLOAD)
    assert (tmp_path / "dl" / "Torrents" / "payload.bin").read_bytes() == PAYLOAD
    assert done.progress == 1.0
    # fast-resume data was written so a restart does not re-hash
    assert (isolated_app_data / "torrents" / f"{task.id}.fastresume").exists()


def test_torrent_file_bytes_and_add_smart_routing(manager, seeder, tmp_path, isolated_app_data):
    tfile = tmp_path / "payload.torrent"
    tfile.write_bytes(seeder.torrent_bytes)
    task = manager.add_torrent(str(tfile), start=False).result(10)
    assert task.status == Status.PAUSED
    assert task.filename == "payload.bin" and task.size == len(PAYLOAD)
    assert (isolated_app_data / "torrents" / f"{task.id}.torrent").exists()

    desc = manager.describe_torrent(seeder.magnet).result(10)
    assert desc["magnet"] is True and desc["name"] == "payload.bin"

    smart = manager.add_smart(seeder.magnet, start=False).result(10)
    assert smart.kind == "torrent"


def test_pause_resume_and_remove_with_files(manager, seeder, tmp_path, isolated_app_data):
    task = manager.add_torrent(seeder.magnet, start=False).result(10)
    assert task.status == Status.PAUSED
    manager.start_task(task.id).result(5)
    done = manager.wait(task.id, 90)
    assert done.status == Status.COMPLETED, done.error
    target = tmp_path / "dl" / "Torrents" / "payload.bin"
    assert target.exists()
    manager.remove_task(task.id, delete_file=True).result(20)
    assert task.id not in manager.tasks
    assert not target.exists()
    assert not (isolated_app_data / "torrents" / f"{task.id}.fastresume").exists()


def test_seeding_keeps_handle_until_removed(tmp_path, seeder):
    settings = Settings(
        download_dir=str(tmp_path / "dl"), use_categories=False, torrent_port=0, torrent_trackers=[], torrent_seed=True
    )
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    try:
        task = m.add_torrent(seeder.magnet).result(10)
        done = m.wait(task.id, 90)
        assert done.status == Status.COMPLETED, done.error
        assert m.torrent_seeding(task.id)
        m.remove_task(task.id).result(20)
        assert not m.torrent_seeding(task.id)
    finally:
        m.stop()
