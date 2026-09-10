#!/usr/bin/env python3
"""Spell-checking core (2026-09-06, see NOTES.md "Stavningskontroll") --
the checker wrapper, tokenizer and exclusion rules shared by the
real-time highlighter and the manual "Kör stavningskontroll" dialog.

No Qt widgets live here (that starts in a later phase) -- just spylls
plus a `Database` for the project's own tag list and approved-word
dictionary, so this module is safe to import from any layer above
database.py without pulling in any panel-specific code.
"""

import hashlib
import json
import re
from functools import lru_cache

from spylls.hunspell import Dictionary

# Short codes used in the UI/app_config; maps to the hunspell dictionary
# name spylls bundles (spylls/hunspell/data/<lang>/<name>.{aff,dic}).
# spylls also bundles 'ru' -- not offered here since this app is
# Swedish/English only.
LANGUAGES = {
    'sv': 'sv_SE',
    'en': 'en_US',
}
DEFAULT_LANGUAGE = 'sv'

# Unicode-aware "word" token -- letters only (matches å/ä/ö and other
# accented letters via \W's own unicode awareness), digits/underscore
# excluded so a tag like "V-101" splits into "V" (checked, and almost
# always excluded via the known-tag list below) with the "101" simply
# never becoming a token at all.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def tokenize(text):
    """Return every word-like token in `text`, in order."""
    if not text:
        return []
    return _WORD_RE.findall(text)


@lru_cache(maxsize=None)
def _load_dictionary(hunspell_name):
    return Dictionary.from_files(hunspell_name)


class SpellChecker:
    """One dictionary for one language, plus the project's own known
    tags and approved user words -- both consulted BEFORE the
    dictionary lookup ever runs, so a known equipment tag or an
    approved custom word never even reaches spylls."""

    def __init__(self, language, known_tags=(), user_words=()):
        hunspell_name = LANGUAGES.get(language, LANGUAGES[DEFAULT_LANGUAGE])
        self._dictionary = _load_dictionary(hunspell_name)
        self._known_tags = {t.casefold() for t in known_tags if t}
        self._user_words = {w.casefold() for w in user_words if w}

    def is_known_tag_or_user_word(self, word):
        folded = word.casefold()
        return folded in self._known_tags or folded in self._user_words

    def add_user_word(self, word):
        """In-memory only -- callers also persist via
        Database.add_spellcheck_user_word so it survives a restart."""
        if word:
            self._user_words.add(word.casefold())

    def check(self, word):
        """True if `word` should be treated as correctly spelled."""
        if not word:
            return True
        if self.is_known_tag_or_user_word(word):
            return True
        return bool(self._dictionary.lookup(word))

    def suggest(self, word, limit=8):
        return list(self._dictionary.suggest(word))[:limit]

    def misspelled_ranges(self, text):
        """Yield (start, end, word) for every misspelled token in text,
        in order. `start`/`end` are Python string-slice offsets into
        `text` (end exclusive), matching re.Match.span()."""
        for match in _WORD_RE.finditer(text or ''):
            word = match.group(0)
            if not self.check(word):
                yield match.start(), match.end(), word


def make_checker(db, language=None):
    """Build a SpellChecker scoped to the current project: language
    from app_config (falls back to DEFAULT_LANGUAGE), known tags from
    Database.equipment_items(), user words from
    Database.spellcheck_user_words()."""
    language = language or db.get_config('spellcheck_language', DEFAULT_LANGUAGE)
    tags = [e['tag'] for e in db.equipment_items() if e['tag']]
    user_words = db.spellcheck_user_words()
    return SpellChecker(language, known_tags=tags, user_words=user_words)


# ══════════════════════════════════════════════════════════════════════════════
# Qt-facing pieces (Fas 2) -- real-time underlining + right-click suggestions.
# ══════════════════════════════════════════════════════════════════════════════

from PyQt6.QtCore import Qt, QEvent, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import QLineEdit, QMenu

from constants import MARKUP_COLORS

SPELLCHECK_UNDERLINE_COLOR = MARKUP_COLORS[0]   # '#E53935', the app's standard red
_MAX_SUGGESTIONS_SHOWN = 6


