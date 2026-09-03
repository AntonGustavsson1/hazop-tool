import sys
import io
import struct
import os
import re
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

INPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp"
OUTPUT_FILE = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\SIL projekt\exp_pst_findings.txt"

def safe_decode(b):
    return b.decode('latin-1', errors='replace')

def hex_and_ascii(data):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"  {i:4d}: {hex_part:<48}  {ascii_part}")
    return '\n'.join(lines)

print(f"Reading file: {INPUT_FILE}")
with open(INPUT_FILE, 'rb') as f:
    data = f.read()
print(f"File size: {len(data):,} bytes")

out = open(OUTPUT_FILE, 'w', encoding='utf-8', errors='replace')

def w(s=''):
    out.write(s + '\n')

w(f"EXP PST/EoBT Coverage Extractor Results")
w(f"File: {INPUT_FILE}")
w(f"File size: {len(data):,} bytes")
w('=' * 80)

# =============================================================================
# APPROACH 1: String search for PST/EoBT field names
# =============================================================================
w()
w('=' * 80)
w("APPROACH 1: String search for PST/EoBT/Coverage field names")
w('=' * 80)

search_terms_1 = [
    b"pst", b"PST", b"PartialStroke", b"partial_stroke", b"CovPST",
    b"eob", b"EoB", b"EndOfBatch", b"batch_coverage", b"BatchTest",
    b"ProofTestCoverage", b"TestCoverage", b"proof_test_coverage",
    b"PartialValveStrokeTest", b"partial_valve",
    b"Coverage", b"coverage", b"Partial", b"stroke", b"batch", b"Batch",
]

total_matches_1 = 0
for term in search_terms_1:
    pos = 0
    term_matches = []
    while True:
        idx = data.find(term, pos)
        if idx == -1:
            break
        term_matches.append(idx)
        pos = idx + 1
    if term_matches:
        total_matches_1 += len(term_matches)
        w()
        w(f"  Term: {term!r}  -> {len(term_matches)} match(es)")
        for idx in term_matches[:20]:  # limit to 20 per term
            start = max(0, idx - 40)
            end = min(len(data), idx + len(term) + 80)
            ctx = data[start:end]
            w(f"    Offset {idx:08x} ({idx}): ...{safe_decode(ctx)}...")
            w(f"    Hex context:")
            w(hex_and_ascii(data[max(0,idx-16):min(len(data),idx+len(term)+48)]))

w()
w(f"Approach 1 total matches: {total_matches_1}")

# =============================================================================
# APPROACH 2: SIF identifier context search
# =============================================================================
w()
w('=' * 80)
w("APPROACH 2: SIF identifier context search")
w('=' * 80)

sif_ids = [b"SIF-011", b"SIF-012", b"SIF-015", b"SIF-019", b"SIF-025", b"SIF-026",
           b"SIF011", b"SIF012", b"SIF015", b"SIF019", b"SIF025", b"SIF026"]

for sif in sif_ids:
    pos = 0
    while True:
        idx = data.find(sif, pos)
        if idx == -1:
            break
        w()
        w(f"  Found {sif!r} at offset {idx:08x} ({idx})")
        # Dump 200 bytes after
        after = data[idx:min(len(data), idx+200)]
        w(f"  Raw bytes (200 after):")
        w(hex_and_ascii(after))
        w(f"  ASCII: {safe_decode(after)}")

        # Scan for float32 and float64 in [0.01, 0.999]
        w(f"  Float32 candidates in range [0.01, 0.999]:")
        for i in range(len(after)-3):
            try:
                v = struct.unpack_from('<f', after, i)[0]
                if 0.01 <= v <= 0.999:
                    abs_off = idx + i
                    w(f"    float32 @ offset {abs_off:08x} ({abs_off}): {v:.6f}")
            except:
                pass
        w(f"  Float64 candidates in range [0.01, 0.999]:")
        for i in range(len(after)-7):
            try:
                v = struct.unpack_from('<d', after, i)[0]
                if 0.01 <= v <= 0.999:
                    abs_off = idx + i
                    w(f"    float64 @ offset {abs_off:08x} ({abs_off}): {v:.6f}")
            except:
                pass
        pos = idx + 1

# =============================================================================
# APPROACH 3: IEEE 754 value search for specific expected values
# =============================================================================
w()
w('=' * 80)
w("APPROACH 3: IEEE 754 specific value search")
w('=' * 80)

