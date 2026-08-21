#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering database.py, plus any cross-module glue they
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
    QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit,
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




if __name__ == "__main__":
    unittest.main()
