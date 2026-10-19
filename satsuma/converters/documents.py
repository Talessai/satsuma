"""Text documents: DOCX DOC ODT MD TXT RTF RTFD EPUB HTML (and PAGES previews).

Office-native formats go through Microsoft Word (COM) or LibreOffice when either is
available; everything else (and the fallback) uses the bundled pandoc. PDFs of
plain-text formats are laid out with PyMuPDF's HTML engine (fitz.Story).
"""
from __future__ import annotations

import html as htmlmod
import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Iterable

from .. import formats, office, proc
from ..i18n import tr
from ..registry import Option, UserError, add

# --------------------------------------------------------------------------- shared helpers


def pandoc_exe() -> str:
    """Path to pandoc: bundled by pypandoc_binary, else one on PATH."""
    try:
        import pypandoc

        path = pypandoc.get_pandoc_path()
        if path:
            for cand in (Path(path), Path(str(path) + ".exe")):
                if cand.is_file():
                    return str(cand)
    except Exception:
        pass
    found = shutil.which("pandoc")
    if found:
        return found
    raise UserError(tr("pandoc is not available"))


def pandoc(args: list[str], ctx=None, cwd: Path | None = None) -> str:
    """Run pandoc with `args`; raises UserError with pandoc's message on failure."""
    return proc.run([pandoc_exe(), *[str(a) for a in args]], ctx, cwd=str(cwd) if cwd else None)


def read_text(path: Path) -> str:
    """Decode a text file: UTF-8 (with or without BOM), UTF-16 by BOM, else cp1252."""
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


