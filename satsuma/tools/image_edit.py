"""Image "Edit" tool: photo adjustments + filters with a live preview.

Also hosts small helpers shared by the other image tools in this package
(loading/saving via converters.images, PIL <-> QImage, a fit-to-size preview).
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

from .. import formats
from ..i18n import N_, tr
from ..registry import Option, UserError, add
from ..ui.common import STYLE, ToolDialog

# ----------------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------------

PREVIEW_MAX = 1000


def load_image(path: Path | str, size_hint: int | None = None) -> Image.Image:
    """Open any supported image (HEIC/AVIF/SVG/EXIF orientation handled).

    `size_hint` (long edge) allows a faster reduced-size decode for previews.
    """
    path = Path(path)
    try:
        from ..converters.images import open_image
    except Exception:  # converters.images not available: minimal fallback
        open_image = None
    if open_image is not None:
        try:
            img = open_image(path, size_hint=size_hint)
        except TypeError:
            img = open_image(path)
    else:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            pass
        if formats.ext(path) == "svg":
            img = _render_svg_fallback(path)
        else:
            try:
                img = Image.open(path)
                img.load()
            except Exception as e:
                raise UserError(tr("Can't open {name}: {error}").format(name=path.name, error=e)) from e
            img = ImageOps.exif_transpose(img)
    info = dict(img.info)
    img = normalize_mode(img)
    img.info.update({k: v for k, v in info.items() if k in ("exif", "icc_profile")})
    return img


def load_preview(path: Path | str, max_side: int = PREVIEW_MAX) -> Image.Image:
    """Downscaled copy for interactive previews."""
    return downscale(load_image(path, size_hint=max_side), max_side)


def _render_svg_fallback(path: Path) -> Image.Image:
    from PySide6.QtSvg import QSvgRenderer

    r = QSvgRenderer(str(path))
    if not r.isValid():
        raise UserError(tr("Can't read {name}").format(name=path.name))
    size = r.defaultSize()
    w, h = max(1, size.width()), max(1, size.height())
    scale = max(1.0, 1600 / max(w, h))
    qi = QImage(int(w * scale), int(h * scale), QImage.Format_RGBA8888)
    qi.fill(Qt.transparent)
    p = QPainter(qi)
    r.render(p)
    p.end()
    return qimage_to_pil(qi)


def normalize_mode(img: Image.Image) -> Image.Image:
    """RGB, or RGBA when the image has transparency."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        return img if img.mode == "RGBA" else img.convert("RGBA")
    if img.mode == "RGB":
        return img
    if img.mode.startswith("I;16") or img.mode == "I":
        arr = np.asarray(img, dtype=np.float32)
        arr = np.clip(arr / (256.0 if arr.max() > 255 else 1.0), 0, 255).astype(np.uint8)
        return Image.fromarray(arr, "L").convert("RGB")
    return img.convert("RGB")


def out_ext(src: Path) -> str:
    """Extension to save an edited copy of `src` with (svg can't be written back)."""
    e = formats.ext(src)
    return "png" if e in ("svg", "") else e


def save_image(img: Image.Image, out: Path, fmt: str | None = None, *,
               meta_from: Image.Image | None = None, strip_exif: bool = False) -> Path:
    """Save through converters.images.save_image, with a plain-PIL fallback.

    `meta_from` copies EXIF/ICC from the source image; `strip_exif` drops EXIF (e.g. GPS).
    """
    out = Path(out)
    fmt = (fmt or formats.ext(out) or "png").lower()
    try:
        from ..converters.images import save_image as _save
    except Exception:
        _save = None
    if meta_from is not None:
        for k in ("exif", "icc_profile"):
            if k in meta_from.info:
                img.info[k] = meta_from.info[k]
    if strip_exif:
        img.info.pop("exif", None)
    if _save is not None:
        res = _save(img, out, fmt, exif=None) if strip_exif else _save(img, out, fmt)
        return Path(res) if isinstance(res, (str, Path)) else out
    return _save_fallback(img, out, fmt)


