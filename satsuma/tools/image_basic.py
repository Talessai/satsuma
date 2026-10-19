"""Basic image tools (Ctrl wheel): Compress, Metadata, Crop, Create PDF, Collage."""
from __future__ import annotations

import io
import math
import os
import statistics
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from .. import formats
from ..converters.images import (
    canonical_ext, encode_image, flatten, has_alpha, open_image, pdf_resolution, pil_to_qimage,
    save_image,
)
from ..paths import localized_stem
from ..i18n import N_, tr
from ..registry import Option, UserError, add
from ..ui.common import ToolDialog, fmt_size

COMPRESS_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "heic", "heif", "avif", "tif", "tiff"})
META_EXTS = COMPRESS_EXTS
LOSSY = {"jpg", "webp", "heic", "avif"}
PRESET_QUALITY = {"high": 82, "medium": 65, "small": 45}
PRESET_PNG_COLORS = {"high": None, "medium": 256, "small": 96}


def _dims(w, h) -> str:
    """'640×480' kept left-to-right inside RTL text (otherwise Arabic shows '480×640')."""
    return f"⁦{w}×{h}⁩"


def _combo_items(table: dict) -> dict:
    """{N_("Label"): value} -> {translated label: value} for ToolDialog.add_combo."""
    return {tr(label): value for label, value in table.items()}


def _oriented_size(path: Path) -> tuple[int, int]:
    """Pixel size as displayed (EXIF orientation applied), without decoding pixels."""
    if formats.ext(path) == "svg":
        return open_image(path, size_hint=1024).size
    try:
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
            return w, h
    except Exception as e:
        raise UserError(tr("Can't read {name}: {error}").format(name=path.name, error=e))


def _out_ext(src: Path) -> str:
    """Extension to keep when re-saving `src` (svg can't be written, so PNG)."""
    e = formats.ext(src)
    return "png" if e == "svg" else e


def _resized(img: Image.Image, resize: dict | None) -> Image.Image:
    """Apply {"kind": "edge"|"percent", "value": n}; never upscales."""
    if not resize:
        return img
    w, h = img.size
    kind, value = resize.get("kind"), float(resize.get("value") or 0)
    if value <= 0:
        return img
    if kind == "edge":
        scale = value / max(w, h)
    elif kind == "percent":
        scale = value / 100
    else:
        return img
    if scale >= 1:
        return img
    return _scale(img, scale)


def _scale(img: Image.Image, scale: float) -> Image.Image:
    info = dict(img.info)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    src = img if img.mode not in ("P", "1") else img.convert("RGBA" if has_alpha(img) else "RGB")
    out = src.resize((nw, nh), Image.Resampling.LANCZOS)
    out.info.update(info)
    return out


# =============================================================================
# Compress
# =============================================================================

def _encode_lossy(img: Image.Image, fmt: str, q: int) -> bytes:
    if fmt == "tiff":
        return encode_image(img, "tiff", compression="jpeg", quality=q)
    return encode_image(img, fmt, quality=q, optimize=True)


def _quantized(img: Image.Image, colors: int) -> Image.Image:
    info = dict(img.info)
    src = img.convert("RGBA" if has_alpha(img) else "RGB")
    method = Image.Quantize.FASTOCTREE if src.mode == "RGBA" else Image.Quantize.MEDIANCUT
    out = src.quantize(colors=colors, method=method, dither=Image.Dither.FLOYDSTEINBERG)
    for k in ("exif", "icc_profile", "dpi"):
        if k in info:
            out.info[k] = info[k]
    return out


def _encode_png(img: Image.Image, colors: int | None) -> bytes:
    work = _quantized(img, colors) if colors else img
    return encode_image(work, "png", optimize=True)


def _search_quality(img: Image.Image, fmt: str, target: int, ctx) -> tuple[bytes, int]:
    """Highest quality (10..95) whose encoding fits `target` bytes (or q=10 if none)."""
    lo, hi = 10, 95
    best: tuple[bytes, int] | None = None
    smallest: tuple[bytes, int] | None = None
    while lo <= hi:
        ctx.check_cancel()
        q = (lo + hi) // 2
        data = _encode_lossy(img, fmt, q)
        if smallest is None or len(data) < len(smallest[0]):
            smallest = (data, q)
        if len(data) <= target:
            best = (data, q)
            lo = q + 3
        else:
            hi = q - 3
    if best is None:
        data = _encode_lossy(img, fmt, 10)
        best = min([(data, 10)] + ([smallest] if smallest else []), key=lambda t: len(t[0]))
    return best


