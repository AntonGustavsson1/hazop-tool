#!/usr/bin/env python3
"""HAZOPPreparationPanel (+ its private DraggableColorSwatch/MatrixCellButton drag-and-drop helpers) -- split out of settings_panels.py 2026-08-21, see NOTES.md "Dela upp settings_panels.py"."""

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
from PyQt6.QtCore import Qt, pyqtSignal, QDate, QEvent, QMimeData, QTimer
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
from participant_matrix_panel import ParticipantMatrixPanel
from standard_causes_panel import StandardCausesSettingsPanel


_PALETTE_MIME = 'application/x-hazop-palette-color'


class DraggableColorSwatch(QLabel):
    """Draggable color swatch in the palette — drag onto a matrix cell."""

    def __init__(self, name: str, color: str, fg_color: str = None, parent=None):
        super().__init__(name, parent)
        self._name     = name
        self._color    = color
        self._fg_color = fg_color  # None = auto-calculated from luminance
        self.setFixedSize(76, 28)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._refresh()

    def _refresh(self):
        r, g, b = int(self._color[1:3], 16), int(self._color[3:5], 16), int(self._color[5:7], 16)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        auto_txt = '#000' if lum > 160 else '#fff'
        txt = self._fg_color if self._fg_color else auto_txt
        self.setStyleSheet(
            f"background:{self._color}; color:{txt}; font-weight:bold; font-size:10px;"
            f"border:1px solid #555; border-radius:4px;")
        self.setText(self._name)

    def set_swatch(self, name: str, color: str, fg_color: str = None):
        self._name = name; self._color = color; self._fg_color = fg_color
        self._refresh()

    def name(self):     return self._name
    def color(self):    return self._color
    def fg_color(self): return self._fg_color

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_PALETTE_MIME,
                         json.dumps({'color': self._color, 'name': self._name,
                                     'fg_color': self._fg_color or '#ffffff'}).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(event.position().toPoint())
            drag.exec(Qt.DropAction.CopyAction)
        else:
            super().mousePressEvent(event)


class MatrixCellButton(QPushButton):
    """Risk matrix cell — collapsed-border grid (no double-lines between cells)."""

    def __init__(self, row, col, color, label, fg_color='#ffffff',
                 is_top_row=False, is_left_col=False, parent=None):
        super().__init__(label, parent)
        self.row = row
        self.col = col
        self._color    = color
        self._fg_color = fg_color
        self._label    = label
        self._is_top   = is_top_row
        self._is_left  = is_left_col
        # Width is controlled by the resizable matrix splitter; only the
        # cell height is fixed so the grid can be narrowed or widened without
        # fighting a widget-level fixed width.
        self.setFixedHeight(40)
        self.setMinimumWidth(30)
        self.setAcceptDrops(True)
        self._apply_style()

    def _apply_style(self):
        top  = "border-top:1px solid #444;"  if self._is_top  else ""
        left = "border-left:1px solid #444;" if self._is_left else ""
        self.setStyleSheet(
            f"QPushButton{{"
            f"background:{self._color}; color:{self._fg_color}; font-weight:bold;"
            f"border-bottom:1px solid #444; border-right:1px solid #444;"
            f"{top}{left}"
            f"border-radius:0px; margin:0px; padding:0px;}}"
            f"QPushButton:hover{{border:2px solid #000; margin:-1px;}}")
        self.setText(self._label)

    def set_cell(self, color, label=None, fg_color=None):
        self._color = color
        if label is not None:
            self._label = label
        if fg_color is not None:
            self._fg_color = fg_color
        self._apply_style()

    def color(self):    return self._color
    def label(self):    return self._label
    def fg_color(self): return self._fg_color

    # ── Drag-and-drop ─────────────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            self.setStyleSheet(
                f"background:{self._color}; color:white; font-weight:bold;"
                f"border:3px dashed #000;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._apply_style()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            data = json.loads(
                event.mimeData().data(_PALETTE_MIME).data().decode())
            self.set_cell(data['color'], data['name'], data.get('fg_color', '#ffffff'))
            event.acceptProposedAction()
        else:
            event.ignore()




