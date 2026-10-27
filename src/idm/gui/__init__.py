"""PySide6 desktop front end: logging, crash reporting, single instance, and startup."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback

log = logging.getLogger("idm")


def setup_logging() -> None:
    """Log to %APPDATA%\\idm-clone\\logs\\app.log (rotating) and, when there is a console, to stderr."""
    from ..engine.settings import app_data_dir

    log_dir = app_data_dir() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError:
        pass
    if sys.stderr is not None:
        logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def install_crash_handler() -> None:
    """Unhandled exceptions get logged and shown instead of silently killing a windowed exe."""
    from ..engine.settings import app_data_dir

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("Unhandled exception:\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Icon.Critical)
                box.setWindowTitle("IDM Clone ran into a problem")
                box.setText(f"{exc_type.__name__}: {exc}")
                box.setInformativeText(
                    f"Details were written to {app_data_dir() / 'logs' / 'app.log'}.\n"
                    "Your downloads are saved and will be there when you reopen the app."
                )
                box.setDetailedText(text)
                box.exec()
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def run_gui(argv: list[str] | None = None, initial_url: str = "", minimized: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    from .. import __version__
    from ..engine import DownloadManager, Settings
    from .main_window import MainWindow, make_app_icon
    from .theme import apply_theme

    setup_logging()
    install_crash_handler()
    log.info("IDM Clone %s starting (frozen=%s)", __version__, getattr(sys, "frozen", False))

    # Single instance: if another copy is running, ask it to show itself and exit.
    if _raise_running_instance(initial_url):
        log.info("another instance is running; handed over and exiting")
        return 0

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("IDM Clone")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("IDM Clone")
    app.setWindowIcon(make_app_icon())
    app.setQuitOnLastWindowClosed(False)
    settings = Settings.load()
    theme = apply_theme(app, settings.theme)

    manager = DownloadManager(settings, enable_ipc=settings.browser_integration)
    manager.record_app_path()
    manager.start()
    win = MainWindow(manager, theme, initial_url)
    if minimized or settings.start_minimized:
        if win.tray:
            win.hide()
        else:
            win.showMinimized()
    else:
        win.show()
    return app.exec()


def _raise_running_instance(initial_url: str = "") -> bool:
    """True if another instance answered (we asked it to show itself / take the URL)."""
    from ..engine.ipc import read_ipc_info, send
    from ..native_host import _pid_alive

    info = read_ipc_info()
    if not info or not _pid_alive(int(info.get("pid", 0))):
        return False
    try:
        if initial_url:
            resp = send({"cmd": "add", "url": initial_url}, timeout=3)
        else:
            resp = send({"cmd": "show"}, timeout=3)
        return bool(resp.get("ok"))
    except (OSError, ValueError):
        return False
