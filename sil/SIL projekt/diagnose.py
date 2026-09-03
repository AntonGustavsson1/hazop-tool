# -*- coding: utf-8 -*-
"""
diagnose.py — Djupanalys av avvikelsemönster i hybrit_calc.py

Undersöker rotorsaker för varje avvikelsekategori:
  A) Logic solver -45/-58%  (SERH-data)
  B) 1oo1 sensor b=10%      (SIF-020/023/024 – vi räknar för högt)
  C) 2oo3 sensor utan PST   (SIF-015/018/022/025/027 – vi räknar för lågt)
  D) FE 1oo2 2585 FIT       (SIF-020-024 – konsekvent -22.8%)
  E) SIF-028/029            (saknar delsystem-ref)
"""

import sys, io, math
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Formel identisk med hybrit_calc.py ────────────────────────────────────────
def pfd1(ldu, ldd, PTC, TI, MTTR, MT):
    return ldd*MTTR + PTC*ldu*(TI/2 + MTTR) + (1-PTC)*ldu*MT/2

def pfd_formula(arch, ldu, ldd, beta, TI, MTTR, MT, CPT, ldu2=None, diverse=False):
    bd = beta/2
    a  = arch.lower().replace(' ','')
    def p1(ld, ldd_): return pfd1(ld, ldd_, CPT, TI, MTTR, MT)

    if a == '1oo1':   return p1(ldu, ldd)
    elif a == '1oo2':
        if diverse and ldu2:
            indep  = (1-beta)**2 * ldu * ldu2 * TI**2/3
            ccf_du = beta * p1(min(ldu,ldu2), 0)
        else:
            indep  = ((1-beta)*ldu)**2 * TI**2/3
            ccf_du = beta * p1(ldu, 0)
        return bd*ldd*MTTR + indep + ccf_du
    elif a == '2oo2': return (2-beta)*p1(ldu, ldd)
    elif a == '2oo3':
        indep  = ((1-beta)*ldu)**2 * TI**2
        ccf_du = beta * p1(ldu, 0)
        return bd*ldd*MTTR + indep + ccf_du
    return p1(ldu, ldd)

def back_calc_ldu(arch, ldd, beta, TI, MTTR, MT, CPT, target_pfd, lo=1e-12, hi=1e-4):
    """Bakåtberäkna λDU som ger target_pfd."""
    for _ in range(80):
        mid = (lo+hi)/2
        if pfd_formula(arch, mid, ldd, beta, TI, MTTR, MT, CPT) < target_pfd:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2

def back_calc_beta(arch, ldu, ldd, TI, MTTR, MT, CPT, target_pfd, lo=0.0, hi=0.99):
    """Bakåtberäkna β som ger target_pfd."""
    for _ in range(80):
        mid = (lo+hi)/2
        if pfd_formula(arch, ldu, ldd, mid, TI, MTTR, MT, CPT) < target_pfd:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2

def back_calc_pst(arch, ldu, ldd, beta, TI, MTTR, MT, CPT, target_pfd, lo=0.0, hi=0.98):
    """Bakåtberäkna PST-täckning (c) som ger target_pfd."""
    def calc(c):
        return pfd_formula(arch, (1-c)*ldu, ldd+c*ldu, beta, TI, MTTR, MT, CPT)
    if calc(0) <= target_pfd: return 0.0
    for _ in range(80):
        mid = (lo+hi)/2
        if calc(mid) > target_pfd: lo = mid
        else: hi = mid
    return (lo+hi)/2

SEP = '─'*90