def _save_fallback(img: Image.Image, out: Path, fmt: str) -> Path:
    from ..config import settings

    pil_fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "tif": "TIFF",
               "tiff": "TIFF", "bmp": "BMP", "gif": "GIF", "ico": "ICO", "heic": "HEIF",
               "heif": "HEIF", "avif": "AVIF"}.get(fmt, fmt.upper())
    kw: dict = {}
    if pil_fmt in ("JPEG", "BMP") and img.mode != "RGB":
        bg = Image.new("RGB", img.size, "white")
        if img.mode == "RGBA":
            bg.paste(img, mask=img.getchannel("A"))
        else:
            bg.paste(img.convert("RGB"))
        img = bg
    if pil_fmt in ("JPEG", "WEBP", "HEIF", "AVIF"):
        kw["quality"] = int(settings.get("jpeg_quality", 90))
    if pil_fmt in ("HEIF", "AVIF"):
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            pass
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, pil_fmt, **kw)
    return out


_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def output_stem(template: str, src: Path) -> str:
    """Output file stem from an N_()-marked template such as N_("{name}-edited"),
    translated here. Characters Windows forbids are stripped from the translated text
    only (the source stem is already a valid name), so English output stays exactly
    `stem + "-edited"`."""
    stem = src.stem
    safe = _BAD_NAME_CHARS.sub("", tr(template))
    try:
        if "{name}" in safe:
            return safe.format(name=stem)
    except (KeyError, IndexError, ValueError):
        pass
    return template.format(name=stem)  # broken translation: English


def downscale(img: Image.Image, max_side: int = PREVIEW_MAX) -> Image.Image:
    """A copy no larger than `max_side` on its longest edge."""
    w, h = img.size
    s = max(w, h)
    if s <= max_side:
        return img.copy()
    k = max_side / s
    return img.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)


def pil_to_qimage(img: Image.Image) -> QImage:
    rgba = img if img.mode == "RGBA" else img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    w, h = rgba.size
    return QImage(data, w, h, 4 * w, QImage.Format_RGBA8888).copy()


def qimage_to_pil(qimg: QImage) -> Image.Image:
    q = qimg.convertToFormat(QImage.Format_RGBA8888)
    w, h, bpl = q.width(), q.height(), q.bytesPerLine()
    data = bytes(q.constBits())[: bpl * h]
    return Image.frombuffer("RGBA", (w, h), data, "raw", "RGBA", bpl, 1).copy()


class FitLabel(QLabel):
    """Shows a PIL image scaled to fit the label, keeping its aspect ratio."""

    def __init__(self):
        super().__init__()
        self._pm: QPixmap | None = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background: #121215; border-radius: 8px;")

    def set_image(self, img: Image.Image) -> None:
        self._pm = QPixmap.fromImage(pil_to_qimage(img))
        self._refit()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._refit()

    def _refit(self) -> None:
        if self._pm is None or self._pm.isNull():
            return
        self.setPixmap(self._pm.scaled(self.size() * 0.98, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation))


def fit_width(widget: QWidget, minimum: int, extra: int = 0) -> int:
    """Fix `widget`'s width to its content (translated labels can be much longer than
    English), but never narrower than `minimum`. Call once the widget is in its dialog."""
    widget.window().ensurePolished()
    widget.ensurePolished()
    w = max(minimum, widget.sizeHint().width() + extra)
    widget.setFixedWidth(w)
    return w


def fit_scroll_panel(scroll: QScrollArea, minimum: int) -> int:
    """Width a vertical-only scroll area needs to show its panel without clipping."""
    panel = scroll.widget()
    panel.ensurePolished()
    extra = scroll.verticalScrollBar().sizeHint().width() + 2 * scroll.frameWidth() + 4
    w = max(minimum, panel.sizeHint().width() + extra)
    scroll.setFixedWidth(w)
    return w


