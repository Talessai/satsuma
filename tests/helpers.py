"""Test helpers: run an option's `run` without the GUI or job manager."""
from __future__ import annotations

import tempfile
from pathlib import Path

from satsuma import registry


class FakeCtx:
    """Mimics jobs.JobContext closely enough for converters and tools."""

    def __init__(self, params: dict | None = None):
        from satsuma.config import settings

        self.params = params or {}
        self.settings = settings
        self.cancelled = False
        self.messages: list[str] = []
        self._tmp = tempfile.TemporaryDirectory(prefix="satsuma-test-")

    def check_cancel(self):
        if self.cancelled:
            raise registry.UserError("Cancelled")

    def progress(self, fraction=None, message=None):
        if message:
            self.messages.append(message)

    def output_path(self, src, ext=None, suffix="", name=None, folder=None):
        from satsuma.paths import output_path

        return output_path(Path(src), ext, suffix, name, folder)

    @property
    def tmpdir(self) -> Path:
        return Path(self._tmp.name)


def run_option(option_id: str, paths, params: dict | None = None) -> list[Path]:
    """Run a registered option on `paths` the same way JobManager does; return outputs."""
    registry.load_all()
    opt = registry.get(option_id)
    paths = [Path(p) for p in paths]
    assert opt.applies(paths), f"{option_id} does not apply to {paths}"
    ctx = FakeCtx(params)
    outs: list[Path] = []
    results = [opt.run(paths, ctx)] if opt.multi else [opt.run(p, ctx) for p in paths]
    for r in results:
        if r is None:
            continue
        outs.extend(Path(x) for x in (r if isinstance(r, (list, tuple)) else [r]))
    for o in outs:
        assert o.exists(), f"{option_id} reported missing output {o}"
    return outs
