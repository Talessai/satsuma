"""Audio/video converters and tools, run against tiny generated samples."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from satsuma import media, registry
from satsuma.config import settings
from satsuma.paths import ffmpeg_exe
from satsuma.proc import CREATE_NO_WINDOW
from tests.helpers import FakeCtx, run_option

DUR = 3.0


def _ff(*args: str) -> None:
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                   check=True, creationflags=CREATE_NO_WINDOW)


def _vsrc(size="160x120", rate=25):
    return ["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={DUR}"]


def _asrc(freq=440, vol=1.0):
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={DUR},volume={vol}"]


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> dict[str, Path]:
    settings.set("output_location", "same")  # in memory only; outputs land next to the samples
    d = tmp_path_factory.mktemp("media")
    s = {k: d / v for k, v in {
        "mp4": "clip.mp4", "noaudio": "silent.mp4", "mkv": "clip.mkv", "small": "small.mp4",
        "wav": "tone.wav", "mp3": "tone.mp3", "quiet": "quiet.wav", "cover": "cover.mp3", "png": "c.png",
    }.items()}
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "25"]
    _ff(*_vsrc(), *_asrc(), *x264, "-c:a", "aac", "-ac", "2", "-shortest", str(s["mp4"]))
    _ff(*_vsrc(), *x264, str(s["noaudio"]))
    _ff("-i", str(s["mp4"]), "-c", "copy", str(s["mkv"]))
    _ff(*_vsrc("128x96", 30), *_asrc(660), *x264, "-c:a", "aac", "-shortest", str(s["small"]))
    _ff(*_asrc(), str(s["wav"]))
    _ff(*_asrc(), "-c:a", "libmp3lame", "-q:a", "4", str(s["mp3"]))
    _ff(*_asrc(vol=0.05), str(s["quiet"]))
    _ff("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", "-update", "1", str(s["png"]))
    _ff(*_asrc(), "-i", str(s["png"]), "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-c:v", "mjpeg",
        "-disposition:v:0", "attached_pic", "-id3v2_version", "3", "-metadata", "title=Tone", str(s["cover"]))
    return s


def approx(a, b, tol=0.3):
    return a is not None and abs(a - b) <= tol


# ------------------------------------------------------------------ probe

def test_probe(samples):
    i = media.probe(samples["mp4"])
    assert i["has_video"] and i["has_audio"]
    assert (i["width"], i["height"]) == (160, 120)
    assert i["vcodec"] == "h264" and i["acodec"] == "aac"
    assert approx(i["fps"], 25, 0.01) and approx(i["duration"], DUR, 0.1)
    assert i["channels"] == 2 and i["sample_rate"] == 44100
    assert not media.probe(samples["noaudio"])["has_audio"]
    a = media.probe(samples["wav"])
    assert a["has_audio"] and not a["has_video"] and a["channels"] == 1
    c = media.probe(samples["cover"])
    assert c["has_cover"] and not c["has_video"]


def test_probe_rejects_non_media(tmp_path):
    f = tmp_path / "notes.mp4"
    f.write_text("hello")
    with pytest.raises(registry.UserError):
        media.probe(f)


def test_parse_rotation():
    text = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':
  Duration: 00:00:05.00, start: 0.000000, bitrate: 900 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1920x1080, 800 kb/s, 29.97 fps, 29.97 tbr, 90k tbn (default)
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, 5.1, fltp, 96 kb/s (default)"""
    i = media.parse_probe(text)
    assert (i["width"], i["height"], i["rotation"]) == (1080, 1920, 270)
    assert i["pix_fmt"] == "yuv420p" and i["channels"] == 6 and i["fps"] == 29.97 and i["bitrate"] == 900


