"""Segmented, resumable download of a single file."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp

CHUNK_SIZE = 64 * 1024
MIN_SPLIT = 1024 * 1024  # a segment is only split if both halves would be >= this
SPEED_WINDOW = 3.0  # seconds
UNKNOWN = -1


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
    connections: int = 8
    segments: list[Segment] = field(default_factory=list)
    status: Status = Status.QUEUED
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    # "file" = plain HTTP download; "media" = page video/audio fetched through yt-dlp
    kind: str = "file"
    media_format: str = ""
    media_client: str = ""  # yt-dlp player client the chosen format came from (e.g. "android")
    title: str = ""
    referrer: str = ""

    # Runtime-only field (not persisted)
    speed: float = field(default=0.0, compare=False)

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
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d.pop("speed", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DownloadTask:
        d = dict(d)
        d["segments"] = [Segment(**s) for s in d.get("segments", [])]
        d["status"] = Status(d.get("status", "queued"))
        d.pop("speed", None)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        task = cls(**known)
        if task.status == Status.DOWNLOADING:
            # We were killed mid-download; treat as paused.
            task.status = Status.PAUSED
        return task


class DownloadError(Exception):
    pass


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
    ) -> None:
        self.task = task
        self.session = session
        self.max_retries = max_retries
        self.speed_limit = speed_limit
        self.proxy = proxy or None
        self._stop = asyncio.Event()
        self._claimed: set[int] = set()  # ids of Segment objects in progress
        self._samples: deque[tuple[float, int]] = deque()
        self._limit_t0 = time.monotonic()
        self._limit_bytes = 0
        self._error: str | None = None
        self._lock = asyncio.Lock()

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
            n_workers = max(1, min(task.connections, 32)) if task.resumable else 1
            workers = [asyncio.create_task(self._worker(i)) for i in range(n_workers)]
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            self._stop.set()
            task.status = Status.PAUSED
            raise
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"

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
            try:
                self._finalize()
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
                            self._stop.set()
                            break
                        await asyncio.sleep(min(30, 2**attempt))
                self._claimed.discard(id(seg))

    async def _download_segment(self, seg: Segment, fh) -> None:
        task = self.task
        headers = dict(task.headers)
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

    async def _throttle(self) -> None:
        if self.speed_limit <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._limit_t0
        expected = self._limit_bytes / self.speed_limit
        if expected > elapsed:
            await asyncio.sleep(expected - elapsed)
        if elapsed > 2.0:
            self._limit_t0 = now
            self._limit_bytes = 0
