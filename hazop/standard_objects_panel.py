#!/usr/bin/env python3
"""StandardObjectsSettingsPanel -- split out of settings_panels.py 2026-08-21, see NOTES.md "Dela upp settings_panels.py"."""

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


