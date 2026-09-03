# -*- coding: utf-8 -*-
import sys, io, re, zipfile, xml.etree.ElementTree as ET, unicodedata
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Kopiera exakt samma funktioner som hybrit_calc.py ──────────────────────────
def norm(s):
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'[      ]', ' ', s)
    return s.strip()

def parse_first_float(s):
    s = norm(s).replace(',', '.')
    if not s or s in ('-', '--', 'N/A', 'n/a', ''): return 0.0
    m = re.search(r'[0-9]+\.?[0-9]*[Ee][+-]?[0-9]{1,2}', s)
    if m: return float(m.group())
    m = re.search(r'[0-9]+\.[0-9]+', s)
    if m: return float(m.group())
    m = re.search(r'[0-9]+', s)
    if m: return float(m.group())
    return 0.0

def parse_all_floats(s):
    s = norm(s).replace(',', '.')
    return [float(x) for x in re.findall(r'[0-9]+\.?[0-9]*[Ee][+-]?[0-9]{1,2}', s)]

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
            t = norm(''.join(x.text for x in child.iter(f'{{{W}}}t') if x.text))
            if t: items.append(('P', t))
        elif tag == 'tbl':
            rows = []
            for tr in child.iter(f'{{{W}}}tr'):
                cells = []
                for tc in tr.iter(f'{{{W}}}tc'):
                    full = norm(''.join(x.text for x in tc.iter(f'{{{W}}}t') if x.text))
                    cells.append(full)
                if any(cells): rows.append(cells)
            if rows: items.append(('T', rows))
    return items

def extract_sif(items, start_idx):
    data = {'mission_time_years': 15.0}
    subsystems = {
        'sensor': {'arch':'1oo1','beta':0.0,'mttr':8.0,'ti':8760.0,'ptc':1.0,
                   'lambda_du':0.0,'lambda_dd':0.0,'pfd_ref':0.0,
                   'n_groups':1,'beta_between':0.0,'batches_before_pt':0,'io_type':None},
        'logic':  {'arch':'1oo1','beta':0.0,'mttr':8.0,'ti':8760.0,'ptc':1.0,
                   'lambda_du':0.0,'lambda_dd':0.0,'pfd_ref':0.0,
                   'n_groups':1,'beta_between':0.0,'batches_before_pt':0,'io_type':None},
        'fe':     {'arch':'1oo1','beta':0.0,'mttr':8.0,'ti':8760.0,'ptc':1.0,
                   'lambda_du':0.0,'lambda_dd':0.0,'pfd_ref':0.0,
                   'n_groups':1,'beta_between':0.0,'batches_before_pt':0,'io_type':None},
    }
    current_sub = None
    expect_beta_between = False
    SIF_STOP = re.compile(r'^SIF-[0-9]+ (SIL[0-9]|Low|High|Diff|Temp|Gas|Flode|Tryck|Kond|Bera)')

    for kind, content in items[start_idx: start_idx + 800]:
        if kind == 'P':
            txt = content
            if txt != items[start_idx][1] and SIF_STOP.match(txt): break
            if 'Sensor Part Configuration' in txt: current_sub = 'sensor'
            elif 'Logic Solver Part Configuration' in txt: current_sub = 'logic'
            elif 'Final Element Part Configuration' in txt: current_sub = 'fe'
            continue
        rows = content
        flat = ' '.join(c for row in rows for c in row)
        for row in rows:
            if row and 'Mission Time' in row[0] and len(row) > 1:
                mt = parse_first_float(row[1])
                if mt > 0: data['mission_time_years'] = mt
        if 'PFDavg' in flat and 'RRF' in flat and 'Achieved SIL' in flat:
            for row in rows:
                if row and re.search(r'[0-9]+E[+-][0-9]+', row[0]):
                    pfd = parse_first_float(row[0])
                    if 0 < pfd < 1:
                        data.setdefault('total_pfd_ref', pfd)
                        if len(row) > 1: data.setdefault('rrf_ref', parse_first_float(row[1]))
        if current_sub and 'SIL Limits' in flat and 'MTTFS' in flat:
            for row in rows:
                if row and re.search(r'[0-9]+E[+-][0-9]+', row[0]):
                    pfd = parse_first_float(row[0])
                    if 0 < pfd < 1: subsystems[current_sub]['pfd_ref'] = pfd
        if current_sub:
            for row in rows:
                if len(row) < 2: continue
                k, v = row[0], row[1]
                if 'Voting within group' in k:
                    subsystems[current_sub]['arch'] = v.strip().replace(' ','')
                elif re.search(r'Number of.*(Sensor|Final Element).*group', k, re.I):
                    n = parse_first_float(v)
                    if n > 1: subsystems[current_sub]['n_groups'] = int(n)
                elif 'MRT' in k and 'Hour' in k:
                    val = parse_first_float(v)
                    if val > 0: subsystems[current_sub]['mttr'] = val
                elif 'Proof Test Interval' in k and 'Month' in k:
                    val = parse_first_float(v)
                    if val > 0: subsystems[current_sub]['ti'] = val * 720
                elif 'Proof Test Coverage' in k:
                    val = parse_first_float(v)
                    if val > 0: subsystems[current_sub]['ptc'] = val / 100
                elif re.search(r'factor\s*\[%\]', k, re.I) or k.strip() == 'pr':
                    beta_val = 0.0 if v in ('N/A','','n/a') else parse_first_float(v)/100
                    if expect_beta_between:
                        subsystems[current_sub]['beta_between'] = beta_val
                        expect_beta_between = False
                    elif beta_val > 0:
                        subsystems[current_sub]['beta'] = beta_val
                elif 'Voting between group' in k:
                    expect_beta_between = True
        if current_sub and 'DU' in flat and 'Component' in flat and len(rows) >= 3:
            du_col = dd_col = None
            for row in rows[:3]:
                for ci, cell in enumerate(row):
                    if cell.strip() == 'DU' and du_col is None: du_col = ci
                    if cell.strip() == 'DD' and dd_col is None: dd_col = ci
                if du_col is not None: break
            if du_col is not None:
                for row in rows:
                    if not row or not row[0]: continue
                    skip = ['Component','SFF','Route','Reliability','Failure',
                            'Sensor Leg','Final ELement','Logic Solver Model']
                    if any(w in row[0] for w in skip): continue
                    if current_sub == 'logic':
                        name = row[0]
                        if 'AI880' in name or 'Analog In Module' in name:
                            subsystems['logic']['io_type'] = 'AI'
                        elif 'DI880' in name or 'Digital In Module' in name:
                            subsystems['logic']['io_type'] = 'DI'
                    if du_col < len(row):
                        vals = parse_all_floats(row[du_col])
                        if vals: subsystems[current_sub]['lambda_du'] += vals[-1]
                    if dd_col is not None and dd_col < len(row):
                        vals = parse_all_floats(row[dd_col])
                        if vals: subsystems[current_sub]['lambda_dd'] += vals[-1]
    data['subsystems'] = subsystems
    return data

