# -*- coding: utf-8 -*-
"""
parse_silver_pdf.py — Extraherar SIF-data från SILver Detailed Report PDFs
och lagrar resultaten i silver_sifs-tabellen i sil_components.db.

Kör: python parse_silver_pdf.py
"""

import sys
import io
import re
import os
import sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: pymupdf not installed. Run: pip install pymupdf")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "sil_components.db")

PDF_FILES = [
    (
        r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIF\GE\Hybrit Pilot Plant SIF-058 - 074 SILver Detailed Report REV 1.pdf",
        "GE",
    ),
    (
        r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIF\Övrigt\Hybrit Pilot Plant SIF-049 - 057, 076, 077 SILver Detailed Report REV 1.pdf",
        "Övrigt",
    ),
    (
        r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIF\Hazop 2\Hybrit Pilot Plant SIF-011 - 048, 075, 079 - 082 SILver Detailed Report REV 1.pdf",
        "Hazop 2",
    ),
]

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
RE_SIF_ID     = re.compile(r'\bSIF-(\d{3})\b')
RE_SCI_FLOAT  = re.compile(r'(\d+\.\d+[Ee][+\-]\d+)')
RE_TI_MONTHS  = re.compile(r'Proof Test Interval\s*\[Months\][:\s]*(\d+)')
RE_PTC        = re.compile(r'Proof Test Coverage\s*\[%\][:\s]*(\d+\.?\d*)')
RE_MRT        = re.compile(r'MRT\s*\[Hours\][:\s]*(\d+)')
RE_BETA       = re.compile(r'-factor\s*\[%\][:\s]*(\d+\.?\d*)')
RE_MISSION    = re.compile(r'Mission Time[:\s]*(\d+)\s*year', re.IGNORECASE)

# Logic solver architecture via HFT (Hardware Fault Tolerance)
# HFT 0 → 1oo1 equivalent, HFT 1 → 1oo2D / Type B
RE_HFT        = re.compile(r'\bHFT[:\s]+(\d+)')

# ---------------------------------------------------------------------------
# Load all PDF pages as (page_no, text) list
# ---------------------------------------------------------------------------

def load_pdf_pages(pdf_path):
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        pages.append((i + 1, page.get_text()))
    doc.close()
    return pages


# ---------------------------------------------------------------------------
# Split full-document text into per-SIF chunks
# ---------------------------------------------------------------------------

def split_into_sif_chunks(pages):
    """Return list of (sif_id, combined_text) tuples, one per SIF section.

    Strategy: look for "This chapter details the SIL Verification results"
    as the anchor for a new SIF section.  The SIF ID is extracted from the
    same paragraph.  This completely avoids ToC lines and multi-line headers.
    """
    # Join all pages
    all_text = '\n'.join(txt for _, txt in pages)

    # Split on the chapter sentinel phrase
    # Each real SIF section starts with: "This chapter details the SIL Verification results..."
    SENTINEL = 'This chapter details the SIL Verification results'
    parts = all_text.split(SENTINEL)

    chunks = []
    seen = set()

    for part in parts[1:]:   # skip leading header before first SIF
        # Find SIF ID in the first 200 chars of this part
        header_region = part[:200]
        m = RE_SIF_ID.search(header_region)
        if not m:
            continue
        sif_id = 'SIF-' + m.group(1)

        # Skip duplicates (the Group-Reuse-Overview section echoes SIF IDs)
        if sif_id in seen:
            continue
        seen.add(sif_id)

        chunks.append((sif_id, SENTINEL + part))

    return chunks


# ---------------------------------------------------------------------------
# Extract a single float value after a regex match in text
# ---------------------------------------------------------------------------

def first_float(pattern, text, default=None):
    m = pattern.search(text)
    if m:
        val = m.group(1)
        if val.upper() == 'N/A':
            return default
        try:
            return float(val)
        except ValueError:
            pass
    return default


# ---------------------------------------------------------------------------
# Extract total PFDavg and RRF from the overall performance table
# ---------------------------------------------------------------------------

