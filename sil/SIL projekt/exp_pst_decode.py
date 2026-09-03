"""
Decode the exSILentia .exp protobuf structure to find PST/EoBT coverage values.

The glossary area shows: field 1555 = PTC, field 1556 = PTI.
The inline structure "0a 03 08 93 0c" at the PTC entry decodes as:
  0a = field 1, wire type 2 (length-delimited)
  03 = length 3
  08 93 0c = field 1 varint: decode 0x93, 0x0c = (0x93 & 0x7f) | (0x0c << 7) = 0x13 | 0x600 = 0x613 = 1555

Wait, that gives 1555 directly... but 0x93 = 1001 0011 -> lower 7 bits = 001 0011 = 0x13
0x0c = 0000 1100 (no more bytes since bit 7 = 0)
Value = 0x13 | (0x0c << 7) = 0x13 + (0x0c * 128) = 19 + 1536 = 1555. YES, confirmed.

So the glossary uses field_id = 1555 as the identifier value FOR the PTC entry.
But how does the actual data reference this? In the SIF data blocks, when a PTC value
is stored, what field number is used in the protobuf encoding?

Let me look at the protobuf structure of an actual SIF block.
From SIF-011 at 0x307db2, the structure shows:
"SIF-011 Open 4 +ABB AC800M High Integrity SIL2 [DigitalOut]...
Â>   Õ'3ûB· HH;õÊ>..."
The Â> = c2 3e pattern appears after the device name.
Then: 12 09 d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15

c2 3e = (0xc2 & 0x7f) | (0x3e << 7) = 0x42 | 0x1f00 = 0x1f42 = 8002
8002 >> 3 = 1000, 8002 & 7 = 2 (length-delimited)
So field 1000, wire type 2, length = 0x09 = 9 bytes

The 9 bytes: d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15
Wait, only 9 bytes: d5 27 95 33 14 02 fb 42 11
As float64: d5 27 95 33 14 02 fb 42 -> struct.unpack('<d', b'\xd5\x27\x95\x33\x14\x02\xfb\x42')
Let me calculate: this is the "type UUID" from the glossary.

THEN: 11 b7 a0 48 48 06 3b f5 15
11 = next protobuf field tag inside the nested message? No, 11 = 0x11 = field 2, wire type 1 (64-bit)
Then 8 bytes: b7 a0 48 48 06 3b f5 15 = the actual float64 value!

But wait - "c2 3e 12 09" = outer: field 1000, LD, length 9
The 9 bytes ARE: "11 <8 bytes>" where 11 = inner field 2 wire type 1, and the 8 bytes are the float64.

So the actual value is:
bytes = b7 a0 48 48 06 3b f5 15 but wait the 9-byte message starts at offset+4 (after c2 3e 12 09):
Byte 0: 11 (inner tag: field 2, wire type 1)
Bytes 1-8: the 8 float64 bytes

So the float64 bytes for SIF-011 Open after the first c2 3e 12 09 are:
At offset 0x307db2 + 68 = 0x307dfa, the c2 3e starts, then:
+0: c2 3e 12 09 = outer tag
+4: 11 = inner tag (field 2, wire type 1)
+5 to +12: 8 bytes float64

From the hex at 0x307db2 + 68 = 0x307dfa:
c2 3e 12 09 d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15

After c2 3e 12 09: d5 27 95 33 14 02 fb 42 (8 bytes, NOT the value - this is 9 bytes including "11")
Wait: the length is 9, so 9 bytes = d5 27 95 33 14 02 fb 42 11
These 9 bytes form a nested message:
  - Byte 0: 11 -> hmm but that's the last byte. Let me re-count.

At 0x307df4: c2 3e 12 09 d5 27 95 33 14 02 fb 42
                ^offset    ^4 bytes after = 9 bytes payload
Payload: d5 27 95 33 14 02 fb 42 [9th byte = ??]

Actually let me just RE-READ the raw bytes from the original hex dump.
From Occurrence 4, at 0x307db2, c2 3e at offset +68:
Offset in file = 0x307db2 + 68 = 0x307dfa

From hex dump at 0x307db2:
0: 53 49 46 2d 30 31 31 20 4f 70 65 6e 12 00 12 34   SIF-011 Open...4
16: 0a 2b 41 42 42 20 41 43 38 30 30 4d 20 48 69 67   .+ABB AC800M Hig
32: 68 20 49 6e 74 65 67 72 69 74 79 20 53 49 4c 32   h Integrity SIL2
48: 20 5b 44 69 67 69 74 61 6c 4f 75 74 5d 12 05 08    [DigitalOut]...
64: 84 07 18 02 c2 3e 12 09 d5 27 95 33 14 02 fb 42   .....>...'.3...B
80: 11 b7 a0 48 48 06 3b f5 15 1a 1b ca 3e 04 0a 00   ...HH.;.....>...

So at offset 64 from the start of the dump (absolute: 0x307db2 + 64 = 0x307df2):
84 07 18 02 c2 3e 12 09 d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15

c2 3e is at offset +68 from dump start = absolute 0x307df6
After c2 3e 12 09 (4 bytes), the 9 payload bytes are:
d5 27 95 33 14 02 fb 42 11

As a nested protobuf:
  0x11 at position 0 of payload... wait that's the 9th byte.
  Actually bytes 0-7: d5 27 95 33 14 02 fb 42
  Byte 8: 11

So the 9-byte message starts with d5 (which is a varint tag for something)
d5 = 1101 0101 -> bit 7 = 1, lower 7 = 101 0101 = 0x55
Next byte: 27 = 0010 0111 -> bit 7 = 0, lower 7 = 010 0111 = 0x27
Value = 0x55 | (0x27 << 7) = 0x55 + 0x1380 = 0x13d5 = 5077
5077 >> 3 = 634, 5077 & 7 = 5 (wire type 5 = 32-bit fixed)
So field 634, wire type 5, then 4 bytes: 95 33 14 02
As float32: struct.unpack('<f', b'\x95\x33\x14\x02') = ?

Hmm this doesn't look right either. The inner structure must have:
11 = wire type 1, field 2
Then 8 bytes float64

But 11 is the 9th byte. Let me try: maybe the 9-byte payload is:
11 b7 a0 48 48 06 3b f5 15  (where 11 is the FIRST byte of the payload, not the LAST)

Wait - I need to re-read. The c2 3e 12 09 appears at absolute offset 0x307df6.
After it: 9 bytes.
From hex dump: "c2 3e 12 09 d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15"
The 9 bytes after "c2 3e 12 09" are: d5 27 95 33 14 02 fb 42 11

Hmm! So:
  d5 27 95 33 14 02 fb 42 = 8 bytes
  11 = 1 more byte = 9 total

As the inner 9-byte message:
  First field tag: d5 -> varint continues...
  OR: the format is not standard protobuf for this inner message.

Actually I think the "c2 3e 12 09" pattern might be:
c2 3e = protobuf tag for the OUTER field (some high field number)
12 = nested message type marker... but 12 IS field 2 wire type 2 normally.
09 = length 9

And the 9 bytes: first byte is d5 which starts a varint, next 27...
d5 | (27 << 7) = ... nope that's getting complex.

Let me try a completely different approach: the 9-byte block IS:
- A UUID in Google's well-known-type format or
- Just: length 1 varint_tag + 8 bytes float64

If the inner tag is at the END:
Bytes 0-7: float64
Byte 8: inner_tag = 11

OR the outer structure is:
c2 3e = tag for field 1000, wire type 2
12 = length-delimited sub-field tag (field 2, wire type 2)?
09 = length 9
Then 9 bytes of UUID or something.
THEN 11 = field 2, wire type 1 (float64 tag for a separate field)
THEN 8 bytes float64

This makes more sense! The "c2 3e 12 09 <9bytes>" and "11 <8bytes>" are two
SEPARATE protobuf fields. So:
- c2 3e 12 09 <9 bytes> = field 1000, LD, 9-byte UUID/type-indicator
- 11 <8 bytes> = field 2, wire type 1, 8-byte float64 VALUE

Let me search for this: after the 9-byte payload, look at byte 13 (offset from c2):
At 0x307df6: c2 3e 12 09 d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15
+0: c2 3e = outer tag
+2: 12 09 = outer length-delimited, length 9
+4..+12: d5 27 95 33 14 02 fb 42 11 = 9-byte UUID payload
+13: b7 = ??? No that's inside the UUID

WAIT. Let me count again from the dump:
"c2 3e 12 09 d5 27 95 33 14 02 fb 42 11 b7 a0 48 48 06 3b f5 15"
Position: 0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20

c2 3e -> position 0,1
12 09 -> position 2,3 (field 2 wire 2, length 9)
d5 27 95 33 14 02 fb 42 11 -> positions 4..12 (the 9-byte payload)
b7 a0 48 48 06 3b f5 15 -> positions 13..20 (these are the NEXT bytes)

Ah - I bet the OUTER field tag is NOT c2 3e but something else, and the pattern breaks down differently.

Let me just decode properly:
At 0x307df2: 84 07 18 02
84 07 = varint: (0x84 & 0x7f) | (0x07 << 7) = 4 | 0x380 = 0x384 = 900
Wait: 0x84 = 1000 0100, bit 7 set, lower 7 = 000 0100 = 4
0x07 = 0000 0111, bit 7 clear, lower 7 = 000 0111 = 7
Value = 4 | (7 << 7) = 4 + 896 = 900
900 >> 3 = 112, 900 & 7 = 4 (wire type 4)
Wire type 4 = END_GROUP. That's a group end marker.

Then 18 02:
18 = field 3, wire type 0 (varint)
02 = value 2

Then c2 3e 12 09 ...:
Already analyzed: field 1000, wire type 2, length 9, then 9 bytes UUID

Then 11:
0x11 = field 2, wire type 1 (64-bit)
Then 8 bytes: b7 a0 48 48 06 3b f5 15

Let me compute this float64:
"""
import sys, io, struct

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

INPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp"
OUTPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIL projekt\exp_pst_findings.txt"

print(f"Reading {INPUT_FILE}...")
with open(INPUT_FILE, 'rb') as f:
    data = f.read()
print(f"File size: {len(data):,} bytes")

out = open(OUTPUT_FILE, 'w', encoding='utf-8', errors='replace')
def w(s=''): out.write(s + '\n')

def safe_decode(b): return b.decode('latin-1', errors='replace')

def hex_dump(data, offset=0):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"  {offset+i:08x}: {hex_part:<48}  {ascii_part}")
    return '\n'.join(lines)

w("EXP PST/EoBT Structure Decode")
w(f"File: {INPUT_FILE}")
w('='*80)

# Verify the c2 3e 12 09 ... 11 ... structure
w()
w("="*80)
w("PART 1: Verify c2 3e 12 09 ... 11 <float64> structure")
w("="*80)

# From SIF-011 at 0x307db2 + 68 = 0x307df6
test_offset = 0x307df6
w(f"\nTest at 0x{test_offset:08x}:")
w(hex_dump(data[test_offset:test_offset+30], test_offset))

# After c2 3e 12 09 (4 bytes): 9 bytes UUID
uuid_bytes = data[test_offset+4:test_offset+13]
w(f"9-byte UUID: {uuid_bytes.hex()}")

