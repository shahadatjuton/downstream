"""The 'Add download' dialog.

Handles every kind of input the app accepts:
  * a plain file link (HTTP download with segments),
  * a web page with a video/audio stream (YouTube, Facebook, Instagram, TikTok, …), analysed
    with yt-dlp and offered with a quality picker, thumbnail preview and "save thumbnail",
  * a magnet link, a .torrent link, or a local .torrent file (BitTorrent),
  * a MEGA.nz file or folder link (decrypted while downloading),
  * many links at once ("Add many"), each routed automatically.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..engine import DownloadManager, DownloadTask, MediaInfo, MegaFile, ProbeResult, looks_like_torrent_file
from ..engine.media import looks_like_media_page, thumbnail_ext
from ..engine.settings import TORRENT_FOLDER
from ..engine.torrent import magnet_name
from .icons import CATEGORY_ICONS, icon
from .theme import Theme
from .util import fmt_bytes, fmt_eta, link_kind


class _FutureBridge(QObject):
    """Turns a concurrent.futures.Future callback (worker thread) into a Qt signal (GUI thread)."""

    finished = Signal(int, object, object)  # (sequence, result | None, error str | None)

    def attach(self, seq: int, fut: concurrent.futures.Future) -> None:
        def done(f: concurrent.futures.Future) -> None:
            try:
                self.finished.emit(seq, f.result(), None)
            except Exception as exc:  # noqa: BLE001
                self.finished.emit(seq, None, f"{type(exc).__name__}: {exc}")

        fut.add_done_callback(done)


class AddDownloadDialog(QDialog):
    install_ffmpeg_requested = Signal(str)  # emitted with the page URL so the window can reopen it afterwards

    def __init__(
        self,
        manager: DownloadManager,
        theme: Theme,
        url: str = "",
        parent: QWidget | None = None,
        *,
        headers: dict[str, str] | None = None,
        kind: str = "auto",
        filename: str = "",
        referrer: str = "",
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.theme = theme
        self.headers = dict(headers or {})
        self.kind_hint = kind  # "auto" | "file" | "media" | "download" | "torrent" | "mega"
        self.referrer = referrer
        self.probe_result: ProbeResult | None = None
        self.media_info: MediaInfo | None = None
        self.torrent_desc: dict | None = None  # name/size/files of a .torrent, or {"magnet": True}
        self.mega_files: list[MegaFile] | None = None
        self.source_kind = ""  # "" | "file" | "media" | "magnet" | "torrent" | "mega"
        self.needs_auth = False  # the server answered 401; show the login row
        self.duplicate_of: DownloadTask | None = None  # an existing task with the same link (user may still proceed)
        self._auth_url = ""  # the link the login row belongs to (survives re-probes of the same link)
        self.start_now = True
        self.batch_mode = False
        self._seq = 0
        self._probe_bridge = _FutureBridge(self)
        self._probe_bridge.finished.connect(self._on_probe_done)
        self._media_bridge = _FutureBridge(self)
        self._media_bridge.finished.connect(self._on_media_done)
        self._thumb_bridge = _FutureBridge(self)
        self._thumb_bridge.finished.connect(self._on_thumb_done)
        self._torrent_bridge = _FutureBridge(self)
        self._torrent_bridge.finished.connect(self._on_torrent_done)
        self._mega_bridge = _FutureBridge(self)
        self._mega_bridge.finished.connect(self._on_mega_done)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(450)
        self._debounce.timeout.connect(self._probe)

        self.setWindowTitle("Add download")
        self.setMinimumWidth(680)

        # --- URL row (single) and batch editor (many)
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("File link, video page (YouTube, Facebook, TikTok…), magnet, MEGA link…")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.textChanged.connect(self._on_url_edited)
        paste = QPushButton(icon("paste", theme.text, 18), "")
        paste.setToolTip("Paste from clipboard")
        paste.setAccessibleName("Paste link from clipboard")
        paste.setFixedWidth(36)
        paste.setAutoDefault(False)
        paste.clicked.connect(self._paste)
        self.torrent_btn = QPushButton(icon("torrent", theme.text, 18), "")
        self.torrent_btn.setToolTip("Choose a .torrent file from this computer")
        self.torrent_btn.setAccessibleName("Choose a torrent file")
        self.torrent_btn.setFixedWidth(36)
        self.torrent_btn.setAutoDefault(False)
        self.torrent_btn.clicked.connect(self._browse_torrent)
        self.many_btn = QPushButton("Add many")
        self.many_btn.setCheckable(True)
        self.many_btn.setAutoDefault(False)
        self.many_btn.setToolTip("Paste a list of links, one per line")
        self.many_btn.toggled.connect(self._set_batch_mode)
        self.many_btn.setFixedWidth(110)
        self.batch_label = QLabel("Links, one per line")
        self.batch_label.setObjectName("muted")
        self.batch_label.hide()
        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        url_row.addWidget(self.batch_label, 1)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(paste)
        url_row.addWidget(self.torrent_btn)
        url_row.addWidget(self.many_btn)

        self.batch_edit = QPlainTextEdit()
        self.batch_edit.setPlaceholderText(
            "One link per line. Files, video pages, magnets and MEGA links are detected automatically."
        )
        self.batch_edit.setFixedHeight(120)
        self.batch_edit.textChanged.connect(self._on_batch_edited)
        self.batch_edit.hide()

        # --- Info card
        self.card = QFrame()
        self.card.setObjectName("card")
        card_lay = QHBoxLayout(self.card)
        card_lay.setContentsMargins(12, 10, 12, 10)
        card_lay.setSpacing(12)
        self.card_icon = QLabel()
        self.card_icon.setFixedSize(36, 36)
        self.thumb = QLabel()
        self.thumb.setFixedSize(112, 63)
        self.thumb.setScaledContents(False)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.hide()
        self.card_title = QLabel("Enter a URL to check it")
        self.card_title.setObjectName("title")
        self.card_title.setWordWrap(True)
        self.card_sub = QLabel("")
        self.card_sub.setObjectName("muted")
        self.card_sub.setWordWrap(True)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(self.card_title)
        texts.addWidget(self.card_sub)
        self.ffmpeg_btn = QPushButton("Install ffmpeg (90 MB) for HD")
        self.ffmpeg_btn.setAutoDefault(False)
        self.ffmpeg_btn.setToolTip("Downloads ffmpeg into the app folder, then reopens this link with all qualities")
        self.ffmpeg_btn.clicked.connect(self._request_ffmpeg)
        self.ffmpeg_btn.hide()
        card_lay.addWidget(self.card_icon, 0, Qt.AlignmentFlag.AlignTop)
        card_lay.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignTop)
        card_lay.addLayout(texts, 1)
        card_lay.addWidget(self.ffmpeg_btn, 0, Qt.AlignmentFlag.AlignTop)
        self._set_card_icon("file", theme.muted)

        # --- Options
        self.quality = QComboBox()
        self.quality.currentIndexChanged.connect(self._on_quality_changed)
        self.quality_row = QWidget()
        q_lay = QHBoxLayout(self.quality_row)
        q_lay.setContentsMargins(0, 0, 0, 0)
        q_lay.addWidget(self.quality, 1)
        self.quality_hint = QLabel("")
        self.quality_hint.setObjectName("muted")
        q_lay.addWidget(self.quality_hint)

        self.name_edit = QLineEdit(filename)
        self.name_edit.setPlaceholderText("Taken from the server or URL")
        self.folder_edit = QLineEdit(manager.settings.download_dir)
        browse = QPushButton("Browse…")
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)

        self.use_category = QCheckBox("Sort into a category sub-folder automatically")
        self.use_category.setChecked(manager.settings.use_categories)

        self.save_thumb = QCheckBox("Also save the thumbnail image next to the video")
        self.save_thumb.hide()

        self.conn_spin = QSpinBox()
        self.conn_spin.setRange(1, 32)
        self.conn_spin.setValue(manager.settings.connections_per_download)
        self.conn_spin.setFixedWidth(80)
        self.conn_row = QWidget()
        c_lay = QHBoxLayout(self.conn_row)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.addWidget(self.conn_spin)
        conn_hint = QLabel("parallel connections")
        conn_hint.setObjectName("muted")
        c_lay.addWidget(conn_hint)
        c_lay.addStretch(1)

        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("Username")
        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("Password")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.returnPressed.connect(self._probe_with_login)
        self.remember_login = QCheckBox("Remember for this site")
        self.remember_login.setChecked(True)
        self.remember_login.setToolTip("Stored encrypted for your Windows account; manage saved logins in Settings")
        self.user_edit.setAccessibleName("Username")
        self.pass_edit.setAccessibleName("Password")
        self.login_btn = QPushButton("Log in")
        self.login_btn.setAutoDefault(False)
        self.login_btn.clicked.connect(self._probe_with_login)
        self.auth_row = QWidget()
        a_lay = QHBoxLayout(self.auth_row)
        a_lay.setContentsMargins(0, 0, 0, 0)
        a_lay.setSpacing(6)
        a_lay.addWidget(self.user_edit, 1)
        a_lay.addWidget(self.pass_edit, 1)
        a_lay.addWidget(self.login_btn)
        a_lay.addWidget(self.remember_login)

        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form.setVerticalSpacing(10)
        self.auth_label = QLabel("Login")
        self.form.addRow(self.auth_label, self.auth_row)
        self.quality_label = QLabel("Quality")
        self.form.addRow(self.quality_label, self.quality_row)
        self.name_label = QLabel("Save as")
        self.form.addRow(self.name_label, self.name_edit)
        self.form.addRow("Folder", folder_row)
        self.form.addRow("", self.use_category)
        self.form.addRow("", self.save_thumb)
        self.conn_label = QLabel("Connections")
        self.form.addRow(self.conn_label, self.conn_row)
        self.checksum_edit = QLineEdit()
        self.checksum_edit.setPlaceholderText("Optional: MD5, SHA-1 or SHA-256 from the download page")
        self.checksum_edit.setToolTip(
            "Paste the digest the site publishes. The file is hashed before it is renamed; a mismatch "
            "marks the download as failed and keeps the .part file."
        )
        self.checksum_edit.textChanged.connect(self._on_checksum_edited)
        self.checksum_label = QLabel("Checksum")
        self.form.addRow(self.checksum_label, self.checksum_edit)
        self._show_rows()

        # --- Buttons
        buttons = QDialogButtonBox()
        self.start_btn = buttons.addButton("Start download", QDialogButtonBox.ButtonRole.AcceptRole)
        self.start_btn.setDefault(True)
        self.start_btn.setObjectName("primary")
        self.later_btn = buttons.addButton("Add paused", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.later_btn.clicked.connect(self._download_later)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)
        root.addLayout(url_row)
        root.addWidget(self.batch_edit)
        root.addWidget(self.card)
        root.addLayout(self.form)
        root.addWidget(buttons)

        self._set_ready(False)
        if url:
            if "\n" in url.strip():
                self.many_btn.setChecked(True)
                self.batch_edit.setPlainText(url)
            else:
                self._probe()
        else:
            self.url_edit.setFocus()

    # --------------------------------------------------------------- state
    @property
    def is_media(self) -> bool:
        return self.media_info is not None

    def _show_rows(self) -> None:
        """Show only the options that make sense for the detected source."""
        kind = self.source_kind
        media = kind == "media"
        torrent = kind in ("magnet", "torrent")
        self.auth_label.setVisible(self.needs_auth)
        self.auth_row.setVisible(self.needs_auth)
        many_files = kind == "mega" and self.mega_files is not None and len(self.mega_files) > 1
        self.quality_label.setVisible(media)
        self.quality_row.setVisible(media)
        show_name = not self.batch_mode and not torrent and not many_files
        self.name_label.setVisible(show_name)
        self.name_edit.setVisible(show_name)
        plain = kind in ("", "file")
        self.conn_label.setVisible(plain and not self.batch_mode)
        self.conn_row.setVisible(plain and not self.batch_mode)
        self.checksum_label.setVisible(plain and not self.batch_mode)
        self.checksum_edit.setVisible(plain and not self.batch_mode)
        fmt = self.quality.currentData() if media else None
        self.save_thumb.setVisible(
            media and bool(self.media_info and self.media_info.thumbnail) and not (fmt and fmt.thumbnail)
        )

    def _set_card_icon(self, name: str, color) -> None:
        self.card_icon.setPixmap(icon(name, color, 36).pixmap(36, 36))
        self.card_icon.show()
        self.thumb.hide()

    def _set_ready(self, ready: bool) -> None:
        self.start_btn.setEnabled(ready)
        self.later_btn.setEnabled(ready)

    def _reset_detection(self) -> None:
        self.duplicate_of = None
        self.start_btn.setText("Start all" if self.batch_mode else "Start download")
        self.needs_auth = bool(self._auth_url) and self.url() == self._auth_url
        self.probe_result = None
        self.media_info = None
        self.torrent_desc = None
        self.mega_files = None
        self.source_kind = ""
        self.ffmpeg_btn.hide()
        self.use_category.setText("Sort into a category sub-folder automatically")
        self._show_rows()

    def _paste(self) -> None:
        text = QApplication.clipboard().text().strip()
        if "\n" in text and not self.batch_mode:
            self.many_btn.setChecked(True)
        if self.batch_mode:
            self.batch_edit.setPlainText(text)
        else:
            self.url_edit.setText(text)

    def _browse_torrent(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a .torrent file", "", "Torrent files (*.torrent)")
        if path:
            if self.batch_mode:
                self.many_btn.setChecked(False)
            self.url_edit.setText(path)
            self._debounce.stop()
            self._probe()

    def _on_url_edited(self, _text: str) -> None:
        self.kind_hint = "auto"  # the user typed something new; forget the browser's hint
        self._debounce.start()

    # --------------------------------------------------------------- batch
    def _set_batch_mode(self, on: bool) -> None:
        self.batch_mode = on
        self.url_edit.setVisible(not on)
        self.torrent_btn.setVisible(not on)
        self.batch_label.setVisible(on)
        self.batch_edit.setVisible(on)
        self.setWindowTitle("Add many downloads" if on else "Add download")
        self.start_btn.setText("Start all" if on else "Start download")
        self.later_btn.setText("Add all paused" if on else "Add paused")
        self._reset_detection()
        if on:
            if self.url_edit.text().strip() and not self.batch_edit.toPlainText().strip():
                self.batch_edit.setPlainText(self.url_edit.text().strip())
            self._on_batch_edited()
            self.batch_edit.setFocus()
        else:
            self._probe()

    def batch_urls(self) -> list[str]:
        return [ln.strip() for ln in self.batch_edit.toPlainText().splitlines() if link_kind(ln.strip())]

    def _on_batch_edited(self) -> None:
        urls = self.batch_urls()
        bad = sum(1 for ln in self.batch_edit.toPlainText().splitlines() if ln.strip() and not link_kind(ln.strip()))
        self.card_title.setText(
            f"{len(urls)} link{'s' if len(urls) != 1 else ''} ready" if urls else "Paste links, one per line"
        )
        self.card_sub.setText(
            "Each link is checked when it starts: video pages go through yt-dlp at the best quality, magnets and "
            "MEGA links are handled natively, files use segments."
            + (f"  {bad} line(s) are not links and will be skipped." if bad else "")
        )
        self._set_card_icon("list", self.theme.accent if urls else self.theme.muted)
        self._set_ready(bool(urls))

    # ------------------------------------------------------------- probing
    def _probe(self) -> None:
        if self.batch_mode:
            return
        url = self.url_edit.text().strip()
        self._reset_detection()
        kind = link_kind(url)
        if not kind:
            self.card_title.setText("Enter a link to check it")
            self.card_sub.setText(
                "Direct file links, video pages (YouTube, Facebook, Instagram, TikTok, …), magnet links, "
                ".torrent files and MEGA links."
            )
            self._set_card_icon("file", self.theme.muted)
            self._set_ready(False)
            return
        self._seq += 1
        self.card_title.setText("Checking link…")
        self.card_sub.setText(url)
        self._set_card_icon("clock", self.theme.muted)
        self._set_ready(False)
        if kind == "magnet" or self.kind_hint == "torrent" and kind != "torrent":
            self._show_magnet(url)
            return
        if kind == "torrent":
            self._start_torrent(url)
            return
        if kind == "mega" or self.kind_hint == "mega":
            self._start_mega(url)
            return
        if self.kind_hint == "media" or (kind == "video" and self.kind_hint == "auto"):
            self._start_media_analysis(url)
            return
        self._probe_bridge.attach(self._seq, self.manager.probe(url, self.headers or None))

    def _probe_with_login(self) -> None:
        """Re-check the link with the username/password typed in the login row."""
        url = self.url()
        if not url or not self.user_edit.text().strip():
            self.user_edit.setFocus()
            return
        self._seq += 1
        self.card_title.setText("Logging in…")
        self.card_sub.setText(url)
        self._set_card_icon("clock", self.theme.muted)
        self._set_ready(False)
        fut = self.manager.probe(
            url,
            self.headers or None,
            username=self.user_edit.text().strip(),
            password=self.pass_edit.text(),
        )
        self._probe_bridge.attach(self._seq, fut)

    def _on_probe_done(self, seq: int, result: ProbeResult | None, error: str | None) -> None:
        if seq != self._seq or self.batch_mode:
            return
        url = self.url_edit.text().strip()
        if error and error.startswith("AuthRequiredError"):
            self.needs_auth = True
            self._auth_url = url
            self.card_title.setText("This site asks for a username and password")
            notes = ["Enter them below and press Log in."]
            if self.user_edit.text():
                notes.append("The saved login for this site was refused.")
            if url.lower().startswith("http://"):
                notes.append("⚠ This link is not encrypted (http://): the password travels in clear text.")
            self.card_sub.setText(" ".join(notes))
            self._set_card_icon("warning", self.theme.accent)
            self._show_rows()
            self._set_ready(False)
            (self.pass_edit if self.user_edit.text() else self.user_edit).setFocus()
            return
        if error or result is None:
            if looks_like_media_page(url, "") and self.kind_hint != "file":
                self._start_media_analysis(url)
                return
            self.card_title.setText("Could not reach this link")
            self.card_sub.setText(error or "unknown error")
            self._set_card_icon("warning", self.theme.danger)
            self._set_ready(False)
            return
        if looks_like_torrent_file(result.filename, result.content_type) and self.kind_hint != "media":
            self._start_torrent(url)
            return
        if self.kind_hint not in ("file", "download") and looks_like_media_page(result.final_url, result.content_type):
            self._start_media_analysis(url)
            return
        self.probe_result = result
        self.source_kind = "file"
        size = fmt_bytes(result.size) if result.size else "unknown size"
        mode = "multi-connection, resumable" if result.resumable else "single connection, cannot resume"
        ctype = result.content_type.split(";")[0] if result.content_type else ""
        self.card_title.setText(result.filename)
        notes = [x for x in (size, mode, ctype) if x]
        self.duplicate_of = self.manager.find_duplicate(url) or self.manager.find_duplicate(result.final_url)
        if self.duplicate_of is not None:
            from ..engine import Status

            state = (
                "already downloaded"
                if self.duplicate_of.status == Status.COMPLETED
                else f"already in your list ({self.duplicate_of.status.value})"
            )
            notes.append(f"⚠ {state}")
            self.start_btn.setText("Download anyway")
        else:
            target = Path(self.manager.settings.resolve_dest_dir(result.filename, self.dest_dir())) / result.filename
            if target.exists():
                notes.append(f"⚠ a file with this name exists; it will be saved as '{target.stem} (1){target.suffix}'")
        self.card_sub.setText("  ·  ".join(notes))
        cat = self.manager.settings.category_for(result.filename)
        self._set_card_icon(CATEGORY_ICONS.get(cat or "", "file"), self.theme.accent)
        if not self.name_edit.text():
            self.name_edit.setText(result.filename)
        if cat:
            self.use_category.setText(f"Sort into the '{cat}' sub-folder automatically")
        self._show_rows()
        self._set_ready(True)

    # ------------------------------------------------------------ torrents
    def _show_magnet(self, url: str) -> None:
        self.source_kind = "magnet"
        self.torrent_desc = {"name": magnet_name(url), "size": None, "files": [], "magnet": True}
        self.card_title.setText(self.torrent_desc["name"])
        self.card_sub.setText(
            "Magnet link  ·  the exact name, size and files arrive from other peers once the download starts"
        )
        self._set_card_icon("torrent", self.theme.accent)
        self.use_category.setText(f"Sort into the '{TORRENT_FOLDER}' sub-folder automatically")
        self._show_rows()
        self._set_ready(True)

    def _start_torrent(self, url: str) -> None:
        self.card_title.setText("Reading torrent…")
        self.card_sub.setText(url)
        self._set_card_icon("torrent", self.theme.muted)
        self._torrent_bridge.attach(self._seq, self.manager.describe_torrent(url, self.headers or None))

    def _on_torrent_done(self, seq: int, desc: dict | None, error: str | None) -> None:
        if seq != self._seq or self.batch_mode:
            return
        if error or desc is None:
            self.card_title.setText("Could not read this torrent")
            self.card_sub.setText(error or "unknown error")
            self._set_card_icon("warning", self.theme.danger)
            self._set_ready(False)
            return
        self.torrent_desc = desc
        self.source_kind = "torrent"
        files = desc.get("files") or []
        bits = ["Torrent", fmt_bytes(desc["size"]) if desc.get("size") else "unknown size"]
        if len(files) > 1:
            bits.append(f"{len(files)} files")
        self.card_title.setText(desc["name"])
        self.card_sub.setText("  ·  ".join(bits))
        if files:
            preview = "\n".join(f"{p}  ({fmt_bytes(s)})" for p, s in files[:40])
            if len(files) > 40:
                preview += f"\n… and {len(files) - 40} more"
            self.card_sub.setToolTip(preview)
        self._set_card_icon("torrent", self.theme.accent)
        self.use_category.setText(f"Sort into the '{TORRENT_FOLDER}' sub-folder automatically")
        self._show_rows()
        self._set_ready(True)

    # ---------------------------------------------------------------- MEGA
    def _start_mega(self, url: str) -> None:
        self.card_title.setText("Asking MEGA about this link…")
        self.card_sub.setText("The file name and size are decrypted locally with the key from the link.")
        self._set_card_icon("cloud", self.theme.muted)
        self._mega_bridge.attach(self._seq, self.manager.resolve_mega(url))

    def _on_mega_done(self, seq: int, files: list[MegaFile] | None, error: str | None) -> None:
        if seq != self._seq or self.batch_mode:
            return
        if error or not files:
            self.card_title.setText("Could not open this MEGA link")
            self.card_sub.setText(error or "unknown error")
            self._set_card_icon("warning", self.theme.danger)
            self._set_ready(False)
            return
        self.mega_files = files
        self.source_kind = "mega"
        total = sum(f.size for f in files)
        if len(files) == 1:
            f = files[0]
            self.card_title.setText(f.name)
            self.card_sub.setText(f"MEGA  ·  {fmt_bytes(f.size)}  ·  multi-connection, decrypted while downloading")
            if not self.name_edit.text():
                self.name_edit.setText(f.name)
            cat = self.manager.settings.category_for(f.name)
            if cat:
                self.use_category.setText(f"Sort into the '{cat}' sub-folder automatically")
        else:
            self.card_title.setText(f"{len(files)} files from a MEGA folder")
            self.card_sub.setText(f"MEGA folder  ·  {fmt_bytes(total)} in total  ·  sub-folders are kept")
            preview = "\n".join(
                f"{(f.path + '/') if f.path else ''}{f.name}  ({fmt_bytes(f.size)})" for f in files[:40]
            )
            if len(files) > 40:
                preview += f"\n… and {len(files) - 40} more"
            self.card_sub.setToolTip(preview)
        self._set_card_icon("cloud", self.theme.accent)
        self._show_rows()
        self._set_ready(True)

    # --------------------------------------------------------------- media
    def _start_media_analysis(self, url: str) -> None:
        self.card_title.setText("Looking for video on this page…")
        self.card_sub.setText("Analysing with yt-dlp. This can take a few seconds.")
        self._set_card_icon("video", self.theme.muted)
        self._media_bridge.attach(self._seq, self.manager.extract_media(url, self.headers or None))

    def _on_media_done(self, seq: int, info: MediaInfo | None, error: str | None) -> None:
        if seq != self._seq or self.batch_mode:
            return
        if error or info is None:
            self.card_title.setText("No downloadable video found on this page")
            self.card_sub.setText(error or "unknown error")
            self._set_card_icon("warning", self.theme.danger)
            self._set_ready(False)
            return
        self.media_info = info
        self.source_kind = "media"
        self._set_card_icon("video", self.theme.accent)
        self.card_title.setText(info.title)
        bits = [info.extractor]
        if info.duration:
            bits.append(fmt_eta(info.duration))
        if not self.manager.ffmpeg:
            video_choices = [f for f in info.formats if not f.audio_only and not f.thumbnail]
            bits.append("no ffmpeg: only audio available" if not video_choices else "without ffmpeg: up to 360p only")
            self.ffmpeg_btn.show()
        self.card_sub.setText("  ·  ".join(bits))
        self.quality.blockSignals(True)
        self.quality.clear()
        for f in info.formats:
            self.quality.addItem(f.label, f)
        self.quality.blockSignals(False)
        self.quality.setCurrentIndex(self._preferred_index(info))
        self._on_quality_changed(self.quality.currentIndex())
        self.use_category.setText("Sort into the 'Video' sub-folder automatically")
        if info.thumbnail:
            self._thumb_bridge.attach(seq, self.manager.fetch_bytes(info.thumbnail))
        self._show_rows()
        self._set_ready(True)

    def _preferred_index(self, info: MediaInfo) -> int:
        """Pick the format closest to (but not above) the quality the user chose last time."""
        want = self.manager.settings.preferred_height
        if not want:
            return 0
        best, best_h = 0, -1
        for i, f in enumerate(info.formats):
            if f.audio_only or f.thumbnail or not f.height:
                continue
            if f.height <= want and f.height > best_h:
                best, best_h = i, f.height
        return best

    def _on_thumb_done(self, seq: int, data: bytes | None, error: str | None) -> None:
        if seq != self._seq or not data or self.batch_mode:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            self.thumb.setPixmap(
                pm.scaled(112, 63, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
            self.card_icon.hide()
            self.thumb.show()

    def _on_quality_changed(self, _index: int) -> None:
        f = self.quality.currentData()
        if not f or not self.media_info:
            return
        self.quality_hint.setText(fmt_bytes(f.filesize) if f.filesize else "")
        name = self.name_edit.text().strip()
        base = Path(name).stem if name else self.media_info.filename_base
        self.name_edit.setText(f"{base}.{f.ext}")
        if f.thumbnail:
            self.use_category.setText("Sort into the 'Images' sub-folder automatically")
        elif f.audio_only:
            self.use_category.setText("Sort into the 'Music' sub-folder automatically")
        else:
            self.use_category.setText("Sort into the 'Video' sub-folder automatically")
        self._show_rows()

    def _request_ffmpeg(self) -> None:
        self.install_ffmpeg_requested.emit(self.url())
        self.reject()

    # ------------------------------------------------------------- helpers
    def _on_checksum_edited(self, text: str) -> None:
        from ..engine.download import parse_checksum

        try:
            parsed = parse_checksum(text)
        except ValueError:
            self.checksum_edit.setStyleSheet("border: 1px solid #d9534f;")
            self.checksum_edit.setToolTip("Not a valid digest: expected 32, 40, 64 or 128 hex digits")
            self._set_ready(False)
            return
        self.checksum_edit.setStyleSheet("")
        self.checksum_edit.setToolTip(f"Will verify with {parsed[0].upper()}" if parsed else "")
        if self.probe_result is not None and self.source_kind == "file":
            self._set_ready(True)

    def checksum(self) -> str:
        return self.checksum_edit.text().strip() if self.checksum_edit.isVisibleTo(self) else ""

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
            self.use_category.setChecked(False)

    def _download_later(self) -> None:
        self.start_now = False
        self.accept()

    # -------------------------------------------------------------- result
    def dest_dir(self) -> str | None:
        """None means 'let settings decide' (category sub-folder); a path overrides it."""
        folder = self.folder_edit.text().strip()
        settings = self.manager.settings
        if self.use_category.isChecked() and Path(folder) == Path(settings.download_dir):
            return None
        return folder or None

    def filename(self) -> str | None:
        return self.name_edit.text().strip() or None

    def connections(self) -> int:
        return self.conn_spin.value()

    def url(self) -> str:
        return self.url_edit.text().strip().strip('"')

    def selected_format(self):
        return self.quality.currentData()

    def submit(self) -> list[concurrent.futures.Future]:
        """Hand the configured download(s) to the manager. Returns a Future per download added
        (a Future[DownloadTask], or Future[list[DownloadTask]] for a MEGA folder)."""
        mgr = self.manager
        if self.batch_mode:
            return [
                mgr.add_smart(
                    u, dest_dir=self.dest_dir(), headers=self.headers, referrer=self.referrer, start=self.start_now
                )
                for u in self.batch_urls()
            ]
        if self.source_kind in ("magnet", "torrent"):
            return [mgr.add_torrent(self.url(), dest_dir=self.dest_dir(), start=self.start_now, headers=self.headers)]
        if self.source_kind == "mega":
            single = self.mega_files is not None and len(self.mega_files) == 1
            return [
                mgr.add_mega(
                    self.url(),
                    dest_dir=self.dest_dir(),
                    filename=self.filename() if single else None,
                    start=self.start_now,
                    files=self.mega_files,
                )
            ]
        if self.media_info is not None:
            info = self.media_info
            fmt = self.selected_format()
            if fmt and fmt.thumbnail:
                return [
                    mgr.add(
                        info.thumbnail or "",
                        dest_dir=self.dest_dir(),
                        filename=self.filename(),
                        connections=1,
                        start=self.start_now,
                        referrer=info.url,
                    )
                ]
            if fmt and fmt.height and not fmt.audio_only:
                from dataclasses import replace

                mgr.update_settings(replace(mgr.settings, preferred_height=fmt.height))
            futures = [
                mgr.add_media(
                    info,
                    fmt.selector if fmt else "best",
                    dest_dir=self.dest_dir(),
                    filename=self.filename(),
                    headers=self.headers,
                    start=self.start_now,
                    ext=fmt.ext if fmt else "mp4",
                    client=fmt.client if fmt else "",
                    postprocess=fmt.postprocess if fmt else "",
                    thumbnail=info.thumbnail or "",
                )
            ]
            if self.save_thumb.isVisibleTo(self) and self.save_thumb.isChecked() and info.thumbnail:
                base = Path(self.filename() or info.filename_base).stem
                futures.append(
                    mgr.add(
                        info.thumbnail,
                        dest_dir=self.dest_dir(),
                        filename=f"{base}.{thumbnail_ext(info.thumbnail)}",
                        connections=1,
                        start=self.start_now,
                        referrer=info.url,
                    )
                )
            return futures
        return [
            mgr.add(
                self.url(),
                dest_dir=self.dest_dir(),
                filename=self.filename(),
                connections=self.connections(),
                headers=self.headers,
                start=self.start_now,
                probe_result=self.probe_result,
                referrer=self.referrer,
                username=self.user_edit.text().strip() if self.needs_auth else "",
                password=self.pass_edit.text() if self.needs_auth else "",
                remember_login=self.remember_login.isChecked(),
                allow_duplicate=True,  # the card warned and the button said 'Download anyway'
                checksum=self.checksum(),
            )
        ]
