"""File actions for the Actions wheel: share, float, copy, move, copy path, terminal,
reveal, rename and hash.

They all work on any selection (files, folders, mixed) unless noted, and run as
GUI-thread `dialog` functions because they touch the clipboard, windows or
other processes; see registry.Option for the contract.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from ctypes import wintypes as W
from datetime import datetime
from pathlib import Path

from .. import paths as pathmod
from ..config import APP_NAME, settings
from ..i18n import N_, tr
from ..registry import Option, UserError, add

log = logging.getLogger(__name__)

ACTION_COLOR_FAMILY = "any"


# ---------------------------------------------------------------------------
# Clipboard helpers (win32clipboard, so they work no matter which Qt platform runs)
# ---------------------------------------------------------------------------

DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2


def _open_clipboard(retries: int = 20) -> None:
    import win32clipboard

    for i in range(retries):
        try:
            win32clipboard.OpenClipboard(None)
            return
        except Exception:  # another process holds it; retry briefly
            if i == retries - 1:
                raise UserError(tr("The clipboard is busy, try again"))
            time.sleep(0.03)


def dropfiles_bytes(paths: list[Path]) -> bytes:
    """A CF_HDROP payload: DROPFILES header + double-NUL terminated UTF-16 list."""
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # pFiles, pt.x, pt.y, fNC, fWide
    body = "".join(str(p) + "\0" for p in paths) + "\0"
    return header + body.encode("utf-16-le")


def set_clipboard_files(paths: list[Path], effect: int = DROPEFFECT_COPY) -> None:
    """Put files on the clipboard so Ctrl+V in Explorer pastes (copies) them."""
    import win32clipboard
    import win32con

    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, dropfiles_bytes(paths))
        fmt = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
        win32clipboard.SetClipboardData(fmt, struct.pack("<I", effect))
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    import win32clipboard
    import win32con

    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def paths_text(paths: list[Path]) -> str:
    return "\r\n".join(str(p) for p in paths)


def _count(n: int, one: str, many: str, **kw) -> str:
    """Translate the singular or plural template (both marked with N_()) and fill in {n}."""
    return tr(one if n == 1 else many).format(n=n, **kw)


# ---------------------------------------------------------------------------
# Copy / Copy Path
# ---------------------------------------------------------------------------

def copy_dialog(paths: list[Path], ctx) -> dict:
    set_clipboard_files(paths)
    return {"done": True, "message": _count(len(paths), N_("Copied {n} item"), N_("Copied {n} items"))}


def copy_path_dialog(paths: list[Path], ctx) -> dict:
    set_clipboard_text(paths_text(paths))
    msg = tr("Copied path") if len(paths) == 1 else tr("Copied {n} paths").format(n=len(paths))
    return {"done": True, "message": msg}


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------

def move_items(paths: list[Path], dest: Path) -> list[Path]:
    """Move `paths` into `dest`, renaming on conflicts. Returns the new locations."""
    dest = Path(dest)
    if not dest.is_dir():
        raise UserError(tr("Not a folder: {path}").format(path=dest))
    moved: list[Path] = []
    for src in paths:
        src = Path(src)
        if not src.exists():
            raise UserError(tr("{name} no longer exists").format(name=src.name))
        if src.parent.resolve() == dest.resolve():
            continue  # already there
        if src.is_dir() and (dest.resolve() == src.resolve() or src.resolve() in dest.resolve().parents):
            raise UserError(tr("Cannot move {name} into itself").format(name=src.name))
        target = pathmod.unique_path(dest / src.name)
        try:
            shutil.move(str(src), str(target))
        except OSError as e:
            raise UserError(tr("Could not move {name}: {error}").format(name=src.name, error=e.strerror or e)) from e
        moved.append(target)
    return moved


def move_dialog(paths: list[Path], ctx) -> dict | None:
    from PySide6.QtWidgets import QFileDialog

    start = str(paths[0].parent)
    title = _count(len(paths), N_("Move {n} item to…"), N_("Move {n} items to…"))
    folder = QFileDialog.getExistingDirectory(None, title, start)
    if not folder:
        return None
    dest = Path(folder)
    moved = move_items(paths, dest)
    if not moved:
        return {"done": True, "message": tr("Already in {folder}").format(folder=dest.name or dest)}
    return {"done": True, "outputs": moved,
            "message": _count(len(moved), N_("Moved {n} item to {folder}"), N_("Moved {n} items to {folder}"),
                              folder=dest.name or dest)}


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

def _which(*names: str) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _wt_exe() -> str | None:
    found = shutil.which("wt.exe")
    if found:
        return found
    alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
    return str(alias) if alias.exists() else None


def target_folders(paths: list[Path]) -> list[Path]:
    """Folders to open for a selection: folders themselves, files' parents (unique, ordered)."""
    seen: OrderedDict[str, Path] = OrderedDict()
    for p in paths:
        folder = p if p.is_dir() else p.parent
        seen.setdefault(str(folder).lower(), folder)
    return list(seen.values())


