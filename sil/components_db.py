"""
Komponentdatabas för SIL-kalkylator.
SQLite med fördefinierade felfrekvenser (OREDA / exida / generiska värden).
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "components.db"

# (category, manufacturer, model, lambda_d, dc, beta, description, source)
_SEED: list[tuple] = [
    # ── Trycktransmittrar ────────────────────────────────────────────────────
    ("Trycktransmitter", "Generisk",         "Utan diagnostik",       1.0e-6, 0.00, 0.02, "Konventionell 4-20 mA transmitter",            "Generic"),
    ("Trycktransmitter", "Generisk",         "Smart / HART",          8.0e-7, 0.60, 0.02, "Smart transmitter med HART-diagnostik",        "Exida"),
    ("Trycktransmitter", "Rosemount",        "3051 (4-20mA)",         7.5e-7, 0.65, 0.02, "Rosemount 3051 utan diagnostik",               "Exida"),
    ("Trycktransmitter", "Rosemount",        "3051S (HART+diag.)",    6.0e-7, 0.85, 0.02, "Rosemount 3051S med full diagnostik",          "Exida"),
    ("Trycktransmitter", "Endress+Hauser",   "Cerabar M",             8.5e-7, 0.60, 0.02, "E+H Cerabar M trycktransmitter",               "Exida"),
    # ── Temperaturtransmittrar ───────────────────────────────────────────────
    ("Temperaturtransmitter", "Generisk",    "Utan diagnostik",       1.5e-6, 0.00, 0.02, "Konventionell temp.transmitter (TC/RTD)",      "Generic"),
    ("Temperaturtransmitter", "Generisk",    "Smart / HART",          1.2e-6, 0.55, 0.02, "Smart temp.tx med HART-diagnostik",            "Exida"),
    ("Temperaturtransmitter", "Rosemount",   "248 (HART)",            1.0e-6, 0.60, 0.02, "Rosemount 248 temperaturtransmitter",          "Exida"),
    # ── Flödestransmittrar ───────────────────────────────────────────────────
    ("Flödestransmitter", "Generisk",        "Coriolis",              1.5e-6, 0.00, 0.02, "Coriolis-flödesmätare",                        "Generic"),
    ("Flödestransmitter", "Generisk",        "Vortex",                2.5e-6, 0.00, 0.02, "Vortex-flödesmätare",                          "Generic"),
    ("Flödestransmitter", "Generisk",        "Magnetisk",             2.0e-6, 0.00, 0.02, "Elektromagnetisk flödesmätare",                "Generic"),
    ("Flödestransmitter", "Endress+Hauser",  "Promass 100 (Coriolis)",1.2e-6, 0.00, 0.02, "E+H Promass Coriolis-flödesmätare",            "Exida"),
    # ── Nivåtransmittrar ─────────────────────────────────────────────────────
    ("Nivåtransmitter",  "Generisk",         "Differenstryck (DP)",   1.5e-6, 0.00, 0.02, "DP-baserad nivåmätning",                       "Generic"),
    ("Nivåtransmitter",  "Generisk",         "Radarvåg (GWR)",        1.0e-6, 0.00, 0.02, "Guidad mikrovåg (guided wave radar)",          "Generic"),
    ("Nivåtransmitter",  "Generisk",         "Ultraljud",             2.0e-6, 0.00, 0.02, "Ultraljudsnivåmätare",                         "Generic"),
    ("Nivåtransmitter",  "Rosemount",        "5300 GWR",              9.0e-7, 0.00, 0.02, "Rosemount 5300 guided wave radar",             "Exida"),
    # ── Tryckvakter ──────────────────────────────────────────────────────────
    ("Tryckvakt",        "Generisk",         "Mekanisk",              2.5e-6, 0.00, 0.02, "Mekanisk tryckvakt (snap-action)",             "Generic"),
    ("Tryckvakt",        "Generisk",         "Elektronisk",           1.0e-6, 0.50, 0.02, "Elektronisk tryckvakt med diagnostik",         "Exida"),
    # ── Logic Solvers ────────────────────────────────────────────────────────
    ("Logic solver",     "Generisk",         "SIL 1-PLC",             2.0e-7, 0.90, 0.02, "Generisk SIL 1-certifierad säkerhets-PLC",     "Generic"),
    ("Logic solver",     "Generisk",         "SIL 2-PLC",             5.0e-8, 0.99, 0.02, "Generisk SIL 2-certifierad säkerhets-PLC",     "Generic"),
    ("Logic solver",     "Generisk",         "SIL 3-PLC (TMR)",       1.0e-8, 0.99, 0.01, "Trippelredundant SIL 3-PLC (2oo3-arkitektur)", "Generic"),
    ("Logic solver",     "Triconex",         "Tricon T3000",          1.0e-8, 0.99, 0.01, "Triconex Tricon TMR-system",                   "Exida"),
    ("Logic solver",     "ABB",              "AC700F",                2.5e-8, 0.99, 0.02, "ABB AC700F Safety PLC",                        "Exida"),
    ("Logic solver",     "Siemens",          "S7-300F / ET200",       4.0e-8, 0.99, 0.02, "Siemens S7-300F failsafe PLC",                 "Exida"),
    ("Logic solver",     "Pilz",             "PSS 4000",              3.0e-8, 0.99, 0.02, "Pilz PSS 4000 säkerhets-PLC",                  "Exida"),
    ("Logic solver",     "Hima",             "HIMatrix F30",          2.0e-8, 0.99, 0.02, "Hima HIMatrix F30 säkerhets-PLC",              "Exida"),
    # ── Säkerhetsreläer ──────────────────────────────────────────────────────
    ("Säkerhetsrelä",    "Generisk",         "Säkerhetsrelä",         3.0e-8, 0.90, 0.02, "Generiskt säkerhetsrelä (EN 954/ISO 13849)",   "Generic"),
    ("Säkerhetsrelä",    "Pilz",             "PNOZ X / m B0",         2.0e-8, 0.95, 0.02, "Pilz PNOZ säkerhetsrelä",                      "Exida"),
    # ── Magnetventiler ───────────────────────────────────────────────────────
    ("Magnetventil",     "Generisk",         "Utan diagnostik",       1.5e-6, 0.00, 0.02, "Generisk solenoidventil, ingen diagnostik",    "Generic"),
    ("Magnetventil",     "Generisk",         "Med kretsövervakning",  1.0e-6, 0.50, 0.02, "Solenoid med kontinuerlig kretsövervakning",   "Exida"),
    ("Magnetventil",     "ASCO",             "Series 8000",           1.2e-6, 0.00, 0.02, "ASCO Series 8000 solenoidventil",              "Exida"),
    # ── Avstängningsventiler ─────────────────────────────────────────────────
    ("Avstängningsventil","Generisk",        "Fjäderåterg. NC",       8.0e-6, 0.00, 0.02, "NC-ventil, fjäderåtergående, ingen diagnostik","OREDA"),
    ("Avstängningsventil","Generisk",        "Fjäderåterg. NO",       8.0e-6, 0.00, 0.02, "NO-ventil, fjäderåtergående, ingen diagnostik","OREDA"),
    ("Avstängningsventil","Generisk",        "Med partial stroke (PST)",6.0e-6,0.50,0.02, "NC-ventil med partial stroke test-diagnostik", "Exida"),
    ("Avstängningsventil","Generisk",        "Med full diagnostik",   5.0e-6, 0.80, 0.02, "NC-ventil med positionsfeedback + PST",        "Exida"),
    ("Avstängningsventil","Valtek",          "Mark One",              7.5e-6, 0.00, 0.02, "Valtek Mark One rörkopplad ventil",            "Exida"),
    ("Avstängningsventil","Fisher",          "ET",                    7.0e-6, 0.00, 0.02, "Fisher ET rörventil",                          "Exida"),
    # ── Reglerventiler (SIS-funktion) ────────────────────────────────────────
    ("Reglerventil",     "Generisk",         "SIS-funktion",          5.0e-6, 0.00, 0.02, "Reglerventil i SIS-säkerhetsfunktion",         "Generic"),
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Skapar tabell och fyller på med standarddata första gången."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS components (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category     TEXT    NOT NULL,
            manufacturer TEXT    NOT NULL,
            model        TEXT    NOT NULL,
            lambda_d     REAL    NOT NULL,
            dc           REAL    NOT NULL DEFAULT 0.0,
            beta         REAL    NOT NULL DEFAULT 0.02,
            description  TEXT,
            source       TEXT,
            custom       INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    if conn.execute("SELECT COUNT(*) FROM components").fetchone()[0] == 0:
        conn.executemany("""
            INSERT INTO components
                (category, manufacturer, model, lambda_d, dc, beta, description, source)
            VALUES (?,?,?,?,?,?,?,?)
        """, _SEED)
        conn.commit()
    conn.close()


def get_categories() -> list[str]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT category FROM components ORDER BY category"
    ).fetchall()
    conn.close()
    return ["Alla"] + [r["category"] for r in rows]


def search(category: str = "Alla", query: str = "") -> list[sqlite3.Row]:
    conn = get_connection()
    q = f"%{query}%"
    if category == "Alla":
        rows = conn.execute("""
            SELECT * FROM components
            WHERE manufacturer LIKE ? OR model LIKE ? OR description LIKE ?
            ORDER BY category, manufacturer, model
        """, (q, q, q)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM components
            WHERE category = ?
              AND (manufacturer LIKE ? OR model LIKE ? OR description LIKE ?)
            ORDER BY manufacturer, model
        """, (category, q, q, q)).fetchall()
    conn.close()
    return rows


def add_custom(
    category: str, manufacturer: str, model: str,
    lambda_d: float, dc: float, beta: float, description: str = "",
) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO components
            (category, manufacturer, model, lambda_d, dc, beta, description, source, custom)
        VALUES (?,?,?,?,?,?,?,'Eget',1)
    """, (category, manufacturer, model, lambda_d, dc, beta, description))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id
