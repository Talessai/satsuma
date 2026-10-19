"""Tests for file actions, the shelf, the settings dialog and shell integration."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import hashlib
import json
import struct
import subprocess
import sys
import uuid
import winreg
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QApplication

from satsuma import integration, registry
from satsuma.actions import file_actions as fa
from satsuma.config import settings
from satsuma.registry import UserError


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    out = []
    for name, data in (("alpha.txt", b"hello"), ("beta.txt", b"world"), ("gamma photo.jpg", b"\xff\xd8x")):
        p = tmp_path / name
        p.write_bytes(data)
        out.append(p)
    return out


@pytest.fixture
def clipboard_guard():
    """Restore the user's clipboard text after a test that overwrites it."""
    import win32clipboard
    import win32con

    saved = None
    try:
        fa._open_clipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                saved = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    yield
    if saved is not None:
        try:
            fa.set_clipboard_text(saved)
        except Exception:
            pass


def _read_clipboard(fmt):
    import win32clipboard

    fa._open_clipboard()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(fmt):
            return None
        return win32clipboard.GetClipboardData(fmt)
    finally:
        win32clipboard.CloseClipboard()


# ---- registration ---------------------------------------------------------------

def test_options_registered(tmp_path, files):
    registry.load_all()
    ids = {"action.share", "action.float", "action.copy", "action.move", "action.copy_path",
           "action.terminal", "action.reveal", "action.rename", "action.hash"}
    for oid in ids:
        opt = registry.get(oid)
        assert opt.mode == "actions" and opt.dialog is not None and opt.run is None
    mixed = [files[0], tmp_path]
    found = {o.id for o in registry.options_for("actions", mixed)}
    assert ids - {"action.hash"} <= found
    assert "action.hash" not in found                        # files only
    assert "action.hash" in {o.id for o in registry.options_for("actions", files)}


# ---- clipboard -------------------------------------------------------------------

def test_copy_path_text(files, clipboard_guard):
    import win32con

    res = fa.copy_path_dialog(files, None)
    assert res["done"] and res["message"] == "Copied 3 paths"
    text = _read_clipboard(win32con.CF_UNICODETEXT)
    assert text.splitlines() == [str(p) for p in files]
    assert fa.copy_path_dialog(files[:1], None)["message"] == "Copied path"


def test_copy_files_hdrop(files, tmp_path, clipboard_guard):
    import win32clipboard
    import win32con

    sub = tmp_path / "a folder"
    sub.mkdir()
    items = files + [sub]
    res = fa.copy_dialog(items, None)
    assert res == {"done": True, "message": "Copied 4 items"}
    dropped = _read_clipboard(win32con.CF_HDROP)
    assert [Path(p) for p in dropped] == items
    effect = _read_clipboard(win32clipboard.RegisterClipboardFormat("Preferred DropEffect"))
    assert struct.unpack("<I", effect[:4])[0] == fa.DROPEFFECT_COPY


def test_dropfiles_layout():
    data = fa.dropfiles_bytes([Path(r"C:\a.txt"), Path(r"C:\b c.txt")])
    off, _x, _y, _nc, wide = struct.unpack("<IiiII", data[:20])
    assert off == 20 and wide == 1
    assert data[20:].decode("utf-16-le") == "C:\\a.txt\0C:\\b c.txt\0\0"


# ---- move ---------------------------------------------------------------------------

def test_move_with_conflict(files, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "alpha.txt").write_text("existing")
    folder = tmp_path / "stuff"
    folder.mkdir()
    (folder / "inner.txt").write_text("x")
    moved = fa.move_items([files[0], files[1], folder], dest)
    assert [p.name for p in moved] == ["alpha (1).txt", "beta.txt", "stuff"]
    assert (dest / "alpha.txt").read_text() == "existing"
    assert (dest / "alpha (1).txt").read_bytes() == b"hello"
    assert (dest / "stuff" / "inner.txt").exists()
    assert not files[0].exists() and not folder.exists()


def test_move_into_itself_and_same_folder(tmp_path, files):
    folder = tmp_path / "f"
    (folder / "child").mkdir(parents=True)
    with pytest.raises(UserError):
        fa.move_items([folder], folder / "child")
    assert fa.move_items([files[2]], tmp_path) == []           # already there: no-op
    assert files[2].exists()


