"""The 'Add download' dialog.

Handles three kinds of input:
  * a plain file link (HTTP download with segments),
  * a web page with a video/audio stream (YouTube etc.), analysed with yt-dlp and offered
    with a quality picker and thumbnail,
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

from ..engine import DownloadManager, MediaInfo, ProbeResult
from ..engine.media import looks_like_media_page
from .icons import CATEGORY_ICONS, icon
from .theme import Theme
from .util import fmt_bytes, fmt_eta, looks_like_url


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
        self.kind_hint = kind  # "auto" | "file" | "media" | "download"
        self.referrer = referrer
        self.probe_result: ProbeResult | None = None
        self.media_info: MediaInfo | None = None
        self.start_now = True
        self.batch_mode = False
        self._seq = 0
        self._probe_bridge = _FutureBridge(self)
        self._probe_bridge.finished.connect(self._on_probe_done)
        self._media_bridge = _FutureBridge(self)
        self._media_bridge.finished.connect(self._on_media_done)
        self._thumb_bridge = _FutureBridge(self)
        self._thumb_bridge.finished.connect(self._on_thumb_done)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(450)
        self._debounce.timeout.connect(self._probe)

        self.setWindowTitle("Add download")
        self.setMinimumWidth(640)

        # --- URL row (single) and batch editor (many)
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://example.com/file.zip  or  a YouTube / video page link")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.textChanged.connect(self._on_url_edited)
        paste = QPushButton(icon("paste", theme.text, 18), "")
        paste.setToolTip("Paste from clipboard")
        paste.setFixedWidth(36)
        paste.setAutoDefault(False)
        paste.clicked.connect(self._paste)
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
        url_row.addWidget(self.many_btn)

        self.batch_edit = QPlainTextEdit()
        self.batch_edit.setPlaceholderText("One link per line. Video pages and files are detected automatically.")
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

        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form.setVerticalSpacing(10)
        self.quality_label = QLabel("Quality")
        self.form.addRow(self.quality_label, self.quality_row)
        self.name_label = QLabel("Save as")
        self.form.addRow(self.name_label, self.name_edit)
        self.form.addRow("Folder", folder_row)
        self.form.addRow("", self.use_category)
        self.conn_label = QLabel("Connections")
        self.form.addRow(self.conn_label, self.conn_row)
        self._show_media_rows(False)

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

    def _show_media_rows(self, media: bool) -> None:
        self.quality_label.setVisible(media)
        self.quality_row.setVisible(media)
        self.conn_label.setVisible(not media and not self.batch_mode)
        self.conn_row.setVisible(not media and not self.batch_mode)

    def _set_card_icon(self, name: str, color) -> None:
        self.card_icon.setPixmap(icon(name, color, 36).pixmap(36, 36))
        self.card_icon.show()
        self.thumb.hide()

    def _set_ready(self, ready: bool) -> None:
        self.start_btn.setEnabled(ready)
        self.later_btn.setEnabled(ready)

    def _paste(self) -> None:
        text = QApplication.clipboard().text().strip()
        if "\n" in text and not self.batch_mode:
            self.many_btn.setChecked(True)
        if self.batch_mode:
            self.batch_edit.setPlainText(text)
        else:
            self.url_edit.setText(text)

    def _on_url_edited(self, _text: str) -> None:
        self.kind_hint = "auto"  # the user typed something new; forget the browser's hint
        self._debounce.start()

    # --------------------------------------------------------------- batch
    def _set_batch_mode(self, on: bool) -> None:
        self.batch_mode = on
        self.url_edit.setVisible(not on)
        self.batch_label.setVisible(on)
        self.batch_edit.setVisible(on)
        self.name_label.setVisible(not on)
        self.name_edit.setVisible(not on)
        self.setWindowTitle("Add many downloads" if on else "Add download")
        self.start_btn.setText("Start all" if on else "Start download")
        self.later_btn.setText("Add all paused" if on else "Add paused")
        self._show_media_rows(False)
        self.ffmpeg_btn.hide()
        if on:
            self.media_info = None
            self.probe_result = None
            if self.url_edit.text().strip() and not self.batch_edit.toPlainText().strip():
                self.batch_edit.setPlainText(self.url_edit.text().strip())
            self._on_batch_edited()
            self.batch_edit.setFocus()
        else:
            self._probe()

    def batch_urls(self) -> list[str]:
        return [ln.strip() for ln in self.batch_edit.toPlainText().splitlines() if looks_like_url(ln.strip())]

    def _on_batch_edited(self) -> None:
        urls = self.batch_urls()
        bad = sum(
            1 for ln in self.batch_edit.toPlainText().splitlines() if ln.strip() and not looks_like_url(ln.strip())
        )
        self.card_title.setText(
            f"{len(urls)} link{'s' if len(urls) != 1 else ''} ready" if urls else "Paste links, one per line"
        )
        self.card_sub.setText(
            "Each link is checked when it starts: pages go through yt-dlp at the best quality, files use segments."
            + (f"  {bad} line(s) are not links and will be skipped." if bad else "")
        )
        self._set_card_icon("list", self.theme.accent if urls else self.theme.muted)
        self._set_ready(bool(urls))

    # ------------------------------------------------------------- probing
    def _probe(self) -> None:
        if self.batch_mode:
            return
        url = self.url_edit.text().strip()
        self.probe_result = None
        self.media_info = None
        self._show_media_rows(False)
        self.ffmpeg_btn.hide()
        if not looks_like_url(url):
            self.card_title.setText("Enter a URL to check it")
            self.card_sub.setText("Direct file links, or video pages such as YouTube, Vimeo, Twitter, TikTok.")
            self._set_card_icon("file", self.theme.muted)
            self._set_ready(False)
            return
        self._seq += 1
        self.card_title.setText("Checking link…")
        self.card_sub.setText(url)
        self._set_card_icon("clock", self.theme.muted)
        self._set_ready(False)
        if self.kind_hint == "media":
            self._start_media_analysis(url)
            return
        self._probe_bridge.attach(self._seq, self.manager.probe(url, self.headers or None))

    def _on_probe_done(self, seq: int, result: ProbeResult | None, error: str | None) -> None:
        if seq != self._seq or self.batch_mode:
            return
        url = self.url_edit.text().strip()
        if error or result is None:
            if looks_like_media_page(url, "") and self.kind_hint != "file":
                self._start_media_analysis(url)
                return
            self.card_title.setText("Could not reach this link")
            self.card_sub.setText(error or "unknown error")
            self._set_card_icon("warning", self.theme.danger)
            self._set_ready(False)
            return
        if self.kind_hint not in ("file", "download") and looks_like_media_page(result.final_url, result.content_type):
            self._start_media_analysis(url)
            return
        self.probe_result = result
        size = fmt_bytes(result.size) if result.size else "unknown size"
        mode = "multi-connection, resumable" if result.resumable else "single connection, cannot resume"
        ctype = result.content_type.split(";")[0] if result.content_type else ""
        self.card_title.setText(result.filename)
        self.card_sub.setText("  ·  ".join(x for x in (size, mode, ctype) if x))
        cat = self.manager.settings.category_for(result.filename)
        self._set_card_icon(CATEGORY_ICONS.get(cat or "", "file"), self.theme.accent)
        if not self.name_edit.text():
            self.name_edit.setText(result.filename)
        if cat:
            self.use_category.setText(f"Sort into the '{cat}' sub-folder automatically")
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
        self._show_media_rows(True)
        self._set_card_icon("video", self.theme.accent)
        self.card_title.setText(info.title)
        bits = [info.extractor]
        if info.duration:
            bits.append(fmt_eta(info.duration))
        if not self.manager.ffmpeg:
            video_choices = [f for f in info.formats if not f.audio_only]
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
        self._set_ready(True)

    def _preferred_index(self, info: MediaInfo) -> int:
        """Pick the format closest to (but not above) the quality the user chose last time."""
        want = self.manager.settings.preferred_height
        if not want:
            return 0
        best, best_h = 0, -1
        for i, f in enumerate(info.formats):
            if f.audio_only or not f.height:
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

    def _request_ffmpeg(self) -> None:
        self.install_ffmpeg_requested.emit(self.url())
        self.reject()

    # ------------------------------------------------------------- helpers
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
        return self.url_edit.text().strip()

    def selected_format(self):
        return self.quality.currentData()

    def submit(self) -> list[concurrent.futures.Future]:
        """Hand the configured download(s) to the manager. Returns a Future[DownloadTask] per link."""
        if self.batch_mode:
            return [
                self.manager.add_smart(
                    u, dest_dir=self.dest_dir(), headers=self.headers, referrer=self.referrer, start=self.start_now
                )
                for u in self.batch_urls()
            ]
        if self.media_info is not None:
            fmt = self.selected_format()
            if fmt and fmt.height and not fmt.audio_only:
                from dataclasses import replace

                self.manager.update_settings(replace(self.manager.settings, preferred_height=fmt.height))
            return [
                self.manager.add_media(
                    self.media_info,
                    fmt.selector if fmt else "best",
                    dest_dir=self.dest_dir(),
                    filename=self.filename(),
                    headers=self.headers,
                    start=self.start_now,
                    ext=fmt.ext if fmt else "mp4",
                    client=fmt.client if fmt else "",
                )
            ]
        return [
            self.manager.add(
                self.url(),
                dest_dir=self.dest_dir(),
                filename=self.filename(),
                connections=self.connections(),
                headers=self.headers,
                start=self.start_now,
                probe_result=self.probe_result,
                referrer=self.referrer,
            )
        ]
