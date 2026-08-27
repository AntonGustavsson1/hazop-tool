#!/usr/bin/env python3
"""Rekommendationer page — new top-level nav page added 2026-08-26 (see
NOTES.md), inserted right after Worksheet. Read-only overview of the whole
recommendation catalog (Database.all_recommendations(), id order): one row
per catalog ENTRY, not per consequence link, so a recommendation reused
across several causes (consequence_recommendations is many-to-many, see
database.py) appears once, not duplicated.

Column 1 mirrors the "R-XXX. description" convention already used for the
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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from database import Database

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


class RecommendationsPanel(QWidget):
    """Simple three-column, read-only QTableWidget: every recommendation in
    the catalog (column 1, "R-XXX. <description>"), its responsible person
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
    _COL_REF = 2

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(
            ["Rekommendation", "Ansvarig person",
             "Referens (studie.nod.avvikelse.orsak.konsekvens)"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setWordWrap(True)
        # Native click-to-sort is effectively free with a QTableWidget and
        # was explicitly called out as fine to include — no custom
        # filtering/search UI beyond that, per the request.
        self._table.setSortingEnabled(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self._COL_REC, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_RESPONSIBLE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_REF, QHeaderView.ResizeMode.ResizeToContents)
        # Qt quirk (verified empirically, not from memory): a freshly
        # created QTableWidget's header already carries an implicit
        # "column 0, DESCENDING" sort indicator even though nothing ever
        # called setSortIndicator/sortItems — re-enabling setSortingEnabled
        # after a bulk-populate (see load()'s was_sorting dance) silently
        # applies THAT indicator, reversing the catalog id order the
        # request asked for ("Lista alla rekommendationer i kolumn 1", in
        # db.all_recommendations() order). Set an explicit ascending
        # indicator on column 0 up front so the default view matches
        # catalog id order; clicking either header still re-sorts freely.
        header.setSortIndicator(self._COL_REC, Qt.SortOrder.AscendingOrder)
        layout.addWidget(self._table)

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
        try:
            maps = _build_position_maps(self.db)
            recs = self.db.all_recommendations()
            self._table.setRowCount(len(recs))
            for row, rec in enumerate(recs):
                rec_id = rec['id']
                desc = rec['description'] or 'Ny rekommendation'
                label = f"R-{rec_id:03d}. {desc}"
                responsible = rec['responsible'] or _PLACEHOLDER

                cons_ids = self.db.consequences_for_recommendation(rec_id)
                refs = [ref for ref in (
                    _reference_for_consequence(cid, maps) for cid in cons_ids
                ) if ref is not None]
                ref_text = ", ".join(refs) if refs else _PLACEHOLDER

                self._table.setItem(row, self._COL_REC, QTableWidgetItem(label))
                self._table.setItem(
                    row, self._COL_RESPONSIBLE, QTableWidgetItem(responsible))
                self._table.setItem(row, self._COL_REF, QTableWidgetItem(ref_text))
            self._table.resizeRowsToContents()
        finally:
            self._table.setSortingEnabled(was_sorting)
