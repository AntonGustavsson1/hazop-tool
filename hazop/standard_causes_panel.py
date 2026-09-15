#!/usr/bin/env python3
"""StandardCausesSettingsPanel -- split out of settings_panels.py 2026-08-21, see NOTES.md "Dela upp settings_panels.py"."""

import re
import json
from pathlib import Path
from functools import partial

from PyQt6.QtWidgets import (
    QAbstractItemView, QColorDialog, QComboBox, QDateEdit,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStyledItemDelegate, QTableWidget,
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
import spellcheck


class _SpellCheckListItemDelegate(QStyledItemDelegate):
    """Swaps in a spellcheck-aware QLineEdit for inline QListWidget item
    editing (2026-09-07, Fas 5, see NOTES.md "Stavningskontroll") -- Qt's
    own default delegate would otherwise always create a plain QLineEdit
    for this. A SpellCheckLineEdit IS a QLineEdit, so every other default
    delegate behavior (reading/writing .text() via Qt's editor user
    property) keeps working unchanged; only createEditor() differs."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    def createEditor(self, parent, option, index):
        context = getattr(self._panel, 'spellcheck_context', None)
        if context is None:
            return super().createEditor(parent, option, index)
        return spellcheck.SpellCheckLineEdit(parent, context=context)


class StandardCausesSettingsPanel(QWidget):
    """Editable hierarchy: Nodtyp → Objekt → Orsak → Avvikelser."""

    def __init__(self, db, spellcheck_context=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.spellcheck_context = spellcheck_context
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
        self._nodetype_list.currentRowChanged.connect(lambda _row: self._load_objects())
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
        # _load_node_types() is deferred until all dependent lists exist.

        # ── Col 1: Objekt ─────────────────────────────────────────────────────
        c1 = QVBoxLayout()
        self._obj_lbl = QLabel("<b>Objekt</b>")
        c1.addWidget(self._obj_lbl)
        self._obj_list = QListWidget()
        self._obj_list.currentRowChanged.connect(self._on_obj_sel)
        c1.addWidget(self._obj_list)
        c1b = QHBoxLayout()
        for icon, slot in (('+', self._add_obj), ('−', self._del_obj),
                           ('↑', lambda: self._move_obj(-1)), ('↓', lambda: self._move_obj(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c1b.addWidget(b)
        c1b.addStretch(); c1.addLayout(c1b)

        # ── Col 2: Orsak ──────────────────────────────────────────────────────
        c2 = QVBoxLayout()
        self._cause_lbl = QLabel("<b>Orsak</b>")
        c2.addWidget(self._cause_lbl)
        self._cause_list = QListWidget()
        self._cause_list.setItemDelegate(_SpellCheckListItemDelegate(self, self._cause_list))
        self._cause_list.currentRowChanged.connect(self._on_cause_sel)
        self._cause_list.itemChanged.connect(self._on_cause_changed)
        c2.addWidget(self._cause_list)
        # Frequency field for the selected reusable cause
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
        c2.addLayout(freq_row)
        c2b = QHBoxLayout()
        for icon, slot in (('+', self._add_cause), ('−', self._del_cause),
                           ('↑', lambda: self._move_cause(-1)), ('↓', lambda: self._move_cause(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c2b.addWidget(b)
        c2b.addStretch(); c2.addLayout(c2b)
        btn_sync = QPushButton("Synka frekvenser →")
        btn_sync.setToolTip("Uppdaterar frekvensen på alla orsaker kopplade till standardorsaker.")
        btn_sync.clicked.connect(self._sync_freqs)
        c2.addWidget(btn_sync)

        # ── Col 3: Avvikelse ──────────────────────────────────────────────────
        c3 = QVBoxLayout()
        self._dev_lbl = QLabel("<b>Avvikelser</b>")
        c3.addWidget(self._dev_lbl)
        self._dev_list = QListWidget()
        self._dev_list.setDragEnabled(True)
        self._dev_list.itemChanged.connect(self._on_deviation_item_changed)
        # Keep copying a deviation to another node type available.  The same
        # list now also carries a checkbox for the selected cause's scope.
        def _dev_list_mime_data(items, _list=self._dev_list):
            md = QMimeData()
            if items:
                dev_id = items[0].data(Qt.ItemDataRole.UserRole)
                md.setText(f'hzp:stddev:{dev_id}')
            return md
        self._dev_list.mimeData = _dev_list_mime_data
        c3.addWidget(self._dev_list)
        c3b = QHBoxLayout()
        for icon, slot in (('+', self._add_dev), ('−', self._del_dev),
                           ('↑', lambda: self._move_dev(-1)), ('↓', lambda: self._move_dev(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c3b.addWidget(b)
        c3b.addStretch(); c3.addLayout(c3b)
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
        btn_exp_xlsx = QPushButton("↑ Exportera Excel")
        btn_exp_xlsx.setToolTip(
            "Exportera samtliga standardavvikelser (grupperade per objekttyp) "
            "till en redigerbar Excel-fil")
        btn_exp_xlsx.clicked.connect(self._export_library_excel)
        c3.addWidget(btn_exp_xlsx)

        layout.addLayout(c0, 1)
        layout.addLayout(c1, 1)
        layout.addLayout(c2, 1)
        layout.addLayout(c3, 1)
        self._load_node_types()   # cascades into _load_objects() via currentRowChanged

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
        cause_id = self._current_cause_id()
        if nt_id is None:
            self._loading = False
            return
        if cause_id:
            rows = self.db.standard_cause_group_deviations(cause_id, nt_id)
        else:
            rows = self.db.standard_deviations_for_node_type(nt_id)
        cause_item = self._cause_list.currentItem()
        cause_name = cause_item.data(Qt.ItemDataRole.UserRole + 1) if cause_item else ''
        self._dev_lbl.setText(
            f"<b>Avvikelser</b>{' — ' + cause_name if cause_name else ''}")
        for d in rows:
            item = QListWidgetItem(d['description'])
            item.setData(Qt.ItemDataRole.UserRole, d['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, d['description'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            if cause_id:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if d.get('applicable') else Qt.CheckState.Unchecked)
            self._dev_list.addItem(item)
        self._loading = False
        self._dev_list.setCurrentRow(max(0, min(cur, self._dev_list.count()-1)))

    def _current_dev_id(self):
        item = self._dev_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_objects(self):
        self._loading = True
        cur = self._obj_list.currentRow()
        self._obj_list.clear()
        nt_id = self._current_node_type_id()
        if nt_id is None:
            self._loading = False
            return
        rows = self.db.all_objects_with_cause_group_counts(nt_id)
        for r in rows:
            label = r['name']
            n = r['n_causes']
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

    def _load_causes(self):
        self._loading = True
        cur = self._cause_list.currentRow()
        self._cause_list.clear()
        nt_id = self._current_node_type_id()
        obj_id = self._current_obj_id()
        if nt_id is None or obj_id is None:
            self._cause_lbl.setText("<b>Orsak</b>")
            self._loading = False
            self._load_deviations()
            return
        obj_item = self._obj_list.currentItem()
        obj_name = obj_item.data(Qt.ItemDataRole.UserRole + 1) if obj_item else ''
        self._cause_lbl.setText(f"<b>Orsak</b> — {obj_name}")
        for c in self.db.standard_cause_groups_for_object(nt_id, obj_id):
            freq = c['frequency']
            label = c['description']
            if freq is not None:
                label += f"  [{freq:g}/år]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, c['id'])
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
        self._load_deviations()

    # ── Slot chains ───────────────────────────────────────────────────────────
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
        self._load_deviations()

    def _current_cause_id(self):
        item = self._cause_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_cause_changed(self, item):
        if self._loading:
            return
        cause_id = item.data(Qt.ItemDataRole.UserRole)
        if not cause_id:
            return
        description = re.sub(r'\s*\[[^\]]+\]\s*$', '', item.text()).strip()
        if not description:
            description = item.data(Qt.ItemDataRole.UserRole + 1)
            self._loading = True
            item.setText(description)
            self._loading = False
            return
        if description != item.data(Qt.ItemDataRole.UserRole + 1):
            self.db.update_standard_cause_group(cause_id, description=description)
            item.setData(Qt.ItemDataRole.UserRole + 1, description)
            freq = item.data(Qt.ItemDataRole.UserRole + 2)
            item.setText(f"{description}  [{freq:g}/år]" if freq is not None else description)
            self._load_deviations()

    def _on_deviation_item_changed(self, item):
        if self._loading:
            return
        deviation_id = item.data(Qt.ItemDataRole.UserRole)
        if not deviation_id:
            return
        description = item.text().strip()
        old_description = item.data(Qt.ItemDataRole.UserRole + 1)
        if description and description != old_description:
            self.db.update_standard_deviation(deviation_id, description)
            item.setData(Qt.ItemDataRole.UserRole + 1, description)
        cause_id = self._current_cause_id()
        if cause_id and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            self.db.set_standard_cause_group_deviation(
                cause_id, deviation_id, item.checkState() == Qt.CheckState.Checked)

    def _save_freq(self):
        """Save the edited frequency for the currently selected standard cause."""
        item = self._cause_list.currentItem()
        if not item: return
        cause_id = item.data(Qt.ItemDataRole.UserRole)
        if cause_id is None: return
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
        self.db.update_standard_cause_group(cause_id, frequency=freq)
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
        self._load_deviations()
        for row in range(self._dev_list.count()):
            item = self._dev_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == new_id:
                self._dev_list.setCurrentItem(item)
                self._dev_list.editItem(item)
                break

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

    # ── Object CRUD ──────────────────────────────────────────────────────────
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
        node_type_id = self._current_node_type_id()
        obj_id = self._current_obj_id()
        if node_type_id is None or obj_id is None:
            return
        new_id = self.db.add_standard_cause_group(node_type_id, obj_id, 'Ny orsak')
        self._load_causes()
        for row in range(self._cause_list.count()):
            item = self._cause_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == new_id:
                self._cause_list.setCurrentItem(item)
                self._cause_list.editItem(item)
                break
        self._load_objects()

    def _del_cause(self):
        item = self._cause_list.currentItem()
        if not item: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_:
            self.db.delete_standard_cause_group(id_)
            self._load_causes()
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
        self.db.reorder_standard_cause_groups(ids)

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

    # ── Excel export (2026-08-26): editable, re-importable spreadsheet ────────
    def _export_library_excel(self):
        """Export every standard cause, grouped by object type (Objekttyp),
        to a plain flat .xlsx table -- one data row per standard cause, no
        merged cells -- so it stays trivially sortable/filterable in Excel
        AND re-importable later (a future importer only needs to match rows
        by their own (Objekttyp, Avvikelse, Orsak) text, the same identity
        JSON import already matches on in _import_library above -- no
        hidden id columns needed)."""
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exportera standardavvikelser till Excel',
            'standardavvikelser.xlsx', 'Excel-filer (*.xlsx)')
        if not path:
            return
        if not path.lower().endswith('.xlsx'):
            path += '.xlsx'
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = 'Standardavvikelser'
        headers = ['Objekttyp', 'Avvikelse', 'Orsak', 'Frekvens (/år)']
        ws.append(headers)
        header_fill = PatternFill('solid', fgColor='1F4E79')
        for cell in ws[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='left')

        deviations = self.db.standard_deviations()
        band_fill = PatternFill('solid', fgColor='EEF2F7')
        band = False
        row = 2
        for obj in self.db.standard_objects():
            obj_rows = []
            for dev in deviations:
                for c in self.db.standard_causes_for_object(dev['id'], obj['id']):
                    obj_rows.append((dev, c))
            if not obj_rows:
                # No causes at all yet for this object type -- skip it
                # rather than emitting an empty group; nothing to edit.
                continue
            band = not band
            for dev, c in obj_rows:
                freq = c.get('frequency')
                ws.append([obj['name'], dev['description'], c['description'],
                           freq if freq is not None else None])
                if band:
                    for cell in ws[row]:
                        cell.fill = band_fill
                row += 1

        widths = {1: 28, 2: 22, 3: 60, 4: 16}
        for col, w in widths.items():
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions

        info = wb.create_sheet('Läs mig')
        info.column_dimensions['A'].width = 100
        for text in (
            'Denna flik listar programmets samtliga standardavvikelser, grupperade per objekttyp.',
            '',
            'Kolumner:',
            '  Objekttyp -- måste stavas exakt som i programmets objekttypslista '
            '(Inställningar -- Standardobjekt) för att kunna matchas vid en framtida import.',
            '  Avvikelse -- t.ex. "Högt flöde", "Lågt tryck".',
            '  Orsak -- fritext, en rad per orsak.',
            '  Frekvens (/år) -- lämna tom om okänd.',
            '',
            'Radera eller redigera rader fritt, eller lägg till nya längst ned i valfri grupp.',
            'Filen är ett rent tabellformat (inga sammanslagna celler) för att gå att läsa in igen.',
        ):
            info.append([text])
        info['A1'].font = Font(bold=True)
        info['A3'].font = Font(bold=True)

        wb.save(path)
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
        # Importing a library is one deliberate operation from the user's
        # perspective, although the existing DB helpers commit each row.
        with self.db.history_group():
            for obj_name in data.get('objects', []):
                if not self.db.conn.execute(
                        "SELECT id FROM standard_objects WHERE name=?", (obj_name,)).fetchone():
                    self.db.add_standard_object(obj_name); added_objs += 1
            for dev_d in data.get('deviations', []):
                dev_row = self.db.conn.execute(
                    "SELECT id FROM standard_deviations WHERE description=? AND active=1",
                    (dev_d['description'],)).fetchone()
                if not dev_row:
                    dev_id = self.db.add_standard_deviation(dev_d['description'])
                    added_devs += 1
                else:
                    dev_id = dev_row[0]
                for c in dev_d.get('causes', []):
                    obj_id = c.get('object_id')
                    if not self.db.conn.execute(
                            "SELECT id FROM standard_causes WHERE deviation_id=? AND description=? AND active=1",
                            (dev_id, c['description'])).fetchone():
                        self.db.add_standard_cause_with_object(dev_id, obj_id or 0, c['description'])
                        added_causes += 1
            self.db.commit()
        self._load_deviations()
        QMessageBox.information(self, 'Importerat',
            f'Lagt till: {added_devs} avvikelser, {added_causes} orsaker, {added_objs} objekt.')