BASE_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.35; }
h1 { font-size: 20pt; margin: 14pt 0 8pt 0; }
h2 { font-size: 16pt; margin: 12pt 0 6pt 0; }
h3 { font-size: 13pt; margin: 10pt 0 4pt 0; }
h4, h5, h6 { font-size: 11pt; margin: 8pt 0 4pt 0; }
p { margin: 0 0 7pt 0; }
pre, code { font-family: monospace; font-size: 9.5pt; }
pre { background-color: #f3f3f3; padding: 6pt; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 0.5pt solid #999; padding: 2pt 4pt; font-size: 9.5pt; }
th { background-color: #eee; font-weight: bold; }
blockquote { margin-left: 18pt; color: #444; }
img { max-width: 100%; }
"""


# --------------------------------------------------------------------------- fonts for every script
#
# PyMuPDF's HTML engine (fitz.Story, also behind page.insert_htmlbox) shapes Arabic and
# Devanagari with HarfBuzz and lays out right-to-left text, but it only uses the first
# CSS font family; glyphs missing from it fall back to MuPDF's built-in Noto fonts.
# To get consistent Windows fonts, text is split into script runs and each non-Latin
# run is wrapped in a <span> whose class picks a font made for that script.
#   latin  : Segoe UI (Latin, Greek, Cyrillic; extracts spaces as real spaces)
#   arabic : Arial (Segoe UI's Arabic ligatures extract as garbage text)
#   deva   : Nirmala UI
#   cjk    : Microsoft YaHei
# A missing font file just drops its rules, so MuPDF's built-in fallback takes over.

FONT_MOUNT = "satsuma-fonts"  # where the Windows font folder appears inside the Archive
_FACES: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    # family key -> [(font-weight, font-style, candidate files in C:\Windows\Fonts)]
    "latin": [("normal", "normal", ("segoeui.ttf", "tahoma.ttf", "arial.ttf")),
              ("bold", "normal", ("segoeuib.ttf", "tahomabd.ttf", "arialbd.ttf")),
              ("normal", "italic", ("segoeuii.ttf", "ariali.ttf")),
              ("bold", "italic", ("segoeuiz.ttf", "arialbi.ttf"))],
    "arabic": [("normal", "normal", ("arial.ttf", "tahoma.ttf", "times.ttf")),
               ("bold", "normal", ("arialbd.ttf", "tahomabd.ttf", "timesbd.ttf")),
               ("normal", "italic", ("ariali.ttf",)),
               ("bold", "italic", ("arialbi.ttf",))],
    "deva": [("normal", "normal", ("Nirmala.ttf", "mangal.ttf", "aparaj.ttf")),
             ("bold", "normal", ("NirmalaB.ttf", "mangalb.ttf", "aparajb.ttf"))],
    "cjk": [("normal", "normal", ("msyh.ttc", "msyh.ttf", "simsun.ttc", "simhei.ttf")),
            ("bold", "normal", ("msyhbd.ttc", "msyhbd.ttf", "simhei.ttf"))],
}
_SCRIPT_RANGES = (
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0x0870, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("deva", ((0x0900, 0x097F), (0xA8E0, 0xA8FF), (0x1CD0, 0x1CFF))),
    ("cjk", ((0x2E80, 0x2FDF), (0x3000, 0x30FF), (0x3100, 0x312F), (0x3190, 0x31FF), (0x3200, 0x33FF),
             (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF),
             (0x20000, 0x3FFFF))),
)


def fonts_dir() -> Path | None:
    import os

    d = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows") / "Fonts"
    return d if d.is_dir() else None


def _font_file(candidates: tuple[str, ...]) -> str | None:
    d = fonts_dir()
    if d is None:
        return None
    for name in candidates:
        if (d / name).is_file():
            return name
    return None


def _script(ch: str) -> str | None:
    """'arabic' | 'deva' | 'cjk' | '' (inherits the neighbour: marks, joiners) | None (Latin etc.)."""
    import unicodedata

    cp = ord(ch)
    if cp < 0x0300:
        return None
    for name, ranges in _SCRIPT_RANGES:
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return name
    if ch in "\u200c\u200d" or 0xFE00 <= cp <= 0xFE0F or unicodedata.category(ch).startswith("M"):
        return ""
    return None


def script_runs(text: str) -> list[tuple[str | None, str]]:
    """Split text into (script, chunk) runs. Combining marks and joiners stay with the
    run before them; spaces, digits and punctuation belong to the Latin font."""
    runs: list[tuple[str | None, str]] = []
    for ch in text:
        s = _script(ch)
        if s == "":
            s = runs[-1][0] if runs else None
        if runs and runs[-1][0] == s:
            runs[-1] = (s, runs[-1][1] + ch)
        else:
            runs.append((s, ch))
    return runs


def scripts_in(text: str) -> set[str]:
    return {s for s, _ in script_runs(text) if s}


def script_html(text: str, escape: bool = True) -> str:
    """`text` as HTML with each non-Latin run in a span that selects its script's font."""
    out = []
    for s, chunk in script_runs(text):
        chunk = htmlmod.escape(chunk, quote=False) if escape else chunk
        out.append(f'<span class="sz-{s}">{chunk}</span>' if s else chunk)
    return "".join(out)


def font_css(scripts: Iterable[str] = ("arabic", "deva", "cjk")) -> str:
    """@font-face rules (Latin plus `scripts`) for the Archive from font_archive(), and the
    body / span classes that use them. Rules for missing font files are left out."""
    rules = []
    for key in ("latin", *sorted(set(scripts))):
        family = f"satsuma-{key}"
        have = False
        for weight, style, cands in _FACES.get(key, []):
            name = _font_file(cands)
            if name:
                have = True
                rules.append(f"@font-face {{ font-family: {family}; src: url({FONT_MOUNT}/{name}); "
                             f"font-weight: {weight}; font-style: {style}; }}")
        if have:
            rules.append(f"body {{ font-family: {family}; }}" if key == "latin"
                         else f".sz-{key} {{ font-family: {family}; }}")
    return "\n".join(rules)


def font_archive(archive=None):
    """A pymupdf.Archive (new, or `archive`) that can see the Windows font folder."""
    import pymupdf

    archive = archive if archive is not None else pymupdf.Archive()
    d = fonts_dir()
    if d is not None:
        archive.add(str(d), FONT_MOUNT)
    return archive


_SKIP_TEXT_IN = ("style", "script", "title", "textarea")


def localize_html(html: str) -> tuple[str, set[str]]:
    """Wrap the non-Latin text of an HTML document in script spans (see script_html).
    Returns (html, scripts found)."""
    parts = re.split(r"(<[^>]*>)", html)
    used: set[str] = set()
    skip = None
    for i, part in enumerate(parts):
        if i % 2:  # a tag
            m = re.match(r"<\s*(/?)\s*([a-zA-Z0-9]+)", part)
            if m:
                name = m.group(2).lower()
                if not m.group(1) and name in _SKIP_TEXT_IN and not part.rstrip().endswith("/>"):
                    skip = name
                elif m.group(1) and name == skip:
                    skip = None
            continue
        if skip or not part or part.isascii():
            continue
        found = scripts_in(part)
        if found:
            used |= found
            parts[i] = script_html(part, escape=False)
    return "".join(parts), used


def save_pdf_subset(pdf: bytes, dst: Path) -> Path:
    """Write PDF bytes to `dst` with embedded fonts cut down to the glyphs used (whole
    CJK / Devanagari fonts would otherwise add megabytes)."""
    import pymupdf

    dst = Path(dst)
    try:
        with pymupdf.open("pdf", pdf) as doc:
            doc.subset_fonts()
            doc.save(str(dst), garbage=3, deflate=True)
        return dst
    except Exception:
        import logging

        logging.getLogger(__name__).warning("font subsetting failed for %s", dst, exc_info=True)
    dst.write_bytes(pdf)
    return dst


def html_to_pdf(html: str, dst: Path, base_dirs: Iterable[Path] = (), *, landscape: bool = False,
                css: str = BASE_CSS, ctx=None) -> Path:
    """Lay out an HTML string onto A4 pages with PyMuPDF's Story engine (any script:
    Arabic, Devanagari and CJK text gets Windows fonts made for it)."""
    import pymupdf

    archive = pymupdf.Archive()
    for d in base_dirs:
        if d and Path(d).is_dir():
            archive.add(str(d))
    font_archive(archive)
    html, used = localize_html(html)
    css = css + "\n" + font_css(used)
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    mediabox = pymupdf.paper_rect("a4-l" if landscape else "a4")
    where = mediabox + (50, 50, -50, -50)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    more, pages = True, 0
    try:
        while more and pages < 10000:
            if ctx is not None:
                ctx.check_cancel()
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            pages += 1
    finally:
        writer.close()
    return save_pdf_subset(buf.getvalue(), dst)


def wrap_html(body: str, title: str = "", css: str = "") -> str:
    style = f"<style>{css}</style>" if css else ""
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{htmlmod.escape(title)}</title>'
            f"{style}</head><body>{body}</body></html>")


def text_to_html(text: str) -> str:
    """Plain text -> HTML paragraphs (blank lines split paragraphs, line breaks kept)."""
    paras = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip("\n"))
    out = []
    for p in paras:
        if p.strip():
            out.append("<p>" + "<br/>".join(htmlmod.escape(line) for line in p.split("\n")) + "</p>")
    return "\n".join(out)


def out_path(src: Path, ext: str, ctx, suffix: str = "") -> Path:
    """ctx.output_path that also strips the extension of bundle folders (Note.rtfd)."""
    name = Path(src.name).stem if src.is_dir() else None
    return ctx.output_path(src, ext, suffix=suffix, name=name)


def media_dir_name(md: Path) -> str:
    """Folder for images extracted next to a Markdown file (no spaces: valid link targets)."""
    return re.sub(r"\s+", "_", f"{md.stem}_media")


def _norm(ext: str) -> str:
    return {"markdown": "md", "htm": "html"}.get(ext, ext)


def rtfd_rtf(src: Path, ctx) -> Path:
    """The TXT.rtf inside an RTFD bundle (folder, or a zipped bundle)."""
    if src.is_dir():
        for p in src.iterdir():
            if p.name.lower() == "txt.rtf":
                return p
        raise UserError(tr("{name} has no TXT.rtf inside").format(name=src.name))
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            for name in z.namelist():
                if Path(name).name.lower() == "txt.rtf":
                    out = ctx.tmpdir / "TXT.rtf"
                    out.write_bytes(z.read(name))
                    return out
    raise UserError(tr("{name} is not a readable RTFD bundle").format(name=src.name))


# --------------------------------------------------------------------------- conversion

PANDOC_READERS = {"docx": "docx", "odt": "odt", "md": "markdown", "html": "html", "epub": "epub",
                  "rtf": "rtf"}
WORD_IN = {"doc", "docx", "odt", "rtf"}
WORD_OUT = {"docx", "pdf", "rtf", "odt"}


def _prepare(src: Path, ctx) -> tuple[Path, str]:
    """Normalise the input to something pandoc reads: (path, kind)."""
    kind = _norm(formats.ext(src))
    if kind == "rtfd":
        return rtfd_rtf(src, ctx), "rtf"
    if kind == "txt":
        out = ctx.tmpdir / f"{src.stem}.html"
        out.write_text(wrap_html(text_to_html(read_text(src)), src.stem), encoding="utf-8")
        return out, "html"
    if kind == "doc":
        out = ctx.tmpdir / f"{src.stem}.docx"
        ctx.progress(None, tr("Opening in Word…"))
        try:
            office.convert_with_office("word", src, out, "docx", ctx)
        except office.OfficeError as e:
            raise UserError(tr("Converting .doc files needs Microsoft Word or LibreOffice")) from e
        return out, "docx"
    return src, kind


def convert_document(src: Path, target: str, ctx) -> Path:
    """Convert one document to `target` (docx pdf md txt epub html odt rtf)."""
    kind = _norm(formats.ext(src))
    dst = out_path(src, target, ctx)

    if kind == "rtfd" and target == "rtf":
        shutil.copyfile(rtfd_rtf(src, ctx), dst)
        return dst

    # Office formats: Word / LibreOffice give the most faithful result.
    if (kind in WORD_IN or kind == "rtfd") and target in WORD_OUT:
        word_src = rtfd_rtf(src, ctx) if kind == "rtfd" else src
        if office.word_available() or office.libreoffice_available():
            ctx.progress(None, tr("Converting with Office…"))
            try:
                return office.convert_with_office("word", word_src, dst, target, ctx)
            except office.OfficeError:
                if kind == "doc":
                    raise UserError(tr("Word could not convert this .doc file"))

    ctx.check_cancel()
    path, kind = _prepare(src, ctx)
    ctx.progress(0.2)

    if target == "pdf":
        if kind == "epub":
            import pymupdf

            with pymupdf.open(str(path)) as book:
                pdf_bytes = book.convert_to_pdf()
            return save_pdf_subset(pdf_bytes, dst)  # MuPDF's CJK fallback font is large
        work = ctx.tmpdir / "pdfwork"
        work.mkdir(exist_ok=True)
        pandoc(["-f", PANDOC_READERS[kind], "-t", "html5", "--extract-media=media",
                f"--resource-path={src.parent};{path.parent}", "--wrap=none", "-o", "body.html",
                str(path.resolve())], ctx, cwd=work)
        frag = (work / "body.html").read_text(encoding="utf-8")
        ctx.progress(0.6, tr("Laying out pages…"))
        html_to_pdf(wrap_html(frag, src.stem), dst, [work, src.parent], ctx=ctx)
        return dst

    reader = PANDOC_READERS[kind]
    args = ["-f", reader, f"--resource-path={src.parent};{path.parent}"]
    cwd = ctx.tmpdir
    if target == "md":
        media = media_dir_name(dst)
        args += ["-t", "gfm", "--wrap=none", f"--extract-media={media}"]
        cwd = dst.parent
    elif target == "txt":
        args += ["-t", "plain", "--wrap=none"]
    elif target == "html":
        args += ["-t", "html5", "--standalone", "--embed-resources", "--extract-media=media",
                 "--metadata", f"pagetitle={src.stem}"]
    elif target == "epub":
        args += ["-t", "epub3", "--metadata", f"title={src.stem}"] if not _has_title(path, kind) \
            else ["-t", "epub3"]
    elif target == "rtf":
        args += ["-t", "rtf", "--standalone"]
    elif target in ("docx", "odt"):
        args += ["-t", target]
    else:
        raise UserError(tr("Cannot convert to {fmt}").format(fmt=target.upper()))
    args += ["-o", str(dst.resolve()), str(path.resolve())]
    ctx.progress(0.4, tr("Converting…"))
    pandoc(args, ctx, cwd=cwd)
    if target == "md":
        _relocate_media(dst)
    return dst


def _has_title(path: Path, kind: str) -> bool:
    if kind == "md":
        head = read_text(path)[:2000]
        return head.startswith("---") and re.search(r"^title:", head, re.M) is not None
    return kind == "epub"


def _relocate_media(md: Path) -> None:
    """Remove the empty media folder pandoc may leave when there were no images."""
    folder = md.parent / media_dir_name(md)
    if folder.is_dir() and not any(folder.rglob("*.*")):
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- PAGES previews


def _pages_preview(src: Path, ctx) -> Path:
    """Extract the embedded QuickLook preview (PDF preferred, else JPG) of a .pages file."""
    wanted = ["quicklook/preview.pdf", "preview.pdf", "quicklook/thumbnail.jpg", "preview.jpg",
              "preview-web.jpg", "quicklook/thumbnail.png", "preview-micro.jpg"]
    if src.is_dir():
        files = {p.relative_to(src).as_posix().lower(): p for p in src.rglob("*") if p.is_file()}
        for w in wanted:
            if w in files:
                return files[w]
    elif zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            names = {n.lower(): n for n in z.namelist()}
            for w in wanted:
                if w in names:
                    out = ctx.tmpdir / Path(w).name
                    out.write_bytes(z.read(names[w]))
                    return out
    raise UserError(tr("{name} has no embedded preview (re-save it in Pages with "
                       "'Include preview' or export from Pages)").format(name=src.name))


def pages_to_pdf(src: Path, ctx) -> Path:
    import pymupdf

    preview = _pages_preview(src, ctx)
    dst = out_path(src, "pdf", ctx)
    if preview.suffix.lower() == ".pdf":
        shutil.copyfile(preview, dst)
    else:
        with pymupdf.open(str(preview)) as img:
            dst.write_bytes(img.convert_to_pdf())
    return dst


def pages_to_png(src: Path, ctx) -> Path:
    import pymupdf

    from ..config import settings

    preview = _pages_preview(src, ctx)
    if preview.suffix.lower() != ".pdf":
        dst = out_path(src, "png", ctx)
        pymupdf.Pixmap(str(preview)).save(str(dst))
        return dst
    from .pdf import render_pages

    return render_pages(preview, "png", ctx, dpi=int(settings.get("image_dpi", 300)), src_for_name=src)


# --------------------------------------------------------------------------- registration

LABELS = {"docx": "DOCX", "pdf": "PDF", "md": "Markdown", "txt": "TXT", "epub": "EPUB",
          "html": "HTML", "odt": "ODT", "rtf": "RTF", "png": "PNG"}


def _bundle_ok(suffix: str) -> Callable[[list[Path]], bool]:
    def ok(paths: list[Path]) -> bool:
        return all(not p.is_dir() or p.name.lower().endswith(suffix) for p in paths)

    return ok


def _make_run(target: str):
    def run(src: Path, ctx):
        return convert_document(Path(src), target, ctx)

    run.__name__ = f"doc_to_{target}"
    return run


for _i, _target in enumerate(formats.DOC_OUT):
    add(Option(id=f"doc.to.{_target}", label=LABELS[_target], mode="convert", run=_make_run(_target),
               exts=frozenset(formats.DOC_IN - {"pages"}), allow_dirs=True, accepts=_bundle_ok(".rtfd"),
               family="document", order=20 + _i, skip_same_ext=_target,
               description="Convert documents to {fmt}", desc_args={"fmt": LABELS[_target]}))

add(Option(id="pages.to.pdf", label="PDF", mode="convert", run=pages_to_pdf, exts=frozenset({"pages"}),
           allow_dirs=True, accepts=_bundle_ok(".pages"), family="document", order=20,
           description="Export the embedded preview of a Pages document as PDF"))
add(Option(id="pages.to.png", label="PNG", mode="convert", run=pages_to_png, exts=frozenset({"pages"}),
           allow_dirs=True, accepts=_bundle_ok(".pages"), family="document", order=21,
           description="Export the embedded preview of a Pages document as PNG"))
