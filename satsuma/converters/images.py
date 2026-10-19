"""Image format conversions (Shift wheel) plus shared image open/save helpers.

Other modules reuse:
    from satsuma.converters.images import open_image, save_image
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageOps, features

from .. import formats
from ..config import settings
from ..i18n import tr
from ..registry import Option, UserError, add

log = logging.getLogger(__name__)

# --- codec availability -------------------------------------------------------

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover - optional dependency
    HEIC_OK = False
    log.warning("pillow_heif unavailable: HEIC support disabled")


def _check_avif() -> bool:
    """True when Pillow (built-in) or pillow_heif can both encode and decode AVIF."""
    try:
        if features.check("avif"):
            return True
    except Exception:
        pass
    try:
        import pillow_heif

        reg = getattr(pillow_heif, "register_avif_opener", None)
        if reg is not None:
            reg()
            return "AVIF" in Image.SAVE
    except Exception:
        pass
    return False


AVIF_OK = _check_avif()

# Canonical Pillow format name per extension.
PIL_FORMAT = {
    "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "heic": "HEIF", "heif": "HEIF",
    "tif": "TIFF", "tiff": "TIFF", "avif": "AVIF", "bmp": "BMP", "ico": "ICO", "pdf": "PDF",
    "gif": "GIF",
}
# Targets that can carry EXIF / ICC profiles.
EXIF_TARGETS = {"JPEG", "PNG", "WEBP", "HEIF", "AVIF", "TIFF"}
ICC_TARGETS = {"JPEG", "PNG", "WEBP", "HEIF", "AVIF", "TIFF", "PDF"}
ALPHA_TARGETS = {"PNG", "WEBP", "HEIF", "AVIF", "TIFF", "ICO", "GIF"}
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def canonical_ext(ext: str) -> str:
    """'jpeg' -> 'jpg', 'tif' -> 'tiff', 'heif' -> 'heic'."""
    ext = ext.lower().lstrip(".")
    return {"jpeg": "jpg", "tif": "tiff", "heif": "heic"}.get(ext, ext)


# --- opening --------------------------------------------------------------------

def _render_svg(path: Path, size_hint: int | None = None) -> Image.Image:
    """Rasterise an SVG with QtSvg at 2x its default size (long edge >= 1024 px)."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        raise UserError(tr("{name} is not a valid SVG").format(name=path.name))
    size = renderer.defaultSize()
    w, h = size.width(), size.height()
    if w <= 0 or h <= 0:
        vb = renderer.viewBoxF()
        w, h = (vb.width(), vb.height()) if vb.width() > 0 and vb.height() > 0 else (1024, 1024)
    scale = 2.0
    long_edge = max(w, h) * scale
    if long_edge < 1024:
        scale = 1024 / max(w, h)
    if size_hint:
        scale = min(scale, size_hint / max(w, h))
    scale = min(scale, 8192 / max(w, h))
    pw, ph = max(1, round(w * scale)), max(1, round(h * scale))
    qimg = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
    qimg.fill(Qt.transparent)
    painter = QPainter(qimg)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(painter, QRectF(0, 0, pw, ph))
    painter.end()
    return qimage_to_pil(qimg)


def qimage_to_pil(qimg) -> Image.Image:
    """Convert a QImage to an RGBA PIL image (copies the pixels)."""
    from PySide6.QtGui import QImage

    q = qimg.convertToFormat(QImage.Format_RGBA8888)
    data = bytes(q.constBits())[: q.sizeInBytes()]
    return Image.frombuffer("RGBA", (q.width(), q.height()), data, "raw", "RGBA",
                            q.bytesPerLine(), 1).copy()


def pil_to_qimage(img: Image.Image):
    """Convert a PIL image to a QImage (RGBA, owns its data)."""
    from PySide6.QtGui import QImage

    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    q = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888)
    return q.copy()


