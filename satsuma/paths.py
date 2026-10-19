"""Output-path helpers and locations of bundled helper binaries."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from .config import settings


def unique_path(path: Path) -> Path:
    """`path` if free, else 'name (1).ext', 'name (2).ext', ..."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    if path.name.lower().endswith(".tar.gz"):
        stem, suffix = path.name[:-7], ".tar.gz"
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, default: str = "output") -> str:
    """Make a (possibly translated) name valid on Windows."""
    return _BAD_NAME_CHARS.sub("_", name).strip(" .") or default


def localized_stem(template: str, src: Path, **kw) -> str:
    """Output stem from a translated template, e.g. tr("{name}-compressed")."""
    stem = src.name if src.is_dir() else src.stem
    return safe_filename(template.format(name=stem, **kw), stem)


def output_dir_for(src: Path) -> Path:
    if settings.get("output_location") == "folder":
        folder = Path(settings.get("output_folder"))
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    return src.parent


def output_path(src: Path, ext: str | None = None, suffix: str = "", name: str | None = None,
                folder: Path | None = None) -> Path:
    """Free output path next to `src` (or in the configured output folder).

    output_path(Path("a/photo.jpg"), "png")              -> a/photo.png
    output_path(Path("a/photo.jpg"), suffix="-compressed") -> a/photo-compressed.jpg
    output_path(Path("a/photo.jpg"), "pdf", name="Combined") -> a/Combined.pdf
    """
    folder = folder or output_dir_for(src)
    stem = name if name is not None else (src.name if src.is_dir() else src.stem)
    if ext is None:
        ext = src.suffix.lstrip(".")
    filename = f"{stem}{suffix}" + (f".{ext.lstrip('.')}" if ext else "")
    return unique_path(folder / filename)


def resource_dir() -> Path:
    """Folder holding bundled resources (works from source and from PyInstaller)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "satsuma" / "resources" if base else Path(__file__).parent / "resources"


def ffmpeg_exe() -> str:
    """Path to an ffmpeg binary: bundled via imageio-ffmpeg, else one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if found:
            return found
        from .i18n import tr
        from .registry import UserError

        raise UserError(tr("ffmpeg is not available"))


def soffice_exe() -> str | None:
    """LibreOffice, if installed (optional fallback for office formats)."""
    for p in (shutil.which("soffice"),
              r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if p and Path(p).exists():
            return p
    return None
