#!/usr/bin/env python3
"""Shared test fixtures/helpers used across the split test_*.py files
(2026-08-20 — see NOTES.md "Dela upp test_regression.py i per-modul
testfiler"). Extracted verbatim from the old test_regression.py's own
top-of-file helpers, unchanged."""
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



def _find_tree_item(tree, type_, id_=None):
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == type_ and (
                id_ is None or item.data(0, Qt.ItemDataRole.UserRole) == id_):
            return item
        it += 1
    return None

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


