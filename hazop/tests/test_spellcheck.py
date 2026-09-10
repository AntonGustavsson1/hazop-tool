#!/usr/bin/env python3
"""Tests for spellcheck.py (2026-09-06, see NOTES.md "Stavningskontroll").

The first classes (Tokenize/SpellChecker/MakeChecker/DatabaseUserDictionary)
are pure logic, no Qt/QApplication needed. The later classes cover the
Qt-facing Fas 2 pieces: SpellCheckContext, the QTextEdit highlighter,
SpellCheckLineEdit, and the right-click suggestion menu."""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from database import Database  # noqa: E402
import spellcheck  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _ensure_qapp():
    """Same one-QApplication-per-process helper every other test file
    uses (test_helpers.py) -- redefined locally here so this file can
    stay decoupled from the heavy `import hazop` that helper otherwise
    drags in, matching this module's own "pure logic + small Qt
    widgets" scope."""
    return QApplication.instance() or QApplication([])


class TokenizeTests(unittest.TestCase):
    def test_splits_on_whitespace_and_punctuation(self):
        self.assertEqual(
            spellcheck.tokenize("Ventilen felar öppen, se V-101."),
            ["Ventilen", "felar", "öppen", "se", "V"])

    def test_swedish_letters_stay_in_one_token(self):
        self.assertEqual(spellcheck.tokenize("stavningskontroll"),
                         ["stavningskontroll"])
        self.assertEqual(spellcheck.tokenize("Åke är på gången"),
                         ["Åke", "är", "på", "gången"])

    def test_digits_never_become_part_of_a_token(self):
        # "V-101" splits into just "V" -- the digits are dropped entirely,
        # never checked against the dictionary at all.
        self.assertEqual(spellcheck.tokenize("V-101"), ["V"])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(spellcheck.tokenize(""), [])
        self.assertEqual(spellcheck.tokenize(None), [])


class SpellCheckerTests(unittest.TestCase):
    def test_correct_swedish_word_passes(self):
        checker = spellcheck.SpellChecker('sv')
        self.assertTrue(checker.check('ventil'))

    def test_misspelled_swedish_word_fails_with_a_suggestion(self):
        checker = spellcheck.SpellChecker('sv')
        self.assertFalse(checker.check('stavningskontrol'))
        self.assertIn('stavningskontroll', checker.suggest('stavningskontrol'))

    def test_correct_english_word_passes_for_en_language(self):
        checker = spellcheck.SpellChecker('en')
        self.assertTrue(checker.check('hello'))
        self.assertFalse(checker.check('helo'))

    def test_unknown_language_falls_back_to_default(self):
        checker = spellcheck.SpellChecker('fr')
        self.assertTrue(checker.check('ventil'))   # sv_SE is DEFAULT_LANGUAGE

    def test_known_tag_is_never_flagged_even_if_not_a_real_word(self):
        checker = spellcheck.SpellChecker('sv', known_tags=['XyzNotAWord123'])
        self.assertTrue(checker.check('XyzNotAWord123'))
        self.assertTrue(checker.check('xyznotaword123'),
            "tag matching must be case-insensitive")

    def test_user_word_is_never_flagged(self):
        checker = spellcheck.SpellChecker('sv', user_words=['Nodbeskrivning'])
        self.assertTrue(checker.check('Nodbeskrivning'))
        self.assertTrue(checker.check('nodbeskrivning'))

    def test_add_user_word_takes_effect_immediately_in_memory(self):
        checker = spellcheck.SpellChecker('sv')
        self.assertFalse(checker.check('Kvarggmuffel'))
        checker.add_user_word('Kvarggmuffel')
        self.assertTrue(checker.check('Kvarggmuffel'))

    def test_empty_word_is_never_flagged(self):
        checker = spellcheck.SpellChecker('sv')
        self.assertTrue(checker.check(''))
        self.assertTrue(checker.check(None))

    def test_misspelled_ranges_skips_tags_and_valid_words(self):
        checker = spellcheck.SpellChecker('sv', known_tags=['V-101', 'V'])
        text = "Ventilen V-101 har en stavningskontrol"
        ranges = list(checker.misspelled_ranges(text))
        self.assertEqual(len(ranges), 1, ranges)
        start, end, word = ranges[0]
        self.assertEqual(word, 'stavningskontrol')
        self.assertEqual(text[start:end], 'stavningskontrol')