def file_caption(index: int, count: int, name: str) -> str:
    """'2/5 · photo.jpg' (just the name for a single file)."""
    if count <= 1:
        return name
    return tr("{index}/{count} · {name}").format(index=index + 1, count=count, name=name)


# ----------------------------------------------------------------------------
# adjustments (pure functions)
# ----------------------------------------------------------------------------

# key, label (translated when shown), min, max
SLIDERS: list[tuple[str, str, int, int]] = [
    ("exposure", N_("Exposure"), -100, 100),
    ("brightness", N_("Brightness"), -100, 100),
    ("contrast", N_("Contrast"), -100, 100),
    ("highlights", N_("Highlights"), -100, 100),
    ("shadows", N_("Shadows"), -100, 100),
    ("saturation", N_("Saturation"), -100, 100),
    ("vibrance", N_("Vibrance"), -100, 100),
    ("temperature", N_("Temperature"), -100, 100),
    ("tint", N_("Tint"), -100, 100),
    ("sharpness", N_("Sharpness"), 0, 100),
    ("clarity", N_("Clarity"), -100, 100),
    ("blur", N_("Blur"), 0, 100),
    ("vignette", N_("Vignette"), -100, 100),
    ("grain", N_("Grain"), 0, 100),
]
# internal filter names (stored in params); shown translated via tr()
FILTERS = [N_("None"), N_("Mono"), N_("Sepia"), N_("Vivid"), N_("Fade"), N_("Noir")]
DEFAULTS = {k: 0 for k, *_ in SLIDERS}

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _gauss(a: np.ndarray, sigma: float) -> np.ndarray:
    import cv2

    return cv2.GaussianBlur(a, (0, 0), sigmaX=max(0.1, float(sigma)), borderType=cv2.BORDER_REFLECT)


def _luma(x: np.ndarray) -> np.ndarray:
    return x @ _LUMA


def _saturate(x: np.ndarray, factor: float) -> np.ndarray:
    g = _luma(x)[..., None]
    return g + (x - g) * factor


def _apply_filter(x: np.ndarray, name: str) -> np.ndarray:
    name = (name or "None").lower()
    if name == "mono":
        g = _luma(x)[..., None]
        return np.repeat(g, 3, axis=2)
    if name == "sepia":
        m = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]], np.float32)
        return x @ m.T
    if name == "vivid":
        x = _saturate(x, 1.4)
        return (x - 0.5) * 1.12 + 0.5
    if name == "fade":
        x = _saturate(x, 0.75)
        return x * 0.82 + 0.1
    if name == "noir":
        g = _luma(x)[..., None]
        g = (g - 0.5) * 1.55 + 0.47
        return np.repeat(g, 3, axis=2)
    return x


