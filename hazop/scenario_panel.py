#!/usr/bin/env python3
"""HAZOP scenario table panel and its dialogs/delegates — split out of
hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer"."""

import re
import json
import logging
import weakref
from html import escape as _html_escape
from functools import partial
from PyQt6 import sip

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter,
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QEvent, QMimeData, QPoint, QPointF, QRect, QSize, QTimer,
)
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter,
    QPen, QPixmap, QTextCharFormat, QTextCursor,
)

from constants import CAUSE_T, CONS_T, SG_T, SG_TYPES, CONFIG
from database import Database, DEFAULT_MATRIX, get_matrix, risk_info, parse_tag_refs
from pid_viewer import _icon, FREQ_LABELS, freq_to_idx, MODE_PICK_REF_TAG
from ui_helpers import (
    freq_axis_label, freq_axis_label_full, cons_axis_label, cons_axis_label_full,
    _lookup_comp_type_for_tag,
    _draw_text_with_bold_tags, standard_cause_options,
    total_freq_reduction, CHAIN_ITEMS, build_consequence_text, parse_chain_from_json,
    find_bold_tag_at_position, _equipment_type_options,
    add_mini_popup_close_button,
)

MAX_GROUP_OBJECTS = 20

# Scenario mixes ordinary QTableWidget items with custom-painted cells. Keep
# the selected-cell treatment explicit so every one of those paths uses the
# same neutral, flat overlay instead of the application-wide blue accent.
_SCENARIO_SELECTION_BG = '#D9DBD8'
_SCENARIO_SELECTION_FG = '#17191C'

# Risk values are rendered as compact colour bars, matching the visual
# footprint of the enabler summary button. The colour still comes from the
# configured risk matrix; only the presentation changes.
_RISK_BAR_HEIGHT = 22
_RISK_BAR_MARGIN_X = 2
_RISK_BAR_MARGIN_Y = 1
_RISK_BAR_RADIUS = 5
_RISK_COLUMN_DEFAULT_WIDTH = 52

from tree_panel import CauseTagPopup, RRFPopup, FrequencyPickerPopup


class _ObjectTagActionPopup(QDialog):
    """Compact, explicit actions for a bold object tag in a HAZOP cell.

    A bold tag is an object reference, not ordinary prose.  Keeping the two
    possible actions separate prevents the old ambiguity where typing a tag
    could either rename the current P&ID object or silently connect another
    one with the same name.
    """
    object_selected = pyqtSignal(object)  # equipment_catalog row as dict
    rename_requested = pyqtSignal(str)
    type_change_requested = pyqtSignal(str)

    def __init__(self, db, equipment, parent=None):
        super().__init__(parent)
        self._db = db
        self._equipment = dict(equipment or {})
        self.setWindowTitle('Redigera objekt')
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName('objectTagActionPopup')
        self.setMinimumWidth(240)
        self.setStyleSheet(
            'QWidget#objectTagActionPopup{background:#FFFFFF;'
            'border:1px solid #4B5563;border-radius:3px;}'
            'QLabel{border:none;color:#17191C;}'
            'QComboBox,QLineEdit{border:1px solid #B8BDC4;border-radius:2px;'
            'padding:2px 5px;background:#FFFFFF;color:#17191C;}'
            'QPushButton{border:1px solid #8D9299;border-radius:2px;'
            'padding:3px 7px;background:#F5F5F3;color:#17191C;}'
            'QPushButton:hover{background:#E8E9E6;}')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 8)
        layout.setSpacing(5)

        heading = QLabel('Objekt')
        heading.setStyleSheet('font-size:10px;font-weight:bold;')
        layout.addWidget(heading)
        tag = str(self._equipment.get('tag') or 'Objekt')
        type_ = str(self._equipment.get('equipment_type') or 'Okänd typ')
        current = QLabel(tag)
        current.setStyleSheet('font-size:11px;font-weight:bold;')
        layout.addWidget(current)
        detail = QLabel(type_)
        detail.setStyleSheet('font-size:9px;color:#6B7280;')
        layout.addWidget(detail)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self._choose_btn = QPushButton('Byt objekt')
        self._rename_btn = QPushButton('Ändra namn')
        actions.addWidget(self._choose_btn)
        actions.addWidget(self._rename_btn)
        layout.addLayout(actions)

        self._type_btn = QPushButton('Ändra objekttyp')
        layout.addWidget(self._type_btn)

        self._object_combo = QComboBox()
        self._object_combo.addItem('Välj objekt från objektdatabas …', None)
        try:
            for candidate in db.equipment_items():
                candidate = dict(candidate)
                candidate_tag = str(candidate.get('tag') or '').strip()
                if not candidate_tag:
                    continue
                label = candidate_tag
                if candidate.get('equipment_type'):
                    label += f"  ·  {candidate['equipment_type']}"
                self._object_combo.addItem(label, candidate)
        except Exception:
            pass
        self._object_combo.hide()
        layout.addWidget(self._object_combo)

        self._rename_row = QWidget()
        rename_layout = QHBoxLayout(self._rename_row)
        rename_layout.setContentsMargins(0, 0, 0, 0)
        rename_layout.setSpacing(4)
        self._rename_edit = QLineEdit(tag)
        self._rename_edit.setPlaceholderText('Ny tagg')
        self._rename_save = QPushButton('Spara namn')
        rename_layout.addWidget(self._rename_edit, 1)
        rename_layout.addWidget(self._rename_save)
        self._rename_row.hide()
        layout.addWidget(self._rename_row)

        self._type_row = QWidget()
        type_layout = QHBoxLayout(self._type_row)
        type_layout.setContentsMargins(0, 0, 0, 0)
        type_layout.setSpacing(4)
        self._type_combo = QComboBox()
        self._type_combo.addItems(_equipment_type_options(db))
        self._type_combo.setCurrentText(type_ if type_ != 'Okänd typ' else '')
        self._type_save = QPushButton('Spara typ')
        type_layout.addWidget(self._type_combo, 1)
        type_layout.addWidget(self._type_save)
        self._type_row.hide()
        layout.addWidget(self._type_row)

        hint = QLabel('Namnbyte uppdaterar objektet på P&ID och i HAZOP.')
        hint.setWordWrap(True)
        hint.setStyleSheet('font-size:9px;color:#6B7280;')
        self._rename_hint = hint
        hint.hide()
        layout.addWidget(hint)

        self._choose_btn.clicked.connect(self._show_object_picker)
        self._rename_btn.clicked.connect(self._show_rename_editor)
        self._type_btn.clicked.connect(self._show_type_picker)
        self._object_combo.activated.connect(self._select_object)
        self._rename_save.clicked.connect(self._request_rename)
        self._rename_edit.returnPressed.connect(self._request_rename)
        self._type_save.clicked.connect(self._request_type_change)
        add_mini_popup_close_button(self)

    def _show_object_picker(self):
        self._object_combo.setVisible(not self._object_combo.isVisible())
        if self._object_combo.isVisible():
            self._rename_row.hide()
            self._rename_hint.hide()
            self._type_row.hide()
            self._object_combo.setFocus()
        self.adjustSize()

    def _show_rename_editor(self):
        self._rename_row.setVisible(not self._rename_row.isVisible())
        if self._rename_row.isVisible():
            self._object_combo.hide()
            self._type_row.hide()
            self._rename_hint.show()
            self._rename_edit.setFocus()
            self._rename_edit.selectAll()
        else:
            self._rename_hint.hide()
        self.adjustSize()

    def _show_type_picker(self):
        self._type_row.setVisible(not self._type_row.isVisible())
        if self._type_row.isVisible():
            self._object_combo.hide()
            self._rename_row.hide()
            self._rename_hint.hide()
            self._type_combo.setFocus()
        self.adjustSize()

    def _select_object(self, index):
        equipment = self._object_combo.itemData(index)
        if not equipment:
            return
        self.object_selected.emit(dict(equipment))
        self.close()

    def _request_rename(self):
        tag = self._rename_edit.text().strip().upper()
        if not tag:
            return
        self.rename_requested.emit(tag)
        self.close()

    def _request_type_change(self):
        equipment_type = self._type_combo.currentText().strip()
        if not equipment_type:
            return
        self.type_change_requested.emit(equipment_type)
        self.close()


class _BoldTagLineEdit(QLineEdit):
    """Inline editor that keeps known P&ID tags visibly bold.

    QLineEdit has no rich-text editing mode.  The normal editor remains fully
    editable, while the known tag tokens are painted once more with a bold
    font over Qt's normal text painting.  This preserves the visual contract
    of the Scenario table during editing without changing the stored text or
    cursor behaviour.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bold_tags = []

    def set_bold_tags(self, tags):
        self._bold_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._bold_tags or not self.text():
            return
        painter = QPainter(self)
        painter.setClipRect(self.contentsRect())
        font = QFont(self.font())
        font.setBold(True)
        fm = QFontMetrics(font)
        normal_fm = QFontMetrics(self.font())
        text = self.text()
        # QLineEdit does not expose a public horizontalScrollBar() in
        # PyQt6.  Derive the current text origin from the visible caret;
        # this remains correct when Qt has horizontally scrolled the text.
        cursor_x = self.cursorRect().x()
        x0 = cursor_x - normal_fm.horizontalAdvance(
            text[:self.cursorPosition()])
        y = self.contentsRect().top()
        h = self.contentsRect().height()
        for tag in self._bold_tags:
            start = 0
            while True:
                pos = text.casefold().find(tag.casefold(), start)
                if pos < 0:
                    break
                x = x0 + normal_fm.horizontalAdvance(text[:pos])
                painter.setFont(font)
                painter.drawText(QRect(x, y, fm.horizontalAdvance(tag) + 2, h),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 text[pos:pos + len(tag)])
                start = pos + len(tag)
        painter.end()


class _BoldTagTextEdit(QTextEdit):
    """Shared multiline scenario editor.

    The table paints wrapped cell text itself, so using a one-line
    ``QLineEdit`` as the delegate editor made the text jump or clip as soon
    as a row became taller.  This editor keeps the same plain-text API used
    by the existing delegate/popup code while letting Qt wrap the live text
    in the same cell rectangle.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bold_tags = []
        self._completer = None
        self._tag_completer = None
        self._tag_completion_serial = 0
        self._tag_completion_range = None
        self._completion_serial = 0
        self._completion_range = None
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setFrameStyle(QFrame.Shape.NoFrame)
        self.setContentsMargins(0, 0, 0, 0)
        self.document().setDocumentMargin(0)

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText('' if text is None else str(text))
        self._apply_bold_formats()

    def deselect(self):
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def selectedText(self):
        return self.textCursor().selectedText()

    def cursorPosition(self):
        return self.textCursor().position()

    def setCursorPosition(self, position):
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(int(position), len(self.toPlainText()))))
        self.setTextCursor(cursor)

    def cursorPositionAt(self, point):
        return self.cursorForPosition(point).position()

    def setCompleter(self, completer):
        self._completer = completer
        completer.setWidget(self)
        popup = completer.popup()
        popup.setWindowFlag(Qt.WindowType.Popup, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        popup.setMinimumWidth(260)
        popup.setStyleSheet(
            "QAbstractItemView { background:#ffffff; color:#17191C; "
            "border:1px solid #8D9299; padding:2px; }"
            "QAbstractItemView::item { padding:3px 6px; }"
        )
        try:
            completer.activated.disconnect()
        except (TypeError, RuntimeError):
            pass
        completer.activated.connect(self._insert_completion)

    def completer(self):
        return self._completer

    def setTagCompleter(self, completer):
        """Attach the shared delayed P&ID-tag popup to this text editor."""
        self._tag_completer = completer
        completer.setWidget(self)
        # A delegate editor lives inside QTableWidget's viewport.  The
        # default completer view can consequently be clipped/painted behind
        # the table on some Windows styles.  Make it an explicit non-modal
        # popup window so the tag suggestions are visible above the table.
        popup = completer.popup()
        popup.setWindowFlag(Qt.WindowType.Popup, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        popup.setMinimumWidth(180)
        popup.setStyleSheet(
            "QAbstractItemView { background:#ffffff; color:#17191C; "
            "border:1px solid #8D9299; padding:2px; }"
            "QAbstractItemView::item { padding:3px 6px; }"
        )
        try:
            completer.activated.disconnect()
        except (TypeError, RuntimeError):
            pass
        completer.activated.connect(self._insert_tag_completion)
        self.textChanged.connect(self._schedule_tag_completion)

    def _schedule_tag_completion(self):
        self._tag_completion_serial += 1
        serial = self._tag_completion_serial
        editor_ref = weakref.ref(self)
        QTimer.singleShot(220, lambda s=serial, ref=editor_ref:
                          _show_tag_completion_if_alive(ref, s))

    def _show_tag_completion(self, serial):
        try:
            if serial != self._tag_completion_serial or self._tag_completer is None:
                return
            cursor = self.textCursor()
            pos = cursor.position()
            text = self.toPlainText()
            start = pos
            while start > 0 and re.match(r'[A-Za-z0-9_.-]', text[start - 1]):
                start -= 1
            token = text[start:pos]
            min_length = int(getattr(self, '_tag_completion_min_length', 2))
            if len(token) < min_length:
                self._tag_completer.popup().hide()
                return
            self._tag_completion_range = (start, pos)
            self._tag_completer.setCompletionPrefix(token)
            if self._tag_completer.completionCount() <= 0:
                self._tag_completer.popup().hide()
                return
            if self._completer is not None:
                self._completer.popup().hide()
            self._tag_completer.complete(self.cursorRect())
            # complete() schedules the view show internally.  Raise it after
            # that call as the editor is embedded in a table viewport.
            popup = self._tag_completer.popup()
            popup.raise_()
        except RuntimeError:
            # A deferred completion can outlive the delegate editor when a
            # rebuild, focus change, or popup closes the cell editor.
            return

    def _insert_tag_completion(self, completion):
        if not self._tag_completion_range:
            return
        start, end = self._tag_completion_range
        cursor = self.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
        cursor.insertText(str(completion))
        caret_pos = cursor.position()
        self.setTextCursor(cursor)
        self._tag_completion_range = None
        if self._tag_completer is not None:
            self._tag_completer.popup().hide()
        # QCompleter may still own keyboard focus while its activated signal
        # is being delivered. Return focus after that event so the user can
        # continue typing immediately after the inserted P&ID tag.
        QTimer.singleShot(0, lambda ed=weakref.ref(self), pos=caret_pos:
                          _resume_tag_editing(ed, pos))
        self._refresh_pid_tag_bolding()

    def _accept_visible_tag_completion(self):
        """Accept the selected tag when Enter is delivered to the editor.

        The popup is deliberately non-activating so focus remains in the
        inline editor.  On some Windows styles that means Enter reaches the
        editor's event filter instead of emitting QCompleter.activated from
        the popup.  Consume the selected completion here before the normal
        Enter handling commits/closes the table editor.
        """
        completer = self._tag_completer
        if completer is None:
            return False
        popup = completer.popup()
        if not popup.isVisible():
            return False
        index = popup.currentIndex()
        completion = index.data(Qt.ItemDataRole.DisplayRole) if index.isValid() else None
        if not completion:
            completion = completer.currentCompletion()
        if not completion:
            return False
        self._insert_tag_completion(completion)
        return True

    def _insert_completion(self, completion):
        cursor = self.textCursor()
        if self._completion_range is not None:
            start, end = self._completion_range
            cursor.setPosition(start)
            cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
        cursor.insertText(str(completion))
        self.setTextCursor(cursor)
        self._completion_range = None

    def _schedule_completion(self):
        """Show the ordinary text-history completer after user typing.

        QCompleter natively knows how to follow QLineEdit, but this project
        uses QTextEdit for wrapped scenario cells. Drive the popup explicitly
        so consequence history (and standard-cause suggestions) is visible
        in the same editor instead of only being attached in memory.
        """
        if self._completer is None:
            return
        self._completion_serial += 1
        serial = self._completion_serial
        editor_ref = weakref.ref(self)
        QTimer.singleShot(120, lambda s=serial, ref=editor_ref:
                          _show_completion_if_alive(ref, s))

    def _show_completion(self, serial):
        try:
            if serial != self._completion_serial or self._completer is None:
                return
            if self._tag_completer is not None and self._tag_completer.popup().isVisible():
                return
            cursor = self.textCursor()
            end = cursor.position()
            prefix = self.toPlainText()[:end]
            if not prefix.strip():
                self._completer.popup().hide()
                return
            self._completion_range = (0, end)
            self._completer.setCompletionPrefix(prefix)
            if self._completer.completionCount() <= 0:
                self._completer.popup().hide()
                return
            self._completer.complete(self.cursorRect())
            self._completer.popup().raise_()
        except RuntimeError:
            return

    def set_bold_tags(self, tags):
        self._bold_tags = [str(tag).strip() for tag in (tags or [])
                           if str(tag).strip()]
        self._apply_bold_formats()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        self._schedule_completion()
        # Apply after QTextEdit has completed the document mutation. Doing
        # this from textChanged can re-enter Qt's layout engine while it is
        # still processing the key event.
        self._apply_bold_formats()
        self._refresh_pid_tag_bolding()

    def _refresh_pid_tag_bolding(self):
        """Bold complete P&ID tag tokens as text is entered."""
        matcher = getattr(self, '_tag_matcher', None)
        if not callable(matcher):
            return
        try:
            matches = matcher(self.toPlainText())
        except Exception:
            matches = []
        tags = list(dict.fromkeys(self._bold_tags + list(matches or [])))
        if tags != self._bold_tags:
            self.set_bold_tags(tags)

    def _apply_bold_formats(self):
        if not self._bold_tags:
            return
        text = self.toPlainText()
        if not text:
            return
        # Reapplying character formats rebuilds parts of QTextEdit's layout.
        # Preserve the complete cursor state and the current scroll offsets,
        # not only the caret position, so the text cannot visibly jump while
        # the user types or a P&ID tag is recognised.
        current_cursor = self.textCursor()
        cursor_pos = current_cursor.position()
        cursor_anchor = current_cursor.anchor()
        v_scroll = self.verticalScrollBar().value()
        h_scroll = self.horizontalScrollBar().value()
        cursor = QTextCursor(self.document())
        normal = QTextCharFormat()
        normal.setFontWeight(QFont.Weight.Normal)
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setCharFormat(normal)
        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        folded = text.casefold()
        for tag in self._bold_tags:
            start = 0
            needle = tag.casefold()
            while True:
                pos = folded.find(needle, start)
                if pos < 0:
                    break
                cursor.setPosition(pos)
                cursor.setPosition(pos + len(tag), QTextCursor.MoveMode.KeepAnchor)
                cursor.setCharFormat(bold)
                start = pos + len(tag)
        restored_cursor = QTextCursor(self.document())
        restored_cursor.setPosition(cursor_anchor)
        restored_cursor.setPosition(cursor_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(restored_cursor)
        self.verticalScrollBar().setValue(v_scroll)
        self.horizontalScrollBar().setValue(h_scroll)

def _show_tag_completion_if_alive(editor_ref, serial):
    """Run a delayed completion only while its Qt editor still exists."""
    editor = editor_ref()
    if editor is None or sip.isdeleted(editor):
        return
    editor._show_tag_completion(serial)


def _show_completion_if_alive(editor_ref, serial):
    """Run a delayed history completion only while its editor still exists."""
    editor = editor_ref()
    if editor is None or sip.isdeleted(editor):
        return
    editor._show_completion(serial)


def _resume_tag_editing(editor_ref, position):
    """Restore the text editor after accepting a tag-completer item."""
    editor = editor_ref()
    if editor is None or sip.isdeleted(editor):
        return
    try:
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        cursor = editor.textCursor()
        cursor.clearSelection()
        cursor.setPosition(max(0, min(position, len(editor.toPlainText()))))
        editor.setTextCursor(cursor)
    except RuntimeError:
        return


class RiskMatrixPopup(QDialog):
    """Popup risk matrix matching the configured format in Settings.

    Optionally (2026-08-26, see NOTES.md "Flytta konsekvenskategori till
    riskmatrisen") also hosts the per-category consequence-level picker
    that used to live behind a separate "📊" badge on the KON cell —
    pass `db`/`cons_id` to show it. Frequency stays a single value taken
    from the cause (unchanged) and is never set per category here; only
    each category's OWN severity is editable, and every category with a
    severity set gets a small marker drawn directly on the matrix cell
    it now occupies (same frequency column, shared across categories —
    only the row differs), so their positions are visible together."""

    selection_made = pyqtSignal(int, int)   # freq_value, cons_value
    category_changed = pyqtSignal()         # a per-category severity was set/cleared

    def __init__(self, current_freq: int, current_cons: int, parent=None,
                 db=None, cons_id=None, final_consequence=False):
        super().__init__(parent)
        self.setWindowTitle("Välj risknivå")
        # Qt.WindowType.Popup (2026-08-26, see NOTES.md): same window type
        # QMenu/QComboBox use for their own dropdowns -- Qt closes it
        # automatically the instant a click lands outside its geometry, on
        # top of the existing Cancel button and Escape handling below. Shown
        # via show() now, not exec() (see the call site) -- a Popup grabs
        # the mouse/keyboard itself and isn't meant to run its own nested
        # modal event loop; nothing here relied on exec()'s return value,
        # every outcome already flows through the selection_made signal.
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("riskMatrixPopup")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            "QWidget#riskMatrixPopup{background:#FFFFFF;"
            "border:1px solid #4B5563;border-radius:3px;}")

        cfg       = get_matrix()
        n_cons    = cfg.get('rows', 5)
        n_freq    = cfg.get('cols', 7)
        x_codes   = cfg.get('x_codes', [f'F{c-1}' for c in range(n_freq)])
        y_codes   = cfg.get('y_codes', [str(r+1) for r in range(n_cons)])
        x_lbls    = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        y_lbls    = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors         = cfg.get('cell_colors', [])
        cell_lbl       = cfg.get('cell_labels', [])
        cell_fg_colors = cfg.get('cell_fg_colors', [])
        freq_on_x = cfg.get('x_axis', 'frequency') == 'frequency'
        x_rev     = cfg.get('x_reversed', False)
        y_rev     = cfg.get('y_reversed', False)

        self._db          = db
        self._cons_id     = cons_id
        self._category_mode = db is not None and cons_id is not None
        self._final_consequence_mode = bool(final_consequence and self._category_mode)
        self._current_freq = current_freq
        self._current_cons = current_cons
        self._n_cons      = n_cons
        self._freq_on_x   = freq_on_x
        self._x_rev       = x_rev
        self._y_rev       = y_rev
        self._grid_buttons = {}   # (freq_val, cons_val) -> (QPushButton, base label text)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        hdr_text = ("Risker efter barriärer: välj egen nivå per kategori "
                    "(standard = Risk före barriär)"
                    if self._final_consequence_mode else
                    "Klicka på en cell för att sätta risknivå")
        hdr = QLabel(hdr_text)
        hdr.setStyleSheet("font-weight:bold; font-size:11px; padding:2px;")
        outer.addWidget(hdr)

        # Keep the actual configured axis value visible when the popup is
        # opened. The table stores numeric F/C ordinals, while the user may
        # have configured arbitrary short codes (for example A..E or 1..5).
        # Showing both code and description makes the current cell
        # unambiguous when the matrix is rotated or used for the final risk.
        self._current_value_label = QLabel(
            f"Aktuellt: {freq_axis_label_full(current_freq)}  ·  "
            f"{cons_axis_label_full(current_cons)}")
        self._current_value_label.setObjectName('currentRiskValue')
        self._current_value_label.setStyleSheet(
            "font-size:9px; color:#4B5563; padding:1px 2px;")
        self._current_value_label.setToolTip(
            "Numeriskt/konfigurerat värde för den markerade riskcellen")
        outer.addWidget(self._current_value_label)
        self._shortcut_buffer = ''
        self._shortcut_status = None
        if self._category_mode:
            self._shortcut_status = QLabel()
            self._shortcut_status.setStyleSheet(
                "font-size:9px; color:#6B7280; padding:1px 2px;")
            self._set_shortcut_status()
            outer.addWidget(self._shortcut_status)

        grid = QGridLayout()
        grid.setSpacing(0)
        # Matrix-cell labels are user-editable in HAZOP Preparation. Keep
        # enough width to show the actual edited text instead of the old
        # four-character abbreviation, which made a saved text change look
        # as though the popup had not refreshed.
        cell_width = 64

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons
            col_codes, row_codes = x_codes, y_codes
            col_lbls, row_lbls = x_lbls, y_lbls
            corner_txt = "C \\ F"
        else:
            n_dcols, n_drows = n_cons, n_freq
            col_codes, row_codes = y_codes, x_codes
            col_lbls, row_lbls = y_lbls, x_lbls
            corner_txt = "F \\ C"

        # Corner
        corner = QLabel(corner_txt)
        corner.setStyleSheet("font-size:9px; color:#666;")
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setFixedWidth(cell_width)
        grid.addWidget(corner, 0, 0)

        # Column headers — respect x_rev
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            full   = col_lbls[data_c] if data_c < len(col_lbls) else ''
            code   = col_codes[data_c] if data_c < len(col_codes) else str(data_c)
            lbl = QLabel(code)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFixedWidth(cell_width)
            lbl.setStyleSheet("font-size:9px; font-weight:bold; padding:1px;")
            lbl.setToolTip(f"{code} — {full}" if full else code)
            grid.addWidget(lbl, 0, c + 1)

        # Rows — respect y_rev
        for r in range(n_drows):
            if y_rev:
                disp_r = r
            else:
                disp_r = n_drows - 1 - r

            # Row header
            full_r = row_lbls[disp_r] if disp_r < len(row_lbls) else ''
            code_r = row_codes[disp_r] if disp_r < len(row_codes) else str(disp_r)
            rl = QLabel(code_r)
            rl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rl.setStyleSheet("font-size:9px; font-weight:bold; padding-right:4px;")
            rl.setToolTip(f"{code_r} — {full_r}" if full_r else code_r)
            rl.setFixedWidth(cell_width)
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

                is_current = (not self._category_mode and
                              freq_val == current_freq and cons_val == current_cons)
                border = '3px solid #000' if is_current else '0px'

                btn = QPushButton(lbl)
                btn.setFixedSize(cell_width, 32)
                btn.setToolTip(
                    f"{freq_axis_label_full(freq_val)}  ·  "
                    f"{cons_axis_label_full(cons_val)}  →  {lbl}")
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
                btn.setProperty('risk_color', color)
                btn.setProperty('risk_fg', fg)
                self._grid_buttons[(freq_val, cons_val)] = (btn, lbl)

        if self._category_mode and self._freq_on_x:
            # Consequence is the Y-axis: place the category selector directly
            # to the left of the matrix so each C row is physically aligned.
            category_host = QWidget()
            category_layout = QVBoxLayout(category_host)
            category_layout.setContentsMargins(0, 0, 4, 0)
            category_layout.setSpacing(0)
            self._build_category_section(category_layout, inline=True)
            matrix_row = QHBoxLayout()
            matrix_row.setSpacing(4)
            matrix_row.addWidget(category_host)
            matrix_row.addLayout(grid)
            outer.addLayout(matrix_row)
        else:
            outer.addLayout(grid)
            if self._category_mode:
                self._build_category_section(outer)

        self.adjustSize()
        add_mini_popup_close_button(self)
        QTimer.singleShot(0, self.setFocus)

    @staticmethod
    def _unique_category_prefix_lengths(categories):
        """Return the shortest case-insensitive unique prefix per category.

        The prefix is also the keyboard shortcut shown in bold next to each
        category. Thus Person becomes P when it is unambiguous, while
        Person/Process become Pe/Pr.
        """
        names = [str(category.get('name') or '').strip()
                 for category in categories]
        folded = [name.casefold() for name in names]
        lengths = {}
        for index, name in enumerate(names):
            if not name:
                lengths[index] = 0
                continue
            length = len(name)
            for candidate_length in range(1, len(name) + 1):
                prefix = folded[index][:candidate_length]
                matches = sum(other.startswith(prefix) for other in folded)
                if matches == 1:
                    length = candidate_length
                    break
            lengths[index] = length
        return lengths

    def _category_label_html(self, category):
        """Render a category with only its unique shortcut prefix in bold."""
        name = str(category.get('name') or '').strip()
        prefix_length = self._category_prefix_lengths.get(
            category.get('id'), len(name))
        prefix = name[:prefix_length]
        return f"<b>{_html_escape(prefix)}</b>{_html_escape(name[prefix_length:])}"

    def _set_shortcut_status(self, text=None):
        label = getattr(self, '_shortcut_status', None)
        if label is None:
            return
        if text is None:
            text = ('Snabbval: kategori-prefix + konsekvenstecken + Enter '
                    '(t.ex. P5)')
        label.setText(text)

    def _build_category_section(self, outer, inline=False):
        """Build the per-category picker on the same axis as the matrix.

        Consequence is the matrix Y-axis when frequency is on X, so each
        category gets a vertical C1..Cn column.  When the matrix is rotated,
        consequence is the X-axis and the category controls run horizontally.
        Cell dimensions match the matrix cells, making the orientation and
        order immediately recognisable.  Severity descriptions are exposed
        through each button's tooltip.
        """
        cats  = [dict(r) for r in self._db.consequence_categories()]
        if not cats:
            return
        prefix_lengths = self._unique_category_prefix_lengths(cats)
        self._category_prefix_lengths = {
            category['id']: prefix_lengths[index]
            for index, category in enumerate(cats)
        }
        self._category_prefixes = {
            category['id']: str(category.get('name') or '').strip()[
                :prefix_lengths[index]]
            for index, category in enumerate(cats)
        }
        saved = {r['category_id']: r['severity']
                 for r in self._db.get_consequence_severities(self._cons_id)}
        final_saved = ({r['category_id']: r['severity']
                        for r in self._db.get_final_consequence_severities(self._cons_id)}
                       if self._final_consequence_mode else {})
        self._cats = cats
        self._cat_base = {c['id']: saved.get(c['id'], 0) for c in cats}
        self._cat_sel = ({c['id']: final_saved.get(c['id'], 0) for c in cats}
                         if self._final_consequence_mode else dict(self._cat_base))
        self._cat_buttons = {}
        severity_defs = self._db.get_severity_definitions()

        if not inline:
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color:#E2E3E1;")
            outer.addWidget(sep)

            hdr2 = QLabel("Konsekvens per kategori (frekvens hämtas från orsaken):")
            hdr2.setStyleSheet("font-size:9px; color:#555;")
            outer.addWidget(hdr2)

        def add_button(cid, s, add_widget):
            cbtn = QPushButton(cons_axis_label(s))
            cbtn.setFixedSize(50, 32)
            cbtn.setCheckable(True)
            cbtn.setChecked(self._cat_sel.get(cid, 0) == s)
            cbtn.setStyleSheet(self._cat_bstyle(cbtn.isChecked()))
            cbtn.setAutoDefault(False)
            cbtn.setDefault(False)
            cbtn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            cat_desc = severity_defs.get(s, {}).get(cid, '')
            tip = f"{cons_axis_label(s)}"
            if cat_desc:
                tip += f": {cat_desc}"
            if self._final_consequence_mode:
                base_sev = self._cat_base.get(cid, 0)
                if base_sev:
                    tip += (f"\nStandard: {cons_axis_label(base_sev)} "
                            "från Risk före barriär")
                else:
                    tip += "\nSätt först konsekvensnivån i Risk före barriär"
                    cbtn.setEnabled(False)
            cbtn.setToolTip(tip)
            cbtn.clicked.connect(lambda _, ci=cid, sv=s: self._toggle_category(ci, sv))
            self._cat_buttons[(cid, s)] = cbtn
            add_widget(cbtn)

        if self._freq_on_x:
            # Consequence is Y: severity values are rows, one aligned vertical
            # column per category.
            grid = QGridLayout(); grid.setSpacing(0)
            corner = QLabel('C')
            corner.setFixedSize(50, 16)
            corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            corner.setStyleSheet("font-size:9px; font-weight:bold; color:#555;")
            grid.addWidget(corner, 0, 0)
            for col, cat in enumerate(cats, 1):
                name_l = QLabel(self._category_label_html(cat))
                name_l.setTextFormat(Qt.TextFormat.RichText)
                name_l.setFixedSize(50, 16)
                name_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
                name_l.setStyleSheet("font-size:8px; font-weight:normal;")
                name_l.setToolTip(
                    f"{cat['name']} (snabbval: "
                    f"{self._category_prefixes.get(cat['id'], '')})")
                grid.addWidget(name_l, 0, col)
            severity_order = (range(1, self._n_cons + 1) if self._y_rev
                              else range(self._n_cons, 0, -1))
            for row, s in enumerate(severity_order, 1):
                axis_l = QLabel(cons_axis_label(s))
                axis_l.setFixedSize(50, 32)
                axis_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
                axis_l.setStyleSheet("font-size:8px; font-weight:bold; color:#555;")
                grid.addWidget(axis_l, row, 0)
                for col, cat in enumerate(cats, 1):
                    add_button(cat['id'], s,
                               lambda widget, r=row, c=col: grid.addWidget(widget, r, c))
            outer.addLayout(grid)
        else:
            # Consequence is X: severity values run horizontally per category.
            hdr = QHBoxLayout(); hdr.setSpacing(0); hdr.setContentsMargins(0, 0, 0, 0)
            pad = QLabel(); pad.setFixedWidth(70); hdr.addWidget(pad)
            severity_order = (range(self._n_cons, 0, -1) if self._x_rev
                              else range(1, self._n_cons + 1))
            for s in severity_order:
                axis_l = QLabel(cons_axis_label(s)); axis_l.setFixedSize(50, 22)
                axis_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
                axis_l.setStyleSheet("font-size:8px; font-weight:bold; color:#555;")
                axis_l.setToolTip(cons_axis_label(s))
                hdr.addWidget(axis_l)
            outer.addLayout(hdr)
            for cat in cats:
                row_l = QHBoxLayout(); row_l.setSpacing(0); row_l.setContentsMargins(0, 0, 0, 0)
                name_l = QLabel(self._category_label_html(cat))
                name_l.setTextFormat(Qt.TextFormat.RichText)
                name_l.setFixedWidth(70)
                name_l.setStyleSheet("font-size:9px; font-weight:normal;")
                name_l.setToolTip(
                    f"{cat['name']} (snabbval: "
                    f"{self._category_prefixes.get(cat['id'], '')})")
                row_l.addWidget(name_l)
                for s in severity_order:
                    add_button(cat['id'], s, row_l.addWidget)
                outer.addLayout(row_l)

        self._refresh_category_markers()

    @staticmethod
    def _cat_bstyle(selected: bool) -> str:
        if selected:
            return ("QPushButton{background:#2F5FD0;color:white;"
                    "border:2px solid #2F5FD0;border-radius:0px;"
                    "font-size:8px;font-weight:bold;}"
                    "QPushButton:hover{background:#3D6BD8;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;"
                "border:1px solid #CFD1CE;border-radius:0px;font-size:8px;}"
                "QPushButton:hover{background:#E8E9E6;border:1px solid #B3B7B2;}")

    def _toggle_category(self, cat_id, sev):
        cur = self._cat_sel.get(cat_id, 0)
        if self._final_consequence_mode:
            # No saved final level means the regular before-barrier severity
            # remains effective. Clicking that same default is therefore a
            # no-op; clicking an alternative creates an explicit override.
            base_sev = self._cat_base.get(cat_id, 0)
            new_sev = 0 if cur == sev or (not cur and base_sev == sev) else sev
        else:
            new_sev = 0 if cur == sev else sev
        self._cat_sel[cat_id] = new_sev
        for s in range(1, self._n_cons + 1):
            btn = self._cat_buttons.get((cat_id, s))
            if btn is not None:
                checked = (s == new_sev)
                btn.setChecked(checked)
                btn.setStyleSheet(self._cat_bstyle(checked))
        if self._final_consequence_mode:
            self._db.set_final_consequence_severity(self._cons_id, cat_id, new_sev)
        else:
            self._db.set_consequence_severity(self._cons_id, cat_id, new_sev)
        self._refresh_category_markers()
        self.category_changed.emit()

    def _refresh_category_markers(self):
        """Overlay each category's current severity onto the matrix
        cell it now occupies — same (shared, from-the-cause) frequency
        column for every category, one marker per row that has one."""
        marks_by_cons_val = {}
        for cat in getattr(self, '_cats', []):
            sev = self._cat_sel.get(cat['id'], 0)
            if self._final_consequence_mode and not sev:
                sev = self._cat_base.get(cat['id'], 0)
            if sev > 0:
                marks_by_cons_val.setdefault(sev, []).append(cat['name'][:3])
        for (fv, cv), (btn, base) in self._grid_buttons.items():
            marks = marks_by_cons_val.get(cv) if fv == self._current_freq else None
            btn.setText(base + "\n" + ",".join(marks) if marks else base)
            if self._category_mode:
                color = btn.property('risk_color') or '#27ae60'
                fg = btn.property('risk_fg') or '#ffffff'
                border = '2px solid #17191C' if marks else '0px'
                btn.setStyleSheet(
                    f"QPushButton{{background:{color}; color:{fg};"
                    f"font-size:8px; font-weight:bold; border:{border};"
                    f"border-radius:0px; margin:0px;}}"
                    f"QPushButton:hover{{border:2px dashed #2f6fed;}}")
        self.adjustSize()

    def _pick(self, freq, cons):
        if self._category_mode:
            # Frequency comes from the cause and severity must be chosen on
            # an explicit category row below. The grid is only a visual map
            # in this mode; clicking it may not create a fallback assessment.
            return
        self.selection_made.emit(freq, cons)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if not self._category_mode:
            super().keyPressEvent(event)
            return

        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._apply_category_shortcut():
                event.accept()
            else:
                self._set_shortcut_status(
                    'Snabbvalet känns inte igen – skriv prefix + nivå, t.ex. P5')
                self._shortcut_buffer = ''
                event.accept()
            return

        if event.key() == Qt.Key.Key_Backspace:
            self._shortcut_buffer = self._shortcut_buffer[:-1]
            self._set_shortcut_status(
                f'Snabbval: {self._shortcut_buffer.upper()}'
                if self._shortcut_buffer else None)
            event.accept()
            return

        text = event.text() or ''
        if text and all(character.isalnum() for character in text):
            self._shortcut_buffer += text
            self._set_shortcut_status(
                f'Snabbval: {self._shortcut_buffer.upper()} – tryck Enter')
            event.accept()
            return
        super().keyPressEvent(event)

    def _apply_category_shortcut(self):
        """Apply one category-prefix + consequence-code command."""
        command = self._shortcut_buffer.strip().casefold()
        if not command:
            return False
        matches = []
        for category in getattr(self, '_cats', []):
            prefix = self._category_prefixes.get(category['id'], '').casefold()
            if not prefix or not command.startswith(prefix):
                continue
            severity_code = command[len(prefix):]
            for severity in range(1, self._n_cons + 1):
                if cons_axis_label(severity).strip().casefold() == severity_code:
                    matches.append((category, severity))
        if len(matches) != 1:
            return False
        category, severity = matches[0]
        self._toggle_category(category['id'], severity)
        self._shortcut_buffer = ''
        self._set_shortcut_status(
            f'Valt: {category["name"]} · {cons_axis_label(severity)}')
        return True


class GroupCausePopup(QDialog):
    """Compact two-column editor for a functional two-object cause."""

    choice_requested = pyqtSignal(int, str)  # column (0/1), choice
    def __init__(self, primary, secondary, direction, effect, parent=None,
                 only_column=0):
        super().__init__(parent)
        self.setWindowTitle(
            "Primärhändelse" if only_column == 0 else
            "Sekundärhändelse")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setMinimumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        for column, (heading, equipment, current, choices) in enumerate((
                ("Primär", primary, direction,
                 ("Felar högt", "Felar lågt")),
                ("Sekundär", secondary, effect,
                 ("Öppnar felaktigt", "Stänger felaktigt", "Öppnar fullt",
                  "Stänger helt")))):
            if only_column is not None and column != only_column:
                continue
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.setSpacing(4)
            role = QLabel(heading.upper())
            role.setStyleSheet("color:#6B7280; font-size:8px; font-weight:bold;")
            box_layout.addWidget(role)
            tag = QLabel(str(equipment.get('tag') or 'Objekt'))
            tag.setStyleSheet("color:#17191C; font-weight:bold; font-size:11px;")
            tag.setToolTip(str(equipment.get('description') or ''))
            box_layout.addWidget(tag)
            info = []
            if equipment.get('equipment_type'):
                info.append(str(equipment['equipment_type']))
            if equipment.get('pid_page'):
                info.append(f"P&ID · sida {equipment['pid_page']}")
            if info:
                typ = QLabel("  ·  ".join(info))
                typ.setStyleSheet("color:#6B7280; font-size:9px;")
                box_layout.addWidget(typ)
            for choice in choices:
                button = QPushButton(choice)
                button.setFixedHeight(CONFIG['H_BTN_SMALL'])
                button.setStyleSheet(
                    "QPushButton{background:#F5F5F3;color:#17191C;border:0px;"
                    "border-radius:3px;text-align:left;padding:3px 7px;}"
                    "QPushButton:hover{background:#E8E9E6;}"
                    "QPushButton:pressed{background:#D9DBD8;}")
                button.clicked.connect(
                    lambda _=False, c=column, value=choice:
                    self.choice_requested.emit(c, value))
                box_layout.addWidget(button)
            current_label = QLabel(f"Valt: {current}")
            current_label.setStyleSheet("color:#6B7280; font-size:9px;")
            self._current_labels = getattr(self, '_current_labels', {})
            self._current_labels[column] = current_label
            box_layout.addWidget(current_label)
            columns.addWidget(box)
        layout.addLayout(columns)
        add_mini_popup_close_button(self)

    def set_current(self, column, value):
        """Keep the open popup's status in sync after an inline choice."""
        label = getattr(self, '_current_labels', {}).get(column)
        if label is not None:
            label.setText(f"Valt: {value}")


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
        "QListWidget { border:1px solid #E2E3E1; border-radius:0px; background:white; }"
        "QListWidget::item { padding:2px 4px; border-radius:0px; font-size:10px; }"
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
                              "background:#F5F5F3; border-radius:0px;")
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
                "border:1px solid #fde8cc; border-radius:0px; padding:4px;")
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
                "QPushButton { border:1px solid #dc2626; border-radius:0px;"
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
        prev_frame.setStyleSheet("background:#f0f9ff; border-radius:0px;")
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
            "border-radius:0px; padding:3px 8px;")
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
                     "background:#2F5FD0; border-radius:0px; padding:2px 3px;")
        elif not has_opts:
            style = ("font-weight:bold; color:#8D9299; font-size:10px;"
                     "background:#F5F5F3; border-radius:0px; padding:2px 3px;")
        else:
            style = ("font-weight:bold; color:#17191C; font-size:10px;"
                     "background:#FFFFFF; border:1px solid #CFD1CE; border-radius:0px; padding:1px 2px;")
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
        """Save the consequence chain as one undoable popup action."""
        with self.db.history_group():
            return self._do_save_inner()

    def _do_save_inner(self):
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
    """Compact editor for consequence-specific enablers/reduction factors."""

    _STANDARD_ENABLERS = frozenset({'antändning', 'eskalering'})

    def __init__(self, db, consequence_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.consequence_id = consequence_id
        self._syncing = False
        self._category_checks = {}
        self.setWindowTitle("Övriga enablers")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName('reductionFactorsPopup')
        self.setFixedWidth(350)
        self.setStyleSheet(
            'QWidget#reductionFactorsPopup{background:#FFFFFF;'
            'border:1px solid #4B5563;border-radius:3px;}'
            # The popup's outer edge is the only frame.  A table border and
            # a second category-frame made this small picker look like a
            # dialog inside another dialog.
            'QTableWidget{border:none;background:#FFFFFF;font-size:9px;}'
            'QHeaderView::section{background:#FFFFFF;border:0;border-bottom:1px solid #E2E3E1;'
            'padding:2px 3px;color:#6B7280;font-size:9px;font-weight:bold;}'
            'QTableWidget::item{padding:1px 3px;color:#17191C;}'
            'QTableWidget::item:selected{background:#E6ECFA;color:#17191C;}'
            'QCheckBox{font-size:9px;color:#17191C;}'
            'QCheckBox::indicator{width:13px;height:13px;}'
            'QWidget#enablerCategorySection{border:none;}'
            'QPushButton#addEnabler{border:0;background:transparent;color:#17191C;'
            'text-align:left;padding:3px 6px;font-size:9px;}'
            'QPushButton#addEnabler:hover{background:#E8E9E6;}'
            'QPushButton#removeEnabler{border:0;background:transparent;color:#17191C;'
            'text-align:left;padding:3px 6px;font-size:9px;}'
            'QPushButton#removeEnabler:hover:enabled{background:#E8E9E6;}'
            'QPushButton#removeEnabler:disabled{color:#9CA3AF;}')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 6, 7, 7)
        layout.setSpacing(3)
        self._tbl = QTableWidget(0, 4)
        self._tbl.setHorizontalHeaderLabels(['', 'Enabler', 'RRF', '%'])
        self._tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(0, 23)
        self._tbl.setColumnWidth(2, 52)
        self._tbl.setColumnWidth(3, 58)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setShowGrid(False)
        self._tbl.setMaximumHeight(156)
        self._tbl.cellChanged.connect(self._on_cell)
        self._tbl.currentCellChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tbl)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(3)
        add_btn = QPushButton("+ Egen enabler")
        add_btn.setObjectName('addEnabler')
        add_btn.setFixedHeight(22)
        add_btn.clicked.connect(self._add)
        action_row.addWidget(add_btn)
        self._remove_btn = QPushButton("− Ta bort vald")
        self._remove_btn.setObjectName('removeEnabler')
        self._remove_btn.setFixedHeight(22)
        self._remove_btn.clicked.connect(self._remove_selected)
        action_row.addWidget(self._remove_btn)
        layout.addLayout(action_row)

        self._category_section = QWidget()
        self._category_section.setObjectName('enablerCategorySection')
        self._category_section.setStyleSheet(
            'QCheckBox{border:none;font-size:9px;color:#17191C;}')
        self._category_layout = QVBoxLayout(self._category_section)
        self._category_layout.setContentsMargins(0, 2, 0, 0)
        self._category_layout.setSpacing(1)
        self._category_section.hide()
        layout.addWidget(self._category_section)
        self._refresh()
        add_mini_popup_close_button(self)

    def position_below(self, global_anchor: QPoint):
        """Place the compact picker under its Enablers cell.

        This matches the other small cell popups.  It opens below the
        invoking cell whenever space permits; near a screen edge it is kept
        as low as possible within the available screen area.
        """
        self.adjustSize()
        screen = (QApplication.screenAt(global_anchor)
                  or QApplication.primaryScreen())
        if screen is None:
            self.move(global_anchor)
            return
        available = screen.availableGeometry()
        max_x = available.right() - self.width() + 1
        max_y = available.bottom() - self.height() + 1
        x = min(max(global_anchor.x(), available.left() + 4), max_x)
        y = min(max(global_anchor.y(), available.top() + 4), max_y)
        self.move(QPoint(x, y))

    @staticmethod
    def _number(value, default=10.0, minimum=0.0001, maximum=1_000_000.0):
        try:
            number = float(str(value).replace(',', '.').replace('%', '').strip())
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _format_number(value):
        return f'{float(value):.6g}'

    @classmethod
    def _presence_for_rrf(cls, rrf):
        return min(100.0, 100.0 / cls._number(rrf, minimum=1.0))

    @classmethod
    def _rrf_for_presence(cls, presence):
        return 100.0 / cls._number(presence, minimum=0.0001, maximum=100.0)

    def _refresh(self):
        selected_description = ''
        current_row = self._tbl.currentRow()
        if current_row >= 0 and self._tbl.item(current_row, 1):
            selected_description = self._tbl.item(current_row, 1).text().strip().casefold()
        self._syncing = True
        self._tbl.blockSignals(True)
        self._tbl.setRowCount(0)
        active = {
            str(rf['description']).strip().casefold(): dict(rf)
            for rf in self.db.reduction_factors(self.consequence_id)
            if rf['active']
        }
        factors = [dict(factor) for factor in self.db.reduction_factor_catalog()]
        known = {str(factor['description']).strip().casefold() for factor in factors}
        factors.extend(rf for key, rf in active.items() if key not in known)
        for factor in factors:
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            key = str(factor['description']).strip().casefold()
            rf = active.get(key)
            rrf = self._number((rf or factor)['rrf'], minimum=1.0)
            checked = QTableWidgetItem()
            checked.setFlags(checked.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked.setData(Qt.ItemDataRole.UserRole, factor)
            checked.setCheckState(Qt.CheckState.Checked if rf else Qt.CheckState.Unchecked)
            self._tbl.setItem(r, 0, checked)
            desc = QTableWidgetItem(str((rf or factor)['description']))
            desc.setData(Qt.ItemDataRole.UserRole, rf['id'] if rf else None)
            self._tbl.setItem(r, 1, desc)
            self._tbl.setItem(r, 2, QTableWidgetItem(self._format_number(rrf)))
            self._tbl.setItem(r, 3, QTableWidgetItem(
                f'{self._format_number(self._presence_for_rrf(rrf))} %'))
            self._tbl.setRowHeight(r, 22)
        self._tbl.blockSignals(False)
        self._syncing = False
        self._tbl.setFixedHeight(min(156, 23 + max(1, self._tbl.rowCount()) * 22))
        selected_row = next(
            (row for row in range(self._tbl.rowCount())
             if self._tbl.item(row, 1) and
             self._tbl.item(row, 1).text().strip().casefold() == selected_description),
            0 if self._tbl.rowCount() else -1)
        if selected_row >= 0:
            self._tbl.setCurrentCell(selected_row, 1)
        self._on_selection_changed(selected_row)

    def _add(self):
        self.db.add_reduction_factor(self.consequence_id, 'Ny enabler', 10)
        self._refresh()

    @classmethod
    def _is_standard_enabler(cls, description):
        return str(description or '').strip().casefold() in cls._STANDARD_ENABLERS

    def _on_selection_changed(self, row, *_):
        self._refresh_category_checks(row)
        description_item = self._tbl.item(row, 1) if row >= 0 else None
        description = description_item.text() if description_item else ''
        removable = bool(description.strip()) and not self._is_standard_enabler(description)
        self._remove_btn.setEnabled(removable)
        self._remove_btn.setToolTip(
            'Ta bort den egna enablern ur listan'
            if removable else 'Antändning och Eskalering är standardenablers')

    def _remove_selected(self):
        row = self._tbl.currentRow()
        description_item = self._tbl.item(row, 1) if row >= 0 else None
        description = description_item.text().strip() if description_item else ''
        if not description or self._is_standard_enabler(description):
            return
        with self.db.history_group():
            for factor in self.db.reduction_factors(self.consequence_id):
                if str(factor['description']).strip().casefold() == description.casefold():
                    self.db.delete_reduction_factor(factor['id'])
            # Retire instead of hard-deleting the catalogue entry: another
            # consequence may still use it, but it disappears from future picks.
            self.db.retire_reduction_factor_catalog_entry(description)
        self._refresh()

    def _on_cell(self, row, col):
        if self._syncing:
            return
        checked = self._tbl.item(row, 0)
        desc_item = self._tbl.item(row, 1)
        if not checked or not desc_item:
            return
        factor = checked.data(Qt.ItemDataRole.UserRole) or {}
        description = desc_item.text().strip() or str(factor.get('description') or '')
        existing = [rf for rf in self.db.reduction_factors(self.consequence_id)
                    if str(rf['description']).strip().casefold() == description.casefold()]
        if col == 0:
            if checked.checkState() == Qt.CheckState.Checked and not existing:
                self.db.add_reduction_factor(self.consequence_id, description, factor.get('rrf', 10))
            elif checked.checkState() == Qt.CheckState.Unchecked:
                for rf in existing:
                    self.db.delete_reduction_factor(rf['id'])
            self._refresh()
            return
        if not existing:
            return
        rf_id = existing[0]['id']
        if col == 3:
            presence = self._number(self._tbl.item(row, 3).text(), maximum=100.0)
            rrf = self._rrf_for_presence(presence)
        else:
            rrf = self._number(self._tbl.item(row, 2).text(), minimum=1.0)
        self.db.update_reduction_factor(rf_id, description, rrf, 1)
        self._syncing = True
        self._tbl.item(row, 2).setText(self._format_number(rrf))
        self._tbl.item(row, 3).setText(
            f'{self._format_number(self._presence_for_rrf(rrf))} %')
        self._syncing = False

    def _clear_category_checks(self):
        self._category_checks = {}
        while self._category_layout.count():
            child = self._category_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_category_checks(self, row):
        """Show safeguard-like category applicability for the selected enabler."""
        self._clear_category_checks()
        desc_item = self._tbl.item(row, 1) if row >= 0 else None
        factor_id = desc_item.data(Qt.ItemDataRole.UserRole) if desc_item else None
        categories = [dict(row) for row in
                      self.db.get_consequence_severities(self.consequence_id)]
        if not factor_id or not categories:
            self._category_section.hide()
            return
        excluded_by_severity = (
            self.db.get_severity_excluded_reduction_factors_for_severities(
                [category['id'] for category in categories]))
        label = QLabel('Gäller för konsekvenskategori:')
        label.setStyleSheet('border:none;color:#5B616B;font-size:9px;')
        self._category_layout.addWidget(label)
        for category in categories:
            check = QCheckBox(str(category['name']))
            check.setChecked(factor_id not in excluded_by_severity.get(category['id'], set()))
            check.toggled.connect(
                lambda checked, sid=category['id'], fid=factor_id:
                self._set_category_applicability(fid, sid, checked))
            self._category_checks[category['id']] = check
            self._category_layout.addWidget(check)
        self._category_section.show()

    def _set_category_applicability(self, factor_id, severity_id, applies):
        excluded = self.db.get_severity_excluded_reduction_factors(severity_id)
        if applies:
            excluded.discard(factor_id)
        else:
            excluded.add(factor_id)
        self.db.set_severity_excluded_reduction_factors(severity_id, excluded)


class _ScenarioDelegate(QStyledItemDelegate):
    """Custom delegate: word-wrap for ORS/KON/SG cells; passes eventFilter to editors."""

    _WRAP_COLS = None   # set after panel constants are known

    def __init__(self, panel):
        super().__init__(panel)
        self._panel   = panel
        self._fm_font = None   # cached QFont
        self._fm      = None   # cached QFontMetrics — rebuilt only when font changes

    def createEditor(self, parent, option, index):
        editor = _BoldTagTextEdit(parent)
        if editor is not None:
            # Inline editing should not introduce a second framed cell on
            # top of the painted table cell.  This is especially distracting
            # when the click lands below the second row of a grouped cause.
            # Keep the editor itself, but remove its frame completely.
            editor.setStyleSheet(
                "QTextEdit{border:none;border-radius:0px;"
                "padding:0px;background:#FFFFFF;}"
                "QTextEdit:focus{border:none;"
                "padding:0px;}")
            editor.setFrameStyle(QFrame.Shape.NoFrame)
            editor.setProperty('editing_row', index.row())
            editor.setProperty('editing_col', index.column())
            # Set the grouped row before Qt asks the delegate for the
            # editor geometry.  If this is delayed until setEditorData(),
            # the first geometry pass can place a secondary-row editor at
            # the top of the cell and it may never be repositioned.
            group_edit_line = getattr(self._panel, '_group_edit_line', None)
            if (index.column() == self._panel._C_ORS and
                    group_edit_line is not None and
                    group_edit_line[0] == index.row() and
                    0 <= group_edit_line[1] < MAX_GROUP_OBJECTS):
                editor.setProperty('group_line', int(group_edit_line[1]))
            editor.installEventFilter(self._panel)
            # Do not let focus acquisition turn a normal cell edit into a
            # whole-text selection.  The panel places the caret explicitly
            # for double-clicks; all other entry points start unselected.
            editor.deselect()
            if index.column() == self._panel._C_REK:
                self._prepare_recommendation_editor(editor, index, option)
                self._attach_tag_completer(editor)
                row = index.row()
                editor.textChanged.connect(
                    lambda r=row, ed=editor:
                    self._panel._resize_recommendation_editor(ed, r))
        return editor

    def _attach_tag_completer(self, editor):
        """Offer P&ID tag completion in the recommendation editor too."""
        db = getattr(self._panel, 'db', None)
        if db is None or not isinstance(editor, _BoldTagTextEdit):
            return
        try:
            tags = sorted({str(row['tag']).strip() for row in db.equipment_items()
                           if row['tag'] and str(row['tag']).strip()},
                          key=str.casefold)
        except Exception:
            tags = []
        if not tags:
            return
        completer = QCompleter(tags, editor)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        editor.setTagCompleter(completer)

    def setEditorData(self, editor, index):
        if index.column() == self._panel._C_REK:
            # The REK seed text is computed and set in createEditor()
            # (see _prepare_recommendation_editor) -- Qt calls
            # setEditorData() unconditionally right after createEditor()
            # returns, and its default QLineEdit implementation would
            # otherwise clobber that seed text with the cell's own raw
            # multi-line "XXX. ..." summary (confirmed empirically:
            # createEditor()'s own editor.setText() calls are silently
            # overwritten a moment later without this override). Skip
            # the default entirely for this column.
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        """Commit the editor's plain text, never QTextEdit's HTML document.

        The recommendation column uses this delegate directly (unlike the
        P&ID text columns, which use ``_PidDelegate``).  QStyledItemDelegate's
        generic QTextEdit handling can otherwise pass the complete HTML
        document to the table model.  The table's ``itemChanged`` handler
        then sees the ``<!DOCTYPE HTML...>`` header and the recommendation is
        saved as empty text, which later renders as ``Ny rekommendation``.
        """
        if isinstance(editor, _BoldTagTextEdit):
            clean = editor.toPlainText().strip()
        elif isinstance(editor, QLineEdit):
            clean = editor.text().strip()
        else:
            clean = str(editor.text()).strip()
        clean = _PID_ICON_RE.sub('', clean)
        if index.column() == self._panel._C_REK:
            # The running number is presentation-only and must never be
            # stored as part of the recommendation description.
            clean = re.sub(r'^(?:R-)?\d+\.\s*', '', clean,
                           flags=re.IGNORECASE)
        model.setData(index, clean, Qt.ItemDataRole.EditRole)

    def _prepare_recommendation_editor(self, editor, index, option):
        """REK inline editing (2026-08-26, see NOTES.md "Redigera
        rekommendationer direkt i HAZOP Scenario" — replaces the old
        modal RecommendationEditorDialog). Since a consequence can have
        0, 1, or several linked recommendations but the cell only has
        one line of live-editable text, the seed text/commit target is
        picked so every case stays unambiguous (_on_cell_changed_inner's
        'recommendation' branch mirrors this exactly):
          0 linked -> editor starts blank; committing non-blank text
                      CREATES a new recommendation ("skapa en ny
                      rekommendation med Enter").
          1 linked -> editor starts with that recommendation's own
                      description; committing UPDATES it in place
                      (through the shared "used elsewhere?" prompt).
          2+ linked -> editor starts blank (no single one to edit);
                      committing non-blank text ADDS one more, existing
                      ones untouched.
        RecommendationAssistPopup (opened alongside, same deferred
        QTimer.singleShot(0, ...) pattern as _show_standard_cause_popup)
        is the "extra information ... i en liten popup ovanför" the
        request asked for -- the reuse-search/link-checkbox list that
        used to be the whole modal dialog."""
        row = index.row()
        row_meta = getattr(self._panel, '_row_meta', [])
        cons_id = row_meta[row][2] if row < len(row_meta) else None
        if cons_id is None:
            return
        acts = self._panel.db.recommendations_for_consequence(cons_id)
        rec_ids = getattr(self._panel, '_row_recommendation_ids', [])
        rec_id = rec_ids[row] if row < len(rec_ids) else None
        force_add = getattr(
            self._panel, '_recommendation_force_add_cons_id', None) == cons_id
        if rec_id is not None and not force_add:
            rec = next((a for a in acts if a['id'] == rec_id), None)
            editor.setText(dict(rec).get('description', '') if rec else '')
        elif len(acts) == 1 and not force_add:
            editor.setText(acts[0]['description'] or '')
        else:
            editor.setText('')
        cell_rect = QRect(option.rect)
        panel = self._panel
        popup_token = getattr(panel, '_recommendation_popup_token', 0) + 1
        panel._recommendation_popup_token = popup_token
        QTimer.singleShot(
            0, lambda ed=editor, r=row, cid=cons_id, rect=cell_rect,
            token=popup_token:
            self._show_recommendation_assist_popup(ed, r, cid, rect, token))

    def updateEditorGeometry(self, editor, option, index):
        """Keep the recommendation editor in the cell's existing layout."""
        panel = self._panel
        # The multiline editor must occupy the same painted text area so its
        # wrapping and vertical position stay aligned while editing.  REK's
        # static painter has a slightly wider inset than the other generic
        # cells, so keep that inset here too.
        if index.column() == panel._C_REK:
            # The recommendation number is display metadata, not part of
            # the editable description. Leave it painted by the delegate and
            # start the editor after it, so it cannot disappear or be edited.
            item = panel._table.item(index.row(), index.column())
            rec_id = panel._row_recommendation_ids[index.row()] \
                if index.row() < len(panel._row_recommendation_ids) else None
            if not rec_id and item is not None:
                meta = item.data(Qt.ItemDataRole.UserRole) or ()
                rec_id = meta[2] if len(meta) > 2 else None
            # ``id`` is the stable foreign-key value.  The compact display
            # shown to the user can change when an earlier recommendation is
            # removed, so use the dedicated display sequence here too.
            rec = panel.db.get_recommendation(rec_id) if rec_id else None
            rec_number = (rec or {}).get('display_number', rec_id)
            number_width = QFontMetrics(option.font).horizontalAdvance(
                f"{int(rec_number):03d}.  ") if rec_number else 0
            rect = QRect(option.rect)
            left = rect.left() + 5 + number_width
            editor.setGeometry(QRect(left, rect.top() + 2,
                                      max(10, rect.right() - left - 3),
                                      max(10, rect.height() - 4)))
            # The static cell can use its full width, whereas the live
            # editor starts after the painted running number.  Re-evaluate
            # immediately now that the narrower editor geometry is known;
            # waiting for a textChanged signal made a long existing
            # recommendation appear clipped/smaller until the user typed.
            panel._resize_recommendation_editor(editor, index.row())
            return
        editor.setGeometry(QRect(option.rect).adjusted(2, 2, -2, -2))

    def _show_recommendation_assist_popup(self, editor, row, cons_id,
                                          cell_rect, popup_token=None):
        """Mirrors _PidDelegate._show_standard_cause_popup's positioning
        and focus-safety approach exactly (see that method's own
        docstring for why this must be a plain non-toplevel child widget
        of the panel's top-level window, not a QDialog/separate top-level
        Popup) -- only the popup class and its cons_id differ."""
        if editor is None or sip.isdeleted(editor) or not editor.isVisible():
            return
        panel = self._panel
        try:
            if (popup_token is not None and
                    popup_token != getattr(panel, '_recommendation_popup_token', None)):
                return
            top_level = panel.window()
            # A previous deferred editor-start can otherwise leave a second
            # assist popup behind when editing is reopened quickly. Keep one
            # visible recommendation popup per active Scenario editor.
            for old_popup in top_level.findChildren(RecommendationAssistPopup):
                old_popup.close()
            popup = RecommendationAssistPopup(panel, cons_id, editor, parent=top_level)
            top_global = panel._table.viewport().mapToGlobal(cell_rect.topLeft())
            top = top_level.mapFromGlobal(top_global)
            tl_rect = top_level.rect()
            pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
            x = min(top.x(), tl_rect.right() - pw)
            y = top.y() - ph - 2
            if y < tl_rect.top():
                bottom_global = panel._table.viewport().mapToGlobal(cell_rect.bottomLeft())
                bottom = top_level.mapFromGlobal(bottom_global)
                y = bottom.y() + 2
            popup.move(max(tl_rect.left(), x), max(tl_rect.top(), y))
            popup.show()
            popup.raise_()
        except Exception:
            logging.exception('_show_recommendation_assist_popup: failed to show '
                              'popup (row=%d)', row)

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

        wrap_cols = {panel._C_ORS, panel._C_KON, panel._C_SG, panel._C_REK}
        if col not in wrap_cols:
            # Non-wrap columns (risk cells) stay at one compact line
            base = super().sizeHint(option, index)
            return QSize(base.width(), one_line_h)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ''
        # A grouped cause may intentionally have an empty description: its
        # two object tags are still rendered as two lines by
        # ``_ors_combined_text``.  Do not return the one-line fallback before
        # looking at the group metadata, otherwise the second object is
        # clipped in the Scenario table (the common freshly-created group
        # case).
        item = panel._table.item(index.row(), col)
        group_rows = (len(item.data(Qt.ItemDataRole.UserRole + 9) or [])
                      if col == panel._C_ORS and item else 1)
        group_rows = max(1, group_rows)
        if not text and group_rows == 1:
            return QSize(option.rect.width(), one_line_h)

        w = option.rect.width() if option.rect.width() > 0 else 200
        if col == panel._C_ORS:
            w = max(40, option.rect.width() - 6 - _RRF_W)
            combined = panel._ors_combined_text(item, text)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, combined)
            return QSize(option.rect.width(), max(one_line_h * group_rows, rect.height() + 4))
        elif col == panel._C_KON:
            w = max(40, w)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, text)
            return QSize(option.rect.width(), max(one_line_h, rect.height() + 4))
        elif col == panel._C_SG:
            w -= _RRF_W
        w = max(40, w)
        rect = fm.boundingRect(0, 0, w, 10000,
                               Qt.TextFlag.TextWordWrap, text)
        height = max(one_line_h, rect.height() + 4)
        if col == panel._C_REK and text and text != '—':
            height += one_line_h
        return QSize(option.rect.width(), height)

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
        # Recommendations are often several lines long.  Draw their
        # selection as a flat, compact row with a narrow accent bar instead
        # of the rounded/padded item treatment supplied by the global QSS.
        # This keeps the text fully readable while retaining a clear modern
        # selection cue.
        if col == panel._C_REK:
            r = option.rect
            # The table delegate paints before Qt paints the live editor.
            # Keep the compact number visible, but never paint the saved
            # description beneath an active REK editor: it otherwise reads
            # as ghost text while the user types a replacement.
            editing = any(
                editor.isVisible() and
                editor.property('editing_row') == index.row() and
                editor.property('editing_col') == col
                for editor in panel._table.findChildren(_BoldTagTextEdit))
            painter.save()
            sel = bool(option.state & QStyle.StateFlag.State_Selected)
            if sel:
                # Keep REK flat and consistent with every other selected
                # Scenario field. The old blue accent strip was a second,
                # visually different selection treatment.
                painter.fillRect(r, QColor(_SCENARIO_SELECTION_BG))
                tc = QColor(_SCENARIO_SELECTION_FG)
            else:
                bg = index.data(Qt.ItemDataRole.BackgroundRole)
                painter.fillRect(r, bg if bg is not None else (
                    option.palette.alternateBase() if index.row() % 2 == 1
                    else option.palette.base()))
                fg = index.data(Qt.ItemDataRole.ForegroundRole)
                tc = fg.color() if fg is not None else option.palette.text().color()
            painter.setPen(tc)
            font = index.data(Qt.ItemDataRole.FontRole) or option.font
            painter.setFont(font)
            if editing:
                rec_id = (panel._row_recommendation_ids[index.row()]
                          if index.row() < len(panel._row_recommendation_ids)
                          else None)
                rec = panel.db.get_recommendation(rec_id) if rec_id else None
                number = (rec or {}).get('display_number')
                text = f"{number:03d}." if number else ''
                painter.drawText(r.adjusted(5, 2, -3, -2),
                                 Qt.AlignmentFlag.AlignLeft |
                                 Qt.AlignmentFlag.AlignTop, text)
            else:
                text = index.data(Qt.ItemDataRole.DisplayRole) or ''
                tags = panel._matching_pid_tags(text)
                _draw_text_with_bold_tags(
                    painter, r.adjusted(5, 2, -3, -2), text, tags,
                    font, tc, word_wrap=True)
            painter.restore()
            return
        if col not in (panel._C_RFORE, panel._C_SLUT):
            super().paint(painter, option, index)
            return
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        r = option.rect
        painter.save()
        has_risk = bool(index.data(Qt.ItemDataRole.DisplayRole))
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        risk_color = (bg.color() if isinstance(bg, QBrush)
                      else QColor('#FFFFFF'))
        neutral_bg = (option.palette.alternateBase() if index.row() % 2 == 1
                      else option.palette.base())

        # Keep the risk presentation visible while the cell is selected.  A
        # selected risk cell is normally the one for which the popup is open;
        # replacing the matrix colour with the generic selection colour made
        # the bar appear to jump away during editing.  The selection state is
        # still shown by the table's focus/current-cell treatment, while the
        # actual risk colour remains the stable visual anchor in both modes.
        if self._panel._risk_bars_enabled and has_risk:
            painter.fillRect(r, (QColor(_SCENARIO_SELECTION_BG)
                                 if sel else neutral_bg))
            bar_height = max(1, min(
                _RISK_BAR_HEIGHT, r.height() - 2 * _RISK_BAR_MARGIN_Y))
            bar = QRect(
                r.left() + _RISK_BAR_MARGIN_X,
                r.top() + _RISK_BAR_MARGIN_Y,
                max(1, r.width() - 2 * _RISK_BAR_MARGIN_X),
                bar_height)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(risk_color))
            painter.drawRoundedRect(bar, _RISK_BAR_RADIUS,
                                    _RISK_BAR_RADIUS)
        elif has_risk:
            # Accessibility/legacy option: use the risk-matrix colour as the
            # complete cell background rather than an inset bar.  This also
            # intentionally survives selection, just like the bar mode.
            painter.fillRect(r, QBrush(risk_color))
        else:
            painter.fillRect(r, neutral_bg if not sel
                             else QColor(_SCENARIO_SELECTION_BG))

        if has_risk:
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            tc = fg.color() if fg is not None else option.palette.text().color()
        else:
            tc = (QColor(_SCENARIO_SELECTION_FG) if sel
                  else option.palette.text().color())
        painter.setPen(tc)
        font = index.data(Qt.ItemDataRole.FontRole)
        painter.setFont(font if font is not None else option.font)
        # Put the short risk value inside the bar.  The selected cell uses the
        # same geometry as the unselected cell so the value does not move when
        # the popup opens.
        if not self._panel._risk_bars_enabled or not has_risk:
            text_rect = r
        else:
            bar_height = max(1, min(
                _RISK_BAR_HEIGHT, r.height() - 2 * _RISK_BAR_MARGIN_Y))
            text_rect = QRect(
                r.left() + _RISK_BAR_MARGIN_X,
                r.top() + _RISK_BAR_MARGIN_Y,
                max(1, r.width() - 2 * _RISK_BAR_MARGIN_X),
                bar_height)
        painter.drawText(text_rect.adjusted(2, 2, -2, -2),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         index.data(Qt.ItemDataRole.DisplayRole) or '')
        painter.restore()


_PID_ICON_W  = 22          # pixels reserved on the left for the pin icon
# Height of the ORS cell's FIRST TEXT LINE — where the (now inline, bold)
# tag prefix, the frequency chip, and the comment dot all live (2026-08-25,
# see NOTES.md "Slå ihop objektbaren i Orsak-kolumnen": the separate tag
# strip this constant used to measure is gone — the tag is now painted as
# a bold prefix ON the description's own first line instead of its own
# reserved band above it). Kept as an explicit named constant (rather than
# recomputing a font-metric line height ad hoc at each of its several call
# sites) for the same reason it always was: this file has a documented
# history of exactly this kind of value silently drifting between
# sizeHint/_resize_rows_manual/_wrap_col_row_height and paint()/
# updateEditorGeometry (2026-08-11, bug report: "text göms på raderna ...
# spöktext ligger kvar när man redigerar" — a 14-vs-17px mismatch clipped
# the bottom of wrapped description text). See NOTES.md.
_ORS_FIRST_LINE_H = 17

_RRF_W       = 32          # compact width shared by RRF and Orsak frequency badges
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
            # _ScenarioDelegate.createEditor() has already seeded REK with
            # the description only. Do not replace it with the painted
            # "XXX. description" cell text.
            if index.column() != self._panel._C_REK:
                editor.setText(clean)
            editor.deselect()
            # Keep the identity tokens bold while the editor is active.  For
            # Orsak the tag prefix remains outside the editor; Konsekvens and
            # Safeguard may carry tags inside their description.
            if isinstance(editor, _BoldTagTextEdit):
                tags = []
                item = self._panel._table.item(index.row(), index.column())
                if index.column() == self._panel._C_ORS:
                    obj_data = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
                    tags = [obj_data[1]] if obj_data and obj_data[1] else []
                elif index.column() == self._panel._C_KON:
                    obj_data = item.data(Qt.ItemDataRole.UserRole + 7) if item else None
                    refs = item.data(Qt.ItemDataRole.UserRole + 8) if item else []
                    tags = ([obj_data[1]] if obj_data and obj_data[1] else []) + (refs or [])
                elif index.column() == self._panel._C_SG:
                    obj_data = item.data(Qt.ItemDataRole.UserRole + 6) if item else None
                    refs = item.data(Qt.ItemDataRole.UserRole + 7) if item else []
                    tags = ([obj_data[1]] if obj_data and obj_data[1] else []) + (refs or [])
                editor.set_bold_tags(tags)
            self._attach_tag_completer(editor)
            if index.column() == self._panel._C_ORS:
                self._attach_cause_completer(editor, index)
                # Deferred to the next event-loop iteration — showing the
                # popup SYNCHRONOUSLY here (still inside Qt's own internal
                # openEditor() sequence) was found empirically to disrupt
                # Qt handing keyboard focus to the freshly-created editor
                # (focus landed on a popup child instead, and in one
                # observed case the editor was torn down outright when
                # code tried to reclaim its focus). Qt's own focus
                # assignment for a brand-new editor happens in steps AFTER
                # createEditor() returns; showing another top-level window
                # mid-sequence — even a non-activating one — interfered
                # with that. See _show_standard_cause_popup's own
                # docstring.
                row = index.row()
                cell_rect = QRect(option.rect)
                self._watch_typed_cause_object(editor, index.row(), cell_rect)
                QTimer.singleShot(0, lambda ed=editor, r=row, rect=cell_rect:
                                   self._show_standard_cause_popup(ed, r, rect))
            elif index.column() == self._panel._C_KON:
                self._attach_consequence_completer(editor)
        return editor

    def _attach_tag_completer(self, editor):
        """Offer matching P&ID tags in every shared scenario text editor."""
        db = getattr(self._panel, 'db', None)
        if db is None or not isinstance(editor, _BoldTagTextEdit):
            return
        try:
            tags = sorted({str(row['tag']).strip() for row in db.equipment_items()
                           if row['tag'] and str(row['tag']).strip()},
                          key=str.casefold)
        except Exception:
            tags = []
        if not tags:
            return
        completer = QCompleter(tags, editor)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        editor.setTagCompleter(completer)

    def setEditorData(self, editor, index):
        # QStyledItemDelegate calls setText() after createEditor().  That
        # replaces the document contents and clears QTextEdit's character
        # formats, so restore the identity formatting after Qt has populated
        # the editor rather than relying only on createEditor().
        super().setEditorData(editor, index)
        if isinstance(editor, _BoldTagTextEdit):
            # Consequence keeps the deliberate two-character trigger, while
            # Barrier/Safeguard must offer a P&ID match after one character.
            editor._tag_completion_min_length = (
                1 if index.column() == self._panel._C_SG else 2)
            editor._tag_matcher = self._panel._matching_pid_tags
            item = self._panel._table.item(index.row(), index.column())
            tags = []
            if index.column() == self._panel._C_ORS:
                obj_data = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
                tags = [obj_data[1]] if obj_data and obj_data[1] else []
            elif index.column() == self._panel._C_KON:
                obj_data = item.data(Qt.ItemDataRole.UserRole + 7) if item else None
                refs = item.data(Qt.ItemDataRole.UserRole + 8) if item else []
                tags = ([obj_data[1]] if obj_data and obj_data[1] else []) + (refs or [])
            elif index.column() == self._panel._C_SG:
                obj_data = item.data(Qt.ItemDataRole.UserRole + 6) if item else None
                refs = item.data(Qt.ItemDataRole.UserRole + 7) if item else []
                tags = ([obj_data[1]] if obj_data and obj_data[1] else []) + (refs or [])
            # A tag typed into the text is still an object reference when it
            # exactly matches the P&ID catalogue.  Include those live matches
            # while editing, so the bold identity is not lost before the next
            # table rebuild.
            tags += self._panel._matching_pid_tags(editor.toPlainText())
            editor.set_bold_tags(tags)
            if index.column() == self._panel._C_ORS:
                # The editor's own row marker is authoritative.  The panel
                # marker is only a hand-off value and may still contain the
                # previous click while Qt is opening this editor.
                stored_line = editor.property('group_line')
                group_line = ((index.row(), int(stored_line))
                              if isinstance(stored_line, int) and 0 <= stored_line < MAX_GROUP_OBJECTS else
                              getattr(self._panel, '_group_edit_line', None))
                if group_line is not None and group_line[0] == index.row():
                    item = self._panel._table.item(index.row(), index.column())
                    group_tags = item.data(Qt.ItemDataRole.UserRole + 9) if item else []
                    meta = item.data(Qt.ItemDataRole.UserRole) if item else None
                    if len(group_tags or []) >= 2 and meta:
                        cause = self._panel.db.get_cause(meta[1])
                        lines = self._panel.db.group_cause_description_lines(cause)
                        selected = (lines[group_line[1]].strip()
                                    if group_line[1] < len(lines) else
                                    str(group_tags[group_line[1]]))
                        tag = str(group_tags[group_line[1]]).strip()
                        if tag and selected.casefold().startswith(tag.casefold()):
                            selected = selected[len(tag):].lstrip(' ,:→-')
                        editor.setText(selected)
                        editor.setProperty('group_line', int(group_line[1]))
                    # The row marker is only needed until Qt has populated
                    # this editor.  Clearing it here (rather than with a
                    # zero-delay timer in the click handler) is important:
                    # QTableWidget may create/populate the delegate editor
                    # on a later event-loop turn.  The old timer could erase
                    # the marker first, so a double-click to the right of a
                    # grouped tag opened the wrong full-cell editor.
                    self._panel._group_edit_line = None

    def _attach_consequence_completer(self, editor):
        """"Spara varje konsekvens som skrivs i HAZOP Scenario i en
        databas. Vid redigering ska en rullgardinslista visa tidigare
        konsekvenser. Filtrera listan direkt när användaren skriver,
        case-insensitive, baserat på att texten börjar med det
        inskrivna värdet." (2026-08-26, see NOTES.md "Återanvänd
        tidigare konsekvenser") — every KON cell ever committed is
        recorded in `consequence_history` (_on_cell_changed_inner's
        'consequence' branch); this suggests from that same growing
        list while inline-editing a KON cell.

        MatchStartsWith (prefix match), NOT MatchContains like the ORS
        completer above -- explicitly requested for this one, unlike
        the standard-cause completer's "match anywhere" behavior."""
        db = getattr(self._panel, 'db', None)
        if db is None:
            return
        try:
            descs = db.consequence_history()
        except Exception:
            return
        if not descs:
            return
        comp = QCompleter(descs, editor)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        editor.setCompleter(comp)

    def _show_standard_cause_popup(self, editor, row, cell_rect,
                                   equipment_override=None):
        """Auto-open StandardCauseSuggestPopup alongside the ORS editor
        (2026-08-25, see NOTES.md "Standardorsak-popup vid redigering
        av Orsak-cellen" — Anton: "När jag vill editera orsakstexten
        och står i editerarläget vill jag även att det dyker upp en
        liten popupruta"). Anchored below the ORS cell's own rect
        (flipped above it if that would run off the bottom of the
        top-level window) so it never covers the cell being edited.

        Parented to the panel's own top-level window (`panel.window()`),
        as a plain non-toplevel child — NOT a separate top-level popup
        window. See StandardCauseSuggestPopup's own docstring for why:
        a genuinely separate top-level window (even a carefully
        non-activating one) was found, empirically, to make the
        platform emit a transient focus-out on the active cell editor
        the moment it appeared, which Qt's own item-delegate FocusOut
        handling then read as "user is done editing" and silently
        committed + closed it. A plain child widget creates no new
        OS-level window and triggers no such event.

        Positioning therefore works in the top-level window's own
        local coordinates (mapFromGlobal), clamped to ITS client rect
        rather than the physical screen — a child widget can't extend
        past its top-level ancestor's own bounds regardless.

        Called via QTimer.singleShot(0, ...) from createEditor() (see
        that call site's comment) rather than synchronously, so Qt has
        fully finished its own internal editor-opening sequence
        (including giving the new editor focus) before this runs.
        `row`/`cell_rect` are plain ints/QRect captured synchronously
        in createEditor(), not the original QModelIndex/
        QStyleOptionViewItem — those aren't safe to hold onto across an
        event-loop iteration.

        Skipped for a placeholder row with no real cause yet
        (row_meta[row][1] is None) — nothing to attach a saved
        description or frequency to. Wrapped defensively, same
        reasoning as _size_hint_impl's own try/except: a failure here
        (including the editor having already been closed/destroyed by
        the time this runs, e.g. a very fast Enter right after opening
        it) must never surface as a crash — there's simply no popup to
        show anymore at that point."""
        if editor is None or sip.isdeleted(editor) or not editor.isVisible():
            return
        panel = self._panel
        row_meta = getattr(panel, '_row_meta', [])
        cause_id = row_meta[row][1] if row < len(row_meta) else None
        if cause_id is None:
            return
        try:
            # A rapid sequence of clicks can leave more than one zero-delay
            # show request in Qt's event queue.  One editor must have one
            # standard-cause popup; otherwise an older instance can survive
            # after the visible editor has been closed and make clicking away
            # appear ineffective.
            top_level = panel.window()
            for existing in top_level.findChildren(StandardCauseSuggestPopup):
                if getattr(existing, '_editor', None) is editor:
                    if not existing.isVisible():
                        existing.show()
                    return
            group_line = editor.property('group_line')
            equipment = equipment_override or panel._recognized_pid_equipment(
                editor.toPlainText())
            current_cause = panel.db.get_cause(cause_id)
            current_equipment_id = (current_cause.get('equipment_id')
                                    if current_cause else None)
            if (equipment and current_equipment_id not in (None, equipment.get('id'))):
                # A free-text reference must not silently replace an existing
                # cause-object link; that still uses the explicit tag popup.
                equipment = None
            _std_dev_id, comp_type, dev_description, rows = \
                panel._ors_standard_causes_for_row(
                    row, group_line if isinstance(group_line, int) and group_line >= 0 else None,
                    equipment_override=equipment)
            popup = StandardCauseSuggestPopup(
                panel, row, cause_id, editor, comp_type, dev_description, rows,
                equipment_id=equipment.get('id') if equipment else None,
                parent=top_level)
            # Prefer ABOVE the cell (2026-08-26, see NOTES.md "Flytta
            # HAZOP-popups ovanför"), falling back to below only if there
            # isn't room above in the top-level window's own rect.
            top_global = panel._table.viewport().mapToGlobal(cell_rect.topLeft())
            top = top_level.mapFromGlobal(top_global)
            tl_rect = top_level.rect()
            pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
            x = min(top.x(), tl_rect.right() - pw)
            y = top.y() - ph - 2
            if y < tl_rect.top():
                bottom_global = panel._table.viewport().mapToGlobal(cell_rect.bottomLeft())
                bottom = top_level.mapFromGlobal(bottom_global)
                y = bottom.y() + 2
            popup.move(max(tl_rect.left(), x), max(tl_rect.top(), y))
            popup.show()
            popup.raise_()
        except Exception as error:
            if 'closed database' in str(error).casefold():
                return
            logging.exception('_show_standard_cause_popup: failed to show '
                              'popup (row=%d)', row)

    def _attach_cause_completer(self, editor, index, equipment_override=None):
        """Suggest standard-cause descriptions while inline-editing an Orsak
        cell, so quick text edits get the same suggestions as every other
        inline cause editor instead of a bare, unassisted QLineEdit.
        """
        db = getattr(self._panel, 'db', None)
        if db is None:
            return
        try:
            row = index.row()
            _std_dev_id, _comp_type, _dev_desc, rows = \
                self._panel._ors_standard_causes_for_row(
                    row, equipment_override=equipment_override)
            descs = [c['description'] for c in rows]
            if not descs:
                # Wider than _ors_standard_causes_for_row's own cascade
                # deliberately goes — fine for a type-to-filter completer
                # (the user is already narrowing it by typing), unlike
                # StandardCauseSuggestPopup's button list where showing
                # every standard cause in the study would be unusable.
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

    def _watch_typed_cause_object(self, editor, row, cell_rect):
        """Refresh ORS help when free text identifies a catalogue object."""
        editor.setProperty('typed_cause_object_id', None)
        editor.setProperty('typed_cause_object_serial', 0)

        def queue_refresh():
            try:
                serial = int(editor.property('typed_cause_object_serial') or 0) + 1
                editor.setProperty('typed_cause_object_serial', serial)
            except RuntimeError:
                return
            editor_ref = weakref.ref(editor)
            QTimer.singleShot(
                0, lambda s=serial, ref=editor_ref, r=row, rect=QRect(cell_rect):
                self._refresh_typed_cause_object(ref, s, r, rect))

        editor.textChanged.connect(queue_refresh)

    def _refresh_typed_cause_object(self, editor_ref, serial, row, cell_rect):
        editor = editor_ref()
        if editor is None or sip.isdeleted(editor) or not editor.isVisible():
            return
        try:
            if serial != int(editor.property('typed_cause_object_serial') or 0):
                return
            equipment = self._panel._recognized_pid_equipment(editor.toPlainText())
            equipment_id = equipment.get('id') if equipment else None
            old_id = editor.property('typed_cause_object_id')
            if old_id == equipment_id:
                return
            editor.setProperty('typed_cause_object_id', equipment_id)
            index = self._panel._table.model().index(row, self._panel._C_ORS)
            self._attach_cause_completer(editor, index, equipment)
            top_level = self._panel.window()
            for popup in top_level.findChildren(StandardCauseSuggestPopup):
                if getattr(popup, '_editor', None) is editor:
                    popup.close()
            if equipment is not None:
                if not (equipment.get('equipment_type') or '').strip():
                    self._show_typed_object_type_popup(editor, row, cell_rect,
                                                        equipment)
                else:
                    self._show_standard_cause_popup(
                        editor, row, cell_rect, equipment_override=equipment)
        except RuntimeError:
            return

    def _show_typed_object_type_popup(self, editor, row, cell_rect, equipment):
        """Ask only for the missing object type after a typed tag resolves."""
        if editor is None or sip.isdeleted(editor) or not editor.isVisible():
            return
        panel = self._panel
        cause_id = panel._row_meta[row][1] if row < len(panel._row_meta) else None
        if cause_id is None:
            return
        equipment_id = equipment.get('id')
        if editor.property('typed_cause_type_popup_id') == equipment_id:
            return
        editor.setProperty('typed_cause_type_popup_id', equipment_id)
        popup = CauseTagPopup(
            panel.db, '', equipment.get('tag') or '', parent=panel,
            cause_id=cause_id, equipment_id=equipment_id)
        # This instance was opened from an inline editor. Keep that link so
        # the editor's Escape handler can close both surfaces together.
        popup._inline_editor = editor
        popup.committed.connect(
            lambda ct, tg, r=row, cid=cause_id:
                panel._apply_cause_obj(r, cid, ct, tg, '', None))
        popup.adjustSize()
        top_level = panel.window()
        anchor = panel._table.viewport().mapToGlobal(cell_rect.topLeft())
        pos = top_level.mapFromGlobal(anchor)
        popup.move(max(top_level.rect().left(), pos.x()),
                   max(top_level.rect().top(), pos.y() - popup.height() - 2))
        popup.show()
        popup.raise_()

    def setModelData(self, editor, model, index):
        clean = _PID_ICON_RE.sub('', editor.text().strip())
        group_line = editor.property('group_line')
        try:
            group_line = int(group_line)
        except (TypeError, ValueError):
            group_line = None
        if (index.column() == self._panel._C_ORS and group_line is not None and
                group_line >= 0):
            meta = self._panel._table.item(index.row(), index.column()).data(
                Qt.ItemDataRole.UserRole)
            cause = self._panel.db.get_cause(meta[1]) if meta else None
            group_ids = self._panel.db.group_equipment_ids_for_cause(cause)
            if cause and len(group_ids) >= 2:
                lines = self._panel.db.group_cause_description_lines(
                    cause, group_ids)
                if group_line >= len(lines):
                    return
                # One group member owns one physical row.  Newlines pasted
                # into the small inline editor must remain prose in that row
                # rather than create an untagged extra row that steals a
                # later member's hit area.
                clean = ' '.join(clean.splitlines()).strip()
                equipment = self._panel.db.get_equipment_by_id(
                    group_ids[int(group_line)])
                tag = str((equipment or {}).get('tag') or '').strip()
                if not tag:
                    tag = lines[int(group_line)].split(' ', 1)[0]
                lines[int(group_line)] = f'{tag} {clean}'.strip() if tag else clean
                clean = '\n'.join(lines)
        if index.column() == self._panel._C_REK:
            # Be defensive for editors created by an older delegate path:
            # the running number is presentation-only and must never be
            # stored as part of the recommendation description.
            clean = re.sub(r'^(?:R-)?\d+\.\s*', '', clean,
                           flags=re.IGNORECASE)
        model.setData(index, clean, Qt.ItemDataRole.EditRole)

    def _active_group_edit_line(self, index, rect):
        """Return the grouped row currently covered by an inline editor.

        The delegate paints the cell before Qt paints the editor widget.  A
        grouped editor only covers its own description area, so the static
        description would otherwise remain visible underneath it as ghost
        text.  Identify the live editor by the row/column identity already
        stored on it; the other grouped line must remain painted as context.
        """
        editor = self._active_inline_editor(index)
        if editor is not None:
            try:
                group_line = int(editor.property('group_line'))
            except (TypeError, ValueError):
                return None
            if group_line >= 0:
                return group_line
        return None

    def _active_inline_editor(self, index):
        """Return the live editor for exactly this table cell, if any.

        The delegate paints before the editor widget, so a saved description
        must be deliberately suppressed here rather than relying on the
        editor's white background to cover it.  That coverage is incomplete
        for the compact group-row editors, which occupy only one visual line.
        """
        for editor in self._panel._table.viewport().findChildren(_BoldTagTextEdit):
            if (editor.isVisible() and
                    editor.property('editing_row') == index.row() and
                    editor.property('editing_col') == index.column()):
                return editor
        return None

    def updateEditorGeometry(self, editor, option, index):
        r = option.rect
        col = index.column()
        if col == self._panel._C_ORS:
            # Editor starts right after the bold tag prefix, on the same
            # first line, leaving "V-101, " visible as context while
            # editing the description (2026-08-25, see NOTES.md "Slå
            # ihop objektbaren i Orsak-kolumnen" — same "keep the row's
            # own prefix visible during inline edit" convention
            # tree_panel.py's _begin_inline_edit already established for
            # the HAZOP tree). The frequency label floats over the first
            # line too but doesn't need excluding here — it always sits
            # right-aligned, past where description editing matters
            # (2026-08-18, see NOTES.md "Frekvensen ... hör hemma mer här").
            r = option.rect
            item = self._panel._table.item(index.row(), col)
            desc = item.text() if item is not None else ''
            prefix_w = self._panel._ors_tag_prefix_pixel_width(item, desc, option.font)
            num = item.data(Qt.ItemDataRole.UserRole + 10) if item is not None else None
            if num:
                # The painted ORS text starts with the cause number before
                # the bold object tag. Leave both parts outside the editor.
                prefix_w += QFontMetrics(option.font).horizontalAdvance(
                    f"{num}.  ")
            group_line = editor.property('group_line')
            group_tags = item.data(Qt.ItemDataRole.UserRole + 9) if item else []
            if (group_line is not None and group_line >= 0 and
                    group_line < len(group_tags or []) and len(group_tags or []) >= 2):
                tag_font = QFont(option.font)
                tag_font.setBold(True)
                prefix_w = (QFontMetrics(option.font).horizontalAdvance(
                                f"{num}.  ") if num and group_line == 0 else 0)
                # Keep the editable text clearly to the right of the
                # object tag on BOTH visual rows.  Do not change group_line
                # or any of the primary/secondary data handling here.
                if int(group_line) > 0:
                    operators = self._panel._group_operators(item)
                    prefix_w += QFontMetrics(option.font).horizontalAdvance(
                        f"{operators[group_line]} ")
                prefix_w += QFontMetrics(tag_font).horizontalAdvance(
                    str(group_tags[group_line])) + 10
                line_h = max(_ORS_FIRST_LINE_H,
                             QFontMetrics(option.font).height() + 4)
                top = r.top() + 2 + line_h * group_line
                editor.setGeometry(QRect(r.left() + 2 + prefix_w, top,
                                         max(10, r.right() - r.left() - prefix_w - 4),
                                         max(10, line_h - 2)))
                return
            freq_x, freq_w, _freq = self._panel._ors_freq_zone_geometry(
                item, r.left() + 2, r.right() - 2)
            editor.setGeometry(QRect(r.left() + 2 + prefix_w, r.top() + 2,
                                     max(10, (freq_x if freq_w else r.right() - 2)
                                         - (r.left() + 2 + prefix_w)),
                                     max(10, r.height() - 4)))
            return
        elif col == self._panel._C_SG:
            # 2026-08-10 fix: this used to span the full remaining width,
            # visually covering the RRF badge (_RRF_W) while editing.
            # 2026-08-26 fix: anchor the editor to a single compact line
            # at the TOP of the cell (matching _sg_row_height / the
            # top-aligned static paint in _PidDelegate.paint()'s SG
            # branch above) instead of stretching it to the full row
            # height. A QLineEdit always vertically centers its own text
            # within whatever rect it's given -- on any row taller than
            # one line (height driven by a sibling ORS/KON cell's
            # wrapped text, not by SG itself, since a row's height is
            # shared across every column) the text visibly jumped from
            # the top (painted) to the middle (editing) of the cell.
            # The painted safeguard text includes its sequence number.  Start
            # the live editor after that prefix, otherwise the editor's white
            # background covers the number as soon as editing begins.
            item = self._panel._table.item(index.row(), col)
            left = r.left() + 2
            num = item.data(Qt.ItemDataRole.UserRole + 10) if item is not None else None
            if not num and index.row() < len(self._panel._row_meta):
                cons_id, sg_id = (self._panel._row_meta[index.row()][2],
                                  self._panel._row_meta[index.row()][3])
                if sg_id:
                    num = self._panel._child_number('safeguard', cons_id, sg_id)
            if num:
                left += QFontMetrics(option.font).horizontalAdvance(f"{num}.  ")
            editor.setGeometry(QRect(left, r.top() + 1,
                                     max(10, r.right() - left - _RRF_W - 2),
                                     max(10, r.height() - 2)))
            return
        left = r.left() + 2
        item = self._panel._table.item(index.row(), col)
        if col == self._panel._C_KON and item is not None:
            num = item.data(Qt.ItemDataRole.UserRole + 10)
            if num:
                left += QFontMetrics(option.font).horizontalAdvance(f"{num}.  ")
        editor.setGeometry(QRect(left, r.top() + 2,
                                 max(10, r.right() - left - 1),
                                 max(10, r.height() - 4)))

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
                    painter.fillRect(r, QColor(_SCENARIO_SELECTION_BG))
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                body_top = r.top()
                body_h   = r.height()

                # Layout: [description ...][RRF badge 54px]. The former
                # left-side safeguard object emoji/picker was removed and
                # archived 2026-08-27, so description uses the full body.
                desc_w    = r.width() - _RRF_W
                desc_rect = QRect(r.left(), body_top, desc_w, body_h)
                rrf_rect  = self._panel._sg_rrf_zone_geometry(r)

                # Description text wraps inside the area left of the RRF
                # badge; drag-appended tags remain bold.
                # Same font size as every other cell — only the row's own
                # padding shrank (self._panel._sg_row_height), not the
                # text (2026-08-18 follow-up: Anton clarified it's the
                # CELL height that should shrink, not the text itself).
                desc = index.data(Qt.ItemDataRole.DisplayRole) or ''
                _num = index.data(Qt.ItemDataRole.UserRole + 10)
                if _num:
                    desc = f"{_num}.  {desc}"
                tc = (QColor(_SCENARIO_SELECTION_FG) if sel
                      else option.palette.text().color())
                tagged_refs = self._panel._active_tag_refs_in_text(
                    index.data(Qt.ItemDataRole.UserRole + 7) or [], desc)
                tagged_refs += self._panel._matching_pid_tags(desc)
                active_editor = self._active_inline_editor(index)
                if active_editor is not None:
                    # The live editor paints the description itself.  Do not
                    # paint the saved description underneath it: on wrapped
                    # rows the editor does not cover the complete cell and
                    # the old value otherwise appears as ghost text.
                    prefix = f"{_num}.  " if _num else ''
                    if prefix:
                        _draw_text_with_bold_tags(
                            painter, desc_rect.adjusted(2, 1, -2, -1),
                            prefix, [], option.font, tc, word_wrap=False)
                else:
                    _draw_text_with_bold_tags(
                        painter, desc_rect.adjusted(2, 1, -2, -1), desc,
                        tagged_refs, option.font, tc, word_wrap=True)

                # RRF badge (right column)
                badge_bg = (QColor(_SCENARIO_SELECTION_BG) if sel
                            else QColor('#F5F5F3'))
                painter.fillRect(rrf_rect, badge_bg)
                badge_tc = (QColor(_SCENARIO_SELECTION_FG) if sel
                            else QColor('#17191C'))
                painter.setPen(badge_tc)
                badge_font = QFont(option.font)
                badge_font.setBold(True)
                painter.setFont(badge_font)
                # Just the number — "RRF" now lives in the column header
                # instead, so this single-line badge doesn't force the
                # row taller than the ORS/description content needs
                # (2026-08-14, see NOTES.md).
                raw_rrf = f"{rrf}"
                rrf_text = QFontMetrics(badge_font).elidedText(
                    raw_rrf, Qt.TextElideMode.ElideRight,
                    max(1, rrf_rect.width() - 4))
                painter.drawText(rrf_rect.adjusted(1, 0, -1, 0),
                                 Qt.AlignmentFlag.AlignCenter |
                                 Qt.AlignmentFlag.AlignVCenter,
                                 rrf_text)

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

                painter.restore()
                return

        # ── Cause cells: inline bold tag prefix + description, frequency
        # chip floating on the first line (2026-08-25, see NOTES.md "Slå
        # ihop objektbaren i Orsak-kolumnen" — replaces the old separate
        # tag strip: "V-101, Felar öppen" as ONE flowing, word-wrapped
        # text block instead of a banner above the description) ──────────
        if col == self._panel._C_ORS:
            obj_data = index.data(Qt.ItemDataRole.UserRole + 2)
            if obj_data is not None:
                item = self._panel._table.item(row, col)
                desc = index.data(Qt.ItemDataRole.DisplayRole) or ''
                tag_label, show_tag = self._panel._ors_tag_prefix(item)
                combined = self._panel._ors_combined_text(item, desc)
                active_editor = self._active_inline_editor(index)

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
                    painter.fillRect(r, QColor(_SCENARIO_SELECTION_BG))
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                desc_rect = QRect(r.left() + 2, r.top() + 2,
                                   max(0, r.width() - 4 - _RRF_W),
                                   max(0, r.height() - 4))

                # ── Combined text: bold tag prefix (if any) + plain
                # description, word-wrapped as ONE block via the same
                # QTextLayout-based helper already used to bold drag-
                # dropped tags inside KON/SG descriptions — reused here
                # instead of a bespoke layout implementation, given this
                # file's own documented history of paint/geometry bugs.
                tc = (QColor(_SCENARIO_SELECTION_FG) if sel
                      else option.palette.text().color())
                tags = list(index.data(Qt.ItemDataRole.UserRole + 9) or [])
                if not tags:
                    tags = [tag_label] if show_tag else []
                tags += self._panel._matching_pid_tags(desc)
                if len(tags) >= 2:
                    # Group causes are deliberately rendered line-by-line.
                    # QTextLayout's wrapping can collapse the explicit line
                    # break when the cell is spanned by QTableWidget; direct
                    # drawing makes the one-cell/two-physical-lines contract
                    # deterministic.
                    bf = QFont(option.font)
                    bf.setBold(True)
                    line_h = max(_ORS_FIRST_LINE_H,
                                 QFontMetrics(option.font).height() + 4)
                    active_group_line = self._active_group_edit_line(index, r)
                    painter.setPen(tc)
                    for line_no, line_text in enumerate(combined.splitlines()[:MAX_GROUP_OBJECTS]):
                        # Keep the child number normal-weight, then bold the
                        # actual object tag.  On the first line the number is
                        # part of the displayed text (``1. FI ...``), whereas
                        # the second line starts directly with the affected
                        # object.
                        number_prefix = ''
                        if line_no == 0 and line_text[:1].isdigit():
                            m = re.match(r'^(\d+\.\s+)(.*)$', line_text)
                            if m:
                                number_prefix, line_text = m.group(1), m.group(2)
                        parts = line_text.split(' ', 1)
                        y = desc_rect.top() + line_no * line_h
                        x = desc_rect.left()
                        if number_prefix:
                            painter.setFont(option.font)
                            painter.drawText(QRect(x, y, desc_rect.width(), line_h),
                                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                             number_prefix)
                            x += QFontMetrics(option.font).horizontalAdvance(number_prefix)
                        if line_no > 0:
                            operators = self._panel._group_operators(item)
                            operator = (operators[line_no]
                                        if line_no < len(operators) else 'OR')
                            painter.setFont(option.font)
                            painter.drawText(QRect(x, y, desc_rect.width(), line_h),
                                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                             f"{operator} ")
                            x += QFontMetrics(option.font).horizontalAdvance(
                                f"{operator} ")
                        painter.setFont(bf)
                        painter.drawText(QRect(x, y,
                                               max(0, desc_rect.right() - x + 1), line_h),
                                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                         parts[0])
                        if len(parts) > 1:
                            # Leave a small, consistent visual gap after the
                            # bold object tag before its mechanism/effect text.
                            if line_no == active_group_line:
                                # The live editor paints this description
                                # itself.  Keep only the number/tag visible
                                # as context and suppress the stale static
                                # description underneath the editor.
                                continue
                            x += (QFontMetrics(bf).horizontalAdvance(parts[0]) +
                                  QFontMetrics(option.font).horizontalAdvance(' '))
                            painter.setFont(option.font)
                            painter.drawText(QRect(x, y,
                                                   max(0, desc_rect.right() - x + 1), line_h),
                                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                             parts[1])
                else:
                    if active_editor is not None:
                        # Keep the structural context outside the editor,
                        # but do not paint the saved description underneath
                        # the text currently being edited.  This mirrors the
                        # recommendation editor's ghost-text rule.
                        number = item.data(Qt.ItemDataRole.UserRole + 10) if item else None
                        context = f"{number}.  " if number else ''
                        if show_tag:
                            context += tag_label
                            if desc.strip() not in ('', 'Ny orsak'):
                                context += ', '
                        if context:
                            _draw_text_with_bold_tags(
                                painter, desc_rect.adjusted(0, 1, 0, -1),
                                context, [tag_label] if show_tag else [],
                                option.font, tc, word_wrap=False)
                    else:
                        _draw_text_with_bold_tags(
                            painter, desc_rect.adjusted(0, 1, 0, -1),
                            combined, tags, option.font, tc, word_wrap=True)

                # ── Frequency — floats over the first line, right-aligned,
                # drawn AFTER the text so it stays on top ("längst ut till
                # höger" — every orsak has its own frequency,
                # causes.likelihood/base_frequency, 2026-08-18, see
                # NOTES.md). Unchanged in substance by this rewrite — only
                # desc_rect's own top moved (no more strip above it).
                freq_zone_x, freq_zone_w, freq_str = \
                    self._panel._ors_freq_zone_geometry(index, r.left() + 2, r.right() - 2)
                if freq_str is not None:
                    ff = QFont(option.font)
                    # Keep the compact first-line badge within the shared
                    # 17px line; boldness and alignment match the RRF badge.
                    ff.setPointSize(max(6, option.font.pointSize() - 1))
                    ff.setBold(True)
                    ffm = QFontMetrics(ff)
                    # The badge belongs to the first line, not the full
                    # wrapped cause cell; otherwise AlignVCenter makes it
                    # visibly drift downward when the description wraps.
                    chip_rect = QRect(freq_zone_x, r.top(), freq_zone_w,
                                      min(_ORS_FIRST_LINE_H, r.height()))
                    chip_bg = (QColor(_SCENARIO_SELECTION_BG) if sel
                               else QColor('#F5F5F3'))
                    painter.fillRect(chip_rect, chip_bg)
                    painter.setFont(ff)
                    f_tc = (QColor(_SCENARIO_SELECTION_FG) if sel
                            else QColor('#17191C'))
                    painter.setPen(f_tc)
                    painter.drawText(chip_rect.adjusted(1, 0, -1, 0),
                                     Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                                     ffm.elidedText(freq_str, Qt.TextElideMode.ElideRight,
                                                    max(1, freq_zone_w - 2)))
                    painter.setPen(QPen(QColor('#bcd'), 1))
                    painter.drawLine(chip_rect.left(), r.top(),
                                     chip_rect.left(), r.bottom())

                # ── Comment dot — the only icon here since the 2026-08-18
                # fill-status "plupp" removal. Drawn AFTER the text (like
                # the frequency chip above) so it stays on top rather than
                # being painted over — it used to sit safely inside its
                # own strip band, which is gone now that the tag is
                # inline. Geometry shared with eventFilter()'s click zone
                # via _ors_comment_dot_geometry.
                if _has_comment:
                    painter.setBrush(QBrush(QColor('#17191C')))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(self._panel._ors_comment_dot_geometry(r))
                    painter.setBrush(Qt.BrushStyle.NoBrush)

                painter.restore()
                return

        # ── Consequence cells: description only (2026-08-26, see
        # NOTES.md "Flytta konsekvenskategori till riskmatrisen" — the
        # category/C-value badge zone that used to live at the left of
        # this cell, e.g. "Per C5", moved to the risk matrix popup) ────
        if col == self._panel._C_KON:
            con_data = index.data(Qt.ItemDataRole.UserRole)
            if con_data and con_data[0] == 'consequence':
                r = option.rect
                painter.save()
                if sel:
                    painter.fillRect(r, QColor(_SCENARIO_SELECTION_BG))
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                txt_rect = r

                # Description text — word-wrapped, drag-appended tags in
                # bold (2026-08-09, see NOTES.md "fetmarkera objekttexten")
                display = index.data(Qt.ItemDataRole.DisplayRole) or ''
                _num = index.data(Qt.ItemDataRole.UserRole + 10)
                if _num:
                    display = f"{_num}.  {display}"
                tc = (QColor(_SCENARIO_SELECTION_FG) if sel
                      else option.palette.text().color())
                tagged_refs = self._panel._active_tag_refs_in_text(
                    index.data(Qt.ItemDataRole.UserRole + 8) or [], display)
                tagged_refs += self._panel._matching_pid_tags(display)
                active_editor = self._active_inline_editor(index)
                if active_editor is not None:
                    # The live editor paints the description itself.  Keep
                    # only the structural consequence number here; otherwise
                    # the saved description remains visible underneath the
                    # editor as ghost text on tall/wrapped rows.
                    prefix = f"{_num}.  " if _num else ''
                    if prefix:
                        _draw_text_with_bold_tags(
                            painter, txt_rect.adjusted(2, 2, -2, -2),
                            prefix, [], option.font, tc, word_wrap=False)
                else:
                    _draw_text_with_bold_tags(
                        painter, txt_rect.adjusted(2, 2, -2, -2), display,
                        tagged_refs, option.font, tc, word_wrap=True)

                painter.restore()
                return

        # ── Default: delegate straight to the base description painting ────────
        super().paint(painter, option, index)

class StandardCauseSuggestPopup(QWidget):
    """Non-covering helper popup shown automatically whenever an ORS
    (Orsak) cell enters inline edit mode (2026-08-25, see NOTES.md
    "Standardorsak-popup vid redigering av Orsak-cellen" — Anton: "det
    dyker upp en liten popupruta (som inte täcker cellen) ... jag skall
    kunna välja bland de 'standard'-orsaker som finns för objektypen
    och avikelsen"). Lists the standard causes applicable to this row's
    object type + deviation (via ScenarioTablePanel._ors_standard_causes_for_row)
    as plain buttons, plus one row for the cause's own current
    frequency that reuses the existing FrequencyPickerPopup.

    Deliberately a plain, non-top-level CHILD widget of the panel's own
    top-level window — not a QDialog/.exec(), and not a separate
    top-level window at all (confirmed empirically, the hard way: a
    genuinely separate top-level window, even one flagged
    Qt.WindowType.Tool + WA_ShowWithoutActivating + NoFocus on every
    descendant, still made the underlying platform emit a transient
    FocusOut on the active cell editor the instant it was shown — with
    nothing else picking up focus in its place. QAbstractItemDelegate's
    default FocusOut handling (the exact mechanism that lets a
    QCompleter's OWN popup coexist with an editor, via
    completer.setWidget(editor)) treats "focus went to nothing in the
    editor's own ancestry" as "user is done editing" and auto-commits
    + closes it — reproduced and confirmed via commitData/closeEditor
    signal tracing before landing on this fix). A plain child widget
    never creates a new OS-level window and so never triggers that
    focus-out at all — the editor's focus is completely undisturbed by
    this popup existing, appearing, or disappearing. The trade-off is
    that positioning must stay within the top-level window's own
    client area (screen-edge clamping doesn't apply to a child
    widget) — see _show_standard_cause_popup's positioning code."""

    _BTN_STYLE = (
        "QPushButton{text-align:left; font-size:10px; padding:3px 6px;"
        "border:none; background:transparent; border-radius:0px;}"
        "QPushButton:hover{background:#F5F5F3;}")
    _FREQ_BTN_STYLE = (
        "QPushButton{color:#17191C; background:#F5F5F3; border-radius:0px;"
        "padding:1px 6px; font-size:10px; font-weight:bold; border:none;}"
        "QPushButton:hover{background:#E8E9E6;}")

    def __init__(self, panel, row, cause_id, editor, comp_type, dev_description,
                 rows, equipment_id=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("standardCauseSuggestPopup")
        # NoFocus on the popup itself and every child (set on each
        # widget built below) — belt-and-suspenders on top of this
        # already being a non-toplevel widget: it must never be able to
        # actively grab keyboard focus via a mouse click either.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Give the child popup a clear boundary against the table and the
        # application background. It remains a non-focusable child widget;
        # only the visual framing changes here.
        self.setStyleSheet("QWidget#standardCauseSuggestPopup{"
                           "background:#FFFFFF;border:1px solid #4B5563;"
                           "border-radius:3px;}")
        self._panel = panel
        self._row = row
        self._cause_id = cause_id
        self._editor = editor
        self._equipment_id = equipment_id

        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QLabel()
        header.setStyleSheet("color:#777; font-size:9px;")
        header.setWordWrap(True)
        if rows and dev_description:
            header.setText(f"Standardorsaker  —  {comp_type}  /  {dev_description}")
        elif rows:
            header.setText(f"Standardorsaker  —  {comp_type}")
        else:
            header.setText("Ingen standardorsak för denna kombination")
        layout.addWidget(header)

        if rows:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setMaximumHeight(150)   # same cap CauseObjectPopup uses
            scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            inner = QWidget()
            inner.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            vbox = QVBoxLayout(inner)
            vbox.setSpacing(1)
            vbox.setContentsMargins(0, 0, 0, 0)
            for r in rows:
                btn = QPushButton(r['description'])
                btn.setStyleSheet(self._BTN_STYLE)
                btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                btn.clicked.connect(partial(self._pick, r['description']))
                vbox.addWidget(btn)
            vbox.addStretch()
            scroll.setWidget(inner)
            layout.addWidget(scroll)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#E2E3E1; margin:2px 0px;")
        layout.addWidget(sep)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Frekvens:"))
        self._freq_btn = QPushButton()
        self._freq_btn.setStyleSheet(self._FREQ_BTN_STYLE)
        self._freq_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._freq_btn.clicked.connect(self._edit_frequency)
        self._refresh_freq_button()
        freq_row.addWidget(self._freq_btn)
        freq_row.addStretch()
        layout.addLayout(freq_row)

        self.setMinimumWidth(200)
        self.setMaximumWidth(320)
        self.adjustSize()
        add_mini_popup_close_button(self)

        # Two redundant close triggers, since neither alone is reliable:
        # editor.destroyed fires on actual C++ object deletion, which Qt
        # defers (via deleteLater) by an unpredictable amount after
        # editing ends — confirmed empirically, an editor closed via
        # commitData/closeEditor stayed a live, non-deleted QObject for
        # a noticeable moment. What DOES happen immediately/synchronously
        # when editing ends, by any means (Enter, Escape, clicking
        # another cell, or this popup's own _pick/_edit_frequency), is
        # the editor being hidden — so an event filter catching Hide
        # closes this popup right away, with the destroyed connection
        # kept only as a backstop for the rare case the editor is torn
        # down without ever being explicitly hidden first.
        editor.installEventFilter(self)
        editor.destroyed.connect(self.close)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Hide:
            self.close()
        return super().eventFilter(obj, event)

    def _current_ors_item(self):
        return self._panel._table.item(self._row, self._panel._C_ORS)

    def _refresh_freq_button(self):
        item = self._current_ors_item()
        f_level = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
        numeric = item.data(Qt.ItemDataRole.UserRole + 5) if item else None
        label = self._panel._ors_freq_label(f_level, numeric)
        self._freq_btn.setText(label or "Ange frekvens…")

    def _pick(self, description):
        """A chosen standard cause commits the description and closes
        editing immediately (confirmed with Anton via AskUserQuestion —
        the fast "pick and you're done" path, not "just fill the field
        and keep editing"), same commitData/closeEditor emit pattern
        the Enter key already uses (eventFilter(), scenario_panel.py)."""
        equipment_bound = False
        if self._equipment_id is not None:
            equipment_bound = self._panel._bind_recognized_cause_equipment(
                self._cause_id, self._equipment_id)
        self._editor.setText(description)
        delegate = self._panel._pid_delegate
        delegate.commitData.emit(self._editor)
        delegate.closeEditor.emit(
            self._editor, QStyledItemDelegate.EndEditHint.NoHint)
        self.close()
        # Binding a tag changes the cell's identity metadata (the bold
        # ``TAG-123, `` prefix), not only its description.  The regular
        # description fast path deliberately leaves that metadata alone, so
        # refresh once the editor has committed its selected text.  Deferring
        # this until after commitData guarantees the rebuilt cell contains
        # both parts: ``TAG-123, Felar stängd``.
        if equipment_bound:
            QTimer.singleShot(0, self._panel._schedule_rebuild)

    def _edit_frequency(self):
        """Commit any not-yet-confirmed description text FIRST — clicking
        here triggers _on_ors_frequency_picked -> _schedule_rebuild(),
        which tears down the active cell editor as a side effect (see
        ScenarioTablePanel._rebuild()'s "Proactively clear focus from
        any active cell editor" comment) — committing first means that
        teardown can never silently discard typed-but-unconfirmed text."""
        delegate = self._panel._pid_delegate
        delegate.commitData.emit(self._editor)
        delegate.closeEditor.emit(
            self._editor, QStyledItemDelegate.EndEditHint.NoHint)

        item = self._current_ors_item()
        f_level = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
        numeric = item.data(Qt.ItemDataRole.UserRole + 5) if item else None
        gp = self._freq_btn.mapToGlobal(self._freq_btn.rect().bottomLeft())
        popup = FrequencyPickerPopup.create_positioned(
            gp, current_f_level=f_level, current_numeric_freq=numeric,
            parent=self._panel)
        popup.frequency_selected.connect(
            lambda f, n, cid=self._cause_id:
                self._panel._on_ors_frequency_picked(cid, f, n))
        popup.exec()
        # The frequency change already triggered a rebuild that tore
        # down the (now-closed) cell editor — close explicitly rather
        # than relying on the editor.destroyed signal to arrive first.
        self.close()


class RecommendationAssistPopup(QWidget):
    """"Extra information kan visas i en liten popup ovanför" (2026-08-26,
    see NOTES.md "Redigera rekommendationer direkt i HAZOP Scenario") —
    shown automatically whenever a REK cell enters inline edit mode,
    same non-toplevel-child-widget/NoFocus/closes-on-editor-Hide
    approach as StandardCauseSuggestPopup (see its own docstring for
    why a genuinely separate top-level window breaks the active cell
    editor's focus).

    Shows only reusable, not-yet-linked catalogue rows as compact numbered
    choice buttons. The active REK cell already renders its linked
    recommendation, so repeating that text in this popup made the UI look
    like it contained duplicates. The editor text acts as the search field:
    it matches both the visible recommendation number and any part of the
    description.
    Choosing a result links it once and closes the editor without creating a
    second recommendation."""

    _RESULT_STYLE = (
        "QPushButton{text-align:left; font-size:10px; padding:4px 6px;"
        "border:none; background:transparent; border-radius:0px;}"
        "QPushButton:hover{background:#F5F5F3;}"
        "QPushButton:pressed{background:#E6ECFA;}")

    def __init__(self, panel, cons_id, editor, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("RecommendationAssistPopup{background:#FFFFFF;"
                           "border:1px solid #E2E3E1;}")
        self._panel = panel
        self._cons_id = cons_id
        self._editor = editor
        self._filter_text = str(
            editor.toPlainText() if hasattr(editor, 'toPlainText')
            else editor.text() or '').strip()
        self._filter_refresh_token = 0

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 6, 8, 6)

        title = QLabel("Återanvänd rekommendation")
        title_font = QFont(); title_font.setBold(True); title_font.setPointSize(9)
        title.setFont(title_font)
        title.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(title)

        hint = QLabel("Skriv för att söka på nummer eller text. Klicka för att använda.")
        hint.setStyleSheet("color:#666; font-size:9px;")
        hint.setWordWrap(True)
        hint.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(hint)

        self._list_layout = QVBoxLayout()
        self._list_layout.setSpacing(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMaximumHeight(150)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        inner = QWidget()
        inner.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        inner.setLayout(self._list_layout)
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        self.setMinimumWidth(250)
        self.setMaximumWidth(360)
        self._refresh_list()
        self.adjustSize()
        add_mini_popup_close_button(self)

        # Same two redundant close triggers as StandardCauseSuggestPopup
        # (see its docstring for why neither alone is reliable).
        editor.installEventFilter(self)
        editor.destroyed.connect(self.close)
        editor.textChanged.connect(self._on_editor_text_changed)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Hide:
            self.close()
        return super().eventFilter(obj, event)

    def _refresh_list(self):
        while self._list_layout.count():
            child = self._list_layout.takeAt(0)
            widget = child.widget()
            if widget:
                # Detach first so stale result buttons are no longer children
                # of this popup during Qt's deferred deletion turn. Besides
                # avoiding a growing hidden-widget tree, this prevents a
                # filtered list from briefly exposing yesterday's results.
                widget.setParent(None)
                widget.deleteLater()
        linked = {r['id'] for r in self._panel.db.recommendations_for_consequence(self._cons_id)}
        recs = self._panel.db.all_recommendations()
        if not recs:
            empty = QLabel("Inga rekommendationer i studien ännu.")
            empty.setStyleSheet("font-size:9px; color:#8D9299;")
            empty.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._list_layout.addWidget(empty)
            return
        needle = self._filter_text.casefold().strip()
        matching_recs = [rec for rec in recs if (
            not needle or needle in
            f"{rec['display_number']:03d}. {rec['description'] or ''}".casefold())]
        visible_recs = [rec for rec in matching_recs if rec['id'] not in linked]
        if not visible_recs:
            empty_text = ("Alla matchande rekommendationer är redan länkade."
                          if matching_recs
                          else "Inga numrerade rekommendationer matchar sökningen.")
            empty = QLabel(empty_text)
            empty.setStyleSheet("font-size:9px; color:#8D9299;")
            empty.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._list_layout.addWidget(empty)
            return
        for rec in visible_recs:
            button = QPushButton(
                f"{rec['display_number']:03d}. {rec['description'] or 'Ny rekommendation'}")
            button.setStyleSheet(self._RESULT_STYLE)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip("Klicka för att länka denna rekommendation")
            button.clicked.connect(partial(self._select_recommendation, rec['id']))
            self._list_layout.addWidget(button)

    def _on_editor_text_changed(self):
        """Reduce the existing recommendation list as the user types.

        Recommendation text is searched with ``contains`` so a word in the
        middle of a longer recommendation is enough to find it. The formatted
        The number is included too, so ``012`` is an equally direct lookup.
        """
        if self._editor is None:
            return
        text = self._editor.toPlainText() if hasattr(self._editor, 'toPlainText') \
            else self._editor.text()
        new_filter = str(text or '').strip()
        if new_filter == self._filter_text:
            return
        self._filter_text = new_filter
        # Rebuilding a popup's button layout while QTextEdit is in the
        # middle of processing a key can make the table delegate lose its
        # editor on some Qt styles. Queue the visual filtering until that key
        # event has completed; the editor keeps focus and the list still
        # updates before the next normal UI turn.
        self._filter_refresh_token += 1
        token = self._filter_refresh_token
        QTimer.singleShot(0, lambda: (
            self._refresh_list() if token == self._filter_refresh_token else None))

    def _select_recommendation(self, rec_id):
        """Use one numbered catalogue row and discard the search text."""
        self._panel.db.link_recommendation_to_consequence(rec_id, self._cons_id)
        # The inline field is a search/new-text field. Clearing it before
        # closing avoids committing that same search as a duplicate new row.
        if hasattr(self._editor, 'setText'):
            self._editor.setText('')
        self._panel._refresh_recommendation_cell(self._cons_id)
        self._panel._delegate.closeEditor.emit(
            self._editor, QStyledItemDelegate.EndEditHint.NoHint)
        self.close()


class SgRRFCategoryPopup(QDialog):
    """Compact popup for changing a safeguard's RRF and type.

    ``Gäller för konsekvenskategori`` remains here because it is part of
    the safeguard's risk applicability.  The former per-cause selection is
    intentionally absent: a safeguard is no longer configured differently
    for individual causes from this popup.
    """

    def __init__(self, db, sg_id, current_rrf, current_sg_type,
                 sev_cat_list, cause_list=None, parent=None):
        super().__init__(parent)
        self.db              = db
        self._sg_id          = sg_id
        self._current_rrf    = current_rrf
        self._current_type   = current_sg_type or 'Övrigt'
        self._sev_cat_list   = list(sev_cat_list or [])  # [(severity_id, category_name), ...]
        self._cat_checks: dict[int, QCheckBox] = {}
        self.setWindowTitle("Barriär — RRF & tillämpning")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("sgRrfPopup")
        self.setStyleSheet(
            "QWidget#sgRrfPopup{background:#FFFFFF;"
            "border:1px solid #4B5563;border-radius:3px;}"
            "QListWidget{border:none;background:#FFFFFF;font-size:10px;}"
            "QListWidget::item{padding:3px 6px;color:#17191C;}"
            "QListWidget::item:hover{background:#F5F5F3;}"
            "QListWidget::item:selected{background:#E8E9E6;color:#17191C;}")
        self._build()
        add_mini_popup_close_button(self)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        title = QLabel("RRF")
        title.setStyleSheet("border:none;color:#17191C;font-size:10px;"
                            "font-weight:bold;")
        outer.addWidget(title)

        # Type selector
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Typ:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(SG_TYPES)
        idx = self._type_combo.findText(self._current_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._type_combo.setStyleSheet("border:1px solid #CFD1CE;"
                                      "font-size:10px;")
        type_row.addWidget(self._type_combo)
        outer.addLayout(type_row)

        # RRF preset list + custom spinbox
        presets = QListWidget()
        presets.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        presets.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        presets.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        presets.setMinimumWidth(150)
        presets.setMaximumHeight(120)
        self._preset_items = {}
        for v in (1, 10, 100, 1000, 10000):
            item = QListWidgetItem(str(v))
            item.setData(Qt.ItemDataRole.UserRole, v)
            presets.addItem(item)
            self._preset_items[v] = item
            if v == self._current_rrf:
                item.setSelected(True)
        presets.itemClicked.connect(
            lambda item: self._spin.setValue(item.data(Qt.ItemDataRole.UserRole)))
        outer.addWidget(presets)
        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("Eget:"))
        self._spin = QSpinBox()
        self._spin.setRange(1, 1_000_000)
        self._spin.setValue(self._current_rrf)
        self._spin.setStyleSheet("font-size:10px;")
        spin_row.addWidget(self._spin)
        outer.addLayout(spin_row)

        # Keep category applicability separate from the retired per-cause
        # selection.  Each category assessment owns its own exclusion set,
        # so changing a checkbox must preserve exclusions for other
        # safeguards in that same category.
        if self._sev_cat_list:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color:#E2E3E1;")
            outer.addWidget(sep)
            applies_label = QLabel("Gäller för konsekvenskategori:")
            applies_label.setStyleSheet("border:none;color:#5B616B;font-size:9px;")
            outer.addWidget(applies_label)
            for severity_id, category_name in self._sev_cat_list:
                try:
                    excluded = self.db.get_severity_excluded_sgs(severity_id) or set()
                except Exception:
                    excluded = set()
                check = QCheckBox(str(category_name))
                check.setStyleSheet("border:none;font-size:10px;color:#17191C;")
                check.setChecked(self._sg_id not in excluded)
                self._cat_checks[severity_id] = check
                outer.addWidget(check)

        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{border:none;font-size:10px;padding:3px 12px;"
            "background:#2F5FD0;color:white;border-radius:0px;}"
            "QPushButton:hover{background:#3D6BD8;}")
        ok.clicked.connect(self._ok)
        outer.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)

    def _ok(self):
        with self.db.history_group():
            return self._ok_inner()

    def _ok_inner(self):
        new_rrf  = self._spin.value()
        new_type = self._type_combo.currentText()
        if new_rrf != self._current_rrf or new_type != self._current_type:
            self.db.update_safeguard(self._sg_id, rrf=new_rrf, sg_type=new_type)

        # Persist only this safeguard's membership in each category.  Do
        # not touch safeguard_cause_exclusions: the per-cause feature was
        # intentionally removed and old stored values remain undisturbed.
        for severity_id, check in self._cat_checks.items():
            excluded = set(self.db.get_severity_excluded_sgs(severity_id) or ())
            if check.isChecked():
                excluded.discard(self._sg_id)
            else:
                excluded.add(self._sg_id)
            self.db.set_severity_excluded_sgs(severity_id, excluded)

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
        add_mini_popup_close_button(self)

    def _build(self):
        excluded = self.db.get_severity_excluded_sgs(self._sev_id)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        title = QLabel("Barriärer för detta scenario")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        note = QLabel("Bocka ur barriärer som inte gäller denna kategori.")
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
            "background:#2F5FD0;color:white;border-radius:0px;}"
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
        add_mini_popup_close_button(self)

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
            "background:#2F5FD0;color:white;border-radius:0px;}"
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
                    "border:2px solid #2F5FD0;border-radius:0px;"
                    "font-size:9px;font-weight:bold;}"
                    "QPushButton:hover{background:#3D6BD8;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;"
                "border:1px solid #CFD1CE;border-radius:0px;font-size:9px;}"
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
        with self.db.history_group():
            for cat_id, sev in self._sel.items():
                self.db.set_consequence_severity(self._cons_id, cat_id, sev)
        self.accept()


class _LopaWidget(QWidget):
    """One compact RRF-like button summarising a consequence's enablers."""

    def __init__(self, cons_id: int, n_active: int, total_rrf, parent=None):
        super().__init__(parent)
        self.cons_id  = cons_id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(0)
        self._extra_btn = QPushButton()
        self._extra_btn.setObjectName('enablerSummaryButton')
        self._extra_btn.setFixedHeight(22)
        # This is a cell action, not a keyboard-focus target.  Letting the
        # embedded button take focus makes QTableWidget ensure its row is
        # visible, which can make the surrounding HAZOP protocol jump when
        # the user only wants to open the enabler popup.
        self._extra_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._selected = False
        self._extra_btn.setToolTip('Klicka för att välja enablers och deras RRF.')
        lay.addWidget(self._extra_btn)
        self.setFixedHeight(24)
        self._apply_button_style()
        self.update_summary(n_active, total_rrf)

    def _apply_button_style(self):
        background = (_SCENARIO_SELECTION_BG if self._selected
                      else '#F5F5F3')
        hover = ('#C8CCC8' if self._selected else '#E8E9E6')
        self._extra_btn.setStyleSheet(
            f'QPushButton#enablerSummaryButton{{background:{background};'
            'color:#17191C;border:none;font-size:9px;font-weight:bold;'
            f'padding:2px 4px;}}'
            f'QPushButton#enablerSummaryButton:hover{{background:{hover};}}')

    def set_selected(self, selected):
        selected = bool(selected)
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_button_style()

    @staticmethod
    def _format_rrf(value):
        try:
            return f'{float(value):.6g}'
        except (TypeError, ValueError):
            return '1'

    def update_summary(self, n_active: int, total_rrf):
        self._extra_btn.setText(f'{int(n_active)} ({self._format_rrf(total_rrf)})')

class ScenarioTablePanel(QWidget):
    """Extended scenario table with unified enablers and final consequence."""

    item_selected              = pyqtSignal(int, int)   # (type_, id_) — cell clicked → open right panel
    new_item_created           = pyqtSignal(int, int)   # (type_, id_) — after quick-add via Enter menu
    item_edited                = pyqtSignal(int, int)   # (type_, id_) — cell edit committed → sync right panel
    structure_changed          = pyqtSignal()           # item moved/deleted/duplicated → refresh tree
    equipment_renamed          = pyqtSignal()           # a shared object-tag popup renamed a catalogue object
    bind_cause_to_pid_requested = pyqtSignal(int)      # choose an existing P&ID object
    bind_secondary_cause_to_pid_requested = pyqtSignal(int)  # group affected object
    place_cause_object_requested = pyqtSignal(int, str, str)  # cause_id, type, tag

    # Column indices
    _C_NOD, _C_UTR, _C_DEV, _C_ORS, _C_KON, _C_RFORE = 0, 1, 2, 3, 4, 5
    _C_SG, _C_LOPA, _C_SLUT, _C_REK                   = 6, 7, 8, 9

    _COLS = [
        'Nod',
        'Utrustning',
        'Avvikelse',
        'Orsak (frekvens)',
        'Konsekvens',
        'Risk före barriär',
        'Barriärer (RRF)',
        'Enablers',
        'Risker efter barriärer',
        'Rekommendation',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        # Scenario and Worksheet share this table implementation.  Bars are
        # the default presentation; Settings can switch both risk columns to
        # fully filled matrix-colour cells without changing their values.
        self._risk_bars_enabled = (
            self.db.get_config('scenario_risk_bars_enabled', '1') == '1')
        self.cause_id = None
        self._node_id = None
        self._deviation_id = None
        self._all_nodes = False  # if True, show every node's full hierarchy (set by load_all)
        self._equipment_filter_id = None  # if set, show only causes mentioning this equipment_catalog id (set by load_equipment)
        self._show_empty_deviations = False  # if True, deviations with zero causes get a placeholder row instead of being omitted
        self._force_dev_column_visible = False  # if True, Avvikelse column stays visible regardless of _all_nodes (set by always_show_deviation_column)
        self._force_utr_column_hidden = False  # if True, Utrustning column stays hidden regardless of _all_nodes (set by hide_equipment_column)
        self._hide_unplaced_tag = False
        self._merge_node_labels = False
        self._office_clipboard_title = 'HAZOP Scenario'
        # Kept as a compatibility attribute for hosts that used to opt out of
        # the empty-consequence chain popup. Double-click now always means
        # inline edit, in Scenario as well as Worksheet; the chain editor is
        # available only from the explicit context-menu action.
        self._empty_consequence_chain_popup_enabled = False
        self._row_meta = []   # list of (dev_id, cause_id, cons_id, sg_id) per visible row
        self._row_recommendation_ids = []
        self._row_plus_cols = {}
        self._cons_id  = None  # if set, show only this consequence (set by load_consequence)
        self._enter_row = -1
        self._enter_col = -1
        self._last_enter_committed = False
        self._double_click_edit = None  # (row, col, viewport position)
        self._empty_cause_click_target = None
        self._empty_cause_click_timer = QTimer(self)
        self._empty_cause_click_timer.setSingleShot(True)
        self._empty_cause_click_timer.setInterval(250)
        self._empty_cause_click_timer.timeout.connect(
            self._open_pending_empty_cause_editor)
        # Set while the blank REK editor below saved recommendations is
        # active; its text must create a sibling, never overwrite the sole
        # existing recommendation.
        self._recommendation_force_add_cons_id = None
        # Keep the next empty recommendation row visible while it is being
        # entered, instead of showing only a reused spanning cell.
        self._recommendation_add_placeholder_cons_id = None
        # Set only while Enter is committing a REK editor. Recommendation
        # saves rebuild their physical rows, so restore the edited row's
        # selection after that rebuild rather than leaving focus nowhere.
        self._recommendation_selection_after_commit = None
        # A physical recommendation row follows the same deliberate
        # select-then-edit interaction as a safeguard row. The table has
        # already moved its current index when cellClicked is emitted, so we
        # retain the preceding REK click explicitly rather than treating a
        # first click as an edit request.
        self._last_recommendation_click = None
        # Tags explicitly disconnected by the user must remain ordinary prose
        # even though their text still matches the P&ID catalogue.
        self._detached_tags = set()
        self._cell_font_size = 9
        # Parallel list to _row_meta: None or (cat_id, cat_name, cat_sev)
        self._row_cat_info: list = []
        # Keep the table usable at its smallest size, but let the surrounding
        # splitter decide how much of the window it may use.
        self.setMinimumHeight(110)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 1, 3, 1)
        outer.setSpacing(1)

        hdr_row = QHBoxLayout()
        self._hdr_lbl = QLabel("HAZOP Scenario")
        f = QFont(); f.setBold(True); f.setPointSize(8)
        self._hdr_lbl.setFont(f)
        hdr_row.addWidget(self._hdr_lbl)
        hdr_row.addStretch()
        # Retained as a non-visible compatibility object for older host code;
        # width filling is automatic and this control is never added to the UI.
        self._fill_btn = QPushButton("Fyll bredd")
        self._fill_btn.hide()
        self._fill_btn.setIcon(_icon('resize-horizontal'))
        self._fill_btn.setToolTip(
            "Fördela om Orsak/Konsekvens/Barriärer-kolumnerna så de fyller "
            "hela bredden just nu — kolumnerna går alltid att dra i")
        self._fill_btn.clicked.connect(self._fill_width_once)
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
            self._C_RFORE: (QHeaderView.ResizeMode.Interactive,
                            _RISK_COLUMN_DEFAULT_WIDTH),
            self._C_SG:    (QHeaderView.ResizeMode.Interactive, 130),
            self._C_LOPA:  (QHeaderView.ResizeMode.Interactive, 130),
            self._C_SLUT:  (QHeaderView.ResizeMode.Interactive,
                            _RISK_COLUMN_DEFAULT_WIDTH),
            self._C_REK:   (QHeaderView.ResizeMode.Interactive, 140),
        }
        for col, (mode, width) in resize_modes.items():
            h.setSectionResizeMode(col, mode)
            self._table.setColumnWidth(col, width)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Allow the user to extend a field selection with Shift before
        # dragging. Normal clicks still select one cell; the existing inline
        # editing and drop handling remain unchanged.
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems)
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
            "QTableWidget{border-radius:0px;}"
            "QTableWidget::item{padding:2px 3px;border:none;}"
            f"QTableWidget::item:selected{{background:{_SCENARIO_SELECTION_BG};"
            f"color:{_SCENARIO_SELECTION_FG};border:none;}}"
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;"
            "font-weight:600;padding:3px;border-radius:0px;}")
        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.itemDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.currentCellChanged.connect(self._on_current_cell_changed)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setAcceptDrops(True)
        self._table.installEventFilter(self)
        self._table.viewport().installEventFilter(self)
        # QTableWidget has NoEditTriggers because the panel starts editors
        # explicitly (double-click/Enter).  That also means Qt does not
        # reliably close an active editor when the user clicks another part
        # of the application.  Listen at application level so a click in a
        # sibling panel, header or empty area can finish the edit and its
        # helper popup too.  The filter is deliberately installed once per
        # panel and is removed automatically with this QObject.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
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
        self._col_widths_user_set = False   # flipped by _on_column_resized
        self._applying_fill_width = False
        saved_widths = self.db.get_config('scenario_col_widths', '')
        if saved_widths:
            try:
                for col_str, w in json.loads(saved_widths).items():
                    col = int(col_str)
                    if 0 <= col < self._table.columnCount():
                        self._table.setColumnWidth(col, w)
            except Exception:
                pass
        else:
            # No saved widths yet (fresh install, or before the user has
            # ever manually resized a column) — "↔ Fyll bredd" behaves as
            # the DEFAULT starting layout instead of requiring a manual
            # click every session (2026-08-26, Anton: "kanppen fyll bredd
            # är ikryssad per default när programmet startar"). Deferred
            # via QTimer.singleShot(0, ...) since the table has no real
            # viewport width yet at construction time, before this widget
            # has been placed in a shown parent/laid out — _fill_width_once
            # needs that width to compute an actual fill, not just its
            # 60px-minimum fallback. Guarded by _col_widths_user_set
            # (checked, not just decided here at schedule-time) because
            # anything — a real drag, or a caller that resizes a column
            # programmatically — could set a real width in the gap
            # between scheduling and this actually firing; that must win
            # over silently overwriting it a moment later.
            pass
        h.sectionResized.connect(self._on_column_resized)
        # Auto-fill is the default for a fresh layout.  It is deferred until
        # the panel has a real viewport width; otherwise the first call can
        # calculate from a construction-time width and appear ineffective.
        self._auto_fill_pending = True
        QTimer.singleShot(0, self._fill_width_once)

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

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fill_width_once)

    def _on_current_cell_changed(self, current_row, current_col,
                                 previous_row, previous_col):
        """Mirror Scenario's flat selection on the embedded enabler bar."""
        for row in {previous_row, current_row}:
            if row is None or row < 0:
                continue
            widget = self._table.cellWidget(row, self._C_LOPA)
            if isinstance(widget, _LopaWidget):
                widget.set_selected(
                    row == current_row and current_col == self._C_LOPA)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the former "Fyll bredd" behavior active continuously.
        QTimer.singleShot(0, self._fill_width_once)

    def allow_full_height(self):
        """Lift the 380px cap so this panel fills whatever container it's
        placed in — for hosts that give it a whole page (e.g. HAZOPWorksheet),
        as opposed to the P&ID page's bottom splitter (which relies on the cap
        to leave room for the canvas above it)."""
        self.setMaximumHeight(16777215)  # Qt's QWIDGETSIZE_MAX — effectively "no cap"
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_office_clipboard_title(self, title):
        """Set the descriptive title used in rich Office clipboard output."""
        self._office_clipboard_title = str(title or 'HAZOP Scenario')

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
        # The old Utrustning presentation column is retired.  Keep the
        # internal compatibility column hidden in every Scenario view.
        self._table.setColumnHidden(self._C_UTR, True)

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

    def hide_unplaced_tag(self):
        """Hide the synthetic 'ej på P&ID' label in worksheet hosts."""
        self._hide_unplaced_tag = True

    def merge_node_labels(self):
        """Merge adjacent rows with the same displayed node name."""
        self._merge_node_labels = True

    def set_empty_consequence_chain_popup_enabled(self, enabled):
        """Compatibility no-op for the retired double-click chain popup.

        A double-click is deliberately consistent for all text fields: it
        opens the inline editor. The chain editor remains an explicit
        right-click action, never a hidden alternate double-click path.
        """
        self._empty_consequence_chain_popup_enabled = False

    # Columns that stretch to fill remaining space in fill mode
    _STRETCH_COLS = None  # set after class constants are known

    def _fill_width_once_unless_user_set(self):
        """Guard for the deferred auto-fill-at-startup call scheduled in
        __init__ (2026-08-26) — only runs _fill_width_once() if nothing
        has resized a column (a real drag, or any other programmatic
        setColumnWidth call) in the gap between __init__ scheduling this
        and the event loop actually running it. _on_column_resized flips
        _col_widths_user_set the instant anything does."""
        if not self._col_widths_user_set:
            self._fill_width_once()

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
        if self._table.viewport().width() <= 0:
            return
        other_cols = [c for c in range(self._table.columnCount())
                      if c not in stretch_cols and not self._table.isColumnHidden(c)]
        used = sum(self._table.columnWidth(c) for c in other_cols)
        available = max(0, self._table.viewport().width() - used)
        per_col = max(60, available // len(stretch_cols))
        # The ORS cell contains an inline bold equipment tag.  Allowing the
        # fill operation to shrink it to the generic 60 px floor makes tags
        # wrap/clip exactly when the user starts editing the cause.  Keep the
        # same practical minimums as the initial layout; a narrow window may
        # scroll horizontally, but it must never hide part of an identity tag.
        minimums = {
            self._C_ORS: 180,
            self._C_KON: 180,
            self._C_SG: 130,
        }
        per_col = max(per_col, max(minimums.values()))
        self._applying_fill_width = True
        try:
            for col in stretch_cols:
                self._table.setColumnWidth(col, per_col)
        finally:
            self._applying_fill_width = False
            self._auto_fill_pending = False

    def _on_column_resized(self, col, old_size, new_size):
        """Persist manually-resized column widths (2026-08-10, see
        NOTES.md) — only meaningful for Interactive columns ("Fyll skärm"
        unchecked), but harmless to also record Stretch/Fixed-driven
        resizes since they'd just re-save the same hardcoded defaults."""
        if self._applying_fill_width:
            return
        self._col_widths_user_set = True
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
                self._row_recommendation_ids = []
                self._row_cat_info = []
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
        # Functional group causes render both linked objects in the cause
        # sentence itself.  Do not repeat the primary tag in the prefix.
        if cause_d.get('secondary_equipment_id'):
            return '', ''
        eq_id = cause_d.get('equipment_id')
        if eq_id:
            eq = self.db.get_equipment_by_id(eq_id)
            if eq:
                return eq.get('equipment_type') or '', eq.get('tag') or ''
        return cause_d.get('comp_type') or '', cause_d.get('comp_tag') or ''

    def _node_number(self, node_id):
        cached = getattr(self, '_display_cache', {}).get('node_numbers', {})
        if node_id in cached:
            return cached[node_id]
        for i, node in enumerate(self.db.nodes(), 1):
            if node['id'] == node_id:
                return i
        return None

    def _deviation_number(self, node_id, deviation_id):
        if not node_id or not deviation_id:
            return None
        cached = getattr(self, '_display_cache', {}).get('deviation_numbers', {})
        if deviation_id in cached:
            return cached[deviation_id]
        # The tree displays deviations with the same guide-word text as one
        # row.  An object assigned through the left-click deviation checklist
        # may create a same-text, equipment-specific sibling; it must share
        # the visible number rather than shifting all later numbers.
        numbers_by_description = {}
        for dev in self.db.deviations(node_id):
            description = dev['description']
            if description not in numbers_by_description:
                numbers_by_description[description] = len(numbers_by_description) + 1
            if dev['id'] == deviation_id:
                return numbers_by_description[description]
        return None

    def _numbered_node(self, node_id, name):
        n = self._node_number(node_id)
        return f"{n}.  {name}" if n else name

    def _numbered_deviation(self, node_id, deviation_id, description):
        n = self._deviation_number(node_id, deviation_id)
        return f"{n}.  {description}" if n else description

    def _child_number(self, kind, parent_id, item_id):
        if not parent_id or not item_id:
            return None
        cached = getattr(self, '_display_cache', {}).get('child_numbers', {})
        cached_number = cached.get(kind, {}).get(item_id)
        if cached_number is not None:
            return cached_number
        if kind == 'cause':
            rows = self.db.causes_for_deviation(parent_id)
        elif kind == 'consequence':
            rows = self.db.consequences(parent_id)
        else:
            grouped = self.db.safeguards_for_consequences([parent_id])
            rows = grouped.get(parent_id, [])
        return next((i + 1 for i, r in enumerate(rows) if r['id'] == item_id), None)

    def _causes_for_node(self, node_id):
        """Return [(cause_dict, deviation_dict), ...] for every cause under
        every deviation of node_id, in deviation/cause order. Used by the
        single-node branch of _build_rows() — see _causes_for_all_nodes()
        for the "all nodes" mode's bulk equivalent (2026-08-24, NOTES.md:
        calling this once per node there used to mean one query per node
        PLUS one query per deviation, on every "Visa samtliga noder")."""
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

    def _causes_for_all_nodes(self):
        """Bulk equivalent of calling _causes_for_node() once per node —
        identical (cause_dict, deviation_dict) list shape and node/
        deviation/cause ordering, but built from 2 batched queries
        (Database.deviations_for_nodes/causes_for_deviations, the same
        bulk helpers TreePanel.refresh() uses — see NOTES.md "Ny toppnivå
        System"/2026-08-24 follow-up) instead of one query PER NODE and
        one PER DEVIATION. Used by _build_rows()'s "all nodes" branch."""
        result = []
        nodes = list(self.db.nodes())
        devs_by_node = self.db.deviations_for_nodes(n['id'] for n in nodes)
        all_dev_ids = [d['id'] for devs in devs_by_node.values() for d in devs]
        causes_by_dev = self.db.causes_for_deviations(all_dev_ids)
        for node in nodes:
            for dev in devs_by_node.get(node['id'], []):
                dev_d = dict(dev)
                causes = list(causes_by_dev.get(dev['id'], []))
                if not causes:
                    if self._show_empty_deviations:
                        result.append((None, dev_d))
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
        # Clear a previous rebuild's lookup data before any placeholder
        # branch can paint a different scope.
        self._display_cache = {}
        # Build list of (cause_dict, deviation_dict) to display
        causes_to_show = []
        if self._all_nodes:
            causes_to_show.extend(self._causes_for_all_nodes())
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
                self._hdr_lbl.setText("HAZOP Scenario")
            elif self._equipment_filter_id is not None:
                # No causes mention this equipment yet — nothing sensible to
                # show as a placeholder (no single deviation to attach it to).
                equip = self.db.get_equipment_by_id(self._equipment_filter_id)
                tag = equip.get('tag', '?') if equip else '?'
                self._hdr_lbl.setText("HAZOP Scenario")
                self._hdr_lbl.setToolTip(f"Objekt: {tag} (inga orsaker än)")
            elif self._deviation_id is not None:
                dev = self.db.get_deviation(self._deviation_id)
                if dev:
                    dev_d = dict(dev)
                    node  = self.db.get_node(dev_d['node_id'])
                    nn    = node['name'] if node else '?'
                    self._hdr_lbl.setText("HAZOP Scenario")
                    self._hdr_lbl.setToolTip(f"{nn} / {dev_d['description']}")
                    self._add_placeholder_row(nn, dev_d)
            elif self._node_id is not None:
                node = self.db.get_node(self._node_id)
                nn   = node['name'] if node else '?'
                self._hdr_lbl.setText("HAZOP Scenario")
                self._hdr_lbl.setToolTip(nn)
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
            self._hdr_lbl.setText("HAZOP Scenario")
        elif self._equipment_filter_id is not None:
            equip = self.db.get_equipment_by_id(self._equipment_filter_id)
            tag = equip.get('tag', '?') if equip else '?'
            self._hdr_lbl.setText("HAZOP Scenario")
            self._hdr_lbl.setToolTip(f"Objekt: {tag}")
        elif self._cons_id is not None:
            cons = self.db.get_consequence(self._cons_id)
            cons_desc = cons['description'] if cons else '?'
            _first_desc = first_cause.get('description', '?') if first_cause is not None else '?'
            self._hdr_lbl.setText("HAZOP Scenario")
            self._hdr_lbl.setToolTip(f"{node_name_hdr} / {_first_desc} / {cons_desc}")
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            self._hdr_lbl.setText("HAZOP Scenario")
            self._hdr_lbl.setToolTip(f"{node_name_hdr} / {dev['description'] if dev else ''}")
        elif self.cause_id is not None:
            self._hdr_lbl.setText("HAZOP Scenario")
            self._hdr_lbl.setToolTip(f"{node_name_hdr} / {first_cause.get('description', '?')}")
        elif self._node_id is not None:
            self._hdr_lbl.setText("HAZOP Scenario")
            self._hdr_lbl.setToolTip(node_name_hdr)
        else:
            self._hdr_lbl.setText("HAZOP Scenario")
            self._hdr_lbl.setToolTip(node_name_hdr)

        logging.info('_build_rows: G0 — header set (%r)', self._hdr_lbl.text())
        self.refresh_placed()
        logging.info('_build_rows: G1 — refresh_placed done, entering cause loop (n=%d)',
                     len(causes_to_show))

        # Prefetch the whole cause → consequence → (severities, safeguards)
        # → exclusions chain for every cause in causes_to_show, in a
        # handful of batched queries instead of one query PER cause, PER
        # consequence, PER category row, and PER safeguard (2026-08-24,
        # see NOTES.md) — this loop runs on nearly every scenario-table
        # refresh, so a study with many causes/consequences used to issue
        # a query storm here. Behavior-identical to the old per-id calls
        # (same row shape/order) — only the data SOURCE changed, none of
        # the row-building logic below.
        nodes_by_id = {n['id']: dict(n) for n in self.db.nodes()}
        # The visible numeric prefixes are requested for every rendered
        # cell.  Keep their source data alongside the existing row-data
        # prefetches so displaying a large study never turns those labels
        # into a new N+1 query path.
        all_devs_by_node = self.db.deviations_for_nodes(nodes_by_id)
        all_dev_ids = [d['id'] for devs in all_devs_by_node.values() for d in devs]
        all_causes_by_dev = self.db.causes_for_deviations(all_dev_ids)
        node_numbers = {node_id: i for i, node_id in
                        enumerate(nodes_by_id, 1)}
        deviation_numbers = {}
        for node_id, deviations in all_devs_by_node.items():
            numbers_by_description = {}
            for deviation in deviations:
                description = deviation['description']
                if description not in numbers_by_description:
                    numbers_by_description[description] = len(numbers_by_description) + 1
                deviation_numbers[deviation['id']] = numbers_by_description[description]
        cause_numbers = {
            cause['id']: i + 1
            for causes in all_causes_by_dev.values()
            for i, cause in enumerate(causes)
        }
        _real_cause_ids = [cd['id'] for cd, _ in causes_to_show if cd is not None]
        cons_by_cause = self.db.consequences_for_causes(_real_cause_ids)
        _all_cons_ids = [dict(c)['id'] for conss in cons_by_cause.values() for c in conss]
        sgs_by_cons = self.db.safeguards_for_consequences(_all_cons_ids)
        cat_rows_by_cons = self.db.get_consequence_severities_for_consequences(_all_cons_ids)
        final_severities_by_cons = \
            self.db.get_final_consequence_severities_for_consequences(_all_cons_ids)
        _all_severity_ids = [dict(r)['id'] for rows in cat_rows_by_cons.values() for r in rows]
        excl_sgs_by_severity = self.db.get_severity_excluded_sgs_for_severities(_all_severity_ids)
        excl_enablers_by_severity = \
            self.db.get_severity_excluded_reduction_factors_for_severities(_all_severity_ids)
        _all_sg_ids = [dict(s)['id'] for sgs in sgs_by_cons.values() for s in sgs]
        excl_causes_by_sg = self.db.get_safeguard_excluded_causes_for_safeguards(_all_sg_ids)
        # _add_row() used to re-fetch reduction_factors(cons_d['id']) once
        # per RENDERED ROW — several rows share the same consequence
        # (n_rows = max(n_cats, n_sgs, 1)) and therefore the same
        # reduction factors, so this was pure repeated work.
        rfs_by_cons = self.db.reduction_factors_for_consequences(_all_cons_ids)
        # _add_row() also used to re-fetch recommendations_for_consequence(cid)
        # once per RENDERED ROW (same reasoning as reduction_factors above) and
        # get_safeguard_excluded_causes(sg['id']) once per row even
        # though excl_causes_by_sg (above) already has it precomputed.
        acts_by_cons = self.db.recommendations_for_consequences(_all_cons_ids)
        self._display_cache = {
            'node_numbers': node_numbers,
            'deviation_numbers': deviation_numbers,
            'child_numbers': {
                'cause': cause_numbers,
                'consequence': {
                    consequence['id']: i + 1
                    for consequences in cons_by_cause.values()
                    for i, consequence in enumerate(consequences)
                },
                'safeguard': {
                    safeguard['id']: i + 1
                    for safeguards in sgs_by_cons.values()
                    for i, safeguard in enumerate(safeguards)
                },
            },
            'causes': {
                cause['id']: dict(cause)
                for causes in all_causes_by_dev.values()
                for cause in causes
            },
        }

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
            # Every cause row owns its visible object tag. Do not suppress a
            # repeated tag on the following row: two causes may be separate
            # records even when they refer to the same object (for example
            # SPADE on cause 3 and cause 4).
            _repeats_previous_tag = False
            _prev_tag_display = _tag_display
            if _cause_idx % 10 == 0 or _cause_idx == len(causes_to_show) - 1:
                logging.info('_build_rows: G2 — cause loop iter %d/%d (cause_id=%s)',
                             _cause_idx, len(causes_to_show), cause_d.get('id'))
            node = nodes_by_id.get(cause_d['node_id'])
            node_name = node['name'] if node else '?'
            # A directly-created blank cause keeps the database's required
            # likelihood value for calculations, but must not show a chosen
            # frequency badge until the user selects one.
            frequency_unset = (bool(cause_d.get('frequency_cleared')) or
                               (not (cause_d.get('description') or '').strip()
                               and not (cause_d.get('comp_tag') or '').strip()
                               and cause_d.get('base_frequency') is None
                               and not cause_d.get('standard_cause_id')
                               and cause_d.get('likelihood') == 0))
            freq = self.db.cause_frequency_level(cause_d)
            if frequency_unset:
                cause_d['_frequency_unset'] = True
            _fi = freq_to_idx(freq)
            freq_lbl = ('' if frequency_unset else
                        (FREQ_LABELS[_fi] if _fi < len(FREQ_LABELS) else f'F{freq}'))
            first_row_for_cause = self._table.rowCount()
            all_cons = list(cons_by_cause.get(cause_d['id'], []))
            # Status icon inputs (feature 5, ORS column) depend on ALL of
            # the cause's consequences, not the possibly cons_id-filtered
            # subset used for row-building below — computed once per
            # cause here instead of once per RENDERED ROW inside
            # _add_row (2026-08-24, see NOTES.md).
            _status_cons = [dict(c) for c in cons_by_cause.get(cause_d['id'], [])]
            cause_status = (
                len(_status_cons) > 0,
                any((c.get('severity') or 0) > 0 for c in _status_cons),
                any(sgs_by_cons.get(c['id']) for c in _status_cons),
            )
            if self._cons_id is not None:
                all_cons = [c for c in all_cons if dict(c)['id'] == self._cons_id]
            for _cons_idx, cons in enumerate(all_cons):
                cons_d = dict(cons)
                logging.info('_build_rows: H0 — cause %s cons_idx %d/%d cons_id=%s',
                             cause_d.get('id'), _cons_idx, len(all_cons), cons_d.get('id'))
                sgs    = [dict(s) for s in sgs_by_cons.get(cons_d['id'], [])]
                cat_rows = [dict(r) for r in
                            cat_rows_by_cons.get(cons_d['id'], [])]
                n_cats = len(cat_rows)
                n_sgs  = len(sgs)
                n_recs = len(acts_by_cons.get(cons_d['id'], []))
                # Safeguards, recommendations and category assessments share
                # the same physical row grid. A trailing empty REK row used
                # to add one unnecessary visual band to every consequence;
                # Enter can instead reuse the visible REK cell in add mode.
                extra_recommendation_row = (
                    1 if self._recommendation_add_placeholder_cons_id == cons_d['id']
                    else 0)
                # Category assessments belong inside the risk block; they
                # must not create empty safeguard rows.  The risk columns
                # span the complete consequence block below.
                n_rows = max(n_sgs, n_recs + extra_recommendation_row, n_cats, 1)

                # Precompute exclusions per severity assessment
                cat_excl_map = {}           # sev_id → set of excluded sg_ids
                cat_enabler_excl_map = {}   # sev_id → set of excluded enabler ids
                for _cr in cat_rows:
                    cat_excl_map[_cr['id']] = excl_sgs_by_severity.get(_cr['id'], set())
                    cat_enabler_excl_map[_cr['id']] = \
                        excl_enablers_by_severity.get(_cr['id'], set())

                # Which safeguards are excluded from at least one category?
                any_excl_map = {}           # sg_id → list of category names
                for _sg in sgs:
                    any_excl_map[_sg['id']] = [
                        _cr['name'] for _cr in cat_rows
                        if _sg['id'] in cat_excl_map.get(_cr['id'], set())]

                # Which safeguards are excluded from this specific cause?
                cause_excl_sgs = set()
                for _sg in sgs:
                    excl_causes = excl_causes_by_sg.get(_sg['id'], set())
                    if cause_d['id'] in excl_causes:
                        cause_excl_sgs.add(_sg['id'])

                # Category list for the RRF popup: [(sev_id, cat_name), ...]
                sev_cat_list = [(cr['id'], cr['name']) for cr in cat_rows]
                # Full category info for stacked badges in KON cell
                all_cat_infos = [(cr['category_id'], cr['id'],
                                  cr['name'], cr['severity']) for cr in cat_rows]
                # Cause list for the RRF popup — cons_d['cause_id'] is
                # always cause_d['id'] here (cons_d came from
                # cons_by_cause[cause_d['id']]), so this is the same row
                # already in scope; re-fetching it via get_cause() per
                # consequence was a pure redundant query (2026-08-24, see
                # NOTES.md).
                _direct_cause = dict(cause_d) if cons_d.get('cause_id') else None
                cause_popup_list = []
                if _direct_cause:
                    cause_popup_list.append((dict(_direct_cause)['id'],
                                             dict(_direct_cause)['description'], False))

                logging.info('_build_rows: H1 — cons_id=%s about to add %d row(s) '
                             '(n_cats=%d n_sgs=%d)',
                             cons_d.get('id'), n_rows, n_cats, n_sgs)
                first_row_for_cons = self._table.rowCount()
                for i in range(n_rows):
                    # Keep safeguards in contiguous independent blocks when
                    # categories/recommendations make the shared grid taller.
                    # This extends the last safeguard's span instead of
                    # inventing a blank safeguard row.
                    sg_i = None
                    if n_sgs:
                        sg_index = min(n_sgs - 1, (i * n_sgs) // n_rows)
                        sg_i = sgs[sg_index]
                    rec_i   = (acts_by_cons.get(cons_d['id'], [])[i]
                               if i < n_recs else None)
                    # Categories and safeguards use the same physical row
                    # grid, but remain independent. Spread the category
                    # assessments across that grid so two categories become
                    # two visible risk rows even when one category spans more
                    # than one safeguard row.
                    cat_i = (min(n_cats - 1,
                                 (i * n_cats + n_rows - 1) // n_rows)
                             if n_cats else None)
                    cr_i    = cat_rows[cat_i] if cat_i is not None else None
                    cat_info_i = ((cr_i['category_id'], cr_i['id'],
                                   cr_i['name'], cr_i['severity'])
                                  if cr_i else None)
                    # A Slutkonsekvens override is optional. Without one,
                    # the exact same category severity as Risk före remains
                    # effective; it must never mutate that original value.
                    final_severity_i = (
                        final_severities_by_cons.get(cons_d['id'], {}).get(
                            cr_i['category_id'], cr_i['severity'])
                        if cr_i else cons_d.get('severity') or 1)
                    excl_for_cat  = cat_excl_map.get(cr_i['id'], set()) if cr_i else set()
                    excl_enablers_for_cat = (
                        cat_enabler_excl_map.get(cr_i['id'], set()) if cr_i else set())
                    excl_cat_names = any_excl_map.get(sg_i['id'], []) if sg_i else []
                    logging.info('_build_rows: H2 — _add_row cons_id=%s row_i=%d/%d '
                                 '(will create _LopaWidget)',
                                 cons_d.get('id'), i, n_rows)
                    self._add_row(node_name, dev_d, cause_d, freq, freq_lbl,
                                  cons_d, sgs, sg_i,
                                  cat_info=cat_info_i,
                                  final_severity=final_severity_i,
                                  excl_cat_names=excl_cat_names,
                                  excl_for_cat=excl_for_cat,
                                  cause_excl_sgs=cause_excl_sgs,
                                  sev_cat_list=sev_cat_list,
                                  all_cat_infos=all_cat_infos,
                                  cause_popup_list=cause_popup_list,
                                  n_cats=n_cats,
                                  repeats_previous_tag=_repeats_previous_tag,
                                  cause_status=cause_status,
                                  rfs=rfs_by_cons.get(cons_d['id'], []),
                                  acts=acts_by_cons.get(cons_d['id'], []),
                                  recommendation=rec_i,
                                  excl_causes_by_sg=excl_causes_by_sg,
                                  excl_enablers_for_cat=excl_enablers_for_cat)
                    logging.info('_build_rows: H3 — _add_row cons_id=%s row_i=%d done',
                                 cons_d.get('id'), i)
            if self._table.rowCount() == first_row_for_cause:
                logging.info('_build_rows: G3 — cause %s had no rows, adding empty row',
                             cause_d.get('id'))
                self._add_empty_row(node_name, dev_d, cause_d, freq, freq_lbl,
                                    repeats_previous_tag=_repeats_previous_tag)
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
            self._table.item(r, self._C_NOD).text() if self._merge_node_labels
            else self._table.item(r, self._C_NOD).data(Qt.ItemDataRole.UserRole)
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

        # KON and LOPA always span by consequence. REK spans too while it
        # has zero or one entry; two or more entries keep their own rows.
        # During sequential recommendation entry an extra physical row is
        # deliberately materialised. That blank editor row must remain a
        # separate REK cell even when there is only one saved recommendation;
        # otherwise the existing cell's span paints over the editor row.
        for col in (self._C_KON, self._C_LOPA):
            _span_col(col, lambda r: _meta(r, 2))

        rec_counts = {}
        for r in range(n):
            cons_id = _meta(r, 2)
            if cons_id is not None and self._row_recommendation_ids[r] is not None:
                rec_counts[cons_id] = rec_counts.get(cons_id, 0) + 1
        def _rek_key(r):
            cons_id = _meta(r, 2)
            if (cons_id == self._recommendation_add_placeholder_cons_id and
                    cons_id is not None):
                rec_id = (self._row_recommendation_ids[r]
                          if r < len(self._row_recommendation_ids) else None)
                # Keep the active blank editor row as a single physical
                # cell. If the consequence has extra safeguard/category
                # rows, spanning all blank REK continuations makes Qt use
                # the span anchor for the editor and can place it above the
                # existing recommendation.
                return ('recommendation', rec_id) if rec_id is not None \
                    else ('recommendation_placeholder', cons_id, r)
            return (cons_id if rec_counts.get(cons_id, 0) <= 1 else None)

        _span_col(self._C_REK, _rek_key)
        logging.info('_apply_spans: J5 — KON/LOPA/REK columns spanned')

        # RFORE and SLUT follow the category rows. With two category
        # assessments the risk block therefore visibly splits into two
        # cells; if the consequence also needs more safeguard rows, each
        # category occupies its share of that same total height.
        def _risk_key(r):
            cons_id = _meta(r, 2)
            cat_info = (self._row_cat_info[r]
                        if r < len(self._row_cat_info) else None)
            if cons_id is None:
                return None
            if cat_info is None:
                return ('consequence', cons_id)
            return ('category', cons_id, cat_info[1])
        for col in (self._C_RFORE, self._C_SLUT):
            _span_col(col, _risk_key)
        # Safeguards have their own independent row blocks.  Repeated ids
        # are intentional when the shared grid is taller than the list.
        _span_col(self._C_SG, lambda r: _meta(r, 3))
        logging.info('_apply_spans: J6 — RFORE/SLUT/SG columns spanned, done')

    def _compute_row_height(self, row, fm=None):
        """The height `row` needs across EVERY column that can affect it —
        ORS/KON wrapped text, the fixed-height enabler summary button in the
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
        the enabler summary button below its own setFixedHeight(). This function is
        now the single source of truth for "how tall must this row be",
        used by both the full _resize_rows_manual() rebuild pass AND
        _update_row_text_only()'s single-row fast path, so the two can
        never again disagree about one row's height. Also folds in the ORS
        minimum-readable-height floor _resize_rows() used to apply in a
        SEPARATE pass reachable only from a full rebuild — the fast path
        never went through that pass at all, so a short ORS text edited via
        the fast path could previously end up below the floor too.

        NOD/UTR/DEV/ORS/KON/LOPA/REK/RFORE/SLUT are all spanned across a
        consequence's safeguard rows (_apply_spans), but every physical
        row still gets its OWN freshly-built item/widget from _add_row()
        regardless (setSpan/setCellWidget only change how Qt PAINTS
        covered cells, they don't clear or skip creating content there).
        The PAINTED area for a spanned cell is the union of every row in
        its group, so a shared requirement — the LOPA widget's fixed
        height, ORS/KON's wrapped-text height, the ORS readability floor —
        only needs to fit somewhere within that union; it does NOT need
        to fit inside any ONE row alone. This function therefore computes
        each shared requirement once, divides it evenly (ceiling) across
        however many physical rows its own span covers, and applies that
        SHARE identically to every row in the group (every row already
        carries the same duplicate item/widget, so each row can compute
        its own share independently without needing to special-case the
        anchor). 2026-08-19 follow-up ("Översta safeguarden blir 3 rader
        lång ... kopplad till FA, ant+övriga"): an earlier version instead
        measured each shared requirement ONLY on the anchor row and
        dumped its FULL height there — safeguard compaction then made the
        other rows in the group compact while the anchor alone absorbed
        the entire LOPA/ORS-floor requirement, looking disproportionately
        (~3 lines) tall next to its own now-compact siblings."""
        table = self._table
        if fm is None:
            fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6
        sg_row_h = self._sg_row_height(table.font())
        wrap_cols = (self._C_ORS, self._C_KON, self._C_SG, self._C_REK)

        def _cause_id(r):
            return self._row_meta[r][1] if 0 <= r < len(self._row_meta) else None
        def _cons_id(r):
            return self._row_meta[r][2] if 0 <= r < len(self._row_meta) else None
        rec_counts = {}
        for r in range(table.rowCount()):
            cons_id = _cons_id(r)
            rec_id = (self._row_recommendation_ids[r]
                      if r < len(self._row_recommendation_ids) else None)
            if cons_id is not None and rec_id is not None:
                rec_counts[cons_id] = rec_counts.get(cons_id, 0) + 1

        def _rek_span_key(r):
            """Match REK's actual span rule from _apply_spans().

            A single recommendation shares its consequence's full height,
            but two or more entries are separate physical rows.  Their
            wrapped text must therefore reserve height for itself rather
            than being divided across all recommendation rows.
            """
            cons_id = _cons_id(r)
            if cons_id is None:
                return None
            if rec_counts.get(cons_id, 0) <= 1:
                return ('consequence', cons_id)
            rec_id = (self._row_recommendation_ids[r]
                      if r < len(self._row_recommendation_ids) else None)
            return ('recommendation', rec_id if rec_id is not None else r)
        def _cat_info(r):
            return self._row_cat_info[r] if 0 <= r < len(self._row_cat_info) else None

        def _span_group_size(r, key_fn):
            """How many CONSECUTIVE physical rows share key_fn(r) — the
            same grouping _apply_spans' own _span_col walks forward to
            find, used here to divide a shared requirement evenly across
            that many rows instead of piling it onto just one."""
            key = key_fn(r)
            if key is None:
                return 1
            n = len(self._row_meta)
            start = r
            while start > 0 and key_fn(start - 1) == key:
                start -= 1
            end = r
            while end + 1 < n and key_fn(end + 1) == key:
                end += 1
            return end - start + 1

        def _share(total, group_n):
            return -(-total // group_n)   # ceiling division

        is_cons_anchor = row == 0 or _cons_id(row) != _cons_id(row - 1)

        # A second, third, ... safeguard row has no independent content of
        # its own in any column but SG only when it's a continuation for
        # BOTH the cons_id-keyed columns (KON/LOPA/REK) AND the finer
        # (cons_id, cat_info)-keyed ones (RFORE/SLUT) — a cat_info change
        # within the SAME consequence still means RFORE/SLUT has fresh
        # content this row. Such a row only needs the compact SG height
        # as its OWN baseline (shared requirements below can still lift
        # it further) — multiplying that saving across several
        # safeguards is the whole point (2026-08-18 follow-up: "krymper
        # höjden på safeguards ... för att spara plats när man lägger
        # till flera safeguards").
        is_pure_sg_continuation = (
            not is_cons_anchor and _cons_id(row) is not None
            and _cat_info(row) == _cat_info(row - 1))
        max_h = sg_row_h if is_pure_sg_continuation else one_line_h

        for col in range(table.columnCount()):
            if table.isColumnHidden(col):
                continue

            if col == self._C_LOPA:
                # LOPA spans by cons_id — divide its fixed height across
                # every row of that span instead of the whole thing
                # landing on one row (see this method's own docstring).
                widget = table.cellWidget(row, col)
                if widget is not None:
                    share = _share(widget.sizeHint().height(),
                                   _span_group_size(row, _cons_id))
                    if share > max_h:
                        max_h = share
                continue

            if col == self._C_SG:
                # SG does not span, but its description must wrap inside the
                # area left of the RRF badge just like the other text cells.
                item = table.item(row, col)
                text = item.text() if item is not None else ''
                if item is not None:
                    num = item.data(Qt.ItemDataRole.UserRole + 10)
                    if num:
                        text = f"{num}.  {text}"
                cell_w = max(40, table.columnWidth(col) - _RRF_W - 6)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                       Qt.TextFlag.TextWordWrap, text)
                h = max(sg_row_h, rect.height() + 4)
                if h > max_h:
                    max_h = h
                continue

            if col not in wrap_cols:
                # Fixed one-line columns (matches _ScenarioDelegate's
                # non-wrap branch) — no font-metric work needed.
                continue

            item = table.item(row, col)
            text = item.text() if item is not None else ''
            grouped_cause = (col == self._C_ORS and item is not None and
                             (item.data(Qt.ItemDataRole.UserRole + 9) or []))
            if not text and not grouped_cause:
                continue

            w = table.columnWidth(col)
            # ORS spans by cause_id (broader — a cause can have several
            # consequences); KON/REK span by cons_id.
            group_key_fn = (_cause_id if col == self._C_ORS else
                            _rek_span_key if col == self._C_REK else
                            _cons_id)
            if col == self._C_ORS:
                cell_w = max(40, w - 6 - _RRF_W)
                combined = self._ors_combined_text(item, text)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, combined)
                # A newly-created group has no description yet, but every
                # linked object still occupies its own explicit line.
                group_line_count = (len(item.data(Qt.ItemDataRole.UserRole + 9) or [])
                                    if grouped_cause else 1)
                h = max(one_line_h * max(1, group_line_count),
                        rect.height() + 4)
            elif col == self._C_KON:
                cell_w = max(40, w)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = max(one_line_h, rect.height() + 4)
            else:   # self._C_REK
                cell_w = max(40, w - 6)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = max(one_line_h, rect.height() + 4)
            share = _share(h, _span_group_size(row, group_key_fn))
            if share > max_h:
                max_h = share

        ors_item = table.item(row, self._C_ORS)
        if ors_item and (ors_item.text() or
                         (ors_item.data(Qt.ItemDataRole.UserRole + 9) or [])):
            group_line_count = len(ors_item.data(Qt.ItemDataRole.UserRole + 9) or [])
            min_ors = fm.height() * max(2, group_line_count) + 20
            share = _share(min_ors, _span_group_size(row, _cause_id))
            if max_h < share:
                max_h = share
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
        # The ORS minimum-readable-height floor (~2 lines) used to be
        # re-applied here in a SEPARATE pass after _resize_rows_manual()
        # — that's gone now (2026-08-19, see _compute_row_height's own
        # docstring): _compute_row_height already divides that floor
        # (and every other shared/spanned requirement — LOPA's fixed
        # height, ORS/KON's wrapped-text height) evenly across each
        # requirement's own row-span and applies the resulting SHARE to
        # every row in the group. A separate pass re-applying the FULL,
        # undivided floor here would have re-inflated the anchor row
        # right back up, undoing that distribution.
        logging.info('_resize_rows: K2 — row-height pass done, restoring scroll position')
        self._table.setUpdatesEnabled(True)
        # Recalculate the viewport before restoring the saved position.
        # During a structural edit (especially adding a safeguard), Qt may
        # otherwise still report a zero scrollbar range while updates are
        # disabled. Setting the old value then silently clamps to zero and
        # makes the page jump to the top.
        self._table.doItemsLayout()
        vbar = self._table.verticalScrollBar()
        hbar = self._table.horizontalScrollBar()
        vbar.setValue(max(vbar.minimum(), min(vscroll_value, vbar.maximum())))
        hbar.setValue(max(hbar.minimum(), min(hscroll_value, hbar.maximum())))
        logging.info('_resize_rows: K3 — done (setUpdatesEnabled True)')

    def _add_placeholder_row(self, node_name, dev_d):
        """Empty row shown when a node/deviation has no causes yet."""
        r = self._table.rowCount()
        self._table.insertRow(r)
        dev_id = dev_d['id'] if dev_d else None
        self._row_meta.append((dev_id, None, None, None))
        self._row_recommendation_ids.append(None)
        self._row_cat_info.append(None)

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(self._numbered_node(dev_d.get('node_id') if dev_d else None, node_name))
        self._table.setItem(r, self._C_NOD, nod)
        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr = _ro(eq_label)
        utr.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr)
        dev_item = _ro(self._numbered_deviation(
            dev_d.get('node_id') if dev_d else None,
            dev_d.get('id') if dev_d else None,
            dev_d['description'] if dev_d else ''))
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
        self._row_recommendation_ids.append(None)
        self._row_cat_info.append(None)

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(self._numbered_node(cause_d.get('node_id'), node_name))
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr = _ro(eq_label)
        utr.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr)

        dev_item = _ro(self._numbered_deviation(
            dev_d.get('node_id') if dev_d else None,
            dev_d.get('id') if dev_d else None,
            dev_d['description'] if dev_d else ''))
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        group_ids = self._group_equipment_ids(cause_d)
        normalised_description = (
            '\n'.join(self.db.group_cause_description_lines(cause_d, group_ids))
            if len(group_ids) >= 2 else cause_d['description'])
        ors = QTableWidgetItem(normalised_description)
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3,
                    None if cause_d.get('_frequency_unset') else freq)
        ors.setData(Qt.ItemDataRole.UserRole + 8, repeats_previous_tag)
        # Group causes keep both tags as live object references and bold them
        # in the sentence (the old ``primary + secondary`` prefix duplicated
        # the first object visually).
        group_tags = []
        for _eid in group_ids:
            _eq = self.db.get_equipment_by_id(_eid)
            if _eq and _eq.get('tag'):
                group_tags.append(_eq.get('tag'))
        if len(group_tags) < 2:
            _legacy = (cause_d.get('comp_tag') or '').strip()
            if re.search(r'\s(?:&|OR|<>|->|\+)\s', _legacy, re.IGNORECASE):
                group_tags = [part.strip() for part in re.split(
                    r'\s+(?:&|OR|<>|->|\+)\s+', _legacy, flags=re.IGNORECASE)
                    if part.strip()]
        ors.setData(Qt.ItemDataRole.UserRole + 9, group_tags)
        ors.setData(Qt.ItemDataRole.UserRole + 10,
                    self._child_number('cause', dev_d.get('id'), cause_d.get('id')))
        self._table.setItem(r, self._C_ORS, ors)

        kon = _ro()
        kon.setToolTip("Enter för att lägga till konsekvens")
        self._table.setItem(r, self._C_KON, kon)

        for col in (self._C_RFORE, self._C_SG,
                    self._C_LOPA, self._C_SLUT):
            self._table.setItem(r, col, _ro())

        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _add_row(self, node_name, dev_d, cause_d, freq, freq_lbl, cons_d, all_sgs, sg,
                 cat_info=None, final_severity=None, excl_cat_names=None, excl_for_cat=None,
                 cause_excl_sgs=None, sev_cat_list=None, all_cat_infos=None,
                 cause_popup_list=None, n_cats=0, repeats_previous_tag=False,
                 cause_status=None, rfs=None, acts=None, recommendation=None,
                 excl_causes_by_sg=None, excl_enablers_for_cat=None):
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
        cause_status  – (has_cons, has_severity, has_safeguard) precomputed by the
                        caller (2026-08-24, see NOTES.md) for the ORS status icon —
                        several rows share the same cause, so _build_rows() computes
                        this once per cause instead of once per row. None re-derives
                        it here via a direct query, for any caller that doesn't pass it.
        rfs           – this row's consequence's reduction_factors, precomputed by
                        the caller (2026-08-24) — several rows can share the same
                        consequence. None re-fetches via a direct query.
        acts          – this row's consequence's actions/recommendations,
                        precomputed by the caller (2026-08-24), same reasoning as
                        rfs. None re-fetches via a direct query.
        excl_causes_by_sg – {sg_id: set(excluded cause_ids)} for every safeguard in
                        this cause, precomputed by the caller (2026-08-24) — avoids
                        re-fetching sg's own exclusion set here when it was already
                        computed in _build_rows(). None re-fetches via a direct query.
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
        if excl_enablers_for_cat is None:
            excl_enablers_for_cat = set()

        r      = self._table.rowCount()
        self._table.insertRow(r)
        cid    = cons_d['id']
        dev_id = dev_d['id'] if dev_d else None

        self._row_meta.append((dev_id, cause_d['id'], cid, sg['id'] if sg else None))
        self._row_recommendation_ids.append(recommendation['id'] if recommendation else None)
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

        if rfs is None:
            rfs = self.db.reduction_factors(cid)
        rfs        = [dict(rf) for rf in rfs]
        category_rfs = ([rf for rf in rfs if rf.get('id') not in excl_enablers_for_cat]
                         if cat_info else rfs)

        final_f, total_rrf, total_steps = total_freq_reduction(
            freq, sg_rrf, False, 10, False, 10, category_rfs)

        final_sev = final_severity if final_severity is not None else sev
        level_b, bg_b, fg_b = risk_info(freq, sev)
        level_s, bg_s, fg_s = risk_info(final_f, final_sev)

        # ── Col 0: Nod ────────────────────────────────────────────────────────
        nod = QTableWidgetItem(self._numbered_node(cause_d.get('node_id'), node_name))
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
        dev_item = QTableWidgetItem(self._numbered_deviation(
            dev_d.get('node_id') if dev_d else None,
            dev_d.get('id') if dev_d else None,
            dev_d['description'] if dev_d else ''))
        dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        # ── Col 2: Orsak ─────────────────────────────────────────────────────
        # Status icon (feature 5): green=complete, orange=partial, red=empty
        if cause_status is not None:
            _has_cons, _has_sev, _has_sg = cause_status
        else:
            _cons_list = self.db.consequences(cause_d['id'])
            _has_cons  = len(_cons_list) > 0
            _has_sev   = any(c.get('severity', 0) and c.get('severity', 0) > 0
                             for c in [dict(x) for x in _cons_list])
            _has_sg    = bool(self.db.safeguards_for_cause(cause_d['id']))
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

        group_ids = self._group_equipment_ids(cause_d)
        normalised_description = (
            '\n'.join(self.db.group_cause_description_lines(cause_d, group_ids))
            if len(group_ids) >= 2 else cause_d['description'])
        ors = QTableWidgetItem(normalised_description)
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3,
                    None if cause_d.get('_frequency_unset') else freq)
        ors.setData(Qt.ItemDataRole.UserRole + 5, cause_d.get('base_frequency'))
        ors.setData(Qt.ItemDataRole.UserRole + 8, repeats_previous_tag)
        ors.setData(Qt.ItemDataRole.UserRole + 10,
                    self._child_number('cause', dev_d.get('id'), cause_d.get('id')))
        group_tags = []
        for _eid in group_ids:
            _eq = self.db.get_equipment_by_id(_eid)
            if _eq and _eq.get('tag'):
                group_tags.append(_eq.get('tag'))
        if len(group_tags) < 2:
            _legacy = (cause_d.get('comp_tag') or '').strip()
            if re.search(r'\s(?:&|OR|<>|->|\+)\s', _legacy, re.IGNORECASE):
                group_tags = [part.strip() for part in re.split(
                    r'\s+(?:&|OR|<>|->|\+)\s+', _legacy, flags=re.IGNORECASE)
                    if part.strip()]
        ors.setData(Qt.ItemDataRole.UserRole + 9, group_tags)
        # _status_icon is no longer stored on the item (2026-08-18, see
        # NOTES.md "skrota pluppen") — the green/yellow/orange/red fill-
        # status dot it drove is gone from paint(); the underlying
        # completeness computation stays, still used for the tooltip below.
        ors.setToolTip(f"{_status_icon} {_status_tip}\n"
                       "Dubbelklicka på texten för att redigera orsaksbeskrivningen\n"
                       "Klicka på taggen (fetstilt) för att redigera objektets tag/typ\n"
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
        kon_item = QTableWidgetItem(cons_d['description'] or '')
        kon_item.setData(Qt.ItemDataRole.UserRole, ('consequence', cid))
        kon_item.setData(Qt.ItemDataRole.UserRole + 3, None)   # no per-row cat badge
        kon_item.setData(Qt.ItemDataRole.UserRole + 7, (cons_d.get('comp_type') or '',
                                                         cons_d.get('comp_tag')  or ''))
        # Every tag ever drag-appended into this text, bolded on paint
        # (2026-08-09, see NOTES.md "fetmarkera objekttexten") — comp_tag
        # above only ever holds the MOST RECENT one.
        kon_item.setData(Qt.ItemDataRole.UserRole + 8,
                         parse_tag_refs(cons_d.get('tagged_refs') or ''))
        kon_item.setData(Qt.ItemDataRole.UserRole + 10,
                         self._child_number('consequence', cause_d.get('id'), cid))
        tip = ("Dra en utrustningsmarkör hit (håll Shift) för att sätta tag\n"
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
        if cat_info:
            cat_short = (cat_name or '')[:3]
            rb_text = f"{cat_short}  {freq_axis_label(freq)}  {cons_axis_label(sev)}"
        else:
            rb_text = ""
            bg_b, fg_b = '#FFFFFF', '#8D9299'
        rb = QTableWidgetItem(rb_text)
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
            sg_item = QTableWidgetItem('')
            sg_item.setFlags(sg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sg_item.setToolTip("Enter för att lägga till barriär")
        else:
            rrf = sg.get('rrf', 1) or 1
            # "—" placeholder when empty (2026-08-12, see NOTES.md) — no
            # separate EditRole set here, see the KON cell's comment above
            # on why that would silently overwrite this back to empty.
            sg_item = QTableWidgetItem(sg['description'] or '')
            sg_item.setData(Qt.ItemDataRole.UserRole,     ('safeguard', sg['id']))
            sg_item.setData(Qt.ItemDataRole.UserRole + 1, rrf)
            # Yellow indicator: list of category names this sg is excluded from
            sg_item.setData(Qt.ItemDataRole.UserRole + 2, excl_cat_names)
            # Category data for extended RRF popup: (cons_id, [(sev_id, cat_name), ...])
            sg_item.setData(Qt.ItemDataRole.UserRole + 3, (cid, sev_cat_list) if sev_cat_list else None)
            # Cause list for RRF popup cause-exclusion section
            sg_item.setData(Qt.ItemDataRole.UserRole + 4, cause_popup_list)
            if excl_causes_by_sg is not None:
                excl_cause_ids = excl_causes_by_sg.get(sg['id'], set())
            else:
                excl_cause_ids = self.db.get_safeguard_excluded_causes(sg['id'])
            excl_cause_names = [desc for cid2, desc, _ in cause_popup_list
                                if cid2 in excl_cause_ids]
            sg_item.setData(Qt.ItemDataRole.UserRole + 5, excl_cause_names)
            sg_item.setData(Qt.ItemDataRole.UserRole + 6,
                            (sg.get('comp_type') or '', sg.get('comp_tag') or ''))
            sg_item.setData(Qt.ItemDataRole.UserRole + 7,
                             parse_tag_refs(sg.get('tagged_refs') or ''))
            sg_item.setData(Qt.ItemDataRole.UserRole + 10,
                            self._child_number('safeguard', cid, sg.get('id')))
            tip = "Dra en utrustningsmarkör hit (håll Shift) för att sätta tag\n" \
                  "Dubbelklicka för att redigera\nEnter för att lägga till ny barriär\nKlicka på RRF-kolumnen för att ändra värdet"
            if excl_cat_names:
                tip += "\n⚠ Gäller ej för kategori: " + ", ".join(excl_cat_names)
            if excl_cause_names:
                tip += "\n⚠ Gäller ej för orsak: " + ", ".join(excl_cause_names)
            sg_item.setToolTip(tip)
        self._table.setItem(r, self._C_SG, sg_item)

        # ── Col Enablers: one compact active-count / aggregate-RRF button ───
        active_rfs = [rf for rf in rfs if rf.get('active')]
        enabler_rrf = 1.0
        for factor in active_rfs:
            try:
                enabler_rrf *= max(1.0, float(factor.get('rrf') or 1))
            except (TypeError, ValueError):
                continue
        lopa_w = _LopaWidget(cid, len(active_rfs), enabler_rrf)
        lopa_w.set_selected(
            self._table.currentRow() == r and
            self._table.currentColumn() == self._C_LOPA)
        lopa_w._extra_btn.pressed.connect(
            lambda row=r: self._select_lopa_row_preserving_scroll(row))
        lopa_w._extra_btn.clicked.connect(partial(self._edit_extra, cid))
        self._table.setCellWidget(r, self._C_LOPA, lopa_w)

        # ── Col SLUT: Risker efter barriärer ───────────────────────────────────
        # Shown for every row now (2026-08-09, see NOTES.md) — same fallback
        # rationale as RFORE above; final_f/sev/bg_s/fg_s are already
        # computed unconditionally regardless of cat_info.
        if cat_info:
            cat_short = (cat_name or '')[:3]
            slut_text = f"{cat_short}  {freq_axis_label(final_f)}  {cons_axis_label(final_sev)}"
        else:
            slut_text = ""
            bg_s, fg_s = '#FFFFFF', '#8D9299'
        rs = QTableWidgetItem(slut_text)
        rs.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        rs.setFlags(rs.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rs.setToolTip(
            "Klicka för att ändra risknivå per kategori\n"
            f"{level_s} — {freq_axis_label(final_f)}  {cons_axis_label(final_sev)}  "
            f"(−{total_steps} steg totalt)")
        rs.setBackground(QBrush(QColor(bg_s)))
        rs.setForeground(QBrush(QColor(fg_s)))
        rs.setFont(QFont("Consolas", 9))
        # Risker efter barriärer uses the same category-aware popup as Risk före,
        # but its displayed matrix position must use the post-barrier
        # frequency. The popup does not save anything on opening; category
        # levels change only when the user explicitly clicks a choice there.
        if cat_info:
            rs.setData(Qt.ItemDataRole.UserRole,
                       ('risk_click_cat', cause_d['id'], cid, cat_id, sev_id,
                        final_f, final_sev))
        else:
            rs.setData(Qt.ItemDataRole.UserRole,
                       ('risk_click', cause_d['id'], cid, final_f, sev))
        self._table.setItem(r, self._C_SLUT, rs)

        # ── Col REK: Rekommendation (2026-08-13, see NOTES.md) ───────────────
        # Backed by the shared recommendations catalog + consequence_
        # recommendations link table (2026-08-25 rework, see NOTES.md
        # "Rekommendationshantering — delad katalog med återanvändning")
        # rather than a new free-text field — a scenario can have several
        # recommendations (responsible/due date/status each), not just
        # one line of text, and the same recommendation can be reused
        # across several consequences without duplicating it.
        if acts is None:
            acts = self.db.recommendations_for_consequence(cid)
        if recommendation is not None and acts:
            # Use the freshly fetched linked row as the source of truth. This
            # avoids rendering the placeholder when the row object passed by
            # the prefetch path is stale after an inline edit.
            rec_id = recommendation['id']
            recommendation = next(
                (a for a in acts if a['id'] == rec_id), recommendation)
        rec_description = ''
        if recommendation is not None:
            rec_description = (recommendation['description'] or '').strip()
        rek_text = (f"{recommendation['display_number']:03d}. "
                    f"{rec_description or 'Ny rekommendation'}"
                    if recommendation else '')
        rek_item = QTableWidgetItem(rek_text or '')
        rek_item.setData(Qt.ItemDataRole.UserRole,
                         ('recommendation', cid,
                          recommendation['id'] if recommendation else None))
        rek_item.setToolTip("Klicka för att redigera direkt eller lägga till/återanvända en rekommendation")
        rek_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        if recommendation is None:
            rek_item.setForeground(QBrush(QColor('#8D9299')))
        self._table.setItem(r, self._C_REK, rek_item)

        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _recommendation_summary(self, acts):
        """REK-cell text for a consequence's linked recommendations
        (2026-08-13, see NOTES.md: "samtliga tillagda rekomendationer
        ... nummereras efter tilläggsordning") — "—" placeholder when
        empty (same convention as KON/SG), otherwise EVERY recommendation
        listed on its own line. Numbered by the recommendation's own compact
        catalog display number (2026-08-25 rework) —
        NOT by position in this list — so the SAME displayed number is
        shown everywhere a reused recommendation appears, letting Anton
        recognize "this is the same one" across different consequences.
        The column joins wrap_cols so multi-line content gets the row
        height it needs, same as ORS/KON."""
        if not acts:
            return ''
        return '\n'.join(
            f"{a['display_number']:03d}. {(a['description'] or '').strip() or 'Ny rekommendation'}"
            for a in acts)

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
        now (e.g. filtered out by the current node/deviation/cause scope).

        Prefers ABOVE the cell (2026-08-26, see NOTES.md "Flytta HAZOP-
        popups ovanför"), falling back to below only if there's no room
        above on screen."""
        row = next((r for r, m in enumerate(self._row_meta) if m[2] == cons_id), -1)
        if row >= 0:
            rect = self._table.visualRect(self._table.model().index(row, self._C_KON))
            top = self._table.viewport().mapToGlobal(rect.topLeft())
            target_height = rect.height()
        else:
            top = QCursor.pos()
            target_height = 0
        scr = (QApplication.screenAt(top) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup_size.width(), popup_size.height()
        x = min(top.x(), scr.right() - pw)
        y = top.y() - ph - 4
        if y < scr.top():
            y = top.y() + target_height + 4
        y = min(y, scr.bottom() - ph)
        return QPoint(max(scr.left(), x), max(scr.top(), y))

    def _open_chain_editor(self, cons_id: int, label_widget=None):
        """Compatibility entry point for the retired five-column dialog.

        A few older callers still ask to "open the consequence editor" by
        this name.  They must now get the ordinary KON inline editor, never a
        separate consequence-chain workflow.
        """
        for row, meta in enumerate(self._row_meta):
            if meta[2] == cons_id:
                self._try_start_edit(row, self._C_KON)
                return

    def _refresh_recommendation_cell(self, cons_id):
        """Fast in-place patch of every row's REK cell for cons_id,
        mirroring _update_row_text_only()'s pattern (same re-entrancy
        guard, same table.item()-is-None check to skip span-covered
        rows that have no real item of their own)."""
        if getattr(self, '_rebuilding', False):
            return
        # REK is row-based now; defer the rebuild until the current editor's
        # commit/close signal has fully unwound. Rebuilding synchronously from
        # inside cellChanged would destroy the live QLineEdit before the
        # eventFilter emits closeEditor, which is the crash seen after several
        # consecutive Enter presses.
        self._schedule_rebuild()

    def _restore_recommendation_selection(self):
        """Keep the just-saved recommendation cell selected after Enter.

        A recommendation save has to rebuild the physical row layout, unlike
        an ordinary in-place text save. Restore by recommendation id (not an
        old row number) so adding a sibling or wrapping its text cannot move
        selection to another cell.
        """
        pending = self._recommendation_selection_after_commit
        self._recommendation_selection_after_commit = None
        if not pending:
            return
        cons_id = pending.get('cons_id')
        rec_id = pending.get('rec_id')
        candidates = [
            row for row, meta in enumerate(self._row_meta)
            if meta[2] == cons_id
        ]
        if not candidates:
            return
        row = next(
            (r for r in candidates
             if r < len(self._row_recommendation_ids) and
             self._row_recommendation_ids[r] == rec_id),
            candidates[0])
        item = self._table.item(row, self._C_REK)
        if item is None:
            return
        self._table.setCurrentCell(row, self._C_REK)
        self._table.scrollToItem(item)
        self._table.setFocus()

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
        # The Enablers button is a cell widget.  Anchor the popup directly
        # below it, like the frequency/RRF popups, rather than letting a
        # modal dialog default to the centre of the main window.
        source = self.sender()
        if isinstance(dlg, QDialog):
            if isinstance(source, QWidget):
                anchor = source.mapToGlobal(QPoint(0, source.height() + 4))
            else:
                anchor = QCursor.pos()
            dlg.position_below(anchor)
        dlg.exec()
        self._schedule_rebuild()

    def _select_lopa_row_preserving_scroll(self, row):
        """Select an Enablers cell without letting Qt move the protocol.

        The cell contains a real QPushButton, so changing the current cell
        can trigger QTableWidget's automatic ensure-visible behaviour.  The
        invoking button is already visible; preserve the exact table view
        synchronously and once more on the next event-loop turn because the
        clicked signal immediately opens a nested popup loop.
        """
        table = self._table
        vbar = table.verticalScrollBar()
        hbar = table.horizontalScrollBar()
        v_value = vbar.value()
        h_value = hbar.value()
        table.setCurrentCell(row, self._C_LOPA)
        vbar.setValue(v_value)
        hbar.setValue(h_value)

        def _restore():
            try:
                table.verticalScrollBar().setValue(v_value)
                table.horizontalScrollBar().setValue(h_value)
            except RuntimeError:
                # The panel may have been closed while the popup's nested
                # event loop was active; in that case there is no view left
                # to restore and the queued callback is harmless.
                return

        QTimer.singleShot(0, _restore)

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
        """Targeted update of the SLUT cell when an enabler changes.

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
        all_sgs = [dict(s) for s in self.db.safeguards(cons_id)]
        final_severities = {
            r['category_id']: r['severity']
            for r in self.db.get_final_consequence_severities(cons_id)}

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
                    excluded_enablers = \
                        self.db.get_severity_excluded_reduction_factors(sev_id)
                    effective_rfs = [rf for rf in rfs
                                     if rf.get('id') not in excluded_enablers]
                else:
                    sev = cons_d.get('severity') or 1
                    sg_rrf = 1
                    for s in all_sgs:
                        if s['id'] not in cause_excl:
                            sg_rrf *= (s.get('rrf') or 1)
                    effective_rfs = rfs

                final_f, total_rrf, total_steps = total_freq_reduction(
                    freq, sg_rrf, False, 10, False, 10, effective_rfs)
                final_sev = (final_severities.get(cat_id, sev)
                             if cat_info else sev)
                level_s, bg_s, fg_s = risk_info(final_f, final_sev)

                # Patched for every row now (2026-08-09, see NOTES.md) — same
                # fallback rationale as _add_row: bg_s/fg_s are already
                # computed unconditionally above, regardless of cat_info, so
                # a non-categorized consequence's SLUT cell used to go
                # stale/blank forever after an RRF change.
                if cat_info:
                    cat_short = (cat_name or '')[:3]
                    slut_text = f"{cat_short}  {freq_axis_label(final_f)}  {cons_axis_label(final_sev)}"
                else:
                    slut_text = ""
                    bg_s, fg_s = '#FFFFFF', '#8D9299'
                rs = self._table.item(row, self._C_SLUT)
                if rs:
                    rs.setText(slut_text)
                    rs.setToolTip(
                        "Klicka för att ändra risknivå per kategori\n"
                        f"{level_s} — {freq_axis_label(final_f)}  "
                        f"{cons_axis_label(final_sev)}  (−{total_steps} steg totalt)")
                    rs.setBackground(QBrush(QColor(bg_s)))
                    rs.setForeground(QBrush(QColor(fg_s)))
                    if cat_info:
                        rs.setData(Qt.ItemDataRole.UserRole,
                                   ('risk_click_cat', cause_id, cons_id,
                                    cat_id, sev_id, final_f, final_sev))
                    else:
                        rs.setData(Qt.ItemDataRole.UserRole,
                                   ('risk_click', cause_id, cons_id,
                                    final_f, sev))
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
                    sequence = item.data(Qt.ItemDataRole.UserRole + 10)
                    item.setText(new_desc)
                    # setText normally preserves custom roles, but explicitly
                    # restore the safeguard sequence number so the separate
                    # painted prefix can never disappear after an edit.
                    if kind == 'safeguard':
                        if not sequence:
                            sequence = self._child_number(
                                'safeguard', meta[2], meta[3])
                        if sequence:
                            item.setData(Qt.ItemDataRole.UserRole + 10, sequence)
                if needs_height_recalc:
                    # Recompute the WHOLE row's height (_compute_row_height),
                    # not just what this one column now needs — otherwise
                    # clearing e.g. a consequence's text back to empty would
                    # shrink the row below what a long cause description (or
                    # the enabler summary button's fixed-height widget) in the SAME
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
        group_rows = (len(item.data(Qt.ItemDataRole.UserRole + 9) or [])
                      if col == self._C_ORS and item else 1)
        group_rows = max(1, group_rows)
        if not text and group_rows == 1:
            return one_line_h
        w = table.columnWidth(col)
        if col == self._C_ORS:
            cell_w = max(40, w - 6 - _RRF_W)
            combined = self._ors_combined_text(item, text)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, combined)
            return max(one_line_h * group_rows, rect.height() + 4)
        else:   # self._C_KON
            cell_w = max(40, w)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, text)
            return max(one_line_h, rect.height() + 4)

    def _resize_recommendation_editor(self, editor, row):
        """Let a live REK editor grow the row as its text wraps.

        The saved table item is intentionally not updated until commit, so
        the normal row-height calculation cannot see text being typed. Use
        the editor's current width and plain text here, then keep the
        existing shared-row requirements as the lower bound. Changing only
        the row height preserves the active editor and its caret.
        """
        if (getattr(self, '_rebuilding', False) or
                not isinstance(editor, _BoldTagTextEdit) or
                row < 0 or row >= self._table.rowCount()):
            return
        width = editor.width()
        if width <= 0:
            return
        fm = QFontMetrics(editor.font())
        text = editor.toPlainText() or ''
        wrapped = fm.boundingRect(
            0, 0, max(40, width), 10000,
            Qt.TextFlag.TextWordWrap, text)
        required = max(fm.height() + 6, wrapped.height() + 4)
        try:
            base = self._compute_row_height(row)
        except Exception:
            base = self._table.rowHeight(row)
        target = max(base, required)
        if target != self._table.rowHeight(row):
            self._table.setRowHeight(row, target)

    def refresh_placed(self):
        """Repaint the table — kept as a thin call so its many existing
        call sites (after any data change that might affect what's shown)
        keep working unchanged; it no longer tracks P&ID placement state
        (2026-08-13, see NOTES.md: the P&ID canvas is now
        object-placement-only, so cause/consequence/safeguard rows have no
        "placed on P&ID" concept anymore)."""
        self._table.viewport().update()

    def refresh_visual_settings(self):
        """Reload table-only visual preferences and repaint immediately.

        Scenario and Worksheet share the project database and this table
        implementation.  Repainting here keeps a live settings toggle from
        rebuilding rows or touching any risk calculation.
        """
        self._risk_bars_enabled = (
            self.db.get_config('scenario_risk_bars_enabled', '1') == '1')
        self._table.viewport().update()

    def set_risk_bars_enabled(self, enabled):
        """Set the risk-cell presentation without rebuilding table data."""
        self._risk_bars_enabled = bool(enabled)
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
        if not isinstance(editor, (_BoldTagTextEdit, QLineEdit)):
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

    def _cancel_inline_editor(self, editor):
        """Dismiss one inline editor and every helper it opened, without save."""
        if editor is None:
            return
        # Cancel delayed completions/popups before closing the delegate. A
        # zero-delay helper must never reappear after the user pressed Escape.
        for serial_name in ('_completion_serial', '_tag_completion_serial'):
            if hasattr(editor, serial_name):
                setattr(editor, serial_name, getattr(editor, serial_name) + 1)
        for completer_name in ('_completer', '_tag_completer'):
            completer = getattr(editor, completer_name, None)
            if completer is not None:
                try:
                    completer.popup().hide()
                except RuntimeError:
                    pass

        top_level = self.window()
        for popup_type in (StandardCauseSuggestPopup, RecommendationAssistPopup,
                           CauseTagPopup):
            for popup in top_level.findChildren(popup_type):
                if (getattr(popup, '_editor', None) is editor or
                        getattr(popup, '_inline_editor', None) is editor):
                    popup.close()

        col = editor.property('editing_col')
        delegate = (self._pid_delegate if col in
                    (self._C_ORS, self._C_KON, self._C_SG) else self._delegate)
        delegate.closeEditor.emit(
            editor, QStyledItemDelegate.EndEditHint.RevertModelCache)
        self._double_click_edit = None
        self._table.setFocus()

    def _inline_editor_widgets(self):
        """Return live inline editors owned by this scenario table.

        ``QTableWidget.focusWidget()`` is not sufficient here: after a click
        outside the table focus may already belong to the clicked widget,
        while the delegate editor is still visible and still owns the
        standard-cause popup.  Looking at the editor's explicit identity
        properties is stable across those focus transitions.
        """
        editors = []
        for editor in self._table.findChildren((_BoldTagTextEdit, QLineEdit)):
            try:
                if (editor.isVisible() and
                        editor.property('editing_row') is not None and
                        editor.property('editing_col') is not None):
                    editors.append(editor)
            except RuntimeError:
                # The delegate may be in the middle of deferred widget
                # teardown.  A disappearing editor is already on its way out.
                continue
        return editors

    @staticmethod
    def _is_descendant_of(widget, ancestor):
        """Whether *widget* is *ancestor* or one of its child widgets."""
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            try:
                current = current.parentWidget()
            except (AttributeError, RuntimeError):
                return False
        return False

    def _is_inline_helper_target(self, target):
        """Keep clicks inside an editor/helper popup from ending the edit."""
        if target is None:
            return False
        for editor in self._inline_editor_widgets():
            if self._is_descendant_of(target, editor):
                return True
        top_level = self.window()
        for popup_type in (StandardCauseSuggestPopup,
                           RecommendationAssistPopup, CauseTagPopup):
            for popup in top_level.findChildren(popup_type):
                if self._is_descendant_of(target, popup):
                    return True
        return False

    def _finish_inline_editor_for_external_click(self, target):
        """Commit/close an editor when a click lands outside its edit UI.

        This is the missing counterpart to the popup's editor-Hide filter.
        It is intentionally a commit (the same path as clicking another
        normal Qt table cell), not a cancel, so text typed immediately before
        clicking away is retained.  The helper popup closes synchronously via
        the editor's Hide event and its explicit close backstop.
        """
        if self._is_inline_helper_target(target):
            return
        editors = self._inline_editor_widgets()
        top_level = self.window()
        popup_types = (StandardCauseSuggestPopup,
                       RecommendationAssistPopup, CauseTagPopup)
        # Clean up helpers whose editor has already been hidden/deleted.  This
        # is deliberately done even when there is no live editor: otherwise a
        # missed Hide/destroyed notification leaves a popup that appears
        # impossible to dismiss with an ordinary click.
        live_ids = {id(editor) for editor in editors}
        for popup_type in popup_types:
            for popup in top_level.findChildren(popup_type):
                linked = (getattr(popup, '_editor', None) or
                          getattr(popup, '_inline_editor', None))
                if linked is None or id(linked) not in live_ids:
                    popup.close()
        if not editors:
            return
        # A pending empty-deviation click belongs to the old click sequence;
        # do not materialise a new cause after the user has already clicked
        # away from that cell.
        if hasattr(self, '_empty_cause_click_timer'):
            self._empty_cause_click_timer.stop()
            self._empty_cause_click_target = None
        for editor in editors:
            col = editor.property('editing_col')
            delegate = (self._pid_delegate if col in
                        (self._C_ORS, self._C_KON, self._C_SG)
                        else self._delegate)
            try:
                delegate.commitData.emit(editor)
                delegate.closeEditor.emit(
                    editor, QStyledItemDelegate.EndEditHint.NoHint)
            except RuntimeError:
                # A rebuild may have deleted this editor while another event
                # in the same click sequence was being delivered.
                continue
        self._double_click_edit = None

    def ors_cell_global_pos(self, dev_id):
        """Return global top-right corner of the first placeholder ORS cell for dev_id."""
        for row, meta in enumerate(self._row_meta):
            if meta[0] == dev_id:
                rect = self._table.visualRect(
                    self._table.model().index(row, self._C_ORS))
                return self._table.viewport().mapToGlobal(rect.topRight())
        return None

    def _on_cell_clicked(self, row, col):
        if col != self._C_REK:
            self._last_recommendation_click = None
        if col == self._C_ORS and row < len(self._row_meta):
            dev_id, cause_id = self._row_meta[row][0], self._row_meta[row][1]
            if cause_id is not None:
                self.item_selected.emit(CAUSE_T, cause_id)
            elif dev_id is not None:
                # Wait briefly only so a double-click can cancel this
                # single-click action. Both routes enter the normal inline
                # editor; the former "Orsak på P&ID" dialog is retired.
                return
            return
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self.item_selected.emit(CONS_T, cons_id)
            return
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][3]
            if sg_id is not None:
                self.item_selected.emit(SG_T, sg_id)
            return
        if col == self._C_REK and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self.item_selected.emit(CONS_T, cons_id)
                click_target = (row, cons_id)
                if self._last_recommendation_click == click_target:
                    self._last_recommendation_click = None
                    QTimer.singleShot(
                        0, lambda r=row: self._try_start_edit(r, self._C_REK))
                else:
                    self._last_recommendation_click = click_target
            return
        if col not in (self._C_RFORE, self._C_SLUT):
            return
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta or meta[0] not in ('risk_click', 'risk_click_cat'):
            return

        if meta[0] == 'risk_click_cat':
            _, cause_id, cons_id, cat_id, sev_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(
                cur_freq, cur_cons, self, db=self.db, cons_id=cons_id,
                final_consequence=(col == self._C_SLUT))
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id, catid=cat_id:
                    self._apply_risk_from_matrix_cat(caid, coid, catid, f, c))
        else:
            _, cause_id, cons_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(
                cur_freq, cur_cons, self, db=self.db, cons_id=cons_id,
                final_consequence=(col == self._C_SLUT))
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id:
                    self._apply_risk_from_matrix(caid, coid, f, c))
        # Per-category severities (2026-08-26, see NOTES.md "Flytta
        # konsekvenskategori till riskmatrisen") save themselves
        # immediately inside the popup -- just needs a table refresh.
        popup.category_changed.connect(self._schedule_rebuild)

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
        popup.show()

    def _apply_risk_from_matrix(self, cause_id, cons_id, new_freq, new_cons):
        with self.db.history_group():
            self.db.update_cause(cause_id, likelihood=new_freq)
            cons = self.db.get_consequence(cons_id)
            if cons:
                self.db.update_consequence(
                    cons_id, cons['description'], new_cons, cons['category'] or '')
        self._schedule_rebuild()

    def _apply_risk_from_matrix_cat(self, cause_id, cons_id, cat_id, new_freq, new_cons):
        """Bidirectional: update frequency on cause and category severity on consequence."""
        with self.db.history_group():
            self.db.update_cause(cause_id, likelihood=new_freq)
            self.db.set_consequence_severity(cons_id, cat_id, new_cons)
        self._schedule_rebuild()

    def _show_cat_sg_popup(self, sev_id, all_sgs):
        """Open the safeguard-selection popup for a category row.
        Prefers ABOVE the cursor (2026-08-26, see NOTES.md "Flytta
        HAZOP-popups ovanför"), falling back to below only if there's no
        room above on screen."""
        popup = CatSGSelectionPopup(self.db, sev_id, all_sgs, self)
        popup.adjustSize()
        gp  = QCursor.pos()
        scr = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(gp.x(), scr.right() - pw)
        y = gp.y() - ph - 4
        if y < scr.top():
            y = gp.y() + 4
        popup.move(max(scr.left(), x), max(scr.top(), min(y, scr.bottom() - ph)))
        if popup.exec() == QDialog.DialogCode.Accepted:
            self._schedule_rebuild()

    def _ensure_consequence_double_click_editor(self, row, cons_id):
        """Open KON's normal editor when Qt failed to emit itemDoubleClicked."""
        if self._table.state() == QAbstractItemView.State.EditingState:
            return
        if (not (0 <= row < len(self._row_meta)) or
                self._row_meta[row][2] != cons_id):
            return
        item = self._table.item(row, self._C_KON)
        if item is not None:
            self._on_cell_double_clicked(item)

    def _on_cell_double_clicked(self, item):
        if item is None:
            return
        row = item.row()
        col = item.column()
        # An empty cause has no backing row yet, so create that row first and
        # then enter its inline editor. Empty consequences already have a
        # backing row and fall through to exactly the same inline-edit path as
        # populated consequences and safeguards.
        if 0 <= row < len(self._row_meta):
            dev_id, cause_id, cons_id, _sg_id = self._row_meta[row]
            if col == self._C_ORS and cause_id is None and dev_id is not None:
                # Cancel the delayed single-click editor. Both routes create
                # a blank cause and continue in the ordinary ORS editor.
                self._empty_cause_click_timer.stop()
                self._empty_cause_click_target = None
                self._quick_add_cause(dev_id)
                self._double_click_edit = None
                return
            if col == self._C_KON and cons_id is None and cause_id is not None:
                # A cause without any consequence is rendered as an empty,
                # non-editable KON placeholder. Treat its double-click like
                # the equivalent empty Safeguard cell: materialise the
                # missing record, then let the normal new-item route rebuild
                # and open its inline editor.
                self._quick_add_consequence(cause_id)
                self._double_click_edit = None
                return
        group_line = None
        # Double-click starts inline edit — consistent across ORS/KON/SG.
        # The former five-column consequence-chain dialog has no active UI
        # path; a consequence is edited exactly like the adjacent text cells.
        if col in (self._C_ORS, self._C_KON, self._C_SG, self._C_REK):
            if col == self._C_ORS and row < len(self._row_meta):
                cause_id = self._row_meta[row][1]
                obj_data = item.data(Qt.ItemDataRole.UserRole + 2) or ('', '')
                group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or []
                if cause_id is not None and len(group_tags) >= 2:
                    # The two bold tag portions open the object/group popup;
                    # the mechanism/effect text goes through the same inline
                    # editor as an ordinary single-object cause.
                    click = self._double_click_edit
                    # On some Qt/platform combinations the viewport event
                    # filter is not the receiver of the double-click, even
                    # though itemDoubleClicked is emitted.  Recover the
                    # actual cursor position here so grouped rows still
                    # select the correct visual line and can enter inline
                    # editing to the right of the tag.
                    if not click or click[0] != row or click[1] != col:
                        vp_pos = self._table.viewport().mapFromGlobal(QCursor.pos())
                        if (self._table.rowAt(vp_pos.y()) == row and
                                self._table.columnAt(vp_pos.x()) == col):
                            click = (row, col, vp_pos)
                    tag_hit = False
                    if click and click[0] == row and click[1] == col:
                        cell_rect = self._table.visualRect(
                            self._table.model().index(row, col))
                        line_h = max(_ORS_FIRST_LINE_H,
                                     QFontMetrics(self._table.font()).height() + 4)
                        rel_y = click[2].y() - cell_rect.top() - 2
                        line_no = int(rel_y // line_h) if rel_y >= 0 else -1
                        if 0 <= line_no < len(group_tags):
                            # Only the painted text band is editable.  The
                            # small vertical margin above the first object
                            # (and between/below rows) must not become an
                            # accidental full-cell edit target.
                            font_h = QFontMetrics(self._table.font()).height()
                            line_top = line_no * line_h
                            text_pad = max(0, (line_h - font_h) // 2)
                            if not (line_top + text_pad <= rel_y <
                                    line_top + text_pad + font_h):
                                self._double_click_edit = None
                                return
                            group_line = line_no
                            bold_font = QFont(self._table.font())
                            bold_font.setBold(True)
                            tag_start = cell_rect.left() + 2
                            if line_no == 0:
                                num = item.data(Qt.ItemDataRole.UserRole + 10) or ''
                                if num:
                                    tag_start += QFontMetrics(self._table.font()).horizontalAdvance(
                                        f"{num}.  ")
                            tag_width = QFontMetrics(bold_font).horizontalAdvance(
                                str(group_tags[line_no]))
                            tag_hit = tag_start <= click[2].x() <= tag_start + tag_width
                        elif rel_y >= len(group_tags) * line_h:
                            # The area below the last group row is only cell
                            # whitespace.  It must not fall through to
                            # QTableWidget's ordinary full-cell editor.
                            self._double_click_edit = None
                            return
                    # The bold object tag is presentation-only.  A
                    # double-click there follows the same inline editor path
                    # as a double-click in the row's free text; no separate
                    # Primär/Sekundär popup is shown.
                # A grouped cause always reports an empty single-tag
                # obj_data (its identity lives in the two-entry group_tags
                # list instead, checked above) -- without the len() guard
                # here, every double-click on a grouped row's free-text
                # zone (tag_hit already False) was wrongly read as "no
                # object bound yet" and re-routed into the tag/object
                # picker popup, so the inline free-text editor below could
                # never be reached for a grouped cause at all.
                # A real cause with no object tag is still an ordinary blank
                # text field.  Keep double-click available for direct inline
                # editing; the tag popup is opened only from the rendered tag
                # zone (when a tag exists).
                if cause_id is not None and len(group_tags) >= 2 and group_line is None:
                    # No visual group row was hit.  Do not let this event
                    # fall through to the generic full-cell editor.
                    self._double_click_edit = None
                    return
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
            click = self._double_click_edit
            self._double_click_edit = None
            # A second double-click can arrive before Qt has completed the
            # previous delegate close sequence (especially when a grouped
            # cause also has its standard-cause popup open).  QTableWidget
            # then still considers the index to be in EditingState and
            # silently refuses the new edit, making the group editor appear
            # stuck.  Close the old editor first and defer the new edit until
            # Qt has processed that close signal.
            if self._inline_editor_widgets():
                self._finish_inline_editor_for_external_click(self._table)
                QTimer.singleShot(
                    0, lambda r=row, c=col, gl=group_line, p=click:
                    self._open_inline_editor_after_close(r, c, gl, p))
                return
            self._open_inline_editor_after_close(row, col, group_line, click)

    def _open_inline_editor_after_close(self, row, col, group_line, click):
        """Open one cell after any previous QTableWidget editor is closed."""
        if col == self._C_ORS and group_line is not None and group_line >= 0:
            self._group_edit_line = (row, group_line)
        else:
            self._group_edit_line = None
        opened = self._table.edit(self._table.model().index(row, col))
        if not opened and not self._inline_editor_widgets():
            # Do not let a rejected edit request leak its row marker into the
            # next ordinary cause edit.
            self._group_edit_line = None
        if click and click[0] == row and click[1] == col:
            QTimer.singleShot(0, lambda r=row, c=col, p=click[2]:
                              self._place_editor_caret(r, c, p))

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

        # Use the extended popup only for consequence-category applicability.
        # Per-cause selection was deliberately retired, so it must not make
        # this alternative popup appear on its own.
        item          = self._table.item(row, self._C_SG)
        cat_pop_data  = item.data(Qt.ItemDataRole.UserRole + 3) if item else None

        if cat_pop_data:
            _cons_id, sev_cat_list = cat_pop_data if cat_pop_data else (None, [])
            popup = SgRRFCategoryPopup(
                self.db, sg_id, current_rrf, current_sg_type,
                sev_cat_list, parent=self)
        else:
            popup = RRFPopup(current_rrf, current_sg_type, self)
            popup.rrf_selected.connect(
                lambda v, t, r=row, sid=sg_id: self._update_sg_rrf(r, sid, v, t))

        popup.adjustSize()
        # Prefer ABOVE global_pos (2026-08-26, see NOTES.md "Flytta
        # HAZOP-popups ovanför"), falling back to below only if there's
        # no room above on screen.
        _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = _scr.availableGeometry()
        pw = popup.sizeHint().width()
        ph = popup.sizeHint().height()
        x = global_pos.x()
        y = global_pos.y() - ph - 6
        if y < screen.top():
            y = global_pos.y() + 6
        if x + pw > screen.right():
            x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top() + 4, min(y, screen.bottom() - ph))
        popup.move(x, y)
        if popup.exec() == QDialog.DialogCode.Accepted:
            self._schedule_rebuild()

    # ORS cell layout constants — shared between paint() (_PidDelegate,
    # below) and the click hit-test in eventFilter() so the drawn zones
    # and the clickable zones can never drift apart. This file has a
    # documented history of exactly that kind of desync between paint
    # code and geometry code computed elsewhere (see NOTES.md's notes on
    # _wrap_col_row_height/_resize_rows_manual needing to stay in sync
    # with paint) — keeping each calculation in one place avoids
    # repeating it.
    _ORS_FREQ_MAX_W  = 90   # sane ceiling; real frequency strings are short ("3/år", "1.2e-3/år")
    _ORS_FREQ_MARGIN = 6    # right-edge margin for the frequency row within the orsaksfält
    _ORS_DOT_RESERVE_W = 12  # room for the comment dot at the cell's right edge (2026-08-25:
    # the dot moved onto this same first line when the tag strip was removed, so the
    # frequency zone must always leave this space clear — reserved unconditionally
    # (not just when a comment exists) so the two geometries never need to agree on
    # whether a given row currently has one, avoiding yet another desync source

    def _ors_tag_prefix(self, item):
        """(tag_label, show_tag) for the ORS cell's bold tag prefix
        (2026-08-25, see NOTES.md "Slå ihop objektbaren i Orsak-kolumnen"
        — replaces the old separate tag strip with an inline bold prefix
        ahead of the description, "V-101, Felar öppen"). Single source
        of truth shared by sizeHint/paint/the click zone/the editor
        offset, same rule every other ORS zone in this class already
        follows.

        repeats_previous (UserRole+8, set in _build_rows) is unchanged
        from the old strip-based design (2026-08-18, see NOTES.md: "om
        det visas flera avikelser efter varandra som tillhör samma
        objekttagg behöver denna inte repeteras") — a consecutive run of
        causes sharing one object still shows the bold tag only once."""
        obj_data = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        comp_type, comp_tag = obj_data if obj_data else ('', '')
        repeats_previous = bool(item.data(Qt.ItemDataRole.UserRole + 8)) if item else False
        tag_label = (comp_tag or '').strip()
        if not tag_label and self._hide_unplaced_tag:
            return '', False
        if not tag_label:
            # Empty/unbound causes stay visually empty; the worksheet no
            # longer needs a placeholder label in the cell.
            return '', False
        return tag_label, not repeats_previous

    def _ors_combined_text(self, item, desc):
        """Return the exact string measured and painted for an ORS cell.

        A grouped cause has a strict one-object/one-line contract.  The
        database helper repairs legacy compact descriptions before the table
        paints them, so paint, hit-testing and the inline editor all see the
        same number and order of rows.
        """
        meta = item.data(Qt.ItemDataRole.UserRole) if item else None
        try:
            cause_id = meta[1] if meta else None
            cause = getattr(self, '_display_cache', {}).get('causes', {}).get(cause_id)
            if cause is None and cause_id is not None:
                cause = self.db.get_cause(cause_id)
            group_ids = self.db.group_equipment_ids_for_cause(cause)
        except Exception:
            # A queued Qt repaint can arrive just after a temporary project
            # window has closed its database.  Painting must remain a safe
            # no-op in that teardown window rather than turning a harmless
            # stale cell into an unhandled GUI crash.
            cause, group_ids = None, []
        if len(group_ids) >= 2:
            try:
                lines = self.db.group_cause_description_lines(cause, group_ids)
            except Exception:
                # The database can close between the two reads above and the
                # line expansion. Keep the already-painted cell usable during
                # teardown; a later rebuild will restore the grouped lines.
                lines = str(desc or '').splitlines() or ['']
            number = item.data(Qt.ItemDataRole.UserRole + 10) if item else None
            stored = '\n'.join(lines)
            return f"{number}.  {stored}" if number else stored

        tag_label, show_tag = self._ors_tag_prefix(item)
        if not show_tag:
            num = item.data(Qt.ItemDataRole.UserRole + 10) if item else None
            return f"{num}.  {desc}" if num else desc
        trivial = desc.strip() in ('', 'Ny orsak')
        num = item.data(Qt.ItemDataRole.UserRole + 10) if item else None
        text = tag_label if trivial else f"{tag_label}, {desc}"
        return f"{num}.  {text}" if num else text

    def _group_operator(self, item):
        """Return the displayed operator between a group's two objects."""
        tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
        if len(tags) < 2:
            return 'OR'
        meta = item.data(Qt.ItemDataRole.UserRole) if item else None
        cause = self.db.get_cause(meta[1]) if meta else None
        raw = (cause.get('comp_tag') or '') if cause else ''
        match = re.search(r'\s(&|OR|<>|->|\+)\s', raw, re.IGNORECASE)
        if not match:
            return 'OR'
        if match.group(1) == '+':
            return '&'
        return 'OR' if match.group(1).casefold() in ('<>', 'or') else match.group(1)

    @staticmethod
    def _normalise_group_operator(value):
        value = str(value or '').strip()
        if value == '+':
            return '&'
        if value.casefold() in ('or', '<>'):
            return 'OR'
        return value if value in ('&', 'OR', '->') else 'OR'

    def _group_operators(self, item):
        """Return one operator for each group row after the first.

        Older data stores one operator for the complete group.  Repeat that
        value for all separators so old groups render exactly as before.
        Newer data can store a different separator between every pair of
        tags in ``comp_tag``.
        """
        tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
        count = len(tags)
        if count < 2:
            return []
        meta = item.data(Qt.ItemDataRole.UserRole) if item else None
        cause = self.db.get_cause(meta[1]) if meta else None
        raw = (cause.get('comp_tag') or '') if cause else ''
        found = [self._normalise_group_operator(value)
                 for value in re.findall(r'\s(&|OR|<>|->|\+)\s', raw,
                                        re.IGNORECASE)]
        if not found:
            found = [self._group_operator(item)]
        if len(found) == 1:
            found *= count - 1
        return ([''] + found + ['OR'] * count)[:count]

    def _group_comp_tag(self, tags, operators):
        """Build the persisted tag expression from ordered rows."""
        if not tags:
            return ''
        return tags[0] + ''.join(
            f" {self._normalise_group_operator(operators[i - 1])} {tags[i]}"
            for i in range(1, len(tags)))

    def _group_link_operators(self, cause, count):
        """Return the persisted separator before every group row after 0."""
        if count < 2:
            return []
        found = [self._normalise_group_operator(value)
                 for value in re.findall(r'\s(&|OR|<>|->|\+)\s',
                                        (cause or {}).get('comp_tag') or '',
                                        re.IGNORECASE)]
        if not found:
            found = ['OR']
        if len(found) == 1:
            found *= count - 1
        return (found + ['OR'] * (count - 1))[:count - 1]

    def _group_equipment_ids(self, cause):
        """Return group ids through the shared database normalizer."""
        return self.db.group_equipment_ids_for_cause(cause)

    def _group_cause_changed(self, cause_id):
        """Refresh every view after a grouped-cause mutation.

        Group actions originally refreshed only this table.  That left the
        tree/P&ID scope and, in the worksheet host, the matching cause cell
        temporarily stale after moving a row, changing one incoming operator
        or selecting a legacy quick choice.  Keep the one rebuild (needed for
        row order and geometry) and publish the same cause-edited signal as
        the normal inline editor.
        """
        # A complete rebuild is still needed for spans and final row geometry,
        # but it is intentionally deferred.  Keep the currently visible cell
        # in sync first: otherwise a just-moved secondary row is painted from
        # the new database order while its hit-testing/editor geometry still
        # uses the old ``group_tags`` role until the next event-loop turn.
        self._refresh_group_cause_cell_now(cause_id)
        self._schedule_rebuild()
        self.item_edited.emit(CAUSE_T, cause_id)

    def _refresh_group_cause_cell_now(self, cause_id):
        """Patch visible grouped-cause cells before the deferred rebuild.

        Group row moves, object replacement and operator changes all persist
        immediately.  The table rebuild follows on the next event-loop turn,
        so the item text and the per-line tag metadata must be refreshed here
        as well.  This keeps the secondary line clickable/editable in the
        short interval before that rebuild runs.
        """
        if getattr(self, '_rebuilding', False):
            return
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        group_ids = self._group_equipment_ids(cause)
        if len(group_ids) < 2:
            return
        group_tags = []
        for equipment_id in group_ids:
            equipment = self.db.get_equipment_by_id(equipment_id)
            if equipment and equipment.get('tag'):
                group_tags.append(equipment['tag'])
        if len(group_tags) < 2:
            legacy = (cause.get('comp_tag') or '').strip()
            group_tags = [part.strip() for part in re.split(
                r'\s+(?:&|OR|<>|->|\+)\s+', legacy, flags=re.IGNORECASE)
                if part.strip()]
        description = '\n'.join(
            self.db.group_cause_description_lines(cause, group_ids))

        self._table.blockSignals(True)
        try:
            for row, meta in enumerate(self._row_meta):
                if len(meta) < 2 or meta[1] != cause_id:
                    continue
                item = self._table.item(row, self._C_ORS)
                if item is None:
                    continue
                item.setText(description)
                item.setData(Qt.ItemDataRole.UserRole + 2,
                             self._cause_tag_display(cause))
                item.setData(Qt.ItemDataRole.UserRole + 9, group_tags)
        finally:
            self._table.blockSignals(False)
        self._table.viewport().update()

    def _set_group_operator(self, cause_id, operator):
        cause = self.db.get_cause(cause_id)
        if not cause or len(self._group_equipment_ids(cause)) < 2:
            return
        equipment_ids = self._group_equipment_ids(cause)
        if operator not in ('&', 'OR', '->'):
            return
        tags = []
        for equipment_id in equipment_ids:
            equipment = self.db.get_equipment_by_id(equipment_id)
            if not equipment:
                return
            tags.append(equipment.get('tag', ''))
        self.db.update_cause(cause_id,
                             comp_tag=f" {operator} ".join(tags),
                             group_equipment_ids=equipment_ids)
        self._group_cause_changed(cause_id)

    def _set_group_row_operator(self, cause_id, row_index, operator):
        """Change only the separator immediately before one group row."""
        cause = self.db.get_cause(cause_id)
        ids = self._group_equipment_ids(cause)
        if not cause or row_index <= 0 or row_index >= len(ids):
            return
        operator = self._normalise_group_operator(operator)
        tags = []
        for equipment_id in ids:
            equipment = self.db.get_equipment_by_id(equipment_id)
            tags.append((equipment.get('tag') if equipment else '') or 'Objekt')
        # Parse the existing expression directly; a single legacy operator is
        # expanded to every separator by the same rule used for painting.
        old_ops = self._group_link_operators(cause, len(ids))
        old_ops[row_index - 1] = operator
        self.db.update_cause(cause_id,
                             comp_tag=self._group_comp_tag(tags, old_ops),
                             group_equipment_ids=ids)
        self._group_cause_changed(cause_id)

    def _move_group_row(self, cause_id, row_index, delta):
        """Move a group row and its description one position up/down."""
        cause = self.db.get_cause(cause_id)
        # Normalise the persisted text against the *current* row order
        # before moving either list.  ``group_cause_description_lines`` uses
        # the supplied ids to decide which tag belongs to each stored line;
        # passing already-swapped ids made the old tag look like free text on
        # its new owner's row (for example ``FV-1 FI-1 fails low``).
        source_ids = self._group_equipment_ids(cause)
        target = row_index + delta
        if (not cause or not (0 <= row_index < len(source_ids))
                or not (0 <= target < len(source_ids))):
            return
        lines = self.db.group_cause_description_lines(cause, source_ids)
        ids = list(source_ids)
        ids[row_index], ids[target] = ids[target], ids[row_index]
        tags = []
        for equipment_id in ids:
            equipment = self.db.get_equipment_by_id(equipment_id)
            tags.append((equipment.get('tag') if equipment else '') or 'Objekt')
        old_ops = self._group_link_operators(cause, len(ids))
        # Keep operator positions stable while object rows move.  The user
        # can change each affected row's incoming operator independently from
        # its own context menu, without silently changing other connections.
        # Keep group descriptions aligned with their object rows.
        lines[row_index], lines[target] = lines[target], lines[row_index]
        self.db.update_cause(
            cause_id,
            comp_type=(self.db.get_equipment_by_id(ids[0]) or {}).get('equipment_type', ''),
            comp_tag=self._group_comp_tag(tags, old_ops),
            equipment_id=ids[0],
            secondary_equipment_id=ids[1] if len(ids) > 1 else None,
            group_equipment_ids=ids,
            description='\n'.join(lines))
        self._group_cause_changed(cause_id)

    def _show_group_tag_menu(self, row, cause_id, group_line, global_pos):
        menu = QMenu(self)
        up = menu.addAction('Flytta uppåt')
        if group_line == 1:
            up.setText('Flytta uppåt (byt till primär)')
        up.setEnabled(group_line > 0)
        up.triggered.connect(lambda: self._move_group_row(cause_id, group_line, -1))
        down = menu.addAction('Flytta nedåt')
        cause = self.db.get_cause(cause_id)
        count = len(self._group_equipment_ids(cause))
        if group_line == 0 and count > 1:
            down.setText('Flytta nedåt (byt till sekundär)')
        down.setEnabled(group_line < count - 1)
        down.triggered.connect(lambda: self._move_group_row(cause_id, group_line, 1))
        if group_line > 0:
            menu.addSeparator()
            submenu = menu.addMenu('Koppling till föregående rad')
            current = self._group_operators(self._table.item(row, self._C_ORS))
            current = current[group_line] if group_line < len(current) else 'OR'
            for operator in ('&', 'OR', '->'):
                action = submenu.addAction(operator.replace('&', '&&'))
                action.setCheckable(True)
                action.setChecked(operator == current)
                action.triggered.connect(
                    lambda _checked=False, op=operator:
                    self._set_group_row_operator(cause_id, group_line, op))
        menu.exec(global_pos)

    def _choose_group_operator(self, row, cause_id, group_line, global_pos):
        menu = QMenu(self)
        for operator in ('&', 'OR', '->'):
            # In Qt menu text, a single ampersand marks a mnemonic and is
            # therefore not painted. Escape it for display while keeping
            # the real operator in QAction.data().
            action = menu.addAction(operator.replace('&', '&&'))
            action.setData(operator)
        chosen = menu.exec(global_pos)
        if chosen is not None:
            self._set_group_row_operator(cause_id, group_line, chosen.data())

    def _ors_tag_prefix_pixel_width(self, item, desc, font):
        """Pixel width of the bold portion of _ors_combined_text — shared
        by the click zone (what counts as "clicked the tag") and
        updateEditorGeometry (where the description editor should
        start), so a click always lands exactly where the bold text
        visually ends."""
        group_tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
        if len(group_tags) >= 2:
            return 0
        tag_label, show_tag = self._ors_tag_prefix(item)
        if tag_label == 'ej på P&ID':
            return 0
        if not show_tag:
            return 0
        trivial = desc.strip() in ('', 'Ny orsak')
        bold_font = QFont(font)
        bold_font.setBold(True)
        fm = QFontMetrics(bold_font)
        prefix = tag_label if trivial else f"{tag_label}, "
        return fm.horizontalAdvance(prefix)

    def _ors_standard_causes_for_row(self, row, group_line=None,
                                     equipment_override=None):
        """(std_dev_id, comp_type, dev_description, rows) of standard
        causes applicable to this ORS row's deviation + object type
        (2026-08-25, see NOTES.md "Standardorsak-popup vid redigering av
        Orsak-cellen"). Shared by _PidDelegate._attach_cause_completer
        (the existing typing-suggestion dropdown) and
        StandardCauseSuggestPopup (the new automatic picker) so the
        dev_id -> std_dev_id -> object_id resolution chain only lives in
        one place. `rows` follows the same fallback cascade used by the
        previous cause picker:
        the richer deviation+object hierarchy first, then comp_type
        matched against this specific deviation's text, then comp_type
        with no deviation filter at all — `rows` is [] if nothing
        matches any step (e.g. no comp_type known yet). Deliberately
        does NOT fall further back to "every standard cause in the
        database" the way the completer's OWN fallback still does after
        calling this — that's fine for a type-to-filter completer, but
        would make this popup's button list unusably long and
        unrelated to the current row."""
        row_meta = getattr(self, '_row_meta', [])
        dev_id = row_meta[row][0] if row < len(row_meta) else None
        cause_id = row_meta[row][1] if row < len(row_meta) else None
        item = self._table.item(row, self._C_ORS)
        obj_data = item.data(Qt.ItemDataRole.UserRole + 2) if item else None

        # The database row is authoritative.  Cell roles are rendering
        # metadata and can be stale for a moment after an object was dragged,
        # selected from the catalogue or created through the tree.  Resolve
        # the live object/type first; only keep the cell role as a legacy
        # fallback for partially migrated projects.
        cause = dict(self.db.get_cause(cause_id)) if cause_id else {}
        comp_type = (cause.get('comp_type') or
                     (obj_data or ('', ''))[0] or '')
        equipment_id = cause.get('equipment_id')
        if group_line is not None and group_line >= 0 and cause:
            equipment_ids = self._group_equipment_ids(cause)
            if group_line < len(equipment_ids):
                equipment_id = equipment_ids[group_line]
        equipment = (equipment_override if group_line is None and equipment_override
                     else self.db.get_equipment_by_id(equipment_id)
                     if equipment_id else None)
        if equipment:
            comp_type = equipment.get('equipment_type') or comp_type

        dev = self.db.get_deviation(dev_id) if dev_id is not None else None
        dev_description = dev['description'] if dev else None
        std_dev_id, _obj_id, rows = standard_cause_options(
            self.db, dev_description, comp_type)
        return std_dev_id, comp_type, dev_description, rows

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
        freq_zone_w = _RRF_W if freq_str else 0
        freq_zone_x = row_right - freq_zone_w if freq_zone_w else row_right
        return freq_zone_x, freq_zone_w, freq_str

    def _ors_comment_dot_geometry(self, cell_rect):
        """The small comment-indicator dot at the right edge of the ORS
        cell's first line — the ONLY icon there since the 2026-08-18
        fill-status "plupp" removal (see _PidDelegate.paint()'s own
        "Comment dot" comment). Sat at the right edge of the old, now-
        removed tag strip (2026-08-25, see NOTES.md "Slå ihop
        objektbaren i Orsak-kolumnen") — same "y = first line's own
        vertical center" concept, now measured from the cell's top
        directly since there's no more separate strip to anchor to.
        Shared by paint() (which only draws it when
        _has_comment) and eventFilter()'s click zone so the two can
        never disagree about where it is — they used to (2026-08-20
        follow-up, found while centralizing zone geometry more broadly):
        eventFilter() still hit-tested a defunct 18/20px "clone"+
        "comment" icon PAIR left over from an older three-icon design
        that no longer exists. Two real, live bugs from that: (1) the
        comment zone's own bounds were written as `pos.x() >= X and
        pos.x() < X` (`cmt_right` used as both ends) — mathematically
        always False, so `_open_comment_popup()` was completely
        unreachable via the UI, and (2) the "clone" zone covered blank
        space nowhere near the actual dot, so clicking near the visible
        dot silently fired "Duplicera scenario" instead of doing
        nothing — a moderately destructive-adjacent action triggered by
        an unlabelled, invisible click target. Clone itself needed no
        fix; it's already available, clearly labelled, from the
        right-click context menu (see the other `_clone_scenario` call
        site) — only the comment dot needed a real, working zone here,
        so that's the only icon this geometry describes now."""
        dot_r = 4
        dot_y = cell_rect.top() + _ORS_FIRST_LINE_H // 2
        dot_x = cell_rect.right() - 5
        return QRect(dot_x - dot_r, dot_y - dot_r, dot_r * 2, dot_r * 2)

    def _sg_rrf_zone_geometry(self, cell_rect):
        """The RRF badge zone at the right of a safeguard cell — shared
        between paint() and eventFilter()."""
        return QRect(cell_rect.right() - _RRF_W, cell_rect.top(),
                     _RRF_W, cell_rect.height())

    def _sg_row_height(self, base_font):
        """Single source of truth for a safeguard row's height — used by
        _ScenarioDelegate._size_hint_impl, _compute_row_height AND
        _PidDelegate.paint()'s SG branch, so all three can never disagree
        about how tall a safeguard row is (same rule as _ORS_FIRST_LINE_H's
        own docstring elsewhere in this file).

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

    @staticmethod
    def _replace_tag_occurrence(text, old_tag, new_tag, offset=None):
        """Replace one whole-tag occurrence, preferring the clicked offset."""
        text = str(text or '')
        old_tag = str(old_tag or '').strip()
        new_tag = str(new_tag or '').strip()
        if not old_tag or not new_tag:
            return text
        matches = list(re.finditer(
            r'(?<![A-Za-z0-9])' + re.escape(old_tag) + r'(?![A-Za-z0-9])',
            text, re.IGNORECASE))
        if not matches:
            return text
        if offset is None:
            match = matches[0]
        else:
            match = min(matches, key=lambda candidate:
                        abs(candidate.start() - max(0, int(offset))))
        return text[:match.start()] + new_tag + text[match.end():]

    def _text_tag_click_context(self, row, col, point):
        """Resolve a click in bold free text to one precise object reference.

        ORS's primary/group identities have their own explicit geometry and
        are handled by ``_show_cause_obj_popup``.  KON, SG and REK render
        object tags inside free text, so they share this layout-driven path.
        """
        if row < 0 or row >= len(self._row_meta):
            return None
        item = self._table.item(row, col)
        if item is None:
            return None
        index = self._table.model().index(row, col)
        cell_rect = self._table.visualRect(index)
        display = ''
        rect = QRect(cell_rect)
        kind = None
        id_ = None
        prefix_len = 0
        cons_id = self._row_meta[row][2]

        if col == self._C_ORS:
            # The primary tag itself is handled earlier by the precise ORS
            # prefix zone.  This path covers an additional catalogue object
            # mentioned in the cause free text, without turning the cause's
            # primary equipment link into that secondary reference.
            group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or []
            if len(group_tags) >= 2:
                return None
            kind, id_ = 'cause_text', self._row_meta[row][1]
            if id_ is None:
                return None
            description = item.text() or ''
            display = self._ors_combined_text(item, description)
            description_start = display.casefold().find(description.casefold())
            if description_start < 0:
                return None
            prefix_len = description_start
            rect = cell_rect.adjusted(2, 2, -2 - _RRF_W, -2)
        elif col == self._C_KON:
            kind, id_ = 'consequence', cons_id
            if id_ is None:
                return None
            number = item.data(Qt.ItemDataRole.UserRole + 10)
            if number:
                prefix = f'{number}.  '
                prefix_len = len(prefix)
                display = prefix + (item.text() or '')
            else:
                display = item.text() or ''
            rect = cell_rect.adjusted(2, 2, -2, -2)
        elif col == self._C_SG:
            kind, id_ = 'safeguard', self._row_meta[row][3]
            if id_ is None:
                return None
            number = item.data(Qt.ItemDataRole.UserRole + 10)
            if number:
                prefix = f'{number}.  '
                prefix_len = len(prefix)
                display = prefix + (item.text() or '')
            else:
                display = item.text() or ''
            rect = QRect(cell_rect.left(), cell_rect.top(),
                         max(1, cell_rect.width() - _RRF_W), cell_rect.height())
            rect = rect.adjusted(2, 1, -2, -1)
        elif col == self._C_REK:
            rec_id = (self._row_recommendation_ids[row]
                      if row < len(self._row_recommendation_ids) else None)
            if not cons_id or not rec_id:
                return None
            rec = self.db.get_recommendation(rec_id)
            if not rec:
                return None
            kind, id_ = 'recommendation', rec_id
            display = item.text() or ''
            description = str(rec.get('description') or '')
            description_start = display.casefold().find(description.casefold())
            if description_start < 0:
                return None
            prefix_len = description_start
            rect = cell_rect.adjusted(5, 2, -3, -2)
        else:
            return None

        tags = self._matching_pid_tags(display)
        hit = find_bold_tag_at_position(display, tags, rect, point,
                                        self._table.font(), word_wrap=True)
        if not hit:
            return None
        equipment = self.db.get_equipment_by_tag(hit['tag'])
        if not equipment:
            return None
        return {
            'kind': kind,
            'id': id_,
            'row': row,
            'cons_id': cons_id,
            'tag': hit['tag'],
            'text_offset': max(0, hit['start'] - prefix_len),
            'equipment': equipment,
        }

    def _show_object_tag_popup(self, context, global_pos):
        """Show the shared object action popup for one resolved reference."""
        equipment = context.get('equipment')
        if not equipment:
            return False
        popup = _ObjectTagActionPopup(self.db, equipment, parent=self)
        popup.object_selected.connect(
            lambda selected, ctx=context: self._replace_object_reference(ctx, selected))
        popup.rename_requested.connect(
            lambda new_tag, eid=equipment.get('id'):
                self._rename_object_from_popup(eid, new_tag))
        popup.type_change_requested.connect(
            lambda equipment_type, eid=equipment.get('id'):
                self._change_object_type_from_popup(eid, equipment_type))
        popup.adjustSize()
        screen = (QApplication.screenAt(global_pos) or
                  QApplication.primaryScreen()).availableGeometry()
        width, height = popup.sizeHint().width(), popup.sizeHint().height()
        x, y = global_pos.x(), global_pos.y() - height - 6
        if y < screen.top():
            y = global_pos.y() + 6
        x = max(screen.left() + 4, min(x, screen.right() - width - 4))
        y = max(screen.top() + 4, min(y, screen.bottom() - height - 4))
        popup.move(x, y)
        popup.show()
        popup.raise_()
        return True

    def _replace_object_reference(self, context, equipment):
        """Replace an object reference as one undoable edit."""
        with self.db.history_group():
            return self._replace_object_reference_inner(context, equipment)

    def _replace_object_reference_inner(self, context, equipment):
        """Apply an explicit ``Byt objekt`` choice without renaming anything."""
        equipment = dict(equipment or {})
        if not equipment.get('id') or not equipment.get('tag'):
            return
        kind = context.get('kind')
        old_tag = context.get('tag') or ''
        if kind == 'cause':
            cause_id = context.get('id')
            group_line = context.get('group_line')
            if group_line is not None:
                self._connect_group_cause_object(cause_id, group_line, equipment)
                return
            cause = self.db.get_cause(cause_id)
            if not cause:
                return
            self.db.update_cause(
                cause_id, comp_type=equipment.get('equipment_type') or '',
                comp_tag=equipment.get('tag') or '', equipment_id=equipment['id'])
            self._adopt_deviation_equipment(cause.get('deviation_id'), equipment['id'])
            self.item_edited.emit(CAUSE_T, cause_id)
            self._schedule_rebuild()
            return

        id_ = context.get('id')
        offset = context.get('text_offset')
        if kind == 'cause_text':
            row = self.db.get_cause(id_)
            if not row:
                return
            description = self._replace_tag_occurrence(
                row.get('description') or '', old_tag, equipment['tag'], offset)
            self.db.update_cause(id_, description=description)
            self.item_edited.emit(CAUSE_T, id_)
        elif kind == 'consequence':
            row = self.db.get_consequence(id_)
            if not row:
                return
            row = dict(row)
            description = self._replace_tag_occurrence(
                row.get('description') or '', old_tag, equipment['tag'], offset)
            refs = self._matching_pid_tags(description)
            active = refs[-1] if refs else ''
            active_equipment = self.db.get_equipment_by_tag(active) if active else None
            self.db.update_consequence(
                id_, description, row.get('severity'), row.get('category') or '',
                row.get('consequence_chain') or '', comp_tag=active,
                comp_type=(active_equipment or {}).get('equipment_type') or '',
                tagged_refs=','.join(refs))
            self.item_edited.emit(CONS_T, id_)
        elif kind == 'safeguard':
            row = self.db.get_safeguard(id_)
            if not row:
                return
            row = dict(row)
            description = self._replace_tag_occurrence(
                row.get('description') or '', old_tag, equipment['tag'], offset)
            refs = self._matching_pid_tags(description)
            active = refs[-1] if refs else ''
            active_equipment = self.db.get_equipment_by_tag(active) if active else None
            self.db.update_safeguard(id_, description=description,
                                     tagged_refs=','.join(refs))
            self.db.set_safeguard_tag(
                id_, active,
                (active_equipment or {}).get('equipment_type') or '')
            self.item_edited.emit(SG_T, id_)
        elif kind == 'recommendation':
            row = self.db.get_recommendation(id_)
            if not row:
                return
            description = self._replace_tag_occurrence(
                row.get('description') or '', old_tag, equipment['tag'], offset)
            self.db.update_recommendation(id_, description=description)
            self.item_edited.emit(CONS_T, context.get('cons_id'))
        else:
            return
        # A tag swap can change row wrapping, number references and the
        # P&ID-context counters.  Rebuild once rather than patching several
        # partially-overlapping row roles in place.
        self._schedule_rebuild()

    def _connect_group_cause_object(self, cause_id, group_line, equipment):
        """Replace one group member while preserving all other group rows."""
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        cause = dict(cause)
        equipment_ids = self._group_equipment_ids(cause)
        if group_line < 0 or group_line >= len(equipment_ids):
            return
        lines = self.db.group_cause_description_lines(cause, equipment_ids)
        old_equipment = self.db.get_equipment_by_id(equipment_ids[group_line])
        old_tag = (old_equipment or {}).get('tag') or ''
        equipment_ids[group_line] = equipment['id']
        tags = []
        for equipment_id in equipment_ids:
            member = self.db.get_equipment_by_id(equipment_id) if equipment_id else None
            tags.append((member or {}).get('tag') or 'Objekt')
        lines[group_line] = self._replace_tag_occurrence(
            lines[group_line], old_tag, equipment['tag'], 0)
        operator = self._group_link_operators(cause, len(equipment_ids))
        primary = self.db.get_equipment_by_id(equipment_ids[0]) if equipment_ids else None
        self.db.update_cause(
            cause_id,
            comp_type=(primary or {}).get('equipment_type') or cause.get('comp_type') or '',
            comp_tag=self._group_comp_tag(tags, operator),
            equipment_id=equipment_ids[0] if equipment_ids else None,
            secondary_equipment_id=(equipment_ids[1] if len(equipment_ids) > 1 else None),
            group_equipment_ids=equipment_ids,
            description='\n'.join(lines))
        self._group_cause_changed(cause_id)

    def _rename_object_from_popup(self, equipment_id, new_tag):
        equipment = self.db.get_equipment_by_id(equipment_id)
        if not equipment:
            return
        old_tag = (equipment.get('tag') or '').strip()
        if old_tag.casefold() == str(new_tag or '').strip().casefold():
            return
        answer = QMessageBox.question(
            self, 'Bekräfta namnbyte',
            f'Byt namn på objektet från {old_tag} till {new_tag}?\n\n'
            'Det uppdaterar P&ID-markören och aktuella HAZOP-referenser.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.db.rename_equipment_and_references(equipment_id, new_tag)
        except ValueError as error:
            QMessageBox.warning(self, 'Kan inte byta namn', str(error))
            return
        self.equipment_renamed.emit()
        self.structure_changed.emit()
        self._schedule_rebuild()

    def _change_object_type_from_popup(self, equipment_id, equipment_type):
        """Change the catalogue type of one object without rebinding it.

        The tag and object id stay untouched, so every existing bold-tag
        reference continues to point to the same catalogue object. P&ID
        overlays resolve their type live from the catalogue on refresh.
        """
        equipment = self.db.get_equipment_by_id(equipment_id)
        if not equipment:
            return
        equipment = dict(equipment)
        new_type = str(equipment_type or '').strip()
        if not new_type or new_type == (equipment.get('equipment_type') or ''):
            return
        self.db.update_equipment_item(
            equipment_id, equipment.get('tag') or '',
            equipment.get('prefix') or '', new_type,
            equipment.get('description') or '')
        self.equipment_renamed.emit()
        self.structure_changed.emit()
        self._schedule_rebuild()

    def _show_cause_obj_popup(self, row, cause_id, global_pos, group_line=None):
        """Open the shared object popup for the clicked primary/group tag."""
        item      = self._table.item(row, self._C_ORS)
        group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or [] if item else []
        group_operator = self._group_operator(item) if len(group_tags) >= 2 else None
        # Grouped object tags use the same compact popup as ordinary cause
        # Primär/Sekundär popup from this shared helper.
        obj_data  = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        equipment_id = None
        if len(group_tags) >= 2 and group_line is not None and 0 <= group_line < len(group_tags):
            cause = self.db.get_cause(cause_id)
            equipment_ids = self._group_equipment_ids(cause)
            equipment_id = (equipment_ids[group_line]
                            if cause and group_line < len(equipment_ids) else None)
            equipment = self.db.get_equipment_by_id(equipment_id) if equipment_id else None
            comp_type = equipment.get('equipment_type', '') if equipment else ''
            comp_tag = equipment.get('tag', '') if equipment else group_tags[group_line]
        else:
            comp_type, comp_tag = obj_data if obj_data else ('', '')
            cause = self.db.get_cause(cause_id)
            equipment_id = cause.get('equipment_id') if cause else None
            equipment = self.db.get_equipment_by_id(equipment_id) if equipment_id else None
            if equipment is None and comp_tag:
                equipment = self.db.get_equipment_by_tag(comp_tag)

        # The common case is a real catalogue object.  Use the same two
        # explicit actions as KON/SG/REK so a plain tag click is never
        # ambiguous between connecting a different object and renaming this
        # one.  Keep the legacy compact type picker for an old/unlinked cause
        # tag: it is still the only path that can define such a loose tag.
        if equipment:
            return self._show_object_tag_popup({
                'kind': 'cause', 'id': cause_id, 'row': row,
                'group_line': group_line, 'tag': equipment.get('tag') or comp_tag,
                'equipment': equipment,
            }, global_pos)

        popup = CauseTagPopup(
            self.db, comp_type, comp_tag, parent=self, cause_id=cause_id,
            equipment_id=equipment_id, group_operator=group_operator)
        popup.bind_requested.connect(
            lambda cid=cause_id: self.bind_cause_to_pid_requested.emit(cid))
        if group_line is not None and group_line >= 0:
            popup.committed.connect(
                lambda ct, tg, r=row, cid=cause_id, line=group_line:
                    self._apply_group_cause_obj(r, cid, line, ct, tg))
        else:
            popup.committed.connect(
                lambda ct, tg, r=row, cid=cause_id:
                    self._apply_cause_obj(r, cid, ct, tg, '', None))
        popup.place_requested.connect(
            lambda cid, ct, tg: self.place_cause_object_requested.emit(cid, ct, tg))
        popup.adjustSize()
        # Prefer ABOVE global_pos (2026-08-26, see NOTES.md "Flytta
        # HAZOP-popups ovanför"), falling back to below only if there's
        # no room above on screen.
        _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = _scr.availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x, y   = global_pos.x(), global_pos.y() - ph - 6
        if y < screen.top(): y = global_pos.y() + 6
        if x + pw > screen.right():  x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top()  + 4, min(y, screen.bottom() - ph))
        popup.move(x, y)
        popup.show()

    def _apply_group_cause_obj(self, row, cause_id, group_line,
                               comp_type, comp_tag):
        """Apply a tag/type edit to the object whose group tag was clicked."""
        cause = self.db.get_cause(cause_id)
        if not cause or group_line is None or group_line < 0:
            return
        equipment_ids = self._group_equipment_ids(cause)
        if group_line >= len(equipment_ids):
            return
        lines = self.db.group_cause_description_lines(cause, equipment_ids)
        old_id = equipment_ids[group_line]
        new_tag = (comp_tag or '').strip()
        selected_id = old_id
        selected_tag = ''
        renamed = False
        old_eq = self.db.get_equipment_by_id(old_id) if old_id else None
        if old_eq:
            selected_tag = (old_eq.get('tag') or '').strip()
            if new_tag and new_tag.casefold() != selected_tag.casefold():
                match = self.db.get_equipment_by_tag(new_tag)
                decision = self._confirm_equipment_tag_change(
                    selected_tag, new_tag, match, linked=True)
                if decision == 'cancel':
                    return
                if decision == 'connect' and match and match.get('id') != old_id:
                    selected_id = match['id']
                    selected_tag = (match.get('tag') or new_tag).strip()
                else:
                    duplicate = self.db.get_equipment_by_tag(new_tag)
                    if duplicate and duplicate.get('id') != old_id:
                        QMessageBox.warning(
                            self, "Taggen finns redan",
                            f"Taggen {new_tag} används redan av ett annat objekt "
                            "på denna P&ID. Välj Koppla för att använda det objektet.")
                        return
                    self.db.rename_equipment_and_references(old_id, new_tag)
                    selected_tag = new_tag
                    renamed = True
        elif new_tag:
            match = self.db.get_equipment_by_tag(new_tag)
            if match:
                decision = self._confirm_equipment_tag_change(
                    '', new_tag, match, linked=False)
                if decision == 'cancel':
                    return
                if decision == 'connect':
                    selected_id = match['id']
                    selected_tag = (match.get('tag') or new_tag).strip()

        old_tag = (old_eq or {}).get('tag') or ''
        if selected_tag and selected_tag.casefold() != old_tag.casefold():
            lines[group_line] = self._replace_tag_occurrence(
                lines[group_line], old_tag, selected_tag, 0)
        equipment_ids[group_line] = selected_id
        primary_id = equipment_ids[0] if equipment_ids else None
        secondary_id = equipment_ids[1] if len(equipment_ids) > 1 else None
        primary = self.db.get_equipment_by_id(primary_id) if primary_id else None
        tags = []
        for index, equipment_id in enumerate(equipment_ids):
            equipment = self.db.get_equipment_by_id(equipment_id) if equipment_id else None
            tag = (equipment.get('tag') or '').strip() if equipment else ''
            if index == group_line and selected_tag:
                tag = selected_tag
            tags.append(tag or 'Objekt')
        operators = self._group_link_operators(cause, len(equipment_ids))
        self.db.update_cause(
            cause_id,
            comp_type=(primary.get('equipment_type') if primary
                       else cause.get('comp_type') or comp_type),
            comp_tag=self._group_comp_tag(tags, operators),
            equipment_id=primary_id,
            secondary_equipment_id=secondary_id,
            group_equipment_ids=equipment_ids,
            description='\n'.join(lines))
        if renamed:
            self.equipment_renamed.emit()
            self.structure_changed.emit()
        self._group_cause_changed(cause_id)

    def _show_group_cause_popup(self, row, cause_id, global_pos,
                                only_column=None):
        """Open the two-column editor used for a grouped cause."""
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        cause = dict(cause)
        primary = self.db.get_equipment_by_id(cause.get('equipment_id'))
        secondary = self.db.get_equipment_by_id(cause.get('secondary_equipment_id'))
        if not primary or not secondary:
            return
        primary, secondary = dict(primary), dict(secondary)
        desc = (cause.get('description') or '').lower()
        choices_set = int(cause.get('group_choices_set') or 0)
        direction = ('Felar lågt' if 'felar lågt' in desc else 'Felar högt') \
            if choices_set & 1 else 'Ej vald'
        effect = next((value for value in (
            'Öppnar felaktigt', 'Stänger felaktigt', 'Öppnar fullt',
            'Stänger helt') if value.lower() in desc), 'Öppnar fullt') \
            if choices_set & 2 else 'Ej vald'
        popup = GroupCausePopup(primary, secondary, direction, effect,
                                parent=self, only_column=only_column)
        def apply_choice(which, choice):
            self._apply_group_cause_choice(cause_id, which, choice)
            popup.set_current(which, choice)
        popup.choice_requested.connect(apply_choice)
        popup.adjustSize()
        screen = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        x = min(global_pos.x(), screen.right() - popup.width() - 4)
        y = global_pos.y() - popup.height() - 6
        if y < screen.top():
            y = global_pos.y() + 6
        popup.move(max(screen.left() + 4, x),
                   max(screen.top() + 4, min(y, screen.bottom() - popup.height())))
        popup.show()

    def _edit_group_cause_choice(self, cause_id, which):
        """Show choices for the first/second ellipsis in a group cause."""
        cause = self.db.get_cause(cause_id)
        if not cause or not cause.get('secondary_equipment_id'):
            return
        primary = self.db.get_equipment_by_id(cause.get('equipment_id'))
        secondary = self.db.get_equipment_by_id(cause.get('secondary_equipment_id'))
        if not primary or not secondary:
            return
        menu = QMenu(self)
        choices = (['Felar högt', 'Felar lågt'] if which == 0 else
                   ['Öppnar felaktigt', 'Stänger felaktigt', 'Öppnar fullt',
                    'Stänger helt', 'Skriv eget…'])
        for choice in choices:
            act = menu.addAction(choice)
            act.triggered.connect(lambda _=False, c=choice:
                                   self._apply_group_cause_choice(cause_id, which, c))
        menu.exec(QCursor.pos())

    def _apply_group_cause_choice(self, cause_id, which, choice):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        # Kept for compatibility with old quick-choice triggers.  Route the
        # active behaviour through the canonical N-member representation so
        # this legacy helper cannot collapse tertiary and later rows back
        # into a primary/secondary-only description.
        group_ids = self._group_equipment_ids(cause)
        if len(group_ids) >= 2 and 0 <= which < len(group_ids):
            lines = self.db.group_cause_description_lines(cause, group_ids)
            # Older groups often have valid event text but no bitmask.  Keep
            # the first two legacy choice flags in sync without inferring or
            # changing any later member rows.
            choices_set = int(cause.get('group_choices_set') or 0)
            for line_no, bit in ((0, 1), (1, 2)):
                if line_no >= len(lines):
                    continue
                member = self.db.get_equipment_by_id(group_ids[line_no])
                member_tag = str((member or {}).get('tag') or '').strip()
                tail = lines[line_no]
                if member_tag and tail.casefold().startswith(member_tag.casefold()):
                    tail = tail[len(member_tag):].strip(' ,:;->')
                if tail:
                    choices_set |= bit
            if str(choice or '').startswith('Skriv'):
                value, accepted = QInputDialog.getText(
                    self, 'Eget alternativ',
                    'Skriv in önskad felmekanism/effekt:')
                if not accepted or not value.strip():
                    return
                choice = value.strip()
            equipment = self.db.get_equipment_by_id(group_ids[which])
            tag = str((equipment or {}).get('tag') or '').strip()
            if not tag:
                tag = lines[which].split(' ', 1)[0]
            lines[which] = f'{tag} {str(choice or "").strip().lower()}'.strip()
            if which == 0:
                choices_set |= 1
            elif which == 1:
                choices_set |= 2
            self.db.update_cause(cause_id, description='\n'.join(lines),
                                 group_choices_set=choices_set)
            self._group_cause_changed(cause_id)
            return
        p = self.db.get_equipment_by_id(cause.get('equipment_id'))
        s = self.db.get_equipment_by_id(cause.get('secondary_equipment_id'))
        if not p or not s:
            return
        old = cause.get('description') or ''
        choices_set = int(cause.get('group_choices_set') or 0)
        old_lines = [line.strip() for line in old.splitlines() if line.strip()]
        primary_tag = str(p.get('tag', 'Objekt')).strip()
        secondary_tag = str(s.get('tag', 'Objekt')).strip()

        def existing_group_line(tag):
            folded = tag.casefold()
            for line in old_lines:
                line_lower = line.casefold()
                tag_pos = line_lower.find(folded)
                if tag_pos < 0:
                    continue
                value = line[tag_pos:]
                # Legacy groups stored both events in one arrow sentence.
                # Extract only this object's segment before returning it as
                # the independent line used by the new two-row model.
                other_tag = secondary_tag if folded == primary_tag.casefold() else primary_tag
                other_pos = value.casefold().find(other_tag.casefold(), len(tag))
                if other_pos >= 0:
                    value = value[:other_pos].rstrip(' ,:→-')
                return value.strip()
            return ''

        existing_primary = existing_group_line(primary_tag)
        existing_secondary = existing_group_line(secondary_tag)

        # Older grouped causes were stored without the choice bitmask. Infer
        # already-entered events from two non-empty tagged lines, but do not
        # treat bare tags (the new-group placeholder) as selected choices.
        for bit, tag, line in ((1, primary_tag, existing_primary),
                               (2, secondary_tag, existing_secondary)):
            remainder = line[len(tag):].strip(' ,:→-') if line else ''
            if remainder and not choices_set & bit:
                choices_set |= bit
        direction = 'felar lågt' if 'lågt' in old else 'felar högt'
        effect = 'öppnar felaktigt'
        for option in ('stänger helt', 'stänger felaktigt', 'öppnar fullt', 'öppnar felaktigt'):
            if option in old.lower():
                effect = option
                break
        if choice.startswith('Skriv fritext'):
            value, accepted = QInputDialog.getText(
                self, "Eget alternativ",
                "Skriv in önskad felmekanism/effekt:")
            if not accepted or not value.strip():
                return
            choice = value.strip()
        if which == 0:
            direction = choice.lower()
            choices_set |= 1
        else:
            effect = choice.lower()
            choices_set |= 2
        if choice == 'Skriv eget…':
            value, accepted = QInputDialog.getText(
                self, "Eget alternativ",
                "Skriv in önskad felmekanism/effekt:")
            if not accepted or not value.strip():
                return
            choice = value.strip()
        # Always retain both linked objects as two rows.  A row without a
        # selected mechanism is represented by its bare tag, never removed.
        lines = [
            (existing_primary if which != 0 and existing_primary
             else f"{primary_tag} {direction}" if choices_set & 1
             else primary_tag),
            (existing_secondary if which != 1 and existing_secondary
             else f"{secondary_tag} {effect}" if choices_set & 2
             else secondary_tag),
        ]
        desc = '\n'.join(lines)
        self.db.update_cause(cause_id, description=desc,
                             group_choices_set=choices_set)
        self._group_cause_changed(cause_id)

    def _swap_group_objects(self, cause_id):
        """Compatibility entry point for the old two-object swap control."""
        cause = self.db.get_cause(cause_id)
        if not cause or len(self._group_equipment_ids(cause)) < 2:
            return
        self._move_group_row(cause_id, 0, 1)

    def _apply_cause_obj(self, row, cause_id, comp_type, comp_tag, description, frequency):
        """Apply a cause identity/text edit as one undoable action."""
        with self.db.history_group():
            return self._apply_cause_obj_inner(
                row, cause_id, comp_type, comp_tag, description, frequency)

    def _apply_cause_obj_inner(self, row, cause_id, comp_type, comp_tag,
                               description, frequency):
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
            old_tag = (old_eq.get('tag') or '').strip() if old_eq else ''
            if (old_eq and comp_type and
                    not (old_eq.get('equipment_type') or '').strip()):
                self.db.update_equipment_item(
                    old_equipment_id, old_tag, old_eq.get('prefix') or '',
                    comp_type, old_eq.get('description') or '')
            if old_eq and new_tag and new_tag.casefold() != old_tag.casefold():
                match = self.db.get_equipment_by_tag(new_tag)
                decision = self._confirm_equipment_tag_change(
                    old_tag, new_tag, match, linked=True)
                if decision == 'cancel':
                    return
                if decision == 'connect' and match and match.get('id') != old_equipment_id:
                    # Reuse the existing catalog identity; never rename the
                    # old object or create a duplicate catalog row.
                    equipment_id = match['id']
                else:
                    # Explicit "rename only" is still protected against
                    # duplicate tags.  The catalog update is the one place
                    # where the P&ID identity changes, so do it only after
                    # the user has confirmed the operation.
                    duplicate = self.db.get_equipment_by_tag(new_tag)
                    if duplicate and duplicate.get('id') != old_equipment_id:
                        QMessageBox.warning(
                            self, "Taggen finns redan",
                            f"Taggen {new_tag} används redan av ett annat objekt "
                            "på denna P&ID. Välj Koppla för att använda det objektet.")
                        return
                    self.db.rename_equipment_and_references(
                        old_equipment_id, new_tag)
                    self.equipment_renamed.emit()
                    renamed = True
        elif new_tag:
            match = self.db.get_equipment_by_tag(new_tag)
            if match:
                decision = self._confirm_equipment_tag_change(
                    '', new_tag, match, linked=False)
                if decision == 'cancel':
                    return
                if decision == 'connect':
                    equipment_id = match['id']


        # Do all DB writes first — learning is handled inside update_cause
        self.db.update_cause(cause_id, comp_type=comp_type, comp_tag=comp_tag,
                              equipment_id=equipment_id)
        if equipment_id is not None:
            self._adopt_deviation_equipment(cause.get('deviation_id'), equipment_id)
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

    def _confirm_equipment_tag_change(self, old_tag, new_tag, match, linked=False):
        """Return ``connect``, ``rename`` or ``cancel`` for a tag edit.

        Tag edits represent object identity, unlike ordinary scenario text.
        Keeping this decision in one helper makes every popup/inline path use
        the same guarded workflow and makes it straightforward to mock in
        regression tests.
        """
        if match and (not linked or match.get('tag', '').casefold() != (old_tag or '').casefold()):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Koppla objekt")
            if old_tag:
                intro = f"Du har ändrat objektnamnet från {old_tag} till {new_tag}.\n\n"
            else:
                intro = "Den här taggen matchar ett identifierat objekt.\n\n"
            details = (f"Taggen {new_tag} finns redan på denna P&ID.\n"
                       f"Objekt: {match.get('tag') or new_tag}"
                       f" ({match.get('equipment_type') or 'Okänd typ'})")
            box.setText(intro + details)
            connect = box.addButton(f"Koppla till {new_tag}", QMessageBox.ButtonRole.AcceptRole)
            rename = box.addButton("Byt endast namn", QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton("Avbryt", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is connect:
                return 'connect'
            if clicked is rename:
                return 'rename'
            return 'cancel'

        if old_tag:
            answer = QMessageBox.question(
                self, "Bekräfta namnbyte",
                f"Du har ändrat objektnamnet från {old_tag} till {new_tag}.\n\n"
                f"Taggen {new_tag} finns inte bland identifierade objekt på denna P&ID.\n"
                "Vill du byta namn på objektet?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return 'rename' if answer == QMessageBox.StandardButton.Yes else 'cancel'
        return 'rename'

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
            self.db.update_cause(cause_id, likelihood=f_level, base_frequency=None,
                                 frequency_cleared=False)
        elif numeric is None:
            # Keep the selected standard cause and its text/object link, but
            # explicitly suppress only its frequency for this cause.
            self.db.update_cause(cause_id, likelihood=0, base_frequency=None,
                                 frequency_cleared=True)
        else:
            self.db.update_cause(cause_id, base_frequency=numeric,
                                 frequency_cleared=False)
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
        # Prefer ABOVE global_pos (2026-08-26, see NOTES.md "Flytta
        # HAZOP-popups ovanför"), falling back to below only if there's
        # no room above on screen.
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = global_pos.y() - ph - 4
        if y < scr.top():
            y = global_pos.y() + 4
        popup.move(max(scr.left(), x), max(scr.top(), min(y, scr.bottom() - ph)))
        if popup.exec() == QDialog.DialogCode.Accepted:
            self.db.set_cause_comment(cause_id, txt.toPlainText().strip())
            self._schedule_rebuild()

    # ── Feature 4: clone scenario ─────────────────────────────────────────────
    def _open_pending_empty_cause_editor(self):
        deviation_id = self._empty_cause_click_target
        self._empty_cause_click_target = None
        if deviation_id is not None:
            self._quick_add_cause(deviation_id)

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
        new_cid = self.db.copy_cause_scoped(
            cause_id, target_dev['id'], include_descendants=True)
        if new_cid is not None:
            self.new_item_created.emit(CAUSE_T, new_cid)
            self._schedule_rebuild()

    # ── Enter-tangent: snabblägg-till ─────────────────────────────────────────

    def eventFilter(self, obj, event):
        # QApplication may deliver one final event while this panel is being
        # torn down.  In that window the table attribute can already be gone;
        # never let the global filter turn normal widget destruction into a
        # crash report.
        if getattr(self, '_table', None) is None:
            return False

        # The table deliberately uses NoEditTriggers and opens its inline
        # editors from explicit handlers.  Consequently a click outside the
        # table is not guaranteed to produce the delegate's normal
        # closeEditor sequence.  Handle that at application level so the
        # standard-cause helper cannot become stuck after repeated clicks.
        if (event.type() == QEvent.Type.MouseButtonPress and
                event.button() in (Qt.MouseButton.LeftButton,
                                   Qt.MouseButton.RightButton)):
            target = obj
            top_level = self.window()
            if (target is top_level or
                    self._is_descendant_of(target, top_level)):
                self._finish_inline_editor_for_external_click(target)

        ctrl = bool(event.type() == QEvent.Type.KeyPress and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # Right-clicking a painted group tag opens the group-row actions.
        # Blank cell space deliberately has no such shortcut.
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.RightButton):
            pos = event.pos()
            row = self._table.rowAt(pos.y())
            col = self._table.columnAt(pos.x())
            if row >= 0 and col == self._C_ORS:
                item = self._table.item(row, col)
                group_tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
                if len(group_tags) >= 2:
                    line_h = max(_ORS_FIRST_LINE_H,
                                 QFontMetrics(self._table.font()).height() + 4)
                    line_no = int((pos.y() - self._table.rowViewportPosition(row) - 2)
                                  // line_h)
                    if 0 <= line_no < len(group_tags):
                        x = self._table.columnViewportPosition(col) + 2
                        if line_no == 0:
                            num = item.data(Qt.ItemDataRole.UserRole + 10) or ''
                            if num:
                                x += QFontMetrics(self._table.font()).horizontalAdvance(
                                    f"{num}.  ")
                        else:
                            operators = self._group_operators(item)
                            operator = operators[line_no] if line_no < len(operators) else 'OR'
                            x += QFontMetrics(self._table.font()).horizontalAdvance(
                                f"{operator} ")
                        tag_font = QFont(self._table.font())
                        tag_font.setBold(True)
                        width = QFontMetrics(tag_font).horizontalAdvance(str(group_tags[line_no]))
                        if x <= pos.x() <= x + width:
                            cause_id = self._row_meta[row][1] if row < len(self._row_meta) else None
                            if cause_id is not None:
                                self._show_group_tag_menu(
                                    row, cause_id, line_no,
                                    self._table.viewport().mapToGlobal(pos))
                                return True

        if (obj in (self._table, self._table.viewport()) and
                event.type() == QEvent.Type.MouseButtonDblClick and
                event.button() == Qt.MouseButton.LeftButton):
            point = event.position().toPoint()
            if obj is self._table:
                point = self._table.viewport().mapFrom(self._table, point)
            row = self._table.rowAt(point.y())
            col = self._table.columnAt(point.x())
            if col in (self._C_ORS, self._C_KON, self._C_SG, self._C_REK):
                if col == self._C_ORS and 0 <= row < self._table.rowCount():
                    item = self._table.item(row, col)
                    group_tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
                    if len(group_tags) >= 2:
                        cell_rect = self._table.visualRect(
                            self._table.model().index(row, col))
                        line_h = max(_ORS_FIRST_LINE_H,
                                     QFontMetrics(self._table.font()).height() + 4)
                        rel_y = point.y() - cell_rect.top() - 2
                        line_no = int(rel_y // line_h) if rel_y >= 0 else -1
                        font_h = QFontMetrics(self._table.font()).height()
                        text_pad = max(0, (line_h - font_h) // 2)
                        line_top = line_no * line_h
                        in_text_band = (
                            0 <= line_no < len(group_tags) and
                            line_top + text_pad <= rel_y <
                            line_top + text_pad + font_h)
                        if not in_text_band:
                            # Consume the event before QTableWidget's native
                            # double-click editor can open a generic editor.
                            self._double_click_edit = None
                            return True
                self._double_click_edit = (row, col, point)
                # QTableWidget's itemDoubleClicked signal is not delivered
                # consistently once NoEditTriggers is in use (notably for a
                # populated KON cell on the viewport path). Keep the signal
                # route as a fallback, but explicitly continue to the same
                # inline-editor handler on the next event-loop turn. The
                # helper is a no-op if the normal signal route already
                # opened the editor.
                if col == self._C_KON and 0 <= row < len(self._row_meta):
                    cons_id = self._row_meta[row][2]
                    if cons_id is not None:
                        QTimer.singleShot(
                            0, lambda r=row, cid=cons_id:
                            self._ensure_consequence_double_click_editor(r, cid))

        # ── Drag: record press position for potential drag-start ─────────────────
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            row = self._table.rowAt(pos.y())
            col = self._table.columnAt(pos.x())
            # A drag in the scenario is always a copy.  Support the same
            # hierarchy levels that Ctrl+C/Ctrl+V supports; the receiver
            # decides the target parent from the row below the cursor.
            if row >= 0 and col in (self._C_NOD, self._C_DEV, self._C_ORS,
                                    self._C_KON, self._C_RFORE, self._C_SG,
                                    self._C_LOPA, self._C_SLUT, self._C_REK):
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

        # When a cause cell is being edited, its QLineEdit sits above the
        # table viewport and receives the native drop instead.  Accept the
        # same P&ID marker MIME there and route it through _handle_drop so the
        # marker's equipment_id is linked to the cause rather than silently
        # being treated as text.
        if (isinstance(obj, (_BoldTagTextEdit, QLineEdit)) and
                event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove,
                                 QEvent.Type.Drop) and
                event.mimeData().hasText() and
                event.mimeData().text().startswith('hzp:')):
            if event.type() != QEvent.Type.Drop:
                event.acceptProposedAction()
            else:
                self._handle_drop(event, source_obj=obj)
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
            col = self._table.columnAt(pos.x())
            row = self._table.rowAt(pos.y())

            # Object-tag zone click — the bold "TAG, " prefix at the start
            # of the cause cell's first line, pixel-exact to what paint()
            # actually rendered bold (2026-08-25, see NOTES.md "Slå ihop
            # objektbaren i Orsak-kolumnen" — replaces the old fixed-width
            # tag-strip zone with the real rendered prefix width via the
            # same _ors_tag_prefix_pixel_width() paint() and
            # updateEditorGeometry() also use, so all three can never
            # disagree about where the tag ends). No zone at all (0 width)
            # when there's no tag to show, or this row repeats the
            # previous one's tag (2026-08-18 dedup, unchanged).
            if (row >= 0 and col == self._C_ORS and row < len(self._row_meta) and
                    pos.y() - self._table.rowViewportPosition(row) <
                    self._table.rowHeight(row)):
                col_x      = self._table.columnViewportPosition(col)
                item       = self._table.item(row, col)
                desc       = item.text() if item is not None else ''
                # Older/partially rebuilt rows may carry an explicit None in
                # UserRole+9.  Treat it as an empty group, just like the
                # paint and geometry helpers do, so a click on an ordinary
                # cause cannot crash the event filter.
                group_tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
                if not group_tags:
                    prefix_w = self._ors_tag_prefix_pixel_width(
                        item, desc, self._table.font())
                    if prefix_w > 0 and col_x <= pos.x() < col_x + 2 + prefix_w:
                        cause_id = self._row_meta[row][1]
                        if cause_id is not None:
                            gp = self._table.viewport().mapToGlobal(pos)
                            self._show_cause_obj_popup(row, cause_id, gp)
                        return True
                elif len(group_tags) >= 2:
                    line_h = max(_ORS_FIRST_LINE_H,
                                 QFontMetrics(self._table.font()).height() + 4)
                    line_no = int((pos.y() - self._table.rowViewportPosition(row) - 2)
                                  // line_h)
                    if 0 <= line_no < len(group_tags):
                        tag_start = col_x + 2
                        if line_no == 0:
                            num = item.data(Qt.ItemDataRole.UserRole + 10) or ''
                            if num:
                                tag_start += QFontMetrics(self._table.font()).horizontalAdvance(
                                    f"{num}.  ")
                        elif line_no > 0:
                            operators = self._group_operators(item)
                            operator = (operators[line_no]
                                        if line_no < len(operators) else 'OR')
                            operator_width = QFontMetrics(self._table.font()).horizontalAdvance(
                                f"{operator} ")
                            if tag_start <= pos.x() < tag_start + operator_width:
                                cause_id = self._row_meta[row][1]
                                if cause_id is not None:
                                    gp = self._table.viewport().mapToGlobal(pos)
                                    self._choose_group_operator(row, cause_id, line_no, gp)
                                return True
                            tag_start += operator_width
                        tag_width = QFontMetrics(self._table.font()).horizontalAdvance(
                            str(group_tags[line_no]))
                        if tag_start <= pos.x() < tag_start + tag_width:
                            cause_id = self._row_meta[row][1]
                            if cause_id is not None:
                                gp = self._table.viewport().mapToGlobal(pos)
                                self._show_cause_obj_popup(
                                    row, cause_id, gp, group_line=line_no)
                            return True
                # The frequency zone below remains reachable for grouped
                # causes when the click was outside either object tag.

            # Frequency zone click — floats over the description's own
            # first line (2026-08-18, see NOTES.md "Frekvensen ... hör
            # hemma mer här" / follow-up "hamnar nu på olika rader"),
            # using the exact same freq_zone_x/freq_zone_w geometry
            # paint() draws the text with (2026-08-14, see NOTES.md:
            # "klickar man på frekvens skall man kunna justera
            # frekvens"). Restricted to that first line's own height so
            # it doesn't also swallow clicks on later description lines
            # below it — shares that Y-band with the tag zone above
            # (2026-08-25: both now live on the same first line, no more
            # separate strip/frequency-row split), disambiguated by X
            # since the tag sits left and frequency sits right-aligned.
            row_top = self._table.rowViewportPosition(row)
            if (row >= 0 and col == self._C_ORS and row < len(self._row_meta) and
                    pos.y() - row_top < _ORS_FIRST_LINE_H):
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

            # 💬 Comment dot click in ORS cell — the only icon here since the
            # 2026-08-18 fill-status "plupp" removal. Hit-tests the exact
            # same geometry paint() draws it at (_ors_comment_dot_geometry,
            # padded a few px for an easier click target), so the two can't
            # drift apart again. Only live when a comment already exists —
            # matches paint()'s own _has_comment gate, since the dot isn't
            # drawn otherwise; adding the FIRST comment goes through the
            # "Kommentar…" context-menu action instead. The old inline
            # "clone" zone here hit-tested blank space next to the dot, not
            # anything visible, and has been removed — cloning already has
            # a real, working, always-visible entry in the right-click
            # context menu ("Duplicera scenario till annan avvikelse…").
            if row >= 0 and col == self._C_ORS and row < len(self._row_meta):
                cause_id = self._row_meta[row][1]
                if cause_id is not None:
                    try:
                        has_comment = bool((self.db.get_cause_comment(cause_id) or '').strip())
                    except Exception:
                        has_comment = False
                    if has_comment:
                        ci = self._table.model().index(row, col)
                        cr = self._table.visualRect(ci)
                        dot = self._ors_comment_dot_geometry(cr).adjusted(-4, -4, 4, 4)
                        if dot.contains(pos):
                            self._open_comment_popup(row, cause_id,
                                                      self._table.viewport().mapToGlobal(pos))
                            return True

            # ⚡ RRF badge click — right _RRF_W pixels of safeguard cell
            if (row >= 0 and col == self._C_SG and row < len(self._row_meta)):
                sg_id = self._row_meta[row][3]
                if sg_id is not None:
                    cell_idx = self._table.model().index(row, col)
                    cr = self._table.visualRect(cell_idx)
                    zone = self._sg_rrf_zone_geometry(cr)
                    if pos.x() >= zone.left():
                        gp = self._table.viewport().mapToGlobal(pos)
                        self._show_rrf_popup_at(row, sg_id, gp)
                        return True

            # Bold P&ID references embedded in KON, SG and REK free text
            # have a much narrower hit target than the whole cell.  This
            # deliberately runs after the RRF badge (and the ORS-specific
            # frequency/comment/tag zones above), so it cannot steal any of
            # the established actions from their visible controls.
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG, self._C_REK):
                context = self._text_tag_click_context(row, col, pos)
                if context:
                    self._show_object_tag_popup(
                        context, self._table.viewport().mapToGlobal(pos))
                    return True

        # Delegate inline editor (regular cell in edit mode)
        if (isinstance(obj, (_BoldTagTextEdit, QLineEdit)) and
                obj.property('editing_row') is not None and
                obj.property('sg_id') is None):
            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape:
                    self._cancel_inline_editor(obj)
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if (not ctrl and isinstance(obj, _BoldTagTextEdit) and
                            obj._accept_visible_tag_completion()):
                        return True
                    row = obj.property('editing_row')
                    col = obj.property('editing_col')
                    cons_id = None
                    if row is not None and 0 <= row < len(self._row_meta):
                        cons_id = self._row_meta[row][2]
                    if col == self._C_REK and cons_id is not None:
                        rec_id = (self._row_recommendation_ids[row]
                                  if row < len(self._row_recommendation_ids)
                                  else None)
                        self._recommendation_selection_after_commit = {
                            'cons_id': cons_id,
                            'rec_id': rec_id,
                            'force_add': (
                                self._recommendation_force_add_cons_id == cons_id),
                        }
                    # ORS/KON/SG have their own PID-aware delegate.  In
                    # particular, its grouped-cause path merges an edited
                    # secondary line back into the complete description.
                    # Sending Enter through the generic delegate replaced the
                    # whole group with that one line, so the saved cause could
                    # disappear until a later rebuild.
                    active_delegate = (
                        self._pid_delegate
                        if col in (self._C_ORS, self._C_KON, self._C_SG)
                        else self._delegate)
                    active_delegate.commitData.emit(obj)
                    active_delegate.closeEditor.emit(
                        obj, QStyledItemDelegate.EndEditHint.NoHint)
                    if col == self._C_REK and cons_id is not None:
                        # _refresh_recommendation_cell has already queued the
                        # rebuild during commitData. Queue this second so the
                        # resulting static cell remains selected after Enter.
                        QTimer.singleShot(0, self._restore_recommendation_selection)
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
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier and
                    event.modifiers() & Qt.KeyboardModifier.AltModifier):
                self._copy_row_to_clipboard(self._table.currentRow(),
                                            self._table.currentColumn())
                return True
            if (event.key() == Qt.Key.Key_C and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                # Keep the normal rich Office representation, but add the
                # same internal entity payload used by drag-and-drop. Ctrl+V
                # can therefore paste within HAZOP and ask the identical
                # cell-only/full-branch question, while Word/Excel still get
                # the exact selected cell rectangle as HTML/TSV.
                self._copy_selection_to_clipboard(
                    self._table.currentRow(), self._table.currentColumn(),
                    self._office_clipboard_title)
                return True
            if (event.key() == Qt.Key.Key_V and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._paste_from_clipboard(self._table.currentRow(),
                                           self._table.currentColumn())
                return True
            if event.key() == Qt.Key.Key_Delete:
                self._delete_current_item()
                return True
            # Do not let QTableWidget's built-in type-ahead search move the
            # selection from a blank cell to the first matching row in the
            # same column.  For an editable blank cell, open its inline
            # editor and place the typed character there.  Non-editable blank
            # cells simply consume the key.
            modifiers = event.modifiers()
            typed = event.text()
            if (typed and typed.isprintable() and not
                    modifiers & (Qt.KeyboardModifier.ControlModifier |
                                 Qt.KeyboardModifier.AltModifier |
                                 Qt.KeyboardModifier.MetaModifier)):
                row = self._table.currentRow()
                col = self._table.currentColumn()
                item = (self._table.item(row, col)
                        if row >= 0 and col >= 0 else None)
                if item is None or not item.text().strip():
                    if (item is not None and
                            item.data(Qt.ItemDataRole.UserRole) and
                            bool(item.flags() & Qt.ItemFlag.ItemIsEditable)):
                        opened = self._try_start_edit(row, col)
                        if opened:
                            QTimer.singleShot(
                                0, lambda r=row, c=col, value=typed:
                                self._insert_first_typed_character(r, c, value))
                    return True
            # F2 starts an inline edit on the selected editable cell.
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
                self._quick_add_cause(dev_id, after_cause_id=cause_id)
        elif col in (self._C_KON, self._C_RFORE):
            if cause_id is not None:
                self._quick_add_consequence(cause_id)
        elif col == self._C_REK:
            # Recommendation is visually a sibling list below the same
            # consequence, just as safeguards are.  Its persistence differs
            # (a link to the shared recommendation catalogue), so it needs
            # its own continuation path: Enter must never create a safeguard
            # merely because the active cell happened to be in REK.
            if cons_id is not None:
                self._continue_recommendation_entry(row, cons_id)
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

    def _continue_recommendation_entry(self, row, cons_id):
        """Show and open the next blank recommendation row after Enter."""
        # Build the physical row before starting the editor so the new line
        # is visible while the user types.
        self._recommendation_add_placeholder_cons_id = cons_id
        self._rebuild()
        row = next((r for r, meta in enumerate(self._row_meta)
                    if meta[2] == cons_id and
                    (r >= len(self._row_recommendation_ids) or
                     self._row_recommendation_ids[r] is None) and
                    self._table.rowSpan(r, self._C_REK) == 1), -1)
        if row < 0:
            # A zero/one-recommendation REK cell spans the whole consequence
            # block. Reuse its anchor in add mode; rebuild then materialises
            # the next physical recommendation row.
            row = next((r for r, meta in enumerate(self._row_meta)
                        if meta[2] == cons_id), -1)
        if row < 0:
            return
        self._recommendation_force_add_cons_id = cons_id
        self._table.setCurrentCell(row, self._C_REK)
        item = self._table.item(row, self._C_REK)
        if item is not None:
            self._table.scrollToItem(item)
        self._try_start_edit(row, self._C_REK)

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

    def _quick_add_cause(self, deviation_id, after_cause_id=None):
        """Create a blank cause and enter the shared inline editor.

        An empty ORS cell, the in-cell plus affordance, the context menu and
        Enter all use this one path. The retired combined ``CauseObjectPopup``
        ("Orsak på P&ID") is not reachable here: users can type or select a
        catalogue tag in the inline editor, which then enables the normal
        standard-cause suggestions.
        """
        # Creating a blank cause currently normalises the inserted row with a
        # second write.  It is still one user action and must undo as one.
        with self.db.history_group():
            new_id = self.db.add_cause_after(deviation_id, after_cause_id)
            self.db.update_cause(new_id, description='', comp_type='', comp_tag='',
                                 likelihood=0, base_frequency=None)
        self.new_item_created.emit(CAUSE_T, new_id)

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

    # Kept as private compatibility helpers for existing programmatic entry
    # points. The in-cell plus badges and their mouse hit zones are removed.
    def _add_cause_via_plus_row(self, deviation_id, global_pos=None):
        self._quick_add_cause(deviation_id)

    def _add_consequence_via_plus_row(self, cause_id):
        self._quick_add_consequence(cause_id)

    def _add_safeguard_via_plus_row(self, cons_id):
        self._quick_add_safeguard(cons_id)

    def _keep_item_visible(self, item):
        """Scroll only when the target is genuinely outside the viewport."""
        if item is None:
            return
        rect = self._table.visualItemRect(item)
        viewport = self._table.viewport().rect()
        if rect.isEmpty() or not viewport.intersects(rect):
            self._table.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter)

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
                self._keep_item_visible(item)
                self._try_start_edit(row, col)  # KON supported too since 2026-08-07 — see NOTES.md
                return

    def undo_last_text_edit(self):
        """Compatibility entry point for the central database history.

        Older callers still use this method name, but the old per-field stack
        could only restore a description and created a second history entry
        while doing so. Delegate to the same atomic session history as the
        main-window Ctrl+Z action.
        """
        return bool(getattr(self.db, 'undo', lambda: False)())

    def _on_cell_changed(self, row, col):
        # A single cell save may update the visible row, tag identity,
        # frequency metadata and completion history. Treat it as one user
        # action even though legacy Database methods commit separately.
        with self.db.history_group():
            try:
                self._on_cell_changed_inner(row, col)
            except Exception as e:
                QMessageBox.critical(self, "Fel vid celländring (scenario)", str(e))

    _INLINE_TAG_RE = re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z]{1,10}[-_][A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)(?![A-Za-z0-9])")

    def _matching_pid_tags(self, text):
        """Return catalogue tags occurring as complete tokens in *text*.

        This is deliberately based on the current P&ID catalogue rather than
        on tag-shaped text alone: ordinary prose must not become bold merely
        because it contains a hyphen.
        """
        if not text:
            return []
        try:
            catalogue = []
            for row in self.db.equipment_items():
                try:
                    tag = row['tag']
                except (KeyError, TypeError, IndexError):
                    tag = row.get('tag') if hasattr(row, 'get') else ''
                tag = str(tag or '').strip()
                if tag:
                    catalogue.append(tag)
        except Exception:
            return []
        found = []
        for tag in sorted({t for t in catalogue if t}, key=len, reverse=True):
            if tag.casefold() in self._detached_tags:
                continue
            if re.search(r'(?<![A-Za-z0-9])' + re.escape(tag) +
                         r'(?![A-Za-z0-9])', str(text), re.IGNORECASE):
                found.append(tag)
        return found

    @staticmethod
    def _active_tag_refs_in_text(refs, text):
        """Discard historical tag references no longer present in a cell."""
        current = str(text or '')
        return [tag for tag in refs or [] if re.search(
            rf'(?<![A-Za-z0-9]){re.escape(str(tag).strip())}(?![A-Za-z0-9])',
            current, re.IGNORECASE)]

    def _recognized_pid_equipment(self, text):
        """Return the first real catalogue object mentioned in *text*."""
        text = str(text or '')
        matches = []
        for tag in self._matching_pid_tags(text):
            position = text.casefold().find(tag.casefold())
            equipment = self.db.get_equipment_by_tag(tag)
            if position >= 0 and equipment:
                matches.append((position, -len(tag), equipment))
        selected = min(matches, key=lambda candidate: candidate[:2], default=None)
        return selected[2] if selected else None

    def _adopt_deviation_equipment(self, deviation_id, equipment_id):
        """Give an unbound deviation the first selected cause object's context."""
        if deviation_id is None or equipment_id is None:
            return
        deviation = self.db.get_deviation(deviation_id)
        if deviation and deviation.get('equipment_id') is None:
            self.db.set_deviation_equipment(deviation_id, equipment_id)

    def _bind_recognized_cause_equipment(self, cause_id, equipment_id):
        """Persist a recognised object only for an unlinked ordinary cause."""
        cause = self.db.get_cause(cause_id)
        equipment = self.db.get_equipment_by_id(equipment_id)
        if not cause or not equipment:
            return False
        existing_id = cause.get('equipment_id')
        if existing_id not in (None, equipment_id):
            return False
        self.db.update_cause(
            cause_id, comp_type=equipment.get('equipment_type') or '',
            comp_tag=equipment.get('tag') or '', equipment_id=equipment_id)
        self._adopt_deviation_equipment(cause.get('deviation_id'), equipment_id)
        return True

    def _strip_leading_recognized_tag(self, text, equipment):
        """Avoid drawing a newly bound ORS tag twice after an inline save."""
        tag = (equipment or {}).get('tag') or ''
        match = re.match(r'\s*' + re.escape(tag) + r'(?=\s|[,;:]|$)',
                         str(text or ''), re.IGNORECASE)
        if not match:
            return str(text or '').strip()
        return str(text or '')[match.end():].lstrip(' ,:;-').strip()

    def _confirm_inline_identity_change(self, kind, id_, desc):
        """Guard a tag replacement embedded in a KON/SG description.

        Returns ``(accepted, description)``.  The identity columns are flat
        for KON/SG, but the same P&ID object semantics still apply: connect to
        an exact catalog match, or explicitly rename the old catalog object.
        """
        row = self.db.get_consequence(id_) if kind == 'consequence' else self.db.get_safeguard(id_)
        if not row:
            return True, desc
        # Database queries return sqlite3.Row objects, which deliberately do
        # not implement dict.get().  Convert once here because the identity
        # flow uses optional fields throughout the confirmation logic.
        row = dict(row)
        old_tag = (row.get('comp_tag') or '').strip()
        if not old_tag or old_tag.casefold() in desc.casefold():
            return True, desc
        candidates = [m.group(1) for m in self._INLINE_TAG_RE.finditer(desc)]
        new_tag = next((tag for tag in candidates
                        if tag.casefold() != old_tag.casefold()), None)
        if not new_tag:
            return True, desc

        old_eq = self.db.get_equipment_by_tag(old_tag)
        match = self.db.get_equipment_by_tag(new_tag)
        decision = self._confirm_equipment_tag_change(
            old_tag, new_tag, match, linked=bool(old_eq))
        if decision == 'cancel':
            return False, desc

        comp_type = row.get('comp_type') or ''
        if decision == 'connect' and match:
            comp_type = match.get('equipment_type') or comp_type
        elif decision == 'rename' and old_eq:
            duplicate = match if match and match.get('id') != old_eq.get('id') else None
            if duplicate:
                QMessageBox.warning(
                    self, "Taggen finns redan",
                    f"Taggen {new_tag} används redan av ett annat objekt på denna P&ID. "
                    "Välj Koppla för att använda det objektet.")
                return False, desc
            self.db.update_equipment_item(
                old_eq['id'], new_tag, old_eq.get('prefix') or '',
                old_eq.get('equipment_type') or comp_type,
                old_eq.get('description') or '')
            self.equipment_renamed.emit()

        refs = parse_tag_refs(row.get('tagged_refs') or '')
        refs = [new_tag if ref.casefold() == old_tag.casefold() else ref for ref in refs]
        if not any(ref.casefold() == new_tag.casefold() for ref in refs):
            refs.append(new_tag)
        if kind == 'consequence':
            self.db.update_consequence(
                id_, row.get('description') or '', row['severity'], row.get('category') or '',
                row.get('consequence_chain') or '', comp_tag=new_tag,
                comp_type=comp_type, tagged_refs=','.join(refs))
        else:
            self.db.update_safeguard(
                id_, tagged_refs=','.join(refs))
            self.db.set_safeguard_tag(id_, new_tag, comp_type)
        return True, desc

    def _on_cell_changed_inner(self, row, col):
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        kind, id_, *meta_extra = meta
        text = item.text().strip()

        if kind == 'cause':
            group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or []
            # Group causes are edited as one two-line text block.  Ordinary
            # causes keep the historic first-line behavior because their
            # first line is the editable description and later lines are
            # presentation metadata.
            desc = text if len(group_tags) >= 2 else text.split('\n')[0].strip()
            cause = self.db.get_cause(id_)
            if cause:
                recognised_equipment = (
                    self._recognized_pid_equipment(desc)
                    if len(group_tags) < 2 else None)
                bound_from_inline_tag = False
                stripped_desc = (self._strip_leading_recognized_tag(
                    desc, recognised_equipment) if recognised_equipment else desc)
                if (recognised_equipment and stripped_desc != desc and
                        cause.get('equipment_id') is None):
                    bound_from_inline_tag = self._bind_recognized_cause_equipment(
                        id_, recognised_equipment['id'])
                    if bound_from_inline_tag:
                        desc = stripped_desc
                        cause = self.db.get_cause(id_)
                # Check if the text is comp_tag (component tag) or description
                old_comp_tag = cause.get('comp_tag', '') or ''

                # If the edited text matches old_comp_tag, we're editing comp_tag
                # Otherwise, we're editing description
                if desc and old_comp_tag and old_comp_tag.strip() == text:
                    # User edited comp_tag
                    self.db.update_cause(id_, comp_tag=desc)
                else:
                    # User edited description
                    if not desc:
                        # An empty cause is also an empty frequency-bearing
                        # row.  A standard cause or a manually entered base
                        # frequency otherwise survives the text edit and is
                        # easy to miss in the compact table; the worksheet
                        # exporter then correctly exposes that stale value.
                        # Mark it cleared explicitly so both the UI and all
                        # exports use the same persisted state.
                        self.db.update_cause(
                            id_, desc, likelihood=0, base_frequency=None,
                            standard_cause_id=None, frequency_cleared=True)
                    else:
                        self.db.update_cause(id_, desc)
                    # Sync any OTHER row showing this same cause (span groups
                    # merge same-id rows visually, but each still has its own
                    # QTableWidgetItem) — no full rebuild needed, see
                    # _update_row_text_only()'s docstring for why.
                    if len(group_tags) >= 2:
                        # A grouped inline editor writes one physical line
                        # into a shared multi-line description. Refresh the
                        # group immediately after either Enter or focus-out;
                        # the ordinary text fast path leaves its visual row
                        # metadata stale until another table action occurs.
                        self._group_cause_changed(id_)
                    else:
                        self._update_row_text_only('cause', id_, desc)
                if bound_from_inline_tag:
                    self._schedule_rebuild()
            if len(group_tags) < 2:
                self.item_edited.emit(CAUSE_T, id_)

        elif kind == 'consequence':
            desc = text.split('\n')[0].strip()
            cons = self.db.get_consequence(id_)
            if cons:
                accepted, desc = self._confirm_inline_identity_change('consequence', id_, desc)
                if not accepted:
                    self._schedule_rebuild()
                    return
                # A text edit is authoritative: old tagged_refs must not
                # keep removed objects bold or connected to this cell.
                refs = self._matching_pid_tags(desc)
                active_tag = refs[-1] if refs else ''
                active_eq = (self.db.get_equipment_by_tag(active_tag)
                             if active_tag else None)
                self.db.update_consequence(id_, desc, cons['severity'],
                                           cons['category'] or '',
                                           comp_tag=active_tag,
                                           comp_type=(active_eq.get('equipment_type')
                                                      if active_eq else ''),
                                           tagged_refs=','.join(refs))
                item.setData(Qt.ItemDataRole.UserRole + 8, refs)
                self._update_row_text_only('consequence', id_, desc)
                # "Spara varje konsekvens som skrivs i HAZOP Scenario i en
                # databas" (2026-08-26, see NOTES.md "Återanvänd tidigare
                # konsekvenser") — feeds the completer _attach_consequence_
                # completer attaches to this same cell's editor.
                self.db.add_consequence_history(desc)
            self.item_edited.emit(CONS_T, id_)

        elif kind == 'safeguard':
            # No 'Ny safeguard' fallback (2026-08-12, see NOTES.md) —
            # clearing the text back to empty must actually save empty
            # (displayed as "—"), not silently resurrect placeholder text.
            edit_val = item.data(Qt.ItemDataRole.EditRole)
            desc = str(edit_val).strip() if edit_val is not None else text.split('\n')[0].strip()
            sg = self.db.get_safeguard(id_)
            if sg:
                accepted, desc = self._confirm_inline_identity_change('safeguard', id_, desc)
                if not accepted:
                    self._schedule_rebuild()
                    return
                # Keep only catalogue tags still present in the edited cell.
                refs = self._matching_pid_tags(desc)
                active_tag = refs[-1] if refs else ''
                active_eq = (self.db.get_equipment_by_tag(active_tag)
                             if active_tag else None)
                self.db.update_safeguard(id_, desc, sg['rrf'] or 1,
                                         tagged_refs=','.join(refs))
                self.db.set_safeguard_tag(
                    id_, active_tag,
                    active_eq.get('equipment_type') if active_eq else '')
                item.setData(Qt.ItemDataRole.UserRole + 7, refs)
                # A safeguard's description never affects its own row's RRF/
                # risk-derived columns (those depend on rrf, not text) or any
                # other row, so a full _rebuild() was pure overhead here —
                # patch the text in place instead (see _update_row_text_only).
                self._update_row_text_only('safeguard', id_, desc)
            self.item_edited.emit(SG_T, id_)

        elif kind == 'recommendation':
            # id_ here is the CONSEQUENCE id (see the ('recommendation', cid)
            # UserRole payload _add_row sets on this item), not a
            # recommendation id — a consequence can have 0..N linked
            # recommendations, so there's no single row id to key off.
            # Same 0/1/N-linked rule as _prepare_recommendation_editor's
            # own docstring (scenario_panel.py, above):
            #   1 linked  -> update it in place (through the shared-
            #                recommendation prompt), but ONLY if the text
            #                actually changed -- committing an untouched
            #                cell (click in, click out) must never pop
            #                that confirmation for no reason.
            #   0 or 2+   -> non-blank text becomes an ADDITIONAL new
            #                recommendation; existing ones are untouched.
            # A recommendation cell may have been produced by an older Qt
            # editor path that wrote a complete QTextEdit HTML document into
            # the item.  Clean before selecting the first line so that such
            # a cell cannot be interpreted as an empty recommendation.
            desc = Database._clean_recommendation_text(text).split('\n')[0].strip()
            acts = self.db.recommendations_for_consequence(id_)
            rec_id = meta_extra[0] if meta_extra else None
            force_add = self._recommendation_force_add_cons_id == id_
            self._recommendation_force_add_cons_id = None
            if force_add:
                # The visible blank entry row is only an editing affordance;
                # remove it once this Enter commit has produced the real row.
                self._recommendation_add_placeholder_cons_id = None
            selected_rec_id = rec_id
            if rec_id is not None and force_add and desc:
                selected_rec_id = self.db.add_recommendation_to_consequence(
                    id_, description=desc)
                desc = ''
            if rec_id is not None and desc:
                rec = next((a for a in acts if a['id'] == rec_id), None)
                if rec and desc == (rec['description'] or '').strip():
                    desc = ''
            if rec_id is not None and desc:
                from hazop import _apply_shared_recommendation_description_update
                _apply_shared_recommendation_description_update(
                    self.db, self, rec_id, id_, desc)
            elif desc:
                selected_rec_id = self.db.add_recommendation_to_consequence(
                    id_, description=desc)
            pending_selection = self._recommendation_selection_after_commit
            if pending_selection and pending_selection.get('cons_id') == id_:
                pending_selection['rec_id'] = selected_rec_id
            self._refresh_recommendation_cell(id_)
            self.item_edited.emit(CONS_T, id_)

        if (row, col) == (self._enter_row, self._enter_col):
            self._last_enter_committed = True

    # ── Feature 7: try start inline edit ──────────────────────────────────────
    def _place_editor_caret(self, row, col, viewport_pos):
        """Place the caret from the actual double-click, never select all."""
        for editor in (self._table.findChildren(_BoldTagTextEdit) +
                       self._table.findChildren(QLineEdit)):
            if (editor.property('editing_row') == row and
                    editor.property('editing_col') == col):
                local = QPoint(viewport_pos.x() - editor.geometry().x(),
                               viewport_pos.y() - editor.geometry().y())
                try:
                    if isinstance(editor, _BoldTagTextEdit):
                        local = editor.viewport().mapFrom(editor, local)
                    position = editor.cursorPositionAt(local)
                    if (isinstance(editor, _BoldTagTextEdit) and position == 0
                            and local.x() > 4 and editor.toPlainText()):
                        # A not-yet-laid-out offscreen QTextEdit can report
                        # position zero for a point inside its text area.
                        # Keep the click intent by using the font's average
                        # advance as a conservative fallback.
                        fm = QFontMetrics(editor.font())
                        position = min(
                            len(editor.toPlainText()),
                            max(1, round((local.x() - 4) /
                                         max(1, fm.averageCharWidth()))))
                except (AttributeError, TypeError):
                    position = len(editor.text())
                editor.setFocus(Qt.FocusReason.MouseFocusReason)
                editor.deselect()
                editor.setCursorPosition(max(0, min(position, len(editor.text()))))
                return

    def _try_start_edit(self, row, col):
        # _C_KON added 2026-08-07 (see NOTES.md "Klicka direkt på
        # konsekvens") — the commit path (_on_cell_changed_inner's
        # 'consequence' branch) already existed and worked; only the trigger
        # was missing. All consequence editing now uses this inline route.
        # _C_REK added 2026-08-26 (see NOTES.md "Redigera
        # rekommendationer direkt i HAZOP Scenario").
        if row < 0 or col not in (self._C_ORS, self._C_SG, self._C_KON, self._C_REK):
            return False
        item = self._table.item(row, col)
        # A grouped cause has two distinct visual edit targets. Never open
        # the generic full-cell editor from F2, a context-menu action, or a
        # programmatic selection because it has no row context.
        if col == self._C_ORS and item is not None:
            group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or []
            if len(group_tags) >= 2:
                return False
        if item and bool(item.flags() & Qt.ItemFlag.ItemIsEditable):
            self._table.setFocus()
            return bool(self._table.edit(self._table.model().index(row, col)))
        return False

    def _insert_first_typed_character(self, row, col, text):
        """Insert a character consumed from a blank table cell.

        Qt opens the delegate editor asynchronously.  This deferred hand-off
        prevents the character from being interpreted as table type-ahead
        while still making a blank editable cell feel natural.  It is limited
        to the matching row and column, so a stale group editor can never
        receive the character.
        """
        for editor in self._inline_editor_widgets():
            if (editor.property('editing_row') != row or
                    editor.property('editing_col') != col):
                continue
            try:
                editor.selectAll()
                if isinstance(editor, _BoldTagTextEdit):
                    editor.insertPlainText(text)
                elif isinstance(editor, QLineEdit):
                    editor.insert(text)
            except RuntimeError:
                pass
            return

    # ── Copy and paste ───────────────────────────────────────────────────────
    # Kept as an application MIME rather than serialising database objects into
    # text. It travels alongside the normal Office HTML/TSV data on Ctrl+C,
    # so the same clipboard supports both Word/Excel and scoped in-HAZOP paste.
    _COPY_MIME = 'application/x-hazop-copy-items'

    @staticmethod
    def _clipboard_brush_css(brush):
        """Return a CSS colour for an explicitly painted table brush."""
        if brush is None or brush.style() == Qt.BrushStyle.NoBrush:
            return ''
        color = brush.color()
        if not color.isValid():
            return ''
        if color.alpha() < 255:
            return (f'rgba({color.red()},{color.green()},{color.blue()},'
                    f'{color.alpha() / 255:.3f})')
        return color.name()

    @staticmethod
    def _clipboard_rich_text(text, tags=()):
        """Escape cell text while retaining the visible bold tag identity."""
        text = str(text or '')
        clean_tags = sorted({str(tag).strip() for tag in tags if str(tag).strip()},
                            key=len, reverse=True)
        if not clean_tags:
            return _html_escape(text).replace('\n', '<br>')
        # A tag is a token, not an arbitrary substring (e.g. FV-1 must not
        # bold the FV-1 part of FV-10). Hyphen and underscore are accepted
        # inside the tag but not immediately outside its match.
        pattern = re.compile(
            r'(?<![A-Za-z0-9_-])(' + '|'.join(re.escape(tag) for tag in clean_tags) +
            r')(?![A-Za-z0-9_-])', re.IGNORECASE)
        result = []
        at = 0
        for match in pattern.finditer(text):
            result.append(_html_escape(text[at:match.start()]))
            result.append(f'<strong>{_html_escape(match.group(0))}</strong>')
            at = match.end()
        result.append(_html_escape(text[at:]))
        return ''.join(result).replace('\n', '<br>')

    def _clipboard_cell_content(self, row, col):
        """Return all visible text and tag identities for a rendered cell.

        Some of the compact worksheet information is painted by the delegate
        instead of living in ``QTableWidgetItem.text()``: the cause frequency
        chip and the safeguard RRF badge are the two important examples.
        Office copy must materialise those values, otherwise a copied table
        loses information compared with the Excel worksheet export.  More
        importantly, object values are read from their persisted entity here
        rather than from the item's rendering roles.  A role can briefly be
        stale after an object/RRF change while the database is already the
        source of truth.
        """
        table = self._table
        item = table.item(row, col)
        text = item.text() if item is not None else ''
        tags = []

        meta = (self._row_meta[row]
                if 0 <= row < len(self._row_meta) else (None, None, None, None))
        dev_id, cause_id, cons_id, sg_id = meta

        if item is not None and col == self._C_UTR:
            # This column is normally hidden in Worksheet but visible in
            # HAZOP Scenario. Resolve its deviation-owned equipment link
            # live so it follows the same no-stale-object rule as ORS/KON/SG.
            deviation = self.db.get_deviation(dev_id) if dev_id else None
            equipment = (self.db.get_equipment_by_id(deviation.get('equipment_id'))
                         if deviation and deviation.get('equipment_id') else None)
            if equipment:
                text = (f"{equipment.get('tag') or ''} — "
                        f"{equipment.get('equipment_type') or ''}").strip(' —')
        elif item is not None and col == self._C_ORS:
            cause = dict(self.db.get_cause(cause_id)) if cause_id else {}
            group_ids = self._group_equipment_ids(cause) if cause else []
            number = item.data(Qt.ItemDataRole.UserRole + 10)
            if len(group_ids) >= 2:
                group_tags = []
                for equipment_id in group_ids:
                    equipment = self.db.get_equipment_by_id(equipment_id)
                    tag = str((equipment or {}).get('tag') or '').strip()
                    if tag:
                        group_tags.append(tag)
                # Keep legacy groups readable too, but only when a current
                # equipment link did not provide the live tag list.
                if len(group_tags) < 2:
                    group_tags = list(item.data(Qt.ItemDataRole.UserRole + 9) or [])
                tags = list(group_tags)
                lines = (self.db.group_cause_description_lines(cause, group_ids)
                         if cause else str(text or '').splitlines())
                operators = self._group_operators(item)
                rebuilt = []
                for idx, tag in enumerate(tags):
                    prefix = f'{number}.  {tag}' if idx == 0 and number else str(tag)
                    if idx:
                        op = operators[idx] if idx < len(operators) else 'OR'
                        prefix = f'{op} {prefix}'
                    rebuilt.append(f'{prefix} {lines[idx] if idx < len(lines) else ""}'.rstrip())
                text = '\n'.join(rebuilt)
            else:
                _type, tag = self._cause_tag_display(cause) if cause else ('', '')
                description = str(cause.get('description') or text or '').strip()
                if tag:
                    tags = [tag]
                    text = tag if not description else f'{tag}, {description}'
                else:
                    text = description
                if number and text:
                    text = f'{number}.  {text}'.rstrip()
            frequency = (self.db.cause_frequency_level(cause)
                         if cause and item.data(Qt.ItemDataRole.UserRole + 3) is not None
                         else None)
            if frequency is not None and text:
                text = f'{text}\n{self._ors_freq_label(frequency, cause.get("base_frequency"))}'.strip()
        elif item is not None and col == self._C_KON:
            consequence = dict(self.db.get_consequence(cons_id)) if cons_id else {}
            text = str(consequence.get('description') or text or '')
            number = item.data(Qt.ItemDataRole.UserRole + 10)
            if number and text:
                text = f'{number}.  {text}'.rstrip()
            refs = parse_tag_refs(consequence.get('tagged_refs') or '')
            tags = (self._active_tag_refs_in_text(refs, text) +
                    self._matching_pid_tags(text))
        elif item is not None and col == self._C_SG:
            safeguard = dict(self.db.get_safeguard(sg_id)) if sg_id else {}
            text = str(safeguard.get('description') or text or '')
            refs = parse_tag_refs(safeguard.get('tagged_refs') or '')
            tags = (self._active_tag_refs_in_text(refs, text) +
                    self._matching_pid_tags(text))
            number = item.data(Qt.ItemDataRole.UserRole + 10)
            if number and text:
                text = f'{number}. {text}'.rstrip()
            rrf = safeguard.get('rrf') if safeguard else item.data(Qt.ItemDataRole.UserRole + 1)
            if rrf is not None and text:
                text = f'{text} (RRF: {rrf})'

        if col == self._C_LOPA and cons_id:
            active_factors = [dict(factor) for factor in self.db.reduction_factors(cons_id)
                              if factor['active']]
            total_rrf = 1.0
            for factor in active_factors:
                try:
                    total_rrf *= max(1.0, float(factor.get('rrf') or 1))
                except (TypeError, ValueError):
                    continue
            text = (f'{len(active_factors)} ({_LopaWidget._format_rrf(total_rrf)})'
                    if active_factors else '')
        elif item is not None and col == self._C_REK:
            recommendation_id = (self._row_recommendation_ids[row]
                                 if row < len(self._row_recommendation_ids) else None)
            recommendation = (self.db.get_recommendation(recommendation_id)
                              if recommendation_id else None)
            if recommendation:
                description = (recommendation.get('description') or '').strip()
                text = (f"{int(recommendation['display_number']):03d}. {description}"
                        if description else '')
            tags = self._matching_pid_tags(text)

        widget = table.cellWidget(row, col)
        # Enabler data was just calculated from the consequence's persisted
        # factors above. Do not overwrite it with a potentially stale button
        # label from the old widget instance.
        if widget is not None and col != self._C_LOPA:
            # The enabler widget has one explicit summary button.  Prefer it
            # to generic child enumeration so internal/hidden controls never
            # leak into an Office export.
            summary = widget.findChild(QPushButton, 'enablerSummaryButton')
            labels = ([summary.text().strip()]
                      if summary is not None and summary.text().strip() else [])
            if not labels:
                labels = [button.text().strip() for button in widget.findChildren(QPushButton)
                          if button.isVisible() and button.text().strip()]
            if not labels:
                labels = [label.text().strip() for label in widget.findChildren(QLabel)
                          if label.isVisible() and label.text().strip()]
            if labels:
                text = '\n'.join(labels)
        return str(text or ''), tags

    def _worksheet_office_reference_payload(self):
        """Build the complete Office copy from the canonical worksheet rows.

        ``worksheet_export._worksheet_rows`` is also used by the saved Excel
        export and already contains the correct shared grid for consequence
        categories, safeguards, enablers and recommendations.  Reusing that
        row motor is important: the Qt table deliberately packs frequency and
        RRF into visual badges, while the worksheet reference has them as
        separate columns.

        This path is only used for an unfiltered complete worksheet.  A
        smaller selection, an equipment filter or a single consequence keeps
        the existing exact-selection clipboard path below.
        """
        if (self._equipment_filter_id is not None or self._cons_id is not None):
            return None
        try:
            from worksheet_export import _worksheet_rows
            rows = list(_worksheet_rows(self.db))
        except Exception:
            logging.exception('Worksheet Office-copy row generation failed')
            return None

        if not self._all_nodes and self._node_id is not None:
            rows = [row for row in rows
                    if row.get('merge_key', (None,))[0] == self._node_id]
        if not rows:
            return '', ''

        headers = [
            'Nod', 'Avvikelse', 'Orsak', 'Frekvens', 'Konsekvens',
            'Riskklass före barriärer', 'Barriär', 'RRF', 'Enablers',
            'Riskklass efter barriärer', 'Rekommendation',
        ]
        # These proportions mirror worksheet_export.py and the Sheet 3
        # reference: text columns are wide, axis/RRF columns are narrow.
        widths = [120, 160, 300, 70, 430, 155, 370, 55, 110, 155, 330]

        def _colour(value, fallback):
            value = str(value or fallback).strip()
            return value if value.startswith('#') else f'#{value.lstrip("#")}'

        def _row_height(values):
            max_lines = 1
            for value, width in zip(values, widths):
                for line in str(value or '').splitlines() or ['']:
                    max_lines = max(max_lines,
                                    1 + len(line) // max(12, width // 7))
            return min(180, max(24, 15 * max_lines + 8))

        # Use the canonical row motor's stable per-column identities.  This
        # gives Excel/Word real vertical merges for hierarchy cells, risk
        # category cells and safeguards, while keeping separate barriers and
        # categories independent when their database IDs differ.
        html_rows = []
        tsv_rows = []
        canonical_keys = [tuple(row.get('office_merge_keys') or ())
                          for row in rows]

        def _canonical_span(row_index, column):
            key = (canonical_keys[row_index][column]
                   if column < len(canonical_keys[row_index]) else None)
            value = (rows[row_index].get('values') or [''] * len(headers))[column]
            if key is None or not value:
                return 1
            span = 1
            while row_index + span < len(rows):
                next_values = rows[row_index + span].get('values') or []
                next_keys = canonical_keys[row_index + span]
                next_key = next_keys[column] if column < len(next_keys) else None
                if next_key != key or not (next_values[column] if column < len(next_values) else ''):
                    break
                span += 1
            return span

        for row_index, row in enumerate(rows):
            values = list(row.get('values') or [''] * len(headers))

            html_cells = []
            tsv_values = []
            for column, value in enumerate(values):
                key = (canonical_keys[row_index][column]
                       if column < len(canonical_keys[row_index]) else None)
                has_value = bool(value)
                is_continuation = (
                    key is not None and row_index > 0 and
                    canonical_keys[row_index - 1][column] == key and
                    has_value)
                if is_continuation:
                    # HTML rowspan was emitted by the previous physical row.
                    tsv_values.append('')
                    continue
                text = str(value or '')
                tags = self._matching_pid_tags(text)
                background = '#FFFFFF' if row_index % 2 == 0 else '#F7F7F5'
                foreground = '#17191C'
                bold = column in (5, 8, 9)
                if column == 5 and row.get('risk_before'):
                    _level, background, foreground = risk_info(
                        row['risk_before'][1], row['risk_before'][2])
                elif column == 9 and row.get('risk_after'):
                    _level, background, foreground = risk_info(
                        row['risk_after'][1], row['risk_after'][2])
                elif column == 8:
                    background = '#F3F4F6'
                style = (
                    f'background:{_colour(background, "#FFFFFF")};'
                    f'color:{_colour(foreground, "#17191C")};'
                    'border:1px solid #CBD5E1;padding:3px 5px;'
                    'vertical-align:top;text-align:left;white-space:normal;'
                    f'font-family:Arial;font-size:9pt;font-weight:'
                    f'{700 if bold else 400};')
                row_span = _canonical_span(row_index, column)
                rowspan = f' rowspan="{row_span}"' if row_span > 1 else ''
                html_cells.append(
                    f'<td{rowspan} style="{style}">{self._clipboard_rich_text(text, tags)}</td>')
                tsv_values.append(text.replace('\t', ' ').replace('\n', ' / '))
            html_rows.append(
                f'<tr style="height:{_row_height(values)}px;">'
                + ''.join(html_cells) + '</tr>')
            tsv_rows.append('\t'.join(tsv_values))

        col_defs = ''.join(f'<col style="width:{width}px">' for width in widths)
        header_cells = ''.join(
            '<th style="background:#EEECE1;color:#111827;border:1px solid #CBD5E1;'
            'padding:4px 5px;text-align:left;font-weight:700;vertical-align:top;">'
            f'{_html_escape(header)}</th>' for header in headers)
        html = (
            '<html><head><meta charset="utf-8"></head><body>'
            '<table style="border-collapse:collapse;border-spacing:0;">'
            f'<colgroup>{col_defs}</colgroup>'
            f'<thead><tr>{header_cells}</tr></thead>'
            f'<tbody>{"".join(html_rows)}</tbody></table></body></html>')
        return html, '\t'.join(headers) + '\n' + '\n'.join(tsv_rows)

    def _office_copy_selection(self):
        """Return the visible selection bounds, or the whole visible table.

        A normal Shift/drag selection is rectangular and keeps merged cells
        intact. Ctrl-selected disjoint cells cannot be one native Excel
        clipboard range, so they are represented faithfully in the selected
        physical rows with unselected positions left blank (never filled with
        data the user did not select).
        """
        table = self._table
        selected_ranges = table.selectedRanges()
        if not selected_ranges:
            return (list(range(table.rowCount())),
                    [col for col in range(table.columnCount())
                     if not table.isColumnHidden(col)], None, False)
        # A normal mouse/Shift selection is one Qt range. Preserve that
        # exact rectangle even though selectedIndexes() omits coordinates
        # covered by a rowspan. This is the common Office-copy workflow and
        # prevents a covered child cell from being interpreted as a separate
        # source object.
        if len(selected_ranges) == 1:
            selection = selected_ranges[0]
            rows = list(range(selection.topRow(), selection.bottomRow() + 1))
            columns = [col for col in range(selection.leftColumn(),
                                             selection.rightColumn() + 1)
                       if not table.isColumnHidden(col)]
            return (rows, columns,
                    {(row, col) for row in rows for col in columns}, False)

        # A Ctrl-selection of individual Nod/Avvikelse cells is a selection
        # of hierarchy *anchors*, not all of the physical consequence rows
        # covered by their rowspans.  Expanding those spans turns two selected
        # nodes into a long block of duplicate node labels and can hide the
        # second deviation in Word/Excel.  Keep this narrow, explicit
        # hierarchy-only selection compact: one exported row per selected
        # visual hierarchy row.
        hierarchy_columns = {self._C_NOD, self._C_UTR, self._C_DEV}
        range_cells = {
            (row, col)
            for selection in selected_ranges
            for row in range(selection.topRow(), selection.bottomRow() + 1)
            for col in range(selection.leftColumn(), selection.rightColumn() + 1)
            if not table.isColumnHidden(col)
        }
        selected_columns = {col for _row, col in range_cells}
        if (range_cells and selected_columns and
                selected_columns.issubset(hierarchy_columns) and
                all(selection.topRow() == selection.bottomRow()
                    for selection in selected_ranges)):
            rows = sorted({row for row, _col in range_cells})
            columns = sorted(selected_columns)
            return rows, columns, range_cells, True

        # For other Ctrl-selections, preserve only the physical rows the user
        # actually selected.  The old bounding rectangle inserted every row
        # between two selected causes/consequences; those unrelated rows then
        # looked like the copied operation had picked the wrong objects.
        rows = sorted({row for row, _col in range_cells})
        columns = sorted(selected_columns)
        if range_cells and rows and columns:
            return rows, columns, range_cells, False

        # QTableWidget deliberately omits coordinates covered by a rowspan
        # from selectedIndexes().  That is correct for editing, but not for
        # an external clipboard: selecting a cause plus two consequence rows
        # must mean that the single visual cause cell is selected across both
        # rows. Expand those covered coordinates before deciding whether this
        # is one rectangular selection or genuinely disjoint Ctrl selection.
        selected = {(index.row(), index.column()) for index in table.selectedIndexes()
                    if not table.isColumnHidden(index.column())}
        if not selected:
            return [], [], set(), False

        expanded = set(selected)
        for row, col in list(selected):
            span = table.rowSpan(row, col)
            if span > 1:
                expanded.update((covered_row, col)
                                for covered_row in range(row, row + span))
        selected = expanded
        row_min = min(row for row, _col in selected)
        row_max = max(row for row, _col in selected)
        col_min = min(col for _row, col in selected)
        col_max = max(col for _row, col in selected)
        rows = list(range(row_min, row_max + 1))
        columns = [col for col in range(col_min, col_max + 1)
                   if not table.isColumnHidden(col)]
        return rows, columns, selected, False

    def _office_cell_merge_key(self, row, col):
        """Stable identity for the visible Office-copy cell at (row, col).

        Qt's ``rowSpan`` is a painting detail and is not reliable for a
        covered coordinate after a mixed selection.  The worksheet itself is
        hierarchical, so use the row metadata and persisted IDs to find the
        actual cell anchor.  In particular, a repeated safeguard ID means
        one barrier cell across category rows, while different consequences
        and different safeguards can never be merged accidentally.
        """
        if row < 0 or row >= len(self._row_meta):
            return None
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        item = self._table.item(row, col)
        if col == self._C_NOD:
            value = item.data(Qt.ItemDataRole.UserRole) if item else None
            return ('node', value) if value is not None else None
        if col == self._C_UTR:
            value = item.data(Qt.ItemDataRole.UserRole) if item else None
            return ('equipment', value) if value is not None else None
        if col == self._C_DEV:
            return ('deviation', dev_id) if dev_id is not None else None
        if col in (self._C_ORS,):
            return ('cause', cause_id) if cause_id is not None else None
        if col == self._C_KON:
            return ('consequence', cons_id) if cons_id is not None else None
        if col in (self._C_RFORE, self._C_SLUT):
            cat_info = (self._row_cat_info[row]
                        if row < len(self._row_cat_info) else None)
            if cat_info is not None:
                return ('risk-category', cons_id, cat_info[1])
            return ('risk', cons_id) if cons_id is not None else None
        if col in (self._C_SG,):
            return ('safeguard', sg_id) if sg_id is not None else None
        if col == self._C_LOPA:
            return ('enablers', cons_id) if cons_id is not None else None
        if col == self._C_REK:
            rec_id = (self._row_recommendation_ids[row]
                      if row < len(self._row_recommendation_ids) else None)
            return ('recommendation', rec_id) if rec_id is not None else None
        return None

    def _office_cell_anchor(self, row, col):
        """Find the first physical row of one stable Office-copy cell."""
        key = self._office_cell_merge_key(row, col)
        if key is None:
            return row, 1
        anchor = row
        while anchor > 0 and self._office_cell_merge_key(anchor - 1, col) == key:
            anchor -= 1
        span = 1
        while (anchor + span < self._table.rowCount() and
               self._office_cell_merge_key(anchor + span, col) == key):
            span += 1
        return anchor, span

    def _office_clipboard_payload(self, title='HAZOP Worksheet'):
        """Build HTML + TSV of a selected or complete visible worksheet grid.

        HTML table clipboard data is understood by both Word and desktop
        Excel. TSV is included as a safe fallback for applications that only
        accept plain text. Internal HAZOP copy data is deliberately separate.
        """
        table = self._table
        rows, columns, selected_cells, compact_hierarchy_rows = (
            self._office_copy_selection())
        if not columns or not rows:
            return '', ''

        full_rectangle = (not compact_hierarchy_rows and
                          (selected_cells is None or all(
            (row, col) in selected_cells for row in rows for col in columns))
        )
        visible_columns = [col for col in range(table.columnCount())
                           if not table.isColumnHidden(col)]
        worksheet_columns = [
            self._C_NOD, self._C_DEV, self._C_ORS, self._C_KON,
            self._C_RFORE, self._C_SG, self._C_LOPA, self._C_SLUT,
            self._C_REK,
        ]
        if (full_rectangle and rows == list(range(table.rowCount())) and
                columns == visible_columns and columns == worksheet_columns):
            reference_payload = self._worksheet_office_reference_payload()
            if reference_payload is not None:
                return reference_payload

        export_columns = [
            (col, table.horizontalHeaderItem(col).text()
             if table.horizontalHeaderItem(col) else '', 'native',
             max(40, table.columnWidth(col)))
            for col in columns
        ]
        def _anchor_for(row, col):
            if compact_hierarchy_rows:
                # Each output row deliberately represents the exact
                # hierarchy anchor the user Ctrl-selected.  Do not let a
                # neighbouring Qt rowspan claim that row; the table still
                # stores a real item at every physical row during rebuild.
                return row, 1
            # Resolve the anchor from persisted entity identity, not from
            # QTableWidget.rowSpan().  Qt can report a covered coordinate as
            # a fresh cell during a mixed Ctrl-selection, which was the
            # source of copied causes/consequences borrowing a neighbour's
            # object.  The stable key also lets a repeated safeguard merge
            # correctly in a partial rectangular selection.
            return self._office_cell_anchor(row, col)

        header_cells = []
        col_defs = []
        for source_col, header_text, _kind, width in export_columns:
            header_cells.append(
                '<th style="background:#F5F5F3;color:#17191C;border:1px solid #B8BDC4;'
                'padding:4px 5px;text-align:left;font-weight:700;vertical-align:top;">'
                f'{_html_escape(header_text)}</th>')
            col_defs.append(f'<col style="width:{max(40, width)}px">')

        html_rows = []
        tsv_rows = []
        row_min, row_max = rows[0], rows[-1]
        for row in rows:
            html_cells = []
            tsv_cells = []
            for source_col, _header_text, kind, _width in export_columns:
                col = source_col
                anchor, row_span = _anchor_for(row, col)
                is_selected = selected_cells is None or (row, col) in selected_cells
                if not is_selected:
                    # For Ctrl-selected, disjoint cells retain their relative
                    # positions in one Excel/Word grid. Empty positions must
                    # remain empty rather than silently importing neighbours.
                    tsv_cells.append('')
                    html_cells.append(
                        '<td style="background:#FFFFFF;border:1px solid #D1D5DB;'
                        'padding:3px 5px;"></td>')
                    continue
                if full_rectangle:
                    span_start = max(anchor, row_min)
                    span_end = min(anchor + row_span, row_max + 1)
                    if row != span_start:
                        # The clipped rowspan cell is already emitted by its
                        # first selected row.
                        continue
                    export_row_span = max(1, span_end - span_start)
                    item = table.item(anchor, col)
                    text, tags = self._clipboard_cell_content(anchor, col)
                else:
                    # Sparse Ctrl-selections cannot safely retain a rowspan:
                    # it could cover an unselected gap. Repeat only the
                    # explicitly selected cell's displayed value instead.
                    export_row_span = 1
                    item = table.item(anchor, col)
                    text, tags = self._clipboard_cell_content(anchor, col)
                tsv_cells.append(text.replace('\t', ' ').replace('\n', ' / '))
                bg = self._clipboard_brush_css(item.background() if item else None) or '#FFFFFF'
                fg = self._clipboard_brush_css(item.foreground() if item else None) or '#17191C'
                font = item.font() if item and item.font().family() else table.font()
                weight = '700' if font.bold() else '400'
                point_size = font.pointSizeF() if font.pointSizeF() > 0 else table.font().pointSizeF()
                style = (
                    f'background:{bg};color:{fg};border:1px solid #D1D5DB;'
                    f'padding:3px 5px;vertical-align:top;text-align:left;'
                    f'font-family:{_html_escape(font.family() or table.font().family())};'
                    f'font-size:{max(7.0, point_size):.1f}pt;font-weight:{weight};'
                    f'white-space:normal;')
                rowspan = (f' rowspan="{export_row_span}"'
                           if export_row_span > 1 else '')
                html_cells.append(
                    f'<td{rowspan} style="{style}">{self._clipboard_rich_text(text, tags)}</td>')
            # TSV cannot express rowspans. Keep covered hierarchy positions
            # empty rather than duplicating their anchor value: this mirrors
            # the visual hierarchy for clients that fall back to plain text.
            if full_rectangle and len(tsv_cells) < len(export_columns):
                rebuilt_tsv = []
                for source_col, _header_text, _kind, _width in export_columns:
                    col = source_col
                    anchor, _row_span = _anchor_for(row, col)
                    if anchor != row:
                        rebuilt_tsv.append('')
                    else:
                        rebuilt_tsv.append(
                            self._clipboard_cell_content(anchor, col)[0]
                            .replace('\t', ' ').replace('\n', ' / '))
                tsv_cells = rebuilt_tsv
            tsv_rows.append('\t'.join(tsv_cells))
            html_rows.append(
                f'<tr style="height:{max(18, table.rowHeight(row))}px;">' +
                ''.join(html_cells) + '</tr>')

        html = (
            '<html><head><meta charset="utf-8"></head><body>'
            '<table style="border-collapse:collapse;border-spacing:0;">'
            f'<colgroup>{"".join(col_defs)}</colgroup>'
            f'<thead><tr>{"".join(header_cells)}</tr></thead>'
            f'<tbody>{"".join(html_rows)}</tbody></table></body></html>')
        headers = [header_text for _source_col, header_text, _kind, _width
                   in export_columns]
        return html, '\t'.join(headers) + '\n' + '\n'.join(tsv_rows)

    def copy_visible_table_to_office_clipboard(self, title='HAZOP Worksheet',
                                               internal_payload=None):
        """Copy selected cells for Office, optionally with HAZOP entity data."""
        html, plain_text = self._office_clipboard_payload(title)
        if not html:
            return False
        mime = QMimeData()
        mime.setHtml(html)
        mime.setText(plain_text)
        if internal_payload:
            mime.setData(self._COPY_MIME, internal_payload)
        QApplication.clipboard().setMimeData(mime)
        return True

    def _copy_row_text(self, row):
        if row < 0 or row >= len(self._row_meta):
            return ''
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
        return '\t'.join(parts)

    def _copy_kind_for_cell(self, row, col):
        """Return the persisted HAZOP entity represented by one table cell."""
        if row < 0 or row >= len(self._row_meta):
            return None, None
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col in (self._C_NOD, self._C_UTR, self._C_DEV, self._C_ORS):
            return ('cause', cause_id) if cause_id is not None else (None, None)
        if col in (self._C_KON, self._C_RFORE, self._C_LOPA, self._C_SLUT):
            return ('cons', cons_id) if cons_id is not None else (None, None)
        if col == self._C_SG:
            return ('sg', sg_id) if sg_id is not None else (None, None)
        if col == self._C_REK:
            rec_id = (self._row_recommendation_ids[row]
                      if row < len(self._row_recommendation_ids) else None)
            return ('rec', rec_id) if rec_id is not None else (None, None)
        return None, None

    def _selected_copy_ids(self, row, col, kind, item_id):
        """Selected same-kind entities, in visible row order, without duplicates."""
        entries = []
        seen = set()
        for index in self._table.selectedIndexes():
            if index.column() != col:
                continue
            selected_kind, selected_id = self._copy_kind_for_cell(
                index.row(), index.column())
            if selected_kind != kind or selected_id is None or selected_id in seen:
                continue
            seen.add(selected_id)
            entries.append((int(selected_id), index.row()))
        if item_id not in seen:
            entries.append((int(item_id), row))
        entries.sort(key=lambda entry: entry[1])
        return [entry_id for entry_id, _ in entries]

    def _copy_entity_payload(self, row, col=None):
        """Return the drag-compatible HAZOP payload for the active cell."""
        if col is None:
            col = self._table.currentColumn()
        kind, item_id = self._copy_kind_for_cell(row, col)
        if not kind or item_id is None:
            return None
        payload = {
            'version': 1,
            'kind': kind,
            'ids': self._selected_copy_ids(row, col, kind, item_id),
        }
        return json.dumps(payload).encode('utf-8')

    def _copy_row_to_clipboard(self, row, col=None):
        """Copy the active entity for the explicit in-HAZOP menu action."""
        payload = self._copy_entity_payload(row, col)
        if not payload:
            return False
        mime = QMimeData()
        mime.setData(self._COPY_MIME, payload)
        mime.setText(self._copy_row_text(row))
        QApplication.clipboard().setMimeData(mime)
        return True

    def _copy_selection_to_clipboard(self, row, col, title):
        """Put one Ctrl+C selection on both the Office and HAZOP clipboards."""
        payload = self._copy_entity_payload(row, col)
        if self.copy_visible_table_to_office_clipboard(title, payload):
            return True
        # A table without exportable cells can still have a valid active
        # entity (for example while a view is rebuilding). Preserve the
        # internal copy path rather than silently losing Ctrl+C entirely.
        if payload:
            mime = QMimeData()
            mime.setData(self._COPY_MIME, payload)
            mime.setText(self._copy_row_text(row))
            QApplication.clipboard().setMimeData(mime)
            return True
        return False

    def _ask_copy_scope(self, kind, count):
        """Ask once per operation whether copied hierarchy children follow."""
        if kind not in ('cause', 'cons'):
            return 'cell'
        label = 'orsak' if kind == 'cause' else 'konsekvens'
        plural = 'er' if count != 1 else ''
        box = QMessageBox(self)
        box.setWindowTitle('Kopiera i HAZOP')
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f'Kopiera {count} {label}{plural}')
        if kind == 'cause':
            box.setInformativeText(
                'Ska endast orsaksinnehållet kopieras, eller även hela '
                'understrukturen med konsekvenser, riskbedömningar, '
                'barriärer, enablers och rekommendationer?')
        else:
            box.setInformativeText(
                'Ska endast konsekvenscellens innehåll kopieras, eller även '
                'riskbedömningar, barriärer, enablers och rekommendationer?')
        cell_btn = box.addButton('Endast cellinnehåll',
                                 QMessageBox.ButtonRole.AcceptRole)
        branch_btn = box.addButton('Inkludera underkategorier',
                                   QMessageBox.ButtonRole.ActionRole)
        cancel_btn = box.addButton('Avbryt', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cell_btn)
        box.exec()
        if box.clickedButton() is branch_btn:
            return 'branch'
        if box.clickedButton() is cell_btn:
            return 'cell'
        return None

    def _copy_entities_to_target(self, kind, item_ids, target_row, target_col,
                                 ask_scope=True):
        """Copy one or more entities atomically in the undo history."""
        with self.db.history_group():
            return self._copy_entities_to_target_inner(
                kind, item_ids, target_row, target_col, ask_scope)

    def _copy_entities_to_target_inner(self, kind, item_ids, target_row, target_col,
                                       ask_scope=True):
        """Copy entities to the hierarchy implied by a table target.

        Used by both drop and paste so their rules cannot drift.  Returns the
        list of created IDs; an empty list means no mutation was made.
        """
        if (target_row < 0 or target_row >= len(self._row_meta) or
                not item_ids):
            return []
        tgt_dev, tgt_cause, tgt_cons, _tgt_sg = self._row_meta[target_row]
        valid_ids = []
        seen = set()
        for item_id in item_ids:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                continue
            if item_id not in seen:
                seen.add(item_id)
                valid_ids.append(item_id)
        if not valid_ids:
            return []

        if kind == 'cause' and tgt_dev is None:
            return []
        if kind == 'cons' and tgt_cause is None:
            return []
        if kind in ('sg', 'rec') and tgt_cons is None:
            return []

        scope = self._ask_copy_scope(kind, len(valid_ids)) if ask_scope else 'branch'
        if scope is None:
            return []

        created = []
        try:
            if kind == 'cause':
                for item_id in valid_ids:
                    new_id = self.db.copy_cause_scoped(
                        item_id, tgt_dev, include_descendants=(scope == 'branch'))
                    if new_id is not None:
                        created.append(('cause', new_id))
            elif kind == 'cons':
                for item_id in valid_ids:
                    new_id = self.db.copy_consequence_scoped(
                        item_id, tgt_cause, include_descendants=(scope == 'branch'))
                    if new_id is not None:
                        created.append(('cons', new_id))
            elif kind == 'sg':
                for item_id in valid_ids:
                    new_id = self.db.copy_safeguard_scoped(item_id, tgt_cons)
                    if new_id is not None:
                        created.append(('sg', new_id))
            elif kind == 'rec':
                existing = {row['id'] for row in
                            self.db.recommendations_for_consequence(tgt_cons)}
                for item_id in valid_ids:
                    if item_id in existing or not self.db.get_recommendation(item_id):
                        continue
                    self.db.link_recommendation_to_consequence(item_id, tgt_cons)
                    created.append(('rec', item_id))
                    existing.add(item_id)
        except Exception:
            logging.exception('HAZOP copy failed: kind=%s target row=%s', kind, target_row)
            QMessageBox.critical(self, 'Kopieringen misslyckades',
                                 'Kopieringen kunde inte slutföras. Inga delvisa '
                                 'ändringar ska ha sparats för den berörda posten.')
            return []

        if created:
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
        return created

    def _paste_from_clipboard(self, target_row, target_col):
        mime = QApplication.clipboard().mimeData()
        if not mime or not mime.hasFormat(self._COPY_MIME):
            return
        try:
            payload = json.loads(bytes(mime.data(self._COPY_MIME)).decode('utf-8'))
            kind = payload.get('kind')
            item_ids = payload.get('ids') or []
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return
        if kind not in ('cause', 'cons', 'sg', 'rec'):
            return
        self._copy_entities_to_target(kind, item_ids, target_row, target_col)

    # ── Feature: Delete key ───────────────────────────────────────────────────
    def _delete_current_item(self):
        row = self._table.currentRow()
        col = self._table.currentColumn()
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_REK:
            rec_id = (self._row_recommendation_ids[row]
                      if row < len(self._row_recommendation_ids) else None)
            if not rec_id or not cons_id:
                return
            rec = self.db.get_recommendation(rec_id)
            label = f"{(rec or {}).get('display_number', rec_id):03d}"
            if rec and (rec.get('description') or '').strip():
                label += f" – {(rec.get('description') or '').strip()[:70]}"
            link_count = self.db.recommendation_consequence_count(rec_id)
            if link_count <= 1:
                box = QMessageBox(self)
                box.setWindowTitle("Ta bort rekommendation")
                box.setText(f"{label} används inte längre efter denna borttagning.")
                box.setInformativeText("Vill du ta bort rekommendationen globalt, eller bara från denna konsekvens?")
                global_btn = box.addButton("Ta bort globalt", QMessageBox.ButtonRole.DestructiveRole)
                unlink_btn = box.addButton("Bara från denna", QMessageBox.ButtonRole.AcceptRole)
                cancel_btn = box.addButton("Avbryt", QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(unlink_btn)
                box.exec()
                clicked = box.clickedButton()
                if clicked is cancel_btn:
                    return
                if clicked is global_btn:
                    self.db.delete_recommendation(rec_id)
                else:
                    self.db.unlink_recommendation_from_consequence(rec_id, cons_id)
            elif QMessageBox.question(
                    self, "Ta bort rekommendation",
                    f"Ta bort {label} från denna konsekvens?\n\n"
                    "Rekommendationen används fortfarande på andra ställen.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                self.db.unlink_recommendation_from_consequence(rec_id, cons_id)
                self._refresh_recommendation_cell(cons_id)
                self.structure_changed.emit()
            if link_count <= 1:
                self._refresh_recommendation_cell(cons_id)
                self.structure_changed.emit()
            return
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
    def _make_compact_drag_pixmap(self, row, col, kind, item_id,
                                  is_copy_modifier, item_count=1):
        """Create a small drag ghost instead of copying the whole cell."""
        labels = {'cause': 'Orsak', 'cons': 'Konsekvens', 'sg': 'Barriär',
                  'rec': 'Rekommendation'}
        # Drag-and-drop is intentionally copy-only.  Explicit move commands
        # remain available in the context menu for the few cases where a
        # hierarchy item genuinely needs to be relocated.
        action = 'Kopiera'
        item = self._table.item(row, col)
        text = ' '.join((item.text() if item else '').split())
        if item_count > 1:
            text = f'{item_count} fält'
        elif not text:
            text = f'#{item_id}'

        font = QFont(self._table.font())
        font.setPointSize(max(8, min(10, font.pointSize())))
        fm = QFontMetrics(font)
        prefix = f'{action} {labels.get(kind, kind)}: '
        max_width = 250
        text_width = max(1, max_width - fm.horizontalAdvance(prefix) - 12)
        shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, text_width)
        display = prefix + shown
        width = min(max_width, max(150, fm.horizontalAdvance(display) + 16))
        height = max(26, fm.height() + 10)

        pm = QPixmap(width, height)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#B8C1D1')))
        painter.setBrush(QBrush(QColor(255, 255, 255, 238)))
        painter.drawRoundedRect(pm.rect().adjusted(1, 1, -2, -2), 5, 5)
        painter.setFont(font)
        painter.setPen(QColor('#20252B'))
        painter.drawText(pm.rect().adjusted(8, 0, -8, 0),
                         Qt.AlignmentFlag.AlignVCenter, display)
        painter.end()
        return pm

    def _selected_drag_entries(self, row, col, kind, item_id):
        """Return unique same-kind fields selected for a multi-drag."""
        ids = self._selected_copy_ids(row, col, kind, item_id)
        rows_by_id = {}
        for index in self._table.selectedIndexes():
            selected_kind, selected_id = self._copy_kind_for_cell(
                index.row(), index.column())
            if selected_kind == kind and selected_id is not None:
                rows_by_id.setdefault(int(selected_id), index.row())
        rows_by_id.setdefault(int(item_id), row)
        return [(entry_id, rows_by_id.get(entry_id, row)) for entry_id in ids]

    def _start_drag(self, row, col, is_copy_modifier):
        if row < 0 or row >= len(self._row_meta):
            return
        kind, item_id = self._copy_kind_for_cell(row, col)
        if not kind or item_id is None:
            return

        entries = self._selected_drag_entries(row, col, kind, item_id)
        mime = QMimeData()
        if len(entries) > 1:
            encoded = ';'.join(f'{entry_id},{source_row}'
                               for entry_id, source_row in entries)
            mime.setText(f'hzp:scenario-multi:{kind}:{encoded}')
        else:
            mime.setText(f'hzp:{kind}:{item_id}:{row}:{col}')

        # Keep the drag ghost compact even for tall/wrapped/grouped cells.
        pm = self._make_compact_drag_pixmap(
            row, col, kind, item_id, is_copy_modifier, len(entries))

        drag = QDrag(self._table)
        drag.setMimeData(mime)
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())
        drag.exec(Qt.DropAction.CopyAction)

    def _handle_drop(self, event, source_obj=None):
        text = event.mimeData().text()
        if not text.startswith('hzp:'):
            return
        parts = text.split(':')
        if len(parts) >= 4 and parts[1] == 'scenario-multi':
            kind = parts[2]
            entries = []
            try:
                for encoded in parts[3].split(';'):
                    item_id_s, src_row_s = encoded.split(',', 1)
                    entries.append((int(item_id_s), int(src_row_s)))
            except (TypeError, ValueError):
                return
            if not entries:
                return
            item_ids = [item_id for item_id, _ in entries]
            source_rows = [source_row for _, source_row in entries]
            src_row = source_rows[0]
            item_id_s = str(item_ids[0])
        else:
            if len(parts) < 5:
                return
            kind, item_id_s, src_row_s = parts[1], parts[2], parts[3]
            try:
                src_row = int(src_row_s)
            except ValueError:
                return
            try:
                item_ids = [int(item_id_s)]
            except ValueError:
                return
            source_rows = [src_row]
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
        if isinstance(source_obj, (_BoldTagTextEdit, QLineEdit)):
            tgt_row = source_obj.property('editing_row')
            tgt_col = source_obj.property('editing_col')
            if tgt_row is None or tgt_col is None:
                event.ignore(); return
            tgt_row, tgt_col = int(tgt_row), int(tgt_col)
        else:
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
            if tgt_col == self._C_ORS and tgt_cause is not None:
                equip = equips[0]
                current = self.db.get_cause(tgt_cause)
                current_ids = self._group_equipment_ids(current)
                is_group = len(current_ids) >= 2 or len(equips) > 1
                if is_group:
                    ids = list(current_ids) if len(current_ids) >= 2 else []
                    for selected in equips:
                        if selected.get('id') not in ids:
                            ids.append(selected.get('id'))
                    ids = [value for value in ids if value is not None][:MAX_GROUP_OBJECTS]
                    operator_match = re.search(
                        r'\s(&|OR|<>|->|\+)\s',
                        (current.get('comp_tag') or '') if current else '',
                        re.IGNORECASE)
                    operator = ('&' if not operator_match or operator_match.group(1) == '+'
                                else ('OR' if operator_match.group(1).casefold() in ('<>', 'or')
                                      else operator_match.group(1)))
                    tags = []
                    for equipment_id in ids:
                        selected = self.db.get_equipment_by_id(equipment_id)
                        if selected:
                            tags.append(selected.get('tag', '').strip())
                    self.db.update_cause(
                        tgt_cause,
                        comp_type=(self.db.get_equipment_by_id(ids[0]) or equip).get(
                            'equipment_type', ''),
                        comp_tag=f' {operator} '.join(tags),
                        equipment_id=ids[0],
                        secondary_equipment_id=ids[1] if len(ids) > 1 else None,
                        group_equipment_ids=ids)
                else:
                    self.db.update_cause(
                        tgt_cause,
                        comp_type=equip.get('equipment_type', ''),
                        comp_tag=equip.get('tag', '').strip(),
                        equipment_id=equip.get('id'),
                        secondary_equipment_id=None,
                        group_equipment_ids='')
                self._schedule_rebuild()
                QTimer.singleShot(0, lambda cid=tgt_cause:
                                  self.item_edited.emit(CAUSE_T, cid))
            elif tgt_col == self._C_KON and tgt_cons is not None:
                for equip in equips:
                    self.db.append_tag_to_consequence(
                        tgt_cons, equip.get('tag', ''), equip.get('equipment_type', ''))
            elif tgt_col == self._C_SG and tgt_cons is not None:
                # The dropped-on row only ever absorbs an object if it has
                # no object on it yet — once it already carries a tag
                # (from an earlier drop, single or multi), a NEW drop must
                # still land on its own new row, not merge into that
                # row's text (2026-08-09, see NOTES.md: "jag vill att den
                # ... skall lägga till flera olika objekt om jag drar till
                # safeguards med (flera rader)" — applies whether the
                # extra objects arrive in one multi-select drag or as
                # separate later single-object drags onto the same row).
                sg_row = self.db.get_safeguard(tgt_sg) if tgt_sg is not None else None
                row_is_free = (tgt_sg is not None and bool(sg_row)
                               and not (sg_row.get('tagged_refs') or '').strip())
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
            # Dropping on KON/SG can add or append rows that are also shown
            # in the left tree (especially Shift-drag with several objects).
            # The scenario table rebuild alone does not notify MainWindow,
            # so refresh the tree after the drop has committed.  ORS already
            # emits item_edited above and keeps its existing path unchanged.
            if tgt_col in (self._C_KON, self._C_SG):
                QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()
            return

        if kind not in ('cause', 'cons', 'sg', 'rec'):
            event.ignore()
            return
        created = self._copy_entities_to_target(
            kind, item_ids, tgt_row, tgt_col, ask_scope=True)
        if not created:
            event.ignore()
            return
        event.acceptProposedAction()

    # ── Feature 4 & 5: Context menu ───────────────────────────────────────────
    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        col = self._table.columnAt(pos.x())
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]

        # The table's custom-context signal is more reliable than depending
        # on which internal table widget received the mouse press.  For a
        # grouped object tag, show the row-order menu before the generic row
        # actions are built.
        if col == self._C_ORS and cause_id:
            item = self._table.item(row, col)
            tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
            if len(tags) >= 2:
                line_h = max(_ORS_FIRST_LINE_H,
                             QFontMetrics(self._table.font()).height() + 4)
                line_no = int((pos.y() - self._table.rowViewportPosition(row) - 2)
                              // line_h)
                if 0 <= line_no < len(tags):
                    x = self._table.columnViewportPosition(col) + 2
                    if line_no == 0:
                        num = item.data(Qt.ItemDataRole.UserRole + 10) or ''
                        if num:
                            x += QFontMetrics(self._table.font()).horizontalAdvance(
                                f"{num}.  ")
                    else:
                        operators = self._group_operators(item)
                        operator = operators[line_no] if line_no < len(operators) else 'OR'
                        x += QFontMetrics(self._table.font()).horizontalAdvance(
                            f"{operator} ")
                    tag_font = QFont(self._table.font())
                    tag_font.setBold(True)
                    width = QFontMetrics(tag_font).horizontalAdvance(str(tags[line_no]))
                    if x <= pos.x() <= x + width:
                        self._show_group_tag_menu(
                            row, cause_id, line_no,
                            self._table.viewport().mapToGlobal(pos))
                        return

        menu = QMenu(self)

        # ── Internal HAZOP copy ─────────────────────────────────────────
        # Ordinary Ctrl+C exports the exact cell selection to Word/Excel.
        # This action intentionally keeps the earlier entity/hierarchy copy
        # path available when the target is another HAZOP cell.
        copy_row = menu.addAction(_icon('clipboard'),
                                   "Kopiera inom HAZOP (Ctrl+Alt+C)")
        copy_row.triggered.connect(
            lambda: self._copy_row_to_clipboard(row, col))
        menu.addSeparator()

        # ── Orsak-åtgärder ──────────────────────────────────────────────
        if col in (self._C_ORS, self._C_NOD, self._C_DEV) and cause_id:
            c = self.db.get_cause(cause_id)
            c_desc = dict(c).get('description', '?')[:40] if c else '?'
            menu.addSection(_icon('settings'), f"Orsak: {c_desc}")
            ors_item = self._table.item(row, self._C_ORS)
            grouped = bool(ors_item and
                           len(ors_item.data(Qt.ItemDataRole.UserRole + 9) or []) >= 2)
            if not grouped:
                menu.addAction(_icon('edit'), "Redigera",
                    lambda: self._try_start_edit(row, self._C_ORS))
            a_dup = menu.addAction(_icon('document'), "Duplicera orsak (med konsekvenser)")
            a_dup.triggered.connect(
                lambda: self._duplicate_cause(cause_id))
            a_move = menu.addAction("↕  Flytta till annan avvikelse…")
            a_move.triggered.connect(
                lambda: self._move_cause_dialog(cause_id))
            a_comment = menu.addAction(_icon('comment'), "Kommentar…")
            a_comment.triggered.connect(
                lambda: self._open_comment_popup(
                    row, cause_id, self._table.viewport().mapToGlobal(pos)))
            a_clone = menu.addAction(_icon('clipboard'), "Duplicera scenario till annan avvikelse…")
            a_clone.triggered.connect(lambda: self._clone_scenario(cause_id))
            cause_tag = (c.get('comp_tag') or '').strip() if c else ''
            if cause_tag:
                a_disconnect = menu.addAction(_icon('close'), f"Ta bort tagg – koppla loss {cause_tag}")
                a_disconnect.triggered.connect(
                    lambda _, cid=cause_id, tag=cause_tag:
                    self._disconnect_tag('cause', cid, tag))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort orsak")
            a_del.triggered.connect(lambda cid=cause_id: self._confirm_delete('cause', cid))

        # ── Konsekvens-åtgärder ─────────────────────────────────────────
        elif col in (self._C_KON, self._C_RFORE) and cons_id:
            k = self.db.get_consequence(cons_id)
            k_desc = dict(k).get('description', '?')[:40] if k else '?'
            menu.addSection(_icon('warning'), f"Konsekvens: {k_desc}")
            menu.addAction(_icon('edit'), "Redigera",
                           lambda: self._try_start_edit(row, self._C_KON))
            a_dup = menu.addAction(_icon('document'), "Duplicera konsekvens (med barriärer)")
            a_dup.triggered.connect(
                lambda: self._duplicate_consequence(cons_id, cause_id))
            a_move = menu.addAction("↕  Flytta till annan orsak…")
            a_move.triggered.connect(
                lambda: self._move_consequence_dialog(cons_id))
            if k:
                k = dict(k)
                tags = parse_tag_refs(k.get('tagged_refs') or '')
                if k.get('comp_tag') and not any(t.casefold() == k['comp_tag'].casefold() for t in tags):
                    tags.insert(0, k['comp_tag'])
                for tag in tags:
                    a_disconnect = menu.addAction(_icon('close'), f"Ta bort tagg – koppla loss {tag}")
                    a_disconnect.triggered.connect(
                        lambda _, cid=cons_id, t=tag:
                        self._disconnect_tag('consequence', cid, t))
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
            a_rrf = menu.addAction(_icon('settings'), "Ändra RRF...")
            a_rrf.triggered.connect(lambda: self._show_rrf_popup(row, sg_id))
            a_copy = menu.addAction(_icon('clipboard'), "Kopiera till annan konsekvens…")
            a_copy.triggered.connect(
                lambda: self._copy_safeguard_dialog(sg_id))
            a_move = menu.addAction("↕  Flytta till annan konsekvens…")
            a_move.triggered.connect(
                lambda: self._move_safeguard_dialog(sg_id))
            if sg:
                sg = dict(sg)
                tags = parse_tag_refs(sg.get('tagged_refs') or '')
                if sg.get('comp_tag') and not any(t.casefold() == sg['comp_tag'].casefold() for t in tags):
                    tags.insert(0, sg['comp_tag'])
                for tag in tags:
                    a_disconnect = menu.addAction(_icon('close'), f"Ta bort tagg – koppla loss {tag}")
                    a_disconnect.triggered.connect(
                        lambda _, sid=sg_id, t=tag:
                        self._disconnect_tag('safeguard', sid, t))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort barriär")
            a_del.triggered.connect(lambda sid=sg_id: self._confirm_delete('sg', sid))

        if not menu.isEmpty():
            menu.exec(self._table.viewport().mapToGlobal(pos))

    def _disconnect_tag(self, kind, id_, tag):
        """Disconnect a tag and keep its related metadata changes atomic."""
        with self.db.history_group():
            return self._disconnect_tag_inner(kind, id_, tag)

    def _disconnect_tag_inner(self, kind, id_, tag):
        """Disconnect one P&ID tag while leaving its characters as prose."""
        tag = (tag or '').strip()
        if not tag:
            return
        self._detached_tags.add(tag.casefold())
        if kind == 'cause':
            row = self.db.get_cause(id_)
            if row and (row.get('comp_tag') or '').strip().casefold() == tag.casefold():
                self.db.update_cause(id_, comp_tag='', comp_type='',
                                     equipment_id=None, secondary_equipment_id=None)
        elif kind == 'consequence':
            row = self.db.get_consequence(id_)
            if row:
                refs = [r for r in parse_tag_refs(row.get('tagged_refs') or '')
                        if r.casefold() != tag.casefold()]
                current = (row.get('comp_tag') or '').strip()
                next_tag = next((r for r in refs if r.casefold() == current.casefold()), '')
                next_type = row.get('comp_type') or '' if next_tag else ''
                self.db.update_consequence(
                    id_, row.get('description') or '', row.get('severity') or 1,
                    row.get('category') or '', row.get('consequence_chain') or '',
                    comp_tag=next_tag, comp_type=next_type,
                    tagged_refs=','.join(refs))
        elif kind == 'safeguard':
            row = self.db.get_safeguard(id_)
            if row:
                refs = [r for r in parse_tag_refs(row.get('tagged_refs') or '')
                        if r.casefold() != tag.casefold()]
                current = (row.get('comp_tag') or '').strip()
                next_tag = next((r for r in refs if r.casefold() == current.casefold()), '')
                self.db.update_safeguard(id_, tagged_refs=','.join(refs))
                self.db.set_safeguard_tag(
                    id_, next_tag, row.get('comp_type') or '' if next_tag else '')
        self._schedule_rebuild()

    def _untag_consequence(self, cons_id):
        """Detach a dragged-in equipment tag from a KON cell without
        deleting the row — the inline "×" this replaced sat in the tag
        strip, which was removed 2026-08-10 (see NOTES.md; the tag still
        shows bolded in the description text via tagged_refs)."""
        row = self.db.get_consequence(cons_id)
        if row:
            self._detached_tags.update(t.casefold() for t in parse_tag_refs(row.get('tagged_refs') or ''))
            self.db.update_consequence(
                cons_id, row.get('description') or '', row.get('severity') or 1,
                row.get('category') or '', row.get('consequence_chain') or '',
                comp_tag='', comp_type='', tagged_refs='')
        self._schedule_rebuild()

    def _untag_safeguard(self, sg_id):
        with self.db.history_group():
            return self._untag_safeguard_inner(sg_id)

    def _untag_safeguard_inner(self, sg_id):
        """Same as _untag_consequence, for a safeguard cell."""
        row = self.db.get_safeguard(sg_id)
        if row:
            self._detached_tags.update(t.casefold() for t in parse_tag_refs(row.get('tagged_refs') or ''))
            self.db.update_safeguard(sg_id, tagged_refs='')
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
        new_id = self.db.copy_consequence_scoped(
            cons_id, cause_id, include_descendants=True)
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
        new_id = self.db.copy_cause_scoped(
            cause_id, dev_id, include_descendants=True)
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
                self.db.copy_safeguard_scoped(sg_id, tgt_cons_id)
            self.structure_changed.emit()
            self._schedule_rebuild()