target_values = [
    0.385, 0.57, 0.575, 0.65, 0.70, 0.75, 0.788, 0.789,
    0.088, 0.874, 0.875, 0.97, 0.52, 0.58, 0.48,
    0.9, 0.95, 0.99, 0.85, 0.80, 0.60, 0.50, 0.40, 0.30
]
TOLERANCE = 0.001

w()
w("  Float64 (double, little-endian) matches:")
for target in target_values:
    target_bytes = struct.pack('<d', target)
    pos = 0
    while True:
        idx = data.find(target_bytes, pos)
        if idx != -1:
            start = max(0, idx - 60)
            end = min(len(data), idx + 8 + 60)
            w(f"    EXACT float64 {target} at offset {idx:08x} ({idx})")
            w(hex_and_ascii(data[start:end]))
            pos = idx + 1
        else:
            break

# Tolerance-based float64
w()
w("  Float64 tolerance-based scan (±0.001):")
found_f64 = {}
for i in range(0, len(data)-7, 1):
    try:
        v = struct.unpack_from('<d', data, i)[0]
        for target in target_values:
            if abs(v - target) <= TOLERANCE:
                if i not in found_f64:
                    found_f64[i] = v
    except:
        pass

for off in sorted(found_f64.keys()):
    v = found_f64[off]
    start = max(0, off - 60)
    end = min(len(data), off + 8 + 60)
    w(f"    float64 {v:.6f} at offset {off:08x} ({off})")
    w(hex_and_ascii(data[start:end]))

w()
w("  Float32 (little-endian) tolerance-based scan (±0.001):")
found_f32 = {}
for i in range(0, len(data)-3, 1):
    try:
        v = struct.unpack_from('<f', data, i)[0]
        for target in target_values:
            if abs(v - target) <= TOLERANCE:
                if i not in found_f32:
                    found_f32[i] = v
    except:
        pass

for off in sorted(found_f32.keys()):
    v = found_f32[off]
    start = max(0, off - 60)
    end = min(len(data), off + 4 + 60)
    w(f"    float32 {v:.6f} at offset {off:08x} ({off})")
    w(hex_and_ascii(data[start:end]))

w(f"  Float64 specific matches: {len(found_f64)}")
w(f"  Float32 specific matches: {len(found_f32)}")

# =============================================================================
# APPROACH 4: General coverage value scan
# =============================================================================
w()
w('=' * 80)
w("APPROACH 4: General coverage value scan (all floats in [0.01, 0.999])")
w('=' * 80)

# Collect all float64 in range
coverage_offsets_f64 = []
for i in range(0, len(data)-7, 8):  # stride 8 for efficiency
    try:
        v = struct.unpack_from('<d', data, i)[0]
        if 0.01 < v < 0.999:
            coverage_offsets_f64.append((i, v))
    except:
        pass

w(f"  Float64 (stride 8) values in (0.01, 0.999): {len(coverage_offsets_f64)} found")

# Cluster by proximity
if coverage_offsets_f64:
    clusters = []
    current_cluster = [coverage_offsets_f64[0]]
    for entry in coverage_offsets_f64[1:]:
        if entry[0] - current_cluster[-1][0] <= 64:
            current_cluster.append(entry)
        else:
            clusters.append(current_cluster)
            current_cluster = [entry]
    clusters.append(current_cluster)

    w(f"  Clusters (gap <= 64 bytes): {len(clusters)}")
    # Sort by cluster size descending, show top 30
    clusters.sort(key=lambda c: -len(c))
    w(f"  Top clusters:")
    for ci, cluster in enumerate(clusters[:30]):
        w(f"    Cluster {ci+1}: {len(cluster)} values, offsets {cluster[0][0]:08x}-{cluster[-1][0]:08x}")
        for off, val in cluster[:10]:
            w(f"      offset {off:08x} ({off}): {val:.6f}")
        if len(cluster) > 10:
            w(f"      ... and {len(cluster)-10} more")

# Show context for values matching expected within ±0.01
w()
w("  Values matching expected targets within ±0.01:")
LOOSE_TOL = 0.01
for i in range(0, len(data)-7, 1):
    try:
        v = struct.unpack_from('<d', data, i)[0]
        for target in target_values:
            if abs(v - target) <= LOOSE_TOL:
                start = max(0, i - 30)
                end = min(len(data), i + 8 + 30)
                w(f"    float64 {v:.6f} (target {target}) at offset {i:08x}")
                w(hex_and_ascii(data[start:end]))
                break
    except:
        pass

