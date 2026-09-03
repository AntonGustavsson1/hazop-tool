"""
Decode UUID-based protobuf structure to find PST/EoBT coverage values.

KEY UNDERSTANDING:
- File uses protobuf with field 1000 (tag c2 3e) as a typed-value container
- Field 1000 has length-delimited 18-byte payload:
  - Bytes 0-8: [tag 0x09 = field 1 wire1] + [8-byte TYPE UUID]
  - Bytes 9-17: [tag 0x11 = field 2 wire1] + [8-byte VALUE (float64)]
- The TYPE UUID identifies what parameter is stored (PTC, PTI, etc.)
- The glossary area (~0x17000) defines the mapping: UUID -> abbreviation/name

PLAN:
1. Extract ALL c2 3e 12 12 blocks and build UUID -> value mapping
2. Build UUID -> field_name from the glossary (where both UUIDs are known)
3. Look for 'PTC', 'PST', 'EoBT', 'partial_stroke' type UUIDs
"""
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

INPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp"
OUTPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIL projekt\exp_pst_findings.txt"

def safe_decode(b): return b.decode('latin-1', errors='replace')
def hex_dump(d, o=0):
    lines = []
    for i in range(0, len(d), 16):
        c = d[i:i+16]
        h = ' '.join(f'{x:02x}' for x in c)
        a = ''.join(chr(x) if 32<=x<127 else '.' for x in c)
        lines.append(f"  {o+i:08x}: {h:<48}  {a}")
    return '\n'.join(lines)

print(f"Reading {INPUT_FILE}...")
with open(INPUT_FILE, 'rb') as f:
    data = f.read()
print(f"File size: {len(data):,} bytes")

out = open(OUTPUT_FILE, 'w', encoding='utf-8', errors='replace')
def w(s=''): out.write(s+'\n')

w("EXP PST/EoBT UUID Decode")
w(f"File: {INPUT_FILE}")
w('='*80)

# =============================================================================
# STEP 1: Find all "c2 3e 12 12" blocks (field 1000, LD, length 18)
# =============================================================================
w()
w("="*80)
w("STEP 1: All c2 3e 12 12 blocks (UUID+VALUE pairs)")
w("="*80)

pattern = bytes([0xc2, 0x3e, 0x12, 0x12])  # field 1000, LD, length 18
all_uuid_value_pairs = []
pos = 0
while True:
    idx = data.find(pattern, pos)
    if idx == -1: break
    payload_start = idx + 4
    if payload_start + 18 <= len(data):
        payload = data[payload_start:payload_start+18]
        # Expect: 09 <8 bytes UUID> 11 <8 bytes VALUE>
        if payload[0] == 0x09 and payload[9] == 0x11:
            uuid = payload[1:9]
            value_bytes = payload[10:18]
            try:
                value = struct.unpack('<d', value_bytes)[0]
                all_uuid_value_pairs.append((idx, uuid, value))
            except:
                pass
    pos = idx + 1

w(f"Found {len(all_uuid_value_pairs)} c2 3e 12 12 blocks with UUID+VALUE structure")

# =============================================================================
# STEP 2: Also collect "c2 3e 12 09" blocks (UUID only, from glossary)
# =============================================================================
w()
w("="*80)
w("STEP 2: Glossary UUID blocks (c2 3e 12 09 -> type UUIDs)")
w("="*80)

pattern9 = bytes([0xc2, 0x3e, 0x12, 0x09])  # field 1000, LD, length 9
glossary_uuid_blocks = []
pos = 0
while True:
    idx = data.find(pattern9, pos)
    if idx == -1: break
    payload_start = idx + 4
    if payload_start + 9 <= len(data):
        payload = data[payload_start:payload_start+9]
        # For length=9: might be just 09 <8 bytes>
        if payload[0] == 0x09:
            uuid = payload[1:9]
            glossary_uuid_blocks.append((idx, uuid))
        # OR: maybe a 9-byte payload with different structure?
        # Also try: d1 <8 bytes> which was seen earlier
        elif payload[0] == 0xd1:  # field 26, wire type 1
            uuid = payload[1:9]
            glossary_uuid_blocks.append((idx, uuid))
    pos = idx + 1

w(f"Found {len(glossary_uuid_blocks)} c2 3e 12 09 blocks (length-9)")

# =============================================================================
# STEP 3: Map UUIDs to field names by looking at context
# The glossary entries are: ...0a NN <abbrev> 12 NN <name>... c2 3e 12 09 <uuid>
# =============================================================================
w()
w("="*80)
w("STEP 3: UUID to field name mapping from glossary")
w("="*80)

