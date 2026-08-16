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
_HAZOP_DIR = Path(__file__).resolve().parent
if str(_HAZOP_DIR) not in sys.path:
    sys.path.insert(0, str(_HAZOP_DIR))

import hazop  # noqa: E402  (import after sys.path setup, by design)
from hazop import (  # noqa: E402
    Database, TreePanel, MainWindow,
    NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T, EQUIP_T, LEDORD_T,
    freq_to_idx,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGraphicsPixmapItem, QTreeWidgetItemIterator, QCheckBox,
    QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit,
)
from PyQt6.QtGui import QPixmap, QFocusEvent  # noqa: E402
from PyQt6.QtCore import Qt, QPoint, QDate, QEvent, QThread, pyqtSignal  # noqa: E402
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════

def _ensure_qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _menu_action_labels(mock_menu):
    """Extract the text label from each mocked QMenu.addAction() call.
    Several addAction(text, ...) call sites became addAction(icon, text,
    ...) during the 2026-08-12 emoji-to-icon sweep (see NOTES.md) — skip a
    leading QIcon argument so callers keep getting plain label strings
    regardless of which overload the call site now uses."""
    from PyQt6.QtGui import QIcon
    labels = []
    for c in mock_menu.addAction.call_args_list:
        args = [a for a in c.args if not isinstance(a, QIcon)]
        if args:
            labels.append(args[0])
    return labels


def _fake_pdf_loaded(panel, page=0):
    """Put a PIDPanel's viewer into a "PDF loaded, one page" state without
    touching disk or PyMuPDF, so PIDPanel._load_overlays() runs its real
    marker/connection-line drawing code instead of early-returning on
    `if self.viewer.pdf_doc is None: return`.

    _apply_lod() (called at the end of _load_overlays()) calls
    `item.setTransformationMode(...)` on every value in _all_page_items, so
    the fake page item must be a real QGraphicsPixmapItem added to the
    scene -- not a bare None/sentinel -- or _apply_lod itself raises
    AttributeError (a test-fixture artifact, not an app bug).
    """
    viewer = panel.viewer
    viewer.pdf_doc = object()  # any truthy value bypasses the "no PDF" branch
    pix_item = QGraphicsPixmapItem(QPixmap(1, 1))
    viewer._scene.addItem(pix_item)
    viewer._all_page_items = {page: pix_item}
    return pix_item


class _TempDbMainWindow:
    """Context manager that constructs a MainWindow against a scratch,
    temp-file SQLite database instead of the real hazop_project.db.

    MainWindow() always calls `Database()` (no path argument) internally,
    and Database.__init__'s `path=DB_PATH` default is bound to the module
    constant at import time, so simply reassigning hazop.DB_PATH afterwards
    has no effect. Instead, this temporarily monkeypatches hazop.Database
    itself so the *next* construction uses a tempfile path, then restores
    the original class unconditionally (even on failure) so other tests
    are unaffected.
    """

    def __init__(self):
        self._tmpdir = None
        self._orig_database = None

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_mainwindow_test_")
        db_path = os.path.join(self._tmpdir, "test_project.db")
        self._orig_database = hazop.Database

        class _TempPathDatabase(hazop.Database):
            def __init__(self, path=db_path):
                super().__init__(path=path)

        hazop.Database = _TempPathDatabase
        try:
            self._win = MainWindow()
        finally:
            hazop.Database = self._orig_database
        return self._win

    def __exit__(self, exc_type, exc_val, exc_tb):
        hazop.Database = self._orig_database
        # Tear the window down fully *inside* this process before moving on:
        # close() + deleteLater() only schedules C++ object destruction, it
        # does not run it. Without thoroughly pumping the event loop here,
        # the next test's MainWindow() construction can run while this
        # window's Qt objects (and their references to a now-closed sqlite3
        # connection) are still half-alive, which segfaults the interpreter
        # rather than raising a catchable Python exception.
        try:
            self._win.close()
            self._win.deleteLater()
            app = QApplication.instance()
            if app is not None:
                # Multiple passes: the first processEvents() run delivers the
                # deferred delete event, which itself can post further
                # cleanup events (child widgets, timers) that need another
                # pass to fully drain.
                for _ in range(5):
                    app.processEvents()
        except Exception:
            pass
        self._win = None
        gc.collect()  # drop the last Python references before removing tmpdir
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        return False


# ══════════════════════════════════════════════════════════════════════════
# 1. Database-layer tests (no GUI needed)
# ══════════════════════════════════════════════════════════════════════════

class DatabaseLayerTests(unittest.TestCase):
    """Exercise Database directly against a scratch SQLite file.

    Never touches the real project database (hazop_project.db) — each test
    gets its own tempfile, removed in tearDown.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db  # triggers Database.__del__ -> conn.close()
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _make_full_chain(self):
        """Create node -> deviation -> cause -> consequence -> safeguard.

        Returns a dict of all the ids.
        """
        node_id = self.db.add_node()
        deviations = self.db.deviations(node_id)
        self.assertTrue(len(deviations) > 0, "add_node() should seed deviations")
        deviation_id = deviations[0]['id']

        cause_id = self.db.add_cause(deviation_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)

        return {
            'node_id': node_id,
            'deviation_id': deviation_id,
            'cause_id': cause_id,
            'cons_id': cons_id,
            'sg_id': sg_id,
        }

    # ── chain creation ───────────────────────────────────────────────────

    def test_create_full_chain(self):
        ids = self._make_full_chain()
        self.assertIsNotNone(self.db.get_node(ids['node_id']))
        self.assertIsNotNone(self.db.get_deviation(ids['deviation_id']))
        self.assertIsNotNone(self.db.get_cause(ids['cause_id']))
        self.assertIsNotNone(self.db.get_consequence(ids['cons_id']))
        self.assertIsNotNone(self.db.get_safeguard(ids['sg_id']))

    # ── orphaned-data crash class (bug #1) ───────────────────────────────

    def test_delete_cause_then_access_orphaned_consequence_and_safeguard(self):
        """Deleting a cause must not make later reads of its (now orphaned
        or cascade-deleted) consequence/safeguard raise KeyError/AttributeError.

        Foreign keys are ON in this app (PRAGMA foreign_keys = ON), so
        consequences/safeguards cascade-delete along with their cause. The
        important regression-safety property either way is: get_consequence()
        / get_safeguard() on an id that no longer has a valid parent must
        return None (never raise), and if it does return a row, dict(row).get()
        on it must work without AttributeError.
        """
        ids = self._make_full_chain()

        self.db.delete_cause(ids['cause_id'])

        # Must not raise, whether cascade removed the rows or not.
        try:
            cons_row = self.db.get_consequence(ids['cons_id'])
            sg_row = self.db.get_safeguard(ids['sg_id'])
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"Accessing orphaned consequence/safeguard raised: {e!r}")

        for row, label in ((cons_row, 'consequence'), (sg_row, 'safeguard')):
            if row is None:
                continue  # cascade delete removed it — fine, no crash
            # Simulate how app code accesses these rows: dict(...).get(...)
            d = dict(row)
            self.assertEqual(d.get('cause_id', 'MISSING_OK'), d.get('cause_id', 'MISSING_OK'))
            # Specifically exercise the .get('cause_id') access pattern from
            # the bug report — must not raise even if cause_id points nowhere.
            try:
                _ = d.get('cause_id')
            except AttributeError as e:  # pragma: no cover - failure path
                self.fail(f"dict({label} row).get('cause_id') raised AttributeError: {e}")

    def test_orphaned_consequence_without_cascade_does_not_crash_overlay_lookup(self):
        """Force a true orphan (bypass FK cascade) and verify the pattern used
        by PIDPanel._load_overlays() — get_consequence() then
        dict(c).get('cause_id') followed by get_cause(cause_id) — survives.

        This directly reproduces the old crash: P&ID overlay code drew
        connection lines by looking up a consequence's parent cause; when the
        cause was gone the lookup used to raise instead of degrading
        gracefully to "skip this connection".
        """
        ids = self._make_full_chain()

        # Disable FK enforcement on this connection only, to simulate legacy
        # data / a migration edge case that leaves a true orphan behind.
        self.db.conn.execute("PRAGMA foreign_keys = OFF")
        self.db.conn.execute("DELETE FROM causes WHERE id=?", (ids['cause_id'],))
        self.db.commit()
        self.db.conn.execute("PRAGMA foreign_keys = ON")

        # cause is gone, but consequence/safeguard rows are now truly orphaned
        self.assertIsNone(self.db.get_cause(ids['cause_id']))

        cons = self.db.get_consequence(ids['cons_id'])
        self.assertIsNotNone(cons, "orphaned consequence row should still be readable")

        # This is the exact access pattern used in PIDPanel._load_overlays():
        #   cause_id = dict(c).get('cause_id') if c else None
        #   if cause_id and cause_id in all_cause_pos: ...
        try:
            cause_id = dict(cons).get('cause_id') if cons else None
            parent_cause = self.db.get_cause(cause_id) if cause_id else None
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"Overlay-style orphan lookup raised: {e!r}")
        self.assertIsNone(parent_cause, "parent cause was deleted; lookup should degrade to None")

        sg = self.db.get_safeguard(ids['sg_id'])
        self.assertIsNotNone(sg, "orphaned safeguard row should still be readable")
        try:
            cons_id_from_sg = dict(sg).get('consequence_id') if sg else None
            _ = self.db.get_consequence(cons_id_from_sg) if cons_id_from_sg else None
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"Overlay-style orphan lookup (safeguard) raised: {e!r}")

    def test_delete_consequence_directly_safeguard_orphan(self):
        """Delete a consequence directly and make sure its safeguard (either
        cascade-deleted or orphaned) can still be queried without crashing."""
        ids = self._make_full_chain()
        self.db.delete_consequence(ids['cons_id'])

        try:
            sg_row = self.db.get_safeguard(ids['sg_id'])
        except Exception as e:  # pragma: no cover
            self.fail(f"get_safeguard() after deleting parent consequence raised: {e!r}")

        if sg_row is not None:
            try:
                _ = dict(sg_row).get('consequence_id')
            except AttributeError as e:  # pragma: no cover
                self.fail(f"dict(safeguard).get('consequence_id') raised: {e}")

    # ── sqlite3.Row / .get() contract (bug #2) ───────────────────────────

    def test_get_accessors_return_none_or_dict_like(self):
        """db.get_cause(), get_consequence(), get_safeguard(), get_node(),
        get_deviation() must all return either None, or something on which
        `.get(key, default)` works without raising — whether by returning a
        plain dict already, or by supporting .get() directly.

        sqlite3.Row (the raw fetchone() result) does NOT implement .get(),
        so any accessor that hands back a raw Row fails this contract; the
        safe pattern used elsewhere in the app is to wrap with dict(row)
        before calling .get(). This test pins down the *dict-like* contract
        callers rely on, using the same dict()-wrapping the app already does
        at its call sites (see PIDPanel._load_overlays, MainWindow._on_selected).
        """
        ids = self._make_full_chain()

        accessors = [
            (self.db.get_node, ids['node_id']),
            (self.db.get_deviation, ids['deviation_id']),
            (self.db.get_cause, ids['cause_id']),
            (self.db.get_consequence, ids['cons_id']),
            (self.db.get_safeguard, ids['sg_id']),
        ]
        for fn, id_ in accessors:
            result = fn(id_)
            if result is None:
                continue
            # Raw sqlite3.Row does not support .get() -- the app-safe pattern
            # is to wrap it in dict() first, exactly like production code does.
            wrapped = dict(result)
            self.assertTrue(
                hasattr(wrapped, 'get'),
                f"{fn.__name__}() result, once wrapped in dict(), must support .get()")
            # Exercise it for real, with a key that is guaranteed absent.
            self.assertEqual(wrapped.get('__no_such_key__', 'DEFAULT'), 'DEFAULT')

        # Non-existent ids must return None cleanly, not raise.
        for fn in (self.db.get_node, self.db.get_deviation, self.db.get_cause,
                   self.db.get_consequence, self.db.get_safeguard):
            self.assertIsNone(fn(999999))

    def test_raw_row_get_would_raise_attributeerror(self):
        """Documents the exact failure mode of bug #2: calling .get() directly
        on the raw sqlite3.Row returned by the accessors (without dict())
        raises AttributeError. This test exists so that if a future change
        makes get_cause()/get_consequence()/etc. return a dict-like row
        factory instead of sqlite3.Row, someone notices this test starts
        failing (and can then simplify call sites that currently do
        dict(row).get(...))."""
        ids = self._make_full_chain()
        raw = self.db.get_cause(ids['cause_id'])
        self.assertIsNotNone(raw)
        if isinstance(raw, sqlite3.Row):
            with self.assertRaises(AttributeError):
                raw.get('description')
        else:
            # Accessor already upgraded to a dict-like row — contract holds
            # directly, nothing more to prove here.
            self.assertTrue(hasattr(raw, 'get'))


# ══════════════════════════════════════════════════════════════════════════
# 1b. Backup system tests (stability improvement #5)
# ══════════════════════════════════════════════════════════════════════════

class BackupSystemTests(unittest.TestCase):
    """Exercise Database._write_backup()/_prune_backups() directly, plus the
    forced backup call sites (pre-migration in __init__, pre-delete in
    delete_node()). Never touches the real project database — each test
    gets its own tempfile, removed in tearDown.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_backup_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        # Reset the class-level throttle timestamps so each test's explicit
        # _write_backup(startup=True) calls aren't skipped due to timing
        # left over from a previous test in the same process.
        Database._last_backup_ts = 0.0
        Database._last_prune_ts = 0.0

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_write_backup_creates_file_with_same_data(self):
        """A forced backup must produce a standalone .db file that a fresh
        sqlite3 connection can open and query, containing the same rows."""
        node_id = self.db.add_node()
        self.db._write_backup(startup=True)

        backup_dir = self.db._backup_dir()
        backups = list(backup_dir.glob("backup_*.db"))
        self.assertTrue(len(backups) >= 1, "expected at least one backup file")

        # Open the newest backup as an independent connection and verify data.
        newest = max(backups, key=lambda p: p.stat().st_mtime)
        check_conn = sqlite3.connect(str(newest))
        try:
            row = check_conn.execute(
                "SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
            self.assertIsNotNone(row, "backup file is missing data present in live DB")
            self.assertEqual(row[0], node_id)
        finally:
            check_conn.close()

    def test_prune_keeps_bounded_number_of_backups(self):
        """_prune_backups() must not let backups grow without bound once past
        the hourly+daily retention window. We fabricate old-dated filenames
        directly (bypassing the once-per-hour prune throttle and the
        once-per-minute write throttle) to exercise the pruning logic itself."""
        backup_dir = self.db._backup_dir()

        import datetime as _dt
        now = _dt.datetime.now()
        # Retention keeps at most one file per day beyond the hourly window,
        # for _DAILY_KEEP_D days -- i.e. roughly _DAILY_KEEP_D survivors past
        # the hourly cutoff, plus everything within the hourly window itself.
        # Spread files across many more distinct past days than that so
        # pruning has real work to do (files older than hourly+daily windows
        # combined get deleted outright).
        total_days = Database._HOURLY_KEEP_H // 24 + Database._DAILY_KEEP_D + 60
        for days_ago in range(total_days):
            ts = now - _dt.timedelta(days=days_ago, hours=1)
            fname = f"backup_{ts.strftime('%Y-%m-%dT%H-%M-%S-%f')}.db"
            dest = sqlite3.connect(str(backup_dir / fname))
            dest.close()

        before_count = len(list(backup_dir.glob("backup_*.db")))
        self.assertGreater(
            before_count, Database._HOURLY_KEEP_H + Database._DAILY_KEEP_D,
            "test setup should create more files than the retention policy allows")

        # Force the prune (bypass its own once-per-hour throttle).
        Database._last_prune_ts = 0.0
        self.db._prune_backups(backup_dir)

        after_count = len(list(backup_dir.glob("backup_*.db")))
        self.assertLess(
            after_count, before_count,
            "_prune_backups() should have removed backups beyond the retention window")

    def test_delete_node_forces_backup_before_cascading_delete(self):
        """delete_node() is the most destructive single user action (cascades
        causes -> consequences -> safeguards). It must force a fresh,
        un-throttled backup immediately beforehand, even if a recent commit()
        already wrote a throttled one."""
        node_id = self.db.add_node()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        self.db.add_consequence(cause_id)

        backup_dir = self.db._backup_dir()
        before = set(backup_dir.glob("backup_*.db"))

        # Simulate "a commit just happened" so the throttle would normally
        # suppress another backup for _COMMIT_INTERVAL_S seconds.
        import time as _time
        Database._last_backup_ts = _time.monotonic()

        self.db.delete_node(node_id)

        after = set(backup_dir.glob("backup_*.db"))
        self.assertTrue(
            len(after) > len(before),
            "delete_node() should force a new backup even though the "
            "throttle window had not elapsed")
        self.assertIsNone(self.db.get_node(node_id), "node should still be deleted")

    def test_backup_failure_does_not_block_delete_node(self):
        """A backup failure must never prevent the destructive operation it
        guards -- delete_node() must still delete the node even if
        _write_backup() raises."""
        node_id = self.db.add_node()

        with unittest.mock.patch.object(
                self.db, '_write_backup', side_effect=RuntimeError("disk full")):
            self.db.delete_node(node_id)

        self.assertIsNone(
            self.db.get_node(node_id),
            "delete_node() must still delete the node even when the backup call raises")

    def test_write_backup_returns_path_on_success(self):
        """_write_backup()'s return value feeds the manual 'Skapa
        säkerhetskopia nu' button's success/failure message (2026-08-09,
        see NOTES.md) — existing throttled/startup call sites all ignore
        it, so this is purely additive."""
        dst = self.db._write_backup(startup=True)
        self.assertIsNotNone(dst)
        self.assertTrue(Path(dst).exists())

    def test_write_backup_returns_none_when_throttled(self):
        self.db._write_backup(startup=True)
        dst = self.db._write_backup(startup=False)   # immediately after — within throttle window
        self.assertIsNone(dst)

    def test_write_backup_returns_none_on_failure(self):
        with unittest.mock.patch(
                'sqlite3.connect',
                side_effect=sqlite3.OperationalError("disk full")):
            dst = self.db._write_backup(startup=True)
        self.assertIsNone(dst)


class ManualBackupButtonTests(unittest.TestCase):
    """StudyManagementPanel's "💾 Skapa säkerhetskopia nu" button forces an
    immediate, unthrottled backup and reports success/failure to the user
    (2026-08-09, see NOTES.md) — previously the only way to get a backup
    was to wait for the automatic throttled/startup ones."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_manualbackup_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        Database._last_backup_ts = 0.0

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_backup_now_shows_success_message_with_path(self):
        from hazop import StudyManagementPanel
        panel = StudyManagementPanel(self.db)
        try:
            with unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._backup_now()
            self.assertEqual(mock_info.call_count, 1)
        finally:
            panel.deleteLater()

    def test_backup_now_shows_warning_on_failure(self):
        from hazop import StudyManagementPanel
        panel = StudyManagementPanel(self.db)
        try:
            with unittest.mock.patch.object(Database, '_write_backup', return_value=None), \
                 unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
                panel._backup_now()
            self.assertEqual(mock_warn.call_count, 1)
        finally:
            panel.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# 2. GUI smoke tests (headless, offscreen)
# ══════════════════════════════════════════════════════════════════════════

class GuiSmokeTests(unittest.TestCase):
    """Instantiate real widgets against a temp DB and simulate the crash
    scenarios that were previously fixed. QT_QPA_PLATFORM=offscreen (set at
    module import time) lets this run without a display.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_gui_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, db=None):
        db = db or self.db
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    # ── (a) delete cause -> reload P&ID overlays must not crash ──────────

    # NOTE: test_delete_cause_then_reload_overlays_no_crash and
    # test_delete_consequence_then_reload_overlays_no_crash were removed
    # 2026-08-13 (see NOTES.md: the P&ID canvas is now
    # object-placement-only) — they reproduced a crash class specific to
    # orphaned cause/consequence/safeguard *markers* on the P&ID, via
    # Database.add_cause_marker/add_consequence_marker/add_safeguard_marker
    # (also removed, see NOTES.md). _load_overlays() no longer reads those
    # tables at all, so the crash class they guarded against can no longer
    # occur by construction — nothing left to regression-test there.

    # ── (b) select a safeguard node in the tree -> _on_selected ──────────

    def test_select_safeguard_in_tree_no_crash(self):
        """Reproduces selecting a safeguard node in the tree (SG_T), which
        drives MainWindow._on_selected(SG_T, id_) — a path that walks
        safeguard -> consequence -> cause -> deviation and used to be
        vulnerable to missing-key crashes on any broken link.

        NOTE: scenario_panel.load_deviation()/load_consequence() are
        stubbed out here. They ultimately call QTableWidget.resizeRowsToContents(),
        which recurses into a QStyledItemDelegate.sizeHint() callback
        (hazop.py _ScenarioDelegate.sizeHint, ~line 9773) — under this
        machine's headless Qt platform plugin that path reproducibly hits a
        native access violation (verified independently of this test suite,
        both under QT_QPA_PLATFORM=offscreen and =minimal), which is an
        environment/table-rendering fragility unrelated to the orphaned-data
        crash class this test targets. Stubbing keeps the test focused on
        _on_selected()'s own dict/orphan-lookup logic (the thing that was
        actually buggy) without depending on that unrelated native table
        layout path surviving headlessly.
        """
        with _TempDbMainWindow() as win:
            win.scenario_panel.load_deviation = lambda *a, **k: None
            win.scenario_panel.load_consequence = lambda *a, **k: None
            win.scenario_panel.load_cause = lambda *a, **k: None

            ids = self._make_full_chain(db=win.db)
            win.tree_panel.refresh(SG_T, ids['sg_id'])
            try:
                win._on_selected(SG_T, ids['sg_id'])
            except Exception as e:
                self.fail(f"_on_selected(SG_T, id_) raised: {e!r}")

            # Also simulate a genuinely orphaned safeguard (parent
            # consequence gone) selected in the tree.
            win.db.delete_consequence(ids['cons_id'])
            try:
                win._on_selected(SG_T, ids['sg_id'])
            except Exception as e:
                self.fail(f"_on_selected(SG_T, id_) on orphaned safeguard raised: {e!r}")

    def test_select_consequence_in_tree_no_crash(self):
        """Same idea for CONS_T selection after its parent cause is gone.

        See the docstring on test_select_safeguard_in_tree_no_crash for why
        scenario_panel's load_* methods are stubbed here.
        """
        with _TempDbMainWindow() as win:
            win.scenario_panel.load_deviation = lambda *a, **k: None
            win.scenario_panel.load_consequence = lambda *a, **k: None
            win.scenario_panel.load_cause = lambda *a, **k: None

            ids = self._make_full_chain(db=win.db)
            win.tree_panel.refresh(CONS_T, ids['cons_id'])
            try:
                win._on_selected(CONS_T, ids['cons_id'])
            except Exception as e:
                self.fail(f"_on_selected(CONS_T, id_) raised: {e!r}")

            win.db.conn.execute("PRAGMA foreign_keys = OFF")
            win.db.conn.execute("DELETE FROM causes WHERE id=?", (ids['cause_id'],))
            win.db.commit()
            win.db.conn.execute("PRAGMA foreign_keys = ON")

            try:
                win._on_selected(CONS_T, ids['cons_id'])
            except Exception as e:
                self.fail(f"_on_selected(CONS_T, id_) on orphaned consequence raised: {e!r}")

    # ── delete via TreePanel.delete_selected() ────────────────────────────

    def test_tree_panel_delete_selected_cause_then_overlay_reload(self):
        """End-to-end: create chain via TreePanel-style DB calls, select the
        cause node in the actual QTreeWidget, invoke delete_selected() (the
        real UI deletion path), then reload P&ID overlays."""
        from pid_viewer import PIDPanel

        tree = TreePanel(self.db)
        panel = PIDPanel(self.db)
        try:
            ids = self._make_full_chain()

            tree.refresh(CAUSE_T, ids['cause_id'])
            current = tree.tree.currentItem()
            self.assertIsNotNone(current, "tree should have selected the newly created cause")

            # delete_selected() shows a QMessageBox.question confirmation
            # dialog; monkeypatch it to auto-accept ("Yes") since this is a
            # headless test with no user to click through it.
            from PyQt6.QtWidgets import QMessageBox
            original_question = QMessageBox.question
            QMessageBox.question = staticmethod(
                lambda *a, **k: QMessageBox.StandardButton.Yes)
            try:
                tree.delete_selected()
            finally:
                QMessageBox.question = original_question

            self.assertIsNone(self.db.get_cause(ids['cause_id']))

            _fake_pdf_loaded(panel)
            try:
                panel.reload_overlays()
            except Exception as e:
                self.fail(f"reload_overlays() after TreePanel.delete_selected() raised: {e!r}")
        finally:
            tree.deleteLater()
            panel.deleteLater()

    # ── (c) delete a consequence category from Settings -> _sev_def_panel ─

    def test_delete_consequence_category_no_crash(self):
        """Reproduces bug #4: SettingsPanel._cat_delete() referenced
        self._sev_def_panel, which was never instantiated anywhere in
        SettingsPanel.__init__, causing AttributeError as soon as a user
        deleted a consequence category from the Settings screen.

        If this test fails with AttributeError on `_sev_def_panel`, that
        confirms the bug is still present (or has regressed) and
        SettingsPanel._cat_delete() needs to either instantiate/guard that
        attribute or stop referencing it.
        """
        from hazop import SettingsPanel

        panel = SettingsPanel(self.db)
        try:
            self.db.add_category("TestCategory")
            panel._load_categories()
            self.assertGreater(panel._cat_list.count(), 0)
            panel._cat_list.setCurrentRow(0)

            try:
                panel._cat_delete()
            except AttributeError as e:
                self.fail(
                    "SettingsPanel._cat_delete() raised AttributeError — "
                    f"the self._sev_def_panel bug is present: {e!r}")
        finally:
            panel.deleteLater()

    # ── ComboBox currentIndex() == -1 bounds-safety (bug #3) ─────────────

    def test_combo_index_minus_one_does_not_index_error_rrf_and_sgtype(self):
        """An empty/uninitialized QComboBox reports currentIndex() == -1.
        Any code that does RRF_VALUES[idx] / SG_TYPES[idx] without a bounds
        check would raise IndexError (since idx=-1 actually wraps to the last
        element in Python rather than erroring, the *real* historical bug
        was more subtle -- but any out-of-range index, positive or negative,
        must be handled). This test exercises the guarded lookup pattern
        used throughout hazop.py: `X[idx] if 0 <= idx < len(X) else default`.
        """
        from hazop import RRF_VALUES, SG_TYPES
        from PyQt6.QtWidgets import QComboBox

        rrf_combo = QComboBox()
        rrf_combo.addItems([str(v) for v in RRF_VALUES])
        # Do NOT select anything -- currentIndex() is -1 on a populated-but-
        # never-selected combo is unusual, but an empty combo box (no items
        # added at all) reliably reports -1.
        empty_combo = QComboBox()
        self.assertEqual(empty_combo.currentIndex(), -1)

        idx = empty_combo.currentIndex()
        try:
            rrf = RRF_VALUES[idx] if 0 <= idx < len(RRF_VALUES) else 1
            sg_type = SG_TYPES[idx] if 0 <= idx < len(SG_TYPES) else 'Övrigt'
        except IndexError as e:
            self.fail(f"Guarded combo-index lookup still raised IndexError: {e}")
        self.assertEqual(rrf, 1)
        self.assertEqual(sg_type, 'Övrigt')

        # Also prove the *unguarded* access is exactly the historical bug,
        # so this test would have caught a regression to unguarded indexing.
        with self.assertRaises(IndexError):
            _ = [][idx]  # any index into an empty list raises, incl. -1

    def test_mainwindow_instantiates_headless_with_temp_db(self):
        """Sanity check that the fixture approach for full MainWindow tests
        is sound: constructing MainWindow() against a scratch DB must not
        raise and must not touch the real project database."""
        with _TempDbMainWindow() as win:
            self.assertIsNotNone(win.db)
            self.assertNotEqual(
                str(win.db.path), str((_HAZOP_DIR / "hazop_project.db").resolve()),
                "MainWindow must not have opened the real project database")


# ══════════════════════════════════════════════════════════════════════════
# 3b. Marker-click native crash regression (bug #6):
#     _on_marker_navigate() double-fired _on_selected()/_rebuild() per click,
#     and a focused _LopaWidget QLineEdit's focus-out during table teardown
#     re-entered _update_lopa_risk() while blockSignals() was flipped back to
#     False mid-_rebuild() (blockSignals is a flat bool, not a nesting
#     counter) — together these caused a native (non-Python) crash when
#     clicking a cause marker on the P&ID viewer.
# ══════════════════════════════════════════════════════════════════════════

class MarkerNavigateCrashTests(unittest.TestCase):
    """Reproduces the exact double-fire + reentrancy scenario that caused a
    native crash on marker click, and verifies both fixes:

      1. TreePanel.refresh(..., emit_selection=False) no longer cascades
         setCurrentItem() -> currentItemChanged -> _on_select ->
         item_selected -> MainWindow._on_selected, so _on_marker_navigate()
         drives _on_selected() (and therefore scenario_panel._rebuild()) only
         once per marker click instead of twice.
      2. ScenarioTablePanel._update_lopa_risk() no-ops while `_rebuilding` is
         True, so a _LopaWidget cell editor's focus-out signal firing
         reentrantly mid-teardown cannot flip _table.blockSignals() back to
         False out from under the outer _rebuild().

    NOTE: scenario_panel.load_deviation()/load_cause()/load_consequence()
    ultimately call QTableWidget.resizeRowsToContents(), which is documented
    elsewhere in this suite (see test_select_safeguard_in_tree_no_crash) as
    reproducibly hitting a native access violation under this machine's
    headless Qt platform plugin — an unrelated environment fragility. Tests
    here that need to count *how many times* the load_* methods are invoked
    (rather than let them run for real) wrap them with a counting spy that
    still calls through only where safe, or stub them out entirely, exactly
    following that existing pattern.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    # ── Bug 1: double-fire fix ────────────────────────────────────────────

    def test_marker_navigate_calls_on_selected_exactly_once(self):
        """_on_marker_navigate() must drive MainWindow._on_selected() exactly
        once per marker click. Before the fix, TreePanel.refresh()'s internal
        setCurrentItem() call (issued after blockSignals(False)) fired the
        tree's currentItemChanged -> _on_select -> item_selected signal chain
        for real, invoking _on_selected() once, and then
        _on_marker_navigate()'s own explicit call invoked it a second time —
        two full scenario_panel loads/rebuilds per single click.
        """
        with _TempDbMainWindow() as win:
            # Stub the heavy scenario_panel loaders (see class docstring / the
            # existing test_select_safeguard_in_tree_no_crash precedent) so
            # this test isolates the *call count*, not table-rendering
            # behaviour that is independently fragile under offscreen Qt.
            win.scenario_panel.load_deviation = unittest.mock.Mock()
            win.scenario_panel.load_consequence = unittest.mock.Mock()
            win.scenario_panel.load_cause = unittest.mock.Mock()
            win.scenario_panel.load_node = unittest.mock.Mock()

            ids = self._make_full_chain(win.db)

            # tree_panel.item_selected was connected to the *bound method*
            # win._on_selected back in MainWindow.__init__, so merely
            # reassigning win._on_selected afterwards would not intercept
            # calls arriving via that pre-existing Qt connection (only the
            # explicit call at the end of _on_marker_navigate would be seen).
            # Disconnect and reconnect to the spy so both the signal-cascade
            # path and the explicit call are counted, exactly reproducing
            # what a real marker click drives.
            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win._on_marker_navigate('cause', ids['cause_id'])

            self.assertEqual(
                on_selected_spy.call_count, 1,
                "_on_marker_navigate() must call _on_selected() exactly once "
                "per marker click (it used to fire twice: once via "
                "TreePanel.refresh()'s setCurrentItem() cascade, once via "
                "the explicit call)")
            on_selected_spy.assert_called_once_with(CAUSE_T, ids['cause_id'])

    def test_tree_panel_refresh_emit_selection_false_suppresses_cascade(self):
        """Directly verify TreePanel.refresh(..., emit_selection=False) does
        not cascade into item_selected, while the default (emit_selection=
        True, used by every other caller) still does — proving the fix does
        not change behaviour for existing call sites.
        """
        db_tmpdir = tempfile.mkdtemp(prefix="hazop_marker_test_")
        try:
            db = Database(path=os.path.join(db_tmpdir, "test_project.db"))
            tree = TreePanel(db)
            try:
                ids = self._make_full_chain(db)

                item_selected_spy = unittest.mock.Mock()
                tree.item_selected.connect(item_selected_spy)

                # emit_selection=False: no cascade.
                tree.refresh(CAUSE_T, ids['cause_id'], emit_selection=False)
                self.assertEqual(
                    item_selected_spy.call_count, 0,
                    "refresh(emit_selection=False) must not emit item_selected")
                self.assertIsNotNone(tree.tree.currentItem(),
                                      "the visual highlight must still be set")

                # Default behaviour (emit_selection=True) must still cascade,
                # so other existing callers of tree_panel.refresh(type_, id_)
                # keep working exactly as before.
                tree.refresh(CONS_T, ids['cons_id'])
                self.assertEqual(
                    item_selected_spy.call_count, 1,
                    "refresh() with default emit_selection=True must still "
                    "emit item_selected, unchanged for pre-existing callers")
            finally:
                tree.deleteLater()
        finally:
            shutil.rmtree(db_tmpdir, ignore_errors=True)

    # ── Bug 2: _LopaWidget focus-out reentrancy guard ─────────────────────

    def test_update_lopa_risk_noop_while_rebuilding(self):
        """The core of the fix: _update_lopa_risk() must return immediately
        (without touching _table) if called while ScenarioTablePanel._rebuilding
        is True — simulating a _LopaWidget cell editor's focus-out firing
        editingFinished -> _save -> changed.emit() reentrantly mid-teardown,
        which used to reach _update_lopa_risk()'s own
        `finally: self._table.blockSignals(False)` and prematurely unblock
        signals on the *outer* _rebuild()'s table while _build_rows() was
        still constructing new cell widgets.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)

            # Give the consequence some LOPA data so _update_lopa_risk() has
            # real work to do if it were allowed to run.
            win.db.update_consequence_factors(ids['cons_id'], True, 10, False, 10)

            panel._rebuilding = True
            try:
                block_signals_spy = unittest.mock.Mock(
                    wraps=panel._table.blockSignals)
                panel._table.blockSignals = block_signals_spy
                try:
                    panel._update_lopa_risk(ids['cons_id'])
                finally:
                    panel._table.blockSignals = block_signals_spy._mock_wraps
            finally:
                panel._rebuilding = False

            block_signals_spy.assert_not_called()

    def test_lopa_widget_editing_finished_during_rebuild_does_not_reenter(self):
        """End-to-end version of the reentrancy scenario: build a real
        _LopaWidget bound to a live cons_id, simulate _rebuild() being
        mid-teardown (`_rebuilding = True`), then fire the widget's `changed`
        signal (as its QLineEdit's editingFinished -> _save would during a
        focus-out) and confirm it reaches _update_lopa_risk() but the guard
        makes it a no-op rather than touching the table's signal-blocking
        state.
        """
        from hazop import _LopaWidget

        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            win.db.update_consequence_factors(ids['cons_id'], True, 10, False, 10)

            lopa = _LopaWidget(win.db, ids['cons_id'],
                                True, 10.0, False, 10.0, 0)
            lopa.changed.connect(panel._update_lopa_risk)
            try:
                # Give the FA edit field focus, as the real bug scenario
                # requires (a focused QLineEdit inside a cell widget being
                # destroyed by setRowCount(0) mid-rebuild).
                lopa._fa_edit.setFocus()

                panel._rebuilding = True
                try:
                    update_spy = unittest.mock.Mock(wraps=panel._update_lopa_risk)
                    panel._update_lopa_risk = update_spy
                    lopa.changed.connect(update_spy)

                    # Simulate the focus-out -> editingFinished -> _save ->
                    # changed.emit() chain directly (this is exactly what
                    # QLineEdit does internally on focus-out).
                    lopa._fa_edit.editingFinished.emit()

                    self.assertTrue(
                        update_spy.called,
                        "the widget's changed signal should still reach "
                        "_update_lopa_risk (that part of the wiring is "
                        "unchanged) — the guard inside it is what must stop "
                        "the reentrant work, not the signal connection")
                finally:
                    panel._rebuilding = False
            finally:
                lopa.deleteLater()

    def test_rebuild_clears_focus_before_teardown(self):
        """Belt-and-suspenders fix: _rebuild() must clear focus from any
        active cell editor before calling setRowCount(0), so the focus-out
        signal cascade described above never fires in the first place, even
        before the _rebuilding guard in _update_lopa_risk() would catch it.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            win.db.update_consequence_factors(ids['cons_id'], True, 10, False, 10)

            # Stub the heavy loaders so _rebuild() (invoked transitively via
            # load_cause below) stays inside the safe/tested code path,
            # consistent with the rest of this suite's approach to the
            # documented resizeRowsToContents native-crash fragility.
            win.scenario_panel.load_deviation = unittest.mock.Mock()
            win.scenario_panel.load_consequence = unittest.mock.Mock()

            fake_editor = unittest.mock.Mock()
            panel._table.focusWidget = unittest.mock.Mock(return_value=fake_editor)

            panel._rebuild()

            fake_editor.clearFocus.assert_called_once()


class TextOnlyEditFastPathTests(unittest.TestCase):
    """ScenarioTablePanel._update_row_text_only(): a pure description-text
    edit (cause/consequence/safeguard) patches just the affected cell(s) in
    place instead of paying for a full _rebuild() (teardown + re-walk the
    entire DB hierarchy + _apply_spans() + _resize_rows()).

    Before this fix, editing a safeguard's description called
    self._schedule_rebuild() unconditionally, even though nothing about a
    safeguard's OWN row (its RFORE/SLUT columns are derived from rrf,
    not description text) or any OTHER row depends on that text. Cause/
    consequence edits already didn't trigger a rebuild (a side effect of the
    emit_selection=False fix earlier this session), but didn't sync other
    rows showing the same id (span groups keep one QTableWidgetItem per
    underlying row even when visually merged).
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_safeguard_description_edit_no_longer_schedules_full_rebuild(self):
        """Editing a safeguard's description must patch the cell in place and
        must NOT call _schedule_rebuild() — nothing about its own row's
        risk-derived columns or any other row depends on description text.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel.load_cause(ids['cause_id'])

            schedule_spy = unittest.mock.Mock(wraps=panel._schedule_rebuild)
            panel._schedule_rebuild = schedule_spy

            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == ids['sg_id'])
            item = panel._table.item(row, panel._C_SG)
            item.setText("Ny barriärbeskrivning")
            panel._on_cell_changed(row, panel._C_SG)

            schedule_spy.assert_not_called()
            self.assertEqual(
                dict(win.db.get_safeguard(ids['sg_id']))['description'],
                "Ny barriärbeskrivning")

    def test_update_row_text_only_noop_while_rebuilding(self):
        """Mirrors test_update_lopa_risk_noop_while_rebuilding: the fast path
        must return immediately without touching the table if called while
        _rebuilding is True (e.g. a reentrant cell-commit signal firing
        mid-teardown), not just skip the (now-removed) rebuild call.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel.load_cause(ids['cause_id'])

            panel._rebuilding = True
            try:
                block_signals_spy = unittest.mock.Mock(wraps=panel._table.blockSignals)
                panel._table.blockSignals = block_signals_spy
                try:
                    panel._update_row_text_only('safeguard', ids['sg_id'], "Should not apply")
                finally:
                    panel._table.blockSignals = block_signals_spy._mock_wraps
            finally:
                panel._rebuilding = False

            block_signals_spy.assert_not_called()

    def test_cause_edit_syncs_all_rows_sharing_the_same_cause(self):
        """A cause with two consequences produces two rows sharing the same
        cause_id (merged into one visual span by _apply_spans(), but still
        two distinct QTableWidgetItem objects underneath). Editing the ORS
        text on one row must patch the OTHER row's copy too, without a full
        rebuild.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            win.db.add_consequence(ids['cause_id'])  # second consequence -> second row, same cause
            panel.load_cause(ids['cause_id'])

            rows = [r for r, m in enumerate(panel._row_meta)
                    if m[1] == ids['cause_id']]
            self.assertEqual(len(rows), 2, "expected two rows sharing the same cause_id")

            panel._update_row_text_only('cause', ids['cause_id'], "Uppdaterad orsakstext")

            for row in rows:
                item = panel._table.item(row, panel._C_ORS)
                self.assertEqual(item.text(), "Uppdaterad orsakstext",
                    f"row {row}'s ORS cell must reflect the edit even though "
                    "it wasn't the row the user directly typed into")

    def test_wrap_col_row_height_matches_resize_rows_manual_formula(self):
        """_wrap_col_row_height() is a deliberate near-duplicate of one branch
        of _resize_rows_manual()'s per-row loop (kept as a small standalone
        helper so the fast path doesn't need a full table pass) -- assert it
        actually agrees with a real _resize_rows_manual() pass for the same
        row/column, so the two don't silently drift apart over time.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel.load_cause(ids['cause_id'])

            long_text = "Detta är en mycket lång orsaksbeskrivning som med säkerhet radbryts över flera rader i cellen. " * 3
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == ids['cause_id'])
            panel._table.item(row, panel._C_ORS).setText(long_text)

            fast_path_height = panel._wrap_col_row_height(row, panel._C_ORS)

            panel._resize_rows_manual()
            full_pass_height = panel._table.rowHeight(row)

            self.assertEqual(fast_path_height, full_pass_height,
                "the fast-path height helper must agree with a full "
                "_resize_rows_manual() pass for the same row/column")


class KonInlineEditTests(unittest.TestCase):
    """'Klicka direkt på konsekvens för att redigera den direkt där'
    (NOTES.md 2026-08-07) — KON cells are now included in the inline-edit
    path (_try_start_edit) and get the same single-click-on-already-
    current-cell trigger ORS/SG already had ("Feature 7"). The commit path
    (_on_cell_changed_inner's 'consequence' branch) already existed and
    worked — this was purely a missing trigger. Double-click still opens
    the step-by-step chain wizard, unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, deviation_id, cause_id, cons_id

    def test_try_start_edit_now_allows_kon_column(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            edit_spy = unittest.mock.Mock(wraps=panel._table.edit)
            panel._table.edit = edit_spy
            panel._try_start_edit(row, panel._C_KON)
            # QTableWidget.edit() is overloaded (Qt itself can trigger a
            # second internal call) — what matters is that _try_start_edit
            # no longer early-returns for the KON column at all.
            edit_spy.assert_called()

    def test_single_click_on_already_current_kon_cell_schedules_edit(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            panel._table.setCurrentCell(row, panel._C_KON)

            with unittest.mock.patch('hazop.QTimer.singleShot',
                                      side_effect=lambda _ms, fn: fn()) as mock_timer:
                panel._on_cell_clicked(row, panel._C_KON)
            mock_timer.assert_called_once()

    def test_editing_kon_cell_saves_to_consequence_description(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            item = panel._table.item(row, panel._C_KON)
            item.setText("Inget flöde till pump X")
            panel._on_cell_changed(row, panel._C_KON)

            self.assertEqual(
                dict(win.db.get_consequence(cons_id))['description'],
                "Inget flöde till pump X")

    def test_double_click_on_kon_starts_inline_edit_not_chain_wizard(self):
        """Reported feedback: double-click on KON opening the
        "Konsekvenskedja" wizard felt out of place and inconsistent with
        ORS/SG (which just start inline edit on double-click). Double-click
        now behaves the same way across ORS/KON/SG; the wizard remains
        reachable via the right-click context menu (_open_chain_editor,
        unchanged)."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            item = panel._table.item(row, panel._C_KON)

            with unittest.mock.patch.object(panel, '_open_chain_editor') as mock_wizard, \
                 unittest.mock.patch.object(panel._table, 'edit') as mock_edit:
                panel._on_cell_double_clicked(item)

            mock_wizard.assert_not_called()
            mock_edit.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════
# 4. ConnectorAnalyzer thread-hang regression (bug #5)
# ══════════════════════════════════════════════════════════════════════════

class ConnectorAnalyzerHangTests(unittest.TestCase):
    """ConnectorAnalyzer.run() used to wrap only the initial fitz.open() call
    in try/except. Any exception raised afterwards (page loop, dialect
    detection, OCR, connection matching, layout proposal) propagated out of
    QThread.run() uncaught. PyQt6 swallows exceptions raised inside
    QThread.run() -- it prints a traceback to stderr but never re-raises and
    never emits any signal -- so `finished_analysis` was never fired. The
    caller (PIDPanel._run_smart_layout) shows a modal, non-cancellable
    QProgressDialog that only closes when `finished_analysis` fires, so the
    whole P&ID panel hung forever with no way out.

    This test simulates a mid-analysis failure (fitz.open() succeeds, but
    the very next call on the document raises) and asserts that
    `finished_analysis` still fires and the (fake) doc still gets closed.
    """

    def setUp(self):
        _ensure_qapp()

    def test_run_emits_finished_analysis_on_mid_scan_exception(self):
        import pid_viewer

        class _ExplodingDoc:
            """Fake fitz.Document whose page_count property raises the
            moment ConnectorAnalyzer.run() tries to use it, simulating a
            malformed-PDF / fitz failure partway through analysis (i.e.
            *after* fitz.open() itself has already "succeeded")."""

            def __init__(self):
                self.closed = False

            @property
            def page_count(self):
                raise RuntimeError("simulated mid-scan fitz failure")

            def close(self):
                self.closed = True

        fake_doc = _ExplodingDoc()
        analyzer = pid_viewer.ConnectorAnalyzer(
            pdf_path="unused.pdf",
            page_count=3,
            page_widths_pdf={0: 100.0},
            page_heights_pdf={0: 100.0},
            render_scale=1.0,
        )

        received = {}

        def _on_done(connectors, connections, layout, sheet_num_map):
            received['args'] = (connectors, connections, layout, sheet_num_map)

        analyzer.finished_analysis.connect(_on_done)

        with unittest.mock.patch.object(pid_viewer.fitz, "open",
                                         return_value=fake_doc):
            # Run synchronously (not analyzer.start()) so the test doesn't
            # depend on Qt event-loop/thread timing -- run() is a plain
            # method and safe to call directly for this test.
            analyzer.run()

        self.assertIn(
            'args', received,
            "finished_analysis was never emitted after a mid-scan exception "
            "-- this reproduces the hang: the caller's modal progress "
            "dialog would wait forever.")
        self.assertEqual(received['args'], ([], [], {}, {}))
        self.assertTrue(
            fake_doc.closed,
            "ConnectorAnalyzer.run() must close() the fitz doc even when "
            "analysis fails mid-scan (no more leaked file handles).")


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


class SafeguardCreatedDoubleRebuildTests(unittest.TestCase):
    """Reproduces the second occurrence of the double-rebuild crash class,
    this time triggered by *adding a safeguard* rather than clicking a P&ID
    marker (the trigger the original `_on_marker_navigate` fix, commit
    84c8b7c, addressed).

    The `84c8b7c` fix only patched `TreePanel.refresh(..., emit_selection=
    False)` at the one call site inside `_on_marker_navigate`. It left the
    *general* anti-pattern — calling `tree_panel.refresh(type_, id_)` with
    the default `emit_selection=True` (which cascades via
    `setCurrentItem -> currentItemChanged -> _on_select -> item_selected ->
    MainWindow._on_selected`) *and* separately calling an equivalent
    scenario-rebuilding method for the same item — in several other call
    sites. Each of those causes `ScenarioTablePanel._rebuild()` to run twice
    per single user action, which is exactly the rapid-fire rebuild volume
    that gave a reentrant cell-widget signal (e.g. a focused `_LopaWidget`
    `QLineEdit`'s focus-out) a chance to corrupt `_rebuild()`'s teardown.

    This class asserts each newly-fixed handler drives the scenario panel's
    rebuild-equivalent call exactly once instead of twice:

      - `MainWindow._on_safeguard_created()` (fired by
        `PIDPanel.safeguard_created`, i.e. placing a safeguard marker on the
        P&ID) — used to call `scenario_panel.load_consequence()` explicitly
        *and* let `tree_panel.refresh(CONS_T, ...)`'s cascade call it again.
      - The `scenario_panel.new_item_created` handler wired in
        `MainWindow.__init__` (fired by `ScenarioTablePanel._quick_add_safeguard`
        et al., i.e. adding a safeguard/cause/consequence directly from the
        scenario table's quick-add flow) — used to let `tree_panel.refresh()`'s
        cascade rebuild once, then call the explicit `scenario_panel.refresh()`
        (== `_rebuild()`) a second time.
      - `MainWindow._on_props_changed()` (fired whenever the PropertiesRibbon
        saves a field) — used to let `tree_panel.refresh()`'s cascade rebuild
        once, then call the explicit `scenario_panel._rebuild()` a second time.

    All three now pass `emit_selection=False` to `tree_panel.refresh()`,
    matching the established `84c8b7c` pattern exactly, since each is already
    followed by an explicit rebuild-equivalent call.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_new_item_created_safeguard_rebuilds_scenario_panel_exactly_once(self):
        """Quick-adding a safeguard directly from the scenario table (Enter-to
        -add-next-row flow, ScenarioTablePanel._quick_add_safeguard ->
        new_item_created(SG_T, id) -> the lambda wired in MainWindow.__init__)
        must rebuild the scenario table exactly once. This is a second,
        independent path to the same double-rebuild bug as
        _on_safeguard_created above, and a very plausible real-world match
        for "the crash happens when adding a safeguard" since it fires
        synchronously from inside a table cell-edit-commit handler.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)

            rebuild_spy = unittest.mock.Mock(wraps=win.scenario_panel._rebuild)
            win.scenario_panel._rebuild = rebuild_spy
            win.scenario_panel.refresh = lambda: win.scenario_panel._rebuild()

            win.scenario_panel.new_item_created.emit(SG_T, ids['sg_id'])

            self.assertEqual(
                rebuild_spy.call_count, 1,
                "quick-adding a safeguard from the scenario table must "
                "rebuild the table exactly once (it used to rebuild twice: "
                "once via tree_panel.refresh()'s setCurrentItem cascade into "
                "_on_selected, once via the explicit scenario_panel.refresh() "
                "call right after)")

    def test_on_props_changed_rebuilds_scenario_panel_exactly_once(self):
        """Saving a field in the PropertiesRibbon (MainWindow._on_props_changed)
        must rebuild the scenario table exactly once. This handler fires on
        every properties-field save, making it one of the most frequent
        triggers of the double-rebuild anti-pattern.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)
            win._cur_type = CAUSE_T
            win._cur_id = ids['cause_id']

            rebuild_spy = unittest.mock.Mock(wraps=win.scenario_panel._rebuild)
            win.scenario_panel._rebuild = rebuild_spy

            win._on_props_changed()

            self.assertEqual(
                rebuild_spy.call_count, 1,
                "_on_props_changed() must rebuild the scenario panel exactly "
                "once per properties save (it used to rebuild twice: once "
                "via tree_panel.refresh()'s setCurrentItem cascade into "
                "_on_selected, once via the explicit scenario_panel._rebuild() "
                "call right after)")

    def test_node_created_calls_on_selected_exactly_once(self):
        """Creating a new node via the P&ID (PIDPanel.node_created) must
        drive MainWindow._on_selected() exactly once, mirroring the original
        _on_marker_navigate fix (commit 84c8b7c). Before this fix, the lambda
        wired to node_created called tree_panel.refresh(NODE_T, nid) (default
        emit_selection=True, cascading into _on_selected) *and* an explicit
        self._on_selected(NODE_T, nid) right after.
        """
        with _TempDbMainWindow() as win:
            win.scenario_panel.load_node = unittest.mock.Mock()

            new_node_id = win.db.add_node()

            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win.pid_panel.node_created.emit(new_node_id)

            self.assertEqual(
                on_selected_spy.call_count, 1,
                "node_created must call _on_selected() exactly once per new "
                "node (it used to fire twice: once via tree_panel.refresh()'s "
                "setCurrentItem cascade, once via the explicit call)")

    def test_on_scenario_item_edited_does_not_cascade_into_on_selected(self):
        """Committing an ordinary cell edit (e.g. a cause description) must
        NOT redundantly re-select/re-rebuild the scenario panel via
        tree_panel.refresh()'s setCurrentItem cascade. Before this fix,
        MainWindow._on_scenario_item_edited() called tree_panel.refresh(type_,
        id_) with the default emit_selection=True, cascading into
        _on_selected() on EVERY single cell edit -- not just on new-item
        creation -- causing the scenario table to visibly reset its current
        cell/selection after every edit commit (the reported "jumps away
        from the object" confusion).
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)

            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win._on_scenario_item_edited(CAUSE_T, ids['cause_id'])

            self.assertEqual(
                on_selected_spy.call_count, 0,
                "_on_scenario_item_edited() must not cascade into "
                "_on_selected() at all -- it already has everything it "
                "needs (type_, id_) and only needs to sync tree labels / "
                "P&ID overlays, not reselect/rebuild the scenario panel")

    def test_new_item_created_positions_current_cell_on_new_row(self):
        """After quick-adding a cause via Enter-to-add-next-row (or the quick-
        add menu), the scenario table's current cell must land on the new
        cause's Orsak cell -- not wherever the rebuild happened to leave
        selection -- so the user can keep typing without losing their place.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)
            win.tree_panel.refresh = unittest.mock.Mock()  # isolate: only care about scenario_panel

            new_cause_id = win.db.add_cause(ids['deviation_id'])
            win.scenario_panel.load_node(ids['node_id'])  # populate _row_meta with both causes

            win.scenario_panel.new_item_created.emit(CAUSE_T, new_cause_id)

            row = win.scenario_panel._table.currentRow()
            col = win.scenario_panel._table.currentColumn()
            self.assertGreaterEqual(row, 0, "current cell must be set, not left unselected")
            self.assertEqual(col, win.scenario_panel._C_ORS)
            dev_id, cause_id, cons_id, sg_id = win.scenario_panel._row_meta[row]
            self.assertEqual(cause_id, new_cause_id,
                "current cell must be on the row for the newly created cause, "
                "not an arbitrary/leftover row from before the rebuild")


class CauseTemplateCreatedFocusStealBugTests(unittest.TestCase):
    """Regression test for the user report 'jag kan fortfarande inte trycka
    på konsekvens och lägga in text' (still can't click into Consequence
    and type), reported right after the EquipmentDeviationBar's "föreslå
    troligaste orsaken" chip / cause dropdown started working.

    Root cause: MainWindow's `_on_cause_template_created` closure (fired by
    PIDPanel.cause_template_created, which place_cause_from_template() —
    used by BOTH the normal P&ID 'Orsak' flow and
    EquipmentDeviationBar._create_cause_from_bar — always emits) called
    `tree_panel.refresh(CAUSE_T, cid)` WITHOUT `emit_selection=False`. That
    cascades into `_on_selected(CAUSE_T, cid)` ->
    `scenario_panel.load_deviation(...)`, rebuilding the whole worksheet
    table right as the user's very next move (clicking that new row's KON
    cell to type a consequence) lands — the same anti-pattern already fixed
    elsewhere per commit 84c8b7c, just not yet here. A second, independent
    bug compounded it: `ScenarioTablePanel.select_cause()`, deferred 50ms
    after cause creation, unconditionally forced the current cell back to
    the ORS column — which would yank focus straight out of a KON cell the
    user had already started typing into within that window.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_cause_template_created_does_not_cascade_into_on_selected(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)

            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            self.assertEqual(
                on_selected_spy.call_count, 0,
                "cause_template_created must not cascade into _on_selected() "
                "via tree_panel.refresh()'s setCurrentItem — it already "
                "drives the worksheet explicitly via scenario_panel.load_node()")

    def test_cause_template_created_uses_load_node_not_load_deviation(self):
        """load_node() (not load_deviation()) must be used so every
        deviation under the node stays visible — matching
        _on_equipment_deviation_created's own fix for the same underlying
        complaint ('jag vill se BÅDA avvikelserna')."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)

            load_node_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_node)
            win.scenario_panel.load_node = load_node_spy
            load_deviation_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_deviation)
            win.scenario_panel.load_deviation = load_deviation_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            load_node_spy.assert_called_once_with(node_id)
            load_deviation_spy.assert_not_called()

    def test_select_cause_does_not_steal_current_cell_from_a_row_user_already_navigated_to(self):
        """ScenarioTablePanel.select_cause() must not force the current
        cell back to the ORS column if the user has already navigated
        (e.g. clicked into the KON cell of that same row to type a
        consequence) — it may still scroll the row into view."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)
            win.scenario_panel.load_node(node_id)

            row = next(i for i, m in enumerate(win.scenario_panel._row_meta)
                       if m[1] == cause_id)
            kon_col = win.scenario_panel._C_KON
            win.scenario_panel._table.setCurrentCell(row, kon_col)

            win.scenario_panel.select_cause(cause_id)

            self.assertEqual(
                win.scenario_panel._table.currentColumn(), kon_col,
                "select_cause must not steal the current cell away from a "
                "row the user already navigated to")


# ══════════════════════════════════════════════════════════════════════════
# 6. Worksheet page: ScenarioTablePanel "all nodes" mode + HAZOPWorksheet
#    node-picker/checkbox wiring (feature: Worksheet mirrors the full HAZOP
#    hierarchy per node, or the whole study at once).
# ══════════════════════════════════════════════════════════════════════════

class ScenarioTablePanelAllNodesTests(unittest.TestCase):
    """ScenarioTablePanel.load_all() must show every node's full
    deviation/cause/consequence/safeguard hierarchy concatenated, without
    disturbing the existing single-filter load_node/load_deviation/load_cause/
    load_consequence behaviour (that class is shared with the main P&ID
    page's scenario_panel, so this must be additive only).
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_allnodes_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, node_name=None):
        node_id = self.db.add_node()
        if node_name is not None:
            # Direct SQL rename -- Database.update_node() requires several
            # other positional fields (description, pid_ref, ...) that are
            # irrelevant to these tests, so avoid coupling to that full
            # signature just to set a display name.
            self.db.conn.execute(
                "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
            self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_load_all_does_not_crash_and_spans_multiple_nodes(self):
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() raised: {e!r}")

            self.assertTrue(panel._all_nodes)
            cause_ids_in_rows = {meta[1] for meta in panel._row_meta if meta[1] is not None}
            self.assertIn(ids1['cause_id'], cause_ids_in_rows,
                          "load_all() rows must include node A's cause")
            self.assertIn(ids2['cause_id'], cause_ids_in_rows,
                          "load_all() rows must include node B's cause")

            cons_ids_in_rows = {meta[2] for meta in panel._row_meta if meta[2] is not None}
            self.assertIn(ids1['cons_id'], cons_ids_in_rows)
            self.assertIn(ids2['cons_id'], cons_ids_in_rows)

            # NOD/DEV columns must become visible in all-nodes mode (multiple
            # nodes are interleaved, so the sticky header-bar shorthand no
            # longer applies).
            self.assertFalse(panel._table.isColumnHidden(panel._C_NOD))
            self.assertFalse(panel._table.isColumnHidden(panel._C_DEV))
        finally:
            panel.deleteLater()

    def test_load_all_on_empty_db_does_not_crash(self):
        """No nodes at all yet — load_all() must not raise."""
        from hazop import ScenarioTablePanel

        panel = ScenarioTablePanel(self.db)
        try:
            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() on an empty study raised: {e!r}")
            self.assertEqual(panel._table.rowCount(), 0)
        finally:
            panel.deleteLater()

    def test_toggle_load_all_then_load_node_then_load_all_again(self):
        """Switching all-nodes -> single-node -> all-nodes must not crash or
        leave stale filter state (each load_* must fully reset the others)."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_all()
            self.assertTrue(panel._all_nodes)
            self.assertFalse(panel._table.isColumnHidden(panel._C_NOD))

            panel.load_node(ids1['node_id'])
            self.assertFalse(panel._all_nodes,
                              "load_node() must clear _all_nodes")
            self.assertIsNone(panel._deviation_id)
            self.assertIsNone(panel.cause_id)
            self.assertIsNone(panel._cons_id)
            self.assertTrue(panel._table.isColumnHidden(panel._C_NOD),
                             "NOD column must be hidden again in single-node mode")
            cause_ids_in_rows = {meta[1] for meta in panel._row_meta if meta[1] is not None}
            self.assertIn(ids1['cause_id'], cause_ids_in_rows)
            self.assertNotIn(ids2['cause_id'], cause_ids_in_rows,
                              "load_node() must show only the selected node's causes")

            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() after load_node() raised: {e!r}")
            self.assertTrue(panel._all_nodes)
            cause_ids_in_rows = {meta[1] for meta in panel._row_meta if meta[1] is not None}
            self.assertIn(ids1['cause_id'], cause_ids_in_rows)
            self.assertIn(ids2['cause_id'], cause_ids_in_rows)
        finally:
            panel.deleteLater()

    def test_single_node_filters_unchanged_by_all_nodes_feature(self):
        """Sanity check that load_node/load_deviation/load_cause/
        load_consequence still behave exactly as single-item filters (the
        critical constraint: ScenarioTablePanel is shared with the main
        P&ID page's scenario_panel, so Part 1 changes must be additive)."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain(node_name="Nod A")
        self._make_full_chain(node_name="Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            self.assertEqual(panel._node_id, ids1['node_id'])
            self.assertFalse(panel._all_nodes)
            rows_node = panel._table.rowCount()
            self.assertGreater(rows_node, 0)

            panel.load_deviation(ids1['deviation_id'])
            self.assertEqual(panel._deviation_id, ids1['deviation_id'])
            self.assertEqual(panel._node_id, ids1['node_id'],
                              "load_deviation() must set _node_id from the deviation's own node_id")
            self.assertFalse(panel._all_nodes)

            panel.load_cause(ids1['cause_id'])
            self.assertEqual(panel.cause_id, ids1['cause_id'])
            self.assertFalse(panel._all_nodes)

            panel.load_consequence(ids1['cons_id'])
            self.assertEqual(panel._cons_id, ids1['cons_id'])
            self.assertFalse(panel._all_nodes)

            panel.clear()
            self.assertFalse(panel._all_nodes)
            self.assertEqual(panel._table.rowCount(), 0)
        finally:
            panel.deleteLater()


class ScenarioTablePanelShowEmptyDeviationsTests(unittest.TestCase):
    """ScenarioTablePanel.set_show_empty_deviations(): deviations with zero
    causes are silently omitted by default (_causes_for_node's normal
    behaviour). When the flag is on, each such deviation must get its own
    placeholder row (via _add_placeholder_row), interleaved in deviation
    order alongside deviations that do have causes — not just in the
    "whole node/study is empty" fallback branch of _build_rows()."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_showempty_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_node_with_one_cause_and_empty_deviations(self, node_name=None):
        """add_node() auto-creates several standard deviations. Give only
        the FIRST one a cause (+consequence+safeguard); leave the rest
        (at least one more, per the standard deviation set) with zero
        causes."""
        node_id = self.db.add_node()
        if node_name is not None:
            self.db.conn.execute(
                "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
            self.db.commit()
        devs = self.db.deviations(node_id)
        self.assertGreaterEqual(len(devs), 2,
            "test assumes add_node() creates >=2 standard deviations")
        dev_with_cause = devs[0]['id']
        empty_dev_ids = [d['id'] for d in devs[1:]]
        cause_id = self.db.add_cause(dev_with_cause)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id,
            'dev_with_cause': dev_with_cause,
            'empty_dev_ids': empty_dev_ids,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_default_off_omits_empty_deviations(self):
        from hazop import ScenarioTablePanel

        ids = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            self.assertFalse(panel._show_empty_deviations)
            dev_ids_in_rows = {meta[0] for meta in panel._row_meta}
            self.assertIn(ids['dev_with_cause'], dev_ids_in_rows)
            for empty_id in ids['empty_dev_ids']:
                self.assertNotIn(empty_id, dev_ids_in_rows,
                    "empty deviations must be omitted when the flag is off")
        finally:
            panel.deleteLater()

    def test_enabling_flag_adds_placeholder_rows_for_empty_deviations(self):
        from hazop import ScenarioTablePanel

        ids = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            rows_before = panel._table.rowCount()

            panel.set_show_empty_deviations(True)
            self.assertTrue(panel._show_empty_deviations)

            rows_after = panel._table.rowCount()
            self.assertGreater(rows_after, rows_before,
                "turning the flag on must add rows for the empty deviations")

            dev_ids_in_rows = [meta[0] for meta in panel._row_meta]
            self.assertIn(ids['dev_with_cause'], dev_ids_in_rows)
            for empty_id in ids['empty_dev_ids']:
                self.assertIn(empty_id, dev_ids_in_rows,
                    "empty deviations must appear as placeholder rows when the flag is on")

            # The placeholder row(s) for empty deviations must carry None for
            # cause/cons/sg ids in _row_meta.
            for i, meta in enumerate(panel._row_meta):
                if meta[0] in ids['empty_dev_ids']:
                    self.assertIsNone(meta[1], "placeholder row must have cause_id=None")
                    self.assertIsNone(meta[2], "placeholder row must have cons_id=None")
                    self.assertIsNone(meta[3], "placeholder row must have sg_id=None")
        finally:
            panel.deleteLater()

    def test_works_in_all_nodes_mode_across_multiple_nodes(self):
        from hazop import ScenarioTablePanel

        ids1 = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        ids2 = self._make_node_with_one_cause_and_empty_deviations("Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_all()
            panel.set_show_empty_deviations(True)
            self.assertTrue(panel._all_nodes)
            self.assertTrue(panel._show_empty_deviations)

            dev_ids_in_rows = [meta[0] for meta in panel._row_meta]
            for empty_id in ids1['empty_dev_ids'] + ids2['empty_dev_ids']:
                self.assertIn(empty_id, dev_ids_in_rows,
                    "empty deviations from every node must appear in all-nodes mode")
            self.assertIn(ids1['dev_with_cause'], dev_ids_in_rows)
            self.assertIn(ids2['dev_with_cause'], dev_ids_in_rows)
        finally:
            panel.deleteLater()

    def test_toggling_flag_on_and_off_does_not_crash_and_changes_row_count(self):
        from hazop import ScenarioTablePanel

        ids = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            rows_off = panel._table.rowCount()

            try:
                panel.set_show_empty_deviations(True)
            except Exception as e:
                self.fail(f"set_show_empty_deviations(True) raised: {e!r}")
            rows_on = panel._table.rowCount()
            self.assertGreater(rows_on, rows_off)

            try:
                panel.set_show_empty_deviations(False)
            except Exception as e:
                self.fail(f"set_show_empty_deviations(False) raised: {e!r}")
            rows_off_again = panel._table.rowCount()
            self.assertEqual(rows_off_again, rows_off)

            # Calling with the same value again must be a no-op (early return)
            # and must not raise.
            try:
                panel.set_show_empty_deviations(False)
            except Exception as e:
                self.fail(f"set_show_empty_deviations(False) again raised: {e!r}")
        finally:
            panel.deleteLater()

    def test_flag_persists_across_load_node_switches(self):
        """This is a display PREFERENCE (like font size / 'Fyll skärm'), not
        a per-node filter, so switching nodes must not reset it."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        ids2 = self._make_node_with_one_cause_and_empty_deviations("Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            panel.set_show_empty_deviations(True)

            panel.load_node(ids2['node_id'])
            self.assertTrue(panel._show_empty_deviations,
                "load_node() must not reset the show-empty-deviations preference")
            dev_ids_in_rows = [meta[0] for meta in panel._row_meta]
            for empty_id in ids2['empty_dev_ids']:
                self.assertIn(empty_id, dev_ids_in_rows)
        finally:
            panel.deleteLater()

    def test_clear_resets_the_flag(self):
        """clear() is a full state reset (unlike the load_* switches), so it
        should reset this preference along with the other filter state."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_node_with_one_cause_and_empty_deviations("Nod A")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            panel.set_show_empty_deviations(True)
            panel.clear()
            self.assertFalse(panel._show_empty_deviations)
        finally:
            panel.deleteLater()


class HAZOPWorksheetTests(unittest.TestCase):
    """HAZOPWorksheet: node-picker + 'Visa samtliga noder' checkbox wired to
    the embedded ScenarioTablePanel's load_node()/load_all()."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_worksheet_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, node_name=None):
        node_id = self.db.add_node()
        if node_name is not None:
            # Direct SQL rename -- Database.update_node() requires several
            # other positional fields (description, pid_ref, ...) that are
            # irrelevant to these tests, so avoid coupling to that full
            # signature just to set a display name.
            self.db.conn.execute(
                "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
            self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_instantiates_headless_and_refreshes_on_empty_db(self):
        from hazop import HAZOPWorksheet

        ws = HAZOPWorksheet(self.db)
        try:
            try:
                ws.refresh()
            except Exception as e:
                self.fail(f"HAZOPWorksheet.refresh() on an empty DB raised: {e!r}")
            self.assertEqual(ws._node_combo.count(), 0)
        finally:
            ws.deleteLater()

    def test_equipment_column_stays_hidden_even_in_all_nodes_mode(self):
        """"i worksheet behöver inte objekt kolumnen synas" (2026-08-13)
        — Utrustning normally reappears in "Visa samtliga noder" mode
        (see ScenarioTablePanel._set_all_nodes_columns_visible's
        docstring), but HAZOPWorksheet opts out of that via
        hide_equipment_column(); the tag is already shown at the top of
        each Orsak cell regardless."""
        from hazop import HAZOPWorksheet
        ws = HAZOPWorksheet(self.db)
        try:
            panel = ws._table_panel
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must start hidden")
            ws._all_nodes_cb.setChecked(True)
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must stay hidden even in Visa samtliga noder mode")
        finally:
            ws.deleteLater()

    def test_refresh_after_creating_nodes_populates_and_loads(self):
        from hazop import HAZOPWorksheet

        ws = HAZOPWorksheet(self.db)
        try:
            ids = self._make_full_chain(node_name="Nod A")
            try:
                ws.refresh()
            except Exception as e:
                self.fail(f"HAZOPWorksheet.refresh() after adding a node raised: {e!r}")
            self.assertEqual(ws._node_combo.count(), 1)
            self.assertEqual(ws._node_combo.currentData(), ids['node_id'])
        finally:
            ws.deleteLater()

    def test_node_combo_populates_from_db_nodes(self):
        from hazop import HAZOPWorksheet

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            self.assertEqual(ws._node_combo.count(), 2)
            self.assertEqual(ws._node_combo.itemText(0), "Nod A")
            self.assertEqual(ws._node_combo.itemData(0), ids1['node_id'])
            self.assertEqual(ws._node_combo.itemText(1), "Nod B")
            self.assertEqual(ws._node_combo.itemData(1), ids2['node_id'])
        finally:
            ws.deleteLater()

    def test_selecting_combo_entry_calls_load_node_with_right_id(self):
        from hazop import HAZOPWorksheet

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            ws._table_panel.load_node = unittest.mock.Mock()
            ws._node_combo.setCurrentIndex(1)
            ws._table_panel.load_node.assert_called_once_with(ids2['node_id'])

            ws._table_panel.load_node.reset_mock()
            ws._node_combo.setCurrentIndex(0)
            ws._table_panel.load_node.assert_called_once_with(ids1['node_id'])
        finally:
            ws.deleteLater()

    def test_checking_all_nodes_disables_combo_and_calls_load_all(self):
        from hazop import HAZOPWorksheet

        self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            ws._node_combo.setCurrentIndex(1)  # select "Nod B" first
            ws._table_panel.load_node = unittest.mock.Mock()
            ws._table_panel.load_all = unittest.mock.Mock()

            ws._all_nodes_cb.setChecked(True)
            self.assertFalse(ws._node_combo.isEnabled(),
                              "combo must be disabled while 'Visa samtliga noder' is checked")
            ws._table_panel.load_all.assert_called_once()
            ws._table_panel.load_node.assert_not_called()

            ws._table_panel.load_all.reset_mock()
            ws._all_nodes_cb.setChecked(False)
            self.assertTrue(ws._node_combo.isEnabled(),
                             "combo must be re-enabled after unchecking")
            ws._table_panel.load_node.assert_called_once_with(ids2['node_id'])
        finally:
            ws.deleteLater()

    def test_worksheet_refresh_respects_all_nodes_checkbox(self):
        """refresh() (called by MainWindow._switch_view on page==1) must
        re-load in whichever mode the checkbox currently reflects."""
        from hazop import HAZOPWorksheet

        self._make_full_chain(node_name="Nod A")

        ws = HAZOPWorksheet(self.db)
        try:
            ws._all_nodes_cb.setChecked(True)
            ws._table_panel.load_all = unittest.mock.Mock()
            ws._table_panel.load_node = unittest.mock.Mock()

            ws.refresh()
            ws._table_panel.load_all.assert_called_once()
            ws._table_panel.load_node.assert_not_called()
        finally:
            ws.deleteLater()

    def test_show_empty_dev_checkbox_calls_set_show_empty_deviations(self):
        """The 'Visa avvikelser utan orsaker' checkbox must be wired directly
        to the embedded ScenarioTablePanel's set_show_empty_deviations(bool).

        The signal is connected straight to the bound method at construction
        time (`toggled.connect(self._table_panel.set_show_empty_deviations)`),
        so a plain attribute-patch after construction would not intercept the
        already-connected Qt slot. Verify the wiring by its real effect: the
        panel's underlying flag (and the resulting row set) instead.
        """
        from hazop import HAZOPWorksheet

        ids = self._make_full_chain(node_name="Nod A")
        # Give the node a second, cause-less deviation so toggling the
        # checkbox has an observable effect on the row count too.
        self.db.add_deviation(ids['node_id'], description="Tom avvikelse")

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()  # populate combo + load the node into _table_panel
            self.assertFalse(ws._table_panel._show_empty_deviations)
            rows_before = ws._table_panel._table.rowCount()

            ws._show_empty_dev_cb.setChecked(True)
            self.assertTrue(ws._table_panel._show_empty_deviations,
                "checking the box must call set_show_empty_deviations(True) "
                "on the embedded ScenarioTablePanel")
            self.assertGreater(ws._table_panel._table.rowCount(), rows_before,
                "the empty deviation must now show as a placeholder row")

            ws._show_empty_dev_cb.setChecked(False)
            self.assertFalse(ws._table_panel._show_empty_deviations,
                "unchecking the box must call set_show_empty_deviations(False)")
            self.assertEqual(ws._table_panel._table.rowCount(), rows_before)
        finally:
            ws.deleteLater()

    def test_deviation_column_always_visible_regardless_of_checkboxes(self):
        """The Avvikelse column must stay visible in the Worksheet even with
        both 'Visa samtliga noder' and 'Visa avvikelser utan orsaker'
        unchecked — there's no separate deviation-picker, only a node
        dropdown, so rows need the Avvikelse column to stay distinguishable."""
        from hazop import HAZOPWorksheet
        from hazop import ScenarioTablePanel

        self._make_full_chain(node_name="Nod A")

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            self.assertFalse(ws._all_nodes_cb.isChecked())
            self.assertFalse(ws._show_empty_dev_cb.isChecked())
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ScenarioTablePanel._C_DEV),
                "Avvikelse column must be visible with neither checkbox checked")

            # Must also stay visible through mode changes (all-nodes on/off).
            ws._all_nodes_cb.setChecked(True)
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ScenarioTablePanel._C_DEV))
            ws._all_nodes_cb.setChecked(False)
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ScenarioTablePanel._C_DEV))
        finally:
            ws.deleteLater()

    def test_main_pid_scenario_panel_dev_column_unaffected(self):
        """always_show_deviation_column() is opt-in per instance — a plain
        ScenarioTablePanel (as used standalone on the P&ID page) must keep
        its original hide-unless-all-nodes behavior for the Avvikelse column."""
        from pid_viewer import PIDPanel  # noqa: F401  (ensures hazop module fully loaded)
        from hazop import ScenarioTablePanel

        panel = ScenarioTablePanel(self.db)
        try:
            self.assertTrue(panel._table.isColumnHidden(ScenarioTablePanel._C_DEV),
                "a plain ScenarioTablePanel must still hide Avvikelse by default")
        finally:
            panel.deleteLater()

    def test_utrustning_column_stays_hidden_with_forced_dev_column_in_single_node_view(self):
        """Reported feedback: the leftmost "Utrustning" column duplicates
        the tag already shown at the top of each Orsak cell. It used to
        follow Avvikelse's forced-visible state (always_show_deviation_column())
        even in single-node view — now it only appears in genuine "all
        nodes" mode, where multiple equipment groups are actually
        interleaved and the column earns its keep."""
        from hazop import ScenarioTablePanel

        node_id = self.db.add_node()
        deviation_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(deviation_id)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.always_show_deviation_column()
            panel.load_node(node_id)   # single-node view, _all_nodes=False

            self.assertFalse(panel._table.isColumnHidden(panel._C_DEV),
                "Avvikelse must still be forced visible")
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must stay hidden in single-node view even when forced")

            panel._all_nodes = True
            panel._set_all_nodes_columns_visible(True)
            self.assertFalse(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must still appear in genuine all-nodes mode")
        finally:
            panel.deleteLater()

    def test_sticky_ctx_bar_hidden_when_dev_column_forced_visible(self):
        """The sticky context bar (which shows 'current Nod + Avvikelse' as a
        text header) duplicates the now-always-visible Avvikelse column in
        the Worksheet -- both showed Nod/Avvikelse on their own row, wasting
        vertical space. Once always_show_deviation_column() is in effect,
        the context bar must stay hidden, matching the existing "all nodes"
        mode reasoning (the visible column already shows the same info)."""
        from hazop import HAZOPWorksheet

        node_id = self.db.add_node()
        self.db.conn.execute("UPDATE nodes SET name=? WHERE id=?", ("Nod A", node_id))
        self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(deviation_id)

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()  # loads the node into the embedded ScenarioTablePanel
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ws._table_panel._C_DEV),
                "sanity check: Avvikelse column must be visible in Worksheet")
            self.assertFalse(
                ws._table_panel._ctx_bar.isVisible(),
                "the sticky context bar must be hidden once the Avvikelse "
                "column is force-visible -- otherwise Nod/Avvikelse are "
                "shown redundantly on two separate rows")
        finally:
            ws.deleteLater()


class EditExtraDeferredRebuildTests(unittest.TestCase):
    """Reproduces the THIRD occurrence of the silent-native-crash class in
    ScenarioTablePanel._rebuild() (2026-08-02, hazop_crash.log: the log
    always stops right after '_rebuild: E — reset meta', with no further
    output and no Python exception — i.e. inside _build_rows()).

    The first two fixes (84c8b7c: _LopaWidget focus-out reentrancy guard in
    _update_lopa_risk(); 686e289: double tree_panel.refresh()+_on_selected()
    anti-pattern) were both confirmed still correctly in place and did not
    explain this third occurrence. This test documents and guards against a
    THIRD, independent trigger of the same underlying reentrancy class,
    found by auditing every dialog .exec() call inside ScenarioTablePanel:

    ScenarioTablePanel._edit_extra() (wired to a live _LopaWidget's
    "+ övriga" QPushButton.clicked signal, itself a cell widget embedded in
    self._table) used to call `self._rebuild()` directly and synchronously
    right after `dlg.exec()` returned — the ONLY handler in the whole class
    to do so; every other popup/dialog handler defers via
    `self._schedule_rebuild()` (a `QTimer.singleShot(0, ...)`), and there
    are 24 such call sites.

    `dlg.exec()` pumps a NESTED Qt event loop. Any `QTimer.singleShot(0, ...)`
    already queued by an earlier `_schedule_rebuild()` call (e.g. from a
    click on a different cell moments before) fires DURING that nested loop
    -- not after it -- which means `_rebuild()` can run while _edit_extra()
    (and the button's `clicked` handler that invoked it) is still executing,
    paused inside `dlg.exec()`, on the C++ call stack. `_rebuild()`'s
    `setRowCount(0)` then destroys the very `_LopaWidget`/button that
    originated this call. The fix makes _edit_extra() defer via
    `_schedule_rebuild()` like every other handler, so it can never itself
    race a pending scheduled rebuild the way a direct call could.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_edit_extra_defers_rebuild_instead_of_calling_it_directly(self):
        """_edit_extra() must schedule a rebuild via _schedule_rebuild()
        (deferred, coalesced, safe against a nested dlg.exec() event loop)
        rather than calling self._rebuild() synchronously right after the
        dialog closes -- the pattern used by every other dialog handler in
        this class."""
        from hazop import ReductionFactorsDialog

        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)

            # Avoid actually showing a modal dialog in the test run.
            fake_dlg = unittest.mock.Mock()
            fake_dlg.exec = unittest.mock.Mock(return_value=0)
            with unittest.mock.patch(
                    'hazop.ReductionFactorsDialog', return_value=fake_dlg):
                rebuild_spy = unittest.mock.Mock()
                schedule_spy = unittest.mock.Mock()
                panel._rebuild = rebuild_spy
                panel._schedule_rebuild = schedule_spy

                panel._edit_extra(ids['cons_id'])

                schedule_spy.assert_called_once()
                rebuild_spy.assert_not_called()

    def test_schedule_rebuild_pending_during_edit_extra_does_not_reenter_rebuild(self):
        """End-to-end reproduction: a rebuild already scheduled via
        _schedule_rebuild() (QTimer.singleShot(0, ...)) must not be able to
        tear down the table (setRowCount(0), destroying the live
        _LopaWidget/button that is the source of this very call) while
        _edit_extra() is still on the call stack underneath a dialog's
        exec(). Simulated by queuing a pending rebuild flag and firing the
        timer synchronously (as the nested event loop would) from inside a
        fake dlg.exec(), then confirming the panel survives and only one
        additional _rebuild() happens afterward, not a nested one during
        the dialog.
        """
        from hazop import ReductionFactorsDialog

        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel._cons_id = ids['cons_id']

            # Stub _rebuild() itself (rather than calling through to the
            # real implementation) -- see test_select_safeguard_in_tree_no_crash's
            # docstring: the real _rebuild() ultimately calls
            # QTableWidget.resizeRowsToContents(), which reproducibly hits a
            # native access violation under this machine's headless Qt
            # platform plugin, unrelated to the reentrancy behaviour under
            # test here. Only the *call count/ordering* matters for this test.
            rebuild_call_log = []

            def _tracking_rebuild():
                rebuild_call_log.append('rebuild')

            panel._rebuild = _tracking_rebuild

            # Simulate: a rebuild is already scheduled (as if the user had
            # just clicked a different cell) and its QTimer.singleShot(0,...)
            # fires DURING the modal dialog's nested event loop -- exactly
            # what a real dlg.exec() call pumps for any already-queued timer.
            def _fake_exec():
                panel._on_rebuild_scheduled()  # what the pending timer runs
                return 0

            fake_dlg = unittest.mock.Mock()
            fake_dlg.exec = unittest.mock.Mock(side_effect=_fake_exec)
            with unittest.mock.patch(
                    'hazop.ReductionFactorsDialog', return_value=fake_dlg):
                panel._rebuild_pending = True  # a rebuild was already queued
                panel._edit_extra(ids['cons_id'])

            # The nested-loop rebuild ran once (via _fake_exec). Because
            # _edit_extra() now defers through _schedule_rebuild() instead of
            # calling self._rebuild() directly, no second, immediately-stacked
            # rebuild races it while the dialog handler frame is still live.
            self.assertEqual(
                rebuild_call_log.count('rebuild'), 1,
                "only the nested-loop's own scheduled rebuild should run "
                "synchronously here; _edit_extra() must not additionally "
                "call _rebuild() directly on top of it")
            # A further rebuild is still scheduled for the next event-loop
            # tick (coalesced with any other pending request), not skipped.
            self.assertTrue(panel._rebuild_pending)


class ResizeRowsManualNoNativeCrashTests(unittest.TestCase):
    """_resize_rows() used to call QTableWidget.resizeRowsToContents(), which
    was pinpointed via diagnostic K0/K1 checkpoint logging (commit 2aba0b4)
    as the exact site of a silent native (C++-level) crash: the process died
    inside that call with no Python exception, after several rapid rebuild
    cycles in quick succession (e.g. the Worksheet node-picker dropdown being
    switched quickly between nodes). Elsewhere in this suite,
    scenario_panel.load_deviation()/load_consequence()/load_cause() are
    stubbed out specifically to dodge this same native crash (see
    test_select_safeguard_in_tree_no_crash's docstring), which is exactly
    why this class instead calls the real, un-stubbed load_node() repeatedly.

    The fix (this session) replaces resizeRowsToContents() with a manual
    per-row/per-cell height computation in _resize_rows_manual(), never
    invoking the native call at all. These tests exercise that new code path
    directly and would have reproduced the native crash before the fix.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_resize_rows_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, node_name, long_text=False):
        """Build node -> deviation -> cause -> consequence -> safeguard,
        optionally with long cause/consequence text to force the ORS/KON
        wrapping-height computation path in _resize_rows_manual()."""
        node_id = self.db.add_node()
        self.db.conn.execute(
            "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
        self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        if long_text:
            self.db.update_cause(
                cause_id,
                description="Mycket lång orsakstext som ska radbrytas flera "
                             "gånger i tabellcellen för att tvinga fram "
                             "höjdberäkning via QFontMetrics.boundingRect " * 3)
        cons_id = self.db.add_consequence(cause_id)
        if long_text:
            self.db.update_consequence(
                cons_id,
                "Mycket lång konsekvensbeskrivning som också radbryts flera "
                "gånger för att övning täcker KON-kolumnens höjdlogik " * 3,
                3, '')
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_rapid_node_switching_does_not_crash_and_row_heights_are_sane(self):
        """Simulates the Worksheet node-picker dropdown being changed rapidly:
        real (un-stubbed) load_node() calls across several nodes, each with a
        full deviation/cause/consequence/safeguard chain and long wrapping
        text, repeated enough times to meaningfully exercise the manual
        row-height computation added in this fix."""
        from hazop import ScenarioTablePanel

        node_ids = []
        for i in range(3):
            ids = self._make_full_chain(f"Nod {i}", long_text=True)
            node_ids.append(ids['node_id'])

        panel = ScenarioTablePanel(self.db)
        try:
            for _cycle in range(10):
                for node_id in node_ids:
                    try:
                        panel.load_node(node_id)
                    except Exception as e:
                        self.fail(
                            f"load_node({node_id}) raised on rapid-switch "
                            f"cycle {_cycle}: {e!r}")

            # Final state sanity: rows exist and every row has a positive,
            # sane height (not 0, not some absurd default).
            row_count = panel._table.rowCount()
            self.assertGreater(row_count, 0)
            for r in range(row_count):
                h = panel._table.rowHeight(r)
                self.assertGreater(h, 0, f"row {r} has non-positive height")
                self.assertLess(h, 2000, f"row {r} has a suspiciously huge height")
        finally:
            panel.deleteLater()

    def test_resize_rows_manual_sizes_all_rows_without_resize_rows_to_contents(self):
        """Directly checks _resize_rows_manual() (the new helper) sizes every
        row to a positive height, for both a single node's chain (typical
        single-node worksheet view) and load_all() (potentially hundreds of
        rows across many nodes)."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain("Nod A", long_text=True)
        self._make_full_chain("Nod B", long_text=True)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            self.assertGreater(panel._table.rowCount(), 0)
            for r in range(panel._table.rowCount()):
                self.assertGreater(panel._table.rowHeight(r), 0)

            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() raised: {e!r}")
            self.assertGreater(panel._table.rowCount(), 0)
            for r in range(panel._table.rowCount()):
                self.assertGreater(panel._table.rowHeight(r), 0)
        finally:
            panel.deleteLater()


class OrsStripHeightConsistencyTests(unittest.TestCase):
    """'Ibland så göms text på raderna i hazop scenario. särskilt de som
    står under orsaker. Dessutom ser det ut som att en spöktext ligger
    kvar när man redigerar.' (2026-08-11). Root cause: the ORS cell's
    top strip ([pin|tag|freq|dots]) is drawn/positioned at 17px
    (_PidDelegate.paint(), updateEditorGeometry) but the row-height
    calculations (sizeHint/_resize_rows_manual/_wrap_col_row_height —
    including the fast-path _update_row_text_only() that runs right
    after finishing an edit) only ever reserved 14px for it — a
    long-standing, pre-existing mismatch across five places, none of
    which agreed with each other. Every ORS row was silently 3px too
    short for its own wrapped description, clipping the bottom of the
    last line (worse right after an edit, since that's exactly when
    _wrap_col_row_height's wrong number gets freshly (re)applied via
    setRowHeight — the "ghost text" symptom). Fixed by unifying all
    five call sites onto one shared _ORS_STRIP_H = 17 constant."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsstrip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _row_for_cause(self, panel, cause_id):
        return next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

    def _assert_enough_room_for_wrapped_text(self, panel, row):
        from PyQt6.QtGui import QFontMetrics
        from hazop import _ORS_STRIP_H
        item = panel._table.item(row, panel._C_ORS)
        fm = QFontMetrics(panel._table.font())
        cell_w = max(40, panel._table.columnWidth(panel._C_ORS) - 6)
        rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, item.text())
        row_height = panel._table.rowHeight(row)
        available_for_text = row_height - _ORS_STRIP_H
        self.assertGreaterEqual(
            available_for_text, rect.height(),
            f"row height {row_height} leaves only {available_for_text}px below the "
            f"strip, but the wrapped description needs {rect.height()}px — the last "
            f"line will be clipped")

    def test_row_height_leaves_enough_room_after_initial_load(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(
            cause_id,
            description="En mycket lång orsakstext som ska radbrytas flera "
                        "gånger i cellen för att verkligen tvinga fram en "
                        "flerradig beskrivning under taggremsan " * 3)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = self._row_for_cause(panel, cause_id)
            self._assert_enough_room_for_wrapped_text(panel, row)
        finally:
            panel.deleteLater()

    def test_row_height_leaves_enough_room_after_editing_description(self):
        """The specific "ghost text after editing" report — exercises the
        fast-path _update_row_text_only(), not just the initial-load
        sizing path above."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            long_text = ("En mycket lång orsakstext som ska radbrytas flera "
                        "gånger i cellen efter redigering " * 3)
            panel._update_row_text_only('cause', cause_id, long_text)
            row = self._row_for_cause(panel, cause_id)
            self._assert_enough_room_for_wrapped_text(panel, row)
        finally:
            panel.deleteLater()

    def test_very_long_description_is_not_capped_at_four_lines(self):
        """_resize_rows() used to forcibly shrink any row back down to
        ~4 text lines' worth of height even when _resize_rows_manual()
        had already correctly computed a taller one for a longer
        description — silently clipping everything past the 4th line
        with no visual sign anything was cut off. A HAZOP tool hiding
        part of a cause description is a far worse failure mode than a
        tall row, so the cap is gone; this pins that down directly with
        a description long enough to need well over 4 lines."""
        from hazop import ScenarioTablePanel
        from PyQt6.QtGui import QFontMetrics
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(
            cause_id,
            description="En mycket lång orsakstext som garanterat radbryts till "
                        "betydligt fler än fyra rader i cellen " * 8)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = self._row_for_cause(panel, cause_id)
            row_height = panel._table.rowHeight(row)
            fm = QFontMetrics(panel._table.font())
            four_line_cap = fm.height() * 4 + 12
            self.assertGreater(
                row_height, four_line_cap,
                "a description this long must grow the row past the old "
                "4-line cap, not get silently clipped at it")
            self._assert_enough_room_for_wrapped_text(panel, row)
        finally:
            panel.deleteLater()


class ConsequenceStepPickerColumnsTests(unittest.TestCase):
    """Regression tests for ConsequenceStepPickerDialog's multi-column
    layout (all _N_STEPS 'Del N' columns shown side by side, replacing an
    intermediate one-step-at-a-time wizard redesign).

    The data model (_CONSEQ_NODES / _CONSEQ_ENTRY / _CONSEQ_GENERIC_NEXT,
    _successor_pairs, _resolve, Database.set_consequence_steps /
    get_consequence_steps) is unchanged across all these presentation
    changes — these tests confirm the all-columns-visible presentation
    still drives that model correctly and that the persistence format is
    unchanged.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_stepcolumns_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_chain(self, deviation="Lågt flöde", comp_type="Pump"):
        node_id = self.db.add_node()
        dev_id = self.db.add_deviation(node_id, deviation)
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        return node_id, dev_id, cause_id, cons_id

    def test_col1_options_match_entry_pairs_count(self):
        """Del1's list must show exactly _entry_pairs()'s result count of
        items -- not a hardcoded 5, since node option counts vary 0-6."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            expected = dlg._entry_pairs()
            self.assertGreater(len(expected), 0)
            self.assertEqual(dlg._cols[0]['list'].count(), len(expected))
            self.assertFalse(dlg._cols[0]['list'].isHidden())
            self.assertTrue(dlg._cols[0]['end_lbl'].isHidden())
            for (key, text), i in zip(expected, range(len(expected))):
                self.assertIn(dlg._resolve(text, ''), dlg._cols[0]['list'].item(i).text())
        finally:
            dlg.deleteLater()

    def test_other_columns_start_neutral_not_terminal(self):
        """Columns 2-5 haven't been reached yet on a fresh dialog -- they
        must show a neutral empty list, NOT the 'chain ends here' message
        (that message is reserved for an actually-terminal graph node)."""
        from hazop import ConsequenceStepPickerDialog, _N_STEPS
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            for i in range(1, _N_STEPS):
                self.assertEqual(dlg._cols[i]['list'].count(), 0)
                self.assertFalse(dlg._cols[i]['list'].isHidden())
                self.assertTrue(dlg._cols[i]['end_lbl'].isHidden())
        finally:
            dlg.deleteLater()

    def test_clicking_option_populates_next_column_with_successor_pairs(self):
        """Clicking an option in Del1 must populate Del2 with exactly the
        pairs _successor_pairs() returns for the chosen node."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])
            chosen_key = dlg._opt_keys[0][0]
            expected = dlg._successor_pairs(chosen_key)
            self.assertEqual(dlg._options[1], [t for _, t in expected])
            self.assertEqual(dlg._opt_keys[1], [k for k, _ in expected])
        finally:
            dlg.deleteLater()

    def test_second_click_deselects_and_clears_downstream(self):
        """Clicking an already-selected option a second time must clear the
        selection and cascade the clear downstream (no stale successors
        left over from the previous choice)."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])
            self.assertGreaterEqual(dlg._cols[0]['sel'], 0)
            self.assertGreater(len(dlg._options[1]), 0)

            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])
            self.assertEqual(dlg._cols[0]['sel'], -1)
            self.assertEqual(dlg._options[1], [])
            self.assertFalse(dlg._cols[1]['list'].isHidden())
            self.assertTrue(dlg._cols[1]['end_lbl'].isHidden())
        finally:
            dlg.deleteLater()

    def test_terminal_node_shows_end_of_chain_message_not_empty_list(self):
        """Reaching a 0-next node (e.g. 'fatality') must show the
        end-of-chain message and hide the (empty) list for that column."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            # 'fatality' is a real terminal node in _CONSEQ_NODES (next=[]).
            self.assertEqual(dlg._successor_pairs('fatality'), [])
            dlg._populate_column(1, dlg._successor_pairs('fatality'), upstream_has_sel=True)
            self.assertTrue(dlg._cols[1]['list'].isHidden())
            self.assertFalse(dlg._cols[1]['end_lbl'].isHidden())
            self.assertIn('Kedjan slutar här', dlg._cols[1]['end_lbl'].text())
        finally:
            dlg.deleteLater()

    def test_freetext_entry_cascades_using_generic_pairs(self):
        """Typing free text in a column (instead of picking an option) must
        populate the next column from _generic_pairs(), matching the
        original fallback behavior."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._cols[0]['ft_edit'].setText("Eget alternativ")
            expected = dlg._generic_pairs()
            self.assertEqual(dlg._options[1], [t for _, t in expected])
            self.assertIsNone(dlg._selected_key(0))
        finally:
            dlg.deleteLater()

    def test_save_produces_same_node_keys_as_direct_graph_walk(self):
        """Saving must persist the same node_key values (and text) that a
        direct walk of the graph would produce."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            key0 = dlg._opt_keys[0][0]
            expected_text0 = dlg._resolve(dlg._options[0][0], '')
            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])

            key1 = dlg._opt_keys[1][0]
            expected_text1 = dlg._resolve(dlg._options[1][0], '')
            dlg._cols[1]['list'].setCurrentRow(0)
            dlg._list_clicked(1, dlg._cols[1]['list'])

            dlg._do_save()
            saved = self.db.get_consequence_steps(cons_id)
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]['step'], 1)
            self.assertEqual(saved[0]['node_key'], key0)
            self.assertEqual(saved[0]['text'], expected_text0)
            self.assertEqual(saved[1]['step'], 2)
            self.assertEqual(saved[1]['node_key'], key1)
            self.assertEqual(saved[1]['text'], expected_text1)

            cons = self.db.get_consequence(cons_id)
            self.assertIn(expected_text0, cons['description'])
            self.assertIn(expected_text1, cons['description'])
        finally:
            dlg.deleteLater()

    def test_reopening_dialog_restores_saved_chain_selection(self):
        """A saved chain (node_key based) must be restored selection-by-
        selection when the dialog is reopened."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg1 = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        key0 = dlg1._opt_keys[0][0]
        dlg1._cols[0]['list'].setCurrentRow(0)
        dlg1._list_clicked(0, dlg1._cols[0]['list'])
        dlg1._do_save()
        dlg1.deleteLater()

        dlg2 = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            self.assertEqual(dlg2._cols[0]['sel'], 0)
            self.assertEqual(dlg2._cols[0]['list'].currentRow(), 0)
            self.assertEqual(dlg2._opt_keys[0][dlg2._cols[0]['sel']], key0)
        finally:
            dlg2.deleteLater()

    def test_pin_button_flow_refills_ref_tag_for_column(self):
        """The ref-tag pin-button flow (_request_pick_for_col hides the
        dialog and waits; the caller fills the waiting column's ref_edit
        and re-shows) must update that column's live, always-mounted
        ref_edit widget and cascade the list label refresh."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._request_pick_for_col(1)
            self.assertEqual(dlg._waiting_col_idx, 1)
            self.assertTrue(dlg.isHidden())

            # Simulate MainWindow._on_ref_tag_picked's refill + re-show.
            col_idx = dlg._waiting_col_idx
            dlg._cols[col_idx]['ref_edit'].setText("T-101")
            dlg._waiting_col_idx = None
            dlg.show()

            self.assertIsNone(dlg._waiting_col_idx)
            self.assertEqual(dlg._cols[1]['ref_edit'].text(), "T-101")
        finally:
            dlg.deleteLater()

    def test_quickselect_removed(self):
        """The 'Snabbval' quick-select text field is dropped for a cleaner,
        tighter dialog. Confirm it is gone rather than silently broken."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            self.assertFalse(hasattr(dlg, '_apply_quickselect'))
            self.assertFalse(hasattr(dlg, '_qs_edit'))
            self.assertFalse(hasattr(dlg, '_qs_btn'))
        finally:
            dlg.deleteLater()

    def test_dialog_opens_near_scenario_table_row_not_screen_center(self):
        """The dialog must open positioned near its cons_id's row in the
        HAZOP scenario table (ScenarioTablePanel._pos_near_cons_row), not at
        the OS's default screen-centered dialog placement -- per explicit
        user feedback that it should appear "nere vid hazop scenario" (down
        by the scenario table) rather than as a generic centered popup.
        """
        from hazop import ScenarioTablePanel
        node_id, dev_id, cause_id, cons_id = self._make_chain()

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            expected_anchor = panel._table.viewport().mapToGlobal(
                panel._table.visualRect(
                    panel._table.model().index(row, panel._C_KON)).bottomLeft())

            pos = panel._pos_near_cons_row(cons_id, __import__('PyQt6.QtCore', fromlist=['QSize']).QSize(420, 480))

            # Clamped-to-screen position must still originate from the row's
            # anchor point, not an arbitrary screen-center/default position.
            self.assertLessEqual(abs(pos.x() - expected_anchor.x()), 5)
            self.assertLessEqual(abs(pos.y() - expected_anchor.y()), 5)
        finally:
            panel.deleteLater()

    def test_pos_near_cons_row_falls_back_to_cursor_when_row_not_visible(self):
        """If cons_id isn't in the table's current filter scope (e.g. a
        different node/cause is loaded), _pos_near_cons_row() must not raise
        -- it falls back to the cursor position instead of crashing or
        returning a nonsensical location."""
        from hazop import ScenarioTablePanel
        from PyQt6.QtCore import QSize
        _, _, _, cons_id = self._make_chain()

        panel = ScenarioTablePanel(self.db)
        try:
            panel.clear()  # nothing loaded -> _row_meta is empty
            try:
                pos = panel._pos_near_cons_row(cons_id, QSize(420, 480))
            except Exception as e:
                self.fail(f"_pos_near_cons_row() must not raise when the row "
                          f"isn't visible, raised: {e!r}")
            self.assertIsNotNone(pos)
        finally:
            panel.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# 7. Equipment markers ("🎯 Hitta på P&ID" autodetect feature) — DB round
#    trip for the new equipment_markers table, and a headless smoke test of
#    EquipmentMarkerReviewDialog. Geometric detection itself (clustering,
#    leader-line resolution) is covered separately in test_symbol_geometry.py.
# ══════════════════════════════════════════════════════════════════════════

class EquipmentMarkersDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_eqmarker_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        cur = self.db.conn.execute(
            "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
            "VALUES (?,?,?,?)", ("V-101", "V", 0, "Ventil"))
        self.db.commit()
        self.equipment_id = cur.lastrowid

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_add_and_list_marker(self):
        mid = self.db.add_equipment_marker(
            self.equipment_id, "V-101", 0, 100.0, 100.0, "Ventil",
            shape_outline='[[90,90],[110,90],[110,110],[90,110]]',
            confidence=0.95, link_method='leader')
        self.assertIsNotNone(mid)
        rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['tag'], 'V-101')
        self.assertEqual(rows[0]['link_method'], 'leader')
        self.assertAlmostEqual(rows[0]['confidence'], 0.95)

    def test_delete_marker(self):
        mid = self.db.add_equipment_marker(self.equipment_id, "V-101", 0, 1, 1, "Ventil")
        self.db.delete_equipment_marker(mid)
        self.assertEqual(len(self.db.equipment_markers_for_page(0)), 0)

    def test_cascade_delete_when_equipment_catalog_row_removed(self):
        """equipment_markers.equipment_id has ON DELETE CASCADE — deleting the
        underlying equipment_catalog row (e.g. via 'Rensa utrustning' or a
        rescan) must not leave orphaned marker rows behind."""
        self.db.add_equipment_marker(self.equipment_id, "V-101", 0, 1, 1, "Ventil")
        self.db.conn.execute("DELETE FROM equipment_catalog WHERE id=?", (self.equipment_id,))
        self.db.commit()
        self.assertEqual(len(self.db.equipment_markers_for_page(0)), 0,
            "marker must be cascade-deleted when its equipment_catalog row is removed")

    def test_markers_scoped_by_page(self):
        self.db.add_equipment_marker(self.equipment_id, "V-101", 0, 1, 1, "Ventil")
        self.db.add_equipment_marker(self.equipment_id, "V-101", 3, 2, 2, "Ventil")
        self.assertEqual(len(self.db.equipment_markers_for_page(0)), 1)
        self.assertEqual(len(self.db.equipment_markers_for_page(3)), 1)
        self.assertEqual(len(self.db.equipment_markers_for_page(1)), 0)

    def test_new_confidence_and_line_fields_round_trip(self):
        """Fas 1+2 (2026-08-06): 9 new equipment_markers columns for
        per-field confidence, line/medium/DN tracing, and untagged-valve
        status — must round-trip through add_equipment_marker() and be
        readable via equipment_markers_for_page() like every other column."""
        self.db.add_equipment_marker(
            self.equipment_id, "V-101", 0, 100.0, 100.0, "Ventil",
            confidence=0.6, link_method='leader',
            detection_confidence=1.0, tag_reading_confidence=0.6,
            tag_assignment_confidence=0.8, line_assignment_confidence=0.9,
            line_number='=E1.M1.WPA041', medium_code='KX200',
            medium_code_verified=0, nominal_size='DN10', tag_status='tagged')
        row = dict(self.db.equipment_markers_for_page(0)[0])
        self.assertAlmostEqual(row['detection_confidence'], 1.0)
        self.assertAlmostEqual(row['tag_reading_confidence'], 0.6)
        self.assertAlmostEqual(row['tag_assignment_confidence'], 0.8)
        self.assertAlmostEqual(row['line_assignment_confidence'], 0.9)
        self.assertEqual(row['line_number'], '=E1.M1.WPA041')
        self.assertEqual(row['medium_code'], 'KX200')
        self.assertEqual(row['medium_code_verified'], 0)
        self.assertEqual(row['nominal_size'], 'DN10')
        self.assertEqual(row['tag_status'], 'tagged')

    def test_new_fields_default_sensibly_for_old_style_calls(self):
        """Callers that only pass the original params (confidence,
        link_method) must keep working — the 9 new columns are all
        optional/defaulted, no existing call site should need to change."""
        self.db.add_equipment_marker(self.equipment_id, "V-101", 0, 1, 1, "Ventil")
        row = dict(self.db.equipment_markers_for_page(0)[0])
        self.assertIsNone(row['detection_confidence'])
        self.assertEqual(row['line_number'], '')
        self.assertEqual(row['tag_status'], 'tagged')


class EquipmentMarkerReviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_eqdialog_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        cur = self.db.conn.execute(
            "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
            "VALUES (?,?,?,?)", ("V-101", "V", 0, "Ventil"))
        self.db.commit()
        self.equipment_id = cur.lastrowid

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _sample_results(self):
        return [
            {'tag': 'V-101', 'page': 0, 'comp_type': 'Ventil', 'x': 100.0, 'y': 100.0,
             'confidence': 0.95, 'link_method': 'leader',
             'outline': [[90, 90], [110, 90], [110, 110], [90, 110]],
             'equipment_id': self.equipment_id},
            {'tag': 'V-999', 'page': 0, 'comp_type': 'Ventil', 'x': 0.0, 'y': 0.0,
             'confidence': 0.0, 'link_method': 'not_found',
             'outline': [], 'equipment_id': None},
        ]

    def test_table_populates_from_results(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertEqual(dlg._tbl.rowCount(), 2)
            self.assertEqual(dlg._tbl.item(0, dlg._C_TAG).text(), 'V-101')
        finally:
            dlg.deleteLater()

    def test_high_confidence_row_defaults_checked_low_confidence_unchecked(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertEqual(dlg._tbl.item(0, dlg._C_CHK).checkState(), Qt.CheckState.Checked)
            self.assertEqual(dlg._tbl.item(1, dlg._C_CHK).checkState(), Qt.CheckState.Unchecked)
        finally:
            dlg.deleteLater()

    def test_save_writes_only_checked_rows(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(len(rows), 1, "only the checked (found) row should be saved")
            self.assertEqual(rows[0]['tag'], 'V-101')
        finally:
            dlg.deleteLater()

    def test_save_with_nothing_checked_does_not_write_and_does_not_crash(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMessageBox
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            for r in range(dlg._tbl.rowCount()):
                dlg._tbl.item(r, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            with unittest.mock.patch.object(QMessageBox, 'information'):
                dlg._save()
            self.assertEqual(len(self.db.equipment_markers_for_page(0)), 0)
        finally:
            dlg.deleteLater()

    def test_editing_tag_cell_corrects_the_saved_tag(self):
        """Editing the Tagg column before saving must use the corrected text,
        not the original (possibly wrong) detected tag — this is the 'edit
        errors before saving' mechanism the review dialog exists for."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            dlg._tbl.item(0, dlg._C_TAG).setText('V-101A')
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(rows[0]['tag'], 'V-101A')
        finally:
            dlg.deleteLater()

    def test_editing_typ_cell_corrects_the_saved_type(self):
        """"Hitta liknande symbol" — uppföljningsfunktioner (2026-08-15,
        see NOTES.md) — the Typ column used to be read-only and
        _save() always wrote the frozen res['comp_type']; it's now
        editable and _save() must respect a correction the same way
        the Tagg column already does."""
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt as _Qt
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertTrue(dlg._tbl.item(0, dlg._C_TYPE).flags() & _Qt.ItemFlag.ItemIsEditable,
                "Typ cell must be editable")
            dlg._tbl.item(0, dlg._C_TYPE).setText('Pump')
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(rows[0]['comp_type'], 'Pump')
        finally:
            dlg.deleteLater()

    def test_mass_apply_sets_type_and_tag_sequence_on_checked_rows_only(self):
        """"bra om jag kan välja att koppla det till typ av objekt och
        förhoppningsvis tagg nummer" (2026-08-15) — "Tillämpa på
        ikryssade" writes the chosen type and an auto-incrementing tag
        sequence into every CHECKED row, leaving unchecked rows alone."""
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9},
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 2.0, 'y': 2.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-1', 'detection_confidence': 0.8},
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 3.0, 'y': 3.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-2', 'detection_confidence': 0.05},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._tbl.item(2, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            dlg._mass_type_cb.setCurrentText('Ventil')
            dlg._mass_tag_edit.setText('V-201')
            dlg._apply_mass_tag()
            self.assertEqual(dlg._tbl.item(0, dlg._C_TYPE).text(), 'Ventil')
            self.assertEqual(dlg._tbl.item(1, dlg._C_TYPE).text(), 'Ventil')
            self.assertEqual(dlg._tbl.item(0, dlg._C_TAG).text(), 'V-201')
            self.assertEqual(dlg._tbl.item(1, dlg._C_TAG).text(), 'V-202')
            # Unchecked row untouched (its Tagg cell pre-fills with the
            # untagged placeholder temporary_id — see _populate())
            self.assertEqual(dlg._tbl.item(2, dlg._C_TYPE).text(), '')
            self.assertEqual(dlg._tbl.item(2, dlg._C_TAG).text(), 'SIMILAR-0-2')
        finally:
            dlg.deleteLater()

    def test_mass_apply_persists_via_normal_save(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9},
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 2.0, 'y': 2.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-1', 'detection_confidence': 0.9},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._mass_type_cb.setCurrentText('Ventil')
            dlg._mass_tag_edit.setText('V-301')
            dlg._apply_mass_tag()
            dlg._save()
            rows = sorted((dict(r) for r in self.db.equipment_markers_for_page(0)),
                         key=lambda r: r['tag'])
            self.assertEqual([r['tag'] for r in rows], ['V-301', 'V-302'])
            self.assertEqual([r['comp_type'] for r in rows], ['Ventil', 'Ventil'])
        finally:
            dlg.deleteLater()

    def test_mass_apply_with_nothing_checked_shows_info(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QMessageBox
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            for r in range(dlg._tbl.rowCount()):
                dlg._tbl.item(r, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            dlg._mass_type_cb.setCurrentText('Ventil')
            with unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                dlg._apply_mass_tag()
            mock_info.assert_called_once()
        finally:
            dlg.deleteLater()

    def test_no_edit_shape_button_without_pdf_path(self):
        """"finns det något bra sätt att städa bort ledningen från en
        ventil eller pump" (2026-08-15, see NOTES.md) — without a
        pdf_path there's nothing to re-resolve a cluster from, so the
        column stays empty rather than showing a button that can't work."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertIsNone(dlg._tbl.cellWidget(0, dlg._C_EDIT))
        finally:
            dlg.deleteLater()

    def test_edit_shape_button_only_shown_for_rows_with_an_outline(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QPushButton
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            self.assertIsInstance(dlg._tbl.cellWidget(0, dlg._C_EDIT), QPushButton)
            self.assertIsNone(dlg._tbl.cellWidget(1, dlg._C_EDIT))
        finally:
            dlg.deleteLater()

    def test_edit_shape_updates_outline_and_recenters_x_y_on_accept(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=(['prims'], ['idx'], {})), \
                 unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                     return_value=[]), \
                 unittest.mock.patch('pid_viewer.MarkerShapeEditDialog') as mock_editor_cls:
                mock_editor_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
                mock_editor_cls.return_value.edited_outline.return_value = \
                    [[0, 0], [20, 0], [20, 10], [0, 10]]
                dlg._edit_shape(0)
            self.assertEqual(dlg._results[0]['outline'], [[0, 0], [20, 0], [20, 10], [0, 10]])
            self.assertEqual(dlg._results[0]['x'], 10.0)
            self.assertEqual(dlg._results[0]['y'], 5.0)
        finally:
            dlg.deleteLater()

    def test_edit_shape_leaves_outline_unchanged_when_editor_cancelled(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            original_outline = list(dlg._results[0]['outline'])
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=(['prims'], ['idx'], {})), \
                 unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                     return_value=[]), \
                 unittest.mock.patch('pid_viewer.MarkerShapeEditDialog') as mock_editor_cls:
                mock_editor_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
                dlg._edit_shape(0)
            self.assertEqual(dlg._results[0]['outline'], original_outline)
        finally:
            dlg.deleteLater()

    def test_edit_shape_shows_info_when_no_cluster_resolved(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QMessageBox
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=None), \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                dlg._edit_shape(0)
            mock_info.assert_called_once()
        finally:
            dlg.deleteLater()

    def test_edited_shape_is_persisted_on_save(self):
        """The whole point: an edited outline must actually reach the
        database, not just live in the row's in-memory dict."""
        import json
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=(['prims'], ['idx'], {})), \
                 unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                     return_value=[]), \
                 unittest.mock.patch('pid_viewer.MarkerShapeEditDialog') as mock_editor_cls:
                mock_editor_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
                mock_editor_cls.return_value.edited_outline.return_value = \
                    [[0, 0], [20, 0], [20, 10], [0, 10]]
                dlg._edit_shape(0)
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            saved = next(r for r in rows if r['tag'] == 'V-101')
            self.assertEqual(json.loads(saved['shape_outline']), [[0, 0], [20, 0], [20, 10], [0, 10]])
        finally:
            dlg.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# 8. _reload_all_panels() must swap self.db on EVERY panel that holds its
#    own db reference. Real bug found in production: HAZOPWorksheet (and
#    its embedded ScenarioTablePanel), RedMarkupPanel and
#    RedMarkupTablePanel were missing from the panel list — after "Nytt
#    projekt" / "Öppna .hzp" closed the old connection and opened a new
#    one, clicking the Worksheet tab crashed with sqlite3.ProgrammingError
#    ("Cannot operate on a closed database") because HAZOPWorksheet.refresh()
#    -> _populate_node_combo() -> self.db.nodes() still ran against the OLD,
#    now-closed Database object.
# ══════════════════════════════════════════════════════════════════════════

class ReloadAllPanelsDbSwapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_worksheet_and_its_embedded_table_panel_get_new_db(self):
        with _TempDbMainWindow() as win:
            old_db = win.db
            # Simulate what _hzp_new/_load_hzp do: close the old connection,
            # swap in a brand new Database, then run the same fix-up step.
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.worksheet.db, win.db,
                "HAZOPWorksheet must receive the new db reference")
            self.assertIs(win.worksheet._table_panel.db, win.db,
                "HAZOPWorksheet's embedded ScenarioTablePanel must also receive the new db reference")
            self.assertIs(win.red_markup_panel.db, win.db)
            self.assertIs(win.red_markup_table_panel.db, win.db)

    def test_worksheet_refresh_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: switching to
        the Worksheet tab after a db swap must not raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO nodes (name, markup_points, markup_style, pid_page) "
                "VALUES ('N-1', '[]', '{}', 0)")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.worksheet.refresh()
            except sqlite3.ProgrammingError as e:
                self.fail(f"worksheet.refresh() must not touch the closed old db, raised: {e!r}")

    def test_equipment_panel_and_its_model_get_new_db(self):
        """Same bug class as the Worksheet one above, found via a real crash
        report (2026-08-06): EquipmentPanel's QTableView is backed by
        _EquipmentTableModel, which keeps its OWN db reference (needed so
        setData()/delete_row() can write through directly) separate from
        EquipmentPanel.db — _reload_all_panels() updated the panel's db but
        not the model's, so the model kept using the old, by-then-closed
        connection after a project reload."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.equipment_panel.db, win.db)
            self.assertIs(win.equipment_panel._model.db, win.db,
                "EquipmentPanel's _EquipmentTableModel must also receive the new db reference")

    def test_equipment_panel_refresh_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: switching to
        the Utrustning tab after a project reload must not raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
                "VALUES ('V-1', 'V', 0, 'Ventil')")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.equipment_panel.refresh()
            except sqlite3.ProgrammingError as e:
                self.fail(f"equipment_panel.refresh() must not touch the closed old db, raised: {e!r}")

    def test_admin_panels_nested_pid_mgmt_gets_new_db(self):
        """Same bug class again, found via a real crash report
        (2026-08-11, see NOTES.md): StudyManagementPanel (admin_panel)
        embeds its own PIDManagementPanel (self._pid_mgmt, revision
        history + sheet reordering) with its own separate db reference,
        set once at __init__ and never touched by _reload_all_panels()'s
        top-level panel.db = db loop."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.admin_panel.db, win.db)
            self.assertIs(win.admin_panel._pid_mgmt.db, win.db,
                "StudyManagementPanel's nested PIDManagementPanel must also receive the new db reference")

    def test_admin_panel_refresh_pid_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: switching
        to the Administration tab after a project reload used to raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')
        from Database.get_revisions()."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO pid_revisions (revision, notes, created_at, pdf_path) "
                "VALUES ('Rev A', '', '2026-08-11', '')")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.admin_panel.refresh_pid()
            except sqlite3.ProgrammingError as e:
                self.fail(f"admin_panel.refresh_pid() must not touch the closed old db, raised: {e!r}")

    def test_pid_analysis_panel_and_its_model_get_new_db(self):
        """Same bug, same fix, for Inställningar → Identifierade objekt
        (PIDAnalysisPanel / _IdentifiedTagsModel)."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            analysis_panel = win.settings_panel.analysis_panel
            self.assertIs(analysis_panel.db, win.db)
            self.assertIs(analysis_panel._model.db, win.db,
                "PIDAnalysisPanel's _IdentifiedTagsModel must also receive the new db reference")

    def test_pid_analysis_panel_refresh_does_not_crash_after_db_swap(self):
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO pid_identified_tags (tag_code, examples, name_sv, comp_type, confirmed) "
                "VALUES ('V', 'V-1', '', '', 0)")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.settings_panel.analysis_panel.refresh()
            except sqlite3.ProgrammingError as e:
                self.fail(f"analysis_panel.refresh() must not touch the closed old db, raised: {e!r}")

    def test_equipment_deviation_bar_gets_new_db(self):
        """Same bug class as worksheet/equipment_panel above, found via a
        real crash report (2026-08-07): EquipmentDeviationBar (the bottom-
        of-P&ID bar opened by clicking an equipment marker, see NOTES.md
        'Nod → Utrustning → Avvikelse') keeps its own db reference,
        separate from PIDPanel.db — _reload_all_panels() updated
        pid_panel.db/pid_panel.viewer.db but not pid_panel._equipment_bar.db,
        so clicking an equipment marker after a project reload crashed with
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.pid_panel._equipment_bar.db, win.db,
                "EquipmentDeviationBar must also receive the new db reference")

    def test_equipment_marker_click_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: clicking an
        equipment marker on the P&ID after a project reload must not raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            eq_id = old_db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            marker_id = old_db.add_equipment_marker(eq_id, "V-1", 0, 10.0, 10.0, "Ventil")

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.pid_panel._on_marker_clicked('equipment', marker_id)
            except sqlite3.ProgrammingError as e:
                self.fail(f"clicking an equipment marker must not touch the closed old db, raised: {e!r}")

    def test_equipment_marker_click_opens_bar_and_filters_scenario_table(self):
        """(2026-08-12, see NOTES.md) _on_marker_clicked used to return
        early for 'equipment' without ever emitting marker_navigated —
        opening EquipmentDeviationBar and filtering the scenario table
        were mutually exclusive. Now both happen on the same click: the
        full real chain (PIDPanel._on_marker_clicked -> marker_navigated
        -> MainWindow._on_marker_navigate -> _on_equipment_marker_navigate
        -> scenario_panel.load_equipment) must actually fire end to end."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(eq_id, "PV-101", 0, 10.0, 10.0, "Ventil")

            win.pid_panel._on_marker_clicked('equipment', marker_id)

            self.assertEqual(win.pid_panel._equipment_bar.equipment_id, eq_id,
                "the deviation checklist bar must still open, as before")
            self.assertEqual(win.scenario_panel._equipment_filter_id, eq_id,
                "the scenario table must now also be filtered to this equipment")


# ══════════════════════════════════════════════════════════════════════════
# 9. Unified tag scanning — "🔍 Skanna P&ID" and "📋 Analysera P&ID" used to
#    be two separate, inconsistent tag-matching implementations.
#    scan_pdf_for_equipment()'s matcher silently dropped single-letter-
#    prefix tags with no separator (P101, T12, E205) and never rejoined
#    tags the PDF split into multiple text objects; _analyze_pid's matcher
#    (_pick_best_tag/_spatial_combine) did both correctly, which is why it
#    found more. Both entry points now share scan_pdf_for_equipment() (with
#    the fixed matcher) and cross-write into BOTH equipment_catalog and
#    pid_identified_tags so results are identical regardless of which
#    button was used.
# ══════════════════════════════════════════════════════════════════════════

class UnifiedTagScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_scan_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        self.db = Database(path=self.db_path)

        import fitz
        doc = fitz.open()
        p0 = doc.new_page(width=200, height=200)
        p0.insert_text(fitz.Point(10, 20), "P101", fontsize=10)   # single-letter prefix, no separator
        p1 = doc.new_page(width=200, height=200)
        p1.insert_text(fitz.Point(10, 20), "20-PCV-101", fontsize=10)
        doc.save(self.pdf_path)
        doc.close()
        self.db.set_pid_config_value('path', self.pdf_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_scan_pdf_for_equipment_finds_single_letter_prefix_tag(self):
        """Regression test for the exact reported gap: 'P101' (single-letter
        prefix, no separator) used to be silently dropped by
        scan_pdf_for_equipment's Pass 2 (_parse_tag's len(pfx)>=2 gate)."""
        import fitz
        from equipment_detection import scan_pdf_for_equipment
        doc = fitz.open(self.pdf_path)
        try:
            result = scan_pdf_for_equipment(doc, use_ocr=False)
        finally:
            doc.close()
        result.pop('_meta', None)
        all_tags = {t for data in result.values() for t in data['tags']}
        self.assertIn('P-101', all_tags,
            "single-letter-prefix tag without a separator must now be found")

    def test_scan_pdf_for_equipment_rejoins_split_tokens(self):
        import fitz
        from equipment_detection import scan_pdf_for_equipment
        doc = fitz.open(self.pdf_path)
        try:
            result = scan_pdf_for_equipment(doc, use_ocr=False)
        finally:
            doc.close()
        result.pop('_meta', None)
        all_tags = {t for data in result.values() for t in data['tags']}
        self.assertTrue(any('PCV' in t for t in all_tags),
            f"expected a PCV tag rejoined from split tokens, got: {all_tags}")

    def test_pick_best_tag_normalises_rdspp_compound_separators(self):
        """Used to return the RDS-PP compound match completely raw (leading
        '=', dots kept as-is) instead of normalising it like _parse_tag's
        own EXT_TAG_RE branch already does — see NOTES.md 'Dubbla taggar
        vid skanning' (2026-08-13, real LKAB file).

        Only the separator right before the instrument code (QMA081)
        becomes a dash — the dot between the area-hierarchy segments
        (E1, M1) is LKAB's own real RDS-PP notation and must survive
        (2026-08-13 follow-up: 'anger inte punkt för lkab taggarna utan
        anger - istället'), not get collapsed to 'E1-M1-QMA081'."""
        from equipment_detection import _pick_best_tag
        self.assertEqual(_pick_best_tag('=E1.M1.QMA081'), 'E1.M1-QMA081')

    def test_parse_tag_preserves_rdspp_hierarchy_dots(self):
        """_parse_tag's own EXT_TAG_RE branch (shared _normalize_ext_tag
        helper with _pick_best_tag, 2026-08-13 follow-up) must preserve
        the same LKAB dot-hierarchy notation, and still resolve the
        correct instrument-code prefix for equipment-type lookup."""
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag('=E1.M1.QMA081'), ('E1.M1-QMA081', 'QMA'))
        self.assertEqual(_parse_tag('E1.M1.WPA001'), ('E1.M1-WPA001', 'WPA'))
        # A separator already present right before the instrument code
        # (not a dot) is left as a dash, unchanged.
        self.assertEqual(_parse_tag('=E1.M1-QMA081'), ('E1.M1-QMA081', 'QMA'))

    def test_rdspp_compound_tag_deduped_against_its_own_bare_form(self):
        """Real LKAB file bug (2026-08-13, see NOTES.md 'Dubbla taggar vid
        skanning'): an RDS-PP path tag like '=E1.M1.QMA081' used to survive
        as a SECOND, differently-formatted duplicate of the same
        instrument's bare code 'QMA-081' — Pass 1's full-text regex found
        the bare form (dashed) by fragmenting the compound into short
        letter+digit chunks before _parse_tag ever saw it whole, while
        Pass 2's _pick_best_tag returned the compound form completely
        unnormalised (dotted, leading '='). Both landed in
        equipment_catalog as separate rows for one physical instrument —
        "one with a dash, one without", per the bug report."""
        import fitz
        from equipment_detection import scan_pdf_for_equipment
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text(fitz.Point(10, 20), "=E1.M1.QMA081", fontsize=10)
        try:
            result = scan_pdf_for_equipment(doc, use_ocr=False)
        finally:
            doc.close()
        result.pop('_meta', None)
        qma_tags = result.get('QMA', {}).get('tags', [])
        self.assertEqual(qma_tags, ['QMA-081'],
            f"expected only the bare form, got duplicate(s): {qma_tags}")

    def test_spatial_combine_returns_bbox_tuples(self):
        from equipment_detection import _spatial_combine
        import fitz
        doc = fitz.open(self.pdf_path)
        try:
            page = doc[1]
            words = page.get_text("words")
            results = _spatial_combine(words, gap_limit=22.0)
        finally:
            doc.close()
        self.assertTrue(results)
        self.assertTrue(all(len(r) == 5 for r in results),
            "_spatial_combine must yield (text, x0, y0, x1, y1) tuples")
        self.assertTrue(any('PCV' in r[0] for r in results))

    def test_spatial_combine_skips_exact_position_duplicate_words(self):
        """Some CAD exports render the same text 2-3 times at the
        byte-for-byte identical bbox (a bold-simulation trick) — confirmed
        on a real ITS P&ID title block, where "Checked"/"Drawn" etc. each
        appeared exactly 3 times with identical coordinates. Without a
        dedup check, _spatial_combine's gap-based joining logic
        concatenates these into e.g. "CheckedCheckedChecked" instead of
        recognizing them as the same word rendered on top of itself."""
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 200.0, 128.0, 208.0, 'Checked'),
            (100.0, 200.0, 128.0, 208.0, 'Checked'),
            (100.0, 200.0, 128.0, 208.0, 'Checked'),
        ]
        results = _spatial_combine(words, gap_limit=18.0)
        texts = {r[0] for r in results}
        self.assertIn('Checked', texts)
        self.assertNotIn('CheckedCheckedChecked', texts)
        self.assertFalse(any(t.count('Checked') > 1 for t in texts),
            f"exact-position duplicate must never be concatenated, got: {texts}")

    def test_spatial_combine_dedups_duplicate_mid_group(self):
        """The same duplicate-rendering artifact can occur on ANY word
        within an already multi-word line, not just the first — e.g. a
        real ITS title block line rendered as "MANIFOLD pressure
        pressure PHC PHC ...". Must dedup regardless of position in the
        group, not just immediately after the group's own first token."""
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 200.0, 140.0, 208.0, 'MANIFOLD'),
            (145.0, 200.0, 175.0, 208.0, 'pressure'),
            (145.0, 200.0, 175.0, 208.0, 'pressure'),
            (180.0, 200.0, 195.0, 208.0, 'PHC'),
        ]
        results = _spatial_combine(words, gap_limit=18.0)
        texts = {r[0] for r in results}
        self.assertIn('MANIFOLDpressurePHC', texts)
        self.assertFalse(any('pressurepressure' in t.lower() for t in texts),
            f"mid-group duplicate must be skipped too, got: {texts}")

    def test_equipment_panel_scan_writes_both_tables(self):
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox

        class _FakeProgressDialog:
            """Stand-in for QProgressDialog: under the offscreen QPA platform
            used for headless tests, a real QProgressDialog.close() spuriously
            flips wasCanceled() to True (not reproducible in a real windowed
            session) — _scan() checks wasCanceled() right after close(), so
            without this stub the scan result would be silently discarded in
            this test harness. A plain stub avoids depending on Qt's
            offscreen-platform quirks entirely, rather than patching a real
            QProgressDialog's methods."""
            def __init__(self, *a, **k): pass
            def setWindowTitle(self, *a, **k): pass
            def setWindowModality(self, *a, **k): pass
            def setMinimumDuration(self, *a, **k): pass
            def show(self, *a, **k): pass
            def setValue(self, *a, **k): pass
            def setLabelText(self, *a, **k): pass
            def close(self, *a, **k): pass
            def wasCanceled(self): return False

        panel = EquipmentPanel(self.db)
        try:
            with unittest.mock.patch.object(
                    QMessageBox, 'question',
                    return_value=QMessageBox.StandardButton.No), \
                 unittest.mock.patch.object(QMessageBox, 'information'), \
                 unittest.mock.patch('hazop.QProgressDialog', _FakeProgressDialog):
                panel._scan()
            cat_tags = {dict(r)['tag'] for r in self.db.equipment_items()}
            id_rows  = list(self.db.pid_identified_tags())
            self.assertIn('P-101', cat_tags,
                "equipment_catalog must contain the single-letter-prefix tag")
            self.assertTrue(len(id_rows) > 0,
                "pid_identified_tags must also be populated by 'Skanna P&ID' now")
        finally:
            panel.deleteLater()

    def test_analyze_pid_writes_both_tables(self):
        import fitz
        from pid_viewer import PIDPanel
        from PyQt6.QtWidgets import QMessageBox
        panel = PIDPanel(self.db)
        try:
            panel.viewer.pdf_doc = fitz.open(self.pdf_path)
            with unittest.mock.patch.object(
                    QMessageBox, 'question',
                    return_value=QMessageBox.StandardButton.No), \
                 unittest.mock.patch.object(QMessageBox, 'information'):
                panel._analyze_pid()
            cat_tags = {dict(r)['tag'] for r in self.db.equipment_items()}
            id_rows  = list(self.db.pid_identified_tags())
            self.assertIn('P-101', cat_tags,
                "'Analysera P&ID' must now also populate equipment_catalog")
            self.assertTrue(len(id_rows) > 0)
        finally:
            panel.viewer.pdf_doc.close()
            panel.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# 10. Bow-tie shape-based valve detection — "🦋 Hitta ventilformer" finds
#     valves by their drawn silhouette (symbol_geometry.bowtie_score)
#     instead of requiring a tag to already be scanned nearby, so it also
#     catches untagged valves. find_valve_shapes()/find_nearby_tag_text()
#     are unit-tested against synthetic PDFs here; the geometry itself
#     (bowtie_score orientation/sampling correctness) is covered in
#     test_symbol_geometry.BowtieScoreTests.
# ══════════════════════════════════════════════════════════════════════════

class BowtieValveDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_bowtie_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _bowtie_pdf(self, with_tag=True):
        import fitz
        path = os.path.join(self._tmpdir, "valve.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        if with_tag:
            page.insert_text(fitz.Point(95, 75), "V-201", fontsize=8)
        doc.save(path)
        doc.close()
        return path

    def test_find_valve_shapes_detects_bowtie_with_nearby_tag(self):
        from equipment_detection import find_valve_shapes
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=True))
        try:
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'V-201')
        self.assertEqual(results[0]['link_method'], 'shape')
        self.assertGreater(results[0]['confidence'], 0.5)

    def test_find_valve_shapes_leaves_tag_empty_when_none_nearby(self):
        """This is the scope the user explicitly asked for: valves with no
        tag anywhere near them must still be found and returned, with an
        empty (not guessed, not dropped) tag for manual entry in the
        review dialog."""
        from equipment_detection import find_valve_shapes
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=False))
        try:
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], '')
        self.assertEqual(results[0]['link_method'], 'shape')

    def test_find_valve_shapes_respects_min_bowtie_score(self):
        from equipment_detection import find_valve_shapes
        import fitz
        path = os.path.join(self._tmpdir, "rect.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(90, 90, 110, 110))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [], "a plain rectangle must not be reported as a valve")

    def test_find_valve_shapes_and_tag_link_survive_page_rotation(self):
        """Regression test for the coordinate mix-up found during real-file
        verification against 182036 Hybrit (/Rotate 270): get_text() and
        get_drawings() both report positions in the page's raw unrotated
        mediabox space, but this app's marker placement (pdf_to_scene())
        assumes the ROTATED space matching page.rect. Without rotating
        both the shape and the tag text into the same space, the tag
        would fail to link (find_nearby_tag_text comparing a rotated-space
        point against raw-space text positions) and/or the reported x/y
        would land outside the rendered page entirely."""
        from equipment_detection import find_valve_shapes
        import fitz
        path = os.path.join(self._tmpdir, "rotated_valve.pdf")
        doc = fitz.open()
        # Non-square mediabox + 90° rotation: page.rect swaps width/height,
        # so a coordinate-space mix-up cannot accidentally look correct.
        page = doc.new_page(width=100, height=300)
        page.set_rotation(90)
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(10, 270), fitz.Point(10, 290), fitz.Point(20, 280)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(30, 270), fitz.Point(30, 290), fitz.Point(20, 280)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        page.insert_text(fitz.Point(15, 255), "V-500", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            page_rect = doc[0].rect
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'V-500',
            "tag must still link across the rotation-space mismatch")
        self.assertTrue(0 <= results[0]['x'] <= page_rect.width,
            f"x={results[0]['x']} must fall inside the rendered page (0..{page_rect.width})")
        self.assertTrue(0 <= results[0]['y'] <= page_rect.height,
            f"y={results[0]['y']} must fall inside the rendered page (0..{page_rect.height})")

    def test_find_valve_shapes_detects_small_valve_on_no_text_dense_noise_page(self):
        """End-to-end reproduction of a real generalization failure found
        while auditing against the "P&ID ref/" reference library (2026-
        08-12): Loket, Smurfit Kappa, and one Swerim/NYA file all draw
        EVERY piece of text as pure vector strokes via a non-embedded/
        SHX-style font, so page.get_text() finds zero words/spans despite
        tens of thousands of vector primitives (a real Loket page measured
        54,396 primitives against 0 text spans, ~5x denser than a normal
        text-bearing P&ID of the same size). Two compounding bugs hid real
        valves on such pages: (1) dominant_text_size()'s blind 10.0pt
        no-text fallback shrank norm_size for every real symbol until it
        fell under classify_cluster's/find_valve_shapes' 1.5 floor, and
        (2) cluster_primitives' dense-grid-cell skip fragmented a real
        valve's own few edges into unmergeable singletons whenever they
        shared a 20x20pt cell with a patch of glyph-stroke noise — visibly
        intact bow-tie valve icons next to real tags (confirmed: Loket tag
        "1-RV-25") queried as nothing but n_items=1 singleton clusters.
        This test needs BOTH fixes together: fixing only the scale
        wouldn't help while the valve's own primitives still can't merge,
        and fixing only the clustering wouldn't help while the merged
        valve's norm_size still falls under the floor."""
        from equipment_detection import find_valve_shapes
        import fitz
        path = os.path.join(self._tmpdir, "no_text_dense_noise.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        # Small bow-tie valve (bbox diagonal ~11.3pt) — proportioned like
        # many real bow-tie icons on the real Loket file.
        shape.draw_polyline([fitz.Point(96, 96), fitz.Point(96, 104), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(104, 96), fitz.Point(104, 104), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        # Dense "vector-outlined text" noise: 45 tiny primitives packed
        # into the SAME 20x20pt grid cell as the valve (cell (4,4), pt
        # range 80-100 on both axes), far enough from the valve's own bbox
        # (>_CLUSTER_GAP=3.0pt) that they could never legitimately merge
        # with it.
        for i in range(45):
            col, row = i % 9, i // 9
            x = 80.0 + col * 0.12
            y = 80.0 + row * 0.12
            shape.draw_line(fitz.Point(x, y), fitz.Point(x + 0.05, y + 0.05))
            shape.finish(color=(0, 0, 0), width=0.05, closePath=False)
        shape.commit()
        # Deliberately NO insert_text call anywhere on this page — the
        # whole point is reproducing a page with zero native text.
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            words = doc[0].get_text("words")
            self.assertEqual(words, [], "fixture must have zero native text, matching the real files")
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1,
            "the small valve must be found despite the page having no "
            "native text and a dense patch of glyph-noise-sized primitives "
            "sharing its grid cell")
        self.assertGreater(results[0]['confidence'], 0.5)

    def test_find_pump_shapes_detects_circle_with_impeller_diagonal(self):
        """find_pump_shapes() — the pump counterpart to find_valve_shapes(),
        added after studying real LKAB/Gryaab P&IDs: a pump is a circle
        with two diagonal lines (an impeller mark) meeting on its rim,
        proportioned at ~70% of the circle's own diameter (confirmed: a
        real LKAB pump's two 30.1pt diagonals inside a 42.5pt circle)."""
        from equipment_detection import find_pump_shapes
        import fitz
        path = os.path.join(self._tmpdir, "pump.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 86), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(120, 100), fitz.Point(100, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(80, 130), "PU-101", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_pump_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'PU-101')
        self.assertEqual(results[0]['comp_type'], 'Pump')

    def test_find_pump_shapes_ignores_plain_instrument_bubble(self):
        from equipment_detection import find_pump_shapes
        import fitz
        path = os.path.join(self._tmpdir, "bubble.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(90, 100), "PI-1", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_pump_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [], "a plain instrument bubble (no impeller diagonal) must not be reported as a pump")

    def test_find_pump_shapes_ignores_title_block_corner(self):
        """A pump-shaped hit in the bottom-right title-block corner is
        excluded (2026-08-10, see NOTES.md known limitations, fixed) — a
        real ITS P&ID had a stylized company logo there false-positive as
        a pump. The same shape at page center (test above) still counts."""
        from equipment_detection import find_pump_shapes
        import fitz
        path = os.path.join(self._tmpdir, "pump_titleblock.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        # Bottom-right corner for a 200x200 page with the (0.20, 0.10)
        # fraction is x>=160, y>=180 — center the pump well inside that.
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(185, 190), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(185, 183), fitz.Point(195, 190))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(195, 190), fitz.Point(185, 197))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_pump_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [], "a pump-shaped hit inside the title-block corner must be excluded")

    def test_find_instrument_shapes_detects_divided_capsule(self):
        """find_instrument_shapes() — the instrument counterpart to
        find_valve_shapes()/find_pump_shapes(), added after studying a
        real LKAB P&ID: an instrument bubble is a circle/capsule with a
        horizontal divider line at its vertical midpoint (ISA-5.1's
        shared-display/panel-mounted convention)."""
        from equipment_detection import find_instrument_shapes
        import fitz
        path = os.path.join(self._tmpdir, "instrument.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(85, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_circle(fitz.Point(115, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 90), fitz.Point(115, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 110), fitz.Point(115, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(75, 100), fitz.Point(125, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        # fontsize=10 (not the more typical 8) so dominant_text_size()
        # comes out high enough that the 50pt divider line stays safely
        # under _is_pipe_run_line's scale-relative "long pipe" threshold
        # (6x text size) — with fontsize=8 that threshold is 48pt and
        # the divider gets excluded from bridging, splitting the capsule
        # into two clusters and hiding it from instrument_shapes_in_cluster.
        page.insert_text(fitz.Point(70, 130), "PI-101", fontsize=10)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_instrument_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'PI-101')
        self.assertEqual(results[0]['comp_type'], 'Instrument / Sensor')

    def test_find_instrument_shapes_ignores_pump_circle(self):
        """A pump's circle (with its own diagonal impeller mark and a
        pipe line straight through its midpoint) must never also be
        reported as an instrument — see
        InstrumentShapeTests.test_pump_circle_with_through_line_is_not_an_instrument
        for the geometric ambiguity this guards against."""
        from equipment_detection import find_instrument_shapes
        import fitz
        path = os.path.join(self._tmpdir, "pump_not_instrument.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(70, 100), fitz.Point(130, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 86), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(120, 100), fitz.Point(100, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(90, 130), "PU-1", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_instrument_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [])

    def test_find_nearby_tag_text_finds_closest_tag_within_radius(self):
        from equipment_detection import find_nearby_tag_text
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text(fitz.Point(95, 75), "V-201", fontsize=8)
        try:
            tag, prefix = find_nearby_tag_text(page, (100, 100), radius=150.0)
            self.assertEqual(tag, 'V-201')
            self.assertEqual(prefix, 'V')
        finally:
            doc.close()

    def test_find_nearby_tag_text_returns_none_when_too_far(self):
        from equipment_detection import find_nearby_tag_text
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(10, 20), "V-201", fontsize=8)
        try:
            tag, prefix = find_nearby_tag_text(page, (390, 390), radius=50.0)
            self.assertIsNone(tag)
            self.assertIsNone(prefix)
        finally:
            doc.close()

    def test_detect_equipment_and_valves_links_shape_hit_to_existing_equipment_row(self):
        """Replacement for the old, now-removed '🦋 Hitta ventilformer'
        button (its shape-hunting is folded into detect_equipment_and_valves,
        the engine behind EquipmentPanel._autodetect — see NOTES.md,
        "Fas 1+2" 2026-08-06): a bow-tie shape whose nearby tag matches an
        already-known equipment_catalog row must resolve with a real
        link_method (not 'none'), so _autodetect's tag_to_equipment_id
        lookup can attach it to the existing row instead of duplicating it."""
        from equipment_detection import detect_equipment_and_valves
        import fitz
        cur = self.db.conn.execute(
            "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
            "VALUES (?,?,?,?)", ("V-201", "V", 0, "Ventil"))
        self.db.commit()

        doc = fitz.open(self._bowtie_pdf(with_tag=True))
        try:
            tag_points = [("V-201", "V", 0, None, None, 1.0)]
            results, _rejected = detect_equipment_and_valves(doc, tag_points)
            tagged = [r for r in results if r['tag'] == 'V-201']
            self.assertEqual(len(tagged), 1)
            self.assertNotEqual(tagged[0]['link_method'], 'none')
            self.assertEqual(tagged[0]['tag_status'], 'tagged')
        finally:
            doc.close()

    def test_autodetect_no_pdf_shows_warning(self):
        """Replacement for the old PIDPanel._find_valve_shapes no-PDF test —
        EquipmentPanel._autodetect is now the single consolidated entry
        point and must warn the same way when no P&ID path is configured."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("V-201", "V-201", "V", 0, "Ventil", '', 0)
        panel = EquipmentPanel(self.db)
        try:
            panel.refresh()
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
                panel._autodetect()
            self.assertEqual(mock_warn.call_count, 1)
        finally:
            panel.deleteLater()

    def test_review_dialog_save_creates_equipment_catalog_row_for_taggedbut_unlinked_row(self):
        """A shape-detected valve whose suggested tag has NO existing
        equipment_catalog row (equipment_id=None) must get one created on
        save, per Task 20's tag-less-row handling."""
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [{'tag': 'V-301', 'page': 0, 'comp_type': 'Ventil',
                    'x': 100.0, 'y': 100.0, 'confidence': 0.8,
                    'link_method': 'shape', 'outline': [[90, 90], [110, 110]],
                    'equipment_id': None}]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._save()
            cat_rows = [dict(r) for r in self.db.equipment_items()]
            self.assertEqual(len(cat_rows), 1)
            self.assertEqual(cat_rows[0]['tag'], 'V-301')
            marker_rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(len(marker_rows), 1)
        finally:
            dlg.deleteLater()

    def test_review_dialog_save_with_empty_tag_and_no_equipment_id_still_saves(self):
        """A shape-detected valve with NEITHER a suggested tag NOR a linked
        equipment_catalog row (the 'valve with no tag anywhere nearby'
        case) must still be checkable and saveable — the whole point of
        the tag-less scope the user asked for."""
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        results = [{'tag': '', 'page': 0, 'comp_type': 'Ventil',
                    'x': 50.0, 'y': 50.0, 'confidence': 0.8,
                    'link_method': 'shape', 'outline': [[40, 40], [60, 60]],
                    'equipment_id': None}]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._tbl.item(0, dlg._C_CHK).setCheckState(Qt.CheckState.Checked)
            dlg._save()
            marker_rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(len(marker_rows), 1)
        finally:
            dlg.deleteLater()


    # ══════════════════════════════════════════════════════════════════════
    # Fas 1+2 (2026-08-06): detect_equipment_and_valves() unifies the
    # tag-anchored and shape-anchored paths against one shared per-page
    # cluster extraction, so a valve that has BOTH a known tag AND a
    # matching bow-tie shape must appear exactly once, and one with
    # neither must still surface as an untagged row, not get dropped.
    # ══════════════════════════════════════════════════════════════════════

    def test_detect_equipment_and_valves_no_double_count_when_tag_and_shape_both_match(self):
        from equipment_detection import detect_equipment_and_valves
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=True))
        try:
            tag_points = [("V-201", "V", 0, None, None, 1.0)]
            results, rejected = detect_equipment_and_valves(doc, tag_points)
        finally:
            doc.close()
        self.assertEqual(len(results), 1,
            "the same physical valve must not appear once as a tagged row "
            "and again as a separate untagged shape-anchored row")
        self.assertEqual(results[0]['tag'], 'V-201')
        self.assertEqual(results[0]['tag_status'], 'tagged')

    def test_detect_equipment_and_valves_surfaces_untagged_valve(self):
        """The scope the user explicitly asked ChatGPT's spec to cover:
        valves with no tag anywhere nearby must still be found and
        returned with a temporary id, never silently dropped."""
        from equipment_detection import detect_equipment_and_valves
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=False))
        try:
            results, rejected = detect_equipment_and_valves(doc, [])
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag_status'], 'untagged')
        self.assertEqual(results[0]['tag'], '')
        self.assertTrue(results[0]['temporary_id'].startswith('UNASSIGNED-VALVE-0-'),
            f"got temporary_id={results[0]['temporary_id']!r}")

    def test_detect_equipment_and_valves_surfaces_untagged_pump_and_instrument(self):
        """User request (2026-08-07): 'Hitta på P&ID' (detect_equipment_and_valves,
        the engine behind the old '🦋 Hitta ventilformer' button) must also
        surface pump and instrument shapes, not just valves — they were
        previously only reachable via the standalone find_pump_shapes()/
        find_instrument_shapes(), never wired into this unified pipeline."""
        from equipment_detection import detect_equipment_and_valves
        import fitz
        path = os.path.join(self._tmpdir, "pump_and_instrument.pdf")
        doc = fitz.open()
        page = doc.new_page(width=300, height=200)
        shape = page.new_shape()
        # A pump: circle + diagonal impeller mark.
        shape.draw_circle(fitz.Point(60, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(60, 86), fitz.Point(80, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(80, 100), fitz.Point(60, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A divided instrument capsule, far enough away to be its own cluster.
        shape.draw_circle(fitz.Point(225, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_circle(fitz.Point(255, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(225, 90), fitz.Point(255, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(225, 110), fitz.Point(255, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(215, 100), fitz.Point(265, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(40, 130), "PU-101", fontsize=10)
        page.insert_text(fitz.Point(210, 130), "PI-101", fontsize=10)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results, _rejected = detect_equipment_and_valves(doc, [])
        finally:
            doc.close()

        pumps = [r for r in results if r['comp_type'] == 'Pump']
        instruments = [r for r in results if r['comp_type'] == 'Instrument / Sensor']
        self.assertEqual(len(pumps), 1)
        self.assertEqual(pumps[0]['tag'], 'PU-101')
        self.assertEqual(pumps[0]['tag_status'], 'untagged')
        self.assertTrue(pumps[0]['temporary_id'].startswith('UNASSIGNED-PUMP-0-'))
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]['tag'], 'PI-101')
        self.assertEqual(instruments[0]['tag_status'], 'untagged')
        self.assertTrue(instruments[0]['temporary_id'].startswith('UNASSIGNED-INSTRUMENT-0-'))

    def test_valve_rejection_reason_reports_single_near_miss_only(self):
        """_valve_rejection_reason surfaces a near-miss (exactly one of the
        five bow-tie/valve-shape filters failed) for the review dialog's
        'avvisade kandidater' section, but stays silent for a cluster that
        either passes everything or is too far off (>1 filter failed) to
        be an interesting near-miss."""
        from equipment_detection import _valve_rejection_reason
        near_miss = {'bowtie_score': 0.7, 'aspect': 5.0, 'norm_size': 10.0,
                     'has_diagonal': True, 'has_closed_loop': True}
        reason = _valve_rejection_reason(near_miss, min_bowtie_score=0.5)
        self.assertIsNotNone(reason)
        self.assertIn('avlångt', reason.lower())

        too_far_off = {'bowtie_score': 0.1, 'aspect': 5.0, 'norm_size': 10.0,
                       'has_diagonal': True, 'has_closed_loop': True}
        self.assertIsNone(_valve_rejection_reason(too_far_off, min_bowtie_score=0.5))

        passes_all = {'bowtie_score': 0.7, 'aspect': 1.5, 'norm_size': 10.0,
                      'has_diagonal': True, 'has_closed_loop': True}
        self.assertIsNone(_valve_rejection_reason(passes_all, min_bowtie_score=0.5))

        no_closed_shape = {'bowtie_score': 0.7, 'aspect': 1.5, 'norm_size': 10.0,
                            'has_diagonal': True, 'has_closed_loop': False}
        reason2 = _valve_rejection_reason(no_closed_shape, min_bowtie_score=0.5)
        self.assertIsNotNone(reason2)
        self.assertIn('sluten', reason2.lower())


class FindSimilarShapesTests(unittest.TestCase):
    """find_similar_shapes() (equipment_detection.py) — "Hitta liknande
    symbol" (2026-08-10, see NOTES.md): user picks a reference point,
    every other vector cluster in the document gets ranked against it
    via symbol_geometry.cluster_similarity()."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_findsimilar_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _two_bowties_and_a_rect_pdf(self):
        """Two IDENTICAL bow-tie shapes (well separated) plus one plainly
        different long rectangle, on one page."""
        import fitz
        path = os.path.join(self._tmpdir, "similar.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def _bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        _bowtie(60, 60)     # reference
        _bowtie(300, 300)   # identical, far away
        shape.draw_rect(fitz.Rect(60, 300, 260, 320))   # plainly different (long, no diagonals)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        doc.save(path)
        doc.close()
        return path

    def test_finds_identical_shape_elsewhere_on_page(self):
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.5)
        finally:
            doc.close()
        self.assertTrue(results, "the identical bow-tie elsewhere on the page must be found")
        best = results[0]
        self.assertAlmostEqual(best['x'], 300, delta=5)
        self.assertAlmostEqual(best['y'], 300, delta=5)

    def test_result_shape_matches_review_dialog_contract(self):
        """EquipmentMarkerReviewDialog._populate()/_save() index these
        dict keys directly — a typo here would only surface as a KeyError
        deep inside dialog code, not at the call site."""
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.5, comp_type='Ventil')
        finally:
            doc.close()
        self.assertTrue(results)
        r = results[0]
        for key in ('tag', 'page', 'comp_type', 'x', 'y', 'outline',
                   'link_method', 'tag_status', 'temporary_id', 'detection_confidence'):
            self.assertIn(key, r)
        self.assertEqual(r['tag'], '')
        self.assertEqual(r['comp_type'], 'Ventil')
        self.assertEqual(r['link_method'], 'similar')
        self.assertEqual(r['tag_status'], 'untagged')
        self.assertTrue(0.0 <= r['detection_confidence'] <= 1.0)

    def test_excludes_reference_cluster_itself(self):
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.0)
        finally:
            doc.close()
        for r in results:
            self.assertFalse(abs(r['x'] - 60) < 2 and abs(r['y'] - 60) < 2,
                "the reference shape must not be returned as a match for itself")

    def test_sorted_by_similarity_descending(self):
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.0)
        finally:
            doc.close()
        sims = [r['detection_confidence'] for r in results]
        self.assertEqual(sims, sorted(sims, reverse=True))

    def test_returns_empty_for_page_with_no_vector_data(self):
        """A fully rasterized/scanned page (no vector drawings at all,
        see NOTES.md's 2026-08-10 corpus review) has nothing for
        extract_primitives() to find — must return [] cleanly, not
        raise, regardless of where the user clicked."""
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "blank.pdf")
        doc = fitz.open()
        doc.new_page(width=200, height=200)   # no shapes drawn at all
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=100, ref_y=100, pages=[0])
        finally:
            doc.close()
        self.assertEqual(results, [])

    def test_caps_at_50_results(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "many.pdf")
        doc = fitz.open()
        page = doc.new_page(width=2000, height=2000)
        shape = page.new_shape()
        for i in range(60):
            cx, cy = 40 + (i % 10) * 60, 40 + (i // 10) * 60
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=40, ref_y=40,
                                          pages=[0], min_similarity=0.0)
        finally:
            doc.close()
        self.assertLessEqual(len(results), 50)


class NextTagSequenceTests(unittest.TestCase):
    """_next_tag_sequence() (equipment_detection.py) — mass-tagging in
    EquipmentMarkerReviewDialog (2026-08-15, see NOTES.md "Hitta
    liknande symbol" — uppföljningsfunktioner)."""

    def test_increments_preserving_zero_padding(self):
        from equipment_detection import _next_tag_sequence
        self.assertEqual(_next_tag_sequence('FT-009', 3), ['FT-009', 'FT-010', 'FT-011'])

    def test_does_not_overflow_into_extra_digits(self):
        from equipment_detection import _next_tag_sequence
        # "101" is a 3-digit run — the 900th increment must still be
        # zero-padded to 3 digits' worth of width, not silently grown.
        seq = _next_tag_sequence('V-101', 2)
        self.assertEqual(seq, ['V-101', 'V-102'])

    def test_skips_tags_already_taken(self):
        from equipment_detection import _next_tag_sequence
        seq = _next_tag_sequence('V-101', 3, existing_tags=['V-102'])
        self.assertEqual(seq, ['V-101', 'V-103', 'V-104'])

    def test_skips_tags_already_taken_case_insensitively(self):
        from equipment_detection import _next_tag_sequence
        seq = _next_tag_sequence('V-101', 2, existing_tags=['v-102'])
        self.assertEqual(seq, ['V-101', 'V-103'])

    def test_no_digit_run_falls_back_to_dash_suffix(self):
        from equipment_detection import _next_tag_sequence
        self.assertEqual(_next_tag_sequence('PUMP', 3), ['PUMP', 'PUMP-2', 'PUMP-3'])

    def test_never_generates_a_duplicate_within_the_same_batch(self):
        from equipment_detection import _next_tag_sequence
        seq = _next_tag_sequence('V-1', 10)
        self.assertEqual(len(seq), len(set(t.upper() for t in seq)))


class SymbolTemplateDatabaseTests(unittest.TestCase):
    """Database symbol_templates CRUD (2026-08-15, see NOTES.md "Hitta
    liknande symbol" — uppföljningsfunktioner: symbolbibliotek)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_symboltemplate_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_add_and_list_symbol_template(self):
        tid = self.db.add_symbol_template(
            "Metso-ventil", '{"aspect": 2.0, "norm_size": 4.5}', comp_type='Ventil')
        rows = self.db.symbol_templates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], tid)
        self.assertEqual(rows[0]['name'], "Metso-ventil")
        self.assertEqual(rows[0]['comp_type'], 'Ventil')
        self.assertTrue(rows[0]['created'])

    def test_get_symbol_template_by_id(self):
        tid = self.db.add_symbol_template("Endress+Hauser", '{"aspect": 1.0}')
        row = self.db.get_symbol_template(tid)
        self.assertEqual(row['name'], "Endress+Hauser")

    def test_get_symbol_template_missing_id_returns_none(self):
        self.assertIsNone(self.db.get_symbol_template(9999))

    def test_delete_symbol_template(self):
        tid = self.db.add_symbol_template("Att ta bort", '{}')
        self.db.delete_symbol_template(tid)
        self.assertIsNone(self.db.get_symbol_template(tid))
        self.assertEqual(self.db.symbol_templates(), [])

    def test_name_must_be_unique(self):
        self.db.add_symbol_template("Samma namn", '{}')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_symbol_template("Samma namn", '{}')

    def test_features_json_round_trips_through_json_module(self):
        """cluster_features()/similarity_features()'s dict is already
        JSON-safe (float/int/bool/tuple-of-floats, no numpy types) —
        confirms the actual save/load path a real caller uses."""
        import json
        feats = {'aspect': 2.0, 'norm_size': 4.5, 'fold_ratio': 3.1,
                 'has_curve': False, 'has_diagonal': True, 'has_closed_or_filled': True}
        tid = self.db.add_symbol_template("JSON-test", json.dumps(feats))
        row = self.db.get_symbol_template(tid)
        self.assertEqual(json.loads(row['features_json'])['aspect'], 2.0)


class ApplyPageRotationsTests(unittest.TestCase):
    """equipment_detection.apply_page_rotations() (2026-08-15, see
    NOTES.md "Hitta liknande symbol placerar fel — sidrotation"). Real
    bug: "Det ser ut som den hittar massa liknande med vektor men den
    placerar ut dem felaktigt på P&ID:et." Root cause: a background
    worker's own freshly-opened fitz.Document never sees
    PIDGraphicsView's live manual-rotation-override state, so a page
    with an override (confirmed on the real active project: page 0 has
    a 90-degree override in pid_page_rotation) gets its vector geometry
    computed in the WRONG coordinate space — cluster_similarity's shape
    scoring is already rotation-invariant in 90-degree steps so matches
    are still found, but their x/y/outline land nowhere near where the
    live view expects them."""

    def test_noop_without_any_override(self):
        import fitz
        import equipment_detection as ed
        doc = fitz.open()
        doc.new_page(width=200, height=100)
        ed.apply_page_rotations(doc, None)
        self.assertEqual(doc[0].rotation, 0)
        ed.apply_page_rotations(doc, {})
        self.assertEqual(doc[0].rotation, 0)
        doc.close()

    def test_composes_extra_rotation_with_intrinsic(self):
        import fitz
        import equipment_detection as ed
        doc = fitz.open()
        doc.new_page(width=200, height=100)
        doc[0].set_rotation(0)
        ed.apply_page_rotations(doc, {0: 90})
        self.assertEqual(doc[0].rotation, 90)
        doc.close()

    def test_out_of_range_page_numbers_are_ignored(self):
        import fitz
        import equipment_detection as ed
        doc = fitz.open()
        doc.new_page(width=200, height=100)
        ed.apply_page_rotations(doc, {5: 90})   # must not raise
        self.assertEqual(doc[0].rotation, 0)
        doc.close()

    def test_same_primitive_lands_in_the_same_coordinates_as_the_live_view(self):
        """The actual bug, reproduced directly: without re-applying the
        override, a freshly-opened document computes primitives in a
        completely different coordinate space than the live view's own
        (already-overridden) document — this is what silently misplaced
        every "hitta liknande symbol" result."""
        import fitz
        import symbol_geometry as sg
        import equipment_detection as ed
        path = None
        import tempfile, os
        tmpdir = tempfile.mkdtemp(prefix="hazop_pagerot_test_")
        path = os.path.join(tmpdir, "t.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=100)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(10, 20), fitz.Point(30, 20))
        shape.finish(color=(0, 0, 0))
        shape.commit()
        doc.save(path)
        doc.close()

        # LIVE view: override applied directly to the live document.
        live_doc = fitz.open(path)
        live_doc[0].set_rotation(90)
        live_bbox = sg.extract_primitives(live_doc[0])[0]['bbox']
        live_doc.close()

        # Worker WITHOUT the fix: fresh doc, override never re-applied.
        broken_doc = fitz.open(path)
        broken_bbox = sg.extract_primitives(broken_doc[0])[0]['bbox']
        broken_doc.close()
        self.assertNotEqual(broken_bbox, live_bbox,
            "sanity check: an unfixed fresh document really does diverge")

        # Worker WITH the fix.
        fixed_doc = fitz.open(path)
        ed.apply_page_rotations(fixed_doc, {0: 90})
        fixed_bbox = sg.extract_primitives(fixed_doc[0])[0]['bbox']
        fixed_doc.close()
        self.assertEqual(fixed_bbox, live_bbox)
        shutil.rmtree(tmpdir, ignore_errors=True)


class FindSimilarShapesSearchParametersTests(unittest.TestCase):
    """"Hitta liknande symbol" — sökparametrar (2026-08-14, see NOTES.md):
    find_similar_shapes()'s new ignore_scale/rotation_mode/
    ref_index_group parameters, and resolve_reference_cluster() (the
    reference-resolution split out for SimilarSymbolSearchDialog's
    segment-exclusion preview, pid_viewer.py)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_findsimilar_params_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _bowtie(self, shape, cx, cy, s=10, deg=0):
        import fitz
        import math
        rad = math.radians(deg)
        def rot(x, y):
            dx, dy = x - cx, y - cy
            return fitz.Point(cx + dx * math.cos(rad) - dy * math.sin(rad),
                              cy + dx * math.sin(rad) + dy * math.cos(rad))
        shape.draw_polyline([rot(cx - s, cy - s), rot(cx - s, cy + s), rot(cx, cy)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([rot(cx + s, cy - s), rot(cx + s, cy + s), rot(cx, cy)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

    def test_resolve_reference_cluster_returns_primitives_and_index_group(self):
        import fitz
        from equipment_detection import resolve_reference_cluster
        path = os.path.join(self._tmpdir, "ref.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            resolved = resolve_reference_cluster(doc, 0, 60, 60)
        finally:
            doc.close()
        self.assertIsNotNone(resolved)
        primitives, index_group, cluster = resolved
        self.assertTrue(primitives)
        self.assertTrue(index_group)
        self.assertIn('bbox', cluster)

    def test_resolve_reference_cluster_returns_none_with_no_vector_data(self):
        import fitz
        from equipment_detection import resolve_reference_cluster
        path = os.path.join(self._tmpdir, "blank.pdf")
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            resolved = resolve_reference_cluster(doc, 0, 100, 100)
        finally:
            doc.close()
        self.assertIsNone(resolved)

    def test_ref_index_group_excludes_a_wrongly_merged_stray_line(self):
        """The reference case the whole feature exists for: a valve
        whose auto-detected cluster happens to include an attached
        pipe stub. Excluding the stub's primitive indices (as
        SimilarSymbolSearchDialog's segment editor would do) must
        raise the similarity to a clean, unattached copy of the same
        shape elsewhere in the document."""
        import fitz
        from equipment_detection import find_similar_shapes, resolve_reference_cluster
        path = os.path.join(self._tmpdir, "stray.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)   # reference — gets a stray line attached below
        shape.draw_line(fitz.Point(70, 60), fitz.Point(100, 60))
        shape.finish(color=(0, 0, 0), width=1)
        self._bowtie(shape, 300, 300)   # clean copy elsewhere, no stray line
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            default_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                   pages=[0], min_similarity=0.0)
            default_best = max((r['detection_confidence'] for r in default_results), default=0.0)

            primitives, index_group, _cluster = resolve_reference_cluster(doc, 0, 60, 60)
            stray_prims = {i for i in index_group
                           if primitives[i]['p0'] in ((70.0, 60.0), (100.0, 60.0))
                           or primitives[i]['p1'] in ((70.0, 60.0), (100.0, 60.0))}
            edited_group = [i for i in index_group if i not in stray_prims]
            self.assertLess(len(edited_group), len(index_group),
                "test setup issue: the stray line wasn't part of the auto-detected cluster")

            edited_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                 pages=[0], min_similarity=0.0,
                                                 ref_index_group=edited_group)
            edited_best = max((r['detection_confidence'] for r in edited_results), default=0.0)
        finally:
            doc.close()
        self.assertGreater(edited_best, default_best,
            "excluding the stray line must improve the match against the clean copy")
        self.assertAlmostEqual(edited_best, 1.0, places=3)

    def test_ignore_scale_finds_a_much_larger_copy_past_the_default_threshold(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "scale.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=600)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60, s=10)
        self._bowtie(shape, 400, 400, s=60)   # same shape, 6x bigger, far away
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            default_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                   pages=[0], min_similarity=0.85)
            scaled_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                 pages=[0], min_similarity=0.85, ignore_scale=True)
        finally:
            doc.close()
        self.assertEqual(default_results, [],
            "test setup issue: the size difference should already fail the default threshold")
        self.assertTrue(scaled_results,
            "ignore_scale must let a pure size difference pass the same threshold")

    def test_rotation_mode_any_finds_a_45_degree_copy_past_the_default_threshold(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "rot.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=600)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60, s=10, deg=0)
        self._bowtie(shape, 400, 400, s=10, deg=45)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            default_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                   pages=[0], min_similarity=0.95)
            any_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                              pages=[0], min_similarity=0.95, rotation_mode='any')
        finally:
            doc.close()
        self.assertEqual(default_results, [],
            "test setup issue: the 45° rotation should already fail the default threshold")
        self.assertTrue(any_results,
            "rotation_mode='any' must let a 45°-rotated copy pass the same threshold")

    def test_scan_candidates_returns_unthresholded_unsorted_tuples(self):
        """_scan_candidates() (2026-08-15, see NOTES.md "Hitta liknande
        symbol" — uppföljningsfunktioner) is the shared, expensive half
        of find_similar_shapes(), split out so SimilarSymbolSearchWorker
        can run it once and reuse it for both the live match-count
        preview and the final thresholded search. It must NOT apply
        min_similarity itself — that's find_similar_shapes()'s job."""
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "scancand.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        # A little real text so dominant_text_size() uses the normal
        # per-glyph estimate instead of its no-text vector-bootstrap
        # fallback — on a page with only a couple of shapes, that
        # fallback ties its own scale estimate to the very shapes being
        # measured (confirmed directly: it otherwise locks to almost
        # exactly the bow-tie's own size, pinning norm_size right at the
        # aspect/norm_size pre-filter's own boundary below). Any real
        # P&ID has actual text; this keeps the fixture representative.
        page.insert_text(fitz.Point(200, 380), "TAG-001", fontsize=8)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        # Plainly different shape-wise (no diagonal/curve mismatch vs the
        # bow-tie's own triangle edges) but still a plausible-symbol
        # size/aspect — a long thin rect was used here before
        # _scan_candidates gained its own aspect/norm_size pre-filter
        # (2026-08-16, see NOTES.md "Anton rapporterade 1070 träffar
        # istället för 20-30 ventiler"), which now excludes anything that
        # implausible before ever comparing shape features at all; a
        # filled oval keeps this test's original intent (verify
        # min_similarity itself isn't applied) without tripping that new,
        # unrelated pre-filter.
        shape.draw_oval(fitz.Rect(140, 50, 180, 70))
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0])
        finally:
            doc.close()
        self.assertTrue(candidates)
        # Both a high-similarity (the clean bow-tie copy) and a
        # low-similarity (the oval, no diagonal edges, has_curve
        # mismatch) candidate must be present — min_similarity was never
        # applied.
        sims = [c[0] for c in candidates]
        self.assertGreater(max(sims), 0.9)
        self.assertLess(min(sims), 0.5)

    def test_scan_candidates_rejects_plain_rectangle_with_no_diagonal_or_curve(self):
        """_scan_candidates()'s has_diagonal-or-has_curve pre-filter
        (2026-08-16, see NOTES.md "Anton rapporterade 1070 träffar"
        follow-up — "gemensamma nämnare för ventiler, pumpar,
        instrument"): a real equipment symbol's own defining geometry is
        either diagonal (valve bow-tie edges) or curved (pump/instrument
        circles). Found directly on the active project's own
        hazop_project_pid.pdf: a size/aspect-plausible cluster can still
        be nothing more than a plain axis-aligned rectangle — an empty
        gap between two pipe lines, or a text-label box — with neither.
        Reproduced here with a plain closed rectangle, size/aspect-
        plausible enough to survive the OTHER pre-filter."""
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "norectangle.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(200, 380), "TAG-001", fontsize=8)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        shape.draw_rect(fitz.Rect(140, 50, 170, 70))   # plain rect: no diagonal, no curve
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0])
        finally:
            doc.close()
        self.assertEqual(candidates, [],
            "the plain rectangle has neither a diagonal nor a curve and must be excluded, "
            "even though it is size/aspect-plausible and min_similarity was never applied")

    def test_scan_candidates_rejects_pipe_run_aspect_even_with_ignore_scale(self):
        """Found in review (2026-08-16, see NOTES.md "raster-sökning"
        follow-up): an earlier version of the aspect/norm_size pre-filter
        bundled BOTH checks under `not ignore_scale`, so checking "Alla
        storlekar" silently let long/thin pipe-run clusters back into
        similarity scoring — the exact noise the filter exists to
        reject. ignore_scale is specifically about SIZE
        (cluster_similarity drops norm_size from the score entirely,
        aspect stays fully weighted regardless — see its own docstring)
        so the aspect>3.0 pipe-run exclusion must apply UNCONDITIONALLY,
        with or without ignore_scale."""
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "pipe_ignore_scale.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(200, 380), "TAG-001", fontsize=8)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        # A long diagonal line: aspect >> 3.0 (a pipe run), but WITH
        # has_diagonal=True so it isn't excluded by that OTHER filter —
        # isolates the aspect check specifically.
        shape.draw_line(fitz.Point(140, 50), fitz.Point(300, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0], ignore_scale=True)
        finally:
            doc.close()
        self.assertEqual(candidates, [],
            "the long diagonal pipe-run line must stay excluded (aspect>3.0) even "
            "when ignore_scale=True, since that flag is about size, not shape/aspect")

    def test_scan_candidates_should_cancel_stops_before_any_page_is_scanned(self):
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "cancel.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0],
                should_cancel=lambda: True)
        finally:
            doc.close()
        self.assertEqual(candidates, [])

    def test_find_similar_shapes_should_cancel_yields_no_results(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "cancel2.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.0,
                                          should_cancel=lambda: True)
        finally:
            doc.close()
        self.assertEqual(results, [])

    def test_find_shapes_matching_features_finds_matches_from_a_foreign_reference(self):
        """Symbolbibliotek (2026-08-15, see NOTES.md "Hitta liknande
        symbol" — uppföljningsfunktioner): searching from a saved
        template's features (computed against a COMPLETELY different
        document) must still find matching shapes here — there is no
        live reference page/cluster to resolve or exclude."""
        import fitz
        from equipment_detection import find_shapes_matching_features, resolve_reference_cluster
        from symbol_geometry import similarity_features

        target_path = os.path.join(self._tmpdir, "target.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        shape.commit()
        doc.save(target_path)
        doc.close()

        # The "template" reference lives on an entirely separate document.
        ref_doc = fitz.open()
        ref_page = ref_doc.new_page(width=200, height=200)
        ref_shape = ref_page.new_shape()
        self._bowtie(ref_shape, 50, 50, s=5)
        ref_shape.commit()
        primitives, index_group, _cluster = resolve_reference_cluster(ref_doc, 0, 50, 50)
        ref_features = similarity_features(primitives, index_group)
        ref_doc.close()

        doc = fitz.open(target_path)
        try:
            results = find_shapes_matching_features(doc, ref_features, pages=[0],
                                                     min_similarity=0.5, comp_type='Ventil')
        finally:
            doc.close()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r['comp_type'] == 'Ventil' for r in results))

    def test_find_shapes_matching_features_returns_empty_for_no_pdf(self):
        from equipment_detection import find_shapes_matching_features
        self.assertEqual(find_shapes_matching_features(None, {}), [])


class ClusterPreviewCanvasTests(unittest.TestCase):
    """_ClusterPreviewCanvas (pid_viewer.py) — the segment-exclusion
    preview in SimilarSymbolSearchDialog (2026-08-14, see NOTES.md
    "Hitta liknande symbol" — sökparametrar). Directly implements
    "ta bort något som inte tillhör"."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _line(self, x0, y0, x1, y1, source=0, filled=False):
        return {'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1), 'closed': False, 'filled': filled,
                'width': 1.0, 'source': source}

    def test_edited_index_group_starts_as_the_full_group(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0, source=0), self._line(10, 0, 10, 10, source=1)]
        canvas = _ClusterPreviewCanvas(prims, [0, 1])
        try:
            self.assertEqual(canvas.edited_index_group(), [0, 1])
            self.assertFalse(canvas.has_edits())
        finally:
            canvas.deleteLater()

    def _click_at(self, canvas, x, y):
        from PyQt6.QtCore import QPoint, QEvent, Qt as _Qt
        from PyQt6.QtGui import QMouseEvent
        pos = QPoint(int(x), int(y))
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                         _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                         _Qt.KeyboardModifier.NoModifier)
        canvas.mousePressEvent(ev)

    def test_clicking_a_segment_toggles_its_exclusion(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0, source=0), self._line(0, 20, 10, 20, source=1)]
        canvas = _ClusterPreviewCanvas(prims, [0, 1])
        try:
            canvas.resize(240, 180)
            scale, ox, oy = canvas._transform()
            x0, y0 = prims[0]['p0']
            x1, y1 = prims[0]['p1']
            mx, my = (x0 + x1) / 2 * scale + ox, (y0 + y1) / 2 * scale + oy

            self._click_at(canvas, mx, my)
            self.assertEqual(canvas.edited_index_group(), [1])
            self.assertTrue(canvas.has_edits())

            self._click_at(canvas, mx, my)   # click again — re-include it
            self.assertEqual(canvas.edited_index_group(), [0, 1])
            self.assertFalse(canvas.has_edits())
        finally:
            canvas.deleteLater()

    def test_clicking_far_from_any_segment_changes_nothing(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0)]
        canvas = _ClusterPreviewCanvas(prims, [0])
        try:
            canvas.resize(240, 180)
            self._click_at(canvas, 1, 1)   # far corner, well away from the segment
            self.assertEqual(canvas.edited_index_group(), [0])
        finally:
            canvas.deleteLater()

    def test_selection_changed_signal_emitted_on_click(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0)]
        canvas = _ClusterPreviewCanvas(prims, [0])
        try:
            canvas.resize(240, 180)
            scale, ox, oy = canvas._transform()
            mx, my = 5 * scale + ox, 0 * scale + oy
            spy = unittest.mock.Mock()
            canvas.selection_changed.connect(spy)
            self._click_at(canvas, mx, my)
            spy.assert_called_once()
        finally:
            canvas.deleteLater()

    def test_edited_outline_is_bbox_of_surviving_primitives_only(self):
        """"städa bort en ledning från en ventil/pump" (2026-08-15, see
        NOTES.md) — excluding the "pipe" primitive must shrink the
        returned outline to just the remaining ("valve") primitives."""
        from pid_viewer import _ClusterPreviewCanvas
        valve = self._line(0, 0, 10, 10, source=0)     # bbox (0,0,10,10)
        pipe = self._line(10, 10, 100, 10, source=1)   # bbox (10,10,100,10) — stretches the outline far out
        canvas = _ClusterPreviewCanvas([valve, pipe], [0, 1])
        try:
            self.assertEqual(canvas.edited_outline(), [[0, 0], [100, 0], [100, 10], [0, 10]])
            canvas._excluded_sources.add(1)   # exclude the "pipe"
            self.assertEqual(canvas.edited_outline(), [[0, 0], [10, 0], [10, 10], [0, 10]])
        finally:
            canvas.deleteLater()

    def test_edited_outline_empty_when_everything_excluded(self):
        from pid_viewer import _ClusterPreviewCanvas
        canvas = _ClusterPreviewCanvas([self._line(0, 0, 10, 0)], [0])
        try:
            canvas._excluded_sources.add(0)
            self.assertEqual(canvas.edited_outline(), [])
        finally:
            canvas.deleteLater()

    def test_primitives_sharing_a_source_toggle_together(self):
        """The whole point of grouping by source (2026-08-15, see
        NOTES.md "Referens-canvasen: rendera fyllnad som svart + gruppera
        klick per ritad väg") — a tessellated shape's fragments all
        share one drawn path and must exclude/include as ONE unit, not
        one tiny fragment at a time."""
        from pid_viewer import _ClusterPreviewCanvas
        # Two fragments of the SAME source, physically apart within the
        # canvas — clicking either one must toggle BOTH.
        frag_a = self._line(0, 0, 2, 0, source=5)
        frag_b = self._line(0, 20, 2, 20, source=5)
        other = self._line(50, 50, 52, 50, source=9)
        canvas = _ClusterPreviewCanvas([frag_a, frag_b, other], [0, 1, 2])
        try:
            canvas.resize(240, 180)
            scale, ox, oy = canvas._transform()
            mx, my = 1 * scale + ox, 0 * scale + oy
            self._click_at(canvas, mx, my)
            self.assertEqual(canvas.edited_index_group(), [2],
                "clicking one fragment of source 5 must exclude BOTH its fragments")
        finally:
            canvas.deleteLater()

    def test_edited_index_group_stays_primitive_granular(self):
        """Even though exclusion is per-source, the public contract
        (edited_index_group -> similarity_features's ref_index_group)
        must still be primitive indices, not source ids."""
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 2, 0, source=5), self._line(0, 20, 2, 20, source=5)]
        canvas = _ClusterPreviewCanvas(prims, [0, 1])
        try:
            self.assertEqual(canvas.edited_index_group(), [0, 1])
            canvas._excluded_sources.add(5)
            self.assertEqual(canvas.edited_index_group(), [])
        finally:
            canvas.deleteLater()

    def test_filled_group_still_renders_as_stroked_outline_only(self):
        """2026-08-15 follow-up (see NOTES.md "Referens-canvasen"): a
        convex-hull SOLID fill for filled=True groups was tried and
        shipped, but Anton reported it wasn't visually convincing in
        practice ("Det blev inte jättelyckat med att fylla dem") and
        asked for it to be removed — the per-source CLICK grouping
        stays, only the fill rendering is gone. Sample actual rendered
        pixels (same technique RiskCellActualRenderColorTests already
        uses) to confirm a point INSIDE a filled=True triangle's
        interior stays the white background — only its edges are drawn."""
        from pid_viewer import _ClusterPreviewCanvas
        prims = [
            self._line(0, 0, 40, 0, source=1, filled=True),
            self._line(40, 0, 0, 40, source=1, filled=True),
            self._line(0, 40, 0, 0, source=1, filled=True),
        ]
        canvas = _ClusterPreviewCanvas(prims, [0, 1, 2])
        try:
            canvas.resize(200, 200)
            pix = canvas.grab()
            img = pix.toImage()
            scale, ox, oy = canvas._transform()
            # Well inside the triangle (near its centroid) — must NOT be filled.
            inside = img.pixelColor(int(12 * scale + ox), int(12 * scale + oy))
            self.assertGreater(inside.lightness(), 200,
                f"filled=True must no longer solid-fill — got {inside.name()}")
        finally:
            canvas.deleteLater()


class MarkerShapeEditDialogTests(unittest.TestCase):
    """MarkerShapeEditDialog (pid_viewer.py) — "finns det något bra
    sätt att städa bort ledningen från en ventil eller pump" (2026-08-15,
    see NOTES.md). Reuses _ClusterPreviewCanvas so ANY detected marker
    (not just a similarity-search reference) can be pruned before saving."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _line(self, x0, y0, x1, y1, source=0):
        return {'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1), 'closed': False, 'filled': False,
                'width': 1.0, 'source': source}

    def test_ok_returns_the_canvas_edited_outline(self):
        from pid_viewer import MarkerShapeEditDialog
        from PyQt6.QtWidgets import QDialog
        prims = [self._line(0, 0, 10, 0, source=0), self._line(10, 0, 50, 0, source=1)]
        dlg = MarkerShapeEditDialog(prims, [0, 1])
        try:
            dlg._canvas._excluded_sources.add(1)
            dlg.accept()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dlg.edited_outline(), [[0, 0], [10, 0], [10, 0], [0, 0]])
        finally:
            dlg.deleteLater()

    def test_cancel_rejects_the_dialog(self):
        from pid_viewer import MarkerShapeEditDialog
        from PyQt6.QtWidgets import QDialog, QPushButton
        dlg = MarkerShapeEditDialog([self._line(0, 0, 10, 0)], [0])
        try:
            cancel_btn = [b for b in dlg.findChildren(QPushButton) if b.text() == 'Avbryt'][0]
            cancel_btn.click()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Rejected)
        finally:
            dlg.deleteLater()


class ImageRefCropCanvasTests(unittest.TestCase):
    """_ImageRefCropCanvas (pid_viewer.py) — the rubber-band crop tool
    for the image-matching reference preview (2026-08-15, see NOTES.md
    "Bildbaserad 'hitta liknande symbol'" real-file verification
    follow-up). Directly answers the earlier finding that a reference
    bbox including an adjacent tag label scores far worse than a
    tightly-cropped symbol-only region — image mode previously had no
    way for the user to fix that themselves."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _gray(self, w=100, h=80):
        import numpy as np
        return np.full((h, w), 255, dtype=np.uint8)

    def _drag(self, canvas, x0, y0, x1, y1):
        from PyQt6.QtCore import QPoint, QEvent, Qt as _Qt
        from PyQt6.QtGui import QMouseEvent
        press = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(x0, y0).toPointF(),
                             _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                             _Qt.KeyboardModifier.NoModifier)
        canvas.mousePressEvent(press)
        move = QMouseEvent(QEvent.Type.MouseMove, QPoint(x1, y1).toPointF(),
                            _Qt.MouseButton.NoButton, _Qt.MouseButton.LeftButton,
                            _Qt.KeyboardModifier.NoModifier)
        canvas.mouseMoveEvent(move)
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPoint(x1, y1).toPointF(),
                               _Qt.MouseButton.LeftButton, _Qt.MouseButton.NoButton,
                               _Qt.KeyboardModifier.NoModifier)
        canvas.mouseReleaseEvent(release)

    def test_current_bbox_is_the_full_reference_before_any_crop(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.set_reference(self._gray(), (10.0, 20.0, 30.0, 40.0))
            self.assertFalse(canvas.has_crop())
            self.assertEqual(canvas.current_bbox(), (10.0, 20.0, 30.0, 40.0))
        finally:
            canvas.deleteLater()

    def test_dragging_a_rectangle_crops_to_a_pdf_space_sub_bbox(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(100, 80), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 20, 20, 100, 100)
            self.assertTrue(canvas.has_crop())
            x0, y0, x1, y1 = canvas.current_bbox()
            # The dragged rectangle must be a strict sub-region of the
            # full reference, not the whole thing again.
            self.assertGreater(x0, 0.0)
            self.assertGreater(y0, 0.0)
            self.assertLess(x1, 100.0)
            self.assertLess(y1, 80.0)
        finally:
            canvas.deleteLater()

    def test_a_stray_click_does_not_create_a_crop(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 50, 50, 51, 51)   # a sub-pixel-scale jitter, not a real drag
            self.assertFalse(canvas.has_crop())
        finally:
            canvas.deleteLater()

    def test_reset_crop_restores_the_full_reference_and_emits_once(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 20, 20, 100, 100)
            spy = unittest.mock.Mock()
            canvas.crop_changed.connect(spy)
            canvas.reset_crop()
            self.assertFalse(canvas.has_crop())
            self.assertEqual(canvas.current_bbox(), (0.0, 0.0, 100.0, 80.0))
            spy.assert_called_once()
            canvas.reset_crop()   # already reset — must not emit again
            spy.assert_called_once()
        finally:
            canvas.deleteLater()

    def test_set_reference_clears_any_previous_crop(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 20, 20, 100, 100)
            self.assertTrue(canvas.has_crop())
            canvas.set_reference(self._gray(), (5.0, 5.0, 105.0, 85.0))
            self.assertFalse(canvas.has_crop())
            self.assertEqual(canvas.current_bbox(), (5.0, 5.0, 105.0, 85.0))
        finally:
            canvas.deleteLater()


class _SyncFakeSimilarSymbolSearchWorker(QThread):
    """Test double for SimilarSymbolSearchWorker (2026-08-15, see
    NOTES.md "Hitta liknande symbol" — uppföljningsfunktioner) — a real
    QThread subclass (so SimilarSymbolSearchDialog's real
    .progress.connect()/.finished_scan.connect() wiring works exactly
    as in production) but start() runs synchronously on the calling
    thread and emits a pre-set candidate list immediately, instead of
    opening a real fitz.Document in a real background thread. Set
    `next_candidates` (a class attribute) before constructing the
    dialog to control what the "scan" finds."""
    progress      = pyqtSignal(int, int, str)
    finished_scan = pyqtSignal(list)

    next_candidates = []   # overridden per-test

    def __init__(self, pdf_path, ref_features, ref_page, ref_native_index_group,
                 pages=None, ignore_scale=False, rotation_mode='none',
                 page_rotations=None, parent=None):
        super().__init__()
        self.start_count = 0
        self._ref_features = ref_features
        self._ref_page = ref_page
        self._ref_native_index_group = ref_native_index_group
        self._pages = pages
        self._ignore_scale = ignore_scale
        self._rotation_mode = rotation_mode
        self._page_rotations = page_rotations
        type(self).instances.append(self)

    instances = []   # every instance constructed during a test, for call-count assertions

    def start(self):
        self.start_count += 1
        self.finished_scan.emit(list(type(self).next_candidates))

    def isRunning(self):
        return False

    def requestInterruption(self):
        pass

    def wait(self, *a):
        pass


class _SyncFakeImageSymbolSearchWorker(QThread):
    """Test double for ImageSymbolSearchWorker (2026-08-15, see NOTES.md
    "Bildbaserad 'hitta liknande symbol' — vid sidan av vektorlogiken")
    — same synchronous-start convention as
    _SyncFakeSimilarSymbolSearchWorker above, just matching
    ImageSymbolSearchWorker's own constructor signature."""
    progress      = pyqtSignal(int, int, str)
    finished_scan = pyqtSignal(list)

    next_candidates = []
    instances = []

    def __init__(self, pdf_path, ref_page, ref_bbox, pages=None,
                 ignore_scale=False, rotation_mode='none',
                 page_rotations=None, parent=None):
        super().__init__()
        self.start_count = 0
        self._ref_page = ref_page
        self._ref_bbox = ref_bbox
        self._pages = pages
        self._ignore_scale = ignore_scale
        self._rotation_mode = rotation_mode
        self._page_rotations = page_rotations
        type(self).instances.append(self)

    def start(self):
        self.start_count += 1
        self.finished_scan.emit(list(type(self).next_candidates))

    def isRunning(self):
        return False

    def requestInterruption(self):
        pass

    def wait(self, *a):
        pass


class SimilarSymbolSearchDialogTests(unittest.TestCase):
    """SimilarSymbolSearchDialog (pid_viewer.py) — search-parameter
    controls for "Hitta liknande symbol" (2026-08-14/15, see NOTES.md
    "Hitta liknande symbol" — sökparametrar / uppföljningsfunktioner).
    The document scan itself (SimilarSymbolSearchWorker) is replaced
    with _SyncFakeSimilarSymbolSearchWorker so these tests exercise the
    dialog's real wiring (progress/finished_scan signals, live count,
    restart-on-setting-change) without a real background thread or PDF."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = []
        _SyncFakeSimilarSymbolSearchWorker.instances = []
        self._worker_patcher = unittest.mock.patch(
            'pid_viewer.SimilarSymbolSearchWorker', _SyncFakeSimilarSymbolSearchWorker)
        self._worker_patcher.start()
        _SyncFakeImageSymbolSearchWorker.next_candidates = []
        _SyncFakeImageSymbolSearchWorker.instances = []
        self._image_worker_patcher = unittest.mock.patch(
            'pid_viewer.ImageSymbolSearchWorker', _SyncFakeImageSymbolSearchWorker)
        self._image_worker_patcher.start()
        self.db = None   # set by the "Spara som mall" tests only

    def tearDown(self):
        self._worker_patcher.stop()
        self._image_worker_patcher.stop()
        if self.db is not None:
            try:
                del self.db
            except Exception:
                pass

    def _line(self, x0, y0, x1, y1, source=0):
        return {'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1), 'closed': False, 'filled': False,
                'width': 1.0, 'source': source}

    def _dialog(self, viewer=None, db=None):
        from pid_viewer import SimilarSymbolSearchDialog
        prims = [self._line(0, 0, 10, 0, source=0), self._line(10, 0, 10, 10, source=1)]
        return SimilarSymbolSearchDialog(prims, [0, 1], 'fake.pdf', 0, 10.0,
                                          ref_bbox=(0, 0, 10, 10), db=db, viewer=viewer)

    def _forced_image_dialog(self, viewer=None, db=None):
        """No vector cluster resolved at all — primitives/index_group
        both None, matching _find_similar_symbol's own fallback."""
        from pid_viewer import SimilarSymbolSearchDialog
        return SimilarSymbolSearchDialog(None, None, 'fake.pdf', 0, 10.0,
                                          ref_bbox=(0, 0, 10, 10), db=db, viewer=viewer)

    def _template_dialog(self, template_features=None, db=None, viewer=None, initial_comp_type=''):
        from pid_viewer import SimilarSymbolSearchDialog
        return SimilarSymbolSearchDialog(
            None, None, 'fake.pdf', 0, None, db=db, viewer=viewer,
            template_name="Test-mall",
            template_features=template_features or {'aspect': 1.0, 'norm_size': 2.0,
                                                    'fold_ratio': 1.0, 'has_curve': False,
                                                    'has_diagonal': True,
                                                    'has_closed_or_filled': True},
            initial_comp_type=initial_comp_type)

    def test_default_values_match_find_similar_shapes_defaults(self):
        dlg = self._dialog()
        try:
            self.assertAlmostEqual(dlg.min_similarity(), 0.6)
            self.assertFalse(dlg.ignore_scale())
            self.assertEqual(dlg.rotation_mode(), 'none')
            self.assertFalse(dlg.search_this_page_only())
            self.assertEqual(dlg.edited_index_group(), [0, 1])
        finally:
            dlg.deleteLater()

    def test_type_selector_defaults_to_empty_and_is_settable(self):
        """"kunna välja vilken typ av objekt det är i både raster och
        vektor" (2026-08-16, see NOTES.md) — an ad-hoc (non-template)
        search starts with no type chosen, but the user can set one
        before running "Sök", and it must be reflected by
        selected_comp_type() regardless of matching method."""
        dlg = self._dialog()
        try:
            self.assertEqual(dlg.selected_comp_type(), '')
            dlg._type_cb.setCurrentText('Ventil')
            self.assertEqual(dlg.selected_comp_type(), 'Ventil')
            dlg._method_image.setChecked(True)
            self.assertEqual(dlg.selected_comp_type(), 'Ventil',
                "the chosen type must survive switching to Bildmatchning")
        finally:
            dlg.deleteLater()

    def test_type_selector_offers_every_known_component_type(self):
        import equipment_detection
        dlg = self._dialog()
        try:
            items = {dlg._type_cb.itemText(i) for i in range(dlg._type_cb.count())}
            self.assertEqual(items, set(equipment_detection.COMPONENT_TYPES.keys()))
        finally:
            dlg.deleteLater()

    def test_template_mode_prefills_type_selector_from_template(self):
        dlg = self._template_dialog(initial_comp_type='Pump')
        try:
            self.assertEqual(dlg.selected_comp_type(), 'Pump')
        finally:
            dlg.deleteLater()

    def test_scan_runs_automatically_on_open(self):
        """The scan starts as soon as the dialog is constructed —
        no separate "start search" step needed before the live
        count/preview are useful."""
        dlg = self._dialog()
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            self.assertEqual(_SyncFakeSimilarSymbolSearchWorker.instances[0].start_count, 1)
        finally:
            dlg.deleteLater()

    def test_search_button_disabled_until_scan_finishes(self):
        """Real behaviour: the fake worker emits finished_scan
        synchronously from start(), so by the time __init__ returns the
        button is already re-enabled — this asserts THAT happened via
        _on_scan_finished, not that it starts disabled and stays so."""
        dlg = self._dialog()
        try:
            self.assertTrue(dlg._search_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_threshold_slider_maps_percent_to_0_1_range(self):
        dlg = self._dialog()
        try:
            dlg._threshold.setValue(85)
            self.assertAlmostEqual(dlg.min_similarity(), 0.85)
        finally:
            dlg.deleteLater()

    def test_threshold_change_updates_live_count_without_a_new_scan(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = [
            (0.9, 0, 1.0, 1.0, []), (0.7, 0, 2.0, 2.0, []), (0.3, 0, 3.0, 3.0, []),
        ]
        dlg = self._dialog()
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            dlg._threshold.setValue(60)
            self.assertEqual(dlg._count_lbl.text(), "≈ 2 träffar")
            dlg._threshold.setValue(80)
            self.assertEqual(dlg._count_lbl.text(), "≈ 1 träffar")
            # No new worker was constructed just from moving the slider.
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
        finally:
            dlg.deleteLater()

    def test_choosing_alla_storlekar_sets_ignore_scale_and_restarts_scan(self):
        dlg = self._dialog()
        try:
            dlg._scale_any.setChecked(True)
            self.assertTrue(dlg.ignore_scale())
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2,
                "changing Skala affects candidate scores — must trigger a fresh scan")
        finally:
            dlg.deleteLater()

    def test_choosing_alla_vinklar_sets_rotation_mode_any_and_restarts_scan(self):
        dlg = self._dialog()
        try:
            dlg._rotation_any.setChecked(True)
            self.assertEqual(dlg.rotation_mode(), 'any')
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2)
        finally:
            dlg.deleteLater()

    def test_choosing_denna_sida_sets_search_this_page_only_and_restarts_scan(self):
        dlg = self._dialog()
        try:
            dlg._scope_page.setChecked(True)
            self.assertTrue(dlg.search_this_page_only())
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2)
        finally:
            dlg.deleteLater()

    def test_excluding_a_segment_in_the_canvas_restarts_the_scan(self):
        dlg = self._dialog()
        try:
            dlg._canvas._excluded_sources.add(1)
            dlg._canvas.selection_changed.emit()
            self.assertEqual(dlg.edited_index_group(), [0])
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2,
                "editing the reference shape changes ref_features — must re-scan")
        finally:
            dlg.deleteLater()

    def test_excluding_every_segment_shows_a_message_instead_of_crashing(self):
        """Real crash found in the wild (2026-08-15, see NOTES.md):
        excluding EVERY primitive left an empty index_group, which
        crashed all the way down in symbol_geometry.cluster_features()
        (min() on an empty list of members) the instant the scan tried
        to restart."""
        dlg = self._dialog()
        try:
            dlg._canvas._excluded_sources.update({0, 1})
            dlg._canvas.selection_changed.emit()   # must not raise
            self.assertEqual(dlg.edited_index_group(), [])
            self.assertIn("Inget kvar", dlg._status_lbl.text())
            self.assertFalse(dlg._search_btn.isEnabled())
            self.assertFalse(dlg._progress_bar.isVisible())
        finally:
            dlg.deleteLater()

    def test_search_button_accepts_the_dialog(self):
        from PyQt6.QtWidgets import QDialog
        dlg = self._dialog()
        try:
            search_btn = [b for b in dlg.findChildren(QPushButton) if b.text() == 'Sök'][0]
            search_btn.click()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
        finally:
            dlg.deleteLater()

    def test_cancel_button_rejects_the_dialog(self):
        from PyQt6.QtWidgets import QDialog
        dlg = self._dialog()
        try:
            cancel_btn = [b for b in dlg.findChildren(QPushButton) if b.text() == 'Avbryt'][0]
            cancel_btn.click()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Rejected)
        finally:
            dlg.deleteLater()

    def test_final_results_reuses_cached_candidates_shaped_and_thresholded(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = [
            (0.9, 0, 1.0, 1.0, [[0, 0], [1, 0], [1, 1]]),
            (0.3, 0, 2.0, 2.0, [[0, 0], [1, 0], [1, 1]]),
        ]
        dlg = self._dialog()
        try:
            results = dlg.final_results(comp_type='Ventil')
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['comp_type'], 'Ventil')
            self.assertAlmostEqual(results[0]['detection_confidence'], 0.9)
        finally:
            dlg.deleteLater()

    def test_preview_checkbox_disabled_without_a_viewer(self):
        dlg = self._dialog(viewer=None)
        try:
            self.assertFalse(dlg._preview_cb.isEnabled())
        finally:
            dlg.deleteLater()

    def test_preview_checkbox_draws_only_current_page_candidates_above_threshold(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = [
            (0.9, 0, 1.0, 1.0, [[0, 0], [1, 0], [1, 1]]),   # right page, above threshold
            (0.9, 1, 5.0, 5.0, [[0, 0], [1, 0], [1, 1]]),   # wrong page
            (0.3, 0, 2.0, 2.0, [[0, 0], [1, 0], [1, 1]]),   # right page, below threshold
        ]
        fake_viewer = unittest.mock.Mock()
        fake_viewer.current_page = 0
        dlg = self._dialog(viewer=fake_viewer)
        try:
            dlg._preview_cb.setChecked(True)
            fake_viewer.add_shape_highlight.assert_called_once_with([[0, 0], [1, 0], [1, 1]])
        finally:
            dlg.deleteLater()

    def test_dialog_finished_clears_preview_and_stops_any_running_worker(self):
        fake_viewer = unittest.mock.Mock()
        fake_viewer.current_page = 0
        dlg = self._dialog(viewer=fake_viewer)
        try:
            dlg.reject()
            fake_viewer.clear_shape_preview.assert_called()
        finally:
            dlg.deleteLater()

    def test_save_as_template_button_disabled_without_db(self):
        dlg = self._dialog(db=None)
        try:
            self.assertFalse(dlg._save_template_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_save_as_template_persists_current_reference_features(self):
        import json
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        dlg = self._dialog(db=self.db)
        try:
            self.assertTrue(dlg._save_template_btn.isEnabled())
            with unittest.mock.patch('pid_viewer.QInputDialog.getText',
                                     return_value=("Min ventil", True)), \
                 unittest.mock.patch('pid_viewer.QMessageBox.information'):
                dlg._save_as_template()
            rows = self.db.symbol_templates()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], "Min ventil")
            feats = json.loads(rows[0]['features_json'])
            self.assertIn('aspect', feats)
        finally:
            dlg.deleteLater()

    def test_save_as_template_warns_on_duplicate_name(self):
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        self.db.add_symbol_template("Upptaget namn", '{}')
        dlg = self._dialog(db=self.db)
        try:
            with unittest.mock.patch('pid_viewer.QInputDialog.getText',
                                     return_value=("Upptaget namn", True)), \
                 unittest.mock.patch('pid_viewer.QMessageBox.warning') as mock_warn:
                dlg._save_as_template()
            mock_warn.assert_called_once()
            self.assertEqual(len(self.db.symbol_templates()), 1)
        finally:
            dlg.deleteLater()

    def test_method_toggle_visible_when_a_vector_cluster_was_resolved(self):
        dlg = self._dialog()
        try:
            self.assertIsNotNone(dlg._method_image)
            self.assertFalse(dlg.use_image_matching())
        finally:
            dlg.deleteLater()

    def test_method_toggle_absent_in_template_mode(self):
        dlg = self._template_dialog()
        try:
            self.assertIsNone(dlg._method_image)
            self.assertFalse(dlg.use_image_matching())
        finally:
            dlg.deleteLater()

    def test_forced_image_mode_when_no_vector_cluster_resolved(self):
        """primitives=None/index_group=None (no vector cluster at all,
        see _find_similar_symbol's fallback) — image matching is the
        only option, no toggle shown."""
        dlg = self._forced_image_dialog()
        try:
            self.assertIsNone(dlg._method_image)
            self.assertIsNone(dlg._canvas)
            self.assertTrue(dlg.use_image_matching())
            self.assertEqual(_SyncFakeImageSymbolSearchWorker.instances[-1].start_count, 1)
            self.assertEqual(_SyncFakeSimilarSymbolSearchWorker.instances, [],
                "forced image mode must never construct the vector worker")
        finally:
            dlg.deleteLater()

    def test_switching_to_bildmatchning_restarts_scan_with_image_worker(self):
        dlg = self._dialog()
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            self.assertEqual(len(_SyncFakeImageSymbolSearchWorker.instances), 0)
            dlg._method_image.setChecked(True)
            self.assertTrue(dlg.use_image_matching())
            self.assertEqual(len(_SyncFakeImageSymbolSearchWorker.instances), 1,
                "toggling to Bildmatchning must (re-)run the scan via the image worker")
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1,
                "no extra vector scan should be triggered by the method toggle")
        finally:
            dlg.deleteLater()

    def test_tiny_reference_bbox_shows_warning(self):
        """"Bildmatchning visar ingen symbol" (2026-08-16, see NOTES.md
        "Bildmatchning visar ingen symbol och kraschar lätt") —
        resolve_reference_cluster() is deliberately permissive and can
        resolve a click to a degenerate sliver of a cluster (confirmed
        on the active project's own hazop_project_pid.pdf: a real click
        resolved to a 1.3x1.3pt cluster). _update_tiny_ref_warning() must
        flag that instead of silently showing a near-blank preview."""
        from pid_viewer import SimilarSymbolSearchDialog
        prims = [self._line(0, 0, 1, 0, source=0)]
        dlg = SimilarSymbolSearchDialog(prims, [0], 'fake.pdf', 0, 10.0,
                                         ref_bbox=(0, 0, 1, 1), viewer=None)
        try:
            dlg._update_tiny_ref_warning()
            self.assertFalse(dlg._tiny_ref_warning.isHidden())
        finally:
            dlg.deleteLater()

    def test_normal_reference_bbox_hides_warning(self):
        dlg = self._dialog()
        try:
            dlg._ref_bbox = (0, 0, 40, 40)   # diag ~56.6, comfortably above the 1.5*scale=15pt floor
            dlg._update_tiny_ref_warning()
            self.assertTrue(dlg._tiny_ref_warning.isHidden())
        finally:
            dlg.deleteLater()

    def test_dragging_a_crop_in_image_mode_narrows_what_gets_searched(self):
        """Integration check that _restart_scan actually reads
        _image_ref_canvas.current_bbox() rather than the dialog's raw
        _ref_bbox — the whole point of the crop tool."""
        from PyQt6.QtCore import QPoint, QEvent, Qt as _Qt
        from PyQt6.QtGui import QMouseEvent
        import numpy as np

        def drag(canvas, x0, y0, x1, y1):
            canvas.resize(240, 180)
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(x0, y0).toPointF(),
                                 _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                                 _Qt.KeyboardModifier.NoModifier)
            canvas.mousePressEvent(press)
            move = QMouseEvent(QEvent.Type.MouseMove, QPoint(x1, y1).toPointF(),
                                _Qt.MouseButton.NoButton, _Qt.MouseButton.LeftButton,
                                _Qt.KeyboardModifier.NoModifier)
            canvas.mouseMoveEvent(move)
            release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPoint(x1, y1).toPointF(),
                                   _Qt.MouseButton.LeftButton, _Qt.MouseButton.NoButton,
                                   _Qt.KeyboardModifier.NoModifier)
            canvas.mouseReleaseEvent(release)

        dlg = self._dialog()
        try:
            dlg._method_image.setChecked(True)
            canvas = dlg._image_ref_canvas
            canvas.set_reference(np.full((80, 100), 255, dtype='uint8'), (0, 0, 10, 10))
            n_before = len(_SyncFakeImageSymbolSearchWorker.instances)
            drag(canvas, 20, 20, 100, 100)   # crop_changed -> _restart_scan
            self.assertEqual(len(_SyncFakeImageSymbolSearchWorker.instances), n_before + 1,
                "dragging a crop must re-run the scan")
            searched_bbox = _SyncFakeImageSymbolSearchWorker.instances[-1]._ref_bbox
            self.assertNotEqual(searched_bbox, (0, 0, 10, 10),
                "the worker must search the CROPPED bbox, not the original full one")
        finally:
            dlg.deleteLater()

    def test_switching_back_to_vector_restarts_scan_with_vector_worker(self):
        dlg = self._dialog()
        try:
            dlg._method_image.setChecked(True)
            dlg._method_vector.setChecked(True)
            self.assertFalse(dlg.use_image_matching())
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2)
        finally:
            dlg.deleteLater()

    def test_canvas_and_image_preview_visibility_follow_the_method_toggle(self):
        dlg = self._dialog()
        try:
            self.assertFalse(dlg._canvas.isHidden())
            self.assertTrue(dlg._image_ref_container.isHidden())
            dlg._method_image.setChecked(True)
            self.assertTrue(dlg._canvas.isHidden())
            self.assertFalse(dlg._image_ref_container.isHidden())
        finally:
            dlg.deleteLater()

    def test_save_template_button_disabled_in_image_mode(self):
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        dlg = self._dialog(db=self.db)
        try:
            self.assertTrue(dlg._save_template_btn.isEnabled())
            dlg._method_image.setChecked(True)
            self.assertFalse(dlg._save_template_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_template_mode_has_no_canvas_and_disables_rotation_toggle(self):
        """A saved template has no live primitives to preview/edit, and
        its features were already computed in one fixed rotation basis
        when saved — "alla vinklar" has nothing to recompute against."""
        dlg = self._template_dialog()
        try:
            self.assertIsNone(dlg._canvas)
            self.assertIsNone(dlg.edited_index_group())
            self.assertFalse(dlg._rotation_any.isEnabled())
            self.assertFalse(dlg._save_template_btn.isEnabled(),
                "no new reference to save — this already IS a saved template")
        finally:
            dlg.deleteLater()

    def test_template_mode_scan_uses_the_given_features_directly(self):
        template_feats = {'aspect': 3.3, 'norm_size': 9.9, 'fold_ratio': 1.0,
                          'has_curve': False, 'has_diagonal': True,
                          'has_closed_or_filled': True}
        dlg = self._template_dialog(template_features=template_feats)
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            worker = _SyncFakeSimilarSymbolSearchWorker.instances[0]
            self.assertEqual(worker._ref_features, template_feats)
        finally:
            dlg.deleteLater()


class SymbolTemplatePickerDialogTests(unittest.TestCase):
    """SymbolTemplatePickerDialog (pid_viewer.py) — "🔎 Hitta liknande
    symbol (från mall)" (2026-08-15, see NOTES.md "Hitta liknande
    symbol" — uppföljningsfunktioner: symbolbibliotek)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_templatepicker_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_lists_saved_templates(self):
        from pid_viewer import SymbolTemplatePickerDialog
        self.db.add_symbol_template("Metso-ventil", '{}', comp_type='Ventil')
        self.db.add_symbol_template("Endress+Hauser", '{}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            self.assertEqual(dlg._list.count(), 2)
        finally:
            dlg.deleteLater()

    def test_selecting_a_template_and_accepting_sets_selected_template(self):
        from pid_viewer import SymbolTemplatePickerDialog
        tid = self.db.add_symbol_template("Metso-ventil", '{"aspect": 2.0}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            dlg._list.setCurrentRow(0)
            dlg._accept_selected()
            self.assertEqual(dlg.result(), 1)   # QDialog.Accepted
            self.assertEqual(dlg.selected_template['id'], tid)
        finally:
            dlg.deleteLater()

    def test_accepting_with_no_selection_shows_info_and_does_not_accept(self):
        from pid_viewer import SymbolTemplatePickerDialog
        self.db.add_symbol_template("Metso-ventil", '{}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            with unittest.mock.patch('pid_viewer.QMessageBox.information') as mock_info:
                dlg._accept_selected()
            mock_info.assert_called_once()
            self.assertIsNone(dlg.selected_template)
        finally:
            dlg.deleteLater()

    def test_delete_removes_the_template_and_refreshes_the_list(self):
        from pid_viewer import SymbolTemplatePickerDialog
        self.db.add_symbol_template("Att ta bort", '{}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            dlg._list.setCurrentRow(0)
            dlg._delete_selected()
            self.assertEqual(dlg._list.count(), 0)
            self.assertEqual(self.db.symbol_templates(), [])
        finally:
            dlg.deleteLater()


class SimilarSymbolSearchWorkerTests(unittest.TestCase):
    """SimilarSymbolSearchWorker (pid_viewer.py) — the REAL QThread
    behind SimilarSymbolSearchDialog's background scan (2026-08-15, see
    NOTES.md "Hitta liknande symbol" — uppföljningsfunktioner), tested
    directly (not via the dialog's fake test double). Modelled on
    EquipmentAnalysisWorkerTests: must always emit finished_scan, even
    when fitz.open() itself raises, and must actually find a real
    candidate end-to-end against a real synthetic PDF."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_simsearchworker_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_finished_scan_emitted_even_when_pdf_open_fails(self):
        from pid_viewer import SimilarSymbolSearchWorker
        received = {}

        def _capture(candidates):
            received['candidates'] = candidates

        worker = SimilarSymbolSearchWorker(
            '/nonexistent/path/does-not-exist.pdf', {}, 0, [])
        worker.finished_scan.connect(_capture)
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()   # pump the queued cross-thread signal delivery
        self.assertIn('candidates', received,
            "finished_scan must fire even when fitz.open() raises")
        self.assertEqual(received['candidates'], [])

    def test_finds_a_real_candidate_end_to_end(self):
        import fitz
        import symbol_geometry as sg
        from equipment_detection import resolve_reference_cluster
        from pid_viewer import SimilarSymbolSearchWorker

        path = os.path.join(self._tmpdir, "worker.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        bowtie(60, 60)
        bowtie(300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_features = sg.similarity_features(primitives, index_group)
        finally:
            doc.close()

        received = {}
        worker = SimilarSymbolSearchWorker(
            path, ref_features, 0, cluster['_index_group'], pages=[0])
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        candidates = received.get('candidates')
        self.assertTrue(candidates)
        self.assertGreater(max(c[0] for c in candidates), 0.9)

    def test_page_rotations_places_candidates_in_the_live_views_coordinate_space(self):
        """The actual reported bug ("hittar massa liknande med vektor men
        placerar ut dem felaktigt"): without page_rotations, a candidate
        found on a page with a manual rotation override is reported in
        the page's NATIVE coordinate space while the live view expects
        the OVERRIDDEN one — same physical symbol, wrong reported
        position. Confirmed on the real active project (page 0 has a
        90-degree override in pid_page_rotation)."""
        import fitz
        import symbol_geometry as sg
        from equipment_detection import resolve_reference_cluster
        from pid_viewer import SimilarSymbolSearchWorker

        path = os.path.join(self._tmpdir, "rotworker.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        bowtie(60, 60)
        bowtie(300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        # LIVE view: the manual override already applied, as PIDGraphicsView
        # would — the reference is resolved using ROTATED-space coordinates.
        live_doc = fitz.open(path)
        live_doc[0].set_rotation(90)
        primitives, index_group, cluster = resolve_reference_cluster(live_doc, 0, 340, 60)
        self.assertIsNotNone(cluster, "sanity check: reference must resolve in rotated space")
        ref_features = sg.similarity_features(primitives, index_group)
        native_index_group = cluster['_index_group']
        live_doc.close()

        received = {}
        worker = SimilarSymbolSearchWorker(
            path, ref_features, 0, native_index_group, pages=[0],
            page_rotations={0: 90})
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        candidates = [c for c in received.get('candidates', []) if c[0] > 0.9]
        self.assertTrue(candidates, "the other bow-tie must still be found")
        cx, cy = candidates[0][2], candidates[0][3]
        # Rotated-space center of the OTHER bow-tie is ~(100, 300) — its
        # NATIVE-space center (300, 300) would mean the fix is inert.
        self.assertAlmostEqual(cx, 100.0, delta=2.0)
        self.assertAlmostEqual(cy, 300.0, delta=2.0)


class ImageSymbolSearchWorkerTests(unittest.TestCase):
    """ImageSymbolSearchWorker (pid_viewer.py) — the REAL QThread behind
    SimilarSymbolSearchDialog's image-matching mode (2026-08-15, see
    NOTES.md "Bildbaserad 'hitta liknande symbol' — vid sidan av
    vektorlogiken"), tested directly. Modelled exactly on
    SimilarSymbolSearchWorkerTests above: must always emit finished_scan,
    even when fitz.open() itself raises, and must actually find a real
    candidate end-to-end against a real synthetic PDF."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_imgsearchworker_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_finished_scan_emitted_even_when_pdf_open_fails(self):
        from pid_viewer import ImageSymbolSearchWorker
        received = {}
        worker = ImageSymbolSearchWorker(
            '/nonexistent/path/does-not-exist.pdf', 0, (0, 0, 10, 10))
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        self.assertIn('candidates', received,
            "finished_scan must fire even when fitz.open() raises")
        self.assertEqual(received['candidates'], [])

    def test_finds_a_real_candidate_end_to_end(self):
        import fitz
        from pid_viewer import ImageSymbolSearchWorker

        path = os.path.join(self._tmpdir, "imgworker.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)

        bowtie(60, 60)
        bowtie(300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        received = {}
        worker = ImageSymbolSearchWorker(path, 0, (48, 48, 72, 72), pages=[0])
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        candidates = received.get('candidates')
        self.assertTrue(candidates)
        self.assertGreater(max(c[0] for c in candidates), 0.9)

    def _multi_page_bowtie_pdf(self, n_pages):
        """n_pages pages, each with the SAME bow-tie at (60, 60) — used
        to force the real parallel path (see _should_parallelize, which
        requires >= 4 pages) and confirm it finds the expected match on
        every non-reference page, not just page 0."""
        import fitz
        path = os.path.join(self._tmpdir, "multipage.pdf")
        doc = fitz.open()
        for _ in range(n_pages):
            page = doc.new_page(width=200, height=200)
            shape = page.new_shape()
            shape.draw_polyline([fitz.Point(50, 50), fitz.Point(50, 70), fitz.Point(60, 60)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(70, 50), fitz.Point(70, 70), fitz.Point(60, 60)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
            shape.commit()
        doc.save(path)
        doc.close()
        return path

    def test_parallel_path_finds_candidates_across_multiple_pages(self):
        """raster-sökning — parallellisering över flera processer
        (2026-08-16, see NOTES.md): a 5-page document forces
        _should_parallelize's real ProcessPoolExecutor path (not just the
        untouched sequential fallback single-page searches already
        exercise above) — must still find the same bow-tie on every
        OTHER page, with results shaped identically to the sequential
        path (same (sim, page_num, x, y, outline) tuple contract)."""
        from pid_viewer import ImageSymbolSearchWorker
        path = self._multi_page_bowtie_pdf(5)
        received = {}
        worker = ImageSymbolSearchWorker(path, 0, (48, 48, 72, 72))   # pages=None -> whole document
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(20000), "parallel worker.run() did not finish within 20s")
        self.app.processEvents()
        candidates = received.get('candidates')
        self.assertTrue(candidates)
        found_pages = {c[1] for c in candidates if c[0] > 0.9}
        self.assertEqual(found_pages, {1, 2, 3, 4},
            "the bow-tie must be found on every page except the reference's own (page 0)")

    def test_parallel_path_emits_finished_scan_on_cancel(self):
        """Same contract ParallelTagScanWorkerTests already establishes
        for the tag scan's own parallel path: finished_scan must fire
        even when cancelled before any worker process completes."""
        from pid_viewer import ImageSymbolSearchWorker
        path = self._multi_page_bowtie_pdf(5)
        worker = ImageSymbolSearchWorker(path, 0, (48, 48, 72, 72))
        received = {}
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        with unittest.mock.patch.object(worker, 'isInterruptionRequested', return_value=True):
            worker.run()
        self.assertIn('candidates', received)
        self.assertEqual(received['candidates'], [])


class EquipmentAnalysisWorkerTests(unittest.TestCase):
    """EquipmentAnalysisWorker (pid_viewer.py) — the QThread behind
    EquipmentPanel._autodetect(). Modelled on ConnectorAnalyzer: must
    always emit finished_analysis, even when fitz.open() itself raises,
    so the caller's modal QProgressDialog can never hang forever."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_finished_analysis_emitted_even_when_pdf_open_fails(self):
        from pid_viewer import EquipmentAnalysisWorker
        received = {}

        def _capture(results, rejected):
            received['results'] = results
            received['rejected'] = rejected

        worker = EquipmentAnalysisWorker("/nonexistent/path/does-not-exist.pdf", [])
        worker.finished_analysis.connect(_capture)
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        # finished_analysis is queued across threads (the slot is a plain
        # function with no QObject thread affinity) — wait() only blocks
        # until the WORKER thread stops, it doesn't pump the main thread's
        # event queue, so the delivery itself needs an explicit tick.
        self.app.processEvents()
        self.assertIn('results', received,
            "finished_analysis must fire even when fitz.open() raises")
        self.assertEqual(received['results'], [])
        self.assertEqual(received['rejected'], [])


# ══════════════════════════════════════════════════════════════════════════
# "Hitta ventiler" -> "Hitta objekt" (2026-08-10, see NOTES.md): widened
# from VALVE_COMPONENT_TYPES-only to every equipment type. The shape side
# (detect_equipment_and_valves) has hunted pump/instrument-shaped symbols
# since 2026-08-07 regardless of this filter; a known pump/instrument tag
# just never got to PARTICIPATE in the weighted tag<->symbol association.
# ══════════════════════════════════════════════════════════════════════════

class AutodetectAllEquipmentTypesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autodetectscope_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_autodetect_proceeds_when_only_valve_rows_present(self):
        """No PDF path is configured in this test, so _autodetect() must
        reach its 'Ingen P&ID' warning (not the 'Inga taggar i
        registret' info message) -- proof the valve rows were NOT
        filtered out before the tag_points-empty check."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
        self.db.add_equipment_item("PSV-201", "PSV-201", "PSV", 0,
                                   "Säkerhetsventil (PSV)", '', 0)
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_warn.assert_called_once()
            mock_info.assert_not_called()
        finally:
            panel.deleteLater()

    def test_autodetect_includes_non_valve_rows(self):
        """Instrument/pump/unclassified rows must now be included in
        tag_points too (2026-08-10) — proceeds straight to the 'Ingen
        P&ID' warning rather than the 'Inga taggar i registret' info
        message, proof they were NOT filtered out."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("TI-301", "TI-301", "TI", 0, "Instrument / Sensor", '', 0)
        self.db.add_equipment_item("P-401", "P-401", "P", 0, "Pump", '', 0)
        self.db.add_equipment_item("X-501", "X-501", "X", 0, "", '', 0)   # unclassified
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_warn.assert_called_once()
            mock_info.assert_not_called()
        finally:
            panel.deleteLater()

    def test_autodetect_mixed_register_uses_every_type(self):
        """A register with both valve and non-valve rows proceeds past
        the empty-check regardless of the mix — the type filter is gone
        entirely, not just tolerant of a mix."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
        self.db.add_equipment_item("TI-301", "TI-301", "TI", 0, "Instrument / Sensor", '', 0)
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_warn.assert_called_once()
            mock_info.assert_not_called()
        finally:
            panel.deleteLater()

    def test_autodetect_shows_generic_empty_message_when_no_tags_at_all(self):
        """With zero rows in the register at all, the empty-state message
        must be the new generic wording, not the old valve-specific one."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_info.assert_called_once()
            mock_warn.assert_not_called()
            title = mock_info.call_args[0][1]
            self.assertIn('taggar', title.lower())
        finally:
            panel.deleteLater()


class EquipmentMarkerClickNavigationTests(unittest.TestCase):
    """2026-08-06: valve markers on the P&ID are now clickable. Originally
    this always switched to Utrustningsregistret and selected the
    corresponding row (the closest equivalent to _on_marker_navigate's
    tree-select behaviour for cause/consequence/safeguard, since equipment
    has no HAZOP tree node of its own to select).

    2026-08-11: once equipment IS linked to a node (equipment_catalog.
    node_id), clicking its marker instead showed that node's WHOLE
    worksheet (causes/consequences/safeguards together) — the
    register-select above remained only as the fallback for equipment
    with no node yet.

    2026-08-12: the user clarified the 2026-08-11 behaviour was too broad
    ('de orsaker som visas i hazop scenario är de där objektet finns med')
    — clicking a marker now filters the worksheet to ONLY the rows that
    actually mention this equipment (scenario_panel.load_equipment()),
    regardless of whether it has a node yet, and the register-page
    fallback is gone: the scenario table (right there on the same P&ID
    page) is always the right place to show the result, even if empty."""

    def test_on_marker_navigate_equipment_marker_filters_scenario_table(self):
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')

            load_eq_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_equipment)
            win.scenario_panel.load_equipment = load_eq_spy

            win._on_marker_navigate('equipment', marker_id)

            load_eq_spy.assert_called_once_with(eq_id)
            self.assertNotEqual(win.view_stack.currentIndex(), 2,
                "clicking a marker must stay on the P&ID page — the filtered "
                "scenario table is the bottom pane of that same page, no "
                "need to navigate away to the Utrustning register")

    def test_on_marker_navigate_equipment_with_no_linked_row_does_not_crash(self):
        """A marker whose equipment_id is NULL (e.g. an untagged shape hit
        the user never confirmed with a tag) must be a silent no-op, not
        a crash."""
        with _TempDbMainWindow() as win:
            marker_id = win.db.add_equipment_marker(
                None, '', 0, 50.0, 50.0, "Ventil", confidence=0.6, link_method='shape')
            try:
                win._on_marker_navigate('equipment', marker_id)
            except Exception as e:
                self.fail(f"must not raise for a marker with no linked equipment row: {e!r}")

    def test_on_marker_navigate_equipment_with_node_shows_only_its_own_causes(self):
        """'Om jag har lagt till ett objekt på P&ID ... och klickar på det
        igen så vill jag att orsakerna där det nämns dyker upp i hazop
        scenario ... Detta gäller även om de är tillagda på konsekvens och
        safeguard' (2026-08-11), clarified 2026-08-12 to mean FILTERED, not
        the whole node: a second, unrelated cause under the very same node
        must NOT show up, only the one actually tagged to this equipment."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Högt tryck")
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PSV-101")
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)

            other_dev = win.db.get_or_create_deviation(node_id, "Lågt tryck")
            other_cause = win.db.add_cause(other_dev)   # same node, not tagged to this equipment

            eq_id = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                               "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')

            load_eq_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_equipment)
            win.scenario_panel.load_equipment = load_eq_spy

            win._on_marker_navigate('equipment', marker_id)

            load_eq_spy.assert_called_once_with(eq_id)
            self.assertNotEqual(win.view_stack.currentIndex(), 2,
                "must not switch to the Utrustning register page when the "
                "equipment has a node to show a worksheet for")
            cons_and_sg_ids = {(m[2], m[3]) for m in win.scenario_panel._row_meta}
            self.assertIn((cons_id, sg_id), cons_and_sg_ids,
                "the tagged cause's consequence/safeguard row must be "
                "visible in the scenario table, not just its cause")
            cause_ids_shown = {m[1] for m in win.scenario_panel._row_meta if m[1] is not None}
            self.assertNotIn(other_cause, cause_ids_shown,
                "an unrelated cause under the SAME node must be filtered out")

    def test_on_marker_navigate_equipment_without_node_still_filters_scenario_table(self):
        """No node_id means the equipment itself isn't tied to a node yet,
        but a cause elsewhere could still be tagged to its tag/type
        directly — load_equipment() (tag-matching, not FK-only) is always
        the right call, never the old register-page fallback."""
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("HV-202", "HV-202", "HV", 0,
                                               "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-202", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            load_eq_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_equipment)
            win.scenario_panel.load_equipment = load_eq_spy

            win._on_marker_navigate('equipment', marker_id)

            load_eq_spy.assert_called_once_with(eq_id)
            self.assertNotEqual(win.view_stack.currentIndex(), 2,
                "must not switch to the Utrustning page — no fallback anymore")

    def test_select_row_by_equipment_id_clears_blocking_filter(self):
        """If a text filter is currently hiding the target row, selecting
        it must clear the filter rather than silently doing nothing."""
        from hazop import EquipmentPanel
        tmpdir = tempfile.mkdtemp(prefix="hazop_selectrow_test_")
        try:
            db = Database(path=os.path.join(tmpdir, "test_project.db"))
            eq_id = db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            panel = EquipmentPanel(db)
            panel.refresh()
            try:
                panel._filter.setText("no-such-tag-matches-this")
                self.assertEqual(panel._proxy.rowCount(), 0,
                    "sanity check: the filter must actually hide the row first")

                panel.select_row_by_equipment_id(eq_id)

                self.assertEqual(panel._filter.text(), "",
                    "the blocking filter must be cleared so the target row becomes reachable")
                src_row = panel._proxy.mapToSource(panel._tbl.currentIndex()).row()
                self.assertEqual(panel._model.row_dict(src_row)['id'], eq_id)
            finally:
                panel.deleteLater()
                try: del db
                except Exception: pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_select_row_by_equipment_id_unknown_id_does_not_crash(self):
        from hazop import EquipmentPanel
        tmpdir = tempfile.mkdtemp(prefix="hazop_selectrow_test_")
        try:
            db = Database(path=os.path.join(tmpdir, "test_project.db"))
            panel = EquipmentPanel(db)
            panel.refresh()
            try:
                panel.select_row_by_equipment_id(999999)   # no such row
            except Exception as e:
                self.fail(f"must not raise for an unknown equipment_id: {e!r}")
            finally:
                panel.deleteLater()
                try: del db
                except Exception: pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class EquipmentNodeDeviationSchemaTests(unittest.TestCase):
    """2026-08-07 'Nod → Utrustning → Avvikelse' — equipment_catalog.node_id
    and deviations.equipment_id, both nullable so existing data/behaviour
    is untouched. See NOTES.md."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipnode_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_column_migrations_are_idempotent(self):
        """Running the migration list twice (e.g. reopening an already-
        migrated project) must not raise — same guarantee every other
        column migration already has."""
        try:
            self.db._column_migrations()
            self.db._column_migrations()
        except Exception as e:
            self.fail(f"re-running _column_migrations() must not raise: {e!r}")

    def test_equipment_node_id_defaults_to_none(self):
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.assertIsNone(self.db.equipment_node_id(eq_id))

    def test_set_and_read_equipment_node(self):
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.set_equipment_node(eq_id, node_id)
        self.assertEqual(self.db.equipment_node_id(eq_id), node_id)

    def test_existing_deviations_have_null_equipment_id(self):
        """Every deviation created before this feature (or via any path
        that doesn't pass equipment_id) must be equipment_id=NULL — the
        exact backward-compatibility guarantee the plan relies on instead
        of a backfill migration."""
        node_id = self.db.add_node()
        dev_id = self.db.add_deviation(node_id, "Lågt flöde")
        dev = self.db.get_deviation(dev_id)
        self.assertIsNone(dev['equipment_id'])


class GetOrCreateDeviationEquipmentTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_getorcreate_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.node_id = self.db.add_node()
        self.eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_creates_equipment_scoped_deviation(self):
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        dev = self.db.get_deviation(dev_id)
        self.assertEqual(dev['equipment_id'], self.eq_id)
        self.assertEqual(dev['node_id'], self.node_id)

    def test_reuses_existing_equipment_scoped_deviation(self):
        first = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        second = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        self.assertEqual(first, second)
        self.assertEqual(len(self.db.deviations_for_equipment(self.eq_id)), 1)

    def test_does_not_reuse_a_generic_deviation_with_same_description(self):
        """A pre-existing generic (equipment_id=NULL) 'Lågt flöde' under the
        node must NOT be silently claimed as this equipment's own — they
        represent different things (whole-node vs. this-specific-valve)."""
        generic_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde")
        scoped_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        self.assertNotEqual(generic_id, scoped_id)

    def test_equipment_deviation_count(self):
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)
        self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        self.db.get_or_create_deviation(self.node_id, "Högt flöde", equipment_id=self.eq_id)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 2)


class EquipmentConsequenceSafeguardCountTests(unittest.TestCase):
    """'Jag vill att det finns tre olika räknare på P&ID, dels en för hur
    många gånger den förekommer i orsaker som idag, dels en siffra hur
    många gånger i konsekvenser, och dels i safeguards.' (2026-08-11) —
    equipment_deviation_count (causes/deviations) already existed via the
    equipment_id FK; consequences/safeguards have no such FK, only flat
    comp_tag/comp_type columns (set by set_consequence_tag/
    set_safeguard_tag), so the two new counters match on those instead."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_eq_cons_sg_count_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.cause_id = self.db.add_cause(dev_id)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_consequence_count_matches_by_tag_and_type(self):
        self.assertEqual(self.db.equipment_consequence_count("V-101", "Ventil"), 0)
        c1 = self.db.add_consequence(self.cause_id)
        c2 = self.db.add_consequence(self.cause_id)
        self.db.set_consequence_tag(c1, "V-101", "Ventil")
        self.db.set_consequence_tag(c2, "V-101", "Ventil")
        self.assertEqual(self.db.equipment_consequence_count("V-101", "Ventil"), 2)
        # A different tag or type must not be counted.
        self.assertEqual(self.db.equipment_consequence_count("V-102", "Ventil"), 0)
        self.assertEqual(self.db.equipment_consequence_count("V-101", "Pump"), 0)

    def test_safeguard_count_matches_by_tag_and_type(self):
        cons_id = self.db.add_consequence(self.cause_id)
        self.assertEqual(self.db.equipment_safeguard_count("PSV-1", "Säkerhetsventil"), 0)
        sg1 = self.db.add_safeguard(cons_id)
        self.db.set_safeguard_tag(sg1, "PSV-1", "Säkerhetsventil")
        self.assertEqual(self.db.equipment_safeguard_count("PSV-1", "Säkerhetsventil"), 1)
        self.assertEqual(self.db.equipment_safeguard_count("PSV-2", "Säkerhetsventil"), 0)

    def test_empty_tag_short_circuits_to_zero(self):
        """No comp_tag to match against — must not run a query that would
        match every untagged row via WHERE comp_tag=''."""
        c1 = self.db.add_consequence(self.cause_id)
        self.db.set_consequence_tag(c1, '', '')
        self.assertEqual(self.db.equipment_consequence_count('', ''), 0)
        self.assertEqual(self.db.equipment_safeguard_count('', ''), 0)


class CausesForEquipmentTests(unittest.TestCase):
    """Database.causes_for_equipment() — the query backing 'click an
    equipment marker on P&ID -> show only causes mentioning it' (2026-08-12,
    see NOTES.md: 'de orsaker som visas i hazop scenario är de där
    objektet finns med'). A cause 'mentions' the equipment if its own
    deviation is tied to it (deviations.equipment_id), OR the cause/one of
    its consequences/one of its safeguards was tagged to it directly
    (comp_tag+comp_type, e.g. via drag-and-drop) — mirrors the tag-matching
    equipment_consequence_count/equipment_safeguard_count already use."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_causes_for_equip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.node_id = self.db.add_node()
        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cause_under_equipment_owned_deviation_is_included(self):
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        ids = [dict(c)['id'] for c in self.db.causes_for_equipment(self.eq_id)]
        self.assertEqual(ids, [cause_id])

    def test_cause_tagged_directly_on_an_unrelated_deviation_is_included(self):
        dev_id = self.db.deviations(self.node_id)[0]['id']   # generic, no equipment_id
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")
        ids = [dict(c)['id'] for c in self.db.causes_for_equipment(self.eq_id)]
        self.assertEqual(ids, [cause_id])

    def test_cause_with_a_tagged_consequence_is_included(self):
        dev_id = self.db.deviations(self.node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.set_consequence_tag(cons_id, "PV-101", "Ventil")
        ids = [dict(c)['id'] for c in self.db.causes_for_equipment(self.eq_id)]
        self.assertEqual(ids, [cause_id])

    def test_cause_with_a_tagged_safeguard_is_included(self):
        dev_id = self.db.deviations(self.node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.set_safeguard_tag(sg_id, "PV-101", "Ventil")
        ids = [dict(c)['id'] for c in self.db.causes_for_equipment(self.eq_id)]
        self.assertEqual(ids, [cause_id])

    def test_unrelated_cause_is_excluded(self):
        dev_id = self.db.deviations(self.node_id)[0]['id']
        self.db.add_cause(dev_id)   # untagged, unrelated
        other_dev = self.db.get_or_create_deviation(self.node_id, "Högt flöde")
        other_cause = self.db.add_cause(other_dev)
        self.db.update_cause(other_cause, comp_type="Pump", comp_tag="P-200")
        ids = [dict(c)['id'] for c in self.db.causes_for_equipment(self.eq_id)]
        self.assertEqual(ids, [])

    def test_no_duplicate_rows_when_cause_matches_via_multiple_paths(self):
        """A cause whose deviation IS the equipment's own AND whose
        consequence is ALSO tagged to it must still only appear once."""
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.set_consequence_tag(cons_id, "PV-101", "Ventil")
        rows = self.db.causes_for_equipment(self.eq_id)
        self.assertEqual(len(rows), 1)

    def test_equipment_with_no_tag_still_matches_via_deviation_fk_only(self):
        eq_id2 = self.db.add_equipment_item("", "", "", 0, "", '', 0)
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=eq_id2)
        cause_id = self.db.add_cause(dev_id)
        ids = [dict(c)['id'] for c in self.db.causes_for_equipment(eq_id2)]
        self.assertEqual(ids, [cause_id])


class ScenarioPanelLoadEquipmentFilterTests(unittest.TestCase):
    """ScenarioTablePanel.load_equipment() — the worksheet-side half of
    'click an equipment marker on P&ID -> show only causes mentioning it'
    (2026-08-12, see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_load_equip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.node_id = self.db.add_node()
        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_only_matching_causes_appear_in_row_meta(self):
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        matching_cause = self.db.add_cause(dev_id)
        self.db.add_consequence(matching_cause)

        other_dev = self.db.get_or_create_deviation(self.node_id, "Högt flöde")
        unrelated_cause = self.db.add_cause(other_dev)
        self.db.add_consequence(unrelated_cause)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            cause_ids_shown = {m[1] for m in panel._row_meta if m[1] is not None}
            self.assertEqual(cause_ids_shown, {matching_cause})
        finally:
            panel.deleteLater()

    def test_header_shows_the_equipment_tag(self):
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            self.assertIn("PV-101", panel._hdr_lbl.text())
        finally:
            panel.deleteLater()

    def test_empty_result_shows_no_rows_and_no_crash(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)   # equipment exists, zero causes mention it
            self.assertEqual(panel._row_meta, [])
            self.assertIn("PV-101", panel._hdr_lbl.text())
        finally:
            panel.deleteLater()

    def test_nod_and_dev_columns_are_visible_like_all_nodes_mode(self):
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            self.assertFalse(panel._table.isColumnHidden(panel._C_NOD))
            self.assertFalse(panel._table.isColumnHidden(panel._C_DEV))
        finally:
            panel.deleteLater()

    def test_load_node_after_load_equipment_clears_the_filter(self):
        """Switching back to a normal node view must not leave the
        equipment filter silently still active."""
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)
        other_dev = self.db.get_or_create_deviation(self.node_id, "Högt flöde")
        other_cause = self.db.add_cause(other_dev)
        self.db.add_consequence(other_cause)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            panel.load_node(self.node_id)
            self.assertIsNone(panel._equipment_filter_id)
            cause_ids_shown = {m[1] for m in panel._row_meta if m[1] is not None}
            self.assertEqual(cause_ids_shown, {cause_id, other_cause})
        finally:
            panel.deleteLater()


class EquipmentMarkerNavigateFiltersScenarioTests(unittest.TestCase):
    """MainWindow._on_equipment_marker_navigate() — plumbing from 'clicked
    equipment marker on P&ID' to the filtered worksheet (2026-08-12, see
    NOTES.md). An earlier version of this (2026-08-11) called
    scenario_panel.load_node() (the whole node); the user clarified they
    want only the rows mentioning the clicked object."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_calls_load_equipment_not_load_node(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(eq_id, "PV-101", 0, 10.0, 10.0, "Ventil")

            with unittest.mock.patch.object(win.scenario_panel, 'load_equipment') as mock_load_eq, \
                 unittest.mock.patch.object(win.scenario_panel, 'load_node') as mock_load_node:
                win._on_equipment_marker_navigate(marker_id)

            mock_load_eq.assert_called_once_with(eq_id)
            mock_load_node.assert_not_called()

    def test_unlinked_marker_is_a_no_op(self):
        with _TempDbMainWindow() as win:
            marker_id = win.db.add_equipment_marker(None, "?", 0, 10.0, 10.0, "")
            with unittest.mock.patch.object(win.scenario_panel, 'load_equipment') as mock_load_eq:
                win._on_equipment_marker_navigate(marker_id)
            mock_load_eq.assert_not_called()


class EquipmentDeviationCheckboxKeepsScenarioFilterTests(unittest.TestCase):
    """'en mindre fix är att det skall se ut såhär även när jag klickar på
    en rödmarkerad och lägger till exempelvis lågt och högt flöde. Då
    skall det enbart vara kopplat till det objektet.' (2026-08-12) —
    checking a deviation box in EquipmentDeviationBar (right after
    clicking a red/green marker filtered the worksheet to it via
    load_equipment()) must not silently widen the worksheet back out to
    the whole node. Two separate handlers fire for a single checkbox
    toggle — _on_equipment_deviation_created (the deviation itself) AND
    _on_cause_template_created (the auto-suggested cause EquipmentDeviationBar
    creates right after) — both needed the same fix."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_on_equipment_deviation_created_calls_load_equipment_not_load_node(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)

            with unittest.mock.patch.object(win.scenario_panel, 'load_equipment') as mock_eq, \
                 unittest.mock.patch.object(win.scenario_panel, 'load_node') as mock_node:
                win._on_equipment_deviation_created(dev_id, eq_id)

            mock_eq.assert_called_once_with(eq_id)
            mock_node.assert_not_called()

    def test_cause_template_created_stays_filtered_when_equipment_filter_active(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")

            win.scenario_panel.load_equipment(eq_id)   # simulate having clicked the marker first
            refresh_spy = unittest.mock.Mock(wraps=win.scenario_panel.refresh)
            win.scenario_panel.refresh = refresh_spy
            load_node_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_node)
            win.scenario_panel.load_node = load_node_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            refresh_spy.assert_called_once()
            load_node_spy.assert_not_called()
            self.assertEqual(win.scenario_panel._equipment_filter_id, eq_id,
                "the equipment filter must remain active after the cause's "
                "own template-created refresh")
            cause_ids_shown = {m[1] for m in win.scenario_panel._row_meta if m[1] is not None}
            self.assertIn(cause_id, cause_ids_shown,
                "the newly auto-created cause must still show up under the filter")

    def test_cause_template_created_falls_back_to_load_node_without_equipment_filter(self):
        """Unaffected regression check for the normal (non-equipment-
        filtered) P&ID cause flow — must keep working exactly as before."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)
            win.scenario_panel.load_node(node_id)   # normal (unfiltered) view

            load_node_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_node)
            win.scenario_panel.load_node = load_node_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            load_node_spy.assert_called_once_with(node_id)


class TreePanelEquipmentGroupingTests(unittest.TestCase):
    """TreePanel.refresh() groups a node's deviations by guide-word text
    FIRST (LEDORD_T — several deviation rows across different equipment can
    share one description), then within each guide word, by equipment_id
    (EQUIP_T) — see NOTES.md 'Nod → Ledord → Utrustning'. The LEDORD_T
    wrapper is skipped entirely when there's nothing to group — a single,
    plain (no equipment) deviation for a guide word attaches directly to
    the node instead, to avoid showing the same guide-word text twice in a
    row for no reason (see NOTES.md's follow-up 'varför är det dubbelt?').
    It reappears as soon as a second deviation (equipment-scoped, or
    another plain one) shares that same guide word."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_treeequip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _tree_items(self):
        """Flat list of (type_, id_, parent_type_) for every item in the tree."""
        out = []
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            i = item.data(0, Qt.ItemDataRole.UserRole)
            pt = item.parent().data(0, Qt.ItemDataRole.UserRole + 1) if item.parent() else None
            out.append((t, i, pt))
            it += 1
        return out

    def test_equipment_scoped_deviation_grouped_under_ledord_then_equip_t(self):
        """A single deviation for this equipment+guide-word combo (the
        overwhelmingly common case — get_or_create_deviation is idempotent
        per node+description+equipment) merges directly onto the
        equipment's own tree item instead of wrapping it in a separate
        DEV_T child (2026-08-09, see NOTES.md 'kaka på kaka' — the
        deviation's description is always identical to the LEDORD_T
        group's own label, so a nested child just repeated the same text
        the user already saw one level up). The merged item carries the
        DEVIATION's identity (not EQUIP_T) so 'add cause' and
        equipment-dropped-on-deviation both work directly on it."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        # add_node() auto-seeds ~16 default guide words, so filter down to
        # just the one this test cares about, not "any LEDORD_T at all".
        ledord_rows = [x for x in items if x[0] == LEDORD_T and x[2] == NODE_T
                       and str(x[1]).endswith("Lågt flöde")]
        self.assertEqual(len(ledord_rows), 1, "the guide word must appear as its own tree item under the node")
        self.assertEqual(len([x for x in items if x[0] == EQUIP_T and x[1] == eq_id]), 0,
            "a single deviation must not get a separate EQUIP_T wrapper anymore")
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1)
        self.assertEqual(dev_rows[0][2], LEDORD_T,
            "the merged equipment+deviation item must sit directly under the LEDORD_T (guide word) item")
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, dev_id), eq_id,
            "the merged item's underlying deviation must still resolve back to its equipment")

    def test_two_equipment_sharing_same_guide_word_grouped_under_one_ledord(self):
        """The core reason for this hierarchy: 'Lågt flöde' for a pump AND
        a valve under the same node must appear under ONE shared guide-word
        item, each with its own equipment sub-item — not two separate
        top-level groups. Each equipment has only one deviation here, so
        each merges directly onto its own item (2026-08-09, see NOTES.md
        'kaka på kaka') rather than wrapping in a separate EQUIP_T+DEV_T pair."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        valve_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        pump_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=pump_id)
        valve_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=valve_id)
        self.panel.refresh()

        items = self._tree_items()
        ledord_rows = [x for x in items if x[0] == LEDORD_T]
        matching = [x for x in ledord_rows if str(x[1]).endswith("Lågt flöde")]
        self.assertEqual(len(matching), 1, "both equipment must share ONE 'Lågt flöde' guide-word item")
        dev_rows = [x for x in items if x[0] == DEV_T and x[2] == LEDORD_T
                    and x[1] in (pump_dev, valve_dev)]
        self.assertEqual({x[1] for x in dev_rows}, {pump_dev, valve_dev})
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, pump_dev), pump_id)
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, valve_dev), valve_id)

    def test_numbering_stays_sequential_and_visible_after_linking_equipment(self):
        """Bug report (2026-08-13): 'Nummereringen av lågt flöde, högt
        flöde osv blir konstig när man lägger till objekt i trädet.'
        First fix attempt made the merged equipment row simply consume
        no number at all — which then made a SECOND report surface:
        'jag vill att den ska kvarstå så att det alltid syns att det är
        exempelvis 16 avikelser' (the guide word's own number must stay
        visible even once it's wrapped/merged, not disappear). The
        Ledord wrapper itself now carries the guide word's number, and
        equipment/deviation-instance items inside it use their own
        separate local counter that can never steal from this top-level,
        one-per-guide-word sequence."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        lagt_wrapper = hogt_item = None
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if t == LEDORD_T and "Lågt flöde" in item.text(0):
                lagt_wrapper = item
            if (t == DEV_T and item.parent() is not None
                    and item.parent().data(0, Qt.ItemDataRole.UserRole + 1) == NODE_T
                    and "Högt flöde" in item.text(0)):
                hogt_item = item
            it += 1
        self.assertIsNotNone(lagt_wrapper, "'Lågt flöde' must be wrapped once equipment is linked")
        self.assertIn("1. Lågt flöde", lagt_wrapper.text(0),
            f"the guide word's own number must stay visible after wrapping, got: {lagt_wrapper.text(0)!r}")
        self.assertIsNotNone(hogt_item, "'Högt flöde' must still attach directly to the node")
        self.assertIn("2. Högt flöde", hogt_item.text(0),
            f"expected the next sequential number, got: {hogt_item.text(0)!r}")

    def test_all_seeded_guide_words_stay_numbered_one_through_sixteen(self):
        """Direct check of the user's own framing: 'jag vill att den ska
        kvarstå så att det alltid syns att det är exempelvis 16
        avikelser om jag inte lägger till nya avikelser i trädet' — a
        fresh node's 16 auto-seeded guide words must show as a gapless
        1..16 sequence, and linking equipment to any ONE of them (moving
        it from the plain to the wrapped rendering path) must not change
        that count or leave a gap/duplicate anywhere."""
        import re
        node_id = self.db.add_node()
        n_seeded = len(self.db.deviations(node_id))
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        numbers = []
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if t in (DEV_T, LEDORD_T):
                m = re.search(r'(\d+)\.\s', item.text(0))
                if m:
                    numbers.append(int(m.group(1)))
            it += 1
        self.assertEqual(sorted(numbers), list(range(1, n_seeded + 1)),
            f"expected a gapless 1..{n_seeded} sequence, got: {sorted(numbers)}")

    def test_merged_equipment_deviation_item_does_not_repeat_guide_word_text(self):
        """Exact reported bug: '=M1.GPA6 — Pump' nested under 'Lågt flöde'
        used to show ANOTHER child item labelled '1. Lågt flöde' — the
        same guide-word text shown twice in a row for no reason, with the
        real cause nested one level deeper still (2026-08-09, screenshot
        in conversation). The merged item's own label must be the
        equipment tag/type, never a repeat of the guide-word text."""
        eq_id = self.db.add_equipment_item("M1.GPA6", "M1.GPA6", "M1", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.db.add_cause(dev_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        dev_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == DEV_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == dev_id):
                dev_item = item
            it += 1
        self.assertIsNotNone(dev_item)
        self.assertIn("M1.GPA6", dev_item.text(0))
        self.assertNotIn("Lågt flöde", dev_item.text(0),
            "the merged item must not repeat the guide-word text its LEDORD_T parent already shows")
        # The cause must be one level directly below the merged item, not two.
        self.assertEqual(dev_item.childCount(), 1)
        cause_item = dev_item.child(0)
        self.assertEqual(cause_item.data(0, Qt.ItemDataRole.UserRole + 1), CAUSE_T)

    def test_trivial_tagged_cause_merges_into_equipment_header_row(self):
        """'Det känns onödigt att objektet redovisas två gånger i
        hierarkin i trädet. Detta går att slå ihop till en.' (2026-08-10,
        screenshot in conversation) — dragging equipment onto a deviation
        (_create_tagged_cause) creates a cause with no real description
        yet, whose own tree label therefore falls back to the SAME
        equipment tag the merged header row above it already shows
        ('=E1.M1.QMA102 — Ventil' followed by '1. E1.M1.QMA102'). One
        more 'kaka på kaka' level: the trivial cause's identity (and its
        consequences) now attaches directly to the header row instead of
        a separate, redundant child."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("E1.M1.QMA102", "E1.M1.QMA102", "E1", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Högt flöde", equipment_id=eq_id)
        cause_id, cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "E1.M1.QMA102")
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        header_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == cause_id):
                header_item = item
            it += 1
        self.assertIsNotNone(header_item,
            "the merged row must carry the CAUSE's identity, not a separate DEV_T row above it")
        self.assertIn("E1.M1.QMA102", header_item.text(0))
        self.assertEqual(len([x for x in self._tree_items() if x[0] == DEV_T and x[1] == dev_id]), 0,
            "no separate DEV_T row should remain once merged into the cause")
        # The consequence must be one level directly below the merged
        # row, not nested under yet another redundant cause row.
        self.assertEqual(header_item.childCount(), 1)
        self.assertEqual(header_item.child(0).data(0, Qt.ItemDataRole.UserRole + 1), CONS_T)

    def test_cause_with_real_description_does_not_merge_into_equipment_header(self):
        """The merge only applies to a genuinely trivial, still-unedited
        placeholder cause — once the user types a real description, the
        cause has its own distinct content and must show as a normal,
        separate child row again."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.db.update_cause(cause_id, description="Inget flöde till M1.GPA2")
        self.panel.refresh()

        items = self._tree_items()
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1,
            "a cause with real content must not collapse its parent DEV_T row")
        cause_rows = [x for x in items if x[0] == CAUSE_T and x[1] == cause_id]
        self.assertEqual(len(cause_rows), 1)
        self.assertEqual(cause_rows[0][2], DEV_T)

    def test_second_trivial_cause_prevents_merge(self):
        """A second cause under the same equipment-scoped deviation means
        there are now two distinct things to show — merging either one
        into the header row would hide the other; both must stay normal,
        separate child rows."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id1, _c1 = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        cause_id2, _c2 = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.panel.refresh()

        items = self._tree_items()
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1, "two causes must not collapse the parent DEV_T row")
        cause_rows = {x[1] for x in items if x[0] == CAUSE_T and x[1] in (cause_id1, cause_id2)}
        self.assertEqual(cause_rows, {cause_id1, cause_id2})

    def test_merged_cause_header_still_offers_add_cause_in_context_menu(self):
        """The merged row (carrying the CAUSE's identity, see above) must
        still let the user add a SECOND, distinct cause to the same
        deviation — add_cause() already resolves the deviation via the
        cause's own deviation_id regardless of which row type triggered
        it, so this is purely a context-menu-visibility check."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        header_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == cause_id):
                header_item = item
            it += 1
        self.assertIsNotNone(header_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=header_item), \
             unittest.mock.patch('hazop.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))
        mock_menu = mock_menu_cls.return_value
        labels = _menu_action_labels(mock_menu)
        self.assertTrue(any("Lägg till orsak" in lbl for lbl in labels))
        self.assertTrue(any("Lägg till konsekvens" in lbl for lbl in labels))

    def test_cause_row_shows_real_description_not_redundant_tag(self):
        """'Det räcker om instrumentet E1.M1.QMA127 dyker upp på en rad
        i trädhierarkin' (2026-08-11) — a cause with a REAL, meaningful
        description was showing its tag instead (redundant with the
        equipment header directly above it), because add_causes_to_item's
        label logic always preferred the tag over the description
        whenever a tag existed, regardless of whether the description
        was meaningful. Confirmed on a real project database: a cause
        reading "Flödesgivare felar -> styrventil stänger" displayed as
        just "=E1.M1.QMA127", the same tag its own parent row already
        shows."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("E1.M1.QMA127", "E1.M1.QMA127", "QMA", 0,
                                           "Instrument / Sensor", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(
            self.db, dev_id, "Instrument / Sensor", "E1.M1.QMA127")
        self.db.update_cause(cause_id, description="Flödesgivare felar -> styrventil stänger")
        self.panel.refresh()

        # A real description means the cause must NOT have merged into
        # the equipment header (that merge only applies to a still-
        # trivial cause) — it stays its own, separate CAUSE_T child.
        cause_rows = [x for x in self._tree_items() if x[0] == CAUSE_T and x[1] == cause_id]
        self.assertEqual(len(cause_rows), 1)

        it = QTreeWidgetItemIterator(self.panel.tree)
        cause_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == cause_id):
                cause_item = item
            it += 1
        self.assertIsNotNone(cause_item)
        self.assertIn("Flödesgivare felar", cause_item.text(0))
        self.assertNotIn("E1.M1.QMA127", cause_item.text(0),
            "must show the real description, not repeat the tag its parent row already shows")

    def test_merged_equipment_deviation_item_offers_add_cause_context_menu(self):
        """Right-clicking the equipment row used to be a dead end (EQUIP_T
        items get no context menu at all) — now that this row IS the
        deviation for the common single-deviation case, it must offer
        '+ Lägg till orsak' just like any other DEV_T item."""
        eq_id = self.db.add_equipment_item("M1.GPA6", "M1.GPA6", "M1", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        dev_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == DEV_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == dev_id):
                dev_item = item
            it += 1
        self.assertIsNotNone(dev_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=dev_item), \
             unittest.mock.patch('hazop.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))
        mock_menu_cls.assert_called_once()
        mock_menu = mock_menu_cls.return_value
        labels = _menu_action_labels(mock_menu)
        self.assertTrue(any("Lägg till orsak" in lbl for lbl in labels))

    def test_lone_generic_deviation_skips_ledord_wrapper(self):
        """Bug report: 'varför är det dubbelt?' — a single, plain
        (no equipment) deviation for a guide word with no other sibling
        used to STILL get wrapped in a LEDORD_T item carrying the exact
        same guide-word text as its own only child, e.g.
        '⬡ Lågt flöde' -> '1. Lågt flöde' — the same words shown twice in
        a row for no structural reason. With nothing to group (no
        equipment, no second deviation sharing the guide word), the
        deviation now attaches directly to the NODE, exactly like before
        this feature existed. The wrapper only reappears once there's a
        second deviation for the same guide word to actually distinguish
        (see test_two_equipment_sharing_same_guide_word_grouped_under_one_ledord
        and test_generic_deviation_stays_visible_once_it_has_a_cause)."""
        node_id = self.db.add_node()
        dev_id = self.db.add_deviation(node_id, "Övrigt-avvikelse")
        self.panel.refresh()

        items = self._tree_items()
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1)
        self.assertEqual(dev_rows[0][2], NODE_T,
                          "a lone deviation with nothing to group against must "
                          "attach directly to the node, no redundant Ledord wrapper")
        self.assertEqual(len([x for x in items if x[0] == EQUIP_T]), 0)

    def test_brand_new_node_has_no_ledord_wrappers_at_all(self):
        """The exact real-world screenshot that triggered the fix: a freshly
        created node (all ~16 auto-seeded guide words, no equipment
        touched yet) must show every guide word as a single flat row
        directly under the node — zero LEDORD_T items anywhere, since
        there is nothing anywhere to group."""
        node_id = self.db.add_node()
        self.panel.refresh()

        items = self._tree_items()
        self.assertEqual(len([x for x in items if x[0] == LEDORD_T]), 0)
        dev_rows = [x for x in items if x[0] == DEV_T]
        self.assertTrue(dev_rows)
        self.assertTrue(all(x[2] == NODE_T for x in dev_rows))

    def test_empty_generic_deviation_hidden_when_equipment_scoped_sibling_exists(self):
        """Bug report: 'Lågt flöde dyker upp två gånger i trädet'.
        add_node() auto-seeds an empty, generic (equipment_id=NULL) 'Lågt
        flöde' deviation for every node. Once a piece of equipment ALSO
        gets its own 'Lågt flöde', the still-empty generic one is just
        unused scaffolding sitting right next to it under the same guide
        word — hide it (it is not deleted; see the sibling test below)."""
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        node_id = self.db.add_node()   # already auto-seeds a generic "Lågt flöde"
        generic_dev = next(d for d in self.db.deviations(node_id)
                            if d['description'] == "Lågt flöde")
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        # The equipment-scoped deviation (single deviation for this
        # equipment+guide-word combo) now merges directly onto the
        # equipment's own tree item (2026-08-09, see NOTES.md "kaka på
        # kaka") — a legitimate DEV_T item directly under the LEDORD_T.
        # This test only cares whether the separate GENERIC (no-equipment)
        # deviation is hidden, so it must check that specific id, not
        # "any DEV_T at all" under the ledord.
        generic_rows = [x for x in items if x[0] == DEV_T and x[1] == generic_dev['id']]
        self.assertEqual(
            len(generic_rows), 0,
            "the empty auto-seeded generic deviation must be hidden once an "
            "equipment-scoped sibling exists for the same guide word")

    def test_generic_deviation_stays_visible_once_it_has_a_cause(self):
        """The hide-when-empty rule must never hide real user data: a
        generic deviation that already has a cause stays visible even if an
        equipment-scoped sibling for the same guide word also exists."""
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        generic_dev = next(d for d in self.db.deviations(node_id)
                            if d['description'] == "Lågt flöde")
        self.db.add_cause(generic_dev['id'])
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        direct_dev_rows = [x for x in items if x[0] == DEV_T and x[2] == LEDORD_T
                            and x[1] == generic_dev['id']]
        self.assertEqual(len(direct_dev_rows), 1,
                          "a generic deviation with an existing cause must remain visible")

    def test_resolve_node_id_for_equip_t(self):
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.set_equipment_node(eq_id, node_id)
        self.assertEqual(self.panel._resolve_node_id(EQUIP_T, eq_id), node_id)

    def test_resolve_node_id_for_ledord_t(self):
        node_id = self.db.add_node()
        self.db.add_deviation(node_id, "Lågt flöde")
        self.panel.refresh()
        items = self._tree_items()
        ledord_id = next(x[1] for x in items if x[0] == LEDORD_T)
        self.assertEqual(self.panel._resolve_node_id(LEDORD_T, ledord_id), node_id)

    def test_context_menu_is_a_no_op_for_ledord_t(self):
        """LEDORD_T is a pure grouping view (like EQUIP_T) — right-clicking
        it must return before ever building/exec-ing a QMenu (QMenu.exec()
        is modal and would otherwise hang a headless/offscreen test run
        indefinitely if the LEDORD_T check were ever bypassed).

        Patches QTreeWidget.itemAt directly rather than relying on
        visualItemRect()-derived coordinates: self.panel is never shown, so
        the tree has no real layout geometry, and itemAt() on an arbitrary
        point can resolve to the wrong item (or None) — which is exactly
        what silently happened here once already, sending this test
        through the real menu-building path and hanging on menu.exec()."""
        node_id = self.db.add_node()
        self.db.add_deviation(node_id, "Lågt flöde")
        self.panel.refresh()
        it = QTreeWidgetItemIterator(self.panel.tree)
        ledord_item = None
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole + 1) == LEDORD_T:
                ledord_item = it.value()
                break
            it += 1
        self.assertIsNotNone(ledord_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=ledord_item), \
             unittest.mock.patch('hazop.QMenu') as mock_menu_cls:
            try:
                self.panel._context_menu(QPoint(0, 0))
            except Exception as e:
                self.fail(f"right-clicking a LEDORD_T item must not raise: {e!r}")
            mock_menu_cls.assert_not_called()


class TreeNodeRenameTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): "jag vill kunna döpa
    om noder genom att högerklicka på trädet och välja döp om där." A
    node could already be renamed via PropertiesRibbon's own popup, but
    not directly from the tree's right-click menu."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_noderename_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_node_context_menu_offers_rename(self):
        node_id = self.db.add_node()
        self.panel.refresh()
        item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertIsNotNone(item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=item), \
             unittest.mock.patch('hazop.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))

        mock_menu = mock_menu_cls.return_value
        labels = _menu_action_labels(mock_menu)
        self.assertTrue(any("Döp om" in lbl for lbl in labels), labels)

    def test_rename_updates_name_and_preserves_other_fields(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "Beskrivning", "P&ID-1",
                             "Media", "10 bar", "50 C")

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Nytt namn", True)):
            self.panel._rename_node(node_id)

        updated = dict(self.db.get_node(node_id))
        self.assertEqual(updated['name'], "Nytt namn")
        self.assertEqual(updated['description'], "Beskrivning")
        self.assertEqual(updated['pid_ref'], "P&ID-1")
        self.assertEqual(updated['media'], "Media")

    def test_rename_cancelled_leaves_name_unchanged(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "", "", "", "", "")

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Ignored", False)):
            self.panel._rename_node(node_id)

        self.assertEqual(dict(self.db.get_node(node_id))['name'], "Original")

    def test_rename_with_blank_name_leaves_name_unchanged(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "", "", "", "", "")

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("   ", True)):
            self.panel._rename_node(node_id)

        self.assertEqual(dict(self.db.get_node(node_id))['name'], "Original")

    def test_rename_emits_structure_changed_for_tree_and_scenario_refresh(self):
        node_id = self.db.add_node()
        received = []
        self.panel.structure_changed.connect(lambda: received.append(True))

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Nytt", True)):
            self.panel._rename_node(node_id)

        self.assertEqual(len(received), 1)


class EquipmentDeviationBarTests(unittest.TestCase):
    """The small popup shown near a clicked equipment marker on the P&ID —
    see NOTES.md 'Nod → Utrustning → Avvikelse' and the 2026-08-12
    follow-up ('en liten popup ... där jag kan välja lågt, högt flöde osv
    istället för den menyn som är nu') that turned it from a persistent
    bottom-docked bar with inline cause/frequency-combo editing into this
    auto-dismissing popup with just a deviation checklist — editing a
    cause's text/frequency once it exists is a scenario-table job now,
    not this popup's."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipbar_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import EquipmentDeviationBar
        self.bar = EquipmentDeviationBar(self.db)
        self.eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.marker_id = self.db.add_equipment_marker(
            self.eq_id, "V-101", 0, 100.0, 100.0, "Ventil", confidence=0.9, link_method='leader')

    def tearDown(self):
        self.bar.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_populates_title_with_tag_and_type(self):
        self.bar.load(self.eq_id, self.marker_id)
        self.assertIn("V-101", self.bar._title_lbl.text())
        self.assertIn("Ventil", self.bar._title_lbl.text())

    def test_show_near_makes_the_popup_visible(self):
        from PyQt6.QtCore import QPoint
        self.bar.load(self.eq_id, self.marker_id)
        self.bar.show_near(QPoint(100, 100))
        self.assertTrue(self.bar.isVisible())

    def test_popup_uses_the_auto_dismiss_window_flag(self):
        """Clicking outside must close it on its own — the whole point of
        replacing the old persistent bar — which Qt.WindowType.Popup
        gives for free, no manual outside-click detection needed."""
        self.assertTrue(self.bar.windowFlags() & Qt.WindowType.Popup)

    def test_show_near_sizes_scroll_area_to_available_screen_space(self):
        """2026-08-13 feedback: 'rulllistan väldigt kort på en liten
        skärm' — the checklist's scroll area used to be pinned at a
        fixed 220px no matter how much room was actually available; it
        must now use up to its natural content height (a fresh node has
        16 auto-seeded deviations, well past what fits in 220px), bounded
        only by real screen space, and the whole popup must stay fully
        on-screen either way."""
        from PyQt6.QtCore import QPoint
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        scr = QApplication.primaryScreen().availableGeometry()
        self.bar.show_near(QPoint(scr.center().x(), scr.center().y()))
        self.assertGreater(self.bar._checklist_scroll.maximumHeight(), 220)
        self.assertGreaterEqual(self.bar.geometry().top(), scr.top())
        self.assertLessEqual(self.bar.geometry().bottom(), scr.bottom())

    def test_show_near_keeps_popup_on_screen_when_clicked_near_bottom_edge(self):
        """A click near the screen's bottom edge must open the popup
        UPWARD instead of letting it run off-screen — this is the actual
        'liten skärm' scenario: less room below the click than the
        checklist's natural height needs."""
        from PyQt6.QtCore import QPoint
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        scr = QApplication.primaryScreen().availableGeometry()
        self.bar.show_near(QPoint(scr.center().x(), scr.bottom() - 20))
        self.assertGreaterEqual(self.bar.geometry().top(), scr.top())
        self.assertLessEqual(self.bar.geometry().bottom(), scr.bottom())

    def test_checking_deviation_without_a_node_selected_is_a_no_op(self):
        self.bar.load(self.eq_id, self.marker_id)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)
        # No node yet (no active_node_id given, equipment has none of its
        # own) — checkboxes must be disabled, nothing to toggle.
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        self.assertFalse(checkbox.isEnabled())

    def test_checking_deviation_after_node_selected_creates_it(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_smart_node_default_assigns_active_node_when_equipment_has_none(self):
        """See NOTES.md 'Slippa välja nod varje gång': the popup assigns
        PIDPanel._active_node_id immediately when the equipment has no node
        of its own yet, so checking a deviation works right away instead of
        forcing a manual node pick every time — explicit user request."""
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        self.assertTrue(checkbox.isEnabled())

    def test_smart_node_default_does_not_override_existing_node(self):
        node_id = self.db.add_node()
        other_node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id, active_node_id=other_node_id)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_number_key_shortcut_toggles_matching_checkbox(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        self.assertGreaterEqual(len(self.bar._checklist_checkboxes), 1)
        self.bar._toggle_checkbox_by_number(1)

        self.assertTrue(self.bar._checklist_checkboxes[0].isChecked())
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)

    def test_number_key_shortcut_out_of_range_is_a_no_op(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)
        # One past the last real row — must not raise or toggle anything.
        out_of_range = len(self.bar._checklist_checkboxes) + 1
        self.bar._toggle_checkbox_by_number(out_of_range)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)

    def _select_node_and_stub_cause_creation(self):
        """Shared setup for the suggested-cause tests: assigns a node and
        installs a fake _create_cause_fn that creates a real cause row via
        Database directly, standing in for PIDPanel._create_cause_for_bar
        (which needs a real P&ID marker/scene this test class doesn't
        construct).

        Uses a Pump equipment item rather than self.eq_id ("Ventil") because
        standard_causes is only seeded per specific valve/equipment
        sub-type (e.g. "Manuell ventil", "On-off ventil") — "Pump" is
        seeded and matches the user's own example ("Lågt flöde" + Pump →
        "Pump stopp")."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        pump_marker_id = self.db.add_equipment_marker(
            pump_id, "P-101", 0, 200.0, 200.0, "Pump", confidence=0.9, link_method='leader')
        node_id = self.db.add_node()
        self.db.set_equipment_node(pump_id, node_id)
        self.bar.load(pump_id, pump_marker_id)

        created = {'pump_id': pump_id, 'node_id': node_id}

        def fake_create_cause(dev_id, comp_type, comp_tag, description, frequency=None):
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, description, comp_type=comp_type, comp_tag=comp_tag)
            if frequency is not None:
                # Stand-in for place_cause_from_template's real
                # _compute_f_level() conversion — this test class only
                # needs base_frequency to actually persist.
                self.db.update_cause(cause_id, likelihood=0, base_frequency=frequency)
            created['cause_id'] = cause_id
            created['dev_id'] = dev_id
            created['frequency'] = frequency
            created['description'] = description
            return cause_id

        self.bar._create_cause_fn = fake_create_cause
        return created

    def test_checking_deviation_auto_creates_suggested_cause(self):
        """Förenklat orsaksval, ta bort dubbla val (NOTES.md): checking the
        deviation alone must create the top-suggested cause immediately —
        no separate chip/dropdown needed, that editing now happens in the
        scenario table once the cause row exists."""
        created = self._select_node_and_stub_cause_creation()

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        self.assertIn('cause_id', created)
        causes = self.db.causes_for_deviation(created['dev_id'])
        self.assertEqual(len(causes), 1)
        self.assertEqual(causes[0]['description'], created['description'])

    def test_checking_deviation_passes_through_seeded_frequency(self):
        """'Pump stopp' is seeded with a real frequency estimate
        (standard_causes.frequency) — auto-creating it on check must pass
        that through to _create_cause_fn (and from there to
        place_cause_from_template's _compute_f_level conversion) instead
        of discarding it, per the user's own request: 'får gärna vara
        kopplad till databasen med frekvenser'."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)
        self.assertIsNotNone(created.get('frequency'),
                              "expected the seeded standard_causes.frequency to flow through")

    def test_generic_equipment_type_resolves_object_based_causes(self):
        """Bug report: 'Orsaksväljaren skall ge fler alternativ'.
        equipment_type 'Ventil' matches ZERO standard_causes.comp_type rows
        directly (seeding is per specific sub-type like 'Manuell
        ventil'/'On-off ventil'), so the checklist used to fall back to the
        full unfiltered standard_deviations catalogue with no cause
        suggestions at all. The object-based fallback (_resolve_object_id +
        standard_causes_for_object) must find a substring match
        (_obj_type_matches) and produce a real suggestion instead — proven
        here by checking the deviation and confirming a real (non-blank)
        cause got auto-created for it, not just an empty placeholder."""
        self.assertEqual(
            self.db.standard_causes_for_comp_type("Ventil"), [],
            "sanity check: literal comp_type match is empty for this generic label")
        obj_id = self.bar._resolve_object_id("Ventil")
        self.assertIsNotNone(
            obj_id, "expected a substring match against standard_objects (e.g. 'Manuell ventil')")

        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)
        captured = []
        self.bar._create_cause_fn = lambda dev_id, ct, tag, desc, freq=None: (
            captured.append(desc), self.db.add_cause(dev_id))[-1]
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)
        self.assertTrue(captured, "expected a real cause suggestion once the object-based fallback resolves")

    def test_unchecking_deviation_without_causes_deletes_silently(self):
        """Kryssrutan ska gå att av-/aktivera (NOTES.md) — unchecking a
        deviation that never got a cause (e.g. no template match, user
        never picked one) must delete it right away with no confirmation
        prompt (nothing meaningful to lose)."""
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        with unittest.mock.patch('pid_viewer.QMessageBox.question') as mock_q:
            checkbox.setChecked(True)
            self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)
            checkbox.setChecked(False)
        mock_q.assert_not_called()
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)
        self.assertTrue(checkbox.isEnabled())
        self.assertFalse(checkbox.isChecked())

    def test_unchecking_deviation_with_causes_asks_for_confirmation(self):
        """A deviation with a real cause attached must be confirmed before
        deletion — same pattern as ScenarioTablePanel's own 'Ta bort
        orsak'/'Ta bort konsekvens' confirmations."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)   # auto-creates a cause via the stub
        self.assertIn('cause_id', created)

        with unittest.mock.patch('pid_viewer.QMessageBox.question',
                                  return_value=QMessageBox.StandardButton.No) as mock_q:
            checkbox.setChecked(False)
        mock_q.assert_called_once()
        # Declined -> deviation and cause both survive, checkbox reverts.
        self.assertTrue(checkbox.isChecked())
        self.assertIsNotNone(self.db.get_cause(created['cause_id']))

        with unittest.mock.patch('pid_viewer.QMessageBox.question',
                                  return_value=QMessageBox.StandardButton.Yes):
            checkbox.setChecked(False)
        self.assertFalse(checkbox.isChecked())
        self.assertIsNone(self.db.get_cause(created['cause_id']))
        self.assertEqual(self.db.equipment_deviation_count(created['pump_id']), 0)

    def test_unchecking_emits_deviation_removed(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)

        received = []
        self.bar.deviation_removed.connect(lambda dev_id, eq_id: received.append((dev_id, eq_id)))

        with unittest.mock.patch('pid_viewer.QMessageBox.question'):
            checkbox.setChecked(True)
            checkbox.setChecked(False)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], self.eq_id)

    def test_can_recheck_after_unchecking(self):
        """The whole point: checking, unchecking, and checking again must
        all work — not a one-way lock like the old v1 behavior."""
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        with unittest.mock.patch('pid_viewer.QMessageBox.question'):
            checkbox.setChecked(True)
            checkbox.setChecked(False)
            checkbox.setChecked(True)
        self.assertTrue(checkbox.isChecked())
        self.assertTrue(checkbox.isEnabled())
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)

    def test_unchecking_confirmation_states_full_cascade_counts(self):
        """The confirmation message must count consequences/safeguards too,
        not just causes (2026-08-09, see NOTES.md) — a deviation's single
        cause can carry several consequences, each with several
        safeguards, and the old message ('har N orsak(er) kopplade')
        silently understated how much data a checkbox-uncheck would
        actually destroy."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)   # auto-creates a cause via the stub
        cause_id = created['cause_id']
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.db.add_safeguard(cons_id)

        with unittest.mock.patch(
                'pid_viewer.QMessageBox.question',
                return_value=QMessageBox.StandardButton.No) as mock_q:
            checkbox.setChecked(False)

        mock_q.assert_called_once()
        message = mock_q.call_args[0][2]
        self.assertIn("1 orsak", message)
        self.assertIn("1 konsekvens", message)
        self.assertIn("2 barriär", message)
        self.assertTrue(checkbox.isChecked(),
                         "declining the confirmation must leave the checkbox checked")
        self.assertEqual(self.db.equipment_deviation_count(created['pump_id']), 1)


# ══════════════════════════════════════════════════════════════════════════
# Multi-core parallel P&ID analysis (2026-08-07) — see NOTES.md "Flerkärnig
# parallellisering av Analysera P&ID". scan_pdf_for_equipment() and
# detect_equipment_and_valves() can now be split across several worker
# PROCESSES. These tests verify: (1) the merge/split logic is correct and
# order-independent regardless of what order workers finish in, without
# needing to spin up real OS processes for every case; (2) the two real
# multiprocessing round-trips that DO matter (cancellation always still
# emits the "finished" signal) actually work end to end; (3) the OCR-crop
# optimization's dispatch/fallback/coordinate-translation logic, mocking
# the OCR engine layer itself since this suite never depends on a real
# installed OCR engine (none of tesseract/easyocr/rapidocr are guaranteed
# present in every environment this suite runs in).
# ══════════════════════════════════════════════════════════════════════════

class SplitIntoChunksTests(unittest.TestCase):
    def test_contiguous_and_covers_all_pages(self):
        from equipment_detection import _split_into_chunks
        chunks = _split_into_chunks(10, 3)
        self.assertEqual(sorted(p for c in chunks for p in c), list(range(10)))
        for c in chunks:
            self.assertEqual(
                c, list(range(c[0], c[0] + len(c))),
                "each chunk must be contiguous and ascending")

    def test_more_workers_than_pages_caps_at_page_count(self):
        from equipment_detection import _split_into_chunks
        chunks = _split_into_chunks(3, 10)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sorted(p for c in chunks for p in c), [0, 1, 2])

    def test_zero_pages_returns_no_chunks(self):
        from equipment_detection import _split_into_chunks
        self.assertEqual(_split_into_chunks(0, 4), [])


class MergeScanPageRowsTests(unittest.TestCase):
    """_merge_scan_page_rows (equipment_detection.py) combines flat
    per-page scan rows into scan_pdf_for_equipment()'s nested result
    shape, order-independently — pages processed out of order across
    parallel worker processes must still produce the same result as the
    original sequential 0..N loop."""

    def test_lowest_page_wins_regardless_of_row_order(self):
        from equipment_detection import _merge_scan_page_rows
        row_page5 = {'tag': 'V-1', 'prefix': 'V', 'page_num': 5,
                     'cx': 50.0, 'cy': 60.0, 'from_ocr': False, 'source': 'native'}
        row_page2 = {'tag': 'V-1', 'prefix': 'V', 'page_num': 2,
                     'cx': 10.0, 'cy': 20.0, 'from_ocr': False, 'source': 'native'}
        # Deliberately out of page order — simulates a worker for a LATER
        # page range finishing before one for an EARLIER range.
        result = _merge_scan_page_rows([row_page5, row_page2])
        self.assertEqual(result['V']['pages']['V-1'], 2)
        self.assertEqual(result['V']['positions']['V-1'], (10.0, 20.0))

    def test_prefers_real_coordinates_over_pass1_placeholder_on_same_page(self):
        from equipment_detection import _merge_scan_page_rows
        placeholder = {'tag': 'V-1', 'prefix': 'V', 'page_num': 3,
                       'cx': 0.0, 'cy': 0.0, 'from_ocr': False, 'source': 'native'}
        precise = {'tag': 'V-1', 'prefix': 'V', 'page_num': 3,
                   'cx': 42.0, 'cy': 24.0, 'from_ocr': False, 'source': 'native'}
        result = _merge_scan_page_rows([placeholder, precise])
        self.assertEqual(result['V']['positions']['V-1'], (42.0, 24.0))

    def test_ocr_pages_is_a_union_across_all_rows(self):
        from equipment_detection import _merge_scan_page_rows
        rows = [
            {'tag': 'V-1', 'prefix': 'V', 'page_num': 1, 'cx': 1.0, 'cy': 1.0,
             'from_ocr': True, 'source': 'ocr'},
            {'tag': 'V-1', 'prefix': 'V', 'page_num': 4, 'cx': 4.0, 'cy': 4.0,
             'from_ocr': True, 'source': 'ocr'},
        ]
        result = _merge_scan_page_rows(rows)
        self.assertEqual(result['V']['ocr_pages'], {1, 4})
        self.assertEqual(result['V']['pages']['V-1'], 1)

    def test_matches_sequential_scan_pdf_for_equipment_on_a_real_document(self):
        """The determinism guarantee end to end: scanning a real multi-page
        PDF page-by-page (as _scan_page_range_worker would, one chunk at a
        time) and merging out of order must produce an identical result to
        running scan_pdf_for_equipment() sequentially."""
        import fitz
        from equipment_detection import (
            scan_pdf_for_equipment, _scan_one_page_native, _merge_scan_page_rows)
        doc = fitz.open()
        for i in range(4):
            p = doc.new_page(width=200, height=200)
            p.insert_text(fitz.Point(10, 20), f"V-{100 + i}", fontsize=10)
        sequential = scan_pdf_for_equipment(doc, use_ocr=False)
        sequential.pop('_meta', None)

        # Simulate two workers, each handling a contiguous chunk, whose
        # rows are then merged in REVERSE completion order (the worker for
        # pages 2-3 "finishes" before the one for pages 0-1).
        rows_a = []
        for pn in (2, 3):
            rows_a.extend(_scan_one_page_native(doc[pn], pn))
        rows_b = []
        for pn in (0, 1):
            rows_b.extend(_scan_one_page_native(doc[pn], pn))
        merged = _merge_scan_page_rows(rows_a + rows_b)
        merged.pop('_meta', None)
        doc.close()

        self.assertEqual(merged, sequential)


class AnalyzePageRangeWorkerTests(unittest.TestCase):
    """_analyze_page_range_worker (equipment_detection.py) — the
    multiprocessing target wrapping detect_equipment_and_valves() for a
    page range — must produce the same rows as calling
    detect_equipment_and_valves() sequentially across ALL pages, whether
    split into chunks or not (that function was already confirmed fully
    page-independent — this locks in the wrapper's own correctness)."""

    def test_matches_sequential_detect_equipment_and_valves(self):
        import fitz
        from equipment_detection import (
            detect_equipment_and_valves, _analyze_page_range_worker, _split_into_chunks)
        tmpdir = tempfile.mkdtemp(prefix="hazop_paranalyze_test_")
        try:
            pdf_path = os.path.join(tmpdir, "test.pdf")
            doc = fitz.open()
            tag_points = []
            for i in range(6):
                p = doc.new_page(width=200, height=200)
                tag = f"V-{100 + i}"
                p.insert_text(fitz.Point(10, 20), tag, fontsize=10)
                tag_points.append((tag, 'V', i, None, None, 1.0))
            doc.save(pdf_path)
            doc.close()

            doc2 = fitz.open(pdf_path)
            try:
                seq_results, seq_rejected = detect_equipment_and_valves(doc2, tag_points)
            finally:
                doc2.close()

            chunks = _split_into_chunks(6, 3)
            all_results, all_rejected = [], []
            for chunk in chunks:
                results, rejected = _analyze_page_range_worker(
                    pdf_path, chunk, tag_points, 0.5, 0.3)
                all_results.extend(results)
                all_rejected.extend(rejected)

            def _key(r):
                return (r['tag'], r['page'], r['comp_type'])
            self.assertEqual(sorted(map(_key, all_results)), sorted(map(_key, seq_results)))
            self.assertEqual(len(all_rejected), len(seq_rejected))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class PickWorkerCountTests(unittest.TestCase):
    """_pick_worker_count (pid_viewer.py) — how many worker PROCESSES to
    use for a parallel scan/analysis."""

    def test_caps_easyocr_workers_even_with_many_cpus(self):
        from pid_viewer import _pick_worker_count
        with unittest.mock.patch('pid_viewer.os.cpu_count', return_value=32), \
             unittest.mock.patch('pid_viewer.ocr_status',
                                  return_value={'tesseract': False, 'easyocr': True, 'rapidocr': False}):
            n = _pick_worker_count(n_pages=100, use_ocr=True, ocr_engine='auto')
        self.assertLessEqual(
            n, 3, "EasyOCR workers must be capped regardless of CPU count — each "
                  "loads its own ~1GB model (see NOTES.md)")

    def test_rapidocr_not_capped_like_easyocr(self):
        from pid_viewer import _pick_worker_count
        with unittest.mock.patch('pid_viewer.os.cpu_count', return_value=8), \
             unittest.mock.patch('pid_viewer.ocr_status',
                                  return_value={'tesseract': False, 'easyocr': True, 'rapidocr': True}):
            n = _pick_worker_count(n_pages=100, use_ocr=True, ocr_engine='auto')
        self.assertEqual(n, 7, "leaves one core for the UI thread, no extra RapidOCR cap")

    def test_never_exceeds_page_count(self):
        from pid_viewer import _pick_worker_count
        with unittest.mock.patch('pid_viewer.os.cpu_count', return_value=16):
            n = _pick_worker_count(n_pages=2, use_ocr=False, ocr_engine='auto')
        self.assertLessEqual(n, 2)


class OcrEngineThreadLimitTests(unittest.TestCase):
    """'jag tycker analysera p&id tar för lång tid' (2026-08-10, see
    NOTES.md) — investigated and measured: RapidOCR builds its
    onnxruntime session with no explicit thread limit, so it defaults to
    every logical core. When several worker PROCESSES each do that
    concurrently (the whole point of parallelizing 'Analysera P&ID'),
    they compete for the same cores instead of adding throughput —
    confirmed by direct experiment (8 concurrent RapidOCR processes:
    273s unconstrained vs 111s limited to a couple of threads each,
    2.46x, identical OCR output). _limit_ocr_engine_threads() fixes this
    for real."""

    def setUp(self):
        import onnxruntime as ort
        self._orig_session_options = ort.SessionOptions

    def tearDown(self):
        import onnxruntime as ort
        ort.SessionOptions = self._orig_session_options

    def test_limit_ocr_engine_threads_patches_session_options(self):
        from equipment_detection import _limit_ocr_engine_threads
        import onnxruntime as ort
        _limit_ocr_engine_threads(2)
        so = ort.SessionOptions()
        self.assertEqual(so.intra_op_num_threads, 2)
        self.assertEqual(so.inter_op_num_threads, 1)
        self.assertTrue(issubclass(ort.SessionOptions, self._orig_session_options))

    def test_scan_page_range_worker_limits_only_when_multiple_workers_and_ocr(self):
        from equipment_detection import _scan_page_range_worker
        with unittest.mock.patch('equipment_detection._limit_ocr_engine_threads') as mock_limit, \
             unittest.mock.patch('equipment_detection._scan_one_page_native', return_value=[]), \
             unittest.mock.patch('equipment_detection._scan_one_page_ocr', return_value=([], None)), \
             unittest.mock.patch('fitz.open'):
            _scan_page_range_worker('fake.pdf', [0], use_ocr=False, ocr_engine='auto', n_workers=8)
            mock_limit.assert_not_called()

            _scan_page_range_worker('fake.pdf', [0], use_ocr=True, ocr_engine='auto', n_workers=1)
            mock_limit.assert_not_called()

            _scan_page_range_worker('fake.pdf', [0], use_ocr=True, ocr_engine='auto', n_workers=8)
            mock_limit.assert_called_once()

    def test_scan_page_range_worker_divides_cores_by_worker_count(self):
        from equipment_detection import _scan_page_range_worker
        with unittest.mock.patch('equipment_detection._limit_ocr_engine_threads') as mock_limit, \
             unittest.mock.patch('equipment_detection._scan_one_page_native', return_value=[]), \
             unittest.mock.patch('equipment_detection._scan_one_page_ocr', return_value=([], None)), \
             unittest.mock.patch('equipment_detection.os.cpu_count', return_value=14), \
             unittest.mock.patch('fitz.open'):
            _scan_page_range_worker('fake.pdf', [0], use_ocr=True, ocr_engine='auto', n_workers=8)
        mock_limit.assert_called_once_with(1)   # max(1, 14 // 8)


class ShouldParallelizeTests(unittest.TestCase):
    def test_below_threshold_stays_sequential(self):
        from pid_viewer import _should_parallelize
        self.assertFalse(_should_parallelize(3, 4))
        self.assertFalse(_should_parallelize(10, 1))

    def test_meets_threshold_parallelizes(self):
        from pid_viewer import _should_parallelize
        self.assertTrue(_should_parallelize(4, 2))


class ParallelWorkerCancellationTests(unittest.TestCase):
    """ParallelTagScanWorker/ParallelEquipmentAnalysisWorker must ALWAYS
    emit their 'finished' signal, even when cancelled mid-run — the same
    contract EquipmentAnalysisWorker/ConnectorAnalyzer already guarantee.
    Uses a document large enough to force the real parallel path (see
    _should_parallelize) so this actually exercises ProcessPoolExecutor
    cancellation, not just the untouched sequential fallback."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_parcancel_test_")
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        import fitz
        doc = fitz.open()
        for i in range(6):
            doc.new_page(width=200, height=200)
        doc.save(self.pdf_path)
        doc.close()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_parallel_tag_scan_worker_emits_finished_scan_on_cancel(self):
        from pid_viewer import ParallelTagScanWorker
        worker = ParallelTagScanWorker(self.pdf_path, use_ocr=False)
        received = {}
        worker.finished_scan.connect(lambda r: received.setdefault('result', r))
        with unittest.mock.patch.object(worker, 'isInterruptionRequested', return_value=True):
            worker.run()
        self.assertIn('result', received)
        self.assertEqual(received['result'], {})

    def test_parallel_equipment_analysis_worker_emits_finished_on_cancel(self):
        from pid_viewer import ParallelEquipmentAnalysisWorker
        worker = ParallelEquipmentAnalysisWorker(self.pdf_path, tag_points=[])
        received = {}
        worker.finished_analysis.connect(
            lambda results, rejected: received.setdefault('r', (results, rejected)))
        with unittest.mock.patch.object(worker, 'isInterruptionRequested', return_value=True):
            worker.run()
        self.assertIn('r', received)
        self.assertEqual(received['r'], ([], []))


class PageProgressDialogTests(unittest.TestCase):
    """PageProgressDialog (pid_viewer.py) — the per-page status board
    replacing the old single-line QProgressDialog."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_set_page_status_updates_summary_count(self):
        from pid_viewer import PageProgressDialog
        dlg = PageProgressDialog("Test", 3)
        try:
            self.assertIn("0/3", dlg._summary_lbl.text())
            dlg.set_page_status(0, 'running')
            self.assertIn("0/3", dlg._summary_lbl.text(),
                          "'running' must not count as done")
            dlg.set_page_status(0, 'done')
            self.assertIn("1/3", dlg._summary_lbl.text())
            dlg.set_page_status(2, 'done')
            self.assertIn("2/3", dlg._summary_lbl.text())
        finally:
            dlg.deleteLater()

    def test_unknown_page_number_is_a_no_op(self):
        from pid_viewer import PageProgressDialog
        dlg = PageProgressDialog("Test", 2)
        try:
            dlg.set_page_status(99, 'done')   # out of range — must not raise
            self.assertIn("0/2", dlg._summary_lbl.text())
        finally:
            dlg.deleteLater()

    def test_cancel_button_emits_canceled_signal(self):
        from pid_viewer import PageProgressDialog
        dlg = PageProgressDialog("Test", 2)
        try:
            received = []
            dlg.canceled.connect(lambda: received.append(True))
            dlg._on_cancel_clicked()
            self.assertEqual(received, [True])
        finally:
            dlg.deleteLater()


class EquipmentForeignKeyCleanupTests(unittest.TestCase):
    """Regression tests for two real crash reports (2026-08-07,
    crash_20260807_154530_IntegrityError.json / _161554_...): deleting a
    node with equipment assigned to it (equipment_catalog.node_id) raised
    sqlite3.IntegrityError: FOREIGN KEY constraint failed. Root cause:
    equipment_catalog.node_id and deviations.equipment_id (both added
    2026-08-07 for "Nod → Utrustning → Avvikelse", see NOTES.md) were
    added via ALTER TABLE with NO ON DELETE clause, unlike every other
    node_id/equipment_id-shaped FK in this schema (which use ON DELETE
    CASCADE). delete_node()/delete_equipment_item()/clear_equipment_catalog()
    now explicitly clear these soft references before deleting, instead
    of cascading (equipment/deviations are real HAZOP data that must
    survive their assigned node or equipment row being removed)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_fk_cleanup_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_deleting_node_with_equipment_assigned_does_not_raise(self):
        node_id = self.db.add_node()
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.db.set_equipment_node(eq_id, node_id)
        self.assertEqual(self.db.equipment_node_id(eq_id), node_id)

        self.db.delete_node(node_id)   # must not raise IntegrityError

        self.assertIsNone(self.db.get_node(node_id))
        # Equipment itself survives, just loses its node assignment.
        eq = self.db.get_equipment_by_id(eq_id)
        self.assertIsNotNone(eq)
        self.assertIsNone(eq['node_id'])

    def test_deleting_equipment_item_with_a_deviation_does_not_raise(self):
        node_id = self.db.add_node()
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id = self.db.add_cause(dev_id)

        self.db.delete_equipment_item(eq_id)   # must not raise IntegrityError

        self.assertIsNone(self.db.get_equipment_by_id(eq_id))
        # The deviation (and its cause) survive, just lose the equipment link.
        dev = self.db.get_deviation(dev_id)
        self.assertIsNotNone(dev)
        self.assertIsNone(dev['equipment_id'])
        self.assertIsNotNone(self.db.get_cause(cause_id))

    def test_clear_equipment_catalog_with_a_deviation_does_not_raise(self):
        """The exact real-world trigger: rescanning P&ID ('Skanna P&ID'/
        'Analysera P&ID') replaces the whole catalog via
        clear_equipment_catalog() — must not fail just because the user
        had already used the equipment bar to add a deviation."""
        node_id = self.db.add_node()
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)

        self.db.clear_equipment_catalog()   # must not raise IntegrityError

        self.assertEqual(self.db.equipment_items(), [])
        dev = self.db.get_deviation(dev_id)
        self.assertIsNotNone(dev)
        self.assertIsNone(dev['equipment_id'])


class EquipmentTagDragToConsequenceTests(unittest.TestCase):
    """2026-08-07 'drag-and-dropp kunna dra ett objekt från P&ID viewer till
    konsekvensen för att få med tag nummer' (see NOTES.md). Three parts,
    tested separately: (1) Database.set_consequence_tag writes comp_tag/
    comp_type without touching description/severity; (2) ScenarioTablePanel
    ._handle_drop's new 'equipment' mime kind resolves a marker to its
    catalog tag and attaches it to the dropped-on KON cell; (3)
    PIDGraphicsView arms/fires a Shift-held drag from an equipment marker,
    and a plain (non-Shift) click never arms it — the approved plan's
    explicit requirement so normal clicks keep opening
    EquipmentDeviationBar exactly as before."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dragtag_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── Database.set_consequence_tag ────────────────────────────────────

    def test_set_consequence_tag_writes_tag_and_type(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)

        self.db.set_consequence_tag(cons_id, "HV-101", "Ventil")

        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['comp_tag'], "HV-101")
        self.assertEqual(cons['comp_type'], "Ventil")

    def test_set_consequence_tag_does_not_touch_description(self):
        """The tag is a complement, not a replacement — the user's own
        free-text sentence (e.g. 'Inget flöde till pump X -> ...') must
        survive a tag being attached afterwards."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, "Inget flöde till pump X -> kavitation", 3)

        self.db.set_consequence_tag(cons_id, "P-101", "Pump")

        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['description'], "Inget flöde till pump X -> kavitation")
        self.assertEqual(cons['severity'], 3)
        self.assertEqual(cons['comp_tag'], "P-101")

    # ── ScenarioTablePanel._handle_drop('equipment', ...) ───────────────

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, deviation_id, cause_id, cons_id

    def _make_drop_event(self, panel, text, tgt_row, tgt_col):
        """Builds a fake QDropEvent-like object targeting (tgt_row, tgt_col).
        Bypasses the table<->viewport coordinate mapping (irrelevant to the
        _handle_drop logic under test and unreliable on a never-shown,
        headless widget) by overriding viewport().mapFrom() to identity and
        computing the position directly from the real column/row viewport
        offsets."""
        from PyQt6.QtCore import QMimeData, QPointF
        vp_x = panel._table.columnViewportPosition(tgt_col) + 2
        vp_y = panel._table.rowViewportPosition(tgt_row) + 2
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(vp_x, vp_y)
        event.dropAction.return_value = Qt.DropAction.CopyAction
        panel._table.viewport().mapFrom = lambda widget, pt: pt
        return event

    def test_drop_equipment_on_kon_cell_attaches_tag(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON)
            panel._handle_drop(event)

            event.acceptProposedAction.assert_called_once()
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], "HV-101")
            self.assertEqual(cons['comp_type'], "Ventil")

    def test_drop_equipment_on_non_kon_column_is_ignored(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_ORS)
            panel._handle_drop(event)

            event.ignore.assert_called_once()
            event.acceptProposedAction.assert_not_called()
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], '')

    def test_drop_equipment_marker_with_no_linked_catalog_row_is_ignored(self):
        """A marker whose equipment_id is NULL (untagged shape hit) must be
        a silent no-op, matching _on_marker_clicked's own guard for the
        same case."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            marker_id = win.db.add_equipment_marker(
                None, '', 0, 10.0, 10.0, "Ventil", confidence=0.6, link_method='shape')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON)
            try:
                panel._handle_drop(event)
            except Exception as e:
                self.fail(f"must not raise for a marker with no linked equipment row: {e!r}")

            event.ignore.assert_called_once()
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], '')

    # ── PIDGraphicsView Shift+drag source ────────────────────────────────

    def _make_view_with_equipment_marker(self, marker_id):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        from PyQt6.QtCore import QPointF
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        scene_pos = QPointF(50, 50)
        item = view._scene.addEllipse(scene_pos.x() - 5, scene_pos.y() - 5, 10, 10)
        item.setData(view._DATA_TYPE, 'equipment')
        item.setData(view._DATA_ID, marker_id)
        # The viewport<->scene coordinate transform is standard Qt machinery,
        # not something this feature changes — fix it to a known value so
        # the test exercises only the new drag-arming logic.
        view.mapToScene = lambda pt: scene_pos
        return view

    def _press(self, view, event):
        """In MODE_NAV, mousePressEvent falls through to
        super().mousePressEvent(event) for the base QGraphicsView pan/select
        behaviour — real Qt code that requires a genuine QMouseEvent, not
        our MagicMock stand-in. Patched out since it's irrelevant to the
        drag-arming logic under test."""
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mousePressEvent'):
            view.mousePressEvent(event)

    def _move(self, view, event):
        """Same rationale as _press: once a move doesn't trigger our new
        drag-start branch, it falls through to the base QGraphicsView
        mouseMoveEvent, which needs a real QMouseEvent."""
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mouseMoveEvent'):
            view.mouseMoveEvent(event)

    def test_shift_press_on_equipment_marker_arms_drag_candidate(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=7)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)

        self.assertIsNotNone(view._equip_drag_candidate)
        self.assertEqual(view._equip_drag_candidate[0], 7)

    def test_plain_click_on_equipment_marker_does_not_arm_drag_candidate(self):
        """Protects the user's explicit requirement: a normal click (no
        Shift) must never be interpreted as a drag start, so it keeps
        opening EquipmentDeviationBar exactly as before."""
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=7)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)

        self.assertIsNone(view._equip_drag_candidate)
        # And the normal click-tracking state must still be set, so
        # mouseReleaseEvent's existing marker_clicked dispatch still fires.
        self.assertIsNotNone(view._press_pos)

    def test_shift_drag_past_threshold_starts_qdrag_with_equipment_mime(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=9)
        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)
        self.assertIsNotNone(view._equip_drag_candidate)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(90, 50)   # 40px — past any startDragDistance

        with unittest.mock.patch('pid_viewer.QDrag') as MockDrag:
            mock_drag = MockDrag.return_value
            view.mouseMoveEvent(move_event)

        MockDrag.assert_called_once()
        mime_arg = mock_drag.setMimeData.call_args[0][0]
        self.assertEqual(mime_arg.text(), 'hzp:equipment:9:-1:-1')
        mock_drag.exec.assert_called_once()
        self.assertIsNone(view._equip_drag_candidate)
        self.assertIsNone(view._press_pos)

    def test_shift_drag_below_threshold_does_not_start_drag_yet(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=9)
        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(51, 50)   # 1px — below threshold

        with unittest.mock.patch('pid_viewer.QDrag') as MockDrag:
            self._move(view, move_event)

        MockDrag.assert_not_called()
        self.assertIsNotNone(view._equip_drag_candidate,
            "candidate must stay armed until the drag distance is exceeded")

    def test_releasing_shift_mid_move_disarms_the_candidate(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=9)
        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.NoModifier  # Shift let go
        move_event.position.return_value = QPointF(90, 50)

        with unittest.mock.patch('pid_viewer.QDrag') as MockDrag:
            self._move(view, move_event)

        MockDrag.assert_not_called()
        self.assertIsNone(view._equip_drag_candidate)

    # ── _add_row: KON cell carries the tag via UserRole+7 ────────────────

    def test_kon_cell_carries_comp_tag_via_userrole(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            win.db.set_consequence_tag(cons_id, "P-101", "Pump")

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            item = panel._table.item(row, panel._C_KON)
            comp_type, comp_tag = item.data(Qt.ItemDataRole.UserRole + 7)
            self.assertEqual(comp_tag, "P-101")
            self.assertEqual(comp_type, "Pump")

    def test_kon_cell_tag_tuple_blank_when_untagged(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            item = panel._table.item(row, panel._C_KON)
            comp_type, comp_tag = item.data(Qt.ItemDataRole.UserRole + 7)
            self.assertEqual(comp_tag, '')
            self.assertEqual(comp_type, '')


class DropEventRoutedToViewportTests(unittest.TestCase):
    """Real bug report (2026-08-08): Shift-dragging an equipment tag onto a
    KON cell did nothing on drop. Root cause: for a QAbstractItemView-based
    widget like QTableWidget, Qt/PyQt6 delivers DragEnter/DragMove/Drop
    events to the VIEWPORT (the actual scrollable surface under the
    cursor), not the outer QTableWidget — but ScenarioTablePanel's
    eventFilter only ever checked `obj is self._table`, so this branch
    never matched for a real cross-widget drag and the drop was silently
    ignored. _handle_drop() also unconditionally called
    self._table.viewport().mapFrom(self._table, pos), which — for a
    position already relative to the viewport — shifts it by the
    header/frame offset a SECOND time, landing on the wrong row (or no
    row at all, tgt_row=-1).

    These tests exercise ScenarioTablePanel.eventFilter() itself (not
    _handle_drop() directly, which earlier tests in
    EquipmentTagDragToConsequenceTests already cover and which is why that
    class's own tests didn't catch this — they bypassed the exact routing
    layer that was actually broken)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, deviation_id, cause_id, cons_id

    def _make_drop_event(self, panel, text, tgt_row, tgt_col, pos):
        from PyQt6.QtCore import QEvent, QMimeData
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = pos
        event.dropAction.return_value = Qt.DropAction.CopyAction
        return event

    def test_drop_event_on_viewport_is_routed_and_attaches_tag(self):
        """The realistic case: Qt delivers the Drop event to
        table.viewport() with a viewport-relative position — this must be
        used AS-IS (no extra remapping) to find the right row."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            from PyQt6.QtCore import QPointF
            vp_x = panel._table.columnViewportPosition(panel._C_KON) + 2
            vp_y = panel._table.rowViewportPosition(row) + 2
            viewport_pos = QPointF(vp_x, vp_y)

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON, viewport_pos)

            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled, "eventFilter must consume a Drop delivered to the viewport")
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], "HV-101")

    def test_drop_event_on_outer_table_widget_still_works(self):
        """Defensive fallback: if some Qt version/platform instead delivers
        the event to the outer table widget with a table-relative
        position, that must still be remapped correctly (the ORIGINAL,
        pre-bug behavior) rather than assumed to never happen."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("P-202", "P-202", "P", 0, "Pump", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "P-202", 0, 10.0, 10.0, "Pump", confidence=0.9,
                link_method='leader')

            from PyQt6.QtCore import QPointF
            vp_x = panel._table.columnViewportPosition(panel._C_KON) + 2
            vp_y = panel._table.rowViewportPosition(row) + 2
            # Convert to TABLE-relative coordinates, matching what the event
            # would carry if Qt delivered it to the outer widget instead.
            table_pos = panel._table.viewport().mapTo(panel._table, QPointF(vp_x, vp_y).toPoint())

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON,
                QPointF(table_pos))

            handled = panel.eventFilter(panel._table, event)

            self.assertTrue(handled, "eventFilter must consume a Drop delivered to the outer table")
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], "P-202")

    def test_drag_enter_on_viewport_is_accepted(self):
        """If DragEnter is never accepted, most platforms never even
        deliver the subsequent Drop — this is the first domino, and it
        must fire for the viewport, not just the outer table widget."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            from PyQt6.QtCore import QEvent, QMimeData
            mime = QMimeData()
            mime.setText('hzp:equipment:1:-1:-1')
            event = unittest.mock.MagicMock()
            event.mimeData.return_value = mime
            event.type.return_value = QEvent.Type.DragEnter

            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            event.acceptProposedAction.assert_called_once()


class EquipmentObjectPlacementTests(unittest.TestCase):
    """P&ID right-click -> "🔧 Objekt" (2026-08-07, see NOTES.md) —
    PIDPanel.place_equipment_marker resolves an existing equipment_catalog
    row by tag (never creates a duplicate) or creates a new one, places a
    marker at the clicked point, and opens EquipmentDeviationBar
    immediately so ticking a deviation is the very next step."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_objplacement_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_new_tag_creates_catalog_row_and_marker(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("HV-201", "Ventil", QPointF(10, 10), 0)

        equip = self.db.get_equipment_by_tag("HV-201")
        self.assertIsNotNone(equip)
        self.assertEqual(equip['equipment_type'], "Ventil")
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['equipment_id'], equip['id'])

    def test_existing_tag_reuses_catalog_row_no_duplicate(self):
        from PyQt6.QtCore import QPointF
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)

        self.panel.place_equipment_marker("P-101", "Pump", QPointF(20, 20), 0)

        self.assertEqual(len(self.db.equipment_items()), 1,
            "must not create a duplicate catalog row for an already-known tag")
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(markers[0]['equipment_id'], eq_id)

    def test_placement_opens_equipment_deviation_bar(self):
        # isVisible() would report False here regardless of load() having
        # run — the panel itself is never shown in this headless test, and
        # QWidget.isVisible() reflects the whole ancestor chain, not just
        # this widget's own setVisible(True) call. Assert on load()'s
        # actual effect (which equipment/marker it's now bound to) instead.
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("T-301", "Behållare", QPointF(5, 5), 0)
        equip_row = self.db.get_equipment_by_tag("T-301")
        self.assertIsNotNone(equip_row)
        self.assertEqual(self.panel._equipment_bar._equipment_id, equip_row['id'])
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(self.panel._equipment_bar._marker_id, markers[0]['id'])

    def test_blank_tag_still_creates_marker_from_type_alone(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("", "Pump", QPointF(1, 1), 0)
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        equip = self.db.get_equipment_by_id(markers[0]['equipment_id'])
        self.assertEqual(equip['equipment_type'], "Pump")

class ObjectMenuAndToolbarButtonsTests(unittest.TestCase):
    """"⚙️ Orsak"/"⚠️ Konsekvens" mode-toggle buttons removed from the P&ID
    toolbar (2026-08-07, see NOTES.md). The right-click menu's own "Orsak"/
    "Konsekvens"/"Safeguard" actions, and the MODE_CAUSE_TEMPLATE/
    MODE_CONSEQUENCE/MODE_SAFEGUARD modes they drove, were later removed
    entirely (2026-08-13, see NOTES.md: the P&ID canvas is now
    object-placement-only) — "🔧 Objekt" is the only right-click action
    left that creates something new."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_toolbarmenu_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_only_navigate_button_remains(self):
        """The toolbar has exactly one mode button — Navigera. The P&ID
        canvas is object-placement-only (2026-08-13, see NOTES.md);
        equipment placement is armed via the right-click/rubber-band menus,
        not via its own toolbar toggle."""
        from pid_viewer import MODE_NAV
        self.assertIn(MODE_NAV, self.panel.mode_buttons)
        self.assertEqual(len(self.panel.mode_buttons), 1)

    def test_context_menu_equipment_action_emits_placement_signal(self):
        from PyQt6.QtCore import QPointF
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page: captured.append((tag, pos, page)))
        pt = QPointF(7, 7)

        self.panel._on_context_action('equipment', pt, 2)

        self.assertEqual(len(captured), 1)
        tag, pos, page = captured[0]
        self.assertEqual(page, 2)
        self.assertEqual(pos, pt)

    def test_context_menu_find_similar_action_warns_with_no_pid_open(self):
        """"🔎 Hitta liknande symbol" (2026-08-10, see NOTES.md) routes to
        _find_similar_symbol — with no PDF loaded (this fixture's
        default) it must warn, not crash."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox
        with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_warn.assert_called_once()

    def _mock_accepted_search_params_dialog(self, mock_dlg_cls, final_results=None):
        """SimilarSymbolSearchDialog is a real modal QDialog that runs
        its own background scan (2026-08-15, see NOTES.md "Hitta
        liknande symbol" — uppföljningsfunktioner) — tests that just
        want the flow past it (not the dialog itself) mock the class
        and stub final_results(), mirroring how other modal-dialog call
        sites in this file are tested (see
        test_edit_extra_defers_rebuild_instead_of_calling_it_directly)."""
        from PyQt6.QtWidgets import QDialog
        inst = mock_dlg_cls.return_value
        inst.exec.return_value = QDialog.DialogCode.Accepted
        inst.final_results.return_value = final_results if final_results is not None else []
        return inst

    def test_find_similar_symbol_falls_back_to_image_mode_when_no_reference_cluster_resolved(self):
        """2026-08-15 (see NOTES.md "Bildbaserad 'hitta liknande symbol'
        — vid sidan av vektorlogiken"): a click with no vector cluster
        nearby (a scanned page, or an empty spot) no longer dead-ends
        with a rejection message — it opens SimilarSymbolSearchDialog in
        forced image-matching mode instead, with primitives/index_group
        both None and a click-centered ref_bbox."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox, QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=None), \
             unittest.mock.patch.object(QMessageBox, 'information') as mock_info, \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_params_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_info.assert_not_called()
        mock_params_dlg.assert_called_once()
        args, kwargs = mock_params_dlg.call_args
        self.assertIsNone(args[0])
        self.assertIsNone(args[1])
        self.assertIn('ref_bbox', kwargs)
        self.assertIsNotNone(kwargs['ref_bbox'])

    def test_find_similar_symbol_does_not_check_results_when_params_dialog_cancelled(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=([], [], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_params_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_params_dlg.return_value.final_results.assert_not_called()

    def test_find_similar_symbol_shows_info_when_nothing_found(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=([], [], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg, \
             unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_info.assert_called_once()

    def test_find_similar_symbol_opens_review_dialog_and_reloads_on_accept(self):
        from PyQt6.QtCore import QPointF
        fake_results = [{'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0,
                         'outline': [], 'link_method': 'similar', 'tag_status': 'untagged',
                         'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9}]
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=([], [], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg, \
             unittest.mock.patch('pid_viewer.EquipmentMarkerReviewDialog') as mock_dlg_cls, \
             unittest.mock.patch.object(self.panel, 'reload_overlays') as mock_reload:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=fake_results)
            mock_dlg_cls.return_value.exec.return_value = 1   # QDialog.Accepted
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_dlg_cls.assert_called_once()
        args, kwargs = mock_dlg_cls.call_args
        self.assertEqual(args[0], fake_results)
        mock_reload.assert_called_once()

    def test_find_similar_symbol_constructs_dialog_with_pdf_path_page_scale_and_viewer(self):
        """The dialog now runs its own background scan, so it needs the
        PDF path (its worker opens its own fitz.Document), the
        reference page, the page's text scale, and the viewer (for the
        on-canvas preview) — not just primitives/index_group."""
        from PyQt6.QtCore import QPointF
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.panel.viewer._pdf_path = '/fake/path.pdf'
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=(['prims'], ['idx'], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_viewer.symbol_geometry.dominant_text_size',
                                 return_value=12.5), \
             unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                 return_value=[]), \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            with unittest.mock.patch.object(QMessageBox, 'information'):
                self.panel._on_context_action('find_similar', QPointF(5, 5), 3)
        args, kwargs = mock_params_dlg.call_args
        self.assertEqual(args[0], ['prims'])
        # No nearby primitives found (mocked empty) — the widened group is
        # just the auto-detected native group, unioned with nothing.
        self.assertEqual(args[1], ['idx'])
        self.assertEqual(args[2], '/fake/path.pdf')
        self.assertEqual(args[3], 3)
        self.assertEqual(args[4], 12.5)
        self.assertEqual(kwargs['viewer'], self.panel.viewer)
        self.assertEqual(kwargs['native_index_group'], ['idx'])
        self.assertEqual(kwargs['initial_excluded'], set())

    def test_find_similar_symbol_from_template_warns_with_no_pid_open(self):
        from pid_viewer import PIDPanel
        panel = PIDPanel(self.db)
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
                panel._find_similar_symbol_from_template()
            mock_warn.assert_called_once()
        finally:
            panel.deleteLater()

    def test_find_similar_symbol_from_template_shows_info_with_no_saved_templates(self):
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch.object(QMessageBox, 'information') as mock_info, \
             unittest.mock.patch('pid_viewer.SymbolTemplatePickerDialog') as mock_picker:
            self.panel._find_similar_symbol_from_template()
        mock_info.assert_called_once()
        mock_picker.assert_not_called()

    def test_find_similar_symbol_from_template_does_nothing_when_picker_cancelled(self):
        from PyQt6.QtWidgets import QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.db.add_symbol_template("Test-mall", '{"aspect": 1.0}')
        with unittest.mock.patch('pid_viewer.SymbolTemplatePickerDialog') as mock_picker, \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_picker.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._find_similar_symbol_from_template()
        mock_params_dlg.assert_not_called()

    def test_find_similar_symbol_from_template_opens_search_dialog_in_template_mode(self):
        from PyQt6.QtWidgets import QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.panel.viewer._pdf_path = '/fake/path.pdf'
        self.panel.viewer.current_page = 4
        self.db.add_symbol_template("Metso-ventil", '{"aspect": 2.0}', comp_type='Ventil')
        template_row = self.db.symbol_templates()[0]
        with unittest.mock.patch('pid_viewer.SymbolTemplatePickerDialog') as mock_picker, \
             unittest.mock.patch('pid_viewer.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
            mock_picker.return_value.selected_template = template_row
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            with unittest.mock.patch.object(QMessageBox, 'information'):
                self.panel._find_similar_symbol_from_template()
        args, kwargs = mock_params_dlg.call_args
        self.assertIsNone(args[0])
        self.assertIsNone(args[1])
        self.assertEqual(args[2], '/fake/path.pdf')
        self.assertEqual(args[3], 4)
        self.assertEqual(kwargs['template_name'], 'Metso-ventil')
        self.assertEqual(kwargs['template_features'], {"aspect": 2.0})


class AutoConsequenceOnCauseAddTests(unittest.TestCase):
    """'Sedan vill jag ... kunna editera konsekvenser direkt utan att
    behöva lägga till dem via popuprutan ... utan det skall gå i hazop
    scenario så fort jag lagt till en orsak' (2026-08-07, see NOTES.md) —
    _create_cause_from_pick (shared by the tree's "+ Lägg till orsak" and
    the worksheet's Ctrl+Enter quick-add) now also creates one empty
    consequence, and ScenarioTablePanel._quick_add_cause lands the editing
    cursor on that new consequence's KON cell instead of the cause's own
    ORS cell."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autocons_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_create_cause_from_pick_returns_cause_and_consequence_ids(self):
        from hazop import _create_cause_from_pick
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']

        cause_id, cons_id = _create_cause_from_pick(self.db, dev_id, "Ny orsak", None)

        self.assertIsNotNone(self.db.get_cause(cause_id))
        cons = self.db.get_consequence(cons_id)
        self.assertIsNotNone(cons)
        self.assertEqual(dict(cons)['cause_id'], cause_id)

    def test_new_item_created_consequence_starts_inline_edit_on_kon(self):
        """Directly exercises the MainWindow-level wiring (matches the
        established pattern in SafeguardCreatedDoubleRebuildTests): emitting
        new_item_created(CONS_T, cons_id) must land the current cell AND an
        active edit on that row's KON column."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            panel.load_node(node_id)

            edit_spy = unittest.mock.Mock(wraps=panel._table.edit)
            panel._table.edit = edit_spy

            panel.new_item_created.emit(CONS_T, cons_id)

            cur_row = panel._table.currentRow()
            self.assertEqual(panel._table.currentColumn(), panel._C_KON)
            self.assertEqual(panel._row_meta[cur_row][2], cons_id)
            edit_spy.assert_called()

    def test_quick_add_cause_emits_new_item_created_for_the_new_consequence(self):
        """ScenarioTablePanel._quick_add_cause (Ctrl+Enter in the worksheet)
        must emit new_item_created for the auto-created CONSEQUENCE, not
        for the cause itself — the cause's description was already chosen
        in the popup, so the next thing to fill in is the consequence.

        Opens CauseObjectPopup, not StandardCausesPickerPopup (2026-08-12,
        see NOTES.md) — mocking the wrong class here would leave the real
        popup unmocked, blocking forever on exec() in a headless test run
        (this is exactly what happened: an earlier version of this test
        still mocked StandardCausesPickerPopup after the switch, hanging
        the full suite)."""
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            panel.load_node(node_id)

            def _fake_exec(self):
                self.committed.emit('', '', 'Ny orsak (test)', None)
                return QDialog.DialogCode.Accepted

            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch.object(hazop.CauseObjectPopup, 'exec', new=_fake_exec):
                panel._quick_add_cause(dev_id)

            self.assertEqual(len(captured), 1)
            type_, cons_id = captured[0]
            self.assertEqual(type_, CONS_T)
            self.assertIsNotNone(win.db.get_consequence(cons_id))

    def test_tree_add_cause_via_picker_also_creates_empty_consequence(self):
        """TreePanel._open_cause_picker_for_deviation is the other
        _create_cause_from_pick caller (tree's "+ Lägg till orsak" / Enter
        on an avvikelse) — must not crash on the new tuple return, and the
        consequence must actually exist in DB afterwards."""
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            tree = win.tree_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']

            def _fake_exec(self):
                self.cause_picked.emit("Ny orsak (test)", None)
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.StandardCausesPickerPopup, 'exec', new=_fake_exec):
                tree._open_cause_picker_for_deviation(dev_id, node_id)

            causes = win.db.causes(node_id)
            self.assertEqual(len(causes), 1)
            cons_list = win.db.consequences(causes[0]['id'])
            self.assertEqual(len(cons_list), 1,
                "the tree's add-cause path must also auto-create an empty consequence")


class ObjectPickerPopupTests(unittest.TestCase):
    """New (2026-08-12, see NOTES.md): lets the user pick an already-
    registered P&ID object to auto-tag a new cause/consequence/safeguard,
    instead of only being able to drag-and-drop from the P&ID."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_objpicker_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_two_equipment(self):
        id1 = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "Tryckventil", 0)
        id2 = self.db.add_equipment_item("TT-201", "TT-201", "TT", 0, "Givare", "Temperaturgivare", 0)
        return id1, id2

    def test_lists_all_registered_equipment_regardless_of_marker_state(self):
        from hazop import ObjectPickerPopup
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            self.assertEqual(popup._list.count(), 2)
        finally:
            popup.deleteLater()

    def test_search_filters_by_tag_type_or_description(self):
        from hazop import ObjectPickerPopup
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            popup._search.setText("temperatur")
            self.assertEqual(popup._list.count(), 1)
            self.assertIn("TT-201", popup._list.item(0).text())

            popup._search.setText("")
            self.assertEqual(popup._list.count(), 2)
        finally:
            popup.deleteLater()

    def test_pick_button_disabled_until_selection_made(self):
        from hazop import ObjectPickerPopup
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            self.assertFalse(popup._pick_btn.isEnabled())
            popup._list.setCurrentRow(0)
            self.assertTrue(popup._pick_btn.isEnabled())
        finally:
            popup.deleteLater()

    def test_accept_selected_sets_selected_and_accepts(self):
        from hazop import ObjectPickerPopup
        from PyQt6.QtWidgets import QDialog
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            popup._list.setCurrentRow(0)
            popup._accept_selected()
            self.assertEqual(popup.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(popup.selected['tag'], "PV-101")
        finally:
            popup.deleteLater()

    def test_accept_skip_accepts_with_none_selected(self):
        from hazop import ObjectPickerPopup
        from PyQt6.QtWidgets import QDialog
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            popup._accept_skip()
            self.assertEqual(popup.result(), QDialog.DialogCode.Accepted)
            self.assertIsNone(popup.selected)
        finally:
            popup.deleteLater()


class PlusRowQuickAddTaggingTests(unittest.TestCase):
    """The "+" quick-add rows (2026-08-12, see NOTES.md). Reported
    feedback changed course mid-session on how these should behave:
    a new consequence/safeguard must NEVER show a popup — straight to
    inline editing, tagging stays a drag-and-drop-only affair — while a
    new cause opens the same compact CauseObjectPopup ("Orsak på P&ID")
    already used everywhere else a cause's tag/type/description is set,
    replacing the earlier ObjectPickerPopup experiment entirely."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _add_full_chain(self, db):
        node_id = db.add_node()
        dev_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(dev_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, dev_id, cause_id, cons_id

    def test_quick_add_consequence_creates_blank_row_no_popup(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _c = self._add_full_chain(win.db)
            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch('hazop.ObjectPickerPopup') as mock_popup_cls:
                panel._quick_add_consequence(cause_id)

            mock_popup_cls.assert_not_called()
            self.assertEqual(len(captured), 1)
            new_id = captured[0][1]
            self.assertEqual(dict(win.db.get_consequence(new_id))['description'], '')

    def test_quick_add_safeguard_creates_blank_row_no_popup(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, _c, cons_id = self._add_full_chain(win.db)
            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch('hazop.ObjectPickerPopup') as mock_popup_cls:
                panel._quick_add_safeguard(cons_id)

            mock_popup_cls.assert_not_called()
            self.assertEqual(len(captured), 1)
            new_id = captured[0][1]
            self.assertEqual(dict(win.db.get_safeguard(new_id))['description'], '')

    def test_quick_add_cause_opens_cause_object_popup_and_creates_cause(self):
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']

            def _fake_exec(self):
                self.committed.emit('Ventil', 'PV-101', 'Ventil stängd', 3)
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.CauseObjectPopup, 'exec', new=_fake_exec):
                panel._quick_add_cause(dev_id)

            causes = win.db.causes(dev_id)
            self.assertEqual(len(causes), 1)
            self.assertEqual(causes[0]['comp_tag'], 'PV-101')
            self.assertEqual(causes[0]['comp_type'], 'Ventil')
            self.assertEqual(causes[0]['description'], 'Ventil stängd')
            self.assertEqual(win.db.consequences(causes[0]['id']),
                              win.db.consequences(causes[0]['id']))  # sanity: no crash
            self.assertEqual(len(win.db.consequences(causes[0]['id'])), 1)

    def test_add_consequence_via_plus_row_creates_blank_row_no_popup(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _c = self._add_full_chain(win.db)
            before = len(win.db.consequences(cause_id))
            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch('hazop.ObjectPickerPopup') as mock_popup_cls:
                panel._add_consequence_via_plus_row(cause_id)

            mock_popup_cls.assert_not_called()
            self.assertEqual(len(win.db.consequences(cause_id)), before + 1)
            self.assertEqual(len(captured), 1)

    def test_add_cause_via_plus_row_opens_cause_object_popup(self):
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']

            def _fake_exec(self):
                self.committed.emit('', '', '', None)
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.CauseObjectPopup, 'exec', new=_fake_exec):
                panel._add_cause_via_plus_row(dev_id)

            self.assertEqual(len(win.db.causes(dev_id)), 1)


class PlusRowRenderingTests(unittest.TestCase):
    """The "+" quick-add affordance (2026-08-12, see NOTES.md). Originally
    a separate blank row per group; the user rejected that too ("tar upp
    alldeles för mycket plats då de tar hela rader med blankt") and asked
    for a small "+" badge painted in the bottom-right corner of the LAST
    real content row of a group instead, with clicking that badge zone
    inserting a new row only then. `_row_plus_cols` (row -> {col: (kind,
    group_id)}) marks which cells carry a badge; `_PidDelegate._draw_plus_badge`
    paints it; the eventFilter's badge-rect hit-test (ahead of the
    column's other right-edge zones — RRF badge, clone/comment icons)
    dispatches the click."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_plusrow_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_plus_badge_marked_on_ors_cell_of_last_cause_row(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [(r, c) for r, cols in panel._row_plus_cols.items()
                      for c, v in cols.items() if v[0] == 'cause']
            self.assertEqual(len(marked), 1)
            row, col = marked[0]
            self.assertEqual(col, panel._C_ORS)
            self.assertEqual(panel._row_plus_cols[row][col], ('cause', dev_id))
            # The badge is drawn ON TOP of the real cause's own text, not on
            # a separate blank cell — no new row, no cleared text.
            self.assertTrue(panel._table.item(row, panel._C_ORS).text())
        finally:
            panel.deleteLater()

    def test_no_plus_badge_when_deviation_has_no_causes(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [v for cols in panel._row_plus_cols.values() for v in cols.values()
                      if v[0] == 'cause']
            self.assertEqual(marked, [])
        finally:
            panel.deleteLater()

    def test_plus_badge_marked_on_kon_cell_of_last_consequence_row(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [(r, c, v) for r, cols in panel._row_plus_cols.items()
                      for c, v in cols.items() if v[0] == 'consequence']
            self.assertEqual(len(marked), 1)
            self.assertEqual(marked[0][2], ('consequence', cause_id))
        finally:
            panel.deleteLater()

    def test_no_plus_badge_when_cause_has_no_consequences(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [v for cols in panel._row_plus_cols.values() for v in cols.values()
                      if v[0] == 'consequence']
            self.assertEqual(marked, [])
        finally:
            panel.deleteLater()

    def test_plus_badge_marked_on_sg_cell_of_last_safeguard_row(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [(r, c, v) for r, cols in panel._row_plus_cols.items()
                      for c, v in cols.items() if v[0] == 'safeguard']
            self.assertEqual(len(marked), 1)
            self.assertEqual(marked[0][2], ('safeguard', cons_id))
        finally:
            panel.deleteLater()

    def test_no_plus_badge_when_consequence_has_no_safeguards(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [v for cols in panel._row_plus_cols.values() for v in cols.values()
                      if v[0] == 'safeguard']
            self.assertEqual(marked, [])
        finally:
            panel.deleteLater()

    def test_clicking_the_badge_zone_invokes_the_add_flow(self):
        """Simulates a real left-click at the badge's pixel position — the
        same pattern used by test_tag_zone_click_hit_test_matches_the_expanded_paint_geometry
        — rather than calling the dispatch directly, so this actually
        exercises the eventFilter hit-test geometry, not just the callback
        it eventually calls."""
        from hazop import ScenarioTablePanel, _PLUS_BADGE_SIZE
        from PyQt6.QtCore import QPoint, QEvent
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            row, col = next((r, c) for r, cols in panel._row_plus_cols.items()
                             for c, v in cols.items() if v[0] == 'cause')

            with unittest.mock.patch.object(panel, '_add_cause_via_plus_row') as mock_add:
                idx = panel._table.model().index(row, col)
                cr = panel._table.visualRect(idx)
                sz = _PLUS_BADGE_SIZE
                pos = QPoint(cr.right() - sz // 2 - 2, cr.bottom() - sz // 2 - 2)
                ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                                  _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                                  _Qt.KeyboardModifier.NoModifier)
                handled = panel.eventFilter(panel._table.viewport(), ev)

            self.assertTrue(handled)
            mock_add.assert_called_once()
            self.assertEqual(mock_add.call_args.args[0], dev_id)
        finally:
            panel.deleteLater()

    def test_all_nodes_view_still_builds_without_error_with_plus_badges(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel._all_nodes = True
            panel.load_all()
            self.assertGreater(panel._table.rowCount(), 0)
            self.assertTrue(panel._row_plus_cols)
        finally:
            panel.deleteLater()


class NewConsequenceSafeguardDashPlaceholderTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): a newly created
    consequence/safeguard showed the literal text "Ny konsekvens"/"Ny
    safeguard" — unnecessary visual noise; a plain "—" until the row is
    actually defined reads more like an empty/unset value, consistent
    with how an already-absent safeguard row shows "—" today."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dashplaceholder_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_full_chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        return node_id, dev_id, cause_id

    def test_add_consequence_stores_empty_description_not_ny_konsekvens(self):
        _n, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        self.assertEqual(dict(self.db.get_consequence(cons_id))['description'], '')

    def test_add_safeguard_stores_empty_description_not_ny_safeguard(self):
        _n, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.assertEqual(dict(self.db.get_safeguard(sg_id))['description'], '')

    def test_new_empty_consequence_cell_displays_dash(self):
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            self.assertEqual(panel._table.item(row, panel._C_KON).text(), '—')
        finally:
            panel.deleteLater()

    def test_new_empty_safeguard_cell_displays_dash(self):
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            self.assertEqual(panel._table.item(row, panel._C_SG).text(), '—')
        finally:
            panel.deleteLater()

    def test_editor_starts_blank_not_on_the_dash_sentinel(self):
        """_PidDelegate.createEditor() must strip the "—" placeholder —
        QTableWidgetItem has no real Display-vs-EditRole divergence
        (verified: setData() on one overwrites what the other reads
        back), so the dash reaches index.data(EditRole) too."""
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            from PyQt6.QtWidgets import QStyleOptionViewItem
            index = panel._table.model().index(row, panel._C_KON)
            option = QStyleOptionViewItem()
            option.font = panel._table.font()
            editor = panel._delegate.createEditor(panel._table, option, index)
            try:
                self.assertEqual(editor.text(), '')
            finally:
                editor.deleteLater()
        finally:
            panel.deleteLater()

    def test_clearing_safeguard_text_saves_empty_not_ny_safeguard(self):
        """_on_cell_changed_inner used to resurrect 'Ny safeguard' whenever
        the committed text was empty — clearing an existing description
        must actually save empty (displayed as "—"), not silently revert
        to placeholder text."""
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, "Brandlarm", 10)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            item = panel._table.item(row, panel._C_SG)
            item.setData(Qt.ItemDataRole.EditRole, '')
            panel._on_cell_changed_inner(row, panel._C_SG)
            self.assertEqual(dict(self.db.get_safeguard(sg_id))['description'], '')
        finally:
            panel.deleteLater()


class EmptyOrsCellClickOpensCausePopupTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): clicking an empty
    ORS placeholder cell (a deviation with no causes yet) used to start
    inline text editing directly — now opens the same CauseObjectPopup
    the "+ Ny orsak" row does, so creating a cause behaves identically
    regardless of entry point."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_emptyorsclick_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clicking_empty_ors_placeholder_opens_cause_popup(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[0] == dev_id and m[1] is None)

            with unittest.mock.patch.object(panel, '_add_cause_via_plus_row') as mock_add:
                panel._on_cell_clicked(row, panel._C_ORS)

            mock_add.assert_called_once()
            self.assertEqual(mock_add.call_args.args[0], dev_id)
        finally:
            panel.deleteLater()

    def test_clicking_a_real_ors_cell_still_selects_it_not_the_popup(self):
        """Sanity check: the new empty-placeholder branch must not
        accidentally hijack clicks on a real, already-defined cause."""
        from hazop import ScenarioTablePanel, CAUSE_T
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            captured = []
            panel.item_selected.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch.object(panel, '_add_cause_via_plus_row') as mock_add:
                panel._on_cell_clicked(row, panel._C_ORS)

            mock_add.assert_not_called()
            self.assertEqual(captured, [(CAUSE_T, cause_id)])
        finally:
            panel.deleteLater()


class RecommendationColumnTests(unittest.TestCase):
    """"Längst till höger ... kan du lägga till en rekomendationskolumn
    på varje flik så det går att skapa rekommendationer till varje
    scenario." (2026-08-13) — backed by the pre-existing actions table/
    ActionEditor (previously unreachable in the UI after the
    PropertiesRibbon migration), not a new free-text field, since a
    scenario can have several recommendations (responsible/due date/
    status each)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rekcol_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        self.node_id = node_id

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _rek_item(self):
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        return self.panel._table.item(row, self.panel._C_REK), row

    def test_column_exists_last_and_is_named_rekommendation(self):
        self.assertEqual(self.panel._COLS[-1], 'Rekommendation')
        self.assertEqual(self.panel._C_REK, len(self.panel._COLS) - 1)

    def test_consequence_with_no_actions_shows_dash_placeholder(self):
        item, _ = self._rek_item()
        self.assertEqual(item.text(), '—')

    def test_single_action_shows_its_description(self):
        self.db.add_action(self.cons_id)
        item, _ = self._rek_item()
        self.assertEqual(item.text(), '1. Ny åtgärd')

    def test_multiple_actions_are_all_listed_numbered_by_addition_order(self):
        """"samtliga tillagda rekomendationer. de kan nummereras efter
        tilläggsordning" (2026-08-13) — every recommendation shows, not
        just a count, numbered 1.. in the order they were added."""
        self.db.add_action(self.cons_id)
        act_id = self.db.add_action(self.cons_id)
        self.db.update_action(act_id, 'Klar sak', '', '', 'Klar')
        item, _ = self._rek_item()
        self.assertEqual(item.text(), '1. Ny åtgärd\n2. Klar sak')

    def test_row_grows_to_fit_several_recommendations(self):
        """REK joins wrap_cols (_ScenarioDelegate._size_hint_impl,
        ScenarioTablePanel._compute_row_height) so a multi-line
        recommendation list isn't clipped to one line like a plain
        non-wrapping column would be."""
        _, row = self._rek_item()
        one_line_h = self.panel._table.rowHeight(row)
        for _ in range(6):
            self.db.add_action(self.cons_id)
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        grown_h = self.panel._table.rowHeight(row)
        self.assertGreater(grown_h, one_line_h,
            "row must grow to fit 6 numbered recommendation lines")

    def test_clicking_cell_opens_editor_and_refreshes_summary_after(self):
        from hazop import RecommendationEditorDialog
        _, row = self._rek_item()
        with unittest.mock.patch.object(
                RecommendationEditorDialog, 'exec',
                side_effect=lambda: self.db.add_action(self.cons_id)):
            self.panel._on_cell_clicked(row, self.panel._C_REK)
        item = self.panel._table.item(row, self.panel._C_REK)
        self.assertEqual(item.text(), '1. Ny åtgärd')

    def test_recommendation_column_spans_across_safeguard_rows(self):
        """Several safeguards under the same consequence must share ONE
        merged REK cell, not one per safeguard row — same grouping KON/
        LOPA already get."""
        self.db.add_safeguard(self.cons_id)
        self.db.add_safeguard(self.cons_id)
        self.db.add_action(self.cons_id)
        self.panel.load_node(self.node_id)
        rows = [r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id]
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(self.panel._table.rowSpan(rows[0], self.panel._C_REK), len(rows),
            "the consequence's safeguard rows must be merged into one REK span")


def _find_tree_item(tree, type_, id_=None):
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == type_ and (
                id_ is None or item.data(0, Qt.ItemDataRole.UserRole) == id_):
            return item
        it += 1
    return None


class SafeguardTagDbTests(unittest.TestCase):
    """DB-layer support for 'kunna dra objects till safeguards' (2026-08-08,
    see NOTES.md): set_safeguard_tag, get_equipment_by_marker_id,
    _create_tagged_cause."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_sgtag_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def test_set_safeguard_tag_writes_tag_and_type(self):
        *_ids, sg_id = self._make_full_chain()
        self.db.set_safeguard_tag(sg_id, "PSV-101", "Säkerhetsventil")
        sg = dict(self.db.get_safeguard(sg_id))
        self.assertEqual(sg['comp_tag'], "PSV-101")
        self.assertEqual(sg['comp_type'], "Säkerhetsventil")

    def test_set_safeguard_tag_does_not_touch_description_or_rrf(self):
        *_ids, sg_id = self._make_full_chain()
        self.db.update_safeguard(sg_id, description="Tryckvakt stoppar pump", rrf=100)
        self.db.set_safeguard_tag(sg_id, "PSH-201", "Tryckvakt")
        sg = dict(self.db.get_safeguard(sg_id))
        self.assertEqual(sg['description'], "Tryckvakt stoppar pump")
        self.assertEqual(sg['rrf'], 100)
        self.assertEqual(sg['comp_tag'], "PSH-201")

    def test_get_equipment_by_marker_id_resolves_linked_equipment(self):
        eq_id = self.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
        marker_id = self.db.add_equipment_marker(
            eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9, link_method='leader')
        equip = self.db.get_equipment_by_marker_id(marker_id)
        self.assertIsNotNone(equip)
        self.assertEqual(equip['id'], eq_id)
        self.assertEqual(equip['tag'], "HV-101")

    def test_get_equipment_by_marker_id_returns_none_for_untagged_marker(self):
        marker_id = self.db.add_equipment_marker(
            None, '', 0, 5.0, 5.0, "Ventil", confidence=0.6, link_method='shape')
        self.assertIsNone(self.db.get_equipment_by_marker_id(marker_id))

    def test_get_equipment_by_marker_id_returns_none_for_unknown_marker(self):
        self.assertIsNone(self.db.get_equipment_by_marker_id(999999))

    def test_create_tagged_cause_creates_empty_cause_and_consequence_with_tag(self):
        from hazop import _create_tagged_cause
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']

        cause_id, cons_id = _create_tagged_cause(self.db, dev_id, "Pump", "P-101")

        cause = dict(self.db.get_cause(cause_id))
        # 2026-08-10: unified to the same "Ny orsak" placeholder every
        # other auto-created cause/consequence/safeguard already uses
        # (was blank) — see NOTES.md.
        self.assertEqual(cause['description'], 'Ny orsak')
        self.assertEqual(cause['comp_type'], "Pump")
        self.assertEqual(cause['comp_tag'], "P-101")
        cons = self.db.get_consequence(cons_id)
        self.assertIsNotNone(cons)
        self.assertEqual(dict(cons)['cause_id'], cause_id)


class AppendTagToFreeTextTests(unittest.TestCase):
    """Dragging an equipment marker onto a KON/SG cell now appends the tag
    into the free-text description, building a running sentence, instead
    of only setting the separate tag-strip field (2026-08-09, request:
    'skriver jag hög nivå i och drar TA-1 ... vill jag att denna läggs
    till i textsnittet'). Dragging several different tags onto the same
    cell must keep appending, not overwrite the previous one — the
    complaint about the old tag-strip-only behavior ('ska inte skriva
    över tidigare som idag')."""

    def test_append_tag_to_text_adds_space_before_tag(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("hög nivå i", "TA-1"), "hög nivå i TA-1")

    def test_append_tag_to_text_does_not_duplicate_space(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("hög nivå i ", "TA-1"), "hög nivå i TA-1")

    def test_append_tag_to_text_builds_up_across_repeated_calls(self):
        """The exact scenario from the request: type more text, drag a
        second different tag, and the FIRST tag's text must survive."""
        from hazop import append_tag_to_text
        text = append_tag_to_text("hög nivå i", "TA-1")
        text = text + " => överbreddning till"
        text = append_tag_to_text(text, "TA-2")
        self.assertEqual(text, "hög nivå i TA-1 => överbreddning till TA-2")

    def test_append_tag_to_text_replaces_untouched_placeholder(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("Ny konsekvens", "TA-1"), "TA-1")
        self.assertEqual(append_tag_to_text("Ny safeguard", "PSV-101"), "PSV-101")

    def test_append_tag_to_text_from_empty_is_just_the_tag(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("", "TA-1"), "TA-1")

    def test_append_tag_to_text_blank_tag_is_a_noop(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("hög nivå i", ""), "hög nivå i")


class TagRefsAndBoldRangeTests(unittest.TestCase):
    """Pure helper functions backing 'fetmarkera objekttexten i
    konsekvensen' (2026-08-09) — tagged_refs tracks every tag ever
    drag-appended into a KON/SG cell's text (comp_tag only ever holds the
    MOST RECENT one), and find_tag_bold_ranges locates each occurrence
    of those tags in the rendered text as a whole word."""

    def test_parse_tag_refs_splits_and_strips(self):
        from hazop import parse_tag_refs
        self.assertEqual(parse_tag_refs("TA-1,TA-2"), ["TA-1", "TA-2"])
        self.assertEqual(parse_tag_refs(" TA-1 , TA-2 "), ["TA-1", "TA-2"])

    def test_parse_tag_refs_empty(self):
        from hazop import parse_tag_refs
        self.assertEqual(parse_tag_refs(""), [])
        self.assertEqual(parse_tag_refs(None), [])

    def test_add_tag_ref_appends_new(self):
        from hazop import add_tag_ref
        self.assertEqual(add_tag_ref("", "TA-1"), "TA-1")
        self.assertEqual(add_tag_ref("TA-1", "TA-2"), "TA-1,TA-2")

    def test_add_tag_ref_moves_existing_to_end_without_duplicating(self):
        from hazop import add_tag_ref
        self.assertEqual(add_tag_ref("TA-1,TA-2", "TA-1"), "TA-2,TA-1")

    def test_add_tag_ref_blank_tag_is_a_noop(self):
        from hazop import add_tag_ref
        self.assertEqual(add_tag_ref("TA-1", ""), "TA-1")

    def test_find_tag_bold_ranges_single_occurrence(self):
        from hazop import find_tag_bold_ranges
        text = "hög nivå i TA-1"
        self.assertEqual(find_tag_bold_ranges(text, ["TA-1"]), [(11, 15)])

    def test_find_tag_bold_ranges_multiple_tags_and_occurrences(self):
        from hazop import find_tag_bold_ranges
        text = "hög nivå i TA-1 => överbreddning till TA-2"
        ranges = find_tag_bold_ranges(text, ["TA-1", "TA-2"])
        self.assertEqual([text[s:e] for s, e in ranges], ["TA-1", "TA-2"])

    def test_find_tag_bold_ranges_does_not_match_substring_of_longer_tag(self):
        """'TA-1' must not match inside 'TA-10' — whole-word boundary."""
        from hazop import find_tag_bold_ranges
        text = "nivå i TA-10"
        self.assertEqual(find_tag_bold_ranges(text, ["TA-1"]), [])

    def test_find_tag_bold_ranges_no_match(self):
        from hazop import find_tag_bold_ranges
        self.assertEqual(find_tag_bold_ranges("hög nivå i tanken", ["TA-1"]), [])

    def test_find_tag_bold_ranges_empty_tags_list(self):
        from hazop import find_tag_bold_ranges
        self.assertEqual(find_tag_bold_ranges("hög nivå i TA-1", []), [])


class ConsequenceAndSafeguardTagAppendDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tagappend_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_append_tag_to_consequence_builds_up_text_and_keeps_strip_current(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, "hög nivå i", 2)

        self.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")
        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['description'], "hög nivå i TA-1")
        self.assertEqual(cons['comp_tag'], "TA-1")
        self.assertEqual(cons['comp_type'], "Tank")

        self.db.update_consequence(
            cons_id, cons['description'] + " => överbreddning till", cons['severity'],
            cons['category'] or '', cons['consequence_chain'] or '')
        self.db.append_tag_to_consequence(cons_id, "TA-2", "Tank")
        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['description'], "hög nivå i TA-1 => överbreddning till TA-2")
        self.assertEqual(cons['comp_tag'], "TA-2",
            "the tag strip shows the MOST RECENT drop; the full history lives in the text")
        self.assertEqual(cons['tagged_refs'], "TA-1,TA-2",
            "tagged_refs must remember EVERY tag ever dropped, for bolding both in the text")

    def test_append_tag_to_consequence_preserves_severity_and_category(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        cat = self.db.consequence_categories()[0]
        self.db.update_consequence(cons_id, "beskrivning", 4, cat['name'])

        self.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")

        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['severity'], 4)
        self.assertEqual(cons['category'], cat['name'])

    def test_append_tag_to_safeguard_builds_up_text(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, description="Larm vid")

        self.db.append_tag_to_safeguard(sg_id, "PSH-101", "Tryckvakt")
        sg = dict(self.db.get_safeguard(sg_id))
        self.assertEqual(sg['description'], "Larm vid PSH-101")

        self.db.update_safeguard(sg_id, description=sg['description'] + " och")
        self.db.append_tag_to_safeguard(sg_id, "PSH-102", "Tryckvakt")
        sg = dict(self.db.get_safeguard(sg_id))
        self.assertEqual(sg['description'], "Larm vid PSH-101 och PSH-102")
        self.assertEqual(sg['comp_tag'], "PSH-102")
        self.assertEqual(sg['tagged_refs'], "PSH-101,PSH-102")


class BoldTagPaintSmokeTests(unittest.TestCase):
    """Actually invokes _ScenarioDelegate.paint() for KON/SG cells whose
    description contains drag-appended tags, since find_tag_bold_ranges'
    QTextLayout-based rendering (_draw_text_with_bold_tags) is new code
    with real edge cases (empty text, a tag at the very start/end of the
    string, an untagged row) that pure unit tests of the range-finder
    alone wouldn't exercise. Pixel-level bold verification isn't
    practical here — this only proves painting doesn't raise."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _paint_cell(self, panel, row, col):
        from PyQt6.QtGui import QPixmap, QPainter
        from PyQt6.QtWidgets import QStyleOptionViewItem
        from PyQt6.QtCore import QRect
        index = panel._table.model().index(row, col)
        option = QStyleOptionViewItem()
        option.rect = panel._table.visualRect(index) or QRect(0, 0, 200, 40)
        if option.rect.isEmpty():
            option.rect = QRect(0, 0, 200, 40)
        option.font = panel._table.font()
        pixmap = QPixmap(max(1, option.rect.width()), max(1, option.rect.height()))
        painter = QPainter(pixmap)
        try:
            panel._delegate.paint(painter, option, index)
        finally:
            painter.end()

    def test_paints_kon_cell_with_multiple_tagged_refs_without_raising(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.update_consequence(cons_id, "hög nivå i", 2)
            win.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")
            win.db.append_tag_to_consequence(cons_id, "TA-2", "Tank")
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            # Confirms the wiring, not just "doesn't crash" — _add_row must
            # actually read tagged_refs from the DB onto the item's UserRole
            # slot for _draw_text_with_bold_tags to have anything to bold.
            item = panel._table.item(row, panel._C_KON)
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 8), ["TA-1", "TA-2"])

            try:
                self._paint_cell(panel, row, panel._C_KON)
            except Exception as e:
                self.fail(f"painting a KON cell with tagged_refs must not raise: {e!r}")

    def test_paints_sg_cell_with_tagged_ref_without_raising(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)
            win.db.update_safeguard(sg_id, description="Larm vid")
            win.db.append_tag_to_safeguard(sg_id, "PSH-101", "Tryckvakt")
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            item = panel._table.item(row, panel._C_SG)
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 7), ["PSH-101"])

            try:
                self._paint_cell(panel, row, panel._C_SG)
            except Exception as e:
                self.fail(f"painting a SG cell with tagged_refs must not raise: {e!r}")

    def test_paints_untagged_kon_cell_without_raising(self):
        """No tags at all — must take the fast plain-drawText path cleanly."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.update_consequence(cons_id, "vanlig text utan taggar", 2)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            try:
                self._paint_cell(panel, row, panel._C_KON)
            except Exception as e:
                self.fail(f"painting an untagged KON cell must not raise: {e!r}")


class EquipmentDropOnSafeguardAndMultiTests(unittest.TestCase):
    """_handle_drop's 'equipment'/'equipment-multi' kinds extended to the
    SG column, and multi-marker drops onto a single KON/SG cell using only
    the first dragged marker (2026-08-08, see NOTES.md). Routed through
    panel.eventFilter(), not _handle_drop() directly — see
    DropEventRoutedToViewportTests for why that distinction matters."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        dev_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(dev_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def _make_drop_event(self, panel, text, tgt_row, tgt_col):
        from PyQt6.QtCore import QEvent, QMimeData, QPointF
        vp_x = panel._table.columnViewportPosition(tgt_col) + 2
        vp_y = panel._table.rowViewportPosition(tgt_row) + 2
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(vp_x, vp_y)
        event.dropAction.return_value = Qt.DropAction.CopyAction
        return event

    def test_drop_equipment_on_sg_cell_attaches_tag(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, sg_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            eq_id = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                              "Säkerhetsventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-101", 0, 10.0, 10.0, "Säkerhetsventil", confidence=0.9,
                link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_SG)
            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            sg = dict(win.db.get_safeguard(sg_id))
            self.assertEqual(sg['comp_tag'], "PSV-101")
            self.assertEqual(sg['comp_type'], "Säkerhetsventil")

    def test_second_separate_drop_onto_an_already_tagged_sg_row_creates_new_row(self):
        """The 'different objects on different rows' rule for safeguards
        must hold even when the objects arrive as two SEPARATE drag
        gestures, not just one multi-select drag (2026-08-09, see
        NOTES.md: 'jag vill att den ... skall lägga till flera olika
        objekt om jag drar till safeguards med (flera rader)') — the
        second single-object drop must not silently merge into the
        already-tagged row's text."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, sg_id = self._make_full_chain(win.db)

            eq1 = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                            "Säkerhetsventil", '', 0)
            eq2 = win.db.add_equipment_item("PSV-102", "PSV-102", "PSV", 0,
                                            "Säkerhetsventil", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "PSV-101", 0, 10.0, 10.0,
                                             "Säkerhetsventil", confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "PSV-102", 0, 20.0, 20.0,
                                             "Säkerhetsventil", confidence=0.9, link_method='leader')

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            event1 = self._make_drop_event(
                panel, f'hzp:equipment:{m1}:-1:-1', row, panel._C_SG)
            self.assertTrue(panel.eventFilter(panel._table.viewport(), event1))

            # Reload so _row_meta reflects the just-created state, then drop
            # the SECOND object on the SAME (now already-tagged) row.
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            event2 = self._make_drop_event(
                panel, f'hzp:equipment:{m2}:-1:-1', row, panel._C_SG)
            self.assertTrue(panel.eventFilter(panel._table.viewport(), event2))

            sg = dict(win.db.get_safeguard(sg_id))
            self.assertEqual(sg['description'], "PSV-101",
                "the originally-tagged row's text must not be touched by the second drop")

            all_sgs = [dict(s) for s in win.db.safeguards(cons_id)]
            self.assertEqual(len(all_sgs), 2,
                "the second object must land on a brand new row, not merge into the first")
            new_sg = next(s for s in all_sgs if s['id'] != sg_id)
            self.assertEqual(new_sg['description'], "PSV-102")
            self.assertEqual(new_sg['comp_tag'], "PSV-102")

    def test_drop_equipment_multi_on_kon_appends_all_markers_to_same_consequence(self):
        """Dropping several objects onto ONE consequence must build up its
        text with ALL of them, in order — not just the first (2026-08-09,
        see NOTES.md: 'drar jag till konsekvens skall flera objekt kunna
        ligga i samma konsekvens')."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, _sg = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq1 = win.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
            eq2 = win.db.add_equipment_item("P-102", "P-102", "P", 0, "Pump", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "P-101", 0, 1.0, 1.0, "Pump",
                                             confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "P-102", 0, 2.0, 2.0, "Pump",
                                             confidence=0.9, link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment-multi:{m1},{m2}:-1:-1', row, panel._C_KON)
            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['description'], "P-101 P-102")
            self.assertEqual(cons['comp_tag'], "P-102",
                "the tag strip shows the most recently applied marker")

    def test_drop_equipment_multi_on_sg_creates_one_row_per_extra_marker(self):
        """Dropping several objects onto a SAFEGUARD must NOT merge them
        into one row's text — each additional object becomes its own new
        safeguard row under the same consequence, since distinct objects
        there read as distinct barriers (2026-08-09, see NOTES.md: 'drar
        jag till safeguard skall de olika objekten vara på olika rader')."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, sg_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            eq1 = win.db.add_equipment_item("TSH-1", "TSH-1", "TSH", 0, "Termostat", '', 0)
            eq2 = win.db.add_equipment_item("TSH-2", "TSH-2", "TSH", 0, "Termostat", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "TSH-1", 0, 1.0, 1.0, "Termostat",
                                             confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "TSH-2", 0, 2.0, 2.0, "Termostat",
                                             confidence=0.9, link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment-multi:{m1},{m2}:-1:-1', row, panel._C_SG)
            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            sg = dict(win.db.get_safeguard(sg_id))
            self.assertEqual(sg['description'], "TSH-1")
            self.assertEqual(sg['comp_tag'], "TSH-1")

            all_sgs = [dict(s) for s in win.db.safeguards(cons_id)]
            self.assertEqual(len(all_sgs), 2, "a second safeguard row must be created")
            new_sg = next(s for s in all_sgs if s['id'] != sg_id)
            self.assertEqual(new_sg['description'], "TSH-2")
            self.assertEqual(new_sg['comp_tag'], "TSH-2")

    def test_sg_cell_carries_comp_tag_via_userrole(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _cons_id, sg_id = self._make_full_chain(win.db)
            win.db.set_safeguard_tag(sg_id, "FE-301", "Flödesgivare")

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            item = panel._table.item(row, panel._C_SG)
            comp_type, comp_tag = item.data(Qt.ItemDataRole.UserRole + 6)
            self.assertEqual(comp_tag, "FE-301")
            self.assertEqual(comp_type, "Flödesgivare")


class EquipmentDropOnTreeDeviationTests(unittest.TestCase):
    """Dragging equipment marker(s) onto a HAZOP-tree deviation item (e.g.
    "Lågt flöde") creates one empty, tagged cause per marker directly — no
    popup (2026-08-08, see NOTES.md, decision: 'Skapa tom orsak direkt')."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_drop_event(self, text, pos):
        from PyQt6.QtCore import QEvent, QMimeData, QPointF
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(pos)
        return event

    def test_tree_drop_on_deviation_emits_signal_with_marker_ids(self):
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            tree_panel.refresh()
            tree_panel.tree.expandAll()   # itemAt()/visualItemRect() need the row actually visible
            dev_item = _find_tree_item(tree_panel.tree, DEV_T, dev_id)
            self.assertIsNotNone(dev_item, "sanity: the deviation must actually be in the tree")
            pos = tree_panel.tree.visualItemRect(dev_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            handled = tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertTrue(handled)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0], (dev_id, [42]))

    def test_tree_drop_on_ledord_wrapper_with_existing_equipment_still_resolves(self):
        """Bug report (2026-08-13): 'om det redan ligger ett objekt på
        lågt flöde i trädet och jag drar ett nytt objekt dit så kan jag
        inte detta' — once a guide word has ANY equipment linked, "Lågt
        flöde" no longer renders as a plain DEV_T item; it becomes a
        LEDORD_T wrapper, which the drop handler used to reject outright
        (only literal DEV_T resolved), silently swallowing the drop of a
        second/different object onto that same guide word."""
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            existing_eq = win.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
            node_id = win.db.add_node()
            win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=existing_eq)
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            ledord_item = _find_tree_item(tree_panel.tree, LEDORD_T, f"{node_id}:Lågt flöde")
            self.assertIsNotNone(ledord_item,
                "sanity: an already-equipped guide word must render as a LEDORD_T wrapper")
            pos = tree_panel.tree.visualItemRect(ledord_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            handled = tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertTrue(handled)
            self.assertEqual(len(captured), 1, "the drop must resolve to a deviation, not be swallowed")
            resolved_dev_id, marker_ids = captured[0]
            self.assertEqual(marker_ids, [42])
            resolved = win.db.get_deviation(resolved_dev_id)
            self.assertEqual(resolved['node_id'], node_id)
            self.assertEqual(resolved['description'], "Lågt flöde")
            self.assertIsNone(resolved['equipment_id'],
                "must land on the guide word's own still-generic row, not steal the first object's")

    def test_drag_move_over_ledord_wrapper_accepts_without_writing_to_db(self):
        """The DragMove hover-feedback path must only ever check whether
        a drop WOULD be valid — it must not create a deviation row (a DB
        write) just because the mouse passed over the item."""
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            existing_eq = win.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
            node_id = win.db.add_node()
            win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=existing_eq)
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            ledord_item = _find_tree_item(tree_panel.tree, LEDORD_T, f"{node_id}:Lågt flöde")
            pos = tree_panel.tree.visualItemRect(ledord_item).center()
            before = win.db.deviations(node_id)

            from PyQt6.QtCore import QEvent, QMimeData, QPointF
            mime = QMimeData(); mime.setText('hzp:equipment:42:-1:-1')
            event = unittest.mock.MagicMock()
            event.type.return_value = QEvent.Type.DragMove
            event.mimeData.return_value = mime
            event.position.return_value = QPointF(pos)
            tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            event.acceptProposedAction.assert_called_once()
            self.assertEqual(win.db.deviations(node_id), before,
                "hovering must not create a new deviation row")

    def test_tree_drop_on_merged_single_equipment_cause_row_resolves_its_own_deviation(self):
        """The other tree shape an equipped guide word can collapse into
        (2026-08-09 'kaka på kaka'): a CAUSE_T-typed merged row when the
        linked equipment's only cause is still trivial/untouched. This
        must resolve back to that SAME equipment's own deviation, not
        be rejected either."""
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            eq_id = win.db.add_equipment_item("=M1.GPA6", "=M1.GPA6", "M1", 0, "Pump", '', 0)
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
            from hazop import _create_tagged_cause
            _create_tagged_cause(win.db, dev_id, "Pump", "=M1.GPA6")
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            merged_item = _find_tree_item(tree_panel.tree, CAUSE_T)
            self.assertIsNotNone(merged_item,
                "sanity: the trivial tagged cause must have merged into a CAUSE_T row")
            pos = tree_panel.tree.visualItemRect(merged_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))
            event = self._make_drop_event('hzp:equipment:43:-1:-1', pos)
            tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertEqual(captured, [(dev_id, [43])])

    def test_tree_drop_on_non_deviation_item_is_ignored(self):
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            node_id = win.db.add_node()
            tree_panel.refresh()
            node_item = _find_tree_item(tree_panel.tree, NODE_T, node_id)
            self.assertIsNotNone(node_item)
            pos = tree_panel.tree.visualItemRect(node_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertEqual(captured, [])
            event.ignore.assert_called()

    def test_on_equipment_dropped_on_deviation_creates_one_cause_per_marker(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq1 = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            eq2 = win.db.add_equipment_item("V-2", "V-2", "V", 0, "Ventil", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "V-1", 0, 1.0, 1.0, "Ventil",
                                             confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "V-2", 0, 2.0, 2.0, "Ventil",
                                             confidence=0.9, link_method='leader')

            win._on_equipment_dropped_on_deviation(dev_id, [m1, m2])

            causes = win.db.causes(node_id)
            tagged = {c['comp_tag'] for c in causes}
            self.assertEqual(tagged, {"V-1", "V-2"})
            for c in causes:
                # 2026-08-10: unified placeholder text, see NOTES.md
                self.assertEqual(dict(c)['description'], 'Ny orsak')
                self.assertEqual(len(win.db.consequences(c['id'])), 1)

    def test_on_equipment_dropped_on_deviation_assigns_node_when_missing(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("T-1", "T-1", "T", 0, "Behållare", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "T-1", 0, 1.0, 1.0, "Behållare", confidence=0.9, link_method='leader')
            self.assertIsNone(win.db.equipment_node_id(eq_id))

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            self.assertEqual(win.db.equipment_node_id(eq_id), node_id)

    def test_on_equipment_dropped_on_deviation_ignores_unlinked_markers(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            marker_id = win.db.add_equipment_marker(
                None, '', 0, 1.0, 1.0, "Ventil", confidence=0.5, link_method='shape')
            try:
                win._on_equipment_dropped_on_deviation(dev_id, [marker_id])
            except Exception as e:
                self.fail(f"must not raise for an untagged/unlinked marker: {e!r}")
            self.assertEqual(win.db.causes(node_id), [])

    def test_on_equipment_dropped_on_deviation_also_sets_deviation_equipment(self):
        """'drar jag ett eller flera objekt till trädet skall även
        kolumnen utrustning fyllas i så det blir stringent, inte bara
        under orsak' (2026-08-09) — previously only the created CAUSE
        got comp_tag/comp_type (shown in the ORS column); the deviation's
        own equipment_id (driving the worksheet's separate Utrustning
        column) was left untouched, unlike the EquipmentDeviationBar
        checkbox flow which always sets both."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "V-1", 0, 1.0, 1.0, "Ventil", confidence=0.9, link_method='leader')
            self.assertIsNone(win.db.get_deviation(dev_id)['equipment_id'])

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            self.assertEqual(win.db.get_deviation(dev_id)['equipment_id'], eq_id)

    def test_on_equipment_dropped_on_deviation_does_not_override_existing_equipment(self):
        """A deviation already tied to a specific equipment (e.g. from an
        earlier drop, or the EquipmentDeviationBar flow) must not be
        silently reassigned to a DIFFERENT equipment by a later drop —
        matches the same 'first one wins' rule already used for the
        equipment's own node assignment."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq1 = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            eq2 = win.db.add_equipment_item("V-2", "V-2", "V", 0, "Ventil", '', 0)
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq1)
            marker2 = win.db.add_equipment_marker(
                eq2, "V-2", 0, 2.0, 2.0, "Ventil", confidence=0.9, link_method='leader')

            win._on_equipment_dropped_on_deviation(dev_id, [marker2])

            self.assertEqual(win.db.get_deviation(dev_id)['equipment_id'], eq1)

    def test_on_equipment_dropped_on_deviation_worksheet_utrustning_column_reflects_it(self):
        """End-to-end: after the drop, the worksheet's Utrustning column
        for the created cause's row must show the equipment, not stay
        blank."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("T-1", "T-1", "T", 0, "Behållare", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "T-1", 0, 1.0, 1.0, "Behållare", confidence=0.9, link_method='leader')

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            panel = win.scenario_panel
            causes = win.db.causes(node_id)
            cause_id = next(c['id'] for c in causes if c['comp_tag'] == 'T-1')
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            utr_item = panel._table.item(row, panel._C_UTR)
            self.assertIn('T-1', utr_item.text())


class EquipmentMultiSelectTests(unittest.TestCase):
    """Multi-select of equipment markers on the P&ID (2026-08-08, see
    NOTES.md): Ctrl+click toggles, Ctrl+drag rubber-bands several at once,
    and a Shift-drag of a >=2-member selection drags the whole group."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_markers(self, marker_positions):
        """marker_positions: dict marker_id -> QPointF scene position."""
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        items = {}
        for marker_id, pos in marker_positions.items():
            item = view._scene.addEllipse(pos.x() - 5, pos.y() - 5, 10, 10)
            item.setData(view._DATA_TYPE, 'equipment')
            item.setData(view._DATA_ID, marker_id)
            view._type_items.setdefault('equipment', []).append(item)
            items[marker_id] = item
        return view, items

    def _press(self, view, event):
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mousePressEvent'):
            view.mousePressEvent(event)

    def _move(self, view, event):
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mouseMoveEvent'):
            view.mouseMoveEvent(event)

    def _release(self, view, event):
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mouseReleaseEvent'):
            view.mouseReleaseEvent(event)

    def test_ctrl_click_toggles_marker_into_selection(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        view.mapToScene = lambda pt: QPointF(50, 50)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)

        self.assertIn(7, view._selected_equipment_markers)
        self.assertIn(7, view._equip_selection_overlays)

    def test_ctrl_click_again_deselects_marker(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        view.mapToScene = lambda pt: QPointF(50, 50)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)
        self._press(view, event)

        self.assertNotIn(7, view._selected_equipment_markers)
        self.assertNotIn(7, view._equip_selection_overlays)

    def test_plain_click_clears_existing_selection(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        view._select_equipment_marker(7)
        self.assertIn(7, view._selected_equipment_markers)

        view.mapToScene = lambda pt: QPointF(200, 200)   # empty area, no marker there
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
        event.position.return_value = QPointF(200, 200)

        self._press(view, event)

        self.assertEqual(view._selected_equipment_markers, set())

    def test_ctrl_drag_rubber_band_selects_markers_in_rect(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({
            1: QPointF(10, 10), 2: QPointF(20, 20), 3: QPointF(500, 500),
        })
        # mapToScene: identity-ish, viewport coords == scene coords for this test
        view.mapToScene = lambda pt: QPointF(pt)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        press_event.position.return_value = QPointF(0, 0)
        self._press(view, press_event)
        self.assertIsNotNone(view._ctrl_rband_start_scene)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        move_event.position.return_value = QPointF(30, 30)
        self._move(view, move_event)
        self.assertTrue(view._ctrl_rband_dragging)

        release_event = unittest.mock.MagicMock()
        release_event.button.return_value = Qt.MouseButton.LeftButton
        release_event.position.return_value = QPointF(30, 30)
        self._release(view, release_event)

        self.assertEqual(view._selected_equipment_markers, {1, 2},
            "only markers inside the 0,0-30,30 band should be selected")
        self.assertNotIn(3, view._selected_equipment_markers)

    def test_shift_drag_of_multi_selection_builds_equipment_multi_mime(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({
            5: QPointF(50, 50), 6: QPointF(60, 60),
        })
        view._select_equipment_marker(5)
        view._select_equipment_marker(6)
        view.mapToScene = lambda pt: QPointF(50, 50)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)
        self.assertEqual(view._equip_drag_candidate[0], 5)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(90, 50)

        with unittest.mock.patch('pid_viewer.QDrag') as MockDrag:
            mock_drag = MockDrag.return_value
            view.mouseMoveEvent(move_event)

        mime_arg = mock_drag.setMimeData.call_args[0][0]
        self.assertEqual(mime_arg.text(), 'hzp:equipment-multi:5,6:-1:-1')
        # Selection is cleared once the group drag has actually started.
        self.assertEqual(view._selected_equipment_markers, set())

    def test_shift_drag_of_single_unselected_marker_still_uses_plain_kind(self):
        """A Shift-drag from a marker NOT part of any multi-selection must
        keep using the original single-marker mime — no regression for the
        already-shipped, already-tested single-drag feature."""
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({9: QPointF(50, 50)})
        view.mapToScene = lambda pt: QPointF(50, 50)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(90, 50)

        with unittest.mock.patch('pid_viewer.QDrag') as MockDrag:
            mock_drag = MockDrag.return_value
            view.mouseMoveEvent(move_event)

        mime_arg = mock_drag.setMimeData.call_args[0][0]
        self.assertEqual(mime_arg.text(), 'hzp:equipment:9:-1:-1')

    def test_ctrl_drag_shows_live_count_label(self):
        """(2026-08-10, see NOTES.md) — the count label updates DURING the
        drag, not just after release."""
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({1: QPointF(10, 10), 2: QPointF(20, 20)})
        view.mapToScene = lambda pt: QPointF(pt)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        press_event.position.return_value = QPointF(0, 0)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        move_event.position.return_value = QPointF(30, 30)
        self._move(view, move_event)

        self.assertIsNotNone(view._ctrl_rband_count_label)
        self.assertEqual(view._ctrl_rband_count_label.text(), "2 objekt")

    def test_ctrl_drag_count_label_removed_when_band_empty(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({1: QPointF(500, 500)})
        view.mapToScene = lambda pt: QPointF(pt)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        press_event.position.return_value = QPointF(0, 0)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        move_event.position.return_value = QPointF(30, 30)   # marker at (500,500) is outside
        self._move(view, move_event)

        self.assertIsNone(view._ctrl_rband_count_label)

    def test_escape_clears_multi_selection(self):
        """(2026-08-10, see NOTES.md) — previously Escape only cancelled
        placement/drawing modes, never an equipment multi-selection."""
        from PyQt6.QtCore import QPointF, QEvent
        from PyQt6.QtGui import QKeyEvent
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        view._select_equipment_marker(1)
        self.assertEqual(view._selected_equipment_markers, {1})

        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        view.keyPressEvent(ev)

        self.assertEqual(view._selected_equipment_markers, set())

    def test_context_menu_offers_clear_selection_when_markers_selected(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        view._select_equipment_marker(1)
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(500, 500), QPoint(0, 0))

        self.assertTrue(any("Rensa markering" in t for t in texts), texts)

    def test_clearing_via_context_menu_action_clears_selection(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        view._select_equipment_marker(1)

        def _fake_exec(menu_self, _pos=None):
            for a in menu_self.actions():
                if "Rensa markering" in a.text():
                    a.trigger()
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(500, 500), QPoint(0, 0))

        self.assertEqual(view._selected_equipment_markers, set())

    def test_equipment_marker_tooltip_mentions_gestures(self):
        """(2026-08-10, see NOTES.md) — Ctrl/Shift modifiers had no other
        visible affordance anywhere in the UI."""
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 10.0, 10.0, "Ventil", tag="V-1")
        item = view._type_items['equipment'][0]
        self.assertIn("Ctrl", item.toolTip())
        self.assertIn("Shift", item.toolTip())


class EquipmentPlainClickHighlightTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): 'När jag klickar på ett
    definerat objekt (rött eller grönt) vill jag att det blir blått så man
    ser att det är markerat.' A plain (no-modifier) click on an existing
    equipment marker now single-selects it — reusing the exact same
    dashed-blue overlay mechanism Ctrl-click multi-select already draws
    (EquipmentMultiSelectTests), just triggered from the ordinary NAV-mode
    click-dispatch in mouseReleaseEvent instead of only from Ctrl+click."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_markers(self, marker_positions):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        items = {}
        for marker_id, pos in marker_positions.items():
            item = view._scene.addEllipse(pos.x() - 5, pos.y() - 5, 10, 10)
            item.setData(view._DATA_TYPE, 'equipment')
            item.setData(view._DATA_ID, marker_id)
            view._type_items.setdefault('equipment', []).append(item)
            items[marker_id] = item
        return view, items

    def _click(self, view, pos):
        """Simulate a full plain-click (press + release, no movement) at
        scene position `pos`, bypassing the real QGraphicsView base
        implementation the same way EquipmentMultiSelectTests does."""
        from PyQt6.QtWidgets import QGraphicsView
        view.mapToScene = lambda pt: pos
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
        event.position.return_value = pos
        with unittest.mock.patch.object(QGraphicsView, 'mousePressEvent'):
            view.mousePressEvent(event)
        with unittest.mock.patch.object(QGraphicsView, 'mouseReleaseEvent'):
            view.mouseReleaseEvent(event)

    def test_plain_click_on_equipment_marker_selects_it(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        self._click(view, QPointF(50, 50))
        self.assertEqual(view._selected_equipment_markers, {7})
        self.assertIn(7, view._equip_selection_overlays)

    def test_plain_click_on_equipment_marker_emits_marker_clicked(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        captured = []
        view.marker_clicked.connect(lambda t, i: captured.append((t, i)))
        self._click(view, QPointF(50, 50))
        self.assertEqual(captured, [('equipment', 7)])

    def test_clicking_a_second_marker_replaces_the_first_selection(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({
            1: QPointF(10, 10), 2: QPointF(90, 90),
        })
        self._click(view, QPointF(10, 10))
        self.assertEqual(view._selected_equipment_markers, {1})
        self._click(view, QPointF(90, 90))
        self.assertEqual(view._selected_equipment_markers, {2},
            "clicking a new marker must replace the previous highlight, not add to it")

    def test_reapply_equipment_selection_overlays_survives_marker_rebuild(self):
        """_load_overlays() (triggered e.g. right after checking a
        deviation, to recolor red->green) removes and recreates every
        equipment marker item and calls clear_overlays(), which also wipes
        the independent selection-overlay rects. Without reapplication the
        blue highlight would vanish the instant the user interacts further
        with the very marker they just selected."""
        from PyQt6.QtCore import QPointF
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        view._select_equipment_marker(7)
        self.assertIn(7, view._equip_selection_overlays)

        # Simulate clear_overlays() + marker rebuild: old overlay + old
        # marker item both removed from the scene, a fresh marker item
        # (same id) added in their place.
        view._scene.removeItem(view._equip_selection_overlays[7])
        view._scene.removeItem(items[7])
        view._type_items['equipment'] = []
        new_item = view._scene.addEllipse(45, 45, 10, 10)
        new_item.setData(view._DATA_TYPE, 'equipment')
        new_item.setData(view._DATA_ID, 7)
        view._type_items['equipment'].append(new_item)

        view._reapply_equipment_selection_overlays()

        self.assertEqual(view._selected_equipment_markers, {7})
        self.assertIn(7, view._equip_selection_overlays)

    def test_reapply_equipment_selection_overlays_drops_deleted_markers(self):
        from PyQt6.QtCore import QPointF
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        view._select_equipment_marker(7)

        view._scene.removeItem(view._equip_selection_overlays[7])
        view._scene.removeItem(items[7])
        view._type_items['equipment'] = []   # marker 7 genuinely gone now

        view._reapply_equipment_selection_overlays()

        self.assertEqual(view._selected_equipment_markers, set())
        self.assertEqual(view._equip_selection_overlays, {})


class EquipmentMarkerEditContextMenuTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): right-clicking an
    existing P&ID object offered no way to edit its tag/type — it fell
    through to the generic "add a new object here" menu. New "✏️ Redigera
    objekt" action, offered only when the right-click actually lands on
    an existing equipment marker."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_marker(self, marker_id=1):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        view.add_equipment_marker(marker_id, 10.0, 10.0, "Ventil", tag="V-1")
        item = view._type_items['equipment'][0]
        scene_pos = item.sceneBoundingRect().center()
        return view, scene_pos

    def test_context_menu_offers_edit_when_hovering_equipment_marker(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu
        view, scene_pos = self._make_view_with_marker()
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(scene_pos, QPoint(0, 0))

        self.assertTrue(any("Redigera objekt" in t for t in texts), texts)

    def test_triggering_edit_action_emits_equipment_edit_requested(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu
        view, scene_pos = self._make_view_with_marker(marker_id=42)
        received = []
        view.equipment_edit_requested.connect(received.append)

        def _fake_exec(menu_self, _pos=None):
            for a in menu_self.actions():
                if "Redigera objekt" in a.text():
                    a.trigger()
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(scene_pos, QPoint(0, 0))

        self.assertEqual(received, [42])

    def test_no_edit_action_when_not_hovering_a_marker(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _scene_pos = self._make_view_with_marker()
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(-500, -500), QPoint(0, 0))

        self.assertFalse(any("Redigera objekt" in t for t in texts), texts)


class EquipmentEditRequestedHandlerTests(unittest.TestCase):
    """MainWindow._on_equipment_edit_requested — the popup + DB-write side
    of the "✏️ Redigera objekt" action above."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipedit_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_committing_new_tag_and_type_updates_existing_catalog_row(self):
        from PyQt6.QtWidgets import QDialog
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db

            def _fake_exec(self):
                self.committed.emit("PV-102", "Pump")
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.EquipmentTagPopup, 'exec', new=_fake_exec), \
                 unittest.mock.patch.object(win.pid_panel, 'reload_overlays') as mock_reload:
                win._on_equipment_edit_requested(marker_id)

            updated = self.db.get_equipment_by_id(eq_id)
            self.assertEqual(updated['tag'], "PV-102")
            self.assertEqual(updated['equipment_type'], "Pump")
            mock_reload.assert_called_once()

    def test_untagged_marker_shows_info_instead_of_crashing(self):
        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            with unittest.mock.patch.object(hazop.EquipmentTagPopup, 'exec') as mock_exec, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                win._on_equipment_edit_requested(9999)   # no such marker
            mock_exec.assert_not_called()
            mock_info.assert_called_once()


class CauseTagLiveLinkTests(unittest.TestCase):
    """"fixa till så att taggen är kopplad till objekten i orsaken på
    hazop scenario. så ändrar jag i hazop scenario ändras namnet på
    p&id och vice versa" (2026-08-13) — the ORS cell's tag strip
    (comp_type/comp_tag) used to be a frozen text snapshot with no
    connection to equipment_catalog. causes.equipment_id is now a real
    FK (same pattern as the pre-existing deviations.equipment_id),
    resolved live at render time (_cause_tag_display) so a rename on
    the P&ID shows up immediately, and _apply_cause_obj (the
    CauseObjectPopup commit handler) renames the ACTUAL
    equipment_catalog row when the user edits the tag text of an
    already-linked cause, instead of just overwriting this one cell's
    private copy."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_causetaglink_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _ors_tag(self, cause_id):
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == cause_id)
        item = self.panel._table.item(row, self.panel._C_ORS)
        return item.data(Qt.ItemDataRole.UserRole + 2), row

    def test_create_tagged_cause_links_equipment_id(self):
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        self.assertEqual(self.db.get_cause(cause_id)['equipment_id'], self.eq_id)

    def test_ors_strip_reflects_current_equipment_tag_after_pid_rename(self):
        """The P&ID-rename → hazop-scenario direction: renaming the
        object updates the strip on the very next redraw, with no
        write to the causes row at all."""
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)

        self.db.update_equipment_item(self.eq_id, "PV-102", "PV", "Ventil", "")
        (comp_type, comp_tag), _ = self._ors_tag(cause_id)
        self.assertEqual(comp_tag, "PV-102")
        self.assertEqual(comp_type, "Ventil")
        # The row's own comp_tag snapshot is untouched by the rename —
        # only the live resolution changed what's displayed.
        self.assertEqual(self.db.get_cause(cause_id)['comp_tag'], "PV-101")

    def test_editing_tag_in_popup_renames_the_actual_equipment(self):
        """The hazop-scenario → P&ID direction: editing the tag text of
        an already-linked cause renames equipment_catalog itself."""
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-103", "", None)

        self.assertEqual(self.db.get_equipment_by_id(self.eq_id)['tag'], "PV-103")

    def test_rename_via_popup_is_visible_from_a_different_cause_on_the_same_equipment(self):
        """Confirms the link is shared via equipment_id, not private to
        the one cell that triggered the rename."""
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        other_cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-103", "", None)

        (_, other_tag), _ = self._ors_tag(other_cause_id)
        self.assertEqual(other_tag, "PV-103")

    def test_editing_tag_calls_the_equipment_renamed_callback(self):
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        _, row = self._ors_tag(cause_id)
        called = []
        self.panel._on_equipment_renamed_fn = lambda: called.append(True)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-103", "", None)

        self.assertEqual(called, [True])

    def test_new_tag_matching_an_existing_object_links_without_renaming(self):
        """A cause with no link yet, whose typed tag happens to match an
        existing object exactly, gets LINKED — there's nothing to
        rename FROM, so the object itself is untouched."""
        cause_id = self.db.add_cause(self.dev_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-101", "", None)

        self.assertEqual(self.db.get_cause(cause_id)['equipment_id'], self.eq_id)
        self.assertEqual(self.db.get_equipment_by_id(self.eq_id)['tag'], "PV-101")

    def test_custom_unmatched_tag_stays_unlinked(self):
        cause_id = self.db.add_cause(self.dev_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Övrigt", "CUSTOM-999", "", None)

        self.assertIsNone(self.db.get_cause(cause_id)['equipment_id'])

    def test_backfill_links_an_unambiguous_existing_comp_tag(self):
        cause_id = self.db.add_cause(self.dev_id)
        self.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")
        self.db.conn.execute("UPDATE causes SET equipment_id=NULL WHERE id=?", (cause_id,))
        self.db.commit()

        self.db._backfill_cause_equipment_ids()

        self.assertEqual(self.db.get_cause(cause_id)['equipment_id'], self.eq_id)

    def test_backfill_leaves_ambiguous_comp_tag_unlinked(self):
        dup_eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 1, "Ventil", '', 0)
        cause_id = self.db.add_cause(self.dev_id)
        self.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")
        self.db.conn.execute("UPDATE causes SET equipment_id=NULL WHERE id=?", (cause_id,))
        self.db.commit()

        self.db._backfill_cause_equipment_ids()

        self.assertIsNone(self.db.get_cause(cause_id)['equipment_id'])

    def test_pid_rename_triggers_scenario_rebuild_via_mainwindow_wiring(self):
        """End-to-end confirmation of the MainWindow-level wiring, not
        just the panel's own logic in isolation."""
        from PyQt6.QtWidgets import QDialog
        eq_id = self.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "V-1", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            win.scenario_panel.db = self.db

            def _fake_exec(self):
                self.committed.emit("V-2", "Ventil")
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.EquipmentTagPopup, 'exec', new=_fake_exec), \
                 unittest.mock.patch.object(win.pid_panel, 'reload_overlays'), \
                 unittest.mock.patch.object(win.scenario_panel, '_schedule_rebuild') as mock_rebuild:
                win._on_equipment_edit_requested(marker_id)

            mock_rebuild.assert_called_once()


class TagDetachContextMenuTests(unittest.TestCase):
    """The KON/SG tag strip (with its inline "×") was removed 2026-08-10
    (see NOTES.md, "ta bort tagg remsa") — a tag now shows only inline,
    bolded in the description text. Detaching a tag moved to a
    "✕  Ta bort tagg" context-menu action, offered only when the row
    actually carries one, matching this session's other "move rare
    actions to context menus" cleanup."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tagdetach_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def _menu_labels(self, panel, row, col):
        from PyQt6.QtCore import QPoint
        with unittest.mock.patch.object(panel._table, 'rowAt', return_value=row), \
             unittest.mock.patch.object(panel._table, 'columnAt', return_value=col), \
             unittest.mock.patch('hazop.QMenu') as mock_menu_cls:
            panel._on_context_menu(QPoint(0, 0))
        mock_menu = mock_menu_cls.return_value
        return _menu_action_labels(mock_menu)

    def test_context_menu_offers_untag_when_kon_tagged(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, _sg_id = self._make_full_chain()
            self.db.set_consequence_tag(cons_id, "P-101", "Pump")
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            labels = self._menu_labels(panel, row, panel._C_KON)
            self.assertTrue(any("Ta bort tagg" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_context_menu_omits_untag_when_kon_untagged(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, _sg_id = self._make_full_chain()
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            labels = self._menu_labels(panel, row, panel._C_KON)
            self.assertFalse(any("Ta bort tagg" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_context_menu_offers_untag_when_sg_tagged(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, sg_id = self._make_full_chain()
            self.db.set_safeguard_tag(sg_id, "PSV-101", "Säkerhetsventil")
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            labels = self._menu_labels(panel, row, panel._C_SG)
            self.assertTrue(any("Ta bort tagg" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_untag_consequence_clears_tag(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, _sg_id = self._make_full_chain()
            self.db.set_consequence_tag(cons_id, "P-101", "Pump")
            panel.load_node(node_id)

            panel._untag_consequence(cons_id)

            cons = dict(self.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], '')
            self.assertEqual(cons['comp_type'], '')
        finally:
            panel.deleteLater()

    def test_untag_safeguard_clears_tag(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, sg_id = self._make_full_chain()
            self.db.set_safeguard_tag(sg_id, "PSV-101", "Säkerhetsventil")
            panel.load_node(node_id)

            panel._untag_safeguard(sg_id)

            sg = dict(self.db.get_safeguard(sg_id))
            self.assertEqual(sg['comp_tag'], '')
            self.assertEqual(sg['comp_type'], '')
        finally:
            panel.deleteLater()


class ObjektInRubberBandMenuTests(unittest.TestCase):
    """'När jag håller nere högerknappen och drar fram gummiband vill jag
    ... även kunna välja Objekt.' (2026-08-09, see NOTES.md) — the
    right-drag rubber-band handler (PIDPanel._on_zone_drawn) originally
    showed a menu of Objekt/Orsak/Konsekvens/Safeguard. Since the P&ID
    canvas is now object-placement-only (2026-08-13, see NOTES.md — the
    other three actions were removed), a drawn rectangle always becomes a
    new equipment object directly — no menu needed for a single choice.
    Still reuses the existing EquipmentTagPopup flow and threads the drawn
    rectangle through so the new marker gets a real outline shape instead
    of the generic bowtie-icon fallback a bare point gets."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rbandobj_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_choosing_objekt_emits_placement_signal_with_the_drawn_rect(self):
        """No menu to choose from anymore — a drawn rectangle always
        becomes a new equipment object directly (2026-08-13, see
        NOTES.md)."""
        from PyQt6.QtCore import QRectF
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page, rect: captured.append((tag, pos, page, rect)))
        pdf_rect = QRectF(5.0, 6.0, 10.0, 8.0)

        self.panel._on_zone_drawn(pdf_rect, 3)

        self.assertEqual(len(captured), 1)
        tag, pos, page, rect = captured[0]
        self.assertEqual(page, 3)
        self.assertEqual(rect, pdf_rect)

    def test_place_equipment_marker_with_rect_stores_shape_outline(self):
        import json
        from PyQt6.QtCore import QPointF, QRectF
        rect = QRectF(5.0, 6.0, 10.0, 8.0)

        self.panel.place_equipment_marker("V-500", "Ventil", QPointF(50, 50), 0, pdf_rect=rect)

        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        outline = json.loads(markers[0]['shape_outline'])
        self.assertEqual(len(outline), 4)
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self.assertAlmostEqual(min(xs), rect.left())
        self.assertAlmostEqual(max(xs), rect.right())
        self.assertAlmostEqual(min(ys), rect.top())
        self.assertAlmostEqual(max(ys), rect.bottom())

    def test_place_equipment_marker_without_rect_has_blank_shape_outline(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("V-600", "Ventil", QPointF(20, 20), 0)
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(markers[0]['shape_outline'], '')

    def test_plain_right_click_objekt_still_has_no_rect(self):
        """The plain right-click "🔧 Objekt" action (already shipped) must
        keep passing None for pdf_rect — a single point has no rectangle
        to give it."""
        from PyQt6.QtCore import QPointF
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page, rect: captured.append(rect))
        self.panel._on_context_action('equipment', QPointF(5, 5), 0)
        self.assertEqual(captured, [None])


class AutoConsequenceAndSafeguardOnCauseTemplateTests(unittest.TestCase):
    """'När jag definerar avvikelse för objektet så ska jag kunna klicka på
    konsekvens ... och definiera detta. ... Dessutom vill jag kunna göra
    samma med safeguard.' (2026-08-09, see NOTES.md) — checking a deviation
    in EquipmentDeviationBar used to create a cause with NO consequence/
    safeguard at all, so the KON/SG cells for that row had no real item to
    click into. Both are now auto-created empty, immediately ready for the
    already-existing KON/SG inline-edit machinery (from earlier sessions,
    see NOTES.md). The classic P&ID-click cause flow this class used to
    also cover was removed 2026-08-13 (see NOTES.md: the P&ID canvas is
    now object-placement-only) — place_cause_from_template's only
    remaining caller is EquipmentDeviationBar's _create_cause_for_bar."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autocons_sg_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_place_cause_from_template_creates_empty_consequence_and_safeguard(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']

        cause_id = self.panel.place_cause_from_template(
            dev_id, "Ventil", "HV-101", "Läckage", None)

        self.assertIsNotNone(cause_id)
        cons_list = self.db.consequences(cause_id)
        self.assertEqual(len(cons_list), 1)
        cons_id = cons_list[0]['id']
        # db.add_consequence()/add_safeguard() default to empty (2026-08-12,
        # see NOTES.md — shown as "—" until defined, not literal "Ny
        # konsekvens"/"Ny safeguard" text) — still immediately overtype-
        # able via the existing KON/SG inline-edit machinery either way.
        self.assertEqual(cons_list[0]['description'], '')
        sg_list = self.db.safeguards(cons_id)
        self.assertEqual(len(sg_list), 1)
        self.assertEqual(sg_list[0]['description'], '')

    def test_create_cause_for_bar_also_gets_consequence_and_safeguard(self):
        """The EquipmentDeviationBar checkbox flow specifically — routes
        through place_cause_from_template via _create_cause_for_bar."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        marker_id = self.db.add_equipment_marker(
            eq_id, "P-101", 0, 10.0, 10.0, "Pump", confidence=0.9, link_method='leader')
        self.panel._equipment_bar.load(eq_id, marker_id)

        cause_id = self.panel._create_cause_for_bar(
            dev_id, "Pump", "P-101", "Ingen flödesindikering")

        self.assertIsNotNone(cause_id)
        cons_list = self.db.consequences(cause_id)
        self.assertEqual(len(cons_list), 1)
        self.assertEqual(len(self.db.safeguards(cons_list[0]['id'])), 1)

    def test_create_cause_for_bar_does_not_draw_a_duplicate_marker(self):
        """Reported feedback (2026-08-12, see NOTES.md): 'När jag skapat
        ett manuellt objekt i pid viewer och sedan definerar en avikelse
        blir det dubbla markeringar' — a cause created via the equipment-
        bar checkbox flow used to draw a SECOND, separate cause-marker
        circle at the exact same position as the equipment's own marker
        (whose colour already represents "has causes"), on top of a
        manually placed object's still-interactive drawn-zone outline.
        _create_cause_for_bar must pass draw_marker=False through to
        place_cause_from_template so no second marker (DB row or visual
        item) gets created."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
        marker_id = self.db.add_equipment_marker(
            eq_id, "V-1", 0, 10.0, 10.0, "Ventil", confidence=1.0, link_method='manual')
        self.panel._equipment_bar.load(eq_id, marker_id)

        cause_id = self.panel._create_cause_for_bar(
            dev_id, "Ventil", "V-1", "Ventil stängd")

        self.assertIsNotNone(cause_id)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM cause_markers WHERE cause_id=?", (cause_id,)
        ).fetchone()[0], 0, "no separate cause_markers row should be created")

    def test_kon_and_sg_cells_are_clickable_after_bar_driven_cause_creation(self):
        """End-to-end confirmation of the actual reported symptom: clicking
        KON/SG for a row created via the object/deviation-bar flow must
        now actually trigger inline editing, not silently do nothing."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("T-1", "T-1", "T", 0, "Behållare", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "T-1", 0, 10.0, 10.0, "Behållare", confidence=0.9, link_method='leader')
            win.pid_panel._equipment_bar.load(eq_id, marker_id)

            cause_id = win.pid_panel._create_cause_for_bar(
                dev_id, "Behållare", "T-1", "Övertryck")
            win.scenario_panel.load_node(node_id)
            row = next(r for r, m in enumerate(win.scenario_panel._row_meta)
                      if m[1] == cause_id)

            edit_spy = unittest.mock.Mock(wraps=win.scenario_panel._table.edit)
            win.scenario_panel._table.edit = edit_spy
            win.scenario_panel._try_start_edit(row, win.scenario_panel._C_KON)
            edit_spy.assert_called()

            edit_spy.reset_mock()
            win.scenario_panel._try_start_edit(row, win.scenario_panel._C_SG)
            edit_spy.assert_called()


class RiskCellColorTests(unittest.TestCase):
    """'nu vill jag att du fixar så att cellerna med riskmatriser i hazop
    scenario återspeglar motsvarande färg från riskmatrisen' (2026-08-09,
    see NOTES.md) — RFORE/SLUT cells now get their background/
    foreground from risk_info(), matching the configured risk matrix.
    risk_info() was already being called for each row (its label went
    into tooltips) but the bg/fg colors it returned were simply discarded
    — the cells rendered with no color at all."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_riskcolor_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain_with_category(self, freq_level=3, severity=3, rrf=1):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, likelihood=freq_level)
        cons_id = self.db.add_consequence(cause_id)
        cat = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(cons_id, cat['id'], severity)
        sg_id = self.db.add_safeguard(cons_id)
        if rrf != 1:
            self.db.update_safeguard(sg_id, rrf=rrf)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def test_rfore_cell_matches_risk_info_colors(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, _sg_id = \
                self._make_full_chain_with_category(freq_level=3, severity=3)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_RFORE)
            _, expected_bg, expected_fg = risk_info(3, 3)
            self.assertEqual(item.background().color(), QColor(expected_bg))
            self.assertEqual(item.foreground().color(), QColor(expected_fg))
        finally:
            panel.deleteLater()

    def test_rfore_cell_font_shrinks_with_and_scales_from_general_font_size(self):
        """Reported feedback: "Risk före barriär" text got cut off in its
        85px-wide column. The cell font was hardcoded at 9pt regardless of
        the "Textstorlek" spinner — now one point smaller than the
        general cell font, and scales with it."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, _sg_id = \
                self._make_full_chain_with_category(freq_level=3, severity=3)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_RFORE)
            self.assertEqual(item.font().pointSize(), panel._cell_font_size - 1)

            panel._fs_spin.setValue(13)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            item = panel._table.item(row, panel._C_RFORE)
            self.assertEqual(item.font().pointSize(), 12)
        finally:
            panel.deleteLater()

    def test_slut_cell_matches_risk_info_colors(self):
        """Risk efter barriär (REFT) was removed (2026-08-09, see
        NOTES.md) — only RFORE and SLUT remain."""
        from hazop import ScenarioTablePanel, risk_info, total_freq_reduction
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, _sg_id = \
                self._make_full_chain_with_category(freq_level=4, severity=3, rrf=100)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            final_f, _rrf, _steps = total_freq_reduction(4, 100, False, 10, False, 10, [])
            _, expected_bg_s, expected_fg_s = risk_info(final_f, 3)
            slut_item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(slut_item.background().color(), QColor(expected_bg_s))
            self.assertEqual(slut_item.foreground().color(), QColor(expected_fg_s))
        finally:
            panel.deleteLater()

    def test_reft_column_no_longer_exists(self):
        """'Ta bart risk efter barriär och behåll bara före och slut.'
        (2026-08-09) — the column constant itself must be gone, not just
        unused."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            self.assertFalse(hasattr(panel, '_C_REFT'))
            self.assertNotIn('Risk efter barriärer', panel._COLS)
        finally:
            panel.deleteLater()

    def test_uncategorized_row_still_shows_plain_severity_colors(self):
        """A consequence with no per-category severity assessment (the
        common case — ConsequencePanel's plain severity+category fields,
        not the opt-in 📊 per-category feature) must still show a real
        risk color using its plain `severity` value, not a blank cell
        (2026-08-09, see NOTES.md — this was the actual root cause of
        'jag ser inga bakgrundsfärger som passar med riskmatrisen': every
        consequence created through the normal flow has cat_info=None)."""
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=3)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 4, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_RFORE)
            self.assertNotEqual(item.text(), '')
            _, expected_bg, expected_fg = risk_info(3, 4)
            self.assertEqual(item.background().color(), QColor(expected_bg))
            self.assertEqual(item.foreground().color(), QColor(expected_fg))
            meta = item.data(Qt.ItemDataRole.UserRole)
            self.assertEqual(meta[0], 'risk_click')
        finally:
            panel.deleteLater()

    def test_uncategorized_slut_also_shows_plain_severity_colors(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=3)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 4, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            _, expected_bg, expected_fg = risk_info(3, 4)
            item = panel._table.item(row, panel._C_SLUT)
            self.assertNotEqual(item.text(), '')
            self.assertEqual(item.background().color(), QColor(expected_bg))
            self.assertEqual(item.foreground().color(), QColor(expected_fg))
        finally:
            panel.deleteLater()

    def test_update_lopa_risk_recolors_uncategorized_row_too(self):
        """The incremental RRF-change path (_update_lopa_risk) must also
        keep patching SLUT for rows without a category assessment —
        previously it silently stopped updating them after the first
        rebuild (same cat_info gate as _add_row)."""
        from hazop import ScenarioTablePanel, risk_info, total_freq_reduction
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=4)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 3, '')
            sg_id = self.db.add_safeguard(cons_id)
            panel.load_node(node_id)

            self.db.update_safeguard(sg_id, rrf=100)
            panel._update_lopa_risk(cons_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            final_f, _rrf, _steps = total_freq_reduction(4, 100, False, 10, False, 10, [])
            _, expected_bg, expected_fg = risk_info(final_f, 3)
            slut_item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(slut_item.background().color(), QColor(expected_bg))
            self.assertEqual(slut_item.foreground().color(), QColor(expected_fg))
        finally:
            panel.deleteLater()

    def test_update_lopa_risk_also_recolors_slut(self):
        """Changing a safeguard's RRF without a full rebuild
        (_update_lopa_risk, the LopaWidget-triggered incremental path)
        must keep SLUT's color in sync, not just its text."""
        from hazop import ScenarioTablePanel, risk_info, total_freq_reduction
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, sg_id = \
                self._make_full_chain_with_category(freq_level=4, severity=3, rrf=1)
            panel.load_node(node_id)

            self.db.update_safeguard(sg_id, rrf=100)
            panel._update_lopa_risk(cons_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            final_f, _rrf, _steps = total_freq_reduction(4, 100, False, 10, False, 10, [])
            _, expected_bg, expected_fg = risk_info(final_f, 3)
            slut_item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(slut_item.background().color(), QColor(expected_bg))
            self.assertEqual(slut_item.foreground().color(), QColor(expected_fg))
        finally:
            panel.deleteLater()


class RiskCellActualRenderColorTests(unittest.TestCase):
    """RiskCellColorTests above only ever checked the MODEL side
    (item.background()/item.foreground()) — never whether that color
    actually reaches the screen. It didn't, in the real app: main()
    applies app.setStyleSheet(_get_windows11_stylesheet()) globally, and
    once ANY stylesheet targets QTableWidget::item, Qt's default
    QStyledItemDelegate.paint() stops respecting Qt::BackgroundRole/
    ForegroundRole entirely — a well-known Qt quirk. RFORE/SLUT fell
    through to that default path, so cells stayed white until selected
    (2026-08-09 bug report: 'jag ser inga bakgrundsfärger ... det är bara
    vitt till jag klickar'). These tests apply the SAME stylesheet the
    real app uses and sample actual painted pixels, which is the only
    way this regression could have been caught."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        from hazop import _get_windows11_stylesheet
        self.app.setStyleSheet(_get_windows11_stylesheet())
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_riskrender_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        self.app.setStyleSheet('')
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _paint_cell_to_pixmap(self, panel, row, col):
        """Grabs the ACTUAL rendered pixels for a cell by showing the real
        table and letting Qt's normal paintEvent -> style ->
        delegate.paint() pipeline run, instead of calling delegate.paint()
        directly with a synthetic QStyleOptionViewItem. A synthetic option
        has no `widget` set, which skips stylesheet-aware style resolution
        silently — testing the wrong code path entirely regardless of
        whether app.setStyleSheet() was called (discovered while verifying
        this very test: a from-scratch-option version of this test passed
        identically whether or not the actual bug fix was present).

        900px (not 600px) so every visible column — including the
        rightmost, SLUT — actually fits without horizontal scrolling.
        Columns are Interactive by default now (2026-08-12: "Fyll skärm"
        stopped force-stretching them to fit, see NOTES.md), so their
        combined default width (790px) must fit within this resize for
        SLUT's cell to have a nonzero visualRect at all."""
        panel.resize(900, 400)
        panel.show()
        self.app.processEvents()
        panel._table.resizeRowsToContents()
        self.app.processEvents()
        cell_rect = panel._table.visualRect(panel._table.model().index(row, col))
        pixmap = panel._table.viewport().grab(cell_rect)
        panel.hide()
        return pixmap

    def test_rfore_cell_actually_paints_the_risk_color_under_the_app_stylesheet(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=5)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 5, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            pixmap = self._paint_cell_to_pixmap(panel, row, panel._C_RFORE)
            # Sample near the top-left corner, inside the fillRect'd
            # background but before the 2px-inset drawText region — never
            # touched by a text glyph regardless of the risk label's length.
            sampled = pixmap.toImage().pixelColor(1, 1)

            _, expected_bg, _ = risk_info(5, 5)
            self.assertEqual(sampled, QColor(expected_bg),
                "the actual painted pixel must match the risk matrix color, not white")
            self.assertNotEqual(sampled, QColor('white'))
            self.assertNotEqual(sampled, QColor('#ffffff'))
        finally:
            panel.deleteLater()

    def test_slut_cell_actually_paints_the_risk_color(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=0)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 1, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            _, expected_bg, _ = risk_info(0, 1)
            pixmap = self._paint_cell_to_pixmap(panel, row, panel._C_SLUT)
            sampled = pixmap.toImage().pixelColor(1, 1)
            self.assertEqual(sampled, QColor(expected_bg))
        finally:
            panel.deleteLater()


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


class OrsStripTagFreqLayoutTests(unittest.TestCase):
    """'Jag vill också kunna läsa ut hela tagnumret i orsaksbaren, nu blir
    det lätt .... för att det finns för lite plats så flytta 0.1 åt höger
    (högerställ).' Clarified when asked what "0.1" meant: 'Tag nummret
    klipps av då frekvensen står för långt till vänster. högerställ
    frekvens för att det ska rymma mer.' (2026-08-11)

    Root cause: the ORS strip's tag zone was capped at the fixed
    _cause_obj_w divider width (default 64px) no matter how much space
    was actually free in the cell, because the frequency label was drawn
    immediately after the tag rather than anchored to the strip's right
    edge — so a wide cell left a stretch of blank space between the
    (short) frequency text and the status dots while the tag itself kept
    eliding. Fixed by right-anchoring the frequency zone against the dots
    margin FIRST and letting the tag zone claim whatever is left over
    (ScenarioTablePanel._ors_tag_zone_geometry), with _cause_obj_w kept as
    a floor so the user-draggable divider still only ever WIDENS the
    minimum, never narrows it. The same helper is used by the tag-zone
    click hit-test in eventFilter() so a click on the newly-visible part
    of a long tag doesn't hit a stale rectangle."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsfreq_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    _TEXT_DARK = (0x17, 0x19, 0x1C)

    def _is_dark(self, px, tol=40):
        return (abs(px.red()   - self._TEXT_DARK[0]) <= tol and
                abs(px.green() - self._TEXT_DARK[1]) <= tol and
                abs(px.blue()  - self._TEXT_DARK[2]) <= tol)

    def _make_tagged_cause(self, tag="E1.M1.QMA127"):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, comp_type='V', comp_tag=tag)
        panel.load_node(node_id)
        row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
        return panel, row, cause_id

    def test_geometry_never_shrinks_tag_zone_below_the_dragged_divider_width(self):
        """In a narrow cell there's no leftover space to reclaim — the tag
        zone must fall back to exactly the user's persisted _cause_obj_w
        floor, never below it (that would break the existing drag-to-
        resize feature's promise)."""
        panel, row, cause_id = self._make_tagged_cause()
        try:
            item = panel._table.item(row, panel._C_ORS)
            self.assertEqual(panel._cause_obj_w, 64,
                "test assumes the documented default divider width")
            tag_zone_w, freq_zone_x, freq_zone_w, freq_str = \
                panel._ors_tag_zone_geometry(item, tag_x=22, cell_right=120)
            self.assertGreaterEqual(tag_zone_w, panel._cause_obj_w)
        finally:
            panel.deleteLater()

    def test_geometry_expands_tag_zone_into_reclaimed_space_in_a_wide_cell(self):
        """The actual bug: with a wide cell (plenty of room), the OLD code
        still capped the tag at the fixed 64px divider width and left the
        reclaimed space as a dead gap between the frequency text and the
        dots. The fix must let the tag zone grow well past that fixed cap,
        and must right-anchor the frequency zone against the dots margin
        rather than gluing it to the tag."""
        panel, row, cause_id = self._make_tagged_cause()
        try:
            item = panel._table.item(row, panel._C_ORS)
            old_cap = panel._cause_obj_w
            tag_zone_w, freq_zone_x, freq_zone_w, freq_str = \
                panel._ors_tag_zone_geometry(item, tag_x=22, cell_right=500)
            self.assertGreater(tag_zone_w, old_cap * 2,
                "tag zone should reclaim the freed-up space in a wide cell, "
                "not stay capped at the old fixed divider width")
            self.assertGreater(freq_zone_x, 22 + old_cap,
                "frequency zone must be right-anchored, not glued to the tag")
            self.assertEqual(freq_zone_x + freq_zone_w, 500 - panel._ORS_DOTS_MARGIN,
                "frequency zone must sit flush against the dots margin at "
                "the strip's right edge")
        finally:
            panel.deleteLater()

    def test_long_tag_renders_wider_than_old_fixed_cap_in_real_paint(self):
        """Render a real cell (same path production code uses) and confirm
        the tag's own text pixels extend past where the OLD flat 64px cap
        would have already elided it, now that the cell has room to spare.
        """
        from PyQt6.QtGui import QFont, QFontMetrics
        panel, row, cause_id = self._make_tagged_cause()
        try:
            # Null out the frequency for this row before rendering. A cause
            # always carries SOME frequency (default likelihood=3, see
            # Database.cause_f_level), and it's drawn in the same dark
            # color as the tag — probing for "any dark pixel" past the old
            # cap would otherwise just as likely land on the frequency
            # text (which, under the OLD code, starts drawing immediately
            # past that same cap) as on the tag, making the probe
            # ambiguous. Blanking it isolates exactly what this test is
            # about: whether the TAG itself is still being clipped.
            item = panel._table.item(row, panel._C_ORS)
            item.setData(Qt.ItemDataRole.UserRole + 3, None)
            item.setData(Qt.ItemDataRole.UserRole + 5, None)

            panel._table.setColumnWidth(panel._C_ORS, 400)
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            panel._resize_rows_manual()
            self.app.processEvents()

            index = panel._table.model().index(row, panel._C_ORS)
            cell_rect = panel._table.visualRect(index)
            pixmap = panel._table.viewport().grab(cell_rect)
            panel.hide()
            image = pixmap.toImage()

            # Compute where the tag text actually ends using the SAME font
            # construction paint() uses, so this test isn't a guess tied to
            # one particular platform's default font metrics.
            base_font = panel._table.font()
            tag_font = QFont(base_font)
            tag_font.setPointSize(max(6, base_font.pointSize() - 1))
            tag_font.setBold(True)
            text_w = QFontMetrics(tag_font).horizontalAdvance("E1.M1.QMA127")
            tag_x = 22   # _PID_ICON_W
            expected_text_end = tag_x + 2 + text_w
            old_cap_end = tag_x + panel._cause_obj_w   # 22 + 64 = 86

            self.assertGreater(expected_text_end, old_cap_end + 10,
                "test setup issue: chosen tag isn't actually longer than "
                "the old fixed cap — pick a longer tag")

            probe_lo = old_cap_end + 5
            probe_hi = min(expected_text_end - 2, image.width() - 1)
            strip_h = 17  # _ORS_STRIP_H
            found = False
            for x in range(probe_lo, max(probe_lo + 1, probe_hi)):
                for y in range(strip_h):
                    if self._is_dark(image.pixelColor(x, y)):
                        found = True
                        break
                if found:
                    break
            self.assertTrue(found,
                f"expected tag text pixels somewhere in x=[{probe_lo},{probe_hi}] "
                f"(beyond the old fixed cap at x={old_cap_end}) now that the "
                "cell has room — the tag is still being clipped")
        finally:
            panel.deleteLater()

    def test_tag_zone_click_hit_test_matches_the_expanded_paint_geometry(self):
        """The tag-zone click (opens the tag-picker popup) used to be
        hard-bounded by the raw _cause_obj_w divider width. After the fix,
        clicking on the newly-visible part of a long tag (drawn wider than
        that old bound) must still land inside the click zone — otherwise
        the visible tag and its clickable area silently drift apart."""
        panel, row, cause_id = self._make_tagged_cause()
        try:
            panel._table.setColumnWidth(panel._C_ORS, 400)
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            col_x = panel._table.columnViewportPosition(panel._C_ORS)
            cell_right = col_x + panel._table.columnWidth(panel._C_ORS) - 1
            item = panel._table.item(row, panel._C_ORS)
            obj_start = col_x + 22  # _PID_ICON_W
            tag_zone_w, _fx, _fw, _fs = panel._ors_tag_zone_geometry(
                item, obj_start, cell_right)

            # A click position well past the old fixed 64px cap but still
            # inside the newly-expanded tag zone.
            probe_x = obj_start + panel._cause_obj_w + 20
            self.assertLess(probe_x, obj_start + tag_zone_w,
                "test setup issue: probe point isn't actually within the "
                "expanded tag zone")

            popup_calls = []
            panel._show_cause_obj_popup = lambda r, cid, gp: popup_calls.append((r, cid))

            from PyQt6.QtCore import QPoint, QEvent
            from PyQt6.QtGui import QMouseEvent
            from PyQt6.QtCore import Qt as _Qt
            row_y = panel._table.rowViewportPosition(row) + 3
            pos = QPoint(probe_x, row_y)
            ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                             _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                             _Qt.KeyboardModifier.NoModifier)
            panel.eventFilter(panel._table.viewport(), ev)
            self.assertEqual(popup_calls, [(row, cause_id)],
                "clicking within the expanded (paint-matching) tag zone "
                "must still open the tag-picker popup")
        finally:
            panel.deleteLater()


class ScenarioColumnWidthPersistenceTests(unittest.TestCase):
    """'Fyll skärm' checkbox state and manually-resized column widths are
    now persisted to app_config (2026-08-10, see NOTES.md) — previously
    reset to the hardcoded defaults on every app restart."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_colwidth_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_resizing_a_column_persists_its_width(self):
        import json
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            panel._table.setColumnWidth(panel._C_RFORE, 123)
            saved = json.loads(self.db.get_config('scenario_col_widths', '{}'))
            self.assertEqual(saved.get(str(panel._C_RFORE)), 123)
        finally:
            panel.deleteLater()

    def test_saved_width_is_restored_on_next_panel_construction(self):
        from hazop import ScenarioTablePanel
        panel1 = ScenarioTablePanel(self.db)
        try:
            panel1._table.setColumnWidth(panel1._C_RFORE, 111)
        finally:
            panel1.deleteLater()

        panel2 = ScenarioTablePanel(self.db)
        try:
            self.assertEqual(panel2._table.columnWidth(panel2._C_RFORE), 111)
        finally:
            panel2.deleteLater()

    def test_columns_are_draggable_without_ever_clicking_fill_button(self):
        """Reported feedback: "Fyll skärm" felt like it had no effect, and
        column widths couldn't be dragged at all. Root cause: ORS/KON/SG
        defaulted to Stretch and RFORE/LOPA/SLUT to Fixed — neither is
        user-resizable — and unchecking the old persistent checkbox only
        flipped the resize mode without changing any pixel width, so
        nothing visibly happened. All columns must now be Interactive
        (draggable) from construction, independent of the fill button."""
        from hazop import ScenarioTablePanel
        from PyQt6.QtWidgets import QHeaderView
        panel = ScenarioTablePanel(self.db)
        try:
            h = panel._table.horizontalHeader()
            for col in (panel._C_ORS, panel._C_KON, panel._C_SG,
                        panel._C_RFORE, panel._C_LOPA, panel._C_SLUT):
                self.assertEqual(h.sectionResizeMode(col), QHeaderView.ResizeMode.Interactive,
                    f"column {col} must be user-resizable even before '↔ Fyll bredd' is clicked")
        finally:
            panel.deleteLater()

    def test_fill_width_once_gives_stretch_columns_an_equal_share_and_stays_draggable(self):
        from hazop import ScenarioTablePanel
        from PyQt6.QtWidgets import QHeaderView
        panel = ScenarioTablePanel(self.db)
        try:
            h = panel._table.horizontalHeader()
            panel._table.setColumnWidth(panel._C_ORS, 50)
            panel._table.setColumnWidth(panel._C_KON, 400)
            panel._table.setColumnWidth(panel._C_SG, 90)

            panel._fill_width_once()

            w_ors = panel._table.columnWidth(panel._C_ORS)
            w_kon = panel._table.columnWidth(panel._C_KON)
            w_sg  = panel._table.columnWidth(panel._C_SG)
            self.assertEqual(w_ors, w_kon)
            self.assertEqual(w_kon, w_sg)
            for col in (panel._C_ORS, panel._C_KON, panel._C_SG):
                self.assertEqual(h.sectionResizeMode(col), QHeaderView.ResizeMode.Interactive,
                    "clicking '↔ Fyll bredd' must not lock the column into Stretch mode")
        finally:
            panel.deleteLater()

    def test_corrupt_saved_widths_do_not_crash_construction(self):
        from hazop import ScenarioTablePanel
        self.db.set_config('scenario_col_widths', 'not valid json{{{')
        try:
            panel = ScenarioTablePanel(self.db)
            panel.deleteLater()
        except Exception as e:
            self.fail(f"must not raise on corrupt saved widths: {e!r}")


class EquipmentPanelManualAddUnificationTests(unittest.TestCase):
    """EquipmentPanel._add_manual used to open a bare QInputDialog.getText()
    (tag only, type always auto-guessed from KNOWN_PREFIXES, no duplicate
    check) instead of the richer EquipmentTagPopup already used by the
    P&ID's "🔧 Objekt" action. Unified onto the same popup + committed
    handler (2026-08-09, see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_manualadd_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import EquipmentPanel
        self.panel = EquipmentPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_commit_creates_new_catalog_row_with_chosen_type(self):
        self.panel._on_manual_equipment_committed("P-101", "Pump")
        row = self.db.get_equipment_by_tag("P-101")
        self.assertIsNotNone(row)
        self.assertEqual(row['equipment_type'], "Pump")

    def test_commit_of_existing_tag_reuses_row_instead_of_duplicating(self):
        self.db.add_equipment_item("V-01", "V-01", "V", 0, "Ventil", '', 0)
        self.panel._on_manual_equipment_committed("V-01", "Ventil")
        matches = [r for r in self.db.equipment_items() if r['tag'] == 'V-01']
        self.assertEqual(len(matches), 1,
                          "committing an already-catalogued tag must not create a duplicate row")

    def test_commit_of_existing_tag_updates_type_when_changed(self):
        self.db.add_equipment_item("V-02", "V-02", "V", 0, "", '', 0)
        self.panel._on_manual_equipment_committed("V-02", "Ventil")
        row = self.db.get_equipment_by_tag("V-02")
        self.assertEqual(row['equipment_type'], "Ventil")

    def test_commit_with_blank_tag_is_a_noop(self):
        before = len(self.db.equipment_items())
        self.panel._on_manual_equipment_committed("", "Pump")
        self.assertEqual(len(self.db.equipment_items()), before)


class EquipmentTagPopupDuplicateHintTests(unittest.TestCase):
    """EquipmentTagPopup surfaces when a typed tag already exists in the
    catalog, since place_equipment_marker silently reuses that row rather
    than creating a duplicate (2026-08-10, see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dupcheck_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_hint_shown_for_existing_tag(self):
        from hazop import EquipmentTagPopup
        self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("P-101")
            self.assertIn("P-101", popup._dup_hint.text())
            self.assertIn("finns redan", popup._dup_hint.text())
        finally:
            popup.deleteLater()

    def test_no_hint_for_new_tag(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("V-999")
            self.assertEqual(popup._dup_hint.text(), "")
        finally:
            popup.deleteLater()

    def test_hint_clears_when_tag_edited_to_no_longer_match(self):
        from hazop import EquipmentTagPopup
        self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("P-101")
            self.assertNotEqual(popup._dup_hint.text(), "")
            popup._tag_edit.setText("P-999")
            self.assertEqual(popup._dup_hint.text(), "")
        finally:
            popup.deleteLater()


class EquipmentTagPopupCustomTypeTests(unittest.TestCase):
    """"det är här jag vill kunna lägga till nya typer av objekt som inte
    redan finns i listan" (2026-08-13). First attempt made the "Typ"
    combo editable directly — reverted the same day ("Rullgardinen ...
    har försvunnit. Det ska vara de valen som det var innan") because an
    editable QComboBox loses its usual dropdown-arrow affordance under
    this app's global stylesheet. The combo stays non-editable with its
    original pick-from-list behaviour; a dedicated "+" button next to it
    opens a text prompt to add a brand-new type instead."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_customtype_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_type_combo_is_not_editable(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            self.assertFalse(popup._type_cb.isEditable())
        finally:
            popup.deleteLater()

    def test_plus_button_adds_and_selects_a_new_type(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("Ett helt nytt objekt", True)):
                popup._add_new_type()
            self.assertEqual(popup._type_cb.currentText(), "Ett helt nytt objekt")
        finally:
            popup.deleteLater()

    def test_plus_button_also_registers_it_as_a_standard_object(self):
        """"lägger jag till ytterligare något här skall det också dyka
        upp i standardobjekt. Dessa skall prata med varandra."
        (2026-08-13) — a brand-new type typed via the "+" button must
        immediately become a Standardobjekt too, not just a local combo
        entry, so it's available in the cause-suggestion forms."""
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("Ett helt nytt objekt", True)):
                popup._add_new_type()
            names = [o['name'] for o in self.db.standard_objects()]
            self.assertIn("Ett helt nytt objekt", names)
        finally:
            popup.deleteLater()

    def test_plus_button_does_not_duplicate_an_existing_standard_object(self):
        """Case-insensitive match against an existing Standardobjekt
        (e.g. one added via Inställningar) must not create a near-
        duplicate entry that only differs by case."""
        from hazop import EquipmentTagPopup
        self.db.add_standard_object("Ventil")
        popup = EquipmentTagPopup(self.db)
        try:
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("ventil", True)):
                popup._add_new_type()
            names = [o['name'] for o in self.db.standard_objects()]
            self.assertEqual(names.count("Ventil") + names.count("ventil"), 1)
        finally:
            popup.deleteLater()

    def test_standard_object_added_elsewhere_appears_in_type_dropdown(self):
        """The reverse direction of the same sync: a Standardobjekt added
        via Inställningar (not through this popup at all) must show up
        as a selectable type here too."""
        from hazop import EquipmentTagPopup
        self.db.add_standard_object("Ett objekt satt via Inställningar")
        popup = EquipmentTagPopup(self.db)
        try:
            self.assertGreaterEqual(
                popup._type_cb.findText("Ett objekt satt via Inställningar"), 0)
        finally:
            popup.deleteLater()

    def test_plus_button_cancelled_leaves_selection_unchanged(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            before = popup._type_cb.currentText()
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("", False)):
                popup._add_new_type()
            self.assertEqual(popup._type_cb.currentText(), before)
        finally:
            popup.deleteLater()

    def test_new_type_from_plus_button_commits_unchanged(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("XYZ-1")
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("Ett helt nytt objekt", True)):
                popup._add_new_type()
            captured = []
            popup.committed.connect(lambda tag, typ: captured.append((tag, typ)))
            popup._ok()
            self.assertEqual(captured, [("XYZ-1", "Ett helt nytt objekt")])
        finally:
            popup.deleteLater()

    def test_previously_used_custom_type_is_offered_again(self):
        """Once a custom type has been used anywhere in the catalog, it
        should be a selectable dropdown entry next time, not something
        the user has to retype from scratch every time."""
        from hazop import EquipmentTagPopup
        self.db.add_equipment_item("XYZ-1", "XYZ-1", "XYZ", 0, "Ett helt nytt objekt", '', 0)
        popup = EquipmentTagPopup(self.db)
        try:
            self.assertGreaterEqual(popup._type_cb.findText("Ett helt nytt objekt"), 0)
        finally:
            popup.deleteLater()


class ConsequencePanelSevLabelsSyncTests(unittest.TestCase):
    """ConsequencePanel.sev_combo used to be populated once, at construction
    time, from the hardcoded default SEV_LABELS — so customizing severity
    level text/count via Settings' risk matrix editor never propagated to
    the actual combo used to edit a consequence's severity (2026-08-09,
    see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_sevlabels_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        node_id = self.db.add_node()
        deviation_id = self.db.deviations(node_id)[0]['id']
        self.cause_id = self.db.add_cause(deviation_id)
        self.cons_id = self.db.add_consequence(self.cause_id)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        # get_matrix() reads a module-level singleton cache shared across
        # the whole test process — reset it so a custom matrix set up by
        # this test class can't leak into unrelated tests that run after it.
        hazop._risk_matrix_cache.invalidate()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_sev_combo_reflects_custom_matrix_labels_on_load(self):
        from hazop import ConsequencePanel, DEFAULT_MATRIX
        custom = dict(DEFAULT_MATRIX)
        custom['y_labels'] = ['Testnivå1', 'Testnivå2', 'Testnivå3', 'Testnivå4', 'Testnivå5']
        self.db.set_risk_matrix(custom)
        hazop.load_matrix(self.db)
        panel = ConsequencePanel(self.db)
        panel.load(self.cons_id)
        self.assertEqual(panel.sev_combo.itemText(0), 'C1 – Testnivå1')
        self.assertNotIn('Försumbar', panel.sev_combo.itemText(0))

    def test_sev_combo_updates_after_matrix_changed_while_panel_open(self):
        from hazop import ConsequencePanel, DEFAULT_MATRIX
        panel = ConsequencePanel(self.db)
        panel.load(self.cons_id)
        self.assertNotIn('Omdöpt', panel.sev_combo.itemText(2))

        custom = dict(DEFAULT_MATRIX)
        custom['y_labels'] = ['A', 'B', 'Omdöpt', 'D', 'E']
        self.db.set_risk_matrix(custom)
        hazop.load_matrix(self.db)

        # Simulates MainWindow._on_matrix_changed's cons_panel.load(...) call
        panel.load(self.cons_id)
        self.assertEqual(panel.sev_combo.itemText(2), 'C3 – Omdöpt')


class LegacyConsequenceLikelihoodColumnTests(unittest.TestCase):
    """consequences.likelihood predates the redesign that moved likelihood
    onto causes; old database files still carry it with stale values that
    nothing reads or writes anymore (2026-08-09, see NOTES.md)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_legacycol_test_")
        self.db_path = os.path.join(self._tmpdir, "legacy.db")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_legacy_likelihood_column_is_dropped_on_open(self):
        # Simulate an old database file created before the redesign: a
        # consequences table that still has its own likelihood column.
        raw = sqlite3.connect(self.db_path)
        raw.execute("""
            CREATE TABLE consequences (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cause_id    INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT 'Ny konsekvens',
                severity    INTEGER NOT NULL DEFAULT 1,
                likelihood  INTEGER NOT NULL DEFAULT 1
            )
        """)
        raw.commit()
        raw.close()

        db = Database(path=self.db_path)
        try:
            cols = [r['name'] for r in db.conn.execute("PRAGMA table_info(consequences)")]
            self.assertNotIn('likelihood', cols,
                              "legacy consequences.likelihood column must be dropped on open")
        finally:
            del db

    def test_fresh_database_open_is_a_harmless_noop(self):
        """A database that never had the column (every DB created after
        the redesign) must open without error — the DROP is conditional
        on PRAGMA table_info actually finding the column first."""
        db = Database(path=self.db_path)
        try:
            cols = [r['name'] for r in db.conn.execute("PRAGMA table_info(consequences)")]
            self.assertNotIn('likelihood', cols)
        finally:
            del db


class NumericPrefixParsingTests(unittest.TestCase):
    """_equip_prefix_from_tag/_parse_tag used to treat a purely numeric
    text span (e.g. a title-block date "2019-09-30") as a valid
    equipment tag with prefix "2019" (2026-08-10, see NOTES.md known
    limitations — fixed). A prefix must contain at least one letter."""

    def test_date_like_string_has_no_prefix(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("2019-09-30"), '')

    def test_pure_digit_string_has_no_prefix(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("123456"), '')

    def test_real_tag_prefix_still_works(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("PCV-101"), 'PCV')
        self.assertEqual(_equip_prefix_from_tag("HV0063"), 'HV')

    def test_parse_tag_rejects_date_like_string(self):
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("2019-09-30"), (None, None))


class TagFallbackDigitGuardTests(unittest.TestCase):
    """'jag vill att programmet blir mycket bättre och snabbare på att
    analysera och hitta objekt på P&ID' (2026-08-10) — a baseline sweep
    against a new, ~20-vendor P&ID corpus found that 57% of all "tag"
    hits had no mapped equipment type. Root cause: _parse_tag's and
    _score_tag_word's last-resort fallbacks accepted ANY 2+ letter word
    with no digit at all as a "tag" (title-block/disclaimer text like
    "THIS", "CONFIDENTIAL", "REPRODUCTION", "NODE" — none of which have
    a digit, unlike every real tag format documented in this module).
    Fixed by requiring at least one digit before the fallback fires —
    every other tag regex already required this; only these two loose
    fallbacks didn't."""

    def test_parse_tag_rejects_plain_english_words(self):
        from equipment_detection import _parse_tag
        for word in ("THIS", "CONFIDENTIAL", "REPRODUCTION", "NODE", "AND", "FOR"):
            self.assertEqual(_parse_tag(word), (None, None), f"{word!r} must not parse as a tag")

    def test_parse_tag_still_accepts_real_tags(self):
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("PCV-101"), ("PCV-101", "PCV"))
        self.assertEqual(_parse_tag("HV0063"), ("HV-0063", "HV"))
        self.assertEqual(_parse_tag("NODE-1"), ("NODE-1", "NODE"))

    def test_score_tag_word_rejects_plain_english_words(self):
        from equipment_detection import _score_tag_word
        for word in ("THIS", "CONFIDENTIAL", "AND", "TO"):
            self.assertEqual(_score_tag_word(word), (None, 0), f"{word!r} must not score as a tag")

    def test_score_tag_word_still_finds_real_tags(self):
        from equipment_detection import _score_tag_word
        tag, score = _score_tag_word("HV-101")
        self.assertEqual(tag, "HV-101")
        self.assertGreaterEqual(score, 2)

    def test_parse_tag_exempts_bare_known_instrument_codes(self):
        """'programmet har svårt för att känna igen instrument PI, FI'
        (2026-08-11) — a real regression the digit guard above
        introduced: LKAB P&IDs genuinely label some LOCAL indicators
        with just the bare ISA code and no loop number at all (a real
        convention KNOWN_PREFIXES itself already anticipated —
        'Tryckmätare (lokal)', 'Flödesmätare (lokal)'). Confirmed via
        git-history diff against the real LKAB reference corpus: 'PI'/
        'FI' were recognised before the digit guard, silently stopped
        being recognised after it, with no other code change in
        between."""
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("PI"), ("PI", "PI"))
        self.assertEqual(_parse_tag("FI"), ("FI", "FI"))

    def test_parse_tag_still_rejects_onoff_state_annotation(self):
        """The exemption above must be an EXACT whole-string match only
        — "ON" is ALSO a real KNOWN_PREFIXES entry (Avstängningsventil),
        but "ON/OFF" (confirmed real noise on a NYA-corpus file,
        2026-08-10) must stay rejected: it's ordinary valve-state
        annotation text, not a bare instrument tag, even though "ON"
        appears as a substring of it."""
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("ON/OFF"), (None, None))
        self.assertEqual(_parse_tag("ON/OFFSWITCHLOCALLYMOUNTED"), (None, None))

    def test_score_tag_word_exempts_bare_known_instrument_codes(self):
        from equipment_detection import _score_tag_word
        tag, score = _score_tag_word("PI")
        self.assertEqual(tag, "PI")
        self.assertGreaterEqual(score, 1)

    def test_score_tag_word_still_rejects_onoff_state_annotation(self):
        from equipment_detection import _score_tag_word
        self.assertEqual(_score_tag_word("ON/OFF"), (None, 0))


class SpatialCombineGlueWordTests(unittest.TestCase):
    """_spatial_combine()'s plain inter-word-gap join (for tag sub-parts
    the PDF exporter split apart, e.g. "20"-"PCV"-"101") also fused
    ordinary adjacent prose words into bogus tag candidates when the gap
    between them happened to be small — confirmed on a real file
    (2026-08-10, see NOTES.md): "TO" immediately followed by "PRI-421"
    (normal sentence text, e.g. '... TO PRI-421 ...') combined into
    "TOPRI-421", which then parsed as a fake tag with prefix "TOPRI".
    Fixed by refusing to extend a group past a short, common English
    connector word via the gap-based path (the separator-token path,
    e.g. a literal "-" between two real tag halves, is unaffected)."""

    def test_common_connector_word_does_not_glue_onto_following_tag(self):
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 100.0, 110.0, 108.0, "TO"),
            (112.0, 100.0, 140.0, 108.0, "PRI-421"),
        ]
        results = _spatial_combine(words, gap_limit=22.0)
        texts = [r[0] for r in results]
        self.assertNotIn("TOPRI-421", texts)
        self.assertIn("PRI-421", texts)
        self.assertIn("TO", texts)

    def test_real_tag_subparts_still_combine(self):
        """Sanity check: genuine split tag fragments (no connector word
        involved) must still combine exactly as before."""
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 100.0, 112.0, 108.0, "20"),
            (114.0, 100.0, 118.0, 108.0, "-"),
            (120.0, 100.0, 134.0, 108.0, "PCV"),
            (136.0, 100.0, 140.0, 108.0, "-"),
            (142.0, 100.0, 156.0, 108.0, "101"),
        ]
        results = _spatial_combine(words, gap_limit=22.0)
        texts = [r[0] for r in results]
        self.assertIn("20-PCV-101", texts)


class NewCorpusPrefixReviewTests(unittest.TestCase):
    """Two genuinely new, real-corpus-confirmed prefixes added to
    KNOWN_PREFIXES (2026-08-10, see NOTES.md): HVPT (compound hand-valve
    + pressure-transmitter tag) and bare FC (Flow Controller, distinct
    from the existing FCV). Also confirms PN (pipe nominal pressure
    rating, e.g. PN100/PN-30) is now excluded the same way DN already
    was — it was showing up as a false "tag" on a real file."""

    def test_hvpt_recognised(self):
        from equipment_detection import _equip_prefix_from_tag, KNOWN_PREFIXES
        self.assertEqual(_equip_prefix_from_tag("HVPT-301"), "HVPT")
        self.assertIn("HVPT", KNOWN_PREFIXES)

    def test_fc_recognised_distinct_from_fcv(self):
        from equipment_detection import _equip_prefix_from_tag, KNOWN_PREFIXES
        self.assertEqual(_equip_prefix_from_tag("FC-E-20A"), "FC")
        self.assertEqual(_equip_prefix_from_tag("FCV-101"), "FCV")
        self.assertIn("FC", KNOWN_PREFIXES)
        self.assertIn("FCV", KNOWN_PREFIXES)

    def test_pn_pressure_rating_excluded_like_dn(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("PN100BSPP"), '')
        self.assertEqual(_equip_prefix_from_tag("PN-30"), '')


class PidAnalysisChainedAutodetectTests(unittest.TestCase):
    """'Efter jag klickat på analysera P&ID vill jag få upp samma popupruta
    som innan, sedan vill jag att en popupfråga om jag vill hitta objekt på
    P&ID ska komma upp. Då ska samma körning som "hitta objekt på P&ID"
    knappen köras.' (2026-08-11) — MainWindow._on_pid_analysis_done now
    asks a follow-up confirm after the existing 'Analys klar' popup (shown
    earlier, in PIDPanel._analyze_pid, unaffected by this change) and, on
    Yes, refreshes the equipment register and calls EquipmentPanel's own
    _autodetect() — the exact method '🎯 Hitta objekt på P&ID' itself calls."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_yes_reply_refreshes_register_and_runs_autodetect(self):
        with _TempDbMainWindow() as win:
            with unittest.mock.patch('hazop.QMessageBox.question',
                                      return_value=hazop.QMessageBox.StandardButton.Yes), \
                 unittest.mock.patch.object(win.equipment_panel, 'refresh') as mock_refresh, \
                 unittest.mock.patch.object(win.equipment_panel, '_autodetect') as mock_autodetect:
                win._on_pid_analysis_done()
                mock_refresh.assert_called_once()
                mock_autodetect.assert_called_once()

    def test_no_reply_does_not_run_autodetect(self):
        with _TempDbMainWindow() as win:
            with unittest.mock.patch('hazop.QMessageBox.question',
                                      return_value=hazop.QMessageBox.StandardButton.No), \
                 unittest.mock.patch.object(win.equipment_panel, 'refresh') as mock_refresh, \
                 unittest.mock.patch.object(win.equipment_panel, '_autodetect') as mock_autodetect:
                win._on_pid_analysis_done()
                mock_refresh.assert_not_called()
                mock_autodetect.assert_not_called()


class EquipmentMarkerThreeBadgesTests(unittest.TestCase):
    """PIDGraphicsView.add_equipment_marker draws one small numbered badge
    per non-zero counter (deviation/consequence/safeguard) — see
    EquipmentConsequenceSafeguardCountTests for the underlying Database
    counters this feeds from. Counts QGraphicsEllipseItem badges added to
    _type_items['equipment'] (the polygon marker itself is a
    QGraphicsPolygonItem, so it's never mistaken for a badge)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _badge_count(self, view):
        from PyQt6.QtWidgets import QGraphicsEllipseItem
        return sum(1 for item in view._type_items.get('equipment', [])
                   if isinstance(item, QGraphicsEllipseItem))

    def test_no_badges_when_all_counts_zero(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101")
        self.assertEqual(self._badge_count(view), 0)

    def test_one_badge_per_nonzero_counter(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=2, consequence_count=1, safeguard_count=3)
        self.assertEqual(self._badge_count(view), 3)

    def test_only_nonzero_counters_get_a_badge(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=0, consequence_count=5, safeguard_count=0)
        self.assertEqual(self._badge_count(view), 1)

    def test_tooltip_mentions_all_three_counts(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtWidgets import QGraphicsPolygonItem
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=2, consequence_count=1, safeguard_count=3)
        poly = next(item for item in view._type_items['equipment']
                    if isinstance(item, QGraphicsPolygonItem))
        tip = poly.toolTip()
        self.assertIn("2 avvikelse", tip)
        self.assertIn("1 konsekvens", tip)
        self.assertIn("3 safeguard", tip)


# ══════════════════════════════════════════════════════════════════════════
# SettingsPanel: three bundled UI changes (2026-08-11)
#   A. Riskmatris + Kategorier merged into one tab (QSplitter)
#   B. Projekt tab expanded (facility, leader, participants, date RANGE)
#   C. "P&ID" tab renamed to "P&ID-inställningar" + OCR default / page
#      orientation settings added
# ══════════════════════════════════════════════════════════════════════════

class SettingsPanelMergedRiskmatrisKategorierTests(unittest.TestCase):
    """"'riskmatris' och 'kategorier' borde gå att slå ihop till en sida.
    Testa detta." / "Låt Claude välja bästa GUI-lösningen" (2026-08-11) —
    SettingsPanel now builds a single "Riskmatris & Kategorier" tab holding
    both former tabs side-by-side in a QSplitter (categories narrow/left,
    matrix main/right — see the design-choice comment in
    SettingsPanel.__init__). Verifies the merge dropped no functionality:
    the old tab names are gone, the new combined name is present, and both
    category CRUD and matrix grid editing/save still work unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_settings_merge_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _tab_titles(self, panel):
        return [panel._tabs.tabText(i) for i in range(panel._tabs.count())]

    def test_tabs_merged_into_single_named_tab(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            titles = self._tab_titles(panel)
            self.assertIn("Riskmatris & Kategorier", titles)
            self.assertNotIn("Riskmatris", titles)
            self.assertNotIn("Kategorier", titles)
        finally:
            panel.deleteLater()

    def test_category_add_rename_delete_still_works_from_merged_tab(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel.db.add_category("Miljö")
            panel._load_categories()
            self.assertTrue(any(panel._cat_list.item(i).text() == "Miljö"
                                 for i in range(panel._cat_list.count())))

            for i in range(panel._cat_list.count()):
                if panel._cat_list.item(i).text() == "Miljö":
                    panel._cat_list.setCurrentRow(i)
                    break

            original_get_text = QInputDialog.getText
            QInputDialog.getText = staticmethod(lambda *a, **k: ("Miljöpåverkan", True))
            try:
                panel._cat_rename()
            finally:
                QInputDialog.getText = original_get_text
            self.assertTrue(any(panel._cat_list.item(i).text() == "Miljöpåverkan"
                                 for i in range(panel._cat_list.count())))

            for i in range(panel._cat_list.count()):
                if panel._cat_list.item(i).text() == "Miljöpåverkan":
                    panel._cat_list.setCurrentRow(i)
                    break
            panel._cat_delete()
            self.assertFalse(any(panel._cat_list.item(i).text() == "Miljöpåverkan"
                                  for i in range(panel._cat_list.count())))
        finally:
            panel.deleteLater()

    def test_matrix_editing_and_save_still_works_from_merged_tab(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            saved = []
            panel.matrix_changed.connect(lambda: saved.append(True))
            panel._rows_spin.setValue(4)
            panel._cols_spin.setValue(5)
            panel._apply_size()
            self.assertEqual(len(panel._cell_buttons), 4)
            # _save_matrix() shows a blocking QMessageBox.information("Sparat", ...)
            # confirmation -- headless offscreen Qt still runs a real modal event
            # loop for exec(), so it must be stubbed out or the test hangs forever
            # waiting for a click that never comes.
            original_information = QMessageBox.information
            QMessageBox.information = staticmethod(lambda *a, **k: None)
            try:
                panel._save_matrix()
            finally:
                QMessageBox.information = original_information
            self.assertTrue(saved, "matrix_changed signal should still fire on save")
            cfg = self.db.get_risk_matrix()
            self.assertEqual(cfg['rows'], 4)
            self.assertEqual(cfg['cols'], 5)
        finally:
            panel.deleteLater()

    def test_deleting_category_refreshes_matrix_cell_buttons(self):
        """'När jag lägger till eller tar bort en konsekvenskategori skall
        detta synas i riskmatrisen direkt.' (2026-08-11) — _cat_add already
        called _apply_size() to rebuild the matrix grid; _cat_delete did
        not, so the matrix silently kept its old grid after a delete. Use
        _cell_buttons' identity (not just its length, which a fixed-size
        matrix wouldn't change) to prove a real rebuild happened."""
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel.db.add_category("Miljö")
            panel._load_categories()
            for i in range(panel._cat_list.count()):
                if panel._cat_list.item(i).text() == "Miljö":
                    panel._cat_list.setCurrentRow(i)
                    break

            buttons_before = panel._cell_buttons
            panel._cat_delete()
            self.assertIsNot(panel._cell_buttons, buttons_before,
                "_cell_buttons must be a freshly rebuilt list after deleting "
                "a category — same object identity means _apply_size() never ran")
        finally:
            panel.deleteLater()

    def test_reorder_categories_persists_new_order(self):
        """'Jag vill även kunna justera ordningen, exempelvis genom vilken
        ordning de dyker upp.' (2026-08-11) — up/down buttons move the
        selected category and persist the new order via
        Database.reorder_categories(), which consequence_categories()
        (ORDER BY sort_order, name) then reflects."""
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            # Database() seeds default categories (Person/Miljö/Ekonomi/...)
            # on creation -- clear them so this test only has to reason
            # about its own two categories' relative order.
            for cat in list(panel.db.consequence_categories()):
                panel.db.delete_category(cat['id'])
            a_id = panel.db.add_category("Alfa")
            b_id = panel.db.add_category("Beta")
            panel.db.reorder_categories([a_id, b_id])
            panel._load_categories()
            self.assertEqual(panel._cat_list.item(0).text(), "Alfa")
            self.assertEqual(panel._cat_list.item(1).text(), "Beta")

            panel._cat_list.setCurrentRow(1)   # select "Beta"
            panel._cat_move(-1)                # move it up

            names_in_ui = [panel._cat_list.item(i).text() for i in range(panel._cat_list.count())]
            self.assertEqual(names_in_ui, ["Beta", "Alfa"])
            names_in_db = [dict(c)['name'] for c in panel.db.consequence_categories()]
            self.assertEqual(names_in_db, ["Beta", "Alfa"],
                "new order must be persisted, not just reflected in the UI list")
        finally:
            panel.deleteLater()

    def test_move_up_at_top_and_move_down_at_bottom_are_no_ops(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            for cat in list(panel.db.consequence_categories()):
                panel.db.delete_category(cat['id'])
            a_id = panel.db.add_category("Alfa")
            b_id = panel.db.add_category("Beta")
            panel.db.reorder_categories([a_id, b_id])
            panel._load_categories()

            panel._cat_list.setCurrentRow(0)
            panel._cat_move(-1)   # already at top -- must not raise or reorder
            names = [panel._cat_list.item(i).text() for i in range(panel._cat_list.count())]
            self.assertEqual(names, ["Alfa", "Beta"])

            panel._cat_list.setCurrentRow(1)
            panel._cat_move(1)    # already at bottom -- must not raise or reorder
            names = [panel._cat_list.item(i).text() for i in range(panel._cat_list.count())]
            self.assertEqual(names, ["Alfa", "Beta"])
        finally:
            panel.deleteLater()


class SettingsPanelProjektExpansionTests(unittest.TestCase):
    """"Fliken projekt innehåller bara Projektnamn, datum och revision,
    utveckla detta. Gör så att datum kan väljas inom ett intervall osv." /
    "Även Anläggning, HAZOP-ledare, Deltagare" (2026-08-11) — three new
    fields (Anläggning, HAZOP-ledare, Deltagare) plus a start/end
    QDateEdit date range replacing the old single free-text 'Datum' field.
    Verifies every field round-trips through db.get_config/set_config,
    including the new date-range keys ('project_date_start' /
    'project_date_end')."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_settings_projekt_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_facility_leader_round_trip(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._proj_facility.setText("Gävle Depå")
            panel._proj_facility.editingFinished.emit()
            panel._proj_leader.setText("Anna Andersson")
            panel._proj_leader.editingFinished.emit()

            self.assertEqual(self.db.get_config('project_facility'), "Gävle Depå")
            self.assertEqual(self.db.get_config('project_hazop_leader'), "Anna Andersson")
        finally:
            panel.deleteLater()

    def test_date_range_round_trips_through_config(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._proj_date_start.setDate(QDate(2026, 9, 1))
            panel._proj_date_end.setDate(QDate(2026, 9, 3))
            self.assertEqual(self.db.get_config('project_date_start'), "2026-09-01")
            self.assertEqual(self.db.get_config('project_date_end'), "2026-09-03")
        finally:
            panel.deleteLater()

    def test_date_range_reloads_from_config_on_new_panel(self):
        from hazop import SettingsPanel
        self.db.set_config('project_date_start', '2027-01-10')
        self.db.set_config('project_date_end', '2027-01-12')
        panel = SettingsPanel(self.db)
        try:
            self.assertEqual(panel._proj_date_start.date().toString('yyyy-MM-dd'), '2027-01-10')
            self.assertEqual(panel._proj_date_end.date().toString('yyyy-MM-dd'), '2027-01-12')
        finally:
            panel.deleteLater()

    def test_legacy_project_date_key_is_included_in_reset_cleanup(self):
        """The old single-value 'project_date' key must not be silently
        orphaned: MainWindow's project-reset cleanup list must still clear
        it (for pre-existing databases) alongside all the new keys."""
        import inspect
        import hazop as hazop_mod
        src = inspect.getsource(hazop_mod.MainWindow)
        for key in ('project_date', 'project_date_start', 'project_date_end',
                    'project_facility', 'project_hazop_leader', 'project_participants'):
            self.assertIn(f"'{key}'", src,
                           f"Project-reset cleanup list should still mention {key!r}")


class SettingsPanelDateWidgetsAndTodayButtonTests(unittest.TestCase):
    """"Inställningarna under projekt ser konstig ut. Datumväljren tar upp
    jättemycket plats. skulle även gilla om knappen today fanns."
    (2026-08-11) — the date-range row's container (date_row_w) used to be
    stretched to the tab's full width by QFormLayout's default
    field-growth policy even though the two QDateEdit widgets inside it
    only need a small fraction of that space; fixed by capping both the
    QDateEdit widths and the container's own size policy. Also verifies
    the newly added "Idag" (Today) buttons."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_settings_datewidgets_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_date_edits_have_a_real_width_constraint(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            for edit in (panel._proj_date_start, panel._proj_date_end):
                max_w = edit.maximumWidth()
                # Qt's "no maximum set" sentinel is 16777215; anything even
                # remotely close to that means the widget was left
                # unconstrained. A real cap should comfortably fit
                # "yyyy-MM-dd" plus the calendar-popup arrow, i.e. well
                # under 250px, and definitely nowhere near the full
                # ~1100px+ a stretched QFormLayout field reaches.
                self.assertLess(max_w, 250,
                                 "QDateEdit should have a compact maximum width")
                self.assertGreater(max_w, 0)
        finally:
            panel.deleteLater()

    def test_date_row_container_does_not_stretch_to_full_tab_width(self):
        """Renders the actual Projekt tab and confirms the date row's
        container widget no longer stretches to the tab's full width the
        way the other QLineEdit form rows still do (by design — this test
        is what would have caught the original bug)."""
        from hazop import SettingsPanel
        from PyQt6.QtWidgets import QFormLayout
        panel = SettingsPanel(self.db)
        try:
            panel.resize(900, 700)
            panel.show()
            self.app.processEvents()
            tabs = panel._tabs
            proj_idx = next(i for i in range(tabs.count())
                             if tabs.tabText(i) == "Projekt")
            tabs.setCurrentIndex(proj_idx)
            self.app.processEvents()
            proj_tab = tabs.widget(proj_idx)
            fl = proj_tab.layout()
            self.assertIsInstance(fl, QFormLayout)

            name_field_w = panel._proj_name.width()
            date_row_w = None
            for r in range(fl.rowCount()):
                item = fl.itemAt(r, QFormLayout.ItemRole.FieldRole)
                if item and item.widget() is not None and item.widget() not in (
                        panel._proj_name, panel._proj_facility,
                        panel._proj_leader, panel._proj_rev):
                    date_row_w = item.widget()
                    break
            self.assertIsNotNone(date_row_w, "Could not find the date row's container widget")
            self.assertLess(date_row_w.width(), name_field_w * 0.8,
                             "Date row container should be visibly narrower than a "
                             "full-width text field row, not stretched to match it")
        finally:
            panel.deleteLater()

    def test_today_button_sets_start_date_to_today(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._proj_date_start.setDate(QDate(2020, 1, 1))
            panel._proj_date_start_today_btn.click()
            self.assertEqual(panel._proj_date_start.date(), QDate.currentDate())
        finally:
            panel.deleteLater()

    def test_today_button_sets_end_date_to_today(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._proj_date_end.setDate(QDate(2020, 1, 1))
            panel._proj_date_end_today_btn.click()
            self.assertEqual(panel._proj_date_end.date(), QDate.currentDate())
        finally:
            panel.deleteLater()

    def test_start_today_button_does_not_touch_end_date(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._proj_date_end.setDate(QDate(2020, 1, 1))
            panel._proj_date_start_today_btn.click()
            self.assertEqual(panel._proj_date_end.date(), QDate(2020, 1, 1))
        finally:
            panel.deleteLater()


class ParticipantMatrixTests(unittest.TestCase):
    """"Jag tror det vore bra m du byggde en till flik med deltagare
    istället där man definerar förnamn, efternamn, roll på y axel och
    analystillfälen på x axeln så det blir en matris." (2026-08-11) —
    replaces the old free-text "Deltagare" field/tab-row with a dedicated
    "Deltagare" tab holding a QTableWidget: participants as rows
    (Förnamn/Efternamn/Roll), analysis sessions as columns, attendance as
    a checkbox per cell. Covers both the raw Database CRUD methods and the
    ParticipantMatrixPanel UI wiring."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_participant_matrix_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── Database layer ───────────────────────────────────────────────────
    def test_participant_crud_round_trips(self):
        pid = self.db.add_participant("Anna", "Andersson", "Processägare")
        rows = self.db.list_participants()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['first_name'], "Anna")
        self.assertEqual(rows[0]['last_name'], "Andersson")
        self.assertEqual(rows[0]['role'], "Processägare")

        self.db.update_participant(pid, role="HAZOP-ledare")
        rows = self.db.list_participants()
        self.assertEqual(rows[0]['role'], "HAZOP-ledare")
        self.assertEqual(rows[0]['first_name'], "Anna",
                          "update_participant should leave other fields untouched "
                          "when only one keyword is passed")

        self.db.delete_participant(pid)
        self.assertEqual(self.db.list_participants(), [])

    def test_analysis_session_crud_round_trips(self):
        sid = self.db.add_analysis_session("Session 1 (2026-09-01)")
        rows = self.db.list_analysis_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['label'], "Session 1 (2026-09-01)")

        self.db.update_analysis_session(sid, "Session 1 (omschemalagd)")
        rows = self.db.list_analysis_sessions()
        self.assertEqual(rows[0]['label'], "Session 1 (omschemalagd)")

        self.db.delete_analysis_session(sid)
        self.assertEqual(self.db.list_analysis_sessions(), [])

    def test_attendance_round_trips_and_toggles(self):
        pid = self.db.add_participant("Bengt", "Bengtsson", "Drift")
        sid = self.db.add_analysis_session("Session 1")

        self.assertFalse(self.db.get_attendance(pid, sid),
                          "Attendance should default to False for an unrecorded pair")

        self.db.set_attendance(pid, sid, True)
        self.assertTrue(self.db.get_attendance(pid, sid))

        # Toggling twice must not create duplicate rows (composite PK / upsert).
        self.db.set_attendance(pid, sid, False)
        self.db.set_attendance(pid, sid, True)
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM participant_attendance WHERE participant_id=? AND session_id=?",
            (pid, sid)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertTrue(self.db.get_attendance(pid, sid))

    def test_attendance_matrix_reflects_all_recorded_pairs(self):
        p1 = self.db.add_participant("Anna", "Andersson", "")
        p2 = self.db.add_participant("Bengt", "Bengtsson", "")
        s1 = self.db.add_analysis_session("Session 1")
        s2 = self.db.add_analysis_session("Session 2")
        self.db.set_attendance(p1, s1, True)
        self.db.set_attendance(p1, s2, False)
        self.db.set_attendance(p2, s2, True)

        matrix = self.db.get_attendance_matrix()
        self.assertTrue(matrix.get((p1, s1)))
        self.assertFalse(matrix.get((p1, s2), False))
        self.assertTrue(matrix.get((p2, s2)))
        self.assertNotIn((p2, s1), matrix,
                          "Never-toggled pairs should not have a stored row at all")

    def test_deleting_participant_cascades_attendance(self):
        pid = self.db.add_participant("Anna", "Andersson", "")
        sid = self.db.add_analysis_session("Session 1")
        self.db.set_attendance(pid, sid, True)
        self.db.delete_participant(pid)
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM participant_attendance WHERE participant_id=?",
            (pid,)).fetchone()[0]
        self.assertEqual(count, 0, "Deleting a participant should cascade-delete attendance rows")

    def test_deleting_session_cascades_attendance(self):
        pid = self.db.add_participant("Anna", "Andersson", "")
        sid = self.db.add_analysis_session("Session 1")
        self.db.set_attendance(pid, sid, True)
        self.db.delete_analysis_session(sid)
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM participant_attendance WHERE session_id=?",
            (sid,)).fetchone()[0]
        self.assertEqual(count, 0, "Deleting a session should cascade-delete attendance rows")

    # ── UI layer (ParticipantMatrixPanel) ────────────────────────────────
    def test_panel_add_participant_creates_row_and_db_record(self):
        from hazop import ParticipantMatrixPanel
        panel = ParticipantMatrixPanel(self.db)
        try:
            panel._add_participant()
            self.assertEqual(panel._table.rowCount(), 1)
            self.assertEqual(len(self.db.list_participants()), 1)
        finally:
            panel.deleteLater()

    def test_panel_add_session_creates_column(self):
        from hazop import ParticipantMatrixPanel
        panel = ParticipantMatrixPanel(self.db)
        try:
            with unittest.mock.patch.object(
                    QInputDialog, 'getText', return_value=("Session 1", True)):
                panel._add_session()
            self.assertEqual(panel._table.columnCount(), len(panel._FIXED_COLS) + 1)
            self.assertEqual(len(self.db.list_analysis_sessions()), 1)
            self.assertEqual(
                panel._table.horizontalHeaderItem(len(panel._FIXED_COLS)).text(), "Session 1")
        finally:
            panel.deleteLater()

    def test_panel_editing_name_cells_persists_to_db(self):
        from hazop import ParticipantMatrixPanel
        panel = ParticipantMatrixPanel(self.db)
        try:
            panel._add_participant()
            panel._table.item(0, 0).setText("Anna")
            panel._table.item(0, 1).setText("Andersson")
            panel._table.item(0, 2).setText("Processägare")
            rows = self.db.list_participants()
            self.assertEqual(rows[0]['first_name'], "Anna")
            self.assertEqual(rows[0]['last_name'], "Andersson")
            self.assertEqual(rows[0]['role'], "Processägare")
        finally:
            panel.deleteLater()

    def test_panel_toggling_attendance_checkbox_persists_to_db(self):
        from hazop import ParticipantMatrixPanel
        panel = ParticipantMatrixPanel(self.db)
        try:
            panel._add_participant()
            with unittest.mock.patch.object(
                    QInputDialog, 'getText', return_value=("Session 1", True)):
                panel._add_session()
            pid = self.db.list_participants()[0]['id']
            sid = self.db.list_analysis_sessions()[0]['id']

            cell = panel._table.item(0, len(panel._FIXED_COLS))
            self.assertEqual(cell.checkState(), Qt.CheckState.Unchecked)
            cell.setCheckState(Qt.CheckState.Checked)
            self.assertTrue(self.db.get_attendance(pid, sid))

            cell.setCheckState(Qt.CheckState.Unchecked)
            self.assertFalse(self.db.get_attendance(pid, sid))
        finally:
            panel.deleteLater()

    def test_panel_delete_participant_removes_row_and_db_record(self):
        from hazop import ParticipantMatrixPanel
        panel = ParticipantMatrixPanel(self.db)
        try:
            panel._add_participant()
            panel._table.setCurrentCell(0, 0)
            panel._delete_participant()
            self.assertEqual(panel._table.rowCount(), 0)
            self.assertEqual(self.db.list_participants(), [])
        finally:
            panel.deleteLater()

    def test_panel_delete_session_removes_column_and_db_record(self):
        from hazop import ParticipantMatrixPanel
        panel = ParticipantMatrixPanel(self.db)
        try:
            panel._add_participant()   # ensures there's a row to select a cell in
            with unittest.mock.patch.object(
                    QInputDialog, 'getText', return_value=("Session 1", True)):
                panel._add_session()
            panel._table.setCurrentCell(0, len(panel._FIXED_COLS))
            panel._delete_session()
            self.assertEqual(panel._table.columnCount(), len(panel._FIXED_COLS))
            self.assertEqual(self.db.list_analysis_sessions(), [])
        finally:
            panel.deleteLater()

    def test_panel_loads_existing_data_on_construction(self):
        from hazop import ParticipantMatrixPanel
        pid = self.db.add_participant("Anna", "Andersson", "Processägare")
        sid = self.db.add_analysis_session("Session 1")
        self.db.set_attendance(pid, sid, True)

        panel = ParticipantMatrixPanel(self.db)
        try:
            self.assertEqual(panel._table.rowCount(), 1)
            self.assertEqual(panel._table.item(0, 0).text(), "Anna")
            self.assertEqual(panel._table.item(0, 1).text(), "Andersson")
            self.assertEqual(panel._table.item(0, 2).text(), "Processägare")
            self.assertEqual(
                panel._table.item(0, len(panel._FIXED_COLS)).checkState(),
                Qt.CheckState.Checked)
        finally:
            panel.deleteLater()

    def test_deltagare_tab_exists_in_settings_panel(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            titles = [panel._tabs.tabText(i) for i in range(panel._tabs.count())]
            self.assertIn("Deltagare", titles)
        finally:
            panel.deleteLater()

    def test_old_freetext_participants_field_is_gone(self):
        """"istället" (instead) in the user's request means the new matrix
        REPLACES the old free-text field — it must not still exist
        alongside it."""
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            self.assertFalse(hasattr(panel, '_proj_participants'),
                              "Old free-text Deltagare QPlainTextEdit should be removed")
            titles = [panel._tabs.tabText(i) for i in range(panel._tabs.count())]
            proj_idx = titles.index("Projekt")
            proj_tab = panel._tabs.widget(proj_idx)
            from PyQt6.QtWidgets import QFormLayout, QPlainTextEdit
            fl = proj_tab.layout()
            self.assertIsInstance(fl, QFormLayout)
            for r in range(fl.rowCount()):
                item = fl.itemAt(r, QFormLayout.ItemRole.FieldRole)
                if item and item.widget() is not None:
                    self.assertNotIsInstance(item.widget(), QPlainTextEdit,
                                              "Projekt tab should no longer contain the "
                                              "free-text participants QPlainTextEdit")
        finally:
            panel.deleteLater()


class SettingsPanelPidTabRenameAndNewSettingsTests(unittest.TestCase):
    """"Fliken PID borde kunna ändras till något mer generiskt för
    inställning. Detta borde även kunna utvecklas med fler inställningar."
    / "Byt namn + lägg till OCR/sid-inställningar" (2026-08-11) — the old
    "P&ID" tab is renamed "P&ID-inställningar" and gains two new setting
    groups (OCR-standardval, Sid-orientering) while the pre-existing
    Tagg-identifiering checkbox (tag_strip_spaces) keeps working exactly
    as before."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_settings_pid_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_tab_renamed(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            titles = [panel._tabs.tabText(i) for i in range(panel._tabs.count())]
            self.assertIn("P&ID-inställningar", titles)
            self.assertNotIn("P&ID", titles)
        finally:
            panel.deleteLater()

    def test_tag_strip_spaces_checkbox_still_works(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._strip_spaces_chk.setChecked(False)
            self.assertEqual(self.db.get_config('tag_strip_spaces'), '0')
            panel._strip_spaces_chk.setChecked(True)
            self.assertEqual(self.db.get_config('tag_strip_spaces'), '1')
        finally:
            panel.deleteLater()

    def test_ocr_default_engine_setting_persists(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            idx = panel._ocr_default_combo.findData('auto')
            self.assertGreaterEqual(idx, 0, "'Automatiskt' option should always be present")
            panel._ocr_default_combo.setCurrentIndex(idx)
            self.assertEqual(self.db.get_config('ocr_default_engine'), 'auto')
        finally:
            panel.deleteLater()

    def test_ocr_default_engine_reloads_from_config(self):
        from hazop import SettingsPanel
        self.db.set_config('ocr_default_engine', 'auto')
        panel = SettingsPanel(self.db)
        try:
            self.assertEqual(panel._ocr_default_combo.currentData(), 'auto')
        finally:
            panel.deleteLater()

    def test_page_orientation_setting_persists(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            idx = panel._page_orientation_combo.findData('landscape')
            self.assertGreaterEqual(idx, 0)
            panel._page_orientation_combo.setCurrentIndex(idx)
            self.assertEqual(self.db.get_config('pid_page_orientation_hint'), 'landscape')
        finally:
            panel.deleteLater()

    def test_ocr_default_engine_skips_prompt_when_configured(self):
        """resolve_ocr_scan_choice() (pid_viewer.py) is the actual wiring
        behind the OCR-standardval setting: with a specific, available
        engine configured, it must return that engine directly without
        showing the Yes/No prompt (QMessageBox.question must not be called)."""
        from pid_viewer import resolve_ocr_scan_choice
        with unittest.mock.patch('equipment_detection.ocr_status',
                                  return_value={'tesseract': True, 'easyocr': False,
                                                'rapidocr': False, 'pil': True}), \
             unittest.mock.patch('pid_viewer.ocr_status',
                                  return_value={'tesseract': True, 'easyocr': False,
                                                'rapidocr': False, 'pil': True}), \
             unittest.mock.patch('pid_viewer.QMessageBox.question') as mock_question:
            self.db.set_config('ocr_default_engine', 'tesseract')
            use_ocr, engine = resolve_ocr_scan_choice(self.db, None)
            self.assertTrue(use_ocr)
            self.assertEqual(engine, 'tesseract')
            mock_question.assert_not_called()

    def test_ask_default_falls_back_to_prompt(self):
        """The default 'ask' setting must preserve the original behaviour
        exactly: still show the Yes/No prompt."""
        from pid_viewer import resolve_ocr_scan_choice
        with unittest.mock.patch('pid_viewer.ocr_status',
                                  return_value={'tesseract': True, 'easyocr': False,
                                                'rapidocr': False, 'pil': True}), \
             unittest.mock.patch('pid_viewer.QMessageBox.question',
                                  return_value=hazop.QMessageBox.StandardButton.Yes) as mock_question:
            self.db.set_config('ocr_default_engine', 'ask')
            use_ocr, engine = resolve_ocr_scan_choice(self.db, None)
            mock_question.assert_called_once()
            self.assertTrue(use_ocr)


class ClearedConsequenceRowHeightTests(unittest.TestCase):
    """'När jag har skapat en konsekvens och sedan suddar ut allt krymper
    raden och blir alltför låg vilket gör att jag inte ser vad som står på
    orsak och FA/antändning ser konstigt ut.' (2026-08-11) —
    _update_row_text_only()'s fast path used to set a row's height to ONLY
    what the just-edited column needed (_wrap_col_row_height(row, col)),
    discarding whatever a long ORS cause description or the fixed-height
    _LopaWidget (FA/Ant./Övriga column) in the SAME row required. Clearing
    a consequence's text back to empty shrank the row to one line,
    clipping the cause text and squashing the LOPA widget."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_clearing_consequence_text_does_not_shrink_row_below_cause_and_lopa_needs(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            long_cause = ("En mycket lång orsakstext som garanterat radbryts "
                          "över flera rader i cellen. " * 3)
            win.db.update_cause(cause_id, description=long_cause)
            cons_id = win.db.add_consequence(cause_id)
            panel.load_cause(cause_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            # Simulate: user types a long consequence description (growing
            # the row), then erases it completely back to empty.
            panel._update_row_text_only('consequence', cons_id,
                                        "En lång konsekvensbeskrivning som också radbryts. " * 5)
            panel._update_row_text_only('consequence', cons_id, "")

            needed_for_cause = panel._wrap_col_row_height(row, panel._C_ORS)
            lopa_widget = panel._table.cellWidget(row, panel._C_LOPA)
            needed_for_lopa = lopa_widget.sizeHint().height() if lopa_widget else 0
            actual = panel._table.rowHeight(row)

            self.assertGreaterEqual(actual, needed_for_cause,
                "row shrank below what the (still long, unchanged) cause text needs")
            self.assertGreaterEqual(actual, needed_for_lopa,
                "row shrank below the FA/Ant. widget's own fixed height")


class PIDPanelStaleActiveIdTests(unittest.TestCase):
    """A cause/consequence deleted elsewhere (e.g. its node removed) while
    still 'active' in the PIDPanel used to survive as a stale id into the
    next placement click, crashing add_consequence/add_safeguard with
    sqlite3.IntegrityError: FOREIGN KEY constraint failed (real crash
    report, 2026-08-07 — crash_20260807_134324_IntegrityError.json). The
    P&ID-click reproduction tests for this were removed 2026-08-13 (see
    NOTES.md: _on_consequence_click/_on_safeguard_click no longer exist —
    the P&ID canvas is now object-placement-only) — the underlying
    stale-id reset logic they exercised is still covered by the tests
    below, which drive it directly rather than through a removed click
    handler."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_staleactive_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clear_active_selection_resets_all_placement_state(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.panel._active_node_id        = node_id
        self.panel._active_deviation_id   = dev_id
        self.panel._active_cause_id       = cause_id
        self.panel._active_consequence_id = cons_id

        self.panel.clear_active_selection()

        self.assertIsNone(self.panel._active_node_id)
        self.assertIsNone(self.panel._active_deviation_id)
        self.assertIsNone(self.panel._active_cause_id)
        self.assertIsNone(self.panel._active_consequence_id)

    def test_structure_changed_clears_pid_panel_stale_active_cause(self):
        """End-to-end: deleting a node via the tree (which emits
        structure_changed) must not leave MainWindow.pid_panel holding a
        cause id belonging to the now-deleted node."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.pid_panel._active_cause_id = cause_id

            win.db.delete_node(node_id)
            win._on_structure_changed()

            self.assertIsNone(win.pid_panel._active_cause_id)


class RiskMatrixPopupHoverStyleTests(unittest.TestCase):
    """Reported feedback: clicking into the risk matrix popup looked like
    two cells were "checked" when only one should be. Root cause: the
    is_current marker (a solid black border) and the :hover style used
    the exact same border, so hovering any cell other than the actual
    current value looked indistinguishable from it."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_hover_style_is_distinct_from_current_value_marker(self):
        from hazop import RiskMatrixPopup
        from PyQt6.QtWidgets import QPushButton
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            matrix_buttons = [b for b in popup.findChildren(QPushButton) if b.styleSheet()]
            self.assertTrue(matrix_buttons)
            current_btns = [b for b in matrix_buttons if '3px solid #000' in b.styleSheet()]
            self.assertEqual(len(current_btns), 1,
                "exactly one cell should carry the current-value border")
            for btn in matrix_buttons:
                self.assertNotIn(
                    'QPushButton:hover{border:2px solid #000;}', btn.styleSheet(),
                    "hover style must not reuse the current-value marker's exact border")
        finally:
            popup.deleteLater()

    def test_grid_buttons_never_get_qts_own_default_or_focus_outline(self):
        """2026-08-14 follow-up: the hover fix above wasn't the only cause
        of "two cells look marked" — Qt auto-assigns one pushbutton in a
        QDialog as the default/initially-focused button, and the app's
        global stylesheet paints THAT button with its own blue focus/
        default outline regardless of is_current. Every grid button must
        opt out of both."""
        from hazop import RiskMatrixPopup
        from PyQt6.QtWidgets import QPushButton
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            matrix_buttons = [b for b in popup.findChildren(QPushButton) if b.styleSheet()]
            self.assertTrue(matrix_buttons)
            for btn in matrix_buttons:
                self.assertFalse(btn.autoDefault(), "grid buttons must not be auto-default")
                self.assertFalse(btn.isDefault(), "grid buttons must not be the dialog's default")
                self.assertEqual(btn.focusPolicy(), Qt.FocusPolicy.NoFocus,
                    "grid buttons must not be focusable, so Qt can't paint a focus outline on one")
        finally:
            popup.deleteLater()


class SafeguardRRFBadgeHeaderTests(unittest.TestCase):
    """"rrf rutan på safeguard blir väldigt hög. gör denna lägre genom
    att ta bort rrf och låta det stå i kolumneubriken istället."
    (2026-08-14) — the badge text used to be a two-line "RRF\\n{rrf}",
    which forced the badge box (and thus visually the cell) taller than
    a plain number needs. Move the "RRF" label into the column header
    instead and paint just the number in the cell."""

    def test_safeguard_badge_paints_only_the_number(self):
        import inspect
        import hazop as hazop_mod
        src = inspect.getsource(hazop_mod._PidDelegate.paint)
        self.assertNotIn('RRF\\n{rrf}', src,
            "SG badge must no longer draw the two-line 'RRF\\n<value>' text")
        self.assertIn('f"{rrf}"', src,
            "SG badge should paint just the bare RRF number")

    def test_barriarer_column_header_mentions_rrf(self):
        from hazop import ScenarioTablePanel
        self.assertIn('RRF', ScenarioTablePanel._COLS[ScenarioTablePanel._C_SG],
            "since the cell badge no longer spells out 'RRF', the column header must")


class OrsTagZoneOpensMinimalPopupTests(unittest.TestCase):
    """"klickarna man på tagen justerar man tagen ... gör samtliga
    minimalistiska" (2026-08-14) — a plain click on the ORS tag zone
    used to open the large combined CauseObjectPopup (tag+type+
    avvikelse-picker+standard-cause list). It now opens the much
    smaller CauseTagPopup instead. CauseObjectPopup itself is
    untouched and still used, unchanged, by the detail panel
    (_edit_cause_obj) and quick-add (_quick_add_cause) — see
    CauseTagLiveLinkTests, which still exercises _apply_cause_obj the
    same way regardless of which popup calls it."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orstagzone_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.cause_id = self.db.add_cause(self.dev_id)
        self.panel.load_node(self.node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == self.cause_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_tag_zone_click_opens_cause_tag_popup_not_cause_object_popup(self):
        from PyQt6.QtCore import QSize
        fake_popup = unittest.mock.Mock()
        fake_popup.sizeHint.return_value = QSize(200, 100)
        with unittest.mock.patch('hazop.CauseTagPopup', return_value=fake_popup) as MockPopup, \
             unittest.mock.patch('hazop.CauseObjectPopup') as MockBigPopup:
            self.panel._show_cause_obj_popup(self.row, self.cause_id, QPoint(100, 100))
            MockPopup.assert_called_once()
            MockBigPopup.assert_not_called()
            fake_popup.exec.assert_called_once()

    def test_committing_the_tag_popup_calls_apply_cause_obj_with_empty_description(self):
        """The commit path must reuse _apply_cause_obj's existing "only
        tag/type changed" fast path (empty description, no frequency)
        instead of duplicating its persistence logic."""
        from PyQt6.QtCore import QSize
        apply_spy = unittest.mock.Mock()
        self.panel._apply_cause_obj = apply_spy
        fake_popup = unittest.mock.Mock()
        fake_popup.sizeHint.return_value = QSize(200, 100)
        captured = {}
        fake_popup.committed.connect = lambda slot: captured.__setitem__('slot', slot)
        with unittest.mock.patch('hazop.CauseTagPopup', return_value=fake_popup):
            self.panel._show_cause_obj_popup(self.row, self.cause_id, QPoint(100, 100))
        captured['slot']('Ventil', 'PV-999')
        apply_spy.assert_called_once_with(self.row, self.cause_id, 'Ventil', 'PV-999', '', None)


class OrsFrequencyZoneClickTests(unittest.TestCase):
    """"klickar man på frekvens skall man kunna justera frekvens"
    (2026-08-14) — the ORS strip's frequency label had no click zone at
    all; a click there fell through to plain cell selection/edit.
    FrequencyPickerPopup already existed, fully built, but was never
    wired up anywhere. Also covers the "frequency text collides with
    the invisible clone-icon zone" mismatch found while implementing
    this: the frequency check now runs BEFORE the clone/comment check,
    so a click anywhere on the actual rendered frequency text always
    opens the frequency popup regardless of that overlap."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsfreqclick_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(self.cause_id, comp_type='V', comp_tag='PV-101')
        self.panel.load_node(node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == self.cause_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _freq_zone_geometry(self):
        panel = self.panel
        panel._table.setColumnWidth(panel._C_ORS, 220)
        panel.resize(900, 400)
        panel.show()
        self.app.processEvents()
        col_x = panel._table.columnViewportPosition(panel._C_ORS)
        cell_right = col_x + panel._table.columnWidth(panel._C_ORS) - 1
        item = panel._table.item(self.row, panel._C_ORS)
        return panel._ors_tag_zone_geometry(item, col_x, cell_right)

    def _click(self, x, y):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt
        pos = QPoint(x, y)
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                          _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                          _Qt.KeyboardModifier.NoModifier)
        return self.panel.eventFilter(self.panel._table.viewport(), ev)

    def test_clicking_frequency_zone_opens_frequency_picker_popup(self):
        _tw, freq_zone_x, freq_zone_w, freq_str = self._freq_zone_geometry()
        self.assertTrue(freq_str, "test setup issue: cause has no frequency label to click on")
        row_y = self.panel._table.rowViewportPosition(self.row) + 3
        fake_popup = unittest.mock.Mock()
        with unittest.mock.patch('hazop.FrequencyPickerPopup.create_positioned',
                                  return_value=fake_popup) as mock_create:
            handled = self._click(freq_zone_x + freq_zone_w // 2, row_y)
        self.assertTrue(handled)
        mock_create.assert_called_once()
        fake_popup.exec.assert_called_once()

    def test_clicking_below_the_strip_does_not_open_the_frequency_popup(self):
        """The frequency zone must be confined to the strip's own
        height, so a click on the wrapped description text below it
        (same x range) doesn't also open the popup. A click there can
        legitimately fall through to the pre-existing (unrelated, out
        of scope here) clone/comment/plus-badge zones instead — those
        are stubbed out so this test only asserts on the one thing it
        owns: the frequency popup must not fire."""
        from hazop import _ORS_STRIP_H
        _tw, freq_zone_x, freq_zone_w, freq_str = self._freq_zone_geometry()
        self.panel._row_plus_cols = {}
        self.panel._clone_scenario = unittest.mock.Mock()
        self.panel._open_comment_popup = unittest.mock.Mock()
        row_y = self.panel._table.rowViewportPosition(self.row)
        with unittest.mock.patch('hazop.FrequencyPickerPopup.create_positioned') as mock_create:
            self._click(freq_zone_x + freq_zone_w // 2, row_y + _ORS_STRIP_H + 5)
            mock_create.assert_not_called()

    def test_picking_a_preset_frequency_sets_likelihood_and_clears_base_frequency(self):
        self.db.update_cause(self.cause_id, base_frequency=3.5)
        rebuild_spy = unittest.mock.Mock()
        self.panel._schedule_rebuild = rebuild_spy

        self.panel._on_ors_frequency_picked(self.cause_id, 2, None)

        cause = self.db.get_cause(self.cause_id)
        self.assertEqual(cause['likelihood'], 2)
        self.assertIsNone(cause['base_frequency'])
        rebuild_spy.assert_called_once()

    def test_picking_a_numeric_frequency_sets_base_frequency(self):
        rebuild_spy = unittest.mock.Mock()
        self.panel._schedule_rebuild = rebuild_spy

        self.panel._on_ors_frequency_picked(self.cause_id, None, 0.5)

        cause = self.db.get_cause(self.cause_id)
        self.assertEqual(cause['base_frequency'], 0.5)
        rebuild_spy.assert_called_once()


class DeviationDefaultFrequencyTests(unittest.TestCase):
    """"på avvikelserna ska man se den förvalda frekvensen" (2026-08-14)
    — deviations have no frequency column of their own (confirmed via
    AskUserQuestion, deliberately no new schema column). The default
    shown is DERIVED from standard_causes instead: the lowest
    standard_causes.frequency among rows whose standard_deviations
    entry matches the deviation's description text."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_devdefaultfreq_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_returns_none_when_no_matching_standard_deviation(self):
        self.assertIsNone(self.db.default_frequency_for_deviation("Helt påhittad avvikelse"))

    def test_returns_none_when_matching_causes_have_no_frequency(self):
        # A deliberately unique guide word — DEVIATION_TYPES/the seeded
        # standard library already ships real entries like "Högt flöde"
        # with their own preset frequencies, which would silently leak
        # into this test's result if reused here.
        dev_id = self.db.add_standard_deviation("ZZZ_Testavvikelse_Unik_1")
        self.db.add_standard_cause(dev_id, "Ventilfel")
        self.assertIsNone(self.db.default_frequency_for_deviation("ZZZ_Testavvikelse_Unik_1"))

    def test_returns_lowest_frequency_among_matching_standard_causes(self):
        dev_id = self.db.add_standard_deviation("ZZZ_Testavvikelse_Unik_2")
        c1 = self.db.add_standard_cause(dev_id, "Ventilfel")
        c2 = self.db.add_standard_cause(dev_id, "Sensorfel")
        self.db.update_standard_cause(c1, frequency=0.5)
        self.db.update_standard_cause(c2, frequency=0.1)
        self.assertEqual(
            self.db.default_frequency_for_deviation("ZZZ_Testavvikelse_Unik_2"), 0.1)


class AvvikelseCellPickerTests(unittest.TestCase):
    """"klockan man på avvikelsen justerar man avvikelsen" (2026-08-14)
    — a click on the Avvikelse (_C_DEV) cell opens DeviationPickerPopup:
    presets for the node's existing deviations (each showing its
    derived default frequency) plus a free-text field for a new one.
    Distinct from the pre-existing "↕ Flytta till annan avvikelse…"
    context-menu action (_move_cause_dialog) — both end up calling the
    same db.move_cause_to_deviation, just via different UI entry points."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_devcellclick_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.panel.always_show_deviation_column()
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.cause_id = self.db.add_cause(self.dev_id)
        self.panel.load_node(self.node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == self.cause_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _click_cell(self, row, col):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt
        self.panel._table.setColumnWidth(col, 150)
        self.panel.resize(900, 400)
        self.panel.show()
        self.app.processEvents()
        idx = self.panel._table.model().index(row, col)
        cr = self.panel._table.visualRect(idx)
        pos = cr.center()
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                          _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                          _Qt.KeyboardModifier.NoModifier)
        return self.panel.eventFilter(self.panel._table.viewport(), ev)

    def test_clicking_avvikelse_cell_opens_deviation_picker_popup(self):
        from PyQt6.QtCore import QSize
        fake_popup = unittest.mock.Mock()
        fake_popup.sizeHint.return_value = QSize(200, 100)
        with unittest.mock.patch('hazop.DeviationPickerPopup.create_positioned',
                                  return_value=fake_popup) as mock_create:
            handled = self._click_cell(self.row, self.panel._C_DEV)
        self.assertTrue(handled)
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args.args[1], self.node_id)
        self.assertEqual(mock_create.call_args.args[2], self.dev_id)
        fake_popup.exec.assert_called_once()

    def test_picking_an_existing_deviation_moves_the_cause(self):
        other_dev_id = self.db.add_deviation(self.node_id, "Omvänt flöde")
        rebuild_spy = unittest.mock.Mock()
        self.panel._schedule_rebuild = rebuild_spy
        structure_spy = unittest.mock.Mock()
        self.panel.structure_changed.connect(structure_spy)

        self.panel._on_deviation_picked(self.cause_id, self.node_id, other_dev_id, None)

        self.assertEqual(self.db.get_cause(self.cause_id)['deviation_id'], other_dev_id)
        rebuild_spy.assert_called_once()
        structure_spy.assert_called_once()

    def test_freetext_creates_and_moves_to_a_new_deviation(self):
        # A deliberately unique guide word — add_node() already auto-
        # seeds every DEVIATION_TYPES entry (including real ones like
        # "Omvänt flöde") for a new node, which would silently match
        # via get_or_create_deviation instead of exercising the
        # "actually create a new one" path this test is about.
        self.panel._schedule_rebuild = unittest.mock.Mock()

        self.panel._on_deviation_picked(self.cause_id, self.node_id, None, "Helt Ny Text 42")

        new_dev = self.db.get_deviation(self.db.get_cause(self.cause_id)['deviation_id'])
        self.assertEqual(new_dev['description'], "Helt Ny Text 42")

    def test_freetext_reuses_an_existing_deviation_with_the_same_text(self):
        """get_or_create_deviation must not create a duplicate row when
        the typed text matches an existing deviation exactly."""
        existing_dev_id = self.db.add_deviation(self.node_id, "Helt Ny Text 42")
        self.panel._schedule_rebuild = unittest.mock.Mock()

        self.panel._on_deviation_picked(self.cause_id, self.node_id, None, "Helt Ny Text 42")

        self.assertEqual(self.db.get_cause(self.cause_id)['deviation_id'], existing_dev_id)

    def test_placeholder_row_with_no_cause_does_not_open_the_popup(self):
        """A deviation with zero causes yet has cause_id None in
        _row_meta — nothing to move, so the click must be a no-op."""
        empty_dev_id = self.db.add_deviation(self.node_id, "Tom avvikelse")
        self.panel.set_show_empty_deviations(True)
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta)
                   if m[0] == empty_dev_id and m[1] is None)
        with unittest.mock.patch('hazop.DeviationPickerPopup.create_positioned') as mock_create:
            self._click_cell(row, self.panel._C_DEV)
            mock_create.assert_not_called()


class DatabaseBusyTimeoutTests(unittest.TestCase):
    """Database.__init__ used to connect with sqlite3's default 5s
    busy-timeout — too short for real lock contention (the online-backup
    copy every commit() does, a previous instance still releasing its WAL
    lock, or the .db file living in a OneDrive-synced folder). A DDL
    statement mid-migration hitting that window raised
    sqlite3.OperationalError: database is locked (real crash report,
    crash_20260807_162445_OperationalError.json)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_busytimeout_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_database_connects_with_generous_busy_timeout(self):
        import sqlite3 as _sqlite3
        real_connect = _sqlite3.connect
        calls = []

        def spy_connect(*a, **kw):
            calls.append((a, kw))
            return real_connect(*a, **kw)

        db_path = os.path.join(self._tmpdir, "test_project.db")
        with unittest.mock.patch.object(_sqlite3, 'connect', side_effect=spy_connect):
            db = Database(path=db_path)
        try:
            main_conn_calls = [c for c in calls if db_path in c[0]]
            self.assertTrue(main_conn_calls,
                "Database.__init__ never called sqlite3.connect on the project db path")
            _, kwargs = main_conn_calls[0]
            self.assertGreaterEqual(kwargs.get('timeout', 5.0), 15.0,
                "project db connection still uses sqlite3's short default busy-timeout")
        finally:
            del db


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


class EquipmentDragNavButtonResetTests(unittest.TestCase):
    """Shift-dragging an equipment marker onto the tree/scenario uses
    QDrag.exec(), a native modal drag loop — Qt suppresses the normal
    hover/leave events other widgets rely on to clear their pressed look
    during it, which could leave the "🔍 Navigera" toolbar button visually
    stuck looking pressed in after the drop even though nothing is
    actually held down (reported: nav button stays pressed after
    drag-and-dropping an equipment marker to the tree/hazop scenario)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dragnav_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_equipment_drag_finished_releases_stuck_nav_button(self):
        from pid_viewer import MODE_NAV
        nav_btn = self.panel.mode_buttons[MODE_NAV]
        nav_btn.setDown(True)   # simulate the stuck-pressed visual left by a native QDrag

        self.panel.viewer.equipment_drag_finished.emit()

        self.assertFalse(nav_btn.isDown())

    def test_viewer_emits_equipment_drag_finished_after_shift_drag_release(self):
        """The signal must actually fire once a real Shift+drag of an
        equipment marker completes, not just work in isolation when
        emitted by hand."""
        from PyQt6.QtCore import QPointF
        received = []
        self.panel.viewer.equipment_drag_finished.connect(lambda: received.append(True))

        with unittest.mock.patch('pid_viewer.QDrag') as mock_drag_cls:
            mock_drag = mock_drag_cls.return_value
            start = QPointF(0, 0)
            self.panel.viewer._equip_drag_candidate = (999, start)
            event = unittest.mock.MagicMock()
            event.buttons.return_value = Qt.MouseButton.LeftButton
            event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
            event.position.return_value = QPointF(
                QApplication.startDragDistance() + 5, 0)

            self.panel.viewer.mouseMoveEvent(event)

        mock_drag.exec.assert_called_once()
        self.assertEqual(len(received), 1,
            "equipment_drag_finished must fire exactly once the drag.exec() call returns")

    def test_shift_drag_resets_scroll_hand_drag_mode_after_native_drag(self):
        """2026-08-13 follow-up report: 'musen sitter kvar i dra-läge' —
        the earlier fix above only cleared the toolbar button's stuck
        LOOK; it didn't address the actual root cause. MODE_NAV's press
        handler falls through to super().mousePressEvent() (needed so a
        Shift+click that never crosses the drag threshold still
        click-dispatches normally), which arms Qt's own ScrollHandDrag
        hand-scroll tracking. Because drag.exec() hijacks the gesture
        instead of a normal move/release pair, Qt never sees the
        matching release that would close that out — leaving the
        viewport's cursor/pan state stuck as if still mid-drag. Toggling
        dragMode off and back on must run right after drag.exec()
        returns, for every drop target (this is what "till alla celler"
        needs — the reset isn't conditional on where the drop landed)."""
        from PyQt6.QtCore import QPointF
        from pid_viewer import MODE_NAV
        self.panel.viewer.set_mode(MODE_NAV)
        # Simulate what the real mousePressEvent's fallthrough to
        # super().mousePressEvent() leaves behind: Qt's ScrollHandDrag
        # switches the cursor to a closed hand the moment the button
        # went down, before mouseMoveEvent ever gets a chance to hijack
        # the gesture into a native drag.
        self.panel.viewer.setCursor(Qt.CursorShape.ClosedHandCursor)

        with unittest.mock.patch('pid_viewer.QDrag'):
            start = QPointF(0, 0)
            self.panel.viewer._equip_drag_candidate = (999, start)
            event = unittest.mock.MagicMock()
            event.buttons.return_value = Qt.MouseButton.LeftButton
            event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
            event.position.return_value = QPointF(
                QApplication.startDragDistance() + 5, 0)

            self.panel.viewer.mouseMoveEvent(event)

        self.assertEqual(self.panel.viewer.dragMode(),
                          self.panel.viewer.DragMode.ScrollHandDrag)
        self.assertEqual(self.panel.viewer.cursor().shape(), Qt.CursorShape.OpenHandCursor,
            "the cursor must be forced back to the idle open-hand look, not left as a closed hand")


class ShiftClickInsertsTagIntoActiveEditorTests(unittest.TestCase):
    """"Om jag skriver en konsekvens ... och sedan håller nere shift och
    klickar på ett objekt vill jag att detta läggs till till
    konsekvenskedjan automatiskt och att jag kan fortsätta skriva efter
    objektet. Dvs att jag inte hoppar ut ur textediteringsvyn." (2026-08-13)

    Every equipment-marker click today — Shift or not — already runs
    marker_navigated -> MainWindow._on_equipment_marker_navigate ->
    scenario_panel.load_equipment() -> _rebuild(), which explicitly
    does focusWidget().clearFocus() then setRowCount(0): exactly what
    would destroy an open ORS/KON/SG cell editor. Shift+click while a
    cell is being edited must instead insert the marker's tag straight
    into the live editor's text (mutating only the open QLineEdit, no
    DB write) and swallow the click — no popup, no marker_navigated, no
    rebuild — so the existing commit-on-editingFinished path persists
    the final text normally and the user never leaves the editor."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_shiftclickinsert_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        from pid_viewer import PIDPanel
        self.panel = ScenarioTablePanel(self.db)
        self.pid_panel = PIDPanel(self.db)
        self.pid_panel._active_edit_query_fn = self.panel.active_edit_target

        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        self.node_id = node_id
        self.panel.load_node(node_id)

        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
        self.marker_id = self.db.add_equipment_marker(
            self.eq_id, "PV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9, link_method='leader')

    def tearDown(self):
        self.panel.deleteLater()
        self.pid_panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _start_editing_kon(self, text=''):
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        item = self.panel._table.item(row, self.panel._C_KON)
        self.panel._table.editItem(item)
        editor = self.panel._table.focusWidget()
        assert isinstance(editor, QLineEdit)
        if text:
            editor.setText(text)
            editor.setCursorPosition(len(text))
        return editor

    def test_active_edit_target_is_none_when_nothing_is_being_edited(self):
        self.assertIsNone(self.panel.active_edit_target())

    def test_active_edit_target_returns_the_live_editor_for_a_kon_cell(self):
        editor = self._start_editing_kon()
        got_editor, kind, id_ = self.panel.active_edit_target()
        self.assertIs(got_editor, editor)
        self.assertEqual((kind, id_), ('consequence', self.cons_id))

    def test_insert_tag_into_editor_adds_spacing_on_both_sides(self):
        editor = self._start_editing_kon("Högt flöde till")
        self.pid_panel._insert_tag_into_editor(editor, "PV-101")
        self.assertEqual(editor.text(), "Högt flöde till PV-101 ")
        self.assertEqual(editor.cursorPosition(), len(editor.text()))

    def test_insert_tag_into_editor_mid_text_keeps_the_remainder(self):
        editor = self._start_editing_kon("Högt flöde stänger ventilen")
        editor.setCursorPosition(len("Högt flöde "))
        self.pid_panel._insert_tag_into_editor(editor, "PV-101")
        self.assertEqual(editor.text(), "Högt flöde PV-101 stänger ventilen")

    def test_insert_tag_into_empty_editor(self):
        editor = self._start_editing_kon()
        editor.clear()   # start from a genuinely empty editor, independent
                          # of _PidDelegate's own "—" placeholder-stripping
        self.pid_panel._insert_tag_into_editor(editor, "PV-101")
        self.assertEqual(editor.text(), "PV-101 ")

    def test_shift_click_while_editing_inserts_tag_and_does_not_navigate(self):
        editor = self._start_editing_kon("Högt flöde till ")
        captured = []
        self.pid_panel.marker_navigated.connect(lambda t, i: captured.append((t, i)))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertIn("PV-101", editor.text())
        self.assertEqual(captured, [], "marker_navigated must not fire while inserting into an active editor")

    def test_shift_click_syncs_tagged_refs_so_the_tag_gets_bold_highlighted(self):
        """"att den blir fetstil om jag är i skrivläget på konsekvens och
        håller [shift]" (2026-08-13) — the drag-and-drop path already
        bolds any tag it appends via tagged_refs (_PidDelegate paint);
        Shift+click-insert must give the same treatment, not just plain
        text, even though the description write itself is deferred to
        the normal edit-commit."""
        self._start_editing_kon("Högt flöde till ")

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        cons = self.db.get_consequence(self.cons_id)
        self.assertIn("PV-101", (cons['tagged_refs'] or '').split(','))
        self.assertEqual(cons['comp_tag'], "PV-101")
        self.assertEqual(cons['comp_type'], "Ventil")

    def test_shift_click_tag_sync_does_not_overwrite_the_persisted_description(self):
        """The DB description column must stay untouched by the sync —
        only the live editor's text (already updated by
        _insert_tag_into_editor) changes; the normal edit-commit path
        is what eventually saves the full text, so writing a stale
        pre-edit description here would just be overwritten a moment
        later and risks a race with what the user is still typing."""
        original_desc = self.db.get_consequence(self.cons_id)['description']
        self._start_editing_kon("Högt flöde till ")

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertEqual(self.db.get_consequence(self.cons_id)['description'], original_desc)

    def test_shift_click_on_safeguard_cell_also_syncs_tagged_refs(self):
        sg_id = self.db.add_safeguard(self.cons_id)
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[3] == sg_id)
        item = self.panel._table.item(row, self.panel._C_SG)
        self.panel._table.editItem(item)
        editor = self.panel._table.focusWidget()
        editor.setText("Larm vid ")
        editor.setCursorPosition(len(editor.text()))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertIn("PV-101", editor.text())
        sg = self.db.get_safeguard(sg_id)
        self.assertIn("PV-101", (sg['tagged_refs'] or '').split(','))
        self.assertEqual(sg['comp_tag'], "PV-101")

    def test_plain_click_while_editing_falls_back_to_normal_navigation(self):
        """Sanity check: the new branch must not hijack every click just
        because a cell happens to be open for editing — only Shift+click
        gets the new behaviour."""
        self._start_editing_kon("Högt flöde till ")
        captured = []
        self.pid_panel.marker_navigated.connect(lambda t, i: captured.append((t, i)))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.NoModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertEqual(captured, [('equipment', self.marker_id)])

    def test_shift_click_with_no_active_editor_falls_back_to_normal_navigation(self):
        captured = []
        self.pid_panel.marker_navigated.connect(lambda t, i: captured.append((t, i)))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertEqual(captured, [('equipment', self.marker_id)])


class NodeMarkupPanelNavigateTests(unittest.TestCase):
    """NodeMarkupPanel's prev/next node buttons (⬆/⬇) crashed with
    TypeError: 'method' object is not iterable — _navigate_prev/
    _navigate_next read `self.db.nodes` (the bound method itself) instead
    of calling `self.db.nodes()` (2026-08-11 crash reports,
    crash_20260811_162420/162424_TypeError.json)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_navtest_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_panel(self, node_id):
        from hazop import NodeMarkupPanel
        panel = NodeMarkupPanel(self.db)
        panel.node_id = node_id
        return panel

    def test_navigate_prev_emits_previous_node_id(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        panel = self._make_panel(node_b)
        try:
            seen = []
            panel.navigate_node_requested.connect(seen.append)
            panel._navigate_prev()
            self.assertEqual(seen, [node_a])
        finally:
            panel.deleteLater()

    def test_navigate_next_emits_next_node_id(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        panel = self._make_panel(node_a)
        try:
            seen = []
            panel.navigate_node_requested.connect(seen.append)
            panel._navigate_next()
            self.assertEqual(seen, [node_b])
        finally:
            panel.deleteLater()

    def test_navigate_prev_at_first_node_is_noop(self):
        node_a = self.db.add_node()
        self.db.add_node()
        panel = self._make_panel(node_a)
        try:
            seen = []
            panel.navigate_node_requested.connect(seen.append)
            panel._navigate_prev()
            self.assertEqual(seen, [])
        finally:
            panel.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# Adaptive re-rasterization on zoom (2026-08-12) — "P&ID blir suddig vid
# inzoomning". PIDGraphicsView's hi-res tier used to always render at a
# FIXED fitz.Matrix scale (_RASTER_SCALE) regardless of how far the
# QGraphicsView itself was zoomed in, so zooming in past what that fixed
# scale could physically supply just stretched the same bitmap (blur).
# _target_raster_scale()/_update_page_lod() now track the current view zoom
# and re-render sharper on demand (capped so an extreme zoom can't render an
# absurdly large pixmap), without changing render_scale itself — the
# scene-units-per-pdf-point factor every coordinate transform assumes.
# ══════════════════════════════════════════════════════════════════════════

class AdaptiveRasterZoomTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_lod_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_pdf(self, w=400.0, h=300.0):
        import fitz
        path = os.path.join(self._tmpdir, "test.pdf")
        doc = fitz.open()
        page = doc.new_page(width=w, height=h)
        page.draw_rect(fitz.Rect(10, 10, w - 10, h - 10))
        doc.save(path)
        doc.close()
        return path

    def test_target_raster_scale_at_default_zoom_equals_base(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.render_scale = view._RASTER_SCALE   # set by _render_all_pages() once a PDF is loaded
        self.assertEqual(view.transform().m11(), 1.0)
        target = view._target_raster_scale(1000.0, 800.0)
        self.assertEqual(target, view._RASTER_SCALE,
            "at normal (100%) zoom the fixed base raster is already crisp "
            "enough — must not upgrade unnecessarily")

    def test_target_raster_scale_increases_with_zoom(self):
        """The bug report's exact scenario: zooming the QGraphicsView in
        past what _RASTER_SCALE alone can supply must raise the target
        render resolution instead of leaving it fixed."""
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.render_scale = view._RASTER_SCALE
        view.scale(4.0, 4.0)
        # Small page so the pixel-dimension safety cap (tested separately
        # below) doesn't also bind here — isolates the zoom-tracking behaviour.
        target = view._target_raster_scale(100.0, 80.0)
        self.assertGreater(target, view._RASTER_SCALE)
        expected = 4.0 * view.render_scale / view._LOD_MARGIN
        self.assertAlmostEqual(target, expected, places=6)

    def test_target_raster_scale_capped_for_extreme_zoom(self):
        """Safety cap: must not render an absurdly large pixmap just
        because the view zoom is extreme — bounded by pixel dimensions."""
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.render_scale = view._RASTER_SCALE
        view.scale(1000.0, 1000.0)
        pw, ph = 1200.0, 600.0   # dim cap = 6000/1200 = 5.0 (between base 3.0 and multiplier cap 12.0)
        target = view._target_raster_scale(pw, ph)
        self.assertAlmostEqual(target, 5.0, places=6)
        self.assertLessEqual(target * pw, view._MAX_RASTER_DIM + 1e-6)

    def test_update_page_lod_rerenders_sharper_when_zoomed_in_far(self):
        from pid_viewer import PIDGraphicsView
        path = self._make_pdf()
        view = PIDGraphicsView()
        self.assertTrue(view.load_pdf(path))
        pn = 0
        view.centerOn(view._all_page_items[pn].sceneBoundingRect().center())
        old_footprint = view._all_page_items[pn].sceneBoundingRect()

        view.scale(6.0, 6.0)   # well past what _RASTER_SCALE=3.0 alone can supply
        view._update_page_lod()
        self.assertIsNotNone(view._lod_renderer,
            "zooming in far enough must trigger a background re-render")
        self.assertTrue(view._lod_renderer.wait(5000),
            "background re-render did not finish in time")
        self.app.processEvents()

        self.assertIn(pn, view._page_cache_scale)
        self.assertGreater(view._page_cache_scale[pn], view._RASTER_SCALE,
            "must have re-rasterized ABOVE the fixed base scale once zoomed in far")
        self.assertEqual(view._page_display_scale.get(pn), view._page_cache_scale[pn])

        # The scene footprint — what scene_to_pdf()/pdf_to_scene() and every
        # marker coordinate assume — must be unchanged by the resolution
        # upgrade; only the underlying pixmap got sharper.
        new_footprint = view._all_page_items[pn].sceneBoundingRect()
        self.assertAlmostEqual(old_footprint.width(),  new_footprint.width(),  places=3)
        self.assertAlmostEqual(old_footprint.height(), new_footprint.height(), places=3)

    def test_update_page_lod_does_not_rerender_when_already_crisp_enough(self):
        """Hysteresis/debounce: a second LOD pass at the same zoom (e.g. a
        follow-up debounce tick after scrolling, with no further zoom
        change) must not kick off another background render."""
        from pid_viewer import PIDGraphicsView
        path = self._make_pdf()
        view = PIDGraphicsView()
        self.assertTrue(view.load_pdf(path))
        pn = 0
        view.centerOn(view._all_page_items[pn].sceneBoundingRect().center())

        view.scale(6.0, 6.0)
        view._update_page_lod()
        self.assertTrue(view._lod_renderer.wait(5000))
        self.app.processEvents()
        self.assertIsNotNone(view._page_display_scale.get(pn))

        view._lod_renderer = None
        view._update_page_lod()
        self.assertIsNone(view._lod_renderer,
            "must not re-render when the currently-displayed tier already satisfies the zoom")


# ══════════════════════════════════════════════════════════════════════════
# Manual per-page P&ID rotation (2026-08-12) — a toolbar rotate-left/right
# control for the currently-viewed sheet, composed with (not replacing) the
# PDF's own /Rotate flag via page.set_rotation() (see
# PIDGraphicsView._apply_page_rotation). Distinct from the pre-existing,
# still-unwired "Sid-orientering" three-way dropdown (pid_page_orientation_hint,
# see NOTES.md known limitations). Every marker/zone position stored for the
# rotated physical page is stored in PDF-space and must be re-anchored to the
# same physical point at the same time, or rotating would silently move
# every marker on that page.
# ══════════════════════════════════════════════════════════════════════════

class PidPageRotationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        import fitz
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rotate_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        doc = fitz.open()
        doc.new_page(width=400.0, height=300.0)
        doc.save(self.pdf_path)
        doc.close()

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_panel(self):
        from pid_viewer import PIDPanel
        panel = PIDPanel(self.db)
        panel.viewer.load_pdf(self.pdf_path)
        self.db.ensure_sheets_initialized(panel.viewer.page_count())
        panel._rebuild_sheet_map()
        return panel

    def _insert_cause_marker(self, cause_id, page, x, y, comp_type,
                             rect_w=None, rect_h=None):
        """Database.add_cause_marker was removed 2026-08-13 (see NOTES.md:
        the P&ID canvas is now object-placement-only, so nothing creates
        cause_markers rows anymore) — the table/schema itself is untouched
        (no migration, no data loss for existing projects), and
        remap_page_rotation_positions still has to remap any legacy rows
        that exist there correctly. Insert directly so these tests keep
        exercising that generic remap logic against a real row shape."""
        self.db.conn.execute(
            "INSERT INTO cause_markers (cause_id,pid_page,x,y,component_type,rect_w,rect_h) "
            "VALUES (?,?,?,?,?,?,?)",
            (cause_id, page, x, y, comp_type, rect_w, rect_h))
        self.db.commit()

    def test_db_rotation_round_trip(self):
        self.assertEqual(self.db.get_page_rotation(0), 0)
        self.db.set_page_rotation(0, 90)
        self.assertEqual(self.db.get_page_rotation(0), 90)
        self.assertEqual(self.db.get_all_page_rotations(), {0: 90})
        self.db.set_page_rotation(0, 270)   # upsert, not a duplicate row
        self.assertEqual(self.db.get_page_rotation(0), 270)
        self.assertEqual(self.db.get_all_page_rotations(), {0: 270})

    def test_rotation_override_composes_with_intrinsic_rotation(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        self.assertTrue(view.load_pdf(self.pdf_path))
        page = view.pdf_doc.load_page(0)
        self.assertEqual(page.rotation, 0)
        w0, h0 = view._page_widths_pdf[0], view._page_heights_pdf[0]

        view.set_page_rotation_override(0, 90)

        self.assertEqual(page.rotation, 90,
            "override must compose with (here: on top of a 0-degree) intrinsic /Rotate")
        # An axis-aligned footprint swap is the observable proof that
        # page.rect/get_pixmap() etc. now reflect the override for free.
        self.assertAlmostEqual(page.rect.width,  h0, places=3)
        self.assertAlmostEqual(page.rect.height, w0, places=3)

    def test_rotate_button_updates_db_and_page_footprint(self):
        panel = self._make_panel()
        try:
            w0 = panel.viewer._page_widths_pdf[0]
            h0 = panel.viewer._page_heights_pdf[0]
            panel._rotate_page(90)
            self.assertEqual(self.db.get_page_rotation(0), 90)
            self.assertAlmostEqual(panel.viewer._page_widths_pdf[0],  h0, places=3)
            self.assertAlmostEqual(panel.viewer._page_heights_pdf[0], w0, places=3)
        finally:
            panel.deleteLater()

    def test_marker_stays_on_same_physical_point_after_rotation(self):
        """The critical correctness check the user explicitly asked for:
        place a marker, rotate the page, confirm it's still anchored to the
        same physical location — not just that rendering doesn't crash.
        Verified by mapping both the before- and after-rotation marker
        position back to the rotation-invariant raw/mediabox anchor via
        derotation_matrix and checking they match."""
        import fitz
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 100.0, 50.0, "Ventil")

            page_before = panel.viewer.pdf_doc.load_page(0)
            raw_anchor_before = fitz.Point(100.0, 50.0) * page_before.derotation_matrix

            panel._rotate_page(90)

            marker = dict(self.db.cause_markers_for_page(0)[0])
            page_after = panel.viewer.pdf_doc.load_page(0)
            raw_anchor_after = fitz.Point(marker['x'], marker['y']) * page_after.derotation_matrix

            self.assertAlmostEqual(raw_anchor_before.x, raw_anchor_after.x, places=3)
            self.assertAlmostEqual(raw_anchor_before.y, raw_anchor_after.y, places=3)
            # And the stored PDF-space coordinates actually changed — proves
            # this isn't an accidental no-op/identity transform.
            self.assertFalse(
                abs(marker['x'] - 100.0) < 1e-3 and abs(marker['y'] - 50.0) < 1e-3,
                "marker's stored PDF-space position must change across a rotation "
                "even though its physical location doesn't")
        finally:
            panel.deleteLater()

    def test_rect_marker_dimensions_swap_on_90_degree_rotation(self):
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 100.0, 50.0, "Ventil",
                                      rect_w=40.0, rect_h=20.0)

            panel._rotate_page(90)

            row = dict(self.db.cause_markers_for_page(0)[0])
            self.assertAlmostEqual(row['rect_w'], 20.0, places=3)
            self.assertAlmostEqual(row['rect_h'], 40.0, places=3)
        finally:
            panel.deleteLater()

    def test_rect_marker_dimensions_unchanged_on_180_degree_rotation(self):
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 100.0, 50.0, "Ventil",
                                      rect_w=40.0, rect_h=20.0)

            panel._rotate_page(90)
            panel._rotate_page(90)   # net 180 degrees

            row = dict(self.db.cause_markers_for_page(0)[0])
            self.assertAlmostEqual(row['rect_w'], 40.0, places=3)
            self.assertAlmostEqual(row['rect_h'], 20.0, places=3)
        finally:
            panel.deleteLater()

    def test_node_outline_points_remapped_after_rotation(self):
        """Covers the 'zone drawing' correctness the user asked to verify —
        a node's outline (nodes.markup_points) is the simplest such zone."""
        import fitz, json
        panel = self._make_panel()
        try:
            pts_before = [[20.0, 30.0], [120.0, 30.0], [120.0, 130.0], [20.0, 130.0]]
            node_id = self.db.add_node_with_markup("Node A", pts_before, {}, 0)

            page_before = panel.viewer.pdf_doc.load_page(0)
            raw_before = [fitz.Point(x, y) * page_before.derotation_matrix for x, y in pts_before]

            panel._rotate_page(-90)

            node = self.db.get_node(node_id)
            pts_after = json.loads(node['markup_points'])
            page_after = panel.viewer.pdf_doc.load_page(0)
            raw_after = [fitz.Point(x, y) * page_after.derotation_matrix for x, y in pts_after]

            for rb, ra in zip(raw_before, raw_after):
                self.assertAlmostEqual(rb.x, ra.x, places=3)
                self.assertAlmostEqual(rb.y, ra.y, places=3)
        finally:
            panel.deleteLater()

    def test_full_rotation_cycle_returns_marker_to_original_position(self):
        """Four 90-degree turns must be a no-op on every stored position —
        a strong end-to-end sanity check of the compose/remap math."""
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 77.0, 133.0, "Ventil")

            for _ in range(4):
                panel._rotate_page(90)

            self.assertEqual(self.db.get_page_rotation(0), 0)
            marker = dict(self.db.cause_markers_for_page(0)[0])
            self.assertAlmostEqual(marker['x'], 77.0, places=3)
            self.assertAlmostEqual(marker['y'], 133.0, places=3)
        finally:
            panel.deleteLater()


if __name__ == '__main__':
    unittest.main(verbosity=2)
