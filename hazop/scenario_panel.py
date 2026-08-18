#!/usr/bin/env python3
"""HAZOP scenario table panel and its dialogs/delegates — split out of
hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer"."""

import re
import json
import logging
from functools import partial

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter,
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QSizePolicy,
    QSpinBox, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QEvent, QMimeData, QPoint, QPointF, QRect, QSize, QTimer,
)
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter,
    QPen, QPixmap,
)

from constants import CAUSE_T, CONS_T, SG_T, SG_TYPES, CONFIG
from database import Database, DEFAULT_MATRIX, get_matrix, risk_info, parse_tag_refs
from pid_viewer import _icon, _obj_type_matches, FREQ_LABELS, freq_to_idx, MODE_PICK_REF_TAG
from ui_helpers import (
    freq_axis_label, cons_axis_label, _lookup_comp_type_for_tag,
    _resolve_std_deviation_id, _draw_text_with_bold_tags,
    total_freq_reduction, CHAIN_ITEMS, build_consequence_text, parse_chain_from_json,
)
from tree_panel import (
    StandardCausesPickerPopup, CauseObjectPopup, CauseTagPopup, RRFPopup,
    FrequencyPickerPopup, DeviationPickerPopup,
)

class RiskMatrixPopup(QDialog):
    """Popup risk matrix matching the configured format in Settings."""

    selection_made = pyqtSignal(int, int)   # freq_value, cons_value

    def __init__(self, current_freq: int, current_cons: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Välj risknivå")
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        cfg       = get_matrix()
        n_cons    = cfg.get('rows', 5)
        n_freq    = cfg.get('cols', 7)
        x_lbls    = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        y_lbls    = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors         = cfg.get('cell_colors', [])
        cell_lbl       = cfg.get('cell_labels', [])
        cell_fg_colors = cfg.get('cell_fg_colors', [])
        freq_on_x = cfg.get('x_axis', 'frequency') == 'frequency'
        x_rev     = cfg.get('x_reversed', False)
        y_rev     = cfg.get('y_reversed', False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        hdr = QLabel("Klicka på en cell för att sätta risknivå")
        hdr.setStyleSheet("font-weight:bold; font-size:11px; padding:2px;")
        outer.addWidget(hdr)

        grid = QGridLayout()
        grid.setSpacing(0)

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons
            col_lbls, row_lbls = x_lbls, y_lbls
            corner_txt = "C \\ F"
        else:
            n_dcols, n_drows = n_cons, n_freq
            col_lbls, row_lbls = y_lbls, x_lbls
            corner_txt = "F \\ C"

        # Corner
        corner = QLabel(corner_txt)
        corner.setStyleSheet("font-size:9px; color:#666;")
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setFixedWidth(50)
        grid.addWidget(corner, 0, 0)

        # Column headers — respect x_rev
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            full   = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            # Short label: take first token (e.g. "F3" from "F3 – Möjlig | 10-100 år")
            short  = full.split()[0] if full.strip() else str(data_c)
            lbl = QLabel(short)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFixedWidth(50)
            lbl.setStyleSheet("font-size:9px; font-weight:bold; padding:1px;")
            lbl.setToolTip(full)
            grid.addWidget(lbl, 0, c + 1)

        # Rows — respect y_rev
        for r in range(n_drows):
            if y_rev:
                disp_r = r
            else:
                disp_r = n_drows - 1 - r

            # Row header
            full_r = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            short_r = full_r.split()[0] if full_r.strip() else str(disp_r)
            rl = QLabel(short_r)
            rl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rl.setStyleSheet("font-size:9px; font-weight:bold; padding-right:4px;")
            rl.setToolTip(full_r)
            rl.setFixedWidth(50)
            grid.addWidget(rl, r + 1, 0)

            for c in range(n_dcols):
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map to (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx, freq_idx = disp_r, data_c
                else:
                    freq_idx, cons_idx = disp_r, data_c

                freq_val = freq_idx - 1   # F=-1..5 (col 0 → F=-1)
                cons_val = cons_idx + 1   # C=1..5

                try:
                    color = colors[cons_idx][freq_idx]
                    lbl   = cell_lbl[cons_idx][freq_idx]
                except (IndexError, KeyError):
                    color, lbl = '#27ae60', 'Låg'
                try:
                    fg = cell_fg_colors[cons_idx][freq_idx] or '#ffffff'
                except (IndexError, KeyError, TypeError):
                    fg = '#ffffff'

                is_current = (freq_val == current_freq and cons_val == current_cons)
                border = '3px solid #000' if is_current else '0px'

                btn = QPushButton(lbl[:4])
                btn.setFixedSize(50, 32)
                btn.setToolTip(f"F={freq_val}  C={cons_val}  →  {lbl}")
                # Qt auto-assigns one pushbutton in a QDialog as the
                # "default"/initially-focused button (normally the first
                # one created) — the app's global stylesheet then paints
                # THAT button with an extra blue focus/default outline
                # (QPushButton:focus/:default rules) on top of whichever
                # cell already has the real black is_current border below,
                # so two cells looked marked regardless of hover
                # (2026-08-14 follow-up to the hover fix below — a
                # second, independent cause of the same symptom).
                btn.setAutoDefault(False)
                btn.setDefault(False)
                btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                # Hover style is intentionally NOT a solid black border —
                # that looked identical to the is_current marker above,
                # so hovering a different cell than the actual current one
                # looked like "two cells are marked" (reported feedback).
                # Dashed blue is unmistakably "just hovering", never "this
                # is the current value".
                btn.setStyleSheet(
                    f"QPushButton{{background:{color}; color:{fg};"
                    f"font-size:8px; font-weight:bold;"
                    f"border:{border}; border-radius:0px; margin:0px;}}"
                    f"QPushButton:hover{{border:2px dashed #2f6fed;}}")
                btn.clicked.connect(
                    lambda _, fv=freq_val, cv=cons_val: self._pick(fv, cv))
                grid.addWidget(btn, r + 1, c + 1)

        outer.addLayout(grid)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        outer.addWidget(cancel_btn)

        self.adjustSize()

    def _pick(self, freq, cons):
        self.selection_made.emit(freq, cons)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)


class ConsequenceChainDialog(QDialog):
    """Popup chain editor with QCheckBoxes — kept for legacy compatibility."""

    def __init__(self, db: Database, cons_id: int, parent=None):
        super().__init__(parent)
        self.db      = db
        self.cons_id = cons_id
        self.setWindowTitle("Konsekvenskedja")
        self.setMinimumWidth(CONFIG['W_PANEL_MIN_MD'])

        row = db.get_consequence(cons_id)
        self._chain = parse_chain_from_json(
            row['consequence_chain'] if row and 'consequence_chain' in row.keys() else '')
        raw_desc = row['description'] if row else ''

        layout = QVBoxLayout(self)

        form = QFormLayout(); form.setSpacing(8)
        self._base_edit = QLineEdit(raw_desc)
        self._base_edit.setPlaceholderText("Händelse / direkt konsekvens")
        self._base_edit.textChanged.connect(self._update_preview)
        form.addRow("Händelse:", self._base_edit)
        layout.addLayout(form)

        chain_box = QGroupBox("Konsekvenskedja — välj eskalering")
        chain_lay = QGridLayout(chain_box)
        chain_lay.setSpacing(4)
        self._checks: dict = {}
        row_idx, col_idx, last_group = 0, 0, None

        for key, label, group in CHAIN_ITEMS:
            if group and group != last_group:
                if col_idx > 0:
                    row_idx += 1; col_idx = 0
                hdr = QLabel(group)
                hdr.setStyleSheet(
                    "color:#8D9299; font-weight:bold; font-size:10px; margin-top:4px;")
                chain_lay.addWidget(hdr, row_idx, 0, 1, 2)
                row_idx += 1; col_idx = 0
                last_group = group
            chk = QCheckBox(label)
            chk.setChecked(bool(self._chain.get(key, False)))
            chk.stateChanged.connect(self._update_preview)
            self._checks[key] = chk
            chain_lay.addWidget(chk, row_idx, col_idx)
            col_idx += 1
            if col_idx >= 2:
                col_idx = 0; row_idx += 1

        layout.addWidget(chain_box)

        preview_lbl = QLabel("Genererad text:")
        preview_lbl.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(preview_lbl)
        self._preview = QLabel("—")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            "color:#17191C; font-weight:bold; font-size:11px;"
            "background:#F5F5F3; border:1px solid #E2E3E1;"
            "border-radius:3px; padding:4px 8px;")
        layout.addWidget(self._preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save_and_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self._update_preview()

    def _update_preview(self):
        chain = {k: chk.isChecked() for k, chk in self._checks.items()}
        text  = build_consequence_text(self._base_edit.text().strip(), chain)
        self._preview.setText(text or "—")

    def _save_and_accept(self):
        chain    = {k: chk.isChecked() for k, chk in self._checks.items()}
        base     = self._base_edit.text().strip()
        full     = build_consequence_text(base, chain) or base or 'Ny konsekvens'
        cons     = self.db.get_consequence(self.cons_id)
        if cons:
            self.db.update_consequence(
                self.cons_id, full,
                cons['severity'] or 1,
                cons['category'] or '',
                json.dumps(chain))
        self.accept()


# ── NEW: column-based step picker ─────────────────────────────────────────────
_N_STEPS = 5

# ── Konsekvensgraf (event-tree-logik) ─────────────────────────────────────────
# Varje nod har 'text' och 'next' (logiska eskaleringssteg).
# Grafen följer event tree-analys (CCPS/DNV). Allt omitigerat.
# [objekt] ersätts med kolumnens Ref-tag vid visning/sparning.
_CONSEQ_NODES: dict = {
    'reduced_flow':         {'text': 'Reducerat flöde till [objekt]',           'next': ['low_level','hx_overheat','reaction_upset','quality_offspec','backpressure_upstream']},
    'no_flow':              {'text': 'Inget flöde till [objekt]',                'next': ['low_level','hx_overheat','pump_dryrun','reaction_upset','production_stop']},
    'high_flow':            {'text': 'För högt flöde till [objekt]',             'next': ['high_level','erosion','hx_undercool','carryover']},
    'low_level':            {'text': 'Låg nivå i [objekt]',                      'next': ['pump_dryrun','vortex_gas','no_flow']},
    'high_level':           {'text': 'Hög nivå i [objekt]',                      'next': ['overfill','carryover']},
    'overfill':             {'text': 'Överfyllnad av [objekt]',                  'next': ['pool_formation','env_release']},
    'carryover':            {'text': 'Vätskedroppar förs med gasen från [objekt]','next': ['liquid_slug']},
    'liquid_slug':          {'text': 'Vätskeslag i [objekt]',                    'next': ['equipment_catastrophic','loc_large']},
    'vortex_gas':           {'text': 'Gasinblandning i utlopp från [objekt]',    'next': ['pump_dryrun']},
    'pump_dryrun':          {'text': '[objekt] torrkör / kaviterar',              'next': ['seal_fail','bearing_fail']},
    'seal_fail':            {'text': 'Tätningsläckage på [objekt]',              'next': ['loc_small']},
    'bearing_fail':         {'text': 'Mekanisk skada på [objekt] (lager / löphjul)', 'next': ['pump_breakdown']},
    'pump_breakdown':       {'text': '[objekt] havererar',                       'next': ['production_stop','no_flow']},
    'backpressure_upstream':{'text': 'Ökat mottryck uppströms [objekt]',         'next': ['overpressure']},
    'erosion':              {'text': 'Erosion i [objekt] (hög strömningshastighet)', 'next': ['loc_small']},
    'hx_overheat':          {'text': 'Otillräcklig kylning — temperaturen i [objekt] stiger', 'next': ['vapor_pressure_rise','runaway','seal_degradation','quality_offspec']},
    'hx_undercool':         {'text': 'Överkylning av processmedium i [objekt]',  'next': ['quality_offspec','freeze_damage','hydrate_blockage']},
    'reaction_upset':       {'text': 'Reaktionsstörning i [objekt]',             'next': ['quality_offspec','runaway','toxic_gas_generation']},
    'quality_offspec':      {'text': 'Produkt utanför specifikation',            'next': ['production_stop']},
    'overpressure':         {'text': 'Trycket i [objekt] överstiger konstruktionstrycket', 'next': ['flange_leak','rupture']},
    'flange_leak':          {'text': 'Fläns- / packningsläckage vid [objekt]',   'next': ['loc_small']},
    'rupture':              {'text': '[objekt] brister',                          'next': ['loc_large','equipment_catastrophic']},
    'vacuum':               {'text': 'Undertryck i [objekt] under lägsta tillåtna driftstryck', 'next': ['vacuum_collapse','air_ingress']},
    'vacuum_collapse':      {'text': '[objekt] kollapsas av undertrycket',        'next': ['equipment_catastrophic','loc_small']},
    'air_ingress':          {'text': 'Luftinträngning i [objekt]',               'next': ['internal_flammable','quality_offspec']},
    'internal_flammable':   {'text': 'Brännbar atmosfär inuti [objekt]',         'next': ['internal_explosion']},
    'internal_explosion':   {'text': 'Intern explosion i [objekt]',              'next': ['equipment_catastrophic','loc_large','personnel_injury']},
    'flashing':             {'text': 'Processvätska förångas / flashar i [objekt]', 'next': ['pump_dryrun','quality_offspec']},
    'temp_above_design':    {'text': 'Temperaturen i [objekt] överstiger konstruktionsgränsen', 'next': ['vapor_pressure_rise','runaway','seal_degradation','material_creep','quality_offspec']},
    'vapor_pressure_rise':  {'text': 'Ångbildning — trycket i [objekt] stiger', 'next': ['overpressure']},
    'runaway':              {'text': 'Okontrollerad exoterm reaktion i [objekt]','next': ['rapid_pressure_rise','toxic_gas_generation']},
    'rapid_pressure_rise':  {'text': 'Snabb tryck- och temperaturökning i [objekt]', 'next': ['rupture']},
    'material_creep':       {'text': 'Reducerad hållfasthet i [objekt] (krypning)', 'next': ['rupture']},
    'seal_degradation':     {'text': 'Tätningar och packningar i [objekt] degraderas', 'next': ['seal_fail','flange_leak']},
    'temp_below_design':    {'text': 'Temperaturen i [objekt] understiger konstruktionsgränsen', 'next': ['brittle_fracture','hydrate_blockage','freeze_damage']},
    'brittle_fracture':     {'text': 'Försprödning av [objekt] — risk för sprödbrott', 'next': ['rupture']},
    'hydrate_blockage':     {'text': 'Hydrat- / isproppsbildning i [objekt]',    'next': ['no_flow','overpressure']},
    'freeze_damage':        {'text': 'Sönderfrysning av [objekt]',               'next': ['loc_small']},
    'reverse_flow':         {'text': 'Backflöde genom [objekt]',                 'next': ['upstream_contamination','pump_reverse','incompatible_mixing']},
    'upstream_contamination':{'text':'Kontaminering av uppströmssystemet via [objekt]', 'next': ['quality_offspec','incompatible_mixing']},
    'pump_reverse':         {'text': '[objekt] roterar baklänges',               'next': ['bearing_fail']},
    'misdirected_flow':     {'text': 'Flödet leds till [objekt] i stället för avsedd destination', 'next': ['high_level','incompatible_mixing','no_flow']},
    'incompatible_mixing':  {'text': 'Inkompatibla medier blandas i [objekt]',  'next': ['runaway','toxic_gas_generation','overpressure']},
    'contamination_feed':   {'text': 'Avvikande sammansättning i inflödet till [objekt]', 'next': ['quality_offspec','incompatible_mixing','reaction_upset']},
    'toxic_gas_generation': {'text': 'Giftig eller korrosiv gas bildas i [objekt]', 'next': ['overpressure','toxic_exposure']},
    'utility_loss':         {'text': 'Hjälpmedier till [objekt] faller bort',   'next': ['hx_overheat','no_flow','production_stop']},
    'loc_small':            {'text': 'Läckage från [objekt]',                    'next': ['jet_fire','pool_formation','flash_fire','toxic_exposure','env_release']},
    'loc_large':            {'text': 'Okontrollerat utsläpp från [objekt]',      'next': ['jet_fire','pool_formation','vce','flash_fire','toxic_exposure','env_release']},
    'pool_formation':       {'text': 'Vätskepöl bildas vid [objekt]',            'next': ['pool_fire','env_release','toxic_exposure']},
    'jet_fire':             {'text': 'Jetbrand vid [objekt]',                    'next': ['escalation_bleve','personnel_injury','equipment_damage']},
    'pool_fire':            {'text': 'Pölbrand vid [objekt]',                    'next': ['escalation_bleve','personnel_injury','equipment_damage']},
    'flash_fire':           {'text': 'Fördröjd antändning — flash fire',         'next': ['personnel_injury']},
    'vce':                  {'text': 'Ångmolnsexplosion (VCE)',                  'next': ['fatality','equipment_catastrophic']},
    'escalation_bleve':     {'text': 'Brandpåverkan på intilliggande kärl — BLEVE / dominoeffekt', 'next': ['fatality','equipment_catastrophic']},
    'toxic_exposure':       {'text': 'Exponering av personal för giftig gas',    'next': ['personnel_injury','fatality']},
    'env_release':          {'text': 'Utsläpp till mark, vatten eller luft',     'next': ['env_impact']},
    'personnel_injury':     {'text': 'Personskada',                              'next': ['fatality']},
    'fatality':             {'text': 'Dödsolycka',                               'next': []},
    'equipment_damage':     {'text': 'Utrustningsskada',                         'next': ['production_stop']},
    'equipment_catastrophic':{'text':'Allvarlig skada på utrustning och struktur','next': ['production_stop']},
    'production_stop':      {'text': 'Produktionsavbrott',                       'next': []},
    'env_impact':           {'text': 'Miljöpåverkan — sanering och myndighetsrapportering krävs', 'next': []},
    'tube_failure':         {'text': 'Rörbrott i värmeväxlare — korsflöde',         'next': ['incompatible_mixing', 'overpressure', 'loc_small']},
}

_CONSEQ_ENTRY: dict = {
    # ── Generic (wildcard object) ──────────────────────────────────────────────
    ('Lågt flöde', '*'):              ['reduced_flow', 'no_flow'],
    ('Högt flöde', '*'):              ['high_flow', 'high_level', 'erosion'],
    ('Högt tryck', '*'):              ['overpressure'],
    ('Lågt tryck', '*'):              ['vacuum', 'flashing', 'reduced_flow'],
    ('Hög nivå', '*'):                ['high_level', 'carryover'],
    ('Låg nivå', '*'):                ['low_level'],
    ('Hög temperatur', '*'):          ['temp_above_design'],
    ('Låg temperatur', '*'):          ['temp_below_design'],
    ('Omvänt flöde', '*'):            ['reverse_flow'],
    ('Missriktat flöde', '*'):        ['misdirected_flow'],
    ('Avvikande sammansättning', '*'): ['contamination_feed'],
    ('Bortfall av hjälpsystem', '*'): ['utility_loss'],
    ('Drift', '*'):                   ['no_flow', 'high_level', 'overpressure', 'loc_small', 'misdirected_flow'],
    ('Underhåll', '*'):               ['loc_small', 'no_flow', 'overpressure', 'internal_flammable'],
    ('Start-up / Shut-down', '*'):    ['loc_small', 'overpressure', 'internal_flammable', 'liquid_slug', 'brittle_fracture'],

    # ── Pump ──────────────────────────────────────────────────────────────────
    ('Lågt flöde',  'Pump'):          ['pump_dryrun', 'vortex_gas', 'reduced_flow'],
    ('Högt flöde',  'Pump'):          ['erosion', 'bearing_fail'],
    ('Högt tryck',  'Pump'):          ['seal_fail', 'overpressure'],
    ('Lågt tryck',  'Pump'):          ['pump_dryrun', 'vacuum'],
    ('Hög temperatur', 'Pump'):       ['seal_degradation', 'bearing_fail'],
    ('Låg temperatur', 'Pump'):       ['freeze_damage', 'hydrate_blockage'],
    ('Omvänt flöde',   'Pump'):       ['pump_reverse', 'bearing_fail'],

    # ── Tank / kärl ───────────────────────────────────────────────────────────
    ('Lågt flöde',  'Tank / kärl / kolonn'):  ['low_level', 'pump_dryrun'],
    ('Högt flöde',  'Tank / kärl / kolonn'):  ['high_level', 'overfill'],
    ('Högt tryck',  'Tank / kärl / kolonn'):  ['overpressure', 'rupture'],
    ('Lågt tryck',  'Tank / kärl / kolonn'):  ['vacuum', 'vacuum_collapse'],
    ('Hög nivå',    'Tank / kärl / kolonn'):  ['high_level', 'overfill', 'carryover'],
    ('Låg nivå',    'Tank / kärl / kolonn'):  ['low_level', 'vortex_gas', 'pump_dryrun'],
    ('Hög temperatur','Tank / kärl / kolonn'):['vapor_pressure_rise', 'runaway', 'temp_above_design'],
    ('Låg temperatur','Tank / kärl / kolonn'):['brittle_fracture', 'freeze_damage', 'temp_below_design'],
    ('Avvikande sammansättning','Tank / kärl / kolonn'): ['incompatible_mixing', 'reaction_upset', 'contamination_feed'],

    # ── Rörledning ────────────────────────────────────────────────────────────
    ('Lågt flöde',  'Rörledning / slang'):  ['reduced_flow', 'hydrate_blockage'],
    ('Högt tryck',  'Rörledning / slang'):  ['flange_leak', 'loc_small'],
    ('Lågt tryck',  'Rörledning / slang'):  ['vacuum', 'air_ingress'],
    ('Hög temperatur','Rörledning / slang'):['seal_degradation', 'flange_leak'],
    ('Låg temperatur','Rörledning / slang'):['freeze_damage', 'brittle_fracture'],
    ('Omvänt flöde', 'Rörledning / slang'): ['upstream_contamination', 'reverse_flow'],

    # ── Värmeväxlare ──────────────────────────────────────────────────────────
    ('Lågt flöde',  'Värmeväxlare / kylare / värmare'): ['hx_overheat', 'reduced_flow'],
    ('Högt flöde',  'Värmeväxlare / kylare / värmare'): ['hx_undercool', 'erosion'],
    ('Hög temperatur','Värmeväxlare / kylare / värmare'):['tube_failure', 'overpressure'],
    ('Låg temperatur','Värmeväxlare / kylare / värmare'):['hx_undercool', 'freeze_damage'],

    # ── Manuell ventil ────────────────────────────────────────────────────────
    ('Lågt flöde',     'Manuell ventil'):     ['reduced_flow', 'no_flow'],
    ('Högt flöde',     'Manuell ventil'):     ['high_flow', 'erosion'],
    ('Högt tryck',     'Manuell ventil'):     ['overpressure', 'flange_leak'],
    ('Omvänt flöde',   'Manuell ventil'):     ['reverse_flow', 'upstream_contamination'],
    ('Missriktat flöde','Manuell ventil'):    ['misdirected_flow'],
    ('Underhåll',      'Manuell ventil'):     ['loc_small', 'no_flow', 'internal_flammable'],

    # ── On-off ventil ─────────────────────────────────────────────────────────
    ('Lågt flöde',     'On-off ventil'):      ['no_flow', 'reduced_flow'],
    ('Högt tryck',     'On-off ventil'):      ['overpressure', 'flange_leak'],
    ('Missriktat flöde','On-off ventil'):     ['misdirected_flow'],
    ('Bortfall av hjälpsystem','On-off ventil'): ['no_flow', 'overpressure'],
    ('Start-up / Shut-down','On-off ventil'): ['overpressure', 'loc_small', 'liquid_slug'],

    # ── Reglerventil ──────────────────────────────────────────────────────────
    ('Lågt flöde',     'Reglerventil'):       ['reduced_flow', 'no_flow'],
    ('Högt flöde',     'Reglerventil'):       ['high_flow', 'high_level', 'erosion'],
    ('Högt tryck',     'Reglerventil'):       ['overpressure', 'flange_leak'],
    ('Missriktat flöde','Reglerventil'):      ['misdirected_flow', 'high_level'],
    ('Bortfall av hjälpsystem','Reglerventil'): ['no_flow', 'overpressure', 'high_level'],
    ('Drift',          'Reglerventil'):       ['no_flow', 'overpressure', 'misdirected_flow'],

    # ── Backventil ────────────────────────────────────────────────────────────
    ('Omvänt flöde',   'Backventil'):         ['reverse_flow', 'upstream_contamination', 'pump_reverse'],
    ('Lågt flöde',     'Backventil'):         ['no_flow', 'reduced_flow'],
    ('Drift',          'Backventil'):         ['reverse_flow', 'upstream_contamination'],
    ('Underhåll',      'Backventil'):         ['reverse_flow', 'loc_small'],

    # ── Fläns / koppling / packning ───────────────────────────────────────────
    ('Högt tryck',     'Fläns / koppling / packning'): ['flange_leak', 'overpressure'],
    ('Hög temperatur', 'Fläns / koppling / packning'): ['seal_degradation', 'flange_leak'],
    ('Låg temperatur', 'Fläns / koppling / packning'): ['brittle_fracture', 'freeze_damage'],
    ('Underhåll',      'Fläns / koppling / packning'): ['loc_small', 'internal_flammable'],
    ('Drift',          'Fläns / koppling / packning'): ['flange_leak', 'loc_small'],

    # ── Kompressor / fläkt ────────────────────────────────────────────────────
    ('Lågt flöde',     'Kompressor / fläkt'): ['pump_dryrun', 'no_flow', 'bearing_fail'],
    ('Högt flöde',     'Kompressor / fläkt'): ['erosion', 'bearing_fail'],
    ('Högt tryck',     'Kompressor / fläkt'): ['overpressure', 'seal_fail'],
    ('Lågt tryck',     'Kompressor / fläkt'): ['pump_dryrun', 'vacuum'],
    ('Hög temperatur', 'Kompressor / fläkt'): ['seal_degradation', 'bearing_fail', 'temp_above_design'],
    ('Bortfall av hjälpsystem','Kompressor / fläkt'): ['no_flow', 'production_stop'],
    ('Start-up / Shut-down','Kompressor / fläkt'): ['liquid_slug', 'overpressure', 'bearing_fail'],

    # ── Filter / sil ──────────────────────────────────────────────────────────
    ('Lågt flöde',     'Filter / sil'):       ['reduced_flow', 'no_flow', 'backpressure_upstream'],
    ('Högt tryck',     'Filter / sil'):       ['overpressure', 'flange_leak'],
    ('Avvikande sammansättning','Filter / sil'): ['contamination_feed', 'quality_offspec'],
    ('Underhåll',      'Filter / sil'):       ['loc_small', 'no_flow'],
    ('Drift',          'Filter / sil'):       ['reduced_flow', 'backpressure_upstream'],

    # ── Säkerhetsventil / sprängbleck ─────────────────────────────────────────
    ('Högt tryck',     'Säkerhetsventil / sprängbleck'): ['loc_small', 'overpressure'],
    ('Drift',          'Säkerhetsventil / sprängbleck'): ['loc_small', 'production_stop'],
    ('Underhåll',      'Säkerhetsventil / sprängbleck'): ['loc_small', 'no_flow', 'overpressure'],
    ('Bortfall av hjälpsystem','Säkerhetsventil / sprängbleck'): ['overpressure', 'rupture'],

    # ── Instrument ────────────────────────────────────────────────────────────
    ('Bortfall av hjälpsystem','Instrument'): ['no_flow', 'overpressure', 'production_stop'],
    ('Drift',          'Instrument'):         ['quality_offspec', 'overpressure', 'no_flow'],
    ('Underhåll',      'Instrument'):         ['no_flow', 'production_stop'],

    # ── Styrsystem / PLC / DCS ────────────────────────────────────────────────
    ('Bortfall av hjälpsystem','Styrsystem / PLC / DCS'): ['no_flow', 'overpressure', 'high_level', 'production_stop'],
    ('Drift',          'Styrsystem / PLC / DCS'): ['misdirected_flow', 'overpressure', 'no_flow', 'high_level'],
    ('Start-up / Shut-down','Styrsystem / PLC / DCS'): ['overpressure', 'no_flow', 'liquid_slug'],
    ('Underhåll',      'Styrsystem / PLC / DCS'): ['no_flow', 'production_stop', 'overpressure'],
}

_CONSEQ_GENERIC_NEXT = [
    'loc_small', 'overpressure', 'equipment_damage',
    'personnel_injury', 'env_release', 'production_stop',
]

class ConsequenceStepPickerDialog(QDialog):
    """Konsekvenskedja Del1 → Del2 → Del3 → Del4 → Del5.

    All _N_STEPS columns are shown side by side — compact and tight, but
    the whole chain stays visible and directly editable at a glance rather
    than navigating one step at a time. Each column has a status-colored
    "Del N" header (muted gray = not reached / chain ended, light blue =
    options available, filled blue = a choice has been made), a scrollable
    option list built from the dependency graph, a free-text fallback, and
    a compact tag + object-type row. Picking an option (or typing free
    text) in column N immediately recomputes column N+1's options
    (cascading) and clears anything further downstream.
    """
    # Set when the "add more objects" button is clicked
    add_more_requested = False

    _COL_W = 150   # fixed width per "Del N" column — keeps the dialog tight

    _LIST_SS = (
        "QListWidget { border:1px solid #E2E3E1; border-radius:5px; background:white; }"
        "QListWidget::item { padding:3px 5px; border-radius:3px; font-size:10px; }"
        "QListWidget::item:selected {"
        "  background:#E6ECFA; color:#17191C; font-weight:bold;"
        "  border:1px solid #2F5FD0; }"
        "QListWidget::item:hover:!selected { background:#F5F5F3; }"
    )

    def __init__(self, db: 'Database', cons_id: int,
                 deviation: str = '', comp_type: str = '',
                 cause_text: str = '', initial_ref_tag: str = '',
                 parent=None):
        super().__init__(parent)
        self.db       = db
        self.cons_id  = cons_id
        self._dev     = deviation
        self._comp    = comp_type
        self._cause   = cause_text
        self.add_more_requested = False

        self.setWindowTitle("Konsekvenskedja — Del 1–5")
        col_gap = 9
        self.setMinimumWidth(min(
            QApplication.primaryScreen().availableGeometry().width() - 80,
            _N_STEPS * self._COL_W + (_N_STEPS - 1) * col_gap + 24))
        self.setMinimumHeight(440)
        # initial_ref_tag is pre-filled in Del1's ref-tag from the P&ID click
        self._initial_ref_tag = initial_ref_tag

        # Existing steps from DB
        existing = {s['step']: s for s in db.get_consequence_steps(cons_id)}

        # Context header
        cons_row = db.get_consequence(cons_id)
        self._orig_desc = cons_row['description'] if cons_row else ''

        main = QVBoxLayout(self)
        main.setSpacing(5)
        main.setContentsMargins(8, 8, 8, 8)

        # ── Context info ──────────────────────────────────────────────────────
        if deviation or comp_type or cause_text:
            ctx_parts = []
            if deviation:  ctx_parts.append(f"<b>Avvikelse:</b> {deviation}")
            if comp_type:  ctx_parts.append(f"<b>Objekt:</b> {comp_type}")
            if cause_text: ctx_parts.append(f"<b>Orsak:</b> {cause_text[:80]}")
            ctx = QLabel("  ·  ".join(ctx_parts))
            ctx.setStyleSheet("color:#17191C; font-size:10px; padding:2px 4px;"
                              "background:#F5F5F3; border-radius:3px;")
            ctx.setWordWrap(True)
            main.addWidget(ctx)

        # ── Column grid ───────────────────────────────────────────────────────
        cols_widget = QWidget()
        cols_layout = QHBoxLayout(cols_widget)
        cols_layout.setSpacing(0)
        cols_layout.setContentsMargins(0, 0, 0, 0)

        self._cols: list = []      # per-step state
        self._options: list = []   # option texts per step (graph node texts)
        self._opt_keys: list = []  # parallel node keys (None = free/saved text)

        for step in range(1, _N_STEPS + 1):
            self._options.append([])
            self._opt_keys.append([])

            if step > 1:
                cols_layout.addSpacing(4)
                divider = QFrame()
                divider.setFixedWidth(1)
                divider.setStyleSheet("background:#e5e7eb;")
                cols_layout.addWidget(divider)
                cols_layout.addSpacing(4)

            col_w = QWidget()
            col_w.setFixedWidth(self._COL_W)
            col_l = QVBoxLayout(col_w)
            col_l.setContentsMargins(0, 0, 0, 0)
            col_l.setSpacing(3)

            hdr = QLabel(f"Del {step}")
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_l.addWidget(hdr)

            # QListWidget — native scrolling + word wrap, stays compact
            lst = QListWidget()
            lst.setWordWrap(True)
            lst.setSpacing(1)
            lst.setAlternatingRowColors(False)
            lst.setMinimumHeight(110)
            lst.setStyleSheet(self._LIST_SS)
            lst.itemClicked.connect(
                lambda it, s=step-1, lw=lst: self._list_clicked(s, lw))
            col_l.addWidget(lst, 1)

            # Terminal-chain message — shares the list's space (only one of
            # the two is ever visible), shown once a graph node with no
            # successors is reached instead of leaving an empty list box.
            end_lbl = QLabel("Kedjan slutar här")
            end_lbl.setWordWrap(True)
            end_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            end_lbl.setStyleSheet(
                "color:#9a6a3d; font-size:9px; background:#fff7ed;"
                "border:1px solid #fde8cc; border-radius:5px; padding:6px;")
            end_lbl.setMinimumHeight(110)
            end_lbl.setVisible(False)
            col_l.addWidget(end_lbl, 1)

            # Free-text input
            ft_edit = QLineEdit()
            ft_edit.setPlaceholderText("Fritext…")
            ft_edit.setStyleSheet("font-size:10px;")
            ft_edit.textChanged.connect(
                lambda _t, s=step-1: (self._cascade_from(s),
                                      self._refresh_header_state(s),
                                      self._update_preview()))
            col_l.addWidget(ft_edit)

            # Tag row: compact "Tag" label + field + pin button (no separate
            # label line — the placeholder text carries the hint instead)
            tag_row = QHBoxLayout()
            tag_row.setContentsMargins(0, 0, 0, 0)
            tag_row.setSpacing(2)
            tag_lbl = QLabel("Tag")
            tag_lbl.setStyleSheet("color:#999; font-size:9px;")
            tag_lbl.setFixedWidth(20)
            ref_edit = QLineEdit()
            ref_edit.setPlaceholderText("t.ex. T-101")
            ref_edit.setMaximumHeight(22)
            ref_edit.setStyleSheet("font-size:10px;")
            if step in existing:
                ref_edit.setText(existing[step].get('ref_tag', '') or '')
            elif step == 1 and self._initial_ref_tag:
                ref_edit.setText(self._initial_ref_tag)
            pin_btn = QPushButton()
            pin_btn.setIcon(_icon('pin', 14, '#dc2626'))
            pin_btn.setFixedSize(22, 22)
            pin_btn.setToolTip("Klicka på P&ID för att välja referensobjekt")
            pin_btn.setStyleSheet(
                "QPushButton { border:1px solid #dc2626; border-radius:3px;"
                "  background:#fee2e2; color:#dc2626; font-size:10px; }"
                "QPushButton:hover { background:#fca5a5; }")
            pin_btn.clicked.connect(partial(self._request_pick_for_col, step-1))
            tag_row.addWidget(tag_lbl)
            tag_row.addWidget(ref_edit)
            tag_row.addWidget(pin_btn)
            col_l.addLayout(tag_row)

            # Object-type row: compact "Typ" label + combo, pre-filled from
            # smart recognition on ref-tag change
            typ_row = QHBoxLayout()
            typ_row.setContentsMargins(0, 0, 0, 0)
            typ_row.setSpacing(2)
            typ_lbl = QLabel("Typ")
            typ_lbl.setStyleSheet("color:#999; font-size:9px;")
            typ_lbl.setFixedWidth(20)
            obj_combo = QComboBox()
            obj_combo.setFixedHeight(22)
            obj_combo.setStyleSheet("font-size:9px;")
            obj_combo.setToolTip("Objekttyp")
            obj_combo.addItem('')
            try:
                for o in db.standard_objects():
                    obj_combo.addItem(o['name'])
            except Exception:
                pass
            # Pre-select based on initial data
            if step == 1 and comp_type:
                _idx = obj_combo.findText(comp_type)
                if _idx >= 0:
                    obj_combo.setCurrentIndex(_idx)
            typ_row.addWidget(typ_lbl)
            typ_row.addWidget(obj_combo, 1)
            col_l.addLayout(typ_row)

            # Connect after layout so step-1 index is correct
            ref_edit.textChanged.connect(
                lambda tag, s=step-1, cb=obj_combo: (
                    self._refresh_list_labels(s, tag),
                    self._autofill_obj_combo(cb, tag),
                    self._update_preview()))
            obj_combo.currentTextChanged.connect(
                lambda txt, s=step-1: (
                    self._on_obj_type_changed(s, txt),
                    self._update_preview()))

            cols_layout.addWidget(col_w)

            col_state = {
                'list':      lst,
                'end_lbl':   end_lbl,
                'sel':       -1,
                'ft_edit':   ft_edit,
                'ref_edit':  ref_edit,
                'obj_combo': obj_combo,
                'hdr':       hdr,
            }
            self._cols.append(col_state)

        # Fill columns: Del1 from entry nodes, Del2+ cascades from selections.
        # Saved chains are restored selection by selection.
        self._init_columns(existing)

        main.addWidget(cols_widget, 1)

        # ── Preview strip ─────────────────────────────────────────────────────
        prev_frame = QFrame()
        prev_frame.setFrameShape(QFrame.Shape.StyledPanel)
        prev_frame.setStyleSheet("background:#f0f9ff; border-radius:4px;")
        prev_lay = QVBoxLayout(prev_frame)
        prev_lay.setContentsMargins(6, 4, 6, 4)
        lbl = QLabel("Genererad kedjetext:")
        lbl.setStyleSheet("color:#555; font-size:10px;")
        prev_lay.addWidget(lbl)
        self._preview = QLabel("—")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            "color:#17191C; font-weight:bold; font-size:11px;")
        prev_lay.addWidget(self._preview)
        main.addWidget(prev_frame)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        add_more_btn = QPushButton("Lägg till ytterligare objekt")
        add_more_btn.setIcon(_icon('pin', 16, '#ffffff'))
        add_more_btn.setToolTip(
            "Spara denna kedja och återgå till P&ID-läge\n"
            "för att omedelbart markera ytterligare ett objekt.")
        add_more_btn.setStyleSheet(
            "background:#2F5FD0; color:white; border:none;"
            "border-radius:4px; padding:4px 10px;")
        add_more_btn.clicked.connect(self._save_and_add_more)
        btn_row.addWidget(add_more_btn)
        btn_row.addStretch()
        std_btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        std_btns.accepted.connect(self._save_and_accept)
        std_btns.rejected.connect(self.reject)
        btn_row.addWidget(std_btns)
        main.addLayout(btn_row)

        self._waiting_col_idx = None

        # ── Tab-key navigation between columns ────────────────────────────────
        # Tab in ft_edit or ref_edit of column N → focus ft_edit of column N+1
        for i, col in enumerate(self._cols):
            for field in (col['ft_edit'], col['ref_edit']):
                field.installEventFilter(self)
        self._field_order = []
        for col in self._cols:
            self._field_order.append(col['ft_edit'])
            self._field_order.append(col['ref_edit'])

        self._update_preview()

    # ── Graf-baserade alternativ ──────────────────────────────────────────────
    def _entry_pairs(self, obj_type: str = ''):
        """[(node_key, text)] for Del1, looked up per deviation + object type."""
        comp = obj_type or (
            self._cols[0]['obj_combo'].currentText()
            if self._cols else self._comp) or self._comp
        keys = (_CONSEQ_ENTRY.get((self._dev, comp)) or
                _CONSEQ_ENTRY.get((self._dev, '*')) or
                _CONSEQ_ENTRY.get((self._dev, '')) or
                _CONSEQ_GENERIC_NEXT)
        return [(k, _CONSEQ_NODES[k]['text']) for k in keys if k in _CONSEQ_NODES]

    def _autofill_obj_combo(self, combo: 'QComboBox', tag: str):
        """Look up object type from smart recognition for this tag and set combo."""
        if not tag.strip():
            return
        comp = _lookup_comp_type_for_tag(tag.strip(), self.db)
        if comp:
            idx = combo.findText(comp)
            if idx >= 0 and combo.currentIndex() != idx:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

    def _on_obj_type_changed(self, step_idx: int, obj_type: str):
        """Object type changed in a column — repopulate Del1 if this is col 0."""
        if step_idx == 0:
            # Re-initialise column 0 with deviation + new object type
            self._populate_column(0, self._entry_pairs(obj_type))
            self._cascade_from(0)

    @staticmethod
    def _successor_pairs(node_key):
        node = _CONSEQ_NODES.get(node_key)
        if not node:
            return []
        return [(k, _CONSEQ_NODES[k]['text'])
                for k in node['next'] if k in _CONSEQ_NODES]

    @staticmethod
    def _generic_pairs():
        return [(k, _CONSEQ_NODES[k]['text'])
                for k in _CONSEQ_GENERIC_NEXT if k in _CONSEQ_NODES]

    def _populate_column(self, step_idx: int, pairs, upstream_has_sel: bool = True):
        """Fill column step_idx with (key, text) pairs; clears selection.

        upstream_has_sel distinguishes two visually different empty states:
        when the PREVIOUS column has a selection but pairs is still empty,
        a real graph terminal was reached ("Kedjan slutar här" is shown);
        when the previous column has nothing chosen yet, this column just
        hasn't been reached, so it's left as a neutral empty list instead.
        """
        col = self._cols[step_idx]
        lst = col['list']
        tag = col['ref_edit'].text().strip()
        lst.clear()
        keys, texts = [], []
        for k, t in pairs:
            keys.append(k)
            texts.append(t)
            lst.addItem(QListWidgetItem(
                f"{len(texts)}. {self._resolve(t, tag)}"))
        self._options[step_idx]  = texts
        self._opt_keys[step_idx] = keys
        col['sel'] = -1
        lst.setCurrentRow(-1)
        has_opts = bool(pairs)
        show_end_msg = (not has_opts) and upstream_has_sel
        lst.setVisible(not show_end_msg)
        col['end_lbl'].setVisible(show_end_msg)
        self._refresh_header_state(step_idx)

    def _selected_key(self, step_idx: int):
        """Node key of the column's selection, or None (free text / none)."""
        col = self._cols[step_idx]
        if col['ft_edit'].text().strip():
            return None
        sel = col['list'].currentRow()
        keys = self._opt_keys[step_idx]
        if 0 <= sel < len(keys):
            return keys[sel]
        return None

    def _next_pairs_after(self, step_idx: int):
        """Compute the (key, text) pairs for step_idx+1 based on step_idx's state."""
        col = self._cols[step_idx]
        ft = col['ft_edit'].text().strip()
        key = self._selected_key(step_idx)
        has_sel = (col['list'].currentRow() >= 0) or bool(ft)
        if key:
            return self._successor_pairs(key)
        elif has_sel:
            return self._generic_pairs()   # free text / unknown node
        return []                           # nothing chosen → chain ends here

    def _cascade_from(self, step_idx: int):
        """Repopulate column step_idx+1 based on this column's state; clear rest."""
        if step_idx + 1 >= _N_STEPS:
            return
        col = self._cols[step_idx]
        upstream_has_sel = (col['list'].currentRow() >= 0) or bool(col['ft_edit'].text().strip())
        self._populate_column(step_idx + 1, self._next_pairs_after(step_idx), upstream_has_sel)
        self._cascade_from(step_idx + 1)     # downstream columns reset too

    def _init_columns(self, existing: dict):
        """Initial fill: entry nodes in Del1, neutral empty state for the
        rest, then walk saved chain if any (restoring selections)."""
        self._populate_column(0, self._entry_pairs())
        for i in range(1, _N_STEPS):
            self._populate_column(i, [], upstream_has_sel=False)
        for i in range(_N_STEPS):
            step  = i + 1
            saved = existing.get(step) if existing else None
            if not saved or not (saved.get('text') or '').strip():
                break
            s_text = saved['text']
            s_key  = (saved.get('node_key') or '').strip() or None
            s_tag  = (saved.get('ref_tag') or '').strip()
            keys   = self._opt_keys[i]
            texts  = self._options[i]
            sel = -1
            if s_key and s_key in keys:
                sel = keys.index(s_key)
            else:
                for j, t in enumerate(texts):
                    if self._resolve(t, s_tag) == s_text or t == s_text:
                        sel = j
                        break
            if sel < 0:
                # Saved text not among graph options — prepend it
                pairs = [(s_key, s_text)] + list(zip(keys, texts))
                self._populate_column(i, pairs)
                sel = 0
            self._cols[i]['sel'] = sel
            self._cols[i]['list'].setCurrentRow(sel)
            self._refresh_header_state(i)
            if i + 1 < _N_STEPS:
                nxt_key = self._opt_keys[i][sel] if sel < len(self._opt_keys[i]) else None
                pairs = self._successor_pairs(nxt_key) if nxt_key else self._generic_pairs()
                self._populate_column(i + 1, pairs, upstream_has_sel=True)

    # ── Column header status color ────────────────────────────────────────────
    def _refresh_header_state(self, step_idx: int):
        """Color-code the 'Del N' header: outlined = options available,
        filled dark = a choice has been made, muted gray = nothing to
        do here (not yet reached, or the chain ended) — an at-a-glance
        progress indicator across all visible columns."""
        col = self._cols[step_idx]
        has_sel  = col['sel'] >= 0 or bool(col['ft_edit'].text().strip())
        has_opts = bool(self._options[step_idx])
        if has_sel:
            style = ("font-weight:bold; color:white; font-size:10px;"
                     "background:#2F5FD0; border-radius:3px; padding:3px;")
        elif not has_opts:
            style = ("font-weight:bold; color:#8D9299; font-size:10px;"
                     "background:#F5F5F3; border-radius:3px; padding:3px;")
        else:
            style = ("font-weight:bold; color:#17191C; font-size:10px;"
                     "background:#FFFFFF; border:1px solid #CFD1CE; border-radius:3px; padding:2px;")
        col['hdr'].setStyleSheet(style)

    # ── Selection logic ───────────────────────────────────────────────────────
    def _list_clicked(self, step_idx: int, listwidget):
        row = listwidget.currentRow()
        old = self._cols[step_idx]['sel']
        if old == row:
            # Second click deselects
            listwidget.clearSelection()
            listwidget.setCurrentRow(-1)
            self._cols[step_idx]['sel'] = -1
        else:
            self._cols[step_idx]['sel'] = row
        # Dependent columns: repopulate Del(n+1) from the new selection
        self._cascade_from(step_idx)
        self._refresh_header_state(step_idx)
        self._update_preview()

    # ── Preview ───────────────────────────────────────────────────────────────
    def _selected_text(self, step_idx: int) -> str:
        col = self._cols[step_idx]
        tag = col['ref_edit'].text().strip()
        ft  = col['ft_edit'].text().strip()
        if ft:
            return self._resolve(ft, tag)
        sel = col['list'].currentRow()
        opts = self._options[step_idx]
        if 0 <= sel < len(opts):
            t = opts[sel]
            if t.startswith("("):
                return ''
            return self._resolve(t, tag)
        return ''

    def _update_preview(self):
        parts = []
        for i in range(_N_STEPS):
            t = self._selected_text(i)
            if t:
                parts.append(t)
        self._preview.setText(' → '.join(parts) if parts else '—')

    # ── [objekt] substitution ─────────────────────────────────────────────────
    @staticmethod
    def _resolve(text: str, tag: str) -> str:
        """Replace [objekt] with the ref-tag when a tag is known.
        When no tag is set, strip the placeholder and trailing preposition
        so the text reads naturally without a dangling noun.

        Examples:
          tag='T-101':  'Låg nivå i [objekt]'      → 'Låg nivå i T-101'
          tag='':       'Låg nivå i [objekt]'      → 'Låg nivå'
          tag='T-101':  '[objekt] torrkör'          → 'T-101 torrkör'
          tag='':       '[objekt] torrkör'          → 'Torrkörning'
          tag='T-101':  'Trycket överstiger gränsen'→ 'Trycket överstiger gränsen'
        """
        if not text:
            return text
        if tag:
            out = text.replace('[objekt]', tag)
            if out and out[0].islower():
                out = out[0].upper() + out[1:]
            return out
        # No tag — strip placeholder + any preceding OR following preposition phrase
        # so "Flödet leds till [objekt] i stället för avsedd destination"
        # → "Flödet leds" (not "Flödet leds i stället för avsedd destination")
        import re as _re
        # Remove "till [objekt] i stället för <something>" as a unit
        stripped = _re.sub(
            r'\s+(?:i\s+stället\s+för|inuti|i\s+inflödet\s+till|från|till|vid|av|för|mot|ur|på|i)\s+\[objekt\](?:\s+i\s+stället\s+för\s+[^,—]*)?',
            '', text, flags=_re.IGNORECASE)
        stripped = stripped.replace(' [objekt]', '').replace('[objekt] ', '').replace('[objekt]', '')
        stripped = stripped.rstrip(' \t,;—')
        if stripped and stripped[0].islower():
            stripped = stripped[0].upper() + stripped[1:]
        return stripped

    def _refresh_list_labels(self, step_idx: int, tag: str):
        """Update list item display text when ref-tag changes for this column."""
        lst  = self._cols[step_idx]['list']
        opts = self._options[step_idx]
        for i, opt in enumerate(opts):
            item = lst.item(i)
            if item is not None:
                item.setText(f"{i+1}. {self._resolve(opt, tag)}")

    # ── Pin button: pick ref-tag from P&ID ────────────────────────────────────
    def _request_pick_for_col(self, col_idx: int):
        """Hide dialog, enter MODE_PICK_REF_TAG; MainWindow refills col on pick."""
        self._waiting_col_idx = col_idx
        self.hide()
        # Walk up to MainWindow and trigger the pick mode
        p = self.parent()
        while p is not None:
            if hasattr(p, 'pid_panel') and hasattr(p.pid_panel, '_set_mode'):
                p.pid_panel._set_mode(MODE_PICK_REF_TAG)
                break
            p = p.parent() if hasattr(p, 'parent') else None

    # ── Tab navigation event filter ───────────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if (event.type() == QEvent.Type.KeyPress and
                event.key() == Qt.Key.Key_Tab):
            try:
                idx = self._field_order.index(obj)
                nxt = self._field_order[(idx + 1) % len(self._field_order)]
                nxt.setFocus()
                nxt.selectAll()
                return True
            except ValueError:
                pass
        return super().eventFilter(obj, event)

    # ── Save helpers ──────────────────────────────────────────────────────────
    def _save_and_add_more(self):
        self.add_more_requested = True
        self._do_save()
        self.accept()

    # ── Save ──────────────────────────────────────────────────────────────────
    def _save_and_accept(self):
        self.add_more_requested = False
        self._do_save()
        self.accept()

    def _do_save(self):
        steps = []
        for i in range(_N_STEPS):
            text = self._selected_text(i)
            ref  = self._cols[i]['ref_edit'].text().strip()
            if text or ref:
                steps.append({'step': i + 1, 'text': text, 'ref_tag': ref,
                              'node_key': self._selected_key(i) or ''})
        self.db.set_consequence_steps(self.cons_id, steps)

        parts = [s['text'] for s in steps if s['text']]
        full  = ' → '.join(parts) if parts else (self._orig_desc or 'Ny konsekvens')
        cons = self.db.get_consequence(self.cons_id)
        if cons:
            self.db.update_consequence(
                self.cons_id, full,
                cons['severity'] or 1,
                cons['category'] or '',
                cons['consequence_chain'] or '')




