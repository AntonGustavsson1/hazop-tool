# -*- coding: utf-8 -*-
import sys, io, re, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

path = r'C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\001 - Hybrit Pilot Plant 2022-08-31.exp'

with open(path, 'rb') as f:
    data = f.read()

all_strings = [(m.start(), m.group().decode('ascii','replace').strip())
               for m in re.finditer(rb'[ -~]{5,}', data)]

# Hitta SIF-001 definition
sif1_def = next((pos for pos,s in all_strings if s.startswith('SIF-001"')), 0)
print(f'SIF-001 definition vid: {sif1_def}')

# Scanna 200000 bytes
chunk_start = sif1_def
chunk = data[chunk_start: chunk_start + 200000]

def find_f32(chunk, val):
    b = struct.pack('<f', val)
    positions = []
    pos = 0
    while True:
        p = chunk.find(b, pos)
        if p < 0: break
        positions.append(p)
        pos = p + 1
    return positions

known_ptcs = [0.975, 0.85, 0.90, 0.87, 0.83, 0.99, 0.98, 0.94, 0.92, 0.10, 0.05]
print()
print('Kanda parametrar som float32 i SIF-001-blocket:')
for ptc in known_ptcs:
    poss = find_f32(chunk, ptc)
    if poss:
        for p in poss[:2]:
            ctx = chunk[max(0,p-25):p+30]
            r = ''.join(chr(c) if 32<=c<127 else '.' for c in ctx)
            print(f'  {ptc:.3f}: +{p:6d}: {r[:70]}')

# Sök 0.975 (PTC for sensor) och 0.85 (PTC for FE) nara varandra
pos975 = find_f32(chunk, 0.975)
pos085 = find_f32(chunk, 0.85)
print()
print(f'0.975 (sensor PTC) forekommer {len(pos975)} ganger')
print(f'0.85  (FE PTC) forekommer {len(pos085)} ganger')
if pos975 and pos085:
    for p1 in pos975[:3]:
        for p2 in pos085[:10]:
            if abs(p1 - p2) < 5000:
                print(f'  Nara varandra: 0.975 @ +{p1}, 0.85 @ +{p2} (avst: {abs(p1-p2)})')