def compress_image(src: Path, params: dict, ctx) -> tuple[bytes, Image.Image]:
    """Return encoded bytes for `src` per Compress params (format is kept)."""
    fmt = canonical_ext(formats.ext(src))
    if fmt not in LOSSY and fmt not in ("png", "tiff"):
        raise UserError(tr("Can't compress {name}").format(name=src.name))
    size = src.stat().st_size
    img = _resized(open_image(src), params.get("resize"))
    mode = params.get("mode", "preset")
    preset = params.get("preset", "medium")
    lossy = fmt in LOSSY or (fmt == "tiff" and not has_alpha(img) and img.mode in ("RGB", "L"))

    if mode == "preset":
        if lossy:
            return _encode_lossy(img, fmt, PRESET_QUALITY.get(preset, 65)), img
        if fmt == "tiff":
            return encode_image(img, "tiff", compression="tiff_adobe_deflate"), img
        return _encode_png(img, PRESET_PNG_COLORS.get(preset)), img

    if "ratio" in params:
        target = int(size * float(params["ratio"]))
    elif mode == "percent":
        target = int(size * (1 - float(params.get("percent", 50)) / 100))
    else:
        target = int(params.get("target_bytes") or size // 2)
    target = max(1024, target)

    data = b""
    for attempt in range(6):
        ctx.check_cancel()
        ctx.progress(0.1 + attempt * 0.15, None)
        if lossy:
            data, _q = _search_quality(img, fmt, target, ctx)
        elif fmt == "tiff":
            data = encode_image(img, "tiff", compression="tiff_adobe_deflate")
        else:
            for colors in (None, 256, 128, 64, 32):
                ctx.check_cancel()
                data = _encode_png(img, colors)
                if len(data) <= target:
                    break
        if len(data) <= target or min(img.size) <= 16:
            break
        # Still too big even at the lowest setting: shrink the pixels and retry.
        img = _scale(img, max(0.3, min(0.95, math.sqrt(target / len(data)) * 0.95)))
    return data, img


def compress_run(src: Path, ctx) -> Path:
    """Compress one image, keeping its format: '<name>-compressed.<ext>'."""
    src = Path(src)
    params = ctx.params or {}
    data, _ = compress_image(src, params, ctx)
    out = ctx.output_path(src, _out_ext(src), name=localized_stem(tr("{name}-compressed"), src))
    if len(data) >= src.stat().st_size and not params.get("resize"):
        data = src.read_bytes()  # already as small as we can get it
        ctx.progress(None, tr("{name} is already well compressed").format(name=src.name))
    out.write_bytes(data)
    return out


def compress_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = CompressDialog(paths)
    return dlg.values() if dlg.exec() else None


def _hrow(*widgets) -> QWidget:
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        if w is None:
            h.addStretch(1)
        else:
            h.addWidget(w)
    return row


COMPRESS_METHODS = {N_("Preset"): "preset", N_("Target size"): "size",
                    N_("Reduce by percentage"): "percent"}
COMPRESS_PRESETS = {N_("High quality"): "high", N_("Medium"): "medium", N_("Small file"): "small"}
RESIZE_KINDS = {N_("Don't resize"): None, N_("Max long edge"): "edge", N_("Scale"): "percent"}
KB, MB = 1024, 1024 * 1024


class CompressDialog(ToolDialog):
    """Preset / target size / percentage, plus optional resize."""

    def __init__(self, paths: list[Path]):
        n = len(paths)
        self.total = sum(p.stat().st_size for p in paths if p.exists())
        if n == 1:
            sub = tr("{name} · {size} total").format(name=paths[0].name, size=fmt_size(self.total))
        else:
            sub = tr("{n} images · {size} total").format(n=n, size=fmt_size(self.total))
        super().__init__(tr("Compress images"), sub, primary=tr("Compress"))
        self.mode = self.add_combo(tr("Method"), _combo_items(COMPRESS_METHODS))
        self.preset = self.add_combo(tr("Preset"), _combo_items(COMPRESS_PRESETS), current="medium")
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(1, 1_000_000)
        self.size_spin.setDecimals(1)
        self.unit = QComboBox()
        self.unit.addItem("KB", KB)  # unit symbols, same in every language (see fmt_size)
        self.unit.addItem("MB", MB)
        half = self.total / 2
        if half >= MB:
            self.unit.setCurrentIndex(self.unit.findData(MB))
            self.size_spin.setValue(round(half / MB, 1))
        else:
            self.size_spin.setValue(max(1.0, round(half / KB, 1)))
        self.size_row = _hrow(self.size_spin, self.unit)
        self.add_row(tr("Target total"), self.size_row)
        self.percent = self.add_slider(tr("Reduce by"), 5, 95, 50, suffix="%")
        self.resize_kind = self.add_combo(tr("Resize"), _combo_items(RESIZE_KINDS))
        self.resize_value = QSpinBox()
        self.resize_value.setRange(1, 20000)
        self.resize_value.setValue(1920)
        self.add_row(tr("Resize to"), self.resize_value)
        self.estimate = QLabel(objectName="subtitle", wordWrap=True)
        self.body.addWidget(self.estimate)
        for sig in (self.mode.currentIndexChanged, self.resize_kind.currentIndexChanged,
                    self.size_spin.valueChanged, self.unit.currentIndexChanged, self.percent.valueChanged):
            sig.connect(self._sync)
        self._sync()

    def _target_bytes(self) -> int:
        return int(self.size_spin.value() * (self.unit.currentData() or KB))

    def _sync(self, *_):
        mode = self.mode.currentData()
        self.form.setRowVisible(self.preset, mode == "preset")
        self.form.setRowVisible(self.size_row, mode == "size")
        self.form.setRowVisible(self.percent.parentWidget(), mode == "percent")
        kind = self.resize_kind.currentData()
        self.form.setRowVisible(self.resize_value, kind is not None)
        self.resize_value.setSuffix(" px" if kind == "edge" else " %")
        if kind == "percent" and self.resize_value.value() > 100:
            self.resize_value.setValue(50)
        elif kind == "edge" and self.resize_value.value() <= 100:
            self.resize_value.setValue(1920)
        if mode == "size":
            t = self._target_bytes()
            self.estimate.setText(tr("Aim: {size} ({pct}% of original)").format(
                size=fmt_size(t), pct=f"{100 * t / max(1, self.total):.0f}"))
        elif mode == "percent":
            t = self.total * (1 - self.percent.value() / 100)
            self.estimate.setText(tr("Aim: about {size}").format(size=fmt_size(t)))
        else:
            self.estimate.setText("")
        self.adjustSize()

    def values(self) -> dict:
        mode = self.mode.currentData()
        kind = self.resize_kind.currentData()
        v: dict = {"mode": mode, "preset": self.preset.currentData(),
                   "resize": {"kind": kind, "value": self.resize_value.value()} if kind else None}
        if mode == "size":
            v["target_bytes"] = self._target_bytes()
            v["ratio"] = min(1.0, v["target_bytes"] / max(1, self.total))
        elif mode == "percent":
            v["percent"] = self.percent.value()
            v["ratio"] = 1 - self.percent.value() / 100
        return v


# =============================================================================
# Metadata
# =============================================================================

META_FIELDS = [  # labels are translated with tr() when shown
    ("make", N_("Camera make")), ("model", N_("Camera model")), ("date", N_("Date taken")),
    ("artist", N_("Author")), ("copyright", N_("Copyright")), ("description", N_("Description")),
    ("lat", N_("GPS latitude")), ("lon", N_("GPS longitude")),
]
_TAG = {"make": 0x010F, "model": 0x0110, "artist": 0x013B, "copyright": 0x8298,
        "description": 0x010E}
EXIF_IFD, GPS_IFD = 0x8769, 0x8825
DATE_ORIGINAL, DATE_DIGITIZED, DATE_TIME = 0x9003, 0x9004, 0x0132


def _clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return str(v).strip("\x00 ").strip()


def _dms_to_deg(dms, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except Exception:
        return None
    deg = d + m / 60 + s / 3600
    if _clean(ref).upper() in ("S", "W"):
        deg = -deg
    return deg


def _deg_to_dms(value: float):
    from PIL.TiffImagePlugin import IFDRational

    value = abs(value)
    d = int(value)
    m_full = (value - d) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(round(s * 10000), 10000))


def parse_date(text: str) -> str:
    """'2024-05-01 13:45[:10]' / '2024:05:01 13:45:10' / '2024-05-01' -> EXIF format."""
    text = text.strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
              "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y:%m:%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, f).strftime("%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
    raise UserError(tr("Can't understand the date “{text}” (use {pattern})").format(
        text=text, pattern="YYYY-MM-DD HH:MM:SS"))


# Whole sentences per coordinate (never build them from a translated "Latitude").
_COORD_ERRORS = {
    "Latitude": (N_("Latitude must be a decimal number (e.g. {example})"),
                 N_("Latitude must be between {lo} and {hi}")),
    "Longitude": (N_("Longitude must be a decimal number (e.g. {example})"),
                  N_("Longitude must be between {lo} and {hi}")),
}


def parse_coord(text: str, limit: float, what: str) -> float:
    """`what` is "Latitude" or "Longitude" (picks the error message)."""
    not_number, out_of_range = _COORD_ERRORS.get(what, _COORD_ERRORS["Latitude"])
    try:
        v = float(text.strip().replace(",", "."))
    except ValueError:
        raise UserError(tr(not_number).format(example="48.8584"))
    if not -limit <= v <= limit:
        raise UserError(tr(out_of_range).format(lo=f"-{limit:g}", hi=f"{limit:g}"))
    return v


def read_metadata(path: Path) -> dict[str, str]:
    """Editable metadata fields of one image as strings ('' when missing)."""
    out = {k: "" for k, _ in META_FIELDS}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(EXIF_IFD)  # TIFF reads sub-IFDs lazily from the open file
            gps = exif.get_ifd(GPS_IFD)
    except Exception:
        return out
    for key, tag in _TAG.items():
        out[key] = _clean(exif.get(tag))
    date = _clean(sub.get(DATE_ORIGINAL) or sub.get(DATE_DIGITIZED) or exif.get(DATE_TIME))
    if date:
        try:
            date = datetime.strptime(date[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    out["date"] = date
    if 2 in gps:
        lat = _dms_to_deg(gps[2], gps.get(1))
        out["lat"] = f"{lat:.6f}" if lat is not None else ""
    if 4 in gps:
        lon = _dms_to_deg(gps[4], gps.get(3))
        out["lon"] = f"{lon:.6f}" if lon is not None else ""
    return out


def image_summary(path: Path) -> str:
    """One-line read-only description: size, format, camera settings."""
    try:
        with Image.open(path) as im:
            fmt, (w, h) = im.format, im.size
            exif = im.getexif()
            sub = exif.get_ifd(EXIF_IFD)
    except Exception:
        return path.name
    bits = [_dims(w, h), fmt or formats.ext(path).upper(), fmt_size(path.stat().st_size)]
    if sub.get(0x829A):
        t = float(sub[0x829A])
        bits.append(f"1/{round(1 / t)} s" if 0 < t < 1 else f"{t:g} s")
    if sub.get(0x829D):
        bits.append(f"f/{float(sub[0x829D]):.1f}")
    if sub.get(0x8827):
        iso = sub[0x8827]
        bits.append(f"ISO {iso[0] if isinstance(iso, tuple) else iso}")
    if sub.get(0x920A):
        bits.append(f"{float(sub[0x920A]):g} mm")
    if sub.get(0xA434):
        bits.append(_clean(sub[0xA434]))
    return " · ".join(bits)


def _ifd_store(exif: Image.Exif, tag: int) -> dict:
    d = exif.get_ifd(tag)
    store = getattr(exif, "_ifds", {}).get(tag)
    return store if isinstance(store, dict) else d


def apply_metadata(exif: Image.Exif, changes: dict[str, str], clear: list[str]) -> Image.Exif:
    """Edit `exif` in place: set fields from `changes`, delete fields in `clear`."""
    for key in clear:
        if key in _TAG:
            exif.pop(_TAG[key], None)
        elif key == "date":
            sub = _ifd_store(exif, EXIF_IFD)
            for t in (DATE_ORIGINAL, DATE_DIGITIZED):
                sub.pop(t, None)
            exif.pop(DATE_TIME, None)
        elif key in ("lat", "lon"):
            gps = _ifd_store(exif, GPS_IFD)
            for t in ((1, 2) if key == "lat" else (3, 4)):
                gps.pop(t, None)
    for key, value in changes.items():
        if key in _TAG:
            exif[_TAG[key]] = value
        elif key == "date":
            v = parse_date(value)
            sub = _ifd_store(exif, EXIF_IFD)
            sub[DATE_ORIGINAL] = v
            sub[DATE_DIGITIZED] = v
            exif[DATE_TIME] = v
        elif key in ("lat", "lon"):
            gps = _ifd_store(exif, GPS_IFD)
            if key == "lat":
                v = parse_coord(value, 90, "Latitude")
                gps[1], gps[2] = ("N" if v >= 0 else "S"), _deg_to_dms(v)
            else:
                v = parse_coord(value, 180, "Longitude")
                gps[3], gps[4] = ("E" if v >= 0 else "W"), _deg_to_dms(v)
            gps.setdefault(0, b"\x02\x03\x00\x00")
    gps = exif.get_ifd(GPS_IFD)
    if not any(k in gps for k in (1, 2, 3, 4)):
        gps.clear()
        getattr(exif, "_ifds", {}).pop(GPS_IFD, None)
        exif.pop(GPS_IFD, None)
    return exif


def _jpeg_with_exif(data: bytes, exif_payload: bytes | None, strip: bool) -> bytes:
    """Rewrite JPEG header segments losslessly: replace EXIF; with `strip` also drop XMP/IPTC/comments."""
    if data[:2] != b"\xff\xd8":
        raise UserError(tr("Not a valid JPEG file"))
    i, n = 2, len(data)
    kept: list[bytes] = []
    while i < n:
        if data[i] != 0xFF:
            raise UserError(tr("Corrupt JPEG header"))
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:  # start of scan: the rest is image data
            break
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            kept.append(data[i:i + 2])
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + length]
        i += 2 + length
        if marker == 0xE1 and seg[4:10] == b"Exif\x00\x00":
            continue
        if strip and marker in (0xE1, 0xED, 0xFE):
            continue
        kept.append(seg)
    rest = data[i:]
    new_exif = b""
    if exif_payload:
        if len(exif_payload) > 65533:
            raise UserError(tr("Metadata is too large for a JPEG file"))
        new_exif = b"\xff\xe1" + (len(exif_payload) + 2).to_bytes(2, "big") + exif_payload
    pos = 1 if kept and kept[0][:2] == b"\xff\xe0" else 0  # keep JFIF first
    kept.insert(pos, new_exif)
    return b"\xff\xd8" + b"".join(kept) + rest


def metadata_run(src: Path, ctx) -> Path:
    """Write edited (or stripped) metadata to '<name>-edited.<ext>' or over the original."""
    src = Path(src)
    p = ctx.params or {}
    strip = bool(p.get("strip"))
    changes = {k: v for k, v in (p.get("set") or {}).items() if str(v).strip()}
    clear = list(p.get("clear") or [])
    fmt = canonical_ext(formats.ext(src))
    with Image.open(src) as im:
        exif = im.getexif()
        orientation = exif.get(0x0112, 1)
        if strip:
            exif = Image.Exif()
            if orientation != 1:
                exif[0x0112] = orientation  # keep the picture the right way up
        else:
            apply_metadata(exif, changes, clear)
        payload = exif.tobytes() if len(exif) or exif.get_ifd(EXIF_IFD) else None
        if fmt == "jpg":
            data = _jpeg_with_exif(src.read_bytes(), payload, strip)
        else:
            im.load()
            img = im.copy()
            buf = io.BytesIO()
            kw: dict = {}
            if fmt == "tiff":
                kw["compression"] = im.info.get("compression", "tiff_lzw") or "tiff_lzw"
                if exif.get_ifd(EXIF_IFD) or exif.get_ifd(GPS_IFD):
                    kw["compression"] = "raw"  # libtiff can't write EXIF/GPS sub-IFDs
            if fmt in ("webp", "heic", "avif"):
                kw["quality"] = 95 if fmt != "webp" or not im.info.get("lossless") else 100
                if fmt == "webp" and im.info.get("lossless"):
                    kw["lossless"] = True
            if fmt == "webp" and not strip and im.info.get("xmp"):
                kw["xmp"] = im.info["xmp"]
            save_image(img, buf, fmt, exif=payload, **kw)
            data = buf.getvalue()
    if p.get("overwrite"):
        tmp = src.with_name(f".{src.name}.satsuma-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, src)
        return src
    out = ctx.output_path(src, name=localized_stem(tr("{name}-edited"), src))
    out.write_bytes(data)
    return out


class MetadataDialog(ToolDialog):
    """Shows shared metadata; edits apply to every selected image."""

    def __init__(self, paths: list[Path]):
        n = len(paths)
        if n == 1:
            sub = image_summary(paths[0])
        else:
            sub = tr("{n} images · fields that differ are blank; blank = unchanged").format(n=n)
        super().__init__(tr("Edit metadata"), sub, primary=tr("Save"))
        metas = [read_metadata(p) for p in paths]
        self.initial: dict[str, str] = {}
        self.edits: dict[str, QWidget] = {}
        example = tr("e.g. {value}")
        placeholders = {"date": "YYYY-MM-DD HH:MM:SS",  # the input format, not translated
                        "lat": example.format(value="48.858370"),
                        "lon": example.format(value="2.294481")}
        for key, label in META_FIELDS:
            values = {m[key] for m in metas}
            shared = values.pop() if len(values) == 1 else ""
            ph = tr("(multiple values)") if len({m[key] for m in metas}) > 1 else placeholders.get(key, "")
            self.initial[key] = shared
            self.edits[key] = self.add_text(tr(label), shared, ph)
        self.strip = QCheckBox(tr("Remove all metadata"))
        self.strip.toggled.connect(self._sync)
        self.overwrite = QCheckBox(tr("Overwrite originals"))
        self.body.addWidget(self.strip)
        self.body.addWidget(self.overwrite)
        self.error = QLabel(objectName="subtitle", wordWrap=True)
        self.error.setStyleSheet("color: #ff6b6b;")
        self.error.hide()
        self.body.addWidget(self.error)

    def _sync(self, *_):
        for e in self.edits.values():
            e.setEnabled(not self.strip.isChecked())

    def values(self) -> dict:
        changes, clear = {}, []
        for key, edit in self.edits.items():
            text = edit.text().strip()
            if text == self.initial[key]:
                continue
            if text:
                changes[key] = text
            elif self.initial[key]:
                clear.append(key)
        return {"set": changes, "clear": clear, "strip": self.strip.isChecked(),
                "overwrite": self.overwrite.isChecked()}

    def accept(self) -> None:
        v = self.values()
        try:
            if not v["strip"]:
                apply_metadata(Image.Exif(), v["set"], [])
        except UserError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        super().accept()


def metadata_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = MetadataDialog(paths)
    return dlg.values() if dlg.exec() else None


# =============================================================================
# Crop
# =============================================================================

def crop_box(size: tuple[int, int], rect: list[float], aspect: float | None) -> tuple[int, int, int, int]:
    """Pixel box for relative `rect` (x, y, w, h) on an image of `size`.

    Without `aspect` the relative rect is used as-is. With a pixel `aspect` (w/h)
    the box keeps that aspect and the same relative area, centred on the same
    relative point (so differently shaped images get an equivalent crop).
    """
    W, H = size
    x, y, rw, rh = (float(v) for v in rect)
    if not aspect:
        box = (x * W, y * H, (x + rw) * W, (y + rh) * H)
    else:
        cx, cy = (x + rw / 2) * W, (y + rh / 2) * H
        h = math.sqrt(max(1e-9, rw * rh * W * H / aspect))
        w = h * aspect
        if w > W:
            w, h = W, W / aspect
        if h > H:
            w, h = H * aspect, H
        x0 = min(max(0.0, cx - w / 2), W - w)
        y0 = min(max(0.0, cy - h / 2), H - h)
        box = (x0, y0, x0 + w, y0 + h)
    l, t, r, b = (round(v) for v in box)
    l, t = max(0, min(l, W - 1)), max(0, min(t, H - 1))
    r, b = max(l + 1, min(r, W)), max(t + 1, min(b, H))
    return l, t, r, b


def crop_run(src: Path, ctx) -> Path:
    """Crop one image (same relative crop for every file); '<name>-cropped.<ext>'."""
    src = Path(src)
    p = ctx.params or {}
    rect = p.get("rect") or [0, 0, 1, 1]
    size = p.get("size")
    aspect = p.get("aspect")
    if size:
        aspect = size[0] / size[1]
    img = open_image(src)
    ctx.progress(0.4, None)
    info = dict(img.info)
    out_img = img.crop(crop_box(img.size, rect, aspect))
    if size:
        out_img = out_img.resize((int(size[0]), int(size[1])), Image.Resampling.LANCZOS)
    out_img.info.update(info)
    ctx.check_cancel()
    ext = _out_ext(src)
    out = ctx.output_path(src, ext, name=localized_stem(tr("{name}-cropped"), src))
    save_image(out_img, out, ext)
    return out


class CropView(QWidget):
    """Image preview with a draggable / resizable crop rectangle (relative coords)."""

    changed = Signal()
    HANDLE = 9

    def __init__(self, qimage, parent: QWidget | None = None):
        super().__init__(parent)
        self.pm = QPixmap.fromImage(qimage)
        self.crop = QRectF(0.0, 0.0, 1.0, 1.0)
        self.aspect: float | None = None   # pixel aspect (w/h) of the crop, None = free
        self._drag: str | None = None
        self._start = QPointF()
        self._start_rect = QRectF()
        self.setMouseTracking(True)
        self.setMinimumSize(520, 380)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # geometry helpers
    def _img_rect(self) -> QRectF:
        m = 14
        aw, ah = max(1, self.width() - 2 * m), max(1, self.height() - 2 * m)
        s = min(aw / self.pm.width(), ah / self.pm.height())
        w, h = self.pm.width() * s, self.pm.height() * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _rel_aspect(self) -> float | None:
        if not self.aspect:
            return None
        return self.aspect * self.pm.height() / self.pm.width()

    def _to_widget(self, r: QRectF) -> QRectF:
        ir = self._img_rect()
        return QRectF(ir.x() + r.x() * ir.width(), ir.y() + r.y() * ir.height(),
                      r.width() * ir.width(), r.height() * ir.height())

    def _to_rel(self, p: QPointF) -> QPointF:
        ir = self._img_rect()
        return QPointF(min(1.0, max(0.0, (p.x() - ir.x()) / ir.width())),
                       min(1.0, max(0.0, (p.y() - ir.y()) / ir.height())))

    def set_aspect(self, aspect: float | None) -> None:
        self.aspect = aspect
        ar = self._rel_aspect()
        if ar:
            c = self.crop.center()
            w, h = 1.0, 1.0 / ar
            if h > 1:
                w, h = ar, 1.0
            x = min(max(0.0, c.x() - w / 2), 1 - w)
            y = min(max(0.0, c.y() - h / 2), 1 - h)
            self.crop = QRectF(x, y, w, h)
        self.update()
        self.changed.emit()

    def reset(self) -> None:
        self.crop = QRectF(0, 0, 1, 1)
        self.set_aspect(self.aspect)

    def _hit(self, pos: QPointF) -> str:
        r = self._to_widget(self.crop)
        hs = self.HANDLE + 3
        near_l, near_r = abs(pos.x() - r.left()) <= hs, abs(pos.x() - r.right()) <= hs
        near_t, near_b = abs(pos.y() - r.top()) <= hs, abs(pos.y() - r.bottom()) <= hs
        inside_x = r.left() - hs <= pos.x() <= r.right() + hs
        inside_y = r.top() - hs <= pos.y() <= r.bottom() + hs
        h = ""
        if inside_x and inside_y:
            if near_t:
                h += "t"
            elif near_b:
                h += "b"
            if near_l:
                h += "l"
            elif near_r:
                h += "r"
        if h:
            return h
        return "move" if r.contains(pos) else "new"

    _CURSORS = {"t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor, "l": Qt.SizeHorCursor,
                "r": Qt.SizeHorCursor, "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
                "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor, "move": Qt.SizeAllCursor,
                "new": Qt.CrossCursor}

    # events
    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        self._drag = self._hit(pos)
        self._start = self._to_rel(pos)
        self._start_rect = QRectF(self.crop)

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if not self._drag:
            self.setCursor(self._CURSORS.get(self._hit(pos), Qt.ArrowCursor))
            return
        p = self._to_rel(pos)
        if self._drag == "move":
            r = QRectF(self._start_rect)
            r.moveTo(min(max(0.0, r.x() + p.x() - self._start.x()), 1 - r.width()),
                     min(max(0.0, r.y() + p.y() - self._start.y()), 1 - r.height()))
            self.crop = r
        elif self._drag == "new":
            self.crop = self._from_anchor(self._start, p)
        elif len(self._drag) == 2:
            sr = self._start_rect
            ax = sr.right() if "l" in self._drag else sr.left()
            ay = sr.bottom() if "t" in self._drag else sr.top()
            self.crop = self._from_anchor(QPointF(ax, ay), p)
        else:
            self.crop = self._edge(self._drag, p)
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag == "new" and (self.crop.width() < 0.01 or self.crop.height() < 0.01):
            self.crop = QRectF(self._start_rect)
            self.update()
            self.changed.emit()
        self._drag = None

    def _from_anchor(self, a: QPointF, p: QPointF) -> QRectF:
        sx = 1 if p.x() >= a.x() else -1
        sy = 1 if p.y() >= a.y() else -1
        w, h = abs(p.x() - a.x()), abs(p.y() - a.y())
        maxw = (1 - a.x()) if sx > 0 else a.x()
        maxh = (1 - a.y()) if sy > 0 else a.y()
        ar = self._rel_aspect()
        if ar:
            if h == 0 or w / max(h, 1e-9) > ar:
                h = w / ar
            else:
                w = h * ar
            if w > maxw:
                w, h = maxw, maxw / ar
            if h > maxh:
                w, h = maxh * ar, maxh
        else:
            w, h = min(w, maxw), min(h, maxh)
        x = a.x() if sx > 0 else a.x() - w
        y = a.y() if sy > 0 else a.y() - h
        return QRectF(x, y, max(w, 0.0), max(h, 0.0))

    def _edge(self, edge: str, p: QPointF) -> QRectF:
        sr = QRectF(self._start_rect)
        ar = self._rel_aspect()
        x0, y0, x1, y1 = sr.left(), sr.top(), sr.right(), sr.bottom()
        if edge == "l":
            x0 = min(p.x(), x1 - 0.01)
        elif edge == "r":
            x1 = max(p.x(), x0 + 0.01)
        elif edge == "t":
            y0 = min(p.y(), y1 - 0.01)
        elif edge == "b":
            y1 = max(p.y(), y0 + 0.01)
        r = QRectF(QPointF(x0, y0), QPointF(x1, y1))
        if ar:
            c = sr.center()
            if edge in ("l", "r"):
                w = r.width()
                h = min(w / ar, 1.0)
                w = h * ar
                if edge == "l":
                    r = QRectF(x1 - w, c.y() - h / 2, w, h)
                else:
                    r = QRectF(x0, c.y() - h / 2, w, h)
            else:
                h = r.height()
                w = min(h * ar, 1.0)
                h = w / ar
                if edge == "t":
                    r = QRectF(c.x() - w / 2, y1 - h, w, h)
                else:
                    r = QRectF(c.x() - w / 2, y0, w, h)
            r.moveTo(min(max(0.0, r.x()), 1 - r.width()), min(max(0.0, r.y()), 1 - r.height()))
        return r

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        ir = self._img_rect()
        p.drawPixmap(ir, self.pm, QRectF(self.pm.rect()))
        cr = self._to_widget(self.crop)
        shade = QPainterPath()
        shade.addRect(ir)
        hole = QPainterPath()
        hole.addRect(cr)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 150))
        p.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for k in (1, 2):
            x = cr.left() + cr.width() * k / 3
            y = cr.top() + cr.height() * k / 3
            p.drawLine(QPointF(x, cr.top()), QPointF(x, cr.bottom()))
            p.drawLine(QPointF(cr.left(), y), QPointF(cr.right(), y))
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(cr)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FF8A1F"))
        hs = self.HANDLE
        for hx in (cr.left(), cr.center().x(), cr.right()):
            for hy in (cr.top(), cr.center().y(), cr.bottom()):
                if hx == cr.center().x() and hy == cr.center().y():
                    continue
                p.drawRect(QRectF(hx - hs / 2, hy - hs / 2, hs, hs))
        p.end()


# Keys are internal ids (and the shown text, translated for the named ones).
ASPECTS = {N_("Free"): None, N_("Original"): "orig", "1:1": 1.0, "4:3": 4 / 3, "3:2": 3 / 2,
           "16:9": 16 / 9, "9:16": 9 / 16, "4:5": 4 / 5}


class CropDialog(ToolDialog):
    """Interactive crop with aspect presets or an exact output size."""

    def __init__(self, paths: list[Path]):
        n = len(paths)
        if n == 1:
            sub = paths[0].name
        else:
            sub = tr("{n} images · the same crop is applied to each (centred)").format(n=n)
        super().__init__(tr("Crop"), sub, primary=tr("Crop"))
        self.setMinimumWidth(640)
        self.full_size = _oriented_size(paths[0])
        preview = open_image(paths[0], size_hint=1400)
        preview.thumbnail((1400, 1400))
        self.view = CropView(pil_to_qimage(preview))
        # The preview maps mouse positions to image coordinates itself: never mirror it.
        self.view.setLayoutDirection(Qt.LeftToRight)
        self.body.insertWidget(0, self.view, 1)
        self.aspect = self.add_combo(tr("Aspect"), {tr(k): k for k in ASPECTS})
        self.exact = QCheckBox(tr("Exact size"))
        self.w_spin = QSpinBox()
        self.h_spin = QSpinBox()
        for s, v in ((self.w_spin, self.full_size[0]), (self.h_spin, self.full_size[1])):
            s.setRange(1, 30000)
            s.setValue(v)
            s.setSuffix(" px")
            s.setEnabled(False)
        reset = QPushButton(tr("Reset"))
        reset.clicked.connect(self.view.reset)
        self.add_row(tr("Output"), _hrow(self.exact, self.w_spin, QLabel("×"), self.h_spin, None, reset))
        self.info = QLabel(objectName="subtitle", wordWrap=True)
        self.body.addWidget(self.info)
        self.aspect.currentIndexChanged.connect(self._aspect_changed)
        self.exact.toggled.connect(self._aspect_changed)
        self.w_spin.valueChanged.connect(self._aspect_changed)
        self.h_spin.valueChanged.connect(self._aspect_changed)
        self.view.changed.connect(self._update_info)
        self._update_info()

    def _current_aspect(self) -> float | None:
        if self.exact.isChecked():
            return self.w_spin.value() / max(1, self.h_spin.value())
        a = ASPECTS.get(self.aspect.currentData())
        if a == "orig":
            return self.full_size[0] / self.full_size[1]
        return a

    def _aspect_changed(self, *_):
        exact = self.exact.isChecked()
        self.w_spin.setEnabled(exact)
        self.h_spin.setEnabled(exact)
        self.aspect.setEnabled(not exact)
        self.view.set_aspect(self._current_aspect())

    def _update_info(self):
        l, t, r, b = crop_box(self.full_size, self._rect(), self._current_aspect())
        crop, full = _dims(r - l, b - t), _dims(*self.full_size)
        if self.exact.isChecked():
            txt = tr("Crop: {crop} px from {full} → resized to {out}").format(
                crop=crop, full=full, out=_dims(self.w_spin.value(), self.h_spin.value()))
        else:
            txt = tr("Crop: {crop} px from {full}").format(crop=crop, full=full)
        self.info.setText(txt)

    def _rect(self) -> list[float]:
        c = self.view.crop
        return [c.x(), c.y(), c.width(), c.height()]

    def values(self) -> dict:
        return {"rect": self._rect(), "aspect": self._current_aspect(),
                "size": [self.w_spin.value(), self.h_spin.value()] if self.exact.isChecked() else None}


def crop_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = CropDialog(paths)
    return dlg.values() if dlg.exec() else None


# =============================================================================
# Reorderable file list (Create PDF, Collage)
# =============================================================================

class FileOrderList(QWidget):
    """List of files with drag-reorder plus Up / Down / Remove buttons."""

    changed = Signal()

    def __init__(self, paths: list[Path], parent: QWidget | None = None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMinimumHeight(150)
        for p in paths:
            it = QListWidgetItem(p.name)
            it.setData(Qt.UserRole, str(p))
            it.setToolTip(str(p))
            self.list.addItem(it)
        self.list.setCurrentRow(0)
        self.list.model().rowsMoved.connect(lambda *a: self.changed.emit())
        v.addWidget(self.list)
        up, down, rm, rev = (QPushButton(t) for t in (tr("↑ Up"), tr("↓ Down"), tr("Remove"),
                                                       tr("Reverse")))
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        rm.clicked.connect(self._remove)
        rev.clicked.connect(self._reverse)
        v.addWidget(_hrow(up, down, rev, None, rm))

    def _move(self, d: int) -> None:
        row = self.list.currentRow()
        new = row + d
        if row < 0 or not 0 <= new < self.list.count():
            return
        it = self.list.takeItem(row)
        self.list.insertItem(new, it)
        self.list.setCurrentRow(new)
        self.changed.emit()

    def _remove(self) -> None:
        if self.list.count() <= 1:
            return
        self.list.takeItem(self.list.currentRow())
        self.changed.emit()

    def _reverse(self) -> None:
        items = [self.list.takeItem(0) for _ in range(self.list.count())]
        for it in reversed(items):
            self.list.addItem(it)
        self.changed.emit()

    def paths(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]


# =============================================================================
# Create PDF
# =============================================================================

PAGE_SIZES = {"a4": (595.28, 841.89), "letter": (612.0, 792.0)}
PAGE_CHOICES = {N_("Fit image"): "fit", "A4": "a4", N_("Letter"): "letter"}
DEFAULT_PDF_NAME = N_("Combined")  # default output file name, in the UI language
LOSSLESS_SRC = {"png", "bmp", "gif", "tiff", "ico", "svg"}


def _pdf_image_bytes(src: Path, img: Image.Image) -> bytes:
    """Bytes to embed: original JPEG when safe, else PNG (lossless/alpha) or JPEG."""
    e = canonical_ext(formats.ext(src))
    if e == "jpg" and img.mode in ("RGB", "L"):
        try:
            with Image.open(src) as im:
                if im.getexif().get(0x0112, 1) == 1 and im.mode in ("RGB", "L"):
                    return src.read_bytes()
        except Exception:
            pass
    if has_alpha(img) or e in LOSSLESS_SRC:
        work = img if img.mode in ("RGB", "RGBA", "L", "LA") else img.convert(
            "RGBA" if has_alpha(img) else "RGB")
        return encode_image(work, "png", exif=None)
    return encode_image(img, "jpg", exif=None)


def make_pdf_run(paths: list[Path], ctx) -> Path:
    """Combine images into one PDF, one page per image."""
    import pymupdf

    p = ctx.params or {}
    order = [Path(x) for x in (p.get("order") or paths)]
    if not order:
        raise UserError(tr("No images to combine"))
    page_kind = p.get("page", "fit")
    margin = float(p.get("margin_mm", 0)) * 72 / 25.4
    doc = pymupdf.open()
    try:
        for i, src in enumerate(order):
            ctx.check_cancel()
            ctx.progress(i / len(order), tr("Page {page}/{total} · {name}").format(
                page=i + 1, total=len(order), name=src.name))
            img = open_image(src)
            data = _pdf_image_bytes(src, img)
            iw, ih = img.size
            if page_kind in PAGE_SIZES:
                pw, ph = PAGE_SIZES[page_kind]
                if iw > ih:
                    pw, ph = ph, pw
                aw, ah = max(1.0, pw - 2 * margin), max(1.0, ph - 2 * margin)
                s = min(aw / iw, ah / ih)
                w, h = iw * s, ih * s
                rect = pymupdf.Rect((pw - w) / 2, (ph - h) / 2, (pw + w) / 2, (ph + h) / 2)
            else:
                dpi = pdf_resolution(img)
                w, h = iw * 72 / dpi, ih * 72 / dpi
                pw, ph = w + 2 * margin, h + 2 * margin
                rect = pymupdf.Rect(margin, margin, margin + w, margin + h)
            page = doc.new_page(width=pw, height=ph)
            page.insert_image(rect, stream=data)
        ctx.progress(0.98, tr("Saving PDF"))
        default = tr(DEFAULT_PDF_NAME)
        out = ctx.output_path(order[0], "pdf", name=(p.get("name") or default).strip() or default)
        doc.save(str(out), garbage=3, deflate=True)
    finally:
        doc.close()
    return out


class MakePdfDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Create PDF"),
                         tr("{n} images · one page per image, drag to reorder").format(n=len(paths)),
                         primary=tr("Create PDF"))
        self.files = FileOrderList(paths)
        self.body.insertWidget(0, self.files, 1)
        self.page = self.add_combo(tr("Page size"), _combo_items(PAGE_CHOICES))
        self.margin = self.add_spin(tr("Margin"), 0, 100, 0, suffix=" mm")
        self.page.currentIndexChanged.connect(
            lambda: self.margin.setValue(10 if self.page.currentData() != "fit" and self.margin.value() == 0
                                         else self.margin.value()))
        self.name = self.add_text(tr("File name"), tr(DEFAULT_PDF_NAME))

    def values(self) -> dict:
        return {"order": self.files.paths(), "page": self.page.currentData(),
                "margin_mm": self.margin.value(),
                "name": self.name.text().strip() or tr(DEFAULT_PDF_NAME)}


def make_pdf_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = MakePdfDialog(paths)
    return dlg.values() if dlg.exec() else None


# =============================================================================
# Collage
# =============================================================================

LAYOUTS = {N_("Grid"): "grid", N_("Row"): "row", N_("Column"): "column", N_("Featured"): "featured"}
DEFAULT_COLLAGE_NAME = N_("Collage")  # default output file name, in the UI language


def _median_aspect(aspects: list[float], lo: float, hi: float) -> float:
    return min(hi, max(lo, statistics.median(aspects))) if aspects else 1.0


def _grid(aspects: list[float], x: float, y: float, inner: float, sp: float, max_cols: int | None = None,
          ar_range=(0.5, 2.0)):
    n = len(aspects)
    cols = max_cols or math.ceil(math.sqrt(n))
    cols = max(1, min(cols, n))
    rows = math.ceil(n / cols)
    cols = math.ceil(n / rows)  # balance rows
    cw = (inner - (cols - 1) * sp) / cols
    ch = cw / _median_aspect(aspects, *ar_range)
    tiles = []
    for i in range(n):
        r, c = divmod(i, cols)
        k = cols if r < rows - 1 else n - r * cols
        off = (inner - (k * cw + (k - 1) * sp)) / 2
        tiles.append((x + off + c * (cw + sp), y + r * (ch + sp), cw, ch, "cover"))
    return tiles, rows * ch + (rows - 1) * sp


def collage_layout(aspects: list[float], layout: str, width: int, spacing: float):
    """Tile boxes [(x, y, w, h, fit)] for each image, and the canvas height."""
    sp = float(spacing)
    inner = width - 2 * sp
    n = len(aspects)
    if n == 0:
        return [], int(width)
    if layout == "row":
        h = (inner - (n - 1) * sp) / sum(aspects)
        x, tiles = sp, []
        for a in aspects:
            tiles.append((x, sp, a * h, h, "exact"))
            x += a * h + sp
        return tiles, h + 2 * sp
    if layout == "column":
        y, tiles = sp, []
        for a in aspects:
            h = inner / a
            tiles.append((sp, y, inner, h, "exact"))
            y += h + sp
        return tiles, y
    if layout == "featured" and n >= 2:
        bh = inner / min(2.0, max(1.2, aspects[0]))
        tiles = [(sp, sp, inner, bh, "cover")]
        rest = aspects[1:]
        sub, sh = _grid(rest, sp, sp + bh + sp, inner, sp, max_cols=min(4, len(rest)),
                        ar_range=(0.75, 1.5))
        return tiles + sub, bh + sh + 3 * sp
    tiles, gh = _grid(aspects, sp, sp, inner, sp)
    return tiles, gh + 2 * sp


def _rounded_mask(size: tuple[int, int], radius: float) -> Image.Image:
    ss = 4
    big = Image.new("L", (size[0] * ss, size[1] * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1),
                                          radius=radius * ss, fill=255)
    return big.resize(size, Image.Resampling.LANCZOS)


def parse_color(text: str) -> tuple[int, int, int, int]:
    t = (text or "#ffffff").strip().lstrip("#")
    if t.lower() == "transparent":
        return (0, 0, 0, 0)
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    try:
        r, g, b = int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
        a = int(t[6:8], 16) if len(t) == 8 else 255
    except (ValueError, IndexError):
        raise UserError(tr("Invalid colour: {color}").format(color=text))
    return (r, g, b, a)


def render_collage(images: list[Image.Image], layout: str = "grid", width: int = 2400,
                   spacing: float = 16, radius: float = 0, background: str = "#ffffff",
                   resample=Image.Resampling.LANCZOS) -> Image.Image:
    """Compose `images` into one RGBA image."""
    aspects = [max(0.05, im.width / max(1, im.height)) for im in images]
    tiles, height = collage_layout(aspects, layout, width, spacing)
    canvas = Image.new("RGBA", (int(width), max(1, round(height))), parse_color(background))
    for im, (x, y, w, h, fit) in zip(images, tiles):
        l, t = round(x), round(y)
        tw, th = max(1, round(x + w) - l), max(1, round(y + h) - t)
        src = im.convert("RGBA")
        tile = (ImageOps.fit(src, (tw, th), method=resample) if fit == "cover"
                else src.resize((tw, th), resample))
        mask = tile.getchannel("A")
        if radius > 0:
            r = min(radius, tw / 2, th / 2)
            rm = _rounded_mask((tw, th), r)
            mask = Image.composite(mask, Image.new("L", (tw, th), 0), rm) if has_alpha(im) else rm
        canvas.paste(tile, (l, t), mask)
    return canvas


def collage_run(paths: list[Path], ctx) -> Path:
    """Render the collage at full size and save 'Collage.png/jpg' next to the first file."""
    p = ctx.params or {}
    order = [Path(x) for x in (p.get("order") or paths)]
    width = int(p.get("width", 2400))
    fmt = canonical_ext(p.get("format", "png"))
    if fmt not in ("png", "jpg"):
        fmt = "png"
    imgs = []
    for i, src in enumerate(order):
        ctx.check_cancel()
        ctx.progress(0.7 * i / len(order), tr("Loading {name}").format(name=src.name))
        im = open_image(src, size_hint=width)
        im.thumbnail((width, width * 4), Image.Resampling.LANCZOS)
        imgs.append(im)
    ctx.progress(0.75, tr("Composing"))
    bg = p.get("background", "#ffffff")
    out_img = render_collage(imgs, p.get("layout", "grid"), width, float(p.get("spacing", 16)),
                             float(p.get("radius", 0)), bg)
    ctx.check_cancel()
    out = ctx.output_path(order[0], fmt, name=p.get("name") or tr(DEFAULT_COLLAGE_NAME))
    if fmt == "jpg":
        r, g, b, a = parse_color(bg)
        out_img = flatten(out_img, (r, g, b) if a else (255, 255, 255))
    save_image(out_img, out, fmt, exif=None, icc=None)
    return out


class CollageDialog(ToolDialog):
    """Layout options with a live preview."""

    PREVIEW_W = 460
    PREVIEW_H = 440

    def __init__(self, paths: list[Path]):
        super().__init__(tr("Collage"), tr("{n} images").format(n=len(paths)),
                         primary=tr("Create collage"))
        self.setMinimumWidth(960)
        self.thumbs: dict[str, Image.Image] = {}
        for p in paths:
            try:
                im = open_image(p, size_hint=640)
                im.thumbnail((640, 2560))
            except UserError:
                im = Image.new("RGB", (100, 100), "#444")
            self.thumbs[str(p)] = im
        self.bg = "#ffffff"
        # left: options + order list, right: preview
        top = QHBoxLayout()
        left = QVBoxLayout()
        self.files = FileOrderList(paths)
        self.preview = QLabel(alignment=Qt.AlignCenter)
        self.preview.setFixedWidth(self.PREVIEW_W + 20)
        self.preview.setMinimumHeight(self.PREVIEW_H + 20)
        self.preview.setStyleSheet("background: #141417; border-radius: 8px;")
        # move the form into the left column
        self.body.removeItem(self.form)
        left.addLayout(self.form)
        left.addWidget(QLabel(tr("Order"), objectName="section"))
        left.addWidget(self.files, 1)
        top.addLayout(left, 1)
        top.addWidget(self.preview, 1)
        self.body.addLayout(top, 1)
        self.layout_combo = self.add_combo(tr("Layout"), _combo_items(LAYOUTS))
        self.spacing = self.add_spin(tr("Spacing"), 0, 400, 16, suffix=" px")
        self.radius = self.add_spin(tr("Corner radius"), 0, 1000, 0, suffix=" px")
        self.color_btn = QPushButton()
        self.color_btn.clicked.connect(self._pick_color)
        self.transparent = QCheckBox(tr("Transparent"))
        self.add_row(tr("Background"), _hrow(self.color_btn, self.transparent, None))
        self.width_spin = self.add_spin(tr("Output width"), 200, 20000, 2400, suffix=" px")
        self.format = self.add_combo(tr("Format"), {"PNG": "png", "JPG": "jpg"})  # format names
        self._paint_color_btn()
        for sig in (self.layout_combo.currentIndexChanged, self.spacing.valueChanged,
                    self.radius.valueChanged, self.width_spin.valueChanged, self.transparent.toggled,
                    self.files.changed, self.format.currentIndexChanged):
            sig.connect(self.refresh)
        self.refresh()

    def _paint_color_btn(self) -> None:
        self.color_btn.setText(self.bg)
        self.color_btn.setStyleSheet(
            f"background: {self.bg}; color: {'#111' if QColor(self.bg).lightness() > 128 else '#eee'};")

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self.bg), self, tr("Background colour"))
        if c.isValid():
            self.bg = c.name()
            self._paint_color_btn()
            self.refresh()

    def background(self) -> str:
        if self.transparent.isChecked() and self.format.currentData() == "png":
            return "transparent"
        return self.bg

    def refresh(self, *_) -> None:
        self.transparent.setEnabled(self.format.currentData() == "png")
        imgs = [self.thumbs[p] for p in self.files.paths() if p in self.thumbs]
        width = self.width_spin.value()
        scale = self.PREVIEW_W / width
        img = render_collage(imgs, self.layout_combo.currentData(), self.PREVIEW_W,
                             self.spacing.value() * scale, self.radius.value() * scale,
                             self.background(), resample=Image.Resampling.BILINEAR)
        max_h = self.PREVIEW_H
        if img.height > max_h:
            img = img.resize((max(1, round(img.width * max_h / img.height)), max_h),
                             Image.Resampling.BILINEAR)
        if self.background() == "transparent":  # checkerboard so transparency is visible
            board = Image.new("RGBA", img.size, "#cccccc")
            d = ImageDraw.Draw(board)
            for yy in range(0, img.height, 10):
                for xx in range((yy // 10) % 2 * 10, img.width, 20):
                    d.rectangle((xx, yy, xx + 9, yy + 9), fill="#eeeeee")
            board.alpha_composite(img)
            img = board
        self.preview.setPixmap(QPixmap.fromImage(pil_to_qimage(img)))
        _, h = collage_layout([max(0.05, i.width / max(1, i.height)) for i in imgs],
                              self.layout_combo.currentData(), width, self.spacing.value())
        self.preview.setToolTip(tr("Output size: {size} px").format(size=_dims(width, round(h))))

    def values(self) -> dict:
        return {"order": self.files.paths(), "layout": self.layout_combo.currentData(),
                "spacing": self.spacing.value(), "radius": self.radius.value(),
                "background": self.background(), "width": self.width_spin.value(),
                "format": self.format.currentData()}


def collage_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = CollageDialog(paths)
    return dlg.values() if dlg.exec() else None


# =============================================================================
# Registration
# =============================================================================

add(Option(id="image.compress", label="Compress", mode="tools", run=compress_run,
           dialog=compress_dialog, exts=COMPRESS_EXTS, family="image", order=20,
           description="Shrink images to a target size, percentage or preset"))
add(Option(id="image.metadata", label="Metadata", mode="tools", run=metadata_run,
           dialog=metadata_dialog, exts=META_EXTS, family="image", order=40,
           description="View, edit or remove EXIF metadata"))
add(Option(id="image.crop", label="Crop", mode="tools", run=crop_run, dialog=crop_dialog,
           exts=frozenset(formats.IMAGE_IN), family="image", order=22,
           description="Crop to a region, aspect ratio or exact size"))
add(Option(id="image.make_pdf", label="Create PDF", mode="tools", run=make_pdf_run,
           dialog=make_pdf_dialog, exts=frozenset(formats.IMAGE_IN), family="image", multi=True,
           min_files=2, order=30, description="Combine images into one PDF"))
add(Option(id="image.collage", label="Collage", mode="tools", run=collage_run,
           dialog=collage_dialog, exts=frozenset(formats.IMAGE_IN), family="image", multi=True,
           min_files=2, order=32, description="Arrange images into a collage"))