class ReductionFactorsDialog(QDialog):
    """Edit the list of extra reduction factors for a consequence."""

    def __init__(self, db, consequence_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.consequence_id = consequence_id
        self.setWindowTitle("Övriga reduktionsfaktorer")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Lägg till faktorer som reducerar slutkonsekvensfrekvensen:"))

        self._tbl = QTableWidget(0, 3)
        self._tbl.setHorizontalHeaderLabels(['Beskrivning', 'RRF', ''])
        self._tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(1, 80); self._tbl.setColumnWidth(2, 64)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.cellChanged.connect(self._on_cell)
        layout.addWidget(self._tbl)

        add_btn = QPushButton("+ Lägg till faktor")
        add_btn.clicked.connect(self._add)
        layout.addWidget(add_btn)
        layout.addWidget(QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                          accepted=self.accept, rejected=self.accept))
        self._refresh()

    def _refresh(self):
        try: self._tbl.cellChanged.disconnect()
        except RuntimeError as e: logging.warning(f"Table cellChanged signal not connected: {e}")
        self._tbl.setRowCount(0)
        for rf in self.db.reduction_factors(self.consequence_id):
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            desc = QTableWidgetItem(rf['description'])
            desc.setData(Qt.ItemDataRole.UserRole, rf['id'])
            self._tbl.setItem(r, 0, desc)
            self._tbl.setItem(r, 1, QTableWidgetItem(str(rf['rrf'])))
            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(lambda _, rid=rf['id']: (
                self.db.delete_reduction_factor(rid), self._refresh()))
            self._tbl.setCellWidget(r, 2, del_btn)
            self._tbl.setRowHeight(r, 26)
        self._tbl.cellChanged.connect(self._on_cell)

    def _add(self):
        new_id = self.db.add_reduction_factor(self.consequence_id, 'Ny faktor', 10)
        self._refresh()

    def _on_cell(self, row, col):
        item = self._tbl.item(row, 0)
        if not item: return
        rf_id = item.data(Qt.ItemDataRole.UserRole)
        desc = self._tbl.item(row, 0).text() if self._tbl.item(row, 0) else ''
        try: rrf = int(self._tbl.item(row, 1).text()) if self._tbl.item(row, 1) else 10
        except ValueError: rrf = 10
        self.db.update_reduction_factor(rf_id, desc, rrf, 1)


