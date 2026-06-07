#!/usr/bin/env python3
"""HAZOP Tool — Hazard and Operability Study Manager v2"""

import sys
import re
import json
import sqlite3
import math
import datetime
from pathlib import Path

from pid_viewer import (
    PIDPanel, COMPONENT_TYPES, CONSEQUENCE_TEMPLATES, HAS_PYMUPDF,
    MODE_NAV, MODE_NODE, MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD,
    scan_pdf_for_equipment, ocr_status, KNOWN_PREFIXES, invert_cause_text,
)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QScrollArea,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QStackedWidget,
    QTabWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLineEdit, QTextEdit, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QComboBox, QDialog, QDialogButtonBox,
    QMessageBox, QFileDialog, QGroupBox,
    QMenu, QToolBar, QStatusBar, QSizePolicy,
    QSpinBox, QSlider, QColorDialog, QFrame, QListWidget, QListWidgetItem,
    QProgressDialog, QAbstractItemView, QToolTip, QInputDialog, QCheckBox,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QPointF, QRectF, QRect, QTimer, QMimeData, QEvent
from PyQt6.QtGui import QFont, QColor, QAction, QBrush, QPen, QPainter, QDrag, QPainterPath, QPixmap, QIcon, QPolygonF, QShortcut, QKeySequence

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = Path(__file__).parent / "hazop_project.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT 'Ny nod',
    description TEXT DEFAULT '',
    pid_ref     TEXT DEFAULT '',
    media       TEXT DEFAULT '',
    pressure    TEXT DEFAULT '',
    temperature TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS causes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    description TEXT NOT NULL DEFAULT 'Ny orsak',
    likelihood  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS consequences (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cause_id          INTEGER NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    description       TEXT NOT NULL DEFAULT 'Ny konsekvens',
    severity          INTEGER NOT NULL DEFAULT 1,
    category          TEXT DEFAULT '',
    consequence_chain TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS safeguards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    rrf             INTEGER NOT NULL DEFAULT 1,
    source_id       INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS reduction_factors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    rrf             INTEGER NOT NULL DEFAULT 10,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    responsible     TEXT DEFAULT '',
    due_date        TEXT DEFAULT '',
    status          TEXT DEFAULT 'Öppen'
);

CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS consequence_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS component_types (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS failure_modes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL REFERENCES component_types(id) ON DELETE CASCADE,
    description  TEXT NOT NULL DEFAULT '',
    freq_per_year REAL DEFAULT NULL,
    sort_order   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reduction_factors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    rrf             INTEGER NOT NULL DEFAULT 10,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS equipment_types (
    prefix          TEXT PRIMARY KEY,
    equipment_type  TEXT NOT NULL,
    display_name    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tag_database (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_code    TEXT NOT NULL,
    name_sv     TEXT DEFAULT '',
    name_en     TEXT DEFAULT '',
    category    TEXT DEFAULT '',
    standard    TEXT DEFAULT '',
    source      TEXT DEFAULT 'excel',
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tag_database_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pid_identified_tags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_code     TEXT NOT NULL UNIQUE,
    examples     TEXT DEFAULT '',
    name_sv      TEXT DEFAULT '',
    comp_type    TEXT DEFAULT '',
    confirmed    INTEGER DEFAULT 0,
    source       TEXT DEFAULT 'scan'
);

CREATE TABLE IF NOT EXISTS equipment_catalog (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tag            TEXT NOT NULL,
    original_tag   TEXT DEFAULT '',
    prefix         TEXT DEFAULT '',
    pid_page       INTEGER DEFAULT 0,
    equipment_type TEXT DEFAULT '',
    description    TEXT DEFAULT '',
    is_ocr         INTEGER DEFAULT 0,
    include        INTEGER DEFAULT 1
);
"""

# Frequency axis: F=-1..5 (7 levels, logarithmic events/year)
# Consequence axis: C=1..5 (5 levels)
DEFAULT_MATRIX = {
    'rows': 5,   # consequence rows, index 0 = C1 (lowest)
    'cols': 7,   # frequency columns, index 0 = F-1 (lowest)
    'x_axis': 'frequency',
    'x_labels': [
        'F-1 – Otänkbar (<1/100 000 år)',
        'F0 – Extremt sällan (1/100 000 år)',
        'F1 – Sällan (1/10 000 år)',
        'F2 – Osannolik (1/1 000 år)',
        'F3 – Möjlig (1/100 år)',
        'F4 – Trolig (1–10 år)',
        'F5 – Frekvent (>1/år)',
    ],
    'y_labels': [
        'C1 – Försumbar',
        'C2 – Liten',
        'C3 – Måttlig',
        'C4 – Allvarlig',
        'C5 – Katastrofal',
    ],
    'cell_colors': [
        # C=1: F-1 → F5
        ['#27ae60', '#27ae60', '#27ae60', '#27ae60', '#f39c12', '#e67e22', '#e74c3c'],
        # C=2
        ['#27ae60', '#27ae60', '#27ae60', '#f39c12', '#e67e22', '#e74c3c', '#e74c3c'],
        # C=3
        ['#27ae60', '#27ae60', '#f39c12', '#e67e22', '#e74c3c', '#e74c3c', '#e74c3c'],
        # C=4
        ['#27ae60', '#f39c12', '#e67e22', '#e74c3c', '#e74c3c', '#e74c3c', '#e74c3c'],
        # C=5
        ['#f39c12', '#e67e22', '#e74c3c', '#e74c3c', '#e74c3c', '#e74c3c', '#e74c3c'],
    ],
    'cell_labels': [
        ['Låg',    'Låg',    'Låg',    'Låg',    'Medium', 'Hög',    'Kritisk'],
        ['Låg',    'Låg',    'Låg',    'Medium', 'Hög',    'Kritisk','Kritisk'],
        ['Låg',    'Låg',    'Medium', 'Hög',    'Kritisk','Kritisk','Kritisk'],
        ['Låg',    'Medium', 'Hög',    'Kritisk','Kritisk','Kritisk','Kritisk'],
        ['Medium', 'Hög',    'Kritisk','Kritisk','Kritisk','Kritisk','Kritisk'],
    ],
}

_current_matrix = None


def _normalise_matrix(cfg: dict) -> dict:
    """Ensure a stored matrix config is internally consistent.

    Pads x_labels / y_labels and cell arrays to match rows/cols.
    Used once on load so the rest of the code can trust the structure.
    """
    rows = int(cfg.get('rows', 5))
    cols = int(cfg.get('cols', 7))

    # Pad or trim x_labels
    x = list(cfg.get('x_labels', []))
    while len(x) < cols:
        x.append(f'F{len(x) - 1}')
    cfg['x_labels'] = x[:cols]

    # Pad or trim y_labels
    y = list(cfg.get('y_labels', []))
    while len(y) < rows:
        y.append(f'C{len(y) + 1}')
    cfg['y_labels'] = y[:rows]

    # Pad or trim cell_colors / cell_labels
    def _pad_grid(grid, default_val):
        result = []
        for r in range(rows):
            row = list(grid[r]) if r < len(grid) else []
            while len(row) < cols:
                row.append(default_val)
            result.append(row[:cols])
        return result

    cfg['cell_colors']    = _pad_grid(cfg.get('cell_colors', []), '#27ae60')
    cfg['cell_labels']    = _pad_grid(cfg.get('cell_labels', []), 'Låg')
    cfg['cell_fg_colors'] = _pad_grid(cfg.get('cell_fg_colors', []), '#ffffff')
    cfg['rows'] = rows
    cfg['cols'] = cols
    return cfg


def load_matrix(db):
    global _current_matrix
    cfg = db.get_risk_matrix()
    if cfg:
        _current_matrix = _normalise_matrix(cfg)
    else:
        _current_matrix = DEFAULT_MATRIX


def get_matrix():
    return _current_matrix or DEFAULT_MATRIX


def risk_info(frequency, consequence):
    """Return (label, bg_color, fg_color) from matrix lookup.

    Data is always stored as cell_colors[cons_idx][freq_idx].
    x_axis only controls display orientation — not data access.
    """
    cfg   = get_matrix()
    rows  = cfg.get('rows', 5)   # consequence levels
    cols  = cfg.get('cols', 7)   # frequency levels
    c_idx = max(0, min(int(consequence) - 1, rows - 1))   # C=1 → 0
    f_idx = max(0, min(int(frequency)  + 1, cols - 1))   # F=-1 → 0
    try:
        color = cfg['cell_colors'][c_idx][f_idx]   # always [cons][freq]
        label = cfg['cell_labels'][c_idx][f_idx]
        if not color:
            color = '#27ae60'
        if not label:
            label = 'Låg'
    except (IndexError, KeyError, TypeError):
        color, label = '#27ae60', 'Låg'
    try:
        fg = cfg['cell_fg_colors'][c_idx][f_idx] or '#ffffff'
    except (IndexError, KeyError, TypeError):
        fg = '#ffffff'
    return label, color, fg


def _contrast_fg(bg_hex):
    """Return black or white text color for best contrast against bg_hex."""
    c = QColor(bg_hex)
    luminance = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255
    return '#000000' if luminance > 0.55 else '#ffffff'


def freq_axis_label(f_val: int) -> str:
    """Short configured label for a frequency value (-1..5). x_labels always stores freq labels."""
    cfg  = get_matrix()
    cols = cfg.get('cols', 7)
    idx  = max(0, min(int(f_val) + 1, cols - 1))
    lbls = cfg.get('x_labels', [])
    full = lbls[idx] if idx < len(lbls) else f'F={f_val}'
    return full.split()[0] if full.strip() else f'F={f_val}'


def cons_axis_label(c_val: int) -> str:
    """Short configured label for a consequence value (1..5). y_labels always stores cons labels."""
    cfg  = get_matrix()
    rows = cfg.get('rows', 5)
    idx  = max(0, min(int(c_val) - 1, rows - 1))
    lbls = cfg.get('y_labels', [])
    full = lbls[idx] if idx < len(lbls) else f'C={c_val}'
    return full.split()[0] if full.strip() else f'C={c_val}'


def effective_frequency(base_freq, rrf):
    """Reduce frequency by floor(log10(rrf)) steps; minimum F=-1."""
    if rrf <= 1:
        return base_freq
    reduction = int(math.log10(max(1, rrf)))
    return max(-1, base_freq - reduction)


# Keep old name as alias so any remaining callers don't crash immediately
effective_likelihood = effective_frequency


def prob_to_reduction(prob_pct) -> int:
    """Convert probability % to frequency step reduction.

    10%  → 1 step  (≈ RRF 10)
    1%   → 2 steps (≈ RRF 100)
    0.1% → 3 steps (≈ RRF 1000)
    ≥100% or ≤0% → 0 steps
    """
    try:
        p = float(prob_pct)
    except (TypeError, ValueError):
        return 0
    if p <= 0 or p >= 100:
        return 0
    return int(math.floor(-math.log10(p / 100.0)))


def total_freq_reduction(base_freq: int, safeguard_rrf: int,
                         fa_active: bool, fa_prob,
                         ignition_active: bool, ignition_prob,
                         extra_rfactors) -> tuple:
    """Return (final_freq, total_equivalent_rrf, total_steps).

    fa_prob / ignition_prob: probability in % (10.0 = 10% = −1 step).
    extra_rfactors: iterable of dicts with 'rrf' (also treated as %) and 'active'.
    """
    # Safeguards reduce by RRF steps
    sg_steps    = int(math.log10(max(1, safeguard_rrf))) if safeguard_rrf > 1 else 0
    fa_steps    = prob_to_reduction(fa_prob)    if fa_active    else 0
    ign_steps   = prob_to_reduction(ignition_prob) if ignition_active else 0
    extra_steps = sum(
        prob_to_reduction(rf.get('rrf', 10))
        for rf in extra_rfactors
        if rf.get('active')
    )
    total_steps = sg_steps + fa_steps + ign_steps + extra_steps
    total_rrf   = 10 ** total_steps if total_steps > 0 else 1
    return max(-1, base_freq - total_steps), total_rrf, total_steps


# ── Consequence chain definitions ────────────────────────────────────────────
# Each entry: (key, display_label, group_header_or_None)
_CHAIN_ITEMS = [
    # Intermediate event
    ('loc',           'LOC — Utsläpp / läcka',                    'Intermediär händelse'),
    # Ignition outcomes
    ('fire',          'Brand (pool fire / jet fire)',              'Antändning / explosion'),
    ('flash_fire',    'Flash fire',                                None),
    ('explosion',     'Explosion (VCE / BLEVE)',                   None),
    # Toxic / environmental
    ('toxic',         'Toxisk exponering',                         'Toxisk / miljö'),
    ('environmental', 'Miljöutsläpp',                              None),
    # Human / asset
    ('personnel',     'Personskador',                              'Personell / tillgång'),
    ('fatality',      'Dödsfall',                                  None),
    ('equipment',     'Utrustningsskador',                         None),
    ('production',    'Driftstopp / produktionsbortfall',          None),
    # User-defined
    ('custom',        'Övrigt (se text)',                          'Övrigt'),
]
_CHAIN_KEYS = [k for k, _, _ in _CHAIN_ITEMS]


def build_consequence_text(base: str, chain: dict) -> str:
    """Build full consequence description from base event + chain selections."""
    parts = [base.strip()] if base.strip() else []
    for key, label, _ in _CHAIN_ITEMS:
        if chain.get(key):
            # Use short label for the chain (without parenthetical detail)
            short = label.split('(')[0].strip().split(' — ')[-1].strip()
            parts.append(short)
    return ' → '.join(parts)


def parse_chain_from_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# Frequency F=-1..5, stored as integer in causes.likelihood
_FREQ_VALUES = [-1, 0, 1, 2, 3, 4, 5]
_FREQ_LABELS = [
    'F-1 – Otänkbar',
    'F0 – Extremt sällan',
    'F1 – Sällan',
    'F2 – Osannolik',
    'F3 – Möjlig',
    'F4 – Trolig',
    'F5 – Frekvent',
]

# Default frequency boundaries (events/year) between each F-column.
# 6 boundaries for 7 columns (F=-1..F5).
# freq < boundaries[0]       → F=-1
# boundaries[i] <= freq < boundaries[i+1] → F=i
# freq >= boundaries[5]      → F=5
DEFAULT_FREQ_BOUNDARIES = [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]


def freq_to_f_level(freq_per_year, boundaries=None) -> int:
    """Convert numeric frequency (events/year) to F-level (-1..5).

    0.05/year → F=3  (10-100 year interval)
    0.5/year  → F=4  (1-10 year interval)
    """
    if boundaries is None:
        cfg = get_matrix()
        boundaries = cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES)
    boundaries = sorted(float(b) for b in boundaries)
    if not freq_per_year or freq_per_year <= 0:
        return -1
    for i, b in enumerate(boundaries):
        if float(freq_per_year) < b:
            return i - 1
    return len(boundaries) - 1   # above all → F=5


def freq_to_idx(f: int) -> int:
    """Frequency value (-1..5) → combo-box index (0..6)."""
    return max(0, min(int(f) + 1, 6))

def idx_to_freq(i: int) -> int:
    """Combo-box index (0..6) → frequency value (-1..5)."""
    return i - 1

# Keep old alias so existing code that references _LIKE_LABELS doesn't crash
_LIKE_LABELS = _FREQ_LABELS

_SEV_LABELS  = ['C1 – Försumbar', 'C2 – Liten', 'C3 – Måttlig', 'C4 – Allvarlig', 'C5 – Katastrofal']
_RRF_VALUES  = [1, 10, 100, 1000, 10000]
_RRF_LABELS  = ['1 – Ingen', '10 – RRF10', '100 – RRF100', '1000 – RRF1000', '10000 – RRF10000']
_SG_TYPES      = ['BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt']
_MARKUP_COLORS = ['#E53935', '#F57C00', '#F9A825', '#388E3C',
                  '#00796B', '#1565C0', '#7B1FA2', '#FF4081']
_RISK_ICON   = {'Låg': '🟢', 'Medium': '🟡', 'Hög': '🟠', 'Kritisk': '🔴'}

# Component-specific standard causes seeded on first run.
# comp_type must match keys in COMPONENT_TYPES (pid_viewer.py).
_COMP_STD_CAUSES = {
    "Lågt flöde": {
        "Pump": [
            "Pump stopp",
            "Kavitation (reducerat flöde)",
            "Slitet impeller / reducerad kapacitet",
            "Pumptätning havererar (intern läcka)",
        ],
        "Kompressor": [
            "Kompressorstopp",
            "Kompressorsurge (reducerat flöde)",
        ],
        "Ventil": [
            "Reglerventil fastnar stängd (fail-closed)",
            "Spjäll stängt — kvarglömt efter underhåll",
            "FCV delvis stängd (stiction / positioneringsfel)",
            "Backventil fastnad stängd",
        ],
        "Rörledning": [
            "Igensatt filter / sil",
            "Rörblockering (avlagringar, hydrater)",
            "Blindplatta kvar efter underhåll",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Tank / Kärl": [
            "Låg nivå i matningskärl",
            "Kärl tömt / dränerat",
        ],
        "Värmeväxlare": [
            "Igensatta rör (processsida)",
            "Vakuumbrott / tömning av värmeväxlare",
        ],
    },
    "Högt flöde": {
        "Pump": [
            "Pump kör mot lågt mottryck (hög kapacitet)",
            "Felaktig pump installerad (för stor)",
        ],
        "Kompressor": [
            "Kompressor kör mot reducerat mottryck",
        ],
        "Ventil": [
            "Reglerventil fastnar öppen (fail-open)",
            "Spjäll öppnat av misstag",
            "FCV sitter kvar öppen (stiction)",
            "Backventil saknas / defekt (backflöde adderas)",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
    },
    "Omvänt flöde": {
        "Pump": [
            "Pump stopp + defekt backventil",
            "Pump roterar baklänges (felkopplad motor)",
        ],
        "Ventil": [
            "Backventil defekt / saknas",
            "Backventil fastnad öppen",
        ],
        "Rörledning": [
            "Sifonverkan",
            "Felaktig rörledningsdragning",
        ],
    },
    "Missriktat flöde": {
        "Ventil": [
            "Felöppen ventil på alternativ flödesväg",
            "Backventil saknas / defekt",
        ],
        "Rörledning": [
            "Felaktig rörkoppling (monteringsfel)",
        ],
    },
    "Högt tryck": {
        "Pump": [
            "Pump deadhead (strypt utlopp)",
            "Pump mot stängd utloppsventil",
        ],
        "Kompressor": [
            "Kompressor mot stängd utloppsventil",
            "Kompressor utan flödesavlastning (PD-typ)",
        ],
        "Ventil": [
            "Utloppsventil stängd / blockerad",
            "Reglerventil på trycksida fastnar stängd",
        ],
        "Rörledning": [
            "Vattenhammare (snabb stängning av ventil)",
            "Termisk expansion i avspärrat rörledningsavsnitt",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Tank / Kärl": [
            "Säkerhetsventil avspärrad (underhåll)",
            "Yttre brand (ångbildning)",
        ],
    },
    "Lågt tryck": {
        "Pump": [
            "Pump stopp (trycksidan faller)",
            "Pumphaveri",
        ],
        "Ventil": [
            "Utloppsventil öppnar (okontrollerat)",
            "Tryckreducerande ventil fastnar öppen",
        ],
        "Rörledning": [
            "Yttre läcka / rörbrott",
            "Flanskläckage",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Tank / Kärl": [
            "Dräneringsventil öppen / läckande",
            "Vakuum (för snabb tömning utan ventilering)",
        ],
    },
    "Hög nivå": {
        "Pump": [
            "Utloppspump stopp",
        ],
        "Ventil": [
            "Utloppsventil stängd / fastnad stängd",
            "Inloppsventil öppen (okontrollerat)",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Tank / Kärl": [
            "Skumbildning (skenbar hög nivå)",
            "Densitetsminskning (kokning / flash)",
        ],
    },
    "Låg nivå": {
        "Pump": [
            "Utloppspump kör med för högt flöde",
        ],
        "Ventil": [
            "Inloppsventil stängd / fastnad stängd",
            "Dräneringsventil öppen / läckande",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Tank / Kärl": [
            "Yttre läcka från kärlet",
            "Avrinning via öppet dräneringsuttag",
        ],
    },
    "Hög temperatur": {
        "Värmeväxlare": [
            "Otillräcklig kylning (kylmedelsflöde avbrutet)",
            "Luftkylare (fin-fan) fläktstopp",
            "Kylventil fastnar stängd",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Tank / Kärl": [
            "Exoterm reaktion / okontrollerad kemisk process",
        ],
    },
    "Låg temperatur": {
        "Värmeväxlare": [
            "Överkylning (för högt kylmedelsflöde)",
            "Ångförlust (värmemedium bortfaller)",
            "Värmeventil fastnar stängd",
        ],
        "Instrument / Sensor": [
            "Signalfel högt",
            "Signalfel lågt",
        ],
        "Rörledning": [
            "Yttre kyla utan värmespårning — isbildning",
        ],
    },
}


def _fix_instrument_causes_v2(conn):
    """Replace verbose v1 instrument cause descriptions with 'Felar högt' / 'Felar lågt'."""
    conn.execute("DELETE FROM standard_causes WHERE comp_type='Instrument / Sensor'")
    instrument_devs = [
        "Lågt flöde", "Högt flöde", "Högt tryck", "Lågt tryck",
        "Hög nivå", "Låg nivå", "Hög temperatur", "Låg temperatur",
    ]
    for dev_name in instrument_devs:
        row = conn.execute(
            "SELECT id FROM standard_deviations WHERE description=?", (dev_name,)).fetchone()
        if not row:
            continue
        dev_id = row[0]
        max_sort = (conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (dev_id,)).fetchone()[0] or 0)
        for i, desc in enumerate(["Felar högt", "Felar lågt"]):
            conn.execute(
                "INSERT INTO standard_causes (deviation_id, description, sort_order, comp_type)"
                " VALUES (?,?,?,?)",
                (dev_id, desc, max_sort + 1 + i, 'Instrument / Sensor'))


def _fix_instrument_causes_v3(conn):
    """Rename 'Felar högt/lågt' back to 'Signalfel högt/lågt'."""
    conn.execute(
        "UPDATE standard_causes SET description='Signalfel högt' "
        "WHERE comp_type='Instrument / Sensor' AND description='Felar högt'")
    conn.execute(
        "UPDATE standard_causes SET description='Signalfel lågt' "
        "WHERE comp_type='Instrument / Sensor' AND description='Felar lågt'")


def _seed_component_causes(conn):
    """Insert component-type-specific standard causes (idempotent)."""
    for dev_name, by_type in _COMP_STD_CAUSES.items():
        row = conn.execute(
            "SELECT id FROM standard_deviations WHERE description=?", (dev_name,)).fetchone()
        if not row:
            continue
        dev_id = row[0]
        max_sort = (conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (dev_id,)).fetchone()[0] or 0)
        sort_i = max_sort + 1
        for comp_type, causes in by_type.items():
            for c_desc in causes:
                exists = conn.execute(
                    "SELECT id FROM standard_causes "
                    "WHERE deviation_id=? AND description=? AND comp_type=?",
                    (dev_id, c_desc, comp_type)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO standard_causes "
                        "(deviation_id, description, sort_order, comp_type) VALUES (?,?,?,?)",
                        (dev_id, c_desc, sort_i, comp_type))
                    sort_i += 1


class Database:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        for sql in [
            "ALTER TABLE nodes ADD COLUMN markup_points TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN markup_style TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN pid_page INTEGER DEFAULT 0",
            "ALTER TABLE nodes ADD COLUMN media TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN pressure TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN temperature TEXT DEFAULT ''",
            "ALTER TABLE causes ADD COLUMN likelihood INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE causes ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE causes ADD COLUMN base_freq REAL DEFAULT NULL",
            "ALTER TABLE causes ADD COLUMN deviation_id INTEGER REFERENCES deviations(id)",
            "ALTER TABLE safeguards ADD COLUMN rrf INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE safeguards ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE consequences ADD COLUMN category TEXT DEFAULT ''",
            "ALTER TABLE consequences ADD COLUMN consequence_chain TEXT DEFAULT ''",
            "ALTER TABLE consequences ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE consequences ADD COLUMN fa_active INTEGER DEFAULT 0",
            "ALTER TABLE consequences ADD COLUMN fa_rrf INTEGER DEFAULT 10",
            "ALTER TABLE consequences ADD COLUMN ignition_active INTEGER DEFAULT 0",
            "ALTER TABLE consequences ADD COLUMN ignition_rrf INTEGER DEFAULT 10",
            "ALTER TABLE cause_markers ADD COLUMN component_tag TEXT DEFAULT ''",
            "ALTER TABLE standard_causes ADD COLUMN comp_type TEXT DEFAULT ''",
            "ALTER TABLE standard_causes ADD COLUMN frequency REAL DEFAULT NULL",
            "ALTER TABLE causes ADD COLUMN standard_cause_id INTEGER DEFAULT NULL",
            "ALTER TABLE safeguards ADD COLUMN sg_type TEXT DEFAULT 'Övrigt'",
            "ALTER TABLE node_markups ADD COLUMN font_size INTEGER DEFAULT 12",
        ]:
            try:
                self.conn.execute(sql)
            except Exception:
                pass

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pid_config (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS equipment_types (
                prefix         TEXT PRIMARY KEY,
                equipment_type TEXT NOT NULL,
                display_name   TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pid_identified_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_code TEXT NOT NULL UNIQUE,
                examples TEXT DEFAULT '', name_sv TEXT DEFAULT '',
                comp_type TEXT DEFAULT '', confirmed INTEGER DEFAULT 0,
                source TEXT DEFAULT 'scan'
            );
            CREATE TABLE IF NOT EXISTS tag_database (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_code TEXT NOT NULL, name_sv TEXT DEFAULT '',
                name_en TEXT DEFAULT '', category TEXT DEFAULT '',
                standard TEXT DEFAULT '', source TEXT DEFAULT 'excel',
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tag_database_settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS reduction_factors (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
                description     TEXT NOT NULL DEFAULT '',
                rrf             INTEGER NOT NULL DEFAULT 10,
                active          INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS equipment_catalog (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tag            TEXT NOT NULL,
                original_tag   TEXT DEFAULT '',
                prefix         TEXT DEFAULT '',
                pid_page       INTEGER DEFAULT 0,
                equipment_type TEXT DEFAULT '',
                description    TEXT DEFAULT '',
                is_ocr         INTEGER DEFAULT 0,
                include        INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS consequence_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deviations (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                description TEXT NOT NULL DEFAULT 'Övrigt'
            );
            CREATE TABLE IF NOT EXISTS cause_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cause_id INTEGER NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                component_type TEXT DEFAULT '', component_tag TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS consequence_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                target_name TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS safeguard_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                safeguard_id INTEGER NOT NULL REFERENCES safeguards(id) ON DELETE CASCADE,
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                tag TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pid_revisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                revision    TEXT NOT NULL DEFAULT '',
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT '',
                pdf_path    TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pid_sheets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                display_order INTEGER NOT NULL,
                physical_page INTEGER NOT NULL,
                sheet_name    TEXT DEFAULT '',
                revision_id   INTEGER DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS standard_deviations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                sort_order  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS standard_causes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                deviation_id INTEGER NOT NULL REFERENCES standard_deviations(id) ON DELETE CASCADE,
                description  TEXT NOT NULL,
                sort_order   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS node_markups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id    INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                type       TEXT NOT NULL DEFAULT 'polygon',
                points     TEXT DEFAULT '[]',
                label      TEXT DEFAULT '',
                color      TEXT DEFAULT '#1565C0',
                opacity    REAL DEFAULT 0.45,
                line_width INTEGER DEFAULT 12,
                font_size  INTEGER DEFAULT 12,
                visible    INTEGER DEFAULT 1,
                pid_page   INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0
            );
        """)

        if not self.conn.execute("SELECT COUNT(*) FROM consequence_categories").fetchone()[0]:
            for i, name in enumerate(['Person', 'Miljö', 'Ekonomi', 'Anläggning', 'Rykte']):
                self.conn.execute(
                    "INSERT INTO consequence_categories (name, sort_order) VALUES (?,?)", (name, i))

        # Seed component types from hardcoded COMPONENT_TYPES if table is empty
        if not self.conn.execute("SELECT COUNT(*) FROM component_types").fetchone()[0]:
            from pid_viewer import COMPONENT_TYPES as _CT
            for sort_i, (comp_name, modes) in enumerate(_CT.items()):
                cur = self.conn.execute(
                    "INSERT INTO component_types (name, sort_order) VALUES (?,?)",
                    (comp_name, sort_i))
                comp_id = cur.lastrowid
                for mode_i, mode in enumerate(modes):
                    self.conn.execute(
                        "INSERT INTO failure_modes (component_id, description, sort_order)"
                        " VALUES (?,?,?)", (comp_id, mode, mode_i))

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS component_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS failure_modes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id INTEGER NOT NULL REFERENCES component_types(id) ON DELETE CASCADE,
                description TEXT NOT NULL DEFAULT '',
                freq_per_year REAL DEFAULT NULL,
                sort_order INTEGER DEFAULT 0
            );
        """)

        # Seed missing deviation_id for existing causes
        orphan_nodes = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT node_id FROM causes WHERE deviation_id IS NULL").fetchall()]
        for nid in orphan_nodes:
            row = self.conn.execute(
                "SELECT id FROM deviations WHERE node_id=? AND description='Övrigt' LIMIT 1",
                (nid,)).fetchone()
            if row:
                dev_id = row[0]
            else:
                cur = self.conn.execute(
                    "INSERT INTO deviations (node_id, description) VALUES (?, 'Övrigt')", (nid,))
                dev_id = cur.lastrowid
            self.conn.execute(
                "UPDATE causes SET deviation_id=? WHERE node_id=? AND deviation_id IS NULL",
                (dev_id, nid))

        # Seed standard_deviations template library if empty
        if not self.conn.execute("SELECT COUNT(*) FROM standard_deviations").fetchone()[0]:
            _STD_CAUSES = {
                "Lågt flöde":    ["Stängd ventil", "Delvis stängd ventil", "Igensatt filter/sil",
                                   "Stoppad pump", "Igensatt rör/ledning", "Läckage uppströms",
                                   "Fel på reglerventil (ej öppnar)"],
                "Högt flöde":    ["Felöppen ventil", "Fel på reglerventil (ej stänger)",
                                   "Ökat drifttryck uppströms", "Ökad pumpkapacitet"],
                "Missriktat flöde": ["Felaktig rörledningsdragning", "Fel rörkoppling",
                                     "Backventil saknas / ur funktion"],
                "Omvänt flöde":  ["Backventil saknas / ur funktion", "Pumpfel – flöde vänds",
                                   "Tryckfall uppströms"],
                "Högt tryck":    ["Stängd utloppsventil", "Blockerat utlopp",
                                   "Ökat inflöde", "Övervärmd gas/vätska", "Felaktig tryckreglering"],
                "Lågt tryck":    ["Läckage i system", "Otäta flänsar/koppling",
                                   "Öppet/läckande utlopp", "Pumphaveri"],
                "Hög nivå":      ["Öppet inlopp", "Stängd utloppsventil", "Felaktig nivåreglering",
                                   "Läckage till kärl"],
                "Låg nivå":      ["Läckage i botten/sida", "Felaktig nivåreglering",
                                   "Stängd inloppsventil", "Pumphaveri"],
                "Hög temperatur": ["Värmeväxlare ur funktion", "Övervärmd inkommande fluid",
                                    "Felaktig temperaturreglering", "Exoterm reaktion"],
                "Låg temperatur": ["Kylmedelfel", "Underkylning av inkommande fluid",
                                    "Felaktig temperaturreglering"],
                "Avvikande sammansättning": ["Fel råvara", "Förorenad råvara",
                                              "Felaktig dosering", "Läckage av annat medium"],
                "Bortfall av hjälpsystem": ["Strömavbrott", "Instrumentluftsfel",
                                             "Kylarfel", "Automatikfel"],
                "Drift":         ["Mänskligt fel vid drift", "Felaktig procedur",
                                   "Kommunikationsfel"],
                "Underhåll":     ["Arbete på trycksatt system", "Felaktig isolering",
                                   "Verktyg kvar i system"],
                "Start-up / Shut-down": ["Felaktig sekvens", "Valves i fel läge",
                                          "Instrument ej kalibrerade"],
                "Övrigt":        [],
            }
            for sort_i, dev_name in enumerate(_DEVIATION_TYPES):
                cur = self.conn.execute(
                    "INSERT INTO standard_deviations (description, sort_order) VALUES (?,?)",
                    (dev_name, sort_i))
                dev_tmpl_id = cur.lastrowid
                for cause_i, c_desc in enumerate(_STD_CAUSES.get(dev_name, [])):
                    self.conn.execute(
                        "INSERT INTO standard_causes (deviation_id, description, sort_order)"
                        " VALUES (?,?,?)", (dev_tmpl_id, c_desc, cause_i))

        # Seed component-type-specific standard causes (one-time, idempotent)
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='comp_causes_seeded_v1'").fetchone():
            _seed_component_causes(self.conn)
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('comp_causes_seeded_v1','1')")

        # Replace verbose v1 instrument causes with simple "Felar högt" / "Felar lågt"
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='comp_causes_seeded_v2'").fetchone():
            _fix_instrument_causes_v2(self.conn)
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('comp_causes_seeded_v2','1')")

        # Rename "Felar högt/lågt" → "Signalfel högt/lågt"
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='comp_causes_seeded_v3'").fetchone():
            _fix_instrument_causes_v3(self.conn)
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('comp_causes_seeded_v3','1')")

        # Ensure every node has all standard deviations from template library
        std_devs = [r[0] for r in self.conn.execute(
            "SELECT description FROM standard_deviations ORDER BY sort_order").fetchall()]
        if not std_devs:
            std_devs = _DEVIATION_TYPES
        all_nodes = [r[0] for r in self.conn.execute("SELECT id FROM nodes").fetchall()]
        for nid in all_nodes:
            existing = {r[0] for r in self.conn.execute(
                "SELECT description FROM deviations WHERE node_id=?", (nid,)).fetchall()}
            for dev_type in std_devs:
                if dev_type not in existing:
                    self.conn.execute(
                        "INSERT INTO deviations (node_id, description) VALUES (?,?)",
                        (nid, dev_type))

        self.conn.commit()

    # ── Config ────────────────────────────────────────────────────────────────
    def get_config(self, key, default=None):
        row = self.conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default

    def set_config(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO app_config (key,value) VALUES (?,?)", (key, value))
        self.conn.commit()

    _DEFAULT_PALETTE = [
        {'name': 'Kritisk', 'color': '#e74c3c', 'fg_color': '#ffffff'},
        {'name': 'Hög',     'color': '#e67e22', 'fg_color': '#ffffff'},
        {'name': 'Medium',  'color': '#f39c12', 'fg_color': '#000000'},
        {'name': 'Låg',     'color': '#27ae60', 'fg_color': '#ffffff'},
    ]

    def get_color_palette(self):
        val = self.get_config('color_palette')
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return list(self._DEFAULT_PALETTE)

    def set_color_palette(self, palette):
        self.set_config('color_palette', json.dumps(palette))

    def get_risk_matrix(self):
        val = self.get_config('risk_matrix')
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return None

    def set_risk_matrix(self, cfg):
        self.set_config('risk_matrix', json.dumps(cfg))

    # ── Tag database ──────────────────────────────────────────────────────────
    def tag_database_entries(self, standard=None):
        if standard:
            return self.conn.execute(
                "SELECT * FROM tag_database WHERE standard=? AND active=1 ORDER BY tag_code",
                (standard,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM tag_database WHERE active=1 ORDER BY tag_code").fetchall()

    def tag_database_standards(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT standard FROM tag_database WHERE standard!='' ORDER BY standard"
        ).fetchall()]

    def import_tag_database_excel(self, filepath: str):
        """Import tag codes from all relevant sheets in the Excel file."""
        try:
            import openpyxl
            wb = openpyxl.load_workbook(filepath, data_only=True)
        except Exception as e:
            return 0, str(e)

        # Sheet name → standard name mapping
        SHEET_MAP = {
            'ISA-5.1':          'ISA-5.1',
            'SSG-5276':         'SSG-5276',
            'ISO-10628_14617':  'ISO-10628',
            'ISO-15519':        'ISO-15519',
            'IEC-DIN_EN_62424': 'IEC-62424',
            'DIN_19227_28000':  'DIN-19227',
            'PIP_PIC001':       'PIP-PIC001',
        }
        imported = 0
        for sheet_name, standard in SHEET_MAP.items():
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            # Find header row (look for 'Taggkod' / 'Tag code')
            header_row = None
            for r in ws.iter_rows(max_row=10, values_only=True):
                for cell in r:
                    if cell and 'taggkod' in str(cell).lower():
                        header_row = r
                        break
                if header_row:
                    break
            if not header_row:
                continue
            # Map column indices
            cols = {str(v).strip().lower(): i
                    for i, v in enumerate(header_row) if v}
            c_code = next((i for k, i in cols.items() if 'taggkod' in k or 'tag code' in k), 0)
            c_sv   = next((i for k, i in cols.items() if 'svenska' in k or 'sv' in k or 'benom' in k), 3)
            c_en   = next((i for k, i in cols.items() if 'english' in k or 'en' in k), 4)
            c_cat  = next((i for k, i in cols.items() if 'kategori' in k or 'categ' in k), 5)

            start_row = ws.max_row  # will be overridden
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if row and row[c_code] and str(row[c_code]).strip().lower() == \
                        (header_row[c_code] or '').lower():
                    start_row = i + 1
                    break

            for row in ws.iter_rows(min_row=start_row, values_only=True):
                if not row or not row[c_code]:
                    continue
                code = str(row[c_code]).strip().upper()
                if not code or len(code) > 10:
                    continue
                sv  = str(row[c_sv]).strip()  if c_sv < len(row) and row[c_sv] else ''
                en  = str(row[c_en]).strip()  if c_en < len(row) and row[c_en] else ''
                cat = str(row[c_cat]).strip() if c_cat < len(row) and row[c_cat] else ''
                # Upsert
                self.conn.execute(
                    "INSERT OR REPLACE INTO tag_database "
                    "(tag_code,name_sv,name_en,category,standard,source,active) "
                    "VALUES (?,?,?,?,?,'excel',1)",
                    (code, sv, en, cat, standard))
                imported += 1

        self.conn.commit()
        return imported, ''

    def tag_db_setting(self, key, default=None):
        r = self.conn.execute(
            "SELECT value FROM tag_database_settings WHERE key=?", (key,)).fetchone()
        return r['value'] if r else default

    def set_tag_db_setting(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO tag_database_settings (key,value) VALUES (?,?)",
            (key, str(value)))
        self.conn.commit()

    def tag_code_lookup(self, prefix: str) -> dict:
        """Look up a tag prefix in the active tag databases. Returns best match."""
        active_std = self.tag_db_setting('active_standard', '')
        if active_std:
            rows = self.conn.execute(
                "SELECT * FROM tag_database WHERE tag_code=? AND standard=? AND active=1",
                (prefix.upper(), active_std)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tag_database WHERE tag_code=? AND active=1",
                (prefix.upper(),)).fetchall()
        return dict(rows[0]) if rows else {}

    # ── PID identified tags ───────────────────────────────────────────────────
    def pid_identified_tags(self):
        return self.conn.execute(
            "SELECT * FROM pid_identified_tags ORDER BY tag_code").fetchall()

    def upsert_pid_tag(self, tag_code, examples, name_sv, comp_type):
        """Insert or update a scanned tag entry (keeps existing confirmed status)."""
        existing = self.conn.execute(
            "SELECT confirmed FROM pid_identified_tags WHERE tag_code=?",
            (tag_code,)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE pid_identified_tags SET examples=?,name_sv=?,"
                "comp_type=CASE WHEN confirmed=0 THEN ? ELSE comp_type END "
                "WHERE tag_code=?",
                (examples, name_sv, comp_type, tag_code))
        else:
            self.conn.execute(
                "INSERT INTO pid_identified_tags "
                "(tag_code,examples,name_sv,comp_type,confirmed) VALUES (?,?,?,?,0)",
                (tag_code, examples, name_sv, comp_type))
        self.conn.commit()

    def confirm_pid_tag(self, tag_code, comp_type, confirmed):
        self.conn.execute(
            "UPDATE pid_identified_tags SET comp_type=?,confirmed=? WHERE tag_code=?",
            (comp_type, int(confirmed), tag_code))
        self.conn.commit()

    def confirmed_comp_for_tag(self, prefix: str) -> str:
        """Return confirmed component type for a tag prefix, or ''."""
        r = self.conn.execute(
            "SELECT comp_type FROM pid_identified_tags "
            "WHERE tag_code=? AND confirmed=1", (prefix.upper(),)).fetchone()
        return r['comp_type'] if r else ''

    def all_active_tag_codes(self) -> list:
        """Return list of all active tag codes for highlight scanning."""
        active_std = self.tag_db_setting('active_standard', '')
        if active_std:
            rows = self.conn.execute(
                "SELECT tag_code FROM tag_database WHERE standard=? AND active=1",
                (active_std,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tag_code FROM tag_database WHERE active=1").fetchall()
        return [r[0] for r in rows]

    # ── Equipment catalog ─────────────────────────────────────────────────────
    def equipment_items(self):
        return self.conn.execute(
            "SELECT * FROM equipment_catalog ORDER BY prefix, tag").fetchall()

    def add_equipment_item(self, tag, original_tag, prefix, page, eq_type, desc, is_ocr):
        cur = self.conn.execute(
            "INSERT INTO equipment_catalog "
            "(tag,original_tag,prefix,pid_page,equipment_type,description,is_ocr,include) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (tag, original_tag, prefix, page, eq_type, desc, is_ocr))
        self.conn.commit()
        return cur.lastrowid

    def update_equipment_item(self, id_, tag, prefix, eq_type, desc):
        self.conn.execute(
            "UPDATE equipment_catalog SET tag=?,prefix=?,equipment_type=?,description=? WHERE id=?",
            (tag, prefix, eq_type, desc, id_))
        self.conn.commit()

    def delete_equipment_item(self, id_):
        self.conn.execute("DELETE FROM equipment_catalog WHERE id=?", (id_,))
        self.conn.commit()

    def clear_equipment_catalog(self):
        self.conn.execute("DELETE FROM equipment_catalog")
        self.conn.commit()

    # ── Equipment types ───────────────────────────────────────────────────────
    def get_equipment_type(self, prefix: str):
        """Return saved equipment_type for this prefix, or None."""
        row = self.conn.execute(
            "SELECT equipment_type FROM equipment_types WHERE prefix=?", (prefix,)).fetchone()
        return row['equipment_type'] if row else None

    def save_equipment_type(self, prefix: str, equipment_type: str, display_name: str = ''):
        self.conn.execute(
            "INSERT OR REPLACE INTO equipment_types (prefix, equipment_type, display_name) "
            "VALUES (?,?,?)", (prefix, equipment_type, display_name))
        self.conn.commit()

    def all_equipment_types(self):
        return self.conn.execute(
            "SELECT * FROM equipment_types ORDER BY prefix").fetchall()

    # ── Categories ────────────────────────────────────────────────────────────
    def consequence_categories(self):
        return self.conn.execute(
            "SELECT * FROM consequence_categories ORDER BY sort_order, name").fetchall()

    def add_category(self, name):
        cur = self.conn.execute(
            "INSERT INTO consequence_categories (name) VALUES (?)", (name,))
        self.conn.commit()
        return cur.lastrowid

    def update_category(self, id_, name):
        self.conn.execute("UPDATE consequence_categories SET name=? WHERE id=?", (name, id_))
        self.conn.commit()

    def delete_category(self, id_):
        self.conn.execute("DELETE FROM consequence_categories WHERE id=?", (id_,))
        self.conn.commit()

    # ── Component types & failure modes ───────────────────────────────────────
    def component_types(self):
        return self.conn.execute(
            "SELECT * FROM component_types ORDER BY sort_order, name").fetchall()

    def failure_modes(self, component_id):
        return self.conn.execute(
            "SELECT * FROM failure_modes WHERE component_id=? ORDER BY sort_order, id",
            (component_id,)).fetchall()

    def add_component_type(self, name):
        cur = self.conn.execute(
            "INSERT INTO component_types (name) VALUES (?)", (name,))
        self.conn.commit()
        return cur.lastrowid

    def update_component_type(self, id_, name):
        self.conn.execute("UPDATE component_types SET name=? WHERE id=?", (name, id_))
        self.conn.commit()

    def delete_component_type(self, id_):
        self.conn.execute("DELETE FROM component_types WHERE id=?", (id_,))
        self.conn.commit()

    def add_failure_mode(self, component_id, description, freq=None):
        cur = self.conn.execute(
            "INSERT INTO failure_modes (component_id, description, freq_per_year) VALUES (?,?,?)",
            (component_id, description, freq))
        self.conn.commit()
        return cur.lastrowid

    def update_failure_mode(self, id_, description, freq=None):
        self.conn.execute(
            "UPDATE failure_modes SET description=?, freq_per_year=? WHERE id=?",
            (description, freq, id_))
        self.conn.commit()

    def delete_failure_mode(self, id_):
        self.conn.execute("DELETE FROM failure_modes WHERE id=?", (id_,))
        self.conn.commit()

    def all_component_types_dict(self):
        """Return dict {type_name: [mode_description, ...]} for ComponentPickerDialog."""
        result = {}
        for ct in self.component_types():
            modes = [fm['description'] for fm in self.failure_modes(ct['id'])]
            result[ct['name']] = modes
        return result

    # ── P&ID helpers ──────────────────────────────────────────────────────────
    def get_pid_path(self):
        row = self.conn.execute("SELECT value FROM pid_config WHERE key='path'").fetchone()
        return row['value'] if row else None

    def set_pid_path(self, path):
        self.conn.execute(
            "INSERT OR REPLACE INTO pid_config (key,value) VALUES ('path',?)", (str(path),))
        self.conn.commit()

    # ── PID revisions & sheets ────────────────────────────────────────────────
    def add_revision(self, revision, notes, pdf_path, created_at=''):
        if not created_at:
            created_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        cur = self.conn.execute(
            "INSERT INTO pid_revisions (revision,notes,created_at,pdf_path) VALUES (?,?,?,?)",
            (revision, notes, created_at, str(pdf_path)))
        self.conn.commit()
        return cur.lastrowid

    def get_revisions(self):
        return self.conn.execute(
            "SELECT * FROM pid_revisions ORDER BY id DESC").fetchall()

    def ensure_sheets_initialized(self, page_count):
        existing = self.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]
        if existing == 0 and page_count > 0:
            for i in range(page_count):
                self.conn.execute(
                    "INSERT INTO pid_sheets (display_order,physical_page,sheet_name) VALUES (?,?,?)",
                    (i, i, f"Blad {i + 1}"))
            self.conn.commit()

    def get_sheets(self):
        return self.conn.execute(
            "SELECT * FROM pid_sheets ORDER BY display_order").fetchall()

    def append_sheets(self, physical_pages, sheet_names, revision_id=None):
        max_row = self.conn.execute(
            "SELECT MAX(display_order) FROM pid_sheets").fetchone()[0]
        start_order = (max_row + 1) if max_row is not None else 0
        for i, (phys, name) in enumerate(zip(physical_pages, sheet_names)):
            self.conn.execute(
                "INSERT INTO pid_sheets (display_order,physical_page,sheet_name,revision_id) "
                "VALUES (?,?,?,?)",
                (start_order + i, phys, name, revision_id))
        self.conn.commit()

    def reorder_sheets(self, ordered_ids):
        for disp_order, sheet_id in enumerate(ordered_ids):
            self.conn.execute(
                "UPDATE pid_sheets SET display_order=? WHERE id=?",
                (disp_order, sheet_id))
        self.conn.commit()

    def update_sheet_name(self, id_, name):
        self.conn.execute("UPDATE pid_sheets SET sheet_name=? WHERE id=?", (name, id_))
        self.conn.commit()

    def delete_sheets(self, ids):
        for id_ in ids:
            self.conn.execute("DELETE FROM pid_sheets WHERE id=?", (id_,))
        remaining = self.conn.execute(
            "SELECT id FROM pid_sheets ORDER BY display_order").fetchall()
        for disp_order, row in enumerate(remaining):
            self.conn.execute(
                "UPDATE pid_sheets SET display_order=? WHERE id=?",
                (disp_order, row['id']))
        self.conn.commit()

    def get_sheet_physical_page(self, display_index):
        row = self.conn.execute(
            "SELECT physical_page FROM pid_sheets ORDER BY display_order "
            "LIMIT 1 OFFSET ?", (display_index,)).fetchone()
        return row['physical_page'] if row else display_index

    def get_display_page_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]

    def clear_sheets(self):
        self.conn.execute("DELETE FROM pid_sheets")
        self.conn.commit()

    def add_node_with_markup(self, name, points, style, page):
        cur = self.conn.execute(
            "INSERT INTO nodes (name, markup_points, markup_style, pid_page) VALUES (?,?,?,?)",
            (name, json.dumps(points), json.dumps(style), page))
        self.conn.commit()
        return cur.lastrowid

    def add_cause_marker(self, cause_id, page, x, y, comp_type, tag=''):
        self.conn.execute(
            "INSERT INTO cause_markers (cause_id,pid_page,x,y,component_type,component_tag) VALUES (?,?,?,?,?,?)",
            (cause_id, page, x, y, comp_type, tag))
        self.conn.commit()

    def add_consequence_marker(self, cons_id, page, x, y, target):
        self.conn.execute(
            "INSERT INTO consequence_markers (consequence_id,pid_page,x,y,target_name) VALUES (?,?,?,?,?)",
            (cons_id, page, x, y, target))
        self.conn.commit()

    def add_safeguard_marker(self, sg_id, page, x, y, tag=''):
        self.conn.execute(
            "INSERT INTO safeguard_markers (safeguard_id,pid_page,x,y,tag) VALUES (?,?,?,?,?)",
            (sg_id, page, x, y, tag))
        self.conn.commit()

    def cause_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM cause_markers WHERE pid_page=?", (page,)).fetchall()

    def consequence_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM consequence_markers WHERE pid_page=?", (page,)).fetchall()

    def safeguard_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM safeguard_markers WHERE pid_page=?", (page,)).fetchall()

    def marked_cause_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT cause_id FROM cause_markers").fetchall()}

    def marked_consequence_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT consequence_id FROM consequence_markers").fetchall()}

    def marked_safeguard_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT safeguard_id FROM safeguard_markers").fetchall()}

    def remove_cause_marker(self, cause_id):
        self.conn.execute("DELETE FROM cause_markers WHERE cause_id=?", (cause_id,))
        self.conn.commit()

    def remove_consequence_marker(self, consequence_id):
        self.conn.execute("DELETE FROM consequence_markers WHERE consequence_id=?", (consequence_id,))
        self.conn.commit()

    def remove_safeguard_marker(self, safeguard_id):
        self.conn.execute("DELETE FROM safeguard_markers WHERE safeguard_id=?", (safeguard_id,))
        self.conn.commit()

    def get_cause_marker(self, cause_id):
        row = self.conn.execute(
            "SELECT pid_page, x, y FROM cause_markers WHERE cause_id=? LIMIT 1",
            (cause_id,)).fetchone()
        return dict(row) if row else None

    def cause_markers_for_cause(self, cause_id):
        """Return all markers for a specific cause (page, x, y, comp_type, tag)."""
        return self.conn.execute(
            "SELECT pid_page, x, y, component_type, component_tag "
            "FROM cause_markers WHERE cause_id=?",
            (cause_id,)).fetchall()

    def get_consequence_marker(self, consequence_id):
        row = self.conn.execute(
            "SELECT pid_page, x, y FROM consequence_markers WHERE consequence_id=? LIMIT 1",
            (consequence_id,)).fetchone()
        return dict(row) if row else None

    def get_safeguard_marker(self, safeguard_id):
        row = self.conn.execute(
            "SELECT pid_page, x, y FROM safeguard_markers WHERE safeguard_id=? LIMIT 1",
            (safeguard_id,)).fetchone()
        return dict(row) if row else None

    # ── Queries ───────────────────────────────────────────────────────────────
    def nodes(self):
        return self.conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()

    def causes(self, node_id):
        return self.conn.execute(
            "SELECT * FROM causes WHERE node_id=? ORDER BY id", (node_id,)).fetchall()

    def consequences(self, cause_id):
        return self.conn.execute(
            "SELECT * FROM consequences WHERE cause_id=? ORDER BY id", (cause_id,)).fetchall()

    def safeguards(self, consequence_id):
        return self.conn.execute(
            "SELECT * FROM safeguards WHERE consequence_id=? ORDER BY id", (consequence_id,)).fetchall()

    def actions(self, consequence_id):
        return self.conn.execute(
            "SELECT * FROM actions WHERE consequence_id=? ORDER BY id", (consequence_id,)).fetchall()

    def get_node(self, id_):
        return self.conn.execute("SELECT * FROM nodes WHERE id=?", (id_,)).fetchone()

    def get_cause(self, id_):
        return self.conn.execute("SELECT * FROM causes WHERE id=?", (id_,)).fetchone()

    def get_consequence(self, id_):
        return self.conn.execute("SELECT * FROM consequences WHERE id=?", (id_,)).fetchone()

    def get_safeguard(self, id_):
        return self.conn.execute("SELECT * FROM safeguards WHERE id=?", (id_,)).fetchone()

    # ── Add ───────────────────────────────────────────────────────────────────
    def add_node(self):
        cur = self.conn.execute("INSERT INTO nodes (name) VALUES ('Ny nod')")
        node_id = cur.lastrowid
        std = [r[0] for r in self.conn.execute(
            "SELECT description FROM standard_deviations ORDER BY sort_order").fetchall()]
        for dev_type in (std or _DEVIATION_TYPES):
            self.conn.execute(
                "INSERT INTO deviations (node_id, description) VALUES (?,?)",
                (node_id, dev_type))
        self.conn.commit()
        return node_id

    def deviations(self, node_id):
        return self.conn.execute(
            "SELECT * FROM deviations WHERE node_id=? ORDER BY id", (node_id,)).fetchall()

    def get_deviation(self, id_):
        return self.conn.execute("SELECT * FROM deviations WHERE id=?", (id_,)).fetchone()

    def causes_for_deviation(self, deviation_id):
        return self.conn.execute(
            "SELECT * FROM causes WHERE deviation_id=? ORDER BY id", (deviation_id,)).fetchall()

    def causes_for_node_excluding_deviation(self, node_id, deviation_id):
        """Return causes for the node that belong to OTHER deviations (for reuse dialog)."""
        return self.conn.execute(
            "SELECT c.id, c.description, d.description AS deviation_name, d.id AS deviation_id "
            "FROM causes c "
            "JOIN deviations d ON c.deviation_id = d.id "
            "WHERE d.node_id=? AND d.id!=? "
            "ORDER BY d.id, c.id",
            (node_id, deviation_id)).fetchall()

    def add_deviation(self, node_id, description="Övrigt"):
        cur = self.conn.execute(
            "INSERT INTO deviations (node_id, description) VALUES (?,?)", (node_id, description))
        self.conn.commit()
        return cur.lastrowid

    def update_deviation(self, id_, description):
        self.conn.execute("UPDATE deviations SET description=? WHERE id=?", (description, id_))
        self.conn.commit()

    def delete_deviation(self, id_):
        for cause in self.causes_for_deviation(id_):
            self.delete_cause(cause['id'])
        self.conn.execute("DELETE FROM deviations WHERE id=?", (id_,))
        self.conn.commit()

    def get_or_create_deviation(self, node_id, description="Övrigt"):
        row = self.conn.execute(
            "SELECT id FROM deviations WHERE node_id=? AND description=? ORDER BY id LIMIT 1",
            (node_id, description)).fetchone()
        return row[0] if row else self.add_deviation(node_id, description)

    # ── Standard deviation / cause template library ───────────────────────────
    def standard_deviations(self):
        return self.conn.execute(
            "SELECT * FROM standard_deviations ORDER BY sort_order, id").fetchall()

    def add_standard_deviation(self, description):
        max_ord = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_deviations").fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO standard_deviations (description, sort_order) VALUES (?,?)",
            (description, max_ord + 1))
        self.conn.commit()
        return cur.lastrowid

    def update_standard_deviation(self, id_, description):
        self.conn.execute(
            "UPDATE standard_deviations SET description=? WHERE id=?", (description, id_))
        self.conn.commit()

    def delete_standard_deviation(self, id_):
        self.conn.execute("DELETE FROM standard_deviations WHERE id=?", (id_,))
        self.conn.commit()

    def reorder_standard_deviations(self, ordered_ids):
        for i, id_ in enumerate(ordered_ids):
            self.conn.execute(
                "UPDATE standard_deviations SET sort_order=? WHERE id=?", (i, id_))
        self.conn.commit()

    def get_standard_cause(self, id_):
        row = self.conn.execute(
            "SELECT * FROM standard_causes WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def standard_causes(self, deviation_id):
        return self.conn.execute(
            "SELECT * FROM standard_causes WHERE deviation_id=? ORDER BY sort_order, id",
            (deviation_id,)).fetchall()

    def standard_causes_for_name(self, deviation_name):
        row = self.conn.execute(
            "SELECT id FROM standard_deviations WHERE description=? LIMIT 1",
            (deviation_name,)).fetchone()
        if not row:
            return []
        return self.standard_causes(row[0])

    def add_standard_cause(self, deviation_id, description):
        max_ord = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (deviation_id,)).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, sort_order) VALUES (?,?,?)",
            (deviation_id, description, max_ord + 1))
        self.conn.commit()
        return cur.lastrowid

    def update_standard_cause(self, id_, description=None, **kwargs):
        sets, vals = [], []
        if description is not None:
            sets.append("description=?"); vals.append(description)
        if 'frequency' in kwargs:
            sets.append("frequency=?"); vals.append(kwargs['frequency'])
        if sets:
            vals.append(id_)
            self.conn.execute(f"UPDATE standard_causes SET {', '.join(sets)} WHERE id=?", vals)
            self.conn.commit()

    def delete_standard_cause(self, id_):
        self.conn.execute("DELETE FROM standard_causes WHERE id=?", (id_,))
        self.conn.commit()

    def reorder_standard_causes(self, ordered_ids):
        for i, id_ in enumerate(ordered_ids):
            self.conn.execute(
                "UPDATE standard_causes SET sort_order=? WHERE id=?", (i, id_))
        self.conn.commit()

    def distinct_comp_types(self):
        """Return sorted list of all comp_type values used in standard_causes (excl. empty)."""
        rows = self.conn.execute(
            "SELECT DISTINCT comp_type FROM standard_causes "
            "WHERE comp_type != '' ORDER BY comp_type").fetchall()
        return [r[0] for r in rows]

    def standard_causes_for_comp_type(self, comp_type):
        """Return all standard_causes for comp_type, annotated with deviation description."""
        return self.conn.execute(
            "SELECT sc.id, sc.description, sc.sort_order, sc.comp_type, "
            "sd.description AS deviation_name, sd.id AS deviation_id "
            "FROM standard_causes sc "
            "JOIN standard_deviations sd ON sc.deviation_id = sd.id "
            "WHERE sc.comp_type=? ORDER BY sd.sort_order, sc.sort_order",
            (comp_type,)).fetchall()

    def add_standard_cause_for_comp_type(self, deviation_id, description, comp_type):
        max_ord = (self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (deviation_id,)).fetchone()[0] or 0)
        cur = self.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, sort_order, comp_type)"
            " VALUES (?,?,?,?)", (deviation_id, description, max_ord + 1, comp_type))
        self.conn.commit()
        return cur.lastrowid

    def add_cause(self, deviation_id):
        dev = self.get_deviation(deviation_id)
        node_id = dev['node_id'] if dev else None
        cur = self.conn.execute(
            "INSERT INTO causes (node_id,deviation_id,description,likelihood) VALUES (?,?,'Ny orsak',1)",
            (node_id, deviation_id))
        self.conn.commit()
        return cur.lastrowid

    def add_consequence(self, cause_id):
        cur = self.conn.execute(
            "INSERT INTO consequences (cause_id,description,severity) VALUES (?,'Ny konsekvens',1)", (cause_id,))
        self.conn.commit()
        return cur.lastrowid

    def add_safeguard(self, consequence_id):
        cur = self.conn.execute(
            "INSERT INTO safeguards (consequence_id,description,rrf) VALUES (?,'Ny safeguard',1)", (consequence_id,))
        self.conn.commit()
        return cur.lastrowid

    def add_action(self, consequence_id):
        cur = self.conn.execute(
            "INSERT INTO actions (consequence_id,description,status) VALUES (?,'Ny åtgärd','Öppen')",
            (consequence_id,))
        self.conn.commit()
        return cur.lastrowid

    # ── Update ────────────────────────────────────────────────────────────────
    def update_node(self, id_, name, description, pid_ref,
                    media='', pressure='', temperature=''):
        self.conn.execute(
            "UPDATE nodes SET name=?,description=?,pid_ref=?,"
            "media=?,pressure=?,temperature=? WHERE id=?",
            (name, description, pid_ref, media, pressure, temperature, id_))
        self.conn.commit()

    # ── Node markup CRUD ──────────────────────────────────────────────────────
    def add_node_markup(self, node_id, type_, pts, label, color, opacity, line_width, page,
                        font_size=12):
        cur = self.conn.execute(
            "INSERT INTO node_markups (node_id,type,points,label,color,opacity,line_width,"
            "font_size,pid_page) VALUES (?,?,?,?,?,?,?,?,?)",
            (node_id, type_, json.dumps(pts), label, color, opacity, line_width,
             font_size, page))
        self.conn.commit()
        return cur.lastrowid

    def node_markups_for_node(self, node_id):
        return self.conn.execute(
            "SELECT * FROM node_markups WHERE node_id=? ORDER BY sort_order,id",
            (node_id,)).fetchall()

    def node_markups_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM node_markups WHERE pid_page=? ORDER BY sort_order,id",
            (page,)).fetchall()

    def get_node_markup(self, mu_id):
        return self.conn.execute(
            "SELECT * FROM node_markups WHERE id=?", (mu_id,)).fetchone()

    def update_node_markup(self, mu_id, label=None, color=None, opacity=None,
                           line_width=None, font_size=None, visible=None, points=None):
        sets, vals = [], []
        if label      is not None: sets.append("label=?");      vals.append(label)
        if color      is not None: sets.append("color=?");      vals.append(color)
        if opacity    is not None: sets.append("opacity=?");    vals.append(opacity)
        if line_width is not None: sets.append("line_width=?"); vals.append(line_width)
        if font_size  is not None: sets.append("font_size=?");  vals.append(font_size)
        if visible    is not None: sets.append("visible=?");    vals.append(int(visible))
        if points     is not None: sets.append("points=?");     vals.append(json.dumps(points))
        if sets:
            vals.append(mu_id)
            self.conn.execute(f"UPDATE node_markups SET {','.join(sets)} WHERE id=?", vals)
            self.conn.commit()

    def delete_node_markup(self, mu_id):
        self.conn.execute("DELETE FROM node_markups WHERE id=?", (mu_id,))
        self.conn.commit()

    def set_all_node_markups_visible(self, node_id, visible):
        self.conn.execute("UPDATE node_markups SET visible=? WHERE node_id=?",
                          (int(visible), node_id))
        self.conn.commit()

    def has_node_markups(self, node_id) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM node_markups WHERE node_id=?", (node_id,)).fetchone()
        return r[0] > 0

    def has_visible_node_markups(self, node_id) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM node_markups WHERE node_id=? AND visible=1",
            (node_id,)).fetchone()
        return r[0] > 0

    def get_node_number(self, node_id) -> int:
        """Return 1-based position of node_id in creation order (0 if not found)."""
        rows = self.conn.execute("SELECT id FROM nodes ORDER BY id").fetchall()
        for i, r in enumerate(rows, 1):
            if r[0] == node_id:
                return i
        return 0

    def sync_node_text_markups(self, node_id, new_name):
        """Update label of all 'text' type markups for a node to match its new name."""
        self.conn.execute(
            "UPDATE node_markups SET label=? WHERE node_id=? AND type='text'",
            (new_name, node_id))
        self.conn.commit()

    _SENTINEL = object()

    def update_cause(self, id_, description=None, likelihood=None, base_freq=_SENTINEL,
                     standard_cause_id=_SENTINEL):
        sets, vals = [], []
        if description is not None:
            sets.append("description=?"); vals.append(description)
        if likelihood is not None:
            sets.append("likelihood=?"); vals.append(likelihood)
        if base_freq is not Database._SENTINEL:
            sets.append("base_freq=?"); vals.append(base_freq)
        if standard_cause_id is not Database._SENTINEL:
            sets.append("standard_cause_id=?"); vals.append(standard_cause_id)
        if sets:
            vals.append(id_)
            self.conn.execute(f"UPDATE causes SET {', '.join(sets)} WHERE id=?", vals)
            self.conn.commit()

    def update_cause_freqs_from_standard(self):
        """Overwrite base_freq on all causes linked to a standard cause that has a frequency."""
        self.conn.execute("""
            UPDATE causes
            SET base_freq = (
                SELECT frequency FROM standard_causes WHERE id = causes.standard_cause_id
            )
            WHERE standard_cause_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM standard_causes
                WHERE id = causes.standard_cause_id AND frequency IS NOT NULL
              )
        """)
        self.conn.commit()
        return self.conn.execute("SELECT changes()").fetchone()[0]

    def update_consequence(self, id_, description, severity, category='',
                           consequence_chain=''):
        self.conn.execute(
            "UPDATE consequences SET description=?,severity=?,category=?,"
            "consequence_chain=? WHERE id=?",
            (description, severity, category, consequence_chain, id_))
        self.conn.commit()

    def update_safeguard(self, id_, description, rrf=1, sg_type='Övrigt'):
        self.conn.execute("UPDATE safeguards SET description=?,rrf=?,sg_type=? WHERE id=?",
                          (description, rrf, sg_type, id_))
        self.conn.commit()

    def update_action(self, id_, description, responsible, due_date, status):
        self.conn.execute(
            "UPDATE actions SET description=?,responsible=?,due_date=?,status=? WHERE id=?",
            (description, responsible, due_date, status, id_))
        self.conn.commit()

    # ── Delete ────────────────────────────────────────────────────────────────
    def delete_node(self, id_):
        self.conn.execute("DELETE FROM nodes WHERE id=?", (id_,)); self.conn.commit()

    def delete_cause(self, id_):
        self.conn.execute("DELETE FROM causes WHERE id=?", (id_,)); self.conn.commit()

    def delete_consequence(self, id_):
        self.conn.execute("DELETE FROM consequences WHERE id=?", (id_,)); self.conn.commit()

    def delete_safeguard(self, id_):
        self.conn.execute("DELETE FROM safeguards WHERE id=?", (id_,)); self.conn.commit()

    def delete_action(self, id_):
        self.conn.execute("DELETE FROM actions WHERE id=?", (id_,)); self.conn.commit()

    # ── Reduction factors ─────────────────────────────────────────────────────
    def reduction_factors(self, consequence_id):
        return self.conn.execute(
            "SELECT * FROM reduction_factors WHERE consequence_id=? ORDER BY id",
            (consequence_id,)).fetchall()

    def add_reduction_factor(self, consequence_id, description='', rrf=10):
        cur = self.conn.execute(
            "INSERT INTO reduction_factors (consequence_id,description,rrf,active) VALUES (?,?,?,1)",
            (consequence_id, description, rrf))
        self.conn.commit()
        return cur.lastrowid

    def update_reduction_factor(self, id_, description, rrf, active):
        self.conn.execute(
            "UPDATE reduction_factors SET description=?,rrf=?,active=? WHERE id=?",
            (description, rrf, int(active), id_))
        self.conn.commit()

    def delete_reduction_factor(self, id_):
        self.conn.execute("DELETE FROM reduction_factors WHERE id=?", (id_,))
        self.conn.commit()

    def update_consequence_factors(self, id_, fa_active, fa_rrf, ignition_active, ignition_rrf):
        self.conn.execute(
            "UPDATE consequences SET fa_active=?,fa_rrf=?,ignition_active=?,ignition_rrf=? WHERE id=?",
            (int(fa_active), fa_rrf, int(ignition_active), ignition_rrf, id_))
        self.conn.commit()

    # ── Copy support ──────────────────────────────────────────────────────────
    def copy_cause(self, cause_id, target_deviation_id):
        orig = self.get_cause(cause_id)
        if not orig:
            return None
        dev = self.get_deviation(target_deviation_id)
        node_id = dev['node_id'] if dev else orig['node_id']
        cur = self.conn.execute(
            "INSERT INTO causes (node_id,deviation_id,description,likelihood,source_id) VALUES (?,?,?,?,?)",
            (node_id, target_deviation_id, orig['description'], orig['likelihood'], cause_id))
        self.conn.commit()
        return cur.lastrowid

    def copy_consequence(self, cons_id, target_cause_id):
        orig = self.get_consequence(cons_id)
        if not orig:
            return None
        cur = self.conn.execute(
            "INSERT INTO consequences (cause_id,description,severity,category,"
            "fa_active,fa_rrf,ignition_active,ignition_rrf,source_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (target_cause_id, orig['description'], orig['severity'], orig['category'] or '',
             orig['fa_active'] or 0, orig['fa_rrf'] or 10,
             orig['ignition_active'] or 0, orig['ignition_rrf'] or 10, cons_id))
        self.conn.commit()
        new_id = cur.lastrowid
        # Copy safeguards
        for sg in self.safeguards(cons_id):
            self.conn.execute(
                "INSERT INTO safeguards (consequence_id,description,rrf,sg_type,source_id) VALUES (?,?,?,?,?)",
                (new_id, sg['description'], sg['rrf'], sg.get('sg_type','Övrigt'), sg['id']))
        # Copy reduction factors
        for rf in self.reduction_factors(cons_id):
            self.conn.execute(
                "INSERT INTO reduction_factors (consequence_id,description,rrf,active) VALUES (?,?,?,?)",
                (new_id, rf['description'], rf['rrf'], rf['active']))
        self.conn.commit()
        return new_id

    def copy_safeguard(self, sg_id, target_cons_id):
        orig = self.get_safeguard(sg_id)
        if not orig:
            return None
        cur = self.conn.execute(
            "INSERT INTO safeguards (consequence_id,description,rrf,sg_type,source_id) VALUES (?,?,?,?,?)",
            (target_cons_id, orig['description'], orig['rrf'],
             dict(orig).get('sg_type', 'Övrigt'), sg_id))
        self.conn.commit()
        return cur.lastrowid

    # ── Move support ──────────────────────────────────────────────────────────
    def move_cause(self, cause_id, target_node_id):
        self.conn.execute("UPDATE causes SET node_id=? WHERE id=?",
                          (target_node_id, cause_id))
        self.conn.commit()

    def move_consequence(self, cons_id, target_cause_id):
        self.conn.execute("UPDATE consequences SET cause_id=? WHERE id=?",
                          (target_cause_id, cons_id))
        self.conn.commit()

    def move_safeguard(self, sg_id, target_cons_id):
        self.conn.execute("UPDATE safeguards SET consequence_id=? WHERE id=?",
                          (target_cons_id, sg_id))
        self.conn.commit()

    def stats(self):
        return {
            'nodes':        self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            'causes':       self.conn.execute("SELECT COUNT(*) FROM causes").fetchone()[0],
            'consequences': self.conn.execute("SELECT COUNT(*) FROM consequences").fetchone()[0],
            'safeguards':   self.conn.execute("SELECT COUNT(*) FROM safeguards").fetchone()[0],
            'open_actions': self.conn.execute(
                "SELECT COUNT(*) FROM actions WHERE status='Öppen'").fetchone()[0],
        }

    def all_data(self):
        rows = []
        for node in self.nodes():
            for cause in self.causes(node['id']):
                for cons in self.consequences(cause['id']):
                    sgs = [dict(s) for s in self.safeguards(cons['id'])]
                    acts = [dict(a) for a in self.actions(cons['id'])]
                    rows.append({
                        'node_name':      node['name'],
                        'node_pid':       node['pid_ref'] or '',
                        'cause_id':       cause['id'],
                        'cause':          cause['description'],
                        'likelihood':     cause['likelihood'] if cause['likelihood'] is not None else 3,
                        'consequence_id': cons['id'],
                        'consequence':    cons['description'],
                        'severity':       cons['severity'],
                        'category':       cons['category'] or '',
                        'safeguards':     sgs,
                        'safeguards_text': '; '.join(s['description'] for s in sgs),
                        'actions':        acts,
                    })
        return rows


# ══════════════════════════════════════════════════════════════════════════════
# SHARED WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

class RiskBadge(QLabel):
    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(200, 38)
        self.setWordWrap(True)
        f = QFont(); f.setBold(True)
        self.setFont(f)
        self.set_empty()

    def update_risk(self, frequency, consequence, base_freq=None):
        label, bg, fg = risk_info(frequency, consequence)
        if base_freq is not None:
            freq_str = f"{base_freq:g}/år"
            self.setText(f"{label}  F={frequency} C={consequence}\n🗄️ {freq_str}")
        else:
            self.setText(f"{label}  F={frequency} C={consequence}")
        self.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:5px; padding:2px 8px;")

    def set_empty(self):
        self.setText("—  (ingen frekvens)")
        self.setStyleSheet(
            "background:#f0f0f0; color:#aaa; border-radius:5px; "
            "padding:2px 8px; border:1px solid #ddd;")


class SafeguardEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None
        self._parent_cause_likelihood = 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        btn = QPushButton("+ Lägg till safeguard")
        btn.clicked.connect(self._add)
        layout.addWidget(btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Beskrivning', 'Typ', 'RRF', 'Eff. risk', ''])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 65)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFixedHeight(160)
        layout.addWidget(self.table)

    def load(self, consequence_id, cause_likelihood=1):
        self.consequence_id = consequence_id
        self._parent_cause_likelihood = cause_likelihood
        self._refresh()

    def _refresh(self):
        try:
            self.table.cellChanged.disconnect()
        except Exception:
            pass
        self.table.setRowCount(0)
        if self.consequence_id is None:
            return
        cons = self.db.get_consequence(self.consequence_id)
        severity = dict(cons)['severity'] if cons else 1
        for sg in self.db.safeguards(self.consequence_id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            sg_d = dict(sg)

            item = QTableWidgetItem(sg_d['description'])
            item.setData(Qt.ItemDataRole.UserRole, sg_d['id'])
            self.table.setItem(row, 0, item)

            sid = sg_d['id']
            type_combo = QComboBox()
            type_combo.addItems(_SG_TYPES)
            sg_type = sg_d.get('sg_type', 'Övrigt') or 'Övrigt'
            type_combo.setCurrentIndex(_SG_TYPES.index(sg_type) if sg_type in _SG_TYPES else len(_SG_TYPES)-1)
            type_combo.currentIndexChanged.connect(
                lambda idx, s=sid, r=row: self._type_changed(s, r, idx))
            self.table.setCellWidget(row, 1, type_combo)

            rrf_combo = QComboBox()
            rrf_combo.addItems(_RRF_LABELS)
            sg_rrf_val = sg_d['rrf'] if sg_d['rrf'] is not None else 1
            rrf_idx = _RRF_VALUES.index(sg_rrf_val) if sg_rrf_val in _RRF_VALUES else 0
            rrf_combo.setCurrentIndex(rrf_idx)
            rrf_combo.currentIndexChanged.connect(
                lambda idx, s=sid, r=row: self._rrf_changed(s, r, idx))
            self.table.setCellWidget(row, 2, rrf_combo)

            eff_f = effective_frequency(self._parent_cause_likelihood, sg_d['rrf'] or 1)
            badge = RiskBadge()
            badge.update_risk(eff_f, severity)
            self.table.setCellWidget(row, 3, badge)

            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(lambda _, s=sid: self._delete(s))
            self.table.setCellWidget(row, 4, del_btn)

        self.table.cellChanged.connect(self._cell_changed)

    def _add(self):
        if self.consequence_id is None:
            return
        self.db.add_safeguard(self.consequence_id)
        self._refresh()
        self.changed.emit()

    def _delete(self, sg_id):
        self.db.delete_safeguard(sg_id)
        self._refresh()
        self.changed.emit()

    def _type_changed(self, sg_id, row, idx):
        sg_type = _SG_TYPES[idx]
        item = self.table.item(row, 0)
        desc = item.text() if item else ''
        rrf_w = self.table.cellWidget(row, 2)
        rrf = _RRF_VALUES[rrf_w.currentIndex()] if rrf_w else 1
        self.db.update_safeguard(sg_id, desc, rrf, sg_type)
        self.changed.emit()

    def _rrf_changed(self, sg_id, row, idx):
        rrf = _RRF_VALUES[idx]
        item = self.table.item(row, 0)
        desc = item.text() if item else ''
        type_w = self.table.cellWidget(row, 1)
        sg_type = _SG_TYPES[type_w.currentIndex()] if type_w else 'Övrigt'
        self.db.update_safeguard(sg_id, desc, rrf, sg_type)
        self._refresh()
        self.changed.emit()

    def _cell_changed(self, row, col):
        if col != 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        sg_id = item.data(Qt.ItemDataRole.UserRole)
        type_w = self.table.cellWidget(row, 1)
        sg_type = _SG_TYPES[type_w.currentIndex()] if type_w else 'Övrigt'
        rrf_w = self.table.cellWidget(row, 2)
        rrf = _RRF_VALUES[rrf_w.currentIndex()] if rrf_w else 1
        self.db.update_safeguard(sg_id, item.text(), rrf, sg_type)


class ActionEditor(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        btn = QPushButton("+ Lägg till åtgärd")
        btn.clicked.connect(self._add)
        layout.addWidget(btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Åtgärd', 'Ansvarig', 'Datum', 'Status', ''])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i, w in zip([1, 2, 3, 4], [100, 90, 90, 72]):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(i, w)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table)

    def load(self, consequence_id):
        self.consequence_id = consequence_id
        self._refresh()

    def _refresh(self):
        try:
            self.table.cellChanged.disconnect()
        except Exception:
            pass
        self.table.setRowCount(0)
        if self.consequence_id is None:
            return
        for act in self.db.actions(self.consequence_id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            desc = QTableWidgetItem(act['description'])
            desc.setData(Qt.ItemDataRole.UserRole, act['id'])
            self.table.setItem(row, 0, desc)
            self.table.setItem(row, 1, QTableWidgetItem(act['responsible'] or ''))
            self.table.setItem(row, 2, QTableWidgetItem(act['due_date'] or ''))
            combo = QComboBox()
            combo.addItems(['Öppen', 'Pågår', 'Klar'])
            combo.setCurrentText(act['status'] or 'Öppen')
            aid = act['id']
            combo.currentTextChanged.connect(lambda s, a=aid, r=row: self._save_row(r))
            self.table.setCellWidget(row, 3, combo)
            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(lambda _, a=aid: self._delete(a))
            self.table.setCellWidget(row, 4, del_btn)
        self.table.cellChanged.connect(self._cell_changed)

    def _add(self):
        if self.consequence_id is None:
            return
        self.db.add_action(self.consequence_id)
        self._refresh()

    def _delete(self, act_id):
        self.db.delete_action(act_id)
        self._refresh()

    def _cell_changed(self, row, col):
        if col <= 2:
            self._save_row(row)

    def _save_row(self, row):
        item = self.table.item(row, 0)
        if not item:
            return
        act_id = item.data(Qt.ItemDataRole.UserRole)
        desc   = item.text()
        resp   = self.table.item(row, 1).text() if self.table.item(row, 1) else ''
        due    = self.table.item(row, 2).text() if self.table.item(row, 2) else ''
        combo  = self.table.cellWidget(row, 3)
        status = combo.currentText() if combo else 'Öppen'
        self.db.update_action(act_id, desc, resp, due, status)


# ══════════════════════════════════════════════════════════════════════════════
# DETAIL PANELS
# ══════════════════════════════════════════════════════════════════════════════

class WelcomePanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel("Välj ett objekt i trädet\neller skapa en ny nod.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(); f.setPointSize(13)
        lbl.setFont(f)
        lbl.setStyleSheet("color: #888;")
        layout.addWidget(lbl)


class NodePanel(QWidget):
    saved = pyqtSignal(int, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.node_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 10, 10, 10)

        self._title_lbl = QLabel("Nod")
        f = QFont(); f.setPointSize(12); f.setBold(True)
        self._title_lbl.setFont(f)
        layout.addWidget(self._title_lbl)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("t.ex. Pump P-101")
        self.name_edit.editingFinished.connect(self._save)
        form.addRow("Namn:", self.name_edit)

        self.pid_edit = QLineEdit()
        self.pid_edit.setPlaceholderText("t.ex. P&ID-001")
        self.pid_edit.editingFinished.connect(self._save)
        form.addRow("P&ID-ref:", self.pid_edit)

        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Beskrivning av noden / systemgränsen...")
        self.desc_edit.setFixedHeight(55)
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        def _desc_foe(e, _w=_w, _s=_s, _orig=_orig_foe):
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        form.addRow("Beskrivning:", self.desc_edit)

        sep2 = QLabel("Processparametrar")
        f2 = QFont(); f2.setBold(True); f2.setPointSize(9)
        sep2.setFont(f2)
        sep2.setStyleSheet("color:#1F4E79; margin-top:2px;")
        form.addRow(sep2)

        self.media_edit = QLineEdit()
        self.media_edit.setPlaceholderText("t.ex. Vätgas (H₂), Vatten, Naturgas, Ammoniak")
        self.media_edit.editingFinished.connect(self._save)
        form.addRow("Media:", self.media_edit)

        self.pressure_edit = QLineEdit()
        self.pressure_edit.setPlaceholderText("t.ex. 10 bar g,  0–25 barg,  1.5 MPa")
        self.pressure_edit.editingFinished.connect(self._save)
        form.addRow("Tryck:", self.pressure_edit)

        self.temperature_edit = QLineEdit()
        self.temperature_edit.setPlaceholderText("t.ex. 150 °C,  −20 till 80 °C")
        self.temperature_edit.editingFinished.connect(self._save)
        form.addRow("Temperatur:", self.temperature_edit)

        layout.addLayout(form)
        layout.addStretch()

    def load(self, node_id):
        self.node_id = node_id
        n = self.db.get_node_number(node_id)
        self._title_lbl.setText(f"Nod {n}" if n else "Nod")
        row = self.db.get_node(node_id)
        if row:
            self._loading = True
            self.name_edit.setText(row['name'])
            self.pid_edit.setText(row['pid_ref'] or '')
            self.desc_edit.setPlainText(row['description'] or '')
            self.media_edit.setText(row['media'] or '')
            self.pressure_edit.setText(row['pressure'] or '')
            self.temperature_edit.setText(row['temperature'] or '')
            self._loading = False

    def _save(self):
        if self._loading or self.node_id is None:
            return
        name = self.name_edit.text().strip() or 'Ny nod'
        self.db.update_node(
            self.node_id, name,
            self.desc_edit.toPlainText(),
            self.pid_edit.text(),
            self.media_edit.text(),
            self.pressure_edit.text(),
            self.temperature_edit.text())
        self.saved.emit(self.node_id, name)


class DeviationPanel(QWidget):
    saved        = pyqtSignal(int, str)   # (id_, description)
    add_cause_requested = pyqtSignal(int) # (deviation_id)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.deviation_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Avvikelse")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("T.ex. Högt flöde, Lågt tryck, Övrigt…")
        self.desc_edit.editingFinished.connect(self._save)
        form.addRow("Beskrivning:", self.desc_edit)
        layout.addLayout(form)

        self._add_btn = QPushButton("⚙  Lägg till orsak")
        self._add_btn.setEnabled(False)
        self._add_btn.setStyleSheet(
            "QPushButton{background:#1F4E79;color:white;border:none;"
            "border-radius:4px;padding:7px;font-weight:bold;margin-top:8px;}"
            "QPushButton:hover{background:#2563a8;}"
            "QPushButton:disabled{background:#aaa;}")
        self._add_btn.clicked.connect(
            lambda: self.add_cause_requested.emit(self.deviation_id))
        layout.addWidget(self._add_btn)
        layout.addStretch()

    def load(self, deviation_id):
        self.deviation_id = deviation_id
        self._add_btn.setEnabled(True)
        dev = self.db.get_deviation(deviation_id)
        if not dev:
            return
        self._loading = True
        self.desc_edit.setText(dev['description'])
        self._loading = False

    def _save(self):
        if self._loading or self.deviation_id is None:
            return
        desc = self.desc_edit.text().strip() or 'Övrigt'
        self.db.update_deviation(self.deviation_id, desc)
        self.saved.emit(self.deviation_id, desc)


class CausePanel(QWidget):
    saved        = pyqtSignal(int, str)
    place_on_pid = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Orsak (Cause)")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Beskriv orsaken till avvikelsen / faran...")
        self.desc_edit.setFixedHeight(80)
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        def _desc_foe(e, _w=_w, _s=_s, _orig=_orig_foe):
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        form.addRow("Beskrivning:", self.desc_edit)

        layout.addLayout(form)

        # Compact frequency section
        freq_box = QGroupBox("Frekvens")
        freq_box.setStyleSheet(
            "QGroupBox{font-size:10px;color:#555;border:1px solid #ddd;"
            "border-radius:4px;margin-top:4px;padding-top:2px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:6px;}")
        freq_lay = QFormLayout(freq_box)
        freq_lay.setSpacing(4)
        freq_lay.setContentsMargins(6, 4, 6, 4)
        freq_lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._db_freq_lbl = QLabel("—")
        self._db_freq_lbl.setStyleSheet(
            "color:#1F4E79; font-style:italic; font-size:10px; padding:1px 3px;"
            "background:#eef4fb; border:1px solid #bee3f8; border-radius:3px;")
        self._db_freq_lbl.setToolTip(
            "F-nivå beräknad från frekvens definierad i Inställningar → Standardorsaker.\n"
            "Tomt om ingen frekvens är definierad för denna orsak.")
        freq_lay.addRow("Standard:", self._db_freq_lbl)

        self.freq_combo = QComboBox()
        self.freq_combo.setMaximumWidth(180)
        self.freq_combo.addItem("— (välj)")
        self.freq_combo.addItems(_FREQ_LABELS)
        self.freq_combo.currentIndexChanged.connect(self._save)
        freq_lay.addRow("Manuell:", self.freq_combo)

        layout.addWidget(freq_box)

        self._pid_btn = QPushButton("📍 Lägg till på P&ID")
        self._pid_btn.setEnabled(False)
        self._pid_btn.setToolTip("Växla till P&ID-läge för att placera en orsaksmakör")
        self._pid_btn.setStyleSheet(
            "QPushButton{background:#1F4E79;color:white;border:none;"
            "border-radius:4px;padding:7px;font-weight:bold;}"
            "QPushButton:hover{background:#2563a8;}"
            "QPushButton:disabled{background:#aaa;}")
        self._pid_btn.clicked.connect(self.place_on_pid)
        layout.addWidget(self._pid_btn)
        layout.addStretch()

    def load(self, cause_id):
        self.cause_id = cause_id
        self._pid_btn.setEnabled(True)
        row = self.db.get_cause(cause_id)
        if not row:
            return
        self._loading = True
        self.desc_edit.setPlainText(row['description'])

        # Field 1: frequency — prefer live standard_causes lookup if linked
        std_cause_id = row['standard_cause_id'] if 'standard_cause_id' in row.keys() else None
        base_freq = None
        if std_cause_id:
            sc = self.db.get_standard_cause(std_cause_id)
            if sc and sc.get('frequency') is not None:
                base_freq = sc['frequency']
        if base_freq is None:
            base_freq = row['base_freq'] if 'base_freq' in row.keys() else None
        if base_freq is not None:
            f_auto = freq_to_f_level(base_freq)
            f_lbl  = _FREQ_LABELS[freq_to_idx(f_auto)] if freq_to_idx(f_auto) < len(_FREQ_LABELS) else f'F={f_auto}'
            suffix = "  🗄️" if std_cause_id else ""
            self._db_freq_lbl.setText(f"F={f_auto} — {base_freq:.4g}/år  →  {f_lbl}{suffix}")
        else:
            self._db_freq_lbl.setText("—")

        # Field 2: manual F-level (likelihood)
        like = row['likelihood']
        if like is not None:
            self.freq_combo.setCurrentIndex(freq_to_idx(like) + 1)  # +1 for the "—" item
        else:
            self.freq_combo.setCurrentIndex(0)   # "— (välj)"
        self._loading = False

    def _save(self):
        if self._loading or self.cause_id is None:
            return
        desc = self.desc_edit.toPlainText().strip() or 'Ny orsak'
        idx  = self.freq_combo.currentIndex()
        if idx == 0:
            # "— (välj)" — keep existing likelihood, just save description
            self.db.update_cause(self.cause_id, description=desc)
        else:
            freq = idx_to_freq(idx - 1)   # -1 for the "—" item
            self.db.update_cause(self.cause_id, desc, freq)
        self.saved.emit(self.cause_id, desc)


class ConsequencePanel(QWidget):
    saved        = pyqtSignal(int)
    place_on_pid = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None
        self._loading = False
        self._chain = {}
        self._chain_checks = {}
        # Initialise preview label early so _rebuild_preview is always safe
        self._chain_preview = QLabel("—")

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 12, 16, 12)

        title = QLabel("Konsekvens (Consequence)")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        # ── Beskrivning (bas-händelse) ─────────────────────────────────────────
        desc_box = QGroupBox("Händelse / Direkt konsekvens")
        desc_lay = QVBoxLayout(desc_box)
        desc_lay.setSpacing(4)
        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText(
            "Beskriv den direkta händelsen, t.ex. 'Högt flöde till T-001'")
        self.desc_edit.setFixedHeight(65)
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        _rb = self._rebuild_preview
        def _desc_foe(e, _w=_w, _s=_s, _rb=_rb, _orig=_orig_foe):
            _rb()
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        desc_lay.addWidget(self.desc_edit)
        layout.addWidget(desc_box)

        # ── Konsekvenskedja ───────────────────────────────────────────────────
        chain_box = QGroupBox("Konsekvenskedja  (kryssa i för att bygga kedjan)")
        chain_lay = QVBoxLayout(chain_box)
        chain_lay.setSpacing(4)

        last_group = None
        grid = QGridLayout(); grid.setSpacing(3)
        col, row_idx = 0, 0

        for key, label, group in _CHAIN_ITEMS:
            if group and group != last_group:
                if col > 0:
                    row_idx += 1; col = 0
                hdr = QLabel(group)
                hdr.setStyleSheet("color:#1F4E79; font-weight:bold; font-size:10px; margin-top:4px;")
                grid.addWidget(hdr, row_idx, 0, 1, 3)
                row_idx += 1; col = 0
                last_group = group

            chk = QCheckBox(label)
            chk.toggled.connect(self._on_chain_changed)
            self._chain_checks[key] = chk
            grid.addWidget(chk, row_idx, col)
            col += 1
            if col >= 3:
                col = 0; row_idx += 1

        chain_lay.addLayout(grid)

        # Preview of generated text (widget was pre-created in __init__)
        sep_lbl = QLabel("Genererad text:")
        sep_lbl.setStyleSheet("color:#555; font-size:10px; margin-top:4px;")
        chain_lay.addWidget(sep_lbl)
        self._chain_preview.setStyleSheet(
            "color:#1F4E79; font-weight:bold; font-size:11px;"
            "background:#eef4fb; border:1px solid #bee3f8; border-radius:3px; padding:3px 6px;")
        self._chain_preview.setWordWrap(True)
        chain_lay.addWidget(self._chain_preview)

        apply_btn = QPushButton("↑ Tillämpa genererad text i beskrivningsfältet")
        apply_btn.setStyleSheet(
            "font-size:10px; padding:2px 8px; background:#1F4E79; color:white;"
            "border:none; border-radius:3px;")
        apply_btn.clicked.connect(self._apply_chain_to_desc)
        chain_lay.addWidget(apply_btn)

        layout.addWidget(chain_box)

        # ── Riskbedömning ─────────────────────────────────────────────────────
        risk_box = QGroupBox("Riskbedömning")
        risk_lay = QFormLayout(risk_box)
        risk_lay.setSpacing(6)

        self.sev_combo = QComboBox()
        self.sev_combo.addItems(_SEV_LABELS)
        self.sev_combo.currentIndexChanged.connect(self._risk_changed)
        risk_lay.addRow("Konsekvens (C):", self.sev_combo)

        self.cat_combo = QComboBox()
        self.cat_combo.currentIndexChanged.connect(self._save)
        risk_lay.addRow("Kategori:", self.cat_combo)

        self.risk_badge = RiskBadge()
        risk_lay.addRow("Risknivå:", self.risk_badge)
        layout.addWidget(risk_box)

        # ── Safeguards + Åtgärder ─────────────────────────────────────────────
        sg_box = QGroupBox("Safeguards")
        sg_lay = QVBoxLayout(sg_box)
        self.sg_editor = SafeguardEditor(db)
        sg_lay.addWidget(self.sg_editor)
        layout.addWidget(sg_box)

        act_box = QGroupBox("Åtgärder / Rekommendationer")
        act_lay = QVBoxLayout(act_box)
        self.act_editor = ActionEditor(db)
        act_lay.addWidget(self.act_editor)
        layout.addWidget(act_box)

        self._pid_btn = QPushButton("📍 Lägg till på P&ID")
        self._pid_btn.setEnabled(False)
        self._pid_btn.setToolTip("Växla till P&ID-läge för att placera en konsekvensmarkör")
        self._pid_btn.setStyleSheet(
            "QPushButton{background:#1F4E79;color:white;border:none;"
            "border-radius:4px;padding:7px;font-weight:bold;}"
            "QPushButton:hover{background:#2563a8;}"
            "QPushButton:disabled{background:#aaa;}")
        self._pid_btn.clicked.connect(self.place_on_pid)
        layout.addWidget(self._pid_btn)
        layout.addStretch()

    # ── Chain helpers ─────────────────────────────────────────────────────────

    def _rebuild_preview(self):
        if not hasattr(self, '_chain_preview') or self._chain_preview is None:
            return
        base = self.desc_edit.toPlainText().strip() if hasattr(self, 'desc_edit') else ''
        text = build_consequence_text(base, self._chain)
        self._chain_preview.setText(text if text else "—")

    def _on_chain_changed(self):
        if self._loading:
            return
        self._chain = {k: chk.isChecked() for k, chk in self._chain_checks.items()}
        self._rebuild_preview()
        self._save()

    def _apply_chain_to_desc(self):
        """Copy generated text into the description field."""
        text = build_consequence_text(
            self.desc_edit.toPlainText().strip(), self._chain)
        if text:
            self._loading = True
            self.desc_edit.setPlainText(text)
            self._loading = False
        self._save()

    # ── Load / Save ───────────────────────────────────────────────────────────

    def _load_categories(self):
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItem('')
        for cat in self.db.consequence_categories():
            self.cat_combo.addItem(cat['name'])
        self.cat_combo.blockSignals(False)

    def load(self, consequence_id):
        self.consequence_id = consequence_id
        self._pid_btn.setEnabled(True)
        self._load_categories()
        row = self.db.get_consequence(consequence_id)
        if row:
            self._loading = True
            self.desc_edit.setPlainText(row['description'])
            self.sev_combo.setCurrentIndex(max(0, (row['severity'] or 1) - 1))
            cat = row['category'] or ''
            idx = self.cat_combo.findText(cat)
            self.cat_combo.setCurrentIndex(max(0, idx))
            # Restore chain checkboxes
            self._chain = parse_chain_from_json(
                row['consequence_chain'] if 'consequence_chain' in row.keys() else '')
            for key, chk in self._chain_checks.items():
                chk.setChecked(bool(self._chain.get(key, False)))
            self._loading = False

        self._rebuild_preview()

        cause_id = dict(row)['cause_id'] if row else None
        base_freq = None
        std_linked = False
        freq = 3
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                std_cause_id = cause['standard_cause_id'] if 'standard_cause_id' in cause.keys() else None
                if std_cause_id:
                    sc = self.db.get_standard_cause(std_cause_id)
                    if sc and sc.get('frequency') is not None:
                        base_freq = sc['frequency']
                        std_linked = True
                if base_freq is None:
                    base_freq = cause['base_freq'] if 'base_freq' in cause.keys() else None
                if base_freq is not None:
                    freq = freq_to_f_level(base_freq)
                elif cause['likelihood'] is not None:
                    freq = cause['likelihood']
        sev = (row['severity'] or 1) if row else 1
        if base_freq is not None:
            self.risk_badge.update_risk(freq, sev, base_freq=base_freq if std_linked else None)
        else:
            self.risk_badge.set_empty()
        self.sg_editor.load(consequence_id, freq)
        self.act_editor.load(consequence_id)

    def _risk_changed(self):
        if not self._loading:
            self._save()

    def _save(self):
        if self._loading or self.consequence_id is None:
            return
        sev   = self.sev_combo.currentIndex() + 1
        desc  = self.desc_edit.toPlainText().strip() or 'Ny konsekvens'
        cat   = self.cat_combo.currentText()
        chain = json.dumps(self._chain)
        self.db.update_consequence(self.consequence_id, desc, sev, cat, chain)
        self.saved.emit(self.consequence_id)


class SafeguardPanel(QWidget):
    saved        = pyqtSignal(int)
    place_on_pid = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.safeguard_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Safeguard")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Beskriv safeguarden...")
        self.desc_edit.setFixedHeight(80)
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        def _desc_foe(e, _w=_w, _s=_s, _orig=_orig_foe):
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        form.addRow("Beskrivning:", self.desc_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(_SG_TYPES)
        self.type_combo.currentIndexChanged.connect(self._save)
        form.addRow("Typ:", self.type_combo)

        self.rrf_combo = QComboBox()
        self.rrf_combo.addItems(_RRF_LABELS)
        self.rrf_combo.currentIndexChanged.connect(self._save)
        form.addRow("RRF:", self.rrf_combo)

        self.risk_badge = RiskBadge()
        form.addRow("Effektiv risk:", self.risk_badge)

        layout.addLayout(form)

        self._pid_btn = QPushButton("📍 Lägg till på P&ID")
        self._pid_btn.setEnabled(False)
        self._pid_btn.setToolTip("Växla till P&ID-läge för att placera en safeguardmarkör")
        self._pid_btn.setStyleSheet(
            "QPushButton{background:#1F4E79;color:white;border:none;"
            "border-radius:4px;padding:7px;font-weight:bold;}"
            "QPushButton:hover{background:#2563a8;}"
            "QPushButton:disabled{background:#aaa;}")
        self._pid_btn.clicked.connect(self.place_on_pid)
        layout.addWidget(self._pid_btn)
        layout.addStretch()

    def load(self, safeguard_id):
        self.safeguard_id = safeguard_id
        self._pid_btn.setEnabled(True)
        sg = self.db.get_safeguard(safeguard_id)
        if not sg:
            return
        self._loading = True
        sg_d = dict(sg)
        self.desc_edit.setPlainText(sg_d['description'])
        sg_type = sg_d.get('sg_type', 'Övrigt') or 'Övrigt'
        self.type_combo.setCurrentIndex(
            _SG_TYPES.index(sg_type) if sg_type in _SG_TYPES else len(_SG_TYPES)-1)
        rrf = sg_d['rrf'] if sg_d['rrf'] in _RRF_VALUES else 1
        self.rrf_combo.setCurrentIndex(_RRF_VALUES.index(rrf))
        self._update_badge(sg_d)
        self._loading = False

    def _update_badge(self, sg=None):
        if sg is None:
            sg = self.db.get_safeguard(self.safeguard_id)
            if not sg:
                return
        cons = self.db.get_consequence(sg['consequence_id'])
        if not cons:
            return
        cause = self.db.get_cause(cons['cause_id'])
        freq = cause['likelihood'] if cause and cause['likelihood'] is not None else 3
        sev = cons['severity'] or 1
        rrf = _RRF_VALUES[self.rrf_combo.currentIndex()]
        eff_f = effective_frequency(freq, rrf)
        self.risk_badge.update_risk(eff_f, sev)

    def _save(self):
        if self._loading or self.safeguard_id is None:
            return
        desc    = self.desc_edit.toPlainText().strip() or 'Ny safeguard'
        rrf     = _RRF_VALUES[self.rrf_combo.currentIndex()]
        sg_type = _SG_TYPES[self.type_combo.currentIndex()]
        self.db.update_safeguard(self.safeguard_id, desc, rrf, sg_type)
        self._update_badge()
        self.saved.emit(self.safeguard_id)


# ══════════════════════════════════════════════════════════════════════════════
# NODE MARKUP — RIBBON + STYLE POPUP + TABLE PANEL
# ══════════════════════════════════════════════════════════════════════════════

# ── Ribbon icon renderer ──────────────────────────────────────────────────────

def _mk_pm(name: str, sz: int, fg: QColor) -> QPixmap:
    """Render one icon onto a transparent QPixmap using QPainter."""
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    m  = sz * 0.11                 # margin
    S  = sz - 2 * m               # drawable area side
    sw = max(1.6, sz * 0.078)     # stroke width
    dr = max(2.0, sz * 0.075)     # vertex dot radius

    def pt(fx, fy):
        return QPointF(m + S * fx, m + S * fy)

    pen = QPen(fg, sw, Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    no_pen   = QPen(Qt.PenStyle.NoPen)
    solid_br = QBrush(fg)
    no_br    = QBrush(Qt.BrushStyle.NoBrush)

    if name == 'close':
        p.setPen(QPen(fg, sw * 1.5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(pt(0.08, 0.08), pt(0.92, 0.92))
        p.drawLine(pt(0.92, 0.08), pt(0.08, 0.92))

    elif name == 'select':
        # Classic cursor arrow: tip at top-left, shaft goes down-right
        path = QPainterPath()
        coords = [
            (0.00, 0.00),   # tip
            (0.00, 0.82),   # left edge base
            (0.26, 0.60),   # inner notch left
            (0.46, 1.00),   # shaft bottom right-inner
            (0.60, 0.93),   # shaft bottom right-outer
            (0.38, 0.54),   # inner notch right
            (0.66, 0.54),   # arrowhead right shoulder
        ]
        first = pt(*coords[0])
        path.moveTo(first)
        for c in coords[1:]:
            path.lineTo(pt(*c))
        path.closeSubpath()
        p.setPen(pen)
        p.setBrush(solid_br)
        p.drawPath(path)

    elif name == 'polygon':
        # Irregular quadrilateral that reads as "polygon" + vertex dots
        verts = [pt(0.10, 0.10), pt(0.90, 0.18),
                 pt(0.82, 0.90), pt(0.12, 0.78)]
        poly = QPolygonF(verts)
        p.setPen(pen)
        p.setBrush(no_br)
        p.drawPolygon(poly)
        p.setPen(no_pen)
        p.setBrush(solid_br)
        for v in verts:
            p.drawEllipse(v, dr, dr)

    elif name == 'polyline':
        # 4-point zigzag with vertex dots
        verts = [pt(0.04, 0.75), pt(0.34, 0.15),
                 pt(0.66, 0.68), pt(0.96, 0.10)]
        p.setPen(pen)
        for i in range(len(verts) - 1):
            p.drawLine(verts[i], verts[i + 1])
        p.setPen(no_pen)
        p.setBrush(solid_br)
        for v in verts:
            p.drawEllipse(v, dr, dr)

    elif name == 'text':
        # Bold "T" — same family as a label/text tool
        font = QFont("Arial", max(10, int(sz * 0.60)))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(fg))
        p.drawText(QRectF(0, 0, sz, sz),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "T")

    elif name == 'comment':
        # Speech bubble: rounded rect body + filled triangle tail
        bw, bh = S, S * 0.70
        radius = S * 0.15
        p.setPen(pen)
        p.setBrush(no_br)
        p.drawRoundedRect(QRectF(m, m, bw, bh), radius, radius)
        # Tail
        tail = QPolygonF([
            pt(0.16, 0.68),
            pt(0.36, 0.68),
            pt(0.18, 1.00),
        ])
        p.setPen(no_pen)
        p.setBrush(solid_br)
        p.drawPolygon(tail)
        # Two horizontal text-lines inside the bubble
        lpen = QPen(fg, sw * 0.75, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(lpen)
        p.drawLine(pt(0.22, 0.28), pt(0.82, 0.28))
        p.drawLine(pt(0.22, 0.50), pt(0.70, 0.50))

    elif name == 'eye':
        # Almond outline + solid pupil
        cy_f = 0.50
        path = QPainterPath()
        path.moveTo(pt(0.02, cy_f))
        path.cubicTo(pt(0.25, cy_f - 0.32), pt(0.75, cy_f - 0.32), pt(0.98, cy_f))
        path.cubicTo(pt(0.75, cy_f + 0.32), pt(0.25, cy_f + 0.32), pt(0.02, cy_f))
        p.setPen(pen)
        p.setBrush(no_br)
        p.drawPath(path)
        pr = S * 0.14
        p.setPen(no_pen)
        p.setBrush(solid_br)
        p.drawEllipse(pt(0.50, cy_f), pr, pr)

    elif name == 'smart':
        # Pipe-route icon: horizontal entry, 90° bend, vertical exit, with endpoint dots
        p.setPen(pen)
        p.setBrush(no_br)
        # Horizontal segment bottom-left
        p.drawLine(pt(0.05, 0.75), pt(0.45, 0.75))
        # Bend corner
        p.drawLine(pt(0.45, 0.75), pt(0.45, 0.25))
        # Horizontal segment top-right
        p.drawLine(pt(0.45, 0.25), pt(0.92, 0.25))
        # Start dot (filled circle at left)
        p.setPen(no_pen)
        p.setBrush(solid_br)
        p.drawEllipse(pt(0.05, 0.75), dr, dr)
        # End dot (filled circle at right)
        p.drawEllipse(pt(0.92, 0.25), dr, dr)
        # Small waypoint at corner
        small_r = dr * 0.65
        p.drawEllipse(pt(0.45, 0.75), small_r, small_r)
        p.drawEllipse(pt(0.45, 0.25), small_r, small_r)

    p.end()
    return pm


def _mk_icon(name: str, sz: int = 28) -> QIcon:
    """Return a QIcon with dark pixmap for normal state, white for checked state."""
    icon = QIcon()
    icon.addPixmap(_mk_pm(name, sz, QColor("#2c2c2c")),
                   QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_mk_pm(name, sz, QColor("#ffffff")),
                   QIcon.Mode.Normal, QIcon.State.On)
    return icon


# ── Style popup ───────────────────────────────────────────────────────────────

class _StylePopup(QWidget):
    """Per-tool flyout popup — appears to the left of the clicked tool button."""

    _TOOL_NAMES = {
        'polygon':  'Rita polygon',
        'polyline': 'Rita polylinje',
        'text':     'Lägg ut nodnamn',
        'comment':  'Lägg till kommentar',
        'smart':    'Smart polylinje',
    }

    def __init__(self, ribbon, parent=None):
        super().__init__(parent,
                         Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(
            "QWidget{background:#fff;border-radius:4px;}"
            "QLabel{font-size:10px;color:#444;border:none;}")
        self._ribbon = ribbon

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)

        # Title
        self._title_lbl = QLabel()
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self._title_lbl.setFont(f)
        outer.addWidget(self._title_lbl)

        sep = QLabel(); sep.setFixedHeight(1)
        sep.setStyleSheet("background:#ddd;border:none;")
        outer.addWidget(sep)

        # Colour swatches (always shown)
        color_widget = QWidget()
        crow = QHBoxLayout(color_widget)
        crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(3)
        crow.addWidget(QLabel("Färg:"))
        self._cbts = []
        for hc in _MARKUP_COLORS:
            cb = QPushButton(); cb.setFixedSize(22, 22)
            cb.setStyleSheet(f"background:{hc};border:2px solid transparent;"
                             f"border-radius:3px;")
            cb.clicked.connect(lambda _, c=hc: self._pick(c))
            crow.addWidget(cb); self._cbts.append((hc, cb))
        pal = QPushButton("···"); pal.setFixedSize(28, 22)
        pal.setStyleSheet("font-size:10px;border:1px solid #ccc;border-radius:3px;")
        pal.clicked.connect(self._open_palette)
        crow.addWidget(pal); crow.addStretch()
        outer.addWidget(color_widget)

        self._bar = QLabel(); self._bar.setFixedHeight(5)
        self._bar.setStyleSheet("border:none;")
        outer.addWidget(self._bar)

        sep2 = QLabel(); sep2.setFixedHeight(1)
        sep2.setStyleSheet("background:#eee;border:none;")
        outer.addWidget(sep2)

        # Opacity row (polygon, polyline, comment)
        self._opacity_row = QWidget()
        orow = QHBoxLayout(self._opacity_row)
        orow.setContentsMargins(0, 0, 0, 0)
        orow.addWidget(QLabel("Opacitet:"))
        self._op_sl = QSlider(Qt.Orientation.Horizontal)
        self._op_sl.setRange(10, 90)
        orow.addWidget(self._op_sl)
        self._op_lbl = QLabel(); self._op_lbl.setFixedWidth(36)
        orow.addWidget(self._op_lbl)
        self._op_sl.valueChanged.connect(
            lambda v: (ribbon._apply_opacity(v), self._op_lbl.setText(f"{v}%")))
        outer.addWidget(self._opacity_row)

        # Line width row (polygon, polyline)
        self._width_row = QWidget()
        wrow = QHBoxLayout(self._width_row)
        wrow.setContentsMargins(0, 0, 0, 0); wrow.setSpacing(5)
        wrow.addWidget(QLabel("Tjocklek:"))
        self._w_sp = QSpinBox(); self._w_sp.setRange(1, 99)
        self._w_sp.setMaximumWidth(58)
        self._w_sp.valueChanged.connect(ribbon._apply_width)
        wrow.addWidget(self._w_sp); wrow.addStretch()
        outer.addWidget(self._width_row)

        # Font size row (text, comment)
        self._font_row = QWidget()
        frow = QHBoxLayout(self._font_row)
        frow.setContentsMargins(0, 0, 0, 0); frow.setSpacing(5)
        frow.addWidget(QLabel("Textstorlek:"))
        self._f_sp = QSpinBox(); self._f_sp.setRange(6, 99)
        self._f_sp.setMaximumWidth(58)
        self._f_sp.valueChanged.connect(ribbon._apply_font)
        frow.addWidget(self._f_sp); frow.addStretch()
        outer.addWidget(self._font_row)

        # Snap row (polygon, polyline)
        self._snap_row = QWidget()
        srow = QHBoxLayout(self._snap_row)
        srow.setContentsMargins(0, 0, 0, 0)
        self._snap_cb = QCheckBox("Snap till befintliga punkter")
        self._snap_cb.setChecked(True)
        self._snap_cb.toggled.connect(ribbon._apply_snap)
        srow.addWidget(self._snap_cb); srow.addStretch()
        outer.addWidget(self._snap_row)

        self.setMinimumWidth(300)

    def _configure_for(self, tool):
        self._title_lbl.setText(self._TOOL_NAMES.get(tool, tool))
        self._opacity_row.setVisible(tool in ('polygon', 'polyline', 'comment', 'smart'))
        self._width_row.setVisible(tool in ('polygon', 'polyline', 'smart'))
        self._font_row.setVisible(tool in ('text', 'comment'))
        self._snap_row.setVisible(tool in ('polygon', 'polyline'))

    def show_for(self, tool, btn):
        self._configure_for(tool)
        self._sync()
        self.adjustSize()
        # Position to the left of the tool button
        gp = btn.mapToGlobal(btn.rect().topLeft())
        self.move(gp.x() - self.width() - 4, gp.y())
        self.show()

    def _sync(self):
        r = self._ribbon
        self._bar.setStyleSheet(f"background:{r._color};border-radius:2px;border:none;")
        self._op_sl.blockSignals(True); self._op_sl.setValue(int(r._opacity * 100))
        self._op_sl.blockSignals(False)
        self._op_lbl.setText(f"{int(r._opacity * 100)}%")
        self._w_sp.blockSignals(True); self._w_sp.setValue(r._width)
        self._w_sp.blockSignals(False)
        self._f_sp.blockSignals(True); self._f_sp.setValue(r._font_size)
        self._f_sp.blockSignals(False)
        self._snap_cb.blockSignals(True); self._snap_cb.setChecked(r._snap)
        self._snap_cb.blockSignals(False)
        for hc, cb in self._cbts:
            cb.setStyleSheet(
                f"background:{hc};border:2px solid "
                f"{'#333' if hc == r._color else 'transparent'};border-radius:3px;")

    def _pick(self, hex_c):
        self._ribbon._apply_color(hex_c)
        self._bar.setStyleSheet(f"background:{hex_c};border-radius:2px;border:none;")
        for hc, cb in self._cbts:
            cb.setStyleSheet(
                f"background:{hc};border:2px solid "
                f"{'#333' if hc == hex_c else 'transparent'};border-radius:3px;")

    def _open_palette(self):
        self.hide()
        c = QColorDialog.getColor(QColor(self._ribbon._color), None, "Välj färg")
        if c.isValid():
            self._ribbon._apply_color(c.name())

    def showEvent(self, event):
        self._sync()
        super().showEvent(event)


class NodeMarkupPanel(QWidget):
    """Narrow vertical ribbon for node markup tool selection."""
    closed          = pyqtSignal()
    tool_changed    = pyqtSignal(str)
    all_vis_toggled = pyqtSignal(bool)
    style_changed   = pyqtSignal(str, float, int)   # color, opacity, line_width
    snap_changed    = pyqtSignal(bool)

    _TOOLS = [
        ('select',   'select',   'Välj/flytta'),
        ('polygon',  'polygon',  'Rita polygon'),
        ('polyline', 'polyline', 'Rita polylinje'),
        ('smart',    'smart',    'Smart polylinje'),
        ('text',     'text',     'Lägg ut nodnamn'),
        ('comment',  'comment',  'Lägg till kommentar'),
    ]

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db            = db
        self.node_id       = None
        self._color        = _MARKUP_COLORS[5]
        self._opacity      = 0.45
        self._width        = 12
        self._font_size    = 24
        self._snap         = True
        self._current_tool = 'select'
        self._popup        = None

        SZ = 48
        ISZ = 28   # icon size within button
        self.setFixedWidth(58)
        self.setStyleSheet("background:#F0F2F5;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 6, 5, 6)
        outer.setSpacing(3)

        _btn_ss = (
            "QPushButton{border:1px solid #D0D4DA;border-radius:5px;"
            "background:#FFFFFF;padding:0px;}"
            "QPushButton:checked{background:#1565C0;border-color:#1565C0;}"
            "QPushButton:hover:!checked{background:#E8EEF8;border-color:#A0AABB;}")

        # ── Close button ──────────────────────────────────────────────────────
        close_btn = QPushButton()
        close_btn.setFixedSize(SZ, SZ)
        close_btn.setToolTip("Avsluta redigering")
        close_icon = QIcon()
        close_icon.addPixmap(_mk_pm('close', ISZ, QColor("#ffffff")))
        close_btn.setIcon(close_icon)
        close_btn.setIconSize(QSize(ISZ, ISZ))
        close_btn.setStyleSheet(
            "QPushButton{background:#546E7A;border:none;border-radius:5px;padding:0px;}"
            "QPushButton:hover{background:#37474F;}")
        close_btn.clicked.connect(self.closed.emit)
        outer.addWidget(close_btn)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("background:#C8CDD5;max-height:1px;border:none;")
        outer.addWidget(sep1)

        # ── Tool buttons — each click selects tool AND opens per-tool popup ───
        self._tool_btns = {}
        for tool, icon_name, tip in self._TOOLS:
            btn = QPushButton()
            btn.setFixedSize(SZ, SZ)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setIcon(_mk_icon(icon_name, ISZ))
            btn.setIconSize(QSize(ISZ, ISZ))
            btn.setStyleSheet(_btn_ss)
            btn.clicked.connect(lambda _, t=tool, b=btn: self._on_tool(t, b))
            outer.addWidget(btn)
            self._tool_btns[tool] = btn

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background:#C8CDD5;max-height:1px;border:none;")
        outer.addWidget(sep2)

        # ── Color strip ───────────────────────────────────────────────────────
        self._color_strip = QLabel()
        self._color_strip.setFixedHeight(7)
        self._color_strip.setStyleSheet(
            f"background:{self._color};border-radius:3px;border:none;")
        outer.addWidget(self._color_strip)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet("background:#C8CDD5;max-height:1px;border:none;")
        outer.addWidget(sep3)

        # ── Visibility toggle ─────────────────────────────────────────────────
        self._all_vis_btn = QPushButton()
        self._all_vis_btn.setFixedSize(SZ, SZ)
        self._all_vis_btn.setCheckable(True)
        self._all_vis_btn.setChecked(True)
        self._all_vis_btn.setToolTip("Dölj/visa alla markeringar")
        eye_icon = QIcon()
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.Off)
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.On)
        self._all_vis_btn.setIcon(eye_icon)
        self._all_vis_btn.setIconSize(QSize(ISZ, ISZ))
        self._all_vis_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:5px;padding:0px;"
            "background:#27AE60;}"
            "QPushButton:!checked{background:#E74C3C;}")
        self._all_vis_btn.clicked.connect(self._on_all_vis)
        outer.addWidget(self._all_vis_btn)

        outer.addStretch()
        self._on_tool('select')

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, node_id):
        self.node_id = node_id
        self._all_vis_btn.setChecked(True)
        self._on_tool('select')

    def refresh(self):
        pass

    def on_markup_saved(self, mu_id):
        pass

    def select_markup(self, mu_id):
        pass

    def get_current_style(self):
        return self._color, self._opacity, self._width, self._font_size

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_tool(self, tool, btn=None):
        self._current_tool = tool
        for t, b in self._tool_btns.items():
            b.setChecked(t == tool)
        self.tool_changed.emit(tool)
        # Open per-tool popup for all drawing tools
        if tool != 'select' and btn is not None:
            self._show_tool_popup(tool, btn)

    def _show_tool_popup(self, tool, btn):
        if self._popup is None:
            self._popup = _StylePopup(self)
        self._popup.show_for(tool, btn)

    def _on_all_vis(self, checked):
        if self.node_id is None:
            return
        self.db.set_all_node_markups_visible(self.node_id, checked)
        self.all_vis_toggled.emit(checked)

    def _apply_color(self, hex_c):
        self._color = hex_c
        self._color_strip.setStyleSheet(f"background:{hex_c};border-radius:3px;")
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_opacity(self, val):
        self._opacity = val / 100.0
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_width(self, val):
        self._width = val
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_font(self, val):
        self._font_size = val

    def _apply_snap(self, enabled):
        self._snap = enabled
        self.snap_changed.emit(enabled)


class _MarkupStyleDialog(QDialog):
    def __init__(self, mu_type, color, opacity, line_width, font_size, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra stil")
        self.setFixedWidth(310)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # Color row
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Färg:"))
        self._color = color
        self._color_btns = []
        for hc in _MARKUP_COLORS:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            sel = hc.lower() == color.lower()
            btn.setStyleSheet(
                f"background:{hc};border:2px solid {'#222' if sel else 'transparent'};"
                f"border-radius:3px;")
            btn.clicked.connect(lambda _, c=hc: self._pick(c))
            color_row.addWidget(btn)
            self._color_btns.append((hc, btn))
        color_row.addStretch()
        outer.addLayout(color_row)

        # Opacity
        self._opacity_row = QWidget()
        op_lay = QHBoxLayout(self._opacity_row)
        op_lay.setContentsMargins(0, 0, 0, 0)
        op_lay.addWidget(QLabel("Opacitet:"))
        self._opacity_sl = QSlider(Qt.Orientation.Horizontal)
        self._opacity_sl.setRange(10, 100)
        self._opacity_sl.setValue(int(opacity * 100))
        op_lay.addWidget(self._opacity_sl)
        self._opacity_row.setVisible(mu_type in ('polygon', 'polyline', 'comment'))
        outer.addWidget(self._opacity_row)

        # Line width
        self._width_row = QWidget()
        w_lay = QHBoxLayout(self._width_row)
        w_lay.setContentsMargins(0, 0, 0, 0)
        w_lay.addWidget(QLabel("Tjocklek:"))
        self._width_sp = QSpinBox()
        self._width_sp.setRange(1, 20)
        self._width_sp.setValue(int(line_width))
        w_lay.addWidget(self._width_sp)
        w_lay.addStretch()
        self._width_row.setVisible(mu_type in ('polygon', 'polyline'))
        outer.addWidget(self._width_row)

        # Font size
        self._font_row = QWidget()
        f_lay = QHBoxLayout(self._font_row)
        f_lay.setContentsMargins(0, 0, 0, 0)
        f_lay.addWidget(QLabel("Teckenstorlek:"))
        self._font_sp = QSpinBox()
        self._font_sp.setRange(6, 72)
        self._font_sp.setValue(int(font_size))
        f_lay.addWidget(self._font_sp)
        f_lay.addStretch()
        self._font_row.setVisible(mu_type in ('text', 'comment'))
        outer.addWidget(self._font_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _pick(self, hc):
        self._color = hc
        for c, btn in self._color_btns:
            btn.setStyleSheet(
                f"background:{c};border:2px solid {'#222' if c.lower()==hc.lower() else 'transparent'};"
                f"border-radius:3px;")

    def get_style(self):
        return (self._color,
                self._opacity_sl.value() / 100.0,
                self._width_sp.value(),
                self._font_sp.value())


# ══════════════════════════════════════════════════════════════════════════════
# MARKUP TABLE PANEL  (bottom panel, shown during markup edit mode)
# ══════════════════════════════════════════════════════════════════════════════

class MarkupTablePanel(QWidget):
    """Table of markups for the active node — lives in bottom splitter alongside scenario panel."""
    item_deleted     = pyqtSignal(int)        # mu_id
    item_vis_toggled = pyqtSignal(int, bool)  # mu_id, visible
    item_selected    = pyqtSignal(int)        # mu_id
    item_style_changed = pyqtSignal(int)      # mu_id
    item_duplicated  = pyqtSignal(int)        # mu_id

    _TYPE_ICON = {'polygon': '◻', 'polyline': '〰', 'text': '𝐀', 'comment': '💬'}
    _COLS      = ['Typ', 'Etikett', 'Färg', 'Opacitet', 'Tjocklek', 'Font', '👁']

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db      = db
        self.node_id = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        hdr = QHBoxLayout()
        title = QLabel("Nodmarkeringar")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()
        lay.addLayout(hdr)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_ctx_menu)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.setStyleSheet(
            "QTableWidget{border:1px solid #ddd;font-size:10px;}"
            "QTableWidget::item:selected{background:#E3F2FD;color:#1565C0;}")

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._table)

    def load(self, node_id):
        self.node_id = node_id
        self.refresh()

    def refresh(self):
        self._table.setRowCount(0)
        if self.node_id is None:
            return
        for mu in self.db.node_markups_for_node(self.node_id):
            m = dict(mu)
            row = self._table.rowCount()
            self._table.insertRow(row)
            mu_id   = m['id']
            typ     = m.get('type', 'polygon')
            label   = m.get('label', '') or ''
            color   = m.get('color', '#1565C0')
            opacity = m.get('opacity', 0.45)
            width   = m.get('line_width', 12)
            font_sz = m.get('font_size', 12)
            visible = bool(m.get('visible', 1))

            icon_item = QTableWidgetItem(self._TYPE_ICON.get(typ, '◻'))
            icon_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, icon_item)

            lbl_item = QTableWidgetItem(label)
            lbl_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            self._table.setItem(row, 1, lbl_item)

            color_item = QTableWidgetItem(color)
            color_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            color_item.setBackground(QColor(color))
            color_item.setForeground(QColor(color))
            color_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, color_item)

            op_item = QTableWidgetItem(f"{int(opacity * 100)}%")
            op_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            op_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, op_item)

            w_item = QTableWidgetItem(str(width))
            w_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            w_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 4, w_item)

            f_item = QTableWidgetItem(str(font_sz))
            f_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 5, f_item)

            vis_item = QTableWidgetItem('👁' if visible else '○')
            vis_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            vis_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 6, vis_item)

    def select_markup(self, mu_id):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == mu_id:
                self._table.selectRow(row)
                break

    def clear(self):
        self.node_id = None
        self._table.setRowCount(0)

    def _on_cell_clicked(self, row, col):
        item = self._table.item(row, 0)
        if item is None:
            return
        mu_id = item.data(Qt.ItemDataRole.UserRole)
        if col == 6:
            self._toggle_visibility(row, mu_id)
        else:
            self.item_selected.emit(mu_id)

    def _toggle_visibility(self, row, mu_id):
        mu = self.db.get_node_markup(mu_id)
        if not mu:
            return
        new_vis = not bool(dict(mu).get('visible', 1))
        self.db.update_node_markup(mu_id, visible=new_vis)
        vis_item = self._table.item(row, 6)
        if vis_item:
            vis_item.setText('👁' if new_vis else '○')
        self.item_vis_toggled.emit(mu_id, new_vis)

    def _on_ctx_menu(self, pos):
        seen, rows = set(), []
        for idx in self._table.selectedIndexes():
            r = idx.row()
            if r not in seen:
                seen.add(r)
                item = self._table.item(r, 0)
                if item:
                    rows.append(item)
        if not rows:
            return
        menu = QMenu(self)
        n = len(rows)
        lbl = f"🗑 Ta bort ({n} valda)" if n > 1 else "🗑 Ta bort"
        act_del = menu.addAction(lbl)
        act_style = None
        act_dup   = None
        if n == 1:
            act_style = menu.addAction("✏ Ändra stil...")
            act_dup   = menu.addAction("📋 Duplicera")
        result = menu.exec(self._table.viewport().mapToGlobal(pos))
        if result == act_del:
            for item in rows:
                mu_id = item.data(Qt.ItemDataRole.UserRole)
                self.db.delete_node_markup(mu_id)
                self.item_deleted.emit(mu_id)
            self.refresh()
        elif act_style is not None and result == act_style:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            mu = self.db.get_node_markup(mu_id)
            if mu:
                mu = dict(mu)
                dlg = _MarkupStyleDialog(
                    mu.get('type', 'polygon'),
                    mu.get('color', '#E53935'),
                    float(mu.get('opacity', 0.7)),
                    int(mu.get('line_width', 2)),
                    int(mu.get('font_size', 12)),
                    self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    c, op, lw, fs = dlg.get_style()
                    self.db.update_node_markup(mu_id, color=c, opacity=op,
                                               line_width=lw, font_size=fs)
                    self.item_style_changed.emit(mu_id)
                    self.refresh()
        elif act_dup is not None and result == act_dup:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            self.item_duplicated.emit(mu_id)


# ══════════════════════════════════════════════════════════════════════════════
# TREE PANEL
# ══════════════════════════════════════════════════════════════════════════════

NODE_T = 1
CAUSE_T = 2
CONS_T = 3
SG_T = 4
DEV_T = 5

_DEVIATION_TYPES = [
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


class _PickDeviationDialog(QDialog):
    """Small dialog to pick/type a deviation description when adding a new deviation."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lägg till avvikelse")
        self.description = ""
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj eller skriv en avvikelse:"))
        self.combo = QComboBox()
        self.combo.addItems(_DEVIATION_TYPES)
        self.combo.setEditable(True)
        self.combo.setCurrentText(_DEVIATION_TYPES[0])
        layout.addWidget(self.combo)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.resize(300, 100)

    def _accept(self):
        self.description = self.combo.currentText().strip() or "Övrigt"
        self.accept()


class HAZOPTreeWidget(QTreeWidget):
    """QTreeWidget with Ctrl+drag=copy, plain drag=move.

    Emits item_drop_requested(drag_type, drag_id, drop_type, drop_id, is_copy)
    instead of rearranging items internally.
    """
    item_drop_requested = pyqtSignal(int, int, int, int, bool)

    _VALID_DROPS = {
        CAUSE_T: (NODE_T, CAUSE_T),
        CONS_T:  (CAUSE_T, CONS_T),
        SG_T:    (CONS_T, SG_T),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._drag_item = None

    def _item_type(self, item):
        return item.data(0, Qt.ItemDataRole.UserRole + 1) if item else None

    def _item_id(self, item):
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _valid(self, drag_t, drop_t):
        return drop_t in self._VALID_DROPS.get(drag_t, ())

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        if self._item_type(item) == NODE_T:
            return  # Nodes are not draggable
        self._drag_item = item
        super().startDrag(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event):
        if self._drag_item is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        if target is None or self._drag_item is None or target is self._drag_item:
            event.ignore()
            return
        drag_t = self._item_type(self._drag_item)
        drop_t = self._item_type(target)
        if self._valid(drag_t, drop_t):
            is_copy = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            event.setDropAction(
                Qt.DropAction.CopyAction if is_copy else Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        source = self._drag_item
        self._drag_item = None

        if source is None or target is None or source is target:
            event.ignore()
            return

        drag_t  = self._item_type(source)
        drag_id = self._item_id(source)
        drop_t  = self._item_type(target)
        drop_id = self._item_id(target)

        if not self._valid(drag_t, drop_t):
            event.ignore()
            return

        is_copy = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # Suppress Qt's built-in item move — we manage the tree ourselves
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()

        self.item_drop_requested.emit(drag_t, drag_id, drop_t, drop_id, is_copy)


class TreePanel(QWidget):
    item_selected               = pyqtSignal(int, int)
    add_causes_on_pid_requested       = pyqtSignal(int)   # deviation_id
    add_consequences_on_pid_requested = pyqtSignal(int)   # cause_id
    add_safeguards_on_pid_requested   = pyqtSignal(int)   # consequence_id
    edit_node_markup_requested        = pyqtSignal(int)        # node_id
    node_markup_vis_requested         = pyqtSignal(int, bool)  # node_id, visible
    node_jump_to_markup               = pyqtSignal(int)         # node_id
    structure_changed           = pyqtSignal()
    visibility_changed          = pyqtSignal(str, bool)   # marker_type, visible
    exit_pid_mode_requested     = pyqtSignal()    # exit any active P&ID placement mode

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._clipboard = None  # {'type': T, 'id': id}

        lbl = QLabel("HAZOP-träd")
        f = QFont(); f.setBold(True)
        lbl.setFont(f)
        layout.addWidget(lbl)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._on_select)
        self.tree.itemDoubleClicked.connect(self._on_item_double_click)
        layout.addWidget(self.tree)

        btn_row = QHBoxLayout()
        self.btn_node  = QPushButton("+ Nod")
        self.btn_cause = QPushButton("+ Cause")
        self.btn_cons  = QPushButton("+ Cons.")
        self.btn_del   = QPushButton("🗑")
        self.btn_del.setToolTip("Ta bort markerat objekt")
        self.btn_del.setStyleSheet("color:#c0392b; font-weight:bold;")
        for b in [self.btn_node, self.btn_cause, self.btn_cons, self.btn_del]:
            b.setFixedHeight(26)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        self.btn_node.clicked.connect(self.add_node)
        self.btn_cause.clicked.connect(self.add_cause)
        self.btn_cons.clicked.connect(self.add_consequence)
        self.btn_del.clicked.connect(self.delete_selected)

        # ── Visibility toggle buttons for P&ID markers ────────────────────────
        vis_lbl = QLabel("Visa på P&ID:")
        vis_lbl.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(vis_lbl)

        vis_row = QHBoxLayout()
        vis_row.setSpacing(4)

        _VIS_BTNS = [
            ('cause',        '⚙️ Orsaker',       '#e74c3c', '#fde8e8'),
            ('consequence',  '⚠️ Konsekvenser',  '#e67e22', '#fef0e0'),
            ('safeguard',    '🛡️ Safeguards',    '#27ae60', '#e8f8e8'),
        ]
        self._vis_btns = {}
        for type_key, label, color_on, color_off in _VIS_BTNS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                f"QPushButton{{background:{color_on}; color:white; border:none;"
                f" border-radius:3px; font-size:10px; font-weight:bold; padding:0 4px;}}"
                f"QPushButton:!checked{{background:{color_off}; color:#aaa;}}")
            btn.toggled.connect(
                lambda checked, t=type_key: self.visibility_changed.emit(t, checked))
            vis_row.addWidget(btn)
            self._vis_btns[type_key] = btn

        layout.addLayout(vis_row)

    def refresh(self, select_type=None, select_id=None):
        expanded = set()
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.isExpanded():
                expanded.add((item.data(0, Qt.ItemDataRole.UserRole + 1),
                              item.data(0, Qt.ItemDataRole.UserRole)))
            it += 1

        self.tree.blockSignals(True)
        self.tree.clear()
        target = None
        bold_font = QFont(); bold_font.setBold(True)

        marked_causes = self.db.marked_cause_ids()
        marked_consequences = self.db.marked_consequence_ids()
        marked_safeguards = self.db.marked_safeguard_ids()

        for ni, node in enumerate(self.db.nodes(), 1):
            node_on_pid = bool(node['markup_points'])
            pid_pin = " 📍" if node_on_pid else ""
            nitem = QTreeWidgetItem([f"🏭  {ni}. {node['name']}{pid_pin}"])
            nitem.setData(0, Qt.ItemDataRole.UserRole, node['id'])
            nitem.setData(0, Qt.ItemDataRole.UserRole + 1, NODE_T)
            nitem.setFont(0, bold_font)
            nitem.setToolTip(0, node['pid_ref'] or '')
            self.tree.addTopLevelItem(nitem)
            if (NODE_T, node['id']) in expanded: nitem.setExpanded(True)
            if select_type == NODE_T and select_id == node['id']: target = nitem

            for di, dev in enumerate(self.db.deviations(node['id']), 1):
                ditem = QTreeWidgetItem([f"  ⬡  {di}. {dev['description'][:55]}"])
                ditem.setData(0, Qt.ItemDataRole.UserRole, dev['id'])
                ditem.setData(0, Qt.ItemDataRole.UserRole + 1, DEV_T)
                dev_font = QFont(); dev_font.setItalic(True)
                ditem.setFont(0, dev_font)
                nitem.addChild(ditem)
                if (DEV_T, dev['id']) in expanded: ditem.setExpanded(True)
                if select_type == DEV_T and select_id == dev['id']: target = ditem

                for ci, cause in enumerate(self.db.causes_for_deviation(dev['id']), 1):
                    placed_c = cause['id'] in marked_causes
                    citem = QTreeWidgetItem([f"    ⚙  {ci}. {cause['description'][:50]}"])
                    citem.setIcon(0, _make_pin_icon(placed_c))
                    citem.setData(0, Qt.ItemDataRole.UserRole, cause['id'])
                    citem.setData(0, Qt.ItemDataRole.UserRole + 1, CAUSE_T)
                    ditem.addChild(citem)
                    if (CAUSE_T, cause['id']) in expanded: citem.setExpanded(True)
                    if select_type == CAUSE_T and select_id == cause['id']: target = citem

                    for ki, cons in enumerate(self.db.consequences(cause['id']), 1):
                        level, _, _ = risk_info(cause['likelihood'], cons['severity'])
                        risk_icon = _RISK_ICON.get(level, '⚪')
                        placed_k = cons['id'] in marked_consequences
                        kitem = QTreeWidgetItem([f"      {risk_icon}  {ki}. {cons['description'][:40]}"])
                        kitem.setIcon(0, _make_pin_icon(placed_k))
                        kitem.setData(0, Qt.ItemDataRole.UserRole, cons['id'])
                        kitem.setData(0, Qt.ItemDataRole.UserRole + 1, CONS_T)
                        citem.addChild(kitem)
                        if (CONS_T, cons['id']) in expanded: kitem.setExpanded(True)
                        if select_type == CONS_T and select_id == cons['id']: target = kitem

                        for si, sg in enumerate(self.db.safeguards(cons['id']), 1):
                            rrf = (sg['rrf'] or 1) if sg['rrf'] is not None else 1
                            rrf_str = f"RRF{rrf}" if rrf > 1 else "—"
                            try:
                                linked = bool(sg['source_id'])
                            except (IndexError, KeyError):
                                linked = False
                            sg_icon = "🔗🛡" if linked else "🛡"
                            placed_s = sg['id'] in marked_safeguards
                            sgitem = QTreeWidgetItem([f"         {sg_icon}  {si}. {sg['description'][:35]}  [{rrf_str}]"])
                            sgitem.setIcon(0, _make_pin_icon(placed_s))
                            sgitem.setData(0, Qt.ItemDataRole.UserRole, sg['id'])
                            sgitem.setData(0, Qt.ItemDataRole.UserRole + 1, SG_T)
                            kitem.addChild(sgitem)
                            if select_type == SG_T and select_id == sg['id']: target = sgitem

        self.tree.blockSignals(False)
        if target:
            self.tree.setCurrentItem(target)
            self.tree.scrollToItem(target)

    def _current(self):
        item = self.tree.currentItem()
        if item is None:
            return None, None
        return (item.data(0, Qt.ItemDataRole.UserRole + 1),
                item.data(0, Qt.ItemDataRole.UserRole))

    def _resolve_node_id(self, type_, id_):
        if type_ == NODE_T: return id_
        if type_ == DEV_T:
            r = self.db.get_deviation(id_); return r['node_id'] if r else None
        if type_ == CAUSE_T:
            r = self.db.get_cause(id_); return r['node_id'] if r else None
        if type_ == CONS_T:
            r = self.db.get_consequence(id_)
            if r:
                c = self.db.get_cause(r['cause_id']); return c['node_id'] if c else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id'])
                if c:
                    ca = self.db.get_cause(c['cause_id']); return ca['node_id'] if ca else None
        return None

    def _resolve_deviation_id(self, type_, id_):
        if type_ == DEV_T: return id_
        if type_ == CAUSE_T:
            r = self.db.get_cause(id_); return r['deviation_id'] if r else None
        if type_ == CONS_T:
            r = self.db.get_consequence(id_)
            if r:
                c = self.db.get_cause(r['cause_id'])
                return c['deviation_id'] if c else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id'])
                if c:
                    ca = self.db.get_cause(c['cause_id'])
                    return ca['deviation_id'] if ca else None
        return None

    def _resolve_cause_id(self, type_, id_):
        if type_ == CAUSE_T: return id_
        if type_ == CONS_T:
            r = self.db.get_consequence(id_); return r['cause_id'] if r else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id']); return c['cause_id'] if c else None
        return None

    def _resolve_consequence_id(self, type_, id_):
        if type_ == CONS_T: return id_
        if type_ == SG_T:
            r = self.db.get_safeguard(id_); return r['consequence_id'] if r else None
        return None

    def add_node(self):
        new_id = self.db.add_node()
        self.refresh(NODE_T, new_id)
        self.structure_changed.emit()

    def add_deviation(self):
        type_, id_ = self._current()
        node_id = self._resolve_node_id(type_, id_) if type_ else None
        if node_id is None:
            QMessageBox.information(self, "Välj nod", "Välj en nod i trädet."); return
        dlg = _PickDeviationDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_id = self.db.add_deviation(node_id, dlg.description)
        self.refresh(DEV_T, new_id)
        self.structure_changed.emit()

    def add_cause(self):
        type_, id_ = self._current()
        dev_id = self._resolve_deviation_id(type_, id_) if type_ else None
        if dev_id is None:
            QMessageBox.information(self, "Välj avvikelse", "Välj en avvikelse i trädet."); return
        new_id = self.db.add_cause(dev_id)
        self.refresh(CAUSE_T, new_id)
        self.structure_changed.emit()

    def add_consequence(self):
        type_, id_ = self._current()
        cause_id = self._resolve_cause_id(type_, id_) if type_ else None
        if cause_id is None:
            QMessageBox.information(self, "Välj cause", "Välj en cause i trädet."); return
        new_id = self.db.add_consequence(cause_id)
        self.exit_pid_mode_requested.emit()
        self.refresh(CONS_T, new_id)
        self.structure_changed.emit()

    def add_safeguard(self):
        type_, id_ = self._current()
        cons_id = self._resolve_consequence_id(type_, id_) if type_ else None
        if cons_id is None:
            QMessageBox.information(self, "Välj konsekvens", "Välj en konsekvens i trädet."); return
        new_id = self.db.add_safeguard(cons_id)
        self.exit_pid_mode_requested.emit()
        self.refresh(SG_T, new_id)
        self.structure_changed.emit()

    def delete_selected(self):
        type_, id_ = self._current()
        if type_ is None: return
        names = {NODE_T: 'noden', DEV_T: 'avvikelsen', CAUSE_T: 'orsaken',
                 CONS_T: 'konsekvensen', SG_T: 'safeguarden'}
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort {names.get(type_, 'objektet')} och allt under den?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        deletors = {NODE_T: self.db.delete_node, DEV_T: self.db.delete_deviation,
                    CAUSE_T: self.db.delete_cause, CONS_T: self.db.delete_consequence,
                    SG_T: self.db.delete_safeguard}
        if type_ in deletors:
            deletors[type_](id_)
        self.refresh()
        self.structure_changed.emit()

    def _on_select(self, current, _previous):
        if current is None: return
        type_ = current.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = current.data(0, Qt.ItemDataRole.UserRole)
        self.item_selected.emit(type_, id_)

    def _on_item_double_click(self, item, col):
        if item is None:
            return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        if type_ == NODE_T and self.db.has_node_markups(id_):
            self.node_jump_to_markup.emit(id_)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None: return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        menu  = QMenu(self)

        if type_ == NODE_T:
            menu.addAction("+ Lägg till avvikelse", self.add_deviation)
            menu.addAction("✏️ Editera nodmarkup",
                           lambda i=id_: self.edit_node_markup_requested.emit(i))
            if self.db.has_node_markups(id_):
                is_vis = self.db.has_visible_node_markups(id_)
                if is_vis:
                    menu.addAction("🙈 Dölj nod på P&ID",
                                   lambda i=id_: self.node_markup_vis_requested.emit(i, False))
                else:
                    menu.addAction("👁 Visa nod på P&ID",
                                   lambda i=id_: self.node_markup_vis_requested.emit(i, True))
        elif type_ == DEV_T:
            menu.addAction("+ Lägg till orsak", self.add_cause)
            menu.addAction("📍 Lägg till orsaker på P&ID",
                           lambda i=id_: self.add_causes_on_pid_requested.emit(i))
        elif type_ == CAUSE_T:
            menu.addAction("+ Lägg till konsekvens", self.add_consequence)
            menu.addAction("📍 Lägg till konsekvens på P&ID",
                           lambda i=id_: self.add_consequences_on_pid_requested.emit(i))
        elif type_ == CONS_T:
            menu.addAction("+ Lägg till safeguard", self.add_safeguard)
            menu.addAction("📍 Lägg till safeguard på P&ID",
                           lambda i=id_: self.add_safeguards_on_pid_requested.emit(i))

        # Copy
        copy_labels = {CAUSE_T: "📋 Kopiera orsak",
                       CONS_T:  "📋 Kopiera konsekvens",
                       SG_T:    "📋 Kopiera safeguard"}
        if type_ in copy_labels:
            menu.addAction(copy_labels[type_],
                           lambda t=type_, i=id_: self._copy_item(t, i))

        # Paste (only if clipboard is compatible with current target)
        if self._clipboard:
            ct = self._clipboard['type']
            can_paste = (
                (ct == CAUSE_T and type_ in (NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T)) or
                (ct == CONS_T  and type_ in (CAUSE_T, CONS_T, SG_T)) or
                (ct == SG_T    and type_ in (CONS_T, SG_T))
            )
            if can_paste:
                menu.addAction("📋 Klistra in här", self._paste_item)

        menu.addSeparator()
        menu.addAction("Ta bort", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy_item(self, type_, id_):
        self._clipboard = {'type': type_, 'id': id_}

    def _paste_item(self):
        if not self._clipboard:
            return
        ct    = self._clipboard['type']
        cid   = self._clipboard['id']
        type_, id_ = self._current()

        if ct == CAUSE_T:
            dev_id = self._resolve_deviation_id(type_, id_)
            if not dev_id:
                # Fall back: get or create "Övrigt" deviation on the resolved node
                node_id = self._resolve_node_id(type_, id_)
                if not node_id:
                    return
                dev_id = self.db.get_or_create_deviation(node_id)
            new_id = self.db.copy_cause(cid, dev_id)
            if new_id:
                self.refresh(CAUSE_T, new_id)
                self.structure_changed.emit()

        elif ct == CONS_T:
            cause_id = self._resolve_cause_id(type_, id_)
            if not cause_id:
                return
            new_id = self.db.copy_consequence(cid, cause_id)
            if new_id:
                self.refresh(CONS_T, new_id)
                self.structure_changed.emit()

        elif ct == SG_T:
            # Resolve consequence
            cons_id = None
            if type_ == CONS_T:
                cons_id = id_
            elif type_ == SG_T:
                sg = self.db.get_safeguard(id_)
                if sg:
                    cons_id = sg['consequence_id']
            if not cons_id:
                return
            new_id = self.db.copy_safeguard(cid, cons_id)
            if new_id:
                self.refresh(SG_T, new_id)
                self.structure_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# EDITABLE SCENARIO PANEL  (bottom)
# ══════════════════════════════════════════════════════════════════════════════

class EditableScenarioPanel(QWidget):
    data_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
        self.setMinimumHeight(120)
        self.setMaximumHeight(300)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        hdr = QHBoxLayout()
        self._hdr_lbl = QLabel("HAZOP Scenario")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        self._hdr_lbl.setFont(f)
        hdr.addWidget(self._hdr_lbl)
        hdr.addStretch()
        outer.addLayout(hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(4)
        self._scroll.setWidget(self._content_widget)
        outer.addWidget(self._scroll)

    def load_cause(self, cause_id):
        self.cause_id = cause_id
        self._rebuild()

    def load_consequence(self, cons_id):
        row = self.db.get_consequence(cons_id)
        if row:
            self.cause_id = dict(row)['cause_id']
            self._rebuild()

    def clear(self):
        self.cause_id = None
        self._rebuild()

    def _rebuild(self):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self.cause_id is None:
            return

        cause = self.db.get_cause(self.cause_id)
        if not cause:
            return
        cause_d = dict(cause)
        node = self.db.get_node(cause_d['node_id'])
        node_name = dict(node)['name'] if node else '?'
        self._hdr_lbl.setText(f"HAZOP Scenario — {node_name}")

        # Cause section
        cause_box = QGroupBox("ORSAK")
        cause_lay = QHBoxLayout(cause_box)
        cause_lay.setSpacing(6)

        cause_desc = QLineEdit(cause_d['description'])
        cause_desc.setPlaceholderText("Beskriv orsaken...")
        cid = self.cause_id
        cause_desc.editingFinished.connect(
            lambda w=cause_desc: (self.db.update_cause(cid, w.text().strip() or 'Ny orsak'),
                                  self.data_changed.emit()))
        cause_lay.addWidget(cause_desc, 3)

        like_combo = QComboBox()
        like_combo.addItems(_LIKE_LABELS)
        like_combo.setCurrentIndex(freq_to_idx(cause_d['likelihood'] if cause_d['likelihood'] is not None else 3))
        like_combo.currentIndexChanged.connect(
            lambda idx, c=cid: (self.db.update_cause(c, likelihood=idx_to_freq(idx)),
                                self.data_changed.emit()))
        cause_lay.addWidget(QLabel("F:"))
        cause_lay.addWidget(like_combo, 2)

        add_cons_btn = QPushButton("+ Konsekvens")
        add_cons_btn.setFixedHeight(24)
        add_cons_btn.clicked.connect(self._add_consequence)
        cause_lay.addWidget(add_cons_btn)
        self._content_layout.addWidget(cause_box)

        # Consequence rows — like_val is the actual frequency value (-1..5)
        like_val = cause_d['likelihood'] if cause_d['likelihood'] is not None else 3
        for cons in self.db.consequences(self.cause_id):
            cons_d = dict(cons)
            self._add_consequence_row(cons_d, like_val)

        self._content_layout.addStretch()

    def _add_consequence_row(self, cons_d, like_val):
        row_frame = QFrame()
        row_frame.setFrameShape(QFrame.Shape.StyledPanel)
        row_lay = QHBoxLayout(row_frame)
        row_lay.setContentsMargins(4, 2, 4, 2)
        row_lay.setSpacing(4)

        cons_box = QGroupBox(f"KONSEKVENS")
        cons_form = QHBoxLayout(cons_box)
        cons_form.setSpacing(4)

        cons_desc = QLineEdit(cons_d['description'])
        cons_id = cons_d['id']
        sev_start = cons_d['severity'] or 1

        sev_combo = QComboBox()
        sev_combo.addItems(_SEV_LABELS)
        sev_combo.setCurrentIndex(max(0, sev_start - 1))

        badge = RiskBadge()
        badge.update_risk(like_val, sev_start)

        def _save_cons():
            desc = cons_desc.text().strip() or 'Ny konsekvens'
            sev = sev_combo.currentIndex() + 1
            cat = cons_d.get('category', '')
            self.db.update_consequence(cons_id, desc, sev, cat)
            badge.update_risk(like_val, sev)
            self.data_changed.emit()

        cons_desc.editingFinished.connect(_save_cons)
        sev_combo.currentIndexChanged.connect(lambda _: _save_cons())

        cons_form.addWidget(cons_desc, 3)
        cons_form.addWidget(QLabel("S:"))
        cons_form.addWidget(sev_combo, 2)
        cons_form.addWidget(badge)

        del_cons_btn = QPushButton("✕")
        del_cons_btn.setFixedSize(22, 22)
        del_cons_btn.setStyleSheet("color:#c0392b;")
        del_cons_btn.clicked.connect(lambda _, cid=cons_id: (
            self.db.delete_consequence(cid), self._rebuild(), self.data_changed.emit()))
        cons_form.addWidget(del_cons_btn)
        row_lay.addWidget(cons_box, 2)

        # Safeguards
        sg_container = QWidget()
        sg_vlay = QVBoxLayout(sg_container)
        sg_vlay.setContentsMargins(0, 0, 0, 0)
        sg_vlay.setSpacing(2)

        for sg in self.db.safeguards(cons_id):
            sg_d = dict(sg)
            sg_row = QHBoxLayout()
            sg_desc = QLineEdit(sg_d['description'])
            sg_id = sg_d['id']

            rrf_combo = QComboBox()
            rrf_combo.addItems(_RRF_LABELS)
            rrf_idx = _RRF_VALUES.index(sg_d['rrf']) if sg_d['rrf'] in _RRF_VALUES else 0
            rrf_combo.setCurrentIndex(rrf_idx)

            eff_badge = RiskBadge()
            eff_badge.setFixedSize(120, 22)
            eff_f = effective_frequency(like_val, sg_d['rrf'])
            eff_badge.update_risk(eff_f, sev_start)

            def _save_sg(s=sg_id, dw=sg_desc, rw=rrf_combo, eb=eff_badge):
                desc = dw.text().strip() or 'Ny safeguard'
                rrf = _RRF_VALUES[rw.currentIndex()]
                self.db.update_safeguard(s, desc, rrf)
                eff = effective_frequency(like_val, rrf)
                eb.update_risk(eff, sev_combo.currentIndex() + 1)
                self.data_changed.emit()

            sg_desc.editingFinished.connect(_save_sg)
            rrf_combo.currentIndexChanged.connect(lambda _, f=_save_sg: f())

            del_sg = QPushButton("✕")
            del_sg.setFixedSize(20, 20)
            del_sg.setStyleSheet("color:#c0392b;")
            del_sg.clicked.connect(lambda _, s=sg_id: (
                self.db.delete_safeguard(s), self._rebuild(), self.data_changed.emit()))

            sg_row.addWidget(QLabel("🛡"))
            sg_row.addWidget(sg_desc, 3)
            sg_row.addWidget(QLabel("RRF:"))
            sg_row.addWidget(rrf_combo, 2)
            sg_row.addWidget(eff_badge)
            sg_row.addWidget(del_sg)
            w = QWidget(); w.setLayout(sg_row)
            sg_vlay.addWidget(w)

        add_sg_btn = QPushButton("+ Safeguard")
        add_sg_btn.setFixedHeight(22)
        add_sg_btn.clicked.connect(lambda _, cid=cons_id: (
            self.db.add_safeguard(cid), self._rebuild(), self.data_changed.emit()))
        sg_vlay.addWidget(add_sg_btn)

        row_lay.addWidget(sg_container, 3)
        self._content_layout.addWidget(row_frame)

    def _add_consequence(self):
        if self.cause_id is None:
            return
        self.db.add_consequence(self.cause_id)
        self._rebuild()
        self.data_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO TABLE PANEL  (6-column bottom panel)
# ══════════════════════════════════════════════════════════════════════════════

class RiskMatrixPopup(QDialog):
    """Popup risk matrix matching the configured format in Settings."""

    selection_made = pyqtSignal(int, int)   # freq_value, cons_value

    def __init__(self, current_freq: int, current_cons: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Välj risknivå")
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        cfg       = get_matrix()
        n_cons    = cfg.get('rows', 5)
        n_freq    = cfg.get('cols', 7)
        x_lbls    = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        y_lbls    = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors         = cfg.get('cell_colors', [])
        cell_lbl       = cfg.get('cell_labels', [])
        cell_fg_colors = cfg.get('cell_fg_colors', [])
        freq_on_x = cfg.get('x_axis', 'frequency') == 'frequency'
        x_rev     = cfg.get('x_reversed', False)
        y_rev     = cfg.get('y_reversed', False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        hdr = QLabel("Klicka på en cell för att sätta risknivå")
        hdr.setStyleSheet("font-weight:bold; font-size:11px; padding:2px;")
        outer.addWidget(hdr)

        grid = QGridLayout()
        grid.setSpacing(0)

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons
            col_lbls, row_lbls = x_lbls, y_lbls
            corner_txt = "C \\ F"
        else:
            n_dcols, n_drows = n_cons, n_freq
            col_lbls, row_lbls = y_lbls, x_lbls
            corner_txt = "F \\ C"

        # Corner
        corner = QLabel(corner_txt)
        corner.setStyleSheet("font-size:9px; color:#666;")
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setFixedWidth(50)
        grid.addWidget(corner, 0, 0)

        # Column headers — respect x_rev
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            full   = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            # Short label: take first token (e.g. "F3" from "F3 – Möjlig | 10-100 år")
            short  = full.split()[0] if full.strip() else str(data_c)
            lbl = QLabel(short)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFixedWidth(50)
            lbl.setStyleSheet("font-size:9px; font-weight:bold; padding:1px;")
            lbl.setToolTip(full)
            grid.addWidget(lbl, 0, c + 1)

        # Rows — respect y_rev
        for r in range(n_drows):
            if y_rev:
                disp_r = r
            else:
                disp_r = n_drows - 1 - r

            # Row header
            full_r = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            short_r = full_r.split()[0] if full_r.strip() else str(disp_r)
            rl = QLabel(short_r)
            rl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rl.setStyleSheet("font-size:9px; font-weight:bold; padding-right:4px;")
            rl.setToolTip(full_r)
            rl.setFixedWidth(50)
            grid.addWidget(rl, r + 1, 0)

            for c in range(n_dcols):
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map to (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx, freq_idx = disp_r, data_c
                else:
                    freq_idx, cons_idx = disp_r, data_c

                freq_val = freq_idx - 1   # F=-1..5 (col 0 → F=-1)
                cons_val = cons_idx + 1   # C=1..5

                try:
                    color = colors[cons_idx][freq_idx]
                    lbl   = cell_lbl[cons_idx][freq_idx]
                except (IndexError, KeyError):
                    color, lbl = '#27ae60', 'Låg'
                try:
                    fg = cell_fg_colors[cons_idx][freq_idx] or '#ffffff'
                except (IndexError, KeyError, TypeError):
                    fg = '#ffffff'

                is_current = (freq_val == current_freq and cons_val == current_cons)
                border = '3px solid #000' if is_current else '0px'

                btn = QPushButton(lbl[:4])
                btn.setFixedSize(50, 32)
                btn.setToolTip(f"F={freq_val}  C={cons_val}  →  {lbl}")
                btn.setStyleSheet(
                    f"QPushButton{{background:{color}; color:{fg};"
                    f"font-size:8px; font-weight:bold;"
                    f"border:{border}; border-radius:0px; margin:0px;}}"
                    f"QPushButton:hover{{border:2px solid #000;}}")
                btn.clicked.connect(
                    lambda _, fv=freq_val, cv=cons_val: self._pick(fv, cv))
                grid.addWidget(btn, r + 1, c + 1)

        outer.addLayout(grid)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        outer.addWidget(cancel_btn)

        self.adjustSize()

    def _pick(self, freq, cons):
        self.selection_made.emit(freq, cons)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)


class ConsequenceChainDialog(QDialog):
    """Popup chain editor with QCheckBoxes — safe because it's a QDialog, not a table cell."""

    def __init__(self, db: Database, cons_id: int, parent=None):
        super().__init__(parent)
        self.db      = db
        self.cons_id = cons_id
        self.setWindowTitle("Konsekvenskedja")
        self.setMinimumWidth(460)

        row = db.get_consequence(cons_id)
        self._chain = parse_chain_from_json(
            row['consequence_chain'] if row and 'consequence_chain' in row.keys() else '')
        raw_desc = row['description'] if row else ''

        layout = QVBoxLayout(self)

        # Base event text (editable)
        form = QFormLayout(); form.setSpacing(8)
        self._base_edit = QLineEdit(raw_desc)
        self._base_edit.setPlaceholderText("Händelse / direkt konsekvens")
        self._base_edit.textChanged.connect(self._update_preview)
        form.addRow("Händelse:", self._base_edit)
        layout.addLayout(form)

        # Chain checkboxes — grouped, QCheckBox is safe here
        chain_box = QGroupBox("Konsekvenskedja — välj eskalering")
        chain_lay = QGridLayout(chain_box)
        chain_lay.setSpacing(4)
        self._checks: dict = {}
        row_idx, col_idx, last_group = 0, 0, None

        for key, label, group in _CHAIN_ITEMS:
            if group and group != last_group:
                if col_idx > 0:
                    row_idx += 1; col_idx = 0
                hdr = QLabel(group)
                hdr.setStyleSheet(
                    "color:#1F4E79; font-weight:bold; font-size:10px; margin-top:4px;")
                chain_lay.addWidget(hdr, row_idx, 0, 1, 2)
                row_idx += 1; col_idx = 0
                last_group = group
            chk = QCheckBox(label)
            chk.setChecked(bool(self._chain.get(key, False)))
            chk.stateChanged.connect(self._update_preview)
            self._checks[key] = chk
            chain_lay.addWidget(chk, row_idx, col_idx)
            col_idx += 1
            if col_idx >= 2:
                col_idx = 0; row_idx += 1

        layout.addWidget(chain_box)

        # Generated chain preview
        preview_lbl = QLabel("Genererad text:")
        preview_lbl.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(preview_lbl)
        self._preview = QLabel("—")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            "color:#1F4E79; font-weight:bold; font-size:11px;"
            "background:#eef4fb; border:1px solid #bee3f8;"
            "border-radius:3px; padding:4px 8px;")
        layout.addWidget(self._preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save_and_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._update_preview()

    def _update_preview(self):
        chain = {k: chk.isChecked() for k, chk in self._checks.items()}
        text  = build_consequence_text(self._base_edit.text().strip(), chain)
        self._preview.setText(text or "—")

    def _save_and_accept(self):
        chain    = {k: chk.isChecked() for k, chk in self._checks.items()}
        base     = self._base_edit.text().strip()
        full     = build_consequence_text(base, chain) or base or 'Ny konsekvens'
        cons     = self.db.get_consequence(self.cons_id)
        if cons:
            self.db.update_consequence(
                self.cons_id, full,
                cons['severity'] or 1,
                cons['category'] or '',
                json.dumps(chain))
        self.accept()


class ReductionFactorsDialog(QDialog):
    """Edit the list of extra reduction factors for a consequence."""

    def __init__(self, db, consequence_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.consequence_id = consequence_id
        self.setWindowTitle("Övriga reduktionsfaktorer")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Lägg till faktorer som reducerar slutkonsekvensfrekvensen:"))

        self._tbl = QTableWidget(0, 3)
        self._tbl.setHorizontalHeaderLabels(['Beskrivning', 'RRF', ''])
        self._tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(1, 80); self._tbl.setColumnWidth(2, 64)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.cellChanged.connect(self._on_cell)
        layout.addWidget(self._tbl)

        add_btn = QPushButton("+ Lägg till faktor")
        add_btn.clicked.connect(self._add)
        layout.addWidget(add_btn)
        layout.addWidget(QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                          accepted=self.accept, rejected=self.accept))
        self._refresh()

    def _refresh(self):
        try: self._tbl.cellChanged.disconnect()
        except Exception: pass
        self._tbl.setRowCount(0)
        for rf in self.db.reduction_factors(self.consequence_id):
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            desc = QTableWidgetItem(rf['description'])
            desc.setData(Qt.ItemDataRole.UserRole, rf['id'])
            self._tbl.setItem(r, 0, desc)
            self._tbl.setItem(r, 1, QTableWidgetItem(str(rf['rrf'])))
            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(lambda _, rid=rf['id']: (
                self.db.delete_reduction_factor(rid), self._refresh()))
            self._tbl.setCellWidget(r, 2, del_btn)
            self._tbl.setRowHeight(r, 26)
        self._tbl.cellChanged.connect(self._on_cell)

    def _add(self):
        new_id = self.db.add_reduction_factor(self.consequence_id, 'Ny faktor', 10)
        self._refresh()

    def _on_cell(self, row, col):
        item = self._tbl.item(row, 0)
        if not item: return
        rf_id = item.data(Qt.ItemDataRole.UserRole)
        desc = self._tbl.item(row, 0).text() if self._tbl.item(row, 0) else ''
        try: rrf = int(self._tbl.item(row, 1).text()) if self._tbl.item(row, 1) else 10
        except ValueError: rrf = 10
        self.db.update_reduction_factor(rf_id, desc, rrf, 1)


class _ScenarioDelegate(QStyledItemDelegate):
    """Delegates that installs the parent panel's eventFilter on every inline editor
    so Ctrl+Enter works even while a cell is being edited."""

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            editor.setProperty('editing_row', index.row())
            editor.setProperty('editing_col', index.column())
            editor.installEventFilter(self._panel)
        return editor


_PID_ICON_W = 22          # pixels reserved on the left for the pin icon

_PID_ICON_RE = re.compile(r'^[🟢📌]\s*')   # strip any old emoji prefix


def _draw_pid_pin(painter, rect, placed):
    """Draw a needle pin (circle + stick) inside rect. Green=placed, red=not placed."""
    color = QColor('#27ae60') if placed else QColor('#e74c3c')
    dark  = color.darker(150)

    r      = 4.5          # circle radius
    stick  = 5.0          # stick length below circle
    total  = r * 2 + stick

    cx  = float(rect.center().x())
    top = float(rect.center().y()) - total / 2.0

    circle_cy = top + r
    stick_top = top + r * 2
    stick_bot = top + total

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Stick
    pen = QPen(dark, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(cx, stick_top), QPointF(cx, stick_bot))

    # Circle head
    painter.setBrush(QBrush(color))
    painter.setPen(QPen(dark, 1.0))
    painter.drawEllipse(QPointF(cx, circle_cy), r, r)

    # White highlight dot
    dot_r = r * 0.3
    painter.setBrush(QBrush(QColor(255, 255, 255, 170)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(cx - r * 0.35, circle_cy - r * 0.35), dot_r, dot_r)
    painter.restore()


def _make_pin_icon(placed, size=16):
    """Return a QIcon with the needle pin rendered at the given size."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    _draw_pid_pin(p, QRect(0, 0, size, size), placed)
    p.end()
    return QIcon(px)


class _PidDelegate(_ScenarioDelegate):
    """Draws a P&ID placement icon on the left of Orsak/Konsekvens/Barriär cells.
    The editor always shows only the clean description (emoji stripped)."""

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            # Show only the clean description (EditRole is already clean)
            raw = index.data(Qt.ItemDataRole.EditRole) or ''
            editor.setText(_PID_ICON_RE.sub('', str(raw)))
        return editor

    def setModelData(self, editor, model, index):
        clean = _PID_ICON_RE.sub('', editor.text().strip())
        model.setData(index, clean, Qt.ItemDataRole.EditRole)

    def paint(self, painter, option, index):
        # Draw cell normally but shift content rect right to make room for icon
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(_PID_ICON_W, 0, 0, 0)
        super().paint(painter, opt, index)
        # Overlay the placement icon on the freed left strip
        row, col = index.row(), index.column()
        icon_rect = QRect(option.rect.left(), option.rect.top(),
                          _PID_ICON_W, option.rect.height())
        # Fill icon strip with selection or alternating-row background
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(icon_rect, option.palette.highlight())
        elif index.row() % 2 == 1:
            painter.fillRect(icon_rect, option.palette.alternateBase())
        else:
            painter.fillRect(icon_rect, option.palette.base())
        # Only draw pin if the cell has a real item to place
        if not self._panel._cell_has_item(row, col):
            return
        is_placed = self._panel._is_cell_placed(row, col)
        _draw_pid_pin(painter, icon_rect, is_placed)


class ScenarioTablePanel(QWidget):
    """Extended scenario table with FA, Antändning, Övriga faktorer and Slutkonsekvens."""

    item_selected    = pyqtSignal(int, int)   # (type_, id_) — cell clicked → open right panel
    new_item_created = pyqtSignal(int, int)   # (type_, id_) — after quick-add via Enter menu
    item_edited      = pyqtSignal(int, int)   # (type_, id_) — cell edit committed → sync right panel
    place_requested  = pyqtSignal(int, int)   # (type_, id_) — place/add marker
    navigate_to_pid  = pyqtSignal(int, int)   # (type_, id_) — navigate to existing marker
    remove_requested = pyqtSignal(int, int)   # (type_, id_) — delete all markers

    # Column indices
    _C_NOD, _C_DEV, _C_ORS, _C_KON, _C_RFORE = 0, 1, 2, 3, 4
    _C_SG, _C_REFT, _C_FA, _C_IGN             = 5, 6, 7, 8
    _C_OVRIGA, _C_SLUT                         = 9, 10

    _COLS = [
        'Nod',
        'Avvikelse',
        'Orsak  →',
        'Konsekvens',
        'Risk före barriär',
        'Barriärer  →',
        'Risk efter barriärer',
        'FA ☑',
        'Antändning ☑',
        'Övriga faktorer',
        'Slutkonsekvens',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
        self._node_id = None
        self._deviation_id = None
        self._row_meta = []   # list of (dev_id, cause_id, cons_id, sg_id) per visible row
        self._cons_id  = None  # if set, show only this consequence (set by load_consequence)
        self._enter_row = -1
        self._enter_col = -1
        self._last_enter_committed = False
        self._cell_font_size = 9
        self.setMinimumHeight(160)
        self.setMaximumHeight(380)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        hdr_row = QHBoxLayout()
        self._hdr_lbl = QLabel("HAZOP Scenario")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        self._hdr_lbl.setFont(f)
        hdr_row.addWidget(self._hdr_lbl)
        hdr_row.addStretch()
        hdr_row.addWidget(QLabel("Textstorlek:"))
        self._fs_spin = QSpinBox()
        self._fs_spin.setRange(7, 16)
        self._fs_spin.setValue(9)
        self._fs_spin.setSuffix(" pt")
        self._fs_spin.setFixedWidth(62)
        self._fs_spin.setToolTip("Teckenstorlek i scenario-tabellen")
        self._fs_spin.valueChanged.connect(self._on_font_size_changed)
        hdr_row.addWidget(self._fs_spin)
        outer.addLayout(hdr_row)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        h = self._table.horizontalHeader()
        resize_modes = {
            self._C_NOD:   (QHeaderView.ResizeMode.Interactive, 70),
            self._C_DEV:   (QHeaderView.ResizeMode.Interactive, 120),
            self._C_ORS:   (QHeaderView.ResizeMode.Interactive, 180),
            self._C_KON:   (QHeaderView.ResizeMode.Interactive, 180),
            self._C_RFORE: (QHeaderView.ResizeMode.Interactive, 130),
            self._C_SG:    (QHeaderView.ResizeMode.Interactive, 160),
            self._C_FA:    (QHeaderView.ResizeMode.Interactive, 140),
            self._C_IGN:   (QHeaderView.ResizeMode.Interactive, 140),
            self._C_OVRIGA:(QHeaderView.ResizeMode.Interactive, 120),
            self._C_REFT:  (QHeaderView.ResizeMode.Interactive, 130),
            self._C_SLUT:  (QHeaderView.ResizeMode.Interactive, 130),
        }
        for col, (mode, width) in resize_modes.items():
            h.setSectionResizeMode(col, mode)
            self._table.setColumnWidth(col, width)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(True)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        self._table.installEventFilter(self)
        self._delegate = _ScenarioDelegate(self)
        self._table.setItemDelegate(self._delegate)
        self._pid_delegate = _PidDelegate(self)
        for col in (self._C_ORS, self._C_KON, self._C_SG):
            self._table.setItemDelegateForColumn(col, self._pid_delegate)
        self._table.viewport().installEventFilter(self)
        self._placed_causes       = set()
        self._placed_consequences = set()
        self._placed_safeguards   = set()
        outer.addWidget(self._table)

    # ── Load ──────────────────────────────────────────────────────────────────

    def load_node(self, node_id):
        self._node_id = node_id
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._rebuild()

    def load_deviation(self, deviation_id):
        dev = self.db.get_deviation(deviation_id)
        self._node_id = dev['node_id'] if dev else None
        self._deviation_id = deviation_id
        self.cause_id = None
        self._cons_id = None
        self._rebuild()

    def load_cause(self, cause_id):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = cause_id
        self._cons_id = None
        self._rebuild()

    def load_consequence(self, cons_id):
        row = self.db.get_consequence(cons_id)
        if row:
            self._node_id = None
            self._deviation_id = None
            self.cause_id = dict(row)['cause_id']
            self._cons_id = cons_id
            self._rebuild()

    def clear(self):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._table.setRowCount(0)
        self._hdr_lbl.setText("HAZOP Scenario")

    def _on_font_size_changed(self, size):
        self._cell_font_size = size
        f = QFont()
        f.setPointSize(size)
        self._table.setFont(f)
        self._table.verticalHeader().setDefaultSectionSize(max(28, size * 5 + 7))
        self._rebuild()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _rebuild(self):
        if getattr(self, '_rebuilding', False):
            return
        self._rebuilding = True
        try:
            self._table.cellChanged.disconnect()
        except Exception:
            pass
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        self._row_meta = []

        # Build list of (cause_dict, deviation_dict) to display
        causes_to_show = []
        if self.cause_id is not None:
            c = self.db.get_cause(self.cause_id)
            if c:
                c_d = dict(c)
                dev = self.db.get_deviation(c_d.get('deviation_id'))
                causes_to_show = [(c_d, dict(dev) if dev else {'id': None, 'description': '—'})]
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            dev_d = dict(dev) if dev else {'id': self._deviation_id, 'description': '—'}
            for c in self.db.causes_for_deviation(self._deviation_id):
                causes_to_show.append((dict(c), dev_d))
        elif self._node_id is not None:
            for dev in self.db.deviations(self._node_id):
                dev_d = dict(dev)
                for c in self.db.causes_for_deviation(dev['id']):
                    causes_to_show.append((dict(c), dev_d))

        if not causes_to_show:
            self._table.blockSignals(False)
            self._table.cellChanged.connect(self._on_cell_changed)
            self._rebuilding = False
            return

        # Determine header title from first cause's node
        first_cause = causes_to_show[0][0]
        node = self.db.get_node(first_cause['node_id'])
        node_name_hdr = node['name'] if node else '?'
        if self._cons_id is not None:
            cons = self.db.get_consequence(self._cons_id)
            cons_desc = cons['description'] if cons else '?'
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {first_cause.get('description', '?')} / {cons_desc}")
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {dev['description'] if dev else ''}")
        elif self.cause_id is not None:
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {first_cause.get('description', '?')}")
        elif self._node_id is not None:
            self._hdr_lbl.setText(f"HAZOP Scenario — {node_name_hdr}")
        else:
            self._hdr_lbl.setText(f"HAZOP Scenario — {node_name_hdr}")

        try:
            self.refresh_placed()
            for cause_d, dev_d in causes_to_show:
                node = self.db.get_node(cause_d['node_id'])
                node_name = node['name'] if node else '?'
                # Resolve frequency: prefer live standard_causes lookup, then base_freq, then likelihood
                std_id = cause_d.get('standard_cause_id')
                base_freq = None
                if std_id:
                    sc = self.db.get_standard_cause(std_id)
                    if sc and sc.get('frequency') is not None:
                        base_freq = sc['frequency']
                if base_freq is None:
                    base_freq = cause_d.get('base_freq')
                if base_freq is not None:
                    freq = freq_to_f_level(base_freq)
                elif cause_d['likelihood'] is not None:
                    freq = cause_d['likelihood']
                else:
                    freq = 3
                _fi = freq_to_idx(freq)
                freq_lbl = _FREQ_LABELS[_fi] if _fi < len(_FREQ_LABELS) else f'F{freq}'
                first_row_for_cause = self._table.rowCount()
                all_cons = list(self.db.consequences(cause_d['id']))
                if self._cons_id is not None:
                    all_cons = [c for c in all_cons if dict(c)['id'] == self._cons_id]
                for cons in all_cons:
                    cons_d = dict(cons)
                    sgs = [dict(s) for s in self.db.safeguards(cons_d['id'])]
                    if sgs:
                        for sg in sgs:
                            self._add_row(node_name, dev_d, cause_d, freq, freq_lbl, cons_d, sgs, sg)
                    else:
                        self._add_row(node_name, dev_d, cause_d, freq, freq_lbl, cons_d, [], None)
                if self._table.rowCount() == first_row_for_cause:
                    self._add_empty_row(node_name, dev_d, cause_d, freq, freq_lbl)
            self._apply_spans()
        except Exception as e:
            QMessageBox.critical(None, "Fel i scenariopanel", str(e))
        finally:
            self._table.blockSignals(False)
            self._table.cellChanged.connect(self._on_cell_changed)
            self._rebuilding = False

    def _apply_spans(self):
        """Merge consecutive rows that share the same Nod or Orsak."""
        n = self._table.rowCount()
        if n < 2:
            return

        def _span_col(col, key_fn):
            r = 0
            while r < n:
                k = key_fn(r)
                span = 1
                while r + span < n and key_fn(r + span) == k and k is not None:
                    span += 1
                if span > 1:
                    self._table.setSpan(r, col, span, 1)
                    item = self._table.item(r, col)
                    if item:
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
                r += span

        def _meta(r, idx):
            return self._row_meta[r][idx] if r < len(self._row_meta) else None

        # Nod: group by node_id stored in UserRole
        _span_col(self._C_NOD, lambda r: (
            self._table.item(r, self._C_NOD).data(Qt.ItemDataRole.UserRole)
            if self._table.item(r, self._C_NOD) else None))

        # Avvikelse: group by dev_id (index 0 in row_meta)
        _span_col(self._C_DEV, lambda r: _meta(r, 0))

        # Orsak: group by cause_id (index 1)
        _span_col(self._C_ORS, lambda r: _meta(r, 1))

        # Consequence-level columns: group by cons_id (index 2)
        for col in (self._C_KON, self._C_RFORE, self._C_REFT,
                    self._C_FA, self._C_IGN, self._C_OVRIGA, self._C_SLUT):
            _span_col(col, lambda r: _meta(r, 2))

    def _add_empty_row(self, node_name, dev_d, cause_d, freq, freq_lbl):
        """Placeholder row when a cause has no consequences yet."""
        r = self._table.rowCount()
        self._table.insertRow(r)
        dev_id = dev_d['id'] if dev_d else None
        self._row_meta.append((dev_id, cause_d['id'], None, None))

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(node_name)
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        dev_item = _ro(dev_d['description'] if dev_d else '')
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(r, self._C_DEV, dev_item)

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole, ('cause', cause_d['id']))
        self._table.setItem(r, self._C_ORS, ors)

        kon = _ro()
        kon.setToolTip("Tryck Enter för att lägga till konsekvens")
        self._table.setItem(r, self._C_KON, kon)

        for col in (self._C_RFORE, self._C_SG, self._C_REFT,
                    self._C_FA, self._C_IGN, self._C_OVRIGA, self._C_SLUT):
            self._table.setItem(r, col, _ro())

        self._table.setRowHeight(r, max(36, self._cell_font_size * 5 + 7))

    def _add_row(self, node_name, dev_d, cause_d, freq, freq_lbl, cons_d, sgs, sg):
        """One row per safeguard (sg=None when no safeguards exist yet).
        sgs = all safeguards for this consequence (used for combined risk calc)."""
        r   = self._table.rowCount()
        self._table.insertRow(r)
        sev = cons_d['severity'] or 1
        cid = cons_d['id']
        dev_id = dev_d['id'] if dev_d else None

        self._row_meta.append((dev_id, cause_d['id'], cid, sg['id'] if sg else None))
        sg_rrf = 1
        for s in sgs:
            sg_rrf *= (s.get('rrf') or 1)

        # Extra reduction factors
        rfs = [dict(rf) for rf in self.db.reduction_factors(cid)]
        fa_active  = bool(cons_d.get('fa_active', 0))
        fa_rrf     = cons_d.get('fa_rrf', 10) or 10
        ign_active = bool(cons_d.get('ignition_active', 0))
        ign_rrf    = cons_d.get('ignition_rrf', 10) or 10

        final_f, total_rrf, total_steps = total_freq_reduction(
            freq, sg_rrf, fa_active, fa_rrf, ign_active, ign_rrf, rfs)

        level_b, bg_b, fg_b = risk_info(freq, sev)
        level_a, bg_a, fg_a = risk_info(effective_frequency(freq, sg_rrf), sev)
        level_s, bg_s, fg_s = risk_info(final_f, sev)

        # ── Col 0: Nod ────────────────────────────────────────────────────────
        nod = QTableWidgetItem(node_name)
        nod.setFlags(nod.flags() & ~Qt.ItemFlag.ItemIsEditable)
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        # ── Col 1: Avvikelse ─────────────────────────────────────────────────
        dev_item = QTableWidgetItem(dev_d['description'] if dev_d else '')
        dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(r, self._C_DEV, dev_item)

        # ── Col 2: Orsak (editable, description only) ────────────────────────
        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole, ('cause', cause_d['id']))
        self._table.setItem(r, self._C_ORS, ors)

        # ── Col 2: Konsekvens (editable description) ─────────────────────────
        chain_data   = parse_chain_from_json(cons_d.get('consequence_chain', ''))
        display_desc = (build_consequence_text(cons_d['description'], chain_data)
                        or cons_d['description'])

        kon_item = QTableWidgetItem(cons_d['description'])
        kon_item.setData(Qt.ItemDataRole.UserRole, ('consequence', cid))
        if display_desc != cons_d['description']:
            kon_item.setToolTip(f"Kedjetext: {display_desc}\n(Redigera kedja i höger panel)")
        self._table.setItem(r, self._C_KON, kon_item)

        # ── Col 3: Risk före barriär — klickbar för att öppna riskmatris ────────
        rb = QTableWidgetItem(f"{level_b}\n{freq_axis_label(freq)}  {cons_axis_label(sev)}")
        rb.setBackground(QBrush(QColor(bg_b))); rb.setForeground(QBrush(QColor(_contrast_fg(bg_b))))
        rb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        rb.setFlags(rb.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rb.setToolTip("🖱 Klicka för att ändra i riskmatrisen")
        # Store ids so cellClicked can open the matrix popup
        rb.setData(Qt.ItemDataRole.UserRole, ('risk_click', cause_d['id'], cid, freq, sev))
        self._table.setItem(r, self._C_RFORE, rb)

        # ── Col 4: Barriär — one row per safeguard ───────────────────────────
        if sg is None:
            sg_item = QTableWidgetItem('—')
            sg_item.setFlags(sg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sg_item.setToolTip("Tryck Enter för att lägga till safeguard")
        else:
            rrf = sg.get('rrf', 1) or 1
            sg_item = QTableWidgetItem()
            display = sg['description'] + (f"\n[RRF {rrf}]" if rrf > 1 else "")
            sg_item.setData(Qt.ItemDataRole.DisplayRole, display)
            sg_item.setData(Qt.ItemDataRole.EditRole, sg['description'])
            sg_item.setData(Qt.ItemDataRole.UserRole, ('safeguard', sg['id']))
            sg_item.setToolTip("Klicka för att redigera" + (f"  [RRF {rrf}]" if rrf > 1 else ""))
        self._table.setItem(r, self._C_SG, sg_item)

        # ── Col 5: FA — checkable, text = probability % ──────────────────────
        fa_steps_txt = f"−{prob_to_reduction(fa_rrf)} steg" if fa_active else f"{fa_rrf}%"
        fa_item = QTableWidgetItem(f"{fa_rrf}%")
        fa_item.setCheckState(
            Qt.CheckState.Checked if fa_active else Qt.CheckState.Unchecked)
        fa_item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                         Qt.ItemFlag.ItemIsUserCheckable |
                         Qt.ItemFlag.ItemIsEditable)
        fa_item.setData(Qt.ItemDataRole.UserRole, ('fa', cid))
        fa_item.setToolTip(
            f"Närvaro/FA-sannolikhet i %.\n"
            f"10% = −1 steg, 1% = −2 steg, 0.1% = −3 steg\n"
            f"Skriv ett nytt värde (t.ex. 10 eller 1) och tryck Enter.")
        self._table.setItem(r, self._C_FA, fa_item)

        # ── Col 6: Antändning — checkable, text = probability % ──────────────
        ign_item = QTableWidgetItem(f"{ign_rrf}%")
        ign_item.setCheckState(
            Qt.CheckState.Checked if ign_active else Qt.CheckState.Unchecked)
        ign_item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                          Qt.ItemFlag.ItemIsUserCheckable |
                          Qt.ItemFlag.ItemIsEditable)
        ign_item.setData(Qt.ItemDataRole.UserRole, ('ignition', cid))
        ign_item.setToolTip(
            f"Antändningssannolikhet i %.\n"
            f"10% = −1 steg, 1% = −2 steg, 0.1% = −3 steg")
        self._table.setItem(r, self._C_IGN, ign_item)

        # ── Col 7: Övriga faktorer ────────────────────────────────────────────
        n_active = sum(1 for rf in rfs if rf.get('active'))
        extra_btn = QPushButton(
            f"📋 {n_active} aktiv(a)" if n_active else "📋 Lägg till…")
        extra_btn.setFlat(True)
        extra_btn.clicked.connect(lambda _, c=cid: self._edit_extra(c))
        self._table.setCellWidget(r, self._C_OVRIGA, extra_btn)

        # ── Col 8: Risk efter barriärer (safeguards only) ────────────────────
        f_eff    = effective_frequency(freq, sg_rrf)
        sg_steps = int(math.log10(max(1, sg_rrf))) if sg_rrf > 1 else 0
        sg_step_str = f"  −{sg_steps} steg" if sg_steps > 0 else ""
        ra = QTableWidgetItem(f"{level_a}{sg_step_str}\n{freq_axis_label(f_eff)}  {cons_axis_label(sev)}")
        ra.setBackground(QBrush(QColor(bg_a))); ra.setForeground(QBrush(QColor(_contrast_fg(bg_a))))
        ra.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        ra.setFlags(ra.flags() & ~Qt.ItemFlag.ItemIsEditable)
        ra.setToolTip(f"{freq_axis_label(f_eff)}  {cons_axis_label(sev)}  (efter safeguards)")
        self._table.setItem(r, self._C_REFT, ra)

        # ── Col 9: Slutkonsekvens (alla reduktioner) ──────────────────────────
        slut_step_str = f"  −{total_steps} steg" if total_steps > 0 else ""
        rs = QTableWidgetItem(f"{level_s}{slut_step_str}\n{freq_axis_label(final_f)}  {cons_axis_label(sev)}")
        rs.setBackground(QBrush(QColor(bg_s))); rs.setForeground(QBrush(QColor(_contrast_fg(bg_s))))
        rs.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        rs.setFlags(rs.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rs.setToolTip(f"{freq_axis_label(final_f)}  {cons_axis_label(sev)}  (−{total_steps} steg totalt)")
        self._table.setItem(r, self._C_SLUT, rs)

        self._table.setRowHeight(r, max(36, self._cell_font_size * 5 + 7))

    def _open_chain_editor(self, cons_id: int, label_widget=None):
        """Open the consequence chain dialog; refresh the label on accept."""
        dlg = ConsequenceChainDialog(self.db, cons_id, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Update the label in the cell without full rebuild
            if label_widget is not None:
                row = self.db.get_consequence(cons_id)
                if row:
                    chain = parse_chain_from_json(
                        row['consequence_chain'] if 'consequence_chain' in row.keys() else '')
                    text = build_consequence_text(row['description'], chain) or row['description']
                    label_widget.setText(text)
            # Rebuild risk cells (description changed)
            QTimer.singleShot(0, self._rebuild)

    def _edit_extra(self, cons_id):
        dlg = ReductionFactorsDialog(self.db, cons_id, self)
        dlg.exec()
        self._rebuild()

    # ── P&ID placement helpers ─────────────────────────────────────────────────

    def refresh_placed(self):
        """Reload which IDs are placed on the P&ID and repaint the table."""
        try:
            self._placed_causes       = set(self.db.marked_cause_ids())
            self._placed_consequences = set(self.db.marked_consequence_ids())
            self._placed_safeguards   = set(self.db.marked_safeguard_ids())
        except Exception:
            pass
        self._table.viewport().update()

    def _cell_has_item(self, row, col):
        """Returns True only when the cell actually has a placeable item ID."""
        if row >= len(self._row_meta):
            return False
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS:
            return cause_id is not None
        if col == self._C_KON:
            return cons_id is not None
        if col == self._C_SG:
            return sg_id is not None
        return False

    def _is_cell_placed(self, row, col):
        if row >= len(self._row_meta):
            return False
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS:
            return cause_id in self._placed_causes
        if col == self._C_KON:
            return cons_id in self._placed_consequences
        if col == self._C_SG:
            return sg_id is not None and sg_id in self._placed_safeguards
        return False

    def _place_from_table(self, row, col):
        if row >= len(self._row_meta):
            return
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS and cause_id is not None:
            self.place_requested.emit(CAUSE_T, cause_id)
        elif col == self._C_KON and cons_id is not None:
            self.place_requested.emit(CONS_T, cons_id)
        elif col == self._C_SG and sg_id is not None:
            self.place_requested.emit(SG_T, sg_id)

    def _emit_navigate(self, row, col):
        if row >= len(self._row_meta):
            return
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS and cause_id is not None:
            self.navigate_to_pid.emit(CAUSE_T, cause_id)
        elif col == self._C_KON and cons_id is not None:
            self.navigate_to_pid.emit(CONS_T, cons_id)
        elif col == self._C_SG and sg_id is not None:
            self.navigate_to_pid.emit(SG_T, sg_id)

    def _remove_from_pid(self, row, col):
        if row >= len(self._row_meta):
            return
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS and cause_id is not None:
            self.remove_requested.emit(CAUSE_T, cause_id)
        elif col == self._C_KON and cons_id is not None:
            self.remove_requested.emit(CONS_T, cons_id)
        elif col == self._C_SG and sg_id is not None:
            self.remove_requested.emit(SG_T, sg_id)

    def _on_table_context_menu(self, pos):
        col = self._table.columnAt(pos.x())
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._row_meta):
            return
        if col not in (self._C_ORS, self._C_KON, self._C_SG):
            return
        if not self._cell_has_item(row, col):
            return  # no item to place/remove — e.g. safeguard row with no safeguard yet
        is_placed = self._is_cell_placed(row, col)
        menu = QMenu(self)
        if not is_placed:
            a = menu.addAction("Lägg till på P&ID")
            a.triggered.connect(lambda: self._place_from_table(row, col))
        else:
            a1 = menu.addAction("Lägg till ytterligare på P&ID")
            a1.triggered.connect(lambda: self._place_from_table(row, col))
            a2 = menu.addAction("Ta bort från P&ID")
            a2.triggered.connect(lambda: self._remove_from_pid(row, col))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_cell_clicked(self, row, col):
        if col == self._C_ORS and row < len(self._row_meta):
            self.item_selected.emit(CAUSE_T, self._row_meta[row][0])
            # Single-click starts editing immediately (deferred so right-panel load runs first)
            QTimer.singleShot(0, lambda r=row, c=col: (
                self._table.setFocus(),
                self._table.edit(self._table.model().index(r, c))
            ))
            return
        if col == self._C_KON and row < len(self._row_meta):
            self.item_selected.emit(CONS_T, self._row_meta[row][1])
            QTimer.singleShot(0, lambda r=row, c=col: (
                self._table.setFocus(),
                self._table.edit(self._table.model().index(r, c))
            ))
            return
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][2]
            if sg_id is not None:
                self.item_selected.emit(SG_T, sg_id)
                QTimer.singleShot(0, lambda r=row, c=col: (
                    self._table.setFocus(),
                    self._table.edit(self._table.model().index(r, c))
                ))
            return
        if col != self._C_RFORE:
            return
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta or meta[0] != 'risk_click':
            return
        _, cause_id, cons_id, cur_freq, cur_cons = meta

        popup = RiskMatrixPopup(cur_freq, cur_cons, self)
        popup.selection_made.connect(
            lambda f, c, caid=cause_id, coid=cons_id:
                self._apply_risk_from_matrix(caid, coid, f, c))

        # Position popup: prefer above the cell, fall back to below if off-screen
        popup.adjustSize()
        cell_rect  = self._table.visualItemRect(item)
        anchor     = self._table.viewport().mapToGlobal(cell_rect.topLeft())
        screen     = QApplication.primaryScreen().availableGeometry()
        ph         = popup.sizeHint().height()
        pw         = popup.sizeHint().width()
        # Try above first
        y = anchor.y() - ph - 4
        if y < screen.top():
            y = anchor.y() + cell_rect.height() + 4   # fall back: below
        x = max(screen.left(), min(anchor.x(), screen.right() - pw))
        popup.move(x, y)
        popup.exec()

    def _apply_risk_from_matrix(self, cause_id, cons_id, new_freq, new_cons):
        self.db.update_cause(cause_id, likelihood=new_freq)
        cons = self.db.get_consequence(cons_id)
        if cons:
            self.db.update_consequence(
                cons_id, cons['description'], new_cons, cons['category'] or '')
        QTimer.singleShot(0, self._rebuild)

    # ── Enter-tangent: snabblägg-till ─────────────────────────────────────────

    def eventFilter(self, obj, event):
        ctrl = bool(event.type() == QEvent.Type.KeyPress and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # Viewport mouse: detect LEFT-click in the 🟢/📌 icon strip
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            col = self._table.columnAt(pos.x())
            row = self._table.rowAt(pos.y())
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                col_x = self._table.columnViewportPosition(col)
                if pos.x() - col_x < _PID_ICON_W:
                    if not self._cell_has_item(row, col):
                        return True  # consume click but do nothing for empty cells
                    if self._is_cell_placed(row, col):
                        # 🟢 → navigate to marker on P&ID
                        self._emit_navigate(row, col)
                    else:
                        # 📌 → place this specific item on P&ID
                        self._place_from_table(row, col)
                    return True  # consume left-click; right-click falls through to context menu

        # Delegate inline editor (regular cell in edit mode)
        if (isinstance(obj, QLineEdit) and
                obj.property('editing_row') is not None and
                obj.property('sg_id') is None):
            if ctrl and event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    row = obj.property('editing_row')
                    col = obj.property('editing_col')
                    # Commit the current edit first, then create sibling
                    self._delegate.commitData.emit(obj)
                    self._delegate.closeEditor.emit(obj, QStyledItemDelegate.EndEditHint.NoHint)
                    self._ctrl_enter(row, col)
                    return True

        # Table-level Enter key
        if obj is self._table and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                row = self._table.currentRow()
                col = self._table.currentColumn()
                if ctrl:
                    self._ctrl_enter(row, col)
                    return True
                self._enter_row = row
                self._enter_col = col
                self._last_enter_committed = False
                QTimer.singleShot(0, self._on_enter_after_edit)
        return False

    def _ctrl_enter(self, row, col):
        """Ctrl+Enter: immediately create a new sibling at the same hierarchy level."""
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, _sg_id = self._row_meta[row]
        if col in (self._C_ORS, self._C_NOD, self._C_DEV):
            if dev_id is not None:
                self._quick_add_cause(dev_id)
        elif col in (self._C_KON, self._C_RFORE):
            self._quick_add_consequence(cause_id)
        else:
            # SG, REFT, FA, IGN, OVRIGA, SLUT → new safeguard
            if cons_id is not None:
                self._quick_add_safeguard(cons_id)

    def _on_enter_after_edit(self):
        row = self._enter_row
        if row < 0 or row >= len(self._row_meta):
            return
        # Editable cells (Orsak): only show menu after a real commit, not when starting to type.
        # Non-editable cells (Barriär) and widget cells (Konsekvens): always show menu.
        item = self._table.item(row, self._enter_col)
        is_editable = item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEditable)
        if is_editable and not self._last_enter_committed:
            return
        self._last_enter_committed = False
        dev_id, cause_id, cons_id, _sg_id = self._row_meta[row]
        self._show_quick_add(row, dev_id, cause_id, cons_id)

    def _show_quick_add(self, row, dev_id, cause_id, cons_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        dev = self.db.get_deviation(dev_id) if dev_id else None
        dev_name = dev['description'] if dev else '?'

        menu = QMenu(self)
        menu.addSection("Lägg till i hierarkin")
        menu.addAction(f'⚙  Ny orsak under avvikelse  [{dev_name}]',
                       lambda: self._quick_add_cause(dev_id))
        menu.addAction("⚠  Ny konsekvens på denna orsak",
                       lambda: self._quick_add_consequence(cause_id))
        sg_action = menu.addAction("🛡  Ny safeguard på denna konsekvens",
                       lambda: self._quick_add_safeguard(cons_id))
        sg_action.setEnabled(cons_id is not None)

        idx   = self._table.model().index(row, self._C_ORS)
        rect  = self._table.visualRect(idx)
        pos   = self._table.viewport().mapToGlobal(rect.bottomLeft())
        menu.exec(pos)

    def _quick_add_cause(self, deviation_id):
        new_id = self.db.add_cause(deviation_id)
        self.new_item_created.emit(CAUSE_T, new_id)

    def _quick_add_consequence(self, cause_id):
        new_id = self.db.add_consequence(cause_id)
        self.new_item_created.emit(CONS_T, new_id)

    def _quick_add_safeguard(self, cons_id):
        new_id = self.db.add_safeguard(cons_id)
        self.new_item_created.emit(SG_T, new_id)

    def _on_cell_changed(self, row, col):
        try:
            self._on_cell_changed_inner(row, col)
        except Exception as e:
            QMessageBox.critical(None, "Fel vid celländring (scenario)", str(e))

    def _on_cell_changed_inner(self, row, col):
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        kind, id_ = meta
        text = item.text().strip()

        if kind == 'cause':
            desc = text.split('\n')[0].strip()
            self.db.update_cause(id_, desc)
            self.item_edited.emit(CAUSE_T, id_)

        elif kind == 'consequence':
            desc = text.split('\n')[0].strip()
            cons = self.db.get_consequence(id_)
            if cons:
                self.db.update_consequence(id_, desc, cons['severity'], cons['category'] or '')
            self.item_edited.emit(CONS_T, id_)

        elif kind == 'safeguard':
            edit_val = item.data(Qt.ItemDataRole.EditRole)
            desc = (str(edit_val).strip() if edit_val is not None else text.split('\n')[0].strip()) or 'Ny safeguard'
            sg = self.db.get_safeguard(id_)
            if sg:
                self.db.update_safeguard(id_, desc, sg['rrf'] or 1)
            self.item_edited.emit(SG_T, id_)
            QTimer.singleShot(0, self._rebuild)

        elif kind in ('fa', 'ignition'):
            # Checkbox state + editable probability % value
            active = (item.checkState() == Qt.CheckState.Checked)
            try:
                # Strip '%' and any spaces, accept both "10" and "10%"
                val_str = text.replace('%', '').strip()
                prob = float(val_str) if val_str else 10.0
                prob = max(0.001, min(99.9, prob))
            except (ValueError, TypeError):
                prob = 10.0
            if kind == 'fa':
                self.db.conn.execute(
                    "UPDATE consequences SET fa_active=?,fa_rrf=? WHERE id=?",
                    (int(active), prob, id_))
            else:
                self.db.conn.execute(
                    "UPDATE consequences SET ignition_active=?,ignition_rrf=? WHERE id=?",
                    (int(active), prob, id_))
            self.db.conn.commit()
            QTimer.singleShot(0, self._rebuild)

        if (row, col) == (self._enter_row, self._enter_col):
            self._last_enter_committed = True


# ══════════════════════════════════════════════════════════════════════════════
# HAZOP WORKSHEET
# ══════════════════════════════════════════════════════════════════════════════

class HAZOPWorksheet(QWidget):
    # Columns: Nod | P&ID | Orsak | F | Konsekvens | C | Risk före | Safeguards | Risk efter | Kategori | Åtgärder
    _HEADERS    = ['Nod', 'P&ID', 'Orsak', 'F', 'Konsekvens', 'C',
                   'Risk före barriär', 'Safeguards', 'Risk efter barriär', 'Kategori', 'Åtgärder']
    _COL_WIDTHS = [110, 55, 170, 38, 170, 38, 110, 170, 110, 65, 180]

    # Column indices
    _C_NOD, _C_PID, _C_ORS, _C_F, _C_KON, _C_C = 0, 1, 2, 3, 4, 5
    _C_RFORE, _C_SG, _C_REFT, _C_KAT, _C_ATG   = 6, 7, 8, 9, 10

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._loading = False
        self._row_meta = []   # [(cause_id, consequence_id), ...]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        bar = QHBoxLayout()
        title = QLabel("HAZOP Worksheet")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        bar.addWidget(title); bar.addStretch()
        note = QLabel("Klicka en cell i F- eller C-kolumnen för att redigera")
        note.setStyleSheet("color:#888; font-size:11px;")
        bar.addWidget(note)
        btn = QPushButton("🔄 Uppdatera")
        btn.clicked.connect(self.refresh)
        bar.addWidget(btn)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(self._HEADERS))
        self.table.setHorizontalHeaderLabels(self._HEADERS)
        hdr = self.table.horizontalHeader()
        for i, w in enumerate(self._COL_WIDTHS):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(i, w)
        hdr.setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        layout.addWidget(self.table)

    def refresh(self):
        self._loading = True
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._row_meta = []
        prev_node = prev_cause = None

        for row in self.db.all_data():
            freq = row['likelihood']   # stored in causes.likelihood, value -1..5
            sev  = row['severity']     # stored in consequences.severity, value 1..5

            # Risk before barriers
            level_b, bg_b, fg_b = risk_info(freq, sev)

            # Total RRF and risk after barriers
            total_rrf = 1
            for sg in row['safeguards']:
                total_rrf *= sg.get('rrf', 1)
            eff_f = effective_frequency(freq, total_rrf)
            level_a, bg_a, fg_a = risk_info(eff_f, sev)

            sg_lines = []
            for sg in row['safeguards']:
                rrf = sg.get('rrf', 1)
                sg_lines.append(f"{sg['description']}" + (f"  RRF {rrf}" if rrf > 1 else ""))
            if total_rrf > 1:
                reduction = int(math.log10(total_rrf))
                sg_lines.append(f"─── Total RRF {total_rrf:,}  (−{reduction} F-steg)")
            sg_text = '\n'.join(sg_lines) or '—'

            act_text = '\n'.join(
                f"• {a['description']} ({a['status']})" for a in row['actions']) or '—'

            r = self.table.rowCount()
            self.table.insertRow(r)
            self._row_meta.append((row.get('cause_id'), row.get('consequence_id')))

            same_node  = row['node_name'] == prev_node
            same_cause = same_node and row['cause'] == prev_cause

            def _ro(text, center=False):
                item = QTableWidgetItem(str(text))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if center else
                                      Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                return item

            self.table.setItem(r, self._C_NOD, _ro('' if same_node  else row['node_name']))
            self.table.setItem(r, self._C_PID, _ro('' if same_node  else row['node_pid']))
            self.table.setItem(r, self._C_ORS, _ro('' if same_cause else row['cause']))

            # F — editable combo
            f_combo = QComboBox()
            f_combo.addItems(_FREQ_LABELS)
            f_combo.setCurrentIndex(freq_to_idx(freq))
            cause_id = row.get('cause_id')
            f_combo.currentIndexChanged.connect(
                lambda idx, cid=cause_id: self._freq_changed(cid, idx))
            self.table.setCellWidget(r, self._C_F, f_combo)

            self.table.setItem(r, self._C_KON, _ro(row['consequence']))

            # C — editable combo
            c_combo = QComboBox()
            c_combo.addItems(_SEV_LABELS)
            c_combo.setCurrentIndex(max(0, sev - 1))
            cons_id = row.get('consequence_id')
            c_combo.currentIndexChanged.connect(
                lambda idx, cid=cons_id, cat=row['category']: self._sev_changed(cid, idx, cat))
            self.table.setCellWidget(r, self._C_C, c_combo)

            # Risk before
            rb = QTableWidgetItem(f"{level_b}\n{freq_axis_label(freq)}  {cons_axis_label(sev)}")
            rb.setBackground(QBrush(QColor(bg_b)))
            rb.setForeground(QBrush(QColor(_contrast_fg(bg_b))))
            rb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            rb.setFlags(rb.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, self._C_RFORE, rb)

            self.table.setItem(r, self._C_SG, _ro(sg_text))

            # Risk after
            ra = QTableWidgetItem(f"{level_a}\n{freq_axis_label(eff_f)}  {cons_axis_label(sev)}")
            ra.setBackground(QBrush(QColor(bg_a)))
            ra.setForeground(QBrush(QColor(_contrast_fg(bg_a))))
            ra.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ra.setFlags(ra.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, self._C_REFT, ra)

            self.table.setItem(r, self._C_KAT, _ro(row['category']))
            self.table.setItem(r, self._C_ATG, _ro(act_text))

            lines = max(2, sg_text.count('\n') + 1, act_text.count('\n') + 1)
            self.table.setRowHeight(r, max(44, min(120, lines * 18)))

            prev_node  = row['node_name']
            prev_cause = row['cause']

        self.table.blockSignals(False)
        self._loading = False

    def _freq_changed(self, cause_id, combo_idx):
        if self._loading or not cause_id:
            return
        new_freq = idx_to_freq(combo_idx)
        self.db.update_cause(cause_id, likelihood=new_freq)
        self.refresh()

    def _sev_changed(self, cons_id, combo_idx, category):
        if self._loading or not cons_id:
            return
        new_sev = combo_idx + 1
        cons = self.db.get_consequence(cons_id)
        desc = dict(cons)['description'] if cons else ''
        self.db.update_consequence(cons_id, desc, new_sev, category)
        self.refresh()


# ══════════════════════════════════════════════════════════════════════════════
# RISK SCENARIO WIZARD
# ══════════════════════════════════════════════════════════════════════════════

class RiskScenarioWizard(QDialog):
    def __init__(self, db: Database, node_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.node_id = node_id
        self.created_cause_id = None
        self.created_cons_id  = None

        self.setWindowTitle("Risk Scenario — Guide")
        self.setMinimumWidth(500)

        outer = QVBoxLayout(self)

        self._step_lbl = QLabel()
        f = QFont(); f.setBold(True); f.setPointSize(11)
        self._step_lbl.setFont(f)
        self._step_lbl.setStyleSheet("color:#1F4E79; padding:4px;")
        outer.addWidget(self._step_lbl)

        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ccc;")
        outer.addWidget(sep)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        # Step 1: Cause
        p1 = QWidget()
        f1 = QFormLayout(p1); f1.setSpacing(10)
        self._cause_desc = QTextEdit()
        self._cause_desc.setPlaceholderText("Beskriv orsaken till avvikelsen / faran...")
        self._cause_desc.setFixedHeight(100)
        f1.addRow("Beskrivning:", self._cause_desc)
        self._cause_like = QComboBox()
        self._cause_like.addItems(_FREQ_LABELS)
        self._cause_like.setCurrentIndex(freq_to_idx(3))
        f1.addRow("Frekvens (F):", self._cause_like)
        self._stack.addWidget(p1)

        # Step 2: Consequence
        p2 = QWidget()
        f2 = QFormLayout(p2); f2.setSpacing(10)
        self._cons_desc = QTextEdit()
        self._cons_desc.setPlaceholderText("Beskriv konsekvensen...")
        self._cons_desc.setFixedHeight(100)
        f2.addRow("Beskrivning:", self._cons_desc)
        self._cons_sev = QComboBox()
        self._cons_sev.addItems(_SEV_LABELS)
        self._cons_sev.currentIndexChanged.connect(self._update_preview)
        f2.addRow("Allvarlighet (S):", self._cons_sev)
        self._cons_cat = QComboBox()
        self._cons_cat.addItem('')
        for cat in self.db.consequence_categories():
            self._cons_cat.addItem(cat['name'])
        f2.addRow("Kategori:", self._cons_cat)
        self._preview_badge = RiskBadge()
        f2.addRow("Förhandsvisning risk:", self._preview_badge)
        self._stack.addWidget(p2)

        # Step 3: Safeguard (optional)
        p3 = QWidget()
        f3 = QFormLayout(p3); f3.setSpacing(10)
        note = QLabel("Lämna tom för att hoppa över safeguard.")
        note.setStyleSheet("color:#888; font-style:italic;")
        f3.addRow(note)
        self._sg_desc = QLineEdit()
        self._sg_desc.setPlaceholderText("t.ex. Säkerhetsventil PSV-101")
        f3.addRow("Beskrivning:", self._sg_desc)
        self._sg_rrf = QComboBox()
        self._sg_rrf.addItems(_RRF_LABELS)
        self._sg_rrf.currentIndexChanged.connect(self._update_preview)
        f3.addRow("RRF:", self._sg_rrf)
        self._sg_badge = RiskBadge()
        f3.addRow("Effektiv risk:", self._sg_badge)
        self._stack.addWidget(p3)

        # Buttons
        btn_row = QHBoxLayout()
        self._back_btn = QPushButton("◀ Tillbaka")
        self._next_btn = QPushButton("Nästa ▶")
        self._finish_btn = QPushButton("✅ Slutför")
        self._finish_btn.setVisible(False)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn.clicked.connect(self._go_next)
        self._finish_btn.clicked.connect(self._finish)
        btn_row.addWidget(self._back_btn)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._next_btn)
        btn_row.addWidget(self._finish_btn)
        outer.addLayout(btn_row)

        self._go_to(0)

    def _go_to(self, step):
        self._stack.setCurrentIndex(step)
        self._step_lbl.setText(
            ["Steg 1 av 3: Orsak", "Steg 2 av 3: Konsekvens", "Steg 3 av 3: Safeguard"][step])
        self._back_btn.setEnabled(step > 0)
        self._next_btn.setVisible(step < 2)
        self._finish_btn.setVisible(step == 2)
        self._update_preview()

    def _go_back(self):
        self._go_to(self._stack.currentIndex() - 1)

    def _go_next(self):
        self._go_to(self._stack.currentIndex() + 1)

    def _update_preview(self):
        sev  = self._cons_sev.currentIndex() + 1
        freq = idx_to_freq(self._cause_like.currentIndex())
        self._preview_badge.update_risk(freq, sev)
        rrf   = _RRF_VALUES[self._sg_rrf.currentIndex()]
        eff_f = effective_frequency(freq, rrf)
        self._sg_badge.update_risk(eff_f, sev)

    def _finish(self):
        cause_desc = self._cause_desc.toPlainText().strip() or 'Ny orsak'
        freq  = idx_to_freq(self._cause_like.currentIndex())
        dev_id = self.db.get_or_create_deviation(self.node_id, "Övrigt")
        c_id  = self.db.add_cause(dev_id)
        self.db.update_cause(c_id, cause_desc, freq)

        cons_desc = self._cons_desc.toPlainText().strip() or 'Ny konsekvens'
        sev  = self._cons_sev.currentIndex() + 1
        cat  = self._cons_cat.currentText()
        k_id = self.db.add_consequence(c_id)
        self.db.update_consequence(k_id, cons_desc, sev, cat)

        sg_desc = self._sg_desc.text().strip()
        if sg_desc:
            rrf  = _RRF_VALUES[self._sg_rrf.currentIndex()]
            s_id = self.db.add_safeguard(k_id)
            self.db.update_safeguard(s_id, sg_desc, rrf)

        self.created_cause_id = c_id
        self.created_cons_id  = k_id
        self.accept()


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS PANEL
# ══════════════════════════════════════════════════════════════════════════════

class ComponentEditorPanel(QWidget):
    """Settings panel for managing component types and failure modes."""

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cur_comp_id = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ── Left: component type list ─────────────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("Komponenttyper:"))
        self._comp_list = QListWidget()
        self._comp_list.currentItemChanged.connect(self._on_comp_selected)
        left.addWidget(self._comp_list)

        comp_btns = QHBoxLayout()
        btn_add_c  = QPushButton("+ Lägg till")
        btn_ren_c  = QPushButton("✎ Byt namn")
        btn_del_c  = QPushButton("✕ Ta bort")
        btn_add_c.clicked.connect(self._comp_add)
        btn_ren_c.clicked.connect(self._comp_rename)
        btn_del_c.clicked.connect(self._comp_delete)
        for b in [btn_add_c, btn_ren_c, btn_del_c]:
            comp_btns.addWidget(b)
        left.addLayout(comp_btns)
        layout.addLayout(left, 1)

        # ── Right: failure modes table ────────────────────────────────────────
        right = QVBoxLayout()
        right.addWidget(QLabel("Felmoder för vald komponent:"))

        self._mode_table = QTableWidget(0, 3)
        self._mode_table.setHorizontalHeaderLabels(
            ['Beskrivning', 'Frekvens (/år)', 'F-nivå (auto)'])
        h = self._mode_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._mode_table.setColumnWidth(1, 110)
        self._mode_table.setColumnWidth(2, 90)
        self._mode_table.verticalHeader().setVisible(False)
        self._mode_table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;"
            "font-weight:bold;padding:3px;}")
        self._mode_table.cellChanged.connect(self._on_mode_cell)
        right.addWidget(self._mode_table)

        mode_btns = QHBoxLayout()
        btn_add_m = QPushButton("+ Lägg till felmod")
        btn_del_m = QPushButton("✕ Ta bort vald")
        btn_add_m.clicked.connect(self._mode_add)
        btn_del_m.clicked.connect(self._mode_delete)
        mode_btns.addWidget(btn_add_m)
        mode_btns.addWidget(btn_del_m)
        mode_btns.addStretch()
        right.addLayout(mode_btns)

        freq_note = QLabel(
            "Frekvens i händelser/år.  Exempel: 0.05/år = en gång per 20 år → F=3 (10-100 år)\n"
            "F-nivån beräknas automatiskt från frekvensgränserna i riskmatrisen.")
        freq_note.setStyleSheet("color:#666; font-size:10px;")
        right.addWidget(freq_note)

        layout.addLayout(right, 2)
        self._refresh_comp_list()

    # ── Component list ────────────────────────────────────────────────────────

    def _refresh_comp_list(self):
        self._comp_list.blockSignals(True)
        self._comp_list.clear()
        for ct in self.db.component_types():
            item = QListWidgetItem(ct['name'])
            item.setData(Qt.ItemDataRole.UserRole, ct['id'])
            self._comp_list.addItem(item)
        self._comp_list.blockSignals(False)
        if self._cur_comp_id:
            for i in range(self._comp_list.count()):
                if self._comp_list.item(i).data(Qt.ItemDataRole.UserRole) == self._cur_comp_id:
                    self._comp_list.setCurrentRow(i)
                    break

    def _on_comp_selected(self, current, _prev):
        if current:
            self._cur_comp_id = current.data(Qt.ItemDataRole.UserRole)
            self._refresh_mode_table()
        else:
            self._cur_comp_id = None
            self._mode_table.setRowCount(0)

    def _comp_add(self):
        name, ok = QInputDialog.getText(self, "Ny komponenttyp", "Namn:")
        if ok and name.strip():
            self._cur_comp_id = self.db.add_component_type(name.strip())
            self._refresh_comp_list()

    def _comp_rename(self):
        item = self._comp_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_component_type(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._refresh_comp_list()

    def _comp_delete(self):
        item = self._comp_list.currentItem()
        if not item: return
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort '{item.text()}' och alla dess felmoder?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_component_type(item.data(Qt.ItemDataRole.UserRole))
            self._cur_comp_id = None
            self._refresh_comp_list()
            self._mode_table.setRowCount(0)

    # ── Failure modes table ───────────────────────────────────────────────────

    def _refresh_mode_table(self):
        try:
            self._mode_table.cellChanged.disconnect()
        except Exception:
            pass
        self._mode_table.setRowCount(0)
        if not self._cur_comp_id:
            self._mode_table.cellChanged.connect(self._on_mode_cell)
            return

        for fm in self.db.failure_modes(self._cur_comp_id):
            r = self._mode_table.rowCount()
            self._mode_table.insertRow(r)

            desc = QTableWidgetItem(fm['description'])
            desc.setData(Qt.ItemDataRole.UserRole, fm['id'])
            self._mode_table.setItem(r, 0, desc)

            freq = fm['freq_per_year']
            freq_item = QTableWidgetItem(
                f"{freq:.4g}" if freq is not None else "")
            freq_item.setToolTip("Händelser per år, t.ex. 0.05 (en gång per 20 år)")
            self._mode_table.setItem(r, 1, freq_item)

            f_level = freq_to_f_level(freq) if freq else None
            f_item = QTableWidgetItem(
                f"F={f_level}" if f_level is not None else "—")
            f_item.setFlags(f_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if f_level is not None:
                label, bg, _ = risk_info(f_level, 3)
                f_item.setBackground(QBrush(QColor(bg)))
                f_item.setForeground(QBrush(QColor('#fff')))
                f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._mode_table.setItem(r, 2, f_item)
            self._mode_table.setRowHeight(r, 26)

        self._mode_table.cellChanged.connect(self._on_mode_cell)

    def _mode_add(self):
        if not self._cur_comp_id:
            QMessageBox.information(self, "Välj komponent",
                "Välj en komponenttyp i listan till vänster.")
            return
        self.db.add_failure_mode(self._cur_comp_id, "Ny felmod")
        self._refresh_mode_table()

    def _mode_delete(self):
        rows = {idx.row() for idx in self._mode_table.selectedIndexes()}
        if not rows: return
        for r in sorted(rows, reverse=True):
            item = self._mode_table.item(r, 0)
            if item:
                self.db.delete_failure_mode(item.data(Qt.ItemDataRole.UserRole))
        self._refresh_mode_table()

    def _on_mode_cell(self, row, col):
        item0 = self._mode_table.item(row, 0)
        if not item0: return
        fm_id = item0.data(Qt.ItemDataRole.UserRole)
        desc  = item0.text().strip() or 'Ny felmod'
        freq_item = self._mode_table.item(row, 1)
        freq = None
        if freq_item:
            try:
                freq = float(freq_item.text().strip()) if freq_item.text().strip() else None
            except ValueError:
                freq = None
        self.db.update_failure_mode(fm_id, desc, freq)
        # Update F-level cell
        f_level = freq_to_f_level(freq) if freq else None
        f_item = self._mode_table.item(row, 2)
        if f_item:
            self._mode_table.blockSignals(True)
            f_item.setText(f"F={f_level}" if f_level is not None else "—")
            if f_level is not None:
                _, bg, _ = risk_info(f_level, 3)
                f_item.setBackground(QBrush(QColor(bg)))
                f_item.setForeground(QBrush(QColor('#fff')))
            self._mode_table.blockSignals(False)


class PIDAnalysisPanel(QWidget):
    """Settings panel: shows all tag prefixes found in the P&ID with component-type mapping."""

    # Component types available for selection
    _COMP_TYPES = [
        '', 'Ventil', 'Säkerhetsventil (PSV)', 'Pump', 'Kompressor',
        'Tank / Kärl', 'Värmeväxlare', 'Instrument / Sensor',
        'Rörledning', 'Övrigt',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("Identifierade objekt — P&ID-nyckel")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()

        note = QLabel(
            "Kryssa i 'Använd' för att pre-fylla orsaksmenyn med rätt komponenttyp.")
        note.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(title)
        layout.addWidget(note)

        # Table
        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels(
            ['Prefix', 'Exempeltaggar', 'Databas-förslag', 'Komponenttyp', 'Använd ✓'])
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(0, 70)
        self._tbl.setColumnWidth(2, 180)
        self._tbl.setColumnWidth(3, 160)
        self._tbl.setColumnWidth(4, 70)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._tbl)

        btn_row = QHBoxLayout()
        sel_all = QPushButton("Välj alla")
        sel_all.clicked.connect(lambda: self._bulk_confirm(True))
        desel   = QPushButton("Avmarkera alla")
        desel.clicked.connect(lambda: self._bulk_confirm(False))
        btn_row.addWidget(sel_all); btn_row.addWidget(desel); btn_row.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet("color:#555; font-size:10px;")
        btn_row.addWidget(self._status)
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        self._loading = True
        self._tbl.blockSignals(True)
        self._tbl.setRowCount(0)

        for entry in self.db.pid_identified_tags():
            r = self._tbl.rowCount()
            self._tbl.insertRow(r)

            code_item = QTableWidgetItem(entry['tag_code'])
            code_item.setFlags(code_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            code_item.setFont(QFont('Courier', 10))
            self._tbl.setItem(r, 0, code_item)

            ex_item = QTableWidgetItem(entry['examples'] or '')
            ex_item.setFlags(ex_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ex_item.setForeground(QBrush(QColor('#555555')))
            self._tbl.setItem(r, 1, ex_item)

            # Database suggestion (read-only)
            sugg = QTableWidgetItem(entry['name_sv'] or '—')
            sugg.setFlags(sugg.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sugg.setForeground(QBrush(QColor('#1F4E79')))
            self._tbl.setItem(r, 2, sugg)

            # Editable component type combo
            combo = QComboBox()
            for t in self._COMP_TYPES:
                combo.addItem(t)
            cur = entry['comp_type'] or ''
            idx = combo.findText(cur)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            code = entry['tag_code']
            combo.currentTextChanged.connect(
                lambda text, c=code: self._on_type_changed(c, text))
            self._tbl.setCellWidget(r, 3, combo)

            # "Använd" checkbox
            chk = QTableWidgetItem()
            chk.setCheckState(
                Qt.CheckState.Checked if entry['confirmed'] else Qt.CheckState.Unchecked)
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setData(Qt.ItemDataRole.UserRole, entry['tag_code'])
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._tbl.setItem(r, 4, chk)

            self._tbl.setRowHeight(r, 28)

        self._tbl.cellChanged.connect(self._on_cell_changed)
        self._tbl.blockSignals(False)
        self._loading = False
        self._update_status()

    def _on_type_changed(self, tag_code, comp_type):
        if self._loading:
            return
        # Find confirmed state
        for r in range(self._tbl.rowCount()):
            item = self._tbl.item(r, 0)
            if item and item.text() == tag_code:
                chk = self._tbl.item(r, 4)
                confirmed = chk and chk.checkState() == Qt.CheckState.Checked
                self.db.confirm_pid_tag(tag_code, comp_type, confirmed)
                break

    def _on_cell_changed(self, row, col):
        if self._loading or col != 4:
            return
        chk = self._tbl.item(row, 4)
        if not chk:
            return
        tag_code = chk.data(Qt.ItemDataRole.UserRole)
        confirmed = chk.checkState() == Qt.CheckState.Checked
        combo = self._tbl.cellWidget(row, 3)
        comp_type = combo.currentText() if combo else ''
        self.db.confirm_pid_tag(tag_code, comp_type, confirmed)
        self._update_status()

    def _bulk_confirm(self, confirm: bool):
        self._loading = True
        state = Qt.CheckState.Checked if confirm else Qt.CheckState.Unchecked
        for r in range(self._tbl.rowCount()):
            chk = self._tbl.item(r, 4)
            if chk:
                chk.setCheckState(state)
                tag_code = chk.data(Qt.ItemDataRole.UserRole)
                combo = self._tbl.cellWidget(r, 3)
                comp_type = combo.currentText() if combo else ''
                self.db.confirm_pid_tag(tag_code, comp_type, confirm)
        self._loading = False
        self._update_status()

    def _update_status(self):
        total     = self._tbl.rowCount()
        confirmed = sum(1 for r in range(total)
                        if self._tbl.item(r, 4) and
                        self._tbl.item(r, 4).checkState() == Qt.CheckState.Checked)
        self._status.setText(f"{total} prefix hittade  |  {confirmed} bekräftade")


class TagDatabasePanel(QWidget):
    """Settings panel for managing the P&ID tag-code database."""

    settings_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Tagdatabas — P&ID taggkodnycklar")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        layout.addWidget(title)

        # ── Import section ────────────────────────────────────────────────────
        import_box = QGroupBox("Importera Excel-databas")
        imp_lay = QHBoxLayout(import_box)
        self._excel_lbl = QLabel("Ingen fil vald")
        self._excel_lbl.setStyleSheet("color:#555;")
        imp_lay.addWidget(self._excel_lbl, 1)
        imp_btn = QPushButton("📂 Välj Excel-fil…")
        imp_btn.clicked.connect(self._import_excel)
        imp_lay.addWidget(imp_btn)
        layout.addWidget(import_box)

        # ── Standard selection ────────────────────────────────────────────────
        std_box = QGroupBox("Aktiv standard")
        std_lay = QHBoxLayout(std_box)
        std_lay.addWidget(QLabel("Följ standard:"))
        self._std_combo = QComboBox()
        self._std_combo.addItem("Alla standarder (union)")
        self._std_combo.currentIndexChanged.connect(self._on_std_changed)
        std_lay.addWidget(self._std_combo, 1)
        layout.addWidget(std_box)

        # ── Smart database ────────────────────────────────────────────────────
        smart_box = QGroupBox("Smart databas")
        smart_lay = QVBoxLayout(smart_box)
        self._smart_chk = QCheckBox(
            "Aktivera smart databas — skannar automatiskt inläst P&ID och "
            "identifierar taggar (pump, ventil, instrument…)")
        self._smart_chk.setChecked(
            self.db.tag_db_setting('smart_enabled', '0') == '1')
        self._smart_chk.toggled.connect(self._on_smart_toggled)
        smart_lay.addWidget(self._smart_chk)
        smart_note = QLabel(
            "Identifierade taggar markeras med ljusgul bakgrund på P&ID:n.\n"
            "Definierade orsaker (HAZOP) markeras med ljusgrön bakgrund.")
        smart_note.setStyleSheet("color:#555; font-size:10px;")
        smart_lay.addWidget(smart_note)
        layout.addWidget(smart_box)

        # ── Tag table ─────────────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels(
            ['Taggkod', 'Svensk benämning', 'Engelsk benämning', 'Kategori', 'Standard'])
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._tbl.setColumnWidth(0, 80)
        self._tbl.setColumnWidth(3, 110)
        self._tbl.setColumnWidth(4, 100)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._tbl)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(self._status)

        self._refresh()

    def _refresh(self):
        # Update standard combo
        self._std_combo.blockSignals(True)
        cur = self.db.tag_db_setting('active_standard', '')
        self._std_combo.clear()
        self._std_combo.addItem("Alla standarder (union)", '')
        for std in self.db.tag_database_standards():
            self._std_combo.addItem(std, std)
        idx = self._std_combo.findData(cur)
        if idx >= 0:
            self._std_combo.setCurrentIndex(idx)
        self._std_combo.blockSignals(False)

        # Update table
        entries = self.db.tag_database_entries()
        self._tbl.setRowCount(0)
        for e in entries:
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            for col, val in enumerate([
                    e['tag_code'], e['name_sv'], e['name_en'],
                    e['category'], e['standard']]):
                self._tbl.setItem(r, col, QTableWidgetItem(val or ''))
            self._tbl.setRowHeight(r, 22)

        n = len(entries)
        stds = self.db.tag_database_standards()
        self._status.setText(
            f"{n} taggkoder  |  {len(stds)} standarder: {', '.join(stds)}")

    def _import_excel(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Välj Excel-databas", "", "Excel (*.xlsx *.xls)")
        if not path:
            return
        n, err = self.db.import_tag_database_excel(path)
        if err:
            QMessageBox.critical(self, "Importfel", err)
        else:
            QMessageBox.information(self, "Importerat",
                f"{n} taggkoder importerade från\n{path}")
            self._excel_lbl.setText(path)
            self._refresh()
            self.settings_changed.emit()

    def _on_std_changed(self):
        std = self._std_combo.currentData() or ''
        self.db.set_tag_db_setting('active_standard', std)
        self.settings_changed.emit()

    def _on_smart_toggled(self, checked):
        self.db.set_tag_db_setting('smart_enabled', '1' if checked else '0')
        self.settings_changed.emit()


_PALETTE_MIME = 'application/x-hazop-palette-color'


class DraggableColorSwatch(QLabel):
    """Draggable color swatch in the palette — drag onto a matrix cell."""

    def __init__(self, name: str, color: str, fg_color: str = None, parent=None):
        super().__init__(name, parent)
        self._name     = name
        self._color    = color
        self._fg_color = fg_color  # None = auto-calculated from luminance
        self.setFixedSize(76, 28)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._refresh()

    def _refresh(self):
        r, g, b = int(self._color[1:3], 16), int(self._color[3:5], 16), int(self._color[5:7], 16)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        auto_txt = '#000' if lum > 160 else '#fff'
        txt = self._fg_color if self._fg_color else auto_txt
        self.setStyleSheet(
            f"background:{self._color}; color:{txt}; font-weight:bold; font-size:10px;"
            f"border:1px solid #555; border-radius:4px;")
        self.setText(self._name)

    def set_swatch(self, name: str, color: str, fg_color: str = None):
        self._name = name; self._color = color; self._fg_color = fg_color
        self._refresh()

    def name(self):     return self._name
    def color(self):    return self._color
    def fg_color(self): return self._fg_color

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_PALETTE_MIME,
                         json.dumps({'color': self._color, 'name': self._name,
                                     'fg_color': self._fg_color or '#ffffff'}).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(event.position().toPoint())
            drag.exec(Qt.DropAction.CopyAction)
        else:
            super().mousePressEvent(event)


class MatrixCellButton(QPushButton):
    """Risk matrix cell — collapsed-border grid (no double-lines between cells)."""

    def __init__(self, row, col, color, label, fg_color='#ffffff',
                 is_top_row=False, is_left_col=False, parent=None):
        super().__init__(label, parent)
        self.row = row
        self.col = col
        self._color    = color
        self._fg_color = fg_color
        self._label    = label
        self._is_top   = is_top_row
        self._is_left  = is_left_col
        self.setFixedSize(80, 40)
        self.setAcceptDrops(True)
        self._apply_style()

    def _apply_style(self):
        top  = "border-top:1px solid #444;"  if self._is_top  else ""
        left = "border-left:1px solid #444;" if self._is_left else ""
        self.setStyleSheet(
            f"QPushButton{{"
            f"background:{self._color}; color:{self._fg_color}; font-weight:bold;"
            f"border-bottom:1px solid #444; border-right:1px solid #444;"
            f"{top}{left}"
            f"border-radius:0px; margin:0px; padding:0px;}}"
            f"QPushButton:hover{{border:2px solid #000; margin:-1px;}}")
        self.setText(self._label)

    def set_cell(self, color, label=None, fg_color=None):
        self._color = color
        if label is not None:
            self._label = label
        if fg_color is not None:
            self._fg_color = fg_color
        self._apply_style()

    def color(self):    return self._color
    def label(self):    return self._label
    def fg_color(self): return self._fg_color

    # ── Drag-and-drop ─────────────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            self.setStyleSheet(
                f"background:{self._color}; color:white; font-weight:bold;"
                f"border:3px dashed #000;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._apply_style()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            data = json.loads(
                event.mimeData().data(_PALETTE_MIME).data().decode())
            self.set_cell(data['color'], data['name'], data.get('fg_color', '#ffffff'))
            event.acceptProposedAction()
        else:
            event.ignore()


class StandardCausesSettingsPanel(QWidget):
    """Editable library of standard deviations and their template causes."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QHBoxLayout(self)

        # ── Left: standard deviations list ───────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Standardavvikelser</b>"))
        self._dev_list = QListWidget()
        self._dev_list.currentRowChanged.connect(self._on_dev_selected)
        left.addWidget(self._dev_list)
        dev_btns = QHBoxLayout()
        btn_add_dev = QPushButton("+")
        btn_add_dev.setFixedWidth(28)
        btn_add_dev.clicked.connect(self._add_deviation)
        btn_del_dev = QPushButton("−")
        btn_del_dev.setFixedWidth(28)
        btn_del_dev.clicked.connect(self._del_deviation)
        btn_up_dev = QPushButton("↑")
        btn_up_dev.setFixedWidth(28)
        btn_up_dev.clicked.connect(lambda: self._move_deviation(-1))
        btn_dn_dev = QPushButton("↓")
        btn_dn_dev.setFixedWidth(28)
        btn_dn_dev.clicked.connect(lambda: self._move_deviation(1))
        for b in (btn_add_dev, btn_del_dev, btn_up_dev, btn_dn_dev):
            dev_btns.addWidget(b)
        dev_btns.addStretch()
        left.addLayout(dev_btns)

        # ── Right: standard causes for selected deviation ─────────────────────
        right = QVBoxLayout()
        self._causes_label = QLabel("<b>Standardorsaker</b>")
        right.addWidget(self._causes_label)

        self._cause_table = QTableWidget(0, 3)
        self._cause_table.setHorizontalHeaderLabels(["Beskrivning", "Frekvens/år", "F-nivå"])
        chdr = self._cause_table.horizontalHeader()
        chdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        chdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        chdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._cause_table.setColumnWidth(1, 90)
        self._cause_table.setColumnWidth(2, 72)
        self._cause_table.verticalHeader().setVisible(False)
        self._cause_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._cause_table.itemChanged.connect(self._on_cause_cell_changed)
        right.addWidget(self._cause_table)

        cause_btns = QHBoxLayout()
        btn_add_c = QPushButton("+")
        btn_add_c.setFixedWidth(28)
        btn_add_c.clicked.connect(self._add_cause)
        btn_del_c = QPushButton("−")
        btn_del_c.setFixedWidth(28)
        btn_del_c.clicked.connect(self._del_cause)
        btn_up_c = QPushButton("↑")
        btn_up_c.setFixedWidth(28)
        btn_up_c.clicked.connect(lambda: self._move_cause(-1))
        btn_dn_c = QPushButton("↓")
        btn_dn_c.setFixedWidth(28)
        btn_dn_c.clicked.connect(lambda: self._move_cause(1))
        for b in (btn_add_c, btn_del_c, btn_up_c, btn_dn_c):
            cause_btns.addWidget(b)
        cause_btns.addStretch()
        right.addLayout(cause_btns)

        btn_sync = QPushButton("Synkronisera frekvenser → orsaker")
        btn_sync.setToolTip(
            "Skriver över frekvensen på alla orsaker som skapades från standardorsaker "
            "med det aktuella värdet i denna lista.\n"
            "Orsaker utan koppling till standardorsak påverkas inte.\n"
            "Standardorsaker utan frekvens påverkas inte.")
        btn_sync.clicked.connect(self._sync_freqs)
        right.addWidget(btn_sync)

        layout.addLayout(left, 1)
        layout.addLayout(right, 2)

        self._loading = False
        self._load_deviations()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _load_deviations(self):
        self._loading = True
        cur_row = self._dev_list.currentRow()
        self._dev_list.clear()
        for dev in self.db.standard_deviations():
            item = QListWidgetItem(dev['description'])
            item.setData(Qt.ItemDataRole.UserRole, dev['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._dev_list.addItem(item)
        self._loading = False
        if cur_row >= 0:
            self._dev_list.setCurrentRow(min(cur_row, self._dev_list.count() - 1))
        elif self._dev_list.count():
            self._dev_list.setCurrentRow(0)

    @staticmethod
    def _f_label(freq):
        """Return the risk matrix frequency label for a given frequency (or '—')."""
        if freq is None:
            return "—"
        return freq_axis_label(freq_to_f_level(freq))

    def _load_causes(self, dev_id):
        self._loading = True
        self._cause_table.setRowCount(0)
        for c in self.db.standard_causes(dev_id):
            cd   = dict(c)
            comp = cd.get('comp_type', '') or ''
            freq = cd.get('frequency')
            label = f"[{comp}]  {cd['description']}" if comp else cd['description']

            row = self._cause_table.rowCount()
            self._cause_table.insertRow(row)

            item0 = QTableWidgetItem(label)
            item0.setData(Qt.ItemDataRole.UserRole, cd['id'])
            if comp:
                item0.setForeground(QColor('#1F4E79'))
            self._cause_table.setItem(row, 0, item0)

            item1 = QTableWidgetItem("" if freq is None else f"{freq:g}")
            item1.setData(Qt.ItemDataRole.UserRole, cd['id'])
            item1.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._cause_table.setItem(row, 1, item1)

            item2 = QTableWidgetItem(self._f_label(freq))
            item2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item2.setFlags(item2.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._cause_table.setItem(row, 2, item2)
        self._loading = False

    def _current_dev_id(self):
        item = self._dev_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _current_cause_id(self):
        row = self._cause_table.currentRow()
        if row < 0:
            return None
        item = self._cause_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # ── Deviation slots ───────────────────────────────────────────────────────
    def _on_dev_selected(self, row):
        item = self._dev_list.item(row)
        if item:
            dev_id = item.data(Qt.ItemDataRole.UserRole)
            self._causes_label.setText(f"<b>Standardorsaker — {item.text()}</b>")
            self._load_causes(dev_id)
        else:
            self._cause_list.clear()

    def _add_deviation(self):
        name, ok = QInputDialog.getText(self, "Ny avvikelse", "Namn:")
        if not ok or not name.strip():
            return
        self.db.add_standard_deviation(name.strip())
        self._load_deviations()
        self._dev_list.setCurrentRow(self._dev_list.count() - 1)

    def _del_deviation(self):
        dev_id = self._current_dev_id()
        if dev_id is None:
            return
        if QMessageBox.question(self, "Ta bort",
                "Ta bort avvikelsen och alla dess standardorsaker?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_standard_deviation(dev_id)
        self._load_deviations()
        self._cause_table.setRowCount(0)

    def _move_deviation(self, direction):
        row = self._dev_list.currentRow()
        new_row = row + direction
        if new_row < 0 or new_row >= self._dev_list.count():
            return
        ids = [self._dev_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._dev_list.count())]
        ids[row], ids[new_row] = ids[new_row], ids[row]
        self.db.reorder_standard_deviations(ids)
        self._load_deviations()
        self._dev_list.setCurrentRow(new_row)

    # ── Cause slots ───────────────────────────────────────────────────────────
    def _on_cause_cell_changed(self, item):
        if self._loading:
            return
        col = item.column()
        cid = item.data(Qt.ItemDataRole.UserRole)
        if not cid:
            return
        if col == 0:
            text = item.text().strip()
            if ']  ' in text:   # strip "[comp_type]  " prefix
                text = text.split(']  ', 1)[1]
            if text:
                self.db.update_standard_cause(cid, description=text)
        elif col == 1:
            raw = item.text().strip().replace(',', '.')
            try:
                freq = float(raw) if raw else None
            except ValueError:
                freq = None
            self.db.update_standard_cause(cid, frequency=freq)
            # Refresh F-nivå column (col 2) in same row
            self._loading = True
            item2 = self._cause_table.item(item.row(), 2)
            if item2 is None:
                item2 = QTableWidgetItem()
                item2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item2.setFlags(item2.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._cause_table.setItem(item.row(), 2, item2)
            item2.setText(self._f_label(freq))
            self._loading = False

    def _add_cause(self):
        dev_id = self._current_dev_id()
        if dev_id is None:
            return
        name, ok = QInputDialog.getText(self, "Ny standardorsak", "Beskrivning:")
        if not ok or not name.strip():
            return
        self.db.add_standard_cause(dev_id, name.strip())
        self._load_causes(dev_id)
        self._cause_table.setCurrentCell(self._cause_table.rowCount() - 1, 0)

    def _del_cause(self):
        cid = self._current_cause_id()
        if cid is None:
            return
        self.db.delete_standard_cause(cid)
        dev_id = self._current_dev_id()
        if dev_id:
            self._load_causes(dev_id)

    def _move_cause(self, direction):
        row = self._cause_table.currentRow()
        new_row = row + direction
        if new_row < 0 or new_row >= self._cause_table.rowCount():
            return
        ids = [self._cause_table.item(i, 0).data(Qt.ItemDataRole.UserRole)
               for i in range(self._cause_table.rowCount())]
        ids[row], ids[new_row] = ids[new_row], ids[row]
        self.db.reorder_standard_causes(ids)
        dev_id = self._current_dev_id()
        if dev_id:
            self._load_causes(dev_id)
        self._cause_table.setCurrentCell(new_row, 0)

    def _sync_freqs(self):
        ret = QMessageBox.warning(
            self, "Synkronisera frekvenser",
            "Alla orsaker som skapades från standardorsaker kommer att få sin frekvens "
            "uppdaterad till det aktuella värdet i standardorsakslistan.\n\n"
            "Orsaker utan koppling till standardorsak påverkas inte.\n"
            "Standardorsaker utan frekvens påverkas inte.\n\n"
            "Vill du fortsätta?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            n = self.db.update_cause_freqs_from_standard()
            QMessageBox.information(self, "Klart", f"{n} orsak(er) uppdaterades.")


class ComponentCausesSettingsPanel(QWidget):
    """Edit standard causes grouped by component type (felmoder per komponent)."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False

        layout = QHBoxLayout(self)

        # ── Left: component type list ─────────────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Komponenttyp</b>"))
        self._type_list = QListWidget()
        self._type_list.currentRowChanged.connect(self._on_type_selected)
        left.addWidget(self._type_list)
        layout.addLayout(left, 1)

        # ── Right: causes for selected component type ─────────────────────────
        right = QVBoxLayout()
        self._causes_label = QLabel("<b>Felmoder</b>")
        right.addWidget(self._causes_label)

        self._cause_tbl = QTableWidget(0, 2)
        self._cause_tbl.setHorizontalHeaderLabels(["Avvikelse", "Orsak / Felmod"])
        self._cause_tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive)
        self._cause_tbl.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._cause_tbl.setColumnWidth(0, 160)
        self._cause_tbl.verticalHeader().setVisible(False)
        self._cause_tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._cause_tbl.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        self._cause_tbl.itemChanged.connect(self._on_cause_edited)
        right.addWidget(self._cause_tbl)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("+ Lägg till felmod")
        btn_add.clicked.connect(self._add_cause)
        btn_del = QPushButton("− Ta bort")
        btn_del.clicked.connect(self._del_cause)
        for b in (btn_add, btn_del):
            btn_row.addWidget(b)
        btn_row.addStretch()
        right.addLayout(btn_row)
        layout.addLayout(right, 3)

        self._load_types()

    def _load_types(self):
        cur = self._type_list.currentRow()
        self._type_list.clear()
        for ct in self.db.distinct_comp_types():
            self._type_list.addItem(ct)
        if cur >= 0:
            self._type_list.setCurrentRow(min(cur, self._type_list.count() - 1))
        elif self._type_list.count():
            self._type_list.setCurrentRow(0)

    def _current_comp_type(self):
        item = self._type_list.currentItem()
        return item.text() if item else None

    def _on_type_selected(self, _row):
        ct = self._current_comp_type()
        if ct:
            self._causes_label.setText(f"<b>Felmoder — {ct}</b>")
            self._load_causes(ct)
        else:
            self._cause_tbl.setRowCount(0)

    def _load_causes(self, comp_type):
        self._loading = True
        self._cause_tbl.blockSignals(True)
        self._cause_tbl.setRowCount(0)
        for c in self.db.standard_causes_for_comp_type(comp_type):
            r = self._cause_tbl.rowCount()
            self._cause_tbl.insertRow(r)
            dev_item = QTableWidgetItem(c['deviation_name'])
            dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            dev_item.setForeground(QColor('#555'))
            dev_item.setData(Qt.ItemDataRole.UserRole, c['id'])
            dev_item.setData(Qt.ItemDataRole.UserRole + 1, c['deviation_id'])
            self._cause_tbl.setItem(r, 0, dev_item)
            desc_item = QTableWidgetItem(c['description'])
            desc_item.setData(Qt.ItemDataRole.UserRole, c['id'])
            self._cause_tbl.setItem(r, 1, desc_item)
        self._cause_tbl.blockSignals(False)
        self._loading = False

    def _on_cause_edited(self, item):
        if self._loading or item.column() != 1:
            return
        cid = item.data(Qt.ItemDataRole.UserRole)
        if cid:
            self.db.update_standard_cause(cid, item.text())

    def _add_cause(self):
        ct = self._current_comp_type()
        if not ct:
            return
        devs = self.db.standard_deviations()
        if not devs:
            QMessageBox.information(self, "Inga avvikelser",
                "Lägg till standardavvikelser under fliken Standardorsaker först.")
            return
        dev_names = [d['description'] for d in devs]
        dev_name, ok = QInputDialog.getItem(
            self, "Välj avvikelse", "Avvikelse:", dev_names, 0, False)
        if not ok:
            return
        desc, ok2 = QInputDialog.getText(
            self, "Ny felmod", f"Orsak / felmod för {ct}:")
        if not ok2 or not desc.strip():
            return
        dev_row = next((d for d in devs if d['description'] == dev_name), None)
        if not dev_row:
            return
        self.db.add_standard_cause_for_comp_type(dev_row['id'], desc.strip(), ct)
        self._load_causes(ct)

    def _del_cause(self):
        row = self._cause_tbl.currentRow()
        if row < 0:
            return
        cid = self._cause_tbl.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if cid is None:
            return
        self.db.delete_standard_cause(cid)
        ct = self._current_comp_type()
        if ct:
            self._load_causes(ct)

    def refresh(self):
        self._load_types()


class SettingsPanel(QWidget):
    matrix_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cell_buttons   = []
        self._x_label_edits  = []   # QLineEdit per column
        self._y_label_edits  = []   # QLineEdit per row (high→low)
        self._palette_swatches = []

        tabs = QTabWidget()
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: Riskmatris ───────────────────────────────────────────────────
        matrix_tab = QWidget()
        ml = QVBoxLayout(matrix_tab)
        ml.setSpacing(6)

        # Size row
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Konsekvens-fält:"))
        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(2, 15)
        self._rows_spin.setValue(5)
        self._rows_spin.setToolTip("Antal nivåer på konsekvens-axeln (C1…Cn)")
        size_row.addWidget(self._rows_spin)

        size_row.addWidget(QLabel("  Frekvens-fält:"))
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(2, 15)
        self._cols_spin.setValue(7)
        self._cols_spin.setToolTip("Antal nivåer på frekvens-axeln (F-1…Fn)")
        size_row.addWidget(self._cols_spin)
        size_row.addStretch()
        ml.addLayout(size_row)

        # ── Colour palette ────────────────────────────────────────────────────
        pal_box = QGroupBox("Färgpalett — dra en färg och släpp på en cell")
        pal_lay = QHBoxLayout(pal_box)
        pal_lay.setSpacing(4)
        self._palette_container = pal_lay

        add_col_btn = QPushButton("+ Lägg till")
        add_col_btn.setFixedHeight(28)
        add_col_btn.clicked.connect(self._palette_add)
        pal_lay.addWidget(add_col_btn)

        edit_col_btn = QPushButton("✎ Redigera")
        edit_col_btn.setFixedHeight(28)
        edit_col_btn.clicked.connect(self._palette_edit)
        pal_lay.addWidget(edit_col_btn)

        del_col_btn = QPushButton("✕ Ta bort")
        del_col_btn.setFixedHeight(28)
        del_col_btn.clicked.connect(self._palette_delete)
        pal_lay.addWidget(del_col_btn)

        pal_lay.addStretch()
        ml.addWidget(pal_box)

        # ── Matrix grid ───────────────────────────────────────────────────────
        # Use a wrapper so matrix stays at natural size (top-left) while the
        # scroll area fills remaining space with the stretch below it.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        _wrap = QWidget()
        _wrap_lay = QVBoxLayout(_wrap)
        _wrap_lay.setContentsMargins(0, 0, 0, 0)
        _wrap_lay.setSpacing(0)

        self._matrix_container = QWidget()
        self._matrix_grid = QGridLayout(self._matrix_container)
        self._matrix_grid.setSpacing(0)
        self._matrix_grid.setContentsMargins(0, 0, 0, 0)

        _wrap_lay.addWidget(self._matrix_container,
                            alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _wrap_lay.addStretch(1)
        scroll.setWidget(_wrap)
        ml.addWidget(scroll)

        # Axis orientation + direction controls
        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axlar:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItem("Frekvens → X,  Konsekvens → Y  (standard)", 'frequency')
        self._axis_combo.addItem("Konsekvens → X,  Frekvens → Y", 'consequence')
        ax_row.addWidget(self._axis_combo, 1)
        ax_row.addWidget(QLabel("  Riktning:"))
        self._x_rev_chk = QCheckBox("Vänd X ←")
        self._x_rev_chk.setToolTip("Vänd X-axeln: hög värde till vänster")
        self._y_rev_chk = QCheckBox("Vänd Y ↓")
        self._y_rev_chk.setToolTip("Vänd Y-axeln: lägst upp, högst ner")
        ax_row.addWidget(self._x_rev_chk)
        ax_row.addWidget(self._y_rev_chk)
        ml.addLayout(ax_row)

        # Live update: rebuild grid immediately on any control change
        self._axis_combo.currentIndexChanged.connect(self._apply_size)
        self._x_rev_chk.toggled.connect(self._apply_size)
        self._y_rev_chk.toggled.connect(self._apply_size)
        self._rows_spin.valueChanged.connect(self._apply_size)
        self._cols_spin.valueChanged.connect(self._apply_size)

        # Frequency label presets
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Frekvens-mall:"))
        norsok_btn = QPushButton("NORSOK Z-013  (AAA – E)")
        norsok_btn.setToolTip(
            "Fyll frekvensaxeln med NORSOK Z-013-etiketter:\n"
            "AAA (< 10⁻⁵/år)  →  E (> 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        norsok_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['AAA', 'AA', 'A', 'B', 'C', 'D', 'E'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        fscale_btn = QPushButton("F-skala  (F-1 – F5)")
        fscale_btn.setToolTip(
            "Fyll frekvensaxeln med internt F-skaleetiketter:\n"
            "F-1 (Otänkbar)  →  F5 (Frekvent > 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        fscale_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['F-1 – Otänkbar', 'F0 – Extremt sällan', 'F1 – Sällan',
             'F2 – Osannolik', 'F3 – Möjlig', 'F4 – Trolig', 'F5 – Frekvent'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        preset_row.addWidget(norsok_btn)
        preset_row.addWidget(fscale_btn)
        preset_row.addStretch()
        ml.addLayout(preset_row)

        save_matrix_btn = QPushButton("💾 Spara riskmatris")
        save_matrix_btn.setStyleSheet(
            "background:#1F4E79; color:#fff; font-weight:bold; padding:4px 12px;")
        save_matrix_btn.clicked.connect(self._save_matrix)
        ml.addWidget(save_matrix_btn)
        tabs.addTab(matrix_tab, "Riskmatris")

        # ── Tab: Kategorier ───────────────────────────────────────────────────
        cat_tab = QWidget()
        cl = QVBoxLayout(cat_tab)
        cl.addWidget(QLabel("Konsekvensskategorier:"))
        self._cat_list = QListWidget()
        cl.addWidget(self._cat_list)
        cat_btns = QHBoxLayout()
        btn_add  = QPushButton("+ Lägg till")
        btn_ren  = QPushButton("Byt namn")
        btn_del  = QPushButton("Ta bort")
        btn_add.clicked.connect(self._cat_add)
        btn_ren.clicked.connect(self._cat_rename)
        btn_del.clicked.connect(self._cat_delete)
        for b in [btn_add, btn_ren, btn_del]: cat_btns.addWidget(b)
        cl.addLayout(cat_btns)
        cl.addStretch()
        tabs.addTab(cat_tab, "Kategorier")

        # ── Tab: Projekt ──────────────────────────────────────────────────────
        proj_tab = QWidget()
        pl = QFormLayout(proj_tab)
        pl.setSpacing(10)
        pl.setContentsMargins(16, 16, 16, 16)

        self._proj_name = QLineEdit()
        self._proj_name.editingFinished.connect(
            lambda: self.db.set_config('project_name', self._proj_name.text()))
        pl.addRow("Projektnamn:", self._proj_name)

        self._proj_date = QLineEdit()
        self._proj_date.editingFinished.connect(
            lambda: self.db.set_config('project_date', self._proj_date.text()))
        pl.addRow("Datum:", self._proj_date)

        self._proj_rev = QLineEdit()
        self._proj_rev.editingFinished.connect(
            lambda: self.db.set_config('project_revision', self._proj_rev.text()))
        pl.addRow("Revision:", self._proj_rev)
        tabs.addTab(proj_tab, "Projekt")

        # ── Tab: Tagdatabas ───────────────────────────────────────────────────
        self._tag_db_panel = TagDatabasePanel(self.db)
        self._tag_db_panel.settings_changed.connect(self.matrix_changed.emit)
        tabs.addTab(self._tag_db_panel, "Tagdatabas")

        # ── Tab: Identifierade objekt ─────────────────────────────────────────
        self.analysis_panel = PIDAnalysisPanel(self.db)
        tabs.addTab(self.analysis_panel, "Identifierade objekt")

        # ── Tab: Standardavvikelser & Orsaker ─────────────────────────────────
        self._std_causes_panel = StandardCausesSettingsPanel(self.db)
        tabs.addTab(self._std_causes_panel, "Standardorsaker")

        # ── Tab: Felmoder per komponent ────────────────────────────────────────
        self._comp_causes_panel = ComponentCausesSettingsPanel(self.db)
        tabs.addTab(self._comp_causes_panel, "Felmoder")

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_palette_ui()
        self._load_categories()
        self._proj_name.setText(self.db.get_config('project_name', ''))
        self._proj_date.setText(self.db.get_config('project_date', ''))
        self._proj_rev.setText(self.db.get_config('project_revision', ''))

    # ── Palette ───────────────────────────────────────────────────────────────

    def _load_palette_ui(self):
        # Remove existing swatches (keep the 3 buttons at end)
        for sw in self._palette_swatches:
            self._palette_container.removeWidget(sw)
            sw.deleteLater()
        self._palette_swatches = []
        palette = self.db.get_color_palette()
        for entry in palette:
            sw = DraggableColorSwatch(entry['name'], entry['color'], entry.get('fg_color'))
            # Insert before the "Lägg till / Redigera / Ta bort" buttons
            insert_pos = self._palette_container.count() - 4
            self._palette_container.insertWidget(max(0, insert_pos), sw)
            self._palette_swatches.append(sw)

    def _palette_add(self):
        name, ok = QInputDialog.getText(self, "Ny palettefärg", "Namn (t.ex. Kritisk):")
        if not ok or not name.strip():
            return
        color = QColorDialog.getColor(QColor('#e74c3c'), self, "Välj bakgrundsfärg")
        if not color.isValid():
            return
        # Auto-calculate fg and let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        fg_color_obj = QColorDialog.getColor(QColor(auto_fg), self, "Välj textfärg (auto-föreslagen)")
        fg = fg_color_obj.name() if fg_color_obj.isValid() else auto_fg
        palette = self.db.get_color_palette()
        palette.append({'name': name.strip(), 'color': color.name(), 'fg_color': fg})
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_edit(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Redigera", "Välj färg:", names, 0, False)
        if not ok:
            return
        idx = names.index(chosen)
        new_name, ok2 = QInputDialog.getText(self, "Nytt namn", "Namn:", text=chosen)
        if not ok2:
            return
        new_color = QColorDialog.getColor(QColor(palette[idx]['color']), self, "Välj färg")
        if not new_color.isValid():
            return
        # Ask for text color too
        old_fg = palette[idx].get('fg_color', '#ffffff')
        fg_color_obj = QColorDialog.getColor(QColor(old_fg), self, "Välj textfärg")
        new_fg = fg_color_obj.name() if fg_color_obj.isValid() else old_fg
        palette[idx] = {'name': new_name.strip() or chosen, 'color': new_color.name(), 'fg_color': new_fg}
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_delete(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Ta bort", "Välj färg att ta bort:", names, 0, False)
        if not ok:
            return
        palette = [e for e in palette if e['name'] != chosen]
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    # ── Matrix ────────────────────────────────────────────────────────────────

    def _load_matrix_ui(self):
        cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        self._rows_spin.setValue(cfg.get('rows', 5))
        self._cols_spin.setValue(cfg.get('cols', 7))
        x_axis = cfg.get('x_axis', 'frequency')
        idx = self._axis_combo.findData(x_axis)
        if idx >= 0:
            self._axis_combo.setCurrentIndex(idx)
        self._x_rev_chk.setChecked(bool(cfg.get('x_reversed', False)))
        self._y_rev_chk.setChecked(bool(cfg.get('y_reversed', False)))
        self._build_matrix_grid(cfg)

    def _apply_size(self):
        """Rebuild the matrix grid. Handles axis swap without losing data."""
        n_cons    = self._rows_spin.value()
        n_freq    = self._cols_spin.value()
        old       = self.db.get_risk_matrix() or DEFAULT_MATRIX
        old_xaxis = old.get('x_axis', 'frequency')
        new_xaxis = self._axis_combo.currentData() or 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()

        # ── Recover labels from the current display widgets ───────────────────
        # _x_label_edits = column headers (display left→right)
        # _y_label_edits = row headers    (display top→bottom)
        if self._x_label_edits and self._y_label_edits:
            col_lbls = [e.text().strip() for e in self._x_label_edits]
            row_lbls = [e.text().strip() for e in self._y_label_edits]  # top→bottom

            # Which semantic axis was on X/Y in the old display?
            if old_xaxis == 'frequency':
                # cols = freq (low→high or high→low depending on old x_rev)
                freq_lbls = col_lbls if not old.get('x_reversed', False) \
                            else list(reversed(col_lbls))
                # rows = cons (high→low at top unless old y_rev)
                cons_lbls = list(reversed(row_lbls)) if not old.get('y_reversed', False) \
                            else row_lbls
            else:
                # cols = cons, rows = freq
                cons_lbls = col_lbls if not old.get('x_reversed', False) \
                            else list(reversed(col_lbls))
                freq_lbls = list(reversed(row_lbls)) if not old.get('y_reversed', False) \
                            else row_lbls
        else:
            freq_lbls = old.get('x_labels', _FREQ_LABELS[:n_freq])
            cons_lbls = old.get('y_labels', _SEV_LABELS[:n_cons])

        # Pad/trim to new dimensions
        while len(freq_lbls) < n_freq:
            freq_lbls.append(f'F{len(freq_lbls)-1}')
        while len(cons_lbls) < n_cons:
            cons_lbls.append(f'C{len(cons_lbls)+1}')
        freq_lbls = freq_lbls[:n_freq]
        cons_lbls = cons_lbls[:n_cons]

        # ── Cell data: current buttons override DB values ─────────────────────
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        lbl2d     = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        # 1. Fill from DB
        old_c  = old.get('cell_colors', [])
        old_l  = old.get('cell_labels', [])
        old_fg = old.get('cell_fg_colors', [])
        for ci in range(n_cons):
            for fi in range(n_freq):
                try:    colors[ci][fi]    = old_c[ci][fi]  or '#27ae60'
                except: colors[ci][fi]    = '#27ae60'
                try:    lbl2d[ci][fi]     = old_l[ci][fi]  or 'Låg'
                except: lbl2d[ci][fi]     = 'Låg'
                try:    fg_colors[ci][fi] = old_fg[ci][fi] or '#ffffff'
                except: fg_colors[ci][fi] = '#ffffff'
        # 2. Override with any user edits in the current buttons
        for _dr, row_btns in self._cell_buttons:
            for btn in row_btns:
                ci, fi = btn.row, btn.col
                if ci < n_cons and fi < n_freq:
                    if btn.color():    colors[ci][fi]    = btn.color()
                    if btn.label():    lbl2d[ci][fi]     = btn.label()
                    if btn.fg_color(): fg_colors[ci][fi] = btn.fg_color()

        new_cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':         new_xaxis,
            'x_reversed':     x_rev,
            'y_reversed':     y_rev,
            'x_labels':       freq_lbls,   # ALWAYS stores frequency labels
            'y_labels':       cons_lbls,   # ALWAYS stores consequence labels
            'cell_colors':    colors,
            'cell_labels':    lbl2d,
            'cell_fg_colors': fg_colors,
            'freq_boundaries': old.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES),
        }
        self._build_matrix_grid(new_cfg)

    def _build_matrix_grid(self, cfg):
        """Build the matrix grid respecting axis orientation and intervals."""
        while self._matrix_grid.count():
            item = self._matrix_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cell_buttons       = []
        self._x_label_edits      = []
        self._y_label_edits      = []
        self._freq_boundary_edits = []

        # Data always stored as [consequence_idx][frequency_idx]
        n_cons = cfg.get('rows', 5)    # consequence levels
        n_freq = cfg.get('cols', 7)    # frequency levels
        freq_labels = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        cons_labels = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors          = cfg.get('cell_colors',    [['#27ae60'] * n_freq] * n_cons)
        cell_labels     = cfg.get('cell_labels',    [['Låg']     * n_freq] * n_cons)
        cell_fg_colors  = cfg.get('cell_fg_colors', [['#ffffff'] * n_freq] * n_cons)
        boundaries  = list(cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES))

        x_axis    = cfg.get('x_axis', 'frequency')
        freq_on_x = (x_axis == 'frequency')
        x_rev     = cfg.get('x_reversed', False)   # True = high value on left/top of X
        y_rev     = cfg.get('y_reversed', False)   # True = low value at top of Y

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons   # cols=freq, rows=cons
            col_lbls, row_lbls = freq_labels, cons_labels
            corner_txt = "C \\ F"
            col_tip = "Frekvensetikett (X-axel)\nExempel: F3 – Möjlig | 10-100 år"
            row_tip = "Konsekvensnivå (Y-axel)\nExempel: C4 – Allvarlig"
        else:
            n_dcols, n_drows = n_cons, n_freq   # cols=cons, rows=freq
            col_lbls, row_lbls = cons_labels, freq_labels
            corner_txt = "F \\ C"
            col_tip = "Konsekvensnivå (X-axel)\nExempel: C4 – Allvarlig"
            row_tip = "Frekvensetikett (Y-axel)\nExempel: F3 – Möjlig | 10-100 år"

        _hdr_style = ("font-size:8px; font-weight:bold;"
                      "border:1px solid #aaa; border-radius:0px;"
                      "background:#eef2f7; padding:0 3px;")

        # Corner
        corner = QLabel(corner_txt)
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setStyleSheet("font-size:9px; color:#555;")
        self._matrix_grid.addWidget(corner, 0, 0)

        # Column headers — apply x_rev: if reversed, col 0 shows the highest value
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            txt = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            e = QLineEdit(txt)
            e.setFixedSize(80, 28)
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e.setStyleSheet(_hdr_style)
            e.setToolTip(col_tip + "\nEtiketten uppdateras automatiskt när du ändrar gränsvärdet.")
            self._matrix_grid.addWidget(e, 0, c + 1)
            self._x_label_edits.append(e)

        # Rows — apply y_rev: if NOT reversed, highest value is at top (default)
        for r in range(n_drows):
            if y_rev:
                disp_r = r              # low at top (r=0 = lowest value)
            else:
                disp_r = n_drows - 1 - r  # high at top (default)

            # Row header
            txt = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            ey = QLineEdit(txt)
            ey.setFixedSize(90, 40)
            ey.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ey.setStyleSheet(_hdr_style)
            ey.setToolTip(row_tip)
            self._matrix_grid.addWidget(ey, r + 1, 0)
            self._y_label_edits.append(ey)   # index 0 = top row

            row_btns = []
            for c in range(n_dcols):
                # Resolve display column to data column (accounting for x_rev)
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map display → data (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx = disp_r
                    freq_idx = data_c
                else:
                    freq_idx = disp_r
                    cons_idx = data_c

                try: cc = colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cc = '#27ae60'
                try: cl = cell_labels[cons_idx][freq_idx]
                except (IndexError, KeyError): cl = 'Låg'
                try: cf = cell_fg_colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cf = '#ffffff'

                btn = MatrixCellButton(cons_idx, freq_idx, cc, cl, cf,
                                       is_top_row=(r == 0),
                                       is_left_col=(c == 0))
                btn.clicked.connect(lambda _, b=btn: self._edit_cell(b))
                self._matrix_grid.addWidget(btn, r + 1, c + 1)
                row_btns.append(btn)
            self._cell_buttons.append((disp_r, row_btns))

        # ── Interval / boundary row below cells ───────────────────────────────
        # Only shown when frequency is on X-axis (boundaries are per-frequency-column)
        if freq_on_x:
            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555; padding:0 3px;")
            self._matrix_grid.addWidget(bnd_lbl, n_drows + 1, 0)

            # When x_rev, the highest-freq column is at c=0 (leftmost) — ">allt" moves there
            # and the boundary values follow the reversed column order.
            highest_col = 0 if x_rev else n_dcols - 1
            for c in range(n_dcols):
                if c == highest_col:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, n_drows + 1, c + 1)
                else:
                    # Map display col → data freq index to pick the correct boundary
                    bval_idx = (n_dcols - 1 - c) if x_rev else c
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedSize(80, 22)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    e.setToolTip(
                        f"Övre gräns (händelser/år) för kolumn {c}.\n"
                        f"Frekvenser under detta värde tillhör denna kolumn.\n"
                        f"Exempel: 0.1 = en gång per 10 år")
                    self._matrix_grid.addWidget(e, n_drows + 1, c + 1)
                    self._freq_boundary_edits.append(e)
                    # Connect boundary edit → auto-update adjacent axis labels
                    e.editingFinished.connect(
                        lambda _e=e, _c=c: self._sync_freq_label_from_boundary(_e, _c))
        else:
            # When frequency on Y: add interval boundary column on the right
            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555;")
            self._matrix_grid.addWidget(bnd_lbl, 0, n_dcols + 1)

            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            # Last row always gets ">allt" (the extreme bucket with no further boundary).
            # bval_idx depends on y_rev: y_rev=False → high-at-top, reversed boundary order.
            for r in range(n_drows):
                if r == n_drows - 1:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, r + 1, n_dcols + 1)
                else:
                    bval_idx = r if y_rev else (n_drows - 2 - r)
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedSize(70, 40)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    self._matrix_grid.addWidget(e, r + 1, n_dcols + 1)
                    self._freq_boundary_edits.append(e)

    def _sync_freq_label_from_boundary(self, boundary_edit, col_idx: int):
        """Auto-update the frequency axis label(s) adjacent to the changed boundary."""
        try:
            val = float(boundary_edit.text().strip())
        except ValueError:
            return
        if val <= 0:
            return

        def _fmt(v):
            if v >= 1:       return f"{v:.3g}/år"
            if v >= 0.001:   return f"{v:.3g}/år"
            return f"{v:.2e}/år"

        # Collect all boundary values to compute ranges
        bvals = []
        for e in self._freq_boundary_edits:
            try:
                bvals.append(float(e.text()))
            except ValueError:
                bvals.append(None)

        def _label_for_col(c):
            """Return an auto-generated interval label for display column c."""
            left  = bvals[c-1] if c > 0 and c-1 < len(bvals) else None
            right = bvals[c]   if c < len(bvals) else None
            if left is None and right is not None:
                return f"< {_fmt(right)}"
            if left is not None and right is None:
                return f"≥ {_fmt(left)}"
            if left is not None and right is not None:
                return f"{_fmt(left)} – {_fmt(right)}"
            return ""

        # Update the two adjacent column labels (col_idx and col_idx+1)
        for affected_c in (col_idx, col_idx + 1):
            if 0 <= affected_c < len(self._x_label_edits):
                new_lbl = _label_for_col(affected_c)
                if new_lbl:
                    self._x_label_edits[affected_c].setText(new_lbl)

    def _edit_cell(self, btn):
        """Click a cell → choose background color, label, and text color."""
        color = QColorDialog.getColor(QColor(btn.color()), self, "Välj bakgrundsfärg för cell")
        if not color.isValid():
            return
        label, ok = QInputDialog.getText(
            self, "Celltext", "Risknivå-etikett (t.ex. Låg, Medium, Hög, Kritisk):",
            text=btn.label())
        if not ok:
            return
        # Auto-suggest fg based on luminance; let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        current_fg = btn.fg_color() if btn.fg_color() else auto_fg
        fg_obj = QColorDialog.getColor(QColor(current_fg), self, "Välj textfärg")
        fg = fg_obj.name() if fg_obj.isValid() else current_fg
        btn.set_cell(color.name(), label.strip() or btn.label(), fg)

    def _save_matrix(self):
        n_cons = self._rows_spin.value()   # consequence levels (rows in data)
        n_freq = self._cols_spin.value()   # frequency levels  (cols in data)
        x_axis = self._axis_combo.currentData() or 'frequency'
        freq_on_x = (x_axis == 'frequency')

        # Cell buttons store (cons_idx, freq_idx) regardless of display orientation
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        labels    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        for _disp_r, row_btns in self._cell_buttons:
            for btn in row_btns:
                cons_i, freq_i = btn.row, btn.col   # (cons_idx, freq_idx)
                if cons_i < n_cons and freq_i < n_freq:
                    colors[cons_i][freq_i]    = btn.color()
                    labels[cons_i][freq_i]    = btn.label()
                    fg_colors[cons_i][freq_i] = btn.fg_color()

        # Axis labels: _x_label_edits are the column headers (whatever axis),
        # _y_label_edits are the row headers (reversed, highest at top)
        raw_col = [e.text().strip() for e in self._x_label_edits]
        raw_row = list(reversed([e.text().strip() for e in self._y_label_edits]))  # low→high

        if freq_on_x:
            # X=freq columns, Y=cons rows
            x_labels = raw_col or [f'F{i-1}' for i in range(n_freq)]
            y_labels = raw_row or [f'C{i+1}' for i in range(n_cons)]
        else:
            # X=cons columns, Y=freq rows
            y_labels = raw_col or [f'C{i+1}' for i in range(n_cons)]
            x_labels = raw_row or [f'F{i-1}' for i in range(n_freq)]

        # Pad/trim to correct lengths
        while len(x_labels) < n_freq: x_labels.append(f'F{len(x_labels)-1}')
        while len(y_labels) < n_cons: y_labels.append(f'C{len(y_labels)+1}')
        x_labels = x_labels[:n_freq]
        y_labels = y_labels[:n_cons]

        cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':      x_axis,
            'x_reversed':  self._x_rev_chk.isChecked(),
            'y_reversed':  self._y_rev_chk.isChecked(),
            'x_labels':    x_labels,
            'y_labels':    y_labels,
            'cell_colors':    colors,
            'cell_labels':    labels,
            'cell_fg_colors': fg_colors,
        }
        # Read frequency boundaries from editable row/column (display order)
        freq_boundaries = []
        for e in getattr(self, '_freq_boundary_edits', []):
            try:
                v = float(e.text().strip())
                if v > 0:
                    freq_boundaries.append(v)
            except ValueError:
                pass
        if not freq_boundaries:
            freq_boundaries = list(DEFAULT_FREQ_BOUNDARIES)
        # Boundary edits were laid out in display order; convert back to data order
        # (lowest freq level first) by reversing when the display was reversed:
        #   freq_on_x + x_rev: highest-freq col is leftmost → edits stored high-to-low
        #   freq_on_y + NOT y_rev: highest-freq row is topmost → edits stored high-to-low
        _is_reversed_display = (freq_on_x and self._x_rev_chk.isChecked()) or \
                               (not freq_on_x and not self._y_rev_chk.isChecked())
        if _is_reversed_display:
            freq_boundaries = list(reversed(freq_boundaries))
        cfg['freq_boundaries'] = freq_boundaries

        cfg = _normalise_matrix(cfg)   # ensure consistent before saving
        self.db.set_risk_matrix(cfg)
        load_matrix(self.db)
        QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
        self.matrix_changed.emit()

    def _apply_freq_preset(self, labels: list, bounds: list):
        """Populate frequency axis headers and boundary edits from a preset.

        labels: ordered lowest-to-highest frequency (data order).
        bounds: n-1 boundary values (events/year), data order lowest first.
        Accounts for current axis orientation (freq_on_x/y) and direction (x_rev/y_rev).
        """
        freq_on_x = (self._axis_combo.currentData() or 'frequency') == 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()
        n         = len(labels)

        if freq_on_x:
            # _x_label_edits[i] = display column i → data index (n-1-i if x_rev else i)
            for i, e in enumerate(self._x_label_edits):
                data_idx = (n - 1 - i) if x_rev else i
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
            # _freq_boundary_edits: edit[i] maps to bval_idx (n-1-(i+1) if x_rev else i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = (n - 2 - i) if x_rev else i
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")
        else:
            # _y_label_edits[0] = top row
            # y_rev=False: top=highest freq → data index n-1-i; y_rev=True: top=lowest → i
            for i, e in enumerate(self._y_label_edits):
                data_idx = i if y_rev else (n - 1 - i)
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
            # _freq_boundary_edits for y case: edit[i] → bval_idx (i if y_rev else n-2-i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = i if y_rev else (n - 2 - i)
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")

    def _load_categories(self):
        self._cat_list.clear()
        for cat in self.db.consequence_categories():
            item = QListWidgetItem(cat['name'])
            item.setData(Qt.ItemDataRole.UserRole, cat['id'])
            self._cat_list.addItem(item)

    def _cat_add(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Ny kategori", "Namn:")
        if ok and name.strip():
            self.db.add_category(name.strip())
            self._load_categories()

    def _cat_rename(self):
        from PyQt6.QtWidgets import QInputDialog
        item = self._cat_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_category(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._load_categories()

    def _cat_delete(self):
        item = self._cat_list.currentItem()
        if not item: return
        self.db.delete_category(item.data(Qt.ItemDataRole.UserRole))
        self._load_categories()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

class PIDManagementPanel(QWidget):
    """PID revision history and sheet reordering panel."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Revision history ───────────────────────────────────────────
        rev_widget = QWidget()
        rev_layout = QVBoxLayout(rev_widget)
        rev_layout.setContentsMargins(8, 8, 8, 8)
        rev_layout.setSpacing(6)

        rev_hdr = QHBoxLayout()
        rev_hdr.addWidget(QLabel("Revisionshistorik:"))
        rev_hdr.addStretch()
        rev_layout.addLayout(rev_hdr)

        self._rev_table = QTableWidget(0, 4)
        self._rev_table.setHorizontalHeaderLabels(['Revision', 'Anteckningar', 'Datum', 'PDF-fil'])
        self._rev_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._rev_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self._rev_table.setColumnWidth(0, 120)
        self._rev_table.setColumnWidth(2, 130)
        self._rev_table.setColumnWidth(3, 180)
        self._rev_table.verticalHeader().setVisible(False)
        self._rev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._rev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._rev_table.setAlternatingRowColors(True)
        self._rev_table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        rev_layout.addWidget(self._rev_table)
        tabs.addTab(rev_widget, "Revisioner")

        # ── Tab 1: Sheet management ───────────────────────────────────────────
        sheets_widget = QWidget()
        sheets_layout = QVBoxLayout(sheets_widget)
        sheets_layout.setContentsMargins(8, 8, 8, 8)
        sheets_layout.setSpacing(6)

        sheet_hdr = QHBoxLayout()
        sheet_hdr.addWidget(QLabel("Bladordning — dra för att ändra ordning:"))
        sheet_hdr.addStretch()
        rename_btn = QPushButton("✏️ Byt namn")
        rename_btn.clicked.connect(self._rename_sheet)
        sheet_hdr.addWidget(rename_btn)
        delete_btn = QPushButton("🗑 Ta bort")
        delete_btn.clicked.connect(self._delete_sheets)
        sheet_hdr.addWidget(delete_btn)
        sheets_layout.addLayout(sheet_hdr)

        self._sheet_list = QListWidget()
        self._sheet_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._sheet_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sheet_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._sheet_list.model().rowsMoved.connect(self._on_sheets_reordered)
        self._sheet_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sheet_list.customContextMenuRequested.connect(self._sheet_context_menu)
        _base_kp = self._sheet_list.keyPressEvent
        def _sheet_key_press(event, _base=_base_kp):
            if event.key() == Qt.Key.Key_Delete:
                self._delete_sheets()
            else:
                _base(event)
        self._sheet_list.keyPressEvent = _sheet_key_press
        sheets_layout.addWidget(self._sheet_list)
        tabs.addTab(sheets_widget, "Blad")

        self.refresh()

    def refresh(self):
        self._rev_table.setRowCount(0)
        for rev in self.db.get_revisions():
            r = self._rev_table.rowCount()
            self._rev_table.insertRow(r)
            self._rev_table.setItem(r, 0, QTableWidgetItem(rev['revision'] or ''))
            self._rev_table.setItem(r, 1, QTableWidgetItem(rev['notes'] or ''))
            self._rev_table.setItem(r, 2, QTableWidgetItem(rev['created_at'] or ''))
            fname = Path(rev['pdf_path']).name if rev['pdf_path'] else ''
            self._rev_table.setItem(r, 3, QTableWidgetItem(fname))
            self._rev_table.setRowHeight(r, 24)

        self._sheet_list.clear()
        for sheet in self.db.get_sheets():
            item = QListWidgetItem(
                f"{sheet['display_order'] + 1}. {sheet['sheet_name']}  "
                f"(PDF-sida {sheet['physical_page'] + 1})")
            item.setData(Qt.ItemDataRole.UserRole, sheet['id'])
            self._sheet_list.addItem(item)

    def _on_sheets_reordered(self, *_):
        ids = [self._sheet_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._sheet_list.count())]
        self.db.reorder_sheets(ids)
        self.refresh()

    def _rename_sheet(self):
        item = self._sheet_list.currentItem()
        if not item:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        current_name = ''
        for s in self.db.get_sheets():
            if s['id'] == sheet_id:
                current_name = s['sheet_name']
                break
        name, ok = QInputDialog.getText(self, "Byt namn", "Bladnamn:", text=current_name)
        if ok and name.strip():
            self.db.update_sheet_name(sheet_id, name.strip())
            self.refresh()

    def _delete_sheets(self):
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        count = len(selected)
        msg = f"Ta bort {count} blad?" if count > 1 else f"Ta bort '{selected[0].text().split('  ')[0]}'?"
        ans = QMessageBox.question(self, "Ta bort blad", msg,
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in selected]
        self.db.delete_sheets(ids)
        self.refresh()

    def _sheet_context_menu(self, pos):
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        menu = QMenu(self)
        if len(selected) == 1:
            menu.addAction("✏️ Byt namn", self._rename_sheet)
        menu.addAction("🗑 Ta bort", self._delete_sheets)
        menu.exec(self._sheet_list.viewport().mapToGlobal(pos))


class StudyManagementPanel(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Studiehantering")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Statistics ─────────────────────────────────────────────────
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(8)

        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet(
            "background:#f0f4f8; border:1px solid #ccc; border-radius:6px; padding:10px;")
        stats_layout.addWidget(self._stats_lbl)

        bar = QHBoxLayout()
        refresh_btn = QPushButton("🔄 Uppdatera")
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn); bar.addStretch()
        stats_layout.addLayout(bar)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ['Nod', 'Orsak', 'L', 'Konsekvens', 'S', 'Risknivå', 'Kategori', 'Safeguards'])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in [2, 3, 4, 5, 6, 7]:
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 100)
        self._table.setColumnWidth(2, 28)
        self._table.setColumnWidth(4, 28)
        self._table.setColumnWidth(5, 80)
        self._table.setColumnWidth(6, 80)
        self._table.setColumnWidth(7, 150)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        stats_layout.addWidget(self._table)
        tabs.addTab(stats_widget, "Statistik")

        # ── Tab 1: PID management ─────────────────────────────────────────────
        self._pid_mgmt = PIDManagementPanel(db)
        tabs.addTab(self._pid_mgmt, "PID-hantering")

        self.refresh()

    def refresh(self):
        s = self.db.stats()
        self._stats_lbl.setText(
            f"  Noder: <b>{s['nodes']}</b>   |   Orsaker: <b>{s['causes']}</b>   |   "
            f"Konsekvenser: <b>{s['consequences']}</b>   |   Safeguards: <b>{s['safeguards']}</b>   |   "
            f"Öppna åtgärder: <b>{s['open_actions']}</b>")
        self._stats_lbl.setTextFormat(Qt.TextFormat.RichText)

        self._table.setRowCount(0)
        for row in self.db.all_data():
            level, bg, fg = risk_info(row['likelihood'], row['severity'])
            r = self._table.rowCount()
            self._table.insertRow(r)

            def _c(t, center=False):
                item = QTableWidgetItem(str(t))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if center else
                                      Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                return item

            self._table.setItem(r, 0, _c(row['node_name']))
            self._table.setItem(r, 1, _c(row['cause']))
            self._table.setItem(r, 2, _c(row['likelihood'], True))
            self._table.setItem(r, 3, _c(row['consequence']))
            self._table.setItem(r, 4, _c(row['severity'], True))
            risk_item = QTableWidgetItem(f"{level}\nF={row['likelihood']} C={row['severity']}")
            risk_item.setBackground(QBrush(QColor(bg)))
            risk_item.setForeground(QBrush(QColor(fg)))
            risk_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(r, 5, risk_item)
            self._table.setItem(r, 6, _c(row['category']))
            sg_text = '; '.join(
                f"{s['description']}{'(RRF' + str(s['rrf']) + ')' if s['rrf'] > 1 else ''}"
                for s in row['safeguards']) or '—'
            self._table.setItem(r, 7, _c(sg_text))
            self._table.setRowHeight(r, 28)

    def refresh_pid(self):
        self._pid_mgmt.refresh()


# Keep old name as alias so any remaining references don't crash
AdminPanel = StudyManagementPanel


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def export_excel(db: Database, filepath: str):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        return False, "openpyxl saknas.\nKör: pip install openpyxl"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HAZOP"

    HEADERS = ['Nod', 'P&ID', 'Orsak', 'F', 'Konsekvens', 'C', 'Risknivå',
               'Kategori', 'Safeguards', 'Åtgärder']
    COL_WIDTHS = [20, 12, 32, 6, 32, 6, 14, 12, 40, 42]
    RISK_FILLS = {'Låg': 'C6EFCE', 'Medium': 'FFEB9C', 'Hög': 'FFC7CE', 'Kritisk': 'FF0000'}

    thin   = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    wrap   = Alignment(vertical='top', wrap_text=True)

    for col, (hdr, width) in enumerate(zip(HEADERS, COL_WIDTHS), 1):
        cell = ws.cell(row=1, column=col, value=hdr)
        cell.fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
        cell.font = Font(bold=True, color='FFFFFF', size=10)
        cell.alignment = center; cell.border = border
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'

    for r, row in enumerate(db.all_data(), 2):
        level, _, _ = risk_info(row['likelihood'], row['severity'])
        acts_str = '\n'.join(
            f"• {a['description']} | {a['responsible']} | {a['due_date']} | {a['status']}"
            for a in row['actions'])
        sg_str = '; '.join(
            f"{s['description']} (RRF{s['rrf']})" if s['rrf'] > 1 else s['description']
            for s in row['safeguards'])
        values = [row['node_name'], row['node_pid'], row['cause'], row['likelihood'],
                  row['consequence'], row['severity'], level,
                  row['category'], sg_str, acts_str]
        for c, val in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = border
            cell.alignment = center if c in (4, 6) else wrap
            if c == 7:
                fc = RISK_FILLS.get(level, 'FFFFFF')
                cell.fill = PatternFill(start_color=fc, end_color=fc, fill_type='solid')
        ws.row_dimensions[r].height = 36

    try:
        wb.save(filepath); return True, ""
    except Exception as e:
        return False, str(e)


def export_pdf(db: Database, filepath: str):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError:
        return False, "reportlab saknas.\nKör: pip install reportlab"

    doc = SimpleDocTemplate(filepath, pagesize=landscape(A4),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    cs = ParagraphStyle('c', fontSize=7, leading=9)
    hs = ParagraphStyle('h', fontSize=8, leading=10, textColor=colors.white,
                        fontName='Helvetica-Bold')

    RISK_COLORS_PDF = {
        'Låg':     colors.HexColor('#27ae60'),
        'Medium':  colors.HexColor('#f39c12'),
        'Hög':     colors.HexColor('#e67e22'),
        'Kritisk': colors.HexColor('#e74c3c'),
    }

    headers = ['Nod / P&ID', 'Orsak', 'L', 'Konsekvens', 'S', 'Risknivå', 'Safeguards', 'Åtgärder']
    table_data = [[Paragraph(h, hs) for h in headers]]
    row_styles = []

    for i, row in enumerate(db.all_data(), 1):
        level, _, _ = risk_info(row['likelihood'], row['severity'])
        acts_str = '<br/>'.join(
            f"• {a['description']} ({a['status']})" for a in row['actions']) or '—'
        sg_str = '<br/>'.join(
            f"• {s['description']}" + (f" RRF{s['rrf']}" if s['rrf'] > 1 else '')
            for s in row['safeguards']) or '—'
        table_data.append([
            Paragraph(f"{row['node_name']}<br/><font size='6'>{row['node_pid']}</font>", cs),
            Paragraph(row['cause'], cs),
            Paragraph(str(row['likelihood']), cs),
            Paragraph(row['consequence'], cs),
            Paragraph(str(row['severity']), cs),
            Paragraph(f"<b>{level}</b><br/>F={row['likelihood']} C={row['severity']}", cs),
            Paragraph(sg_str, cs),
            Paragraph(acts_str, cs),
        ])
        rc = RISK_COLORS_PDF.get(level, colors.white)
        row_styles.append(('BACKGROUND', (5, i), (5, i), rc))
        row_styles.append(('TEXTCOLOR', (5, i), (5, i), colors.white))

    col_widths = [32*mm, 40*mm, 8*mm, 45*mm, 8*mm, 22*mm, 55*mm, 55*mm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E79')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f5f5f5'), colors.white]),
    ] + row_styles))

    elements = [Paragraph("HAZOP — Rapport", styles['Title']), Spacer(1, 5*mm), t]
    try:
        doc.build(elements); return True, ""
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
# EQUIPMENT PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _tag_prefix(tag: str) -> str:
    m = re.match(r'^([A-Z]+)', tag.upper())
    return m.group(1) if m else tag


_EQ_TYPE_ITEMS = [''] + sorted(COMPONENT_TYPES.keys()) + ['Rörledning', 'Övrigt / Okänd']

# Column indices
_EC_CHK  = 0
_EC_TAG  = 1
_EC_PFX  = 2
_EC_PAGE = 3
_EC_OCR  = 4
_EC_TYPE = 5
_EC_DESC = 6
_EC_DEL  = 7


class EquipmentPanel(QWidget):
    """Persistent equipment register — scan P&ID, review, edit and create nodes."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        title = QLabel("Utrustningsregister")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        # Toolbar
        tb = QHBoxLayout()
        self._scan_btn = QPushButton("🔍 Skanna P&ID")
        self._scan_btn.setToolTip("Skannar inläst P&ID-fil efter utrustningstaggar")
        self._scan_btn.setStyleSheet(
            "background:#1F4E79; color:white; border:none; border-radius:4px; padding:3px 10px;")
        self._scan_btn.clicked.connect(self._scan)

        add_btn = QPushButton("+ Lägg till")
        add_btn.setToolTip("Lägg till en tagg manuellt")
        add_btn.clicked.connect(self._add_manual)

        refresh_btn = QPushButton("🔄 Uppdatera")
        refresh_btn.clicked.connect(self.refresh)

        self._create_btn = QPushButton("🏭 Skapa HAZOP-noder")
        self._create_btn.setToolTip("Skapar en nod per ikryssad rad")
        self._create_btn.clicked.connect(self._create_nodes)

        clear_btn = QPushButton("🗑 Rensa utrustning")
        clear_btn.setToolTip("Tar bort alla poster i utrustningsregistret")
        clear_btn.setStyleSheet("color:#c0392b; font-weight:bold;")
        clear_btn.clicked.connect(self._clear)

        for btn in [self._scan_btn, add_btn, refresh_btn, self._create_btn, clear_btn]:
            tb.addWidget(btn)
        tb.addStretch()
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#555; font-size:11px;")
        tb.addWidget(self._status_lbl)
        layout.addLayout(tb)

        # Filter bar
        fb = QHBoxLayout()
        fb.addWidget(QLabel("Filtrera:"))
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Sök tagg, typ, sida…")
        self._filter.textChanged.connect(self._apply_filter)
        fb.addWidget(self._filter)
        sel_all  = QPushButton("Välj alla")
        desel    = QPushButton("Avmarkera alla")
        self._ocr_only = QPushButton("Visa OCR")
        self._ocr_only.setCheckable(True)
        self._ocr_only.toggled.connect(self._apply_filter)
        sel_all.clicked.connect(lambda: self._bulk_check(True))
        desel.clicked.connect(lambda: self._bulk_check(False))
        for b in [sel_all, desel, self._ocr_only]:
            fb.addWidget(b)
        layout.addLayout(fb)

        # Table
        self._tbl = QTableWidget(0, 8)
        self._tbl.setHorizontalHeaderLabels(
            ['✓', 'Tagg', 'Prefix', 'Sida', 'OCR', 'Utrustningstyp', 'Beskrivning', ''])
        hdr = self._tbl.horizontalHeader()
        modes = [
            (_EC_CHK,  QHeaderView.ResizeMode.Fixed),
            (_EC_TAG,  QHeaderView.ResizeMode.Interactive),
            (_EC_PFX,  QHeaderView.ResizeMode.Fixed),
            (_EC_PAGE, QHeaderView.ResizeMode.Fixed),
            (_EC_OCR,  QHeaderView.ResizeMode.Fixed),
            (_EC_TYPE, QHeaderView.ResizeMode.Interactive),
            (_EC_DESC, QHeaderView.ResizeMode.Stretch),
            (_EC_DEL,  QHeaderView.ResizeMode.Fixed),
        ]
        widths = {_EC_CHK: 30, _EC_TAG: 110, _EC_PFX: 60, _EC_PAGE: 44,
                  _EC_OCR: 36, _EC_TYPE: 185, _EC_DEL: 64}
        for col, mode in modes:
            hdr.setSectionResizeMode(col, mode)
        for col, w in widths.items():
            self._tbl.setColumnWidth(col, w)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        self._tbl.cellChanged.connect(self._on_cell_changed)
        layout.addWidget(self._tbl)

        self.refresh()

    # ── Populate ──────────────────────────────────────────────────────────────

    def refresh(self):
        self._loading = True
        self._tbl.blockSignals(True)
        self._tbl.setRowCount(0)
        for item in self.db.equipment_items():
            self._insert_row(dict(item))
        self._tbl.blockSignals(False)
        self._loading = False
        self._apply_filter()

    def _insert_row(self, item: dict):
        r = self._tbl.rowCount()
        self._tbl.insertRow(r)

        chk = QTableWidgetItem()
        chk.setCheckState(
            Qt.CheckState.Checked if item.get('include', 1) else Qt.CheckState.Unchecked)
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setData(Qt.ItemDataRole.UserRole, item['id'])
        self._tbl.setItem(r, _EC_CHK, chk)

        tag_item = QTableWidgetItem(item['tag'])
        if item.get('is_ocr'):
            tag_item.setBackground(QBrush(QColor('#fff3cd')))
            tag_item.setToolTip("Identifierad via OCR — kontrollera taggen")
        self._tbl.setItem(r, _EC_TAG, tag_item)

        pfx = QTableWidgetItem(item.get('prefix', _tag_prefix(item['tag'])))
        pfx.setFlags(pfx.flags() & ~Qt.ItemFlag.ItemIsEditable)
        pfx.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tbl.setItem(r, _EC_PFX, pfx)

        pg = QTableWidgetItem(str(item.get('pid_page', 0) + 1))
        pg.setFlags(pg.flags() & ~Qt.ItemFlag.ItemIsEditable)
        pg.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tbl.setItem(r, _EC_PAGE, pg)

        ocr = QTableWidgetItem('🔬' if item.get('is_ocr') else '')
        ocr.setFlags(ocr.flags() & ~Qt.ItemFlag.ItemIsEditable)
        ocr.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        ocr.setToolTip("Hittad via OCR" if item.get('is_ocr') else "Hittad via PDF-text")
        self._tbl.setItem(r, _EC_OCR, ocr)

        combo = QComboBox()
        for t in _EQ_TYPE_ITEMS:
            combo.addItem(t)
        et = item.get('equipment_type', '')
        if et:
            idx = combo.findText(et)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        iid = item['id']
        combo.currentTextChanged.connect(lambda typ, i=iid: self._save_type(i, typ))
        self._tbl.setCellWidget(r, _EC_TYPE, combo)

        self._tbl.setItem(r, _EC_DESC, QTableWidgetItem(item.get('description', '')))

        del_btn = QPushButton("Ta bort")
        del_btn.setFixedHeight(22)
        del_btn.clicked.connect(lambda _, i=iid: self._delete(i))
        self._tbl.setCellWidget(r, _EC_DEL, del_btn)
        self._tbl.setRowHeight(r, 26)

    # ── Cell editing ──────────────────────────────────────────────────────────

    def _on_cell_changed(self, row, col):
        try:
            self._on_cell_changed_inner(row, col)
        except Exception as e:
            QMessageBox.critical(None, "Fel vid celländring (utrustning)", str(e))

    def _on_cell_changed_inner(self, row, col):
        if self._loading:
            return
        chk = self._tbl.item(row, _EC_CHK)
        if not chk:
            return
        iid = chk.data(Qt.ItemDataRole.UserRole)

        if col == _EC_CHK:
            inc = 1 if chk.checkState() == Qt.CheckState.Checked else 0
            self.db.conn.execute("UPDATE equipment_catalog SET include=? WHERE id=?", (inc, iid))
            self.db.conn.commit()
            self._update_status()

        elif col == _EC_TAG:
            new_tag = (self._tbl.item(row, _EC_TAG).text().strip().upper()
                       if self._tbl.item(row, _EC_TAG) else '')
            new_pfx = _tag_prefix(new_tag)
            self._tbl.blockSignals(True)
            pfx_item = self._tbl.item(row, _EC_PFX)
            if pfx_item:
                pfx_item.setText(new_pfx)
            # Suggest type from new prefix if none set
            combo = self._tbl.cellWidget(row, _EC_TYPE)
            if combo and not combo.currentText():
                known = KNOWN_PREFIXES.get(new_pfx)
                if known:
                    idx = combo.findText(known[1])
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
            self._tbl.blockSignals(False)
            desc = self._tbl.item(row, _EC_DESC)
            self.db.update_equipment_item(
                iid, new_tag, new_pfx,
                combo.currentText() if combo else '',
                desc.text() if desc else '')

        elif col == _EC_DESC:
            tag_i  = self._tbl.item(row, _EC_TAG)
            pfx_i  = self._tbl.item(row, _EC_PFX)
            combo  = self._tbl.cellWidget(row, _EC_TYPE)
            desc_i = self._tbl.item(row, _EC_DESC)
            self.db.update_equipment_item(
                iid,
                tag_i.text() if tag_i else '',
                pfx_i.text() if pfx_i else '',
                combo.currentText() if combo else '',
                desc_i.text() if desc_i else '')

    def _save_type(self, iid, typ):
        if not self._loading:
            self.db.conn.execute(
                "UPDATE equipment_catalog SET equipment_type=? WHERE id=?", (typ, iid))
            self.db.conn.commit()

    # ── Filter / selection ────────────────────────────────────────────────────

    def _apply_filter(self):
        text     = self._filter.text().lower()
        ocr_only = self._ocr_only.isChecked()
        for r in range(self._tbl.rowCount()):
            tag_t  = (self._tbl.item(r, _EC_TAG).text().lower()
                      if self._tbl.item(r, _EC_TAG) else '')
            type_w = self._tbl.cellWidget(r, _EC_TYPE)
            type_t = type_w.currentText().lower() if type_w else ''
            pg_t   = self._tbl.item(r, _EC_PAGE).text() if self._tbl.item(r, _EC_PAGE) else ''
            is_ocr = bool(self._tbl.item(r, _EC_OCR) and self._tbl.item(r, _EC_OCR).text())
            hidden = (text and text not in tag_t and text not in type_t and text not in pg_t) \
                     or (ocr_only and not is_ocr)
            self._tbl.setRowHidden(r, hidden)
        self._update_status()

    def _bulk_check(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._loading = True
        for r in range(self._tbl.rowCount()):
            if not self._tbl.isRowHidden(r):
                item = self._tbl.item(r, _EC_CHK)
                if item:
                    item.setCheckState(state)
                    self.db.conn.execute(
                        "UPDATE equipment_catalog SET include=? WHERE id=?",
                        (1 if checked else 0, item.data(Qt.ItemDataRole.UserRole)))
        self.db.conn.commit()
        self._loading = False
        self._update_status()

    def _update_status(self):
        visible = sum(1 for r in range(self._tbl.rowCount())
                      if not self._tbl.isRowHidden(r))
        checked = sum(1 for r in range(self._tbl.rowCount())
                      if not self._tbl.isRowHidden(r)
                      and self._tbl.item(r, _EC_CHK)
                      and self._tbl.item(r, _EC_CHK).checkState() == Qt.CheckState.Checked)
        total_all = sum(1 for _ in range(self._tbl.rowCount()))
        self._status_lbl.setText(
            f"{total_all} taggar totalt  |  {visible} visas  |  {checked} valda")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _add_manual(self):
        from PyQt6.QtWidgets import QInputDialog
        tag, ok = QInputDialog.getText(self, "Ny tagg", "Ange taggnummer (t.ex. PCV-101):")
        if not ok or not tag.strip():
            return
        tag = tag.strip().upper()
        pfx = _tag_prefix(tag)
        known = KNOWN_PREFIXES.get(pfx, ('', ''))
        self.db.add_equipment_item(tag, tag, pfx, 0, known[1] if known else '', '', 0)
        self.refresh()

    def _delete(self, iid):
        self.db.delete_equipment_item(iid)
        self.refresh()

    def _clear(self):
        n = len(self.db.equipment_items())
        reply = QMessageBox.question(
            self, "Rensa utrustning",
            f"Ta bort alla {n} poster i utrustningsregistret?\n\n"
            "Detta kan inte ångras.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Ok:
            self.db.clear_equipment_catalog()
            self.refresh()

    def _create_nodes(self):
        to_create = []
        for r in range(self._tbl.rowCount()):
            chk = self._tbl.item(r, _EC_CHK)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                tag   = self._tbl.item(r, _EC_TAG).text() if self._tbl.item(r, _EC_TAG) else ''
                pg    = int(self._tbl.item(r, _EC_PAGE).text()) - 1 if self._tbl.item(r, _EC_PAGE) else 0
                combo = self._tbl.cellWidget(r, _EC_TYPE)
                et    = combo.currentText() if combo else ''
                desc  = self._tbl.item(r, _EC_DESC).text() if self._tbl.item(r, _EC_DESC) else ''
                if tag:
                    to_create.append((tag, pg, et, desc))
        if not to_create:
            QMessageBox.information(self, "Ingen vald", "Kryssa i minst en rad.")
            return
        created = 0
        for tag, pg, et, desc in to_create:
            nid = self.db.add_node_with_markup(
                tag, [], {'color': '#FF8C00', 'width': 2, 'alpha': 180}, pg)
            self.db.conn.execute(
                "UPDATE nodes SET name=?, pid_ref=?, description=? WHERE id=?",
                (tag, f"Sida {pg + 1}", f"{et}{': ' + desc if desc else ''}", nid))
            self.db.conn.commit()
            created += 1
        QMessageBox.information(self, "Klart",
            f"{created} HAZOP-noder skapade.\nGå till P&ID-vyn och uppdatera trädet.")

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _scan(self):
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:  pip install PyMuPDF")
            return

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID",
                "Öppna en P&ID-fil i P&ID-vyn först, sedan kan du skanna härifrån.")
            return

        try:
            import fitz
            pdf_doc = fitz.open(str(path))
        except Exception as e:
            QMessageBox.warning(self, "PDF-fel", f"Kunde inte öppna PDF:\n{e}")
            return

        # OCR choice
        st = ocr_status()
        use_ocr = False
        if st['tesseract'] or st['easyocr']:
            engines = [n for n, v in [('pytesseract', st['tesseract']),
                                       ('easyocr', st['easyocr'])] if v]
            reply = QMessageBox.question(
                self, "OCR",
                f"Tillgänglig OCR-motor: {', '.join(engines)}\n\n"
                "Använd OCR för sidor med lite text?\n"
                "(Bättre för skannade ritningar, tar längre tid.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            use_ocr = (reply == QMessageBox.StandardButton.Yes)

        # Progress
        n_pages  = pdf_doc.page_count
        progress = QProgressDialog("Förbereder…", "Avbryt", 0, n_pages, self)
        progress.setWindowTitle("Skannar P&ID")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def _cb(pn, _total, msg):
            if progress.wasCanceled():
                return
            progress.setValue(pn)
            progress.setLabelText(msg)
            QApplication.processEvents()

        result = scan_pdf_for_equipment(
            pdf_doc, use_ocr=use_ocr, progress_callback=_cb)
        progress.setValue(n_pages)
        progress.close()
        pdf_doc.close()

        if progress.wasCanceled():
            return

        meta = result.pop('_meta', {})
        real = {k: v for k, v in result.items() if not k.startswith('_')}

        if not real:
            QMessageBox.warning(
                self, "Inga taggar",
                "Inga utrustningstaggar hittades.\n\n"
                + ("Prova med OCR aktiverat (installera pytesseract eller easyocr)."
                   if not use_ocr else
                   "Kontrollera att PDF-texten är läsbar och försök med OCR."))
            return

        # Import to DB
        self.db.clear_equipment_catalog()
        for prefix, data in real.items():
            known      = KNOWN_PREFIXES.get(prefix, ('', ''))
            saved_type = (self.db.get_equipment_type(prefix)
                          if hasattr(self.db, 'get_equipment_type') else '')
            eq_type    = saved_type or (known[1] if known else '')
            ocr_pages  = data.get('ocr_pages', set())
            for tag in data['tags']:
                page   = data['pages'].get(tag, 0)
                is_ocr = int(page in ocr_pages)
                self.db.add_equipment_item(tag, tag, prefix, page, eq_type, '', is_ocr)

        # Build summary
        n_tags   = sum(len(d['tags']) for d in real.values())
        n_groups = len(real)
        ocr_used = meta.get('ocr_used', False)
        ocr_eng  = meta.get('ocr_engine', '')

        type_counts: dict = {}
        for prefix, data in real.items():
            known = KNOWN_PREFIXES.get(prefix)
            et    = known[1] if known else 'Okänd'
            type_counts[et] = type_counts.get(et, 0) + len(data['tags'])

        lines = "\n".join(
            f"  • {t}: {c} st"
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))
        ocr_line = f"\n🔬 OCR användes ({ocr_eng})\n" if ocr_used else "\n"

        QMessageBox.information(
            self, "Skanning klar ✅",
            f"Skanning klar!\n\n"
            f"Totalt hittade:  {n_tags}  taggar\n"
            f"Prefix-grupper:  {n_groups}{ocr_line}\n"
            f"Utrustningstyper:\n{lines}\n\n"
            f"Tabellen nedan har uppdaterats.\n"
            f"Redigera eventuella OCR-fel (gul bakgrund) och kryssa i\n"
            f"de taggar du vill skapa HAZOP-noder för.")

        self.refresh()


# ══════════════════════════════════════════════════════════════════════════════
# REUSE CAUSES DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class ReuseDeviationCausesDialog(QDialog):
    """Pre-step dialog shown before P&ID placement.

    Lists causes from other deviations in the same node, organised by
    deviation with hierarchical reference labels (e.g. 1.2.3).
    User can toggle Referera / Invers per cause; accepted selections are
    created as new causes in the target deviation before P&ID mode opens.
    """

    SKIP = 2   # dialog result code for "Hoppa över"

    def __init__(self, target_dev_name, existing_causes, parent=None):
        """
        existing_causes — list of dicts with keys:
            id, description, deviation_name, deviation_id,
            ref_label (e.g. '1.2.3'), dev_label (e.g. '1.2')
        """
        super().__init__(parent)
        self.setWindowTitle("Återanvänd orsaker från andra avvikelser")
        self.setMinimumWidth(720)
        self.setMinimumHeight(560)
        self.resize(800, 640)

        # key → (mode, description, original_cause_id)
        # key is cause_id (int) for individual causes, or f"dev_{dev_id}" for deviation-level
        # original_cause_id is None for deviation-level entries (no marker to copy)
        self._selections: dict = {}

        layout = QVBoxLayout(self)

        hdr = QLabel(
            f"Lägger till orsaker under avvikelsen: <b>{target_dev_name}</b><br>"
            "<span style='color:gray;font-size:11px'>"
            "Välj orsaker från andra avvikelser att referera (kopiera) eller invertera "
            "(högt↔lågt, stänger↔öppnar, …).</span>")
        hdr.setWordWrap(True)
        layout.addWidget(hdr)

        # ── Scrollable cause list ─────────────────────────────────────────────
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(2, 2, 2, 2)
        inner_layout.setSpacing(2)

        grouped: dict = {}
        order:   list = []
        for c in existing_causes:
            dn = c['deviation_name']
            if dn not in grouped:
                grouped[dn] = []
                order.append(dn)
            grouped[dn].append(c)

        global_pos = 0
        for dev_n in order:
            causes  = grouped[dev_n]
            dev_lbl = causes[0]['dev_label']
            dev_id  = causes[0]['deviation_id']
            dev_key = f"dev_{dev_id}"
            dev_pos = global_pos
            global_pos += 1

            # ── Deviation header row ──────────────────────────────────────────
            hdr_w = QWidget()
            hdr_w.setStyleSheet("background:#ebebeb;border-radius:3px;")
            hdr_h = QHBoxLayout(hdr_w)
            hdr_h.setContentsMargins(6, 3, 4, 3)
            hdr_h.setSpacing(6)

            hdr_lbl = QLabel(
                f"<b><span style='color:#555'>{dev_lbl}</span>&nbsp;&nbsp;{dev_n}</b>")
            hdr_h.addWidget(hdr_lbl, 1)

            ref_label   = f"Se {dev_lbl}"
            ref_dev_btn = QPushButton(f"↗ {ref_label}")
            ref_dev_btn.setCheckable(True)
            ref_dev_btn.setToolTip(f"Skapar en referensorsak med texten: {ref_label}")
            ref_dev_btn.setStyleSheet(
                "QPushButton{font-size:10px;padding:2px 8px;border:1px solid #2980b9;"
                "border-radius:3px;background:transparent;color:#2980b9;font-style:italic;}"
                "QPushButton:checked{background:#2980b9;color:white;font-style:normal;}"
                "QPushButton:hover:!checked{background:#d6eaf8;}")
            ref_dev_btn.toggled.connect(
                self._make_ref_handler(dev_key, ref_label, None, None, dev_pos))
            hdr_h.addWidget(ref_dev_btn)
            inner_layout.addWidget(hdr_w)

            for cause in causes:
                cid      = cause['id']
                orig     = cause['description']
                inv_text = invert_cause_text(orig)
                c_pos    = global_pos
                global_pos += 1

                row_w = QWidget()
                row_h = QHBoxLayout(row_w)
                row_h.setContentsMargins(12, 1, 4, 1)
                row_h.setSpacing(6)

                num_lbl = QLabel(
                    f"<span style='color:#888;font-family:monospace'>"
                    f"{cause['ref_label']}</span>")
                num_lbl.setFixedWidth(42)
                row_h.addWidget(num_lbl)

                desc_lbl = QLabel(orig)
                desc_lbl.setToolTip(orig)
                row_h.addWidget(desc_lbl, 1)

                ref_btn = QPushButton("Referera")
                ref_btn.setCheckable(True)
                ref_btn.setFixedWidth(72)
                ref_btn.setStyleSheet(
                    "QPushButton{font-size:10px;padding:2px 4px;border:1px solid #2980b9;"
                    "border-radius:3px;}"
                    "QPushButton:checked{background:#2980b9;color:white;}"
                    "QPushButton:hover:!checked{background:#d6eaf8;}")

                has_inv = inv_text != orig
                inv_btn = QPushButton("Invers")
                inv_btn.setCheckable(has_inv)
                inv_btn.setEnabled(has_inv)
                inv_btn.setFixedWidth(56)
                if has_inv:
                    inv_btn.setToolTip(f"Skapar: {inv_text}")
                    inv_btn.setStyleSheet(
                        "QPushButton{font-size:10px;padding:2px 4px;border:1px solid #8e44ad;"
                        "border-radius:3px;}"
                        "QPushButton:checked{background:#8e44ad;color:white;}"
                        "QPushButton:hover:!checked{background:#e8daef;}")
                else:
                    inv_btn.setToolTip("Ingen invers hittades för denna orsak")
                    inv_btn.setStyleSheet(
                        "QPushButton{font-size:10px;padding:2px 4px;border:1px solid #ccc;"
                        "border-radius:3px;color:#aaa;background:#f5f5f5;}")

                ref_btn.toggled.connect(
                    self._make_ref_handler(cid, orig, inv_btn, cid, c_pos))
                if has_inv:
                    inv_btn.toggled.connect(
                        self._make_inv_handler(cid, inv_text, ref_btn, cid, c_pos))

                row_h.addWidget(ref_btn)
                row_h.addWidget(inv_btn)
                inner_layout.addWidget(row_w)

        inner_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        # ── Summary ───────────────────────────────────────────────────────────
        self._summary_lbl = QLabel("Inga orsaker markerade — tryck 'Hoppa över' för att gå direkt till P&ID.")
        self._summary_lbl.setStyleSheet("color:gray;font-style:italic;font-size:11px;")
        layout.addWidget(self._summary_lbl)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._create_btn = QPushButton("✔ Skapa markerade och fortsätt till P&ID")
        self._create_btn.setEnabled(False)
        self._create_btn.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border:none;border-radius:4px;"
            "padding:6px 12px;font-weight:bold;}"
            "QPushButton:hover:enabled{background:#2ecc71;}"
            "QPushButton:disabled{background:#aaa;}")
        self._create_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._create_btn, 1)

        skip_btn = QPushButton("Hoppa över →")
        skip_btn.setToolTip("Gå direkt till P&ID utan att skapa orsaker härifrån")
        skip_btn.setStyleSheet(
            "QPushButton{border:1px solid #aaa;border-radius:4px;padding:6px 10px;}"
            "QPushButton:hover{background:#f0f0f0;}")
        skip_btn.clicked.connect(lambda: self.done(self.SKIP))
        btn_row.addWidget(skip_btn)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.setStyleSheet(
            "QPushButton{border:1px solid #aaa;border-radius:4px;padding:6px 10px;}"
            "QPushButton:hover{background:#f0f0f0;}")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_ref_handler(self, cid, description, inv_btn, original_cause_id, sort_pos):
        def handler(checked):
            if checked:
                self._selections[cid] = ('ref', description, original_cause_id, sort_pos)
                if inv_btn is not None:
                    inv_btn.blockSignals(True)
                    inv_btn.setChecked(False)
                    inv_btn.blockSignals(False)
            else:
                self._selections.pop(cid, None)
            self._update_summary()
        return handler

    def _make_inv_handler(self, cid, inv_text, ref_btn, original_cause_id, sort_pos):
        def handler(checked):
            if checked:
                self._selections[cid] = ('inv', inv_text, original_cause_id, sort_pos)
                ref_btn.blockSignals(True)
                ref_btn.setChecked(False)
                ref_btn.blockSignals(False)
            else:
                self._selections.pop(cid, None)
            self._update_summary()
        return handler

    def _update_summary(self):
        n = len(self._selections)
        if n == 0:
            self._summary_lbl.setText(
                "Inga orsaker markerade — tryck 'Hoppa över' för att gå direkt till P&ID.")
            self._create_btn.setEnabled(False)
        else:
            kinds = {'ref': 0, 'inv': 0}
            for mode, _, _, _ in self._selections.values():
                kinds[mode] += 1
            parts = []
            if kinds['ref']: parts.append(f"{kinds['ref']} referens")
            if kinds['inv']: parts.append(f"{kinds['inv']} invers")
            self._summary_lbl.setText(f"{n} orsak(er) markerade: {', '.join(parts)}")
            self._create_btn.setEnabled(True)

    def get_selections(self):
        """Return (description, original_cause_id) pairs in original list order."""
        sorted_vals = sorted(self._selections.values(), key=lambda v: v[3])
        return [(desc, orig_id) for _, desc, orig_id, _ in sorted_vals]


# ══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database()
        load_matrix(self.db)
        self._markup_undo_stack = []
        self.setWindowTitle(f"HAZOP Tool  —  {self.db.path.name}")
        self.resize(1440, 900)

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = self.addToolBar("Verktyg")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        def act(label, tip, slot):
            a = QAction(label, self); a.setToolTip(tip); a.triggered.connect(slot)
            tb.addAction(a); return a

        act("+ Nod",         "Lägg till ny nod",          lambda: self.tree_panel.add_node())
        act("+ Avvikelse",   "Lägg till ny avvikelse",    lambda: self.tree_panel.add_deviation())
        act("+ Orsak",       "Lägg till ny orsak",        lambda: self.tree_panel.add_cause())
        act("+ Konsekvens",  "Lägg till ny konsekvens",   lambda: self.tree_panel.add_consequence())
        tb.addSeparator()
        act("Ta bort",       "Ta bort markerat",       lambda: self.tree_panel.delete_selected())
        tb.addSeparator()
        act("🔀 Risk Scenario", "Starta risk scenario-guide", self._open_risk_scenario_wizard)
        tb.addSeparator()
        act("📊 Excel",      "Exportera till Excel",   self._export_excel)
        act("📄 PDF",        "Exportera till PDF",     self._export_pdf)

        # ── Status bar ────────────────────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(f"Databas: {self.db.path}")

        # ── Central widget ────────────────────────────────────────────────────
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        # Toggle bar
        toggle_bar = QWidget()
        toggle_bar.setStyleSheet("background:#1F4E79;")
        toggle_lay = QHBoxLayout(toggle_bar)
        toggle_lay.setContentsMargins(8, 4, 8, 4)
        toggle_lay.setSpacing(6)

        self.btn_pid       = QPushButton("🗺  P&ID-vy")
        self.btn_sheet     = QPushButton("📋  Worksheet")
        self.btn_equip     = QPushButton("🔩  Utrustning")
        self.btn_admin     = QPushButton("⚙️  Studiehantering")
        self.btn_settings  = QPushButton("🔧  Inställningar")

        for btn in (self.btn_pid, self.btn_sheet, self.btn_equip,
                    self.btn_admin, self.btn_settings):
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                "QPushButton{color:#fff;background:#2d6ca3;border:none;"
                "border-radius:4px;padding:0 12px;font-weight:bold;}"
                "QPushButton:checked{background:#fff;color:#1F4E79;}")
            toggle_lay.addWidget(btn)

        toggle_lay.addStretch()
        lbl_db = QLabel(f"DB: {self.db.path.name}")
        lbl_db.setStyleSheet("color:#aac;font-size:11px;")
        toggle_lay.addWidget(lbl_db)
        root_layout.addWidget(toggle_bar)

        self.btn_pid.setChecked(True)
        self.btn_pid.clicked.connect(lambda: self._switch_view(0))
        self.btn_sheet.clicked.connect(lambda: self._switch_view(1))
        self.btn_equip.clicked.connect(lambda: self._switch_view(2))
        self.btn_admin.clicked.connect(lambda: self._switch_view(3))
        self.btn_settings.clicked.connect(lambda: self._switch_view(4))

        # View stack
        self.view_stack = QStackedWidget()
        root_layout.addWidget(self.view_stack)

        # ── Page 0: P&ID view ─────────────────────────────────────────────────
        pid_page = QWidget()
        pid_layout = QVBoxLayout(pid_page)
        pid_layout.setContentsMargins(0, 0, 0, 0)
        pid_layout.setSpacing(0)

        self._v_splitter = QSplitter(Qt.Orientation.Vertical)
        pid_layout.addWidget(self._v_splitter)

        self._h_splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tree_panel = TreePanel(self.db)
        self.tree_panel.setMinimumWidth(220)
        self.tree_panel.setMaximumWidth(340)
        self._h_splitter.addWidget(self.tree_panel)

        self.pid_panel = PIDPanel(self.db)
        self.pid_panel.setMinimumWidth(400)
        self._h_splitter.addWidget(self.pid_panel)

        self._right_scroll = QScrollArea()
        self._right_scroll.setWidgetResizable(True)
        self._right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.stack = QStackedWidget()
        self.welcome_panel    = WelcomePanel()
        self.node_panel       = NodePanel(self.db)
        self.deviation_panel  = DeviationPanel(self.db)
        self.cause_panel      = CausePanel(self.db)
        self.cons_panel       = ConsequencePanel(self.db)
        self.sg_panel         = SafeguardPanel(self.db)
        for panel in [self.welcome_panel, self.node_panel, self.deviation_panel,
                      self.cause_panel, self.cons_panel, self.sg_panel]:
            self.stack.addWidget(panel)
        self._right_scroll.setWidget(self.stack)
        self._h_splitter.addWidget(self._right_scroll)

        self.node_markup_panel = NodeMarkupPanel(self.db)
        self.node_markup_panel.setVisible(False)
        self._h_splitter.addWidget(self.node_markup_panel)

        self._h_splitter.setSizes([260, 650, 370, 0])
        self._v_splitter.addWidget(self._h_splitter)

        self.scenario_panel = ScenarioTablePanel(self.db)
        self._v_splitter.addWidget(self.scenario_panel)

        self.markup_table_panel = MarkupTablePanel(self.db)
        self.markup_table_panel.setVisible(False)
        self._v_splitter.addWidget(self.markup_table_panel)

        self._v_splitter.setSizes([640, 220, 0])
        self.view_stack.addWidget(pid_page)

        # ── Page 1: Worksheet ─────────────────────────────────────────────────
        self.worksheet = HAZOPWorksheet(self.db)
        self.view_stack.addWidget(self.worksheet)

        # ── Page 2: Equipment ─────────────────────────────────────────────────
        self.equipment_panel = EquipmentPanel(self.db)
        self.view_stack.addWidget(self.equipment_panel)

        # ── Page 3: Study management ──────────────────────────────────────────
        self.admin_panel = StudyManagementPanel(self.db)
        self.view_stack.addWidget(self.admin_panel)

        # ── Page 4: Settings ──────────────────────────────────────────────────
        self.settings_panel = SettingsPanel(self.db)
        self.settings_panel.matrix_changed.connect(self._on_matrix_changed)
        self.view_stack.addWidget(self.settings_panel)

        # ── Undo shortcut (Ctrl+Z) — only active during markup editing ────────
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.setEnabled(False)
        self._undo_shortcut.activated.connect(self._undo_last_markup)

        # ── Wire signals ──────────────────────────────────────────────────────
        self.tree_panel.item_selected.connect(self._on_selected)
        self.tree_panel.structure_changed.connect(self._on_structure_changed)
        self.tree_panel.visibility_changed.connect(
            lambda t, v: self.pid_panel.viewer.set_marker_visibility(t, v))

        self.node_panel.saved.connect(
            lambda id_, name: (
                self.tree_panel.refresh(NODE_T, id_),
                self.db.sync_node_text_markups(id_, name),
                self.pid_panel.refresh_markup_overlays(),
            ))
        self.deviation_panel.saved.connect(
            lambda id_, _: self.tree_panel.refresh(DEV_T, id_))
        self.deviation_panel.add_cause_requested.connect(self._on_deviation_add_cause)
        self.cause_panel.saved.connect(
            lambda id_, _: (self.tree_panel.refresh(CAUSE_T, id_),
                            self.scenario_panel.load_cause(id_)))
        self.cons_panel.saved.connect(
            lambda id_: (self.tree_panel.refresh(CONS_T, id_),
                         self.scenario_panel.load_consequence(id_)))
        self.sg_panel.saved.connect(
            lambda id_: self.tree_panel.refresh(SG_T, id_))

        self.cause_panel.place_on_pid.connect(
            lambda: self.pid_panel._set_mode(MODE_CAUSE))
        self.cons_panel.place_on_pid.connect(
            lambda: self.pid_panel._set_mode(MODE_CONSEQUENCE))
        self.sg_panel.place_on_pid.connect(
            lambda: self.pid_panel._set_mode(MODE_SAFEGUARD))

        self.scenario_panel.item_selected.connect(self._on_selected)
        self.scenario_panel.new_item_created.connect(
            lambda type_, id_: (self.tree_panel.refresh(type_, id_),
                                self._on_selected(type_, id_)))
        self.scenario_panel.item_edited.connect(self._on_scenario_item_edited)
        self.scenario_panel.place_requested.connect(self._on_scenario_place_requested)
        self.scenario_panel.navigate_to_pid.connect(self._on_scenario_navigate_to_pid)
        self.scenario_panel.remove_requested.connect(self._on_scenario_remove_from_pid)

        self.tree_panel.add_causes_on_pid_requested.connect(self._on_add_causes_on_pid)
        self.tree_panel.add_consequences_on_pid_requested.connect(
            self._on_add_consequences_on_pid)
        self.tree_panel.add_safeguards_on_pid_requested.connect(
            self._on_add_safeguards_on_pid)
        self.tree_panel.edit_node_markup_requested.connect(self._on_edit_node_markup)
        self.tree_panel.node_markup_vis_requested.connect(self._on_node_markup_vis)
        self.tree_panel.node_jump_to_markup.connect(self._on_jump_to_node_markup)

        # Node markup ribbon signals
        self.node_markup_panel.closed.connect(self._on_close_node_markup)
        self.node_markup_panel.tool_changed.connect(
            lambda t: self.pid_panel.set_markup_tool(
                t, *self.node_markup_panel.get_current_style()[:3]))
        self.node_markup_panel.all_vis_toggled.connect(
            lambda _: self.pid_panel.refresh_markup_overlays())
        self.node_markup_panel.style_changed.connect(
            lambda color, opacity, width: self.pid_panel.viewer.set_pen_style(
                color, width, int(opacity * 210)))
        self.node_markup_panel.snap_changed.connect(
            self.pid_panel.viewer.set_snap)
        # Markup table panel signals
        self.markup_table_panel.item_deleted.connect(
            lambda _: self.pid_panel.refresh_markup_overlays())
        self.markup_table_panel.item_vis_toggled.connect(
            lambda mu_id, vis: self.pid_panel.viewer.set_markup_item_visible(mu_id, vis))
        self.markup_table_panel.item_selected.connect(
            lambda mu_id: self.pid_panel.viewer.highlight_markup(mu_id))

        self.pid_panel.markup_label_edited.connect(self._on_markup_label_edited)
        self.pid_panel.markup_duplicate_requested.connect(self._on_duplicate_markup)
        self.markup_table_panel.item_style_changed.connect(
            lambda _: self.pid_panel.refresh_markup_overlays())
        self.markup_table_panel.item_duplicated.connect(self._on_duplicate_markup)
        self.pid_panel.markup_draw_finished.connect(self._on_markup_draw_finished)
        self.pid_panel.markup_moved.connect(self._on_markup_moved)
        self.pid_panel.markup_item_selected.connect(
            self.markup_table_panel.select_markup)
        self.tree_panel.exit_pid_mode_requested.connect(
            lambda: self.pid_panel._set_mode(MODE_NAV))

        self.pid_panel.node_created.connect(
            lambda nid: (self.tree_panel.refresh(NODE_T, nid),
                         self._on_selected(NODE_T, nid)))
        self.pid_panel.cause_created.connect(
            lambda cid: (self.tree_panel.refresh(CAUSE_T, cid),
                         self._on_selected(CAUSE_T, cid),
                         self.scenario_panel.refresh_placed()))
        self.pid_panel.consequence_created.connect(
            lambda cid: (self.tree_panel.refresh(CONS_T, cid),
                         self._on_selected(CONS_T, cid),
                         self.scenario_panel.refresh_placed()))
        self.pid_panel.safeguard_created.connect(self._on_safeguard_created)
        self.pid_panel.existing_marker_placed.connect(self._on_existing_marker_placed)
        self.pid_panel.cause_template_created.connect(
            lambda cid: (self.tree_panel.refresh(CAUSE_T, cid),
                         self._on_selected(CAUSE_T, cid),
                         self.scenario_panel.refresh_placed()))
        self.pid_panel.risk_scenario_requested.connect(self._on_pid_risk_scenario)
        self.pid_panel.marker_navigated.connect(self._on_marker_navigate)
        self.pid_panel.pid_analysis_done.connect(self._on_pid_analysis_done)

        self._cur_type = None
        self._cur_id   = None

        self.tree_panel.refresh()
        self.pid_panel.try_reload_pdf()

    def _switch_view(self, page):
        self.view_stack.setCurrentIndex(page)
        self.btn_pid.setChecked(page == 0)
        self.btn_sheet.setChecked(page == 1)
        self.btn_equip.setChecked(page == 2)
        self.btn_admin.setChecked(page == 3)
        self.btn_settings.setChecked(page == 4)
        if page == 1: self.worksheet.refresh()
        if page == 2: self.equipment_panel.refresh()
        if page == 3:
            self.admin_panel.refresh()
            self.admin_panel.refresh_pid()

    def _on_selected(self, type_, id_):
        self._cur_type = type_
        self._cur_id   = id_
        if type_ == NODE_T:
            self.node_panel.load(id_)
            self.stack.setCurrentWidget(self.node_panel)
            self.pid_panel.set_active_node(id_)
            self.scenario_panel.load_node(id_)
        elif type_ == DEV_T:
            self.deviation_panel.load(id_)
            self.stack.setCurrentWidget(self.deviation_panel)
            dev = self.db.get_deviation(id_)
            if dev:
                self.pid_panel.set_active_node(dev['node_id'])
            self.scenario_panel.load_deviation(id_)
        elif type_ == CAUSE_T:
            self.cause_panel.load(id_)
            self.stack.setCurrentWidget(self.cause_panel)
            self.pid_panel.set_active_cause(id_)
            self.scenario_panel.load_cause(id_)
        elif type_ == CONS_T:
            self.cons_panel.load(id_)
            self.stack.setCurrentWidget(self.cons_panel)
            self.pid_panel.set_active_consequence(id_)
            self.scenario_panel.load_consequence(id_)
        elif type_ == SG_T:
            self.sg_panel.load(id_)
            self.stack.setCurrentWidget(self.sg_panel)
            sg = self.db.get_safeguard(id_)
            if sg:
                cons = self.db.get_consequence(sg['consequence_id'])
                if cons:
                    self.pid_panel.set_active_consequence(cons['id'])
                    self.scenario_panel.load_consequence(cons['id'])

    def _on_deviation_add_cause(self, dev_id):
        new_id = self.db.add_cause(dev_id)
        self.tree_panel.refresh(CAUSE_T, new_id)
        self.tree_panel.structure_changed.emit()

    def _on_scenario_item_edited(self, type_, id_):
        """Scenario table committed an edit — sync the tree and the right panel."""
        self.tree_panel.refresh(type_, id_)
        if type_ == CAUSE_T:
            if self.cause_panel.cause_id == id_:
                self.cause_panel.load(id_)
        elif type_ == CONS_T:
            if self.cons_panel.consequence_id == id_:
                self.cons_panel.load(id_)
        elif type_ == SG_T:
            if self.sg_panel.safeguard_id == id_:
                self.sg_panel.load(id_)

    def _on_structure_changed(self):
        self._cur_type = None
        self._cur_id   = None
        self.stack.setCurrentWidget(self.welcome_panel)
        self.scenario_panel.clear()

    def _on_pid_analysis_done(self):
        """Switch to Settings → Identifierade objekt after P&ID analysis."""
        self._switch_view(4)   # Settings page
        self.settings_panel.analysis_panel.refresh()
        # Switch to the "Identifierade objekt" tab inside settings
        tabs = self.settings_panel.findChild(QTabWidget)
        if tabs:
            for i in range(tabs.count()):
                if "Identifierade" in tabs.tabText(i):
                    tabs.setCurrentIndex(i)
                    break

    def _on_marker_navigate(self, item_type: str, item_id: int):
        """Navigate tree and detail panel when a P&ID marker is clicked."""
        type_map = {'cause': CAUSE_T, 'consequence': CONS_T, 'safeguard': SG_T}
        t = type_map.get(item_type)
        if t is None:
            return
        self.tree_panel.refresh(t, item_id)
        self._on_selected(t, item_id)

    def _on_safeguard_created(self, _sg_id):
        if self._cur_type == CONS_T and self._cur_id is not None:
            self.cons_panel.load(self._cur_id)
            self.scenario_panel.load_consequence(self._cur_id)
            self.tree_panel.refresh(CONS_T, self._cur_id)
        self.scenario_panel.refresh_placed()

    def _on_scenario_place_requested(self, type_, id_):
        """User clicked red pin or context menu 'Lägg till på P&ID' — fast path, no panel reload."""
        # Set only what the P&ID panel needs; skip full _on_selected to avoid heavy reloads
        if type_ == CAUSE_T:
            self.pid_panel.set_active_cause(id_)
        elif type_ == CONS_T:
            self.pid_panel.set_active_consequence(id_)
        elif type_ == SG_T:
            sg = self.db.get_safeguard(id_)
            if sg:
                self.pid_panel.set_active_consequence(sg['consequence_id'])
        self._switch_view(0)
        type_str = {CAUSE_T: 'cause', CONS_T: 'consequence', SG_T: 'safeguard'}.get(type_)
        if type_str:
            self.pid_panel.start_place_existing(type_str, id_)

    def _on_scenario_navigate_to_pid(self, type_, id_):
        """User clicked green pin — switch to P&ID view and zoom to the marker."""
        marker = None
        if type_ == CAUSE_T:
            marker = self.db.get_cause_marker(id_)
        elif type_ == CONS_T:
            marker = self.db.get_consequence_marker(id_)
        elif type_ == SG_T:
            marker = self.db.get_safeguard_marker(id_)
        if not marker:
            return
        self._on_selected(type_, id_)
        self._switch_view(0)
        self.pid_panel.navigate_to_marker(marker['pid_page'], marker['x'], marker['y'])

    def _on_scenario_remove_from_pid(self, type_, id_):
        """Context menu 'Ta bort från P&ID' — delete all markers for this item."""
        type_str = {CAUSE_T: 'cause', CONS_T: 'consequence', SG_T: 'safeguard'}.get(type_)
        if not type_str:
            return
        self.pid_panel.remove_existing_marker(type_str, id_)
        self.scenario_panel.refresh_placed()
        self.tree_panel.refresh()

    def _on_add_causes_on_pid(self, deviation_id):
        """Right-click deviation → 'Lägg till orsaker på P&ID'.

        Shows a pre-dialog listing causes from other deviations in the same node
        (with hierarchical ref-labels) so the user can reference/invert them before
        entering P&ID placement mode.
        """
        dev = self.db.get_deviation(deviation_id)
        if not dev:
            return
        node_id  = dev['node_id']
        dev_name = dev['description']

        # ── Build hierarchical reference labels ───────────────────────────────
        all_nodes = self.db.nodes()
        node_idx  = next((i + 1 for i, n in enumerate(all_nodes) if n['id'] == node_id), 1)

        all_devs     = self.db.deviations(node_id)
        dev_pos_map  = {d['id']: i + 1 for i, d in enumerate(all_devs)}

        cause_pos_map: dict = {}
        for d in all_devs:
            for j, c in enumerate(self.db.causes_for_deviation(d['id'])):
                cause_pos_map[c['id']] = j + 1

        raw = self.db.causes_for_node_excluding_deviation(node_id, deviation_id)
        existing_causes = []
        for c in raw:
            cd = dict(c)
            dp = dev_pos_map.get(cd['deviation_id'], 0)
            cp = cause_pos_map.get(cd['id'], 0)
            cd['dev_label'] = f"{node_idx}.{dp}"
            cd['ref_label'] = f"{node_idx}.{dp}.{cp}"
            existing_causes.append(cd)

        # ── Show pre-dialog if there are causes to reuse ──────────────────────
        if existing_causes:
            dlg = ReuseDeviationCausesDialog(dev_name, existing_causes, parent=self)
            result = dlg.exec()
            if result == QDialog.DialogCode.Rejected:
                return   # user cancelled — do not enter P&ID mode
            if result == QDialog.DialogCode.Accepted:
                markers_need_reload = False
                for desc, orig_cause_id in dlg.get_selections():
                    new_cid = self.db.add_cause(deviation_id)
                    self.db.update_cause(new_cid, desc)
                    # Copy P&ID markers from the original cause (same physical component)
                    if orig_cause_id is not None:
                        for m in self.db.cause_markers_for_cause(orig_cause_id):
                            self.db.add_cause_marker(
                                new_cid, m['pid_page'], m['x'], m['y'],
                                m['component_type'], m['component_tag'])
                            markers_need_reload = True
                self.tree_panel.refresh()
                if markers_need_reload:
                    self.pid_panel.reload_overlays()

        # ── Enter P&ID placement mode ─────────────────────────────────────────
        self.pid_panel.set_active_node(node_id)
        self._switch_view(0)
        self.pid_panel.start_cause_template_mode(deviation_id)

    def _on_add_consequences_on_pid(self, cause_id):
        """Right-click cause → 'Lägg till konsekvens på P&ID'."""
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        node_id = cause['node_id']
        self.pid_panel.set_active_node(node_id)
        self.pid_panel.set_active_cause(cause_id)
        self._switch_view(0)
        self.pid_panel._set_mode(MODE_CONSEQUENCE)

    def _on_add_safeguards_on_pid(self, cons_id):
        """Right-click consequence → 'Lägg till safeguard på P&ID'."""
        cons = self.db.get_consequence(cons_id)
        if not cons:
            return
        cause = self.db.get_cause(cons['cause_id'])
        node_id = cause['node_id'] if cause else None
        if node_id:
            self.pid_panel.set_active_node(node_id)
        self.pid_panel.set_active_consequence(cons_id)
        self._switch_view(0)
        self.pid_panel._set_mode(MODE_SAFEGUARD)

    def _on_edit_node_markup(self, node_id):
        """Tree right-click NODE → 'Editera nodmarkup'."""
        self._switch_view(0)
        self.node_markup_panel.load(node_id)
        self.markup_table_panel.load(node_id)
        self.tree_panel.setVisible(False)
        self._right_scroll.setVisible(False)
        self.node_markup_panel.setVisible(True)
        self.scenario_panel.setVisible(False)
        self.markup_table_panel.setVisible(True)
        self._h_splitter.setSizes([0, 800, 0, 64])
        self._v_splitter.setSizes([560, 0, 200])
        self.pid_panel.enter_markup_edit(node_id)
        self._markup_undo_stack.clear()
        self._undo_shortcut.setEnabled(True)

    def _on_close_node_markup(self):
        """Ribbon close button clicked — leave markup edit mode."""
        self.pid_panel.exit_markup_mode()
        self.pid_panel.reload_overlays()
        self.tree_panel.setVisible(True)
        self._right_scroll.setVisible(True)
        self.node_markup_panel.setVisible(False)
        self.scenario_panel.setVisible(True)
        self.markup_table_panel.setVisible(False)
        self._h_splitter.setSizes([260, 650, 370, 0])
        self._v_splitter.setSizes([640, 220, 0])
        self.stack.setCurrentWidget(self.welcome_panel)
        self._markup_undo_stack.clear()
        self._undo_shortcut.setEnabled(False)

    def _on_node_markup_vis(self, node_id, visible):
        """Tree context menu hide/show all markups for a node."""
        self.db.set_all_node_markups_visible(node_id, visible)
        self.pid_panel.refresh_markup_overlays()
        if self.markup_table_panel.isVisible() and self.markup_table_panel.node_id == node_id:
            self.markup_table_panel.refresh()

    def _on_markup_draw_finished(self, type_, node_id, pts, page, label):
        """New markup drawn on P&ID — save to DB and refresh."""
        color, opacity, line_width, font_size = self.node_markup_panel.get_current_style()
        mu_id = self.db.add_node_markup(
            node_id, type_, pts, label, color, opacity, line_width, page, font_size)
        self._markup_undo_stack.append({'op': 'draw', 'mu_id': mu_id})
        self.pid_panel.viewer._pending_path_item = None
        self.pid_panel.refresh_markup_overlays()
        self.markup_table_panel.refresh()
        self.markup_table_panel.select_markup(mu_id)

    def _on_markup_moved(self, mu_id, new_pts):
        """Markup item dragged to new position — save to DB, push undo entry."""
        old_row = self.db.get_node_markup(mu_id)
        if old_row:
            old_pts = json.loads(dict(old_row).get('points', '[]') or '[]')
            self._markup_undo_stack.append({'op': 'move', 'mu_id': mu_id, 'old_pts': old_pts})
        self.db.update_node_markup(mu_id, points=new_pts)
        self.markup_table_panel.refresh()

    def _on_markup_label_edited(self, mu_id, new_label):
        self.db.update_node_markup(mu_id, label=new_label)
        self.pid_panel.refresh_markup_overlays()
        self.markup_table_panel.refresh()

    def _on_jump_to_node_markup(self, node_id):
        """Double-click node with markups in tree → enter markup edit mode and zoom to items."""
        self._on_edit_node_markup(node_id)
        markups = self.db.node_markups_for_node(node_id)
        if not markups:
            return
        markups = [dict(m) for m in markups]
        mu_ids = [m['id'] for m in markups]

        # Navigate to the physical page of the first markup
        phys_page = markups[0].get('pid_page', 0)
        sheet_map = self.pid_panel._sheet_map  # display_index → physical_page
        if sheet_map:
            display_n = next((k for k, v in sheet_map.items() if v == phys_page), 0)
        else:
            display_n = phys_page
        self.pid_panel._goto_page(display_n)

        # Zoom to markup bounding box (overlays already loaded by enter_markup_edit)
        self.pid_panel.viewer.zoom_to_markup_items(mu_ids)

    def _on_duplicate_markup(self, mu_id):
        mu = self.db.get_node_markup(mu_id)
        if not mu:
            return
        mu = dict(mu)
        pts = json.loads(mu.get('points', '[]') or '[]')
        offset_pts = [[p[0] + 20, p[1] + 20] if len(p) >= 2 else p for p in pts]
        new_id = self.db.add_node_markup(
            node_id=mu['node_id'],
            type_=mu['type'],
            pts=offset_pts,
            label=mu['label'] + ' (kopia)',
            color=mu['color'],
            opacity=float(mu['opacity']),
            line_width=int(mu['line_width']),
            page=mu['pid_page'],
            font_size=int(mu['font_size']))
        self._markup_undo_stack.append({'op': 'draw', 'mu_id': new_id})
        self.pid_panel.refresh_markup_overlays()
        self.markup_table_panel.refresh()
        self.markup_table_panel.select_markup(new_id)

    def _undo_last_markup(self):
        if not self._markup_undo_stack:
            return
        entry = self._markup_undo_stack.pop()
        if entry['op'] == 'draw':
            self.db.delete_node_markup(entry['mu_id'])
            self.pid_panel.refresh_markup_overlays()
            self.markup_table_panel.refresh()
        elif entry['op'] == 'move':
            self.db.update_node_markup(entry['mu_id'], points=entry['old_pts'])
            self.pid_panel.refresh_markup_overlays()
            self.markup_table_panel.refresh()

    def _on_existing_marker_placed(self, type_str, id_):
        """Marker placed via 'place existing' flow — refresh pins and tree without reloading panels."""
        type_ = {'cause': CAUSE_T, 'consequence': CONS_T, 'safeguard': SG_T}.get(type_str)
        if type_ is not None:
            self.tree_panel.refresh(type_, id_)
        self.scenario_panel.refresh_placed()

    def _on_matrix_changed(self):
        if self._cur_type == CONS_T and self._cur_id is not None:
            self.cons_panel.load(self._cur_id)
        self.tree_panel.refresh()
        if self._cur_type == CAUSE_T and self._cur_id:
            self.scenario_panel.load_cause(self._cur_id)

    def _open_risk_scenario_wizard(self, node_id=None):
        """Start guided Risk Scenario mode using existing P&ID dialogs."""
        # Resolve node from current selection if not supplied
        if not node_id:
            if self._cur_type in (NODE_T, CAUSE_T, CONS_T, SG_T) and self._cur_id:
                node_id = self.tree_panel._resolve_node_id(self._cur_type, self._cur_id)
        if not node_id:
            nodes = self.db.nodes()
            if not nodes:
                QMessageBox.information(self, "Ingen nod",
                    "Lägg till en nod i trädet innan du startar Risk Scenario.")
                return
            node_id = nodes[0]['id']

        # Switch to P&ID view if not already there
        self._switch_view(0)

        # Start guided mode in PIDPanel
        self.pid_panel.start_scenario_mode(node_id)
        self.status_bar.showMessage(
            "Risk Scenario startat — följ stegen i bannern ovan P&ID:n.", 5000)

    def _on_pid_risk_scenario(self, node_id, pos, page):
        self._open_risk_scenario_wizard(node_id)

    def _export_excel(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportera Excel", "hazop_rapport.xlsx", "Excel (*.xlsx)")
        if not path: return
        ok, err = export_excel(self.db, path)
        if ok:
            self.status_bar.showMessage(f"Excel sparad: {path}", 6000)
            QMessageBox.information(self, "Klar", f"Exporterad till:\n{path}")
        else:
            QMessageBox.critical(self, "Fel vid export", err)

    def _export_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportera PDF", "hazop_rapport.pdf", "PDF (*.pdf)")
        if not path: return
        ok, err = export_pdf(self.db, path)
        if ok:
            self.status_bar.showMessage(f"PDF sparad: {path}", 6000)
            QMessageBox.information(self, "Klar", f"Exporterad till:\n{path}")
        else:
            QMessageBox.critical(self, "Fel vid export", err)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
