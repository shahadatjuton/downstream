"""Entry point.

python -m idm                 -> desktop GUI
python -m idm URL [URL...]    -> download in the terminal (no GUI)
python -m idm --gui URL       -> open the GUI with the add-dialog prefilled
"""

from __future__ import annotations

import argparse
import sys
import time

from .engine import DownloadManager, Settings, Status
from .gui.util import fmt_bytes, fmt_eta, fmt_speed


def _cli_download(urls: list[str], args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.dir:
        settings.download_dir = args.dir
        settings.use_categories = False
    if args.connections:
        settings.connections_per_download = args.connections
    manager = DownloadManager(settings)
    manager.start()
    exit_code = 0
    try:
        ids = []
        for url in urls:
            try:
                task = manager.add_smart(url, filename=args.output if len(urls) == 1 else None).result(120)
            except Exception as exc:  # noqa: BLE001
                print(f"error: {url}: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            what = "video via yt-dlp" if task.kind == "media" else ("resumable" if task.resumable else "single stream")
            print(f"{task.filename}  ({fmt_bytes(task.size)}, {what})")
            ids.append(task.id)

        pending = set(ids)
        while pending:
            time.sleep(0.25)
            lines = []
            for task in manager.snapshot():
                if task.id not in pending:
                    continue
                if task.status not in (Status.QUEUED, Status.DOWNLOADING):
                    pending.discard(task.id)
                    sys.stdout.write("\r\x1b[K")
                    if task.status == Status.COMPLETED:
                        print(f"done: {task.final_path}")
                    else:
                        print(f"{task.status.value}: {task.filename}: {task.error or ''}", file=sys.stderr)
                        exit_code = 1
                    continue
                pct = f"{task.progress * 100:5.1f}%" if task.progress is not None else fmt_bytes(task.downloaded)
                lines.append(f"{task.filename[:30]:30} {pct} {fmt_speed(task.speed):>12} {fmt_eta(task.eta):>8}")
            if lines:
                sys.stdout.write("\r\x1b[K" + " | ".join(lines))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\npausing…", file=sys.stderr)
        exit_code = 130
    finally:
        manager.stop()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="idm-clone", description="Segmented download manager")
    parser.add_argument("urls", nargs="*", help="URLs to download from the terminal; omit to open the GUI")
    parser.add_argument("--gui", action="store_true", help="open the GUI (with the first URL prefilled)")
    parser.add_argument("-d", "--dir", help="destination folder (CLI mode)")
    parser.add_argument("-o", "--output", help="output filename (CLI mode, single URL)")
    parser.add_argument("-n", "--connections", type=int, help="connections per download (CLI mode)")
    parser.add_argument(
        "--minimized", action="store_true", help="start hidden in the tray (used by 'start with Windows')"
    )
    args = parser.parse_args(argv)

    # A windowed (PyInstaller) build has no console: a URL argument means "open the GUI with it".
    headless_ok = sys.stdout is not None and not getattr(sys, "frozen", False)
    if args.urls and not args.gui and headless_ok:
        return _cli_download(args.urls, args)

    from .gui import run_gui

    return run_gui([sys.argv[0]], initial_url=args.urls[0] if args.urls else "", minimized=args.minimized)


if __name__ == "__main__":
    sys.exit(main())
