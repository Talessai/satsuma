"""Core checks: plugin list, registry filtering, output paths, wheel geometry."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import satsuma
from satsuma import paths, registry


def test_plugin_list_matches_files():
    root = Path(satsuma.__file__).parent
    on_disk = {f"{pkg}.{f.stem}" for pkg in ("converters", "tools", "actions")
               for f in (root / pkg).glob("*.py") if f.stem != "__init__"}
    assert set(registry.PLUGIN_MODULES) == on_disk


def test_all_plugins_load():
    registry.load_all()
    assert registry.load_errors == {}
    ids = [o.id for o in registry.all_options()]
    assert len(ids) == len(set(ids)) > 100


def test_wheel_options_for_common_files(tmp_path):
    registry.load_all()
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"x")
    labels = [o.label for o in registry.options_for("convert", [jpg])]
    assert "PNG" in labels and "JPG" not in labels and "ZIP" not in labels
    folder = tmp_path / "dir"
    folder.mkdir()
    assert [o.label for o in registry.options_for("convert", [folder])] == ["ZIP", "TAR", "TGZ"]
    assert {"Copy", "Copy Path", "Terminal"} <= {o.label for o in registry.options_for("actions", [folder])}


def test_unique_output_path(tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"x")
    assert paths.output_path(src, "png").name == "photo.png"
    (tmp_path / "photo.png").write_bytes(b"x")
    assert paths.output_path(src, "png").name == "photo (1).png"
    assert paths.output_path(src, suffix="-compressed").name == "photo-compressed.jpg"


def test_wheel_hit_testing():
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    from satsuma.wheel import Wheel

    QApplication.instance() or QApplication([])
    w = Wheel()
    w.options = [registry.Option(id=f"o{i}", label=str(i), mode="convert") for i in range(8)]
    w._resize_for(1)
    c = w._center()
    r = 102 * w.scale
    assert w._index_at(QPointF(c.x(), c.y() - r)) == 0      # top
    assert w._index_at(QPointF(c.x() + r, c.y())) == 2      # right
    assert w._index_at(QPointF(c.x(), c.y() + r)) == 4      # bottom
    assert w._index_at(c) == -1                             # centre cancels