# After UUID (position 13): should be 11 then 8 bytes float64
next_tag = data[test_offset+13]
w(f"Next byte after UUID: 0x{next_tag:02x} (field {next_tag>>3}, wire type {next_tag&7})")

if next_tag == 0x11:
    float_bytes = data[test_offset+14:test_offset+22]
    val = struct.unpack('<d', float_bytes)[0]
    w(f"Float64 after 0x11: {float_bytes.hex()} = {val:.8f}")
else:
    w("Expected 0x11 but got something else")
    # Try to find 0x11 in next few bytes
    for offset_try in range(1, 5):
        b = data[test_offset+13+offset_try]
        if b == 0x11:
            float_bytes = data[test_offset+14+offset_try:test_offset+22+offset_try]
            val = struct.unpack('<d', float_bytes)[0]
            w(f"  Found 0x11 at +{13+offset_try}, value = {val:.8f}")

# =============================================================================
# PART 2: The UUID identifies what TYPE of value follows
# Search the glossary area for the UUID of PTC to understand what UUID = PTC
# =============================================================================
w()
w("="*80)
w("PART 2: Map UUID -> field name from glossary area")
w("="*80)

# From the hex dump at offset ~0x17bb3 (PTC entry):
# 0a 03 50 54 43 = field1 LD, len 3, "PTC"
# 12 13 50 72 6f 6f 66... = field2 LD, len 19, "Proof Test Coverage"
# 18 00 = field3 varint, value 0
# a2 06 05 0a 03 08 93 0c = field20 (a2 06 = varint for field 20? let's check)
# a2 = 1010 0010, bit7=1, lower7 = 010 0010 = 0x22 = 34
# 06 = 0000 0110, bit7=0, lower7 = 000 0110 = 6
# value = 34 | (6 << 7) = 34 + 768 = 802
# 802 >> 3 = 100, 802 & 7 = 2 (wire type 2, LD)
# So field 100, wire type 2, next byte = 05 = length 5
# Content: 0a 03 08 93 0c
#   0a = field1 wire2 LD, len 3: 08 93 0c
#   08 93 0c = field1 varint: 0x93 = bit7+lower = 0x13, 0x0c = 12
#   value = 0x13 | (0x0c << 7) = 19 + 1536 = 1555 <- this is the field_id 1555 for PTC

