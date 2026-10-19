"""The floating shelf: a small always-on-top window that holds files for later.

Drop files on it (or use the "Float" action), then drag them out into any app.
Drag the header near a screen edge to pin it there; a pinned shelf collapses to
a slim tab and expands again on hover or when files are dragged over it.

Edges are physical screen edges: window geometry is in screen coordinates and is
not mirrored by a right-to-left layout direction, so "left" stays the left edge in
Arabic too (only the shelf's inner layout is mirrored).
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QFileInfo, QMimeData, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QFont, QGuiApplication, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QFileIconProvider, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QToolButton, QVBoxLayout, QWidget,
)

from ..config import ACCENT, APP_NAME
from ..i18n import tr
from ..ui.common import STYLE

SNAP_PX = 28                 # how close to an edge a release must be to pin
TAB_THICK, TAB_LEN = 18, 120  # collapsed tab size (across, along the edge)
PANEL_SIZE = QSize(290, 340)
EDGES = ("left", "right", "top", "bottom")


# ---- pure geometry helpers (unit-tested) -----------------------------------

def snap_edge(win: QRect, avail: QRect, threshold: int = SNAP_PX) -> str | None:
    """The screen edge `win` is within `threshold` px of (closest wins), else None."""
    dist = {
        "left": win.left() - avail.left(),
        "right": avail.right() - win.right(),
        "top": win.top() - avail.top(),
        "bottom": avail.bottom() - win.bottom(),
    }
    edge, d = min(dist.items(), key=lambda kv: kv[1])
    return edge if d <= threshold else None


def docked_rect(edge: str, along: int, avail: QRect, collapsed: bool,
                panel: QSize = PANEL_SIZE) -> QRect:
    """Geometry for a shelf pinned to `edge`; `along` is its x (top/bottom) or y (left/right)."""
    vertical_edge = edge in ("left", "right")
    if collapsed:
        w, h = (TAB_THICK, TAB_LEN) if vertical_edge else (TAB_LEN, TAB_THICK)
    else:
        w, h = panel.width(), panel.height()
    if vertical_edge:
        y = min(max(along, avail.top()), avail.bottom() + 1 - h)
        x = avail.left() if edge == "left" else avail.right() + 1 - w
    else:
        x = min(max(along, avail.left()), avail.right() + 1 - w)
        y = avail.top() if edge == "top" else avail.bottom() + 1 - h
    return QRect(x, y, w, h)


def urls_mime(paths: list[Path]) -> QMimeData:
    """Mime data that Explorer and other apps accept as dropped files."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def local_paths(mime: QMimeData) -> list[Path]:
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []


# ---- widgets ----------------------------------------------------------------

class ShelfList(QListWidget):
    """The file chips. Drags out as file URLs (copy); accepts external file drops."""

    files_dropped = Signal(list)
    remove_requested = Signal()
    drag_started = Signal()
    drag_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QSize(28, 28))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setSpacing(2)
        self.setMinimumSize(80, 60)
        self.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { background: #26262c; border-radius: 8px; padding: 4px; color: #ececf1; }"
            f"QListWidget::item:selected {{ background: #3a3026; border: 1px solid {ACCENT}; }}"
            "QListWidget::item:hover { background: #2f2f36; }")

    def item_paths(self, items) -> list[Path]:
        return [Path(i.data(Qt.UserRole)) for i in items]

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]

    def mimeData(self, items) -> QMimeData:
        return urls_mime(self.item_paths(items))

    def supportedDropActions(self):
        return Qt.CopyAction

    def startDrag(self, actions) -> None:
        items = self.selectedItems()
        paths = [p for p in self.item_paths(items) if p.exists()]
        if not paths:
            return
        drag = QDrag(self)
        drag.setMimeData(urls_mime(paths))
        icon = items[0].icon()
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(32, 32))
        self.drag_started.emit()
        try:
            drag.exec(Qt.CopyAction, Qt.CopyAction)
        finally:
            self.drag_finished.emit()

    def _external(self, e) -> bool:
        return e.source() is not self and bool(local_paths(e.mimeData()))

    def dragEnterEvent(self, e) -> None:
        if self._external(e):
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        if self._external(e):
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e) -> None:
        if not self._external(e):
            e.ignore()
            return
        e.setDropAction(Qt.CopyAction)
        e.accept()
        self.files_dropped.emit(local_paths(e.mimeData()))

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_requested.emit()
            return
        super().keyPressEvent(e)