def test_move_dialog_uses_picker(monkeypatch, files, tmp_path, qapp):
    from PySide6.QtWidgets import QFileDialog

    dest = tmp_path / "picked"
    dest.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(dest)))
    res = fa.move_dialog(files[:2], None)
    assert res["message"] == "Moved 2 items to picked"
    assert all(p.parent == dest for p in res["outputs"])
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    assert fa.move_dialog([files[2]], None) is None


# ---- rename ---------------------------------------------------------------------------

def test_rename_pattern_and_counter(files):
    plan = fa.rename_plan(files, pattern="{name}-{n}")
    assert [d.name for _s, d, _p in plan] == ["alpha-1.txt", "beta-2.txt", "gamma photo-3.jpg"]
    plan = fa.rename_plan(files, pattern="img_{n:03}", start=7)
    assert [d.name for _s, d, _p in plan] == ["img_007.txt", "img_008.txt", "img_009.jpg"]
    assert all(not p for _s, _d, p in plan)


def test_rename_find_replace_and_case(files):
    plan = fa.rename_plan(files, find=" photo", replace="", case="upper")
    assert [d.name for _s, d, _p in plan] == ["ALPHA.TXT", "BETA.TXT", "GAMMA.JPG"]
    plan = fa.rename_plan(files, find=r"^(\w)", replace=r"x_\1", regex=True)
    assert [d.name for _s, d, _p in plan] == ["x_alpha.txt", "x_beta.txt", "x_gamma photo.jpg"]
    plan = fa.rename_plan(files, find="A", replace="o")                 # case-insensitive
    assert plan[0][1].name == "olpho.txt"
    plan = fa.rename_plan(files, find="A", replace="o", match_case=True)
    assert plan[0][1].name == "alpha.txt"
    plan = fa.rename_plan([files[2]], case="title")
    assert plan[0][1].name == "Gamma Photo.jpg"


def test_rename_conflicts_and_invalid(files, tmp_path):
    (tmp_path / "taken.txt").write_text("x")
    plan = fa.rename_plan(files[:1], pattern="taken")
    assert plan[0][2] == "already exists"
    plan = fa.rename_plan(files[:2], pattern="same")
    assert [p for _s, _d, p in plan] == ["duplicate name", "duplicate name"]
    assert fa.rename_plan(files[:1], pattern="a:b")[0][2] == "invalid character"
    assert fa.rename_plan(files[:1], pattern="con")[0][2] == "reserved name"
    with pytest.raises(UserError):
        fa.rename_plan(files, pattern="{nope}")
    with pytest.raises(UserError):
        fa.apply_renames(fa.rename_plan(files[:2], pattern="same"))


def test_rename_apply_swap(files):
    a, b = files[0], files[1]
    plan = [(a, b, ""), (b, a, "")]          # swap names: needs the two-phase rename
    # the plan builder would flag these as existing only if they were not sources
    out = fa.apply_renames(plan)
    assert out == [b, a]
    assert a.read_bytes() == b"world" and b.read_bytes() == b"hello"
    plan = fa.rename_plan([a, b], pattern="{name}-{n}")
    fa.apply_renames(plan)
    assert sorted(p.name for p in a.parent.iterdir() if p.suffix == ".txt") == ["alpha-1.txt", "beta-2.txt"]


# ---- hash ---------------------------------------------------------------------------------

def test_hash_values(tmp_path):
    p = tmp_path / "data.bin"
    data = os.urandom(3 * (1 << 20) + 123)    # spans several read chunks
    p.write_bytes(data)
    seen = []
    h = fa.file_hashes(p, progress=seen.append)
    assert h["sha256"] == hashlib.sha256(data).hexdigest()
    assert h["md5"] == hashlib.md5(data).hexdigest()
    assert h["sha1"] == hashlib.sha1(data).hexdigest()
    assert seen and seen[-1] == pytest.approx(1.0)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert fa.file_hashes(empty)["md5"] == "d41d8cd98f00b204e9800998ecf8427e"


# ---- terminal / reveal helpers ---------------------------------------------------------------

