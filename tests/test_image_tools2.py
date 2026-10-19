"""Tests for the Edit / Annotate / Add BG / Redact image tools."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QApplication

from satsuma import registry
from satsuma.tools import image_annotate, image_background, image_edit, image_redact
from tests.helpers import FakeCtx, run_option


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _photo(path: Path, size=(320, 240), mode="RGB") -> Path:
    """A colourful test image: gradient + shapes."""
    w, h = size
    x = np.linspace(0, 255, w, dtype=np.float32)[None, :].repeat(h, 0)
    y = np.linspace(0, 255, h, dtype=np.float32)[:, None].repeat(w, 1)
    arr = np.stack([x, y, 255 - x * 0.5], axis=2).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    d = ImageDraw.Draw(img)
    d.rectangle((w // 4, h // 4, w // 2, h // 2), fill=(250, 250, 250))
    d.ellipse((w // 2, h // 2, w * 3 // 4, h * 3 // 4), fill=(20, 20, 20))
    if mode == "RGBA":
        img = img.convert("RGBA")
    img.save(path)
    return path


def _mean(img: Image.Image) -> float:
    return float(np.asarray(img.convert("RGB"), dtype=np.float32).mean())


# ---------------------------------------------------------------- registry

def test_options_registered():
    registry.load_all()
    for oid in ("image.edit", "image.annotate", "image.add_background", "image.redact"):
        opt = registry.get(oid)
        assert opt.mode == "tools" and opt.family == "image"
        assert opt.exts == frozenset(image_edit.formats.IMAGE_IN)
    assert registry.get("image.annotate").max_files == 1


# ---------------------------------------------------------------- edit

def test_identity_adjustments_keep_pixels(tmp_path):
    img = Image.open(_photo(tmp_path / "a.png")).convert("RGB")
    out = image_edit.apply_adjustments(img, {}, "None")
    diff = np.abs(np.asarray(out, np.int16) - np.asarray(img, np.int16))
    assert diff.max() <= 1


def test_adjustments_change_pixels_as_expected(tmp_path):
    img = Image.open(_photo(tmp_path / "a.png")).convert("RGB")
    base = _mean(img)
    assert _mean(image_edit.apply_adjustments(img, {"exposure": 60})) > base + 10
    assert _mean(image_edit.apply_adjustments(img, {"exposure": -60})) < base - 10
    assert _mean(image_edit.apply_adjustments(img, {"brightness": 50})) > base
    # contrast raises the spread
    hi = np.asarray(image_edit.apply_adjustments(img, {"contrast": 60}), np.float32)
    assert hi.std() > np.asarray(img, np.float32).std()
    # warm temperature: more red, less blue
    warm = np.asarray(image_edit.apply_adjustments(img, {"temperature": 80}), np.float32)
    orig = np.asarray(img, np.float32)
    assert warm[..., 0].mean() > orig[..., 0].mean() and warm[..., 2].mean() < orig[..., 2].mean()
    # mono filter / -100 saturation -> grey
    mono = np.asarray(image_edit.apply_adjustments(img, {}, "Mono"), np.int16)
    assert np.abs(mono[..., 0] - mono[..., 1]).max() <= 1
    gray = np.asarray(image_edit.apply_adjustments(img, {"saturation": -100}), np.int16)
    assert np.abs(gray[..., 0] - gray[..., 2]).max() <= 2
    # vignette darkens corners, not the centre
    vig = np.asarray(image_edit.apply_adjustments(img, {"vignette": 100}), np.float32)
    assert vig[:10, :10].mean() < orig[:10, :10].mean() - 5
    # blur smooths edges
    blur = np.asarray(image_edit.apply_adjustments(img, {"blur": 50}), np.float32)
    assert np.abs(np.diff(blur, axis=1)).mean() < np.abs(np.diff(orig, axis=1)).mean()
    for key in ("highlights", "shadows", "vibrance", "tint", "sharpness", "clarity", "grain"):
        out = image_edit.apply_adjustments(img, {key: 70})
        assert out.size == img.size and not np.array_equal(np.asarray(out), np.asarray(img)), key
    for flt in image_edit.FILTERS[1:]:
        assert image_edit.apply_adjustments(img, {}, flt).size == img.size


def test_edit_keeps_alpha(tmp_path):
    img = Image.new("RGBA", (50, 40), (200, 100, 50, 0))
    img.paste((10, 20, 30, 255), (0, 0, 25, 40))
    out = image_edit.apply_adjustments(img, {"exposure": 30})
    assert out.mode == "RGBA"
    assert np.array_equal(np.asarray(out.getchannel("A")), np.asarray(img.getchannel("A")))


def test_edit_run_multiple(tmp_path):
    a = _photo(tmp_path / "one.jpg")
    b = _photo(tmp_path / "two.png", mode="RGBA")
    outs = run_option("image.edit", [a, b], {"adjustments": {"exposure": 40}, "filter": "Sepia"})
    assert [o.name for o in outs] == ["one-edited.jpg", "two-edited.png"]
    assert _mean(Image.open(outs[0])) > _mean(Image.open(a))
    assert Image.open(outs[1]).size == (320, 240)


# ---------------------------------------------------------------- background

@pytest.mark.parametrize("aspect,ratio", [("1:1", 1.0), ("4:5", 0.8), ("16:9", 16 / 9), ("9:16", 9 / 16),
                                          ("4:3", 4 / 3)])
def test_background_aspect(tmp_path, aspect, ratio):
    src = _photo(tmp_path / "p.jpg", size=(300, 200))
    outs = run_option("image.add_background", [src], {"kind": "solid", "color1": "#00ff00",
                                                      "aspect": aspect, "padding": 10})
    out = Image.open(outs[0])
    assert outs[0].name == "p-background.png"
    assert abs(out.width / out.height - ratio) < 0.01
    assert out.width >= 300 and out.height >= 200
    assert out.getpixel((1, 1))[:3] == (0, 255, 0)


def test_background_original_padding_and_gradient(tmp_path):
    img = Image.new("RGB", (100, 50), "white")
    out = image_background.render_background(img, {"kind": "gradient", "color1": "#000000",
                                                   "color2": "#ffffff", "angle": 0, "aspect": "Original",
                                                   "padding": 10, "radius": 0, "shadow": False})
    assert out.size == (120, 70)
    left, right = out.getpixel((0, 35)), out.getpixel((119, 35))
    assert left[0] < 30 and right[0] > 225  # left->right gradient
    assert out.getpixel((60, 35)) == (255, 255, 255)  # photo in the middle


def test_background_image_kind_and_keep_jpg(tmp_path):
    bg = _photo(tmp_path / "bg.png", size=(80, 80))
    src = _photo(tmp_path / "p.jpg", size=(120, 90))
    outs = run_option("image.add_background", [src], {"kind": "image", "image": str(bg), "blur_image": True,
                                                      "aspect": "1:1", "radius": 10, "shadow": True,
                                                      "format": "keep"})
    assert outs[0].suffix == ".jpg"
    out = Image.open(outs[0])
    assert out.width == out.height


def test_background_image_kind_requires_image(tmp_path):
    src = _photo(tmp_path / "p.png")
    with pytest.raises(registry.UserError):
        run_option("image.add_background", [src], {"kind": "image", "image": ""})


# ---------------------------------------------------------------- redact

BOX = (0.2, 0.2, 0.4, 0.4)  # straddles the white rectangle and dark ellipse edges


def _region(img: Image.Image, rel=BOX) -> np.ndarray:
    x, y, w, h = image_redact.to_abs(rel, img.size)
    return np.asarray(img.convert("RGB"), np.int16)[y:y + h, x:x + w]


def test_redact_styles_change_box_only(tmp_path):
    img = Image.open(_photo(tmp_path / "a.png")).convert("RGB")
    outside = (slice(200, 240), slice(0, 40))
    for style in ("solid", "blur", "pixelate"):
        out = image_redact.apply_redaction(img, [BOX], style, "#000000", 60)
        before, after = _region(img), _region(out)
        assert np.abs(before - after).mean() > 3, style
        assert np.array_equal(np.asarray(out)[outside], np.asarray(img)[outside]), style
    solid = image_redact.apply_redaction(img, [BOX], "solid", "#ff0000")
    assert (_region(solid) == [255, 0, 0]).all()


def test_redact_blur_hides_detail(tmp_path):
    img = Image.new("RGB", (200, 200), "white")
    d = ImageDraw.Draw(img)
    for i in range(0, 200, 4):
        d.line((i, 0, i, 200), fill="black")
    rel = (0.2, 0.2, 0.6, 0.6)
    for style in ("blur", "pixelate"):
        out = image_redact.apply_redaction(img, [rel], style, strength=60)
        assert _region(out, rel).std() < _region(img, rel).std() * 0.5, style


def test_redact_run_with_boxes(tmp_path):
    src = _photo(tmp_path / "a.jpg")
    outs = run_option("image.redact", [src], {"boxes": {str(src): [list(BOX)]}, "style": "solid",
                                              "color": "#000000"})
    assert outs[0].name == "a-redacted.jpg"
    reg = _region(Image.open(outs[0]))
    assert reg[3:-3, 3:-3].mean() < 15  # jpeg noise allowed


def test_redact_run_auto_detect_without_boxes(tmp_path):
    src = _photo(tmp_path / "a.png")
    outs = run_option("image.redact", [src], {"auto": ["faces"], "style": "pixelate"})
    assert outs[0].exists()


def test_detect_faces_runs_on_synthetic(tmp_path):
    img = Image.new("RGB", (400, 300), (200, 180, 160))
    d = ImageDraw.Draw(img)
    d.ellipse((150, 70, 250, 200), fill=(230, 200, 170))
    d.ellipse((175, 110, 190, 120), fill=(30, 30, 30))
    d.ellipse((210, 110, 225, 120), fill=(30, 30, 30))
    d.rectangle((185, 165, 215, 172), fill=(120, 40, 40))
    boxes = image_redact.detect_faces(img)
    assert isinstance(boxes, list)
    for b in boxes:
        assert len(b) == 4 and b[2] > 0 and b[3] > 0
        assert 0 <= b[0] <= 400 and 0 <= b[1] <= 300


def test_detect_faces_without_backend_returns_empty(monkeypatch):
    monkeypatch.setattr(image_redact, "face_backend", lambda: None)
    assert image_redact.detect_faces(Image.new("RGB", (100, 100))) == []
    assert image_redact.faces_available() is False


@pytest.mark.skipif(not image_redact.faces_available(), reason="no local face detector installed")
def test_detect_faces_backend_handles_large_image():
    img = Image.new("RGB", (3000, 2000), (128, 128, 128))
    for b in image_redact.detect_faces(img):
        assert b[0] + b[2] <= 3000 and b[1] + b[3] <= 2000


def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


@pytest.mark.skipif(not image_redact.ocr_available(), reason="Windows OCR not available")
def test_detect_text_finds_words():
    img = Image.new("RGB", (900, 260), "white")
    ImageDraw.Draw(img).text((40, 70), "SECRET 12345", fill="black", font=_font(90))
    boxes = image_redact.detect_text(img)
    assert len(boxes) >= 1
    for x, y, w, h in boxes:
        assert 0 <= x < 900 and 0 <= y < 260 and w > 10 and h > 10


def test_detect_text_unavailable_returns_list(monkeypatch):
    monkeypatch.setattr(image_redact, "ocr_available", lambda: False)
    assert image_redact.detect_text(Image.new("RGB", (50, 50))) == []


def test_rel_abs_roundtrip():
    size = (640, 480)
    b = (64, 48, 128, 96)
    assert image_redact.to_abs(image_redact.to_rel(b, size), size) == b


# ---------------------------------------------------------------- dialogs

def test_edit_dialog_constructs(qapp, tmp_path):
    a, b = _photo(tmp_path / "a.png"), _photo(tmp_path / "b.jpg")
    dlg = image_edit.EditDialog([a, b])
    dlg.sliders["exposure"].setValue(30)
    dlg._render()
    dlg._go(1)
    assert dlg.index == 1
    v = dlg.values()
    assert v["adjustments"]["exposure"] == 30 and v["filter"] == "None"
    dlg.reset()
    assert dlg.values()["adjustments"]["exposure"] == 0
    dlg.deleteLater()


def test_background_dialog_constructs(qapp, tmp_path):
    dlg = image_background.BackgroundDialog([_photo(tmp_path / "a.png")])
    dlg.aspect.setCurrentText("16:9")
    dlg._render()
    v = dlg.values()
    assert v["aspect"] == "16:9" and v["kind"] in ("solid", "gradient", "image")
    dlg.deleteLater()


def test_redact_dialog_constructs(qapp, tmp_path):
    a = _photo(tmp_path / "a.png")
    dlg = image_redact.RedactDialog([a])
    dlg.add_box(QRectF(10, 10, 50, 40), "manual")
    dlg.show_result.setChecked(True)
    v = dlg.values()
    boxes = v["boxes"][str(a)]
    assert len(boxes) >= 1 and all(0 <= c <= 1 for c in boxes[-1])
    # toggling a box off excludes it
    item = dlg._boxes[0][-1]
    item.enabled = False
    dlg.boxes_changed()
    assert len(dlg.values()["boxes"][str(a)]) == len(boxes) - 1
    dlg.deleteLater()


def test_annotate_dialog_draw_undo_and_save(qapp, tmp_path):
    src = _photo(tmp_path / "shot.png", size=(400, 300))
    dlg = image_annotate.AnnotateDialog(src)
    view = dlg.view
    scene = view.scene_
    # add items the same way the mouse handlers do
    rect = image_annotate.QGraphicsRectItem(QRectF(20, 20, 100, 60))
    rect.setPen(view.pen())
    view._prepare(rect)
    view.undo.push(image_annotate.AddCmd(scene, rect))
    arrow = image_annotate.LineItem(QPointF(200, 200), view.pen(), arrow=True)
    arrow.set_end(QPointF(350, 120))
    view._prepare(arrow)
    view.undo.push(image_annotate.AddCmd(scene, arrow))
    for i in range(2):
        c = image_annotate.CalloutItem(view.next_callout(), view.color, 15)
        c.setPos(60 + 40 * i, 250)
        view._prepare(c)
        view.undo.push(image_annotate.AddCmd(scene, c))
    assert view.next_callout() == 3
    blur = image_annotate.BlurItem(QRectF(250, 20, 100, 80), view.blurred_pixmap(), view.unit)
    view._prepare(blur)
    view.undo.push(image_annotate.AddCmd(scene, blur))
    assert len(view.annotations()) == 5
    view.undo.undo()
    assert len(view.annotations()) == 4
    view.undo.redo()
    assert len(view.annotations()) == 5
    rect.setSelected(True)
    view.delete_selected()
    assert rect.scene() is None
    view.undo.undo()
    assert rect.scene() is scene
    for tool, *_ in image_annotate.TOOLS:
        dlg.set_tool(tool)

    outs = None
    ctx = FakeCtx()
    outs = dlg.save(ctx)
    assert outs.name == "shot-annotated.png"
    out = Image.open(outs)
    assert out.size == (400, 300)
    assert not np.array_equal(np.asarray(out.convert("RGB")), np.asarray(Image.open(src).convert("RGB")))
    # the red rectangle's outline is drawn at full resolution
    r, g, b = out.convert("RGB").getpixel((20, 50))
    assert r > 200 and g < 100 and b < 100
    dlg.deleteLater()


# ---------------------------------------------------------------- i18n / RTL

FAKE_AR = {
    "Edit {n} images": "تحرير {n} صور",
    "Save": "حفظ",
    "None": "بلا",
    "Sepia": "بني داكن",
    "Exposure": "التعريض",
    "Original": "الأصلي",
    "Add background": "إضافة خلفية",
    "Redact": "تنقيح",
    "Detect faces": "اكتشاف الوجوه",
    "Annotate": "تعليق توضيحي",
    "Arrow": "سهم",
}


@pytest.fixture
def arabic_rtl(qapp, monkeypatch):
    from PySide6.QtCore import Qt

    from satsuma import i18n

    i18n.set_language("ar")
    monkeypatch.setattr(i18n, "_catalog", dict(FAKE_AR))
    qapp.setLayoutDirection(Qt.RightToLeft)
    try:
        yield qapp
    finally:
        i18n.set_language("en")
        qapp.setLayoutDirection(Qt.LeftToRight)


def _halves(path: Path) -> Path:
    """Left half red, right half blue: mirroring would be obvious."""
    arr = np.zeros((300, 400, 3), np.uint8)
    arr[:, :200] = (255, 0, 0)
    arr[:, 200:] = (0, 0, 255)
    Image.fromarray(arr).save(path)
    return path


def _drag(widget, a, b):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.mousePress(widget, Qt.LeftButton, Qt.NoModifier, a)
    QTest.mouseMove(widget, b)
    QTest.mouseRelease(widget, Qt.LeftButton, Qt.NoModifier, b)


def _redact_draw(src: Path, a=None, b=None):
    """Draw a box at viewport points a->b (defaults: scene (40,30)-(160,120) in this
    direction). Returns (a, b, saved boxes, colour under scene (50,150))."""
    dlg = image_redact.RedactDialog([src])
    dlg.clear_boxes()
    dlg.resize(1000, 700)
    dlg.show()
    QApplication.processEvents()
    v = dlg.view
    a = a or v.mapFromScene(QPointF(40, 30))
    b = b or v.mapFromScene(QPointF(160, 120))
    _drag(v.viewport(), a, b)
    shot = v.viewport().grab().toImage()
    colour = shot.pixelColor(v.mapFromScene(QPointF(50, 150))).name()
    boxes = dlg.values()["boxes"][str(src)]
    dlg.close()
    dlg.deleteLater()
    return a, b, boxes, colour


def _annotate_draw(src: Path, a=None, b=None):
    dlg = image_annotate.AnnotateDialog(src)
    dlg.resize(1000, 700)
    dlg.show()
    QApplication.processEvents()
    v = dlg.view
    dlg.set_tool("rect")
    a = a or v.mapFromScene(QPointF(60, 50))
    b = b or v.mapFromScene(QPointF(300, 220))
    _drag(v.viewport(), a, b)
    out = np.asarray(dlg.render_pil().convert("RGB"), np.int16)
    dlg.close()
    dlg.deleteLater()
    return a, b, out


def test_dialogs_translated_and_rtl_coordinates_match_ltr(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QComboBox

    src = _halves(tmp_path / "halves.png")
    # LTR reference (face detection off so only the drawn box is saved)
    monkeypatch.setattr(image_redact, "faces_available", lambda: False)
    ra, rb, ltr_boxes, _ = _redact_draw(src)
    aa, ab, ltr_annot = _annotate_draw(src)
    assert len(ltr_boxes) == 1

    from satsuma import i18n

    i18n.set_language("ar")
    monkeypatch.setattr(i18n, "_catalog", dict(FAKE_AR))
    qapp.setLayoutDirection(Qt.RightToLeft)
    try:
        # every dialog builds with translated text; internal values keep English keys
        b2 = _photo(tmp_path / "b.jpg")
        edit = image_edit.EditDialog([src, b2])
        assert "تحرير 2 صور" in edit.windowTitle() and edit.primary_btn.text() == "حفظ"
        assert edit.filter_combo.currentText() == "بلا"
        edit.filter_combo.setCurrentIndex(edit.filter_combo.findText("بني داكن"))
        assert edit.values()["filter"] == "Sepia"
        edit._render()
        edit.deleteLater()

        bg = image_background.BackgroundDialog([src])
        assert "إضافة خلفية" in bg.windowTitle()
        assert bg.aspect.currentText() == "الأصلي" and bg.values()["aspect"] == "Original"
        bg.aspect.setCurrentText("16:9")
        assert bg.values()["aspect"] == "16:9"
        bg._render()
        bg.deleteLater()

        red = image_redact.RedactDialog([src])
        assert "تنقيح" in red.windowTitle() and red.faces_btn.text() == "اكتشاف الوجوه"
        assert red.style_combo.currentData() == "solid"
        assert all(isinstance(w, QComboBox) for w in (red.style_combo,))
        red.deleteLater()

        ann = image_annotate.AnnotateDialog(src)
        assert ann.isRightToLeft() and ann.tool_buttons["arrow"].text() == "سهم"
        ann.deleteLater()

        # same mouse gesture -> same saved region, image not mirrored
        _, _, rtl_boxes, colour = _redact_draw(src, ra, rb)
        assert colour == "#ff0000"  # left half of the image is still on the left
        assert rtl_boxes == ltr_boxes
        _, _, rtl_annot = _annotate_draw(src, aa, ab)
        assert np.array_equal(rtl_annot, ltr_annot)
    finally:
        i18n.set_language("en")
        qapp.setLayoutDirection(Qt.LeftToRight)


def test_annotate_arabic_text_is_rtl_and_saved(arabic_rtl, tmp_path):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtTest import QTest

    src = _halves(tmp_path / "halves.png")
    dlg = image_annotate.AnnotateDialog(src)
    dlg.resize(1000, 700)
    dlg.show()
    QApplication.processEvents()
    v = dlg.view
    dlg.set_tool("text")
    click = QPointF(300, 100)
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, v.mapFromScene(click))
    item = v.scene_.focusItem()
    assert isinstance(item, image_annotate.TextItem)
    text = "مرحبا بالعالم"
    for ch in text:  # what an Arabic keyboard layout sends
        for t in (QEvent.KeyPress, QEvent.KeyRelease):
            QApplication.sendEvent(v.viewport(), QKeyEvent(t, 0, Qt.NoModifier, ch))
    assert item.toPlainText() == text
    assert item.is_rtl()
    # grows leftwards: the right edge stays near the click point
    right = item.x() + item.boundingRect().width()
    assert abs(right - click.x()) < 20 * v.unit and item.x() < click.x() - 20
    item.clearFocus()
    assert v.undo.count() == 1
    ctx = FakeCtx()
    out = Image.open(dlg.save(ctx)).convert("RGB")
    orig = np.asarray(Image.open(src).convert("RGB"), np.int16)
    ys, xs = np.nonzero(np.abs(np.asarray(out, np.int16) - orig).sum(2) > 60)
    assert len(xs) and xs.max() <= click.x() + 5 and xs.min() >= item.x() - 5
    dlg.close()
    dlg.deleteLater()


def test_output_names_translated_and_safe(monkeypatch, tmp_path):
    from satsuma import i18n

    src = tmp_path / ".odd name.v2.png"
    assert image_edit.output_stem("{name}-edited", src) == src.stem + "-edited"  # English unchanged
    monkeypatch.setattr(i18n, "_catalog", {"{name}-edited": "{name}-<مُعدّل>?",
                                           "{name}-redacted": "broken {nme}"})
    assert image_edit.output_stem("{name}-edited", src) == src.stem + "-مُعدّل"
    assert image_edit.output_stem("{name}-redacted", src) == src.stem + "-redacted"  # bad translation
    outs = run_option("image.edit", [_photo(tmp_path / "p.png")], {"adjustments": {}, "filter": "None"})
    assert outs[0].name == "p-مُعدّل.png"
