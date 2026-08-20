#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering ui_helpers.py, plus any cross-module glue they
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


from test_helpers import (
    _ensure_qapp, _menu_action_labels, _fake_pdf_loaded,
    _TempDbMainWindow, _find_tree_item,
)

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




if __name__ == "__main__":
    unittest.main()
