"""Palette-aware theme: colors plus a Qt stylesheet. Follows the OS light/dark setting."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ..engine import Status


@dataclass(frozen=True)
class Theme:
    dark: bool
    bg: QColor
    surface: QColor
    alt: QColor
    border: QColor
    hover: QColor
    text: QColor
    muted: QColor
    accent: QColor
    accent_hover: QColor
    accent_soft: QColor
    success: QColor
    warning: QColor
    danger: QColor
    track: QColor

    def status_color(self, status: Status) -> QColor:
        return {
            Status.DOWNLOADING: self.accent,
            Status.COMPLETED: self.success,
            Status.PAUSED: self.muted,
            Status.QUEUED: self.muted,
            Status.ERROR: self.danger,
        }[status]


LIGHT = Theme(
    dark=False,
    bg=QColor("#ffffff"),
    surface=QColor("#f6f7f9"),
    alt=QColor("#fafbfc"),
    border=QColor("#e3e6ea"),
    hover=QColor("#eceff3"),
    text=QColor("#1f2328"),
    muted=QColor("#6b7280"),
    accent=QColor("#2d7dd2"),
    accent_hover=QColor("#2468b3"),
    accent_soft=QColor("#e4effa"),
    success=QColor("#2e9e5b"),
    warning=QColor("#d98b0b"),
    danger=QColor("#d64545"),
    track=QColor("#e6e8eb"),
)

DARK = Theme(
    dark=True,
    bg=QColor("#1c1f24"),
    surface=QColor("#23272e"),
    alt=QColor("#1f2227"),
    border=QColor("#30353d"),
    hover=QColor("#2c3139"),
    text=QColor("#e6e8eb"),
    muted=QColor("#9aa3af"),
    accent=QColor("#4a9eff"),
    accent_hover=QColor("#6cb0ff"),
    accent_soft=QColor("#273a52"),
    success=QColor("#3fbf73"),
    warning=QColor("#e6a23c"),
    danger=QColor("#ef6464"),
    track=QColor("#343941"),
)


def detect_theme(app: QApplication | None = None, mode: str = "auto") -> Theme:
    if mode == "light":
        return LIGHT
    if mode == "dark":
        return DARK
    app = app or QApplication.instance()
    if app is None:
        return LIGHT
    window = app.palette().color(QPalette.ColorRole.Window)
    return DARK if window.lightness() < 128 else LIGHT


def _c(color: QColor) -> str:
    return color.name()


def stylesheet(t: Theme) -> str:
    return f"""
    QMainWindow, QDialog, QWidget#central {{ background: {_c(t.bg)}; color: {_c(t.text)}; }}
    QWidget {{ color: {_c(t.text)}; font-size: 10pt; }}

    QMenuBar {{ background: {_c(t.surface)}; color: {_c(t.text)}; border-bottom: 1px solid {_c(t.border)}; padding: 2px 6px; }}
    QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; background: transparent; }}
    QMenuBar::item:selected {{ background: {_c(t.hover)}; }}
    QMenuBar::item:pressed {{ background: {_c(t.accent_soft)}; }}
    QToolBar {{
        background: {_c(t.surface)}; border: none; border-bottom: 1px solid {_c(t.border)};
        padding: 4px 8px; spacing: 2px;
    }}
    QToolBar::separator {{ background: {_c(t.border)}; width: 1px; margin: 8px 6px; }}
    QToolButton {{ border: none; border-radius: 6px; padding: 5px 10px; color: {_c(t.text)}; font-size: 9pt; }}
    QToolButton:hover {{ background: {_c(t.hover)}; }}
    QToolButton:pressed, QToolButton:checked {{ background: {_c(t.accent_soft)}; }}
    QToolButton:disabled {{ color: {_c(t.muted)}; }}

    QListWidget#sidebar {{
        background: {_c(t.surface)}; border: none; border-right: 1px solid {_c(t.border)};
        outline: 0; padding: 8px 6px;
    }}
    QListWidget#sidebar::item {{ border-radius: 6px; padding: 0px; color: {_c(t.text)}; }}
    QListWidget#sidebar::item:selected {{ background: {_c(t.accent_soft)}; }}
    QListWidget#sidebar::item:hover:!selected {{ background: {_c(t.hover)}; }}
    QWidget#sidebarColumn {{ background: {_c(t.surface)}; border-right: 1px solid {_c(t.border)}; }}
    QPushButton#sidebarButton {{
        border: none; border-top: 1px solid {_c(t.border)}; border-radius: 0; background: {_c(t.surface)};
        text-align: left; padding: 10px 14px; color: {_c(t.accent)};
    }}
    QPushButton#sidebarButton:hover {{ background: {_c(t.hover)}; }}
    QPushButton#detailsButton {{ padding: 4px 10px; font-size: 9pt; }}
    QLineEdit#search {{ padding: 5px 10px; min-width: 180px; max-width: 260px; border-radius: 14px; }}
    QComboBox {{
        border: 1px solid {_c(t.border)}; border-radius: 6px; padding: 5px 8px; background: {_c(t.surface)};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {_c(t.surface)}; border: 1px solid {_c(t.border)}; selection-background-color: {_c(t.accent_soft)};
        selection-color: {_c(t.text)}; outline: 0;
    }}
    QComboBox#whenDone {{ border: none; background: transparent; color: {_c(t.muted)}; padding: 2px 6px; }}
    QFrame#setupCard {{ background: {_c(t.surface)}; border: 1px solid {_c(t.border)}; border-radius: 10px; }}
    QLabel#ok {{ color: {_c(t.success)}; }}
    QLabel#warn {{ color: {_c(t.warning)}; }}

    QTableView {{
        background: {_c(t.bg)}; alternate-background-color: {_c(t.alt)}; border: none; outline: 0;
        selection-background-color: {_c(t.accent_soft)}; selection-color: {_c(t.text)};
    }}
    QTableView::item {{ border: none; padding: 0 8px; }}
    QTableView::item:selected {{ background: {_c(t.accent_soft)}; color: {_c(t.text)}; }}
    QHeaderView::section {{
        background: {_c(t.bg)}; color: {_c(t.muted)}; border: none;
        border-bottom: 1px solid {_c(t.border)}; padding: 7px 8px; font-weight: 600; font-size: 9pt;
    }}
    QHeaderView {{ background: {_c(t.bg)}; }}
    QTableCornerButton::section {{ background: {_c(t.bg)}; border: none; }}

    QStatusBar {{ background: {_c(t.surface)}; border-top: 1px solid {_c(t.border)}; color: {_c(t.muted)}; }}
    QStatusBar::item {{ border: none; }}
    QSplitter::handle {{ background: transparent; }}

    QFrame#details {{ background: {_c(t.surface)}; border-top: 1px solid {_c(t.border)}; }}
    QLabel#muted {{ color: {_c(t.muted)}; }}
    QLabel#title {{ font-size: 11pt; font-weight: 600; }}
    QLabel#emptyTitle {{ color: {_c(t.muted)}; font-size: 14pt; }}
    QLabel#emptyHint {{ color: {_c(t.muted)}; font-size: 10pt; }}
    QLabel#error {{ color: {_c(t.danger)}; }}
    QLabel#link {{ color: {_c(t.accent)}; }}
    QFrame#card {{ background: {_c(t.surface)}; border: 1px solid {_c(t.border)}; border-radius: 8px; }}

    QLineEdit, QSpinBox, QPlainTextEdit {{
        border: 1px solid {_c(t.border)}; border-radius: 6px; padding: 6px 8px;
        background: {_c(t.surface)}; selection-background-color: {_c(t.accent)}; selection-color: white;
    }}
    QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {_c(t.accent)}; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; }}

    QPushButton {{
        border: 1px solid {_c(t.border)}; border-radius: 6px; padding: 6px 14px;
        background: {_c(t.surface)}; min-height: 16px;
    }}
    QPushButton:hover {{ background: {_c(t.hover)}; }}
    QPushButton:pressed {{ background: {_c(t.border)}; }}
    QPushButton:checked {{ background: {_c(t.accent_soft)}; border-color: {_c(t.accent)}; color: {_c(t.accent)}; }}
    QPushButton#primary {{ background: {_c(t.accent)}; color: white; border-color: {_c(t.accent)}; }}
    QPushButton#primary:hover {{ background: {_c(t.accent_hover)}; }}
    QPushButton:disabled {{ color: {_c(t.muted)}; background: {_c(t.surface)}; }}
    QPushButton#primary:disabled {{ background: {_c(t.track)}; border-color: {_c(t.track)}; color: {_c(t.muted)}; }}

    QTabWidget::pane {{ border: 1px solid {_c(t.border)}; border-radius: 6px; top: -1px; background: {_c(t.bg)}; }}
    QTabBar::tab {{
        padding: 7px 16px; border: none; border-bottom: 2px solid transparent;
        color: {_c(t.muted)}; background: transparent;
    }}
    QTabBar::tab:selected {{ color: {_c(t.accent)}; border-bottom-color: {_c(t.accent)}; }}
    QTabBar::tab:hover:!selected {{ color: {_c(t.text)}; }}

    QMenu {{ background: {_c(t.surface)}; border: 1px solid {_c(t.border)}; padding: 4px; border-radius: 6px; }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {_c(t.accent_soft)}; }}
    QMenu::item:disabled {{ color: {_c(t.muted)}; }}
    QMenu::separator {{ height: 1px; background: {_c(t.border)}; margin: 4px 8px; }}

    QCheckBox {{ spacing: 8px; }}
    QToolTip {{ background: {_c(t.surface)}; color: {_c(t.text)}; border: 1px solid {_c(t.border)}; padding: 4px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {_c(t.track)}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {_c(t.muted)}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {_c(t.track)}; border-radius: 5px; min-width: 30px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    """


def apply_theme(app: QApplication, mode: str = "auto") -> Theme:
    theme = detect_theme(app, mode)
    app.setStyleSheet(stylesheet(theme))
    return theme
