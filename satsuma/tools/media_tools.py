"""Video and audio tools: compress, trim, split, merge, rotate, mute, resize, speed, frames,
normalize, fade. Dialogs (GUI thread) only gather params; ffmpeg runs in `run` on a worker."""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)

from .. import formats, media
from ..i18n import N_, tr
from ..registry import Option, UserError, add
from ..ui.common import ToolDialog, fmt_size, fmt_time, parse_time

VIDEO = frozenset(formats.VIDEO_IN - {"gif"})
AUDIO = frozenset(formats.AUDIO_IN)

# Internal keys are what dialogs return in params; the English names are only shown,
# through tr(), and never read back.
COMPRESS_PRESETS = {  # name: (crf, audio kbps)
    "high": (23, 128),
    "medium": (27, 96),
    "small": (31, 64),
}
COMPRESS_PRESET_NAMES = {"high": N_("High"), "medium": N_("Medium"), "small": N_("Small file")}
RESIZE_PRESETS = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
AUDIO_BITRATES = [320, 256, 192, 128, 96, 64]
SPLIT_MODES = {"parts": N_("Into equal parts"), "every": N_("Every…"), "at": N_("At timestamps")}
CUT_METHODS = {True: N_("Precise (re-encode)"), False: N_("Fast (cuts at keyframes, no re-encode)")}
ROTATIONS = {90: N_("90° clockwise"), 180: N_("180°"), 270: N_("90° counter-clockwise"), 0: N_("Don't rotate")}
LOUDNESS_TARGETS = {-16.0: N_("{lufs} LUFS (podcasts, general)"), -14.0: N_("{lufs} LUFS (streaming services)"),
                    -23.0: N_("{lufs} LUFS (broadcast)")}
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# =========================================================================== helpers

def _filename(text: str) -> str:
    """A translated file/folder name made safe for Windows (translations may contain ':' etc.)."""
    return _BAD_NAME_CHARS.sub("_", text).strip(" .") or "_"


def _named(src: Path, template: str, **kw) -> str:
    """Output stem from an already translated `template` with {name} = the source stem."""
    return _filename(template.format(name=src.stem, **kw))


def _height_label(height: int) -> str:
    return tr("{height}p").format(height=height)


def _lufs_label(target: float) -> str:
    return tr(LOUDNESS_TARGETS[target]).format(lufs=f"−{abs(target):g}")


def _set_unit(spin, template: str) -> None:
    """Apply a translated '{value} unit' template as the spin box prefix/suffix, so the
    unit can sit on either side of the number."""
    before, _, after = template.partition("{value}")
    spin.setPrefix(before)
    spin.setSuffix(after)


def _count_text(paths: list, template: str) -> str:
    """File name for one input, else the translated '{n} files'-style template."""
    return Path(paths[0]).name if len(paths) == 1 else template.format(n=len(paths))


def _method_items() -> dict:
    return {tr(text): value for value, text in CUT_METHODS.items()}


def _parse_times(value) -> list[float]:
    """'0:30, 1:15 2:00' or [30, 75] -> sorted seconds."""
    if isinstance(value, (list, tuple)):
        items = [float(v) if not isinstance(v, str) else parse_time(v) for v in value]
    else:
        items = []
        for tok in re.split(r"[,;\s]+", str(value or "").strip()):
            if tok:
                try:
                    items.append(parse_time(tok))
                except ValueError:
                    raise UserError(tr("Can't read the time “{time}”").format(time=tok)) from None
    return sorted(set(round(t, 3) for t in items))


def split_points(duration: float, params: dict) -> list[tuple[float, float]]:
    """(start, end) segments for the split tools."""
    mode = params.get("mode", "parts")
    if mode == "every":
        step = float(params.get("every", 60))
        if step <= 0:
            raise UserError(tr("The interval must be longer than 0 seconds"))
        cuts = []
        t = step
        while t < duration - 0.05:
            cuts.append(t)
            t += step
    elif mode == "at":
        cuts = [t for t in _parse_times(params.get("at", [])) if 0.05 < t < duration - 0.05]
    else:
        n = int(params.get("parts", 2))
        if n < 2:
            raise UserError(tr("Split into at least {n} parts").format(n=2))
        cuts = [duration * i / n for i in range(1, n)]
    if not cuts:
        raise UserError(tr("Nothing to split: no cut points fall inside the file"))
    edges = [0.0, *cuts, duration]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _cut_video(src: Path, out: Path, start: float, dur: float, info: dict, precise: bool, ctx,
               span=(0.0, 1.0)) -> None:
    args = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}"]
    if precise:
        args += media.av_args(out.suffix.lstrip("."), info, copy_video=False, copy_audio=False)
    else:
        args += ["-map", "0:v:0?", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero",
                 "-sn", "-dn", "-map_metadata", "0"]
    media.run_ffmpeg(args, ctx, dur, output=out, span=span)


def _cut_audio(src: Path, out: Path, start: float, dur: float, info: dict, ctx,
               span=(0.0, 1.0)) -> None:
    args = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
            *media.audio_args(out.suffix.lstrip("."), info)]
    media.run_ffmpeg(args, ctx, dur, output=out, span=span)


