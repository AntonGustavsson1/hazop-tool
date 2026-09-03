"""
Focused PST/EoBT extractor - based on findings from exp_pst_extractor.py.
The .exp file is protobuf-encoded. Key findings:
- "Coverage" appears at offset 97227 (0x17bcb) with text "Proof Test Coverage"
- "PTC" field name and "PTI" (Proof Test Interval) found nearby
- SIF identifiers found. Floats are stored as float64 (double).
- Pattern: c2 3e 12 09 <8 bytes float64> precedes float values
  (c2 3e = field tag, 12 09 = length-delimited 9-byte... actually 12 09 =
   field 2 wire type 2 length 9, but 09 after field tag 12 might be different)
- Looking at protobuf: tag byte c2 3e -> varint encoding of field number,
  then 12 09 <8 bytes> -> this is actually wire type 2 (length-delimited)
  field with 9 bytes... but 09 before 8 bytes could be field 1 wire type 1 (64-bit)

Strategy:
1. Find "PTC" and surrounding context with float64 values
2. Find all protobuf-style c2 3e 12 09 patterns and extract the float64s
3. Search around SIF identifiers for the specific numeric fields
4. Look for the specific pattern that represents PST/EoBT coverage values
"""

import sys
import io
import struct
import re
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

INPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp"
OUTPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIL projekt\exp_pst_focused_findings.txt"

def safe_decode(b):
    return b.decode('latin-1', errors='replace')

def hex_dump(data, offset=0):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"  {offset+i:08x}: {hex_part:<48}  {ascii_part}")
    return '\n'.join(lines)

print(f"Reading {INPUT_FILE}...")
with open(INPUT_FILE, 'rb') as f:
    data = f.read()
print(f"File size: {len(data):,} bytes")

out = open(OUTPUT_FILE, 'w', encoding='utf-8', errors='replace')

def w(s=''):
    out.write(s + '\n')

w("EXP PST/EoBT Focused Extraction Results")
w(f"File: {INPUT_FILE}")
w(f"Size: {len(data):,} bytes")
w('='*80)

# =============================================================================
# 1. Study the "Coverage" string context at 0x17bcb
# =============================================================================
w()
w("="*80)
w("SECTION 1: 'Proof Test Coverage' field context (offset 0x17bcb)")
w("="*80)

cov_offset = 0x17bcb
# Dump 512 bytes before and 512 after
start = max(0, cov_offset - 200)
end = min(len(data), cov_offset + 400)
w(f"Context around 'Coverage' at 0x{cov_offset:x}:")
w(hex_dump(data[start:end], start))
w()
w(f"ASCII: {safe_decode(data[start:end])}")

# =============================================================================
# 2. Find ALL occurrences of "PTC" (Proof Test Coverage field name)
#    and "PTI" (Proof Test Interval) in the file
# =============================================================================
w()
w("="*80)
w("SECTION 2: All 'PTC'/'PTI'/'PST'/'EoB' field name occurrences")
w("="*80)

for term in [b'PTC', b'PTI', b'PST', b'EoB', b'pst', b'eob', b'CovPST', b'CovPTI', b'PTC\x12', b'PTI\x12']:
    pos = 0
    count = 0
    while True:
        idx = data.find(term, pos)
        if idx == -1:
            break
        count += 1
        # Show 100 bytes around it
        s = max(0, idx-40)
        e = min(len(data), idx+len(term)+120)
        w(f"\n  {term!r} at 0x{idx:08x}:")
        w(hex_dump(data[s:e], s))
        w(f"  ASCII: ...{safe_decode(data[s:e])}...")
        pos = idx + 1
        if count > 20:
            w(f"  ... (truncated at 20)")
            break
    w(f"  Total occurrences of {term!r}: {count}")

# =============================================================================
# 3. Study the protobuf pattern c2 3e 12 09 which precedes float64 values
#    Based on: "c2 3e 12 09 <8 bytes>" seen repeatedly in SIF contexts
# =============================================================================
w()
w("="*80)
w("SECTION 3: Pattern c2 3e 12 09 <float64> - all float values in [0.01, 0.999]")
w("="*80)