def pfd_formula(arch, ldu, ldd, beta, TI, MTTR, MT, CPT):
    bd = beta/2
    a = arch.lower().replace(' ','')
    def pfd1(l_du, l_dd):
        return l_dd*MTTR + CPT*l_du*(TI/2+MTTR) + (1-CPT)*l_du*MT/2
    if a == '1oo1': return pfd1(ldu, ldd)
    elif a == '1oo2':
        indep = ((1-beta)*ldu)**2 * TI**2/3
        ccf_du = beta*pfd1(ldu, 0)
        ccf_dd = bd*ldd*MTTR
        return ccf_dd + indep + ccf_du
    elif a == '2oo2': return (2-beta)*pfd1(ldu,ldd)
    elif a == '2oo3':
        indep = ((1-beta)*ldu)**2 * TI**2
        ccf_du = beta*pfd1(ldu,0)
        ccf_dd = bd*ldd*MTTR
        return ccf_dd + indep + ccf_du
    return pfd1(ldu, ldd)

# ── Kör ────────────────────────────────────────────────────────────────────────
PATH = 'Hybrit Pilot Plant 4-24-2022 SILver Detailed Report.docx'
items = extract_all(PATH)

SIF_HDR = re.compile(r'^(SIF-[0-9]+)\s+(SIL[0-9]|Low|High|Diff|Temp|Gas|Flode|Tryck|Kond|Bera)')
content_start = next(i for i,(k,d) in enumerate(items) if k=='P' and d=='Purpose and Scope')
sif_starts = {}
for i,(kind,data) in enumerate(items[content_start:], content_start):
    if kind!='P': continue
    m = SIF_HDR.match(data)
    if m:
        sid = m.group(1)
        if sid not in sif_starts: sif_starts[sid] = i

REFS = {
    'SIF-001': {'sensor': 1.370e-3, 'logic': 7.31e-6, 'fe': 8.25e-5},
    'SIF-003': {'sensor': 1.050e-3, 'logic': 6.90e-6, 'fe': 9.68e-4},
    'SIF-007': {'sensor': 3.250e-3, 'logic': 7.31e-6, 'fe': 9.66e-4},
    'SIF-008': {'sensor': 1.050e-3, 'logic': 6.90e-6, 'fe': 2.40e-2},
    'SIF-016': {'sensor': 1.400e-3, 'logic': 7.31e-6, 'fe': 7.98e-3},
    'SIF-017': {'sensor': 7.86e-4,  'logic': 7.31e-6, 'fe': 7.98e-3},
}

print(f'{'SIF/Sub':<18} {'arch':>6} {'lDU FIT':>8} {'lDD FIT':>8} {'PTC':>5} '
      f'{'TI h':>6} {'MTTR':>5} {'MT yr':>6} {'PFD_kalk':>10} {'PFD_ref':>10} {'diff':>8}')
print('─'*100)

for sif_id in ['SIF-001','SIF-003','SIF-007','SIF-008','SIF-016','SIF-017']:
    d = extract_sif(items, sif_starts[sif_id])
    MT_h = d['mission_time_years'] * 8760
    subs = d['subsystems']
    for sk in ('sensor','fe'):
        s = subs[sk]
        ldu  = s['lambda_du']
        ldd  = s['lambda_dd']
        arch = s['arch']
        ti   = s['ti']
        mttr = s['mttr']
        ptc  = s['ptc']
        beta = s['beta']
        pfd  = pfd_formula(arch, ldu, ldd, beta, ti, mttr, MT_h, ptc)
        ref  = REFS[sif_id].get(sk, 0)
        diff = (pfd-ref)/ref*100 if ref else 0
        mt_yr = MT_h/8760
        print(f'{sif_id} {sk:<8} {arch:>6} {ldu*1e9:>8.2f} {ldd*1e9:>8.2f} {ptc:>5.2f} '
              f'{ti:>6.0f} {mttr:>5.0f} {mt_yr:>6.1f} {pfd:>10.3e} {ref:>10.3e} {diff:>+7.1f}%')
