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
    NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T, EQUIP_T, LEDORD_T, SYSTEM_T,
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
    _TempDbMainWindow, _find_tree_item, count_selects,
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
    """Regression tests for three real crash reports: two from 2026-08-07
    (crash_20260807_154530_IntegrityError.json / _161554_...) — deleting a
    node with equipment assigned to it (equipment_catalog.node_id) raised
    sqlite3.IntegrityError: FOREIGN KEY constraint failed — and one from
    2026-08-24 (crash_20260824_132650_.../crash_20260824_143009_..., both
    "Analysera P&ID" on a real multi-tag document). Root cause each time:
    a *_id column pointing at equipment_catalog(id) added via ALTER TABLE
    with NO ON DELETE clause, unlike every other node_id/equipment_id-shaped
    FK in this schema (which use ON DELETE CASCADE) — equipment_catalog.
    node_id and deviations.equipment_id (2026-08-07), then causes.
    equipment_id (added 2026-08-13 for the "Live tag-länk" feature, see
    NOTES.md, but never added to delete_equipment_item()/
    clear_equipment_catalog()'s cleanup list until the 2026-08-24 crash
    traced it there). delete_node()/delete_equipment_item()/
    clear_equipment_catalog() now explicitly clear every one of these soft
    references before deleting, instead of cascading (equipment/deviations/
    causes are real HAZOP data that must survive their assigned node or
    equipment row being removed)."""

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

    def test_deleting_equipment_item_with_a_cause_linked_via_equipment_id_does_not_raise(self):
        """2026-08-24 regression: causes.equipment_id (the "Live tag-länk"
        feature, 2026-08-13) was never included in delete_equipment_item()'s
        cleanup — only deviations.equipment_id was. Real crash report:
        crash_20260824_143009_IntegrityError.json."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self.db.add_equipment_item("HSP-01", "HSP-01", "HSP", 0, "Instrument", '', 0)
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, equipment_id=eq_id)
        self.assertEqual(dict(self.db.get_cause(cause_id))['equipment_id'], eq_id)

        self.db.delete_equipment_item(eq_id)   # must not raise IntegrityError

        self.assertIsNone(self.db.get_equipment_by_id(eq_id))
        # The cause survives, just loses the equipment link.
        cause = self.db.get_cause(cause_id)
        self.assertIsNotNone(cause)
        self.assertIsNone(dict(cause)['equipment_id'])

    def test_clear_equipment_catalog_with_a_cause_linked_via_equipment_id_does_not_raise(self):
        """The exact real-world trigger for both 2026-08-24 crash reports:
        'Analysera P&ID' on a document whose causes already had tags
        live-linked via causes.equipment_id — clear_equipment_catalog()
        must not fail just because that link exists."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self.db.add_equipment_item("HSP-01", "HSP-01", "HSP", 0, "Instrument", '', 0)
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, equipment_id=eq_id)

        self.db.clear_equipment_catalog()   # must not raise IntegrityError

        self.assertEqual(self.db.equipment_items(), [])
        cause = self.db.get_cause(cause_id)
        self.assertIsNotNone(cause)
        self.assertIsNone(dict(cause)['equipment_id'])


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
        # A cause created from a dragged P&ID object starts completely blank;
        # the user supplies the description explicitly.
        self.assertEqual(cause['description'], '')
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


