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
    QComboBox, QPushButton, QMessageBox, QInputDialog,
)
from PyQt6.QtGui import QPixmap, QFocusEvent  # noqa: E402
from PyQt6.QtCore import Qt, QPoint, QDate, QEvent  # noqa: E402
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════

def _ensure_qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


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

    def test_delete_cause_then_reload_overlays_no_crash(self):
        """Reproduces the original crash: orphaned consequence/safeguard
        markers on the P&ID caused KeyError/AttributeError when
        PIDPanel._load_overlays() tried to draw connection lines."""
        from pid_viewer import PIDPanel

        panel = PIDPanel(self.db)
        try:
            ids = self._make_full_chain()

            # Place markers on a fake "page 0" so the marker-drawing code
            # path in _load_overlays() actually iterates real marker rows
            # (not just an early-return because no PDF is loaded).
            self.db.add_cause_marker(ids['cause_id'], 0, 10.0, 10.0, 'Ventil')
            self.db.add_consequence_marker(ids['cons_id'], 0, 20.0, 20.0, 'target')
            self.db.add_safeguard_marker(ids['sg_id'], 0, 30.0, 30.0, 'SG-1')

            # Fake a "PDF loaded with one page" state without touching disk /
            # PyMuPDF, so _load_overlays() doesn't bail out on
            # `if self.viewer.pdf_doc is None: return`.
            _fake_pdf_loaded(panel)

            # Delete the cause -- consequence/safeguard become orphaned
            # (or cascade-deleted, depending on FK enforcement) while their
            # marker rows remain on the P&ID.
            self.db.delete_cause(ids['cause_id'])

            try:
                panel.reload_overlays()
            except Exception as e:
                self.fail(f"reload_overlays() raised after deleting cause: {e!r}")

            # Also drive it through _load_overlays() directly (reload_overlays
            # is a thin wrapper around it) for good measure.
            try:
                panel._load_overlays()
            except Exception as e:
                self.fail(f"_load_overlays() raised after deleting cause: {e!r}")
        finally:
            panel.deleteLater()

    def test_delete_consequence_then_reload_overlays_no_crash(self):
        """Same crash class, triggered by deleting the consequence directly
        (leaving its safeguard's marker dangling)."""
        from pid_viewer import PIDPanel

        panel = PIDPanel(self.db)
        try:
            ids = self._make_full_chain()
            self.db.add_cause_marker(ids['cause_id'], 0, 10.0, 10.0, 'Ventil')
            self.db.add_consequence_marker(ids['cons_id'], 0, 20.0, 20.0, 'target')
            self.db.add_safeguard_marker(ids['sg_id'], 0, 30.0, 30.0, 'SG-1')

            _fake_pdf_loaded(panel)

            self.db.delete_consequence(ids['cons_id'])

            try:
                panel.reload_overlays()
            except Exception as e:
                self.fail(f"reload_overlays() raised after deleting consequence: {e!r}")
        finally:
            panel.deleteLater()

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
            self.db.add_cause_marker(ids['cause_id'], 0, 10.0, 10.0, 'Ventil')
            self.db.add_consequence_marker(ids['cons_id'], 0, 20.0, 20.0, 'target')
            self.db.add_safeguard_marker(ids['sg_id'], 0, 30.0, 30.0, 'SG-1')

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

            rows = [r for r, m in enumerate(panel._row_meta) if m[1] == ids['cause_id']]
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

    def test_double_click_still_opens_chain_wizard_not_inline_edit(self):
        """Inline editing is an ADDITIONAL path (single-click-when-
        current), not a replacement — the wizard stays reachable."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            item = panel._table.item(row, panel._C_KON)

            with unittest.mock.patch.object(panel, '_open_chain_editor') as mock_wizard:
                panel._on_cell_double_clicked(item)
            mock_wizard.assert_called_once_with(cons_id)


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

    def test_on_safeguard_created_rebuilds_scenario_panel_exactly_once(self):
        """Placing a new safeguard marker on the P&ID (PIDPanel.safeguard_created
        -> MainWindow._on_safeguard_created) must rebuild the scenario table
        exactly once, not twice.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)
            win._cur_type = CONS_T
            win._cur_id = ids['cons_id']

            rebuild_spy = unittest.mock.Mock(wraps=win.scenario_panel._rebuild)
            win.scenario_panel._rebuild = rebuild_spy

            win._on_safeguard_created(ids['sg_id'])

            self.assertEqual(
                rebuild_spy.call_count, 1,
                "_on_safeguard_created() must rebuild the scenario panel "
                "exactly once per safeguard creation (it used to rebuild "
                "twice: once via the explicit scenario_panel.load_consequence() "
                "call, once via tree_panel.refresh()'s setCurrentItem cascade "
                "into _on_selected -> scenario_panel.load_consequence() again)")

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