class SpellCheckContext:
    """Shared per-open-project spellcheck state. One instance lives on
    MainWindow; every widget attached via `attach_spellcheck`/
    `SpellCheckLineEdit(context=...)` registers itself here, so toggling
    on/off (Redigera-menyn) or rebuilding the checker (tag list, user
    dictionary, or language changed) refreshes every attached widget at
    once instead of each widget tracking this independently."""

    def __init__(self, db):
        self.db = db
        self.enabled = db.get_config('spellcheck_enabled', '1') == '1'
        self.language = db.get_config('spellcheck_language', DEFAULT_LANGUAGE)
        # "Ignorera" (persist=False) words: not written to the DB, so a
        # fresh make_checker() call would never know about them -- kept
        # here separately and re-applied by _build_checker() on every
        # refresh(), instead of relying on mutating a checker object
        # that refresh() is about to throw away and rebuild anyway.
        self._session_words = set()
        self.checker = self._build_checker() if self.enabled else None
        self._cache_context_key = self._make_cache_context_key()
        self._highlighters = []       # SpellCheckHighlighter instances
        self._paint_widgets = []       # SpellCheckLineEdit instances
        self._repaint_targets = []     # static (non-editing) table/label widgets
        # text -> list[(start,end,word)], memoizes misspelled_ranges_cached()
        # for the STATIC paint path -- cleared whenever refresh() rebuilds
        # the checker (2026-09-07: a single misspelled_ranges() call was
        # measured at ~15ms for one sentence, and paint() runs far more
        # often per second than editing ever does -- every scroll/hover/
        # selection repaint across a table's many visible rows -- so an
        # uncached call there would be visibly janky).
        self._ranges_cache = {}

    def _build_checker(self):
        checker = make_checker(self.db)
        for word in self._session_words:
            checker.add_user_word(word)
        return checker

    def _make_cache_context_key(self):
        tags = sorted(
            str(e['tag']).casefold() for e in self.db.equipment_items()
            if e['tag'])
        words = sorted(
            {str(word).casefold() for word in self.db.spellcheck_user_words()
             if word} | self._session_words)
        payload = json.dumps(
            {'engine': 'spylls', 'language': self.language,
             'tags': tags, 'words': words},
            ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def cached_suggestions(self, word):
        raw = self.db.get_spellcheck_suggestions(
            self.language, self._cache_context_key, word)
        if raw is None:
            return None
        try:
            return list(json.loads(raw))
        except (TypeError, ValueError):
            return None

    def cache_suggestions(self, word, suggestions):
        self.db.save_spellcheck_suggestions(
            self.language, self._cache_context_key, word,
            json.dumps(list(suggestions), ensure_ascii=False))

    def refresh(self):
        """Call after the user dictionary, equipment tags, or language
        setting changes (or on/off is toggled) -- rebuilds the checker
        and repaints every attached widget."""
        self.checker = self._build_checker() if self.enabled else None
        self.language = self.db.get_config('spellcheck_language', DEFAULT_LANGUAGE)
        self._cache_context_key = self._make_cache_context_key()
        self._ranges_cache = {}
        still_alive = []
        for hl in self._highlighters:
            try:
                hl.rehighlight()
                still_alive.append(hl)
            except RuntimeError:
                pass   # underlying QTextDocument's widget was deleted
        self._highlighters = still_alive
        still_alive = []
        for w in self._paint_widgets:
            try:
                w.update()
                still_alive.append(w)
            except RuntimeError:
                pass
        self._paint_widgets = still_alive
        still_alive = []
        for w in self._repaint_targets:
            try:
                w.update()
                still_alive.append(w)
            except RuntimeError:
                pass
        self._repaint_targets = still_alive

    def misspelled_ranges_cached(self, text):
        """Cached wrapper around checker.misspelled_ranges() for the
        STATIC (non-editing) paint path -- see the cache's own comment
        in __init__ for why this matters. Keyed by exact text, since a
        cell's saved text changes rarely between the many repaints that
        happen while it's simply on screen."""
        if self.checker is None:
            return []
        cached = self._ranges_cache.get(text)
        if cached is None:
            cached = list(self.checker.misspelled_ranges(text))
            self._ranges_cache[text] = cached
        return cached

    def _register_repaint_target(self, widget):
        self._repaint_targets.append(widget)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.db.set_config('spellcheck_enabled', '1' if self.enabled else '0')
        self.refresh()

    def add_user_word(self, word, persist=True):
        """`persist=False` is "Ignorera" (this session only); `persist=True`
        is "Lägg till i ordlista" (survives a restart)."""
        if not word:
            return
        if persist:
            self.db.add_spellcheck_user_word(word)
        else:
            self._session_words.add(word.casefold())
        self.refresh()

    def _register_highlighter(self, highlighter):
        self._highlighters.append(highlighter)

    def _register_paint_widget(self, widget):
        self._paint_widgets.append(widget)


def _spellcheck_underline_format():
    fmt = QTextCharFormat()
    fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
    fmt.setUnderlineColor(QColor(SPELLCHECK_UNDERLINE_COLOR))
    return fmt


class SpellCheckHighlighter(QSyntaxHighlighter):
    """Real-time squiggly underline for a QTextEdit-based widget
    (including `_BoldTagTextEdit`, ui_helpers.py). Coexists safely with
    that class's own manual bold-tag QTextCursor formatting -- verified
    empirically: a QSyntaxHighlighter's setFormat() calls compose with a
    block's separately-applied cursor char format instead of replacing
    it, since Qt renders them as independent, merged layers."""

    def __init__(self, document, context):
        super().__init__(document)
        self._context = context

    def highlightBlock(self, text):
        checker = self._context.checker
        if checker is None:
            return
        fmt = _spellcheck_underline_format()
        for start, end, _word in checker.misspelled_ranges(text):
            self.setFormat(start, end - start, fmt)


def _menu_action_replace_range(menu, label, on_pick):
    action = menu.addAction(label)
    action.triggered.connect(on_pick)
    return action


def _build_suggestion_menu(parent_menu, checker, word, on_replace):
    """Populate `parent_menu` with up to _MAX_SUGGESTIONS_SHOWN
    suggestions for `word`, a separator, then "Lägg till i ordlista"/
    "Ignorera". `on_replace(suggestion)` performs the actual text
    substitution -- the only part that differs between a QTextEdit and
    a QLineEdit host. Returns nothing; mutates parent_menu in place."""
    suggestions = checker.suggest(word, limit=_MAX_SUGGESTIONS_SHOWN)
    if suggestions:
        for suggestion in suggestions:
            _menu_action_replace_range(
                parent_menu, suggestion, lambda _=False, s=suggestion: on_replace(s))
    else:
        no_suggestions = parent_menu.addAction("(inga förslag)")
        no_suggestions.setEnabled(False)
    parent_menu.addSeparator()


class _SpellCheckTextEditMenuFilter(QObject):
    """Installed on a QTextEdit-based widget: right-click on a misspelled
    word prepends suggestions + "Lägg till i ordlista"/"Ignorera" to the
    widget's normal context menu."""

    def __init__(self, widget, context, parent=None):
        super().__init__(parent or widget)
        self._widget = widget
        self._context = context

    def eventFilter(self, obj, event):
        if obj is self._widget and event.type() == QEvent.Type.ContextMenu:
            self._show_menu(event)
            return True
        return False

    def _misspelled_range_at(self, local_pos):
        checker = self._context.checker
        if checker is None:
            return None
        widget = self._widget
        char_pos = widget.cursorPositionAt(local_pos)
        text = widget.toPlainText()
        for start, end, word in checker.misspelled_ranges(text):
            if start <= char_pos < end:
                return start, end, word
        return None

    def _show_menu(self, event):
        widget = self._widget
        checker = self._context.checker
        pos = event.pos()
        hit = self._misspelled_range_at(pos) if checker is not None else None
        menu = widget.createStandardContextMenu()
        if hit is not None:
            start, end, word = hit
            def _replace(suggestion, start=start, end=end):
                cursor = widget.textCursor()
                cursor.setPosition(start)
                cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
                cursor.insertText(suggestion)
            # Scratch container: just collects the prepended actions --
            # never shown itself, only its .actions() are transplanted
            # into `menu` below (same technique SpellCheckLineEdit's own
            # contextMenuEvent uses).
            top = QMenu(menu)
            _build_suggestion_menu(top, checker, word, _replace)
            add_action = top.addAction("Lägg till i ordlista")
            add_action.triggered.connect(
                lambda: self._context.add_user_word(word, persist=True))
            ignore_action = top.addAction("Ignorera")
            ignore_action.triggered.connect(
                lambda: self._context.add_user_word(word, persist=False))
            first_existing = menu.actions()[0] if menu.actions() else None
            for action in reversed(top.actions()):
                menu.insertAction(first_existing, action)
            if first_existing is not None:
                menu.insertSeparator(first_existing)
        menu.exec(event.globalPos())


def attach_spellcheck(text_edit, context):
    """Wire real-time spell-check underlining + right-click suggestions
    onto an existing QTextEdit (including `_BoldTagTextEdit`). Safe to
    call once per widget; both the highlighter and the context-menu
    filter are parented to the widget, so they're cleaned up
    automatically with it."""
    highlighter = SpellCheckHighlighter(text_edit.document(), context)
    context._register_highlighter(highlighter)
    menu_filter = _SpellCheckTextEditMenuFilter(text_edit, context)
    text_edit.installEventFilter(menu_filter)
    # Belt-and-suspenders against the filter being garbage-collected --
    # QObject parent/child ownership already keeps it alive, but every
    # other event-filter installation in this codebase also stashes a
    # direct attribute reference (see tree_panel.py/scenario_panel.py).
    text_edit._spellcheck_menu_filter = menu_filter
    return highlighter


class SpellCheckLineEdit(QLineEdit):
    """A QLineEdit that draws a red squiggly underline under misspelled
    words and offers the same right-click suggestions as
    `attach_spellcheck` -- QSyntaxHighlighter only works on a
    QTextDocument (QTextEdit), so a single-line field needs its own
    small paint overlay instead. Positions are computed via
    cursorRect()'s own calibration rather than reimplementing QLineEdit's
    internal margin/scroll handling, so it stays correct even while the
    field is scrolled past its visible width."""

    def __init__(self, *args, context=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._spellcheck_context = context
        if context is not None:
            context._register_paint_widget(self)

    def _char_x(self, pos, anchor_pos, anchor_x, fm, text):
        if pos == anchor_pos:
            return anchor_x
        if pos > anchor_pos:
            return anchor_x + fm.horizontalAdvance(text[anchor_pos:pos])
        return anchor_x - fm.horizontalAdvance(text[pos:anchor_pos])

    def _char_pos_at_x(self, x):
        """Inverse of _char_x: nearest character offset for a given
        widget-local x. Fields are short, so a linear scan is cheap."""
        text = self.text()
        anchor_pos = self.cursorPosition()
        anchor_x = self.cursorRect().left()
        fm = self.fontMetrics()
        best_pos, best_dist = 0, None
        for pos in range(len(text) + 1):
            px = self._char_x(pos, anchor_pos, anchor_x, fm, text)
            dist = abs(x - px)
            if best_dist is None or dist < best_dist:
                best_dist, best_pos = dist, pos
        return best_pos

    def _misspelled_range_at_x(self, x):
        context = self._spellcheck_context
        if context is None or context.checker is None:
            return None
        char_pos = self._char_pos_at_x(x)
        text = self.text()
        for start, end, word in context.checker.misspelled_ranges(text):
            if start <= char_pos < end:
                return start, end, word
        return None

    def paintEvent(self, event):
        super().paintEvent(event)
        context = self._spellcheck_context
        if context is None or context.checker is None:
            return
        text = self.text()
        ranges = list(context.checker.misspelled_ranges(text))
        if not ranges:
            return
        anchor_pos = self.cursorPosition()
        anchor_x = self.cursorRect().left()
        fm = self.fontMetrics()
        y = self.cursorRect().bottom() - 1
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QPen(QColor(SPELLCHECK_UNDERLINE_COLOR), 1))
            for start, end, _word in ranges:
                x1 = self._char_x(start, anchor_pos, anchor_x, fm, text)
                x2 = self._char_x(end, anchor_pos, anchor_x, fm, text)
                painter.drawLine(x1, y, x2, y)
        finally:
            painter.end()

    def contextMenuEvent(self, event):
        context = self._spellcheck_context
        checker = context.checker if context is not None else None
        hit = self._misspelled_range_at_x(event.pos().x()) if checker is not None else None
        menu = self.createStandardContextMenu()
        if hit is not None:
            start, end, word = hit
            def _replace(suggestion, start=start, end=end):
                self.setSelection(start, end - start)
                self.insert(suggestion)
            top = QMenu(menu)
            _build_suggestion_menu(top, checker, word, _replace)
            add_action = top.addAction("Lägg till i ordlista")
            add_action.triggered.connect(lambda: context.add_user_word(word, persist=True))
            ignore_action = top.addAction("Ignorera")
            ignore_action.triggered.connect(lambda: context.add_user_word(word, persist=False))
            first_existing = menu.actions()[0] if menu.actions() else None
            for action in reversed(top.actions()):
                menu.insertAction(first_existing, action)
            if first_existing is not None:
                menu.insertSeparator(first_existing)
        menu.exec(event.globalPos())


# ══════════════════════════════════════════════════════════════════════════════
# Fas 4 -- "Kör stavningskontroll": manual whole-study walkthrough.
# ══════════════════════════════════════════════════════════════════════════════

import html as _html

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QTextEdit, QVBoxLayout,
)

