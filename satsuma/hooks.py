"""Global low-level mouse/keyboard hooks that notice "drag in progress + key tap".

Runs SetWindowsHookEx(WH_MOUSE_LL / WH_KEYBOARD_LL) on its own thread with a
message loop. Signals are emitted from that thread; Qt queues them to the GUI.
The hook callbacks only update a few fields and never block, so Windows'
low-level hook timeout is never hit.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

from PySide6.QtCore import QObject, Signal

from .config import settings

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x200, 0x201, 0x202
VK_ESCAPE = 0x1B
NUDGE_MARK = 0x5A75  # dwExtraInfo tag on our own synthetic mouse moves
DRAG_THRESHOLD = 6  # px of movement with the button held before it counts as a drag

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class DragKeyHook(QObject):
    """Emits `key_during_drag(mode)` when a wheel key is tapped mid-drag."""

    key_during_drag = Signal(str)
    drag_ended = Signal()
    escape_pressed = Signal()

    def __init__(self):
        super().__init__()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._button_down = False
        self._down_pt = (0, 0)
        self.dragging = False
        self._held: set[int] = set()
        # Keep references so ctypes callbacks are not garbage collected.
        self._mouse_cb = HOOKPROC(self._on_mouse)
        self._kb_cb = HOOKPROC(self._on_key)
        self._hooks: list[int] = []

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="satsuma-hooks", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        self._hooks = [
            user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_cb, hmod, 0),
            user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, hmod, 0),
        ]
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for h in self._hooks:
            if h:
                user32.UnhookWindowsHookEx(h)

    def _on_mouse(self, n_code, w_param, l_param):
        if n_code >= 0:
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            # Ignore the 1px nudges the wheel injects to wake up the OLE drag loop.
            if info.dwExtraInfo != NUDGE_MARK:
                if w_param == WM_LBUTTONDOWN:
                    self._button_down = True
                    self._down_pt = (info.pt.x, info.pt.y)
                    self.dragging = False
                elif w_param == WM_MOUSEMOVE and self._button_down and not self.dragging:
                    dx = info.pt.x - self._down_pt[0]
                    dy = info.pt.y - self._down_pt[1]
                    if dx * dx + dy * dy >= DRAG_THRESHOLD * DRAG_THRESHOLD:
                        self.dragging = True
                elif w_param == WM_LBUTTONUP:
                    was = self.dragging
                    self._button_down = False
                    self.dragging = False
                    if was:
                        self.drag_ended.emit()
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_key(self, n_code, w_param, l_param):
        if n_code >= 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = info.vkCode
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                first = vk not in self._held   # ignore auto-repeat
                self._held.add(vk)
                if first and self.dragging and settings.get("enabled", True):
                    if vk == VK_ESCAPE:
                        self.escape_pressed.emit()
                    else:
                        mode = settings.mode_for_vk(vk)
                        if mode:
                            self.key_during_drag.emit(mode)
            elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                self._held.discard(vk)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)


# --- helpers used by the wheel ---------------------------------------------

MOUSEEVENTF_MOVE = 0x0001


def nudge_mouse() -> None:
    """Move the cursor 1px and back so the OLE drag loop re-checks the window under it."""
    user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, NUDGE_MARK)
    user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, NUDGE_MARK)
