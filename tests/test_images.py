"""Tests for image conversions (satsuma.converters.images) and tools (satsuma.tools.image_basic)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from satsuma import registry
from satsuma.config import settings
from tests.helpers import FakeCtx, run_option

TARGETS = ["jpg", "png", "webp", "heic", "tiff", "avif", "bmp", "ico", "pdf"]


@pytest.fixture(scope="session", autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication

    settings.set("output_location", "same")  # in-memory only, never saved
    app = QApplication.instance() or QApplication([])
    registry.load_all()
    yield app


def _photo(size=(640, 480), seed=1) -> Image.Image:
    """Noisy gradient so lossy compression has something to chew on."""
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (x * 255 // size[0], y * 255 // size[1], rnd.randrange(256))
    d = ImageDraw.Draw(img)
    d.ellipse((50, 50, 250, 250), fill=(240, 200, 40))
    return img


def _exif(orientation: int = 6) -> bytes:
    ex = Image.Exif()
    ex[0x0112] = orientation
    ex[0x010F] = "TestCam"
    ex[0x0110] = "Model X"
    ex.get_ifd(0x8769)[0x9003] = "2023:04:05 06:07:08"
    return ex.tobytes()


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("samples")
    s: dict[str, Path] = {}
    photo = _photo()
    s["jpg"] = d / "photo.jpg"
    photo.save(s["jpg"], quality=95, exif=_exif(6))  # stored 640x480, displayed 480x640
    alpha = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(alpha).rectangle((50, 50, 250, 150), fill=(255, 0, 0, 255))
    s["png"] = d / "alpha.png"
    alpha.save(s["png"])
    s["webp"] = d / "pic.webp"
    _photo((320, 240), 2).save(s["webp"], quality=90)
    s["tiff"] = d / "pic.tiff"
    _photo((200, 300), 3).save(s["tiff"])
    s["bmp"] = d / "pic.bmp"
    _photo((120, 80), 4).save(s["bmp"])
    s["heic"] = d / "pic.heic"
    _photo((256, 192), 5).save(s["heic"], quality=80)
    s["avif"] = d / "pic.avif"
    _photo((256, 192), 6).save(s["avif"], quality=80)
    s["ico"] = d / "icon.ico"
    alpha.resize((64, 64)).save(s["ico"], sizes=[(16, 16), (32, 32), (64, 64)])
    frames = [Image.new("RGB", (60, 40), c) for c in ("red", "green", "blue")]
    s["gif"] = d / "anim.gif"
    frames[0].save(s["gif"], save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    s["svg"] = d / "vector.svg"
    s["svg"].write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
        '<rect x="10" y="10" width="180" height="80" rx="12" fill="#ff8a1f"/>'
        '<circle cx="100" cy="50" r="30" fill="#1c1c20"/></svg>', encoding="utf-8")
    return s


def _expected_size(key: str) -> tuple[int, int]:
    return {"jpg": (480, 640), "png": (300, 200), "webp": (320, 240), "tiff": (200, 300),
            "bmp": (120, 80), "heic": (256, 192), "avif": (256, 192), "ico": (64, 64),
            "gif": (60, 40), "svg": (1024, 512)}[key]


# --- registration -------------------------------------------------------------------

def test_conversion_options_registered():
    from satsuma.converters import images

    ids = {o.id for o in registry.all_options()}
    for t in TARGETS:
        if t == "avif" and not images.AVIF_OK:
            continue
        assert f"image.to.{t}" in ids
        opt = registry.get(f"image.to.{t}")
        assert opt.mode == "convert" and opt.family == "image"
    assert registry.get("image.to.png").label == "PNG"
    for tid in ("image.compress", "image.metadata", "image.crop", "image.make_pdf", "image.collage"):
        assert registry.get(tid).mode == "tools"


def test_skip_same_ext(samples):
    labels = [o.label for o in registry.options_for("convert", [samples["png"]])]
    assert "PNG" not in labels and "JPG" in labels
    jpeg = samples["jpg"].with_name("x.jpeg")
    assert not registry.get("image.to.jpg").applies([jpeg])
    assert registry.get("image.to.png").applies([samples["png"], samples["jpg"]])
    assert not registry.get("image.make_pdf").applies([samples["png"]])


# --- conversions --------------------------------------------------------------------

@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("key", ["jpg", "png", "webp", "tiff", "bmp", "heic", "avif", "ico", "gif", "svg"])
def test_convert(samples, key, target):
    from satsuma.converters import images

    if target == "avif" and not images.AVIF_OK:
        pytest.skip("no AVIF codec")
    opt = registry.get(f"image.to.{target}")
    src = samples[key]
    if not opt.applies([src]):
        assert images.canonical_ext(key) == target
        return
    (out,) = run_option(f"image.to.{target}", [src])
    assert out.suffix == f".{target}"
    assert out.parent == src.parent
    if target == "pdf":
        import pymupdf

        with pymupdf.open(out) as doc:
            assert doc.page_count == 1
            w, h = _expected_size(key)
            r = doc[0].rect
            assert abs(r.width / r.height - w / h) < 0.02
        return
    with Image.open(out) as im:
        im.load()
        assert im.format == images.PIL_FORMAT[target]
        if target == "ico":
            assert max(im.size) <= 256 and im.size[0] == im.size[1]
        else:
            assert im.size == _expected_size(key), (key, target, im.size)
        if target in ("jpg", "bmp"):
            assert im.mode in ("RGB", "L")
        if key == "jpg" and target in ("png", "webp", "heic", "avif", "tiff", "jpg"):
            exif = im.getexif()
            assert exif.get(0x010F) == "TestCam"
            assert exif.get(0x0112, 1) == 1  # orientation baked into the pixels
    out.unlink()


def test_alpha_flattened_to_white_for_jpg(samples):
    (out,) = run_option("image.to.jpg", [samples["png"]])
    with Image.open(out) as im:
        r, g, b = im.convert("RGB").getpixel((5, 5))
        assert min(r, g, b) > 240
        r, g, b = im.convert("RGB").getpixel((150, 100))
        assert r > 200 and g < 60


def test_png_keeps_alpha(samples):
    (out,) = run_option("image.to.webp", [samples["png"]])
    with Image.open(out) as im:
        assert im.mode == "RGBA" and im.getpixel((5, 5))[3] == 0


def test_gif_to_webp_keeps_animation(samples):
    (out,) = run_option("image.to.webp", [samples["gif"]])
    with Image.open(out) as im:
        assert getattr(im, "n_frames", 1) == 3


def test_gif_to_png_first_frame(samples):
    (out,) = run_option("image.to.png", [samples["gif"]])
    with Image.open(out) as im:
        assert im.convert("RGB").getpixel((5, 5)) == (255, 0, 0)


def test_svg_render_size(samples):
    from satsuma.converters.images import open_image

    img = open_image(samples["svg"])
    assert img.size == (1024, 512) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # transparent corner
    assert img.getpixel((100, 256))[:3] == (255, 138, 31)
    assert img.getpixel((512, 256))[:3] == (28, 28, 32)


def test_open_save_helpers(samples, tmp_path):
    from satsuma.converters.images import encode_image, open_image, save_image

    img = open_image(samples["jpg"])
    assert img.size == (480, 640)
    out = save_image(img, tmp_path / "x.jpeg", "jpeg", quality=50)
    assert Image.open(out).format == "JPEG"
    assert len(encode_image(img, "png")) > 1000
    hint = open_image(samples["jpg"], size_hint=100)
    assert max(hint.size) < 640


@pytest.mark.parametrize("target", ["jpg", "png", "webp", "ico", "pdf", "heic", "bmp", "tiff"])
def test_odd_modes(tmp_path, target):
    cmyk = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (40, 30), (0, 255, 255, 0)).save(cmyk)
    deep = tmp_path / "deep.png"
    Image.new("I;16", (40, 30), 40000).save(deep)
    pal = tmp_path / "pal.gif"
    p = Image.new("P", (40, 30), 1)
    p.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    p.info["transparency"] = 0
    p.save(pal, transparency=0)
    for src in (cmyk, deep, pal):
        if not registry.get(f"image.to.{target}").applies([src]):
            continue
        (out,) = run_option(f"image.to.{target}", [src])
        if target != "pdf":
            with Image.open(out) as im:
                im.load()


def test_multi_select_convert(samples):
    outs = run_option("image.to.png", [samples["jpg"], samples["webp"], samples["svg"]])
    assert len(outs) == 3 and all(o.suffix == ".png" for o in outs)


# --- compress -----------------------------------------------------------------------

@pytest.mark.parametrize("key", ["jpg", "png", "webp", "heic", "avif", "tiff"])
@pytest.mark.parametrize("preset", ["high", "small"])
def test_compress_preset(samples, key, preset):
    (out,) = run_option("image.compress", [samples[key]], {"mode": "preset", "preset": preset})
    assert out.name.endswith(f"-compressed{samples[key].suffix}")
    with Image.open(out) as im:
        im.load()
    assert out.stat().st_size <= samples[key].stat().st_size
    out.unlink()


def test_compress_png_quantizes(tmp_path):
    src = tmp_path / "noisy.png"
    _photo((300, 300), 9).save(src)
    (out,) = run_option("image.compress", [src], {"mode": "preset", "preset": "medium"})
    assert out.stat().st_size < src.stat().st_size * 0.8
    with Image.open(out) as im:
        assert im.size == (300, 300)


def test_compress_target_size_jpg(samples):
    src = samples["jpg"]
    target = src.stat().st_size // 4
    (out,) = run_option("image.compress", [src], {"mode": "size", "target_bytes": target})
    assert out.stat().st_size <= max(1024, target)
    with Image.open(out) as im:
        assert im.size == (480, 640)


def test_compress_ratio_shrinks_pixels_when_needed(samples):
    src = samples["webp"]
    (out,) = run_option("image.compress", [src], {"mode": "size", "ratio": 0.05})
    assert out.stat().st_size <= max(1024, int(src.stat().st_size * 0.05))


def test_compress_percent_png_and_resize(samples):
    src = samples["png"]
    (out,) = run_option("image.compress", [src], {"mode": "percent", "percent": 30,
                                                  "resize": {"kind": "edge", "value": 150}})
    with Image.open(out) as im:
        assert max(im.size) == 150


def test_compress_selection(samples):
    outs = run_option("image.compress", [samples["jpg"], samples["png"]],
                      {"mode": "percent", "ratio": 0.5, "percent": 50})
    assert len(outs) == 2


# --- metadata -----------------------------------------------------------------------

def _scan_data(path: Path) -> bytes:
    data = path.read_bytes()
    return data[data.index(b"\xff\xda"):]


def test_metadata_read(samples):
    from satsuma.tools.image_basic import read_metadata

    m = read_metadata(samples["jpg"])
    assert m["make"] == "TestCam" and m["model"] == "Model X"
    assert m["date"] == "2023-04-05 06:07:08"


def test_metadata_edit_jpg_lossless(samples):
    from satsuma.tools.image_basic import read_metadata

    params = {"set": {"artist": "Jane Doe", "copyright": "(c) 2024", "description": "A test",
                      "date": "2024-01-02 03:04:05", "lat": "-33.8688", "lon": "151.2093",
                      "model": "Model Y"}, "clear": ["make"]}
    (out,) = run_option("image.metadata", [samples["jpg"]], params)
    assert out.name == "photo-edited.jpg"
    m = read_metadata(out)
    assert m["artist"] == "Jane Doe" and m["copyright"] == "(c) 2024"
    assert m["description"] == "A test" and m["model"] == "Model Y" and m["make"] == ""
    assert m["date"] == "2024-01-02 03:04:05"
    assert abs(float(m["lat"]) + 33.8688) < 1e-4 and abs(float(m["lon"]) - 151.2093) < 1e-4
    assert _scan_data(out) == _scan_data(samples["jpg"])  # pixels untouched
    with Image.open(out) as im:
        assert im.getexif().get(0x0112) == 6


def test_metadata_strip(samples):
    from satsuma.tools.image_basic import read_metadata

    (out,) = run_option("image.metadata", [samples["jpg"]], {"strip": True})
    m = read_metadata(out)
    assert all(v == "" for v in m.values())
    with Image.open(out) as im:
        assert im.getexif().get(0x0112) == 6  # orientation kept so it still displays upright
        assert im.size == (640, 480)


@pytest.mark.parametrize("key", ["png", "webp", "heic", "tiff", "avif"])
def test_metadata_other_formats(samples, key):
    from satsuma.tools.image_basic import read_metadata

    (out,) = run_option("image.metadata", [samples[key]], {"set": {"artist": "Bob", "lat": "10.5"}})
    m = read_metadata(out)
    assert m["artist"] == "Bob" and abs(float(m["lat"]) - 10.5) < 1e-4
    (out2,) = run_option("image.metadata", [out], {"strip": True})
    assert read_metadata(out2)["artist"] == ""


def test_metadata_overwrite(samples, tmp_path):
    from satsuma.tools.image_basic import read_metadata

    src = tmp_path / "copy.jpg"
    src.write_bytes(samples["jpg"].read_bytes())
    (out,) = run_option("image.metadata", [src], {"set": {"artist": "Over"}, "overwrite": True})
    assert out == src and read_metadata(src)["artist"] == "Over"
    assert list(tmp_path.iterdir()) == [src]


def test_metadata_bad_date(samples):
    with pytest.raises(registry.UserError):
        run_option("image.metadata", [samples["jpg"]], {"set": {"date": "yesterday"}})


# --- crop ---------------------------------------------------------------------------

def test_crop_free(samples):
    (out,) = run_option("image.crop", [samples["jpg"]], {"rect": [0.25, 0.25, 0.5, 0.5], "aspect": None})
    assert out.name == "photo-cropped.jpg"
    with Image.open(out) as im:
        assert im.size == (240, 320)


def test_crop_aspect_and_exact_size(samples):
    (out,) = run_option("image.crop", [samples["png"]], {"rect": [0, 0, 0.5, 0.75], "aspect": 1.0})
    with Image.open(out) as im:
        assert im.size == (150, 150) and im.mode == "RGBA"
    (out,) = run_option("image.crop", [samples["webp"]], {"rect": [0, 0, 1, 1], "size": [160, 90]})
    with Image.open(out) as im:
        assert im.size == (160, 90)


def test_crop_selection_same_relative_crop(samples):
    outs = run_option("image.crop", [samples["jpg"], samples["tiff"], samples["svg"]],
                      {"rect": [0.1, 0.1, 0.5, 0.5], "aspect": 16 / 9})
    assert outs[2].suffix == ".png"
    for o in outs:
        with Image.open(o) as im:
            assert abs(im.width / im.height - 16 / 9) < 0.03


def test_crop_box_reproduces_reference():
    from satsuma.tools.image_basic import crop_box

    W, H = 1000, 500
    rect = [0.2, 0.1, 0.4, 0.6]
    aspect = (0.4 * W) / (0.6 * H)
    assert crop_box((W, H), rect, aspect) == crop_box((W, H), rect, None) == (200, 50, 600, 350)


# --- create pdf ---------------------------------------------------------------------

def test_make_pdf(samples):
    import pymupdf

    order = [str(samples["png"]), str(samples["jpg"]), str(samples["heic"]), str(samples["svg"])]
    (out,) = run_option("image.make_pdf", [samples["jpg"], samples["png"], samples["heic"], samples["svg"]],
                        {"order": order, "page": "fit", "margin_mm": 0})
    assert out.name == "Combined.pdf" and out.parent == samples["png"].parent
    with pymupdf.open(out) as doc:
        assert doc.page_count == 4
        assert doc[0].rect.width > doc[0].rect.height  # alpha.png is landscape
        assert doc[1].rect.width < doc[1].rect.height  # rotated photo is portrait
        assert len(doc[1].get_images()) == 1
    out.unlink()


def test_make_pdf_a4(samples):
    import pymupdf

    (out,) = run_option("image.make_pdf", [samples["jpg"], samples["webp"]],
                        {"page": "a4", "margin_mm": 10, "name": "Album"})
    assert out.name == "Album.pdf"
    with pymupdf.open(out) as doc:
        r0, r1 = doc[0].rect, doc[1].rect
        assert round(r0.width) == 595 and round(r0.height) == 842
        assert round(r1.width) == 842  # landscape image -> landscape page
    out.unlink()


# --- collage ------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["grid", "row", "column", "featured"])
def test_collage_layouts(samples, layout):
    paths = [samples["jpg"], samples["png"], samples["webp"], samples["heic"], samples["svg"]]
    (out,) = run_option("image.collage", paths, {"layout": layout, "width": 800, "spacing": 10,
                                                 "radius": 12, "background": "#202020"})
    assert out.stem.startswith("Collage") and out.suffix == ".png"
    with Image.open(out) as im:
        assert im.width == 800 and im.height > 50
        assert im.convert("RGB").getpixel((2, 2)) == (32, 32, 32)
    out.unlink()


def test_collage_jpg_and_transparent(samples):
    (out,) = run_option("image.collage", [samples["jpg"], samples["png"]],
                        {"layout": "grid", "width": 600, "format": "jpg", "background": "#ffffff"})
    assert out.suffix == ".jpg"
    out.unlink()
    (out,) = run_option("image.collage", [samples["jpg"], samples["png"]],
                        {"layout": "row", "width": 600, "background": "transparent", "spacing": 20})
    with Image.open(out) as im:
        assert im.mode == "RGBA" and im.getpixel((2, 2))[3] == 0
    out.unlink()


def test_collage_layout_math():
    from satsuma.tools.image_basic import collage_layout

    tiles, h = collage_layout([1.0, 2.0], "row", 320, 10)
    assert abs(tiles[1][0] + tiles[1][2] - 310) < 1e-6 and abs(h - (290 / 3 + 20)) < 1e-6
    tiles, h = collage_layout([1.0, 1.0, 1.0], "grid", 320, 0)
    assert len(tiles) == 3 and h > 0


# --- dialogs (constructed only, never exec'd) ------------------------------------------

def test_dialogs_construct(samples):
    from satsuma.tools import image_basic as ib

    two = [samples["jpg"], samples["png"]]
    d = ib.CompressDialog(two)
    for i in range(3):
        d.mode.setCurrentIndex(i)
        d.resize_kind.setCurrentIndex(i)
        v = d.values()
    assert v["mode"] == "percent" and 0 < v["ratio"] < 1
    d.mode.setCurrentIndex(1)
    assert "target_bytes" in d.values()

    m = ib.MetadataDialog(two)
    assert m.values() == {"set": {}, "clear": [], "strip": False, "overwrite": False}
    m.edits["artist"].setText("X")
    assert m.values()["set"] == {"artist": "X"}
    m1 = ib.MetadataDialog([samples["jpg"]])
    assert m1.edits["make"].text() == "TestCam"
    m1.edits["make"].setText("")
    assert m1.values()["clear"] == ["make"]

    c = ib.CropDialog(two)
    c.resize(800, 700)
    c.aspect.setCurrentText("16:9")
    v = c.values()
    assert abs(v["aspect"] - 16 / 9) < 1e-9
    w, h = c.full_size
    rw, rh = v["rect"][2] * w, v["rect"][3] * h
    assert abs(rw / rh - 16 / 9) < 0.01
    c.exact.setChecked(True)
    assert c.values()["size"] == [480, 640]
    c.grab()  # paints the crop view

    p = ib.MakePdfDialog(two)
    p.files._move(1)
    assert p.values()["order"] == [str(samples["png"]), str(samples["jpg"])]

    col = ib.CollageDialog([samples["jpg"], samples["png"], samples["svg"]])
    for i in range(4):
        col.layout_combo.setCurrentIndex(i)
    col.transparent.setChecked(True)
    assert col.values()["background"] == "transparent"
    assert col.preview.pixmap() is not None and not col.preview.pixmap().isNull()


AR = {
    "Compress images": "ضغط الصور", "Compress": "ضغط", "Preset": "إعداد مسبق",
    "Reduce by percentage": "تقليل بنسبة مئوية", "Aim: about {size}": "الهدف: حوالي {size}",
    "Edit metadata": "تحرير البيانات الوصفية", "Author": "المؤلف", "Save": "حفظ",
    "Remove all metadata": "إزالة كل البيانات الوصفية", "e.g. {value}": "مثال: {value}",
    "Crop": "قص", "Free": "حر", "Exact size": "حجم دقيق", "Reset": "إعادة تعيين",
    "Crop: {crop} px from {full}": "القص: {crop} بكسل من {full}",
    "Create PDF": "إنشاء PDF", "Combined": "مدمج", "Fit image": "ملاءمة الصورة",
    "{n} images · one page per image, drag to reorder": "{n} صور · صفحة لكل صورة، اسحب لإعادة الترتيب",
    "↑ Up": "↑ أعلى", "Collage": "كولاج", "Grid": "شبكة", "Featured": "مميز", "{n} images": "{n} صور",
    "Create collage": "إنشاء كولاج", "Transparent": "شفاف",
    "Latitude must be between {lo} and {hi}": "يجب أن يكون خط العرض بين {lo} و {hi}",
}


def test_dialogs_translated_rtl(samples, monkeypatch):
    """Every dialog builds in Arabic (RTL) with translated text; logic uses item data."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from satsuma import i18n
    from satsuma.tools import image_basic as ib

    two = [samples["jpg"], samples["png"]]
    i18n.set_language("ar")
    monkeypatch.setattr(i18n, "_catalog", dict(AR))
    QApplication.setLayoutDirection(Qt.RightToLeft)
    try:
        assert i18n.is_rtl()
        d = ib.CompressDialog(two)
        assert AR["Compress images"] in d.windowTitle() and d.primary_btn.text() == AR["Compress"]
        assert d.mode.itemText(0) == AR["Preset"]
        d.mode.setCurrentIndex(d.mode.findData("percent"))
        assert d.mode.currentText() == AR["Reduce by percentage"]
        assert d.values()["mode"] == "percent" and d.estimate.text().startswith("الهدف")
        d.mode.setCurrentIndex(d.mode.findData("size"))
        d.unit.setCurrentIndex(d.unit.findData(1024 * 1024))
        d.size_spin.setValue(2)
        assert d.values()["target_bytes"] == 2 * 1024 * 1024
        d.grab()

        m = ib.MetadataDialog([samples["jpg"]])
        assert m.form.labelForField(m.edits["artist"]).text() == AR["Author"]
        assert m.strip.text() == AR["Remove all metadata"] and m.primary_btn.text() == AR["Save"]
        assert m.edits["lat"].placeholderText() == "مثال: 48.858370"
        m.edits["lat"].setText("95")
        m.accept()  # invalid latitude: shows the translated error instead of closing
        assert m.error.text() == "يجب أن يكون خط العرض بين -90 و 90"
        m.grab()

        c = ib.CropDialog(two)
        c.resize(800, 700)
        assert c.aspect.itemText(0) == AR["Free"] and c.exact.text() == AR["Exact size"]
        assert c.values()["aspect"] is None
        c.aspect.setCurrentIndex(c.aspect.findData("16:9"))
        assert abs(c.values()["aspect"] - 16 / 9) < 1e-9
        c.aspect.setCurrentIndex(c.aspect.findData("Original"))
        assert abs(c.values()["aspect"] - 480 / 640) < 1e-9
        assert c.info.text().startswith("القص:")
        c.grab()

        p = ib.MakePdfDialog(two)
        assert p.name.text() == AR["Combined"] and p.page.itemText(0) == AR["Fit image"]
        assert p.values()["page"] == "fit" and p.values()["name"] == AR["Combined"]
        p.name.setText("")
        assert p.values()["name"] == AR["Combined"]
        p.grab()

        col = ib.CollageDialog([samples["jpg"], samples["png"], samples["svg"]])
        assert col.layout_combo.itemText(0) == AR["Grid"] and col.transparent.text() == AR["Transparent"]
        col.layout_combo.setCurrentIndex(col.layout_combo.findData("featured"))
        assert col.values()["layout"] == "featured"
        assert not col.preview.pixmap().isNull()
        col.grab()

        (out,) = run_option("image.make_pdf", two, {"page": "fit"})
        assert out.name == f"{AR['Combined']}.pdf"
        out.unlink()
    finally:
        QApplication.setLayoutDirection(Qt.LeftToRight)
        monkeypatch.undo()
        i18n.set_language("en")
    assert i18n.tr("Collage") == "Collage"


def test_crop_view_drag(samples):
    from PySide6.QtCore import QPointF, QRectF

    from satsuma.tools import image_basic as ib

    c = ib.CropDialog([samples["png"]])
    v = c.view
    v.resize(600, 420)
    v.set_aspect(1.0)
    r = v._from_anchor(QPointF(0.1, 0.1), QPointF(0.9, 0.5))
    assert abs(r.width() * 300 / (r.height() * 200) - 1.0) < 1e-6
    v._start_rect = QRectF(0.2, 0.2, 0.3, 0.45)
    e = v._edge("r", QPointF(0.7, 0.5))
    assert abs(e.width() * 300 / (e.height() * 200) - 1.0) < 1e-6
    assert e.right() <= 1 and e.bottom() <= 1