def open_image(path: Path | str, size_hint: int | None = None) -> Image.Image:
    """Open any supported image (svg/heic/avif/...) with EXIF orientation applied.

    Returns a loaded, single-frame image (the first frame of animations). `info`
    keeps 'exif' (orientation reset) and 'icc_profile' when present. `size_hint`
    (long edge in px) lets JPEG/SVG decode faster at a reduced size for previews.
    """
    path = Path(path)
    if formats.ext(path) == "svg":
        return _render_svg(path, size_hint)
    try:
        img = Image.open(path)
    except FileNotFoundError:
        raise UserError(tr("{name} not found").format(name=path.name))
    except Exception as e:
        raise UserError(tr("Can't read {name}: {error}").format(name=path.name, error=e))
    try:
        if size_hint and img.format == "JPEG":
            img.draft("RGB" if img.mode == "RGB" else img.mode, (size_hint, size_hint))
        img.seek(0)
        img.load()
        out = ImageOps.exif_transpose(img)
        if out is None:  # pragma: no cover - old Pillow in_place semantics
            out = img
        if out is img:
            out = img.copy()
        for key in ("icc_profile", "dpi", "duration", "loop", "transparency"):
            if key in img.info and key not in out.info:
                out.info[key] = img.info[key]
        return out
    except UserError:
        raise
    except Exception as e:
        raise UserError(tr("Can't decode {name}: {error}").format(name=path.name, error=e))
    finally:
        img.close()


# --- saving ---------------------------------------------------------------------

def has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or (
        img.mode == "P" and "transparency" in img.info)


def to_8bit(img: Image.Image) -> Image.Image:
    """Bring 16-bit / float images down to 8-bit L so they can be converted further."""
    if img.mode in ("I", "F") or img.mode.startswith("I;"):
        info = dict(img.info)
        out = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
        out.info.update(info)
        return out
    return img


def flatten(img: Image.Image, bg: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """Composite onto a solid background, returning RGB (L stays L if opaque)."""
    info = dict(img.info)
    was_cmyk = img.mode == "CMYK"
    img = to_8bit(img)
    if has_alpha(img):
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, bg + (255,))
        base.alpha_composite(rgba)
        out = base.convert("RGB")
    elif img.mode in ("RGB", "L"):
        out = img
    else:
        out = img.convert("RGB")
    if out is not img:
        info.pop("transparency", None)
        out.info.update(info)
        if was_cmyk:
            out.info.pop("icc_profile", None)  # a CMYK profile doesn't describe RGB pixels
    return out


def _rgb_or_rgba(img: Image.Image) -> Image.Image:
    info = dict(img.info)
    was_cmyk = img.mode == "CMYK"
    img = to_8bit(img)
    if img.mode in ("RGB", "RGBA"):
        return img
    out = img.convert("RGBA" if has_alpha(img) else "RGB")
    info.pop("transparency", None)
    out.info.update(info)
    if was_cmyk:
        out.info.pop("icc_profile", None)
    return out


def _exif_bytes(img: Image.Image) -> bytes | None:
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not len(exif) and not exif.get_ifd(0x8769) and not exif.get_ifd(0x8825):
        return None
    try:
        return exif.tobytes()
    except Exception:
        return img.info.get("exif") if isinstance(img.info.get("exif"), bytes) else None


def pdf_resolution(img: Image.Image) -> float:
    """DPI that puts the image on a sensible page (long edge <= A4 height)."""
    return max(72.0, max(img.size) / 11.69)


def _ico_prepare(img: Image.Image) -> tuple[Image.Image, list[tuple[int, int]]]:
    img = _rgb_or_rgba(img).convert("RGBA")
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    if side > 256:
        canvas = canvas.resize((256, 256), Image.Resampling.LANCZOS)
    elif side < 16:
        canvas = canvas.resize((16, 16), Image.Resampling.LANCZOS)
    top = canvas.width
    sizes = [(s, s) for s in ICO_SIZES if s <= top] or [(16, 16)]
    return canvas, sizes


def _tiff_safe_exif(exif_b: bytes) -> bytes | None:
    """libtiff can't write EXIF/GPS sub-IFD pointers: keep only main-IFD tags."""
    try:
        src = Image.Exif()
        src.load(exif_b)
        out = Image.Exif()
        for k, v in src.items():
            if k not in (0x8769, 0x8825, 0xA005) and not isinstance(v, dict):
                out[k] = v
        return out.tobytes() if len(out) else None
    except Exception:
        return None


