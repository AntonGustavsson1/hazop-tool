#!/usr/bin/env python3
"""Settings and HAZOP-preparation panels -- split out of hazop.py
2026-08-17, see NOTES.md "Forenkla koden + dela upp hazop.py i fler
filer". Further split into per-panel modules 2026-08-21 (see NOTES.md
"Dela upp settings_panels.py") -- this file is now the umbrella that
re-exports everything hazop.py already imports from it, so hazop.py
needed zero changes (same layer + re-export pattern used throughout
this codebase)."""

import re
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont

from database import Database, risk_info
from pid_viewer import _icon, FREQ_LABELS, ocr_status
from ui_helpers import freq_axis_label
from equipment_panel import TagDatabasePanel, PIDAnalysisPanel

from hazop_preparation_panel import HAZOPPreparationPanel
from participant_matrix_panel import ParticipantMatrixPanel
from standard_causes_panel import StandardCausesSettingsPanel
from standard_objects_panel import StandardObjectsSettingsPanel
from tag_memory_panel import TagMemoryPanel


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

class SettingsPanel(QWidget):
    """P&ID-scoped and tag-recognition settings. Used to also host Projekt/
    Deltagare/Riskmatris & Kategorier/Standardorsaker — those four moved out
    into their own top-level HAZOPPreparationPanel (2026-08-17, see NOTES.md)
    since Anton wanted them front-and-center in the nav rail rather than
    buried as tabs here. Keeps its own `matrix_changed` (TagDatabasePanel's
    settings_changed still forwards into it, unrelated to the risk matrix
    itself) — see HAZOPPreparationPanel's own docstring for why this signal
    is duplicated across both panels rather than shared."""

    matrix_changed = pyqtSignal()
    pid_render_settings_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        tabs = QTabWidget()
        self._tabs = tabs   # kept as an attribute for testability (tabText() lookups)
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: P&ID-inställningar ───────────────────────────────────────────
        # Renamed from "P&ID" (2026-08-11, user request: "Fliken PID borde
        # kunna ändras till något mer generiskt för inställning" / "Byt namn
        # + lägg till OCR/sid-inställningar"). "P&ID-inställningar" was
        # chosen over a fully generic name like "Analys" or "Inställningar"
        # because this tab already lives inside a settings screen next to
        # "Tagdatabas" and "Identifierade objekt" (both P&ID-specific DATA
        # views) — a bare "Analys" would read as ambiguous next to those,
        # while "P&ID-inställningar" keeps the P&ID scope clear but no
        # longer implies (like the old "P&ID" name did) that tag-stripping
        # is the only setting that belongs here.
        pid_tab = QWidget()
        pid_l = QVBoxLayout(pid_tab)
        pid_l.setContentsMargins(16, 16, 16, 16)
        pid_l.setSpacing(12)

        tag_grp = QGroupBox("Tagg-identifiering")
        tag_gl = QVBoxLayout(tag_grp)
        tag_gl.setSpacing(6)

        self._strip_spaces_chk = QCheckBox(
            "Ta bort mellanslag i tagg-nummer  (t.ex. \"P 101\" → \"P101\")")
        self._strip_spaces_chk.setToolTip(
            "När ett tagg-nummer identifieras via klick eller gummiband på P&ID\n"
            "tas alla mellanslag bort automatiskt innan det fylls i tag-fältet.")
        self._strip_spaces_chk.toggled.connect(
            lambda on: self.db.set_config('tag_strip_spaces', '1' if on else '0'))
        tag_gl.addWidget(self._strip_spaces_chk)
        tag_gl.addWidget(QLabel('Ersätt tecken i identifierade taggar:'))
        tag_gl.addWidget(QLabel('Ange tecknet som ska ersättas i X och ersättningen i Y.'))
        self._replacement_rows = []
        self._replacement_rows_host = QWidget()
        self._replacement_rows_layout = QVBoxLayout(self._replacement_rows_host)
        self._replacement_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._replacement_rows_layout.setSpacing(3)
        tag_gl.addWidget(self._replacement_rows_host)
        add_rule_btn = QPushButton('+')
        add_rule_btn.setFixedWidth(28)
        add_rule_btn.setToolTip('Lägg till ersättningsregel')
        add_rule_btn.clicked.connect(lambda: self._add_replacement_row())
        tag_gl.addWidget(add_rule_btn, 0, Qt.AlignmentFlag.AlignLeft)

        pid_l.addWidget(tag_grp)

        # ── Objektidentifiering ──────────────────────────────────────────
        # Anton: "Jag vill också implemetera en inställning som gör hur
        # länge programmet maximalt letar efter en tag." (2026-08-18, see
        # NOTES.md "kombinerad placeringsmeny") — placing a new object via
        # högerklick/gummiband now opens EquipmentPlacementPopup instantly
        # instead of waiting for the native-text/OCR tag search to finish;
        # this caps how long that background search (PIDPanel.
        # _start_equipment_tag_search) gets before the popup gives up
        # waiting and leaves the tag field open for manual entry.
        search_grp = QGroupBox("Objektidentifiering")
        search_gl = QVBoxLayout(search_grp)
        search_gl.setSpacing(6)
        search_lbl = QLabel(
            "När ett nytt objekt placeras på P&ID (högerklick eller\n"
            "gummiband) visas rutan direkt, och letandet efter en tagg\n"
            "sker i bakgrunden. Hur länge det max får pågå innan fältet\n"
            "lämnas öppet för manuell inmatning:")
        search_lbl.setWordWrap(True)
        search_gl.addWidget(search_lbl)
        search_row = QHBoxLayout()
        self._tag_search_timeout_spin = QDoubleSpinBox()
        self._tag_search_timeout_spin.setRange(0.5, 10.0)
        self._tag_search_timeout_spin.setSingleStep(0.5)
        self._tag_search_timeout_spin.setDecimals(1)
        self._tag_search_timeout_spin.setSuffix(" s")
        self._tag_search_timeout_spin.valueChanged.connect(
            lambda v: self.db.set_config(
                'equipment_tag_search_timeout_ms', str(round(v * 1000))))
        search_row.addWidget(self._tag_search_timeout_spin)
        search_row.addStretch()
        search_gl.addLayout(search_row)
        pid_l.addWidget(search_grp)

        # ── OCR-standardval ───────────────────────────────────────────────
        # Lets the user skip the per-scan "Använd OCR?" Yes/No prompt shown
        # by "🔍 Skanna P&ID" (EquipmentPanel._scan, hazop.py) and "📋
        # Analysera P&ID" (PIDPanel._analyze_pid, pid_viewer.py) by picking
        # a fixed default engine ahead of time. Wired into both scan entry
        # points via pid_viewer.resolve_ocr_scan_choice() — this is NOT a
        # dead setting, it actually changes scan behaviour.
        ocr_grp = QGroupBox("OCR-standardval")
        ocr_gl = QVBoxLayout(ocr_grp)
        ocr_gl.setSpacing(6)
        ocr_lbl = QLabel(
            "Motor att använda automatiskt vid P&ID-skanning\n"
            "(🔍 Skanna P&ID / 📋 Analysera P&ID), utan att fråga varje gång:")
        ocr_gl.addWidget(ocr_lbl)
        self._ocr_default_combo = QComboBox()
        self._ocr_default_combo.addItem("Fråga varje gång (standard)", 'ask')
        self._ocr_default_combo.addItem("Automatiskt — bästa tillgängliga motor", 'auto')
        _ocr_st = ocr_status()
        if _ocr_st.get('rapidocr'):
            self._ocr_default_combo.addItem("RapidOCR", 'rapidocr')
        if _ocr_st.get('tesseract'):
            self._ocr_default_combo.addItem("Tesseract", 'tesseract')
        if _ocr_st.get('easyocr'):
            self._ocr_default_combo.addItem("EasyOCR", 'easyocr')
        self._ocr_default_combo.setToolTip(
            "Styr om/vilken OCR-motor som används automatiskt vid P&ID-skanning —\n"
            "hoppar då över Ja/Nej-frågan om OCR för den körningen.\n"
            "\"Fråga varje gång\" behåller nuvarande beteende.")
        self._ocr_default_combo.currentIndexChanged.connect(
            lambda: self.db.set_config(
                'ocr_default_engine', self._ocr_default_combo.currentData()))
        ocr_gl.addWidget(self._ocr_default_combo)
        pid_l.addWidget(ocr_grp)

        # ── Sid-orientering ───────────────────────────────────────────────
        # Investigated first (per process convention) whether an
        # auto-detection system already exists: it does not — the app
        # always just follows the PDF's own /Rotate page attribute
        # (fitz_page.rotation_matrix, see PIDPanel._highlight_tags in
        # pid_viewer.py), there is no heuristic "guess the orientation"
        # layer to conflict with. This setting is therefore stored as a
        # forward-looking override/hint only; it is NOT yet read by the
        # rendering/scanning pipeline (that would mean threading an
        # override through PDF rendering, OCR preprocessing, and the
        # multi-process scan workers — out of scope for this change; see
        # NOTES.md "Kända begränsningar" for this known limitation).
        orient_grp = QGroupBox("Sid-orientering")
        orient_gl = QVBoxLayout(orient_grp)
        orient_gl.setSpacing(6)
        orient_lbl = QLabel(
            "Förvalt antagande om sidans orientering vid rendering/analys\n"
            "av P&ID-sidor. OBS: sparas som inställning men styr ännu inte\n"
            "den faktiska renderingen/analysen (appen använder idag alltid\n"
            "PDF-filens egen rotationsflagga automatiskt) — känd begränsning,\n"
            "se NOTES.md.")
        orient_lbl.setWordWrap(True)
        orient_gl.addWidget(orient_lbl)
        self._page_orientation_combo = QComboBox()
        self._page_orientation_combo.addItem(
            "Använd PDF:ens egen rotation (standard)", 'auto')
        self._page_orientation_combo.addItem("Tvinga liggande", 'landscape')
        self._page_orientation_combo.addItem("Tvinga stående", 'portrait')
        self._page_orientation_combo.currentIndexChanged.connect(
            lambda: self.db.set_config(
                'pid_page_orientation_hint', self._page_orientation_combo.currentData()))
        orient_gl.addWidget(self._page_orientation_combo)
        pid_l.addWidget(orient_grp)

        # ── Tunna P&ID-linjer ────────────────────────────────────────────────
        line_grp = QGroupBox("P&ID-linjer")
        line_gl = QVBoxLayout(line_grp)
        line_gl.setSpacing(6)
        line_lbl = QLabel(
            "Förstärker mycket tunna linjer i skärmvisningen så att de "
            "inte försvinner vid låg zoom. Påverkar inte original-PDF:en "
            "eller sparade positioner.")
        line_lbl.setWordWrap(True)
        line_gl.addWidget(line_lbl)
        self._min_pid_lines_chk = QCheckBox("Förstärk tunna linjer")
        self._min_pid_lines_chk.setToolTip(
            "Gör mycket tunna streck tydligare i P&ID-vyn. Avmarkera "
            "för originalets oförändrade rastervisning.")
        self._min_pid_lines_chk.toggled.connect(self._on_pid_line_setting_changed)
        line_gl.addWidget(self._min_pid_lines_chk)
        line_row = QHBoxLayout()
        line_row.addWidget(QLabel("Förstärkning:"))
        self._min_pid_lines_spin = QSpinBox()
        self._min_pid_lines_spin.setRange(1, 4)
        self._min_pid_lines_spin.setValue(1)
        self._min_pid_lines_spin.setSuffix(" px")
        self._min_pid_lines_spin.setToolTip(
            "Antal bildpunkter som läggs till på varje sida av mycket tunna streck.")
        self._min_pid_lines_spin.valueChanged.connect(self._on_pid_line_setting_changed)
        line_row.addWidget(self._min_pid_lines_spin)
        line_row.addStretch()
        line_gl.addLayout(line_row)
        pid_l.addWidget(line_grp)

        pid_l.addStretch()
        tabs.addTab(pid_tab, "P&ID-inställningar")

        # ── Tab: Tagdatabas ───────────────────────────────────────────────────
        self._tag_db_panel = TagDatabasePanel(self.db)
        self._tag_db_panel.settings_changed.connect(self.matrix_changed.emit)
        tabs.addTab(self._tag_db_panel, "Tagdatabas")

        # ── Tab: Identifierade objekt ─────────────────────────────────────────
        self.analysis_panel = PIDAnalysisPanel(self.db)
        tabs.addTab(self.analysis_panel, "Identifierade objekt")

        # ── Tab: Standardobjekt ───────────────────────────────────────────────
        self._std_objects_panel = StandardObjectsSettingsPanel(self.db)
        tabs.addTab(self._std_objects_panel, "Standardobjekt")

        # ── Tab: Smart igenkänning ────────────────────────────────────────────
        self._tag_memory_panel = TagMemoryPanel(self.db)
        tabs.addTab(self._tag_memory_panel, _icon('brain'), "Smart igenkänning")
        tabs.currentChanged.connect(
            lambda i: self._tag_memory_panel.refresh()
            if tabs.widget(i) is self._tag_memory_panel else None)

        self._load_all()

    def _load_all(self):
        self._strip_spaces_chk.setChecked(
            self.db.get_config('tag_strip_spaces', '1') == '1')
        self._load_replacement_rows(
            self.db.get_config('tag_identifier_replacements', '') or '')
        self._min_pid_lines_chk.setChecked(
            self.db.get_config('pid_min_line_width_enabled', '1') == '1')
        try:
            width = int(self.db.get_config('pid_min_line_width', '1') or '1')
        except (TypeError, ValueError):
            width = 1
        self._min_pid_lines_spin.setValue(max(1, min(4, width)))
        self._min_pid_lines_spin.setEnabled(self._min_pid_lines_chk.isChecked())

    def _add_replacement_row(self, source='', target=''):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        source_edit = QLineEdit(source)
        source_edit.setPlaceholderText('X')
        source_edit.setToolTip('Tecken eller text som ska ersättas')
        target_edit = QLineEdit(target)
        target_edit.setPlaceholderText('Y')
        target_edit.setToolTip('Ersättande tecken eller text')
        arrow = QLabel('→')
        confirm_btn = QPushButton('Bekräfta')
        confirm_btn.setToolTip(
            'Visa definierade objekt där X finns och bekräfta ersättning')
        confirm_btn.clicked.connect(
            lambda: self._confirm_replacement(source_edit, target_edit))
        remove_btn = QPushButton('-')
        remove_btn.setFixedWidth(28)
        remove_btn.setToolTip('Ta bort ersättningsregel')
        layout.addWidget(source_edit)
        layout.addWidget(arrow)
        layout.addWidget(target_edit)
        layout.addWidget(confirm_btn)
        layout.addWidget(remove_btn)
        self._replacement_rows_layout.addWidget(row)
        entry = (row, source_edit, target_edit)
        self._replacement_rows.append(entry)
        source_edit.editingFinished.connect(self._save_replacement_rows)
        target_edit.editingFinished.connect(self._save_replacement_rows)
        remove_btn.clicked.connect(lambda: self._remove_replacement_row(entry))
        return entry

    def _confirm_replacement(self, source_edit, target_edit):
        """Preview and explicitly apply a tag replacement to defined objects."""
        source = source_edit.text().strip()
        target = target_edit.text().strip()
        if not source:
            QMessageBox.information(self, 'Ersätt tecken',
                                    'Ange först tecknet/texten som ska ersättas.')
            return
        rows = self.db.conn.execute(
            "SELECT id, tag, equipment_type, pid_page FROM equipment_catalog "
            "WHERE LOWER(tag) LIKE LOWER(?) ORDER BY prefix, tag",
            (f'%{source}%',)).fetchall()
        if not rows:
            QMessageBox.information(self, 'Ersätt tecken',
                                    f'Inga definierade objekt innehåller “{source}”.')
            return
        lines = []
        for row in rows:
            old = row['tag'] or ''
            new = re.sub(re.escape(source), target, old, flags=re.IGNORECASE)
            lines.append(f'{old}  →  {new}')
        answer = QMessageBox.question(
            self, 'Bekräfta ersättning',
            f'{len(lines)} definierade objekt innehåller “{source}”.\n\n' +
            '\n'.join(lines[:30]) +
            ('\n…' if len(lines) > 30 else '') +
            '\n\nSka objekten och deras P&ID-kopplingar uppdateras?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            changes = self.db.replace_equipment_tag_text(source, target)
        except Exception as exc:
            QMessageBox.critical(self, 'Ersättning misslyckades', str(exc))
            return
        QMessageBox.information(self, 'Ersättning klar',
                                f'{len(changes)} objekt uppdaterades.')
        if hasattr(self, 'analysis_panel'):
            self.analysis_panel.refresh()

    def _remove_replacement_row(self, entry):
        if entry not in self._replacement_rows:
            return
        self._replacement_rows.remove(entry)
        entry[0].deleteLater()
        self._save_replacement_rows()

    def _load_replacement_rows(self, raw):
        for entry in list(self._replacement_rows):
            entry[0].deleteLater()
        self._replacement_rows.clear()
        for rule in str(raw or '').split(';'):
            rule = rule.strip()
            if not rule:
                continue
            if '->' in rule:
                source, target = rule.split('->', 1)
            elif ':' in rule:
                source, target = rule.split(':', 1)
            elif len(rule) >= 3:
                source, target = rule[0], rule[2:]
            else:
                continue
            self._add_replacement_row(source.strip(), target.strip())

    def _save_replacement_rows(self):
        rules = []
        for _row, source_edit, target_edit in self._replacement_rows:
            source, target = source_edit.text().strip(), target_edit.text().strip()
            if source:
                rules.append(f'{source}:{target}')
        self.db.set_config('tag_identifier_replacements', '; '.join(rules))

        timeout_ms = int(self.db.get_config('equipment_tag_search_timeout_ms', '2000') or '2000')
        self._tag_search_timeout_spin.setValue(timeout_ms / 1000)

        idx = self._ocr_default_combo.findData(self.db.get_config('ocr_default_engine', 'ask'))
        if idx >= 0:
            self._ocr_default_combo.setCurrentIndex(idx)
        idx = self._page_orientation_combo.findData(
            self.db.get_config('pid_page_orientation_hint', 'auto'))
        if idx >= 0:
            self._page_orientation_combo.setCurrentIndex(idx)
    def _on_pid_line_setting_changed(self, *_args):
        enabled = self._min_pid_lines_chk.isChecked()
        self._min_pid_lines_spin.setEnabled(enabled)
        self.db.set_config('pid_min_line_width_enabled', '1' if enabled else '0')
        self.db.set_config('pid_min_line_width', str(self._min_pid_lines_spin.value()))
        self.pid_render_settings_changed.emit()

    def refresh_tag_memory(self):
        """Refresh the Smart igenkänning tab so newly learned tags show up."""
        self._tag_memory_panel.refresh()


class PIDManagementPanel(QWidget):
    """PID revision history and sheet reordering panel."""
    sheets_changed = pyqtSignal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Revision history ───────────────────────────────────────────
        rev_widget = QWidget()
        rev_layout = QVBoxLayout(rev_widget)
        rev_layout.setContentsMargins(8, 8, 8, 8)
        rev_layout.setSpacing(6)

        rev_hdr = QHBoxLayout()
        rev_hdr.addWidget(QLabel("Revisionshistorik:"))
        rev_hdr.addStretch()
        clear_all_btn = QPushButton("Rensa samtliga P&ID och all data")
        clear_all_btn.setIcon(_icon('delete'))
        clear_all_btn.setStyleSheet(
            "QPushButton{color:#fff;background:#C62828;border-radius:4px;padding:4px 10px;}"
            "QPushButton:hover{background:#B71C1C;}")
        clear_all_btn.clicked.connect(self._clear_all_pid)
        rev_hdr.addWidget(clear_all_btn)
        rev_layout.addLayout(rev_hdr)

        self._rev_table = QTableWidget(0, 4)
        self._rev_table.setHorizontalHeaderLabels(['Revision', 'Anteckningar', 'Datum', 'PDF-fil'])
        self._rev_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._rev_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self._rev_table.setColumnWidth(0, 120)
        self._rev_table.setColumnWidth(2, 130)
        self._rev_table.setColumnWidth(3, 180)
        self._rev_table.verticalHeader().setVisible(False)
        self._rev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._rev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._rev_table.setAlternatingRowColors(True)
        self._rev_table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        rev_layout.addWidget(self._rev_table)
        tabs.addTab(rev_widget, "Revisioner")

        # "Blad" (sheet ordering/rename/delete) moved out to
        # HAZOPPreparationPanel (2026-08-17, see NOTES.md) — this panel now
        # only manages revision history. sheets_changed is still emitted
        # from here by _clear_all_pid (which also wipes sheets), so the
        # moved Blad list can still react to a full-clear from this tab.
        self.refresh()

    def refresh(self):
        self._rev_table.setRowCount(0)
        for rev in self.db.get_revisions():
            r = self._rev_table.rowCount()
            self._rev_table.insertRow(r)
            self._rev_table.setItem(r, 0, QTableWidgetItem(rev['revision'] or ''))
            self._rev_table.setItem(r, 1, QTableWidgetItem(rev['notes'] or ''))
            self._rev_table.setItem(r, 2, QTableWidgetItem(rev['created_at'] or ''))
            fname = Path(rev['pdf_path']).name if rev['pdf_path'] else ''
            self._rev_table.setItem(r, 3, QTableWidgetItem(fname))
            self._rev_table.setRowHeight(r, 24)

    def _clear_all_pid(self):
        count = len(self.db.get_revisions())
        n_sheets = self.db.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]
        box = QMessageBox(self)
        box.setWindowTitle("Rensa samtliga P&ID och all data")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"Det finns <b>{count} P&ID-revision{'er' if count != 1 else ''}</b> "
            f"och <b>{n_sheets} blad</b> inlagda.\n\n"
            f"Vill du permanent ta bort <b>alla</b> P&ID, blad, markeringar och kopplingar?")
        box.setInformativeText(
            "Denna åtgärd kan inte ångras. HAZOP-analysen (noder, orsaker, konsekvenser) "
            "berörs inte, men alla positioner på P&ID-vyn raderas.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("Rensa allt")
        box.button(QMessageBox.StandardButton.No).setText("Avbryt")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.db.clear_all_pid_data()
        self.refresh()
        self.sheets_changed.emit()


class StudyManagementPanel(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Studiehantering")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Statistics ─────────────────────────────────────────────────
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(8)

        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet(
            "background:#f0f4f8; border:1px solid #ccc; border-radius:6px; padding:10px;")
        stats_layout.addWidget(self._stats_lbl)

        bar = QHBoxLayout()
        refresh_btn = QPushButton("Uppdatera")
        refresh_btn.setIcon(_icon('refresh'))
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn)
        backup_btn = QPushButton("Skapa säkerhetskopia nu")
        backup_btn.setIcon(_icon('save'))
        backup_btn.setToolTip(
            "Automatiska säkerhetskopior sparas redan löpande i hazop_backups/ — "
            "denna knapp tvingar fram en omedelbar kopia, utan att vänta på nästa automatiska tillfälle.")
        backup_btn.clicked.connect(self._backup_now)
        bar.addWidget(backup_btn)
        bar.addStretch()
        stats_layout.addLayout(bar)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ['Nod', 'Orsak', 'L', 'Konsekvens', 'S', 'Risknivå', 'Kategori', 'Safeguards'])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in [2, 3, 4, 5, 6, 7]:
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 100)
        self._table.setColumnWidth(2, 28)
        self._table.setColumnWidth(4, 28)
        self._table.setColumnWidth(5, 80)
        self._table.setColumnWidth(6, 80)
        self._table.setColumnWidth(7, 150)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        stats_layout.addWidget(self._table)
        tabs.addTab(stats_widget, "Statistik")

        # ── Tab 1: PID management ─────────────────────────────────────────────
        self._pid_mgmt = PIDManagementPanel(db)
        tabs.addTab(self._pid_mgmt, "PID-hantering")

        self.refresh()

    def _backup_now(self):
        dst = self.db._write_backup(startup=True)   # startup=True bypasses the throttle
        if dst is not None:
            QMessageBox.information(self, "Säkerhetskopia skapad",
                f"Sparade en säkerhetskopia:\n{dst}")
        else:
            QMessageBox.warning(self, "Säkerhetskopiering misslyckades",
                "Kunde inte skapa säkerhetskopian. Kontrollera att det finns "
                "diskutrymme och skrivbehörighet i projektmappen.")

    def refresh(self):
        s = self.db.stats()
        self._stats_lbl.setText(
            f"  Noder: <b>{s['nodes']}</b>   |   Orsaker: <b>{s['causes']}</b>   |   "
            f"Konsekvenser: <b>{s['consequences']}</b>   |   Safeguards: <b>{s['safeguards']}</b>   |   "
            f"Öppna rekommendationer: <b>{s['open_recommendations']}</b>")
        self._stats_lbl.setTextFormat(Qt.TextFormat.RichText)

        self._table.setRowCount(0)
        for row in self.db.all_data():
            level, bg, fg = risk_info(row['likelihood'], row['severity'])
            r = self._table.rowCount()
            self._table.insertRow(r)

            def _c(t, center=False):
                item = QTableWidgetItem(str(t))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if center else
                                      Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                return item

            self._table.setItem(r, 0, _c(row['node_name']))
            self._table.setItem(r, 1, _c(row['cause']))
            self._table.setItem(r, 2, _c(row['likelihood'], True))
            self._table.setItem(r, 3, _c(row['consequence']))
            self._table.setItem(r, 4, _c(row['severity'], True))
            risk_item = QTableWidgetItem(f"{level}\nF={row['likelihood']} C={row['severity']}")
            risk_item.setBackground(QBrush(QColor(bg)))
            risk_item.setForeground(QBrush(QColor(fg)))
            risk_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(r, 5, risk_item)
            self._table.setItem(r, 6, _c(row['category']))
            sg_text = '; '.join(
                f"{s['description']}{'(RRF' + str(s['rrf']) + ')' if s['rrf'] > 1 else ''}"
                for s in row['safeguards']) or '—'
            self._table.setItem(r, 7, _c(sg_text))
            self._table.setRowHeight(r, 28)

    def refresh_pid(self):
        self._pid_mgmt.refresh()


# Keep old name as alias so any remaining references don't crash
AdminPanel = StudyManagementPanel
