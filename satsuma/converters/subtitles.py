"""Subtitles: SRT <-> VTT, and either to plain dialogue text. Pure Python."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import formats
from ..i18n import tr
from ..registry import Option, UserError, add

_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})"
_TIMING_RE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}(.*)$")


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]
    settings: str = ""


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _read(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse(text: str) -> list[Cue]:
    """Parse SRT or WebVTT text into cues (header, NOTE, STYLE and REGION blocks skipped)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = [ln for ln in block.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        head = lines[0].strip()
        if head.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")) and "-->" not in head:
            continue
        for i, ln in enumerate(lines[:3]):
            m = _TIMING_RE.match(ln)
            if m:
                g = m.groups()
                start, end = _secs(*g[0:4]), _secs(*g[4:8])
                body = [x.rstrip() for x in lines[i + 1:]]
                while body and not body[-1].strip():
                    body.pop()
                cues.append(Cue(start, end, body, g[8].strip()))
                break
    return cues


def _stamp(t: float, sep: str) -> str:
    ms = int(round(max(t, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


_SRT_OK_TAGS = re.compile(r"<(?!/?(?:i|b|u|font)\b)[^>]*>", re.I)
_ALL_TAGS = re.compile(r"<[^>]*>")
_ASS_TAGS = re.compile(r"\{\\[^}]*\}")
_VTT_SETTING = re.compile(r"^(align|line|position|size|vertical|region):\S+$")


def to_srt(cues: list[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        lines = [_SRT_OK_TAGS.sub("", ln) for ln in c.lines]
        out.append(f"{i}\n{_stamp(c.start, ',')} --> {_stamp(c.end, ',')}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n"


def to_vtt(cues: list[Cue]) -> str:
    out = ["WEBVTT"]
    for c in cues:
        lines = [_ASS_TAGS.sub("", re.sub(r"</?font[^>]*>", "", ln, flags=re.I)) for ln in c.lines]
        lines = [ln.replace("-->", "->") for ln in lines]
        keep = [t for t in c.settings.split() if _VTT_SETTING.match(t)]
        settings = (" " + " ".join(keep)) if keep else ""
        out.append(f"{_stamp(c.start, '.')} --> {_stamp(c.end, '.')}{settings}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n"


def to_txt(cues: list[Cue]) -> str:
    """Dialogue only: tags stripped, consecutive duplicate lines (rolling captions) removed."""
    import html

    out: list[str] = []
    for c in cues:
        for ln in c.lines:
            t = html.unescape(_ASS_TAGS.sub("", _ALL_TAGS.sub("", ln))).strip()
            if t and (not out or out[-1] != t):
                out.append(t)
    return "\n".join(out) + "\n"


WRITERS = {"srt": to_srt, "vtt": to_vtt, "txt": to_txt}


def _make(target: str):
    def run(src: Path, ctx) -> Path:
        src = Path(src)
        cues = parse(_read(src))
        if not cues:
            raise UserError(tr("No subtitles found in {name}").format(name=src.name))
        dst = ctx.output_path(src, target)
        dst.write_text(WRITERS[target](cues), encoding="utf-8")
        return dst

    run.__name__ = f"subtitle_to_{target}"
    return run


for _i, _target in enumerate(formats.SUB_OUT):
    add(Option(id=f"subtitle.to.{_target}", label=_target.upper(), mode="convert", run=_make(_target),
               exts=frozenset(formats.SUB_IN), family="subtitle", order=20 + _i, skip_same_ext=_target,
               description="Convert subtitles to {fmt}", desc_args={"fmt": _target.upper()}))
