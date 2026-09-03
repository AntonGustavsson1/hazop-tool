"""
Final PST/EoBT extractor for exSILentia .exp (protobuf) files.

KEY FINDINGS from prior analysis:
- File is protobuf encoded
- Pattern near "PTC" (Proof Test Coverage) and "PTI" (Proof Test Interval):
  c2 3e 12 09 <1 byte: 11> <8 bytes float64>
  - c2 3e = varint for field_tag (field 0x7e=126, wire type 2 = length-delimited)
  - 12 09 = nested message length = 9 bytes
  - 11 = byte 0x11 = field 2, wire type 1 (64-bit) = float64
  - 8 bytes = the actual double value

- The "PTC" field name is at offset 0x17bbb and the float after "c2 3e 12 09"
  contains the actual value.

- SIF-011, SIF-012, SIF-015, SIF-019, SIF-025, SIF-026 all found
- "PST" and "EoBT" literal strings NOT found in the file
- The file contains a "glossary/enum" area around 0x17000-0x18000 with
  all the field names/abbreviations.

New strategy:
1. Decode the protobuf pattern c2 3e 12 09 11 <8 bytes> properly - the
   float64 IS the value. But they showed as huge numbers, meaning the
   byte "11" is NOT the protobuf tag but rather the data bytes include it.

   Wait - let me re-examine. The c2 3e 12 09 pattern occurs 23,991 times.
   After it comes 9 bytes. The structure:
   c2 3e = field 0x7c/0x7e... let me decode:
   c2 = 0xc2 = 1100 0010. In protobuf varint: bit 7 = 1, so continues.
   lower 7 bits = 100 0010 = 0x42
   next byte: 3e = 0x3e = 0011 1110, bit 7 = 0, so final byte.
   lower 7 bits = 011 1110 = 0x3e
   Combined: (0x3e << 7) | 0x42 = 0x1f42 = 8002 (decimal)
   field_number = 8002 >> 3 = 1000, wire type = 8002 & 7 = 2 (length-delimited)
   So: field 1000, wire type 2 (length-delimited), length 9

   Then the 9 bytes:
   byte 0: 11 = field 2, wire type 1 (64-bit) = start of a float64 field
   bytes 1-8: the 8 bytes of the float64

   So actually c2 3e 12 09 11 is the full preamble, and bytes 6-13 are the float64!

Let me verify: at offset 0x17bd3 after "PTC...Coverage":
  18 00 a2 06 05 0a 03 08 93 0c c2 3e 12 09 d1 e0 c9 70 5f 10 0d 48
  ^18 00 = field 3 (varint), value 0
  ^a2 06 = varint for field 0x14c/2 = actually field 0x14c=332? No...

Actually let me just look for: c2 3e 12 09 11 <8 bytes> and interpret those 8 bytes.
"""

import sys
import io
import struct
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

INPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp"
OUTPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIL projekt\exp_pst_findings.txt"

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

w("EXP PST/EoBT Final Extraction Results")
w(f"File: {INPUT_FILE}")
w(f"Size: {len(data):,} bytes")
w('='*80)

# =============================================================================
# CRITICAL ANALYSIS: What is the correct structure?
# Pattern found: c2 3e 12 09 d1 e0 c9 70 5f 10 0d 48 11 8d 75 52 43 4d
# At offset after "PTC" (Proof Test Coverage)
# =============================================================================
w()
w("="*80)
w("ANALYSIS 1: Decoding the protobuf structure near PTC/PTI")
w("="*80)

# Verified offsets from prior analysis:
# PTC field at 0x17bbb: "PTC" -> "Proof Test Coverage"
# After "Coverage\x18\x00": c2 3e 12 09 d1 e0 c9 70 5f 10 0d 48 11 8d 75 52 43 4d
# PTI field at 0x17bfb: "PTI" -> "Proof Test Interval"
# After "Interval\x18\x00": c2 3e 12 09 b3 f6 65 68 96 62 36 46 11 ba 35 23 4f bc 99 87 28

# The 9 bytes after c2 3e 12 09 for PTC: d1 e0 c9 70 5f 10 0d 48
# As float64 LE:
ptc_bytes = bytes([0xd1, 0xe0, 0xc9, 0x70, 0x5f, 0x10, 0x0d, 0x48])
ptc_val = struct.unpack('<d', ptc_bytes)[0]
w(f"PTC float64 bytes: {ptc_bytes.hex()} -> {ptc_val:.6f}")

