# -*- coding: utf-8 -*-
with open('hybrit_calc.py', encoding='utf-8') as f:
    content = f.read()

# Ersatt berakningsloopen
MARKER_START = "    calc_sub = {}\n    pst_map = PST_COVERAGE.get(sif_id, {})"
MARKER_END   = "                            diverse=is_diverse)"

idx_start = content.find(MARKER_START)
idx_end   = content.find(MARKER_END, idx_start) + len(MARKER_END)

if idx_start < 0 or idx_end < len(MARKER_END):
    print(f"FAILED: start={idx_start}, end={idx_end}")
else:
    new_loop = """    calc_sub = {}
    pst_map = PST_COVERAGE.get(sif_id, {})
    ovr_map = OVERRIDES.get(sif_id, {})
    ref_pfd_map = {
        'sensor': HYBRIT_REF[sif_id][2] if sif_id in HYBRIT_REF and len(HYBRIT_REF[sif_id]) > 2 else 0,
        'logic':  HYBRIT_REF[sif_id][3] if sif_id in HYBRIT_REF and len(HYBRIT_REF[sif_id]) > 3 else 0,
        'fe':     HYBRIT_REF[sif_id][4] if sif_id in HYBRIT_REF and len(HYBRIT_REF[sif_id]) > 4 else 0,
    }

    for sk in ('sensor', 'logic', 'fe'):
        s = dict(subs[sk])

        # Manuella overrides
        ovr = ovr_map.get(sk, {})
        if ovr:
            for k, v in ovr.items():
                if k not in ('pst_c',):
                    s[k] = v

        ldu  = s['lambda_du']
        ldd  = 0.0 if sk == 'logic' else s['lambda_dd']
        arch = s['arch']
        ti   = s['ti']
        mttr = s['mttr']
        ptc  = s['ptc']

        # Multi-grupp korrektion (sensor + FE):
        # Extraktorn summerar ldu fran ALLA grupper -> dela med n_groups
        n_groups     = s.get('n_groups', 1)
        beta_between = s.get('beta_between', 0.0)
        beta_within  = s.get('beta', 0.0)
        batches      = s.get('batches_before_pt', 0)

        if n_groups > 1:
            ldu = ldu / n_groups
            ldd = ldd / n_groups
            # Effektiv beta: ytterstruktur (NooN-serie) * inre voting
            beta = (n_groups - (n_groups - 1) * beta_between) * beta_within
        else:
            beta = beta_within

        # PST/EoBT-korrektion
        pst_c = ovr.get('pst_c', pst_map.get(sk, 0.0))

        # Auto-EoBT: om batches > 0 och ingen manuell pst_c
        if batches > 0 and pst_c == 0.0 and ldu > 0:
            ref_sub = ref_pfd_map.get(sk, 0)
            if ref_sub > 0:
                pst_c = back_calc_eob(ldu, ldd, beta, arch, ti, mttr, MT_h, ptc, ref_sub)

        if pst_c > 0:
            ldd = ldd + pst_c * ldu
            ldu = (1.0 - pst_c) * ldu

        # Diverse 1oo2
        ldu2_diverse = None
        is_diverse = (s.get('voting_type', '').lower() == 'diverse' and arch.lower() == '1oo2')
        if is_diverse and sk == 'fe':
            ldu2_diverse = ovr.get('lambda_du2', None)
            if ldu2_diverse is None:
                ldu2_diverse = ldu

        pfd_c = pfd_formula(arch, ldu, ldd, beta, ti, mttr, MT_h, ptc,
                            ldu2=ldu2_diverse, diverse=is_diverse) if ldu > 0 else 0.0
        calc_sub[sk] = dict(s, pfd_calc=pfd_c, ldd_eff=ldd, pst_c=pst_c,
                            diverse=is_diverse, beta_used=beta, n_groups=n_groups)"""

    content = content[:idx_start] + new_loop + content[idx_end:]
    with open('hybrit_calc.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("OK: berakningsloopen uppdaterad")
