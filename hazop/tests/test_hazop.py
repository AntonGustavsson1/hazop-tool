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




if __name__ == "__main__":
    unittest.main()
