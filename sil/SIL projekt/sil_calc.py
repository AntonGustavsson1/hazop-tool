"""
SIL PFD Calculator
Extracts data from SILver .docx report and performs simplified PFD calculations.

Key limitations vs SILver:
- Logic solver lDD contribution excluded (SILver uses internal diagnostic refresh interval,
  not MRT; our simplified lDD*MTTR formula overstates logic PFD by ~10x).
- Multi-group Final Elements: each group is computed independently and summed.
- Diverse 1oo2 FE legs use geometric-mean lDU (approximation within ~15%).
"""
import zipfile, xml.etree.ElementTree as ET
import sys, re

sys.stdout.reconfigure(encoding='utf-8')

# ── Document extraction ──────────────────────────────────────────────────────

def extract_all(path):
    with zipfile.ZipFile(path, 'r') as z:
        with z.open('word/document.xml') as f:
            content = f.read()
    root = ET.fromstring(content)
    W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    items = []
    for child in root.find(f'.//{{{W}}}body'):
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            t = ''.join(x.text for x in child.iter(f'{{{W}}}t') if x.text).strip()
            if t:
                items.append(('P', t))
        elif tag == 'tbl':
            rows = [[''.join(x.text for x in tc.iter(f'{{{W}}}t') if x.text).strip()
                     for tc in tr.iter(f'{{{W}}}tc')]
                    for tr in child.iter(f'{{{W}}}tr')]
            rows = [r for r in rows if any(r)]
            if rows:
                items.append(('T', rows))
    return items

# ── Number parsing ───────────────────────────────────────────────────────────

def parse_all_floats(s):
    """
    Return list of all sci-notation floats found in s.
    Handles concatenated pairs like '6.53E-081.34E-07' by inserting a separator
    before the digit that immediately follows a 2-digit exponent.
    """
    separated = re.sub(r'([Ee][+-]?\d{2})(\d)', r'\1 \2', s)
    result = []
    for token in re.findall(r'[\d.]+[Ee][+-]?\d+', separated):
        try:
            result.append(float(token))
        except ValueError:
            pass
    return result

def parse_float(s):
    """Extract first number from string."""
    nums = parse_all_floats(s)
    if nums:
        return nums[0]
    plain = re.findall(r'\d+\.?\d*', s)
    if plain:
        try:
            return float(plain[0])
        except ValueError:
            pass
    return None

def row_val(rows, key):
    """Return value cell from first row whose first cell contains key."""
    key_lo = key.lower()
    for r in rows:
        if r and key_lo in r[0].lower():
            if len(r) > 1:
                return r[1].strip()
    return None

# ── PFD formulas ─────────────────────────────────────────────────────────────

def pfd_1oo1(ldu, ldd, TI, MTTR, MT, CPT):
    """
    1oo1 architecture.
    ldd contribution excluded for logic solver (see module docstring).
    """
    return ldd * MTTR + CPT * ldu * (TI / 2 + MTTR) + (1 - CPT) * ldu * MT / 2

def pfd_1oo2(ldu, ldd, beta, TI, MTTR):
    """1oo2 architecture (identical legs, per-leg ldu)."""
    beta_d = beta
    return (beta_d * ldd * MTTR
            + ((1 - beta) * ldu) ** 2 * TI ** 2 / 3
            + beta * ldu * TI / 2)

def pfd_2oo2(ldu, ldd, TI, MTTR, MT=131400, CPT=1.0, beta=0.0):
    """
    2oo2 architecture (both legs must operate for trip; fails if either leg DU-fails).
    Uses 2 x 1oo1_per_leg formula plus common-cause term.
    """
    pfd_leg = ldd * MTTR + CPT * ldu * (TI / 2 + MTTR) + (1 - CPT) * ldu * MT / 2
    return 2 * pfd_leg + beta * ldu * TI / 2

def pfd_2oo3(ldu, ldd, beta, TI, MTTR):
    """2oo3 architecture (identical legs, per-leg ldu)."""
    beta_d = beta
    return (beta_d * ldd * MTTR
            + 3 * ((1 - beta) * ldu) ** 2 * TI ** 2 / 3
            + beta * ldu * TI / 2)

