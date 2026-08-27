#!/usr/bin/env python3
"""P&ID panel — EquipmentDeviationBar + PIDPanel, split out of pid_viewer.py
2026-08-18, see NOTES.md "Förenkla koden + dela upp hazop.py i fler
filer". Moved as whole classes (not split internally) — same rationale as
pid_graphics_view.py."""

import re
import os
import json
import logging
import datetime
import shutil
import tempfile
from pathlib import Path
from functools import partial

import symbol_geometry
import equipment_detection

from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QPointF, QRectF, QTimer
from PyQt6.QtGui import (
    QBrush, QColor, QDrag, QFont, QKeySequence, QPen, QShortcut,
)

from pid_graphics_view import PIDGraphicsView
from equipment_detection import (
    _pick_best_tag, _rotate_words, _spatial_combine, find_tag_near_point,
)
from ui_helpers import _equipment_type_options, _make_tag_completer
from pid_viewer import (
    fitz, CONFIG, HAS_PYMUPDF, Z_TEMP,
    resolve_tree_context_color,
    MODE_NAV, MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE,
    MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT, MODE_MARKUP_SELECT,
    MODE_RED_MARKUP_SYMBOL, MODE_BOARD_LAYOUT,
    MODE_PICK_REF_TAG, MODE_ANNOTATION,
    _icon, _vline, _draw_pid_marker, _hex_to_fitz_rgb, _sheet_ref_variants,
    _equip_prefix_from_tag, _obj_type_matches, ensure_ocr_available,
    _MEDIA_COLORS,
    ConnectorDotItem, PIDImportDialog,
    SimilarSymbolSearchDialog, SymbolTemplatePickerDialog,
    EquipmentMarkerReviewDialog, _ClusterPreviewCanvas,
    ParallelTagScanWorker, PageProgressDialog, EquipmentTagSearchWorker,
    scan_pdf_for_equipment, apply_scan_result_to_equipment_catalog,
    upsert_identified_tags_from_scan, resolve_ocr_scan_choice, ocr_status,
)


