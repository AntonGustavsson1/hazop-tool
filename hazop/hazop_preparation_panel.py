#!/usr/bin/env python3
"""HAZOPPreparationPanel (+ its private DraggableColorSwatch/MatrixCellButton drag-and-drop helpers) -- split out of settings_panels.py 2026-08-21, see NOTES.md "Dela upp settings_panels.py"."""

import re
import json
from pathlib import Path
from functools import partial

from PyQt6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDateEdit,
    QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QStackedWidget, QTabWidget, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal, QDate, QEvent, QMimeData, QPoint, QPointF, QTimer
from PyQt6.QtGui import (
    QBrush, QColor, QDrag, QFont, QFontMetrics, QPainter, QPainterPath,
    QPen, QPolygonF,
)

from constants import CONFIG, SEV_LABELS
from database import (
    Database, DEFAULT_MATRIX, DEFAULT_FREQ_BOUNDARIES, _STD_OBJECTS,
    _normalise_matrix, get_matrix, freq_to_f_level,
    risk_info,
)
from pid_viewer import _icon, FREQ_LABELS, ocr_status
from ui_helpers import freq_axis_label
from equipment_panel import TagDatabasePanel, PIDAnalysisPanel
from participant_matrix_panel import ParticipantMatrixPanel
from standard_causes_panel import StandardCausesSettingsPanel


_PALETTE_MIME = 'application/x-hazop-palette-color'
_MATRIX_CELL_MIME = 'application/x-hazop-matrix-cell'
_RISK_LEVEL_MIME = 'application/x-hazop-risk-level'
_MATRIX_CELL_WIDTH_DEFAULT = 92

# ST1 Sverige AB risk matrix transcribed from
# ej_programfiler/reference_material/St1/St1 SA 04 - Riskmatris#4_161189.pdf.
# Grid data is stored low-to-high (severity 0..5, likelihood A..E); the
# display is reversed vertically so the low-severity row appears at the top,
# matching the source document.  The PDF uses qualitative likelihood classes,
# so the numeric boundaries below are only the application's technical
# fallback for converting a manually entered frequency.
ST1_RISK_MATRIX_PRESET = {
    'rows': 6,
    'cols': 5,
    'x_axis': 'frequency',
    'x_reversed': False,
    'y_reversed': True,
    'x_codes': ['A', 'B', 'C', 'D', 'E'],
    'y_codes': ['0', '1', '2', '3', '4', '5'],
    'x_labels': [
        'Aldrig hört talas om inom industrin',
        'Har inträffat inom industrin',
        'Har inträffat flertalet gånger inom industrin',
        'Har inträffat inom bolaget',
        'Har inträffat flertalet gånger inom bolaget',
    ],
    'y_labels': [
        'Ingen skada eller hälsoeffekt',
        'Liten skada eller hälsoeffekt',
        'Begränsad skada eller hälsoeffekt',
        'Stor skada eller hälsoeffekt',
        'Bestående total invaliditet eller upp till 3 döda',
        '4 eller fler döda',
    ],
    'cell_colors': [
        ['#8DB1DF', '#8DB1DF', '#8DB1DF', '#8DB1DF', '#8DB1DF'],
        ['#8DB1DF', '#8DB1DF', '#3769EE', '#3769EE', '#3769EE'],
        ['#8DB1DF', '#3769EE', '#3769EE', '#FFF500', '#FFF500'],
        ['#3769EE', '#3769EE', '#FFF500', '#FFF500', '#FF0000'],
        ['#3769EE', '#FFF500', '#FFF500', '#FF0000', '#FF0000'],
        ['#FFF500', '#FFF500', '#FF0000', '#FF0000', '#FF0000'],
    ],
    'cell_labels': [
        ['Marginell risk', '', '', '', 'Ljusblå: lämnas utan åtgärd'],
        ['', '', 'Mörkblå: lämnas efter bedömning', '', ''],
        ['', 'Liten risk', '', 'Gul: kräver åtgärd', ''],
        ['Teknisk handbok', '', 'Stor risk', '', 'Röd: kräver åtgärd'],
        ['', 'Bedöm ALARP', '', 'Mycket stor risk', ''],
        ['', '', 'Bedöm ALARP', '', ''],
    ],
    'cell_fg_colors': [
        ['#000000', '#000000', '#000000', '#000000', '#000000'],
        ['#000000', '#000000', '#FFFFFF', '#FFFFFF', '#FFFFFF'],
        ['#000000', '#FFFFFF', '#FFFFFF', '#000000', '#000000'],
        ['#FFFFFF', '#FFFFFF', '#000000', '#000000', '#000000'],
        ['#FFFFFF', '#000000', '#000000', '#000000', '#000000'],
        ['#000000', '#000000', '#000000', '#000000', '#000000'],
    ],
    'freq_boundaries': [1e-5, 1e-4, 1e-3, 1e-2],
    # Template-owned consequence categories.  Short risk-matrix codes and
    # their full descriptions remain separate just as for the two axes.
    'consequence_categories': [
        {'key': 'person', 'name': 'Person', 'color': '#2563eb',
         'descriptions': ['Ingen skada eller hälsoeffekt', 'Liten skada eller hälsoeffekt',
                          'Begränsad skada eller hälsoeffekt', 'Stor skada eller hälsoeffekt',
                          'Bestående total invaliditet eller upp till 3 döda', '4 eller fler döda']},
        {'key': 'miljo', 'name': 'Miljö', 'color': '#16a34a',
         'descriptions': ['Ingen miljöpåverkan', 'Liten lokal påverkan', 'Begränsad påverkan',
                          'Stor lokal eller regional påverkan', 'Allvarlig regional påverkan',
                          'Omfattande eller långvarig miljöpåverkan']},
        {'key': 'ekonomi', 'name': 'Ekonomi', 'color': '#d97706',
         'descriptions': ['Obetydlig kostnad', 'Mindre kostnad', 'Betydande kostnad',
                          'Stor ekonomisk skada', 'Mycket stor ekonomisk skada',
                          'Kritisk ekonomisk skada']},
        {'key': 'tillgangar', 'name': 'Tillgångar', 'color': '#d97706',
         'descriptions': ['Ingen egendomsskada', 'Mindre skada', 'Betydande skada',
                          'Stor skada', 'Mycket stor skada eller långt stopp',
                          'Förlust av anläggning eller verksamhet']},
        {'key': 'rykte', 'name': 'Rykte', 'color': '#475569',
         'descriptions': ['Ingen extern påverkan', 'Lokal uppmärksamhet', 'Regional uppmärksamhet',
                          'Nationell uppmärksamhet', 'Omfattande negativ uppmärksamhet',
                          'Långvarig internationell uppmärksamhet']},
    ],
}


class DraggableColorSwatch(QLabel):
    """Draggable color swatch in the palette — drag onto a matrix cell."""

    def __init__(self, name: str, color: str, fg_color: str = None, parent=None):
        super().__init__(name, parent)
        self._name     = name
        self._color    = color
        self._fg_color = fg_color  # None = auto-calculated from luminance
        self.setFixedSize(76, 28)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._refresh()

    def _refresh(self):
        r, g, b = int(self._color[1:3], 16), int(self._color[3:5], 16), int(self._color[5:7], 16)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        auto_txt = '#000' if lum > 160 else '#fff'
        txt = self._fg_color if self._fg_color else auto_txt
        self.setStyleSheet(
            f"background:{self._color}; color:{txt}; font-weight:bold; font-size:10px;"
            f"border:1px solid #555; border-radius:4px;")
        self.setText(self._name)

    def set_swatch(self, name: str, color: str, fg_color: str = None):
        self._name = name; self._color = color; self._fg_color = fg_color
        self._refresh()

    def name(self):     return self._name
    def color(self):    return self._color
    def fg_color(self): return self._fg_color

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_PALETTE_MIME,
                         json.dumps({'color': self._color, 'name': self._name,
                                     'fg_color': self._fg_color or '#ffffff'}).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(event.position().toPoint())
            drag.exec(Qt.DropAction.CopyAction)
        else:
            super().mousePressEvent(event)


class MatrixCellButton(QPushButton):
    """Risk matrix cell — collapsed-border grid (no double-lines between cells)."""

    def __init__(self, row, col, color, label, fg_color='#ffffff',
                 is_top_row=False, is_left_col=False, parent=None):
        super().__init__(label, parent)
        self.row = row
        self.col = col
        self._color    = color
        self._fg_color = fg_color
        self._label    = label
        self._is_top   = is_top_row
        self._is_left  = is_left_col
        # Width is controlled by the resizable matrix splitter; only the
        # cell height is fixed so the grid can be narrowed or widened without
        # fighting a widget-level fixed width.
        self.setFixedHeight(40)
        self.setMinimumWidth(30)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_start = None
        self._drag_in_progress = False
        self._apply_style()

    def _apply_style(self):
        top  = "border-top:1px solid #444;"  if self._is_top  else ""
        left = "border-left:1px solid #444;" if self._is_left else ""
        self.setStyleSheet(
            f"QPushButton{{"
            f"background:{self._color}; color:{self._fg_color}; font-weight:bold;"
            f"border-bottom:1px solid #444; border-right:1px solid #444;"
            f"{top}{left}"
            f"border-radius:0px; margin:0px; padding:0px;}}"
            # Do not change the border thickness or margin on hover. Those
            # values feed the grid's size calculation and made neighbouring
            # cells visibly jump while moving the pointer across the matrix.
            f"QPushButton:hover{{border-bottom:1px solid #111; border-right:1px solid #111;"
            f"{top}{left} border-radius:0px; margin:0px; padding:0px;}}")
        self.setText(self._label)

    def set_cell(self, color, label=None, fg_color=None):
        self._color = color
        if label is not None:
            self._label = label
        if fg_color is not None:
            self._fg_color = fg_color
        self._apply_style()

    def color(self):    return self._color
    def label(self):    return self._label
    def fg_color(self): return self._fg_color

    def _matrix_cell_payload(self):
        """The complete visual value copied between matrix cells."""
        return {'color': self._color, 'label': self._label,
                'fg_color': self._fg_color}

    def _apply_matrix_cell_payload(self, payload):
        """Copy another risk cell's visual value, including a blank label."""
        self.set_cell(str(payload.get('color') or self._color),
                      str(payload.get('label') or ''),
                      str(payload.get('fg_color') or self._fg_color))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position().toPoint()
            self._drag_in_progress = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_start is not None and
                event.buttons() & Qt.MouseButton.LeftButton and
                (event.position().toPoint() - self._drag_start).manhattanLength()
                >= QApplication.startDragDistance()):
            self._drag_in_progress = True
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_MATRIX_CELL_MIME,
                         json.dumps(self._matrix_cell_payload()).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(event.position().toPoint())
            drag.exec(Qt.DropAction.CopyAction)
            self._drag_start = None
            self.setDown(False)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_in_progress:
            self._drag_in_progress = False
            self._drag_start = None
            self.setDown(False)
            event.accept()
            return
        self._drag_start = None
        super().mouseReleaseEvent(event)

    # ── Drag-and-drop ─────────────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if (event.mimeData().hasFormat(_PALETTE_MIME) or
                event.mimeData().hasFormat(_MATRIX_CELL_MIME)):
            self.setStyleSheet(
                f"background:{self._color}; color:white; font-weight:bold;"
                f"border:3px dashed #000;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._apply_style()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            data = json.loads(
                event.mimeData().data(_PALETTE_MIME).data().decode())
            self.set_cell(data['color'], data['name'], data.get('fg_color', '#ffffff'))
            event.acceptProposedAction()
        elif event.mimeData().hasFormat(_MATRIX_CELL_MIME):
            data = json.loads(
                event.mimeData().data(_MATRIX_CELL_MIME).data().decode())
            self._apply_matrix_cell_payload(data)
            event.acceptProposedAction()
        else:
            event.ignore()


class RiskScaleLevelList(QListWidget):
    """One side of a risk-scale mapping drag and drop operation."""

    level_dropped = pyqtSignal(str, int, int)  # kind, source ordinal, target ordinal

    def __init__(self, kind, accepts_drops=False, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._accepts_drops = accepts_drops
        self.setDragEnabled(not accepts_drops)
        self.setAcceptDrops(accepts_drops)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setStyleSheet(
            "QListWidget{border:1px solid #9ca3af; background:#fff;}"
            "QListWidget::item{padding:4px 6px;}"
            "QListWidget::item:hover{background:#eef2f7;}")

    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_RISK_LEVEL_MIME, json.dumps({
            'kind': self._kind,
            'value': int(item.data(Qt.ItemDataRole.UserRole)),
        }).encode())
        drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(self.visualItemRect(item)))
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event):
        if not self._accepts_drops or not event.mimeData().hasFormat(_RISK_LEVEL_MIME):
            event.ignore()
            return
        try:
            data = json.loads(event.mimeData().data(_RISK_LEVEL_MIME).data().decode())
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        if data.get('kind') == self._kind:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        if target is None or not event.mimeData().hasFormat(_RISK_LEVEL_MIME):
            event.ignore()
            return
        try:
            data = json.loads(event.mimeData().data(_RISK_LEVEL_MIME).data().decode())
            source = int(data['value'])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            event.ignore()
            return
        if data.get('kind') != self._kind:
            event.ignore()
            return
        self.level_dropped.emit(self._kind, source,
                                int(target.data(Qt.ItemDataRole.UserRole)))
        event.acceptProposedAction()