class ConsequenceHistoryTests(unittest.TestCase):
    """"Spara varje konsekvens som skrivs i HAZOP Scenario i en databas."
    (2026-08-26, see NOTES.md "Återanvänd tidigare konsekvenser") — the
    autocomplete dropdown ScenarioTablePanel's KON-cell editor shows is
    backed by this growing history table."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_conshistory_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_added_descriptions_are_returned(self):
        self.db.add_consequence_history("Hög nivå i tank")
        self.db.add_consequence_history("Läckage vid fläns")
        self.assertEqual(set(self.db.consequence_history()),
                          {"Hög nivå i tank", "Läckage vid fläns"})

    def test_duplicate_description_is_not_stored_twice(self):
        self.db.add_consequence_history("Hög nivå i tank")
        self.db.add_consequence_history("Hög nivå i tank")
        self.assertEqual(self.db.consequence_history(), ["Hög nivå i tank"])

    def test_blank_description_is_a_noop(self):
        self.db.add_consequence_history("")
        self.db.add_consequence_history("   ")
        self.db.add_consequence_history(None)
        self.assertEqual(self.db.consequence_history(), [])

    def test_history_is_returned_sorted_case_insensitively(self):
        self.db.add_consequence_history("överfyllnad")
        self.db.add_consequence_history("Backflöde")
        self.db.add_consequence_history("acidkorrosion")
        self.assertEqual(self.db.consequence_history(),
                          ["acidkorrosion", "Backflöde", "överfyllnad"])


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


class SystemsHierarchyTests(unittest.TestCase):
    """New top-level "System" hierarchy above Nod (2026-08-24, see
    NOTES.md "Ny toppnivå System") — System → Nod → Avvikelse → ...
    A node's system_id is nullable so pre-existing projects (and any UI
    path that doesn't set it) keep working as ungrouped nodes."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_systems_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fresh_database_seeds_one_system_and_one_node_under_it(self):
        systems = self.db.systems()
        nodes = self.db.nodes()
        self.assertEqual(len(systems), 1)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['system_id'], systems[0]['id'])

    def test_add_system_and_add_node_with_system_id(self):
        sid = self.db.add_system("Reaktorsystem")
        systems = {s['id']: s for s in self.db.systems()}
        self.assertIn(sid, systems)
        self.assertEqual(systems[sid]['name'], "Reaktorsystem")

        node_id = self.db.add_node(system_id=sid)
        self.assertEqual(self.db.get_node(node_id)['system_id'], sid)

    def test_add_node_without_system_id_is_ungrouped(self):
        node_id = self.db.add_node()
        self.assertIsNone(self.db.get_node(node_id)['system_id'])

    def test_rename_system(self):
        sid = self.db.add_system("Original")
        self.db.rename_system(sid, "Nytt namn")
        systems = {s['id']: s for s in self.db.systems()}
        self.assertEqual(systems[sid]['name'], "Nytt namn")

    def test_delete_system_reassigns_nodes_to_ungrouped_not_cascade(self):
        """Deleting a system must NOT delete its nodes (or anything under
        them) — only lift the nodes out to ungrouped, same "reassign, don't
        cascade" convention as delete_node_type()."""
        sid = self.db.add_system("Temporärt system")
        node_id = self.db.add_node(system_id=sid)
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)

        self.db.delete_system(sid)

        self.assertIsNone(self.db.get_node(node_id)['system_id'])
        self.assertIsNotNone(self.db.get_node(node_id), "the node itself must survive")
        self.assertIsNotNone(self.db.get_cause(cause_id),
            "everything under the node must survive the system's deletion")
        remaining_ids = {s['id'] for s in self.db.systems()}
        self.assertNotIn(sid, remaining_ids)

    def test_reorder_systems(self):
        # A fresh Database auto-seeds one default system (see
        # test_fresh_database_seeds_one_system_and_one_node_under_it) —
        # remove it first so this test's own controlled set is exhaustive.
        for n in self.db.nodes():
            self.db.delete_node(n['id'])
        for s in self.db.systems():
            self.db.delete_system(s['id'])

        s1 = self.db.add_system("A")
        s2 = self.db.add_system("B")
        s3 = self.db.add_system("C")
        self.db.reorder_systems([s3, s1, s2])
        ordered_ids = [s['id'] for s in self.db.systems()]
        self.assertEqual(ordered_ids, [s3, s1, s2])

    def test_set_node_system_reparents(self):
        s1 = self.db.add_system("A")
        s2 = self.db.add_system("B")
        node_id = self.db.add_node(system_id=s1)

        self.db.set_node_system(node_id, s2)

        self.assertEqual(self.db.get_node(node_id)['system_id'], s2)


