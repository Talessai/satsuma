"""Image "Redact" tool: hide faces / text / any region (solid, blur or pixelate).

Detection is fully local: OpenCV (Haar cascades on 4.x, YuNet if bundled) or Windows
FaceAnalysis for faces, Windows' built-in OCR (Windows.Media.Ocr via winrt) for text.
"""
from __future__ import annotations

import asyncio
import io
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .. import formats
from ..i18n import N_, tr
from ..registry import Option, add
from ..ui.common import STYLE, ToolDialog
from .image_background import ColorButton
from .image_edit import (
    file_caption, fit_width, load_image, load_preview, out_ext, output_stem, pil_to_qimage, save_image,
)

Box = tuple[int, int, int, int]            # x, y, w, h in pixels
RelBox = tuple[float, float, float, float]  # x, y, w, h as fractions of the image size

# display text (translated when shown) -> internal style id
STYLES = {N_("Solid colour"): "solid", N_("Blur"): "blur", N_("Pixelate"): "pixelate"}
BOX_TIPS = {
    "face": N_("Face: click to toggle, right-click to remove"),
    "text": N_("Text: click to toggle, right-click to remove"),
    "manual": N_("Box: click to toggle, right-click to remove"),
}


# ----------------------------------------------------------------------------
# detection
# ----------------------------------------------------------------------------

def _iou(a: Box, b: Box) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _merge(boxes: list[Box], thresh: float = 0.3) -> list[Box]:
    """Drop boxes overlapping a bigger kept box (simple NMS)."""
    kept: list[Box] = []
    for b in sorted(boxes, key=lambda b: b[2] * b[3], reverse=True):
        if all(_iou(b, k) < thresh for k in kept):
            kept.append(b)
    return kept


# --- face detection backends -------------------------------------------------
# OpenCV 4.x ships Haar cascades (cv2.CascadeClassifier + cv2.data.haarcascades);
# OpenCV 5 dropped them. Fallbacks: YuNet (cv2.FaceDetectorYN) when its ONNX model
# is bundled in satsuma/resources, then Windows' built-in FaceAnalysis via winrt.

_HAAR = ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")
_YUNET_NAMES = ("face_detection_yunet_2023mar.onnx", "face_detection_yunet.onnx")
_backend: str | None | bool = False  # False = not probed yet


def _haar_dir() -> Path | None:
    import cv2

    if not hasattr(cv2, "CascadeClassifier"):
        return None
    d = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
    return Path(d) if d and (Path(d) / _HAAR[0]).exists() else None


def _yunet_model() -> Path | None:
    import cv2

    if not hasattr(cv2, "FaceDetectorYN"):
        return None
    from ..paths import resource_dir

    for folder in (resource_dir(), resource_dir() / "models"):
        for name in _YUNET_NAMES:
            if (folder / name).exists():
                return folder / name
    return None


def _winrt_faces_ok() -> bool:
    try:
        from winrt.windows.media.faceanalysis import FaceDetector

        return bool(FaceDetector.is_supported)
    except Exception:
        return False


def face_backend() -> str | None:
    """'haar' | 'yunet' | 'windows' | None (no face detector available)."""
    global _backend
    if _backend is False:
        _backend = None
        try:
            if _haar_dir() is not None:
                _backend = "haar"
            elif _yunet_model() is not None:
                _backend = "yunet"
            elif _winrt_faces_ok():
                _backend = "windows"
        except Exception:
            _backend = None
    return _backend  # type: ignore[return-value]


def faces_available() -> bool:
    return face_backend() is not None


def _faces_haar(small: Image.Image) -> list[Box]:
    import cv2

    gray = cv2.equalizeHist(np.asarray(small.convert("L")))
    sw = gray.shape[1]
    min_side = max(20, int(min(gray.shape) * 0.03))
    found: list[Box] = []
    folder = _haar_dir()
    for name in _HAAR:
        casc = cv2.CascadeClassifier(str(folder / name))
        if casc.empty():
            continue
        for flipped in ((False, True) if "profile" in name else (False,)):
            src = cv2.flip(gray, 1) if flipped else gray
            rects = casc.detectMultiScale(src, scaleFactor=1.1, minNeighbors=5,
                                          minSize=(min_side, min_side))
            for (x, y, bw, bh) in (rects if len(rects) else []):
                if flipped:
                    x = sw - x - bw
                found.append((int(x), int(y), int(bw), int(bh)))
    return found