def test_terminal_command(tmp_path, files, monkeypatch):
    assert fa.target_folders(files + [tmp_path]) == [tmp_path]
    argv, cwd = fa.terminal_command(tmp_path, "cmd")
    assert argv[-1] == "/K" and cwd == str(tmp_path)
    argv, cwd = fa.terminal_command(tmp_path, "powershell")
    assert argv[0].lower().endswith("powershell.exe") and cwd == str(tmp_path)
    monkeypatch.setattr(fa, "_wt_exe", lambda: r"C:\wt.exe")
    assert fa.terminal_command(Path("C:\\"), "auto") == ([r"C:\wt.exe", "-d", "C:\\."], None)
    monkeypatch.setattr(fa, "_wt_exe", lambda: None)
    argv, _ = fa.terminal_command(tmp_path, "wt")          # falls back when not installed
    assert "wt" not in Path(argv[0]).name.lower()


# ---- share plumbing (no UI) --------------------------------------------------------------------

def test_pinterface_iids():
    # Known values from the Windows SDK headers.
    assert fa.IID_IIterable_IStorageItem == "bb8b8418-65d1-544b-b083-6d172f568c73"
    assert fa.pinterface_iid("faa585ea-6214-4217-afda-7f46de5869b3", "string") == \
        "e2fcc7c1-3bfc-5a0b-b2b0-72e769d1cb7e"


def test_storage_items_and_iterable(files, tmp_path):
    items = [fa._storage_item(files[0]), fa._storage_item(tmp_path)]
    try:
        iterable = fa._ItemIterable(items)
        it_ptr = fa._get(iterable.ptr, 6, "First")
        has = fa.ctypes.c_ubyte()
        seen = []
        while True:
            fa._call(it_ptr, 7, "HasCurrent", [fa.ctypes.POINTER(fa.ctypes.c_ubyte)], fa.ctypes.byref(has))
            if not has.value:
                break
            cur = fa._get(it_ptr, 6, "Current")
            seen.append(cur)
            fa._release(cur)
            fa._call(it_ptr, 8, "MoveNext", [fa.ctypes.POINTER(fa.ctypes.c_ubyte)], fa.ctypes.byref(has))
        fa._release(it_ptr)
        iterable.release()
        assert seen == items
        assert iterable not in fa._ComObject._alive          # freed once COM let go
        # QueryInterface answers for the WinRT IIDs and refuses others.
        it2 = fa._ItemIterable(items)
        p = fa._qi(it2.ptr, fa.IID_IIterable_IStorageItem)
        assert p == it2.ptr
        with pytest.raises(OSError):
            fa._qi(p, "00000003-0000-0000-c000-000000000046")   # IMarshal
        fa._release(p)
        it2.release()
        assert it2 not in fa._ComObject._alive
    finally:
        for p in items:
            fa._release(p)
    with pytest.raises(OSError):
        fa._storage_item(tmp_path / "missing.txt")


# ---- shelf ------------------------------------------------------------------------------------------

def test_shelf_items(qapp, files, tmp_path):
    from satsuma.actions.shelf import Shelf, get_shelf, local_paths, urls_mime

    shelf = Shelf()
    assert shelf.add_paths(files) == 3
    assert shelf.add_paths([files[0], str(files[1])]) == 0          # duplicates ignored
    assert shelf.add_paths([tmp_path]) == 1
    assert shelf.paths == files + [tmp_path]
    assert shelf.count_label.text() == "· 4"
    assert shelf.remove_paths([files[1]]) == 1
    assert shelf.paths == [files[0], files[2], tmp_path]
    mime = shelf.list.mimeData([shelf.list.item(0), shelf.list.item(1)])
    assert local_paths(mime) == [files[0], files[2]]
    files[2].unlink()
    shelf.remove_missing()
    assert shelf.paths == [files[0], tmp_path]
    shelf.clear()
    assert shelf.paths == [] and shelf.hint.isVisibleTo(shelf)
    assert local_paths(urls_mime(files[:1])) == files[:1]
    assert get_shelf() is get_shelf()


def test_float_action_adds_to_shelf(qapp, files):
    from satsuma.actions.shelf import get_shelf

    shelf = get_shelf()
    shelf.clear()
    assert fa.float_dialog(files[:2], None) == {"done": True}
    assert shelf.paths == files[:2] and shelf.isVisible()
    shelf.clear()
    shelf.hide()