def extract_total_pfd_rrf(text):
    """Find the overall Functional Safety Performance table (with PFD + RRF).

    Table layout (linearised):
      Table N Functional Safety Performance
      PFDAVG  RRF  ACHIEVED SIL  MTTFS  PFDAVG  ARCH. CONSTRAINTS  IEC 61511  SYSTEMATIC CAPABILITY
      1.30E-02  76.7  1  2  N/A  29.66

    The subsystem tables use "SIL LIMITS / HFT / SSI" — no RRF column.
    Three search strategies in order of specificity:

    1. "Functional Safety Performance" + "PFDAVG RRF ACHIEVED SIL" nearby → data row
    2. Page-break case: column header on next page ("PFDAVG\nRRF\nACHIEVED") immediately
       followed by data row with sci-float + plain decimal + integer
    3. Any occurrence of pattern: sci-float  decimal  integer (SIL) right after column header
    """
    data_re = re.compile(
        r'(\d+\.\d+[Ee][+\-]\d+)\s+'   # PFDavg
        r'(\d+\.?\d*)\s+'               # RRF (plain decimal)
        r'([12345])\b'                   # achieved SIL digit
    )

    # Strategy 1: title + column header on the same page
    pat1 = re.compile(
        r'Functional Safety Performance\s+'
        r'PFDAVG\s+RRF\s+ACHIEVED SIL',
        re.DOTALL,
    )
    m1 = pat1.search(text)
    if m1:
        snippet = text[m1.end(): m1.end() + 400]
        dm = data_re.search(snippet)
        if dm:
            return float(dm.group(1)), float(dm.group(2))

    # Strategy 2: column header starts a new paragraph (page-break split)
    # "PFDAVG\nRRF\nACHIEVED SIL\n..." followed by data
    pat2 = re.compile(
        r'PFDAVG\s+RRF\s+ACHIEVED SIL\s+'
        r'MTTFS.*?'                  # skip over column names
        r'(\d+\.\d+[Ee][+\-]\d+)\s+'   # PFDavg
        r'(\d+\.?\d*)\s+'               # RRF
        r'([12345])\b',
        re.DOTALL,
    )
    m2 = pat2.search(text)
    if m2:
        return float(m2.group(1)), float(m2.group(2))

    # Strategy 3: Table N Functional Safety Performance → scan next 600 chars
    pat3 = re.compile(
        r'Table\s+\d+\s+Functional Safety Performance\b'
    )
    for m3 in pat3.finditer(text):
        snippet = text[m3.start(): m3.start() + 600]
        dm = data_re.search(snippet)
        if dm:
            return float(dm.group(1)), float(dm.group(2))

    return None, None


# ---------------------------------------------------------------------------
# Extract subsystem parameters
# ---------------------------------------------------------------------------

