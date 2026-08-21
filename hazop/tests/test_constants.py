"""Unit tests for constants.py's _app_dir()/_bundle_dir() frozen-build path
helpers (2026-08-21, see NOTES.md "Paketera HAZOP-appen som en
installationsfil"). No Qt dependency -- pure Python, run standalone.

Run with:
    python -m unittest tests.test_constants -v
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import constants


class AppDirBundleDirTests(unittest.TestCase):
    """_app_dir() (writable user data: db/crashes/backups/log) and
    _bundle_dir() (read-only shipped assets: icons/) must both fall back to
    Path(__file__).resolve().parent -- i.e. behave exactly as the old plain
    `Path(__file__).parent` code they replaced did -- when the app is NOT
    frozen (the normal `python hazop.py` case, and every existing test run).
    When frozen (sys.frozen set by PyInstaller), _app_dir() must resolve
    next to the actual installed exe (sys.executable) so user data survives
    across runs, while _bundle_dir() must resolve to PyInstaller's own
    extracted-resource root (sys._MEIPASS) since that's where bundled
    read-only files like icons/ actually live once packaged."""

    def test_app_dir_unfrozen_matches_module_directory(self):
        self.assertFalse(getattr(sys, 'frozen', False))
        self.assertEqual(constants._app_dir(), Path(constants.__file__).resolve().parent)

    def test_bundle_dir_unfrozen_matches_module_directory(self):
        self.assertFalse(getattr(sys, 'frozen', False))
        self.assertEqual(constants._bundle_dir(), Path(constants.__file__).resolve().parent)

    def test_app_dir_frozen_resolves_next_to_the_executable(self):
        """This is the case that matters for a packaged build: the
        database/crash-reports/backups/log must live next to the real
        installed HazopTool.exe, not inside a temp extraction directory
        that can be wiped between runs."""
        fake_exe = str(Path('C:/Users/Someone/AppData/Local/ProSa/HAZOP Tool/HazopTool.exe'))
        with unittest.mock.patch.object(sys, 'frozen', True, create=True), \
             unittest.mock.patch.object(sys, 'executable', fake_exe):
            self.assertEqual(constants._app_dir(), Path(fake_exe).resolve().parent)

    def test_bundle_dir_frozen_resolves_to_meipass(self):
        """icons/ ships inside the PyInstaller bundle itself -- must
        resolve to sys._MEIPASS (the extraction root PyInstaller sets for
        both --onefile and --onedir builds), NOT next to the exe."""
        fake_meipass = str(Path('C:/Users/Someone/AppData/Local/Temp/_MEI123456'))
        with unittest.mock.patch.object(sys, 'frozen', True, create=True), \
             unittest.mock.patch.object(sys, '_MEIPASS', fake_meipass, create=True):
            self.assertEqual(constants._bundle_dir(), Path(fake_meipass))

    def test_bundle_dir_frozen_falls_back_to_executable_dir_without_meipass(self):
        """Defensive fallback if sys._MEIPASS is somehow absent while
        frozen (shouldn't happen with real PyInstaller builds, but must
        not crash)."""
        fake_exe = str(Path('C:/Users/Someone/AppData/Local/ProSa/HAZOP Tool/HazopTool.exe'))
        with unittest.mock.patch.object(sys, 'frozen', True, create=True), \
             unittest.mock.patch.object(sys, 'executable', fake_exe):
            if hasattr(sys, '_MEIPASS'):
                self.skipTest("sys._MEIPASS already set in this process")
            self.assertEqual(constants._bundle_dir(), Path(fake_exe).resolve().parent)


if __name__ == '__main__':
    unittest.main()
