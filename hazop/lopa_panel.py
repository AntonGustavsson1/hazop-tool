"""First LOPA workspace page.

The page is intentionally a small, usable vertical slice: create/manual
number LOPAs, manage revisions, inspect HAZOP-linked scenarios and see the
calculated governing RRF/SIL.  Detailed editors and the HAZOP context-menu
entry build on the same Database methods in subsequent phases.
"""

from __future__ import annotations

import json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database import Database
from lopa_export import export_lopa_excel
from design import (
    LOPA_CARD_PADDING,
    MUTED_TEXT,
    SECONDARY_TEXT,
    TEXT,
    lopa_card_stylesheet,
    lopa_note_stylesheet,
    lopa_section_title_stylesheet,
    lopa_status_stylesheet,
    lopa_table_stylesheet,
    lopa_title_stylesheet,
)


class LopaPanel(QWidget):
    """LOPA list + revision detail page, independent of ``hazop.py``."""

    changed = pyqtSignal()
    hazop_navigation_requested = pyqtSignal(int)

    _ROLE_LOPA_ID = int(Qt.ItemDataRole.UserRole)
    _ROLE_REVISION_ID = int(Qt.ItemDataRole.UserRole) + 1
    _ROLE_SOURCE_ID = int(Qt.ItemDataRole.UserRole) + 2
    _ROLE_ENTITY_ID = int(Qt.ItemDataRole.UserRole) + 3
    _ROLE_FACTOR_KEY = int(Qt.ItemDataRole.UserRole) + 4

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._lopa_id = None
        self._revision_id = None
        self._source_id = None
        self._sensor_group_id = None
        self._final_group_id = None
        self._loading = False
        self._build()
        self.refresh()

    def _card(self):
        card = QFrame()
        card.setObjectName('lopaCard')
        card.setStyleSheet(lopa_card_stylesheet())
        return card

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        heading = QHBoxLayout()
        title = QLabel('LOPA')
        title.setStyleSheet(lopa_title_stylesheet())
        heading.addWidget(title)
        subtitle = QLabel('Skyddsbarriäranalys – samma riskmatris som HAZOP')
        subtitle.setStyleSheet(lopa_note_stylesheet())
        heading.addWidget(subtitle)
        heading.addStretch(1)
        self._show_archived = QPushButton('Visa arkiverade')
        self._show_archived.setCheckable(True)
        self._show_archived.toggled.connect(self.refresh)
        heading.addWidget(self._show_archived)
        self._new_btn = QPushButton('Ny tom LOPA')
        self._new_btn.clicked.connect(self._create_lopa)
        heading.addWidget(self._new_btn)
        self._export_btn = QPushButton('Exportera Excel…')
        self._export_btn.clicked.connect(self._export_excel)
        heading.addWidget(self._export_btn)
        outer.addLayout(heading)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        left = self._card()
        left.setMinimumWidth(215)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                       LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        label = QLabel('LOPA-ark')
        label.setStyleSheet(lopa_section_title_stylesheet())
        left_layout.addWidget(label)
        hint = QLabel('Varje ark är en SIF. Arkiverade ark kan återställas men automatiska nummer återanvänds inte.')
        hint.setWordWrap(True)
        hint.setStyleSheet(lopa_note_stylesheet())
        left_layout.addWidget(hint)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentItemChanged.connect(self._on_lopa_selected)
        left_layout.addWidget(self._list, 1)
        splitter.addWidget(left)

        detail = QScrollArea()
        detail.setWidgetResizable(True)
        detail.setFrameShape(QFrame.Shape.NoFrame)
        detail_body = QWidget()
        detail_layout = QVBoxLayout(detail_body)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)

        header = self._card()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                         LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        top = QHBoxLayout()
        self._record_title = QLabel('Välj eller skapa en LOPA')
        self._record_title.setStyleSheet('font-size:15px;font-weight:700;')
        top.addWidget(self._record_title)
        top.addStretch(1)
        self._status = QLabel('')
        self._status.hide()
        top.addWidget(self._status)
        self._new_revision_btn = QPushButton('Ny revision')
        self._new_revision_btn.clicked.connect(self._create_revision)
        top.addWidget(self._new_revision_btn)
        self._lock_btn = QPushButton('Lås revision')
        self._lock_btn.clicked.connect(self._toggle_lock)
        top.addWidget(self._lock_btn)
        self._archive_btn = QPushButton('Arkivera LOPA')
        self._archive_btn.clicked.connect(self._archive_lopa)
        top.addWidget(self._archive_btn)
        header_layout.addLayout(top)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._number = QLineEdit()
        self._number.setPlaceholderText('001')
        self._number.editingFinished.connect(self._save_header)
        self._sif_number = QLineEdit()
        self._sif_number.setPlaceholderText('SIF-001')
        self._sif_number.editingFinished.connect(self._save_header)
        self._sif_name = QLineEdit()
        self._sif_name.setPlaceholderText('SIF-beteckning / namn')
        self._sif_name.editingFinished.connect(self._save_header)
        self._sis_name = QLineEdit()
        self._sis_name.setPlaceholderText('SIS / system')
        self._sis_name.editingFinished.connect(self._save_header)
        self._performed_by = QLineEdit()
        self._performed_by.setPlaceholderText('Utförd av')
        self._performed_by.editingFinished.connect(self._save_revision_details)
        self._choose_performed_btn = QPushButton('Välj deltagare…')
        self._choose_performed_btn.clicked.connect(self._choose_performed_by)
        self._approved_by = QLineEdit()
        self._approved_by.setPlaceholderText('Godkänd av')
        self._approved_by.editingFinished.connect(self._save_revision_details)
        self._document_date = QLineEdit()
        self._document_date.setPlaceholderText('YYYY-MM-DD')
        self._document_date.editingFinished.connect(self._save_revision_details)
        self._revision = QComboBox()
        self._revision.currentIndexChanged.connect(self._on_revision_changed)
        form.addRow('LOPA-nr', self._number)
        form.addRow('SIF-nr', self._sif_number)
        form.addRow('SIF-namn', self._sif_name)
        form.addRow('SIS', self._sis_name)
        form.addRow('Revision', self._revision)
        form.addRow('Datum', self._document_date)
        performed_row = QWidget()
        performed_layout = QHBoxLayout(performed_row)
        performed_layout.setContentsMargins(0, 0, 0, 0)
        performed_layout.addWidget(self._performed_by, 1)
        performed_layout.addWidget(self._choose_performed_btn)
        form.addRow('Utförd av', performed_row)
        form.addRow('Godkänd av', self._approved_by)
        header_layout.addLayout(form)
        self._sync_note = QLabel('Tom LOPA: koppla en HAZOP-barriär för att importera källscenario och givardel.')
        self._sync_note.setWordWrap(True)
        self._sync_note.setStyleSheet(lopa_note_stylesheet())
        header_layout.addWidget(self._sync_note)
        detail_layout.addWidget(header)

        source_card = self._card()
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                         LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        source_label = QLabel('Källscenarier från HAZOP')
        source_label.setStyleSheet(lopa_section_title_stylesheet())
        source_layout.addWidget(source_label)
        self._sources = QTableWidget(0, 6)
        self._sources.setHorizontalHeaderLabels([
            'Aktiv', 'Objekt / anrop', 'Orsak', 'Grundfrekvens', 'HAZOP-koppling', 'Status',
        ])
        self._sources.verticalHeader().setVisible(False)
        self._sources.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._sources.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._sources.setWordWrap(True)
        self._sources.horizontalHeader().setStretchLastSection(True)
        self._sources.setStyleSheet(lopa_table_stylesheet())
        self._sources.setMinimumHeight(150)
        self._sources.itemChanged.connect(self._on_source_item_changed)
        self._sources.itemSelectionChanged.connect(self._on_source_selection_changed)
        source_layout.addWidget(self._sources)
        source_actions = QHBoxLayout()
        self._sync_sources_btn = QPushButton('Kontrollera HAZOP-kopplingar')
        self._sync_sources_btn.clicked.connect(self._check_hazop_links)
        source_actions.addWidget(self._sync_sources_btn)
        source_actions.addStretch(1)
        self._source_sync_note = QLabel('')
        self._source_sync_note.setStyleSheet(lopa_note_stylesheet())
        source_actions.addWidget(self._source_sync_note)
        source_layout.addLayout(source_actions)
        detail_layout.addWidget(source_card, 2)

        scenario_card = self._card()
        scenario_layout = QVBoxLayout(scenario_card)
        scenario_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                           LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        scenario_title = QLabel('Scenario')
        scenario_title.setStyleSheet(lopa_section_title_stylesheet())
        scenario_layout.addWidget(scenario_title)
        self._scenario_note = QLabel('Välj ett källscenario för att beskriva vad som händer i processen.')
        self._scenario_note.setWordWrap(True)
        self._scenario_note.setStyleSheet(lopa_note_stylesheet())
        scenario_layout.addWidget(self._scenario_note)
        self._scenario_text = QPlainTextEdit()
        self._scenario_text.setPlaceholderText('Vad händer i processen?')
        self._scenario_text.setFixedHeight(72)
        scenario_layout.addWidget(self._scenario_text)
        scenario_actions = QHBoxLayout()
        self._goto_hazop_btn = QPushButton('Gå till HAZOP')
        self._goto_hazop_btn.clicked.connect(self._go_to_hazop)
        scenario_actions.addWidget(self._goto_hazop_btn)
        scenario_actions.addStretch()
        self._save_scenario_btn = QPushButton('Spara lokal scenariotext')
        self._save_scenario_btn.clicked.connect(self._save_scenario_text)
        scenario_actions.addWidget(self._save_scenario_btn)
        scenario_layout.addLayout(scenario_actions)

        worst_card = self._card()
        worst_layout = QVBoxLayout(worst_card)
        worst_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                        LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        worst_title = QLabel('Värsta representativa konsekvens')
        worst_title.setStyleSheet(lopa_section_title_stylesheet())
        worst_layout.addWidget(worst_title)
        self._worst_note = QLabel('Visar den aktiva konsekvens som är dimensionerande per kategori.')
        self._worst_note.setWordWrap(True)
        self._worst_note.setStyleSheet(lopa_note_stylesheet())
        worst_layout.addWidget(self._worst_note)
        self._worst_consequences = QTableWidget(0, 4)
        self._worst_consequences.setHorizontalHeaderLabels(['Kategori', 'Nivå', 'Beskrivning', 'TEL (/år)'])
        self._worst_consequences.verticalHeader().setVisible(False)
        self._worst_consequences.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._worst_consequences.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._worst_consequences.horizontalHeader().setStretchLastSection(True)
        self._worst_consequences.setWordWrap(True)
        self._worst_consequences.setStyleSheet(lopa_table_stylesheet())
        self._worst_consequences.setMinimumHeight(132)
        worst_layout.addWidget(self._worst_consequences)

        consequence_card = self._card()
        consequence_layout = QVBoxLayout(consequence_card)
        consequence_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                              LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        consequence_title = QLabel('Konsekvenser från HAZOP')
        consequence_title.setStyleSheet(lopa_section_title_stylesheet())
        consequence_layout.addWidget(consequence_title)
        self._consequence_note = QLabel('Kryssa ur en konsekvens om den inte ska dimensionera just denna LOPA.')
        self._consequence_note.setWordWrap(True)
        self._consequence_note.setStyleSheet(lopa_note_stylesheet())
        consequence_layout.addWidget(self._consequence_note)
        self._consequences = QTableWidget(0, 6)
        self._consequences.setHorizontalHeaderLabels(
            ['Aktiv', 'Kategori', 'Nivå', 'Beskrivning', 'HAZOP-koppling', 'Status'])
        self._consequences.verticalHeader().setVisible(False)
        self._consequences.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._consequences.setWordWrap(True)
        self._consequences.horizontalHeader().setStretchLastSection(True)
        self._consequences.setStyleSheet(lopa_table_stylesheet())
        self._consequences.setMinimumHeight(145)
        self._consequences.itemChanged.connect(self._on_consequence_item_changed)
        consequence_layout.addWidget(self._consequences)
        consequence_actions = QHBoxLayout()
        self._add_consequence_btn = QPushButton('+ Egen LOPA-konsekvens')
        self._add_consequence_btn.clicked.connect(self._add_custom_consequence)
        consequence_actions.addWidget(self._add_consequence_btn)
        consequence_actions.addStretch()
        self._edit_consequence_btn = QPushButton('Ändra lokalt…')
        self._edit_consequence_btn.clicked.connect(self._edit_selected_consequence)
        consequence_actions.addWidget(self._edit_consequence_btn)
        consequence_layout.addLayout(consequence_actions)
        overview_row = QWidget()
        overview_layout = QHBoxLayout(overview_row)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.setSpacing(8)
        overview_layout.addWidget(scenario_card, 1)
        overview_layout.addWidget(worst_card, 1)
        detail_layout.addWidget(overview_row)
        detail_layout.addWidget(consequence_card)

        sensor_card = self._card()
        sensor_layout = QVBoxLayout(sensor_card)
        sensor_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                         LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        sensor_title = QLabel('Givardel')
        sensor_title.setStyleSheet(lopa_section_title_stylesheet())
        sensor_layout.addWidget(sensor_title)
        self._sensor_note = QLabel('Givare kommer från den kopplade HAZOP-barriären. Flera givare kräver att voting bekräftas.')
        self._sensor_note.setWordWrap(True)
        self._sensor_note.setStyleSheet(lopa_note_stylesheet())
        sensor_layout.addWidget(self._sensor_note)
        sensor_controls = QHBoxLayout()
        sensor_controls.addWidget(QLabel('Givargrupp'))
        self._sensor_group = QComboBox()
        self._sensor_group.currentIndexChanged.connect(self._on_sensor_group_changed)
        sensor_controls.addWidget(self._sensor_group)
        self._add_sensor_group_btn = QPushButton('+ Givargrupp')
        self._add_sensor_group_btn.clicked.connect(self._add_sensor_group)
        sensor_controls.addWidget(self._add_sensor_group_btn)
        sensor_controls.addWidget(QLabel('Voting'))
        self._sensor_voting = QComboBox()
        self._sensor_voting.setEditable(True)
        self._sensor_voting.addItems(['1oo1', '1oo2', '2oo2', '1oo3', '2oo3', '2oo4'])
        self._sensor_voting.activated.connect(self._save_sensor_voting)
        self._sensor_voting.lineEdit().editingFinished.connect(self._save_sensor_voting)
        sensor_controls.addWidget(self._sensor_voting)
        sensor_layout.addLayout(sensor_controls)
        self._sensor_members = QTableWidget(0, 4)
        self._sensor_members.setHorizontalHeaderLabels(['Aktiv', 'Objekt', 'Anrop', 'Källa'])
        self._sensor_members.verticalHeader().setVisible(False)
        self._sensor_members.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._sensor_members.horizontalHeader().setStretchLastSection(True)
        self._sensor_members.setStyleSheet(lopa_table_stylesheet())
        self._sensor_members.itemChanged.connect(self._on_sensor_member_item_changed)
        self._sensor_members.setMinimumHeight(115)
        sensor_layout.addWidget(self._sensor_members)
        sensor_add = QHBoxLayout()
        sensor_add.addWidget(QLabel('Lägg till givare'))
        self._sensor_equipment = QComboBox()
        sensor_add.addWidget(self._sensor_equipment, 1)
        self._sensor_trigger = QComboBox()
        self._sensor_trigger.addItems(['', 'H', 'HH', 'L', 'LL', 'Till', 'Från', 'Eget…'])
        self._sensor_trigger.currentTextChanged.connect(self._toggle_sensor_custom_trigger)
        sensor_add.addWidget(self._sensor_trigger)
        self._sensor_custom_trigger = QLineEdit()
        self._sensor_custom_trigger.setPlaceholderText('Eget anrop')
        self._sensor_custom_trigger.hide()
        sensor_add.addWidget(self._sensor_custom_trigger)
        self._add_sensor_btn = QPushButton('+ Lägg till')
        self._add_sensor_btn.clicked.connect(self._add_sensor_member)
        sensor_add.addWidget(self._add_sensor_btn)
        sensor_layout.addLayout(sensor_add)
        final_card = self._card()
        final_layout = QVBoxLayout(final_card)
        final_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                        LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        final_title = QLabel('Manöverdel')
        final_title.setStyleSheet(lopa_section_title_stylesheet())
        final_layout.addWidget(final_title)
        self._final_note = QLabel(
            'Manöverobjekt och voting är LOPA-specifika. Flera aktiva objekt '
            'kräver att voting bekräftas.')
        self._final_note.setWordWrap(True)
        self._final_note.setStyleSheet(lopa_note_stylesheet())
        final_layout.addWidget(self._final_note)
        final_controls = QHBoxLayout()
        final_controls.addWidget(QLabel('Manövergrupp'))
        self._final_group = QComboBox()
        self._final_group.currentIndexChanged.connect(self._on_final_group_changed)
        final_controls.addWidget(self._final_group)
        self._add_final_group_btn = QPushButton('+ Manövergrupp')
        self._add_final_group_btn.clicked.connect(self._add_final_group)
        final_controls.addWidget(self._add_final_group_btn)
        final_controls.addWidget(QLabel('Voting'))
        self._final_voting = QComboBox()
        self._final_voting.setEditable(True)
        self._final_voting.addItems(['1oo1', '1oo2', '2oo2', '1oo3', '2oo3', '2oo4'])
        self._final_voting.activated.connect(self._save_final_voting)
        self._final_voting.lineEdit().editingFinished.connect(self._save_final_voting)
        final_controls.addWidget(self._final_voting)
        final_layout.addLayout(final_controls)
        self._final_members = QTableWidget(0, 4)
        self._final_members.setHorizontalHeaderLabels(['Aktiv', 'Objekt', 'Åtgärd', 'Källa'])
        self._final_members.verticalHeader().setVisible(False)
        self._final_members.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._final_members.horizontalHeader().setStretchLastSection(True)
        self._final_members.setStyleSheet(lopa_table_stylesheet())
        self._final_members.itemChanged.connect(self._on_final_member_item_changed)
        self._final_members.cellDoubleClicked.connect(self._edit_final_member)
        self._final_members.setMinimumHeight(115)
        final_layout.addWidget(self._final_members)
        final_add = QHBoxLayout()
        final_add.addWidget(QLabel('Lägg till objekt'))
        self._final_equipment = QComboBox()
        final_add.addWidget(self._final_equipment, 1)
        self._final_name = QLineEdit()
        self._final_name.setPlaceholderText('eller fritt objektnamn')
        final_add.addWidget(self._final_name, 1)
        self._final_action = QLineEdit()
        self._final_action.setPlaceholderText('Åtgärd')
        final_add.addWidget(self._final_action, 1)
        self._add_final_btn = QPushButton('+ Lägg till')
        self._add_final_btn.clicked.connect(self._add_final_member)
        final_add.addWidget(self._add_final_btn)
        final_layout.addLayout(final_add)

        drive_row = QWidget()
        drive_layout = QHBoxLayout(drive_row)
        drive_layout.setContentsMargins(0, 0, 0, 0)
        drive_layout.setSpacing(8)
        drive_layout.addWidget(sensor_card, 1)
        drive_layout.addWidget(final_card, 1)
        detail_layout.addWidget(drive_row)

        barrier_card = self._card()
        barrier_layout = QVBoxLayout(barrier_card)
        barrier_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                          LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        barrier_title = QLabel('Oberoende barriärer')
        barrier_title.setStyleSheet(lopa_section_title_stylesheet())
        barrier_layout.addWidget(barrier_title)
        self._barrier_note = QLabel('HAZOP-barriärer speglas som underlag. Lokala ändringar påverkar inte HAZOP.')
        self._barrier_note.setWordWrap(True)
        self._barrier_note.setStyleSheet(lopa_note_stylesheet())
        barrier_layout.addWidget(self._barrier_note)
        self._barrier_matrix = QTableWidget(0, 0)
        self._barrier_matrix.verticalHeader().setVisible(False)
        self._barrier_matrix.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._barrier_matrix.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._barrier_matrix.setWordWrap(True)
        self._barrier_matrix.setStyleSheet(lopa_table_stylesheet())
        self._barrier_matrix.setMinimumHeight(118)
        barrier_layout.addWidget(self._barrier_matrix)
        self._barriers = QTableWidget(0, 6)
        self._barriers.setHorizontalHeaderLabels(
            ['Aktiv', 'Typ', 'Beskrivning', 'RRF', 'Gäller kategori', 'Status'])
        self._barriers.verticalHeader().setVisible(False)
        self._barriers.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._barriers.horizontalHeader().setStretchLastSection(True)
        self._barriers.setWordWrap(True)
        self._barriers.setStyleSheet(lopa_table_stylesheet())
        self._barriers.itemChanged.connect(self._on_barrier_item_changed)
        self._barriers.setMinimumHeight(145)
        barrier_layout.addWidget(self._barriers)
        barrier_actions = QHBoxLayout()
        self._add_barrier_btn = QPushButton('+ Manuell barriär')
        self._add_barrier_btn.clicked.connect(self._add_manual_barrier)
        barrier_actions.addWidget(self._add_barrier_btn)
        self._edit_barrier_btn = QPushButton('Ändra lokalt…')
        self._edit_barrier_btn.clicked.connect(self._edit_selected_barrier)
        barrier_actions.addWidget(self._edit_barrier_btn)
        barrier_actions.addStretch()
        barrier_layout.addLayout(barrier_actions)
        barrier_footer = QHBoxLayout()
        barrier_footer.addWidget(QLabel('Kontrollfrekvens'))
        self._control_frequency = QLineEdit()
        self._control_frequency.setPlaceholderText('t.ex. 1 gång/år')
        barrier_footer.addWidget(self._control_frequency, 1)
        barrier_footer.addWidget(QLabel('Förutsättning %'))
        self._assumption_percent = QDoubleSpinBox()
        self._assumption_percent.setRange(0.0, 100000.0)
        self._assumption_percent.setDecimals(4)
        self._assumption_percent.setSuffix(' %')
        barrier_footer.addWidget(self._assumption_percent)
        self._assumption_reason = QLineEdit()
        self._assumption_reason.setPlaceholderText('Motivering')
        barrier_footer.addWidget(self._assumption_reason, 2)
        self._save_source_analysis_btn = QPushButton('Spara LOPA-underlag')
        self._save_source_analysis_btn.clicked.connect(self._save_source_analysis_details)
        barrier_footer.addWidget(self._save_source_analysis_btn)
        barrier_layout.addLayout(barrier_footer)
        self._barrier_summary = QLabel('')
        self._barrier_summary.setWordWrap(True)
        self._barrier_summary.setStyleSheet(lopa_note_stylesheet())
        barrier_layout.addWidget(self._barrier_summary)
        detail_layout.addWidget(barrier_card)

        escalation_card = self._card()
        escalation_layout = QVBoxLayout(escalation_card)
        escalation_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                             LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        escalation_title = QLabel('Eskalering')
        escalation_title.setStyleSheet(lopa_section_title_stylesheet())
        escalation_layout.addWidget(escalation_title)
        self._escalation_note = QLabel('Procentfaktorer är LOPA-specifika och multipliceras med återstående frekvens.')
        self._escalation_note.setWordWrap(True)
        self._escalation_note.setStyleSheet(lopa_note_stylesheet())
        escalation_layout.addWidget(self._escalation_note)
        self._escalation = QTableWidget(0, 0)
        self._escalation.verticalHeader().setVisible(False)
        self._escalation.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._escalation.setWordWrap(True)
        self._escalation.setStyleSheet(lopa_table_stylesheet())
        self._escalation.itemChanged.connect(self._on_escalation_item_changed)
        self._escalation.setMinimumHeight(125)
        escalation_layout.addWidget(self._escalation)
        detail_layout.addWidget(escalation_card)

        calculation_card = self._card()
        calculation_layout = QVBoxLayout(calculation_card)
        calculation_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                              LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        calculation_title = QLabel('Beräkningsöversikt')
        calculation_title.setStyleSheet(lopa_section_title_stylesheet())
        calculation_layout.addWidget(calculation_title)
        self._calculation = QTableWidget(0, 5)
        self._calculation.setHorizontalHeaderLabels(
            ['Källscenario', 'Dimensionerande kategori', 'Behov av RRF', 'SIL', 'Underlag'])
        self._calculation.verticalHeader().setVisible(False)
        self._calculation.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._calculation.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._calculation.horizontalHeader().setStretchLastSection(True)
        self._calculation.setStyleSheet(lopa_table_stylesheet())
        self._calculation.setMinimumHeight(120)
        calculation_layout.addWidget(self._calculation)
        self._dimensioning_summary = QLabel('')
        self._dimensioning_summary.setWordWrap(True)
        self._dimensioning_summary.setStyleSheet(lopa_note_stylesheet())
        calculation_layout.addWidget(self._dimensioning_summary)
        detail_layout.addWidget(calculation_card, 1)

        document_card = self._card()
        document_layout = QVBoxLayout(document_card)
        document_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                           LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        document_title = QLabel('Ytterligare åtgärder och krav')
        document_title.setStyleSheet(lopa_section_title_stylesheet())
        document_layout.addWidget(document_title)
        document_hint = QLabel('Dessa fält hör till den öppna LOPA-revisionen och följer med i revisionshistoriken.')
        document_hint.setWordWrap(True)
        document_hint.setStyleSheet(lopa_note_stylesheet())
        document_layout.addWidget(document_hint)
        document_row = QHBoxLayout()
        action_column = QVBoxLayout()
        action_column.addWidget(QLabel('Ytterligare åtgärder'))
        self._additional_actions = QPlainTextEdit()
        self._additional_actions.setPlaceholderText('Åtgärder som behöver genomföras …')
        self._additional_actions.setFixedHeight(92)
        action_column.addWidget(self._additional_actions)
        document_row.addLayout(action_column, 1)
        requirement_column = QVBoxLayout()
        requirement_column.addWidget(QLabel('Ytterligare säkerhetskrav'))
        self._additional_requirements = QPlainTextEdit()
        self._additional_requirements.setPlaceholderText('Krav för konstruktion, drift eller SRS …')
        self._additional_requirements.setFixedHeight(92)
        requirement_column.addWidget(self._additional_requirements)
        document_row.addLayout(requirement_column, 1)
        document_layout.addLayout(document_row)
        safety_time_row = QHBoxLayout()
        safety_time_row.addWidget(QLabel('Processäkerhetstid'))
        self._process_safety_time = QLineEdit()
        self._process_safety_time.setPlaceholderText('Ej angiven (s)')
        safety_time_row.addWidget(self._process_safety_time)
        safety_time_row.addWidget(QLabel('sekunder'))
        safety_time_row.addStretch(1)
        self._save_document_btn = QPushButton('Spara dokumentuppgifter')
        self._save_document_btn.clicked.connect(self._save_document_details)
        safety_time_row.addWidget(self._save_document_btn)
        document_layout.addLayout(safety_time_row)
        detail_layout.addWidget(document_card)

        comments_card = self._card()
        comments_layout = QVBoxLayout(comments_card)
        comments_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                           LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        comments_title = QLabel('Kommentarer')
        comments_title.setStyleSheet(lopa_section_title_stylesheet())
        comments_layout.addWidget(comments_title)
        self._comments = QTableWidget(0, 3)
        self._comments.setHorizontalHeaderLabels(['Datum', 'Namn', 'Kommentar'])
        self._comments.verticalHeader().setVisible(False)
        self._comments.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._comments.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._comments.horizontalHeader().setStretchLastSection(True)
        self._comments.setWordWrap(True)
        self._comments.setStyleSheet(lopa_table_stylesheet())
        self._comments.setMinimumHeight(105)
        comments_layout.addWidget(self._comments)
        comment_add = QHBoxLayout()
        self._comment_author = QLineEdit()
        self._comment_author.setPlaceholderText('Namn')
        comment_add.addWidget(self._comment_author)
        self._comment_text = QLineEdit()
        self._comment_text.setPlaceholderText('Lägg till kommentar')
        self._comment_text.returnPressed.connect(self._add_comment)
        comment_add.addWidget(self._comment_text, 1)
        self._add_comment_btn = QPushButton('+ Kommentar')
        self._add_comment_btn.clicked.connect(self._add_comment)
        comment_add.addWidget(self._add_comment_btn)
        comments_layout.addLayout(comment_add)
        detail_layout.addWidget(comments_card)
        detail_layout.addStretch(1)
        detail.setWidget(detail_body)
        splitter.addWidget(detail)
        splitter.setSizes([270, 850])

    def _selected_lopa_id(self):
        item = self._list.currentItem()
        return item.data(self._ROLE_LOPA_ID) if item else None

    def refresh(self, *_args):
        selected = self._lopa_id or self._selected_lopa_id()
        self._loading = True
        self._list.clear()
        for record in self.db.lopa_records(include_archived=self._show_archived.isChecked()):
            title = f"{record['display_number']}  {record['sif_name'] or 'Namnlös SIF'}"
            if record['archived']:
                title += '  (arkiverad)'
            item = QListWidgetItem(title)
            item.setData(self._ROLE_LOPA_ID, record['id'])
            self._list.addItem(item)
            if record['id'] == selected:
                self._list.setCurrentItem(item)
        self._loading = False
        if self._list.currentItem() is None and self._list.count():
            self._list.setCurrentRow(0)
        if self._list.currentItem() is not None:
            # Selection changes while the list is being rebuilt are ignored
            # above; explicitly load the retained/new current row afterwards.
            self._on_lopa_selected(self._list.currentItem(), None)
        elif not self._list.count():
            self._set_empty_detail()

    def _set_empty_detail(self):
        self._lopa_id = None
        self._revision_id = None
        self._source_id = None
        self._sensor_group_id = None
        self._final_group_id = None
        self._record_title.setText('Välj eller skapa en LOPA')
        self._status.hide()
        self._revision.clear()
        for widget in (self._number, self._sif_number, self._sif_name, self._sis_name,
                       self._document_date, self._performed_by, self._approved_by):
            widget.clear()
            widget.setEnabled(False)
        self._new_revision_btn.setEnabled(False)
        self._lock_btn.setEnabled(False)
        self._archive_btn.setEnabled(False)
        self._choose_performed_btn.setEnabled(False)
        self._sources.setRowCount(0)
        self._sync_sources_btn.setEnabled(False)
        self._source_sync_note.clear()
        self._consequences.setRowCount(0)
        self._worst_consequences.setRowCount(0)
        self._sensor_group.clear()
        self._sensor_members.setRowCount(0)
        self._sensor_equipment.clear()
        self._final_group.clear()
        self._final_members.setRowCount(0)
        self._final_equipment.clear()
        self._barriers.setRowCount(0)
        self._barrier_matrix.setRowCount(0)
        self._barrier_matrix.setColumnCount(0)
        self._escalation.setRowCount(0)
        self._calculation.setRowCount(0)
        self._dimensioning_summary.clear()
        self._additional_actions.clear()
        self._additional_actions.setEnabled(False)
        self._additional_requirements.clear()
        self._additional_requirements.setEnabled(False)
        self._process_safety_time.clear()
        self._process_safety_time.setEnabled(False)
        self._save_document_btn.setEnabled(False)
        self._comments.setRowCount(0)
        self._comment_author.clear()
        self._comment_author.setEnabled(False)
        self._comment_text.clear()
        self._comment_text.setEnabled(False)
        self._add_comment_btn.setEnabled(False)
        self._scenario_text.clear()
        self._scenario_text.setEnabled(False)
        self._save_scenario_btn.setEnabled(False)
        self._goto_hazop_btn.setEnabled(False)
        self._edit_consequence_btn.setEnabled(False)
        self._sensor_voting.setEnabled(False)
        self._add_sensor_group_btn.setEnabled(False)
        self._add_sensor_btn.setEnabled(False)
        self._sensor_trigger.setEnabled(False)
        self._sensor_custom_trigger.setEnabled(False)
        self._final_voting.setEnabled(False)
        self._add_final_group_btn.setEnabled(False)
        self._add_final_btn.setEnabled(False)
        self._final_name.setEnabled(False)
        self._final_action.setEnabled(False)
        self._add_barrier_btn.setEnabled(False)
        self._edit_barrier_btn.setEnabled(False)
        self._control_frequency.clear()
        self._control_frequency.setEnabled(False)
        self._assumption_percent.setValue(0)
        self._assumption_percent.setEnabled(False)
        self._assumption_reason.clear()
        self._assumption_reason.setEnabled(False)
        self._save_source_analysis_btn.setEnabled(False)
        self._barrier_summary.clear()
        self._sync_note.setText('Skapa en tom LOPA eller koppla en HAZOP-barriär när den funktionen används.')

    def _on_lopa_selected(self, current, _previous):
        if self._loading:
            return
        lopa_id = current.data(self._ROLE_LOPA_ID) if current else None
        if not lopa_id:
            self._set_empty_detail()
            return
        self._lopa_id = lopa_id
        self._load_record()

    def _load_record(self, revision_id=None):
        record = self.db.get_lopa_record(self._lopa_id)
        if not record:
            self._set_empty_detail()
            return
        self._loading = True
        self._record_title.setText(f"LOPA {record['display_number']}")
        self._number.setText(record['display_number'])
        self._sif_number.setText(record.get('sif_number') or '')
        self._sif_name.setText(record['sif_name'])
        self._sis_name.setText(record['sis_name'])
        for widget in (self._number, self._sif_number, self._sif_name, self._sis_name):
            widget.setEnabled(not bool(record['archived']))
        self._revision.clear()
        revisions = self.db.lopa_revisions(self._lopa_id)
        target = revision_id or self._revision_id
        selected_index = 0
        for index, revision in enumerate(revisions):
            self._revision.addItem(f"{revision['label']} – {revision['status']}", revision['id'])
            if revision['id'] == target:
                selected_index = index
        self._revision.setCurrentIndex(selected_index)
        self._loading = False
        self._revision_id = self._revision.currentData()
        self._refresh_revision_detail()

    def _on_revision_changed(self, _index):
        if self._loading:
            return
        self._revision_id = self._revision.currentData()
        self._refresh_revision_detail()

    def _refresh_revision_detail(self):
        revision = self.db.get_lopa_revision(self._revision_id) if self._revision_id else None
        if not revision:
            return
        locked = revision['status'] in ('Låst', 'Godkänd', 'Arkiverad')
        record = self.db.get_lopa_record(self._lopa_id) or {}
        self._status.setText(revision['status'])
        self._status.setStyleSheet(lopa_status_stylesheet(revision['status']))
        self._status.show()
        self._new_revision_btn.setEnabled(not bool(record.get('archived')))
        self._lock_btn.setEnabled(not bool(record.get('archived')))
        self._archive_btn.setEnabled(not bool(record.get('archived')))
        self._lock_btn.setText('Lås upp revision' if locked else 'Lås revision')
        for widget in (self._document_date, self._performed_by, self._approved_by):
            widget.setEnabled(not locked and not bool(record.get('archived')))
        self._choose_performed_btn.setEnabled(not locked and not bool(record.get('archived')))
        self._document_date.setText(revision.get('document_date') or '')
        self._performed_by.setText(revision.get('performed_by_text') or '')
        self._approved_by.setText(revision.get('approved_by_text') or '')
        self._additional_actions.setPlainText(revision.get('additional_actions') or '')
        self._additional_requirements.setPlainText(revision.get('additional_requirements') or '')
        safety_time = revision.get('process_safety_time')
        self._process_safety_time.setText('' if safety_time is None else f'{safety_time:.6g}')
        for widget in (self._additional_actions, self._additional_requirements,
                       self._process_safety_time, self._comment_author, self._comment_text):
            widget.setEnabled(not locked and not bool(record.get('archived')))
        self._save_document_btn.setEnabled(not locked and not bool(record.get('archived')))
        self._add_comment_btn.setEnabled(not locked and not bool(record.get('archived')))
        self._populate_sources()
        self._populate_sensor_groups()
        self._populate_final_groups()
        self._populate_calculation()
        self._populate_comments()

    @staticmethod
    def _cell(text, alignment=None):
        item = QTableWidgetItem(str(text or '—'))
        if alignment is not None:
            item.setTextAlignment(int(alignment))
        return item

    @staticmethod
    def _check_cell(checked, *, enabled=True, entity_id=None):
        item = QTableWidgetItem()
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if enabled:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        item.setFlags(flags)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        if entity_id is not None:
            item.setData(LopaPanel._ROLE_ENTITY_ID, entity_id)
        return item

    @staticmethod
    def _readonly_cell(text, entity_id=None):
        item = QTableWidgetItem(str(text or '—'))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if entity_id is not None:
            item.setData(LopaPanel._ROLE_ENTITY_ID, entity_id)
        return item

    def _revision_is_editable(self):
        revision = self.db.get_lopa_revision(self._revision_id) if self._revision_id else None
        record = self.db.get_lopa_record(self._lopa_id) if self._lopa_id else None
        return bool(revision and record and not record.get('archived') and
                    revision.get('status') not in ('Låst', 'Godkänd', 'Arkiverad'))

    def _confirm_lopa_only(self, text):
        return QMessageBox.question(
            self, 'Ändra endast i LOPA?', text + '\n\nHAZOP ändras inte.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _selected_entity_id(self, table):
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        return item.data(self._ROLE_ENTITY_ID) if item is not None else None

    def _populate_sources(self):
        rows = self.db.lopa_sources(self._revision_id) if self._revision_id else []
        selected = self._source_id if any(row['id'] == self._source_id for row in rows) else None
        if selected is None and rows:
            selected = rows[0]['id']
        old_loading = self._loading
        self._loading = True
        self._sources.setRowCount(len(rows))
        selected_row = -1
        for row_index, source in enumerate(rows):
            status = 'Följer HAZOP' if source['follows_hazop'] else 'Frikopplad från HAZOP'
            sync = self.db.lopa_source_sync_state(source['id'])
            if sync['state'] == 'missing' or source['source_missing']:
                status = 'Källa saknas i HAZOP'
            elif sync['state'] == 'changed':
                status = 'HAZOP ändrad – granska'
            object_trigger = ' '.join(part for part in (
                source.get('equipment_tag') or '', source.get('trigger_code') or '',
                source.get('trigger_custom') or '') if part).strip() or '—'
            active_item = self._check_cell(
                bool(source['active']), enabled=self._revision_is_editable(), entity_id=source['id'])
            active_item.setData(self._ROLE_SOURCE_ID, source['id'])
            self._sources.setItem(row_index, 0, active_item)
            self._sources.setItem(row_index, 1, self._readonly_cell(object_trigger))
            self._sources.setItem(row_index, 2, self._readonly_cell(source['cause_text']))
            frequency = (f"{source['base_frequency']:.3g} /år"
                         if source['base_frequency'] is not None else 'Numeriskt värde saknas')
            self._sources.setItem(row_index, 3, self._readonly_cell(frequency))
            self._sources.setItem(row_index, 4, self._readonly_cell(
                f"Orsak {source['hazop_cause_id']}" if source['hazop_cause_id'] else 'Ingen HAZOP-källa'))
            self._sources.setItem(row_index, 5, self._readonly_cell(status))
            if source['id'] == selected:
                selected_row = row_index
        if selected_row >= 0:
            self._sources.selectRow(selected_row)
        self._sources.resizeRowsToContents()
        self._loading = old_loading
        self._source_id = selected
        self._sync_sources_btn.setEnabled(bool(rows))
        changed = sum(1 for source in rows
                      if self.db.lopa_source_sync_state(source['id'])['state'] == 'changed')
        self._source_sync_note.setText(
            'HAZOP-källor är aktuella.' if not changed else
            f'{changed} HAZOP-koppling(ar) har ändrats – LOPA skrivs inte över automatiskt.')
        self._sync_note.setText(
            'Aktiva rader följer HAZOP tills de uttryckligen kopplas loss. '
            'Låsta revisioner behåller sin egen riskmatris och sitt underlag.')
        self._load_source_detail()

    def _check_hazop_links(self):
        """Re-evaluate sync state without changing any revision snapshot."""
        self._populate_sources()

    def _on_source_selection_changed(self):
        if self._loading:
            return
        self._source_id = self._selected_entity_id(self._sources)
        self._load_source_detail()

    def _on_source_item_changed(self, item):
        if self._loading or item.column() != 0:
            return
        source_id = item.data(self._ROLE_ENTITY_ID)
        if not source_id:
            return
        active = item.checkState() == Qt.CheckState.Checked
        if not self._confirm_lopa_only(
                'Ska källscenariot inkluderas i denna LOPA-revision?'):
            self._populate_sources()
            return
        try:
            self.db.set_lopa_source_active(source_id, active)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra källscenario', str(exc))
        self._populate_sources()
        self._populate_calculation()
        self.changed.emit()

    def _load_source_detail(self):
        source = next((row for row in self.db.lopa_sources(self._revision_id)
                       if row['id'] == self._source_id), None) if self._source_id else None
        editable = self._revision_is_editable()
        old_loading = self._loading
        self._loading = True
        if not source:
            self._scenario_text.clear()
            self._scenario_text.setEnabled(False)
            self._save_scenario_btn.setEnabled(False)
            self._goto_hazop_btn.setEnabled(False)
            self._scenario_note.setText('Välj ett källscenario för att beskriva vad som händer i processen.')
            self._consequences.setRowCount(0)
            self._worst_consequences.setRowCount(0)
            self._barriers.setRowCount(0)
            self._barrier_matrix.setRowCount(0)
            self._barrier_matrix.setColumnCount(0)
            self._escalation.setRowCount(0)
            self._edit_consequence_btn.setEnabled(False)
            self._add_consequence_btn.setEnabled(False)
            self._add_barrier_btn.setEnabled(False)
            self._edit_barrier_btn.setEnabled(False)
            self._control_frequency.clear()
            self._control_frequency.setEnabled(False)
            self._assumption_percent.setValue(0)
            self._assumption_percent.setEnabled(False)
            self._assumption_reason.clear()
            self._assumption_reason.setEnabled(False)
            self._save_source_analysis_btn.setEnabled(False)
            self._barrier_summary.clear()
            self._loading = old_loading
            return
        self._scenario_text.setPlainText(source.get('scenario_text') or '')
        self._scenario_text.setEnabled(editable)
        self._save_scenario_btn.setEnabled(editable)
        self._goto_hazop_btn.setEnabled(bool(source.get('hazop_cause_id')))
        self._control_frequency.setText(source.get('control_frequency') or '')
        self._control_frequency.setEnabled(editable)
        self._assumption_percent.setValue(float(source.get('assumption_percent') or 0.0))
        self._assumption_percent.setEnabled(editable)
        self._assumption_reason.setText(source.get('assumption_reason') or '')
        self._assumption_reason.setEnabled(editable)
        self._save_source_analysis_btn.setEnabled(editable)
        sync = self.db.lopa_source_sync_state(source['id'])
        sync_text = {
            'current': 'Kopplingen följer aktuell HAZOP-data.',
            'changed': 'HAZOP har ändrats; granska innan LOPA-uppgifterna uppdateras.',
            'detached': 'Raden är uttryckligen frikopplad från HAZOP lokalt.',
            'missing': 'HAZOP-källan finns inte längre; LOPA-underlaget är kvar som historik.',
        }.get(sync['state'], '')
        self._scenario_note.setText(
            f"Källscenario från orsak {source.get('hazop_cause_id') or '—'}. "
            f"{sync_text} Sparad text blir en uttrycklig lokal LOPA-avvikelse.")
        self._loading = old_loading
        self._populate_consequences()
        self._populate_barriers()
        self._populate_escalation()

    def _save_scenario_text(self):
        if not self._source_id:
            return
        source = next((row for row in self.db.lopa_sources(self._revision_id)
                       if row['id'] == self._source_id), None)
        if not source:
            return
        if source.get('follows_hazop') and not self._confirm_lopa_only(
                'Ska scenariotexten kopplas loss från HAZOP och ändras bara här?'):
            self._load_source_detail()
            return
        try:
            self.db.set_lopa_source_scenario_text(self._source_id, self._scenario_text.toPlainText())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara scenariotext', str(exc))
            return
        self._populate_sources()
        self.changed.emit()

    def _go_to_hazop(self):
        source = next((row for row in self.db.lopa_sources(self._revision_id)
                       if row['id'] == self._source_id), None) if self._source_id else None
        cause_id = source.get('hazop_cause_id') if source else None
        if not cause_id:
            QMessageBox.information(self, 'HAZOP-källa saknas',
                                    'Det valda scenariot har ingen aktiv HAZOP-orsak att gå till.')
            return
        self.hazop_navigation_requested.emit(int(cause_id))

    def _save_source_analysis_details(self):
        """Save LOPA-only frequency assumptions for the selected source."""
        if not self._source_id:
            return
        try:
            self.db.update_lopa_source_analysis_details(
                self._source_id,
                control_frequency=self._control_frequency.text(),
                assumption_percent=self._assumption_percent.value(),
                assumption_reason=self._assumption_reason.text())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara LOPA-underlag', str(exc))
            return
        self._populate_sources()
        self._populate_calculation()
        self.changed.emit()

    def _populate_consequences(self):
        rows = self.db.lopa_source_consequences(self._source_id) if self._source_id else []
        old_loading = self._loading
        self._loading = True
        self._consequences.setRowCount(len(rows))
        for row_index, consequence in enumerate(rows):
            status = 'Följer HAZOP' if consequence['follows_hazop'] else 'Frikopplad från HAZOP'
            if consequence['source_missing']:
                status = 'Källa saknas i HAZOP'
            self._consequences.setItem(
                row_index, 0, self._check_cell(bool(consequence['active']),
                                                enabled=self._revision_is_editable(),
                                                entity_id=consequence['id']))
            self._consequences.setItem(row_index, 1, self._readonly_cell(consequence['category_name']))
            self._consequences.setItem(row_index, 2, self._readonly_cell(consequence['severity']))
            self._consequences.setItem(row_index, 3, self._readonly_cell(consequence['description']))
            self._consequences.setItem(
                row_index, 4, self._readonly_cell(
                    f"Konsekvens {consequence['hazop_consequence_id']}"
                    if consequence['hazop_consequence_id'] else 'Lokal LOPA-rad'))
            self._consequences.setItem(row_index, 5, self._readonly_cell(status))
        self._consequences.resizeRowsToContents()
        self._edit_consequence_btn.setEnabled(bool(rows) and self._revision_is_editable())
        self._add_consequence_btn.setEnabled(bool(self._source_id) and self._revision_is_editable())
        self._loading = old_loading
        self._populate_worst_consequences()

    def _populate_worst_consequences(self):
        if not self._source_id:
            self._worst_consequences.setRowCount(0)
            self._worst_note.setText('Välj ett källscenario för att se representativa konsekvenser.')
            return
        result = self.db.lopa_source_calculation(self._source_id)
        candidates = {}
        for row in result['categories']:
            if not row['active']:
                continue
            previous = candidates.get(row['category_key'])
            # Required RRF is the primary LOPA criterion.  Severity makes the
            # tie deterministic when TEL or frequency is still incomplete.
            row_key = (row['required_rrf'] if row['required_rrf'] is not None else -1,
                       row['severity'])
            previous_key = ((previous['required_rrf'] if previous and
                             previous['required_rrf'] is not None else -1),
                            previous['severity'] if previous else -1)
            if previous is None or row_key > previous_key:
                candidates[row['category_key']] = row
        old_loading = self._loading
        self._loading = True
        rows = list(candidates.values())
        self._worst_consequences.setRowCount(len(rows))
        for index, row in enumerate(rows):
            description = next((item['description'] for item in self.db.lopa_source_consequences(self._source_id)
                                if item['category_key'] == row['category_key'] and
                                item['severity'] == row['severity']), '')
            self._worst_consequences.setItem(index, 0, self._readonly_cell(row['category_name']))
            self._worst_consequences.setItem(index, 1, self._readonly_cell(row['severity']))
            self._worst_consequences.setItem(index, 2, self._readonly_cell(description))
            self._worst_consequences.setItem(
                index, 3, self._readonly_cell('—' if row['tel'] is None else f"{row['tel']:.6g}"))
        self._worst_consequences.resizeRowsToContents()
        self._worst_note.setText(
            'Aktiva LOPA-rader visas. Saknad TEL markeras med — och ger ingen beräknad SIL.')
        self._loading = old_loading

    def _on_consequence_item_changed(self, item):
        if self._loading or item.column() != 0:
            return
        consequence_id = item.data(self._ROLE_ENTITY_ID)
        if not consequence_id:
            return
        active = item.checkState() == Qt.CheckState.Checked
        if not self._confirm_lopa_only(
                'Ska konsekvensen inkluderas i just denna LOPA-beräkning?'):
            self._populate_consequences()
            return
        try:
            self.db.set_lopa_consequence_active(consequence_id, active)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra konsekvens', str(exc))
        self._populate_consequences()
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _edit_selected_consequence(self):
        consequence_id = self._selected_entity_id(self._consequences)
        if not consequence_id:
            QMessageBox.information(self, 'Välj konsekvens', 'Välj först en konsekvensrad att ändra.')
            return
        consequence = next((row for row in self.db.lopa_source_consequences(self._source_id)
                            if row['id'] == consequence_id), None)
        if not consequence:
            return
        dialog = LopaConsequenceDialog(consequence, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if consequence.get('follows_hazop') and not self._confirm_lopa_only(
                'Ska konsekvensens text eller nivå ändras lokalt i LOPA?'):
            return
        try:
            self.db.update_lopa_consequence(
                consequence_id, description=dialog.description(), severity=dialog.severity())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra konsekvens', str(exc))
            return
        self._populate_consequences()
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _add_custom_consequence(self):
        if not self._source_id:
            return
        options = self._category_options()
        if not options:
            QMessageBox.warning(self, 'Kategori saknas',
                                'Riskmatrisen saknar konsekvenskategorier för LOPA.')
            return
        dialog = LopaNewConsequenceDialog(options, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            key, name = dialog.category()
            self.db.add_lopa_custom_consequence(
                self._source_id, key, name, dialog.severity(), dialog.description())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till konsekvens', str(exc))
            return
        self._populate_consequences()
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _populate_sensor_groups(self):
        groups = self.db.lopa_sensor_groups(self._revision_id) if self._revision_id else []
        selected = (self._sensor_group_id if any(group['id'] == self._sensor_group_id
                                                  for group in groups) else None)
        if selected is None and groups:
            selected = groups[0]['id']
        old_loading = self._loading
        self._loading = True
        self._sensor_group.clear()
        for index, group in enumerate(groups, start=1):
            title = f'Givardel {index} – {group["voting"]}'
            if group['needs_voting_review']:
                title += ' (bekräfta voting)'
            self._sensor_group.addItem(title, group['id'])
        if selected is not None:
            self._sensor_group.setCurrentIndex(self._sensor_group.findData(selected))
        self._sensor_group_id = selected
        self._sensor_equipment.clear()
        self._sensor_equipment.addItem('Välj objekt…', None)
        for equipment in self.db.equipment_items():
            text = equipment['tag'] or f"Objekt {equipment['id']}"
            if equipment['equipment_type']:
                text += f" – {equipment['equipment_type']}"
            self._sensor_equipment.addItem(text, equipment['id'])
        editable = self._revision_is_editable()
        self._sensor_group.setEnabled(bool(groups))
        self._sensor_voting.setEnabled(bool(groups) and editable)
        self._add_sensor_group_btn.setEnabled(editable)
        self._add_sensor_btn.setEnabled(bool(groups) and editable)
        self._sensor_equipment.setEnabled(bool(groups) and editable)
        self._sensor_trigger.setEnabled(bool(groups) and editable)
        self._sensor_custom_trigger.setEnabled(bool(groups) and editable)
        self._loading = old_loading
        self._populate_sensor_members()

    def _on_sensor_group_changed(self, _index):
        if self._loading:
            return
        self._sensor_group_id = self._sensor_group.currentData()
        self._populate_sensor_members()

    def _populate_sensor_members(self):
        group = next((item for item in self.db.lopa_sensor_groups(self._revision_id)
                      if item['id'] == self._sensor_group_id), None) if self._sensor_group_id else None
        members = self.db.lopa_sensor_members(group['id']) if group else []
        old_loading = self._loading
        self._loading = True
        self._sensor_members.setRowCount(len(members))
        for row_index, member in enumerate(members):
            self._sensor_members.setItem(
                row_index, 0, self._check_cell(bool(member['active']),
                                                enabled=self._revision_is_editable(),
                                                entity_id=member['id']))
            self._sensor_members.setItem(row_index, 1, self._readonly_cell(member.get('tag') or '—'))
            trigger = ' '.join(part for part in (
                member.get('trigger_code') or '', member.get('trigger_custom') or '') if part) or '—'
            self._sensor_members.setItem(row_index, 2, self._readonly_cell(trigger))
            origin = (f"HAZOP-barriär {member['origin_safeguard_id']}"
                      if member.get('origin_safeguard_id') else 'Lokal LOPA-givare')
            self._sensor_members.setItem(row_index, 3, self._readonly_cell(origin))
        self._sensor_members.resizeRowsToContents()
        if group:
            self._sensor_voting.setCurrentText(group['voting'])
            self._sensor_note.setText(
                'Flera aktiva givare kräver bekräftad voting.' if group['needs_voting_review'] else
                'Givardel från HAZOP. Voting och givare sparas på denna LOPA-revision.')
        else:
            self._sensor_voting.setCurrentText('1oo1')
            self._sensor_note.setText('Lägg till en givargrupp när LOPA:n behöver en definierad givardel.')
        self._loading = old_loading

    def _save_sensor_voting(self, *_args):
        if self._loading or not self._sensor_group_id:
            return
        try:
            self.db.set_lopa_sensor_group_voting(self._sensor_group_id, self._sensor_voting.currentText())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara voting', str(exc))
        self._populate_sensor_groups()
        self.changed.emit()

    def _add_sensor_group(self):
        if not self._revision_id:
            return
        voting, ok = QInputDialog.getText(
            self, 'Ny givargrupp', 'Voting (exempelvis 1oo1 eller 2oo3):', text='1oo1')
        if not ok:
            return
        try:
            self._sensor_group_id = self.db.add_lopa_sensor_group(self._revision_id, voting)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till givargrupp', str(exc))
            return
        self._populate_sensor_groups()
        self.changed.emit()

    def _toggle_sensor_custom_trigger(self, value):
        custom = value == 'Eget…'
        self._sensor_custom_trigger.setVisible(custom)
        if not custom:
            self._sensor_custom_trigger.clear()

    def _add_sensor_member(self):
        equipment_id = self._sensor_equipment.currentData()
        if equipment_id is None:
            QMessageBox.information(self, 'Välj objekt', 'Välj objektet som ska ingå i givardelen.')
            return
        trigger = self._sensor_trigger.currentText()
        custom = self._sensor_custom_trigger.text().strip() if trigger == 'Eget…' else ''
        if trigger == 'Eget…':
            trigger = ''
            if not custom:
                QMessageBox.information(self, 'Anrop saknas', 'Ange ett eget anrop för givaren.')
                return
        try:
            self.db.add_lopa_sensor_member(
                self._revision_id, equipment_id, trigger, custom, group_id=self._sensor_group_id)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till givare', str(exc))
            return
        self._populate_sensor_groups()
        self.changed.emit()

    def _on_sensor_member_item_changed(self, item):
        if self._loading or item.column() != 0:
            return
        member_id = item.data(self._ROLE_ENTITY_ID)
        if not member_id:
            return
        active = item.checkState() == Qt.CheckState.Checked
        if not self._confirm_lopa_only('Ska givaren ingå i den här LOPA-revisionens votinggrupp?'):
            self._populate_sensor_members()
            return
        try:
            self.db.set_lopa_sensor_member_active(member_id, active)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra givare', str(exc))
        self._populate_sensor_groups()
        self.changed.emit()

    # ── Manöverdel ───────────────────────────────────────────────────────
    # The final-element path intentionally mirrors Givardel.  It is stored
    # separately because a valve/actuator is not a sensor and must not be
    # inferred from an HAZOP safeguard's free text.
    def _populate_final_groups(self):
        groups = self.db.lopa_final_groups(self._revision_id) if self._revision_id else []
        selected = (self._final_group_id if any(group['id'] == self._final_group_id
                                                for group in groups) else None)
        if selected is None and groups:
            selected = groups[0]['id']
        old_loading = self._loading
        self._loading = True
        self._final_group.clear()
        for index, group in enumerate(groups, start=1):
            title = f'Manöverdel {index} – {group["voting"]}'
            if group['needs_voting_review']:
                title += ' (bekräfta voting)'
            self._final_group.addItem(title, group['id'])
        if selected is not None:
            self._final_group.setCurrentIndex(self._final_group.findData(selected))
        self._final_group_id = selected
        self._final_equipment.clear()
        self._final_equipment.addItem('Välj objekt…', None)
        for equipment in self.db.equipment_items():
            label = equipment['tag'] or f"Objekt {equipment['id']}"
            if equipment['equipment_type']:
                label += f" – {equipment['equipment_type']}"
            self._final_equipment.addItem(label, equipment['id'])
        editable = self._revision_is_editable()
        self._final_group.setEnabled(bool(groups))
        self._final_voting.setEnabled(bool(groups) and editable)
        self._add_final_group_btn.setEnabled(editable)
        self._add_final_btn.setEnabled(bool(groups) and editable)
        self._final_equipment.setEnabled(bool(groups) and editable)
        self._final_name.setEnabled(bool(groups) and editable)
        self._final_action.setEnabled(bool(groups) and editable)
        self._loading = old_loading
        self._populate_final_members()

    def _on_final_group_changed(self, _index):
        if self._loading:
            return
        self._final_group_id = self._final_group.currentData()
        self._populate_final_members()

    def _populate_final_members(self):
        group = next((item for item in self.db.lopa_final_groups(self._revision_id)
                      if item['id'] == self._final_group_id), None) if self._final_group_id else None
        members = self.db.lopa_final_members(group['id']) if group else []
        old_loading = self._loading
        self._loading = True
        self._final_members.setRowCount(len(members))
        for row_index, member in enumerate(members):
            self._final_members.setItem(
                row_index, 0, self._check_cell(bool(member['active']),
                                                enabled=self._revision_is_editable(),
                                                entity_id=member['id']))
            self._final_members.setItem(row_index, 1, self._readonly_cell(member.get('tag') or '—'))
            self._final_members.setItem(row_index, 2, self._readonly_cell(member.get('action_text') or '—'))
            origin = 'Objektdatabas' if member.get('equipment_id') else 'Lokalt LOPA-objekt'
            self._final_members.setItem(row_index, 3, self._readonly_cell(origin))
        self._final_members.resizeRowsToContents()
        if group:
            self._final_voting.setCurrentText(group['voting'])
            self._final_note.setText(
                'Flera aktiva manöverobjekt kräver bekräftad voting.' if group['needs_voting_review'] else
                'Manöverdel sparas på denna LOPA-revision. Dubbelklicka på en rad för att ändra objekt eller åtgärd.')
        else:
            self._final_voting.setCurrentText('1oo1')
            self._final_note.setText('Lägg till en manövergrupp när LOPA:n behöver en definierad manöverdel.')
        self._loading = old_loading

    def _save_final_voting(self, *_args):
        if self._loading or not self._final_group_id:
            return
        try:
            self.db.set_lopa_final_group_voting(self._final_group_id, self._final_voting.currentText())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara voting', str(exc))
        self._populate_final_groups()
        self.changed.emit()

    def _add_final_group(self):
        if not self._revision_id:
            return
        voting, ok = QInputDialog.getText(
            self, 'Ny manövergrupp', 'Voting (exempelvis 1oo1 eller 2oo3):', text='1oo1')
        if not ok:
            return
        try:
            self._final_group_id = self.db.add_lopa_final_group(self._revision_id, voting)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till manövergrupp', str(exc))
            return
        self._populate_final_groups()
        self.changed.emit()

    def _add_final_member(self):
        equipment_id = self._final_equipment.currentData()
        name = self._final_name.text().strip()
        if equipment_id is None and not name:
            QMessageBox.information(self, 'Objekt saknas',
                                    'Välj ett objekt eller ange ett fritt objektnamn för manöverdelen.')
            return
        try:
            self.db.add_lopa_final_member(
                self._revision_id, equipment_id=equipment_id, name=name,
                action_text=self._final_action.text().strip(), group_id=self._final_group_id)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till manöverobjekt', str(exc))
            return
        self._final_name.clear()
        self._final_action.clear()
        self._populate_final_groups()
        self.changed.emit()

    def _on_final_member_item_changed(self, item):
        if self._loading or item.column() != 0:
            return
        member_id = item.data(self._ROLE_ENTITY_ID)
        if not member_id:
            return
        active = item.checkState() == Qt.CheckState.Checked
        if not self._confirm_lopa_only('Ska manöverobjektet ingå i den här LOPA-revisionens votinggrupp?'):
            self._populate_final_members()
            return
        try:
            self.db.update_lopa_final_member(member_id, active=active)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra manöverobjekt', str(exc))
        self._populate_final_groups()
        self.changed.emit()

    def _edit_final_member(self, row, _column):
        if not self._revision_is_editable():
            return
        member_id = self._selected_entity_id(self._final_members)
        if not member_id:
            return
        member = next((item for item in self.db.lopa_final_members(self._final_group_id)
                       if item['id'] == member_id), None)
        if not member:
            return
        name, ok = QInputDialog.getText(
            self, 'Ändra manöverobjekt', 'Objekt:', text=member.get('tag') or '')
        if not ok:
            return
        action, ok = QInputDialog.getText(
            self, 'Ändra manöveråtgärd', 'Åtgärd:', text=member.get('action_text') or '')
        if not ok:
            return
        try:
            self.db.update_lopa_final_member(member_id, name=name, action_text=action)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra manöverobjekt', str(exc))
            return
        self._populate_final_members()
        self.changed.emit()

    def _category_options(self):
        matrix = self.db.lopa_revision_matrix(self._revision_id) if self._revision_id else {}
        config = matrix.get('lopa') or {}
        settings = config.get('category_settings') or {}
        return [(key, value.get('name') or key)
                for key, value in settings.items() if not value.get('orphaned')]

    def _populate_barriers(self):
        rows = self.db.lopa_barriers(self._revision_id, self._source_id) if self._source_id else []
        self._populate_barrier_matrix()
        names = dict(self._category_options())
        old_loading = self._loading
        self._loading = True
        self._barriers.setRowCount(len(rows))
        for row_index, barrier in enumerate(rows):
            if barrier['manual']:
                status = 'Manuell LOPA-barriär'
            else:
                status = 'Följer HAZOP' if barrier['follows_hazop'] else 'Frikopplad från HAZOP'
            if barrier['source_missing']:
                status = 'Källa saknas i HAZOP'
            if barrier['applies_all_categories']:
                applies = 'Alla'
            else:
                keys = [row['category_key'] for row in self.db.lopa_barrier_categories(barrier['id'])
                        if row['active']]
                applies = ', '.join(names.get(key, key) for key in keys) or 'Ingen'
            self._barriers.setItem(
                row_index, 0, self._check_cell(bool(barrier['active']),
                                                enabled=self._revision_is_editable(), entity_id=barrier['id']))
            self._barriers.setItem(row_index, 1, self._readonly_cell(barrier['sg_type']))
            self._barriers.setItem(row_index, 2, self._readonly_cell(barrier['description']))
            self._barriers.setItem(row_index, 3, self._readonly_cell(f"{barrier['rrf']:.6g}"))
            self._barriers.setItem(row_index, 4, self._readonly_cell(applies))
            self._barriers.setItem(row_index, 5, self._readonly_cell(status))
        self._barriers.resizeRowsToContents()
        enabled = bool(self._source_id) and self._revision_is_editable()
        self._add_barrier_btn.setEnabled(enabled)
        self._edit_barrier_btn.setEnabled(bool(rows) and enabled)
        if self._source_id:
            result = self.db.lopa_source_calculation(self._source_id)
            governing = next((row for row in result['categories']
                              if row['category_key'] == result['governing_category_key']), None)
            if governing and governing['remaining_frequency'] is not None:
                required = ('—' if governing['required_rrf'] is None
                            else f"{governing['required_rrf']:.6g}")
                self._barrier_summary.setText(
                    f"Återstående frekvens efter oberoende barriärer: "
                    f"{governing['remaining_frequency']:.6g} /år. "
                    f"Dimensionerande behov: "
                    f"{required} "
                    f"({governing['sil'] or 'SIL saknas'}).")
            else:
                self._barrier_summary.setText(
                    'Återstående frekvens och SIL kan beräknas när numerisk grundfrekvens och TEL finns.')
        else:
            self._barrier_summary.clear()
        self._loading = old_loading

    def _populate_barrier_matrix(self):
        """Show the screen-wide LOPA barrier picture without duplicating HAZOP.

        The editable list below stays scoped to the selected scenario.  This
        compact matrix mirrors the reference sheet: one source per row and
        one readable column for every configured safeguard type.
        """
        sources = self.db.lopa_sources(self._revision_id) if self._revision_id else []
        types = self.db.safeguard_types()
        headers = ['Källscenario', 'Grundfrekvens'] + list(types) + ['Återstående frekvens']
        old_loading = self._loading
        self._loading = True
        self._barrier_matrix.setColumnCount(len(headers))
        self._barrier_matrix.setHorizontalHeaderLabels(headers)
        self._barrier_matrix.setRowCount(len(sources))
        for row_index, source in enumerate(sources):
            cause = source.get('cause_text') or f"Källscenario {source['id']}"
            self._barrier_matrix.setItem(row_index, 0, self._readonly_cell(cause))
            frequency = source.get('base_frequency')
            self._barrier_matrix.setItem(
                row_index, 1, self._readonly_cell('—' if frequency is None else f'{frequency:.6g} /år'))
            by_type = {kind: [] for kind in types}
            for barrier in self.db.lopa_barriers(self._revision_id, source['id']):
                if not barrier['active']:
                    continue
                kind = barrier.get('sg_type') or 'Övrigt'
                by_type.setdefault(kind, []).append(
                    f"{barrier.get('description') or 'Namnlös'} (RRF {barrier['rrf']:.6g})")
            for column, kind in enumerate(types, start=2):
                self._barrier_matrix.setItem(
                    row_index, column, self._readonly_cell('\n'.join(by_type.get(kind) or []) or '—'))
            result = self.db.lopa_source_calculation(source['id'])
            remaining = [item['remaining_frequency'] for item in result['categories']
                         if item['active'] and item['remaining_frequency'] is not None]
            text = f'{max(remaining):.6g} /år' if remaining else '—'
            self._barrier_matrix.setItem(row_index, len(headers) - 1, self._readonly_cell(text))
        self._barrier_matrix.resizeRowsToContents()
        self._barrier_matrix.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._loading = old_loading

    def _on_barrier_item_changed(self, item):
        if self._loading or item.column() != 0:
            return
        barrier_id = item.data(self._ROLE_ENTITY_ID)
        if not barrier_id:
            return
        active = item.checkState() == Qt.CheckState.Checked
        if not self._confirm_lopa_only('Ska barriären ingå i just denna LOPA-beräkning?'):
            self._populate_barriers()
            return
        try:
            self.db.set_lopa_barrier_active(barrier_id, active)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra barriär', str(exc))
        self._populate_barriers()
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _add_manual_barrier(self):
        if not self._source_id:
            return
        dialog = LopaBarrierDialog(self.db.safeguard_types(), self._category_options(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.db.add_lopa_barrier(
                self._revision_id, self._source_id, description=dialog.description(), rrf=dialog.rrf(),
                sg_type=dialog.sg_type(), category_keys=dialog.category_keys(), manual=True)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till barriär', str(exc))
            return
        self._populate_barriers()
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _edit_selected_barrier(self):
        barrier_id = self._selected_entity_id(self._barriers)
        if not barrier_id:
            QMessageBox.information(self, 'Välj barriär', 'Välj först en barriärrad att ändra.')
            return
        barrier = next((row for row in self.db.lopa_barriers(self._revision_id, self._source_id)
                        if row['id'] == barrier_id), None)
        if not barrier:
            return
        categories = (None if barrier['applies_all_categories'] else
                      [row['category_key'] for row in self.db.lopa_barrier_categories(barrier_id)
                       if row['active']])
        dialog = LopaBarrierDialog(
            self.db.safeguard_types(), self._category_options(), barrier=barrier,
            category_keys=categories, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if barrier.get('follows_hazop') and not barrier.get('manual') and not self._confirm_lopa_only(
                'Ska barriärens data ändras lokalt i LOPA?'):
            return
        try:
            with self.db.history_group():
                self.db.update_lopa_barrier(
                    barrier_id, description=dialog.description(), rrf=dialog.rrf(),
                    sg_type=dialog.sg_type())
                self.db.set_lopa_barrier_category_keys(barrier_id, dialog.category_keys())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra barriär', str(exc))
            return
        self._populate_barriers()
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _populate_escalation(self):
        if not self._source_id:
            self._escalation.setRowCount(0)
            self._escalation.setColumnCount(0)
            return
        matrix = self.db.lopa_revision_matrix(self._revision_id)
        config = matrix.get('lopa') or {}
        settings = config.get('category_settings') or {}
        consequences = self.db.lopa_source_consequences(self._source_id)
        per_category = {}
        for consequence in consequences:
            key = consequence['category_key']
            current = per_category.get(key)
            if current is None or consequence['severity'] > current['severity']:
                per_category[key] = consequence
        escalation_rows = {row['category_key']: row
                           for row in self.db.lopa_escalation_rows(self._source_id)}
        factor_defs = []
        seen_factors = set()
        for key in per_category:
            for factor in settings.get(key, {}).get('escalation_factors') or []:
                factor_key = factor.get('key')
                if factor_key and factor_key not in seen_factors:
                    factor_defs.append(factor)
                    seen_factors.add(factor_key)
        headers = (['Aktiv', 'Kategori', 'Nivå', 'TEL (/år)'] +
                   [f"{factor['label']} (%)" for factor in factor_defs] +
                   ['Eskaleringsfaktor', 'Olycksfrekvens', 'RRF'])
        calculations = self.db.lopa_source_calculation(self._source_id)
        calculated_by_key = {}
        for row in calculations['categories']:
            old = calculated_by_key.get(row['category_key'])
            if old is None or (row['required_rrf'] or -1) > (old['required_rrf'] or -1):
                calculated_by_key[row['category_key']] = row
        old_loading = self._loading
        self._loading = True
        self._escalation.setColumnCount(len(headers))
        self._escalation.setHorizontalHeaderLabels(headers)
        self._escalation.setRowCount(len(per_category))
        editable = self._revision_is_editable()
        for row_index, (key, consequence) in enumerate(per_category.items()):
            setting = settings.get(key, {})
            escalation = escalation_rows.get(key, {})
            try:
                values = json.loads(escalation.get('factor_values_json') or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                values = {}
            active = bool(escalation.get('active', 1))
            calculated = calculated_by_key.get(key, {})
            active_item = self._check_cell(active, enabled=editable, entity_id=key)
            active_item.setData(self._ROLE_SOURCE_ID, self._source_id)
            self._escalation.setItem(row_index, 0, active_item)
            self._escalation.setItem(row_index, 1, self._readonly_cell(consequence['category_name']))
            self._escalation.setItem(row_index, 2, self._readonly_cell(consequence['severity']))
            tel = calculated.get('tel')
            self._escalation.setItem(
                row_index, 3, self._readonly_cell('—' if tel is None else f'{tel:.6g}'))
            supported = {factor.get('key'): factor for factor in setting.get('escalation_factors') or []}
            for column, factor in enumerate(factor_defs, start=4):
                definition = supported.get(factor['key'])
                if definition is None:
                    self._escalation.setItem(row_index, column, self._readonly_cell('—'))
                    continue
                percent = values.get(factor['key'], definition.get('default_percent', 100.0))
                item = QTableWidgetItem(f'{float(percent):.6g}')
                item.setData(self._ROLE_ENTITY_ID, key)
                item.setData(self._ROLE_FACTOR_KEY, factor['key'])
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._escalation.setItem(row_index, column, item)
            computed_column = 4 + len(factor_defs)
            factor = calculated.get('escalation_factor')
            accident = calculated.get('accident_frequency')
            rrf = calculated.get('required_rrf')
            self._escalation.setItem(
                row_index, computed_column,
                self._readonly_cell('—' if factor is None else f'{factor:.6g}'))
            self._escalation.setItem(
                row_index, computed_column + 1,
                self._readonly_cell('—' if accident is None else f'{accident:.6g} /år'))
            self._escalation.setItem(
                row_index, computed_column + 2,
                self._readonly_cell('—' if rrf is None else f'{rrf:.6g}'))
        self._escalation.resizeRowsToContents()
        self._escalation.resizeColumnsToContents()
        self._loading = old_loading

    def _on_escalation_item_changed(self, item):
        if self._loading or not self._source_id:
            return
        category_key = item.data(self._ROLE_ENTITY_ID)
        if not category_key:
            return
        escalation = next((row for row in self.db.lopa_escalation_rows(self._source_id)
                           if row['category_key'] == category_key), {})
        try:
            values = json.loads(escalation.get('factor_values_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            values = {}
        active = bool(escalation.get('active', 1))
        factor_key = item.data(self._ROLE_FACTOR_KEY)
        if factor_key:
            try:
                numeric = float(item.text().replace(',', '.'))
            except ValueError:
                QMessageBox.warning(self, 'Ogiltig procentsats', 'Ange ett tal mellan 0 och 100 eller högre vid behov.')
                self._populate_escalation()
                return
            if numeric < 0:
                QMessageBox.warning(self, 'Ogiltig procentsats', 'Procentsatsen får inte vara negativ.')
                self._populate_escalation()
                return
            values[factor_key] = numeric
        else:
            active = item.checkState() == Qt.CheckState.Checked
        try:
            self.db.set_lopa_escalation_values(self._source_id, category_key, values, active=active)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara eskalering', str(exc))
            self._populate_escalation()
            return
        self._populate_escalation()
        self._populate_calculation()
        self.changed.emit()

    def _populate_calculation(self):
        rows = self.db.lopa_sources(self._revision_id) if self._revision_id else []
        self._calculation.setRowCount(len(rows))
        for index, source in enumerate(rows):
            result = self.db.lopa_source_calculation(source['id'])
            source_text = source['cause_text'] or f"Källscenario {source['id']}"
            rrf = result['required_rrf'] if source['active'] else None
            rrf_text = f"{rrf:.3g}" if rrf is not None else '—'
            evidence = ('Exkluderat lokalt' if not source['active'] else
                        ('Komplett' if result['complete'] else
                         '; '.join(result['messages']) or 'Underlag saknas'))
            self._calculation.setItem(index, 0, self._cell(source_text))
            self._calculation.setItem(index, 1, self._cell(
                result['governing_category_name'] if source['active'] else '—'))
            self._calculation.setItem(index, 2, self._cell(rrf_text))
            self._calculation.setItem(index, 3, self._cell(result['sil'] if source['active'] else '—'))
            self._calculation.setItem(index, 4, self._cell(evidence))
        self._calculation.resizeRowsToContents()
        governing = []
        for source in rows:
            if not source['active']:
                continue
            result = self.db.lopa_source_calculation(source['id'])
            if result['required_rrf'] is not None:
                governing.append((result['required_rrf'], result, source))
        if governing:
            _rrf, result, source = max(governing, key=lambda item: item[0])
            self._dimensioning_summary.setText(
                f"Dimensionerande kriterium: {result['governing_category_name']} från "
                f"{source.get('cause_text') or 'källscenario'}. Beräknat behov: "
                f"RRF {result['required_rrf']:.6g} ({result['sil'] or 'SIL saknas'}).")
        elif rows:
            self._dimensioning_summary.setText(
                'Dimensionerande kriterium kan inte fastställas förrän aktivt scenario har numerisk frekvens och TEL.')
        else:
            self._dimensioning_summary.setText(
                'Koppla ett HAZOP-scenario eller skapa en tom LOPA med lokala underlag.')

    def _populate_comments(self):
        rows = self.db.lopa_comments(self._revision_id) if self._revision_id else []
        old_loading = self._loading
        self._loading = True
        self._comments.setRowCount(len(rows))
        for index, comment in enumerate(rows):
            self._comments.setItem(index, 0, self._readonly_cell(comment.get('created_at') or ''))
            self._comments.setItem(index, 1, self._readonly_cell(comment.get('author') or '—'))
            self._comments.setItem(index, 2, self._readonly_cell(comment.get('body') or ''))
        self._comments.resizeRowsToContents()
        self._loading = old_loading

    def _save_document_details(self):
        if self._loading or not self._revision_id:
            return
        try:
            self.db.update_lopa_revision_details(
                self._revision_id,
                document_date=self._document_date.text(),
                performed_by_text=self._performed_by.text(),
                approved_by_text=self._approved_by.text(),
                additional_actions=self._additional_actions.toPlainText(),
                additional_requirements=self._additional_requirements.toPlainText(),
                process_safety_time=self._process_safety_time.text())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara dokumentuppgifter', str(exc))
            return
        self.changed.emit()

    def _add_comment(self):
        if not self._revision_id:
            return
        try:
            self.db.add_lopa_comment(
                self._revision_id, self._comment_text.text(), self._comment_author.text())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte lägga till kommentar', str(exc))
            return
        self._comment_text.clear()
        self._populate_comments()
        self.changed.emit()

    def _create_lopa(self):
        try:
            created = self.db.create_lopa()
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte skapa LOPA', str(exc))
            return
        self._lopa_id = created['lopa_id']
        self._revision_id = created['revision_id']
        self.refresh()
        self.changed.emit()

    def _export_excel(self):
        dialog = LopaExportDialog(self.db, self._lopa_id, self._revision_id, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selections = dialog.selections()
        if not selections:
            QMessageBox.information(self, 'Välj LOPA', 'Välj minst en LOPA-revision att exportera.')
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Exportera LOPA till Excel', 'lopa.xlsx', 'Excel-filer (*.xlsx)')
        if not path:
            return
        if not path.lower().endswith('.xlsx'):
            path += '.xlsx'
        ok, error = export_lopa_excel(self.db, path, selections)
        if not ok:
            QMessageBox.warning(self, 'Kunde inte exportera LOPA', error)
            return
        QMessageBox.information(self, 'LOPA exporterad',
                                f'Excel-exporten skapades:\n{path}')

    def _archive_lopa(self):
        if not self._lopa_id:
            return
        record = self.db.get_lopa_record(self._lopa_id)
        if not record:
            return
        result = QMessageBox.question(
            self, 'Arkivera LOPA?',
            f"Arkivera LOPA {record['display_number']}?\n\n"
            'Arket och alla revisioner finns kvar men döljs i normal listvy.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if result != QMessageBox.StandardButton.Yes:
            return
        self.db.archive_lopa(self._lopa_id)
        self.refresh()
        self.changed.emit()

    def _save_header(self):
        if self._loading or not self._lopa_id:
            return
        try:
            self.db.update_lopa_record(
                self._lopa_id,
                display_number=self._number.text(),
                sif_number=self._sif_number.text(),
                sif_name=self._sif_name.text(),
                sis_name=self._sis_name.text())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara LOPA', str(exc))
            self._load_record(self._revision_id)
            return
        self.refresh()
        self.changed.emit()

    def _save_revision_details(self):
        if self._loading or not self._revision_id:
            return
        try:
            self.db.update_lopa_revision_details(
                self._revision_id,
                document_date=self._document_date.text(),
                performed_by_text=self._performed_by.text(),
                approved_by_text=self._approved_by.text())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara revisionsuppgifter', str(exc))
            self._refresh_revision_detail()
            return
        self.changed.emit()

    def _choose_performed_by(self):
        participants = []
        for participant in self.db.list_participants():
            name = ' '.join(part for part in (
                participant['first_name'], participant['last_name']) if str(part or '').strip()).strip()
            if name:
                participants.append(name)
        if not participants:
            QMessageBox.information(
                self, 'Inga projektdeltagare',
                'Lägg först till deltagare under HAZOP preparation. Du kan alltid skriva externa namn fritt här.')
            return
        selected = {value.strip() for value in self._performed_by.text().split(',') if value.strip()}
        dialog = LopaParticipantPickerDialog(participants, selected, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._performed_by.setText(', '.join(dialog.selected_names()))
        self._save_revision_details()

    def _create_revision(self):
        if not self._lopa_id:
            return
        try:
            revision_id = self.db.create_lopa_revision(self._lopa_id, self._revision_id)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte skapa revision', str(exc))
            return
        self._revision_id = revision_id
        self._load_record(revision_id)
        self.changed.emit()

    def _toggle_lock(self):
        if not self._revision_id:
            return
        revision = self.db.get_lopa_revision(self._revision_id)
        if not revision:
            return
        try:
            if revision['status'] in ('Låst', 'Godkänd', 'Arkiverad'):
                self.db.unlock_lopa_revision(self._revision_id, 'Upplåst från LOPA-vyn')
            else:
                self.db.lock_lopa_revision(self._revision_id)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte ändra revisionsstatus', str(exc))
            return
        self._refresh_revision_detail()
        self.changed.emit()

    def activate_lopa(self, lopa_id, revision_id=None):
        """Select a LOPA after a HAZOP barrier has been linked to it."""
        self._lopa_id = lopa_id
        self._revision_id = revision_id
        self.refresh()


class LopaConsequenceDialog(QDialog):
    """Small local-override editor for one HAZOP-derived consequence row."""

    def __init__(self, consequence, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Ändra LOPA-konsekvens')
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        hint = QLabel('Ändringen gäller bara den öppna LOPA-revisionen. HAZOP påverkas inte.')
        hint.setWordWrap(True)
        hint.setStyleSheet(lopa_note_stylesheet())
        layout.addWidget(hint)
        form = QFormLayout()
        self._description = QPlainTextEdit(consequence.get('description') or '')
        self._description.setFixedHeight(90)
        self._severity = QSpinBox()
        self._severity.setRange(0, 99)
        self._severity.setValue(int(consequence.get('severity') or 0))
        form.addRow('Beskrivning', self._description)
        form.addRow('Konsekvensnivå', self._severity)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def description(self):
        return self._description.toPlainText()

    def severity(self):
        return self._severity.value()


class LopaNewConsequenceDialog(QDialog):
    """Create an explicitly local LOPA consequence for the selected source."""

    def __init__(self, category_options, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Egen LOPA-konsekvens')
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        hint = QLabel('Raden skapas endast i den öppna LOPA-revisionen och ändrar inte HAZOP.')
        hint.setWordWrap(True)
        hint.setStyleSheet(lopa_note_stylesheet())
        layout.addWidget(hint)
        form = QFormLayout()
        self._category = QComboBox()
        for key, name in category_options:
            self._category.addItem(name, (key, name))
        self._description = QPlainTextEdit()
        self._description.setFixedHeight(82)
        self._severity = QSpinBox()
        self._severity.setRange(1, 99)
        self._severity.setValue(1)
        form.addRow('Kategori', self._category)
        form.addRow('Nivå', self._severity)
        form.addRow('Beskrivning', self._description)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def category(self):
        return self._category.currentData()

    def description(self):
        return self._description.toPlainText()

    def severity(self):
        return self._severity.value()


class LopaBarrierDialog(QDialog):
    """One compact editor for both manual and locally overridden barriers."""

    def __init__(self, safeguard_types, category_options, barrier=None, category_keys=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('LOPA-barriär')
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._type = QComboBox()
        self._type.addItems([str(value) for value in safeguard_types])
        self._description = QLineEdit()
        self._rrf = QDoubleSpinBox()
        self._rrf.setRange(1.0, 1_000_000_000_000.0)
        self._rrf.setDecimals(6)
        self._rrf.setValue(float((barrier or {}).get('rrf') or 1.0))
        if barrier:
            self._type.setCurrentText(str(barrier.get('sg_type') or 'Övrigt'))
            self._description.setText(str(barrier.get('description') or ''))
        form.addRow('Typ', self._type)
        form.addRow('Beskrivning', self._description)
        form.addRow('RRF', self._rrf)
        layout.addLayout(form)
        category_title = QLabel('Gäller konsekvenskategorier')
        category_title.setStyleSheet(lopa_section_title_stylesheet())
        layout.addWidget(category_title)
        hint = QLabel('Alla ikryssade betyder att barriären används för dessa kategorier. Alla valda = gäller alla.')
        hint.setWordWrap(True)
        hint.setStyleSheet(lopa_note_stylesheet())
        layout.addWidget(hint)
        self._categories = QListWidget()
        self._categories.setMaximumHeight(150)
        selected = None if category_keys is None else set(category_keys)
        for key, name in category_options:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if selected is None or key in selected else Qt.CheckState.Unchecked)
            self._categories.addItem(item)
        layout.addWidget(self._categories)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def sg_type(self):
        return self._type.currentText().strip() or 'Övrigt'

    def description(self):
        return self._description.text()

    def rrf(self):
        return self._rrf.value()

    def category_keys(self):
        keys = []
        all_checked = self._categories.count() > 0
        for index in range(self._categories.count()):
            item = self._categories.item(index)
            checked = item.checkState() == Qt.CheckState.Checked
            all_checked = all_checked and checked
            if checked:
                keys.append(item.data(Qt.ItemDataRole.UserRole))
        return None if all_checked else keys


class LopaParticipantPickerDialog(QDialog):
    """Multi-select project participants without restricting free-text names."""

    def __init__(self, participants, selected_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Välj deltagare')
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        label = QLabel('Kryssa i projektdeltagare. Externa namn kan fortfarande skrivas direkt i fältet.')
        label.setWordWrap(True)
        label.setStyleSheet(lopa_note_stylesheet())
        layout.addWidget(label)
        self._items = QListWidget()
        for name in participants:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in selected_names else Qt.CheckState.Unchecked)
            self._items.addItem(item)
        layout.addWidget(self._items)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_names(self):
        return [self._items.item(index).text() for index in range(self._items.count())
                if self._items.item(index).checkState() == Qt.CheckState.Checked]


class LopaExportDialog(QDialog):
    """Choose one or more stored LOPA revisions for a workbook export."""

    def __init__(self, db, current_lopa_id=None, current_revision_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Exportera LOPA till Excel')
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        hint = QLabel(
            'Varje vald revision exporteras från sitt sparade revisionsunderlag. '
            'En låst revision ändras alltså aldrig av dagens HAZOP-data.')
        hint.setWordWrap(True)
        hint.setStyleSheet(lopa_note_stylesheet())
        layout.addWidget(hint)
        self._items = QListWidget()
        for record in db.lopa_records(include_archived=True):
            for revision in db.lopa_revisions(record['id']):
                text = (f"LOPA {record['display_number']} – "
                        f"{record.get('sif_number') or record['sif_name'] or 'Namnlös SIF'} – "
                        f"rev. {revision['label']} ({revision['status']})")
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, (record['id'], revision['id']))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                selected = (record['id'] == current_lopa_id and revision['id'] == current_revision_id)
                item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
                self._items.addItem(item)
        layout.addWidget(self._items)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Välj export')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selections(self):
        return [self._items.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self._items.count())
                if self._items.item(index).checkState() == Qt.CheckState.Checked]


class LopaLinkDialog(QDialog):
    """Small, explicit bridge from one HAZOP safeguard to a LOPA source.

    The bridge asks for a stable equipment object and structured trigger
    before creating the link; it deliberately never infers ``HH``/``LL``
    from free text.
    """

    _NEW_LOPA = '__new_lopa__'

    def __init__(self, db: Database, safeguard_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.safeguard_id = safeguard_id
        self.result_lopa_id = None
        self.result_revision_id = None
        safeguard = db.get_safeguard(safeguard_id) or {}
        self.setWindowTitle('Koppla barriär till LOPA')
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        intro = QLabel(
            'Barriären blir givardel i LOPA:n. Övriga HAZOP-barriärer på '
            'samma orsak importeras som oberoende skydd.')
        intro.setWordWrap(True)
        intro.setStyleSheet(f'color:{SECONDARY_TEXT};')
        layout.addWidget(intro)
        form = QFormLayout()
        self._lopa = QComboBox()
        self._lopa.addItem('Ny LOPA…', self._NEW_LOPA)
        for record in db.lopa_records():
            self._lopa.addItem(
                f"{record['display_number']} – {record['sif_name'] or 'Namnlös SIF'}",
                record['id'])
        self._new_name = QLineEdit()
        self._new_name.setText(safeguard.get('description') or '')
        self._new_name.setPlaceholderText('SIF-beteckning / namn')
        self._equipment = QComboBox()
        self._equipment.addItem('Välj objekt…', None)
        for equipment in db.equipment_items():
            label = equipment['tag'] or f"Objekt {equipment['id']}"
            if equipment['equipment_type']:
                label += f" – {equipment['equipment_type']}"
            self._equipment.addItem(label, equipment['id'])
        linked = db.safeguard_equipment_links(safeguard_id)
        preferred_id = linked[0]['equipment_id'] if linked else None
        if preferred_id is None and safeguard.get('comp_tag'):
            equipment = db.get_equipment_by_tag(safeguard['comp_tag'])
            preferred_id = equipment.get('id') if equipment else None
        if preferred_id is not None:
            index = self._equipment.findData(preferred_id)
            if index >= 0:
                self._equipment.setCurrentIndex(index)
        self._trigger = QComboBox()
        self._trigger.addItems(['', 'H', 'HH', 'L', 'LL', 'Till', 'Från', 'Eget…'])
        if linked and linked[0].get('trigger_code'):
            index = self._trigger.findText(linked[0]['trigger_code'])
            if index >= 0:
                self._trigger.setCurrentIndex(index)
        self._custom_trigger = QLineEdit()
        self._custom_trigger.setPlaceholderText('Egen utlösare')
        self._custom_trigger.setVisible(False)
        self._trigger.currentTextChanged.connect(
            lambda text: self._custom_trigger.setVisible(text == 'Eget…'))
        form.addRow('LOPA', self._lopa)
        form.addRow('Namn vid ny LOPA', self._new_name)
        form.addRow('Objekt', self._equipment)
        form.addRow('Utlösare', self._trigger)
        form.addRow('', self._custom_trigger)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Koppla')
        buttons.accepted.connect(self._accept_link)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_link(self):
        equipment_id = self._equipment.currentData()
        if equipment_id is None:
            QMessageBox.warning(self, 'Objekt saknas',
                                'Välj objektet som ska utgöra LOPA:ns givardel.')
            return
        selected = self._lopa.currentData()
        try:
            if selected == self._NEW_LOPA:
                created = self.db.create_lopa(sif_name=self._new_name.text())
                lopa_id = created['lopa_id']
            else:
                lopa_id = int(selected)
            trigger = self._trigger.currentText()
            custom = self._custom_trigger.text().strip() if trigger == 'Eget…' else ''
            if trigger == 'Eget…':
                trigger = ''
            self.db.add_safeguard_equipment_link(
                self.safeguard_id, equipment_id, trigger, custom)
            imported = self.db.add_lopa_source_from_safeguard(lopa_id, self.safeguard_id)
            revision = self.db.current_lopa_revision(lopa_id)
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte koppla till LOPA', str(exc))
            return
        self.result_lopa_id = lopa_id
        self.result_revision_id = revision['id'] if revision else None
        self.accept()
