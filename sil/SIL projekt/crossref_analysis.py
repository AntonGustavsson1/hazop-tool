# -*- coding: utf-8 -*-
"""
crossref_analysis.py — Korsreferensanalys: certifikat-λDU vs SILver-rapportvärden.

Försöker dekomponera kända FE λDU-värden från SILver-rapporten i kända
komponentvärden från certifikat-databasen.
Uppdaterar sil_components.db med en component_map-tabell.
"""

import sqlite3, sys, io, itertools

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB_PATH = 'sil_components.db'

# ── Kända SILver-värden (extraherade ur rapport) ──────────────────────────────
# Format: sif_id → {'sensor': λDU_FIT, 'fe': λDU_FIT, 'fe_arch': arch, 'fe_note': str}
SILVER_VALUES = {
    'SIF-001': {
        'sensor_fit': 202.0,   'sensor_note': '1oo1 transmitter',
        'fe_fit':      56.91,  'fe_note':     '1oo2 FE',
    },
    'SIF-003': {
        'sensor_fit': 105.2,   'sensor_note': 'transmitter',
        'fe_fit':      70.41,  'fe_note':     'FE',
    },
    'SIF-007': {
        'sensor_fit': 311.0,   'sensor_note': 'transmitter',
        'fe_fit':      72.42,  'fe_note':     'FE',
    },
    'SIF-008': {
        'sensor_fit': 105.2,   'sensor_note': 'transmitter',
        'fe_fit':    1710.0,   'fe_note':     'FE (Metso VD + GU)',
    },
    'SIF-011': {
        'sensor_fit': None,
        'fe_fit':     516.0,   'fe_note':     '2oo2 FE groups, Group1=285 FIT, Group2=747 FIT (mean=516)',
    },
    'SIF-012': {
        'sensor_fit': None,
        'fe_fit':    5184.0,   'fe_note':     '1oo1 FE. Leg: ParkerN(654)+Rotork(31.5)+Mars(373)=1058 OR ParkerLucifer(993)+Rotork(31.5)+Pekos(1900)=2924.5 (multiple groups sum)',
    },
    'SIF-015': {
        'sensor_fit': None,
        'fe_fit':    5224.0,   'fe_note':     '1oo1 FE. ParkerN(654)+Rotork(31.5)+Pekos(1900) x2 groups + ASC880(13.5) = 2*2585.5+13.5=5184.5',
    },
    'SIF-016': {
        'sensor_fit': None,
        'fe_fit':    1240.5,   'fe_note':     'FE',
    },
    'SIF-017': {
        'sensor_fit': None,
        'fe_fit':    1240.5,   'fe_note':     'FE',
    },
    'SIF-018': {
        'sensor_fit': None,
        'fe_fit':    1240.5,   'fe_note':     'FE',
    },
    'SIF-019': {
        'sensor_fit': None,
        'fe_fit':    2585.5,   'fe_note':     '3x1oo2 groups, per-group-leg value',
    },
    'SIF-020': {
        'sensor_fit': None,
        'fe_fit':    2585.5,   'fe_note':     'FE (same assembly as SIF-019)',
    },
    'SIF-021': {
        'sensor_fit': None,
        'fe_fit':    2585.5,   'fe_note':     'FE (same assembly as SIF-019)',
    },
    'SIF-022': {
        'sensor_fit': None,
        'fe_fit':    2585.5,   'fe_note':     'FE (same assembly as SIF-019)',
    },
    'SIF-023': {
        'sensor_fit': None,
        'fe_fit':    2585.5,   'fe_note':     'FE (same assembly as SIF-019)',
    },
    'SIF-024': {
        'sensor_fit': None,
        'fe_fit':    2585.5,   'fe_note':     'FE (same assembly as SIF-019)',
    },
}