class _ScenarioDelegate(QStyledItemDelegate):
    """Custom delegate: word-wrap for ORS/KON/SG cells; passes eventFilter to editors."""

    _WRAP_COLS = None   # set after panel constants are known

    def __init__(self, panel):
        super().__init__(panel)
        self._panel   = panel
        self._fm_font = None   # cached QFont
        self._fm      = None   # cached QFontMetrics — rebuilt only when font changes

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            editor.setProperty('editing_row', index.row())
            editor.setProperty('editing_col', index.column())
            editor.installEventFilter(self._panel)
        return editor

    def sizeHint(self, option, index):
        # Defensive hardening: sizeHint() is invoked for every visible cell
        # during resizeRowsToContents(), including — in theory — cells whose
        # backing _row_meta/_row_cat_info could be read mid-_build_rows() if
        # Qt ever triggers a repaint/layout pass reentrantly while rows are
        # still being constructed. A genuinely native (C++-level) crash here
        # cannot be caught by Python try/except, but if any part of this path
        # (QFontMetrics.boundingRect, index.data, attribute access on a
        # transient state) raises a Python-level exception instead, falling
        # back to a safe default size costs nothing and avoids compounding
        # a silent failure with an unhandled Python exception on top.
        try:
            return self._size_hint_impl(option, index)
        except Exception:
            logging.exception('_ScenarioDelegate.sizeHint: fallback after exception '
                              '(row=%d col=%d)', index.row(), index.column())
            return QSize(max(40, option.rect.width()), 24)

    def _size_hint_impl(self, option, index):
        col = index.column()
        panel = self._panel
        # Cache QFontMetrics — reconstructed only when the font changes
        if option.font != self._fm_font:
            self._fm_font = option.font
            self._fm = QFontMetrics(option.font)
        fm = self._fm
        one_line_h = fm.height() + 6

        wrap_cols = {panel._C_ORS, panel._C_KON, panel._C_REK}
        if col not in wrap_cols:
            if col == panel._C_SG:
                # SG's description never word-wraps (unlike ORS/KON) — a
                # single compact line is always enough. Uses its own
                # (smaller) row height, not one_line_h — see
                # panel._sg_row_height's docstring.
                return QSize(option.rect.width(), panel._sg_row_height(option.font))
            # Non-wrap columns (risk cells) stay at one compact line
            base = super().sizeHint(option, index)
            return QSize(base.width(), one_line_h)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ''
        if not text:
            return QSize(option.rect.width(), one_line_h)

        w = option.rect.width() if option.rect.width() > 0 else 200
        if col == panel._C_ORS:
            w = max(40, option.rect.width() - 6)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, text)
            return QSize(option.rect.width(),
                         _ORS_HEADER_H + max(one_line_h, rect.height() + 4))
        elif col == panel._C_KON:
            w -= _KON_CAT_W
            w = max(40, w)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, text)
            return QSize(option.rect.width(), max(one_line_h, rect.height() + 4))
        elif col == panel._C_SG:
            w -= _RRF_W
        w = max(40, w)
        rect = fm.boundingRect(0, 0, w, 10000,
                               Qt.TextFlag.TextWordWrap, text)
        return QSize(option.rect.width(), max(one_line_h, rect.height() + 4))

    def paint(self, painter, option, index):
        """RFORE/SLUT (this base delegate — ORS/KON/SG are handled by
        the _PidDelegate subclass installed for those specific columns,
        see ScenarioTablePanel.__init__) need their own custom paint:
        the app-wide QSS rule targeting QTableWidget::item (see CONFIG's
        global stylesheet, applied via app.setStyleSheet() in main())
        means Qt's default QStyledItemDelegate.paint() (super().paint(),
        used for every other column here — NOD/UTR/DEV/LOPA) stops
        respecting Qt::BackgroundRole/ForegroundRole once any stylesheet
        touches ::item, a well-known Qt quirk: stylesheet-driven item
        rendering only ever reads QSS rules, never the model's own
        background/foreground data. setBackground()/setForeground() on
        these items (set in _add_row/_update_lopa_risk) therefore had no
        visible effect in the real app — cells stayed white until
        selected (the QSS DOES define its own :selected background,
        which is why only THAT part ever showed); this was invisible to
        every earlier test because none of them ever applied the real
        app stylesheet before painting (2026-08-09, see NOTES.md).
        NOD/UTR/DEV/LOPA never set a custom background, so their default
        palette-driven look is unaffected and stays on the super().paint()
        path unchanged."""
        col = index.column()
        panel = self._panel
        if col not in (panel._C_RFORE, panel._C_SLUT):
            super().paint(painter, option, index)
            return
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        r = option.rect
        painter.save()
        if sel:
            painter.fillRect(r, option.palette.highlight())
        else:
            bg = index.data(Qt.ItemDataRole.BackgroundRole)
            painter.fillRect(r, bg if bg is not None else (
                option.palette.alternateBase() if index.row() % 2 == 1
                else option.palette.base()))
        if sel:
            tc = option.palette.highlightedText().color()
        else:
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            tc = fg.color() if fg is not None else option.palette.text().color()
        painter.setPen(tc)
        font = index.data(Qt.ItemDataRole.FontRole)
        painter.setFont(font if font is not None else option.font)
        text_rect = QRect(r.left() + _PID_ICON_W, r.top(),
                          r.width() - _PID_ICON_W, r.height())
        painter.drawText(text_rect.adjusted(2, 2, -2, -2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         index.data(Qt.ItemDataRole.DisplayRole) or '')
        painter.restore()


_PID_ICON_W  = 22          # pixels reserved on the left for the pin icon
_KON_CAT_W   = 26          # pixels for the category badge zone in KON cells
_ORS_COMMENT_W = 22        # 💬 comment icon zone (rightmost of ORS)
_ORS_CLONE_W   = 22        # 📋 clone-scenario icon zone
# Height of the ORS cell's top strip ([tag|comment dot], see _PidDelegate.
# paint()'s "Cause cells" branch). MUST match everywhere a row's needed
# height is computed (sizeHint/_resize_rows_manual/_wrap_col_row_height)
# AND everywhere the strip is actually drawn/the editor is positioned below
# it (paint()/updateEditorGeometry) — these used to disagree (14 vs 17px),
# which under-allocated 3px of vertical space for the wrapped description
# text below the strip on every ORS row, silently clipping its bottom few
# pixels (2026-08-11, bug report: "text göms på raderna ... spöktext ligger
# kvar när man redigerar" — the clipped-then-stale pixels from the
# undersized row explain both symptoms). See NOTES.md.
_ORS_STRIP_H = 17

# Frequency moved out of the tag strip above and into the orsaksfält
# itself (the description area) — right-aligned there instead (2026-08-18,
# see NOTES.md "Frekvensen ... hör hemma mer här" — every orsak has its
# own frequency, causes.likelihood/base_frequency, so it belongs with the
# cause's own content, not the shared object-identity strip above it). It
# is drawn OVERLAID on the description's own first line rather than in a
# separate reserved row — a dedicated row for it wasted a full extra line
# of height on every ORS cell for a value that's usually just a few
# characters (2026-08-18 follow-up: "hamnar nu på olika rader vilket tar
# onödigt mycket plats"). _ORS_HEADER_H is therefore just the tag strip's
# own height — kept as its own name (rather than switching call sites
# back to _ORS_STRIP_H directly) so "where does the description begin"
# stays a single, separately-named concept from "how tall is the tag
# strip", even though the two happen to be equal right now.
_ORS_HEADER_H = _ORS_STRIP_H

_RRF_W       = 54          # pixel width of the RRF badge column on the right of safeguard cells
_PLUS_BADGE_SIZE = 16      # pixel size of the in-cell "+" quick-add badge (bottom-right corner)

_PID_ICON_RE = re.compile(r'^[🟢📌]\s*')   # strip any old emoji prefix


def _draw_pid_pin(painter, rect, placed):
    """Draw a needle pin (circle + stick) inside rect. Green=placed, red=not placed."""
    color = QColor('#27ae60') if placed else QColor('#e74c3c')
    dark  = color.darker(150)

    r      = 4.5          # circle radius
    stick  = 5.0          # stick length below circle
    total  = r * 2 + stick

    cx  = float(rect.center().x())
    top = float(rect.center().y()) - total / 2.0

    circle_cy = top + r
    stick_top = top + r * 2
    stick_bot = top + total

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Stick
    pen = QPen(dark, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(cx, stick_top), QPointF(cx, stick_bot))

    # Circle head
    painter.setBrush(QBrush(color))
    painter.setPen(QPen(dark, 1.0))
    painter.drawEllipse(QPointF(cx, circle_cy), r, r)

    # White highlight dot
    dot_r = r * 0.3
    painter.setBrush(QBrush(QColor(255, 255, 255, 170)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(cx - r * 0.35, circle_cy - r * 0.35), dot_r, dot_r)
    painter.restore()


def _make_pin_icon(placed, size=16):
    """Return a QIcon with the needle pin rendered at the given size."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    _draw_pid_pin(p, QRect(0, 0, size, size), placed)
    p.end()
    return QIcon(px)


class _PidDelegate(_ScenarioDelegate):
    """Draws a P&ID placement icon on the left of Orsak/Konsekvens/Barriär cells.
    The editor always shows only the clean description (emoji stripped)."""

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            # Show only the clean description (EditRole is already clean)
            raw = index.data(Qt.ItemDataRole.EditRole) or ''
            clean = _PID_ICON_RE.sub('', str(raw))
            # A brand-new/cleared KON or SG cell displays a plain "—"
            # placeholder (2026-08-12, see NOTES.md) rather than literal
            # "Ny konsekvens"/"Ny safeguard" text — QTableWidgetItem has
            # no real Display-vs-EditRole divergence (setting one
            # overwrites what the other reads back), so the dash reaches
            # here too; start the editor blank instead of on top of it.
            if clean == '—':
                clean = ''
            editor.setText(clean)
            if index.column() == self._panel._C_ORS:
                self._attach_cause_completer(editor, index)
        return editor

    def _attach_cause_completer(self, editor, index):
        """Suggest standard-cause descriptions while inline-editing an Orsak
        cell — the same library StandardCausesPickerPopup/CauseObjectPopup
        draw from, so quick text edits get the same suggestions as the
        popups instead of a bare, unassisted QLineEdit.
        """
        db = getattr(self._panel, 'db', None)
        if db is None:
            return
        try:
            row = index.row()
            row_meta = getattr(self._panel, '_row_meta', [])
            dev_id = row_meta[row][0] if row < len(row_meta) else None
            item = self._panel._table.item(row, self._panel._C_ORS)
            obj_data = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
            comp_type = (obj_data or ('', ''))[0]

            descs = []
            if dev_id is not None:
                dev = db.get_deviation(dev_id)
                std_dev_id = _resolve_std_deviation_id(db, dev['description'] if dev else '')
                if std_dev_id is not None:
                    obj_id = None
                    if comp_type:
                        for o in db.standard_objects():
                            if _obj_type_matches(comp_type, o['name']):
                                obj_id = o['id']
                                break
                    if obj_id is not None:
                        descs = [c['description'] for c in
                                 db.standard_causes_for_object(std_dev_id, obj_id)]
            if not descs:
                descs = [r[0] for r in db.conn.execute(
                    "SELECT DISTINCT description FROM standard_causes").fetchall()]

            if descs:
                comp = QCompleter(descs, editor)
                comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                comp.setFilterMode(Qt.MatchFlag.MatchContains)
                editor.setCompleter(comp)
        except Exception:
            pass

    def setModelData(self, editor, model, index):
        clean = _PID_ICON_RE.sub('', editor.text().strip())
        model.setData(index, clean, Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        r = option.rect
        col = index.column()
        if col == self._panel._C_ORS:
            # Editor sits in the description area, below the tag strip
            # (the frequency label floats over the description's own
            # first line rather than reserving space of its own, so it
            # doesn't need to be excluded here — 2026-08-18, see NOTES.md
            # "Frekvensen ... hör hemma mer här").
            r = option.rect
            editor.setGeometry(QRect(r.left() + 2, r.top() + _ORS_HEADER_H,
                                     max(10, r.width() - 4),
                                     max(10, r.height() - _ORS_HEADER_H)))
            return
        elif col == self._panel._C_KON:
            offset = _KON_CAT_W
            editor.setGeometry(QRect(r.left() + offset, r.top(),
                                     max(10, r.width() - offset), r.height()))
            return
        elif col == self._panel._C_SG:
            # 2026-08-10 fix: this used to span the full remaining width,
            # visually covering the RRF badge (_RRF_W) while editing.
            editor.setGeometry(QRect(r.left(), r.top(),
                                     max(10, r.width() - _RRF_W),
                                     r.height()))
            return
        editor.setGeometry(r)

    def paint(self, painter, option, index):
        row, col = index.row(), index.column()
        sel = bool(option.state & QStyle.StateFlag.State_Selected)

        # ── Safeguard cells: side-by-side description | RRF badge ───────────
        if col == self._panel._C_SG:
            rrf = index.data(Qt.ItemDataRole.UserRole + 1)
            if rrf is not None:
                r = option.rect
                painter.save()
                # Background
                if sel:
                    painter.fillRect(r, option.palette.highlight())
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                body_top = r.top()
                body_h   = r.height()

                # Layout: [description ...][RRF badge 54px]
                desc_w    = r.width() - _RRF_W
                desc_rect = QRect(r.left(), body_top, desc_w, body_h)
                rrf_rect  = QRect(r.right() - _RRF_W, body_top, _RRF_W, body_h)

                # Description text (elided to one line), drag-appended tags
                # in bold (2026-08-09, see NOTES.md "fetmarkera objekttexten").
                # Same font size as every other cell — only the row's own
                # padding shrank (self._panel._sg_row_height), not the
                # text (2026-08-18 follow-up: Anton clarified it's the
                # CELL height that should shrink, not the text itself).
                desc = index.data(Qt.ItemDataRole.DisplayRole) or ''
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                tagged_refs = index.data(Qt.ItemDataRole.UserRole + 7) or []
                _draw_text_with_bold_tags(
                    painter, desc_rect.adjusted(2, 1, -2, -1), desc,
                    tagged_refs, option.font, tc, word_wrap=False)

                # RRF badge (right column)
                badge_bg = QColor('#2F5FD0') if sel else QColor('#F5F5F3')
                painter.fillRect(rrf_rect, badge_bg)
                badge_tc = QColor('#ffffff') if sel else QColor('#17191C')
                painter.setPen(badge_tc)
                badge_font = QFont(option.font)
                badge_font.setBold(True)
                painter.setFont(badge_font)
                # Just the number — "RRF" now lives in the column header
                # instead, so this single-line badge doesn't force the
                # row taller than the ORS/description content needs
                # (2026-08-14, see NOTES.md).
                painter.drawText(rrf_rect.adjusted(2, 0, -2, 0),
                                 Qt.AlignmentFlag.AlignCenter,
                                 f"{rrf}")

                # Separator line between description and badge
                painter.setPen(QPen(QColor('#bcd'), 1))
                painter.drawLine(rrf_rect.left(), r.top(), rrf_rect.left(), r.bottom())

                # Amber ○ indicator when safeguard excluded from any
                # category — muted to match the app's near-monochrome
                # theme (2026-08-09, see NOTES.md) instead of a pure,
                # saturated gold that stood out against everything else.
                excl_cats = index.data(Qt.ItemDataRole.UserRole + 2) or []
                if excl_cats:
                    csz = 9
                    circ = QRect(rrf_rect.right() - csz - 2,
                                 rrf_rect.top() + 2, csz, csz)
                    painter.setBrush(QBrush(QColor('#F5C97A')))
                    painter.setPen(QPen(QColor('#B8860B'), 1))
                    painter.drawEllipse(circ)
                    painter.setBrush(Qt.BrushStyle.NoBrush)

                self._draw_plus_badge(painter, r, row, col)
                painter.restore()
                return

        # ── Cause cells: tag strip + frequency row + description below ────────
        if col == self._panel._C_ORS:
            obj_data = index.data(Qt.ItemDataRole.UserRole + 2)
            if obj_data is not None:
                comp_type, comp_tag = obj_data
                # Hidden when this row's object is the SAME one as the
                # immediately preceding cause row's (UserRole+8, set in
                # _build_rows — see its own comment) — repeating the same
                # tag banner down a run of consecutive deviations for one
                # object wastes space (2026-08-18 follow-up: "om det visas
                # flera avikelser efter varandra som tillhör samma
                # objekttagg behöver denna inte repeteras"). This replaced
                # an earlier, too-broad rule that hid the tag whenever the
                # Utrustning column was merely VISIBLE — that hid it even
                # on views (e.g. "click an object's marker on P&ID") where
                # EVERY row shares one tag, so it never showed at all
                # ("Orsaken har tidigare visat objekt-tagen i bannern men
                # denna är nu borttagen"). Consecutive-repeat dedup still
                # covers the original same-row-as-Utrustning-column case,
                # since a group sharing one Utrustning value is by
                # definition a run of consecutive same-tag rows.
                repeats_previous = bool(index.data(Qt.ItemDataRole.UserRole + 8))
                has_tag = bool(comp_tag or comp_type) and not repeats_previous

                meta_      = self._panel._row_meta
                _cause_id  = meta_[row][1] if row < len(meta_) else None
                _has_comment = False
                if _cause_id:
                    try:
                        c = self._panel.db.get_cause_comment(_cause_id)
                        _has_comment = bool(c and c.strip())
                    except Exception:
                        pass

                r = option.rect
                painter.save()

                # Cell background
                if sel:
                    painter.fillRect(r, option.palette.highlight())
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                # ── Vertical split: tag strip, description (frequency floats
                # over the description's own first line — see below) ───────
                _SH = _ORS_STRIP_H
                strip_rect = QRect(r.left(), r.top(), r.width(), _SH)
                desc_rect = QRect(r.left() + 2, r.top() + _ORS_HEADER_H,
                                   r.width() - 4, max(0, r.height() - _ORS_HEADER_H))

                # Strip background — the frequency row below it is
                # deliberately left unfilled (just the cell's own
                # background), reading as part of the orsak's own content
                # rather than the shared object-identity strip above it.
                if not sel:
                    painter.fillRect(strip_rect, QColor('#F5F5F3'))
                else:
                    painter.fillRect(strip_rect,
                                     option.palette.highlight().color().darker(110))

                # Separator line between strip and the rest of the cell
                painter.setPen(QPen(QColor('#bcd'), 1))
                painter.drawLine(r.left(), r.top() + _SH, r.right(), r.top() + _SH)

                # ── Tag zone geometry (shared with the click hit-test in
                # eventFilter() via _ors_tag_zone_width — see its docstring).
                # Spans nearly the full strip now that frequency no longer
                # shares it (2026-08-18) — only the comment dot still needs
                # room at the right edge.
                tag_x = r.left()
                tag_zone_w = self._panel._ors_tag_zone_width(tag_x, r.right())

                # ── Tag number (bold, left-aligned)
                tag_label = comp_tag or ''
                tf = QFont(option.font)
                tf.setPointSize(max(6, option.font.pointSize() - 1))
                tf.setBold(True)
                painter.setFont(tf)
                tfm = painter.fontMetrics()
                if has_tag:
                    tag_w = min(tfm.horizontalAdvance(tag_label) + 6, tag_zone_w)
                    tag_draw_rect = QRect(tag_x, r.top(), tag_w, _SH)
                    tag_tc = (option.palette.highlightedText().color() if sel
                              else QColor('#17191C'))
                    painter.setPen(tag_tc)
                    painter.drawText(tag_draw_rect.adjusted(2, 0, -1, 0),
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                     tfm.elidedText(tag_label,
                                                    Qt.TextElideMode.ElideRight,
                                                    tag_draw_rect.width() - 3))

                # ── Comment dot (right of strip) — the green/yellow/orange/
                # red fill-status dot that used to sit next to it is gone
                # entirely (2026-08-18, see NOTES.md "skrota pluppen").
                dot_r = 4
                dot_y = r.top() + _SH // 2
                dot_x = r.right() - 5
                if _has_comment:
                    painter.setBrush(QBrush(QColor('#17191C')))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(QRect(dot_x - dot_r, dot_y - dot_r,
                                              dot_r * 2, dot_r * 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)

                # ── Description text (full cell width — frequency floats
                # over its own first line, drawn after so it stays on top) ──
                desc = index.data(Qt.ItemDataRole.DisplayRole) or ''
                painter.setFont(option.font)
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                painter.setPen(tc)
                painter.drawText(desc_rect.adjusted(0, 1, 0, -1),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop |
                                 Qt.TextFlag.TextWordWrap, desc)

                # ── Frequency — shares the description's own first line
                # instead of a separate reserved row ("längst ut till
                # höger"): a dedicated row wasted a full extra line of
                # height on every ORS cell for a value that's usually just
                # a few characters (2026-08-18 follow-up: "hamnar nu på
                # olika rader vilket tar onödigt mycket plats"). Painted
                # AFTER the description and backed by an opaque patch
                # matching the cell's own background, so it reads as a
                # small floating label — if the description's first line
                # is long enough to reach under it, that corner is covered
                # rather than visually colliding with it.
                freq_zone_x, freq_zone_w, freq_str = \
                    self._panel._ors_freq_zone_geometry(index, desc_rect.left(), desc_rect.right())
                if freq_str is not None:
                    ff = QFont(option.font)
                    ff.setPointSize(max(6, option.font.pointSize() - 1))
                    ffm = QFontMetrics(ff)
                    chip_h = ffm.height() + 2
                    chip_rect = QRect(freq_zone_x, desc_rect.top(), freq_zone_w, chip_h)
                    chip_bg = (option.palette.highlight() if sel else
                              (option.palette.alternateBase() if row % 2 == 1
                               else option.palette.base()))
                    painter.fillRect(chip_rect, chip_bg)
                    painter.setFont(ff)
                    f_tc = (option.palette.highlightedText().color() if sel
                            else QColor('#17191C'))
                    painter.setPen(f_tc)
                    painter.drawText(chip_rect.adjusted(0, 0, -3, 0),
                                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                     ffm.elidedText(freq_str, Qt.TextElideMode.ElideRight,
                                                    freq_zone_w - 3))

                self._draw_plus_badge(painter, r, row, col)
                painter.restore()
                return

        # ── Consequence cells: [cat-badge][description] ────────────────────────
        if col == self._panel._C_KON:
            con_data = index.data(Qt.ItemDataRole.UserRole)
            if con_data and con_data[0] == 'consequence':
                r = option.rect
                painter.save()
                if sel:
                    painter.fillRect(r, option.palette.highlight())
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                body_top = r.top()
                body_h   = r.height()

                cat_rect   = QRect(r.left(), body_top, _KON_CAT_W, body_h)
                txt_rect   = QRect(r.left() + _KON_CAT_W, body_top,
                                   r.width() - _KON_CAT_W, body_h)

                # Category badges — stacked vertically, one per category
                n_cats      = index.data(Qt.ItemDataRole.UserRole + 4) or 0
                all_cats    = index.data(Qt.ItemDataRole.UserRole + 5) or []
                if all_cats:
                    n         = len(all_cats)
                    badge_h   = max(14, cat_rect.height() // n)
                    cf = QFont(option.font)
                    cf.setPointSize(max(6, option.font.pointSize() - 2))
                    cf.setBold(True)
                    painter.setFont(cf)
                    for i, (cat_id, sev_id, cat_name, cat_sev) in enumerate(all_cats):
                        badge = QRect(cat_rect.left() + 2,
                                      cat_rect.top() + i * badge_h,
                                      cat_rect.width() - 4,
                                      badge_h - 1)
                        badge_tc = (option.palette.highlightedText().color() if sel
                                    else option.palette.text().color())
                        painter.setPen(badge_tc)
                        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter,
                                         f"{cat_name[:3]} {cons_axis_label(cat_sev)}")
                elif n_cats > 0:
                    painter.setPen(QColor('#17191C'))
                    f2 = QFont(option.font)
                    f2.setPointSize(max(6, option.font.pointSize() - 1))
                    painter.setFont(f2)
                    painter.drawText(cat_rect, Qt.AlignmentFlag.AlignCenter, f"📊{n_cats}")
                # else: no category assessment yet — leave the zone blank
                # rather than showing a muted "📊" placeholder on every
                # single uncategorized row (2026-08-10, see NOTES.md
                # "det känns lite plottrigt"; reduce chrome for unused
                # features instead of always reserving visual weight for
                # them).

                painter.setPen(QPen(QColor('#E2E3E1'), 1))
                painter.drawLine(cat_rect.right(), r.top(), cat_rect.right(), r.bottom())

                # Description text — word-wrapped, drag-appended tags in
                # bold (2026-08-09, see NOTES.md "fetmarkera objekttexten")
                display = index.data(Qt.ItemDataRole.DisplayRole) or ''
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                tagged_refs = index.data(Qt.ItemDataRole.UserRole + 8) or []
                _draw_text_with_bold_tags(
                    painter, txt_rect.adjusted(2, 2, -2, -2), display,
                    tagged_refs, option.font, tc, word_wrap=True)

                self._draw_plus_badge(painter, r, row, col)
                painter.restore()
                return

        # ── Default: delegate straight to the base description painting ────────
        super().paint(painter, option, index)

    def _draw_plus_badge(self, painter, rect, row, col):
        """Small "+" in a cell's bottom-right corner, offering to add
        another cause/consequence/safeguard right where you're already
        looking — reported feedback (2026-08-12, see NOTES.md): a
        dedicated "+" ROW "tar upp alldeles för mycket plats då de tar
        hela rader med blankt" (takes up way too much space with whole
        blank rows); this only ever marks the LAST real row of a group,
        drawn on top of that row's own real content instead of a
        separate row. Hit-tested by eventFilter()'s _PLUS_BADGE_SIZE
        zone, not by this delegate — painting and hit-testing share
        nothing but the corner geometry, matching every other in-cell
        zone in this class (tag zone, category badge, RRF badge, …)."""
        if self._panel._row_plus_cols.get(row, {}).get(col) is None:
            return
        sz = _PLUS_BADGE_SIZE
        badge = QRect(rect.right() - sz - 2, rect.bottom() - sz - 2, sz, sz)
        painter.setPen(QColor('#8D9299'))
        f = QFont(painter.font())
        f.setBold(True)
        f.setPointSize(max(7, painter.font().pointSize()))
        painter.setFont(f)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, '+')


class SgRRFCategoryPopup(QDialog):
    """Popup: change a safeguard's RRF, type, per-category and per-cause exclusions."""

    def __init__(self, db, sg_id, current_rrf, current_sg_type,
                 sev_cat_list, cause_list=None, parent=None):
        super().__init__(parent)
        self.db              = db
        self._sg_id          = sg_id
        self._current_rrf    = current_rrf
        self._current_type   = current_sg_type or 'Övrigt'
        self._sev_cat_list   = sev_cat_list    # [(sev_id, cat_name), ...]
        self._cause_list     = cause_list or [] # [(cause_id, desc, is_chain), ...]
        self._cat_checks:   dict[int, QCheckBox] = {}
        self._cause_checks: dict[int, QCheckBox] = {}
        self.setWindowTitle("Barriär — RRF & tillämpning")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._build()

    def _build(self):
        excl_by_sev   = {sev_id: self._sg_id in self.db.get_severity_excluded_sgs(sev_id)
                         for sev_id, _ in self._sev_cat_list}
        excl_cause_ids = self.db.get_safeguard_excluded_causes(self._sg_id)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(5)

        title = QLabel("RRF & tillämpning")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        # Type selector
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Typ:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(SG_TYPES)
        idx = self._type_combo.findText(self._current_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._type_combo.setStyleSheet("font-size:10px;")
        type_row.addWidget(self._type_combo)
        outer.addLayout(type_row)

        # RRF preset buttons + custom spinbox
        rrf_lbl = QLabel("RRF:")
        rrf_lbl.setStyleSheet("font-size:9px; color:#666;")
        outer.addWidget(rrf_lbl)
        presets = QHBoxLayout()
        for v in (1, 10, 100, 1000, 10000):
            btn = QPushButton(str(v))
            btn.setFixedWidth(52)
            btn.setStyleSheet(
                "QPushButton{background:#2F5FD0;color:white;border:none;"
                "border-radius:3px;padding:3px;font-weight:bold;font-size:9px;}"
                "QPushButton:hover{background:#3D6BD8;}")
            btn.clicked.connect(lambda _, v=v: self._spin.setValue(v))
            presets.addWidget(btn)
        outer.addLayout(presets)
        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("Eget:"))
        self._spin = QSpinBox()
        self._spin.setRange(1, 1_000_000)
        self._spin.setValue(self._current_rrf)
        self._spin.setStyleSheet("font-size:10px;")
        spin_row.addWidget(self._spin)
        outer.addLayout(spin_row)

        if self._sev_cat_list:
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep)
            lbl = QLabel("Gäller ej för kategori:")
            lbl.setStyleSheet("font-size:9px; color:#666;")
            outer.addWidget(lbl)
            for sev_id, cat_name in self._sev_cat_list:
                cb = QCheckBox(f"{cat_name}")
                cb.setStyleSheet("font-size:10px;")
                cb.setChecked(excl_by_sev.get(sev_id, False))
                self._cat_checks[sev_id] = cb
                outer.addWidget(cb)

        if self._cause_list:
            sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
            sep3.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep3)
            lbl2 = QLabel("Gäller ej för orsak:")
            lbl2.setStyleSheet("font-size:9px; color:#666;")
            outer.addWidget(lbl2)
            for cause_id, desc, is_chain in self._cause_list:
                prefix = "⛓ " if is_chain else "⚙ "
                label  = f"{prefix}{desc[:40]}"
                cb = QCheckBox(label)
                cb.setStyleSheet("font-size:10px;")
                cb.setChecked(cause_id in excl_cause_ids)
                self._cause_checks[cause_id] = cb
                outer.addWidget(cb)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep2)

        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#2F5FD0;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#3D6BD8;}")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setStyleSheet("font-size:10px; padding:2px 8px;")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch(); btn_row.addWidget(cancel); btn_row.addWidget(ok)
        outer.addLayout(btn_row)

    def _ok(self):
        new_rrf  = self._spin.value()
        new_type = self._type_combo.currentText()
        if new_rrf != self._current_rrf or new_type != self._current_type:
            self.db.update_safeguard(self._sg_id, rrf=new_rrf, sg_type=new_type)

        # Save category exclusions
        for sev_id, cb in self._cat_checks.items():
            excl_set = self.db.get_severity_excluded_sgs(sev_id)
            if cb.isChecked():
                excl_set.add(self._sg_id)
            else:
                excl_set.discard(self._sg_id)
            self.db.set_severity_excluded_sgs(sev_id, excl_set)

        # Save cause exclusions
        excl_cause_ids = {cid for cid, cb in self._cause_checks.items() if cb.isChecked()}
        self.db.set_safeguard_excluded_causes(self._sg_id, excl_cause_ids)

        self.accept()


class CatSGSelectionPopup(QDialog):
    """Popup: select which safeguards apply for a category-row (gäller ej för)."""

    def __init__(self, db, severity_id, all_sgs, parent=None):
        super().__init__(parent)
        self.db = db
        self._sev_id = severity_id
        self._all_sgs = all_sgs
        self.setWindowTitle("Barriärer — gäller ej för")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._checks: dict[int, QCheckBox] = {}
        self._build()

    def _build(self):
        excluded = self.db.get_severity_excluded_sgs(self._sev_id)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        title = QLabel("Barriärer för detta scenario")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        note = QLabel("Avmarkera 'Gäller ej' för barriärer som inte gäller denna kategori.")
        note.setStyleSheet("font-size:9px; color:#666;")
        note.setWordWrap(True)
        outer.addWidget(note)

        if not self._all_sgs:
            outer.addWidget(QLabel("Inga barriärer tillagda ännu."))
        for sg in self._all_sgs:
            sg_id = sg['id']
            rrf   = sg.get('rrf', 1) or 1
            cb = QCheckBox(f"{sg['description']}  (RRF {rrf})")
            cb.setStyleSheet("font-size:10px;")
            cb.setChecked(sg_id not in excluded)
            self._checks[sg_id] = cb
            outer.addWidget(cb)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep)

        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#2F5FD0;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#3D6BD8;}")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setStyleSheet("font-size:10px; padding:2px 8px;")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch(); btn_row.addWidget(cancel); btn_row.addWidget(ok)
        outer.addLayout(btn_row)

    def _ok(self):
        excluded = [sg_id for sg_id, cb in self._checks.items() if not cb.isChecked()]
        self.db.set_severity_excluded_sgs(self._sev_id, excluded)
        self.accept()