# The 9 bytes after c2 3e 12 09 for PTI: b3 f6 65 68 96 62 36 46
pti_bytes = bytes([0xb3, 0xf6, 0x65, 0x68, 0x96, 0x62, 0x36, 0x46])
pti_val = struct.unpack('<d', pti_bytes)[0]
w(f"PTI float64 bytes: {pti_bytes.hex()} -> {pti_val:.6f}")

# These are type UUIDs/hashes, not actual coverage values. They are FIELD DEFINITIONS.
# The glossary area defines field types and their IDs.
# When a SIF element stores PTC=0.85, it uses field_id=0x930c (from "08 93 0c")
# with a float64 value.

# From the protobuf field parse at 0x17b9f:
# "field 20, wire type 2 (length 6): '\x05\n\x03\x08\x92\x0c'"
# This means the field number for PLC is 0x0c92 = 3218
# For PTC: field number = 0x0c93 = 3219
# For PTI: field number = 0x0c94 = 3220

# The field tag for PTC in protobuf would be: field_number=3219, wire_type=1 (64-bit)
# Tag = (3219 << 3) | 1 = 25753 = 0x6499... as varint: ?

# Actually let me look at the inline data pattern.
# The structure 0a 03 08 93 0c is:
#   0a = field 1, wire type 2 (length-delimited)
#   03 = length 3
#   08 93 0c = field 1 wire type 0, value (varint) = 0x0c93 = 3219
# So field id 3219 = PTC

# When the SIF stores PTC value, the protobuf will have field 3219, wire type 1:
# Tag = (3219 << 3) | 1 = 25753 in decimal
# As protobuf varint: 25753 = 0x6499
# 0x6499 in varint: 0x99, 0x64 ? No, varint encoding:
# 25753 in binary: 110001010011001
# Split into 7-bit groups from LSB: 0011001 (25) = 0x19, then 1100010 = 0x62...
# Actually: 25753 = 0110010010011001 - no
# 25753 decimal:
# 25753 / 128 = 201 remainder 25 -> low byte: 25 | 0x80 = 0x99
# 201 / 128 = 1 remainder 73 -> next byte: 73 | 0x80 = 0xC9
# 1 / 128 = 0 remainder 1 -> high byte: 1
# So varint: 0x99, 0xC9, 0x01 = [0x99, 0xC9, 0x01]

# Let me verify: field 3219, wire type 1:
field_for_ptc = 3219
wire_type = 1  # 64-bit
tag = (field_for_ptc << 3) | wire_type
w(f"\nPTC field number: {field_for_ptc}")
w(f"PTC protobuf tag (field<<3|wire): {tag} = 0x{tag:x}")
# Encode as varint
tag_bytes = []
v = tag
while v > 127:
    tag_bytes.append((v & 0x7f) | 0x80)
    v >>= 7
tag_bytes.append(v)
w(f"PTC tag as varint bytes: {bytes(tag_bytes).hex()} = {tag_bytes}")

# Search for this tag in the file
tag_pattern = bytes(tag_bytes)
w(f"\nSearching for PTC field tag pattern {tag_pattern.hex()}...")
pos = 0
ptc_occurrences = []
while True:
    idx = data.find(tag_pattern, pos)
    if idx == -1:
        break
    ptc_occurrences.append(idx)
    pos = idx + 1

w(f"Found {len(ptc_occurrences)} occurrences")
for occ in ptc_occurrences[:30]:
    # After tag (3 bytes), read 8 bytes float64
    val_off = occ + len(tag_bytes)
    if val_off + 8 <= len(data):
        val = struct.unpack_from('<d', data, val_off)[0]
        # Find nearby SIF
        look_back = data[max(0, occ-200):occ]
        sif_ctx = ''
        for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
            if sif.encode() in look_back or sif.encode() in data[occ:min(len(data),occ+200)]:
                sif_ctx = f" [{sif}]"
        w(f"  0x{occ:08x}: PTC = {val:.6f}{sif_ctx}")
        w(hex_dump(data[max(0,occ-30):min(len(data),occ+len(tag_bytes)+20)], max(0,occ-30)))

# Do the same for PTI
field_for_pti = 3220
tag_pti = (field_for_pti << 3) | 1
tag_pti_bytes = []
v = tag_pti
while v > 127:
    tag_pti_bytes.append((v & 0x7f) | 0x80)
    v >>= 7
tag_pti_bytes.append(v)
tag_pti_pattern = bytes(tag_pti_bytes)