def _trim_range(params: dict, duration: float) -> tuple[float, float]:
    start = float(params.get("start", 0.0) or 0.0)
    end = params.get("end")
    end = duration if end is None else min(float(end), duration)
    if start < 0 or start >= duration:
        raise UserError(tr("The start time is past the end of the file"))
    if end - start < 0.05:
        raise UserError(tr("The end time must be after the start time"))
    return start, end


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def _info_text(info: dict, path: Path) -> str:
    bits = []
    if info.get("width"):
        bits.append(f"{info['width']}×{info['height']}")
    if info.get("duration"):
        bits.append(fmt_time(info["duration"]))
    try:
        bits.append(fmt_size(path.stat().st_size))
    except OSError:
        pass
    return " · ".join(bits)


def _probe_first(paths: list[Path]) -> dict:
    return media.probe(Path(paths[0]))


# =========================================================================== video: compress

def compress_video(src: Path, ctx) -> Path:
    p = ctx.params
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    dur = media.require_duration(info, src)
    vf = None
    limit = p.get("max_height")
    if limit and info.get("width") and info.get("height"):
        w, h = media.fit_short_side(info["width"], info["height"], int(limit))
        if (w, h) != (info["width"], info["height"]):
            vf = f"scale={w}:{h}"
    out = ctx.output_path(src, "mp4", name=_named(src, tr("{name}-compressed")))
    if p.get("mode") == "size":
        target_kbits = float(p.get("target_mb", 10)) * 8 * 1024 * 0.97  # leave room for the container
        total_kbps = target_kbits / dur
        audio_kbps = 0
        if info["has_audio"]:
            audio_kbps = 128 if total_kbps > 1200 else 96 if total_kbps > 500 else 64 if total_kbps > 200 else 32
        video_kbps = int(total_kbps - audio_kbps)
        if video_kbps < 30:
            raise UserError(tr("That target size is too small for a video this long"))
        venc = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_kbps}k",
                "-maxrate", f"{int(video_kbps * 1.5)}k", "-bufsize", f"{video_kbps * 2}k",
                "-pix_fmt", "yuv420p"]
        log = str(ctx.tmpdir / "x264pass")
        pass1 = ["-i", str(src), *media.av_args("mp4", info, copy_video=False, vfilter=vf, audio=False,
                                                video_codec=venc),
                 "-pass", "1", "-passlogfile", log, "-f", "null", "-"]
        # av_args adds container flags for mp4 that the null muxer ignores harmlessly.
        ctx.progress(0.0, tr("Analyzing (pass {n} of {total})").format(n=1, total=2))
        media.run_ffmpeg(pass1, ctx, dur, span=(0.0, 0.45))
        ctx.check_cancel()
        ctx.progress(0.45, tr("Compressing (pass {n} of {total})").format(n=2, total=2))
        pass2 = ["-i", str(src), *media.av_args("mp4", info, copy_video=False, copy_audio=False,
                                                vfilter=vf, video_codec=venc,
                                                audio_codec=["-c:a", "aac", "-b:a", f"{audio_kbps}k"]),
                 "-pass", "2", "-passlogfile", log]
        media.run_ffmpeg(pass2, ctx, dur, output=out, span=(0.45, 1.0))
    else:
        crf, akbps = COMPRESS_PRESETS.get(str(p.get("preset", "medium")).lower(), COMPRESS_PRESETS["medium"])
        venc = ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p"]
        args = ["-i", str(src), *media.av_args("mp4", info, copy_video=False, copy_audio=False, vfilter=vf,
                                               video_codec=venc,
                                               audio_codec=["-c:a", "aac", "-b:a", f"{akbps}k"])]
        media.run_ffmpeg(args, ctx, dur, output=out)
    return out


class CompressVideoDialog(ToolDialog):
    def __init__(self, paths: list[Path], info: dict | None = None):
        n = len(paths)
        sub = _info_text(info, Path(paths[0])) if (info and n == 1) else tr("{n} videos").format(n=n)
        super().__init__(tr("Compress video"), sub, primary=tr("Compress"))
        self.mode = self.add_combo(tr("Method"), {tr("Quality preset"): "quality", tr("Target size"): "size"})
        self.preset = self.add_combo(tr("Quality"), {tr(name): key for key, name in COMPRESS_PRESET_NAMES.items()},
                                     current="medium")
        size_mb = 10.0
        try:
            size_mb = max(1.0, round(Path(paths[0]).stat().st_size / 1024 / 1024 / 2, 1))
        except OSError:
            pass
        self.target = self.add_double(tr("Target size"), 0.1, 100000, size_mb, step=1, decimals=1)
        _set_unit(self.target, tr("{value} MB"))
        self.res = self.add_combo(tr("Max resolution"), {tr("Original"): None,
                                                         **{_height_label(h): h for h in (1080, 720, 480)}})
        self.mode.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        size = self.mode.currentData() == "size"
        self.form.setRowVisible(self.preset, not size)
        self.form.setRowVisible(self.target, size)

    def values(self) -> dict:
        return {"mode": self.mode.currentData(), "preset": self.preset.currentData(),
                "target_mb": self.target.value(), "max_height": self.res.currentData()}


def compress_video_dialog(paths, ctx):
    dlg = CompressVideoDialog(paths, _probe_first(paths) if len(paths) == 1 else None)
    return dlg.values() if dlg.exec() else None


# =========================================================================== trim (video + audio)