# ══════════════════════════════════════════════════════════════════════════════════
print(SEP)
print('KATEGORI A — LOGIC SOLVER  (SERH-bidrag)')
print(SEP)
# Identiska parametrar men 3 olika ref-nivåer: 7.31e-6, 6.90e-6, 9.40e-6
logic_cases = [
    ('Grupp 1 (SIF-001/002/006/007/009/016/017/018/021/024/025/027)',
     0.38e-9, 2279.3e-9, 0.0, 8640, 24, 15*8760, 0.90, 7.31e-6),
    ('Grupp 2 (SIF-003/004/005/008)',
     0.36e-9, 2109.6e-9, 0.0, 8640, 24, 15*8760, 0.90, 6.90e-6),
    ('Grupp 3 (SIF-015/020/022/023)',
     0.38e-9, 2279.3e-9, 0.0, 8640, 24, 15*8760, 0.90, 9.40e-6),
    ('SIF-011 (MT=10yr)',
     0.38e-9, 2279.3e-9, 0.0, 8640, 24, 10*8760, 0.90, 2.76e-6),
    ('SIF-019 (PST 5%)',
     0.38e-9, 2279.3e-9, 0.0, 8640, 24, 15*8760, 0.90, 3.79e-6),
    ('SIF-026 (PTC=100%, PST=94%, MT=10yr)',
     0.33e-9, 2279.3e-9, 0.0, 8640, 24, 10*8760, 1.00, 9.76e-8),
]
for name, ldu, ldd, beta, TI, MTTR, MT, PTC, ref in logic_cases:
    calc = pfd_formula('1oo1', ldu, ldd, beta, TI, MTTR, MT, PTC)
    ratio = ref/calc
    needed_ldu = back_calc_ldu('1oo1', ldd, beta, TI, MTTR, MT, PTC, ref) * 1e9
    print(f'\n  {name}')
    print(f'  PFD_kalk={calc:.3e}  PFD_ref={ref:.3e}  '
          f'Ratio={ratio:.3f}  => SERH-bidrag ≈ +{(ratio-1)*100:.0f}%')
    print(f'  λDU_behövs={needed_ldu:.3f} FIT  (extraherat: {ldu*1e9:.2f} FIT, '
          f'skillnad: +{needed_ldu - ldu*1e9:.2f} FIT)')

print(f'\n  Slutsats: exSILentia/SERH lägger till ~0.5-2.7 FIT "intern" λDU i Logic Solver')
print(f'  som inte syns i komponenttabellerna. Kan ej replikeras utan SERH-licens.')

# ══════════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('KATEGORI B — SENSOR 1oo1 β=10%  (SIF-020/023/024 – vi räknar FÖR HÖGT)')
print(SEP)
# Dessa tre är de enda sensorer med b=10% + arkitektur 1oo1
# Vi undersöker om den korrekta arkitekturen är 2oo3 eller 2oo2
cases_B = [
    ('SIF-020', 268.0e-9, 36.6e-9, 0.10, 8640, 24, 15*8760, 0.99, 9.06e-4),
    ('SIF-023', 379.01e-9,54.9e-9, 0.10, 8640, 24, 15*8760, 0.90, 2.20e-3),
    ('SIF-024', 268.0e-9, 36.6e-9, 0.10, 8640, 24, 15*8760, 0.99, 8.94e-4),
]
print(f'\n  {'SIF':<8} {'Extraherad λDU':>14} {'lDD FIT':>8} {'n*18.3':>8} '
      f"{'PFD_kalk':>10} {'PFD_ref':>10} {'diff':>8}")
print(f'  {"":-<80}')
for name, ldu, ldd, beta, TI, MTTR, MT, PTC, ref in cases_B:
    n_dd = round(ldd*1e9 / 18.3)
    calc_1oo1 = pfd_formula('1oo1', ldu, ldd, beta, TI, MTTR, MT, PTC)
    diff = (calc_1oo1-ref)/ref*100
    print(f'  {name:<8} {ldu*1e9:>14.1f} FIT {ldd*1e9:>6.1f} FIT {n_dd:>6}x    '
          f'{calc_1oo1:>10.3e} {ref:>10.3e} {diff:>+7.1f}%')

print()
print('  Testa om korrekt arkitektur är 2oo3 (med per-kanal λDU = total/n_kanaler):')
print(f'  {'SIF':<8} {'Arkitektur':>12} {'λDU/kanal FIT':>14} '
      f"{'PFD_kalk':>10} {'PFD_ref':>10} {'diff':>8} {'β_rätt':>10}")
for name, ldu_tot, ldd, beta, TI, MTTR, MT, PTC, ref in cases_B:
    n_kanaler = round(ldd*1e9 / 18.3)  # Antal kanaler baserat på lDD
    ldu_per = ldu_tot / n_kanaler
    for arch in ['2oo2', '2oo3', '1oo2']:
        calc = pfd_formula(arch, ldu_per, ldd/n_kanaler, beta, TI, MTTR, MT, PTC)
        diff = (calc-ref)/ref*100
        beta_fit = back_calc_beta(arch, ldu_per, ldd/n_kanaler, TI, MTTR, MT, PTC, ref)
        print(f'  {name:<8} {arch:>12} {ldu_per*1e9:>12.1f} FIT '
              f'{calc:>10.3e} {ref:>10.3e} {diff:>+7.1f}%  β_rätt={beta_fit:.3f}')
    print()

print('  Slutsats: Trolig rotorsak = extraktorn läser "1oo1" som inner-voting men')
print('  missar outer multi-group struktur. λDU summeras felaktigt.')