def extract_subsystem(text, marker_start, marker_end=None):
    """Extract parameters from one subsystem block (sensor / logic / FE).

    Returns dict with:
      arch, ti_months, ptc, mrt, beta, pfd_ref, ldu_fit, ldd_fit
    """
    idx = text.find(marker_start)
    if idx == -1:
        return {}

    if marker_end:
        idx2 = text.find(marker_end, idx + len(marker_start))
        block = text[idx: idx2] if idx2 != -1 else text[idx: idx + 8000]
    else:
        block = text[idx: idx + 8000]

    result = {}

    # --- Architecture ---
    # Sensor/FE: "Voting within group: 1oo2" (most detail) or "Voting between groups: 1oo1"
    arch_within = re.search(r'Voting within group[:\s]+(1oo1|1oo2|2oo2|2oo3|1oo3|2oo4)', block)
    arch_between = re.search(r'Voting between groups[:\s]+(1oo1|1oo2|2oo2|2oo3|1oo3|2oo4)', block)

    if arch_within:
        result['arch'] = arch_within.group(1)
        # If multiple groups: prefer the highest redundancy (most protective)
        # by finding all 'within group' arches and picking the dominant one
        all_within = re.findall(r'Voting within group[:\s]+(1oo1|1oo2|2oo2|2oo3|1oo3|2oo4)', block)
        if len(all_within) > 1:
            # Use the first detailed group architecture
            result['arch'] = all_within[0]
        if arch_between:
            result['arch_between'] = arch_between.group(1)
    elif arch_between:
        result['arch'] = arch_between.group(1)
    else:
        # Logic solver: "Architecture Type: B" with HFT field
        # Map HFT to architecture string
        hft_m = RE_HFT.search(block)
        if hft_m:
            hft = int(hft_m.group(1))
            result['arch'] = f'LS-HFT{hft}'  # e.g. LS-HFT1 for redundant LS

    # --- TI / PTC / MRT / Beta ---
    result['ti_months'] = first_float(RE_TI_MONTHS, block)
    result['ptc']       = first_float(RE_PTC, block)
    result['mrt']       = first_float(RE_MRT, block)

    beta_m = RE_BETA.search(block)
    if beta_m:
        val = beta_m.group(1)
        if val.upper() not in ('N/A', ''):
            try:
                result['beta'] = float(val)
            except ValueError:
                pass

    # --- Subsystem PFD reference ---
    # Pattern: "Table N <Subsystem> Part Functional Safety Performance\n...data row"
    sub_perf_re = re.compile(
        r'(?:Sensor|Logic Solver|Final Element) Part Functional Safety Performance\s+'
        r'PFDAVG\s+SIL LIMITS.*?'
        r'(\d+\.\d+[Ee][+\-]\d+)',
        re.DOTALL,
    )
    sub_m = sub_perf_re.search(block[:4000])
    if sub_m:
        result['pfd_ref'] = float(sub_m.group(1))
    else:
        # Fallback: first sci-float that follows the table header
        fallback = re.search(
            r'Part Functional Safety Performance.*?(\d+\.\d+[Ee][+\-]\d+)',
            block[:3000], re.DOTALL
        )
        if fallback:
            result['pfd_ref'] = float(fallback.group(1))

    # --- Lambda DU / DD from Reliability Data tables ---
    # The table has columns (linearised): SD SU DD DU AD AU NE
    # For sensor/FE tables there are extra header columns: FAIL LOW, FAIL HIGH, FAIL DET.
    # Each component row produces exactly 7 numbers in sequence.
    # We find every "Reliability Data ... Group" section and sum DU and DD.

    rel_table_re = re.compile(
        r'Reliability Data (?:Sensor|Logic Solver|Final Element) Group'
    )
    sff_positions = [m.start() for m in re.finditer(r'SFF \[%\]', block)]

    ldu_sum = 0.0
    ldd_sum = 0.0
    found_any = False

    for rt_m in rel_table_re.finditer(block):
        tbl_start = rt_m.start()
        # End at next SFF or after 2000 chars
        tbl_end = next((p for p in sff_positions if p > tbl_start), tbl_start + 2000)
        tbl_text = block[tbl_start:tbl_end]

        # Check if this is a logic solver table (7 columns) vs sensor/FE (10 columns)
        # Logic solver tables lack FAIL LOW / FAIL HIGH / FAIL DET headers
        is_logic = 'FAIL LOW' not in tbl_text and 'FAIL\nLOW' not in tbl_text

        all_nums = RE_SCI_FLOAT.findall(tbl_text)

        if is_logic:
            # Logic solver: columns are SD SU DD DU AD AU NE (7)
            # Groups of 7 = one component row
            stride = 7
            dd_idx = 2
            du_idx = 3
        else:
            # Sensor/FE: PDF linearises as 10 values per component
            # (FAIL LOW / FAIL HIGH / FAIL DET headers = 3 extra column pairs)
            # Actual column order in data: SD SU / DD DU / AD AU / NE  (per leg: 2+2+2+2+2 = 10 values)
            # But sometimes 7 values are present (no FAIL LOW/HIGH/DET data)
            # Heuristic: use groups of 10 if enough data, else 7
            if len(all_nums) >= 10:
                stride = 10
                dd_idx = 4   # 0=SD,1=SU,2=DD?,3=DU? -- need to verify
                du_idx = 6
            else:
                stride = 7
                dd_idx = 2
                du_idx = 3

        # Sum DU / DD per component
        n = len(all_nums)
        for i in range(0, n - stride + 1, stride):
            try:
                dd_val = float(all_nums[i + dd_idx])
                du_val = float(all_nums[i + du_idx])
                ldd_sum += dd_val
                ldu_sum += du_val
                found_any = True
            except (ValueError, IndexError):
                pass

    if found_any:
        result['ldu_fit'] = ldu_sum * 1e9
        result['ldd_fit'] = ldd_sum * 1e9

    return result


