"""
build_sil_db.py
Reads all PDF files under SIF/, extracts SIL-related data, and stores results
in a SQLite database sil_components.db.

Requires: pymupdf (import fitz)
"""

import sys
import os
import re
import sqlite3
from pathlib import Path

# Force UTF-8 stdout so Swedish/special chars work on Windows
sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)

try:
    import fitz  # pymupdf
except ImportError:
    print("ERROR: pymupdf not installed. Run: pip install pymupdf")
    sys.exit(1)

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIF")
DB_PATH  = Path(r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIL projekt\sil_components.db")

# ── SILver report filenames ────────────────────────────────────────────────────
SILVER_REPORT_PATTERNS = [
    "SILver Detailed Report",
    "SILver",
]

# ── regex helpers ──────────────────────────────────────────────────────────────

# Matches numbers like 2.42E-8, 5.57e-9, 1.23E+2, 0.000042, 4.2×10-8
NUM_RE = re.compile(
    r"(\d+[\.,]?\d*)\s*[xX×]\s*10\s*[\^]?\s*[-−]?\s*(\d+)"  # 1.2×10^-8
    r"|(\d+[\.,]?\d*)\s*[eE][-−]?\s*(\d+)"                   # 1.2E-8 / 1.2e8
    r"|(\d[\d\.,]+)",                                          # plain number
    re.IGNORECASE,
)

# λDU / lDU / LDU / lambda_DU / "dangerous undetected" failure rate
LAMBDA_DU_RE = re.compile(
    r"(?:λ|lambda|l)\s*[_\-]?\s*DU\b"
    r"|dangerous\s+undetected"
    r"|λdu",
    re.IGNORECASE,
)

LAMBDA_DD_RE = re.compile(
    r"(?:λ|lambda|l)\s*[_\-]?\s*DD\b"
    r"|dangerous\s+detected"
    r"|λdd",
    re.IGNORECASE,
)

LAMBDA_D_RE = re.compile(
    r"(?:λ|lambda|l)\s*[_\-]?\s*D\b(?!\w)"
    r"|total\s+dangerous",
    re.IGNORECASE,
)

LAMBDA_S_RE = re.compile(
    r"(?:λ|lambda|l)\s*[_\-]?\s*S\b(?!\w)"
    r"|safe\s+failure",
    re.IGNORECASE,
)

DC_RE  = re.compile(r"\bDC\b.*?(\d{1,3}(?:[.,]\d+)?)\s*%", re.IGNORECASE | re.DOTALL)
SFF_RE = re.compile(r"\bSFF\b.*?(\d{1,3}(?:[.,]\d+)?)\s*%", re.IGNORECASE | re.DOTALL)
SIL_RE = re.compile(r"\bSIL\s*([123])\b", re.IGNORECASE)
PTI_RE = re.compile(
    r"(?:proof\s+test|PTI|test\s+interval)\s*[:\-–]?\s*(\d[\d\s,\.]*(?:year|month|hour|yr|mo|h)\b)",
    re.IGNORECASE,
)

FIT_MENTION_RE = re.compile(r"\bFIT\b")


def parse_number(text):
    """Return the first float found in text, or None."""
    m = NUM_RE.search(text)
    if not m:
        return None
    g = m.groups()
    try:
        if g[0] and g[1]:          # a×10^b form
            return float(g[0].replace(",", ".")) * 10 ** (-int(g[1]))
        if g[2] and g[3]:          # aEb form
            return float(g[2].replace(",", ".") + "e-" + g[3])
        if g[4]:
            return float(g[4].replace(",", "."))
    except (ValueError, IndexError):
        pass
    return None


def extract_rate_near_keyword(text, keyword_re, window=200):
    """
    Find keyword_re in text, then extract the first number in the following
    `window` characters.  Returns value in FIT.
    """
    for m in keyword_re.finditer(text):
        snippet = text[m.start(): m.start() + window]
        val = parse_number(snippet[len(m.group()):])
        if val is None:
            continue
        # Heuristic: if the number looks like a /h rate (< 1e-3), convert to FIT
        if val < 1e-3:
            val_fit = val * 1e9
        else:
            val_fit = val          # assume already in FIT
        return round(val_fit, 6)
    return None


def extract_pct_near_keyword(text, kw_re, window=150):
    m = kw_re.search(text)
    if not m:
        return None
    snippet = text[m.start(): m.start() + window]
    pm = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%?", snippet[len(m.group(0)):])
    if pm:
        try:
            return float(pm.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def extract_sil(text):
    m = SIL_RE.search(text)
    return int(m.group(1)) if m else None


def extract_proof_test(text):
    m = PTI_RE.search(text)
    return m.group(0)[:120] if m else None


def extract_text_from_pdf(pdf_path, max_pages=None):
    """Return full text from PDF, or '' on failure."""
    try:
        doc = fitz.open(str(pdf_path))
        pages = doc.pages() if max_pages is None else list(doc.pages())[:max_pages]
        chunks = []
        for page in pages:
            try:
                chunks.append(page.get_text("text"))
            except Exception:
                pass
        doc.close()
        return "\n".join(chunks)
    except Exception as e:
        print(f"  WARN: could not read {pdf_path.name}: {e}")
        return ""


# ── tag parsing ───────────────────────────────────────────────────────────────

# e.g. 347SV671, 345LI354, 253PZ323, 251SV520, 346PI304
TAG_RE = re.compile(r"^(\d{3})([A-Z]{1,3})(\d+)", re.IGNORECASE)

INSTRUMENT_TYPE_MAP = {
    "SV": "valve",      "RV": "valve",
    "PZ": "pressure",   "PI": "pressure",  "PD": "pressure",
    "LI": "level",      "LZ": "level",     "LS": "level",
    "TI": "temp",       "TZ": "temp",      "TS": "temp",
    "FI": "flow",       "FZ": "flow",      "FS": "flow",
    "AZ": "analyzer",
    "EC": "compressor",
    "PU": "pump",
    "YS": "solenoid",
    "TSX": "temp",      "TX": "temp",
}

SUBSYSTEM_MAP = {
    "valve": "FE",       "compressor": "FE",
    "pump": "FE",        "solenoid": "FE",
    "pressure": "sensor","level": "sensor",
    "temp": "sensor",    "flow": "sensor",
    "analyzer": "sensor",
}


def parse_tag(filename_stem):
    """Parse tag number from filename stem like '347SV671-0001-EA MODEL'."""
    # remove suffix after first dash beyond tag
    candidate = filename_stem.split("-")[0].strip()
    m = TAG_RE.match(candidate)
    if not m:
        return None
    process_area = m.group(1)
    letters      = m.group(2).upper()
    instrument_no = m.group(3)
    tag_number    = process_area + letters + instrument_no

    # find tag type
    tag_type = None
    for key in sorted(INSTRUMENT_TYPE_MAP.keys(), key=len, reverse=True):
        if letters.startswith(key):
            tag_type = INSTRUMENT_TYPE_MAP[key]
            break
    if tag_type is None:
        tag_type = "unknown"

    subsystem = SUBSYSTEM_MAP.get(tag_type, "unknown")
    return {
        "tag_number":    tag_number,
        "tag_type":      tag_type,
        "process_area":  process_area,
        "subsystem_type": subsystem,
    }


# ── SIF-range extraction ──────────────────────────────────────────────────────

def extract_sif_range(filename):
    """Return SIF range string from SILver report filename."""
    m = re.search(r"SIF[-\s]*([\d\s,\-–/]+)", filename, re.IGNORECASE)
    if m:
        return m.group(0).strip()[:80]
    return filename[:80]


def project_area_from_path(path: Path):
    """Derive project area from top-level SIF subfolder."""
    parts = path.relative_to(BASE_DIR).parts
    return parts[0] if parts else "unknown"


# ── database setup ────────────────────────────────────────────────────────────

def create_db(db_path):
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS certificates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            manufacturer    TEXT,
            model           TEXT,
            file_path       TEXT UNIQUE,
            lambda_du_fit   REAL,
            lambda_dd_fit   REAL,
            lambda_d_fit    REAL,
            lambda_s_fit    REAL,
            dc_pct          REAL,
            sff_pct         REAL,
            sil_level       INTEGER,
            proof_test_note TEXT,
            summary_text    TEXT
        );

        CREATE TABLE IF NOT EXISTS silver_reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sif_range    TEXT,
            file_path    TEXT UNIQUE,
            project_area TEXT,
            summary_text TEXT
        );

        CREATE TABLE IF NOT EXISTS tag_drawings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_number    TEXT,
            tag_type      TEXT,
            process_area  TEXT,
            subsystem_type TEXT,
            file_path     TEXT UNIQUE
        );
    """)
    con.commit()
    return con


# ── main logic ────────────────────────────────────────────────────────────────

def is_silver_report(pdf_path: Path):
    name = pdf_path.name
    return any(pat.lower() in name.lower() for pat in SILVER_REPORT_PATTERNS)


def is_kretsschema(pdf_path: Path):
    parts = [p.lower() for p in pdf_path.parts]
    return any("kretsschema" in p for p in parts)


def manufacturer_from_path(pdf_path: Path):
    """
    For certificate PDFs the folder structure is:
    <area>/Certifikat/<Manufacturer>/<file>.pdf
    Return the manufacturer folder name.
    """
    try:
        rel = pdf_path.relative_to(BASE_DIR)
        parts = rel.parts  # e.g. ('Caloric', 'Certifikat', 'ABB', 'some.pdf')
        # find 'certifikat' part (case-insensitive)
        for i, p in enumerate(parts):
            if p.lower() == "certifikat" and i + 1 < len(parts) - 1:
                return parts[i + 1]
    except Exception:
        pass
    return "Unknown"


def process_all(con):
    cur = con.cursor()

    stats = {
        "certs_processed": 0,
        "certs_skipped": 0,
        "silver_processed": 0,
        "silver_skipped": 0,
        "tags_processed": 0,
        "tags_skipped": 0,
    }

    for pdf_path in sorted(BASE_DIR.rglob("*.pdf")):
        rel_path = str(pdf_path.relative_to(BASE_DIR))

        # ── SILver reports ────────────────────────────────────────────
        if is_silver_report(pdf_path):
            print(f"[SILVER] {rel_path}")
            text = extract_text_from_pdf(pdf_path, max_pages=3)
            if not text:
                stats["silver_skipped"] += 1
                continue
            sif_range    = extract_sif_range(pdf_path.name)
            project_area = project_area_from_path(pdf_path)
            summary      = text[:500].replace("\x00", "")
            try:
                cur.execute(
                    "INSERT OR REPLACE INTO silver_reports "
                    "(sif_range, file_path, project_area, summary_text) "
                    "VALUES (?,?,?,?)",
                    (sif_range, rel_path, project_area, summary),
                )
                con.commit()
                stats["silver_processed"] += 1
            except sqlite3.Error as e:
                print(f"  DB error: {e}")
                stats["silver_skipped"] += 1
            continue

        # ── Kretsschema / tag drawings ────────────────────────────────
        if is_kretsschema(pdf_path):
            stem   = pdf_path.stem
            parsed = parse_tag(stem)
            if parsed is None:
                # not a standard tag filename — skip silently
                stats["tags_skipped"] += 1
                continue
            try:
                cur.execute(
                    "INSERT OR REPLACE INTO tag_drawings "
                    "(tag_number, tag_type, process_area, subsystem_type, file_path) "
                    "VALUES (?,?,?,?,?)",
                    (
                        parsed["tag_number"],
                        parsed["tag_type"],
                        parsed["process_area"],
                        parsed["subsystem_type"],
                        rel_path,
                    ),
                )
                con.commit()
                stats["tags_processed"] += 1
            except sqlite3.Error as e:
                print(f"  DB error: {e}")
                stats["tags_skipped"] += 1
            continue

        # ── Certificate PDFs ──────────────────────────────────────────
        # Only process files under a 'Certifikat' directory
        parts_lower = [p.lower() for p in pdf_path.parts]
        if "certifikat" not in parts_lower:
            # Root-level docs (SRS, functional descriptions, etc.) — skip
            continue

        print(f"[CERT]   {rel_path}")
        text = extract_text_from_pdf(pdf_path)
        if not text:
            stats["certs_skipped"] += 1
            continue

        manufacturer  = manufacturer_from_path(pdf_path)
        model         = pdf_path.stem[:120]
        lambda_du     = extract_rate_near_keyword(text, LAMBDA_DU_RE)
        lambda_dd     = extract_rate_near_keyword(text, LAMBDA_DD_RE)
        lambda_d      = extract_rate_near_keyword(text, LAMBDA_D_RE)
        lambda_s      = extract_rate_near_keyword(text, LAMBDA_S_RE)
        dc_pct        = extract_pct_near_keyword(text, DC_RE)
        sff_pct       = extract_pct_near_keyword(text, SFF_RE)
        sil_level     = extract_sil(text)
        proof_test    = extract_proof_test(text)
        summary       = text[:500].replace("\x00", "")

        try:
            cur.execute(
                "INSERT OR REPLACE INTO certificates "
                "(manufacturer, model, file_path, lambda_du_fit, lambda_dd_fit, "
                " lambda_d_fit, lambda_s_fit, dc_pct, sff_pct, sil_level, "
                " proof_test_note, summary_text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    manufacturer, model, rel_path,
                    lambda_du, lambda_dd, lambda_d, lambda_s,
                    dc_pct, sff_pct, sil_level,
                    proof_test, summary,
                ),
            )
            con.commit()
            stats["certs_processed"] += 1
        except sqlite3.Error as e:
            print(f"  DB error: {e}")
            stats["certs_skipped"] += 1

    return stats


def print_summary(con, stats):
    cur = con.cursor()

    print("\n" + "=" * 60)
    print("BUILD SUMMARY")
    print("=" * 60)

    print(f"\nCertificates   processed : {stats['certs_processed']}")
    print(f"Certificates   skipped   : {stats['certs_skipped']}")
    print(f"SILver reports processed : {stats['silver_processed']}")
    print(f"SILver reports skipped   : {stats['silver_skipped']}")
    print(f"Tag drawings   processed : {stats['tags_processed']}")
    print(f"Tag drawings   skipped   : {stats['tags_skipped']}")

    # Certificates with failure rate data
    cur.execute(
        "SELECT COUNT(*) FROM certificates WHERE lambda_du_fit IS NOT NULL"
    )
    n_with_ldu = cur.fetchone()[0]
    print(f"\nCertificates with λDU data : {n_with_ldu}")

    cur.execute(
        "SELECT COUNT(*) FROM certificates WHERE sil_level IS NOT NULL"
    )
    n_with_sil = cur.fetchone()[0]
    print(f"Certificates with SIL level: {n_with_sil}")

    # Breakdown by manufacturer
    cur.execute(
        "SELECT manufacturer, COUNT(*) as n "
        "FROM certificates GROUP BY manufacturer ORDER BY n DESC"
    )
    rows = cur.fetchall()
    if rows:
        print("\nCertificates by manufacturer:")
        for mfr, n in rows:
            print(f"  {mfr:<30} {n:>4}")

    # SILver report areas
    cur.execute("SELECT project_area, sif_range FROM silver_reports ORDER BY project_area")
    rows = cur.fetchall()
    if rows:
        print("\nSILver reports:")
        for area, sif_range in rows:
            print(f"  [{area}] {sif_range}")

    # Tag breakdown
    cur.execute(
        "SELECT tag_type, subsystem_type, COUNT(*) as n "
        "FROM tag_drawings GROUP BY tag_type, subsystem_type ORDER BY n DESC"
    )
    rows = cur.fetchall()
    if rows:
        print("\nTag drawings by type:")
        for ttype, stype, n in rows:
            print(f"  {ttype:<12} ({stype:<8})  {n:>4}")

    # Sample certificates with λDU
    cur.execute(
        "SELECT manufacturer, model, lambda_du_fit, sil_level "
        "FROM certificates WHERE lambda_du_fit IS NOT NULL "
        "ORDER BY manufacturer LIMIT 10"
    )
    rows = cur.fetchall()
    if rows:
        print("\nSample certificates with λDU (FIT):")
        for mfr, model, ldu, sil in rows:
            sil_str = f"SIL{sil}" if sil else "SIL?"
            print(f"  {mfr:<20} {model[:40]:<42} λDU={ldu:<10} {sil_str}")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"Source  : {BASE_DIR}")
    print(f"Database: {DB_PATH}")

    if not BASE_DIR.exists():
        print(f"ERROR: Source folder not found: {BASE_DIR}")
        sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = create_db(DB_PATH)

    try:
        stats = process_all(con)
        print_summary(con, stats)
    finally:
        con.close()

    print(f"\nDatabase written to: {DB_PATH}")


if __name__ == "__main__":
    main()
