#!/usr/bin/env python3
"""Moved out of tests/test_pid_viewer.py 2026-08-26 alongside the retired
"Smart layout" feature itself (see archive/smart_layout.py's own
docstring) -- ConnectorAnalyzer no longer lives in pid_viewer.py, so this
test now targets archive.smart_layout instead. Test body is otherwise
unedited. Not part of the active suite (tests/) and not collected by it;
run directly if this archived module is ever touched again:

    python -m unittest archive.tests.test_smart_layout_archived -v
"""

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TEST_DIR  = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent.parent
for _p in (_HAZOP_DIR, _HAZOP_DIR / 'tests'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from test_helpers import _ensure_qapp  # noqa: E402


class ConnectorAnalyzerHangTests(unittest.TestCase):
    """ConnectorAnalyzer.run() used to wrap only the initial fitz.open() call
    in try/except. Any exception raised afterwards (page loop, dialect
    detection, OCR, connection matching, layout proposal) propagated out of
    QThread.run() uncaught. PyQt6 swallows exceptions raised inside
    QThread.run() -- it prints a traceback to stderr but never re-raises and
    never emits any signal -- so `finished_analysis` was never fired. The
    caller (PIDPanel._run_smart_layout, since removed -- see
    archive/smart_layout.py) showed a modal, non-cancellable
    QProgressDialog that only closes when `finished_analysis` fires, so the
    whole P&ID panel hung forever with no way out.

    This test simulates a mid-analysis failure (fitz.open() succeeds, but
    the very next call on the document raises) and asserts that
    `finished_analysis` still fires and the (fake) doc still gets closed.
    """

    def setUp(self):
        _ensure_qapp()

    def test_run_emits_finished_analysis_on_mid_scan_exception(self):
        import archive.smart_layout as smart_layout

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
        analyzer = smart_layout.ConnectorAnalyzer(
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

        with unittest.mock.patch.object(smart_layout.fitz, "open",
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


if __name__ == "__main__":
    unittest.main()