class _EquipmentSearchPopup(QWidget):
    """Small, live-filtered popup for the defined P&ID equipment model.

    Rows come from ``equipment_catalog`` joined to ``equipment_markers``;
    consequently an arbitrary word in the PDF can never become a result.
    The marker coordinates are carried on each list item for immediate
    navigation when the user clicks it.
    """
    result_chosen = pyqtSignal(int, int, float, float)

    def __init__(self, panel):
        super().__init__(panel, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._panel = panel
        self.setObjectName("equipmentSearchPopup")
        self.setFixedWidth(330)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(5)
        title = QLabel("Sök definierat objekt")
        title.setStyleSheet("font-weight:600;")
        outer.addWidget(title)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Tagg, typ eller ID …")
        self._edit.textChanged.connect(self._filter)
        outer.addWidget(self._edit)
        self._list = QListWidget()
        self._list.setMinimumHeight(90)
        self._list.itemClicked.connect(self._choose)
        outer.addWidget(self._list)

    def show_near(self, global_pos):
        self.adjustSize()
        screen = QApplication.screenAt(global_pos)
        if screen:
            rect = screen.availableGeometry()
            x = min(global_pos.x(), rect.right() - self.width())
            y = min(global_pos.y(), rect.bottom() - self.height())
            global_pos = QPoint(max(rect.left(), x), max(rect.top(), y))
        self.move(global_pos)
        self.show()
        self._edit.setFocus()
        self._edit.selectAll()

    def refresh(self):
        self._rows = []
        # A catalog row without a marker has no navigable P&ID position and
        # is intentionally excluded.  One row per defined equipment object;
        # duplicate marker shapes for the same object use the first marker.
        try:
            rows = self._panel.db.conn.execute(
                "SELECT ec.id, ec.tag, ec.prefix, ec.equipment_type, "
                "em.id AS marker_id, em.pid_page, em.x, em.y "
                "FROM equipment_catalog ec "
                "JOIN equipment_markers em ON em.equipment_id=ec.id "
                "WHERE COALESCE(ec.include,1)=1 "
                "GROUP BY ec.id ORDER BY UPPER(COALESCE(ec.tag,'')), ec.id"
            ).fetchall()
            self._rows = [dict(r) for r in rows]
        except Exception:
            logging.exception("Could not load defined P&ID objects for search")
        self._filter(self._edit.text())

    def _filter(self, text):
        query = (text or '').strip().casefold()
        self._list.clear()
        matches = []
        for row in getattr(self, '_rows', []):
            hay = ' '.join(str(row.get(k) or '') for k in
                           ('tag', 'prefix', 'equipment_type', 'id')).casefold()
            if not query or query in hay:
                matches.append(row)
        if not matches:
            empty = QListWidgetItem("Inga definierade objekt matchar sökningen")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(empty)
            return
        for row in matches:
            tag = row.get('tag') or row.get('prefix') or '(utan tagg)'
            typ = row.get('equipment_type') or 'Okänd typ'
            item = QListWidgetItem(f"{tag}  ·  {typ}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self._list.addItem(item)

    def _choose(self, item):
        row = item.data(Qt.ItemDataRole.UserRole)
        if not row:
            return
        self.result_chosen.emit(int(row['marker_id']), int(row['pid_page']),
                                float(row['x']), float(row['y']))
        self.hide()

class _DeviationChecklist(QWidget):
    """Embeddable per-node deviation checklist — checking off which of the
    node's deviations apply to a piece of equipment ("Lågt flöde", "Högt
    flöde", etc.), auto-creating a first suggested cause on check. Factored
    out of EquipmentDeviationBar (2026-08-18, see NOTES.md "kombinerad
    placeringsmeny") so EquipmentPlacementPopup can embed the exact same
    checklist logic alongside its own tag/typ fields, instead of a second
    ~200-line copy of the same matching/suggestion code. No window flags —
    a plain child widget; EquipmentDeviationBar below wraps it in a
    self-dismissing popup shell, unchanged from the user's perspective.

    The checklist is driven by the equipment's NODE's own already-defined
    deviations (2026-08-13, see NOTES.md "alla avvikelser i noden") — a
    generic standard-catalog suggestion list only kicks in as a bootstrap
    fallback for a brand new node with no deviations of its own yet."""

    # A deviation was newly created (or already existed) for this equipment —
    # PIDPanel listens to refresh the marker's colour/badge and the tree/worksheet.
    deviation_added = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    # A previously-checked deviation was unchecked and deleted (see
    # "Kryssrutan ska gå att av-/aktivera", NOTES.md) — same refresh needs
    # as deviation_added (marker badge count, tree, worksheet).
    deviation_removed = pyqtSignal(int, int)   # (deviation_id, equipment_id)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._equipment_id = None
        self._checklist_checkboxes = []   # display order, for number-key shortcuts
        # Set by the embedding widget (PIDPanel, via EquipmentDeviationBar's
        # own _create_cause_fn passthrough) to PIDPanel._create_cause_for_bar
        # — same auto-suggest-a-cause path the old inline cause combo used
        # on first check, just without a dropdown to change it afterward
        # (do that in the scenario table).
        self._create_cause_fn = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._hint_lbl = QLabel("Utrustningen har ingen nod än — välj/skapa\nen nod i trädet först.")
        self._hint_lbl.setStyleSheet("color:#8D9299; font-style:italic; font-size:10px;")
        self._hint_lbl.setWordWrap(True)
        outer.addWidget(self._hint_lbl)

        self._checklist_host = QWidget()
        self._checklist_layout = QVBoxLayout(self._checklist_host)
        self._checklist_layout.setContentsMargins(0, 0, 0, 0)
        self._checklist_layout.setSpacing(2)
        self._checklist_scroll = QScrollArea()
        self._checklist_scroll.setWidget(self._checklist_host)
        self._checklist_scroll.setWidgetResizable(True)
        self._checklist_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Real height is set per-popup by the embedding widget's own
        # show_near()-equivalent, from available screen space (2026-08-13
        # feedback: a fixed 220px looked "väldigt kort" once the list grew
        # to every deviation in the node, which can be 16+ rows) — this
        # fallback only matters before that first sizing pass.
        self._checklist_scroll.setMaximumHeight(220)
        outer.addWidget(self._checklist_scroll)

        # Number-key shortcuts (1-9, see NOTES.md "snabbknappar") — a
        # QShortcut with WidgetWithChildrenShortcut fires as long as focus
        # is anywhere inside the embedding popup (including these
        # checkboxes), unlike a plain keyPressEvent override which only
        # fires when this widget itself literally has focus.
        for i in range(1, 10):
            sc = QShortcut(QKeySequence(str(i)), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda n=i: self._toggle_checkbox_by_number(n))

    def _toggle_checkbox_by_number(self, number):
        idx = number - 1
        if 0 <= idx < len(self._checklist_checkboxes):
            cb = self._checklist_checkboxes[idx]
            if cb.isEnabled():
                cb.setChecked(True)

    def natural_height(self):
        """Uncapped natural height of the checklist content — the
        embedding popup's own sizing pass (e.g. EquipmentDeviationBar.
        show_near()) uses this to size its scroll area against available
        screen space."""
        return self._checklist_host.sizeHint().height()

    def set_max_height(self, h):
        self._checklist_scroll.setMaximumHeight(h)

    def load(self, equipment_id, active_node_id=None):
        """Populate the checklist for equipment_id. Does not show/position
        anything itself — the embedding widget owns that.

        `active_node_id` (PIDPanel._active_node_id — the node the user is
        already working in elsewhere in the app, see NOTES.md) is used to
        skip the manual node-picking step entirely when this equipment has
        no node yet: it's assigned immediately, not just pre-filled as a
        suggestion, so checking a deviation works right away — "jag vill
        inte behöva välja nod varje gång" (explicit user request)."""
        eq = self.db.get_equipment_by_id(equipment_id)
        if not eq:
            return
        if eq.get('node_id') is None and active_node_id is not None \
                and self.db.get_node(active_node_id) is not None:
            self.db.set_equipment_node(equipment_id, active_node_id)
        self._equipment_id = equipment_id
        self._rebuild_checklist()

    def _rebuild_checklist(self):
        while self._checklist_layout.count():
            item = self._checklist_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        eq = self.db.get_equipment_by_id(self._equipment_id) if self._equipment_id else None
        if not eq:
            return
        node_id = eq.get('node_id')
        self._hint_lbl.setVisible(node_id is None)

        comp_type = eq['equipment_type'] or ''
        obj_id = self._resolve_object_id(comp_type)

        # Show every deviation already defined in this node (2026-08-13:
        # "jag vill att alla avvikelser i noden kommer upp som val inkl
        # nummer") — a node's deviations are usually decided up front (guide
        # words), so each piece of equipment in it should be checked
        # against ALL of them, not a generic standard-catalog subset for
        # its own equipment TYPE. The old type-based suggestion chain
        # (object → comp_type → full catalogue) only kicks in as a
        # bootstrap fallback for a brand new node that has no deviations
        # of its own yet, so the checklist still isn't empty.
        descriptions = []
        seen = set()

        def _add(desc):
            if desc not in seen:
                seen.add(desc)
                descriptions.append(desc)

        if node_id is not None:
            for d in self.db.deviations(node_id):
                _add(d['description'])
        if not descriptions:
            if obj_id is not None:
                for sd in self.db.deviations_for_object(obj_id):
                    _add(sd['description'])
        if not descriptions:
            rows = self.db.standard_causes_for_comp_type(comp_type) if comp_type else []
            for r in rows:
                _add(r['deviation_name'])
        if not descriptions:
            for sd in self.db.standard_deviations():
                _add(sd['description'])

        # Full row (not just a name-in-set check) so an already-checked
        # deviation's already-saved cause can be looked up and shown —
        # see _build_deviation_row's existing_dev handling.
        existing_by_desc = {d['description']: d
                             for d in self.db.deviations_for_equipment(self._equipment_id)}

        # Tracked in display order so number-key shortcuts (1-9, see
        # keyPressEvent) can toggle the matching row without the mouse —
        # explicit user request ("snabbknappar 1, 2.."). The number LABEL
        # is shown for every row regardless of count (2026-08-13); only
        # the keyboard shortcut itself is capped at 1-9 (a real key limit).
        self._checklist_checkboxes = []
        for i, desc in enumerate(descriptions, 1):
            row_w = self._build_deviation_row(desc, existing_by_desc.get(desc),
                                              enabled=node_id is not None, number=i,
                                              obj_id=obj_id)
            self._checklist_layout.addWidget(row_w)

    def _resolve_object_id(self, comp_type):
        """Match equipment_catalog.equipment_type against standard_objects
        (the richer taxonomy StandardCausesPickerPopup already uses) so the
        checklist can offer the same breadth of causes — see NOTES.md."""
        if not comp_type:
            return None
        for o in self.db.standard_objects():
            if _obj_type_matches(comp_type, o['name']):
                return o['id']
        return None

    def _resolve_std_deviation_id(self, description):
        """Best-effort match of a real per-node deviation's description
        text against the standard_deviations catalogue, so the richer
        object-based cause suggestions (standard_causes_for_object) can
        still be tried for it — returns None for a custom/freeform
        deviation with no catalogue match, which just falls through to
        the plain comp_type-based lookup in _build_deviation_row."""
        row = self.db.conn.execute(
            "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
            (description,)).fetchone()
        return row[0] if row else None

    def _build_deviation_row(self, description, existing_dev, enabled, number, obj_id=None):
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        checked = existing_dev is not None
        num_lbl = QLabel(f"{number}.")
        num_lbl.setFixedWidth(20)
        num_lbl.setStyleSheet("color:#8D9299;")
        row.addWidget(num_lbl)
        cb = QCheckBox(description)
        cb.setChecked(checked)
        cb.setEnabled(enabled)   # can be checked AND unchecked — see NOTES.md "av-/aktivera"
        row.addWidget(cb)
        self._checklist_checkboxes.append(cb)

        # Top-suggested cause for this deviation, used to auto-create a
        # first cause on check (2026-08-12, see NOTES.md — the cause
        # combo/frequency editing this used to render inline is gone;
        # editing a cause's text/frequency now happens in the scenario
        # table, once it exists).
        eq = self.db.get_equipment_by_id(self._equipment_id)
        comp_type = eq['equipment_type'] if eq else ''
        std_dev_id = self._resolve_std_deviation_id(description)
        causes = []
        if obj_id is not None and std_dev_id is not None:
            causes = self.db.standard_causes_for_object(std_dev_id, obj_id)
        if not causes and comp_type:
            causes = self.db.standard_causes_for_comp_type(comp_type, description)
        if not causes and comp_type:
            causes = self.db.standard_causes_for_comp_type(comp_type)
        # standard_causes_for_object already returns plain dicts;
        # standard_causes_for_comp_type returns raw sqlite3.Row objects,
        # which don't support .get() — normalise both to dicts before any
        # .get() call here or in _activate_deviation's cause['frequency'].
        causes = [dict(c) for c in causes]
        if std_dev_id is not None:
            causes = [c for c in causes if c.get('deviation_id', std_dev_id) == std_dev_id]

        cb.toggled.connect(
            lambda on, desc=description, box=cb, cs=causes:
                self._on_deviation_toggled(desc, on, box, cs))
        return row_w

    def _on_deviation_toggled(self, description, checked, checkbox, causes):
        if checked:
            self._activate_deviation(description, checkbox, causes)
        else:
            self._deactivate_deviation(description, checkbox)

    def _activate_deviation(self, description, checkbox, causes):
        eq = self.db.get_equipment_by_id(self._equipment_id)
        node_id = eq.get('node_id') if eq else None
        if node_id is None:
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
            return
        dev_id = self.db.get_or_create_deviation(node_id, description, equipment_id=self._equipment_id)
        self.deviation_added.emit(dev_id, self._equipment_id)
        # Auto-create the top-suggested cause right away, same as before —
        # just without a dropdown here to change it; that's a scenario-
        # table edit now (see NOTES.md "Resten av valen får jag nog göra
        # nere i hazop scenario").
        if causes and self._create_cause_fn is not None:
            top = causes[0]
            self._create_cause_fn(dev_id, eq.get('equipment_type') or '', eq.get('tag') or '',
                                  '', top.get('frequency'))

    def _deactivate_deviation(self, description, checkbox):
        """Kryssrutan ska gå att av-/aktivera (NOTES.md): unchecking now
        actually removes the deviation (db.delete_deviation — the same
        cascading delete the tree's own "Ta bort" already uses, so any
        causes/consequences/safeguards/markers under it go too), instead
        of the old v1 behavior where a checked box was locked forever.
        Confirms first if there's real data to lose, matching the exact
        confirmation pattern ScenarioTablePanel._delete_current_item
        already uses for "Ta bort orsak"/"Ta bort konsekvens".

        Softer confirmation (2026-08-09, see NOTES.md): the old message
        only counted causes ("har N orsak(er) kopplade"), understating
        the real cascade for a deviation whose causes each have their own
        consequences/safeguards — a checkbox uncheck looks like a light
        action but can silently delete a whole scenario tree. The message
        now spells out the full count and states plainly that it can't be
        undone, matching how destructive this action actually is."""
        eq = self.db.get_equipment_by_id(self._equipment_id)
        node_id = eq.get('node_id') if eq else None
        dev_id = (self.db.get_or_create_deviation(node_id, description, equipment_id=self._equipment_id)
                  if node_id is not None else None)
        if dev_id is None:
            return

        causes = self.db.causes_for_deviation(dev_id)
        n_causes = len(causes)
        if n_causes:
            n_cons = 0
            n_sg = 0
            for cause in causes:
                conss = self.db.consequences(cause['id'])
                n_cons += len(conss)
                for cons in conss:
                    n_sg += len(self.db.safeguards(cons['id']))
            parts = [f"{n_causes} orsak(er)"]
            if n_cons:
                parts.append(f"{n_cons} konsekvens(er)")
            if n_sg:
                parts.append(f"{n_sg} barriär(er)")
            reply = QMessageBox.question(
                self, "Ta bort avvikelse",
                f"Avvikelsen '{description}' tas bort tillsammans med "
                f"{', '.join(parts)}. Detta går inte att ångra.\n\n"
                "Vill du fortsätta?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                checkbox.blockSignals(True)
                checkbox.setChecked(True)
                checkbox.blockSignals(False)
                return

        self.db.delete_deviation(dev_id)
        self.deviation_removed.emit(dev_id, self._equipment_id)


class EquipmentDeviationBar(QWidget):
    """Small popup that appears right next to a clicked equipment marker on
    the P&ID and lets the user check off which of the node's deviations
    apply to it. Auto-dismisses on an outside click (Qt.WindowType.Popup),
    matching hazop.py's own small popups (RiskMatrixPopup etc.) — replaces
    the earlier persistent bottom-docked bar + inline cause/frequency-combo
    editing (2026-08-12, see NOTES.md: "en liten popup på P&ID viewer som
    kommer upp tillfälligt ... Resten av valen får jag nog göra nere i
    hazop scenario" — the rest of the editing already happens in the
    scenario table once a cause exists, so this popup's job is just the
    deviation checklist).

    A thin title+hint shell around the shared `_DeviationChecklist`
    (2026-08-18, see that class's own docstring) — this class's own job is
    now just the popup chrome/positioning, not the checklist logic itself.

    2026-08-12 through 2026-08-25 (see NOTES.md): retyping the equipment
    (comp_type) and reassigning it to a different node used to be handled
    only via right-click "Redigera objekt" / dragging it in the tree,
    deliberately dropped from here to keep this popup small. Reinstated
    2026-08-25 at Anton's explicit request ("Om jag vänsterklickar på ett
    objekt på pid viewer ska man kunna editera objektnamn (tag) och
    objekttyp. Man ska även kunna klicka på deleteknappen för att ta
    bort.") — this is now the combined tag+typ+avvikelse+delete editor for
    an EXISTING object, mirroring what EquipmentPlacementPopup already is
    for a brand-new one. Tag/type edits commit live (editingFinished /
    combo activation), same convention as every other popup in this file;
    unlike EquipmentPlacementPopup's placeholder-merge dance, editing an
    EXISTING object's tag to collide with another one just shows the same
    informational duplicate hint the right-click editor already accepted
    without blocking — there's no blank placeholder row here that could
    need merging away.

    An "add a brand-new object" button briefly lived here too (2026-08-12)
    but was removed the same day it shipped: placing a new object doesn't
    belong in a popup anchored to an EXISTING one — right-click "🔧 Objekt"
    and the rubber-band menu already cover that (now via
    EquipmentPlacementPopup, see NOTES.md "kombinerad placeringsmeny").
    """

    deviation_added   = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    deviation_removed = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    equipment_updated  = pyqtSignal(int)       # equipment_id — tag/type edited
    equipment_deleted  = pyqtSignal(int)       # equipment_id — "Ta bort" confirmed

    def __init__(self, db, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._db = db
        self._marker_id = None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet(
            "EquipmentDeviationBar { background:#FFFFFF; border:1px solid #CFD1CE;"
            " border-radius:6px; }")
        self.setMaximumWidth(260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        _small = "font-size:10px;"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        self._title_lbl = QLabel("<b>Objekt</b>")
        self._title_lbl.setStyleSheet("font-size:11px; color:#8D9299;")
        outer.addWidget(self._title_lbl)

        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._tag_edit = QLineEdit()
        self._tag_edit.setPlaceholderText("t.ex. P-101")
        self._tag_edit.setFixedHeight(CONFIG['H_SMALL_BTN'])
        self._tag_edit.setStyleSheet(_small)
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        self._tag_edit.textChanged.connect(self._update_dup_hint)
        self._tag_edit.editingFinished.connect(self._commit_tag)
        tag_lbl = QLabel("Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_SMALL_BTN'])
        self._type_cb.setStyleSheet(_small)
        self._type_cb.addItems(_equipment_type_options(db))
        self._type_cb.activated.connect(self._commit_type)
        typ_row = QHBoxLayout()
        typ_row.setSpacing(4)
        typ_row.addWidget(self._type_cb)
        add_type_btn = QPushButton("+")
        add_type_btn.setFixedSize(CONFIG['H_SMALL_BTN'], CONFIG['H_SMALL_BTN'])
        add_type_btn.setStyleSheet(_small)
        add_type_btn.setToolTip("Lägg till en ny objekttyp")
        add_type_btn.clicked.connect(self._add_new_type)
        typ_row.addWidget(add_type_btn)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, typ_row)
        outer.addLayout(form)

        self._dup_hint = QLabel("")
        self._dup_hint.setStyleSheet("font-size:9px; color:#b8860b;")
        self._dup_hint.setWordWrap(True)
        outer.addWidget(self._dup_hint)

        self._checklist = _DeviationChecklist(db, self)
        self._checklist.deviation_added.connect(self.deviation_added)
        self._checklist.deviation_removed.connect(self.deviation_removed)
        outer.addWidget(self._checklist)

        self._delete_btn = QPushButton("Ta bort")
        self._delete_btn.setIcon(_icon('delete'))
        self._delete_btn.setStyleSheet(_small)
        self._delete_btn.setToolTip("Ta bort objektet och dess markörer från P&ID")
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        del_row = QHBoxLayout()
        del_row.addStretch()
        del_row.addWidget(self._delete_btn)
        outer.addLayout(del_row)

    @property
    def db(self):
        return self._db

    @db.setter
    def db(self, value):
        """Reassigning .db (MainWindow._reload_all_panels() after a
        project swap, see hazop.py) must also update the embedded
        checklist's own db reference — it keeps a SEPARATE db attribute
        since the _DeviationChecklist extraction (2026-08-18, see
        NOTES.md "kombinerad placeringsmeny"). Without this, a project
        reload left the checklist pointed at the OLD, now-closed
        database, crashing the next click on an existing equipment
        marker with sqlite3.ProgrammingError — same bug class
        test_equipment_deviation_bar_gets_new_db already guards this
        class against as a whole."""
        self._db = value
        self._checklist.db = value

    @property
    def equipment_id(self):
        return self._checklist._equipment_id

    @property
    def _equipment_id(self):
        """Kept for existing direct-attribute access (see
        EquipmentObjectPlacementTests) — the embedded checklist is the
        real owner now."""
        return self._checklist._equipment_id

    @property
    def marker_id(self):
        return self._marker_id

    @property
    def _create_cause_fn(self):
        return self._checklist._create_cause_fn

    @_create_cause_fn.setter
    def _create_cause_fn(self, fn):
        self._checklist._create_cause_fn = fn

    def load(self, equipment_id, marker_id, active_node_id=None):
        """Populate the popup for the equipment behind `marker_id` (an
        equipment_markers.id — the caller already has this from the
        marker_clicked signal). Does not show/position the popup itself —
        call show_near() right after. See _DeviationChecklist.load() for
        the active_node_id auto-assign behavior."""
        self._marker_id = marker_id
        self._checklist.load(equipment_id, active_node_id)
        eq = self.db.get_equipment_by_id(equipment_id)
        if not eq:
            return
        self._tag_edit.setText(eq.get('tag') or '')
        self._dup_hint.setText("")
        comp_type = eq.get('equipment_type') or ''
        idx = self._type_cb.findText(comp_type) if comp_type else -1
        if comp_type and idx < 0:
            self._type_cb.addItem(comp_type)
            idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(idx if idx >= 0 else -1)

    def _update_dup_hint(self, text):
        """Live (textChanged, not textEdited — 2026-08-25, see NOTES.md
        "Dublett-taggens varningstext ... uppdateras nu live") duplicate
        check: informational only, no merge/confirm dance — see class
        docstring for why that's the right call for an EXISTING object."""
        tag = text.strip().upper()
        existing = self.db.get_equipment_by_tag(tag) if tag else None
        if existing and existing['id'] != self.equipment_id:
            self._dup_hint.setText(
                f"ℹ️ \"{existing['tag']}\" finns redan i katalogen "
                f"({existing.get('equipment_type') or '?'}).")
        else:
            self._dup_hint.setText("")

    def _commit_tag(self):
        eq = self.db.get_equipment_by_id(self.equipment_id)
        if not eq:
            return
        tag = self._tag_edit.text().strip().upper()
        if self._tag_edit.text() != tag:
            self._tag_edit.blockSignals(True)
            self._tag_edit.setText(tag)
            self._tag_edit.blockSignals(False)
        pfx = _equip_prefix_from_tag(tag) if tag else (eq.get('prefix') or '')
        self.db.update_equipment_item(
            self.equipment_id, tag, pfx, eq.get('equipment_type') or '', eq.get('description') or '')
        self.equipment_updated.emit(self.equipment_id)

    def _commit_type(self, _index=None):
        eq = self.db.get_equipment_by_id(self.equipment_id)
        if not eq:
            return
        comp_type = self._type_cb.currentText().strip()
        self.db.update_equipment_item(
            self.equipment_id, eq.get('tag') or '', eq.get('prefix') or '', comp_type, eq.get('description') or '')
        # Rebuild so per-row standard-cause suggestions (keyed on
        # comp_type) use the type just picked — same reasoning as
        # EquipmentPlacementPopup._commit_type.
        self._checklist._rebuild_checklist()
        self.equipment_updated.emit(self.equipment_id)

    def _add_new_type(self):
        """Same behavior as EquipmentPlacementPopup._add_new_type — also
        registers the name as a Standardobjekt right away."""
        name, ok = QInputDialog.getText(self, "Ny objekttyp", "Namn:")
        name = (name or '').strip()
        if not ok or not name:
            return
        idx = self._type_cb.findText(name)
        if idx < 0:
            self._type_cb.addItem(name)
            idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(idx)
        exists = self.db.conn.execute(
            "SELECT 1 FROM standard_objects WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not exists:
            self.db.add_standard_object(name)
        self._commit_type()

    def _on_delete_clicked(self):
        """2026-08-25, see NOTES.md — "Man ska även kunna klicka på
        deleteknappen för att ta bort." delete_equipment_item() cascades
        the deletion to every equipment_markers row pointing at it
        (ON DELETE CASCADE), so this removes the object from every P&ID
        page it was placed on, not just the marker that was clicked."""
        eq = self.db.get_equipment_by_id(self.equipment_id)
        label = (eq.get('tag') or 'objektet') if eq else 'objektet'
        reply = QMessageBox.question(
            self, "Ta bort", f"Ta bort {label}? Objektet och dess markörer på P&ID tas bort.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        equipment_id = self.equipment_id
        self.db.delete_equipment_item(equipment_id)
        self.close()
        self.equipment_deleted.emit(equipment_id)

    def show_near(self, global_pos):
        """Show the popup anchored near global_pos (a QPoint), clamped to
        stay on-screen — same screen-clamped positioning hazop.py's own
        popups (RiskMatrixPopup etc.) already use. Clicking anywhere else
        closes it automatically (Qt.WindowType.Popup).

        The checklist's scroll area is sized to whatever room is actually
        available (opening downward or upward, whichever side of
        global_pos has more space) instead of a fixed height (2026-08-13
        feedback: "rulllistan väldigt kort på en liten skärm" — on a small
        screen the old fixed 220px cap left very little of the popup's
        already-small screen share usable, on top of now listing every
        deviation in the node instead of a short suggestion subset)."""
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()

        # Measure the checklist's true, uncapped height first so the cap
        # below never reserves blank space for a short list.
        self._checklist.set_max_height(16777215)
        self.adjustSize()
        natural_total_h = self.sizeHint().height()
        natural_scroll_h = self._checklist.natural_height()
        chrome_h = natural_total_h - natural_scroll_h   # title + margins

        space_below = scr.bottom() - global_pos.y()
        space_above = global_pos.y() - scr.top()
        open_below = space_below >= space_above
        available = (space_below if open_below else space_above) - chrome_h - 12
        scroll_h = max(120, min(natural_scroll_h, available))
        self._checklist.set_max_height(int(scroll_h))
        self.adjustSize()

        pw, ph = self.sizeHint().width(), self.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = global_pos.y() if open_below else global_pos.y() - ph
        y = min(max(scr.top(), y), scr.bottom() - ph)
        self.move(max(scr.left(), x), y)
        self.show()
        self.setFocus()   # so number-key shortcuts work immediately


class EquipmentPlacementPopup(QWidget):
    """Combined tag + typ + avvikelse-checklist popup shown the moment a
    NEW equipment object is placed on the P&ID — replaces the previous
    two separate, sequential steps (EquipmentTagPopup's modal tag/typ
    dialog, THEN EquipmentDeviationBar's checklist right after) with one
    view (2026-08-18, see NOTES.md "kombinerad placeringsmeny": "Idag
    sker detta i två steg men jag tror det snabbar upp flödet med bara
    en meny").

    No OK/Avbryt buttons (same live-commit convention CauseTagPopup
    already established this session, tree_panel.py) — the tag field
    commits on Enter/focus-out, the type combo commits the moment a
    selection is made, and each deviation checkbox commits itself
    immediately (via the embedded _DeviationChecklist, unchanged).
    place_equipment_marker() has already created the equipment_catalog
    row + marker before this popup is constructed, so every field here
    edits a REAL, already-existing row from the first keystroke.

    Shows a "⏳ Söker tagg…" hint while place_equipment_marker()'s
    background EquipmentTagSearchWorker is still running (tag was blank
    at placement time) — set_detected_tag() fills the tag field once the
    search resolves (or times out), but never overwrites text the user
    already typed themselves.

    deviation_added/deviation_removed (forwarded straight from the
    embedded _DeviationChecklist, same as EquipmentDeviationBar already
    does) — PIDPanel.place_equipment_marker() connects these to
    _on_equipment_deviation_added/_removed so a checkbox ticked here
    refreshes the tree and HAZOP scenario table exactly like the
    existing-marker popup already does. Missing this connection was a
    real bug (2026-08-18 follow-up: "Jag ser dessutom inget i hazop
    scenario när jag klickar") — the deviation/cause got created in the
    database just fine, nothing ever told the rest of the app to redraw."""

    deviation_added   = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    deviation_removed = pyqtSignal(int, int)   # (deviation_id, equipment_id)

    def __init__(self, db, equipment_id, marker_id, parent=None, simple=False):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.db = db
        self._equipment_id = equipment_id
        self._marker_id = marker_id
        self._tag_edited_by_user = False
        # Set the moment "Skapa dublett" is clicked for a given tag string
        # (2026-08-25, see NOTES.md) — lets that exact tag bypass the
        # auto-merge in _commit_tag once the user has explicitly said they
        # want a real duplicate, without needing to re-click on every
        # subsequent commit as long as the text hasn't changed.
        self._dup_confirmed_tag = None
        # `simple` (2026-08-24, see NOTES.md "Åtta UX/logik-förbättringar")
        # — the rubber-band placement flow (PIDPanel.place_equipment_marker
        # when pdf_rect is not None) now only creates the object itself:
        # no embedded deviation checklist. The plain right-click "🔧
        # Objekt" placement (no drawn rectangle) keeps the full popup with
        # the checklist, unchanged.
        self._simple = simple
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet(
            "EquipmentPlacementPopup { background:#FFFFFF; border:1px solid #CFD1CE;"
            " border-radius:6px; }")
        self.setMaximumWidth(260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        _small = "font-size:10px;"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        title = QLabel("<b>Nytt objekt</b>" if simple else "<b>Nytt objekt på P&amp;ID</b>")
        title.setStyleSheet("font-size:11px; color:#8D9299;")
        outer.addWidget(title)

        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._tag_edit = QLineEdit()
        self._tag_edit.setPlaceholderText("t.ex. P-101 (valfritt)")
        self._tag_edit.setFixedHeight(CONFIG['H_SMALL_BTN'])
        self._tag_edit.setStyleSheet(_small)
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        self._tag_edit.textEdited.connect(self._on_tag_edited_by_user)
        # textChanged (not textEdited) for the live dup-check (2026-08-25
        # follow-up, see NOTES.md): textEdited only fires for actual
        # keystrokes, not for a completer popup selection — picking an
        # existing tag from the completer's dropdown (built from this same
        # tag list) changed the text without ever showing the duplicate
        # warning. textChanged fires either way.
        self._tag_edit.textChanged.connect(self._update_dup_hint_live)
        self._tag_edit.editingFinished.connect(self._commit_tag)
        # "Objekt:" in simple mode to match the requested Objekt/Objekttyp
        # wording — same QLineEdit + tag-completer either way, so the user
        # can still type a new tag or pick a matching existing one live.
        tag_lbl = QLabel("Objekt:" if simple else "Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        # Same non-editable-dropdown-plus-add-button pattern as
        # EquipmentTagPopup/CauseTagPopup (2026-08-13 follow-up, see
        # their own docstrings for why an editable combo was rejected).
        # Was briefly a labeled "+ Lägg till" button (2026-08-24, see
        # NOTES.md "Åtta UX/logik-förbättringar") but that made this small
        # popup feel cramped after a rubber-band drag — reverted to a
        # compact square "+" (tooltip still explains the action) at
        # 2026-08-25.
        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_SMALL_BTN'])
        self._type_cb.setStyleSheet(_small)
        self._type_cb.addItems(_equipment_type_options(db))
        typ_row = QHBoxLayout()
        typ_row.setSpacing(4)
        typ_row.addWidget(self._type_cb)
        add_type_btn = QPushButton("+")
        add_type_btn.setFixedSize(CONFIG['H_SMALL_BTN'], CONFIG['H_SMALL_BTN'])
        add_type_btn.setStyleSheet(_small)
        add_type_btn.setToolTip("Lägg till en ny objekttyp")
        add_type_btn.clicked.connect(self._add_new_type)
        typ_row.addWidget(add_type_btn)
        typ_lbl = QLabel("Objekttyp:" if simple else "Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, typ_row)
        outer.addLayout(form)
        self._type_cb.activated.connect(lambda _index: self._commit_type())

        self._searching_lbl = QLabel("⏳ Söker tagg…")
        self._searching_lbl.setStyleSheet("font-size:9px; color:#8D9299; font-style:italic;")
        self._searching_lbl.setVisible(False)
        outer.addWidget(self._searching_lbl)

        # Duplicate-tag hint (same wording as EquipmentTagPopup) — kept
        # purely informational when the placeholder already carries real
        # data (deviations checked) rather than force-merging and risking
        # orphaning it; see _reassign_to_existing. A blocking QMessageBox
        # warning (2026-08-24, see NOTES.md) fires alongside this the
        # moment a real duplicate is found — see _commit_tag. Updated live
        # on every keystroke (2026-08-25 follow-up), not just on commit, so
        # editing the tag to no longer match makes the warning disappear
        # immediately instead of lingering until the field loses focus.
        self._dup_hint = QLabel("")
        self._dup_hint.setStyleSheet("font-size:9px; color:#b8860b;")
        self._dup_hint.setWordWrap(True)
        outer.addWidget(self._dup_hint)

        # "Skapa dublett" (2026-08-25, see NOTES.md) — without this, a
        # matching tag always silently merges into the existing catalog
        # row (_reassign_to_existing) with no way to deliberately create a
        # second, separate row for the same tag. Only shown while the
        # typed tag currently matches another object; clicking it confirms
        # THIS exact tag text should NOT be merged. NoFocus so clicking it
        # doesn't steal focus from _tag_edit first — a StrongFocus button
        # here would fire _tag_edit's editingFinished (and thus the merge)
        # before this button's own clicked() handler ever runs.
        self._dup_confirm_btn = QPushButton("Skapa dublett")
        self._dup_confirm_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._dup_confirm_btn.setStyleSheet(_small)
        self._dup_confirm_btn.setToolTip(
            "Skapa som ett nytt, separat objekt istället för att slå ihop "
            "med det befintliga objektet som redan har denna tagg.")
        self._dup_confirm_btn.setVisible(False)
        self._dup_confirm_btn.clicked.connect(self._confirm_duplicate)
        outer.addWidget(self._dup_confirm_btn)

        # Simple mode (rubber-band placement, 2026-08-24) drops the
        # embedded deviation checklist entirely — this popup now only
        # creates the object itself. Adding deviations happens afterward
        # by clicking the placed marker, which opens EquipmentDeviationBar
        # exactly like it does for any other existing object.
        self._checklist = None
        if not simple:
            self._checklist = _DeviationChecklist(db, self)
            self._checklist.deviation_added.connect(self.deviation_added)
            self._checklist.deviation_removed.connect(self.deviation_removed)
            outer.addWidget(self._checklist)

        eq = self.db.get_equipment_by_id(equipment_id)
        if eq:
            self._tag_edit.setText(eq.get('tag') or '')
            comp_type = eq.get('equipment_type') or ''
            if comp_type:
                idx = self._type_cb.findText(comp_type)
                if idx < 0:
                    self._type_cb.addItem(comp_type)
                    idx = self._type_cb.count() - 1
                self._type_cb.setCurrentIndex(idx)

    def load_checklist(self, active_node_id=None):
        if self._checklist is not None:
            self._checklist.load(self._equipment_id, active_node_id)

    def show_near(self, global_pos):
        """Same screen-clamped positioning + available-space checklist
        sizing as EquipmentDeviationBar.show_near() — see that method's
        own docstring for the reasoning; duplicated rather than shared
        because the two popups' surrounding chrome (tag/typ form here,
        just a title there) differs enough that a shared helper would
        need to take the chrome height as a parameter anyway.

        Used for the plain right-click "🔧 Objekt" placement, which only
        has a single click point (no drawn area to avoid overlapping) —
        see show_near_rect() for the rubber-band case."""
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()

        if self._checklist is not None:
            self._checklist.set_max_height(16777215)
        self.adjustSize()
        natural_total_h = self.sizeHint().height()
        natural_scroll_h = self._checklist.natural_height() if self._checklist is not None else 0
        chrome_h = natural_total_h - natural_scroll_h

        space_below = scr.bottom() - global_pos.y()
        space_above = global_pos.y() - scr.top()
        open_below = space_below >= space_above
        available = (space_below if open_below else space_above) - chrome_h - 12
        scroll_h = max(120, min(natural_scroll_h, available))
        if self._checklist is not None:
            self._checklist.set_max_height(int(scroll_h))
        self.adjustSize()

        pw, ph = self.sizeHint().width(), self.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = global_pos.y() if open_below else global_pos.y() - ph
        y = min(max(scr.top(), y), scr.bottom() - ph)
        self.move(max(scr.left(), x), y)
        self.show()
        self.setFocus()
        self._tag_edit.setFocus()
        self._tag_edit.selectAll()

    def show_near_rect(self, left, top, right, bottom):
        """Positions the popup beside a drawn rubber-band rectangle
        (global screen coordinates) instead of anchored to a single point
        — show_near() alone could land the popup directly on top of the
        marked area, hiding the very selection the user just made
        (2026-08-24, see NOTES.md). Tries, in order, the side with room:
        right of the rect, left of it, below it, above it — falling back
        to whichever of those has the most available space if none fits
        fully, then clamps to the screen like show_near()."""
        center = QPoint(int((left + right) / 2), int((top + bottom) / 2))
        scr = (QApplication.screenAt(center) or QApplication.primaryScreen()).availableGeometry()

        self.adjustSize()
        pw, ph = self.sizeHint().width(), self.sizeHint().height()

        space_right  = scr.right() - right
        space_left   = left - scr.left()
        space_below  = scr.bottom() - bottom
        space_above  = top - scr.top()
        sides = [
            ('right', space_right),
            ('left',  space_left),
            ('below', space_below),
            ('above', space_above),
        ]
        best_side = max(sides, key=lambda s: s[1])[0]
        if space_right >= pw:
            best_side = 'right'
        elif space_left >= pw:
            best_side = 'left'
        elif space_below >= ph:
            best_side = 'below'
        elif space_above >= ph:
            best_side = 'above'

        if best_side == 'right':
            x, y = right + 8, top
        elif best_side == 'left':
            x, y = left - pw - 8, top
        elif best_side == 'below':
            x, y = left, bottom + 8
        else:
            x, y = left, top - ph - 8

        x = min(max(scr.left(), x), scr.right() - pw)
        y = min(max(scr.top(), y), scr.bottom() - ph)
        self.move(int(x), int(y))
        self.show()
        self.setFocus()
        self._tag_edit.setFocus()
        self._tag_edit.selectAll()

    @property
    def create_cause_fn(self):
        return self._checklist._create_cause_fn if self._checklist is not None else None

    @create_cause_fn.setter
    def create_cause_fn(self, fn):
        if self._checklist is not None:
            self._checklist._create_cause_fn = fn

    def set_searching(self, searching):
        self._searching_lbl.setVisible(searching)

    def set_detected_tag(self, tag):
        """Fills the tag field with the async search's result — never
        overwrites text the user already typed themselves while the
        search was still running.

        Passes show_warning=False to _commit_tag: this fill is a passive
        background event the user did nothing to trigger, so silently
        auto-linking to an existing catalog row (still fully logged via
        the inline _dup_hint, just not a blocking dialog) is the right
        call — a modal QMessageBox popping up on its own, with no typing
        or click behind it, would be startling rather than helpful
        (2026-08-24, found while adding the duplicate-tag warning: this
        exact path could pop a real dialog during a test's tearDown()
        once the background worker's queued result was delivered)."""
        self.set_searching(False)
        if self._tag_edited_by_user or not tag:
            return
        self._tag_edit.setText(tag)
        self._commit_tag(show_warning=False)

    def _on_tag_edited_by_user(self, _text):
        self._tag_edited_by_user = True

    def _update_dup_hint_live(self, text):
        """Live duplicate check, separate from _commit_tag's commit-time
        check (2026-08-25, see NOTES.md) — only ever updates the
        hint/button, never merges or writes to the database, so typing
        (or completer-selecting) a tag that happens to match another
        object doesn't trigger a merge before the field is committed.
        Wired to textChanged rather than textEdited so it also fires when
        a completer popup selection changes the text, not just raw
        keystrokes. Editing the tag so it no longer matches makes the
        hint disappear immediately, rather than lingering until the field
        loses focus."""
        tag = text.strip().upper()
        existing = self.db.get_equipment_by_tag(tag) if tag else None
        if existing and existing['id'] != self._equipment_id and tag != self._dup_confirmed_tag:
            self._show_duplicate_hint(existing)
        else:
            self._dup_hint.setText("")
            self._dup_confirm_btn.setVisible(False)

    def _show_duplicate_hint(self, existing):
        self._dup_hint.setText(
            f"ℹ️ \"{existing['tag']}\" finns redan i katalogen "
            f"({existing.get('equipment_type') or '?'}) — kopplas till den befintliga raden.")
        self._dup_confirm_btn.setVisible(True)

    def _confirm_duplicate(self):
        """Skapa dublett clicked: the user has explicitly said this tag
        should stay its own, separate object rather than merge into the
        existing one (2026-08-25, see NOTES.md). Saves the tag on THIS
        placeholder right away instead of waiting for _tag_edit's
        editingFinished, since _dup_confirm_btn is NoFocus and so never
        triggers that signal itself."""
        tag = self._tag_edit.text().strip().upper()
        self._dup_confirmed_tag = tag
        self._dup_hint.setText("")
        self._dup_confirm_btn.setVisible(False)
        eq = self.db.get_equipment_by_id(self._equipment_id)
        if not eq:
            return
        self.db.update_equipment_item(
            self._equipment_id, tag, _equip_prefix_from_tag(tag) if tag else (eq.get('prefix') or ''),
            eq.get('equipment_type') or '', eq.get('description') or '')

    def _add_new_type(self):
        """Same behavior as EquipmentTagPopup._add_new_type — also
        registers the name as a Standardobjekt right away, see that
        method's own docstring."""
        name, ok = QInputDialog.getText(self, "Ny objekttyp", "Namn:")
        name = (name or '').strip()
        if not ok or not name:
            return
        idx = self._type_cb.findText(name)
        if idx < 0:
            self._type_cb.addItem(name)
            idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(idx)
        exists = self.db.conn.execute(
            "SELECT 1 FROM standard_objects WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not exists:
            self.db.add_standard_object(name)
        self._commit_type()

    def _commit_tag(self, show_warning=True):
        tag = self._tag_edit.text().strip().upper()
        if self._tag_edit.text() != tag:
            self._tag_edit.blockSignals(True)
            self._tag_edit.setText(tag)
            self._tag_edit.blockSignals(False)
        eq = self.db.get_equipment_by_id(self._equipment_id)
        if not eq:
            return

        existing = self.db.get_equipment_by_tag(tag) if tag else None
        if existing and existing['id'] != self._equipment_id and tag != self._dup_confirmed_tag:
            self._show_duplicate_hint(existing)
            # 2026-08-24 (see NOTES.md): the hint label above was too easy
            # to miss — a real, blocking dialog makes the duplicate
            # unmissable. The merge itself (_reassign_to_existing, with
            # its own "don't orphan real data" guard) still happens right
            # after, unchanged — only the notification is upgraded.
            # show_warning=False from set_detected_tag's passive auto-fill
            # (the user didn't type anything, so a modal popping up on its
            # own would be startling, not helpful — the inline hint above
            # still shows either way).
            if show_warning:
                QMessageBox.warning(
                    self, "Objekt finns redan",
                    f"Ett objekt med taggnummer {tag} finns redan på denna P&ID.")
            self._reassign_to_existing(existing['id'])
            return

        self._dup_hint.setText("")
        self._dup_confirm_btn.setVisible(False)
        self.db.update_equipment_item(
            self._equipment_id, tag, _equip_prefix_from_tag(tag) if tag else (eq.get('prefix') or ''),
            eq.get('equipment_type') or '', eq.get('description') or '')

    def _commit_type(self):
        eq = self.db.get_equipment_by_id(self._equipment_id)
        if not eq:
            return
        comp_type = self._type_cb.currentText().strip()
        self.db.update_equipment_item(
            self._equipment_id, eq.get('tag') or '', eq.get('prefix') or '',
            comp_type, eq.get('description') or '')
        # Rebuild the checklist so its per-row standard-cause suggestions
        # (_build_deviation_row's `causes` lookup, keyed on comp_type) use
        # the type just picked, not whatever it was — usually blank — when
        # the checklist was first built at placement time. Without this,
        # ticking a deviation box after choosing a type here silently
        # created the deviation but never the auto-suggested standard
        # cause that goes with it (2026-08-18 follow-up: "läggs det inte
        # till någon standardorsak när jag definerat objekttyp + avikelse
        # som innan").
        if self._checklist is not None:
            self._checklist._rebuild_checklist()

    def _reassign_to_existing(self, existing_id):
        """A tag typed/detected AFTER placement can turn out to already
        belong to a different, real catalog row — never leave a
        duplicate lying around (the same "aldrig dubbletter" guarantee
        place_equipment_marker's creation-time check already enforces;
        this is that same check applied retroactively, now that a blank
        placeholder row exists BEFORE the tag is known).

        Only merges when the placeholder is still safe to discard (no
        deviations checked against it yet) — if the user already ticked
        boxes here before a conflicting tag arrived, merging would
        silently orphan that real data, so this leaves the placeholder
        as a (rare, informational-only) duplicate instead."""
        placeholder_id = self._equipment_id
        if placeholder_id == existing_id:
            return
        if self.db.deviations_for_equipment(placeholder_id):
            return
        placeholder = self.db.get_equipment_by_id(placeholder_id)
        existing = self.db.get_equipment_by_id(existing_id)
        if not existing:
            return
        if placeholder and (placeholder.get('equipment_type') or '') and \
                not (existing.get('equipment_type') or ''):
            self.db.update_equipment_item(
                existing_id, existing.get('tag') or '', existing.get('prefix') or '',
                placeholder['equipment_type'], existing.get('description') or '')
            existing = self.db.get_equipment_by_id(existing_id)
        if self._marker_id is not None:
            self.db.update_equipment_marker_link(
                self._marker_id, existing_id, existing.get('tag') or '')
        self.db.delete_equipment_item(placeholder_id)
        self._equipment_id = existing_id
        if self._checklist is not None:
            self._checklist.load(existing_id)
        comp_type = existing.get('equipment_type') or ''
        if comp_type:
            idx = self._type_cb.findText(comp_type)
            if idx >= 0:
                self._type_cb.setCurrentIndex(idx)


class PIDPanel(QWidget):
    node_created            = pyqtSignal(int)
    cause_template_created  = pyqtSignal(int)
    marker_navigated        = pyqtSignal(str, int)
    equipment_deviation_created = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    pid_analysis_done       = pyqtSignal()
    # Emitted when user right-clicks P&ID -> "🔧 Objekt"; MainWindow shows
    # EquipmentTagPopup then calls place_equipment_marker() (2026-08-07,
    # see NOTES.md).
    # Also emitted from the right-drag rubber-band menu's own "Objekt"
    # entry (2026-08-09) — pdf_rect then carries the drawn rectangle (PDF
    # units) so the marker gets a real outline shape instead of the
    # generic bowtie-icon fallback; None for the plain right-click (a
    # single point has no rectangle to give it).
    equipment_placement_requested = pyqtSignal(str, object, int, object)
    # (suggested_tag, scene_pos, page, pdf_rect_or_None)
    equipment_edit_requested = pyqtSignal(int)   # equipment_markers.id — bubbled from viewer
    # EquipmentDeviationBar's own tag/typ/delete UI (2026-08-25, see
    # NOTES.md) — bubbled up so MainWindow can refresh tree/scenario
    # (equipment_updated) or tear down any UI still pointed at the
    # now-gone row (equipment_deleted). Both carry equipment_catalog.id,
    # NOT a marker id — the bar already resolved that.
    equipment_updated = pyqtSignal(int)   # equipment_id — tag/type edited from the P&ID marker popup
    equipment_deleted = pyqtSignal(int)   # equipment_id — deleted from the P&ID marker popup
    ref_tag_picked            = pyqtSignal(str)   # forwarded from viewer after MODE_PICK_REF_TAG
    annotation_placed         = pyqtSignal(int)   # annotation id (feature 8)
    # Node markup signals
    markup_draw_finished    = pyqtSignal(str, int, list, int, str)  # type_, node_id, pts, page, label
    markup_item_selected    = pyqtSignal(int)                        # markup_id
    markup_moved            = pyqtSignal(int, list)                  # mu_id, new PDF pts
    markup_label_edited     = pyqtSignal(int, str)                   # mu_id, new_label
    markup_duplicate_requested = pyqtSignal(int)                     # mu_id
    # Red markup signals
    red_markup_draw_finished = pyqtSignal(str, int, list, int, str)  # type_, node_id, pts, page, label/symbol_id
    red_markup_item_selected = pyqtSignal(int)                        # mu_id
    red_markup_moved         = pyqtSignal(int, list)                  # mu_id, new PDF pts
    markup_symbol_dims_changed = pyqtSignal(int, float, float, float)  # mu_id, w, h, rot_deg
    board_layout_changed = pyqtSignal(str)
    cause_equipment_bound = pyqtSignal(int, int)  # cause_id, equipment_id

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._pen_color             = QColor(255, 140, 0)
        self._active_node_id              = None
        self._active_cause_id             = None
        self._active_consequence_id       = None
        self._active_deviation_id         = None   # kept in sync with the current tree selection
        # Tree-context equipment highlight (2026-08-27, see NOTES.md
        # "Dynamisk färgmarkering av objekt på P&ID") — two-tier cache:
        # _tree_scope_colors (equipment_id -> QColor) is only recomputed
        # by set_tree_context() when the tree selection itself changes
        # (one DB tree-walk); _apply_tree_context_highlight() re-maps that
        # same cache onto whichever markers currently exist on-screen,
        # cheaply, on every overlay reload too (page switch, edit) — see
        # both methods' own docstrings below.
        self._tree_scope_type             = None
        self._tree_scope_id               = None
        self._tree_scope_colors: dict     = {}
        # Visibility of the three tree-context role layers.  This is separate
        # from the viewer's marker-layer visibility: an unchecked role keeps
        # its linked equipment visible, but colours it grey.
        self._tree_context_layer_visibility = {
            'cause': True, 'consequence': True, 'safeguard': True,
        }
        self._active_markup_class         = 'node' # 'node' or 'red'
        self._active_symbol_id            = None   # set when red markup symbol tool selected
        self._pending_markup_pts          = None
        self._pending_markup_page         = None
        self._current_display_page  = 0
        # In-flight EquipmentTagSearchWorker instances, keyed by nothing —
        # just a keep-alive list (2026-08-18, see NOTES.md "kombinerad
        # placeringsmeny") so a QThread with no other living reference
        # can't be garbage-collected mid-run; several can be in flight at
        # once if the user places more than one object in quick succession.
        self._tag_search_workers    = []
        self._sheet_map: dict       = {}
        # Set by MainWindow to ScenarioTablePanel.active_edit_target
        # (2026-08-13, see NOTES.md) — lets a Shift+click on a marker
        # insert its tag into an already-open ORS/KON/SG cell editor
        # instead of navigating away and destroying it. None (and thus
        # a no-op) when PIDPanel is used standalone, e.g. in tests.
        self._active_edit_query_fn  = None
        self._pending_cause_bind_id = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout(); bar.setSpacing(4)

        self.open_btn = QPushButton("Importera P&ID")
        self.open_btn.setIcon(_icon('import'))
        self.open_btn.clicked.connect(self._import_pdf)
        bar.addWidget(self.open_btn)

        self.analyze_btn = QPushButton("Analysera P&ID")
        self.analyze_btn.setIcon(_icon('document'))
        self.analyze_btn.setToolTip(
            "Skannar hela P&ID:n, identifierar alla taggnummer-prefix\n"
            "och skapar en nyckel i Inställningar → Identifierade objekt.")
        self.analyze_btn.clicked.connect(self._analyze_pid)
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setVisible(False)
        bar.addWidget(self.analyze_btn)

        self.export_btn = QPushButton("Exportera PDF")
        self.export_btn.setIcon(_icon('export'))
        self.export_btn.setToolTip(
            "Exportera P&ID med alla HAZOP-markeringar (nodgränser, orsaker,\n"
            "konsekvenser, barriärer och kopplingslinjer) som en ny PDF-fil.")
        self.export_btn.clicked.connect(self._export_pdf)
        self.export_btn.setEnabled(False)
        bar.addWidget(self.export_btn)

        bar.addWidget(_vline())

        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.prev_btn.clicked.connect(lambda: self._goto_page(self._current_display_page - 1))
        bar.addWidget(self.prev_btn)

        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setValue(1)
        self.page_spin.setFixedWidth(CONFIG['W_SPINNER'])
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setToolTip("Skriv sidnummer och tryck Enter för att navigera")
        self.page_spin.editingFinished.connect(self._on_page_spin_changed)
        bar.addWidget(self.page_spin)

        self.page_total_label = QLabel("/ —")
        self.page_total_label.setMinimumWidth(35)
        bar.addWidget(self.page_total_label)

        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.next_btn.clicked.connect(lambda: self._goto_page(self._current_display_page + 1))
        bar.addWidget(self.next_btn)

        bar.addWidget(_vline())

        # Manual per-page rotation (2026-08-12, see NOTES.md) — rotates the
        # CURRENTLY VIEWED sheet 90° at a time, composed with (not replacing)
        # the PDF's own /Rotate flag. Deliberately separate from the
        # three-way "Sid-orientering" dropdown in P&ID-inställningar, which
        # NOTES.md documents as unread by rendering — this is a coarser but
        # actually-wired manual control for a specific rotated sheet.
        self.rotate_left_btn = QPushButton("⟲")
        self.rotate_left_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.rotate_left_btn.setToolTip("Rotera detta blad 90° moturs")
        self.rotate_left_btn.clicked.connect(lambda: self._rotate_page(-90))
        bar.addWidget(self.rotate_left_btn)

        self.rotate_right_btn = QPushButton("⟳")
        self.rotate_right_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.rotate_right_btn.setToolTip("Rotera detta blad 90° medurs")
        self.rotate_right_btn.clicked.connect(lambda: self._rotate_page(90))
        bar.addWidget(self.rotate_right_btn)

        bar.addWidget(_vline())

        # "⚙️ Orsak"/"⚠️ Konsekvens" mode-toggle buttons removed 2026-08-07
        # (see NOTES.md); the P&ID canvas is now equipment-object-placement-
        # only (2026-08-13, see NOTES.md) — Navigera is the only toolbar mode.
        self.mode_buttons = {}
        mode_defs = [
            (MODE_NAV,         "Navigera", 'search'),
        ]
        for mode, label, icon_name in mode_defs:
            btn = QPushButton(label)
            btn.setIcon(_icon(icon_name))
            btn.setCheckable(True)
            btn.clicked.connect(partial(self._set_mode, mode))
            bar.addWidget(btn)
            self.mode_buttons[mode] = btn

        bar.addWidget(_vline())

        self.style_widget = QWidget()
        sl = QHBoxLayout(self.style_widget)
        sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(4)

        sl.addWidget(QLabel("Tjocklek:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 15); self.width_spin.setValue(3)
        self.width_spin.valueChanged.connect(self._update_pen)
        sl.addWidget(self.width_spin)

        sl.addWidget(QLabel("Transparens:"))
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(20, 255); self.alpha_slider.setValue(220)
        self.alpha_slider.setFixedWidth(80)
        self.alpha_slider.valueChanged.connect(self._update_pen)
        sl.addWidget(self.alpha_slider)

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(28, 28)
        self.color_btn.clicked.connect(self._pick_color)
        self._refresh_color_btn()
        sl.addWidget(self.color_btn)

        self.create_node_btn = QPushButton("Skapa Nod")
        self.create_node_btn.setIcon(_icon('check'))
        self.create_node_btn.setEnabled(False)
        self.create_node_btn.clicked.connect(self._create_node_from_markup)
        sl.addWidget(self.create_node_btn)

        self.style_widget.setVisible(False)
        bar.addWidget(self.style_widget)
        bar.addWidget(_vline())
        self.layout_btn = QPushButton("Layout")
        self.layout_btn.setIcon(_icon('resize-rotate'))
        self.layout_btn.setCheckable(True)
        self.layout_btn.setToolTip("Dra ritningsbladen för att ordna om dem")
        self.layout_btn.toggled.connect(self._on_layout_mode_toggled)
        bar.addWidget(self.layout_btn)

        # Search only the equipment objects already identified in the P&ID
        # model.  This is deliberately a small popup beside Layout rather
        # than a modal or a PDF text search (2026-08-27).
        self.search_btn = QPushButton("Sök objekt")
        self.search_btn.setToolTip("Sök bland definierade objekt i P&ID-modellen")
        self.search_btn.clicked.connect(self._show_equipment_search)
        bar.addWidget(self.search_btn)

        self._annot_btn = QPushButton("Notering")
        self._annot_btn.setIcon(_icon('edit'))
        self._annot_btn.setCheckable(True)
        self._annot_btn.setToolTip("Klicka på brädet för att lägga till en klisterlapps-notering")
        self._annot_btn.toggled.connect(
            lambda on: (self._set_mode(MODE_ANNOTATION) if on
                        else self._set_mode(MODE_NAV)))
        bar.addWidget(self._annot_btn)

        bar.addStretch()
        layout.addLayout(bar)

        # ── Viewer ────────────────────────────────────────────────────────────
        self.viewer = PIDGraphicsView()
        self.viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.viewer.node_markup_finished.connect(self._on_markup_finished)
        self.viewer.context_action.connect(self._on_context_action)
        self.viewer.zone_drawn.connect(self._on_zone_drawn)
        self.viewer.equipment_drag_finished.connect(self._on_equipment_drag_finished)
        self.viewer.equipment_edit_requested.connect(self.equipment_edit_requested.emit)
        self.viewer.equipment_delete_requested.connect(self._on_equipment_delete_requested)
        self.viewer.ref_tag_picked.connect(self.ref_tag_picked)
        self.viewer.annotation_clicked.connect(self._on_annotation_click)
        self.viewer.marker_clicked.connect(self._on_marker_clicked)
        self.viewer.markup_moved.connect(self.markup_moved)
        self.viewer.markup_label_edited.connect(self.markup_label_edited)
        self.viewer.markup_duplicate_requested.connect(self.markup_duplicate_requested)
        self.viewer.markup_symbol_dims_changed.connect(self.markup_symbol_dims_changed)
        self.viewer.board_layout_changed.connect(self.board_layout_changed)
        self.viewer.board_layout_changed.connect(self._load_overlays)
        self.viewer.sheet_conn_break_requested.connect(self._break_sheet_link)
        self.viewer.sheet_conn_add_requested.connect(self._add_sheet_link)

        layout.addWidget(self.viewer)

        self._equipment_search_popup = _EquipmentSearchPopup(self)
        self._equipment_search_popup.result_chosen.connect(self._on_equipment_search_result)

        # A floating popup (Qt.WindowType.Popup), not docked into this
        # layout — see EquipmentDeviationBar's own docstring and
        # PIDPanel._on_marker_clicked/place_equipment_marker, which call
        # show_near() to position and show it (2026-08-12, see NOTES.md).
        self._equipment_bar = EquipmentDeviationBar(self.db, parent=self.viewer)
        self._equipment_bar.deviation_added.connect(self._on_equipment_deviation_added)
        self._equipment_bar.deviation_removed.connect(self._on_equipment_deviation_removed)
        self._equipment_bar.equipment_updated.connect(self._on_equipment_bar_updated)
        self._equipment_bar.equipment_deleted.connect(self._on_equipment_bar_deleted)
        # Plain callback, not a signal, so the popup gets the (created)
        # cause_id back synchronously — see EquipmentDeviationBar._create_cause_fn.
        # self._equipment_bar.marker_id is read fresh INSIDE the lambda body
        # (not captured at wiring time here, when nothing has been loaded
        # yet) — late-bound, so it always reflects whichever equipment the
        # shared, reused bar was most recently loaded with (2026-08-18:
        # marker_id became an explicit _create_cause_for_bar() parameter
        # instead of that method reaching into self._equipment_bar itself,
        # so the same method can also serve EquipmentPlacementPopup's own,
        # unrelated marker_id — see place_equipment_marker()).
        self._equipment_bar._create_cause_fn = (
            lambda dev_id, ct, tag, desc, freq=None:
                self._create_cause_for_bar(self._equipment_bar.marker_id, dev_id, ct, tag, desc, freq))

        self._set_mode(MODE_NAV)
        self._update_pen()

    def _analyze_pid(self):
        """Scan all PDF pages via the shared scan_pdf_for_equipment() pipeline
        (same one "🔍 Skanna P&ID" in Utrustningsregistret uses — see
        NOTES.md "Slå ihop Skanna/Analysera P&ID"), collect unique tag
        prefixes, and cross-ref with the tag database.

        Runs on background worker PROCESSES (ParallelTagScanWorker) when
        the document is large enough for multi-core parallelism to be
        worth it, with live per-page progress — see NOTES.md "Flerkärnig
        parallellisering av Analysera P&ID". Falls back to a single
        sequential pass automatically for small documents or if the
        process pool can't start."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return

        # Offer OCR auto-install if no engine is available at all, then ask
        # whether to actually use it for this scan (same prompt as "🔍 Skanna
        # P&ID" in EquipmentPanel._scan, hazop.py) -- unless the user has
        # set a specific default engine in Inställningar → P&ID-inställningar
        # ("OCR-standardval"), in which case resolve_ocr_scan_choice() skips
        # the prompt and uses that engine directly.
        st = ocr_status()
        if not st['tesseract'] and not st['easyocr']:
            ensure_ocr_available(self)
        use_ocr, ocr_engine = resolve_ocr_scan_choice(self.db, self)

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return
        n = self.viewer.pdf_doc.page_count

        dlg = PageProgressDialog("Analyserar P&ID…", n, self)
        worker = ParallelTagScanWorker(path, use_ocr=use_ocr, ocr_engine=ocr_engine)
        self._scan_thread = worker   # keep a reference so it isn't GC'd mid-run

        cancelled_flag = {'v': False}

        def _on_cancel():
            cancelled_flag['v'] = True
            worker.requestInterruption()

        def _on_finished(scan_result):
            dlg.close()
            self._scan_thread = None
            if cancelled_flag['v']:
                return

            real = {k: v for k, v in scan_result.items() if not k.startswith('_')}
            found = {pfx: set(data['tags']) for pfx, data in real.items()}
            if not found:
                QMessageBox.information(self, "Inga taggar",
                    "Inga taggnummer hittades i P&ID:n.")
                return

            # Shared with "🔍 Skanna P&ID" (EquipmentPanel._scan, hazop.py) —
            # both scan entry points now populate BOTH the per-tag equipment
            # register and the per-prefix "Identifierade objekt" list, so
            # results are identical regardless of which button was used.
            apply_scan_result_to_equipment_catalog(self.db, scan_result)
            upsert_identified_tags_from_scan(self.db, scan_result)

            QMessageBox.information(self, "Analys klar ✅",
                f"Hittade {len(found)} unika prefix.\n\n"
                "Öppna Inställningar → Identifierade objekt\n"
                "för att bekräfta typerna och aktivera 'Använd'.\n\n"
                "Utrustningsregistret har också uppdaterats.")
            self.pid_analysis_done.emit()

        worker.page_progress.connect(dlg.set_page_status)
        worker.finished_scan.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        worker.start()
        dlg.exec()

    def _refresh_color_btn(self):
        c = self._pen_color
        self.color_btn.setStyleSheet(
            f"background:{c.name()}; border:1px solid #555; border-radius:3px;")

    def _pick_color(self):
        c = QColorDialog.getColor(self._pen_color, self, "Välj färg")
        if c.isValid():
            self._pen_color = c
            self._refresh_color_btn()
            self._update_pen()

    def _working_pdf_path(self):
        """Returns the project-local working copy path, e.g. hazop_project_pid.pdf."""
        db_path = Path(self.db.path)
        return db_path.with_name(db_path.stem + '_pid.pdf')

    def _rebuild_sheet_map(self):
        sheets = self.db.get_sheets()
        self._sheet_map = {i: int(s['physical_page']) for i, s in enumerate(sheets)}

    def _export_pdf(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Export", "Öppna ett P&ID-dokument först.")
            return
        working = self._working_pdf_path()
        if not working.exists():
            QMessageBox.warning(self, "Export", "Ingen P&ID-fil att exportera.")
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Exportera P&ID med markup", "", "PDF-dokument (*.pdf)")
        if not out_path:
            return
        if not out_path.lower().endswith('.pdf'):
            out_path += '.pdf'

        sheets = self.db.get_sheets()
        page_order = ([int(s['physical_page']) for s in sheets]
                      if sheets else list(range(self.viewer.page_count())))

        prog = QProgressDialog("Exporterar P&ID…", None, 0, len(page_order), self)
        prog.setWindowTitle("Export")
        prog.setMinimumDuration(0)
        prog.setValue(0)
        QApplication.processEvents()

        try:
            src_doc = fitz.open(str(working))
        except Exception as e:
            prog.close()
            QMessageBox.critical(self, "Export misslyckades",
                                 f"Kunde inte öppna PDF:\n{e}")
            return

        out_doc = fitz.open()
        for phys in page_order:
            out_doc.insert_pdf(src_doc, from_page=phys, to_page=phys)

        # A page with a manual rotation override (Database.get_all_page_
        # rotations(), keyed by PHYSICAL page number) must be rotated in
        # out_doc too, or the export comes out sideways/upside-down on
        # exactly the pages the live viewer shows rotated (2026-08-17,
        # see NOTES.md "Exportera PDF hanterar inte P&ID-rotation").
        # Every marker's own x/y below is stored in the LIVE view's
        # already-rotated coordinate space (same reason
        # equipment_detection.apply_page_rotations exists at all — see
        # its own docstring), so out_doc's pages must be rotated BEFORE
        # any drawing happens, not after. Remap physical-page keys to
        # OUTPUT page indices first — out_doc's own page order follows
        # page_order, which is not necessarily identity (custom sheet
        # ordering via get_sheets()).
        raw_rotations = self.db.get_all_page_rotations()
        out_rotations = {out_idx: raw_rotations[phys]
                          for out_idx, phys in enumerate(page_order)
                          if phys in raw_rotations}
        equipment_detection.apply_page_rotations(out_doc, out_rotations)

        for out_idx, phys_page in enumerate(page_order):
            prog.setValue(out_idx)
            QApplication.processEvents()
            page = out_doc.load_page(out_idx)

            # ── Node markup polygons ──────────────────────────────────────
            for node in self.db.nodes():
                nd = dict(node)
                if int(nd.get('pid_page', 0) or 0) != phys_page:
                    continue
                raw_pts = nd.get('markup_points', '') or ''
                if not raw_pts:
                    continue
                try:
                    # Stored in the live view's rotated display space, same
                    # as marker x/y above — un-rotate before drawing (see
                    # _draw_pid_marker's docstring for why).
                    pts = [fitz.Point(float(p[0]), float(p[1])) * page.derotation_matrix
                           for p in json.loads(raw_pts)]
                    style = json.loads(nd.get('markup_style', '') or '{}')
                except Exception:
                    continue
                if len(pts) < 2:
                    continue
                color = _hex_to_fitz_rgb(style.get('color', '#ff8c00'))
                width = max(0.5, style.get('width', 2) * 0.4)
                alpha = style.get('alpha', 120) / 255
                close = len(pts) >= 3
                shape = page.new_shape()
                shape.draw_polyline(pts + [pts[0]] if close else pts)
                try:
                    shape.finish(color=color, width=width,
                                 fill=color, fill_opacity=alpha * 0.35)
                except TypeError:
                    shape.finish(color=color, width=width)
                name = nd.get('name', '')
                if name and pts:
                    cx = sum(p.x for p in pts) / len(pts)
                    cy = sum(p.y for p in pts) / len(pts)
                    try:
                        # 11pt to match the on-screen node label exactly
                        # (PIDGraphicsView.add_node_overlay's QFont
                        # setPointSize(11)) — this used to be hardcoded
                        # to 8pt here with no relation to the live view
                        # at all (2026-08-17, see NOTES.md "text i fel
                        # storlek vid PDF-export"). Centered using
                        # PyMuPDF's own text-width measurement instead of
                        # a per-character magic-number guess, since that
                        # guess was tuned for the old 8pt size and would
                        # have needed re-tuning by hand for 11pt too.
                        fontsize = 11
                        text_w = fitz.get_text_length(name, fontname='helv', fontsize=fontsize)
                        shape.insert_text(
                            fitz.Point(cx - text_w / 2, cy + fontsize * 0.35),
                            name, fontsize=fontsize, color=color, fontname='helv')
                    except Exception:
                        pass
                shape.commit()

            # ── Node markup overlays as editable PDF annotations ──────────
            if hasattr(self.db, 'node_markups_for_page'):
                node_ocgs = {}  # node_id -> OCG xref (one layer per node)

                for mu in self.db.node_markups_for_page(phys_page):
                    m = dict(mu)
                    if not m.get('visible', 1):
                        continue

                    # ── OCG: one layer per node, named after the node ─────
                    node_id = m.get('node_id')
                    if node_id not in node_ocgs:
                        node_row = (self.db.get_node(node_id)
                                    if hasattr(self.db, 'get_node') else None)
                        nname = (dict(node_row)['name'] if node_row
                                 else f'Nod {node_id}')
                        try:
                            node_ocgs[node_id] = out_doc.add_ocg(nname, on=True)
                        except Exception:
                            node_ocgs[node_id] = None
                    ocg_xref = node_ocgs[node_id]

                    # ── Parse geometry & style ─────────────────────────────
                    try:
                        pts_raw = json.loads(m.get('points', '[]') or '[]')
                        # Same rotated-display-space storage as above —
                        # un-rotate before use in annot geometry.
                        pts = [fitz.Point(float(p[0]), float(p[1])) * page.derotation_matrix
                               for p in pts_raw]
                    except Exception:
                        pts = []

                    rgb      = _hex_to_fitz_rgb(m.get('color', '#1565C0'))
                    opacity  = float(m.get('opacity', 0.45))
                    width    = max(0.5, int(m.get('line_width', 12)) * 0.4)
                    font_sz  = max(6, int(m.get('font_size', 12)))
                    mu_type  = m.get('type', 'polygon')
                    label    = m.get('label', '') or ''
                    # Light fill: blend stroke colour with white at 30%
                    fill_rgb = tuple(min(1.0, 0.70 + 0.30 * c) for c in rgb)

                    annot = None
                    try:
                        if mu_type == 'polygon' and len(pts) >= 2:
                            annot = page.add_polygon_annot(pts)
                            annot.set_colors({"stroke": list(rgb),
                                              "fill":   list(fill_rgb)})
                            annot.set_border({"width": width})
                            if label:
                                annot.set_info(title=label, content=label)
                            annot.update(opacity=opacity)

                        elif mu_type == 'polyline' and len(pts) >= 2:
                            annot = page.add_polyline_annot(pts)
                            annot.set_colors({"stroke": list(rgb)})
                            annot.set_border({"width": width})
                            if label:
                                annot.set_info(title=label)
                            annot.update(opacity=opacity)

                        elif mu_type in ('text', 'comment') and pts:
                            txt = label or '?'
                            x, y = pts[0].x, pts[0].y
                            rect_w = len(txt) * font_sz * 0.58 + 8
                            rect_h = font_sz * 1.7
                            rect   = fitz.Rect(x, y - rect_h,
                                               x + rect_w, y + 2)
                            # Only 'comment' gets a visible highlight box
                            # on screen (_add_markup_text_item's own
                            # `if type_ == 'comment':` guard) — a plain
                            # 'text' node-name label never has one there.
                            # This used to fill EVERY text-type item with
                            # fill_rgb regardless, giving node-name labels
                            # a background they never show live
                            # (2026-08-17, see NOTES.md "text får en
                            # bakgrundsfärg som inte syns annars"). None
                            # is PyMuPDF's own "no fill" convention here
                            # (confirmed empirically: annot.colors['fill']
                            # comes back [] and the rendered box shows no
                            # background at all).
                            bg = [1.0, 1.0, 0.82] if mu_type == 'comment' else None
                            annot = page.add_freetext_annot(
                                rect, txt,
                                fontsize=font_sz,
                                fontname='helv',
                                text_color=list(rgb),
                                fill_color=bg)
                            annot.set_info(
                                title=('Kommentar' if mu_type == 'comment'
                                       else 'Nodnamn'),
                                content=txt)
                            annot.update(opacity=opacity)

                    except Exception:
                        pass  # skip faulty item; never crash the export

                    if annot is not None and ocg_xref:
                        try:
                            annot.set_oc(ocg_xref)
                        except Exception:
                            pass

            # ── Cause markers ─────────────────────────────────────────────
            cause_pos = {}
            for m in self.db.cause_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                cause_pos[md['cause_id']] = (x, y)
                cause  = self.db.get_cause(md['cause_id'])
                desc   = dict(cause).get('description', '') if cause else ''
                tag    = md.get('component_tag', '') or md.get('component_type', '')
                _draw_pid_marker(page, x, y, (0.75, 0.18, 0.09), 'C', tag or desc)

            # ── Consequence markers ───────────────────────────────────────
            cons_pos = {}
            for m in self.db.consequence_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                cons_pos[md['consequence_id']] = (x, y)
                cons = self.db.get_consequence(md['consequence_id'])
                desc = dict(cons).get('description', '') if cons else ''
                _draw_pid_marker(page, x, y, (0.87, 0.42, 0.06), 'K', desc)

            # ── Safeguard markers ─────────────────────────────────────────
            sg_pos = {}
            for m in self.db.safeguard_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                sg_pos[md['safeguard_id']] = (x, y)
                row = self.db.conn.execute(
                    "SELECT description FROM safeguards WHERE id=?",
                    (md['safeguard_id'],)).fetchone()
                desc = row['description'] if row else ''
                tag  = md.get('tag', '')
                _draw_pid_marker(page, x, y, (0.15, 0.62, 0.27), 'S', tag or desc)

            # ── Equipment/object markers ──────────────────────────────────
            # Missing entirely before this fix (2026-08-17, see NOTES.md
            # "övriga objekt jag satt ut på P&ID... kommer inte med,
            # varken dom röda eller gröna") — the loop below simply
            # didn't exist; only cause/consequence/safeguard markers were
            # ever drawn. Same red/green split as PIDGraphicsView.
            # add_equipment_marker's on-screen pen/brush colours
            # (0,130,60 green / 160,0,0 red, scaled to PyMuPDF's 0-1
            # range), keyed on the same deviation_count PIDPanel.
            # _load_overlays() already computes for the live marker.
            for m in self.db.equipment_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                eq_id = md.get('equipment_id')
                dev_count = self.db.equipment_deviation_count(eq_id) if eq_id else 0
                rgb = (0.0, 0.51, 0.24) if dev_count > 0 else (0.63, 0.0, 0.0)
                # Live-resolved tag, same reasoning as _load_overlays()
                # (2026-08-18, see NOTES.md "Objektets identitet ...") —
                # otherwise a PDF export could show a renamed object's OLD
                # tag even though the live viewer shows the new one.
                eq = self.db.get_equipment_by_id(eq_id) if eq_id else None
                tag_val = (eq.get('tag') or '') if eq else md.get('tag', '')
                _draw_pid_marker(page, x, y, rgb, 'O', tag_val)

            # ── Connection lines ──────────────────────────────────────────
            shape = page.new_shape()
            for cid, cpos in cons_pos.items():
                c = self.db.get_consequence(cid)
                if c:
                    cause_id = dict(c).get('cause_id') if c else None
                    if cause_id and cause_id in cause_pos:
                        shape.draw_line(fitz.Point(*cause_pos[cause_id]),
                                        fitz.Point(*cpos))
                        shape.finish(color=(0.75, 0.18, 0.09), width=0.8)
            for sid, spos in sg_pos.items():
                s = self.db.get_safeguard(sid)
                if s:
                    cons_id = dict(s).get('consequence_id') if s else None
                    if cons_id and cons_id in cons_pos:
                        shape.draw_line(fitz.Point(*cons_pos[cons_id]),
                                        fitz.Point(*spos))
                    try:
                        shape.finish(color=(0.15, 0.62, 0.27), width=0.8,
                                     dashes="[3 3] 0")
                    except TypeError:
                        shape.finish(color=(0.15, 0.62, 0.27), width=0.8)
            shape.commit()

        src_doc.close()
        prog.setValue(len(page_order))
        QApplication.processEvents()

        try:
            out_doc.save(out_path, garbage=4, deflate=True)
            out_doc.close()
            prog.close()
            QMessageBox.information(self, "Export klar",
                                    f"P&ID exporterat med markup till:\n{out_path}")
        except Exception as e:
            out_doc.close()
            prog.close()
            QMessageBox.critical(self, "Export misslyckades",
                                 f"Kunde inte spara PDF:\n{e}")

    def _import_pdf(self):
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:\n    pip install PyMuPDF\nStarta sedan om.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Importera P&ID", "", "PDF-dokument (*.pdf);;Alla filer (*.*)")
        if not paths:
            return
        paths = sorted(paths)   # alphabetical merge order

        working      = self._working_pdf_path()
        has_existing = working.exists()
        created_at   = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        # Import always starts with a fresh (sequential) layout so that
        # smart layout can re-propose unbiased groupings afterwards.
        # Positions are only restored from DB on project reload (try_reload_pdf).

        if has_existing:
            dlg = PIDImportDialog(has_existing=True, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            rev_label = dlg.label() or created_at
            rev_notes = dlg.notes()

            if dlg.is_new_revision():
                # Merge all selected files into one new PDF → replace working copy
                try:
                    base_doc = fitz.open(paths[0])
                    for p in paths[1:]:
                        ext = fitz.open(p)
                        base_doc.insert_pdf(ext)
                        ext.close()
                    total_pages = base_doc.page_count
                    if self.viewer.pdf_doc is not None:
                        try: self.viewer.pdf_doc.close()
                        except Exception as e: logging.error(f"Failed to close previous PDF document: {e}")
                        self.viewer.pdf_doc = None
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    base_doc.save(tmp_path, garbage=4, deflate=True)
                    base_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel", f"Kunde inte skapa PDF:\n{e}")
                    return
                prog = QProgressDialog(
                    f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
                prog.setWindowTitle("Importerar P&ID")
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(0)
                QApplication.processEvents()
                if not self.viewer.load_pdf(
                        str(working), page=0,
                        layout_offsets=None,
                        progress_cb=lambda cur, tot: prog.setValue(cur),
                        page_rotations=self.db.get_all_page_rotations()):
                    prog.close()
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                    return
                prog.setAutoClose(False)
                prog.setValue(total_pages)
                self.db.set_pid_path(str(working))
                self.db.clear_sheets()
                self.db.clear_page_rotations()
                self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                self.db.ensure_sheets_initialized(self.viewer.page_count(), str(working))
                self._current_display_page = 0

            else:
                # Append all selected files to the existing working PDF
                try:
                    existing_doc    = fitz.open(str(working))
                    existing_pg_cnt = existing_doc.page_count
                    n_new = 0
                    for p in paths:
                        ext = fitz.open(p)
                        n_new += ext.page_count
                        existing_doc.insert_pdf(ext)
                        ext.close()
                    total_pages = existing_pg_cnt + n_new
                    if self.viewer.pdf_doc is not None:
                        try: self.viewer.pdf_doc.close()
                        except Exception as e: logging.error(f"Failed to close existing PDF document during merge: {e}")
                        self.viewer.pdf_doc = None
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    existing_doc.save(tmp_path, garbage=4, deflate=True)
                    existing_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel vid sammanslagning",
                                         f"Kunde inte sammanfoga PDF:\n{e}")
                    return

                prog = QProgressDialog(
                    f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
                prog.setWindowTitle("Importerar P&ID")
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(0)
                QApplication.processEvents()
                keep_phys = self.viewer.current_page
                if not self.viewer.load_pdf(
                        str(working), page=keep_phys,
                        layout_offsets=None,
                        progress_cb=lambda cur, tot: prog.setValue(cur),
                        page_rotations=self.db.get_all_page_rotations()):
                    prog.close()
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna sammanfogad PDF.")
                    return
                prog.setAutoClose(False)
                prog.setValue(total_pages)

                if self.db.get_display_page_count() == 0:
                    self.db.ensure_sheets_initialized(existing_pg_cnt, str(working))

                rev_id = self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                physical_pages = list(range(existing_pg_cnt, existing_pg_cnt + n_new))
                # Bladnamn = "Filnamn – sida N" (2026-08-17, user-confirmed
                # format, see NOTES.md), same as ensure_sheets_initialized.
                stem = Path(working).stem
                sheet_names    = [f"{stem} – sida {existing_pg_cnt + i + 1}"
                                  for i in range(n_new)]
                self.db.append_sheets(physical_pages, sheet_names, rev_id)

        else:
            # First import — merge all selected files into working copy
            try:
                base_doc = fitz.open(paths[0])
                for p in paths[1:]:
                    ext = fitz.open(p)
                    base_doc.insert_pdf(ext)
                    ext.close()
                total_pages = base_doc.page_count
                tmp_fd, tmp_path = tempfile.mkstemp(
                    suffix='.pdf', dir=str(working.parent))
                os.close(tmp_fd)
                base_doc.save(tmp_path, garbage=4, deflate=True)
                base_doc.close()
                shutil.move(tmp_path, str(working))
            except Exception as e:
                QMessageBox.critical(self, "Fel", f"Kunde inte kopiera PDF:\n{e}")
                return
            prog = QProgressDialog(
                f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
            prog.setWindowTitle("Importerar P&ID")
            prog.setWindowModality(Qt.WindowModality.WindowModal)
            prog.setMinimumDuration(0)
            prog.setValue(0)
            QApplication.processEvents()
            if not self.viewer.load_pdf(
                    str(working), page=0,
                    layout_offsets=None,
                    progress_cb=lambda cur, tot: prog.setValue(cur),
                    page_rotations=self.db.get_all_page_rotations()):
                prog.close()
                QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                return
            prog.setAutoClose(False)
            prog.setValue(total_pages)
            self.db.set_pid_path(str(working))
            self.db.clear_sheets()
            self.db.clear_page_rotations()
            self.db.add_revision(created_at, '', str(working), created_at)
            self.db.ensure_sheets_initialized(self.viewer.page_count(), str(working))
            self._current_display_page = 0

        # Phase 2: apply active-page filter if needed, then load markers/connections
        self._rebuild_sheet_map()
        self._update_page_label()
        sheets = self.db.get_sheets()
        active = sorted(int(s['physical_page']) for s in sheets) if sheets else None
        already = sorted(self.viewer._all_page_items.keys())
        prog.setMaximum(0)
        prog.setLabelText("Laddar markeringar…")
        QApplication.processEvents()
        if active != already:
            # Active-page set differs from what was rendered (e.g. some sheets
            # were deleted before appending) — re-render to apply the filter.
            n_active = len(active) if active else 0
            prog.setMaximum(n_active)
            prog.setValue(0)
            prog.setLabelText(f"Bygger P&ID-vy ({n_active} sidor)…")
            QApplication.processEvents()
            self.viewer._render_all_pages(
                active_pages=active,
                progress_cb=lambda cur, tot: prog.setValue(cur))
            prog.setMaximum(0)
            prog.setLabelText("Laddar markeringar…")
            QApplication.processEvents()
        self._load_overlays()
        prog.close()
        self.analyze_btn.setEnabled(True)
        self.export_btn.setEnabled(True)

    def _goto_page(self, display_n):
        if self.viewer.pdf_doc is None:
            return
        total = len(self._sheet_map) if self._sheet_map else (
            self.db.get_display_page_count() or self.viewer.page_count())
        display_n = max(0, min(display_n, total - 1))
        # Feature 10: save zoom/scroll for the page we're leaving
        self._save_page_view(self._current_display_page)
        if self._sheet_map:
            physical = self._sheet_map.get(display_n, display_n)
        elif self.db.get_display_page_count() > 0:
            physical = self.db.get_sheet_physical_page(display_n)
        else:
            physical = display_n
        self._current_display_page = display_n
        self.viewer.goto_page(physical)
        self._update_page_label()
        # Feature 10: restore saved zoom/scroll for new page
        self._restore_page_view(display_n)

    def _on_page_spin_changed(self):
        self._goto_page(self.page_spin.value() - 1)

    def _current_physical_page(self):
        """Physical page number behind self._current_display_page — same
        display->physical resolution _goto_page uses, factored out here so
        _rotate_page (2026-08-12) doesn't duplicate/diverge from it."""
        display_n = self._current_display_page
        if self._sheet_map:
            return self._sheet_map.get(display_n, display_n)
        if self.db.get_display_page_count() > 0:
            return self.db.get_sheet_physical_page(display_n)
        return display_n

    def _rotate_page(self, delta_degrees):
        """Rotate the currently-viewed sheet by delta_degrees (+-90),
        composed on top of whatever the PDF file itself already declares via
        /Rotate (2026-08-12, see NOTES.md "P&ID-sidrotation").

        Markers/zones are stored in PDF-space (see cause_markers etc. in
        CLAUDE.md's schema table), which this changes — every position
        recorded for this physical page must be re-anchored to the same
        physical point at the same time, or a rotated page would silently
        move every marker on it. The transform is built from this page's
        own derotation_matrix (old-rotated -> raw/physical-invariant space)
        composed with its new rotation_matrix (raw -> new-rotated space),
        i.e. the exact inverse of how equipment_detection._rotate_words
        already converts raw PyMuPDF coordinates into this app's PDF-space.
        """
        if self.viewer.pdf_doc is None or not HAS_PYMUPDF:
            return
        physical = self._current_physical_page()
        page = self.viewer.pdf_doc.load_page(physical)
        old_extra = self.viewer._page_rotation_override.get(physical, 0)
        old_derot = page.derotation_matrix   # current (pre-change) rotated-space -> raw
        new_extra = (old_extra + delta_degrees) % 360

        self.viewer.set_page_rotation_override(physical, new_extra)  # mutates page.rotation in place
        new_rotmat = page.rotation_matrix    # raw -> new rotated-space, reflects the change above

        def _tf(x, y):
            raw = fitz.Point(float(x), float(y)) * old_derot
            newp = raw * new_rotmat
            return (newp.x, newp.y)

        self.db.set_page_rotation(physical, new_extra)
        self.db.remap_page_rotation_positions(physical, _tf, angle_delta_deg=delta_degrees)

        self._save_page_view(self._current_display_page)
        active = sorted(self.viewer._all_page_items.keys())
        # Preserve whatever board layout (auto-flow or a user-dragged custom
        # arrangement, see "📐 Layout") is currently on screen — only the
        # rotated page's own footprint changes (width/height swap for a
        # +-90 turn), everything else keeps its existing position. A
        # rotated page's new footprint can end up overlapping its neighbour
        # in that case — known limitation, see NOTES.md.
        layout_offsets = dict(self.viewer._page_offsets)
        self.viewer._render_all_pages(active_pages=active, layout_offsets=layout_offsets)
        self._load_overlays()
        self._restore_page_view(self._current_display_page)

    def _update_page_label(self):
        total = len(self._sheet_map) if self._sheet_map else (
            self.db.get_display_page_count() or self.viewer.page_count())
        if total > 0:
            self.page_spin.blockSignals(True)
            try:
                self.page_spin.setRange(1, total)
                self.page_spin.setValue(self._current_display_page + 1)
            finally:
                self.page_spin.blockSignals(False)
            self.page_total_label.setText(f"/ {total}")
        else:
            self.page_spin.blockSignals(True)
            try:
                self.page_spin.setRange(1, 1)
                self.page_spin.setValue(1)
            finally:
                self.page_spin.blockSignals(False)
            self.page_total_label.setText("/ —")

    # ── Feature 10: per-page zoom/scroll state ────────────────────────────────

    def _save_page_view(self, display_n):
        if not hasattr(self, '_page_views'):
            self._page_views = {}
        t = self.viewer.transform()
        self._page_views[display_n] = (t.m11(), t.m12(), t.m21(), t.m22(),
                                       self.viewer.horizontalScrollBar().value(),
                                       self.viewer.verticalScrollBar().value())

    def _restore_page_view(self, display_n):
        if not hasattr(self, '_page_views'):
            return
        state = self._page_views.get(display_n)
        if not state:
            return
        from PyQt6.QtGui import QTransform
        m11, m12, m21, m22, hv, vv = state
        self.viewer.setTransform(QTransform(m11, m12, m21, m22, 0, 0))
        # Block scrollContentsBy from firing _schedule_lod_update while we restore
        self.viewer.horizontalScrollBar().blockSignals(True)
        self.viewer.verticalScrollBar().blockSignals(True)
        self.viewer.horizontalScrollBar().setValue(hv)
        self.viewer.verticalScrollBar().setValue(vv)
        self.viewer.horizontalScrollBar().blockSignals(False)
        self.viewer.verticalScrollBar().blockSignals(False)
        self.viewer._apply_lod(self.viewer.transform().m11())

    def navigate_to_marker(self, physical_page, x_pdf, y_pdf):
        """Navigate to the page containing a marker and zoom in on it."""
        if self.viewer.pdf_doc is None:
            return
        display_n = physical_page
        if self._sheet_map:
            rev = {phys: disp for disp, phys in self._sheet_map.items()}
            display_n = rev.get(physical_page, physical_page)
        # Skip view-state save/restore for navigate_to_marker — we override the
        # transform immediately with resetTransform + scale + centerOn anyway.
        self._save_page_view(self._current_display_page)
        if self._sheet_map:
            physical = self._sheet_map.get(display_n, display_n)
        elif self.db.get_display_page_count() > 0:
            physical = self.db.get_sheet_physical_page(display_n)
        else:
            physical = display_n
        self._current_display_page = display_n
        self.viewer.goto_page(physical)
        self._update_page_label()
        scene_pt = self.viewer.pdf_to_scene(x_pdf, y_pdf, page=physical_page)
        self.viewer.resetTransform()
        self.viewer.scale(2.5, 2.5)
        self.viewer.centerOn(scene_pt)
        self.viewer._apply_lod(self.viewer.transform().m11())
        self.viewer._schedule_lod_update()

    def _set_mode(self, mode):
        for m, btn in self.mode_buttons.items():
            btn.setChecked(m == mode)
        self.viewer.set_mode(mode)
        self.style_widget.setVisible(mode == MODE_NODE)

    def _on_equipment_drag_finished(self):
        """Shift-dragging an equipment marker to the tree/scenario uses
        QDrag.exec(), a native drag loop that suppresses the normal
        hover/leave events toolbar buttons rely on to clear their pressed
        look — the "🔍 Navigera" button could be left visually stuck
        looking pressed in after the drop even though nothing is actually
        held down anymore. Force every mode button's transient down/hover
        state to release right as the drop itself is released."""
        for btn in self.mode_buttons.values():
            btn.setDown(False)
            btn.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
            btn.update()

    def _on_layout_mode_toggled(self, checked):
        if checked:
            self._set_mode(MODE_BOARD_LAYOUT)
        else:
            self._set_mode(MODE_NAV)

    def _show_equipment_search(self):
        """Show the compact object search next to the toolbar button."""
        self._equipment_search_popup.refresh()
        pos = self.search_btn.mapToGlobal(self.search_btn.rect().bottomLeft())
        self._equipment_search_popup.show_near(pos)

    def _on_equipment_search_result(self, marker_id, page, x, y):
        """Navigate to a selected catalog object and mark that result blue."""
        self.navigate_to_marker(int(page), float(x), float(y))
        self.viewer.set_search_highlight(int(marker_id))

    def _break_sheet_link(self, conn_id: int):
        """Delete a sheet connection from DB and redraw arcs."""
        if conn_id < 0:
            return
        self.db.delete_pid_connection(conn_id)
        self._load_overlays()

    def _add_sheet_link(self, from_page: int, to_page: int):
        """Create a manual sheet connection in DB and redraw arcs."""
        self.db.add_manual_pid_connection(from_page, to_page)
        self._load_overlays()

    def _draw_sheet_connections(self):
        """Draw one bezier arc per connector symbol.

        Iterates over pid_connection records (reliable from_page/to_page pairs),
        then for each page-pair draws one line per individual connector symbol —
        exiting from the edge where the symbol was detected.
        """
        import json as _json
        connections = self.db.get_pid_connections()
        if not connections:
            return
        connectors = self.db.get_connectors()
        raw_map = self.db.get_pid_config_value('sheet_num_map') or '{}'
        try:
            sheet_num_map = {int(k): v.upper()
                             for k, v in _json.loads(raw_map).items()}
        except Exception:
            sheet_num_map = {}

        # Build direction-aware lookups.
        # 'out' = outgoing connector (pentagon leaving the page)
        # 'in'  = incoming connector (rectangle receiving flow)
        # 'any' = unknown direction — included in both buckets
        _pair_out: dict = {}  # (pid_page, ref_page) → [out connectors]
        _pair_in:  dict = {}  # (pid_page, ref_page) → [in  connectors]
        _pair_any: dict = {}  # (pid_page, ref_page) → [all connectors]
        _ref_out:  dict = {}  # (pid_page, ref_sheet) → [out]
        _ref_in:   dict = {}  # (pid_page, ref_sheet) → [in]
        _ref_any:  dict = {}  # (pid_page, ref_sheet) → [all]
        for c in connectors:
            cd   = dict(c)
            dirn = (cd.get('direction') or '').lower()
            rp   = cd.get('ref_page')
            ref  = (cd.get('ref_sheet') or '').upper()
            # direction buckets: unknown goes into both out AND in
            buckets_pair = (
                [_pair_out, _pair_any] if dirn == 'out' else
                [_pair_in,  _pair_any] if dirn == 'in'  else
                [_pair_out, _pair_in, _pair_any]
            )
            buckets_ref = (
                [_ref_out, _ref_any] if dirn == 'out' else
                [_ref_in,  _ref_any] if dirn == 'in'  else
                [_ref_out, _ref_in, _ref_any]
            )
            if rp is not None:
                key_p = (cd['pid_page'], int(rp))
                for d in buckets_pair:
                    d.setdefault(key_p, []).append(cd)
            for v in _sheet_ref_variants(ref):
                key_r = (cd['pid_page'], v)
                for d in buckets_ref:
                    d.setdefault(key_r, []).append(cd)

        def _get_connectors(page, target_page, target_sheet_str, prefer='out'):
            """Direction-aware connector lookup.
            prefer='out' for src_list, 'in' for dst_list.
            Falls back: preferred direction → any direction → ref_sheet fuzzy.
            """
            pref_pair = _pair_out if prefer == 'out' else _pair_in
            pref_ref  = _ref_out  if prefer == 'out' else _ref_in
            for lookup_pair, lookup_ref in [(pref_pair, pref_ref),
                                            (_pair_any, _ref_any)]:
                r = lookup_pair.get((page, target_page), [])
                if r:
                    return r
                for v in _sheet_ref_variants(target_sheet_str):
                    r = lookup_ref.get((page, v), [])
                    if r:
                        return r
            return []

        drawn_pairs:      set  = set()
        gap_slot_counter: dict = {}
        rs = self.viewer.render_scale

        def _edge_pt(c, ox, oy, pw, ph, fallback_edge):
            """Scene point at the connector symbol position on the P&ID.

            Uses the raw x_pdf/y_pdf coordinates — the actual location of the
            connector symbol on the drawing.  The bezier control points (in
            add_sheet_conn_arc) still use src_edge/dst_edge to push the curve
            outward in the correct direction, so the bezier exits cleanly even
            though it starts/ends at the symbol rather than the page edge.

            Falls back to the edge midpoint when coordinates are missing.
            """
            if c:
                xp = c.get('x_pdf')
                yp = c.get('y_pdf')
                if xp is not None and yp is not None:
                    return QPointF(ox + xp * rs, oy + yp * rs)
            if fallback_edge == 'right':  return QPointF(ox + pw,    oy + ph / 2)
            if fallback_edge == 'left':   return QPointF(ox,          oy + ph / 2)
            if fallback_edge == 'top':    return QPointF(ox + pw / 2, oy)
            return                               QPointF(ox + pw / 2, oy + ph)

        for row in connections:
            conn = dict(row)
            fp = conn.get('from_page')
            tp = conn.get('to_page')
            if fp is None or tp is None or fp == tp:
                continue
            if fp not in self.viewer._all_page_items or tp not in self.viewer._all_page_items:
                continue

            media      = conn.get('media_type', 'unknown') or 'unknown'
            color_hex  = _MEDIA_COLORS.get(media, _MEDIA_COLORS['unknown'])
            confidence = float(conn.get('confidence', 0.5))
            bidir      = bool(conn.get('is_bidirectional'))
            weight     = float(conn.get('weight', 0.5) or 0.5)
            conn_id    = conn.get('id', -1)

            ox_fp, oy_fp = self.viewer._page_offsets.get(fp, (0, 0))
            ox_tp, oy_tp = self.viewer._page_offsets.get(tp, (0, 0))
            pw_fp = self.viewer._page_widths_pdf.get(fp, 800) * rs
            ph_fp = self.viewer._page_heights_pdf.get(fp, 600) * rs
            pw_tp = self.viewer._page_widths_pdf.get(tp, 800) * rs
            ph_tp = self.viewer._page_heights_pdf.get(tp, 600) * rs

            fp_sheet = sheet_num_map.get(fp, '')
            tp_sheet = sheet_num_map.get(tp, '')

            # Outgoing connectors on fp (departure symbols) and
            # incoming connectors on tp (arrival symbols).
            src_list = _get_connectors(fp, tp, tp_sheet, prefer='out')
            dst_list = _get_connectors(tp, fp, fp_sheet, prefer='in')

            # Fallback edges from relative page positions (horizontal only)
            dx_pages = ox_tp - ox_fp
            def_src = 'right' if dx_pages >= 0 else 'left'
            def_dst = 'left'  if dx_pages >= 0 else 'right'

            def _make_dot(c, fallback_pt, page_ox, page_oy, c_hex):
                """Create a draggable ConnectorDotItem at the connector's P&ID position.

                Priority: 1) manually saved position, 2) x_pdf/y_pdf on the drawing,
                3) fallback to the bezier endpoint.
                """
                if c is not None:
                    sx = c.get('dot_scene_x')
                    sy = c.get('dot_scene_y')
                    if sx is not None and sy is not None:
                        pos = QPointF(sx, sy)
                    else:
                        xp = c.get('x_pdf')
                        yp = c.get('y_pdf')
                        pos = QPointF(page_ox + xp * rs, page_oy + yp * rs) \
                              if xp is not None and yp is not None else fallback_pt
                    cid = c.get('id', -1)
                else:
                    pos = fallback_pt
                    cid = -1
                dot = ConnectorDotItem(cid, self.db, c_hex, pos)
                self.viewer._scene.addItem(dot)

            if not src_list:
                # No connectors detected on fp side — draw one fallback line
                pair_key = (fp, tp, media)
                if pair_key in drawn_pairs:
                    continue
                drawn_pairs.add(pair_key)
                drawn_pairs.add((tp, fp, media))
                dst_c  = dst_list[0] if dst_list else None
                src_pt = _edge_pt(None, ox_fp, oy_fp, pw_fp, ph_fp, def_src)
                dst_pt = _edge_pt(dst_c, ox_tp, oy_tp, pw_tp, ph_tp,
                                  dst_c.get('edge', def_dst) if dst_c else def_dst)
                src_edge = def_src
                dst_edge = dst_c.get('edge', def_dst) if dst_c else def_dst
                label = media.replace('_', ' ').upper()
                gap_key = (min(fp, tp), max(fp, tp))
                arc_idx = gap_slot_counter.get(gap_key, 0)
                gap_slot_counter[gap_key] = arc_idx + 1
                self.viewer.add_sheet_conn_arc(
                    src_pt, dst_pt, color_hex, confidence, label, bidir,
                    conn_id=conn_id, src_edge=src_edge, dst_edge=dst_edge,
                    arc_index=arc_idx, weight=weight)
                _make_dot(None,  src_pt, ox_fp, oy_fp, color_hex)
                _make_dot(dst_c, dst_pt, ox_tp, oy_tp, color_hex)
                continue

            # One line per src connector — match to nearest dst connector by Y
            used_dst = set()
            for sc in src_list:
                pair_key = (fp, id(sc), media)
                if pair_key in drawn_pairs:
                    continue
                drawn_pairs.add(pair_key)

                # Find nearest unmatched dst connector
                avail = [d for d in dst_list if id(d) not in used_dst]
                if avail:
                    sy = sc.get('y_pdf', 0)
                    dc = min(avail, key=lambda d: abs(d.get('y_pdf', 0) - sy))
                    used_dst.add(id(dc))
                else:
                    dc = None

                src_edge = sc.get('edge') or def_src
                dst_edge = dc.get('edge') if dc else def_dst
                if not dst_edge:
                    dst_edge = def_dst

                src_pt = _edge_pt(sc, ox_fp, oy_fp, pw_fp, ph_fp, src_edge)
                dst_pt = _edge_pt(dc, ox_tp, oy_tp, pw_tp, ph_tp, dst_edge)

                rt = sc.get('raw_text', '')
                rt_clean = re.sub(r'=[\w./\-]+', '', rt)
                rt_clean = re.sub(r'\bS\d{6,8}\b', '', rt_clean, flags=re.I)
                label = ' '.join(rt_clean.split())[:28].strip() or \
                        media.replace('_', ' ').upper()

                gap_key = (min(fp, tp), max(fp, tp))
                arc_idx = gap_slot_counter.get(gap_key, 0)
                gap_slot_counter[gap_key] = arc_idx + 1

                self.viewer.add_sheet_conn_arc(
                    src_pt, dst_pt, color_hex, confidence, label, bidir,
                    conn_id=conn_id, src_edge=src_edge, dst_edge=dst_edge,
                    arc_index=arc_idx, weight=weight)
                _make_dot(sc, src_pt, ox_fp, oy_fp, color_hex)
                _make_dot(dc, dst_pt, ox_tp, oy_tp, color_hex)

    def _update_pen(self):
        self.viewer.set_pen_style(
            self._pen_color, self.width_spin.value(), self.alpha_slider.value())

    def _on_markup_finished(self, pts, page):
        self._pending_markup_pts  = pts
        self._pending_markup_page = page
        self.create_node_btn.setEnabled(True)

    def _create_node_from_markup(self):
        if not self._pending_markup_pts:
            return
        name, ok = QInputDialog.getText(self, "Ny nod", "Namn på nod:", text="Ny nod")
        if not ok:
            return
        name  = name.strip() or "Ny nod"
        style = {'color': self._pen_color.name(),
                 'width': self.width_spin.value(),
                 'alpha': self.alpha_slider.value()}
        node_id = self.db.add_node_with_markup(
            name, self._pending_markup_pts, style, self._pending_markup_page)
        self._pending_markup_pts  = None
        self._pending_markup_page = None
        self.create_node_btn.setEnabled(False)
        self._load_overlays()
        self.node_created.emit(node_id)

    def _on_zone_drawn(self, pdf_rect, page):
        """Right-drag rubber band completed — places a new equipment object
        with the drawn rectangle as its outline shape (2026-08-09, see
        NOTES.md), instead of the generic bowtie-icon fallback the plain
        right-click "🔧 Objekt" action uses. Used to offer a menu of
        objekt/orsak/konsekvens/safeguard here; the P&ID canvas is now
        object-placement-only (2026-08-13, see NOTES.md), so the drawn
        zone always becomes a new equipment object directly — no menu
        needed for a single choice.

        Emits with tag='' immediately (2026-08-18, see NOTES.md
        "kombinerad placeringsmeny") — the native-text/OCR tag search
        used to run SYNCHRONOUSLY right here, so the popup couldn't even
        appear until it finished. place_equipment_marker() now starts
        that same search in the background (EquipmentTagSearchWorker)
        AFTER its popup is already showing."""
        rs = self.viewer.render_scale
        center_scene = QPointF(pdf_rect.center().x() * rs, pdf_rect.center().y() * rs)
        self.equipment_placement_requested.emit('', center_scene, page, pdf_rect)

    def _on_annotation_click(self, scene_pos):
        """Feature 8: create a sticky note annotation at scene_pos."""
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, 'Ny notering', 'Anteckning:')
        if not ok or not text.strip():
            self._set_mode(MODE_NAV); self._annot_btn.setChecked(False); return
        ann_id = self.db.add_board_annotation(
            scene_pos.x(), scene_pos.y(), text=text.strip())
        self._draw_annotation(ann_id, scene_pos.x(), scene_pos.y(),
                              200.0, 80.0, text.strip(), '#fff9c4')
        self._set_mode(MODE_NAV); self._annot_btn.setChecked(False)
        self.annotation_placed.emit(ann_id)

    def _draw_annotation(self, ann_id, x, y, w, h, text, color):
        # Use QGraphicsRectItem + child QGraphicsTextItem so they move together
        # and are treated as one unit by clear_overlays (remove parent = remove child)
        rect = self.viewer._scene.addRect(
            QRectF(0, 0, w, h),        # local coords: origin at (0,0)
            QPen(QColor('#f9a825'), 2),
            QBrush(QColor(color)))
        rect.setPos(x, y)
        rect.setFlag(rect.GraphicsItemFlag.ItemIsMovable, True)
        rect.setFlag(rect.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        rect.setZValue(Z_TEMP + 2)
        rect.setData(0, ('annotation', ann_id))

        # Text as child of rect — moves with it, removed when rect is removed
        from PyQt6.QtWidgets import QGraphicsTextItem
        txt = QGraphicsTextItem(text, rect)   # rect is parent → child of scene via rect
        txt.setPos(6, 4)
        txt.setTextWidth(w - 12)
        txt.setZValue(0.1)   # slightly above parent (rect is at Z_TEMP+2 in scene coords)

    def _on_context_action(self, action, pos, page):
        if action == 'node':
            self._set_mode(MODE_NODE)
        elif action == 'equipment':
            # tag='' — the search runs in the background from
            # place_equipment_marker() instead of synchronously here
            # (2026-08-18, see NOTES.md "kombinerad placeringsmeny").
            self.equipment_placement_requested.emit('', pos, page, None)
        elif action == 'find_similar':
            self._find_similar_symbol(pos, page)
        elif action == 'find_similar_template':
            self._find_similar_symbol_from_template()

    def _find_similar_symbol(self, pos, page):
        """🔎 Hitta liknande symbol (2026-08-10, see NOTES.md) — the click
        point becomes the reference shape; every other vector-drawn
        cluster in the document is scored against it
        (equipment_detection._scan_candidates / symbol_geometry.
        cluster_similarity) and surfaced through the same
        EquipmentMarkerReviewDialog "🎯 Hitta objekt på P&ID" already
        uses, so confirming/renaming/saving works identically.

        2026-08-14 (see NOTES.md "Hitta liknande symbol" —
        sökparametrar): the reference cluster is now resolved FIRST
        (equipment_detection.resolve_reference_cluster) and shown in
        SimilarSymbolSearchDialog for pruning + search-parameter
        choices (threshold/scale/rotation/scope) before the actual
        search runs — previously this ran immediately with fixed
        defaults and no way to fix a reference cluster that pulled in
        an unwanted neighbour (e.g. a pipe stub next to a valve).

        2026-08-15 (see NOTES.md "Hitta liknande symbol" —
        uppföljningsfunktioner): the dialog now runs the document scan
        itself, in a background thread, with live progress/cancel and
        a live match-count/on-canvas preview as the threshold slider
        moves — so by the time it's accepted the result is already
        computed; final_results() reuses it directly instead of this
        method running find_similar_shapes() a second time.

        2026-08-15 follow-up (see NOTES.md "'Hitta liknande symbol'
        visar bara ett streck"): the reference canvas now also shows
        every primitive within a generous radius of the click point
        (symbol_geometry.primitives_near_point), not just what
        auto-detection grouped together — everything outside the
        auto-detected core starts excluded/unchecked, but is still
        there to click and ADD. Needed on very densely-fragmented CAD
        exports where a real symbol's own strokes can end up split
        across many small, ungrouped pieces that auto-detection alone
        has nothing complete to offer for.

        2026-08-15 follow-up (see NOTES.md "Bildbaserad 'hitta liknande
        symbol' — vid sidan av vektorlogiken"): a click with NO nearby
        vector data at all (a scanned page, or just an empty spot) used
        to be a hard dead end here. It now falls through to opening
        SimilarSymbolSearchDialog in forced image-matching mode instead,
        using a scale-sized square around the click point as the
        reference region — exactly the gap
        equipment_detection.find_similar_shapes()'s own docstring
        already flagged as "a separate, not-yet-built undertaking"."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return
        pdf_x, pdf_y = self.viewer.scene_to_pdf(pos)
        resolved = equipment_detection.resolve_reference_cluster(
            self.viewer.pdf_doc, page, pdf_x, pdf_y)
        ref_scale = symbol_geometry.dominant_text_size(self.viewer.pdf_doc[page])
        if resolved is None:
            half = max(ref_scale * 2.0, 20.0)
            fallback_bbox = (pdf_x - half, pdf_y - half, pdf_x + half, pdf_y + half)
            params_dlg = SimilarSymbolSearchDialog(
                None, None, self.viewer._pdf_path, page, ref_scale,
                ref_bbox=fallback_bbox, db=self.db, viewer=self.viewer,
                page_rotations=self.db.get_all_page_rotations(), parent=self)
        else:
            primitives, native_index_group, ref_cluster = resolved
            radius = max(ref_scale * 1.0, 12.0)
            nearby = symbol_geometry.primitives_near_point(
                primitives, pdf_x, pdf_y, radius, scale=ref_scale)
            # Connectivity-grown, not just "nearby" (2026-08-15, see
            # NOTES.md "det jag definerar som ett objekt"): only ACCEPT a
            # nearby primitive once something already accepted touches
            # it (symbol_geometry.widen_by_connectivity), so a tag label
            # or other unrelated content that merely happens to sit
            # within the search radius — but isn't actually connected to
            # the reference — stays out entirely, instead of starting as
            # a "click to add" option the user has to notice and reject.
            wide_index_group = sorted(symbol_geometry.widen_by_connectivity(
                primitives, native_index_group, nearby))
            initial_excluded = set(wide_index_group) - set(native_index_group)

            # Bildmatchning's reference crop is exactly this bbox (see
            # SimilarSymbolSearchDialog._render_image_preview) — using
            # ref_cluster['bbox'] (the auto-detected core ALONE) here cuts
            # the raster reference down to whatever tiny fragment
            # resolve_reference_cluster happened to seed the core from on
            # a densely-fragmented file, so only a sliver of the actual
            # symbol was ever shown or searched with (2026-08-16, see
            # NOTES.md "Bildmatchning klipper fel — visar bara en del av
            # det markerade fältet": confirmed on the active project's own
            # hazop_project_pid.pdf — an instrument bubble's resolved core
            # was a single 6x6pt curve fragment, one corner of the circle,
            # while the wider connectivity-grown group's own bbox tightly
            # covers the whole circle+label). Union over wide_index_group
            # instead — the same set already shown/editable in Vektorform —
            # so switching to Bildmatchning never loses what Vektorform
            # already had.
            # An empty wide_index_group (e.g. resolve_reference_cluster
            # itself returned an empty native group with nothing nearby to
            # widen with — a genuinely empty/degenerate reference) has
            # nothing to union over; fall back to the auto-detected
            # cluster's own bbox rather than crashing on min()/max() of an
            # empty sequence.
            image_ref_bbox = ref_cluster['bbox'] if not wide_index_group else (
                min(primitives[i]['bbox'][0] for i in wide_index_group),
                min(primitives[i]['bbox'][1] for i in wide_index_group),
                max(primitives[i]['bbox'][2] for i in wide_index_group),
                max(primitives[i]['bbox'][3] for i in wide_index_group),
            )

            params_dlg = SimilarSymbolSearchDialog(
                primitives, wide_index_group, self.viewer._pdf_path, page, ref_scale,
                ref_bbox=image_ref_bbox, db=self.db, viewer=self.viewer,
                initial_excluded=initial_excluded, native_index_group=native_index_group,
                page_rotations=self.db.get_all_page_rotations(), parent=self)
        if params_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        results = params_dlg.final_results(comp_type=params_dlg.selected_comp_type())
        if not results:
            QMessageBox.information(
                self, "Inget liknande hittat",
                "Inga tillräckligt lika symboler hittades med de valda "
                "sökinställningarna.")
            return
        review_dlg = EquipmentMarkerReviewDialog(
            results, self.db, parent=self, rejected=[], pdf_path=self.viewer._pdf_path,
            active_node_id=self._active_node_id)
        if review_dlg.exec():
            self.reload_overlays()

    def _find_similar_symbol_from_template(self):
        """"🔎 Hitta liknande symbol (från mall)" (2026-08-15, see
        NOTES.md "Hitta liknande symbol" — uppföljningsfunktioner:
        symbolbibliotek) — search using a previously saved
        Database.symbol_templates() row's features instead of clicking
        a reference point on this specific document. Otherwise
        identical to _find_similar_symbol from here on: same
        SimilarSymbolSearchDialog (in "mall-läge" — no
        _ClusterPreviewCanvas, no rotation toggle) and the same
        EquipmentMarkerReviewDialog confirm/save step."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return
        if not self.db.symbol_templates():
            QMessageBox.information(
                self, "Inga sparade mallar",
                'Inga symbolmallar sparade än. Spara en via "💾 Spara som mall…" '
                'i sökdialogen för "Hitta liknande symbol".')
            return
        picker = SymbolTemplatePickerDialog(self.db, parent=self)
        if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected_template:
            return
        template = picker.selected_template
        ref_features = json.loads(template['features_json'])
        page = self.viewer.current_page

        params_dlg = SimilarSymbolSearchDialog(
            None, None, self.viewer._pdf_path, page, None,
            db=self.db, viewer=self.viewer,
            template_name=template['name'], template_features=ref_features,
            initial_comp_type=template.get('comp_type', ''),
            page_rotations=self.db.get_all_page_rotations(), parent=self)
        if params_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        results = params_dlg.final_results(comp_type=params_dlg.selected_comp_type())
        if not results:
            QMessageBox.information(
                self, "Inget liknande hittat",
                "Inga tillräckligt lika symboler hittades med de valda "
                "sökinställningarna.")
            return
        review_dlg = EquipmentMarkerReviewDialog(
            results, self.db, parent=self, rejected=[], pdf_path=self.viewer._pdf_path,
            active_node_id=self._active_node_id)
        if review_dlg.exec():
            self.reload_overlays()

    def _draw_tag_highlights(self):
        """Highlight complete tag numbers found on the current PDF page.

        Yellow  = tag recognised but not yet a HAZOP cause.
        Green   = tag has at least one defined HAZOP cause.
        Only runs when smart database is enabled OR a tag database is loaded.
        """
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            return
        if not hasattr(self.db, 'tag_db_setting'):
            return

        smart_on  = self.db.tag_db_setting('smart_enabled', '0') == '1'
        tag_codes = set(self.db.all_active_tag_codes()) \
                    if hasattr(self.db, 'all_active_tag_codes') else set()

        # Nothing to do if both sources are inactive
        if not smart_on and not tag_codes:
            return

        try:
            self.viewer.clear_highlights()
            page_num  = self.viewer.current_page
            fitz_page = self.viewer.pdf_doc.load_page(page_num)

            # Tags already used as HAZOP causes (→ green)
            used_tags: set = set()
            try:
                for m in self.db.cause_markers_for_page(page_num):
                    t = (m['component_tag'] or '').upper().strip()
                    if t:
                        used_tags.add(t)
                for node in self.db.nodes():
                    for cause in self.db.causes(node['id']):
                        t = _pick_best_tag(cause['description'])
                        if t:
                            used_tags.add(t.upper())
            except Exception:
                pass

            # Scan page text for complete tag numbers using spatial combining
            raw_words = _rotate_words(fitz_page.get_text("words"), fitz_page)
            seen: set = set()

            for candidate, *_box in _spatial_combine(raw_words, gap_limit=22.0):
                tag = _pick_best_tag(candidate)
                if not tag or tag in seen:
                    continue
                pfx = _equip_prefix_from_tag(tag)
                # Only highlight if prefix is known (in DB or confirmed mapping)
                known = (smart_on or pfx in tag_codes or
                         (hasattr(self.db, 'confirmed_comp_for_tag') and
                          self.db.confirmed_comp_for_tag(pfx)))
                if not known:
                    continue
                seen.add(tag)

                # Find exact bounding box on the page. search_for(), like
                # get_text(), returns raw unrotated-mediabox rects — rotate
                # into this app's PDF space (see _rotate_words) before
                # handing bbox to add_tag_highlight, which draws it in
                # scene coordinates via pdf_to_scene().
                try:
                    mat = fitz_page.rotation_matrix
                    hits = fitz_page.search_for(tag)
                    if not hits:
                        # Try just the code part (e.g., PSV-101 from 20-PSV-101)
                        simple = f"{pfx}-" + tag.split('-')[-1] if '-' in tag else tag
                        hits = fitz_page.search_for(simple)
                    hits = [h * mat for h in hits]
                    for bbox in hits:
                        is_used = tag in used_tags or simple in used_tags \
                                  if 'simple' in dir() else tag in used_tags
                        color = '#90EE90' if is_used else '#FFFFE0'
                        label = f"{'✓ HAZOP-orsak' if is_used else '○ Tagg'}: {tag}"
                        self.viewer.add_tag_highlight(bbox, color, label)
                except Exception:
                    continue

        except Exception:
            pass  # Never crash during highlight drawing

    def _load_overlays(self):
        self.viewer.clear_overlays()
        self.viewer.clear_markup_overlays()
        self.viewer.clear_red_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page   = self.viewer.current_page
        active_pages = sorted(self.viewer._all_page_items.keys())
        all_nodes   = list(self.db.nodes())

        for page in active_pages:
            self.viewer.current_page = page  # ensures pdf_to_scene uses this page's offset

            for node in all_nodes:
                nd      = dict(node)
                raw_pts = nd.get('markup_points', '') or ''
                nd_page = int(nd.get('pid_page', 0) or 0)
                if not raw_pts or nd_page != page:
                    continue
                try:
                    points = [(float(p[0]), float(p[1])) for p in json.loads(raw_pts)]
                    style  = json.loads(nd.get('markup_style', '') or '{}')
                except Exception:
                    continue
                if points:
                    self.viewer.add_node_overlay(nd['id'], points, style, nd.get('name', ''))

            if hasattr(self.db, 'node_markups_for_page'):
                for mu in self.db.node_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#1565C0'),
                        float(m.get('opacity', 0.45)), int(m.get('line_width', 2)),
                        bool(m.get('visible', 1)))

            if hasattr(self.db, 'node_red_markups_for_page'):
                for mu in self.db.node_red_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_red_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#CC0000'),
                        float(m.get('opacity', 1.0)), int(m.get('line_width', 4)),
                        bool(m.get('visible', 1)), int(m.get('font_size', 12)),
                        float(m.get('symbol_w', 40)), float(m.get('symbol_h', 40)),
                        float(m.get('symbol_rot', 0)))

            for m in self.db.equipment_markers_for_page(page):
                md = dict(m)
                eq_id = md.get('equipment_id')
                dev_count = self.db.equipment_deviation_count(eq_id) if eq_id else 0
                # Resolved LIVE from equipment_catalog when linked, same
                # live-FK pattern as ScenarioTablePanel._cause_tag_display
                # (2026-08-18, see NOTES.md "Objektets identitet ...") —
                # equipment_markers.tag/comp_type are just the values at
                # PLACEMENT time and were never updated afterward, so a
                # rename/retype made in the tree, the scenario table's tag
                # popup, or the Utrustningsregister showed correctly
                # everywhere except back on the P&ID marker itself.
                # Falls back to the frozen marker columns for a marker
                # that was never linked to a catalog row.
                eq = self.db.get_equipment_by_id(eq_id) if eq_id else None
                tag_val = (eq.get('tag') or '') if eq else md.get('tag', '')
                comp_type_val = (eq.get('equipment_type') or '') if eq else md.get('comp_type', '')
                cons_count = (self.db.equipment_consequence_count(tag_val, comp_type_val)
                              if tag_val else 0)
                sg_count = (self.db.equipment_safeguard_count(tag_val, comp_type_val)
                            if tag_val else 0)
                self.viewer.add_equipment_marker(
                    md['id'], md['x'], md['y'], comp_type_val,
                    tag_val, md.get('confidence', 0.0) or 0.0,
                    outline_pdf=md.get('shape_outline'), deviation_count=dev_count,
                    consequence_count=cons_count, safeguard_count=sg_count)

        # Feature 8: load sticky note annotations
        if hasattr(self.db, 'get_board_annotations'):
            for ann in self.db.get_board_annotations():
                self._draw_annotation(ann['id'], ann['x'], ann['y'],
                                      ann['w'], ann['h'], ann['text'], ann['color'])

        self.viewer.current_page = orig_page
        self._draw_tag_highlights()
        self._draw_sheet_connections()
        # Reapply LOD so newly added items get correct visibility at current zoom
        self.viewer._apply_lod(self.viewer.transform().m11(), force=True)
        self.viewer._reapply_equipment_selection_overlays()
        self.viewer._reapply_search_highlight()
        # Tree-context highlight (2026-08-27, see NOTES.md) — the cheap
        # remap step, not a DB tree-walk: re-maps the already-cached
        # equipment_id->color scope onto whichever equipment_markers rows
        # exist now. Deliberately NOT also calling
        # viewer._reapply_tree_context_highlights() here — that method
        # exists as a defensive/directly-testable primitive on the view,
        # but _apply_tree_context_highlight() already fully replaces the
        # view's highlight set via set_tree_context_highlights(), so
        # invoking both would just be redundant work.
        self._apply_tree_context_highlight()

    def reload_overlays(self):
        """Public helper to refresh all P&ID markers and connection lines."""
        self._load_overlays()

    # ── Markup editing API (shared node/red markup implementation) ────────────

    def _enter_markup_mode(self, node_id, markup_class):
        self._active_markup_class = markup_class
        if markup_class == 'red':
            self._active_symbol_id = None
        self.set_active_node(node_id)
        self._set_mode(MODE_MARKUP_SELECT)
        # UniqueConnection (PyQt6 raises TypeError on a duplicate attempt
        # rather than silently ignoring it, hence the try/except — same
        # defensive style already used for the disconnects below):
        # enter_markup_edit()/enter_red_markup_edit() are documented as
        # idempotent — re-entering (rebinding to the same or a different
        # node) while already active is expected and, since the
        # 2026-08-19 ribbon merge (see NOTES.md "Slå ihop nodmarkup i
        # nodinställningar"), more frequent — the ✏️ toggle button lets a
        # user flip in/out of markup mode repeatedly for one node without
        # ever calling exit_markup_mode() first. Without this guard, each
        # re-entry stacked another connection, so a single draw fired
        # _on_viewer_markup_drawn/_on_viewer_markup_clicked once per
        # (re-)entry instead of once — a real latent bug found while
        # verifying that merge, not previously exercised because entering
        # only ever happened once per node before.
        try:
            self.viewer.markup_draw_finished.connect(
                self._on_viewer_markup_drawn, Qt.ConnectionType.UniqueConnection)
        except TypeError:
            pass
        try:
            self.viewer.markup_item_clicked.connect(
                self._on_viewer_markup_clicked, Qt.ConnectionType.UniqueConnection)
        except TypeError:
            pass

    def enter_markup_edit(self, node_id):
        """Enter markup editing mode for a node: show existing markup + enable tools."""
        self._enter_markup_mode(node_id, 'node')

    def enter_red_markup_edit(self, node_id):
        """Enter red markup editing mode for a node."""
        self._enter_markup_mode(node_id, 'red')

    def _exit_markup_mode(self, label):
        try: self.viewer.markup_draw_finished.disconnect(self._on_viewer_markup_drawn)
        except RuntimeError as e: logging.warning(f"{label} draw finished signal not connected: {e}")
        try: self.viewer.markup_item_clicked.disconnect(self._on_viewer_markup_clicked)
        except RuntimeError as e: logging.warning(f"{label} item clicked signal not connected: {e}")
        self._active_markup_class = 'node'
        self._active_symbol_id = None
        self._set_mode(MODE_NAV)

    def exit_markup_mode(self):
        """Return to normal navigation mode."""
        self._exit_markup_mode("Markup")

    def exit_red_markup_mode(self):
        """Return to normal navigation mode from red markup."""
        self._exit_markup_mode("Red markup")

    def _set_markup_tool(self, markup_class, tool, color=None, opacity=None, width=None, symbol_id=None):
        if markup_class == 'red':
            _map = {'polygon':  MODE_MARKUP_POLYGON,
                    'polyline': MODE_MARKUP_POLYLINE,
                    'comment':  MODE_MARKUP_COMMENT,
                    'select':   MODE_MARKUP_SELECT,
                    'symbol':   MODE_RED_MARKUP_SYMBOL}
        else:
            _map = {'polygon':  MODE_MARKUP_POLYGON,
                    'polyline': MODE_MARKUP_POLYLINE,
                    'text':     MODE_MARKUP_TEXT,
                    'comment':  MODE_MARKUP_COMMENT,
                    'select':   MODE_MARKUP_SELECT}
        if tool in _map:
            self._set_mode(_map[tool])
        if markup_class == 'red':
            if tool == 'symbol' and symbol_id is not None:
                self._active_symbol_id = symbol_id
            elif tool != 'symbol':
                self._active_symbol_id = None
        if color is not None:
            default_width = 4 if markup_class == 'red' else 3
            default_opacity = 1.0 if markup_class == 'red' else 0.45
            self.viewer.set_pen_style(color, width or default_width, int((opacity or default_opacity) * 210))

    def set_markup_tool(self, tool, color=None, opacity=None, width=None):
        """Set drawing tool: 'polygon'|'polyline'|'text'|'comment'|'select'."""
        self._set_markup_tool('node', tool, color=color, opacity=opacity, width=width)

    def set_red_markup_tool(self, tool, color=None, opacity=None, width=None, symbol_id=None):
        """Set red markup tool: 'polygon'|'polyline'|'comment'|'select'|'symbol'."""
        self._set_markup_tool('red', tool, color=color, opacity=opacity, width=width, symbol_id=symbol_id)

    def _refresh_markup_overlays(self, markup_class):
        if markup_class == 'red':
            self.viewer.clear_red_markup_overlays()
        else:
            self.viewer.clear_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page = self.viewer.current_page
        for page in sorted(self.viewer._all_page_items.keys()):
            self.viewer.current_page = page
            if markup_class == 'red':
                if hasattr(self.db, 'node_red_markups_for_page'):
                    for mu in self.db.node_red_markups_for_page(page):
                        m = dict(mu)
                        try: pts = json.loads(m.get('points', '[]') or '[]')
                        except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                        self.viewer.add_red_markup_overlay(
                            m['id'], m.get('type', 'polygon'), pts,
                            m.get('label', ''), m.get('color', '#CC0000'),
                            float(m.get('opacity', 1.0)), int(m.get('line_width', 4)),
                            bool(m.get('visible', 1)), int(m.get('font_size', 12)),
                            float(m.get('symbol_w', 40)), float(m.get('symbol_h', 40)),
                            float(m.get('symbol_rot', 0)))
            elif hasattr(self.db, 'node_markups_for_page'):
                for mu in self.db.node_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#1565C0'),
                        float(m.get('opacity', 0.45)), int(m.get('line_width', 12)),
                        bool(m.get('visible', 1)),
                        int(m.get('font_size', 12)))
        self.viewer.current_page = orig_page

    def refresh_markup_overlays(self):
        """Reload only the markup overlays (cheap — no cause/cons/sg reload)."""
        self._refresh_markup_overlays('node')

    def refresh_red_markup_overlays(self):
        """Reload only the red markup overlays."""
        self._refresh_markup_overlays('red')

    def _on_viewer_markup_drawn(self, type_, pts, page):
        """Called when user finishes drawing in the viewer; route to appropriate panel."""
        node_id = self._active_node_id
        if node_id is None:
            return
        if self._active_markup_class == 'red':
            # Red markup mode
            if type_ == 'comment':
                label, ok = QInputDialog.getText(self, 'Kommentar', 'Kommentar:')
                if not ok or not label.strip():
                    self.viewer.clear_red_markup_overlays()
                    self.refresh_red_markup_overlays()
                    return
            elif type_ == 'symbol':
                label = self._active_symbol_id or ''
                self._set_mode(MODE_MARKUP_SELECT)
            else:
                label = ''
            self.red_markup_draw_finished.emit(type_, node_id, pts, page, label)
        else:
            # Node markup mode
            if type_ == 'text':
                node = self.db.get_node(node_id) if hasattr(self.db, 'get_node') else None
                label = node['name'] if node else ''
            elif type_ == 'comment':
                label, ok = QInputDialog.getText(self, 'Kommentar', 'Kommentar:')
                if not ok or not label.strip():
                    self.viewer.clear_markup_overlays()
                    self.refresh_markup_overlays()
                    return
            else:
                label = ''
            self.markup_draw_finished.emit(type_, node_id, pts, page, label)

    def _on_viewer_markup_clicked(self, mu_id):
        if self._active_markup_class == 'red':
            self.red_markup_item_selected.emit(mu_id)
        else:
            self.markup_item_selected.emit(mu_id)
            self.viewer.highlight_markup(mu_id)

    def place_cause_from_template(self, dev_id, comp_type, comp_tag, description, frequency,
                                  equipment_id=None):
        """Called by EquipmentDeviationBar._create_cause_for_bar — the only
        remaining caller since the classic P&ID-click cause flow was
        removed (2026-08-13, see NOTES.md: the P&ID canvas is now
        object-placement-only). No cause marker is drawn on the P&ID — the
        equipment marker's own colour/badge already represents "this
        equipment has causes"."""
        # Empty is intentional here: the tag belongs in the object-tag field,
        # not as a pre-filled cause description. None keeps the old fallback.
        label = description if description is not None else (comp_tag or 'Ny orsak')

        try:
            cause_id = self.db.add_cause(dev_id)
        except Exception as e:
            QMessageBox.critical(self, "Databasfel", f"Kunde inte skapa orsak:\n{e}")
            return None
        self.db.update_cause(cause_id, label, comp_type=comp_type, comp_tag=comp_tag,
                             equipment_id=equipment_id)
        if frequency is not None:
            f_level = self._compute_f_level(frequency)
            self.db.update_cause(cause_id, likelihood=f_level, base_freq=frequency)

        # Auto-create an empty consequence + safeguard (2026-08-09, see
        # NOTES.md) so the HAZOP scenario row is immediately ready for
        # direct inline editing on KON and SG — no separate add-
        # consequence/add-safeguard step.
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)

        self._load_overlays()
        self.cause_template_created.emit(cause_id)
        return cause_id

    # ── Equipment marker click → EquipmentDeviationBar (2026-08-07) ────────
    # See NOTES.md "Nod → Utrustning → Avvikelse". Clicking an equipment
    # marker used to always navigate away to the Utrustningsregister
    # (marker_navigated.emit('equipment', ...)); it now opens the bottom
    # bar instead. (2026-08-12: also re-emits marker_navigated again — the
    # bar and the filtered-scenario-table navigation are no longer
    # mutually exclusive, see NOTES.md "de orsaker som visas i hazop
    # scenario är de där objektet finns med".)

    def _on_marker_clicked(self, item_type, item_id):
        if item_type == 'equipment' and self._pending_cause_bind_id is not None:
            row = self.db.conn.execute(
                "SELECT equipment_id FROM equipment_markers WHERE id=?", (item_id,)).fetchone()
            equipment_id = row['equipment_id'] if row else None
            cause_id = self._pending_cause_bind_id
            self._pending_cause_bind_id = None
            self.viewer.setCursor(Qt.CursorShape.ArrowCursor)
            if equipment_id is not None:
                eq = self.db.get_equipment_by_id(equipment_id)
                self.db.update_cause(cause_id,
                                     comp_type=eq.get('equipment_type', '') if eq else '',
                                     comp_tag=eq.get('tag', '') if eq else '',
                                     equipment_id=equipment_id)
                self._load_overlays()
                self.cause_equipment_bound.emit(cause_id, equipment_id)
            return
        if item_type == 'equipment' and self._active_edit_query_fn is not None:
            # Shift+click a marker while an ORS/KON/SG cell is being
            # edited inserts its tag right into the open text instead
            # of the normal navigate-to-equipment flow below, which
            # would tear the editor down via a full scenario-table
            # rebuild (2026-08-13, see NOTES.md: "jag hoppar inte ut ur
            # textediteringsvyn"). Checked via QApplication.keyboardModifiers()
            # rather than threading a modifier through marker_clicked's
            # signature — zero risk to that signal's other callers.
            shift_held = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
            if shift_held:
                target = self._active_edit_query_fn()
                if target is not None:
                    editor, kind, target_id = target
                    row = self.db.conn.execute(
                        "SELECT equipment_id FROM equipment_markers WHERE id=?",
                        (item_id,)).fetchone()
                    eq = self.db.get_equipment_by_id(row['equipment_id']) \
                        if row and row['equipment_id'] is not None else None
                    tag = (eq.get('tag') or '').strip() if eq else ''
                    if tag:
                        self._insert_tag_into_editor(editor, tag)
                        self._sync_tag_ref(kind, target_id, tag,
                                           (eq.get('equipment_type') or '') if eq else '')
                    return   # swallow: no popup, no marker_navigated, no rebuild
        if item_type == 'equipment':
            row = self.db.conn.execute(
                "SELECT equipment_id, x, y, pid_page FROM equipment_markers WHERE id=?",
                (item_id,)).fetchone()
            if row and row['equipment_id'] is not None:
                self._equipment_bar.load(row['equipment_id'], item_id,
                                         active_node_id=self._active_node_id)
                scene_pos = self.viewer.pdf_to_scene(row['x'], row['y'], page=row['pid_page'])
                gp = self.viewer.viewport().mapToGlobal(self.viewer.mapFromScene(scene_pos))
                self._equipment_bar.show_near(gp)
            self.marker_navigated.emit(item_type, item_id)
            return
        self.marker_navigated.emit(item_type, item_id)

    def start_cause_equipment_bind(self, cause_id):
        """Arm the viewer so the next clicked P&ID object is bound to a cause."""
        self._pending_cause_bind_id = int(cause_id)
        self.viewer.setCursor(Qt.CursorShape.CrossCursor)

    def _insert_tag_into_editor(self, editor, tag):
        """Insert `tag` at the cursor of a live ORS/KON/SG QLineEdit
        editor (Shift+click a P&ID marker while editing, 2026-08-13,
        see NOTES.md) — mutates only the open editor's text, exactly as
        if the user had typed it, so the existing commit-on-
        editingFinished path persists it normally and the cursor lands
        ready to keep typing right after. No DB write, no rebuild."""
        pos = editor.cursorPosition()
        text = editor.text()
        before, after = text[:pos], text[pos:]
        if before and not before.endswith(' '):
            before += ' '
        insert = tag if after.startswith(' ') else tag + ' '
        editor.setText(before + insert + after)
        editor.setCursorPosition(len(before) + len(insert))
        editor.setFocus()

    def _sync_tag_ref(self, kind, id_, tag, comp_type):
        """Immediately records `tag` in tagged_refs/comp_tag/comp_type
        (2026-08-13, see NOTES.md: "att den blir fetstil") so the
        description text _insert_tag_into_editor just inserted gets the
        same bold-tag-highlight treatment the drag-and-drop path
        already gives KON/SG cells (_PidDelegate's paint, via
        find_tag_bold_ranges/parse_tag_refs, hazop.py). Deliberately
        does NOT touch the description column here — the live editor's
        full text (already updated) is what the normal edit-commit path
        saves when editing finishes; writing the STALE pre-edit
        description here too would just get overwritten a moment later
        anyway. 'cause' (ORS) has no tagged_refs column at all — its tag
        still lands as plain, un-bolded text via _insert_tag_into_editor.

        Small local reimplementation of hazop.py's add_tag_ref() — can't
        import it directly (hazop.py imports FROM pid_viewer.py, never
        the reverse)."""
        def _add_ref(raw, t):
            refs = [r for r in (s.strip() for s in (raw or '').split(',')) if r and r != t]
            refs.append(t)
            return ','.join(refs)

        if kind == 'consequence':
            row = self.db.get_consequence(id_)
            if not row:
                return
            new_refs = _add_ref(row.get('tagged_refs'), tag)
            self.db.update_consequence(id_, row['description'], row['severity'],
                                        row['category'] or '', row.get('consequence_chain') or '',
                                        comp_tag=tag, comp_type=comp_type, tagged_refs=new_refs)
        elif kind == 'safeguard':
            row = self.db.get_safeguard(id_)
            if not row:
                return
            new_refs = _add_ref(row.get('tagged_refs'), tag)
            self.db.update_safeguard(id_, tagged_refs=new_refs)
            self.db.set_safeguard_tag(id_, tag, comp_type)

    def place_equipment_marker(self, tag, comp_type, scene_pos, page, pdf_rect=None):
        """Callback for the P&ID right-click "🔧 Objekt" action and the
        rubber-band menu's own entry (2026-08-07, extended 2026-08-18 —
        see NOTES.md "kombinerad placeringsmeny"). Resolves an existing
        equipment_catalog row by tag if one exists (never creates a
        duplicate for a tag that's already catalogued) or creates a new
        one, places a marker at the clicked point, and opens
        EquipmentPlacementPopup immediately — tag+typ fields AND the
        deviation checklist together in ONE view, replacing the previous
        two separate, sequential popups (EquipmentTagPopup then
        EquipmentDeviationBar).

        `tag` is normally '' here — the native-text/OCR search now runs
        in the BACKGROUND (started below, after the popup is already
        showing) instead of blocking this whole call until it finishes.
        A non-blank `tag` (still supported — some callers/tests already
        know it) skips the search entirely; the popup just opens already
        filled in.

        `pdf_rect` (2026-08-09, see NOTES.md) — optional QRectF in PDF
        units from the right-drag rubber-band menu's "🔧 Objekt" entry.
        When given, its four corners become the marker's shape_outline so
        it renders with a real outline (like a scanned/auto-detected
        symbol) instead of the generic bowtie-icon fallback a bare point
        gets; the same rectangle is also where the background tag search
        looks first."""
        tag = (tag or '').strip().upper()
        existing = self.db.get_equipment_by_tag(tag) if tag else None
        if existing:
            equipment_id = existing['id']
        else:
            prefix = _equip_prefix_from_tag(tag) if tag else ''
            equipment_id = self.db.add_equipment_item(tag, tag, prefix, page, comp_type, '', 0)

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)
        outline = None
        if pdf_rect is not None:
            outline = [[pdf_rect.left(), pdf_rect.top()], [pdf_rect.right(), pdf_rect.top()],
                       [pdf_rect.right(), pdf_rect.bottom()], [pdf_rect.left(), pdf_rect.bottom()]]
        outline_json = json.dumps(outline) if outline else ''
        marker_id = self.db.add_equipment_marker(
            equipment_id, tag, page, pdf_x, pdf_y, comp_type, shape_outline=outline_json,
            confidence=1.0, link_method='manual')
        self.viewer.add_equipment_marker(marker_id, pdf_x, pdf_y, comp_type, tag=tag,
                                         outline_pdf=outline)

        # Rubber-band placements (pdf_rect given) get the simplified
        # Objekt/Objekttyp-only popup with no deviation checklist
        # (2026-08-24, see NOTES.md) — a plain right-click "🔧 Objekt"
        # placement (pdf_rect=None) keeps the full popup, unchanged.
        simple = pdf_rect is not None
        popup = EquipmentPlacementPopup(self.db, equipment_id, marker_id,
                                        parent=self.viewer, simple=simple)
        if not simple:
            popup.create_cause_fn = (
                lambda dev_id, ct, cmp_tag, desc, freq=None:
                    self._create_cause_for_bar(marker_id, dev_id, ct, cmp_tag, desc, freq))
            # Same tree/scenario-refresh wiring the existing-marker popup
            # (_equipment_bar) already gets in __init__ — missing here was a
            # real bug (2026-08-18 follow-up: "Jag ser dessutom inget i hazop
            # scenario när jag klickar"): the deviation/cause got created in
            # the database just fine, nothing ever told the tree or scenario
            # table to redraw.
            popup.deviation_added.connect(self._on_equipment_deviation_added)
            popup.deviation_removed.connect(self._on_equipment_deviation_removed)
            popup.load_checklist(active_node_id=self._active_node_id)

        if pdf_rect is not None:
            # Position beside the drawn rectangle, not on top of it
            # (2026-08-24, see NOTES.md) — show_near_rect picks whichever
            # side (right/left/below/above) actually has room.
            tl = self.viewer.pdf_to_scene(pdf_rect.left(), pdf_rect.top(), page)
            br = self.viewer.pdf_to_scene(pdf_rect.right(), pdf_rect.bottom(), page)
            gtl = self.viewer.viewport().mapToGlobal(self.viewer.mapFromScene(tl))
            gbr = self.viewer.viewport().mapToGlobal(self.viewer.mapFromScene(br))
            popup.show_near_rect(gtl.x(), gtl.y(), gbr.x(), gbr.y())
        else:
            gp = self.viewer.viewport().mapToGlobal(self.viewer.mapFromScene(scene_pos))
            popup.show_near(gp)

        if not tag and HAS_PYMUPDF and self.viewer.pdf_doc is not None:
            self._start_equipment_tag_search(popup, page, pdf_x, pdf_y, pdf_rect)

    def _start_equipment_tag_search(self, popup, page, pdf_x, pdf_y, pdf_rect):
        """Starts EquipmentTagSearchWorker in the background for a
        freshly-placed object whose tag wasn't known at placement time
        (2026-08-18, see NOTES.md "kombinerad placeringsmeny") — the popup
        is already showing by the time this runs. Whichever finishes
        first, the worker's real result or the configurable timeout
        (default 2s, see settings_panels.py's P&ID-inställningar), wins
        via a `state['done']` flag — same pattern as this session's other
        no-confirm-button live popups (e.g. tree_panel._InlineTreeEdit).
        The worker itself keeps running to completion regardless (OCR
        calls aren't cleanly interruptible mid-call) — a timeout only
        means the UI stops waiting for it, not that the thread is killed;
        its late result is simply ignored via the same flag."""
        working = self._working_pdf_path()
        if not working.exists():
            popup.set_searching(False)
            return
        popup.set_searching(True)

        rect = None
        if pdf_rect is not None:
            rect = (pdf_rect.left(), pdf_rect.top(), pdf_rect.right(), pdf_rect.bottom())
        point = None if rect is not None else (pdf_x, pdf_y)
        worker = EquipmentTagSearchWorker(str(working), page, rect=rect, point=point)
        self._tag_search_workers.append(worker)

        state = {'done': False}

        def cleanup():
            if worker in self._tag_search_workers:
                self._tag_search_workers.remove(worker)

        def on_result(found_tag):
            if not state['done']:
                state['done'] = True
                try:
                    popup.set_detected_tag(found_tag)
                except RuntimeError:
                    pass   # popup already closed — nothing left to update
            cleanup()

        def on_timeout():
            if not state['done']:
                state['done'] = True
                try:
                    popup.set_searching(False)
                except RuntimeError:
                    pass
            # The worker itself is left running — on_result's own
            # cleanup() removes it from the keep-alive list once it
            # genuinely finishes, whether or not the result still matters.

        worker.finished_search.connect(on_result)
        worker.start()
        timeout_ms = int(self.db.get_config('equipment_tag_search_timeout_ms', '2000') or '2000')
        QTimer.singleShot(timeout_ms, on_timeout)

    def _on_equipment_deviation_added(self, deviation_id, equipment_id):
        self._refresh_equipment_marker_visual(equipment_id)
        self.equipment_deviation_created.emit(deviation_id, equipment_id)

    def _on_equipment_deviation_removed(self, deviation_id, equipment_id):
        # Same refresh needs as _on_equipment_deviation_added (marker badge
        # count, tree, worksheet) — see NOTES.md "av-/aktivera".
        self._refresh_equipment_marker_visual(equipment_id)
        self.equipment_deviation_created.emit(deviation_id, equipment_id)

    def _on_equipment_bar_updated(self, equipment_id):
        """EquipmentDeviationBar's tag/typ fields committed a change
        (2026-08-25, see NOTES.md) — redraw so the marker's own label
        picks up the new tag/type immediately, then bubble up so
        MainWindow can refresh the tree (EQUIP_T rows) and scenario table
        (ORS tag strips), which resolve an object's identity live from
        equipment_catalog and don't otherwise know anything changed."""
        self._refresh_equipment_marker_visual(equipment_id)
        self.equipment_updated.emit(equipment_id)

    def _on_equipment_bar_deleted(self, equipment_id):
        """EquipmentDeviationBar's "Ta bort" confirmed (2026-08-25, see
        NOTES.md) — delete_equipment_item() already removed the row and
        cascaded its markers; reload so the now-gone marker(s) actually
        disappear from the canvas, then bubble up for the same tree/
        scenario refresh _on_equipment_bar_updated needs."""
        self._load_overlays()
        self.equipment_deleted.emit(equipment_id)

    def _on_equipment_delete_requested(self, marker_id):
        """Right-click "Ta bort" on an existing equipment marker
        (2026-08-25, see NOTES.md — Anton: "om man högerklickar på
        objektet så ska också alternativet att ta bort finnas"). Confirms
        here (PIDPanel already owns a db connection and can parent a
        QMessageBox directly) rather than bubbling further up, unlike
        equipment_edit_requested — there's no popup/dialog to construct
        that would need MainWindow as its parent, just a yes/no prompt.
        Reuses the same equipment_deleted signal (and MainWindow's same
        tree/scenario refresh) as EquipmentDeviationBar's own delete
        button."""
        row = self.db.conn.execute(
            "SELECT equipment_id FROM equipment_markers WHERE id=?", (marker_id,)).fetchone()
        if not row or row['equipment_id'] is None:
            return
        equipment_id = row['equipment_id']
        eq = self.db.get_equipment_by_id(equipment_id)
        label = (eq.get('tag') or 'objektet') if eq else 'objektet'
        reply = QMessageBox.question(
            self, "Ta bort", f"Ta bort {label}? Objektet och dess markörer på P&ID tas bort.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_equipment_item(equipment_id)
        self._load_overlays()
        self.equipment_deleted.emit(equipment_id)

    def _create_cause_for_bar(self, marker_id, deviation_id, comp_type, comp_tag, description,
                               frequency=None):
        """Callback wired into EquipmentDeviationBar._create_cause_fn AND
        EquipmentPlacementPopup.create_cause_fn (2026-08-18, see NOTES.md
        "kombinerad placeringsmeny") — same creation path as the normal
        cause-template flow, but returns cause_id synchronously so the
        caller can enable/set its frequency combo. `marker_id` is an
        explicit parameter (not read off self._equipment_bar) so this one
        method serves both the reused, persistent bar AND a one-off
        placement popup's own, unrelated marker_id. `frequency`
        (events/year, from standard_causes.frequency when known) is passed
        straight through to place_cause_from_template's existing
        _compute_f_level() conversion — see NOTES.md."""
        marker = self.db.conn.execute(
            "SELECT equipment_id FROM equipment_markers WHERE id=?", (marker_id,)).fetchone()
        if not marker:
            return None
        return self.place_cause_from_template(
            deviation_id, comp_type, comp_tag, description, frequency,
            equipment_id=marker['equipment_id'])


    def _refresh_equipment_marker_visual(self, _equipment_id):
        """Redraw overlays so this equipment's marker picks up its new
        colour/deviation-count badge (or new comp_type/shape after a
        reclassification) — _load_overlays() re-reads every marker's
        current deviation count from the DB, see add_equipment_marker."""
        self._load_overlays()

    def clear_active_selection(self):
        """Reset every id used when placing new cause/consequence/safeguard
        markers on the P&ID, so a deleted-elsewhere cause/consequence can
        never survive as a stale id into a later placement click (root
        cause of the add_consequence FOREIGN KEY crash, 2026-08-07 — see
        NOTES.md). Called on every tree structural change, mirroring the
        equally aggressive reset _on_structure_changed already does for
        the scenario/tree selection."""
        self._active_node_id      = None
        self._active_deviation_id = None
        self._active_cause_id     = None
        self._active_consequence_id = None

    def set_active_node(self, node_id):
        self._active_node_id        = node_id
        self._active_cause_id       = None
        self._active_consequence_id = None

    def set_active_deviation(self, dev_id):
        self._active_deviation_id = dev_id
        dev = self.db.get_deviation(dev_id) if dev_id else None
        if dev:
            self._active_node_id = dict(dev).get('node_id')

    def set_active_cause(self, cause_id):
        self._active_cause_id       = cause_id
        self._active_consequence_id = None
        row = self.db.get_cause(cause_id)
        if row:
            d = dict(row)
            self._active_node_id      = d.get('node_id')
            self._active_deviation_id = d.get('deviation_id')

    def set_active_consequence(self, cons_id):
        self._active_consequence_id = cons_id
        row = self.db.get_consequence(cons_id)
        if not row:
            return
        cause_id = dict(row).get('cause_id')
        self._active_cause_id = cause_id
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                self._active_node_id = dict(cause).get('node_id')

    # ── Tree-context equipment highlight (2026-08-27, see NOTES.md
    # "Dynamisk färgmarkering av objekt på P&ID") ──────────────────────────
    def set_tree_context(self, type_, id_):
        """Called by MainWindow._on_selected on every tree selection
        change (any of NODE_T/DEV_T/CAUSE_T/CONS_T/SG_T/SYSTEM_T, or
        (None, None) to clear) — the EXPENSIVE half of the two-tier
        cache: one DB tree-walk (Database.equipment_link_types_in_scope)
        to find which equipment is in scope and via which link type(s),
        then one QColor lookup per equipment id
        (pid_viewer.resolve_tree_context_color). Also re-run when the
        underlying tag data itself might have changed under an unchanged
        selection (MainWindow._on_scenario_item_edited) — cheap enough
        for a single scope to redo on every edit, unlike a full tree
        walk of the whole study."""
        self._tree_scope_type, self._tree_scope_id = type_, id_
        if id_ is None:
            self._tree_scope_colors = {}
        else:
            link_types_by_equipment = self.db.equipment_link_types_in_scope(type_, id_)
            disabled = {
                role for role, visible in self._tree_context_layer_visibility.items()
                if not visible
            }
            self._tree_scope_colors = {
                eq_id: resolve_tree_context_color(link_types, disabled)
                for eq_id, link_types in link_types_by_equipment.items()
            }
        self._apply_tree_context_highlight()

    def set_tree_context_layer_visibility(self, layer_type, visible):
        """Apply a tree role button state to the current P&ID context.

        Cause/consequence/safeguard objects stay in the context when a role is
        unchecked; only their accent changes to grey.  Recompute from the
        cached selection so no database traversal is needed for a toggle.
        """
        if layer_type not in self._tree_context_layer_visibility:
            return
        self._tree_context_layer_visibility[layer_type] = bool(visible)
        if self._tree_scope_id is not None:
            self.set_tree_context(self._tree_scope_type, self._tree_scope_id)

    def _apply_tree_context_highlight(self):
        """The CHEAP half of the two-tier cache — no DB tree-walk. Re-maps
        the already-cached equipment_id->color scope (_tree_scope_colors,
        set by set_tree_context() above) onto whichever equipment_markers
        rows exist on currently-active pages right now, using the exact
        same per-page equipment_markers_for_page() query _load_overlays()
        already issues — safe/cheap to call after every overlay rebuild
        (page switch, edit) even when the tree selection itself hasn't
        changed, since equipment_markers.equipment_id is the DB's own
        source of truth for which marker belongs to which object (no
        separate id-mapping index to keep in sync)."""
        marker_color_map = {}
        if self._tree_scope_colors:
            for page in self.viewer._all_page_items.keys():
                for m in self.db.equipment_markers_for_page(page):
                    eq_id = m['equipment_id']
                    if eq_id in self._tree_scope_colors:
                        marker_color_map[m['id']] = self._tree_scope_colors[eq_id]
        self.viewer.set_tree_context_highlights(marker_color_map)

    # Maps Excel category strings → component_type keys used in the app
    _CAT_TO_COMP = {
        'instrument':        'Instrument / Sensor',
        'givare':            'Instrument / Sensor',
        'reglerfunktion':    'Instrument / Sensor',
        'larm':              'Instrument / Sensor',
        'brytare':           'Instrument / Sensor',
        'mätvärde':          'Instrument / Sensor',
        'transmitter':       'Instrument / Sensor',
        'reglerventil':      'Ventil',
        'ventil':            'Ventil',
        'pump':              'Pump',
        'kompressor':        'Kompressor',
        'blåsmaskin':        'Kompressor',
        'tank':              'Tank / Kärl',
        'kärl':              'Tank / Kärl',
        'behållare':         'Tank / Kärl',
        'kolonn':            'Tank / Kärl',
        'värmeväxlare':      'Värmeväxlare',
        'kylare':            'Värmeväxlare',
        'kondensor':         'Värmeväxlare',
        'filter':            'Övrigt',
        'sil':               'Övrigt',
        'säkerhetsventil':   'Säkerhetsventil (PSV)',
        'avlastningsventil': 'Säkerhetsventil (PSV)',
        'rörledning':        'Rörledning',
    }

    def _comp_from_db_entry(self, entry: dict) -> str:
        """Map a tag_database entry's category to a component type string."""
        if not entry:
            return ''
        cat = str(entry.get('category', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in cat:
                return comp
        name = str(entry.get('name_sv', '') + ' ' + entry.get('name_en', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in name:
                return comp
        return ''

    def _learn_tag_type(self, tag: str, comp_type: str):
        """Implicitly learn prefix → comp_type from user's own selection.

        Stored as a confirmed entry in pid_identified_tags so it's used
        automatically next time the same prefix is encountered.
        """
        if not tag or not comp_type:
            return
        pfx = _equip_prefix_from_tag(tag)
        if not pfx or len(pfx) < 2:
            return
        try:
            if hasattr(self.db, 'upsert_pid_tag') and hasattr(self.db, 'confirm_pid_tag'):
                self.db.upsert_pid_tag(pfx, tag, '', comp_type)
                self.db.confirm_pid_tag(pfx, comp_type, True)
        except Exception:
            pass

    def _compute_zone_phash(self, page_num: int,
                            cx_pdf: float, cy_pdf: float,
                            w_pdf: float, h_pdf: float) -> str:
        """Compute a 16×16 average-hash for the PDF zone. Returns hex string or ''."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            return ''
        try:
            import fitz as _fitz
            page = self.viewer.pdf_doc.load_page(page_num)
            margin = max(4.0, min(w_pdf, h_pdf) * 0.15)
            clip = _fitz.Rect(cx_pdf - w_pdf / 2 - margin,
                              cy_pdf - h_pdf / 2 - margin,
                              cx_pdf + w_pdf / 2 + margin,
                              cy_pdf + h_pdf / 2 + margin)
            if clip.width <= 0 or clip.height <= 0:
                return ''
            SIZE = 16
            mat = _fitz.Matrix(SIZE / clip.width, SIZE / clip.height)
            pix = page.get_pixmap(matrix=mat, clip=clip,
                                   colorspace=_fitz.csGRAY, alpha=False)
            if pix.width == 0 or pix.height == 0:
                return ''
            pixels = list(pix.samples)
            if not pixels:
                return ''
            mean = sum(pixels) / len(pixels)
            bits = [1 if p >= mean else 0 for p in pixels]
            val = 0
            for b in bits:
                val = (val << 1) | b
            n_hex = (len(bits) + 3) // 4
            return hex(val)[2:].zfill(n_hex)
        except Exception:
            return ''

    def _db_comp_for_tag(self, tag: str) -> str:
        """Return the component type the user has taught for this tag's prefix.

        ONLY uses study_tag_memory — the single source of truth for smart
        recognition.  Populated exclusively by the user's rubber-band markup
        confirmations.  Numbers are ignored (321HV3333 → prefix HV).
        Returns '' if not yet taught or smart recognition is disabled.
        """
        if not tag:
            return ''
        if hasattr(self.db, 'get_config'):
            if self.db.get_config('smart_recognition_enabled', '1') != '1':
                return ''
        pfx = _equip_prefix_from_tag(tag)
        if not pfx:
            return ''
        try:
            return self.db.get_prefix_memory(pfx) if hasattr(self.db, 'get_prefix_memory') else ''
        except Exception:
            return ''

    def _load_mode_freqs(self):
        """Return {comp_type: {mode_desc: freq_per_year}} from DB."""
        if not hasattr(self.db, 'component_types'):
            return {}
        result = {}
        for ct in self.db.component_types():
            freqs = {}
            for fm in self.db.failure_modes(ct['id']):
                if fm['freq_per_year'] is not None:
                    freqs[fm['description']] = fm['freq_per_year']
            result[ct['name']] = freqs
        return result

    def _compute_f_level(self, freq_per_year):
        """Convert frequency (events/year) to F-level using matrix boundaries."""
        if not freq_per_year or freq_per_year <= 0:
            return 3   # default
        cfg        = self.db.get_risk_matrix() if hasattr(self.db, 'get_risk_matrix') else {}
        boundaries = sorted(
            float(b) for b in (cfg or {}).get('freq_boundaries',
                                              [1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0]))
        for i, b in enumerate(boundaries):
            if float(freq_per_year) < b:
                return i - 1
        return len(boundaries) - 1

    def try_reload_pdf(self, override_path=None):
        path = override_path or self.db.get_pid_path()
        if path and Path(path).exists() and HAS_PYMUPDF:
            layout_offsets = None
            if hasattr(self.db, 'get_pid_config_value'):
                raw = self.db.get_pid_config_value('board_layout')
                if raw:
                    try:
                        data = json.loads(raw)
                        layout_offsets = {int(k): v for k, v in data.items()}
                    except Exception:
                        layout_offsets = None
            # Only render sheets that exist in pid_sheets; fall back to all pages
            sheets = self.db.get_sheets()
            active_pages = ([int(s['physical_page']) for s in sheets]
                            if sheets else None)
            if self.viewer.load_pdf(path, page=0, layout_offsets=layout_offsets,
                                    active_pages=active_pages,
                                    page_rotations=self.db.get_all_page_rotations()):
                self.db.ensure_sheets_initialized(self.viewer.page_count(), path)
                self._rebuild_sheet_map()
                self._current_display_page = 0
                self._update_page_label()
                self._load_overlays()
                self.analyze_btn.setEnabled(True)
        else:
            # No P&ID in database — clear the canvas completely
            if self.viewer.pdf_doc is not None:
                try:
                    self.viewer.pdf_doc.close()
                except Exception:
                    pass
                self.viewer.pdf_doc = None
            for item in list(self.viewer._all_page_items.values()):
                try:
                    self.viewer._scene.removeItem(item)
                except Exception:
                    pass
            self.viewer._all_page_items.clear()
            self.viewer._page_offsets.clear()
            self.viewer._page_cache.clear()
            self.viewer._cache_order.clear()
            self.viewer.page_item = None
            self._load_overlays()   # clears all overlay items (pdf_doc is None → returns early)
            self._rebuild_sheet_map()
            self._current_display_page = 0
            self._update_page_label()
            self.viewer._show_placeholder(
                "Ingen P&ID inläst.\nImportera en PDF-fil med knappen ovan.")
            self.analyze_btn.setEnabled(False)
        self.export_btn.setEnabled(True)
