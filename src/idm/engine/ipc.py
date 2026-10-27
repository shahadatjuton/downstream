"""Local IPC between the desktop app and the browser native-messaging host.

The app listens on 127.0.0.1 on a random port and writes {port, token, pid} to
%APPDATA%\\idm-clone\\ipc.json. Only processes running as the same user can read that
file, so a web page cannot talk to the app directly. Protocol: newline-delimited JSON;
every request must carry the token.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .settings import app_data_dir

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def ipc_info_path() -> Path:
    return app_data_dir() / "ipc.json"


def app_path_file() -> Path:
    return app_data_dir() / "app_path.txt"


def read_ipc_info() -> dict[str, Any] | None:
    try:
        data = json.loads(ipc_info_path().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data or "token" not in data:
        return None
    return data


class IpcServer:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.port = 0
        self.token = ""
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.token = secrets.token_hex(16)
        info = {"port": self.port, "token": self.token, "pid": os.getpid()}
        path = ipc_info_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info), "utf-8")
        tmp.replace(path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            info = read_ipc_info()
            if info and info.get("pid") == os.getpid():
                ipc_info_path().unlink()
        except OSError:
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=60)
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    await self._reply(writer, {"ok": False, "error": "bad json"})
                    continue
                if not isinstance(msg, dict) or not secrets.compare_digest(str(msg.get("token", "")), self.token):
                    await self._reply(writer, {"ok": False, "error": "unauthorized"})
                    break
                msg.pop("token", None)
                try:
                    resp = await self.handler(msg)
                except Exception as exc:  # noqa: BLE001
                    resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                await self._reply(writer, resp)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await writer.drain()


def send(msg: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    """Synchronous client used by the native host and the CLI. Raises OSError if the app is not running."""
    info = read_ipc_info()
    if not info:
        raise ConnectionRefusedError("app is not running (no ipc.json)")
    payload = dict(msg)
    payload["token"] = info["token"]
    with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise ConnectionError("no reply from app")
    return json.loads(buf)