# ---------------------------------------------------------------------------
# Parse one SIF chunk
# ---------------------------------------------------------------------------

def parse_sif_chunk(sif_id, text):
    """Extract all relevant parameters from one SIF text chunk."""
    record = {'sif_id': sif_id}

    # Mission time
    mt_m = RE_MISSION.search(text)
    record['mt_years'] = float(mt_m.group(1)) if mt_m else None

    # Total PFD and RRF
    total_pfd, rrf = extract_total_pfd_rrf(text)
    record['total_pfd_ref'] = total_pfd
    record['rrf_ref']       = rrf

    # Subsystem blocks
    markers = [
        ('sensor', 'Sensor Part Configuration',       'Logic Solver Part Configuration'),
        ('logic',  'Logic Solver Part Configuration', 'Final Element Part Configuration'),
        ('fe',     'Final Element Part Configuration', None),
    ]

    for key, m_start, m_end in markers:
        sub = extract_subsystem(text, m_start, m_end)
        record[f'{key}_arch']    = sub.get('arch')
        record[f'{key}_ti']      = sub.get('ti_months')
        record[f'{key}_ptc']     = sub.get('ptc')
        record[f'{key}_mrt']     = sub.get('mrt')
        record[f'{key}_beta']    = sub.get('beta')
        record[f'{key}_pfd_ref'] = sub.get('pfd_ref')
        record[f'{key}_ldu']     = sub.get('ldu_fit')
        record[f'{key}_ldd']     = sub.get('ldd_fit')

    return record


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS silver_sifs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sif_id          TEXT NOT NULL,
    source_pdf      TEXT,
    mt_years        REAL,
    total_pfd_ref   REAL,
    rrf_ref         REAL,
    sensor_arch     TEXT,
    sensor_ti       REAL,
    sensor_ptc      REAL,
    sensor_mrt      REAL,
    sensor_beta     REAL,
    sensor_pfd_ref  REAL,
    sensor_ldu      REAL,
    sensor_ldd      REAL,
    logic_arch      TEXT,
    logic_ti        REAL,
    logic_ptc       REAL,
    logic_mrt       REAL,
    logic_beta      REAL,
    logic_pfd_ref   REAL,
    logic_ldu       REAL,
    logic_ldd       REAL,
    fe_arch         TEXT,
    fe_ti           REAL,
    fe_ptc          REAL,
    fe_mrt          REAL,
    fe_beta         REAL,
    fe_pfd_ref      REAL,
    fe_ldu          REAL,
    fe_ldd          REAL,
    UNIQUE(sif_id)
)
"""

INSERT_SQL = """
INSERT OR REPLACE INTO silver_sifs (
    sif_id, source_pdf, mt_years,
    total_pfd_ref, rrf_ref,
    sensor_arch, sensor_ti, sensor_ptc, sensor_mrt, sensor_beta, sensor_pfd_ref, sensor_ldu, sensor_ldd,
    logic_arch,  logic_ti,  logic_ptc,  logic_mrt,  logic_beta,  logic_pfd_ref,  logic_ldu,  logic_ldd,
    fe_arch,     fe_ti,     fe_ptc,     fe_mrt,     fe_beta,     fe_pfd_ref,     fe_ldu,     fe_ldd
) VALUES (
    :sif_id, :source_pdf, :mt_years,
    :total_pfd_ref, :rrf_ref,
    :sensor_arch, :sensor_ti, :sensor_ptc, :sensor_mrt, :sensor_beta, :sensor_pfd_ref, :sensor_ldu, :sensor_ldd,
    :logic_arch,  :logic_ti,  :logic_ptc,  :logic_mrt,  :logic_beta,  :logic_pfd_ref,  :logic_ldu,  :logic_ldd,
    :fe_arch,     :fe_ti,     :fe_ptc,     :fe_mrt,     :fe_beta,     :fe_pfd_ref,     :fe_ldu,     :fe_ldd
)
"""


def init_db(conn):
    conn.execute(CREATE_TABLE)
    conn.commit()


def store_records(conn, records):
    conn.executemany(INSERT_SQL, records)
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    all_records = []

    for pdf_path, source_label in PDF_FILES:
        if not os.path.exists(pdf_path):
            print(f"[SKIP] File not found: {pdf_path}")
            continue

        print(f"\n{'='*70}")
        print(f"Processing: {os.path.basename(pdf_path)}")
        print(f"{'='*70}")

        pages = load_pdf_pages(pdf_path)
        print(f"  Pages loaded: {len(pages)}")

        chunks = split_into_sif_chunks(pages)
        print(f"  SIF chunks found: {len(chunks)}  → {[c[0] for c in chunks]}")

        for sif_id, chunk_text in chunks:
            rec = parse_sif_chunk(sif_id, chunk_text)
            rec['source_pdf'] = source_label
            all_records.append(rec)

    # Store to DB
    store_records(conn, all_records)
    conn.close()

    # ---------------------------------------------------------------------------
    # Print summary table
    # ---------------------------------------------------------------------------
    print(f"\n{'='*70}")
    print(f"SUMMARY — {len(all_records)} SIFs extracted")
    print(f"{'='*70}")

    fmt = "{:<10} {:<8} {:>12}  {:>8}  {:<8} {:<8} {:<8}"
    print(fmt.format("SIF", "Source", "PFDavg", "RRF", "S-arch", "L-arch", "FE-arch"))
    print("-" * 75)

    ok_count = 0
    for r in sorted(all_records, key=lambda x: x['sif_id']):
        pfd_str = f"{r['total_pfd_ref']:.2E}" if r['total_pfd_ref'] else "N/A"
        rrf_str = f"{r['rrf_ref']:.1f}"       if r['rrf_ref']       else "N/A"
        s_arch  = r.get('sensor_arch') or "?"
        l_arch  = r.get('logic_arch')  or "?"
        fe_arch = r.get('fe_arch')     or "?"
        print(fmt.format(
            r['sif_id'], r['source_pdf'], pfd_str, rrf_str, s_arch, l_arch, fe_arch
        ))
        if r['total_pfd_ref'] is not None:
            ok_count += 1

    print("-" * 75)
    print(f"Successfully extracted PFDavg for {ok_count}/{len(all_records)} SIFs")

    # Detailed sample output
    print(f"\n{'='*70}")
    print("DETAILED SAMPLE (first 5 SIFs):")
    print(f"{'='*70}")
    for r in sorted(all_records, key=lambda x: x['sif_id'])[:5]:
        pfd_str = f"{r['total_pfd_ref']:.4E}" if r['total_pfd_ref'] else "N/A"
        print(f"\n  {r['sif_id']} [{r['source_pdf']}]  PFDavg={pfd_str}  RRF={r['rrf_ref']}  MT={r['mt_years']}yr")
        print(f"    Sensor: arch={r['sensor_arch']}, TI={r['sensor_ti']}mon, PTC={r['sensor_ptc']}%, β={r['sensor_beta']}%")
        print(f"            PFD_ref={r['sensor_pfd_ref']},  λDU={r['sensor_ldu']} FIT,  λDD={r['sensor_ldd']} FIT")
        print(f"    Logic:  arch={r['logic_arch']}, TI={r['logic_ti']}mon, PTC={r['logic_ptc']}%, β={r['logic_beta']}%")
        print(f"            PFD_ref={r['logic_pfd_ref']},  λDU={r['logic_ldu']} FIT,  λDD={r['logic_ldd']} FIT")
        print(f"    FE:     arch={r['fe_arch']}, TI={r['fe_ti']}mon, PTC={r['fe_ptc']}%, β={r['fe_beta']}%")
        print(f"            PFD_ref={r['fe_pfd_ref']},  λDU={r['fe_ldu']} FIT,  λDD={r['fe_ldd']} FIT")


if __name__ == "__main__":
    main()
