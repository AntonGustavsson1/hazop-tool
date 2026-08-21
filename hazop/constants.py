#!/usr/bin/env python3
"""Shared constants — no Qt, no other project modules (split out of hazop.py
2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i fler filer").
Pure Python only, so this sits at the bottom of the import layer graph."""

import sys
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
# APP/BUNDLE DIRECTORY RESOLUTION (2026-08-21, see NOTES.md "Paketera HAZOP-
# appen som en installationsfil")
# ══════════════════════════════════════════════════════════════════════════════
# Several modules used to compute paths as Path(__file__).parent, assuming
# the running script's own directory is always the right place for both (a)
# the app's own writable data (database, crash reports, backups, logs) and
# (b) read-only bundled assets (icons/). That assumption breaks once the app
# is packaged with PyInstaller: __file__ then points *inside* the frozen
# bundle, not at the actual installed location, and for a one-file build
# that's a temporary extraction directory that can be wiped between runs.
#
# Two distinct helpers, because the two cases need different answers once
# frozen:
#   - _app_dir()    -> where the app's OWN WRITABLE data belongs. Next to
#                      sys.executable (the real, stable install location)
#                      when frozen; unchanged (next to this source file)
#                      otherwise.
#   - _bundle_dir() -> where READ-ONLY bundled resources shipped with the
#                      app live. PyInstaller always sets sys._MEIPASS (both
#                      --onefile and --onedir builds) to the resource root;
#                      unchanged (next to this source file) otherwise.
# Both are no-ops (identical to the old Path(__file__).parent behaviour)
# when the app is run unpackaged, i.e. `python hazop.py` as before.

def _app_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & MAGIC NUMBER CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # ===== TIMERS (milliseconds) =====
    'TIMER_DEFERRED_MS': 0,           # Deferred execution
    'TIMER_NAV_QUICK_MS': 50,         # Quick navigation/pan
    'TIMER_ZOOM_MS': 80,              # Zoom animation
    'TIMER_EDIT_START_MS': 200,       # Start edit deferred
    'TIMER_PDF_EXTRACT_MS': 100,      # PDF line extraction

    # ===== WIDGET HEIGHTS (pixels) =====
    'H_SEP_LINE': 1,                  # Separator line
    'H_COLOR_STRIP': 7,               # Color strip
    'H_BADGE': 20,                    # Icon/badge
    'H_BTN_SMALL': 22,                # Small buttons
    'H_CTRL_STD': 24,                 # Standard control
    'H_ROW_COMPACT': 26,              # Compact row
    'H_ROW_STD': 28,                  # Standard row
    'H_BTN_OK': 34,                   # OK/Cancel button
    'H_DESC_SM': 55,                  # Small description
    'H_DESC_MD': 65,                  # Medium description
    'H_DESC_LG': 80,                  # Large description
    'H_EDIT_LG': 100,                 # Large text editor
    'H_PANEL_MIN_LG': 120,            # Large panel minimum
    'H_FREQ_MAX': 140,                # Frequency panel max
    'H_TABLE_MIN': 150,               # Table minimum
    'H_TABLE_STD': 160,               # Standard table
    'H_PANEL_MAX': 300,               # Panel maximum
    'H_PANEL_MAX_ALT': 380,           # Alternative max
    'H_PANEL_MIN_XL': 520,            # Extra-large panel
    'H_PANEL_MIN_XXL': 560,           # XXL panel

    # ===== WIDGET WIDTHS (pixels) =====
    'W_LABEL_PCT': 10,                # Percentage label
    'W_ICON_BTN': 28,                 # Icon button
    'W_OPACITY_LBL': 36,              # Opacity label
    'W_LABEL_MD': 42,                 # Medium label
    'W_CORNER': 50,                   # Corner widget
    'W_BTN_COMPACT': 52,              # Compact button
    'W_SPINNER': 58,                  # Spinner width
    'W_FREQ_LBL': 88,                 # Frequency label
    'W_COL_MD': 100,                  # Medium column
    'W_COL_LG': 120,                  # Large column
    'W_CAT_LBL': 130,                 # Category label
    'W_DIALOG_MIN': 260,              # Min dialog
    'W_PANEL_MIN': 280,               # Min panel
    'W_DIALOG_MD': 300,               # Medium dialog
    'W_DIALOG_LG': 320,               # Large dialog
    'W_DIALOG_XL': 340,               # Extra-large dialog
    'W_PANEL_MIN_MD': 460,            # Medium panel min
    'W_PANEL_MIN_LG': 480,            # Large panel min
    'W_PANEL_MIN_XL': 500,            # XL panel min
    'W_PANEL_MIN_XXL': 640,           # XXL panel min

    # ===== SEMANTIC ZONE WIDTHS (pixel regions in cells) =====
    'ZONE_PID_ICON': 22,              # P&ID pin icon
    'ZONE_CONS_CAT': 26,              # Consequence category
    'ZONE_CONS_CHAIN': 24,            # Consequence chain link
    'ZONE_CAUSE_OBJ': 64,             # Cause object-tag
    'ZONE_CAUSE_COMMENT': 22,         # Cause comment icon
    'ZONE_CAUSE_CLONE': 22,           # Cause clone icon
    'ZONE_CAUSE_FREQ': 50,            # Cause frequency badge
    'ZONE_SG_RRF': 54,                # Safeguard RRF badge
    'ZONE_EQUIP_ICON': 20,            # Equipment icon
}

SEV_LABELS  = ['C1 – Försumbar', 'C2 – Liten', 'C3 – Måttlig', 'C4 – Allvarlig', 'C5 – Katastrofal']

RRF_VALUES  = [1, 10, 100, 1000, 10000]
RRF_LABELS  = ['1 – Ingen', '10 – RRF10', '100 – RRF100', '1000 – RRF1000', '10000 – RRF10000']
SG_TYPES      = ['BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt']
MARKUP_COLORS = ['#E53935', '#F57C00', '#F9A825', '#388E3C',
                  '#00796B', '#1565C0', '#7B1FA2', '#FF4081']
RISK_ICON   = {'Låg': '🟢', 'Medium': '🟡', 'Hög': '🟠', 'Kritisk': '🔴'}

# ══════════════════════════════════════════════════════════════════════════════
# TREE NODE TYPES
# ══════════════════════════════════════════════════════════════════════════════

NODE_T = 1
CAUSE_T = 2
CONS_T = 3
SG_T = 4
DEV_T = 5
EQUIP_T = 6
LEDORD_T = 7   # pure grouping level (guide word / "ledord") — no DB row of
               # its own, several deviation rows across different equipment
               # can share one. See NOTES.md "Nod → Ledord → Utrustning".

DEVIATION_TYPES = [
    "Lågt flöde",
    "Högt flöde",
    "Missriktat flöde",
    "Omvänt flöde",
    "Högt tryck",
    "Lågt tryck",
    "Hög nivå",
    "Låg nivå",
    "Hög temperatur",
    "Låg temperatur",
    "Avvikande sammansättning",
    "Bortfall av hjälpsystem",
    "Drift",
    "Underhåll",
    "Start-up / Shut-down",
    "Övrigt",
]
