"""User settings, stored as JSON in %APPDATA%\\Satsuma\\settings.json."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .i18n import N_

APP_NAME = "Satsuma"
ACCENT = "#FF8A1F"

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "satsuma.log"

# Keys that can open a wheel while dragging. Values are Windows virtual-key codes
# as reported by a low-level keyboard hook (left/right variants included).
KEY_CHOICES: dict[str, tuple[int, ...]] = {
    "shift": (0x10, 0xA0, 0xA1),
    "ctrl": (0x11, 0xA2, 0xA3),
    "alt": (0x12, 0xA4, 0xA5),
    "capslock": (0x14,),
    "tab": (0x09,),
    "space": (0x20,),
    "f1": (0x70,), "f2": (0x71,), "f3": (0x72,), "f4": (0x73,),
}
KEY_LABELS = {
    "shift": "Shift", "ctrl": "Ctrl", "alt": "Alt", "capslock": N_("Caps Lock"),
    "tab": N_("Tab"), "space": N_("Space"), "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
}

MODES = ("convert", "tools", "actions")
MODE_TITLES = {"convert": N_("Convert formats"), "tools": N_("File tools"), "actions": N_("File actions")}

DEFAULTS: dict = {
    "keys": {"convert": "shift", "tools": "ctrl", "actions": "alt"},
    "enabled": True,
    "language": "system",  # "system" or a code from i18n.LANGUAGES
    # "same" = next to the source file, "folder" = output_folder below
    "output_location": "same",
    "output_folder": str(Path.home() / "Downloads"),
    "reveal_after": False,
    "jpeg_quality": 90,
    "image_dpi": 300,
    "terminal": "auto",  # auto | wt | powershell | cmd
    "start_with_windows": False,
    "wheel_size": 1.0,
}


class Settings:
    """Thread-safe settings dict with dotted get/set and JSON persistence."""

    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self._lock:
            _merge(self._data, stored)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            text = json.dumps(self._data, indent=2)
        self.path.write_text(text, encoding="utf-8")

    def get(self, key: str, default=None):
        with self._lock:
            node = self._data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def set(self, key: str, value) -> None:
        with self._lock:
            node = self._data
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    def mode_for_vk(self, vk: int) -> str | None:
        for mode in MODES:
            if vk in KEY_CHOICES.get(self.get(f"keys.{mode}"), ()):
                return mode
        return None


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


settings = Settings()