def test_helpers():
    assert media.atempo_chain(0.25) == "atempo=0.5,atempo=0.5"
    assert media.atempo_chain(0.3) == "atempo=0.5,atempo=0.6"
    assert media.atempo_chain(4) == "atempo=2.0,atempo=2"
    assert media.fit_short_side(1920, 1080, 720) == (1280, 720)
    assert media.fit_short_side(1080, 1920, 720) == (720, 1280)
    assert media.fit_short_side(640, 360, 720) == (640, 360)
    assert {"libx264", "libmp3lame", "aac", "libopus", "libvorbis", "wmav2"} <= media.encoders()
    assert media.thumbnail_image  # imported lazily below in the dialog test
    assert media.thumbnail(Path("missing-file.mp4")) is None


# ------------------------------------------------------------------ converters

@pytest.mark.parametrize("target", ["mp3", "m4a", "wav", "flac", "ogg", "opus", "aiff", "wma"])
def test_audio_converters(samples, target):
    src = samples["mp3"] if target == "wav" else samples["wav"]
    (out,) = run_option(f"audio.to.{target}", [src])
    assert out.suffix == f".{target}"
    i = media.probe(out)
    assert i["has_audio"] and not i["has_video"]
    assert approx(i["duration"], DUR, 0.15)


def test_audio_keeps_cover(samples):
    (out,) = run_option("audio.to.m4a", [samples["cover"]])
    assert media.probe(out)["has_cover"]
    (out,) = run_option("audio.to.ogg", [samples["cover"]])
    assert not media.probe(out)["has_cover"]


VIDEO_CODECS = {"mp4": "h264", "mov": "h264", "mkv": "h264", "webm": "vp9", "avi": "mpeg4", "wmv": "wmv2"}


@pytest.mark.parametrize("target", ["mp4", "mov", "mkv", "webm", "avi", "wmv"])
def test_video_converters(samples, target):
    src = samples["mkv"] if target == "mp4" else samples["mp4"]
    (out,) = run_option(f"video.to.{target}", [src])
    i = media.probe(out)
    assert out.suffix == f".{target}"
    assert i["has_video"] and i["has_audio"]
    assert i["vcodec"] == VIDEO_CODECS[target]
    assert (i["width"], i["height"]) == (160, 120)
    assert approx(i["duration"], DUR)


def test_video_to_gif(samples):
    (out,) = run_option("video.to.gif", [samples["mp4"]])
    i = media.probe(out)
    assert out.suffix == ".gif" and i["vcodec"] == "gif" and i["width"] == 160
    assert not i["has_audio"]


def test_video_to_mp3(samples):
    (out,) = run_option("video.to.mp3", [samples["mp4"]])
    i = media.probe(out)
    assert i["acodec"] == "mp3" and not i["has_video"] and approx(i["duration"], DUR)
    with pytest.raises(registry.UserError, match="no audio"):
        run_option("video.to.mp3", [samples["noaudio"]])


def test_options_hide_same_format(samples):
    registry.load_all()
    ids = {o.id for o in registry.options_for("convert", [samples["mp4"]])}
    assert "video.to.mov" in ids and "video.to.mp4" not in ids
    ids = {o.id for o in registry.options_for("convert", [samples["wav"]])}
    assert "audio.to.mp3" in ids and "audio.to.wav" not in ids


# ------------------------------------------------------------------ video tools

def test_video_compress(samples):
    (out,) = run_option("video.compress", [samples["mp4"]], {"mode": "quality", "preset": "small"})
    assert out.name == "clip-compressed.mp4" and media.probe(out)["has_audio"]
    target = 0.08
    (out,) = run_option("video.compress", [samples["mp4"]], {"mode": "size", "target_mb": target,
                                                             "max_height": 96})
    i = media.probe(out)
    assert i["height"] == 96 and i["width"] == 128
    assert out.stat().st_size <= target * 1024 * 1024 * 1.3
    with pytest.raises(registry.UserError, match="too small"):
        run_option("video.compress", [samples["mp4"]], {"mode": "size", "target_mb": 0.001})


