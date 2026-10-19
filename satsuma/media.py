"""Shared ffmpeg helpers for the audio/video converters and tools.

* `probe(path)`      - stream info parsed from `ffmpeg -hide_banner -i` (no ffprobe needed)
* `run_ffmpeg(...)`  - run ffmpeg with progress from its "time=" lines and tidy errors
* `thumbnail(...)`   - grab one frame as PNG bytes (`thumbnail_image` -> QImage)
* codec/container helpers shared by converters and tools
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from . import formats, proc
from .i18n import tr
from .paths import ffmpeg_exe
from .registry import UserError

# --------------------------------------------------------------------------- probing

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_BITRATE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_STREAM = re.compile(r"Stream #\d+:\d+\S*:\s*(Video|Audio|Subtitle|Data|Attachment):\s*(.*)")
_INPUT = re.compile(r"Input #0, (.+?), from ")
_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
_TIME = re.compile(r"time=\s*(-?\d+):(\d+):(\d+(?:\.\d+)?)")
_DIMS = re.compile(r"^(\d+)x(\d+)")
_LAYOUT_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5,
                    "5.1": 6, "6.1": 7, "7.1": 8}


def _split_top(text: str) -> list[str]:
    """Split on commas that are not inside (...) or [...]."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_probe(text: str) -> dict:
    """Parse the stderr of `ffmpeg -i file` into an info dict (see `probe`)."""
    info: dict = {
        "format": "", "duration": None, "bitrate": None,
        "has_video": False, "has_audio": False, "has_cover": False, "cover_codec": None,
        "width": None, "height": None, "fps": None, "vcodec": None, "pix_fmt": None,
        "rotation": 0, "acodec": None, "sample_rate": None, "channels": None,
        "audio_streams": 0, "subtitle_streams": 0,
    }
    m = _INPUT.search(text)
    if m:
        info["format"] = m.group(1)
    m = _DURATION.search(text)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    for line in text.splitlines():
        if "Duration:" in line:
            b = _BITRATE.search(line)
            if b:
                info["bitrate"] = int(b.group(1))
            break
    in_video = False
    for line in text.splitlines():
        sm = _STREAM.search(line)
        if sm:
            kind, rest = sm.groups()
            fields = _split_top(rest)
            codec = fields[0].split()[0] if fields and fields[0] else ""
            in_video = False
            if kind == "Video":
                if "(attached pic)" in line:
                    if not info["has_cover"]:
                        info["has_cover"] = True
                        info["cover_codec"] = codec
                    continue
                if info["has_video"]:
                    continue
                info["has_video"] = True
                in_video = True
                info["vcodec"] = codec
                if len(fields) > 1:
                    info["pix_fmt"] = fields[1].split("(")[0].strip() or None
                for f in fields[1:]:
                    d = _DIMS.match(f)
                    if d:
                        info["width"], info["height"] = int(d.group(1)), int(d.group(2))
                        break
                fps = re.search(r"([\d.]+)\s*fps", rest) or re.search(r"([\d.]+)\s*tbr", rest)
                if fps:
                    try:
                        info["fps"] = float(fps.group(1))
                    except ValueError:
                        pass
            elif kind == "Audio":
                info["audio_streams"] += 1
                if info["has_audio"]:
                    continue
                info["has_audio"] = True
                info["acodec"] = codec
                hz = re.search(r"(\d+)\s*Hz", rest)
                if hz:
                    info["sample_rate"] = int(hz.group(1))
                for f in fields[1:]:
                    name = f.split("(")[0].strip()
                    if name in _LAYOUT_CHANNELS:
                        info["channels"] = _LAYOUT_CHANNELS[name]
                        break
                    ch = re.match(r"(\d+)\s*channels", f)
                    if ch:
                        info["channels"] = int(ch.group(1))
                        break
            elif kind == "Subtitle":
                info["subtitle_streams"] += 1
            continue
        if in_video:
            r = _ROTATION.search(line)
            if r:
                info["rotation"] = int(round(float(r.group(1)))) % 360
    # Report display dimensions: ffmpeg auto-rotates frames, so filters see these.
    if info["rotation"] in (90, 270) and info["width"] and info["height"]:
        info["width"], info["height"] = info["height"], info["width"]
    return info