# =============================================================================
# APPROACH 5: Protobuf varint parsing
# =============================================================================
w()
w('=' * 80)
w("APPROACH 5: Protobuf-style field parsing")
w('=' * 80)

def read_varint(data, pos):
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            return None, pos
    return None, pos

# Try to find protobuf-like structures
# Wire type 1 = 64-bit (double), wire type 5 = 32-bit (float)
coverage_proto = []
pos = 0
while pos < len(data) - 8:
    b = data[pos]
    wire_type = b & 0x07
    field_num = b >> 3
    if wire_type in (1, 5) and 1 <= field_num <= 10000:
        next_pos = pos + 1
        if wire_type == 1 and next_pos + 8 <= len(data):
            try:
                v = struct.unpack_from('<d', data, next_pos)[0]
                if 0.01 < v < 0.999:
                    coverage_proto.append((pos, field_num, 'float64', v))
            except:
                pass
        elif wire_type == 5 and next_pos + 4 <= len(data):
            try:
                v = struct.unpack_from('<f', data, next_pos)[0]
                if 0.01 < v < 0.999:
                    coverage_proto.append((pos, field_num, 'float32', v))
            except:
                pass
    pos += 1

w(f"  Protobuf-style coverage candidates: {len(coverage_proto)}")
# Group by field_num
by_field = defaultdict(list)
for off, fn, typ, val in coverage_proto:
    by_field[fn].append((off, typ, val))

for fn in sorted(by_field.keys()):
    entries = by_field[fn]
    w(f"  Field {fn}: {len(entries)} occurrence(s)")
    for off, typ, val in entries[:5]:
        w(f"    offset {off:08x} ({off}): {typ} = {val:.6f}")
        start = max(0, off - 20)
        end = min(len(data), off + 20)
        ctx_bytes = data[start:end]
        w(f"    Context: {safe_decode(ctx_bytes)}")
        w(hex_and_ascii(ctx_bytes))

# =============================================================================
# APPROACH 6: Search for XML/text structures that may contain coverage data
# =============================================================================
w()
w('=' * 80)
w("APPROACH 6: XML/JSON/text structure search")
w('=' * 80)

xml_patterns = [b"<PST", b"<pst", b"<EoB", b"<eob", b"<Coverage", b"<coverage",
                b'"PST"', b'"pst"', b'"EoB"', b'"coverage"', b'"Coverage"',
                b"PST=", b"EoB=", b"coverage=", b"Coverage=",
                b"CovPST", b"CovEoB", b"covPST", b"covEoB",
                b"FractionDetected", b"fraction_detected",
                b"DC_", b"dc_", b"DiagnosticCoverage",
                b"SafeFailureFraction", b"SFF",
                b"lambda", b"Lambda", b"failure_rate",
                b"proof_test", b"ProofTest", b"TestInterval",
                b"<SIF", b"SIL", b"<loop", b"<Loop",
                ]

for term in xml_patterns:
    pos = 0
    matches = []
    while True:
        idx = data.find(term, pos)
        if idx == -1:
            break
        matches.append(idx)
        pos = idx + 1
    if matches:
        w(f"  {term!r}: {len(matches)} match(es)")
        for idx in matches[:5]:
            start = max(0, idx - 20)
            end = min(len(data), idx + len(term) + 120)
            w(f"    offset {idx:08x}: {safe_decode(data[start:end])}")

# =============================================================================
# APPROACH 7: Look at file header and structure
# =============================================================================
w()
w('=' * 80)
w("APPROACH 7: File header and structure analysis")
w('=' * 80)

w("  First 256 bytes:")
w(hex_and_ascii(data[:256]))
w()
w("  Bytes 256-512:")
w(hex_and_ascii(data[256:512]))
w()
w("  Last 256 bytes:")
w(hex_and_ascii(data[-256:]))

# Try to detect file format
w()
w("  File format detection:")
if data[:4] == b'PK\x03\x04':
    w("  -> ZIP archive (could be XML-based format like XLSX/DOCX)")
elif data[:2] == b'\xff\xfe' or data[:2] == b'\xfe\xff':
    w("  -> UTF-16 text file")
elif data[:3] == b'\xef\xbb\xbf':
    w("  -> UTF-8 with BOM text file")
elif data[:4] == b'\xd0\xcf\x11\xe0':
    w("  -> OLE2 Compound Document (legacy Office format)")
elif data[:5] == b'<?xml':
    w("  -> XML file")
else:
    w(f"  -> Unknown, magic bytes: {data[:16].hex()}")