class ConsCategoryMatrixPopup(QDialog):
    """Small popup: select severity per consequence category, one row per category."""

    def __init__(self, db: 'Database', consequence_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self._cons_id = consequence_id
        self.setWindowTitle("Konsekvens per kategori")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._sel: dict[int, int] = {}
        self._buttons: dict[tuple, QPushButton] = {}
        self._build()

    def _build(self):
        cats  = [dict(r) for r in self.db.consequence_categories()]
        saved = {r['category_id']: r['severity']
                 for r in self.db.get_consequence_severities(self._cons_id)}
        mat   = self.db.get_risk_matrix() or DEFAULT_MATRIX
        n_sev = mat.get('n_consequences', 5)

        self._sel = {c['id']: saved.get(c['id'], 0) for c in cats}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        title = QLabel("Konsekvens per kategori")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        # Header: severity labels
        hdr = QHBoxLayout(); hdr.setSpacing(2)
        pad = QLabel(); pad.setFixedWidth(88)
        hdr.addWidget(pad)
        for s in range(1, n_sev + 1):
            lbl = QLabel(cons_axis_label(s))
            lbl.setFixedWidth(42)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-size:9px; color:#555;")
            hdr.addWidget(lbl)
        outer.addLayout(hdr)

        # One row per category
        for cat in cats:
            cid = cat['id']
            row_l = QHBoxLayout(); row_l.setSpacing(2); row_l.setContentsMargins(0,0,0,0)
            name_l = QLabel(cat['name'])
            name_l.setFixedWidth(88)
            name_l.setStyleSheet("font-size:10px;")
            row_l.addWidget(name_l)
            for s in range(1, n_sev + 1):
                btn = QPushButton()
                btn.setFixedSize(42, 22)
                btn.setCheckable(True)
                btn.setChecked(self._sel.get(cid, 0) == s)
                btn.setStyleSheet(self._bstyle(btn.isChecked()))
                btn.clicked.connect(lambda _, ci=cid, sv=s: self._toggle(ci, sv))
                self._buttons[(cid, s)] = btn
                row_l.addWidget(btn)
            outer.addLayout(row_l)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep)

        btn_row = QHBoxLayout()
        clr = QPushButton("Rensa alla")
        clr.setStyleSheet("font-size:10px; padding:2px 8px;")
        clr.clicked.connect(self._clear)
        ok  = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#2F5FD0;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#3D6BD8;}")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setStyleSheet("font-size:10px; padding:2px 8px;")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(clr); btn_row.addStretch()
        btn_row.addWidget(cancel); btn_row.addWidget(ok)
        outer.addLayout(btn_row)

    @staticmethod
    def _bstyle(selected: bool) -> str:
        if selected:
            return ("QPushButton{background:#2F5FD0;color:white;"
                    "border:2px solid #2F5FD0;border-radius:3px;"
                    "font-size:9px;font-weight:bold;}"
                    "QPushButton:hover{background:#3D6BD8;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;"
                "border:1px solid #CFD1CE;border-radius:3px;font-size:9px;}"
                "QPushButton:hover{background:#E8E9E6;border:1px solid #B3B7B2;}")

    def _toggle(self, cat_id: int, sev: int):
        self._sel[cat_id] = 0 if self._sel.get(cat_id) == sev else sev
        self._refresh()

    def _clear(self):
        self._sel = {k: 0 for k in self._sel}
        self._refresh()

    def _refresh(self):
        for (cid, s), btn in self._buttons.items():
            selected = self._sel.get(cid, 0) == s
            btn.setChecked(selected)
            btn.setStyleSheet(self._bstyle(selected))

    def _ok(self):
        for cat_id, sev in self._sel.items():
            self.db.set_consequence_severity(self._cons_id, cat_id, sev)
        self.accept()


class _LopaWidget(QWidget):
    """Compact stacked FA / Antändning / Övriga cell widget for ScenarioTablePanel."""

    changed = pyqtSignal(int)   # emits cons_id after any save

    def __init__(self, db: 'Database', cons_id: int,
                 fa_active: bool, fa_rrf,
                 ign_active: bool, ign_rrf,
                 n_extra: int, parent=None):
        super().__init__(parent)
        self.db       = db
        self.cons_id  = cons_id
        self._saving  = False

        _ROW_H = 16   # fixed height per mini-row — keeps widget compact

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(0)

        _cb_ss  = "font-size:8pt;"
        _ed_ss  = "font-size:8pt; padding:0px 1px;"
        _btn_ss = "font-size:7pt; text-align:left; padding:0px 2px; border:none;"

        def _pct_edit(val):
            e = QLineEdit(str(val))
            e.setMaximumWidth(36); e.setMinimumWidth(28)
            e.setFixedHeight(_ROW_H)
            e.setAlignment(Qt.AlignmentFlag.AlignRight)
            e.setStyleSheet(_ed_ss)
            return e

        def _cb(label, checked, tip):
            c = QCheckBox(label)
            c.setChecked(checked)
            c.setToolTip(tip)
            c.setFixedHeight(_ROW_H)
            c.setStyleSheet(_cb_ss)
            return c

        # FA row
        fa_row = QHBoxLayout()
        fa_row.setContentsMargins(0, 0, 0, 0); fa_row.setSpacing(2)
        self._fa_cb   = _cb("FA", bool(fa_active), "Närvaro/FA-sannolikhet\n10%=−1 steg")
        self._fa_edit = _pct_edit(fa_rrf)
        self._fa_edit.setToolTip("Sannolikhet i % (t.ex. 10 eller 1)")
        fa_pct = QLabel("%"); fa_pct.setFixedWidth(10); fa_pct.setStyleSheet(_cb_ss)
        fa_row.addWidget(self._fa_cb); fa_row.addStretch()
        fa_row.addWidget(self._fa_edit); fa_row.addWidget(fa_pct)
        lay.addLayout(fa_row)

        # Antändning row
        ign_row = QHBoxLayout()
        ign_row.setContentsMargins(0, 0, 0, 0); ign_row.setSpacing(2)
        self._ign_cb   = _cb("Ant.", bool(ign_active), "Antändningssannolikhet\n10%=−1 steg")
        self._ign_edit = _pct_edit(ign_rrf)
        self._ign_edit.setToolTip("Sannolikhet i % (t.ex. 10 eller 1)")
        ign_pct = QLabel("%"); ign_pct.setFixedWidth(10); ign_pct.setStyleSheet(_cb_ss)
        ign_row.addWidget(self._ign_cb); ign_row.addStretch()
        ign_row.addWidget(self._ign_edit); ign_row.addWidget(ign_pct)
        lay.addLayout(ign_row)

        # Övriga faktorer button
        self._extra_btn = QPushButton(
            f"+{n_extra} övr." if n_extra else "+ övriga")
        self._extra_btn.setFlat(True)
        self._extra_btn.setFixedHeight(_ROW_H)
        self._extra_btn.setStyleSheet(_btn_ss)
        lay.addWidget(self._extra_btn)

        self.setFixedHeight(_ROW_H * 3 + 2)

        self._fa_cb.toggled.connect(self._save)
        self._fa_edit.editingFinished.connect(self._save)
        self._ign_cb.toggled.connect(self._save)
        self._ign_edit.editingFinished.connect(self._save)

    def update_extra_count(self, n: int):
        self._extra_btn.setText(f"+ {n} övr." if n else "+ övriga")

    def _parse_pct(self, edit: 'QLineEdit') -> float:
        try:
            v = float(edit.text().replace('%', '').strip() or '10')
            return max(0.001, min(99.9, v))
        except ValueError:
            return 10.0

    def _save(self):
        if self._saving:
            return
        self._saving = True
        try:
            self.db.update_consequence_factors(
                self.cons_id,
                self._fa_cb.isChecked(),  self._parse_pct(self._fa_edit),
                self._ign_cb.isChecked(), self._parse_pct(self._ign_edit))
            self.changed.emit(self.cons_id)
        finally:
            self._saving = False


