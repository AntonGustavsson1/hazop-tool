# -*- coding: utf-8 -*-
"""
io_analysis.py — Extraherar Logic Solver I/O-moduler ur Hybrit-rapporten
och korrelerar med de tre referensnivåerna 6.90e-6 / 7.31e-6 / 9.40e-6.
"""
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import zipfile, xml.etree.ElementTree as ET, unicodedata

def norm(s):
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'[      ]', ' ', s)
    return s.strip()

def parse_all_floats(s):
    s = norm(s).replace(',', '.')
    return [float(x) for x in re.findall(r'[0-9]+\.?[0-9]*[Ee][+-]?[0-9]{1,2}', s)]

def extract_all_italic(path):
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
            if t:
                items.append(('P', t))
        elif tag == 'tbl':
            rows = []
            for tr in child.iter(f'{{{W}}}tr'):
                cells = []
                for tc in tr.iter(f'{{{W}}}tc'):
                    plain_parts, italic_parts = [], []
                    for r in tc.iter(f'{{{W}}}r'):
                        txt = ''.join(x.text for x in r.findall(f'{{{W}}}t') if x.text)
                        if txt:
                            rpr = r.find(f'{{{W}}}rPr')
                            is_italic = (rpr is not None and rpr.find(f'{{{W}}}i') is not None)
                            if is_italic:
                                italic_parts.append(txt)
                            else:
                                plain_parts.append(txt)
                    full = norm(''.join(plain_parts + italic_parts))
                    cells.append({'text': full, 'plain': norm(''.join(plain_parts)),
                                  'italic': norm(''.join(italic_parts))})
                if any(c['text'] for c in cells):
                    rows.append(cells)
            if rows:
                items.append(('T_ITALIC', rows))
    return items

PATH = 'Hybrit Pilot Plant 4-24-2022 SILver Detailed Report.docx'

LOGIC_REF = {
    'SIF-001': 7.31e-6, 'SIF-002': 7.31e-6, 'SIF-003': 6.90e-6,
    'SIF-004': 6.90e-6, 'SIF-005': 6.90e-6, 'SIF-006': 7.31e-6,
    'SIF-007': 7.31e-6, 'SIF-008': 6.90e-6, 'SIF-009': 7.31e-6,
    'SIF-015': 9.40e-6, 'SIF-016': 7.31e-6, 'SIF-017': 7.31e-6,
    'SIF-018': 7.31e-6, 'SIF-020': 9.40e-6, 'SIF-021': 7.31e-6,
    'SIF-022': 9.40e-6, 'SIF-023': 9.40e-6, 'SIF-024': 7.31e-6,
    'SIF-025': 7.31e-6, 'SIF-027': 7.31e-6,
}

items = extract_all_italic(PATH)

SIF_HDR = re.compile(r'^(SIF-[0-9]+)\s+(SIL[0-9]|Low|High|Diff|Temp|Gas|Flode|Tryck|Kond|Bera)')
content_start = next(i for i, (k, d) in enumerate(items) if k == 'P' and d == 'Purpose and Scope')

sif_starts = {}
for i, (kind, data) in enumerate(items[content_start:], content_start):
    if kind == 'P':
        m = SIF_HDR.match(data)
        if m:
            sid = m.group(1)
            if sid not in sif_starts:
                sif_starts[sid] = i

def extract_logic_components(items, start):
    """Extraherar komponentrader ur Logic Solver-sektionen."""
    in_logic = False
    components = []
    SIF_STOP = re.compile(r'^SIF-[0-9]+ (SIL[0-9]|Low|High|Diff|Temp|Gas|Flode|Tryck|Kond|Bera)')

    for kind, data in items[start: start + 600]:
        if kind == 'P':
            if (data != items[start][1] and SIF_STOP.match(data)):
                break
            if 'Logic Solver Part Configuration' in data:
                in_logic = True
            elif 'Final Element Part Configuration' in data:
                break
        elif kind == 'T_ITALIC' and in_logic:
            flat = ' '.join(c['text'] for row in data for c in row)
            if 'DU' in flat and 'Component' in flat:
                for row in data:
                    if not row or not row[0]['text']:
                        continue
                    skip = ['Component', 'SFF', 'Route', 'Reliability', 'Failure',
                            'Logic Solver Model', 'Sensor Leg', 'Final ELement']
                    if any(w in row[0]['text'] for w in skip):
                        continue
                    name = row[0]['text'].strip()
                    if not name:
                        continue
                    du_vals = []
                    for ci, cell in enumerate(row[1:], 1):
                        vals = parse_all_floats(cell['text'])
                        if vals:
                            du_vals.append((ci, vals[-1]))
                    if du_vals and name:
                        components.append((name, du_vals))
    return components

# ── Extrahera och analysera ────────────────────────────────────────────────────
TARGET = ['SIF-001','SIF-003','SIF-008','SIF-015','SIF-020','SIF-022',
          'SIF-007','SIF-016','SIF-021','SIF-023','SIF-027']

print('═'*90)
print('LOGIC SOLVER I/O-MODULER PER SIF')
print('(Korrelerat med referens-PFD-grupp)')
print('═'*90)

group_components = {'6.90e-6': {}, '7.31e-6': {}, '9.40e-6': {}}

for sif_id in TARGET:
    if sif_id not in sif_starts:
        continue
    comps = extract_logic_components(items, sif_starts[sif_id])
    ref = LOGIC_REF.get(sif_id, 0)
    grp = '9.40e-6' if ref > 8e-6 else ('7.31e-6' if ref > 7e-6 else '6.90e-6')

    print(f'\n{sif_id}  [Logic ref={ref:.2e}  Grupp={grp}]')
    for name, vals in comps:
        du_str = '  '.join(f'col{c}={v:.3e}' for c, v in vals)
        # Markera AI/DI/DO/AO-moduler
        tag = ''
        nl = name.lower()
        if any(x in nl for x in ['ai ', 'analog input', 'ai/']):
            tag = '  ← AI'
        elif any(x in nl for x in ['di ', 'digital input', 'di/']):
            tag = '  ← DI'
        elif any(x in nl for x in ['do ', 'digital output', 'do/']):
            tag = '  ← DO'
        elif any(x in nl for x in ['ao ', 'analog output', 'ao/']):
            tag = '  ← AO'
        elif any(x in nl for x in ['pm ', 'cpu', 'processor', 'power']):
            tag = '  ← CPU/PM'
        print(f'  {name:<55} {du_str}{tag}')

    # Lagra för gruppsummering
    for name, vals in comps:
        if name not in group_components[grp]:
            group_components[grp][name] = 0
        group_components[grp][name] += 1

print()
print('═'*90)
print('KOMPONENTER PER GRUPP (antal förekomster)')
print('═'*90)
all_names = set()
for g in group_components.values():
    all_names.update(g.keys())

print(f'\n{"Komponent":<60} {"6.90e-6":>9} {"7.31e-6":>9} {"9.40e-6":>9}')
print('-'*90)
for name in sorted(all_names):
    c1 = group_components['6.90e-6'].get(name, 0)
    c2 = group_components['7.31e-6'].get(name, 0)
    c3 = group_components['9.40e-6'].get(name, 0)
    diff = ' ← SKILJER' if (c1 or c2 or c3) and not all([c1==c2, c2==c3]) else ''
    print(f'{name:<60} {c1:>9} {c2:>9} {c3:>9}{diff}')

print()
print('═'*90)
print('SLUTSATS')
print('═'*90)