def probe(path: Path | str) -> dict:
    """Media info for `path`.

    Keys: format, duration (s | None), bitrate (kb/s), has_video, has_audio, has_cover,
    cover_codec, width, height (display size, rotation applied), fps, vcodec, pix_fmt,
    rotation, acodec, sample_rate, channels, audio_streams, subtitle_streams.
    Raises UserError if ffmpeg cannot read the file.
    """
    path = Path(path)
    out = proc.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], None, check=False)
    if "Input #0" not in out:
        detail = _clean_error(out.splitlines())  # ffmpeg's own text, shown untranslated
        if detail:
            raise UserError(tr("Can't read {name}: {error}").format(name=path.name, error=detail))
        raise UserError(tr("Can't read {name}: it is not a media file").format(name=path.name))
    info = parse_probe(out)
    if not info["has_video"] and not info["has_audio"]:
        raise UserError(tr("{name} has no audio or video").format(name=path.name))
    return info


# --------------------------------------------------------------------------- running

_NOISE = ("frame=", "size=", "video:", "Press [q]", "Conversion failed", "Exiting normally",
          "Stream mapping", "Output #", "Input #", "  ")
# Follow-up lines that only repeat that something above failed.
_GENERIC = ("Error opening output file", "Error opening input files", "Error parsing filterchain",
            "Error while filtering", "Error initializing", "Nothing was written", "Task finished with error",
            "Terminating thread", "Conversion failed")


def _clean_error(lines: list[str]) -> str:
    """Pick the last meaningful line(s) of ffmpeg output for a user-facing message."""
    useful = []
    for ln in lines:
        s = ln.strip()
        if not s or ln.startswith(_NOISE) or s.startswith(("frame=", "size=")):
            continue
        s = re.sub(r"^\[[^\]]*(@\s*[0-9a-fA-Fx]+)?\]\s*", "", s)
        if s.startswith(_GENERIC):
            continue
        useful.append(s)
    msg = "; ".join(dict.fromkeys(useful[-2:]))
    return msg[:300]


def run_ffmpeg(args: list[str], ctx=None, duration: float | None = None, *,
               output: Path | None = None, span: tuple[float, float] = (0.0, 1.0)) -> str:
    """Run `ffmpeg -hide_banner -nostdin -y <args> [output]`.

    Progress (scaled into `span`) is reported through `ctx.progress` from the "time=" lines
    when `duration` is known. A partial `output` is deleted on failure or cancel.
    """
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *[str(a) for a in args]]
    if output is not None:
        cmd.append(str(output))
    lines: list[str] = []
    lo, hi = span

    def on_line(line: str) -> None:
        lines.append(line)
        if len(lines) > 400:
            del lines[:200]
        if ctx is not None and duration and duration > 0:
            m = _TIME.search(line)
            if m:
                h, mi, s = m.groups()
                t = int(h) * 3600 + int(mi) * 60 + float(s)
                ctx.progress(lo + (hi - lo) * max(0.0, min(1.0, t / duration)))

    try:
        out = proc.run(cmd, ctx, on_line=on_line)
    except UserError as e:
        if output is not None:
            Path(output).unlink(missing_ok=True)
        if (ctx is not None and getattr(ctx, "cancelled", False)) or str(e) in ("Cancelled", tr("Cancelled")):
            raise
        detail = _clean_error(lines)  # ffmpeg's own text, shown untranslated
        if detail:
            raise UserError(tr("ffmpeg failed: {error}").format(error=detail)) from None
        raise UserError(tr("ffmpeg failed with an unknown error")) from None
    except BaseException:
        if output is not None:
            Path(output).unlink(missing_ok=True)
        raise
    if ctx is not None:
        ctx.progress(hi)
    return out


@lru_cache(maxsize=1)
def encoders() -> frozenset[str]:
    """Names of the encoders the bundled ffmpeg supports."""
    out = proc.run([ffmpeg_exe(), "-hide_banner", "-encoders"], None, check=False)
    names = set()
    for line in out.splitlines():
        m = re.match(r"\s*[VAS][\w.]{5}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return frozenset(names)


def has_encoder(name: str) -> bool:
    try:
        return name in encoders()
    except Exception:
        return False


def thumbnail(path: Path | str, t: float = 0.0, width: int = 320) -> bytes | None:
    """One frame at `t` seconds as PNG bytes (None if it can't be decoded). Fast seek."""
    try:
        exe = ffmpeg_exe()
    except UserError:
        return None
    args = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}",
            "-i", str(path), "-frames:v", "1", "-vf", f"scale={int(width)}:-2",
            "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        res = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=20,
                             creationflags=proc.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout or None


