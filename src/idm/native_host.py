"""Chrome/Edge native messaging host.

The browser launches this process and talks to it over stdin/stdout using Chrome's
4-byte-length-prefixed JSON framing. We forward each message to the running desktop
app over the local IPC socket, starting the app first if it is not running.

Run manually for debugging:  python -m idm.native_host
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
from typing import Any

from .engine.ipc import app_path_file, read_ipc_info, send

HOST_NAME = "com.idmclone.host"


def _binary_stdio() -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def read_message() -> dict[str, Any] | None:
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def write_message(obj: dict[str, Any]) -> None:
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 (Win32 constant)
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _app_running() -> bool:
    info = read_ipc_info()
    return bool(info and _pid_alive(int(info.get("pid", 0))))


def launch_app() -> bool:
    """Start the desktop app using the command the app recorded on its last run."""
    try:
        cmd = app_path_file().read_text("utf-8").strip()
    except OSError:
        return False
    if not cmd:
        return False
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, shell=False, creationflags=flags, close_fds=True)
    except OSError:
        try:
            subprocess.Popen(cmd, shell=True, creationflags=flags, close_fds=True)  # noqa: S602
        except OSError:
            return False
    return True


def forward(msg: dict[str, Any]) -> dict[str, Any]:
    if not _app_running():
        if not launch_app():
            return {
                "ok": False,
                "error": "IDM Clone is not running and could not be started. Open it once so it records its location.",
            }
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.3)
            if _app_running():
                break
        else:
            return {"ok": False, "error": "IDM Clone did not start in time"}
    last_err = ""
    for _ in range(10):
        try:
            return send(msg)
        except (OSError, ValueError) as exc:
            last_err = str(exc)
            time.sleep(0.3)
    return {"ok": False, "error": f"could not reach IDM Clone: {last_err}"}


def main() -> int:
    _binary_stdio()
    while True:
        try:
            msg = read_message()
        except (ValueError, OSError):
            return 1
        if msg is None:
            return 0
        if msg.get("cmd") == "host-ping":
            write_message({"ok": True, "host": HOST_NAME, "app_running": _app_running()})
            continue
        write_message(forward(msg))


if __name__ == "__main__":
    sys.exit(main())
