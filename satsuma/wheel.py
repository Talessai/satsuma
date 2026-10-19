"""The radial wheel overlay.

Shown at the cursor while the user drags files. It is a translucent, topmost,
non-activating window that accepts OLE drops: when the drag enters it we read
the dragged paths, fill the wedges with options that apply, highlight the
wedge under the cursor and run the option the files are dropped on.

It also works without a drag (Send To / drop zone): `show_for(paths)` presets
the files and a click on a wedge runs it.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import formats, registry
from .i18n import N_, current, is_rtl, tr
from .config import ACCENT, KEY_CHOICES, KEY_LABELS, MODE_TITLES, MODES, settings

RING_CAPACITY = 9      # wedges per ring before a second ring is added
INNER_R = 56           # radius of the centre "cancel" hole
RING_W = 92            # thickness of each ring
MODE_SHORT = {"convert": N_("Convert"), "tools": N_("Tools"), "actions": N_("Actions")}
MARGIN = 46            # room around the outer ring for the mode hint bar


class Wheel(QWidget):
    chosen = Signal(object, list)   # Option, [Path]

    def __init__(self, focusable: bool = False):
        """focusable=False for the drag wheel (must never steal focus mid-drag);
        True for the click wheel opened from Send To / the drop zone."""
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
        if not focusable:
            flags |= Qt.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        if not focusable:
            self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.mode = "convert"
        self.paths: list[Path] = []
        self.options: list[registry.Option] = []
        self.hover = -1
        self.click_mode = False   # True when opened without a drag (click wedges)
        self._hide_timer = QTimer(self, singleShot=True, interval=350, timeout=self._hide_if_idle)
        self._dropping = False
        self._rings = 1
        self._resize_for(1)

    # ---- geometry ---------------------------------------------------------

    @property
    def scale(self) -> float:
        return float(settings.get("wheel_size", 1.0) or 1.0)

    def _radii(self) -> list[tuple[float, float]]:
        s = self.scale
        return [((INNER_R + i * RING_W) * s, (INNER_R + (i + 1) * RING_W) * s) for i in range(self._rings)]

    def _resize_for(self, rings: int) -> None:
        self._rings = rings
        outer = (INNER_R + rings * RING_W) * self.scale
        side = int(2 * (outer + MARGIN * self.scale))
        self.setFixedSize(side, side)

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    def _slots(self) -> list[tuple[int, int, int]]:
        """(ring, index_in_ring, ring_count) for each option."""
        n = len(self.options)
        rings = max(1, math.ceil(n / RING_CAPACITY)) if n > RING_CAPACITY else 1
        # Share wedges between rings in proportion to each ring's circumference.
        mids = [INNER_R + (i + 0.5) * RING_W for i in range(rings)]
        counts = [max(1, round(n * m / sum(mids))) for m in mids]
        counts[-1] = n - sum(counts[:-1])
        out = []
        for ring, count in enumerate(counts):
            out.extend((ring, i, count) for i in range(count))
        return out

    def _index_at(self, pos: QPointF) -> int:
        c = self._center()
        dx, dy = pos.x() - c.x(), pos.y() - c.y()
        r = math.hypot(dx, dy)
        radii = self._radii()
        ring = next((i for i, (r0, r1) in enumerate(radii) if r0 <= r < r1), None)
        if ring is None:
            return -1
        # 0 degrees = straight up, clockwise.
        ang = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
        for idx, (rg, i, count) in enumerate(self._slots()):
            if rg != ring:
                continue
            span = 360 / count
            start = (i * span - span / 2) % 360
            if (ang - start) % 360 < span:
                return idx
        return -1

    # ---- showing ----------------------------------------------------------

    def open_for_drag(self, mode: str) -> None:
        """Called when a wheel key is tapped mid-drag."""
        logging.info("wheel key: %s (visible=%s)", mode, self.isVisible())
        self.click_mode = False
        self.mode = mode
        if not self.isVisible():
            self.paths = []
            self.options = []
            self._place_at_cursor()
            self.show()
            self.raise_()
            logging.info("wheel shown at %s visible=%s", self.geometry(), self.isVisible())
            # Make the OLE drag loop notice us right away so dragEnterEvent fires.
            from .hooks import nudge_mouse
            QTimer.singleShot(30, nudge_mouse)
        else:
            self._refresh_options()
        self.update()

    def show_for(self, paths: list[Path], mode: str = "convert") -> None:
        """Open in click mode for a known selection (Send To, drop zone)."""
        self.click_mode = True
        self.mode = mode
        self.paths = paths
        self._refresh_options()
        self._place_at_cursor()
        self.show()
        self.raise_()
        self.activateWindow()
        self.update()

    def _place_at_cursor(self) -> None:
        self._resize_for(self._rings)
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = min(max(pos.x() - self.width() // 2, geo.left()), geo.right() - self.width())
        y = min(max(pos.y() - self.height() // 2, geo.top()), geo.bottom() - self.height())
        self.move(QPoint(x, y))

    def _refresh_options(self) -> None:
        self.options = registry.options_for(self.mode, self.paths) if self.paths else []
        rings = max(1, len({s[0] for s in self._slots()}))
        if rings != self._rings:
            centre = self.geometry().center()
            self._resize_for(rings)
            self.move(centre - QPoint(self.width() // 2, self.height() // 2))
        self.hover = -1

    def drag_ended(self) -> None:
        # The hook sees the button-up before the drop is delivered, so wait a beat.
        if self.isVisible() and not self.click_mode:
            self._hide_timer.start()

    def _hide_if_idle(self) -> None:
        if not self._dropping:
            self.hide()

    def cancel(self) -> None:
        self.hide()

    # ---- drag & drop ------------------------------------------------------

    def dragEnterEvent(self, e) -> None:
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        logging.info("drag enter with %d file(s)", len(urls))
        if not urls:
            e.ignore()
            return
        self.paths = [Path(u) for u in urls]
        self._refresh_options()
        e.acceptProposedAction()
        self.update()

    def dragMoveEvent(self, e) -> None:
        idx = self._index_at(e.position())
        if idx != self.hover:
            self.hover = idx
            self.update()
        if idx >= 0:
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            # Accept anyway so dropping in the centre just cancels quietly.
            e.setDropAction(Qt.CopyAction)
            e.accept()

    def dragLeaveEvent(self, e) -> None:
        self.hover = -1
        self.update()

    def dropEvent(self, e) -> None:
        self._dropping = True
        idx = self._index_at(e.position())
        e.setDropAction(Qt.CopyAction)
        e.accept()
        option = self.options[idx] if 0 <= idx < len(self.options) else None
        paths = list(self.paths)
        logging.info("drop at %s -> wedge %d (%s) with %d path(s)", e.position().toPoint(), idx,
                     option.id if option else None, len(paths))
        self.hide()
        self._dropping = False
        if option is not None:
            # Let the drag source finish its loop before we open dialogs.
            QTimer.singleShot(0, lambda: self.chosen.emit(option, paths))

    # ---- click mode -------------------------------------------------------

    def mouseMoveEvent(self, e) -> None:
        idx = self._index_at(e.position())
        if idx != self.hover:
            self.hover = idx
            self.update()

    def mousePressEvent(self, e) -> None:
        if not self.click_mode:
            return
        if e.button() == Qt.RightButton:
            self.hide()
            return
        # Clicking a mode chip switches mode.
        for mode, rect in self._mode_chip_rects().items():
            if rect.contains(e.position()):
                self.mode = mode
                self._refresh_options()
                self.update()
                return
        idx = self._index_at(e.position())
        self.hide()
        if 0 <= idx < len(self.options):
            option, paths = self.options[idx], list(self.paths)
            QTimer.singleShot(0, lambda: self.chosen.emit(option, paths))

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            self.hide()

    def changeEvent(self, e) -> None:
        # A click wheel closes when the user clicks elsewhere.
        if (getattr(self, "click_mode", False) and e.type() == e.Type.ActivationChange
                and not self.isActiveWindow()):
            QTimer.singleShot(0, self.hide)
        super().changeEvent(e)

    # ---- painting ---------------------------------------------------------

    def _mode_chip_rects(self) -> dict[str, QRectF]:
        s = self.scale
        f = QFont("Segoe UI", 8)
        fm = QFontMetrics(f)
        labels = {m: self._chip_text(m) for m in MODES}
        widths = {m: fm.horizontalAdvance(t) + 20 * s for m, t in labels.items()}
        total = sum(widths.values()) + 6 * s * (len(MODES) - 1)
        x = (self.width() - total) / 2
        y = self.height() - MARGIN * s + 8 * s
        rects = {}
        for m in (reversed(MODES) if is_rtl() else MODES):
            rects[m] = QRectF(x, y, widths[m], 22 * s)
            x += widths[m] + 6 * s
        return rects

    @staticmethod
    def _chip_text(mode: str) -> str:
        key = tr(KEY_LABELS.get(settings.get(f"keys.{mode}"), "?"))
        short = tr(MODE_SHORT[mode])
        return f"{key}  {short}"

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._center()
        radii = self._radii()
        outer = radii[-1][1]
        s = self.scale

        # Backdrop disc. Alpha must stay > 0 everywhere we want drops: Windows
        # treats fully transparent pixels of a layered window as click-through.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(24, 24, 28, 236))
        p.drawEllipse(c, outer + 6 * s, outer + 6 * s)

        slots = self._slots()
        lfont = label_font(s)
        for idx, (ring, i, count) in enumerate(slots):
            opt = self.options[idx]
            r0, r1 = radii[ring]
            span = 360 / count
            gap = 1.6 if count > 1 else 0
            # Qt angles: 0 = 3 o'clock, counter-clockwise. Ours: 0 = 12 o'clock, clockwise.
            mid = i * span
            start_qt = 90 - (mid - span / 2 + gap / 2)
            sweep_qt = -(span - gap)
            path = QPainterPath()
            path.arcMoveTo(QRectF(c.x() - r1, c.y() - r1, 2 * r1, 2 * r1), start_qt)
            path.arcTo(QRectF(c.x() - r1, c.y() - r1, 2 * r1, 2 * r1), start_qt, sweep_qt)
            path.arcTo(QRectF(c.x() - r0, c.y() - r0, 2 * r0, 2 * r0), start_qt + sweep_qt, -sweep_qt)
            path.closeSubpath()
            tint = QColor(formats.FAMILY_COLORS.get(opt.family, ACCENT))
            if idx == self.hover:
                fill = tint
            else:
                fill = QColor(tint)
                fill.setAlpha(46)
            p.setBrush(fill)
            p.setPen(QPen(QColor(255, 255, 255, 18), 1))
            p.drawPath(path)

            rad = math.radians(mid)
            rm = (r0 + r1) / 2
            tx, ty = c.x() + rm * math.sin(rad), c.y() - rm * math.cos(rad)
            p.setPen(QColor("#111") if idx == self.hover else QColor(240, 240, 245))
            text = opt.title.upper()
            maxw = label_width(r0, r1, count, mid)
            maxw = min(maxw, (r1 - r0) * 1.6)  # keep side labels from spilling out of the ring
            box = QRectF(tx - maxw / 2, ty - 18 * s, maxw, 36 * s)
            font, flags, shown = fit_label(lfont, text, box)
            p.setFont(font)
            p.drawText(box, flags, shown)

        # Centre: title + what is being dragged, or the hovered option's description.
        p.setBrush(QColor(14, 14, 16, 245))
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        hole = radii[0][0] - 4 * s
        p.drawEllipse(c, hole, hole)
        p.setPen(QColor(ACCENT))
        p.setFont(QFont("Segoe UI Semibold", max(6, int(7.5 * s))))
        title = tr(MODE_TITLES[self.mode]).upper()
        p.drawText(QRectF(c.x() - hole, c.y() - 26 * s, 2 * hole, 16 * s), Qt.AlignCenter, title)
        p.setPen(QColor(220, 220, 225))
        p.setFont(QFont("Segoe UI", max(6, int(7.5 * s))))
        fm_sub = QFontMetrics(p.font())
        sub_box = QRectF(c.x() - hole + 8 * s, c.y() - 8 * s, 2 * hole - 16 * s, 3 * fm_sub.lineSpacing() + 2)
        is_name = False
        if 0 <= self.hover < len(self.options):
            sub = self.options[self.hover].text or self.options[self.hover].title
        elif not self.paths:
            sub = tr("Drop files here")
        elif not self.options:
            sub = tr("Nothing for these files")
        elif len(self.paths) == 1:
            sub, is_name = self.paths[0].name, True
        else:
            sub = tr("{n} items").format(n=len(self.paths))
        if is_name:  # file names: one line, cut in the middle so the extension stays visible
            p.drawText(sub_box, Qt.AlignHCenter | Qt.AlignTop,
                       fm_sub.elidedText(sub, Qt.ElideMiddle, int(sub_box.width())))
        else:
            p.drawText(sub_box, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                       wrap_elide(fm_sub, sub, sub_box))

        # Mode chips under the wheel: which key opens which wheel.
        p.setFont(QFont("Segoe UI", 8))
        for mode, rect in self._mode_chip_rects().items():
            active = mode == self.mode
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT) if active else QColor(24, 24, 28, 230))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            p.setPen(QColor("#111") if active else QColor(220, 220, 225))
            p.drawText(rect, Qt.AlignCenter, self._chip_text(mode))
        p.end()


def label_font(scale: float) -> QFont:
    f = QFont("Segoe UI Semibold")
    f.setPointSizeF(max(7.0, 9.5 * scale))
    if current() in ("en", "es"):  # spacing breaks Arabic joining and Devanagari/CJK
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
    return f


def label_width(r0: float, r1: float, count: int, angle: float = 0.0) -> float:
    """Usable width of a horizontal label centred in a wedge at `angle` degrees
    (0 = top). Wedges at the sides are as wide as the ring is thick; at the top and
    bottom only the wedge's chord is available."""
    radial = (r1 - r0) * 0.95
    if count <= 1:
        return radial
    chord = 2 * (r0 + r1) / 2 * math.sin(math.pi / count) * 0.92
    a = math.radians(angle)
    sin_a, cos_a = abs(math.sin(a)), abs(math.cos(a))
    return min(radial / sin_a if sin_a > 1e-3 else 1e9, chord / cos_a if cos_a > 1e-3 else 1e9)


