"""Bottom-right panel listing running and finished jobs."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..config import ACCENT, APP_NAME, settings
from ..i18n import tr
from ..jobs import Job, JobManager

HIDE_AFTER_MS = 6000


def reveal(path: Path) -> None:
    """Open Explorer with `path` selected."""
    subprocess.Popen(["explorer.exe", "/select,", str(path)])


class JobRow(QFrame):
    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self.setObjectName("row")
        self.setStyleSheet("#row { background: #24242a; border-radius: 10px; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        top = QHBoxLayout()
        self.title = QLabel(job.title)
        self.title.setStyleSheet("font-weight: 600; background: transparent;")
        top.addWidget(self.title, 1)
        self.running = True
        self.btn = QPushButton(tr("Cancel"))
        self.btn.setStyleSheet("padding: 2px 10px;")
        self.btn.clicked.connect(self._on_button)
        top.addWidget(self.btn)
        v.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        v.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #9a9aa6; background: transparent;")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.update_from_job()

    def update_from_job(self) -> None:
        j = self.job
        self.bar.setValue(int(j.fraction * 1000))
        self.status.setText(j.message)

    def mark_done(self) -> None:
        j = self.job
        self.running = False
        self.bar.hide()
        if j.error:
            self.status.setText(j.error)
            self.status.setStyleSheet("color: #ff6b6b; background: transparent;")
            self.btn.setText(tr("Dismiss"))
        elif j.outputs:
            n = len(j.outputs)
            self.status.setText(j.message or (j.outputs[0].name if n == 1
                                              else tr("{n} files created").format(n=n)))
            self.btn.setText(tr("Show"))
        else:
            self.status.setText(j.message or tr("Done"))
            self.btn.setText(tr("Dismiss"))

    def _on_button(self) -> None:
        if self.running:
            self.job.cancel()
            self.btn.setEnabled(False)
        elif self.job.outputs and not self.job.error:
            reveal(self.job.outputs[0])
            self.deleteLater()
        else:
            self.deleteLater()


class JobsPanel(QWidget):
    def __init__(self, manager: JobManager):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(360)
        self.setStyleSheet("background: #1c1c20; color: #ececf1;")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setStyleSheet(f"color: {ACCENT}; font-weight: 700;")
        head.addWidget(title, 1)
        close = QPushButton("✕")
        close.setFixedSize(24, 24)
        close.setStyleSheet("padding: 0; border: none; background: transparent;")
        close.clicked.connect(self.hide)
        head.addWidget(close)
        outer.addLayout(head)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(8)
        outer.addLayout(self.rows_box)
        self.rows: dict[int, JobRow] = {}
        self._hide_timer = QTimer(self, singleShot=True, interval=HIDE_AFTER_MS, timeout=self._auto_hide)
        manager.job_started.connect(self.on_started)
        manager.job_progressed.connect(self.on_progress)
        manager.job_finished.connect(self.on_finished)

    def on_started(self, job: Job) -> None:
        row = JobRow(job)
        row.destroyed.connect(lambda *_: self._row_gone(job.id))
        self.rows[job.id] = row
        self.rows_box.addWidget(row)
        # Keep the panel short: drop the oldest finished rows.
        finished = [r for r in self.rows.values() if not r.running]
        if len(self.rows) > 5 and finished:
            finished[0].deleteLater()
        self._hide_timer.stop()
        self._relayout()
        self.show()

    def on_progress(self, job: Job) -> None:
        row = self.rows.get(job.id)
        if row:
            row.update_from_job()

    def on_finished(self, job: Job) -> None:
        row = self.rows.get(job.id)
        if row:
            row.mark_done()
        if not job.error and job.outputs and settings.get("reveal_after"):
            reveal(job.outputs[0])
        if not any(r.running for r in self.rows.values()):
            self._hide_timer.start()

    def _row_gone(self, job_id: int) -> None:
        self.rows.pop(job_id, None)
        if not self.rows:
            self.hide()
        else:
            QTimer.singleShot(0, self._relayout)

    def _auto_hide(self) -> None:
        if self.underMouse():
            self._hide_timer.start()
            return
        for row in list(self.rows.values()):
            if not row.running:
                row.deleteLater()

    def _relayout(self) -> None:
        self.adjustSize()
        screen = QGuiApplication.primaryScreen().availableGeometry()
        # Bottom corner on the reading-end side: right for LTR, left for RTL languages.
        x = screen.left() + 16 if self.layoutDirection() == Qt.RightToLeft else screen.right() - self.width() - 16
        self.move(QPoint(x, screen.bottom() - self.height() - 16))

    def enterEvent(self, e) -> None:
        self._hide_timer.stop()

    def leaveEvent(self, e) -> None:
        if self.rows and not any(r.running for r in self.rows.values()):
            self._hide_timer.start()