class Shelf(QWidget):
    """Always-on-top frameless shelf. Use `get_shelf()` for the app-wide instance."""

    changed = Signal()

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(tr("{app} shelf").format(app=APP_NAME))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE + "QWidget#shelfRoot { background: transparent; }")
        self.edge: str | None = None
        self.collapsed = False
        self._along = 0
        self._press: QPoint | None = None
        self._moved = False
        self._busy = False   # dragging out / menu open: do not collapse
        self._icons = QFileIconProvider()
        self._collapse_timer = QTimer(self, singleShot=True, interval=700, timeout=self._maybe_collapse)

        self.content = QWidget(self, objectName="shelfRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.content)
        lay = QVBoxLayout(self.content)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.setSpacing(6)

        self.header = QWidget()
        self.header.setCursor(Qt.SizeAllCursor)
        self.header.setStyleSheet("background: transparent;")
        h = QHBoxLayout(self.header)
        h.setContentsMargins(0, 0, 0, 0)
        title = QLabel(tr("Shelf"))
        title.setStyleSheet(f"color: {ACCENT}; font-weight: 600; background: transparent;")
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #9a9aa6; background: transparent;")
        h.addWidget(title)
        h.addWidget(self.count_label)
        h.addStretch(1)
        self.clear_btn = self._tool_button(tr("Clear"), tr("Remove all items"), self.clear)
        self.close_btn = self._tool_button("✕", tr("Hide the shelf (items are kept)"), self.hide)
        h.addWidget(self.clear_btn)
        h.addWidget(self.close_btn)
        self.header.installEventFilter(self)
        title.installEventFilter(self)
        self.count_label.installEventFilter(self)
        lay.addWidget(self.header)

        self.list = ShelfList()
        self.list.files_dropped.connect(self.add_paths)
        self.list.remove_requested.connect(self.remove_selected)
        self.list.drag_started.connect(lambda: setattr(self, "_busy", True))
        self.list.drag_finished.connect(self._drag_done)
        self.list.itemDoubleClicked.connect(self._open_item)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        lay.addWidget(self.list, 1)

        self.hint = QLabel(tr("Drop files here, drag them out later.\nDrag the header to a screen edge to pin."))
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #6b6b75; background: transparent; font-size: 9pt;")
        lay.addWidget(self.hint)

        delete = QAction(self, shortcut=QKeySequence.Delete, triggered=self.remove_selected)
        self.addAction(delete)

        self.resize(PANEL_SIZE)
        self._place_default()
        self._refresh()

    def _tool_button(self, text: str, tip: str, slot) -> QToolButton:
        b = QToolButton(text=text, toolTip=tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet("QToolButton { background: transparent; border: none; color: #9a9aa6; padding: 2px 6px; }"
                        "QToolButton:hover { color: #ececf1; background: #2c2c33; border-radius: 4px; }")
        b.clicked.connect(slot)
        return b

    # ---- items ------------------------------------------------------------

    @property
    def paths(self) -> list[Path]:
        return [Path(self.list.item(i).data(Qt.UserRole)) for i in range(self.list.count())]

    def add_paths(self, paths) -> int:
        """Add files/folders (duplicates ignored). Returns how many were added."""
        have = {str(p).lower() for p in self.paths}
        added = 0
        for p in paths:
            p = Path(os.path.abspath(p))
            key = str(p).lower()
            if key in have:
                continue
            have.add(key)
            item = QListWidgetItem(self._icons.icon(QFileInfo(str(p))), p.name or str(p))
            item.setData(Qt.UserRole, str(p))
            item.setToolTip(str(p))
            item.setSizeHint(QSize(0, 38))
            self.list.addItem(item)
            added += 1
        if added:
            self._refresh()
        return added

    def remove_paths(self, paths) -> int:
        keys = {str(Path(p)).lower() for p in paths}
        removed = 0
        for i in reversed(range(self.list.count())):
            if str(self.list.item(i).data(Qt.UserRole)).lower() in keys:
                self.list.takeItem(i)
                removed += 1
        if removed:
            self._refresh()
        return removed

    def remove_selected(self) -> None:
        self.remove_paths(self.list.item_paths(self.list.selectedItems()))

    def clear(self) -> None:
        self.list.clear()
        self._refresh()

    def _refresh(self) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            missing = not Path(item.data(Qt.UserRole)).exists()
            item.setForeground(QColor("#6b6b75") if missing else QColor("#ececf1"))
            item.setToolTip(item.data(Qt.UserRole) + ("\n" + tr("(missing)") if missing else ""))
        n = self.list.count()
        self.count_label.setText(f"· {n}" if n else "")
        self.hint.setVisible(n == 0)
        self.clear_btn.setEnabled(n > 0)
        self.update()
        self.changed.emit()

    # ---- context menu / item actions ---------------------------------------

    def _selected_or(self, item) -> list[Path]:
        sel = self.list.item_paths(self.list.selectedItems())
        return sel or ([Path(item.data(Qt.UserRole))] if item else [])

    def _open_item(self, item) -> None:
        try:
            os.startfile(item.data(Qt.UserRole))  # noqa: S606 - opening the user's own file
        except OSError:
            pass

    def _context_menu(self, pos) -> None:
        from . import file_actions as fa

        item = self.list.itemAt(pos)
        paths = self._selected_or(item)
        menu = QMenu(self)
        if paths:
            existing = [p for p in paths if p.exists()]
            menu.addAction(tr("Open"), lambda: [self._open_item(self.list.item(i)) for i in range(self.list.count())
                                             if Path(self.list.item(i).data(Qt.UserRole)) in paths])
            a = menu.addAction(tr("Show in Explorer"), lambda: fa.reveal(existing))
            a.setEnabled(bool(existing))
            a = menu.addAction(tr("Copy"), lambda: fa.set_clipboard_files(existing))
            a.setEnabled(bool(existing))
            menu.addAction(tr("Copy path"), lambda: fa.set_clipboard_text(fa.paths_text(paths)))
            menu.addSeparator()
            menu.addAction(tr("Remove"), lambda: self.remove_paths(paths))
        menu.addAction(tr("Remove missing"), self.remove_missing)
        a = menu.addAction(tr("Clear all"), self.clear)
        a.setEnabled(self.list.count() > 0)
        self._busy = True
        try:
            menu.exec(self.list.viewport().mapToGlobal(pos))
        finally:
            self._busy = False
            self._schedule_collapse()

    def remove_missing(self) -> None:
        self.remove_paths([p for p in self.paths if not p.exists()])

    # ---- showing / pinning -------------------------------------------------

    def _screen_geo(self) -> QRect:
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _place_default(self) -> None:
        geo = (QGuiApplication.primaryScreen().availableGeometry()
               if QGuiApplication.primaryScreen() else QRect(0, 0, 1280, 800))
        self.move(geo.right() - PANEL_SIZE.width() - 40, geo.top() + (geo.height() - PANEL_SIZE.height()) // 2)

    def present(self) -> None:
        """Show (expanded) and bring to front."""
        if self.collapsed:
            self.expand()
        self.show()
        self.raise_()
        if self.edge:
            self._schedule_collapse()

    def pin(self, edge: str | None) -> None:
        """Pin to `edge` (or unpin with None) at the current position.

        `edge` is a physical screen edge (see the module docstring), whatever the layout direction.
        """
        self.edge = edge
        if edge is None:
            self.expand()
            return
        g = self.geometry()
        self._along = g.y() if edge in ("left", "right") else g.x()
        self.setGeometry(docked_rect(edge, self._along, self._screen_geo(), False))

    def collapse(self) -> None:
        if not self.edge or self.collapsed:
            return
        geo = self._screen_geo()
        self.collapsed = True
        self.content.hide()
        rect = docked_rect(self.edge, self._along, geo, True)
        self.setMinimumSize(0, 0)
        self.setGeometry(rect)
        self.update()

    def expand(self) -> None:
        if not self.collapsed:
            return
        self.collapsed = False
        self.content.show()
        if self.edge:
            self.setGeometry(docked_rect(self.edge, self._along, self._screen_geo(), False))
        else:
            self.resize(PANEL_SIZE)
        self.update()

    def _schedule_collapse(self) -> None:
        if self.edge and not self.collapsed:
            self._collapse_timer.start()

    def _maybe_collapse(self) -> None:
        if self._busy or self._press is not None or self.underMouse() or not self.isVisible():
            if self.edge and not self.collapsed and not self.underMouse():
                self._collapse_timer.start()
            return
        self.collapse()

    def _drag_done(self) -> None:
        self._busy = False
        self._refresh()
        self._schedule_collapse()

    # ---- events -------------------------------------------------------------

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            return True
        if t == QEvent.MouseMove and self._press is not None:
            if self.edge is not None:   # tearing it off the edge
                self.edge = None
            self._moved = True
            self.move(e.globalPosition().toPoint() - self._press)
            return True
        if t == QEvent.MouseButtonRelease and self._press is not None:
            self._press = None
            if self._moved:
                edge = snap_edge(self.frameGeometry(), self._screen_geo())
                self.pin(edge)
            return True
        return super().eventFilter(obj, e)

    def mousePressEvent(self, e) -> None:
        if self.collapsed:
            self.expand()

    def enterEvent(self, e) -> None:
        self._collapse_timer.stop()
        if self.collapsed:
            self.expand()

    def leaveEvent(self, e) -> None:
        self._schedule_collapse()

    def dragEnterEvent(self, e) -> None:
        if self.collapsed:
            self.expand()
        if local_paths(e.mimeData()) and e.source() is not self.list:
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        self.dragEnterEvent(e)

    def dropEvent(self, e) -> None:
        paths = local_paths(e.mimeData())
        if paths and e.source() is not self.list:
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self.add_paths(paths)
            self._schedule_collapse()

    def dragLeaveEvent(self, e) -> None:
        self._schedule_collapse()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        if self.collapsed:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(r, 6, 6)
            p.setPen(QColor("#111"))
            p.setFont(QFont("Segoe UI Semibold", 8))
            text = str(self.list.count()) if self.list.count() else "≡"
            p.drawText(r, Qt.AlignCenter, text)
        else:
            p.setPen(QPen(QColor("#3a3a43"), 1))
            p.setBrush(QColor(28, 28, 32, 248))
            p.drawRoundedRect(r, 12, 12)
        p.end()


_shelf: Shelf | None = None


def get_shelf() -> Shelf:
    """The app-wide shelf (created on first use, hidden until shown)."""
    global _shelf
    if _shelf is None:
        _shelf = Shelf()
    return _shelf


def show_shelf() -> Shelf:
    """For the tray menu: show the shelf expanded."""
    shelf = get_shelf()
    shelf.present()
    return shelf