_KEEP = object()


def save_image(img: Image.Image, out, fmt: str, *, quality: int | None = None,
               exif=_KEEP, icc=_KEEP, optimize: bool = False, **opts):
    """Save `img` as `fmt` (extension like 'jpg', 'png', 'heic', 'pdf', ...).

    Handles mode conversion per target (alpha flattened onto white where needed),
    keeps EXIF / ICC where the format supports them (pass exif=None / icc=None to
    drop, or bytes to override), quality defaults to settings 'jpeg_quality'.
    Extra keyword args are passed to PIL's save. `out` may be a path or a
    binary file object. Returns `out`.
    """
    is_path = isinstance(out, (str, Path))
    if is_path:
        out = Path(out)
    ext = canonical_ext(fmt)
    pil_fmt = PIL_FORMAT.get(ext)
    if pil_fmt is None:
        raise UserError(tr("Unsupported image format: {fmt}").format(fmt=fmt))
    if pil_fmt == "HEIF" and not HEIC_OK:
        raise UserError(tr("{fmt} support is not available").format(fmt="HEIC"))
    if pil_fmt == "AVIF" and not AVIF_OK:
        raise UserError(tr("{fmt} support is not available").format(fmt="AVIF"))
    q = int(quality if quality is not None else settings.get("jpeg_quality", 90))
    q = max(1, min(100, q))

    exif_b = _exif_bytes(img) if exif is _KEEP else exif
    icc_b = img.info.get("icc_profile") if icc is _KEEP else icc
    kw: dict = {}

    if pil_fmt == "JPEG":
        work = flatten(img)
        if work.mode not in ("RGB", "L", "CMYK"):
            work = work.convert("RGB")
        kw.update(quality=q, optimize=True, progressive=opts.pop("progressive", False))
    elif pil_fmt == "PNG":
        work = to_8bit(img) if img.mode in ("F",) else img
        if work.mode == "CMYK":
            work = _rgb_or_rgba(work)
        elif work.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"):
            work = _rgb_or_rgba(work)
        kw.update(optimize=optimize, compress_level=opts.pop("compress_level", 9 if optimize else 6))
    elif pil_fmt in ("WEBP", "HEIF", "AVIF"):
        work = _rgb_or_rgba(img)
        kw.update(quality=q)
        if pil_fmt == "WEBP":
            kw.update(method=opts.pop("method", 6 if optimize else 4))
    elif pil_fmt == "TIFF":
        work = to_8bit(img) if img.mode == "F" else img
        if work.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "I;16"):
            work = _rgb_or_rgba(work)
        kw.update(compression=opts.pop("compression", "tiff_lzw"))
    elif pil_fmt == "BMP":
        work = flatten(img)
        if work.mode not in ("1", "L", "P", "RGB"):
            work = work.convert("RGB")
    elif pil_fmt == "GIF":
        work = to_8bit(img)
        if work.mode not in ("1", "L", "P", "RGB", "RGBA"):
            work = _rgb_or_rgba(work)
    elif pil_fmt == "ICO":
        work, sizes = _ico_prepare(img)
        kw.update(sizes=sizes)
    elif pil_fmt == "PDF":
        work = flatten(img)
        if work.mode not in ("RGB", "L"):
            work = work.convert("RGB")
        kw.update(resolution=opts.pop("resolution", pdf_resolution(work)), quality=q)
    else:  # pragma: no cover
        work = img

    if exif_b and pil_fmt == "TIFF" and kw.get("compression", "raw") != "raw":
        exif_b = _tiff_safe_exif(exif_b)
    if exif_b and pil_fmt in EXIF_TARGETS:
        kw["exif"] = exif_b
    elif pil_fmt in EXIF_TARGETS and ("exif" in work.info or (exif is None and "xmp" in work.info)):
        # Some plugins (pillow_heif) fall back to info["exif"]; make "drop" really drop.
        if work is img:
            work = img.copy()
        work.info.pop("exif", None)
        if exif is None:
            work.info.pop("xmp", None)
            work.info.pop("XML:com.adobe.xmp", None)
    if icc_b and pil_fmt in ICC_TARGETS and pil_fmt != "PDF":
        kw["icc_profile"] = icc_b
    if "dpi" in img.info and pil_fmt in ("JPEG", "PNG", "TIFF", "BMP"):
        try:
            kw.setdefault("dpi", tuple(float(x) for x in img.info["dpi"]))
        except Exception:
            pass
    kw.update(opts)
    if is_path:
        out.parent.mkdir(parents=True, exist_ok=True)
    name = out.name if is_path else "image"
    try:
        work.save(out, pil_fmt, **kw)
    except (OSError, ValueError, TypeError, RuntimeError) as e:
        if "exif" not in kw and "icc_profile" not in kw:
            raise UserError(tr("Couldn't write {name}: {error}").format(name=name, error=e))
        # Some encoders reject a metadata blob; retry once without metadata.
        log.warning("saving %s with metadata failed (%s); retrying without", name, e)
        kw.pop("exif", None)
        kw.pop("icc_profile", None)
        if is_path:
            out.unlink(missing_ok=True)
        else:
            out.seek(0)
            out.truncate()
        try:
            work.save(out, pil_fmt, **kw)
        except (OSError, ValueError, TypeError, RuntimeError) as e2:
            raise UserError(tr("Couldn't write {name}: {error}").format(name=name, error=e2))
    return out


