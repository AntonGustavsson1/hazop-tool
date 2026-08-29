#!/usr/bin/env python3
"""Rekommendationer page — new top-level nav page added 2026-08-26 (see
NOTES.md), inserted right after Worksheet. Read-only overview of the whole
recommendation catalog (Database.all_recommendations(), display-number order): one row
per catalog ENTRY, not per consequence link, so a recommendation reused
across several causes (consequence_recommendations is many-to-many, see
database.py) appears once, not duplicated.

Column 1 mirrors the "XXX. description" convention already used for the
REK column in scenario_panel.py (_recommendation_summary/the picker popup).
Column 2 shows the responsible person stored on the recommendation.
Column 3 shows the hierarchical studie.nod.avvikelse.orsak.konsekvens
reference(s) this recommendation currently resolves to (see
_build_position_maps()'s docstring below for exactly how each level is
numbered, and the deliberate simplification vs. the HAZOP tree's own
numbering).

Follows this app's "layer + re-export" convention (see hazop/CLAUDE.md
Architecture section) — a standalone module with no dependency on
hazop.py, imported and re-exported from there so `from hazop import
RecommendationsPanel` keeps working for tests, same as
`from hazop import HAZOPWorksheet` already does today. Unlike worksheet.py
this needs no deferred/circular-import dance: it only reaches into
database.py, nothing defined in hazop.py itself."""

import re

from PyQt6.QtCore import Qt, QDate, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QComboBox, QDateEdit, QPushButton, QMessageBox, QLabel,
    QLineEdit,
)

from database import Database

_STATUS_ALL = 'Alla statusar'
_STATUS_VALUES = ('Öppen', 'Pågår', 'Klar', 'Försenad')

_PLACEHOLDER = '—'   # same "no link yet" convention as KON/SG/REK cells elsewhere


def _build_position_maps(db: Database):
    """Walk the whole study once (nodes -> deviations -> causes ->
    consequences) and return the lookup dicts needed to resolve any
    consequence_id to its full 1-based
    studie.nod.avvikelse.orsak.konsekvens position.

    Numbering rule (deliberate design decision, see NOTES.md): every level
    is numbered by its OWN raw DB row order — db.nodes() flat across the
    whole study (the systems/SYSTEM_T grouping layer is ignored — folding
    it in would need a 6th number, which was never asked for),
    db.deviations(node_id), db.causes_for_deviation(deviation_id) and
    db.consequences(cause_id), all of which are stable ORDER BY id.

    This is DELIBERATELY NOT the same numbering the HAZOP tree itself
    displays for deviations: tree_panel.py's _add_node_item groups several
    raw `deviations` rows that share the same guide-word text into one
    numbered "ledord group" row (see its `ledord_groups` dict), so a raw
    deviation row's tree-visible number can differ from its raw row
    position. Reproducing that text-matching merge here would be
    substantial extra complexity for unclear benefit — this simpler, fully
    deterministic raw-DB-order numbering is used instead. Flagged here
    (and in NOTES.md) as a known, intentional divergence, not an
    oversight."""
    node_pos, dev_pos, cause_pos, cons_pos = {}, {}, {}, {}
    dev_node, cause_dev, cons_cause = {}, {}, {}
    for n_i, node in enumerate(db.nodes(), start=1):
        node_pos[node['id']] = n_i
        for d_i, dev in enumerate(db.deviations(node['id']), start=1):
            dev_pos[dev['id']] = d_i
            dev_node[dev['id']] = node['id']
            for c_i, cause in enumerate(db.causes_for_deviation(dev['id']), start=1):
                cause_pos[cause['id']] = c_i
                cause_dev[cause['id']] = dev['id']
                for k_i, cons in enumerate(db.consequences(cause['id']), start=1):
                    cons_pos[cons['id']] = k_i
                    cons_cause[cons['id']] = cause['id']
    return {
        'node_pos': node_pos, 'dev_pos': dev_pos, 'cause_pos': cause_pos, 'cons_pos': cons_pos,
        'dev_node': dev_node, 'cause_dev': cause_dev, 'cons_cause': cons_cause,
    }


def _reference_for_consequence(cons_id, maps):
    """Return "1.<nod>.<avvikelse>.<orsak>.<konsekvens>" for one
    consequence_id, or None if any step of its chain is missing from the
    position maps (an orphaned link pointing at a since-deleted
    cause/deviation/node — defensive, not expected in normal use)."""
    cause_id = maps['cons_cause'].get(cons_id)
    k = maps['cons_pos'].get(cons_id)
    if cause_id is None or k is None:
        return None
    dev_id = maps['cause_dev'].get(cause_id)
    c = maps['cause_pos'].get(cause_id)
    if dev_id is None or c is None:
        return None
    node_id = maps['dev_node'].get(dev_id)
    d = maps['dev_pos'].get(dev_id)
    if node_id is None or d is None:
        return None
    n = maps['node_pos'].get(node_id)
    if n is None:
        return None
    return f"1.{n}.{d}.{c}.{k}"


