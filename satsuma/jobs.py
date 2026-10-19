"""Runs options: optional dialog on the GUI thread, then work on a thread pool."""
from __future__ import annotations

import itertools
import logging
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from . import paths as pathmod
from .config import settings
from .i18n import tr
from .registry import Option, UserError

_ids = itertools.count(1)


class JobContext:
    """Handed to every option's `dialog` and `run`."""

    def __init__(self, job: "Job"):
        self._job = job
        self.params: dict = {}
        self.settings = settings
        self._tmp: tempfile.TemporaryDirectory | None = None

    @property
    def cancelled(self) -> bool:
        return self._job.cancelled

    def check_cancel(self) -> None:
        if self._job.cancelled:
            raise UserError(tr("Cancelled"))

    def progress(self, fraction: float | None = None, message: str | None = None) -> None:
        """Report progress (0..1 of the current file) and/or a status line."""
        self._job.report(fraction, message)

    def output_path(self, src: Path, ext: str | None = None, suffix: str = "",
                    name: str | None = None, folder: Path | None = None) -> Path:
        return pathmod.output_path(src, ext, suffix, name, folder)

    @property
    def tmpdir(self) -> Path:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="satsuma-")
        return Path(self._tmp.name)

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


class Job(QObject):
    progressed = Signal(object)  # self
    finished = Signal(object)    # self

    def __init__(self, option: Option, paths: list[Path]):
        super().__init__()
        self.id = next(_ids)
        self.option = option
        self.paths = paths
        self.ctx = JobContext(self)
        self.cancelled = False
        self.fraction = 0.0      # overall 0..1
        self.message = ""
        self.outputs: list[Path] = []
        self.error: str | None = None
        self._index = 0
        self._count = 1

    @property
    def title(self) -> str:
        what = self.paths[0].name if len(self.paths) == 1 else tr("{n} items").format(n=len(self.paths))
        if self.option.mode == "convert":
            return tr("Convert to {fmt}").format(fmt=self.option.title) + f" · {what}"
        return f"{self.option.title} · {what}"

    def cancel(self) -> None:
        self.cancelled = True

    def report(self, fraction: float | None, message: str | None) -> None:
        if fraction is not None:
            frac = max(0.0, min(1.0, fraction))
            self.fraction = (self._index + frac) / self._count
        if message is not None:
            self.message = message
        self.progressed.emit(self)

    def execute(self) -> None:
        opt = self.option
        try:
            if opt.multi:
                self._count = 1
                self._collect(opt.run(self.paths, self.ctx))
            else:
                self._count = len(self.paths)
                for i, p in enumerate(self.paths):
                    self.ctx.check_cancel()
                    self._index = i
                    if self._count > 1:
                        self.report(0.0, f"{i + 1}/{self._count} · {p.name}")
                    self._collect(opt.run(p, self.ctx))
            self.fraction = 1.0
        except UserError as e:
            self.error = str(e)
        except Exception as e:
            logging.error("job %s failed:\n%s", opt.id, traceback.format_exc())
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.ctx.cleanup()
        self.finished.emit(self)

    def _collect(self, result) -> None:
        if result is None:
            return
        if isinstance(result, (list, tuple)):
            self.outputs.extend(Path(r) for r in result)
        else:
            self.outputs.append(Path(result))


class _Runnable(QRunnable):
    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def run(self) -> None:
        self.job.execute()


class JobManager(QObject):
    job_started = Signal(object)
    job_progressed = Signal(object)
    job_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(3)
        self.jobs: list[Job] = []

    def submit(self, option: Option, paths: list[Path]) -> Job | None:
        job = Job(option, paths)
        if option.dialog is not None:
            try:
                params = option.dialog(paths, job.ctx)
            except UserError as e:
                job.error = str(e)
                self._emit_done(job)
                return job
            except Exception as e:
                logging.error("dialog %s failed:\n%s", option.id, traceback.format_exc())
                job.error = f"{type(e).__name__}: {e}"
                self._emit_done(job)
                return job
            if params is None:
                return None
            job.ctx.params = params
            if params.get("done") or option.run is None:
                job._collect(params.get("outputs"))
                job.message = params.get("message", "")
                job.fraction = 1.0
                # Silent actions (e.g. opening a terminal) show no toast.
                if job.outputs or job.message or option.mode != "actions":
                    self._emit_done(job)
                return job
        if option.run is None:
            return None
        job.progressed.connect(self.job_progressed)
        job.finished.connect(self._on_finished)
        self.jobs.append(job)
        self.job_started.emit(job)
        self.pool.start(_Runnable(job))
        return job

    def _emit_done(self, job: Job) -> None:
        self.jobs.append(job)
        self.job_started.emit(job)
        self.job_finished.emit(job)

    def _on_finished(self, job: Job) -> None:
        self.job_finished.emit(job)