# Then: c2 3e 12 09 d1 e0 c9 70 5f 10 0d 48
# Field 1000 (c2 3e = varint 8002 -> field 1000 wire2), len 9
# 9-byte UUID: d1 e0 c9 70 5f 10 0d 48 11 <- wait, that's only 8+1=9
# The 9 bytes: d1 e0 c9 70 5f 10 0d 48 11
# Hmm: d1 e0 c9 70 5f 10 0d 48 = 8 bytes as float64?
ptc_uuid_9 = bytes([0xd1, 0xe0, 0xc9, 0x70, 0x5f, 0x10, 0x0d, 0x48, 0x11])
w(f"PTC 9-byte payload: {ptc_uuid_9.hex()}")
# First 8 bytes as float64:
ptc_float1 = struct.unpack('<d', ptc_uuid_9[:8])[0]
w(f"First 8 bytes as float64: {ptc_float1}")

# Then following: 8d 75 52 43 4d a2 56 89
# = 8 bytes. As float64:
ptc_offset_at_glossary = 0x17bd3  # after "Coverage\x18\x00"
w(f"\nAt glossary offset 0x{ptc_offset_at_glossary:08x}:")
w(hex_dump(data[ptc_offset_at_glossary:ptc_offset_at_glossary+30], ptc_offset_at_glossary))

# The structure around it:
# a2 06 05 0a 03 08 93 0c c2 3e 12 09 d1 e0 c9 70 5f 10 0d 48 11 8d 75 52 43 4d a2 56
# a2 06 05 0a 03 08 93 0c = field 100, LD, len 5, content: field1 varint 1555
# c2 3e 12 09 = field 1000, LD, len 9
# d1 e0 c9 70 5f 10 0d 48 11 = 9-byte UUID
# 8d 75 52 43 4d = ??? these follow the UUID block

# 8d 75 52 43 4d:
# 8d = field 17 (0x8d >> 3 = 17), wire type 5 (32-bit)?
# No: 8d = 1000 1101. Wire type = 101 = 5. Field = 10001 = 17.
# 4 bytes: 75 52 43 4d = as float32: struct.unpack('<f', b'\x75\x52\x43\x4d')
val_8d_float = struct.unpack('<f', bytes([0x75, 0x52, 0x43, 0x4d]))[0]
w(f"\nBytes 8d 75 52 43 = field 17 wire5 float32: {val_8d_float}")

# Then a2 56 89...
# a2 56 = varint: 0x22 | (0x56 << 7) = 34 | 0x2b00 = 0x2b22 = 11042
# >> 3 = 1380, & 7 = 2 (LD)

# =============================================================================
# PART 3: Try a completely different approach.
# The structure after a SIF identifier seems to have field 2 (0x11) float64 values.
# Let's find ALL 0x11 tags followed by float64 in [0.01, 0.999] within 500 bytes of SIF ids
# =============================================================================
w()
w("="*80)
w("PART 3: Field 2 (tag 0x11) float64 values near SIF identifiers")
w("="*80)