def test_shelf_snapping(qapp):
    from satsuma.actions.shelf import PANEL_SIZE, TAB_THICK, Shelf, docked_rect, snap_edge

    screen = QRect(0, 0, 1920, 1040)
    assert snap_edge(QRect(5, 300, 270, 330), screen) == "left"
    assert snap_edge(QRect(1920 - 270 - 10, 300, 270, 330), screen) == "right"
    assert snap_edge(QRect(600, 2, 270, 330), screen) == "top"
    assert snap_edge(QRect(600, 1040 - 330 - 3, 270, 330), screen) == "bottom"
    assert snap_edge(QRect(600, 300, 270, 330), screen) is None
    r = docked_rect("right", 500, screen, collapsed=True)
    assert r.right() == screen.right() and r.width() == TAB_THICK
    r = docked_rect("bottom", 5000, screen, collapsed=False)
    assert r.bottom() == screen.bottom() and r.right() == screen.right() and r.size() == PANEL_SIZE

    shelf = Shelf()
    shelf.show()
    shelf.move(10, 200)
    shelf.pin("left")
    assert shelf.edge == "left" and shelf.x() == shelf._screen_geo().left()
    shelf.collapse()
    assert shelf.collapsed and shelf.width() == TAB_THICK
    shelf.expand()
    assert not shelf.collapsed and shelf.size() == PANEL_SIZE
    shelf.pin(None)
    shelf.collapse()                       # unpinned shelves never collapse
    assert not shelf.collapsed
    shelf.hide()


# ---- settings dialog ----------------------------------------------------------------------------------

@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    saved = copy.deepcopy(settings._data)
    monkeypatch.setattr(settings, "path", tmp_path / "settings.json")
    calls = []
    for name in ("set_autostart", "install_send_to", "remove_send_to", "install_context_menu",
                 "remove_context_menu"):
        monkeypatch.setattr(integration, name, lambda *a, _n=name, **k: calls.append((_n, a)))
    for name in ("is_autostart", "has_send_to", "has_context_menu"):
        monkeypatch.setattr(integration, name, lambda *a, **k: False)
    yield calls
    settings._data = saved


def test_settings_dialog_saves(qapp, temp_settings, tmp_path):
    from satsuma.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    assert dlg.tabs.count() == 5
    # keys: picking another wheel's key swaps them
    keys0 = dlg.keys()
    combo = dlg.key_combos["convert"]
    combo.setCurrentIndex(combo.findData(keys0["tools"]))
    keys = dlg.keys()
    assert keys["convert"] == keys0["tools"] and keys["tools"] == keys0["convert"]
    assert len(set(keys.values())) == 3
    dlg.out_folder.setChecked(True)
    dlg.folder_edit.setText(str(tmp_path / "out"))
    dlg.jpeg_quality.setValue(77)
    dlg.image_dpi.setValue(150)
    dlg.wheel_size.setValue(120)
    dlg.reveal_after.setChecked(True)
    dlg.terminal.setCurrentIndex(dlg.terminal.findData("cmd"))
    dlg.autostart.setChecked(True)
    assert dlg.apply() == []
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["keys"] == keys
    assert stored["output_location"] == "folder" and stored["output_folder"] == str(tmp_path / "out")
    assert stored["jpeg_quality"] == 77 and stored["image_dpi"] == 150
    assert stored["wheel_size"] == 1.2 and stored["reveal_after"] is True
    assert stored["terminal"] == "cmd" and stored["start_with_windows"] is True
    assert temp_settings == [("set_autostart", (True,))]
    # About tab counts options per mode
    registry.load_all()
    n_actions = sum(1 for o in registry.all_options() if o.mode == "actions")
    assert dlg.count_labels["actions"].text().count(str(n_actions)) >= 1
    dlg.deleteLater()


def test_open_settings_single_instance(qapp, temp_settings):
    from satsuma.ui import settings_dialog as sd

    a = sd.open_settings(modal=False)
    b = sd.open_settings(modal=False)
    assert a is b and a.isVisible()
    a.reject()
    c = sd.open_settings(modal=False)
    assert c is not a
    c.reject()


# ---- integration (restores state) ---------------------------------------------------------------

