"""Image "Annotate" tool: arrows, shapes, pen, text, numbered callouts, blur boxes.

The scene is in full-resolution image pixels, so saving renders the scene 1:1.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageFilter
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut,
    QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QButtonGroup, QGraphicsEllipseItem, QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem,
    QGraphicsScene, QGraphicsTextItem, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider,
    QStyle,
)

from .. import formats
from ..i18n import N_, tr
from ..registry import Option, UserError, add
from ..ui.common import STYLE, ToolDialog
from .image_edit import load_image, out_ext, output_stem, pil_to_qimage, qimage_to_pil, save_image

TOOLS: list[tuple[str, str, str]] = [
    # id, label, tooltip (label/tooltip are translated when shown)
    ("select", N_("Select"), N_("Select, move (drag) and delete (Del) annotations")),
    ("arrow", N_("Arrow"), N_("Drag to draw an arrow")),
    ("line", N_("Line"), N_("Drag to draw a line")),
    ("rect", N_("Rect"), N_("Drag to draw a rectangle")),
    ("ellipse", N_("Ellipse"), N_("Drag to draw an ellipse")),
    ("pen", N_("Pen"), N_("Freehand drawing")),
    ("highlighter", N_("Highlight"), N_("Translucent marker")),
    ("text", N_("Text"), N_("Click to type text")),
    ("callout", "①②③", N_("Click to drop numbered callouts")),  # symbols: not translated
    ("blur", N_("Blur"), N_("Drag to blur a region")),
]
PALETTE = ["#FF3B30", "#FF9500", "#FFCC00", "#34C759", "#0A84FF", "#AF52DE", "#000000", "#FFFFFF"]


def _sel_pen(unit: float) -> QPen:
    return QPen(QColor("#0A84FF"), max(1.0, unit), Qt.DashLine)


# ----------------------------------------------------------------------------
# items
# ----------------------------------------------------------------------------

class LineItem(QGraphicsPathItem):
    """Straight line, optionally with an arrow head at the end."""

    def __init__(self, p1: QPointF, pen: QPen, arrow: bool):
        super().__init__()
        self.arrow = arrow
        self.p1, self.p2 = QPointF(p1), QPointF(p1)
        self.setPen(pen)
        self.rebuild()

    def set_end(self, p2: QPointF) -> None:
        self.p2 = QPointF(p2)
        self.rebuild()

    def length(self) -> float:
        return QLineF(self.p1, self.p2).length()

    def rebuild(self) -> None:
        pen = self.pen()
        path = QPainterPath(self.p1)
        if not self.arrow:
            path.lineTo(self.p2)
            self.setBrush(Qt.NoBrush)
            self.setPath(path)
            return
        line = QLineF(self.p1, self.p2)
        head = max(pen.widthF() * 4.0, 10.0)
        ang = math.atan2(line.dy(), line.dx())
        # shorten the shaft so the round cap doesn't poke through the tip
        tip = self.p2
        base = QPointF(tip.x() - head * 0.8 * math.cos(ang), tip.y() - head * 0.8 * math.sin(ang))
        path.lineTo(base if line.length() > head else tip)
        left = QPointF(tip.x() - head * math.cos(ang - 0.45), tip.y() - head * math.sin(ang - 0.45))
        right = QPointF(tip.x() - head * math.cos(ang + 0.45), tip.y() - head * math.sin(ang + 0.45))
        path.moveTo(tip)
        path.lineTo(left)
        path.lineTo(right)
        path.closeSubpath()
        self.setBrush(QBrush(pen.color()))
        self.setPath(path)

    def set_color(self, c: QColor) -> None:
        pen = self.pen()
        pen.setColor(c)
        self.setPen(pen)
        self.rebuild()

    def set_width(self, w: float) -> None:
        pen = self.pen()
        pen.setWidthF(w)
        self.setPen(pen)
        self.rebuild()


class StrokeItem(QGraphicsPathItem):
    """Freehand pen or highlighter stroke."""

    def __init__(self, start: QPointF, pen: QPen):
        super().__init__()
        self._path = QPainterPath(start)
        self._path.lineTo(start + QPointF(0.01, 0.01))
        self.setPen(pen)
        self.setPath(self._path)

    def extend(self, p: QPointF) -> None:
        self._path.lineTo(p)
        self.setPath(self._path)


class CalloutItem(QGraphicsItem):
    """Filled circle with a number in it."""

    def __init__(self, number: int, color: QColor, radius: float):
        super().__init__()
        self.number = number
        self.color = QColor(color)
        self.radius = radius

    def boundingRect(self) -> QRectF:  # noqa: N802
        r = self.radius * 1.15
        return QRectF(-r, -r, 2 * r, 2 * r)

    def paint(self, p: QPainter, option, widget=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        r = self.radius
        p.setPen(QPen(QColor("#ffffff"), r * 0.14))
        p.setBrush(self.color)
        p.drawEllipse(QPointF(0, 0), r, r)
        f = QFont("Segoe UI")
        f.setBold(True)
        f.setPixelSize(max(1, int(r * (1.1 if self.number < 10 else 0.85))))
        p.setFont(f)
        p.setPen(QColor("#111111") if self.color.lightness() > 160 else QColor("#ffffff"))
        p.drawText(QRectF(-r, -r, 2 * r, 2 * r), Qt.AlignCenter, str(self.number))
        if option.state & QStyle.State_Selected:
            p.setPen(_sel_pen(r / 12))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.boundingRect())


class BlurItem(QGraphicsRectItem):
    """Rectangle that shows a blurred copy of the image underneath it."""

    def __init__(self, rect: QRectF, source: QPixmap, unit: float):
        super().__init__(rect)
        self.source = source
        self.unit = unit
        self.setPen(Qt.NoPen)

    def paint(self, p: QPainter, option, widget=None) -> None:
        src = self.mapRectToScene(self.rect()).intersected(QRectF(self.source.rect()))
        if src.isValid():
            p.drawPixmap(self.mapRectFromScene(src), self.source, src)
        if option.state & QStyle.State_Selected:
            p.setPen(_sel_pen(self.unit))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.rect())


class TextItem(QGraphicsTextItem):
    """Editable text; double-click to edit again."""

    def __init__(self, on_done):
        super().__init__()
        self._on_done = on_done
        self.pushed = False
        self._width = 0.0
        # the text keeps its own direction (Arabic is laid out right-to-left whatever the
        # UI language); while typing RTL text, grow leftwards from the click point
        self.document().contentsChanged.connect(self._keep_anchor)

    def is_rtl(self) -> bool:
        return self.document().firstBlock().textDirection() == Qt.RightToLeft

    def _keep_anchor(self) -> None:
        w = self.boundingRect().width()
        if self.textInteractionFlags() & Qt.TextEditorInteraction and self.is_rtl():
            self.setX(self.x() - (w - self._width))
        self._width = w

    def start_edit(self) -> None:
        self._width = self.boundingRect().width()
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus(Qt.MouseFocusReason)

    def focusOutEvent(self, e):  # noqa: N802
        super().focusOutEvent(e)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        c = self.textCursor()
        c.clearSelection()
        self.setTextCursor(c)
        self._on_done(self)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self.start_edit()
        super().mouseDoubleClickEvent(e)

    def set_color(self, c: QColor) -> None:
        self.setDefaultTextColor(c)


def item_color(item: QGraphicsItem) -> QColor | None:
    if isinstance(item, TextItem):
        return item.defaultTextColor()
    if isinstance(item, CalloutItem):
        return item.color
    if isinstance(item, (QGraphicsPathItem, QGraphicsRectItem, QGraphicsEllipseItem)) \
            and not isinstance(item, BlurItem):
        return item.pen().color()
    return None


# ----------------------------------------------------------------------------
# undo commands
# ----------------------------------------------------------------------------

class AddCmd(QUndoCommand):
    def __init__(self, scene: QGraphicsScene, item: QGraphicsItem, text: str = "Add"):
        super().__init__(text)
        self.scene, self.item = scene, item

    def redo(self) -> None:
        if self.item.scene() is not self.scene:
            self.scene.addItem(self.item)

    def undo(self) -> None:
        if self.item.scene() is self.scene:
            self.scene.removeItem(self.item)


class RemoveCmd(QUndoCommand):
    def __init__(self, scene: QGraphicsScene, items: list[QGraphicsItem]):
        super().__init__("Delete")
        self.scene, self.items = scene, list(items)

    def redo(self) -> None:
        for it in self.items:
            if it.scene() is self.scene:
                self.scene.removeItem(it)

    def undo(self) -> None:
        for it in self.items:
            if it.scene() is not self.scene:
                self.scene.addItem(it)


class MoveCmd(QUndoCommand):
    def __init__(self, moves: list[tuple[QGraphicsItem, QPointF, QPointF]]):
        super().__init__("Move")
        self.moves = moves

    def redo(self) -> None:
        for it, _old, new in self.moves:
            it.setPos(new)

    def undo(self) -> None:
        for it, old, _new in self.moves:
            it.setPos(old)


class StyleCmd(QUndoCommand):
    """Recolour / re-size selected items; `apply(item, value)` does the change."""

    def __init__(self, changes: list[tuple[QGraphicsItem, object, object]], apply, text: str):
        super().__init__(text)
        self.changes, self.apply = changes, apply

    def redo(self) -> None:
        for it, _old, new in self.changes:
            self.apply(it, new)

    def undo(self) -> None:
        for it, old, _new in self.changes:
            self.apply(it, old)


# ----------------------------------------------------------------------------
# canvas
# ----------------------------------------------------------------------------

class AnnotateView(QGraphicsView):
    def __init__(self, image: Image.Image):
        super().__init__()
        self.image = image
        self.setStyleSheet("background: #121215; border: none; border-radius: 8px;")
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform
                            | QPainter.TextAntialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        w, h = image.size
        self.pixmap = QPixmap.fromImage(pil_to_qimage(image))
        bg = self.scene_.addPixmap(self.pixmap)
        bg.setZValue(-1000)
        bg.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self.scene_.setSceneRect(QRectF(0, 0, w, h))
        self.unit = max(1.0, max(w, h) / 1000.0)
        self.undo = QUndoStack(self)
        self.tool = "arrow"
        self.color = QColor(PALETTE[0])
        self.width_value = 4
        self._blurred: QPixmap | None = None
        self._drawing: QGraphicsItem | None = None
        self._start = QPointF()
        self._press_pos: dict[QGraphicsItem, QPointF] = {}
        self._auto_fit = True
        self._z = 1.0

    # --- helpers ---
    @property
    def stroke(self) -> float:
        return self.width_value * self.unit

    def pen(self, width: float | None = None, color: QColor | None = None) -> QPen:
        pen = QPen(color or self.color, width or self.stroke)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def blurred_pixmap(self) -> QPixmap:
        if self._blurred is None:
            img = self.image.convert("RGB")
            w, h = img.size
            k = 4
            small = img.resize((max(1, w // k), max(1, h // k)), Image.BILINEAR)
            small = small.filter(ImageFilter.GaussianBlur(max(2.0, max(w, h) / 320)))
            self._blurred = QPixmap.fromImage(pil_to_qimage(small.resize((w, h), Image.BILINEAR)))
        return self._blurred

    def annotations(self) -> list[QGraphicsItem]:
        return [i for i in self.scene_.items() if i.flags() & QGraphicsItem.ItemIsSelectable]

    def next_callout(self) -> int:
        nums = [i.number for i in self.scene_.items() if isinstance(i, CalloutItem)]
        return max(nums, default=0) + 1

    def _prepare(self, item: QGraphicsItem, z: float | None = None) -> QGraphicsItem:
        item.setFlag(QGraphicsItem.ItemIsSelectable, True)
        item.setFlag(QGraphicsItem.ItemIsMovable, True)
        if z is None:
            self._z += 1
            z = self._z
        item.setZValue(z)
        return item

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.setDragMode(QGraphicsView.RubberBandDrag if tool == "select" else QGraphicsView.NoDrag)
        self.viewport().setCursor(Qt.ArrowCursor if tool == "select" else
                                  Qt.IBeamCursor if tool == "text" else Qt.CrossCursor)
        if tool != "select":
            self.scene_.clearSelection()

    # --- zoom ---
    def fit(self) -> None:
        self._auto_fit = True
        self.fitInView(self.scene_.sceneRect(), Qt.KeepAspectRatio)

    def actual_size(self) -> None:
        self._auto_fit = False
        self.resetTransform()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self._auto_fit:
            self.fitInView(self.scene_.sceneRect(), Qt.KeepAspectRatio)

    def wheelEvent(self, e):  # noqa: N802
        if e.modifiers() & Qt.ControlModifier:
            self._auto_fit = False
            f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
        else:
            super().wheelEvent(e)

    # --- mouse ---
    def mousePressEvent(self, e):  # noqa: N802
        pos = self.mapToScene(e.position().toPoint())
        if self.tool == "select" or e.button() != Qt.LeftButton:
            super().mousePressEvent(e)
            self._press_pos = {i: QPointF(i.pos()) for i in self.scene_.selectedItems()}
            return
        focus = self.scene_.focusItem()
        if isinstance(focus, TextItem):
            focus.clearFocus()  # finishes editing (may remove an empty text)
            if self.tool == "text":
                return
        self._start = pos
        t = self.tool
        if t in ("arrow", "line"):
            item = LineItem(pos, self.pen(), arrow=(t == "arrow"))
        elif t == "rect":
            item = QGraphicsRectItem(QRectF(pos, pos))
            pen = self.pen()
            pen.setJoinStyle(Qt.MiterJoin)
            item.setPen(pen)
        elif t == "ellipse":
            item = QGraphicsEllipseItem(QRectF(pos, pos))
            item.setPen(self.pen())
        elif t == "pen":
            item = StrokeItem(pos, self.pen())
        elif t == "highlighter":
            c = QColor(self.color)
            c.setAlpha(110)
            pen = self.pen(self.stroke * 4, c)
            pen.setCapStyle(Qt.SquareCap)
            item = StrokeItem(pos, pen)
        elif t == "blur":
            item = BlurItem(QRectF(pos, pos), self.blurred_pixmap(), self.unit)
        elif t == "callout":
            item = CalloutItem(self.next_callout(), self.color, max(12.0, 5 * self.stroke))
            item.setPos(pos)
            self._prepare(item)
            self.undo.push(AddCmd(self.scene_, item, "Callout"))
            return
        elif t == "text":
            item = TextItem(self.text_done)
            item.setDefaultTextColor(self.color)
            f = QFont("Segoe UI")
            f.setBold(True)
            f.setPixelSize(max(8, int((14 + self.width_value * 3) * self.unit)))
            item.setFont(f)
            item.setPos(pos)
            self._prepare(item)
            self.scene_.addItem(item)
            item.start_edit()
            return
        else:
            return
        self._prepare(item, z=0.5 if t == "highlighter" else None)
        self.scene_.addItem(item)
        self._drawing = item

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drawing is None:
            super().mouseMoveEvent(e)
            return
        pos = self.mapToScene(e.position().toPoint())
        it = self._drawing
        if isinstance(it, LineItem):
            if e.modifiers() & Qt.ShiftModifier:  # snap to 45 degrees
                d = pos - self._start
                ang = round(math.atan2(d.y(), d.x()) / (math.pi / 4)) * (math.pi / 4)
                r = math.hypot(d.x(), d.y())
                pos = self._start + QPointF(r * math.cos(ang), r * math.sin(ang))
            it.set_end(pos)
        elif isinstance(it, StrokeItem):
            it.extend(pos)
        elif isinstance(it, (QGraphicsRectItem, QGraphicsEllipseItem)):
            r = QRectF(self._start, pos).normalized()
            if e.modifiers() & Qt.ShiftModifier:
                s = max(r.width(), r.height())
                r = QRectF(self._start, self._start + QPointF(
                    math.copysign(s, pos.x() - self._start.x()), math.copysign(s, pos.y() - self._start.y()))
                ).normalized()
            it.setRect(r)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drawing is None:
            super().mouseReleaseEvent(e)
            if self.tool == "select" and self._press_pos:
                moves = [(i, old, QPointF(i.pos())) for i, old in self._press_pos.items()
                         if i.pos() != old and i.scene() is self.scene_]
                if moves:
                    self.undo.push(MoveCmd(moves))  # redo() re-applies the same positions
                self._press_pos = {}
            return
        it, self._drawing = self._drawing, None
        tiny = 3 * self.unit
        if isinstance(it, LineItem):
            small = it.length() < tiny
        elif isinstance(it, StrokeItem):
            small = False
        else:
            r = it.rect()
            small = r.width() < tiny or r.height() < tiny
        if small:
            self.scene_.removeItem(it)
        else:
            self.undo.push(AddCmd(self.scene_, it, self.tool.title()))

    def text_done(self, item: TextItem) -> None:
        empty = not item.toPlainText().strip()
        if not item.pushed:
            if empty:
                if item.scene() is not None:
                    self.scene_.removeItem(item)
            else:
                item.pushed = True
                self.undo.push(AddCmd(self.scene_, item, "Text"))
        elif empty and item.scene() is not None:
            self.undo.push(RemoveCmd(self.scene_, [item]))

    def keyPressEvent(self, e):  # noqa: N802
        focus = self.scene_.focusItem()
        editing = isinstance(focus, TextItem) and focus.textInteractionFlags() & Qt.TextEditorInteraction
        if not editing and e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
            return
        if not editing and e.key() == Qt.Key_Escape:
            self.scene_.clearSelection()
            return
        super().keyPressEvent(e)

    def delete_selected(self) -> None:
        sel = self.scene_.selectedItems()
        if sel:
            self.undo.push(RemoveCmd(self.scene_, sel))

    # --- styling the selection ---
    def apply_color(self, c: QColor) -> None:
        self.color = QColor(c)
        changes = [(i, item_color(i), QColor(c)) for i in self.scene_.selectedItems()
                   if item_color(i) is not None]
        if changes:
            self.undo.push(StyleCmd(changes, _set_item_color, "Colour"))

    def apply_width(self, value: int) -> None:
        self.width_value = value
        changes = []
        for i in self.scene_.selectedItems():
            if isinstance(i, (QGraphicsPathItem, QGraphicsRectItem, QGraphicsEllipseItem)) \
                    and not isinstance(i, BlurItem):
                hl = i.pen().color().alpha() < 255
                changes.append((i, i.pen().widthF(), self.stroke * (4 if hl else 1)))
        if changes:
            self.undo.push(StyleCmd(changes, _set_item_width, "Width"))

    # --- output ---
    def render_image(self) -> QImage:
        focus = self.scene_.focusItem()
        if focus is not None:
            focus.clearFocus()
        self.scene_.clearSelection()
        w, h = self.image.size
        out = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.scene_.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
        p.end()
        return out


def _set_item_color(item: QGraphicsItem, c: QColor) -> None:
    c = QColor(c)
    if isinstance(item, TextItem):
        item.setDefaultTextColor(c)
    elif isinstance(item, CalloutItem):
        item.color = c
        item.update()
    elif isinstance(item, LineItem):
        item.set_color(c)
    else:
        pen = item.pen()
        if pen.color().alpha() < 255:  # keep highlighter translucency
            c.setAlpha(pen.color().alpha())
        pen.setColor(c)
        item.setPen(pen)


def _set_item_width(item: QGraphicsItem, w: float) -> None:
    if isinstance(item, LineItem):
        item.set_width(w)
    else:
        pen = item.pen()
        pen.setWidthF(w)
        item.setPen(pen)


# ----------------------------------------------------------------------------
# dialog
# ----------------------------------------------------------------------------

class AnnotateDialog(ToolDialog):
    def __init__(self, path: Path):
        super().__init__(tr("Annotate"), path.name, primary=tr("Save"))
        self.setStyleSheet(STYLE)
        self.setSizeGripEnabled(True)
        self.resize(1280, 840)
        self.path = path
        self.source = load_image(path)
        # Enter must not save while typing / Escape handled by the canvas first
        self.primary_btn.setDefault(False)
        self.primary_btn.setAutoDefault(False)
        self.cancel_btn.setAutoDefault(False)

        self.view = AnnotateView(self.source)

        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.tool_group = QButtonGroup(self)
        self.tool_buttons: dict[str, QPushButton] = {}
        for tid, label, tip in TOOLS:
            b = QPushButton(tr(label), checkable=True, toolTip=tr(tip))
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, t=tid: self.set_tool(t))
            self.tool_group.addButton(b)
            self.tool_buttons[tid] = b
            tools.addWidget(b)
        tools.addStretch(1)
        self.body.addLayout(tools)

        opts = QHBoxLayout()
        opts.setSpacing(6)
        self.color_group = QButtonGroup(self)
        for c in PALETTE:
            b = QPushButton(checkable=True, toolTip=c)
            b.setAutoDefault(False)
            b.setFixedSize(26, 26)
            b.setStyleSheet(f"QPushButton {{ background: {c}; border: 2px solid #3a3a43; border-radius: 13px; }}"
                            f"QPushButton:checked {{ background: {c}; border: 3px solid #ececf1; }}")
            b.clicked.connect(lambda _=False, col=c: self.view.apply_color(QColor(col)))
            self.color_group.addButton(b)
            opts.addWidget(b)
        self.color_group.buttons()[0].setChecked(True)
        opts.addSpacing(12)
        opts.addWidget(QLabel(tr("Width")))
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(1, 20)
        self.width_slider.setValue(self.view.width_value)
        self.width_slider.setFixedWidth(140)
        self.width_slider.valueChanged.connect(self.view.apply_width)
        opts.addWidget(self.width_slider)
        opts.addStretch(1)
        # key names in tooltips (Ctrl+Z, Del) stay as printed on the keyboard
        for text, fn, tip in ((tr("Undo"), self.view.undo.undo, "Ctrl+Z"),
                              (tr("Redo"), self.view.undo.redo, "Ctrl+Y"),
                              (tr("Delete"), self.view.delete_selected, "Del"),
                              (tr("Fit"), self.view.fit, tr("Zoom to fit")),
                              ("100%", self.view.actual_size, tr("Actual pixels (Ctrl+wheel zooms)"))):
            b = QPushButton(text, toolTip=tip)
            b.setAutoDefault(False)
            b.clicked.connect(fn)
            opts.addWidget(b)
        self.body.addLayout(opts)
        self.body.addWidget(self.view, 1)

        for seq, fn in ((QKeySequence.Undo, self.view.undo.undo), (QKeySequence.Redo, self.view.undo.redo),
                        (QKeySequence("Ctrl+Y"), self.view.undo.redo)):
            QShortcut(seq, self, fn)
        self.set_tool("arrow")

    def set_tool(self, tool: str) -> None:
        self.tool_buttons[tool].setChecked(True)
        self.view.set_tool(tool)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            return  # never save by accident
        super().keyPressEvent(e)

    def render_pil(self) -> Image.Image:
        img = qimage_to_pil(self.view.render_image())
        return img if self.source.mode == "RGBA" else img.convert("RGB")

    def save(self, ctx) -> Path:
        ext = out_ext(self.path)
        out = ctx.output_path(self.path, ext, name=output_stem(N_("{name}-annotated"), self.path))
        return save_image(self.render_pil(), out, ext)


def annotate_dialog(paths: list[Path], ctx) -> dict | None:
    dlg = AnnotateDialog(Path(paths[0]))
    if not dlg.exec():
        return None
    try:
        out = dlg.save(ctx)
    except UserError:
        raise
    except Exception as e:
        raise UserError(tr("Couldn't save the annotated image: {error}").format(error=e)) from e
    return {"done": True, "outputs": [out]}


add(Option(
    id="image.annotate", label="Annotate", mode="tools", dialog=annotate_dialog,
    exts=frozenset(formats.IMAGE_IN), max_files=1, family="image", order=30,
    description="Draw arrows, shapes, text and numbered callouts on an image",
))