def terminal_command(folder: Path, choice: str = "auto") -> tuple[list[str], str | None]:
    """(argv, cwd) that opens a terminal in `folder` for terminal setting `choice`."""
    folder_s = str(folder)
    if choice in ("auto", "wt"):
        wt = _wt_exe()
        if wt:
            # wt treats a trailing backslash as escaping the closing quote (C:\ -> C:\.).
            d = folder_s + "." if folder_s.endswith("\\") else folder_s
            return [wt, "-d", d], None
        if choice == "wt":
            choice = "auto"
    if choice in ("auto", "powershell"):
        ps = _which("pwsh.exe") if choice == "auto" else None
        ps = ps or _which("powershell.exe")
        if ps:
            return [ps, "-NoExit", "-NoLogo"], folder_s
    cmd = os.environ.get("COMSPEC") or "cmd.exe"
    return [cmd, "/K"], folder_s


def open_terminal(folder: Path, choice: str | None = None) -> None:
    argv, cwd = terminal_command(folder, choice or settings.get("terminal", "auto"))
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    try:
        subprocess.Popen(argv, cwd=cwd, creationflags=flags, close_fds=True)
    except OSError as e:
        raise UserError(tr("Could not start the terminal: {error}").format(error=e)) from e


def terminal_dialog(paths: list[Path], ctx) -> dict:
    for folder in target_folders(paths)[:5]:
        open_terminal(folder)
    return {"done": True}


# ---------------------------------------------------------------------------
# Reveal in Explorer
# ---------------------------------------------------------------------------

def reveal(paths: list[Path]) -> None:
    """Open Explorer with the items selected (one window per parent folder)."""
    groups: OrderedDict[str, list[Path]] = OrderedDict()
    for p in paths:
        groups.setdefault(str(p.parent).lower(), []).append(p)
    for items in list(groups.values())[:5]:
        try:
            from win32com.shell import shell

            folder_pidl = shell.SHILCreateFromPath(str(items[0].parent), 0)[0]
            children = [shell.SHILCreateFromPath(str(p), 0)[0] for p in items]
            shell.SHOpenFolderAndSelectItems(folder_pidl, children, 0)
        except Exception:
            log.debug("SHOpenFolderAndSelectItems failed, using explorer /select", exc_info=True)
            subprocess.Popen(f'explorer.exe /select,"{items[0]}"')


def reveal_dialog(paths: list[Path], ctx) -> dict:
    reveal(paths)
    return {"done": True}


# ---------------------------------------------------------------------------
# Batch rename
# ---------------------------------------------------------------------------

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
CASES = {"keep": N_("Keep case"), "lower": N_("lowercase"), "upper": N_("UPPERCASE"), "title": N_("Title Case")}
# Rename pattern placeholders: the same in every language (only the text around them is translated).
PATTERN_TOKENS = "{name} {n} {n:03} {ext} {parent} {date}"


def split_name(p: Path) -> tuple[str, str]:
    """(stem, '.ext') - folders and dotfiles have no extension."""
    if p.is_dir() or (p.name.startswith(".") and p.name.count(".") == 1):
        return p.name, ""
    return p.stem, p.suffix


def new_name(p: Path, index: int, pattern: str = "{name}", find: str = "", replace: str = "",
             case: str = "keep", regex: bool = False, match_case: bool = False) -> str:
    """The new file name for `p` (extension kept, the pattern applies to the stem).

    Placeholders: {name} original stem (after find/replace), {n} counter (supports
    {n:03}), {ext} extension without dot, {parent} folder name, {date} modified date.
    """
    stem, ext = split_name(p)
    if find:
        flags = 0 if match_case else re.IGNORECASE
        try:
            rx = re.compile(find if regex else re.escape(find), flags)
            stem = rx.sub(replace if regex else replace.replace("\\", "\\\\"), stem)
        except re.error as e:
            raise UserError(tr("Invalid pattern: {error}").format(error=e)) from e
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime) if p.exists() else datetime.now()
    except OSError:
        mtime = datetime.now()
    try:
        base = (pattern or "{name}").format(name=stem, n=index, ext=ext.lstrip("."),
                                            parent=p.parent.name, date=mtime.strftime("%Y-%m-%d"))
    except (KeyError, IndexError, ValueError) as e:
        raise UserError(tr("Invalid pattern {pattern}: {error}").format(pattern=repr(pattern), error=e)) from e
    if case == "lower":
        base, ext = base.lower(), ext.lower()
    elif case == "upper":
        base, ext = base.upper(), ext.upper()
    elif case == "title":
        base = base.title()
    return base.strip() + ext


