#!/usr/bin/env python3
"""ParticipantMatrixPanel + its inline header-rename editor -- split out of settings_panels.py 2026-08-21, see NOTES.md "Dela upp settings_panels.py"."""

import re
import json
from pathlib import Path
from functools import partial

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDateEdit,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal, QDate, QEvent, QMimeData
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


class ParticipantMatrixPanel(QWidget):
    """Deltagarmatris: participants as rows (Förnamn/Efternamn/Roll) ×
    analystillfällen as columns, with a checkbox per cell marking
    attendance. Replaces the old free-text "Deltagare" field in the
    Projekt tab (2026-08-11, user request: "en till flik med deltagare
    istället där man definerar förnamn, efternamn, roll på y axel och
    analystillfälen på x axeln så det blir en matris" — see NOTES.md for
    the full design rationale)."""

    _FIXED_COLS = ['Förnamn', 'Efternamn']

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False
        self._participant_ids = []
        self._session_ids = []
        self._column_ids = []   # custom participant_columns, between Efternamn and sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        lbl = QLabel(
            "<b>Deltagarmatris</b> — en rad per deltagare (förnamn, efternamn, roll) "
            "och en kolumn per analystillfälle. Bocka i cellen för att markera att "
            "deltagaren var med vid det tillfället.")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        self._table = QTableWidget(0, len(self._FIXED_COLS))
        self._table.setHorizontalHeaderLabels(self._FIXED_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._on_item_changed)
        # Enter on a selected (non-editing) cell adds a new participant row,
        # matching the same "+"-button action (2026-08-17 user request).
        _base_kp = self._table.keyPressEvent
        def _table_key_press(event, _base=_base_kp):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                    self._table.state() != QTableWidget.State.EditingState:
                self._add_participant()
            else:
                _base(event)
        self._table.keyPressEvent = _table_key_press
        header = _SessionHeaderView(Qt.Orientation.Horizontal, self._table)
        self._table.setHorizontalHeader(header)
        header.setSectionsClickable(True)
        header.sectionDoubleClicked.connect(self._edit_header_label)
        header.location_dropped.connect(self._copy_session_location)
        layout.addWidget(self._table)

        # Compact controls directly below the matrix: plus/minus are placed
        # next to the thing they affect instead of one long toolbar.
        btn_row = QHBoxLayout()
        btn_add_p = QPushButton("+")
        btn_add_p.setToolTip("Lägg till deltagare")
        btn_add_p.setFixedWidth(30)
        btn_add_p.clicked.connect(self._add_participant)
        btn_del_p = QPushButton("−")
        btn_del_p.setToolTip("Ta bort markerad deltagare")
        btn_del_p.setFixedWidth(30)
        btn_del_p.setToolTip("Tar bort den markerade raden (deltagaren)")
        btn_del_p.clicked.connect(self._delete_participant)
        btn_row.addWidget(QLabel("Deltagare"))
        btn_row.addWidget(btn_add_p)
        btn_row.addWidget(btn_del_p)
        btn_add_col = QPushButton("+ Övrig kolumn")
        btn_add_col.setToolTip("Lägg till en egen namngiven kolumn (t.ex. E-post, Företag, Roll)")
        btn_add_col.clicked.connect(self._add_column)
        btn_del_col = QPushButton("− Övrig kolumn")
        btn_del_col.setToolTip("Tar bort den egna kolumn en markerad cell tillhör")
        btn_del_col.clicked.connect(self._delete_column)
        btn_add_s = QPushButton("+ Analystillfälle")
        btn_add_s.clicked.connect(self._add_session)
        btn_del_s = QPushButton("− Analystillfälle")
        btn_del_s.setToolTip("Tar bort kolumnen för det tillfälle en markerad cell tillhör")
        btn_del_s.clicked.connect(self._delete_session)
        for b in (btn_add_col, btn_del_col, btn_add_s, btn_del_s):
            btn_row.addWidget(b)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        self._loading = True
        try:
            columns = self.db.list_participant_columns()
            sessions = self.db.list_analysis_sessions()
            participants = self.db.list_participants()
            attendance = self.db.get_attendance_matrix()
            col_values = self.db.get_participant_column_values()

            self._column_ids = [c['id'] for c in columns]
            self._session_ids = [s['id'] for s in sessions]
            self._participant_ids = [p['id'] for p in participants]

            headers = (list(self._FIXED_COLS) + [c['name'] for c in columns] +
                       [self._session_header_text(s) for s in sessions])
            self._table.setColumnCount(len(headers))
            self._table.setHorizontalHeaderLabels(headers)
            self._table.setRowCount(len(participants))

            n_custom = len(columns)
            n_fixed = len(self._FIXED_COLS) + n_custom
            for row, p in enumerate(participants):
                self._table.setItem(row, 0, QTableWidgetItem(p['first_name'] or ''))
                self._table.setItem(row, 1, QTableWidgetItem(p['last_name'] or ''))
                for ci, col_def in enumerate(columns):
                    val = col_values.get((p['id'], col_def['id']), '')
                    self._table.setItem(row, len(self._FIXED_COLS) + ci, QTableWidgetItem(val))
                for col, sess in enumerate(sessions):
                    item = QTableWidgetItem()
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable)
                    attended = attendance.get((p['id'], sess['id']), False)
                    item.setCheckState(
                        Qt.CheckState.Checked if attended else Qt.CheckState.Unchecked)
                    self._table.setItem(row, n_fixed + col, item)
        finally:
            self._loading = False

    def _on_item_changed(self, item):
        if self._loading:
            return
        row, col = item.row(), item.column()
        if row < 0 or row >= len(self._participant_ids):
            return
        pid = self._participant_ids[row]
        n_base = len(self._FIXED_COLS)
        if col == 0:
            self.db.update_participant(pid, first_name=item.text())
        elif col == 1:
            self.db.update_participant(pid, last_name=item.text())
        elif col < n_base + len(self._column_ids):
            col_id = self._column_ids[col - n_base]
            self.db.set_participant_column_value(pid, col_id, item.text())
        else:
            sess_idx = col - n_base - len(self._column_ids)
            if 0 <= sess_idx < len(self._session_ids):
                sess_id = self._session_ids[sess_idx]
                attended = item.checkState() == Qt.CheckState.Checked
                self.db.set_attendance(pid, sess_id, attended)

    def _add_participant(self):
        self.db.add_participant('', '', '')
        self.refresh()

    def _delete_participant(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._participant_ids):
            return
        self.db.delete_participant(self._participant_ids[row])
        self.refresh()

    def _add_column(self):
        name, ok = QInputDialog.getText(
            self, "Ny kolumn", "Kolumnnamn (t.ex. E-post, Företag, Roll):")
        if ok and name.strip():
            self.db.add_participant_column(name.strip())
            self.refresh()


    def _delete_column(self):
        col = self._table.currentColumn()
        col_idx = col - len(self._FIXED_COLS)
        if col < 0 or col_idx < 0 or col_idx >= len(self._column_ids):
            return
        self.db.delete_participant_column(self._column_ids[col_idx])
        self.refresh()

    def _add_session(self):
        """Adds an analystillfälle directly with today's date as the label —
        no popup — then drops straight into inline header editing so the
        user can adjust the date/label without leaving the table
        (2026-08-18 user request, replacing the old _AnalysisSessionDateDialog
        popup)."""
        new_id = self.db.add_analysis_session()
        self.refresh()

    def _delete_session(self):
        col = self._table.currentColumn()
        sess_idx = col - len(self._FIXED_COLS) - len(self._column_ids)
        if col < 0 or sess_idx < 0 or sess_idx >= len(self._session_ids):
            return
        self.db.delete_analysis_session(self._session_ids[sess_idx])
        self.refresh()

    @staticmethod
    def _session_header_text(session):
        session = dict(session)
        date = session.get('date') or session.get('label') or f"Tillfälle {session['id']}"
        location = session.get('location') or ''
        return f"{date}\n{location}" if location else str(date)

    def _copy_session_location(self, source_col, target_col):
        n_fixed = len(self._FIXED_COLS) + len(self._column_ids)
        src = source_col - n_fixed
        dst = target_col - n_fixed
        if src < 0 or dst < 0 or src >= len(self._session_ids) or dst >= len(self._session_ids):
            return
        sessions = [dict(s) for s in self.db.list_analysis_sessions()]
        if src >= len(sessions):
            return
        location = sessions[src].get('location') or ''
        if location:
            self.db.set_analysis_session_location(self._session_ids[dst], location)
            self.refresh()

    def _header_kind(self, col):
        """Returns ('column', id) / ('session', id) for a renamable header,
        or None for the fixed Förnamn/Efternamn columns."""
        n_fixed = len(self._FIXED_COLS)
        n_custom = len(self._column_ids)
        if col < n_fixed:
            return None
        if col < n_fixed + n_custom:
            return ('column', self._column_ids[col - n_fixed])
        sess_idx = col - n_fixed - n_custom
        if 0 <= sess_idx < len(self._session_ids):
            return ('session', self._session_ids[sess_idx])
        return None

    def _edit_header_label(self, col):
        """Opens an inline QLineEdit directly over the header section for
        renaming a custom column or analystillfälle — no popup dialog
        (2026-08-18 user request)."""
        kind = self._header_kind(col)
        if kind is None:
            return
        if kind[0] == 'session':
            self._edit_session_details(col)
            return
        header = self._table.horizontalHeader()
        item = self._table.horizontalHeaderItem(col)
        current_text = item.text() if item else ''

        editor = _InlineHeaderEdit(header)
        editor.setText(current_text)
        editor.setGeometry(
            header.sectionViewportPosition(col), 0,
            header.sectionSize(col), header.height())

        state = {'done': False}

        def finish(save):
            if state['done']:
                return
            state['done'] = True
            text = editor.text().strip()
            editor.deleteLater()
            if save and text and text != current_text:
                self._rename_header(kind, text)

        editor.editingFinished.connect(lambda: finish(True))
        editor.canceled.connect(lambda: finish(False))
        editor.show()
        editor.setFocus()
        editor.selectAll()

    def _rename_header(self, kind, text):
        typ, id_ = kind
        if typ == 'column':
            self.db.update_participant_column(id_, text)
        else:
            self.db.update_analysis_session(id_, text)
        self.refresh()

    def _edit_session_details(self, col):
        kind = self._header_kind(col)
        if not kind or kind[0] != 'session':
            return
        session = next((dict(s) for s in self.db.list_analysis_sessions() if s['id'] == kind[1]), None)
        if not session:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Analystillfälle")
        form = QFormLayout(dlg)
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        raw_date = session.get('date') or session.get('label') or ''
        parsed = QDate.fromString(str(raw_date)[:10], 'yyyy-MM-dd')
        date_edit.setDate(parsed if parsed.isValid() else QDate.currentDate())
        location_edit = QLineEdit(session.get('location') or '')
        location_edit.setPlaceholderText("Plats")
        form.addRow("Datum:", date_edit)
        form.addRow("Plats:", location_edit)
        buttons = QHBoxLayout()
        ok = QPushButton("OK"); cancel = QPushButton("Avbryt")
        ok.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject)
        buttons.addWidget(ok); buttons.addWidget(cancel)
        form.addRow(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            iso = date_edit.date().toString('yyyy-MM-dd')
            self.db.update_analysis_session_details(kind[1], date=iso,
                                                    location=location_edit.text().strip(),
                                                    label=iso)
            self.refresh()


class _SessionHeaderView(QHeaderView):
    """Header that supports dragging a filled session location to another
    analystillfälle column."""
    location_dropped = pyqtSignal(int, int)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setAcceptDrops(True)
        self._press_pos = None
        self._source_section = -1

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._source_section = self.logicalIndexAt(self._press_pos.x())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._press_pos is None:
            return super().mouseMoveEvent(event)
        if (event.position().toPoint() - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            return super().mouseMoveEvent(event)
        model = self.model(); source = self._source_section
        if source < 0 or model is None:
            return super().mouseMoveEvent(event)
        text = model.headerData(source, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or ''
        parts = str(text).split('\n', 1)
        if len(parts) < 2 or not parts[1].strip():
            return super().mouseMoveEvent(event)
        mime = QMimeData()
        mime.setData('application/x-prosa-session-location', str(source).encode('utf-8'))
        drag = QDrag(self); drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)
        self._press_pos = None

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-prosa-session-location'):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat('application/x-prosa-session-location'):
            event.ignore(); return
        try:
            source = int(bytes(event.mimeData().data('application/x-prosa-session-location')).decode('utf-8'))
            target = self.logicalIndexAt(event.position().toPoint().x())
        except (TypeError, ValueError):
            event.ignore(); return
        if target >= 0 and source != target:
            self.location_dropped.emit(source, target)
        event.acceptProposedAction()


class _InlineHeaderEdit(QLineEdit):
    """QLineEdit embedded as a header-section child for inline header
    renaming — Escape cancels without committing (plain QLineEdit has no
    such behavior built in outside of an item delegate)."""

    canceled = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.canceled.emit()
            return
        super().keyPressEvent(event)
