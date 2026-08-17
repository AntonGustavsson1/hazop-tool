#!/usr/bin/env python3
"""HAZOP worksheet page — split out of hazop.py 2026-08-17, see NOTES.md
"Förenkla koden + dela upp hazop.py i fler filer"."""

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox

from database import Database

class HAZOPWorksheet(QWidget):
    """Worksheet page: mirrors the full HAZOP hierarchy (Nod → Avvikelse →
    Orsak → Konsekvens → Barriärer) for one node at a time via a dropdown,
    or the entire study at once via "Visa samtliga noder".

    Reuses ScenarioTablePanel (the same row-building/editing logic used on
    the main P&ID page) instead of duplicating it in a second flat table —
    see load_all()/_all_nodes on ScenarioTablePanel.
    """

    def __init__(self, db: Database):
        super().__init__()
        # Deferred import: ScenarioTablePanel still lives in hazop.py, which
        # imports HAZOPWorksheet from this module — a module-level import
        # here would be circular. By the time a HAZOPWorksheet is actually
        # constructed, hazop.py has finished loading.
        from hazop import ScenarioTablePanel
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top bar: node picker + "show all nodes" checkbox
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Nod:"))
        self._node_combo = QComboBox()
        self._node_combo.setMinimumWidth(240)
        top_bar.addWidget(self._node_combo)
        self._all_nodes_cb = QCheckBox("Visa samtliga noder")
        top_bar.addWidget(self._all_nodes_cb)
        self._show_empty_dev_cb = QCheckBox("Visa avvikelser utan orsaker")
        top_bar.addWidget(self._show_empty_dev_cb)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        # Embedded scenario table (full hierarchy for selected node, or all nodes).
        # Lift the 380px height cap ScenarioTablePanel normally uses for the
        # P&ID page's bottom-splitter placement — here it owns the whole page,
        # so it should fill all available vertical space instead of leaving a
        # large blank gap below/around a height-capped table.
        self._table_panel = ScenarioTablePanel(db)
        self._table_panel.allow_full_height()
        # Avvikelse column should always be visible here — there's no separate
        # deviation-picker (only a node dropdown), and rows aren't distinguishable
        # by node/deviation otherwise when neither checkbox above is checked.
        self._table_panel.always_show_deviation_column()
        # "i worksheet behöver inte objekt kolumnen synas" (2026-08-13,
        # see NOTES.md) — Utrustning stays hidden here even in "Visa
        # samtliga noder" mode; the tag is already shown at the top of
        # each Orsak cell regardless.
        self._table_panel.hide_equipment_column()
        layout.addWidget(self._table_panel, 1)

        self._node_combo.currentIndexChanged.connect(self._on_node_combo_changed)
        self._all_nodes_cb.toggled.connect(self._on_all_nodes_toggled)
        self._show_empty_dev_cb.toggled.connect(self._table_panel.set_show_empty_deviations)

        # item_selected (row click -> update right-hand properties ribbon)
        # is not wired for v1: the Worksheet page has no properties ribbon
        # of its own, and piping it to MainWindow's ribbon would couple this
        # page to P&ID-page-only UI state for little benefit.

        self._populate_node_combo()

    def _populate_node_combo(self):
        """Refill the node dropdown from the DB, preserving the current selection if possible."""
        current_id = self._node_combo.currentData() if self._node_combo.count() else None
        self._node_combo.blockSignals(True)
        try:
            self._node_combo.clear()
            for node in self.db.nodes():
                self._node_combo.addItem(node['name'] or f"Nod {node['id']}", node['id'])
            if current_id is not None:
                idx = self._node_combo.findData(current_id)
                if idx >= 0:
                    self._node_combo.setCurrentIndex(idx)
        finally:
            self._node_combo.blockSignals(False)

    def _on_node_combo_changed(self, idx):
        if self._all_nodes_cb.isChecked():
            return  # combo is disabled in all-nodes mode; ignore stray signals
        node_id = self._node_combo.currentData()
        if node_id is not None:
            self._table_panel.load_node(node_id)

    def _on_all_nodes_toggled(self, checked):
        self._node_combo.setEnabled(not checked)
        if checked:
            self._table_panel.load_all()
        else:
            node_id = self._node_combo.currentData()
            if node_id is not None:
                self._table_panel.load_node(node_id)

    def refresh(self):
        """Called when the Worksheet page becomes visible (MainWindow._switch_view page==1)."""
        self._populate_node_combo()
        if self._all_nodes_cb.isChecked():
            self._table_panel.load_all()
        elif self._node_combo.count() > 0:
            node_id = self._node_combo.currentData()
            if node_id is not None:
                self._table_panel.load_node(node_id)

