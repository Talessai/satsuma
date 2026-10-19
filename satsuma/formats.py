"""File-extension families. Extensions are lowercase without the dot."""
from __future__ import annotations

from pathlib import Path

IMAGE_IN = {"jpg", "jpeg", "png", "webp", "heic", "heif", "tif", "tiff", "avif", "bmp", "gif", "ico", "svg"}
IMAGE_OUT = ["jpg", "png", "webp", "heic", "tiff", "avif", "bmp", "ico", "pdf"]
RASTER_IN = IMAGE_IN - {"svg"}

AUDIO_IN = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus", "aiff", "aif", "wma"}
AUDIO_OUT = ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aiff", "wma"]

VIDEO_IN = {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "mpg", "mpeg", "3gp", "ts"}
VIDEO_OUT = ["mp4", "mov", "mkv", "webm", "avi", "wmv", "gif", "mp3"]

PDF_IN = {"pdf"}
PDF_OUT = ["docx", "pptx", "xlsx", "jpg", "png", "epub", "txt", "md", "html"]

DOC_IN = {"docx", "doc", "odt", "md", "markdown", "txt", "rtf", "rtfd", "epub", "html", "htm", "pages"}
DOC_OUT = ["docx", "pdf", "md", "txt", "epub", "html", "odt", "rtf"]

SLIDES_IN = {"ppt", "pptx", "odp"}
SLIDES_OUT = ["pdf", "jpg", "png"]

SHEET_IN = {"xlsx", "xls", "xlsm", "ods", "csv", "tsv"}
SHEET_OUT = ["xlsx", "ods", "csv", "tsv", "pdf", "html"]

SUB_IN = {"srt", "vtt"}
SUB_OUT = ["srt", "vtt", "txt"]

ARCHIVE_IN = {"zip", "tar", "gz", "tgz", "rar", "7z", "bz2", "xz"}

FAMILIES = {
    "image": IMAGE_IN,
    "audio": AUDIO_IN,
    "video": VIDEO_IN,
    "pdf": PDF_IN,
    "document": DOC_IN,
    "slides": SLIDES_IN,
    "sheet": SHEET_IN,
    "subtitle": SUB_IN,
    "archive": ARCHIVE_IN,
}

# Accent colours per family, used to tint wheel wedges.
FAMILY_COLORS = {
    "image": "#FF8A1F",
    "audio": "#E85D75",
    "video": "#9B6BFF",
    "pdf": "#E5484D",
    "document": "#3E8EF7",
    "slides": "#F5A524",
    "sheet": "#30A46C",
    "subtitle": "#12A594",
    "archive": "#8E8C99",
    "any": "#FF8A1F",
}


def ext(path: Path | str) -> str:
    """Lowercase extension without the dot; '.tar.gz' style names return 'tgz'."""
    name = Path(path).name.lower()
    if name.endswith(".tar.gz"):
        return "tgz"
    return Path(name).suffix.lstrip(".")


def family(path: Path | str) -> str | None:
    if Path(path).is_dir():
        return "folder"
    e = ext(path)
    for fam, exts in FAMILIES.items():
        if e in exts:
            return fam
    return None


def same_format(a: str, b: str) -> bool:
    """True if two extensions are the same format (jpg/jpeg, tif/tiff, ...)."""
    aliases = {"jpeg": "jpg", "tif": "tiff", "heif": "heic", "aif": "aiff", "htm": "html",
               "markdown": "md", "oga": "ogg", "m4v": "mp4", "mpeg": "mpg"}
    return aliases.get(a, a) == aliases.get(b, b)