class MakeCheckerTests(unittest.TestCase):
    """make_checker() wires a SpellChecker to a real project's own
    equipment tags and approved-word dictionary."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_defaults_to_swedish(self):
        checker = spellcheck.make_checker(self.db)
        self.assertTrue(checker.check('ventil'))
        self.assertFalse(checker.check('stavningskontrol'))

    def test_reads_language_from_config(self):
        self.db.set_config('spellcheck_language', 'en')
        checker = spellcheck.make_checker(self.db)
        self.assertTrue(checker.check('hello'))

    def test_explicit_language_overrides_config(self):
        self.db.set_config('spellcheck_language', 'en')
        checker = spellcheck.make_checker(self.db, language='sv')
        self.assertTrue(checker.check('ventil'))

    def test_project_equipment_tags_are_excluded(self):
        self.db.add_equipment_item('V-101', 'V-101', 'V', 0, 'Ventil', '', 0)
        checker = spellcheck.make_checker(self.db)
        self.assertTrue(checker.check('V-101'))

    def test_project_user_dictionary_words_are_excluded(self):
        self.db.add_spellcheck_user_word('Nodbeskrivning')
        checker = spellcheck.make_checker(self.db)
        self.assertTrue(checker.check('Nodbeskrivning'))

    def test_a_different_project_does_not_see_this_projects_tags(self):
        self.db.add_equipment_item('V-101', 'V-101', 'V', 0, 'Ventil', '', 0)
        other_tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_other_test_")
        try:
            other_db = Database(path=os.path.join(other_tmpdir, "project.db"))
            try:
                checker = spellcheck.make_checker(other_db)
                self.assertFalse(checker.is_known_tag_or_user_word('V-101'))
            finally:
                del other_db
        finally:
            shutil.rmtree(other_tmpdir, ignore_errors=True)


class DatabaseUserDictionaryTests(unittest.TestCase):
    """Database.spellcheck_user_words()/add_.../remove_... persistence."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_db_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_new_project_has_an_empty_user_dictionary(self):
        self.assertEqual(self.db.spellcheck_user_words(), [])

    def test_added_word_is_persisted_and_read_back(self):
        self.db.add_spellcheck_user_word('Nodbeskrivning')
        self.assertEqual(self.db.spellcheck_user_words(), ['Nodbeskrivning'])

    def test_word_survives_reopening_the_same_project(self):
        self.db.add_spellcheck_user_word('Nodbeskrivning')
        reopened = Database(path=self.db.path)
        try:
            self.assertEqual(reopened.spellcheck_user_words(), ['Nodbeskrivning'])
        finally:
            del reopened

    def test_adding_the_same_word_twice_does_not_duplicate(self):
        self.db.add_spellcheck_user_word('Nodbeskrivning')
        self.db.add_spellcheck_user_word('nodbeskrivning')
        self.assertEqual(self.db.spellcheck_user_words(), ['Nodbeskrivning'])

    def test_blank_word_is_a_no_op(self):
        self.db.add_spellcheck_user_word('   ')
        self.assertEqual(self.db.spellcheck_user_words(), [])

    def test_remove_word_is_case_insensitive(self):
        self.db.add_spellcheck_user_word('Nodbeskrivning')
        self.db.remove_spellcheck_user_word('NODBESKRIVNING')
        self.assertEqual(self.db.spellcheck_user_words(), [])


def _underlined_ranges(text_edit):
    """(start, length) pairs actually painted with SpellCheckUnderline
    in the first block's layout -- the real, rendered format list a
    QSyntaxHighlighter produces, not just the plain per-fragment char
    format (which never shows highlighter overlays at all; verified
    empirically while building this feature)."""
    from PyQt6.QtGui import QTextCharFormat
    layout = text_edit.document().firstBlock().layout()
    return [(r.start, r.length) for r in layout.formats()
            if r.format.underlineStyle() == QTextCharFormat.UnderlineStyle.SpellCheckUnderline]


class SpellCheckContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_context_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_enabled_by_default_with_a_real_checker(self):
        context = spellcheck.SpellCheckContext(self.db)
        self.assertTrue(context.enabled)
        self.assertIsNotNone(context.checker)

    def test_reads_previously_saved_enabled_state(self):
        self.db.set_config('spellcheck_enabled', '0')
        context = spellcheck.SpellCheckContext(self.db)
        self.assertFalse(context.enabled)
        self.assertIsNone(context.checker)

    def test_set_enabled_false_persists_and_clears_the_checker(self):
        context = spellcheck.SpellCheckContext(self.db)
        context.set_enabled(False)
        self.assertIsNone(context.checker)
        self.assertEqual(self.db.get_config('spellcheck_enabled'), '0')

    def test_set_enabled_true_rebuilds_the_checker(self):
        context = spellcheck.SpellCheckContext(self.db)
        context.set_enabled(False)
        context.set_enabled(True)
        self.assertIsNotNone(context.checker)
        self.assertEqual(self.db.get_config('spellcheck_enabled'), '1')

    def test_add_user_word_persist_true_survives_a_fresh_context(self):
        context = spellcheck.SpellCheckContext(self.db)
        context.add_user_word('Nodbeskrivning', persist=True)
        self.assertTrue(context.checker.check('Nodbeskrivning'))
        fresh_context = spellcheck.SpellCheckContext(self.db)
        self.assertTrue(fresh_context.checker.check('Nodbeskrivning'))

    def test_add_user_word_persist_false_is_session_only(self):
        context = spellcheck.SpellCheckContext(self.db)
        context.add_user_word('Nodbeskrivning', persist=False)
        self.assertTrue(context.checker.check('Nodbeskrivning'))
        self.assertEqual(self.db.spellcheck_user_words(), [])
        fresh_context = spellcheck.SpellCheckContext(self.db)
        self.assertFalse(fresh_context.checker.check('Nodbeskrivning'))

    def test_suggestion_cache_survives_a_fresh_context(self):
        context = spellcheck.SpellCheckContext(self.db)
        context.cache_suggestions('stavningskontrol', ['stavningskontroll'])
        fresh_context = spellcheck.SpellCheckContext(self.db)
        self.assertEqual(
            fresh_context.cached_suggestions('stavningskontrol'),
            ['stavningskontroll'])

    def test_suggestion_cache_is_invalidated_by_user_dictionary_change(self):
        context = spellcheck.SpellCheckContext(self.db)
        context.cache_suggestions('stavningskontrol', ['stavningskontroll'])
        context.add_user_word('stavningskontrol', persist=True)
        self.assertIsNone(context.cached_suggestions('stavningskontrol'))