def test_video_trim(samples):
    (out,) = run_option("video.trim", [samples["mp4"]], {"start": 0.5, "end": 2.0, "precise": True})
    assert out.name == "clip-trimmed.mp4"
    i = media.probe(out)
    assert approx(i["duration"], 1.5) and i["has_audio"]
    (fast,) = run_option("video.trim", [samples["mp4"]], {"start": 1.0, "end": 2.5, "precise": False})
    assert media.probe(fast)["has_video"]
    with pytest.raises(registry.UserError):
        run_option("video.trim", [samples["mp4"]], {"start": 2.0, "end": 1.0})


def test_video_split(samples):
    outs = run_option("video.split", [samples["mp4"]], {"mode": "parts", "parts": 3})
    assert [o.name for o in outs] == ["clip part 1.mp4", "clip part 2.mp4", "clip part 3.mp4"]
    assert all(approx(media.probe(o)["duration"], 1.0) for o in outs)
    outs = run_option("video.split", [samples["noaudio"]], {"mode": "at", "at": "0:01, 2"})
    assert len(outs) == 3
    outs = run_option("video.split", [samples["noaudio"]], {"mode": "every", "every": 2})
    assert len(outs) == 2


def test_video_merge(samples):
    out = run_option("video.merge", [samples["mp4"], samples["mp4"]])[0]  # identical -> concat copy
    assert out.name == "Merged.mp4" and approx(media.probe(out)["duration"], 2 * DUR, 0.4)
    order = [str(samples["small"]), str(samples["noaudio"]), str(samples["mp4"])]
    (out,) = run_option("video.merge", [samples["mp4"], samples["noaudio"], samples["small"]], {"order": order})
    i = media.probe(out)
    assert approx(i["duration"], 3 * DUR, 0.5) and i["has_audio"]
    assert (i["width"], i["height"]) == (128, 96)  # first clip in the chosen order sets the size


def test_video_rotate(samples):
    (out,) = run_option("video.rotate", [samples["mp4"]], {"rotate": 90})
    i = media.probe(out)
    assert (i["width"], i["height"]) == (120, 160) and i["has_audio"]
    (out,) = run_option("video.rotate", [samples["mp4"]], {"rotate": 0, "flip_h": True})
    assert (media.probe(out)["width"], media.probe(out)["height"]) == (160, 120)


def test_video_mute(samples):
    (out,) = run_option("video.mute", [samples["mp4"]])
    i = media.probe(out)
    assert out.name == "clip-muted.mp4" and i["has_video"] and not i["has_audio"]


def test_video_resize(samples):
    (out,) = run_option("video.resize", [samples["mp4"]], {"preset": "50%"})
    assert (media.probe(out)["width"], media.probe(out)["height"]) == (80, 60)
    (out,) = run_option("video.resize", [samples["mp4"]], {"preset": "custom", "width": 100, "height": 0})
    assert (media.probe(out)["width"], media.probe(out)["height"]) == (100, 76)


def test_video_speed(samples):
    (out,) = run_option("video.speed", [samples["mp4"]], {"factor": 2.0})
    i = media.probe(out)
    assert out.name == "clip-2x.mp4" and approx(i["duration"], DUR / 2) and i["has_audio"]
    (out,) = run_option("video.speed", [samples["small"]], {"factor": 0.25})
    assert approx(media.probe(out)["duration"], DUR * 4, 0.5)


def test_video_frames(samples):
    (folder,) = run_option("video.frames", [samples["mp4"]], {"every": 1.0, "format": "jpg"})
    assert folder.is_dir() and folder.name == "clip frames"
    frames = sorted(folder.glob("*.jpg"))
    assert 3 <= len(frames) <= 4


# ------------------------------------------------------------------ audio tools

def test_audio_trim(samples):
    (out,) = run_option("audio.trim", [samples["wav"]], {"start": 1.0, "end": 2.2})
    assert out.name == "tone-trimmed.wav" and approx(media.probe(out)["duration"], 1.2, 0.05)