def trim_video(src: Path, ctx) -> Path:
    info = media.probe(src)
    dur = media.require_duration(info, src)
    start, end = _trim_range(ctx.params, dur)
    precise = bool(ctx.params.get("precise", True))
    ext = media.video_out_ext(src) if precise else formats.ext(src)
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-trimmed")))
    _cut_video(src, out, start, end - start, info, precise, ctx)
    return out


def trim_audio(src: Path, ctx) -> Path:
    info = media.probe(src)
    dur = media.require_duration(info, src)
    start, end = _trim_range(ctx.params, dur)
    out = ctx.output_path(src, media.audio_out_ext(src), name=_named(src, tr("{name}-trimmed")))
    _cut_audio(src, out, start, end - start, info, ctx)
    return out


class TrimDialog(ToolDialog):
    """Start/end fields with sliders and (for video) frame previews of both ends."""

    THUMB_W = 220

    def __init__(self, path: Path, info: dict, video: bool):
        super().__init__(tr("Trim video") if video else tr("Trim audio"),
                         f"{Path(path).name} · {_info_text(info, Path(path))}", primary=tr("Trim"))
        self.path = Path(path)
        self.info = info
        self.duration = float(info.get("duration") or 0.0)
        self.video = video and info.get("has_video")
        self._pending: set[str] = set()
        self._timer = QTimer(self, singleShot=True, interval=200)
        self._timer.timeout.connect(self._refresh_thumbs)

        if self.video:
            row = QHBoxLayout()
            self.thumb = {}
            # In right-to-left languages the layout puts "start" on the right, matching the
            # mirrored sliders (time still grows with the slider value).
            for key, caption in (("start", tr("Start")), ("end", tr("End"))):
                col = QVBoxLayout()
                lab = QLabel(alignment=Qt.AlignCenter)
                lab.setFixedSize(self.THUMB_W, int(self.THUMB_W * 9 / 16))
                lab.setStyleSheet("background: #111; border-radius: 6px;")
                col.addWidget(lab)
                col.addWidget(QLabel(caption, objectName="subtitle", alignment=Qt.AlignCenter, wordWrap=True))
                row.addLayout(col)
                self.thumb[key] = lab
            self.body.insertLayout(0, row)

        ms = max(1, int(self.duration * 1000))
        self.start_slider, self.start_edit = self._time_row(tr("Start"), 0, ms)
        self.end_slider, self.end_edit = self._time_row(tr("End"), ms, ms)
        self.length_label = self.add_row(tr("Length"), QLabel(wordWrap=True))
        if self.video:
            self.method = self.add_combo(tr("Method"), _method_items())
        else:
            self.method = None
        self.start_slider.valueChanged.connect(lambda v: self._slider_moved("start", v))
        self.end_slider.valueChanged.connect(lambda v: self._slider_moved("end", v))
        self.start_edit.editingFinished.connect(lambda: self._edited("start"))
        self.end_edit.editingFinished.connect(lambda: self._edited("end"))
        self._update_length()
        if self.video:
            self._pending = {"start", "end"}
            self._timer.start(0)

    def _time_row(self, label: str, value: int, ms: int):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        s = QSlider(Qt.Horizontal)
        s.setRange(0, ms)
        s.setValue(value)
        e = self._make_edit(fmt_time(value / 1000))
        h.addWidget(s, 1)
        h.addWidget(e)
        self.add_row(label, w)
        return s, e

    @staticmethod
    def _make_edit(text: str) -> QLineEdit:
        e = QLineEdit(text)
        # Times read left to right in every language; size to the content, not a fixed width.
        e.setLayoutDirection(Qt.LeftToRight)
        e.setMinimumWidth(e.fontMetrics().horizontalAdvance("00:00:00.00") + 24)
        e.setMaximumWidth(e.minimumWidth() * 2)
        return e

    def _slider_moved(self, key: str, v: int) -> None:
        edit = self.start_edit if key == "start" else self.end_edit
        edit.setText(fmt_time(v / 1000))
        self._update_length()
        if self.video:
            self._pending.add(key)
            self._timer.start()

    def _edited(self, key: str) -> None:
        edit, slider = (self.start_edit, self.start_slider) if key == "start" else (self.end_edit, self.end_slider)
        try:
            v = int(parse_time(edit.text()) * 1000)
        except ValueError:
            edit.setText(fmt_time(slider.value() / 1000))
            return
        slider.setValue(max(0, min(slider.maximum(), v)))
        edit.setText(fmt_time(slider.value() / 1000))

    def _update_length(self) -> None:
        a, b = self.start_slider.value() / 1000, self.end_slider.value() / 1000
        ok = b - a >= 0.05
        self.length_label.setText(fmt_time(b - a) if ok else tr("End must be after start"))
        self.primary_btn.setEnabled(ok)

    def _refresh_thumbs(self) -> None:
        for key in list(self._pending):
            slider = self.start_slider if key == "start" else self.end_slider
            t = slider.value() / 1000
            if key == "end":
                t = max(0.0, t - 0.1)  # last decodable frame
            img = media.thumbnail_image(self.path, t, self.THUMB_W * 2)
            lab = self.thumb[key]
            if not img.isNull():
                pm = QPixmap.fromImage(img).scaled(lab.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                lab.setPixmap(pm)
        self._pending.clear()

    def values(self) -> dict:
        return {"start": self.start_slider.value() / 1000, "end": self.end_slider.value() / 1000,
                "precise": self.method.currentData() if self.method is not None else True}


def _trim_dialog(video: bool):
    def dialog(paths, ctx):
        info = _probe_first(paths)
        media.require_duration(info, Path(paths[0]))
        dlg = TrimDialog(Path(paths[0]), info, video)
        return dlg.values() if dlg.exec() else None
    return dialog


# =========================================================================== split (video + audio)

def split_video(src: Path, ctx) -> list[Path]:
    info = media.probe(src)
    dur = media.require_duration(info, src)
    segs = split_points(dur, ctx.params)
    precise = bool(ctx.params.get("precise", True))
    ext = media.video_out_ext(src) if precise else formats.ext(src)
    outs = []
    n = len(segs)
    for i, (a, b) in enumerate(segs):
        ctx.check_cancel()
        ctx.progress(i / n, tr("Part {n} of {total}").format(n=i + 1, total=n))
        out = ctx.output_path(src, ext, name=_named(src, tr("{name} part {n}"), n=i + 1))
        _cut_video(src, out, a, b - a, info, precise, ctx, span=(i / n, (i + 1) / n))
        outs.append(out)
    return outs


def split_audio(src: Path, ctx) -> list[Path]:
    info = media.probe(src)
    dur = media.require_duration(info, src)
    segs = split_points(dur, ctx.params)
    ext = media.audio_out_ext(src)
    outs = []
    n = len(segs)
    for i, (a, b) in enumerate(segs):
        ctx.check_cancel()
        ctx.progress(i / n, tr("Part {n} of {total}").format(n=i + 1, total=n))
        out = ctx.output_path(src, ext, name=_named(src, tr("{name} part {n}"), n=i + 1))
        _cut_audio(src, out, a, b - a, info, ctx, span=(i / n, (i + 1) / n))
        outs.append(out)
    return outs


class SplitDialog(ToolDialog):
    def __init__(self, paths: list[Path], info: dict | None, video: bool):
        sub = (f"{Path(paths[0]).name} · {_info_text(info, Path(paths[0]))}" if info
               else tr("{n} files").format(n=len(paths)))
        super().__init__(tr("Split video") if video else tr("Split audio"), sub, primary=tr("Split"))
        self.mode = self.add_combo(tr("Split"), {tr(text): key for key, text in SPLIT_MODES.items()})
        self.parts = self.add_spin(tr("Parts"), 2, 1000, 2)
        self.every = self.add_double(tr("Every"), 0.5, 86400, 60, step=5, decimals=1)
        _set_unit(self.every, tr("{value} s"))
        self.at = self.add_text(tr("Timestamps"), "", tr("e.g. {example}").format(example="0:30, 1:45, 2:10"))
        self.precise = None
        if video:
            self.precise = self.add_combo(tr("Method"), _method_items())
        self.mode.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        m = self.mode.currentData()
        self.form.setRowVisible(self.parts, m == "parts")
        self.form.setRowVisible(self.every, m == "every")
        self.form.setRowVisible(self.at, m == "at")

    def values(self) -> dict:
        return {"mode": self.mode.currentData(), "parts": self.parts.value(), "every": self.every.value(),
                "at": self.at.text(), "precise": self.precise.currentData() if self.precise else True}


def _split_dialog(video: bool):
    def dialog(paths, ctx):
        info = _probe_first(paths) if len(paths) == 1 else None
        dlg = SplitDialog(paths, info, video)
        while dlg.exec():
            vals = dlg.values()
            if vals["mode"] == "at":
                try:
                    if not _parse_times(vals["at"]):
                        raise UserError(tr("Enter at least one timestamp"))
                except UserError as e:
                    dlg.at.setPlaceholderText(str(e))
                    dlg.at.clear()
                    continue
            return vals
        return None
    return dialog


# =========================================================================== merge (video + audio)

def _ordered(paths: list[Path], params: dict) -> list[Path]:
    order = params.get("order")
    if order:
        return [Path(p) for p in order]
    return [Path(p) for p in paths]


def _concat_list(paths: list[Path], folder: Path) -> Path:
    lst = folder / "concat.txt"
    lines = []
    for p in paths:
        s = Path(p).resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{s}'")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lst


def _can_concat_copy(paths: list[Path], infos: list[dict]) -> bool:
    exts = {formats.ext(p) for p in paths}
    if len(exts) != 1 or not media.video_profile(exts.pop()):
        return False
    first = infos[0]
    keys = ("vcodec", "width", "height", "pix_fmt", "has_audio", "acodec", "sample_rate", "channels")
    for inf in infos[1:]:
        if any(inf.get(k) != first.get(k) for k in keys):
            return False
        if abs((inf.get("fps") or 0) - (first.get("fps") or 0)) > 0.01:
            return False
    return True


def merge_video(paths: list[Path], ctx) -> Path:
    paths = _ordered(paths, ctx.params)
    if len(paths) < 2:
        raise UserError(tr("Pick at least two videos to merge"))
    infos = [media.probe(p) for p in paths]
    for p, inf in zip(paths, infos):
        if not inf["has_video"]:
            raise UserError(tr("{name} has no video").format(name=p.name))
        media.require_duration(inf, p)
    total = sum(inf["duration"] for inf in infos)
    if _can_concat_copy(paths, infos):
        out = ctx.output_path(paths[0], formats.ext(paths[0]), name=_filename(tr("Merged")))
        lst = _concat_list(paths, ctx.tmpdir)
        media.run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v", "-map", "0:a?",
                          "-c", "copy", *media.container_args(formats.ext(out))], ctx, total, output=out)
        return out

    ext = media.video_out_ext(paths[0])
    out = ctx.output_path(paths[0], ext, name=_filename(tr("Merged")))
    w, h = _even(infos[0]["width"] or 1280), _even(infos[0]["height"] or 720)
    fps = infos[0].get("fps") or 30
    any_audio = any(inf["has_audio"] for inf in infos)
    args: list[str] = []
    for p in paths:
        args += ["-i", str(p)]
    silent_idx = {}
    n_in = len(paths)
    if any_audio:
        for i, inf in enumerate(infos):
            if not inf["has_audio"]:
                args += ["-f", "lavfi", "-t", f"{inf['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                silent_idx[i] = n_in
                n_in += 1
    chains, labels = [], []
    for i, inf in enumerate(infos):
        chains.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:g},format=yuv420p[v{i}]")
        labels.append(f"[v{i}]")
        if any_audio:
            src = f"{silent_idx[i]}:a" if i in silent_idx else f"{i}:a:0"
            chains.append(f"[{src}]aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
            labels.append(f"[a{i}]")
    chains.append("".join(labels) + f"concat=n={len(paths)}:v=1:a={1 if any_audio else 0}"
                  + ("[v][a]" if any_audio else "[v]"))
    args += ["-filter_complex", ";".join(chains), "-map", "[v]"]
    args += media.video_encoder_args(ext)
    if any_audio:
        args += ["-map", "[a]", *media.audio_encoder_args(ext)]
    args += ["-map_metadata", "0", *media.container_args(ext)]
    media.run_ffmpeg(args, ctx, total, output=out)
    return out


def merge_audio(paths: list[Path], ctx) -> Path:
    paths = _ordered(paths, ctx.params)
    if len(paths) < 2:
        raise UserError(tr("Pick at least two audio files to merge"))
    infos = [media.probe(p) for p in paths]
    for p, inf in zip(paths, infos):
        if not inf["has_audio"]:
            raise UserError(tr("{name} has no audio").format(name=p.name))
        media.require_duration(inf, p)
    total = sum(inf["duration"] for inf in infos)
    ext = media.audio_out_ext(paths[0])
    out = ctx.output_path(paths[0], ext, name=_filename(tr("Merged")))
    rate = 48000 if ext == "opus" else (infos[0].get("sample_rate") or 44100)
    layout = "stereo" if any((inf.get("channels") or 2) >= 2 for inf in infos) else "mono"
    args: list[str] = []
    for p in paths:
        args += ["-i", str(p)]
    chains = [f"[{i}:a:0]aformat=sample_rates={rate}:channel_layouts={layout}[a{i}]" for i in range(len(paths))]
    chains.append("".join(f"[a{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=0:a=1[a]")
    enc = media.audio_args(ext, {**infos[0], "has_cover": False})
    enc = enc[enc.index("-vn") + 1:]  # drop the input-0 mapping; we map the filter output instead
    args += ["-filter_complex", ";".join(chains), "-map", "[a]", "-vn", *enc]
    media.run_ffmpeg(args, ctx, total, output=out)
    return out


class MergeDialog(ToolDialog):
    """Reorderable list (drag, or Up/Down buttons)."""

    def __init__(self, paths: list[Path], video: bool):
        super().__init__(tr("Merge videos") if video else tr("Merge audio"),
                         tr("Drag to change the order."), primary=tr("Merge"))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setMinimumHeight(180)
        for p in paths:
            it = QListWidgetItem(Path(p).name)
            it.setData(Qt.UserRole, str(p))
            self.list.addItem(it)
        self.list.setCurrentRow(0)
        self.body.addWidget(self.list)
        row = QHBoxLayout()
        for text, delta in ((tr("Move up"), -1), (tr("Move down"), 1)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, d=delta: self._move(d))
            row.addWidget(b)
        sort_btn = QPushButton(tr("Sort by name"))
        sort_btn.clicked.connect(lambda: self.list.sortItems())
        row.addWidget(sort_btn)
        row.addStretch(1)
        self.body.addLayout(row)

    def _move(self, delta: int) -> None:
        r = self.list.currentRow()
        t = r + delta
        if r < 0 or not (0 <= t < self.list.count()):
            return
        it = self.list.takeItem(r)
        self.list.insertItem(t, it)
        self.list.setCurrentRow(t)

    def values(self) -> dict:
        return {"order": [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]}


def _merge_dialog(video: bool):
    def dialog(paths, ctx):
        dlg = MergeDialog(paths, video)
        return dlg.values() if dlg.exec() else None
    return dialog


# =========================================================================== rotate / mute / resize / speed

def rotate_video(src: Path, ctx) -> Path:
    p = ctx.params
    rot = int(p.get("rotate", 90)) % 360
    filters = {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(rot, [])
    if p.get("flip_h"):
        filters.append("hflip")
    if p.get("flip_v"):
        filters.append("vflip")
    if not filters:
        raise UserError(tr("Choose a rotation or a flip"))
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    ext = media.video_out_ext(src)
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-rotated")))
    media.run_ffmpeg(["-i", str(src), *media.av_args(ext, info, copy_video=False, vfilter=",".join(filters))],
                     ctx, info["duration"], output=out)
    return out


class RotateDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Rotate video"), _count_text(paths, tr("{n} videos")), primary=tr("Rotate"))
        self.rot = self.add_combo(tr("Rotate"), {tr(text): deg for deg, text in ROTATIONS.items()})
        self.flip_h = self.add_row("", QCheckBox(tr("Flip horizontally (mirror)")))
        self.flip_v = self.add_row("", QCheckBox(tr("Flip vertically")))

    def values(self) -> dict:
        return {"rotate": self.rot.currentData(), "flip_h": self.flip_h.isChecked(),
                "flip_v": self.flip_v.isChecked()}


def rotate_dialog(paths, ctx):
    dlg = RotateDialog(paths)
    while dlg.exec():
        v = dlg.values()
        if v["rotate"] or v["flip_h"] or v["flip_v"]:
            return v
    return None


def mute_video(src: Path, ctx) -> Path:
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    out = ctx.output_path(src, formats.ext(src), name=_named(src, tr("{name}-muted")))
    media.run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-c", "copy", "-an", "-sn", "-dn",
                      "-map_metadata", "0"], ctx, info["duration"], output=out)
    return out


def resize_dims(width: int, height: int, params: dict) -> tuple[int, int]:
    preset = str(params.get("preset", "720p"))
    if preset in RESIZE_PRESETS:
        return media.fit_short_side(width, height, RESIZE_PRESETS[preset], upscale=True)
    if preset.endswith("%"):
        f = float(preset[:-1]) / 100
        return _even(width * f), _even(height * f)
    w, h = int(params.get("width") or 0), int(params.get("height") or 0)
    if w <= 0 and h <= 0:
        raise UserError(tr("Enter a width or a height"))
    if w <= 0:
        w = width * h / height
    elif h <= 0:
        h = height * w / width
    return _even(w), _even(h)


def resize_video(src: Path, ctx) -> Path:
    info = media.probe(src)
    if not info["has_video"] or not info["width"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    w, h = resize_dims(info["width"], info["height"], ctx.params)
    ext = media.video_out_ext(src)
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-resized")))
    media.run_ffmpeg(["-i", str(src), *media.av_args(ext, info, copy_video=False,
                                                     vfilter=f"scale={w}:{h}:flags=lanczos,setsar=1")],
                     ctx, info["duration"], output=out)
    return out


class ResizeDialog(ToolDialog):
    def __init__(self, paths: list[Path], info: dict | None):
        sub = (f"{Path(paths[0]).name} · {_info_text(info, Path(paths[0]))}" if info
               else tr("{n} videos").format(n=len(paths)))
        super().__init__(tr("Resize video"), sub, primary=tr("Resize"))
        items = {_height_label(h): k for k, h in RESIZE_PRESETS.items()}
        items.update({tr("{percent}%").format(percent=50): "50%", tr("Custom…"): "custom"})
        self.preset = self.add_combo(tr("Size"), items, current="720p")
        w0 = (info or {}).get("width") or 1280
        h0 = (info or {}).get("height") or 720
        self.w = self.add_spin(tr("Width"), 0, 8192, w0)
        self.h = self.add_spin(tr("Height"), 0, 8192, 0)
        for spin in (self.w, self.h):
            _set_unit(spin, tr("{value} px"))
            spin.setSpecialValueText(tr("auto"))
        self._h0 = h0
        self.preset.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        custom = self.preset.currentData() == "custom"
        self.form.setRowVisible(self.w, custom)
        self.form.setRowVisible(self.h, custom)

    def values(self) -> dict:
        return {"preset": self.preset.currentData(), "width": self.w.value(), "height": self.h.value()}


def resize_dialog(paths, ctx):
    info = _probe_first(paths) if len(paths) == 1 else None
    dlg = ResizeDialog(paths, info)
    return dlg.values() if dlg.exec() else None


def speed_video(src: Path, ctx) -> Path:
    factor = float(ctx.params.get("factor", 2.0))
    if not 0.25 <= factor <= 4.0:
        raise UserError(tr("Speed must be between {low}× and {high}×").format(low="0.25", high="4"))
    if abs(factor - 1.0) < 1e-6:
        raise UserError(tr("Speed is already 1×"))
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    ext = media.video_out_ext(src)
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-{factor}x"), factor=f"{factor:g}"))
    args = ["-i", str(src), *media.av_args(ext, info, copy_video=False, copy_audio=False,
                                           vfilter=f"setpts=PTS/{factor:g}",
                                           afilter=media.atempo_chain(factor))]
    dur = info["duration"] / factor if info["duration"] else None
    media.run_ffmpeg(args, ctx, dur, output=out)
    return out


class SpeedDialog(ToolDialog):
    PRESETS = [0.25, 0.5, 0.75, 1.25, 1.5, 2.0, 3.0, 4.0]

    def __init__(self, paths: list[Path]):
        super().__init__(tr("Change speed"), _count_text(paths, tr("{n} videos")), primary=tr("Apply"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        for f in self.PRESETS:
            b = QPushButton(tr("{factor}×").format(factor=f"{f:g}"))
            b.setStyleSheet("padding: 6px 8px;")  # compact, but sized by its text
            b.clicked.connect(lambda _=False, v=f: self.factor.setValue(v))
            h.addWidget(b)
        self.add_row(tr("Presets"), row)
        self.factor = self.add_double(tr("Speed"), 0.25, 4.0, 2.0, step=0.25)
        _set_unit(self.factor, tr("{value} ×"))

    def values(self) -> dict:
        return {"factor": self.factor.value()}


def speed_dialog(paths, ctx):
    dlg = SpeedDialog(paths)
    return dlg.values() if dlg.exec() else None


# =========================================================================== frames

def extract_frames(src: Path, ctx) -> Path:
    every = float(ctx.params.get("every", 1.0))
    fmt = str(ctx.params.get("format", "png")).lower()
    fmt = "jpg" if fmt in ("jpg", "jpeg") else "png"
    if every <= 0:
        raise UserError(tr("The interval must be longer than 0 seconds"))
    info = media.probe(src)
    if not info["has_video"]:
        raise UserError(tr("{name} has no video").format(name=src.name))
    folder = ctx.output_path(src, "", name=_named(src, tr("{name} frames")))
    folder.mkdir(parents=True)
    pattern = folder / f"{src.stem.replace('%', '%%')} %04d.{fmt}"
    args = ["-i", str(src), "-map", "0:v:0", "-vf", f"fps=1/{every:g}", "-fps_mode", "vfr"]
    if fmt == "jpg":
        args += ["-q:v", "2"]
    try:
        media.run_ffmpeg(args, ctx, info["duration"], output=pattern)
    except BaseException:
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        folder.rmdir()
        raise
    return folder


class FramesDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Extract frames"), tr("Frames are saved into a folder next to the video."),
                         primary=tr("Extract"))
        self.every = self.add_double(tr("Every"), 0.04, 3600, 1.0, step=0.5, decimals=2)
        _set_unit(self.every, tr("{value} s"))
        self.fmt = self.add_combo(tr("Format"), {"PNG": "png", "JPG": "jpg"})  # format names stay as-is

    def values(self) -> dict:
        return {"every": self.every.value(), "format": self.fmt.currentData()}


def frames_dialog(paths, ctx):
    dlg = FramesDialog(paths)
    return dlg.values() if dlg.exec() else None


# =========================================================================== audio: compress / normalize / fade

def compress_audio(src: Path, ctx) -> Path:
    kbps = int(ctx.params.get("bitrate", 128))
    info = media.probe(src)
    if not info["has_audio"]:
        raise UserError(tr("{name} has no audio").format(name=src.name))
    e = formats.ext(src)
    ext = "mp3" if (e in media.LOSSLESS_AUDIO or e not in media.AUDIO_ENCODE) else e
    if ext == "aac":
        ext = "m4a"
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-compressed")))
    media.run_ffmpeg(["-i", str(src), *media.audio_args(ext, info, bitrate=kbps)], ctx, info["duration"],
                     output=out)
    return out


class AudioCompressDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Compress audio"),
                         tr("Lossless files ({formats}) become {target}.").format(formats="WAV, FLAC, AIFF",
                                                                                   target="MP3"),
                         primary=tr("Compress"))
        self.rate = self.add_combo(tr("Bitrate"), {self._rate_label(b): b for b in AUDIO_BITRATES}, current=128)

    @staticmethod
    def _rate_label(kbps: int) -> str:
        if kbps == 64:
            return tr("{kbps} kbps (smallest)").format(kbps=kbps)
        if kbps == 128:
            return tr("{kbps} kbps (recommended)").format(kbps=kbps)
        return tr("{kbps} kbps").format(kbps=kbps)

    def values(self) -> dict:
        return {"bitrate": self.rate.currentData()}


def audio_compress_dialog(paths, ctx):
    dlg = AudioCompressDialog(paths)
    return dlg.values() if dlg.exec() else None


def measure_loudness(src: Path, ctx=None, target: float = -16.0, span=(0.0, 1.0),
                     duration: float | None = None) -> dict:
    """First loudnorm pass: returns the measured values (input_i, input_tp, ...)."""
    out = media.run_ffmpeg(["-i", str(src), "-map", "0:a:0", "-af",
                            f"loudnorm=I={target:g}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                           ctx, duration, span=span)
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", out)
    if not blocks:
        raise UserError(tr("Couldn't measure the loudness"))
    return json.loads(blocks[-1])


def normalize_audio(src: Path, ctx) -> Path:
    target = float(ctx.params.get("target", -16.0))
    info = media.probe(src)
    if not info["has_audio"]:
        raise UserError(tr("{name} has no audio").format(name=src.name))
    dur = info["duration"]
    ctx.progress(0.0, tr("Measuring loudness"))
    m = measure_loudness(src, ctx, target, span=(0.0, 0.4), duration=dur)
    try:
        vals = {k: float(m[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (KeyError, ValueError):
        raise UserError(tr("This audio is silent")) from None
    if vals["input_i"] == float("-inf") or vals["input_i"] < -70:
        raise UserError(tr("This audio is silent"))
    ctx.check_cancel()
    af = (f"loudnorm=I={target:g}:TP=-1.5:LRA=11:measured_I={vals['input_i']}:measured_TP={vals['input_tp']}"
          f":measured_LRA={vals['input_lra']}:measured_thresh={vals['input_thresh']}"
          f":offset={vals['target_offset']}:linear=true")
    ext = media.audio_out_ext(src)
    rate = 48000 if ext == "opus" else (info.get("sample_rate") or 48000)
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-normalized")))
    ctx.progress(0.4, tr("Normalizing"))
    media.run_ffmpeg(["-i", str(src), *media.audio_args(ext, info, afilter=af, sample_rate=rate)], ctx, dur,
                     output=out, span=(0.4, 1.0))
    return out


class NormalizeDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Normalize loudness"), tr("EBU R128 loudness normalization."),
                         primary=tr("Normalize"))
        self.target = self.add_combo(tr("Target"), {_lufs_label(t): t for t in LOUDNESS_TARGETS})

    def values(self) -> dict:
        return {"target": self.target.currentData()}


def normalize_dialog(paths, ctx):
    dlg = NormalizeDialog(paths)
    return dlg.values() if dlg.exec() else None


def fade_audio(src: Path, ctx) -> Path:
    fin = max(0.0, float(ctx.params.get("fade_in", 2.0)))
    fout = max(0.0, float(ctx.params.get("fade_out", 2.0)))
    if fin == 0 and fout == 0:
        raise UserError(tr("Set a fade-in or fade-out length"))
    info = media.probe(src)
    dur = media.require_duration(info, src)
    if fin + fout > dur:
        scale = dur / (fin + fout)
        fin, fout = fin * scale, fout * scale
    filters = []
    if fin > 0:
        filters.append(f"afade=t=in:st=0:d={fin:.3f}")
    if fout > 0:
        filters.append(f"afade=t=out:st={dur - fout:.3f}:d={fout:.3f}")
    ext = media.audio_out_ext(src)
    out = ctx.output_path(src, ext, name=_named(src, tr("{name}-faded")))
    media.run_ffmpeg(["-i", str(src), *media.audio_args(ext, info, afilter=",".join(filters))], ctx, dur,
                     output=out)
    return out


class FadeDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        super().__init__(tr("Fade in / out"), _count_text(paths, tr("{n} files")), primary=tr("Apply"))
        self.fin = self.add_double(tr("Fade in"), 0, 600, 2.0, step=0.5, decimals=1)
        self.fout = self.add_double(tr("Fade out"), 0, 600, 2.0, step=0.5, decimals=1)
        for spin in (self.fin, self.fout):
            _set_unit(spin, tr("{value} s"))

    def values(self) -> dict:
        return {"fade_in": self.fin.value(), "fade_out": self.fout.value()}


def fade_dialog(paths, ctx):
    dlg = FadeDialog(paths)
    return dlg.values() if dlg.exec() else None


# =========================================================================== registration

def _reg(id: str, label: str, run, dialog, exts, family: str, order: int, desc: str, **kw) -> None:
    add(Option(id=id, label=label, mode="tools", run=run, dialog=dialog, exts=exts, family=family,
               order=order, description=desc, **kw))


_reg("video.compress", N_("Compress"), compress_video, compress_video_dialog, VIDEO, "video", 300,
     N_("Make a video smaller (H.264 MP4)"))
_reg("video.trim", N_("Trim"), trim_video, _trim_dialog(True), VIDEO, "video", 301,
     N_("Cut the start and end of a video"), max_files=1)
_reg("video.split", N_("Split"), split_video, _split_dialog(True), VIDEO, "video", 302,
     N_("Split a video into parts"))
_reg("video.merge", N_("Merge"), merge_video, _merge_dialog(True), VIDEO, "video", 303,
     N_("Join videos one after another"), multi=True, min_files=2)
_reg("video.rotate", N_("Rotate"), rotate_video, rotate_dialog, VIDEO, "video", 304, N_("Rotate or flip a video"))
_reg("video.mute", N_("Mute"), mute_video, None, VIDEO, "video", 305, N_("Remove the audio track (no re-encode)"))
_reg("video.resize", N_("Resize"), resize_video, resize_dialog, VIDEO, "video", 306, N_("Change the resolution"))
_reg("video.speed", N_("Speed"), speed_video, speed_dialog, VIDEO, "video", 307, N_("Speed up or slow down"))
_reg("video.frames", N_("Frames"), extract_frames, frames_dialog, VIDEO, "video", 308,
     N_("Save frames as images into a folder"))

_reg("audio.trim", N_("Trim"), trim_audio, _trim_dialog(False), AUDIO, "audio", 320,
     N_("Cut the start and end of an audio file"), max_files=1)
_reg("audio.merge", N_("Merge"), merge_audio, _merge_dialog(False), AUDIO, "audio", 321,
     N_("Join audio files one after another"), multi=True, min_files=2)
_reg("audio.compress", N_("Compress"), compress_audio, audio_compress_dialog, AUDIO, "audio", 322,
     N_("Re-encode at a lower bitrate"))
_reg("audio.normalize", N_("Normalize"), normalize_audio, normalize_dialog, AUDIO, "audio", 323,
     N_("Even out loudness (EBU R128)"))
_reg("audio.split", N_("Split"), split_audio, _split_dialog(False), AUDIO, "audio", 324,
     N_("Split audio into parts"))
_reg("audio.fade", N_("Fade in/out"), fade_audio, fade_dialog, AUDIO, "audio", 325, N_("Fade in and out"))