def rename_plan(paths: list[Path], pattern: str = "{name}", find: str = "", replace: str = "",
                case: str = "keep", start: int = 1, regex: bool = False,
                match_case: bool = False) -> list[tuple[Path, Path, str]]:
    """[(src, dst, problem)] - problem is "" when the rename is fine.

    Problems are English source strings (stable for callers); show them with tr().
    """
    plan: list[tuple[Path, Path, str]] = []
    sources = {str(p).lower() for p in paths}
    taken: dict[str, int] = {}
    for i, p in enumerate(paths):
        name = new_name(p, start + i, pattern, find, replace, case, regex, match_case)
        dst = p.with_name(name)
        problem = ""
        if not name or name in (".", ".."):
            problem = N_("empty name")
        elif INVALID_CHARS.search(name):
            problem = N_("invalid character")
        elif name.split(".")[0].lower() in RESERVED:
            problem = N_("reserved name")
        elif name.endswith((".", " ")):
            problem = N_("ends with dot/space")
        key = str(dst).lower()
        taken[key] = taken.get(key, 0) + 1
        plan.append((p, dst, problem))
    out = []
    for src, dst, problem in plan:
        key = str(dst).lower()
        if not problem:
            if taken[key] > 1:
                problem = N_("duplicate name")
            elif dst.exists() and key not in sources:
                problem = N_("already exists")
        out.append((src, dst, problem))
    return out


def apply_renames(plan: list[tuple[Path, Path, str]]) -> list[Path]:
    """Rename everything in `plan` (two-phase so swaps/case changes work). Rolls back on error."""
    bad = [(s, p) for s, _d, p in plan if p]
    if bad:
        raise UserError(tr("{name}: {problem}").format(name=bad[0][0].name, problem=tr(bad[0][1])))
    work = [(s, d) for s, d, _p in plan if s.name != d.name]
    staged: list[tuple[Path, Path, Path]] = []  # (original, temp, final)
    try:
        for src, dst in work:
            tmp = pathmod.unique_path(src.with_name(f".satsuma-rename-{uuid.uuid4().hex[:8]}{src.suffix}"))
            os.rename(src, tmp)
            staged.append((src, tmp, dst))
        done: list[Path] = []
        for i, (src, tmp, dst) in enumerate(staged):
            os.rename(tmp, dst)
            done.append(dst)
    except OSError as e:
        # roll back whatever moved
        for src, tmp, dst in staged:
            for cur in (tmp, dst):
                try:
                    if cur.exists() and not src.exists():
                        os.rename(cur, src)
                except OSError:
                    pass
        raise UserError(tr("Rename failed: {error}").format(error=e.strerror or e)) from e
    return [d for _s, d, _p in plan]


def rename_dialog(paths: list[Path], ctx) -> dict | None:
    from ..ui.common import ToolDialog
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QCheckBox, QHeaderView, QTableWidget, QTableWidgetItem

    class RenameDialog(ToolDialog):
        def __init__(self):
            subtitle = _count(len(paths), N_("{n} item · placeholders: {tokens}"),
                              N_("{n} items · placeholders: {tokens}"), tokens=PATTERN_TOKENS)
            super().__init__(tr("Rename"), subtitle, primary=tr("Rename"))
            self.setMinimumWidth(620)
            self.pattern = self.add_text(tr("Name pattern"), "{name}-{n}" if len(paths) > 1 else "{name}")
            self.start = self.add_spin(tr("Start number"), 0, 999999, 1)
            self.find = self.add_text(tr("Find"), placeholder=tr("text to replace (optional)"))
            self.replace = self.add_text(tr("Replace with"))
            self.regex = QCheckBox(tr("Regular expression"))
            self.match_case = QCheckBox(tr("Match case"))
            self.form.addRow("", self.regex)
            self.form.addRow("", self.match_case)
            self.case = self.add_combo(tr("Case"), {tr(v): k for k, v in CASES.items()}, "keep")
            self.table = QTableWidget(0, 2)
            self.table.setHorizontalHeaderLabels([tr("Current name"), tr("New name")])
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setMinimumHeight(220)
            self.body.addWidget(self.table)
            for w in (self.pattern, self.find, self.replace):
                w.textChanged.connect(self.refresh)
            self.start.valueChanged.connect(self.refresh)
            self.case.currentIndexChanged.connect(self.refresh)
            self.regex.toggled.connect(self.refresh)
            self.match_case.toggled.connect(self.refresh)
            self.plan: list = []
            self.refresh()

        def params(self) -> dict:
            return dict(pattern=self.pattern.text(), find=self.find.text(), replace=self.replace.text(),
                        case=self.case.currentData(), start=self.start.value(),
                        regex=self.regex.isChecked(), match_case=self.match_case.isChecked())

        def refresh(self) -> None:
            try:
                self.plan = rename_plan(paths, **self.params())
                error = ""
            except UserError as e:
                self.plan, error = [], str(e)
            self.table.setRowCount(len(paths) if not error else 1)
            if error:
                self.table.setItem(0, 0, QTableWidgetItem(error))
                self.table.setItem(0, 1, QTableWidgetItem(""))
            for row, (src, dst, problem) in enumerate(self.plan):
                self.table.setItem(row, 0, QTableWidgetItem(src.name))
                item = QTableWidgetItem(dst.name + ("   ⚠ " + tr(problem) if problem else ""))
                if problem:
                    item.setForeground(QColor("#ff6b6b"))
                elif dst.name != src.name:
                    item.setForeground(QColor("#ffb066"))
                item.setToolTip(str(dst))
                self.table.setItem(row, 1, item)
            ok = bool(self.plan) and not any(p for _s, _d, p in self.plan)
            self.primary_btn.setEnabled(ok)

    dlg = RenameDialog()
    if not dlg.exec():
        return None
    plan = rename_plan(paths, **dlg.params())
    changed = sum(1 for s, d, _p in plan if s.name != d.name)
    if not changed:
        return {"done": True, "message": tr("Nothing to rename")}
    outs = apply_renames(plan)
    return {"done": True, "outputs": outs,
            "message": _count(changed, N_("Renamed {n} item"), N_("Renamed {n} items"))}


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

