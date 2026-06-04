#!/usr/bin/env python3
"""HAZOP Tool — Hazard and Operability Study Manager v2"""

import sys
import re
import json
import sqlite3
import math
from pathlib import Path

from pid_viewer import (
    PIDPanel, COMPONENT_TYPES, CONSEQUENCE_TEMPLATES, HAS_PYMUPDF,
    MODE_NAV, MODE_NODE, MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD,
    scan_pdf_for_equipment, ocr_status, KNOWN_PREFIXES,
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
    QSpinBox, QColorDialog, QFrame, QListWidget, QListWidgetItem,
    QProgressDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QPointF, QRectF, QTimer
from PyQt6.QtGui import QFont, QColor, QAction, QBrush, QPen, QPainter

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
    pid_ref     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS causes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    description TEXT NOT NULL DEFAULT 'Ny orsak',
    likelihood  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS consequences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cause_id    INTEGER NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    description TEXT NOT NULL DEFAULT 'Ny konsekvens',
    severity    INTEGER NOT NULL DEFAULT 1,
    category    TEXT DEFAULT ''
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


def load_matrix(db):
    global _current_matrix
    cfg = db.get_risk_matrix()
    _current_matrix = cfg if cfg else DEFAULT_MATRIX


def get_matrix():
    return _current_matrix or DEFAULT_MATRIX


def risk_info(frequency, consequence):
    """Return (label, bg_color, fg_color) from matrix lookup.

    frequency  : integer -1..5  (stored in causes.likelihood)
    consequence: integer  1..5  (stored in consequences.severity)
    No S×L — direct matrix cell lookup only.
    """
    cfg  = get_matrix()
    rows = cfg.get('rows', 5)
    cols = cfg.get('cols', 7)
    c_idx = max(0, min(int(consequence) - 1, rows - 1))
    f_idx = max(0, min(int(frequency) + 1, cols - 1))   # F=-1 → col 0
    if cfg.get('x_axis', 'frequency') == 'frequency':
        row_idx, col_idx = c_idx, f_idx
    else:
        row_idx, col_idx = f_idx, c_idx
    try:
        color = cfg['cell_colors'][row_idx][col_idx]
        label = cfg['cell_labels'][row_idx][col_idx]
    except (IndexError, KeyError):
        color, label = '#27ae60', 'Låg'
    return label, color, '#ffffff'


def effective_frequency(base_freq, rrf):
    """Reduce frequency by floor(log10(rrf)) steps; minimum F=-1."""
    if rrf <= 1:
        return base_freq
    reduction = int(math.log10(max(1, rrf)))
    return max(-1, base_freq - reduction)


# Keep old name as alias so any remaining callers don't crash immediately
effective_likelihood = effective_frequency


def total_freq_reduction(base_freq: int, safeguard_rrf: int,
                         fa_active: bool, fa_rrf: int,
                         ignition_active: bool, ignition_rrf: int,
                         extra_rfactors) -> tuple:
    """Return (final_freq, total_rrf, step_reduction).

    extra_rfactors: iterable of dicts with 'rrf' and 'active'.
    """
    total_rrf = safeguard_rrf
    if fa_active and fa_rrf > 1:
        total_rrf *= fa_rrf
    if ignition_active and ignition_rrf > 1:
        total_rrf *= ignition_rrf
    for rf in extra_rfactors:
        if rf.get('active') and rf.get('rrf', 1) > 1:
            total_rrf *= rf['rrf']
    reduction = int(math.log10(max(1, total_rrf)))
    return max(-1, base_freq - reduction), total_rrf, reduction


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
_RISK_ICON   = {'Låg': '🟢', 'Medium': '🟡', 'Hög': '🟠', 'Kritisk': '🔴'}


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
            "ALTER TABLE causes ADD COLUMN likelihood INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE causes ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE safeguards ADD COLUMN rrf INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE safeguards ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE consequences ADD COLUMN category TEXT DEFAULT ''",
            "ALTER TABLE consequences ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE consequences ADD COLUMN fa_active INTEGER DEFAULT 0",
            "ALTER TABLE consequences ADD COLUMN fa_rrf INTEGER DEFAULT 10",
            "ALTER TABLE consequences ADD COLUMN ignition_active INTEGER DEFAULT 0",
            "ALTER TABLE consequences ADD COLUMN ignition_rrf INTEGER DEFAULT 10",
            "ALTER TABLE cause_markers ADD COLUMN component_tag TEXT DEFAULT ''",
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
        """)

        if not self.conn.execute("SELECT COUNT(*) FROM consequence_categories").fetchone()[0]:
            for i, name in enumerate(['Person', 'Miljö', 'Ekonomi', 'Anläggning', 'Rykte']):
                self.conn.execute(
                    "INSERT INTO consequence_categories (name, sort_order) VALUES (?,?)", (name, i))
        self.conn.commit()

    # ── Config ────────────────────────────────────────────────────────────────
    def get_config(self, key, default=None):
        row = self.conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default

    def set_config(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO app_config (key,value) VALUES (?,?)", (key, value))
        self.conn.commit()

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

    # ── P&ID helpers ──────────────────────────────────────────────────────────
    def get_pid_path(self):
        row = self.conn.execute("SELECT value FROM pid_config WHERE key='path'").fetchone()
        return row['value'] if row else None

    def set_pid_path(self, path):
        self.conn.execute(
            "INSERT OR REPLACE INTO pid_config (key,value) VALUES ('path',?)", (str(path),))
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
        self.conn.commit()
        return cur.lastrowid

    def add_cause(self, node_id):
        cur = self.conn.execute(
            "INSERT INTO causes (node_id,description,likelihood) VALUES (?,'Ny orsak',1)", (node_id,))
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
    def update_node(self, id_, name, description, pid_ref):
        self.conn.execute("UPDATE nodes SET name=?,description=?,pid_ref=? WHERE id=?",
                          (name, description, pid_ref, id_))
        self.conn.commit()

    def update_cause(self, id_, description=None, likelihood=None):
        if description is not None and likelihood is not None:
            self.conn.execute("UPDATE causes SET description=?,likelihood=? WHERE id=?",
                              (description, likelihood, id_))
        elif description is not None:
            self.conn.execute("UPDATE causes SET description=? WHERE id=?", (description, id_))
        elif likelihood is not None:
            self.conn.execute("UPDATE causes SET likelihood=? WHERE id=?", (likelihood, id_))
        self.conn.commit()

    def update_consequence(self, id_, description, severity, category=''):
        self.conn.execute("UPDATE consequences SET description=?,severity=?,category=? WHERE id=?",
                          (description, severity, category, id_))
        self.conn.commit()

    def update_safeguard(self, id_, description, rrf=1):
        self.conn.execute("UPDATE safeguards SET description=?,rrf=? WHERE id=?",
                          (description, rrf, id_))
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
    def copy_cause(self, cause_id, target_node_id):
        orig = self.get_cause(cause_id)
        if not orig:
            return None
        cur = self.conn.execute(
            "INSERT INTO causes (node_id,description,likelihood,source_id) VALUES (?,?,?,?)",
            (target_node_id, orig['description'], orig['likelihood'], cause_id))
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
                "INSERT INTO safeguards (consequence_id,description,rrf,source_id) VALUES (?,?,?,?)",
                (new_id, sg['description'], sg['rrf'], sg['id']))
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
            "INSERT INTO safeguards (consequence_id,description,rrf,source_id) VALUES (?,?,?,?)",
            (target_cons_id, orig['description'], orig['rrf'], sg_id))
        self.conn.commit()
        return cur.lastrowid

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
        self.setFixedSize(150, 26)
        f = QFont(); f.setBold(True)
        self.setFont(f)
        self.update_risk(1, 1)

    def update_risk(self, frequency, consequence):
        label, bg, fg = risk_info(frequency, consequence)
        self.setText(f"{label}  F={frequency} C={consequence}")
        self.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:5px; padding:2px 8px;")


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

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Beskrivning', 'RRF', 'Eff. risk', ''])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 130)
        self.table.setColumnWidth(3, 72)
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

            item = QTableWidgetItem(sg['description'])
            item.setData(Qt.ItemDataRole.UserRole, sg['id'])
            self.table.setItem(row, 0, item)

            rrf_combo = QComboBox()
            rrf_combo.addItems(_RRF_LABELS)
            rrf_idx = _RRF_VALUES.index(sg['rrf']) if sg['rrf'] in _RRF_VALUES else 0
            rrf_combo.setCurrentIndex(rrf_idx)
            sid = sg['id']
            rrf_combo.currentIndexChanged.connect(
                lambda idx, s=sid, r=row: self._rrf_changed(s, r, idx))
            self.table.setCellWidget(row, 1, rrf_combo)

            eff_f = effective_frequency(self._parent_cause_likelihood, sg['rrf'])
            badge = RiskBadge()
            badge.update_risk(eff_f, severity)
            self.table.setCellWidget(row, 2, badge)

            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(lambda _, s=sid: self._delete(s))
            self.table.setCellWidget(row, 3, del_btn)

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

    def _rrf_changed(self, sg_id, row, idx):
        rrf = _RRF_VALUES[idx]
        item = self.table.item(row, 0)
        desc = item.text() if item else ''
        self.db.update_safeguard(sg_id, desc, rrf)
        self._refresh()
        self.changed.emit()

    def _cell_changed(self, row, col):
        if col != 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        sg_id = item.data(Qt.ItemDataRole.UserRole)
        rrf_w = self.table.cellWidget(row, 1)
        rrf = _RRF_VALUES[rrf_w.currentIndex()] if rrf_w else 1
        self.db.update_safeguard(sg_id, item.text(), rrf)


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
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Nod")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(10)
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
        self.desc_edit.setPlaceholderText("Beskrivning av noden...")
        self.desc_edit.setFixedHeight(100)
        self.desc_edit.focusOutEvent = lambda e: (self._save(), QTextEdit.focusOutEvent(self.desc_edit, e))
        form.addRow("Beskrivning:", self.desc_edit)

        layout.addLayout(form)
        layout.addStretch()

    def load(self, node_id):
        self.node_id = node_id
        row = self.db.get_node(node_id)
        if row:
            self._loading = True
            self.name_edit.setText(row['name'])
            self.pid_edit.setText(row['pid_ref'] or '')
            self.desc_edit.setPlainText(row['description'] or '')
            self._loading = False

    def _save(self):
        if self._loading or self.node_id is None:
            return
        name = self.name_edit.text().strip() or 'Ny nod'
        self.db.update_node(self.node_id, name, self.desc_edit.toPlainText(), self.pid_edit.text())
        self.saved.emit(self.node_id, name)


class CausePanel(QWidget):
    saved = pyqtSignal(int, str)

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
        self.desc_edit.setFixedHeight(120)
        self.desc_edit.focusOutEvent = lambda e: (self._save(), QTextEdit.focusOutEvent(self.desc_edit, e))
        form.addRow("Beskrivning:", self.desc_edit)

        self.freq_combo = QComboBox()
        self.freq_combo.addItems(_FREQ_LABELS)
        self.freq_combo.currentIndexChanged.connect(self._save)
        form.addRow("Frekvens (F):", self.freq_combo)

        layout.addLayout(form)
        layout.addStretch()

    def load(self, cause_id):
        self.cause_id = cause_id
        row = self.db.get_cause(cause_id)
        if row:
            self._loading = True
            self.desc_edit.setPlainText(row['description'])
            freq = row['likelihood'] if row['likelihood'] is not None else 3
            self.freq_combo.setCurrentIndex(freq_to_idx(freq))
            self._loading = False

    def _save(self):
        if self._loading or self.cause_id is None:
            return
        desc = self.desc_edit.toPlainText().strip() or 'Ny orsak'
        freq = idx_to_freq(self.freq_combo.currentIndex())
        self.db.update_cause(self.cause_id, desc, freq)
        self.saved.emit(self.cause_id, desc)


class ConsequencePanel(QWidget):
    saved = pyqtSignal(int)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Konsekvens (Consequence)")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(1); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        desc_box = QGroupBox("Beskrivning")
        desc_lay = QVBoxLayout(desc_box)
        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Beskriv konsekvensen...")
        self.desc_edit.setFixedHeight(80)
        self.desc_edit.focusOutEvent = lambda e: (self._save(), QTextEdit.focusOutEvent(self.desc_edit, e))
        desc_lay.addWidget(self.desc_edit)
        layout.addWidget(desc_box)

        risk_box = QGroupBox("Riskbedömning")
        risk_lay = QFormLayout(risk_box)
        risk_lay.setSpacing(8)

        self.sev_combo = QComboBox()
        self.sev_combo.addItems(_SEV_LABELS)
        self.sev_combo.currentIndexChanged.connect(self._risk_changed)
        risk_lay.addRow("Konsekvens (S):", self.sev_combo)

        self.cat_combo = QComboBox()
        self.cat_combo.currentIndexChanged.connect(self._save)
        risk_lay.addRow("Kategori:", self.cat_combo)

        self.risk_badge = RiskBadge()
        risk_lay.addRow("Risknivå (S×L orsak):", self.risk_badge)
        layout.addWidget(risk_box)

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

        layout.addStretch()

    def _load_categories(self):
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItem('')
        for cat in self.db.consequence_categories():
            self.cat_combo.addItem(cat['name'])
        self.cat_combo.blockSignals(False)

    def load(self, consequence_id):
        self.consequence_id = consequence_id
        self._load_categories()
        row = self.db.get_consequence(consequence_id)
        if row:
            self._loading = True
            self.desc_edit.setPlainText(row['description'])
            self.sev_combo.setCurrentIndex(max(0, (row['severity'] or 1) - 1))
            cat = row['category'] or ''
            idx = self.cat_combo.findText(cat)
            self.cat_combo.setCurrentIndex(max(0, idx))
            self._loading = False

        cause_id = dict(row)['cause_id'] if row else None
        freq = 3
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                freq = cause['likelihood'] if cause['likelihood'] is not None else 3
        sev = (row['severity'] or 1) if row else 1
        self.risk_badge.update_risk(freq, sev)
        self.sg_editor.load(consequence_id, freq)
        self.act_editor.load(consequence_id)

    def _risk_changed(self):
        if not self._loading:
            self._save()

    def _save(self):
        if self._loading or self.consequence_id is None:
            return
        sev  = self.sev_combo.currentIndex() + 1
        desc = self.desc_edit.toPlainText().strip() or 'Ny konsekvens'
        cat  = self.cat_combo.currentText()
        self.db.update_consequence(self.consequence_id, desc, sev, cat)
        self.saved.emit(self.consequence_id)


class SafeguardPanel(QWidget):
    saved = pyqtSignal(int)

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
        self.desc_edit.setFixedHeight(100)
        self.desc_edit.focusOutEvent = lambda e: (self._save(), QTextEdit.focusOutEvent(self.desc_edit, e))
        form.addRow("Beskrivning:", self.desc_edit)

        self.rrf_combo = QComboBox()
        self.rrf_combo.addItems(_RRF_LABELS)
        self.rrf_combo.currentIndexChanged.connect(self._save)
        form.addRow("RRF:", self.rrf_combo)

        self.risk_badge = RiskBadge()
        form.addRow("Effektiv risk:", self.risk_badge)

        layout.addLayout(form)
        layout.addStretch()

    def load(self, safeguard_id):
        self.safeguard_id = safeguard_id
        sg = self.db.get_safeguard(safeguard_id)
        if not sg:
            return
        self._loading = True
        self.desc_edit.setPlainText(sg['description'])
        rrf = sg['rrf'] if sg['rrf'] in _RRF_VALUES else 1
        self.rrf_combo.setCurrentIndex(_RRF_VALUES.index(rrf))
        self._update_badge(sg)
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
        desc = self.desc_edit.toPlainText().strip() or 'Ny safeguard'
        rrf  = _RRF_VALUES[self.rrf_combo.currentIndex()]
        self.db.update_safeguard(self.safeguard_id, desc, rrf)
        self._update_badge()
        self.saved.emit(self.safeguard_id)


# ══════════════════════════════════════════════════════════════════════════════
# TREE PANEL
# ══════════════════════════════════════════════════════════════════════════════

NODE_T = 1
CAUSE_T = 2
CONS_T = 3
SG_T = 4


class TreePanel(QWidget):
    item_selected    = pyqtSignal(int, int)
    structure_changed = pyqtSignal()

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

        for node in self.db.nodes():
            nitem = QTreeWidgetItem([f"🏭  {node['name']}"])
            nitem.setData(0, Qt.ItemDataRole.UserRole, node['id'])
            nitem.setData(0, Qt.ItemDataRole.UserRole + 1, NODE_T)
            nitem.setFont(0, bold_font)
            nitem.setToolTip(0, node['pid_ref'] or '')
            self.tree.addTopLevelItem(nitem)
            if (NODE_T, node['id']) in expanded: nitem.setExpanded(True)
            if select_type == NODE_T and select_id == node['id']: target = nitem

            for cause in self.db.causes(node['id']):
                citem = QTreeWidgetItem([f"  ⚙  {cause['description'][:50]}"])
                citem.setData(0, Qt.ItemDataRole.UserRole, cause['id'])
                citem.setData(0, Qt.ItemDataRole.UserRole + 1, CAUSE_T)
                nitem.addChild(citem)
                if (CAUSE_T, cause['id']) in expanded: citem.setExpanded(True)
                if select_type == CAUSE_T and select_id == cause['id']: target = citem

                for cons in self.db.consequences(cause['id']):
                    level, _, _ = risk_info(cause['likelihood'], cons['severity'])
                    icon = _RISK_ICON.get(level, '⚪')
                    kitem = QTreeWidgetItem([f"    {icon}  {cons['description'][:40]}"])
                    kitem.setData(0, Qt.ItemDataRole.UserRole, cons['id'])
                    kitem.setData(0, Qt.ItemDataRole.UserRole + 1, CONS_T)
                    citem.addChild(kitem)
                    if (CONS_T, cons['id']) in expanded: kitem.setExpanded(True)
                    if select_type == CONS_T and select_id == cons['id']: target = kitem

                    for sg in self.db.safeguards(cons['id']):
                        rrf = sg['rrf'] or 1
                        rrf_str = f"RRF{rrf}" if rrf > 1 else "—"
                        linked = sg['source_id'] is not None if 'source_id' in sg.keys() else False
                        icon   = "🔗🛡" if linked else "🛡"
                        sgitem = QTreeWidgetItem([f"       {icon}  {sg['description'][:35]}  [{rrf_str}]"])
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

    def _resolve_cause_id(self, type_, id_):
        if type_ == CAUSE_T: return id_
        if type_ == CONS_T:
            r = self.db.get_consequence(id_); return r['cause_id'] if r else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id']); return c['cause_id'] if c else None
        return None

    def add_node(self):
        new_id = self.db.add_node()
        self.refresh(NODE_T, new_id)
        self.structure_changed.emit()

    def add_cause(self):
        type_, id_ = self._current()
        node_id = self._resolve_node_id(type_, id_) if type_ else None
        if node_id is None:
            QMessageBox.information(self, "Välj nod", "Välj en nod i trädet."); return
        new_id = self.db.add_cause(node_id)
        self.refresh(CAUSE_T, new_id)
        self.structure_changed.emit()

    def add_consequence(self):
        type_, id_ = self._current()
        cause_id = self._resolve_cause_id(type_, id_) if type_ else None
        if cause_id is None:
            QMessageBox.information(self, "Välj cause", "Välj en cause i trädet."); return
        new_id = self.db.add_consequence(cause_id)
        self.refresh(CONS_T, new_id)
        self.structure_changed.emit()

    def delete_selected(self):
        type_, id_ = self._current()
        if type_ is None: return
        names = {NODE_T: 'noden', CAUSE_T: 'causeen', CONS_T: 'konsekvensen', SG_T: 'safeguarden'}
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort {names[type_]} och allt under den?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        {NODE_T: self.db.delete_node, CAUSE_T: self.db.delete_cause,
         CONS_T: self.db.delete_consequence, SG_T: self.db.delete_safeguard}[type_](id_)
        self.refresh()
        self.structure_changed.emit()

    def _on_select(self, current, _previous):
        if current is None: return
        type_ = current.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = current.data(0, Qt.ItemDataRole.UserRole)
        self.item_selected.emit(type_, id_)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None: return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        menu  = QMenu(self)

        if type_ == NODE_T:
            menu.addAction("+ Lägg till Cause", self.add_cause)
        elif type_ == CAUSE_T:
            menu.addAction("+ Lägg till Consequence", self.add_consequence)
        menu.addSeparator()

        # Copy
        copy_labels = {CAUSE_T: "📋 Kopiera Cause",
                       CONS_T:  "📋 Kopiera Consequence",
                       SG_T:    "📋 Kopiera Safeguard"}
        if type_ in copy_labels:
            menu.addAction(copy_labels[type_],
                           lambda t=type_, i=id_: self._copy_item(t, i))

        # Paste (only if clipboard is compatible with current target)
        if self._clipboard:
            ct = self._clipboard['type']
            can_paste = (
                (ct == CAUSE_T and type_ in (NODE_T, CAUSE_T, CONS_T, SG_T)) or
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
            node_id = self._resolve_node_id(type_, id_)
            if not node_id:
                return
            new_id = self.db.copy_cause(cid, node_id)
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


class ScenarioTablePanel(QWidget):
    """Extended scenario table with FA, Antändning, Övriga faktorer and Slutkonsekvens."""

    # Column indices
    _C_NOD, _C_ORS, _C_KON, _C_RFORE = 0, 1, 2, 3
    _C_SG, _C_FA, _C_IGN, _C_OVRIGA  = 4, 5, 6, 7
    _C_REFT, _C_SLUT                   = 8, 9

    _COLS = [
        'Nod',
        'Orsak  →',
        'Konsekvens',
        'Risk före barriär',
        'Barriärer  →',
        'FA ☑',
        'Antändning ☑',
        'Övriga faktorer',
        'Risk efter barriärer',
        'Slutkonsekvens',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
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
        outer.addLayout(hdr_row)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        h = self._table.horizontalHeader()
        resize_modes = {
            self._C_NOD:   (QHeaderView.ResizeMode.Interactive, 70),
            self._C_ORS:   (QHeaderView.ResizeMode.Stretch,     0),
            self._C_KON:   (QHeaderView.ResizeMode.Stretch,     0),
            self._C_RFORE: (QHeaderView.ResizeMode.Fixed,       130),
            self._C_SG:    (QHeaderView.ResizeMode.Interactive, 160),
            self._C_FA:    (QHeaderView.ResizeMode.Fixed,       140),
            self._C_IGN:   (QHeaderView.ResizeMode.Fixed,       140),
            self._C_OVRIGA:(QHeaderView.ResizeMode.Interactive, 120),
            self._C_REFT:  (QHeaderView.ResizeMode.Fixed,       130),
            self._C_SLUT:  (QHeaderView.ResizeMode.Fixed,       130),
        }
        for col, (mode, width) in resize_modes.items():
            h.setSectionResizeMode(col, mode)
            if width:
                self._table.setColumnWidth(col, width)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(True)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        self._table.cellChanged.connect(self._on_cell_changed)
        outer.addWidget(self._table)

    # ── Load ──────────────────────────────────────────────────────────────────

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
        self._table.setRowCount(0)
        self._hdr_lbl.setText("HAZOP Scenario")

    # ── Build ─────────────────────────────────────────────────────────────────

    def _rebuild(self):
        try: self._table.cellChanged.disconnect()
        except Exception: pass
        self._table.setRowCount(0)

        if self.cause_id is None:
            self._table.cellChanged.connect(self._on_cell_changed)
            return

        cause = self.db.get_cause(self.cause_id)
        if not cause:
            self._table.cellChanged.connect(self._on_cell_changed)
            return
        cause_d   = dict(cause)
        node      = self.db.get_node(cause_d['node_id'])
        node_name = dict(node)['name'] if node else '?'
        freq      = cause_d['likelihood'] if cause_d['likelihood'] is not None else 3

        self._hdr_lbl.setText(f"HAZOP Scenario — {node_name}")
        freq_lbl = _FREQ_LABELS[freq_to_idx(freq)]

        for cons in self.db.consequences(self.cause_id):
            cons_d = dict(cons)
            self._add_row(node_name, cause_d, freq, freq_lbl, cons_d)

        self._table.cellChanged.connect(self._on_cell_changed)

    def _add_row(self, node_name, cause_d, freq, freq_lbl, cons_d):
        r    = self._table.rowCount()
        self._table.insertRow(r)
        sev  = cons_d['severity'] or 1
        cid  = cons_d['id']

        # Safeguards
        sgs       = [dict(s) for s in self.db.safeguards(cid)]
        sg_rrf    = 1
        for sg in sgs:
            sg_rrf *= sg.get('rrf', 1)

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
        self._table.setItem(r, self._C_NOD, nod)

        # ── Col 1: Orsak (editable) ───────────────────────────────────────────
        ors = QTableWidgetItem(f"{cause_d['description']}\n{freq_lbl}")
        ors.setData(Qt.ItemDataRole.UserRole, ('cause', cause_d['id']))
        self._table.setItem(r, self._C_ORS, ors)

        # ── Col 2: Konsekvens (editable) ──────────────────────────────────────
        sev_lbl = _SEV_LABELS[max(0, sev - 1)]
        kon = QTableWidgetItem(f"{cons_d['description']}\n{sev_lbl}")
        kon.setData(Qt.ItemDataRole.UserRole, ('consequence', cid))
        self._table.setItem(r, self._C_KON, kon)

        # ── Col 3: Risk före barriär ──────────────────────────────────────────
        rb = QTableWidgetItem(f"{level_b}\nF={freq}  C={sev}")
        rb.setBackground(QBrush(QColor(bg_b))); rb.setForeground(QBrush(QColor(fg_b)))
        rb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        rb.setFlags(rb.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(r, self._C_RFORE, rb)

        # ── Col 4: Safeguards (editable) ──────────────────────────────────────
        sg_lines = [f"{sg['description']}" + (f"  RRF {sg['rrf']}" if sg.get('rrf', 1) > 1 else "")
                    for sg in sgs]
        if sg_rrf > 1:
            sg_lines.append(f"─── RRF {sg_rrf:,}  (−{int(math.log10(sg_rrf))} steg)")
        sg_item = QTableWidgetItem('\n'.join(sg_lines) or '—')
        sg_item.setFlags(sg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(r, self._C_SG, sg_item)

        # ── Col 5: FA widget ──────────────────────────────────────────────────
        self._table.setCellWidget(r, self._C_FA,
                                  self._fa_widget(cid, fa_active, fa_rrf, 'fa'))

        # ── Col 6: Antändning widget ──────────────────────────────────────────
        self._table.setCellWidget(r, self._C_IGN,
                                  self._fa_widget(cid, ign_active, ign_rrf, 'ignition'))

        # ── Col 7: Övriga faktorer ────────────────────────────────────────────
        n_active = sum(1 for rf in rfs if rf.get('active'))
        extra_btn = QPushButton(
            f"📋 {n_active} aktiv(a)" if n_active else "📋 Lägg till…")
        extra_btn.setFlat(True)
        extra_btn.clicked.connect(lambda _, c=cid: self._edit_extra(c))
        self._table.setCellWidget(r, self._C_OVRIGA, extra_btn)

        # ── Col 8: Risk efter barriärer ───────────────────────────────────────
        f_eff = effective_frequency(freq, sg_rrf)
        ra = QTableWidgetItem(f"{level_a}\nF={f_eff}  C={sev}")
        ra.setBackground(QBrush(QColor(bg_a))); ra.setForeground(QBrush(QColor(fg_a)))
        ra.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        ra.setFlags(ra.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(r, self._C_REFT, ra)

        # ── Col 9: Slutkonsekvens ─────────────────────────────────────────────
        change = f"  (−{total_steps} tot.)" if total_steps > 0 else ""
        rs = QTableWidgetItem(f"{level_s}\nF={final_f}  C={sev}{change}")
        rs.setBackground(QBrush(QColor(bg_s))); rs.setForeground(QBrush(QColor(fg_s)))
        rs.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        rs.setFlags(rs.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(r, self._C_SLUT, rs)

        self._table.setRowHeight(r, max(52, min(140, (len(sg_lines) + 2) * 18)))

    def _fa_widget(self, cons_id, active, rrf_val, field):
        """Return a widget with checkbox + RRF spinbox for FA or Ignition."""
        label = 'FA' if field == 'fa' else 'Antändning'
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(3)

        chk = QCheckBox(label)
        chk.setChecked(bool(active))

        spin = QSpinBox()
        spin.setRange(1, 1_000_000)
        spin.setValue(int(rrf_val))
        spin.setPrefix("RRF ")
        spin.setEnabled(bool(active))
        spin.setFixedWidth(80)

        def _save_to_db():
            fa = int(chk.isChecked())
            rv = spin.value()
            spin.setEnabled(bool(fa))
            if field == 'fa':
                self.db.conn.execute(
                    "UPDATE consequences SET fa_active=?,fa_rrf=? WHERE id=?",
                    (fa, rv, cons_id))
            else:
                self.db.conn.execute(
                    "UPDATE consequences SET ignition_active=?,ignition_rrf=? WHERE id=?",
                    (fa, rv, cons_id))
            self.db.conn.commit()
            # Defer rebuild so current widget event finishes first
            QTimer.singleShot(0, self._rebuild)

        # stateChanged fires after the checkbox has settled
        chk.stateChanged.connect(lambda _state: _save_to_db())
        spin.editingFinished.connect(_save_to_db)

        layout.addWidget(chk)
        layout.addWidget(spin)
        return w

    def _edit_extra(self, cons_id):
        dlg = ReductionFactorsDialog(self.db, cons_id, self)
        dlg.exec()
        self._rebuild()

    def _on_cell_changed(self, row, col):
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        kind, id_ = meta
        text = item.text()
        # Only the first line is the description (second line = label)
        desc = text.split('\n')[0].strip()
        if kind == 'cause':
            self.db.update_cause(id_, desc)
        elif kind == 'consequence':
            cons = self.db.get_consequence(id_)
            if cons:
                self.db.update_consequence(id_, desc, cons['severity'], cons['category'] or '')


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
            rb = QTableWidgetItem(f"{level_b}\nF={freq}  C={sev}")
            rb.setBackground(QBrush(QColor(bg_b)))
            rb.setForeground(QBrush(QColor(fg_b)))
            rb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            rb.setFlags(rb.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, self._C_RFORE, rb)

            self.table.setItem(r, self._C_SG, _ro(sg_text))

            # Risk after
            ra = QTableWidgetItem(f"{level_a}\nF={eff_f}  C={sev}")
            ra.setBackground(QBrush(QColor(bg_a)))
            ra.setForeground(QBrush(QColor(fg_a)))
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
        c_id  = self.db.add_cause(self.node_id)
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

class MatrixCellButton(QPushButton):
    def __init__(self, row, col, color, label, parent=None):
        super().__init__(label, parent)
        self.row = row
        self.col = col
        self._color = color
        self._label = label
        self.setFixedSize(80, 40)
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet(
            f"background:{self._color}; color:white; font-weight:bold; border:1px solid #888;")
        self.setText(self._label)

    def set_cell(self, color, label):
        self._color = color
        self._label = label
        self._apply_style()

    def color(self): return self._color
    def label(self): return self._label


class SettingsPanel(QWidget):
    matrix_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cell_buttons = []

        tabs = QTabWidget()
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: Riskmatris ───────────────────────────────────────────────────
        matrix_tab = QWidget()
        ml = QVBoxLayout(matrix_tab)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Rader (Konsekvens):"))
        self._rows_spin = QSpinBox(); self._rows_spin.setRange(2, 10); self._rows_spin.setValue(5)
        size_row.addWidget(self._rows_spin)
        size_row.addWidget(QLabel("  Kolumner (Sannolikhet):"))
        self._cols_spin = QSpinBox(); self._cols_spin.setRange(2, 10); self._cols_spin.setValue(5)
        size_row.addWidget(self._cols_spin)
        apply_size_btn = QPushButton("Tillämpa storlek")
        apply_size_btn.clicked.connect(self._apply_size)
        size_row.addWidget(apply_size_btn)
        size_row.addStretch()
        ml.addLayout(size_row)

        self._matrix_container = QWidget()
        self._matrix_grid = QGridLayout(self._matrix_container)
        self._matrix_grid.setSpacing(2)
        ml.addWidget(self._matrix_container)

        save_matrix_btn = QPushButton("💾 Spara riskmatris")
        save_matrix_btn.clicked.connect(self._save_matrix)
        ml.addWidget(save_matrix_btn)
        ml.addStretch()
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

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_categories()
        self._proj_name.setText(self.db.get_config('project_name', ''))
        self._proj_date.setText(self.db.get_config('project_date', ''))
        self._proj_rev.setText(self.db.get_config('project_revision', ''))

    def _load_matrix_ui(self):
        cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        rows = cfg.get('rows', 5)
        cols = cfg.get('cols', 5)
        self._rows_spin.setValue(rows)
        self._cols_spin.setValue(cols)
        self._build_matrix_grid(cfg)

    def _apply_size(self):
        rows = self._rows_spin.value()
        cols = self._cols_spin.value()
        old_cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        new_cfg = {
            'rows': rows, 'cols': cols,
            'x_axis': old_cfg.get('x_axis', 'likelihood'),
            'x_labels': (old_cfg.get('x_labels', []) + _LIKE_LABELS)[:cols],
            'y_labels': (old_cfg.get('y_labels', []) + _SEV_LABELS)[:rows],
            'cell_colors': [],
            'cell_labels': [],
        }
        old_colors = old_cfg.get('cell_colors', [])
        old_labels = old_cfg.get('cell_labels', [])
        for r in range(rows):
            crow = []; lrow = []
            for c in range(cols):
                try: crow.append(old_colors[r][c])
                except (IndexError, KeyError): crow.append('#27ae60')
                try: lrow.append(old_labels[r][c])
                except (IndexError, KeyError): lrow.append('Låg')
            new_cfg['cell_colors'].append(crow)
            new_cfg['cell_labels'].append(lrow)
        self._build_matrix_grid(new_cfg)

    def _build_matrix_grid(self, cfg):
        while self._matrix_grid.count():
            item = self._matrix_grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._cell_buttons = []

        rows = cfg['rows']
        cols = cfg['cols']
        x_labels = cfg.get('x_labels', [str(i+1) for i in range(cols)])
        y_labels = cfg.get('y_labels', [str(i+1) for i in range(rows)])
        colors   = cfg.get('cell_colors', [['#27ae60']*cols]*rows)
        labels   = cfg.get('cell_labels', [['Låg']*cols]*rows)

        # Column headers (likelihood)
        self._matrix_grid.addWidget(QLabel("↑ S \\ L →"), 0, 0)
        for c in range(cols):
            lbl = QLabel(x_labels[c] if c < len(x_labels) else str(c+1))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-weight:bold; font-size:9px;")
            self._matrix_grid.addWidget(lbl, 0, c + 1)

        for r in range(rows):
            display_r = rows - 1 - r  # high severity at top
            row_lbl = QLabel(y_labels[display_r] if display_r < len(y_labels) else str(display_r+1))
            row_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_lbl.setStyleSheet("font-size:9px;")
            self._matrix_grid.addWidget(row_lbl, r + 1, 0)
            row_btns = []
            for c in range(cols):
                try: cell_color = colors[display_r][c]
                except (IndexError, KeyError): cell_color = '#27ae60'
                try: cell_label = labels[display_r][c]
                except (IndexError, KeyError): cell_label = 'Låg'
                btn = MatrixCellButton(display_r, c, cell_color, cell_label)
                btn.clicked.connect(lambda _, b=btn: self._edit_cell(b))
                self._matrix_grid.addWidget(btn, r + 1, c + 1)
                row_btns.append(btn)
            self._cell_buttons.append((display_r, row_btns))

    def _edit_cell(self, btn):
        color = QColorDialog.getColor(QColor(btn.color()), self, "Välj färg för cell")
        if not color.isValid():
            return
        label, ok = __import__('PyQt6.QtWidgets', fromlist=['QInputDialog']).QInputDialog.getText(
            self, "Celltext", "Risknivå (t.ex. Låg, Medium, Hög, Kritisk):", text=btn.label())
        if ok:
            btn.set_cell(color.name(), label.strip() or btn.label())

    def _save_matrix(self):
        rows = self._rows_spin.value()
        cols = self._cols_spin.value()
        old_cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX

        colors = [['' for _ in range(cols)] for _ in range(rows)]
        labels = [['' for _ in range(cols)] for _ in range(rows)]
        for display_r, row_btns in self._cell_buttons:
            for btn in row_btns:
                r, c = btn.row, btn.col
                if r < rows and c < cols:
                    colors[r][c] = btn.color()
                    labels[r][c] = btn.label()

        cfg = {
            'rows': rows, 'cols': cols,
            'x_axis': old_cfg.get('x_axis', 'likelihood'),
            'x_labels': old_cfg.get('x_labels', _LIKE_LABELS[:cols]),
            'y_labels': old_cfg.get('y_labels', _SEV_LABELS[:rows]),
            'cell_colors': colors,
            'cell_labels': labels,
        }
        self.db.set_risk_matrix(cfg)
        load_matrix(self.db)
        QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
        self.matrix_changed.emit()

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

class AdminPanel(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Administration")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        # Stats
        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet(
            "background:#f0f4f8; border:1px solid #ccc; border-radius:6px; padding:10px;")
        layout.addWidget(self._stats_lbl)

        bar = QHBoxLayout()
        refresh_btn = QPushButton("🔄 Uppdatera")
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn); bar.addStretch()
        layout.addLayout(bar)

        # Data table
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
        layout.addWidget(self._table)

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
        self._scan_btn.clicked.connect(self._scan)

        add_btn = QPushButton("+ Lägg till")
        add_btn.setToolTip("Lägg till en tagg manuellt")
        add_btn.clicked.connect(self._add_manual)

        refresh_btn = QPushButton("🔄 Uppdatera")
        refresh_btn.clicked.connect(self.refresh)

        self._create_btn = QPushButton("🏭 Skapa HAZOP-noder")
        self._create_btn.setToolTip("Skapar en nod per ikryssad rad")
        self._create_btn.clicked.connect(self._create_nodes)

        clear_btn = QPushButton("🗑 Rensa register")
        clear_btn.setStyleSheet("color:#c0392b;")
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
        reply = QMessageBox.question(
            self, "Rensa register",
            "Ta bort alla poster i utrustningsregistret?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
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
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database()
        load_matrix(self.db)
        self.setWindowTitle(f"HAZOP Tool  —  {self.db.path.name}")
        self.resize(1440, 900)

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = self.addToolBar("Verktyg")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        def act(label, tip, slot):
            a = QAction(label, self); a.setToolTip(tip); a.triggered.connect(slot)
            tb.addAction(a); return a

        act("+ Nod",         "Lägg till ny nod",       lambda: self.tree_panel.add_node())
        act("+ Cause",       "Lägg till cause",        lambda: self.tree_panel.add_cause())
        act("+ Consequence", "Lägg till consequence",  lambda: self.tree_panel.add_consequence())
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
        self.btn_admin     = QPushButton("⚙️  Administration")
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

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        pid_layout.addWidget(v_splitter)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tree_panel = TreePanel(self.db)
        self.tree_panel.setMinimumWidth(220)
        self.tree_panel.setMaximumWidth(340)
        h_splitter.addWidget(self.tree_panel)

        self.pid_panel = PIDPanel(self.db)
        self.pid_panel.setMinimumWidth(400)
        h_splitter.addWidget(self.pid_panel)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.stack = QStackedWidget()
        self.welcome_panel = WelcomePanel()
        self.node_panel    = NodePanel(self.db)
        self.cause_panel   = CausePanel(self.db)
        self.cons_panel    = ConsequencePanel(self.db)
        self.sg_panel      = SafeguardPanel(self.db)
        for panel in [self.welcome_panel, self.node_panel,
                      self.cause_panel, self.cons_panel, self.sg_panel]:
            self.stack.addWidget(panel)
        scroll.setWidget(self.stack)
        h_splitter.addWidget(scroll)
        h_splitter.setSizes([260, 650, 370])
        v_splitter.addWidget(h_splitter)

        self.scenario_panel = ScenarioTablePanel(self.db)
        v_splitter.addWidget(self.scenario_panel)
        v_splitter.setSizes([640, 220])
        self.view_stack.addWidget(pid_page)

        # ── Page 1: Worksheet ─────────────────────────────────────────────────
        self.worksheet = HAZOPWorksheet(self.db)
        self.view_stack.addWidget(self.worksheet)

        # ── Page 2: Equipment ─────────────────────────────────────────────────
        self.equipment_panel = EquipmentPanel(self.db)
        self.view_stack.addWidget(self.equipment_panel)

        # ── Page 3: Admin ─────────────────────────────────────────────────────
        self.admin_panel = AdminPanel(self.db)
        self.view_stack.addWidget(self.admin_panel)

        # ── Page 4: Settings ──────────────────────────────────────────────────
        self.settings_panel = SettingsPanel(self.db)
        self.settings_panel.matrix_changed.connect(self._on_matrix_changed)
        self.view_stack.addWidget(self.settings_panel)

        # ── Wire signals ──────────────────────────────────────────────────────
        self.tree_panel.item_selected.connect(self._on_selected)
        self.tree_panel.structure_changed.connect(self._on_structure_changed)

        self.node_panel.saved.connect(
            lambda id_, name: self.tree_panel.refresh(NODE_T, id_))
        self.cause_panel.saved.connect(
            lambda id_, _: (self.tree_panel.refresh(CAUSE_T, id_),
                            self.scenario_panel.load_cause(id_)))
        self.cons_panel.saved.connect(
            lambda id_: (self.tree_panel.refresh(CONS_T, id_),
                         self.scenario_panel.load_consequence(id_)))
        self.sg_panel.saved.connect(
            lambda id_: self.tree_panel.refresh(SG_T, id_))

        # ScenarioTablePanel has no data_changed signal — edits go through panels above

        self.pid_panel.node_created.connect(
            lambda nid: (self.tree_panel.refresh(NODE_T, nid),
                         self._on_selected(NODE_T, nid)))
        self.pid_panel.cause_created.connect(
            lambda cid: (self.tree_panel.refresh(CAUSE_T, cid),
                         self._on_selected(CAUSE_T, cid)))
        self.pid_panel.consequence_created.connect(
            lambda cid: (self.tree_panel.refresh(CONS_T, cid),
                         self._on_selected(CONS_T, cid)))
        self.pid_panel.safeguard_created.connect(self._on_safeguard_created)
        self.pid_panel.risk_scenario_requested.connect(self._on_pid_risk_scenario)

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
        if page == 3: self.admin_panel.refresh()

    def _on_selected(self, type_, id_):
        self._cur_type = type_
        self._cur_id   = id_
        if type_ == NODE_T:
            self.node_panel.load(id_)
            self.stack.setCurrentWidget(self.node_panel)
            self.pid_panel.set_active_node(id_)
            self.scenario_panel.clear()
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

    def _on_structure_changed(self):
        self._cur_type = None
        self._cur_id   = None
        self.stack.setCurrentWidget(self.welcome_panel)
        self.scenario_panel.clear()

    def _on_safeguard_created(self, _sg_id):
        if self._cur_type == CONS_T and self._cur_id is not None:
            self.cons_panel.load(self._cur_id)
            self.scenario_panel.load_consequence(self._cur_id)
            self.tree_panel.refresh(CONS_T, self._cur_id)

    def _on_matrix_changed(self):
        if self._cur_type == CONS_T and self._cur_id is not None:
            self.cons_panel.load(self._cur_id)
        self.tree_panel.refresh()
        if self._cur_type == CAUSE_T and self._cur_id:
            self.scenario_panel.load_cause(self._cur_id)

    def _open_risk_scenario_wizard(self, node_id=None):
        if node_id is None or node_id == 0:
            if self._cur_type in (NODE_T, CAUSE_T, CONS_T, SG_T) and self._cur_id:
                node_id = self.tree_panel._resolve_node_id(self._cur_type, self._cur_id)
        if not node_id:
            nodes = self.db.nodes()
            if not nodes:
                QMessageBox.information(self, "Ingen nod",
                    "Lägg till en nod först.")
                return
            node_id = nodes[0]['id']
        dlg = RiskScenarioWizard(self.db, node_id, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.tree_panel.refresh(CAUSE_T, dlg.created_cause_id)
            self._on_selected(CAUSE_T, dlg.created_cause_id)
            self.status_bar.showMessage("Risk scenario skapat.", 4000)

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
