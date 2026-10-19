"""Image "Add BG" tool: place a photo on a colour, gradient or image backdrop."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .. import formats
from ..i18n import N_, tr
from ..registry import Option, UserError, add
from ..ui.common import STYLE, ToolDialog
from .image_edit import (
    FitLabel, file_caption, fit_scroll_panel, load_image, load_preview, output_stem, save_image,
)

# key (stored in params) -> ratio; ratio keys are shown as-is, "Original" is translated
ASPECTS: dict[str, float | None] = {
    N_("Original"): None, "1:1": 1.0, "4:5": 4 / 5, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3,
}

DEFAULTS: dict = {
    "kind": "gradient",          # solid | gradient | image
    "color1": "#ff8a1f",
    "color2": "#7b3fe4",
    "angle": 135,                 # degrees, 0 = left->right, 90 = top->bottom
    "image": "",                  # backdrop image path for kind == "image"
    "blur_image": True,
    "aspect": "Original",
    "padding": 10,                # % of the photo's longest side, per edge
    "radius": 4,                  # % of the photo's shortest side
    "shadow": True,
    "format": "png",              # png | keep (keep the source format, e.g. jpg)
}


def _rgb(c: str | tuple) -> tuple[int, int, int]:
    if isinstance(c, (tuple, list)):
        return tuple(int(v) for v in c[:3])  # type: ignore[return-value]
    q = QColor(c)
    if not q.isValid():
        raise UserError(tr("Invalid colour {value}").format(value=repr(c)))
    return q.red(), q.green(), q.blue()


def canvas_size(w: int, h: int, padding: float, aspect: str | float | None) -> tuple[int, int]:
    """Canvas that fits the photo plus padding, expanded to the requested aspect ratio."""
    ratio = ASPECTS.get(aspect, aspect) if isinstance(aspect, str) else aspect
    pad = max(0.0, padding) / 100.0 * max(w, h)
    cw, ch = w + 2 * pad, h + 2 * pad
    if ratio:
        if cw / ch < ratio:
            cw = ch * ratio
        else:
            ch = cw / ratio
    return max(1, round(cw)), max(1, round(ch))


def gradient(size: tuple[int, int], c1, c2, angle: float) -> Image.Image:
    """Linear gradient from c1 to c2 along `angle` degrees."""
    W, H = size
    a = math.radians(angle)
    dx, dy = math.cos(a), math.sin(a)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    proj = (xx - W / 2) * dx + (yy - H / 2) * dy
    half = (abs(W * dx) + abs(H * dy)) / 2 or 1.0
    t = np.clip((proj / half + 1) / 2, 0, 1)[..., None]
    a1 = np.array(_rgb(c1), np.float32)
    a2 = np.array(_rgb(c2), np.float32)
    arr = a1 * (1 - t) + a2 * t
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(img.convert("RGB"), size, Image.LANCZOS)


def make_backdrop(size: tuple[int, int], s: dict, bg_image: Image.Image | None = None) -> Image.Image:
    kind = s.get("kind", "solid")
    if kind == "gradient":
        return gradient(size, s.get("color1", "#000"), s.get("color2", "#fff"), float(s.get("angle", 0)))
    if kind == "image":
        if bg_image is None:
            if not s.get("image"):
                raise UserError(tr("Pick a background image"))
            bg_image = load_image(s["image"])
        bg = _cover(bg_image, size)
        if s.get("blur_image", True):
            bg = bg.filter(ImageFilter.GaussianBlur(max(size) * 0.025))
        return bg
    return Image.new("RGB", size, _rgb(s.get("color1", "#000000")))


def _rounded_mask(size: tuple[int, int], radius: float) -> Image.Image:
    w, h = size
    if radius <= 0.5:
        return Image.new("L", size, 255)
    ss = max(1, min(4, int(8000 / max(w, h))))  # supersample for smooth corners
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=255)
    return m.resize(size, Image.LANCZOS) if ss > 1 else m


def render_background(img: Image.Image, settings: dict | None = None,
                      bg_image: Image.Image | None = None) -> Image.Image:
    """Compose `img` centred on a backdrop. Returns an RGB image."""
    s = {**DEFAULTS, **(settings or {})}
    photo = img.convert("RGBA")
    w, h = photo.size
    W, H = canvas_size(w, h, float(s["padding"]), s["aspect"])
    canvas = make_backdrop((W, H), s, bg_image).convert("RGBA")
    x, y = (W - w) // 2, (H - h) // 2

    radius = max(0.0, float(s["radius"])) / 100.0 * min(w, h)
    mask = _rounded_mask((w, h), radius)
    if photo.mode == "RGBA":
        mask = Image.fromarray(np.minimum(np.asarray(mask), np.asarray(photo.getchannel("A"))))

    if s.get("shadow"):
        blur = max(2.0, max(w, h) * 0.025)
        margin = int(blur * 3)
        sh = Image.new("L", (w + 2 * margin, h + 2 * margin), 0)
        sh.paste(mask, (margin, margin))
        sh = sh.filter(ImageFilter.GaussianBlur(blur))
        sh = sh.point(lambda v: int(v * 0.55))
        off = int(max(w, h) * 0.015)
        alpha = Image.new("L", (W, H), 0)
        alpha.paste(sh, (x - margin, y - margin + off))  # paste clips at the canvas edges
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        layer.putalpha(alpha)
        canvas.alpha_composite(layer)
    photo.putalpha(mask)
    canvas.paste(photo, (x, y), photo)
    return canvas.convert("RGB")


def run_background(src: Path, ctx) -> Path:
    s = {**DEFAULTS, **(ctx.params or {})}
    ctx.progress(0.1, tr("Loading"))
    img = load_image(src)
    bg = load_image(s["image"]) if s.get("kind") == "image" and s.get("image") else None
    ctx.check_cancel()
    ctx.progress(0.4, tr("Composing"))
    out = render_background(img, s, bg)
    ctx.check_cancel()
    ctx.progress(0.85, tr("Saving"))
    src_ext = formats.ext(src)
    ext = src_ext if s.get("format") == "keep" and src_ext in ("jpg", "jpeg", "webp") else "png"
    path = ctx.output_path(src, ext, name=output_stem(N_("{name}-background"), src))
    return save_image(out, path, ext)


class ColorButton(QPushButton):
    """Button showing a colour swatch; click to pick."""

    def __init__(self, color: str, on_change):
        super().__init__()
        self._color = QColor(color)
        self._cb = on_change
        self.setMinimumWidth(90)
        self.clicked.connect(self._pick)
        self._paint()

    def color(self) -> str:
        return self._color.name()

    def _paint(self) -> None:
        fg = "#111" if self._color.lightness() > 140 else "#fff"
        self.setText(self._color.name())
        self.setStyleSheet(f"background: {self._color.name()}; color: {fg}; border: 1px solid #3a3a43;")

    def _pick(self) -> None:
        c = QColorDialog.getColor(self._color, self, tr("Choose colour"))
        if c.isValid():
            self._color = c
            self._paint()
            self._cb()


class BackgroundDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        n = len(paths)
        super().__init__(tr("Add background"), tr("Same settings are applied to every selected image.")
                         if n > 1 else "", primary=tr("Save"))
        self.setStyleSheet(STYLE)
        self.setSizeGripEnabled(True)
        self.resize(1100, 720)
        self.paths = paths
        self.index = 0
        self._previews: dict[int, Image.Image] = {}
        self._bg_cache: tuple[str, Image.Image] | None = None

        split = QHBoxLayout()
        self.body.addLayout(split, 1)
        left = QVBoxLayout()
        self.preview = FitLabel()
        left.addWidget(self.preview, 1)
        nav = QHBoxLayout()
        self.prev_btn, self.next_btn = QPushButton(tr("‹ Prev")), QPushButton(tr("Next ›"))
        self.file_lbl = QLabel("", objectName="subtitle")
        self.prev_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn.clicked.connect(lambda: self._go(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.file_lbl, 1)
        nav.addWidget(self.next_btn)
        left.addLayout(nav)
        split.addLayout(left, 1)

        panel = QWidget()
        self.form = QFormLayout(panel)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        split.addWidget(scroll)

        d = DEFAULTS
        self.kind = self.add_combo(tr("Background"), {tr("Solid colour"): "solid", tr("Gradient"): "gradient",
                                                      tr("Image"): "image"}, d["kind"])
        self.color1 = self.add_row(tr("Colour"), ColorButton(d["color1"], self._schedule))
        self.color2 = self.add_row(tr("Colour 2"), ColorButton(d["color2"], self._schedule))
        self.angle = self.add_slider(tr("Angle"), 0, 360, d["angle"], suffix="°")
        img_row = QWidget()
        hl = QHBoxLayout(img_row)
        hl.setContentsMargins(0, 0, 0, 0)
        self.img_lbl = QLabel(tr("No image"), objectName="subtitle")
        pick = QPushButton(tr("Choose…"))
        pick.clicked.connect(self._pick_image)
        hl.addWidget(self.img_lbl, 1)
        hl.addWidget(pick)
        self.img_row = self.add_row(tr("Image"), img_row)
        self.blur_image = self.add_row("", QCheckBox(tr("Blur background image")))
        self.blur_image.setChecked(d["blur_image"])
        self._image_path = ""
        self.aspect = self.add_combo(tr("Aspect ratio"), {tr(k): k for k in ASPECTS}, d["aspect"])
        self.padding = self.add_slider(tr("Padding"), 0, 50, d["padding"], suffix="%")
        self.radius = self.add_slider(tr("Corner radius"), 0, 50, d["radius"], suffix="%")
        self.shadow = self.add_row("", QCheckBox(tr("Drop shadow")))
        self.shadow.setChecked(d["shadow"])
        self.fmt = self.add_combo(tr("Save as"), {"PNG": "png", tr("Keep JPG/WebP"): "keep"}, d["format"])
        fit_scroll_panel(scroll, 400)  # wider when the labels are longer (e.g. Hindi)

        for sig in (self.kind.currentIndexChanged, self.angle.valueChanged, self.aspect.currentIndexChanged,
                    self.padding.valueChanged, self.radius.valueChanged, self.shadow.toggled,
                    self.blur_image.toggled):
            sig.connect(self._schedule)
        self.kind.currentIndexChanged.connect(self._update_visibility)
        self._timer = QTimer(self, singleShot=True, interval=120)
        self._timer.timeout.connect(self._render)
        self._update_visibility()
        self._show_file()

    def values(self) -> dict:
        return {
            "kind": self.kind.currentData(), "color1": self.color1.color(), "color2": self.color2.color(),
            "angle": self.angle.value(), "image": self._image_path,
            "blur_image": self.blur_image.isChecked(), "aspect": self.aspect.currentData(),
            "padding": self.padding.value(), "radius": self.radius.value(),
            "shadow": self.shadow.isChecked(), "format": self.fmt.currentData(),
        }

    def accept(self) -> None:
        if self.kind.currentData() == "image" and not self._image_path:
            self.img_lbl.setText(tr("Choose an image first"))
            return
        super().accept()

    def _update_visibility(self) -> None:
        kind = self.kind.currentData()
        for w, show in ((self.color1, kind in ("solid", "gradient")), (self.color2, kind == "gradient"),
                        (self.angle.parentWidget(), kind == "gradient"), (self.img_row, kind == "image"),
                        (self.blur_image, kind == "image")):
            w.setVisible(show)
            lbl = self.form.labelForField(w)
            if lbl is not None:
                lbl.setVisible(show)

    def _pick_image(self) -> None:
        exts = " ".join(f"*.{e}" for e in sorted(formats.IMAGE_IN))
        path, _ = QFileDialog.getOpenFileName(self, tr("Background image"), "",
                                              tr("Images ({patterns})").format(patterns=exts))
        if path:
            self._image_path = path
            self.img_lbl.setText(Path(path).name)
            self._schedule()

    def _go(self, step: int) -> None:
        self.index = max(0, min(len(self.paths) - 1, self.index + step))
        self._show_file()

    def _show_file(self) -> None:
        n = len(self.paths)
        for b in (self.prev_btn, self.next_btn):
            b.setVisible(n > 1)
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < n - 1)
        self.file_lbl.setText(file_caption(self.index, n, self.paths[self.index].name))
        self._render()

    def _schedule(self, *_):
        self._timer.start()

    def _base(self) -> Image.Image:
        if self.index not in self._previews:
            try:
                self._previews[self.index] = load_preview(self.paths[self.index], 700)
            except Exception:
                self._previews[self.index] = Image.new("RGB", (400, 300), "#555")
        return self._previews[self.index]

    def _bg(self) -> Image.Image | None:
        if not self._image_path:
            return None
        if self._bg_cache is None or self._bg_cache[0] != self._image_path:
            try:
                self._bg_cache = (self._image_path, load_preview(self._image_path, 1000))
            except Exception:
                return None
        return self._bg_cache[1]

    def _render(self) -> None:
        v = self.values()
        bg = self._bg()
        if v["kind"] == "image" and bg is None:
            v["kind"] = "solid"
            v["color1"] = "#2c2c33"
        try:
            self.preview.set_image(render_background(self._base(), v, bg))
        except UserError as e:
            self.file_lbl.setText(str(e))


def background_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = BackgroundDialog([Path(p) for p in paths])
    return dlg.values() if dlg.exec() else None


add(Option(
    id="image.add_background", label="Add BG", mode="tools", run=run_background,
    dialog=background_dialog, exts=frozenset(formats.IMAGE_IN), family="image", order=40,
    description="Place the image on a colour, gradient or picture backdrop",
))