uuid_to_name = {}
for idx, uuid in glossary_uuid_blocks:
    # Look back ~200 bytes for an abbreviation
    look_back = data[max(0, idx-200):idx]
    lb_str = safe_decode(look_back)
    # Find last occurrence of printable ASCII words (likely the abbreviation)
    # The structure before c2 3e is: a2 06 05 0a 03 08 <field_id_varint>
    # Before that: 18 00 a2 06 05...
    # Before that: ...12 NN <description>
    # Before that: 0a NN <abbreviation>

    # Simple heuristic: find last 2-8 char uppercase word in the 80 bytes before
    import re
    words = re.findall(r'[A-Z][A-Z0-9/\-]{1,7}', lb_str[-80:])
    if words:
        abbrev = words[-1]
        # Also find the full description
        desc_match = re.findall(r'[A-Z][a-zA-Z\s,\(\)/&]{10,60}', lb_str[-150:])
        desc = desc_match[-1].strip() if desc_match else ''
        uuid_to_name[uuid.hex()] = (abbrev, desc, idx)

w(f"Found {len(uuid_to_name)} UUID -> name mappings:")
for uuid_hex, (abbrev, desc, idx) in sorted(uuid_to_name.items(), key=lambda x: x[1][0]):
    w(f"  UUID {uuid_hex}: [{abbrev}] {desc}")

# =============================================================================
# STEP 4: For each UUID+VALUE pair, look up the name
# =============================================================================
w()
w("="*80)
w("STEP 4: All typed values with names")
w("="*80)

# Build a compact name lookup
uuid_name_lookup = {}
for uuid_hex, (abbrev, desc, _) in uuid_to_name.items():
    uuid_name_lookup[uuid_hex] = abbrev

# Also try exact match from the glossary c2 3e 12 09 blocks
# The VALUE pair UUID should match the TYPE UUID from glossary

w("UUID -> VALUE pairs with identified names:")
named_values = []
unnamed_values = []
for idx, uuid, value in all_uuid_value_pairs:
    name = uuid_name_lookup.get(uuid.hex(), None)
    if name:
        named_values.append((idx, uuid, value, name))
    else:
        unnamed_values.append((idx, uuid, value))

w(f"Named: {len(named_values)}, Unnamed: {len(unnamed_values)}")

# Show all named values
for idx, uuid, value, name in named_values:
    w(f"  0x{idx:08x}: [{name}] = {value:.8f}")

# Show unnamed values in coverage range [0.01, 0.999]
w()
w("Unnamed values in coverage range [0.01, 0.999]:")
for idx, uuid, value in unnamed_values:
    if 0.01 <= value <= 0.999:
        w(f"  0x{idx:08x}: uuid={uuid.hex()} value={value:.6f}")

# =============================================================================
# STEP 5: Try alternate UUID matching - direct byte search
# The glossary UUID blocks might have different tag bytes than data blocks
# Let me collect all UUIDs seen in data blocks and those in glossary blocks
# and find common UUIDs
# =============================================================================
w()
w("="*80)
w("STEP 5: UUID inventory and cross-matching")
w("="*80)

data_uuids = set(uuid.hex() for _, uuid, _ in all_uuid_value_pairs)
glossary_uuids_set = set(uuid.hex() for _, uuid in glossary_uuid_blocks)

w(f"Unique UUIDs in data blocks: {len(data_uuids)}")
w(f"Unique UUIDs in glossary: {len(glossary_uuids_set)}")
common = data_uuids & glossary_uuids_set
w(f"Common UUIDs: {len(common)}")
w(f"Common UUIDs: {sorted(common)}")

# For each common UUID, find its name from glossary context
# and show associated values
if common:
    w()
    w("Values for common UUIDs:")
    for uuid_hex in sorted(common):
        # Find in glossary
        gname = ''
        for gidx, guuid in glossary_uuid_blocks:
            if guuid.hex() == uuid_hex:
                # Context lookup
                lb = data[max(0,gidx-150):gidx]
                import re
                words = re.findall(r'[A-Z][A-Z0-9/\-]{1,7}', safe_decode(lb[-80:]))
                if words:
                    gname = words[-1]
        # Values
        vals = [(idx, v) for idx, uuid, v in all_uuid_value_pairs if uuid.hex() == uuid_hex]
        w(f"\n  UUID {uuid_hex} [{gname}]: {len(vals)} value(s)")
        for idx, v in vals[:10]:
            w(f"    0x{idx:08x}: {v:.8f}")