# Same set of fields Fas 2's real-time underlining covers (node name/
# description, cause/consequence/safeguard/recommendation text, cause
# comments) -- P&ID-ref/media/tryck/temperatur/ansvarig etc. stay out of
# scope here too, per the plan's explicit exclusions.
_FIELD_KINDS = (
    'node_name', 'node_desc', 'cause_desc', 'cause_comment',
    'cons_desc', 'sg_desc', 'rec_desc',
)


def _apply_node_name(db, id_, new_text):
    row = db.get_node(id_)
    if not row:
        return
    db.update_node(id_, new_text, row.get('description', '') or '',
                   row.get('pid_ref', '') or '', row.get('media', '') or '',
                   row.get('pressure', '') or '', row.get('temperature', '') or '')


def _apply_node_desc(db, id_, new_text):
    row = db.get_node(id_)
    if not row:
        return
    db.update_node(id_, row.get('name', '') or '', new_text,
                   row.get('pid_ref', '') or '', row.get('media', '') or '',
                   row.get('pressure', '') or '', row.get('temperature', '') or '')


def _apply_cause_desc(db, id_, new_text):
    db.update_cause(id_, description=new_text)


def _apply_cause_comment(db, id_, new_text):
    db.set_cause_comment(id_, new_text)


def _apply_cons_desc(db, id_, new_text):
    cons = db.get_consequence(id_)
    if not cons:
        return
    db.update_consequence(id_, new_text, cons['severity'],
                          cons.get('category', '') or '',
                          cons.get('consequence_chain', '') or '')