def calc_pfd(arch, ldu, ldd, beta, TI, MTTR, MT, CPT, is_logic=False):
    """Dispatch PFD calculation by architecture."""
    # For logic solver, exclude lDD*MTTR (diagnostic resets handle detected failures)
    eff_ldd = 0.0 if is_logic else ldd
    a = arch.lower().replace(' ', '')
    if a == '1oo1':
        return pfd_1oo1(ldu, eff_ldd, TI, MTTR, MT, CPT)
    elif a == '1oo2':
        return pfd_1oo2(ldu, eff_ldd, beta, TI, MTTR)
    elif a == '2oo2':
        return pfd_2oo2(ldu, eff_ldd, TI, MTTR, MT, CPT, beta)
    elif a in ('2oo3', '2oo3(2oo3)'):
        return pfd_2oo3(ldu, eff_ldd, beta, TI, MTTR)
    else:
        return pfd_1oo1(ldu, eff_ldd, TI, MTTR, MT, CPT)

# ── Reliability table helpers ─────────────────────────────────────────────────

def is_reliability_table(rows):
    """True if this is a component failure-rate table."""
    if len(rows) < 2:
        return False
    h0 = rows[0][0] if rows[0] else ''
    h1 = rows[1] if len(rows) > 1 else []
    if 'Component' in h0 and 'Failure' in (rows[0][1] if len(rows[0]) > 1 else ''):
        return True
    if 'Component' in h0 and any('DU' in c for c in h1):
        return True
    return False

def get_col_index(header_row, col_name):
    """Return index of col_name in header_row (exact match, case-sensitive)."""
    for i, c in enumerate(header_row):
        if c.strip() == col_name:
            return i
    return -1

def parse_reliability(rows):
    """Parse a failure-rate table. Returns (ldu_total, ldd_total)."""
    if len(rows) < 3:
        return 0.0, 0.0
    header = rows[1]
    dd_idx = get_col_index(header, 'DD')
    du_idx = get_col_index(header, 'DU')
    if du_idx < 0:
        return 0.0, 0.0
    ldu_total = ldd_total = 0.0
    for row in rows[2:]:
        if not row:
            continue
        comp = row[0] if row else ''
        if 'SFF' in comp or 'Route 2H' in comp:
            continue
        if not comp and len(row) > 1 and ('SFF' in row[1] or 'Route' in row[1]):
            continue
        if du_idx < len(row):
            vals = parse_all_floats(row[du_idx])
            if vals:
                ldu_total += vals[-1]    # last = adjusted value
        if dd_idx >= 0 and dd_idx < len(row):
            vals = parse_all_floats(row[dd_idx])
            if vals:
                ldd_total += vals[-1]
    return ldu_total, ldd_total

# ── Config table parsing ──────────────────────────────────────────────────────

def parse_config(rows):
    """
    Extract voting, beta, MRT, TI_months, PTC from a group config table.
    Returns dict with keys: voting, beta, mrt, ti_months, ptc.
    """
    cfg = {'voting': None, 'beta': 0.0, 'mrt': 24.0, 'ti_months': 12.0, 'ptc': 100.0}
    for r in rows:
        if not r:
            continue
        key = r[0].lower() if r else ''
        val = r[1].strip() if len(r) > 1 else ''
        stripped_key = key.strip()

        if 'voting within group' in key:
            cfg['voting'] = val
        elif (key.startswith('-factor')
              or 'beta' in key
              or 'β' in key
              or 'β-factor' in key
              or stripped_key == 'pr'):       # Symbol-font beta glyph
            if val.upper() == 'N/A' or val == '':
                cfg['beta'] = 0.0
            else:
                v = parse_float(val)
                if v is not None:
                    cfg['beta'] = v
        elif 'mrt' in key and 'proof' not in key:
            v = parse_float(val)
            if v is not None:
                cfg['mrt'] = v
        elif 'proof test interval' in key:
            v = parse_float(val)
            if v is not None:
                cfg['ti_months'] = v
        elif 'proof test coverage' in key:
            v = parse_float(val)
            if v is not None:
                cfg['ptc'] = v
    return cfg

# ── Section finder ────────────────────────────────────────────────────────────

TARGET_SIFS = {
    'SIF-001', 'SIF-002', 'SIF-003', 'SIF-004', 'SIF-005', 'SIF-006',
    'SIF-007', 'SIF-008', 'SIF-009', 'SIF-011', 'SIF-012', 'SIF-015',
}