def test_audio_merge(samples):
    out = run_option("audio.merge", [samples["mp3"], samples["wav"]])[0]
    i = media.probe(out)
    assert out.name == "Merged.mp3" and approx(i["duration"], 2 * DUR)


def test_audio_compress(samples):
    (out,) = run_option("audio.compress", [samples["wav"]], {"bitrate": 64})
    assert out.name == "tone-compressed.mp3"
    assert out.stat().st_size < samples["wav"].stat().st_size / 5


def test_audio_normalize(samples):
    (out,) = run_option("audio.normalize", [samples["quiet"]], {"target": -16})
    before = media.probe(samples["quiet"])
    assert out.name == "quiet-normalized.wav" and media.probe(out)["sample_rate"] == before["sample_rate"]
    from satsuma.tools.media_tools import measure_loudness

    loud_in = float(measure_loudness(samples["quiet"])["input_i"])
    loud_out = float(measure_loudness(out)["input_i"])
    assert abs(loud_out + 16) < abs(loud_in + 16) and abs(loud_out + 16) < 2.5


def test_audio_split(samples):
    outs = run_option("audio.split", [samples["mp3"]], {"mode": "every", "every": 1.0})
    assert len(outs) == 3 and outs[0].name == "tone part 1.mp3"


def test_audio_fade(samples):
    (out,) = run_option("audio.fade", [samples["wav"]], {"fade_in": 1, "fade_out": 1})
    assert out.name == "tone-faded.wav" and approx(media.probe(out)["duration"], DUR, 0.05)


def test_split_points():
    from satsuma.tools.media_tools import split_points

    assert split_points(10, {"mode": "parts", "parts": 2}) == [(0.0, 5.0), (5.0, 10)]
    assert len(split_points(10, {"mode": "every", "every": 3})) == 4
    assert split_points(10, {"mode": "at", "at": [4, "0:07", 20]}) == [(0.0, 4.0), (4.0, 7.0), (7.0, 10)]
    with pytest.raises(registry.UserError):
        split_points(10, {"mode": "at", "at": "12"})


def test_cancel_removes_partial_output(samples, tmp_path):
    import shutil

    from satsuma.converters.media import convert_video

    src = tmp_path / "c.mp4"
    shutil.copy(samples["mp4"], src)
    ctx = FakeCtx()
    ctx.cancelled = True
    with pytest.raises(registry.UserError, match="Cancelled"):
        convert_video(src, "webm", ctx)
    assert not list(tmp_path.glob("*.webm"))


# ------------------------------------------------------------------ dialogs

def _all_dialogs(samples):
    from satsuma.tools import media_tools as mt

    vid, aud = [samples["mp4"]], [samples["wav"]]
    vinfo, ainfo = media.probe(samples["mp4"]), media.probe(samples["wav"])
    return [
        mt.CompressVideoDialog(vid, vinfo), mt.TrimDialog(vid[0], vinfo, True), mt.TrimDialog(aud[0], ainfo, False),
        mt.SplitDialog(vid, vinfo, True), mt.SplitDialog(aud, ainfo, False),
        mt.MergeDialog([samples["mp4"], samples["small"]], True), mt.MergeDialog(aud * 2, False),
        mt.RotateDialog(vid), mt.ResizeDialog(vid, vinfo), mt.SpeedDialog(vid), mt.FramesDialog(vid),
        mt.AudioCompressDialog(aud), mt.NormalizeDialog(aud), mt.FadeDialog(aud),
        mt.RotateDialog(vid * 2), mt.SpeedDialog(vid * 2), mt.FadeDialog(aud * 2), mt.SplitDialog(vid * 2, None, True),
    ]


def test_dialogs_construct(samples):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialogs = _all_dialogs(samples)
    for d in dialogs:
        assert isinstance(d.values(), dict)
    trim = dialogs[1]
    assert trim.values() == {"start": 0.0, "end": DUR, "precise": True}
    trim._refresh_thumbs()
    assert not trim.thumb["start"].pixmap().isNull()
    trim.start_edit.setText("0:01.5")
    trim._edited("start")
    assert trim.values()["start"] == 1.5
    merge = dialogs[5]
    merge.list.setCurrentRow(1)
    merge._move(-1)
    assert merge.values()["order"] == [str(samples["small"]), str(samples["mp4"])]
    for d in dialogs:
        d.deleteLater()
    app.processEvents()