# ══════════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('KATEGORI C — 2oo3 SENSOR utan PST  (SIF-015/018/022/025/027 – vi räknar FÖR LÅGT)')
print(SEP)
cases_C = [
    ('SIF-015', 76.0e-9,  18.3e-9, 0.10, 8640, 24, 15*8760, 0.90, 1.31e-4),
    ('SIF-018', 595.0e-9, 36.6e-9, 0.10, 8640, 24, 15*8760, 0.99, 1.22e-3),
    ('SIF-022', 134.0e-9, 18.3e-9, 0.10, 8640, 24, 15*8760, 0.99, 1.20e-4),
    ('SIF-025', 402.0e-9, 18.3e-9, 0.10, 8640, 24, 15*8760, 0.90, 7.62e-4),
    ('SIF-027', 76.0e-9,  18.3e-9, 0.10, 8640, 24, 15*8760, 0.99, 6.77e-5),
]
print(f'\n  Bakåtberäknar λDU, β, och lDD som matchade referensen:')
print(f'  {'SIF':<8} {'λDU_ext':>10} {'lDD_ext':>9} {'n*18.3':>8} '
      f"{'PFD_kalk':>10} {'PFD_ref':>10} {'diff':>8}")
print(f'  {"":-<80}')
for name, ldu, ldd, beta, TI, MTTR, MT, PTC, ref in cases_C:
    n_dd = round(ldd*1e9 / 18.3)
    calc = pfd_formula('2oo3', ldu, ldd, beta, TI, MTTR, MT, PTC)
    diff = (calc-ref)/ref*100
    print(f'  {name:<8} {ldu*1e9:>8.1f} FIT {ldd*1e9:>7.1f} FIT {n_dd:>6}x    '
          f'{calc:>10.3e} {ref:>10.3e} {diff:>+7.1f}%')

print()
print('  Bakåtberäknade λDU-värden som matchade (2oo3-formel oförändrad):')
for name, ldu, ldd, beta, TI, MTTR, MT, PTC, ref in cases_C:
    ldu_needed = back_calc_ldu('2oo3', ldd, beta, TI, MTTR, MT, PTC, ref) * 1e9
    n_dd = round(ldd*1e9 / 18.3)
    ratio = ldu_needed / (ldu*1e9)
    print(f'  {name:<8}: λDU_ext={ldu*1e9:.1f} FIT  λDU_behövs={ldu_needed:.1f} FIT  '
          f'(faktor ×{ratio:.2f}, +{ldu_needed-ldu*1e9:.1f} FIT saknas)')

print()
print('  Bakåtberäknade β-värden som matchade (2oo3, λDU oförändrad):')
for name, ldu, ldd, beta, TI, MTTR, MT, PTC, ref in cases_C:
    beta_needed = back_calc_beta('2oo3', ldu, ldd, TI, MTTR, MT, PTC, ref)
    calc_match = pfd_formula('2oo3', ldu, ldd, beta_needed, TI, MTTR, MT, PTC)
    print(f'  {name:<8}: β_ext={beta:.2f}  β_behövs={beta_needed:.3f}  '
          f'(PFD_check={calc_match:.3e})')

print()
print('  Fördelning av termer i 2oo3-formeln (vid extraherade värden):')
for name, ldu, ldd, beta, TI, MTTR, MT, PTC, ref in cases_C:
    indep  = ((1-beta)*ldu)**2 * TI**2
    ccf_du = beta * pfd1(ldu, 0, PTC, TI, 24, MT)
    ccf_dd = (beta/2)*ldd*24
    tot    = indep + ccf_du + ccf_dd
    print(f'  {name:<8}: indep={indep/tot*100:.0f}%  ccf_du={ccf_du/tot*100:.0f}%  '
          f'ccf_dd={ccf_dd/tot*100:.1f}%  '
          f'(CCF-termen β×pfd1 dominerar: {ccf_du:.2e})')

print()
print('  Testa om n_groups > 1 ger bättre matchning:')
print('  (Hypotes: extraktorn missar outer-grupper, λDU = summa av alla grupper)')
for name, ldu_tot, ldd, beta, TI, MTTR, MT, PTC, ref in cases_C:
    print(f'\n  {name}: λDU_tot={ldu_tot*1e9:.1f} FIT, lDD={ldd*1e9:.1f} FIT')
    for n in [2, 3]:
        ldu_per = ldu_tot / n
        # Outer NooN serie med beta_between=0.10 (standard)
        for bb in [0.0, 0.05, 0.10]:
            outer = n - (n-1)*bb
            calc_inner = pfd_formula('2oo3', ldu_per, ldd, beta, TI, MTTR, MT, PTC)
            calc_total = outer * calc_inner
            diff = (calc_total - ref)/ref*100
            if abs(diff) < 20:
                print(f'    n_grupper={n}, β_between={bb:.2f}: '
                      f'PFD={calc_total:.3e} vs ref={ref:.3e}  diff={diff:+.1f}%  ← TROLIG MATCH')

