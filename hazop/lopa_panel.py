"""First LOPA workspace page.

The page is intentionally a small, usable vertical slice: create/manual
number LOPAs, manage revisions, inspect HAZOP-linked scenarios and see the
calculated governing RRF/SIL.  Detailed editors and the HAZOP context-menu
entry build on the same Database methods in subsequent phases.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database import Database
from design import (
    LOPA_CARD_PADDING,
    MUTED_TEXT,
    SECONDARY_TEXT,
    TEXT,
    lopa_card_stylesheet,
    lopa_status_stylesheet,
)


class LopaPanel(QWidget):
    """LOPA list + revision detail page, independent of ``hazop.py``."""

    changed = pyqtSignal()

    _ROLE_LOPA_ID = int(Qt.ItemDataRole.UserRole)
    _ROLE_REVISION_ID = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._lopa_id = None
        self._revision_id = None
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
        title.setStyleSheet('font-size:20px;font-weight:700;color:#17191C;')
        heading.addWidget(title)
        subtitle = QLabel('Skyddsbarriäranalys – samma riskmatris som HAZOP')
        subtitle.setStyleSheet(f'color:{SECONDARY_TEXT};')
        heading.addWidget(subtitle)
        heading.addStretch(1)
        self._show_archived = QPushButton('Visa arkiverade')
        self._show_archived.setCheckable(True)
        self._show_archived.toggled.connect(self.refresh)
        heading.addWidget(self._show_archived)
        self._new_btn = QPushButton('Ny tom LOPA')
        self._new_btn.clicked.connect(self._create_lopa)
        heading.addWidget(self._new_btn)
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
        label.setStyleSheet('font-weight:700;')
        left_layout.addWidget(label)
        hint = QLabel('Varje ark är en SIF. Arkiverade ark kan återställas men automatiska nummer återanvänds inte.')
        hint.setWordWrap(True)
        hint.setStyleSheet(f'color:{MUTED_TEXT};font-size:9pt;')
        left_layout.addWidget(hint)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentItemChanged.connect(self._on_lopa_selected)
        left_layout.addWidget(self._list, 1)
        splitter.addWidget(left)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
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
        header_layout.addLayout(top)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._number = QLineEdit()
        self._number.setPlaceholderText('001')
        self._number.editingFinished.connect(self._save_header)
        self._sif_name = QLineEdit()
        self._sif_name.setPlaceholderText('SIF-beteckning / namn')
        self._sif_name.editingFinished.connect(self._save_header)
        self._sis_name = QLineEdit()
        self._sis_name.setPlaceholderText('SIS / system')
        self._sis_name.editingFinished.connect(self._save_header)
        self._revision = QComboBox()
        self._revision.currentIndexChanged.connect(self._on_revision_changed)
        form.addRow('LOPA-nr', self._number)
        form.addRow('SIF', self._sif_name)
        form.addRow('SIS', self._sis_name)
        form.addRow('Revision', self._revision)
        header_layout.addLayout(form)
        self._sync_note = QLabel('Tom LOPA: koppla en HAZOP-barriär för att importera källscenario och givardel.')
        self._sync_note.setWordWrap(True)
        self._sync_note.setStyleSheet(f'color:{SECONDARY_TEXT};font-size:9pt;')
        header_layout.addWidget(self._sync_note)
        detail_layout.addWidget(header)

        source_card = self._card()
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                         LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        source_label = QLabel('Källscenarier från HAZOP')
        source_label.setStyleSheet('font-weight:700;')
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
        self._sources.setMinimumHeight(150)
        source_layout.addWidget(self._sources)
        detail_layout.addWidget(source_card, 2)

        calculation_card = self._card()
        calculation_layout = QVBoxLayout(calculation_card)
        calculation_layout.setContentsMargins(LOPA_CARD_PADDING, LOPA_CARD_PADDING,
                                              LOPA_CARD_PADDING, LOPA_CARD_PADDING)
        calculation_title = QLabel('Beräkningsöversikt')
        calculation_title.setStyleSheet('font-weight:700;')
        calculation_layout.addWidget(calculation_title)
        self._calculation = QTableWidget(0, 5)
        self._calculation.setHorizontalHeaderLabels(
            ['Källscenario', 'Dimensionerande kategori', 'Behov av RRF', 'SIL', 'Underlag'])
        self._calculation.verticalHeader().setVisible(False)
        self._calculation.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._calculation.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._calculation.horizontalHeader().setStretchLastSection(True)
        self._calculation.setMinimumHeight(120)
        calculation_layout.addWidget(self._calculation)
        detail_layout.addWidget(calculation_card, 1)
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
        self._record_title.setText('Välj eller skapa en LOPA')
        self._status.hide()
        self._revision.clear()
        for widget in (self._number, self._sif_name, self._sis_name):
            widget.clear()
            widget.setEnabled(False)
        self._new_revision_btn.setEnabled(False)
        self._lock_btn.setEnabled(False)
        self._sources.setRowCount(0)
        self._calculation.setRowCount(0)
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
        self._sif_name.setText(record['sif_name'])
        self._sis_name.setText(record['sis_name'])
        for widget in (self._number, self._sif_name, self._sis_name):
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
        self._lock_btn.setText('Lås upp revision' if locked else 'Lås revision')
        self._populate_sources()
        self._populate_calculation()

    @staticmethod
    def _cell(text, alignment=None):
        item = QTableWidgetItem(str(text or '—'))
        if alignment is not None:
            item.setTextAlignment(int(alignment))
        return item

    def _populate_sources(self):
        rows = self.db.lopa_sources(self._revision_id) if self._revision_id else []
        self._sources.setRowCount(len(rows))
        for row_index, source in enumerate(rows):
            status = 'Följer HAZOP' if source['follows_hazop'] else 'Frikopplad från HAZOP'
            if source['source_missing']:
                status = 'Källa saknas i HAZOP'
            object_trigger = ' '.join(part for part in (
                source.get('equipment_tag') or '', source.get('trigger_code') or '',
                source.get('trigger_custom') or '') if part).strip() or '—'
            self._sources.setItem(row_index, 0, self._cell('Ja' if source['active'] else 'Nej'))
            self._sources.setItem(row_index, 1, self._cell(object_trigger))
            self._sources.setItem(row_index, 2, self._cell(source['cause_text']))
            frequency = (f"{source['base_frequency']:.3g} /år"
                         if source['base_frequency'] is not None else 'Numeriskt värde saknas')
            self._sources.setItem(row_index, 3, self._cell(frequency))
            self._sources.setItem(row_index, 4, self._cell(
                f"Orsak {source['hazop_cause_id']}" if source['hazop_cause_id'] else 'Ingen HAZOP-källa'))
            self._sources.setItem(row_index, 5, self._cell(status))
        self._sources.resizeRowsToContents()
        self._sync_note.setText(
            'Aktiva rader följer HAZOP tills de uttryckligen kopplas loss. '
            'Låsta revisioner behåller sin egen riskmatris och sitt underlag.')

    def _populate_calculation(self):
        rows = self.db.lopa_sources(self._revision_id) if self._revision_id else []
        self._calculation.setRowCount(len(rows))
        for index, source in enumerate(rows):
            result = self.db.lopa_source_calculation(source['id'])
            source_text = source['cause_text'] or f"Källscenario {source['id']}"
            rrf = result['required_rrf']
            rrf_text = f"{rrf:.3g}" if rrf is not None else '—'
            evidence = 'Komplett' if result['complete'] else '; '.join(result['messages']) or 'Underlag saknas'
            self._calculation.setItem(index, 0, self._cell(source_text))
            self._calculation.setItem(index, 1, self._cell(result['governing_category_name']))
            self._calculation.setItem(index, 2, self._cell(rrf_text))
            self._calculation.setItem(index, 3, self._cell(result['sil']))
            self._calculation.setItem(index, 4, self._cell(evidence))
        self._calculation.resizeRowsToContents()

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

    def _save_header(self):
        if self._loading or not self._lopa_id:
            return
        try:
            self.db.update_lopa_record(
                self._lopa_id,
                display_number=self._number.text(),
                sif_name=self._sif_name.text(),
                sis_name=self._sis_name.text())
        except Exception as exc:
            QMessageBox.warning(self, 'Kunde inte spara LOPA', str(exc))
            self._load_record(self._revision_id)
            return
        self.refresh()
        self.changed.emit()

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
