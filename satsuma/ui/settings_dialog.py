"""The Settings window (tray menu > Settings…)."""
from __future__ import annotations

import logging
import shutil
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .. import __version__, about, i18n, integration, registry
from ..config import ACCENT, APP_NAME, DEFAULTS, KEY_CHOICES, KEY_LABELS, LOG_FILE, MODE_TITLES, MODES, settings
from ..i18n import N_, tr
from .common import STYLE, app_icon

log = logging.getLogger(__name__)

# setting value -> label (translated when shown)
TERMINALS = {
    "auto": N_("Automatic (Windows Terminal → PowerShell → cmd)"),
    "wt": N_("Windows Terminal"),
    "powershell": N_("PowerShell"),
    "cmd": N_("Command Prompt"),
}


def _install_dir() -> Path:
    import sys

    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]


def _open_licenses() -> None:
    """Open the bundled license notices (next to the exe, or in the source tree)."""
    for name in ("THIRD_PARTY_NOTICES.md", "LICENSE"):
        path = _install_dir() / name
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return


def _section(text: str) -> QLabel:
    return QLabel(text, objectName="section")


def _page() -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    return w, lay


def _key_label(key: str | None) -> str:
    return tr(KEY_LABELS.get(key, key or "?"))


class SettingsDialog(QDialog):
    """Dark settings window. Changes are applied with Save."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Settings — {app}").format(app=APP_NAME))
        self.setWindowIcon(app_icon())
        self.setStyleSheet(STYLE)
        self.setMinimumSize(560, 470)
        self.language_changed = False   # set by apply(): the UI language needs a restart
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        outer.addWidget(QLabel(tr("Settings"), objectName="title"))
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._build_general()
        self._build_keys()
        self._build_output()
        self._build_actions()
        self._build_about()
        footer = QHBoxLayout()
        self.status = QLabel("", objectName="subtitle", wordWrap=True)
        footer.addWidget(self.status, 1)
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton(tr("Save"), objectName="primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save_and_close)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.save_btn)
        outer.addLayout(footer)

    # ---- pages -----------------------------------------------------------------

    def _build_general(self) -> None:
        page, lay = _page()
        lay.addWidget(_section(tr("Language")))
        self.language = QComboBox()
        # "System default (English)": what Windows' UI language resolves to, in its own name.
        system = i18n.LANGUAGES.get(i18n.system_language(), "English")
        self.language.addItem(tr("System default ({name})").format(name=system), "system")
        for code, native in i18n.LANGUAGES.items():
            self.language.addItem(native, code)
        self._language0 = settings.get("language", "system") or "system"
        i = self.language.findData(self._language0)
        self.language.setCurrentIndex(max(0, i))
        row = QHBoxLayout()
        row.addWidget(self.language)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel(tr("Changing the language restarts {app}.").format(app=APP_NAME),
                             objectName="subtitle", wordWrap=True))

        lay.addWidget(_section(tr("Wheel")))
        self.enabled = QCheckBox(tr("Open the wheel when a key is tapped while dragging files"))
        self.enabled.setChecked(bool(settings.get("enabled", True)))
        lay.addWidget(self.enabled)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Wheel size")))
        self.wheel_size = QSlider(Qt.Horizontal)
        self.wheel_size.setRange(70, 150)
        self.wheel_size.setSingleStep(5)
        self.wheel_size.setValue(round(float(settings.get("wheel_size", 1.0) or 1.0) * 100))
        self.wheel_size_label = QLabel()
        self.wheel_size_label.setMinimumWidth(44)
        self.wheel_size.valueChanged.connect(lambda v: self.wheel_size_label.setText(tr("{n}%").format(n=v)))
        self.wheel_size_label.setText(tr("{n}%").format(n=self.wheel_size.value()))
        row.addWidget(self.wheel_size, 1)
        row.addWidget(self.wheel_size_label)
        lay.addLayout(row)

        lay.addWidget(_section(tr("Windows")))
        self._autostart0 = self._safe(integration.is_autostart, False)
        self.autostart = QCheckBox(tr("Start {app} when I sign in to Windows").format(app=APP_NAME))
        self.autostart.setChecked(self._autostart0)
        lay.addWidget(self.autostart)
        self._sendto0 = self._safe(integration.has_send_to, False)
        self.sendto = QCheckBox(tr("Add {app} to Explorer's \"Send to\" menu").format(app=APP_NAME))
        self.sendto.setChecked(self._sendto0)
        lay.addWidget(self.sendto)
        self._menu0 = self._safe(integration.has_context_menu, False)
        self.context_menu = QCheckBox(tr("Add \"{label}\" to the right-click menu").format(
            label=integration.menu_label()))
        self.context_menu.setChecked(self._menu0)
        lay.addWidget(self.context_menu)
        hint = QLabel(tr("Send to / right-click open the wheel for the selected files without dragging."),
                      objectName="subtitle", wordWrap=True)
        lay.addWidget(hint)
        lay.addStretch(1)
        self.tabs.addTab(page, tr("General"))

    def _build_keys(self) -> None:
        page, lay = _page()
        lay.addWidget(_section(tr("Tap while dragging to open")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.key_combos: dict[str, QComboBox] = {}
        self._key_prev: dict[str, str] = {}
        for mode in MODES:
            c = QComboBox()
            for key in KEY_CHOICES:
                c.addItem(_key_label(key), key)
            current = settings.get(f"keys.{mode}") or DEFAULTS["keys"][mode]
            i = c.findData(current)
            c.setCurrentIndex(max(0, i))
            self._key_prev[mode] = c.currentData()
            c.currentIndexChanged.connect(lambda _i, m=mode: self._key_changed(m))
            self.key_combos[mode] = c
            form.addRow(tr(MODE_TITLES[mode]), c)
        lay.addLayout(form)
        lay.addWidget(QLabel(tr("Each wheel needs its own key; picking a key that is in use swaps the two."),
                             objectName="subtitle", wordWrap=True))
        reset = QPushButton(tr("Restore defaults"))
        reset.clicked.connect(self._reset_keys)
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.tabs.addTab(page, tr("Keys"))

    def _build_output(self) -> None:
        page, lay = _page()
        lay.addWidget(_section(tr("Save converted files")))
        self.out_same = QRadioButton(tr("Next to the original file"))
        self.out_folder = QRadioButton(tr("In this folder:"))
        group = QButtonGroup(self)
        group.addButton(self.out_same)
        group.addButton(self.out_folder)
        folder_mode = settings.get("output_location") == "folder"
        (self.out_folder if folder_mode else self.out_same).setChecked(True)
        lay.addWidget(self.out_same)
        row = QHBoxLayout()
        row.addWidget(self.out_folder)
        self.folder_edit = QLineEdit(str(settings.get("output_folder") or DEFAULTS["output_folder"]))
        row.addWidget(self.folder_edit, 1)
        self.browse_btn = QPushButton(tr("Browse…"))
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)
        self.out_folder.toggled.connect(self._sync_output)
        self._sync_output()
        self.reveal_after = QCheckBox(tr("Show the result in Explorer after converting"))
        self.reveal_after.setChecked(bool(settings.get("reveal_after", False)))
        lay.addWidget(self.reveal_after)

        lay.addWidget(_section(tr("Quality")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(1, 100)
        self.jpeg_quality.setSuffix(" %")
        self.jpeg_quality.setValue(int(settings.get("jpeg_quality", 90)))
        form.addRow(tr("JPEG quality"), self.jpeg_quality)
        self.image_dpi = QSpinBox()
        self.image_dpi.setRange(36, 1200)
        self.image_dpi.setSingleStep(25)
        self.image_dpi.setSuffix(" " + tr("dpi"))
        self.image_dpi.setValue(int(settings.get("image_dpi", 300)))
        form.addRow(tr("Image resolution (PDF/slides → image)"), self.image_dpi)
        lay.addLayout(form)
        lay.addStretch(1)
        self.tabs.addTab(page, tr("Output"))

    def _build_actions(self) -> None:
        page, lay = _page()
        lay.addWidget(_section(tr("Terminal action")))
        self.terminal = QComboBox()
        for key, text in TERMINALS.items():
            self.terminal.addItem(tr(text), key)
        i = self.terminal.findData(settings.get("terminal", "auto"))
        self.terminal.setCurrentIndex(max(0, i))
        lay.addWidget(self.terminal)
        found = []
        from ..actions.file_actions import _wt_exe  # noqa: PLC0415 - optional dependency on actions

        # Product names: not translated.
        if _wt_exe():
            found.append("Windows Terminal")
        if shutil.which("pwsh.exe"):
            found.append("PowerShell 7")
        if shutil.which("powershell.exe"):
            found.append("Windows PowerShell")
        found.append("cmd")
        lay.addWidget(QLabel(tr("Installed: {list}").format(list=tr(", ").join(found)),
                             objectName="subtitle", wordWrap=True))
        lay.addWidget(_section(tr("Shelf")))
        lay.addWidget(QLabel(tr("Drag the shelf's header to a screen edge to pin it; it then collapses to a "
                                "slim tab and opens when you hover or drag files onto it."),
                             objectName="subtitle", wordWrap=True))
        lay.addStretch(1)
        self.tabs.addTab(page, tr("Actions"))

    def _build_about(self) -> None:
        page, lay = _page()
        title = QLabel(f"{APP_NAME} {__version__}", objectName="title")
        lay.addWidget(title)
        lay.addWidget(QLabel(tr("Drag files, tap a key, drop on a wedge. Everything runs locally and offline."),
                             objectName="subtitle", wordWrap=True))
        lay.addWidget(QLabel(tr("Made by {author} · {copyright} · Free and open source ({license})").format(
            author=about.AUTHOR, copyright=about.COPYRIGHT, license=about.LICENSE_ID), wordWrap=True))
        links = QHBoxLayout()
        for text, url in ((tr("Source code"), about.REPO_URL), (tr("Report a problem"), about.issues_url()),
                          (tr("Website"), about.WEBSITE_URL)):
            if url:
                b = QPushButton(text)
                b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
                links.addWidget(b)
        notices = QPushButton(tr("Licenses"))
        notices.setToolTip(tr("Licenses of Satsuma and the open-source components it includes"))
        notices.clicked.connect(_open_licenses)
        links.addWidget(notices)
        links.addStretch(1)
        lay.addLayout(links)
        if about.DONATE_URL:
            donate = QPushButton(tr("♥ Support {app} on Ko-fi").format(app=APP_NAME), objectName="primary")
            donate.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(about.DONATE_URL)))
            lay.addWidget(QLabel(tr("{app} is free. If it saves you time, a small donation keeps it going.").format(
                app=APP_NAME), objectName="subtitle", wordWrap=True))
            lay.addWidget(donate, 0, Qt.AlignLeft)
        lay.addWidget(_section(tr("Available options")))
        try:
            registry.load_all()
        except Exception:
            log.exception("loading options failed")
        counts = Counter(o.mode for o in registry.all_options())
        grid = QFormLayout()
        grid.setHorizontalSpacing(14)
        self.count_labels: dict[str, QLabel] = {}
        for mode in MODES:
            key = _key_label(settings.get(f"keys.{mode}"))
            lbl = QLabel(f"<b style='color:{ACCENT}'>{counts.get(mode, 0)}</b>  ({key})")
            self.count_labels[mode] = lbl
            grid.addRow(tr(MODE_TITLES[mode]), lbl)
        lay.addLayout(grid)
        line = QFrame(frameShape=QFrame.HLine)
        line.setStyleSheet("color: #3a3a43;")
        lay.addWidget(line)
        lay.addWidget(QLabel(tr("Settings file: {path}").format(path=settings.path), objectName="subtitle",
                             wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse))
        lay.addWidget(QLabel(tr("Log file: {path}").format(path=LOG_FILE), objectName="subtitle",
                             wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse))
        lay.addWidget(QLabel(tr("Installed in: {path}").format(path=_install_dir()), objectName="subtitle",
                             wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse))
        lay.addStretch(1)
        self.tabs.addTab(page, tr("About"))

    # ---- behaviour ----------------------------------------------------------------

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            log.exception("%s failed", getattr(fn, "__name__", fn))
            return default

    def _key_changed(self, mode: str) -> None:
        """Keep the three keys distinct: taking another wheel's key swaps them."""
        new = self.key_combos[mode].currentData()
        old = self._key_prev[mode]
        for other, combo in self.key_combos.items():
            if other != mode and combo.currentData() == new:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(old))
                combo.blockSignals(False)
                self._key_prev[other] = old
        self._key_prev[mode] = new

    def _reset_keys(self) -> None:
        for mode, combo in self.key_combos.items():
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(DEFAULTS["keys"][mode]))
            combo.blockSignals(False)
            self._key_prev[mode] = combo.currentData()

    def _sync_output(self) -> None:
        on = self.out_folder.isChecked()
        self.folder_edit.setEnabled(on)
        self.browse_btn.setEnabled(on)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Output folder"), self.folder_edit.text())
        if folder:
            self.folder_edit.setText(str(Path(folder)))
            self.out_folder.setChecked(True)

    def keys(self) -> dict[str, str]:
        return {m: c.currentData() for m, c in self.key_combos.items()}

    def apply(self) -> list[str]:
        """Write the widgets to settings, save, apply Windows integration. Returns errors.

        Sets `language_changed` when the chosen language differs from the one running now
        (it takes effect after a restart).
        """
        keys = self.keys()
        if len(set(keys.values())) != len(keys):
            raise ValueError("each wheel needs a different key")
        for mode, key in keys.items():
            settings.set(f"keys.{mode}", key)
        language = self.language.currentData() or "system"
        settings.set("language", language)
        settings.set("enabled", self.enabled.isChecked())
        settings.set("wheel_size", round(self.wheel_size.value() / 100, 2))
        settings.set("output_location", "folder" if self.out_folder.isChecked() else "same")
        folder = self.folder_edit.text().strip()
        if folder:
            settings.set("output_folder", folder)
        settings.set("reveal_after", self.reveal_after.isChecked())
        settings.set("jpeg_quality", self.jpeg_quality.value())
        settings.set("image_dpi", self.image_dpi.value())
        settings.set("terminal", self.terminal.currentData())
        settings.set("start_with_windows", self.autostart.isChecked())
        settings.save()

        errors = []

        def run(what, fn, *args):
            try:
                fn(*args)
            except Exception as e:  # registry/COM failures should not lose the other settings
                log.exception("%s failed", what)
                errors.append(tr("{what}: {error}").format(what=tr(what), error=e))

        if self.autostart.isChecked() != self._autostart0:
            run(N_("Start with Windows"), integration.set_autostart, self.autostart.isChecked())
            self._autostart0 = self.autostart.isChecked()
        if self.sendto.isChecked() != self._sendto0:
            run(N_("Send to"), integration.install_send_to if self.sendto.isChecked() else integration.remove_send_to)
            self._sendto0 = self.sendto.isChecked()
        if self.context_menu.isChecked() != self._menu0:
            run(N_("Context menu"),
                integration.install_context_menu if self.context_menu.isChecked() else integration.remove_context_menu)
            self._menu0 = self.context_menu.isChecked()

        new_code = i18n.resolve(language)
        self.language_changed = language != self._language0 and new_code != i18n.current()
        if language != self._language0:
            self._language0 = language
            if self.language_changed:
                # Explorer shows these labels, so write them in the new language now.
                run(N_("Context menu"), integration.relabel_shell_entries, new_code)
        return errors

    def _ask_restart(self) -> bool:
        """Ask whether to restart now for the new language. True = restart now."""
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Question)
        box.setText(tr("Restart {app} now to apply the language?").format(app=APP_NAME))
        now = box.addButton(tr("Restart now"), QMessageBox.AcceptRole)
        box.addButton(tr("Later"), QMessageBox.RejectRole)
        box.setDefaultButton(now)
        box.exec()
        return box.clickedButton() is now

    def save_and_close(self) -> None:
        if self.out_folder.isChecked() and not self.folder_edit.text().strip():
            self.tabs.setCurrentIndex(2)
            self.status.setText(tr("Choose an output folder"))
            return
        errors = self.apply()
        if errors:
            QMessageBox.warning(self, APP_NAME,
                                tr("Settings were saved, but:\n\n{errors}").format(errors="\n".join(errors)))
        restart = self.language_changed and self._ask_restart()
        self.accept()
        if restart:
            integration.restart_app()


_dialog: SettingsDialog | None = None


def open_settings(modal: bool = True) -> SettingsDialog:
    """Show the settings window, keeping a single instance.

    If it is already open it is just brought to the front. Otherwise it is shown and,
    with `modal`, this returns only after it is closed (so callers can refresh things
    like the tray tooltip afterwards).
    """
    global _dialog
    if _dialog is not None and _dialog.isVisible():
        _dialog.raise_()
        _dialog.activateWindow()
        return _dialog
    _dialog = SettingsDialog()
    _dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    if modal:
        _dialog.exec()
    return _dialog
