r"""Refresh the committed website images from the app's own renders (run locally, not in CI).

    $env:QT_QPA_PLATFORM='windows'; $env:QT_SCALE_FACTOR='2'
    .venv\Scripts\python.exe scripts\render_screens.py SHOTS
    .venv\Scripts\python.exe site\make_images.py SHOTS

Writes WebP (1x + 2x) and PNG (1x) screenshots, Open Graph cards and icons into
site/assets/img, which build.py copies into public/ as-is. Needs Pillow.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from build import HERE, LANGS


def make_images(shots: Path) -> None:
    """Optimise app renders into site/assets/img (WebP 1x/2x + PNG 1x) and make OG cards."""
    from PIL import Image, ImageDraw, ImageFont

    img = HERE / "assets" / "img"
    img.mkdir(parents=True, exist_ok=True)

    def save(im: Image.Image, base: str, w1: int) -> None:
        h1 = round(im.height * w1 / im.width)
        im2 = im.resize((w1 * 2, h1 * 2), Image.LANCZOS) if im.width != w1 * 2 else im
        im1 = im.resize((w1, h1), Image.LANCZOS)
        im2.save(img / f"{base}@2x.webp", "WEBP", quality=86, method=6)
        im1.save(img / f"{base}.webp", "WEBP", quality=88, method=6)
        im1.save(img / f"{base}.png", "PNG", optimize=True)

    for code, *_ in LANGS:
        for mode in ("convert", "tools", "actions"):
            save(Image.open(shots / f"{code}-wheel-{mode}.png").convert("RGBA"), f"{code}-wheel-{mode}", 388)
        save(Image.open(shots / f"{code}-welcome.png").convert("RGB"), f"{code}-welcome", 480)

    # Open Graph cards: 1200x630, the translated wheel on the app's dark background.
    icon = Image.open(HERE.parent / "satsuma" / "resources" / "satsuma.png").convert("RGBA")
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 92)
    except OSError:
        font = ImageFont.load_default()
    for code, *_ in LANGS:
        card = Image.new("RGB", (1200, 630), "#141417")
        d = ImageDraw.Draw(card)
        d.ellipse((530, -30, 1190, 630), fill="#2a1d12")
        wheel = Image.open(shots / f"{code}-wheel-convert.png").convert("RGBA").resize((600, 600), Image.LANCZOS)
        card.paste(wheel, (560, 15), wheel)
        card.paste(icon.resize((120, 120), Image.LANCZOS), (80, 200), icon.resize((120, 120), Image.LANCZOS))
        d.text((80, 340), "Satsuma", font=font, fill="#ececf1")
        d.rectangle((84, 470, 244, 478), fill="#FF8A1F")
        card.save(img / f"og-{code}.png", "PNG", optimize=True)

    for size, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"), (32, "favicon-32.png")):
        icon.resize((size, size), Image.LANCZOS).save(img / name, "PNG", optimize=True)
    shutil.copy2(HERE.parent / "satsuma" / "resources" / "satsuma.ico", img / "favicon.ico")


if __name__ == "__main__":
    make_images(Path(sys.argv[1]))
    print("images written to", HERE / "assets" / "img")