def apply_adjustments(img: Image.Image, adj: dict | None = None, filter_name: str = "None") -> Image.Image:
    """Return a new image with the adjustments (slider values) and filter applied.

    All spatial parameters are relative to the image size, so a downscaled preview
    looks like the full-resolution result.
    """
    a = {**DEFAULTS, **(adj or {})}
    img = normalize_mode(img)
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    x = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    h, w = x.shape[:2]
    size = max(w, h)

    v = a["exposure"] / 100.0
    if v:
        x = x * (2.0 ** (v * 2.0))
    t, tint = a["temperature"] / 100.0, a["tint"] / 100.0
    if t or tint:
        x = x * np.array([1 + 0.18 * t, 1 - 0.12 * tint, 1 - 0.18 * t], np.float32)
    v = a["brightness"] / 100.0
    if v:
        x = np.clip(x, 0, None)
        # gamma-style brightness keeps blacks/whites anchored
        x = np.power(x, 1.0 / (1.0 + v * 0.8) if v > 0 else 1.0 - v * 0.8)
    v = a["contrast"] / 100.0
    if v:
        x = (x - 0.5) * (1.0 + v) + 0.5
    sh, hl = a["shadows"] / 100.0, a["highlights"] / 100.0
    if sh or hl:
        lum = np.clip(_luma(x), 0, 1)
        lum = _gauss(lum.astype(np.float32), size * 0.01)[..., None]
        if sh:
            x = x + sh * 0.45 * (1 - lum) ** 2
        if hl:
            x = x + hl * 0.45 * lum ** 2
    v = a["saturation"] / 100.0
    if v:
        x = _saturate(x, 1.0 + v)
    v = a["vibrance"] / 100.0
    if v:
        g = _luma(x)[..., None]
        sat = np.clip(x.max(axis=2, keepdims=True) - x.min(axis=2, keepdims=True), 0, 1)
        x = g + (x - g) * (1.0 + v * (1.0 - sat) * 1.2)
    x = _apply_filter(x, filter_name)
    v = a["clarity"] / 100.0
    if v:
        base = _gauss(x, size * 0.02)
        x = x + (x - base) * v * 0.9
    v = a["sharpness"] / 100.0
    if v:
        base = _gauss(x, max(0.8, size / 1500.0))
        x = x + (x - base) * v * 2.0
    v = a["blur"] / 100.0
    if v:
        x = _gauss(np.clip(x, 0, 1), v * size * 0.015)
    v = a["vignette"] / 100.0
    if v:
        yy, xx = np.ogrid[:h, :w]
        r2 = ((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2
        mask = np.clip(r2 / 2.0, 0, 1).astype(np.float32) ** 1.5
        if v > 0:
            x = x * (1 - v * 0.85 * mask)[..., None]
        else:
            x = x + (1 - x) * (-v * 0.85 * mask)[..., None]
    v = a["grain"] / 100.0
    if v:
        k = max(1.0, size / 1200.0)
        nh, nw = max(1, int(h / k)), max(1, int(w / k))
        rng = np.random.default_rng(1234)
        noise = rng.normal(0, 1, (nh, nw)).astype(np.float32)
        if (nh, nw) != (h, w):
            import cv2

            noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_LINEAR)
        x = x + (noise * v * 0.12)[..., None]
    out = Image.fromarray((np.clip(x, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGB")
    if alpha is not None:
        out.putalpha(alpha)
    return out


def is_identity(adj: dict, filter_name: str) -> bool:
    return all(not adj.get(k) for k in DEFAULTS) and (filter_name or "None") == "None"


# ----------------------------------------------------------------------------
# option
# ----------------------------------------------------------------------------

def run_edit(src: Path, ctx) -> Path:
    p = ctx.params or {}
    adj = p.get("adjustments", {})
    flt = p.get("filter", "None")
    ctx.progress(0.1, tr("Loading"))
    img = load_image(src)
    ctx.check_cancel()
    ctx.progress(0.3, tr("Applying adjustments"))
    out_img = apply_adjustments(img, adj, flt)
    ctx.check_cancel()
    ctx.progress(0.85, tr("Saving"))
    ext = out_ext(src)
    out = ctx.output_path(src, ext, name=output_stem(N_("{name}-edited"), src))
    return save_image(out_img, out, ext, meta_from=img)


class EditDialog(ToolDialog):
    """Sliders on the right, preview (with file navigation) on the left."""

    def __init__(self, paths: list[Path]):
        n = len(paths)
        super().__init__(tr("Edit image") if n == 1 else tr("Edit {n} images").format(n=n),
                         tr("Adjustments apply to every selected image.") if n > 1 else "",
                         primary=tr("Save"))
        self.setStyleSheet(STYLE)
        self.setSizeGripEnabled(True)
        self.resize(1180, 760)
        self.paths = paths
        self.index = 0
        self._previews: dict[int, Image.Image] = {}
        self._showing_original = False

        split = QHBoxLayout()
        split.setSpacing(14)
        self.body.addLayout(split, 1)

        left = QVBoxLayout()
        self.preview = FitLabel()
        left.addWidget(self.preview, 1)
        nav = QHBoxLayout()
        self.prev_btn = QPushButton(tr("‹ Prev"))
        self.next_btn = QPushButton(tr("Next ›"))
        self.file_lbl = QLabel("", objectName="subtitle")
        self.compare_btn = QPushButton(tr("Hold to see original"))
        self.compare_btn.pressed.connect(lambda: self._set_original(True))
        self.compare_btn.released.connect(lambda: self._set_original(False))
        self.prev_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn.clicked.connect(lambda: self._go(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.file_lbl, 1, Qt.AlignCenter)
        nav.addWidget(self.next_btn)
        left.addLayout(nav)
        left.addWidget(self.compare_btn)
        split.addLayout(left, 1)

        panel = QWidget()
        self.form = QFormLayout(panel)  # ToolDialog.add_* helpers now fill the side panel
        self.form.setVerticalSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right = QVBoxLayout()
        right.addWidget(scroll, 1)
        split.addLayout(right)

        # display text -> internal filter name (params keep the English key)
        self.filter_combo: QComboBox = self.add_combo(tr("Filter"),{tr(f): f for f in FILTERS}, "None")
        self.filter_combo.currentIndexChanged.connect(self._schedule)
        self.sliders: dict[str, QSlider] = {}
        for key, label, lo, hi in SLIDERS:
            s = self.add_slider(tr(label), lo, hi, 0)
            s.valueChanged.connect(self._schedule)
            self.sliders[key] = s
        fit_scroll_panel(scroll, 380)  # wider when the labels are longer (e.g. Hindi)
        reset = QPushButton(tr("Reset"))
        reset.clicked.connect(self.reset)
        self.footer.insertWidget(0, reset)

        self._timer = QTimer(self, singleShot=True, interval=120)
        self._timer.timeout.connect(self._render)
        self._show_file()

    # --- state ---
    def adjustments(self) -> dict:
        return {k: s.value() for k, s in self.sliders.items()}

    def values(self) -> dict:
        return {"adjustments": self.adjustments(), "filter": self.filter_combo.currentData() or "None"}

    def reset(self) -> None:
        for s in self.sliders.values():
            s.setValue(0)  # debounced re-render via _schedule
        self.filter_combo.setCurrentIndex(0)

    # --- preview ---
    def _base(self) -> Image.Image:
        if self.index not in self._previews:
            try:
                self._previews[self.index] = load_preview(self.paths[self.index])
            except Exception:
                self._previews[self.index] = Image.new("RGB", (400, 300), "#333")
        return self._previews[self.index]

    def _show_file(self) -> None:
        n = len(self.paths)
        self.prev_btn.setVisible(n > 1)
        self.next_btn.setVisible(n > 1)
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < n - 1)
        self.file_lbl.setText(file_caption(self.index, n, self.paths[self.index].name))
        self._render()

    def _go(self, step: int) -> None:
        self.index = max(0, min(len(self.paths) - 1, self.index + step))
        self._show_file()

    def _schedule(self, *_):
        self._timer.start()

    def _set_original(self, on: bool) -> None:
        self._showing_original = on
        self._render()

    def _render(self) -> None:
        base = self._base()
        if self._showing_original:
            self.preview.set_image(base)
            return
        v = self.values()
        img = base if is_identity(v["adjustments"], v["filter"]) else \
            apply_adjustments(base, v["adjustments"], v["filter"])
        self.preview.set_image(img)


def edit_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = EditDialog([Path(p) for p in paths])
    return dlg.values() if dlg.exec() else None


add(Option(
    id="image.edit", label="Edit", mode="tools", run=run_edit, dialog=edit_dialog,
    exts=frozenset(formats.IMAGE_IN), family="image", order=20,
    description="Adjust exposure, colour, sharpness and apply filters",
))
