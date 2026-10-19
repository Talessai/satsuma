"""Application entry: tray icon, global hook, wheels, single-instance handling.

    satsuma                -> start (or focus) the tray app
    satsuma FILE [FILE...] -> open the click wheel for those files (Send To / context menu)
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from . import __version__, about, i18n, registry
from .config import ACCENT, APP_NAME, DATA_DIR, KEY_LABELS, LOG_FILE, settings
from .hooks import DragKeyHook
from .i18n import tr
from .jobs import JobManager
from .ui.common import app_icon, apply_style
from .ui.toast import JobsPanel
from .wheel import Wheel

SERVER_NAME = f"{APP_NAME}-{Path.home().name}"


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class DropZone(QWidget):
    """A small always-on-top target: drop files on it to open the click wheel."""

    def __init__(self, on_files):
        super().__init__(None, Qt.WindowStaysOnTopHint | Qt.Tool)
        self.on_files = on_files
        self.setWindowTitle(tr("{app} drop zone").format(app=APP_NAME))
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.setMinimumSize(220, 150)
        v = QVBoxLayout(self)
        self.label = QLabel(tr("Drop files here"), alignment=Qt.AlignCenter, wordWrap=True)
        self.label.setStyleSheet(f"border: 2px dashed {ACCENT}; border-radius: 14px; color: #ddd; font-size: 11pt;")
        v.addWidget(self.label)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        if paths:
            QTimer.singleShot(0, lambda: self.on_files(paths))


class SatsumaApp(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        registry.load_all()
        self.jobs = JobManager()
        self.panel = JobsPanel(self.jobs)
        self.drag_wheel = Wheel(focusable=False)
        self.click_wheel = Wheel(focusable=True)
        for w in (self.drag_wheel, self.click_wheel):
            w.chosen.connect(self.run_option)
        self.hook = DragKeyHook()
        self.hook.key_during_drag.connect(self.drag_wheel.open_for_drag)
        self.hook.drag_ended.connect(self.drag_wheel.drag_ended)
        self.hook.escape_pressed.connect(self.drag_wheel.cancel)
        self.hook.start()
        self.drop_zone: DropZone | None = None
        self._pending: list[Path] = []
        self._pending_timer = QTimer(self, singleShot=True, interval=250, timeout=self._flush_pending)
        self._sync_autostart()
        self._build_tray()
        logging.info("started with %d options", len(registry.all_options()))

    def _sync_autostart(self) -> None:
        """Keep the Run key in step with the setting (e.g. after the exe moved)."""
        try:
            from . import integration

            want = bool(settings.get("start_with_windows"))
            if integration.is_autostart() and not want:
                # Turned on outside the app (the installer's checkbox): adopt it.
                want = True
                settings.set("start_with_windows", True)
                settings.save()
            if want:
                integration.set_autostart(True)  # rewrite so the path follows the exe
        except Exception:
            logging.exception("autostart sync failed")

    # ---- tray -------------------------------------------------------------

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(self._tooltip())
        menu = QMenu()
        self.enabled_action = QAction(tr("Wheel enabled"), menu, checkable=True,
                                      checked=bool(settings.get("enabled", True)))
        self.enabled_action.toggled.connect(self._set_enabled)
        menu.addAction(self.enabled_action)
        menu.addSeparator()
        menu.addAction(tr("Open files…"), self.pick_files)
        menu.addAction(tr("Show drop zone"), self.show_drop_zone)
        menu.addAction(tr("Show shelf"), self.show_shelf)
        menu.addSeparator()
        menu.addAction(tr("How it works"), self.show_help)
        if about.DONATE_URL:
            menu.addAction(tr("♥ Support {app}…").format(app=APP_NAME),
                           lambda: QDesktopServices.openUrl(QUrl(about.DONATE_URL)))
        menu.addAction(tr("Settings…"), self.open_settings)
        menu.addSeparator()
        menu.addAction(tr("Quit {app}").format(app=APP_NAME), self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self._menu = menu

    def _tooltip(self) -> str:
        k = {m: tr(KEY_LABELS.get(settings.get(f"keys.{m}"), "?")) for m in ("convert", "tools", "actions")}
        return f"{APP_NAME} {__version__}\n" + tr(
            "Drag a file, then tap\n{convert}: convert · {tools}: tools · {actions}: actions").format(**k)

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_drop_zone()

    def _set_enabled(self, on: bool) -> None:
        settings.set("enabled", on)
        settings.save()

    # ---- entry points -----------------------------------------------------

    def open_click_wheel(self, paths: list[Path]) -> None:
        paths = [p for p in paths if p.exists()]
        if paths:
            self.click_wheel.show_for(paths)

    def pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(None, tr("{app} — choose files").format(app=APP_NAME))
        if files:
            self.open_click_wheel([Path(f) for f in files])

    def show_drop_zone(self) -> None:
        if self.drop_zone is None:
            self.drop_zone = DropZone(self.open_click_wheel)
        self.drop_zone.show()
        self.drop_zone.raise_()
        self.drop_zone.activateWindow()

    def show_shelf(self) -> None:
        try:
            from .actions.shelf import get_shelf
        except ImportError:
            logging.exception("shelf unavailable")
            return
        get_shelf().present()

    def open_settings(self) -> None:
        try:
            from .ui.settings_dialog import open_settings
        except ImportError:
            logging.exception("settings dialog unavailable")
            return
        open_settings()
        self.tray.setToolTip(self._tooltip())
        self.enabled_action.blockSignals(True)
        self.enabled_action.setChecked(bool(settings.get("enabled", True)))
        self.enabled_action.blockSignals(False)

    def show_help(self) -> None:
        from .ui.welcome import show_welcome

        show_welcome()

    def run_option(self, option, paths: list[Path]) -> None:
        logging.info("run %s on %d item(s)", option.id, len(paths))
        self.jobs.submit(option, paths)

    def handle_message(self, args: list[str]) -> None:
        """Arguments from Send To / the context menu. Explorer may start one process per
        selected file, so collect paths arriving within a short window into one selection."""
        paths = [Path(a) for a in args if a and not a.startswith("--")]
        if not paths:
            self.show_drop_zone()
            return
        self._pending.extend(p for p in paths if p not in self._pending)
        self._pending_timer.start()

    def _flush_pending(self) -> None:
        paths, self._pending = self._pending, []
        self.open_click_wheel(paths)

    def quit(self) -> None:
        self.hook.stop()
        self.tray.hide()
        self.app.quit()


# ---- single instance ------------------------------------------------------

def _send_to_running(args: list[str]) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(400):
        return False
    # Let the running instance bring its wheel to the foreground.
    ctypes.windll.user32.AllowSetForegroundWindow(-1)
    sock.write(json.dumps(args).encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _listen(handler) -> QLocalServer:
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    def on_conn():
        conn = server.nextPendingConnection()

        def read():
            data = bytes(conn.readAll())
            try:
                handler(json.loads(data.decode("utf-8")))
            except ValueError:
                logging.warning("bad single-instance message")
            conn.disconnectFromServer()

        conn.readyRead.connect(read)

    server.newConnection.connect(on_conn)
    return server


def _wait_for_old_instance(timeout_s: float = 5.0) -> None:
    """After a restart, wait until the previous instance has released its server name."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if not sock.waitForConnected(100):
            return
        sock.abort()
        time.sleep(0.15)