HASH_ALGOS = ("sha256", "sha1", "md5")


def file_hashes(path: Path, algos=HASH_ALGOS, progress=None, cancelled=None) -> dict[str, str]:
    """Hex digests of `path` for each algorithm, read in one pass."""
    hs = {a: hashlib.new(a) for a in algos}
    total = max(1, path.stat().st_size)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancelled is not None and cancelled():
                raise UserError(tr("Cancelled"))
            chunk = f.read(1 << 20)
            if not chunk:
                break
            for h in hs.values():
                h.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done / total)
    return {a: h.hexdigest() for a, h in hs.items()}


def hash_dialog(paths: list[Path], ctx) -> dict:
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton

    from ..ui.common import ToolDialog

    class Worker(QThread):
        result = Signal(int, object)      # index, dict | str (error)
        progressed = Signal(float)

        def __init__(self):
            super().__init__()
            self.stop = False

        def run(self):
            for i, p in enumerate(paths):
                try:
                    r = file_hashes(p, progress=lambda f, i=i: self.progressed.emit((i + f) / len(paths)),
                                    cancelled=lambda: self.stop)
                except UserError:
                    return
                except OSError as e:
                    r = tr("error: {error}").format(error=e.strerror or e)
                self.result.emit(i, r)

    class HashDialog(ToolDialog):
        def __init__(self):
            super().__init__(tr("Checksums"), _count(len(paths), N_("{n} file"), N_("{n} files")),
                             primary=tr("Copy all"))
            self.setMinimumWidth(640)
            self.results: dict[int, object] = {}
            self.text = QPlainTextEdit(readOnly=True)
            self.text.setFont(QFont("Consolas", 9))
            self.text.setMinimumHeight(min(420, 90 + 70 * len(paths)))
            self.body.addWidget(self.text)
            self.bar = QProgressBar()
            self.bar.setRange(0, 1000)
            self.bar.setTextVisible(False)
            self.body.addWidget(self.bar)
            self.verify = QLineEdit(placeholderText=tr("Paste a checksum to verify…"))
            self.verify.textChanged.connect(self.check)
            self.body.addWidget(self.verify)
            self.verdict = QLabel("", objectName="subtitle")
            self.body.addWidget(self.verdict)
            self.cancel_btn.setText(tr("Close"))
            self.copy256 = QPushButton(tr("Copy {algo}").format(algo="SHA-256"))
            self.footer.insertWidget(1, self.copy256)
            self.copy256.clicked.connect(lambda: self.copy("sha256"))
            self.primary_btn.clicked.disconnect()
            self.primary_btn.clicked.connect(lambda: self.copy(None))
            self.primary_btn.setEnabled(False)
            self.copy256.setEnabled(False)
            self.worker = Worker()
            self.worker.result.connect(self.on_result)
            self.worker.progressed.connect(lambda f: self.bar.setValue(int(f * 1000)))
            self.worker.finished.connect(self.on_done)
            self.render()
            self.worker.start()

        def render(self):
            lines = []
            for i, p in enumerate(paths):
                lines.append(p.name)
                r = self.results.get(i)
                if r is None:
                    lines.append("  " + tr("computing…"))
                elif isinstance(r, str):
                    lines.append("  " + r)
                else:
                    for a in HASH_ALGOS:
                        lines.append(f"  {a.upper():<7}{r[a]}")
                lines.append("")
            self.text.setPlainText("\n".join(lines).rstrip())

        def on_result(self, i, r):
            self.results[i] = r
            self.render()
            self.check()

        def on_done(self):
            self.bar.setValue(1000)
            self.primary_btn.setEnabled(True)
            self.copy256.setEnabled(True)

        def copy(self, algo):
            if algo is None:
                text = self.text.toPlainText()
            else:
                parts = []
                for i, p in enumerate(paths):
                    r = self.results.get(i)
                    if isinstance(r, dict):
                        parts.append(r[algo] if len(paths) == 1 else f"{r[algo]}  {p.name}")
                text = "\n".join(parts)
            QGuiApplication.clipboard().setText(text)
            try:
                set_clipboard_text(text)
            except Exception:
                pass
            self.verdict.setText(tr("Copied to clipboard"))

        def check(self):
            want = self.verify.text().strip().lower()
            if not want:
                self.verdict.setText("")
                return
            for i, p in enumerate(paths):
                r = self.results.get(i)
                if isinstance(r, dict):
                    for a, v in r.items():
                        if v == want:
                            self.verdict.setText(tr("✓ Matches {algo} of {name}").format(
                                algo=a.upper(), name=p.name))
                            self.verdict.setStyleSheet("color: #5fd38d;")
                            return
            self.verdict.setText(tr("✗ No match") if len(self.results) == len(paths) else tr("Checking…"))
            self.verdict.setStyleSheet("color: #ff6b6b;" if len(self.results) == len(paths) else "")

        def done(self, r):
            self.worker.stop = True
            self.worker.wait(2000)
            super().done(r)

    HashDialog().exec()
    return {"done": True}