# If ZIP, try to list contents
if data[:4] == b'PK\x03\x04':
    import zipfile
    import zipfile as zf
    try:
        import io as _io
        z = zipfile.ZipFile(_io.BytesIO(data))
        w("  ZIP contents:")
        for name in z.namelist():
            info = z.getinfo(name)
            w(f"    {name} ({info.file_size} bytes)")

        # For each XML file inside, search for PST/EoBT
        w()
        w("  Searching XML content inside ZIP:")
        for name in z.namelist():
            if any(name.lower().endswith(ext) for ext in ['.xml', '.json', '.txt', '.csv']):
                try:
                    content = z.read(name)
                    for term in [b'PST', b'EoB', b'Coverage', b'coverage', b'pst', b'eob',
                                 b'CovPST', b'partial', b'Partial', b'batch', b'Batch']:
                        idx = content.find(term)
                        if idx != -1:
                            w(f"    In {name}: found {term!r} at {idx}")
                            start = max(0, idx - 60)
                            end = min(len(content), idx + len(term) + 120)
                            w(f"      Context: {safe_decode(content[start:end])}")
                except Exception as e:
                    w(f"    Error reading {name}: {e}")
    except Exception as e:
        w(f"  ZIP read error: {e}")

# OLE2 compound document analysis
if data[:4] == b'\xd0\xcf\x11\xe0':
    w()
    w("  OLE2 compound document detected - trying to extract streams:")
    try:
        import olefile
        ole = olefile.OleFileIO(_io.BytesIO(data) if 'data' in dir() else data)
        for entry in ole.listdir():
            w(f"    Stream: {'/'.join(entry)}")
        w("  olefile available, attempting stream extraction...")
    except ImportError:
        w("  olefile not available, doing manual OLE2 parsing...")
        # Try to find readable strings in OLE2
        # Look for UTF-16 encoded strings
        try:
            decoded_utf16 = data.decode('utf-16-le', errors='replace')
            for term in ['PST', 'EoB', 'Coverage', 'coverage', 'pst', 'eob', 'CovPST']:
                idx = decoded_utf16.find(term)
                if idx != -1:
                    w(f"  UTF-16LE: found {term!r} at char {idx}")
                    w(f"    Context: {decoded_utf16[max(0,idx-40):idx+len(term)+80]}")
        except:
            pass

# =============================================================================
# Search for UTF-16 encoded PST strings
# =============================================================================
w()
w('=' * 80)
w("APPROACH 8: UTF-16 encoded string search")
w('=' * 80)

utf16_terms = ['PST', 'EoB', 'pst', 'eob', 'Coverage', 'coverage',
               'CovPST', 'PartialStroke', 'BatchTest', 'partial', 'batch',
               'SIF-011', 'SIF-012', 'SIF-015', 'SIF-019', 'SIF-025', 'SIF-026']

for term in utf16_terms:
    # UTF-16 LE
    term_bytes_le = term.encode('utf-16-le')
    pos = 0
    matches_le = []
    while True:
        idx = data.find(term_bytes_le, pos)
        if idx == -1:
            break
        matches_le.append(idx)
        pos = idx + 1
    if matches_le:
        w(f"  UTF-16LE {term!r}: {len(matches_le)} match(es)")
        for idx in matches_le[:10]:
            start = max(0, idx - 40)
            end = min(len(data), idx + len(term_bytes_le) + 160)
            w(f"    offset {idx:08x}: {safe_decode(data[start:end])}")
            w(hex_and_ascii(data[start:end]))

# =============================================================================
# Final summary
# =============================================================================
w()
w('=' * 80)
w("SUMMARY")
w('=' * 80)
w(f"  File size: {len(data):,} bytes")
w(f"  Approach 1 (string search) total matches: {total_matches_1}")
w(f"  Approach 3 float64 specific matches: {len(found_f64)}")
w(f"  Approach 3 float32 specific matches: {len(found_f32)}")
w(f"  Approach 4 float64 coverage values (stride 8): {len(coverage_offsets_f64)}")
w(f"  Approach 5 protobuf candidates: {len(coverage_proto)}")

out.close()
print(f"Done. Results written to: {OUTPUT_FILE}")
print(f"Approach 1 matches: {total_matches_1}")
print(f"Approach 3 float64: {len(found_f64)}, float32: {len(found_f32)}")
print(f"Approach 4 float64 values: {len(coverage_offsets_f64)}")
print(f"Approach 5 protobuf: {len(coverage_proto)}")