w(f"\nPTI field number: {field_for_pti}, tag: {tag_pti} = {tag_pti_pattern.hex()}")
pos = 0
pti_count = 0
while True:
    idx = data.find(tag_pti_pattern, pos)
    if idx == -1:
        break
    pti_count += 1
    val_off = idx + len(tag_pti_bytes)
    if val_off + 8 <= len(data):
        val = struct.unpack_from('<d', data, val_off)[0]
        look_back = data[max(0, idx-200):idx]
        sif_ctx = ''
        for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
            if sif.encode() in look_back or sif.encode() in data[idx:min(len(data),idx+200)]:
                sif_ctx = f" [{sif}]"
        if pti_count <= 30:
            w(f"  0x{idx:08x}: PTI = {val:.2f}{sif_ctx}")
    pos = idx + 1
w(f"Found {pti_count} PTI occurrences")

# =============================================================================
# ANALYSIS 2: Look for ALL field definitions in the glossary area and
# find PST/EoBT field numbers
# =============================================================================
w()
w("="*80)
w("ANALYSIS 2: Complete glossary scan for ALL field abbreviations and IDs")
w("="*80)

# The glossary pattern is:
# 0a NN <NN bytes: field_abbrev> 12 NN <NN bytes: field_name> 18 00 a2 06 05 0a 03 <varint field_id>
# Let's scan the whole glossary area

# The glossary seems to be between ~0x0000 and ~0x20000 based on where PTC/PTI are
# Let's find ALL short uppercase codes and their field numbers

# Pattern: 0a NN (where NN <= 8 for short codes) then ASCII uppercase letters
# After the name: 18 00 a2 06 05 0a 03 <varint>
# Then: c2 3e 12 09 <8 bytes (type UUID)> 11 <8 bytes>

# More robust: search for all "0a NN PTC/PST/EOB/EoB/..." type abbreviations

w("Looking for 2-10 char uppercase codes in glossary area...")
glossary_entries = {}

# Scan for length-prefixed strings that look like field abbreviations
i = 0
end_scan = min(len(data), 0x200000)  # scan first 2MB
while i < end_scan - 50:
    # Pattern: 0a NN <NN ASCII chars> 12 <length> <ASCII name> 18 00
    if data[i] == 0x0a:
        length = data[i+1]
        if 2 <= length <= 12:
            # Check if the bytes are ASCII uppercase/lowercase letters/digits/underscore
            abbrev_bytes = data[i+2:i+2+length]
            if all(32 <= b < 127 for b in abbrev_bytes):
                abbrev = abbrev_bytes.decode('ascii', errors='replace')
                # Look for the description after it
                j = i + 2 + length
                if j + 2 < len(data) and data[j] == 0x12:
                    desc_len = data[j+1]
                    if 2 <= desc_len <= 100 and j + 2 + desc_len < len(data):
                        desc_bytes = data[j+2:j+2+desc_len]
                        if all(32 <= b < 127 for b in desc_bytes):
                            desc = desc_bytes.decode('ascii', errors='replace')
                            # Look for 18 00 a2 06 after this
                            k = j + 2 + desc_len
                            if k + 8 < len(data) and data[k] == 0x18 and data[k+1] == 0x00:
                                # Found a valid glossary entry! Now get the field number
                                # Skip 18 00 a2 06 05 0a 03 (or 0a 04 etc)
                                m = k + 2
                                # look for 0a NN 08 <varint> pattern within next 20 bytes
                                for x in range(m, min(len(data), m+20)):
                                    if data[x] == 0x0a and x+2 < len(data):
                                        inner_len = data[x+1]
                                        if inner_len <= 5 and x + 2 + inner_len < len(data):
                                            inner = data[x+2:x+2+inner_len]
                                            if inner[0] == 0x08:  # varint field
                                                # decode varint
                                                field_id = 0
                                                shift = 0
                                                for b in inner[1:]:
                                                    field_id |= (b & 0x7f) << shift
                                                    shift += 7
                                                    if not (b & 0x80):
                                                        break
                                                glossary_entries[field_id] = (abbrev, desc)
                                                break
    i += 1

w(f"Found {len(glossary_entries)} glossary entries")
w()
w("All entries (abbrev -> description, field_id):")
for fid in sorted(glossary_entries.keys()):
    abbrev, desc = glossary_entries[fid]
    w(f"  field {fid} (0x{fid:04x}): [{abbrev}] = {desc}")