# ══════════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('KATEGORI D — FE 1oo2 λDU=2585.5 FIT  (SIF-020-024 – konsekvent -22.8%)')
print(SEP)
ldu_fe = 2585.5e-9
cases_D = [
    ('SIF-020', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98, 2.04e-3),
    ('SIF-021', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98, 2.04e-3),
    ('SIF-022', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98, 2.04e-3),
    ('SIF-023', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98, 2.04e-3),
    ('SIF-024', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98, 2.04e-3),
]
calc_base = pfd_formula('1oo2', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98)
print(f'\n  Grundberäkning: PFD_kalk={calc_base:.4e}, PFD_ref=2.04e-3')
print(f'  Diff: {(calc_base-2.04e-3)/2.04e-3*100:+.1f}%')

# Bryt ner i termer
indep  = ((1-0.10)*ldu_fe)**2 * 8640**2/3
ccf_du = 0.10 * pfd1(ldu_fe, 0, 0.98, 8640, 24, 15*8760)
print(f'\n  Termanalys för 1oo2 med λDU=2585.5 FIT:')
print(f'    Oberoende term:    {indep:.3e}  ({indep/calc_base*100:.0f}%)')
print(f'    CCF-DU term (β×pfd1): {ccf_du:.3e}  ({ccf_du/calc_base*100:.0f}%)')
print(f'    CCF dominerar totalt!')

print()
print('  Bakåtberäknade PST-täckningar som matchar PFD_ref=2.04e-3:')
pst_needed = back_calc_pst('1oo2', ldu_fe, 0.0, 0.10, 8640, 24, 15*8760, 0.98, 2.04e-3)
ldu_eff = (1-pst_needed)*ldu_fe
calc_check = pfd_formula('1oo2', ldu_eff, pst_needed*ldu_fe, 0.10, 8640, 24, 15*8760, 0.98)
print(f'    PST-täckning behövs: {pst_needed*100:.1f}%')
print(f'    λDU_eff efter PST:   {ldu_eff*1e9:.1f} FIT (från {ldu_fe*1e9:.1f} FIT)')
print(f'    PFD_check: {calc_check:.4e}  (ska vara 2.04e-3)')

# Jämför med SIF-019 FE (samma ventiltyp, EoBT=8.8%)
print()
print('  Jämförelse SIF-019 FE (samma ventiltyp, bekräftad EoBT=8.8%):')
ldu_019 = (1-0.088)*ldu_fe
ldd_019 = 0.088*ldu_fe
calc_019 = pfd_formula('1oo2', ldu_019, ldd_019, 0.28, 8640, 24, 15*8760, 0.98)
print(f'    SIF-019 (3-grupp 3oo3 outer_factor=2.44, per-grupp 1oo2, EoBT=8.8%):')
print(f'    PFD_per_grupp = {calc_019:.3e}, ×2.44 = {calc_019*2.44:.3e}')
print(f'    PFD_ref = 3.68e-3')
print()
print('  Testa alternativa TI för FE (12 mån = 8640h är extraherat, men kanske längre):')
for ti_months in [12, 18, 24, 36]:
    ti_h = ti_months * 720
    calc = pfd_formula('1oo2', ldu_fe, 0.0, 0.10, ti_h, 24, 15*8760, 0.98)
    diff = (calc - 2.04e-3)/2.04e-3*100
    print(f'    TI={ti_months}mån ({ti_h}h): PFD={calc:.3e}  diff={diff:+.1f}%')

print()
print('  Slutsats: FE-avvikelsen kräver PST-täckning ≈29.5% ELLER TI>24 mån.')
print('  Troligt: EoBT (End-of-Batch Test) tillämpas men syns ej i dokument.')

