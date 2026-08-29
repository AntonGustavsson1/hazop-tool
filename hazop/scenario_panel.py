#!/usr/bin/env python3
"""HAZOP scenario table panel and its dialogs/delegates — split out of
hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer"."""

import re
import json
import logging
import weakref
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
    freq_axis_label, cons_axis_label, _lookup_comp_type_for_tag,
    _draw_text_with_bold_tags, standard_cause_options,
    total_freq_reduction, CHAIN_ITEMS, build_consequence_text, parse_chain_from_json,
)

MAX_GROUP_OBJECTS = 20
from tree_panel import CauseTagPopup, RRFPopup, FrequencyPickerPopup


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
                 db=None, cons_id=None):
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

        self._db          = db
        self._cons_id     = cons_id
        self._category_mode = db is not None and cons_id is not None
        self._current_freq = current_freq
        self._n_cons      = n_cons
        self._freq_on_x   = freq_on_x
        self._x_rev       = x_rev
        self._y_rev       = y_rev
        self._grid_buttons = {}   # (freq_val, cons_val) -> (QPushButton, base label text)

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

                is_current = (not self._category_mode and
                              freq_val == current_freq and cons_val == current_cons)
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
                btn.setProperty('risk_color', color)
                btn.setProperty('risk_fg', fg)
                self._grid_buttons[(freq_val, cons_val)] = (btn, lbl[:4])

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

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        outer.addWidget(cancel_btn)

        self.adjustSize()

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
        saved = {r['category_id']: r['severity']
                 for r in self._db.get_consequence_severities(self._cons_id)}
        self._cats = cats
        self._cat_sel = {c['id']: saved.get(c['id'], 0) for c in cats}
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
                name_l = QLabel(cat['name'])
                name_l.setFixedSize(50, 16)
                name_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
                name_l.setStyleSheet("font-size:8px; font-weight:bold;")
                name_l.setToolTip(cat['name'])
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
                name_l = QLabel(cat['name']); name_l.setFixedWidth(70)
                name_l.setStyleSheet("font-size:9px; font-weight:bold;")
                name_l.setToolTip(cat['name']); row_l.addWidget(name_l)
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
        new_sev = 0 if cur == sev else sev
        self._cat_sel[cat_id] = new_sev
        for s in range(1, self._n_cons + 1):
            btn = self._cat_buttons.get((cat_id, s))
            if btn is not None:
                checked = (s == new_sev)
                btn.setChecked(checked)
                btn.setStyleSheet(self._cat_bstyle(checked))
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
        else:
            super().keyPressEvent(event)


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
        catalog_row = QHBoxLayout()
        self._catalog_combo = QComboBox()
        self._populate_catalog()
        catalog_row.addWidget(self._catalog_combo, 1)
        use_btn = QPushButton("Lägg till vald")
        use_btn.clicked.connect(self._add_catalog_factor)
        catalog_row.addWidget(use_btn)
        layout.addLayout(catalog_row)
        layout.addWidget(QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                          accepted=self.accept, rejected=self.accept))
        self._refresh()

    def _refresh(self):
        # Re-read the shared catalog whenever the dialog refreshes.  A factor
        # edited/created in one consequence can otherwise remain absent from
        # the selector until that dialog is reconstructed.
        self._populate_catalog()
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

    def _populate_catalog(self):
        current = self._catalog_combo.currentData() if hasattr(self, '_catalog_combo') else None
        self._catalog_combo.blockSignals(True)
        self._catalog_combo.clear()
        self._catalog_combo.addItem("Välj sparad faktor…", None)
        selected = -1
        for factor in self.db.reduction_factor_catalog():
            data = dict(factor)
            self._catalog_combo.addItem(
                f"{data['description']} (RRF {data['rrf']})", data)
            if current and data.get('id') == current.get('id'):
                selected = self._catalog_combo.count() - 1
        if selected >= 0:
            self._catalog_combo.setCurrentIndex(selected)
        self._catalog_combo.blockSignals(False)

    def _add(self):
        new_id = self.db.add_reduction_factor(self.consequence_id, 'Ny faktor', 10)
        self._refresh()

    def _add_catalog_factor(self):
        factor = self._catalog_combo.currentData()
        if not factor:
            return
        self.db.add_reduction_factor(
            self.consequence_id, factor.get('description', ''), factor.get('rrf', 10))
        self._refresh()

    def _on_cell(self, row, col):
        item = self._tbl.item(row, 0)
        if not item: return
        rf_id = item.data(Qt.ItemDataRole.UserRole)
        desc = self._tbl.item(row, 0).text() if self._tbl.item(row, 0) else ''
        try: rrf = int(self._tbl.item(row, 1).text()) if self._tbl.item(row, 1) else 10
        except ValueError: rrf = 10
        self.db.update_reduction_factor(rf_id, desc, rrf, 1)
        self._populate_catalog()


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
            return
        editor.setGeometry(QRect(option.rect).adjusted(2, 2, -2, -2))

    def _show_recommendation_assist_popup(self, editor, row, cons_id,
                                          cell_rect, popup_token=None):
        """Mirrors _PidDelegate._show_standard_cause_popup's positioning
        and focus-safety approach exactly (see that method's own
        docstring for why this must be a plain non-toplevel child widget
        of the panel's top-level window, not a QDialog/separate top-level
        Popup) -- only the popup class and its cons_id differ."""
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
                painter.fillRect(r, QColor('#E6ECFA'))
                painter.fillRect(QRect(r.left(), r.top(), 3, r.height()),
                                 QColor('#2F6FED'))
                tc = QColor('#17191C')
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
        # Risk cells no longer contain a pin/icon strip.  Use the whole
        # compact cell and center the short `Per F1 C1` value.
        text_rect = r
        painter.drawText(text_rect.adjusted(2, 2, -2, -2),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
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
                        lines = ((cause.get('description') or '').splitlines()
                                 if cause else [])
                        # Older grouped causes stored both events as one
                        # arrow sentence.  Split that legacy representation
                        # before selecting the clicked row, otherwise the
                        # primary editor receives the secondary event too.
                        if len(lines) == 1 and len(group_tags or []) >= 2:
                            legacy = lines[0]
                            secondary_tag = str(group_tags[1]).strip()
                            secondary_pos = legacy.casefold().find(
                                secondary_tag.casefold(),
                                len(str(group_tags[0]).strip()))
                            if secondary_pos >= 0:
                                lines = [legacy[:secondary_pos].rstrip(' ,:→-'),
                                         legacy[secondary_pos:]]
                            elif '→' in legacy:
                                left, right = legacy.split('→', 1)
                                lines = [left.strip(), right.strip()]
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
        panel = self._panel
        row_meta = getattr(panel, '_row_meta', [])
        cause_id = row_meta[row][1] if row < len(row_meta) else None
        if cause_id is None:
            return
        try:
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
            top_level = panel.window()
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
        except Exception:
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
        if editor is None or sip.isdeleted(editor):
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
            if cause and cause.get('secondary_equipment_id'):
                lines = (cause.get('description') or '').splitlines()
                group_tags = self._panel._table.item(
                    index.row(), index.column()).data(
                        Qt.ItemDataRole.UserRole + 9) or []
                if group_line >= len(group_tags):
                    return
                while len(lines) < len(group_tags):
                    lines.append('')
                # A newly created group may still have an empty description
                # when the secondary row is edited first. Preserve both
                # linked object tags as the two visual row anchors instead
                # of letting the untouched primary row become blank.
                for line_no in range(len(group_tags)):
                    if not lines[line_no].strip() and line_no < len(group_tags):
                        lines[line_no] = str(group_tags[line_no]).strip()
                tag = str(group_tags[int(group_line)]).strip()
                lines[int(group_line)] = f'{tag} {clean}'.strip() if tag else clean
                while lines and not lines[-1].strip():
                    lines.pop()
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
        viewport = self._panel._table.viewport()
        for editor in viewport.findChildren(_BoldTagTextEdit):
            if not editor.isVisible():
                continue
            if (editor.property('editing_row') != index.row() or
                    editor.property('editing_col') != index.column()):
                continue
            # QAbstractItemView normally parents the editor to the viewport,
            # but use an explicit mapping so this remains correct if Qt or a
            # style inserts another intermediate parent later.  Both rects
            # are then expressed in the viewport's coordinate system.
            editor_top_left = editor.mapTo(viewport, QPoint(0, 0))
            editor_rect = QRect(editor_top_left, editor.size())
            if not editor_rect.intersects(rect):
                continue
            try:
                group_line = int(editor.property('group_line'))
            except (TypeError, ValueError):
                continue
            if group_line is not None and group_line >= 0:
                return group_line
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
                    painter.fillRect(r, option.palette.highlight())
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
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                tagged_refs = list(index.data(Qt.ItemDataRole.UserRole + 7) or [])
                tagged_refs += self._panel._matching_pid_tags(desc)
                _draw_text_with_bold_tags(
                    painter, desc_rect.adjusted(2, 1, -2, -1), desc,
                    tagged_refs, option.font, tc, word_wrap=True)

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

                self._draw_plus_badge(painter, r, row, col)
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

                desc_rect = QRect(r.left() + 2, r.top() + 2,
                                   max(0, r.width() - 4 - _RRF_W),
                                   max(0, r.height() - 4))

                # ── Combined text: bold tag prefix (if any) + plain
                # description, word-wrapped as ONE block via the same
                # QTextLayout-based helper already used to bold drag-
                # dropped tags inside KON/SG descriptions — reused here
                # instead of a bespoke layout implementation, given this
                # file's own documented history of paint/geometry bugs.
                tc = (option.palette.highlightedText().color() if sel
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
                    chip_bg = QColor('#2F5FD0') if sel else QColor('#F5F5F3')
                    painter.fillRect(chip_rect, chip_bg)
                    painter.setFont(ff)
                    f_tc = (option.palette.highlightedText().color() if sel
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

                self._draw_plus_badge(painter, r, row, col)
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
                    painter.fillRect(r, option.palette.highlight())
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
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                tagged_refs = list(index.data(Qt.ItemDataRole.UserRole + 8) or [])
                tagged_refs += self._panel._matching_pid_tags(display)
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
        separate row. Hit-tested by eventFilter(), which now calls the
        same _plus_badge_geometry() this does (2026-08-20 follow-up —
        previously each side computed the corner rect independently;
        still agreed by construction, but no longer just by luck)."""
        if self._panel._row_plus_cols.get(row, {}).get(col) is None:
            return
        badge = self._panel._plus_badge_geometry(rect)
        painter.setPen(QColor('#8D9299'))
        f = QFont(painter.font())
        f.setBold(True)
        f.setPointSize(max(7, painter.font().pointSize()))
        painter.setFont(f)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, '+')


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
        # NoFocus on the popup itself and every child (set on each
        # widget built below) — belt-and-suspenders on top of this
        # already being a non-toplevel widget: it must never be able to
        # actively grab keyboard focus via a mouse click either.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("StandardCauseSuggestPopup{background:#FFFFFF;"
                           "border:1px solid #E2E3E1;}")
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
                "border-radius:0px;padding:2px 8px;font-weight:bold;font-size:9px;}"
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
            lbl = QLabel("Gäller för kategori:")
            lbl.setStyleSheet("font-size:9px; color:#666;")
            outer.addWidget(lbl)
            for sev_id, cat_name in self._sev_cat_list:
                cb = QCheckBox(f"{cat_name}")
                cb.setStyleSheet("font-size:10px;")
                cb.setChecked(not excl_by_sev.get(sev_id, False))
                self._cat_checks[sev_id] = cb
                outer.addWidget(cb)

        if self._cause_list:
            sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
            sep3.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep3)
            lbl2 = QLabel("Gäller för orsak:")
            lbl2.setStyleSheet("font-size:9px; color:#666;")
            outer.addWidget(lbl2)
            for cause_id, desc, is_chain in self._cause_list:
                prefix = "⛓ " if is_chain else "⚙ "
                label  = f"{prefix}{desc[:40]}"
                cb = QCheckBox(label)
                cb.setStyleSheet("font-size:10px;")
                cb.setChecked(cause_id not in excl_cause_ids)
                self._cause_checks[cause_id] = cb
                outer.addWidget(cb)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep2)

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
        new_rrf  = self._spin.value()
        new_type = self._type_combo.currentText()
        if new_rrf != self._current_rrf or new_type != self._current_type:
            self.db.update_safeguard(self._sg_id, rrf=new_rrf, sg_type=new_type)

        # Save category exclusions
        for sev_id, cb in self._cat_checks.items():
            excl_set = self.db.get_severity_excluded_sgs(sev_id)
            if cb.isChecked():
                excl_set.discard(self._sg_id)
            else:
                excl_set.add(self._sg_id)
            self.db.set_severity_excluded_sgs(sev_id, excl_set)

        # Save cause exclusions
        excl_cause_ids = {cid for cid, cb in self._cause_checks.items() if not cb.isChecked()}
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
        self._hide_unplaced_tag = False
        self._merge_node_labels = False
        # Kept as a compatibility attribute for hosts that used to opt out of
        # the empty-consequence chain popup. Double-click now always means
        # inline edit, in Scenario as well as Worksheet; the chain editor is
        # available only from the explicit context-menu action.
        self._empty_consequence_chain_popup_enabled = False
        self._row_meta = []   # list of (dev_id, cause_id, cons_id, sg_id) per visible row
        self._row_recommendation_ids = []
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
        self._text_undo_stack = []
        self._undoing_text = False
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
            self._C_RFORE: (QHeaderView.ResizeMode.Interactive,  58),
            self._C_SG:    (QHeaderView.ResizeMode.Interactive, 130),
            self._C_LOPA:  (QHeaderView.ResizeMode.Interactive, 130),
            self._C_SLUT:  (QHeaderView.ResizeMode.Interactive,  58),
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
            "QTableWidget::item:selected{background:#E6ECFA;color:#17191C;}"
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;"
            "font-weight:600;padding:3px;border-radius:0px;}")
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
        for i, node in enumerate(self.db.nodes(), 1):
            if node['id'] == node_id:
                return i
        return None

    def _deviation_number(self, node_id, deviation_id):
        if not node_id or not deviation_id:
            return None
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
        _real_cause_ids = [cd['id'] for cd, _ in causes_to_show if cd is not None]
        cons_by_cause = self.db.consequences_for_causes(_real_cause_ids)
        _all_cons_ids = [dict(c)['id'] for conss in cons_by_cause.values() for c in conss]
        sgs_by_cons = self.db.safeguards_for_consequences(_all_cons_ids)
        cat_rows_by_cons = self.db.get_consequence_severities_for_consequences(_all_cons_ids)
        _all_severity_ids = [dict(r)['id'] for rows in cat_rows_by_cons.values() for r in rows]
        excl_sgs_by_severity = self.db.get_severity_excluded_sgs_for_severities(_all_severity_ids)
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
            node = nodes_by_id.get(cause_d['node_id'])
            node_name = node['name'] if node else '?'
            # A directly-created blank cause keeps the database's required
            # likelihood value for calculations, but must not show a chosen
            # frequency badge until the user selects one.
            frequency_unset = (not (cause_d.get('description') or '').strip()
                               and not (cause_d.get('comp_tag') or '').strip()
                               and cause_d.get('base_frequency') is None
                               and not cause_d.get('standard_cause_id')
                               and cause_d.get('likelihood') == 0)
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
                n_rows = max(n_cats, n_sgs, n_recs, 1)

                # Precompute exclusions per severity assessment
                cat_excl_map = {}           # sev_id → set of excluded sg_ids
                for _cr in cat_rows:
                    cat_excl_map[_cr['id']] = excl_sgs_by_severity.get(_cr['id'], set())

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
                    sg_i    = sgs[i] if i < n_sgs else None
                    rec_i   = (acts_by_cons.get(cons_d['id'], [])[i]
                               if i < n_recs else None)
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
                                  repeats_previous_tag=_repeats_previous_tag,
                                  cause_status=cause_status,
                                  rfs=rfs_by_cons.get(cons_d['id'], []),
                                  acts=acts_by_cons.get(cons_d['id'], []),
                                  recommendation=rec_i,
                                  excl_causes_by_sg=excl_causes_by_sg)
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

        # Consequence-level columns: group by (cons_id, cat_id) so each
        # category assessment forms its own span group
        def _cat_key(r):
            cons_id  = _meta(r, 2)
            cat_info = self._row_cat_info[r] if r < len(self._row_cat_info) else None
            cat_id   = cat_info[0] if cat_info else None
            return (cons_id, cat_id)

        # KON and LOPA always span by consequence. REK spans too while it
        # has zero or one entry; two or more entries keep their own rows.
        for col in (self._C_KON, self._C_LOPA):
            _span_col(col, lambda r: _meta(r, 2))

        rec_counts = {}
        for r in range(n):
            cons_id = _meta(r, 2)
            if cons_id is not None and self._row_recommendation_ids[r] is not None:
                rec_counts[cons_id] = rec_counts.get(cons_id, 0) + 1
        _span_col(
            self._C_REK,
            lambda r: (_meta(r, 2) if rec_counts.get(_meta(r, 2), 0) <= 1 else None))
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

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3,
                    None if cause_d.get('_frequency_unset') else freq)
        ors.setData(Qt.ItemDataRole.UserRole + 8, repeats_previous_tag)
        # Group causes keep both tags as live object references and bold them
        # in the sentence (the old ``primary + secondary`` prefix duplicated
        # the first object visually).
        group_tags = []
        for _eid in self._group_equipment_ids(cause_d):
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
                 cause_popup_list=None, n_cats=0, repeats_previous_tag=False,
                 cause_status=None, rfs=None, acts=None, recommendation=None,
                 excl_causes_by_sg=None):
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
        fa_active  = bool(cons_d.get('fa_active', 0))
        fa_rrf     = cons_d.get('fa_rrf', 10) or 10
        ign_active = bool(cons_d.get('ignition_active', 0))
        ign_rrf    = cons_d.get('ignition_rrf', 10) or 10

        final_f, total_rrf, total_steps = total_freq_reduction(
            freq, sg_rrf, fa_active, fa_rrf, ign_active, ign_rrf, rfs)

        level_b, bg_b, fg_b = risk_info(freq, sev)
        level_s, bg_s, fg_s = risk_info(final_f, sev)

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

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3,
                    None if cause_d.get('_frequency_unset') else freq)
        ors.setData(Qt.ItemDataRole.UserRole + 5, cause_d.get('base_frequency'))
        ors.setData(Qt.ItemDataRole.UserRole + 8, repeats_previous_tag)
        ors.setData(Qt.ItemDataRole.UserRole + 10,
                    self._child_number('cause', dev_d.get('id'), cause_d.get('id')))
        group_tags = []
        for _eid in self._group_equipment_ids(cause_d):
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
        if cat_info:
            cat_short = (cat_name or '')[:3]
            slut_text = f"{cat_short}  {freq_axis_label(final_f)}  {cons_axis_label(sev)}"
        else:
            slut_text = ""
            bg_s, fg_s = '#FFFFFF', '#8D9299'
        rs = QTableWidgetItem(slut_text)
        rs.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        rs.setFlags(rs.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rs.setToolTip(f"{level_s} — {freq_axis_label(final_f)}  {cons_axis_label(sev)}  (−{total_steps} steg totalt)")
        rs.setBackground(QBrush(QColor(bg_s)))
        rs.setForeground(QBrush(QColor(fg_s)))
        rs.setFont(QFont("Consolas", 9))
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
                if cat_info:
                    cat_short = (cat_name or '')[:3]
                    slut_text = f"{cat_short}  {freq_axis_label(final_f)}  {cons_axis_label(sev)}"
                else:
                    slut_text = ""
                    bg_s, fg_s = '#FFFFFF', '#8D9299'
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
                # A manually entered cause should expose the same
                # StandardCauseSuggestPopup as a newly created cause.  The
                # rendered tag zone is consumed earlier by eventFilter and
                # still opens the tag/object popup; only the ordinary cause
                # text area starts inline editing here.  Group causes retain
                # their row-specific editing rules and must not use a generic
                # full-cell editor.
                item = self._table.item(row, col)
                group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or [] \
                    if item else []
                if len(group_tags) < 2:
                    self._try_start_edit(row, col)
            elif dev_id is not None:
                # Wait briefly only so a double-click can cancel this
                # single-click action. Both routes enter the normal inline
                # editor; the former "Orsak på P&ID" dialog is retired.
                self._empty_cause_click_target = dev_id
                self._empty_cause_click_timer.start()
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
            popup = RiskMatrixPopup(cur_freq, cur_cons, self,
                                     db=self.db, cons_id=cons_id)
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id, catid=cat_id:
                    self._apply_risk_from_matrix_cat(caid, coid, catid, f, c))
        else:
            _, cause_id, cons_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(cur_freq, cur_cons, self,
                                     db=self.db, cons_id=cons_id)
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
            if col == self._C_ORS and group_line is not None and group_line >= 0:
                self._group_edit_line = (row, group_line)
            else:
                self._group_edit_line = None
            self._table.edit(self._table.model().index(row, col))
            click = self._double_click_edit
            self._double_click_edit = None
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
        _group_tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
        # Recover legacy grouped tags while a row is being rebuilt.  Some
        # persisted grouped causes only expose the original ``A + B`` tag;
        # the display must still keep the two objects on separate lines.
        if len(_group_tags) < 2 and item:
            _obj_data = item.data(Qt.ItemDataRole.UserRole + 2)
            _legacy_tag = (_obj_data[1] if isinstance(_obj_data, (tuple, list))
                           and len(_obj_data) > 1 else '')
            if isinstance(_legacy_tag, str) and re.search(
                    r'\s(?:&|OR|<>|->|\+)\s', _legacy_tag, re.IGNORECASE):
                _group_tags = [part.strip() for part in re.split(
                    r'\s+(?:&|OR|<>|->|\+)\s+', _legacy_tag,
                    flags=re.IGNORECASE) if part.strip()]
        if len(_group_tags) >= 2:
            _num = item.data(Qt.ItemDataRole.UserRole + 10) or ''
            cause_id = item.data(Qt.ItemDataRole.UserRole)[1] if item.data(Qt.ItemDataRole.UserRole) else None
            cause = self.db.get_cause(cause_id) if cause_id else None
            # A newly created group with no selected standard choices uses
            # the two bare tags as its placeholder.  If a description has
            # nevertheless been entered (for example in the tree), keep it
            # visible in the Scenario cell; the old condition hid that text
            # until the cell entered edit mode.
            if (cause and not cause.get('group_choices_set') and
                    not (desc or '').strip()):
                stored = '\n'.join(_group_tags)
                return f"{_num}.  {stored}" if _num else stored
            # Once a group has a choice, display the stored text verbatim.
            # This preserves partial groups (only primary or only secondary)
            # and also lets the user edit arbitrary group wording later.
            stored = (desc or '').strip()
            if '\n' not in stored and len(_group_tags) >= 2:
                # Older groups stored both events as one arrow sentence.
                # Keep the visual contract of the Scenario group cell by
                # splitting that legacy value back into primary/secondary
                # lines without changing the database text.
                if '→' in stored:
                    left, right = stored.split('→', 1)
                    stored = f'{left.strip()}\n{right.strip()}'
                else:
                    positions = [(stored.casefold().find(tag.casefold()), i)
                                 for i, tag in enumerate(_group_tags[1:], 1)
                                 if tag and stored.casefold().find(tag.casefold()) >= 0]
                    if positions:
                        pos, _ = min(positions)
                        stored = f'{stored[:pos].strip()}\n{stored[pos:].strip()}'
                    else:
                        stored = f'{stored}\n{_group_tags[1]}'
            lines = stored.splitlines()
            while len(lines) < len(_group_tags):
                lines.append(_group_tags[len(lines)])
            stored = '\n'.join(lines[:len(_group_tags)])
            return f"{_num}.  {stored}" if _num else stored
        """The exact string measured (sizeHint) and painted (paint) for
        an ORS cell — "TAG, beskrivning", just "TAG" while the
        description is still an untouched placeholder (same "bare tag
        until something is filled in" rule the HAZOP tree's own Orsak
        rows use, 2026-08-25, for consistency across the app), or just
        the plain description when no tag should show."""
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

    def _group_equipment_ids(self, cause):
        """Return all live equipment links for a group, in display order.

        The first two links remain mirrored in the legacy cause columns for
        backwards compatibility; newer groups use the JSON list as the
        authoritative extension point.
        """
        if not cause:
            return []
        raw = cause.get('group_equipment_ids') or ''
        try:
            ids = [int(value) for value in json.loads(raw)] if raw else []
        except (TypeError, ValueError, json.JSONDecodeError):
            ids = []
        if not ids:
            ids = [cause.get('equipment_id'), cause.get('secondary_equipment_id')]
        return list(dict.fromkeys(value for value in ids if value is not None))[:MAX_GROUP_OBJECTS]

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
        self._schedule_rebuild()

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
        old_ops = [self._normalise_group_operator(value)
                   for value in re.findall(r'\s(&|OR|<>|->|\+)\s',
                                           cause.get('comp_tag') or '',
                                           re.IGNORECASE)]
        if not old_ops:
            old_ops = ['OR']
        if len(old_ops) == 1:
            old_ops *= len(ids) - 1
        old_ops = (old_ops + ['OR'] * len(ids))[:len(ids) - 1]
        old_ops[row_index - 1] = operator
        self.db.update_cause(cause_id,
                             comp_tag=self._group_comp_tag(tags, old_ops),
                             group_equipment_ids=ids)
        self._schedule_rebuild()

    def _move_group_row(self, cause_id, row_index, delta):
        """Move a group row and its description one position up/down."""
        cause = self.db.get_cause(cause_id)
        ids = self._group_equipment_ids(cause)
        target = row_index + delta
        if not cause or not (0 <= row_index < len(ids)) or not (0 <= target < len(ids)):
            return
        ids[row_index], ids[target] = ids[target], ids[row_index]
        tags = []
        for equipment_id in ids:
            equipment = self.db.get_equipment_by_id(equipment_id)
            tags.append((equipment.get('tag') if equipment else '') or 'Objekt')
        old_ops = [self._normalise_group_operator(value)
                   for value in re.findall(r'\s(&|OR|<>|->|\+)\s',
                                           cause.get('comp_tag') or '',
                                           re.IGNORECASE)]
        if not old_ops:
            old_ops = ['OR']
        if len(old_ops) == 1:
            old_ops *= len(ids) - 1
        old_ops = (old_ops + ['OR'] * len(ids))[:len(ids) - 1]
        # Keep operator positions stable while object rows move.  The user
        # can change each affected row's incoming operator independently from
        # its own context menu, without silently changing other connections.
        # Keep group descriptions aligned with their object rows.
        lines = (cause.get('description') or '').splitlines()
        if len(lines) >= len(ids):
            lines[row_index], lines[target] = lines[target], lines[row_index]
        self.db.update_cause(
            cause_id,
            comp_type=(self.db.get_equipment_by_id(ids[0]) or {}).get('equipment_type', ''),
            comp_tag=self._group_comp_tag(tags, old_ops),
            equipment_id=ids[0],
            secondary_equipment_id=ids[1] if len(ids) > 1 else None,
            group_equipment_ids=ids,
            description='\n'.join(lines) if len(lines) >= len(ids) else cause.get('description', ''))
        self._schedule_rebuild()

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
            low = (desc or '').lower()
            direction = 'felar lågt' if 'felar lågt' in low else 'felar högt'
            effect = 'öppnar fullt'
            for option in ('stänger helt', 'stänger felaktigt', 'stänger',
                           'öppnar felaktigt', 'öppnar fullt'):
                if option in low:
                    effect = option
                    break
            return 0
        group_tags = (item.data(Qt.ItemDataRole.UserRole + 9) or []) if item else []
        if len(group_tags) >= 2:
            low = (desc or '').lower()
            direction = 'felar lågt' if 'felar lågt' in low else 'felar högt'
            effect = 'öppnar fullt'
            for option in ('stänger helt', 'stänger felaktigt', 'stänger',
                           'öppnar felaktigt', 'öppnar fullt'):
                if option in low:
                    effect = option
                    break
            return f"{group_tags[0]} {direction}\n{group_tags[1]} {effect}"
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

    def _plus_badge_geometry(self, cell_rect):
        """The in-cell "+" quick-add badge, bottom-right corner — shared
        by _draw_plus_badge() (paint side) and eventFilter()'s click
        zone for it. `_PLUS_BADGE_SIZE` is the badge's own edge length."""
        sz = _PLUS_BADGE_SIZE
        return QRect(cell_rect.right() - sz - 2, cell_rect.bottom() - sz - 2, sz, sz)

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

    def _show_cause_obj_popup(self, row, cause_id, global_pos, group_line=None):
        """A plain click on the ORS tag zone opens just a tag+type
        popup (2026-08-14, see NOTES.md). CauseTagPopup has no OK button
        (2026-08-18)
        — it commits live and dismisses itself on Escape/outside click,
        so it's shown non-modally instead of exec()'d."""
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
        old_id = equipment_ids[group_line]
        new_tag = (comp_tag or '').strip()
        selected_id = old_id
        selected_tag = ''
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
                    self.db.update_equipment_item(
                        old_id, new_tag, old_eq.get('prefix') or '',
                        old_eq.get('equipment_type') or comp_type,
                        old_eq.get('description') or '')
                    selected_tag = new_tag
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
        operator_match = re.search(r'\s(&|OR|<>|->|\+)\s',
                                   cause.get('comp_tag') or '', re.IGNORECASE)
        operator = ('&' if not operator_match or operator_match.group(1) == '+'
                    else ('OR' if operator_match.group(1).casefold() in ('<>', 'or')
                          else operator_match.group(1)))
        self.db.update_cause(
            cause_id,
            comp_type=(primary.get('equipment_type') if primary
                       else cause.get('comp_type') or comp_type),
            comp_tag=f" {operator} ".join(tags),
            equipment_id=primary_id,
            secondary_equipment_id=secondary_id,
            group_equipment_ids=equipment_ids)
        self._schedule_rebuild()

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
        self._schedule_rebuild()

    def _swap_group_objects(self, cause_id):
        """Swap the live primary/secondary P&ID links of a group cause."""
        cause = self.db.get_cause(cause_id)
        if not cause or not cause.get('secondary_equipment_id'):
            return
        primary = self.db.get_equipment_by_id(cause.get('equipment_id'))
        secondary = self.db.get_equipment_by_id(cause.get('secondary_equipment_id'))
        if not primary or not secondary:
            return
        operator_match = re.search(r'\s(&|OR|<>|->|\+)\s',
                                   cause.get('comp_tag') or '', re.IGNORECASE)
        operator = ('&' if not operator_match or operator_match.group(1) == '+'
                    else ('OR' if operator_match.group(1).casefold() in ('<>', 'or')
                          else operator_match.group(1)))
        self.db.update_cause(
            cause_id,
            equipment_id=secondary['id'],
            secondary_equipment_id=primary['id'],
            comp_type=secondary.get('equipment_type', ''),
            comp_tag=f"{secondary.get('tag', '')} {operator} {primary.get('tag', '')}",
            group_equipment_ids=(
                [self._group_equipment_ids(cause)[1],
                 self._group_equipment_ids(cause)[0]] +
                self._group_equipment_ids(cause)[2:]))
        self._schedule_rebuild()

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
                    self.db.update_equipment_item(
                        old_equipment_id, new_tag, old_eq.get('prefix') or '',
                        old_eq.get('equipment_type') or comp_type,
                        old_eq.get('description') or '')
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
            self.db.update_cause(cause_id, likelihood=f_level, base_frequency=None)
        else:
            self.db.update_cause(cause_id, base_frequency=numeric)
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
                    badge = self._plus_badge_geometry(cr)
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

        # Delegate inline editor (regular cell in edit mode)
        if (isinstance(obj, (_BoldTagTextEdit, QLineEdit)) and
                obj.property('editing_row') is not None and
                obj.property('sg_id') is None):
            if event.type() == QEvent.Type.KeyPress:
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
                    self._delegate.commitData.emit(obj)
                    self._delegate.closeEditor.emit(obj, QStyledItemDelegate.EndEditHint.NoHint)
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
        """Open the next blank recommendation box after an Enter commit."""
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

    def _add_cause_via_plus_row(self, deviation_id, global_pos=None):
        # ``global_pos`` is retained for mouse-event callers. There is no
        # dialog to position now that creation goes straight to inline edit.
        self._quick_add_cause(deviation_id)

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

    def undo_last_text_edit(self):
        """Restore the last committed HAZOP text edit (Ctrl+Z)."""
        if not self._text_undo_stack:
            return False
        kind, id_, old = self._text_undo_stack.pop()
        self._undoing_text = True
        try:
            if kind == 'cause':
                self.db.update_cause(id_, description=old)
            elif kind == 'consequence':
                c = self.db.get_consequence(id_)
                if c:
                    self.db.update_consequence(id_, old, c['severity'], c['category'] or '')
            elif kind == 'safeguard':
                s = self.db.get_safeguard(id_)
                if s:
                    self.db.update_safeguard(id_, old, s['rrf'] or 1)
            elif kind == 'recommendation':
                self.db.update_recommendation(id_, description=old)
            else:
                return False
        finally:
            self._undoing_text = False
        self._schedule_rebuild()
        self.item_edited.emit({'cause': CAUSE_T, 'consequence': CONS_T,
                               'safeguard': SG_T, 'recommendation': CONS_T}[kind], id_)
        return True

    def _on_cell_changed(self, row, col):
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
                old_desc = cause.get('description', '') or ''

                # If the edited text matches old_comp_tag, we're editing comp_tag
                # Otherwise, we're editing description
                if desc and old_comp_tag and old_comp_tag.strip() == text:
                    # User edited comp_tag
                    self.db.update_cause(id_, comp_tag=desc)
                else:
                    # User edited description
                    if not self._undoing_text and desc != old_desc:
                        self._text_undo_stack.append(('cause', id_, old_desc))
                    self.db.update_cause(id_, desc)
                    # Sync any OTHER row showing this same cause (span groups
                    # merge same-id rows visually, but each still has its own
                    # QTableWidgetItem) — no full rebuild needed, see
                    # _update_row_text_only()'s docstring for why.
                    self._update_row_text_only('cause', id_, desc)
                if bound_from_inline_tag:
                    self._schedule_rebuild()
            self.item_edited.emit(CAUSE_T, id_)

        elif kind == 'consequence':
            desc = text.split('\n')[0].strip()
            cons = self.db.get_consequence(id_)
            if cons:
                accepted, desc = self._confirm_inline_identity_change('consequence', id_, desc)
                if not accepted:
                    self._schedule_rebuild()
                    return
                old_desc = cons.get('description', '') or ''
                if not self._undoing_text and desc != old_desc:
                    self._text_undo_stack.append(('consequence', id_, old_desc))
                refs = parse_tag_refs(cons.get('tagged_refs') or '')
                for tag in self._matching_pid_tags(desc):
                    self._detached_tags.discard(tag.casefold())
                    if not any(ref.casefold() == tag.casefold() for ref in refs):
                        refs.append(tag)
                self.db.update_consequence(id_, desc, cons['severity'],
                                           cons['category'] or '',
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
                old_desc = sg.get('description', '') or ''
                if not self._undoing_text and desc != old_desc:
                    self._text_undo_stack.append(('safeguard', id_, old_desc))
                refs = parse_tag_refs(sg.get('tagged_refs') or '')
                for tag in self._matching_pid_tags(desc):
                    self._detached_tags.discard(tag.casefold())
                    if not any(ref.casefold() == tag.casefold() for ref in refs):
                        refs.append(tag)
                self.db.update_safeguard(id_, desc, sg['rrf'] or 1,
                                         tagged_refs=','.join(refs))
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
                rec = next((a for a in acts if a['id'] == rec_id), None)
                if rec and not self._undoing_text:
                    rec = dict(rec)
                    self._text_undo_stack.append(('recommendation', rec_id,
                                                  rec.get('description', '') or ''))
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
            return
        item = self._table.item(row, col)
        # A grouped cause has two distinct visual edit targets. Never open
        # the generic full-cell editor from F2, a context-menu action, or a
        # programmatic selection because it has no row context.
        if col == self._C_ORS and item is not None:
            group_tags = item.data(Qt.ItemDataRole.UserRole + 9) or []
            if len(group_tags) >= 2:
                return
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
        labels = {'cause': 'Orsak', 'cons': 'Konsekvens', 'sg': 'Barriär'}
        action = 'Kopiera' if is_copy_modifier else 'Flytta'
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
        entries = []
        seen = set()
        for index in self._table.selectedIndexes():
            if index.column() != col or not (0 <= index.row() < len(self._row_meta)):
                continue
            meta = self._row_meta[index.row()]
            candidate = (meta[3] if kind == 'sg' else
                         meta[1] if kind == 'cause' else meta[2])
            if candidate is None or candidate in seen:
                continue
            seen.add(candidate)
            entries.append((int(candidate), index.row()))
        if item_id not in seen:
            entries.insert(0, (int(item_id), row))
        return entries

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
        action = (Qt.DropAction.CopyAction if is_copy_modifier
                  else Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)
        drag.exec(action)

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

        try:
            item_id = int(item_id_s)
        except ValueError:
            return

        if kind == 'sg':
            if tgt_cons is None:
                event.ignore(); return
            changed = False
            for item_id, source_row in zip(item_ids, source_rows):
                if not (0 <= source_row < len(self._row_meta)):
                    continue
                if tgt_cons == self._row_meta[source_row][2]:
                    continue
                if is_copy:
                    self.db.copy_safeguard(item_id, tgt_cons)
                else:
                    self.db.move_safeguard(item_id, tgt_cons)
                changed = True
            if not changed:
                event.ignore(); return
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

        elif kind == 'cons':
            if tgt_cause is None:
                event.ignore(); return
            changed = False
            for item_id, source_row in zip(item_ids, source_rows):
                if not (0 <= source_row < len(self._row_meta)):
                    continue
                if tgt_cause == self._row_meta[source_row][1]:
                    continue
                if is_copy:
                    self.db.copy_consequence(item_id, tgt_cause)
                else:
                    self.db.move_consequence(item_id, tgt_cause)
                changed = True
            if not changed:
                event.ignore(); return
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

        elif kind == 'cause':
            if tgt_dev is None:
                event.ignore(); return
            changed = False
            for item_id, source_row in zip(item_ids, source_rows):
                if not (0 <= source_row < len(self._row_meta)):
                    continue
                if tgt_dev == self._row_meta[source_row][0]:
                    continue
                if is_copy:
                    self.db.copy_cause(item_id, tgt_dev)
                else:
                    self.db.move_cause_to_deviation(item_id, tgt_dev)
                changed = True
            if not changed:
                event.ignore(); return
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

        # ── Ctrl+C shortcut hint ────────────────────────────────────────
        copy_row = menu.addAction(_icon('clipboard'), "Kopiera rad  (Ctrl+C)")
        copy_row.triggered.connect(lambda: self._copy_row_to_clipboard(row))
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
