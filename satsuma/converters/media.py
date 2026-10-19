"""Audio and video format converters (ffmpeg)."""
from __future__ import annotations

from pathlib import Path

from .. import formats, media
from ..i18n import N_, tr
from ..registry import Option, UserError, add

AUDIO_LABELS = {"mp3": "MP3", "m4a": "M4A", "wav": "WAV", "flac": "FLAC", "ogg": "OGG",
                "opus": "OPUS", "aiff": "AIFF", "wma": "WMA"}
VIDEO_LABELS = {"mp4": "MP4", "mov": "MOV", "mkv": "MKV", "webm": "WEBM", "avi": "AVI",
                "wmv": "WMV", "gif": "GIF", "mp3": "MP3"}

GIF_FPS = 15
GIF_MAX_WIDTH = 640


def convert_audio(src: Path, target: str, ctx) -> Path:
    """Re-encode an audio file to `target`, keeping tags and cover art where possible."""
    info = media.probe(src)
    if not info["has_audio"]:
        raise UserError(tr("{name} has no audio").format(name=src.name))
    out = ctx.output_path(src, target)
    media.run_ffmpeg(["-i", str(src), *media.audio_args(target, info)], ctx, info["duration"],
                     output=out)
    return out


def video_to_audio(src: Path, target: str, ctx) -> Path:
    """Extract the soundtrack of a video as `target` audio."""
    info = media.probe(src)
    if not info["has_audio"]:
        raise UserError(tr("This video has no audio"))
    out = ctx.output_path(src, target)
    media.run_ffmpeg(["-i", str(src), *media.audio_args(target, info, cover=False)], ctx,
                     info["duration"], output=out)
    return out


def convert_video(src: Path, target: str, ctx) -> Path:
    """Remux when the codecs fit the target container, else re-encode."""
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    out = ctx.output_path(src, target)
    media.run_ffmpeg(["-i", str(src), *media.av_args(target, info)], ctx, info["duration"],
                     output=out)
    return out


def video_to_gif(src: Path, ctx) -> Path:
    """Two-pass palette GIF: 15 fps, at most 640 px wide."""
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    base = f"fps={GIF_FPS},scale='min({GIF_MAX_WIDTH},iw)':-1:flags=lanczos"
    palette = ctx.tmpdir / "palette.png"
    dur = info["duration"]
    ctx.progress(0.0, tr("Building palette"))
    media.run_ffmpeg(["-i", str(src), "-vf", f"{base},palettegen=stats_mode=diff", "-frames:v", "1",
                      "-update", "1"], ctx, dur, output=palette, span=(0.0, 0.35))
    ctx.check_cancel()
    out = ctx.output_path(src, "gif")
    ctx.progress(0.35, tr("Encoding GIF"))
    media.run_ffmpeg(["-i", str(src), "-i", str(palette), "-lavfi",
                      f"{base}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                      "-loop", "0", "-an"], ctx, dur, output=out, span=(0.35, 1.0))
    return out


def _register() -> None:
    for i, target in enumerate(formats.AUDIO_OUT):
        add(Option(
            id=f"audio.to.{target}", label=AUDIO_LABELS.get(target, target.upper()), mode="convert",
            run=lambda src, ctx, t=target: convert_audio(Path(src), t, ctx),
            exts=frozenset(formats.AUDIO_IN), family="audio", order=200 + i,
            skip_same_ext=target, description=N_("Convert audio to {fmt}"), desc_args={"fmt": target.upper()},
        ))
    for i, target in enumerate(formats.VIDEO_OUT):
        if target == "gif":
            fn = lambda src, ctx: video_to_gif(Path(src), ctx)  # noqa: E731
            desc = N_("Animated GIF (15 fps, max 640 px wide)")
        elif target in media.AUDIO_ENCODE:
            fn = lambda src, ctx, t=target: video_to_audio(Path(src), t, ctx)  # noqa: E731
            desc = N_("Extract the audio as {fmt}")
        else:
            fn = lambda src, ctx, t=target: convert_video(Path(src), t, ctx)  # noqa: E731
            desc = N_("Convert video to {fmt}")
        add(Option(
            id=f"video.to.{target}", label=VIDEO_LABELS.get(target, target.upper()), mode="convert",
            run=fn, exts=frozenset(formats.VIDEO_IN), family="video", order=300 + i,
            skip_same_ext=target, description=desc, desc_args={"fmt": target.upper()},
        ))


_register()