# ══════════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('KATEGORI E — SIF-028 och SIF-029  (delsystem-PFD saknas i rapport)')
print(SEP)
print("""
  SIF-027/028/029 har identisk total referens-PFD=3.92e-3.
  SIF-028 och SIF-029 har inga sub-PFD i rapporten → extraktorn hittar inte
  individuella delsystems-PFD, bara totalen.

  SIF-027: total PFD=3.92e-3, kalkyl=3.19e-3  diff=-18.6%
  SIF-028: total PFD=3.92e-3, kalkyl=6.95e-3  diff=+77.4%  ← FE skiljer!
  SIF-029: total PFD=3.92e-3, kalkyl=2.88e-3  diff=-26.7%

  Alla tre har samma sensor (2oo3, 76 FIT) och logic.
  SIF-028 FE = 1oo1 (λDU=1240.5 FIT, PTC=98%)
  SIF-029 FE = 1oo2 (λDU=5171 FIT, PTC=99%)
  SIF-027 FE = 1oo2 (λDU=5171 FIT, PTC=98%)

  Troligt: SIF-028/029 delar referenstotal med SIF-027 pga samma rad i summary-tabell.
  Faktisk FE-arkitektur/parameter extraktion behöver verifieras mot originaldokumentet.
""")
# Beräkna vad SIF-028 FE borde vara om total=3.92e-3
sensor_027 = pfd_formula('2oo3', 76e-9, 18.3e-9, 0.10, 8640, 24, 15*8760, 0.99)
logic_027  = pfd_formula('1oo1', 0.38e-9, 2279.3e-9, 0.0, 8640, 24, 15*8760, 0.90)
fe_budget  = 3.92e-3 - sensor_027 - logic_027
print(f'  Om SIF-028 total=3.92e-3 och sensor+logic kvar:')
print(f'    sensor PFD kalk = {sensor_027:.3e}')
print(f'    logic  PFD kalk = {logic_027:.3e}  (men verklig ≈ 7.31e-6)')
print(f'    FE budget (kvar) = {fe_budget:.3e}')
print(f'    Vår FE kalk för SIF-028 (1oo1, 1240FIT) = '
      f'{pfd_formula("1oo1",1240.5e-9,0,0,8640,24,15*8760,0.98):.3e}')
print(f'    → SIF-028 FE måste vara fundamentalt annorlunda än vad vi extraherat.')

# ══════════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('SAMMANFATTNING: ROTORSAKER & ÅTGÄRDER')
print(SEP)
print("""
A) LOGIC SOLVER (−45% till −58%):
   Rotorsak: SERH lägger till 0.5–2.7 FIT intern λDU (firmware, watchdog, diverse
   elektronik) som inte syns i fältkomponent-tabellerna.
   Åtgärd: Ej möjlig utan SERH-licens. Acceptera som känd systematisk avvikelse.
   Påverkan på total: minimal (logic är ~0.3% av total PFD i de flesta SIF:ar).

B) 1oo1 SENSOR β=10% (SIF-020/023/024) – kalkyl FÖR HÖG:
   Rotorsak: Extraktorn läser inner-voting = 1oo1 korrekt, men verkar missa att
   sensorn faktiskt är ett multi-kanal-system (2 eller 3 kanaler summerade).
   β=10% tillhör INTER-kanal CCF, inte intra-komponent. Vår 1oo1-formel ignorerar β.
   Det innebär att vi räknar med felaktig total λDU (summan av alla kanaler).
   Korrekt: dela λDU / n_kanaler och välj rätt yttre arkitektur.
   lDD / 18.3 = antal kanaler: SIF-020/024 → 2 kanaler, SIF-023 → 3 kanaler.

C) 2oo3 SENSOR utan PST (SIF-015/022/025/027 ~−44%, SIF-018 −75%):
   Rotorsak: λDU extraheras korrekt MEN exSILentia lägger till SERH-bidrag
   för transmitters (liknande logikens SERH-tillägg). Behövs ~1.77× λDU för match.
   SIF-018 är extremt (faktor ×4): troligt att sensorn har n_groups > 1 som
   ej extraheras (λDU=595 FIT = 3×~198 FIT om 3-grupp).
   Åtgärd: Svår utan SERH-data. SIF-018 bör kontrolleras manuellt mot rapport.

D) FE 1oo2 λDU=2585.5 FIT (SIF-020-024) – konsekvent −22.8%:
   Rotorsak: PST/EoBT-täckning ~29.5% tillämpas i exSILentia men syns EJ i
   Word-dokumentet (ingen explicit PST-rad för dessa FE). Samma ventiltyp som
   SIF-019 (bekräftad EoBT) → troligt att exSILentia tillämpar EoBT automatiskt
   baserat på "Batches before Proof Test" som ej extraheras korrekt här.
   Alternativt TI = 24 månader (inte 12) ger +1.5% (ej tillräckligt).
   Åtgärd: Lägg till PST_COVERAGE: SIF-020-024 fe: ~0.295 (29.5%).

E) SIF-028/029:
   Rotorsak: Dessa delar troligt referenstotal med SIF-027 i summary-tabellen.
   FE-parameterextraktionen är osäker. Kräver manuell kontroll mot originaldokument.
""")
