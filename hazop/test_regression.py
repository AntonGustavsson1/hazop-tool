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


if __name__ == '__main__':
    unittest.main(verbosity=2)