# ── Kända komponentvärden (från certifikat, konsoliderade) ───────────────────
# Dessa är de faktiska certifikat-λDU-värden vi vill matcha mot.
CERT_COMPONENTS = [
    # Transmitters / sensorer
    {'type': 'transmitter', 'mfr': 'E+H', 'model': 'PMP71/75',     'ldu': 20.0,
     'note': 'trycktransmitter, hög DC'},
    {'type': 'transmitter', 'mfr': 'E+H', 'model': 'PMP51/55',     'ldu': 114.0,
     'note': 'trycktransmitter'},
    {'type': 'transmitter', 'mfr': 'E+H', 'model': 'TMT82',        'ldu': 20.0,
     'note': 'temperaturtransmitter'},
    {'type': 'transmitter', 'mfr': 'E+H', 'model': 'PMD75/FMD77',  'ldu': 20.0,
     'note': 'differenstryck/nivå'},
    {'type': 'signal_cond', 'mfr': 'E+H', 'model': 'RB223',        'ldu': 47.0,
     'note': 'signalomvandlare/barriär'},
    {'type': 'signal_cond', 'mfr': 'E+H', 'model': 'SD006',        'ldu': 107.0,
     'note': 'signalomvandlare'},
    {'type': 'transmitter', 'mfr': 'E+H', 'model': 'RN221N',       'ldu': 66.0,
     'note': 'räknare/frekv.omvandlare'},
    # Ventiler
    {'type': 'valve',       'mfr': 'Metso', 'model': '5/9000 no PST',  'ldu': 24.2,
     'note': 'kulventil utan PST (ldu=5.57+18.6=24.17)'},
    {'type': 'valve',       'mfr': 'Metso', 'model': '5/9000 with PST','ldu': 5.57,
     'note': 'kulventil med PST (lambda_du reducerad)'},
    {'type': 'valve',       'mfr': 'Metso', 'model': 'VD',             'ldu': 560.0,
     'note': 'styrventil'},
    {'type': 'valve',       'mfr': 'Metso', 'model': 'Globe GU',       'ldu': 1150.0,
     'note': 'globventil'},
    {'type': 'valve',       'mfr': 'Pekos', 'model': 'ball valve',     'ldu': 492.0,
     'note': 'kulventil'},
    {'type': 'valve',       'mfr': 'Worcester', 'model': '3-piece',    'ldu': 468.0,
     'note': '3-piece kulventil'},
    {'type': 'valve',       'mfr': 'Mars',  'model': 'Q09 ball valve', 'ldu': 373.0,
     'note': '2-way kulventil'},
    # Ställdon (aktuatorer)
    {'type': 'actuator',    'mfr': 'Metso', 'model': 'B1J/B1C',        'ldu': 4.08,
     'note': 'pneumatiskt cylinderdon, utan PST ldu=4.08'},
    {'type': 'actuator',    'mfr': 'Metso', 'model': 'B1J (with PST)', 'ldu': 4.1,
     'note': 'pneumatiskt cylinderdon med PST (DC=83%)'},
    # Solenoidventiler
    {'type': 'solenoid',    'mfr': 'Parker', 'model': '0802-28',        'ldu': 131.0,
     'note': 'solenoidventil'},
    {'type': 'solenoid',    'mfr': 'Parker', 'model': 'N-series',       'ldu': 654.0,
     'note': 'Parker N solenoid (extraherat ur SILver-rapport: 6.54E-07 /hr = 654 FIT)'},
    {'type': 'solenoid',    'mfr': 'Parker', 'model': 'Lucifer N-series','ldu': 993.0,
     'note': 'Parker Lucifer N-series (extraherat ur SILver-rapport: 9.93E-07 /hr = 993 FIT)'},
    {'type': 'solenoid',    'mfr': 'Norgren', 'model': '2401x',         'ldu': 0.0228,
     'note': 'solenoidventil (ldu i FIT = 0.0228, osäkert)'},
    # Ställdon ur rapport (ej certifikat, men bekräftade värden)
    {'type': 'actuator',    'mfr': 'Rotork', 'model': '(generic)',      'ldu': 31.5,
     'note': 'Rotork ställdon (extraherat ur SILver-rapport: 3.15E-08 /hr = 31.5 FIT)'},
    # Logic solver
    {'type': 'logic',       'mfr': 'ABB',  'model': 'AC800M AI880A',   'ldu': 0.698,
     'note': 'analog ingångsmodul (SERH-korrigerad)'},
    {'type': 'logic',       'mfr': 'ABB',  'model': 'AC800M DI880',    'ldu': 0.659,
     'note': 'digital ingångsmodul (SERH-korrigerad)'},
]

# Hjälplookup: identifiera komponentmodell per typ för snabb access
VALVES    = [c for c in CERT_COMPONENTS if c['type'] == 'valve']
ACTUATORS = [c for c in CERT_COMPONENTS if c['type'] == 'actuator']
SOLENOIDS = [c for c in CERT_COMPONENTS if c['type'] == 'solenoid']
TRANSMITTERS = [c for c in CERT_COMPONENTS if c['type'] in ('transmitter', 'signal_cond')]