def test_launch_command():
    cmd = integration.launch_command()
    assert cmd[1:] == ["-m", "satsuma"] and Path(cmd[0]).name.lower() in ("pythonw.exe", "python.exe")
    boot = integration.startup_command()
    assert boot[1] == "-c"
    # The bootstrap must find the package from any working directory.
    code = boot[2].replace("runpy.run_module('satsuma',run_name='__main__',alter_sys=True)",
                           "import satsuma;print(satsuma.__file__)")
    out = subprocess.run([sys.executable, "-c", code], cwd=os.environ.get("SystemRoot", "C:\\"),
                         capture_output=True, text=True, timeout=60)
    assert Path(out.stdout.strip()).parent == integration.project_root() / "satsuma"


def test_autostart_round_trip():
    name = f"SatsumaTest-{uuid.uuid4().hex[:8]}"
    try:
        assert not integration.is_autostart(name)
        integration.set_autostart(True, name)
        assert integration.is_autostart(name)
        assert integration.autostart_command(name) == integration.command_line(integration.startup_command())
        integration.set_autostart(False, name)
        assert not integration.is_autostart(name)
        integration.set_autostart(False, name)      # idempotent
    finally:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, integration.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass


def test_send_to_round_trip(tmp_path):
    lnk = tmp_path / "Satsuma.lnk"
    assert not integration.has_send_to(lnk)
    integration.install_send_to(lnk)
    assert integration.has_send_to(lnk)
    info = integration.read_shortcut(lnk)
    cmd = integration.launch_command()
    assert Path(info["target"]) == Path(cmd[0])
    assert info["arguments"] == "-m satsuma"
    assert Path(info["workdir"]) == integration.project_root()
    assert integration.remove_send_to(lnk) and not integration.has_send_to(lnk)
    assert not integration.remove_send_to(lnk)
    assert integration.send_to_path().name == "Satsuma.lnk"
    assert integration.send_to_path().parent.name.lower() == "sendto"


def test_context_menu_round_trip():
    name = f"SatsumaTest{uuid.uuid4().hex[:8]}"
    try:
        assert not integration.has_context_menu(name)
        integration.install_context_menu(name)
        assert integration.has_context_menu(name)
        for cls in integration.MENU_CLASSES:
            cmd = integration.context_menu_command(name, cls)
            assert cmd.endswith(' "%1"') and "-c" in cmd
    finally:
        integration.remove_context_menu(name)
    assert not integration.has_context_menu(name)
    assert integration.context_menu_command(name, "Directory") is None


def test_context_menu_label_language(monkeypatch):
    from satsuma import i18n

    assert integration.menu_label() == "Open with Satsuma wheel" == integration.MENU_LABEL
    # Another language's label comes from its catalog file (missing entries fall back to English).
    monkeypatch.setattr(i18n, "locales_dir", lambda: Path(__file__).parent / "no-such-dir")
    assert integration.menu_label("ar") == "Open with Satsuma wheel"
    name = f"SatsumaTest{uuid.uuid4().hex[:8]}"
    try:
        integration.install_context_menu(name, label="فتح عجلة Satsuma")
        assert integration.context_menu_label(name) == "فتح عجلة Satsuma"
        assert integration.context_menu_label(name, "Directory") == "فتح عجلة Satsuma"
    finally:
        integration.remove_context_menu(name)


# ---- language setting / restart -------------------------------------------------------------------

def test_restart_app(monkeypatch):
    from PySide6.QtWidgets import QApplication

    popen, quits = [], []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: popen.append((argv, kw)))
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    integration.restart_app()
    (argv, kw), = popen
    assert argv == integration.launch_command() + ["--restart"]
    assert kw["cwd"] == str(integration.project_root())
    assert kw["creationflags"] & integration.DETACHED_PROCESS
    assert quits == [True]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\Satsuma\Satsuma.exe")
    assert integration.restart_command() == [r"C:\Apps\Satsuma\Satsuma.exe", "--restart"]