# ---------------------------------------------------------------------------
# Float (shelf)
# ---------------------------------------------------------------------------

def float_dialog(paths: list[Path], ctx) -> dict:
    from .shelf import get_shelf

    shelf = get_shelf()
    shelf.add_paths(paths)
    shelf.present()
    return {"done": True}


# ---------------------------------------------------------------------------
# Share: the Windows share sheet via DataTransferManager (raw COM/WinRT through ctypes)
#
# pywinrt has no IDataTransferManagerInterop and pywin32's IContextMenu.InvokeCommand
# crashes with string verbs (and the "Windows.ModernShare" verb rejects a window it did
# not get from an Explorer site), so we talk to the ABI directly:
#   RoGetActivationFactory(DataTransferManager, IDataTransferManagerInterop)
#   -> GetForWindow(hwnd) -> add_DataRequested(our delegate) -> ShowShareUIForWindow(hwnd)
#   In DataRequested: request.Data.Properties.Title = ...; Data.SetStorageItems(our IIterable)
# StorageFile/StorageFolder objects come from Get{File,Folder}FromPathAsync (polled).
# ---------------------------------------------------------------------------

_HR = ctypes.c_long
S_OK = 0
E_NOINTERFACE = -2147467262
E_FAIL = -2147467259
E_BOUNDS = -2147483637

IID_IUnknown = "00000000-0000-0000-c000-000000000046"
IID_IAgileObject = "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"
IID_IInspectable = "af86e2e0-b12d-4c6a-9c5a-d7aa65101e90"
IID_IAsyncInfo = "00000036-0000-0000-c000-000000000046"
IID_IDataTransferManagerInterop = "3a3dcd6c-3eab-43dc-bcde-45671ce800c8"
IID_IDataTransferManager = "a5caee9b-8708-49d1-8d36-67d25a8da00c"
IID_IDataRequestedEventArgs = "cb8ba807-6ac5-43c9-8ac5-9ba232163182"
IID_IStorageItem = "4207a996-ca2f-42f7-bde8-8b10457a7f30"
IID_IStorageFileStatics = "5984c710-daf2-43c8-8bb4-a4d3eacfd03f"
IID_IStorageFolderStatics = "08f327ff-85d5-48b9-aee9-28511e339f9f"


