#!/usr/bin/env python3
"""Equipment register panel and its dialogs/models — split out of hazop.py
2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i fler
filer"."""

import re
import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QStyle, QStyledItemDelegate,
    QStyleOptionButton, QTableView, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QEvent, QAbstractTableModel, QModelIndex,
    QSortFilterProxyModel,
)
from PyQt6.QtGui import QBrush, QColor, QFont

from constants import CONFIG
from database import Database, freq_to_f_level, risk_info
from pid_viewer import (
    _icon, apply_scan_result_to_equipment_catalog, EquipmentMarkerReviewDialog,
    HAS_PYMUPDF, KNOWN_PREFIXES, PageProgressDialog, ParallelEquipmentAnalysisWorker,
    ParallelTagScanWorker, resolve_ocr_scan_choice, upsert_identified_tags_from_scan,
)
from ui_helpers import (
    _EQ_TYPE_ITEMS, _equipment_type_options, _make_tag_completer,
    add_mini_popup_close_button,
)

class ComponentEditorPanel(QWidget):
    """Settings panel for managing component types and failure modes."""

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cur_comp_id = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ── Left: component type list ─────────────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("Komponenttyper:"))
        self._comp_list = QListWidget()
        self._comp_list.currentItemChanged.connect(self._on_comp_selected)
        left.addWidget(self._comp_list)

        comp_btns = QHBoxLayout()
        btn_add_c  = QPushButton("+ Lägg till")
        btn_ren_c  = QPushButton("Byt namn")
        btn_ren_c.setIcon(_icon('edit'))
        btn_del_c  = QPushButton("Ta bort")
        btn_del_c.setIcon(_icon('delete'))
        btn_add_c.clicked.connect(self._comp_add)
        btn_ren_c.clicked.connect(self._comp_rename)
        btn_del_c.clicked.connect(self._comp_delete)
        for b in [btn_add_c, btn_ren_c, btn_del_c]:
            comp_btns.addWidget(b)
        left.addLayout(comp_btns)
        layout.addLayout(left, 1)

        # ── Right: failure modes table ────────────────────────────────────────
        right = QVBoxLayout()
        right.addWidget(QLabel("Felmoder för vald komponent:"))

        self._mode_table = QTableWidget(0, 3)
        self._mode_table.setHorizontalHeaderLabels(
            ['Beskrivning', 'Frekvens (/år)', 'F-nivå (auto)'])
        h = self._mode_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._mode_table.setColumnWidth(1, 110)
        self._mode_table.setColumnWidth(2, 90)
        self._mode_table.verticalHeader().setVisible(False)
        self._mode_table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;"
            "font-weight:600;padding:3px;}")
        self._mode_table.cellChanged.connect(self._on_mode_cell)
        right.addWidget(self._mode_table)

        mode_btns = QHBoxLayout()
        btn_add_m = QPushButton("+ Lägg till felmod")
        btn_del_m = QPushButton("Ta bort vald")
        btn_del_m.setIcon(_icon('delete'))
        btn_add_m.clicked.connect(self._mode_add)
        btn_del_m.clicked.connect(self._mode_delete)
        mode_btns.addWidget(btn_add_m)
        mode_btns.addWidget(btn_del_m)
        mode_btns.addStretch()
        right.addLayout(mode_btns)

        freq_note = QLabel(
            "Frekvens i händelser/år.  Exempel: 0.05/år = en gång per 20 år → F=3 (10-100 år)\n"
            "F-nivån beräknas automatiskt från frekvensgränserna i riskmatrisen.")
        freq_note.setStyleSheet("color:#666; font-size:10px;")
        right.addWidget(freq_note)

        layout.addLayout(right, 2)
        self._refresh_comp_list()

    # ── Component list ────────────────────────────────────────────────────────

    def _refresh_comp_list(self):
        self._comp_list.blockSignals(True)
        self._comp_list.clear()
        for ct in self.db.component_types():
            item = QListWidgetItem(ct['name'])
            item.setData(Qt.ItemDataRole.UserRole, ct['id'])
            self._comp_list.addItem(item)
        self._comp_list.blockSignals(False)
        if self._cur_comp_id:
            for i in range(self._comp_list.count()):
                if self._comp_list.item(i).data(Qt.ItemDataRole.UserRole) == self._cur_comp_id:
                    self._comp_list.setCurrentRow(i)
                    break

    def _on_comp_selected(self, current, _prev):
        if current:
            self._cur_comp_id = current.data(Qt.ItemDataRole.UserRole)
            self._refresh_mode_table()
        else:
            self._cur_comp_id = None
            self._mode_table.setRowCount(0)

    def _comp_add(self):
        name, ok = QInputDialog.getText(self, "Ny komponenttyp", "Namn:")
        if ok and name.strip():
            self._cur_comp_id = self.db.add_component_type(name.strip())
            self._refresh_comp_list()

    def _comp_rename(self):
        item = self._comp_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_component_type(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._refresh_comp_list()

    def _comp_delete(self):
        item = self._comp_list.currentItem()
        if not item: return
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort '{item.text()}' och alla dess felmoder?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_component_type(item.data(Qt.ItemDataRole.UserRole))
            self._cur_comp_id = None
            self._refresh_comp_list()
            self._mode_table.setRowCount(0)

    # ── Failure modes table ───────────────────────────────────────────────────

    def _refresh_mode_table(self):
        try:
            self._mode_table.cellChanged.disconnect()
        except Exception:
            pass
        self._mode_table.setRowCount(0)
        if not self._cur_comp_id:
            self._mode_table.cellChanged.connect(self._on_mode_cell)
            return

        for fm in self.db.failure_modes(self._cur_comp_id):
            r = self._mode_table.rowCount()
            self._mode_table.insertRow(r)

            desc = QTableWidgetItem(fm['description'])
            desc.setData(Qt.ItemDataRole.UserRole, fm['id'])
            self._mode_table.setItem(r, 0, desc)

            freq = fm['freq_per_year']
            freq_item = QTableWidgetItem(
                f"{freq:.4g}" if freq is not None else "")
            freq_item.setToolTip("Händelser per år, t.ex. 0.05 (en gång per 20 år)")
            self._mode_table.setItem(r, 1, freq_item)

            f_level = freq_to_f_level(freq) if freq else None
            f_item = QTableWidgetItem(
                f"F={f_level}" if f_level is not None else "—")
            f_item.setFlags(f_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if f_level is not None:
                label, bg, _ = risk_info(f_level, 3)
                f_item.setBackground(QBrush(QColor(bg)))
                f_item.setForeground(QBrush(QColor('#fff')))
                f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._mode_table.setItem(r, 2, f_item)
            self._mode_table.setRowHeight(r, 26)

        self._mode_table.cellChanged.connect(self._on_mode_cell)

    def _mode_add(self):
        if not self._cur_comp_id:
            QMessageBox.information(self, "Välj komponent",
                "Välj en komponenttyp i listan till vänster.")
            return
        self.db.add_failure_mode(self._cur_comp_id, "Ny felmod")
        self._refresh_mode_table()

    def _mode_delete(self):
        rows = {idx.row() for idx in self._mode_table.selectedIndexes()}
        if not rows: return
        for r in sorted(rows, reverse=True):
            item = self._mode_table.item(r, 0)
            if item:
                self.db.delete_failure_mode(item.data(Qt.ItemDataRole.UserRole))
        self._refresh_mode_table()

    def _on_mode_cell(self, row, col):
        item0 = self._mode_table.item(row, 0)
        if not item0: return
        fm_id = item0.data(Qt.ItemDataRole.UserRole)
        desc  = item0.text().strip() or 'Ny felmod'
        freq_item = self._mode_table.item(row, 1)
        freq = None
        if freq_item:
            try:
                freq = float(freq_item.text().strip()) if freq_item.text().strip() else None
            except ValueError:
                freq = None
        self.db.update_failure_mode(fm_id, desc, freq)
        # Update F-level cell
        f_level = freq_to_f_level(freq) if freq else None
        f_item = self._mode_table.item(row, 2)
        if f_item:
            self._mode_table.blockSignals(True)
            f_item.setText(f"F={f_level}" if f_level is not None else "—")
            if f_level is not None:
                _, bg, _ = risk_info(f_level, 3)
                f_item.setBackground(QBrush(QColor(bg)))
                f_item.setForeground(QBrush(QColor('#fff')))
            self._mode_table.blockSignals(False)


class _ComboBoxCellDelegate(QStyledItemDelegate):
    """Editable-combo-box cell for a QTableView, without a persistent QComboBox
    per row. The combo only exists while a cell is actually being edited —
    used by PIDAnalysisPanel and EquipmentPanel, both of which used to embed
    one real QComboBox per row via setCellWidget(); with thousands of rows
    that alone took tens of seconds to build. Pair with a view whose
    `clicked` signal calls view.edit(index) for this column so a single
    click opens the dropdown, matching the old always-visible-combo feel."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self._items = items

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(self._items)
        return combo

    def setEditorData(self, editor, index):
        text = index.data(Qt.ItemDataRole.EditRole) or ''
        i = editor.findText(text)
        editor.setCurrentIndex(i if i >= 0 else 0)
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class _ButtonCellDelegate(QStyledItemDelegate):
    """Paints a push-button label in a QTableView cell without a persistent
    QPushButton per row (same rationale as _ComboBoxCellDelegate above).
    on_click(index) is called with the *view's* model index (which may be a
    proxy index — map through the proxy before touching the source model)."""

    def __init__(self, text, on_click, parent=None):
        super().__init__(parent)
        self._text     = text
        self._on_click = on_click

    def paint(self, painter, option, index):
        opt = QStyleOptionButton()
        opt.rect  = option.rect.adjusted(3, 2, -3, -2)
        opt.text  = self._text
        opt.state = QStyle.StateFlag.State_Enabled
        QApplication.style().drawControl(QStyle.ControlElement.CE_PushButton, opt, painter)

    def editorEvent(self, event, model, option, index):
        if (event.type() == QEvent.Type.MouseButtonRelease
                and option.rect.contains(event.pos())):
            self._on_click(index)
            return True
        return False

    def createEditor(self, parent, option, index):
        return None   # never a real editor widget — clicks are handled above


_PA_CODE, _PA_EX, _PA_SUGG, _PA_TYPE, _PA_USE = range(5)
_PA_HEADERS = ['Prefix', 'Exempeltaggar', 'Databas-förslag', 'Komponenttyp', 'Använd ✓']


class _IdentifiedTagsModel(QAbstractTableModel):
    """Backs PIDAnalysisPanel's QTableView. Rows are kept as plain dicts in
    memory (cheap) and DB writes happen in setData() — no per-row widgets."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db    = db
        self._rows = []   # list[dict], one per pid_identified_tags row

    def load(self):
        self.beginResetModel()
        self._rows = [dict(r) for r in self.db.pid_identified_tags()]
        self.endResetModel()

    def rows(self):
        return self._rows

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 5

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _PA_HEADERS[section]
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        col = index.column()
        if col == _PA_TYPE:
            return base | Qt.ItemFlag.ItemIsEditable
        if col == _PA_USE:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == _PA_CODE: return row['tag_code']
            if col == _PA_EX:   return row['examples'] or ''
            if col == _PA_SUGG: return row['name_sv'] or '—'
            if col == _PA_TYPE: return row['comp_type'] or ''
            return None
        if role == Qt.ItemDataRole.CheckStateRole and col == _PA_USE:
            return Qt.CheckState.Checked if row['confirmed'] else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.FontRole and col == _PA_CODE:
            return QFont('Courier', 10)
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == _PA_EX:   return QBrush(QColor('#555555'))
            if col == _PA_SUGG: return QBrush(QColor('#8D9299'))
        if role == Qt.ItemDataRole.TextAlignmentRole and col == _PA_USE:
            return Qt.AlignmentFlag.AlignCenter
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        row = self._rows[index.row()]
        col = index.column()
        try:
            if role == Qt.ItemDataRole.CheckStateRole and col == _PA_USE:
                confirmed = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                row['confirmed'] = 1 if confirmed else 0
                self.db.confirm_pid_tag(row['tag_code'], row['comp_type'] or '', confirmed)
            elif role == Qt.ItemDataRole.EditRole and col == _PA_TYPE:
                row['comp_type'] = str(value)
                self.db.confirm_pid_tag(row['tag_code'], row['comp_type'], bool(row['confirmed']))
            else:
                return False
        except Exception:
            logging.exception('_IdentifiedTagsModel.setData: DB write failed (row=%d col=%d)',
                              index.row(), col)
            return False
        self.dataChanged.emit(index, index, [role])
        return True

    def bulk_set_confirmed(self, confirm: bool):
        """Set 'confirmed' for every row with a single commit — setData()
        commits per call, which is fine for one edit but would mean one
        fsync-ish SQLite commit per row (thousands of them) for 'Välj alla'."""
        if not self._rows:
            return
        conf = 1 if confirm else 0
        for row in self._rows:
            row['confirmed'] = conf
        try:
            self.db.conn.executemany(
                "UPDATE pid_identified_tags SET comp_type=?,confirmed=? WHERE tag_code=?",
                [(row['comp_type'] or '', conf, row['tag_code']) for row in self._rows])
            self.db.commit()
        except Exception:
            logging.exception('_IdentifiedTagsModel.bulk_set_confirmed: DB write failed')
            return
        self.dataChanged.emit(self.index(0, _PA_USE), self.index(len(self._rows) - 1, _PA_USE),
                              [Qt.ItemDataRole.CheckStateRole])


class PIDAnalysisPanel(QWidget):
    """Settings panel: shows all tag prefixes found in the P&ID with component-type mapping."""

    # Component types available for selection
    _COMP_TYPES = [
        '', 'Ventil', 'Säkerhetsventil (PSV)', 'Pump', 'Kompressor',
        'Tank / Kärl', 'Värmeväxlare', 'Instrument / Sensor',
        'Rörledning', 'Övrigt',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._loaded = False   # first refresh() deferred to showEvent — see below
        self._model  = _IdentifiedTagsModel(db, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("Identifierade objekt — P&ID-nyckel")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()

        note = QLabel(
            "Kryssa i 'Använd' för att pre-fylla orsaksmenyn med rätt komponenttyp.")
        note.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(title)
        layout.addWidget(note)

        # Table — QTableView + QAbstractTableModel instead of QTableWidget:
        # populating this used to mean inserting one row (with a real
        # QComboBox widget) per identified tag prefix, which does not scale.
        # See _IdentifiedTagsModel / _ComboBoxCellDelegate above.
        self._tbl = QTableView()
        self._tbl.setModel(self._model)
        self._tbl.setItemDelegateForColumn(
            _PA_TYPE, _ComboBoxCellDelegate(self._COMP_TYPES, self._tbl))
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(0, 70)
        self._tbl.setColumnWidth(2, 180)
        self._tbl.setColumnWidth(3, 160)
        self._tbl.setColumnWidth(4, 70)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.verticalHeader().setDefaultSectionSize(28)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                  QAbstractItemView.EditTrigger.EditKeyPressed)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:3px;}")
        self._tbl.clicked.connect(self._on_cell_clicked)
        layout.addWidget(self._tbl)

        btn_row = QHBoxLayout()
        sel_all = QPushButton("Välj alla")
        sel_all.clicked.connect(lambda: self._bulk_confirm(True))
        desel   = QPushButton("Avmarkera alla")
        desel.clicked.connect(lambda: self._bulk_confirm(False))
        btn_row.addWidget(sel_all); btn_row.addWidget(desel); btn_row.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet("color:#555; font-size:10px;")
        btn_row.addWidget(self._status)
        layout.addLayout(btn_row)

        self._model.dataChanged.connect(lambda *a: self._update_status())

    def showEvent(self, event):
        # See _IdentifiedTagsModel docstring: populating used to block the
        # whole app at startup even when the user never opens Inställningar
        # → Identifierade objekt. Defer to the first time the tab is shown.
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def _on_cell_clicked(self, index):
        if index.column() == _PA_TYPE:
            self._tbl.edit(index)

    def refresh(self):
        self._loaded = True   # any explicit refresh() satisfies showEvent's lazy-load too
        self._model.load()
        self._update_status()

    def _bulk_confirm(self, confirm: bool):
        self._model.bulk_set_confirmed(confirm)
        self._update_status()

    def _update_status(self):
        total     = self._model.rowCount()
        confirmed = sum(1 for row in self._model.rows() if row['confirmed'])
        self._status.setText(f"{total} prefix hittade  |  {confirmed} bekräftade")


class TagDatabasePanel(QWidget):
    """Settings panel for managing the P&ID tag-code database."""

    settings_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Tagdatabas — P&ID taggkodnycklar")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        layout.addWidget(title)

        # ── Import section ────────────────────────────────────────────────────
        import_box = QGroupBox("Importera Excel-databas")
        imp_lay = QHBoxLayout(import_box)
        self._excel_lbl = QLabel("Ingen fil vald")
        self._excel_lbl.setStyleSheet("color:#555;")
        imp_lay.addWidget(self._excel_lbl, 1)
        imp_btn = QPushButton("📂 Välj Excel-fil…")
        imp_btn.clicked.connect(self._import_excel)
        imp_lay.addWidget(imp_btn)
        layout.addWidget(import_box)

        # ── Standard selection ────────────────────────────────────────────────
        std_box = QGroupBox("Aktiv standard")
        std_lay = QHBoxLayout(std_box)
        std_lay.addWidget(QLabel("Följ standard:"))
        self._std_combo = QComboBox()
        self._std_combo.addItem("Alla standarder (union)")
        self._std_combo.currentIndexChanged.connect(self._on_std_changed)
        std_lay.addWidget(self._std_combo, 1)
        layout.addWidget(std_box)

        # ── Smart database ────────────────────────────────────────────────────
        smart_box = QGroupBox("Smart databas")
        smart_lay = QVBoxLayout(smart_box)
        self._smart_chk = QCheckBox(
            "Aktivera smart databas — skannar automatiskt inläst P&ID och "
            "identifierar taggar (pump, ventil, instrument…)")
        self._smart_chk.setChecked(
            self.db.tag_db_setting('smart_enabled', '0') == '1')
        self._smart_chk.toggled.connect(self._on_smart_toggled)
        smart_lay.addWidget(self._smart_chk)
        smart_note = QLabel(
            "Identifierade taggar markeras med ljusgul bakgrund på P&ID:n.\n"
            "Definierade orsaker (HAZOP) markeras med ljusgrön bakgrund.")
        smart_note.setStyleSheet("color:#555; font-size:10px;")
        smart_lay.addWidget(smart_note)
        layout.addWidget(smart_box)

        # ── Tag table ─────────────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels(
            ['Taggkod', 'Svensk benämning', 'Engelsk benämning', 'Kategori', 'Standard'])
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._tbl.setColumnWidth(0, 80)
        self._tbl.setColumnWidth(3, 110)
        self._tbl.setColumnWidth(4, 100)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:3px;}")
        layout.addWidget(self._tbl)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(self._status)

        self._refresh()

    def _refresh(self):
        # Update standard combo
        self._std_combo.blockSignals(True)
        cur = self.db.tag_db_setting('active_standard', '')
        self._std_combo.clear()
        self._std_combo.addItem("Alla standarder (union)", '')
        for std in self.db.tag_database_standards():
            self._std_combo.addItem(std, std)
        idx = self._std_combo.findData(cur)
        if idx >= 0:
            self._std_combo.setCurrentIndex(idx)
        self._std_combo.blockSignals(False)

        # Update table
        entries = self.db.tag_database_entries()
        self._tbl.setRowCount(0)
        for e in entries:
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            for col, val in enumerate([
                    e['tag_code'], e['name_sv'], e['name_en'],
                    e['category'], e['standard']]):
                self._tbl.setItem(r, col, QTableWidgetItem(val or ''))
            self._tbl.setRowHeight(r, 22)

        n = len(entries)
        stds = self.db.tag_database_standards()
        self._status.setText(
            f"{n} taggkoder  |  {len(stds)} standarder: {', '.join(stds)}")

    def _import_excel(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Välj Excel-databas", "", "Excel (*.xlsx *.xls)")
        if not path:
            return
        n, err = self.db.import_tag_database_excel(path)
        if err:
            QMessageBox.critical(self, "Importfel", err)
        else:
            QMessageBox.information(self, "Importerat",
                f"{n} taggkoder importerade från\n{path}")
            self._excel_lbl.setText(path)
            self._refresh()
            self.settings_changed.emit()

    def _on_std_changed(self):
        std = self._std_combo.currentData() or ''
        self.db.set_tag_db_setting('active_standard', std)
        self.settings_changed.emit()

    def _on_smart_toggled(self, checked):
        self.db.set_tag_db_setting('smart_enabled', '1' if checked else '0')
        self.settings_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# EQUIPMENT PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _tag_prefix(tag: str) -> str:
    m = re.match(r'^([A-Z]+)', tag.upper())
    return m.group(1) if m else tag


class ObjectPickerPopup(QDialog):
    """Lets the user pick an already-registered P&ID object
    (equipment_catalog row — everything found via manual add or
    "🎯 Hitta objekt på P&ID", regardless of its green/red marker state)
    to auto-tag a newly created cause/consequence/safeguard. Used by the
    "+" quick-add rows (2026-08-12, see NOTES.md) as an alternative to
    having to drag-and-drop a marker from the P&ID for every new row.

    Three outcomes, distinguished by `.selected` after a call to exec():
    - exec() != Accepted (Escape/closed): whole add was cancelled, caller
      must not create a new row at all.
    - exec() == Accepted and .selected is a dict: user picked that object.
    - exec() == Accepted and .selected is None: user explicitly skipped
      tagging — caller still creates the row, just untagged (free text),
      same as clicking "+" always did before this feature existed.
    """

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self.selected = None
        self.setWindowTitle("Välj objekt")
        self.setMinimumWidth(340)
        self.setMinimumHeight(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        hdr = QLabel(
            "Välj ett registrerat P&ID-objekt att koppla till den nya "
            "raden, eller hoppa över för fri text.")
        hdr.setWordWrap(True)
        hdr.setStyleSheet("font-size:10px; color:#8D9299;")
        layout.addWidget(hdr)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Sök tagg, typ eller beskrivning…")
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_pick_enabled)
        self._list.itemDoubleClicked.connect(lambda _item: self._accept_selected())
        layout.addWidget(self._list, 1)

        btns = QHBoxLayout()
        self._pick_btn = QPushButton("Välj objekt")
        self._pick_btn.setDefault(True)
        self._pick_btn.setEnabled(False)
        self._pick_btn.clicked.connect(self._accept_selected)
        skip_btn = QPushButton("Hoppa över (fri text)")
        skip_btn.clicked.connect(self._accept_skip)
        btns.addWidget(self._pick_btn)
        btns.addStretch()
        btns.addWidget(skip_btn)
        layout.addLayout(btns)

        # Populated after _pick_btn exists — _populate() enables/disables
        # it based on the current selection.
        self._items = [dict(r) for r in db.equipment_items()]
        self._populate(self._items)

        self._search.setFocus()
        add_mini_popup_close_button(self)

    def _populate(self, items):
        self._list.clear()
        for row in items:
            label = f"{row.get('tag') or '(ingen tagg)'}  —  {row.get('equipment_type') or '?'}"
            if row.get('description'):
                label += f"  —  {row['description']}"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, row)
            self._list.addItem(it)
        self._update_pick_enabled()

    def _update_pick_enabled(self):
        self._pick_btn.setEnabled(self._list.currentItem() is not None)

    def _apply_filter(self, text):
        text = (text or '').strip().lower()
        if not text:
            self._populate(self._items)
            return
        filtered = [r for r in self._items
                    if text in (r.get('tag') or '').lower()
                    or text in (r.get('equipment_type') or '').lower()
                    or text in (r.get('description') or '').lower()]
        self._populate(filtered)

    def _accept_selected(self):
        it = self._list.currentItem()
        if it is None:
            return
        self.selected = it.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _accept_skip(self):
        self.selected = None
        self.accept()