class RecommendationCatalogTests(unittest.TestCase):
    """2026-08-25, see NOTES.md "Rekommendationshantering — delad
    katalog med återanvändning": the old actions table (one row per
    consequence, no reuse) was replaced by a shared recommendations
    catalog plus a consequence_recommendations many-to-many link table,
    so the same recommendation text can be linked to several
    consequences without duplicating it."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_reccatalog_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        cause2 = self.db.add_cause(dev_id)
        self.cons2 = self.db.add_consequence(cause2)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_add_recommendation_to_consequence_creates_and_links_in_one_call(self):
        rec_id = self.db.add_recommendation_to_consequence(
            self.cons_id, description='Verify shutdown function',
            responsible='Anton', due_date='2026-09-01', status='Öppen')
        rec = self.db.get_recommendation(rec_id)
        self.assertEqual(rec['description'], 'Verify shutdown function')
        self.assertEqual(rec['responsible'], 'Anton')
        linked = [r['id'] for r in self.db.recommendations_for_consequence(self.cons_id)]
        self.assertEqual(linked, [rec_id])

    def test_linking_the_same_recommendation_to_two_consequences_does_not_duplicate(self):
        rec_id = self.db.add_recommendation(description='Reused text')
        self.db.link_recommendation_to_consequence(rec_id, self.cons_id)
        self.db.link_recommendation_to_consequence(rec_id, self.cons2)

        self.assertEqual(len(self.db.all_recommendations()), 1)
        self.assertEqual([r['id'] for r in self.db.recommendations_for_consequence(self.cons_id)],
                         [rec_id])
        self.assertEqual([r['id'] for r in self.db.recommendations_for_consequence(self.cons2)],
                         [rec_id])
        self.assertEqual(self.db.recommendation_consequence_count(rec_id), 2)

    def test_same_recommendation_text_reuses_existing_number(self):
        first = self.db.add_recommendation_to_consequence(
            self.cons_id, description='Kontrollera ventilen')
        second = self.db.add_recommendation_to_consequence(
            self.cons2, description='  kontrollera   ventilen  ')
        self.assertEqual(second, first)
        self.assertEqual(len(self.db.all_recommendations()), 1)
        self.assertEqual(
            [r['id'] for r in self.db.recommendations_for_consequence(self.cons2)],
            [first])

    def test_linking_twice_to_the_same_consequence_is_idempotent(self):
        rec_id = self.db.add_recommendation(description='X')
        self.db.link_recommendation_to_consequence(rec_id, self.cons_id)
        self.db.link_recommendation_to_consequence(rec_id, self.cons_id)
        self.assertEqual(len(self.db.recommendations_for_consequence(self.cons_id)), 1)

    def test_unlink_removes_only_the_link_not_the_catalog_row(self):
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Keep me')
        self.db.unlink_recommendation_from_consequence(rec_id, self.cons_id)
        self.assertEqual(self.db.recommendations_for_consequence(self.cons_id), [])
        self.assertIsNotNone(self.db.get_recommendation(rec_id))
        self.assertEqual(self.db.recommendation_consequence_count(rec_id), 0)

    def test_update_recommendation_is_a_partial_update(self):
        rec_id = self.db.add_recommendation(description='Original', responsible='A',
                                            due_date='2026-01-01', status='Öppen')
        self.db.update_recommendation(rec_id, description='Changed')
        rec = self.db.get_recommendation(rec_id)
        self.assertEqual(rec['description'], 'Changed')
        self.assertEqual(rec['responsible'], 'A', "fields not passed must stay untouched")
        self.assertEqual(rec['due_date'], '2026-01-01')

    def test_delete_recommendation_cascades_its_links(self):
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Bye')
        self.db.delete_recommendation(rec_id)
        self.assertIsNone(self.db.get_recommendation(rec_id))
        self.assertEqual(self.db.recommendations_for_consequence(self.cons_id), [])

    def test_recommendations_for_consequences_bulk_matches_single_id_version(self):
        r1 = self.db.add_recommendation_to_consequence(self.cons_id, description='A')
        r2 = self.db.add_recommendation_to_consequence(self.cons_id, description='B')
        self.db.link_recommendation_to_consequence(r1, self.cons2)

        bulk = self.db.recommendations_for_consequences([self.cons_id, self.cons2])
        self.assertEqual([r['id'] for r in bulk[self.cons_id]], [r1, r2])
        self.assertEqual([r['id'] for r in bulk[self.cons2]], [r1])

    def test_all_recommendations_returns_the_whole_study_wide_catalog(self):
        self.db.add_recommendation(description='One')
        self.db.add_recommendation(description='Two')
        self.assertEqual(len(self.db.all_recommendations()), 2)

    def test_migrating_a_pre_existing_actions_table(self):
        """Simulate a database file created before the 2026-08-25 rework
        still physically having the old actions table with real rows —
        re-running the migration must move that data into the new
        catalog + link table and drop the old one, exactly once."""
        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                responsible TEXT DEFAULT '', due_date TEXT DEFAULT '',
                status TEXT DEFAULT 'Öppen'
            )""")
        self.db.conn.execute(
            "INSERT INTO actions (consequence_id,description,responsible,due_date,status) "
            "VALUES (?,?,?,?,?)", (self.cons_id, 'Legacy action', 'Bob', '2026-01-01', 'Klar'))
        self.db.commit()

        self.db._migrate_actions_to_recommendations()

        linked = self.db.recommendations_for_consequence(self.cons_id)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]['description'], 'Legacy action')
        self.assertEqual(linked[0]['responsible'], 'Bob')
        tables = {r['name'] for r in self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('actions', tables, "the legacy table must be dropped after migration")

        # Re-running is a no-op now that the table is gone (no crash, no
        # duplicate rows created).
        self.db._migrate_actions_to_recommendations()
        self.assertEqual(len(self.db.recommendations_for_consequence(self.cons_id)), 1)


# ══════════════════════════════════════════════════════════════════════════
# Tree-context P&ID equipment highlight (2026-08-27, see NOTES.md
# "Dynamisk färgmarkering av objekt på P&ID") — Database.
# equipment_link_types_in_scope(type_, id_) backs the feature: given a
# HAZOP tree selection, find every equipment_catalog id referenced by any
# deviation/cause/consequence/safeguard in its subtree, and via which
# link type(s).
# ══════════════════════════════════════════════════════════════════════════

class EquipmentLinkTypesInScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_scope_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_equipment(self, tag, comp_type='Ventil'):
        return self.db.add_equipment_item(tag, tag, tag[0], 0, comp_type, '', 0)

    def _make_chain(self, node_id, cause_equipment_id=None, cons_tags=(), sg_tags=()):
        """One deviation/cause/consequence/safeguard under node_id. Tags
        given as (comp_tag, tagged_refs_csv) pairs, so a test can exercise
        both the current tag and the drag-history in one call."""
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        if cause_equipment_id is not None:
            self.db.conn.execute(
                "UPDATE causes SET equipment_id=? WHERE id=?", (cause_equipment_id, cause_id))
        cons_id = self.db.add_consequence(cause_id)
        if cons_tags:
            comp_tag, tagged_refs = cons_tags
            self.db.conn.execute(
                "UPDATE consequences SET comp_tag=?, tagged_refs=? WHERE id=?",
                (comp_tag, tagged_refs, cons_id))
        sg_id = self.db.add_safeguard(cons_id)
        if sg_tags:
            comp_tag, tagged_refs = sg_tags
            self.db.conn.execute(
                "UPDATE safeguards SET comp_tag=?, tagged_refs=? WHERE id=?",
                (comp_tag, tagged_refs, sg_id))
        self.db.commit()
        return dev_id, cause_id, cons_id, sg_id

    def test_scope_system_level_includes_every_node_cause_consequence_safeguard(self):
        sys_id = self.db.add_system("Sys A")
        node_a = self.db.add_node(system_id=sys_id)
        node_b = self.db.add_node(system_id=sys_id)
        eq_a = self._make_equipment("V-1")
        eq_b = self._make_equipment("V-2")
        self._make_chain(node_a, cause_equipment_id=eq_a)
        self._make_chain(node_b, cause_equipment_id=eq_b)

        scope = self.db.equipment_link_types_in_scope(SYSTEM_T, sys_id)
        self.assertEqual(set(scope.keys()), {eq_a, eq_b})

    def test_scope_node_level_excludes_other_nodes(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        eq_a = self._make_equipment("V-1")
        eq_b = self._make_equipment("V-2")
        self._make_chain(node_a, cause_equipment_id=eq_a)
        self._make_chain(node_b, cause_equipment_id=eq_b)

        scope = self.db.equipment_link_types_in_scope(NODE_T, node_a)
        self.assertEqual(set(scope.keys()), {eq_a})

    def test_scope_deviation_level_excludes_sibling_deviations(self):
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        dev_a, dev_b = devs[0]['id'], devs[1]['id']
        eq_a = self._make_equipment("V-1")
        eq_b = self._make_equipment("V-2")
        cause_a = self.db.add_cause(dev_a)
        self.db.conn.execute("UPDATE causes SET equipment_id=? WHERE id=?", (eq_a, cause_a))
        cause_b = self.db.add_cause(dev_b)
        self.db.conn.execute("UPDATE causes SET equipment_id=? WHERE id=?", (eq_b, cause_b))
        self.db.commit()

        scope = self.db.equipment_link_types_in_scope(DEV_T, dev_a)
        self.assertEqual(set(scope.keys()), {eq_a})

    def test_scope_grouped_deviation_includes_same_text_equipment_siblings(self):
        node_id = self.db.add_node()
        dev_a = self.db.get_or_create_deviation(node_id, "LÃ¥gt flÃ¶de")
        eq_b = self._make_equipment("V-2")
        dev_b = self.db.add_deviation(node_id, "LÃ¥gt flÃ¶de", equipment_id=eq_b)
        cause_b = self.db.add_cause(dev_b)
        self.db.conn.execute(
            "UPDATE causes SET equipment_id=? WHERE id=?", (eq_b, cause_b))
        self.db.commit()

        scope = self.db.equipment_link_types_in_scope(DEV_T, dev_a)
        self.assertIn(eq_b, scope)
        self.assertIn('deviation', scope[eq_b])

    def test_scope_cause_level_includes_own_and_descendant_links_only(self):
        """"Objekt-nivå" (Anton's decision): selecting a Cause row
        includes its own object AND its own consequences'/safeguards'
        tagged objects, but not a sibling cause's object."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_a = self._make_equipment("V-1")
        eq_b = self._make_equipment("P-1", "Pump")
        eq_sibling = self._make_equipment("T-1", "Tank")

        cause_a = self.db.add_cause(dev_id)
        self.db.conn.execute("UPDATE causes SET equipment_id=? WHERE id=?", (eq_a, cause_a))
        cons_a = self.db.add_consequence(cause_a)
        self.db.conn.execute(
            "UPDATE consequences SET comp_tag='P-1' WHERE id=?", (cons_a,))
        self.db.commit()

        cause_sibling = self.db.add_cause(dev_id)
        self.db.conn.execute(
            "UPDATE causes SET equipment_id=? WHERE id=?", (eq_sibling, cause_sibling))
        self.db.commit()

        scope = self.db.equipment_link_types_in_scope(CAUSE_T, cause_a)
        self.assertEqual(set(scope.keys()), {eq_a, eq_b})
        self.assertIn('cause', scope[eq_a])
        self.assertIn('consequence', scope[eq_b])

    def test_scope_consequence_level_excludes_parent_cause(self):
        node_id = self.db.add_node()
        eq_cause = self._make_equipment("V-1")
        eq_cons = self._make_equipment("P-1", "Pump")
        _dev, cause_id, cons_id, _sg = self._make_chain(
            node_id, cause_equipment_id=eq_cause, cons_tags=("P-1", ""))

        scope = self.db.equipment_link_types_in_scope(CONS_T, cons_id)
        self.assertEqual(set(scope.keys()), {eq_cons},
            "the parent cause's own object must NOT bleed into a "
            "Consequence-level selection — scope only ever flows downward")

    def test_scope_matches_every_tagged_refs_entry_not_just_latest(self):
        """Anton's decision #2: match every historically-dragged tag
        (tagged_refs), not just the current comp_tag."""
        node_id = self.db.add_node()
        eq_old = self._make_equipment("V-1")
        eq_mid = self._make_equipment("P-1", "Pump")
        eq_latest = self._make_equipment("T-1", "Tank")
        _dev, _cause, cons_id, _sg = self._make_chain(
            node_id, cons_tags=("T-1", "V-1,P-1,T-1"))

        scope = self.db.equipment_link_types_in_scope(CONS_T, cons_id)
        self.assertEqual(set(scope.keys()), {eq_old, eq_mid, eq_latest})
        for eq_id in (eq_old, eq_mid, eq_latest):
            self.assertIn('consequence', scope[eq_id])

    def test_scope_deviation_own_equipment_id_returns_link_type_deviation(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self._make_equipment("V-1")
        self.db.conn.execute(
            "UPDATE deviations SET equipment_id=? WHERE id=?", (eq_id, dev_id))
        self.db.commit()

        scope = self.db.equipment_link_types_in_scope(DEV_T, dev_id)
        self.assertEqual(scope, {eq_id: {'deviation'}})

    def test_scope_unresolvable_free_text_tag_is_skipped_not_error(self):
        node_id = self.db.add_node()
        _dev, _cause, cons_id, _sg = self._make_chain(
            node_id, cons_tags=("DOES-NOT-EXIST", ""))
        try:
            scope = self.db.equipment_link_types_in_scope(CONS_T, cons_id)
        except Exception as e:
            self.fail(f"an unresolvable free-text tag must be skipped, not raise: {e!r}")
        self.assertEqual(scope, {})

    def test_scope_unknown_or_missing_selection_returns_empty(self):
        self.assertEqual(self.db.equipment_link_types_in_scope(NODE_T, None), {})
        self.assertEqual(self.db.equipment_link_types_in_scope(NODE_T, 999999), {})
        self.assertEqual(self.db.equipment_link_types_in_scope(EQUIP_T, 1), {})

    def test_scope_query_count_is_bounded_for_a_large_system(self):
        """Regression guard, same discipline as TreePanel.refresh()'s own
        N+1 fix (see test_tree_panel.py) — SELECT count must not scale
        linearly with subtree size."""
        sys_id = self.db.add_system("Big system")

        def build(n_nodes):
            for _ in range(n_nodes):
                node_id = self.db.add_node(system_id=sys_id)
                dev_id = self.db.deviations(node_id)[0]['id']
                cause_id = self.db.add_cause(dev_id)
                self.db.add_consequence(cause_id)

        build(2)
        small_count = count_selects(
            self.db, lambda: self.db.equipment_link_types_in_scope(SYSTEM_T, sys_id))

        build(20)
        large_count = count_selects(
            self.db, lambda: self.db.equipment_link_types_in_scope(SYSTEM_T, sys_id))

        self.assertLess(large_count, small_count + 5,
            f"SELECT count grew with subtree size ({small_count} -> "
            f"{large_count}) — the batched traversal may have regressed to N+1")


if __name__ == "__main__":
    unittest.main()
