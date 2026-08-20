#!/usr/bin/env python3
"""HAZOP tree panel and its cause/deviation picker dialogs — split out of
hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer"."""

import math
import traceback
from functools import partial

from PyQt6.QtWidgets import (
    QApplication, QWidget, QScrollArea, QTreeWidget, QTreeWidgetItem,
    QTreeWidgetItemIterator, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGridLayout, QLineEdit, QLabel, QPushButton, QComboBox, QDialog,
    QDialogButtonBox, QMessageBox, QGroupBox, QMenu, QSpinBox,
    QDoubleSpinBox, QFrame, QListWidget, QListWidgetItem, QInputDialog,
    QCheckBox, QButtonGroup, QRadioButton, QSplitter,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QRect, QMimeData, QEvent
from PyQt6.QtGui import (
    QFont, QFontMetrics, QColor, QBrush, QPen, QPainter, QPixmap, QPolygonF,
)

from constants import (
    NODE_T, CAUSE_T, CONS_T, SG_T, DEV_T, EQUIP_T, LEDORD_T,
    DEVIATION_TYPES, CONFIG, MARKUP_COLORS, RISK_ICON, SG_TYPES,
)
from database import Database, get_matrix, risk_info, freq_to_f_level
from pid_viewer import _icon, _obj_type_matches
from ui_helpers import (
    freq_axis_label, freq_axis_label_full, _equipment_type_options,
    _lookup_comp_type_for_tag, _make_tag_completer,
    _maybe_save_as_standard_cause, _resolve_std_deviation_id,
    _create_cause_from_pick,
)

class _PickDeviationDialog(QDialog):
    """Small dialog to pick/type a deviation description when adding a new deviation."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lägg till avvikelse")
        self.description = ""
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj eller skriv en avvikelse:"))
        self.combo = QComboBox()
        self.combo.addItems(DEVIATION_TYPES)
        self.combo.setEditable(True)
        self.combo.setCurrentText(DEVIATION_TYPES[0])
        layout.addWidget(self.combo)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.resize(300, 100)

    def _accept(self):
        self.description = self.combo.currentText().strip() or "Övrigt"
        self.accept()


class _InlineTreeEdit(QLineEdit):
    """QLineEdit overlay used for inline tree-item renaming — floats over
    just the editable description portion of a row, leaving the item's own
    numbering/icon prefix untouched underneath (2026-08-18, see NOTES.md
    "trädet: numrering bryts ut"). Escape cancels without committing —
    plain QLineEdit has no such behavior built in outside of an item
    delegate."""

    canceled = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.canceled.emit()
            return
        super().keyPressEvent(event)


class TreePanel(QWidget):
    item_selected               = pyqtSignal(int, int)
    edit_node_markup_requested        = pyqtSignal(int)        # node_id
    node_markup_vis_requested         = pyqtSignal(int, bool)  # node_id, visible
    node_jump_to_markup               = pyqtSignal(int)         # node_id
    structure_changed           = pyqtSignal()
    visibility_changed          = pyqtSignal(str, bool)   # marker_type, visible
    exit_pid_mode_requested     = pyqtSignal()    # exit any active P&ID placement mode
    # Equipment marker(s) dragged from the P&ID onto a deviation item (e.g.
    # "Lågt flöde") — 2026-08-08, see NOTES.md. Args: (deviation_id, list
    # of equipment_markers.id).
    equipment_dropped_on_deviation = pyqtSignal(int, object)
    # A Nod/Avvikelse/Orsak/Konsekvens/Safeguard's text was edited inline in
    # the tree (2026-08-17, see NOTES.md "Dubbelklick -> redigera direkt i
    # trädet") — args: (type_, id_), same shape as ScenarioTablePanel's own
    # item_edited, so MainWindow can refresh scenario/P&ID the same way.
    item_edited_inline = pyqtSignal(int, int)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._clipboard = None  # {'type': T, 'id': id}

        lbl = QLabel("HAZOP-träd")
        f = QFont(); f.setBold(True)
        lbl.setFont(f)
        layout.addWidget(lbl)

        # ── Visibility toggle buttons at TOP (before tree) ──────────────────────
        vis_row = QHBoxLayout()
        vis_row.setSpacing(4)

        _VIS_BTNS = [
            ('cause',        '⚙️ Orsaker',       '#e74c3c', '#fde8e8'),
            ('consequence',  '⚠️ Konsekvenser',  '#e67e22', '#fef0e0'),
            ('safeguard',    '🛡️ Safeguards',    '#27ae60', '#e8f8e8'),
            ('equipment',    '🔧 Utrustning',    '#7f8c8d', '#ecf0f1'),
        ]
        self._vis_btns = {}
        for type_key, label, color_on, color_off in _VIS_BTNS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setFixedHeight(CONFIG['H_CTRL_STD'])
            btn.setStyleSheet(
                f"QPushButton{{background:{color_on}; color:white; border:none;"
                f" border-radius:3px; font-size:10px; font-weight:bold; padding:0 4px;}}"
                f"QPushButton:!checked{{background:{color_off}; color:#aaa;}}")
            btn.toggled.connect(
                lambda checked, t=type_key: self.visibility_changed.emit(t, checked))
            vis_row.addWidget(btn)
            self._vis_btns[type_key] = btn

        layout.addLayout(vis_row)

        # ── Tree action buttons (2nd row) — Nod/Avvikelse have no natural
        # right-click target of their own (they act on the whole tree or
        # need a node selected first), and "Ta bort" is common enough to
        # warrant a one-click button alongside the context-menu entry.
        # Orsak/Konsekvens/Safeguard stay right-click-only (add_deviation on
        # a NODE_T item, add_cause on DEV_T, etc.) since those already have
        # an obvious parent item to right-click.
        action_row = QHBoxLayout()
        action_row.setSpacing(4)
        for label, icon_name, tip, slot in (
            ("+ Nod",       None,     "Lägg till ny nod",       self.add_node),
            ("+ Avvikelse", None,     "Lägg till ny avvikelse", self.add_deviation),
            ("Ta bort",     'delete', "Ta bort markerat",       self.delete_selected),
        ):
            btn = QPushButton(label)
            if icon_name:
                btn.setIcon(_icon(icon_name))
            btn.setToolTip(tip)
            btn.setFixedHeight(CONFIG['H_CTRL_STD'])
            btn.clicked.connect(slot)
            action_row.addWidget(btn)
        layout.addLayout(action_row)

        # ── Tree widget ──────────────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        # Accepts an external drop (equipment marker dragged from the P&ID
        # view onto a deviation, e.g. "Lågt flöde" — 2026-08-08, see
        # NOTES.md) — handled in eventFilter below, not Qt's own internal
        # DragDropMode (this tree has no internal drag-reordering).
        self.tree.setAcceptDrops(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._on_select)
        self.tree.itemDoubleClicked.connect(self._on_item_double_click)
        self._inline_edit_target = None   # (type_, id_) while an inline edit is in progress
        self.tree.installEventFilter(self)   # keyboard navigation (feature 17)
        # Internal drag-and-drop between tree levels (2026-08-17, user
        # request: drag a Cause/Consequence/Safeguard onto a different
        # parent to reparent it there — e.g. dragging a Consequence onto
        # a different object/cause brings its own Safeguards along for
        # free, since they're only ever looked up via the Consequence's
        # own id, never copied/duplicated). Shift+drag copies instead of
        # moving (uses the same copy_cause/copy_consequence/copy_safeguard
        # DB methods already proven by the tree's own right-click
        # Kopiera/Klistra in feature). setDragEnabled + a custom
        # mimeData() override — same instance-level-override convention
        # already used for StandardCausesSettingsPanel._dev_list — rather
        # than Qt's own internal DragDropMode, matching this tree's
        # existing "no built-in drag-reordering" design (see the
        # setAcceptDrops comment above) since reparenting across
        # non-adjacent levels needs custom resolution logic, not a simple
        # row move.
        self.tree.setDragEnabled(True)
        def _tree_mime_data(items, _tree=self.tree):
            md = QMimeData()
            if len(items) == 1:
                type_ = items[0].data(0, Qt.ItemDataRole.UserRole + 1)
                id_ = items[0].data(0, Qt.ItemDataRole.UserRole)
                if type_ in (CAUSE_T, CONS_T, SG_T) and id_ is not None:
                    md.setText(f'hzp:treeitem:{type_}:{id_}')
            return md
        self.tree.mimeData = _tree_mime_data
        layout.addWidget(self.tree)

        # ── Collapse/Expand buttons (compact control bar) ───────────────────────
        compact_row = QHBoxLayout()
        compact_row.setSpacing(4)
        compact_row.addStretch()

        btn_collapse = QPushButton("⊟")
        btn_collapse.setFixedSize(26, 26)
        btn_collapse.setToolTip("Kollapsa alla")
        btn_collapse.clicked.connect(lambda: self.tree.collapseAll())
        compact_row.addWidget(btn_collapse)

        btn_expand = QPushButton("⊞")
        btn_expand.setFixedSize(26, 26)
        btn_expand.setToolTip("Expandera alla")
        btn_expand.clicked.connect(lambda: self.tree.expandAll())
        compact_row.addWidget(btn_expand)

        layout.addLayout(compact_row)

    def _reveal(self, item):
        """setCurrentItem() alone never expands anything (verified against
        PyQt6 directly) — the auto-unfolding came entirely from
        scrollToItem(), which DOES silently expand every collapsed
        ancestor so the item becomes visible. That's fine for Nod/Ledord/
        Utrustning/Avvikelse, but must not force open an Orsak/
        Konsekvens/Safeguard's own collapsed ancestor chain just because
        one was added or selected (2026-08-18, see NOTES.md "trädet:
        öppnar bara till objektet"). Skip the scroll for those types
        unless every ancestor already happens to be expanded — then
        nothing would be force-opened anyway, so scrolling there is just
        a convenience, not new unfolding."""
        self.tree.setCurrentItem(item)
        if item.data(0, Qt.ItemDataRole.UserRole + 1) in self._COLLAPSE_BY_DEFAULT_TYPES:
            p = item.parent()
            while p is not None:
                if not p.isExpanded():
                    return
                p = p.parent()
        self.tree.scrollToItem(item)

    def refresh(self, select_type=None, select_id=None, emit_selection=True):
        expanded = set()
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.isExpanded():
                expanded.add((item.data(0, Qt.ItemDataRole.UserRole + 1),
                              item.data(0, Qt.ItemDataRole.UserRole)))
            it += 1

        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            target = None
            bold_font = QFont(); bold_font.setBold(True)

            def add_cause_children(citem, cause):
                """Append the consequence/safeguard subtree for a single
                cause as children of citem — factored out of
                add_causes_to_item so the equipment-merged trivial-cause
                case below (an empty, just-tagged cause whose only
                content duplicates its own equipment header) can attach
                it directly to that header row instead of a separate,
                redundant cause item (2026-08-10, see NOTES.md "objektet
                redovisas två gånger")."""
                nonlocal target
                for ki, cons in enumerate(self.db.consequences(cause['id']), 1):
                    cause_freq = self.db.cause_frequency_level(cause)
                    level, _, _ = risk_info(cause_freq, cons['severity'])
                    risk_icon = RISK_ICON.get(level, '⚪')
                    kitem = QTreeWidgetItem([f"      {risk_icon}  {ki}. {cons['description'][:40]}"])
                    kitem.setData(0, Qt.ItemDataRole.UserRole, cons['id'])
                    kitem.setData(0, Qt.ItemDataRole.UserRole + 1, CONS_T)
                    kitem.setData(0, self._PREFIX_ROLE, f"      {risk_icon}  {ki}. ")
                    citem.addChild(kitem)
                    if (CONS_T, cons['id']) in expanded: kitem.setExpanded(True)
                    if select_type == CONS_T and select_id == cons['id']: target = kitem

                    for si, sg in enumerate(self.db.safeguards(cons['id']), 1):
                        rrf = (sg['rrf'] or 1) if sg['rrf'] is not None else 1
                        rrf_str = f"RRF{rrf}" if rrf > 1 else "—"
                        try:
                            linked = bool(sg['source_id'])
                        except (IndexError, KeyError):
                            linked = False
                        sg_icon = "🔗🛡" if linked else "🛡"
                        sgitem = QTreeWidgetItem([f"         {sg_icon}  {si}. {sg['description'][:35]}  [{rrf_str}]"])
                        sgitem.setData(0, Qt.ItemDataRole.UserRole, sg['id'])
                        sgitem.setData(0, Qt.ItemDataRole.UserRole + 1, SG_T)
                        sgitem.setData(0, self._PREFIX_ROLE, f"         {sg_icon}  {si}. ")
                        kitem.addChild(sgitem)
                        if select_type == SG_T and select_id == sg['id']: target = sgitem

            def add_causes_to_item(ditem, dev_id):
                """Append the cause/consequence/safeguard subtree for
                deviation dev_id as children of ditem — factored out of
                add_deviation_subtree so the equipment-grouped single-
                deviation case (below) can attach it directly to the
                equipment item instead of a separate, redundant deviation
                item (2026-08-09, see NOTES.md "kaka på kaka")."""
                nonlocal target
                for ci, cause in enumerate(self.db.causes_for_deviation(dev_id), 1):
                    tag    = (cause['comp_tag'] or '').strip() if cause['comp_tag'] else ''
                    desc   = (cause['description'] or '').strip()
                    # A REAL description is always more useful in the tree
                    # than repeating the tag a second row down (the tag is
                    # already visible one level up, on the equipment/
                    # deviation header) — only fall back to the tag for a
                    # still-untouched placeholder cause with nothing else
                    # to show yet (2026-08-11, bug report: a real cause
                    # "Flödesgivare felar -> styrventil stänger" was
                    # showing as just "=E1.M1.QMA127", the same tag its
                    # own parent row already displays, see NOTES.md).
                    trivial_desc = desc in ('', 'Ny orsak')
                    c_label = (tag if tag else desc[:50]) if trivial_desc else desc[:50]
                    citem = QTreeWidgetItem([f"    ⚙ {ci}. {c_label}"])
                    citem.setData(0, Qt.ItemDataRole.UserRole, cause['id'])
                    citem.setData(0, Qt.ItemDataRole.UserRole + 1, CAUSE_T)
                    citem.setData(0, self._PREFIX_ROLE, f"    ⚙ {ci}. ")
                    ditem.addChild(citem)
                    if (CAUSE_T, cause['id']) in expanded: citem.setExpanded(True)
                    if select_type == CAUSE_T and select_id == cause['id']: target = citem
                    add_cause_children(citem, cause)

            def add_deviation_subtree(parent_item, dev, di):
                nonlocal target
                ditem = QTreeWidgetItem([f"  ⬡  {di}. {dev['description'][:55]}"])
                ditem.setData(0, Qt.ItemDataRole.UserRole, dev['id'])
                ditem.setData(0, Qt.ItemDataRole.UserRole + 1, DEV_T)
                ditem.setData(0, self._PREFIX_ROLE, f"  ⬡  {di}. ")
                dev_font = QFont(); dev_font.setItalic(True)
                ditem.setFont(0, dev_font)
                parent_item.addChild(ditem)
                if (DEV_T, dev['id']) in expanded: ditem.setExpanded(True)
                if select_type == DEV_T and select_id == dev['id']: target = ditem
                add_causes_to_item(ditem, dev['id'])

            for ni, node in enumerate(self.db.nodes(), 1):
                node_on_pid = bool(node['markup_points'])
                pid_pin = " 📍" if node_on_pid else ""
                nitem = QTreeWidgetItem([f"  {ni}. {node['name']}{pid_pin}"])
                nitem.setIcon(0, _icon('factory'))
                nitem.setData(0, Qt.ItemDataRole.UserRole, node['id'])
                nitem.setData(0, Qt.ItemDataRole.UserRole + 1, NODE_T)
                nitem.setData(0, self._PREFIX_ROLE, f"  {ni}. ")
                nitem.setFont(0, bold_font)
                nitem.setToolTip(0, node['pid_ref'] or '')
                self.tree.addTopLevelItem(nitem)
                if (NODE_T, node['id']) in expanded: nitem.setExpanded(True)
                if select_type == NODE_T and select_id == node['id']: target = nitem

                # Nod → Ledord → Utrustning → Avvikelse (2026-08-07, see
                # NOTES.md): deviations are grouped by their guide-word text
                # FIRST (several deviation rows across different equipment
                # can share the same description, e.g. "Lågt flöde" for both
                # a pump and a valve under one node), then WITHIN each guide
                # word, split into equipment_id-tagged rows (grouped under a
                # "Utrustning" item) and equipment_id=NULL rows (shown
                # directly under the guide word — every deviation that
                # existed before this feature, unaffected in substance,
                # just one extra grouping level to expand).
                ledord_groups = {}
                for dev in self.db.deviations(node['id']):
                    ledord_groups.setdefault(dev['description'], []).append(dev)

                di = 0
                for description, dev_list in ledord_groups.items():
                    equipment_groups = {}
                    ungrouped_devs = []
                    for dev in dev_list:
                        eq_id = dev['equipment_id']
                        if eq_id:
                            equipment_groups.setdefault(eq_id, []).append(dev)
                        else:
                            ungrouped_devs.append(dev)

                    # Skip the Ledord wrapper for the common case: exactly
                    # one plain (no equipment) deviation for this guide
                    # word — no equipment to distinguish between, so the
                    # wrapper item would just repeat the SAME guide-word
                    # text directly above its own single child (reported:
                    # "varför är det dubbelt?" — every guide word showed
                    # its own name twice). Put the deviation straight under
                    # the node instead, exactly like before this feature
                    # existed. Once a SECOND deviation for this guide word
                    # shows up (equipment-scoped, or another plain one),
                    # the wrapper starts pulling real weight and comes back.
                    if not equipment_groups and len(ungrouped_devs) == 1:
                        di += 1
                        add_deviation_subtree(nitem, ungrouped_devs[0], di)
                        continue

                    # The Ledord wrapper itself carries the guide word's
                    # running number now (2026-08-13 follow-up: "jag vill
                    # att den ska kvarstå så att det alltid syns att det
                    # är exempelvis 16 avikelser") — linking an object to
                    # a guide word switches it from the plain, ungrouped
                    # branch above to this wrapped one, and the number
                    # must not disappear just because of that. Equipment/
                    # deviation-instance items INSIDE this group get their
                    # own separate local counter (sub_di below) so they
                    # never steal from this top-level, one-per-guide-word
                    # sequence — that shared-counter bug is exactly what
                    # caused the earlier "nummereringen blir konstig" report.
                    di += 1
                    sub_di = 0
                    litem = QTreeWidgetItem([f"  ⬡  {di}. {description}"])
                    ledord_key = f"{node['id']}:{description}"
                    litem.setData(0, Qt.ItemDataRole.UserRole, ledord_key)
                    litem.setData(0, Qt.ItemDataRole.UserRole + 1, LEDORD_T)
                    led_font = QFont(); led_font.setItalic(True)
                    litem.setFont(0, led_font)
                    nitem.addChild(litem)
                    if (LEDORD_T, ledord_key) in expanded: litem.setExpanded(True)

                    for eq_id, eq_devs in equipment_groups.items():
                        eq = self.db.get_equipment_by_id(eq_id)
                        etype = (eq.get('equipment_type') or '').strip() if eq else ''
                        # "TAG-ABC —" (2026-08-17, see NOTES.md "ej
                        # definierad-hantering") — an empty equipment_type
                        # used to leave a bare trailing dash with nothing
                        # after it. Now reads "TAG-ABC, ej definierad"
                        # (italic, a visible call to action) instead of
                        # silently looking broken; a real type reads
                        # "TAG-ABC, ventil" (not italic — nothing left to do).
                        undefined = not eq or not etype
                        eq_label = (f"{eq['tag']}, {etype}" if eq and etype
                                    else f"{eq['tag']}, ej definierad" if eq
                                    else f"Utrustning #{eq_id}")
                        eitem = QTreeWidgetItem([f"    {eq_label}"])
                        eitem.setIcon(0, _icon('settings'))
                        # 2026-08-20: objects no longer bold in the tree
                        # (Anton — "Objekt behöver inte vara fetstilta i
                        # hazopträdet"), just the "ej definierad" italic
                        # call-to-action stays.
                        eq_font = QFont()
                        eq_font.setItalic(undefined)
                        eitem.setFont(0, eq_font)
                        eitem.setData(0, self._EQUIP_TAG_ROLE, eq_id)
                        litem.addChild(eitem)
                        if len(eq_devs) == 1:
                            # Collapse the redundant deviation-description
                            # level (2026-08-09, see NOTES.md "kaka på
                            # kaka") — a deviation's description is always
                            # identical to this Ledord group's own label
                            # (grouped by description above), so a separate
                            # child item under the equipment just repeats
                            # text the user already sees one level up. This
                            # item carries the DEVIATION's identity instead
                            # of EQUIP_T (get_or_create_deviation makes this
                            # the only deviation for this equipment+guide-word
                            # combo in practice), so it's the direct,
                            # interactive target for "add cause" and
                            # equipment-dropped-on-deviation — previously
                            # dead ends when the row was EQUIP_T.
                            dev = eq_devs[0]
                            # NOT "di += 1" here (2026-08-13 bug report:
                            # "Nummereringen ... blir konstig när man
                            # lägger till objekt i trädet") — this branch's
                            # eitem keeps its equipment-tag label from
                            # above (never relabelled with "{di}. "), so
                            # bumping the counter here silently ate a
                            # number that the NEXT plain guide word's
                            # add_deviation_subtree() call would otherwise
                            # have shown, making every later number one
                            # (or more) higher than it should be as soon
                            # as any object got added to a node.
                            dev_causes = self.db.causes_for_deviation(dev['id'])
                            merge_tag = ((eq['tag'] or '').strip() if eq else '')
                            trivial_desc = (dev_causes[0]['description'] or '').strip() in ('', 'Ny orsak') \
                                if dev_causes else False
                            if (len(dev_causes) == 1
                                    and trivial_desc
                                    and (dev_causes[0]['comp_tag'] or '').strip() == merge_tag
                                    and merge_tag):
                                # One more "kaka på kaka" level (2026-08-10,
                                # see NOTES.md "objektet redovisas två
                                # gånger"): this deviation's only cause has
                                # no real content yet — created empty by a
                                # drag-and-drop tag placement — so its own
                                # tree label falls back to the SAME
                                # equipment tag this header row already
                                # shows. Attach the cause's identity (and
                                # its consequences) directly to this row
                                # instead of a redundant child that repeats
                                # the tag a second time with nothing new to
                                # say. Reappears as a normal child row the
                                # moment the cause gets a real description,
                                # or a second cause is added.
                                cause = dev_causes[0]
                                eitem.setData(0, Qt.ItemDataRole.UserRole, cause['id'])
                                eitem.setData(0, Qt.ItemDataRole.UserRole + 1, CAUSE_T)
                                if (CAUSE_T, cause['id']) in expanded: eitem.setExpanded(True)
                                if select_type == CAUSE_T and select_id == cause['id']: target = eitem
                                add_cause_children(eitem, cause)
                            else:
                                eitem.setData(0, Qt.ItemDataRole.UserRole, dev['id'])
                                eitem.setData(0, Qt.ItemDataRole.UserRole + 1, DEV_T)
                                if (DEV_T, dev['id']) in expanded: eitem.setExpanded(True)
                                if select_type == DEV_T and select_id == dev['id']: target = eitem
                                add_causes_to_item(eitem, dev['id'])
                        else:
                            eitem.setData(0, Qt.ItemDataRole.UserRole, eq_id)
                            eitem.setData(0, Qt.ItemDataRole.UserRole + 1, EQUIP_T)
                            if (EQUIP_T, eq_id) in expanded: eitem.setExpanded(True)
                            if select_type == EQUIP_T and select_id == eq_id: target = eitem
                            for dev in eq_devs:
                                sub_di += 1
                                add_deviation_subtree(eitem, dev, sub_di)

                    for dev in ungrouped_devs:
                        # Every node is auto-seeded with one empty, generic
                        # (equipment_id=NULL) deviation per guide word — see
                        # add_node(). Once THIS SAME guide word also has a
                        # real equipment-scoped entry, the still-empty
                        # generic one is just unused scaffolding sitting
                        # right next to it under the same Ledord label —
                        # reads as "Lågt flöde" appearing twice. Hide it
                        # (not delete — reappears the moment it gets a real
                        # cause, and non-empty generic entries always show).
                        if equipment_groups and not self.db.causes_for_deviation(dev['id']):
                            continue
                        di += 1
                        add_deviation_subtree(litem, dev, di)

            if target and not emit_selection:
                # Update the tree's visual highlight while signals are still
                # blocked, so setCurrentItem does NOT cascade into
                # currentItemChanged -> _on_select -> item_selected -> _on_selected.
                # Callers that pass emit_selection=False (e.g. _on_marker_navigate)
                # already trigger the selection-handling logic explicitly afterward,
                # so we must not let the tree fire it a second time here.
                self._reveal(target)
        finally:
            self.tree.blockSignals(False)
            if target and emit_selection:
                self._reveal(target)

    def _current(self):
        item = self.tree.currentItem()
        if item is None:
            return None, None
        return (item.data(0, Qt.ItemDataRole.UserRole + 1),
                item.data(0, Qt.ItemDataRole.UserRole))

    def _resolve_node_id(self, type_, id_):
        if type_ == NODE_T: return id_
        if type_ == EQUIP_T: return self.db.equipment_node_id(id_)
        if type_ == LEDORD_T:
            # id_ is "node_id:description" (see refresh()) — LEDORD_T has no
            # DB row of its own, but the node_id is encoded right in the key.
            try:
                return int(str(id_).split(':', 1)[0])
            except (ValueError, IndexError):
                return None
        if type_ == DEV_T:
            r = self.db.get_deviation(id_); return r['node_id'] if r else None
        if type_ == CAUSE_T:
            r = self.db.get_cause(id_); return r['node_id'] if r else None
        if type_ == CONS_T:
            r = self.db.get_consequence(id_)
            if r:
                c = self.db.get_cause(r['cause_id']); return c['node_id'] if c else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id'])
                if c:
                    ca = self.db.get_cause(c['cause_id']); return ca['node_id'] if ca else None
        return None

    def _resolve_equipment_id(self, type_, id_):
        """Walk any tree item back to the equipment it's grouped under, or
        None if it sits directly under a node (no equipment_id set on its
        deviation) — see 'Nod → Utrustning → Avvikelse' in NOTES.md."""
        if type_ == EQUIP_T: return id_
        dev_id = self._resolve_deviation_id(type_, id_) if type_ != DEV_T else id_
        if dev_id is None:
            return None
        r = self.db.get_deviation(dev_id)
        return r['equipment_id'] if r else None

    def _resolve_deviation_id(self, type_, id_):
        if type_ == DEV_T: return id_
        if type_ == CAUSE_T:
            r = self.db.get_cause(id_); return r['deviation_id'] if r else None
        if type_ == CONS_T:
            r = self.db.get_consequence(id_)
            if r:
                c = self.db.get_cause(r['cause_id'])
                return c['deviation_id'] if c else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id'])
                if c:
                    ca = self.db.get_cause(c['cause_id'])
                    return ca['deviation_id'] if ca else None
        return None

    def _resolve_cause_id(self, type_, id_):
        if type_ == CAUSE_T: return id_
        if type_ == CONS_T:
            r = self.db.get_consequence(id_); return r['cause_id'] if r else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id']); return c['cause_id'] if c else None
        return None

    def _resolve_consequence_id(self, type_, id_):
        if type_ == CONS_T: return id_
        if type_ == SG_T:
            r = self.db.get_safeguard(id_); return r['consequence_id'] if r else None
        return None

    def add_node(self):
        new_id = self.db.add_node()
        self.refresh(NODE_T, new_id)
        self.structure_changed.emit()

    def _rename_node(self, node_id):
        """Right-click "✏️ Döp om" on a node (2026-08-12, see NOTES.md —
        reported feedback: "jag vill kunna döpa om noder genom att
        högerklicka på trädet"). A node could already be renamed via
        PropertiesRibbon's own "Namn"/"P&ID-ref" popup, but only after
        first selecting it there — this adds the more direct path asked
        for. Only the name changes; every other field round-trips through
        update_node() unchanged, same as PropertiesRibbon's own rename."""
        node = self.db.get_node(node_id)
        if not node:
            return
        node_d = dict(node)
        name, ok = QInputDialog.getText(self, "Döp om nod", "Nytt namn:",
                                         text=node_d.get('name') or '')
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        self.db.update_node(node_id, name, node_d.get('description') or '',
                             node_d.get('pid_ref') or '', node_d.get('media') or '',
                             node_d.get('pressure') or '', node_d.get('temperature') or '')
        self.refresh(NODE_T, node_id)
        self.structure_changed.emit()

    def add_deviation(self):
        type_, id_ = self._current()
        node_id = self._resolve_node_id(type_, id_) if type_ else None
        if node_id is None:
            QMessageBox.information(self, "Välj nod", "Välj en nod i trädet."); return
        dlg = _PickDeviationDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_id = self.db.add_deviation(node_id, dlg.description)
        self.refresh(DEV_T, new_id)
        self.structure_changed.emit()

    def add_cause(self):
        type_, id_ = self._current()
        dev_id = self._resolve_deviation_id(type_, id_) if type_ else None
        if dev_id is None:
            QMessageBox.information(self, "Välj avvikelse", "Välj en avvikelse i trädet."); return
        self._open_cause_picker_for_deviation(dev_id, self._resolve_node_id(type_, id_))

    def _open_cause_picker_for_deviation(self, dev_id, node_id=None):
        """Open the standard-causes picker to create a new cause under dev_id.
        Shared by every 'add cause under this deviation' entry point in the
        tree so a freshly created cause always starts with real content
        instead of a silent blank 'Ny orsak' placeholder.
        """
        dev = self.db.get_deviation(dev_id)
        dev_desc = dev['description'] if dev else ''
        if node_id is None:
            node_id = dev['node_id'] if dev else None
        std_dev_id = _resolve_std_deviation_id(self.db, dev_desc)

        popup = StandardCausesPickerPopup(
            self.db, std_dev_id, deviation_name=dev_desc,
            node_id=node_id, parent=self)

        def _on_picked(description, frequency):
            new_id, _cons_id = _create_cause_from_pick(self.db, dev_id, description, frequency)
            self.refresh(CAUSE_T, new_id)
            self.structure_changed.emit()

        popup.cause_picked.connect(_on_picked)
        popup.exec()

    def add_consequence(self):
        type_, id_ = self._current()
        cause_id = self._resolve_cause_id(type_, id_) if type_ else None
        if cause_id is None:
            QMessageBox.information(self, "Välj cause", "Välj en cause i trädet."); return
        new_id = self.db.add_consequence(cause_id)
        self.exit_pid_mode_requested.emit()
        self.refresh(CONS_T, new_id)
        self.structure_changed.emit()

    def add_safeguard(self):
        type_, id_ = self._current()
        cons_id = self._resolve_consequence_id(type_, id_) if type_ else None
        if cons_id is None:
            QMessageBox.information(self, "Välj konsekvens", "Välj en konsekvens i trädet."); return
        new_id = self.db.add_safeguard(cons_id)
        self.exit_pid_mode_requested.emit()
        self.refresh(SG_T, new_id)
        self.structure_changed.emit()

    def delete_selected(self):
        type_, id_ = self._current()
        if type_ is None: return
        names = {NODE_T: 'noden', DEV_T: 'avvikelsen', CAUSE_T: 'orsaken',
                 CONS_T: 'konsekvensen', SG_T: 'safeguarden'}
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort {names.get(type_, 'objektet')} och allt under den?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        deletors = {NODE_T: self.db.delete_node, DEV_T: self.db.delete_deviation,
                    CAUSE_T: self.db.delete_cause, CONS_T: self.db.delete_consequence,
                    SG_T: self.db.delete_safeguard}
        if type_ in deletors:
            deletors[type_](id_)
        self.refresh()
        self.structure_changed.emit()

    def _on_select(self, current, _previous):
        if current is None: return
        type_ = current.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = current.data(0, Qt.ItemDataRole.UserRole)
        self.item_selected.emit(type_, id_)

    # Types editable directly in the tree via double-click (2026-08-17, see
    # NOTES.md "Dubbelklick -> redigera direkt i trädet"). EQUIP_T/LEDORD_T
    # are pure grouping views with no DB row of their own — not included.
    _INLINE_EDIT_TYPES = (NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T)

    # Revealing an item deeper than "objektet" (Nod/Ledord/Utrustning/
    # Avvikelse) must never force those levels open by itself (2026-08-18
    # user request: "by default inte öppnar upp trädet mer än till
    # objektet ... skippa orsakstexten, konsekvensen och safeguards" — a
    # cause/consequence/safeguard added anywhere in the app, not just the
    # tree, used to silently unfold its whole ancestor chain via
    # refresh()'s _reveal(), defeating the tree's use as an overview).
    # Still opens exactly like any other branch on an explicit manual
    # click — this only stops the AUTOMATIC reveal-on-select.
    _COLLAPSE_BY_DEFAULT_TYPES = (CAUSE_T, CONS_T, SG_T)

    # Stores each item's numbering/icon prefix (e.g. "  ⬡  1. ") separately
    # from its DB id/type roles, so inline editing can position an overlay
    # editor AFTER the prefix instead of reverse-engineering it out of the
    # (possibly truncated) decorated display text (2026-08-18, see NOTES.md
    # "trädet: numrering bryts ut").
    _PREFIX_ROLE = Qt.ItemDataRole.UserRole + 2
    _PREFIX_ICON_W = 18   # approximate rendered icon width + spacing

    # Set on every equipment-tag header row ("TAG-101, Ventil"), regardless
    # of whether its own type_/id_ data identifies it as EQUIP_T or (via the
    # "kaka på kaka" collapse, see refresh()) as the single DEV_T/CAUSE_T it
    # merged with — double-clicking any row carrying this must always open
    # the equipment type picker, never inline-edit the deviation/cause text
    # that row happens to be standing in for (2026-08-18 bug report:
    # "dubbelklickar jag på ett objekt (taggen) så kommer avvikelsetexten
    # upp").
    _EQUIP_TAG_ROLE = Qt.ItemDataRole.UserRole + 3

    def _on_item_double_click(self, item, col):
        if item is None:
            return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        if type_ == NODE_T and self.db.has_node_markups(id_):
            self.node_jump_to_markup.emit(id_)
            return
        if type_ == EQUIP_T:
            self._open_equipment_tag_popup(item, id_)
            return
        equip_tag_id = item.data(0, self._EQUIP_TAG_ROLE)
        if equip_tag_id is not None:
            self._open_equipment_tag_popup(item, equip_tag_id)
            return
        if type_ in self._INLINE_EDIT_TYPES:
            self._begin_inline_edit(item, type_, id_)

    def _open_equipment_tag_popup(self, item, eq_id):
        """Double-click an equipment/tag header row -> the same
        minimalistic Tag+Typ popup already used for a tag click in the
        HAZOP scenario table (CauseTagPopup, see scenario_panel.py's
        _show_cause_obj_popup) — 2026-08-18 user request: "samma typ av
        ruta dyker upp ... på om man dubbelklickar på objekt i trädet så
        jag kan ändra Tag och typ av objekt på detta sätt." Reached both
        for a genuinely EQUIP_T row AND for the common "kaka på kaka"
        case where a single-deviation equipment group collapses onto a
        DEV_T/CAUSE_T row instead (see refresh()'s _EQUIP_TAG_ROLE) —
        that row visually still shows the equipment tag, so its
        double-click must edit the tag, not inline-edit the
        deviation/cause text it happens to be standing in for
        (2026-08-18 bug report: double-clicking a tag row showed the
        deviation's own text instead)."""
        eq = self.db.get_equipment_by_id(eq_id)
        if not eq:
            return
        rect = self.tree.visualItemRect(item)
        global_pos = self.tree.viewport().mapToGlobal(rect.bottomLeft())

        popup = CauseTagPopup(self.db, eq.get('equipment_type') or '', eq.get('tag') or '',
                               parent=self)
        popup.committed.connect(
            lambda comp_type, tag, eqid=eq_id: self._apply_equipment_tag_edit(eqid, comp_type, tag))
        popup.adjustSize()
        scr = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = scr.availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x, y = global_pos.x(), global_pos.y() + 2
        if y + ph > screen.bottom(): y = global_pos.y() - ph - 2
        if x + pw > screen.right(): x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top() + 4, y)
        popup.move(x, y)
        popup.show()

    def _apply_equipment_tag_edit(self, eq_id, comp_type, tag):
        eq = self.db.get_equipment_by_id(eq_id)
        if not eq:
            return
        new_tag = tag.strip() or (eq.get('tag') or '')
        new_type = comp_type.strip() or (eq.get('equipment_type') or '')
        self.db.update_equipment_item(
            eq_id, new_tag, eq.get('prefix') or '', new_type, eq.get('description') or '')
        self.refresh(EQUIP_T, eq_id, emit_selection=False)
        self.item_edited_inline.emit(EQUIP_T, eq_id)

    def _raw_text_for(self, type_, id_):
        """Current raw description for an editable item, fetched fresh from
        the DB rather than reverse-engineered from the tree's decorated
        display text (numbering/icons/emoji/truncation baked directly into
        item text at construction time, see add_deviation_subtree etc.)."""
        if type_ == NODE_T:
            node = self.db.get_node(id_)
            return (node.get('name') or '') if node else ''
        if type_ == DEV_T:
            dev = self.db.get_deviation(id_)
            return (dev.get('description') or '') if dev else ''
        if type_ == CAUSE_T:
            cause = self.db.get_cause(id_)
            return (cause.get('description') or '') if cause else ''
        if type_ == CONS_T:
            cons = self.db.get_consequence(id_)
            return (cons.get('description') or '') if cons else ''
        if type_ == SG_T:
            sg = self.db.get_safeguard(id_)
            return (sg.get('description') or '') if sg else ''
        return ''

    def _begin_inline_edit(self, item, type_, id_):
        """Opens a floating QLineEdit over just the description portion of
        the row, positioned right after the item's stored numbering/icon
        prefix (_PREFIX_ROLE) — unlike Qt's native "double-click to edit
        item text" (which replaces the WHOLE cell), the item's own
        column-0 text is never touched, so the "N. "/emoji numbering stays
        visible underneath for the entire edit (2026-08-18 user report:
        "när jag dubbelklickar på trädet så försvinner numreringen")."""
        raw = self._raw_text_for(type_, id_)
        self._inline_edit_target = (type_, id_)

        rect = self.tree.visualItemRect(item)
        prefix = item.data(0, self._PREFIX_ROLE) or ''
        icon_w = self._PREFIX_ICON_W if not item.icon(0).isNull() else 0
        x = rect.x() + icon_w + QFontMetrics(item.font(0)).horizontalAdvance(prefix)
        width = max(rect.right() - x, 60)

        editor = _InlineTreeEdit(self.tree.viewport())
        editor.setText(raw)
        editor.setGeometry(x, rect.y(), width, rect.height())

        state = {'done': False}

        def finish(save):
            if state['done']:
                return
            state['done'] = True
            text = editor.text().strip()
            editor.deleteLater()
            self._inline_edit_target = None
            if save:
                self._commit_inline_text(type_, id_, text)

        editor.editingFinished.connect(lambda: finish(True))
        editor.canceled.connect(lambda: finish(False))
        editor.show()
        editor.setFocus()
        editor.selectAll()

    def _commit_inline_text(self, type_, id_, text):
        if type_ == NODE_T:
            node = self.db.get_node(id_)
            if node:
                self.db.update_node(id_, text, node.get('description') or '',
                                     node.get('pid_ref') or '', node.get('media') or '',
                                     node.get('pressure') or '', node.get('temperature') or '')
        elif type_ == DEV_T:
            self.db.update_deviation(id_, text)
        elif type_ == CAUSE_T:
            self.db.update_cause(id_, description=text)
        elif type_ == CONS_T:
            cons = self.db.get_consequence(id_)
            if cons:
                self.db.update_consequence(id_, text, cons['severity'], cons['category'] or '')
        elif type_ == SG_T:
            sg = self.db.get_safeguard(id_)
            self.db.update_safeguard(id_, text, (sg['rrf'] or 1) if sg else 1)

        self.refresh(type_, id_, emit_selection=False)
        self.item_edited_inline.emit(type_, id_)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None: return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        if type_ in (EQUIP_T, LEDORD_T):
            # Both are live grouping views (over equipment_catalog/deviations,
            # or over deviation description text), not their own DB row —
            # nothing to add/copy/delete here from the tree (use the
            # equipment's own bottom bar on the P&ID, or the Utrustningsregister).
            return
        menu  = QMenu(self)

        if type_ == NODE_T:
            menu.addAction(_icon('edit'), "Döp om", lambda i=id_: self._rename_node(i))
            menu.addAction("+ Lägg till avvikelse", self.add_deviation)
            menu.addAction(_icon('edit'), "Editera nodmarkup",
                           lambda i=id_: self.edit_node_markup_requested.emit(i))
            if self.db.has_node_markups(id_):
                is_vis = self.db.has_visible_node_markups(id_)
                if is_vis:
                    menu.addAction("🙈 Dölj nod på P&ID",
                                   lambda i=id_: self.node_markup_vis_requested.emit(i, False))
                else:
                    menu.addAction(_icon('eye'), "Visa nod på P&ID",
                                   lambda i=id_: self.node_markup_vis_requested.emit(i, True))
        elif type_ == DEV_T:
            menu.addAction("+ Lägg till orsak", self.add_cause)
        elif type_ == CAUSE_T:
            # "+ Lägg till orsak" also offered here (not just on DEV_T) so
            # a cause row that merged with its deviation's own header
            # (2026-08-10, see NOTES.md "objektet redovisas två gånger")
            # still lets you add a SECOND, distinct cause to the same
            # deviation — add_cause() already resolves the deviation via
            # the cause's own deviation_id regardless of which row type
            # triggered it.
            menu.addAction("+ Lägg till orsak", self.add_cause)
            menu.addAction("+ Lägg till konsekvens", self.add_consequence)
        elif type_ == CONS_T:
            menu.addAction("+ Lägg till safeguard", self.add_safeguard)

        # Copy
        copy_labels = {CAUSE_T: "Kopiera orsak",
                       CONS_T:  "Kopiera konsekvens",
                       SG_T:    "Kopiera safeguard"}
        if type_ in copy_labels:
            menu.addAction(_icon('clipboard'), copy_labels[type_],
                           lambda t=type_, i=id_: self._copy_item(t, i))

        # Paste (only if clipboard is compatible with current target)
        if self._clipboard:
            ct = self._clipboard['type']
            can_paste = (
                (ct == CAUSE_T and type_ in (NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T)) or
                (ct == CONS_T  and type_ in (CAUSE_T, CONS_T, SG_T)) or
                (ct == SG_T    and type_ in (CONS_T, SG_T))
            )
            if can_paste:
                menu.addAction(_icon('clipboard'), "Klistra in här", self._paste_item)

        menu.addSeparator()
        menu.addAction("Ta bort", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy_item(self, type_, id_):
        self._clipboard = {'type': type_, 'id': id_}

    def _paste_item(self):
        if not self._clipboard:
            return
        ct    = self._clipboard['type']
        cid   = self._clipboard['id']
        type_, id_ = self._current()

        if ct == CAUSE_T:
            dev_id = self._resolve_deviation_id(type_, id_)
            if not dev_id:
                # Fall back: get or create "Övrigt" deviation on the resolved node
                node_id = self._resolve_node_id(type_, id_)
                if not node_id:
                    return
                dev_id = self.db.get_or_create_deviation(node_id)
            new_id = self.db.copy_cause(cid, dev_id)
            if new_id:
                self.refresh(CAUSE_T, new_id)
                self.structure_changed.emit()

        elif ct == CONS_T:
            cause_id = self._resolve_cause_id(type_, id_)
            if not cause_id:
                return
            new_id = self.db.copy_consequence(cid, cause_id)
            if new_id:
                self.refresh(CONS_T, new_id)
                self.structure_changed.emit()

        elif ct == SG_T:
            # Resolve consequence
            cons_id = None
            if type_ == CONS_T:
                cons_id = id_
            elif type_ == SG_T:
                sg = self.db.get_safeguard(id_)
                if sg:
                    cons_id = sg['consequence_id']
            if not cons_id:
                return
            new_id = self.db.copy_safeguard(cid, cons_id)
            if new_id:
                self.refresh(SG_T, new_id)
                self.structure_changed.emit()

    # ── Feature 17: keyboard navigation ───────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        # ── External drop: equipment marker(s) dragged from the P&ID onto a
        # deviation item (2026-08-08, see NOTES.md). Qt delivers drag/drop
        # events to the tree's VIEWPORT, not the outer QTreeWidget — see
        # the identical lesson in ScenarioTablePanel.eventFilter — so both
        # objects are accepted defensively.
        _drop_targets = (self.tree, self.tree.viewport())
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:equipment'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:equipment'):
                if self._deviation_item_at(event, obj) is not None:
                    event.acceptProposedAction()
                else:
                    event.ignore()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:equipment'):
                self._handle_equipment_drop(event, obj)
                return True

        # ── Internal drag-and-drop between tree levels (2026-08-17) ──────────
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:treeitem:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:treeitem:'):
                if self._tree_reparent_target_at(event, obj) is not None:
                    event.acceptProposedAction()
                else:
                    event.ignore()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:treeitem:'):
                self._handle_tree_reparent_drop(event, obj)
                return True

        if obj is not self.tree or event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        key  = event.key()
        item = self.tree.currentItem()
        if item is None:
            return False
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)

        if key == Qt.Key.Key_Right:
            if item.childCount():
                item.setExpanded(True)
                self.tree.setCurrentItem(item.child(0))
            return True
        if key == Qt.Key.Key_Left:
            if item.isExpanded():
                item.setExpanded(False)
            elif item.parent():
                self.tree.setCurrentItem(item.parent())
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Add child at next level
            if type_ == NODE_T:
                self.add_cause()
            elif type_ == DEV_T and id_ is not None:
                self._add_cause_for_deviation(id_)
            elif type_ == CAUSE_T and id_ is not None:
                new_id = self.db.add_consequence(id_)
                self.refresh(CONS_T, new_id); self.structure_changed.emit()
            elif type_ == CONS_T and id_ is not None:
                new_id = self.db.add_safeguard(id_)
                self.refresh(SG_T, new_id); self.structure_changed.emit()
            return True
        if key == Qt.Key.Key_Delete and id_ is not None:
            label = {NODE_T: 'nod', DEV_T: 'avvikelse', CAUSE_T: 'orsak',
                     CONS_T: 'konsekvens', SG_T: 'safeguard'}.get(type_, 'objekt')
            if QMessageBox.question(
                    self, 'Ta bort', f'Ta bort {label}?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
                self._delete_item(type_, id_)
            return True
        return False

    def _event_pos_in_viewport(self, event, source_obj):
        """Drag/drop event positions are relative to whichever widget the
        event was actually delivered to — remap to viewport coordinates
        only when that was the outer tree widget, matching
        ScenarioTablePanel._handle_drop's identical fix (2026-08-08, see
        NOTES.md)."""
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        if source_obj is self.tree:
            return self.tree.viewport().mapFrom(self.tree, pos)
        return pos

    def _deviation_item_at(self, event, source_obj, create=False):
        """Return the dev_id a drag position resolves to, or None.

        A literal DEV_T item resolves directly. Once a guide word has
        ANY equipment linked to it, "Lågt flöde" no longer renders as a
        plain DEV_T item — it becomes a LEDORD_T wrapper (or, for the
        single-equipment "kaka på kaka" merge, a CAUSE_T item) — so
        those must resolve too, or dropping a further/different object
        onto that same guide word silently does nothing (2026-08-13 bug
        report: "om det redan ligger ett objekt på 'lågt flöde' i
        trädet och jag drar ett nytt objekt dit så kan jag inte detta").

        `create=False` (the DragMove hover-feedback caller) only ever
        SELECTs — it must never write to the DB just because the mouse
        passed over a tree item. `create=True` (the actual Drop) may
        create the node's still-generic (equipment_id IS NULL) seeded
        deviation row for that guide word if none exists yet, via the
        same get_or_create_deviation() every other equipment-linking
        path already uses — so a SECOND/different object dropped on an
        already-equipped guide word lands on its own deviation row,
        never stealing the first object's."""
        pos = self._event_pos_in_viewport(event, source_obj)
        target = self.tree.itemAt(pos)
        if target is None:
            return None
        type_ = target.data(0, Qt.ItemDataRole.UserRole + 1)
        id_ = target.data(0, Qt.ItemDataRole.UserRole)
        if type_ == DEV_T:
            return id_
        if type_ == CAUSE_T:
            return self._resolve_deviation_id(CAUSE_T, id_)
        if type_ == LEDORD_T:
            try:
                node_id_str, description = str(id_).split(':', 1)
                node_id = int(node_id_str)
            except (ValueError, IndexError):
                return None
            if create:
                return self.db.get_or_create_deviation(node_id, description)
            row = self.db.conn.execute(
                "SELECT id FROM deviations WHERE node_id=? AND description=? ORDER BY id LIMIT 1",
                (node_id, description)).fetchone()
            return row[0] if row else None
        return None

    def _handle_equipment_drop(self, event, source_obj):
        text = event.mimeData().text()
        parts = text.split(':')
        if len(parts) < 3:
            event.ignore(); return
        ids_field = parts[2]
        try:
            marker_ids = [int(s) for s in ids_field.split(',') if s.strip()]
        except ValueError:
            event.ignore(); return
        if not marker_ids:
            event.ignore(); return

        dev_id = self._deviation_item_at(event, source_obj, create=True)
        if dev_id is None:
            event.ignore(); return

        self.equipment_dropped_on_deviation.emit(dev_id, marker_ids)
        event.acceptProposedAction()

    def _tree_reparent_target_at(self, event, source_obj):
        """Return (target_type, target_id) the drop position resolves to,
        or None if there's no tree item there. Actual compatibility
        (whether THIS source type can legally land there) is checked by
        the caller via _resolve_deviation_id/_resolve_cause_id/
        _resolve_consequence_id — same resolvers the tree's own Kopiera/
        Klistra in feature already uses, so drag-and-drop accepts exactly
        the same targets paste does."""
        pos = self._event_pos_in_viewport(event, source_obj)
        item = self.tree.itemAt(pos)
        if item is None:
            return None
        return (item.data(0, Qt.ItemDataRole.UserRole + 1),
                item.data(0, Qt.ItemDataRole.UserRole))

    def _handle_tree_reparent_drop(self, event, source_obj):
        text = event.mimeData().text()
        parts = text.split(':')   # ['hzp', 'treeitem', type, id]
        if len(parts) < 4:
            event.ignore(); return
        try:
            src_type = int(parts[2])
            src_id = int(parts[3])
        except ValueError:
            event.ignore(); return

        target = self._tree_reparent_target_at(event, source_obj)
        if target is None:
            event.ignore(); return
        tgt_type, tgt_id = target

        # Shift+drag copies (reuses the same copy_cause/copy_consequence/
        # copy_safeguard the right-click Kopiera/Klistra in feature already
        # uses); a plain drag moves. Checked against the LIVE keyboard
        # state rather than event.dropAction() — Qt's own drag-modifier ->
        # drop-action mapping is platform-specific (e.g. Windows maps
        # Ctrl to copy, not Shift), and the user asked specifically for
        # Shift here regardless of platform convention.
        is_copy = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)

        if src_type == CAUSE_T:
            dev_id = self._resolve_deviation_id(tgt_type, tgt_id)
            if dev_id is None:
                # Same NODE_T fallback _paste_item uses: dropping a cause
                # directly on a node lands it on that node's "Övrigt"
                # deviation (created on demand) rather than being rejected.
                node_id = self._resolve_node_id(tgt_type, tgt_id)
                if node_id is None:
                    event.ignore(); return
                dev_id = self.db.get_or_create_deviation(node_id)
            src_cause = self.db.get_cause(src_id)
            if not src_cause or dev_id == src_cause.get('deviation_id'):
                event.ignore(); return   # no-op: dropped back onto its own parent
            if is_copy:
                new_id = self.db.copy_cause(src_id, dev_id)
            else:
                self.db.move_cause_to_deviation(src_id, dev_id)
                new_id = src_id
            self.refresh(CAUSE_T, new_id)
            self.structure_changed.emit()
            event.acceptProposedAction()

        elif src_type == CONS_T:
            cause_id = self._resolve_cause_id(tgt_type, tgt_id)
            if cause_id is None:
                event.ignore(); return
            src_cons = self.db.get_consequence(src_id)
            if not src_cons or cause_id == src_cons.get('cause_id'):
                event.ignore(); return
            if is_copy:
                new_id = self.db.copy_consequence(src_id, cause_id)
            else:
                self.db.move_consequence(src_id, cause_id)
                new_id = src_id
            self.refresh(CONS_T, new_id)
            self.structure_changed.emit()
            event.acceptProposedAction()

        elif src_type == SG_T:
            cons_id = self._resolve_consequence_id(tgt_type, tgt_id)
            if cons_id is None:
                event.ignore(); return
            src_sg = self.db.get_safeguard(src_id)
            if not src_sg or cons_id == src_sg.get('consequence_id'):
                event.ignore(); return
            if is_copy:
                new_id = self.db.copy_safeguard(src_id, cons_id)
            else:
                self.db.move_safeguard(src_id, cons_id)
                new_id = src_id
            self.refresh(SG_T, new_id)
            self.structure_changed.emit()
            event.acceptProposedAction()
        else:
            event.ignore()

    def _add_cause_for_deviation(self, dev_id):
        self._open_cause_picker_for_deviation(dev_id)

    def _delete_item(self, type_, id_):
        if type_ == NODE_T:      self.db.delete_node(id_)
        elif type_ == DEV_T:     self.db.delete_deviation(id_)
        elif type_ == CAUSE_T:   self.db.delete_cause(id_)
        elif type_ == CONS_T:    self.db.delete_consequence(id_)
        elif type_ == SG_T:      self.db.delete_safeguard(id_)
        self.refresh(); self.structure_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO TABLE PANEL  (6-column bottom panel)
# ══════════════════════════════════════════════════════════════════════════════

_CAUSE_OBJ_W = 64   # width of the object-tag zone on the left of Orsak cells

# Icon size used in the obj-zone (square, left part of _CAUSE_OBJ_W)
_EQUIP_ICON_SZ = 20


def _icon_category(comp_type: str) -> str:
    """Map any object-type name (from the DB-backed standard_objects list,
    which is more granular than the drawing categories below) onto the
    fixed set of icon categories _draw_equip_icon knows how to draw.
    """
    if not comp_type:
        return ''
    t = comp_type.lower()
    if 'säkerhetsventil' in t or 'sprängbleck' in t:
        return 'Säkerhetsventil (PSV)'
    if 'ventil' in t:
        return 'Ventil'
    if 'pump' in t:
        return 'Pump'
    if 'kompressor' in t or 'fläkt' in t:
        return 'Kompressor'
    if 'tank' in t or 'kärl' in t or 'kolonn' in t:
        return 'Tank / Kärl'
    if 'värmeväxlare' in t or 'kylare' in t or 'värmare' in t:
        return 'Värmeväxlare'
    if 'rörledning' in t or 'slang' in t:
        return 'Rörledning'
    if 'instrument' in t or 'sensor' in t:
        return 'Instrument / Sensor'
    return ''


def _draw_equip_icon(painter, rect, comp_type):
    """Draw a colorful QPainter icon for the given equipment type.

    rect  -- the QRect to draw inside (icon is centred/fitted)
    comp_type -- a standard_objects name (or empty / unknown); mapped onto
    a drawing category via _icon_category() before matching below.
    """
    original_empty = not comp_type
    comp_type = _icon_category(comp_type) or comp_type
    sz    = min(rect.width(), rect.height()) - 4
    sz    = max(6, sz)
    cx    = float(rect.center().x())
    cy    = float(rect.center().y())
    half  = sz / 2.0

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if comp_type == 'Pump':
        # Blue filled circle with a white rotation arrow inside
        body = QColor('#2980b9')
        dark = QColor('#1a5276')
        painter.setBrush(QBrush(body))
        painter.setPen(QPen(dark, 1.2))
        painter.drawEllipse(QPointF(cx, cy), half, half)
        # White curved arrow (two small arcs simulated as a triangle near top-right)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
        # Impeller: three short radial lines
        pen_imp = QPen(QColor(255, 255, 255, 220), max(1.0, sz * 0.12))
        pen_imp.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_imp)
        for angle_deg in (0, 120, 240):
            angle = math.radians(angle_deg)
            r_in  = half * 0.25
            r_out = half * 0.65
            painter.drawLine(
                QPointF(cx + r_in  * math.cos(angle), cy + r_in  * math.sin(angle)),
                QPointF(cx + r_out * math.cos(angle), cy + r_out * math.sin(angle)),
            )

    elif comp_type == 'Ventil':
        # Orange bowtie / valve body
        col  = QColor('#e67e22')
        dark = QColor('#935116')
        half_v = half * 0.85
        pts = [
            QPointF(cx - half_v, cy - half_v),
            QPointF(cx + half_v, cy + half_v),
            QPointF(cx + half_v, cy - half_v),
            QPointF(cx - half_v, cy + half_v),
        ]
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawPolygon(QPolygonF(pts))
        # Stem line upward
        painter.setPen(QPen(dark, max(1.0, sz * 0.12)))
        painter.drawLine(QPointF(cx, cy - half_v), QPointF(cx, cy - half_v - half * 0.4))
        # Handwheel circle at top of stem
        painter.setPen(QPen(dark, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy - half_v - half * 0.4), half * 0.25, half * 0.25)

    elif comp_type == 'Kompressor':
        # Green diamond-ish rotary symbol
        col  = QColor('#27ae60')
        dark = QColor('#1a6b3c')
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawEllipse(QPointF(cx, cy), half * 0.9, half * 0.9)
        # Inner × marks
        pen_x = QPen(QColor(255, 255, 255, 200), max(1.0, sz * 0.14))
        pen_x.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_x)
        off = half * 0.5
        painter.drawLine(QPointF(cx - off, cy - off), QPointF(cx + off, cy + off))
        painter.drawLine(QPointF(cx + off, cy - off), QPointF(cx - off, cy + off))

    elif comp_type == 'Tank / Kärl':
        # Gray rounded rectangle (vessel)
        col  = QColor('#7f8c8d')
        dark = QColor('#2c3e50')
        rw   = half * 0.85
        rh   = half * 0.95
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawRoundedRect(
            QRectF(cx - rw, cy - rh, rw * 2, rh * 2), 3.0, 3.0)
        # Horizontal seam line
        painter.setPen(QPen(QColor(255, 255, 255, 150), 1.0))
        painter.drawLine(QPointF(cx - rw + 2, cy), QPointF(cx + rw - 2, cy))

    elif comp_type == 'Värmeväxlare':
        # Red/blue split rectangle with heat exchange arrows
        rw  = half * 0.85
        rh  = half * 0.8
        painter.setBrush(QBrush(QColor('#e74c3c')))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(QRectF(cx - rw, cy - rh, rw * 2, rh))
        painter.setBrush(QBrush(QColor('#2980b9')))
        painter.drawRect(QRectF(cx - rw, cy,      rw * 2, rh))
        painter.setPen(QPen(QColor('#2c3e50'), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(cx - rw, cy - rh, rw * 2, rh * 2))
        # Divider
        painter.setPen(QPen(QColor('#2c3e50'), 1.0))
        painter.drawLine(QPointF(cx - rw, cy), QPointF(cx + rw, cy))

    elif comp_type == 'Rörledning':
        # Teal horizontal pipe with end flanges
        col  = QColor('#16a085')
        dark = QColor('#0e6655')
        rh   = half * 0.28
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.0))
        painter.drawRect(QRectF(cx - half * 0.9, cy - rh, half * 1.8, rh * 2))
        # Flanges
        flange_w = max(1.5, sz * 0.1)
        for fx in (cx - half * 0.9, cx + half * 0.9):
            painter.drawLine(QPointF(fx, cy - rh * 1.8), QPointF(fx, cy + rh * 1.8))
        # Arrow head
        painter.setBrush(QBrush(QColor('#ffffff')))
        painter.setPen(Qt.PenStyle.NoPen)
        ax = cx + half * 0.3
        aw = half * 0.25
        ah = rh * 1.4
        arrow = QPolygonF([
            QPointF(ax,       cy),
            QPointF(ax - aw,  cy - ah),
            QPointF(ax - aw,  cy + ah),
        ])
        painter.drawPolygon(arrow)

    elif comp_type == 'Instrument / Sensor':
        # White circle with blue border (ISA instrument bubble) + letter I
        painter.setBrush(QBrush(QColor('#ffffff')))
        painter.setPen(QPen(QColor('#2471a3'), 1.8))
        painter.drawEllipse(QPointF(cx, cy), half * 0.85, half * 0.85)
        # Dashed line inside (indicates field-mounted)
        pen_d = QPen(QColor('#2471a3'), 1.0)
        pen_d.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen_d)
        painter.drawLine(QPointF(cx - half * 0.6, cy), QPointF(cx + half * 0.6, cy))

    elif comp_type == 'Säkerhetsventil (PSV)':
        # Purple filled diamond with upward spike
        col  = QColor('#8e44ad')
        dark = QColor('#6c3483')
        hd   = half * 0.75
        diamond = QPolygonF([
            QPointF(cx,       cy - hd),
            QPointF(cx + hd,  cy),
            QPointF(cx,       cy + hd),
            QPointF(cx - hd,  cy),
        ])
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawPolygon(diamond)
        # Discharge spike upward
        painter.setPen(QPen(dark, max(1.5, sz * 0.13)))
        painter.drawLine(QPointF(cx, cy - hd), QPointF(cx, cy - hd - half * 0.45))
        # Small horizontal discharge bar at top
        painter.drawLine(QPointF(cx - half * 0.3, cy - hd - half * 0.45),
                         QPointF(cx + half * 0.3, cy - hd - half * 0.45))

    else:
        # Generic: gray circle with '?' — Övrigt or unknown
        painter.setBrush(QBrush(QColor('#bdc3c7')))
        painter.setPen(QPen(QColor('#7f8c8d'), 1.2))
        painter.drawEllipse(QPointF(cx, cy), half * 0.85, half * 0.85)
        if original_empty:
            # '+' — not yet set
            pen_plus = QPen(QColor('#7f8c8d'), max(1.2, sz * 0.13))
            pen_plus.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen_plus)
            painter.drawLine(QPointF(cx - half * 0.45, cy), QPointF(cx + half * 0.45, cy))
            painter.drawLine(QPointF(cx, cy - half * 0.45), QPointF(cx, cy + half * 0.45))
        else:
            # '?' mark
            f = QFont()
            f.setPointSize(max(5, int(sz * 0.45)))
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor('#555'))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, '?')

    painter.restore()


