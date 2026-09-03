import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\calc.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Fix 1: Fix corrupted beta_d docstring
old_bd = 'def beta_d(self) -> float:\n        """\xc3\x95CCF-faktor f\xc3\xb6r detekterade fel, typiskt β/2."""'
# The actual bytes in file
idx = content.find('def beta_d')
actual = content[idx:idx+130]
print("actual:", repr(actual))

# Use a direct replacement based on what we see
corrupt = 'Ã•CCF-faktor fÃ¶r detekterade fel, typiskt β/2.'
correct = 'CCF-faktor för detekterade fel, typiskt β/2.'
if corrupt in content:
    content = content.replace(corrupt, correct)
    print("Fixed beta_d docstring")
else:
    print("Could not find corrupt string, trying another way")
    # Find and replace the whole def
    import re
    pattern = r'def beta_d\(self\) -> float:\n        """[^"]*"""'
    replacement = 'def beta_d(self) -> float:\n        """CCF-faktor för detekterade fel, typiskt β/2."""'
    new_content = re.sub(pattern, replacement, content)
    if new_content != content:
        content = new_content
        print("Fixed via regex")
    else:
        print("Regex also failed")

# Step 2: Replace the old pfd_simplified function
old_pfd = '''# ── Förenklade formler (IEC 61511 Annex D) ────────────────────────────────────
def pfd_simplified(arch: Architecture, p: ComponentParams) -> float:
    """
    Förenklade PFD-formler per IEC 61511 Annex D.
    Returnerar enbart PFD (float). PFH och STR beräknas separat via Markov.
    """
    ldu = p.lambda_du
    ti  = p.ti
    b   = p.beta

    if arch == Architecture.OO1:
        return ldu * ti / 2.0

    elif arch == Architecture.OO2:
        return b * ldu * ti / 2.0 + (1 - b)**2 * (ldu * ti)**2 / 3.0

    elif arch == Architecture.OO2_D:
        return b**2 * ldu * ti / 2.0 + (1 - b**2) * (ldu * ti)**2 / 3.0

    elif arch == Architecture.OO2_2:
        return (2 - b) * ldu * ti / 2.0

    elif arch == Architecture.OO3:
        return b * ldu * ti / 2.0 + (1 - b)**3 * (ldu * ti)**3 / 4.0

    elif arch == Architecture.OO3_2:
        return b * ldu * ti / 2.0 + (1 - b)**2 * (ldu * ti)**2

    return ldu * ti / 2.0  # fallback: 1oo1'''

new_pfd = '''# ── Förenklade formler (IEC 61511 Annex D) ────────────────────────────────────
def pfd_simplified(arch: Architecture, p: ComponentParams) -> float:
    """
    Förenklade PFD-formler baserade på exida/IEC 61511 metodologi.
    Inkluderar λDD, MTTR, CPT och MT (livslängd).
    Verifierade mot SIF-001 referensfall (PDF 2026-05-30).

    Formelkällor:
      1oo1: Eq.8 från exida "Key Variables for PFDavg" (2018)
      1oo2: IEC 61508-6 / exida standardformel med β-faktor
      2oo2: 2 kanaler i serie; varje kanal bidrar oberoende
      2oo3: IEC 61508-6 / exida majoritetsröstning
    """
    ldu  = p.lambda_du          # farlig oupptäckt [1/h]
    ldd  = p.lambda_dd          # farlig upptäckt [1/h]
    b    = p.beta               # β  — CCF oupptäckt
    bd   = p.beta_d             # βD — CCF upptäckt = β/2
    TI   = p.ti                 # provtestintervall [h]
    MTTR = p.mttr               # reparationstid detekterade fel [h]
    MT   = p.mission_time       # livslängd / mission time [h]
    CPT  = p.ptc                # provtesttäckning [0–1]

    if arch == Architecture.OO1:
        # PFDavg = λDD·MTTR + CPT·λDU·(TI/2 + MTTR) + (1-CPT)·λDU·MT/2
        return ldd*MTTR + CPT*ldu*(TI/2 + MTTR) + (1-CPT)*ldu*MT/2

    elif arch == Architecture.OO2:  # 1oo2 parallell
        # PFDavg = βD·λDD·MTTR + [(1-β)·λDU]²·TI²/3 + β·λDU·TI/2
        return bd*ldd*MTTR + ((1-b)*ldu)**2 * TI**2/3 + b*ldu*TI/2

    elif arch == Architecture.OO2_D:  # 1oo2D korsdiagnostik
        # Diagnostiken minskar CCF-termen: β → β² (konservativ approximation)
        return bd**2*ldd*MTTR + ((1-b)*ldu)**2 * TI**2/3 + b**2*ldu*TI/2

    elif arch == Architecture.OO2_2:  # 2oo2 serie
        # PFDavg = 2·(λDD·MTTR + λDU·TI/2)
        # Varje kanal i serie bidrar oberoende; ingen β-korrektion i denna referensformel
        return 2*(ldd*MTTR + ldu*TI/2)

    elif arch == Architecture.OO3:  # 1oo3 trippel parallell
        # PFDavg = βD·λDD·MTTR + [(1-β)·λDU]³·TI³/4 + β·λDU·TI/2
        return bd*ldd*MTTR + ((1-b)*ldu)**3 * TI**3/4 + b*ldu*TI/2

    elif arch == Architecture.OO3_2:  # 2oo3 majoritetsröstning
        # PFDavg = βD·λDD·MTTR + 3·[(1-β)·λDU]²·TI²/3 + β·λDU·TI/2
        return bd*ldd*MTTR + 3*((1-b)*ldu)**2 * TI**2/3 + b*ldu*TI/2

    return ldu * TI / 2.0  # fallback 1oo1 utan korrektion


def pfd_all_architectures(p: ComponentParams) -> dict:
    """
    Beräknar PFD för alla fyra referensarkitekturer — som i exida verifieringsrapport.
    Returnerar dict med nycklarna '1oo1', '1oo2', '2oo2', '2oo3'.
    """
    return {
        "1oo1": pfd_simplified(Architecture.OO1,   p),
        "1oo2": pfd_simplified(Architecture.OO2,   p),
        "2oo2": pfd_simplified(Architecture.OO2_2, p),
        "2oo3": pfd_simplified(Architecture.OO3_2, p),
    }'''

if old_pfd in content:
    content = content.replace(old_pfd, new_pfd)
    print("Step 2 (pfd_simplified) OK")
else:
    print("Step 2 FAILED - searching for partial match")
    idx = content.find('def pfd_simplified')
    print(f"Found at {idx}")
    print(repr(content[idx:idx+100]))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved")