# =============================================================================
# STEP 6: Let's approach from a completely different angle.
# Find the UUID for PTC directly from the KNOWN structure.
#
# From the hex: at 0x17bdd we have c2 3e 12 12:
# Let me verify by checking: what's at 0x17bdd exactly?
# =============================================================================
w()
w("="*80)
w("STEP 6: Direct structure decode at known glossary offsets")
w("="*80)

# Known glossary entries (from prior analysis):
# PTC = "Proof Test Coverage" at 0x17bb3
# PTI = "Proof Test Interval" at 0x17bf3

for label, search_text in [("PTC", b"Proof Test Coverage"), ("PTI", b"Proof Test Interval")]:
    idx = data.find(search_text)
    if idx == -1:
        w(f"  {label}: '{search_text}' NOT FOUND")
        continue
    w(f"\n  {label} at 0x{idx:08x}:")
    # After the name, look for c2 3e
    window = data[idx+len(search_text):idx+len(search_text)+40]
    for i in range(len(window)-4):
        if window[i] == 0xc2 and window[i+1] == 0x3e:
            abs_off = idx + len(search_text) + i
            length_byte = window[i+2]
            if length_byte == 0x12:  # length 18
                payload = window[i+3:i+3+18]
                w(f"    c2 3e 12 12 at 0x{abs_off:08x}")
                if len(payload) >= 18:
                    uuid = payload[1:9]  # after 09 tag
                    val_b = payload[10:18]  # after 11 tag
                    try:
                        v = struct.unpack('<d', val_b)[0]
                        w(f"    TYPE UUID: {uuid.hex()}")
                        w(f"    VALUE: {v:.8f}")
                    except:
                        pass
                hex_window = data[abs_off:abs_off+25]
                w(hex_dump(hex_window, abs_off))
            elif length_byte == 0x09:  # length 9
                payload = window[i+3:i+3+9]
                w(f"    c2 3e 12 09 at 0x{abs_off:08x}")
                if len(payload) >= 9:
                    # Try 09 + 8 bytes UUID
                    if payload[0] == 0x09:
                        uuid = payload[1:9]
                        w(f"    TYPE UUID (field1): {uuid.hex()}")
                    else:
                        w(f"    Unknown tag: {payload[0]:02x}")
                        uuid = payload[1:9]
                        w(f"    UUID bytes: {uuid.hex()}")
                hex_window = data[abs_off:abs_off+20]
                w(hex_dump(hex_window, abs_off))
            break

# =============================================================================
# STEP 7: Search for all distinct UUIDs in the data and show values in [0.01, 0.999]
# grouped by UUID (to identify what each UUID represents)
# =============================================================================
w()
w("="*80)
w("STEP 7: All UUIDs with coverage-range values")
w("="*80)

from collections import defaultdict
uuid_to_values = defaultdict(list)
for idx, uuid, value in all_uuid_value_pairs:
    if 0.001 <= value <= 0.9999:
        uuid_to_values[uuid.hex()].append((idx, value))

w(f"UUIDs with values in [0.001, 0.9999]: {len(uuid_to_values)}")
for uuid_hex in sorted(uuid_to_values.keys()):
    vals = uuid_to_values[uuid_hex]
    name = uuid_name_lookup.get(uuid_hex, '?')
    w(f"\n  UUID {uuid_hex} [{name}]: {len(vals)} values")
    for idx, v in vals[:15]:
        # Look for nearby SIF
        lb = data[max(0,idx-300):idx]
        sif_ctx = ''
        for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
            if sif.encode() in lb:
                sif_ctx = f" [near {sif}]"
                break
        w(f"    0x{idx:08x}: {v:.6f}{sif_ctx}")

# =============================================================================
# STEP 8: Full statistics on all UUIDs and their value ranges
# =============================================================================
w()
w("="*80)
w("STEP 8: UUID statistics (all values)")
w("="*80)

uuid_all_values = defaultdict(list)
for idx, uuid, value in all_uuid_value_pairs:
    try:
        if not (value != value) and abs(value) < 1e200:  # not NaN, not inf
            uuid_all_values[uuid.hex()].append(value)
    except:
        pass

w(f"UUIDs with parseable values: {len(uuid_all_values)}")
for uuid_hex in sorted(uuid_all_values.keys()):
    vals = uuid_all_values[uuid_hex]
    name = uuid_name_lookup.get(uuid_hex, '?')
    if vals:
        min_v = min(vals)
        max_v = max(vals)
        avg_v = sum(vals)/len(vals)
        in_range = [v for v in vals if 0.001 <= v <= 0.9999]
        w(f"  UUID {uuid_hex} [{name}]: n={len(vals)}, min={min_v:.4g}, max={max_v:.4g}, avg={avg_v:.4g}, in_range={len(in_range)}")

out.close()
print("Done.")
