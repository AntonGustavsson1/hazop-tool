#!/usr/bin/env python3
"""P&ID graphics view — the pan/zoom/draw-mode QGraphicsView subclass, split
out of pid_viewer.py 2026-08-18, see NOTES.md "Förenkla koden + dela upp
hazop.py i fler filer". Moved as a single whole class (not split
internally) — see NOTES.md for why: its ~3000 lines are already organized
into clearly named method groups (pan/zoom, markup overlays, red-markup
overlays, symbol resize/rotate, equipment marker multiselect), and breaking
a single class's own methods across files would need mixin inheritance,
a materially bigger and riskier change than moving the whole class."""

import re
import os
import json
import math
import logging
from functools import partial

from PyQt6.QtWidgets import (
    QApplication, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QGraphicsPixmapItem, QGraphicsPathItem, QGraphicsEllipseItem,
    QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsTextItem, QLineEdit, QMenu,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QTimer, QMimeData
from PyQt6.QtGui import (
    QColor, QPen, QBrush, QPainterPath, QPolygonF, QPixmap, QImage, QFont,
    QPainter, QDrag,
)

import symbol_geometry
import equipment_detection
from equipment_detection import _get_easyocr_reader, _preprocess_for_ocr
from pid_viewer import (
    fitz, HAS_PYMUPDF, HAS_OPENGL, QOpenGLWidget, HAS_SVG_RENDERER,
    QSvgRenderer, HAS_PIL, HAS_TESSERACT, HAS_EASYOCR, _PILImage, pytesseract,
    CONFIG,
    Z_PAGE, Z_HIGHLIGHT, Z_SHEET_CONN, Z_CONNECT, Z_OVERLAY, Z_TEMP,
    MODE_NAV, MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE,
    MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT, MODE_MARKUP_SELECT,
    MODE_RED_MARKUP_SYMBOL, MODE_BOARD_LAYOUT,
    MODE_ADD_SHEET_LINK, MODE_PICK_REF_TAG, MODE_ANNOTATION,
    MODE_PLACE_EQUIPMENT,
    MODE_EDIT_EQUIPMENT,
    _icon, _get_red_symbol_svg, _PageRenderer, _apply_min_pdf_line_width,
    SimilarSymbolSearchDialog,
)
# MODE_SMART_POLYLINE / SmartPipeTracer ("Smart polylinje") removed
# 2026-08-26 -- see NOTES.md and archive/smart_pipe_tracer.py.

class PIDGraphicsView(QGraphicsView):
    node_markup_finished    = pyqtSignal(list, int)
    context_action          = pyqtSignal(str, object, int)
    marker_clicked          = pyqtSignal(str, int)
    ref_tag_picked          = pyqtSignal(str)   # MODE_PICK_REF_TAG one-shot result
    equipment_place_requested = pyqtSignal(object, int)  # scene_pos, page
    equipment_place_zone_requested = pyqtSignal(object, int)  # PDF QRectF, page
    annotation_clicked      = pyqtSignal(object)  # QPointF — MODE_ANNOTATION click
    # Markup editing signals
    markup_draw_finished    = pyqtSignal(str, list, int)   # type_, pdf_pts, page
    markup_item_clicked     = pyqtSignal(int)              # markup_id
    markup_moved            = pyqtSignal(int, list)        # mu_id, new PDF points [[x,y],...]
    markup_label_edited     = pyqtSignal(int, str)         # mu_id, new_label
    markup_duplicate_requested = pyqtSignal(int)           # mu_id
    markup_symbol_dims_changed = pyqtSignal(int, float, float, float)  # mu_id, w, h, rot_deg
    board_layout_changed     = pyqtSignal(str)  # JSON {"0": [ox, oy], ...}
    sheet_conn_break_requested = pyqtSignal(int)          # connection row id
    sheet_conn_add_requested   = pyqtSignal(int, int)     # (from_page, to_page)
    zone_drawn    = pyqtSignal(object, int)                # (QRectF pdf_coords, page)
    equipment_drag_finished = pyqtSignal()  # Shift+drag of an equipment marker released (drop accepted or not)
    equipment_edit_requested = pyqtSignal(int)  # equipment_markers.id — right-click "✏️ Redigera objekt"
    equipment_delete_requested = pyqtSignal(int)  # equipment_markers.id — right-click "Ta bort" (2026-08-25)
    equipment_reposition_requested = pyqtSignal(int)
    equipment_reposition_finished = pyqtSignal(int, object, int)

    # Keys for QGraphicsItem.setData / .data
    _DATA_TYPE      = 0    # 'cause' | 'consequence' | 'safeguard' | 'markup'
    _DATA_ID        = 1    # database id
    _DATA_MARKUP_ID = 2    # markup id (for markup items)
    _DATA_MARKUP_PTS = 3   # stores PDF points list on path/text items
    _DATA_SYMBOL_W   = 6   # float PDF-unit width stored on symbol items
    _DATA_SYMBOL_H   = 7   # float PDF-unit height stored on symbol items
    _DATA_SYMBOL_ROT = 8   # float rotation degrees stored on symbol items

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # White background — matches typical P&ID drawing background
        self.setBackgroundBrush(QBrush(QColor(255, 255, 255)))

        # ── GPU-accelerated rendering (OpenGL) ────────────────────────────────
        if HAS_OPENGL:
            gl = QOpenGLWidget()
            self.setViewport(gl)
            # With OpenGL, full-viewport update is required (GPU composites all)
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        else:
            # CPU fallback: only repaint changed tiles
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)

        # Avoid unnecessary bounding-rect adjustments during pan/zoom
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        # Don't save/restore painter state per item — slight speedup
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True)

        self._press_pos  = None
        # Shift+drag of an equipment marker onto a worksheet consequence row
        # (see mousePressEvent/mouseMoveEvent) — armed on Shift+press over a
        # marker, consumed by mouseMoveEvent once the drag distance is exceeded.
        self._equip_drag_candidate = None
        # Multi-select of equipment markers (2026-08-08, see NOTES.md):
        # Ctrl+click toggles one marker in/out, Ctrl+drag rubber-bands
        # several at once; a Shift-drag started from a marker that's part
        # of a >=2-member selection then drags the whole group.
        self._selected_equipment_markers: set = set()
        self._equip_selection_overlays: dict = {}   # marker_id -> highlight QGraphicsItem
        self._search_highlight_marker = None
        self._search_highlight_overlay = None
        # Tree-context equipment highlight (2026-08-27, see NOTES.md
        # "Dynamisk färgmarkering av objekt på P&ID") — separate from the
        # multi-select state just above; a marker can be both multi-
        # selected AND tree-context-highlighted at once (see
        # set_tree_context_highlights()'s docstring for the visual
        # layering that keeps the two unambiguous).
        self._tree_context_highlights: dict = {}          # marker_id -> QColor
        self._ctrl_rband_start_scene  = None
        self._ctrl_rband_dragging     = False
        self._ctrl_rband_preview_item = None
        self._ctrl_rband_count_label  = None   # "N objekt" label shown live during the drag
        # Per-type marker tracking for visibility toggle
        self._type_items: dict = {'cause': [], 'consequence': [], 'safeguard': [], 'equipment': []}
        self._type_visible: dict = {'cause': True, 'consequence': True, 'safeguard': True, 'equipment': True}

        self.mode             = MODE_NAV
        self.pdf_doc          = None
        self.current_page     = 0
        self.page_item        = None
        self.render_scale     = 1.0
        self.page_rect_width  = 0.0
        self.page_rect_height = 0.0
        self._RASTER_SCALE    = 3.0
        # Display-only enhancement for sub-pixel P&ID strokes. Zero disables
        # it; the PIDPanel initialises this from the project settings.
        self._min_pdf_line_width = 0
        self._pdf_path        = None
        self._page_cache: dict = {}
        # Raster scale each cached pixmap was actually rendered at — a page
        # can now be cached above _RASTER_SCALE when the adaptive-zoom
        # re-rasterization (see _target_raster_scale) upgrades it.
        self._page_cache_scale: dict = {}
        self._cache_order: list = []
        self._CACHE_SIZE      = 10
        self._prefetch_thread = None

        # ── Manual per-page rotation override (2026-08-12, see NOTES.md) ──────
        # Extra clockwise degrees (0/90/180/270) the user chose for a physical
        # page, composed with (not replacing) the PDF's own /Rotate flag by
        # mutating the in-memory fitz.Page's rotation — see
        # _apply_page_rotation(). Never written back to the PDF file.
        self._page_rotation_override: dict = {}   # physical_page -> extra degrees
        self._intrinsic_rotation: dict = {}        # physical_page -> page.rotation before any override

        # ── Page level-of-detail (study board) ────────────────────────────────
        # Board pages render at _LOW_SCALE (cheap, small) and are scaled up to
        # the same scene footprint; only pages visible at high zoom are swapped
        # to full _RASTER_SCALE pixmaps in the background. Keeps zoomed-out
        # panning fast and memory flat regardless of page count.
        self._LOW_SCALE       = 0.5
        self._MAX_HIRES       = 6
        self._low_pixmaps: dict = {}    # pn → low-res QPixmap
        self._hires_pages: set  = set() # pages currently showing hi-res
        # Raster scale actually displayed on the hi-res item for each hires
        # page — may exceed _RASTER_SCALE, see adaptive re-rasterization below.
        self._page_display_scale: dict = {}
        self._lod_renderer    = None    # background _PageRenderer for hi-res
        # Supersampling margin: a tier is "crisp enough" once it supplies at
        # least this many raw pixels per screen pixel. Shared by
        # _hires_worthwhile() (low->hires threshold) and _target_raster_scale()
        # (adaptive hires->sharper-hires threshold) so both tiers use the same
        # visual standard for "still blurry".
        self._LOD_MARGIN      = 1.25
        # Adaptive re-rasterization caps (2026-08-12, see NOTES.md "P&ID blir
        # suddig vid inzoomning") — a page can be re-rendered at a scale
        # higher than _RASTER_SCALE once the view zooms in past what
        # _RASTER_SCALE can supply crisply, but never above these safety caps
        # so a pathological zoom level can't render an absurdly large pixmap.
        self._MAX_RASTER_SCALE = self._RASTER_SCALE * 4   # absolute multiplier cap
        self._MAX_RASTER_DIM   = 6000  # cap on the larger pixel dimension of any single raster
        self._lod_timer       = QTimer(self)
        self._lod_timer.setSingleShot(True)
        self._lod_timer.setInterval(150)
        self._lod_timer.timeout.connect(self._update_page_lod)

        self.draw_points        = []
        self.draw_pen           = QPen(QColor(255, 140, 0), 3)
        self.draw_pen.setCosmetic(True)
        self.draw_brush         = QBrush(QColor(255, 140, 0, 60))
        self.temp_items         = []
        self.rubber_line        = None

        # Cache for PDF line segments (for smart snapping to drawing details)
        self._pdf_line_segments: dict = {}  # page_num → list of line tuples (x1,y1,x2,y2)

        # Right-drag rubber-band (NAV mode)
        self._rband_start_scene  = None
        self._rband_preview_item = None
        self._rband_label_item   = None   # QGraphicsTextItem in right-drag rband
        self._rband_dragging     = False
        self._place_rband_start_scene = None
        self._place_rband_preview_item = None
        self._place_rband_dragging = False

        # Label slot tracker: (qx, qy) → next free row index (reset on clear_overlays)
        self._label_slots: dict = {}
        self._pending_path_item = None

        # Markup overlay tracking: markup_id → list of QGraphicsItems
        self._markup_items: dict = {}
        self._markup_highlighted: int = -1
        self._snap_enabled: bool = True
        self._markup_types: dict   = {}   # mu_id → 'polygon'|'polyline'|'text'|'comment'
        # Red markup overlay tracking (separate from node markup)
        self._red_markup_items: dict = {}
        self._red_markup_types: dict = {}
        self._edit_mu_id            = None
        self._vertex_handles: list  = []
        self._drag_mode             = None   # 'vertex' | 'item' | 'symbol_transform' | None
        self._drag_vertex_idx       = None
        self._drag_start_scene      = None
        self._drag_original_pts: list = []
        self._drag_current_pts: list  = []
        self._drag_threshold_exceeded = False
        self._drag_item_origins: list = []  # [(QGraphicsItem, QPointF)] for text/comment
        # Symbol resize/rotate handles and live state
        self._corner_handles: list   = []
        self._rot_handle              = None   # QGraphicsEllipseItem
        self._rot_handle_line         = None   # QGraphicsLineItem
        self._symbol_bbox_proxy       = None   # QGraphicsPathItem (live preview)
        self._symbol_drag_mode        = None   # 'nw'|'ne'|'sw'|'se'|'rotate'
        self._symbol_orig_w           = 40.0
        self._symbol_orig_h           = 40.0
        self._symbol_orig_rot         = 0.0
        self._symbol_orig_center      = None   # QPointF scene coords
        self._symbol_live_w           = 40.0
        self._symbol_live_h           = 40.0
        self._symbol_live_rot         = 0.0
        self._inline_edit_widget = None

        # "Smart polylinje" state (_smart_*) removed 2026-08-26 along with
        # MODE_SMART_POLYLINE/SmartPipeTracer -- see archive/smart_pipe_tracer.py.

        # Study board: multi-page layout
        self._all_page_items: dict  = {}   # page_idx → QGraphicsPixmapItem
        self._page_offsets: dict    = {}   # page_idx → (ox: float, oy: float)
        self._page_widths_pdf: dict = {}   # page_idx → float PDF width
        self._page_heights_pdf: dict = {}  # page_idx → float PDF height
        self._dragging_page         = None  # page_idx being dragged in MODE_BOARD_LAYOUT
        self._drag_page_start_scene = None  # QPointF where drag began
        self._drag_page_orig_offset = None  # (ox, oy) before drag
        self._add_link_source_page  = None  # page_idx chosen in MODE_ADD_SHEET_LINK

        self._lod_overview = None   # None = unset; True/False = current LOD

        self._placeholder = None
        self._show_placeholder("Öppna en P&ID-fil (PDF) för att börja.")
        self.set_mode(MODE_NAV)
        self.setBackgroundBrush(QBrush(QColor(160, 160, 160)))

    def _show_placeholder(self, text):
        self._clear_placeholder()
        item = self._scene.addSimpleText(text)
        f = QFont(); f.setPointSize(14)
        item.setFont(f)
        item.setBrush(QBrush(QColor(70, 70, 70)))
        item.setZValue(Z_TEMP)
        self._placeholder = item
        self._scene.setSceneRect(item.boundingRect().adjusted(-40, -40, 40, 40))

    def _clear_placeholder(self):
        if self._placeholder is not None:
            try:
                self._scene.removeItem(self._placeholder)
            except Exception:
                pass
            self._placeholder = None

    def __del__(self):
        """Ensure PDF document is properly closed on object destruction."""
        if hasattr(self, 'pdf_doc') and self.pdf_doc:
            try:
                self.pdf_doc.close()
            except Exception:
                pass
            self.pdf_doc = None

    def set_min_pdf_line_width(self, width):
        """Set the display-only thin-line enhancement in source pixels.

        Existing raster caches are discarded because their pixels were made
        with the previous setting. The caller is responsible for invoking
        ``_render_all_pages`` afterwards when a document is open.
        """
        try:
            width = max(0, min(4, int(width or 0)))
        except (TypeError, ValueError):
            width = 0
        if width == self._min_pdf_line_width:
            return False
        self._min_pdf_line_width = width
        self._cancel_prefetch()
        self._cancel_lod_render()
        self._page_cache.clear()
        self._page_cache_scale.clear()
        self._cache_order.clear()
        self._page_display_scale.clear()
        return True

    def load_pdf(self, path, page=0, layout_offsets=None, active_pages=None,
                 progress_cb=None, page_rotations=None):
        if not HAS_PYMUPDF:
            self._show_placeholder("Installera PyMuPDF:\n  pip install PyMuPDF")
            return False
        try:
            self.pdf_doc = fitz.open(str(path))
        except Exception as e:
            self._show_placeholder(f"Kunde inte öppna PDF:\n{e}")
            self.pdf_doc = None
            return False
        if self.pdf_doc.page_count == 0:
            self._show_placeholder("PDF saknar sidor.")
            return False
        # A fresh document has no rotation state carried over from whatever
        # was previously open — reset, then repopulate from the caller's
        # persisted overrides (PIDPanel passes db.get_all_page_rotations()).
        self._page_rotation_override.clear()
        self._intrinsic_rotation.clear()
        if page_rotations:
            self._page_rotation_override.update(
                {int(k): int(v) % 360 for k, v in page_rotations.items()})
        self._pdf_path = str(path)
        self._page_cache.clear()
        self._page_cache_scale.clear()
        self._cache_order.clear()
        self._cancel_prefetch()
        self.current_page = max(0, min(page, self.pdf_doc.page_count - 1))
        self._render_all_pages(layout_offsets=layout_offsets, active_pages=active_pages,
                               progress_cb=progress_cb)
        return True

    def _render_page(self):
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        page = self.pdf_doc.load_page(self.current_page)
        rect = page.rect
        self.page_rect_width  = float(rect.width)
        self.page_rect_height = float(rect.height)

        if self.page_item is not None:
            try:
                self._scene.removeItem(self.page_item)
            except Exception:
                pass
            self.page_item = None

        pn = self.current_page
        if pn in self._page_cache:
            pixmap = self._page_cache[pn]
            self._update_lru(pn)
        else:
            mat = fitz.Matrix(self._RASTER_SCALE, self._RASTER_SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            raw, width, height, stride = _apply_min_pdf_line_width(
                pix, self._min_pdf_line_width)
            img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(img.copy())
            self._add_to_cache(pn, pixmap)

        self.page_item = QGraphicsPixmapItem(pixmap)
        self.page_item.setZValue(Z_PAGE)
        self.page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self.page_item)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.render_scale = self._RASTER_SCALE

        self._prefetch_adjacent()

    def _render_all_pages(self, layout_offsets=None, active_pages=None,
                          progress_cb=None):
        """Render PDF pages (only active_pages if given) as tiled scene items."""
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        self._cancel_lod_render()
        for item in list(self._all_page_items.values()):
            try: self._scene.removeItem(item)
            except RuntimeError as e: logging.warning(f"Failed to remove page item from scene: {e}")
        self._all_page_items.clear()
        self.page_item = None  # Clear reference to avoid double-removal errors
        self._low_pixmaps.clear()
        self._hires_pages.clear()
        self._page_display_scale.clear()
        self._page_offsets.clear()
        self._page_widths_pdf.clear()
        self._page_heights_pdf.clear()
        self.render_scale = self._RASTER_SCALE
        GAP = 100.0  # scene pixels between pages

        pages_to_render = (sorted(active_pages)
                           if active_pages is not None
                           else list(range(self.pdf_doc.page_count)))
        total_pages = len(pages_to_render)
        x_cursor = 0.0
        for render_idx, pn in enumerate(pages_to_render):
            fitz_page = self.pdf_doc.load_page(pn)
            # Compose the user's manual rotation override (if any) with the
            # PDF's own /Rotate BEFORE reading rect/rendering — every
            # coordinate/pixmap derived below (page.rect, get_pixmap()) then
            # reflects it automatically. See _apply_page_rotation().
            self._apply_page_rotation(pn)
            rect = fitz_page.rect
            pw_pdf = float(rect.width)
            ph_pdf = float(rect.height)
            pw_scene = pw_pdf * self.render_scale
            self._page_widths_pdf[pn] = pw_pdf
            self._page_heights_pdf[pn] = ph_pdf

            # Low-res base render: ~36× fewer pixels than _RASTER_SCALE.
            # The item is scaled up so the scene footprint (and every saved
            # coordinate) stays identical to a full-res render.
            mat = fitz.Matrix(self._LOW_SCALE, self._LOW_SCALE)
            pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
            raw, width, height, stride = _apply_min_pdf_line_width(
                pix, self._min_pdf_line_width)
            img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(img.copy())
            self._low_pixmaps[pn] = pixmap

            if layout_offsets and pn in layout_offsets:
                ox, oy = float(layout_offsets[pn][0]), float(layout_offsets[pn][1])
            else:
                ox = x_cursor
                oy = 0.0
            x_cursor = ox + pw_scene + GAP
            self._page_offsets[pn] = (ox, oy)

            page_item = QGraphicsPixmapItem(pixmap)
            page_item.setZValue(Z_PAGE)
            page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            page_item.setScale(self.render_scale / self._LOW_SCALE)
            page_item.setPos(ox, oy)
            self._scene.addItem(page_item)
            self._all_page_items[pn] = page_item

            if progress_cb is not None:
                progress_cb(render_idx + 1, total_pages)
                QApplication.processEvents()

        self.page_item = self._all_page_items.get(self.current_page)
        self.page_rect_width  = self._page_widths_pdf.get(self.current_page, 0.0)
        self.page_rect_height = self._page_heights_pdf.get(self.current_page, 0.0)
        self._update_board_scene_rect()
        self._schedule_lod_update()

    # ── Manual per-page rotation (2026-08-12, see NOTES.md) ───────────────────

    def _apply_page_rotation(self, pn):
        """Compose the user's manual rotation override for physical page `pn`
        with the PDF's own intrinsic /Rotate value, by mutating the
        in-memory fitz.Page's rotation (never written back to the file).

        Every existing rotation-aware code path — page.rect (hence
        page_rect_width/height and the scene footprint), get_pixmap(),
        rotation_matrix/derotation_matrix, and
        equipment_detection._rotate_words — already keys off page.rotation,
        so composing here makes the override transparent to all of them for
        free instead of threading a separate override through each layer.
        """
        if self.pdf_doc is None:
            return
        page = self.pdf_doc.load_page(pn)
        if pn not in self._intrinsic_rotation:
            self._intrinsic_rotation[pn] = page.rotation
        total = (self._intrinsic_rotation[pn] + self._page_rotation_override.get(pn, 0)) % 360
        if page.rotation != total:
            page.set_rotation(total)

    def set_page_rotation_override(self, pn, degrees):
        """Set/replace the manual rotation override (clockwise degrees,
        normalized to 0/90/180/270) for physical page `pn`. Purges any
        cached/displayed raster pixmaps for this page since they were
        rendered at the old orientation and footprint — callers still need
        to re-render (e.g. via _render_all_pages) to see the new page."""
        degrees = int(degrees) % 360
        self._page_rotation_override[pn] = degrees
        self._apply_page_rotation(pn)
        self._page_cache.pop(pn, None)
        self._page_cache_scale.pop(pn, None)
        if pn in self._cache_order:
            self._cache_order.remove(pn)
        self._hires_pages.discard(pn)
        self._page_display_scale.pop(pn, None)
        self._low_pixmaps.pop(pn, None)

    def _update_board_scene_rect(self):
        if not self._page_offsets:
            return
        rs = self.render_scale
        min_x = min(ox for ox, oy in self._page_offsets.values()) - 40
        min_y = min(oy for ox, oy in self._page_offsets.values()) - 40
        max_x = max(ox + self._page_widths_pdf[p]  * rs
                    for p, (ox, oy) in self._page_offsets.items()) + 40
        max_y = max(oy + self._page_heights_pdf[p] * rs
                    for p, (ox, oy) in self._page_offsets.items()) + 40
        self._scene.setSceneRect(QRectF(min_x, min_y, max_x - min_x, max_y - min_y))

    def _cancel_prefetch(self):
        if self._prefetch_thread and self._prefetch_thread.isRunning():
            self._prefetch_thread.requestInterruption()
            self._prefetch_thread.wait(300)

    def _add_to_cache(self, pn, pixmap, scale=None):
        if pn in self._page_cache:
            self._cache_order.remove(pn)
        elif len(self._page_cache) >= self._CACHE_SIZE:
            oldest = self._cache_order.pop(0)
            del self._page_cache[oldest]
            self._page_cache_scale.pop(oldest, None)
        self._page_cache[pn] = pixmap
        self._page_cache_scale[pn] = scale if scale is not None else self._RASTER_SCALE
        self._cache_order.append(pn)

    def _update_lru(self, pn):
        if pn in self._cache_order:
            self._cache_order.remove(pn)
            self._cache_order.append(pn)

    def _prefetch_adjacent(self):
        if not self._pdf_path or self.pdf_doc is None:
            return
        total = self.pdf_doc.page_count
        to_fetch = []
        for offset in (1, -1, 2, -2):
            n = self.current_page + offset
            if 0 <= n < total and n not in self._page_cache:
                to_fetch.append(n)
                if len(to_fetch) >= 2:
                    break
        if not to_fetch:
            return
        self._cancel_prefetch()
        rotations = {n: (self._intrinsic_rotation.get(n, 0) +
                         self._page_rotation_override.get(n, 0)) % 360
                    for n in to_fetch if n in self._page_rotation_override}
        self._prefetch_thread = _PageRenderer(
            self._pdf_path, to_fetch, self._RASTER_SCALE,
            rotations=rotations, min_line_width=self._min_pdf_line_width)
        self._prefetch_thread.page_ready.connect(self._on_page_prefetched)
        self._prefetch_thread.start()

    def _on_page_prefetched(self, pn, raw, width, height, stride, scale):
        if pn not in self._page_cache:
            img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
            self._add_to_cache(pn, QPixmap.fromImage(img), scale)

    # ── Page LOD: hi-res swap-in for visible pages at high zoom ──────────────

    def _schedule_lod_update(self):
        """Debounced trigger — call after any zoom/pan/layout change."""
        if self._all_page_items:
            self._lod_timer.start()

    def _cancel_lod_render(self):
        if self._lod_renderer and self._lod_renderer.isRunning():
            self._lod_renderer.requestInterruption()
            self._lod_renderer.wait(300)
        self._lod_renderer = None

    def _hires_worthwhile(self):
        # Hi-res pays off once one low-res pixel covers more than _LOD_MARGIN
        # screen pixels — i.e. the low tier can no longer supply ~1 raw pixel
        # per screen pixel with a small supersampling margin.
        zoom = self.transform().m11()
        return zoom > self._LOD_MARGIN * self._LOW_SCALE / self._RASTER_SCALE

    def _target_raster_scale(self, pw_pdf, ph_pdf):
        """Adaptive re-rasterization (2026-08-12, see NOTES.md "P&ID blir
        suddig vid inzoomning"): the fitz.Matrix scale a page should be
        re-rendered at so its raster stays crisp at the CURRENT view zoom,
        instead of a fixed _RASTER_SCALE that the QGraphicsView transform
        then stretches (blur) once zoom exceeds what it can supply.

        Uses the same "at least _LOD_MARGIN raw pixels per screen pixel"
        standard as _hires_worthwhile(), just applied one tier further up.
        render_scale (the scene-units-per-pdf-point factor every marker/
        click-hit-test coordinate transform assumes, see scene_to_pdf/
        pdf_to_scene) is NEVER changed by this — only which physical
        pixmap resolution is displayed for that same scene footprint
        (see _promote_page's item.setScale compensation).

        Clamped to two safety caps so an extreme zoom can't render an
        absurdly large pixmap: an absolute multiplier (_MAX_RASTER_SCALE)
        and a cap on the resulting pixel dimensions (_MAX_RASTER_DIM),
        whichever is more restrictive for this page's size. Never goes
        below the baseline _RASTER_SCALE.
        """
        zoom = self.transform().m11()
        ideal = zoom * self.render_scale / self._LOD_MARGIN
        ideal = max(ideal, self._RASTER_SCALE)
        dim_cap = self._MAX_RASTER_DIM / max(pw_pdf, ph_pdf, 1.0)
        cap = max(min(self._MAX_RASTER_SCALE, dim_cap), self._RASTER_SCALE)
        return min(ideal, cap)

    # A target scale must exceed what's currently shown/cached by this factor
    # before triggering a re-render — cheap hysteresis so a page that's
    # already crisp enough (or slightly over-crisp from a previous, higher
    # zoom level) isn't re-rendered on every debounce tick.
    _RESCALE_HYSTERESIS = 1.15

    def _update_page_lod(self):
        if not self._all_page_items or self.pdf_doc is None:
            return
        needed = []
        if self._hires_worthwhile():
            vis = self.mapToScene(self.viewport().rect()).boundingRect()
            vis = vis.adjusted(-vis.width() * 0.25, -vis.height() * 0.25,
                               vis.width() * 0.25,  vis.height() * 0.25)
            c = vis.center()
            for pn, item in self._all_page_items.items():
                r = item.sceneBoundingRect()
                if r.intersects(vis):
                    d = abs(r.center().x() - c.x()) + abs(r.center().y() - c.y())
                    needed.append((d, pn))
            needed.sort()
            needed = [pn for _, pn in needed[:self._MAX_HIRES]]
        needed_set = set(needed)

        # Demote pages that left the viewport or are no longer zoomed in
        for pn in list(self._hires_pages):
            if pn not in needed_set:
                item = self._all_page_items.get(pn)
                low  = self._low_pixmaps.get(pn)
                if item is not None and low is not None:
                    item.setPixmap(low)
                    item.setScale(self.render_scale / self._LOW_SCALE)
                self._hires_pages.discard(pn)
                self._page_display_scale.pop(pn, None)

        # Promote/upgrade: a page needs (re-)rendering when what's currently
        # shown (or cached) can't supply the target resolution for the
        # CURRENT zoom — not just once, on the initial low->hires swap.
        to_render = []
        target_scales = {}
        for pn in needed:
            pw = self._page_widths_pdf.get(pn, 0.0)
            ph = self._page_heights_pdf.get(pn, 0.0)
            target = self._target_raster_scale(pw, ph)
            target_scales[pn] = target
            displayed = self._page_display_scale.get(pn, 0.0) if pn in self._hires_pages else 0.0
            if displayed and displayed >= target / self._RESCALE_HYSTERESIS:
                continue  # already crisp enough on screen — nothing to do
            cached_scale = self._page_cache_scale.get(pn, 0.0)
            if pn in self._page_cache and cached_scale >= target / self._RESCALE_HYSTERESIS:
                self._promote_page(pn, self._page_cache[pn], cached_scale)
                self._update_lru(pn)
            else:
                to_render.append(pn)
        if to_render and self._pdf_path:
            # One fitz.Matrix scale per background-render batch — use the
            # smallest of the individually-needed (and already capped)
            # target scales so every page in the batch stays within its own
            # safety cap even if page sizes differ.
            batch_scale = max(min(target_scales[pn] for pn in to_render), self._RASTER_SCALE)
            rotations = {pn: (self._intrinsic_rotation.get(pn, 0) +
                              self._page_rotation_override.get(pn, 0)) % 360
                        for pn in to_render}
            self._cancel_lod_render()
            self._lod_renderer = _PageRenderer(
                self._pdf_path, to_render, batch_scale,
                rotations=rotations, min_line_width=self._min_pdf_line_width)
            self._lod_renderer.page_ready.connect(self._on_lod_page_ready)
            self._lod_renderer.start()

    def _promote_page(self, pn, pixmap, scale=None):
        item = self._all_page_items.get(pn)
        if item is None:
            return
        scale = scale if scale is not None else self._RASTER_SCALE
        item.setPixmap(pixmap)
        # Compensate so the item's scene footprint (render_scale per PDF
        # point) never changes regardless of which raster tier is shown —
        # this is what keeps scene_to_pdf/pdf_to_scene and every marker
        # coordinate correct across adaptive re-rasterization.
        item.setScale(self.render_scale / scale)
        self._hires_pages.add(pn)
        self._page_display_scale[pn] = scale

    def _on_lod_page_ready(self, pn, raw, width, height, stride, scale):
        img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
        pm  = QPixmap.fromImage(img)
        self._add_to_cache(pn, pm, scale)
        # Zoom may have changed while rendering — only promote if still useful;
        # a follow-up _update_page_lod demotes/upgrades anything now stale.
        if self._hires_worthwhile():
            self._promote_page(pn, pm, scale)

    def page_count(self):
        return self.pdf_doc.page_count if self.pdf_doc else 0

    def goto_page(self, n):
        if self.pdf_doc is None:
            return
        n = max(0, min(n, self.pdf_doc.page_count - 1))
        self.current_page = n
        self.page_item = self._all_page_items.get(n)
        self.page_rect_width  = self._page_widths_pdf.get(n, 0.0)
        self.page_rect_height = self._page_heights_pdf.get(n, 0.0)
        self._cancel_drawing()
        # Pre-extract PDF lines for smart snapping (background cache)
        QTimer.singleShot(CONFIG['TIMER_PDF_EXTRACT_MS'],
                         partial(self._extract_pdf_lines_for_page, n))
        if n in self._page_offsets:
            ox, oy = self._page_offsets[n]
            rs = self.render_scale
            cx = ox + self._page_widths_pdf.get(n, 0.0) * rs / 2
            cy = oy + self._page_heights_pdf.get(n, 0.0) * rs / 2
            self.centerOn(QPointF(cx, cy))
            self._schedule_lod_update()
        else:
            self._render_all_pages()

    def scene_to_pdf(self, point):
        p = self._hit_test_page(point)
        ox, oy = self._page_offsets.get(p, (0.0, 0.0))
        rs = self.render_scale
        return ((point.x() - ox) / rs, (point.y() - oy) / rs)

    def pdf_to_scene(self, x, y, page=None):
        p = page if page is not None else self.current_page
        ox, oy = self._page_offsets.get(p, (0.0, 0.0))
        return QPointF(x * self.render_scale + ox, y * self.render_scale + oy)

    def _hit_test_page(self, scene_pt):
        """Return the page index whose rendered area contains scene_pt, or current_page."""
        rs = self.render_scale
        for pn, (ox, oy) in self._page_offsets.items():
            pw = self._page_widths_pdf.get(pn, 0) * rs
            ph = self._page_heights_pdf.get(pn, 0) * rs
            if ox <= scene_pt.x() < ox + pw and oy <= scene_pt.y() < oy + ph:
                return pn
        return self.current_page

    def _purge_rubber_band_state(self):
        """Remove any dangling rubber-band scene items and reset drag state.

        Called from both set_mode() and clear_overlays() so there is a single
        canonical cleanup location; add new rubber-band attributes here only.
        """
        for attr in ('_rband_preview_item', '_rband_label_item',
                     '_place_rband_preview_item',
                     '_ctrl_rband_preview_item', '_ctrl_rband_count_label'):
            item = getattr(self, attr, None)
            if item is not None:
                try:
                    self._scene.removeItem(item)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._rband_start_scene      = None
        self._rband_dragging         = False
        self._place_rband_start_scene = None
        self._place_rband_dragging = False
        self._ctrl_rband_start_scene = None
        self._ctrl_rband_dragging    = False

    def set_mode(self, mode):
        self._purge_rubber_band_state()

        self.mode = mode
        if mode == MODE_NAV:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE,
                      MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_MARKUP_SELECT:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._clear_edit_handles()
        elif mode == MODE_PICK_REF_TAG:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_ANNOTATION:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_PLACE_EQUIPMENT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_EDIT_EQUIPMENT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_RED_MARKUP_SYMBOL:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_BOARD_LAYOUT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        elif mode == MODE_ADD_SHEET_LINK:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        # Cancel any in-progress freehand draw when leaving draw-modes.
        staying_in_draw = mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE)
        if not staying_in_draw:
            self._cancel_drawing()
        self.setFocus()

    def set_pen_style(self, color, width, alpha):
        c = QColor(color)
        self.draw_pen = QPen(QColor(c.red(), c.green(), c.blue(), alpha), width)
        # Keep the live drawing pen in the same scene-unit coordinate system
        # as the persisted markup overlay.  A cosmetic pen stays a fixed
        # screen width while the reloaded polyline scales with the scene,
        # making width (and the perceived alpha at different zoom) jump as
        # soon as drawing mode is left.
        self.draw_pen.setCosmetic(False)
        self.draw_brush = QBrush(QColor(c.red(), c.green(), c.blue(), max(30, alpha // 4)))

    def set_snap(self, enabled: bool):
        self._snap_enabled = enabled

    def _add_draw_point(self, sp):
        self.draw_points.append(sp)
        r = max(4.0, self.draw_pen.widthF() * 0.6)
        dot = self._scene.addEllipse(
            sp.x() - r, sp.y() - r, 2 * r, 2 * r,
            QPen(Qt.PenStyle.NoPen), QBrush(self.draw_pen.color())
        )
        dot.setZValue(Z_TEMP)
        self.temp_items.append(dot)
        if len(self.draw_points) >= 2:
            p0  = self.draw_points[-2]
            seg = self._scene.addLine(p0.x(), p0.y(), sp.x(), sp.y(), self.draw_pen)
            seg.setZValue(Z_TEMP)
            self.temp_items.append(seg)

    def _update_rubber_band(self, sp):
        if not self.draw_points:
            return
        last = self.draw_points[-1]
        pen  = QPen(self.draw_pen); pen.setStyle(Qt.PenStyle.DashLine)
        if self.rubber_line is None:
            self.rubber_line = self._scene.addLine(
                last.x(), last.y(), sp.x(), sp.y(), pen)
            self.rubber_line.setZValue(Z_TEMP)
        else:
            self.rubber_line.setPen(pen)
            self.rubber_line.setLine(last.x(), last.y(), sp.x(), sp.y())

    def _finish_drawing(self):
        """Legacy finish for MODE_NODE — creates node boundary polygon."""
        self._finish_markup_drawing()

    def _finish_markup_drawing(self):
        """Finish drawing for MODE_NODE, MARKUP_POLYGON and MARKUP_POLYLINE."""
        if len(self.draw_points) < 2:
            self._cancel_drawing()
            return
        mode = self.mode
        path = QPainterPath()
        path.moveTo(self.draw_points[0])
        for pt in self.draw_points[1:]:
            path.lineTo(pt)
        if mode in (MODE_NODE, MODE_MARKUP_POLYGON):
            path.closeSubpath()

        pdf_points = [list(self.scene_to_pdf(pt)) for pt in self.draw_points]
        self._remove_temp_items()

        item = QGraphicsPathItem(path)
        item.setPen(self.draw_pen)
        item.setBrush(self.draw_brush if mode in (MODE_NODE, MODE_MARKUP_POLYGON)
                      else QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(Z_OVERLAY)
        self._scene.addItem(item)
        self._pending_path_item = item

        self.draw_points = []
        if mode == MODE_NODE:
            self.node_markup_finished.emit(pdf_points, self.current_page)
        elif mode == MODE_MARKUP_POLYGON:
            self.markup_draw_finished.emit('polygon', pdf_points, self.current_page)
        elif mode == MODE_MARKUP_POLYLINE:
            self.markup_draw_finished.emit('polyline', pdf_points, self.current_page)

    def _cancel_drawing(self):
        self._remove_temp_items()
        self.draw_points = []

    def _remove_temp_items(self):
        for item in self.temp_items:
            try: self._scene.removeItem(item)
            except RuntimeError as e: logging.warning(f"Failed to remove temp item from scene: {e}")
        self.temp_items = []
        if self.rubber_line is not None:
            try: self._scene.removeItem(self.rubber_line)
            except RuntimeError as e: logging.warning(f"Failed to remove rubber line from scene: {e}")
            self.rubber_line = None

    def add_node_overlay(self, node_id, points_pdf, style, label):
        if not points_pdf:
            return
        self._pending_path_item = None
        color = QColor(style.get('color', '#FF8C00'))
        width = int(style.get('width', 3))
        alpha = int(style.get('alpha', 220))
        pen   = QPen(QColor(color.red(), color.green(), color.blue(), alpha), width)
        pen.setCosmetic(True)
        brush = QBrush(QColor(color.red(), color.green(), color.blue(), max(25, alpha // 5)))

        path = QPainterPath()
        first = self.pdf_to_scene(*points_pdf[0])
        path.moveTo(first)
        for x, y in points_pdf[1:]:
            path.lineTo(self.pdf_to_scene(x, y))
        path.closeSubpath()

        item = QGraphicsPathItem(path)
        item.setPen(pen); item.setBrush(brush); item.setZValue(Z_OVERLAY)
        item.setToolTip(label or '')
        self._scene.addItem(item)

        if label:
            cx = sum(p[0] for p in points_pdf) / len(points_pdf)
            cy = sum(p[1] for p in points_pdf) / len(points_pdf)
            center = self.pdf_to_scene(cx, cy)
            txt = QGraphicsSimpleTextItem(label)
            f = QFont(); f.setBold(True); f.setPointSize(11)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor(30, 30, 30)))
            br = txt.boundingRect()
            txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
            txt.setZValue(Z_OVERLAY + 1)
            self._scene.addItem(txt)

    # ── Node markup overlays ──────────────────────────────────────────────────

    def add_markup_overlay(self, mu_id, type_, points_pdf, label,
                           color_hex, opacity, line_width, visible=True, font_size=12,
                           opaque_fill=False, symbol_svg=None,
                           symbol_w=40, symbol_h=40, symbol_rot=0,
                           _items_dict=None):
        """Render a node_markup or red_markup item.  type_: polygon|polyline|text|comment|symbol"""
        if _items_dict is None:
            _items_dict = self._markup_items
        items = []
        c = QColor(color_hex)
        border_alpha = int(opacity * 210)
        fill_alpha   = int(opacity * 210) if opaque_fill else int(opacity * 52)
        # Non-cosmetic pen: width in scene coords so lines scale proportionally with zoom
        pen = QPen(QColor(c.red(), c.green(), c.blue(), border_alpha), line_width)

        if type_ in ('polygon', 'polyline') and len(points_pdf) >= 2:
            path = QPainterPath()
            first = self.pdf_to_scene(*points_pdf[0])
            path.moveTo(first)
            for p in points_pdf[1:]:
                path.lineTo(self.pdf_to_scene(*p))
            if type_ == 'polygon':
                path.closeSubpath()
                brush = QBrush(QColor(c.red(), c.green(), c.blue(), fill_alpha))
            else:
                brush = QBrush(Qt.BrushStyle.NoBrush)
            gi = QGraphicsPathItem(path)
            gi.setPen(pen); gi.setBrush(brush)
            gi.setZValue(Z_OVERLAY)
            gi.setData(self._DATA_MARKUP_ID, mu_id)
            gi.setData(self._DATA_TYPE, 'markup')
            gi.setData(self._DATA_MARKUP_PTS, points_pdf)
            gi.setCursor(Qt.CursorShape.PointingHandCursor)
            gi.setToolTip(f"Klicka för att markera  [{type_}]" + (f": {label}" if label else ""))
            self._scene.addItem(gi)
            items.append(gi)
            if label and type_ == 'polygon':
                cx = sum(p[0] for p in points_pdf) / len(points_pdf)
                cy = sum(p[1] for p in points_pdf) / len(points_pdf)
                items.extend(self._add_markup_label(mu_id, label, cx, cy, c, border_alpha,
                                                    font_size))

        elif type_ in ('text', 'comment') and len(points_pdf) >= 1:
            px, py = points_pdf[0]
            items.extend(self._add_markup_text_item(mu_id, type_, label or '?',
                                                     px, py, c, opacity, line_width, font_size))
            if items:
                items[0].setData(self._DATA_MARKUP_PTS, [[points_pdf[0][0], points_pdf[0][1]]])

        elif type_ == 'symbol' and symbol_svg and len(points_pdf) >= 1:
            items.extend(self._add_markup_symbol_item(
                mu_id, symbol_svg, points_pdf[0], c, opacity,
                symbol_w, symbol_h, symbol_rot, label=label))
            if items:
                items[0].setData(self._DATA_MARKUP_PTS, [[points_pdf[0][0], points_pdf[0][1]]])

        _items_dict[mu_id] = items
        if _items_dict is self._markup_items:
            self._markup_types[mu_id] = type_
        else:
            self._red_markup_types[mu_id] = type_
        if not visible:
            for gi in items:
                gi.setVisible(False)

    def _add_markup_label(self, mu_id, label, cx_pdf, cy_pdf, color, alpha, font_size=12):
        center = self.pdf_to_scene(cx_pdf, cy_pdf)
        txt = QGraphicsSimpleTextItem(label)
        f = QFont(); f.setBold(True); f.setPointSize(font_size)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                   min(255, alpha + 30))))
        br = txt.boundingRect()
        txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
        txt.setZValue(Z_OVERLAY + 1)
        txt.setData(self._DATA_MARKUP_ID, mu_id)
        txt.setData(self._DATA_TYPE, 'markup')
        self._scene.addItem(txt)
        return [txt]

    def _add_markup_text_item(self, mu_id, type_, label, px_pdf, py_pdf,
                              color, opacity, line_width, font_size=12):
        pos = self.pdf_to_scene(px_pdf, py_pdf)
        txt = QGraphicsSimpleTextItem(label)
        f = QFont()
        if type_ == 'comment':
            f.setItalic(True)
        f.setPointSize(font_size)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                   int(opacity * 230))))
        br = txt.boundingRect()
        txt.setPos(pos.x(), pos.y())
        txt.setZValue(Z_OVERLAY + 1)
        txt.setData(self._DATA_MARKUP_ID, mu_id)
        txt.setData(self._DATA_TYPE, 'markup')
        items = []
        if type_ == 'comment':
            # Draw a slightly rounded rect behind the text
            pad = 5
            bg_alpha = int(opacity * 90)
            border_alpha = int(opacity * 200)
            bg = QGraphicsRectItem(pos.x() - pad, pos.y() - pad,
                                   br.width() + 2*pad, br.height() + 2*pad)
            bg.setPen(QPen(QColor(color.red(), color.green(), color.blue(), border_alpha),
                           max(1, line_width - 1)))
            bg.setBrush(QBrush(QColor(255, 255, 200, bg_alpha)))
            bg.setZValue(Z_OVERLAY)
            bg.setData(self._DATA_MARKUP_ID, mu_id)
            bg.setData(self._DATA_TYPE, 'markup')
            bg.setCursor(Qt.CursorShape.PointingHandCursor)
            self._scene.addItem(bg)
            items.append(bg)
        self._scene.addItem(txt)
        items.append(txt)
        return items

    def _line_segments_intersect(self, p1, p2, p3, p4):
        """Check if line segment p1-p2 intersects p3-p4. Return (True, intersection_point) or (False, None)."""
        x1, y1 = p1[0], p1[1]
        x2, y2 = p2[0], p2[1]
        x3, y3 = p3[0], p3[1]
        x4, y4 = p4[0], p4[1]

        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-10:
            return False, None

        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom

        if 0 <= t <= 1 and 0 <= u <= 1:
            ix = x1 + t * (x2 - x1)
            iy = y1 + t * (y2 - y1)
            return True, (ix, iy)
        return False, None

    def _get_boundary_crossings(self, boundary_polygon, pdf_lines):
        """Find all points where PDF lines cross the boundary polygon.
        Returns list of (crossing_point, line_seg) tuples."""
        crossings = []

        # Create boundary segments
        boundary_segs = []
        for i in range(len(boundary_polygon)):
            p1 = boundary_polygon[i]
            p2 = boundary_polygon[(i + 1) % len(boundary_polygon)]
            boundary_segs.append((p1, p2))

        # Find intersections
        for line_seg in pdf_lines:
            x0, y0, x1, y1 = line_seg
            for b_p1, b_p2 in boundary_segs:
                intersects, pt = self._line_segments_intersect((x0, y0), (x1, y1), b_p1, b_p2)
                if intersects and pt:
                    crossings.append((pt, line_seg))

        return crossings

    def _closest_point_on_line_segment(self, p, line_seg):
        """Find closest point on a line segment to point p. Returns (closest_point, distance)."""
        x, y = p.x(), p.y()
        x0, y0, x1, y1 = line_seg

        # Vector from start to end
        dx = x1 - x0
        dy = y1 - y0

        # If line segment is a point
        if dx == 0 and dy == 0:
            dist = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
            return QPointF(x0, y0), dist

        # Parameter t for the projection onto the line
        t = ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)
        t = max(0, min(1, t))  # Clamp to [0, 1] to stay on segment

        # Closest point on segment
        closest_x = x0 + t * dx
        closest_y = y0 + t * dy

        dist = ((x - closest_x) ** 2 + (y - closest_y) ** 2) ** 0.5
        return QPointF(closest_x, closest_y), dist

    def _extract_pdf_lines_for_page(self, page_num):
        """Extract all visible line segments from a PDF page for snapping.

        Was previously always returning [] — get_drawings() returns plain
        dicts, so the old hasattr(path, 'rects')/hasattr(path, 'lines')
        checks were always False. Now delegates to the shared primitive
        extractor in symbol_geometry.py (also used for equipment-symbol
        clustering), so there is one source of truth for "what vector
        geometry is really on this page".
        """
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return []
        if page_num in self._pdf_line_segments:
            return self._pdf_line_segments[page_num]

        try:
            page = self.pdf_doc[page_num]
            self._pdf_line_segments[page_num] = symbol_geometry.extract_line_segments(page)
        except Exception:
            self._pdf_line_segments[page_num] = []

        return self._pdf_line_segments[page_num]

    def _snap_to_nearest(self, scene_pos):
        """Smart snapping: PDF lines (priority), markup paths, markers, and in-progress points."""
        if not self._snap_enabled:
            return scene_pos
        SNAP_PX = 18.0
        best_dist = SNAP_PX
        best_pos = scene_pos

        # Convert scene position to PDF coordinates for line snapping
        pdf_pos = self.scene_to_pdf(scene_pos)
        pdf_x, pdf_y = pdf_pos[0], pdf_pos[1]
        pdf_pt = QPointF(pdf_x, pdf_y)

        # 1. PRIORITY: Snap to PDF drawing details (lines, rectangles) — MOST IMPORTANT
        pdf_lines = self._extract_pdf_lines_for_page(self.current_page)
        for line_seg in pdf_lines:
            closest_pdf_pt, dist = self._closest_point_on_line_segment(pdf_pt, line_seg)
            if dist < best_dist:
                best_dist = dist
                # Convert back to scene coordinates
                best_pos = self.pdf_to_scene(closest_pdf_pt.x(), closest_pdf_pt.y())

        # 2. Snap to existing node markup path points
        for mu_id, items in self._markup_items.items():
            for gi in items:
                if not isinstance(gi, QGraphicsPathItem):
                    continue
                path = gi.path()
                for i in range(path.elementCount()):
                    el = path.elementAt(i)
                    pt = QPointF(el.x, el.y)
                    dx = pt.x() - scene_pos.x()
                    dy = pt.y() - scene_pos.y()
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_pos = pt

        # 3. Snap to cause/consequence/safeguard marker centers
        for marker_type in ('cause', 'consequence', 'safeguard'):
            for item in self._type_items.get(marker_type, []):
                center = item.pos() + item.boundingRect().center()
                dx = center.x() - scene_pos.x()
                dy = center.y() - scene_pos.y()
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_pos = center

        # 4. Snap to in-progress draw points
        for pt in self.draw_points:
            dx = pt.x() - scene_pos.x()
            dy = pt.y() - scene_pos.y()
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_pos = pt

        return best_pos

    def clear_markup_overlays(self):
        """Remove all node markup overlay items from the scene."""
        self._clear_edit_handles()
        self._markup_types.clear()
        for mu_id, items in self._markup_items.items():
            for gi in items:
                try: self._scene.removeItem(gi)
                except RuntimeError as e: logging.warning(f"Failed to remove markup item from scene: {e}")
        self._markup_items.clear()
        self._markup_highlighted = -1

    def set_markup_item_visible(self, mu_id, visible):
        for gi in self._markup_items.get(mu_id, []):
            try: gi.setVisible(visible)
            except RuntimeError as e: logging.warning(f"Failed to set markup item visibility: {e}")

    # ── Red markup overlay methods ────────────────────────────────────────────

    def add_red_markup_overlay(self, mu_id, type_, points_pdf, label,
                               color_hex, opacity, line_width, visible=True, font_size=12,
                               symbol_w=40, symbol_h=40, symbol_rot=0):
        """Render a red markup item (same as node markup but fully filled and supports symbols)."""
        svg = _get_red_symbol_svg(label) if type_ == 'symbol' else None
        self.add_markup_overlay(
            mu_id, type_, points_pdf, label, color_hex, opacity, line_width,
            visible, font_size, opaque_fill=True, symbol_svg=svg,
            symbol_w=symbol_w, symbol_h=symbol_h, symbol_rot=symbol_rot,
            _items_dict=self._red_markup_items)

    def clear_red_markup_overlays(self):
        """Remove all red markup overlay items from the scene."""
        self._clear_edit_handles()
        self._red_markup_types.clear()
        for mu_id, items in self._red_markup_items.items():
            for gi in items:
                try: self._scene.removeItem(gi)
                except RuntimeError as e: logging.warning(f"Failed to remove red markup item from scene: {e}")
        self._red_markup_items.clear()

    def set_red_markup_item_visible(self, mu_id, visible):
        for gi in self._red_markup_items.get(mu_id, []):
            try: gi.setVisible(visible)
            except RuntimeError as e: logging.warning(f"Failed to set red markup item visibility: {e}")

    def _add_markup_symbol_item(self, mu_id, svg_str, pos_pdf, color, opacity,
                                symbol_w=40, symbol_h=40, symbol_rot=0, label=''):
        """Render an SVG symbol at pos_pdf with given PDF-unit size and rotation."""
        if not HAS_SVG_RENDERER or QSvgRenderer is None:
            return []
        # Replace placeholder color in SVG with user's chosen color
        colored_svg = svg_str.replace('"red"', f'"{color.name()}"')
        colored_svg = colored_svg.replace("'red'", f"'{color.name()}'")
        renderer = QSvgRenderer()
        renderer.load(colored_svg.encode('utf-8'))
        if not renderer.isValid():
            return []
        # Compute scene coords and size
        scene_pos = self.pdf_to_scene(*pos_pdf)
        scene_pt2 = self.pdf_to_scene(pos_pdf[0] + symbol_w, pos_pdf[1] + symbol_h)
        sw = abs(scene_pt2.x() - scene_pos.x())
        sh = abs(scene_pt2.y() - scene_pos.y())
        pm = QPixmap(max(1, int(sw * 2)), max(1, int(sh * 2)))
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha_val = int(opacity * 255)
        painter.setOpacity(alpha_val / 255.0)
        renderer.render(painter)
        painter.end()
        gi = QGraphicsPixmapItem(pm)
        gi.setTransformOriginPoint(sw, sh)  # center
        gi.setRotation(symbol_rot)
        gi.setPos(scene_pos.x() - sw / 2, scene_pos.y() - sh / 2)
        gi.setScale(0.5)  # we rendered at 2× for crispness
        gi.setZValue(Z_OVERLAY)
        gi.setData(self._DATA_MARKUP_ID, mu_id)
        gi.setData(self._DATA_TYPE, 'red_markup')
        gi.setData(self._DATA_MARKUP_PTS, [[pos_pdf[0], pos_pdf[1]]])
        gi.setData(self._DATA_SYMBOL_W, float(symbol_w))
        gi.setData(self._DATA_SYMBOL_H, float(symbol_h))
        gi.setData(self._DATA_SYMBOL_ROT, float(symbol_rot))
        gi.setCursor(Qt.CursorShape.PointingHandCursor)
        gi.setToolTip(f"Symbol: {label}" if label else "P&ID-symbol")
        self._scene.addItem(gi)
        return [gi]

    def _clear_edit_handles(self):
        for h in self._vertex_handles:
            try: self._scene.removeItem(h)
            except RuntimeError as e: logging.warning(f"Failed to remove vertex handle: {e}")
        for h in self._corner_handles:
            try: self._scene.removeItem(h)
            except RuntimeError as e: logging.warning(f"Failed to remove corner handle: {e}")
        if self._rot_handle is not None:
            try: self._scene.removeItem(self._rot_handle)
            except RuntimeError as e: logging.warning(f"Failed to remove rotation handle: {e}")
            self._rot_handle = None
        if self._rot_handle_line is not None:
            try: self._scene.removeItem(self._rot_handle_line)
            except RuntimeError as e: logging.warning(f"Failed to remove rotation handle line: {e}")
            self._rot_handle_line = None
        if self._symbol_bbox_proxy is not None:
            try: self._scene.removeItem(self._symbol_bbox_proxy)
            except RuntimeError as e: logging.warning(f"Failed to remove symbol bounding box: {e}")
            self._symbol_bbox_proxy = None
        self._corner_handles          = []
        self._vertex_handles          = []
        self._edit_mu_id              = None
        self._drag_mode               = None
        self._drag_vertex_idx         = None
        self._drag_original_pts       = []
        self._drag_current_pts        = []
        self._drag_item_origins       = []
        self._drag_threshold_exceeded = False
        self._symbol_drag_mode        = None

    def _select_for_edit(self, mu_id):
        """Select a markup item and show vertex handles."""
        self._clear_edit_handles()
        self._edit_mu_id = mu_id

        # Look in both node markup and red markup dicts
        items_dict = (self._markup_items if mu_id in self._markup_items
                      else self._red_markup_items)
        types_dict = (self._markup_types if mu_id in self._markup_types
                      else self._red_markup_types)

        pts_pdf = None
        for gi in items_dict.get(mu_id, []):
            pts_pdf = gi.data(self._DATA_MARKUP_PTS)
            if pts_pdf:
                break
        if not pts_pdf:
            return

        pts_scene = [self.pdf_to_scene(*p) for p in pts_pdf]
        self._drag_current_pts = list(pts_scene)

        typ = types_dict.get(mu_id, 'polygon')

        if typ in ('polygon', 'polyline'):
            HANDLE_R = 5
            for pt in pts_scene:
                h = QGraphicsEllipseItem(-HANDLE_R, -HANDLE_R, HANDLE_R * 2, HANDLE_R * 2)
                h.setBrush(QBrush(QColor(255, 255, 255, 220)))
                h.setPen(QPen(QColor(30, 120, 230), 1.5))
                h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                h.setPos(pt)
                h.setZValue(Z_OVERLAY + 5)
                self._scene.addItem(h)
                self._vertex_handles.append(h)
        elif typ in ('text', 'comment'):
            self._drag_item_origins = [
                (gi, QPointF(gi.pos()))
                for gi in items_dict.get(mu_id, [])
            ]
        elif typ == 'symbol':
            sw_pdf = sh_pdf = rot_deg = None
            for gi in items_dict.get(mu_id, []):
                sw_pdf  = gi.data(self._DATA_SYMBOL_W)
                sh_pdf  = gi.data(self._DATA_SYMBOL_H)
                rot_deg = gi.data(self._DATA_SYMBOL_ROT)
                break
            sw_pdf  = float(sw_pdf  if sw_pdf  is not None else 40.0)
            sh_pdf  = float(sh_pdf  if sh_pdf  is not None else 40.0)
            rot_deg = float(rot_deg if rot_deg is not None else 0.0)
            cx, cy = pts_scene[0].x(), pts_scene[0].y()
            self._symbol_orig_w      = sw_pdf
            self._symbol_orig_h      = sh_pdf
            self._symbol_orig_rot    = rot_deg
            self._symbol_orig_center = QPointF(cx, cy)
            self._symbol_live_w      = sw_pdf
            self._symbol_live_h      = sh_pdf
            self._symbol_live_rot    = rot_deg
            self._add_symbol_handles(cx, cy, sw_pdf, sh_pdf, rot_deg)
            self._drag_item_origins = [
                (gi, QPointF(gi.pos()))
                for gi in items_dict.get(mu_id, [])
            ]

        self.highlight_markup(mu_id)

    # ── Symbol resize/rotate helpers ─────────────────────────────────────────

    def _sym_rpt(self, cx, cy, lx, ly, rot_deg):
        """Rotate local (lx,ly) by rot_deg around (cx,cy) in screen coords."""
        a = math.radians(rot_deg)
        ca, sa = math.cos(a), math.sin(a)
        return QPointF(cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

    def _add_symbol_handles(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Create resize corner handles and rotation handle for a symbol."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        SZ = 5
        ROT_DIST = max(22.0, sh / 2 + 18.0)
        corners_local = [(-sw/2, -sh/2), (sw/2, -sh/2), (-sw/2, sh/2), (sw/2, sh/2)]
        for lx, ly in corners_local:
            pt = self._sym_rpt(cx, cy, lx, ly, rot_deg)
            h = QGraphicsRectItem(-SZ, -SZ, SZ * 2, SZ * 2)
            h.setBrush(QBrush(QColor(255, 140, 0, 220)))
            h.setPen(QPen(QColor(180, 80, 0), 1.5))
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPos(pt)
            h.setZValue(Z_OVERLAY + 6)
            self._scene.addItem(h)
            self._corner_handles.append(h)
        rot_pt = self._sym_rpt(cx, cy, 0, -ROT_DIST, rot_deg)
        line = QGraphicsLineItem(cx, cy, rot_pt.x(), rot_pt.y())
        line.setPen(QPen(QColor(100, 80, 200, 160), 1.5, Qt.PenStyle.DashLine))
        line.setZValue(Z_OVERLAY + 5)
        self._scene.addItem(line)
        self._rot_handle_line = line
        ROT_R = 6
        rh = QGraphicsEllipseItem(-ROT_R, -ROT_R, ROT_R * 2, ROT_R * 2)
        rh.setBrush(QBrush(QColor(100, 80, 200, 200)))
        rh.setPen(QPen(QColor(60, 40, 180), 1.5))
        rh.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        rh.setPos(rot_pt)
        rh.setZValue(Z_OVERLAY + 6)
        self._scene.addItem(rh)
        self._rot_handle = rh

    def _update_symbol_handles(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Reposition corner handles and rotation handle to match new symbol state."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        ROT_DIST = max(22.0, sh / 2 + 18.0)
        corners_local = [(-sw/2, -sh/2), (sw/2, -sh/2), (-sw/2, sh/2), (sw/2, sh/2)]
        for i, (lx, ly) in enumerate(corners_local):
            if i < len(self._corner_handles):
                self._corner_handles[i].setPos(self._sym_rpt(cx, cy, lx, ly, rot_deg))
        rot_pt = self._sym_rpt(cx, cy, 0, -ROT_DIST, rot_deg)
        if self._rot_handle is not None:
            self._rot_handle.setPos(rot_pt)
        if self._rot_handle_line is not None:
            self._rot_handle_line.setLine(cx, cy, rot_pt.x(), rot_pt.y())

    def _update_symbol_bbox_proxy(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Show/update the dashed bounding-box preview during resize/rotate."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        corners = [
            self._sym_rpt(cx, cy, -sw/2, -sh/2, rot_deg),
            self._sym_rpt(cx, cy,  sw/2, -sh/2, rot_deg),
            self._sym_rpt(cx, cy,  sw/2,  sh/2, rot_deg),
            self._sym_rpt(cx, cy, -sw/2,  sh/2, rot_deg),
        ]
        path = QPainterPath()
        path.moveTo(corners[0])
        for p in corners[1:]:
            path.lineTo(p)
        path.closeSubpath()
        if self._symbol_bbox_proxy is None:
            proxy = QGraphicsPathItem(path)
            proxy.setPen(QPen(QColor(255, 140, 0, 200), 2.0, Qt.PenStyle.DashLine))
            proxy.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            proxy.setZValue(Z_OVERLAY + 4)
            self._scene.addItem(proxy)
            self._symbol_bbox_proxy = proxy
        else:
            self._symbol_bbox_proxy.setPath(path)

    def _do_symbol_transform(self, sp):
        """Apply live resize or rotate of symbol during handle drag."""
        cx = self._symbol_orig_center.x()
        cy = self._symbol_orig_center.y()
        dx = sp.x() - cx
        dy = sp.y() - cy
        rs = self.render_scale
        if self._symbol_drag_mode == 'rotate':
            self._symbol_live_rot = math.degrees(math.atan2(dy, dx)) + 90.0
        else:
            a = math.radians(self._symbol_live_rot)
            ca, sa = math.cos(a), math.sin(a)
            lx =  dx * ca + dy * sa   # inverse rotation
            ly = -dx * sa + dy * ca
            min_pdf = 10.0
            if self._symbol_drag_mode == 'se':
                self._symbol_live_w = max(min_pdf, lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, ly * 2 / rs)
            elif self._symbol_drag_mode == 'nw':
                self._symbol_live_w = max(min_pdf, -lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, -ly * 2 / rs)
            elif self._symbol_drag_mode == 'ne':
                self._symbol_live_w = max(min_pdf, lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, -ly * 2 / rs)
            elif self._symbol_drag_mode == 'sw':
                self._symbol_live_w = max(min_pdf, -lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, ly * 2 / rs)
        self._update_symbol_bbox_proxy(
            cx, cy, self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)
        self._update_symbol_handles(
            cx, cy, self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)

    def _finish_symbol_transform(self):
        """Save new symbol dims/rotation, emit signal for DB save."""
        mu_id = self._edit_mu_id
        if mu_id is None:
            return
        # Update stored data on item so it survives until the next render
        items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                      else self._markup_items)
        for gi in items_dict.get(mu_id, []):
            gi.setData(self._DATA_SYMBOL_W,   self._symbol_live_w)
            gi.setData(self._DATA_SYMBOL_H,   self._symbol_live_h)
            gi.setData(self._DATA_SYMBOL_ROT, self._symbol_live_rot)
        self.markup_symbol_dims_changed.emit(
            mu_id,
            float(self._symbol_live_w),
            float(self._symbol_live_h),
            float(self._symbol_live_rot))
        self._clear_edit_handles()

    # ── Edit path / drag helpers ──────────────────────────────────────────────

    def _update_edit_path(self, pts_scene):
        """Rebuild the path/positions of the currently edited markup."""
        mu_id = self._edit_mu_id
        if mu_id not in self._markup_items and mu_id not in self._red_markup_items:
            return
        items_dict = (self._markup_items if mu_id in self._markup_items
                      else self._red_markup_items)
        types_dict = (self._markup_types if mu_id in self._markup_types
                      else self._red_markup_types)
        typ = types_dict.get(mu_id, 'polygon')

        if typ in ('polygon', 'polyline') and pts_scene:
            for gi in items_dict[mu_id]:
                if isinstance(gi, QGraphicsPathItem):
                    path = QPainterPath()
                    path.moveTo(pts_scene[0])
                    for pt in pts_scene[1:]:
                        path.lineTo(pt)
                    if typ == 'polygon':
                        path.closeSubpath()
                    gi.setPath(path)
                    break

        elif typ in ('text', 'comment') and pts_scene and self._drag_original_pts:
            delta = pts_scene[0] - self._drag_original_pts[0]
            for gi, orig in self._drag_item_origins:
                gi.setPos(orig + delta)

        elif typ == 'symbol' and pts_scene and self._drag_original_pts:
            delta = pts_scene[0] - self._drag_original_pts[0]
            for gi, orig in self._drag_item_origins:
                gi.setPos(orig + delta)
            # Also move the resize/rotate handles to follow the symbol
            self._update_symbol_handles(
                pts_scene[0].x(), pts_scene[0].y(),
                self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)

    def _update_handle_positions(self, pts_scene):
        for i, handle in enumerate(self._vertex_handles):
            if i < len(pts_scene):
                handle.setPos(pts_scene[i])

    def _finish_edit_drag(self):
        """Called on mouseRelease after a drag — save new points."""
        if self._edit_mu_id is None:
            return
        mu_id = self._edit_mu_id
        is_red = mu_id in self._red_markup_items
        items_dict = self._red_markup_items if is_red else self._markup_items
        types_dict = self._red_markup_types if is_red else self._markup_types
        new_pdf_pts = [list(self.scene_to_pdf(pt)) for pt in self._drag_current_pts]
        for gi in items_dict.get(mu_id, []):
            if gi.data(self._DATA_MARKUP_PTS) is not None:
                gi.setData(self._DATA_MARKUP_PTS, new_pdf_pts)
                break
        # For symbols moved via 'item' drag, keep _symbol_orig_center in sync
        if types_dict.get(mu_id) == 'symbol' and new_pdf_pts:
            self._symbol_orig_center = self.pdf_to_scene(*new_pdf_pts[0])
        self.markup_moved.emit(mu_id, new_pdf_pts)

    def _start_inline_label_edit(self, mu_id):
        """Show a floating QLineEdit over the text item for in-place label editing."""
        txt_item = None
        items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                      else self._markup_items)
        for gi in items_dict.get(mu_id, []):
            if isinstance(gi, QGraphicsSimpleTextItem):
                txt_item = gi
                break
        if txt_item is None:
            return
        if self._inline_edit_widget is not None:
            self._inline_edit_widget.deleteLater()
            self._inline_edit_widget = None

        vp = self.mapFromScene(txt_item.pos())
        br = txt_item.boundingRect()
        edit = QLineEdit(self.viewport())
        edit.setFont(txt_item.font())
        edit.setText(txt_item.text())
        edit.move(vp.x(), vp.y())
        edit.resize(max(160, int(br.width()) + 20), int(br.height()) + 6)
        edit.selectAll()
        edit.setStyleSheet(
            "background:white;border:2px solid #1e78e6;border-radius:2px;padding:1px;")
        edit.show()
        edit.setFocus()
        self._inline_edit_widget = edit
        committed = [False]

        def commit():
            if committed[0]:
                return
            committed[0] = True
            new_text = edit.text().strip() or edit.text()
            if new_text:
                self.markup_label_edited.emit(mu_id, new_text)
            edit.deleteLater()
            self._inline_edit_widget = None

        edit.returnPressed.connect(commit)
        edit.editingFinished.connect(commit)

    # "Smart polylinje" (SmartPipeTracer-backed path tracing/preview/
    # confirm/cancel methods) removed 2026-08-26 -- see NOTES.md and
    # archive/smart_pipe_tracer.py.

    def zoom_to_markup_items(self, mu_ids):
        """Zoom and pan the view to fit all given markup items."""
        combined = QRectF()
        for mu_id in mu_ids:
            items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                          else self._markup_items)
            for gi in items_dict.get(mu_id, []):
                br = gi.mapToScene(gi.boundingRect()).boundingRect()
                if combined.isNull():
                    combined = br
                else:
                    combined = combined.united(br)
        if not combined.isNull():
            combined.adjust(-60, -60, 60, 60)
            self.fitInView(combined, Qt.AspectRatioMode.KeepAspectRatio)
            self._apply_lod(self.transform().m11())
            self._schedule_lod_update()

    def contextMenuEvent(self, event):
        if self.mode == MODE_MARKUP_SELECT and self._edit_mu_id is not None:
            sp = self.mapToScene(event.pos())
            for gi in self._scene.items(sp):
                if gi.data(self._DATA_MARKUP_ID) is not None:
                    menu = QMenu(self)
                    menu.addAction(_icon('clipboard'), "Duplicera",
                                   partial(self.markup_duplicate_requested.emit, self._edit_mu_id))
                    menu.exec(event.globalPos())
                    event.accept()
                    return
        super().contextMenuEvent(event)

    def highlight_markup(self, mu_id):
        """Briefly pulse-highlight a markup item (thicken its border)."""
        if self._markup_highlighted == mu_id:
            return
        # Reset previous highlight - check BOTH dicts
        if self._markup_highlighted:
            prev_dict = None
            if self._markup_highlighted in self._red_markup_items:
                prev_dict = self._red_markup_items
            elif self._markup_highlighted in self._markup_items:
                prev_dict = self._markup_items
            if prev_dict:
                for gi in prev_dict[self._markup_highlighted]:
                    if isinstance(gi, (QGraphicsPathItem, QGraphicsRectItem)):
                        p = gi.pen()
                        p.setWidthF(max(1, p.widthF() - 2))
                        gi.setPen(p)
        self._markup_highlighted = mu_id
        # Highlight current - check BOTH dicts
        curr_dict = None
        if mu_id in self._red_markup_items:
            curr_dict = self._red_markup_items
        elif mu_id in self._markup_items:
            curr_dict = self._markup_items
        if curr_dict and mu_id in curr_dict:
            for gi in curr_dict[mu_id]:
                if isinstance(gi, (QGraphicsPathItem, QGraphicsRectItem)):
                    p = gi.pen()
                    p.setWidthF(p.widthF() + 2)
                    gi.setPen(p)

    def _add_tracked(self, item, marker_type: str):
        """Add item to scene and track it for visibility toggling."""
        self._scene.addItem(item)
        self._type_items.setdefault(marker_type, []).append(item)
        if not self._type_visible.get(marker_type, True):
            item.setVisible(False)

    # ── Equipment marker multi-select (2026-08-08, see NOTES.md) ───────────
    def _find_equipment_item(self, marker_id):
        for item in self._type_items.get('equipment', []):
            if item.data(self._DATA_TYPE) == 'equipment' and item.data(self._DATA_ID) == marker_id:
                return item
        return None

    def _select_equipment_marker(self, marker_id):
        if marker_id in self._selected_equipment_markers:
            return
        self._selected_equipment_markers.add(marker_id)
        item = self._find_equipment_item(marker_id)
        if item is None:
            return
        # Multi-selection remains a separate dashed overlay so it stays
        # distinct from the tree-context color applied to the marker itself.
        rect = item.mapRectToScene(item.boundingRect()).adjusted(-3, -3, 3, 3)
        pen = QPen(QColor(30, 110, 220), 2.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        overlay = self._scene.addRect(rect, pen, QBrush(Qt.BrushStyle.NoBrush))
        overlay.setZValue(Z_OVERLAY + 5)
        self._equip_selection_overlays[marker_id] = overlay

    def _deselect_equipment_marker(self, marker_id):
        self._selected_equipment_markers.discard(marker_id)
        overlay = self._equip_selection_overlays.pop(marker_id, None)
        if overlay is not None:
            try: self._scene.removeItem(overlay)
            except RuntimeError as e: logging.warning(f"Failed to remove selection overlay: {e}")

    def _toggle_equipment_selection(self, marker_id):
        if marker_id in self._selected_equipment_markers:
            self._deselect_equipment_marker(marker_id)
        else:
            self._select_equipment_marker(marker_id)

    def _clear_equipment_selection(self):
        for marker_id in list(self._equip_selection_overlays.keys()):
            self._deselect_equipment_marker(marker_id)
        self._selected_equipment_markers.clear()

    def _reapply_equipment_selection_overlays(self):
        """Re-draw the dashed-blue selection overlay for every currently
        selected marker after a full overlay rebuild. clear_overlays()
        (called by PIDPanel._load_overlays() on every marker recolor —
        e.g. right after checking a deviation in EquipmentDeviationBar)
        removes ANY scene item whose zValue falls in the overlay range,
        including these — they're plain addRect() items, not tracked in
        _type_items like the markers themselves. Without this, the
        highlight from a plain click (2026-08-12, see NOTES.md) would
        vanish the instant the user checks a deviation on the very
        marker they just selected, defeating its purpose."""
        ids = list(self._selected_equipment_markers)
        self._equip_selection_overlays.clear()
        self._selected_equipment_markers.clear()
        for marker_id in ids:
            if self._find_equipment_item(marker_id) is not None:
                self._select_equipment_marker(marker_id)

    def set_search_highlight(self, marker_id):
        """Highlight one object selected by the defined-object search in blue."""
        self._clear_search_highlight()
        self._search_highlight_marker = int(marker_id) if marker_id is not None else None
        self._reapply_search_highlight()

    def _clear_search_highlight(self):
        overlay = self._search_highlight_overlay
        if overlay is not None:
            try:
                self._scene.removeItem(overlay)
            except RuntimeError:
                pass
        self._search_highlight_overlay = None

    def _reapply_search_highlight(self):
        self._clear_search_highlight()
        if self._search_highlight_marker is None:
            return
        item = self._find_equipment_item(self._search_highlight_marker)
        if item is None:
            return
        rect = item.mapRectToScene(item.boundingRect()).adjusted(-5, -5, 5, 5)
        pen = QPen(QColor(30, 105, 235), 3.0)
        pen.setCosmetic(True)
        self._search_highlight_overlay = self._scene.addRect(
            rect, pen, QBrush(Qt.BrushStyle.NoBrush))
        self._search_highlight_overlay.setZValue(Z_OVERLAY + 8)
        self._search_highlight_overlay.setVisible(
            self._type_visible.get('equipment', True))

    # ── Tree-context equipment highlight (2026-08-27, see NOTES.md
    # "Dynamisk färgmarkering av objekt på P&ID") ──────────────────────────
    def set_tree_context_highlights(self, marker_color_map: dict):
        """Replace the WHOLE tree-context highlight set in one call — the
        caller (PIDPanel._apply_tree_context_highlight) always recomputes
        the full marker_id->QColor map from scratch on every tree
        selection change rather than diffing against the previous one;
        scope size is small enough that this is far simpler than tracking
        incremental adds/removes across arbitrary tree navigation, and it
        naturally satisfies "objekt som inte längre tillhör aktuell
        kontext ska återgå till sin normala färg" — anything not in the
        new map just never gets redrawn.

        Tree context now colors the EXISTING equipment/rubber-band polygon;
        it no longer draws a separate circular halo. Anything leaving the
        context is reset to the one neutral grey base style. The dashed-blue
        multi-selection rectangle remains a separate overlay and can coexist
        with the context-colored polygon."""
        previous_ids = set(self._tree_context_highlights)
        self._tree_context_highlights = dict(marker_color_map)
        for marker_id in previous_ids - set(self._tree_context_highlights):
            self._apply_tree_context_color(marker_id, None)
        for marker_id, color in self._tree_context_highlights.items():
            self._apply_tree_context_color(marker_id, color)

    def _apply_tree_context_color(self, marker_id, color=None):
        item = self._find_equipment_item(marker_id)
        if item is None:
            return
        if color is None:
            pen_color = QColor(120, 120, 120)
            fill = QColor(150, 150, 150, 90)
            width = 1.5
        else:
            pen_color = QColor(color)
            fill = QColor(color)
            fill.setAlpha(100)
            width = 3.0
        pen = QPen(pen_color, width)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setBrush(QBrush(fill))

    def clear_tree_context_highlights(self):
        for marker_id in list(self._tree_context_highlights):
            self._apply_tree_context_color(marker_id, None)
        self._tree_context_highlights = {}

    def _reapply_tree_context_highlights(self):
        """Reapply cached context colors after equipment marker rebuild."""
        saved = dict(self._tree_context_highlights)
        self._tree_context_highlights = {}
        for marker_id, color in saved.items():
            if self._find_equipment_item(marker_id) is not None:
                self._tree_context_highlights[marker_id] = color
                self._apply_tree_context_color(marker_id, color)

    def _equipment_markers_in_rect(self, band_rect):
        """Return equipment_markers.id values whose scene bounding rect
        intersects band_rect — shared by the Ctrl-drag rubber-band's live
        count label and its release-time selection commit (2026-08-10,
        see NOTES.md)."""
        ids = []
        for item in self._type_items.get('equipment', []):
            marker_id = item.data(self._DATA_ID)
            if (item.data(self._DATA_TYPE) == 'equipment' and marker_id is not None and
                    band_rect.intersects(item.mapRectToScene(item.boundingRect()))):
                ids.append(int(marker_id))
        return ids

    def set_marker_visibility(self, marker_type: str, visible: bool):
        """Show or hide all markers of a given type."""
        self._type_visible[marker_type] = visible
        for item in self._type_items.get(marker_type, []):
            try:
                item.setVisible(visible)
            except Exception:
                pass
        if marker_type == 'equipment' and self._search_highlight_overlay is not None:
            self._search_highlight_overlay.setVisible(bool(visible))

    def _show_context_menu(self, sp, global_pos):
        menu = QMenu(self.viewport())

        # ── Explicit "clear selection" (2026-08-10, see NOTES.md) — Escape
        # and a plain click already clear it, but a discoverable menu
        # entry doesn't hurt, and gives a keyboard-free way to do it too.
        if self._selected_equipment_markers:
            n = len(self._selected_equipment_markers)
            act = menu.addAction(_icon('close'), f"Rensa markering ({n} objekt)")
            act.triggered.connect(self._clear_equipment_selection)
            menu.addSeparator()

        # ── Check if right-click landed on a sheet-connection arc ──────────────
        for item in self._scene.items(sp):
            conn_id = getattr(item, '_sheet_conn_id', None)
            if conn_id is not None:
                act = menu.addAction(_icon('unlink'), "Bryt länk")
                _cid = conn_id
                act.triggered.connect(partial(self.sheet_conn_break_requested.emit, _cid))
                menu.exec(global_pos)
                return

        # ── Check if right-click landed on a page (board layout) ───────────────
        clicked_page = self._hit_test_page(sp)
        if self.mode == MODE_BOARD_LAYOUT:
            act = menu.addAction(_icon('link'), "Lägg till länk till annat blad…")
            _cp = clicked_page
            act.triggered.connect(partial(self._start_add_sheet_link, _cp))
            menu.exec(global_pos)
            return

        # If cursor is on an existing equipment marker, offer to edit it at the top
        hovered_type = hovered_id = None
        for item in self._scene.items(sp):
            t = item.data(self._DATA_TYPE)
            i = item.data(self._DATA_ID)
            if t == 'equipment' and i is not None:
                hovered_type, hovered_id = t, int(i)
                break
        if hovered_type == 'equipment':
            # Reported feedback: right-click an existing object to edit its
            # tag number and equipment type (2026-08-12, see NOTES.md) —
            # right-clicking it previously fell through to the generic
            # "add new object here" menu with no way to edit the one
            # already under the cursor.
            act = menu.addAction(_icon('edit'), "Redigera objekt")
            mid = hovered_id
            act.triggered.connect(partial(self.equipment_edit_requested.emit, mid))
            move_act = menu.addAction(_icon('move'), "Redigera placering")
            move_act.triggered.connect(partial(self.equipment_reposition_requested.emit, mid))
            # "Ta bort" alongside it (2026-08-25, see NOTES.md — Anton:
            # "om man högerklickar på objektet så ska också alternativet
            # att ta bort finnas") — this menu previously had no way to
            # delete an existing object at all, only edit it.
            del_act = menu.addAction(_icon('delete'), "Ta bort")
            del_act.triggered.connect(partial(self.equipment_delete_requested.emit, mid))
            menu.addSeparator()

        menu.addAction("🔧 Objekt",
                       partial(self.context_action.emit, 'equipment', sp, self.current_page))
        menu.addSeparator()
        menu.addAction(_icon('search'), "Hitta liknande symbol",
                       partial(self.context_action.emit, 'find_similar', sp, self.current_page))
        menu.addAction(_icon('search'), "Hitta liknande symbol (från mall)…",
                       partial(self.context_action.emit, 'find_similar_template', sp, self.current_page))
        menu.exec(global_pos)

    def _start_add_sheet_link(self, source_page: int):
        """Enter MODE_ADD_SHEET_LINK — next left-click on a different page creates a link."""
        self._add_link_source_page = source_page
        self.set_mode(MODE_ADD_SHEET_LINK)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _place_label(self, text: str, x_pdf: float, y_pdf: float,
                     r: float, color: QColor, marker_type: str):
        """Add a text label to the right of a marker circle with white background
        and automatic vertical offset when multiple markers share the same position."""
        center = self.pdf_to_scene(x_pdf, y_pdf)
        slot_key = (round(x_pdf * 5), round(y_pdf * 5))
        slot = self._label_slots.get(slot_key, 0)
        self._label_slots[slot_key] = slot + 1
        ROW_H = 17.0
        x0 = center.x() + r + 3
        y0 = center.y() - 8 + slot * ROW_H

        txt = QGraphicsSimpleTextItem(text[:35])
        f = QFont(); f.setPointSize(8)
        txt.setFont(f)
        txt.setBrush(QBrush(color))
        txt.setPos(x0, y0)
        txt.setZValue(Z_OVERLAY + 2)

        br = txt.boundingRect()
        pad = 2.0
        bg = QGraphicsRectItem(x0 - pad, y0 - pad, br.width() + 2 * pad, br.height() + 2 * pad)
        bg.setPen(QPen(Qt.PenStyle.NoPen))
        bg.setBrush(QBrush(QColor(255, 255, 255, 230)))
        bg.setZValue(Z_OVERLAY + 1)

        self._add_tracked(bg, marker_type)
        self._add_tracked(txt, marker_type)

    def add_equipment_marker(self, marker_id, x_pdf, y_pdf, comp_type, tag='',
                             confidence=0.0, outline_pdf=None, deviation_count=0,
                             consequence_count=0, safeguard_count=0):
        """Draw an auto-detected equipment symbol marker: a semi-transparent
        shape tracing the detected symbol's outline (or a generic
        valve-bowtie icon if no outline was captured), linked to `tag` via
        geometric detection — see symbol_geometry.py / detect_equipment_symbols
        and the "🎯 Hitta på P&ID" flow in EquipmentPanel.

        Equipment polygons always start neutral grey. Their live green color
        is applied separately by set_tree_context_highlights() according to
        the selected HAZOP tree scope. `deviation_count` only controls its
        numbered information badge and tooltip; it never controls color.

        `consequence_count`/`safeguard_count` (2026-08-11, see NOTES.md
        "Tre räknare på P&ID") — two further badges (bottom-right/
        bottom-left), each only drawn when >0, counting how many times
        this equipment's tag appears in consequences/safeguards
        (Database.equipment_consequence_count/equipment_safeguard_count —
        tag+type match, since those tables have no equipment_id FK to
        join on the way deviations does)."""
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 12.0
        has_deviations = deviation_count > 0
        pen = QPen(QColor(120, 120, 120), 1.5)
        brush = QBrush(QColor(150, 150, 150, 90))

        points = None
        if outline_pdf:
            try:
                raw = json.loads(outline_pdf) if isinstance(outline_pdf, str) else outline_pdf
                if raw and len(raw) >= 3:
                    points = [self.pdf_to_scene(px, py) for px, py in raw]
            except Exception:
                points = None

        if not points:
            # Fallback: a generic valve-bowtie (two triangles meeting at the
            # center) when no detected outline was captured for this marker.
            cx, cy = center.x(), center.y()
            points = [QPointF(cx - r, cy - r), QPointF(cx - r, cy + r), QPointF(cx, cy),
                      QPointF(cx + r, cy - r), QPointF(cx + r, cy + r), QPointF(cx, cy)]

        item = QGraphicsPolygonItem(QPolygonF(points))
        item.setPen(pen)
        item.setBrush(brush)
        item.setZValue(Z_OVERLAY)
        pct = int(round(confidence * 100))
        tip = f"{tag + ': ' if tag else ''}{comp_type}\nAutodetekterad ({pct}% konfidens)"
        if has_deviations:
            tip += f"\n{deviation_count} avvikelse{'r' if deviation_count != 1 else ''} registrerad{'e' if deviation_count != 1 else ''}"
        if consequence_count > 0:
            tip += f"\n{consequence_count} konsekvens{'er' if consequence_count != 1 else ''}"
        if safeguard_count > 0:
            tip += f"\n{safeguard_count} safeguard{'s' if safeguard_count != 1 else ''}"
        # Gesture hints (2026-08-10, see NOTES.md) — Ctrl/Shift modifiers on
        # equipment markers have no other visible affordance in the UI.
        tip += ("\n\nCtrl+klick: markera flera\nCtrl+drag: gummiband-markera flera\n"
                "Shift+drag: dra (markerade) till konsekvens/safeguard/avvikelse")
        item.setToolTip(tip)
        item.setData(self._DATA_TYPE, 'equipment')
        item.setData(self._DATA_ID, marker_id)
        item.setAcceptHoverEvents(True)
        item.setCursor(Qt.CursorShape.PointingHandCursor)   # matches cause/consequence/safeguard markers
        self._add_tracked(item, 'equipment')

        def _draw_corner_badge(bx, by, count, outline, fill):
            badge_r = 8.0
            badge = QGraphicsEllipseItem(bx - badge_r, by - badge_r, 2 * badge_r, 2 * badge_r)
            badge.setPen(QPen(outline, 1))
            badge.setBrush(QBrush(fill))
            badge.setZValue(Z_OVERLAY + 1)
            badge.setToolTip(tip)
            badge.setCursor(Qt.CursorShape.PointingHandCursor)
            self._add_tracked(badge, 'equipment')
            count_txt = QGraphicsSimpleTextItem(str(count))
            f = QFont(); f.setPointSize(8); f.setBold(True)
            count_txt.setFont(f)
            count_txt.setBrush(QBrush(QColor(255, 255, 255)))
            tb = count_txt.boundingRect()
            count_txt.setPos(bx - tb.width() / 2, by - tb.height() / 2)
            count_txt.setZValue(Z_OVERLAY + 2)
            self._add_tracked(count_txt, 'equipment')

        poly_rect = QPolygonF(points).boundingRect()
        # Three corners, one counter each — colours distinct from the
        # cause/consequence/safeguard markers themselves (red/orange/green)
        # so the two never get visually conflated at a glance.
        if has_deviations:
            _draw_corner_badge(poly_rect.right(), poly_rect.top(), deviation_count,
                               QColor(0, 100, 40), QColor(0, 140, 60))
        if consequence_count > 0:
            _draw_corner_badge(poly_rect.right(), poly_rect.bottom(), consequence_count,
                               QColor(180, 100, 0), QColor(230, 140, 20))
        if safeguard_count > 0:
            _draw_corner_badge(poly_rect.left(), poly_rect.bottom(), safeguard_count,
                               QColor(20, 60, 130), QColor(52, 110, 200))

        if tag:
            self._place_label(tag, x_pdf, y_pdf, r, QColor(140, 0, 0), 'equipment')

    def _extract_tag_from_rect(self, pdf_rect: QRectF) -> str:
        """Thin wrapper around equipment_detection.extract_tag_from_rect
        (2026-08-18, see NOTES.md "kombinerad placeringsmeny") — the
        actual native-text/OCR logic moved there so
        EquipmentTagSearchWorker can run the exact same extraction off
        the UI thread against its OWN fitz.Document, instead of this
        method's self.pdf_doc (never shared across threads)."""
        return equipment_detection.extract_tag_from_rect(
            self.pdf_doc, self.current_page,
            pdf_rect.x(), pdf_rect.y(),
            pdf_rect.x() + pdf_rect.width(), pdf_rect.y() + pdf_rect.height())

    def _text_in_rect(self, pdf_rect: QRectF) -> str:
        """Return all text inside pdf_rect. Uses native PDF text first, OCR as fallback."""
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return ''
        try:
            pg    = self.pdf_doc.load_page(self.current_page)
            frect = fitz.Rect(pdf_rect.x(), pdf_rect.y(),
                               pdf_rect.x() + pdf_rect.width(),
                               pdf_rect.y() + pdf_rect.height())
            raw_words = pg.get_text("words", clip=frect)
            native = ' '.join(w[4].strip() for w in raw_words if w[4].strip())
            if native.strip():
                return native.strip()
            # OCR fallback
            if not HAS_PIL:
                return ''
            min_dim = max(pdf_rect.width(), pdf_rect.height(), 10.0)
            scale   = max(4.0, min(16.0, 300.0 / min_dim))
            mat     = fitz.Matrix(scale, scale)
            pix     = pg.get_pixmap(matrix=mat, clip=frect, alpha=False)
            pil     = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pil     = _preprocess_for_ocr(pil)
            if HAS_TESSERACT:
                try:
                    ocr = pytesseract.image_to_string(pil).strip()
                    if ocr:
                        return ocr
                except Exception:
                    pass
            if HAS_EASYOCR:
                try:
                    import numpy as np
                    reader = _get_easyocr_reader()
                    if reader:
                        results = reader.readtext(np.array(pil))
                        txt = ' '.join(r[1] for r in results if r[2] > 0.3)
                        if txt.strip():
                            return txt.strip()
                except Exception:
                    pass
        except Exception:
            pass
        return ''

    def add_tag_highlight(self, bbox: 'fitz.Rect', color: str, tooltip: str = ''):
        """Draw a semi-transparent highlight rectangle at the tag's PDF position."""
        r = QRectF(bbox.x0, bbox.y0, bbox.width, bbox.height)
        pen = QPen(Qt.PenStyle.NoPen)
        brush = QBrush(QColor(color))
        item = self._scene.addRect(r, pen, brush)
        item.setOpacity(0.35)
        item.setZValue(Z_HIGHLIGHT)
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def clear_highlights(self):
        """Remove all tag highlights (Z_HIGHLIGHT items)."""
        for item in list(self._scene.items()):
            if item.zValue() == Z_HIGHLIGHT:
                try: self._scene.removeItem(item)
                except RuntimeError as e: logging.warning(f"Failed to remove highlight item: {e}")

    def add_shape_highlight(self, outline_pdf, color='#2F5FD0'):
        """Draw a semi-transparent polygon preview of a candidate's
        outline — used by SimilarSymbolSearchDialog's "Visa på P&ID"
        live preview (2026-08-15, see NOTES.md "Hitta liknande symbol"
        — uppföljningsfunktioner). Modelled on add_equipment_marker's
        outline-drawing, but on Z_TEMP (ephemeral, explicitly cleared —
        same convention as this class's rubber-band/drag previews)
        instead of Z_OVERLAY, and with no marker_id/DB link at all —
        this is a preview, not a saved marker."""
        if not outline_pdf or len(outline_pdf) < 3:
            return None
        points = [self.pdf_to_scene(px, py) for px, py in outline_pdf]
        item = self._scene.addPolygon(
            QPolygonF(points), QPen(QColor(color), 1.5), QBrush(QColor(color)))
        item.setOpacity(0.30)
        item.setZValue(Z_TEMP)
        return item

    def clear_shape_preview(self):
        """Remove all "Visa på P&ID" preview polygons (Z_TEMP items) —
        see add_shape_highlight()."""
        for item in list(self._scene.items()):
            if item.zValue() == Z_TEMP:
                try: self._scene.removeItem(item)
                except RuntimeError as e: logging.warning(f"Failed to remove preview item: {e}")

    def _conn_obstacles(self, src_page, dst_page):
        """Inflated scene rects of every page except the connection's own two."""
        rs = self.render_scale
        rects = []
        for pn, (ox, oy) in self._page_offsets.items():
            if pn == src_page or pn == dst_page:
                continue
            w = self._page_widths_pdf.get(pn, 0.0) * rs
            h = self._page_heights_pdf.get(pn, 0.0) * rs
            rects.append(QRectF(ox - 60, oy - 60, w + 120, h + 120))
        return rects

    @staticmethod
    def _seg_rect_entry(p0, p1, r):
        """Liang-Barsky: param t where segment p0→p1 first enters rect, else None."""
        dx = p1.x() - p0.x()
        dy = p1.y() - p0.y()
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, p0.x() - r.left()), (dx, r.right() - p0.x()),
                     (-dy, p0.y() - r.top()),  (dy, r.bottom() - p0.y())):
            if abs(p) < 1e-9:
                if q < 0:
                    return None
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        return None
                    if t > t0:
                        t0 = t
                else:
                    if t < t0:
                        return None
                    if t < t1:
                        t1 = t
        return t0 if t0 < t1 else None

    def _route_around_pages(self, p0, p1, obstacles, depth=0, wiggle=0.0):
        """Waypoint list p0→p1 detouring around page rects (greedy, recursive).

        Each blocking page is passed on its nearest free side; the two detour
        corners are then routed recursively. Depth-capped — when the board
        truly forces a crossing the direct segment is kept.
        """
        if depth >= 8:
            return [p0, p1]
        best_t, best_r = None, None
        for r in obstacles:
            if r.contains(p0) and r.contains(p1):
                continue
            t = self._seg_rect_entry(p0, p1, r)
            if t is not None and 0.0 < t < 1.0 and (best_t is None or t < best_t):
                best_t, best_r = t, r
        if best_r is None:
            return [p0, p1]
        r = best_r
        M = 28.0 + abs(wiggle) * 0.5   # clearance beyond the inflated rect
        horiz = abs(p1.x() - p0.x()) >= abs(p1.y() - p0.y())
        if horiz:
            y = (r.top() - M if (p0.y() + p1.y()) / 2 <= r.center().y()
                 else r.bottom() + M) + wiggle
            x_in, x_out = ((r.left() - M, r.right() + M)
                           if p0.x() <= p1.x() else
                           (r.right() + M, r.left() - M))
            w1, w2 = QPointF(x_in, y), QPointF(x_out, y)
        else:
            x = (r.left() - M if (p0.x() + p1.x()) / 2 <= r.center().x()
                 else r.right() + M) + wiggle
            y_in, y_out = ((r.top() - M, r.bottom() + M)
                           if p0.y() <= p1.y() else
                           (r.bottom() + M, r.top() - M))
            w1, w2 = QPointF(x, y_in), QPointF(x, y_out)
        a = self._route_around_pages(p0, w1, obstacles, depth + 1, wiggle)
        b = self._route_around_pages(w2, p1, obstacles, depth + 1, wiggle)
        return a + b

    @staticmethod
    def _rounded_path(pts, radius=130.0):
        """QPainterPath through waypoints with rounded (quad-bezier) corners."""
        import math
        path = QPainterPath()
        path.moveTo(pts[0])
        for i in range(1, len(pts) - 1):
            prev, p, nxt = pts[i - 1], pts[i], pts[i + 1]
            v1x, v1y = p.x() - prev.x(), p.y() - prev.y()
            v2x, v2y = nxt.x() - p.x(), nxt.y() - p.y()
            l1 = math.hypot(v1x, v1y) or 1.0
            l2 = math.hypot(v2x, v2y) or 1.0
            r1 = min(radius, l1 * 0.5)
            r2 = min(radius, l2 * 0.5)
            a = QPointF(p.x() - v1x / l1 * r1, p.y() - v1y / l1 * r1)
            b = QPointF(p.x() + v2x / l2 * r2, p.y() + v2y / l2 * r2)
            path.lineTo(a)
            path.quadTo(p, b)
        path.lineTo(pts[-1])
        return path

    def add_sheet_conn_arc(self, src: QPointF, dst: QPointF,
                           color_hex: str, confidence: float, label: str,
                           bidirectional: bool = False, conn_id: int = -1,
                           src_edge: str = 'right', dst_edge: str = 'left',
                           src_page: int = -1, dst_page: int = -1,
                           arc_index: int = 0, weight: float = 0.5):
        """Draw a routed connection line over the board at 50% opacity.

        Short edge stubs let the line turn steeply toward its target; the
        middle leg detours around other pages (rounded corners) so lines do
        not cut straight across P&IDs. Parallel connections are staggered.
        Pen width scales with weight; drawn above all page pixmaps.
        """
        import math
        color = QColor(color_hex)
        color.setAlpha(128)   # 50 % transparent

        pen_width = round(max(2.0, min(6.0, 2.0 + weight * 5.0)), 1)
        pen = QPen(color, pen_width)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if confidence < 0.50:
            pen.setStyle(Qt.PenStyle.DashLine)
        elif confidence < 0.70:
            pen.setStyle(Qt.PenStyle.DotLine)

        sx, sy = src.x(), src.y()
        ex, ey = dst.x(), dst.y()

        # Stagger parallel connections perpendicular to the chord
        STEP = 20
        if arc_index == 0:
            slot_off = 0
        else:
            slot_off = ((arc_index + 1) // 2) * STEP * (1 if arc_index % 2 == 1 else -1)

        chord = math.hypot(ex - sx, ey - sy) or 1.0
        perp_x = -(ey - sy) / chord
        perp_y =  (ex - sx) / chord
        src_pt = QPointF(sx + perp_x * slot_off, sy + perp_y * slot_off)
        dst_pt = QPointF(ex + perp_x * slot_off, ey + perp_y * slot_off)

        # Short edge stubs — the line may turn steeply once clear of the page
        stub = max(70.0, min(260.0, chord * 0.18))
        _edx = {'right': 1, 'left': -1, 'top': 0, 'bottom': 0}
        _edy = {'right': 0, 'left':  0, 'top': -1, 'bottom': 1}
        src_stub = QPointF(src_pt.x() + _edx.get(src_edge, 1)  * stub,
                           src_pt.y() + _edy.get(src_edge, 0)  * stub)
        dst_stub = QPointF(dst_pt.x() + _edx.get(dst_edge, -1) * stub,
                           dst_pt.y() + _edy.get(dst_edge,  0) * stub)

        # Route the middle leg around other pages for a cleaner board;
        # fall back to the direct curve when the detour grows absurd.
        obstacles = self._conn_obstacles(src_page, dst_page)
        mids = self._route_around_pages(src_stub, dst_stub, obstacles,
                                        wiggle=float(slot_off))
        pts = [src_pt] + mids + [dst_pt]
        clean = [pts[0]]
        for p in pts[1:]:
            if abs(p.x() - clean[-1].x()) + abs(p.y() - clean[-1].y()) > 3.0:
                clean.append(p)
        if len(clean) < 2:
            clean = [src_pt, dst_pt]
        total = sum(math.hypot(clean[i + 1].x() - clean[i].x(),
                               clean[i + 1].y() - clean[i].y())
                    for i in range(len(clean) - 1))
        if total > chord * 3.0 + 1200.0:
            clean = [src_pt, src_stub, dst_stub, dst_pt]

        path = self._rounded_path(clean)

        pi = QGraphicsPathItem(path)
        pi.setPen(pen)
        pi.setZValue(Z_SHEET_CONN)   # above page pixmaps
        pi._sheet_conn_id = conn_id
        pi.setFlag(pi.GraphicsItemFlag.ItemIsSelectable, False)
        self._scene.addItem(pi)

        # Arrowhead tangent follows the final segment into the dot
        arrow_angle = math.atan2(clean[-1].y() - clean[-2].y(),
                                 clean[-1].x() - clean[-2].x())

        def _arrowhead(tip_pt, angle, col):
            AL, AH = 16, 7
            lp = QPointF(tip_pt.x() - AL * math.cos(angle) + AH * math.sin(angle),
                         tip_pt.y() - AL * math.sin(angle) - AH * math.cos(angle))
            rp = QPointF(tip_pt.x() - AL * math.cos(angle) - AH * math.sin(angle),
                         tip_pt.y() - AL * math.sin(angle) + AH * math.cos(angle))
            ah = QGraphicsPolygonItem(QPolygonF([tip_pt, lp, rp]))
            ah.setBrush(QBrush(col))
            ah.setPen(QPen(Qt.PenStyle.NoPen))
            ah.setZValue(Z_SHEET_CONN)
            self._scene.addItem(ah)

        _arrowhead(dst_pt, arrow_angle, color)
        if bidirectional:
            src_angle = math.atan2(clean[0].y() - clean[1].y(),
                                   clean[0].x() - clean[1].x())
            _arrowhead(src_pt, src_angle, color)

        # Label at bezier midpoint with white background
        if label:
            mid = path.pointAtPercent(0.5)
            txt = QGraphicsSimpleTextItem(label)
            label_color = QColor(color_hex)   # opaque for readability
            label_color.setAlpha(200)
            txt.setBrush(QBrush(label_color.darker(130)))
            fnt = txt.font(); fnt.setPointSize(8); fnt.setBold(True); txt.setFont(fnt)
            tr = txt.boundingRect()
            txt.setPos(mid.x() - tr.width() / 2, mid.y() - tr.height() / 2 - 10)
            bg = QGraphicsRectItem(txt.pos().x() - 3, txt.pos().y() - 2,
                                   tr.width() + 6, tr.height() + 4)
            bg.setBrush(QBrush(QColor(250, 250, 250, 210)))
            bg.setPen(QPen(Qt.PenStyle.NoPen))
            bg.setZValue(Z_SHEET_CONN + 0.1)
            txt.setZValue(Z_SHEET_CONN + 0.2)
            self._scene.addItem(bg)
            self._scene.addItem(txt)

    def clear_overlays(self):
        self._purge_rubber_band_state()

        _keep = set(self._all_page_items.values()) | {self._placeholder}
        for item in list(self._scene.items()):
            if item in _keep:
                continue
            if item.zValue() >= Z_SHEET_CONN or item.zValue() < Z_PAGE:
                try: self._scene.removeItem(item)
                except RuntimeError as e: logging.warning(f"Failed to remove overlay item: {e}")
        if self._pending_path_item is not None:
            try: self._scene.removeItem(self._pending_path_item)
            except RuntimeError as e: logging.warning(f"Failed to remove pending path item: {e}")
            self._pending_path_item = None
        # Clear per-type item lists and label slots
        for key in self._type_items:
            self._type_items[key] = []
        self._label_slots.clear()

    def mousePressEvent(self, event):
        # Update current_page to whichever page was clicked
        _sp = self.mapToScene(event.position().toPoint())
        _detected = self._hit_test_page(_sp)
        if _detected != self.current_page:
            self.current_page = _detected
            self.page_item = self._all_page_items.get(_detected)

        # ── Add sheet link: click target page ────────────────────────────────
        if self.mode == MODE_ADD_SHEET_LINK and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            target = self._hit_test_page(sp)
            src = self._add_link_source_page
            self._add_link_source_page = None
            self.set_mode(MODE_BOARD_LAYOUT)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            if src is not None and target != src and target in self._all_page_items:
                self.sheet_conn_add_requested.emit(src, target)
            event.accept(); return
        if self.mode == MODE_ADD_SHEET_LINK and event.button() == Qt.MouseButton.RightButton:
            # Cancel add-link mode
            self._add_link_source_page = None
            self.set_mode(MODE_BOARD_LAYOUT)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            event.accept(); return

        # ── Board layout: drag page ───────────────────────────────────────────
        if self.mode == MODE_BOARD_LAYOUT and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            page = self._hit_test_page(sp)
            if page in self._all_page_items:
                self._dragging_page        = page
                self._drag_page_orig_offset = self._page_offsets.get(page, (0.0, 0.0))
                self._drag_page_start_scene = sp
                self.current_page           = page
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                # Hide connection lines during drag (re-drawn on release)
                for _ci in list(self._scene.items()):
                    if _ci.zValue() == Z_CONNECT:
                        _ci.setVisible(False)
                event.accept(); return
            super().mousePressEvent(event); return

        if self.mode in (MODE_NAV, MODE_MARKUP_SELECT):
            self._press_pos = event.position()
        sp = self.mapToScene(event.position().toPoint())

        if self.mode == MODE_PLACE_EQUIPMENT and event.button() == Qt.MouseButton.LeftButton:
            self._place_rband_start_scene = sp
            self._place_rband_dragging = False
            event.accept(); return

        if self.mode == MODE_EDIT_EQUIPMENT and event.button() == Qt.MouseButton.LeftButton:
            marker_id = getattr(self, '_reposition_marker_id', None)
            self._reposition_marker_id = None
            self.set_mode(MODE_NAV)
            if marker_id is not None:
                self.equipment_reposition_finished.emit(int(marker_id), sp, self.current_page)
            event.accept(); return

        # ── Shift+press on an equipment marker: arm a possible drag-to-worksheet ──
        # (dragging a tag onto a HAZOP consequence row). A plain click (no Shift)
        # never arms this, so it can never interfere with the normal
        # click-opens-EquipmentDeviationBar interaction below.
        self._equip_drag_candidate = None
        if (self.mode == MODE_NAV and event.button() == Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            for item in self._scene.items(sp):
                itype = item.data(self._DATA_TYPE)
                iid   = item.data(self._DATA_ID)
                if itype == 'equipment' and iid is not None:
                    self._equip_drag_candidate = (int(iid), event.position())
                    break

        # ── Ctrl+click: toggle one marker in/out of the multi-selection.
        # Ctrl+drag over empty canvas: arm a rubber-band to select several
        # at once (2026-08-08, see NOTES.md). Mutually exclusive with the
        # Shift-drag above — Ctrl selects, Shift drags, never both at once.
        if (self.mode == MODE_NAV and event.button() == Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ControlModifier and
                not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            hit_marker = None
            for item in self._scene.items(sp):
                itype = item.data(self._DATA_TYPE)
                iid   = item.data(self._DATA_ID)
                if itype == 'equipment' and iid is not None:
                    hit_marker = int(iid)
                    break
            if hit_marker is not None:
                self._toggle_equipment_selection(hit_marker)
            else:
                self._ctrl_rband_start_scene = sp
                self._ctrl_rband_dragging    = False
            self._press_pos = None
            event.accept(); return

        # Plain click (no Ctrl, no Shift) clears any existing multi-
        # selection — same convention as file managers/desktop icons. Not
        # an early return: the normal click-dispatch (open
        # EquipmentDeviationBar etc., in mouseReleaseEvent) still applies.
        if (self.mode == MODE_NAV and event.button() == Qt.MouseButton.LeftButton and
                not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                          Qt.KeyboardModifier.ShiftModifier)) and
                self._selected_equipment_markers):
            self._clear_equipment_selection()

        if self.mode == MODE_NODE:
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(sp); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_drawing(); event.accept(); return
        elif self.mode in (MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(self._snap_to_nearest(sp)); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._finish_markup_drawing(); event.accept(); return
        elif self.mode in (MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT):
            if event.button() == Qt.MouseButton.LeftButton:
                # Single-click immediately triggers finished signal
                pdf_pt = self.scene_to_pdf(sp)
                type_ = 'text' if self.mode == MODE_MARKUP_TEXT else 'comment'
                self.markup_draw_finished.emit(type_, [list(pdf_pt)], self.current_page)
                event.accept(); return
        elif self.mode == MODE_RED_MARKUP_SYMBOL:
            if event.button() == Qt.MouseButton.LeftButton:
                pdf_pt = self.scene_to_pdf(sp)
                self.markup_draw_finished.emit('symbol', [list(pdf_pt)], self.current_page)
                event.accept(); return
        elif self.mode == MODE_MARKUP_SELECT:
            if event.button() == Qt.MouseButton.LeftButton:
                view_pos = event.position().toPoint()
                # Priority 0a: corner resize handle (symbol)
                _corner_cursors = [
                    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
                    Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeFDiagCursor,
                ]
                for i, handle in enumerate(self._corner_handles):
                    hvp = self.mapFromScene(handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 144:   # 12 screen-pixel radius
                        self._symbol_drag_mode = ['nw', 'ne', 'sw', 'se'][i]
                        self._drag_mode = 'symbol_transform'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(_corner_cursors[i])
                        event.accept(); return
                # Priority 0b: rotation handle (symbol)
                if self._rot_handle is not None:
                    hvp = self.mapFromScene(self._rot_handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 196:   # 14 screen-pixel radius
                        self._symbol_drag_mode = 'rotate'
                        self._drag_mode = 'symbol_transform'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.OpenHandCursor)
                        event.accept(); return
                # Priority 1: vertex handle hit
                for i, handle in enumerate(self._vertex_handles):
                    hvp = self.mapFromScene(handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 144:   # 12 screen-pixel radius
                        self._drag_mode = 'vertex'
                        self._drag_vertex_idx = i
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.CrossCursor)
                        event.accept(); return
                # Priority 2: markup item hit
                for item in self._scene.items(sp):
                    mu_id = item.data(self._DATA_MARKUP_ID)
                    if mu_id is not None:
                        mu_id_int = int(mu_id)
                        self._select_for_edit(mu_id_int)
                        self._drag_mode = 'item'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.SizeAllCursor)
                        event.accept(); return
                # Priority 3: empty space → clear selection, fall through for panning
                self._clear_edit_handles()

        if event.button() == Qt.MouseButton.RightButton and self.mode in (MODE_NAV, MODE_BOARD_LAYOUT):
            # Start rubber-band drag; show context menu only if no drag occurs
            self._rband_start_scene = sp
            self._rband_dragging    = False
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.mode == MODE_MARKUP_SELECT and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            for gi in self._scene.items(sp):
                mu_id = gi.data(self._DATA_MARKUP_ID)
                if mu_id is not None:
                    mu_id_int = int(mu_id)
                    mtype = self._markup_types.get(mu_id_int,
                                self._red_markup_types.get(mu_id_int))
                    if mtype in ('text', 'comment'):
                        self._start_inline_label_edit(mu_id_int)
                        event.accept()
                        return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE) \
                and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            self._add_draw_point(sp)
            self._finish_markup_drawing()
            event.accept(); return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if (self.mode == MODE_PLACE_EQUIPMENT and
                event.button() == Qt.MouseButton.LeftButton and
                self._place_rband_start_scene is not None):
            sp = self.mapToScene(event.position().toPoint())
            start = self._place_rband_start_scene
            dragging = self._place_rband_dragging
            if self._place_rband_preview_item is not None:
                try: self._scene.removeItem(self._place_rband_preview_item)
                except RuntimeError: pass
                self._place_rband_preview_item = None
            self._place_rband_start_scene = None
            self._place_rband_dragging = False
            page = self.current_page
            self.set_mode(MODE_NAV)
            if dragging:
                rect = QRectF(start, sp).normalized()
                rs = self.render_scale
                ox, oy = self._page_offsets.get(page, (0.0, 0.0))
                pdf_rect = QRectF((rect.x() - ox) / rs, (rect.y() - oy) / rs,
                                  rect.width() / rs, rect.height() / rs)
                self.equipment_place_zone_requested.emit(pdf_rect, page)
            else:
                self.equipment_place_requested.emit(sp, page)
            event.accept(); return

        # ── Board layout page drag release ────────────────────────────────────
        if (self.mode == MODE_BOARD_LAYOUT and self._dragging_page is not None and
                event.button() == Qt.MouseButton.LeftButton):
            self._dragging_page = None
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self._update_board_scene_rect()
            layout = {str(p): [off[0], off[1]] for p, off in self._page_offsets.items()}
            self.board_layout_changed.emit(json.dumps(layout))
            event.accept(); return

        # ── Right-drag rubber band end ────────────────────────────────────────
        if event.button() == Qt.MouseButton.RightButton and self._rband_start_scene is not None:
            if self._rband_dragging:
                sp = self.mapToScene(event.position().toPoint())
                if self._rband_preview_item is not None:
                    try: self._scene.removeItem(self._rband_preview_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove rubber-band preview: {e}")
                    self._rband_preview_item = None
                if self._rband_label_item is not None:
                    try: self._scene.removeItem(self._rband_label_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove rubber-band label: {e}")
                    self._rband_label_item = None
                rect = QRectF(self._rband_start_scene, sp).normalized()
                rs = self.render_scale
                ox, oy = self._page_offsets.get(self.current_page, (0.0, 0.0))
                pdf_rect = QRectF((rect.x() - ox) / rs, (rect.y() - oy) / rs,
                                   rect.width() / rs, rect.height() / rs)
                self._rband_start_scene = None
                self._rband_dragging    = False
                self.zone_drawn.emit(pdf_rect, self.current_page)
            else:
                # No drag — show context menu as usual
                sp = self.mapToScene(event.position().toPoint())
                self._rband_start_scene = None
                self._rband_dragging    = False
                self._show_context_menu(sp, event.globalPosition().toPoint())
            event.accept(); return

        # ── Ctrl+drag rubber band end — commit the multi-selection ─────────────
        # (2026-08-08, see NOTES.md). Adds to the existing selection rather
        # than replacing it, so repeated Ctrl-drags can build up a group.
        if (event.button() == Qt.MouseButton.LeftButton and
                self._ctrl_rband_start_scene is not None):
            if self._ctrl_rband_dragging:
                sp = self.mapToScene(event.position().toPoint())
                if self._ctrl_rband_preview_item is not None:
                    try: self._scene.removeItem(self._ctrl_rband_preview_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove ctrl rubber-band preview: {e}")
                    self._ctrl_rband_preview_item = None
                if self._ctrl_rband_count_label is not None:
                    try: self._scene.removeItem(self._ctrl_rband_count_label)
                    except RuntimeError as e: logging.warning(f"Failed to remove ctrl-drag count label: {e}")
                    self._ctrl_rband_count_label = None
                band_rect = QRectF(self._ctrl_rband_start_scene, sp).normalized()
                for marker_id in self._equipment_markers_in_rect(band_rect):
                    self._select_equipment_marker(marker_id)
            self._ctrl_rband_start_scene = None
            self._ctrl_rband_dragging    = False
            self._press_pos = None
            event.accept(); return

        # ── MARKUP_SELECT drag-aware release ──────────────────────────────────
        if self.mode == MODE_MARKUP_SELECT and self._drag_mode is not None and \
                event.button() == Qt.MouseButton.LeftButton:
            if self._drag_threshold_exceeded and self._edit_mu_id is not None:
                if self._drag_mode == 'symbol_transform':
                    self._finish_symbol_transform()
                else:
                    self._finish_edit_drag()
            elif not self._drag_threshold_exceeded and self._edit_mu_id is not None:
                if self._drag_mode != 'symbol_transform':
                    self.markup_item_clicked.emit(self._edit_mu_id)
            self._drag_mode = None
            self._drag_vertex_idx = None
            self._drag_threshold_exceeded = False
            self._press_pos = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        # ── NAV mode: click on marker navigates tree ──────────────────────────
        if (self.mode in (MODE_NAV,) and
                event.button() == Qt.MouseButton.LeftButton and
                self._press_pos is not None):
            p  = event.position()
            dx = p.x() - self._press_pos.x()
            dy = p.y() - self._press_pos.y()
            if dx * dx + dy * dy < 25:
                sp = self.mapToScene(p.toPoint())
                for item in self._scene.items(sp):
                    itype = item.data(self._DATA_TYPE)
                    iid   = item.data(self._DATA_ID)
                    if itype in ('cause', 'consequence', 'safeguard', 'equipment') and iid is not None:
                        if itype == 'equipment':
                            # Plain click on a defined (red/green) object
                            # marker highlights it blue, same dashed
                            # overlay Ctrl-click multi-select already uses
                            # — single-select semantics here: clicking a
                            # new one replaces the old highlight (2026-08-12,
                            # see NOTES.md). Selection is cleared elsewhere
                            # for any other plain click (mousePressEvent).
                            self._clear_equipment_selection()
                            self._select_equipment_marker(int(iid))
                        self.marker_clicked.emit(itype, int(iid))
                        break
        self._press_pos = None
        self._equip_drag_candidate = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if (self.mode == MODE_PLACE_EQUIPMENT and
                self._place_rband_start_scene is not None and
                event.buttons() & Qt.MouseButton.LeftButton):
            sp = self.mapToScene(event.position().toPoint())
            start = self._place_rband_start_scene
            dx, dy = sp.x() - start.x(), sp.y() - start.y()
            if not self._place_rband_dragging and dx * dx + dy * dy > 100:
                self._place_rband_dragging = True
            if self._place_rband_dragging:
                rect = QRectF(start, sp).normalized()
                if self._place_rband_preview_item is None:
                    pen = QPen(QColor(0, 100, 200), 1.5)
                    pen.setStyle(Qt.PenStyle.DashLine); pen.setCosmetic(True)
                    self._place_rband_preview_item = self._scene.addRect(
                        rect, pen, QBrush(QColor(0, 100, 200, 28)))
                    self._place_rband_preview_item.setZValue(Z_TEMP)
                else:
                    self._place_rband_preview_item.setRect(rect)
            event.accept(); return

        # ── Shift+drag of an equipment marker → worksheet consequence row ─────
        if (self._equip_drag_candidate is not None and
                event.buttons() & Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            marker_id, start_pos = self._equip_drag_candidate
            p  = event.position()
            dx = p.x() - start_pos.x()
            dy = p.y() - start_pos.y()
            if (dx * dx + dy * dy) ** 0.5 >= QApplication.startDragDistance():
                self._equip_drag_candidate = None
                self._press_pos = None  # prevent mouseReleaseEvent's click-dispatch from also firing
                mime = QMimeData()
                # Dragging a marker that's part of a >=2-member multi-
                # selection (2026-08-08, see NOTES.md) drags the WHOLE
                # group, not just the one under the cursor; otherwise
                # unchanged single-marker behaviour.
                if (len(self._selected_equipment_markers) >= 2 and
                        marker_id in self._selected_equipment_markers):
                    ids_text = ",".join(str(i) for i in sorted(self._selected_equipment_markers))
                    mime.setText(f'hzp:equipment-multi:{ids_text}:-1:-1')
                else:
                    mime.setText(f'hzp:equipment:{marker_id}:-1:-1')
                drag = QDrag(self)
                drag.setMimeData(mime)
                drag.exec(Qt.DropAction.CopyAction)
                # drag.exec() blocks until the drop is released; a native
                # drag suppresses normal hover/leave events for widgets the
                # cursor passes over (e.g. the toolbar's "🔍 Navigera"
                # button), which can leave that button visually stuck in
                # its pressed/hover look after the drop even though nothing
                # is actually still held down (reported: the nav button
                # stays "intryckt" after dropping onto the tree/scenario).
                self.equipment_drag_finished.emit()
                # The mousePressEvent that started this gesture fell through
                # to super().mousePressEvent() (needed so a Shift+click that
                # never crosses the drag threshold still click-dispatches
                # normally), which armed Qt's OWN ScrollHandDrag hand-scroll
                # tracking for MODE_NAV — cursor switched to a closed hand,
                # internal "currently panning" flag set. Because drag.exec()
                # took over instead of a normal mouseMoveEvent/mouseReleaseEvent
                # pair, Qt never saw the matching release that would close
                # that out, leaving the view's cursor/pan state stuck as if
                # still mid-drag after the drop (2026-08-13 follow-up report:
                # "musen sitter kvar i dra-läge"). Toggling the drag mode off
                # and back on resets Qt's internal hand-scroll state cleanly.
                if self.mode == MODE_NAV:
                    self.setDragMode(QGraphicsView.DragMode.NoDrag)
                    self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
                self._clear_equipment_selection()
                return
        elif self._equip_drag_candidate is not None and not (
                event.buttons() & Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            # Shift released or button let go before threshold — disarm
            self._equip_drag_candidate = None

        # ── Board layout page drag ────────────────────────────────────────────
        if self.mode == MODE_BOARD_LAYOUT and self._dragging_page is not None:
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._drag_page_start_scene.x()
            dy = sp.y() - self._drag_page_start_scene.y()
            orig_ox, orig_oy = self._drag_page_orig_offset
            new_ox = orig_ox + dx
            new_oy = orig_oy + dy
            self._page_offsets[self._dragging_page] = (new_ox, new_oy)
            self._all_page_items[self._dragging_page].setPos(new_ox, new_oy)
            event.accept(); return

        # ── Right-drag rubber band ────────────────────────────────────────────
        if (self._rband_start_scene is not None and
                event.buttons() & Qt.MouseButton.RightButton):
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._rband_start_scene.x()
            dy = sp.y() - self._rband_start_scene.y()
            if not self._rband_dragging and dx * dx + dy * dy > 100:
                self._rband_dragging = True
            if self._rband_dragging:
                rect = QRectF(self._rband_start_scene, sp).normalized()
                # Reuse the existing preview item — just update its geometry.
                # Creating/removing scene items on every mouseMoveEvent is slow.
                if self._rband_preview_item is None:
                    pen = QPen(QColor(0, 100, 200), 1.5)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    self._rband_preview_item = self._scene.addRect(
                        rect, pen, QBrush(QColor(0, 100, 200, 28)))
                    self._rband_preview_item.setZValue(Z_TEMP)
                else:
                    self._rband_preview_item.setRect(rect)
                # Remove live tag label during drag — tag is extracted on release.
                # _extract_tag_from_rect reads the PDF on every move event which is slow.
                if self._rband_label_item is not None:
                    try: self._scene.removeItem(self._rband_label_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove dragged label: {e}")
                    self._rband_label_item = None
            event.accept(); return

        # ── Ctrl+drag rubber band — multi-select equipment markers ────────────
        # (2026-08-08, see NOTES.md). Distinct orange colour from the
        # right-drag zone-rubber-band above so the two are never confused.
        if (self._ctrl_rband_start_scene is not None and
                event.buttons() & Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._ctrl_rband_start_scene.x()
            dy = sp.y() - self._ctrl_rband_start_scene.y()
            if not self._ctrl_rband_dragging and dx * dx + dy * dy > 25:
                self._ctrl_rband_dragging = True
            if self._ctrl_rband_dragging:
                rect = QRectF(self._ctrl_rband_start_scene, sp).normalized()
                if self._ctrl_rband_preview_item is None:
                    pen = QPen(QColor(230, 140, 0), 1.5)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    self._ctrl_rband_preview_item = self._scene.addRect(
                        rect, pen, QBrush(QColor(230, 140, 0, 28)))
                    self._ctrl_rband_preview_item.setZValue(Z_TEMP)
                else:
                    self._ctrl_rband_preview_item.setRect(rect)

                # Live "N objekt"-etikett — visar hur många som skulle bli
                # markerade om man släppte just nu (2026-08-10, se
                # NOTES.md), istället för att bara visa antalet EFTER släpp.
                count = len(self._equipment_markers_in_rect(rect))
                if count > 0:
                    text = f"{count} objekt"
                    if self._ctrl_rband_count_label is None:
                        self._ctrl_rband_count_label = QGraphicsSimpleTextItem(text)
                        lbl_font = QFont()
                        lbl_font.setBold(True)
                        lbl_font.setPointSize(9)
                        self._ctrl_rband_count_label.setFont(lbl_font)
                        self._ctrl_rband_count_label.setBrush(QBrush(QColor(180, 100, 0)))
                        self._ctrl_rband_count_label.setZValue(Z_TEMP + 1)
                        self._scene.addItem(self._ctrl_rband_count_label)
                    else:
                        self._ctrl_rband_count_label.setText(text)
                    self._ctrl_rband_count_label.setPos(sp.x() + 12, sp.y() + 12)
                elif self._ctrl_rband_count_label is not None:
                    try: self._scene.removeItem(self._ctrl_rband_count_label)
                    except RuntimeError as e: logging.warning(f"Failed to remove ctrl-drag count label: {e}")
                    self._ctrl_rband_count_label = None
            event.accept(); return

        if self.mode == MODE_MARKUP_SELECT and self._drag_mode is not None:
            sp = self.mapToScene(event.position().toPoint())
            if not self._drag_threshold_exceeded:
                dx = sp.x() - self._drag_start_scene.x()
                dy = sp.y() - self._drag_start_scene.y()
                if dx * dx + dy * dy > 4.0:
                    self._drag_threshold_exceeded = True
            if self._drag_threshold_exceeded:
                delta = sp - self._drag_start_scene
                if self._drag_mode == 'symbol_transform':
                    self._do_symbol_transform(sp)
                elif self._drag_mode == 'vertex' and self._drag_vertex_idx is not None:
                    new_pts = list(self._drag_current_pts)
                    idx = self._drag_vertex_idx
                    new_pts[idx] = self._drag_original_pts[idx] + delta
                    self._drag_current_pts = new_pts
                    self._update_edit_path(self._drag_current_pts)
                    self._update_handle_positions(self._drag_current_pts)
                elif self._drag_mode == 'item':
                    self._drag_current_pts = [p + delta for p in self._drag_original_pts]
                    self._update_edit_path(self._drag_current_pts)
                    self._update_handle_positions(self._drag_current_pts)
                event.accept()
                return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE) \
                and self.draw_points:
            sp = self.mapToScene(event.position().toPoint())
            if self.mode in (MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
                sp = self._snap_to_nearest(sp)
            self._update_rubber_band(sp)
        super().mouseMoveEvent(event)

    _LOD_THRESHOLD = 0.12   # view scale below which overview mode activates

    def _apply_lod(self, scale: float, force: bool = False):
        """Switch rendering quality based on zoom level.

        Only iterates scene items when the LOD tier actually changes so there
        is no per-frame overhead on continuous zoom within the same tier.
        """
        overview = scale < self._LOD_THRESHOLD
        if not force and overview == self._lod_overview:
            return
        self._lod_overview = overview

        aa = not overview
        self.setRenderHint(QPainter.RenderHint.Antialiasing, aa)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, aa)

        mode = (Qt.TransformationMode.FastTransformation if overview
                else Qt.TransformationMode.SmoothTransformation)
        for item in self._all_page_items.values():
            item.setTransformationMode(mode)

        # Overlay items (markers, labels, handles): invisible at overview zoom
        for item in self._scene.items():
            if item.zValue() >= Z_OVERLAY:
                item.setVisible(not overview)

    def wheelEvent(self, event):
        # Smooth zoom: scale by a factor proportional to the wheel delta
        # so trackpad pinch-zoom gives fine-grained control
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept(); return
        # 1.001^delta gives ~1.15× per 120-unit tick (standard wheel notch)
        factor = 1.001 ** delta
        # Clamp to prevent extreme zoom
        cur = self.transform().m11()
        if cur * factor < 0.02:
            factor = 0.02 / cur
        elif cur * factor > 200:
            factor = 200 / cur
        self.scale(factor, factor)
        self._apply_lod(self.transform().m11())
        self._schedule_lod_update()
        event.accept()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._schedule_lod_update()

    def keyPressEvent(self, event):
        # Escape clears an active equipment multi-selection (2026-08-10,
        # see NOTES.md) — previously only placement modes/drawing had an
        # Escape handler; a Ctrl-click/Ctrl-drag selection had no keyboard
        # way to back out of short of clicking empty canvas.
        if (self.mode == MODE_NAV and event.key() == Qt.Key.Key_Escape and
                self._selected_equipment_markers):
            self._clear_equipment_selection()
            event.accept(); return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_markup_drawing(); event.accept(); return
            elif event.key() == Qt.Key.Key_Escape:
                self._cancel_drawing(); event.accept(); return
        super().keyPressEvent(event)