class HighlighterIntegrationTests(unittest.TestCase):
    """attach_spellcheck()/SpellCheckHighlighter against a real QTextEdit
    (and the actual _BoldTagTextEdit this app uses as its cell editor)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_hl_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))
        self.context = spellcheck.SpellCheckContext(self.db)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_misspelled_word_gets_underlined_correct_word_does_not(self):
        from PyQt6.QtWidgets import QTextEdit
        edit = QTextEdit()
        edit.setPlainText('Ventilen har en stavningskontrol')
        spellcheck.attach_spellcheck(edit, self.context)
        edit.document().firstBlock().layout()  # ensure layout exists
        self.app.processEvents()
        ranges = _underlined_ranges(edit)
        misspelled_start = edit.toPlainText().index('stavningskontrol')
        self.assertIn((misspelled_start, len('stavningskontrol')), ranges)
        correct_start = edit.toPlainText().index('Ventilen')
        self.assertFalse(
            any(start <= correct_start < start + length for start, length in ranges),
            "a correctly-spelled word must never be underlined")

    def test_disabling_spellcheck_clears_existing_underlines(self):
        from PyQt6.QtWidgets import QTextEdit
        edit = QTextEdit()
        edit.setPlainText('stavningskontrol')
        spellcheck.attach_spellcheck(edit, self.context)
        self.app.processEvents()
        self.assertTrue(_underlined_ranges(edit))
        self.context.set_enabled(False)
        self.assertEqual(_underlined_ranges(edit), [])

    def test_coexists_with_bold_tag_text_edits_own_manual_formatting(self):
        """The one real technical risk flagged in the plan: does a
        QSyntaxHighlighter's underline survive _BoldTagTextEdit's own
        direct QTextCursor.setCharFormat() bold-tag pass, or does one
        clobber the other? Verified here against the REAL class, not a
        stand-in QTextEdit."""
        from ui_helpers import _BoldTagTextEdit
        edit = _BoldTagTextEdit()
        edit.set_bold_tags(['V-101'])
        edit.setText('V-101 stavningskontrol')
        spellcheck.attach_spellcheck(edit, self.context)
        self.app.processEvents()
        ranges = _underlined_ranges(edit)
        misspelled_start = edit.toPlainText().index('stavningskontrol')
        self.assertIn((misspelled_start, len('stavningskontrol')), ranges,
            "the spellcheck underline must survive _BoldTagTextEdit's own "
            "bold-tag formatting pass")
        # And the bold formatting itself must be untouched by attaching
        # the highlighter.
        tag_start = edit.toPlainText().index('V-101')
        cursor = edit.textCursor()
        cursor.setPosition(tag_start)
        self.assertEqual(cursor.charFormat().fontWeight(), 700)


class RealUiWiringTests(unittest.TestCase):
    """The three Fas 2 attachment points, exercised through their real
    call sites rather than just the reusable spellcheck.py building
    blocks above: the ORS/KON/SG cell editor, the cause comment popup,
    and the node name/description dialog."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_wiring_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))
        self.context = spellcheck.SpellCheckContext(self.db)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_ors_cell_editor_gets_spellcheck_when_panel_has_a_context(self):
        from hazop import ScenarioTablePanel, CAUSE_T
        panel = ScenarioTablePanel(self.db)
        try:
            panel.spellcheck_context = self.context
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            self.db.add_cause(dev_id)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[0] == dev_id)
            index = panel._table.model().index(row, panel._C_ORS)
            option = self._simple_option(panel, index)
            editor = panel._pid_delegate.createEditor(panel._table, option, index)
            try:
                self.assertTrue(getattr(editor, '_spellcheck_attached', False),
                    "the ORS cell editor must have gone through attach_spellcheck")
            finally:
                editor.deleteLater()
        finally:
            panel.deleteLater()

    def test_ors_cell_editor_has_no_spellcheck_without_a_context(self):
        """Every existing test that builds ScenarioTablePanel directly
        (no MainWindow) must be completely unaffected -- the default
        spellcheck_context is None."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            self.assertIsNone(panel.spellcheck_context)
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            self.db.add_cause(dev_id)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[0] == dev_id)
            index = panel._table.model().index(row, panel._C_ORS)
            option = self._simple_option(panel, index)
            editor = panel._pid_delegate.createEditor(panel._table, option, index)
            try:
                self.assertFalse(getattr(editor, '_spellcheck_attached', False))
            finally:
                editor.deleteLater()
        finally:
            panel.deleteLater()

    @staticmethod
    def _simple_option(panel, index):
        from PyQt6.QtWidgets import QStyleOptionViewItem
        from PyQt6.QtCore import QRect
        option = QStyleOptionViewItem()
        option.rect = panel._table.visualRect(index)
        if option.rect.isEmpty():
            option.rect = QRect(0, 0, 200, 40)
        option.font = panel._table.font()
        return option

    def test_comment_popup_attaches_spellcheck_to_its_text_editor(self):
        from hazop import ScenarioTablePanel
        from PyQt6.QtWidgets import QDialog, QTextEdit
        from PyQt6.QtCore import QPoint
        panel = ScenarioTablePanel(self.db)
        try:
            panel.spellcheck_context = self.context
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            captured = {}
            def _fake_exec(dlg_self):
                captured['texts'] = dlg_self.findChildren(QTextEdit)
                return QDialog.DialogCode.Rejected
            with unittest.mock.patch.object(QDialog, 'exec', _fake_exec):
                panel._open_comment_popup(0, cause_id, QPoint(200, 200))
            self.assertEqual(len(captured.get('texts', [])), 1)
            self.assertTrue(
                getattr(captured['texts'][0], '_spellcheck_menu_filter', None) is not None,
                "the comment popup's QTextEdit must have gone through attach_spellcheck")
        finally:
            panel.deleteLater()

    def test_node_name_and_description_get_spellcheck_when_ribbon_has_a_main_window(self):
        from hazop import PropertiesRibbon, NODE_T
        mw = unittest.mock.Mock()
        mw.spellcheck_context = self.context
        panel = PropertiesRibbon(self.db, main_window=mw)
        try:
            node_id = self.db.add_node()
            panel.set_item(NODE_T, node_id)
            info_btn = next(b for b in panel._btns
                            if hasattr(b, 'toolTip') and b.toolTip().startswith('Redigera nod'))
            captured = {}
            from PyQt6.QtWidgets import QDialog, QTextEdit
            def _fake_show_popup(btn, dlg):
                captured['name_e'] = dlg.findChild(spellcheck.SpellCheckLineEdit, 'node_name_edit')
                captured['desc_e_attached'] = getattr(
                    dlg.findChild(QTextEdit, 'node_desc_edit'), '_spellcheck_menu_filter', None)
                return QDialog.DialogCode.Rejected
            with unittest.mock.patch.object(panel, '_show_popup', side_effect=_fake_show_popup):
                info_btn.click()
            self.assertIsNotNone(captured.get('name_e'),
                "node_name_edit must be a SpellCheckLineEdit when a spellcheck_context exists")
            self.assertIs(captured['name_e']._spellcheck_context, self.context)
            self.assertIsNotNone(captured.get('desc_e_attached'),
                "node_desc_edit must have gone through attach_spellcheck")
        finally:
            panel.deleteLater()


class SpellCheckLineEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_lineedit_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))
        self.context = spellcheck.SpellCheckContext(self.db)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make(self, text):
        edit = spellcheck.SpellCheckLineEdit(context=self.context)
        edit.resize(300, 30)
        edit.setText(text)
        edit.show()
        self.app.processEvents()
        return edit

    def test_paints_without_raising_when_text_has_a_misspelling(self):
        edit = self._make('Nod med stavningskontrol')
        try:
            edit.repaint()
            self.app.processEvents()
        except Exception as e:
            self.fail(f"paintEvent must not raise: {e!r}")

    def test_hit_test_finds_the_misspelled_word_by_x_position(self):
        edit = self._make('Nod med stavningskontrol')
        text = edit.text()
        misspelled_start = text.index('stavningskontrol')
        # x for the middle of the misspelled word
        x_mid = edit._char_x(
            misspelled_start + len('stavningskontrol') // 2,
            edit.cursorPosition(), edit.cursorRect().left(),
            edit.fontMetrics(), text)
        hit = edit._misspelled_range_at_x(x_mid)
        self.assertIsNotNone(hit)
        start, end, word = hit
        self.assertEqual(word, 'stavningskontrol')

    def test_hit_test_returns_none_over_a_correctly_spelled_word(self):
        edit = self._make('Nod med stavningskontrol')
        text = edit.text()
        x_start_of_field = edit._char_x(
            0, edit.cursorPosition(), edit.cursorRect().left(),
            edit.fontMetrics(), text)
        self.assertIsNone(edit._misspelled_range_at_x(x_start_of_field))

    def test_no_context_is_a_silent_no_op(self):
        edit = spellcheck.SpellCheckLineEdit()
        edit.setText('stavningskontrol')
        try:
            edit.repaint()
        except Exception as e:
            self.fail(f"must not raise without a context: {e!r}")
        self.assertIsNone(edit._misspelled_range_at_x(10))


class ContextMenuTests(unittest.TestCase):
    """Right-click suggestions + "Lägg till i ordlista"/"Ignorera"."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_menu_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))
        self.context = spellcheck.SpellCheckContext(self.db)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_lineedit_menu_offers_suggestions_and_dictionary_actions(self):
        from PyQt6.QtCore import QPoint
        edit = spellcheck.SpellCheckLineEdit(context=self.context)
        edit.resize(300, 30)
        edit.setText('stavningskontrol')
        edit.show()
        self.app.processEvents()
        x_mid = edit._char_x(8, edit.cursorPosition(), edit.cursorRect().left(),
                             edit.fontMetrics(), edit.text())
        event = unittest.mock.Mock()
        event.pos.return_value = QPoint(x_mid, edit.height() // 2)
        event.globalPos.return_value = edit.mapToGlobal(edit.rect().center())

        captured = {}
        def _fake_exec(menu_self, *a, **kw):
            captured['labels'] = [a.text() for a in menu_self.actions()]
            return None
        with unittest.mock.patch('PyQt6.QtWidgets.QMenu.exec', _fake_exec):
            edit.contextMenuEvent(event)

        self.assertIn('stavningskontroll', captured['labels'])
        self.assertIn('Lägg till i ordlista', captured['labels'])
        self.assertIn('Ignorera', captured['labels'])

    def test_picking_add_to_dictionary_persists_and_unflags_the_word(self):
        edit = spellcheck.SpellCheckLineEdit(context=self.context)
        edit.resize(300, 30)
        edit.setText('stavningskontrol')
        edit.show()
        self.app.processEvents()
        self.assertIsNotNone(edit._misspelled_range_at_x(
            edit._char_x(8, edit.cursorPosition(), edit.cursorRect().left(),
                        edit.fontMetrics(), edit.text())))

        self.context.add_user_word('stavningskontrol', persist=True)

        self.assertEqual(self.db.spellcheck_user_words(), ['stavningskontrol'])
        self.assertTrue(self.context.checker.check('stavningskontrol'))
        self.app.processEvents()
        x_mid = edit._char_x(8, edit.cursorPosition(), edit.cursorRect().left(),
                             edit.fontMetrics(), edit.text())
        self.assertIsNone(edit._misspelled_range_at_x(x_mid))


class CollectProjectTextFieldsTests(unittest.TestCase):
    """_collect_project_text_fields() -- Fas 4's whole-study walk."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_collect_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _build_scenario(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Reaktor R-101", "En beskrivning",
                             '', '', '', '')
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description="Orsakstext")
        self.db.set_cause_comment(cause_id, "En kommentar")
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, "Konsekvenstext", 2)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, description="Barriärtext")
        rec_id = self.db.add_recommendation_to_consequence(
            cons_id, description="Rekommendationstext")
        return {
            'node_id': node_id, 'cause_id': cause_id, 'cons_id': cons_id,
            'sg_id': sg_id, 'rec_id': rec_id,
        }

    def test_collects_every_populated_field_kind(self):
        ids = self._build_scenario()
        fields = spellcheck._collect_project_text_fields(self.db)
        by_kind = {f['kind']: f for f in fields}
        self.assertEqual(by_kind['node_name']['text'], "Reaktor R-101")
        self.assertEqual(by_kind['node_name']['id'], ids['node_id'])
        self.assertEqual(by_kind['node_desc']['text'], "En beskrivning")
        self.assertEqual(by_kind['cause_desc']['text'], "Orsakstext")
        self.assertEqual(by_kind['cause_comment']['text'], "En kommentar")
        self.assertEqual(by_kind['cons_desc']['text'], "Konsekvenstext")
        self.assertEqual(by_kind['sg_desc']['text'], "Barriärtext")
        self.assertEqual(by_kind['rec_desc']['text'], "Rekommendationstext")

    def test_blank_fields_are_skipped(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "", "", '', '', '', '')
        fields = spellcheck._collect_project_text_fields(self.db)
        kinds_for_this_node = [f for f in fields if f.get('id') == node_id]
        self.assertEqual(kinds_for_this_node, [])

    def test_consequence_with_a_chain_is_skipped(self):
        ids = self._build_scenario()
        cons = self.db.get_consequence(ids['cons_id'])
        self.db.update_consequence(
            ids['cons_id'], cons['description'], cons['severity'],
            cons.get('category', '') or '', '[{"text": "steg 1"}]')
        fields = spellcheck._collect_project_text_fields(self.db)
        self.assertFalse(any(f['kind'] == 'cons_desc' for f in fields),
            "a consequence with a non-empty consequence_chain must be excluded")

    def test_recommendation_shared_by_two_consequences_appears_once(self):
        ids = self._build_scenario()
        cons2_id = self.db.add_consequence(ids['cause_id'])
        self.db.update_consequence(cons2_id, "Andra konsekvensen", 1)
        self.db.link_recommendation_to_consequence(ids['rec_id'], cons2_id)
        fields = spellcheck._collect_project_text_fields(self.db)
        rec_fields = [f for f in fields if f['kind'] == 'rec_desc'
                      and f['id'] == ids['rec_id']]
        self.assertEqual(len(rec_fields), 1)


class ReviewDialogWriteBackTests(unittest.TestCase):
    """_APPLY_FUNCS -- each write-back path preserves the fields it must
    not itself have been asked to change."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_apply_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_apply_node_name_preserves_description_and_other_fields(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Gammalt namn", "Beskrivning", 'P1', 'Vatten', '2 bar', '20 C')
        spellcheck._apply_node_name(self.db, node_id, "Nytt namn")
        row = self.db.get_node(node_id)
        self.assertEqual(row['name'], "Nytt namn")
        self.assertEqual(row['description'], "Beskrivning")
        self.assertEqual(row['pid_ref'], 'P1')
        self.assertEqual(row['media'], 'Vatten')

    def test_apply_node_desc_preserves_name(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Nodnamn", "Gammal beskrivning", '', '', '', '')
        spellcheck._apply_node_desc(self.db, node_id, "Ny beskrivning")
        row = self.db.get_node(node_id)
        self.assertEqual(row['name'], "Nodnamn")
        self.assertEqual(row['description'], "Ny beskrivning")

    def test_apply_cons_desc_preserves_category_and_chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, "Gammal text", 3, "Miljö")
        spellcheck._apply_cons_desc(self.db, cons_id, "Ny text")
        cons = self.db.get_consequence(cons_id)
        self.assertEqual(cons['description'], "Ny text")
        self.assertEqual(cons['severity'], 3)
        self.assertEqual(cons['category'], "Miljö")

    def test_apply_cause_comment_round_trips(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        spellcheck._apply_cause_comment(self.db, cause_id, "Ny kommentar")
        self.assertEqual(self.db.get_cause_comment(cause_id), "Ny kommentar")


class SpellCheckReviewDialogTests(unittest.TestCase):
    """The manual "Kör stavningskontroll" walkthrough end to end, against
    a real project and a real SpellCheckContext -- Ändra / Ändra alla /
    Ignorera / Ignorera alla / Lägg till i ordlista, and the final
    "Klar!" message."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_spellcheck_dialog_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "project.db"))
        self.context = spellcheck.SpellCheckContext(self.db)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_cause(self, description):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description=description)
        return cause_id

    def _wait_for_suggestions(self, dlg, timeout_ms=5000):
        """Suggestions now compute on a background _SuggestWorker (2026-09-06,
        see NOTES.md -- spylls' suggest() measured 200ms-5s+ per word).
        Pump the event loop until the in-flight worker(s) finish and
        deliver their queued cross-thread signal, instead of reading
        dlg._suggestion_list before the real result has arrived."""
        import time
        deadline = time.monotonic() + timeout_ms / 1000
        while dlg._suggest_workers and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertFalse(dlg._suggest_workers,
            "suggestion worker never finished within the test timeout")

    def test_no_misspellings_shows_finished_message_immediately(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Nod", "En korrekt beskrivning", '', '', '', '')
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            self.assertIsNone(dlg._current)
            self.assertIn("Klar!", dlg._label.text())
            self.assertFalse(dlg._change_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_change_writes_the_replacement_back_to_the_database(self):
        cause_id = self._make_cause("Detta har en stavningskontrol")
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            self.assertIsNotNone(dlg._current)
            _field, _start, _end, word = dlg._current
            self.assertEqual(word, "stavningskontrol")
            self._wait_for_suggestions(dlg)
            dlg._suggestion_list.setCurrentRow(0)
            replacement = dlg._suggestion_list.currentItem().text()
            self.assertEqual(replacement, "stavningskontroll",
                "must be the real spylls suggestion, not the placeholder")
            dlg._on_change_clicked()
            cause = self.db.get_cause(cause_id)
            self.assertEqual(cause['description'], f"Detta har en {replacement}")
            self.assertEqual(dlg._changed_count, 1)
        finally:
            dlg.deleteLater()

    def test_custom_replacement_writes_any_user_text_back_to_database(self):
        cause_id = self._make_cause("Detta har en stavningskontrol")
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            dlg._custom_edit.setText("valfrittord")
            self.assertTrue(dlg._custom_change_btn.isEnabled())
            dlg._on_custom_change_clicked()
            cause = self.db.get_cause(cause_id)
            self.assertEqual(cause['description'], "Detta har en valfrittord")
            self.assertEqual(dlg._changed_count, 1)
        finally:
            dlg.deleteLater()

    def test_ignore_moves_on_without_writing_back(self):
        cause_id = self._make_cause("Detta har en stavningskontrol")
        original = self.db.get_cause(cause_id)['description']
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            dlg._on_ignore_clicked()
            self.assertIsNone(dlg._current)
            self.assertEqual(self.db.get_cause(cause_id)['description'], original)
            self.assertEqual(dlg._changed_count, 0)
        finally:
            dlg.deleteLater()

    def test_ignore_all_skips_every_later_occurrence_of_the_same_word(self):
        self._make_cause("stavningskontrol i en orsak")
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Nod", "stavningskontrol i en till", '', '', '', '')
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            dlg._on_ignore_all_clicked()
            self.assertIsNone(dlg._current)
            self.assertIn("Klar!", dlg._label.text())
            self.assertEqual(dlg._changed_count, 0)
        finally:
            dlg.deleteLater()

    def test_add_to_dictionary_persists_and_skips_remaining_occurrences(self):
        self._make_cause("stavningskontrol i hela texten")
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            dlg._on_add_to_dictionary_clicked()
            self.assertIn('stavningskontrol', self.db.spellcheck_user_words())
            self.assertIsNone(dlg._current)
        finally:
            dlg.deleteLater()

    def test_change_all_remembers_the_replacement_for_later_fields(self):
        self._make_cause("Ett fel: stavningskontrol")
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Nod", "Ett till fel: stavningskontrol",
                             '', '', '', '')
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            self._wait_for_suggestions(dlg)
            dlg._suggestion_list.setCurrentRow(0)
            replacement = dlg._suggestion_list.currentItem().text()
            dlg._on_change_all_clicked()
            self.assertIsNone(dlg._current)
            self.assertEqual(dlg._changed_count, 2)
            node_row = self.db.get_node(node_id)
            self.assertIn(replacement, node_row['description'])
        finally:
            dlg.deleteLater()

    def test_change_buttons_are_disabled_until_suggestions_arrive(self):
        """(2026-09-06, Anton: "det tar lite lång tid emellan varje") --
        spylls' suggest() is slow (measured 200ms-5s+ per word); Ändra/
        Ändra alla must not be clickable against the "Beräknar förslag…"
        placeholder while the background worker is still computing."""
        self._make_cause("Detta har en stavningskontrol")
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            self.assertTrue(dlg._suggest_workers,
                "a worker must already be in flight right after _present()")
            self.assertFalse(dlg._change_btn.isEnabled())
            self.assertFalse(dlg._change_all_btn.isEnabled())
            self._wait_for_suggestions(dlg)
            self.assertTrue(dlg._change_btn.isEnabled())
            self.assertTrue(dlg._change_all_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_repeated_word_reuses_the_cached_suggestion_without_a_new_worker(self):
        """The same typo repeated across a study (common: jargon, a
        consistently misspelled term) must only ever run spylls' slow
        suggest() once per run, not once per occurrence."""
        self._make_cause("Ett fel: stavningskontrol")
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Nod", "Ett till fel: stavningskontrol",
                             '', '', '', '')
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            self._wait_for_suggestions(dlg)
            self.assertIn('stavningskontrol', dlg._suggestion_cache)
            dlg._on_ignore_clicked()   # move on to the second occurrence
            self.assertIsNotNone(dlg._current)
            self.assertEqual(dlg._current[3], 'stavningskontrol')
            # The cache hit must apply synchronously -- no new worker,
            # and the real suggestion is already selectable immediately.
            self.assertFalse(dlg._suggest_workers)
            self.assertTrue(dlg._change_btn.isEnabled())
            self.assertEqual(dlg._suggestion_list.currentItem().text(),
                             "stavningskontroll")
        finally:
            dlg.deleteLater()

    def test_disabled_spellcheck_shows_an_explanatory_message(self):
        self.context.set_enabled(False)
        self._make_cause("stavningskontrol")
        dlg = spellcheck.SpellCheckReviewDialog(self.db, self.context)
        try:
            self.assertIn("avstängd", dlg._label.text())
            self.assertIsNone(dlg._current)
        finally:
            dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
