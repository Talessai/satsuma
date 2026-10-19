"""PDF tools: compress, split, merge, rotate, extract/delete pages, protect, unlock,
watermark and page numbers. Dialogs collect options on the GUI thread; the work
itself happens in `run` on a worker thread.

Watermarks and page numbers are laid out with PyMuPDF's HTML engine (HarfBuzz
shaping, right-to-left support) using Windows fonts per script, so Arabic, Hindi
and Chinese text stamps correctly; see _Stamps."""
from __future__ import annotations

import io
import logging
import math
import re
import secrets
import shutil
import unicodedata
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from .. import formats
from ..i18n import N_, tr
from ..paths import localized_stem, safe_filename
from ..registry import Option, UserError, add
from ..ui.common import ToolDialog, fmt_size

PDF = frozenset(formats.PDF_IN)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- helpers


def _items(table: dict) -> dict:
    """{N_("Label"): value} -> {translated label: value} for ToolDialog.add_combo."""
    return {tr(k): v for k, v in table.items()}


def _ltr(edit: QLineEdit) -> QLineEdit:
    """Page ranges ('1-3, 5') read left to right even in a right-to-left UI."""
    edit.setLayoutDirection(Qt.LeftToRight)
    return edit


def open_pdf(src: Path, password: str | None = None):
    """Open a PDF for editing; UserError if it is damaged or locked."""
    import pymupdf

    name = Path(src).name
    try:
        doc = pymupdf.open(str(src))
    except Exception as e:
        raise UserError(tr("Cannot open {name}: {error}").format(name=name, error=e)) from e
    if not doc.is_pdf:
        doc.close()
        raise UserError(tr("{name} is not a PDF").format(name=name))
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            doc.close()
            if not password:
                raise UserError(tr("{name} is password protected — unlock it first").format(name=name))
            raise UserError(tr("Wrong password for {name}").format(name=name))
    return doc


def page_count(src: Path) -> int:
    try:
        import pymupdf

        with pymupdf.open(str(src)) as d:
            return 0 if d.needs_pass else d.page_count
    except Exception:
        return 0