class _RecommendationsTable(QTableWidget):
    """Recommendation table with a safe row-delete keyboard shortcut."""

    delete_requested = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.delete_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class RecommendationsPanel(QWidget):
    """Simple three-column, read-only QTableWidget: every recommendation in
    the catalog (column 1, "XXX. <description>"), its responsible person
    (column 2), and every hierarchical reference it currently resolves to
    (column 3, comma-
    separated when linked to several consequences, "—" when linked to
    none — an orphaned but still-reusable catalog entry; this app
    deliberately never deletes a recommendation just because its last
    link was removed, see database.py's unlink_recommendation_from_consequence).

    No editing here — recommendations are still created/edited from their
    existing entry points in HAZOP Scenario (the REK column), unchanged."""

    _COL_REC = 0
    _COL_RESPONSIBLE = 1
    _COL_DUE = 2
    _COL_REF = 3
    _COL_STATUS = 4

    recommendations_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        filters.addWidget(QLabel('Sök:'))
        self._search = QLineEdit()
        self._search.setPlaceholderText('Rekommendation, ansvarig, status eller referens…')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filters)
        filters.addWidget(self._search, 1)
        filters.addWidget(QLabel('Status:'))
        self._status_filter = QComboBox()
        self._status_filter.addItem(_STATUS_ALL)
        self._status_filter.addItems(_STATUS_VALUES)
        self._status_filter.currentTextChanged.connect(self._apply_filters)
        filters.addWidget(self._status_filter)
        self._count_label = QLabel('Visar 0 av 0')
        filters.addWidget(self._count_label)
        layout.addLayout(filters)

        self._table = _RecommendationsTable(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Rekommendation", "Ansvarig", "Ska vara åtgärdat",
             "Referens (studie.nod.avvikelse.orsak.konsekvens)", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                    QAbstractItemView.EditTrigger.EditKeyPressed |
                                    QAbstractItemView.EditTrigger.SelectedClicked)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setWordWrap(True)
        self._table.setAlternatingRowColors(True)
        self._table.delete_requested.connect(self._delete_selected)
        self._table.itemSelectionChanged.connect(self._update_delete_button)
        # Native click-to-sort is effectively free with a QTableWidget and
        # was explicitly called out as fine to include — no custom
        # filtering/search UI beyond that, per the request.
        self._table.setSortingEnabled(True)
        header = self._table.horizontalHeader()
        # All columns are manually resizable/reorderable. ResizeToContents
        # made Ansvarig especially hard to widen and made the list feel
        # fixed, despite the other HAZOP tables being user-adjustable.
        for col in range(4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        header.setSectionsMovable(True)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(80)
        self._table.setColumnWidth(self._COL_REC, 360)
        self._table.setColumnWidth(self._COL_RESPONSIBLE, 240)
        self._table.setColumnWidth(self._COL_DUE, 150)
        self._table.setColumnWidth(self._COL_REF, 360)
        self._table.setColumnWidth(self._COL_STATUS, 130)
        # Qt quirk (verified empirically, not from memory): a freshly
        # created QTableWidget's header already carries an implicit
        # "column 0, DESCENDING" sort indicator even though nothing ever
        # called setSortIndicator/sortItems — re-enabling setSortingEnabled
        # after a bulk-populate (see load()'s was_sorting dance) silently
        # applies THAT indicator, reversing the catalog display-number order the
        # request asked for ("Lista alla rekommendationer i kolumn 1", in
        # db.all_recommendations() order). Set an explicit ascending
        # indicator on column 0 up front so the default view matches
        # catalog display-number order; clicking either header still re-sorts freely.
        header.setSortIndicator(self._COL_REC, Qt.SortOrder.AscendingOrder)
        self._table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._table)

        self._delete_btn = QPushButton("Ta bort markerad rekommendation")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_selected)
        layout.addWidget(self._delete_btn)

    def _update_delete_button(self):
        self._delete_btn.setEnabled(bool(self._selected_recommendation_ids()))

    def _selected_recommendation_ids(self):
        ids = []
        for row in sorted({index.row() for index in self._table.selectedIndexes()}):
            item = self._table.item(row, self._COL_REC)
            rec_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            if rec_id is not None:
                ids.append(int(rec_id))
        return ids

    def _delete_selected(self):
        rec_ids = self._selected_recommendation_ids()
        if not rec_ids:
            return
        if len(rec_ids) == 1:
            rec = self.db.get_recommendation(rec_ids[0]) or {}
            number = rec.get('display_number', rec_ids[0])
            label = f"{number:03d}. {(rec.get('description') or '').strip()}"
            question = f"Ta bort rekommendationen {label or rec_ids[0]}?"
        else:
            question = f"Ta bort {len(rec_ids)} markerade rekommendationer?"
        answer = QMessageBox.question(
            self, "Ta bort rekommendation", question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        for rec_id in rec_ids:
            self.db.delete_recommendation(rec_id)
        self.load()
        self.recommendations_changed.emit()

    def _on_item_changed(self, item):
        rec_id = item.data(Qt.ItemDataRole.UserRole)
        if rec_id is None:
            return
        value = item.text().strip()
        if item.column() == self._COL_REC:
            # The compact number is display metadata, regardless of whether
            # the user is editing a legacy "R-001." or current "001." row.
            description = re.sub(r'^(?:R-)?\d+\.\s*', '', value,
                                 flags=re.IGNORECASE)
            self.db.update_recommendation(int(rec_id), description=description)
        elif item.column() == self._COL_RESPONSIBLE:
            self.db.update_recommendation(int(rec_id), responsible=value)
        elif item.column() == self._COL_DUE:
            self.db.update_recommendation(int(rec_id), due_date=value)
        self._apply_filters()

    def refresh(self):
        """Called when the Rekommendationer page becomes visible
        (MainWindow._switch_view page==3) — same per-page refresh pattern
        HAZOPWorksheet.refresh()/EquipmentPanel.refresh() already use."""
        self.load()

    def load(self):
        """(Re)builds every row from the DB. Safe to call on an empty DB
        (all_recommendations() returns [] -> zero rows, no crash)."""
        was_sorting = self._table.isSortingEnabled()
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        try:
            maps = _build_position_maps(self.db)
            recs = self.db.all_recommendations()
            self._table.setRowCount(len(recs))
            for row, rec in enumerate(recs):
                rec_id = rec['id']
                desc = rec['description'] or 'Ny rekommendation'
                label = f"{rec['display_number']:03d}. {desc}"
                responsible = rec['responsible'] or _PLACEHOLDER
                due_date = rec['due_date'] or _PLACEHOLDER

                cons_ids = self.db.consequences_for_recommendation(rec_id)
                refs = [ref for ref in (
                    _reference_for_consequence(cid, maps) for cid in cons_ids
                ) if ref is not None]
                ref_text = ", ".join(refs) if refs else _PLACEHOLDER

                for col, value in ((self._COL_REC, label),
                                   (self._COL_RESPONSIBLE, responsible),
                                   (self._COL_DUE, due_date),
                                   (self._COL_REF, ref_text),
                                   (self._COL_STATUS, rec['status'] or 'Öppen')):
                    cell = QTableWidgetItem(value)
                    cell.setData(Qt.ItemDataRole.UserRole, rec_id)
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignLeft |
                                          Qt.AlignmentFlag.AlignTop)
                    # The visible number is managed by the catalog; only
                    # the description after it can be edited in this view.
                    if col == self._COL_REF:
                        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row, col, cell)
                due = QDateEdit(self._table)
                due.setCalendarPopup(True)
                due.setDisplayFormat('yyyy-MM-dd')
                due.setProperty('recommendation_id', rec_id)
                parsed = QDate.fromString(rec['due_date'] or '', 'yyyy-MM-dd')
                if parsed.isValid():
                    due.setDate(parsed)
                else:
                    due.setSpecialValueText('')
                    due.setDate(QDate.currentDate())
                due.dateChanged.connect(
                    lambda date, rid=rec_id: self.db.update_recommendation(
                        rid, due_date=date.toString('yyyy-MM-dd')))
                self._table.setCellWidget(row, self._COL_DUE, due)
                combo = QComboBox(self._table)
                combo.setEditable(True)
                combo.addItem('')
                for person in self.db.list_participants():
                    name = f"{person['first_name']} {person['last_name']}".strip()
                    if name:
                        combo.addItem(name)
                combo.setCurrentText(rec['responsible'] or '')
                combo.currentTextChanged.connect(
                    lambda text, rid=rec_id: self.db.update_recommendation(
                        rid, responsible=text.strip()))
                self._table.setCellWidget(row, self._COL_RESPONSIBLE, combo)
                status = QComboBox(self._table)
                status.addItems(_STATUS_VALUES)
                stored_status = rec['status'] or 'Öppen'
                if stored_status not in _STATUS_VALUES:
                    status.addItem(stored_status)
                status.setCurrentText(stored_status)
                status.currentTextChanged.connect(
                    lambda text, rid=rec_id: self.db.update_recommendation(
                        rid, status=text))
                self._table.setCellWidget(row, self._COL_STATUS, status)
            self._table.resizeRowsToContents()
        finally:
            self._table.blockSignals(False)
            self._table.setSortingEnabled(was_sorting)
            self._apply_filters()

    def _apply_filters(self):
        """Hide non-matching rows without rebuilding the table."""
        query = self._search.text().strip().casefold()
        selected_status = self._status_filter.currentText()
        visible = 0
        total = self._table.rowCount()
        for row in range(total):
            values = []
            for col in range(self._table.columnCount()):
                item = self._table.item(row, col)
                if item:
                    values.append(item.text())
                widget = self._table.cellWidget(row, col)
                if isinstance(widget, QComboBox):
                    values.append(widget.currentText())
            status_widget = self._table.cellWidget(row, self._COL_STATUS)
            row_status = status_widget.currentText() if isinstance(status_widget, QComboBox) else ''
            matches = (not query or query in ' '.join(values).casefold())
            matches = matches and (selected_status == _STATUS_ALL or row_status == selected_status)
            self._table.setRowHidden(row, not matches)
            visible += int(matches)
        self._count_label.setText(f'Visar {visible} av {total}')
