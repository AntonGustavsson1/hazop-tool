#!/usr/bin/env python3
"""Settings and HAZOP-preparation panels — split out of hazop.py
2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i fler
filer"."""

import re
import json
from pathlib import Path
from functools import partial

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDateEdit,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal, QDate, QEvent, QMimeData
from PyQt6.QtGui import QBrush, QColor, QDrag, QFont, QFontMetrics

from constants import CONFIG, SEV_LABELS
from database import (
    Database, DEFAULT_MATRIX, DEFAULT_FREQ_BOUNDARIES, _STD_OBJECTS,
    _normalise_matrix, _risk_matrix_cache, get_matrix, freq_to_f_level,
    risk_info,
)
from pid_viewer import _icon, FREQ_LABELS, ocr_status
from ui_helpers import freq_axis_label
from equipment_panel import TagDatabasePanel, PIDAnalysisPanel

_PALETTE_MIME = 'application/x-hazop-palette-color'


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
        self.setFixedSize(80, 40)
        self.setAcceptDrops(True)
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
            f"QPushButton:hover{{border:2px solid #000; margin:-1px;}}")
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

    # ── Drag-and-drop ─────────────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
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
        else:
            event.ignore()


class StandardCausesSettingsPanel(QWidget):
    """4-level editable hierarchy: Nodtyp → Avvikelse → Objekt → Orsaker."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False
        self._loading_nt = False
        self._node_type_ids = []

        layout = QHBoxLayout(self)

        # ── Col 0: Nodtyp (2026-08-17 user request) ──────────────────────────
        # Filters _dev_list to deviations belonging to the selected node
        # type; drag a deviation from _dev_list onto a node type here to
        # COPY it (deep, independent copy incl. its causes — user confirmed
        # via AskUserQuestion, not a move/link) into that type.
        c0 = QVBoxLayout()
        c0.addWidget(QLabel("<b>Nodtyp</b>"))
        self._nodetype_list = QListWidget()
        self._nodetype_list.currentRowChanged.connect(lambda _row: self._load_deviations())
        self._nodetype_list.itemChanged.connect(self._on_nodetype_item_changed)
        self._nodetype_list.setAcceptDrops(True)
        self._nodetype_list.viewport().setAcceptDrops(True)
        self._nodetype_list.installEventFilter(self)
        self._nodetype_list.viewport().installEventFilter(self)
        c0.addWidget(self._nodetype_list)
        c0b = QHBoxLayout()
        for icon, slot in (('+', self._add_node_type), ('−', self._del_node_type)):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c0b.addWidget(b)
        c0b.addStretch(); c0.addLayout(c0b)
        # _load_node_types() is deferred to the end of __init__ (below,
        # after _dev_list exists) — its setCurrentRow() call fires
        # currentRowChanged immediately, which cascades into
        # _load_deviations(), so calling it here (before _dev_list is
        # built) would crash with AttributeError.

        # ── Col 1: Avvikelse ──────────────────────────────────────────────────
        c1 = QVBoxLayout()
        c1.addWidget(QLabel("<b>Avvikelse</b>"))
        self._dev_list = QListWidget()
        self._dev_list.currentRowChanged.connect(self._on_dev_sel)
        self._dev_list.setDragEnabled(True)
        # Instance-level override (same monkeypatch pattern already used
        # elsewhere in this file, e.g. ParticipantMatrixPanel's Enter-key
        # handling) — carries the deviation's DB id as custom mime text
        # instead of Qt's default internal model-index payload, so the
        # Nodtyp column's drop handler can read it directly.
        def _dev_list_mime_data(items, _list=self._dev_list):
            md = QMimeData()
            if items:
                dev_id = items[0].data(Qt.ItemDataRole.UserRole)
                md.setText(f'hzp:stddev:{dev_id}')
            return md
        self._dev_list.mimeData = _dev_list_mime_data
        c1.addWidget(self._dev_list)
        c1b = QHBoxLayout()
        for icon, slot in (('+', self._add_dev), ('−', self._del_dev),
                           ('↑', lambda: self._move_dev(-1)), ('↓', lambda: self._move_dev(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c1b.addWidget(b)
        c1b.addStretch(); c1.addLayout(c1b)

        # ── Col 2: Objekt ─────────────────────────────────────────────────────
        c2 = QVBoxLayout()
        self._obj_lbl = QLabel("<b>Objekt</b>")
        c2.addWidget(self._obj_lbl)
        self._obj_list = QListWidget()
        self._obj_list.currentRowChanged.connect(self._on_obj_sel)
        c2.addWidget(self._obj_list)
        # "implementera de funktioner som finns i standardobjekt även i
        # standard orsaker så man kan lägga till nya objekt under
        # standardorsaker" (2026-08-17, see NOTES.md) — same add/delete/
        # reorder/rename CRUD as StandardObjectsSettingsPanel's own
        # _list, over the exact same standard_objects table, so a new
        # object type no longer requires switching tabs.
        c2b = QHBoxLayout()
        for icon, slot in (('+', self._add_obj), ('−', self._del_obj),
                           ('↑', lambda: self._move_obj(-1)), ('↓', lambda: self._move_obj(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c2b.addWidget(b)
        c2b.addStretch(); c2.addLayout(c2b)
        # Show all objects; objects with causes are highlighted
        self._show_all_obj_chk = QCheckBox("Visa alla objekt")
        self._show_all_obj_chk.setChecked(True)
        self._show_all_obj_chk.stateChanged.connect(lambda _: self._load_objects())
        c2.addWidget(self._show_all_obj_chk)

        # ── Col 3: Orsaker ────────────────────────────────────────────────────
        c3 = QVBoxLayout()
        self._cause_lbl = QLabel("<b>Orsaker</b>")
        c3.addWidget(self._cause_lbl)
        self._cause_list = QListWidget()
        self._cause_list.currentRowChanged.connect(self._on_cause_sel)
        c3.addWidget(self._cause_list)

        # Frequency field for selected cause
        freq_row = QHBoxLayout()
        freq_lbl = QLabel("Frekvens (/år):")
        freq_lbl.setStyleSheet("font-size:10px; color:#555;")
        freq_row.addWidget(freq_lbl)
        self._freq_edit = QLineEdit()
        self._freq_edit.setPlaceholderText("t.ex. 0.01")
        self._freq_edit.setMaximumWidth(90)
        self._freq_edit.setToolTip("Basfrekvens för vald orsak (händelser/år). Lämna tomt om okänd.")
        self._freq_edit.editingFinished.connect(self._save_freq)
        freq_row.addWidget(self._freq_edit)
        self._freq_level_lbl = QLabel("")
        self._freq_level_lbl.setStyleSheet("color:#8D9299; font-size:10px;")
        freq_row.addWidget(self._freq_level_lbl)
        freq_row.addStretch()
        c3.addLayout(freq_row)

        c3b = QHBoxLayout()
        for icon, slot in (('+', self._add_cause), ('−', self._del_cause),
                           ('↑', lambda: self._move_cause(-1)), ('↓', lambda: self._move_cause(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c3b.addWidget(b)
        c3b.addStretch(); c3.addLayout(c3b)
        btn_sync = QPushButton("Synka frekvenser →")
        btn_sync.setToolTip("Uppdaterar frekvensen på alla orsaker kopplade till standardorsaker.")
        btn_sync.clicked.connect(self._sync_freqs)
        c3.addWidget(btn_sync)
        # Feature 16: export/import buttons
        io_row = QHBoxLayout()
        btn_exp = QPushButton("↑ Exportera")
        btn_exp.setToolTip("Exportera hela standardbiblioteket till JSON")
        btn_exp.clicked.connect(self._export_library)
        btn_imp = QPushButton("↓ Importera")
        btn_imp.setToolTip("Importera standardbibliotek från JSON (lägger till, skriver ej över)")
        btn_imp.clicked.connect(self._import_library)
        io_row.addWidget(btn_exp); io_row.addWidget(btn_imp)
        c3.addLayout(io_row)

        layout.addLayout(c0, 1)
        layout.addLayout(c1, 1)
        layout.addLayout(c2, 1)
        layout.addLayout(c3, 1)
        self._load_node_types()   # cascades into _load_deviations() via currentRowChanged

    # ── Load helpers ──────────────────────────────────────────────────────────
    # ── Node type CRUD (2026-08-17, see NOTES.md) ────────────────────────────
    def _load_node_types(self):
        self._loading_nt = True
        cur = self._nodetype_list.currentRow()
        self._nodetype_list.clear()
        types = self.db.node_types()
        self._node_type_ids = [t['id'] for t in types]
        for t in types:
            item = QListWidgetItem(t['name'])
            item.setData(Qt.ItemDataRole.UserRole, t['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._nodetype_list.addItem(item)
        self._loading_nt = False
        self._nodetype_list.setCurrentRow(max(0, min(cur, self._nodetype_list.count() - 1)))

    def _current_node_type_id(self):
        item = self._nodetype_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_nodetype_item_changed(self, item):
        if self._loading_nt:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        name = item.text().strip()
        if id_ is not None and name:
            self.db.rename_node_type(id_, name)

    def _add_node_type(self):
        new_id = self.db.add_node_type('Ny nodtyp')
        item = QListWidgetItem('Ny nodtyp')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._nodetype_list.addItem(item)
        self._node_type_ids.append(new_id)
        self._nodetype_list.setCurrentItem(item)
        self._nodetype_list.editItem(item)

    def _del_node_type(self):
        item = self._nodetype_list.currentItem()
        if not item:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if len(self._node_type_ids) <= 1:
            QMessageBox.information(self, 'Kan inte ta bort',
                                     'Minst en nodtyp måste finnas kvar.')
            return
        if QMessageBox.question(
                self, 'Ta bort nodtyp',
                'Ta bort nodtypen? Avvikelser under den flyttas till standardtypen.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            self.db.delete_node_type(id_)
            self._load_node_types()

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        _drop_targets = (self._nodetype_list, self._nodetype_list.viewport())
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:stddev:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:stddev:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            text = event.mimeData().text() if event.mimeData().hasText() else ''
            if text.startswith('hzp:stddev:'):
                self._handle_deviation_drop(event, obj)
                return True
        return super().eventFilter(obj, event)

    def _handle_deviation_drop(self, event, source_obj):
        text = event.mimeData().text()
        try:
            dev_id = int(text.split(':')[2])
        except (IndexError, ValueError):
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        # Qt delivers drop events to either the outer QListWidget or its
        # viewport depending on version/setup (same lesson as TreePanel's
        # equipment-drop handling) — itemAt() always expects viewport
        # coordinates, so remap only when the event landed on the outer
        # widget instead of the viewport directly.
        if source_obj is self._nodetype_list:
            pos = self._nodetype_list.viewport().mapFrom(self._nodetype_list, pos)
        item = self._nodetype_list.itemAt(pos)
        if item is None:
            event.ignore()
            return
        node_type_id = item.data(Qt.ItemDataRole.UserRole)
        self.db.copy_standard_deviation_to_node_type(dev_id, node_type_id)
        event.acceptProposedAction()
        if node_type_id == self._current_node_type_id():
            self._load_deviations()

    def _load_deviations(self):
        self._loading = True
        cur = self._dev_list.currentRow()
        self._dev_list.clear()
        nt_id = self._current_node_type_id()
        default_nt_id = self._node_type_ids[0] if self._node_type_ids else None
        for d in self.db.standard_deviations():
            d_nt = d['node_type_id']
            belongs = (d_nt == nt_id) or (d_nt is None and nt_id == default_nt_id)
            if not belongs:
                continue
            item = QListWidgetItem(d['description'])
            item.setData(Qt.ItemDataRole.UserRole, d['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._dev_list.addItem(item)
        self._loading = False
        self._dev_list.setCurrentRow(max(0, min(cur, self._dev_list.count()-1)))

    def _current_dev_id(self):
        item = self._dev_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_objects(self, dev_id=None):
        if dev_id is None:
            dev_id = self._current_dev_id()
        self._loading = True
        cur = self._obj_list.currentRow()
        self._obj_list.clear()
        if dev_id is None:
            self._loading = False; return
        show_all = self._show_all_obj_chk.isChecked()
        if show_all:
            rows = self.db.all_objects_with_cause_counts(dev_id)
        else:
            rows = self.db.objects_for_deviation(dev_id)
        for r in rows:
            label = r['name']
            n = r.get('n_causes', 0)
            if n:
                label = f"{r['name']}  ({n})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, r['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, r['name'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            if n:
                item.setForeground(QColor('#17191C'))
            self._obj_list.addItem(item)
        try:
            self._obj_list.itemChanged.disconnect(self._on_obj_changed)
        except TypeError:
            pass   # wasn't connected yet (first call)
        self._obj_list.itemChanged.connect(self._on_obj_changed)
        self._loading = False
        self._obj_list.setCurrentRow(max(0, min(cur, self._obj_list.count()-1)))

    def _current_obj_id(self):
        item = self._obj_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_causes(self, dev_id=None, obj_id=None):
        if dev_id is None: dev_id = self._current_dev_id()
        if obj_id is None: obj_id = self._current_obj_id()
        self._loading = True
        cur = self._cause_list.currentRow()
        self._cause_list.clear()
        if dev_id is None or obj_id is None:
            self._loading = False; return
        dev_item = self._dev_list.currentItem()
        obj_item = self._obj_list.currentItem()
        dev_name = dev_item.text() if dev_item else ''
        obj_name = obj_item.data(Qt.ItemDataRole.UserRole + 1) if obj_item else ''
        self._cause_lbl.setText(f"<b>Orsaker</b> — {dev_name} / {obj_name}")
        for c in self.db.standard_causes_for_object(dev_id, obj_id):
            freq = c.get('frequency')
            label = c['description']
            if freq is not None:
                label += f"  [{freq:g}/år]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole,     c['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, c['description'])
            item.setData(Qt.ItemDataRole.UserRole + 2, freq)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._cause_list.addItem(item)
        self._loading = False
        self._cause_list.setCurrentRow(max(0, min(cur, self._cause_list.count()-1)))
        # Clear freq field if no cause selected after reload
        if self._cause_list.currentRow() < 0:
            self._freq_edit.clear()
            self._freq_level_lbl.setText('')

    # ── Slot chains ───────────────────────────────────────────────────────────
    def _on_dev_sel(self, row):
        if self._loading: return
        dev_item = self._dev_list.item(row)
        if dev_item:
            self._obj_lbl.setText(f"<b>Objekt</b> — {dev_item.text()}")
        self._load_objects()

    def _on_obj_sel(self, row):
        if self._loading: return
        self._load_causes()

    def _on_cause_sel(self, row):
        if self._loading: return
        item = self._cause_list.item(row)
        # Populate freq field
        freq = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        self._freq_edit.blockSignals(True)
        self._freq_edit.setText(f"{freq:g}" if freq is not None else '')
        self._freq_edit.blockSignals(False)
        self._freq_level_lbl.setText(
            freq_axis_label(freq_to_f_level(freq)) if freq is not None else '')

    def _save_freq(self):
        """Save the edited frequency for the currently selected standard cause."""
        item = self._cause_list.currentItem()
        if not item: return
        cid = item.data(Qt.ItemDataRole.UserRole)
        if cid is None: return
        text = self._freq_edit.text().strip()
        if not text:
            freq = None
            self._freq_level_lbl.setText('')
        else:
            try:
                freq = float(text)
                self._freq_level_lbl.setText(freq_axis_label(freq_to_f_level(freq)))
            except ValueError:
                self._freq_level_lbl.setText('Ogiltigt')
                return
        self.db.update_standard_cause(cid, frequency=freq)
        # Update display label in list
        item.setData(Qt.ItemDataRole.UserRole + 2, freq)
        desc = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        if freq is not None:
            item.setText(f"{desc}  [{freq:g}/år]")
        else:
            item.setText(desc)

    # ── Deviation CRUD ────────────────────────────────────────────────────────
    def _add_dev(self):
        new_id = self.db.add_standard_deviation('Ny avvikelse', self._current_node_type_id())
        item = QListWidgetItem('Ny avvikelse')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._dev_list.addItem(item)
        self._dev_list.editItem(item)

    def _del_dev(self):
        item = self._dev_list.currentItem()
        if not item: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ and QMessageBox.question(self, 'Ta bort', 'Ta bort avvikelse och alla dess orsaker?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            self.db.delete_standard_deviation(id_)
            self._load_deviations()

    def _move_dev(self, d):
        row = self._dev_list.currentRow()
        new_row = row + d
        if not (0 <= new_row < self._dev_list.count()): return
        a = self._dev_list.takeItem(row)
        self._dev_list.insertItem(new_row, a)
        self._dev_list.setCurrentRow(new_row)
        ids = [self._dev_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._dev_list.count())]
        self.db.reorder_standard_deviations(ids)

    # ── Object CRUD (2026-08-17, see NOTES.md — same standard_objects
    # table/methods as StandardObjectsSettingsPanel's own _list) ─────────────────
    def _on_obj_changed(self, item):
        if self._loading:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        # Strip the "  (n)" cause-count suffix _load_objects() appends
        # for display — an edit must only ever change the object's own
        # name, never bake the count into it.
        name = re.sub(r'\s*\(\d+\)$', '', item.text()).strip()
        if name:
            self.db.update_standard_object(id_, name)
        self._load_objects()

    def _add_obj(self):
        new_id = self.db.add_standard_object('Nytt objekt')
        if not self._show_all_obj_chk.isChecked():
            # A brand-new object has zero causes yet, so it wouldn't
            # appear in the "only objects with causes" view at all —
            # switch to "Visa alla objekt" so it's actually visible to
            # rename/edit right away. Its own stateChanged already
            # triggers _load_objects().
            self._show_all_obj_chk.setChecked(True)
        else:
            self._load_objects()
        for i in range(self._obj_list.count()):
            if self._obj_list.item(i).data(Qt.ItemDataRole.UserRole) == new_id:
                self._obj_list.setCurrentRow(i)
                self._obj_list.editItem(self._obj_list.item(i))
                break

    def _del_obj(self):
        item = self._obj_list.currentItem()
        if not item:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        self.db.delete_standard_object(id_)
        self._load_objects()

    def _move_obj(self, direction):
        row = self._obj_list.currentRow()
        new_row = row + direction
        if not (0 <= new_row < self._obj_list.count()):
            return
        a = self._obj_list.takeItem(row)
        self._obj_list.insertItem(new_row, a)
        self._obj_list.setCurrentRow(new_row)
        ids = [self._obj_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._obj_list.count())]
        self.db.reorder_standard_objects(ids)

    # ── Cause CRUD ────────────────────────────────────────────────────────────
    def _add_cause(self):
        dev_id = self._current_dev_id()
        obj_id = self._current_obj_id()
        if dev_id is None or obj_id is None: return
        new_id = self.db.add_standard_cause_with_object(dev_id, obj_id, 'Ny orsak')
        item = QListWidgetItem('Ny orsak')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, 'Ny orsak')
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._cause_list.addItem(item)
        self._cause_list.editItem(item)
        self._load_objects()   # refresh object cause counts

    def _del_cause(self):
        item = self._cause_list.currentItem()
        if not item: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_:
            self.db.delete_standard_cause(id_)
            row = self._cause_list.row(item)
            self._cause_list.takeItem(row)
            self._load_objects()

    def _move_cause(self, d):
        row = self._cause_list.currentRow()
        new_row = row + d
        if not (0 <= new_row < self._cause_list.count()): return
        a = self._cause_list.takeItem(row)
        self._cause_list.insertItem(new_row, a)
        self._cause_list.setCurrentRow(new_row)
        ids = [self._cause_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._cause_list.count())]
        self.db.reorder_standard_causes(ids)

    def _on_cause_changed(self, item):
        if self._loading: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_:
            self.db.update_standard_cause(id_, description=item.text().strip())

    # ── Sync ──────────────────────────────────────────────────────────────────
    def _sync_freqs(self):
        ret = QMessageBox.question(self, 'Synka frekvenser',
            'Uppdatera frekvenser på alla kopplade orsaker?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            n = self.db.update_cause_freqs_from_standard()
            QMessageBox.information(self, 'Klart', f'{n} orsak(er) uppdaterades.')

    # ── Feature 16: Export/import standard library ────────────────────────────
    def _export_library(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exportera standardbibliotek', '', 'JSON (*.json)')
        if not path: return
        data = {'deviations': [], 'objects': []}
        for dev in self.db.standard_deviations():
            dd = {'description': dev['description'], 'causes': []}
            for c in self.db.standard_causes(dev['id']):
                cd = dict(c)
                dd['causes'].append({k: cd.get(k) for k in
                    ['description', 'comp_type', 'frequency', 'object_id']})
            data['deviations'].append(dd)
        for obj in self.db.standard_objects():
            data['objects'].append(obj['name'])
        import json as _json
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_json.dumps(data, ensure_ascii=False, indent=2))
        QMessageBox.information(self, 'Exporterat', f'Sparat till:\n{path}')

    def _import_library(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Importera standardbibliotek', '', 'JSON (*.json)')
        if not path: return
        import json as _json
        try:
            with open(path, encoding='utf-8') as f:
                data = _json.loads(f.read())
        except Exception as e:
            QMessageBox.critical(self, 'Fel', str(e)); return
        added_devs = added_causes = added_objs = 0
        for obj_name in data.get('objects', []):
            if not self.db.conn.execute(
                    "SELECT id FROM standard_objects WHERE name=?", (obj_name,)).fetchone():
                self.db.add_standard_object(obj_name); added_objs += 1
        for dev_d in data.get('deviations', []):
            dev_row = self.db.conn.execute(
                "SELECT id FROM standard_deviations WHERE description=?",
                (dev_d['description'],)).fetchone()
            if not dev_row:
                dev_id = self.db.add_standard_deviation(dev_d['description'])
                added_devs += 1
            else:
                dev_id = dev_row[0]
            for c in dev_d.get('causes', []):
                obj_id = c.get('object_id')
                if not self.db.conn.execute(
                        "SELECT id FROM standard_causes WHERE deviation_id=? AND description=?",
                        (dev_id, c['description'])).fetchone():
                    self.db.add_standard_cause_with_object(dev_id, obj_id or 0, c['description'])
                    added_causes += 1
        self.db.conn.commit()
        self._load_deviations()
        QMessageBox.information(self, 'Importerat',
            f'Lagt till: {added_devs} avvikelser, {added_causes} orsaker, {added_objs} objekt.')


class StandardObjectsSettingsPanel(QWidget):
    """Editable list of standard object types (from orsaker.txt)."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Standardobjekt</b> — dessa objekttyper är tillgängliga i orsaksformulären "
            "och kan kopplas till orsaksbeskrivningar."))

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list)

        btns = QHBoxLayout()
        btn_add = QPushButton("+ Lägg till")
        btn_add.clicked.connect(self._add)
        btn_del = QPushButton("− Ta bort")
        btn_del.clicked.connect(self._delete)
        btn_up  = QPushButton("↑")
        btn_up.setFixedWidth(28)
        btn_up.clicked.connect(lambda: self._move(-1))
        btn_dn  = QPushButton("↓")
        btn_dn.setFixedWidth(28)
        btn_dn.clicked.connect(lambda: self._move(1))
        btn_reset = QPushButton("Återställ standard")
        btn_reset.setToolTip("Lägger tillbaka alla standardobjekt från ursprungslistan (lägger inte till dubbletter)")
        btn_reset.clicked.connect(self._reset)
        for b in (btn_add, btn_del, btn_up, btn_dn, btn_reset):
            btns.addWidget(b)
        btns.addStretch()
        layout.addLayout(btns)

        self._loading = False
        self._load()

    def _load(self):
        self._loading = True
        cur = self._list.currentRow()
        self._list.clear()
        for obj in self.db.standard_objects():
            item = QListWidgetItem(obj['name'])
            item.setData(Qt.ItemDataRole.UserRole, obj['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._list.addItem(item)
        try:
            self._list.itemChanged.disconnect(self._on_changed)
        except TypeError:
            pass   # wasn't connected yet (first call)
        self._list.itemChanged.connect(self._on_changed)
        self._loading = False
        if cur >= 0:
            self._list.setCurrentRow(min(cur, self._list.count() - 1))

    def _on_changed(self, item):
        if self._loading:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is not None:
            self.db.update_standard_object(id_, item.text().strip())

    def _add(self):
        new_id = self.db.add_standard_object('Nytt objekt')
        item = QListWidgetItem('Nytt objekt')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._list.addItem(item)
        self._list.editItem(item)

    def _delete(self):
        item = self._list.currentItem()
        if not item:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is not None:
            self.db.delete_standard_object(id_)
        self._list.takeItem(self._list.row(item))

    def _move(self, direction):
        row = self._list.currentRow()
        new_row = row + direction
        if not (0 <= new_row < self._list.count()):
            return
        a = self._list.takeItem(row)
        self._list.insertItem(new_row, a)
        self._list.setCurrentRow(new_row)
        ids = [self._list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._list.count())]
        self.db.reorder_standard_objects(ids)

    def _reset(self):
        for name in _STD_OBJECTS:
            exists = self.db.conn.execute(
                "SELECT id FROM standard_objects WHERE name=?", (name,)).fetchone()
            if not exists:
                self.db.add_standard_object(name)
        self._load()


class SeverityDefinitionsPanel(QWidget):
    """Grid panel: consequence categories (rows) × severity levels (cols).
    Each cell holds a short description of what that level means for that category."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._edits = {}   # (severity_level, category_id) → QLineEdit

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        lbl = QLabel(
            "Definiera vad varje konsekvensgrad (C1–CN) innebär per kategori. "
            "Värdena visas som referens vid bedömning av konsekvenser.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#555; font-size:11px;")
        outer.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._scroll_widget = QWidget()
        self._grid_layout = QGridLayout(self._scroll_widget)
        self._grid_layout.setSpacing(4)
        scroll.setWidget(self._scroll_widget)
        outer.addWidget(scroll)

        self.refresh()

    def refresh(self):
        """Rebuild grid from current matrix config + categories."""
        # Save pending edits before rebuild
        self._flush_pending()

        cfg  = get_matrix()
        y    = cfg.get('y_labels', [])
        n    = cfg.get('rows', 5)
        cats = self.db.consequence_categories()
        defs = self.db.get_severity_definitions()

        # Clear grid
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._edits.clear()

        if not cats:
            self._grid_layout.addWidget(
                QLabel("Lägg till konsekvenskategorier i fliken Kategorier först."), 0, 0)
            return

        # Header row: severity level labels
        self._grid_layout.addWidget(QLabel(""), 0, 0)  # top-left corner
        for col_idx in range(n):
            label = y[col_idx] if col_idx < len(y) else f"C{col_idx+1}"
            hdr = QLabel(f"<b>C{col_idx+1}</b><br><small>{label}</small>")
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr.setStyleSheet(
                "background:#F5F5F3; color:#17191C; border-radius:3px; padding:4px 6px;")
            hdr.setMinimumWidth(120)
            self._grid_layout.addWidget(hdr, 0, col_idx + 1)

        # Data rows: one per category
        for row_idx, cat in enumerate(cats):
            cat_id = cat['id']
            cat_name = cat['name']

            cat_lbl = QLabel(f"<b>{cat_name}</b>")
            cat_lbl.setStyleSheet("padding:2px 4px;")
            cat_lbl.setMinimumWidth(90)
            self._grid_layout.addWidget(cat_lbl, row_idx + 1, 0)

            for col_idx in range(n):
                sev_lvl = col_idx + 1  # 1-based
                desc = defs.get(sev_lvl, {}).get(cat_id, '')
                edit = QLineEdit(desc)
                edit.setPlaceholderText(f"C{sev_lvl}, {cat_name}…")
                edit.setMinimumWidth(120)
                # Save on focus-out
                _lvl, _cid = sev_lvl, cat_id
                edit.editingFinished.connect(
                    lambda _e=edit, _l=_lvl, _c=_cid:
                        self.db.set_severity_definition(_l, _c, _e.text().strip()))
                self._edits[(_lvl, _cid)] = edit
                self._grid_layout.addWidget(edit, row_idx + 1, col_idx + 1)

        self._grid_layout.setColumnStretch(0, 0)
        for c in range(1, n + 1):
            self._grid_layout.setColumnStretch(c, 1)

    def _flush_pending(self):
        """Save all currently displayed edits to DB."""
        for (lvl, cid), edit in self._edits.items():
            self.db.set_severity_definition(lvl, cid, edit.text().strip())


class TagMemoryPanel(QWidget):
    """View and edit the smart object recognition memory for this project."""

    # Column indices
    _C_USE  = 0   # "Använd" checkbox
    _C_PFX  = 1   # prefix
    _C_TYPE = 2   # comp_type (editable)
    _C_CNT  = 3   # usage count
    _C_UPD  = 4   # updated

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        tf = QFont(); tf.setBold(True); tf.setPointSize(10)

        # ── Master toggle ──────────────────────────────────────────────────────
        master_row = QHBoxLayout()
        self._master_cb = QCheckBox("Använd smart igenkänning")
        self._master_cb.setToolTip(
            "När ikryssad föreslår programmet objekttyp automatiskt baserat på "
            "tagg-prefixet (t.ex. GPA → Pump).")
        self._master_cb.setChecked(
            db.get_config('smart_recognition_enabled', '1') == '1')
        self._master_cb.toggled.connect(self._on_master_toggled)
        f = QFont(); f.setBold(True)
        self._master_cb.setFont(f)
        master_row.addWidget(self._master_cb)
        master_row.addStretch()
        btn_clear = QPushButton("Rensa allt")
        btn_clear.setIcon(_icon('delete'))
        btn_clear.setToolTip("Ta bort alla lärda mappningar för detta projekt")
        btn_clear.clicked.connect(self._clear_all)
        master_row.addWidget(btn_clear)
        lay.addLayout(master_row)

        info = QLabel(
            "Ikryssad rad = aktiv förval för det prefixet. "
            "Att kryssa i en rad inaktiverar automatiskt övriga för samma prefix. "
            "Lägg till mappningar manuellt nedan — de gäller omedelbart.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555; font-size:10px;")
        lay.addWidget(info)

        # ── Manual add row ─────────────────────────────────────────────────────
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self._pfx_edit  = QLineEdit()
        self._pfx_edit.setPlaceholderText("Prefix  t.ex. QMA")
        self._pfx_edit.setMaximumWidth(100)
        self._pfx_edit.setFixedHeight(CONFIG['H_CTRL_STD'])
        add_row.addWidget(self._pfx_edit)
        self._type_combo = QComboBox()
        self._type_combo.setFixedHeight(CONFIG['H_CTRL_STD'])
        from pid_viewer import KNOWN_PREFIXES as _KP
        obj_names = sorted({v[1] for v in _KP.values() if v[1]})
        # Also add standard object names
        for nm in ['Manuell ventil','On-off ventil','Reglerventil','Backventil',
                   'Säkerhetsventil / sprängbleck','Pump','Kompressor / fläkt',
                   'Värmeväxlare / kylare / värmare','Tank / kärl / kolonn',
                   'Rörledning / slang','Instrument','Övrigt']:
            if nm not in obj_names:
                obj_names.append(nm)
        obj_names.sort()
        self._type_combo.addItems(obj_names)
        add_row.addWidget(self._type_combo, 1)
        btn_add = QPushButton("+ Lägg till")
        btn_add.setFixedHeight(CONFIG['H_CTRL_STD'])
        btn_add.clicked.connect(self._add_manual_entry)
        add_row.addWidget(btn_add)
        lay.addLayout(add_row)

        # ── Tag memory table ───────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels(
            ["Använd", "Prefix", "Komponenttyp", "Antal", "Senast"])
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(self._C_USE,  QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(self._C_PFX,  QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_TYPE, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(self._C_CNT,  QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(self._C_UPD,  QHeaderView.ResizeMode.ResizeToContents)
        self._tbl.setColumnWidth(self._C_PFX, 90)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._tbl.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self._tbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_del = QPushButton("Ta bort markerade")
        btn_del.setIcon(_icon('delete'))
        btn_del.clicked.connect(self._delete_selected)
        btn_row.addWidget(btn_del)
        lay.addLayout(btn_row)

        # ── Fingerprints ───────────────────────────────────────────────────────
        fp_hdr = QHBoxLayout()
        fp_title = QLabel("Visuella fingeravtryck (symbolmönster)")
        fp_title.setFont(tf)
        fp_hdr.addWidget(fp_title)
        fp_hdr.addStretch()
        btn_fp_clear = QPushButton("Rensa fingeravtryck")
        btn_fp_clear.setIcon(_icon('delete'))
        btn_fp_clear.clicked.connect(self._clear_fingerprints)
        fp_hdr.addWidget(btn_fp_clear)
        lay.addLayout(fp_hdr)

        self._fp_tbl = QTableWidget(0, 3)
        self._fp_tbl.setHorizontalHeaderLabels(
            ["Komponenttyp", "Exempeltagg", "Antal matchningar"])
        fp_h = self._fp_tbl.horizontalHeader()
        fp_h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fp_h.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        fp_h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._fp_tbl.setColumnWidth(1, 120)
        self._fp_tbl.verticalHeader().setVisible(False)
        self._fp_tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._fp_tbl.setAlternatingRowColors(True)
        self._fp_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._fp_tbl.setMaximumHeight(140)
        lay.addWidget(self._fp_tbl)

        self.refresh()

    def _on_master_toggled(self, checked: bool):
        self.db.set_config('smart_recognition_enabled', '1' if checked else '0')

    def _add_manual_entry(self):
        """Manually add/override a prefix→type mapping and make it the active choice."""
        pfx = self._pfx_edit.text().strip().upper()
        comp = self._type_combo.currentText().strip()
        if not pfx or not comp:
            return
        # Deactivate all existing entries for this prefix
        try:
            self.db.conn.execute(
                "UPDATE study_tag_memory SET active=0 WHERE UPPER(tag)=UPPER(?)",
                (pfx,))
            # Insert/update the chosen (prefix, type) with high count + active=1
            existing = self.db.conn.execute(
                "SELECT usage_count FROM study_tag_memory "
                "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                (pfx, comp)).fetchone()
            now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')
            if existing:
                self.db.conn.execute(
                    "UPDATE study_tag_memory SET active=1, usage_count=usage_count+1, updated=? "
                    "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                    (now, pfx, comp))
            else:
                self.db.conn.execute(
                    "INSERT INTO study_tag_memory (tag,comp_type,active,updated) VALUES (?,?,1,?)",
                    (pfx, comp, now))
            self.db.commit()
        except Exception as e:
            QMessageBox.warning(self, "Fel", str(e))
            return
        self._pfx_edit.clear()
        self.refresh()

    def refresh(self):
        self._tbl.blockSignals(True)
        self._tbl.setRowCount(0)
        try:
            rows = self.db.conn.execute(
                "SELECT tag, comp_type, usage_count, updated, active "
                "FROM study_tag_memory ORDER BY tag, usage_count DESC").fetchall()
        except Exception:
            try:
                rows = self.db.conn.execute(
                    "SELECT tag, comp_type, usage_count, updated, 1 as active "
                    "FROM study_tag_memory ORDER BY tag, usage_count DESC").fetchall()
            except Exception:
                rows = []

        # Find the winning (highest-count active) type per prefix for highlighting
        best: dict = {}  # prefix → max active usage_count
        for row in rows:
            d = dict(row)
            if d['active']:
                best[d['tag']] = max(best.get(d['tag'], 0), d['usage_count'])

        for row in rows:
            r = self._tbl.rowCount()
            self._tbl.insertRow(r)
            d = dict(row)
            is_winner = d['active'] and d['usage_count'] == best.get(d['tag'], -1)

            # Col 0 — "Använd" checkbox
            use_item = QTableWidgetItem()
            use_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use_item.setCheckState(
                Qt.CheckState.Checked if d['active'] else Qt.CheckState.Unchecked)
            # Store both prefix AND comp_type for the DB update
            use_item.setData(Qt.ItemDataRole.UserRole, (d['tag'], d['comp_type']))
            self._tbl.setItem(r, self._C_USE, use_item)

            # Col 1 — prefix (bold if this is the winning row)
            pfx_item = QTableWidgetItem(d['tag'])
            pfx_item.setData(Qt.ItemDataRole.UserRole, d['tag'])
            colour = QColor('#17191C') if d['active'] else QColor('#aaa')
            pfx_item.setForeground(QBrush(colour))
            if is_winner:
                f = pfx_item.font(); f.setBold(True); pfx_item.setFont(f)
            pfx_item.setFlags(pfx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(r, self._C_PFX, pfx_item)

            # Col 2 — comp_type (not editable — type is defined by what you pick)
            ct = QTableWidgetItem(d['comp_type'])
            if not d['active']:
                ct.setForeground(QBrush(QColor('#aaa')))
            elif is_winner:
                f = ct.font(); f.setBold(True); ct.setFont(f)
                ct.setToolTip('Används som förval (flest val)')
            ct.setFlags(ct.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(r, self._C_TYPE, ct)

            # Col 3 — count
            uc = QTableWidgetItem(str(d['usage_count']))
            uc.setFlags(uc.flags() & ~Qt.ItemFlag.ItemIsEditable)
            uc.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_winner:
                f = uc.font(); f.setBold(True); uc.setFont(f)
            self._tbl.setItem(r, self._C_CNT, uc)

            # Col 4 — updated
            upd = QTableWidgetItem(d['updated'] or '')
            upd.setFlags(upd.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(r, self._C_UPD, upd)

        self._tbl.blockSignals(False)

        # Fingerprints
        self._fp_tbl.setRowCount(0)
        try:
            fp_rows = self.db.conn.execute(
                "SELECT comp_type, tag_example, usage_count "
                "FROM symbol_fingerprints ORDER BY usage_count DESC").fetchall()
        except Exception:
            fp_rows = []
        for row in fp_rows:
            r = self._fp_tbl.rowCount()
            self._fp_tbl.insertRow(r)
            d = dict(row)
            self._fp_tbl.setItem(r, 0, QTableWidgetItem(d['comp_type']))
            self._fp_tbl.setItem(r, 1, QTableWidgetItem(d['tag_example']))
            uc = QTableWidgetItem(str(d['usage_count']))
            uc.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._fp_tbl.setItem(r, 2, uc)

    def _on_item_changed(self, item):
        col = item.column()
        row = item.row()
        key_item = self._tbl.item(row, self._C_USE)
        if not key_item:
            return
        key_data = key_item.data(Qt.ItemDataRole.UserRole)  # (prefix, comp_type) tuple
        if not isinstance(key_data, tuple) or len(key_data) != 2:
            return
        prefix, comp_type = key_data

        if col == self._C_USE:
            active = item.checkState() == Qt.CheckState.Checked
            self._tbl.blockSignals(True)
            try:
                if active:
                    # Exclusive per prefix — deactivate all other types for this prefix
                    self.db.conn.execute(
                        "UPDATE study_tag_memory SET active=0 WHERE UPPER(tag)=UPPER(?)",
                        (prefix,))
                    self.db.conn.execute(
                        "UPDATE study_tag_memory SET active=1 "
                        "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                        (prefix, comp_type))
                    self.db.commit()
                    # Reflect the change visually for all rows of this prefix
                    for r2 in range(self._tbl.rowCount()):
                        ki2 = self._tbl.item(r2, self._C_USE)
                        if ki2 and isinstance(ki2.data(Qt.ItemDataRole.UserRole), tuple):
                            pfx2, ct2 = ki2.data(Qt.ItemDataRole.UserRole)
                            if pfx2.upper() == prefix.upper():
                                is_this_row = (ct2.upper() == comp_type.upper())
                                ki2.setCheckState(
                                    Qt.CheckState.Checked if is_this_row
                                    else Qt.CheckState.Unchecked)
                                colour = QColor('#17191C') if is_this_row else QColor('#aaa')
                                for c in (self._C_PFX, self._C_TYPE, self._C_CNT):
                                    it2 = self._tbl.item(r2, c)
                                    if it2:
                                        it2.setForeground(QBrush(colour))
                else:
                    self.db.set_tag_memory_active(prefix, comp_type, False)
                    grey = QColor('#aaa')
                    for c in (self._C_PFX, self._C_TYPE, self._C_CNT):
                        it = self._tbl.item(row, c)
                        if it:
                            it.setForeground(QBrush(grey))
            except Exception:
                pass
            self._tbl.blockSignals(False)

    def _delete_selected(self):
        rows = sorted({i.row() for i in self._tbl.selectedItems()}, reverse=True)
        for r in rows:
            key_item = self._tbl.item(r, self._C_USE)
            if key_item:
                key_data = key_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(key_data, tuple) and len(key_data) == 2:
                    prefix, comp_type = key_data
                    try:
                        self.db.conn.execute(
                            "DELETE FROM study_tag_memory "
                            "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                            (prefix, comp_type))
                    except Exception:
                        pass
        self.db.commit()
        self.refresh()

    def _clear_all(self):
        if QMessageBox.question(
                self, "Rensa tagminne",
                "Ta bort alla lärda tagg-mappningar för detta projekt?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            try:
                self.db.conn.execute("DELETE FROM study_tag_memory")
                self.db.commit()
            except Exception:
                pass
            self.refresh()

    def _clear_fingerprints(self):
        if QMessageBox.question(
                self, "Rensa fingeravtryck",
                "Ta bort alla visuella fingeravtryck?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            try:
                self.db.conn.execute("DELETE FROM symbol_fingerprints")
                self.db.commit()
            except Exception:
                pass
            self.refresh()


class ParticipantMatrixPanel(QWidget):
    """Deltagarmatris: participants as rows (Förnamn/Efternamn/Roll) ×
    analystillfällen as columns, with a checkbox per cell marking
    attendance. Replaces the old free-text "Deltagare" field in the
    Projekt tab (2026-08-11, user request: "en till flik med deltagare
    istället där man definerar förnamn, efternamn, roll på y axel och
    analystillfälen på x axeln så det blir en matris" — see NOTES.md for
    the full design rationale)."""

    _FIXED_COLS = ['Förnamn', 'Efternamn']

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False
        self._participant_ids = []
        self._session_ids = []
        self._column_ids = []   # custom participant_columns, between Efternamn and sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        lbl = QLabel(
            "<b>Deltagarmatris</b> — en rad per deltagare (förnamn, efternamn, roll) "
            "och en kolumn per analystillfälle. Bocka i cellen för att markera att "
            "deltagaren var med vid det tillfället.")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        self._table = QTableWidget(0, len(self._FIXED_COLS))
        self._table.setHorizontalHeaderLabels(self._FIXED_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._on_item_changed)
        # Enter on a selected (non-editing) cell adds a new participant row,
        # matching the same "+"-button action (2026-08-17 user request).
        _base_kp = self._table.keyPressEvent
        def _table_key_press(event, _base=_base_kp):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                    self._table.state() != QTableWidget.State.EditingState:
                self._add_participant()
            else:
                _base(event)
        self._table.keyPressEvent = _table_key_press
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_add_p = QPushButton("+ Lägg till deltagare")
        btn_add_p.clicked.connect(self._add_participant)
        btn_del_p = QPushButton("Ta bort deltagare")
        btn_del_p.setToolTip("Tar bort den markerade raden (deltagaren)")
        btn_del_p.clicked.connect(self._delete_participant)
        btn_add_col = QPushButton("+ Lägg till kolumn")
        btn_add_col.setToolTip("Lägg till en egen namngiven kolumn (t.ex. E-post, Företag, Roll)")
        btn_add_col.clicked.connect(self._add_column)
        btn_del_col = QPushButton("Ta bort kolumn")
        btn_del_col.setToolTip("Tar bort den egna kolumn en markerad cell tillhör")
        btn_del_col.clicked.connect(self._delete_column)
        btn_add_s = QPushButton("+ Lägg till analystillfälle")
        btn_add_s.clicked.connect(self._add_session)
        btn_del_s = QPushButton("Ta bort analystillfälle")
        btn_del_s.setToolTip("Tar bort kolumnen för det tillfälle en markerad cell tillhör")
        btn_del_s.clicked.connect(self._delete_session)
        for b in (btn_add_p, btn_del_p, btn_add_col, btn_del_col, btn_add_s, btn_del_s):
            btn_row.addWidget(b)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        self._loading = True
        try:
            columns = self.db.list_participant_columns()
            sessions = self.db.list_analysis_sessions()
            participants = self.db.list_participants()
            attendance = self.db.get_attendance_matrix()
            col_values = self.db.get_participant_column_values()

            self._column_ids = [c['id'] for c in columns]
            self._session_ids = [s['id'] for s in sessions]
            self._participant_ids = [p['id'] for p in participants]

            headers = (list(self._FIXED_COLS) + [c['name'] for c in columns] +
                       [(s['label'] or f"Tillfälle {s['id']}") for s in sessions])
            self._table.setColumnCount(len(headers))
            self._table.setHorizontalHeaderLabels(headers)
            self._table.setRowCount(len(participants))

            n_custom = len(columns)
            n_fixed = len(self._FIXED_COLS) + n_custom
            for row, p in enumerate(participants):
                self._table.setItem(row, 0, QTableWidgetItem(p['first_name'] or ''))
                self._table.setItem(row, 1, QTableWidgetItem(p['last_name'] or ''))
                for ci, col_def in enumerate(columns):
                    val = col_values.get((p['id'], col_def['id']), '')
                    self._table.setItem(row, len(self._FIXED_COLS) + ci, QTableWidgetItem(val))
                for col, sess in enumerate(sessions):
                    item = QTableWidgetItem()
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable)
                    attended = attendance.get((p['id'], sess['id']), False)
                    item.setCheckState(
                        Qt.CheckState.Checked if attended else Qt.CheckState.Unchecked)
                    self._table.setItem(row, n_fixed + col, item)
        finally:
            self._loading = False

    def _on_item_changed(self, item):
        if self._loading:
            return
        row, col = item.row(), item.column()
        if row < 0 or row >= len(self._participant_ids):
            return
        pid = self._participant_ids[row]
        n_base = len(self._FIXED_COLS)
        if col == 0:
            self.db.update_participant(pid, first_name=item.text())
        elif col == 1:
            self.db.update_participant(pid, last_name=item.text())
        elif col < n_base + len(self._column_ids):
            col_id = self._column_ids[col - n_base]
            self.db.set_participant_column_value(pid, col_id, item.text())
        else:
            sess_idx = col - n_base - len(self._column_ids)
            if 0 <= sess_idx < len(self._session_ids):
                sess_id = self._session_ids[sess_idx]
                attended = item.checkState() == Qt.CheckState.Checked
                self.db.set_attendance(pid, sess_id, attended)

    def _add_participant(self):
        self.db.add_participant('', '', '')
        self.refresh()

    def _delete_participant(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._participant_ids):
            return
        self.db.delete_participant(self._participant_ids[row])
        self.refresh()

    def _add_column(self):
        name, ok = QInputDialog.getText(
            self, "Ny kolumn", "Kolumnnamn (t.ex. E-post, Företag, Roll):")
        if ok and name.strip():
            self.db.add_participant_column(name.strip())
            self.refresh()

    def _delete_column(self):
        col = self._table.currentColumn()
        col_idx = col - len(self._FIXED_COLS)
        if col < 0 or col_idx < 0 or col_idx >= len(self._column_ids):
            return
        self.db.delete_participant_column(self._column_ids[col_idx])
        self.refresh()

    def _add_session(self):
        dlg = _AnalysisSessionDateDialog(self)
        if dlg.exec():
            self.db.add_analysis_session(dlg.selected_date_label())
            self.refresh()

    def _delete_session(self):
        col = self._table.currentColumn()
        sess_idx = col - len(self._FIXED_COLS) - len(self._column_ids)
        if col < 0 or sess_idx < 0 or sess_idx >= len(self._session_ids):
            return
        self.db.delete_analysis_session(self._session_ids[sess_idx])
        self.refresh()


class _AnalysisSessionDateDialog(QDialog):
    """Date-picker replacement for the old free-text QInputDialog when adding
    an analystillfälle (2026-08-17 user request) — a QDateEdit + "Idag"
    button, same widgets/pattern as the Projekt tab's date-range row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nytt analystillfälle")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Datum för analystillfället:"))
        row = QHBoxLayout()
        self._date_edit = QDateEdit()
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_edit.setDate(QDate.currentDate())
        today_btn = QPushButton("Idag")
        today_btn.clicked.connect(lambda: self._date_edit.setDate(QDate.currentDate()))
        row.addWidget(self._date_edit)
        row.addWidget(today_btn)
        lay.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def selected_date_label(self):
        return self._date_edit.date().toString('yyyy-MM-dd')


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
        self._sev_def_edits  = {}   # (cat_id, sev_level) → QLineEdit, embedded in matrix grid

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

        # ── Tab: Riskmatris ───────────────────────────────────────────────────
        matrix_tab = QWidget()
        ml = QVBoxLayout(matrix_tab)
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

        self._matrix_container = QWidget()
        self._matrix_grid = QGridLayout(self._matrix_container)
        self._matrix_grid.setSpacing(0)
        self._matrix_grid.setContentsMargins(0, 0, 0, 0)

        _wrap_lay.addWidget(self._matrix_container,
                            alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _wrap_lay.addStretch(1)
        scroll.setWidget(_wrap)
        ml.addWidget(scroll)

        # Axis orientation + direction controls
        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axlar:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItem("Frekvens → X,  Konsekvens → Y  (standard)", 'frequency')
        self._axis_combo.addItem("Konsekvens → X,  Frekvens → Y", 'consequence')
        ax_row.addWidget(self._axis_combo, 1)
        ax_row.addWidget(QLabel("  Riktning:"))
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
        ax_row.addWidget(self._x_rev_chk)
        ax_row.addWidget(self._y_rev_chk)
        ml.addLayout(ax_row)

        # Live update: rebuild grid immediately on any control change
        self._axis_combo.currentIndexChanged.connect(self._apply_size)
        self._x_rev_chk.toggled.connect(self._apply_size)
        self._y_rev_chk.toggled.connect(self._apply_size)
        self._rows_spin.valueChanged.connect(self._apply_size)
        self._cols_spin.valueChanged.connect(self._apply_size)

        # Frequency label presets
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Frekvens-mall:"))
        norsok_btn = QPushButton("NORSOK Z-013  (AAA – E)")
        norsok_btn.setToolTip(
            "Fyll frekvensaxeln med NORSOK Z-013-etiketter:\n"
            "AAA (< 10⁻⁵/år)  →  E (> 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        norsok_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['AAA', 'AA', 'A', 'B', 'C', 'D', 'E'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        fscale_btn = QPushButton("F-skala  (F-1 – F5)")
        fscale_btn.setToolTip(
            "Fyll frekvensaxeln med internt F-skaleetiketter:\n"
            "F-1 (Otänkbar)  →  F5 (Frekvent > 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        fscale_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['F-1 – Otänkbar', 'F0 – Extremt sällan', 'F1 – Sällan',
             'F2 – Osannolik', 'F3 – Möjlig', 'F4 – Trolig', 'F5 – Frekvent'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        preset_row.addWidget(norsok_btn)
        preset_row.addWidget(fscale_btn)
        preset_row.addStretch()
        ml.addLayout(preset_row)

        save_matrix_btn = QPushButton("Spara riskmatris")
        save_matrix_btn.setIcon(_icon('save', 16, '#ffffff'))
        save_matrix_btn.setStyleSheet(
            "background:#2F5FD0; color:#fff; font-weight:bold; padding:4px 12px;")
        save_matrix_btn.clicked.connect(self._save_matrix)
        ml.addWidget(save_matrix_btn)

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
        splitter.addWidget(cat_tab)
        splitter.addWidget(matrix_tab)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 760])
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

        self._sheet_list = QListWidget()
        self._sheet_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._sheet_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sheet_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._sheet_list.model().rowsMoved.connect(self._on_sheets_reordered)
        self._sheet_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sheet_list.customContextMenuRequested.connect(self._sheet_context_menu)
        self._sheet_list.currentItemChanged.connect(self._on_sheet_selection_changed)
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
        self._nodes_table = QTableWidget(0, 3)
        self._nodes_table.setHorizontalHeaderLabels(["Nummer", "Namn", "Blad"])
        self._nodes_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.verticalHeader().setVisible(False)
        self._nodes_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._nodes_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._nodes_table.cellDoubleClicked.connect(self._on_nodes_table_double_clicked)
        nodes_layout.addWidget(self._nodes_table)
        tabs.addTab(nodes_widget, "Noder")

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_palette_ui()
        self._load_categories()
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
        self.refresh_sheets()
        self.refresh_nodes()

    # ── Blad (2026-08-17, moved from PIDManagementPanel, see NOTES.md) ──────
    def refresh_sheets(self):
        self._sheet_rev_combo.blockSignals(True)
        self._sheet_rev_combo.clear()
        self._sheet_rev_combo.addItem("(ingen)", None)
        for rev in self.db.get_revisions():
            self._sheet_rev_combo.addItem(rev['revision'] or f"Revision {rev['id']}", rev['id'])
        self._sheet_rev_combo.blockSignals(False)

        self._sheet_list.clear()
        for sheet in self.db.get_sheets():
            item = QListWidgetItem(
                f"{sheet['display_order'] + 1}. {sheet['sheet_name']}  "
                f"(PDF-sida {sheet['physical_page'] + 1})")
            item.setData(Qt.ItemDataRole.UserRole, sheet['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, sheet['revision_id'])
            nodes = self.db.nodes_on_page(sheet['physical_page'])
            if nodes:
                names = ', '.join(n['name'] or f"Nod {n['id']}" for n in nodes)
                item.setToolTip(f"Noder på detta blad: {names}")
            self._sheet_list.addItem(item)

    def _on_sheets_reordered(self, *_):
        ids = [self._sheet_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._sheet_list.count())]
        self.db.reorder_sheets(ids)
        self.refresh_sheets()

    def _rename_sheet(self):
        item = self._sheet_list.currentItem()
        if not item:
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
        selected = self._sheet_list.selectedItems()
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
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        menu = QMenu(self)
        if len(selected) == 1:
            menu.addAction(_icon('edit'), "Byt namn", self._rename_sheet)
        menu.addAction(_icon('delete'), "Ta bort", self._delete_sheets)
        menu.exec(self._sheet_list.viewport().mapToGlobal(pos))

    def _on_sheet_selection_changed(self, current, previous):
        self._sheet_rev_combo.blockSignals(True)
        if current is None:
            self._sheet_rev_combo.setCurrentIndex(0)
        else:
            rev_id = current.data(Qt.ItemDataRole.UserRole + 1)
            idx = self._sheet_rev_combo.findData(rev_id)
            self._sheet_rev_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sheet_rev_combo.blockSignals(False)

    def _on_sheet_revision_changed(self, _index):
        item = self._sheet_list.currentItem()
        if item is None:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        rev_id = self._sheet_rev_combo.currentData()
        self.db.set_sheet_revision(sheet_id, rev_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, rev_id)

    # ── Noder (2026-08-17, see NOTES.md "Ny Noder-flik") ─────────────────────
    def refresh_nodes(self):
        sheets_by_page = {s['physical_page']: s['sheet_name'] for s in self.db.get_sheets()}
        nodes = self.db.nodes()
        self._nodes_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            self._nodes_table.setItem(row, 0, QTableWidgetItem(str(node['id'])))
            name_item = QTableWidgetItem(node['name'] or f"Nod {node['id']}")
            name_item.setData(Qt.ItemDataRole.UserRole, node['id'])
            self._nodes_table.setItem(row, 1, name_item)
            pages = self.db.pages_for_node(node['id'])
            sheet_names = [sheets_by_page.get(p, f"sida {p + 1}") for p in pages]
            self._nodes_table.setItem(row, 2, QTableWidgetItem(', '.join(sheet_names)))

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

    def _load_matrix_ui(self):
        cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        self._last_built_cfg = None   # reset before blocking so _apply_size sees None
        # Block all signals that would trigger _apply_size while we populate controls
        _senders = (self._rows_spin, self._cols_spin, self._axis_combo,
                    self._x_rev_chk, self._y_rev_chk)
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

        freq_lbls = list(disp.get('x_labels', old.get('x_labels', FREQ_LABELS[:n_freq])))
        cons_lbls = list(disp.get('y_labels', old.get('y_labels', SEV_LABELS[:n_cons])))

        # Apply any manual text edits from display widgets by mapping each
        # widget directly to its data index (no reversal ambiguity).
        if self._x_label_edits:
            nc = len(self._x_label_edits)
            for c, e in enumerate(self._x_label_edits):
                data_c = (nc - 1 - c) if disp_x_rev else c
                txt = e.text().strip()
                if not txt:
                    continue
                if disp_freq_on_x:
                    if data_c < len(freq_lbls):
                        freq_lbls[data_c] = txt
                else:
                    if data_c < len(cons_lbls):
                        cons_lbls[data_c] = txt

        if self._y_label_edits:
            nr = len(self._y_label_edits)
            for r, e in enumerate(self._y_label_edits):
                data_r = r if disp_y_rev else (nr - 1 - r)
                txt = e.text().strip()
                if not txt:
                    continue
                if disp_freq_on_x:
                    if data_r < len(cons_lbls):
                        cons_lbls[data_r] = txt
                else:
                    if data_r < len(freq_lbls):
                        freq_lbls[data_r] = txt

        # Pad/trim to new dimensions
        while len(freq_lbls) < n_freq:
            freq_lbls.append(f'F{len(freq_lbls)-1}')
        while len(cons_lbls) < n_cons:
            cons_lbls.append(f'C{len(cons_lbls)+1}')
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
                try:    lbl2d[ci][fi]     = old_l[ci][fi]  or 'Låg'
                except: lbl2d[ci][fi]     = 'Låg'
                try:    fg_colors[ci][fi] = old_fg[ci][fi] or '#ffffff'
                except: fg_colors[ci][fi] = '#ffffff'
        # 2. Override with any user edits in the current buttons
        for _dr, row_btns in self._cell_buttons:
            for btn in row_btns:
                ci, fi = btn.row, btn.col
                if ci < n_cons and fi < n_freq:
                    if btn.color():    colors[ci][fi]    = btn.color()
                    if btn.label():    lbl2d[ci][fi]     = btn.label()
                    if btn.fg_color(): fg_colors[ci][fi] = btn.fg_color()

        new_cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':         new_xaxis,
            'x_reversed':     x_rev,
            'y_reversed':     y_rev,
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
                item.widget().deleteLater()
        self._cell_buttons       = []
        self._x_label_edits      = []
        self._y_label_edits      = []
        self._freq_boundary_edits = []
        self._sev_def_edits       = {}

        # Data always stored as [consequence_idx][frequency_idx]
        n_cons = cfg.get('rows', 5)    # consequence levels
        n_freq = cfg.get('cols', 7)    # frequency levels
        freq_labels = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        cons_labels = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
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
            col_lbls, row_lbls = freq_labels, cons_labels
            corner_txt = "C \\ F"
            col_tip = "Frekvensetikett (X-axel)\nExempel: F3 – Möjlig | 10-100 år"
            row_tip = "Konsekvensnivå (Y-axel)\nExempel: C4 – Allvarlig"
        else:
            n_dcols, n_drows = n_cons, n_freq   # cols=cons, rows=freq
            col_lbls, row_lbls = cons_labels, freq_labels
            corner_txt = "F \\ C"
            col_tip = "Konsekvensnivå (X-axel)\nExempel: C4 – Allvarlig"
            row_tip = "Frekvensetikett (Y-axel)\nExempel: F3 – Möjlig | 10-100 år"

        _hdr_style = ("font-size:8px; font-weight:bold;"
                      "border:1px solid #aaa; border-radius:0px;"
                      "background:#eef2f7; padding:0 3px;")

        # Corner
        corner = QLabel(corner_txt)
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setStyleSheet("font-size:9px; color:#555;")
        self._matrix_grid.addWidget(corner, 0, 0)

        # Column headers — apply x_rev: if reversed, col 0 shows the highest value
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            txt = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            e = QLineEdit(txt)
            e.setFixedSize(80, 28)
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e.setStyleSheet(_hdr_style)
            e.setToolTip(col_tip + "\nEtiketten uppdateras automatiskt när du ändrar gränsvärdet.")
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
            txt = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            ey = QLineEdit(txt)
            ey.setFixedSize(90, 40)
            ey.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ey.setStyleSheet(_hdr_style)
            ey.setToolTip(row_tip)
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
                    e.setFixedSize(80, 22)
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
                    # Connect boundary edit → auto-update adjacent axis labels
                    e.editingFinished.connect(
                        lambda _e=e, _c=c: self._sync_freq_label_from_boundary(_e, _c))
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
                    e.setFixedSize(70, 40)
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

        if not freq_on_x:
            # Consequence on X (columns) → category rows go BELOW the matrix
            # n_drows = n_freq; no boundary row exists (boundary is a column)
            base_row = n_drows + 1

            # Thin separator spanning all columns
            sep = QLabel("── Konsekvensdefinitioner ──")
            sep.setStyleSheet("font-size:8px; color:#888; padding:2px 4px;")
            sep.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._matrix_grid.addWidget(sep, base_row, 0, 1, n_dcols + 1)

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_row = base_row + 1 + cat_i

                cat_lbl = QLabel(cat['name'])
                cat_lbl.setStyleSheet(_cat_hdr_style)
                cat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                cat_lbl.setMinimumHeight(CONFIG['H_ROW_STD'])
                self._matrix_grid.addWidget(cat_lbl, cat_row, 0)

                for c in range(n_dcols):      # n_dcols = n_cons
                    data_c    = (n_dcols - 1 - c) if x_rev else c
                    sev_level = data_c + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QLineEdit(text)
                    e.setMinimumHeight(CONFIG['H_ROW_STD'])
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.editingFinished.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.text().strip()))
                    self._matrix_grid.addWidget(e, cat_row, c + 1)
                    self._sev_def_edits[(cat_id, sev_level)] = e
        else:
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
                cat_hdr.setMinimumWidth(130)
                cat_hdr.setWordWrap(True)
                self._matrix_grid.addWidget(cat_hdr, 0, cat_col)

                for r in range(n_drows):      # n_drows = n_cons
                    disp_r    = (n_drows - 1 - r) if not y_rev else r
                    sev_level = disp_r + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QTextEdit()
                    e.setPlainText(text)
                    e.setFixedWidth(130)
                    e.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                    e.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.textChanged.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.toPlainText().strip()))
                    self._matrix_grid.addWidget(e, r + 1, cat_col)
                    self._sev_def_edits[(cat_id, sev_level)] = e
                    row_cat_edits[r].append(e)

            for r in range(n_drows):
                if not row_cat_edits[r]:
                    continue
                needed = CONFIG['H_ROW_STD']
                for e in row_cat_edits[r]:
                    doc = e.document()
                    doc.setTextWidth(e.width())
                    needed = max(needed, int(doc.size().height()) + 8)
                self._y_label_edits[r].setFixedHeight(needed)
                for btn in self._cell_buttons[r][1]:
                    btn.setFixedHeight(needed)
                for e in row_cat_edits[r]:
                    e.setFixedHeight(needed)

    def _sync_freq_label_from_boundary(self, boundary_edit, col_idx: int):
        """Auto-update the frequency axis label(s) adjacent to the changed boundary."""
        try:
            val = float(boundary_edit.text().strip())
        except ValueError:
            return
        if val <= 0:
            return

        def _fmt(v):
            if v >= 1:       return f"{v:.3g}/år"
            if v >= 0.001:   return f"{v:.3g}/år"
            return f"{v:.2e}/år"

        # Collect all boundary values to compute ranges
        bvals = []
        for e in self._freq_boundary_edits:
            try:
                bvals.append(float(e.text()))
            except ValueError:
                bvals.append(None)

        def _label_for_col(c):
            """Return an auto-generated interval label for display column c."""
            left  = bvals[c-1] if c > 0 and c-1 < len(bvals) else None
            right = bvals[c]   if c < len(bvals) else None
            if left is None and right is not None:
                return f"< {_fmt(right)}"
            if left is not None and right is None:
                return f"≥ {_fmt(left)}"
            if left is not None and right is not None:
                return f"{_fmt(left)} – {_fmt(right)}"
            return ""

        # Update the two adjacent column labels (col_idx and col_idx+1)
        for affected_c in (col_idx, col_idx + 1):
            if 0 <= affected_c < len(self._x_label_edits):
                new_lbl = _label_for_col(affected_c)
                if new_lbl:
                    self._x_label_edits[affected_c].setText(new_lbl)
                    self._x_label_edits[affected_c].setCursorPosition(0)

    def _edit_cell(self, btn):
        """Click a cell → choose background color, label, and text color."""
        color = QColorDialog.getColor(QColor(btn.color()), self, "Välj bakgrundsfärg för cell")
        if not color.isValid():
            return
        label, ok = QInputDialog.getText(
            self, "Celltext", "Risknivå-etikett (t.ex. Låg, Medium, Hög, Kritisk):",
            text=btn.label())
        if not ok:
            return
        # Auto-suggest fg based on luminance; let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        current_fg = btn.fg_color() if btn.fg_color() else auto_fg
        fg_obj = QColorDialog.getColor(QColor(current_fg), self, "Välj textfärg")
        fg = fg_obj.name() if fg_obj.isValid() else current_fg
        btn.set_cell(color.name(), label.strip() or btn.label(), fg)

    def _save_matrix(self):
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

        # Axis labels: _x_label_edits are the column headers (whatever axis),
        # _y_label_edits are the row headers (reversed, highest at top)
        raw_col = [e.text().strip() for e in self._x_label_edits]
        raw_row = list(reversed([e.text().strip() for e in self._y_label_edits]))  # low→high

        if freq_on_x:
            # X=freq columns, Y=cons rows
            x_labels = raw_col or [f'F{i-1}' for i in range(n_freq)]
            y_labels = raw_row or [f'C{i+1}' for i in range(n_cons)]
        else:
            # X=cons columns, Y=freq rows
            y_labels = raw_col or [f'C{i+1}' for i in range(n_cons)]
            x_labels = raw_row or [f'F{i-1}' for i in range(n_freq)]

        # Pad/trim to correct lengths
        while len(x_labels) < n_freq: x_labels.append(f'F{len(x_labels)-1}')
        while len(y_labels) < n_cons: y_labels.append(f'C{len(y_labels)+1}')
        x_labels = x_labels[:n_freq]
        y_labels = y_labels[:n_cons]

        cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':      x_axis,
            'x_reversed':  self._x_rev_chk.isChecked(),
            'y_reversed':  self._y_rev_chk.isChecked(),
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

        cfg = _normalise_matrix(cfg)   # ensure consistent before saving
        self.db.set_risk_matrix(cfg)
        # set_risk_matrix() automatically invalidates the cache; reload from DB
        _risk_matrix_cache.reload_from_db()
        QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
        self.matrix_changed.emit()

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

    def _cat_rename(self):
        from PyQt6.QtWidgets import QInputDialog
        item = self._cat_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_category(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._load_categories()
            self._apply_size()

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


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

class SettingsPanel(QWidget):
    """P&ID-scoped and tag-recognition settings. Used to also host Projekt/
    Deltagare/Riskmatris & Kategorier/Standardorsaker — those four moved out
    into their own top-level HAZOPPreparationPanel (2026-08-17, see NOTES.md)
    since Anton wanted them front-and-center in the nav rail rather than
    buried as tabs here. Keeps its own `matrix_changed` (TagDatabasePanel's
    settings_changed still forwards into it, unrelated to the risk matrix
    itself) — see HAZOPPreparationPanel's own docstring for why this signal
    is duplicated across both panels rather than shared."""

    matrix_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        tabs = QTabWidget()
        self._tabs = tabs   # kept as an attribute for testability (tabText() lookups)
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: P&ID-inställningar ───────────────────────────────────────────
        # Renamed from "P&ID" (2026-08-11, user request: "Fliken PID borde
        # kunna ändras till något mer generiskt för inställning" / "Byt namn
        # + lägg till OCR/sid-inställningar"). "P&ID-inställningar" was
        # chosen over a fully generic name like "Analys" or "Inställningar"
        # because this tab already lives inside a settings screen next to
        # "Tagdatabas" and "Identifierade objekt" (both P&ID-specific DATA
        # views) — a bare "Analys" would read as ambiguous next to those,
        # while "P&ID-inställningar" keeps the P&ID scope clear but no
        # longer implies (like the old "P&ID" name did) that tag-stripping
        # is the only setting that belongs here.
        pid_tab = QWidget()
        pid_l = QVBoxLayout(pid_tab)
        pid_l.setContentsMargins(16, 16, 16, 16)
        pid_l.setSpacing(12)

        tag_grp = QGroupBox("Tagg-identifiering")
        tag_gl = QVBoxLayout(tag_grp)
        tag_gl.setSpacing(6)

        self._strip_spaces_chk = QCheckBox(
            "Ta bort mellanslag i tagg-nummer  (t.ex. \"P 101\" → \"P101\")")
        self._strip_spaces_chk.setToolTip(
            "När ett tagg-nummer identifieras via klick eller gummiband på P&ID\n"
            "tas alla mellanslag bort automatiskt innan det fylls i tag-fältet.")
        self._strip_spaces_chk.toggled.connect(
            lambda on: self.db.set_config('tag_strip_spaces', '1' if on else '0'))
        tag_gl.addWidget(self._strip_spaces_chk)

        pid_l.addWidget(tag_grp)

        # ── OCR-standardval ───────────────────────────────────────────────
        # Lets the user skip the per-scan "Använd OCR?" Yes/No prompt shown
        # by "🔍 Skanna P&ID" (EquipmentPanel._scan, hazop.py) and "📋
        # Analysera P&ID" (PIDPanel._analyze_pid, pid_viewer.py) by picking
        # a fixed default engine ahead of time. Wired into both scan entry
        # points via pid_viewer.resolve_ocr_scan_choice() — this is NOT a
        # dead setting, it actually changes scan behaviour.
        ocr_grp = QGroupBox("OCR-standardval")
        ocr_gl = QVBoxLayout(ocr_grp)
        ocr_gl.setSpacing(6)
        ocr_lbl = QLabel(
            "Motor att använda automatiskt vid P&ID-skanning\n"
            "(🔍 Skanna P&ID / 📋 Analysera P&ID), utan att fråga varje gång:")
        ocr_gl.addWidget(ocr_lbl)
        self._ocr_default_combo = QComboBox()
        self._ocr_default_combo.addItem("Fråga varje gång (standard)", 'ask')
        self._ocr_default_combo.addItem("Automatiskt — bästa tillgängliga motor", 'auto')
        _ocr_st = ocr_status()
        if _ocr_st.get('rapidocr'):
            self._ocr_default_combo.addItem("RapidOCR", 'rapidocr')
        if _ocr_st.get('tesseract'):
            self._ocr_default_combo.addItem("Tesseract", 'tesseract')
        if _ocr_st.get('easyocr'):
            self._ocr_default_combo.addItem("EasyOCR", 'easyocr')
        self._ocr_default_combo.setToolTip(
            "Styr om/vilken OCR-motor som används automatiskt vid P&ID-skanning —\n"
            "hoppar då över Ja/Nej-frågan om OCR för den körningen.\n"
            "\"Fråga varje gång\" behåller nuvarande beteende.")
        self._ocr_default_combo.currentIndexChanged.connect(
            lambda: self.db.set_config(
                'ocr_default_engine', self._ocr_default_combo.currentData()))
        ocr_gl.addWidget(self._ocr_default_combo)
        pid_l.addWidget(ocr_grp)

        # ── Sid-orientering ───────────────────────────────────────────────
        # Investigated first (per process convention) whether an
        # auto-detection system already exists: it does not — the app
        # always just follows the PDF's own /Rotate page attribute
        # (fitz_page.rotation_matrix, see PIDPanel._highlight_tags in
        # pid_viewer.py), there is no heuristic "guess the orientation"
        # layer to conflict with. This setting is therefore stored as a
        # forward-looking override/hint only; it is NOT yet read by the
        # rendering/scanning pipeline (that would mean threading an
        # override through PDF rendering, OCR preprocessing, and the
        # multi-process scan workers — out of scope for this change; see
        # NOTES.md "Kända begränsningar" for this known limitation).
        orient_grp = QGroupBox("Sid-orientering")
        orient_gl = QVBoxLayout(orient_grp)
        orient_gl.setSpacing(6)
        orient_lbl = QLabel(
            "Förvalt antagande om sidans orientering vid rendering/analys\n"
            "av P&ID-sidor. OBS: sparas som inställning men styr ännu inte\n"
            "den faktiska renderingen/analysen (appen använder idag alltid\n"
            "PDF-filens egen rotationsflagga automatiskt) — känd begränsning,\n"
            "se NOTES.md.")
        orient_lbl.setWordWrap(True)
        orient_gl.addWidget(orient_lbl)
        self._page_orientation_combo = QComboBox()
        self._page_orientation_combo.addItem(
            "Använd PDF:ens egen rotation (standard)", 'auto')
        self._page_orientation_combo.addItem("Tvinga liggande", 'landscape')
        self._page_orientation_combo.addItem("Tvinga stående", 'portrait')
        self._page_orientation_combo.currentIndexChanged.connect(
            lambda: self.db.set_config(
                'pid_page_orientation_hint', self._page_orientation_combo.currentData()))
        orient_gl.addWidget(self._page_orientation_combo)
        pid_l.addWidget(orient_grp)

        pid_l.addStretch()
        tabs.addTab(pid_tab, "P&ID-inställningar")

        # ── Tab: Tagdatabas ───────────────────────────────────────────────────
        self._tag_db_panel = TagDatabasePanel(self.db)
        self._tag_db_panel.settings_changed.connect(self.matrix_changed.emit)
        tabs.addTab(self._tag_db_panel, "Tagdatabas")

        # ── Tab: Identifierade objekt ─────────────────────────────────────────
        self.analysis_panel = PIDAnalysisPanel(self.db)
        tabs.addTab(self.analysis_panel, "Identifierade objekt")

        # ── Tab: Standardobjekt ───────────────────────────────────────────────
        self._std_objects_panel = StandardObjectsSettingsPanel(self.db)
        tabs.addTab(self._std_objects_panel, "Standardobjekt")

        # ── Tab: Smart igenkänning ────────────────────────────────────────────
        self._tag_memory_panel = TagMemoryPanel(self.db)
        tabs.addTab(self._tag_memory_panel, _icon('brain'), "Smart igenkänning")
        tabs.currentChanged.connect(
            lambda i: self._tag_memory_panel.refresh()
            if tabs.widget(i) is self._tag_memory_panel else None)

        self._load_all()

    def _load_all(self):
        self._strip_spaces_chk.setChecked(
            self.db.get_config('tag_strip_spaces', '1') == '1')

        idx = self._ocr_default_combo.findData(self.db.get_config('ocr_default_engine', 'ask'))
        if idx >= 0:
            self._ocr_default_combo.setCurrentIndex(idx)
        idx = self._page_orientation_combo.findData(
            self.db.get_config('pid_page_orientation_hint', 'auto'))
        if idx >= 0:
            self._page_orientation_combo.setCurrentIndex(idx)

    def refresh_tag_memory(self):
        """Refresh the Smart igenkänning tab so newly learned tags show up."""
        self._tag_memory_panel.refresh()


class PIDManagementPanel(QWidget):
    """PID revision history and sheet reordering panel."""
    sheets_changed = pyqtSignal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Revision history ───────────────────────────────────────────
        rev_widget = QWidget()
        rev_layout = QVBoxLayout(rev_widget)
        rev_layout.setContentsMargins(8, 8, 8, 8)
        rev_layout.setSpacing(6)

        rev_hdr = QHBoxLayout()
        rev_hdr.addWidget(QLabel("Revisionshistorik:"))
        rev_hdr.addStretch()
        clear_all_btn = QPushButton("Rensa samtliga P&ID och all data")
        clear_all_btn.setIcon(_icon('delete'))
        clear_all_btn.setStyleSheet(
            "QPushButton{color:#fff;background:#C62828;border-radius:4px;padding:4px 10px;}"
            "QPushButton:hover{background:#B71C1C;}")
        clear_all_btn.clicked.connect(self._clear_all_pid)
        rev_hdr.addWidget(clear_all_btn)
        rev_layout.addLayout(rev_hdr)

        self._rev_table = QTableWidget(0, 4)
        self._rev_table.setHorizontalHeaderLabels(['Revision', 'Anteckningar', 'Datum', 'PDF-fil'])
        self._rev_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._rev_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self._rev_table.setColumnWidth(0, 120)
        self._rev_table.setColumnWidth(2, 130)
        self._rev_table.setColumnWidth(3, 180)
        self._rev_table.verticalHeader().setVisible(False)
        self._rev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._rev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._rev_table.setAlternatingRowColors(True)
        self._rev_table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        rev_layout.addWidget(self._rev_table)
        tabs.addTab(rev_widget, "Revisioner")

        # "Blad" (sheet ordering/rename/delete) moved out to
        # HAZOPPreparationPanel (2026-08-17, see NOTES.md) — this panel now
        # only manages revision history. sheets_changed is still emitted
        # from here by _clear_all_pid (which also wipes sheets), so the
        # moved Blad list can still react to a full-clear from this tab.
        self.refresh()

    def refresh(self):
        self._rev_table.setRowCount(0)
        for rev in self.db.get_revisions():
            r = self._rev_table.rowCount()
            self._rev_table.insertRow(r)
            self._rev_table.setItem(r, 0, QTableWidgetItem(rev['revision'] or ''))
            self._rev_table.setItem(r, 1, QTableWidgetItem(rev['notes'] or ''))
            self._rev_table.setItem(r, 2, QTableWidgetItem(rev['created_at'] or ''))
            fname = Path(rev['pdf_path']).name if rev['pdf_path'] else ''
            self._rev_table.setItem(r, 3, QTableWidgetItem(fname))
            self._rev_table.setRowHeight(r, 24)

    def _clear_all_pid(self):
        count = len(self.db.get_revisions())
        n_sheets = self.db.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]
        box = QMessageBox(self)
        box.setWindowTitle("Rensa samtliga P&ID och all data")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"Det finns <b>{count} P&ID-revision{'er' if count != 1 else ''}</b> "
            f"och <b>{n_sheets} blad</b> inlagda.\n\n"
            f"Vill du permanent ta bort <b>alla</b> P&ID, blad, markeringar och kopplingar?")
        box.setInformativeText(
            "Denna åtgärd kan inte ångras. HAZOP-analysen (noder, orsaker, konsekvenser) "
            "berörs inte, men alla positioner på P&ID-vyn raderas.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("Rensa allt")
        box.button(QMessageBox.StandardButton.No).setText("Avbryt")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.db.clear_all_pid_data()
        self.refresh()
        self.sheets_changed.emit()


class StudyManagementPanel(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Studiehantering")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Statistics ─────────────────────────────────────────────────
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(8)

        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet(
            "background:#f0f4f8; border:1px solid #ccc; border-radius:6px; padding:10px;")
        stats_layout.addWidget(self._stats_lbl)

        bar = QHBoxLayout()
        refresh_btn = QPushButton("Uppdatera")
        refresh_btn.setIcon(_icon('refresh'))
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn)
        backup_btn = QPushButton("Skapa säkerhetskopia nu")
        backup_btn.setIcon(_icon('save'))
        backup_btn.setToolTip(
            "Automatiska säkerhetskopior sparas redan löpande i hazop_backups/ — "
            "denna knapp tvingar fram en omedelbar kopia, utan att vänta på nästa automatiska tillfälle.")
        backup_btn.clicked.connect(self._backup_now)
        bar.addWidget(backup_btn)
        bar.addStretch()
        stats_layout.addLayout(bar)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ['Nod', 'Orsak', 'L', 'Konsekvens', 'S', 'Risknivå', 'Kategori', 'Safeguards'])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in [2, 3, 4, 5, 6, 7]:
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 100)
        self._table.setColumnWidth(2, 28)
        self._table.setColumnWidth(4, 28)
        self._table.setColumnWidth(5, 80)
        self._table.setColumnWidth(6, 80)
        self._table.setColumnWidth(7, 150)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        stats_layout.addWidget(self._table)
        tabs.addTab(stats_widget, "Statistik")

        # ── Tab 1: PID management ─────────────────────────────────────────────
        self._pid_mgmt = PIDManagementPanel(db)
        tabs.addTab(self._pid_mgmt, "PID-hantering")

        self.refresh()

    def _backup_now(self):
        dst = self.db._write_backup(startup=True)   # startup=True bypasses the throttle
        if dst is not None:
            QMessageBox.information(self, "Säkerhetskopia skapad",
                f"Sparade en säkerhetskopia:\n{dst}")
        else:
            QMessageBox.warning(self, "Säkerhetskopiering misslyckades",
                "Kunde inte skapa säkerhetskopian. Kontrollera att det finns "
                "diskutrymme och skrivbehörighet i projektmappen.")

    def refresh(self):
        s = self.db.stats()
        self._stats_lbl.setText(
            f"  Noder: <b>{s['nodes']}</b>   |   Orsaker: <b>{s['causes']}</b>   |   "
            f"Konsekvenser: <b>{s['consequences']}</b>   |   Safeguards: <b>{s['safeguards']}</b>   |   "
            f"Öppna åtgärder: <b>{s['open_actions']}</b>")
        self._stats_lbl.setTextFormat(Qt.TextFormat.RichText)

        self._table.setRowCount(0)
        for row in self.db.all_data():
            level, bg, fg = risk_info(row['likelihood'], row['severity'])
            r = self._table.rowCount()
            self._table.insertRow(r)

            def _c(t, center=False):
                item = QTableWidgetItem(str(t))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if center else
                                      Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                return item

            self._table.setItem(r, 0, _c(row['node_name']))
            self._table.setItem(r, 1, _c(row['cause']))
            self._table.setItem(r, 2, _c(row['likelihood'], True))
            self._table.setItem(r, 3, _c(row['consequence']))
            self._table.setItem(r, 4, _c(row['severity'], True))
            risk_item = QTableWidgetItem(f"{level}\nF={row['likelihood']} C={row['severity']}")
            risk_item.setBackground(QBrush(QColor(bg)))
            risk_item.setForeground(QBrush(QColor(fg)))
            risk_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(r, 5, risk_item)
            self._table.setItem(r, 6, _c(row['category']))
            sg_text = '; '.join(
                f"{s['description']}{'(RRF' + str(s['rrf']) + ')' if s['rrf'] > 1 else ''}"
                for s in row['safeguards']) or '—'
            self._table.setItem(r, 7, _c(sg_text))
            self._table.setRowHeight(r, 28)

    def refresh_pid(self):
        self._pid_mgmt.refresh()


# Keep old name as alias so any remaining references don't crash
AdminPanel = StudyManagementPanel