def _faces_yunet(small: Image.Image) -> list[Box]:
    import cv2

    bgr = np.ascontiguousarray(np.asarray(small.convert("RGB"))[..., ::-1])
    h, w = bgr.shape[:2]
    det = cv2.FaceDetectorYN.create(str(_yunet_model()), "", (w, h), 0.7, 0.3, 5000)
    det.setInputSize((w, h))
    _, faces = det.detect(bgr)
    return [] if faces is None else [(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces]


async def _software_bitmap(png: bytes, gray: bool = False):
    """PNG bytes -> Windows SoftwareBitmap (BGRA8, or Gray8 for face detection)."""
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapDecoder, BitmapPixelFormat
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    if gray:
        return await decoder.get_software_bitmap_converted_async(BitmapPixelFormat.GRAY8,
                                                                 BitmapAlphaMode.IGNORE)
    return await decoder.get_software_bitmap_converted_async(BitmapPixelFormat.BGRA8,
                                                             BitmapAlphaMode.PREMULTIPLIED)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def _winrt_faces(png: bytes) -> list[Box]:
    from winrt.windows.media.faceanalysis import FaceDetector

    detector = await FaceDetector.create_async()
    bitmap = await _software_bitmap(png, gray=True)
    faces = await detector.detect_faces_async(bitmap)
    return [(int(f.face_box.x), int(f.face_box.y), int(f.face_box.width), int(f.face_box.height))
            for f in faces]


def detect_faces(img: Image.Image, pad: float = 0.12) -> list[Box]:
    """Faces as padded pixel boxes; [] when no face detector is available."""
    backend = face_backend()
    if backend is None:
        return []
    w, h = img.size
    k = min(1.0, 1600 / max(w, h))
    small = img.convert("RGB")
    if k < 1:
        small = small.resize((max(1, int(w * k)), max(1, int(h * k))), Image.BILINEAR)
    if backend == "haar":
        found = _faces_haar(small)
    elif backend == "yunet":
        found = _faces_yunet(small)
    else:
        found = _run_in_thread(_winrt_faces, _png(small))
    out: list[Box] = []
    for x, y, bw, bh in _merge(found):
        px, py = bw * pad, bh * pad
        x0, y0 = max(0, (x - px) / k), max(0, (y - py) / k)
        x1, y1 = min(w, (x + bw + px) / k), min(h, (y + bh + py) / k)
        if x1 > x0 and y1 > y0:
            out.append((int(x0), int(y0), int(x1 - x0), int(y1 - y0)))
    return out


# --- text detection (Windows OCR) --------------------------------------------

def ocr_available() -> bool:
    """True when Windows OCR can be used (winrt installed and a language pack present)."""
    try:
        from winrt.windows.media.ocr import OcrEngine

        return OcrEngine.try_create_from_user_profile_languages() is not None
    except Exception:
        return False


async def _ocr_words(png: bytes) -> list[tuple[str, float, float, float, float]]:
    from winrt.windows.media.ocr import OcrEngine

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("no OCR language installed")
    bitmap = await _software_bitmap(png)
    result = await engine.recognize_async(bitmap)
    words = []
    for line in result.lines:
        for word in line.words:
            r = word.bounding_rect
            words.append((word.text, r.x, r.y, r.width, r.height))
    return words


def _run_in_thread(coro_fn, *args):
    """Run an async function on a fresh event loop in its own thread (keeps COM/Qt apart)."""
    box: dict = {}

    def target():
        try:
            box["result"] = asyncio.run(coro_fn(*args))
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(120)
    if "error" in box:
        raise box["error"]
    return box.get("result", [])


def detect_text(img: Image.Image, pad: float = 0.3) -> list[Box]:
    """Word boxes found by Windows OCR. Returns [] when OCR is unavailable."""
    if not ocr_available():
        return []
    from winrt.windows.media.ocr import OcrEngine

    w, h = img.size
    limit = min(int(OcrEngine.max_image_dimension or 4000), 4000)
    k = min(1.0, limit / max(w, h))
    work = img.convert("RGB")
    if k < 1:
        work = work.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    # small images OCR poorly; upscale tiny ones
    up = 1.0
    if max(work.size) < 800:
        up = 800 / max(work.size)
        work = work.resize((int(work.width * up), int(work.height * up)), Image.LANCZOS)
    try:
        words = _run_in_thread(_ocr_words, _png(work))
    except Exception:
        return []
    s = k * up
    out: list[Box] = []
    for _text, x, y, bw, bh in words:
        px, py = bh * pad, bh * pad
        x0, y0 = max(0.0, (x - px) / s), max(0.0, (y - py) / s)
        x1, y1 = min(w, (x + bw + px) / s), min(h, (y + bh + py) / s)
        if x1 > x0 and y1 > y0:
            out.append((int(x0), int(y0), int(round(x1 - x0)), int(round(y1 - y0))))
    return out


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------

def to_rel(box: Box, size: tuple[int, int]) -> RelBox:
    w, h = size
    return (box[0] / w, box[1] / h, box[2] / w, box[3] / h)


def to_abs(rel: RelBox, size: tuple[int, int]) -> Box:
    w, h = size
    x0 = max(0, min(w, round(rel[0] * w)))
    y0 = max(0, min(h, round(rel[1] * h)))
    x1 = max(0, min(w, round((rel[0] + rel[2]) * w)))
    y1 = max(0, min(h, round((rel[1] + rel[3]) * h)))
    return (x0, y0, x1 - x0, y1 - y0)


def apply_redaction(img: Image.Image, boxes: list[RelBox], style: str = "solid",
                    color: str | tuple = "#000000", strength: int = 50) -> Image.Image:
    """Cover each relative box. `strength` (1..100) scales blur radius / pixel block size."""
    out = img.copy()
    if out.mode not in ("RGB", "RGBA"):
        out = out.convert("RGB")
    k = max(1, min(100, int(strength))) / 50.0
    fill = QColor(color) if isinstance(color, str) else QColor(*color[:3])
    rgb = (fill.red(), fill.green(), fill.blue())
    draw = ImageDraw.Draw(out)
    for rel in boxes:
        x, y, bw, bh = to_abs(tuple(rel), out.size)  # type: ignore[arg-type]
        if bw < 1 or bh < 1:
            continue
        region = (x, y, x + bw, y + bh)
        if style == "blur":
            # blur a margin around the box so edges don't leak sharp pixels, then crop
            r = max(4.0, max(bw, bh) * 0.18 * k)
            m = int(r * 2)
            big = (max(0, x - m), max(0, y - m), min(out.width, x + bw + m), min(out.height, y + bh + m))
            patch = out.crop(big)
            for _ in range(2):
                patch = patch.filter(ImageFilter.GaussianBlur(r))
            out.paste(patch.crop((x - big[0], y - big[1], x - big[0] + bw, y - big[1] + bh)), (x, y))
        elif style == "pixelate":
            blocks = max(2, int(12 / k))
            cell = max(2, max(bw, bh) // blocks)
            patch = out.crop(region)
            small = patch.resize((max(1, bw // cell), max(1, bh // cell)), Image.BOX)
            out.paste(small.resize((bw, bh), Image.NEAREST), (x, y))
        else:
            draw.rectangle((x, y, x + bw - 1, y + bh - 1), fill=rgb + ((255,) if out.mode == "RGBA" else ()))
    return out


def run_redact(src: Path, ctx) -> Path | None:
    p = ctx.params or {}
    ctx.progress(0.1, tr("Loading"))
    img = load_image(src)
    per_file: dict = p.get("boxes", {})
    boxes = per_file.get(str(src))
    if boxes is None:  # file never opened in the dialog: auto-detect
        auto = p.get("auto", ["faces"])
        found: list[Box] = []
        if "faces" in auto:
            ctx.progress(0.3, tr("Finding faces"))
            found += detect_faces(img)
        if "text" in auto:
            ctx.progress(0.5, tr("Finding text"))
            found += detect_text(img)
        boxes = [to_rel(b, img.size) for b in found]
    ctx.check_cancel()
    ctx.progress(0.7, tr("Redacting"))
    out = apply_redaction(img, boxes, p.get("style", "solid"), p.get("color", "#000000"),
                          p.get("strength", 50))
    ext = out_ext(src)
    # EXIF can hold GPS/location and a full-size thumbnail of the unredacted photo: drop it
    path = ctx.output_path(src, ext, name=output_stem(N_("{name}-redacted"), src))
    return save_image(out, path, ext, strip_exif=True)


# ----------------------------------------------------------------------------
# dialog
# ----------------------------------------------------------------------------

def _found_text(found_tpl: str, added_tpl: str, n: int, added: int) -> str:
    return (found_tpl if added == n else added_tpl).format(n=n, added=added)


_ON = QColor("#FF8A1F")
_OFF = QColor(160, 160, 170)


class BoxItem(QGraphicsRectItem):
    """A redaction box on the preview; click toggles it on/off."""

    def __init__(self, rect: QRectF, kind: str, scale: float):
        super().__init__(rect)
        self.kind = kind          # face | text | manual
        self.enabled = True
        self._scale = scale
        self.setZValue(10)
        self.setAcceptHoverEvents(True)
        self.setToolTip(tr(BOX_TIPS.get(kind, BOX_TIPS["manual"])))
        self.refresh()

    def refresh(self) -> None:
        pen = QPen(_ON if self.enabled else _OFF, 2.0 * self._scale)
        pen.setCosmetic(False)
        if not self.enabled:
            pen.setStyle(Qt.DashLine)
        self.setPen(pen)
        fill = QColor(_ON if self.enabled else _OFF)
        fill.setAlpha(60 if self.enabled else 20)
        self.setBrush(QBrush(fill))


class RedactView(QGraphicsView):
    """Preview: click a box to toggle, drag on empty space to draw a box."""

    def __init__(self, dialog: "RedactDialog"):
        super().__init__()
        self.dlg = dialog
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setStyleSheet("background: #121215; border: none; border-radius: 8px;")
        self.setDragMode(QGraphicsView.NoDrag)
        self._start: QPointF | None = None
        self._rubber: QGraphicsRectItem | None = None

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self.fit()

    def fit(self) -> None:
        if self.scene() is not None:
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def mousePressEvent(self, e):  # noqa: N802
        pos = self.mapToScene(e.position().toPoint())
        item = next((i for i in self.items(e.position().toPoint()) if isinstance(i, BoxItem)), None)
        if e.button() == Qt.RightButton:
            if item is not None:
                self.dlg.remove_box(item)
            return
        if e.button() != Qt.LeftButton:
            return
        if item is not None:
            item.enabled = not item.enabled
            item.refresh()
            self.dlg.boxes_changed()
            return
        self._start = pos
        self._rubber = QGraphicsRectItem(QRectF(pos, pos))
        self._rubber.setPen(QPen(_ON, 1.5 * self.dlg.ui_scale, Qt.DashLine))
        self._rubber.setZValue(20)
        self.scene().addItem(self._rubber)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._start is not None and self._rubber is not None:
            pos = self.mapToScene(e.position().toPoint())
            self._rubber.setRect(QRectF(self._start, pos).normalized())

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._start is None or self._rubber is None:
            return
        rect = self._rubber.rect().intersected(self.scene().sceneRect())
        self.scene().removeItem(self._rubber)
        self._rubber, self._start = None, None
        if rect.width() >= 4 and rect.height() >= 4:
            self.dlg.add_box(rect, "manual")


class RedactDialog(ToolDialog):
    def __init__(self, paths: list[Path]):
        n = len(paths)
        hint = (tr("Click a box to toggle it, drag to add one, right-click to remove. "
                   "Unopened images are auto-redacted (faces).") if n > 1 else
                tr("Click a box to toggle it, drag to add one, right-click to remove."))
        super().__init__(tr("Redact"), hint, primary=tr("Save"))
        self.setStyleSheet(STYLE)
        self.setSizeGripEnabled(True)
        self.resize(1180, 760)
        self.paths = paths
        self.index = 0
        self._images: dict[int, Image.Image] = {}     # preview-size images
        self._boxes: dict[int, list[BoxItem]] = {}
        self._items_state: dict[int, list[tuple[RelBox, str, bool]]] = {}
        self._used_text = False
        self.ui_scale = 1.0

        split = QHBoxLayout()
        self.body.addLayout(split, 1)
        left = QVBoxLayout()
        self.scene = QGraphicsScene(self)
        self.view = RedactView(self)
        self.view.setScene(self.scene)
        left.addWidget(self.view, 1)
        nav = QHBoxLayout()
        self.prev_btn, self.next_btn = QPushButton(tr("‹ Prev")), QPushButton(tr("Next ›"))
        self.file_lbl = QLabel("", objectName="subtitle")
        self.prev_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn.clicked.connect(lambda: self._go(1))
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.file_lbl, 1)
        nav.addWidget(self.next_btn)
        left.addLayout(nav)
        split.addLayout(left, 1)

        side = QWidget()  # width fitted to the (translated) button texts below
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        self.faces_btn = QPushButton(tr("Detect faces"))
        self.faces_btn.clicked.connect(self.detect_faces)
        self.faces_ok = faces_available()
        if not self.faces_ok:
            self.faces_btn.setEnabled(False)
            self.faces_btn.setToolTip(tr("No local face detector available (needs OpenCV 4.x Haar "
                                         "cascades, a bundled YuNet model, or Windows FaceAnalysis)."))
        self.text_btn = QPushButton(tr("Detect text"))
        self.text_btn.clicked.connect(self.detect_text)
        self.ocr_ok = ocr_available()
        if not self.ocr_ok:
            self.text_btn.setEnabled(False)
            self.text_btn.setToolTip(tr("Windows OCR isn't available (needs Windows 10+ with an OCR "
                                        "language pack, and the winrt packages)."))
        self.clear_btn = QPushButton(tr("Remove all boxes"))
        self.clear_btn.clicked.connect(self.clear_boxes)
        self.status = QLabel("", objectName="subtitle", wordWrap=True)
        for wdg in (self.faces_btn, self.text_btn, self.clear_btn, self.status):
            sv.addWidget(wdg)
        sv.addSpacing(8)
        sv.addWidget(QLabel(tr("Cover style"), objectName="section"))
        self.style_combo = QComboBox()
        for text, data in STYLES.items():
            self.style_combo.addItem(tr(text), data)
        self.style_combo.currentIndexChanged.connect(self._style_changed)
        sv.addWidget(self.style_combo)
        self.color_btn = ColorButton("#000000", self._refresh_result)
        sv.addWidget(self.color_btn)
        self.strength_lbl = QLabel(tr("Strength"), objectName="subtitle")
        sv.addWidget(self.strength_lbl)
        self.strength = QSlider(Qt.Horizontal)
        self.strength.setRange(10, 100)
        self.strength.setValue(50)
        self.strength.valueChanged.connect(self._refresh_result)
        sv.addWidget(self.strength)
        self.show_result = QCheckBox(tr("Preview result"))
        self.show_result.toggled.connect(self._refresh_result)
        sv.addWidget(self.show_result)
        sv.addStretch(1)
        split.addWidget(side)
        fit_width(side, 300)

        self._bg_item: QGraphicsPixmapItem | None = None
        self._style_changed()
        self._show_file()

    # --- files ---
    def _image(self) -> Image.Image:
        if self.index not in self._images:
            try:
                self._images[self.index] = load_preview(self.paths[self.index], 1400)
            except Exception:
                self._images[self.index] = Image.new("RGB", (400, 300), "#333")
        return self._images[self.index]

    def _go(self, step: int) -> None:
        self._save_state()
        self.index = max(0, min(len(self.paths) - 1, self.index + step))
        self._show_file()

    def _save_state(self) -> None:
        if self.index in self._boxes:
            size = self._image().size
            self._items_state[self.index] = [
                (to_rel(self._rect_box(b), size), b.kind, b.enabled) for b in self._boxes[self.index]
            ]

    def _show_file(self) -> None:
        n = len(self.paths)
        for b in (self.prev_btn, self.next_btn):
            b.setVisible(n > 1)
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < n - 1)
        self.file_lbl.setText(file_caption(self.index, n, self.paths[self.index].name))
        img = self._image()
        self.scene.clear()
        self._bg_item = self.scene.addPixmap(QPixmap.fromImage(pil_to_qimage(img)))
        self.scene.setSceneRect(QRectF(0, 0, img.width, img.height))
        self.ui_scale = max(1.0, max(img.size) / 900)
        first = self.index not in self._items_state
        self._boxes[self.index] = []
        for rel, kind, enabled in self._items_state.get(self.index, []):
            b = self._add_item(self._qrect(to_abs(rel, img.size)), kind)
            b.enabled = enabled
            b.refresh()
        self.view.fit()
        if first:
            self._items_state[self.index] = []
            if self.faces_ok:
                self.detect_faces()
        self._refresh_result()

    # --- boxes ---
    @staticmethod
    def _qrect(b: Box) -> QRectF:
        return QRectF(b[0], b[1], b[2], b[3])

    @staticmethod
    def _rect_box(item: BoxItem) -> Box:
        r = item.mapRectToScene(item.rect())
        return (int(r.x()), int(r.y()), int(r.width()), int(r.height()))

    def _add_item(self, rect: QRectF, kind: str) -> BoxItem:
        item = BoxItem(rect, kind, self.ui_scale)
        self.scene.addItem(item)
        self._boxes.setdefault(self.index, []).append(item)
        return item

    def add_box(self, rect: QRectF, kind: str) -> None:
        self._add_item(rect, kind)
        self.boxes_changed()

    def remove_box(self, item: BoxItem) -> None:
        self.scene.removeItem(item)
        self._boxes[self.index].remove(item)
        self.boxes_changed()

    def clear_boxes(self) -> None:
        for item in list(self._boxes.get(self.index, [])):
            self.scene.removeItem(item)
        self._boxes[self.index] = []
        self.boxes_changed()

    def _add_detected(self, boxes: list[Box], kind: str) -> int:
        existing = [self._rect_box(b) for b in self._boxes.get(self.index, [])]
        added = 0
        for b in boxes:
            if all(_iou(b, e) < 0.5 for e in existing):
                self._add_item(self._qrect(b), kind)
                existing.append(b)
                added += 1
        return added

    def detect_faces(self) -> None:
        if not self.faces_ok:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            found = detect_faces(self._image())
        finally:
            QApplication.restoreOverrideCursor()
        added = self._add_detected(found, "face")
        self.status.setText(_found_text(tr("Found {n} face(s)"), tr("Found {n} face(s), added {added}"),
                                        len(found), added))
        self.boxes_changed()

    def detect_text(self) -> None:
        if not self.ocr_ok:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            found = detect_text(self._image())
        finally:
            QApplication.restoreOverrideCursor()
        self._used_text = True
        added = self._add_detected(found, "text")
        self.status.setText(_found_text(tr("Found {n} word(s)"), tr("Found {n} word(s), added {added}"),
                                        len(found), added))
        self.boxes_changed()

    def boxes_changed(self) -> None:
        self._save_state()
        self._refresh_result()

    # --- result preview ---
    def _style_changed(self, *_):
        style = self.style_combo.currentData()
        self.color_btn.setVisible(style == "solid")
        self.strength.setVisible(style != "solid")
        self.strength_lbl.setVisible(style != "solid")
        self._refresh_result()

    def _refresh_result(self, *_):
        if self._bg_item is None:
            return
        img = self._image()
        if self.show_result.isChecked():
            v = self.values()
            rel = v["boxes"].get(str(self.paths[self.index]), [])
            img = apply_redaction(img, rel, v["style"], v["color"], v["strength"])
        self._bg_item.setPixmap(QPixmap.fromImage(pil_to_qimage(img)))
        for b in self._boxes.get(self.index, []):
            b.setVisible(not self.show_result.isChecked() or not b.enabled)

    def values(self) -> dict:
        self._save_state()
        boxes = {str(self.paths[i]): [rel for rel, _k, on in state if on]
                 for i, state in self._items_state.items()}
        return {
            "boxes": boxes,
            "auto": ["faces"] + (["text"] if self._used_text and self.ocr_ok else []),
            "style": self.style_combo.currentData(),
            "color": self.color_btn.color(),
            "strength": self.strength.value(),
        }


def redact_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = RedactDialog([Path(p) for p in paths])
    return dlg.values() if dlg.exec() else None


add(Option(
    id="image.redact", label="Redact", mode="tools", run=run_redact, dialog=redact_dialog,
    exts=frozenset(formats.IMAGE_IN), family="image", order=50,
    description="Hide faces, text or any region with a solid box, blur or pixelation",
))
