import fitz, re, os
from collections import Counter

folder = "C:\\Users\\AntonGustavsson\\OneDrive - ProSa Process Safety Consulting AB\\Desktop\\ClaudeCodeTest\\hazop\\P&ID ref\\Ref från LKAB Demo"

EXT_TAG_RE = re.compile(
    r'((?:(?:\d{1,4}|[A-Z][A-Z0-9]{0,3})[.\-/]){1,3}[A-Z]{2,6}[.\-/]?\d{1,5}[A-Z]{0,3})',
    re.IGNORECASE)

def leading_letters(s):
    m = re.match(r'^([A-Z]+)', s.upper())
    return m.group(1) if m else ''

def extract_instrument_prefix(tag):
    parts = re.split(r'[-./]', tag.upper())
    candidates = [leading_letters(p) for p in parts]
    for ltrs in candidates:
        if len(ltrs) >= 2:
            return ltrs
    return ''

prefix_count = Counter()
prefix_examples = {}

pdfs = sorted([f for f in os.listdir(folder) if f.upper().endswith('.PDF')])[:12]
print(f"Processing {len(pdfs)} PDFs: {pdfs[:5]}...")

for pdf_name in pdfs:
    path = os.path.join(folder, pdf_name)
    try:
        doc = fitz.open(path)
        for page in doc:
            text = page.get_text()
            lines = text.split('\n')
            for i, line in enumerate(lines):
                for m in EXT_TAG_RE.finditer(line):
                    tag = m.group(1).lstrip('=')
                    pfx = extract_instrument_prefix(tag)
                    if pfx and len(pfx) >= 2:
                        prefix_count[pfx] += 1
                        if pfx not in prefix_examples:
                            # grab surrounding context
                            ctx_start = max(0, i-1)
                            ctx_end = min(len(lines), i+2)
                            ctx = ' | '.join(lines[ctx_start:ctx_end]).strip()
                            prefix_examples[pfx] = (tag, ctx[:120])
        doc.close()
    except Exception as e:
        print(f"Error {pdf_name}: {e}")

print("\nPREFIX COUNTS (top 60):")
print(f"{'PREFIX':<10} {'COUNT':>6}  EXAMPLE TAG + CONTEXT")
print("-"*90)
for pfx, cnt in prefix_count.most_common(60):
    ex_tag, ex_ctx = prefix_examples.get(pfx, ('',''))
    print(f"  {pfx:<8} {cnt:>5}  [{ex_tag}]  {ex_ctx[:70]}")

print(f"\nTotal unique prefixes: {len(prefix_count)}")