# Pattern: c2 3e 12 09 then 8 bytes of float64, then 11 bytes, then next float64
pattern = bytes([0xc2, 0x3e, 0x12, 0x09])
pos = 0
found_values = []
while True:
    idx = data.find(pattern, pos)
    if idx == -1:
        break
    float_start = idx + 4
    if float_start + 8 <= len(data):
        v1 = struct.unpack_from('<d', data, float_start)[0]
        # Check if there's also a second float64 at +11 (11 = 8 + 3 bytes of something)
        v2 = None
        if float_start + 8 + 3 + 8 <= len(data):
            try:
                v2 = struct.unpack_from('<d', data, float_start + 11)[0]
            except:
                pass

        found_values.append((idx, v1, v2))

        # Look back for SIF/field name context
        look_back = data[max(0, idx-200):idx]
        context_str = safe_decode(look_back)
        # Find SIF identifier in context
        sif_match = None
        for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
            if sif in context_str:
                pos_in_ctx = context_str.rfind(sif)
                sif_match = sif
                break

        if 0.01 <= v1 <= 0.999 or (v2 is not None and 0.01 <= v2 <= 0.999):
            w(f"\n  At 0x{idx:08x}: pattern c2 3e 12 09 found")
            w(f"    float64 value 1 = {v1:.6f}")
            if v2 is not None:
                w(f"    float64 value 2 = {v2:.6f}")
            if sif_match:
                w(f"    Nearest SIF: {sif_match}")
            w(hex_dump(data[max(0,idx-40):min(len(data),idx+30)], max(0,idx-40)))
    pos = idx + 1

w(f"\nTotal c2 3e 12 09 patterns found: {len(found_values)}")
w(f"Those with coverage-range values: {sum(1 for _,v1,v2 in found_values if 0.01<=v1<=0.999 or (v2 and 0.01<=v2<=0.999))}")

# =============================================================================
# 4. Find SIF entries and extract all associated float64 values
#    Pattern: SIF-xxx ... c2 3e 12 09 <float64> 11 <float64>
# =============================================================================
w()
w("="*80)
w("SECTION 4: Per-SIF float64 value extraction")
w("="*80)

sif_terms = {
    'SIF-011': b'SIF-011',
    'SIF-012': b'SIF-012',
    'SIF-015': b'SIF-015',
    'SIF-019': b'SIF-019',
    'SIF-025': b'SIF-025',
    'SIF-026': b'SIF-026',
}

for sif_name, sif_bytes in sif_terms.items():
    w(f"\n--- {sif_name} ---")
    pos = 0
    sif_occurrences = []
    while True:
        idx = data.find(sif_bytes, pos)
        if idx == -1:
            break
        sif_occurrences.append(idx)
        pos = idx + 1

    w(f"  Found {sif_name} at {len(sif_occurrences)} location(s)")

    for occ_idx, sif_off in enumerate(sif_occurrences):
        # Look for c2 3e 12 09 pattern in next 500 bytes
        search_region = data[sif_off:min(len(data), sif_off+500)]
        w(f"\n  Occurrence {occ_idx+1} at 0x{sif_off:08x}:")

        # Find all float64 values in next 300 bytes
        floats_found = []
        for i in range(min(300, len(search_region)-7)):
            try:
                v = struct.unpack_from('<d', search_region, i)[0]
                if 0.001 <= abs(v) <= 10000 and not (v != v):  # valid, not NaN
                    floats_found.append((i, v))
            except:
                pass

        # Filter to interesting values (likely coverage/rate ranges)
        coverage_floats = [(i, v) for i, v in floats_found if 0.01 <= v <= 0.999]
        rate_floats = [(i, v) for i, v in floats_found if 1e-10 <= v <= 0.01]

        if coverage_floats:
            w(f"    Coverage-range floats (0.01-0.999):")
            for rel_off, val in coverage_floats[:20]:
                abs_off = sif_off + rel_off
                w(f"      0x{abs_off:08x} (+{rel_off}): {val:.6f}")

        # Look specifically for c2 3e 12 09 pattern
        pat = bytes([0xc2, 0x3e, 0x12, 0x09])
        p = 0
        while True:
            pi = search_region.find(pat, p)
            if pi == -1 or pi > 400:
                break
            abs_off = sif_off + pi + 4
            if abs_off + 8 <= len(data):
                v = struct.unpack_from('<d', data, abs_off)[0]
                v2_off = abs_off + 11
                v2 = None
                if v2_off + 8 <= len(data):
                    try:
                        v2 = struct.unpack_from('<d', data, v2_off)[0]
                    except:
                        pass
                v2_str = f"{v2:.8f}" if v2 is not None else 'N/A'
                w(f"    c2 3e 12 09 at +{pi}: val1={v:.8f}, val2={v2_str}")
            p = pi + 1

# =============================================================================
# 5. Exhaustive search for known coverage field names in context
#    exSILentia uses specific XML/field names - search nearby text
# =============================================================================
w()
w("="*80)
w("SECTION 5: exSILentia-specific field names search")
w("="*80)

