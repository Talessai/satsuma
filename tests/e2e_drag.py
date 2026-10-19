"""End-to-end check of the drag -> key tap -> wheel -> drop flow with real OLE drag & drop.

Takes over the mouse for a few seconds. Run manually:
    .venv\\Scripts\\python.exe tests\\e2e_drag.py [shift|ctrl|alt]

It starts Satsuma with an isolated APPDATA, opens a small window that drags a
sample JPG (like Explorer would), taps the wheel key mid-drag, drops on the
top wedge and reports which new files appeared.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QLabel

ROOT = Path(__file__).resolve().parents[1]
user32 = ctypes.windll.user32
LEFTDOWN, LEFTUP = 0x0002, 0x0004
KEYUP = 0x0002
VK = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12}


class Source(QLabel):
    def __init__(self, file: Path):
        super().__init__("drag source")
        self.file = file
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(760, 760)  # big backdrop: a failed drop lands here, not on other apps
        self.setStyleSheet("background:#333;color:#fff;")
        self.result = None

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and self.result is None:
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(str(self.file))])
            drag = QDrag(self)
            drag.setMimeData(md)
            self.result = drag.exec(Qt.CopyAction | Qt.MoveAction | Qt.LinkAction)


def move_to(x: int, y: int) -> None:
    """Absolute move that, unlike SetCursorPos, reaches low-level mouse hooks."""
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    user32.mouse_event(0x0001 | 0x8000, int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1)), 0, 0)


SHOTS = Path(os.environ.get("E2E_SHOTS", tempfile.gettempdir()))


def shot(name: str, cx: int, cy: int) -> None:
    from PIL import ImageGrab

    r = 330
    ImageGrab.grab(bbox=(cx - r, cy - r, cx + r, cy + r), all_screens=True).save(SHOTS / f"e2e-{name}.png")


def watchdog(seconds: float) -> None:
    """Never leave the mouse button stuck if something hangs."""
    time.sleep(seconds)
    user32.mouse_event(LEFTUP, 0, 0, 0, 0)
    print("watchdog: timed out")
    os._exit(2)


def drive(src: Source, key: str, log: list):
    """Synthetic input, physical pixels."""
    time.sleep(1.0)
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(int(src.winId()), ctypes.byref(rect))
    x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    scale = user32.GetDpiForWindow(int(src.winId())) / 96
    move_to(x, y)
    time.sleep(0.2)
    user32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
    for i in range(1, 8):
        time.sleep(0.03)
        move_to(x + i * 3, y)
    cx, cy = x + 21, y
    time.sleep(0.4)
    user32.keybd_event(VK[key], 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK[key], 0, KEYUP, 0)
    log.append(f"tapped {key} at {cx},{cy}")
    time.sleep(0.8)
    shot(f"after-{key}", cx, cy)
    # Move into the wedge at ANGLE degrees (0 = top, clockwise) on ring 0, mid radius 56+46 px.
    import math
    r = 102 * scale
    tx, ty = cx + int(r * math.sin(math.radians(ANGLE))), cy - int(r * math.cos(math.radians(ANGLE)))
    for i in range(1, 11):
        time.sleep(0.04)
        move_to(cx + (tx - cx) * i // 10, cy + (ty - cy) * i // 10)
    time.sleep(0.5)
    shot("before-drop", cx, cy)
    user32.mouse_event(LEFTUP, 0, 0, 0, 0)
    log.append("dropped")


ANGLE = 0.0


def main() -> int:
    global ANGLE
    key = sys.argv[1] if len(sys.argv) > 1 else "shift"
    ANGLE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    work = Path(tempfile.mkdtemp(prefix="satsuma-e2e-"))
    appdata = work / "appdata"
    appdata.mkdir()
    (appdata / "Satsuma").mkdir()
    (appdata / "Satsuma" / "settings.json").write_text('{"welcomed": true}', encoding="utf-8")
    sample = work / "files" / "photo.jpg"
    sample.parent.mkdir()
    Image.new("RGB", (320, 200), (200, 120, 40)).save(sample)

    env = dict(os.environ, APPDATA=str(appdata))
    app_proc = subprocess.Popen([sys.executable, "-m", "satsuma"], cwd=ROOT, env=env)
    time.sleep(4)

    app = QApplication([])
    src = Source(sample)
    screen = app.primaryScreen().availableGeometry()
    src.move(screen.center().x() - 380, screen.center().y() - 380)
    src.show()
    log: list[str] = []
    t = threading.Thread(target=drive, args=(src, key, log), daemon=True)
    t.start()
    threading.Thread(target=watchdog, args=(30,), daemon=True).start()
    QTimer.singleShot(12000, app.quit)
    app.exec()

    time.sleep(1)
    created = sorted(p.name for p in sample.parent.iterdir() if p != sample)
    print("log:", log)
    print("drag result:", src.result)
    print("new files:", created)
    log_file = appdata / "Satsuma" / "satsuma.log"
    if log_file.exists():
        print("--- app log ---\n" + log_file.read_text(encoding="utf-8")[-3000:])
    app_proc.terminate()
    ran = log_file.exists() and " run " in log_file.read_text(encoding="utf-8")
    return 0 if (created or ran) else 1


if __name__ == "__main__":
    raise SystemExit(main())
