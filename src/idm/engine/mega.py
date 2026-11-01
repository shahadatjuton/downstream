"""MEGA.nz downloads: public file links and folder links.

A MEGA link carries the decryption key in its fragment (`#`), which never reaches the server.
We ask MEGA's API for the file's size, encrypted name and a temporary download URL, then
fetch the ciphertext with the normal segmented engine and decrypt it chunk by chunk
(AES-128-CTR is seekable, so every connection decrypts its own byte range).
"""

from __future__ import annotations

import base64
import json
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from .download import Status, TaskRunner
from .probe import sanitize_filename

API_URL = "https://g.api.mega.co.nz/cs"

_FILE_RE = re.compile(r"mega(?:\.co)?\.nz/(?:file/|#!)([\w-]{8})(?:[#!]([\w-]{43}))?", re.I)
_FOLDER_RE = re.compile(
    r"mega(?:\.co)?\.nz/(?:folder/|#F!)([\w-]{8})(?:[#!]([\w-]{22}))?(?:/(?:file|folder)/([\w-]{8}))?", re.I
)

_ERRORS = {
    -1: "internal MEGA error, try again later",
    -2: "invalid link",
    -3: "MEGA asked to retry (rate limited)",
    -4: "too many requests, try again later",
    -6: "too many concurrent connections",
    -9: "file not found (removed or the link is wrong)",
    -11: "access denied",
    -16: "file blocked by MEGA (terms of service)",
    -17: "MEGA transfer quota exceeded for your IP; wait a few hours or use a MEGA account",
    -18: "file temporarily unavailable",
}


class MegaError(Exception):
    pass


@dataclass
class MegaFile:
    name: str
    size: int
    key: bytes  # 32 bytes: file key + iv + meta-mac, as MEGA stores it
    handle: str  # file (node) handle
    folder: str = ""  # containing public folder handle, "" for a plain file link
    folder_key: bytes = b""  # the folder link's key (needed to re-open the folder later)
    url: str = ""  # temporary download URL (expires after a few hours)
    path: str = ""  # relative folder path inside a folder link ("" at the root)

    @property
    def link(self) -> str:
        """A link that identifies this file again later (used to refresh the download URL)."""
        if self.folder:
            return f"https://mega.nz/folder/{self.folder}#{b64encode(self.folder_key)}/file/{self.handle}"
        return f"https://mega.nz/file/{self.handle}#{b64encode(self.key)}"


# ------------------------------------------------------------------ helpers
def is_mega_link(text: str) -> bool:
    text = text.strip()
    return bool(_FILE_RE.search(text) or _FOLDER_RE.search(text))


def b64decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/").rstrip("=")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def b64encode(b: bytes) -> str:
    return base64.b64encode(b).decode().replace("+", "-").replace("/", "_").rstrip("=")