def _apply_sg_desc(db, id_, new_text):
    db.update_safeguard(id_, description=new_text)


def _apply_rec_desc(db, id_, new_text):
    db.update_recommendation(id_, description=new_text)


_APPLY_FUNCS = {
    'node_name': _apply_node_name,
    'node_desc': _apply_node_desc,
    'cause_desc': _apply_cause_desc,
    'cause_comment': _apply_cause_comment,
    'cons_desc': _apply_cons_desc,
    'sg_desc': _apply_sg_desc,
    'rec_desc': _apply_rec_desc,
}


def _collect_project_text_fields(db):
    """Walk the whole open study (every node/deviation/cause/
    consequence/safeguard/recommendation) and return a flat list of
    {'kind', 'id', 'label', 'text'} dicts to spellcheck -- one per
    non-blank field, in the same batched-fetch style tree_panel.py's
    own full-tree walk uses (nodes()/deviations_for_nodes()/
    causes_for_deviations()/...) so this stays a handful of queries
    instead of one per row.

    A consequence with a non-empty `consequence_chain` is skipped: its
    `description` is derived/managed by the chain editor, not raw typed
    prose, and a piecemeal word-replacement here could fight with that
    feature -- out of scope for this dialog.

    A recommendation shared by several consequences (consequence_
    recommendations is a real N:M link) is included only once.
    """
    fields = []
    nodes = db.nodes()
    node_ids = [n['id'] for n in nodes]
    devs_by_node = db.deviations_for_nodes(node_ids)
    all_dev_ids = [d['id'] for devs in devs_by_node.values() for d in devs]
    causes_by_dev = db.causes_for_deviations(all_dev_ids)
    all_cause_ids = [c['id'] for causes in causes_by_dev.values() for c in causes]
    cons_by_cause = db.consequences_for_causes(all_cause_ids)
    all_cons_ids = [c['id'] for conss in cons_by_cause.values() for c in conss]
    sgs_by_cons = db.safeguards_for_consequences(all_cons_ids)
    recs_by_cons = db.recommendations_for_consequences(all_cons_ids)
    seen_rec_ids = set()

    for node in nodes:
        node_id = node['id']
        node_name = node['name'] or ''
        if node_name.strip():
            fields.append({'kind': 'node_name', 'id': node_id,
                           'label': "Nod – Namn", 'text': node_name})
        node_desc = node['description'] or ''
        if node_desc.strip():
            fields.append({'kind': 'node_desc', 'id': node_id,
                           'label': f"Nod ({node_name}) – Beskrivning",
                           'text': node_desc})
        for dev in devs_by_node.get(node_id, []):
            for cause in causes_by_dev.get(dev['id'], []):
                cause_id = cause['id']
                cause_desc = cause['description'] or ''
                if cause_desc.strip():
                    fields.append({'kind': 'cause_desc', 'id': cause_id,
                                   'label': f"Orsak ({node_name}) – Beskrivning",
                                   'text': cause_desc})
                comment = db.get_cause_comment(cause_id) or ''
                if comment.strip():
                    fields.append({'kind': 'cause_comment', 'id': cause_id,
                                   'label': f"Orsak ({node_name}) – Kommentar",
                                   'text': comment})
                for cons in cons_by_cause.get(cause_id, []):
                    cons_id = cons['id']
                    if (cons['consequence_chain'] or '').strip():
                        continue
                    cons_desc = cons['description'] or ''
                    if cons_desc.strip():
                        fields.append({'kind': 'cons_desc', 'id': cons_id,
                                       'label': f"Konsekvens ({node_name}) – Beskrivning",
                                       'text': cons_desc})
                    for sg in sgs_by_cons.get(cons_id, []):
                        sg_desc = sg['description'] or ''
                        if sg_desc.strip():
                            fields.append({'kind': 'sg_desc', 'id': sg['id'],
                                           'label': f"Barriär ({node_name}) – Beskrivning",
                                           'text': sg_desc})
                    for rec in recs_by_cons.get(cons_id, []):
                        if rec['id'] in seen_rec_ids:
                            continue
                        seen_rec_ids.add(rec['id'])
                        rec_desc = rec['description'] or ''
                        if rec_desc.strip():
                            fields.append({'kind': 'rec_desc', 'id': rec['id'],
                                           'label': f"Rekommendation ({node_name}) – Beskrivning",
                                           'text': rec_desc})
    return fields