def wrap_elide(fm: QFontMetrics, text: str, box: QRectF) -> str:
    """`text` shortened with '…' so that, word-wrapped, it fits in `box`."""
    flags = int(Qt.AlignHCenter | Qt.TextWordWrap)

    def fits(t: str) -> bool:
        r = fm.boundingRect(box.toRect(), flags, t)
        return r.height() <= box.height() and r.width() <= box.width() + 1

    if fits(text):
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # longest prefix that still fits with an ellipsis
        mid = (lo + hi + 1) // 2
        if fits(text[:mid].rstrip() + "…"):
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


LABEL_SHRINK = (1.0, 0.9, 0.8, 0.72)  # font size steps tried before a label is cut


def fit_label(font: QFont, text: str, box: QRectF) -> tuple[QFont, int, str]:
    """(font, flags, text) to draw a wedge label in `box`. Tries one line, then two
    wrapped lines, at slightly smaller sizes; only then cuts the text with '…'."""
    wrap = Qt.AlignCenter | Qt.TextWordWrap
    for step in LABEL_SHRINK:
        f = QFont(font)
        f.setPointSizeF(font.pointSizeF() * step)
        fm = QFontMetrics(f)
        if fm.horizontalAdvance(text) <= box.width():
            return f, int(Qt.AlignCenter), text
        two = fm.boundingRect(box.toRect(), int(wrap), text)
        if " " in text.strip() and two.height() <= box.height() and two.width() <= box.width():
            return f, int(wrap), text
    f = QFont(font)
    f.setPointSizeF(font.pointSizeF() * LABEL_SHRINK[-1])
    return f, int(Qt.AlignCenter), QFontMetrics(f).elidedText(text, Qt.ElideRight, int(box.width()))


def is_wheel_key(vk: int) -> bool:
    return any(vk in KEY_CHOICES.get(settings.get(f"keys.{m}"), ()) for m in MODES)
