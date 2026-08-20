import fitz, re, os
from collections import Counter, defaultdict

folder = "C:\\Users\\AntonGustavsson\\OneDrive - ProSa Process Safety Consulting AB\\Desktop\\ClaudeCodeTest\\hazop\\P&ID ref\\Ref från LKAB Demo"

# LKAB RDS-PP tag: optional = prefix, then area codes (letter+digit or digit sequences separated by dots),
# then the equipment code (2+ letters + digits)
LKAB_TAG_RE = re.compile(
    r'=?(?:[A-Z]\d+\.)+([A-Z]{2,6})\d+',
    re.IGNORECASE)

prefix_count = Counter()
prefix_all_examples = defaultdict(set)
prefix_nearby = defaultdict(list)

# Equipment keywords to look for near tags
EQUIP_KW = re.compile(
    r'\b(PUMP|HEAT EXCHANGER|EXCHANGER|TANK|VESSEL|REACTOR|FILTER|COMPRESSOR|'
    r'AGITATOR|MIXER|CONVEYOR|FEEDER|BIN|SILO|COLUMN|SEPARATOR|STRIPPER|'
    r'SCRUBBER|CONDENSER|EVAPORATOR|COOLER|HEATER|FAN|BLOWER|DRYER|VALVE|'
    r'MOTOR|CRUSHER|SCREEN|CYCLONE|THICKENER|CLARIFIER|CENTRIFUGE)\b',
    re.IGNORECASE)

pdfs = sorted([f for f in os.listdir(folder) if f.upper().endswith('.PDF')])
print(f"Processing {len(pdfs)} PDFs...")

for pdf_name in pdfs:
    path = os.path.join(folder, pdf_name)
    try:
        doc = fitz.open(path)
        for page in doc:
            text = page.get_text()
            lines = text.split('\n')
            for i, line in enumerate(lines):
                for m in LKAB_TAG_RE.finditer(line):
                    pfx = m.group(1).upper()
                    prefix_count[pfx] += 1
                    prefix_all_examples[pfx].add(m.group(0).upper())
                    # check nearby lines (window of ±3 lines) for equipment keywords
                    window_start = max(0, i-3)
                    window_end = min(len(lines), i+4)
                    window_text = ' '.join(lines[window_start:window_end])
                    kw_matches = EQUIP_KW.findall(window_text)
                    for kw in kw_matches:
                        prefix_nearby[pfx].append(kw.upper())
        doc.close()
    except Exception as e:
        print(f"Error {pdf_name}: {e}")

print(f"\nTotal unique prefixes found: {len(prefix_count)}")
print(f"\n{'PREFIX':<10} {'COUNT':>6}  TOP KEYWORDS NEARBY                      EXAMPLE TAGS")
print("-"*100)
for pfx, cnt in prefix_count.most_common(60):
    kw_counter = Counter(prefix_nearby[pfx])
    top_kw = ', '.join(f"{k}({v})" for k,v in kw_counter.most_common(3))
    examples = ', '.join(sorted(prefix_all_examples[pfx])[:3])
    print(f"  {pfx:<8} {cnt:>5}  {top_kw:<40} {examples}")
