"""ARCHIVED 2026-08-27 — inactive safeguard object-picker feature.

This module preserves the removed implementation for historical reference.
It is deliberately not imported by the application. The live HAZOP Scenario
safeguard cell no longer draws an emoji, reserves an icon click zone, or
opens an object-selection popup. Object association through drag-and-drop is
unchanged.
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QCompleter, QLabel, QVBoxLayout, QWidget

from constants import CONFIG
from ui_helpers import _equipment_tags_for_types, _resolve_comp_type_for_tag


class ArchivedSafeguardObjectPopup(QWidget):
    """Former searchable P&ID-tag dropdown opened by the safeguard emoji."""

    committed = pyqtSignal()
    _NONE_LABEL = "— Inget objekt —"

    def __init__(self, db, sg_id, current_tag, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.db = db
        self._sg_id = sg_id
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setStyleSheet(
            "ArchivedSafeguardObjectPopup { background:#FFFFFF; "
            "border:1px solid #CFD1CE; border-radius:6px; }")
        self.setMinimumWidth(220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)
        title = QLabel("<b>Objekt</b>")
        title.setStyleSheet("font-size:11px; color:#8D9299;")
        outer.addWidget(title)

        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.setFixedHeight(CONFIG['H_BTN_SMALL'])
        outer.addWidget(self._combo)

        self._populate(current_tag)
        self._combo.activated.connect(lambda _i: self._commit())
        self._combo.lineEdit().editingFinished.connect(self._commit)
        self._combo.setFocus()

    def _populate(self, current_tag):
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem(self._NONE_LABEL, '')
        tags = _equipment_tags_for_types(self.db)
        for tag in tags:
            self._combo.addItem(tag, tag)
        if current_tag and current_tag not in tags:
            self._combo.addItem(current_tag, current_tag)
        idx = self._combo.findData(current_tag or '')
        self._combo.setCurrentIndex(idx if idx >= 0 else 0)
        completer = QCompleter(tags, self._combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._combo.setCompleter(completer)
        self._combo.blockSignals(False)

    def _commit(self):
        tag = self._combo.currentText().strip()
        if not tag or tag == self._NONE_LABEL:
            self.db.set_safeguard_tag(self._sg_id, '', '')
        else:
            comp_type = _resolve_comp_type_for_tag(self.db, tag)
            self.db.set_safeguard_tag(self._sg_id, tag, comp_type)
        self.committed.emit()
