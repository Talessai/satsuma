"""Render key screens in every language to PNGs for a visual check.

    python scripts/render_screens.py OUTDIR
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from satsuma import i18n, registry  # noqa: E402
from satsuma.ui.common import apply_style  # noqa: E402


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    apply_style(app)
    registry.load_all()
    jpg = Path(tempfile.mkdtemp()) / "photo.jpg"
    from PIL import Image

    Image.new("RGB", (64, 48), "orange").save(jpg)
    for lang in ("en", "zh", "hi", "es", "ar"):
        i18n.set_language(lang)
        app.setLayoutDirection(Qt.RightToLeft if i18n.is_rtl() else Qt.LeftToRight)
        from satsuma.wheel import Wheel

        w = Wheel()
        w.paths = [jpg]
        for mode in ("convert", "tools", "actions"):
            w.mode = mode
            w._refresh_options()
            w.hover = 1
            w.grab().save(str(out / f"{lang}-wheel-{mode}.png"))

        from satsuma.ui import welcome

        welcome._dialog = None
        d = welcome._build()
        d.adjustSize()
        d.grab().save(str(out / f"{lang}-welcome.png"))

        from satsuma.ui import settings_dialog

        s = settings_dialog.SettingsDialog()
        s.adjustSize()
        s.grab().save(str(out / f"{lang}-settings.png"))

        from satsuma.tools.image_basic import CompressDialog

        c = CompressDialog([jpg])
        c.adjustSize()
        c.grab().save(str(out / f"{lang}-compress.png"))
    print("saved to", out)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
