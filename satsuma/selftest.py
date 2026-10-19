"""`Satsuma.exe --selftest REPORT.json`: run one conversion per bundled engine.

Checks that a frozen build can reach ffmpeg, pandoc, PyMuPDF, the HEIC/AVIF
codecs, OCR and the face cascades. Writes {check: "ok" | error} to REPORT.json.
"""
from __future__ import annotations

import json
import tempfile
import traceback
from pathlib import Path


def run(report: Path) -> int:
    from PIL import Image, ImageDraw

    from . import registry
    from .config import settings

    registry.load_all()
    settings.set("output_location", "same")
    work = Path(tempfile.mkdtemp(prefix="satsuma-selftest-"))
    img = work / "photo.jpg"
    pic = Image.new("RGB", (640, 360), "white")
    ImageDraw.Draw(pic).text((40, 150), "HELLO SATSUMA", fill="black", font_size=48)
    pic.save(img)
    md = work / "note.md"
    md.write_text("# Title\n\nSome *text*.\n", encoding="utf-8")

    from .jobs import Job

    def convert(option_id: str, paths: list[Path], params: dict | None = None) -> str:
        opt = registry.get(option_id)
        job = Job(opt, paths)
        job.ctx.params = params or {}
        job.execute()
        if job.error:
            raise RuntimeError(job.error)
        return ", ".join(p.name for p in job.outputs)

    results: dict[str, str] = {"options": str(len(registry.all_options()))}
    for mod, err in registry.load_errors.items():
        results[f"import {mod}"] = f"FAIL: {err}"
    checks = {
        "image->png": lambda: convert("image.to.png", [img]),
        "image->heic": lambda: convert("image.to.heic", [img]),
        "image->avif": lambda: convert("image.to.avif", [img]),
        "image->pdf": lambda: convert("image.to.pdf", [img]),
        "pdf->png (pymupdf)": lambda: convert("pdf.to.png", [work / "photo.pdf"]),
        "md->docx (pandoc)": lambda: convert("doc.to.docx", [md]),
        "docx->pdf (Word/fallback)": lambda: convert("doc.to.pdf", [work / "note.docx"]),
        "video->gif (ffmpeg)": lambda: _video(work, convert),
        "ocr": lambda: _ocr(img),
        "translations": _translations,
        "faces": lambda: _faces(img),
    }
    for name, fn in checks.items():
        try:
            results[name] = "ok: " + str(fn())
        except Exception:
            results[name] = "FAIL: " + traceback.format_exc(limit=3)
    report.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(not v.startswith("FAIL") for v in results.values()) else 1


def _video(work: Path, convert) -> str:
    from . import proc
    from .paths import ffmpeg_exe

    clip = work / "clip.mp4"
    proc.run([ffmpeg_exe(), "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "1",
              "-pix_fmt", "yuv420p", str(clip)])
    return convert("video.to.gif", [clip])


def _translations() -> str:
    from . import i18n

    found = []
    try:
        for code in ("zh", "hi", "es", "ar"):
            i18n.set_language(code)
            if i18n.tr("Cancel") == "Cancel":
                raise RuntimeError(f"{code} catalog missing from the build")
            found.append(code)
    finally:
        i18n.set_language("en")
    return ", ".join(found)


def _ocr(img: Path) -> str:
    from PIL import Image

    from .tools.image_redact import detect_text, ocr_available

    if not ocr_available():
        raise RuntimeError("OCR unavailable")
    return f"{len(detect_text(Image.open(img)))} text boxes"


def _faces(img: Path) -> str:
    from PIL import Image

    from .tools.image_redact import detect_faces, faces_available

    if not faces_available():
        raise RuntimeError("face detection unavailable")
    return f"{len(detect_faces(Image.open(img)))} faces (expected 0)"