def unpack_file_key(key32: bytes) -> tuple[bytes, bytes]:
    """MEGA's 256-bit file key -> (16-byte AES key, 8-byte CTR nonce)."""
    if len(key32) != 32:
        raise MegaError("bad file key length")
    k = struct.unpack(">8I", key32)
    aes_key = struct.pack(">4I", k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
    nonce = key32[16:24]
    return aes_key, nonce


def encrypt_attributes(attrs: dict[str, Any], aes_key: bytes) -> str:
    """Inverse of decrypt_attributes (used by tests and kept here so the two stay in sync)."""
    from Crypto.Cipher import AES

    raw = b"MEGA" + json.dumps(attrs, separators=(",", ":")).encode("utf-8")
    raw += b"\0" * (-len(raw) % 16)
    return b64encode(AES.new(aes_key, AES.MODE_CBC, iv=b"\0" * 16).encrypt(raw))


def decrypt_attributes(attr_b64: str, aes_key: bytes) -> dict[str, Any]:
    """Node attributes: base64(AES-CBC(zero IV, "MEGA" + json))."""
    from Crypto.Cipher import AES

    data = b64decode(attr_b64)
    data = data[: len(data) - len(data) % 16]
    plain = AES.new(aes_key, AES.MODE_CBC, iv=b"\0" * 16).decrypt(data) if data else b""
    if not plain.startswith(b"MEGA"):
        raise MegaError("could not decrypt the file name (wrong key in the link?)")
    text = plain[4:].rstrip(b"\0").decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise MegaError("could not decrypt the file name (wrong key in the link?)") from exc


def ctr_decryptor(key32: bytes, position: int) -> Callable[[bytes], bytes]:
    """Return a function that decrypts consecutive ciphertext chunks starting at byte `position`."""
    from Crypto.Cipher import AES

    aes_key, nonce = unpack_file_key(key32)
    cipher = AES.new(aes_key, AES.MODE_CTR, nonce=nonce, initial_value=position // 16)
    skip = position % 16
    if skip:
        cipher.decrypt(b"\0" * skip)  # advance the keystream inside the first block
    return cipher.decrypt


def ctr_encrypt(key32: bytes, data: bytes) -> bytes:
    """Encrypt a whole file the way MEGA stores it (tests and tooling)."""
    return ctr_decryptor(key32, 0)(data)  # CTR: encrypt == decrypt


def parse_link(text: str) -> tuple[str, str, str, str]:
    """-> (kind, handle, key_b64, sub_handle). kind is 'file' or 'folder'."""
    text = text.strip()
    m = _FILE_RE.search(text)
    if m:
        if not m.group(2):
            raise MegaError("this MEGA link has no decryption key (the part after '#')")
        return "file", m.group(1), m.group(2), ""
    m = _FOLDER_RE.search(text)
    if m:
        if not m.group(2):
            raise MegaError("this MEGA folder link has no decryption key (the part after '#')")
        return "folder", m.group(1), m.group(2), m.group(3) or ""
    raise MegaError("not a MEGA file or folder link")


# ------------------------------------------------------------------ API
class MegaClient:
    def __init__(self, session: aiohttp.ClientSession, proxy: str | None = None, api_url: str | None = None) -> None:
        self.session = session
        self.proxy = proxy or None
        self.api_url = api_url  # None = the module-level API_URL (tests point it at a local server)
        self._seq = 0

    async def _api(self, payload: dict[str, Any], folder: str = "") -> Any:
        self._seq += 1
        params = {"id": str(self._seq)}
        if folder:
            params["n"] = folder
        async with self.session.post(self.api_url or API_URL, params=params, json=[payload], proxy=self.proxy) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, int):
            raise MegaError(_ERRORS.get(data, f"MEGA API error {data}"))
        if not isinstance(data, dict):
            raise MegaError("unexpected reply from MEGA")
        return data

    async def resolve(self, link: str) -> list[MegaFile]:
        """Everything the link points at: one file, or every file inside a folder link."""
        kind, handle, key_b64, sub = parse_link(link)
        if kind == "file":
            return [await self._resolve_file(handle, b64decode(key_b64))]
        files = await self._list_folder(handle, b64decode(key_b64))
        if sub:
            chosen = [f for f in files if f.handle == sub]
            if not chosen:
                raise MegaError("that file is not in this MEGA folder any more")
            files = chosen
        return files

    async def download_url(self, link: str) -> str:
        """Fresh temporary download URL for a file link (also the folder/file form MegaFile.link makes)."""
        kind, handle, _key, sub = parse_link(link)
        if kind == "file":
            info = await self._api({"a": "g", "g": 1, "ssl": 2, "p": handle})
        else:
            if not sub:
                raise MegaError("a folder link needs a /file/ part to pick one file")
            info = await self._api({"a": "g", "g": 1, "ssl": 2, "n": sub}, folder=handle)
        url = info.get("g")
        if not url:
            raise MegaError(_ERRORS[-18])
        return str(url)

    async def _resolve_file(self, handle: str, key32: bytes) -> MegaFile:
        info = await self._api({"a": "g", "g": 1, "ssl": 2, "p": handle})
        aes_key, _ = unpack_file_key(key32)
        attrs = decrypt_attributes(str(info.get("at", "")), aes_key)
        name = sanitize_filename(str(attrs.get("n") or "mega-file"))
        return MegaFile(name=name, size=int(info.get("s") or 0), key=key32, handle=handle, url=str(info.get("g") or ""))

    async def _list_folder(self, folder: str, folder_key: bytes) -> list[MegaFile]:
        from Crypto.Cipher import AES

        if len(folder_key) != 16:
            raise MegaError("bad folder key in the link")
        data = await self._api({"a": "f", "c": 1, "r": 1}, folder=folder)
        nodes = data.get("f") or []
        ecb = AES.new(folder_key, AES.MODE_ECB)
        names: dict[str, str] = {}
        parents: dict[str, str] = {}
        files: list[MegaFile] = []
        for node in nodes:
            h = str(node.get("h", ""))
            k = str(node.get("k", ""))
            if ":" not in k:
                continue
            enc_key = b64decode(k.split(":", 1)[1])
            enc_key = enc_key[: len(enc_key) - len(enc_key) % 16]
            if not enc_key:
                continue
            node_key = ecb.decrypt(enc_key)
            t = node.get("t")
            attr_key = node_key[:16]
            if t == 0:
                if len(node_key) != 32:
                    continue
                attr_key, _ = unpack_file_key(node_key)
            try:
                attrs = decrypt_attributes(str(node.get("a", "")), attr_key)
            except MegaError:
                continue
            name = sanitize_filename(str(attrs.get("n") or h))
            names[h] = name
            parents[h] = str(node.get("p", ""))
            if t == 0:
                files.append(
                    MegaFile(
                        name=name,
                        size=int(node.get("s") or 0),
                        key=node_key,
                        handle=h,
                        folder=folder,
                        folder_key=folder_key,
                    )
                )
        for f in files:
            parts: list[str] = []
            p = parents.get(f.handle, "")
            while p in names and p != folder:
                parts.append(names[p])
                p = parents.get(p, "")
            f.path = "/".join(reversed(parts))
        if not files:
            raise MegaError("this MEGA folder is empty")
        return files


# ------------------------------------------------------------------ runner
class MegaRunner(TaskRunner):
    """Segmented download of a MEGA file with on-the-fly decryption and download-URL refresh."""

    def __init__(self, task, session, *, client: MegaClient, **kwargs) -> None:
        super().__init__(task, session, **kwargs)
        self.client = client
        self._key = b64decode(task.crypto_key)

    async def run(self) -> None:
        try:
            self.task.url = await self.client.download_url(self.task.origin or self.task.url)
        except MegaError as exc:
            self.task.status = Status.ERROR
            self.task.error = str(exc)
            return
        except Exception:  # noqa: BLE001 - offline or API hiccup: try the URL we already have
            pass
        await super().run()

    def _transform_at(self, position: int) -> Callable[[bytes], bytes] | None:
        return ctr_decryptor(self._key, position)