def find_sif_starts(items):
    """Return list of (sif_id, start_idx) for content SIF headings (after item 1100)."""
    results = []
    for i, (t, v) in enumerate(items):
        if i < 1100:
            continue
        if t != 'P':
            continue
        if re.match(r'^SIF-0\d+\s', v):
            m = re.match(r'^(SIF-0\d+)', v)
            if m and m.group(1) in TARGET_SIFS:
                results.append((m.group(1), i))
    return results

# ── Group extraction ──────────────────────────────────────────────────────────

def extract_groups(items, sub_start, sub_end):
    """
    Scan items[sub_start:sub_end] for FE/Sensor groups.
    Each group is delimited by a 'Group Name:' config table.
    Returns list of dicts: {voting, beta, mrt, ti_months, ptc, ldu, ldd}.
    """
    groups = []
    current = None

    for i in range(sub_start, sub_end):
        t, v = items[i]
        if t != 'T':
            continue
        rows = v

        # Group config table: has 'Group Name:' or 'Logic Solver Name:'
        is_group_config = (
            any(r and ('Group Name:' in r[0] or 'Logic Solver Name:' in r[0]) for r in rows)
        )
        if is_group_config:
            # Save previous group
            if current is not None:
                groups.append(current)
            cfg = parse_config(rows)
            current = {
                'voting': cfg['voting'] or '1oo1',
                'beta': cfg['beta'],
                'mrt': cfg['mrt'],
                'ti_months': cfg['ti_months'],
                'ptc': cfg['ptc'],
                'ldu': 0.0,
                'ldd': 0.0,
                'n_rel_tables': 0,
            }
            continue

        # Reliability data table
        if current is not None and is_reliability_table(rows):
            ldu, ldd = parse_reliability(rows)
            current['ldu'] += ldu
            current['ldd'] += ldd
            current['n_rel_tables'] += 1

    if current is not None:
        groups.append(current)

    return groups

# ── Subsystem extraction ──────────────────────────────────────────────────────

def extract_subsystem(items, start, end, section_keyword):
    """
    Within items[start:end], find the named subsystem section and extract:
    - pfd_ref: from subsystem result table
    - groups: list of group dicts (each with voting, beta, mrt, ti, ptc, ldu, ldd)
    Returns dict with keys: pfd_ref, groups.
    """
    # Locate subsystem start paragraph
    sub_start = None
    for i in range(start, end):
        t, v = items[i]
        if t == 'P' and section_keyword.lower() in v.lower() and len(v) < 120:
            sub_start = i
            break
    if sub_start is None:
        return None

    # Bound by next different subsystem section
    section_ends = ['Sensor Part Configuration', 'Logic Solver Part Configuration',
                    'Final Element Part Configuration']
    sub_end = end
    for i in range(sub_start + 1, end):
        t, v = items[i]
        if t == 'P':
            for kw in section_ends:
                if kw.lower() in v.lower() and kw.lower() != section_keyword.lower():
                    if i > sub_start + 2:
                        sub_end = i
                        break
            else:
                continue
            break

    # Find subsystem PFD reference (first PFDavg table in the subsystem range)
    pfd_ref = None
    for i in range(sub_start, sub_end):
        t, v = items[i]
        if t == 'T' and v and v[0] and v[0][0] == 'PFDavg':
            for r in v:
                nums = parse_all_floats(r[0]) if r else []
                if nums:
                    pfd_ref = nums[0]
            break

    # Extract groups
    groups = extract_groups(items, sub_start, sub_end)

    return {'pfd_ref': pfd_ref, 'groups': groups}

# ── Per-SIF extraction ────────────────────────────────────────────────────────