def apply_language(app: QApplication) -> str:
    """Load the UI language from settings and mirror the layout for right-to-left scripts."""
    code = i18n.init_from_settings()
    app.setLayoutDirection(Qt.RightToLeft if i18n.is_rtl() else Qt.LeftToRight)
    logging.info("language: %s", code)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    if argv[:1] == ["--selftest"] and len(argv) > 1:
        from .selftest import run as selftest

        return selftest(Path(argv[1]))
    if "--restart" in argv:
        argv.remove("--restart")
        _wait_for_old_instance()
    if _send_to_running(argv):
        return 0
    setup_logging()
    apply_language(app)
    apply_style(app)
    app.setWindowIcon(app_icon())
    sat = SatsumaApp(app)
    server = _listen(sat.handle_message)  # noqa: F841  (kept alive for the app's lifetime)
    if argv:
        QTimer.singleShot(200, lambda: sat.handle_message(argv))
    elif not settings.get("welcomed"):
        settings.set("welcomed", True)
        settings.save()
        QTimer.singleShot(300, sat.show_help)
    else:
        key = tr(KEY_LABELS.get(settings.get("keys.convert"), "Shift"))
        sat.tray.showMessage(APP_NAME, tr("Running in the tray. Drag a file and tap {key}.").format(key=key),
                             app_icon(), 2500)
    return app.exec()
