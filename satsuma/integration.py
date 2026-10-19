"""Windows shell integration: start with Windows, Send To shortcut, Explorer context menu,
and restarting the app (e.g. after changing the UI language).

Everything is per-user (HKCU / %APPDATA%), so no admin rights are needed. Text that
Explorer shows (the context-menu verb, the shortcut's tooltip) is written in the UI
language at install time; registry key/value names and file names are never translated.
"""
from __future__ import annotations

import os
import subprocess
import sys
import winreg
from pathlib import Path

from .config import APP_NAME
from .i18n import N_, tr

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_NAME
MENU_TEMPLATE = N_("Open with {app} wheel")
MENU_LABEL = MENU_TEMPLATE.format(app=APP_NAME)   # English; see menu_label() for the UI language
SENDTO_TEMPLATE = N_("Open the {app} wheel for these files")
MENU_CLASSES = (r"*", r"Directory")


def tr_in(lang: str | None, text: str) -> str:
    """`text` translated into `lang` (a code from i18n.LANGUAGES); None = the current UI language.

    Used to relabel shell entries for a newly chosen language before the app restarts.
    """
    if lang is None:
        return tr(text)
    from . import i18n

    if lang == i18n.current():
        return tr(text)
    if lang == "en" or lang not in i18n.LANGUAGES:
        return text
    import json

    try:
        catalog = json.loads((i18n.locales_dir() / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return text
    return catalog.get(text) or text


def menu_label(lang: str | None = None) -> str:
    """The Explorer context-menu verb ("Open with Satsuma wheel") in the UI language (or `lang`)."""
    return tr_in(lang, MENU_TEMPLATE).format(app=APP_NAME)


# ---- commands -----------------------------------------------------------------

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """The folder that contains the `satsuma` package (source runs only)."""
    return Path(__file__).resolve().parent.parent


def pythonw_exe() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def launch_command() -> list[str]:
    """How to start the app: [exe] when frozen, else [pythonw, -m, satsuma].

    File paths appended to this command open the wheel in click mode. The source
    form needs the project root as working directory (the Send To shortcut sets it).
    """
    if is_frozen():
        return [sys.executable]
    return [pythonw_exe(), "-m", "satsuma"]


def startup_command() -> list[str]:
    """Like launch_command() but independent of the working directory.

    Registry commands (Run key, context menu) cannot set a working directory, so a
    source checkout is put on sys.path explicitly.
    """
    if is_frozen():
        return [sys.executable]
    boot = (f"import sys,runpy;sys.path.insert(0,{str(project_root())!r});"
            "runpy.run_module('satsuma',run_name='__main__',alter_sys=True)")
    return [pythonw_exe(), "-c", boot]


def command_line(args: list[str]) -> str:
    return subprocess.list2cmdline(args)


def icon_location() -> str:
    if is_frozen():
        return sys.executable
    ico = project_root() / "satsuma" / "resources" / "satsuma.ico"
    return str(ico) if ico.exists() else pythonw_exe()


# ---- start with Windows -----------------------------------------------------------

def set_autostart(enabled: bool, value_name: str = RUN_VALUE) -> None:
    """Add/remove the HKCU Run entry that starts the app at sign-in."""
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command_line(startup_command()))
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass


def autostart_command(value_name: str = RUN_VALUE) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _type = winreg.QueryValueEx(key, value_name)
            return str(value)
    except OSError:
        return None


def is_autostart(value_name: str = RUN_VALUE) -> bool:
    return autostart_command(value_name) is not None


# ---- Send To --------------------------------------------------------------------------

def sendto_dir() -> Path:
    try:
        from win32com.shell import shell, shellcon

        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_SENDTO, None, 0))
    except Exception:
        return Path(os.environ.get("APPDATA", Path.home())) / "Microsoft" / "Windows" / "SendTo"


def send_to_path() -> Path:
    return sendto_dir() / f"{APP_NAME}.lnk"


