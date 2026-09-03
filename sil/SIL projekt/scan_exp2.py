# -*- coding: utf-8 -*-
import sys, io, re, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

path = r'C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp'

with open(path, 'rb') as f:
    data = f.read()

all_strings = [(m.start(), m.group().decode('ascii','replace').strip())
               for m in re.finditer(rb'[ -~]{5,}', data)]

# Hitta alla SIF-XXXx positioner
sifx_pos = {}
for pos, s in all_strings:
    m = re.match(r'^(SIF-\d+)x$', s)
    if m:
        sifx_pos[m.group(1)] = pos

print(f'SIF-XXXx positioner: {len(sifx_pos)}')
print(f'SIF-001x vid: {sifx_pos.get("SIF-001", "N/A")}')
print(f'SIF-009x vid: {sifx_pos.get("SIF-009", "N/A")}')
print()

# Scanna kring SIF-001x (vid slutet av filen)
sif1x_start = sifx_pos.get('SIF-001', len(data) - 100000)
chunk_start = max(0, sif1x_start - 5000)
chunk_end = min(len(data), sif1x_start + 100000)
chunk = data[chunk_start:chunk_end]

def find_f32(data, val):
    b = struct.pack('<f', val)
    p, positions = 0, []
    while True:
        p = data.find(b, p)
        if p < 0: break
        positions.append(p); p += 1
    return positions

print('=== Sök i SIF-001x-blocket ===')
for val in [0.975, 0.85, 0.87, 0.90, 0.94, 0.10, 0.05, 0.02]:
    poss = find_f32(chunk, val)
    if poss:
        for p in poss[:2]:
            ctx = chunk[max(0,p-20):p+25]
            r = ''.join(chr(c) if 32<=c<127 else '.' for c in ctx)
            print(f'  {val:.3f}: +{p:6d}: {r[:60]}')

print()
# Sök som integers (1000 = 100%, 975 = 97.5%, 850 = 85%)
print('=== Sök PTC som integer (x10 eller x1000) ===')
for val_int in [975, 850, 870, 900, 940, 100, 10, 5, 2, 12, 24]:
    # som varint32 little-endian 2-byte
    if val_int < 128:
        b_vi = bytes([val_int])
    elif val_int < 16384:
        b_vi = bytes([val_int & 0x7F | 0x80, val_int >> 7])
    else:
        b_vi = bytes([val_int & 0x7F | 0x80, (val_int >> 7) & 0x7F | 0x80, val_int >> 14])

    p = chunk.find(b_vi)
    if p >= 0:
        ctx = chunk[max(0,p-15):p+20]
        r = ''.join(chr(c) if 32<=c<127 else '.' for c in ctx)
        print(f'  int={val_int}: +{p:6d}: {r[:50]}')

print()
# Visa läsbara strängar i SIF-001x-blocket
print('=== Strängar i SIF-001x-blocket ===')
block_strings = [(pos - chunk_start, s) for pos, s in all_strings
                 if chunk_start <= pos <= chunk_end]
for offset, s in sorted(block_strings)[:40]:
    print(f'  +{offset:6d}: {s[:100]}')