class StandardCausesPickerPopup(QDialog):
    """Orsaksväljare: Avvikelse (header) → Objekt (vänster) → Orsaker (höger).

    Visas vid P&ID-klick och från knappen 'Orsak' ovanför P&ID-vyn.
    Innehåller sökfält, tag-ID-fält, frekvensvisning och fritext-fallback.
    """
    cause_picked = pyqtSignal(str, object)   # (description, frequency_or_None)

    _OBJ_BTN_STYLE = (
        "QPushButton { text-align:left; padding:2px 6px; border:1px solid #E2E3E1;"
        " border-radius:3px; background:#FAFAFA; font-size:10px; }"
        "QPushButton:hover { background:#F5F5F3; border-color:#CFD1CE; }"
        "QPushButton:checked { background:#2F5FD0; color:white; border-color:#2F5FD0;"
        " font-weight:bold; }")

    def __init__(self, db, deviation_id: int, deviation_name: str = '',
                 comp_type: str = '', initial_tag: str = '',
                 node_id: int = None, parent=None):
        super().__init__(parent)
        self._db = db
        self._dev_id = deviation_id   # standard_deviations.id
        self.selected_node_dev_id = None  # deviations.id to use in add_cause()
        self._dev_items = []   # [(deviations.id, desc, standard_deviations.id)]
        self._dev_combo = None

        # Resolve selected_node_dev_id from node's deviations
        if node_id is not None:
            for d in db.deviations(node_id):
                std_row = db.conn.execute(
                    "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
                    (d['description'],)).fetchone()
                self._dev_items.append((d['id'], d['description'],
                                        std_row[0] if std_row else None))
            # Pre-select by deviation_name
            match = next((item for item in self._dev_items
                          if item[1] == deviation_name), None)
            if match:
                self.selected_node_dev_id = match[0]
                self._dev_id = match[2]
            elif self._dev_items:
                self.selected_node_dev_id = self._dev_items[0][0]
                self._dev_id = self._dev_items[0][2]
                deviation_name = self._dev_items[0][1]

        self.setWindowTitle("Lägg till orsak")
        self.setMinimumWidth(640)
        self.setMinimumHeight(CONFIG['H_PANEL_MIN_XL'])
        main = QVBoxLayout(self)
        main.setSpacing(0)
        main.setContentsMargins(0, 0, 0, 0)

        # ── Coloured header band ──────────────────────────────────────────────
        hdr_w = QWidget()
        hdr_w.setStyleSheet("background:#2F5FD0;")
        hdr_l = QVBoxLayout(hdr_w)
        hdr_l.setContentsMargins(12, 8, 12, 8)
        hdr_l.setSpacing(3)
        title = QLabel("Lägg till orsak på P&ID")
        title.setStyleSheet("color:white; font-size:13px; font-weight:bold;")
        hdr_l.addWidget(title)
        sub = QLabel(f"Avvikelse: {deviation_name}")
        sub.setStyleSheet("color:#D7E3FA; font-size:10px;")
        hdr_l.addWidget(sub)
        main.addWidget(hdr_w)

        body = QWidget()
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(10, 10, 10, 10)
        body_l.setSpacing(8)
        main.addWidget(body, 1)

        # ── Deviation picker (shown when node has multiple deviations) ─────────
        if len(self._dev_items) > 1:
            dev_row = QHBoxLayout()
            dev_row.setSpacing(6)
            dev_lbl = QLabel("Avvikelse:")
            dev_lbl.setStyleSheet("font-size:10px; color:#555; font-weight:bold;")
            dev_row.addWidget(dev_lbl)
            self._dev_combo = QComboBox()
            for _, desc, _ in self._dev_items:
                self._dev_combo.addItem(desc)
            cur_idx = next((i for i, (nid, _, _) in enumerate(self._dev_items)
                            if nid == self.selected_node_dev_id), 0)
            self._dev_combo.setCurrentIndex(cur_idx)
            self._dev_combo.currentIndexChanged.connect(self._on_dev_combo_changed)
            dev_row.addWidget(self._dev_combo, 1)
            body_l.addLayout(dev_row)

        # ── Tag + search row ──────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        tag_lbl = QLabel("Tag-ID:")
        tag_lbl.setStyleSheet("font-size:10px; color:#555;")
        top_row.addWidget(tag_lbl)
        self._tag_edit = QLineEdit(initial_tag)
        self._tag_edit.setPlaceholderText("t.ex. P-101")
        self._tag_edit.setMaximumWidth(110)
        # Autocomplete from equipment catalog
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        self._tag_edit.textChanged.connect(self._on_tag_id_changed)
        self._tag_edit.textChanged.connect(self._update_tag_style)
        self._update_tag_style(initial_tag)
        top_row.addWidget(self._tag_edit)

        top_row.addSpacing(8)
        search_lbl = QLabel("Sök:")
        search_lbl.setStyleSheet("font-size:10px; color:#555;")
        top_row.addWidget(search_lbl)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filtrera orsaker…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search)
        top_row.addWidget(self._search_edit, 1)
        body_l.addLayout(top_row)

        # ── Main split: objects (left) + causes (right) ───────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: object buttons (scrollable)
        obj_frame = QFrame()
        obj_frame.setFrameShape(QFrame.Shape.StyledPanel)
        obj_frame.setStyleSheet(
            "QFrame { border:1px solid #e5e7eb; border-radius:6px; background:#f9fafb; }")
        obj_fl = QVBoxLayout(obj_frame)
        obj_fl.setContentsMargins(4, 4, 4, 4)
        obj_fl.setSpacing(2)
        obj_hdr_row = QHBoxLayout()
        obj_hdr = QLabel("<b>Objekttyp</b>")
        obj_hdr.setStyleSheet("font-size:10px; color:#8D9299; padding:2px 4px;")
        obj_hdr_row.addWidget(obj_hdr)
        obj_hdr_row.addStretch()
        btn_add_obj = QPushButton("+ Ny")
        btn_add_obj.setFixedHeight(CONFIG['H_BADGE'])
        btn_add_obj.setStyleSheet(
            "QPushButton{font-size:9px;padding:1px 5px;border:1px solid #CFD1CE;"
            "border-radius:3px;background:#F5F5F3;color:#17191C;}"
            "QPushButton:hover{background:#E8E9E6;}")
        btn_add_obj.setToolTip("Lägg till ny objekttyp (t.ex. Transportör)")
        btn_add_obj.clicked.connect(self._add_object_type)
        obj_hdr_row.addWidget(btn_add_obj)
        obj_fl.addLayout(obj_hdr_row)
        obj_scroll = QScrollArea()
        obj_scroll.setWidgetResizable(True)
        obj_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._obj_inner = QWidget()
        self._obj_inner_l = QVBoxLayout(self._obj_inner)
        self._obj_inner_l.setContentsMargins(0, 0, 0, 0)
        self._obj_inner_l.setSpacing(1)
        obj_scroll.setWidget(self._obj_inner)
        obj_fl.addWidget(obj_scroll)
        self._obj_btn_group = []   # list of QPushButton
        splitter.addWidget(obj_frame)

        # Right: cause list
        cause_frame = QFrame()
        cause_frame.setFrameShape(QFrame.Shape.StyledPanel)
        cause_frame.setStyleSheet(
            "QFrame { border:1px solid #e5e7eb; border-radius:6px; background:white; }")
        cause_fl = QVBoxLayout(cause_frame)
        cause_fl.setContentsMargins(4, 4, 4, 4)
        cause_fl.setSpacing(2)
        self._cause_hdr = QLabel("<b>Orsaker</b>")
        self._cause_hdr.setStyleSheet("font-size:10px; color:#8D9299; padding:2px 4px;")
        cause_fl.addWidget(self._cause_hdr)
        self._cause_list = QListWidget()
        self._cause_list.setAlternatingRowColors(False)
        self._cause_list.setWordWrap(True)
        self._cause_list.setSpacing(1)
        self._cause_list.setStyleSheet(
            "QListWidget { border:none; }"
            "QListWidget::item { padding:6px 10px; border-bottom:1px solid #f3f4f6; }"
            "QListWidget::item:selected { background:#E6ECFA; color:#17191C;"
            "  border-left:3px solid #2F5FD0; font-weight:bold; }"
            "QListWidget::item:hover:!selected { background:#F5F5F3; }")
        cause_fl.addWidget(self._cause_list)
        splitter.addWidget(cause_frame)
        splitter.setSizes([200, 420])
        body_l.addWidget(splitter, 1)

        # ── Free-text + bottom row ────────────────────────────────────────────
        ft_frame = QFrame()
        ft_frame.setStyleSheet(
            "QFrame { background:#f8fafc; border:1px solid #e2e8f0; border-radius:4px; }")
        ft_l = QHBoxLayout(ft_frame)
        ft_l.setContentsMargins(8, 4, 8, 4)
        ft_l.setSpacing(6)
        ft_lbl = QLabel("Fritext:")
        ft_lbl.setStyleSheet("font-size:10px; color:#64748b;")
        ft_l.addWidget(ft_lbl)
        self._ft_edit = QLineEdit()
        self._ft_edit.setPlaceholderText("Ange orsak manuellt om inget standardalternativ passar…")
        self._ft_edit.setStyleSheet("border:none; background:transparent;")
        ft_l.addWidget(self._ft_edit, 1)
        body_l.addWidget(ft_frame)

        # Buttons
        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("Välj orsak")
        self._ok_btn.setIcon(_icon('check', 16, '#ffffff'))
        self._ok_btn.setDefault(True)
        self._ok_btn.setMinimumHeight(CONFIG['H_BTN_OK'])
        self._ok_btn.setStyleSheet(
            "QPushButton { background:#2F5FD0; color:white; border:none;"
            " border-radius:5px; padding:6px 20px; font-weight:bold; font-size:11px; }"
            "QPushButton:hover { background:#3D6BD8; }"
            "QPushButton:pressed { background:#254B9E; }")
        self._ok_btn.clicked.connect(self._pick_selected)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.setMinimumHeight(CONFIG['H_BTN_OK'])
        cancel_btn.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#17191C; border:1px solid #E2E3E1;"
            " border-radius:5px; padding:6px 16px; }"
            "QPushButton:hover { background:#F5F5F3; }")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._ok_btn)
        body_l.addLayout(btn_row)

        self._cause_list.itemDoubleClicked.connect(self._pick_selected)
        self._ft_edit.returnPressed.connect(self._pick_selected)
        self._search_edit.installEventFilter(self)

        # Derive preselect from tag first (most reliable), fall back to comp_type
        preselect = _lookup_comp_type_for_tag(initial_tag, db) if initial_tag else ''
        if not preselect:
            preselect = comp_type
        self._initial_comp_type = preselect
        self._populate_objects(preselect)

    # ── Object buttons ────────────────────────────────────────────────────────
    def _on_tag_id_changed(self, text: str):
        """Tag-ID field changed — look up type from smart recognition and pre-select."""
        if not text.strip():
            return
        comp = _lookup_comp_type_for_tag(text.strip(), self._db)
        if comp:
            self._initial_comp_type = comp
            self._select_obj_by_type(comp)

    def _update_tag_style(self, text: str):
        """Highlight tag field orange when empty — tag is required for smart learning."""
        if text.strip():
            self._tag_edit.setStyleSheet('')
            self._tag_edit.setToolTip('')
        else:
            self._tag_edit.setStyleSheet(
                'QLineEdit { background:#fff7ed; border:1px solid #f97316; border-radius:3px; }')
            self._tag_edit.setToolTip(
                'Fyll i Tag-ID för att programmet ska lära sig objekttypen.\n'
                'Utan tag lagras ingen information i Smart igenkänning.')

    def _select_obj_by_type(self, comp_type: str):
        """Check the object button that matches comp_type (case-insensitive substring).
        If the matching button doesn't exist yet (filtered out by deviation),
        repopulates the list with all objects so the button is present.
        """
        if not comp_type:
            return
        # Try existing buttons first
        for btn in self._obj_btn_group:
            if _obj_type_matches(comp_type, btn.property('obj_name') or ''):
                if not btn.isChecked():
                    for b in self._obj_btn_group:
                        b.setChecked(False)
                    btn.setChecked(True)
                    self._load_causes_for_obj(
                        btn.property('obj_id'), btn.property('obj_name'))
                return
        # Button not found — expand the list to all objects and try again
        self._populate_objects(comp_type)

    def _on_dev_combo_changed(self, idx):
        """Deviation combo changed — update selected IDs and reload object list."""
        if 0 <= idx < len(self._dev_items):
            self.selected_node_dev_id = self._dev_items[idx][0]
            self._dev_id              = self._dev_items[idx][2]
        self._populate_objects(getattr(self, '_initial_comp_type', ''))

    def _populate_objects(self, preselect_comp: str = ''):
        # Remove old buttons
        for btn in self._obj_btn_group:
            btn.setParent(None)
        self._obj_btn_group.clear()

        if self._dev_id is None:
            objs = self._db.standard_objects()
        else:
            objs = self._db.objects_for_deviation(self._dev_id)

        if not objs:
            objs = self._db.standard_objects()

        # If a preselect type is requested but absent from the filtered list,
        # fall back to all objects so the button actually exists.
        if preselect_comp:
            if not any(_obj_type_matches(preselect_comp, o['name']) for o in objs):
                objs = self._db.standard_objects()

        def _make_btn(obj):
            btn = QPushButton(obj['name'])
            btn.setCheckable(True)
            btn.setStyleSheet(self._OBJ_BTN_STYLE)
            btn.setProperty('obj_id',   obj['id'])
            btn.setProperty('obj_name', obj['name'])
            btn.clicked.connect(partial(self._on_obj_btn, btn))
            return btn

        sel_btn = None
        for obj in objs:
            btn = _make_btn(obj)
            self._obj_inner_l.addWidget(btn)
            self._obj_btn_group.append(btn)
            if preselect_comp and sel_btn is None and _obj_type_matches(preselect_comp, obj['name']):
                sel_btn = btn
        self._obj_inner_l.addStretch()

        # Only pre-select if smart recognition produced a match.
        # If nothing was learned for this prefix, leave all buttons unchecked
        # so the user clearly sees there is no suggestion — no random default.
        if sel_btn:
            sel_btn.setChecked(True)
            self._load_causes_for_obj(sel_btn.property('obj_id'), sel_btn.property('obj_name'))

    def _on_obj_btn(self, clicked_btn):
        for btn in self._obj_btn_group:
            btn.setChecked(btn is clicked_btn)
        self._search_edit.clear()
        self._load_causes_for_obj(clicked_btn.property('obj_id'),
                                   clicked_btn.property('obj_name'))

    def _add_object_type(self):
        """Add a new standard object type (e.g. Transportör) to the database."""
        name, ok = QInputDialog.getText(
            self, "Ny objekttyp",
            "Ange namn på den nya objekttypen\n(t.ex. Transportör, Kross, Silovinagg):")
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            # Check if already exists
            existing = self._db.conn.execute(
                "SELECT id FROM standard_objects WHERE LOWER(name)=LOWER(?)",
                (name,)).fetchone()
            if existing:
                new_id = existing[0]
            else:
                new_id = self._db.add_standard_object(name)
            self._db.commit()
        except Exception as e:
            QMessageBox.warning(self, "Fel", f"Kunde inte lägga till objekttyp:\n{e}")
            return
        # Re-populate to include the new type and select it
        self._populate_objects(name)

    def _load_causes_for_obj(self, obj_id, obj_name):
        self._cause_list.clear()
        self._cause_hdr.setText(f"<b>Orsaker</b> — {obj_name}")
        filt = self._search_edit.text().strip().lower()
        # When no standard deviation is matched, show ALL causes for this object
        if self._dev_id is None:
            rows = self._db.conn.execute(
                "SELECT sc.id, sc.description, sc.frequency "
                "FROM standard_causes sc "
                "WHERE sc.object_id=? ORDER BY sc.deviation_id, sc.sort_order",
                (obj_id,)).fetchall()
            causes = [dict(r) for r in rows]
        else:
            causes = self._db.standard_causes_for_object(self._dev_id, obj_id)
        for c in causes:
            freq  = c.get('frequency')
            label = c['description']
            if filt and filt not in label.lower():
                continue
            # Show frequency visibly in the list item text
            if freq is not None:
                f_level = freq_to_f_level(freq) if freq else None
                freq_lbl = freq_axis_label(f_level) if f_level is not None else ''
                display = f"{label}  [{freq:g}/år{(' · ' + freq_lbl) if freq_lbl else ''}]"
            else:
                display = label
            ci = QListWidgetItem(display)
            ci.setData(Qt.ItemDataRole.UserRole + 1, c['description'])
            ci.setData(Qt.ItemDataRole.UserRole + 2, freq)
            if freq is not None:
                ci.setForeground(QColor('#17191C'))
            self._cause_list.addItem(ci)
        if self._cause_list.count():
            self._cause_list.setCurrentRow(0)

    # ── Search ────────────────────────────────────────────────────────────────
    def _on_search(self, text):
        filt = text.strip().lower()
        if not filt:
            # Restore current object
            for btn in self._obj_btn_group:
                if btn.isChecked():
                    self._load_causes_for_obj(btn.property('obj_id'), btn.property('obj_name'))
                    return
            return
        self._cause_list.clear()
        self._cause_hdr.setText("<b>Sökresultat</b>")
        for obj in self._db.objects_for_deviation(self._dev_id):
            for c in self._db.standard_causes_for_object(self._dev_id, obj['id']):
                if filt in c['description'].lower():
                    freq  = c.get('frequency')
                    ci = QListWidgetItem(f"{c['description']}  ·  {obj['name']}")
                    ci.setData(Qt.ItemDataRole.UserRole + 1, c['description'])
                    ci.setData(Qt.ItemDataRole.UserRole + 2, freq)
                    ci.setForeground(QColor('#374151'))
                    self._cause_list.addItem(ci)
        if self._cause_list.count():
            self._cause_list.setCurrentRow(0)

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.KeyPress:
            if obj is self._search_edit:
                if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._cause_list.setFocus()
                    if self._cause_list.count():
                        self._cause_list.setCurrentRow(0)
                    if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                            and self._cause_list.currentItem():
                        self._pick_selected()
                    return True
            if obj is self._cause_list:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._pick_selected()
                    return True
        return super().eventFilter(obj, event)

    # ── Pick ──────────────────────────────────────────────────────────────────
    def _pick_selected(self):
        try:
            ft = self._ft_edit.text().strip()
            if ft:
                # Ask if the user wants to save this as a standard cause
                self._maybe_save_as_standard(ft)
                self.cause_picked.emit(ft, None)
                self.accept(); return
            cause_item = self._cause_list.currentItem()
            if not cause_item: return
            desc = cause_item.data(Qt.ItemDataRole.UserRole + 1) or cause_item.text()
            freq = cause_item.data(Qt.ItemDataRole.UserRole + 2)
            self.cause_picked.emit(desc, freq)
            self.accept()
        except Exception as e:
            import traceback
            QMessageBox.critical(self, "Fel i orsakspickern",
                                 f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")

    def _maybe_save_as_standard(self, description: str):
        """Ask if the free-text cause should be saved as a standard cause with frequency."""
        checked = next((b for b in self._obj_btn_group if b.isChecked()), None)
        obj_id   = checked.property('obj_id')   if checked else None
        obj_name = checked.property('obj_name') if checked else ''
        _maybe_save_as_standard_cause(self, self._db, self._dev_id, obj_id, obj_name, description)


class CauseObjectPopup(QDialog):
    """Combined popup: set Tag-ID + equipment type, then pick a standard cause."""
    committed = pyqtSignal(str, str, str, object)  # (comp_type, comp_tag, description, freq|None)

    def __init__(self, comp_type: str, comp_tag: str, db,
                 dev_description=None, current_description='',
                 node_id=None, deviation_id=None, parent=None):
        super().__init__(parent)
        self._db              = db
        self._dev_description = dev_description
        self._deviation_id    = deviation_id   # preferred: used for new hierarchy lookup
        self._dev_combo       = None
        self._cause_buttons   = []   # list of (QRadioButton, description, freq)
        self._freq_overrides  = {}   # QRadioButton → custom freq (overrides standard)

        self.setWindowTitle("Objekt / Standardorsak")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumWidth(CONFIG['W_PANEL_MIN'])
        self.setMaximumWidth(340)

        _small = "font-size:10px;"
        _btn_style = ("QPushButton{font-size:10px; padding:2px 10px;"
                      "border:1px solid #E2E3E1; border-radius:3px; background:#FFFFFF;}"
                      "QPushButton:hover{background:#F5F5F3;}"
                      "QPushButton:default{background:#2F5FD0; color:white; border-color:#2F5FD0;}"
                      "QPushButton:default:hover{background:#3D6BD8;}")

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Header: icon + title ──────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(22, 22)
        hdr.addWidget(self._icon_lbl)
        title = QLabel("<b>Orsak på P&amp;ID</b>")
        title.setStyleSheet("font-size:11px; color:#8D9299;")
        hdr.addWidget(title)
        hdr.addStretch()
        layout.addLayout(hdr)

        # ── Form: (Avvikelse) + Tag-ID + Type ────────────────────────────────
        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Optional deviation picker — only shown when node_id is supplied
        if node_id is not None and db:
            self._dev_combo = QComboBox()
            self._dev_combo.setFixedHeight(22)
            self._dev_combo.setStyleSheet(_small)
            self._dev_combo.setMaxVisibleItems(12)
            try:
                devs = db.deviations(node_id)
            except Exception:
                devs = []
            for d in devs:
                self._dev_combo.addItem(d['description'][:70], d['id'])
            if deviation_id:
                for i in range(self._dev_combo.count()):
                    if self._dev_combo.itemData(i) == deviation_id:
                        self._dev_combo.setCurrentIndex(i)
                        break
            dev_lbl = QLabel("Avvikelse:")
            dev_lbl.setStyleSheet(_small)
            form.addRow(dev_lbl, self._dev_combo)
            # Keep _dev_description in sync and refresh cause list on change
            if self._dev_combo.count() > 0:
                self._dev_description = self._dev_combo.currentText()
            def _on_dev_changed():
                self._dev_description = self._dev_combo.currentText() or None
                self._populate_type_combo(self._type_cb.currentText())
                self._rebuild_causes(self._type_cb.currentText())
            self._dev_combo.currentIndexChanged.connect(_on_dev_changed)

        self._tag_edit = QLineEdit(comp_tag)
        self._tag_edit.setPlaceholderText("t.ex. P-101")
        self._tag_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._tag_edit.setStyleSheet(_small)
        if db:
            completer = _make_tag_completer(db, self)
            if completer:
                self._tag_edit.setCompleter(completer)
        tag_lbl = QLabel("Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._type_cb.setStyleSheet(_small)
        self._populate_type_combo(comp_type)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, self._type_cb)
        layout.addLayout(form)

        # ── Thin separator ────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#e0e0e0; margin:0px;")
        sep.setFixedHeight(CONFIG['H_SEP_LINE'])
        layout.addWidget(sep)

        # ── Standard causes section ───────────────────────────────────────────
        self._causes_header = QLabel()
        self._causes_header.setStyleSheet("color:#777; font-size:9px;")
        layout.addWidget(self._causes_header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setMaximumHeight(150)
        layout.addWidget(self._scroll)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)

        # Freetext radio — always the last option
        self._freetext_radio = QRadioButton("Fritext:")
        self._freetext_radio.setStyleSheet(_small)
        self._freetext_edit  = QLineEdit(current_description)
        self._freetext_edit.setPlaceholderText("Beskriv orsaken…")
        self._freetext_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._freetext_edit.setStyleSheet(_small)
        self._freetext_radio.toggled.connect(
            lambda on: self._freetext_edit.setEnabled(on))

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setSpacing(4)
        ok  = QPushButton("OK")
        ok.setDefault(True)
        ok.setFixedHeight(CONFIG['H_CTRL_STD'])
        ok.setStyleSheet(_btn_style)
        ok.clicked.connect(self._ok)
        clr = QPushButton("Rensa")
        clr.setFixedHeight(CONFIG['H_CTRL_STD'])
        clr.setStyleSheet(_btn_style)
        clr.clicked.connect(self._clear)
        cancel = QPushButton("Avbryt")
        cancel.setFixedHeight(CONFIG['H_CTRL_STD'])
        cancel.setStyleSheet(_btn_style)
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addStretch()
        btns.addWidget(clr)
        btns.addWidget(cancel)
        layout.addLayout(btns)

        # ── Wire signals ──────────────────────────────────────────────────────
        self._type_cb.currentTextChanged.connect(self._rebuild_causes)
        self._tag_edit.textChanged.connect(self._on_tag_changed)
        self._tag_edit.returnPressed.connect(self._ok)
        if comp_tag:
            self._on_tag_changed(comp_tag)

        # Build initial causes list (triggers icon update too)
        self._rebuild_causes(self._type_cb.currentText(), pre_select=current_description)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _populate_type_combo(self, preselect_comp: str = ''):
        """Populate the type combo from the same DB-backed standard_objects
        list StandardCausesPickerPopup uses, instead of a hardcoded list —
        so both dialogs offer the same object-type vocabulary.
        """
        self._type_cb.blockSignals(True)
        self._type_cb.clear()
        self._type_cb.addItem('')
        objs = []
        if self._db is not None:
            try:
                dev_id = self._deviation_id
                if dev_id is None and self._dev_description:
                    r = self._db.conn.execute(
                        "SELECT id FROM standard_deviations WHERE description=? LIMIT 1",
                        (self._dev_description,)).fetchone()
                    if r:
                        dev_id = r[0]
                objs = self._db.objects_for_deviation(dev_id) if dev_id is not None else []
                if not objs:
                    objs = self._db.standard_objects()
            except Exception:
                objs = []
        for o in objs:
            self._type_cb.addItem(o['name'])

        idx = -1
        if preselect_comp:
            for i in range(self._type_cb.count()):
                if _obj_type_matches(preselect_comp, self._type_cb.itemText(i)):
                    idx = i
                    break
            if idx < 0:
                # Not in the standard list (e.g. legacy free-typed value) —
                # keep it selectable rather than silently discarding it.
                self._type_cb.addItem(preselect_comp)
                idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(max(0, idx))
        self._type_cb.blockSignals(False)

    def _resolve_dev_obj_ids(self, comp_type):
        """Resolve (deviation_id, object_id) for the given comp_type string
        against the DB-backed standard_deviations/standard_objects tables.
        Caches the resolved deviation id on self._deviation_id.
        """
        dev_id = self._deviation_id
        if dev_id is None and self._dev_description and self._db is not None:
            r = self._db.conn.execute(
                "SELECT id FROM standard_deviations WHERE description=? LIMIT 1",
                (self._dev_description,)).fetchone()
            if r:
                dev_id = r[0]
                self._deviation_id = dev_id
        obj_id = None
        if comp_type and self._db is not None:
            for o in self._db.standard_objects():
                if _obj_type_matches(comp_type, o['name']):
                    obj_id = o['id']
                    break
        return dev_id, obj_id

    def _update_icon(self, comp_type):
        px = QPixmap(22, 22)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        _draw_equip_icon(p, QRect(0, 0, 22, 22), comp_type)
        p.end()
        self._icon_lbl.setPixmap(px)

    def _on_tag_changed(self, text):
        if not self._db or not text.strip():
            return
        detected = _lookup_comp_type_for_tag(text.strip(), self._db)
        if detected:
            idx = next((i for i in range(self._type_cb.count())
                        if _obj_type_matches(detected, self._type_cb.itemText(i))), -1)
            if idx >= 0:
                self._type_cb.setCurrentIndex(idx)
                self._rebuild_causes(self._type_cb.itemText(idx))
            else:
                # Not in the current list (e.g. filtered by deviation) —
                # repopulate so the learned type is selectable.
                self._populate_type_combo(detected)
                self._rebuild_causes(self._type_cb.currentText())

    def _rebuild_causes(self, comp_type, pre_select=''):
        self._update_icon(comp_type)

        # Clear old buttons from group
        for btn, _, _ in self._cause_buttons:
            self._btn_group.removeButton(btn)
        self._cause_buttons.clear()
        self._freq_overrides.clear()
        if self._freetext_radio in self._btn_group.buttons():
            self._btn_group.removeButton(self._freetext_radio)

        # Query causes: prefer new hierarchy (deviation + object), fall back to comp_type
        rows = []
        if comp_type and self._db is not None:
            dev_id, obj_id = self._resolve_dev_obj_ids(comp_type)
            if dev_id is not None and obj_id is not None:
                rows = self._db.standard_causes_for_object(dev_id, obj_id)
            if not rows:
                rows = self._db.standard_causes_for_comp_type(comp_type, self._dev_description)
            if not rows:
                rows = self._db.standard_causes_for_comp_type(comp_type)

        _rs = "font-size:10px;"

        inner = QWidget()
        vbox  = QVBoxLayout(inner)
        vbox.setSpacing(1)
        vbox.setContentsMargins(2, 1, 2, 1)

        to_check = None   # radio to pre-select

        for r in rows:
            r = dict(r)
            freq  = r.get('frequency')
            desc  = r['description']
            radio = QRadioButton(desc)
            radio.setStyleSheet(_rs)
            self._btn_group.addButton(radio)
            self._cause_buttons.append((radio, desc, freq))

            row_w = QWidget()
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(4)
            row_h.addWidget(radio, stretch=1)

            if freq is not None:
                freq_str = f"{freq:.3g} /år" if freq >= 0.01 else f"{freq:.2e} /år"
                fb = QPushButton(freq_str)
                fb.setFixedHeight(CONFIG['H_BADGE'])
                fb.setStyleSheet(
                    "QPushButton{color:#17191C; background:#F5F5F3; border-radius:3px;"
                    "padding:1px 5px; font-size:10px; font-weight:bold; border:none;}"
                    "QPushButton:hover{background:#E8E9E6;}")
                fb.setToolTip("Klicka för att ange anpassad frekvens")
                # capture radio + fb in closure
                def _make_freq_handler(r=radio, btn=fb, base=freq):
                    def _handler():
                        cur = self._freq_overrides.get(r, base)
                        val, ok = QInputDialog.getDouble(
                            self, "Anpassad frekvens",
                            "Frekvens (händelser/år):",
                            cur, 0.0, 1e6, 6)
                        if ok:
                            self._freq_overrides[r] = val
                            label = f"{val:.3g} /år" if val >= 0.01 else f"{val:.2e} /år"
                            btn.setText(label)
                            btn.setStyleSheet(
                                "QPushButton{color:#7B2D00; background:#fde8cc;"
                                "border-radius:3px; padding:1px 5px;"
                                "font-size:10px; font-weight:bold; border:none;}"
                                "QPushButton:hover{background:#fbd4a0;}")
                    return _handler
                fb.clicked.connect(_make_freq_handler())
                row_h.addWidget(fb)

            vbox.addWidget(row_w)

            if pre_select and desc == pre_select:
                to_check = radio

        # Freetext option (always last)
        ft_row = QWidget()
        ft_h   = QHBoxLayout(ft_row)
        ft_h.setContentsMargins(0, 0, 0, 0)
        ft_h.setSpacing(6)
        ft_h.addWidget(self._freetext_radio)
        ft_h.addWidget(self._freetext_edit, stretch=1)
        vbox.addWidget(ft_row)
        self._btn_group.addButton(self._freetext_radio)

        vbox.addStretch()
        self._scroll.setWidget(inner)

        # Pre-select
        if to_check:
            to_check.setChecked(True)
            self._freetext_edit.setEnabled(False)
        else:
            self._freetext_radio.setChecked(True)
            self._freetext_edit.setEnabled(True)

        # Update header text
        has_std = bool(rows)
        if has_std and self._dev_description:
            self._causes_header.setText(
                f"Standardorsaker  —  {comp_type}  /  {self._dev_description}")
        elif has_std:
            self._causes_header.setText(f"Standardorsaker  —  {comp_type}")
        else:
            self._causes_header.setText("Ingen standardorsak — ange fritext")
        self._causes_header.setVisible(True)

    def _ok(self):
        comp_type = self._type_cb.currentText()
        comp_tag  = self._tag_edit.text().strip()

        desc, freq = '', None
        if self._freetext_radio.isChecked():
            desc = self._freetext_edit.text().strip()
            if desc:
                dev_id, obj_id = self._resolve_dev_obj_ids(comp_type)
                _maybe_save_as_standard_cause(self, self._db, dev_id, obj_id, comp_type, desc)
        else:
            for radio, d, f in self._cause_buttons:
                if radio.isChecked():
                    desc = d
                    freq = self._freq_overrides.get(radio, f)
                    break

        self.committed.emit(comp_type, comp_tag, desc, freq)
        self.accept()

    @property
    def selected_deviation_id(self):
        if self._dev_combo is not None:
            return self._dev_combo.currentData()
        return None

    def _clear(self):
        self.committed.emit('', '', '', None)
        self.accept()


class CauseTagPopup(QDialog):
    """Minimalistic popup for editing a tag + type — just those two
    fields, nothing else (2026-08-14, see NOTES.md: "klickar man på
    tagen justerar man tagen ... gör samtliga minimalistiska").
    CauseObjectPopup (above) still has the full avvikelse-context +
    standard-cause picker, unchanged, for its other two entry points
    (the detail panel and quick-add) — this only replaces what a bare
    tag click (scenario table) or a tag double-click (tree, see
    TreePanel._open_equipment_tag_popup) opens. Modelled on
    EquipmentTagPopup's compact style.

    No OK/Avbryt buttons (2026-08-18, user request: "Du kan ta bort
    dialogrutorna ok och avbryt och låta mig trycka och tillåta
    redigering utan bekräftande knapptryck") — each field commits the
    moment it changes (Enter/focus-out on the tag field, selecting a
    type), and the popup is a self-dismissing `Qt.WindowType.Popup`
    (closes on Escape or an outside click, same mechanism as
    EquipmentDeviationBar) rather than a modal dialog requiring an
    explicit confirm."""
    committed = pyqtSignal(str, str)  # (comp_type, comp_tag)

    def __init__(self, db, comp_type='', comp_tag='', parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Tagg")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setMinimumWidth(220)

        _small = "font-size:10px;"
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._tag_edit = QLineEdit(comp_tag)
        self._tag_edit.setPlaceholderText("t.ex. PV-101")
        self._tag_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._tag_edit.setStyleSheet(_small)
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        tag_lbl = QLabel("Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._type_cb.setStyleSheet(_small)
        self._type_cb.addItems(_equipment_type_options(db))
        if comp_type:
            idx = self._type_cb.findText(comp_type)
            if idx < 0:
                self._type_cb.addItem(comp_type)
                idx = self._type_cb.count() - 1
            self._type_cb.setCurrentIndex(idx)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, self._type_cb)
        layout.addLayout(form)

        self._tag_edit.editingFinished.connect(self._commit)
        self._type_cb.activated.connect(lambda _index: self._commit())
        self._tag_edit.setFocus()

    def _commit(self):
        tag = self._tag_edit.text().strip().upper()
        comp_type = self._type_cb.currentText().strip()
        self.committed.emit(comp_type, tag)


class RRFPopup(QDialog):
    """Quick-pick popup for setting a safeguard's RRF value and type."""
    rrf_selected = pyqtSignal(int, str)   # (rrf_value, sg_type)

    def __init__(self, current_rrf: int, current_sg_type: str = 'Övrigt', parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra RRF")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        layout.addWidget(QLabel("<b>Risk Reduction Factor (RRF)</b>"))

        # Type selector
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Typ:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(SG_TYPES)
        idx = self._type_combo.findText(current_sg_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._type_combo.setStyleSheet("font-size:10px;")
        type_row.addWidget(self._type_combo)
        layout.addLayout(type_row)

        # Preset buttons
        presets = QHBoxLayout()
        for val in (1, 10, 100, 1000, 10000):
            btn = QPushButton(str(val))
            btn.setFixedWidth(62)
            btn.setStyleSheet(
                "QPushButton{background:#2F5FD0;color:white;border:none;"
                "border-radius:4px;padding:5px;font-weight:bold;}"
                "QPushButton:hover{background:#3D6BD8;}")
            btn.clicked.connect(partial(self._pick, val))
            presets.addWidget(btn)
        layout.addLayout(presets)

        # Custom value
        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Eget:"))
        self._spin = QSpinBox()
        self._spin.setRange(1, 1_000_000)
        self._spin.setValue(current_rrf)
        custom_row.addWidget(self._spin)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(partial(self._pick, self._spin.value()))
        custom_row.addWidget(ok_btn)
        layout.addLayout(custom_row)

    def _pick(self, val: int):
        self.rrf_selected.emit(val, self._type_combo.currentText())
        self.accept()


class FrequencyPickerPopup(QDialog):
    """Quick-pick popup for setting a cause's frequency: either a matrix
    F-level preset (labelled with the live-configured axis text) or an
    exact numeric events/year value.

    Mirrors RRFPopup's "preset buttons + custom spinbox" layout and
    ConsCategoryMatrixPopup's frameless small-popup styling.
    """

    # (f_level_int_or_None, numeric_freq_or_None) — exactly one is non-None.
    frequency_selected = pyqtSignal(object, object)

    def __init__(self, current_f_level=None, current_numeric_freq=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra frekvens")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        layout.addWidget(QLabel("<b>Frekvens</b>"))

        cfg  = get_matrix()
        cols = cfg.get('cols', 7)
        # Valid F-level range is -1 .. (cols - 2): column 0 is F=-1.
        f_levels = list(range(-1, cols - 1))

        # ── Preset buttons (wrapped grid, matrix-configured labels) ──────────
        presets = QGridLayout()
        presets.setSpacing(4)
        self._preset_btns = {}
        per_row = 4
        for i, f in enumerate(f_levels):
            btn = QPushButton(freq_axis_label_full(f))
            btn.setToolTip(freq_axis_label_full(f))
            btn.setStyleSheet(self._bstyle(f == current_f_level))
            btn.clicked.connect(partial(self._pick_preset, f))
            self._preset_btns[f] = btn
            presets.addWidget(btn, i // per_row, i % per_row)
        layout.addLayout(presets)

        # ── Custom numeric value (events/year) ───────────────────────────────
        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Eget (händelser/år):"))
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(6)
        self._spin.setRange(0.0, 1_000_000.0)
        self._spin.setSingleStep(0.01)
        if current_numeric_freq is not None:
            self._spin.setValue(float(current_numeric_freq))
        self._spin.valueChanged.connect(self._update_preview_label)
        custom_row.addWidget(self._spin)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._pick_numeric)
        custom_row.addWidget(ok_btn)
        layout.addLayout(custom_row)

        # ── Live F-level preview for the numeric field ───────────────────────
        self._preview_lbl = QLabel()
        self._preview_lbl.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(self._preview_lbl)
        if current_numeric_freq is not None:
            self._update_preview_label(float(current_numeric_freq))
        else:
            self._update_preview_label(self._spin.value())

        self.adjustSize()

    @staticmethod
    def _bstyle(selected: bool) -> str:
        if selected:
            return ("QPushButton{background:#2F5FD0;color:white;border:none;"
                    "border-radius:4px;padding:5px;font-weight:bold;font-size:10px;}"
                    "QPushButton:hover{background:#3D6BD8;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;border:1px solid #CFD1CE;"
                "border-radius:4px;padding:5px;font-size:10px;}"
                "QPushButton:hover{background:#E8E9E6;border:1px solid #B3B7B2;}")

    def _update_preview_label(self, val):
        f_lvl = freq_to_f_level(val) if val else -1
        self._preview_lbl.setText(f"→ {freq_axis_label_full(f_lvl)}")

    def _pick_preset(self, f_level: int):
        self.frequency_selected.emit(f_level, None)
        self.accept()

    def _pick_numeric(self):
        self.frequency_selected.emit(None, self._spin.value())
        self.accept()

    @classmethod
    def create_positioned(cls, global_pos, current_f_level=None,
                           current_numeric_freq=None, parent=None):
        """Construct the popup and position it near global_pos, clamped to
        the screen — mirrors the clamping pattern used at RRFPopup's and
        ConsCategoryMatrixPopup's call sites elsewhere in this file
        (adjustSize() → compute available screen geometry → clamp x/y).

        Callers should connect `frequency_selected` and then call
        `.exec()` themselves, exactly like the existing RRFPopup /
        ConsCategoryMatrixPopup call sites do.
        """
        popup = cls(current_f_level, current_numeric_freq, parent)
        popup.adjustSize()
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = min(global_pos.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        return popup


class DeviationPickerPopup(QDialog):
    """Minimalistic popup for a click on the Avvikelse (_C_DEV) cell —
    pick an existing deviation for the node, each showing its derived
    default frequency when one exists, or type a new/custom deviation
    (2026-08-14, see NOTES.md: "klockan man på avvikelsen justerar man
    avvikelsen ... avvikelsen skall bestå dels av celler med förvalda
    och dels med möjlighet att välja text själv"). Mirrors
    FrequencyPickerPopup's "preset buttons + custom field" layout.
    Distinct from the pre-existing "↕ Flytta till annan avvikelse…"
    context-menu action (_move_cause_dialog, a plain QInputDialog
    list) — this is the same underlying move, just reachable with one
    click and previewing each preset's frequency."""

    # (deviation_id_or_None, new_description_or_None) — exactly one is non-None.
    deviation_picked = pyqtSignal(object, object)

    def __init__(self, db, node_id, current_deviation_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Avvikelse")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(QLabel("<b>Avvikelse</b>"))

        # Existing deviations for this node, deduped by description text
        # (a node can end up with more than one row sharing the same
        # guide word after merges — only one button per distinct text).
        seen = {}
        for d in db.deviations(node_id):
            seen.setdefault(d['description'], d['id'])

        presets = QGridLayout()
        presets.setSpacing(4)
        per_row = 3
        for i, (desc, dev_id) in enumerate(seen.items()):
            freq = db.default_frequency_for_deviation(desc)
            label = desc if freq is None else f"{desc}\n({freq:g}/år)"
            btn = QPushButton(label)
            btn.setToolTip(desc if freq is None
                           else f"{desc} — förvald frekvens {freq:g}/år")
            btn.setStyleSheet(self._bstyle(dev_id == current_deviation_id))
            btn.clicked.connect(partial(self._pick_existing, dev_id))
            presets.addWidget(btn, i // per_row, i % per_row)
        layout.addLayout(presets)

        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Ny/egen:"))
        self._freetext = QLineEdit()
        self._freetext.setPlaceholderText("t.ex. Omvänt flöde")
        custom_row.addWidget(self._freetext)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._pick_freetext)
        custom_row.addWidget(ok_btn)
        layout.addLayout(custom_row)
        self._freetext.returnPressed.connect(self._pick_freetext)

        self.adjustSize()

    @staticmethod
    def _bstyle(selected: bool) -> str:
        if selected:
            return ("QPushButton{background:#2F5FD0;color:white;border:none;"
                    "border-radius:4px;padding:5px;font-weight:bold;font-size:10px;}"
                    "QPushButton:hover{background:#3D6BD8;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;border:1px solid #CFD1CE;"
                "border-radius:4px;padding:5px;font-size:10px;}"
                "QPushButton:hover{background:#E8E9E6;border:1px solid #B3B7B2;}")

    def _pick_existing(self, dev_id):
        self.deviation_picked.emit(dev_id, None)
        self.accept()

    def _pick_freetext(self):
        text = self._freetext.text().strip()
        if not text:
            return
        self.deviation_picked.emit(None, text)
        self.accept()

    @classmethod
    def create_positioned(cls, db, node_id, current_deviation_id, global_pos, parent=None):
        popup = cls(db, node_id, current_deviation_id, parent)
        popup.adjustSize()
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = min(global_pos.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        return popup