def match_sensor(sensor_fit):
    """Försök matcha sensor λDU mot kända transmitter-modeller (+/- 20%)."""
    if sensor_fit is None:
        return []
    results = []
    tol = 0.20
    # Direkt match: enkel transmitter
    for t in TRANSMITTERS:
        if t['ldu'] <= 0:
            continue
        ratio = abs(sensor_fit - t['ldu']) / t['ldu']
        if ratio <= tol:
            results.append({
                'match_type': 'direct',
                'components': [t],
                'total_fit': t['ldu'],
                'error_pct': (sensor_fit - t['ldu']) / t['ldu'] * 100,
            })
    # Transmitter + RB223 barriär
    for t in [c for c in TRANSMITTERS if c['type'] == 'transmitter']:
        for s in [c for c in TRANSMITTERS if c['type'] == 'signal_cond']:
            combo = t['ldu'] + s['ldu']
            if combo <= 0:
                continue
            ratio = abs(sensor_fit - combo) / combo
            if ratio <= tol:
                results.append({
                    'match_type': 'transmitter+signal_cond',
                    'components': [t, s],
                    'total_fit': combo,
                    'error_pct': (sensor_fit - combo) / combo * 100,
                })
    results.sort(key=lambda x: abs(x['error_pct']))
    return results[:3]  # Returnera de 3 bästa matcherna


def match_fe(fe_fit, tol=0.20):
    """Försök dekomponera FE λDU i ventil + ställdon + solenoid(er).

    Testar kombinationer:
      a) valve alone
      b) valve + actuator
      c) valve + actuator + 1 solenoid
      d) valve + actuator + 2 solenoids (1oo2-konfiguration: varje ben har sin solenoid)
    """
    if fe_fit is None:
        return []

    results = []

    def add_if_match(components, combo_fit, combo_label):
        if combo_fit <= 0:
            return
        ratio = abs(fe_fit - combo_fit) / max(fe_fit, combo_fit)
        if ratio <= tol:
            results.append({
                'match_type': combo_label,
                'components': components,
                'total_fit': combo_fit,
                'error_pct': (fe_fit - combo_fit) / combo_fit * 100,
            })

    # a) Ventil ensam
    for v in VALVES:
        add_if_match([v], v['ldu'], 'valve_only')

    # b) Ventil + ställdon
    for v in VALVES:
        for a in ACTUATORS:
            add_if_match([v, a], v['ldu'] + a['ldu'], 'valve+act')

    # c) Ventil + ställdon + 1 solenoid
    for v in VALVES:
        for a in ACTUATORS:
            for sol in SOLENOIDS:
                if sol['ldu'] <= 0:
                    continue
                add_if_match([v, a, sol], v['ldu'] + a['ldu'] + sol['ldu'],
                             'valve+act+1sol')

    # d) Ventil + ställdon + 2 solenoidventiler (1oo2 config, oberoende ben)
    for v in VALVES:
        for a in ACTUATORS:
            for sol in SOLENOIDS:
                if sol['ldu'] <= 0:
                    continue
                add_if_match([v, a, sol], v['ldu'] + a['ldu'] + 2 * sol['ldu'],
                             'valve+act+2sol(1oo2)')

    # e) Ventil + 1 solenoid (inget separat ställdon i certifikat)
    for v in VALVES:
        for sol in SOLENOIDS:
            if sol['ldu'] <= 0:
                continue
            add_if_match([v, sol], v['ldu'] + sol['ldu'], 'valve+1sol')

    # f) Ventil + 2 solenoidventiler
    for v in VALVES:
        for sol in SOLENOIDS:
            if sol['ldu'] <= 0:
                continue
            add_if_match([v, sol], v['ldu'] + 2 * sol['ldu'], 'valve+2sol(1oo2)')

    results.sort(key=lambda x: abs(x['error_pct']))
    return results[:5]  # Returnera de 5 bästa matcherna