class _SuggestWorker(QThread):
    """Computes spylls suggestions for one word off the UI thread
    (2026-09-06, Anton: "det tar lite lång tid emellan varje" -- spylls'
    Hunspell-suggestion algorithm is pure Python and genuinely slow,
    measured 200ms-5s+ for a single Swedish compound word, which
    previously froze the whole review dialog between every Ändra/
    Ignorera click). Modelled on pid_viewer.py's EquipmentTagSearchWorker
    -- always emits finished_suggest exactly once, even on failure, so
    the caller's UI state can never hang waiting for it."""
    finished_suggest = pyqtSignal(str, list)   # word, suggestions

    def __init__(self, checker, word, limit=8, parent=None):
        super().__init__(parent)
        self._checker = checker
        self._word = word
        self._limit = limit

    def run(self):
        try:
            suggestions = self._checker.suggest(self._word, limit=self._limit)
        except Exception:
            suggestions = []
        self.finished_suggest.emit(self._word, suggestions)


class SpellCheckReviewDialog(QDialog):
    """Redigera > "Kör stavningskontroll…" -- steps through every
    misspelled word across the whole open study, offering the same
    Ändra / Ändra alla / Ignorera / Ignorera alla / Lägg till i ordlista
    choices a standard word-processor spellchecker gives. Each accepted
    change is written straight to the project (through the same
    Database.update_* methods every other editor in this app uses, so
    it's tied into undo/redo the normal way) as soon as it's accepted --
    closing early ("Avbryt") keeps whatever was already changed.
    """

    def __init__(self, db, context, parent=None):
        super().__init__(parent)
        self.db = db
        self.context = context
        self.setWindowTitle("Kör stavningskontroll")
        self.setMinimumWidth(440)

        self._fields = _collect_project_text_fields(db)
        self._field_idx = 0
        self._always_replace = {}          # word.casefold() -> replacement, rest of run
        self._always_ignore = set()         # word.casefold(), rest of run
        # (field index, word.casefold()) -> how many of ITS occurrences in
        # that one field have already been individually ignored. Recomputed
        # fresh on every pass, so this is only wrong if an EARLIER
        # occurrence of the exact same word in the exact same field is
        # itself edited later in the run (renumbering the ones after it) --
        # a narrow, documented edge case, not a data-safety issue: the
        # worst outcome is being asked about an already-ignored occurrence
        # a second time.
        self._ignored_in_field = {}
        self._current = None                # (field_dict, start, end, word)
        self._changed_count = 0
        # word.casefold() -> suggestions, so re-encountering the same
        # misspelled word later in the same run (typos/jargon repeated
        # across a HAZOP study are common) never re-runs spylls' slow
        # suggestion algorithm a second time.
        self._suggestion_cache = {}
        self._scheduled_suggestions = set()
        # Keeps in-flight _SuggestWorker instances referenced (same
        # pattern as pid_panel_mod.py's _tag_search_workers) so they're
        # never garbage-collected mid-computation; removed once each
        # finishes. A worker still running when the dialog closes is
        # simply abandoned -- its result is cached but never applied to
        # a UI that's gone (see _on_suggestions_ready's RuntimeError guard).
        self._suggest_workers = []

        lay = QVBoxLayout(self)
        self._label = QLabel()
        self._label.setWordWrap(True)
        lay.addWidget(self._label)

        self._context_view = QTextEdit()
        self._context_view.setReadOnly(True)
        self._context_view.setFixedHeight(80)
        lay.addWidget(self._context_view)

        self._suggestion_list = QListWidget()
        self._suggestion_list.itemDoubleClicked.connect(
            lambda _item: self._on_change_clicked())
        lay.addWidget(self._suggestion_list)

        row1 = QHBoxLayout()
        self._change_btn = QPushButton("Ändra")
        self._change_btn.clicked.connect(self._on_change_clicked)
        self._change_all_btn = QPushButton("Ändra alla")
        self._change_all_btn.clicked.connect(self._on_change_all_clicked)
        row1.addWidget(self._change_btn)
        row1.addWidget(self._change_all_btn)
        lay.addLayout(row1)

        custom_row = QHBoxLayout()
        self._custom_edit = QLineEdit()
        self._custom_edit.setPlaceholderText("Skriv valfri ersättning")
        self._custom_edit.textChanged.connect(self._update_custom_buttons)
        custom_row.addWidget(self._custom_edit, 1)
        self._custom_change_btn = QPushButton("Ändra med egen text")
        self._custom_change_btn.clicked.connect(self._on_custom_change_clicked)
        self._custom_change_all_btn = QPushButton("Ändra alla med egen text")
        self._custom_change_all_btn.clicked.connect(
            self._on_custom_change_all_clicked)
        custom_row.addWidget(self._custom_change_btn)
        custom_row.addWidget(self._custom_change_all_btn)
        lay.addLayout(custom_row)

        row2 = QHBoxLayout()
        self._ignore_btn = QPushButton("Ignorera")
        self._ignore_btn.clicked.connect(self._on_ignore_clicked)
        self._ignore_all_btn = QPushButton("Ignorera alla")
        self._ignore_all_btn.clicked.connect(self._on_ignore_all_clicked)
        self._add_dict_btn = QPushButton("Lägg till i ordlista")
        self._add_dict_btn.clicked.connect(self._on_add_to_dictionary_clicked)
        row2.addWidget(self._ignore_btn)
        row2.addWidget(self._ignore_all_btn)
        row2.addWidget(self._add_dict_btn)
        lay.addLayout(row2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        lay.addWidget(buttons)

        self._advance()
        self._prefetch_suggestions()

    def _prefetch_suggestions(self):
        """Warm the persistent cache for a few distinct misspelled words.

        The workers keep the dialog responsive and make later occurrences
        nearly instant.  A small cap avoids creating many CPU-heavy spylls
        threads for a large project; spylls itself is pure Python, so an
        unlimited number of threads would mostly add contention.
        """
        checker = self.context.checker
        if checker is None:
            return
        words = []
        seen = set()
        for field in self._fields:
            for _start, _end, word in checker.misspelled_ranges(field['text']):
                key = word.casefold()
                if key in seen or self.context.cached_suggestions(word) is not None:
                    continue
                seen.add(key)
                words.append(word)
        for word in words[:4]:
            self._start_suggest_worker(word)

    # ── Word-by-word walk ────────────────────────────────────────────────
    def _advance(self):
        """Find the next actionable misspelled word (skipping ones
        already resolved via Ändra alla/Ignorera alla/this-field's own
        Ignorera count) and present it, or show a finishing message once
        every field has been fully checked."""
        checker = self.context.checker
        if checker is None:
            self._show_finished("Stavningskontroll är avstängd.")
            return
        while self._field_idx < len(self._fields):
            field = self._fields[self._field_idx]
            text = field['text']
            seen_counts = {}
            found = None
            for start, end, word in checker.misspelled_ranges(text):
                folded = word.casefold()
                seen_counts[folded] = seen_counts.get(folded, 0) + 1
                if folded in self._always_ignore:
                    continue
                occurrence = seen_counts[folded]
                already_ignored = self._ignored_in_field.get(
                    (self._field_idx, folded), 0)
                if occurrence <= already_ignored:
                    continue
                if folded in self._always_replace:
                    replacement = self._always_replace[folded]
                    field['text'] = text[:start] + replacement + text[end:]
                    self._write_back(field)
                    self._advance()
                    return
                found = (start, end, word)
                break
            if found is None:
                self._field_idx += 1
                continue
            self._present(field, *found)
            return
        self._show_finished(
            f"Klar! Ingen mer felstavning hittades. "
            f"{self._changed_count} ändring"
            f"{'ar' if self._changed_count != 1 else ''} gjordes.")

    def _present(self, field, start, end, word):
        self._current = (field, start, end, word)
        self._custom_edit.clear()
        text = field['text']
        ctx_start = max(0, start - 60)
        ctx_end = min(len(text), end + 60)
        before = _html.escape(text[ctx_start:start])
        after = _html.escape(text[end:ctx_end])
        highlighted = _html.escape(word)
        self._label.setText(f"<b>{_html.escape(field['label'])}:</b>")
        self._context_view.setHtml(
            f"{before}<span style='background:#FFECEC;"
            f"text-decoration:underline;text-decoration-color:"
            f"{SPELLCHECK_UNDERLINE_COLOR};font-weight:bold;'>"
            f"{highlighted}</span>{after}")

        cached = self._suggestion_cache.get(word.casefold())
        if cached is None:
            cached = self.context.cached_suggestions(word)
            if cached is not None:
                self._suggestion_cache[word.casefold()] = cached
        if cached is not None:
            self._apply_suggestions(word, cached)
        else:
            self._suggestion_list.clear()
            placeholder = QListWidgetItem("Beräknar förslag…")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._suggestion_list.addItem(placeholder)
            self._change_btn.setEnabled(False)
            self._change_all_btn.setEnabled(False)
            self._start_suggest_worker(word)

    def _start_suggest_worker(self, word):
        key = word.casefold()
        if key in self._scheduled_suggestions:
            return
        self._scheduled_suggestions.add(key)
        worker = _SuggestWorker(self.context.checker, word, parent=self)
        self._suggest_workers.append(worker)
        worker.finished_suggest.connect(self._on_suggestions_ready)
        worker.start()

    def _on_suggestions_ready(self, word, suggestions):
        self._suggestion_cache[word.casefold()] = suggestions
        try:
            self.context.cache_suggestions(word, suggestions)
        except Exception:
            # A project may be closed while a background suggestion finishes;
            # the in-memory result remains usable for the current dialog.
            pass
        worker = self.sender()
        if worker in self._suggest_workers:
            self._suggest_workers.remove(worker)
        try:
            current_word = self._current[3] if self._current is not None else None
            if current_word is not None and current_word.casefold() == word.casefold():
                self._apply_suggestions(word, suggestions)
        except RuntimeError:
            pass   # dialog already closed -- nothing left to update

    def _apply_suggestions(self, word, suggestions):
        self._suggestion_list.clear()
        if suggestions:
            for suggestion in suggestions:
                self._suggestion_list.addItem(suggestion)
            self._suggestion_list.setCurrentRow(0)
        else:
            no_suggestions = QListWidgetItem("(inga förslag)")
            no_suggestions.setFlags(Qt.ItemFlag.NoItemFlags)
            self._suggestion_list.addItem(no_suggestions)
        has_suggestions = bool(suggestions)
        self._change_btn.setEnabled(has_suggestions)
        self._change_all_btn.setEnabled(has_suggestions)
        self._update_custom_buttons()

    def _update_custom_buttons(self):
        enabled = self._current is not None and bool(self._custom_edit.text().strip())
        self._custom_change_btn.setEnabled(enabled)
        self._custom_change_all_btn.setEnabled(enabled)

    def _show_finished(self, message):
        self._current = None
        self._label.setText(message)
        self._context_view.clear()
        self._suggestion_list.clear()
        for btn in (self._change_btn, self._change_all_btn, self._ignore_btn,
                    self._ignore_all_btn, self._add_dict_btn,
                    self._custom_change_btn, self._custom_change_all_btn):
            btn.setEnabled(False)
        self._custom_edit.clear()

    def _write_back(self, field):
        apply_fn = _APPLY_FUNCS.get(field['kind'])
        if apply_fn is not None:
            apply_fn(self.db, field['id'], field['text'])
        self._changed_count += 1

    # ── Button handlers ──────────────────────────────────────────────────
    def _on_change_clicked(self):
        self._apply_replacement(remember=False)

    def _on_change_all_clicked(self):
        self._apply_replacement(remember=True)

    def _on_custom_change_clicked(self):
        self._apply_custom_replacement(remember=False)

    def _on_custom_change_all_clicked(self):
        self._apply_custom_replacement(remember=True)

    def _apply_custom_replacement(self, remember):
        if self._current is None:
            return
        replacement = self._custom_edit.text().strip()
        if not replacement:
            return
        field, start, end, word = self._current
        if remember:
            self._always_replace[word.casefold()] = replacement
        field['text'] = field['text'][:start] + replacement + field['text'][end:]
        self._write_back(field)
        self._custom_edit.clear()
        self._advance()

    def _apply_replacement(self, remember):
        if self._current is None:
            return
        item = self._suggestion_list.currentItem()
        if item is None:
            return
        replacement = item.text()
        field, start, end, word = self._current
        if remember:
            self._always_replace[word.casefold()] = replacement
        field['text'] = field['text'][:start] + replacement + field['text'][end:]
        self._write_back(field)
        self._advance()

    def _on_ignore_clicked(self):
        if self._current is None:
            return
        _field, _start, _end, word = self._current
        key = (self._field_idx, word.casefold())
        self._ignored_in_field[key] = self._ignored_in_field.get(key, 0) + 1
        self._advance()

    def _on_ignore_all_clicked(self):
        if self._current is None:
            return
        _field, _start, _end, word = self._current
        self._always_ignore.add(word.casefold())
        self._advance()

    def _on_add_to_dictionary_clicked(self):
        if self._current is None:
            return
        _field, _start, _end, word = self._current
        self.context.add_user_word(word, persist=True)
        self._advance()
