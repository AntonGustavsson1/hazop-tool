# PyInstaller runtime hook (2026-08-24, see NOTES.md "Analysera P&ID
# kraschar och startar om appen vid flera sidor" -- part 3, a startup
# crash found while re-verifying the actual fix). Runs before hazop.py's
# own code, inside the frozen bootstrap.
#
# Symptom this fixes: "Could not load the Qt platform plugin 'windows' in
# '' even though it was found" -- Qt's own qwindows.dll platform plugin
# (bundled under _internal/PyQt6/Qt6/plugins/platforms/) depends on
# Qt6Core.dll/Qt6Gui.dll, which PyInstaller's PyQt6 hook bundles in a
# DIFFERENT, sibling directory (_internal/PyQt6/Qt6/bin/). Windows' default
# DLL search order only checks the loading executable's own directory,
# System32, and PATH -- NOT arbitrary nested subdirectories -- so
# qwindows.dll's dependency load silently fails, and Qt reports it as
# merely "found but not loadable" rather than naming the missing DLL.
#
# os.add_dll_directory() (Windows-only, Python 3.8+) explicitly widens the
# search path for the rest of the process's lifetime, which is exactly
# what's needed here -- added before any Qt plugin load is attempted.
import os
import sys

if sys.platform == 'win32' and hasattr(sys, '_MEIPASS'):
    _qt_bin_dir = os.path.join(sys._MEIPASS, 'PyQt6', 'Qt6', 'bin')
    if os.path.isdir(_qt_bin_dir):
        try:
            os.add_dll_directory(_qt_bin_dir)
        except (OSError, AttributeError):
            pass