def test_language_combo_saves_and_restarts(qapp, temp_settings, monkeypatch, tmp_path):
    from satsuma import i18n
    from satsuma.ui.settings_dialog import SettingsDialog

    monkeypatch.setattr(i18n, "system_language", lambda: "es")
    asks, restarts = [], []
    answer = {"restart": True}
    monkeypatch.setattr(SettingsDialog, "_ask_restart", lambda self: asks.append(1) or answer["restart"])
    monkeypatch.setattr(integration, "restart_app", lambda: restarts.append(1))

    def stored():
        return json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["language"]

    settings.set("language", "system")
    dlg = SettingsDialog()
    combo = dlg.language
    assert combo.count() == 1 + len(i18n.LANGUAGES) == 6
    assert [combo.itemData(i) for i in range(combo.count())] == ["system", "en", "zh", "hi", "es", "ar"]
    assert combo.itemText(0) == "System default (Español)"
    assert [combo.itemText(i) for i in range(1, 6)] == list(i18n.LANGUAGES.values())
    assert combo.currentData() == "system"

    # Arabic differs from the running language (English): ask, then restart.
    combo.setCurrentIndex(combo.findData("ar"))
    dlg.save_and_close()
    assert stored() == "ar" and asks == [1] and restarts == [1]
    assert temp_settings == []                 # no shell entries installed: nothing to relabel

    # Back to "system" (Spanish here): still a change from English; "Later" does not restart.
    answer["restart"] = False
    dlg = SettingsDialog()
    assert dlg.language.currentData() == "ar"
    dlg.language.setCurrentIndex(dlg.language.findData("system"))
    dlg.save_and_close()
    assert stored() == "system" and asks == [1, 1] and restarts == [1]

    # Choosing the running language explicitly: saved, no prompt.
    dlg = SettingsDialog()
    dlg.language.setCurrentIndex(dlg.language.findData("en"))
    dlg.save_and_close()
    assert stored() == "en" and asks == [1, 1]

    # Saving again without touching the language never prompts.
    dlg = SettingsDialog()
    dlg.save_and_close()
    assert asks == [1, 1]

    # An installed context menu is relabelled in the new language.
    monkeypatch.setattr(integration, "has_context_menu", lambda *a, **k: True)
    dlg = SettingsDialog()
    dlg.language.setCurrentIndex(dlg.language.findData("hi"))
    assert dlg.apply() == [] and dlg.language_changed
    assert ("install_context_menu", ()) in temp_settings


# ---- right-to-left / translated UI ---------------------------------------------------------------------

FAKE_AR = {
    "Settings — {app}": "الإعدادات — {app}",
    "General": "عام",
    "System default ({name})": "افتراضي النظام ({name})",
    "Restart {app} now to apply the language?": "هل تريد إعادة تشغيل {app} الآن لتطبيق اللغة؟",
    "Restart now": "إعادة التشغيل الآن",
    "Later": "لاحقاً",
    "Rename": "إعادة تسمية",
    "{n} items · placeholders: {tokens}": "{n} عناصر · العناصر النائبة: {tokens}",
    "Keep case": "إبقاء حالة الأحرف",
    "Current name": "الاسم الحالي",
    "invalid character": "حرف غير صالح",
    "Checksums": "المجاميع الاختبارية",
    "Copy {algo}": "نسخ {algo}",
    "Close": "إغلاق",
    "Moved {n} items to {folder}": "تم نقل {n} عناصر إلى {folder}",
    "Opening Share…": "جارٍ فتح المشاركة…",
    "Shelf": "الرف",
    "Share": "مشاركة",
}


@pytest.fixture
def arabic(qapp):
    """UI in Arabic (right to left) with a tiny fake catalog; back to English/LTR afterwards."""
    from PySide6.QtCore import Qt

    from satsuma import i18n

    with pytest.MonkeyPatch.context() as mp:
        i18n.set_language("ar")
        mp.setattr(i18n, "_catalog", dict(FAKE_AR))
        qapp.setLayoutDirection(Qt.RightToLeft)
        try:
            yield
        finally:
            qapp.setLayoutDirection(Qt.LeftToRight)
    i18n.set_language("en")
    assert not i18n.is_rtl() and i18n.tr("Rename") == "Rename"


def test_settings_dialog_rtl(arabic, temp_settings, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    from satsuma import i18n
    from satsuma.ui.settings_dialog import SettingsDialog

    assert i18n.is_rtl()
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    dlg = SettingsDialog()
    assert dlg.layoutDirection() == Qt.RightToLeft
    assert dlg.windowTitle() == "الإعدادات — Satsuma"
    assert dlg.tabs.tabText(0) == "عام" and dlg.tabs.tabText(1) == "Keys"     # untranslated: English
    assert dlg.language.itemText(0) == "افتراضي النظام (English)"
    assert dlg.language.itemText(5) == "العربية"
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self) or 0)
    assert dlg._ask_restart() is False            # closed without choosing: later
    box, = shown
    assert box.text() == "هل تريد إعادة تشغيل Satsuma الآن لتطبيق اللغة؟"
    assert sorted(b.text() for b in box.buttons()) == sorted(["إعادة التشغيل الآن", "لاحقاً"])
    dlg.deleteLater()


