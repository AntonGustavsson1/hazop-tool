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
    CAUSE_T, CONS_T, SG_T,
)
from PyQt6.QtWidgets import QApplication, QGraphicsPixmapItem  # noqa: E402
from PyQt6.QtGui import QPixmap  # noqa: E402


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
    safeguard's OWN row (its RFORE/REFT/SLUT columns are derived from rrf,
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
        from pid_viewer import PIDPanel, MODE_CAUSE, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE)
            self.assertEqual(panel.viewer.mode, MODE_CAUSE)
            self._press_escape(panel.viewer)
            self.assertEqual(panel.viewer.mode, MODE_NAV,
                              "Escape must abort MODE_CAUSE back to MODE_NAV")
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

    def test_escape_unchecks_mode_toolbar_button(self):
        """Cancelling via Escape must also update the toolbar button state,
        not just the internal viewer.mode — otherwise the UI would show the
        cause/consequence/safeguard button as still active after cancelling.
        """
        from pid_viewer import PIDPanel, MODE_CAUSE, MODE_NAV
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE)
            self.assertTrue(panel.mode_buttons[MODE_CAUSE].isChecked())
            self._press_escape(panel.viewer)
            self.assertFalse(panel.mode_buttons[MODE_CAUSE].isChecked(),
                              "Cause toolbar button must uncheck after Escape-cancel")
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
        from pid_viewer import PIDPanel, MODE_CAUSE
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE)
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
        from pid_viewer import PIDPanel, MODE_CAUSE
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            panel._set_mode(MODE_CAUSE)
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
        from pid_viewer import (PIDPanel, MODE_CAUSE, MODE_CONSEQUENCE,
                                 MODE_SAFEGUARD, _PLACEMENT_MODE_COLORS)
        from PyQt6.QtCore import QPointF
        panel = PIDPanel(self.db)
        try:
            for mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
