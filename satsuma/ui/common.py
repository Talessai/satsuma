"""Shared look and building blocks for every Satsuma window.

Tool dialogs subclass `ToolDialog`:

    class CompressDialog(ToolDialog):
        def __init__(self, paths):
            super().__init__("Compress images", primary="Compress")
            self.quality = self.add_slider("Quality", 10, 100, 80, suffix="%")
            ...
        def values(self) -> dict:
            return {"quality": self.quality.value()}

    def compress_dialog(paths, ctx):
        dlg = CompressDialog(paths)
        return dlg.values() if dlg.exec() else None
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from ..config import ACCENT, APP_NAME
from ..i18n import tr

STYLE = f"""
QWidget {{ background: #1c1c20; color: #ececf1; font-family: "Segoe UI", "Microsoft YaHei UI", "Nirmala UI"; font-size: 10pt; }}
QDialog, QMainWindow {{ background: #1c1c20; }}
QLabel#title {{ font-size: 14pt; font-weight: 600; }}
QLabel#subtitle {{ color: #9a9aa6; }}
QLabel#section {{ color: #9a9aa6; font-size: 8.5pt; font-weight: 600; text-transform: uppercase; }}
QPushButton {{ background: #2c2c33; border: 1px solid #3a3a43; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ background: #34343c; }}
QPushButton:disabled {{ color: #6b6b75; }}
QPushButton#primary {{ background: {ACCENT}; color: #111; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: #ff9d45; }}
QPushButton:checked {{ background: {ACCENT}; color: #111; border: none; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QListWidget, QTimeEdit {{
    background: #26262c; border: 1px solid #3a3a43; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {ACCENT}; selection-color: #111; }}
QComboBox QAbstractItemView {{ background: #26262c; selection-background-color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: #3a3a43; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #fff; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QTabBar::tab {{ background: #26262c; padding: 6px 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabBar::tab:selected {{ background: {ACCENT}; color: #111; }}
QTabWidget::pane {{ border: 1px solid #3a3a43; border-radius: 6px; }}
QScrollArea {{ border: none; }}
QProgressBar {{ background: #26262c; border: none; border-radius: 3px; height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QToolTip {{ background: #2c2c33; color: #ececf1; border: 1px solid #3a3a43; }}
"""


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)


def app_icon() -> QIcon:
    """A simple orange disc icon drawn in code (no image files needed)."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT))
        m = size * 0.06
        p.drawEllipse(m, m + size * 0.06, size - 2 * m, size - 2 * m - size * 0.06)
        p.setBrush(QColor("#3fae4a"))
        p.drawEllipse(size * 0.5, 0, size * 0.32, size * 0.2)
        p.end()
        icon.addPixmap(pm)
    return icon


class ToolDialog(QDialog):
    """Dark dialog with a title, a form area and Cancel / primary buttons."""

    def __init__(self, title: str, subtitle: str = "", primary: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"{title} — {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setMinimumWidth(420)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        t = QLabel(title, objectName="title")
        outer.addWidget(t)
        if subtitle:
            outer.addWidget(QLabel(subtitle, objectName="subtitle", wordWrap=True))
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignLeft)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(10)
        self.body.addLayout(self.form)
        line = QFrame(frameShape=QFrame.HLine)
        line.setStyleSheet("color: #3a3a43;")
        outer.addWidget(line)
        self.footer = QHBoxLayout()
        self.footer.addStretch(1)
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.clicked.connect(self.reject)
        self.primary_btn = QPushButton(primary or tr("Save"), objectName="primary")
        self.primary_btn.setDefault(True)
        self.primary_btn.clicked.connect(self.accept)
        self.footer.addWidget(self.cancel_btn)
        self.footer.addWidget(self.primary_btn)
        outer.addLayout(self.footer)

    # --- convenience adders (return the widget) ---

    def add_row(self, label: str, widget: QWidget) -> QWidget:
        self.form.addRow(label, widget)
        return widget

    def add_slider(self, label: str, lo: int, hi: int, value: int, suffix: str = "") -> QSlider:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        v = QLabel(f"{value}{suffix}")
        v.setMinimumWidth(48)
        s.valueChanged.connect(lambda x: v.setText(f"{x}{suffix}"))
        h.addWidget(s, 1)
        h.addWidget(v)
        self.form.addRow(label, row)
        return s

    def add_spin(self, label: str, lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        if suffix:
            s.setSuffix(suffix)
        self.form.addRow(label, s)
        return s

    def add_double(self, label: str, lo: float, hi: float, value: float, step: float = 0.1,
                   suffix: str = "", decimals: int = 2) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setValue(value)
        if suffix:
            s.setSuffix(suffix)
        self.form.addRow(label, s)
        return s

    def add_combo(self, label: str, items: list[str] | dict[str, object], current=None) -> QComboBox:
        c = QComboBox()
        if isinstance(items, dict):
            for text, data in items.items():
                c.addItem(text, data)
        else:
            c.addItems(items)
        if current is not None:
            i = c.findData(current) if isinstance(items, dict) else c.findText(str(current))
            if i >= 0:
                c.setCurrentIndex(i)
        self.form.addRow(label, c)
        return c

    def add_text(self, label: str, value: str = "", placeholder: str = "") -> QLineEdit:
        e = QLineEdit(value)
        e.setPlaceholderText(placeholder)
        self.form.addRow(label, e)
        return e


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def parse_time(text: str) -> float:
    """'1:23.5' / '83.5' / '01:02:03' -> seconds."""
    parts = [float(p) for p in text.strip().split(":") if p != ""]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def fmt_time(secs: float) -> str:
    secs = max(0.0, secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{int(m)}:{s:05.2f}"
