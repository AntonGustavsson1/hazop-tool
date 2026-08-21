#!/usr/bin/env python3
"""TagMemoryPanel -- split out of settings_panels.py 2026-08-21, see NOTES.md "Dela upp settings_panels.py"."""

import re
import json
from pathlib import Path
from functools import partial

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDateEdit,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
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