def test_action_dialogs_rtl(arabic, monkeypatch, files, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFileDialog, QLabel

    from satsuma.ui.common import ToolDialog

    opened = []
    monkeypatch.setattr(ToolDialog, "exec", lambda self: opened.append(self) or 0)
    assert fa.rename_dialog(files, None) is None          # cancelled
    dlg = opened.pop()
    assert dlg.layoutDirection() == Qt.RightToLeft
    texts = [w.text() for w in dlg.findChildren(QLabel)]
    assert "إعادة تسمية" in texts
    # placeholders stay as-is; only the help text around them is translated
    assert f"3 عناصر · العناصر النائبة: {fa.PATTERN_TOKENS}" in texts
    assert "{name} {n} {n:03} {ext} {parent} {date}" == fa.PATTERN_TOKENS
    assert dlg.primary_btn.text() == "إعادة تسمية"
    assert [dlg.case.itemData(i) for i in range(dlg.case.count())] == ["keep", "lower", "upper", "title"]
    assert dlg.case.itemText(0) == "إبقاء حالة الأحرف" and dlg.case.itemText(1) == "lowercase"
    assert dlg.table.horizontalHeaderItem(0).text() == "الاسم الحالي"
    dlg.pattern.setText("a:b")
    assert dlg.table.item(0, 1).text().endswith("⚠ حرف غير صالح")
    assert fa.rename_plan(files[:1], pattern="a:b")[0][2] == "invalid character"   # stable value
    assert not dlg.primary_btn.isEnabled()

    assert fa.hash_dialog(files[:1], None) == {"done": True}
    dlg = opened.pop()
    dlg.worker.wait(5000)
    assert dlg.copy256.text() == "نسخ SHA-256" and dlg.cancel_btn.text() == "إغلاق"
    assert "المجاميع الاختبارية" in [w.text() for w in dlg.findChildren(QLabel)]
    dlg.done(0)

    dest = tmp_path / "picked"
    dest.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(dest)))
    assert fa.move_dialog(files[:2], None)["message"] == "تم نقل 2 عناصر إلى picked"

    card = fa.share_card()
    label = card.findChild(QLabel, "shareText")
    assert label.text() == "جارٍ فتح المشاركة…" and label.wordWrap()
    assert label.alignment() & Qt.AlignHCenter and label.alignment() & Qt.AlignVCenter
    assert card.layoutDirection() == Qt.RightToLeft
    card.deleteLater()

    registry.load_all()
    assert registry.get("action.share").title == "مشاركة"
    assert registry.get("action.float").title == "Float"       # not in the fake catalog


def test_shelf_rtl_pins_to_physical_edge(arabic):
    from PySide6.QtCore import Qt

    from satsuma.actions.shelf import TAB_THICK, Shelf, snap_edge

    shelf = Shelf()
    assert shelf.layoutDirection() == Qt.RightToLeft
    shelf.show()
    geo = shelf._screen_geo()
    # dragged to the physical right edge: pins (and collapses) on the right, not the mirrored left
    shelf.move(geo.right() - shelf.width() - 5, geo.top() + 50)
    edge = snap_edge(shelf.frameGeometry(), geo)
    assert edge == "right"
    shelf.pin(edge)
    assert shelf.geometry().right() == geo.right()
    shelf.collapse()
    assert shelf.collapsed and shelf.width() == TAB_THICK and shelf.geometry().right() == geo.right()
    shelf.expand()
    shelf.pin(None)
    shelf.move(geo.left() + 3, geo.top() + 50)
    edge = snap_edge(shelf.frameGeometry(), geo)
    assert edge == "left"
    shelf.pin(edge)
    assert shelf.x() == geo.left()
    shelf.collapse()
    assert shelf.geometry().left() == geo.left()
    shelf.hide()
    shelf.deleteLater()