class EquipmentTagPopup(QDialog):
    """Small popup for the P&ID right-click menu's "🔧 Objekt" action —
    pick an object type and optionally set/edit its tag, independent of
    any cause (2026-08-07, see NOTES.md). Deliberately not CauseObjectPopup:
    this has no standard-cause list to show, it only resolves
    (tag, comp_type) for PIDPanel.place_equipment_marker."""
    committed = pyqtSignal(str, str)  # (comp_tag, comp_type)

    def __init__(self, db, suggested_tag='', suggested_type='', parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Objekt")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumWidth(240)

        _small = "font-size:10px;"
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("<b>Nytt objekt på P&amp;ID</b>")
        title.setStyleSheet("font-size:11px; color:#8D9299;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._tag_edit = QLineEdit(suggested_tag)
        self._tag_edit.setPlaceholderText("t.ex. P-101 (valfritt)")
        self._tag_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._tag_edit.setStyleSheet(_small)
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        tag_lbl = QLabel("Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        # Non-editable dropdown (2026-08-13 follow-up: "Rullgardinen ...
        # har försvunnit. Det ska vara de valen som det var innan" — an
        # editable QComboBox loses its usual dropdown-arrow affordance
        # under this app's global stylesheet, so it looked broken instead
        # of just "also typable"). A brand-new type is instead added via
        # the "+" button next to it, which keeps the normal pick-from-list
        # experience intact and adds a distinct, explicit action for the
        # rarer "this type doesn't exist yet" case.
        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._type_cb.setStyleSheet(_small)
        self._type_cb.addItems(_equipment_type_options(db))
        if suggested_type:
            idx = self._type_cb.findText(suggested_type)
            if idx < 0:
                self._type_cb.addItem(suggested_type)
                idx = self._type_cb.count() - 1
            self._type_cb.setCurrentIndex(idx)
        typ_row = QHBoxLayout()
        typ_row.setSpacing(4)
        typ_row.addWidget(self._type_cb)
        add_type_btn = QPushButton("+ Lägg till")
        add_type_btn.setFixedHeight(CONFIG['H_BTN_SMALL'])
        add_type_btn.setStyleSheet(_small)
        add_type_btn.setToolTip("Lägg till en ny objekttyp")
        add_type_btn.clicked.connect(self._add_new_type)
        typ_row.addWidget(add_type_btn)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, typ_row)
        layout.addLayout(form)

        # Duplicate-tag hint (2026-08-10, see NOTES.md) — place_equipment_marker
        # silently reuses an existing equipment_catalog row for a tag that's
        # already known (never creates a duplicate); this just surfaces that
        # fact to the user instead of leaving it invisible.
        self._dup_hint = QLabel("")
        self._dup_hint.setStyleSheet("font-size:9px; color:#b8860b;")
        self._dup_hint.setWordWrap(True)
        layout.addWidget(self._dup_hint)
        self._tag_edit.textChanged.connect(self._check_duplicate_tag)
        self._check_duplicate_tag(suggested_tag)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setFixedHeight(CONFIG['H_CTRL_STD'])
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setFixedHeight(CONFIG['H_CTRL_STD'])
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addStretch()
        btns.addWidget(cancel)
        layout.addLayout(btns)

        self._tag_edit.returnPressed.connect(self._ok)
        add_mini_popup_close_button(self)

    def _add_new_type(self):
        """"+" button next to the Typ dropdown (2026-08-13 follow-up) —
        equipment_catalog.equipment_type is plain free text, so a brand
        new type just needs adding to the combo and selecting; no
        equipment_catalog write happens until the popup itself is
        committed via _ok(). Also registers the name as a Standardobjekt
        right away (2026-08-13, same-day follow-up: "lägger jag till
        ytterligare något här skall det också dyka upp i standardobjekt
        ... Dessa skall prata med varandra") so it's immediately
        available in the cause-suggestion forms too, not just here."""
        name, ok = QInputDialog.getText(self, "Ny objekttyp", "Namn:")
        name = (name or '').strip()
        if not ok or not name:
            return
        idx = self._type_cb.findText(name)
        if idx < 0:
            self._type_cb.addItem(name)
            idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(idx)
        exists = self._db.conn.execute(
            "SELECT 1 FROM standard_objects WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not exists:
            self._db.add_standard_object(name)

    def _check_duplicate_tag(self, text):
        tag = (text or '').strip()
        existing = self._db.get_equipment_by_tag(tag) if tag else None
        if existing:
            self._dup_hint.setText(
                f"ℹ️ \"{existing['tag']}\" finns redan i katalogen "
                f"({existing.get('equipment_type') or '?'}) — kopplas till den befintliga raden.")
        else:
            self._dup_hint.setText("")

    def _ok(self):
        tag = self._tag_edit.text().strip().upper()
        comp_type = self._type_cb.currentText().strip()
        if not tag and not comp_type:
            QMessageBox.information(self, "Ange typ eller tag",
                "Ange minst en typ eller ett taggnummer för objektet.")
            return
        self.committed.emit(tag, comp_type)
        self.accept()


# Column indices
_EC_CHK  = 0
_EC_TAG  = 1
_EC_PFX  = 2
_EC_PAGE = 3
_EC_OCR  = 4
_EC_TYPE = 5
_EC_DESC = 6
_EC_DEL  = 7


class _EquipmentTableModel(QAbstractTableModel):
    """Backs EquipmentPanel's QTableView. Rows are kept as plain dicts in
    memory (cheap) and DB writes happen in setData()/delete_row() — no more
    persistent per-row QComboBox/QPushButton widgets, which is what made
    populating 10k+ rows take upwards of a minute (see NOTES.md, 2026-08-06)."""

    write_failed = pyqtSignal(str)   # emitted with an error message on a failed DB write
    # Tag/type of an equipment_catalog row changed here — the tree and
    # scenario table both resolve an object's identity LIVE from
    # equipment_catalog (2026-08-18, see NOTES.md "Objektets identitet
    # ..."), so they must be told to refresh too, not just this table.
    identity_changed = pyqtSignal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db    = db
        self._rows = []   # list[dict], one per equipment_catalog row

    def load(self):
        self.beginResetModel()
        self._rows = [dict(r) for r in self.db.equipment_items()]
        self.endResetModel()

    def rows(self):
        return self._rows

    def row_dict(self, row):
        return self._rows[row]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 8

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ['✓', 'Tagg', 'Prefix', 'Sida', 'OCR', 'Utrustningstyp', 'Beskrivning', ''][section]
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        col = index.column()
        if col == _EC_CHK:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        if col in (_EC_TAG, _EC_TYPE, _EC_DESC):
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == _EC_TAG:  return row['tag']
            if col == _EC_PFX:  return row.get('prefix') or _tag_prefix(row['tag'])
            if col == _EC_PAGE: return str(row.get('pid_page', 0) + 1)
            if col == _EC_OCR:  return '🔬' if row.get('is_ocr') else ''
            if col == _EC_TYPE: return row.get('equipment_type', '') or ''
            if col == _EC_DESC: return row.get('description', '') or ''
            if col == _EC_DEL:  return 'Ta bort' if role == Qt.ItemDataRole.DisplayRole else None
            return None
        if role == Qt.ItemDataRole.CheckStateRole and col == _EC_CHK:
            return Qt.CheckState.Checked if row.get('include', 1) else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (_EC_PFX, _EC_PAGE, _EC_OCR):
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.BackgroundRole and col == _EC_TAG and row.get('is_ocr'):
            return QBrush(QColor('#fff3cd'))
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == _EC_TAG and row.get('is_ocr'):
                return "Identifierad via OCR — kontrollera taggen"
            if col == _EC_OCR:
                return "Hittad via OCR" if row.get('is_ocr') else "Hittad via PDF-text"
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        row_i = index.row()
        col   = index.column()
        row   = self._rows[row_i]
        try:
            if role == Qt.ItemDataRole.CheckStateRole and col == _EC_CHK:
                checked = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                row['include'] = 1 if checked else 0
                self.db.conn.execute("UPDATE equipment_catalog SET include=? WHERE id=?",
                                     (row['include'], row['id']))
                self.db.commit()
                self.dataChanged.emit(index, index, [role])
                return True

            if role != Qt.ItemDataRole.EditRole:
                return False

            if col == _EC_TAG:
                new_tag = str(value).strip().upper()
                new_pfx = _tag_prefix(new_tag)
                row['tag']    = new_tag
                row['prefix'] = new_pfx
                # Suggest a type from the new prefix only if none set yet —
                # matches the pre-rewrite behaviour exactly.
                if not row.get('equipment_type'):
                    known = KNOWN_PREFIXES.get(new_pfx)
                    if known:
                        row['equipment_type'] = known[1]
                self.db.update_equipment_item(
                    row['id'], new_tag, new_pfx,
                    row.get('equipment_type', ''), row.get('description', ''))
                first = self.index(row_i, 0)
                last  = self.index(row_i, self.columnCount() - 1)
                self.dataChanged.emit(first, last)
                self.identity_changed.emit()
                return True

            if col == _EC_TYPE:
                row['equipment_type'] = str(value)
                self.db.conn.execute(
                    "UPDATE equipment_catalog SET equipment_type=? WHERE id=?",
                    (row['equipment_type'], row['id']))
                self.db.commit()
                self.dataChanged.emit(index, index, [role])
                self.identity_changed.emit()
                return True

            if col == _EC_DESC:
                row['description'] = str(value)
                self.db.update_equipment_item(
                    row['id'], row['tag'], row.get('prefix', ''),
                    row.get('equipment_type', ''), row['description'])
                self.dataChanged.emit(index, index, [role])
                return True
        except Exception as e:
            logging.exception('_EquipmentTableModel.setData: DB write failed (row=%d col=%d)',
                              row_i, col)
            self.write_failed.emit(str(e))
            return False
        return False

    def delete_row(self, row_i):
        row = self._rows[row_i]
        self.db.delete_equipment_item(row['id'])
        self.beginRemoveRows(QModelIndex(), row_i, row_i)
        del self._rows[row_i]
        self.endRemoveRows()

    def bulk_set_include(self, row_indices, checked: bool):
        """Set 'include' for many rows with a single commit — setData() commits
        per call, which is correct for one edit but would mean one fsync-ish
        SQLite commit per row (thousands of them) for a bulk checkbox action."""
        if not row_indices:
            return
        inc = 1 if checked else 0
        ids = []
        for r in row_indices:
            row = self._rows[r]
            row['include'] = inc
            ids.append(row['id'])
        try:
            self.db.conn.executemany(
                "UPDATE equipment_catalog SET include=? WHERE id=?", [(inc, i) for i in ids])
            self.db.commit()
        except Exception as e:
            logging.exception('_EquipmentTableModel.bulk_set_include: DB write failed')
            self.write_failed.emit(str(e))
            return
        top = self.index(min(row_indices), _EC_CHK)
        bot = self.index(max(row_indices), _EC_CHK)
        self.dataChanged.emit(top, bot, [Qt.ItemDataRole.CheckStateRole])


class _EquipmentFilterProxy(QSortFilterProxyModel):
    """Search-text + 'OCR only' filter for EquipmentPanel — replaces the old
    per-row setRowHidden() loop, which needed the underlying QTableWidget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text     = ''
        self._ocr_only = False

    def set_filter_text(self, text: str):
        self._text = text.lower()
        self.invalidateFilter()

    def set_ocr_only(self, on: bool):
        self._ocr_only = on
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        row   = model.row_dict(source_row)
        if self._ocr_only and not row.get('is_ocr'):
            return False
        if self._text:
            tag_t  = row['tag'].lower()
            type_t = (row.get('equipment_type') or '').lower()
            pg_t   = str(row.get('pid_page', 0) + 1)
            if (self._text not in tag_t and self._text not in type_t
                    and self._text not in pg_t):
                return False
        return True


class EquipmentPanel(QWidget):
    """Persistent equipment register — scan P&ID, review, edit and create nodes."""

    markers_saved = pyqtSignal()   # equipment_markers layer changed — P&ID view should reload

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._model = _EquipmentTableModel(db, self)
        self._model.write_failed.connect(
            lambda msg: QMessageBox.critical(self, "Fel vid celländring (utrustning)", msg))
        # An inline tag/type edit here is exactly the kind of change
        # markers_saved already exists to announce (2026-08-18, see
        # NOTES.md "Objektets identitet ...") — reuse it so MainWindow's
        # existing pid_panel.reload_overlays() wiring picks it up too,
        # instead of adding a second, parallel signal for the same effect.
        self._model.identity_changed.connect(self.markers_saved.emit)
        self._proxy = _EquipmentFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        title = QLabel("Utrustningsregister")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        # Toolbar
        tb = QHBoxLayout()
        self._scan_btn = QPushButton("Skanna P&ID")
        self._scan_btn.setIcon(_icon('scan', 16, '#ffffff'))
        self._scan_btn.setToolTip("Skannar inläst P&ID-fil efter utrustningstaggar")
        self._scan_btn.setStyleSheet(
            "background:#2F5FD0; color:white; border:none; border-radius:4px; padding:3px 10px;")
        self._scan_btn.clicked.connect(self._scan)

        add_btn = QPushButton("+ Lägg till")
        add_btn.setToolTip("Lägg till en tagg manuellt")
        add_btn.clicked.connect(self._add_manual)

        refresh_btn = QPushButton("Uppdatera")
        refresh_btn.setIcon(_icon('refresh'))
        refresh_btn.clicked.connect(self.refresh)

        self._create_btn = QPushButton("🏭 Skapa HAZOP-noder")
        self._create_btn.setToolTip("Skapar en nod per ikryssad rad")
        self._create_btn.clicked.connect(self._create_nodes)

        self._autodetect_btn = QPushButton("Hitta objekt på P&ID")
        self._autodetect_btn.setIcon(_icon('target'))
        self._autodetect_btn.setToolTip(
            "Analyserar utrustning (ventiler, pumpar, instrument m.fl.): kopplar\n"
            "varje känd tagg till dess ritade symbol OCH letar efter formigenkända\n"
            "symboler som saknar tagg — allt i en bakgrundskörning med synlig\n"
            "progress.\n"
            "Kör 🔍 Skanna P&ID först om registret är tomt.")
        self._autodetect_btn.clicked.connect(self._autodetect)

        clear_btn = QPushButton("Rensa utrustning")
        clear_btn.setIcon(_icon('delete'))
        clear_btn.setToolTip("Tar bort alla poster i utrustningsregistret")
        clear_btn.setStyleSheet("color:#c0392b; font-weight:bold;")
        clear_btn.clicked.connect(self._clear)

        for btn in [self._scan_btn, add_btn, refresh_btn, self._create_btn,
                    self._autodetect_btn, clear_btn]:
            tb.addWidget(btn)
        tb.addStretch()
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#555; font-size:11px;")
        tb.addWidget(self._status_lbl)
        layout.addLayout(tb)

        # Filter bar
        fb = QHBoxLayout()
        fb.addWidget(QLabel("Filtrera:"))
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Sök tagg, typ, sida…")
        self._filter.textChanged.connect(self._apply_filter)
        fb.addWidget(self._filter)
        sel_all  = QPushButton("Välj alla")
        desel    = QPushButton("Avmarkera alla")
        self._ocr_only = QPushButton("Visa OCR")
        self._ocr_only.setCheckable(True)
        self._ocr_only.toggled.connect(self._apply_filter)
        sel_all.clicked.connect(lambda: self._bulk_check(True))
        desel.clicked.connect(lambda: self._bulk_check(False))
        for b in [sel_all, desel, self._ocr_only]:
            fb.addWidget(b)
        layout.addLayout(fb)

        # Table — QTableView + QAbstractTableModel instead of QTableWidget:
        # populating this used to mean inserting one row (with a real
        # QComboBox *and* a real QPushButton widget) per equipment item,
        # which does not scale. See _EquipmentTableModel / _ComboBoxCellDelegate
        # / _ButtonCellDelegate above.
        self._tbl = QTableView()
        self._tbl.setModel(self._proxy)
        self._tbl.setItemDelegateForColumn(
            _EC_TYPE, _ComboBoxCellDelegate(_EQ_TYPE_ITEMS, self._tbl))
        self._tbl.setItemDelegateForColumn(
            _EC_DEL, _ButtonCellDelegate("Ta bort", self._on_delete_clicked, self._tbl))
        hdr = self._tbl.horizontalHeader()
        modes = [
            (_EC_CHK,  QHeaderView.ResizeMode.Fixed),
            (_EC_TAG,  QHeaderView.ResizeMode.Interactive),
            (_EC_PFX,  QHeaderView.ResizeMode.Fixed),
            (_EC_PAGE, QHeaderView.ResizeMode.Fixed),
            (_EC_OCR,  QHeaderView.ResizeMode.Fixed),
            (_EC_TYPE, QHeaderView.ResizeMode.Interactive),
            (_EC_DESC, QHeaderView.ResizeMode.Stretch),
            (_EC_DEL,  QHeaderView.ResizeMode.Fixed),
        ]
        widths = {_EC_CHK: 30, _EC_TAG: 110, _EC_PFX: 60, _EC_PAGE: 44,
                  _EC_OCR: 36, _EC_TYPE: 185, _EC_DEL: 64}
        for col, mode in modes:
            hdr.setSectionResizeMode(col, mode)
        for col, w in widths.items():
            self._tbl.setColumnWidth(col, w)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.verticalHeader().setDefaultSectionSize(26)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                  QAbstractItemView.EditTrigger.EditKeyPressed)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        self._tbl.clicked.connect(self._on_cell_clicked)
        layout.addWidget(self._tbl)

        self._model.dataChanged.connect(lambda *a: self._update_status())

        # No eager refresh() here: populating this table used to mean
        # building thousands of cell widgets, which does not scale — doing
        # that unconditionally in __init__ used to block the whole app at
        # startup even when the user never opens the Equipment page.
        # MainWindow._switch_view() already calls refresh() every time this
        # page (index 4 as of 2026-08-26's Rekommendationer insertion; was
        # index 3 before that, and index 2 before HAZOP preparation became
        # index 0 on 2026-08-17 — this comment has lagged the real index
        # more than once, see _switch_view itself for the current source
        # of truth) becomes active, including the first time.

    # ── Populate ──────────────────────────────────────────────────────────────

    def refresh(self):
        self._model.load()
        self._apply_filter()

    def select_row_by_equipment_id(self, equipment_id):
        """Select and scroll to the register row for `equipment_id` — used
        when a valve marker on the P&ID is clicked
        (MainWindow._on_equipment_marker_navigate). Clears any active
        filter first so the target row can never be hidden by it."""
        src_row = next((i for i, row in enumerate(self._model.rows())
                        if row.get('id') == equipment_id), None)
        if src_row is None:
            return
        if self._filter.text() or self._ocr_only.isChecked():
            self._filter.clear()
            self._ocr_only.setChecked(False)
        proxy_index = self._proxy.mapFromSource(self._model.index(src_row, _EC_TAG))
        if not proxy_index.isValid():
            return
        self._tbl.setCurrentIndex(proxy_index)
        self._tbl.selectRow(proxy_index.row())
        self._tbl.scrollTo(proxy_index)

    def _on_cell_clicked(self, index):
        if index.column() == _EC_TYPE:
            self._tbl.edit(index)

    def _on_delete_clicked(self, proxy_index):
        self._model.delete_row(self._proxy.mapToSource(proxy_index).row())
        self._update_status()

    # ── Filter / selection ────────────────────────────────────────────────────

    def _apply_filter(self):
        self._proxy.set_filter_text(self._filter.text())
        self._proxy.set_ocr_only(self._ocr_only.isChecked())
        self._update_status()

    def _bulk_check(self, checked: bool):
        src_rows = [self._proxy.mapToSource(self._proxy.index(pr, _EC_CHK)).row()
                    for pr in range(self._proxy.rowCount())]
        self._model.bulk_set_include(src_rows, checked)
        self._update_status()

    def _update_status(self):
        total_all = self._model.rowCount()
        visible   = self._proxy.rowCount()
        checked   = sum(1 for pr in range(visible)
                        if self._model.row_dict(
                            self._proxy.mapToSource(self._proxy.index(pr, _EC_CHK)).row()
                        ).get('include', 1))
        self._status_lbl.setText(
            f"{total_all} taggar totalt  |  {visible} visas  |  {checked} valda")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _add_manual(self):
        # Same EquipmentTagPopup used by the P&ID's "🔧 Objekt" action
        # (2026-08-09, see NOTES.md) — one dialog for "manually add an
        # equipment item" everywhere, with an actual type field and the
        # duplicate-tag hint, instead of this page's own bare
        # QInputDialog.getText() that only ever guessed the type from
        # KNOWN_PREFIXES and had no duplicate check at all.
        popup = EquipmentTagPopup(self.db, parent=self)
        popup.committed.connect(self._on_manual_equipment_committed)
        popup.exec()

    def _on_manual_equipment_committed(self, tag, comp_type):
        tag = tag.strip().upper()
        if not tag:
            return
        existing = self.db.get_equipment_by_tag(tag)
        if existing:
            if comp_type and comp_type != existing.get('equipment_type'):
                self.db.update_equipment_item(
                    existing['id'], existing['tag'], existing['prefix'],
                    comp_type, existing.get('description') or '')
        else:
            pfx = _tag_prefix(tag)
            known = KNOWN_PREFIXES.get(pfx, ('', ''))
            self.db.add_equipment_item(
                tag, tag, pfx, 0, comp_type or (known[1] if known else ''), '', 0)
        self.refresh()

    def _clear(self):
        n = len(self.db.equipment_items())
        reply = QMessageBox.question(
            self, "Rensa utrustning",
            f"Ta bort alla {n} poster i utrustningsregistret?\n\n"
            "Detta kan inte ångras.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Ok:
            self.db.clear_equipment_catalog()
            self.refresh()

    def _create_nodes(self):
        # Iterates ALL rows (not just those matching the current filter) —
        # only the checkbox state matters, same as before the rewrite.
        to_create = []
        for row in self._model.rows():
            if row.get('include', 1):
                tag  = row['tag']
                pg   = row.get('pid_page', 0)
                et   = row.get('equipment_type', '') or ''
                desc = row.get('description', '') or ''
                if tag:
                    to_create.append((tag, pg, et, desc))
        if not to_create:
            QMessageBox.information(self, "Ingen vald", "Kryssa i minst en rad.")
            return
        created = 0
        # Each selected equipment item creates a node plus its initial
        # metadata.  The whole "create nodes" action should be one undo step,
        # even when several rows are selected.
        with self.db.history_group():
            for tag, pg, et, desc in to_create:
                nid = self.db.add_node_with_markup(
                    tag, [], {'color': '#FF8C00', 'width': 2, 'alpha': 180}, pg)
                self.db.conn.execute(
                    "UPDATE nodes SET name=?, pid_ref=?, description=? WHERE id=?",
                    (tag, f"Sida {pg + 1}", f"{et}{': ' + desc if desc else ''}", nid))
                self.db.commit()
                created += 1
        QMessageBox.information(self, "Klart",
            f"{created} HAZOP-noder skapade.\nGå till P&ID-vyn och uppdatera trädet.")

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _scan(self):
        """🔍 Skanna P&ID — runs on background worker PROCESSES
        (ParallelTagScanWorker) when the document is large enough for
        multi-core parallelism to be worth it, with live per-page
        progress (PageProgressDialog) — see NOTES.md "Flerkärnig
        parallellisering av Analysera P&ID". Falls back to a single
        sequential pass for small documents or if the process pool can't
        start."""
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:  pip install PyMuPDF")
            return

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID",
                "Öppna en P&ID-fil i P&ID-vyn först, sedan kan du skanna härifrån.")
            return

        try:
            import fitz
            n_pages = fitz.open(str(path)).page_count
        except Exception as e:
            QMessageBox.warning(self, "PDF-fel", f"Kunde inte öppna PDF:\n{e}")
            return

        # OCR choice -- honours "OCR-standardval" (Inställningar →
        # P&ID-inställningar, config key 'ocr_default_engine') to skip the
        # Yes/No prompt when the user has picked a specific default engine.
        use_ocr, ocr_engine = resolve_ocr_scan_choice(self.db, self)

        dlg = PageProgressDialog("Skannar P&ID…", n_pages, self)
        worker = ParallelTagScanWorker(path, use_ocr=use_ocr, ocr_engine=ocr_engine)
        self._scan_thread = worker   # keep a reference so it isn't GC'd mid-run

        cancelled_flag = {'v': False}

        def _on_cancel():
            cancelled_flag['v'] = True
            worker.requestInterruption()

        def _on_finished(result):
            dlg.close()
            self._scan_thread = None
            if cancelled_flag['v']:
                return

            meta = result.pop('_meta', {})
            real = {k: v for k, v in result.items() if not k.startswith('_')}

            if not real:
                QMessageBox.warning(
                    self, "Inga taggar",
                    "Inga utrustningstaggar hittades.\n\n"
                    + ("Prova med OCR aktiverat (installera pytesseract eller easyocr)."
                       if not use_ocr else
                       "Kontrollera att PDF-texten är läsbar och försök med OCR."))
                return

            # Import to DB — shared with "📋 Analysera P&ID" (PIDPanel._analyze_pid,
            # pid_viewer.py) now that both buttons trigger the same underlying
            # scan; also cross-write "Identifierade objekt" so that panel stays
            # in sync regardless of which button was used.
            apply_scan_result_to_equipment_catalog(self.db, real)
            upsert_identified_tags_from_scan(self.db, real)

            # Build summary
            n_tags   = sum(len(d['tags']) for d in real.values())
            n_groups = len(real)
            ocr_used = meta.get('ocr_used', False)
            ocr_eng  = meta.get('ocr_engine', '')

            type_counts: dict = {}
            for prefix, data in real.items():
                known = KNOWN_PREFIXES.get(prefix)
                et    = known[1] if known else 'Okänd'
                type_counts[et] = type_counts.get(et, 0) + len(data['tags'])

            lines = "\n".join(
                f"  • {t}: {c} st"
                for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))
            ocr_line = f"\n🔬 OCR användes ({ocr_eng})\n" if ocr_used else "\n"

            QMessageBox.information(
                self, "Skanning klar ✅",
                f"Skanning klar!\n\n"
                f"Totalt hittade:  {n_tags}  taggar\n"
                f"Prefix-grupper:  {n_groups}{ocr_line}\n"
                f"Utrustningstyper:\n{lines}\n\n"
                f"Tabellen nedan har uppdaterats.\n"
                f"Redigera eventuella OCR-fel (gul bakgrund) och kryssa i\n"
                f"de taggar du vill skapa HAZOP-noder för.")

            self.refresh()

        worker.page_progress.connect(dlg.set_page_status)
        worker.finished_scan.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        worker.start()
        dlg.exec()

    def set_db(self, db):
        """Swap the database, including the table model's own separate
        db reference (needed for setData()/delete_row() to write through
        directly — see MainWindow._reload_all_panels)."""
        self.db = db
        self._model.db = db

    def autodetect(self):
        """Public entry point for 🎯 Hitta objekt på P&ID — see _autodetect."""
        self._autodetect()

    def _autodetect(self):
        """🎯 Hitta objekt på P&ID — full analysis: weighted tag<->symbol
        association for every known tag in the register (any equipment
        type) AND shape-anchored hunting for valve/pump/instrument-shaped
        symbols with no tag, against one shared per-page cluster
        extraction (detect_equipment_and_valves). Runs on background
        worker PROCESSES (ParallelEquipmentAnalysisWorker) when the
        document is large enough for multi-core parallelism to be worth
        it — falls back to the proven single-thread EquipmentAnalysisWorker
        path otherwise — with live per-page progress (PageProgressDialog),
        including on a 50-page document. See NOTES.md "Flerkärnig
        parallellisering av Analysera P&ID".

        Widened from valve-only to every equipment type (2026-08-10, see
        NOTES.md) — the underlying detect_equipment_and_valves() pipeline
        has done shape-anchored pump/instrument hunting on UNTAGGED
        clusters since 2026-08-07 regardless of this filter; restricting
        tag_points to VALVE_COMPONENT_TYPES only meant a real, already-
        known pump/instrument tag never got a chance at weighted
        association with its own symbol, even though the shape side was
        perfectly capable of confirming it. Renamed from "Hitta ventiler"
        to reflect what it's always been trending toward: recognizing the
        SHAPE of any piece of equipment, not just valves.

        Uses EVERY row with a tag in the register, not just checked ones —
        the global weighted association gets WORSE, not just redundant, if
        the candidate pool is pre-filtered, since a real symbol match for
        an unchecked tag would otherwise be unavailable to steal a
        cluster away from a genuinely wrong candidate.
        """
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:  pip install PyMuPDF")
            return

        tag_points = []          # (tag, prefix, page, x, y, conf) — x/y resolved in-thread
        tag_to_equipment_id = {}
        for row in self._model.rows():
            tag = (row.get('tag') or '').strip()
            if not tag:
                continue
            prefix = row.get('prefix') or _tag_prefix(tag)
            tag_points.append((tag, prefix, row.get('pid_page', 0), None, None, 1.0))
            tag_to_equipment_id[tag] = row['id']

        if not tag_points:
            QMessageBox.information(
                self, "Inga taggar i registret",
                "Hittade inga taggade rader i Utrustningsregistret.\n\n"
                "Kör 🔍 Skanna P&ID om registret är tomt.")
            return

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID",
                "Öppna en P&ID-fil i P&ID-vyn först.")
            return
        try:
            import fitz
            n_pages = fitz.open(str(path)).page_count
        except Exception as e:
            QMessageBox.warning(self, "PDF-fel", f"Kunde inte öppna PDF:\n{e}")
            return

        dlg = PageProgressDialog("Analyserar P&ID…", n_pages, self)
        thread = ParallelEquipmentAnalysisWorker(path, tag_points)
        self._analysis_thread = thread   # keep a reference so it isn't GC'd mid-run

        cancelled_flag = {'v': False}

        def _on_cancel():
            cancelled_flag['v'] = True
            thread.requestInterruption()

        def _on_finished(results, rejected):
            dlg.close()
            self._analysis_thread = None
            if cancelled_flag['v']:
                return
            for res in results:
                if res.get('tag_status') != 'untagged':
                    res['equipment_id'] = tag_to_equipment_id.get(res['tag'])
            if not results:
                QMessageBox.information(self, "Inget hittat",
                    "Inga objekt eller symboler hittades.")
                return
            review_dlg = EquipmentMarkerReviewDialog(
                results, self.db, parent=self, rejected=rejected, pdf_path=path)
            if review_dlg.exec():
                self.markers_saved.emit()

        thread.page_progress.connect(dlg.set_page_status)
        thread.finished_analysis.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        thread.start()
        dlg.exec()

