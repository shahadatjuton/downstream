"""Segmented, resumable download of a single file."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger("idm.download")

CHUNK_SIZE = 64 * 1024
MIN_SPLIT = 1024 * 1024  # a segment is only split if both halves would be >= this
SPEED_WINDOW = 3.0  # seconds
BURST_CREDIT = 0.25  # seconds of unused limit a throttled download may catch up on
UNKNOWN = -1


CHECKSUM_ALGOS = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def parse_checksum(text: str) -> tuple[str, str] | None:
    """'sha256:abc…', 'ABC…' (algorithm from the length) or 'abc…  file.iso' (sha256sum line) ->
    (algorithm, lowercase hex). None when empty; ValueError when it is not a known digest."""
    raw = (text or "").strip()
    if not raw:
        return None
    algo = ""
    if ":" in raw:
        algo, _, raw = raw.partition(":")
        algo = algo.strip().lower().replace("-", "")
    digest = raw.split()[0].lower() if raw.split() else ""
    if not digest or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("a checksum is hexadecimal (MD5, SHA-1, SHA-256 or SHA-512)")
    inferred = CHECKSUM_ALGOS.get(len(digest))
    if not algo:
        algo = inferred or ""
    if algo not in CHECKSUM_ALGOS.values() or (inferred and inferred != algo):
        raise ValueError(f"{len(digest)} hex digits do not match a known digest")
    return algo, digest


def hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Status(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class Segment:
    start: int
    end: int  # inclusive; UNKNOWN when size is not known
    downloaded: int = 0

    @property
    def position(self) -> int:
        return self.start + self.downloaded

    @property
    def remaining(self) -> int:
        if self.end == UNKNOWN:
            return UNKNOWN
        return self.end - self.start + 1 - self.downloaded

    @property
    def done(self) -> bool:
        return self.end != UNKNOWN and self.remaining <= 0


@dataclass
class DownloadTask:
    url: str
    filename: str
    dest_dir: str
    size: int | None = None
    resumable: bool = False
    connections: int = 8  # may be changed while downloading; the runner adds/retires workers
    speed_limit: int = 0  # bytes/s for this download only; 0 = use the global setting
    segments: list[Segment] = field(default_factory=list)
    status: Status = Status.QUEUED
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    # "file" = plain HTTP download; "media" = page video/audio fetched through yt-dlp;
    # "mega" = MEGA.nz file (segmented download, decrypted on the fly); "torrent" = BitTorrent
    kind: str = "file"
    media_format: str = ""
    media_client: str = ""  # yt-dlp player client the chosen format came from (e.g. "android")
    media_postprocess: str = ""  # e.g. "mp3": convert the audio track with ffmpeg after download
    title: str = ""
    referrer: str = ""
    origin: str = ""  # the link the user gave (MEGA link, magnet, .torrent) when `url` is derived from it
    crypto_key: str = ""  # MEGA: base64 file key (the download is decrypted while it streams in)
    thumbnail: str = ""  # media: thumbnail URL, so it can be saved later from the context menu
    checksum: str = ""  # 'sha256:<hex>' expected digest; verified before the .part is renamed
    checksum_result: str = ""  # '' | 'ok' | 'mismatch' (last verification)

    # Runtime-only fields (not persisted)
    speed: float = field(default=0.0, compare=False)
    upload_speed: float = field(default=0.0, compare=False)
    peers: int = field(default=0, compare=False)
    seeds: int = field(default=0, compare=False)
    note: str = field(default="", compare=False)  # short live status ("fetching metadata", "seeding", ...)

    # ---- derived ----
    @property
    def final_path(self) -> Path:
        return Path(self.dest_dir) / self.filename

    @property
    def part_path(self) -> Path:
        return Path(self.dest_dir) / (self.filename + ".part")

    @property
    def downloaded(self) -> int:
        return sum(s.downloaded for s in self.segments)

    @property
    def progress(self) -> float | None:
        if self.status == Status.COMPLETED:
            return 1.0
        if not self.size:
            return None
        return min(1.0, self.downloaded / self.size)

    @property
    def eta(self) -> float | None:
        if self.status != Status.DOWNLOADING or not self.size or self.speed <= 0:
            return None
        return max(0.0, (self.size - self.downloaded) / self.speed)

    # ---- persistence ----
    RUNTIME_FIELDS = ("speed", "upload_speed", "peers", "seeds", "note")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        for k in self.RUNTIME_FIELDS:
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DownloadTask:
        d = dict(d)
        d["segments"] = [Segment(**s) for s in d.get("segments", [])]
        d["status"] = Status(d.get("status", "queued"))
        for k in cls.RUNTIME_FIELDS:
            d.pop(k, None)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        task = cls(**known)
        if task.status == Status.DOWNLOADING:
            # We were killed mid-download; treat as paused.
            task.status = Status.PAUSED
        return task


class DownloadError(Exception):
    pass


class DuplicateError(Exception):
    """The same link is already in the list. `task` is the existing entry."""

    def __init__(self, task: DownloadTask) -> None:
        state = "was already downloaded" if task.status == Status.COMPLETED else "is already in the list"
        super().__init__(f"{task.filename} {state}")
        self.task = task


class TaskRunner:
    """Drives one DownloadTask to completion (or pause/error) inside an asyncio loop."""

    def __init__(
        self,
        task: DownloadTask,
        session: aiohttp.ClientSession,
        *,
        max_retries: int = 5,
        speed_limit: int = 0,
        proxy: str | None = None,
        auth_header: str | None = None,
    ) -> None:
        self.task = task
        self.session = session
        self.max_retries = max_retries
        self.speed_limit = speed_limit
        self.proxy = proxy or None
        self.auth_header = auth_header  # "Basic …" from the credential store; never persisted with the task
        self._stop = asyncio.Event()
        self._claimed: set[int] = set()  # ids of Segment objects in progress
        self._samples: deque[tuple[float, int]] = deque()
        self._limit_t0 = time.monotonic()
        self._limit_bytes = 0
        self._error: str | None = None
        self._lock = asyncio.Lock()
        self._workers: set[asyncio.Task] = set()
        self._limit_seen = -1

    # ------------------------------------------------------------------ public
    def pause(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        task = self.task
        task.status = Status.DOWNLOADING
        task.error = None
        self._stop.clear()
        try:
            self._prepare_file()
            await self._supervise()
        except asyncio.CancelledError:
            self._stop.set()
            task.status = Status.PAUSED
            await self._cancel_workers()
            raise
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._stop.set()
            await self._cancel_workers()

        task.speed = 0.0
        if self._error:
            task.status = Status.ERROR
            task.error = self._error
            return
        if self._stop.is_set():
            task.status = Status.PAUSED
            if not task.resumable:
                task.segments = []  # cannot resume; start over next time
            return
        if self._all_done():
            if task.checksum and not await self._verify_checksum():
                return
            try:
                await self._finalize_async()
            except OSError as exc:
                task.status = Status.ERROR
                task.error = f"could not finalize file: {exc}"
                return
            task.status = Status.COMPLETED
            task.completed_at = time.time()
        else:
            task.status = Status.ERROR
            task.error = "download ended before all segments completed"

    # ---------------------------------------------------------------- helpers
    def _wanted_workers(self) -> int:
        return max(1, min(self.task.connections, 32)) if self.task.resumable else 1

    def _has_work(self) -> bool:
        """True while some segment is unclaimed or a claimed one is big enough to split."""
        for s in self.task.segments:
            if s.done:
                continue
            if id(s) not in self._claimed:
                return True
            if s.end != UNKNOWN and s.remaining >= 2 * MIN_SPLIT:
                return True
        return False

    async def _supervise(self) -> None:
        """Keep `task.connections` workers busy; the count can change while running."""
        index = 0
        while True:
            self._workers = {w for w in self._workers if not w.done()}
            while (
                len(self._workers) < self._wanted_workers()
                and not self._stop.is_set()
                and self._error is None
                and self._has_work()
            ):
                self._workers.add(asyncio.create_task(self._worker(index)))
                index += 1
            if not self._workers:
                return
            done, _ = await asyncio.wait(self._workers, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            for w in done:
                w.result()  # re-raise anything a worker leaked

    async def _cancel_workers(self) -> None:
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def _prepare_file(self) -> None:
        task = self.task
        Path(task.dest_dir).mkdir(parents=True, exist_ok=True)
        part = task.part_path

        if task.segments and task.resumable and part.exists():
            # Resume: trust persisted segment state.
            return

        # Fresh start.
        task.segments = []
        with open(part, "wb") as f:
            if task.size:
                f.truncate(task.size)
        if task.size and task.resumable:
            n = max(1, min(task.connections, 32))
            # Do not create segments smaller than MIN_SPLIT.
            n = max(1, min(n, task.size // MIN_SPLIT or 1))
            step = task.size // n
            for i in range(n):
                start = i * step
                end = task.size - 1 if i == n - 1 else start + step - 1
                task.segments.append(Segment(start, end))
        else:
            end = task.size - 1 if task.size else UNKNOWN
            task.segments.append(Segment(0, end))

    def _all_done(self) -> bool:
        if not self.task.size:
            # Unknown size: done if the single segment read to EOF without being stopped.
            return not self._stop.is_set() and self._error is None
        return all(s.done for s in self.task.segments)

    async def _verify_checksum(self) -> bool:
        """Hash the finished .part against task.checksum. On mismatch the task becomes ERROR and the
        .part is kept, so the user can inspect it or retry."""
        task = self.task
        try:
            parsed = parse_checksum(task.checksum)
        except ValueError as exc:
            task.status = Status.ERROR
            task.error = f"bad checksum: {exc}"
            return False
        if parsed is None:
            return True
        algo, expected = parsed
        loop = asyncio.get_running_loop()
        task.note = "verifying checksum"
        got = await loop.run_in_executor(None, hash_file, task.part_path, algo)
        if got != expected:
            task.checksum_result = "mismatch"
            task.status = Status.ERROR
            task.error = f"checksum mismatch: expected {algo} {expected[:16]}…, file has {got[:16]}…"
            task.note = ""
            log.warning("%r: %s", task.filename, task.error)
            return False
        task.checksum_result = "ok"
        task.note = "checksum verified"
        return True

    async def _finalize_async(self) -> None:
        """Hook for subclasses that need work between the last byte and the rename."""
        self._finalize()

    def _finalize(self) -> None:
        task = self.task
        part = task.part_path
        final = task.final_path
        if final.exists():
            stem, suffix = final.stem, final.suffix
            i = 1
            while final.exists():
                final = final.with_name(f"{stem} ({i}){suffix}")
                i += 1
        os.replace(part, final)
        task.filename = final.name
        if not task.size:
            task.size = final.stat().st_size

    def _claim(self) -> Segment | None:
        for seg in self.task.segments:
            if not seg.done and id(seg) not in self._claimed:
                self._claimed.add(id(seg))
                return seg
        return None

    def _split_busiest(self) -> Segment | None:
        """Split the in-progress segment with the most remaining bytes and claim the new half."""
        candidates = [s for s in self.task.segments if id(s) in self._claimed and s.end != UNKNOWN and not s.done]
        if not candidates:
            return None
        busiest = max(candidates, key=lambda s: s.remaining)
        if busiest.remaining < 2 * MIN_SPLIT:
            return None
        mid = busiest.position + busiest.remaining // 2
        new = Segment(mid, busiest.end)
        busiest.end = mid - 1
        self.task.segments.append(new)
        self._claimed.add(id(new))
        return new

    async def _worker(self, index: int) -> None:
        task = self.task
        with open(task.part_path, "r+b") as fh:
            while not self._stop.is_set() and self._error is None:
                if len([w for w in self._workers if not w.done()]) > self._wanted_workers():
                    return  # connections were lowered; let this worker retire
                async with self._lock:
                    seg = self._claim() or self._split_busiest()
                if seg is None:
                    return
                attempt = 0
                while True:
                    try:
                        await self._download_segment(seg, fh)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        if self._stop.is_set():
                            break
                        attempt += 1
                        if attempt > self.max_retries or not task.resumable:
                            self._error = f"{type(exc).__name__}: {exc}"
                            log.warning("%r: giving up on bytes %d-%s: %s", task.filename, seg.start, seg.end, exc)
                            self._stop.set()
                            break
                        log.info(
                            "%r: bytes %d-%s failed (%s), retry %d/%d",
                            task.filename,
                            seg.position,
                            seg.end,
                            exc,
                            attempt,
                            self.max_retries,
                        )
                        await asyncio.sleep(min(30, 2**attempt))
                self._claimed.discard(id(seg))

    async def _download_segment(self, seg: Segment, fh) -> None:
        task = self.task
        headers = dict(task.headers)
        if self.auth_header and "Authorization" not in headers:
            headers["Authorization"] = self.auth_header
        if task.resumable:
            if seg.end == UNKNOWN:
                headers["Range"] = f"bytes={seg.position}-"
            else:
                headers["Range"] = f"bytes={seg.position}-{seg.end}"
            if task.etag:
                headers["If-Range"] = task.etag
            elif task.last_modified:
                headers["If-Range"] = task.last_modified

        async with self.session.get(task.url, headers=headers, proxy=self.proxy) as resp:
            if task.resumable and resp.status != 206:
                whole_file_ok = resp.status == 200 and seg.position == 0 and len(task.segments) == 1
                if not whole_file_ok:
                    raise DownloadError(f"server did not honour Range request (HTTP {resp.status})")
            resp.raise_for_status()

            transform = self._transform_at(seg.position)
            fh.seek(seg.position)
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                if self._stop.is_set():
                    resp.close()
                    return
                if seg.end != UNKNOWN:
                    room = seg.end - seg.position + 1
                    if room <= 0:
                        resp.close()
                        break
                    if len(chunk) > room:
                        chunk = chunk[:room]
                if transform is not None:
                    chunk = transform(chunk)
                fh.write(chunk)
                seg.downloaded += len(chunk)
                self._record(len(chunk))
                if seg.done:
                    resp.close()
                    break
                await self._throttle()
            fh.flush()

            if self._stop.is_set():
                return
            if seg.end == UNKNOWN:
                # Stream reached EOF: now we know the size, and the segment is complete.
                seg.end = seg.start + seg.downloaded - 1
                task.size = seg.end + 1
            elif not seg.done:
                raise DownloadError(f"connection closed early at byte {seg.position} (expected up to {seg.end})")

    def _transform_at(self, position: int) -> Callable[[bytes], bytes] | None:
        """Subclasses can return a function applied to every chunk written from `position` on
        (MEGA uses it to decrypt while downloading). None = write bytes as they arrive."""
        return None

    def _record(self, n: int) -> None:
        now = time.monotonic()
        self._samples.append((now, n))
        cutoff = now - SPEED_WINDOW
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        total = sum(b for _, b in self._samples)
        span = now - self._samples[0][0] if len(self._samples) > 1 else SPEED_WINDOW
        self.task.speed = total / max(span, 0.25)
        self._limit_bytes += n

    @property
    def effective_limit(self) -> int:
        return self.task.speed_limit or self.speed_limit

    async def _throttle(self) -> None:
        limit = self.effective_limit
        if limit != self._limit_seen:
            self._limit_seen = limit
            self._limit_t0 = time.monotonic()
            self._limit_bytes = 0
        if limit <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._limit_t0
        expected = self._limit_bytes / limit
        if expected > elapsed:
            await asyncio.sleep(expected - elapsed)
        elif elapsed - expected > BURST_CREDIT:
            # Idle time only buys a small burst, so a stall does not turn into a spike afterwards.
            self._limit_t0 = now - BURST_CREDIT
            self._limit_bytes = 0