def thumbnail_image(path: Path | str, t: float = 0.0, width: int = 320):
    """`thumbnail` as a QImage (null QImage on failure)."""
    from PySide6.QtGui import QImage

    data = thumbnail(path, t, width)
    img = QImage()
    if data:
        img.loadFromData(data, "PNG")
    return img


# --------------------------------------------------------------------------- codecs

AUDIO_ENCODE: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "aac": ["-c:a", "aac", "-b:a", "256k"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    "oga": ["-c:a", "libvorbis", "-q:a", "6"],
    "opus": ["-c:a", "libopus", "-b:a", "160k"],
    "aiff": ["-c:a", "pcm_s16be"],
    "aif": ["-c:a", "pcm_s16be"],
    "wma": ["-c:a", "wmav2", "-b:a", "192k"],
}
# Containers where an embedded cover (mjpeg/png attached picture) can be kept.
COVER_EXTS = {"mp3", "m4a", "flac"}
# Encoders that only take up to two channels.
_STEREO_ONLY = {"mp3", "wma"}
LOSSLESS_AUDIO = {"wav", "flac", "aiff", "aif"}


def audio_args(ext: str, info: dict, *, afilter: str | None = None, bitrate: int | None = None,
               cover: bool = True, sample_rate: int | None = None) -> list[str]:
    """Map + encode arguments to write the first audio stream of input 0 as `ext`."""
    ext = ext.lower()
    if ext not in AUDIO_ENCODE:
        raise UserError(tr("Unsupported audio format: {fmt}").format(fmt=ext.upper()))
    if not info.get("has_audio"):
        raise UserError(tr("This file has no audio"))
    args = ["-map", "0:a:0"]
    keep_cover = (cover and ext in COVER_EXTS and info.get("has_cover")
                  and info.get("cover_codec") in ("mjpeg", "png"))
    if keep_cover:
        args += ["-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        args += ["-vn"]
    enc = list(AUDIO_ENCODE[ext])
    if bitrate:
        enc = enc[:2] + ["-b:a", f"{int(bitrate)}k"]
    args += enc
    if afilter:
        args += ["-af", afilter]
    if ext in _STEREO_ONLY and (info.get("channels") or 2) > 2:
        args += ["-ac", "2"]
    if sample_rate:
        args += ["-ar", str(int(sample_rate))]
    if ext == "mp3":
        args += ["-id3v2_version", "3"]
    args += ["-map_metadata", "0", "-sn", "-dn"]
    return args


def audio_out_ext(src: Path) -> str:
    """Extension to keep when re-encoding audio `src` (its own, if we can encode it)."""
    e = formats.ext(src)
    return e if e in AUDIO_ENCODE else "m4a"


# Video container profiles: encoders when re-encoding, codecs that can be stream-copied.
_EVEN = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
VIDEO_PROFILES: dict[str, dict] = {
    "mp4": {
        "v": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"],
        "a": ["-c:a", "aac", "-b:a", "192k"],
        "vcopy": {"h264", "hevc", "mpeg4", "av1"},
        "acopy": {"aac", "mp3", "ac3", "eac3", "alac", "opus"},
        "extra": ["-movflags", "+faststart"], "even": True,
    },
    "mov": {
        "v": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"],
        "a": ["-c:a", "aac", "-b:a", "192k"],
        "vcopy": {"h264", "hevc", "mpeg4", "prores", "mjpeg"},
        "acopy": {"aac", "mp3", "ac3", "alac", "pcm_s16le", "pcm_s16be", "pcm_s24le"},
        "extra": ["-movflags", "+faststart"], "even": True,
    },
    "mkv": {
        "v": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"],
        "a": ["-c:a", "aac", "-b:a", "192k"],
        "vcopy": {"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video", "mpeg1video",
                  "theora", "mjpeg", "prores"},
        "acopy": {"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "dts", "alac",
                  "pcm_s16le", "pcm_s24le", "mp2", "truehd"},
        "extra": [], "even": True,
    },
    "webm": {
        "v": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-deadline", "good",
              "-cpu-used", "4", "-row-mt", "1", "-pix_fmt", "yuv420p"],
        "a": ["-c:a", "libopus", "-b:a", "128k"],
        "vcopy": {"vp8", "vp9", "av1"},
        "acopy": {"opus", "vorbis"},
        "extra": [], "even": True,
    },
    "avi": {
        "v": ["-c:v", "mpeg4", "-q:v", "3", "-tag:v", "XVID", "-pix_fmt", "yuv420p"],
        "a": ["-c:a", "libmp3lame", "-b:a", "192k"],
        "vcopy": {"mpeg4", "mjpeg", "msmpeg4v3"},
        "acopy": {"mp3", "ac3", "pcm_s16le"},
        "extra": [], "even": True,
    },
    "wmv": {
        "v": ["-c:v", "wmv2", "-q:v", "3", "-pix_fmt", "yuv420p"],
        "a": ["-c:a", "wmav2", "-b:a", "192k"],
        "vcopy": {"wmv2", "wmv1", "msmpeg4v3"},
        "acopy": {"wmav2", "wmav1"},
        "extra": [], "even": True,
    },
}
_PROFILE_ALIAS = {"m4v": "mp4"}


