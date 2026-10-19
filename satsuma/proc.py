"""Run helper processes without flashing console windows."""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from typing import Callable

from .i18n import tr
from .registry import UserError

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run(args: list[str], ctx=None, *, on_line: Callable[[str], None] | None = None,
        cwd: str | None = None, check: bool = True) -> str:
    """Run `args`, returning combined stdout+stderr text.

    `on_line` receives every output line as it arrives (ffmpeg progress etc.).
    If `ctx.cancelled` becomes true the process is killed and UserError raised.
    """
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=cwd, creationflags=CREATE_NO_WINDOW, text=True, encoding="utf-8", errors="replace",
    )
    lines: list[str] = []
    stop = threading.Event()

    def watch_cancel():
        while not stop.wait(0.2):
            if ctx is not None and ctx.cancelled:
                proc.kill()
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    try:
        assert proc.stdout is not None
        # ffmpeg separates progress updates with \r, so split on both.
        buf = ""
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            if chunk in "\r\n":
                if buf:
                    lines.append(buf)
                    if on_line:
                        on_line(buf)
                buf = ""
            else:
                buf += chunk
        if buf:
            lines.append(buf)
        proc.wait()
    finally:
        stop.set()
    if ctx is not None and ctx.cancelled:
        raise UserError(tr("Cancelled"))
    output = "\n".join(lines)
    if check and proc.returncode != 0:
        tail = "\n".join(lines[-6:])
        raise UserError(tr("{tool} failed:\n{details}").format(tool=_name(args[0]), details=tail))
    return output


def _name(exe: str) -> str:
    return re.split(r"[\\/]", exe)[-1]