sif_offsets = {}
for sif in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
    offs = []
    sif_b = sif.encode()
    pos = 0
    while True:
        idx = data.find(sif_b, pos)
        if idx == -1: break
        offs.append(idx)
        pos = idx + 1
    sif_offsets[sif] = offs

for sif, offsets in sif_offsets.items():
    w(f"\n{sif}: {len(offsets)} occurrence(s)")
    for occ in offsets:
        # Scan 500 bytes after for 0x11 followed by float64 in [0.01, 0.999]
        region = data[occ:min(len(data), occ+500)]
        found_in_occ = []
        for i in range(len(region)-8):
            if region[i] == 0x11:  # field 2, wire type 1
                try:
                    v = struct.unpack_from('<d', region, i+1)[0]
                    if 0.01 <= v <= 0.999:
                        found_in_occ.append((i, v))
                except: pass
        if found_in_occ:
            w(f"  At 0x{occ:08x}:")
            for rel, v in found_in_occ:
                w(f"    +{rel}: 0x11 -> float64 = {v:.6f}")

# =============================================================================
# PART 4: Look at the DATA AREA for SIF elements more carefully
# Find what protobuf field tags are used for SIF properties
# The SIF blocks at 0x307a93 have the structure. Let's parse it completely.
# =============================================================================
w()
w("="*80)
w("PART 4: Deep parse of SIF-011 data block")
w("="*80)

sif011_first_data = 0x307a93
w(f"SIF-011 data block at 0x{sif011_first_data:08x}")
w(hex_dump(data[sif011_first_data:sif011_first_data+250], sif011_first_data))

# Parse this as protobuf
w("\nProtobuf parse:")
pos = sif011_first_data + 7  # skip "SIF-011"
end = sif011_first_data + 250

def parse_varint(data, pos):
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            return None, pos
    return None, pos

while pos < end:
    if pos >= len(data):
        break
    start_pos = pos
    tag, pos = parse_varint(data, pos)
    if tag is None:
        w(f"  0x{start_pos:08x}: failed to parse varint tag")
        break
    field_num = tag >> 3
    wire_type = tag & 7
    if wire_type == 0:
        val, pos = parse_varint(data, pos)
        w(f"  0x{start_pos:08x}: field {field_num} varint = {val}")
    elif wire_type == 1:
        if pos + 8 <= len(data):
            val = struct.unpack_from('<d', data, pos)[0]
            w(f"  0x{start_pos:08x}: field {field_num} float64 = {val:.8f}")
            pos += 8
        else:
            w(f"  0x{start_pos:08x}: field {field_num} float64 (truncated)")
            break
    elif wire_type == 2:
        length, pos = parse_varint(data, pos)
        if length is None or length > 100000:
            w(f"  0x{start_pos:08x}: field {field_num} LD, bad length {length}")
            break
        content = data[pos:pos+min(length, 200)]
        content_str = safe_decode(content)
        w(f"  0x{start_pos:08x}: field {field_num} LD len={length}: {content_str!r}")
        pos += length
    elif wire_type == 5:
        if pos + 4 <= len(data):
            val = struct.unpack_from('<f', data, pos)[0]
            w(f"  0x{start_pos:08x}: field {field_num} float32 = {val:.8f}")
            pos += 4
        else:
            break
    else:
        w(f"  0x{start_pos:08x}: tag={tag} field={field_num} wire={wire_type} (unknown)")
        pos += 1

# =============================================================================
# PART 5: Look for the "partial_stroke", "pst", "eobt" in the data blocks
# nearby SIF elements by searching broader patterns
# =============================================================================
w()
w("="*80)
w("PART 5: Look at complete SIF-011 data block (all 21 occurrences)")
w("="*80)

# The "data" occurrences are those with substantial content after them
# Let's look for occurrences that are followed by float values in a range
# typical for SIL calculations: 1e-5 to 1e-2 (PFD range), or 0.3-0.999 (coverage)