def parse_ranges(text: str, n: int) -> list[list[int]]:
    """'1-3, 5, 8-' -> [[0,1,2],[4],[7..n-1]] (0-based). Accepts 'end'/'last' and '-3'."""
    groups: list[list[int]] = []
    text = (text or "").strip()
    if not text:
        raise UserError(tr("Enter page numbers, e.g. 1-3, 5"))
    for part in re.split(r"[,;،、，]+", text):
        part = part.strip().lower().replace(" ", "")
        if not part:
            continue
        part = part.replace("end", str(n)).replace("last", str(n))
        m = re.fullmatch(r"(\d*)[-–](\d*)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        elif part.isdigit():
            a = b = int(part)
        else:
            raise UserError(tr("'{text}' is not a page range").format(text=part))
        if a < 1 or b < 1 or a > n or b > n:
            bad = max(a, b) if max(a, b) > n else min(a, b)
            raise UserError(tr("Page {page} is out of range (1–{total})").format(page=bad, total=n))
        step = 1 if b >= a else -1
        groups.append(list(range(a - 1, b - 1 + step, step)))
    if not groups:
        raise UserError(tr("Enter page numbers, e.g. 1-3, 5"))
    return groups


def page_set(text: str, n: int) -> list[int]:
    """Blank = every page; else the sorted unique pages of `text`."""
    if not (text or "").strip():
        return list(range(n))
    return sorted({p for g in parse_ranges(text, n) for p in g})


def subset(doc, pages: list[int]):
    """New document holding `pages` of `doc` in that order (contiguous runs copied at once)."""
    import pymupdf

    out = pymupdf.open()
    i = 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.insert_pdf(doc, from_page=pages[i], to_page=pages[j])
        i = j + 1
    return out


def save(doc, dst: Path, **kw) -> Path:
    opts = dict(garbage=3, deflate=True)
    opts.update(kw)
    doc.save(str(dst), **opts)
    return dst


def _out(ctx, src: Path, template: str) -> Path:
    """Output PDF named by a translated template such as tr("{name}-compressed")."""
    return ctx.output_path(src, "pdf", name=localized_stem(template, Path(src)))


def _label(pages: list[int]) -> str:
    if len(pages) == 1:
        return tr("page {n}").format(n=pages[0] + 1)
    return tr("pages {first}-{last}").format(first=pages[0] + 1, last=pages[-1] + 1)


def _pages_hint(paths: list[Path]) -> str:
    if len(paths) == 1:
        n = page_count(paths[0])
        if not n:
            return paths[0].name
        text = tr("{name} · 1 page") if n == 1 else tr("{name} · {n} pages")
        return text.format(name=paths[0].name, n=n)
    return tr("{n} PDFs").format(n=len(paths))


# --------------------------------------------------------------------------- compress

PRESETS = {"light": (150, 80), "medium": (100, 65), "strong": (72, 50)}
PRESET_LABELS = {"light": N_("Light — images at {dpi} DPI"), "medium": N_("Medium — images at {dpi} DPI"),
                 "strong": N_("Strong — images at {dpi} DPI")}


class CompressDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Compress PDF"), _pages_hint(paths), primary=tr("Compress"))
        items = {tr(text).format(dpi=PRESETS[key][0]): key for key, text in PRESET_LABELS.items()}
        self.preset = self.add_combo(tr("Compression"), items, current="medium")

    def values(self) -> dict:
        return {"preset": self.preset.currentData()}


def compress_dialog(paths, ctx):
    dlg = CompressDialog(paths)
    return dlg.values() if dlg.exec() else None


def compress(src: Path, ctx) -> Path:
    dpi, quality = PRESETS.get(ctx.params.get("preset", "medium"), PRESETS["medium"])
    dst = _out(ctx, src, tr("{name}-compressed"))
    before = src.stat().st_size
    with open_pdf(src) as doc:
        ctx.progress(0.1, tr("Recompressing images…"))
        try:
            doc.rewrite_images(dpi_threshold=int(dpi * 1.3), dpi_target=dpi, quality=quality,
                               lossy=True, lossless=True, bitonal=False, color=True, gray=True)
        except Exception:
            pass  # keep going: structural compression still helps
        ctx.check_cancel()
        ctx.progress(0.7, tr("Optimising…"))
        tmp = ctx.tmpdir / f"c-{secrets.token_hex(4)}.pdf"
        save(doc, tmp, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
             clean=True, use_objstms=1)
    after = tmp.stat().st_size
    if after >= before:
        shutil.copyfile(src, dst)
        ctx.progress(1.0, tr("Already optimised — no smaller version possible"))
    else:
        shutil.move(str(tmp), str(dst))
        ctx.progress(1.0, tr("Saved {size} ({percent}%)").format(
            size=fmt_size(before - after), percent=(before - after) * 100 // before))
    return dst


# --------------------------------------------------------------------------- split

SPLIT_MODES = {N_("Every page"): "every", N_("Every N pages"): "n", N_("Page ranges"): "ranges"}


class SplitDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Split PDF"), _pages_hint(paths), primary=tr("Split"))
        self.mode = self.add_combo(tr("Split into"), _items(SPLIT_MODES), current="every")
        self.n = self.add_spin(tr("Pages per file"), 1, 9999, 2)
        self.ranges = _ltr(self.add_text(tr("Ranges"), "", tr("e.g. 1-3, 4, 5-9 (one file each)")))
        self.mode.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        m = self.mode.currentData()
        self.n.setEnabled(m == "n")
        self.ranges.setEnabled(m == "ranges")

    def values(self) -> dict:
        return {"mode": self.mode.currentData(), "n": self.n.value(), "ranges": self.ranges.text()}


def split_dialog(paths, ctx):
    dlg = SplitDialog(paths)
    return dlg.values() if dlg.exec() else None


def split(src: Path, ctx) -> Path | list[Path]:
    p = ctx.params
    mode = p.get("mode", "every")
    stem = src.stem
    with open_pdf(src) as doc:
        n = doc.page_count
        if mode == "ranges":
            groups = parse_ranges(p.get("ranges", ""), n)
        else:
            size = 1 if mode == "every" else max(1, int(p.get("n", 2)))
            groups = [list(range(i, min(i + size, n))) for i in range(0, n, size)]
        if len(groups) == 1:
            if groups[0] == list(range(n)):
                raise UserError(tr("Nothing to split — that is the whole document"))
            name = tr("{name} ({pages})").format(name=stem, pages=_label(groups[0]))
            with subset(doc, groups[0]) as part:
                return save(part, ctx.output_path(src, "pdf", name=safe_filename(name, stem)))
        folder = ctx.output_path(src, "", name=localized_stem(tr("{name} split"), src))
        folder.mkdir(parents=True)
        pad = max(2, len(str(n)))
        for i, g in enumerate(groups):
            ctx.check_cancel()
            ctx.progress(i / len(groups), tr("Part {n}/{total}").format(n=i + 1, total=len(groups)))
            if len(g) == 1:
                name = tr("{name} page {n}").format(name=stem, n=f"{g[0] + 1:0{pad}d}")
            else:
                name = tr("{name} pages {first}-{last}").format(name=stem, first=g[0] + 1, last=g[-1] + 1)
            target = folder / f"{safe_filename(name, stem)}.pdf"
            if target.exists():
                name = tr("{name} part {n}").format(name=stem, n=i + 1)
                target = folder / f"{safe_filename(name, stem)}.pdf"
            with subset(doc, g) as part:
                save(part, target)
    return folder


# --------------------------------------------------------------------------- merge

MERGE_EXTS = PDF | frozenset(formats.IMAGE_IN)


class MergeDialog(ToolDialog):
    """Reorderable list of the files to merge (drag to reorder)."""

    def __init__(self, paths: list[Path]):
        super().__init__(tr("Merge into one PDF"), tr("Drag to change the order."), primary=tr("Merge"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMinimumHeight(220)
        for p in paths:
            item = QListWidgetItem(Path(p).name)
            item.setData(Qt.UserRole, str(p))
            item.setToolTip(str(p))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        h.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, tip, fn in ((tr("▲ Up"), tr("Move the selected file up"), lambda: self._move(-1)),
                              (tr("▼ Down"), tr("Move the selected file down"), lambda: self._move(1)),
                              (tr("A–Z"), tr("Sort by file name"), self._sort),
                              (tr("Remove"), tr("Leave the selected file out"), self._remove)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        h.addLayout(side)
        self.body.addWidget(row)

    def _move(self, delta: int) -> None:
        r = self.list.currentRow()
        t = r + delta
        if r < 0 or not 0 <= t < self.list.count():
            return
        item = self.list.takeItem(r)
        self.list.insertItem(t, item)
        self.list.setCurrentRow(t)

    def _sort(self) -> None:
        self.list.sortItems()

    def _remove(self) -> None:
        if self.list.count() > 2:
            self.list.takeItem(self.list.currentRow())

    def values(self) -> dict:
        return {"order": [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]}


def merge_dialog(paths, ctx):
    dlg = MergeDialog(paths)
    return dlg.values() if dlg.exec() else None


def _image_pdf(path: Path):
    """An image as a one-page PDF document."""
    import pymupdf

    try:
        with pymupdf.open(str(path)) as img:
            return pymupdf.open("pdf", img.convert_to_pdf())
    except Exception:
        from PIL import Image

        try:
            import pillow_heif  # noqa: F401

            pillow_heif.register_heif_opener()
        except Exception:
            pass
        try:
            with Image.open(path) as im:
                buf = io.BytesIO()
                im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
        except Exception as e:
            raise UserError(tr("Cannot read image {name}: {error}").format(name=path.name, error=e)) from e
        with pymupdf.open("png", buf.getvalue()) as img:
            return pymupdf.open("pdf", img.convert_to_pdf())


def merge(paths: list[Path], ctx) -> Path:
    import pymupdf

    order = [Path(p) for p in ctx.params.get("order") or paths]
    if len(order) < 2:
        raise UserError(tr("Choose at least two files to merge"))
    out = pymupdf.open()
    try:
        for i, p in enumerate(order):
            ctx.check_cancel()
            ctx.progress(i / len(order), tr("Adding {name}").format(name=p.name))
            part = open_pdf(p) if formats.ext(p) == "pdf" else _image_pdf(p)
            with part:
                out.insert_pdf(part)
        dst = ctx.output_path(order[0], "pdf", name=safe_filename(tr("Merged"), "Merged"))
        return save(out, dst, garbage=4)
    finally:
        out.close()


def _has_pdf(paths: list[Path]) -> bool:
    return any(formats.ext(p) == "pdf" for p in paths)


# --------------------------------------------------------------------------- rotate / extract / delete

ANGLES = {N_("90° clockwise"): 90, N_("180°"): 180, N_("90° counter-clockwise"): 270}


class RotateDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Rotate pages"), _pages_hint(paths), primary=tr("Rotate"))
        self.angle = self.add_combo(tr("Rotate"), _items(ANGLES), current=90)
        self.pages = _ltr(self.add_text(tr("Pages"), "", tr("All pages (or e.g. 1-3, 7)")))

    def values(self) -> dict:
        return {"angle": self.angle.currentData(), "pages": self.pages.text()}


def rotate_dialog(paths, ctx):
    dlg = RotateDialog(paths)
    return dlg.values() if dlg.exec() else None


def rotate(src: Path, ctx) -> Path:
    angle = int(ctx.params.get("angle", 90)) % 360
    if angle not in (90, 180, 270):
        raise UserError(tr("Rotation must be 90, 180 or 270 degrees"))
    with open_pdf(src) as doc:
        for pno in page_set(ctx.params.get("pages", ""), doc.page_count):
            page = doc[pno]
            page.set_rotation((page.rotation + angle) % 360)
        return save(doc, _out(ctx, src, tr("{name}-rotated")))


class PagesDialog(ToolDialog):
    """Asks for a page selection. `title`, `primary` and `placeholder` are shown as given
    (pass them translated)."""

    def __init__(self, paths: list[Path], title: str, primary: str, placeholder: str):
        super().__init__(title, _pages_hint(paths), primary=primary)
        self.pages = _ltr(self.add_text(tr("Pages"), "", placeholder))

    def values(self) -> dict:
        return {"pages": self.pages.text()}


def extract_dialog(paths, ctx):
    dlg = PagesDialog(paths, tr("Extract pages"), tr("Extract"), tr("e.g. 1-3, 5"))
    return dlg.values() if dlg.exec() else None


def delete_dialog(paths, ctx):
    dlg = PagesDialog(paths, tr("Delete pages"), tr("Delete"), tr("e.g. 2, 4-6"))
    return dlg.values() if dlg.exec() else None


def extract(src: Path, ctx) -> Path:
    with open_pdf(src) as doc:
        groups = parse_ranges(ctx.params.get("pages", ""), doc.page_count)
        pages = [p for g in groups for p in g]
        with subset(doc, pages) as part:
            return save(part, _out(ctx, src, tr("{name}-extracted")), garbage=4)


def delete(src: Path, ctx) -> Path:
    with open_pdf(src) as doc:
        n = doc.page_count
        text = (ctx.params.get("pages") or "").strip()
        if not text:
            raise UserError(tr("Enter the pages to delete, e.g. 2, 4-6"))
        drop = {p for g in parse_ranges(text, n) for p in g}
        if len(drop) >= n:
            raise UserError(tr("Cannot delete every page"))
        doc.delete_pages(sorted(drop))
        return save(doc, _out(ctx, src, tr("{name}-edited")), garbage=4)


# --------------------------------------------------------------------------- protect / unlock

PERMISSIONS = (("print", N_("Allow printing")), ("copy", N_("Allow copying text")),
               ("edit", N_("Allow editing")), ("annotate", N_("Allow comments & forms")))


class ProtectDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Protect PDF"), tr("Encrypts with AES-256. Opening the PDF will need the password."),
                         primary=tr("Protect"))
        self.pw = self.add_text(tr("Password"))
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw2 = self.add_text(tr("Confirm"))
        self.pw2.setEchoMode(QLineEdit.Password)
        self.owner = self.add_text(tr("Owner password"), "", tr("Optional — needed to lift restrictions"))
        self.owner.setEchoMode(QLineEdit.Password)
        self.checks = {}
        for key, text in PERMISSIONS:
            cb = QCheckBox(tr(text))
            cb.setChecked(key != "edit")
            self.form.addRow("", cb)
            self.checks[key] = cb

    def accept(self) -> None:
        if not self.pw.text():
            QMessageBox.warning(self, tr("Protect PDF"), tr("Enter a password."))
            return
        if self.pw.text() != self.pw2.text():
            QMessageBox.warning(self, tr("Protect PDF"), tr("The passwords do not match."))
            return
        super().accept()

    def values(self) -> dict:
        v = {"password": self.pw.text(), "owner_password": self.owner.text()}
        v.update({f"allow_{k}": cb.isChecked() for k, cb in self.checks.items()})
        return v


def protect_dialog(paths, ctx):
    dlg = ProtectDialog(paths)
    return dlg.values() if dlg.exec() else None


def protect(src: Path, ctx) -> Path:
    import pymupdf

    p = ctx.params
    pw = p.get("password") or ""
    if not pw:
        raise UserError(tr("No password given"))
    perm = pymupdf.PDF_PERM_ACCESSIBILITY
    if p.get("allow_print", True):
        perm |= pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ
    if p.get("allow_copy", True):
        perm |= pymupdf.PDF_PERM_COPY
    if p.get("allow_edit", False):
        perm |= pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ASSEMBLE
    if p.get("allow_annotate", True):
        perm |= pymupdf.PDF_PERM_ANNOTATE | pymupdf.PDF_PERM_FORM
    owner = p.get("owner_password") or secrets.token_urlsafe(24)
    with open_pdf(src) as doc:
        return save(doc, _out(ctx, src, tr("{name}-protected")), garbage=3,
                    encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=owner, permissions=perm)


class UnlockDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Unlock PDF"), _pages_hint(paths) if len(paths) > 1 else paths[0].name,
                         primary=tr("Unlock"))
        self.pw = self.add_text(tr("Password"))
        self.pw.setEchoMode(QLineEdit.Password)

    def values(self) -> dict:
        return {"password": self.pw.text()}


def _needs_password(path: Path) -> bool:
    try:
        import pymupdf

        with pymupdf.open(str(path)) as d:
            return bool(d.needs_pass)
    except Exception:
        return False


def unlock_dialog(paths, ctx):
    if not any(_needs_password(Path(p)) for p in paths):
        return {"password": ""}  # only permission restrictions: no password needed
    dlg = UnlockDialog(paths)
    return dlg.values() if dlg.exec() else None


def unlock(src: Path, ctx) -> Path:
    import pymupdf

    try:
        doc = pymupdf.open(str(src))
    except Exception as e:
        raise UserError(tr("Cannot open {name}: {error}").format(name=src.name, error=e)) from e
    with doc:
        if not doc.is_encrypted and not doc.needs_pass:
            raise UserError(tr("{name} is not password protected").format(name=src.name))
        if doc.needs_pass and not doc.authenticate(ctx.params.get("password") or ""):
            raise UserError(tr("Wrong password for {name}").format(name=src.name))
        return save(doc, _out(ctx, src, tr("{name}-unlocked")), garbage=3,
                    encryption=pymupdf.PDF_ENCRYPT_NONE)


# --------------------------------------------------------------------------- text stamps (any script)


def _text_dir(text: str) -> str:
    """'rtl' when the first strong character is right-to-left (Arabic, Hebrew), else 'ltr'."""
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            return "rtl"
        if b == "L":
            return "ltr"
    return "ltr"


class _Stamps:
    """Text labels laid out by PyMuPDF's HTML engine (fitz.Story: HarfBuzz shaping for
    Arabic / Devanagari, bidi for right-to-left text, Windows fonts per script via
    converters.documents.font_css) in a small helper PDF, then shown on target pages
    with show_pdf_page - which, unlike insert_htmlbox, rotates by any angle.

    Each label gets a row of a stamp page; `boxes[i]` is (stamp page, ink box) of label i.
    Fonts are shared by all rows and subset, so they are embedded in the target only once.
    """

    def __init__(self, labels: list[str], size: float, color=(0, 0, 0), opacity: float = 1.0):
        import pymupdf

        from ..converters.documents import font_archive, font_css, script_html, scripts_in

        row = size * 2.0  # fixed row pitch (line-height), so each row's text is found by its y
        per_page = max(1, int(14000 // row))
        width = min(14400.0, max(len(t) for t in labels) * size * 1.5 + size * 4)
        scripts: set[str] = set()
        for t in dict.fromkeys(re.sub(r"\d", "0", t) for t in labels):
            scripts |= scripts_in(t)
        rgb = "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in color)
        css = (font_css(scripts) + f"\nbody {{ margin: 0; padding: 0; }}\n"
               f"p {{ margin: 0; padding: 0; white-space: nowrap; font-size: {size:g}pt; "
               f"line-height: {row:g}pt; color: {rgb}; }}")
        archive = font_archive()
        chunks = [labels[i:i + per_page] for i in range(0, len(labels), per_page)]
        buf = io.BytesIO()
        writer = pymupdf.DocumentWriter(buf)
        for chunk in chunks:
            html = "".join(f'<p dir="{_text_dir(t)}">{script_html(t)}</p>' for t in chunk)
            story = pymupdf.Story(html=html, user_css=css, archive=archive)
            rect = pymupdf.Rect(0, 0, width, row * len(chunk))
            dev = writer.begin_page(rect)
            story.place(rect)
            story.draw(dev)
            writer.end_page()
        writer.close()
        doc = pymupdf.open("pdf", buf.getvalue())
        try:
            doc.subset_fonts()
            doc = pymupdf.open("pdf", doc.tobytes(garbage=3, deflate=True))
        except Exception:
            log.warning("font subsetting failed for stamp text", exc_info=True)
        self.doc = doc
        self.boxes: list[tuple[int, object]] = []
        for pno, chunk in enumerate(chunks):
            found: list = [None] * len(chunk)
            for block in doc[pno].get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if not span["text"].strip():
                            continue
                        r = pymupdf.Rect(span["bbox"])
                        k = int(((r.y0 + r.y1) / 2) // row)
                        if 0 <= k < len(chunk):
                            found[k] = r if found[k] is None else found[k] | r
            self.boxes.extend((pno, r) for r in found)
        if opacity < 1:
            for page in doc:
                try:  # what page.insert_htmlbox(opacity=...) does
                    gs = page._set_opacity(CA=opacity, ca=opacity)
                    pymupdf.TOOLS._insert_contents(page, f"/{gs} gs\n".encode(), 0)
                except Exception:
                    log.warning("could not set stamp opacity", exc_info=True)

    def width(self, i: int) -> float:
        box = self.boxes[i][1]
        return box.width if box is not None else 0.0

    def show(self, page, i: int, center_vis, angle: float = 0.0) -> None:
        """Draw label i centred on a point in visual (rotated-page) coordinates, at
        `angle` degrees counter-clockwise as the reader sees the page."""
        import pymupdf

        pno, box = self.boxes[i]
        if box is None:
            return
        clip = (box + (-1, -1, 1, 1)) & self.doc[pno].rect
        rot = (page.rotation + angle) % 360  # show_pdf_page rotates in PDF (y-up) space
        a = math.radians(rot)
        w, h = clip.width, clip.height
        bw = abs(w * math.cos(a)) + abs(h * math.sin(a))
        bh = abs(w * math.sin(a)) + abs(h * math.cos(a))
        c = pymupdf.Point(center_vis) * page.derotation_matrix
        target = pymupdf.Rect(c.x - bw / 2, c.y - bh / 2, c.x + bw / 2, c.y + bh / 2)
        page.show_pdf_page(target, self.doc, pno, clip=clip, rotate=rot)

    def close(self) -> None:
        self.doc.close()


# --------------------------------------------------------------------------- watermark / page numbers

COLORS = {"Gray": (0.5, 0.5, 0.5), "Red": (0.85, 0.1, 0.1), "Blue": (0.1, 0.3, 0.85), "Black": (0, 0, 0)}
COLOR_NAMES = {"Gray": N_("Gray"), "Red": N_("Red"), "Blue": N_("Blue"), "Black": N_("Black")}


class WatermarkDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Watermark"), _pages_hint(paths), primary=tr("Add watermark"))
        self.text = self.add_text(tr("Text"), tr("CONFIDENTIAL"))
        self.opacity = self.add_slider(tr("Opacity"), 5, 100, 25, suffix="%")
        self.angle = self.add_spin(tr("Angle"), -90, 90, 45, suffix="°")
        self.size = self.add_spin(tr("Size"), 8, 300, 60, suffix=" pt")
        self.color = self.add_combo(tr("Colour"), {tr(COLOR_NAMES[k]): k for k in COLORS}, current="Gray")

    def values(self) -> dict:
        return {"text": self.text.text(), "opacity": self.opacity.value(), "angle": self.angle.value(),
                "size": self.size.value(), "color": self.color.currentData()}


def watermark_dialog(paths, ctx):
    dlg = WatermarkDialog(paths)
    return dlg.values() if dlg.exec() else None


def watermark(src: Path, ctx) -> Path:
    p = ctx.params
    text = (p.get("text") or "").strip()
    if not text:
        raise UserError(tr("Enter the watermark text"))
    opacity = max(0.01, min(1.0, float(p.get("opacity", 25)) / 100))
    color = COLORS.get(p.get("color", "Gray"), COLORS["Gray"])
    size = float(p.get("size", 60))
    angle = float(p.get("angle", 45))
    with open_pdf(src) as doc:
        stamps = _Stamps([text], size, color, opacity)
        try:
            n = doc.page_count
            for i, page in enumerate(doc):
                ctx.check_cancel()
                if i % 10 == 0:
                    ctx.progress(i / n)
                r = page.rect
                stamps.show(page, 0, (r.width / 2, r.height / 2), angle)
        finally:
            stamps.close()
        return save(doc, _out(ctx, src, tr("{name}-watermarked")))


POSITIONS = {N_("Bottom centre"): "bottom-center", N_("Bottom right"): "bottom-right",
             N_("Bottom left"): "bottom-left", N_("Top centre"): "top-center", N_("Top right"): "top-right",
             N_("Top left"): "top-left"}
# Templates stamped on the pages ({n} = page number, {total} = last page number). The
# dialog offers plain digits plus the worded formats in the UI language (and in English).
NUMBER_FORMATS = ("{n}", "{n} / {total}", N_("Page {n}"), N_("Page {n} of {total}"))
_LEGACY_FORMATS = {"1": "{n}", "Page 1": "Page {n}", "1 / N": "{n} / {total}", "Page 1 of N": "Page {n} of {total}"}


def _valid_format(fmt: str) -> bool:
    try:
        fmt.format(n=1, total=2)
    except (KeyError, IndexError, ValueError, AttributeError):
        return False
    return "{n}" in fmt


def number_format_items() -> dict[str, str]:
    """{example shown in the combo: template} - digits, then worded formats in the UI
    language, then the English ones when the UI is not English."""
    templates = [f for f in NUMBER_FORMATS if f.startswith("{")]
    templates += [tr(f) for f in NUMBER_FORMATS if not f.startswith("{")]
    templates += [f for f in NUMBER_FORMATS if not f.startswith("{")]
    items: dict[str, str] = {}
    for fmt in dict.fromkeys(templates):
        if _valid_format(fmt):
            items.setdefault(fmt.format(n=1, total="N"), fmt)
    return items


class PageNumbersDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Page numbers"), _pages_hint(paths), primary=tr("Add numbers"))
        self.position = self.add_combo(tr("Position"), _items(POSITIONS), current="bottom-center")
        self.fmt = self.add_combo(tr("Format"), number_format_items(), current="{n}")
        self.fmt.setToolTip(tr("Text stamped on each page; N is the number of the last page"))
        self.start = self.add_spin(tr("Start at"), 0, 99999, 1)
        self.size = self.add_spin(tr("Size"), 6, 48, 10, suffix=" pt")

    def values(self) -> dict:
        return {"position": self.position.currentData(), "format": self.fmt.currentData(),
                "start": self.start.value(), "size": self.size.value()}


def page_numbers_dialog(paths, ctx):
    dlg = PageNumbersDialog(paths)
    return dlg.values() if dlg.exec() else None


def page_numbers(src: Path, ctx) -> Path:
    p = ctx.params
    pos = p.get("position", "bottom-center")
    fmt = p.get("format", "{n}")
    fmt = _LEGACY_FORMATS.get(fmt, fmt)
    if not _valid_format(fmt):
        raise UserError(tr("The page number format must contain {n}"))
    start = int(p.get("start", 1))
    size = float(p.get("size", 10))
    margin = 28.0
    with open_pdf(src) as doc:
        total = doc.page_count + start - 1
        ctx.progress(0.05, tr("Laying out page numbers…"))
        stamps = _Stamps([fmt.format(n=i + start, total=total) for i in range(doc.page_count)], size)
        try:
            for i, page in enumerate(doc):
                ctx.check_cancel()
                r = page.rect
                width = stamps.width(i)
                y = margin if pos.startswith("top") else r.height - margin
                if pos.endswith("left"):
                    x = margin + width / 2
                elif pos.endswith("right"):
                    x = r.width - margin - width / 2
                else:
                    x = r.width / 2
                stamps.show(page, i, (x, y))
        finally:
            stamps.close()
        return save(doc, _out(ctx, src, tr("{name}-numbered")))


# --------------------------------------------------------------------------- registration

_TOOLS = [
    ("pdf.compress", N_("Compress"), compress, compress_dialog, N_("Shrink the PDF by recompressing images")),
    ("pdf.split", N_("Split"), split, split_dialog, N_("Split into several PDFs")),
    ("pdf.rotate", N_("Rotate"), rotate, rotate_dialog, N_("Rotate all or some pages")),
    ("pdf.extract", N_("Extract pages"), extract, extract_dialog, N_("Copy pages into a new PDF")),
    ("pdf.delete", N_("Delete pages"), delete, delete_dialog, N_("Remove pages")),
    ("pdf.protect", N_("Protect"), protect, protect_dialog, N_("Add a password (AES-256)")),
    ("pdf.unlock", N_("Unlock"), unlock, unlock_dialog, N_("Remove the password")),
    ("pdf.watermark", N_("Watermark"), watermark, watermark_dialog, N_("Stamp text across every page")),
    ("pdf.page_numbers", N_("Page numbers"), page_numbers, page_numbers_dialog, N_("Number every page")),
]

for _i, (_id, _label_, _run, _dlg, _desc) in enumerate(_TOOLS):
    add(Option(id=_id, label=_label_, mode="tools", run=_run, dialog=_dlg, exts=PDF, family="pdf",
               order=20 + _i, description=_desc))

add(Option(id="pdf.merge", label="Merge", mode="tools", run=merge, dialog=merge_dialog, exts=MERGE_EXTS,
           multi=True, min_files=2, family="pdf", order=19, accepts=_has_pdf,
           description="Combine PDFs (and images) into one PDF"))
