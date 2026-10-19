"""Translations.

Source strings are written in English and wrapped in `tr()`:

    from .i18n import tr
    QLabel(tr("Compress images"))
    tr("Moved {n} items to {folder}").format(n=3, folder=name)

Each language has a JSON catalog in satsuma/locales/<code>.json mapping the English
text to its translation. Missing entries fall back to English. The language comes
from settings "language": "system" (follow Windows) or a code from LANGUAGES.

Strings stored in tables at import time are marked with N_() and translated with
tr() when shown. scripts/extract_strings.py collects every tr()/N_() literal plus
option labels and descriptions into the catalogs.

Rules for source strings: keep placeholders named (`{n}`, `{name}`), never build
sentences by concatenating translated pieces, and never pass f-strings to tr().
"""
from __future__ import annotations

import json
import locale
from pathlib import Path

LANGUAGES = {
    "en": "English",
    "zh": "中文（简体）",
    "hi": "हिन्दी",
    "es": "Español",
    "ar": "العربية",
}
RTL_LANGUAGES = {"ar"}

_catalog: dict[str, str] = {}
_current = "en"


def locales_dir() -> Path:
    import sys

    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) / "satsuma" / "locales") if base else Path(__file__).parent / "locales"


def system_language() -> str:
    """Windows UI language mapped to one of LANGUAGES (English if unsupported)."""
    code = ""
    try:
        import ctypes

        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        code = locale.windows_locale.get(lcid, "")
    except Exception:
        code = (locale.getlocale()[0] or "")
    code = code.lower().replace("-", "_").split("_")[0]
    return code if code in LANGUAGES else "en"


def resolve(setting: str | None) -> str:
    if not setting or setting == "system" or setting not in LANGUAGES:
        return system_language()
    return setting


def set_language(code: str) -> None:
    """Load the catalog for `code` (already resolved, e.g. 'ar')."""
    global _catalog, _current
    _current = code if code in LANGUAGES else "en"
    _catalog = {}
    if _current != "en":
        try:
            _catalog = json.loads((locales_dir() / f"{_current}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _catalog = {}


def current() -> str:
    return _current


def is_rtl() -> bool:
    return _current in RTL_LANGUAGES


def tr(text: str) -> str:
    return _catalog.get(text) or text


def N_(text: str) -> str:
    """Mark a string for translation without translating it yet (for tables built at
    import time). Pass the value through tr() when it is shown."""
    return text


def init_from_settings() -> str:
    from .config import settings

    code = resolve(settings.get("language", "system"))
    set_language(code)
    return code