def encode_image(img: Image.Image, fmt: str, **kw) -> bytes:
    """Encode to bytes in memory via save_image (used for size estimation)."""
    buf = io.BytesIO()
    save_image(img, buf, fmt, **kw)
    return buf.getvalue()


# --- conversion -----------------------------------------------------------------

def _animated_to_webp(src: Path, out: Path) -> Path | None:
    """Keep animation for GIF -> WEBP. Returns None when the source isn't animated."""
    with Image.open(src) as im:
        n = getattr(im, "n_frames", 1)
        if n <= 1:
            return None
        frames, durations = [], []
        for i in range(n):
            im.seek(i)
            frames.append(im.convert("RGBA"))
            durations.append(int(im.info.get("duration", 100) or 100))
        loop = int(im.info.get("loop", 0) or 0)
    q = int(settings.get("jpeg_quality", 90))
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=durations,
                   loop=loop, quality=q, method=4)
    return out


def _convert(src: Path, ctx, target: str) -> Path:
    out = ctx.output_path(src, target)
    src_ext = canonical_ext(formats.ext(src))
    if target == "webp" and src_ext in ("gif", "webp"):
        ctx.progress(0.1, None)
        if _animated_to_webp(src, out) is not None:
            return out
    ctx.progress(0.2, None)
    img = open_image(src)
    ctx.check_cancel()
    ctx.progress(0.6, None)
    save_image(img, out, target)
    return out


def _make_run(target: str):
    def run(src: Path, ctx) -> Path:
        return _convert(Path(src), ctx, target)

    run.__name__ = f"to_{target}"
    run.__doc__ = f"Convert one image to {target.upper()}."
    return run


LABELS = {"jpg": "JPG", "png": "PNG", "webp": "WEBP", "heic": "HEIC", "tiff": "TIFF",
          "avif": "AVIF", "bmp": "BMP", "ico": "ICO", "pdf": "PDF"}


def _register() -> None:
    for i, target in enumerate(formats.IMAGE_OUT):
        if target == "heic" and not HEIC_OK:
            continue
        if target == "avif" and not AVIF_OK:
            continue
        add(Option(
            id=f"image.to.{target}",
            label=LABELS.get(target, target.upper()),
            mode="convert",
            run=_make_run(target),
            exts=frozenset(formats.IMAGE_IN),
            family="image",
            order=10 + i,
            skip_same_ext=target,
            description="Convert images to {fmt}", desc_args={"fmt": LABELS.get(target, target.upper())},
        ))


_register()