class HAZOPPreparationPanel(QWidget):
    """Administrative HAZOP-prep material, collected under its own top-level
    nav entry (2026-08-17, user request: "flytta om flikarna... Skapa en ny
    huvudflik i Claude med namnet HAZOP preperation. Fliken ska samla
    följande administrativa underlag: Projekt, Deltagare, Riskmatris,
    Standardorsaker... Denna fliken ska ligga ute i det svarta fältet till
    vänster högst upp") — these four used to live buried several clicks deep
    as tabs inside Inställningar; extracted here into their own page since
    Anton wanted them front-and-center. Placed at MainWindow.view_stack
    index 0 (see NOTES.md for why: not just visually first in the nav rail,
    Anton explicitly wants it structurally first, so every OTHER page's
    index shifts +1 — see the "_switch_view" renumbering that accompanies
    this class).

    "Riskmatris & Kategorier" brings essentially all of the OLD
    SettingsPanel's own methods along with it (17 of them) — before this
    split, that risk-matrix/palette/category editing WAS almost the entire
    class; SettingsPanel keeps only the tabs that were already their own
    standalone panel classes or simple inline forms unrelated to the matrix.

    Keeps its OWN `matrix_changed` signal (rather than somehow reaching
    across to SettingsPanel's) — SettingsPanel's TagDatabasePanel forwards
    its own settings_changed into a `matrix_changed` of its own for the same
    "please refresh" purpose (MainWindow._on_matrix_changed refreshes tree/
    scenario views generically, not just for matrix edits) — cleanest to let
    each panel own the exact signal for whatever changes it makes, and have
    MainWindow.__init__ connect both to the same handler."""

    matrix_changed = pyqtSignal()
    sheets_changed = pyqtSignal()
    structure_changed = pyqtSignal()   # a node was added/renamed from the Noder tab

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cell_buttons   = []
        self._x_label_edits  = []   # QLineEdit per column
        self._y_label_edits  = []   # QLineEdit per row (high→low)
        self._palette_swatches = []
        self._sev_def_edits  = {}   # (cat_id, sev_level) → QLineEdit, embedded in matrix grid
        self._tor_report_fields = {}  # (tor|report, prepared|reviewed|approved) → QComboBox

        tabs = QTabWidget()
        self._tabs = tabs   # kept as an attribute for testability (tabText() lookups)
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: Projekt ──────────────────────────────────────────────────────
        proj_tab = QWidget()
        proj_outer = QVBoxLayout(proj_tab)
        proj_outer.setContentsMargins(0, 0, 0, 0)
        proj_form_w = QWidget()
        pl = QFormLayout(proj_form_w)
        pl.setSpacing(10)
        pl.setContentsMargins(16, 16, 16, 16)
        proj_outer.addWidget(proj_form_w)

        self._proj_name = QLineEdit()
        self._proj_name.editingFinished.connect(
            lambda: self.db.set_config('project_name', self._proj_name.text()))
        pl.addRow("Projektnamn:", self._proj_name)

        self._proj_number = QLineEdit()
        self._proj_number.editingFinished.connect(
            lambda: self.db.set_config('project_number', self._proj_number.text()))
        pl.addRow("Projektnummer:", self._proj_number)

        self._proj_client = QLineEdit()
        self._proj_client.editingFinished.connect(
            lambda: self.db.set_config('project_client', self._proj_client.text()))
        pl.addRow("Kund/Företag:", self._proj_client)

        self._proj_facility = QLineEdit()
        self._proj_facility.editingFinished.connect(
            lambda: self.db.set_config('project_facility', self._proj_facility.text()))
        pl.addRow("Anläggning:", self._proj_facility)

        date_row_w = QWidget()
        date_row_w.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        date_row_l = QHBoxLayout(date_row_w)
        date_row_l.setContentsMargins(0, 0, 0, 0)
        date_row_l.setSpacing(6)
        self._proj_date_start = QDateEdit()
        self._proj_date_start.setCalendarPopup(True)
        self._proj_date_start.setDisplayFormat("yyyy-MM-dd")
        self._proj_date_end = QDateEdit()
        self._proj_date_end.setCalendarPopup(True)
        self._proj_date_end.setDisplayFormat("yyyy-MM-dd")
        _date_edit_w = QFontMetrics(self._proj_date_start.font()).horizontalAdvance(
            "9999-99-99") + 40
        self._proj_date_start.setMaximumWidth(_date_edit_w)
        self._proj_date_end.setMaximumWidth(_date_edit_w)
        self._proj_date_start.dateChanged.connect(
            lambda d: self.db.set_config('project_date_start', d.toString('yyyy-MM-dd')))
        self._proj_date_end.dateChanged.connect(
            lambda d: self.db.set_config('project_date_end', d.toString('yyyy-MM-dd')))
        self._proj_date_start_today_btn = QPushButton("Idag")
        self._proj_date_start_today_btn.setToolTip("Sätt startdatum till dagens datum")
        self._proj_date_start_today_btn.clicked.connect(
            lambda: self._proj_date_start.setDate(QDate.currentDate()))
        self._proj_date_end_today_btn = QPushButton("Idag")
        self._proj_date_end_today_btn.setToolTip("Sätt slutdatum till dagens datum")
        self._proj_date_end_today_btn.clicked.connect(
            lambda: self._proj_date_end.setDate(QDate.currentDate()))
        date_row_l.addWidget(self._proj_date_start)
        date_row_l.addWidget(self._proj_date_start_today_btn)
        date_row_l.addWidget(QLabel("  –  "))
        date_row_l.addWidget(self._proj_date_end)
        date_row_l.addWidget(self._proj_date_end_today_btn)
        pl.addRow("Datum (från–till):", date_row_w)

        # ── Revision: flera rader (Rev/Datum/Beskrivning) ────────────────────
        rev_box = QGroupBox("Revision")
        rev_lay = QVBoxLayout(rev_box)
        self._proj_rev_table = QTableWidget(0, 3)
        self._proj_rev_table.setHorizontalHeaderLabels(["Rev", "Datum", "Beskrivning"])
        self._proj_rev_table.horizontalHeader().setStretchLastSection(True)
        self._proj_rev_table.setColumnWidth(0, 60)
        self._proj_rev_table.setColumnWidth(1, 120)
        self._proj_rev_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._proj_rev_table.customContextMenuRequested.connect(self._proj_rev_context_menu)
        self._proj_rev_table.itemChanged.connect(self._on_proj_rev_item_changed)
        rev_lay.addWidget(self._proj_rev_table)
        rev_btn_row = QHBoxLayout()
        rev_add_btn = QPushButton("+ Lägg till rad")
        rev_add_btn.clicked.connect(self._add_project_revision_row)
        rev_btn_row.addWidget(rev_add_btn)
        rev_btn_row.addStretch()
        rev_lay.addLayout(rev_btn_row)
        proj_outer.addWidget(rev_box)

        # ── Egna fria fält ────────────────────────────────────────────────
        fields_box = QGroupBox("Egna fält")
        self._proj_fields_lay = QVBoxLayout(fields_box)
        self._proj_field_rows = {}   # field id -> (name_edit, value_edit)
        fields_add_btn = QPushButton("+ Lägg till fält")
        fields_add_btn.clicked.connect(self._add_project_custom_field_row)
        fields_btn_row = QHBoxLayout()
        fields_btn_row.addWidget(fields_add_btn)
        fields_btn_row.addStretch()
        self._proj_fields_lay.addLayout(fields_btn_row)
        proj_outer.addWidget(fields_box)
        proj_outer.addStretch()

        tabs.addTab(proj_tab, "Projekt")

        # ── Tab: Deltagare ────────────────────────────────────────────────────
        # Replaces the old free-text "Deltagare" field (2026-08-11, user
        # request: "skulle även gilla ... en till flik med deltagare
        # istället där man definerar förnamn, efternamn, roll på y axel och
        # analystillfälen på x axeln så det blir en matris" — "istället"
        # means this REPLACES the free-text field, not adds to it). See
        # ParticipantMatrixPanel below and NOTES.md for the schema/UI
        # design rationale.
        self._participant_matrix_panel = ParticipantMatrixPanel(self.db)
        tabs.addTab(self._participant_matrix_panel, "Deltagare")

        # ── Tab: ToR and Report ───────────────────────────────
        # Names are editable combo boxes: registered participants are offered
        # as suggestions, while an arbitrary free-text name remains valid.
        tor_report_tab = QWidget()
        tr_outer = QVBoxLayout(tor_report_tab)
        tr_outer.setContentsMargins(16, 16, 16, 16)
        tr_outer.setSpacing(12)
        intro = QLabel(
            "Ange ansvariga personer för Terms of Reference (ToR) och rapporten. "
            "Välj en deltagare eller skriv ett eget namn.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#666; font-size:10px;")
        tr_outer.addWidget(intro)

        self._tor_report_add_section(tr_outer, "tor", "ToR")
        self._tor_report_add_section(tr_outer, "report", "Report")
        tr_outer.addStretch()
        tabs.addTab(tor_report_tab, "ToR and Report")
        tabs.currentChanged.connect(self._on_prep_tab_changed)

        # ── Tab: Riskmatris ───────────────────────────────────────────────────
        matrix_tab = QWidget()
        ml = QVBoxLayout(matrix_tab)
        ml.setSpacing(6)

        # Size row
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Konsekvens-fält:"))
        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(2, 15)
        self._rows_spin.setValue(5)
        self._rows_spin.setToolTip("Antal nivåer på konsekvens-axeln (C1…Cn)")
        size_row.addWidget(self._rows_spin)

        size_row.addWidget(QLabel("  Frekvens-fält:"))
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(2, 15)
        self._cols_spin.setValue(7)
        self._cols_spin.setToolTip("Antal nivåer på frekvens-axeln (F-1…Fn)")
        size_row.addWidget(self._cols_spin)
        size_row.addStretch()
        ml.addLayout(size_row)

        # ── Colour palette ────────────────────────────────────────────────────
        pal_box = QGroupBox("Färgpalett — dra en färg och släpp på en cell")
        pal_lay = QHBoxLayout(pal_box)
        pal_lay.setSpacing(4)
        self._palette_container = pal_lay

        add_col_btn = QPushButton("+ Lägg till")
        add_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        add_col_btn.clicked.connect(self._palette_add)
        pal_lay.addWidget(add_col_btn)

        edit_col_btn = QPushButton("Redigera")
        edit_col_btn.setIcon(_icon('edit'))
        edit_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        edit_col_btn.clicked.connect(self._palette_edit)
        pal_lay.addWidget(edit_col_btn)

        del_col_btn = QPushButton("Ta bort")
        del_col_btn.setIcon(_icon('delete'))
        del_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        del_col_btn.clicked.connect(self._palette_delete)
        pal_lay.addWidget(del_col_btn)

        pal_lay.addStretch()
        ml.addWidget(pal_box)

        # ── Matrix grid ───────────────────────────────────────────────────────
        # Use a wrapper so matrix stays at natural size (top-left) while the
        # scroll area fills remaining space with the stretch below it.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        _wrap = QWidget()
        _wrap_lay = QVBoxLayout(_wrap)
        _wrap_lay.setContentsMargins(0, 0, 0, 0)
        _wrap_lay.setSpacing(0)

        self._matrix_container = QWidget()
        self._matrix_container.setMinimumWidth(0)
        self._matrix_grid = QGridLayout(self._matrix_container)
        self._matrix_grid.setSpacing(0)
        self._matrix_grid.setContentsMargins(0, 0, 0, 0)

        # A horizontal splitter gives the matrix a visible right-hand drag
        # edge.  The empty pane is intentional: it keeps the matrix anchored
        # left while allowing its complete width (including definitions below)
        # to be widened without resizing the surrounding settings page.
        matrix_splitter = QSplitter(Qt.Orientation.Horizontal)
        matrix_splitter.setChildrenCollapsible(False)
        matrix_splitter.addWidget(self._matrix_container)
        matrix_spacer = QWidget()
        matrix_spacer.setMinimumWidth(8)
        matrix_splitter.addWidget(matrix_spacer)
        matrix_splitter.setStretchFactor(0, 0)
        matrix_splitter.setStretchFactor(1, 1)
        matrix_splitter.splitterMoved.connect(self._on_matrix_splitter_moved)
        self._matrix_splitter = matrix_splitter
        _wrap_lay.addWidget(matrix_splitter)
        scroll.setWidget(_wrap)
        ml.addWidget(scroll)

        # Axis orientation + direction controls
        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axlar:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItem("Frekvens → X,  Konsekvens → Y  (standard)", 'frequency')
        self._axis_combo.addItem("Konsekvens → X,  Frekvens → Y", 'consequence')
        ax_row.addWidget(self._axis_combo, 1)
        ax_row.addWidget(QLabel("  Riktning:"))
        # Clickable arrows instead of checkboxes (2026-08-17 user request) —
        # QToolButton in checkable mode is a drop-in for QCheckBox here:
        # every other call site only ever touches .isChecked()/.setChecked()/
        # .toggled, which QAbstractButton gives both classes identically, so
        # nothing downstream (_apply_size, _load_matrix_ui, _build_matrix_grid,
        # _save_matrix) needed to change.
        self._x_rev_chk = QToolButton()
        self._x_rev_chk.setCheckable(True)
        self._x_rev_chk.setAutoRaise(True)
        self._y_rev_chk = QToolButton()
        self._y_rev_chk.setCheckable(True)
        self._y_rev_chk.setAutoRaise(True)

        def _update_x_arrow(checked):
            self._x_rev_chk.setText("X ←" if checked else "X →")
            self._x_rev_chk.setToolTip(
                "X-axeln vänd: högt värde till vänster" if checked
                else "X-axeln normal: klicka för att vända (högt värde till vänster)")

        def _update_y_arrow(checked):
            self._y_rev_chk.setText("Y ↑" if checked else "Y ↓")
            self._y_rev_chk.setToolTip(
                "Y-axeln vänd: högst upp" if checked
                else "Y-axeln normal: klicka för att vända (högst upp)")

        self._x_rev_chk.toggled.connect(_update_x_arrow)
        self._y_rev_chk.toggled.connect(_update_y_arrow)
        _update_x_arrow(False)
        _update_y_arrow(False)
        ax_row.addWidget(self._x_rev_chk)
        ax_row.addWidget(self._y_rev_chk)
        ml.addLayout(ax_row)

        # Live update: rebuild grid immediately on any control change
        self._axis_combo.currentIndexChanged.connect(self._apply_size)
        self._x_rev_chk.toggled.connect(self._apply_size)
        self._y_rev_chk.toggled.connect(self._apply_size)
        self._rows_spin.valueChanged.connect(self._apply_size)
        self._cols_spin.valueChanged.connect(self._apply_size)

        # Frequency label presets
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Frekvens-mall:"))
        norsok_btn = QPushButton("NORSOK Z-013  (AAA – E)")
        norsok_btn.setToolTip(
            "Fyll frekvensaxeln med NORSOK Z-013-etiketter:\n"
            "AAA (< 10⁻⁵/år)  →  E (> 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        norsok_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['AAA', 'AA', 'A', 'B', 'C', 'D', 'E'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        fscale_btn = QPushButton("F-skala  (F-1 – F5)")
        fscale_btn.setToolTip(
            "Fyll frekvensaxeln med internt F-skaleetiketter:\n"
            "F-1 (Otänkbar)  →  F5 (Frekvent > 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        fscale_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['F-1 – Otänkbar', 'F0 – Extremt sällan', 'F1 – Sällan',
             'F2 – Osannolik', 'F3 – Möjlig', 'F4 – Trolig', 'F5 – Frekvent'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        preset_row.addWidget(norsok_btn)
        preset_row.addWidget(fscale_btn)
        preset_row.addStretch()
        ml.addLayout(preset_row)

        save_matrix_btn = QPushButton("Spara riskmatris")
        save_matrix_btn.setIcon(_icon('save', 16, '#ffffff'))
        save_matrix_btn.setStyleSheet(
            "background:#2F5FD0; color:#fff; font-weight:bold; padding:4px 12px;")
        save_matrix_btn.clicked.connect(self._save_matrix)
        ml.addWidget(save_matrix_btn)

        # ── Tab: Kategorier ───────────────────────────────────────────────────
        cat_tab = QWidget()
        cl = QVBoxLayout(cat_tab)
        cl.addWidget(QLabel("Konsekvensskategorier:"))
        self._cat_list = QListWidget()
        cl.addWidget(self._cat_list)
        cat_btns = QHBoxLayout()
        btn_add  = QPushButton("+ Lägg till")
        btn_ren  = QPushButton("Byt namn")
        btn_del  = QPushButton("Ta bort")
        btn_up   = QPushButton("▲")
        btn_down = QPushButton("▼")
        btn_up.setToolTip("Flytta vald kategori uppåt")
        btn_down.setToolTip("Flytta vald kategori nedåt")
        btn_up.setFixedWidth(CONFIG['W_ICON_BTN'])
        btn_down.setFixedWidth(CONFIG['W_ICON_BTN'])
        btn_add.clicked.connect(self._cat_add)
        btn_ren.clicked.connect(self._cat_rename)
        btn_del.clicked.connect(self._cat_delete)
        btn_up.clicked.connect(lambda: self._cat_move(-1))
        btn_down.clicked.connect(lambda: self._cat_move(1))
        for b in [btn_add, btn_ren, btn_del, btn_up, btn_down]: cat_btns.addWidget(b)
        cl.addLayout(cat_btns)
        cl.addStretch()

        # ── Merged tab: Riskmatris & Kategorier ─────────────────────────────
        # Design choice (2026-08-11, user request: "'riskmatris' och
        # 'kategorier' borde gå att slå ihop till en sida" / "Låt Claude
        # välja bästa GUI-lösningen"): a QSplitter, categories on the left
        # and the matrix on the right, rather than a nested tab-within-tab.
        # Reasoning: the matrix tab is inherently tall/wide (size controls +
        # colour palette + a scrollable grid + axis controls + frequency
        # presets + a save button), while the categories tab is just a short
        # list with three buttons — putting categories in their own nested
        # tab would hide them behind an extra click AND waste most of that
        # tab's vertical space. Categories also feed the matrix conceptually
        # (they're consequence-axis metadata), so keeping both visible
        # side-by-side, with the narrow categories panel user-resizable via
        # the splitter handle, reads as one coherent risk-classification
        # screen instead of two unrelated hidden pages.
        combined_tab = QWidget()
        combined_l = QHBoxLayout(combined_tab)
        combined_l.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(cat_tab)
        splitter.addWidget(matrix_tab)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 760])
        combined_l.addWidget(splitter)
        tabs.addTab(combined_tab, "Riskmatris")

        # ── Tab: Standardorsaker ─────────────────────────────────────────────
        self._std_causes_panel = StandardCausesSettingsPanel(self.db)
        tabs.addTab(self._std_causes_panel, "Avvikelser & Orsaker")

        # ── Tab: Blad (moved from Studiehantering → PID-hantering, 2026-08-17,
        # see NOTES.md) ───────────────────────────────────────────────────────
        sheets_widget = QWidget()
        sheets_layout = QVBoxLayout(sheets_widget)
        sheets_layout.setContentsMargins(8, 8, 8, 8)
        sheets_layout.setSpacing(6)

        sheet_hdr = QHBoxLayout()
        sheet_hdr.addWidget(QLabel("Bladordning — dra för att ändra ordning:"))
        sheet_hdr.addStretch()
        rename_btn = QPushButton("Byt namn")
        rename_btn.setIcon(_icon('edit'))
        rename_btn.clicked.connect(self._rename_sheet)
        sheet_hdr.addWidget(rename_btn)
        delete_btn = QPushButton("Ta bort")
        delete_btn.setIcon(_icon('delete'))
        delete_btn.clicked.connect(self._delete_sheets)
        sheet_hdr.addWidget(delete_btn)
        sheets_layout.addLayout(sheet_hdr)

        rev_row = QHBoxLayout()
        rev_row.addWidget(QLabel("P&ID-revision för valt blad:"))
        self._sheet_rev_combo = QComboBox()
        self._sheet_rev_combo.currentIndexChanged.connect(self._on_sheet_revision_changed)
        rev_row.addWidget(self._sheet_rev_combo, 1)
        sheets_layout.addLayout(rev_row)

        self._sheet_list = QTableWidget(0, 5)
        self._sheet_list.setHorizontalHeaderLabels(["Ritningsnummer", "Ritningsnamn", "Revision", "Datum", "PDF-sida"])
        self._sheet_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._sheet_list.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._sheet_list.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed | QTableWidget.EditTrigger.SelectedClicked)
        self._sheet_list.setDragDropMode(QTableWidget.DragDropMode.InternalMove)
        self._sheet_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sheet_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._sheet_list.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._sheet_list.itemChanged.connect(self._on_sheet_item_changed)
        self._sheet_list.model().rowsMoved.connect(self._on_sheets_reordered)
        self._sheet_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sheet_list.customContextMenuRequested.connect(self._sheet_context_menu)
        self._sheet_list.currentCellChanged.connect(self._on_sheet_selection_changed)
        _base_kp = self._sheet_list.keyPressEvent
        def _sheet_key_press(event, _base=_base_kp):
            if event.key() == Qt.Key.Key_Delete:
                self._delete_sheets()
            else:
                _base(event)
        self._sheet_list.keyPressEvent = _sheet_key_press
        sheets_layout.addWidget(self._sheet_list)
        tabs.addTab(sheets_widget, "Blad")

        # ── Tab: Noder ────────────────────────────────────────────────────────
        # Mirrors the HAZOP tree's node list both ways: renaming/creating a
        # node here refreshes the tree via structure_changed, and any tree
        # change that calls this panel's refresh_nodes() shows up here
        # (2026-08-17, see NOTES.md "Ny Noder-flik").
        nodes_widget = QWidget()
        nodes_layout = QVBoxLayout(nodes_widget)
        nodes_layout.setContentsMargins(8, 8, 8, 8)
        nodes_layout.setSpacing(6)
        nodes_hdr = QHBoxLayout()
        nodes_hdr.addWidget(QLabel("Alla noder:"))
        nodes_hdr.addStretch()
        add_node_btn = QPushButton("+ Ny nod")
        add_node_btn.clicked.connect(self._add_node_from_noder_tab)
        nodes_hdr.addWidget(add_node_btn)
        nodes_layout.addLayout(nodes_hdr)
        self._nodes_table = QTableWidget(0, 7)
        self._nodes_table.setHorizontalHeaderLabels([
            "Nod nummer", "Namn", "Blad", "Objekt per blad", "Objekttyp",
            "Avvikelser per objekt", "Antal"])
        self._nodes_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.setWordWrap(True)
        self._nodes_table.verticalHeader().setVisible(False)
        self._nodes_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._nodes_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._nodes_table.cellDoubleClicked.connect(self._on_nodes_table_double_clicked)
        nodes_layout.addWidget(self._nodes_table)
        tabs.addTab(nodes_widget, "Noder")

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_palette_ui()
        self._load_categories()
        self._proj_name.setText(self.db.get_config('project_name', ''))
        self._proj_number.setText(self.db.get_config('project_number', ''))
        self._proj_client.setText(self.db.get_config('project_client', ''))
        self._proj_facility.setText(self.db.get_config('project_facility', ''))

        today = QDate.currentDate()
        start_str = self.db.get_config('project_date_start', '')
        end_str   = self.db.get_config('project_date_end', '')
        start_d = QDate.fromString(start_str, 'yyyy-MM-dd') if start_str else QDate()
        end_d   = QDate.fromString(end_str, 'yyyy-MM-dd') if end_str else QDate()
        self._proj_date_start.setDate(start_d if start_d.isValid() else today)
        self._proj_date_end.setDate(end_d if end_d.isValid() else today)

        self._load_project_revisions()
        self._load_project_custom_fields()
        self._load_tor_report_fields()
        self.refresh_sheets()
        self.refresh_nodes()

    def _tor_report_add_section(self, outer, section, title):
        """Build one compact sign-off form and wire each value to app_config."""
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(7)
        for key, label in (
                ("prepared", "Framtagen av:"),
                ("reviewed", "Kvalitetsgranskad av:"),
                ("approved", "Godkänd av:")):
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setPlaceholderText("Välj deltagare eller skriv namn")
            combo.setMinimumWidth(280)
            combo.setStyleSheet(
                "QComboBox{padding:3px 6px;border:1px solid #CFD1CE;"
                "border-radius:0px;background:#FFFFFF;}"
                "QComboBox:focus{border:2px solid #2F6FED;padding:2px 5px;}")
            cfg_key = f"{section}_{key}_by"
            combo.currentTextChanged.connect(
                lambda text, k=cfg_key: self.db.set_config(k, text.strip()))
            combo.lineEdit().editingFinished.connect(
                lambda k=cfg_key, cb=combo: self.db.set_config(k, cb.currentText().strip()))
            self._tor_report_fields[(section, key)] = combo
            form.addRow(label, combo)
        outer.addWidget(box)

    def _participant_display_names(self):
        names = []
        for p in self.db.list_participants():
            first = (p['first_name'] or '').strip()
            last = (p['last_name'] or '').strip()
            name = ' '.join(part for part in (first, last) if part)
            if name and name not in names:
                names.append(name)
        return names

    def _load_tor_report_fields(self):
        names = self._participant_display_names()
        for (section, key), combo in self._tor_report_fields.items():
            value = self.db.get_config(f"{section}_{key}_by", '') or ''
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentText(value)
            combo.blockSignals(False)

    def _on_prep_tab_changed(self, index):
        """Refresh participant suggestions when the new tab is opened."""
        if self._tabs.tabText(index) == "ToR and Report":
            self._load_tor_report_fields()

    # ── Blad (2026-08-17, moved from PIDManagementPanel, see NOTES.md) ──────
    def refresh_sheets(self):
        self._sheet_rev_combo.blockSignals(True)
        self._sheet_rev_combo.clear()
        self._sheet_rev_combo.addItem("(ingen)", None)
        for rev in self.db.get_revisions():
            self._sheet_rev_combo.addItem(rev['revision'] or f"Revision {rev['id']}", rev['id'])
        self._sheet_rev_combo.blockSignals(False)

        self._sheet_list.blockSignals(True)
        sheets = self.db.get_sheets()
        self._sheet_list.setRowCount(len(sheets))
        for row, sheet in enumerate(sheets):
            values = [sheet['drawing_number'] or '', sheet['drawing_name'] or sheet['sheet_name'] or '',
                      sheet['drawing_revision'] or '', sheet['drawing_date'] or '', str(sheet['physical_page'] + 1)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, sheet['id'])
                item.setData(Qt.ItemDataRole.UserRole + 1, sheet['revision_id'])
                if col == 4:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._sheet_list.setItem(row, col, item)
            nodes = self.db.nodes_on_page(sheet['physical_page'])
            if nodes:
                names = ', '.join(n['name'] or f"Nod {n['id']}" for n in nodes)
                item.setToolTip(f"Noder på detta blad: {names}")
        self._sheet_list.blockSignals(False)

    def _on_sheets_reordered(self, *_):
        ids = [self._sheet_list.item(i, 0).data(Qt.ItemDataRole.UserRole)
               for i in range(self._sheet_list.rowCount())]
        self.db.reorder_sheets(ids)
        self.refresh_sheets()

    def _rename_sheet(self):
        row = self._sheet_list.currentRow()
        item = self._sheet_list.item(row, 1) if row >= 0 else None
        if item is None:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        current_name = ''
        for s in self.db.get_sheets():
            if s['id'] == sheet_id:
                current_name = s['sheet_name']
                break
        name, ok = QInputDialog.getText(self, "Byt namn", "Bladnamn:", text=current_name)
        if ok and name.strip():
            self.db.update_sheet_name(sheet_id, name.strip())
            self.refresh_sheets()

    def _delete_sheets(self):
        selected = [self._sheet_list.item(row, 0)
                    for row in sorted({idx.row() for idx in self._sheet_list.selectionModel().selectedRows()})]
        if not selected:
            return
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in selected]

        all_sheets = {s['id']: s for s in self.db.get_sheets()}
        pages_info = [(ids[i], all_sheets[ids[i]]['physical_page'],
                       all_sheets[ids[i]]['sheet_name'])
                      for i in range(len(ids)) if ids[i] in all_sheets]
        physical_pages = [p for _, p, _ in pages_info]

        objects = self.db.objects_on_pages(physical_pages)
        affected_lines = []
        for sheet_id, phys, name in pages_info:
            obj = objects.get(phys, {})
            parts = []
            if obj.get('markups'):
                parts.append(f"{obj['markups']} nodmarkering{'ar' if obj['markups'] != 1 else ''}")
            if obj.get('causes'):
                parts.append(f"{obj['causes']} orsak{'er' if obj['causes'] != 1 else ''}")
            if obj.get('consequences'):
                parts.append(f"{obj['consequences']} konsekvens{'er' if obj['consequences'] != 1 else ''}")
            if obj.get('safeguards'):
                parts.append(f"{obj['safeguards']} safeguard{'s' if obj['safeguards'] != 1 else ''}")
            if parts:
                affected_lines.append(f"• {name}: {', '.join(parts)}")

        if affected_lines:
            detail = "\n".join(affected_lines)
            box = QMessageBox(self)
            box.setWindowTitle("Ta bort blad")
            box.setIcon(QMessageBox.Icon.Warning)
            count = len(selected)
            box.setText(
                f"{'Dessa blad innehåller' if count > 1 else 'Detta blad innehåller'} "
                f"HAZOP-objekt som kommer tas bort:")
            box.setInformativeText(detail)
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("Ta bort ändå")
            box.button(QMessageBox.StandardButton.No).setText("Avbryt")
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        else:
            count = len(selected)
            msg = (f"Ta bort {count} blad?" if count > 1
                   else f"Ta bort '{all_sheets[ids[0]]['sheet_name']}'?")
            ans = QMessageBox.question(self, "Ta bort blad", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return

        self.db.delete_objects_on_pages(physical_pages)
        self.db.delete_sheets(ids)
        self.refresh_sheets()
        self.sheets_changed.emit()

    def _sheet_context_menu(self, pos):
        selected_rows = self._sheet_list.selectionModel().selectedRows()
        if not selected_rows:
            return
        menu = QMenu(self)
        if len(selected_rows) == 1:
            menu.addAction(_icon('edit'), "Byt namn", self._rename_sheet)
        menu.addAction(_icon('delete'), "Ta bort", self._delete_sheets)
        menu.exec(self._sheet_list.viewport().mapToGlobal(pos))

    def _on_sheet_item_changed(self, item):
        """Persist editable drawing metadata immediately."""
        if item.column() >= 4:
            return
        row = item.row()
        id_item = self._sheet_list.item(row, 0)
        if id_item is None:
            return
        sheet_id = id_item.data(Qt.ItemDataRole.UserRole)
        values = [self._sheet_list.item(row, col).text().strip()
                  if self._sheet_list.item(row, col) else '' for col in range(4)]
        self.db.update_sheet_metadata(sheet_id, *values)

    def _on_sheet_selection_changed(self, current_row, current_col, previous_row, previous_col):
        current = self._sheet_list.item(current_row, 0) if current_row >= 0 else None
        self._sheet_rev_combo.blockSignals(True)
        if current is None:
            self._sheet_rev_combo.setCurrentIndex(0)
        else:
            rev_id = current.data(Qt.ItemDataRole.UserRole + 1)
            idx = self._sheet_rev_combo.findData(rev_id)
            self._sheet_rev_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sheet_rev_combo.blockSignals(False)

    def _on_sheet_revision_changed(self, _index):
        row = self._sheet_list.currentRow()
        item = self._sheet_list.item(row, 0) if row >= 0 else None
        if item is None:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        rev_id = self._sheet_rev_combo.currentData()
        self.db.set_sheet_revision(sheet_id, rev_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, rev_id)

    # ── Noder (2026-08-17, see NOTES.md "Ny Noder-flik") ─────────────────────
    def refresh_nodes(self):
        sheets_by_page = {s['physical_page']: (s['drawing_name'] or s['sheet_name'] or
                                               f"PDF-sida {s['physical_page'] + 1}")
                          for s in self.db.get_sheets()}
        nodes = self.db.nodes()
        self._nodes_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            self._nodes_table.setItem(row, 0, QTableWidgetItem(f"Nod {row + 1}"))
            name_item = QTableWidgetItem(node['name'] or f"Nod {node['id']}")
            name_item.setData(Qt.ItemDataRole.UserRole, node['id'])
            self._nodes_table.setItem(row, 1, name_item)
            pages = self.db.analysis_pages_for_node(node['id'])
            sheet_names = [sheets_by_page.get(p, f"sida {p + 1}") for p in pages]
            objects_by_page = self.db.analysis_objects_for_node(node['id'])
            details_by_page = self.db.analysis_object_details_for_node(node['id'])
            object_lines = [', '.join(objects_by_page.get(p, [])) or '—' for p in pages]
            detail_lines = [details_by_page.get(p, []) for p in pages]
            self._nodes_table.setItem(row, 2, QTableWidgetItem('\n'.join(sheet_names)))
            self._nodes_table.setItem(row, 3, QTableWidgetItem('\n'.join(object_lines)))
            self._nodes_table.setItem(row, 4, QTableWidgetItem('\n'.join(
                '\n'.join(obj['type'] or '—' for obj in objs) or '—' for objs in detail_lines)))
            self._nodes_table.setItem(row, 5, QTableWidgetItem('\n'.join(
                '\n'.join(', '.join(obj['deviations']) or '—' for obj in objs) or '—'
                for objs in detail_lines)))
            self._nodes_table.setItem(row, 6, QTableWidgetItem('\n'.join(
                '\n'.join(str(obj['count']) for obj in objs) or '0' for objs in detail_lines)))
            self._nodes_table.resizeRowToContents(row)

    def _add_node_from_noder_tab(self):
        self.db.add_node()
        self.refresh_nodes()
        self.structure_changed.emit()

    def _on_nodes_table_double_clicked(self, row, col):
        if col != 1:
            return
        item = self._nodes_table.item(row, 1)
        if item is None:
            return
        node_id = item.data(Qt.ItemDataRole.UserRole)
        node = self.db.get_node(node_id)
        if not node:
            return
        name, ok = QInputDialog.getText(self, "Döp om nod", "Nytt namn:",
                                         text=node['name'] or '')
        if not ok or not name.strip():
            return
        self.db.update_node(node_id, name.strip(), node.get('description') or '',
                             node.get('pid_ref') or '', node.get('media') or '',
                             node.get('pressure') or '', node.get('temperature') or '')
        self.refresh_nodes()
        self.structure_changed.emit()

    def _next_revision_letter(self):
        n = len(self.db.project_revisions())
        letters = ''
        n1 = n
        while True:
            letters = chr(65 + n1 % 26) + letters
            n1 = n1 // 26 - 1
            if n1 < 0:
                break
        return letters

    def _load_project_revisions(self):
        self._proj_rev_table.blockSignals(True)
        rows = self.db.project_revisions()
        self._proj_rev_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            item_label = QTableWidgetItem(row['label'])
            item_label.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._proj_rev_table.setItem(r, 0, item_label)
            date_edit = QDateEdit()
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            d = QDate.fromString(row['date'], 'yyyy-MM-dd')
            date_edit.setDate(d if d.isValid() else QDate.currentDate())
            date_edit.dateChanged.connect(
                lambda d, id_=row['id']: self.db.update_project_revision(
                    id_, date=d.toString('yyyy-MM-dd')))
            self._proj_rev_table.setCellWidget(r, 1, date_edit)
            item_desc = QTableWidgetItem(row['description'])
            item_desc.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._proj_rev_table.setItem(r, 2, item_desc)
        self._proj_rev_table.blockSignals(False)

    def _add_project_revision_row(self):
        label = self._next_revision_letter()
        self.db.add_project_revision(label, QDate.currentDate().toString('yyyy-MM-dd'), '')
        self._load_project_revisions()

    def _on_proj_rev_item_changed(self, item):
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        if item.column() == 0:
            self.db.update_project_revision(id_, label=item.text())
        elif item.column() == 2:
            self.db.update_project_revision(id_, description=item.text())

    def _proj_rev_context_menu(self, pos):
        item = self._proj_rev_table.itemAt(pos)
        if item is None:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        del_action = menu.addAction("Ta bort rad")
        action = menu.exec(self._proj_rev_table.viewport().mapToGlobal(pos))
        if action == del_action and id_ is not None:
            self.db.delete_project_revision(id_)
            self._load_project_revisions()

    def _load_project_custom_fields(self):
        for name_edit, value_edit, row_w in self._proj_field_rows.values():
            self._proj_fields_lay.removeWidget(row_w)
            row_w.deleteLater()
        self._proj_field_rows = {}
        for field in self.db.project_custom_fields():
            self._add_project_custom_field_widget(field['id'], field['name'], field['value'])

    def _add_project_custom_field_widget(self, id_, name, value):
        row_w = QWidget()
        row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(0, 0, 0, 0)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("Fältnamn")
        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText("Värde")
        del_btn = QPushButton("✕")
        del_btn.setFixedWidth(28)
        name_edit.editingFinished.connect(
            lambda: self.db.update_project_custom_field(id_, name=name_edit.text()))
        value_edit.editingFinished.connect(
            lambda: self.db.update_project_custom_field(id_, value=value_edit.text()))
        del_btn.clicked.connect(lambda: self._delete_project_custom_field(id_))
        row_l.addWidget(name_edit)
        row_l.addWidget(value_edit)
        row_l.addWidget(del_btn)
        self._proj_fields_lay.insertWidget(self._proj_fields_lay.count() - 1, row_w)
        self._proj_field_rows[id_] = (name_edit, value_edit, row_w)

    def _add_project_custom_field_row(self):
        id_ = self.db.add_project_custom_field('', '')
        self._add_project_custom_field_widget(id_, '', '')

    def _delete_project_custom_field(self, id_):
        self.db.delete_project_custom_field(id_)
        name_edit, value_edit, row_w = self._proj_field_rows.pop(id_)
        self._proj_fields_lay.removeWidget(row_w)
        row_w.deleteLater()

    # ── Palette ───────────────────────────────────────────────────────────────

    def _load_palette_ui(self):
        # Remove existing swatches (keep the 3 buttons at end)
        for sw in self._palette_swatches:
            self._palette_container.removeWidget(sw)
            sw.deleteLater()
        self._palette_swatches = []
        palette = self.db.get_color_palette()
        for entry in palette:
            sw = DraggableColorSwatch(entry['name'], entry['color'], entry.get('fg_color'))
            # Insert before the "Lägg till / Redigera / Ta bort" buttons
            insert_pos = self._palette_container.count() - 4
            self._palette_container.insertWidget(max(0, insert_pos), sw)
            self._palette_swatches.append(sw)

    def _palette_add(self):
        name, ok = QInputDialog.getText(self, "Ny palettefärg", "Namn (t.ex. Kritisk):")
        if not ok or not name.strip():
            return
        color = QColorDialog.getColor(QColor('#e74c3c'), self, "Välj bakgrundsfärg")
        if not color.isValid():
            return
        # Auto-calculate fg and let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        fg_color_obj = QColorDialog.getColor(QColor(auto_fg), self, "Välj textfärg (auto-föreslagen)")
        fg = fg_color_obj.name() if fg_color_obj.isValid() else auto_fg
        palette = self.db.get_color_palette()
        palette.append({'name': name.strip(), 'color': color.name(), 'fg_color': fg})
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_edit(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Redigera", "Välj färg:", names, 0, False)
        if not ok:
            return
        idx = names.index(chosen)
        new_name, ok2 = QInputDialog.getText(self, "Nytt namn", "Namn:", text=chosen)
        if not ok2:
            return
        new_color = QColorDialog.getColor(QColor(palette[idx]['color']), self, "Välj färg")
        if not new_color.isValid():
            return
        # Ask for text color too
        old_fg = palette[idx].get('fg_color', '#ffffff')
        fg_color_obj = QColorDialog.getColor(QColor(old_fg), self, "Välj textfärg")
        new_fg = fg_color_obj.name() if fg_color_obj.isValid() else old_fg
        palette[idx] = {'name': new_name.strip() or chosen, 'color': new_color.name(), 'fg_color': new_fg}
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_delete(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Ta bort", "Välj färg att ta bort:", names, 0, False)
        if not ok:
            return
        palette = [e for e in palette if e['name'] != chosen]
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    # ── Matrix ────────────────────────────────────────────────────────────────

    def _on_matrix_splitter_moved(self, _pos, _index):
        """Keep grid controls readable when the matrix pane is widened.

        The grid uses compact fixed-size controls by default.  On a drag of
        the splitter handle, distribute the available width over its columns
        so the matrix and the consequence definitions below grow together.
        """
        container = getattr(self, '_matrix_container', None)
        grid = getattr(self, '_matrix_grid', None)
        if container is None or grid is None:
            return
        target = max(0, container.width())
        count = grid.columnCount()
        if target <= 0 or count <= 0:
            return
        # Use current content widths as proportions.  Unlike the previous
        # implementation this also handles dragging left (shrinking), not
        # only dragging right to enlarge the matrix.
        base = []
        for col in range(count):
            widest = 30
            for row in range(grid.rowCount()):
                item = grid.itemAtPosition(row, col)
                widget = item.widget() if item else None
                if widget is not None:
                    widest = max(widest, widget.sizeHint().width())
            base.append(widest)
        natural = max(sum(base), 1)
        usable = max(count * 30, target)
        scaled = [max(30, int(usable * width / natural)) for width in base]
        correction = usable - sum(scaled)
        if correction:
            scaled[-1] += correction
        for col in range(count):
            width = scaled[col]
            grid.setColumnMinimumWidth(col, width)
            for row in range(grid.rowCount()):
                item = grid.itemAtPosition(row, col)
                widget = item.widget() if item else None
                if widget is not None:
                    widget.setMinimumWidth(width)
                    widget.setMaximumWidth(width)

    def _load_matrix_ui(self):
        cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        self._last_built_cfg = None   # reset before blocking so _apply_size sees None
        # Block all signals that would trigger _apply_size while we populate controls
        _senders = (self._rows_spin, self._cols_spin, self._axis_combo,
                    self._x_rev_chk, self._y_rev_chk)
        for w in _senders:
            w.blockSignals(True)
        self._rows_spin.setValue(cfg.get('rows', 5))
        self._cols_spin.setValue(cfg.get('cols', 7))
        x_axis = cfg.get('x_axis', 'frequency')
        idx = self._axis_combo.findData(x_axis)
        if idx >= 0:
            self._axis_combo.setCurrentIndex(idx)
        self._x_rev_chk.setChecked(bool(cfg.get('x_reversed', False)))
        self._y_rev_chk.setChecked(bool(cfg.get('y_reversed', False)))
        for w in _senders:
            w.blockSignals(False)
        self._build_matrix_grid(cfg)

    def _apply_size(self):
        """Rebuild the matrix grid. Handles axis swap without losing data."""
        n_cons    = self._rows_spin.value()
        n_freq    = self._cols_spin.value()
        old       = self.db.get_risk_matrix() or DEFAULT_MATRIX
        new_xaxis = self._axis_combo.currentData() or 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()

        # ── Recover semantic labels ───────────────────────────────────────────
        # Start from last-built config (source of truth for semantic order).
        # Only fall back to DB when the grid has never been built.
        disp          = getattr(self, '_last_built_cfg', None) or old
        disp_freq_on_x = disp.get('x_axis', 'frequency') == 'frequency'
        disp_x_rev    = disp.get('x_reversed', False)
        disp_y_rev    = disp.get('y_reversed', False)

        freq_lbls = list(disp.get('x_labels', old.get('x_labels', FREQ_LABELS[:n_freq])))
        cons_lbls = list(disp.get('y_labels', old.get('y_labels', SEV_LABELS[:n_cons])))

        # Apply any manual text edits from display widgets by mapping each
        # widget directly to its data index (no reversal ambiguity).
        if self._x_label_edits:
            nc = len(self._x_label_edits)
            for c, e in enumerate(self._x_label_edits):
                data_c = (nc - 1 - c) if disp_x_rev else c
                txt = e.text().strip()
                if not txt:
                    continue
                if disp_freq_on_x:
                    if data_c < len(freq_lbls):
                        freq_lbls[data_c] = txt
                else:
                    if data_c < len(cons_lbls):
                        cons_lbls[data_c] = txt

        if self._y_label_edits:
            nr = len(self._y_label_edits)
            for r, e in enumerate(self._y_label_edits):
                data_r = r if disp_y_rev else (nr - 1 - r)
                txt = e.text().strip()
                if not txt:
                    continue
                if disp_freq_on_x:
                    if data_r < len(cons_lbls):
                        cons_lbls[data_r] = txt
                else:
                    if data_r < len(freq_lbls):
                        freq_lbls[data_r] = txt

        # Pad/trim to new dimensions
        while len(freq_lbls) < n_freq:
            freq_lbls.append(f'F{len(freq_lbls)-1}')
        while len(cons_lbls) < n_cons:
            cons_lbls.append(f'C{len(cons_lbls)+1}')
        freq_lbls = freq_lbls[:n_freq]
        cons_lbls = cons_lbls[:n_cons]

        # ── Cell data: current buttons override DB values ─────────────────────
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        lbl2d     = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        # 1. Fill from DB
        old_c  = old.get('cell_colors', [])
        old_l  = old.get('cell_labels', [])
        old_fg = old.get('cell_fg_colors', [])
        for ci in range(n_cons):
            for fi in range(n_freq):
                try:    colors[ci][fi]    = old_c[ci][fi]  or '#27ae60'
                except: colors[ci][fi]    = '#27ae60'
                try:    lbl2d[ci][fi]     = old_l[ci][fi]  or 'Låg'
                except: lbl2d[ci][fi]     = 'Låg'
                try:    fg_colors[ci][fi] = old_fg[ci][fi] or '#ffffff'
                except: fg_colors[ci][fi] = '#ffffff'
        # 2. Override with any user edits in the current buttons
        for _dr, row_btns in self._cell_buttons:
            for btn in row_btns:
                ci, fi = btn.row, btn.col
                if ci < n_cons and fi < n_freq:
                    if btn.color():    colors[ci][fi]    = btn.color()
                    if btn.label():    lbl2d[ci][fi]     = btn.label()
                    if btn.fg_color(): fg_colors[ci][fi] = btn.fg_color()

        new_cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':         new_xaxis,
            'x_reversed':     x_rev,
            'y_reversed':     y_rev,
            'x_labels':       freq_lbls,   # ALWAYS stores frequency labels
            'y_labels':       cons_lbls,   # ALWAYS stores consequence labels
            'cell_colors':    colors,
            'cell_labels':    lbl2d,
            'cell_fg_colors': fg_colors,
            'freq_boundaries': old.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES),
        }
        self._last_built_cfg = new_cfg
        self._build_matrix_grid(new_cfg)

    def _build_matrix_grid(self, cfg):
        """Build the matrix grid respecting axis orientation and intervals."""
        self._last_built_cfg = cfg   # track for _apply_size label recovery
        while self._matrix_grid.count():
            item = self._matrix_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cell_buttons       = []
        self._x_label_edits      = []
        self._y_label_edits      = []
        self._freq_boundary_edits = []
        self._sev_def_edits       = {}
        self._category_row_edits  = []
        self._x_category_rows     = []

        # Data always stored as [consequence_idx][frequency_idx]
        n_cons = cfg.get('rows', 5)    # consequence levels
        n_freq = cfg.get('cols', 7)    # frequency levels
        freq_labels = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        cons_labels = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors          = cfg.get('cell_colors',    [['#27ae60'] * n_freq] * n_cons)
        cell_labels     = cfg.get('cell_labels',    [['Låg']     * n_freq] * n_cons)
        cell_fg_colors  = cfg.get('cell_fg_colors', [['#ffffff'] * n_freq] * n_cons)
        boundaries  = list(cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES))

        x_axis    = cfg.get('x_axis', 'frequency')
        freq_on_x = (x_axis == 'frequency')
        x_rev     = cfg.get('x_reversed', False)   # True = high value on left/top of X
        y_rev     = cfg.get('y_reversed', False)   # True = low value at top of Y

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons   # cols=freq, rows=cons
            col_lbls, row_lbls = freq_labels, cons_labels
            corner_txt = "C \\ F"
            col_tip = "Frekvensetikett (X-axel)\nExempel: F3 – Möjlig | 10-100 år"
            row_tip = "Konsekvensnivå (Y-axel)\nExempel: C4 – Allvarlig"
        else:
            n_dcols, n_drows = n_cons, n_freq   # cols=cons, rows=freq
            col_lbls, row_lbls = cons_labels, freq_labels
            corner_txt = "F \\ C"
            col_tip = "Konsekvensnivå (X-axel)\nExempel: C4 – Allvarlig"
            row_tip = "Frekvensetikett (Y-axel)\nExempel: F3 – Möjlig | 10-100 år"

        _hdr_style = ("font-size:8px; font-weight:bold;"
                      "border:1px solid #aaa; border-radius:0px;"
                      "background:#eef2f7; padding:0 3px;")

        # Corner
        corner = QLabel(corner_txt)
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setStyleSheet("font-size:9px; color:#555;")
        self._matrix_grid.addWidget(corner, 0, 0)

        # Column headers — apply x_rev: if reversed, col 0 shows the highest value
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            txt = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            e = QLineEdit(txt)
            e.setFixedHeight(28)
            e.setMinimumWidth(30)
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e.setStyleSheet(_hdr_style)
            e.setToolTip(col_tip + "\nEtiketten uppdateras automatiskt när du ändrar gränsvärdet.")
            # QLineEdit.setText() leaves the cursor at the END of the text —
            # for a label wider than the fixed 80px field (e.g. "< 0.1/år"
            # at 8px font measures ~96px, see NOTES.md "'<'-tecknet syns
            # inte i gränsvärden"), the widget auto-scrolls to keep the
            # cursor visible, which scrolls the leading "<"/"≥" out of view.
            # Reset to show from the start instead (2026-08-17).
            e.setCursorPosition(0)
            self._matrix_grid.addWidget(e, 0, c + 1)
            self._x_label_edits.append(e)

        # Rows — apply y_rev: if NOT reversed, highest value is at top (default)
        for r in range(n_drows):
            if y_rev:
                disp_r = r              # low at top (r=0 = lowest value)
            else:
                disp_r = n_drows - 1 - r  # high at top (default)

            # Row header
            txt = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            ey = QLineEdit(txt)
            ey.setFixedHeight(40)
            ey.setMinimumWidth(30)
            ey.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ey.setStyleSheet(_hdr_style)
            ey.setToolTip(row_tip)
            ey.setCursorPosition(0)   # see column-header comment above
            self._matrix_grid.addWidget(ey, r + 1, 0)
            self._y_label_edits.append(ey)   # index 0 = top row

            row_btns = []
            for c in range(n_dcols):
                # Resolve display column to data column (accounting for x_rev)
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map display → data (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx = disp_r
                    freq_idx = data_c
                else:
                    freq_idx = disp_r
                    cons_idx = data_c

                try: cc = colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cc = '#27ae60'
                try: cl = cell_labels[cons_idx][freq_idx]
                except (IndexError, KeyError): cl = 'Låg'
                try: cf = cell_fg_colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cf = '#ffffff'

                btn = MatrixCellButton(cons_idx, freq_idx, cc, cl, cf,
                                       is_top_row=(r == 0),
                                       is_left_col=(c == 0))
                btn.clicked.connect(partial(self._edit_cell, btn))
                self._matrix_grid.addWidget(btn, r + 1, c + 1)
                row_btns.append(btn)
            self._cell_buttons.append((disp_r, row_btns))

        # ── Interval / boundary row below cells ───────────────────────────────
        # Only shown when frequency is on X-axis (boundaries are per-frequency-column)
        if freq_on_x:
            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555; padding:0 3px;")
            self._matrix_grid.addWidget(bnd_lbl, n_drows + 1, 0)

            # When x_rev, the highest-freq column is at c=0 (leftmost) — ">allt" moves there
            # and the boundary values follow the reversed column order.
            highest_col = 0 if x_rev else n_dcols - 1
            for c in range(n_dcols):
                if c == highest_col:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, n_drows + 1, c + 1)
                else:
                    # Map display col → data freq index to pick the correct boundary
                    bval_idx = (n_dcols - 1 - c) if x_rev else c
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedHeight(22)
                    e.setMinimumWidth(30)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    e.setToolTip(
                        f"Övre gräns (händelser/år) för kolumn {c}.\n"
                        f"Frekvenser under detta värde tillhör denna kolumn.\n"
                        f"Exempel: 0.1 = en gång per 10 år")
                    self._matrix_grid.addWidget(e, n_drows + 1, c + 1)
                    self._freq_boundary_edits.append(e)
                    # Connect boundary edit → auto-update adjacent axis labels
                    e.editingFinished.connect(
                        lambda _e=e, _c=c: self._sync_freq_label_from_boundary(_e, _c))
        else:
            # When frequency on Y: add interval boundary column on the right
            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555;")
            self._matrix_grid.addWidget(bnd_lbl, 0, n_dcols + 1)

            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            # Last row always gets ">allt" (the extreme bucket with no further boundary).
            # bval_idx depends on y_rev: y_rev=False → high-at-top, reversed boundary order.
            for r in range(n_drows):
                if r == n_drows - 1:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, r + 1, n_dcols + 1)
                else:
                    bval_idx = r if y_rev else (n_drows - 2 - r)
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedHeight(40)
                    e.setMinimumWidth(30)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    self._matrix_grid.addWidget(e, r + 1, n_dcols + 1)
                    self._freq_boundary_edits.append(e)

        # ── Consequence category definitions embedded in matrix ────────────────
        cats = self.db.consequence_categories()
        defs = self.db.get_severity_definitions()  # {sev_level: {cat_id: description}}

        _def_style = ("font-size:9px; border:1px solid #ccc; border-radius:0;"
                      "background:#f8f8ff; padding:1px 3px;")
        _cat_hdr_style = ("font-size:9px; font-weight:bold; background:#e8edf5;"
                          "border:1px solid #bbb; padding:2px 6px;")

        if not freq_on_x:
            # Consequence on X (columns) → category rows go BELOW the matrix
            # n_drows = n_freq; no boundary row exists (boundary is a column)
            base_row = n_drows + 1

            # Thin separator spanning all columns
            sep = QLabel("── Konsekvensdefinitioner ──")
            sep.setStyleSheet("font-size:8px; color:#888; padding:2px 4px;")
            sep.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._matrix_grid.addWidget(sep, base_row, 0, 1, n_dcols + 1)

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_row = base_row + 1 + cat_i

                cat_lbl = QLabel(cat['name'])
                cat_lbl.setStyleSheet(_cat_hdr_style)
                cat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                cat_lbl.setMinimumHeight(CONFIG['H_ROW_STD'])
                self._matrix_grid.addWidget(cat_lbl, cat_row, 0)
                row_edits = []

                for c in range(n_dcols):      # n_dcols = n_cons
                    data_c    = (n_dcols - 1 - c) if x_rev else c
                    sev_level = data_c + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QTextEdit()
                    e.setPlainText(text)
                    e.setMinimumWidth(30)
                    e.setMaximumWidth(1000)
                    e.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                    e.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setMinimumHeight(CONFIG['H_ROW_STD'])
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.textChanged.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.toPlainText().strip()))
                    e.textChanged.connect(self._schedule_category_row_resize)
                    self._matrix_grid.addWidget(e, cat_row, c + 1)
                    self._sev_def_edits[(cat_id, sev_level)] = e
                    row_edits.append(e)
                self._x_category_rows.append((cat_lbl, row_edits))
            self._resize_category_rows()
        else:
            # Consequence on Y (rows) → category columns go to the RIGHT
            # n_dcols = n_freq; no boundary column exists (boundary is a row)
            base_col = n_dcols + 1
            # r -> list of this row's category QTextEdits, used below to size
            # the row header + cell buttons + category cells all to the
            # tallest wrapped text in that row (2026-08-17 user request —
            # only this orientation needed it, the `not freq_on_x` branch
            # above already had a working fixed row height).
            row_cat_edits = [[] for _ in range(n_drows)]

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_col = base_col + cat_i

                cat_hdr = QLabel(cat['name'])
                cat_hdr.setStyleSheet(_cat_hdr_style)
                cat_hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cat_hdr.setMinimumHeight(CONFIG['H_ROW_STD'])
                cat_hdr.setMinimumWidth(30)
                cat_hdr.setMaximumWidth(1000)
                cat_hdr.setWordWrap(True)
                self._matrix_grid.addWidget(cat_hdr, 0, cat_col)

                for r in range(n_drows):      # n_drows = n_cons
                    disp_r    = (n_drows - 1 - r) if not y_rev else r
                    sev_level = disp_r + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QTextEdit()
                    e.setPlainText(text)
                    e.setMinimumWidth(30)
                    e.setMaximumWidth(1000)
                    e.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                    e.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.textChanged.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.toPlainText().strip()))
                    e.textChanged.connect(self._schedule_category_row_resize)
                    self._matrix_grid.addWidget(e, r + 1, cat_col)
                    self._sev_def_edits[(cat_id, sev_level)] = e
                    row_cat_edits[r].append(e)

            self._category_row_edits = row_cat_edits
            self._resize_category_rows()

    def _schedule_category_row_resize(self):
        """Resize matrix rows after wrapped category text changes.

        QTextEdit updates its document layout asynchronously, so queue the
        measurement until the next event-loop turn.  All consequence levels
        deliberately receive the same height, matching the matrix cells.
        """
        QTimer.singleShot(0, self._resize_category_rows)

    def _resize_category_rows(self):
        # Consequence on X: category rows are independent and may therefore
        # grow to different heights according to their own wrapped text.
        x_rows = getattr(self, '_x_category_rows', None) or []
        if x_rows:
            for label, edits in x_rows:
                needed = CONFIG['H_ROW_STD']
                for edit in edits:
                    doc = edit.document()
                    doc.setTextWidth(edit.viewport().width())
                    needed = max(needed, int(doc.size().height()) + 8)
                label.setFixedHeight(needed)
                for edit in edits:
                    edit.setFixedHeight(needed)
            return
        rows = getattr(self, '_category_row_edits', None) or []
        if not rows or not getattr(self, '_y_label_edits', None):
            return
        needed = CONFIG['H_ROW_STD']
        for edits in rows:
            for edit in edits:
                doc = edit.document()
                doc.setTextWidth(edit.viewport().width())
                needed = max(needed, int(doc.size().height()) + 8)
        for row in range(min(len(self._y_label_edits), len(self._cell_buttons))):
            self._y_label_edits[row].setFixedHeight(needed)
            for btn in self._cell_buttons[row][1]:
                btn.setFixedHeight(needed)
            for edit in rows[row]:
                edit.setFixedHeight(needed)

    def _sync_freq_label_from_boundary(self, boundary_edit, col_idx: int):
        """Auto-update the frequency axis label(s) adjacent to the changed boundary."""
        try:
            val = float(boundary_edit.text().strip())
        except ValueError:
            return
        if val <= 0:
            return

        def _fmt(v):
            if v >= 1:       return f"{v:.3g}/år"
            if v >= 0.001:   return f"{v:.3g}/år"
            return f"{v:.2e}/år"

        # Collect all boundary values to compute ranges
        bvals = []
        for e in self._freq_boundary_edits:
            try:
                bvals.append(float(e.text()))
            except ValueError:
                bvals.append(None)

        def _label_for_col(c):
            """Return an auto-generated interval label for display column c."""
            left  = bvals[c-1] if c > 0 and c-1 < len(bvals) else None
            right = bvals[c]   if c < len(bvals) else None
            if left is None and right is not None:
                return f"< {_fmt(right)}"
            if left is not None and right is None:
                return f"≥ {_fmt(left)}"
            if left is not None and right is not None:
                return f"{_fmt(left)} – {_fmt(right)}"
            return ""

        # Update the two adjacent column labels (col_idx and col_idx+1)
        for affected_c in (col_idx, col_idx + 1):
            if 0 <= affected_c < len(self._x_label_edits):
                new_lbl = _label_for_col(affected_c)
                if new_lbl:
                    self._x_label_edits[affected_c].setText(new_lbl)
                    self._x_label_edits[affected_c].setCursorPosition(0)

    def _edit_cell(self, btn):
        """Click a cell → choose background color, label, and text color."""
        color = QColorDialog.getColor(QColor(btn.color()), self, "Välj bakgrundsfärg för cell")
        if not color.isValid():
            return
        label, ok = QInputDialog.getText(
            self, "Celltext", "Risknivå-etikett (t.ex. Låg, Medium, Hög, Kritisk):",
            text=btn.label())
        if not ok:
            return
        # Auto-suggest fg based on luminance; let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        current_fg = btn.fg_color() if btn.fg_color() else auto_fg
        fg_obj = QColorDialog.getColor(QColor(current_fg), self, "Välj textfärg")
        fg = fg_obj.name() if fg_obj.isValid() else current_fg
        btn.set_cell(color.name(), label.strip() or btn.label(), fg)

    def _save_matrix(self):
        n_cons = self._rows_spin.value()   # consequence levels (rows in data)
        n_freq = self._cols_spin.value()   # frequency levels  (cols in data)
        x_axis = self._axis_combo.currentData() or 'frequency'
        freq_on_x = (x_axis == 'frequency')

        # Cell buttons store (cons_idx, freq_idx) regardless of display orientation
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        labels    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        for _disp_r, row_btns in self._cell_buttons:
            for btn in row_btns:
                cons_i, freq_i = btn.row, btn.col   # (cons_idx, freq_idx)
                if cons_i < n_cons and freq_i < n_freq:
                    colors[cons_i][freq_i]    = btn.color()
                    labels[cons_i][freq_i]    = btn.label()
                    fg_colors[cons_i][freq_i] = btn.fg_color()

        # Axis labels: _x_label_edits are the column headers (whatever axis),
        # _y_label_edits are the row headers (reversed, highest at top)
        raw_col = [e.text().strip() for e in self._x_label_edits]
        raw_row = list(reversed([e.text().strip() for e in self._y_label_edits]))  # low→high

        if freq_on_x:
            # X=freq columns, Y=cons rows
            x_labels = raw_col or [f'F{i-1}' for i in range(n_freq)]
            y_labels = raw_row or [f'C{i+1}' for i in range(n_cons)]
        else:
            # X=cons columns, Y=freq rows
            y_labels = raw_col or [f'C{i+1}' for i in range(n_cons)]
            x_labels = raw_row or [f'F{i-1}' for i in range(n_freq)]

        # Pad/trim to correct lengths
        while len(x_labels) < n_freq: x_labels.append(f'F{len(x_labels)-1}')
        while len(y_labels) < n_cons: y_labels.append(f'C{len(y_labels)+1}')
        x_labels = x_labels[:n_freq]
        y_labels = y_labels[:n_cons]

        cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':      x_axis,
            'x_reversed':  self._x_rev_chk.isChecked(),
            'y_reversed':  self._y_rev_chk.isChecked(),
            'x_labels':    x_labels,
            'y_labels':    y_labels,
            'cell_colors':    colors,
            'cell_labels':    labels,
            'cell_fg_colors': fg_colors,
        }
        # Read frequency boundaries from editable row/column (display order)
        freq_boundaries = []
        for e in getattr(self, '_freq_boundary_edits', []):
            try:
                v = float(e.text().strip())
                if v > 0:
                    freq_boundaries.append(v)
            except ValueError:
                pass
        if not freq_boundaries:
            freq_boundaries = list(DEFAULT_FREQ_BOUNDARIES)
        # Boundary edits were laid out in display order; convert back to data order
        # (lowest freq level first) by reversing when the display was reversed:
        #   freq_on_x + x_rev: highest-freq col is leftmost → edits stored high-to-low
        #   freq_on_y + NOT y_rev: highest-freq row is topmost → edits stored high-to-low
        _is_reversed_display = (freq_on_x and self._x_rev_chk.isChecked()) or \
                               (not freq_on_x and not self._y_rev_chk.isChecked())
        if _is_reversed_display:
            freq_boundaries = list(reversed(freq_boundaries))
        cfg['freq_boundaries'] = freq_boundaries

        cfg = _normalise_matrix(cfg)   # ensure consistent before saving
        self.db.set_risk_matrix(cfg)
        # set_risk_matrix() automatically invalidates the cache; reload from DB
        _risk_matrix_cache.reload_from_db()
        QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
        self.matrix_changed.emit()

    def _apply_freq_preset(self, labels: list, bounds: list):
        """Populate frequency axis headers and boundary edits from a preset.

        labels: ordered lowest-to-highest frequency (data order).
        bounds: n-1 boundary values (events/year), data order lowest first.
        Accounts for current axis orientation (freq_on_x/y) and direction (x_rev/y_rev).
        """
        freq_on_x = (self._axis_combo.currentData() or 'frequency') == 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()
        n         = len(labels)

        if freq_on_x:
            # _x_label_edits[i] = display column i → data index (n-1-i if x_rev else i)
            for i, e in enumerate(self._x_label_edits):
                data_idx = (n - 1 - i) if x_rev else i
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
                    e.setCursorPosition(0)
            # _freq_boundary_edits: edit[i] maps to bval_idx (n-1-(i+1) if x_rev else i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = (n - 2 - i) if x_rev else i
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")
        else:
            # _y_label_edits[0] = top row
            # y_rev=False: top=highest freq → data index n-1-i; y_rev=True: top=lowest → i
            for i, e in enumerate(self._y_label_edits):
                data_idx = i if y_rev else (n - 1 - i)
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
                    e.setCursorPosition(0)
            # _freq_boundary_edits for y case: edit[i] → bval_idx (i if y_rev else n-2-i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = i if y_rev else (n - 2 - i)
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")

    def _load_categories(self):
        self._cat_list.clear()
        for cat in self.db.consequence_categories():
            item = QListWidgetItem(cat['name'])
            item.setData(Qt.ItemDataRole.UserRole, cat['id'])
            self._cat_list.addItem(item)

    def _cat_add(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Ny kategori", "Namn:")
        if ok and name.strip():
            self.db.add_category(name.strip())
            self._load_categories()
            self._apply_size()

    def _cat_rename(self):
        from PyQt6.QtWidgets import QInputDialog
        item = self._cat_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_category(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._load_categories()
            self._apply_size()

    def _cat_delete(self):
        item = self._cat_list.currentItem()
        if not item: return
        self.db.delete_category(item.data(Qt.ItemDataRole.UserRole))
        self._load_categories()
        # 2026-08-11 fix ('När jag ... tar bort en konsekvenskategori skall
        # detta synas i riskmatrisen direkt') — _cat_add/_cat_rename already
        # called _apply_size() to rebuild the matrix grid; delete was
        # missing this call, so the matrix kept showing the deleted
        # category's severity-definition row until the next unrelated
        # rebuild (e.g. resizing the rows/cols spinners).
        self._apply_size()
        if hasattr(self, '_sev_def_panel') and self._sev_def_panel:
            self._sev_def_panel.refresh()

    def _cat_move(self, direction):
        """Move the selected category up (direction=-1) or down (+1) in
        display order (2026-08-11, 'jag vill även kunna justera ordningen,
        exempelvis genom vilken ordning de dyker upp')."""
        item = self._cat_list.currentItem()
        if not item:
            return
        row = self._cat_list.row(item)
        new_row = row + direction
        if not (0 <= new_row < self._cat_list.count()):
            return
        ordered_ids = [self._cat_list.item(i).data(Qt.ItemDataRole.UserRole)
                       for i in range(self._cat_list.count())]
        ordered_ids[row], ordered_ids[new_row] = ordered_ids[new_row], ordered_ids[row]
        self.db.reorder_categories(ordered_ids)
        self._load_categories()
        self._cat_list.setCurrentRow(new_row)
        self._apply_size()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════