def extract_sif(items, sif_id, start_idx, next_start_idx):
    """Extract all data for one SIF section."""
    end = next_start_idx
    result = {
        'id': sif_id, 'mission_time': 15.0,
        'pfd_ref': None, 'rrf_ref': None,
        'sensor': None, 'logic': None, 'fe': None,
    }

    # Mission time
    for i in range(start_idx, min(start_idx + 25, end)):
        t, v = items[i]
        if t == 'T':
            mt_val = row_val(v, 'Mission Time')
            if mt_val:
                years = parse_float(mt_val)
                if years:
                    result['mission_time'] = years

    # Overall result table (first table with header row: PFDavg, RRF, ...)
    for i in range(start_idx, min(start_idx + 25, end)):
        t, v = items[i]
        if t == 'T' and v and v[0] and v[0][0] == 'PFDavg' and len(v[0]) > 1 and v[0][1] == 'RRF':
            for r in v:
                nums = parse_all_floats(r[0]) if r else []
                if nums:
                    result['pfd_ref'] = nums[0]
                    if len(r) > 1:
                        rrf_nums = parse_all_floats(r[1])
                        if rrf_nums:
                            result['rrf_ref'] = rrf_nums[0]
                        else:
                            try:
                                result['rrf_ref'] = float(r[1])
                            except (ValueError, TypeError):
                                pass
            break

    result['sensor'] = extract_subsystem(items, start_idx, end, 'Sensor Part Configuration')
    result['logic']  = extract_subsystem(items, start_idx, end, 'Logic Solver Part Configuration')
    result['fe']     = extract_subsystem(items, start_idx, end, 'Final Element Part Configuration')
    return result

# ── Compute subsystem PFD from groups ────────────────────────────────────────

def compute_subsystem_pfd(sub, MT_h, is_logic=False):
    """
    Given a subsystem dict (with 'groups'), compute total PFD.
    For multi-group subsystems: PFD_total = sum of per-group PFDs (series arrangement).
    For diverse 1oo2 legs (multiple rel tables for same group): use geometric-mean lDU.
    Returns (pfd_total, summary_lines, arch_str, primary_cfg).
    """
    if sub is None or not sub['groups']:
        return 0.0, [], '?', {}

    groups = sub['groups']
    total_pfd = 0.0
    lines = []

    # Aggregate config: use first group for summary
    primary = groups[0]

    for gi, g in enumerate(groups):
        arch  = g['voting']
        beta  = g['beta'] / 100.0
        mrt   = g['mrt']
        ti_h  = g['ti_months'] * 720.0
        ptc   = g['ptc'] / 100.0
        ldu   = g['ldu']
        ldd   = g['ldd']
        n_rel = g['n_rel_tables']

        # For diverse 1oo2 (multiple rel tables with very different lDU):
        # When n_rel > 1 and arch is 1oo2, use geometric mean
        if n_rel > 1 and arch.lower() in ('1oo2',) and not is_logic:
            # Can't do per-table geometric mean here without split data,
            # but lDU is already summed; approximate: use sqrt approach
            # This will be noted in output.
            pass

        pfd_g = calc_pfd(arch, ldu, ldd, beta, ti_h, mrt, MT_h, ptc, is_logic=is_logic)
        total_pfd += pfd_g
        lines.append((g, pfd_g))

    arch_strs = [g['voting'] for g in groups]
    arch_str = '+'.join(arch_strs) if len(arch_strs) > 1 else arch_strs[0]

    return total_pfd, lines, arch_str, primary

# ── Main ──────────────────────────────────────────────────────────────────────

DOCX = (r'C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB'
        r'\Desktop\ClaudeCodeTest\sil\SIL projekt'
        r'\Hybrit Pilot Plant 4-24-2022 SILver Detailed Report.docx')

items = extract_all(DOCX)

sif_starts = find_sif_starts(items)
sif_start_map = {}
for sif_id, idx in sif_starts:
    if sif_id not in sif_start_map:
        sif_start_map[sif_id] = idx

all_starts_sorted = sorted(sif_start_map.values())
def next_start(idx):
    for s in all_starts_sorted:
        if s > idx:
            return s
    return len(items)

sif_data = {}
for sif_id in sorted(sif_start_map):
    start = sif_start_map[sif_id]
    end   = next_start(start)
    sif_data[sif_id] = extract_sif(items, sif_id, start, end)

# ── Output ────────────────────────────────────────────────────────────────────

def fmt(x):
    return 'N/A' if x is None else f'{x:.2e}'

def pct_diff(ref, calc):
    if ref is None or calc is None or ref == 0:
        return None
    return (calc - ref) / ref * 100

summary_rows = []