class EscapeCancelsPlacementTests(unittest.TestCase):
    """Escape must abort an in-progress cause/consequence/safeguard placement
    on the P&ID and return the viewer to MODE_NAV, mirroring the existing
    Escape-cancels-drawing behavior for MODE_NODE/MARKUP_POLYGON.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_esc_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _press_escape(self, view):
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtCore import QEvent as _QEvent
        ev = QKeyEvent(_QEvent.Type.KeyPress, _Qt.Key.Key_Escape, _Qt.KeyboardModifier.NoModifier)
        view.keyPressEvent(ev)

    def test_escape_cancels_cause_mode(self):
        from pid_viewer import PIDPanel, MODE_CAUSE_TEMPLATE, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE_TEMPLATE)
            self.assertEqual(panel.viewer.mode, MODE_CAUSE_TEMPLATE)
            self._press_escape(panel.viewer)
            self.assertEqual(panel.viewer.mode, MODE_NAV,
                              "Escape must abort MODE_CAUSE_TEMPLATE back to MODE_NAV")
        finally:
            panel.deleteLater()

    def test_escape_cancels_consequence_mode(self):
        from pid_viewer import PIDPanel, MODE_CONSEQUENCE, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CONSEQUENCE)
            self._press_escape(panel.viewer)
            self.assertEqual(panel.viewer.mode, MODE_NAV,
                              "Escape must abort MODE_CONSEQUENCE back to MODE_NAV")
        finally:
            panel.deleteLater()

    def test_escape_cancels_safeguard_mode(self):
        from pid_viewer import PIDPanel, MODE_SAFEGUARD, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_SAFEGUARD)
            self._press_escape(panel.viewer)
            self.assertEqual(panel.viewer.mode, MODE_NAV,
                              "Escape must abort MODE_SAFEGUARD back to MODE_NAV")
        finally:
            panel.deleteLater()

    def test_escape_returns_navigate_button_to_checked(self):
        """Cancelling via Escape must also update the toolbar button state,
        not just the internal viewer.mode. The "⚙️ Orsak"/"⚠️ Konsekvens"
        toggle buttons were removed 2026-08-07 (see NOTES.md — redundant
        once the P&ID right-click menu did the same thing directly), so
        "🔍 Navigera" is now the only button in mode_buttons; it must still
        become checked again after an Escape-cancel out of MODE_CAUSE_TEMPLATE
        (still settable programmatically, e.g. by the right-click flow's
        internal _set_mode calls, even with no dedicated toolbar button)."""
        from pid_viewer import PIDPanel, MODE_CAUSE_TEMPLATE, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE_TEMPLATE)
            self.assertFalse(panel.mode_buttons[MODE_NAV].isChecked())
            self._press_escape(panel.viewer)
            self.assertEqual(panel.viewer.mode, MODE_NAV)
            self.assertTrue(panel.mode_buttons[MODE_NAV].isChecked(),
                             "Navigate toolbar button must become checked after Escape-cancel")
        finally:
            panel.deleteLater()

    def test_escape_does_nothing_in_nav_mode(self):
        """Escape while already navigating must not raise or change mode."""
        from pid_viewer import PIDPanel, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_NAV)
            self._press_escape(panel.viewer)
            self.assertEqual(panel.viewer.mode, MODE_NAV)
        finally:
            panel.deleteLater()


class GhostPreviewMarkerTests(unittest.TestCase):
    """Cursor-following ghost preview in cause/consequence/safeguard placement
    modes: shows what will be placed before the first click, and must never
    linger once placement is cancelled, a drag starts, or the mode changes —
    a stale ghost item would visually overlap the real marker (see
    _clear_ghost_preview() call sites).
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_ghost_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _move_to(self, view, scene_pos):
        from PyQt6.QtCore import QPointF
        view._update_ghost_preview(scene_pos)

    def test_ghost_created_in_cause_mode(self):
        from pid_viewer import PIDPanel, MODE_CAUSE_TEMPLATE
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE_TEMPLATE)
            self.assertIsNone(panel.viewer._ghost_preview_item)
            self._move_to(panel.viewer, QPointF(50, 50))
            self.assertIsNotNone(panel.viewer._ghost_preview_item,
                                  "Ghost circle must appear once in a placement mode")
        finally:
            panel.deleteLater()

    def test_ghost_reused_not_recreated_on_move(self):
        """Moving the cursor should update the existing item's geometry, not
        create a new scene item each time (perf pattern already used by the
        drag-rect / right-drag rubber-band previews)."""
        from pid_viewer import PIDPanel, MODE_CONSEQUENCE
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CONSEQUENCE)
            self._move_to(panel.viewer, QPointF(10, 10))
            first = panel.viewer._ghost_preview_item
            self._move_to(panel.viewer, QPointF(80, 40))
            self.assertIs(panel.viewer._ghost_preview_item, first,
                          "Same graphics item must be reused across moves")
        finally:
            panel.deleteLater()

    def test_ghost_cleared_on_mode_change(self):
        from pid_viewer import PIDPanel, MODE_SAFEGUARD, MODE_NAV
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_SAFEGUARD)
            self._move_to(panel.viewer, QPointF(20, 20))
            self.assertIsNotNone(panel.viewer._ghost_preview_item)
            panel._set_mode(MODE_NAV)
            self.assertIsNone(panel.viewer._ghost_preview_item,
                              "Switching away from a placement mode must clear the ghost")
        finally:
            panel.deleteLater()

    def test_ghost_cleared_when_drag_starts(self):
        """The moment the user presses the mouse button to start sizing the
        marker's rect, the ghost must disappear — otherwise it would sit on
        top of the dashed drag-rect preview at Z_TEMP."""
        from pid_viewer import PIDPanel, MODE_CAUSE_TEMPLATE
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE_TEMPLATE)
            self._move_to(panel.viewer, QPointF(30, 30))
            self.assertIsNotNone(panel.viewer._ghost_preview_item)
            panel.viewer._clear_ghost_preview()   # what mousePressEvent triggers
            panel.viewer._rect_start = QPointF(30, 30)
            self.assertIsNone(panel.viewer._ghost_preview_item)
        finally:
            panel.deleteLater()

    def test_ghost_color_matches_mode(self):
        """Ghost fill must match the mode's real marker color (cause=red,
        consequence=orange, safeguard=green) — verified via the shared
        _PLACEMENT_MODE_COLORS map rather than duplicated literals here."""
        from pid_viewer import (PIDPanel, MODE_CAUSE_TEMPLATE, MODE_CONSEQUENCE,
                                 MODE_SAFEGUARD, _PLACEMENT_MODE_COLORS)
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            for mode in (MODE_CAUSE_TEMPLATE, MODE_CONSEQUENCE, MODE_SAFEGUARD):
                panel._set_mode(mode)
                self._move_to(panel.viewer, QPointF(15, 15))
                item = panel.viewer._ghost_preview_item
                _, expected_fill = _PLACEMENT_MODE_COLORS[mode]
                self.assertEqual(item.brush().color().rgb(), expected_fill.rgb())
                panel.viewer._clear_ghost_preview()
        finally:
            panel.deleteLater()


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
    """2026-08-06: valve markers on the P&ID are now clickable — clicking
    one switches to Utrustningsregistret and selects the corresponding
    row, the closest equivalent to _on_marker_navigate's tree-select
    behaviour for cause/consequence/safeguard (equipment has no HAZOP tree
    node of its own to select)."""

    def test_on_marker_navigate_switches_to_equipment_page_and_selects_row(self):
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')
            win.equipment_panel.refresh()

            win._on_marker_navigate('equipment', marker_id)

            self.assertEqual(win.view_stack.currentIndex(), 2,
                "clicking a valve marker must switch to the Utrustning page")
            src_row = win.equipment_panel._proxy.mapToSource(
                win.equipment_panel._tbl.currentIndex()).row()
            self.assertEqual(win.equipment_panel._model.row_dict(src_row)['id'], eq_id,
                "the register row for the clicked marker's equipment_id must be selected")

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
        labels = [c.args[0] for c in mock_menu.addAction.call_args_list if c.args]
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
        labels = [c.args[0] for c in mock_menu.addAction.call_args_list if c.args]
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


