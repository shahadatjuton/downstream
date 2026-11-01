"""downloads.json: the persisted task list.

Cookies and Authorization headers are protected (DPAPI on Windows) before they reach disk and
restored on load. `prune_completed` keeps the finished part of the list from growing forever.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .download import DownloadTask, Status
from .secrets import protect, unprotect

log = logging.getLogger("idm.engine")

# Request headers whose values are protected before they reach downloads.json.
SENSITIVE_HEADERS = ("cookie", "authorization")


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[list[DownloadTask], list[str]]:
        """(tasks, ids of tasks that were running or queued when the app last stopped)."""
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return [], []
        tasks: list[DownloadTask] = []
        interrupted: list[str] = []
        for item in data.get("tasks", []):
            try:
                headers = item.get("headers") or {}
                if isinstance(headers, dict):
                    item["headers"] = {k: _unprotect_header(k, v) for k, v in headers.items()}
                task = DownloadTask.from_dict(item)
            except (TypeError, ValueError, OSError) as exc:
                log.warning("skipping unreadable history entry: %s", exc)
                continue
            if item.get("status") in (Status.DOWNLOADING.value, Status.QUEUED.value):
                interrupted.append(task.id)
            tasks.append(task)
        return tasks, interrupted

    def save(self, tasks: Iterable[DownloadTask]) -> None:
        data = {"tasks": [serialize(t) for t in tasks]}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1), "utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save download history: %s", exc)


def serialize(task: DownloadTask) -> dict[str, Any]:
    """to_dict() with cookies and Authorization values protected for the current user."""
    d = task.to_dict()
    if d.get("headers"):
        d["headers"] = {
            k: (protect(v) if k.lower() in SENSITIVE_HEADERS and isinstance(v, str) else v)
            for k, v in d["headers"].items()
        }
    return d


def _unprotect_header(name: str, value: Any) -> Any:
    if name.lower() in SENSITIVE_HEADERS and isinstance(value, str):
        return unprotect(value)
    return value


def prune_completed(tasks: dict[str, DownloadTask], limit: int) -> list[DownloadTask]:
    """Remove the oldest finished entries so at most `limit` remain (0 = keep everything).
    Files on disk are untouched. Returns the removed tasks so callers can announce them."""
    if limit <= 0:
        return []
    done = [t for t in tasks.values() if t.status == Status.COMPLETED]
    if len(done) <= limit:
        return []
    done.sort(key=lambda t: t.completed_at or t.created_at)
    removed = done[: len(done) - limit]
    for t in removed:
        tasks.pop(t.id, None)
    return removed