# exSILentia field names based on known protobuf schema
exsilentia_terms = [
    b'PartialStrokeTest', b'PartialStroke', b'partial_stroke',
    b'EoBT', b'EoB', b'EndOfBatch', b'end_of_batch',
    b'CovPST', b'CovEoBT', b'cov_pst', b'cov_eobt',
    b'pst_coverage', b'eobt_coverage',
    b'PST_cov', b'EoBT_cov',
    b'partialstroke', b'endofbatch',
    b'partial', b'batch',
    b'valve_test', b'ValveTest',
    b'FractionTested', b'fraction_tested',
    b'TestCoverage', b'test_coverage',
    b'beta', b'Beta', b'CCF', b'ccf',
    b'lambda_D', b'lambda_d', b'lambdaD',
    b'MTTF', b'mttf', b'MTTR', b'mttr',
    b'SFF', b'sff',
    b'HFT', b'hft',
    b'DC', b'diagnostic_coverage', b'DiagnosticCoverage',
]

for term in exsilentia_terms:
    pos = 0
    matches = []
    while True:
        idx = data.find(term, pos)
        if idx == -1:
            break
        matches.append(idx)
        pos = idx + 1
    if matches:
        w(f"\n  {term!r}: {len(matches)} match(es)")
        for idx in matches[:5]:
            s = max(0, idx-30)
            e = min(len(data), idx+len(term)+100)
            w(f"    0x{idx:08x}: {safe_decode(data[s:e])}")

# =============================================================================
# 6. Look for the "PST" string in all its forms - UTF-8, UTF-16, in XML tags
# =============================================================================
w()
w("="*80)
w("SECTION 6: Comprehensive 'PST' and 'EoBT' search in all encodings")
w("="*80)

search_variants = {
    'ASCII PST': b'PST',
    'ASCII EoBT': b'EoBT',
    'ASCII EoB ': b'EoB ',
    'ASCII EoB_': b'EoB_',
    'ASCII pst': b'pst',
    'ASCII eobt': b'eobt',
    'UTF16-LE PST': 'PST'.encode('utf-16-le'),
    'UTF16-LE EoBT': 'EoBT'.encode('utf-16-le'),
    'XML <PST': b'<PST',
    'XML PST>': b'PST>',
    'Var PST=': b'PST=',
    'Var pst=': b'pst=',
}

for desc, term in search_variants.items():
    pos = 0
    count = 0
    while True:
        idx = data.find(term, pos)
        if idx == -1:
            break
        count += 1
        if count <= 5:
            s = max(0, idx-40)
            e = min(len(data), idx+len(term)+80)
            w(f"  {desc} at 0x{idx:08x}: {safe_decode(data[s:e])}")
        pos = idx + 1
    if count > 0:
        w(f"  Total {desc}: {count}")

# =============================================================================
# 7. Look at the area around offset 97227 where "Coverage" was found
#    and find the actual data field entries
# =============================================================================
w()
w("="*80)
w("SECTION 7: Deep analysis around 'Proof Test Coverage' field definition")
w("="*80)

# The Coverage match was at 0x17bcb
# Let's search for "PTC" in a 10KB window around it
search_start = max(0, 0x17bcb - 2000)
search_end = min(len(data), 0x17bcb + 10000)
region = data[search_start:search_end]

w(f"Searching {search_end-search_start} bytes around Coverage field at 0x{0x17bcb:x}")
w()
w("Hex dump of region (first 800 bytes):")
w(hex_dump(data[search_start:search_start+800], search_start))

w()
w("All printable ASCII strings of length >= 4 in region:")
# Extract strings
i = 0
while i < len(region):
    # Find start of a string
    if 32 <= region[i] < 127:
        j = i
        while j < len(region) and 32 <= region[j] < 127:
            j += 1
        if j - i >= 4:
            s = region[i:j].decode('ascii', errors='replace')
            abs_off = search_start + i
            w(f"  0x{abs_off:08x}: {s!r}")
        i = j
    else:
        i += 1

# =============================================================================
# 8. Scan for float64 values near SIF identifiers specifically using
#    the "c2 3e 12 09" protobuf pattern that was seen in the SIF contexts
# =============================================================================
w()
w("="*80)
w("SECTION 8: Field-number-based protobuf scan near SIF identifiers")
w("="*80)

# From Section 3, we know the pattern is c2 3e 12 09 <float64> then 11 <float64>
# The "11" byte is actually the field tag for wire type 1 (64-bit) in protobuf
# Let's look at what field numbers the coverage values use

# Near the "Coverage" field at 97227, look at what protobuf field numbers appear
cov_region_start = max(0, 0x17bcb - 100)
cov_region_end = min(len(data), 0x17bcb + 500)

w(f"Protobuf field analysis near Coverage field definition (0x{cov_region_start:x} - 0x{cov_region_end:x}):")
w(hex_dump(data[cov_region_start:cov_region_end], cov_region_start))