def video_profile(ext: str) -> dict | None:
    ext = ext.lower()
    return VIDEO_PROFILES.get(_PROFILE_ALIAS.get(ext, ext))


def video_out_ext(src: Path) -> str:
    """Extension for a re-encoded copy of `src`: its own if we have a profile, else mp4."""
    e = formats.ext(src)
    return e if video_profile(e) else "mp4"


def video_encoder_args(ext: str) -> list[str]:
    prof = video_profile(ext) or VIDEO_PROFILES["mp4"]
    v = list(prof["v"])
    if v[1] == "libx264" and not has_encoder("libx264"):  # very unlikely with the bundle
        v = ["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"]
    return v


def audio_encoder_args(ext: str) -> list[str]:
    prof = video_profile(ext) or VIDEO_PROFILES["mp4"]
    return list(prof["a"])


def container_args(ext: str) -> list[str]:
    prof = video_profile(ext) or VIDEO_PROFILES["mp4"]
    return list(prof["extra"])


def av_args(ext: str, info: dict, *, copy_video: bool = True, copy_audio: bool = True,
            vfilter: str | None = None, afilter: str | None = None, audio: bool = True,
            video_codec: list[str] | None = None, audio_codec: list[str] | None = None) -> list[str]:
    """Map + codec arguments to write input 0 into container `ext`.

    Streams are copied when allowed, unfiltered and compatible with the container;
    otherwise re-encoded with the container's default encoders (or the given overrides).
    """
    prof = video_profile(ext) or VIDEO_PROFILES["mp4"]
    args: list[str] = []
    if info.get("has_video"):
        args += ["-map", "0:v:0"]
        if copy_video and not vfilter and video_codec is None and info.get("vcodec") in prof["vcopy"]:
            args += ["-c:v", "copy"]
        else:
            args += video_codec if video_codec is not None else video_encoder_args(ext)
            chain = [f for f in (vfilter, _EVEN if prof["even"] else None) if f]
            if chain:
                args += ["-vf", ",".join(chain)]
    else:
        args += ["-vn"]
    if audio and info.get("has_audio"):
        args += ["-map", "0:a:0"]
        if copy_audio and not afilter and audio_codec is None and info.get("acodec") in prof["acopy"]:
            args += ["-c:a", "copy"]
        else:
            args += audio_codec if audio_codec is not None else audio_encoder_args(ext)
            if afilter:
                args += ["-af", afilter]
            if (info.get("channels") or 2) > 2 and prof["a"][1] in ("libmp3lame", "wmav2"):
                args += ["-ac", "2"]
    else:
        args += ["-an"]
    args += ["-sn", "-dn", "-map_metadata", "0"] + list(prof["extra"])
    return args


def fit_short_side(width: int, height: int, limit: int, *, upscale: bool = False) -> tuple[int, int]:
    """Scale (width, height) so the shorter side is `limit` (only down unless upscale); even dims."""
    short = min(width, height)
    if short <= 0 or (short <= limit and not upscale):
        w, h = width, height
    else:
        f = limit / short
        w, h = width * f, height * f
    return max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2)


def atempo_chain(factor: float) -> str:
    """atempo filters (each 0.5..2.0) whose product is `factor`."""
    parts = []
    f = float(factor)
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    parts.append(f"atempo={f:.6g}")
    return ",".join(parts)


def require_duration(info: dict, path: Path) -> float:
    d = info.get("duration")
    if not d or d <= 0:
        raise UserError(tr("Can't tell how long {name} is").format(name=Path(path).name))
    return float(d)