# A tiny stand-in for locales/ar.json (the real catalog may still be untranslated).
AR_CATALOG = {
    "{name} part {n}": "{name} الجزء {n}",
    "Part {n} of {total}": "الجزء {n} من {total}",
    "Merged": "مدمج",
    "Split video": "تقسيم الفيديو",
    "Trim video": "قص الفيديو",
    "Start": "البداية",
    "End": "النهاية",
    "{value} s": "{value} ث",
    "{value} MB": "{value} ميغابايت",
    "Precise (re-encode)": "دقيق (إعادة ترميز)",
    "High": "عالية",
    "{height}p": "{height}p",
    "{kbps} kbps (recommended)": "{kbps} ك.ب/ث (موصى به)",
    "This video has no audio": "لا يحتوي هذا الفيديو على صوت",
}


def test_dialogs_and_names_in_arabic(samples, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from satsuma import i18n

    app = QApplication.instance() or QApplication([])
    english = _all_dialogs(samples)
    english_values = [d.values() for d in english]
    i18n.set_language("ar")
    monkeypatch.setattr(i18n, "_catalog", dict(AR_CATALOG))
    app.setLayoutDirection(Qt.RightToLeft)
    try:
        assert i18n.is_rtl()
        dialogs = _all_dialogs(samples)
        # Translated labels never leak into params: every dialog returns the same values.
        assert [d.values() for d in dialogs] == english_values
        compress, trim, split = dialogs[0], dialogs[1], dialogs[3]
        assert split.windowTitle().startswith("تقسيم الفيديو")
        assert split.every.suffix() == " ث" and split.precise.currentText() == "دقيق (إعادة ترميز)"
        assert compress.preset.itemText(0) == "عالية" and compress.preset.itemData(0) == "high"
        assert compress.target.suffix() == " ميغابايت"
        assert compress.res.findData(1080) >= 0 and compress.res.itemText(compress.res.findData(1080)) == "1080p"
        rate = dialogs[11].rate
        assert rate.currentData() == 128 and rate.currentText() == "128 ك.ب/ث (موصى به)"
        # RTL mirrors the sliders, but time still grows with the slider value.
        assert trim.start_slider.isRightToLeft() and trim.windowTitle().startswith("قص الفيديو")
        trim.start_slider.setValue(1500)
        trim.end_slider.setValue(2500)
        assert trim.values()["start"] == 1.5 and trim.values()["end"] == 2.5
        assert trim.start_edit.text() == "0:01.50" and trim.start_edit.layoutDirection() == Qt.LeftToRight
        assert trim.primary_btn.isEnabled()
        trim._refresh_thumbs()
        assert not trim.thumb["start"].pixmap().isNull() and not trim.thumb["end"].pixmap().isNull()
        for d in dialogs:
            d.deleteLater()

        outs = run_option("video.split", [samples["mp4"]], {"mode": "parts", "parts": 2})
        assert [o.name for o in outs] == ["clip الجزء 1.mp4", "clip الجزء 2.mp4"]
        (merged,) = run_option("audio.merge", [samples["mp3"], samples["wav"]])
        assert merged.name.startswith("مدمج") and merged.suffix == ".mp3"
        with pytest.raises(registry.UserError, match="لا يحتوي"):
            run_option("video.to.mp3", [samples["noaudio"]])
    finally:
        app.setLayoutDirection(Qt.LeftToRight)
        monkeypatch.undo()
        i18n.set_language("en")
        for d in english:
            d.deleteLater()
        app.processEvents()
    assert i18n.tr("{name} part {n}") == "{name} part {n}"
