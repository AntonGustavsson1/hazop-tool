# -*- coding: utf-8 -*-
"""Uppdaterar berakningsloopen i hybrit_calc.py med korrekt multi-grupp-logik."""

with open('hybrit_calc.py', encoding='utf-8') as f:
    content = f.read()

MARKER_START = "    calc_sub = {}\n    pst_map = PST_COVERAGE.get(sif_id, {})"
MARKER_END   = "                            diverse=is_diverse, beta_used=beta, n_groups=n_groups)"

idx_start = content.find(MARKER_START)
idx_end   = content.find(MARKER_END, idx_start) + len(MARKER_END)

if idx_start < 0:
    print(f"FAILED: hittade inte starten")
elif idx_end < len(MARKER_END):
    print(f"FAILED: hittade inte slutet")
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

        # Manuella overrides (appliceras foerst)
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

        # Multi-grupp korrektion:
        # Om OVERRIDE tillhandahaller lambda_du: antas vara korrekt per-grupp-varde.
        # Annars: dela lambda_du med n_groups och multiplicera PFD med outer_factor.
        n_groups          = s.get('n_groups', 1)
        beta_between      = s.get('beta_between', 0.0)
        beta_within       = s.get('beta', 0.0)
        batches           = s.get('batches_before_pt', 0)
        override_has_ldu  = 'lambda_du' in ovr
        outer_factor      = 1.0

        if n_groups > 1 and not override_has_ldu:
            # Dela med antal grupper for att fa per-grupp-varde
            ldu /= n_groups
            ldd /= n_groups
            # Yttre faktor for NooN-serie med CCF beta_between
            outer_factor = n_groups - (n_groups - 1) * beta_between

        # Anvand within-group beta i formeln
        beta = beta_within

        # PST/EoBT-korrektion
        pst_c = ovr.get('pst_c', pst_map.get(sk, 0.0))

        # Auto-EoBT: om batches > 0 och ingen manuell pst_c och referens finns
        if batches > 0 and pst_c == 0.0 and ldu > 0:
            ref_sub = ref_pfd_map.get(sk, 0)
            if ref_sub > 0:
                # Back-berakna EoBT mot per-grupp PFD-referens
                ref_per_group = ref_sub / outer_factor if outer_factor > 0 else ref_sub
                pst_c = back_calc_eob(ldu, ldd, beta, arch, ti, mttr, MT_h, ptc, ref_per_group)

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

        pfd_per_group = pfd_formula(arch, ldu, ldd, beta, ti, mttr, MT_h, ptc,
                                    ldu2=ldu2_diverse, diverse=is_diverse) if ldu > 0 else 0.0
        pfd_c = outer_factor * pfd_per_group
        calc_sub[sk] = dict(s, pfd_calc=pfd_c, ldd_eff=ldd, pst_c=pst_c,
                            diverse=is_diverse, beta_used=beta, n_groups=n_groups)"""

    content = content[:idx_start] + new_loop + content[idx_end:]
    with open('hybrit_calc.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("OK: loop uppdaterad med korrekt multi-grupp-logik")
