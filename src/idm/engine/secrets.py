"""Small secrets helpers: DPAPI-protected strings (Windows) and the per-host credential store.

On Windows the password is encrypted with CryptProtectData for the current user, so the file in
%APPDATA% is useless on another account or machine. Elsewhere it is only base64-obscured (there
is no portable user-bound key store); the prefix tells which form a value is in.
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .settings import app_data_dir

_DPAPI = "dpapi:"
_PLAIN = "b64:"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    import ctypes.wintypes as wt

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(text: str) -> str:
    raw = text.encode("utf-8")
    if sys.platform == "win32":
        try:
            return _DPAPI + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except OSError:
            pass
    return _PLAIN + base64.b64encode(raw).decode("ascii")


def unprotect(value: str) -> str:
    if value.startswith(_DPAPI):
        return _dpapi(base64.b64decode(value[len(_DPAPI) :]), False).decode("utf-8")
    if value.startswith(_PLAIN):
        return base64.b64decode(value[len(_PLAIN) :]).decode("utf-8")
    return value  # legacy / hand-edited plain text


@dataclass
class Credential:
    username: str
    password: str


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


class CredentialStore:
    """host -> Credential, persisted as JSON with protected passwords."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_data_dir() / "credentials.json")
        self._items: dict[str, Credential] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for host, item in data.items():
            try:
                self._items[host.lower()] = Credential(str(item["username"]), unprotect(str(item["password"])))
            except (KeyError, TypeError, ValueError, OSError):
                continue

    def _save(self) -> None:
        data = {h: {"username": c.username, "password": protect(c.password)} for h, c in self._items.items()}
        try:
            self.path.write_text(json.dumps(data, indent=2), "utf-8")
        except OSError:
            pass

    def get(self, url_or_host: str) -> Credential | None:
        host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
        return self._items.get(host)

    def set(self, url_or_host: str, username: str, password: str) -> None:
        host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
        if not host:
            return
        self._items[host] = Credential(username, password)
        self._save()

    def remove(self, url_or_host: str) -> None:
        host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
        if self._items.pop(host, None) is not None:
            self._save()

    def hosts(self) -> list[str]:
        return sorted(self._items)


def basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"
