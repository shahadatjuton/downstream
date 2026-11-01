"""Register IDM Clone as the handler for magnet: links and .torrent files (per user, no admin).

Chrome, Edge, Firefox and Explorer all look at HKCU\\Software\\Classes, so after this the
"Open magnet link" prompt in the browser and a double-click on a .torrent file land in the app.
"""

from __future__ import annotations

import sys

PROGID = "IDMClone.Torrent"
MAGNET_KEY = r"Software\Classes\magnet"
TORRENT_EXT_KEY = r"Software\Classes\.torrent"
PROGID_KEY = rf"Software\Classes\{PROGID}"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    return f'"{sys.executable}" -m idm --gui "%1"'


def _read_default(path: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, "")
            return str(value)
    except OSError:
        return None


def is_registered() -> bool:
    """True when magnet: links currently open with this app."""
    if sys.platform != "win32":
        return False
    cmd = _read_default(MAGNET_KEY + r"\shell\open\command") or ""
    exe = sys.executable.lower()
    return exe in cmd.lower()


def set_registered(enabled: bool) -> bool:
    """Register or unregister the magnet: protocol and the .torrent file type. Returns True on success."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        if enabled:
            _write(winreg, MAGNET_KEY, "URL:Magnet link")
            _write(winreg, MAGNET_KEY, "", name="URL Protocol")
            _write(winreg, MAGNET_KEY + r"\DefaultIcon", f'"{sys.executable}",0')
            _write(winreg, MAGNET_KEY + r"\shell\open\command", launch_command())
            _write(winreg, PROGID_KEY, "BitTorrent file")
            _write(winreg, PROGID_KEY + r"\DefaultIcon", f'"{sys.executable}",0')
            _write(winreg, PROGID_KEY + r"\shell\open\command", launch_command())
            _write(winreg, TORRENT_EXT_KEY, PROGID)
        else:
            if is_registered():
                _delete_tree(winreg, MAGNET_KEY)
            if _read_default(TORRENT_EXT_KEY) == PROGID:
                _delete_tree(winreg, TORRENT_EXT_KEY)
            _delete_tree(winreg, PROGID_KEY)
        return True
    except OSError:
        return False


def _write(winreg, path: str, value: str, name: str = "") -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _delete_tree(winreg, path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(winreg, path + "\\" + sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass
