"""First-run window explaining the drag-and-tap gesture."""
from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import registry
from ..config import ACCENT, APP_NAME, KEY_LABELS, MODE_TITLES, settings
from ..i18n import tr
from .common import app_icon

_dialog: QDialog | None = None


def show_welcome() -> None:
    global _dialog
    if _dialog is None:
        _dialog = _build()
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()


def _build() -> QDialog:
    d = QDialog(None, Qt.WindowStaysOnTopHint)
    d.setWindowTitle(tr("Welcome to {app}").format(app=APP_NAME))
    d.setWindowIcon(app_icon())
    d.setMinimumWidth(480)
    v = QVBoxLayout(d)
    v.setContentsMargins(26, 22, 26, 20)
    v.setSpacing(14)
    title = QLabel(tr("The zero-click offline file converter."))
    title.setStyleSheet("font-size: 16pt; font-weight: 600;")
    title.setWordWrap(True)
    v.addWidget(title)
    v.addWidget(QLabel(tr("Everything happens locally on your PC. Nothing is uploaded."), objectName="subtitle", wordWrap=True))

    counts = Counter(o.mode for o in registry.all_options())
    for mode in ("convert", "tools", "actions"):
        key = tr(KEY_LABELS.get(settings.get(f"keys.{mode}"), "?"))
        row = QHBoxLayout()
        chip = QLabel(key)
        chip.setAlignment(Qt.AlignCenter)
        chip.setMinimumWidth(84)
        chip.setStyleSheet(f"background: {ACCENT}; color: #111; border-radius: 8px; padding: 6px; font-weight: 600;")
        row.addWidget(chip)
        count = tr("{n} options").format(n=counts.get(mode, 0))
        text = QLabel(f"<b>{tr(MODE_TITLES[mode])}</b><br><span style='color:#9a9aa6'>{count}</span>")
        row.addWidget(text, 1)
        v.addLayout(row)

    how = QLabel(
        tr("Start dragging a file or folder in Explorer, then tap a key. The wheel opens under "
           "your cursor and stays open until you drop. Drop on a wedge to run it, or in the "
           "middle to cancel. Press Esc to close.")
        + "<br><br>"
        + tr("You can also right-click files → <b>Send to → {app}</b> (enable it in Settings), "
             "or click the tray icon for a drop zone.").format(app=APP_NAME)
    )
    how.setWordWrap(True)
    how.setTextFormat(Qt.RichText)
    v.addWidget(how)
    btns = QHBoxLayout()
    btns.addStretch(1)
    ok = QPushButton(tr("Got it"), objectName="primary")
    ok.clicked.connect(d.accept)
    btns.addWidget(ok)
    v.addLayout(btns)
    return d