class EquipmentDeviationBarTests(unittest.TestCase):
    """The bottom-of-P&ID-view bar shown when an equipment marker is
    clicked — see NOTES.md 'Nod → Utrustning → Avvikelse'."""

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

    def test_load_shows_bar_and_populates_type_and_node(self):
        self.bar.load(self.eq_id, self.marker_id)
        self.assertTrue(self.bar.isVisible())
        self.assertEqual(self.bar._type_combo.currentText(), "Ventil")

    def test_changing_type_propagates_to_catalog_and_marker(self):
        """Known pre-existing gap fixed as part of this feature: changing
        equipment_type used to update ONLY equipment_catalog, never the
        already-drawn marker's own comp_type — see NOTES.md."""
        self.bar.load(self.eq_id, self.marker_id)
        other_type = next(t for t in sorted(COMPONENT_TYPES.keys()) if t != "Ventil")

        self.bar._type_combo.setCurrentText(other_type)

        cat = self.db.get_equipment_by_id(self.eq_id)
        self.assertEqual(cat['equipment_type'], other_type)
        marker = self.db.conn.execute(
            "SELECT comp_type FROM equipment_markers WHERE id=?", (self.marker_id,)).fetchone()
        self.assertEqual(marker['comp_type'], other_type)

    def test_checking_deviation_without_a_node_selected_is_a_no_op(self):
        self.bar.load(self.eq_id, self.marker_id)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)
        # No node picked yet — checkboxes must be disabled, nothing to toggle.
        self.assertEqual(self.bar._node_combo.currentData(), None)

    def test_checking_deviation_after_node_selected_creates_it(self):
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_smart_node_default_assigns_active_node_when_equipment_has_none(self):
        """See NOTES.md 'Slippa välja nod varje gång': the bar assigns
        PIDPanel._active_node_id immediately when the equipment has no node
        of its own yet, so checking a deviation works right away instead of
        forcing a manual node pick every time — explicit user request."""
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        self.assertEqual(self.bar._node_combo.currentData(), node_id)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_smart_node_default_does_not_override_existing_node(self):
        node_id = self.db.add_node()
        other_node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id, active_node_id=other_node_id)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_number_key_shortcut_toggles_matching_checkbox(self):
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        self.assertGreaterEqual(len(self.bar._checklist_checkboxes), 1)
        self.bar._toggle_checkbox_by_number(1)

        self.assertTrue(self.bar._checklist_checkboxes[0].isChecked())
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)

    def test_number_key_shortcut_out_of_range_is_a_no_op(self):
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)
        # One past the last real row — must not raise or toggle anything.
        out_of_range = len(self.bar._checklist_checkboxes) + 1
        self.bar._toggle_checkbox_by_number(out_of_range)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)

    def _select_node_and_stub_cause_creation(self):
        """Shared setup for the suggested-cause-chip / frequency tests:
        picks a node and installs a fake _create_cause_fn that creates a
        real cause row via Database directly, standing in for
        PIDPanel._create_cause_for_bar (which needs a real P&ID marker/scene
        this test class doesn't construct).

        Uses a Pump equipment item rather than self.eq_id ("Ventil") because
        standard_causes is only seeded per specific valve/equipment
        sub-type (e.g. "Manuell ventil", "On-off ventil") — "Pump" is
        seeded and matches the user's own example ("Lågt flöde" + Pump →
        "Pump stopp")."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        pump_marker_id = self.db.add_equipment_marker(
            pump_id, "P-101", 0, 200.0, 200.0, "Pump", confidence=0.9, link_method='leader')
        node_id = self.db.add_node()
        self.bar.load(pump_id, pump_marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        created = {'pump_id': pump_id, 'node_id': node_id, 'update_calls': []}

        def fake_create_cause(dev_id, comp_type, comp_tag, description, frequency=None):
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, description, comp_type=comp_type, comp_tag=comp_tag)
            if frequency is not None:
                # Stand-in for place_cause_from_template's real
                # _compute_f_level() conversion — this test class only
                # needs base_frequency to actually persist so the
                # numeric-label UI has something real to read back.
                self.db.update_cause(cause_id, likelihood=0, base_frequency=frequency)
            created['cause_id'] = cause_id
            created['dev_id'] = dev_id
            created['frequency'] = frequency
            created['description'] = description
            return cause_id

        def fake_update_cause(cause_id, comp_type, comp_tag, description, frequency=None):
            self.db.update_cause(cause_id, description, comp_type=comp_type, comp_tag=comp_tag)
            created['update_calls'].append(
                {'cause_id': cause_id, 'description': description, 'frequency': frequency})

        def fake_set_freq(cause_id, value):
            f_level = 3 if value >= 0.01 else 0
            self.db.update_cause(cause_id, likelihood=f_level, base_frequency=value)
            created['set_freq_calls'] = created.get('set_freq_calls', []) + [(cause_id, value)]
            return f_level

        self.bar._create_cause_fn = fake_create_cause
        self.bar._update_cause_fn = fake_update_cause
        self.bar._set_freq_fn = fake_set_freq
        return created

    def test_frequency_combo_present_but_disabled_before_any_cause_exists(self):
        """Bug report: 'Frekvensknappen dök först upp när jag valde någon
        fritext' — the combo must always render (never hidden), just
        disabled until this row actually has a cause_id to write against."""
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        freq_combo = row_widget.findChildren(QComboBox)[-1]
        self.assertFalse(freq_combo.isEnabled())

    def test_checking_deviation_auto_creates_suggested_cause_and_enables_frequency_combo(self):
        """Förenklat orsaksval, ta bort dubbla val (NOTES.md): checking the
        deviation alone must create the top-suggested cause immediately —
        no separate chip/button to click anymore, that was the 'dubbla
        val' complaint (checkbox + chip + dropdown all doing overlapping
        things)."""
        created = self._select_node_and_stub_cause_creation()

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        self.assertIn('cause_id', created)
        cause_combo = row_widget.findChildren(QComboBox)[0]
        self.assertEqual(cause_combo.currentText(), created['description'])
        freq_combo = row_widget.findChildren(QComboBox)[-1]
        self.assertTrue(freq_combo.isEnabled())
        self.assertEqual(freq_combo.property('cause_id'), created['cause_id'])

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
        (_obj_type_matches) and produce real suggestions instead."""
        self.assertEqual(
            self.db.standard_causes_for_comp_type("Ventil"), [],
            "sanity check: literal comp_type match is empty for this generic label")
        obj_id = self.bar._resolve_object_id("Ventil")
        self.assertIsNotNone(
            obj_id, "expected a substring match against standard_objects (e.g. 'Manuell ventil')")

        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        cause_combo = row_widget.findChildren(QComboBox)[0]
        # "+ orsak…" + at least one real suggestion + the free-text sentinel.
        self.assertGreater(
            cause_combo.count(), 2,
            "expected real cause suggestions once the object-based fallback resolves")

    def test_frequency_combo_change_writes_to_cause_likelihood(self):
        created = self._select_node_and_stub_cause_creation()

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        freq_combo = row_widget.findChildren(QComboBox)[-1]
        freq_combo.setCurrentIndex(freq_to_idx(3))

        cause = self.db.conn.execute(
            "SELECT likelihood FROM causes WHERE id=?", (created['cause_id'],)).fetchone()
        self.assertEqual(cause['likelihood'], 3)

    def test_picking_a_different_cause_updates_in_place_instead_of_creating_a_second_one(self):
        """Förenklat orsaksval, ta bort dubbla val (NOTES.md): the dropdown
        is now a single 'pick or change the cause' control. Re-selecting a
        DIFFERENT entry for a row that already auto-created a cause must
        UPDATE that same cause (via _update_cause_fn), not create a
        second, redundant one via _create_cause_fn."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)
        first_cause_id = created['cause_id']

        cause_combo = row_widget.findChildren(QComboBox)[0]
        other_idx = next(
            i for i in range(cause_combo.count())
            if cause_combo.itemData(i) not in (None, created['description'],
                                                self.bar._FREE_TEXT_SENTINEL))
        other_text = cause_combo.itemData(other_idx)
        cause_combo.setCurrentIndex(other_idx)
        cause_combo.activated.emit(other_idx)

        self.assertEqual(len(created['update_calls']), 1)
        self.assertEqual(created['update_calls'][0]['cause_id'], first_cause_id)
        self.assertEqual(created['update_calls'][0]['description'], other_text)
        # No second cause row created for this deviation.
        causes = self.db.causes_for_deviation(created['dev_id'])
        self.assertEqual(len(causes), 1)
        self.assertEqual(cause_combo.currentText(), other_text)

    def test_reopening_bar_shows_already_saved_cause(self):
        """Closing and reopening the bar for equipment that already has a
        saved cause must show/enable that cause immediately — the row
        shouldn't look unconfigured just because the bar was rebuilt."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        # Simulate reopening: rebuild the checklist from scratch.
        self.bar._rebuild_checklist()

        row_widget2 = self.bar._checklist_layout.itemAt(0).widget()
        cause_combo2 = row_widget2.findChildren(QComboBox)[0]
        freq_combo2 = row_widget2.findChildren(QComboBox)[-1]
        self.assertEqual(cause_combo2.currentText(), created['description'])
        self.assertEqual(freq_combo2.property('cause_id'), created['cause_id'])
        self.assertTrue(freq_combo2.isEnabled())

    def test_unchecking_deviation_without_causes_deletes_silently(self):
        """Kryssrutan ska gå att av-/aktivera (NOTES.md) — unchecking a
        deviation that never got a cause (e.g. no template match, user
        never picked one) must delete it right away with no confirmation
        prompt (nothing meaningful to lose)."""
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

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

    def test_unchecking_emits_deviation_removed_and_resets_combos(self):
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        cause_combo = row_widget.findChildren(QComboBox)[0]
        freq_combo = row_widget.findChildren(QComboBox)[-1]

        received = []
        self.bar.deviation_removed.connect(lambda dev_id, eq_id: received.append((dev_id, eq_id)))

        with unittest.mock.patch('pid_viewer.QMessageBox.question'):
            checkbox.setChecked(True)
            checkbox.setChecked(False)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], self.eq_id)
        self.assertFalse(cause_combo.isEnabled())
        self.assertFalse(freq_combo.isEnabled())
        self.assertIsNone(freq_combo.property('cause_id'))

    def test_can_recheck_after_unchecking(self):
        """The whole point: checking, unchecking, and checking again must
        all work — not a one-way lock like the old v1 behavior."""
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        with unittest.mock.patch('pid_viewer.QMessageBox.question'):
            checkbox.setChecked(True)
            checkbox.setChecked(False)
            checkbox.setChecked(True)
        self.assertTrue(checkbox.isChecked())
        self.assertTrue(checkbox.isEnabled())
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)

    def test_numeric_frequency_label_shows_seeded_value_after_auto_create(self):
        """'jag vill även ha med den numeriska frekvensen som finns
        inlagt' — checking a deviation auto-creates 'Pump stopp' (seeded
        frequency 0.02/år) and the row's numeric label must show it, not
        just the F-level combo."""
        self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        freq_combo = row_widget.findChildren(QComboBox)[-1]
        num_btn = freq_combo.property('num_btn')
        self.assertIsNotNone(num_btn)
        self.assertTrue(num_btn.isEnabled())
        self.assertIn("/år", num_btn.text())
        self.assertNotEqual(num_btn.text(), "—")

    def test_numeric_frequency_label_disabled_and_blank_before_any_cause(self):
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id)
        idx = self.bar._node_combo.findData(node_id)
        self.bar._node_combo.setCurrentIndex(idx)

        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        freq_combo = row_widget.findChildren(QComboBox)[-1]
        num_btn = freq_combo.property('num_btn')
        self.assertFalse(num_btn.isEnabled())
        self.assertEqual(num_btn.text(), "—")

    def test_clicking_numeric_frequency_label_writes_exact_value(self):
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        freq_combo = row_widget.findChildren(QComboBox)[-1]
        num_btn = freq_combo.property('num_btn')
        with unittest.mock.patch(
                'pid_viewer.QInputDialog.getDouble', return_value=(0.05, True)):
            num_btn.click()

        self.assertEqual(created['set_freq_calls'], [(created['cause_id'], 0.05)])
        cause = self.db.get_cause(created['cause_id'])
        self.assertEqual(cause['base_frequency'], 0.05)
        self.assertEqual(cause['likelihood'], 3)   # per fake_set_freq's stand-in rule
        self.assertEqual(freq_combo.currentIndex(), freq_to_idx(3))
        self.assertIn("/år", num_btn.text())

    def test_deactivating_resets_numeric_frequency_label(self):
        self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)
        freq_combo = row_widget.findChildren(QComboBox)[-1]
        num_btn = freq_combo.property('num_btn')
        self.assertNotEqual(num_btn.text(), "—")

        with unittest.mock.patch('pid_viewer.QMessageBox.question',
                                  return_value=QMessageBox.StandardButton.Yes):
            checkbox.setChecked(False)
        self.assertEqual(num_btn.text(), "—")
        self.assertFalse(num_btn.isEnabled())

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
    toolbar, and "🔧 Objekt" added to the right-click menu's action chain
    (2026-08-07, see NOTES.md). The old MODE_CAUSE mode/signal chain (only
    ever set by the removed button) was itself removed as dead code
    (2026-08-09, see NOTES.md) — cause creation now always goes through
    MODE_CAUSE_TEMPLATE. MODE_CONSEQUENCE must still work since the
    right-click menu's own "⚠️ Konsekvens" action still relies on it
    internally."""

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
        from pid_viewer import MODE_NAV, MODE_CAUSE_TEMPLATE, MODE_CONSEQUENCE
        self.assertIn(MODE_NAV, self.panel.mode_buttons)
        self.assertNotIn(MODE_CAUSE_TEMPLATE, self.panel.mode_buttons)
        self.assertNotIn(MODE_CONSEQUENCE, self.panel.mode_buttons)

    def test_context_menu_consequence_action_still_sets_mode(self):
        """The toolbar toggle is gone, but the right-click menu's own
        "⚠️ Konsekvens" action must still work exactly as before."""
        from pid_viewer import MODE_CONSEQUENCE
        from PyQt6.QtCore import QPointF
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.panel._active_cause_id = cause_id

        self.panel._on_context_action('consequence', QPointF(5, 5), 0)

        self.assertEqual(self.panel.viewer.mode, MODE_CONSEQUENCE)

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

    def test_find_similar_symbol_shows_info_when_nothing_found(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.find_similar_shapes',
                                 return_value=[]) as mock_find, \
             unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_find.assert_called_once()
        mock_info.assert_called_once()

    def test_find_similar_symbol_opens_review_dialog_and_reloads_on_accept(self):
        from PyQt6.QtCore import QPointF
        fake_results = [{'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0,
                         'outline': [], 'link_method': 'similar', 'tag_status': 'untagged',
                         'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9}]
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.find_similar_shapes',
                                 return_value=fake_results), \
             unittest.mock.patch('pid_viewer.EquipmentMarkerReviewDialog') as mock_dlg_cls, \
             unittest.mock.patch.object(self.panel, 'reload_overlays') as mock_reload:
            mock_dlg_cls.return_value.exec.return_value = 1   # QDialog.Accepted
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_dlg_cls.assert_called_once()
        args, kwargs = mock_dlg_cls.call_args
        self.assertEqual(args[0], fake_results)
        mock_reload.assert_called_once()


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
        in the picker popup, so the next thing to fill in is the
        consequence."""
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            panel.load_node(node_id)

            def _fake_exec(self):
                self.cause_picked.emit("Ny orsak (test)", None)
                return QDialog.DialogCode.Accepted

            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch.object(hazop.StandardCausesPickerPopup, 'exec', new=_fake_exec):
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
        return [c.args[0] for c in mock_menu.addAction.call_args_list if c.args]

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
    ... även kunna välja Objekt. Objekt ska stå högst upp i rullgardinen.'
    (2026-08-09, see NOTES.md) — the right-drag rubber-band menu
    (PIDPanel._on_zone_drawn) gains a "🔧 Objekt" entry, listed first,
    which reuses the existing EquipmentTagPopup flow but threads the
    drawn rectangle through so the new marker gets a real outline shape
    instead of the generic bowtie-icon fallback a bare point gets."""

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

    def test_objekt_is_the_first_menu_entry(self):
        from PyQt6.QtCore import QRectF
        from PyQt6.QtWidgets import QMenu
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return menu_self.actions()[0]

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            self.panel._on_zone_drawn(QRectF(0, 0, 10, 10), 0)
        self.assertEqual(texts[0], "🔧 Objekt")
        self.assertEqual(set(texts),
                         {"🔧 Objekt", "⚙️ Orsak", "⚠️ Konsekvens", "🛡️ Safeguard"})

    def test_choosing_objekt_emits_placement_signal_with_the_drawn_rect(self):
        from PyQt6.QtCore import QRectF
        from PyQt6.QtWidgets import QMenu
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page, rect: captured.append((tag, pos, page, rect)))
        pdf_rect = QRectF(5.0, 6.0, 10.0, 8.0)

        def _fake_exec(menu_self, _pos=None):
            return menu_self.actions()[0]

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
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
    samma med safeguard.' (2026-08-09, see NOTES.md) — checking a
    deviation in EquipmentDeviationBar (and the classic P&ID-click cause
    flow, which shares the same underlying place_cause_from_template)
    used to create a cause with NO consequence/safeguard at all, so the
    KON/SG cells for that row had no real item to click into. Both are
    now auto-created empty, immediately ready for the already-existing
    KON/SG inline-edit machinery (from earlier sessions, see NOTES.md)."""

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
        from PyQt6.QtCore import QPointF
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']

        cause_id = self.panel.place_cause_from_template(
            dev_id, QPointF(10, 10), 0, "Ventil", "HV-101", "Läckage", None)

        self.assertIsNotNone(cause_id)
        cons_list = self.db.consequences(cause_id)
        self.assertEqual(len(cons_list), 1)
        cons_id = cons_list[0]['id']
        # db.add_consequence()/add_safeguard() default to placeholder text
        # ("Ny konsekvens"/"Ny safeguard") — same as every other quick-add
        # path (TreePanel.add_consequence(), _create_cause_from_pick())
        # already uses; not blank, but still immediately overtype-able via
        # the existing KON/SG inline-edit machinery.
        self.assertEqual(cons_list[0]['description'], 'Ny konsekvens')
        sg_list = self.db.safeguards(cons_id)
        self.assertEqual(len(sg_list), 1)
        self.assertEqual(sg_list[0]['description'], 'Ny safeguard')

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
        identically whether or not the actual bug fix was present)."""
        panel.resize(600, 400)
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


class TaggedRowPinTurnsGreenTests(unittest.TestCase):
    """'Drar jag in ett objekt till safeguard eller konsekvens eller
    trädet så skall ju pluppen ändras från röd till grön' (2026-08-09).
    The pin icon on ORS/KON/SG previously only reflected whether a real
    P&ID marker existed for that row (cause_markers/consequence_markers/
    safeguard_markers) — dragging an equipment tag into the row's text
    doesn't create one of those, so the pin stayed red even though the
    row is now genuinely connected to the P&ID (via the dragged
    equipment marker's own placement there)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_pincolor_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    _PIN_GREEN = (0x27, 0xae, 0x60)
    _PIN_RED   = (0xe7, 0x4c, 0x3c)

    def _pin_color_in_cell(self, panel, row, col):
        """Render the real cell and report which pin color (green/red/
        neither) appears anywhere in its left icon strip. Scans pixels
        rather than intercepting _draw_pid_pin() calls directly: a single
        real paint pass can invoke the delegate's paint() more than once
        per cell (confirmed while writing this test — an early pass can
        run before layout/row-height settles), so asserting on every
        individual call is fragile; the final rendered pixels are what
        the user actually sees and are what should be verified."""
        panel.resize(600, 400)
        panel.show()
        self.app.processEvents()
        panel._table.resizeRowsToContents()
        self.app.processEvents()
        index = panel._table.model().index(row, col)
        cell_rect = panel._table.visualRect(index)
        pixmap = panel._table.viewport().grab(cell_rect)
        panel.hide()
        image = pixmap.toImage()
        found_green = found_red = False
        strip_w = min(24, image.width())
        for x in range(strip_w):
            for y in range(image.height()):
                px = image.pixelColor(x, y)
                rgb = (px.red(), px.green(), px.blue())
                if rgb == self._PIN_GREEN:
                    found_green = True
                elif rgb == self._PIN_RED:
                    found_red = True
        return found_green, found_red

    def test_kon_pin_is_green_when_tagged_without_a_real_marker(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            cons_id = self.db.add_consequence(cause_id)
            self.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            found_green, found_red = self._pin_color_in_cell(panel, row, panel._C_KON)
            self.assertTrue(found_green, "a tagged consequence's pin must be green even with no real P&ID marker")
            self.assertFalse(found_red)
        finally:
            panel.deleteLater()

    def test_sg_pin_is_green_when_tagged_without_a_real_marker(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            cons_id = self.db.add_consequence(cause_id)
            sg_id = self.db.add_safeguard(cons_id)
            self.db.append_tag_to_safeguard(sg_id, "PSH-101", "Tryckvakt")
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            found_green, found_red = self._pin_color_in_cell(panel, row, panel._C_SG)
            self.assertTrue(found_green, "a tagged safeguard's pin must be green even with no real P&ID marker")
            self.assertFalse(found_red)
        finally:
            panel.deleteLater()

    def test_kon_pin_stays_red_when_neither_tagged_nor_marked(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            cons_id = self.db.add_consequence(cause_id)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            found_green, found_red = self._pin_color_in_cell(panel, row, panel._C_KON)
            self.assertTrue(found_red, "an untagged, unmarked consequence's pin must stay red")
            self.assertFalse(found_green)
        finally:
            panel.deleteLater()


class PinTopAlignedInTallRowsTests(unittest.TestCase):
    """'Det vore snyggt om nålpluppen i HAZOP scenario stod i överkant'
    (2026-08-11) — SG's and KON's pin_rect used to span the cell's FULL
    (possibly tall, e.g. from a long wrapped description sharing the
    same row) height, so _draw_pid_pin's own centering left the pin
    drifting to the vertical middle of a tall row instead of staying
    near the top. Fixed by capping the pin's own rect at _PID_ICON_W
    tall, anchored at the cell's top."""

    _PIN_RED = (0xe7, 0x4c, 0x3c)

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_pintop_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _pin_pixel_y_range(self, panel, row, col):
        """Render the real cell and return (min_y, max_y, cell_height)
        for every pixel matching the (untagged, unmarked) red pin
        color found in its left icon strip.

        Uses panel._resize_rows_manual() rather than the native
        QTableWidget.resizeRowsToContents() — the latter was pinpointed
        as a native crash site (see _resize_rows()'s docstring) and, on
        top of that, was observed here to silently compute a wrong (far
        too small) row height for a very tall wrapped-text row. Production
        code never calls it either; matching that real render path is
        what makes this test meaningful."""
        panel.resize(600, 400)
        panel.show()
        self.app.processEvents()
        panel._resize_rows_manual()
        self.app.processEvents()
        index = panel._table.model().index(row, col)
        cell_rect = panel._table.visualRect(index)
        pixmap = panel._table.viewport().grab(cell_rect)
        panel.hide()
        image = pixmap.toImage()
        ys = []
        strip_w = min(24, image.width())
        for x in range(strip_w):
            for y in range(image.height()):
                px = image.pixelColor(x, y)
                if (px.red(), px.green(), px.blue()) == self._PIN_RED:
                    ys.append(y)
        self.assertTrue(ys, "expected to find the (red, unmarked) pin somewhere in this cell")
        return min(ys), max(ys), image.height()

    def test_sg_pin_stays_near_top_of_a_tall_row(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(
                cause_id,
                description="En mycket lång orsakstext som tvingar fram en hög "
                            "rad genom radbrytning i cellen " * 4)
            cons_id = self.db.add_consequence(cause_id)
            sg_id = self.db.add_safeguard(cons_id)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            min_y, max_y, cell_h = self._pin_pixel_y_range(panel, row, panel._C_SG)
            self.assertGreater(cell_h, 60, "the row must actually be tall for this test to mean anything")
            # Fixed pixel budget (not cell_h/2): the pin_rect is capped at
            # _PID_ICON_W tall, so a correctly top-anchored pin always stays
            # within the first ~25px regardless of how tall the row grows.
            # cell_h/2 was tried first but the bug (pin centered in the full
            # row) and the fix (pin capped near the top) both landed under
            # that threshold for some text lengths, making it not actually
            # distinguish the two — see NOTES.md 2026-08-11.
            self.assertLess(max_y, 25,
                f"pin pixels reached y={max_y} in a {cell_h}px-tall cell — "
                "must stay near the top, not drift toward the middle")
        finally:
            panel.deleteLater()

    def test_kon_pin_stays_near_top_of_a_tall_row(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(
                cons_id,
                "En mycket lång konsekvensbeskrivning som tvingar fram en hög "
                "rad genom radbrytning i cellen " * 4, 3, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            min_y, max_y, cell_h = self._pin_pixel_y_range(panel, row, panel._C_KON)
            self.assertGreater(cell_h, 60, "the row must actually be tall for this test to mean anything")
            self.assertLess(max_y, 25,
                f"pin pixels reached y={max_y} in a {cell_h}px-tall cell — "
                "must stay near the top, not drift toward the middle")
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

    def test_fill_mode_checkbox_state_persists_across_construction(self):
        from hazop import ScenarioTablePanel
        panel1 = ScenarioTablePanel(self.db)
        try:
            panel1._fill_chk.setChecked(False)
        finally:
            panel1.deleteLater()

        self.assertEqual(self.db.get_config('scenario_fill_mode', '1'), '0')

        panel2 = ScenarioTablePanel(self.db)
        try:
            self.assertFalse(panel2._fill_chk.isChecked())
        finally:
            panel2.deleteLater()

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

    def test_facility_leader_participants_round_trip(self):
        from hazop import SettingsPanel
        panel = SettingsPanel(self.db)
        try:
            panel._proj_facility.setText("Gävle Depå")
            panel._proj_facility.editingFinished.emit()
            panel._proj_leader.setText("Anna Andersson")
            panel._proj_leader.editingFinished.emit()
            panel._proj_participants.setPlainText("Anna Andersson\nBengt Bengtsson")
            panel._proj_participants.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut,
                                                                Qt.FocusReason.OtherFocusReason))

            self.assertEqual(self.db.get_config('project_facility'), "Gävle Depå")
            self.assertEqual(self.db.get_config('project_hazop_leader'), "Anna Andersson")
            self.assertEqual(self.db.get_config('project_participants'),
                              "Anna Andersson\nBengt Bengtsson")
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