for sif_name in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
    sif_b = sif_name.encode()
    pos = 0
    occ_idx = 0
    while True:
        idx = data.find(sif_b, pos)
        if idx == -1: break
        occ_idx += 1
        # Check next 20 bytes for field type
        region = data[idx+len(sif_b):idx+len(sif_b)+20]
        region_str = safe_decode(region)
        # Find all float64 values in next 1000 bytes in coverage/PFD range
        coverage_vals = []
        pfd_vals = []
        for i in range(min(1000, len(data)-idx-len(sif_b)-8)):
            off = idx + len(sif_b) + i
            if off + 8 <= len(data):
                try:
                    v = struct.unpack_from('<d', data, off)[0]
                    if 0.3 <= v <= 0.9999:
                        coverage_vals.append((i, v))
                    elif 1e-6 <= v <= 0.1:
                        pfd_vals.append((i, v))
                except: pass
        if coverage_vals or pfd_vals:
            w(f"\n{sif_name} occ {occ_idx} at 0x{idx:08x}:")
            w(f"  Coverage vals (0.3-1.0): {[(f'+{r}: {v:.4f}') for r,v in coverage_vals[:5]]}")
            w(f"  PFD vals (1e-6 to 0.1): {[(f'+{r}: {v:.2e}') for r,v in pfd_vals[:5]]}")
        pos = idx + 1

# =============================================================================
# PART 6: Search for float64 values in the entire file near SIF identifiers
# but use a more targeted approach - look for the protobuf sub-message structure
# that contains PFD and other SIL parameters
# =============================================================================
w()
w("="*80)
w("PART 6: Search for PFD-range values (1e-4 to 0.1) near each SIF")
w("="*80)

# For each SIF, find nearest float64 values in PFD range
for sif_name in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
    sif_b = sif_name.encode()
    # Find the "main" occurrence (the one with most data)
    pos = 0
    best_occ = None
    best_count = 0
    while True:
        idx = data.find(sif_b, pos)
        if idx == -1: break
        # Count float64 vals in next 2000 bytes
        cnt = 0
        for i in range(2000):
            off = idx + i
            if off + 8 <= len(data):
                try:
                    v = struct.unpack_from('<d', data, off)[0]
                    if 1e-8 <= abs(v) <= 1e8 and not (v != v):
                        cnt += 1
                except: pass
        if cnt > best_count:
            best_count = cnt
            best_occ = idx
        pos = idx + 1

    if best_occ is None:
        continue

    w(f"\n{sif_name} (best occurrence at 0x{best_occ:08x}, {best_count} floats in range):")

    # Collect ALL float64 in various ranges
    pfd_vals = []
    coverage_vals = []
    for i in range(4000):
        off = best_occ + i
        if off + 8 > len(data): break
        try:
            v = struct.unpack_from('<d', data, off)[0]
            if 1e-7 <= v <= 0.05:
                pfd_vals.append((i, v))
            elif 0.5 <= v <= 0.9999:
                coverage_vals.append((i, v))
        except: pass

    w(f"  PFD-range vals (1e-7 to 0.05):")
    for rel, v in pfd_vals[:15]:
        tag_byte = data[best_occ+rel-1] if rel > 0 else 0
        w(f"    +{rel} (tag_prev=0x{tag_byte:02x}): {v:.4e}")
    w(f"  Coverage-range vals (0.5-0.9999):")
    for rel, v in coverage_vals[:15]:
        tag_byte = data[best_occ+rel-1] if rel > 0 else 0
        w(f"    +{rel} (tag_prev=0x{tag_byte:02x}): {v:.4f}")

# =============================================================================
# PART 7: Read the FULL raw hex blocks around each SIF's "data record"
# There appear to be 2 types: "definition" (first occurrence) and "data" (second)
# =============================================================================
w()
w("="*80)
w("PART 7: Full hex dump of SIF data blocks (2nd occurrence = actual data)")
w("="*80)

for sif_name in ['SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']:
    sif_b = sif_name.encode()
    occurrences = []
    pos = 0
    while True:
        idx = data.find(sif_b, pos)
        if idx == -1: break
        occurrences.append(idx)
        pos = idx + 1

    w(f"\n{sif_name}: {len(occurrences)} occurrences")
    for occ_i, occ in enumerate(occurrences[:3]):
        w(f"\n  Occurrence {occ_i+1} at 0x{occ:08x}:")
        # Dump 300 bytes
        w(hex_dump(data[occ:min(len(data), occ+300)], occ))

out.close()
print("Done.")