def install_send_to(path: Path | None = None, lang: str | None = None) -> Path:
    """Create the "Send to > Satsuma" shortcut (its tooltip in the UI language or `lang`).

    Returns its path. The shortcut's file name is the app name in every language.
    """
    import pythoncom
    import win32com.client

    lnk = Path(path) if path else send_to_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    cmd = launch_command()
    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(lnk))
    sc.TargetPath = cmd[0]
    sc.Arguments = command_line(cmd[1:])
    sc.WorkingDirectory = str(Path(cmd[0]).parent if is_frozen() else project_root())
    sc.IconLocation = icon_location()
    sc.Description = tr_in(lang, SENDTO_TEMPLATE).format(app=APP_NAME)
    sc.Save()
    return lnk


def remove_send_to(path: Path | None = None) -> bool:
    lnk = Path(path) if path else send_to_path()
    try:
        lnk.unlink()
        return True
    except FileNotFoundError:
        return False


def has_send_to(path: Path | None = None) -> bool:
    return (Path(path) if path else send_to_path()).exists()


def read_shortcut(path: Path) -> dict:
    """TargetPath / Arguments / WorkingDirectory of a .lnk (for tests and diagnostics)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    return {"target": sc.TargetPath, "arguments": sc.Arguments, "workdir": sc.WorkingDirectory}


# ---- Explorer context menu (optional, off by default) ----------------------------------

def _menu_key(cls: str, name: str) -> str:
    return rf"Software\Classes\{cls}\shell\{name}"


def install_context_menu(name: str = APP_NAME, label: str | None = None) -> None:
    """Add "Open with Satsuma wheel" to files' and folders' context menus (HKCU).

    `label` defaults to menu_label() - the verb in the current UI language. `name` is the
    registry key name and is never translated.

    MultiSelectModel=Player lets the verb show for multi-selections; Explorer may still
    start one process per item, which the single-instance app forwards to the wheel.
    """
    label = label or menu_label()
    cmd = command_line(startup_command()) + ' "%1"'
    for cls in MENU_CLASSES:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _menu_key(cls, name), 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_location())
            winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _menu_key(cls, name) + r"\command", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)


def remove_context_menu(name: str = APP_NAME) -> None:
    for cls in MENU_CLASSES:
        for sub in (r"\command", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _menu_key(cls, name) + sub)
            except FileNotFoundError:
                pass


def has_context_menu(name: str = APP_NAME) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _menu_key("*", name) + r"\command"):
            return True
    except OSError:
        return False


def context_menu_command(name: str = APP_NAME, cls: str = "*") -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _menu_key(cls, name) + r"\command") as k:
            return str(winreg.QueryValueEx(k, "")[0])
    except OSError:
        return None


def context_menu_label(name: str = APP_NAME, cls: str = "*") -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _menu_key(cls, name)) as k:
            return str(winreg.QueryValueEx(k, "")[0])
    except OSError:
        return None


def relabel_shell_entries(lang: str) -> None:
    """Rewrite the installed context-menu verb and Send To tooltip in language `lang`."""
    if has_context_menu():
        install_context_menu(label=menu_label(lang))
    if has_send_to():
        install_send_to(lang=lang)


# ---- restart ---------------------------------------------------------------------------------

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def restart_command() -> list[str]:
    """launch_command() + ["--restart"] (frozen: [exe, "--restart"]).

    The new instance sees --restart and waits for this one to exit before it takes over
    the single-instance server.
    """
    return launch_command() + ["--restart"]


def restart_app() -> None:
    """Start a detached new instance of the app, then quit this one."""
    from PySide6.QtWidgets import QApplication

    cmd = restart_command()
    # The source form (`-m satsuma`) needs the project root as working directory.
    cwd = str(Path(cmd[0]).parent) if is_frozen() else str(project_root())
    subprocess.Popen(cmd, cwd=cwd, close_fds=True, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    QApplication.quit()