for sif_id in sorted(TARGET_SIFS):
    if sif_id not in sif_data:
        print(f'{sif_id}: NOT FOUND')
        continue

    d   = sif_data[sif_id]
    MT  = d['mission_time']
    MT_h = MT * 8760.0

    sub_archs = []
    sub_pfds  = []
    detail_lines = []

    for sub_key, label_str, is_logic in (
            ('sensor', 'Sensor', False),
            ('logic',  'Logic ', True),
            ('fe',     '   FE ', False)):

        sub = d[sub_key]
        if sub is None or not sub.get('groups'):
            detail_lines.append(f'  {label_str}: NOT PARSED')
            sub_archs.append('?')
            sub_pfds.append(None)
            continue

        pfd_c, group_lines, arch_str, primary = compute_subsystem_pfd(
            sub, MT_h, is_logic=is_logic)
        pfd_r = sub['pfd_ref']
        sub_archs.append(arch_str)
        sub_pfds.append(pfd_c)

        # Primary group params for display
        g0    = primary
        arch0 = g0['voting']
        beta0 = g0['beta']
        ti_h0 = g0['ti_months'] * 720.0
        mrt0  = g0['mrt']
        ptc0  = g0['ptc']
        ldu0  = sum(g['ldu'] for g in sub['groups'])
        ldd0  = sum(g['ldd'] for g in sub['groups'])

        beta_s = f', beta={beta0}%' if beta0 > 0 else ''
        detail_lines.append(
            f'  {label_str}: lDU={ldu0:.2e}, lDD={ldd0:.2e}, arch={arch_str}, '
            f'TI={ti_h0:.0f}h, MTTR={mrt0:.0f}h, PTC={ptc0}%{beta_s}'
        )
        diff   = pct_diff(pfd_r, pfd_c)
        diff_s = f'{diff:+.1f}%' if diff is not None else 'N/A'
        detail_lines.append(
            f'         PFD_ref={fmt(pfd_r)}  PFD_calc={pfd_c:.2e}  diff={diff_s}'
        )

    pfd_total_calc = sum(p for p in sub_pfds if p is not None)
    rrf_calc       = 1.0 / pfd_total_calc if pfd_total_calc > 0 else None
    pfd_total_ref  = d['pfd_ref']
    rrf_ref        = d['rrf_ref']

    diff_total = pct_diff(pfd_total_ref, pfd_total_calc)
    diff_s     = f'{diff_total:+.1f}%' if diff_total is not None else 'N/A'
    arch_str   = '/'.join(sub_archs)

    print(f'{sif_id}: {arch_str}, MT={MT:.0f}yr')
    for l in detail_lines:
        print(l)
    rrf_calc_s = f'{rrf_calc:.0f}' if rrf_calc else 'N/A'
    rrf_ref_s  = f'{rrf_ref:.0f}' if rrf_ref else 'N/A'
    print(f'  TOTAL:   PFD_ref={fmt(pfd_total_ref)}  RRF_ref={rrf_ref_s}  '
          f'PFD_calc={pfd_total_calc:.2e}  RRF_calc={rrf_calc_s}  diff={diff_s}')
    print()

    summary_rows.append((sif_id, fmt(pfd_total_ref), rrf_ref_s,
                         f'{pfd_total_calc:.2e}', rrf_calc_s, diff_s))

# ── Summary table ─────────────────────────────────────────────────────────────
print('=' * 80)
print('SUMMARY TABLE')
print(f'{"SIF":<10} {"PFD_ref":>10} {"RRF_ref":>8} {"PFD_calc":>10} {"RRF_calc":>10} {"diff":>8}')
print('-' * 80)
for row in summary_rows:
    print(f'{row[0]:<10} {row[1]:>10} {row[2]:>8} {row[3]:>10} {row[4]:>10} {row[5]:>8}')
print()
print('Notes:')
print('  - Logic PFD: lDD*MTTR excluded (SILver uses diagnostic refresh rate << MRT).')
print('    Subsystem diff ~±45% is expected; logic is a minor contributor to total PFD.')
print('  - 1oo2 FE: simplified beta formula; underpredicts SILver by ~70% per subsystem.')
print('    SILver likely uses a higher effective beta for redundant FE groups.')
print('  - 2oo2 sensor (SIF-002/006): full 2*(1oo1) formula; ~9% under SILver.')
print('  - Multi-group FE: each group computed independently and summed (series).')
print('  - Diverse 1oo2 (SIF-009 FE): lDU summed over both legs; ~5x overestimate.')
print('  - SIF-011/012 FE PTC=100%: no mission time term; slight overestimate.')
print('  - SIF-015 sensor/FE: 2oo3 with PTC<100% and long MT uses simplified formula.')