def build_component_map(conn):
    """Bygg component_map-tabell och fyll den med analysen."""
    c = conn.cursor()
    c.execute('DROP TABLE IF EXISTS component_map')
    c.execute('''
        CREATE TABLE component_map (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sif_id        TEXT,
            subsystem     TEXT,  -- 'sensor' | 'fe' | 'logic'
            component_type TEXT, -- 'valve' | 'solenoid' | 'transmitter' | 'actuator' | 'signal_cond'
            manufacturer  TEXT,
            model         TEXT,
            lambda_du_fit REAL,
            quantity      INTEGER,
            notes         TEXT
        )
    ''')

    rows_to_insert = []

    for sif_id, sv in SILVER_VALUES.items():
        # Sensor
        sensor_fit = sv.get('sensor_fit')
        if sensor_fit is not None:
            matches = match_sensor(sensor_fit)
            if matches:
                best = matches[0]
                for comp in best['components']:
                    rows_to_insert.append((
                        sif_id, 'sensor', comp['type'],
                        comp['mfr'], comp['model'], comp['ldu'],
                        1,
                        f"best_match={best['match_type']} err={best['error_pct']:+.1f}% "
                        f"(SILver={sensor_fit} FIT, cert={best['total_fit']} FIT)"
                    ))
            else:
                rows_to_insert.append((
                    sif_id, 'sensor', 'transmitter',
                    'unknown', 'unmatched', sensor_fit,
                    1,
                    f"no cert match within 20% (SILver={sensor_fit} FIT)"
                ))

        # FE
        fe_fit = sv.get('fe_fit')
        if fe_fit is not None:
            matches = match_fe(fe_fit)
            if matches:
                best = matches[0]
                comp_qty = {}
                for comp in best['components']:
                    key = (comp['mfr'], comp['model'], comp['type'])
                    comp_qty[key] = comp_qty.get(key, {'comp': comp, 'qty': 0})
                    comp_qty[key]['qty'] += 1
                for (mfr, model, ctype), v2 in comp_qty.items():
                    comp = v2['comp']
                    rows_to_insert.append((
                        sif_id, 'fe', ctype,
                        mfr, model, comp['ldu'],
                        v2['qty'],
                        f"best_match={best['match_type']} err={best['error_pct']:+.1f}% "
                        f"(SILver={fe_fit} FIT, cert_total={best['total_fit']} FIT)"
                    ))
            else:
                rows_to_insert.append((
                    sif_id, 'fe', 'valve',
                    'unknown', 'unmatched', fe_fit,
                    1,
                    f"no cert match within 20% (SILver={fe_fit} FIT)"
                ))

    c.executemany(
        'INSERT INTO component_map (sif_id, subsystem, component_type, manufacturer, model, lambda_du_fit, quantity, notes) VALUES (?,?,?,?,?,?,?,?)',
        rows_to_insert
    )
    conn.commit()
    return len(rows_to_insert)


def print_separator(char='-', width=90):
    print(char * width)