class AxisMappingChip(QPushButton):
    """Clickable and draggable axis step shared by both migration views."""

    def __init__(self, canvas, role, kind, value, code, description='', compact=False):
        text = code if compact else f"{code}   {description or '—'}"
        super().__init__(text, canvas)
        self.canvas, self.role = canvas, role
        self.kind, self.value = kind, value
        self.code, self.description = code, description
        self.compact = compact
        self._pressed = False
        self.setToolTip(description or code)
        self.setCursor(Qt.CursorShape.OpenHandCursor if role == 'old'
                       else Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(28 if compact else 31)
        self.setStyleSheet("text-align:left; padding:3px 6px;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.canvas.chip_pressed(self, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed and event.buttons() & Qt.MouseButton.LeftButton:
            self.canvas.chip_moved(self, event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            self.canvas.chip_released(self, event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def apply_state(self, armed=False, mapped=False, over=False, count=0):
        if armed:
            border, background = '#1d2d3d', '#dbeafe'
        elif over:
            border, background = '#1d2d3d', '#e0f2fe'
        elif mapped:
            border, background = '#2f5fd0', '#eef4ff'
        else:
            border, background = '#8b949e', '#ffffff'
        suffix = f"   ({count})" if self.role == 'target' and count else ''
        text = self.code if self.compact else f"{self.code}   {self.description or '—'}"
        self.setText(text + suffix)
        self.setStyleSheet(
            f"QPushButton{{background:{background}; border:1px solid {border};"
            "border-radius:0px; text-align:left; padding:3px 6px; font-size:10px;}"
            "QPushButton:hover{background:#eef2f7;}"
            "QPushButton:pressed{background:#dbeafe;}" )


class _AxisMappingCanvas(QWidget):
    """Base canvas that draws links behind interactive mapping chips."""

    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog = dialog
        self.old_chips = {}
        self.target_chips = {}
        self.drag_chip = None
        self.drag_point = None
        self.drag_over = None
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setHorizontalSpacing(80)
        self._layout.setVerticalSpacing(6)

    @staticmethod
    def _section_label(text):
        label = QLabel(text)
        label.setStyleSheet("font-size:9px; font-weight:bold; letter-spacing:1px; color:#374151;")
        return label

    def clear_canvas(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.old_chips.clear(); self.target_chips.clear()

    def add_chip(self, role, kind, value, code, description, row, col, compact=False):
        chip = AxisMappingChip(self, role, kind, value, code, description, compact)
        self._layout.addWidget(chip, row, col)
        (self.old_chips if role == 'old' else self.target_chips)[(kind, value)] = chip
        return chip

    def _chip_at(self, global_pos):
        widget = QApplication.widgetAt(global_pos)
        while widget is not None and not isinstance(widget, AxisMappingChip):
            widget = widget.parentWidget()
        return widget

    def chip_pressed(self, chip, global_pos):
        if chip.role == 'old':
            self.drag_chip = chip
            self.drag_point = self.mapFromGlobal(global_pos)
            self.dialog.activate_old_step(chip.kind, chip.value)
        else:
            self.dialog.activate_target_step(chip.kind, chip.value)
        self.sync_state()

    def chip_moved(self, chip, global_pos):
        if chip is not self.drag_chip:
            return
        target = self._chip_at(global_pos)
        self.drag_point = self.mapFromGlobal(global_pos)
        self.drag_over = (target if target and target.role == 'target' and
                          target.kind == chip.kind else None)
        self.sync_state()

    def chip_released(self, chip, global_pos):
        if chip is self.drag_chip:
            target = self._chip_at(global_pos)
            if target and target.role == 'target' and target.kind == chip.kind:
                self.dialog.set_axis_mapping(chip.kind, chip.value, target.value)
            self.drag_chip = self.drag_point = self.drag_over = None
        self.sync_state()

    def sync_state(self):
        armed = self.dialog.armed
        for key, chip in self.old_chips.items():
            chip.apply_state(armed=(key == armed), mapped=self.dialog.is_mapped(*key))
        for key, chip in self.target_chips.items():
            chip.apply_state(mapped=self.dialog.target_count(*key) > 0,
                             over=(chip is self.drag_over),
                             count=self.dialog.target_count(*key))
        self.update()

    def _chip_center(self, chip, side):
        # Chips in the matrix tab are children of a group box, while chips in
        # the link tab are direct children of the canvas.  Map their local
        # coordinates so the links use one common coordinate system.
        if side == 'right':
            local = QPoint(chip.width(), chip.height() // 2)
        elif side == 'left':
            local = QPoint(0, chip.height() // 2)
        elif side == 'top':
            local = QPoint(chip.width() // 2, 0)
        else:
            local = QPoint(chip.width() // 2, chip.height())
        return QPointF(chip.mapTo(self, local))

    def _draw_curve(self, painter, start, end, dashed=False, arrow=False, fan=0):
        dx = max(45, min(120, abs(end.x() - start.x()) * .30) + fan)
        path = QPainterPath(start)
        path.cubicTo(start.x() + dx, start.y(), end.x() - dx, end.y(), end.x(), end.y())
        pen = QPen(QColor('#5980a6' if dashed else '#1d1f20'), 1.5)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if arrow:
            painter.setBrush(QColor('#1d1f20')); painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF([
                QPointF(end.x(), end.y()), QPointF(end.x() - 7, end.y() - 4),
                QPointF(end.x() - 7, end.y() + 4)]))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, ((kind, source), target) in enumerate(self.dialog.iter_mappings()):
            old = self.old_chips.get((kind, source))
            new = self.target_chips.get((kind, target))
            if old and new:
                self._draw_curve(painter, self._chip_center(old, 'right'),
                                 self._chip_center(new, 'left'),
                                 arrow=getattr(self, 'arrow_links', False),
                                 fan=index * (13 if getattr(self, 'arrow_links', False) else 7))
        if self.drag_chip and self.drag_point:
            self._draw_curve(painter, self._chip_center(self.drag_chip, 'right'),
                             self.drag_point, dashed=True)


class AxisLinkField(_AxisMappingCanvas):
    """Tab 1a: list-to-list mapping with visible curved links."""

    def rebuild(self):
        self.clear_canvas()
        row = 0
        for kind, title in (('severity', 'KONSEKVENS'), ('frequency', 'FREKVENS / SANNOLIKHET')):
            self._layout.addWidget(self._section_label(title), row, 0, 1, 3); row += 1
            self._layout.addWidget(self._section_label('GAMMAL MATRIS'), row, 0)
            self._layout.addWidget(self._section_label('NY MATRIS'), row, 2); row += 1
            old_levels = self.dialog.axis_levels('source', kind)
            new_levels = self.dialog.axis_levels('target', kind)
            for index in range(max(len(old_levels), len(new_levels))):
                if index < len(old_levels):
                    value, code, description = old_levels[index]
                    self.add_chip('old', kind, value, code, description, row + index, 0)
                if index < len(new_levels):
                    value, code, description = new_levels[index]
                    self.add_chip('target', kind, value, code, description, row + index, 2)
            row += max(len(old_levels), len(new_levels)) + 2
        self._layout.setColumnStretch(0, 1); self._layout.setColumnStretch(1, 1); self._layout.setColumnStretch(2, 1)
        QTimer.singleShot(0, self.sync_state)


class MatrixAgainstMatrix(_AxisMappingCanvas):
    """Tab 1b: two real coloured matrices linked only through their axes."""

    arrow_links = True

    def rebuild(self):
        self.clear_canvas()
        old = self._matrix_widget('source', 'Nuvarande matris')
        new = self._matrix_widget('target', 'Ny matris')
        self._layout.addWidget(old, 0, 0)
        self._layout.addWidget(new, 2, 2)
        self._layout.setColumnStretch(0, 1); self._layout.setColumnStretch(1, 1); self._layout.setColumnStretch(2, 1)
        self._layout.setRowStretch(0, 1); self._layout.setRowStretch(1, 1); self._layout.setRowStretch(2, 1)
        QTimer.singleShot(0, self.sync_state)

    def _matrix_widget(self, side, title):
        cfg = self.dialog.matrix_cfg(side)
        group = QGroupBox(title)
        grid = QGridLayout(group); grid.setSpacing(0)
        x_kind = self.dialog.display_x_axis
        y_kind = 'severity' if x_kind == 'frequency' else 'frequency'
        x_levels = self.dialog.axis_levels(side, x_kind)
        y_levels = self.dialog.axis_levels(side, y_kind)
        # Preserve the saved visual orientation of each real matrix.  The
        # mappable ordinal remains attached to the chip, so the view order
        # never changes the migration meaning.
        stored_x_kind = ('frequency' if cfg.get('x_axis', 'frequency') == 'frequency'
                         else 'severity')
        x_reversed = cfg.get('x_reversed', False) if x_kind == stored_x_kind \
            else cfg.get('y_reversed', False)
        y_reversed = cfg.get('y_reversed', False) if y_kind != stored_x_kind \
            else cfg.get('x_reversed', False)
        if x_reversed:
            x_levels = list(reversed(x_levels))
        if not y_reversed:
            y_levels = list(reversed(y_levels))
        corner = QLabel('K \\ F' if x_kind == 'frequency' else 'F \\ K')
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setStyleSheet('font-size:9px; color:#555; border:1px solid #888;')
        grid.addWidget(corner, 0, 0)
        role = 'old' if side == 'source' else 'target'
        for col, (value, code, description) in enumerate(x_levels, start=1):
            chip = AxisMappingChip(self, role, x_kind, value, code, description, compact=True)
            grid.addWidget(chip, 0, col)
            (self.old_chips if role == 'old' else self.target_chips)[(x_kind, value)] = chip
        for row, (value, code, description) in enumerate(y_levels, start=1):
            chip = AxisMappingChip(self, role, y_kind, value, code, description, compact=True)
            grid.addWidget(chip, row, 0)
            (self.old_chips if role == 'old' else self.target_chips)[(y_kind, value)] = chip
            for col, (x_value, _x_code, _x_desc) in enumerate(x_levels, start=1):
                cons = value if y_kind == 'severity' else x_value
                freq = value if y_kind == 'frequency' else x_value
                ci, fi = int(cons) - 1, int(freq) + 1
                try: color = cfg['cell_colors'][ci][fi]
                except (KeyError, IndexError): color = '#ffffff'
                try: text = cfg['cell_labels'][ci][fi]
                except (KeyError, IndexError): text = ''
                cell = QLabel(text)
                cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setWordWrap(True)
                cell.setToolTip(f"Celltext: {text}" if text else 'Tom celltext')
                cell.setMinimumSize(48, 32)
                try: foreground = cfg['cell_fg_colors'][ci][fi]
                except (KeyError, IndexError): foreground = '#000000'
                cell.setStyleSheet(
                    f"background:{color}; color:{foreground}; border:1px solid #444; "
                    "font-size:8px; padding:1px;")
                grid.addWidget(cell, row, col)
        return group


class CategoryMappingChip(QPushButton):
    """One draggable source/target category chip in the mapping drawer."""

    def __init__(self, panel, role, key, name, color):
        super().__init__(name, panel)
        self.panel, self.role, self.key = panel, role, key
        self.color = color or '#64748b'
        self._pressed = False
        self.setCursor(Qt.CursorShape.OpenHandCursor if role == 'source'
                       else Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(28)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.panel.category_pressed(self, event.globalPosition().toPoint())
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed and event.buttons() & Qt.MouseButton.LeftButton:
            self.panel.category_moved(self, event.globalPosition().toPoint())
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            self.panel.category_released(self, event.globalPosition().toPoint())
            event.accept(); return
        super().mouseReleaseEvent(event)

    def set_state(self, armed=False, mapped=False, over=False):
        background = '#dbeafe' if armed else ('#eef4ff' if mapped else '#ffffff')
        border = '#1d2d3d' if armed or over else self.color
        self.setStyleSheet(
            f"QPushButton{{background:{background}; border:1px solid {border}; "
            "border-radius:0px; padding:3px 6px; text-align:left; font-size:10px;}"
            "QPushButton:hover{background:#eef2f7;}")


class CategoryMappingPanel(QWidget):
    """Collapsible category and per-category severity conversion editor."""

    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog = dialog
        self.source_chips, self.target_chips = {}, {}
        self.drag_chip = self.drag_over = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(5)

    def _chip_at(self, global_pos):
        widget = QApplication.widgetAt(global_pos)
        while widget is not None and not isinstance(widget, CategoryMappingChip):
            widget = widget.parentWidget()
        return widget

    def category_pressed(self, chip, _global_pos):
        if chip.role == 'source':
            self.drag_chip = chip
            self.dialog.activate_source_category(chip.key)
        else:
            self.dialog.activate_target_category(chip.key)
        self.sync_state()

    def category_moved(self, chip, global_pos):
        if chip is not self.drag_chip:
            return
        target = self._chip_at(global_pos)
        self.drag_over = target if target and target.role == 'target' else None
        self.sync_state()

    def category_released(self, chip, global_pos):
        if chip is self.drag_chip:
            target = self._chip_at(global_pos)
            if target and target.role == 'target':
                self.dialog.set_category_mapping(chip.key, target.key)
        self.drag_chip = self.drag_over = None
        self.sync_state()

    def rebuild(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.source_chips.clear(); self.target_chips.clear()
        title = QLabel("Koppla varje befintlig kategori till en kategori i den nya mallen.")
        title.setStyleSheet("color:#4b5563; font-size:10px;")
        self._layout.addWidget(title)
        links = QWidget(); grid = QGridLayout(links)
        grid.setContentsMargins(0, 0, 0, 0); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(4)
        source_header = QLabel("BEFINTLIG KATEGORI")
        target_header = QLabel("NY MALLKATEGORI")
        for header in (source_header, target_header):
            header.setStyleSheet("color:#374151; font-size:9px; font-weight:bold;")
        grid.addWidget(source_header, 0, 0)
        grid.addWidget(target_header, 0, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        source_categories = self.dialog.plan.get('source_categories', [])
        target_categories = self.dialog.plan.get('target_categories', [])
        for row, category in enumerate(source_categories, start=1):
            chip = CategoryMappingChip(self, 'source', str(category['source_id']),
                                       category['name'], category.get('color'))
            self.source_chips[chip.key] = chip; grid.addWidget(chip, row, 0)
            arrow = QLabel("→")
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            arrow.setStyleSheet("color:#4b5563; font-size:14px; font-weight:bold;")
            grid.addWidget(arrow, row, 1)
        for row, category in enumerate(target_categories, start=1):
            chip = CategoryMappingChip(self, 'target', category['key'],
                                       category['name'], category.get('color'))
            self.target_chips[chip.key] = chip; grid.addWidget(chip, row, 2)
        self._layout.addWidget(links)

        converters = QGroupBox("Nivåöversättning per kategori")
        converters_lay = QVBoxLayout(converters)
        for category in source_categories:
            source_id = str(category['source_id'])
            target = self.dialog.target_category(source_id)
            toggle = QToolButton()
            toggle.setCheckable(True); toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            target_name = target.get('name') if target else 'välj mallkategori'
            toggle.setText(f"{category['name']}  →  {target_name}")
            toggle.setArrowType(Qt.ArrowType.RightArrow)
            body = QWidget(); form = QFormLayout(body); form.setContentsMargins(18, 0, 0, 4)
            for source_value, source_code, _source_description in self.dialog.axis_levels('source', 'severity'):
                combo = QComboBox()
                for target_value, target_code, target_description in self.dialog.axis_levels('target', 'severity'):
                    combo.addItem(f"{target_code} — {target_description}" if target_description else target_code,
                                  target_value)
                combo.setCurrentIndex(max(0, combo.findData(
                    self.dialog.category_level_target(source_id, source_value))))
                combo.currentIndexChanged.connect(
                    lambda _index, sid=source_id, sv=source_value, c=combo:
                    self.dialog.set_category_level_mapping(sid, sv, c.currentData()))
                form.addRow(QLabel(source_code), combo)
            body.setVisible(False)
            toggle.toggled.connect(body.setVisible)
            toggle.toggled.connect(lambda checked, t=toggle: t.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
            converters_lay.addWidget(toggle); converters_lay.addWidget(body)
        self._layout.addWidget(converters)
        QTimer.singleShot(0, self.sync_state)

    def sync_state(self):
        armed = self.dialog.category_armed
        for key, chip in self.source_chips.items():
            chip.set_state(armed=(key == armed), mapped=self.dialog.category_mapped(key))
        for key, chip in self.target_chips.items():
            chip.set_state(mapped=self.dialog.category_target_count(key) > 0,
                           over=(chip is self.drag_over))


class RiskMatrixMigrationDialog(QDialog):
    """Review and explicitly map HAZOP data before changing matrix template."""

    def __init__(self, db, source_cfg, target_cfg, template_name, parent=None):
        super().__init__(parent)
        self.db = db
        self.template_name = template_name
        self.plan = db.risk_matrix_migration_preview(source_cfg, target_cfg)
        self.armed = None
        self.category_armed = None
        self._display_x_axis = ('frequency' if self.plan['source_matrix'].get(
            'x_axis', 'frequency') == 'frequency' else 'severity')
        self._mapping = {}
        for kind, map_key in (('frequency', 'frequency_map'), ('severity', 'severity_map')):
            self._mapping.update({(kind, int(source)): int(target)
                                  for source, target in self.plan[map_key].items()})
        self._category_mapping = dict(self.plan.get('category_map', {}))
        self.setWindowTitle(f"Byt riskmatris till {template_name}")
        self.setMinimumSize(1060, 720)
        self.resize(1220, 820)

        outer = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Visa X-axel som:"))
        self._x_frequency = QToolButton(); self._x_frequency.setText("X = Frekvens")
        self._x_consequence = QToolButton(); self._x_consequence.setText("X = Konsekvens")
        for button in (self._x_frequency, self._x_consequence):
            button.setCheckable(True)
            button.setStyleSheet("QToolButton{border:1px solid #8b949e; padding:4px 8px;}"
                                 "QToolButton:checked{background:#dbeafe; border-color:#1d2d3d;}")
            toolbar.addWidget(button)
        self._x_frequency.setChecked(self._display_x_axis == 'frequency')
        self._x_consequence.setChecked(self._display_x_axis == 'severity')
        self._x_frequency.clicked.connect(lambda: self._set_display_x_axis('frequency'))
        self._x_consequence.clicked.connect(lambda: self._set_display_x_axis('severity'))
        toolbar.addStretch()
        auto_button = QPushButton("Föreslå automatiskt")
        auto_button.clicked.connect(self.suggest_automatically)
        clear_button = QPushButton("Rensa")
        clear_button.clicked.connect(self.clear_mappings)
        toolbar.addWidget(auto_button); toolbar.addWidget(clear_button)
        outer.addLayout(toolbar)

        self._progress = QLabel()
        self._progress.setStyleSheet("background:#f3f4f6; border:1px solid #9ca3af; padding:5px 7px;")
        outer.addWidget(self._progress)

        self._tabs = QTabWidget()
        self._category_panel = CategoryMappingPanel(self)
        self._link_field = AxisLinkField(self)
        self._matrix_against_matrix = MatrixAgainstMatrix(self)
        self._tabs.addTab(self._category_panel, "1. Konsekvenskategorier")
        self._tabs.addTab(self._link_field, "2. Kopplingsfält")
        self._tabs.addTab(self._matrix_against_matrix, "3. Matris mot matris")
        outer.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox()
        self._apply_button = buttons.addButton("Genomför migrering", QDialogButtonBox.ButtonRole.AcceptRole)
        self._apply_button.setStyleSheet("background:#b45309; color:white; font-weight:bold; padding:5px 12px;")
        cancel_button = buttons.addButton("Avbryt", QDialogButtonBox.ButtonRole.RejectRole)
        self._apply_button.clicked.connect(self._confirm_accept)
        cancel_button.clicked.connect(self.reject)
        outer.addWidget(buttons)
        self._refresh_all()

    @property
    def display_x_axis(self):
        return self._display_x_axis

    def matrix_cfg(self, side):
        return self.plan['source_matrix' if side == 'source' else 'target_matrix']

    def axis_levels(self, side, kind):
        return self._axis_levels(self.matrix_cfg(side), kind)

    def _mapping_key(self, kind, value):
        return (kind, int(value))

    def iter_mappings(self):
        return sorted(self._mapping.items(), key=lambda item: (item[0][0], item[0][1]))

    def is_mapped(self, kind, value):
        return self._mapping_key(kind, value) in self._mapping

    def target_count(self, kind, target):
        return sum(1 for (mapped_kind, _source), mapped_target in self._mapping.items()
                   if mapped_kind == kind and mapped_target == target)

    def target_category(self, source_id):
        key = self._category_mapping.get(str(source_id))
        return next((category for category in self.plan.get('target_categories', [])
                     if category.get('key') == key), None)

    def category_mapped(self, source_id):
        return str(source_id) in self._category_mapping

    def category_target_count(self, target_key):
        return sum(1 for key in self._category_mapping.values() if key == target_key)

    def category_level_target(self, source_id, source_value):
        mapping = self.plan.get('category_severity_maps', {}).get(str(source_id), {})
        return mapping.get(str(source_value), self.plan['severity_map'].get(str(source_value), 1))

    def activate_source_category(self, source_id):
        source_id = str(source_id)
        if self.category_mapped(source_id) and self.category_armed != source_id:
            self.remove_category_mapping(source_id)
            return
        self.category_armed = None if self.category_armed == source_id else source_id
        self._refresh_visuals()

    def activate_target_category(self, target_key):
        if self.category_armed:
            self.set_category_mapping(self.category_armed, target_key)

    def set_category_mapping(self, source_id, target_key):
        source_id = str(source_id)
        previous_source = next((source for source, target in self._category_mapping.items()
                                if target == target_key and source != source_id), None)
        if previous_source:
            self._category_mapping.pop(previous_source, None)
        self._category_mapping[source_id] = target_key
        self.plan['category_map'] = dict(self._category_mapping)
        self.category_armed = None
        self._sync_category_record_targets(source_id)
        if previous_source:
            self._sync_category_record_targets(previous_source)
        self._category_panel.rebuild()
        self._refresh_visuals()

    def remove_category_mapping(self, source_id):
        self._category_mapping.pop(str(source_id), None)
        self.plan['category_map'] = dict(self._category_mapping)
        self.category_armed = None
        self._category_panel.rebuild()
        self._refresh_visuals()

    def set_category_level_mapping(self, source_id, source_value, target_value):
        source_id = str(source_id)
        mapping = self.plan.setdefault('category_severity_maps', {}).setdefault(source_id, {})
        mapping[str(source_value)] = int(target_value)
        self._sync_category_record_targets(source_id)
        self._refresh_visuals()

    def _sync_category_record_targets(self, source_id):
        source_id = str(source_id)
        mapping = self.plan.get('category_severity_maps', {}).get(source_id, {})
        target_key = self._category_mapping.get(source_id)
        for record in self.plan.get('severity_records', []):
            if str(record.get('category_id')) == source_id:
                record['target'] = mapping.get(str(record['source']), record['target'])
        for record in self.plan.get('definition_records', []):
            if str(record.get('category_id')) == source_id:
                record['target'] = mapping.get(str(record['source']), record['target'])
                record['target_category_key'] = target_key

    def activate_old_step(self, kind, value):
        key = self._mapping_key(kind, value)
        if key in self._mapping and self.armed != key:
            self.remove_axis_mapping(kind, value)
            return
        self.armed = None if self.armed == key else key
        self._refresh_visuals()

    def activate_target_step(self, kind, value):
        if self.armed and self.armed[0] == kind:
            self.set_axis_mapping(kind, self.armed[1], value)

    def _apply_mapping_to_plan(self, kind, source, target):
        map_key = 'frequency_map' if kind == 'frequency' else 'severity_map'
        self.plan[map_key][str(source)] = target
        records_key = 'frequency_records' if kind == 'frequency' else 'severity_records'
        for record in self.plan[records_key]:
            if record.get('source') == source and not record.get('override'):
                if kind != 'frequency' or record.get('source_kind') == 'manual':
                    record['target'] = target
        if kind == 'severity':
            for record in self.plan['definition_records']:
                if record.get('source') == source and not record.get('override'):
                    record['target'] = target

    def set_axis_mapping(self, kind, source, target):
        self._mapping[self._mapping_key(kind, source)] = int(target)
        self._apply_mapping_to_plan(kind, int(source), int(target))
        self.armed = None
        self._refresh_visuals()

    def remove_axis_mapping(self, kind, source):
        key = self._mapping_key(kind, source)
        self._mapping.pop(key, None)
        map_key = 'frequency_map' if kind == 'frequency' else 'severity_map'
        self.plan[map_key].pop(str(source), None)
        records_key = 'frequency_records' if kind == 'frequency' else 'severity_records'
        for record in self.plan[records_key]:
            if record.get('source') == source and not record.get('override'):
                if kind != 'frequency' or record.get('source_kind') == 'manual':
                    record['target'] = None
        if kind == 'severity':
            for record in self.plan['definition_records']:
                if record.get('source') == source and not record.get('override'):
                    record['target'] = None
        self.armed = None
        self._refresh_visuals()

    def clear_mappings(self):
        self._mapping.clear(); self.armed = None; self.category_armed = None
        self._category_mapping.clear(); self.plan['category_map'] = {}
        self.plan['frequency_map'].clear(); self.plan['severity_map'].clear()
        for record in self.plan['frequency_records']:
            if record.get('source_kind') == 'manual' and not record.get('override'):
                record['target'] = None
        for record in self.plan['severity_records']:
            if not record.get('override'):
                record['target'] = None
        for record in self.plan['definition_records']:
            if not record.get('override'):
                record['target'] = None
                record['target_category_key'] = None
        self._category_panel.rebuild()
        self._refresh_visuals()

    def suggest_automatically(self):
        source = self.plan['source_matrix']; target = self.plan['target_matrix']
        frequency = self.db._rank_level_map(source['cols'], target['cols'], -1, -1)
        severity = self.db._rank_level_map(source['rows'], target['rows'], 1, 1)
        self._mapping.clear()
        self.plan['frequency_map'].clear(); self.plan['severity_map'].clear()
        for kind, mapping in (('frequency', frequency), ('severity', severity)):
            for old, new in mapping.items():
                self._mapping[(kind, int(old))] = int(new)
                self._apply_mapping_to_plan(kind, int(old), int(new))
        self.armed = None
        # Category suggestions use the safe proposal produced by the database
        # (matching stable key/name first, then position), while retaining a
        # separately editable severity map for each category.
        self._category_mapping = dict(self.db.risk_matrix_migration_preview(
            self.plan['source_matrix'], self.plan['target_matrix']).get('category_map', {}))
        self.plan['category_map'] = dict(self._category_mapping)
        self.plan['category_severity_maps'] = {
            str(category['source_id']): dict(severity)
            for category in self.plan.get('source_categories', [])
        }
        for source_id in self._category_mapping:
            self._sync_category_record_targets(source_id)
        self._category_panel.rebuild()
        self._refresh_visuals()

    def _set_display_x_axis(self, kind):
        if kind == self._display_x_axis:
            return
        self._display_x_axis = kind
        self._x_frequency.setChecked(kind == 'frequency')
        self._x_consequence.setChecked(kind == 'severity')
        # Switching the display axis is a new mapping session.  This avoids
        # carrying an interpretation made in the other orientation.
        self.clear_mappings()
        self._matrix_against_matrix.rebuild()
        self._refresh_visuals()

    def _mapping_complete(self):
        expected = sum(len(self.axis_levels('source', kind))
                       for kind in ('frequency', 'severity'))
        categories_complete = len(self._category_mapping) == len(
            self.plan.get('source_categories', []))
        return len(self._mapping) == expected and categories_complete

    def _refresh_visuals(self):
        self._link_field.sync_state()
        self._matrix_against_matrix.sync_state()
        self._category_panel.sync_state()
        self._refresh_summary()

    @staticmethod
    def _axis_levels(cfg, kind):
        """Return stored ordinal, short code and optional hover description."""
        if kind == 'frequency':
            codes, descriptions, start = cfg.get('x_codes', []), cfg.get('x_labels', []), -1
            count = cfg.get('cols', 0)
        else:
            codes, descriptions, start = cfg.get('y_codes', []), cfg.get('y_labels', []), 1
            count = cfg.get('rows', 0)
        result = []
        for index in range(count):
            fallback = f"F{index - 1}" if kind == 'frequency' else str(index + 1)
            result.append((index + start,
                           str(codes[index]) if index < len(codes) else fallback,
                           str(descriptions[index]) if index < len(descriptions) else ''))
        return result

    def _swap_target_axes(self):
        self._set_display_x_axis(
            'severity' if self.display_x_axis == 'frequency' else 'frequency')

    def _on_level_dropped(self, kind, source, target):
        self._set_global_mapping(kind, source, target)

    def _set_global_mapping(self, kind, source, target):
        """Compatibility entry point for the retained drag helper/tests."""
        self.set_axis_mapping(kind, source, target)

    def _set_record_mapping(self, records_key, index, value):
        self.plan[records_key][index]['target'] = value
        self.plan[records_key][index]['override'] = True
        self._refresh_summary()

    def _refresh_summary(self):
        total = sum(len(self.axis_levels('source', kind))
                    for kind in ('frequency', 'severity'))
        mapped = len(self._mapping)
        category_total = len(self.plan.get('source_categories', []))
        category_mapped = len(self._category_mapping)
        self._progress.setText(
            f"{mapped} av {total} gamla steg och {category_mapped} av "
            f"{category_total} kategorier mappade. "
            "Inget sparas förrän du genomför migreringen.")
        self._apply_button.setEnabled(mapped == total)

    def _refresh_all(self):
        self._link_field.rebuild()
        self._matrix_against_matrix.rebuild()
        self._category_panel.rebuild()
        self._refresh_summary()

    def _confirm_accept(self):
        if not self._mapping_complete():
            QMessageBox.warning(
                self, "Ofullständig kartläggning",
                "Koppla varje gammalt frekvens- och konsekvenssteg innan "
                "migreringen kan genomföras.")
            return
        answer = QMessageBox.question(
            self, "Genomför mallbyte",
            "En backup skapas. Därefter används kartläggningen för att byta "
            "riskmatris. Vill du genomföra bytet?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.accept()




class HAZOPPreparationPanel(QWidget):
    """Administrative HAZOP-prep material, collected under its own top-level
    nav entry (2026-08-17, user request: "flytta om flikarna... Skapa en ny
    huvudflik i Claude med namnet HAZOP preperation. Fliken ska samla
    följande administrativa underlag: Projekt, Deltagare, Riskmatris,
    Standardorsaker... Denna fliken ska ligga ute i det svarta fältet till
    vänster högst upp") — these four used to live buried several clicks deep
    as tabs inside Inställningar; extracted here into their own page since
    Anton wanted them front-and-center. Placed at MainWindow.view_stack
    index 0 (see NOTES.md for why: not just visually first in the nav rail,
    Anton explicitly wants it structurally first, so every OTHER page's
    index shifts +1 — see the "_switch_view" renumbering that accompanies
    this class).

    "Riskmatris & Kategorier" brings essentially all of the OLD
    SettingsPanel's own methods along with it (17 of them) — before this
    split, that risk-matrix/palette/category editing WAS almost the entire
    class; SettingsPanel keeps only the tabs that were already their own
    standalone panel classes or simple inline forms unrelated to the matrix.

    Keeps its OWN `matrix_changed` signal (rather than somehow reaching
    across to SettingsPanel's) — SettingsPanel's TagDatabasePanel forwards
    its own settings_changed into a `matrix_changed` of its own for the same
    "please refresh" purpose (MainWindow._on_matrix_changed refreshes tree/
    scenario views generically, not just for matrix edits) — cleanest to let
    each panel own the exact signal for whatever changes it makes, and have
    MainWindow.__init__ connect both to the same handler."""

    matrix_changed = pyqtSignal()
    sheets_changed = pyqtSignal()
    structure_changed = pyqtSignal()   # a node was added/renamed from the Noder tab

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cell_buttons   = []
        self._x_label_edits  = []   # QLineEdit per column
        self._y_label_edits  = []   # QLineEdit per row (high→low)
        self._palette_swatches = []
        self._sev_def_edits  = {}   # (cat_id, sev_level) → editable description table item
        self._tor_report_fields = {}  # (tor|report, prepared|reviewed|approved) → QComboBox
        self._matrix_cell_width = _MATRIX_CELL_WIDTH_DEFAULT
        self._axes_loading = False
        self._axes_dirty = False

        tabs = QTabWidget()
        self._tabs = tabs   # kept as an attribute for testability (tabText() lookups)
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: Projekt ──────────────────────────────────────────────────────
        proj_tab = QWidget()
        proj_outer = QVBoxLayout(proj_tab)
        proj_outer.setContentsMargins(0, 0, 0, 0)
        proj_form_w = QWidget()
        pl = QFormLayout(proj_form_w)
        pl.setSpacing(10)
        pl.setContentsMargins(16, 16, 16, 16)
        proj_outer.addWidget(proj_form_w)

        self._proj_name = QLineEdit()
        self._proj_name.editingFinished.connect(
            lambda: self.db.set_config('project_name', self._proj_name.text()))
        pl.addRow("Projektnamn:", self._proj_name)

        self._proj_number = QLineEdit()
        self._proj_number.editingFinished.connect(
            lambda: self.db.set_config('project_number', self._proj_number.text()))
        pl.addRow("Projektnummer:", self._proj_number)

        self._proj_client = QLineEdit()
        self._proj_client.editingFinished.connect(
            lambda: self.db.set_config('project_client', self._proj_client.text()))
        pl.addRow("Kund/Företag:", self._proj_client)

        self._proj_facility = QLineEdit()
        self._proj_facility.editingFinished.connect(
            lambda: self.db.set_config('project_facility', self._proj_facility.text()))
        pl.addRow("Anläggning:", self._proj_facility)

        date_row_w = QWidget()
        date_row_w.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        date_row_l = QHBoxLayout(date_row_w)
        date_row_l.setContentsMargins(0, 0, 0, 0)
        date_row_l.setSpacing(6)
        self._proj_date_start = QDateEdit()
        self._proj_date_start.setCalendarPopup(True)
        self._proj_date_start.setDisplayFormat("yyyy-MM-dd")
        self._proj_date_end = QDateEdit()
        self._proj_date_end.setCalendarPopup(True)
        self._proj_date_end.setDisplayFormat("yyyy-MM-dd")
        _date_edit_w = QFontMetrics(self._proj_date_start.font()).horizontalAdvance(
            "9999-99-99") + 40
        self._proj_date_start.setMaximumWidth(_date_edit_w)
        self._proj_date_end.setMaximumWidth(_date_edit_w)
        self._proj_date_start.dateChanged.connect(
            lambda d: self.db.set_config('project_date_start', d.toString('yyyy-MM-dd')))
        self._proj_date_end.dateChanged.connect(
            lambda d: self.db.set_config('project_date_end', d.toString('yyyy-MM-dd')))
        self._proj_date_start_today_btn = QPushButton("Idag")
        self._proj_date_start_today_btn.setToolTip("Sätt startdatum till dagens datum")
        self._proj_date_start_today_btn.clicked.connect(
            lambda: self._proj_date_start.setDate(QDate.currentDate()))
        self._proj_date_end_today_btn = QPushButton("Idag")
        self._proj_date_end_today_btn.setToolTip("Sätt slutdatum till dagens datum")
        self._proj_date_end_today_btn.clicked.connect(
            lambda: self._proj_date_end.setDate(QDate.currentDate()))
        date_row_l.addWidget(self._proj_date_start)
        date_row_l.addWidget(self._proj_date_start_today_btn)
        date_row_l.addWidget(QLabel("  –  "))
        date_row_l.addWidget(self._proj_date_end)
        date_row_l.addWidget(self._proj_date_end_today_btn)
        pl.addRow("Datum (från–till):", date_row_w)

        # ── Revision: flera rader (Rev/Datum/Beskrivning) ────────────────────
        rev_box = QGroupBox("Revision")
        rev_lay = QVBoxLayout(rev_box)
        self._proj_rev_table = QTableWidget(0, 3)
        self._proj_rev_table.setHorizontalHeaderLabels(["Rev", "Datum", "Beskrivning"])
        self._proj_rev_table.horizontalHeader().setStretchLastSection(True)
        self._proj_rev_table.setColumnWidth(0, 60)
        self._proj_rev_table.setColumnWidth(1, 120)
        self._proj_rev_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._proj_rev_table.customContextMenuRequested.connect(self._proj_rev_context_menu)
        self._proj_rev_table.itemChanged.connect(self._on_proj_rev_item_changed)
        rev_lay.addWidget(self._proj_rev_table)
        rev_btn_row = QHBoxLayout()
        rev_add_btn = QPushButton("+ Lägg till rad")
        rev_add_btn.clicked.connect(self._add_project_revision_row)
        rev_btn_row.addWidget(rev_add_btn)
        rev_btn_row.addStretch()
        rev_lay.addLayout(rev_btn_row)
        proj_outer.addWidget(rev_box)

        # ── Egna fria fält ────────────────────────────────────────────────
        fields_box = QGroupBox("Egna fält")
        self._proj_fields_lay = QVBoxLayout(fields_box)
        self._proj_field_rows = {}   # field id -> (name_edit, value_edit)
        fields_add_btn = QPushButton("+ Lägg till fält")
        fields_add_btn.clicked.connect(self._add_project_custom_field_row)
        fields_btn_row = QHBoxLayout()
        fields_btn_row.addWidget(fields_add_btn)
        fields_btn_row.addStretch()
        self._proj_fields_lay.addLayout(fields_btn_row)
        proj_outer.addWidget(fields_box)
        proj_outer.addStretch()

        tabs.addTab(proj_tab, "Projekt")

        # ── Tab: Deltagare ────────────────────────────────────────────────────
        # Replaces the old free-text "Deltagare" field (2026-08-11, user
        # request: "skulle även gilla ... en till flik med deltagare
        # istället där man definerar förnamn, efternamn, roll på y axel och
        # analystillfälen på x axeln så det blir en matris" — "istället"
        # means this REPLACES the free-text field, not adds to it). See
        # ParticipantMatrixPanel below and NOTES.md for the schema/UI
        # design rationale.
        self._participant_matrix_panel = ParticipantMatrixPanel(self.db)
        tabs.addTab(self._participant_matrix_panel, "Deltagare")

        # ── Tab: ToR and Report ───────────────────────────────
        # Names are editable combo boxes: registered participants are offered
        # as suggestions, while an arbitrary free-text name remains valid.
        tor_report_tab = QWidget()
        tr_outer = QVBoxLayout(tor_report_tab)
        tr_outer.setContentsMargins(16, 16, 16, 16)
        tr_outer.setSpacing(12)
        intro = QLabel(
            "Ange ansvariga personer för Terms of Reference (ToR) och rapporten. "
            "Välj en deltagare eller skriv ett eget namn.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#666; font-size:10px;")
        tr_outer.addWidget(intro)

        self._tor_report_add_section(tr_outer, "tor", "ToR")
        self._tor_report_add_section(tr_outer, "report", "Report")
        tr_outer.addStretch()
        tabs.addTab(tor_report_tab, "ToR and Report")
        tabs.currentChanged.connect(self._on_prep_tab_changed)

        # ── Riskmatris: local subviews ────────────────────────────────────────
        # The main preparation navigation stays compact.  Riskmatris gets its
        # own second row only while that main tab is active: one view for the
        # coloured matrix and one (built below) for readable axis/category text.
        matrix_editor = QWidget()
        ml = QVBoxLayout(matrix_editor)
        ml.setSpacing(6)

        # Size row
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Konsekvens-fält:"))
        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(2, 15)
        self._rows_spin.setValue(5)
        self._rows_spin.setToolTip("Antal nivåer på konsekvens-axeln (C1…Cn)")
        size_row.addWidget(self._rows_spin)

        size_row.addWidget(QLabel("  Frekvens-fält:"))
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(2, 15)
        self._cols_spin.setValue(7)
        self._cols_spin.setToolTip("Antal nivåer på frekvens-axeln (F-1…Fn)")
        size_row.addWidget(self._cols_spin)
        size_row.addWidget(QLabel("  Cellbredd:"))
        self._matrix_cell_width_spin = QSpinBox()
        self._matrix_cell_width_spin.setRange(48, 260)
        self._matrix_cell_width_spin.setValue(_MATRIX_CELL_WIDTH_DEFAULT)
        self._matrix_cell_width_spin.setSuffix(" px")
        self._matrix_cell_width_spin.setToolTip(
            "Gemensam bredd för alla riskmatrisceller. "
            "Konsekvenskategoriernas bredd påverkas inte.")
        size_row.addWidget(self._matrix_cell_width_spin)
        size_row.addStretch()
        ml.addLayout(size_row)

        # ── Colour palette ────────────────────────────────────────────────────
        pal_box = QGroupBox("Färgpalett — dra en färg och släpp på en cell")
        pal_lay = QHBoxLayout(pal_box)
        pal_lay.setSpacing(4)
        self._palette_container = pal_lay

        add_col_btn = QPushButton("+ Lägg till")
        add_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        add_col_btn.clicked.connect(self._palette_add)
        pal_lay.addWidget(add_col_btn)

        edit_col_btn = QPushButton("Redigera")
        edit_col_btn.setIcon(_icon('edit'))
        edit_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        edit_col_btn.clicked.connect(self._palette_edit)
        pal_lay.addWidget(edit_col_btn)

        del_col_btn = QPushButton("Ta bort")
        del_col_btn.setIcon(_icon('delete'))
        del_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        del_col_btn.clicked.connect(self._palette_delete)
        pal_lay.addWidget(del_col_btn)

        pal_lay.addStretch()
        ml.addWidget(pal_box)

        # ── Matrix grid ───────────────────────────────────────────────────────
        # Use a wrapper so matrix stays at natural size (top-left) while the
        # scroll area fills remaining space with the stretch below it.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        _wrap = QWidget()
        _wrap_lay = QVBoxLayout(_wrap)
        _wrap_lay.setContentsMargins(0, 0, 0, 0)
        _wrap_lay.setSpacing(0)
        _wrap_lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self._matrix_container = QWidget()
        self._matrix_container.setMinimumWidth(0)
        self._matrix_grid = QGridLayout(self._matrix_container)
        # The matrix is one continuous table.  Set both directions explicitly
        # (rather than relying on QLayout's combined spacing property) and
        # remove any style-provided outer padding from the host widget.
        self._matrix_grid.setHorizontalSpacing(0)
        self._matrix_grid.setVerticalSpacing(0)
        self._matrix_grid.setContentsMargins(0, 0, 0, 0)
        # Keep spare viewport height below the matrix, never between rows.
        # QGridLayout otherwise distributes it across zero-stretch rows on
        # larger screens, making the apparent row gap resolution-dependent.
        self._matrix_grid.setAlignment(Qt.AlignmentFlag.AlignTop |
                                       Qt.AlignmentFlag.AlignLeft)
        self._matrix_container.setStyleSheet("QWidget { margin: 0px; padding: 0px; }")
        self._matrix_container.setSizePolicy(QSizePolicy.Policy.Expanding,
                                             QSizePolicy.Policy.Preferred)

        # A horizontal splitter gives the matrix a visible right-hand drag
        # edge.  The empty pane is intentional: it keeps the matrix anchored
        # left while allowing its complete width (including definitions below)
        # to be widened without resizing the surrounding settings page.
        matrix_splitter = QSplitter(Qt.Orientation.Horizontal)
        matrix_splitter.setChildrenCollapsible(False)
        matrix_splitter.addWidget(self._matrix_container)
        matrix_spacer = QWidget()
        matrix_spacer.setMinimumWidth(8)
        matrix_splitter.addWidget(matrix_spacer)
        matrix_splitter.setStretchFactor(0, 0)
        matrix_splitter.setStretchFactor(1, 1)
        matrix_splitter.splitterMoved.connect(self._on_matrix_splitter_moved)
        self._matrix_splitter = matrix_splitter
        _wrap_lay.addWidget(matrix_splitter)
        scroll.setWidget(_wrap)
        ml.addWidget(scroll)

        # Axis orientation + direction controls
        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axlar:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItem("Frekvens → X,  Konsekvens → Y  (standard)", 'frequency')
        self._axis_combo.addItem("Konsekvens → X,  Frekvens → Y", 'consequence')
        ax_row.addWidget(self._axis_combo, 1)
        # Clickable arrows instead of checkboxes (2026-08-17 user request) —
        # QToolButton in checkable mode is a drop-in for QCheckBox here:
        # every other call site only ever touches .isChecked()/.setChecked()/
        # .toggled, which QAbstractButton gives both classes identically, so
        # nothing downstream (_apply_size, _load_matrix_ui, _build_matrix_grid,
        # _save_matrix) needed to change.
        self._x_rev_chk = QToolButton()
        self._x_rev_chk.setCheckable(True)
        self._x_rev_chk.setAutoRaise(True)
        self._y_rev_chk = QToolButton()
        self._y_rev_chk.setCheckable(True)
        self._y_rev_chk.setAutoRaise(True)

        def _update_x_arrow(checked):
            self._x_rev_chk.setText("X ←" if checked else "X →")
            self._x_rev_chk.setToolTip(
                "X-axeln vänd: högt värde till vänster" if checked
                else "X-axeln normal: klicka för att vända (högt värde till vänster)")

        def _update_y_arrow(checked):
            self._y_rev_chk.setText("Y ↑" if checked else "Y ↓")
            self._y_rev_chk.setToolTip(
                "Y-axeln vänd: högst upp" if checked
                else "Y-axeln normal: klicka för att vända (högst upp)")

        self._x_rev_chk.toggled.connect(_update_x_arrow)
        self._y_rev_chk.toggled.connect(_update_y_arrow)
        _update_x_arrow(False)
        _update_y_arrow(False)
        # Put direction controls in a shell around the matrix: X spans the
        # matrix width above it, Y spans its height to the left.
        self._matrix_axis_shell = QWidget()
        shell_grid = QGridLayout(self._matrix_axis_shell)
        shell_grid.setContentsMargins(0, 0, 0, 0)
        shell_grid.setSpacing(0)
        self._x_rev_chk.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Fixed)
        self._y_rev_chk.setSizePolicy(QSizePolicy.Policy.Fixed,
                                      QSizePolicy.Policy.Expanding)
        self._y_rev_chk.setMinimumWidth(30)
        shell_grid.addWidget(self._x_rev_chk, 0, 1)
        shell_grid.addWidget(self._y_rev_chk, 1, 0)
        shell_grid.addWidget(self._matrix_container, 1, 1)
        shell_grid.setAlignment(self._x_rev_chk,
                                Qt.AlignmentFlag.AlignLeft |
                                Qt.AlignmentFlag.AlignBottom)
        matrix_splitter.replaceWidget(0, self._matrix_axis_shell)
        ml.addLayout(ax_row)

        # Live update: rebuild grid immediately on any control change
        self._axis_combo.currentIndexChanged.connect(self._apply_size)
        self._x_rev_chk.toggled.connect(self._apply_size)
        self._y_rev_chk.toggled.connect(self._apply_size)
        self._rows_spin.valueChanged.connect(self._apply_size)
        self._cols_spin.valueChanged.connect(self._apply_size)
        self._matrix_cell_width_spin.valueChanged.connect(
            self._on_matrix_cell_width_changed)

        # Standard matrix/frequency presets
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Standardmallar:"))
        st1_btn = QPushButton("ST1 Sverige AB")
        st1_btn.setToolTip(
            "Läser in ST1:s 6×5-riskmatris med sannolikhet A–E.\n"
            "Om studien redan har bedömningar granskas och migreras de först; "
            "annars laddas mallen som en arbetskopia.")
        st1_btn.clicked.connect(lambda: self._request_matrix_template(
            ST1_RISK_MATRIX_PRESET, "ST1 Sverige AB"))
        norsok_btn = QPushButton("NORSOK Z-013  (AAA – E)")
        norsok_btn.setToolTip(
            "Fyll frekvensaxeln med NORSOK Z-013-etiketter:\n"
            "AAA (< 10⁻⁵/år)  →  E (> 1/år)\n"
            "Gränsvärden sätts automatiskt och befintliga bedömningar granskas före migrering.")
        norsok_btn.clicked.connect(lambda: self._request_frequency_template(
            ['< 10⁻⁵/år', '10⁻⁵–10⁻⁴/år', '10⁻⁴–10⁻³/år',
             '10⁻³–10⁻²/år', '10⁻²–10⁻¹/år', '10⁻¹–1/år', '> 1/år'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0], "NORSOK Z-013",
            ['AAA', 'AA', 'A', 'B', 'C', 'D', 'E']))
        fscale_btn = QPushButton("F-skala  (F-1 – F5)")
        fscale_btn.setToolTip(
            "Fyll frekvensaxeln med internt F-skaleetiketter:\n"
            "F-1 (Otänkbar)  →  F5 (Frekvent > 1/år)\n"
            "Gränsvärden sätts automatiskt och befintliga bedömningar granskas före migrering.")
        fscale_btn.clicked.connect(lambda: self._request_frequency_template(
            ['Otänkbar', 'Extremt sällan', 'Sällan', 'Osannolik',
             'Möjlig', 'Trolig', 'Frekvent'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0], "F-skala",
            ['F-1', 'F0', 'F1', 'F2', 'F3', 'F4', 'F5']))
        preset_row.addWidget(st1_btn)
        preset_row.addWidget(norsok_btn)
        preset_row.addWidget(fscale_btn)
        preset_row.addStretch()
        ml.addLayout(preset_row)

        self._custom_matrix_templates_widget = QWidget()
        self._custom_matrix_templates_layout = QHBoxLayout(self._custom_matrix_templates_widget)
        self._custom_matrix_templates_layout.setContentsMargins(0, 0, 0, 0)
        self._custom_matrix_templates_layout.setSpacing(5)
        ml.addWidget(self._custom_matrix_templates_widget)
        self._reload_custom_matrix_templates()

        risk_tab = QWidget()
        risk_lay = QVBoxLayout(risk_tab)
        risk_lay.setContentsMargins(0, 0, 0, 0)
        risk_lay.setSpacing(4)
        subnav = QHBoxLayout()
        subnav.setContentsMargins(8, 6, 8, 0)
        self._risk_matrix_btn = QPushButton("Riskmatris")
        self._risk_axes_btn = QPushButton("Axlar")
        for btn in (self._risk_matrix_btn, self._risk_axes_btn):
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            subnav.addWidget(btn)
        subnav.addStretch()
        risk_lay.addLayout(subnav)
        self._risk_substack = QStackedWidget()
        self._risk_substack.addWidget(matrix_editor)
        self._axes_page = self._create_axes_page()
        self._risk_substack.addWidget(self._axes_page)
        risk_lay.addWidget(self._risk_substack)
        save_row = QHBoxLayout()
        save_row.setContentsMargins(8, 0, 8, 6)
        save_row.addStretch()
        self._save_matrix_btn = QPushButton("Spara ändringar som mall…")
        self._save_matrix_btn.setIcon(_icon('save', 16, '#ffffff'))
        self._save_matrix_btn.setStyleSheet(
            "background:#2F5FD0; color:#fff; font-weight:bold; padding:4px 12px;")
        self._save_matrix_btn.clicked.connect(self._save_matrix)
        save_row.addWidget(self._save_matrix_btn)
        risk_lay.addLayout(save_row)
        self._risk_matrix_btn.clicked.connect(
            lambda: self._set_risk_subview(0))
        self._risk_axes_btn.clicked.connect(
            lambda: self._set_risk_subview(1))
        self._set_risk_subview(0)

        # ── Tab: Kategorier ───────────────────────────────────────────────────
        cat_tab = QWidget()
        cl = QVBoxLayout(cat_tab)
        cl.addWidget(QLabel("Konsekvensskategorier:"))
        self._cat_list = QListWidget()
        cl.addWidget(self._cat_list)
        cat_btns = QHBoxLayout()
        btn_add  = QPushButton("+ Lägg till")
        btn_ren  = QPushButton("Byt namn")
        btn_del  = QPushButton("Ta bort")
        btn_up   = QPushButton("▲")
        btn_down = QPushButton("▼")
        btn_up.setToolTip("Flytta vald kategori uppåt")
        btn_down.setToolTip("Flytta vald kategori nedåt")
        btn_up.setFixedWidth(CONFIG['W_ICON_BTN'])
        btn_down.setFixedWidth(CONFIG['W_ICON_BTN'])
        btn_add.clicked.connect(self._cat_add)
        btn_ren.clicked.connect(self._cat_rename)
        btn_del.clicked.connect(self._cat_delete)
        btn_up.clicked.connect(lambda: self._cat_move(-1))
        btn_down.clicked.connect(lambda: self._cat_move(1))
        for b in [btn_add, btn_ren, btn_del, btn_up, btn_down]: cat_btns.addWidget(b)
        cl.addLayout(cat_btns)
        cl.addStretch()

        # ── Merged tab: Riskmatris & Kategorier ─────────────────────────────
        # Design choice (2026-08-11, user request: "'riskmatris' och
        # 'kategorier' borde gå att slå ihop till en sida" / "Låt Claude
        # välja bästa GUI-lösningen"): a QSplitter, categories on the left
        # and the matrix on the right, rather than a nested tab-within-tab.
        # Reasoning: the matrix tab is inherently tall/wide (size controls +
        # colour palette + a scrollable grid + axis controls + frequency
        # presets + a save button), while the categories tab is just a short
        # list with three buttons — putting categories in their own nested
        # tab would hide them behind an extra click AND waste most of that
        # tab's vertical space. Categories also feed the matrix conceptually
        # (they're consequence-axis metadata), so keeping both visible
        # side-by-side, with the narrow categories panel user-resizable via
        # the splitter handle, reads as one coherent risk-classification
        # screen instead of two unrelated hidden pages.
        combined_tab = QWidget()
        combined_l = QHBoxLayout(combined_tab)
        combined_l.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        # Matrix first, categories second: widening the matrix is now a
        # deliberate left-pane operation and never silently stretches the
        # category definitions or makes matrix columns unequal.
        splitter.addWidget(risk_tab)
        splitter.addWidget(cat_tab)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([760, 220])
        combined_l.addWidget(splitter)
        tabs.addTab(combined_tab, "Riskmatris")

        # ── Tab: Standardorsaker ─────────────────────────────────────────────
        self._std_causes_panel = StandardCausesSettingsPanel(self.db)
        tabs.addTab(self._std_causes_panel, "Avvikelser & Orsaker")

        # ── Tab: Blad (moved from Studiehantering → PID-hantering, 2026-08-17,
        # see NOTES.md) ───────────────────────────────────────────────────────
        sheets_widget = QWidget()
        sheets_layout = QVBoxLayout(sheets_widget)
        sheets_layout.setContentsMargins(8, 8, 8, 8)
        sheets_layout.setSpacing(6)

        sheet_hdr = QHBoxLayout()
        sheet_hdr.addWidget(QLabel("Bladordning — dra för att ändra ordning:"))
        sheet_hdr.addStretch()
        rename_btn = QPushButton("Byt namn")
        rename_btn.setIcon(_icon('edit'))
        rename_btn.clicked.connect(self._rename_sheet)
        sheet_hdr.addWidget(rename_btn)
        delete_btn = QPushButton("Ta bort")
        delete_btn.setIcon(_icon('delete'))
        delete_btn.clicked.connect(self._delete_sheets)
        sheet_hdr.addWidget(delete_btn)
        sheets_layout.addLayout(sheet_hdr)

        rev_row = QHBoxLayout()
        rev_row.addWidget(QLabel("P&ID-revision för valt blad:"))
        self._sheet_rev_combo = QComboBox()
        self._sheet_rev_combo.currentIndexChanged.connect(self._on_sheet_revision_changed)
        rev_row.addWidget(self._sheet_rev_combo, 1)
        sheets_layout.addLayout(rev_row)

        self._sheet_list = QTableWidget(0, 5)
        self._sheet_list.setHorizontalHeaderLabels(["Ritningsnummer", "Ritningsnamn", "Revision", "Datum", "PDF-sida"])
        self._sheet_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._sheet_list.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._sheet_list.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed | QTableWidget.EditTrigger.SelectedClicked)
        self._sheet_list.setDragDropMode(QTableWidget.DragDropMode.InternalMove)
        self._sheet_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sheet_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._sheet_list.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._sheet_list.itemChanged.connect(self._on_sheet_item_changed)
        self._sheet_list.model().rowsMoved.connect(self._on_sheets_reordered)
        self._sheet_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sheet_list.customContextMenuRequested.connect(self._sheet_context_menu)
        self._sheet_list.currentCellChanged.connect(self._on_sheet_selection_changed)
        _base_kp = self._sheet_list.keyPressEvent
        def _sheet_key_press(event, _base=_base_kp):
            if event.key() == Qt.Key.Key_Delete:
                self._delete_sheets()
            else:
                _base(event)
        self._sheet_list.keyPressEvent = _sheet_key_press
        sheets_layout.addWidget(self._sheet_list)
        tabs.addTab(sheets_widget, "Blad")

        # ── Tab: Noder ────────────────────────────────────────────────────────
        # Mirrors the HAZOP tree's node list both ways: renaming/creating a
        # node here refreshes the tree via structure_changed, and any tree
        # change that calls this panel's refresh_nodes() shows up here
        # (2026-08-17, see NOTES.md "Ny Noder-flik").
        nodes_widget = QWidget()
        nodes_layout = QVBoxLayout(nodes_widget)
        nodes_layout.setContentsMargins(8, 8, 8, 8)
        nodes_layout.setSpacing(6)
        nodes_hdr = QHBoxLayout()
        nodes_hdr.addWidget(QLabel("Alla noder:"))
        nodes_hdr.addStretch()
        add_node_btn = QPushButton("+ Ny nod")
        add_node_btn.clicked.connect(self._add_node_from_noder_tab)
        nodes_hdr.addWidget(add_node_btn)
        nodes_layout.addLayout(nodes_hdr)
        self._nodes_table = QTableWidget(0, 7)
        self._nodes_table.setHorizontalHeaderLabels([
            "Nod nummer", "Namn", "Blad", "Objekt per blad", "Objekttyp",
            "Avvikelser per objekt", "Antal"])
        self._nodes_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.setWordWrap(True)
        self._nodes_table.verticalHeader().setVisible(False)
        self._nodes_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._nodes_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._nodes_table.cellDoubleClicked.connect(self._on_nodes_table_double_clicked)
        nodes_layout.addWidget(self._nodes_table)
        tabs.addTab(nodes_widget, "Noder")

        # Keep ToR and Report as the rightmost preparation tab.
        tabs.removeTab(tabs.indexOf(tor_report_tab))
        tabs.addTab(tor_report_tab, "ToR and Report")

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_palette_ui()
        self._load_categories()
        self._reload_axes_tables()
        self._proj_name.setText(self.db.get_config('project_name', ''))
        self._proj_number.setText(self.db.get_config('project_number', ''))
        self._proj_client.setText(self.db.get_config('project_client', ''))
        self._proj_facility.setText(self.db.get_config('project_facility', ''))

        today = QDate.currentDate()
        start_str = self.db.get_config('project_date_start', '')
        end_str   = self.db.get_config('project_date_end', '')
        start_d = QDate.fromString(start_str, 'yyyy-MM-dd') if start_str else QDate()
        end_d   = QDate.fromString(end_str, 'yyyy-MM-dd') if end_str else QDate()
        self._proj_date_start.setDate(start_d if start_d.isValid() else today)
        self._proj_date_end.setDate(end_d if end_d.isValid() else today)

        self._load_project_revisions()
        self._load_project_custom_fields()
        self._load_tor_report_fields()
        self.refresh_sheets()
        self.refresh_nodes()

    def _tor_report_add_section(self, outer, section, title):
        """Build one compact sign-off form and wire each value to app_config."""
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(7)
        for key, label in (
                ("prepared", "Framtagen av:"),
                ("reviewed", "Kvalitetsgranskad av:"),
                ("approved", "Godkänd av:")):
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setPlaceholderText("Välj deltagare eller skriv namn")
            combo.setMinimumWidth(280)
            combo.setStyleSheet(
                "QComboBox{padding:3px 6px;border:1px solid #CFD1CE;"
                "border-radius:0px;background:#FFFFFF;}"
                "QComboBox:focus{border:2px solid #2F6FED;padding:2px 5px;}")
            cfg_key = f"{section}_{key}_by"
            combo.currentTextChanged.connect(
                lambda text, k=cfg_key: self.db.set_config(k, text.strip()))
            combo.lineEdit().editingFinished.connect(
                lambda k=cfg_key, cb=combo: self.db.set_config(k, cb.currentText().strip()))
            self._tor_report_fields[(section, key)] = combo
            form.addRow(label, combo)
        outer.addWidget(box)

    def _participant_display_names(self):
        names = []
        for p in self.db.list_participants():
            first = (p['first_name'] or '').strip()
            last = (p['last_name'] or '').strip()
            name = ' '.join(part for part in (first, last) if part)
            if name and name not in names:
                names.append(name)
        return names

    def _load_tor_report_fields(self):
        names = self._participant_display_names()
        for (section, key), combo in self._tor_report_fields.items():
            value = self.db.get_config(f"{section}_{key}_by", '') or ''
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentText(value)
            combo.blockSignals(False)

    def _on_prep_tab_changed(self, index):
        """Refresh participant suggestions when the new tab is opened."""
        if self._tabs.tabText(index) == "ToR and Report":
            self._load_tor_report_fields()

    # ── Blad (2026-08-17, moved from PIDManagementPanel, see NOTES.md) ──────
    def refresh_sheets(self):
        self._sheet_rev_combo.blockSignals(True)
        self._sheet_rev_combo.clear()
        self._sheet_rev_combo.addItem("(ingen)", None)
        for rev in self.db.get_revisions():
            self._sheet_rev_combo.addItem(rev['revision'] or f"Revision {rev['id']}", rev['id'])
        self._sheet_rev_combo.blockSignals(False)

        self._sheet_list.blockSignals(True)
        sheets = self.db.get_sheets()
        self._sheet_list.setRowCount(len(sheets))
        for row, sheet in enumerate(sheets):
            values = [sheet['drawing_number'] or '', sheet['drawing_name'] or sheet['sheet_name'] or '',
                      sheet['drawing_revision'] or '', sheet['drawing_date'] or '', str(sheet['physical_page'] + 1)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, sheet['id'])
                item.setData(Qt.ItemDataRole.UserRole + 1, sheet['revision_id'])
                if col == 4:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._sheet_list.setItem(row, col, item)
            nodes = self.db.nodes_on_page(sheet['physical_page'])
            if nodes:
                names = ', '.join(n['name'] or f"Nod {n['id']}" for n in nodes)
                item.setToolTip(f"Noder på detta blad: {names}")
        self._sheet_list.blockSignals(False)

    def _on_sheets_reordered(self, *_):
        ids = [self._sheet_list.item(i, 0).data(Qt.ItemDataRole.UserRole)
               for i in range(self._sheet_list.rowCount())]
        self.db.reorder_sheets(ids)
        self.refresh_sheets()

    def _rename_sheet(self):
        row = self._sheet_list.currentRow()
        item = self._sheet_list.item(row, 1) if row >= 0 else None
        if item is None:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        current_name = ''
        for s in self.db.get_sheets():
            if s['id'] == sheet_id:
                current_name = s['sheet_name']
                break
        name, ok = QInputDialog.getText(self, "Byt namn", "Bladnamn:", text=current_name)
        if ok and name.strip():
            self.db.update_sheet_name(sheet_id, name.strip())
            self.refresh_sheets()

    def _delete_sheets(self):
        selected = [self._sheet_list.item(row, 0)
                    for row in sorted({idx.row() for idx in self._sheet_list.selectionModel().selectedRows()})]
        if not selected:
            return
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in selected]

        all_sheets = {s['id']: s for s in self.db.get_sheets()}
        pages_info = [(ids[i], all_sheets[ids[i]]['physical_page'],
                       all_sheets[ids[i]]['sheet_name'])
                      for i in range(len(ids)) if ids[i] in all_sheets]
        physical_pages = [p for _, p, _ in pages_info]

        objects = self.db.objects_on_pages(physical_pages)
        affected_lines = []
        for sheet_id, phys, name in pages_info:
            obj = objects.get(phys, {})
            parts = []
            if obj.get('markups'):
                parts.append(f"{obj['markups']} nodmarkering{'ar' if obj['markups'] != 1 else ''}")
            if obj.get('causes'):
                parts.append(f"{obj['causes']} orsak{'er' if obj['causes'] != 1 else ''}")
            if obj.get('consequences'):
                parts.append(f"{obj['consequences']} konsekvens{'er' if obj['consequences'] != 1 else ''}")
            if obj.get('safeguards'):
                parts.append(f"{obj['safeguards']} safeguard{'s' if obj['safeguards'] != 1 else ''}")
            if parts:
                affected_lines.append(f"• {name}: {', '.join(parts)}")

        if affected_lines:
            detail = "\n".join(affected_lines)
            box = QMessageBox(self)
            box.setWindowTitle("Ta bort blad")
            box.setIcon(QMessageBox.Icon.Warning)
            count = len(selected)
            box.setText(
                f"{'Dessa blad innehåller' if count > 1 else 'Detta blad innehåller'} "
                f"HAZOP-objekt som kommer tas bort:")
            box.setInformativeText(detail)
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("Ta bort ändå")
            box.button(QMessageBox.StandardButton.No).setText("Avbryt")
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        else:
            count = len(selected)
            msg = (f"Ta bort {count} blad?" if count > 1
                   else f"Ta bort '{all_sheets[ids[0]]['sheet_name']}'?")
            ans = QMessageBox.question(self, "Ta bort blad", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return

        self.db.delete_objects_on_pages(physical_pages)
        self.db.delete_sheets(ids)
        self.refresh_sheets()
        self.sheets_changed.emit()

    def _sheet_context_menu(self, pos):
        selected_rows = self._sheet_list.selectionModel().selectedRows()
        if not selected_rows:
            return
        menu = QMenu(self)
        if len(selected_rows) == 1:
            menu.addAction(_icon('edit'), "Byt namn", self._rename_sheet)
            menu.addAction(_icon('document'), "Lägg till ny revision av P&ID…",
                           self._add_pid_revision)
        menu.addAction(_icon('delete'), "Ta bort", self._delete_sheets)
        menu.exec(self._sheet_list.viewport().mapToGlobal(pos))

    def _add_pid_revision(self):
        """Create a revision record from the currently loaded P&ID file."""
        current = self._sheet_list.currentRow()
        if current < 0:
            return
        revision, ok = QInputDialog.getText(
            self, "Ny P&ID-revision", "Revision (t.ex. Rev F):")
        if not ok or not revision.strip():
            return
        date, ok = QInputDialog.getText(
            self, "Ny P&ID-revision", "Datum (ÅÅÅÅ-MM-DD):",
            text=QDate.currentDate().toString("yyyy-MM-dd"))
        if not ok:
            return
        pdf_path = self.db.get_pid_path() or ''
        self.db.add_revision(revision.strip(), '', pdf_path, date.strip())
        self.refresh_sheets()
        self.sheets_changed.emit()

    def _on_sheet_item_changed(self, item):
        """Persist editable drawing metadata immediately."""
        if item.column() >= 4:
            return
        row = item.row()
        id_item = self._sheet_list.item(row, 0)
        if id_item is None:
            return
        sheet_id = id_item.data(Qt.ItemDataRole.UserRole)
        values = [self._sheet_list.item(row, col).text().strip()
                  if self._sheet_list.item(row, col) else '' for col in range(4)]
        self.db.update_sheet_metadata(sheet_id, *values)

    def _on_sheet_selection_changed(self, current_row, current_col, previous_row, previous_col):
        current = self._sheet_list.item(current_row, 0) if current_row >= 0 else None
        self._sheet_rev_combo.blockSignals(True)
        if current is None:
            self._sheet_rev_combo.setCurrentIndex(0)
        else:
            rev_id = current.data(Qt.ItemDataRole.UserRole + 1)
            idx = self._sheet_rev_combo.findData(rev_id)
            self._sheet_rev_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sheet_rev_combo.blockSignals(False)

    def _on_sheet_revision_changed(self, _index):
        row = self._sheet_list.currentRow()
        item = self._sheet_list.item(row, 0) if row >= 0 else None
        if item is None:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        rev_id = self._sheet_rev_combo.currentData()
        self.db.set_sheet_revision(sheet_id, rev_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, rev_id)

    # ── Noder (2026-08-17, see NOTES.md "Ny Noder-flik") ─────────────────────
    def refresh_nodes(self):
        sheets_by_page = {s['physical_page']: (s['drawing_name'] or s['sheet_name'] or
                                               f"PDF-sida {s['physical_page'] + 1}")
                          for s in self.db.get_sheets()}
        nodes = self.db.nodes()
        self._nodes_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            self._nodes_table.setItem(row, 0, QTableWidgetItem(f"Nod {row + 1}"))
            name_item = QTableWidgetItem(node['name'] or f"Nod {node['id']}")
            name_item.setData(Qt.ItemDataRole.UserRole, node['id'])
            self._nodes_table.setItem(row, 1, name_item)
            pages = self.db.analysis_pages_for_node(node['id'])
            sheet_names = [sheets_by_page.get(p, f"sida {p + 1}") for p in pages]
            objects_by_page = self.db.analysis_objects_for_node(node['id'])
            details_by_page = self.db.analysis_object_details_for_node(node['id'])
            object_lines = [', '.join(objects_by_page.get(p, [])) or '—' for p in pages]
            detail_lines = [details_by_page.get(p, []) for p in pages]
            self._nodes_table.setItem(row, 2, QTableWidgetItem('\n'.join(sheet_names)))
            self._nodes_table.setItem(row, 3, QTableWidgetItem('\n'.join(object_lines)))
            self._nodes_table.setItem(row, 4, QTableWidgetItem('\n'.join(
                '\n'.join(obj['type'] or '—' for obj in objs) or '—' for objs in detail_lines)))
            self._nodes_table.setItem(row, 5, QTableWidgetItem('\n'.join(
                '\n'.join(', '.join(obj['deviations']) or '—' for obj in objs) or '—'
                for objs in detail_lines)))
            self._nodes_table.setItem(row, 6, QTableWidgetItem('\n'.join(
                '\n'.join(str(obj['count']) for obj in objs) or '0' for objs in detail_lines)))
            self._nodes_table.resizeRowToContents(row)

    def _add_node_from_noder_tab(self):
        self.db.add_node()
        self.refresh_nodes()
        self.structure_changed.emit()

    def _on_nodes_table_double_clicked(self, row, col):
        if col != 1:
            return
        item = self._nodes_table.item(row, 1)
        if item is None:
            return
        node_id = item.data(Qt.ItemDataRole.UserRole)
        node = self.db.get_node(node_id)
        if not node:
            return
        name, ok = QInputDialog.getText(self, "Döp om nod", "Nytt namn:",
                                         text=node['name'] or '')
        if not ok or not name.strip():
            return
        self.db.update_node(node_id, name.strip(), node.get('description') or '',
                             node.get('pid_ref') or '', node.get('media') or '',
                             node.get('pressure') or '', node.get('temperature') or '')
        self.refresh_nodes()
        self.structure_changed.emit()

    def _next_revision_letter(self):
        n = len(self.db.project_revisions())
        letters = ''
        n1 = n
        while True:
            letters = chr(65 + n1 % 26) + letters
            n1 = n1 // 26 - 1
            if n1 < 0:
                break
        return letters

    def _load_project_revisions(self):
        self._proj_rev_table.blockSignals(True)
        rows = self.db.project_revisions()
        self._proj_rev_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            item_label = QTableWidgetItem(row['label'])
            item_label.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._proj_rev_table.setItem(r, 0, item_label)
            date_edit = QDateEdit()
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            d = QDate.fromString(row['date'], 'yyyy-MM-dd')
            date_edit.setDate(d if d.isValid() else QDate.currentDate())
            date_edit.dateChanged.connect(
                lambda d, id_=row['id']: self.db.update_project_revision(
                    id_, date=d.toString('yyyy-MM-dd')))
            self._proj_rev_table.setCellWidget(r, 1, date_edit)
            item_desc = QTableWidgetItem(row['description'])
            item_desc.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._proj_rev_table.setItem(r, 2, item_desc)
        self._proj_rev_table.blockSignals(False)

    def _add_project_revision_row(self):
        label = self._next_revision_letter()
        self.db.add_project_revision(label, QDate.currentDate().toString('yyyy-MM-dd'), '')
        self._load_project_revisions()

    def _on_proj_rev_item_changed(self, item):
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        if item.column() == 0:
            self.db.update_project_revision(id_, label=item.text())
        elif item.column() == 2:
            self.db.update_project_revision(id_, description=item.text())

    def _proj_rev_context_menu(self, pos):
        item = self._proj_rev_table.itemAt(pos)
        if item is None:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        del_action = menu.addAction("Ta bort rad")
        action = menu.exec(self._proj_rev_table.viewport().mapToGlobal(pos))
        if action == del_action and id_ is not None:
            self.db.delete_project_revision(id_)
            self._load_project_revisions()

    def _load_project_custom_fields(self):
        for name_edit, value_edit, row_w in self._proj_field_rows.values():
            self._proj_fields_lay.removeWidget(row_w)
            row_w.deleteLater()
        self._proj_field_rows = {}
        for field in self.db.project_custom_fields():
            self._add_project_custom_field_widget(field['id'], field['name'], field['value'])

    def _add_project_custom_field_widget(self, id_, name, value):
        row_w = QWidget()
        row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(0, 0, 0, 0)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("Fältnamn")
        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText("Värde")
        del_btn = QPushButton("✕")
        del_btn.setFixedWidth(28)
        name_edit.editingFinished.connect(
            lambda: self.db.update_project_custom_field(id_, name=name_edit.text()))
        value_edit.editingFinished.connect(
            lambda: self.db.update_project_custom_field(id_, value=value_edit.text()))
        del_btn.clicked.connect(lambda: self._delete_project_custom_field(id_))
        row_l.addWidget(name_edit)
        row_l.addWidget(value_edit)
        row_l.addWidget(del_btn)
        self._proj_fields_lay.insertWidget(self._proj_fields_lay.count() - 1, row_w)
        self._proj_field_rows[id_] = (name_edit, value_edit, row_w)

    def _add_project_custom_field_row(self):
        id_ = self.db.add_project_custom_field('', '')
        self._add_project_custom_field_widget(id_, '', '')

    def _delete_project_custom_field(self, id_):
        self.db.delete_project_custom_field(id_)
        name_edit, value_edit, row_w = self._proj_field_rows.pop(id_)
        self._proj_fields_lay.removeWidget(row_w)
        row_w.deleteLater()

    # ── Palette ───────────────────────────────────────────────────────────────

    def _load_palette_ui(self):
        # Remove existing swatches (keep the 3 buttons at end)
        for sw in self._palette_swatches:
            self._palette_container.removeWidget(sw)
            sw.deleteLater()
        self._palette_swatches = []
        palette = self.db.get_color_palette()
        for entry in palette:
            sw = DraggableColorSwatch(entry['name'], entry['color'], entry.get('fg_color'))
            # Insert before the "Lägg till / Redigera / Ta bort" buttons
            insert_pos = self._palette_container.count() - 4
            self._palette_container.insertWidget(max(0, insert_pos), sw)
            self._palette_swatches.append(sw)

    def _palette_add(self):
        name, ok = QInputDialog.getText(self, "Ny palettefärg", "Namn (t.ex. Kritisk):")
        if not ok or not name.strip():
            return
        color = QColorDialog.getColor(QColor('#e74c3c'), self, "Välj bakgrundsfärg")
        if not color.isValid():
            return
        # Auto-calculate fg and let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        fg_color_obj = QColorDialog.getColor(QColor(auto_fg), self, "Välj textfärg (auto-föreslagen)")
        fg = fg_color_obj.name() if fg_color_obj.isValid() else auto_fg
        palette = self.db.get_color_palette()
        palette.append({'name': name.strip(), 'color': color.name(), 'fg_color': fg})
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_edit(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Redigera", "Välj färg:", names, 0, False)
        if not ok:
            return
        idx = names.index(chosen)
        new_name, ok2 = QInputDialog.getText(self, "Nytt namn", "Namn:", text=chosen)
        if not ok2:
            return
        new_color = QColorDialog.getColor(QColor(palette[idx]['color']), self, "Välj färg")
        if not new_color.isValid():
            return
        # Ask for text color too
        old_fg = palette[idx].get('fg_color', '#ffffff')
        fg_color_obj = QColorDialog.getColor(QColor(old_fg), self, "Välj textfärg")
        new_fg = fg_color_obj.name() if fg_color_obj.isValid() else old_fg
        palette[idx] = {'name': new_name.strip() or chosen, 'color': new_color.name(), 'fg_color': new_fg}
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_delete(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Ta bort", "Välj färg att ta bort:", names, 0, False)
        if not ok:
            return
        palette = [e for e in palette if e['name'] != chosen]
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    # ── Matrix ────────────────────────────────────────────────────────────────

    def _set_risk_subview(self, index):
        """Select the local Riskmatris/Axlar view without touching data."""
        if not hasattr(self, '_risk_substack'):
            return
        index = 1 if int(index) == 1 else 0
        if index == 1 and self._risk_substack.currentIndex() != 1:
            # Bring unsaved matrix header/cell edits into the same working
            # configuration before showing the larger, spreadsheet-style
            # axis editor.  This is presentation only; Save remains explicit.
            self._apply_size()
            self._reload_axes_tables()
        self._risk_substack.setCurrentIndex(index)
        self._risk_matrix_btn.setChecked(index == 0)
        self._risk_axes_btn.setChecked(index == 1)

    def _create_axes_page(self):
        """Build the large, paste-friendly editor for axes and categories."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(7)

        intro = QLabel(
            "Redigera axlar och konsekvensbeskrivningar här. Markera en eller "
            "flera beskrivningsceller och klistra in från Excel med Ctrl+V.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#444; font-size:11px;")
        layout.addWidget(intro)

        axis_split = QSplitter(Qt.Orientation.Horizontal)
        freq_box = QGroupBox("Frekvensaxel")
        freq_lay = QVBoxLayout(freq_box)
        self._frequency_axis_table = QTableWidget(0, 3)
        self._frequency_axis_table.setHorizontalHeaderLabels(
            ["Tecken", "Beskrivning", "Övre gräns (/år)"])
        self._configure_axes_table(self._frequency_axis_table)
        self._frequency_axis_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._frequency_axis_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._frequency_axis_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self._frequency_axis_table.itemChanged.connect(self._on_axes_item_changed)
        freq_lay.addWidget(self._frequency_axis_table)
        axis_split.addWidget(freq_box)

        cons_box = QGroupBox("Konsekvensaxel")
        cons_lay = QVBoxLayout(cons_box)
        self._consequence_axis_table = QTableWidget(0, 2)
        self._consequence_axis_table.setHorizontalHeaderLabels(["Tecken", "Beskrivning"])
        self._configure_axes_table(self._consequence_axis_table)
        self._consequence_axis_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._consequence_axis_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._consequence_axis_table.itemChanged.connect(self._on_axes_item_changed)
        cons_lay.addWidget(self._consequence_axis_table)
        axis_split.addWidget(cons_box)
        axis_split.setSizes([430, 360])
        layout.addWidget(axis_split, 0)

        definitions_box = QGroupBox("Beskrivningar per konsekvenskategori")
        definitions_lay = QVBoxLayout(definitions_box)
        definition_hint = QLabel(
            "En rad per konsekvensnivå. Klistra in en kolumn med exempelvis "
            "fem Excel-celler direkt i den markerade kategorin.")
        definition_hint.setWordWrap(True)
        definition_hint.setStyleSheet("color:#555; font-size:10px;")
        definitions_lay.addWidget(definition_hint)
        self._category_definition_table = QTableWidget(0, 0)
        self._configure_axes_table(self._category_definition_table)
        self._category_definition_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._category_definition_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems)
        self._category_definition_table.setWordWrap(True)
        self._category_definition_table.installEventFilter(self)
        self._category_definition_table.itemChanged.connect(self._on_axes_item_changed)
        definitions_lay.addWidget(self._category_definition_table)
        layout.addWidget(definitions_box, 1)

        actions = QHBoxLayout()
        generate = QPushButton("Generera frekvensetiketter från gränser")
        generate.setToolTip("Skapar etiketter som < 0,1/år och 0,1–1/år."
                            " Körs bara när du väljer knappen.")
        generate.clicked.connect(self._generate_frequency_labels)
        actions.addWidget(generate)
        actions.addStretch()
        layout.addLayout(actions)
        return page

    @staticmethod
    def _configure_axes_table(table):
        table.setAlternatingRowColors(True)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.EditKeyPressed |
            QAbstractItemView.EditTrigger.SelectedClicked)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)

    @staticmethod
    def _axes_item(text, editable=True):
        item = QTableWidgetItem(str(text))
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _on_axes_item_changed(self, _item):
        if self._axes_loading:
            return
        self._axes_dirty = True
        QTimer.singleShot(0, self._resize_axes_category_rows)

    def _reload_axes_tables(self, cfg=None):
        """Reload the Axlar page from its current in-memory working copy."""
        if not hasattr(self, '_frequency_axis_table'):
            return
        cfg = cfg or getattr(self, '_last_built_cfg', None) or \
            self.db.get_risk_matrix() or DEFAULT_MATRIX
        n_freq = int(cfg.get('cols', 7))
        n_cons = int(cfg.get('rows', 5))
        freq_labels = list(cfg.get('x_labels', []))
        cons_labels = list(cfg.get('y_labels', []))
        freq_codes = list(cfg.get('x_codes', []))
        cons_codes = list(cfg.get('y_codes', []))
        boundaries = list(cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES))
        cats = self.db.consequence_categories()
        definitions = self.db.get_severity_definitions()

        self._axes_loading = True
        try:
            self._frequency_axis_table.setRowCount(n_freq)
            for row in range(n_freq):
                label = freq_labels[row] if row < len(freq_labels) else ''
                code = freq_codes[row] if row < len(freq_codes) else f"F{row - 1}"
                bound = '' if row >= n_freq - 1 else (
                    f"{float(boundaries[row]):.8g}" if row < len(boundaries) else '')
                self._frequency_axis_table.setItem(row, 0, self._axes_item(code))
                self._frequency_axis_table.setItem(row, 1, self._axes_item(label))
                self._frequency_axis_table.setItem(
                    row, 2, self._axes_item(bound if row < n_freq - 1 else "—", row < n_freq - 1))

            self._consequence_axis_table.setRowCount(n_cons)
            for row in range(n_cons):
                label = cons_labels[row] if row < len(cons_labels) else ''
                code = cons_codes[row] if row < len(cons_codes) else str(row + 1)
                self._consequence_axis_table.setItem(row, 0, self._axes_item(code))
                self._consequence_axis_table.setItem(row, 1, self._axes_item(label))

            table = self._category_definition_table
            table.setRowCount(n_cons)
            table.setColumnCount(len(cats) + 1)
            table.setHorizontalHeaderLabels(
                ["Konsekvensnivå"] + [cat['name'] for cat in cats])
            for row in range(n_cons):
                code = cons_codes[row] if row < len(cons_codes) else str(row + 1)
                table.setItem(row, 0, self._axes_item(code, False))
                for cat_col, cat in enumerate(cats, start=1):
                    text = definitions.get(row + 1, {}).get(cat['id'], '')
                    item = self._axes_item(text)
                    item.setData(Qt.ItemDataRole.UserRole, cat['id'])
                    table.setItem(row, cat_col, item)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            for col in range(1, table.columnCount()):
                table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
                table.setColumnWidth(col, 250)
        finally:
            self._axes_loading = False
        self._axes_dirty = False
        self._resize_axes_category_rows()

    def _resize_axes_category_rows(self):
        table = getattr(self, '_category_definition_table', None)
        if table is None:
            return
        table.resizeRowsToContents()
        for row in range(table.rowCount()):
            table.setRowHeight(row, max(CONFIG['H_ROW_STD'], table.rowHeight(row)))

    def _selected_category_description_cells(self):
        table = self._category_definition_table
        return sorted(
            (index.row(), index.column()) for index in table.selectedIndexes()
            if index.column() > 0)

    def _copy_category_description_cells(self):
        selected = self._selected_category_description_cells()
        if not selected:
            return False
        min_row, max_row = selected[0][0], selected[-1][0]
        min_col = min(col for _row, col in selected)
        max_col = max(col for _row, col in selected)
        selected_set = set(selected)
        lines = []
        for row in range(min_row, max_row + 1):
            values = []
            for col in range(min_col, max_col + 1):
                item = self._category_definition_table.item(row, col)
                values.append(item.text() if (row, col) in selected_set and item else '')
            lines.append('\t'.join(values))
        QApplication.clipboard().setText('\n'.join(lines))
        return True

    def _paste_category_description_cells(self):
        text = QApplication.clipboard().text()
        if not text:
            return False
        source = [line.split('\t') for line in text.replace('\r\n', '\n').split('\n')]
        if source and source[-1] == ['']:
            source.pop()
        if not source:
            return False
        selected = self._selected_category_description_cells()
        if not selected:
            current = self._category_definition_table.currentIndex()
            if not current.isValid() or current.column() == 0:
                return False
            selected = [(current.row(), current.column())]

        # A matching multi-selection receives values cell-for-cell; otherwise
        # paste starts at the active top-left cell exactly as in Excel.
        flat = [value for row in source for value in row]
        if len(selected) > 1 and len(flat) == len(selected):
            targets = list(zip(selected, flat))
        else:
            start_row = min(row for row, _col in selected)
            start_col = min(col for _row, col in selected)
            targets = []
            for source_row, values in enumerate(source):
                for source_col, value in enumerate(values):
                    targets.append(((start_row + source_row, start_col + source_col), value))

        table = self._category_definition_table
        changed = False
        self._axes_loading = True
        try:
            for (row, col), value in targets:
                if not (0 <= row < table.rowCount() and 0 < col < table.columnCount()):
                    continue
                item = table.item(row, col)
                if item is not None and item.text() != value:
                    item.setText(value)
                    changed = True
        finally:
            self._axes_loading = False
        if changed:
            self._axes_dirty = True
            self._resize_axes_category_rows()
        return changed

    def eventFilter(self, obj, event):
        if obj is getattr(self, '_category_definition_table', None) and \
                event.type() == QEvent.Type.KeyPress and \
                self._category_definition_table.state() != QAbstractItemView.State.EditingState:
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl and event.key() == Qt.Key.Key_C and self._copy_category_description_cells():
                return True
            if ctrl and event.key() == Qt.Key.Key_V and self._paste_category_description_cells():
                return True
        return super().eventFilter(obj, event)

    def _generate_frequency_labels(self):
        table = self._frequency_axis_table
        bounds = []
        for row in range(max(0, table.rowCount() - 1)):
            item = table.item(row, 2)
            try:
                value = float(item.text().strip()) if item else 0
            except ValueError:
                value = 0
            bounds.append(value if value > 0 else None)

        def _format(value):
            return f"{value:.4g}/år" if value and value >= 0.001 else f"{value:.2e}/år"

        self._axes_loading = True
        try:
            for row in range(table.rowCount()):
                left = bounds[row - 1] if row > 0 else None
                right = bounds[row] if row < len(bounds) else None
                if left is None and right is not None:
                    label = f"< {_format(right)}"
                elif left is not None and right is None:
                    label = f"≥ {_format(left)}"
                elif left is not None and right is not None:
                    label = f"{_format(left)} – {_format(right)}"
                else:
                    label = ''
                table.item(row, 1).setText(label)
        finally:
            self._axes_loading = False
        self._axes_dirty = True

    def _save_axes_and_categories_values(self, show_confirmation=True):
        """Persist the Axlar working copy, including all pasted descriptions."""
        cfg = json.loads(json.dumps(
            getattr(self, '_last_built_cfg', None) or self.db.get_risk_matrix() or DEFAULT_MATRIX))
        n_freq, n_cons = self._frequency_axis_table.rowCount(), self._consequence_axis_table.rowCount()
        cfg['cols'], cfg['rows'] = n_freq, n_cons
        cfg['x_codes'] = [self._frequency_axis_table.item(row, 0).text().strip()
                          for row in range(n_freq)]
        cfg['y_codes'] = [self._consequence_axis_table.item(row, 0).text().strip()
                          for row in range(n_cons)]
        cfg['x_labels'] = [self._frequency_axis_table.item(row, 1).text().strip()
                           for row in range(n_freq)]
        cfg['y_labels'] = [self._consequence_axis_table.item(row, 1).text().strip()
                           for row in range(n_cons)]
        boundaries = []
        for row in range(max(0, n_freq - 1)):
            raw = self._frequency_axis_table.item(row, 2).text().strip()
            try:
                value = float(raw)
            except ValueError:
                QMessageBox.warning(self, "Ogiltig gräns",
                                    f"F{row + 1} har en ogiltig övre gräns: {raw or 'tom'}.")
                return
            if value <= 0:
                QMessageBox.warning(self, "Ogiltig gräns",
                                    "Frekvensgränser måste vara större än noll.")
                return
            boundaries.append(value)
        if any(right <= left for left, right in zip(boundaries, boundaries[1:])):
            QMessageBox.warning(self, "Ogiltiga gränser",
                                "Frekvensgränserna måste öka rad för rad.")
            return
        cfg['freq_boundaries'] = boundaries
        pending_categories = getattr(self, '_pending_template_categories', None)
        if pending_categories is not None:
            cfg['consequence_categories'] = json.loads(json.dumps(pending_categories))
        cfg = _normalise_matrix(cfg)
        try:
            if pending_categories is not None:
                self.db.apply_risk_matrix_template_without_assessments(cfg)
                self._pending_template_categories = None
                self._load_categories()
            else:
                self.db.set_risk_matrix(cfg)
        except Exception as exc:
            QMessageBox.critical(self, "Axlar sparades inte", str(exc))
            return

        if pending_categories is not None:
            self._last_built_cfg = cfg
            self._load_matrix_ui()
            self._reload_axes_tables(cfg)
            if show_confirmation:
                QMessageBox.information(self, "Sparat", "Mallens axlar och konsekvenskategorier sparade.")
            self.matrix_changed.emit()
            return

        table = self._category_definition_table
        for row in range(table.rowCount()):
            for col in range(1, table.columnCount()):
                item = table.item(row, col)
                if item is not None:
                    cat_id = item.data(Qt.ItemDataRole.UserRole)
                    if cat_id is not None:
                        self.db.set_severity_definition(row + 1, cat_id, item.text().strip())

        # Store the just-saved category names, colours and descriptions with
        # the matrix as a complete reusable profile.  Descriptions are saved
        # first because the snapshot is deliberately authoritative.
        cfg['consequence_categories'] = self.db._project_category_template(
            n_cons, cfg.get('consequence_categories'))
        self.db.set_risk_matrix(cfg)

        self._last_built_cfg = cfg
        self._load_matrix_ui()
        self._reload_axes_tables(cfg)
        if show_confirmation:
            QMessageBox.information(self, "Sparat", "Axlar och konsekvenskategorier sparade.")
        self.matrix_changed.emit()

    def _on_matrix_cell_width_changed(self, value):
        """Keep every visual matrix column equally wide at a chosen width."""
        self._matrix_cell_width = max(48, int(value))
        self.db.set_config('risk_matrix_cell_width', str(self._matrix_cell_width))
        self._apply_matrix_column_widths()

    def _on_matrix_splitter_moved(self, _pos, _index):
        """Splitter movement must not distort individual matrix columns."""
        QTimer.singleShot(0, self._sync_x_axis_button_width)

    def _apply_matrix_column_widths(self):
        """Apply one common width to matrix data columns only.

        Axis/category support columns keep their own readable widths.  This
        is deliberately independent of the outer splitter: a narrower pane
        scrolls rather than silently making one risk column wider than
        another.
        """
        grid = getattr(self, '_matrix_grid', None)
        if grid is None:
            return
        main_cols = getattr(self, '_matrix_main_column_count', 0)
        if not main_cols:
            return
        header_width = max(96, min(180, self._matrix_cell_width + 24))
        for col in range(min(main_cols, grid.columnCount())):
            width = header_width if col == 0 else self._matrix_cell_width
            grid.setColumnMinimumWidth(col, width)
            grid.setColumnStretch(col, 0)
            for row in range(grid.rowCount()):
                item = grid.itemAtPosition(row, col)
                widget = item.widget() if item else None
                if widget is not None:
                    widget.setMinimumWidth(width)
                    widget.setMaximumWidth(width)
        self._sync_x_axis_button_width()

    def _sync_x_axis_button_width(self):
        """Keep the X-direction button over the matrix, not category fields."""
        grid = getattr(self, '_matrix_grid', None)
        button = getattr(self, '_x_rev_chk', None)
        main_cols = getattr(self, '_matrix_main_column_count', 0)
        if grid is None or button is None or not main_cols:
            return
        grid.activate()
        width = 0
        for col in range(min(main_cols, grid.columnCount())):
            cell_width = grid.cellRect(0, col).width()
            widest = max(30, grid.columnMinimumWidth(col), cell_width)
            if cell_width <= 0:
                widest = max(widest, 30)
            for row in range(grid.rowCount()):
                item = grid.itemAtPosition(row, col)
                widget = item.widget() if item else None
                if widget is not None:
                    # A widget's width is the full grid cell/container width
                    # while layouts are being resolved; use its natural size
                    # instead so the button cannot grow to the category area.
                    if cell_width <= 0:
                        widest = max(widest, widget.sizeHint().width())
            width += widest
        if width > 0:
            button.setFixedWidth(width)

    def _load_matrix_ui(self):
        cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        self._last_built_cfg = None   # reset before blocking so _apply_size sees None
        # Block all signals that would trigger _apply_size while we populate controls
        _senders = (self._rows_spin, self._cols_spin, self._axis_combo,
                    self._x_rev_chk, self._y_rev_chk,
                    self._matrix_cell_width_spin)
        for w in _senders:
            w.blockSignals(True)
        self._rows_spin.setValue(cfg.get('rows', 5))
        self._cols_spin.setValue(cfg.get('cols', 7))
        x_axis = cfg.get('x_axis', 'frequency')
        idx = self._axis_combo.findData(x_axis)
        if idx >= 0:
            self._axis_combo.setCurrentIndex(idx)
        self._x_rev_chk.setChecked(bool(cfg.get('x_reversed', False)))
        self._y_rev_chk.setChecked(bool(cfg.get('y_reversed', False)))
        try:
            cell_width = int(self.db.get_config(
                'risk_matrix_cell_width', str(_MATRIX_CELL_WIDTH_DEFAULT)))
        except (TypeError, ValueError):
            cell_width = _MATRIX_CELL_WIDTH_DEFAULT
        self._matrix_cell_width = max(48, min(260, cell_width))
        self._matrix_cell_width_spin.setValue(self._matrix_cell_width)
        for w in _senders:
            w.blockSignals(False)
        self._build_matrix_grid(cfg)

    def _apply_size(self):
        """Rebuild the matrix grid. Handles axis swap without losing data."""
        n_cons    = self._rows_spin.value()
        n_freq    = self._cols_spin.value()
        old       = self.db.get_risk_matrix() or DEFAULT_MATRIX
        new_xaxis = self._axis_combo.currentData() or 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()

        # ── Recover semantic labels ───────────────────────────────────────────
        # Start from last-built config (source of truth for semantic order).
        # Only fall back to DB when the grid has never been built.
        disp          = getattr(self, '_last_built_cfg', None) or old
        disp_freq_on_x = disp.get('x_axis', 'frequency') == 'frequency'
        disp_x_rev    = disp.get('x_reversed', False)
        disp_y_rev    = disp.get('y_reversed', False)

        freq_codes = list(disp.get('x_codes', old.get(
            'x_codes', [f'F{i - 1}' for i in range(n_freq)])))
        cons_codes = list(disp.get('y_codes', old.get(
            'y_codes', [str(i + 1) for i in range(n_cons)])))
        # Descriptions belong to Axlar. They must not be reconstructed from
        # matrix header widgets when X/Y is changed.
        freq_lbls = list(disp.get('x_labels', old.get('x_labels', FREQ_LABELS[:n_freq])))
        cons_lbls = list(disp.get('y_labels', old.get('y_labels', SEV_LABELS[:n_cons])))

        # Apply any manual level-code edits from display widgets by mapping each
        # widget directly to its data index (no reversal ambiguity).
        if self._x_label_edits:
            nc = len(self._x_label_edits)
            for c, e in enumerate(self._x_label_edits):
                data_c = (nc - 1 - c) if disp_x_rev else c
                txt = e.text().strip()
                if disp_freq_on_x:
                    if data_c < len(freq_codes):
                        freq_codes[data_c] = txt
                else:
                    if data_c < len(cons_codes):
                        cons_codes[data_c] = txt

        if self._y_label_edits:
            nr = len(self._y_label_edits)
            for r, e in enumerate(self._y_label_edits):
                data_r = r if disp_y_rev else (nr - 1 - r)
                txt = e.text().strip()
                if disp_freq_on_x:
                    if data_r < len(cons_codes):
                        cons_codes[data_r] = txt
                else:
                    if data_r < len(freq_codes):
                        freq_codes[data_r] = txt

        # Pad/trim to new dimensions
        while len(freq_codes) < n_freq:
            freq_codes.append(f'F{len(freq_codes)-1}')
        while len(cons_codes) < n_cons:
            cons_codes.append(str(len(cons_codes)+1))
        while len(freq_lbls) < n_freq:
            freq_lbls.append('')
        while len(cons_lbls) < n_cons:
            cons_lbls.append('')
        freq_codes = freq_codes[:n_freq]
        cons_codes = cons_codes[:n_cons]
        freq_lbls = freq_lbls[:n_freq]
        cons_lbls = cons_lbls[:n_cons]

        # ── Cell data: current buttons override DB values ─────────────────────
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        lbl2d     = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        # 1. Fill from DB
        old_c  = old.get('cell_colors', [])
        old_l  = old.get('cell_labels', [])
        old_fg = old.get('cell_fg_colors', [])
        for ci in range(n_cons):
            for fi in range(n_freq):
                try:    colors[ci][fi]    = old_c[ci][fi]  or '#27ae60'
                except: colors[ci][fi]    = '#27ae60'
                try:    lbl2d[ci][fi]     = old_l[ci][fi]
                except: lbl2d[ci][fi]     = 'Låg'
                try:    fg_colors[ci][fi] = old_fg[ci][fi] or '#ffffff'
                except: fg_colors[ci][fi] = '#ffffff'
        # 2. Override with any user edits in the current buttons
        for _dr, row_btns in self._cell_buttons:
            for btn in row_btns:
                ci, fi = btn.row, btn.col
                if ci < n_cons and fi < n_freq:
                    if btn.color():    colors[ci][fi]    = btn.color()
                    # An intentionally blank cell label is real data, not a
                    # missing value that an axis rebuild may replace.
                    lbl2d[ci][fi]     = btn.label()
                    if btn.fg_color(): fg_colors[ci][fi] = btn.fg_color()

        new_cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':         new_xaxis,
            'x_reversed':     x_rev,
            'y_reversed':     y_rev,
            'x_codes':        freq_codes,
            'y_codes':        cons_codes,
            'x_labels':       freq_lbls,   # ALWAYS stores frequency labels
            'y_labels':       cons_lbls,   # ALWAYS stores consequence labels
            'cell_colors':    colors,
            'cell_labels':    lbl2d,
            'cell_fg_colors': fg_colors,
            'freq_boundaries': old.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES),
        }
        self._last_built_cfg = new_cfg
        self._build_matrix_grid(new_cfg)

    def _build_matrix_grid(self, cfg):
        """Build the matrix grid respecting axis orientation and intervals."""
        self._last_built_cfg = cfg   # track for _apply_size label recovery
        while self._matrix_grid.count():
            item = self._matrix_grid.takeAt(0)
            if item.widget():
                widget = item.widget()
                # X/Y controls are persistent widgets owned by the panel,
                # not disposable grid contents.  They are moved to new grid
                # positions on every axis rebuild; deleteLater() here leaves
                # a queued deletion that crashes the next click on F\\C.
                if widget not in (self._x_rev_chk, self._y_rev_chk):
                    widget.deleteLater()
        # QGridLayout retains explicit row minimums across rebuilds.  When
        # switching orientation, an old 40 px row could therefore remain
        # behind a new 28 px category editor and appear as air between rows.
        for row in range(32):
            self._matrix_grid.setRowMinimumHeight(row, 0)
            self._matrix_grid.setRowStretch(row, 0)
        self._cell_buttons       = []
        self._x_label_edits      = []
        self._y_label_edits      = []
        self._freq_boundary_edits = []
        self._sev_def_edits       = {}
        self._category_row_edits  = []
        self._x_category_rows     = []

        # Data always stored as [consequence_idx][frequency_idx]
        n_cons = cfg.get('rows', 5)    # consequence levels
        n_freq = cfg.get('cols', 7)    # frequency levels
        freq_codes = cfg.get('x_codes', [f'F{c-1}' for c in range(n_freq)])
        cons_codes = cfg.get('y_codes', [str(r+1) for r in range(n_cons)])
        freq_labels = cfg.get('x_labels', ['' for _ in range(n_freq)])
        cons_labels = cfg.get('y_labels', ['' for _ in range(n_cons)])
        colors          = cfg.get('cell_colors',    [['#27ae60'] * n_freq] * n_cons)
        cell_labels     = cfg.get('cell_labels',    [['Låg']     * n_freq] * n_cons)
        cell_fg_colors  = cfg.get('cell_fg_colors', [['#ffffff'] * n_freq] * n_cons)
        boundaries  = list(cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES))

        x_axis    = cfg.get('x_axis', 'frequency')
        freq_on_x = (x_axis == 'frequency')
        x_rev     = cfg.get('x_reversed', False)   # True = high value on left/top of X
        y_rev     = cfg.get('y_reversed', False)   # True = low value at top of Y

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons   # cols=freq, rows=cons
            col_codes, row_codes = freq_codes, cons_codes
            col_descs, row_descs = freq_labels, cons_labels
            corner_txt = "C \\ F"
            col_tip = "Frekvensetikett (X-axel)\nExempel: F3 – Möjlig | 10-100 år"
            row_tip = "Konsekvensnivå (Y-axel)\nExempel: C4 – Allvarlig"
        else:
            n_dcols, n_drows = n_cons, n_freq   # cols=cons, rows=freq
            col_codes, row_codes = cons_codes, freq_codes
            col_descs, row_descs = cons_labels, freq_labels
            corner_txt = "F \\ C"
            col_tip = "Konsekvensnivå (X-axel)\nExempel: C4 – Allvarlig"
            row_tip = "Frekvensetikett (Y-axel)\nExempel: F3 – Möjlig | 10-100 år"

        # The X-direction button belongs only to the matrix width. Category
        # definitions must not widen this span.
        self._matrix_main_column_count = n_dcols + 1 + (0 if freq_on_x else 1)

        _hdr_style = ("font-size:8px; font-weight:bold;"
                      "border:1px solid #aaa; border-radius:0px;"
                      "background:#eef2f7; padding:0 3px; margin:0px;")

        # The corner swaps presentation only; it never generates or changes
        # label text.
        corner = QToolButton()
        corner.setText("Byt\naxlar")
        corner.setToolTip("Byt vilken axel som visar frekvens respektive konsekvens")
        corner.clicked.connect(lambda: self._axis_combo.setCurrentIndex(
            1 - max(0, self._axis_combo.currentIndex())))
        corner.setStyleSheet("font-size:9px; font-weight:bold; border:1px solid #888; background:#eef2f7;")
        self._matrix_grid.addWidget(corner, 0, 0)

        # Column headers — apply x_rev: if reversed, col 0 shows the highest value
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            txt = col_codes[data_c] if data_c < len(col_codes) else str(data_c)
            e = QLineEdit(txt)
            e.setFixedHeight(28)
            e.setMinimumWidth(30)
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e.setStyleSheet(_hdr_style)
            desc = col_descs[data_c] if data_c < len(col_descs) else ''
            e.setToolTip((f"{txt} — {desc}" if desc else txt) +
                         "\nÄndra tecken eller beskrivning i vyn Axlar.")
            # QLineEdit.setText() leaves the cursor at the END of the text —
            # for a label wider than the fixed 80px field (e.g. "< 0.1/år"
            # at 8px font measures ~96px, see NOTES.md "'<'-tecknet syns
            # inte i gränsvärden"), the widget auto-scrolls to keep the
            # cursor visible, which scrolls the leading "<"/"≥" out of view.
            # Reset to show from the start instead (2026-08-17).
            e.setCursorPosition(0)
            self._matrix_grid.addWidget(e, 0, c + 1)
            self._x_label_edits.append(e)

        # Rows — apply y_rev: if NOT reversed, highest value is at top (default)
        for r in range(n_drows):
            if y_rev:
                disp_r = r              # low at top (r=0 = lowest value)
            else:
                disp_r = n_drows - 1 - r  # high at top (default)

            # Row header
            txt = row_codes[disp_r] if disp_r < len(row_codes) else str(disp_r)
            ey = QLineEdit(txt)
            ey.setFixedHeight(40)
            ey.setMinimumWidth(30)
            ey.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ey.setStyleSheet(_hdr_style)
            desc = row_descs[disp_r] if disp_r < len(row_descs) else ''
            ey.setToolTip((f"{txt} — {desc}" if desc else txt) +
                          "\nÄndra tecken eller beskrivning i vyn Axlar.")
            ey.setCursorPosition(0)   # see column-header comment above
            self._matrix_grid.addWidget(ey, r + 1, 0)
            self._y_label_edits.append(ey)   # index 0 = top row

            row_btns = []
            for c in range(n_dcols):
                # Resolve display column to data column (accounting for x_rev)
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map display → data (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx = disp_r
                    freq_idx = data_c
                else:
                    freq_idx = disp_r
                    cons_idx = data_c

                try: cc = colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cc = '#27ae60'
                try: cl = cell_labels[cons_idx][freq_idx]
                except (IndexError, KeyError): cl = 'Låg'
                try: cf = cell_fg_colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cf = '#ffffff'

                btn = MatrixCellButton(cons_idx, freq_idx, cc, cl, cf,
                                       is_top_row=(r == 0),
                                       is_left_col=(c == 0))
                btn.clicked.connect(partial(self._edit_cell, btn))
                self._matrix_grid.addWidget(btn, r + 1, c + 1)
                row_btns.append(btn)
            self._cell_buttons.append((disp_r, row_btns))

        # ── Interval / boundary row below cells ───────────────────────────────
        # Only shown when frequency is on X-axis (boundaries are per-frequency-column)
        if freq_on_x:
            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555; padding:0 3px;")
            self._matrix_grid.addWidget(bnd_lbl, n_drows + 1, 0)

            # When x_rev, the highest-freq column is at c=0 (leftmost) — ">allt" moves there
            # and the boundary values follow the reversed column order.
            highest_col = 0 if x_rev else n_dcols - 1
            for c in range(n_dcols):
                if c == highest_col:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, n_drows + 1, c + 1)
                else:
                    # Map display col → data freq index to pick the correct boundary
                    bval_idx = (n_dcols - 1 - c) if x_rev else c
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedHeight(22)
                    e.setMinimumWidth(30)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    e.setToolTip(
                        f"Övre gräns (händelser/år) för kolumn {c}.\n"
                        f"Frekvenser under detta värde tillhör denna kolumn.\n"
                        f"Exempel: 0.1 = en gång per 10 år")
                    self._matrix_grid.addWidget(e, n_drows + 1, c + 1)
                    self._freq_boundary_edits.append(e)
        else:
            # When frequency on Y: add interval boundary column on the right
            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555;")
            self._matrix_grid.addWidget(bnd_lbl, 0, n_dcols + 1)

            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            # Last row always gets ">allt" (the extreme bucket with no further boundary).
            # bval_idx depends on y_rev: y_rev=False → high-at-top, reversed boundary order.
            for r in range(n_drows):
                if r == n_drows - 1:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, r + 1, n_dcols + 1)
                else:
                    bval_idx = r if y_rev else (n_drows - 2 - r)
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedHeight(40)
                    e.setMinimumWidth(30)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    self._matrix_grid.addWidget(e, r + 1, n_dcols + 1)
                    self._freq_boundary_edits.append(e)

        # ── Consequence category definitions embedded in matrix ────────────────
        cats = self.db.consequence_categories()
        defs = self.db.get_severity_definitions()  # {sev_level: {cat_id: description}}

        _def_style = ("font-size:9px; border:1px solid #ccc; border-radius:0;"
                      "background:#f8f8ff; padding:1px 3px;")
        _cat_hdr_style = ("font-size:9px; font-weight:bold; background:#e8edf5;"
                          "border:1px solid #bbb; padding:2px 6px;")

        if False:  # retired inline description layout; Axlar owns this content
            # Consequence on X (columns) → category rows go BELOW the matrix
            # n_drows = n_freq; no boundary row exists (boundary is a column)
            base_row = n_drows + 1

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                # Begin immediately after the matrix/boundary row.  The old
                # spanning separator reserved an extra band and made the
                # definition editors appear detached at the bottom.
                cat_row = base_row + cat_i

                cat_lbl = QLabel(cat['name'])
                cat_lbl.setStyleSheet(_cat_hdr_style)
                cat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                cat_lbl.setMinimumHeight(CONFIG['H_ROW_STD'])
                self._matrix_grid.addWidget(cat_lbl, cat_row, 0)
                row_edits = []

                for c in range(n_dcols):      # n_dcols = n_cons
                    data_c    = (n_dcols - 1 - c) if x_rev else c
                    sev_level = data_c + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QTextEdit()
                    e.setPlainText(text)
                    e.setMinimumWidth(30)
                    e.setMaximumWidth(1000)
                    e.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                    e.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setMinimumHeight(CONFIG['H_ROW_STD'])
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.textChanged.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.toPlainText().strip()))
                    e.textChanged.connect(self._schedule_category_row_resize)
                    self._matrix_grid.addWidget(e, cat_row, c + 1)
                    self._sev_def_edits[(cat_id, sev_level)] = e
                    row_edits.append(e)
                self._x_category_rows.append((cat_lbl, row_edits))
            self._resize_category_rows()
        elif False:
            # Consequence on Y (rows) → category columns go to the RIGHT
            # n_dcols = n_freq; no boundary column exists (boundary is a row)
            base_col = n_dcols + 1
            # r -> list of this row's category QTextEdits, used below to size
            # the row header + cell buttons + category cells all to the
            # tallest wrapped text in that row (2026-08-17 user request —
            # only this orientation needed it, the `not freq_on_x` branch
            # above already had a working fixed row height).
            row_cat_edits = [[] for _ in range(n_drows)]

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_col = base_col + cat_i

                cat_hdr = QLabel(cat['name'])
                cat_hdr.setStyleSheet(_cat_hdr_style)
                cat_hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cat_hdr.setMinimumHeight(CONFIG['H_ROW_STD'])
                cat_hdr.setMinimumWidth(30)
                cat_hdr.setMaximumWidth(1000)
                cat_hdr.setWordWrap(True)
                self._matrix_grid.addWidget(cat_hdr, 0, cat_col)

                for r in range(n_drows):      # n_drows = n_cons
                    disp_r    = (n_drows - 1 - r) if not y_rev else r
                    sev_level = disp_r + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QTextEdit()
                    e.setPlainText(text)
                    e.setMinimumWidth(30)
                    e.setMaximumWidth(1000)
                    e.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                    e.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.textChanged.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.toPlainText().strip()))
                    e.textChanged.connect(self._schedule_category_row_resize)
                    self._matrix_grid.addWidget(e, r + 1, cat_col)
                    self._sev_def_edits[(cat_id, sev_level)] = e
                    row_cat_edits[r].append(e)

            self._category_row_edits = row_cat_edits
            self._resize_category_rows()

        self._apply_matrix_column_widths()
        QTimer.singleShot(0, self._sync_x_axis_button_width)

    def _schedule_category_row_resize(self):
        """Resize matrix rows after wrapped category text changes.

        QTextEdit updates its document layout asynchronously, so queue the
        measurement until the next event-loop turn.  All consequence levels
        deliberately receive the same height, matching the matrix cells.
        """
        QTimer.singleShot(0, self._resize_category_rows)

    def _resize_category_rows(self):
        # Consequence on X: category rows are independent and may therefore
        # grow to different heights according to their own wrapped text.
        x_rows = getattr(self, '_x_category_rows', None) or []
        if x_rows:
            for label, edits in x_rows:
                needed = CONFIG['H_ROW_STD']
                for edit in edits:
                    doc = edit.document()
                    doc.setTextWidth(edit.viewport().width())
                    needed = max(needed, int(doc.size().height()) + 8)
                label.setFixedHeight(needed)
                for edit in edits:
                    edit.setFixedHeight(needed)
            return
        rows = getattr(self, '_category_row_edits', None) or []
        if not rows or not getattr(self, '_y_label_edits', None):
            return
        # The risk cells themselves are 40 px high.  A shorter category
        # editor is vertically centred in that row and looks like a gap
        # between rows; once its text reaches three lines it merely happens
        # to grow to 40 px, which masked the problem.  Keep the whole row
        # aligned from the start and grow all members together thereafter.
        needed = max(CONFIG['H_ROW_STD'], 40)
        for edits in rows:
            for edit in edits:
                doc = edit.document()
                doc.setTextWidth(edit.viewport().width())
                needed = max(needed, int(doc.size().height()) + 8)
        for row in range(min(len(self._y_label_edits), len(self._cell_buttons))):
            self._matrix_grid.setRowMinimumHeight(row + 1, needed)
            self._matrix_grid.setRowStretch(row + 1, 0)
            self._y_label_edits[row].setFixedHeight(needed)
            for btn in self._cell_buttons[row][1]:
                btn.setFixedHeight(needed)
            for edit in rows[row]:
                edit.setFixedHeight(needed)

    def _sync_freq_label_from_boundary(self, boundary_edit, col_idx: int):
        """Validate a boundary edit without changing level codes/descriptions.

        Codes (such as ST1 A--E) and descriptions are deliberately separate
        user input.  Earlier versions regenerated the visible header here,
        which made a boundary edit look as though it had overwritten an axis.
        """
        try:
            val = float(boundary_edit.text().strip())
        except ValueError:
            return
        if val <= 0:
            return

    def _cell_edit_menu(self, btn):
        """Build the explicit left-click menu for one risk-matrix cell."""
        menu = QMenu(self)
        change_color = menu.addAction("Ändra färg…")
        change_color.setData('color')
        change_text = menu.addAction("Ändra text…")
        change_text.setData('text')
        # QAction.triggered is Qt's stable path for a menu click. Keeping
        # the edit here avoids depending on which Python wrapper QMenu.exec
        # happens to return on a given platform.
        change_color.triggered.connect(lambda: self._edit_cell_color(btn))
        change_text.triggered.connect(lambda: self._edit_cell_text(btn))
        return menu

    def _edit_cell(self, btn):
        """Left-click a matrix cell and choose whether to edit colour or text."""
        menu = self._cell_edit_menu(btn)
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _edit_cell_color(self, btn):
        color = QColorDialog.getColor(
            QColor(btn.color()), self, "Välj bakgrundsfärg för cell")
        if not color.isValid():
            return
        # Keep the text legible automatically when only the cell colour is
        # changed. This avoids forcing the user through an unrelated second
        # dialog merely to make a small colour adjustment.
        r, g, b = color.red(), color.green(), color.blue()
        fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        btn.set_cell(color.name(), fg_color=fg)
        btn.update()
        self._matrix_grid.activate()

    def _edit_cell_text(self, btn):
        label, ok = QInputDialog.getText(
            self, "Celltext",
            "Risknivå-etikett (t.ex. Låg, Medium, Hög, Kritisk):",
            text=btn.label())
        if ok:
            # A blank label is meaningful: it deliberately leaves this risk
            # cell without text instead of silently restoring its old label.
            btn.set_cell(btn.color(), label.strip(), btn.fg_color())
            btn.update()
            self._matrix_grid.activate()

    def _save_matrix_values(self, show_confirmation=True):
        n_cons = self._rows_spin.value()   # consequence levels (rows in data)
        n_freq = self._cols_spin.value()   # frequency levels  (cols in data)
        x_axis = self._axis_combo.currentData() or 'frequency'
        freq_on_x = (x_axis == 'frequency')

        # Cell buttons store (cons_idx, freq_idx) regardless of display orientation
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        labels    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        for _disp_r, row_btns in self._cell_buttons:
            for btn in row_btns:
                cons_i, freq_i = btn.row, btn.col   # (cons_idx, freq_idx)
                if cons_i < n_cons and freq_i < n_freq:
                    colors[cons_i][freq_i]    = btn.color()
                    labels[cons_i][freq_i]    = btn.label()
                    fg_colors[cons_i][freq_i] = btn.fg_color()

        # Level codes are held in display order. Map them back to semantic
        # data indices: either axis can carry frequency/consequence and both
        # display directions can be reversed.
        raw_col = [e.text().strip() for e in self._x_label_edits]
        raw_row = [e.text().strip() for e in self._y_label_edits]

        def _semantic_codes(display_codes, count, reversed_display):
            codes = ['' for _ in range(count)]
            for display_index, text in enumerate(display_codes):
                data_index = (count - 1 - display_index
                              if reversed_display else display_index)
                if 0 <= data_index < count:
                    codes[data_index] = text
            return codes

        if freq_on_x:
            # X=freq columns, Y=cons rows
            x_codes = _semantic_codes(raw_col, n_freq, self._x_rev_chk.isChecked())
            y_codes = _semantic_codes(raw_row, n_cons,
                                        not self._y_rev_chk.isChecked())
        else:
            # X=cons columns, Y=freq rows
            y_codes = _semantic_codes(raw_col, n_cons, self._x_rev_chk.isChecked())
            x_codes = _semantic_codes(raw_row, n_freq,
                                        not self._y_rev_chk.isChecked())

        # Pad/trim to correct lengths
        while len(x_codes) < n_freq: x_codes.append(f'F{len(x_codes)-1}')
        while len(y_codes) < n_cons: y_codes.append(str(len(y_codes)+1))
        x_codes = x_codes[:n_freq]
        y_codes = y_codes[:n_cons]
        working = getattr(self, '_last_built_cfg', None) or self.db.get_risk_matrix() or DEFAULT_MATRIX
        x_labels = list(working.get('x_labels', []))[:n_freq]
        y_labels = list(working.get('y_labels', []))[:n_cons]
        x_labels.extend([''] * (n_freq - len(x_labels)))
        y_labels.extend([''] * (n_cons - len(y_labels)))

        cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':      x_axis,
            'x_reversed':  self._x_rev_chk.isChecked(),
            'y_reversed':  self._y_rev_chk.isChecked(),
            'x_codes':     x_codes,
            'y_codes':     y_codes,
            'x_labels':    x_labels,
            'y_labels':    y_labels,
            'cell_colors':    colors,
            'cell_labels':    labels,
            'cell_fg_colors': fg_colors,
        }
        # Read frequency boundaries from editable row/column (display order)
        freq_boundaries = []
        for e in getattr(self, '_freq_boundary_edits', []):
            try:
                v = float(e.text().strip())
                if v > 0:
                    freq_boundaries.append(v)
            except ValueError:
                pass
        if not freq_boundaries:
            freq_boundaries = list(DEFAULT_FREQ_BOUNDARIES)
        # Boundary edits were laid out in display order; convert back to data order
        # (lowest freq level first) by reversing when the display was reversed:
        #   freq_on_x + x_rev: highest-freq col is leftmost → edits stored high-to-low
        #   freq_on_y + NOT y_rev: highest-freq row is topmost → edits stored high-to-low
        _is_reversed_display = (freq_on_x and self._x_rev_chk.isChecked()) or \
                               (not freq_on_x and not self._y_rev_chk.isChecked())
        if _is_reversed_display:
            freq_boundaries = list(reversed(freq_boundaries))
        cfg['freq_boundaries'] = freq_boundaries

        pending_categories = getattr(self, '_pending_template_categories', None)
        if pending_categories is not None:
            cfg['consequence_categories'] = json.loads(json.dumps(pending_categories))

        cfg = _normalise_matrix(cfg)   # ensure consistent before saving
        try:
            if pending_categories is not None:
                self.db.apply_risk_matrix_template_without_assessments(cfg)
                self._pending_template_categories = None
                self._load_categories()
                self._last_built_cfg = cfg
                self._load_matrix_ui()
                self._reload_axes_tables(cfg)
            else:
                self.db.set_risk_matrix(cfg)
        except Exception as exc:
            QMessageBox.critical(self, "Riskmatrisen sparades inte", str(exc))
            return
        if show_confirmation:
            QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
        self.matrix_changed.emit()

    def _reload_custom_matrix_templates(self):
        """Show project-local templates immediately below the standard ones."""
        layout = getattr(self, '_custom_matrix_templates_layout', None)
        widget = getattr(self, '_custom_matrix_templates_widget', None)
        if layout is None or widget is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        templates = self.db.get_custom_risk_matrix_templates()
        widget.setVisible(bool(templates))
        if not templates:
            return
        label = QLabel("Egna mallar:")
        label.setStyleSheet("font-size:10px; color:#4b5563;")
        layout.addWidget(label)
        for template in templates:
            button = QPushButton(template['name'])
            button.setToolTip("Egen riskmatrismall i detta projekt")
            button.clicked.connect(
                lambda _checked=False, t=template: self._request_matrix_template(
                    t['matrix'], t['name']))
            layout.addWidget(button)
            delete_button = QToolButton()
            delete_button.setText("×")
            delete_button.setToolTip(f"Ta bort egna mallen '{template['name']}'")
            delete_button.setAutoRaise(True)
            delete_button.setFixedWidth(20)
            delete_button.clicked.connect(
                lambda _checked=False, name=template['name']:
                self._delete_custom_matrix_template(name))
            layout.addWidget(delete_button)
        layout.addStretch()

    def _delete_custom_matrix_template(self, name):
        answer = QMessageBox.question(
            self, "Ta bort egen riskmatrismall",
            f"Vill du ta bort mallen '{name}'? Den aktiva riskmatrisen ändras inte.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.db.delete_custom_risk_matrix_template(name):
            self._reload_custom_matrix_templates()

    def _ask_custom_matrix_template_name(self):
        return QInputDialog.getText(
            self, "Spara egen riskmatrismall", "Mallnamn:",
            text="Egen riskmatris")

    def _save_matrix(self):
        """Save the complete risk profile once and retain it as a named template."""
        # Bring matrix cell/header changes into the same working copy before
        # deciding whether Axlar also needs to be saved.
        self._apply_size()
        has_axes_edits = self._axes_dirty
        has_matrix_edits = not self._working_matrix_matches_saved()
        if not has_axes_edits and not has_matrix_edits and \
                getattr(self, '_pending_template_categories', None) is None:
            QMessageBox.information(self, "Inga ändringar", "Det finns inga riskmatrisändringar att spara.")
            return

        name, ok = self._ask_custom_matrix_template_name()
        name = name.strip()
        if not ok:
            return
        if not name:
            QMessageBox.warning(self, "Mallnamn saknas", "Ange ett namn för den egna riskmatrismallen.")
            return

        before = json.dumps(_normalise_matrix(json.loads(json.dumps(
            self.db.get_risk_matrix() or DEFAULT_MATRIX))), sort_keys=True)
        if has_axes_edits:
            self._save_axes_and_categories_values(show_confirmation=False)
        else:
            self._save_matrix_values(show_confirmation=False)
        saved = self.db.get_risk_matrix() or DEFAULT_MATRIX
        after = json.dumps(_normalise_matrix(json.loads(json.dumps(saved))), sort_keys=True)
        if after == before:
            # Validation errors in the delegated save routines leave the
            # database untouched; in that case never create a misleading
            # named template.
            return
        try:
            self.db.save_custom_risk_matrix_template(name, saved)
        except Exception as exc:
            QMessageBox.critical(self, "Mallen sparades inte", str(exc))
            return
        self._reload_custom_matrix_templates()
        QMessageBox.information(self, "Sparat", f"Riskmatrisen sparades som mallen '{name}'.")

    def _save_axes_and_categories(self):
        """Compatibility entry point for tests and older callers."""
        self._save_matrix()

    def _working_matrix_matches_saved(self):
        """Template migration must never silently discard unsaved grid edits."""
        saved = _normalise_matrix(json.loads(json.dumps(
            self.db.get_risk_matrix() or DEFAULT_MATRIX)))
        working = _normalise_matrix(json.loads(json.dumps(
            getattr(self, '_last_built_cfg', None) or saved)))
        return json.dumps(saved, sort_keys=True) == json.dumps(working, sort_keys=True)

    def _load_matrix_template_working_copy(self, cfg):
        """Show a candidate template without writing data or database config."""
        cfg = _normalise_matrix(json.loads(json.dumps(cfg)))
        senders = (self._rows_spin, self._cols_spin, self._axis_combo,
                   self._x_rev_chk, self._y_rev_chk)
        for widget in senders:
            widget.blockSignals(True)
        try:
            self._rows_spin.setValue(cfg['rows'])
            self._cols_spin.setValue(cfg['cols'])
            self._axis_combo.setCurrentIndex(
                max(0, self._axis_combo.findData(cfg.get('x_axis', 'frequency'))))
            self._x_rev_chk.setChecked(bool(cfg.get('x_reversed', False)))
            self._y_rev_chk.setChecked(bool(cfg.get('y_reversed', False)))
        finally:
            for widget in senders:
                widget.blockSignals(False)
        self._last_built_cfg = cfg
        self._pending_template_categories = json.loads(json.dumps(
            cfg.get('consequence_categories', [])))
        self._build_matrix_grid(cfg)

    def _request_frequency_template(self, labels, bounds, name, codes=None):
        """Build a complete, reusable template for a frequency standard."""
        # Do not borrow project-local categories or colours here.  A standard
        # template must carry its own categories, axis orientation and cell
        # colours so the subsequent migration can review every difference.
        candidate = json.loads(json.dumps(DEFAULT_MATRIX))
        candidate['cols'] = len(labels)
        candidate['x_labels'] = list(labels)
        candidate['x_codes'] = list(codes or labels)
        candidate['freq_boundaries'] = list(bounds)
        self._request_matrix_template(candidate, name)

    def _request_matrix_template(self, candidate, name):
        """Open a safe migration when an existing study has assessed data."""
        if not self._working_matrix_matches_saved():
            QMessageBox.warning(
                self, "Osparade ändringar",
                "Spara eller ladda om den pågående riskmatrisredigeringen först. "
                "Mallbytet jämför alltid med den senast sparade matrisen.")
            return
        source = self.db.get_risk_matrix() or DEFAULT_MATRIX
        candidate = _normalise_matrix(json.loads(json.dumps(candidate)))
        preview = self.db.risk_matrix_migration_preview(source, candidate)
        has_assessments = bool(preview['frequency_records'] or preview['severity_records'] or
                               preview['definition_records'])
        if not has_assessments:
            self._load_matrix_template_working_copy(candidate)
            return
        dialog = RiskMatrixMigrationDialog(self.db, source, candidate, name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = self.db.apply_risk_matrix_migration(dialog.plan)
        except Exception as exc:
            QMessageBox.critical(self, "Mallbytet genomfördes inte", str(exc))
            return
        self._load_matrix_ui()
        self._reload_axes_tables()
        self._pending_template_categories = None
        QMessageBox.information(
            self, "Riskmatris migrerad",
            f"{result['frequency_count']} frekvens- och {result['severity_count']} "
            f"konsekvensbedömningar migrerades.\n\nBackup:\n{result['backup_path']}")
        self.matrix_changed.emit()

    def _apply_st1_preset(self):
        """Load the ST1 Sverige AB matrix into the editable matrix UI.

        This is deliberately a working copy only; the database is changed
        only when the user presses ``Spara riskmatris``.
        """
        self._load_matrix_template_working_copy(ST1_RISK_MATRIX_PRESET)

    def _apply_freq_preset(self, labels: list, bounds: list):
        """Populate frequency axis headers and boundary edits from a preset.

        labels: ordered lowest-to-highest frequency (data order).
        bounds: n-1 boundary values (events/year), data order lowest first.
        Accounts for current axis orientation (freq_on_x/y) and direction (x_rev/y_rev).
        """
        freq_on_x = (self._axis_combo.currentData() or 'frequency') == 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()
        n         = len(labels)

        if freq_on_x:
            # _x_label_edits[i] = display column i → data index (n-1-i if x_rev else i)
            for i, e in enumerate(self._x_label_edits):
                data_idx = (n - 1 - i) if x_rev else i
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
                    e.setCursorPosition(0)
            # _freq_boundary_edits: edit[i] maps to bval_idx (n-1-(i+1) if x_rev else i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = (n - 2 - i) if x_rev else i
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")
        else:
            # _y_label_edits[0] = top row
            # y_rev=False: top=highest freq → data index n-1-i; y_rev=True: top=lowest → i
            for i, e in enumerate(self._y_label_edits):
                data_idx = i if y_rev else (n - 1 - i)
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
                    e.setCursorPosition(0)
            # _freq_boundary_edits for y case: edit[i] → bval_idx (i if y_rev else n-2-i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = i if y_rev else (n - 2 - i)
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")

    def _load_categories(self):
        self._cat_list.clear()
        for cat in self.db.consequence_categories():
            item = QListWidgetItem(cat['name'])
            item.setData(Qt.ItemDataRole.UserRole, cat['id'])
            self._cat_list.addItem(item)

    def _cat_add(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Ny kategori", "Namn:")
        if ok and name.strip():
            self.db.add_category(name.strip())
            self._load_categories()
            self._apply_size()
            self._reload_axes_tables()

    def _cat_rename(self):
        from PyQt6.QtWidgets import QInputDialog
        item = self._cat_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_category(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._load_categories()
            self._apply_size()
            self._reload_axes_tables()

    def _cat_delete(self):
        item = self._cat_list.currentItem()
        if not item: return
        self.db.delete_category(item.data(Qt.ItemDataRole.UserRole))
        self._load_categories()
        # 2026-08-11 fix ('När jag ... tar bort en konsekvenskategori skall
        # detta synas i riskmatrisen direkt') — _cat_add/_cat_rename already
        # called _apply_size() to rebuild the matrix grid; delete was
        # missing this call, so the matrix kept showing the deleted
        # category's severity-definition row until the next unrelated
        # rebuild (e.g. resizing the rows/cols spinners).
        self._apply_size()
        self._reload_axes_tables()
        if hasattr(self, '_sev_def_panel') and self._sev_def_panel:
            self._sev_def_panel.refresh()

    def _cat_move(self, direction):
        """Move the selected category up (direction=-1) or down (+1) in
        display order (2026-08-11, 'jag vill även kunna justera ordningen,
        exempelvis genom vilken ordning de dyker upp')."""
        item = self._cat_list.currentItem()
        if not item:
            return
        row = self._cat_list.row(item)
        new_row = row + direction
        if not (0 <= new_row < self._cat_list.count()):
            return
        ordered_ids = [self._cat_list.item(i).data(Qt.ItemDataRole.UserRole)
                       for i in range(self._cat_list.count())]
        ordered_ids[row], ordered_ids[new_row] = ordered_ids[new_row], ordered_ids[row]
        self.db.reorder_categories(ordered_ids)
        self._load_categories()
        self._cat_list.setCurrentRow(new_row)
        self._apply_size()
        self._reload_axes_tables()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════