def pinterface_iid(generic: str, *args: str) -> str:
    """IID of a parameterized WinRT interface (the documented UUIDv5 algorithm)."""
    sig = "pinterface({" + generic + "};" + ";".join(args) + ")"
    return str(uuid.uuid5(uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee"), sig))


IID_IIterable_IStorageItem = pinterface_iid("faa585ea-6214-4217-afda-7f46de5869b3", "{" + IID_IStorageItem + "}")
IID_IIterator_IStorageItem = pinterface_iid("6a79e863-4300-459a-9966-cbb660963ee1", "{" + IID_IStorageItem + "}")
IID_DataRequestedHandler = pinterface_iid(
    "9de1c534-6ae1-11e0-84e1-18a905bcc53f",
    "rc(Windows.ApplicationModel.DataTransfer.DataTransferManager;{" + IID_IDataTransferManager + "})",
    "rc(Windows.ApplicationModel.DataTransfer.DataRequestedEventArgs;{" + IID_IDataRequestedEventArgs + "})",
)


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, s: str) -> "_GUID":
        g = cls()
        ctypes.memmove(g.data, uuid.UUID(s).bytes_le, 16)
        return g


def _hr_text(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}: {ctypes.FormatError(ctypes.c_long(hr).value).strip()}"


def _vfunc(ptr, index: int, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _call(ptr, index: int, what: str, argtypes: list, *args) -> None:
    hr = _vfunc(ptr, index, _HR, *argtypes)(ptr, *args)
    if hr < 0:
        raise OSError(f"{what} failed ({_hr_text(hr)})")


def _get(ptr, index: int, what: str) -> int:
    """Call a getter returning an interface pointer (`HRESULT f(this, void**)`)."""
    out = ctypes.c_void_p()
    _call(ptr, index, what, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
    if not out.value:
        raise OSError(f"{what} returned nothing")
    return out.value


def _addref(ptr) -> None:
    _vfunc(ptr, 1, ctypes.c_ulong)(ptr)


def _release(ptr) -> None:
    if ptr:
        _vfunc(ptr, 2, ctypes.c_ulong)(ptr)


def _qi(ptr, iid: str) -> int:
    out = ctypes.c_void_p()
    g = _GUID.of(iid)
    _call(ptr, 0, f"QueryInterface({iid})", [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
          ctypes.byref(g), ctypes.byref(out))
    return out.value


class _HString:
    """An HSTRING for the duration of a `with` block."""

    def __init__(self, text: str):
        self.text = text
        self.handle = ctypes.c_void_p()

    def __enter__(self):
        _combase().WindowsCreateString(self.text, len(self.text), ctypes.byref(self.handle))
        return self.handle

    def __exit__(self, *exc):
        _combase().WindowsDeleteString(self.handle)


_combase_dll = None


def _combase():
    global _combase_dll
    if _combase_dll is None:
        dll = ctypes.WinDLL("combase")
        dll.WindowsCreateString.argtypes = [W.LPCWSTR, W.UINT, ctypes.POINTER(ctypes.c_void_p)]
        dll.WindowsCreateString.restype = _HR
        dll.WindowsDeleteString.argtypes = [ctypes.c_void_p]
        dll.WindowsDeleteString.restype = _HR
        dll.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        dll.RoGetActivationFactory.restype = _HR
        dll.RoInitialize.argtypes = [ctypes.c_int]
        dll.RoInitialize.restype = _HR
        _combase_dll = dll
    return _combase_dll


def _factory(class_name: str, iid: str) -> int:
    # Make sure this thread has COM/WinRT (Qt's windows platform already did OleInitialize;
    # S_FALSE / RPC_E_CHANGED_MODE are fine).
    _combase().RoInitialize(0)
    out = ctypes.c_void_p()
    with _HString(class_name) as hs:
        hr = _combase().RoGetActivationFactory(hs, ctypes.byref(_GUID.of(iid)), ctypes.byref(out))
    if hr < 0 or not out.value:
        raise OSError(f"RoGetActivationFactory({class_name}) failed ({_hr_text(hr)})")
    return out.value


class _ComObject:
    """A tiny COM object implemented in Python: IUnknown (+ IInspectable) + `methods`.

    `methods` is a list of (argtypes, callable(*args) -> HRESULT) appended to the vtable.
    Instances stay alive (in `_alive`) until their COM reference count drops to zero.
    """

    _alive: set = set()

    def __init__(self, iids: list[str], methods: list[tuple[list, object]], inspectable: bool = True):
        base = [IID_IUnknown, IID_IAgileObject] + ([IID_IInspectable] if inspectable else [])
        self._iids = {uuid.UUID(i).bytes_le for i in base + iids}
        self._refs = 1
        P = ctypes.c_void_p
        funcs = [
            ctypes.WINFUNCTYPE(_HR, P, ctypes.POINTER(_GUID), ctypes.POINTER(P))(self._query),
            ctypes.WINFUNCTYPE(ctypes.c_ulong, P)(self._add),
            ctypes.WINFUNCTYPE(ctypes.c_ulong, P)(self._rel),
        ]
        if inspectable:
            funcs += [
                ctypes.WINFUNCTYPE(_HR, P, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(P))(self._get_iids),
                ctypes.WINFUNCTYPE(_HR, P, ctypes.POINTER(P))(self._class_name),
                ctypes.WINFUNCTYPE(_HR, P, ctypes.POINTER(ctypes.c_int))(self._trust),
            ]
        for argtypes, fn in methods:
            funcs.append(ctypes.WINFUNCTYPE(_HR, P, *argtypes)(self._guard(fn)))
        self._funcs = funcs
        self._vtbl = (P * len(funcs))(*[ctypes.cast(f, P).value for f in funcs])
        self._obj = (P * 1)(ctypes.addressof(self._vtbl))
        self.ptr = ctypes.addressof(self._obj)
        _ComObject._alive.add(self)

    @staticmethod
    def _guard(fn):
        def wrapper(this, *args):
            try:
                return fn(*args)
            except Exception:
                log.exception("COM callback failed")
                return E_FAIL
        return wrapper

    def _query(self, this, riid, ppv):
        if bytes(riid.contents.data) in self._iids:
            ppv[0] = self.ptr
            self._refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _add(self, this):
        self._refs += 1
        return self._refs

    def _rel(self, this):
        self._refs -= 1
        if self._refs <= 0:
            _ComObject._alive.discard(self)
        return max(self._refs, 0)

    def release(self) -> None:
        """Drop the Python-side reference taken at construction."""
        self._rel(None)

    def _get_iids(self, this, count, iids):
        count[0] = 0
        iids[0] = None
        return S_OK

    def _class_name(self, this, name):
        name[0] = None
        return S_OK

    def _trust(self, this, level):
        level[0] = 0
        return S_OK


class _ItemIterator(_ComObject):
    """IIterator<IStorageItem> over a fixed list of IStorageItem pointers."""

    def __init__(self, items: list[int]):
        self.items, self.i = items, 0
        P = ctypes.c_void_p
        super().__init__([IID_IIterator_IStorageItem], [
            ([ctypes.POINTER(P)], self.current),
            ([ctypes.POINTER(ctypes.c_ubyte)], self.has_current),
            ([ctypes.POINTER(ctypes.c_ubyte)], self.move_next),
            ([ctypes.c_uint, ctypes.POINTER(P), ctypes.POINTER(ctypes.c_uint)], self.get_many),
        ])

    def current(self, out):
        if self.i >= len(self.items):
            out[0] = None
            return E_BOUNDS
        _addref(self.items[self.i])
        out[0] = self.items[self.i]
        return S_OK

    def has_current(self, out):
        out[0] = 1 if self.i < len(self.items) else 0
        return S_OK

    def move_next(self, out):
        if self.i < len(self.items):
            self.i += 1
        out[0] = 1 if self.i < len(self.items) else 0
        return S_OK

    def get_many(self, capacity, items, actual):
        n = 0
        while n < capacity and self.i < len(self.items):
            _addref(self.items[self.i])
            items[n] = self.items[self.i]
            n += 1
            self.i += 1
        actual[0] = n
        return S_OK


class _ItemIterable(_ComObject):
    """IIterable<IStorageItem>."""

    def __init__(self, items: list[int]):
        self.items = items
        super().__init__([IID_IIterable_IStorageItem], [([ctypes.POINTER(ctypes.c_void_p)], self.first)])

    def first(self, out):
        it = _ItemIterator(self.items)   # its construction reference passes to the caller
        out[0] = it.ptr
        return S_OK


def _wait_async(op: int, what: str, timeout: float = 15.0) -> int:
    """Wait for an IAsyncOperation<T> and return its (interface pointer) result."""
    info = _qi(op, IID_IAsyncInfo)
    try:
        status = ctypes.c_int(0)
        deadline = time.monotonic() + timeout
        while True:
            _call(info, 7, "IAsyncInfo.Status", [ctypes.POINTER(ctypes.c_int)], ctypes.byref(status))
            if status.value != 0:
                break
            if time.monotonic() > deadline:
                _call(info, 9, "IAsyncInfo.Cancel", [])
                raise OSError(f"{what} timed out")
            time.sleep(0.005)
        if status.value != 1:
            code = ctypes.c_long(0)
            _call(info, 8, "IAsyncInfo.ErrorCode", [ctypes.POINTER(ctypes.c_long)], ctypes.byref(code))
            raise OSError(f"{what} failed ({_hr_text(code.value)})")
        return _get(op, 8, f"{what}.GetResults")
    finally:
        _release(info)


def _storage_item(path: Path) -> int:
    """An IStorageItem pointer (StorageFile or StorageFolder) for `path`."""
    if path.is_dir():
        cls, iid = "Windows.Storage.StorageFolder", IID_IStorageFolderStatics
    else:
        cls, iid = "Windows.Storage.StorageFile", IID_IStorageFileStatics
    statics = _factory(cls, iid)
    try:
        op = ctypes.c_void_p()
        with _HString(str(path)) as hs:
            _call(statics, 6, "Get*FromPathAsync", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                  hs, ctypes.byref(op))
        try:
            obj = _wait_async(op.value, f"Opening {path.name}")
        finally:
            _release(op.value)
        try:
            return _qi(obj, IID_IStorageItem)
        finally:
            _release(obj)
    finally:
        _release(statics)


def share_card(size: tuple[int, int] = (400, 470)):
    """The opaque card the share flyout is anchored to ("Opening Share…", centred).

    AlignCenter and word wrap keep long or right-to-left translations centred on the card.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    card = QWidget(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    card.setWindowTitle(tr("{app} share").format(app=APP_NAME))
    card.setStyleSheet("background: #1c1c20; color: #9a9aa6;")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(24, 24, 24, 24)
    label = QLabel(tr("Opening Share…"), alignment=Qt.AlignCenter, wordWrap=True, objectName="shareText")
    lay.addWidget(label)
    card.resize(*size)
    return card


class WindowsShare:
    """The Windows share sheet, anchored to a small helper window (one per app).

    Findings on Windows 10 (19045): ShowShareUIForWindow only shows the flyout when
    the anchor window is visible, foreground and *not layered* (setWindowOpacity
    breaks it), and the flyout takes the anchor's rectangle - a 1x1 anchor gives an
    unusable/flaky flyout. So we show a small opaque card at the cursor, open the
    flyout over it, and hide the card once the flyout window exists (hiding the
    anchor does not close the flyout).
    """

    ANCHOR_SIZE = (400, 470)

    def __init__(self):
        from PySide6.QtCore import QTimer

        self.window = share_card(self.ANCHOR_SIZE)
        self.hwnd = int(self.window.winId())
        self._watch = QTimer(interval=100)
        self._watch.timeout.connect(self._check_flyout)
        self._watch_started = 0.0
        self.interop = _factory("Windows.ApplicationModel.DataTransfer.DataTransferManager",
                                IID_IDataTransferManagerInterop)
        out = ctypes.c_void_p()
        _call(self.interop, 3, "GetForWindow", [W.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
              self.hwnd, ctypes.byref(_GUID.of(IID_IDataTransferManager)), ctypes.byref(out))
        self.manager = out.value
        self.items: list[int] = []
        self.paths: list[Path] = []
        self.requests = 0
        self.handler = _ComObject([IID_DataRequestedHandler],
                                  [([ctypes.c_void_p, ctypes.c_void_p], self._on_data_requested)],
                                  inspectable=False)
        self.token = ctypes.c_int64(0)
        _call(self.manager, 6, "add_DataRequested", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)],
              self.handler.ptr, ctypes.byref(self.token))

    def _set_items(self, items: list[int]) -> None:
        for p in self.items:
            _release(p)
        self.items = items

    def share(self, paths: list[Path]) -> None:
        items: list[int] = []
        try:
            for p in paths:
                items.append(_storage_item(p))
        except Exception:
            for p in items:
                _release(p)
            raise
        self._set_items(items)
        self.paths = list(paths)
        self._bring_forward()
        try:
            _call(self.interop, 4, "ShowShareUIForWindow", [W.HWND], self.hwnd)
        except OSError:
            self.window.hide()
            raise
        self._watch_started = time.monotonic()
        self._watch.start()

    def _bring_forward(self) -> None:
        """Show the anchor card near the cursor and make it the foreground window."""
        from PySide6.QtGui import QCursor, QGuiApplication

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = W.HWND
        w = self.window
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        aw, ah = self.ANCHOR_SIZE
        x = min(max(pos.x() - aw // 2, geo.left()), geo.right() - aw)
        y = min(max(pos.y() - ah // 2, geo.top()), geo.bottom() - ah)
        w.move(x, y)
        w.show()
        w.raise_()
        w.activateWindow()
        user32.SetForegroundWindow(W.HWND(self.hwnd))
        if user32.GetForegroundWindow() != self.hwnd:
            # Foreground lock: a synthetic Alt tap lets us take the foreground.
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(W.HWND(self.hwnd))

    @staticmethod
    def flyout_hwnd() -> int:
        """The share flyout's window (hosted by ShellExperienceHost), or 0."""
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = W.HWND
        h = user32.FindWindowW("Windows.UI.Core.CoreWindow", "Share")
        return h if h and user32.IsWindowVisible(h) else 0

    def _check_flyout(self) -> None:
        if self.flyout_hwnd() or time.monotonic() - self._watch_started > 6:
            self._watch.stop()
            self.window.hide()

    def _on_data_requested(self, sender, args) -> int:
        self.requests += 1
        request = _get(args, 6, "DataRequestedEventArgs.Request")
        try:
            if not self.items:
                with _HString(tr("Nothing to share")) as hs:
                    _call(request, 9, "FailWithDisplayText", [ctypes.c_void_p], hs)
                return S_OK
            data = _get(request, 6, "DataRequest.Data")
            try:
                props = _get(data, 7, "DataPackage.Properties")
                try:
                    n = len(self.paths)
                    title = self.paths[0].name if n == 1 else _count(n, N_("{n} item"), N_("{n} items"))
                    with _HString(title) as hs:
                        _call(props, 7, "put_Title", [ctypes.c_void_p], hs)
                    with _HString(tr("Shared with {app}").format(app=APP_NAME)) as hs:
                        _call(props, 9, "put_Description", [ctypes.c_void_p], hs)
                finally:
                    _release(props)
                iterable = _ItemIterable(list(self.items))
                try:
                    _call(data, 22, "SetStorageItems", [ctypes.c_void_p], iterable.ptr)
                finally:
                    iterable.release()
            finally:
                _release(data)
        finally:
            _release(request)
        return S_OK


_share: WindowsShare | None = None


def share_files(paths: list[Path]) -> None:
    """Open the Windows share sheet for `paths`."""
    global _share
    try:
        if _share is None:
            _share = WindowsShare()
        _share.share(paths)
    except OSError as e:
        log.exception("share failed")
        raise UserError(tr("Sharing is not available: {error}").format(error=e)) from e


def share_dialog(paths: list[Path], ctx) -> dict:
    share_files(paths)
    return {"done": True}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _opt(id: str, label: str, dialog, order: int, description: str, **kw) -> None:
    # label/description are N_()-marked English; Option.title / Option.text translate them.
    kw.setdefault("allow_dirs", True)
    kw.setdefault("files_only", False)
    add(Option(id=id, label=label, mode="actions", dialog=dialog, run=None, exts=None,
               multi=True, family=ACTION_COLOR_FAMILY, order=order, description=description, **kw))


_opt("action.share", N_("Share"), share_dialog, 10, N_("Share via Nearby Sharing, Mail, apps…"))
_opt("action.float", N_("Float"), float_dialog, 20, N_("Keep on the floating shelf"))
_opt("action.copy", N_("Copy"), copy_dialog, 30, N_("Copy to clipboard (paste in Explorer)"))
_opt("action.move", N_("Move"), move_dialog, 40, N_("Move to a folder…"))
_opt("action.copy_path", N_("Copy Path"), copy_path_dialog, 50, N_("Copy full paths as text"))
_opt("action.terminal", N_("Terminal"), terminal_dialog, 60, N_("Open a terminal here"))
_opt("action.reveal", N_("Reveal"), reveal_dialog, 70, N_("Show in Explorer"))
_opt("action.rename", N_("Rename"), rename_dialog, 80, N_("Batch rename with preview"))
_opt("action.hash", N_("Hash"), hash_dialog, 90, N_("SHA-256 / SHA-1 / MD5 checksums"),
     allow_dirs=False, files_only=True)