def print_analysis():
    conn = sqlite3.connect(DB_PATH)
    n_inserted = build_component_map(conn)

    print()
    print_separator('=')
    print('  KORSREFERENSANALYS: Certifikat-λDU vs SILver-rapport')
    print_separator('=')
    print()

    # ── Sensormatchning ──────────────────────────────────────────────────────
    print('SENSOR-MATCHNING')
    print_separator()
    print(f"{'SIF':<10} {'SILver λDU':>11} {'Bästa match':>13} {'Fel%':>7}  Komponenter")
    print_separator()

    sensor_sifs = {k: v for k, v in SILVER_VALUES.items() if v.get('sensor_fit') is not None}
    for sif_id in sorted(sensor_sifs.keys()):
        sv = SILVER_VALUES[sif_id]
        sensor_fit = sv['sensor_fit']
        matches = match_sensor(sensor_fit)
        if matches:
            best = matches[0]
            comp_str = ' + '.join(f"{c['mfr']} {c['model']} ({c['ldu']} FIT)" for c in best['components'])
            print(f"  {sif_id:<8} {sensor_fit:>9.1f} FIT  {best['total_fit']:>9.2f} FIT  "
                  f"{best['error_pct']:>+6.1f}%  [{best['match_type']}] {comp_str}")
        else:
            print(f"  {sif_id:<8} {sensor_fit:>9.1f} FIT  {'INGEN MATCH':>13}         (tolerans ±20%)")

    print()

    # ── FE-dekompositon ──────────────────────────────────────────────────────
    print('FINAL ELEMENT DEKOMPOSITION')
    print_separator()

    fe_sifs = {k: v for k, v in SILVER_VALUES.items() if v.get('fe_fit') is not None}
    for sif_id in sorted(fe_sifs.keys()):
        sv = SILVER_VALUES[sif_id]
        fe_fit = sv['fe_fit']
        fe_note = sv.get('fe_note', '')
        matches = match_fe(fe_fit)
        print(f"  {sif_id}  SILver λDU = {fe_fit} FIT  [{fe_note}]")
        if matches:
            for i, m in enumerate(matches):
                comp_str = ' + '.join(
                    f"{c['mfr']} {c['model']} ({c['ldu']} FIT)" for c in m['components'])
                prefix = '  => BAST' if i == 0 else '     Alt.'
                print(f"    {prefix} [{m['match_type']}]  total={m['total_fit']:.2f} FIT  "
                      f"err={m['error_pct']:+.1f}%")
                print(f"           Komponenter: {comp_str}")
        else:
            print('    Ingen match inom ±20% hittad.')
        print()

    # ── Sammanfattningstabll ─────────────────────────────────────────────────
    print_separator('=')
    print('KOMPONENTKARTA (component_map) — lagrad i sil_components.db')
    print_separator('=')
    c = conn.cursor()
    c.execute('''SELECT sif_id, subsystem, component_type, manufacturer, model,
                        lambda_du_fit, quantity, notes
                 FROM component_map ORDER BY sif_id, subsystem''')
    rows = c.fetchall()
    print(f"{'SIF':<10} {'Sub':<7} {'Typ':<13} {'Tillverkare':<14} {'Modell':<22} "
          f"{'λDU':>8} {'Qty':>4}  Anteckningar")
    print_separator()
    current_sif = None
    for row in rows:
        sif_id, sub, ctype, mfr, model, ldu, qty, notes = row
        if sif_id != current_sif:
            if current_sif is not None:
                print()
            current_sif = sif_id
        note_short = notes[:60] if notes else ''
        print(f"  {sif_id:<8} {sub:<7} {ctype:<13} {mfr:<14} {model:<22} "
              f"{ldu:>8.2f} {qty:>4}  {note_short}")

    print()
    print(f"  {n_inserted} rader infogade i component_map-tabellen.")
    print()

    # ── Ej matchade FE-värden ─────────────────────────────────────────────────
    print_separator('=')
    print('ANALYS AV EJ MATCHADE FE-VÄRDEN')
    print_separator('=')
    for sif_id in sorted(fe_sifs.keys()):
        fe_fit = SILVER_VALUES[sif_id]['fe_fit']
        matches = match_fe(fe_fit)
        if not matches:
            print(f"  {sif_id}: {fe_fit} FIT — kräver manuell analys.")
            # Visa närmaste möjliga kombinationer
            best_delta = None
            best_label = None
            for v in VALVES:
                for sol in SOLENOIDS:
                    if sol['ldu'] <= 0:
                        continue
                    for n_sol in [1, 2]:
                        combo = v['ldu'] + n_sol * sol['ldu']
                        delta = abs(fe_fit - combo) / max(fe_fit, combo)
                        if best_delta is None or delta < best_delta:
                            best_delta = delta
                            best_label = (f"{v['mfr']} {v['model']} ({v['ldu']}) + "
                                         f"{n_sol}x {sol['mfr']} {sol['model']} ({sol['ldu']}) = {combo:.1f} FIT")
            if best_label:
                print(f"       Närmaste: {best_label}  (err={best_delta*100:+.1f}%)")

    print()
    print('NOTERINGAR:')
    print('  * Tolerans ±20% för komponentmatchning.')
    print('  * FE = valve + actuator + solenoid(er) (certifikat-λDU summeras).')
    print('  * 1oo2-solenoidkonfig: varje ben har sin solenoid → 2x solenoid-λDU.')
    print('  * Sensor = transmitter (+ signalomvandlare om barriär ingår).')
    print('  * Certifikat-värden: E+H PMP71/75=20, PMP51/55=114, TMT82=20,')
    print('    RB223=47, SD006=107, Metso 5/9000(no PST)=24.2, B1=4.08,')
    print('    Parker 0802-28=131, Pekos=492, Worcester 3-piece=468, VD=560, GU=1150.')
    print()

    conn.close()


if __name__ == '__main__':
    print_analysis()
