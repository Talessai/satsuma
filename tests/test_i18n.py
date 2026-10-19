"""Translation catalogs and language switching."""
from __future__ import annotations

import json
import os
import string
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from satsuma import i18n

ROOT = Path(__file__).resolve().parents[1]


def _catalog(lang: str) -> dict:
    return json.loads((ROOT / "satsuma" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


def _fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}


def test_catalogs_complete_and_in_sync():
    """Every tr()/N_() string in the code has a translation with the same placeholders."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "extract_strings.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("lang", ["zh", "hi", "es", "ar"])
def test_translations_sane(lang):
    cat = _catalog(lang)
    for key, value in cat.items():
        assert _fields(key) == _fields(value), (key, value)
        for tag in ("<b>", "<br>"):
            assert key.count(tag) == value.count(tag), (key, value)
        if key.startswith("{name}") and "-" in key:  # output-name templates
            assert not set('<>:"/\\|?*') & set(value), value


_FIT_SCRIPT = r"""
import json, sys
from pathlib import Path
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication
app = QApplication([])
from satsuma import i18n
from satsuma.wheel import INNER_R, RING_CAPACITY, RING_W, fit_label, label_font, label_width
labels = json.loads(Path("scripts/wedge_labels.json").read_text(encoding="utf-8"))
cut = {}
# Worst case: the top wedge of a full inner ring.
box = QRectF(0, 0, label_width(INNER_R, INNER_R + RING_W, RING_CAPACITY, 0), 36)
for lang in ("en", "zh", "hi", "es", "ar"):
    i18n.set_language(lang)
    bad = [i18n.tr(k) for k in labels
           if fit_label(label_font(1.0), i18n.tr(k).upper(), box)[2] != i18n.tr(k).upper()]
    if bad:
        cut[lang] = bad
print(json.dumps(cut, ensure_ascii=True))
"""


def test_wedge_labels_fit():
    """Every wedge label fits its wedge (one or two lines) at the wheel's real font.

    Measured in a subprocess on the real Windows platform: the offscreen platform the
    other tests use has no system fonts, so its metrics are wrong.
    """
    env = dict(os.environ, QT_QPA_PLATFORM="windows")
    r = subprocess.run([sys.executable, "-c", _FIT_SCRIPT], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {}


def test_system_language_mapping(monkeypatch):
    import ctypes

    for lcid, expected in ((0x0401, "ar"), (0x0804, "zh"), (0x0439, "hi"), (0x0C0A, "es"),
                           (0x0409, "en"), (0x040C, "en")):  # French is unsupported -> English
        monkeypatch.setattr(ctypes.windll.kernel32, "GetUserDefaultUILanguage", lambda v=lcid: v)
        assert i18n.system_language() == expected


def test_switching_language_and_rtl():
    try:
        i18n.set_language("ar")
        assert i18n.is_rtl() and i18n.tr("Drop files here") != "Drop files here"
        i18n.set_language("zh")
        assert not i18n.is_rtl() and i18n.tr("Cancel") != "Cancel"
        assert i18n.tr("no such string") == "no such string"
    finally:
        i18n.set_language("en")
    assert i18n.tr("Cancel") == "Cancel"


def test_wheel_paints_in_every_language(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from satsuma import registry
    from satsuma.wheel import Wheel

    app = QApplication.instance() or QApplication([])
    registry.load_all()
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"x")
    try:
        for lang in ("en", "zh", "hi", "es", "ar"):
            i18n.set_language(lang)
            app.setLayoutDirection(Qt.RightToLeft if i18n.is_rtl() else Qt.LeftToRight)
            w = Wheel()
            w.paths = [jpg]
            for mode in ("convert", "tools", "actions"):
                w.mode = mode
                w._refresh_options()
                w.hover = 0
                assert not w.grab().isNull()
    finally:
        i18n.set_language("en")
        app.setLayoutDirection(Qt.LeftToRight)
