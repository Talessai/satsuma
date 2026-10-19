"""Presentations (PPT PPTX ODP) -> PDF / JPG / PNG via PowerPoint, else LibreOffice."""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import formats, office
from ..i18n import N_, tr
from ..paths import localized_stem
from ..registry import Option, UserError, add

SLIDE_WIDTH_PX = 1920
NEEDS = N_("Needs Microsoft PowerPoint or LibreOffice")


def _check() -> None:
    if not (office.powerpoint_available() or office.libreoffice_available()):
        raise UserError(tr(NEEDS))


def slides_to_pdf(src: Path, ctx) -> Path:
    _check()
    dst = ctx.output_path(src, "pdf")
    ctx.progress(0.1, tr("Converting with PowerPoint…") if office.powerpoint_available() else tr("Converting…"))
    try:
        return office.convert_with_office("powerpoint", src, dst, "pdf", ctx)
    except office.OfficeError as e:
        raise UserError(tr("Could not convert {name}: {error}").format(name=src.name, error=e)) from e


def slides_to_images(src: Path, ext: str, ctx) -> Path:
    """Each slide at 1920 px wide; a folder '<name> slides' when there are several."""
    _check()
    work = ctx.tmpdir / "slides"
    work.mkdir(exist_ok=True)
    images: list[Path] = []
    errors = []
    if office.powerpoint_available():
        ctx.progress(0.05, tr("Exporting slides with PowerPoint…"))
        try:
            images = office.powerpoint_export_images(src, work, src.stem, ext, SLIDE_WIDTH_PX, ctx)
        except office.OfficeError as e:
            errors.append(str(e))
    if not images and office.libreoffice_available():
        try:
            pdf = office.soffice_convert(src, work / "pdf", "pdf", ctx)
            images = _render_pdf(pdf, work, src.stem, ext, ctx)
        except office.OfficeError as e:
            errors.append(str(e))
    if not images:
        raise UserError("; ".join(errors) or tr("{name} has no slides").format(name=src.name))
    if len(images) == 1:
        out = ctx.output_path(src, ext)
        shutil.move(str(images[0]), str(out))
        return out
    folder = ctx.output_path(src, "", name=localized_stem(tr("{name} slides"), src))
    folder.mkdir(parents=True)
    for img in images:
        shutil.move(str(img), str(folder / img.name))
    return folder


def _render_pdf(pdf: Path, work: Path, stem: str, ext: str, ctx) -> list[Path]:
    import pymupdf

    from ..config import settings

    quality = int(settings.get("jpeg_quality", 90))
    outs = []
    with pymupdf.open(str(pdf)) as doc:
        n = doc.page_count
        pad = max(2, len(str(n)))
        for i, page in enumerate(doc, 1):
            ctx.check_cancel()
            zoom = SLIDE_WIDTH_PX / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            path = work / f"{stem} {i:0{pad}d}.{ext}"
            if ext == "jpg":
                pix.save(str(path), output="jpg", jpg_quality=quality)
            else:
                pix.save(str(path))
            outs.append(path)
    return outs


def _make(target: str):
    if target == "pdf":
        return slides_to_pdf

    def run(src: Path, ctx):
        return slides_to_images(Path(src), target, ctx)

    run.__name__ = f"slides_to_{target}"
    return run


for _i, _target in enumerate(formats.SLIDES_OUT):
    add(Option(id=f"slides.to.{_target}", label=_target.upper(), mode="convert", run=_make(_target),
               exts=frozenset(formats.SLIDES_IN), family="slides", order=20 + _i, skip_same_ext=_target,
               description="Convert presentations to {fmt}", desc_args={"fmt": _target.upper()}))
