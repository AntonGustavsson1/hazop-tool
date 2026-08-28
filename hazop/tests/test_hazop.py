#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering hazop.py, plus any cross-module glue they
directly depend on. Test bodies are unchanged from the
original file, only their file location moved."""
"""Regression test suite for the HAZOP PyQt6 application.

Covers crash patterns that have been found and fixed in this codebase over
recent sessions:

  1. Orphaned-data crashes: deleting a "cause" can leave orphaned
     "consequence"/"safeguard" records; P&ID overlay code that draws
     connection lines between markers used to crash with KeyError /
     AttributeError when it hit an orphaned record's missing parent
     reference.
  2. sqlite3.Row objects do not support `.get()` — several code paths used
     to call `.get()` directly on a raw Row instead of converting to a dict
     first, causing AttributeError.
  3. ComboBox `currentIndex()` returning -1 (uninitialized/empty widget)
     used to cause IndexError when used to index into arrays such as
     RRF_VALUES / SG_TYPES.
  4. A settings panel referenced `self._sev_def_panel`, which was never
     actually instantiated, causing AttributeError when deleting a
     consequence category.

Run with:
    python -m pytest hazop/test_regression.py -v
or:
    python -m unittest hazop.test_regression -v

Requires QT_QPA_PLATFORM=offscreen for headless CI environments — this is
set automatically at the top of this file, before PyQt6/hazop is imported,
so the suite runs without a display (CI, SSH, etc.).
"""

import gc
import io
import os
import sys
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# ── Headless Qt setup — MUST happen before importing PyQt6 or hazop ────────
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# hazop.py / pid_viewer.py are large standalone scripts (not a package) that
# import each other via plain `from pid_viewer import ...`, so the hazop/
# directory must be on sys.path for those imports to resolve regardless of
# the current working directory the tests are launched from.
_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hazop  # noqa: E402  (import after sys.path setup, by design)
from hazop import (  # noqa: E402
    Database, TreePanel, MainWindow,
    NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T, EQUIP_T, LEDORD_T,
    freq_to_idx,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGraphicsPixmapItem, QTreeWidgetItemIterator, QCheckBox,
    QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit, QSizePolicy,
)
from PyQt6.QtGui import QPixmap, QFocusEvent  # noqa: E402
from PyQt6.QtCore import Qt, QPoint, QDate, QEvent, QThread, pyqtSignal  # noqa: E402
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════


from test_helpers import (
    _ensure_qapp, _menu_action_labels, _fake_pdf_loaded,
    _TempDbMainWindow, _find_tree_item,
)


class GlobalSearchDialogTests(unittest.TestCase):
    """Global search covers user fields and exposes useful navigation data."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.db = Database(self.path)

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def _add_node(self, name):
        cur = self.db.conn.execute("INSERT INTO nodes(name) VALUES (?)", (name,))
        self.db.conn.commit()
        return cur.lastrowid

    def test_partial_search_is_case_insensitive_and_names_the_field(self):
        node_id = self._add_node('Stockholms terminal')
        self.db.conn.execute(
            "UPDATE nodes SET description=? WHERE id=?",
            ('Placering i STOCKHOLM', node_id))
        self.db.conn.commit()
        dlg = hazop.GlobalSearchDialog(self.db)
        dlg._search('stock')
        labels = [dlg._list.item(i).text() for i in range(dlg._list.count())]
        self.assertTrue(any('Nod #' in label and 'Namn:' in label for label in labels))
        self.assertTrue(any('Beskrivning:' in label for label in labels))

    def test_search_includes_non_scenario_user_data(self):
        self.db.conn.execute(
            "INSERT INTO recommendations(description, responsible) VALUES (?, ?)",
            ('Byt ventil i Stockholm', 'Anna Andersson'))
        self.db.conn.execute(
            "INSERT INTO project_custom_fields(name, value) VALUES (?, ?)",
            ('Anläggning', 'Stockholmsdepån'))
        self.db.conn.commit()
        dlg = hazop.GlobalSearchDialog(self.db)
        dlg._search('stock')
        hits = [dlg._list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(dlg._list.count())]
        self.assertIn('recommendation', {h['kind'] for h in hits})
        self.assertIn('project_field', {h['kind'] for h in hits})
        self.assertTrue(all(h['document'] and h['post'] and h['field_label'] for h in hits))

    def test_activating_result_emits_complete_navigation_hit(self):
        node_id = self._add_node('Stockholm')
        dlg = hazop.GlobalSearchDialog(self.db)
        received = []
        dlg.navigate_requested.connect(received.append)
        dlg._search('stock')
        dlg._navigate(dlg._list.item(0))
        self.assertEqual(received[0]['kind'], 'node')
        self.assertEqual(received[0]['id'], node_id)
        self.assertEqual(received[0]['field'], 'name')

    def test_replace_current_changes_only_selected_hit_and_moves_on(self):
        node_id = self._add_node('Nod')
        dev_id = self.db.add_deviation(node_id, 'Pump stopped')
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description='Pump stopped twice')
        dlg = hazop.GlobalSearchDialog(self.db)
        dlg._edit.setText('Pump stopped')
        dlg._replace_edit.setText('Pump trip')
        cause_row = next(i for i in range(dlg._list.count())
                         if dlg._list.item(i).data(Qt.ItemDataRole.UserRole)['kind'] == 'cause')
        dlg._list.setCurrentRow(cause_row)
        dlg._replace_current()
        self.assertEqual(self.db.get_cause(cause_id)['description'], 'Pump trip twice')
        self.assertEqual(self.db.conn.execute(
            'SELECT description FROM deviations WHERE id=?', (dev_id,)).fetchone()[0],
            'Pump stopped')

    def test_replace_all_uses_checked_preview_and_is_one_undo_operation(self):
        node_id = self._add_node('Nod')
        dev_id = self.db.add_deviation(node_id, 'Pump stopped')
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description='Pump stopped')
        with _TempDbMainWindow() as win:
            # The dialog must use the same database as its parent window.
            window_db = win.db
            try:
                win.db = self.db
                dlg = hazop.GlobalSearchDialog(self.db, win)
                dlg._edit.setText('Pump stopped')
                dlg._replace_edit.setText('Pump trip')
                with unittest.mock.patch.object(
                        QMessageBox, 'question', return_value=QMessageBox.StandardButton.Ok), \
                     unittest.mock.patch.object(QMessageBox, 'information'):
                    dlg._replace_all()
                self.assertEqual(self.db.get_cause(cause_id)['description'], 'Pump trip')
                self.assertEqual(len(win._global_replace_undo_stack), 1)
                self.assertTrue(win._undo_global_replace())
                self.assertEqual(self.db.get_cause(cause_id)['description'], 'Pump stopped')
            finally:
                win.db = window_db

    def test_tag_identity_hit_is_protected_from_direct_replace(self):
        equipment_id = self.db.add_equipment_item(
            'PSHH-101', 'PSHH-101', 'PSHH', 0, 'Instrument', '', 0)
        dlg = hazop.GlobalSearchDialog(self.db)
        dlg._edit.setText('PSHH')
        hit_item = next(dlg._list.item(i) for i in range(dlg._list.count())
                        if dlg._list.item(i).data(Qt.ItemDataRole.UserRole)['field'] == 'tag')
        self.assertTrue(hit_item.data(Qt.ItemDataRole.UserRole)['protected'])
        self.assertEqual(hit_item.checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(dlg._selected_replace_hits(), [])
        self.assertEqual(self.db.get_equipment_by_id(equipment_id)['tag'], 'PSHH-101')

# ══════════════════════════════════════════════════════════════════════════
# 5. Global sys.excepthook regression — exception in a Qt slot must not
#    silently close the whole application
# ══════════════════════════════════════════════════════════════════════════

class GlobalExceptHookTests(unittest.TestCase):
    """In PyQt5.5+/PyQt6, an exception raised inside a signal/slot callback
    (button click, tree selection, etc.) propagates to sys.excepthook. With
    the default hook, PyQt prints the traceback and the process aborts.
    hazop.py installs hazop._global_exception_hook as sys.excepthook at
    module import time specifically so this no longer happens: the slot call
    fails, but the QApplication event loop keeps running.

    This test proves the *new* hook specifically fires (not just that
    nothing crashes) by temporarily wrapping it to record calls, and
    silences the QMessageBox.critical dialog it would otherwise pop up so
    the test doesn't hang waiting for a user click.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_exception_in_button_click_slot_invokes_global_hook_not_crash(self):
        from PyQt6.QtWidgets import QWidget, QPushButton, QMessageBox

        self.assertIs(
            sys.excepthook, hazop._global_exception_hook,
            "sys.excepthook is not hazop._global_exception_hook -- either "
            "it was never installed or something later overwrote it.")

        # Silence the dialog the hook shows -- headless test, no user to
        # click it away, and we don't want the test to hang.
        original_critical = QMessageBox.critical
        QMessageBox.critical = staticmethod(lambda *a, **k: None)

        # Don't actually persist a crash-report JSON file to hazop/crashes/
        # on every test run -- that's a disk side effect unrelated to what
        # this test is checking. Stub out just the persistence step; the
        # hook's own try/except around this call is exactly what makes that
        # safe to do.
        original_handle_exception = hazop.CrashReporter.handle_exception
        hazop.CrashReporter.handle_exception = classmethod(lambda cls, *a, **k: None)

        # Wrap (not replace) the real hook so we can assert it specifically
        # fired, with which exception type, while still exercising the real
        # logging/dialog code path.
        original_hook = sys.excepthook
        calls = []

        def _recording_hook(exc_type, exc_value, exc_tb):
            calls.append(exc_type)
            return original_hook(exc_type, exc_value, exc_tb)

        sys.excepthook = _recording_hook

        widget = QWidget()
        button = QPushButton("Crash me", widget)

        def _raise(*_a, **_k):
            raise ValueError("test exception for excepthook")

        button.clicked.connect(_raise)

        try:
            try:
                button.click()
            except Exception as e:
                self.fail(
                    "button.click() must not raise out to the test -- the "
                    f"exception should have gone through sys.excepthook "
                    f"instead: {e!r}")

            self.assertIsNotNone(
                QApplication.instance(),
                "QApplication must survive an unhandled exception in a "
                "slot -- this is the whole point of installing the hook.")
            self.assertEqual(
                len(calls), 1,
                "sys.excepthook (wrapping _global_exception_hook) should "
                "have fired exactly once for the exception raised in the "
                "button's clicked slot.")
            self.assertIs(
                calls[0], ValueError,
                "the hook fired but with the wrong exception type.")
        finally:
            sys.excepthook = original_hook
            QMessageBox.critical = original_critical
            hazop.CrashReporter.handle_exception = original_handle_exception
            button.deleteLater()
            widget.deleteLater()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()


class GlobalStylesheetFontSizeTests(unittest.TestCase):
    """'När du skrev om UI så försvann möjligheten att förstora och
    förminska texten' (2026-08-11) — the near-monochrome theme's
    universal `* { font-size: 9pt; }` rule wins over ANY later
    QWidget.setFont() call for that property on a matching widget (the
    same well-known "QSS always beats Qt::*Role/dynamic properties"
    quirk already documented and fixed elsewhere in this file for cell
    background/foreground colors) — silently freezing
    ScenarioTablePanel's "Textstorlek" spinbox at 9pt no matter what
    value the user picked. Testing the actual Qt style cascade
    end-to-end is exactly the kind of thing this project's own history
    has shown to be unreliable in a headless test (a synthetic,
    real-render-free check can pass even when the real bug is present —
    see RiskCellActualRenderColorTests' docstring) — so this instead
    pins down the concrete, textual root cause: the universal selector
    must never carry a font-size rule again."""

    def test_universal_selector_has_no_font_size_rule(self):
        import re
        from hazop import _get_windows11_stylesheet
        css = _get_windows11_stylesheet()
        m = re.search(r'\*\s*\{([^}]*)\}', css)
        self.assertIsNotNone(m, "expected a universal '*' selector block in the stylesheet")
        self.assertNotIn('font-size', m.group(1),
            "a font-size rule on the universal selector overrides every widget's own "
            "setFont() call, including ScenarioTablePanel's zoom spinbox")


class GlobalTooltipContrastTests(unittest.TestCase):
    """Tooltip colors belong to the application, not individual widgets."""

    def test_main_sets_readable_application_tooltip_palette(self):
        src = Path(hazop.__file__).read_text(encoding='utf-8')
        main_idx = src.index("if __name__ == '__main__':")
        startup = src[main_idx:]
        self.assertIn(
            "QPalette.ColorRole.ToolTipBase, QColor('#17191C')", startup)
        self.assertIn(
            "QPalette.ColorRole.ToolTipText, QColor('#FFFFFF')", startup)


class Utf8ConsoleOutputTests(unittest.TestCase):
    """The console echo log handler wrote straight to the unreconfigured
    sys.stderr, whose default encoding on a Windows console is the system
    codepage (cp1252 here) rather than UTF-8 — logging any message
    containing an emoji (the app's own status messages are full of them)
    raised UnicodeEncodeError: 'charmap' codec can't encode character
    (real crash report, crash_20260807_115134_UnicodeEncodeError.json;
    trivially reproducible on this machine via print('\U0001f3ed'))."""

    def test_configure_reconfigures_stdout_and_stderr_to_utf8(self):
        real_stdout, real_stderr = hazop.sys.stdout, hazop.sys.stderr
        fake_out = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        fake_err = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        try:
            hazop.sys.stdout = fake_out
            hazop.sys.stderr = fake_err
            hazop._configure_utf8_console_output()
            self.assertEqual(fake_out.encoding.lower(), 'utf-8')
            self.assertEqual(fake_err.encoding.lower(), 'utf-8')
        finally:
            hazop.sys.stdout = real_stdout
            hazop.sys.stderr = real_stderr

    def test_writing_emoji_through_reconfigured_stream_does_not_raise(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        # Without the fix, this exact write raises UnicodeEncodeError —
        # the same exception/message as the real crash report.
        with self.assertRaises(UnicodeEncodeError):
            stream.write('🏭 HAZOP Tool started')
            stream.flush()

        stream2 = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        stream2.reconfigure(encoding='utf-8', errors='replace')
        stream2.write('🏭 HAZOP Tool started')  # must not raise
        stream2.flush()


class MainWindowOpensHzpPassedOnConstructionTests(unittest.TestCase):
    """2026-08-21 (see NOTES.md "Paketera HAZOP-appen som en
    installationsfil"): MainWindow.__init__ already accepted an hzp_path
    parameter, but never actually did anything with it -- self._hzp_path
    was set to None unconditionally regardless of what was passed in, so a
    .hzp file double-clicked via a Windows file association (the whole
    point of adding one) would open the app on an empty default project,
    silently ignoring the file. Fixed by calling the already-existing
    self._load_hzp(hzp_path) at the end of __init__ when a path is given.

    Both MainWindow() constructions below must be pointed at throwaway
    temp databases, never the real hazop_project.db -- _load_hzp() itself
    copies onto the module-level DB_PATH name and reopens Database(DB_PATH)
    directly (not through the Database() call MainWindow.__init__ makes),
    so both hazop.Database (for the initial empty-project construction)
    AND hazop.DB_PATH (for _load_hzp's own copy/reopen target) need
    patching -- patching only one leaves the other pointed at production
    data."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_hzp_launch_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _close_window(self, win):
        """Mirrors _TempDbMainWindow.__exit__'s careful multi-pass
        cleanup -- closing a MainWindow only schedules Qt object
        destruction, and constructing the next one before that finishes
        (with its now-closed sqlite3 connection still half-alive) segfaults
        the interpreter rather than raising a catchable exception."""
        try:
            win.close()
            win.deleteLater()
            for _ in range(5):
                self.app.processEvents()
        except Exception:
            pass
        gc.collect()

    def test_hzp_path_given_at_construction_is_actually_loaded(self):
        src_db_path = os.path.join(self._tmpdir, "source.db")
        dest_db_path = os.path.join(self._tmpdir, "dest.db")
        hzp_path = os.path.join(self._tmpdir, "kollegans_projekt.hzp")

        class _SourceDatabase(hazop.Database):
            def __init__(self, path=src_db_path):
                super().__init__(path=path)

        class _DestDatabase(hazop.Database):
            def __init__(self, path=dest_db_path):
                super().__init__(path=path)

        orig_database, orig_db_path = hazop.Database, hazop.DB_PATH
        distinguishing_node_name = "Kollegans unika nod XKCD-42"
        win1 = None
        win2 = None
        try:
            # 1. Build a source project with a distinguishing node, save it
            # as a real .hzp (exercises the real _write_hzp, not a
            # hand-crafted zip fixture).
            hazop.Database = _SourceDatabase
            win1 = MainWindow()
            node_id = win1.db.add_node()
            win1.db.update_node(node_id, distinguishing_node_name, '', '')
            win1.db.conn.commit()
            win1._write_hzp(hzp_path)
            self.assertTrue(os.path.isfile(hzp_path))
            self._close_window(win1)
            win1 = None

            # 2. Construct a FRESH MainWindow with hzp_path=... and confirm
            # it actually loaded the source project instead of starting on
            # a blank default one.
            hazop.Database = _DestDatabase
            hazop.DB_PATH = Path(dest_db_path)
            win2 = MainWindow(hzp_path)

            self.assertEqual(win2._hzp_path, hzp_path)
            node_names = [dict(n).get('name') for n in win2.db.nodes()]
            self.assertIn(
                distinguishing_node_name, node_names,
                "MainWindow(hzp_path=...) must actually load that project "
                "-- the window came up without the node from the .hzp "
                "file, meaning hzp_path was accepted but ignored again.")
        finally:
            hazop.Database = orig_database
            hazop.DB_PATH = orig_db_path
            if win1 is not None:
                self._close_window(win1)
            if win2 is not None:
                self._close_window(win2)

    def test_opening_a_different_project_mid_session_via_load_hzp_actually_loads_it(self):
        """A separate, more common real-world trigger for the SAME
        underlying bug found while writing the test above: 'Öppna
        (.hzp)...' (_hzp_open, a thin QFileDialog wrapper around
        _load_hzp) lets a user switch to a different project in an
        ALREADY-RUNNING window -- not just at startup. _load_hzp used to
        close its old sqlite3 connection AFTER copying the new project's
        database over DB_PATH; closing a still-open WAL-mode connection
        checkpoints ITS OWN (pre-copy) buffered writes back onto whatever
        file DB_PATH now names, silently clobbering the just-copied
        project back to the old one's state. Reproduced directly against
        the real Database class (not just theorised) before fixing the
        ordering (close-then-copy, with a same-path reopen on a failed
        copy to preserve the original "don't strand the user with no
        working db" recovery guarantee)."""
        other_project_db_path = os.path.join(self._tmpdir, "other_project_source.db")
        running_window_db_path = os.path.join(self._tmpdir, "the_running_window.db")
        other_hzp_path = os.path.join(self._tmpdir, "annat_projekt.hzp")
        other_node_name = "Annat projekts nod QRST-99"

        class _OtherProjectDatabase(hazop.Database):
            def __init__(self, path=other_project_db_path):
                super().__init__(path=path)

        class _RunningDatabase(hazop.Database):
            def __init__(self, path=running_window_db_path):
                super().__init__(path=path)

        orig_database, orig_db_path = hazop.Database, hazop.DB_PATH
        win = None
        other_win = None
        try:
            # Build the SEPARATE project to switch to, as a real .hzp --
            # its OWN db file, distinct from the "already running" window's.
            hazop.Database = _OtherProjectDatabase
            other_win = MainWindow()
            node_id = other_win.db.add_node()
            other_win.db.update_node(node_id, other_node_name, '', '')
            other_win.db.conn.commit()
            other_win._write_hzp(other_hzp_path)
            self._close_window(other_win)
            other_win = None

            # A fresh "already running" window on its OWN (different,
            # empty) project -- _load_hzp copies the other project's
            # database over hazop.DB_PATH, which must point at THIS
            # window's db path for the copy-onto-an-open-connection
            # scenario (the one that exposed the bug) to actually apply.
            hazop.Database = _RunningDatabase
            hazop.DB_PATH = Path(running_window_db_path)
            win = MainWindow()
            # 2026-08-24: a brand-new study now auto-seeds one default node
            # (see Database.__init__'s `pre_existing_db` check) instead of
            # starting completely empty.
            self.assertEqual([dict(n).get('name') for n in win.db.nodes()], ['Ny nod'])

            win._load_hzp(other_hzp_path)

            self.assertEqual(win._hzp_path, other_hzp_path)
            node_names = [dict(n).get('name') for n in win.db.nodes()]
            self.assertIn(
                other_node_name, node_names,
                "_load_hzp() must actually switch to the other project's "
                "data, not silently keep (or revert to) the previously "
                "open project.")
        finally:
            hazop.Database = orig_database
            hazop.DB_PATH = orig_db_path
            if other_win is not None:
                self._close_window(other_win)
            if win is not None:
                self._close_window(win)

    def test_no_hzp_path_starts_on_the_default_empty_project_as_before(self):
        """Regression guard for the opposite direction: passing nothing
        (the normal `python hazop.py` launch, no file argument) must not
        suddenly start trying to load something."""
        dest_db_path = os.path.join(self._tmpdir, "dest2.db")

        class _DestDatabase(hazop.Database):
            def __init__(self, path=dest_db_path):
                super().__init__(path=path)

        orig_database = hazop.Database
        win = None
        try:
            hazop.Database = _DestDatabase
            win = MainWindow(None)
            self.assertIsNone(win._hzp_path)
        finally:
            hazop.Database = orig_database
            if win is not None:
                self._close_window(win)


class MultiprocessingFreezeSupportTests(unittest.TestCase):
    """2026-08-24 (see NOTES.md "Analysera P&ID kraschar och startar om
    appen vid flera sidor"): "Analysera P&ID" (and equipment/similar-symbol
    analysis) hand off to concurrent.futures.ProcessPoolExecutor for
    documents with 4+ pages (see pid_viewer.py's _should_parallelize).
    On Windows, spawning a process re-executes a PyInstaller-frozen .exe
    from scratch UNLESS multiprocessing.freeze_support() has already run
    -- without it, every worker process re-entered __main__ as if freshly
    launched, opening another full MainWindow. That read as "the app
    crashes and restarts" from the outside, and only reproduces in an
    actual frozen build with a real 4+ page PDF (freeze_support() is a
    documented no-op when unpackaged/non-Windows, so there is no way to
    exercise the real failure mode from a normal `python -m unittest`
    run) -- this is a source-level regression guard, not a behavioural
    test, and deliberately says so.

    What it actually checks: multiprocessing.freeze_support() is called,
    and is the FIRST statement inside `if __name__ == '__main__':`, before
    anything else (logging setup, QApplication, etc.) -- per Python's own
    documentation, it must run before any Process/Pool/ProcessPoolExecutor
    could conceivably be created, and moving other setup ahead of it would
    silently reintroduce the exact bug this guards against."""

    def test_freeze_support_is_the_first_statement_in_main_block(self):
        src = Path(hazop.__file__).read_text(encoding='utf-8')
        main_idx = src.index("if __name__ == '__main__':")
        after_main = src[main_idx:]
        # First non-comment, non-blank line after the guard.
        body_lines = after_main.splitlines()[1:]
        first_code_line = next(
            (ln.strip() for ln in body_lines
             if ln.strip() and not ln.strip().startswith('#')),
            None)
        self.assertEqual(
            first_code_line, 'import multiprocessing',
            "the first statement in __main__ must import multiprocessing "
            "immediately before calling freeze_support() -- found "
            f"{first_code_line!r} instead; something got inserted ahead "
            "of the fix")

    def test_freeze_support_is_called_before_qapplication_is_constructed(self):
        src = Path(hazop.__file__).read_text(encoding='utf-8')
        main_idx = src.index("if __name__ == '__main__':")
        freeze_idx = src.index('multiprocessing.freeze_support()', main_idx)
        qapp_idx = src.index('QApplication(sys.argv)', main_idx)
        self.assertLess(
            freeze_idx, qapp_idx,
            "multiprocessing.freeze_support() must run before QApplication "
            "is constructed (and before any ProcessPoolExecutor could be "
            "created later during a scan) -- found it AFTER QApplication "
            "construction instead")


class PrintScenarioTableTests(unittest.TestCase):
    """Real crash found in the wild (crash_20260825_135308_AttributeError.
    json): _print_scenario_table() built its QPrinter with the PyQt5-style
    enum access `QPrinter.PageSize.A4` / `QPrinter.Orientation.Landscape`
    -- PyQt6 removed both from QPrinter (page size/orientation now live on
    QPageSize/QPageLayout instead), so "Skriv ut scenariotabell" crashed
    with AttributeError every single time it was used, before a print
    preview ever appeared. Fixing that surfaced a second, not-yet-reported
    break in the same method one line later: `doc.print_` (the PyQt5 name,
    trailing underscore because `print` is a Python keyword) was renamed to
    plain `doc.print` in PyQt6 -- also fixed here."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_print_scenario_table_does_not_crash_building_the_printer(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.add_deviation(node_id, "Högt flöde")
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.add_safeguard(cons_id)

            with unittest.mock.patch(
                    'PyQt6.QtPrintSupport.QPrintPreviewDialog.exec', return_value=0):
                win._print_scenario_table()   # must not raise AttributeError


class PDFViewerSplitterFlexibilityTests(unittest.TestCase):
    """The PDF canvas and information areas must have a broad usable range."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_main_pdf_panels_are_no_longer_tightly_capped(self):
        with _TempDbMainWindow() as win:
            self.assertLessEqual(win.tree_panel.minimumWidth(), 120)
            self.assertGreater(win.tree_panel.maximumWidth(), 1000)
            self.assertLessEqual(win.pid_panel.minimumWidth(), 240)
            self.assertLessEqual(win.scenario_panel.minimumHeight(), 110)
            self.assertGreater(win.scenario_panel.maximumHeight(), 1000)
            self.assertEqual(
                win._outer_splitter.widget(0).sizePolicy().verticalPolicy(),
                QSizePolicy.Policy.Ignored)
            self.assertFalse(win._h_splitter.isCollapsible(0))
            self.assertFalse(win._h_splitter.isCollapsible(1))


if __name__ == "__main__":
    unittest.main()