# =============================================================================
# ANALYSIS 3: Search for ANY field that contains "pst", "partial", "eobt", "batch"
# (case-insensitive) in the glossary
# =============================================================================
w()
w("="*80)
w("ANALYSIS 3: PST/EoBT related fields from glossary")
w("="*80)

pst_related = {}
eobt_related = {}
all_interesting = {}
for fid, (abbrev, desc) in glossary_entries.items():
    a_lower = abbrev.lower()
    d_lower = desc.lower()
    if any(kw in a_lower or kw in d_lower for kw in ['pst', 'partial', 'stroke', 'eobt', 'eob', 'batch', 'ptc', 'proof', 'coverage', 'diagnostic', 'dc ']):
        all_interesting[fid] = (abbrev, desc)
        w(f"  INTERESTING: field {fid} [{abbrev}] = {desc}")

# =============================================================================
# ANALYSIS 4: Even if PST/EoBT not in glossary explicitly, try all field IDs
# near PTC (3219) and search for their values in the data
# =============================================================================
w()
w("="*80)
w("ANALYSIS 4: Search for field IDs near PTC (3219) field number range")
w("="*80)

# PTC = 3219, PTI = 3220. Other coverage-related fields might be in same range
# Let's search for field IDs from 3200 to 3300
w("Searching for float64 values from field range 3200-3300...")

interesting_fields = {}
for field_num in range(3200, 3400):
    tag = (field_num << 3) | 1  # wire type 1 = 64-bit
    tag_bytes_list = []
    v = tag
    while v > 127:
        tag_bytes_list.append((v & 0x7f) | 0x80)
        v >>= 7
    tag_bytes_list.append(v)
    tag_pat = bytes(tag_bytes_list)

    pos = 0
    vals = []
    while True:
        idx = data.find(tag_pat, pos)
        if idx == -1:
            break
        val_off = idx + len(tag_pat)
        if val_off + 8 <= len(data):
            try:
                val = struct.unpack_from('<d', data, val_off)[0]
                if 0.001 <= val <= 1.0 and not (val != val):
                    vals.append((idx, val))
            except:
                pass
        pos = idx + 1

    if vals:
        abbrev = glossary_entries.get(field_num, ('?', '?'))[0]
        desc = glossary_entries.get(field_num, ('?', '?'))[1]
        interesting_fields[field_num] = (abbrev, desc, vals)

w(f"Fields in range 3200-3400 with float64 values in [0.001, 1.0]: {len(interesting_fields)}")
for fnum in sorted(interesting_fields.keys()):
    abbrev, desc, vals = interesting_fields[fnum]
    w(f"\n  Field {fnum} [{abbrev}] = {desc}: {len(vals)} value(s)")
    for idx, val in vals[:20]:
        # Find nearby SIF
        look_back = data[max(0, idx-300):idx]
        sif_ctx = ''
        for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
            if sif.encode() in look_back:
                sif_ctx = f" [{sif}]"
                break
        w(f"    0x{idx:08x}: {val:.6f}{sif_ctx}")

# =============================================================================
# ANALYSIS 5: Brute-force: find all field_IDs that store values in [0.3, 1.0]
# where the field_ID is in the glossary, and report the top ones
# =============================================================================
w()
w("="*80)
w("ANALYSIS 5: All glossary fields with values in [0.3, 1.0] stored as float64")
w("="*80)

for fid, (abbrev, desc) in sorted(glossary_entries.items()):
    tag = (fid << 3) | 1
    tag_bytes_list = []
    v = tag
    while v > 127:
        tag_bytes_list.append((v & 0x7f) | 0x80)
        v >>= 7
    tag_bytes_list.append(v)
    tag_pat = bytes(tag_bytes_list)

    pos = 0
    vals = []
    while True:
        idx = data.find(tag_pat, pos)
        if idx == -1:
            break
        val_off = idx + len(tag_pat)
        if val_off + 8 <= len(data):
            try:
                val = struct.unpack_from('<d', data, val_off)[0]
                if 0.3 <= val <= 1.0 and not (val != val):
                    vals.append((idx, val))
            except:
                pass
        pos = idx + 1

    if vals:
        w(f"\n  [{abbrev}] ({desc}) field {fid}: {len(vals)} value(s)")
        for idx, val in vals[:10]:
            look_back = data[max(0, idx-300):idx]
            sif_ctx = ''
            for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
                if sif.encode() in look_back:
                    sif_ctx = f" [near {sif}]"
                    break
            w(f"    0x{idx:08x}: {val:.6f}{sif_ctx}")

out.close()
print("Done. Results in:", OUTPUT_FILE)