# Try to parse protobuf fields in this region
w()
w("Attempting protobuf field parse:")
pos = cov_region_start
while pos < cov_region_end:
    try:
        b = data[pos]
        wire_type = b & 0x07
        field_num = b >> 3
        if wire_type == 0:  # varint
            w(f"  0x{pos:08x}: field {field_num}, wire type 0 (varint)")
            pos += 1
            # read varint
            result = 0
            shift = 0
            while pos < len(data):
                byte = data[pos]
                pos += 1
                result |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            w(f"    value: {result}")
        elif wire_type == 1:  # 64-bit
            w(f"  0x{pos:08x}: field {field_num}, wire type 1 (64-bit)")
            pos += 1
            if pos + 8 <= len(data):
                v = struct.unpack_from('<d', data, pos)[0]
                w(f"    float64: {v:.8f}")
                pos += 8
        elif wire_type == 2:  # length-delimited
            pos += 1
            # read length varint
            length = 0
            shift = 0
            while pos < len(data):
                byte = data[pos]
                pos += 1
                length |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            if length > 0 and length < 10000:
                content = data[pos:pos+min(length, 100)]
                w(f"  0x{pos:08x}: field {field_num}, wire type 2 (length {length}): {safe_decode(content)!r}")
                pos += length
            else:
                w(f"  0x{pos:08x}: field {field_num}, wire type 2, length {length} (too large, stopping parse)")
                break
        elif wire_type == 5:  # 32-bit
            w(f"  0x{pos:08x}: field {field_num}, wire type 5 (32-bit)")
            pos += 1
            if pos + 4 <= len(data):
                v = struct.unpack_from('<f', data, pos)[0]
                w(f"    float32: {v:.8f}")
                pos += 4
        else:
            w(f"  0x{pos:08x}: byte 0x{b:02x} -> wire type {wire_type}, field {field_num} (unexpected)")
            pos += 1
    except Exception as ex:
        w(f"  Parse error at 0x{pos:08x}: {ex}")
        pos += 1

# =============================================================================
# 9. Find "11" byte (field wire type 1) followed by float64 in [0.3, 0.999]
#    specifically in regions that also contain SIF identifiers or "PTC"/"PST"
# =============================================================================
w()
w("="*80)
w("SECTION 9: Wire-type-1 floats near SIF/PTC/PST keywords")
w("="*80)

# Build a set of "interesting regions" - 1000 bytes after each SIF/PTC occurrence
interesting_offsets = set()
for term in [b'SIF-011', b'SIF-012', b'SIF-015', b'SIF-019', b'SIF-025', b'SIF-026',
             b'PTC', b'PTI', b'PST']:
    pos = 0
    while True:
        idx = data.find(term, pos)
        if idx == -1:
            break
        for o in range(idx, min(len(data), idx + 2000)):
            interesting_offsets.add(o)
        pos = idx + 1

w(f"Interesting region bytes: {len(interesting_offsets)}")

# Scan for wire-type-1 (field + 0x09 = field_num << 3 | 1) float64 in [0.3, 0.999]
wire1_floats = []
for off in sorted(interesting_offsets):
    if off + 9 > len(data):
        continue
    b = data[off]
    wire_type = b & 0x07
    field_num = b >> 3
    if wire_type == 1 and 1 <= field_num <= 500:  # wire type 1 = 64-bit
        try:
            v = struct.unpack_from('<d', data, off + 1)[0]
            if 0.01 <= v <= 0.999:
                wire1_floats.append((off, field_num, v))
        except:
            pass

w(f"Wire-type-1 float64 values in [0.01, 0.999] near SIF/PTC: {len(wire1_floats)}")

# Group by field number
by_field = defaultdict(list)
for off, fn, v in wire1_floats:
    by_field[fn].append((off, v))

for fn in sorted(by_field.keys()):
    entries = by_field[fn]
    w(f"\n  Field {fn} ({len(entries)} values):")
    # Find nearby SIF or text
    for off, v in entries[:10]:
        # Look back for context
        look_back = data[max(0, off-150):off]
        ctx = safe_decode(look_back)
        sif_ctx = ''
        for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
            if sif in ctx:
                sif_ctx = f" [near {sif}]"
        w(f"    0x{off:08x}: field {fn} = {v:.6f}{sif_ctx}")

# =============================================================================
# Summary
# =============================================================================
w()
w("="*80)
w("SUMMARY")
w("="*80)
w(f"  File size: {len(data):,} bytes")
w(f"  'Proof Test Coverage' field found at 0x17bcb (97227)")
w(f"  'PTC' field tag precedes coverage value in protobuf")
w(f"  Wire-type-1 float64 values in [0.01,0.999] near SIF/PTC: {len(wire1_floats)}")
w(f"  Unique field numbers with coverage-range values: {list(by_field.keys())}")

out.close()
print("Done. Results written to:", OUTPUT_FILE)