class ScenarioTablePanel(QWidget):
    """Extended scenario table with FA, Antändning, Övriga faktorer and Slutkonsekvens."""

    item_selected              = pyqtSignal(int, int)   # (type_, id_) — cell clicked → open right panel
    new_item_created           = pyqtSignal(int, int)   # (type_, id_) — after quick-add via Enter menu
    item_edited                = pyqtSignal(int, int)   # (type_, id_) — cell edit committed → sync right panel
    structure_changed          = pyqtSignal()           # item moved/deleted/duplicated → refresh tree
    equipment_renamed          = pyqtSignal()           # an ORS tag edit renamed the linked equipment_catalog row

    # Column indices
    _C_NOD, _C_UTR, _C_DEV, _C_ORS, _C_KON, _C_RFORE = 0, 1, 2, 3, 4, 5
    _C_SG, _C_LOPA, _C_SLUT, _C_REK                   = 6, 7, 8, 9

    _COLS = [
        'Nod',
        'Utrustning',
        'Avvikelse',
        'Orsak',
        'Konsekvens',
        'Risk före barriär',
        'Barriärer (RRF)',
        'Enablers',
        'Slutkonsekvens',
        'Rekommendation',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
        self._node_id = None
        self._deviation_id = None
        self._all_nodes = False  # if True, show every node's full hierarchy (set by load_all)
        self._equipment_filter_id = None  # if set, show only causes mentioning this equipment_catalog id (set by load_equipment)
        self._show_empty_deviations = False  # if True, deviations with zero causes get a placeholder row instead of being omitted
        self._force_dev_column_visible = False  # if True, Avvikelse column stays visible regardless of _all_nodes (set by always_show_deviation_column)
        self._force_utr_column_hidden = False  # if True, Utrustning column stays hidden regardless of _all_nodes (set by hide_equipment_column)
        self._row_meta = []   # list of (dev_id, cause_id, cons_id, sg_id) per visible row
        # row index -> {col: ('cause', dev_id) | ('consequence', cause_id) |
        # ('safeguard', cons_id)} — marks the LAST real row of a group that
        # already has content with a small in-cell "+" quick-add badge in
        # the given column's bottom-right corner (2026-08-12, see NOTES.md
        # — a dedicated "+" ROW took up too much space with whole blank
        # rows). A group with zero content still invites Enter-to-add on
        # its existing placeholder row instead (unchanged) — no badge
        # needed there since that row's own cell is already the target.
        self._row_plus_cols = {}
        self._cons_id  = None  # if set, show only this consequence (set by load_consequence)
        self._enter_row = -1
        self._enter_col = -1
        self._last_enter_committed = False
        self._cell_font_size = 9
        self._cause_obj_w = int(self.db.get_config('cause_obj_w', '64'))
        self._drag_obj_w_active = False
        self._drag_obj_w_start_x = 0
        self._drag_obj_w_start_w = 0
        # Parallel list to _row_meta: None or (cat_id, cat_name, cat_sev)
        self._row_cat_info: list = []
        self.setMinimumHeight(CONFIG['H_TABLE_STD'])
        # 380px cap fits the P&ID page's bottom-splitter usage, where this panel
        # shares vertical space with the canvas above it. A full-page host
        # (e.g. HAZOPWorksheet) should call allow_full_height() to lift this cap.
        self.setMaximumHeight(380)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        hdr_row = QHBoxLayout()
        self._hdr_lbl = QLabel("HAZOP Scenario")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        self._hdr_lbl.setFont(f)
        hdr_row.addWidget(self._hdr_lbl)
        hdr_row.addStretch()
        self._fill_btn = QPushButton("Fyll bredd")
        self._fill_btn.setIcon(_icon('resize-horizontal'))
        self._fill_btn.setToolTip(
            "Fördela om Orsak/Konsekvens/Barriärer-kolumnerna så de fyller "
            "hela bredden just nu — kolumnerna går alltid att dra i")
        self._fill_btn.clicked.connect(self._fill_width_once)
        hdr_row.addWidget(self._fill_btn)
        hdr_row.addSpacing(8)
        hdr_row.addWidget(QLabel("Textstorlek:"))
        self._fs_spin = QSpinBox()
        self._fs_spin.setRange(7, 16)
        self._fs_spin.setValue(9)
        self._fs_spin.setSuffix(" pt")
        self._fs_spin.setFixedWidth(62)
        self._fs_spin.setToolTip("Teckenstorlek i scenario-tabellen")
        self._fs_spin.valueChanged.connect(self._on_font_size_changed)
        hdr_row.addWidget(self._fs_spin)
        outer.addLayout(hdr_row)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        h = self._table.horizontalHeader()
        # NOD, UTR and DEV are hidden — context is shown in the header label instead
        self._table.setColumnHidden(self._C_NOD, True)
        self._table.setColumnHidden(self._C_UTR, True)
        self._table.setColumnHidden(self._C_DEV, True)
        resize_modes = {
            self._C_NOD:   (QHeaderView.ResizeMode.Interactive,  70),
            self._C_UTR:   (QHeaderView.ResizeMode.Interactive, 110),
            self._C_DEV:   (QHeaderView.ResizeMode.Interactive, 120),
            self._C_ORS:   (QHeaderView.ResizeMode.Interactive, 180),
            self._C_KON:   (QHeaderView.ResizeMode.Interactive, 180),
            self._C_RFORE: (QHeaderView.ResizeMode.Interactive,  85),
            self._C_SG:    (QHeaderView.ResizeMode.Interactive, 130),
            self._C_LOPA:  (QHeaderView.ResizeMode.Interactive, 130),
            self._C_SLUT:  (QHeaderView.ResizeMode.Interactive,  85),
            self._C_REK:   (QHeaderView.ResizeMode.Interactive, 140),
        }
        for col, (mode, width) in resize_modes.items():
            h.setSectionResizeMode(col, mode)
            self._table.setColumnWidth(col, width)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.verticalHeader().setVisible(False)
        # Compact row heights — category sub-rows should be tight
        _init_fm = QFontMetrics(self._table.font())
        _compact = _init_fm.height() + 4
        self._table.verticalHeader().setDefaultSectionSize(_compact)
        self._table.verticalHeader().setMinimumSectionSize(_compact)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:3px;}")
        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.itemDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setAcceptDrops(True)
        self._table.installEventFilter(self)
        self._table.viewport().installEventFilter(self)
        # Drag state
        self._drag_press_pos  = None
        self._drag_press_row  = -1
        self._drag_press_col  = -1
        self._delegate = _ScenarioDelegate(self)
        self._table.setItemDelegate(self._delegate)
        self._pid_delegate = _PidDelegate(self)
        for col in (self._C_ORS, self._C_KON, self._C_SG):
            self._table.setItemDelegateForColumn(col, self._pid_delegate)
        self._table.viewport().installEventFilter(self)

        # ── Persist manually-resized column widths (2026-08-10, see
        # NOTES.md) — previously reset to the hardcoded defaults every
        # time the app restarted. Columns are always Interactive (see
        # resize_modes above) so dragging works regardless of whether
        # "↔ Fyll bredd" has ever been clicked.
        saved_widths = self.db.get_config('scenario_col_widths', '')
        if saved_widths:
            try:
                for col_str, w in json.loads(saved_widths).items():
                    col = int(col_str)
                    if 0 <= col < self._table.columnCount():
                        self._table.setColumnWidth(col, w)
            except Exception:
                pass
        h.sectionResized.connect(self._on_column_resized)

        # ── Sticky context bar — always shows current Nod + Avvikelse ──────────
        self._ctx_bar = QLabel()
        self._ctx_bar.setStyleSheet(
            "QLabel { background:#F5F5F3; color:#17191C; font-size:10px;"
            " padding:3px 8px; border-bottom:1px solid #E2E3E1; }")
        self._ctx_bar.setWordWrap(False)
        self._ctx_bar.hide()   # hidden until content is loaded
        outer.addWidget(self._ctx_bar)
        outer.addWidget(self._table)

        self._table.verticalScrollBar().valueChanged.connect(
            self._update_ctx_bar)

        # ── Deferred rebuild system (signal-based, not timer-based) ──────────────
        self._rebuild_pending = False

    def allow_full_height(self):
        """Lift the 380px cap so this panel fills whatever container it's
        placed in — for hosts that give it a whole page (e.g. HAZOPWorksheet),
        as opposed to the P&ID page's bottom splitter (which relies on the cap
        to leave room for the canvas above it)."""
        self.setMaximumHeight(16777215)  # Qt's QWIDGETSIZE_MAX — effectively "no cap"
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_show_empty_deviations(self, show: bool):
        """Toggle whether deviations with zero causes get their own placeholder
        row (interleaved with deviations that do have causes), instead of being
        silently omitted from the single-node/all-nodes view."""
        if self._show_empty_deviations == show:
            return
        self._show_empty_deviations = show
        self._rebuild()

    # ── Load ──────────────────────────────────────────────────────────────────

    def load_node(self, node_id):
        self._node_id = node_id
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_deviation(self, deviation_id):
        dev = self.db.get_deviation(deviation_id)
        self._node_id = dev['node_id'] if dev else None
        self._deviation_id = deviation_id
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_cause(self, cause_id):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = cause_id
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_consequence(self, cons_id):
        row = self.db.get_consequence(cons_id)
        if row:
            self._node_id = None
            self._deviation_id = None
            self.cause_id = dict(row)['cause_id']
            self._cons_id = cons_id
            self._all_nodes = False
            self._equipment_filter_id = None
            self._set_all_nodes_columns_visible(False)
            self._rebuild()

    def load_all(self):
        """Show the entire study: every node's full deviation/cause/consequence/safeguard hierarchy."""
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = True
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(True)
        self._rebuild()

    def load_equipment(self, equipment_id):
        """Filter the scenario table to only the causes that mention this
        specific P&ID equipment object anywhere in their chain (deviation,
        cause, consequence or safeguard) — used when the user clicks a
        defined (red/green) equipment marker on the P&ID (2026-08-12, see
        NOTES.md: 'de orsaker som visas i hazop scenario är de där
        objektet finns med'). NOD/DEV/UTR columns are shown (same as "all
        nodes" mode) since the matching rows can span several deviations
        or even nodes."""
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = equipment_id
        self._set_all_nodes_columns_visible(True)
        self._rebuild()

    def refresh(self):
        """Rebuild in place — keeps the current filter unchanged."""
        self._rebuild()

    def clear(self):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = None
        self._show_empty_deviations = False
        self._set_all_nodes_columns_visible(False)
        self._table.setRowCount(0)
        self._hdr_lbl.setText("HAZOP Scenario")

    def _set_all_nodes_columns_visible(self, visible: bool):
        """NOD/UTR/DEV columns are normally hidden (context shown in the
        sticky header bar / _hdr_lbl instead). In "all nodes" mode multiple
        nodes and deviations are interleaved in one table, so those columns
        must become visible so rows remain identifiable.

        `self._force_dev_column_visible` (set via always_show_deviation_column())
        keeps only the Avvikelse column visible regardless of `visible` —
        for hosts like HAZOPWorksheet/the embedded P&ID scenario panel
        where deviation context should always be in the grid, not just in
        the sticky header bar. Utrustning does NOT follow that same force
        (reported feedback: it duplicated the tag already shown at the top
        of each Orsak cell) — it only appears in genuine "all nodes" mode,
        where it still earns its keep by disambiguating the several
        equipment groups a single interleaved view can span. `self.
        _force_utr_column_hidden` (set via hide_equipment_column()) is the
        opposite override — for a host that doesn't want Utrustning even
        in "all nodes" mode (2026-08-13, see NOTES.md: "i worksheet
        behöver inte objekt kolumnen synas")."""
        self._table.setColumnHidden(self._C_NOD, not visible)
        self._table.setColumnHidden(
            self._C_DEV, not (visible or self._force_dev_column_visible))
        self._table.setColumnHidden(
            self._C_UTR, self._force_utr_column_hidden or not visible)

    def always_show_deviation_column(self):
        """Keep the Avvikelse column visible at all times, regardless of
        "Visa samtliga noder" / "Visa avvikelser utan orsaker" state —
        opt-in for hosts (e.g. HAZOPWorksheet, the embedded P&ID scenario
        panel) that want deviation context always shown in the grid
        itself. Utrustning is unaffected — see _set_all_nodes_columns_visible."""
        self._force_dev_column_visible = True
        self._set_all_nodes_columns_visible(self._all_nodes)

    def hide_equipment_column(self):
        """Keep the Utrustning column hidden even in "all nodes" mode
        (2026-08-13, see NOTES.md) — opt-in for hosts (HAZOPWorksheet)
        that don't need it disambiguating equipment groups in the
        interleaved view; the tag is already shown at the top of each
        Orsak cell regardless."""
        self._force_utr_column_hidden = True
        self._set_all_nodes_columns_visible(self._all_nodes)

    # Columns that stretch to fill remaining space in fill mode
    _STRETCH_COLS = None  # set after class constants are known

    def _fill_width_once(self):
        """Redistribute ORS/KON/SG to fill the table's current width right
        now. Previously "Fyll skärm" was a persistent checkbox that locked
        those columns into Stretch mode (blocking manual dragging
        entirely) and locked RFORE/LOPA/SLUT into Fixed — unchecking it
        only changed the resize MODE, not any pixel width, so it looked
        like it "had no effect" (reported feedback). Columns are now
        always Interactive (see resize_modes in __init__), so dragging
        works regardless of whether this button has ever been clicked;
        this just gives it one immediate, visible effect instead of a
        silent mode flip."""
        stretch_cols = [self._C_ORS, self._C_KON, self._C_SG]
        other_cols = [c for c in range(self._table.columnCount())
                      if c not in stretch_cols and not self._table.isColumnHidden(c)]
        used = sum(self._table.columnWidth(c) for c in other_cols)
        available = max(0, self._table.viewport().width() - used)
        per_col = max(60, available // len(stretch_cols))
        for col in stretch_cols:
            self._table.setColumnWidth(col, per_col)

    def _on_column_resized(self, col, old_size, new_size):
        """Persist manually-resized column widths (2026-08-10, see
        NOTES.md) — only meaningful for Interactive columns ("Fyll skärm"
        unchecked), but harmless to also record Stretch/Fixed-driven
        resizes since they'd just re-save the same hardcoded defaults."""
        try:
            saved = self.db.get_config('scenario_col_widths', '')
            widths = json.loads(saved) if saved else {}
        except Exception:
            widths = {}
        widths[str(col)] = new_size
        self.db.set_config('scenario_col_widths', json.dumps(widths))

    def _on_font_size_changed(self, size):
        self._cell_font_size = size
        f = QFont()
        f.setPointSize(size)
        self._table.setFont(f)
        # Keep default section size at one-line height so resizeRowToContents
        # can shrink rows freely.  Row heights are set by resizeRowsToContents
        # at the end of _rebuild.
        fm = QFontMetrics(f)
        self._table.verticalHeader().setDefaultSectionSize(fm.height() + 6)
        self._table.verticalHeader().setMinimumSectionSize(fm.height() + 4)
        self._rebuild()

    # ── Build ─────────────────────────────────────────────────────────────────

    def rebuild(self):
        """Public entry point for a full, immediate table rebuild."""
        self._rebuild()

    def schedule_rebuild(self):
        """Public entry point for a deferred rebuild — see _schedule_rebuild."""
        self._schedule_rebuild()

    def get_equipment_filter(self):
        """The equipment_catalog id the table is currently filtered to, or None."""
        return self._equipment_filter_id

    def position_near_row(self, cons_id: int, popup_size):
        """Public wrapper — see _pos_near_cons_row."""
        return self._pos_near_cons_row(cons_id, popup_size)

    def _schedule_rebuild(self):
        """
        Schedule a deferred rebuild on the next event loop iteration.
        Prevents cascading timer-based deferred calls and ensures proper event ordering.
        Safe to call multiple times — queued as a single rebuild.
        """
        if self._rebuild_pending:
            return  # Already scheduled
        self._rebuild_pending = True
        QTimer.singleShot(0, self._on_rebuild_scheduled)

    def _on_rebuild_scheduled(self):
        """Called when rebuild is scheduled. Executes the deferred rebuild."""
        self._rebuild_pending = False
        self._rebuild()

    def _rebuild(self):
        """
        Orchestrate full table rebuild: clear, build rows, apply spans, resize, restore state.
        Re-entrancy safe via _rebuilding flag to prevent cascading rebuilds.
        """
        if getattr(self, '_rebuilding', False):
            return
        self._rebuilding = True
        try:
            # Save scroll position and suppress visual updates to prevent jumping
            _vscroll = self._table.verticalScrollBar().value()
            _hscroll = self._table.horizontalScrollBar().value()
            self._table.setUpdatesEnabled(False)
            try:
                self._table.cellChanged.disconnect()
            except Exception:
                pass
            self._table.blockSignals(True)
            try:
                self._table.clearSpans()
                # Proactively clear focus from any active cell editor (e.g. a
                # _LopaWidget QLineEdit) before tearing down rows. Destroying a
                # focused widget forces a synchronous focus-out, which would
                # fire editingFinished -> _save -> changed.emit -> _update_lopa_risk
                # reentrantly mid-teardown. The _rebuilding guard in
                # _update_lopa_risk covers this too, but avoiding the signal
                # firing at all is a cleaner first line of defense.
                focused = self._table.focusWidget()
                if focused is not None:
                    focused.clearFocus()
                logging.info('_rebuild: D — setRowCount(0)')
                self._table.setRowCount(0)
                logging.info('_rebuild: E — reset meta')
                self._row_meta = []
                self._row_cat_info = []
                self._row_plus_cols = {}

                # Build rows with signals blocked
                self._build_rows()
                logging.info('_rebuild: F — _build_rows() done (rowCount=%d)',
                             self._table.rowCount())

                # Reconnect signals before calling _apply_spans
                self._table.cellChanged.connect(self._on_cell_changed)

                # Apply row merging (spans)
                self._apply_spans()
                logging.info('_rebuild: G — _apply_spans() done')

                # Finalize: resize rows and restore scroll position
                self._resize_rows(_vscroll, _hscroll)
                logging.info('_rebuild: H — _resize_rows() done')
            finally:
                self._table.blockSignals(False)
        except Exception as e:
            logging.exception('_rebuild: Python exception')
            QMessageBox.critical(self, "Fel i scenariopanel", str(e))
        finally:
            self._rebuild_pending = False
            self._rebuilding = False
            self._update_ctx_bar()

    def _equipment_for_dev(self, dev_d):
        """(equipment_id, label) for a deviation dict's equipment_id, or
        (None, '') if it's not tied to a specific equipment — see NOTES.md
        "Nod → Utrustning → Avvikelse"."""
        eq_id = dev_d.get('equipment_id') if dev_d else None
        if not eq_id:
            return None, ''
        eq = self.db.get_equipment_by_id(eq_id)
        return eq_id, (f"{eq['tag']} — {eq['equipment_type']}" if eq else '')

    def _cause_tag_display(self, cause_d):
        """(comp_type, comp_tag) for the ORS tag strip — resolved LIVE
        from equipment_catalog via causes.equipment_id when the cause is
        linked to a real object (2026-08-13, see NOTES.md: "taggen är
        kopplad till objekten ... ändrar jag ... p&id" — renaming the
        object must show up here on the very next redraw, same live-FK
        pattern _equipment_for_dev above already uses for the Utrustning
        column), falling back to the frozen comp_type/comp_tag strings
        for a custom/unmatched tag (equipment_id is None) or if the
        linked row was since deleted."""
        eq_id = cause_d.get('equipment_id')
        if eq_id:
            eq = self.db.get_equipment_by_id(eq_id)
            if eq:
                return eq.get('equipment_type') or '', eq.get('tag') or ''
        return cause_d.get('comp_type') or '', cause_d.get('comp_tag') or ''

    def _causes_for_node(self, node_id):
        """Return [(cause_dict, deviation_dict), ...] for every cause under
        every deviation of node_id, in deviation/cause order. Shared by the
        single-node branch of _build_rows() and the "all nodes" mode (used
        once per node, in node order) so both walk the exact same hierarchy."""
        result = []
        for dev in self.db.deviations(node_id):
            dev_d = dict(dev)
            causes = list(self.db.causes_for_deviation(dev['id']))
            if not causes:
                if self._show_empty_deviations:
                    result.append((None, dev_d))  # sentinel: deviation has no causes
                continue
            for c in causes:
                result.append((dict(c), dev_d))
        return result

    def _causes_for_equipment(self, equipment_id):
        """Return [(cause_dict, deviation_dict), ...] for every cause that
        mentions this equipment anywhere in its chain — see
        Database.causes_for_equipment(). Used by the "click an equipment
        marker on P&ID" filter (load_equipment)."""
        result = []
        for c in self.db.causes_for_equipment(equipment_id):
            c_d = dict(c)
            dev = self.db.get_deviation(c_d.get('deviation_id'))
            dev_d = dict(dev) if dev else {'id': None, 'node_id': c_d.get('node_id'), 'description': '—'}
            result.append((c_d, dev_d))
        return result

    def _build_rows(self):
        """
        Build the scenario table rows from current filters (node, deviation, cause, consequence).
        Modifies self._table, self._row_meta, self._row_cat_info in place.
        Called with table signals blocked, so cellChanged won't fire during construction.
        """
        logging.info('_build_rows: F0 — entry (all_nodes=%s node_id=%s dev_id=%s '
                     'cause_id=%s cons_id=%s)',
                     self._all_nodes, self._node_id, self._deviation_id,
                     self.cause_id, self._cons_id)
        # Build list of (cause_dict, deviation_dict) to display
        causes_to_show = []
        if self._all_nodes:
            for node_row in self.db.nodes():
                causes_to_show.extend(self._causes_for_node(node_row['id']))
        elif self._equipment_filter_id is not None:
            causes_to_show.extend(self._causes_for_equipment(self._equipment_filter_id))
        elif self.cause_id is not None:
            c = self.db.get_cause(self.cause_id)
            if c:
                c_d = dict(c)
                dev = self.db.get_deviation(c_d.get('deviation_id'))
                causes_to_show = [(c_d, dict(dev) if dev else {'id': None, 'description': '—'})]
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            dev_d = dict(dev) if dev else {'id': self._deviation_id, 'description': '—'}
            for c in self.db.causes_for_deviation(self._deviation_id):
                causes_to_show.append((dict(c), dev_d))
        elif self._node_id is not None:
            causes_to_show.extend(self._causes_for_node(self._node_id))

        logging.info('_build_rows: F1 — causes_to_show resolved (n=%d)', len(causes_to_show))

        if not causes_to_show:
            # Show placeholder rows so the user can start adding content
            if self._all_nodes:
                # No nodes (or no deviations/causes) anywhere in the study yet —
                # nothing sensible to show as a placeholder across all nodes.
                self._hdr_lbl.setText("HAZOP Scenario — Hela studien")
            elif self._equipment_filter_id is not None:
                # No causes mention this equipment yet — nothing sensible to
                # show as a placeholder (no single deviation to attach it to).
                equip = self.db.get_equipment_by_id(self._equipment_filter_id)
                tag = equip.get('tag', '?') if equip else '?'
                self._hdr_lbl.setText(f"HAZOP Scenario — Objekt: {tag} (inga orsaker än)")
            elif self._deviation_id is not None:
                dev = self.db.get_deviation(self._deviation_id)
                if dev:
                    dev_d = dict(dev)
                    node  = self.db.get_node(dev_d['node_id'])
                    nn    = node['name'] if node else '?'
                    self._hdr_lbl.setText(f"HAZOP Scenario — {nn} / {dev_d['description']}")
                    self._add_placeholder_row(nn, dev_d)
            elif self._node_id is not None:
                node = self.db.get_node(self._node_id)
                nn   = node['name'] if node else '?'
                self._hdr_lbl.setText(f"HAZOP Scenario — {nn}")
                devs = list(self.db.deviations(self._node_id))
                if devs:
                    for dev in devs:
                        self._add_placeholder_row(nn, dict(dev))
                else:
                    self._add_placeholder_row(nn, None)
            logging.info('_build_rows: F2 — placeholder-only branch done, returning')
            return

        # Determine header title from first cause's node (or, for a sentinel
        # "empty deviation" entry — cause_d is None — fall back to its dev_d,
        # which always carries node_id).
        first_cause, first_dev = causes_to_show[0]
        first_node_id = first_cause['node_id'] if first_cause is not None else first_dev.get('node_id')
        node = self.db.get_node(first_node_id) if first_node_id else None
        node_name_hdr = node['name'] if node else '?'
        if self._all_nodes:
            self._hdr_lbl.setText("HAZOP Scenario — Hela studien")
        elif self._equipment_filter_id is not None:
            equip = self.db.get_equipment_by_id(self._equipment_filter_id)
            tag = equip.get('tag', '?') if equip else '?'
            self._hdr_lbl.setText(f"HAZOP Scenario — Objekt: {tag}")
        elif self._cons_id is not None:
            cons = self.db.get_consequence(self._cons_id)
            cons_desc = cons['description'] if cons else '?'
            _first_desc = first_cause.get('description', '?') if first_cause is not None else '?'
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {_first_desc} / {cons_desc}")
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {dev['description'] if dev else ''}")
        elif self.cause_id is not None:
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {first_cause.get('description', '?')}")
        elif self._node_id is not None:
            self._hdr_lbl.setText(f"HAZOP Scenario — {node_name_hdr}")
        else:
            self._hdr_lbl.setText(f"HAZOP Scenario — {node_name_hdr}")

        logging.info('_build_rows: G0 — header set (%r)', self._hdr_lbl.text())
        self.refresh_placed()
        logging.info('_build_rows: G1 — refresh_placed done, entering cause loop (n=%d)',
                     len(causes_to_show))
        # Tracks the previous REAL cause's (comp_type, comp_tag) so the ORS
        # tag banner can be hidden on a run of consecutive deviations that
        # all belong to the same object (2026-08-18 follow-up: "om det
        # visas flera avikelser efter varandra som tillhör samma
        # objekttagg behöver denna inte repeteras ... tagbannern [kan]
        # försvinna på nummer två i listan och nedåt"). A sentinel
        # "empty deviation" row (cause_d is None) never carries a tag of
        # its own and doesn't break the run — left untouched here so a
        # real cause immediately after one still dedups correctly.
        _prev_tag_display = None
        for _cause_idx, (cause_d, dev_d) in enumerate(causes_to_show):
            if cause_d is None:
                # Sentinel from _causes_for_node(): this deviation has zero causes,
                # but "Visa avvikelser utan orsaker" is on — show it as its own
                # placeholder row instead of skipping it.
                if _cause_idx % 10 == 0 or _cause_idx == len(causes_to_show) - 1:
                    logging.info('_build_rows: G2 — cause loop iter %d/%d (empty deviation, '
                                 'dev_id=%s)', _cause_idx, len(causes_to_show), dev_d.get('id'))
                node = self.db.get_node(dev_d.get('node_id')) if dev_d.get('node_id') else None
                node_name = node['name'] if node else '?'
                self._add_placeholder_row(node_name, dev_d)
                continue
            _tag_display = self._cause_tag_display(cause_d)
            _repeats_previous_tag = (bool(_tag_display[0] or _tag_display[1])
                                     and _tag_display == _prev_tag_display)
            _prev_tag_display = _tag_display
            if _cause_idx % 10 == 0 or _cause_idx == len(causes_to_show) - 1:
                logging.info('_build_rows: G2 — cause loop iter %d/%d (cause_id=%s)',
                             _cause_idx, len(causes_to_show), cause_d.get('id'))
            node = self.db.get_node(cause_d['node_id'])
            node_name = node['name'] if node else '?'
            freq = self.db.cause_frequency_level(cause_d)
            _fi = freq_to_idx(freq)
            freq_lbl = FREQ_LABELS[_fi] if _fi < len(FREQ_LABELS) else f'F{freq}'
            first_row_for_cause = self._table.rowCount()
            all_cons = list(self.db.consequences(cause_d['id']))
            if self._cons_id is not None:
                all_cons = [c for c in all_cons if dict(c)['id'] == self._cons_id]
            for _cons_idx, cons in enumerate(all_cons):
                cons_d = dict(cons)
                logging.info('_build_rows: H0 — cause %s cons_idx %d/%d cons_id=%s',
                             cause_d.get('id'), _cons_idx, len(all_cons), cons_d.get('id'))
                sgs    = [dict(s) for s in self.db.safeguards(cons_d['id'])]
                cat_rows = [dict(r) for r in
                            self.db.get_consequence_severities(cons_d['id'])]
                n_cats = len(cat_rows)
                n_sgs  = len(sgs)
                n_rows = max(n_cats, n_sgs, 1)

                # Precompute exclusions per severity assessment
                cat_excl_map = {}           # sev_id → set of excluded sg_ids
                for _cr in cat_rows:
                    cat_excl_map[_cr['id']] = self.db.get_severity_excluded_sgs(_cr['id'])

                # Which safeguards are excluded from at least one category?
                any_excl_map = {}           # sg_id → list of category names
                for _sg in sgs:
                    any_excl_map[_sg['id']] = [
                        _cr['name'] for _cr in cat_rows
                        if _sg['id'] in cat_excl_map.get(_cr['id'], set())]

                # Which safeguards are excluded from this specific cause?
                cause_excl_sgs = set()
                for _sg in sgs:
                    excl_causes = self.db.get_safeguard_excluded_causes(_sg['id'])
                    if cause_d['id'] in excl_causes:
                        cause_excl_sgs.add(_sg['id'])

                # Category list for the RRF popup: [(sev_id, cat_name), ...]
                sev_cat_list = [(cr['id'], cr['name']) for cr in cat_rows]
                # Full category info for stacked badges in KON cell
                all_cat_infos = [(cr['category_id'], cr['id'],
                                  cr['name'], cr['severity']) for cr in cat_rows]
                # Cause list for the RRF popup
                _direct_cause = self.db.get_cause(cons_d.get('cause_id')) if cons_d.get('cause_id') else None
                cause_popup_list = []
                if _direct_cause:
                    cause_popup_list.append((dict(_direct_cause)['id'],
                                             dict(_direct_cause)['description'], False))

                logging.info('_build_rows: H1 — cons_id=%s about to add %d row(s) '
                             '(n_cats=%d n_sgs=%d)',
                             cons_d.get('id'), n_rows, n_cats, n_sgs)
                first_row_for_cons = self._table.rowCount()
                for i in range(n_rows):
                    sg_i    = sgs[i] if i < n_sgs else None
                    cr_i    = cat_rows[i] if i < n_cats else None
                    cat_info_i = ((cr_i['category_id'], cr_i['id'],
                                   cr_i['name'], cr_i['severity'])
                                  if cr_i else None)
                    excl_for_cat  = cat_excl_map.get(cr_i['id'], set()) if cr_i else set()
                    excl_cat_names = any_excl_map.get(sg_i['id'], []) if sg_i else []
                    logging.info('_build_rows: H2 — _add_row cons_id=%s row_i=%d/%d '
                                 '(will create _LopaWidget)',
                                 cons_d.get('id'), i, n_rows)
                    self._add_row(node_name, dev_d, cause_d, freq, freq_lbl,
                                  cons_d, sgs, sg_i,
                                  cat_info=cat_info_i,
                                  excl_cat_names=excl_cat_names,
                                  excl_for_cat=excl_for_cat,
                                  cause_excl_sgs=cause_excl_sgs,
                                  sev_cat_list=sev_cat_list,
                                  all_cat_infos=all_cat_infos,
                                  cause_popup_list=cause_popup_list,
                                  n_cats=n_cats,
                                  repeats_previous_tag=_repeats_previous_tag)
                    logging.info('_build_rows: H3 — _add_row cons_id=%s row_i=%d done',
                                 cons_d.get('id'), i)
                # "+" badge on the SG cell — only when this consequence
                # already has at least one real safeguard; an empty one
                # already invites Enter-to-add on its own placeholder row.
                if n_sgs > 0:
                    self._mark_plus_target(self._table.rowCount() - 1, self._C_SG,
                                            'safeguard', cons_d['id'])
            if self._table.rowCount() == first_row_for_cause:
                logging.info('_build_rows: G3 — cause %s had no rows, adding empty row',
                             cause_d.get('id'))
                self._add_empty_row(node_name, dev_d, cause_d, freq, freq_lbl,
                                    repeats_previous_tag=_repeats_previous_tag)
            elif all_cons:
                # "+" badge on the KON cell — only when this cause already
                # has at least one real consequence (mirrors the safeguard
                # rule above; the empty case is _add_empty_row, just above).
                # Anchored at first_row_for_cons (the LAST consequence's
                # own first row), not the last physical row — KON spans by
                # cons_id (_apply_spans), and Qt's delegate paints a
                # spanned cell using its ANCHOR (first) row, not any later
                # row the span happens to cover; a badge keyed to the last
                # row would silently never be found by paint().
                self._mark_plus_target(first_row_for_cons, self._C_KON,
                                        'consequence', cause_d['id'])
            # "+" badge on the ORS cell — once per deviation, after its
            # LAST real cause (sentinel/empty-deviation entries never
            # reach this point, they `continue` above, so this only fires
            # for deviations that had at least one real cause). Anchored
            # at first_row_for_cause for the same span-anchor reason as
            # the KON badge above — ORS spans by cause_id.
            _next_dev_id = (causes_to_show[_cause_idx + 1][1].get('id')
                            if _cause_idx + 1 < len(causes_to_show) else None)
            if dev_d.get('id') != _next_dev_id:
                self._mark_plus_target(first_row_for_cause, self._C_ORS,
                                        'cause', dev_d.get('id'))
        logging.info('_build_rows: I0 — cause loop complete, rowCount=%d',
                     self._table.rowCount())

    def _apply_spans(self):
        """Merge consecutive rows that share the same Nod or Orsak."""
        n = self._table.rowCount()
        logging.info('_apply_spans: J0 — entry (rowCount=%d)', n)
        if n < 2:
            logging.info('_apply_spans: J1 — fewer than 2 rows, nothing to span')
            return

        def _span_col(col, key_fn):
            r = 0
            while r < n:
                k = key_fn(r)
                span = 1
                while r + span < n and key_fn(r + span) == k and k is not None:
                    span += 1
                if span > 1:
                    self._table.setSpan(r, col, span, 1)
                    item = self._table.item(r, col)
                    if item:
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                r += span

        def _meta(r, idx):
            return self._row_meta[r][idx] if r < len(self._row_meta) else None

        # Nod: group by node_id stored in UserRole
        _span_col(self._C_NOD, lambda r: (
            self._table.item(r, self._C_NOD).data(Qt.ItemDataRole.UserRole)
            if self._table.item(r, self._C_NOD) else None))
        logging.info('_apply_spans: J2 — NOD column spanned')

        # Utrustning: group by equipment_id stored in UserRole — spans ALL of
        # an equipment's deviation rows together (like NOD spans a whole
        # node), not just rows sharing one deviation. Rows with no equipment
        # (key None) never span, same as _span_col's general None handling.
        _span_col(self._C_UTR, lambda r: (
            self._table.item(r, self._C_UTR).data(Qt.ItemDataRole.UserRole)
            if self._table.item(r, self._C_UTR) else None))
        logging.info('_apply_spans: J2b — UTR column spanned')

        # Avvikelse: group by dev_id (index 0 in row_meta)
        _span_col(self._C_DEV, lambda r: _meta(r, 0))
        logging.info('_apply_spans: J3 — DEV column spanned')

        # Orsak: group by cause_id (index 1)
        _span_col(self._C_ORS, lambda r: _meta(r, 1))
        logging.info('_apply_spans: J4 — ORS column spanned')

        # Consequence-level columns: group by (cons_id, cat_id) so each
        # category assessment forms its own span group
        def _cat_key(r):
            cons_id  = _meta(r, 2)
            cat_info = self._row_cat_info[r] if r < len(self._row_cat_info) else None
            cat_id   = cat_info[0] if cat_info else None
            return (cons_id, cat_id)

        # KON, LOPA and REK: span by cons_id (whole consequence merged) —
        # a recommendation belongs to the CONSEQUENCE, not to one of its
        # safeguard rows, same as KON/LOPA already group.
        for col in (self._C_KON, self._C_LOPA, self._C_REK):
            _span_col(col, lambda r: _meta(r, 2))
        logging.info('_apply_spans: J5 — KON/LOPA/REK columns spanned')

        # RFORE, SLUT: span by (cons_id, cat_id)
        # → non-category rows all merge; per-category rows each stay separate
        for col in (self._C_RFORE, self._C_SLUT):
            _span_col(col, _cat_key)
        logging.info('_apply_spans: J6 — RFORE/SLUT columns spanned, done')

    def _compute_row_height(self, row, fm=None):
        """The height `row` needs across EVERY column that can affect it —
        ORS/KON wrapped text, the fixed-height _LopaWidget in the FA/Ant.
        column, SG's one-line minimum, and the ORS readability floor —
        folded into a single per-row function so a caller updating just
        ONE column's text can never accidentally shrink the row below what
        its OTHER columns need.

        2026-08-11 ("skapat en konsekvens och sedan suddar ut allt krymper
        raden ... jag inte ser vad som står på orsak och FA/antändning ser
        konstigt ut"): before this, _update_row_text_only()'s fast path set
        a row's height to ONLY what the just-edited column (e.g. KON, once
        cleared back to empty text) needed, discarding whatever a long ORS
        cause description or the LOPA widget's own fixed height required —
        the row shrank to one line, clipping the cause text and squashing
        the FA/Ant. widget below its own setFixedHeight(). This function is
        now the single source of truth for "how tall must this row be",
        used by both the full _resize_rows_manual() rebuild pass AND
        _update_row_text_only()'s single-row fast path, so the two can
        never again disagree about one row's height. Also folds in the ORS
        minimum-readable-height floor _resize_rows() used to apply in a
        SEPARATE pass reachable only from a full rebuild — the fast path
        never went through that pass at all, so a short ORS text edited via
        the fast path could previously end up below the floor too.
        """
        table = self._table
        if fm is None:
            fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6
        sg_row_h = self._sg_row_height(table.font())
        wrap_cols = (self._C_ORS, self._C_KON, self._C_REK)

        # NOD/UTR/DEV/ORS/KON/LOPA/REK/RFORE/SLUT are all spanned across a
        # consequence's safeguard rows (_apply_spans), but every physical
        # row still gets its OWN freshly-built item/widget from _add_row()
        # regardless (setSpan/setCellWidget only change how Qt PAINTS
        # covered cells, they don't clear or skip creating content there)
        # — table.item(row, c) is not None and table.cellWidget(row, c) is
        # not None are therefore both ALWAYS true and useless for
        # detecting "does this row have content of its own", a mistake an
        # earlier version of this fix made throughout. The only reliable
        # signal is comparing each column's own span key (from
        # _apply_spans) against the previous row: unchanged means this
        # row is a covered continuation for that column, not its anchor,
        # so that column's (duplicate) content must not count toward this
        # row's height.
        def _cause_id(r):
            return self._row_meta[r][1] if 0 <= r < len(self._row_meta) else None
        def _cons_id(r):
            return self._row_meta[r][2] if 0 <= r < len(self._row_meta) else None
        def _cat_info(r):
            return self._row_cat_info[r] if 0 <= r < len(self._row_cat_info) else None

        is_ors_anchor  = row == 0 or _cause_id(row) != _cause_id(row - 1)
        is_cons_anchor = row == 0 or _cons_id(row)  != _cons_id(row - 1)

        # A second, third, ... safeguard row has no independent content of
        # its own in any column but SG only when it's a continuation for
        # BOTH the cons_id-keyed columns (KON/LOPA/REK) AND the finer
        # (cons_id, cat_info)-keyed ones (RFORE/SLUT) — a cat_info change
        # within the SAME consequence still means RFORE/SLUT has fresh
        # content this row. Such a row only needs the compact SG height,
        # not a full text line's worth of space — multiplying that saving
        # across several safeguards is the whole point (2026-08-18
        # follow-up: "krymper höjden på safeguards ... för att spara
        # plats när man lägger till flera safeguards").
        is_pure_sg_continuation = (
            not is_cons_anchor and _cons_id(row) is not None
            and _cat_info(row) == _cat_info(row - 1))
        max_h = sg_row_h if is_pure_sg_continuation else one_line_h

        for col in range(table.columnCount()):
            if table.isColumnHidden(col):
                continue

            if col == self._C_LOPA:
                # LOPA spans by cons_id alone — see the module-level note
                # above this function.
                if is_cons_anchor:
                    widget = table.cellWidget(row, col)
                    if widget is not None:
                        h = widget.sizeHint().height()
                        if h > max_h:
                            max_h = h
                continue

            if col == self._C_SG:
                # SG's description never word-wraps — a single
                # compact line is always enough.
                if sg_row_h > max_h:
                    max_h = sg_row_h
                continue

            if col not in wrap_cols:
                # Fixed one-line columns (matches _ScenarioDelegate's
                # non-wrap branch) — no font-metric work needed.
                continue

            # ORS spans by cause_id, KON/REK by cons_id — a non-anchor row
            # for either must not have its (duplicate) text measured, same
            # reasoning as the LOPA branch above.
            if col == self._C_ORS and not is_ors_anchor:
                continue
            if col in (self._C_KON, self._C_REK) and not is_cons_anchor:
                continue

            item = table.item(row, col)
            text = item.text() if item is not None else ''
            if not text:
                continue

            w = table.columnWidth(col)
            if col == self._C_ORS:
                cell_w = max(40, w - 6)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = _ORS_HEADER_H + max(one_line_h, rect.height() + 4)
            elif col == self._C_KON:
                cell_w = max(40, w - _KON_CAT_W)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = max(one_line_h, rect.height() + 4)
            else:   # self._C_REK
                cell_w = max(40, w - 6)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = max(one_line_h, rect.height() + 4)
            if h > max_h:
                max_h = h

        if is_ors_anchor:
            ors_item = table.item(row, self._C_ORS)
            if ors_item and ors_item.text():
                min_ors = fm.height() * 2 + 20  # floor for ORS rows: ~2 lines + strip
                if max_h < min_ors:
                    max_h = min_ors
        return max_h

    def _resize_rows_manual(self):
        """
        Compute and apply each row's height directly in Python instead of
        calling QTableWidget.resizeRowsToContents() (see _resize_rows()
        docstring for why). Delegates the actual per-row formula to
        _compute_row_height() (shared with _update_row_text_only()'s fast
        path — see that method's docstring) but keeps the QFontMetrics
        instance built ONCE up here rather than once per row: only the
        columns that actually need wrapping-height computation (_C_ORS,
        _C_KON — see _ScenarioDelegate._size_hint_impl) run the expensive
        QFontMetrics.boundingRect() path, but constructing QFontMetrics
        itself hundreds of times (once per row, in "all nodes" mode) is
        needless allocation churn worth avoiding when a single shared
        instance works just as well.
        """
        table = self._table
        row_count = table.rowCount()
        col_count = table.columnCount()
        fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6

        logging.info('_resize_rows_manual: L0 — entry (rows=%d, cols=%d)',
                     row_count, col_count)

        for row in range(row_count):
            try:
                max_h = self._compute_row_height(row, fm=fm)
            except Exception:
                # Defensive: this is user-facing rebuild code and a single
                # row's height computation should never take down the whole
                # rebuild. This can only catch genuine Python-level
                # exceptions (attribute errors, etc.) — it is not a safety
                # net for native crashes, since the whole point of this
                # method is to avoid the native resizeRowsToContents() path
                # that was pinpointed as the actual crash site.
                logging.exception('_resize_rows_manual: L1 — row %d height calc raised', row)
                max_h = one_line_h

            if max_h > 0:
                table.setRowHeight(row, max_h)

        logging.info('_resize_rows_manual: L2 — done (%d rows sized)', row_count)

    def _resize_rows(self, vscroll_value, hscroll_value):
        """
        Apply row height constraints and restore scroll position.
        Called after _apply_spans() to finalize table layout.
        Extracted from _rebuild() closure for clarity and testability.

        NOTE: this deliberately does NOT call QTableWidget.resizeRowsToContents().
        That native Qt call was pinpointed (via the K0/K1 checkpoint logging
        added in 2aba0b4) as the exact site of a silent native (C++-level)
        crash after rapid rebuild cycles — the process died inside the C++
        call with no Python exception and no further log output. Since a
        native crash can't be fixed with a Python try/except (there's nothing
        to catch), the fix is to never invoke that specific machinery at all:
        the per-row/per-cell height is instead computed directly in Python
        below, using the same logic _ScenarioDelegate.sizeHint() uses
        internally, and applied via the plain (safe) setRowHeight() API.
        """
        logging.info('_resize_rows: K0 — entry (rowCount=%d), computing row heights manually',
                     self._table.rowCount())
        self._resize_rows_manual()
        logging.info('_resize_rows: K1 — manual row-height loop done')
        _fm  = QFontMetrics(self._table.font())
        _min_ors = _fm.height() * 2 + 20  # floor for ORS rows: ~2 lines + strip
        for _r in range(self._table.rowCount()):
            h = self._table.rowHeight(_r)
            # ORS cell in this row is a real, VISIBLE cell (the anchor of
            # its cause's span, not one of the extra physical rows a
            # multi-safeguard consequence adds below it) → enforce minimum
            # readable height. Checking ors_item.text() alone used to
            # wrongly match every row in the span: _add_row() gives EVERY
            # physical row its own freshly-built ORS item with the same
            # cause text (setSpan only changes how Qt paints covered
            # cells, it doesn't clear their items), so this floor was
            # silently re-inflating the compact safeguard-continuation
            # rows _compute_row_height() had just shrunk (2026-08-18
            # follow-up: "krymper höjden på safeguards ... för att spara
            # plats" — same underlying bug as that fix's own, now
            # corrected, table.item()-based check). cause_id (row_meta[1])
            # differing from the previous row is what actually marks the
            # anchor — same span key ORS itself groups by (_apply_spans).
            # There used to also be an upper CAP here (~4 text lines) that
            # silently shrank any row whose wrapped description needed more
            # room than that, clipping the rest of the text with no visual
            # indication anything was cut off — exactly the "text göms på
            # raderna ... särskilt de som står under orsaker" bug report
            # (2026-08-11, see NOTES.md). In a safety-documentation tool,
            # a tall row is a far smaller problem than a hazard/cause
            # description silently missing its last few lines, so the cap
            # is gone — rows now grow to fit however much text is actually
            # there, exactly what "flerradig, auto-höjd" is supposed to mean.
            ors_item = self._table.item(_r, self._C_ORS)
            _cause_id = self._row_meta[_r][1] if _r < len(self._row_meta) else None
            _is_ors_anchor = (_cause_id is not None and (
                _r == 0 or self._row_meta[_r - 1][1] != _cause_id))
            if ors_item and ors_item.text() and _is_ors_anchor and h < _min_ors:
                self._table.setRowHeight(_r, _min_ors)
        logging.info('_resize_rows: K2 — row-height pass done, restoring scroll position')
        self._table.verticalScrollBar().setValue(vscroll_value)
        self._table.horizontalScrollBar().setValue(hscroll_value)
        self._table.setUpdatesEnabled(True)
        logging.info('_resize_rows: K3 — done (setUpdatesEnabled True)')

    def _add_placeholder_row(self, node_name, dev_d):
        """Empty row shown when a node/deviation has no causes yet."""
        r = self._table.rowCount()
        self._table.insertRow(r)
        dev_id = dev_d['id'] if dev_d else None
        self._row_meta.append((dev_id, None, None, None))
        self._row_cat_info.append(None)

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(node_name)
        self._table.setItem(r, self._C_NOD, nod)
        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr = _ro(eq_label)
        utr.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr)
        dev_item = _ro(dev_d['description'] if dev_d else '')
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        # ORS cell shows the two-zone layout (with '+' in obj zone) but has no cause yet
        ors = QTableWidgetItem('')
        ors.setData(Qt.ItemDataRole.UserRole + 2, ('', ''))
        ors.setToolTip("Enter för att lägga till orsak")
        self._table.setItem(r, self._C_ORS, ors)

        for col in (self._C_KON, self._C_RFORE, self._C_SG,
                    self._C_LOPA, self._C_SLUT):
            self._table.setItem(r, col, _ro())
        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _add_empty_row(self, node_name, dev_d, cause_d, freq, freq_lbl,
                        repeats_previous_tag=False):
        """Placeholder row when a cause has no consequences yet."""
        r = self._table.rowCount()
        self._table.insertRow(r)
        dev_id = dev_d['id'] if dev_d else None
        self._row_meta.append((dev_id, cause_d['id'], None, None))
        self._row_cat_info.append(None)

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(node_name)
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr = _ro(eq_label)
        utr.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr)

        dev_item = _ro(dev_d['description'] if dev_d else '')
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3, freq)
        ors.setData(Qt.ItemDataRole.UserRole + 8, repeats_previous_tag)
        self._table.setItem(r, self._C_ORS, ors)

        kon = _ro()
        kon.setToolTip("Enter för att lägga till konsekvens")
        self._table.setItem(r, self._C_KON, kon)

        for col in (self._C_RFORE, self._C_SG,
                    self._C_LOPA, self._C_SLUT):
            self._table.setItem(r, col, _ro())

        pass  # row height set by resizeRowsToContents at end of _rebuild

    _PLUS_TIPS = {
        'cause':       "Klicka för att lägga till en ny orsak, valfritt kopplad till ett P&ID-objekt",
        'consequence': "Klicka för att lägga till en ny konsekvens, valfritt kopplad till ett P&ID-objekt",
        'safeguard':   "Klicka för att lägga till en ny barriär, valfritt kopplad till ett P&ID-objekt",
    }

    def _mark_plus_target(self, row, col, kind, group_id):
        """Flags an ALREADY-BUILT real row's cell to show a small in-cell
        "+" badge in its bottom-right corner (2026-08-12, see NOTES.md —
        reported feedback: a dedicated "+" ROW "tar upp alldeles för
        mycket plats då de tar hela rader med blankt", i.e. took up way
        too much space with whole blank rows; "lägg hellre ett plus om
        det redan finns en orsak/konsekvens/safeguard i den rutan"). Only
        ever called on the LAST real row of a non-empty group — a group
        with zero content still invites Enter-to-add on its existing
        placeholder row instead (_add_placeholder_row/_add_empty_row,
        unchanged). Painted by _PidDelegate._draw_plus_badge(), hit-
        tested by eventFilter()."""
        self._row_plus_cols.setdefault(row, {})[col] = (kind, group_id)
        item = self._table.item(row, col)
        if item is not None:
            item.setToolTip((item.toolTip() + '\n' if item.toolTip() else '')
                             + self._PLUS_TIPS[kind])

    def _add_row(self, node_name, dev_d, cause_d, freq, freq_lbl, cons_d, all_sgs, sg,
                 cat_info=None, excl_cat_names=None, excl_for_cat=None,
                 cause_excl_sgs=None, sev_cat_list=None, all_cat_infos=None,
                 cause_popup_list=None, n_cats=0, repeats_previous_tag=False):
        """One row in the scenario table.

        sg            – the safeguard for this row (None = no safeguard on this row).
        cat_info      – (cat_id, sev_id, cat_name, cat_sev) for the category shown on
                        this row; None when this row has no category assessment.
        excl_cat_names – list of category names this safeguard is excluded from
                        (used for yellow ○ indicator on RRF badge).
        excl_for_cat  – set of sg_ids excluded from THIS row's category (for REFT calc).
        sev_cat_list  – [(sev_id, cat_name), ...] all category assessments for this
                        consequence (stored in SG cell for the extended RRF popup).
        n_cats        – total number of category assessments.
        """
        if excl_cat_names is None:
            excl_cat_names = []
        if excl_for_cat is None:
            excl_for_cat = set()
        if cause_excl_sgs is None:
            cause_excl_sgs = set()
        if sev_cat_list is None:
            sev_cat_list = []
        if cause_popup_list is None:
            cause_popup_list = []

        r      = self._table.rowCount()
        self._table.insertRow(r)
        cid    = cons_d['id']
        dev_id = dev_d['id'] if dev_d else None

        self._row_meta.append((dev_id, cause_d['id'], cid, sg['id'] if sg else None))
        self._row_cat_info.append(cat_info)

        # ── Risk calculations ─────────────────────────────────────────────────
        if cat_info:
            cat_id, sev_id, cat_name, cat_sev = cat_info
            sev = cat_sev or 1
            # Effective RRF for this category (exclude excluded safeguards)
            active_sgs = [s for s in all_sgs
                          if s['id'] not in excl_for_cat and s['id'] not in cause_excl_sgs]
            sg_rrf = 1
            for s in active_sgs:
                sg_rrf *= (s.get('rrf') or 1)
        else:
            sev = cons_d['severity'] or 1
            sg_rrf = 1
            for s in all_sgs:
                if s['id'] not in cause_excl_sgs:
                    sg_rrf *= (s.get('rrf') or 1)

        rfs        = [dict(rf) for rf in self.db.reduction_factors(cid)]
        fa_active  = bool(cons_d.get('fa_active', 0))
        fa_rrf     = cons_d.get('fa_rrf', 10) or 10
        ign_active = bool(cons_d.get('ignition_active', 0))
        ign_rrf    = cons_d.get('ignition_rrf', 10) or 10

        final_f, total_rrf, total_steps = total_freq_reduction(
            freq, sg_rrf, fa_active, fa_rrf, ign_active, ign_rrf, rfs)

        level_b, bg_b, fg_b = risk_info(freq, sev)
        level_s, bg_s, fg_s = risk_info(final_f, sev)

        # ── Col 0: Nod ────────────────────────────────────────────────────────
        nod = QTableWidgetItem(node_name)
        nod.setFlags(nod.flags() & ~Qt.ItemFlag.ItemIsEditable)
        nod.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        # ── Col: Utrustning ──────────────────────────────────────────────────
        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr_item = QTableWidgetItem(eq_label)
        utr_item.setFlags(utr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        utr_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        utr_item.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr_item)

        # ── Col 1: Avvikelse ─────────────────────────────────────────────────
        dev_item = QTableWidgetItem(dev_d['description'] if dev_d else '')
        dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        # ── Col 2: Orsak ─────────────────────────────────────────────────────
        # Status icon (feature 5): green=complete, orange=partial, red=empty
        _cons_list   = self.db.consequences(cause_d['id'])
        _has_cons    = len(_cons_list) > 0
        _has_sev     = any(c.get('severity', 0) and c.get('severity', 0) > 0
                           for c in [dict(x) for x in _cons_list])
        _has_sg      = bool(self.db.safeguards_for_cause(cause_d['id']))
        if _has_cons and _has_sev and _has_sg:
            _status_icon = '🟢'
            _status_tip  = 'Komplett: konsekvens + allvarlighet + barriär'
        elif _has_cons and _has_sev:
            _status_icon = '🟡'
            _status_tip  = 'Saknar barriär'
        elif _has_cons:
            _status_icon = '🟠'
            _status_tip  = 'Saknar allvarlighetsgradering'
        else:
            _status_icon = '🔴'
            _status_tip  = 'Ingen konsekvens angiven'

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3, freq)
        ors.setData(Qt.ItemDataRole.UserRole + 5, cause_d.get('base_frequency'))
        ors.setData(Qt.ItemDataRole.UserRole + 8, repeats_previous_tag)
        # _status_icon is no longer stored on the item (2026-08-18, see
        # NOTES.md "skrota pluppen") — the green/yellow/orange/red fill-
        # status dot it drove is gone from paint(); the underlying
        # completeness computation stays, still used for the tooltip below.
        ors.setToolTip(f"{_status_icon} {_status_tip}\n"
                       "Dubbelklicka för att redigera\n"
                       "Klicka på objektzonen (vänster) för att sätta utrustnings-tag\n"
                       "Enter för att lägga till ny orsak")
        self._table.setItem(r, self._C_ORS, ors)

        # ── Col 3: Konsekvens ─────────────────────────────────────────────────
        chain_data   = parse_chain_from_json(cons_d.get('consequence_chain', ''))
        display_desc = (build_consequence_text(cons_d['description'], chain_data)
                        or cons_d['description'])

        # "—" placeholder when empty, not literal "Ny konsekvens" text
        # (2026-08-12, see NOTES.md). No separate EditRole is set here —
        # QTableWidgetItem aliases Display/EditRole to the same storage
        # (verified: setData() on one overwrites what the other reads
        # back), so _PidDelegate.createEditor() strips the "—" sentinel
        # itself when opening the editor instead.
        kon_item = QTableWidgetItem(cons_d['description'] or '—')
        kon_item.setData(Qt.ItemDataRole.UserRole, ('consequence', cid))
        kon_item.setData(Qt.ItemDataRole.UserRole + 3, None)   # no per-row cat badge
        kon_item.setData(Qt.ItemDataRole.UserRole + 4, n_cats)
        kon_item.setData(Qt.ItemDataRole.UserRole + 5, all_cat_infos or [])
        kon_item.setData(Qt.ItemDataRole.UserRole + 7, (cons_d.get('comp_type') or '',
                                                         cons_d.get('comp_tag')  or ''))
        # Every tag ever drag-appended into this text, bolded on paint
        # (2026-08-09, see NOTES.md "fetmarkera objekttexten") — comp_tag
        # above only ever holds the MOST RECENT one.
        kon_item.setData(Qt.ItemDataRole.UserRole + 8,
                         parse_tag_refs(cons_d.get('tagged_refs') or ''))
        tip = ("Klicka på 📊-ikonen för att sätta konsekvens per kategori\n"
               "Dra en utrustningsmarkör hit (håll Shift) för att sätta tag\n"
               "Dubbelklicka för att redigera\nEnter för att lägga till ny konsekvens")
        if display_desc != cons_d['description']:
            tip += f"\nKedjetext: {display_desc}"
        kon_item.setToolTip(tip)
        self._table.setItem(r, self._C_KON, kon_item)

        # ── Col 4: Risk före barriär ──────────────────────────────────────────
        # Shown for EVERY row, not just ones with a per-category severity
        # assessment (2026-08-09, see NOTES.md) — freq/sev/bg_b/fg_b are
        # already computed unconditionally above (the cat_info/plain-severity
        # branch just above), so a consequence that only ever got its plain
        # severity+category set via ConsequencePanel (the common case — the
        # per-category 📊 assessment is an opt-in power-user feature) used to
        # render this cell completely blank/uncolored despite having a
        # perfectly valid risk value. Falls back to the plain 'risk_click'
        # action (pre-existing in _on_risk_cell_clicked, previously dead code
        # since nothing ever emitted it) instead of 'risk_click_cat' when
        # there's no real category_id/severity_id to edit.
        rb = QTableWidgetItem(f"{freq_axis_label(freq)}  {cons_axis_label(sev)}")
        rb.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        rb.setFlags(rb.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rb.setToolTip(f"🖱 Klicka för att ändra i riskmatrisen\n{level_b}")
        if cat_info:
            rb.setData(Qt.ItemDataRole.UserRole,
                       ('risk_click_cat', cause_d['id'], cid, cat_id, sev_id, freq, sev))
        else:
            rb.setData(Qt.ItemDataRole.UserRole,
                       ('risk_click', cause_d['id'], cid, freq, sev))
        rb.setBackground(QBrush(QColor(bg_b)))
        rb.setForeground(QBrush(QColor(fg_b)))
        # One point smaller than the general cell font (reported: this
        # cell's text got cut off at the default 85px column width) and
        # scales with "Textstorlek" instead of being hardcoded at 9pt.
        rb.setFont(QFont("Consolas", max(6, self._cell_font_size - 1)))
        self._table.setItem(r, self._C_RFORE, rb)

        # ── Col 5: Barriär ───────────────────────────────────────────────────
        if sg is None:
            sg_item = QTableWidgetItem('—')
            sg_item.setFlags(sg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sg_item.setToolTip("Enter för att lägga till barriär")
        else:
            rrf = sg.get('rrf', 1) or 1
            # "—" placeholder when empty (2026-08-12, see NOTES.md) — no
            # separate EditRole set here, see the KON cell's comment above
            # on why that would silently overwrite this back to empty.
            sg_item = QTableWidgetItem(sg['description'] or '—')
            sg_item.setData(Qt.ItemDataRole.UserRole,     ('safeguard', sg['id']))
            sg_item.setData(Qt.ItemDataRole.UserRole + 1, rrf)
            # Yellow indicator: list of category names this sg is excluded from
            sg_item.setData(Qt.ItemDataRole.UserRole + 2, excl_cat_names)
            # Category data for extended RRF popup: (cons_id, [(sev_id, cat_name), ...])
            sg_item.setData(Qt.ItemDataRole.UserRole + 3, (cid, sev_cat_list) if sev_cat_list else None)
            # Cause list for RRF popup cause-exclusion section
            sg_item.setData(Qt.ItemDataRole.UserRole + 4, cause_popup_list)
            excl_cause_ids = self.db.get_safeguard_excluded_causes(sg['id'])
            excl_cause_names = [desc for cid2, desc, _ in cause_popup_list
                                if cid2 in excl_cause_ids]
            sg_item.setData(Qt.ItemDataRole.UserRole + 5, excl_cause_names)
            sg_item.setData(Qt.ItemDataRole.UserRole + 6, (sg.get('comp_type') or '',
                                                            sg.get('comp_tag')  or ''))
            sg_item.setData(Qt.ItemDataRole.UserRole + 7,
                             parse_tag_refs(sg.get('tagged_refs') or ''))
            tip = "Dra en utrustningsmarkör hit (håll Shift) för att sätta tag\n" \
                  "Dubbelklicka för att redigera\nEnter för att lägga till ny barriär\nKlicka på RRF-kolumnen för att ändra värdet"
            if excl_cat_names:
                tip += "\n⚠ Gäller ej för kategori: " + ", ".join(excl_cat_names)
            if excl_cause_names:
                tip += "\n⚠ Gäller ej för orsak: " + ", ".join(excl_cause_names)
            sg_item.setToolTip(tip)
        self._table.setItem(r, self._C_SG, sg_item)

        # ── Col LOPA: FA / Antändning / Övriga (merged LOPA column) ──────────
        n_active = sum(1 for rf in rfs if rf.get('active'))
        lopa_w = _LopaWidget(self.db, cid,
                             fa_active, fa_rrf, ign_active, ign_rrf, n_active)
        lopa_w._extra_btn.clicked.connect(partial(self._edit_extra, cid))
        lopa_w.changed.connect(self._update_lopa_risk)
        self._table.setCellWidget(r, self._C_LOPA, lopa_w)

        # ── Col SLUT: Slutkonsekvens ──────────────────────────────────────────
        # Shown for every row now (2026-08-09, see NOTES.md) — same fallback
        # rationale as RFORE above; final_f/sev/bg_s/fg_s are already
        # computed unconditionally regardless of cat_info.
        slut_text = (f"−{total_steps} steg\n" if total_steps else "") + \
                    f"{freq_axis_label(final_f)}  {cons_axis_label(sev)}"
        rs = QTableWidgetItem(slut_text)
        rs.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        rs.setFlags(rs.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rs.setToolTip(f"{level_s} — {freq_axis_label(final_f)}  {cons_axis_label(sev)}  (−{total_steps} steg totalt)")
        rs.setBackground(QBrush(QColor(bg_s)))
        rs.setForeground(QBrush(QColor(fg_s)))
        rs.setFont(QFont("Consolas", 9))
        self._table.setItem(r, self._C_SLUT, rs)

        # ── Col REK: Rekommendation (2026-08-13, see NOTES.md) ───────────────
        # Backed by the pre-existing actions table/ActionEditor (previously
        # unreachable in the UI) rather than a new free-text field — a
        # scenario can have several recommendations (responsible/due date/
        # status each), not just one line of text.
        acts = self.db.actions(cid)
        rek_item = QTableWidgetItem(self._recommendation_summary(acts))
        rek_item.setFlags(rek_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rek_item.setData(Qt.ItemDataRole.UserRole, ('recommendation', cid))
        rek_item.setToolTip("Klicka för att lägga till/redigera rekommendationer")
        if not acts:
            rek_item.setForeground(QBrush(QColor('#8D9299')))
        self._table.setItem(r, self._C_REK, rek_item)

        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _recommendation_summary(self, acts):
        """REK-cell text for a consequence's action/recommendation list
        (2026-08-13, see NOTES.md: "samtliga tillagda rekomendationer
        ... nummereras efter tilläggsordning") — "—" placeholder when
        empty (same convention as KON/SG), otherwise EVERY recommendation
        listed on its own line, numbered 1.. in the order they were
        added (db.actions() already returns them ORDER BY id). The
        column joins wrap_cols so multi-line content gets the row
        height it needs, same as ORS/KON."""
        if not acts:
            return '—'
        return '\n'.join(f"{i}. {a['description'] or 'Ny åtgärd'}"
                          for i, a in enumerate(acts, 1))

    def _get_cons_context(self, cons_id: int):
        """Return (deviation, comp_type, cause_text) for the consequence."""
        cons = self.db.get_consequence(cons_id)
        if not cons:
            return '', '', ''
        cause = self.db.get_cause(cons['cause_id'])
        if not cause:
            return '', '', ''
        cause_d  = dict(cause)
        comp     = cause_d.get('comp_type', '') or ''
        cause_tx = cause_d.get('description', '') or ''
        dev_id   = cause_d.get('deviation_id')
        dev_desc = ''
        if dev_id:
            dev = self.db.get_deviation(dev_id)
            if dev:
                dev_desc = dev['description'] or ''
        return dev_desc, comp, cause_tx

    def _pos_near_cons_row(self, cons_id: int, popup_size):
        """Global top-left position to show a popup near cons_id's KON cell in
        the scenario table, clamped to the screen — so it opens right where
        the user is working instead of centered on screen. Falls back to the
        current cursor position if cons_id isn't visible in the table right
        now (e.g. filtered out by the current node/deviation/cause scope)."""
        row = next((r for r, m in enumerate(self._row_meta) if m[2] == cons_id), -1)
        if row >= 0:
            rect = self._table.visualRect(self._table.model().index(row, self._C_KON))
            anchor = self._table.viewport().mapToGlobal(rect.bottomLeft())
        else:
            anchor = QCursor.pos()
        scr = (QApplication.screenAt(anchor) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup_size.width(), popup_size.height()
        x = min(anchor.x(), scr.right() - pw)
        y = min(anchor.y() + 4, scr.bottom() - ph)
        return QPoint(max(scr.left(), x), max(scr.top(), y))

    def _open_chain_editor(self, cons_id: int, label_widget=None):
        """Open the consequence step picker dialog; refresh the cell on accept."""
        dev, comp, cause_tx = self._get_cons_context(cons_id)
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id,
            deviation=dev, comp_type=comp, cause_text=cause_tx,
            parent=self)
        dlg.move(self._pos_near_cons_row(cons_id, dlg.sizeHint()))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Rebuild risk cells (description changed)
            self._schedule_rebuild()

    def _open_recommendation_editor(self, cons_id):
        """Open the Rekommendation-column popup (2026-08-13, see
        NOTES.md) — ActionEditor already persists every change itself,
        so no Accepted/Rejected distinction is needed; just refresh the
        cell's summary text once the dialog closes either way."""
        # Deferred import: RecommendationEditorDialog still lives in
        # hazop.py, which imports ScenarioTablePanel from this module — a
        # module-level import here would be circular.
        from hazop import RecommendationEditorDialog
        dlg = RecommendationEditorDialog(self.db, cons_id, self)
        dlg.move(self._pos_near_cons_row(cons_id, dlg.sizeHint()))
        dlg.exec()
        self._refresh_recommendation_cell(cons_id)

    def _refresh_recommendation_cell(self, cons_id):
        """Fast in-place patch of every row's REK cell for cons_id,
        mirroring _update_row_text_only()'s pattern (same re-entrancy
        guard, same table.item()-is-None check to skip span-covered
        rows that have no real item of their own)."""
        if getattr(self, '_rebuilding', False):
            return
        acts = self.db.actions(cons_id)
        summary = self._recommendation_summary(acts)
        self._table.blockSignals(True)
        try:
            for row, meta in enumerate(self._row_meta):
                if meta[2] != cons_id:
                    continue
                item = self._table.item(row, self._C_REK)
                if item is not None:
                    item.setText(summary)
                    item.setForeground(QBrush(QColor('#8D9299' if not acts else '#000000')))
        finally:
            self._table.blockSignals(False)

    def _edit_extra(self, cons_id):
        # This slot runs on the call stack of a _LopaWidget's _extra_btn
        # QPushButton.clicked signal — that button is a live cell widget
        # embedded in self._table. dlg.exec() below pumps a NESTED Qt event
        # loop; any QTimer.singleShot(0, ...) already queued by
        # _schedule_rebuild() (24 call sites in this class) fires DURING
        # that nested loop, not after it. If it fires here, it calls
        # _rebuild() while THIS method (and the button's clicked handler)
        # is still executing underneath dlg.exec() on the C++ call stack.
        # _rebuild()'s setRowCount(0) then destroys the _LopaWidget/_extra_btn
        # that originated this very call — a use-after-free once dlg.exec()
        # returns and this frame resumes. Calling self._rebuild() directly
        # here (as this used to do) compounds the same risk a second time.
        # Every other dialog handler in this class defers via
        # _schedule_rebuild() for exactly this reason — do the same here.
        dlg = ReductionFactorsDialog(self.db, cons_id, self)
        dlg.exec()
        self._schedule_rebuild()

    # ── P&ID placement helpers ─────────────────────────────────────────────────

    def _update_ctx_bar(self, *_):
        """Refresh the sticky context bar to show Nod + Avvikelse of the topmost visible row."""
        if self._all_nodes or self._force_dev_column_visible or self._equipment_filter_id is not None:
            # Multiple nodes are interleaved in one table in "all nodes" mode,
            # or the host (e.g. HAZOPWorksheet, via always_show_deviation_
            # column()) forces the Avvikelse column always visible — either
            # way, a single "current node/deviation" context bar is redundant
            # with what the now-visible NOD/DEV columns already show per row,
            # and just costs an extra row of vertical space for the same info.
            self._ctx_bar.hide()
            return
        if not self._row_meta:
            self._ctx_bar.hide()
            return
        # Find the first row whose top edge is at or below the viewport top
        vp = self._table.viewport()
        top_row = self._table.rowAt(0)   # row at y=0 of the viewport
        if top_row < 0:
            top_row = 0
        if top_row >= len(self._row_meta):
            top_row = len(self._row_meta) - 1

        dev_id, cause_id, _, _ = self._row_meta[top_row]

        # Resolve Nod name from cause or deviation
        node_name = ''
        dev_desc  = ''
        try:
            if cause_id:
                cause = self.db.get_cause(cause_id)
                if cause:
                    node = self.db.get_node(cause['node_id'])
                    if node:
                        node_name = node['name']
            if dev_id:
                dev = self.db.get_deviation(dev_id)
                if dev:
                    dev_desc = dev['description']
        except Exception:
            pass

        if not node_name and not dev_desc:
            self._ctx_bar.hide()
            return

        parts = []
        if node_name:
            parts.append(f"🏭 <b>{node_name}</b>")
        if dev_desc:
            parts.append(f"⬡ {dev_desc}")
        self._ctx_bar.setText("   " + "     ›     ".join(parts))
        self._ctx_bar.show()

    def _update_lopa_risk(self, cons_id: int):
        """Targeted update of the SLUT cell when FA/IGN/Övriga changes.

        Avoids a full _rebuild() — only recalculates risk values for the
        rows belonging to *cons_id* and patches those cells in-place.
        """
        if getattr(self, '_rebuilding', False):
            return  # Table is mid-teardown/rebuild; a cell widget's focus-out signal
                     # fired reentrantly — ignore it, _rebuild() will reflect current
                     # state correctly once it completes.
        cons_d = self.db.get_consequence(cons_id)
        if not cons_d:
            return
        cons_d = dict(cons_d)
        cause_id = cons_d.get('cause_id')
        cause = self.db.get_cause(cause_id) if cause_id else None
        if not cause:
            return
        cause_d = dict(cause)
        freq    = self.db.cause_frequency_level(cause_d)
        rfs     = [dict(rf) for rf in self.db.reduction_factors(cons_id)]
        fa_active  = bool(cons_d.get('fa_active', 0))
        fa_rrf     = cons_d.get('fa_rrf', 10) or 10
        ign_active = bool(cons_d.get('ignition_active', 0))
        ign_rrf    = cons_d.get('ignition_rrf', 10) or 10
        all_sgs = [dict(s) for s in self.db.safeguards(cons_id)]

        cause_excl = set()
        for sg in all_sgs:
            ec = self.db.get_safeguard_excluded_causes(sg['id'])
            if cause_d['id'] in ec:
                cause_excl.add(sg['id'])

        self._table.blockSignals(True)
        try:
            for row, (_, cid_row, cid, sg_id) in enumerate(self._row_meta):
                if cid != cons_id:
                    continue
                cat_info = self._row_cat_info[row] if row < len(self._row_cat_info) else None

                # Build sg_rrf for this row
                if cat_info:
                    cat_id, sev_id, cat_name, cat_sev = cat_info
                    sev = cat_sev or 1
                    excl_for_cat = self.db.get_severity_excluded_sgs(sev_id)
                    active_sgs = [s for s in all_sgs
                                  if s['id'] not in excl_for_cat and s['id'] not in cause_excl]
                    sg_rrf = 1
                    for s in active_sgs:
                        sg_rrf *= (s.get('rrf') or 1)
                else:
                    sev = cons_d.get('severity') or 1
                    sg_rrf = 1
                    for s in all_sgs:
                        if s['id'] not in cause_excl:
                            sg_rrf *= (s.get('rrf') or 1)

                final_f, total_rrf, total_steps = total_freq_reduction(
                    freq, sg_rrf, fa_active, fa_rrf, ign_active, ign_rrf, rfs)
                _, bg_s, fg_s       = risk_info(final_f, sev)

                # Patched for every row now (2026-08-09, see NOTES.md) — same
                # fallback rationale as _add_row: bg_s/fg_s are already
                # computed unconditionally above, regardless of cat_info, so
                # a non-categorized consequence's SLUT cell used to go
                # stale/blank forever after an RRF change.
                slut_text = (f"−{total_steps} steg\n" if total_steps else "") + \
                            f"{freq_axis_label(final_f)}  {cons_axis_label(sev)}"
                rs = self._table.item(row, self._C_SLUT)
                if rs:
                    rs.setText(slut_text)
                    rs.setBackground(QBrush(QColor(bg_s)))
                    rs.setForeground(QBrush(QColor(fg_s)))
        finally:
            self._table.blockSignals(False)

    def _update_row_text_only(self, kind, id_, new_desc):
        """Fast path for a pure description-text edit: patch just the
        affected cell's text on every row referencing id_ (a cause/
        consequence/safeguard can appear on more than one row when spans
        merge same-id rows visually), without a full _rebuild().

        No _apply_spans() or _resize_rows() pass is needed: _apply_spans()
        groups rows purely by IDs in _row_meta (never by cell text — see its
        docstring), which a description edit never changes, and only
        _C_ORS/_C_KON ever need a height recompute for long/short text
        (_C_SG is a fixed one-line column per _ScenarioDelegate's wrap_cols).
        Mirrors _update_lopa_risk()'s established pattern (re-entrancy guard,
        blockSignals, patch in place, no rebuild).
        """
        if getattr(self, '_rebuilding', False):
            return  # mid-teardown/rebuild — the coming _rebuild() will show
                     # correct text anyway; avoid touching a row index that
                     # may no longer correspond to the same item.
        col = {'cause': self._C_ORS, 'consequence': self._C_KON,
               'safeguard': self._C_SG}.get(kind)
        if col is None:
            return
        field_idx = {'cause': 1, 'consequence': 2, 'safeguard': 3}[kind]
        needs_height_recalc = col in (self._C_ORS, self._C_KON)

        self._table.blockSignals(True)
        try:
            for row, meta in enumerate(self._row_meta):
                if meta[field_idx] != id_:
                    continue
                item = self._table.item(row, col)
                if item is not None:
                    item.setText(new_desc)
                if needs_height_recalc:
                    # Recompute the WHOLE row's height (_compute_row_height),
                    # not just what this one column now needs — otherwise
                    # clearing e.g. a consequence's text back to empty would
                    # shrink the row below what a long cause description (or
                    # the FA/Ant. column's fixed-height widget) in the SAME
                    # row still requires (2026-08-11, see NOTES.md).
                    self._table.setRowHeight(row, self._compute_row_height(row))
        finally:
            self._table.blockSignals(False)

    def _wrap_col_row_height(self, row, col):
        """Height a single ORS/KON cell alone needs for its current text —
        no longer used by _update_row_text_only() (see _compute_row_height,
        which considers the whole row), kept as a small standalone helper
        purely because TextOnlyEditFastPathTests asserts it still agrees
        with a real _resize_rows_manual() pass for a single column, so the
        two formulas can never silently drift apart."""
        table = self._table
        fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6
        item = table.item(row, col)
        text = item.text() if item is not None else ''
        if not text:
            return one_line_h
        w = table.columnWidth(col)
        if col == self._C_ORS:
            cell_w = max(40, w - 6)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, text)
            return _ORS_HEADER_H + max(one_line_h, rect.height() + 4)
        else:   # self._C_KON
            cell_w = max(40, w - _KON_CAT_W)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, text)
            return max(one_line_h, rect.height() + 4)

    def refresh_placed(self):
        """Repaint the table — kept as a thin call so its many existing
        call sites (after any data change that might affect what's shown)
        keep working unchanged; it no longer tracks P&ID placement state
        (2026-08-13, see NOTES.md: the P&ID canvas is now
        object-placement-only, so cause/consequence/safeguard rows have no
        "placed on P&ID" concept anymore)."""
        self._table.viewport().update()

    def select_cause(self, cause_id: int):
        """Scroll to and select the first row for *cause_id* in the scenario
        table. Never steals the current cell away from an active edit or a
        row the user has already navigated to on their own — this used to
        unconditionally force the ORS column current a moment after cause
        creation, which could yank focus out from under a user who had
        already clicked into that very row's KON cell to type a
        consequence (reported as "kan fortfarande inte lägga in text
        [i konsekvens]"). Always still scrolls the row into view."""
        for row, (dev_id, cid, cons_id, sg_id) in enumerate(self._row_meta):
            if cid == cause_id:
                already_editing = self._table.state() == QAbstractItemView.State.EditingState
                already_on_row = self._table.currentRow() == row
                if not already_editing and not already_on_row:
                    self._table.setCurrentCell(row, self._C_ORS)
                self._table.scrollTo(
                    self._table.model().index(row, self._C_ORS),
                    QAbstractItemView.ScrollHint.PositionAtCenter)
                return

    def active_edit_target(self):
        """Returns (editor, kind, id_) for the ORS/KON/SG cell currently
        being edited, or None — kind is 'cause'/'consequence'/'safeguard',
        id_ the matching cause_id/cons_id/sg_id. Used by PIDPanel's
        Shift+click-on-marker tag-insert feature (2026-08-13, see
        NOTES.md: "jag kan fortsätta skriva efter objektet ... jag
        hoppar inte ut ur textediteringsvyn") so it can insert into an
        already-open editor instead of disturbing it — same EditingState
        guard select_cause() above already uses to avoid stealing focus.
        kind/id_ let the caller also sync tagged_refs so the eventual
        saved text gets the same bold-tag-highlight treatment the
        drag-and-drop path already gives KON/SG cells.

        Resolves row/col from the editor's OWN 'editing_row'/'editing_col'
        properties (set by _ScenarioDelegate.createEditor) rather than
        self._table.currentItem()/currentRow() — those aren't reliably
        in sync with which cell is actually mid-edit (e.g. right after
        editItem()), while the editor's own properties always are, same
        as eventFilter() already relies on elsewhere in this class."""
        if self._table.state() != QAbstractItemView.State.EditingState:
            return None
        editor = self._table.focusWidget()
        if not isinstance(editor, QLineEdit):
            return None
        row, col = editor.property('editing_row'), editor.property('editing_col')
        if row is None or col is None:
            return None
        item = self._table.item(row, col)
        if item is None:
            return None
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta or meta[0] not in ('cause', 'consequence', 'safeguard'):
            return None
        return editor, meta[0], meta[1]

    def ors_cell_global_pos(self, dev_id):
        """Return global top-right corner of the first placeholder ORS cell for dev_id."""
        for row, meta in enumerate(self._row_meta):
            if meta[0] == dev_id:
                rect = self._table.visualRect(
                    self._table.model().index(row, self._C_ORS))
                return self._table.viewport().mapToGlobal(rect.topRight())
        return None

    def _cell_has_item(self, row, col):
        """Returns True only when the cell actually has a placeable item ID."""
        if row >= len(self._row_meta):
            return False
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS:
            return cause_id is not None
        if col == self._C_KON:
            return cons_id is not None
        if col == self._C_SG:
            return sg_id is not None
        return False

    def _on_table_context_menu(self, pos):
        col = self._table.columnAt(pos.x())
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._row_meta):
            return
        if col not in (self._C_ORS, self._C_KON, self._C_SG):
            return
        if not self._cell_has_item(row, col):
            return  # no item here at all — e.g. safeguard row with no safeguard yet
        menu = QMenu(self)
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                menu.addSeparator()
                a_chain = menu.addAction(_icon('clipboard'), "Redigera konsekvenskedja (Del1–Del5)…")
                a_chain.triggered.connect(lambda: self._open_chain_editor(cons_id))
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][3]
            if sg_id is not None:
                menu.addSeparator()
                a_rrf = menu.addAction(_icon('settings'), "Ändra RRF...")
                a_rrf.triggered.connect(lambda: self._show_rrf_popup(row, sg_id))
        # Feature 4: clone scenario to another deviation
        if col == self._C_ORS and row < len(self._row_meta):
            cause_id = self._row_meta[row][1]
            if cause_id is not None:
                menu.addSeparator()
                a_clone = menu.addAction(_icon('clipboard'), "Duplicera scenario till annan avvikelse…")
                a_clone.triggered.connect(lambda: self._clone_scenario(cause_id))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_cell_clicked(self, row, col):
        if col == self._C_ORS and row < len(self._row_meta):
            dev_id, cause_id = self._row_meta[row][0], self._row_meta[row][1]
            if cause_id is not None:
                self.item_selected.emit(CAUSE_T, cause_id)
            elif dev_id is not None:
                # Empty placeholder ORS cell (2026-08-12, see NOTES.md) —
                # open the same CauseObjectPopup as "+ Ny orsak" instead of
                # starting inline text edit, so creating a cause behaves
                # identically regardless of entry point.
                idx = self._table.model().index(row, col)
                gp = self._table.viewport().mapToGlobal(self._table.visualRect(idx).topLeft())
                self._add_cause_via_plus_row(dev_id, global_pos=gp)
                return
            # Feature 7: single-click on already-current ORS cell → start edit
            if self._table.currentRow() == row and self._table.currentColumn() == col:
                QTimer.singleShot(200, lambda r=row, c=col: self._try_start_edit(r, c))
            return
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self.item_selected.emit(CONS_T, cons_id)
            # Feature 7 (2026-08-07): single-click on already-current KON
            # cell → start inline edit, same as ORS/SG — "trycka direkt på
            # konsekvensen för att redigera den direkt där" (NOTES.md).
            # Double-click still opens the chain wizard (_on_cell_double_clicked).
            if self._table.currentRow() == row and self._table.currentColumn() == col:
                QTimer.singleShot(200, lambda r=row, c=col: self._try_start_edit(r, c))
            return
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][3]
            if sg_id is not None:
                self.item_selected.emit(SG_T, sg_id)
            # Feature 7: single-click on already-current SG cell → start edit
            if self._table.currentRow() == row and self._table.currentColumn() == col:
                QTimer.singleShot(200, lambda r=row, c=col: self._try_start_edit(r, c))
            return
        if col == self._C_REK and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self._open_recommendation_editor(cons_id)
            return
        if col != self._C_RFORE:
            return
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta or meta[0] not in ('risk_click', 'risk_click_cat'):
            return

        if meta[0] == 'risk_click_cat':
            _, cause_id, cons_id, cat_id, sev_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(cur_freq, cur_cons, self)
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id, catid=cat_id:
                    self._apply_risk_from_matrix_cat(caid, coid, catid, f, c))
        else:
            _, cause_id, cons_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(cur_freq, cur_cons, self)
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id:
                    self._apply_risk_from_matrix(caid, coid, f, c))

        # Position popup: prefer above the cell, fall back to below if off-screen
        popup.adjustSize()
        cell_rect  = self._table.visualItemRect(item)
        anchor     = self._table.viewport().mapToGlobal(cell_rect.topLeft())
        _scr       = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        screen     = _scr.availableGeometry()
        ph         = popup.sizeHint().height()
        pw         = popup.sizeHint().width()
        # Try above first
        y = anchor.y() - ph - 4
        if y < screen.top():
            y = anchor.y() + cell_rect.height() + 4   # fall back: below
        x = max(screen.left(), min(anchor.x(), screen.right() - pw))
        popup.move(x, y)
        popup.exec()

    def _apply_risk_from_matrix(self, cause_id, cons_id, new_freq, new_cons):
        self.db.update_cause(cause_id, likelihood=new_freq)
        cons = self.db.get_consequence(cons_id)
        if cons:
            self.db.update_consequence(
                cons_id, cons['description'], new_cons, cons['category'] or '')
        self._schedule_rebuild()

    def _apply_risk_from_matrix_cat(self, cause_id, cons_id, cat_id, new_freq, new_cons):
        """Bidirectional: update frequency on cause and category severity on consequence."""
        self.db.update_cause(cause_id, likelihood=new_freq)
        self.db.set_consequence_severity(cons_id, cat_id, new_cons)
        self._schedule_rebuild()

    def _show_cat_sg_popup(self, sev_id, all_sgs):
        """Open the safeguard-selection popup for a category row."""
        popup = CatSGSelectionPopup(self.db, sev_id, all_sgs, self)
        popup.adjustSize()
        gp  = QCursor.pos()
        scr = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(gp.x(), scr.right() - pw)
        y = min(gp.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        if popup.exec() == QDialog.DialogCode.Accepted:
            self._schedule_rebuild()

    def _on_cell_double_clicked(self, item):
        if item is None:
            return
        row = item.row()
        col = item.column()
        # Double-click starts inline edit — consistent across ORS/KON/SG
        # (reported feedback: KON used to open the "Konsekvenskedja" wizard
        # instead, which felt out of place and inconsistent with ORS/SG's
        # plain edit-in-place). The chain wizard remains reachable via the
        # right-click context menu (_open_chain_editor, unchanged there).
        if col in (self._C_ORS, self._C_KON, self._C_SG):
            if not bool(item.flags() & Qt.ItemFlag.ItemIsEditable):
                # A KON cell is always backed by a real (if blank)
                # consequence row — every cause gets one auto-created —
                # so it's never left non-editable. An SG cell IS, since
                # safeguards aren't auto-created (2026-08-17, see
                # NOTES.md "dubbelklicka på safeguards"): double-click on
                # an empty one used to just do nothing, unlike KON's
                # "double-click to add" feel. Quick-add one here instead,
                # same no-popup straight-to-inline-edit path Enter/the
                # "+" row already use.
                if col == self._C_SG:
                    cons_id = self._row_meta[row][2] if row < len(self._row_meta) else None
                    if cons_id is not None:
                        self._quick_add_safeguard(cons_id)
                return
            self._table.setFocus()
            self._table.edit(self._table.model().index(row, col))

    def _show_rrf_popup(self, row, sg_id):
        """Called from context menu — centre on the cell."""
        item = self._table.item(row, self._C_SG)
        if item:
            cr = self._table.visualItemRect(item)
            gp = self._table.viewport().mapToGlobal(cr.center())
        else:
            gp = self._table.viewport().mapToGlobal(self._table.viewport().rect().center())
        self._show_rrf_popup_at(row, sg_id, gp)

    def _show_rrf_popup_at(self, row, sg_id, global_pos):
        """Show RRF popup near global_pos, keeping it within the screen."""
        sg = self.db.get_safeguard(sg_id)
        sg_d        = dict(sg) if sg else {}
        current_rrf     = int(sg_d.get('rrf', 1))
        current_sg_type = sg_d.get('sg_type', 'Övrigt') or 'Övrigt'

        # Use extended popup when consequence has category assessments
        item          = self._table.item(row, self._C_SG)
        cat_pop_data  = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
        cause_pop_list = item.data(Qt.ItemDataRole.UserRole + 4) if item else None

        if cat_pop_data or cause_pop_list:
            _cons_id, sev_cat_list = cat_pop_data if cat_pop_data else (None, [])
            popup = SgRRFCategoryPopup(
                self.db, sg_id, current_rrf, current_sg_type,
                sev_cat_list, cause_pop_list or [], self)
        else:
            popup = RRFPopup(current_rrf, current_sg_type, self)
            popup.rrf_selected.connect(
                lambda v, t, r=row, sid=sg_id: self._update_sg_rrf(r, sid, v, t))

        popup.adjustSize()
        _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = _scr.availableGeometry()
        pw = popup.sizeHint().width()
        ph = popup.sizeHint().height()
        x = global_pos.x()
        y = global_pos.y() + 6
        if y + ph > screen.bottom():
            y = global_pos.y() - ph - 6
        if x + pw > screen.right():
            x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top() + 4, y)
        popup.move(x, y)
        if popup.exec() == QDialog.DialogCode.Accepted:
            self._schedule_rebuild()

    # ORS strip/frequency-row layout constants — shared between paint()
    # (_PidDelegate, below) and the click hit-test in eventFilter() so the
    # drawn zones and the clickable zones can never drift apart. This file
    # has a documented history of exactly that kind of desync between
    # paint code and geometry code computed elsewhere (see NOTES.md's
    # notes on _wrap_col_row_height/_resize_rows_manual needing to stay in
    # sync with paint) — keeping each calculation in one place avoids
    # repeating it.
    _ORS_DOTS_MARGIN = 22   # room reserved for the comment dot at the strip's right edge
    _ORS_FREQ_MAX_W  = 90   # sane ceiling; real frequency strings are short ("3/år", "1.2e-3/år")
    _ORS_FREQ_MARGIN = 6    # right-edge margin for the frequency row within the orsaksfält

    def _ors_freq_label(self, freq_val, base_freq_per_year):
        """The exact frequency text shown in the orsaksfält's frequency
        row. Split out so the width calc below and the paint code always
        agree on what string they're sizing/drawing."""
        if freq_val is None:
            return None
        if base_freq_per_year is not None:
            bfv = float(base_freq_per_year)
            if bfv >= 0.1:     return f"{bfv:.2g}/år"
            elif bfv >= 0.001: return f"{bfv:.3g}/år"
            else:              return f"{bfv:.1e}".replace('e-0', 'e-') + "/år"
        return freq_axis_label(freq_val)

    def _ors_tag_zone_width(self, tag_x, cell_right):
        """Width of the ORS tag strip's tag zone.

        2026-08-11: "tag numret klipps av ... högerställ frekvens" — the
        tag used to be capped at the fixed _cause_obj_w divider width no
        matter how much space was actually free. _cause_obj_w (the
        user-draggable divider, still used for the drag handle and its
        persisted width) stays in play as a FLOOR so dragging it can still
        only ever make the promised tag zone wider, never narrower than
        what the user last set.

        2026-08-18: frequency moved out of this strip entirely (see
        _ors_freq_zone_geometry below), so the tag zone now only needs to
        leave room for the comment dot at the strip's right edge, not a
        frequency zone too — simpler than the geometry this used to share
        a single method with."""
        return max(self._cause_obj_w, cell_right - self._ORS_DOTS_MARGIN - tag_x)

    def _ors_freq_zone_geometry(self, item, row_left, row_right):
        """Return (freq_zone_x, freq_zone_w, freq_str) for the frequency
        row now drawn at the TOP of the orsaksfält (description area),
        right-aligned — moved out of the object-identity tag strip above
        it (2026-08-18, see NOTES.md: "Frekvensen ... skall flyttas från
        objektbannern till orsaksfältet då det hör hemma mer här" — every
        orsak has its own frequency, causes.likelihood/base_frequency, so
        it belongs with the cause's own content, not the shared object
        banner). `row_left`/`row_right` are the frequency row's own
        horizontal extent (the orsaksfält's, not the whole cell's)."""
        freq_val = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
        base_freq_per_year = item.data(Qt.ItemDataRole.UserRole + 5) if item else None
        freq_str = self._ors_freq_label(freq_val, base_freq_per_year)
        freq_zone_w = 0
        if freq_str:
            ff = QFont(self._table.font())
            ff.setPointSize(max(6, self._table.font().pointSize() - 1))
            freq_zone_w = min(QFontMetrics(ff).horizontalAdvance(freq_str) + 6,
                              self._ORS_FREQ_MAX_W)
        freq_zone_x = row_right - self._ORS_FREQ_MARGIN - freq_zone_w
        return freq_zone_x, freq_zone_w, freq_str

    def _sg_row_height(self, base_font):
        """Single source of truth for a safeguard row's height — used by
        _ScenarioDelegate._size_hint_impl, _compute_row_height AND
        _PidDelegate.paint()'s SG branch, so all three can never disagree
        about how tall a safeguard row is (same rule as _ORS_STRIP_H's own
        docstring elsewhere in this file).

        A consequence with several safeguards stacks one physical table
        row per safeguard (see _apply_spans/_add_row), so the per-row
        height directly multiplies out across however many are added —
        2026-08-18 follow-up ("krymper höjden på safeguards ... för att
        spara plats när man lägger till flera safeguards"). Text stays
        the SAME size as every other cell (a first attempt also shrank
        the font, but Anton clarified it's the CELL height that should
        shrink, not the text) — only the padding around it is trimmed to
        the bare minimum a line of text needs to avoid clipping, which is
        a much smaller saving than font-shrinking would give but keeps
        safeguard text exactly as readable as everywhere else.

        `+ 4`, not `+ 2`: `_on_font_size_changed` sets the table's own
        `verticalHeader().setMinimumSectionSize(fm.height() + 4)`, a hard
        floor `setRowHeight()` can't go below regardless of what this
        function returns — returning anything smaller just meant the
        real, Qt-enforced height silently differed from what this
        "single source of truth" claimed."""
        return QFontMetrics(base_font).height() + 4

    def _show_cause_obj_popup(self, row, cause_id, global_pos):
        """A plain click on the ORS tag zone opens just a tag+type
        popup (2026-08-14, see NOTES.md) — the full avvikelse-context +
        standard-cause CauseObjectPopup is still reachable, unchanged,
        from the detail panel (_edit_cause_obj) and quick-add
        (_quick_add_cause). CauseTagPopup has no OK button (2026-08-18)
        — it commits live and dismisses itself on Escape/outside click,
        so it's shown non-modally instead of exec()'d."""
        item      = self._table.item(row, self._C_ORS)
        obj_data  = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        comp_type, comp_tag = obj_data if obj_data else ('', '')

        popup = CauseTagPopup(self.db, comp_type, comp_tag, parent=self)
        popup.committed.connect(
            lambda ct, tg, r=row, cid=cause_id:
                self._apply_cause_obj(r, cid, ct, tg, '', None))
        popup.adjustSize()
        _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = _scr.availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x, y   = global_pos.x(), global_pos.y() + 6
        if y + ph > screen.bottom(): y = global_pos.y() - ph - 6
        if x + pw > screen.right():  x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top()  + 4, y)
        popup.move(x, y)
        popup.show()

    def _apply_cause_obj(self, row, cause_id, comp_type, comp_tag, description, frequency):
        # Live tag link (2026-08-13, see NOTES.md: "taggen är kopplad
        # till objekten i orsaken ... ändrar jag i hazop scenario
        # ändras namnet på p&id och vice versa"). Two cases:
        # - Already linked to a real object and the typed tag text now
        #   differs from that object's own tag → RENAME the object
        #   itself (equipment_catalog), everywhere in the app, not just
        #   this cell's frozen comp_tag copy.
        # - Not linked yet, but the typed tag happens to match an
        #   existing object's tag exactly → just link to it (no rename,
        #   nothing to rename FROM).
        cause = self.db.get_cause(cause_id)
        old_equipment_id = cause.get('equipment_id') if cause else None
        new_tag = (comp_tag or '').strip()
        equipment_id = old_equipment_id
        renamed = False
        if old_equipment_id is not None:
            old_eq = self.db.get_equipment_by_id(old_equipment_id)
            if old_eq and new_tag and new_tag != (old_eq.get('tag') or ''):
                self.db.update_equipment_item(
                    old_equipment_id, new_tag, old_eq.get('prefix') or '',
                    old_eq.get('equipment_type') or comp_type, old_eq.get('description') or '')
                self.equipment_renamed.emit()
                renamed = True
        elif new_tag:
            match = self.db.get_equipment_by_tag(new_tag)
            equipment_id = match['id'] if match else None

        # Do all DB writes first — learning is handled inside update_cause
        self.db.update_cause(cause_id, comp_type=comp_type, comp_tag=comp_tag,
                              equipment_id=equipment_id)
        if description:
            kwargs = {'description': description}
            if frequency is not None:
                kwargs['base_frequency'] = frequency
            self.db.update_cause(cause_id, **kwargs)
        if description or renamed:
            # Description changed, or a rename may affect OTHER rows
            # sharing the same equipment_id too → full rebuild (item
            # refs are stale after rebuild anyway).
            self._schedule_rebuild()
        else:
            # Only this row's own tag/type changed → update item in-place
            # with signals blocked.
            self._table.blockSignals(True)
            item = self._table.item(row, self._C_ORS)
            if item:
                item.setData(Qt.ItemDataRole.UserRole + 2, (comp_type, comp_tag))
            self._table.blockSignals(False)
            self._table.viewport().update()

    def _update_sg_rrf(self, row, sg_id, rrf, sg_type=None):
        self.db.update_safeguard(sg_id, rrf=rrf, sg_type=sg_type)
        self._schedule_rebuild()

    def _on_ors_frequency_picked(self, cause_id, f_level, numeric):
        """FrequencyPickerPopup.frequency_selected handler for the ORS
        strip's frequency zone click (2026-08-14, see NOTES.md). Exactly
        one of f_level/numeric is non-None — a preset sets the matrix
        F-level and clears any manual numeric override; a custom value
        sets base_frequency directly (causes.likelihood is then derived
        from it, see _sync_f_levels_from_base_frequency)."""
        if f_level is not None:
            self.db.update_cause(cause_id, likelihood=f_level, base_frequency=None)
        else:
            self.db.update_cause(cause_id, base_frequency=numeric)
        self._schedule_rebuild()

    def _on_deviation_picked(self, cause_id, node_id, dev_id, new_desc):
        """DeviationPickerPopup.deviation_picked handler for the
        Avvikelse cell click (2026-08-14, see NOTES.md). Exactly one of
        dev_id/new_desc is non-None — a preset moves the cause directly;
        free text gets-or-creates that deviation first."""
        target_dev_id = dev_id if dev_id is not None else \
            self.db.get_or_create_deviation(node_id, new_desc)
        self.db.move_cause_to_deviation(cause_id, target_dev_id)
        self.structure_changed.emit()
        self._schedule_rebuild()

    def _open_comment_popup(self, row, cause_id, global_pos):
        """Floating comment editor for a cause row."""
        current = self.db.get_cause_comment(cause_id) or ''
        popup = QDialog(self)
        popup.setWindowTitle("Kommentar")
        popup.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        popup.setMinimumWidth(340)
        lay = QVBoxLayout(popup)
        lay.setContentsMargins(10, 10, 10, 10)
        lbl = QLabel("💬  Kommentar till orsaksraden:")
        lbl.setStyleSheet("font-weight:bold; font-size:10px; color:#8D9299;")
        lay.addWidget(lbl)
        txt = QTextEdit(current)
        txt.setPlaceholderText("Ange notering, beslut eller referens…")
        txt.setFixedHeight(CONFIG['H_EDIT_LG'])
        lay.addWidget(txt)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(popup.accept)
        btns.rejected.connect(popup.reject)
        lay.addWidget(btns)
        popup.adjustSize()
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = min(global_pos.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        if popup.exec() == QDialog.DialogCode.Accepted:
            self.db.set_cause_comment(cause_id, txt.toPlainText().strip())
            self._schedule_rebuild()

    # ── Feature 4: clone scenario ─────────────────────────────────────────────
    def _clone_scenario(self, cause_id):
        cause = self.db.get_cause(cause_id)
        if not cause: return
        cause = dict(cause)   # sqlite3.Row → dict so .get() works
        dev_id = cause['deviation_id']
        node_id = cause['node_id']
        devs = [d for d in self.db.deviations(node_id) if d['id'] != dev_id]
        if not devs:
            QMessageBox.information(self, 'Duplicera scenario',
                'Inga andra avvikelser att duplicera till på denna nod.')
            return
        items = [d['description'] for d in devs]
        choice, ok = QInputDialog.getItem(self, 'Duplicera scenario',
            'Välj avvikelse att kopiera scenario till:', items, 0, False)
        if not ok: return
        target_dev = next(d for d in devs if d['description'] == choice)
        # Copy cause
        new_cid = self.db.add_cause(target_dev['id'])
        self.db.update_cause(new_cid,
            description=cause['description'],
            comp_type=cause.get('comp_type', ''),
            comp_tag=cause.get('comp_tag', ''))
        # Copy consequences + safeguards
        for cons in self.db.consequences(cause_id):
            new_oid = self.db.copy_consequence(cons['id'], new_cid)
        self.new_item_created.emit(CAUSE_T, new_cid)
        self._schedule_rebuild()

    # ── Enter-tangent: snabblägg-till ─────────────────────────────────────────

    def eventFilter(self, obj, event):
        ctrl = bool(event.type() == QEvent.Type.KeyPress and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # Viewport mouse: drag divider between obj-zone and text in ORS column.
        # This handle intentionally still tracks _cause_obj_w itself (the
        # user's persisted minimum), NOT the wider tag_zone_w the strip may
        # actually render at (2026-08-11, see _ors_tag_zone_width) — the
        # handle is where the user asked the floor to be, and dragging it
        # only ever raises or lowers that floor, regardless of how much
        # extra elbow room a given row's tag currently happens to have.
        if obj is self._table.viewport() and event.type() == QEvent.Type.MouseMove:
            pos = event.pos()
            if self._drag_obj_w_active:
                delta = pos.x() - self._drag_obj_w_start_x
                self._cause_obj_w = max(30, min(300, self._drag_obj_w_start_w + delta))
                self._table.viewport().update()
                return True
            div_x = self._table.columnViewportPosition(self._C_ORS) + self._cause_obj_w
            if abs(pos.x() - div_x) <= 4:
                self._table.viewport().setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self._table.viewport().unsetCursor()

        if obj is self._table.viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            if self._drag_obj_w_active:
                self._drag_obj_w_active = False
                self._table.viewport().unsetCursor()
                self.db.set_config('cause_obj_w', str(self._cause_obj_w))
                return True

        # ── Drag: record press position for potential drag-start ─────────────────
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            row = self._table.rowAt(pos.y())
            col = self._table.columnAt(pos.x())
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                self._drag_press_pos = pos
                self._drag_press_row = row
                self._drag_press_col = col
            else:
                self._drag_press_pos = None

        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonRelease):
            self._drag_press_pos = None

        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseMove and
                self._drag_press_pos is not None and
                event.buttons() & Qt.MouseButton.LeftButton):
            dist = (event.pos() - self._drag_press_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._drag_press_pos = None
                self._start_drag(self._drag_press_row, self._drag_press_col,
                                 event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                return True

        # ── Drop events on table ──────────────────────────────────────────────
        # Qt/PyQt6 delivers DragEnter/DragMove/Drop to whichever widget is
        # actually under the cursor — for a QAbstractItemView-based widget
        # like QTableWidget that's the VIEWPORT, not the outer table widget
        # (the viewport is the real scrollable surface; the outer widget is
        # just its frame). Checking only `obj is self._table` here meant
        # this branch never matched for a REAL cross-widget drag (e.g. the
        # Shift-drag-a-tag-from-P&ID feature), so the drop silently did
        # nothing — this only worked at all in tests because they called
        # _handle_drop() directly, bypassing event delivery entirely (see
        # NOTES.md "Drag-and-drop till KON fungerade inte i praktiken").
        # Accept either object defensively rather than betting on one.
        _drop_targets = (self._table, self._table.viewport())
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:'):
                self._handle_drop(event, source_obj=obj)
                return True

        # Viewport mouse: detect LEFT-click in icon strip or RRF row
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            # Check for divider drag start before any other click handling
            div_x = self._table.columnViewportPosition(self._C_ORS) + self._cause_obj_w
            if abs(pos.x() - div_x) <= 4:
                self._drag_obj_w_active = True
                self._drag_obj_w_start_x = pos.x()
                self._drag_obj_w_start_w = self._cause_obj_w
                self._table.viewport().setCursor(Qt.CursorShape.SizeHorCursor)
                return True
            col = self._table.columnAt(pos.x())
            row = self._table.rowAt(pos.y())

            # ➕ In-cell "+" quick-add badge — bottom-right corner of the last
            # row of a group (2026-08-12 redesign, see NOTES.md: replaces the
            # old separate blank "+" row, which "tar upp alldeles för mycket
            # plats"). Checked here, ahead of the column's other right-edge
            # zones (RRF badge, clone/comment icons, …), so a click landing
            # specifically inside the small badge box wins; anywhere else in
            # those wider zones falls through to them unaffected.
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                plus = self._row_plus_cols.get(row, {}).get(col)
                if plus is not None:
                    idx = self._table.model().index(row, col)
                    cr  = self._table.visualRect(idx)
                    sz  = _PLUS_BADGE_SIZE
                    badge = QRect(cr.right() - sz - 2, cr.bottom() - sz - 2, sz, sz)
                    if badge.contains(pos):
                        kind, group_id = plus
                        if group_id is not None:
                            if kind == 'cause':
                                gp = self._table.viewport().mapToGlobal(pos)
                                self._add_cause_via_plus_row(group_id, global_pos=gp)
                            elif kind == 'consequence':
                                self._add_consequence_via_plus_row(group_id)
                            elif kind == 'safeguard':
                                self._add_safeguard_via_plus_row(group_id)
                        return True

            # Avvikelse cell click — reassign which deviation the row's
            # cause belongs to (2026-08-14, see NOTES.md: "klockan man
            # på avvikelsen justerar man avvikelsen"). Only meaningful
            # once the row actually has a cause (placeholder "no causes
            # yet" rows have cause_id None and nothing to move).
            if row >= 0 and col == self._C_DEV and row < len(self._row_meta):
                dev_id, cause_id = self._row_meta[row][0], self._row_meta[row][1]
                if cause_id is not None and dev_id is not None:
                    node_id = self.db.get_deviation(dev_id)['node_id']
                    gp = self._table.viewport().mapToGlobal(pos)
                    popup = DeviationPickerPopup.create_positioned(
                        self.db, node_id, dev_id, gp, parent=self)
                    popup.deviation_picked.connect(
                        lambda picked_dev_id, new_desc, cid=cause_id, nid=node_id:
                            self._on_deviation_picked(cid, nid, picked_dev_id, new_desc))
                    popup.exec()
                    return True

            # Object-tag zone click — left (0 .. tag_zone_w) of cause cell,
            # within the tag strip's own height. tag_zone_w is computed the
            # same way paint() computes it (via _ors_tag_zone_width) rather
            # than the raw _cause_obj_w divider width — otherwise, once a
            # long tag's DRAWN width expands past the old fixed cap
            # (2026-08-11 fix), clicking on the now-visible-but-previously-
            # uncounted part of the tag would silently do nothing (stale
            # hit-test rectangle). Still clickable even when the Utrustning
            # column is visible and the tag TEXT itself isn't drawn here
            # (2026-08-18, see NOTES.md "dubbla objektbanners") — only the
            # duplicate DISPLAY was removed, not the edit affordance.
            if (row >= 0 and col == self._C_ORS and row < len(self._row_meta) and
                    pos.y() - self._table.rowViewportPosition(row) < _ORS_STRIP_H):
                col_x      = self._table.columnViewportPosition(col)
                obj_start  = col_x
                cell_right = col_x + self._table.columnWidth(col) - 1
                tag_zone_w = self._ors_tag_zone_width(obj_start, cell_right)
                obj_end    = obj_start + tag_zone_w
                if obj_start <= pos.x() < obj_end:
                    cause_id = self._row_meta[row][1]
                    if cause_id is not None:
                        gp = self._table.viewport().mapToGlobal(pos)
                        self._show_cause_obj_popup(row, cause_id, gp)
                    return True

            # Frequency zone click — floats over the description's own
            # first line now, below the tag strip (2026-08-18, see
            # NOTES.md "Frekvensen ... hör hemma mer här" / follow-up
            # "hamnar nu på olika rader"), using the exact same
            # freq_zone_x/freq_zone_w geometry paint() draws the text with
            # (2026-08-14, see NOTES.md: "klickar man på frekvens skall
            # man kunna justera frekvens"). Restricted to that first
            # line's own height so it doesn't also swallow clicks on the
            # tag strip above it or later description lines below it.
            row_top = self._table.rowViewportPosition(row)
            if (row >= 0 and col == self._C_ORS and row < len(self._row_meta) and
                    _ORS_HEADER_H <= pos.y() - row_top < _ORS_HEADER_H + _ORS_STRIP_H):
                cause_id = self._row_meta[row][1]
                if cause_id is not None:
                    col_x      = self._table.columnViewportPosition(col)
                    cell_right = col_x + self._table.columnWidth(col) - 1
                    item       = self._table.item(row, col)
                    freq_zone_x, freq_zone_w, freq_str = self._ors_freq_zone_geometry(
                        item, col_x + 2, cell_right - 2)
                    if freq_str and freq_zone_x <= pos.x() < freq_zone_x + freq_zone_w:
                        gp = self._table.viewport().mapToGlobal(pos)
                        cur_f_level = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
                        cur_numeric = item.data(Qt.ItemDataRole.UserRole + 5) if item else None
                        popup = FrequencyPickerPopup.create_positioned(
                            gp, current_f_level=cur_f_level,
                            current_numeric_freq=cur_numeric, parent=self)
                        popup.frequency_selected.connect(
                            lambda f_level, numeric, cid=cause_id:
                                self._on_ors_frequency_picked(cid, f_level, numeric))
                        popup.exec()
                        return True

            # 💬 Comment + 📋 Clone icon clicks in ORS cell (inline, replaces context menu)
            if row >= 0 and col == self._C_ORS and row < len(self._row_meta):
                cause_id = self._row_meta[row][1]
                if cause_id is not None:
                    ci = self._table.model().index(row, col)
                    cr = self._table.visualRect(ci)
                    # rightmost zones: [📋clone:18][💬comment:20][🟢status:18]
                    clone_right  = cr.right() - 18 - 20        # start of 📋 zone
                    cmt_right    = cr.right() - 18              # start of 💬 zone
                    if pos.x() >= clone_right and pos.x() < cmt_right:
                        # 📋 Clone scenario
                        self._clone_scenario(cause_id)
                        return True
                    if pos.x() >= cmt_right and pos.x() < cr.right() - 18:
                        # 💬 Comment popup
                        self._open_comment_popup(row, cause_id,
                                                  self._table.viewport().mapToGlobal(pos))
                        return True

            # 📊 Category badge click in KON cell
            if row >= 0 and col == self._C_KON and row < len(self._row_meta):
                col_x     = self._table.columnViewportPosition(col)
                cat_start = col_x
                cat_end   = cat_start + _KON_CAT_W
                if cat_start <= pos.x() < cat_end:
                    cons_id = self._row_meta[row][2]
                    if cons_id is not None:
                        gp = self._table.viewport().mapToGlobal(pos)
                        popup = ConsCategoryMatrixPopup(self.db, cons_id, self)
                        popup.adjustSize()
                        scr    = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
                        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
                        x = min(gp.x(), scr.right() - pw)
                        y = min(gp.y() + 4, scr.bottom() - ph)
                        popup.move(max(scr.left(), x), max(scr.top(), y))
                        if popup.exec() == QDialog.DialogCode.Accepted:
                            self._schedule_rebuild()
                    return True

            # ⚡ RRF badge click — right _RRF_W pixels of safeguard cell
            if (row >= 0 and col == self._C_SG and row < len(self._row_meta)):
                sg_id = self._row_meta[row][3]
                if sg_id is not None:
                    cell_idx = self._table.model().index(row, col)
                    cr = self._table.visualRect(cell_idx)
                    if pos.x() >= cr.right() - _RRF_W:
                        gp = self._table.viewport().mapToGlobal(pos)
                        self._show_rrf_popup_at(row, sg_id, gp)
                        return True

        # Delegate inline editor (regular cell in edit mode)
        if (isinstance(obj, QLineEdit) and
                obj.property('editing_row') is not None and
                obj.property('sg_id') is None):
            if event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    row = obj.property('editing_row')
                    col = obj.property('editing_col')
                    self._delegate.commitData.emit(obj)
                    self._delegate.closeEditor.emit(obj, QStyledItemDelegate.EndEditHint.NoHint)
                    if ctrl:
                        self._ctrl_enter(row, col)
                    return True  # always consume Enter in editor — prevents table-level handler

        # Table-level keyboard shortcuts
        if obj is self._table and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                row = self._table.currentRow()
                col = self._table.currentColumn()
                self._ctrl_enter(row, col)
                return True
            if (event.key() == Qt.Key.Key_C and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._copy_row_to_clipboard(self._table.currentRow())
                return True
            if event.key() == Qt.Key.Key_Delete:
                self._delete_current_item()
                return True
            # F2 or any printable key → start inline edit on ORS/SG cells (feature 7)
            if event.key() == Qt.Key.Key_F2:
                row = self._table.currentRow()
                col = self._table.currentColumn()
                self._try_start_edit(row, col)
                return True
        return False

    def _ctrl_enter(self, row, col):
        """Enter on table (not in editor): create a new sibling at the same level."""
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, _sg_id = self._row_meta[row]
        if col in (self._C_ORS, self._C_NOD, self._C_DEV):
            if dev_id is not None:
                self._quick_add_cause(dev_id)
        elif col in (self._C_KON, self._C_RFORE):
            if cause_id is not None:
                self._quick_add_consequence(cause_id)
        else:
            if cons_id is not None:
                self._quick_add_safeguard(cons_id)

    def _on_enter_after_edit(self):
        row = self._enter_row
        if row < 0 or row >= len(self._row_meta):
            return
        item = self._table.item(row, self._enter_col)
        is_editable = item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEditable)
        if is_editable and not self._last_enter_committed:
            return
        self._last_enter_committed = False
        # Directly add next item based on column (no menu, feature 3)
        self._ctrl_enter(row, self._enter_col)

    def _show_quick_add(self, row, dev_id, cause_id, cons_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        dev = self.db.get_deviation(dev_id) if dev_id else None
        dev_name = dev['description'] if dev else '?'

        menu = QMenu(self)
        menu.addSection("Lägg till i hierarkin")
        menu.addAction(_icon('settings'), f'Ny orsak under avvikelse  [{dev_name}]',
                       lambda: self._quick_add_cause(dev_id))
        menu.addAction(_icon('warning'), "Ny konsekvens på denna orsak",
                       lambda: self._quick_add_consequence(cause_id))
        sg_action = menu.addAction(_icon('shield'), "Ny safeguard på denna konsekvens",
                       lambda: self._quick_add_safeguard(cons_id))
        sg_action.setEnabled(cons_id is not None)

        idx   = self._table.model().index(row, self._C_ORS)
        rect  = self._table.visualRect(idx)
        pos   = self._table.viewport().mapToGlobal(rect.bottomLeft())
        menu.exec(pos)

    def _quick_add_cause(self, deviation_id, global_pos=None):
        """Reported feedback (2026-08-12, see NOTES.md): a new/empty cause
        in HAZOP scenario should open the same compact CauseObjectPopup
        ("Orsak på P&ID" — Tag + Typ + Standardorsaker) already used
        everywhere a cause's tag/type/description is edited, instead of
        the larger StandardCausesPickerPopup this used to open. Reused by
        both the "+ Ny orsak" affordance and clicking an empty ORS
        placeholder cell (_on_cell_clicked), so both entry points behave
        identically."""
        dev = self.db.get_deviation(deviation_id)
        dev_desc = dev['description'] if dev else ''

        popup = CauseObjectPopup(
            '', '', self.db, dev_description=dev_desc,
            current_description='', deviation_id=deviation_id, parent=self)

        def _on_committed(comp_type, comp_tag, description, frequency):
            new_id = self.db.add_cause(deviation_id)
            self.db.update_cause(new_id, comp_type=comp_type, comp_tag=comp_tag,
                                  description=description or '',
                                  base_frequency=frequency)
            cons_id = self.db.add_consequence(new_id)
            # Jump straight to the new consequence's KON cell (not the cause's
            # own ORS cell) — the cause's description was already chosen in
            # the popup above, so typing the consequence is the next natural
            # step ("så fort jag lagt till en orsak", see NOTES.md).
            self.new_item_created.emit(CONS_T, cons_id)

        popup.committed.connect(_on_committed)
        if global_pos is not None:
            popup.adjustSize()
            _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
            screen = _scr.availableGeometry()
            pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
            x, y   = global_pos.x(), global_pos.y() + 6
            if y + ph > screen.bottom(): y = global_pos.y() - ph - 6
            if x + pw > screen.right():  x = screen.right() - pw - 4
            popup.move(max(screen.left() + 4, x), max(screen.top() + 4, y))
        popup.exec()

    def _quick_add_consequence(self, cause_id):
        """Reported feedback (2026-08-12): unlike a new cause, a new
        consequence should never show a popup — create it blank and jump
        straight to inline editing (unchanged from before the object-
        picker experiment). Tagging an object onto it is still done via
        drag-and-drop from the P&ID, same as editing an existing row."""
        new_id = self.db.add_consequence(cause_id)
        self.new_item_created.emit(CONS_T, new_id)

    def _quick_add_safeguard(self, cons_id):
        """Same as _quick_add_consequence — no popup, straight to inline edit."""
        new_id = self.db.add_safeguard(cons_id)
        self.new_item_created.emit(SG_T, new_id)

    def _add_cause_via_plus_row(self, deviation_id, global_pos=None):
        self._quick_add_cause(deviation_id, global_pos=global_pos)

    def _add_consequence_via_plus_row(self, cause_id):
        self._quick_add_consequence(cause_id)

    def _add_safeguard_via_plus_row(self, cons_id):
        self._quick_add_safeguard(cons_id)

    def select_item(self, type_, id_):
        """Move the current cell to the row for (type_, id_) and start inline
        editing where supported (Orsak/Safeguard columns). Call this after
        refresh()/_rebuild() so the row/table is already populated — used
        when a new cause/consequence/safeguard was just created (e.g. via
        Enter-to-add-next-row), so the user's editing cursor stays on the
        new item instead of the table rebuild silently dropping selection
        and leaving the user unsure where they ended up."""
        col_for_type = {CAUSE_T: self._C_ORS, CONS_T: self._C_KON, SG_T: self._C_SG}
        col = col_for_type.get(type_)
        if col is None:
            return
        # index into each _row_meta tuple: (dev_id, cause_id, cons_id, sg_id)
        field_idx = {CAUSE_T: 1, CONS_T: 2, SG_T: 3}[type_]
        for row, meta in enumerate(self._row_meta):
            if meta[field_idx] == id_:
                self._table.setCurrentCell(row, col)
                item = self._table.item(row, col)
                if item is not None:
                    self._table.scrollToItem(item)
                self._try_start_edit(row, col)  # KON supported too since 2026-08-07 — see NOTES.md
                return

    def _on_cell_changed(self, row, col):
        try:
            self._on_cell_changed_inner(row, col)
        except Exception as e:
            QMessageBox.critical(self, "Fel vid celländring (scenario)", str(e))

    def _on_cell_changed_inner(self, row, col):
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        kind, id_ = meta
        text = item.text().strip()

        if kind == 'cause':
            desc = text.split('\n')[0].strip()
            cause = self.db.get_cause(id_)
            if cause:
                # Check if the text is comp_tag (component tag) or description
                old_comp_tag = cause.get('comp_tag', '') or ''
                old_desc = cause.get('description', '') or ''

                # If the edited text matches old_comp_tag, we're editing comp_tag
                # Otherwise, we're editing description
                if desc and old_comp_tag and old_comp_tag.strip() == text:
                    # User edited comp_tag
                    self.db.update_cause(id_, comp_tag=desc)
                else:
                    # User edited description
                    self.db.update_cause(id_, desc)
                    # Sync any OTHER row showing this same cause (span groups
                    # merge same-id rows visually, but each still has its own
                    # QTableWidgetItem) — no full rebuild needed, see
                    # _update_row_text_only()'s docstring for why.
                    self._update_row_text_only('cause', id_, desc)
            self.item_edited.emit(CAUSE_T, id_)

        elif kind == 'consequence':
            desc = text.split('\n')[0].strip()
            cons = self.db.get_consequence(id_)
            if cons:
                self.db.update_consequence(id_, desc, cons['severity'], cons['category'] or '')
                self._update_row_text_only('consequence', id_, desc)
            self.item_edited.emit(CONS_T, id_)

        elif kind == 'safeguard':
            # No 'Ny safeguard' fallback (2026-08-12, see NOTES.md) —
            # clearing the text back to empty must actually save empty
            # (displayed as "—"), not silently resurrect placeholder text.
            edit_val = item.data(Qt.ItemDataRole.EditRole)
            desc = str(edit_val).strip() if edit_val is not None else text.split('\n')[0].strip()
            sg = self.db.get_safeguard(id_)
            if sg:
                self.db.update_safeguard(id_, desc, sg['rrf'] or 1)
                # A safeguard's description never affects its own row's RRF/
                # risk-derived columns (those depend on rrf, not text) or any
                # other row, so a full _rebuild() was pure overhead here —
                # patch the text in place instead (see _update_row_text_only).
                self._update_row_text_only('safeguard', id_, desc)
            self.item_edited.emit(SG_T, id_)

        if (row, col) == (self._enter_row, self._enter_col):
            self._last_enter_committed = True

    # ── Feature 7: try start inline edit ──────────────────────────────────────
    def _try_start_edit(self, row, col):
        # _C_KON added 2026-08-07 (see NOTES.md "Klicka direkt på
        # konsekvens") — the commit path (_on_cell_changed_inner's
        # 'consequence' branch) already existed and worked; only the
        # trigger was missing. Double-click still opens the step-by-step
        # chain wizard (_open_chain_editor) for anyone who wants that.
        if row < 0 or col not in (self._C_ORS, self._C_SG, self._C_KON):
            return
        item = self._table.item(row, col)
        if item and bool(item.flags() & Qt.ItemFlag.ItemIsEditable):
            self._table.setFocus()
            self._table.edit(self._table.model().index(row, col))

    # ── Feature 2: Ctrl+C clipboard copy ─────────────────────────────────────
    def _copy_row_to_clipboard(self, row):
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        parts = []
        def _txt(col):
            item = self._table.item(row, col)
            return item.text().strip() if item else ''
        parts.append(_txt(self._C_NOD))
        parts.append(_txt(self._C_DEV))
        c = self.db.get_cause(cause_id) if cause_id else None
        parts.append(dict(c).get('description', '') if c else '')
        k = self.db.get_consequence(cons_id) if cons_id else None
        parts.append(dict(k).get('description', '') if k else '')
        parts.append(_txt(self._C_RFORE))
        sg = self.db.get_safeguard(sg_id) if sg_id else None
        parts.append(dict(sg).get('description', '') if sg else '')
        parts.append(_txt(self._C_SLUT))
        QApplication.clipboard().setText('\t'.join(parts))

    # ── Feature: Delete key ───────────────────────────────────────────────────
    def _delete_current_item(self):
        row = self._table.currentRow()
        col = self._table.currentColumn()
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_SG and sg_id:
            if QMessageBox.question(self, "Ta bort barriär",
                    "Ta bort denna barriär?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self.db.delete_safeguard(sg_id)
                self.structure_changed.emit()
                self._schedule_rebuild()
        elif col == self._C_KON and cons_id:
            if QMessageBox.question(self, "Ta bort konsekvens",
                    "Ta bort konsekvens och alla dess barriärer?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self.db.delete_consequence(cons_id)
                self.structure_changed.emit()
                self._schedule_rebuild()
        elif col == self._C_ORS and cause_id:
            if QMessageBox.question(self, "Ta bort orsak",
                    "Ta bort orsak och alla konsekvenser/barriärer?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self.db.delete_cause(cause_id)
                self.structure_changed.emit()
                self._schedule_rebuild()

    # ── Feature 1 & 6: Drag start ─────────────────────────────────────────────
    def _start_drag(self, row, col, is_copy_modifier):
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_SG and sg_id:
            kind = 'sg'; item_id = sg_id
        elif col == self._C_ORS and cause_id:
            kind = 'cause'; item_id = cause_id
        elif col == self._C_KON and cons_id:
            kind = 'cons'; item_id = cons_id
        else:
            return

        mime = QMimeData()
        mime.setText(f'hzp:{kind}:{item_id}:{row}:{col}')

        # Drag pixmap: render the source cell
        idx = self._table.model().index(row, col)
        cell_rect = self._table.visualRect(idx)
        px = self._table.viewport().grab(cell_rect)
        pm = QPixmap(px.size())
        pm.fill(QColor(255, 255, 255, 180))
        p = QPainter(pm)
        p.drawPixmap(0, 0, px)
        p.end()

        drag = QDrag(self._table)
        drag.setMimeData(mime)
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())
        action = (Qt.DropAction.CopyAction if is_copy_modifier
                  else Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)
        drag.exec(action)

    def _handle_drop(self, event, source_obj=None):
        text = event.mimeData().text()
        if not text.startswith('hzp:'):
            return
        parts = text.split(':')
        if len(parts) < 5:
            return
        kind, item_id_s, src_row_s = parts[1], parts[2], parts[3]
        try:
            src_row = int(src_row_s)
        except ValueError:
            return
        is_copy = bool(event.dropAction() == Qt.DropAction.CopyAction)

        # Find target row/col from drop position. The event's position is
        # relative to whichever widget it was actually delivered to
        # (source_obj) — only remap it into viewport coordinates when it
        # came in relative to the outer table widget; if it's already
        # viewport-relative (the common case — see the eventFilter comment
        # above), remapping again would shift it by the header/frame
        # offset a second time and silently miss the target row.
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        if source_obj is self._table:
            vp_pos = self._table.viewport().mapFrom(self._table, pos)
        else:
            vp_pos = pos
        tgt_row = self._table.rowAt(vp_pos.y())
        tgt_col = self._table.columnAt(vp_pos.x())
        if tgt_row < 0 or tgt_row >= len(self._row_meta):
            event.ignore(); return

        tgt_dev, tgt_cause, tgt_cons, tgt_sg = self._row_meta[tgt_row]

        if kind in ('equipment', 'equipment-multi'):
            # Shift-drag of one or more P&ID equipment markers onto a KON
            # or SG cell. `item_id_s` is a comma-separated list of
            # equipment_markers.id values (a single id for the plain
            # 'equipment' kind). Dropping several objects at once (2026-08-09,
            # see NOTES.md) behaves differently per target: onto a
            # CONSEQUENCE, every dragged object builds up the SAME
            # consequence's text (they describe one scenario in sequence,
            # e.g. "TA-1 ... TA-2"); onto a SAFEGUARD, only the first
            # object goes onto the cell actually dropped on — each
            # additional object becomes its OWN new safeguard row under
            # the same consequence, since distinct objects there read as
            # distinct barriers, not one merged sentence.
            try:
                marker_ids = [int(s) for s in item_id_s.split(',') if s.strip()]
            except ValueError:
                event.ignore(); return
            if not marker_ids:
                event.ignore(); return
            equips = [e for e in (self.db.get_equipment_by_marker_id(m) for m in marker_ids) if e]
            if not equips:
                event.ignore(); return
            if tgt_col == self._C_KON and tgt_cons is not None:
                for equip in equips:
                    self.db.append_tag_to_consequence(
                        tgt_cons, equip.get('tag', ''), equip.get('equipment_type', ''))
            elif tgt_col == self._C_SG and tgt_sg is not None:
                # The dropped-on row only ever absorbs an object if it has
                # no object on it yet — once it already carries a tag
                # (from an earlier drop, single or multi), a NEW drop must
                # still land on its own new row, not merge into that
                # row's text (2026-08-09, see NOTES.md: "jag vill att den
                # ... skall lägga till flera olika objekt om jag drar till
                # safeguards med (flera rader)" — applies whether the
                # extra objects arrive in one multi-select drag or as
                # separate later single-object drags onto the same row).
                sg_row = self.db.get_safeguard(tgt_sg)
                row_is_free = bool(sg_row) and not (sg_row.get('tagged_refs') or '').strip()
                for i, equip in enumerate(equips):
                    if i == 0 and row_is_free:
                        self.db.append_tag_to_safeguard(
                            tgt_sg, equip.get('tag', ''), equip.get('equipment_type', ''))
                    else:
                        new_sg_id = self.db.add_safeguard(tgt_cons)
                        self.db.append_tag_to_safeguard(
                            new_sg_id, equip.get('tag', ''), equip.get('equipment_type', ''))
            else:
                event.ignore(); return
            self._schedule_rebuild()
            event.acceptProposedAction()
            return

        try:
            item_id = int(item_id_s)
        except ValueError:
            return

        if kind == 'sg':
            if tgt_cons is None or tgt_cons == self._row_meta[src_row][2]:
                event.ignore(); return
            if is_copy:
                self.db.copy_safeguard(item_id, tgt_cons)
            else:
                self.db.move_safeguard(item_id, tgt_cons)
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

        elif kind == 'cons':
            if tgt_cause is None or tgt_cause == self._row_meta[src_row][1]:
                event.ignore(); return
            if is_copy:
                self.db.copy_consequence(item_id, tgt_cause)
            else:
                self.db.move_consequence(item_id, tgt_cause)
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

        elif kind == 'cause':
            if tgt_dev is None or tgt_dev == self._row_meta[src_row][0]:
                event.ignore(); return
            if is_copy:
                self.db.copy_cause(item_id, tgt_dev)
            else:
                self.db.move_cause_to_deviation(item_id, tgt_dev)
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

    # ── Feature 4 & 5: Context menu ───────────────────────────────────────────
    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        col = self._table.columnAt(pos.x())
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]

        menu = QMenu(self)

        # ── Ctrl+C shortcut hint ────────────────────────────────────────
        copy_row = menu.addAction(_icon('clipboard'), "Kopiera rad  (Ctrl+C)")
        copy_row.triggered.connect(lambda: self._copy_row_to_clipboard(row))
        menu.addSeparator()

        # ── Orsak-åtgärder ──────────────────────────────────────────────
        if col in (self._C_ORS, self._C_NOD, self._C_DEV) and cause_id:
            c = self.db.get_cause(cause_id)
            c_desc = dict(c).get('description', '?')[:40] if c else '?'
            menu.addSection(_icon('settings'), f"Orsak: {c_desc}")
            menu.addAction(_icon('edit'), "Redigera",
                lambda: self._try_start_edit(row, self._C_ORS))
            a_dup = menu.addAction(_icon('document'), "Duplicera orsak (med konsekvenser)")
            a_dup.triggered.connect(
                lambda: self._duplicate_cause(cause_id))
            a_move = menu.addAction("↕  Flytta till annan avvikelse…")
            a_move.triggered.connect(
                lambda: self._move_cause_dialog(cause_id))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort orsak")
            a_del.triggered.connect(lambda cid=cause_id: self._confirm_delete('cause', cid))

        # ── Konsekvens-åtgärder ─────────────────────────────────────────
        elif col in (self._C_KON, self._C_RFORE) and cons_id:
            k = self.db.get_consequence(cons_id)
            k_desc = dict(k).get('description', '?')[:40] if k else '?'
            menu.addSection(_icon('warning'), f"Konsekvens: {k_desc}")
            a_dup = menu.addAction(_icon('document'), "Duplicera konsekvens (med barriärer)")
            a_dup.triggered.connect(
                lambda: self._duplicate_consequence(cons_id, cause_id))
            a_move = menu.addAction("↕  Flytta till annan orsak…")
            a_move.triggered.connect(
                lambda: self._move_consequence_dialog(cons_id))
            if k and (dict(k).get('comp_tag') or dict(k).get('comp_type')):
                a_untag = menu.addAction(_icon('close'), "Ta bort tagg")
                a_untag.triggered.connect(lambda cid=cons_id: self._untag_consequence(cid))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort konsekvens")
            a_del.triggered.connect(lambda cid=cons_id: self._confirm_delete('cons', cid))

        # ── Barriär-åtgärder ────────────────────────────────────────────
        elif col in (self._C_SG, self._C_LOPA, self._C_SLUT) and sg_id:
            sg = self.db.get_safeguard(sg_id)
            sg_desc = dict(sg).get('description', '?')[:40] if sg else '?'
            menu.addSection(_icon('shield'), f"Barriär: {sg_desc}")
            menu.addAction(_icon('edit'), "Redigera",
                lambda: self._try_start_edit(row, self._C_SG))
            a_copy = menu.addAction(_icon('clipboard'), "Kopiera till annan konsekvens…")
            a_copy.triggered.connect(
                lambda: self._copy_safeguard_dialog(sg_id))
            a_move = menu.addAction("↕  Flytta till annan konsekvens…")
            a_move.triggered.connect(
                lambda: self._move_safeguard_dialog(sg_id))
            if sg and (dict(sg).get('comp_tag') or dict(sg).get('comp_type')):
                a_untag = menu.addAction(_icon('close'), "Ta bort tagg")
                a_untag.triggered.connect(lambda sid=sg_id: self._untag_safeguard(sid))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort barriär")
            a_del.triggered.connect(lambda sid=sg_id: self._confirm_delete('sg', sid))

        if not menu.isEmpty():
            menu.exec(self._table.viewport().mapToGlobal(pos))

    def _untag_consequence(self, cons_id):
        """Detach a dragged-in equipment tag from a KON cell without
        deleting the row — the inline "×" this replaced sat in the tag
        strip, which was removed 2026-08-10 (see NOTES.md; the tag still
        shows bolded in the description text via tagged_refs)."""
        self.db.set_consequence_tag(cons_id, '', '')
        self._schedule_rebuild()

    def _untag_safeguard(self, sg_id):
        """Same as _untag_consequence, for a safeguard cell."""
        self.db.set_safeguard_tag(sg_id, '', '')
        self._schedule_rebuild()

    def _confirm_delete(self, kind, item_id):
        labels = {'cause': ('orsak', 'cause'), 'cons': ('konsekvens', 'consequence'),
                  'sg': ('barriär', 'safeguard')}
        swe, db_kind = labels.get(kind, (kind, kind))
        if QMessageBox.question(self, f"Ta bort {swe}",
                f"Ta bort {swe}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            if db_kind == 'cause':
                self.db.delete_cause(item_id)
            elif db_kind == 'consequence':
                self.db.delete_consequence(item_id)
            else:
                self.db.delete_safeguard(item_id)
            self.structure_changed.emit()
            self._schedule_rebuild()

    # ── Feature 5: Duplicate ──────────────────────────────────────────────────
    def _duplicate_consequence(self, cons_id, cause_id):
        new_id = self.db.copy_consequence(cons_id, cause_id)
        if new_id:
            self.structure_changed.emit()
            self._schedule_rebuild()

    def _duplicate_cause(self, cause_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        dev_id = dict(cause).get('deviation_id')
        if dev_id is None:
            return
        new_id = self.db.copy_cause(cause_id, dev_id)
        if new_id:
            self.structure_changed.emit()
            self._schedule_rebuild()

    # ── Feature 6: Move dialogs ───────────────────────────────────────────────
    def _move_cause_dialog(self, cause_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        node_id = dict(cause)['node_id']
        cur_dev = dict(cause).get('deviation_id')
        devs = [d for d in self.db.deviations(node_id) if d['id'] != cur_dev]
        if not devs:
            QMessageBox.information(self, "Flytta orsak",
                "Ingen annan avvikelse finns under denna nod.\n"
                "Lägg till fler avvikelser i trädet först.")
            return
        items = [f"{d['description']}" for d in devs]
        choice, ok = QInputDialog.getItem(self, "Flytta orsak",
            "Välj målавvikelse:", items, 0, False)
        if ok:
            idx = items.index(choice)
            self.db.move_cause_to_deviation(cause_id, devs[idx]['id'])
            self.structure_changed.emit()
            self._schedule_rebuild()

    def _move_consequence_dialog(self, cons_id):
        cons = self.db.get_consequence(cons_id)
        if not cons:
            return
        cur_cause = dict(cons)['cause_id']
        cur_cause_row = self.db.get_cause(cur_cause)
        if not cur_cause_row:
            return
        node_id = dict(cur_cause_row)['node_id']
        all_causes = []
        for dev in self.db.deviations(node_id):
            for c in self.db.causes_for_deviation(dev['id']):
                if c['id'] != cur_cause:
                    all_causes.append((c, dev))
        if not all_causes:
            QMessageBox.information(self, "Flytta konsekvens",
                "Ingen annan orsak finns under denna nod.")
            return
        items = [f"{dev['description']} → {c['description']}"
                 for c, dev in all_causes]
        choice, ok = QInputDialog.getItem(self, "Flytta konsekvens",
            "Välj målorsak:", items, 0, False)
        if ok:
            idx = items.index(choice)
            tgt_cause_id = all_causes[idx][0]['id']
            self.db.move_consequence(cons_id, tgt_cause_id)
            self.structure_changed.emit()
            self._schedule_rebuild()

    def _copy_safeguard_dialog(self, sg_id):
        self._pick_target_cons_dialog(sg_id, move=False)

    def _move_safeguard_dialog(self, sg_id):
        self._pick_target_cons_dialog(sg_id, move=True)

    def _pick_target_cons_dialog(self, sg_id, move=False):
        sg = self.db.get_safeguard(sg_id)
        if not sg:
            return
        cur_cons = dict(sg)['consequence_id']
        # Collect all consequences across all nodes
        all_cons = []
        for node in self.db.nodes():
            for dev in self.db.deviations(node['id']):
                for cause in self.db.causes_for_deviation(dev['id']):
                    for cons in self.db.consequences(cause['id']):
                        if cons['id'] != cur_cons:
                            all_cons.append((cons, cause, dev, node))
        if not all_cons:
            QMessageBox.information(self, "Välj konsekvens",
                "Inga andra konsekvenser finns.")
            return
        items = [f"{n['name']} / {d['description']} / {c['description'][:30]} / {k['description'][:30]}"
                 for k, c, d, n in all_cons]
        verb = "Flytta" if move else "Kopiera"
        choice, ok = QInputDialog.getItem(self, f"{verb} barriär",
            "Välj målkonsekvens:", items, 0, False)
        if ok:
            idx = items.index(choice)
            tgt_cons_id = all_cons[idx][0]['id']
            if move:
                self.db.move_safeguard(sg_id, tgt_cons_id)
            else:
                self.db.copy_safeguard(sg_id, tgt_cons_id)
            self.structure_changed.emit()
            self._schedule_rebuild()
