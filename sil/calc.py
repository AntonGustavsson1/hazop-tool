"""
SIL PFD Calculator — Markov-baserad beräkningsmotor
IEC 61508 / IEC 61511

Stöder: 1oo1, 1oo2, 1oo2D, 2oo2, 1oo3, 2oo3
Beräknar: PFD_avg, PFH, STR samt HFT/SFF-kontroll
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import numpy as np

T_LIFETIME = 175200.0   # Antagen utrustningslivslängd: 20 år [h] (IEC 61511)

SIL_LIMITS = {
    1: (1e-2, 1e-1),
    2: (1e-3, 1e-2),
    3: (1e-4, 1e-3),
    4: (1e-5, 1e-4),
}


class Architecture(str, Enum):
    OO1   = "1oo1"
    OO2   = "1oo2"
    OO2_D = "1oo2D"
    OO2_2 = "2oo2"
    OO3   = "1oo3"
    OO3_2 = "2oo3"

    def label(self) -> str:
        return {
            "1oo1":  "1oo1  (enkelt)",
            "1oo2":  "1oo2  (2 parallella)",
            "1oo2D": "1oo2D (2 parallella + korsdiag.)",
            "2oo2":  "2oo2  (2 i serie)",
            "1oo3":  "1oo3  (3 parallella)",
            "2oo3":  "2oo3  (majoritet 2 av 3)",
        }.get(self.value, self.value)

    def hft(self) -> int:
        return {
            "1oo1": 0, "1oo2": 1, "1oo2D": 1,
            "2oo2": 0, "1oo3": 2, "2oo3": 1,
        }[self.value]


# ── Indata ────────────────────────────────────────────────────────────────────
@dataclass
class ComponentParams:
    name: str         = ""
    lambda_d: float   = 1e-6    # farlig felfrekvens [1/h]
    dc: float         = 0.0     # diagnostiktäckning [0–1]
    beta: float       = 0.02    # CCF-faktor [0–1]
    ti: float         = 8760.0  # provtestintervall [h]
    mttr: float       = 8.0     # reparationstid detekterade fel [h]
    ptc: float        = 1.0     # provtesttäckning [0–1]
    sff: float        = 0.0     # säker felfraktion [0–1] (för HFT + STR)
    comp_type: str    = "A"     # "A" enkel / "B" komplex (IEC 61508 tab 2/3)
    # Nya fält
    st: float         = 0.0     # self-test intervall [h], 0 = ej modellerat separat
    mission_time: float = 175200.0  # utrustningslivslängd [h] (20 år)
    sc: int           = 0       # SIL Claim Limit / Systematic Capability (0 = ej angiven, 1–4)
    pst_coverage: float = 0.0   # partial stroke test coverage [0–1] (för ventiler)
    pst_interval: float = 720.0 # PST-intervall [h] (standard 1 månad)
    ccf_model: str    = "beta"  # "beta" eller "mooNbeta"
    # FIT-ingång: när > 0 åsidosätter lambda_d*(1-dc) / lambda_d*dc
    lambda_du_fit: float = 0.0  # λDU i FIT (fel per 10⁹ h)
    lambda_dd_fit: float = 0.0  # λDD i FIT (fel per 10⁹ h)
    # Avancerade exida-parametrar (A1–A6 från exSILentia-metodologi)
    ptd: float        = 0.0     # A1: Proof Test Duration [h] — bypasstid vid online-provtest
    pif: float        = 0.0     # A2: Probability of Initial Failure [0–1] — initial haveri vid idriftsättning
    dti: float        = 0.0     # A3: Diagnostic Test Interval [h] — 0=kontinuerlig diagnostik
    ssi: int          = 2       # A6: Site Safety Index [0–4] — 0=svag,2=standard,4=perfekt anläggning
    pt_online: bool   = False   # D1: True = provtest körs med process igång (SIF på bypass)
    useful_life: float = 0.0   # A8: Nyttjoliv [h] — Weibull slitage startar vid denna tid. 0=inaktivt

    @property
    def lambda_du(self) -> float:
        if self.lambda_du_fit > 0:
            return self.lambda_du_fit * 1e-9
        return self.lambda_d * (1.0 - self.dc)

    @property
    def lambda_dd(self) -> float:
        if self.lambda_dd_fit > 0:
            return self.lambda_dd_fit * 1e-9
        return self.lambda_d * self.dc

    @property
    def beta_d(self) -> float:
        """CCF-faktor för detekterade fel, typiskt β/2."""
        return self.beta / 2.0

    @property
    def lambda_s(self) -> float:
        """Säker felfrekvens (ger spurious trips)."""
        if self.sff <= 0.0 or self.sff >= 1.0:
            return 0.0
        return self.lambda_d * self.sff / (1.0 - self.sff)

    @property
    def mu_du(self) -> float:
        """Effektiv reparationsfrekvens för DU-fel med hänsyn till PTC och PST."""
        mt = self.mission_time if self.mission_time > 0 else T_LIFETIME
        if self.pst_coverage > 0 and self.pst_interval > 0:
            pst_part = self.pst_coverage / self.pst_interval
            remaining = 1.0 - self.pst_coverage
            pt_part = remaining * self.ptc / self.ti
            lt_part = remaining * (1.0 - self.ptc) / mt
            return pst_part + pt_part + lt_part
        return self.ptc / self.ti + (1.0 - self.ptc) / mt


@dataclass
class SubsystemParams:
    name: str               = ""
    architecture: Architecture = Architecture.OO1
    component: ComponentParams = field(default_factory=ComponentParams)
    # Nya fält
    channels: list          = field(default_factory=list)  # tom = använd component för alla kanaler
    calc_method: str        = "markov"  # "markov" eller "simplified"


# ── Resultat ──────────────────────────────────────────────────────────────────
@dataclass
class MarkovDetails:
    states: list[str]
    steady_state: list[float]
    pfd_states: list[int]


@dataclass
class SubsystemResult:
    name: str
    architecture: str
    pfd: float
    pfh: float
    str_rate: float       # spurious trip rate [1/h]
    sil_pfd: int          # SIL från PFD
    sil_hft: int          # max SIL från HFT/SFF-restriktioner (0 = ej kontrollerat)
    sil: int              # min(sil_pfd, sil_hft, sil_sc) beroende på vad som är aktivt
    markov: MarkovDetails
    # Nya fält
    sil_sc: int       = 0          # SIL från SIL Claim Limit (0 = ej kontrollerat)
    calc_method: str  = "markov"   # "markov" eller "simplified"


@dataclass
class SIFResult:
    name: str
    sensor: SubsystemResult
    logic: SubsystemResult
    final_element: SubsystemResult
    pfd_total: float
    pfh_total: float
    str_total: float      # total spurious trip rate [1/h]
    mttfs: float          # medelid för falskt utlösning [h]
    sil_achieved: int
    sil_required: int
    passed: bool


# ── Hjälpfunktioner ───────────────────────────────────────────────────────────
def _solve_markov(Q: np.ndarray) -> np.ndarray:
    n = Q.shape[0]
    A = Q.T.copy()
    A[-1, :] = 1.0
    b = np.zeros(n); b[-1] = 1.0
    return np.linalg.solve(A, b)


def _calc_pfh(Q: np.ndarray, pi: np.ndarray, failed: list[int]) -> float:
    """Flöde in i havererade tillstånd = PFH."""
    n = Q.shape[0]
    pfh = 0.0
    for i in range(n):
        if i not in failed:
            for j in failed:
                pfh += float(pi[i]) * Q[i, j]
    return max(pfh, 0.0)


def sil_from_pfd(pfd: float) -> int:
    if pfd < SIL_LIMITS[4][0]:
        return 4
    for sil in (4, 3, 2, 1):
        lo, hi = SIL_LIMITS[sil]
        if lo <= pfd < hi:
            return sil
    return 0


def check_hft_sff(arch: Architecture, sff: float, comp_type: str = "A") -> int:
    """
    Kontrollerar arkitekturrestriktioner per IEC 61508 tabell 2 (typ A)
    och tabell 3 (typ B). Returnerar maximalt tillåtet SIL-krav.
    0 = ej kontrollerat (sff = 0, ingen data).
    """
    if sff <= 0:
        return 0   # SFF ej angivet — hoppa över kontroll
    hft = arch.hft()
    # (sff_min, sff_max) : {hft: max_sil}
    if comp_type == "A":
        table = [
            (0.0,  0.6,  {0: 0, 1: 1, 2: 2}),
            (0.6,  0.9,  {0: 1, 1: 2, 2: 3}),
            (0.9,  0.99, {0: 2, 1: 3, 2: 4}),
            (0.99, 1.01, {0: 3, 1: 4, 2: 4}),
        ]
    else:
        table = [
            (0.0,  0.6,  {0: 0, 1: 0, 2: 1}),
            (0.6,  0.9,  {0: 0, 1: 1, 2: 2}),
            (0.9,  0.99, {0: 1, 1: 2, 2: 3}),
            (0.99, 1.01, {0: 2, 1: 3, 2: 4}),
        ]
    for lo, hi, sil_map in table:
        if lo <= sff < hi:
            return sil_map.get(min(hft, 2), 0)
    return 0


def _calc_str(arch: Architecture, lambda_s: float, mttr: float) -> float:
    """Spurious trip rate per delsystem (IEC 61511 förenklade formler)."""
    if lambda_s <= 0:
        return 0.0
    if arch == Architecture.OO1:
        return lambda_s
    elif arch in (Architecture.OO2, Architecture.OO2_D):
        return 2.0 * lambda_s          # endera kanalen triggar
    elif arch == Architecture.OO2_2:
        return 2.0 * lambda_s**2 * mttr  # båda måste trigga
    elif arch == Architecture.OO3:
        return 3.0 * lambda_s
    elif arch == Architecture.OO3_2:
        return 6.0 * lambda_s**2 * mttr  # valfria 2 av 3
    return lambda_s


# ── Markov-modeller ───────────────────────────────────────────────────────────
def _pfd_1oo1(p: ComponentParams) -> tuple[float, float, MarkovDetails]:
    ldu, ldd = p.lambda_du, p.lambda_dd
    mu_du, mu_dd = p.mu_du, 1.0 / p.mttr
    Q = np.array([
        [-(ldu + ldd),  ldu,   ldd   ],
        [mu_du,        -mu_du, 0.0   ],
        [mu_dd,         0.0,  -mu_dd ],
    ])
    pi = _solve_markov(Q)
    return float(pi[1]), _calc_pfh(Q, pi, [1]), MarkovDetails(
        states=["OK", "DU-fel", "DD-fel"],
        steady_state=pi.tolist(), pfd_states=[1])


def _pfd_1oo2(p: ComponentParams) -> tuple[float, float, MarkovDetails]:
    ldu_i = p.lambda_du * (1.0 - p.beta)
    ldu_c = p.lambda_du * p.beta
    mu = p.mu_du
    Q = np.array([
        [-(2*ldu_i + ldu_c), 2*ldu_i, ldu_c      ],
        [mu,                 -(ldu_i + mu), ldu_i  ],
        [mu,                  0.0,         -mu     ],
    ])
    pi = _solve_markov(Q)
    return float(pi[2]), _calc_pfh(Q, pi, [2]), MarkovDetails(
        states=["Båda OK", "1 DU-fel", "2 DU-fel (SIF-fel)"],
        steady_state=pi.tolist(), pfd_states=[2])


def _pfd_1oo2D(p: ComponentParams) -> tuple[float, float, MarkovDetails]:
    ldu, beta, ti = p.lambda_du, p.beta, p.ti
    pfd = (1 - beta**2) * (ldu * ti)**2 / 3.0 + beta * ldu * ti / 2.0
    ldu_i = ldu * (1.0 - beta)
    ldu_c = ldu * beta
    mu = p.mu_du
    Q = np.array([
        [-(2*ldu_i + ldu_c), 2*ldu_i, ldu_c   ],
        [mu,                 -(ldu_i + mu), ldu_i],
        [mu,                  0.0,         -mu  ],
    ])
    try:
        pi = _solve_markov(Q)
    except np.linalg.LinAlgError:
        pi = np.array([1.0, 0.0, 0.0])
    pfh = _calc_pfh(Q, pi, [2])
    return pfd, pfh, MarkovDetails(
        states=["Båda OK", "1 DU-fel", "2 DU-fel (SIF-fel)"],
        steady_state=pi.tolist(), pfd_states=[2])


def _pfd_2oo2(p: ComponentParams) -> tuple[float, float, MarkovDetails]:
    ldu_i = p.lambda_du * (1.0 - p.beta)
    ldu_c = p.lambda_du * p.beta
    mu = p.mu_du
    Q = np.array([
        [-(2*ldu_i + ldu_c), 2*ldu_i,      ldu_c      ],
        [mu,                 -(ldu_i + mu), ldu_i      ],
        [mu,                  0.0,         -mu         ],
    ])
    pi = _solve_markov(Q)
    pfd = float(pi[1] + pi[2])
    return pfd, _calc_pfh(Q, pi, [1, 2]), MarkovDetails(
        states=["Båda OK", "1 DU-fel (SIF-fel)", "2 DU-fel (SIF-fel)"],
        steady_state=pi.tolist(), pfd_states=[1, 2])


def _pfd_1oo3(p: ComponentParams) -> tuple[float, float, MarkovDetails]:
    ldu_i = p.lambda_du * (1.0 - p.beta)
    ldu_c = p.lambda_du * p.beta
    mu = p.mu_du
    Q = np.array([
        [-(3*ldu_i + ldu_c), 3*ldu_i,       0.0,           ldu_c  ],
        [mu,                 -(2*ldu_i + mu), 2*ldu_i,       0.0   ],
        [mu,                  0.0,           -(ldu_i + mu),  ldu_i ],
        [mu,                  0.0,            0.0,           -mu   ],
    ])
    pi = _solve_markov(Q)
    return float(pi[3]), _calc_pfh(Q, pi, [3]), MarkovDetails(
        states=["Alla OK", "1 DU-fel", "2 DU-fel", "3 DU-fel (SIF-fel)"],
        steady_state=pi.tolist(), pfd_states=[3])


def _pfd_2oo3(p: ComponentParams) -> tuple[float, float, MarkovDetails]:
    ldu_i = p.lambda_du * (1.0 - p.beta)
    ldu_c = p.lambda_du * p.beta
    mu = p.mu_du
    Q = np.array([
        [-(3*ldu_i + ldu_c), 3*ldu_i,        0.0,          ldu_c  ],
        [mu,                 -(2*ldu_i + mu),  2*ldu_i,      0.0   ],
        [mu,                  0.0,           -(ldu_i + mu),  ldu_i ],
        [mu,                  0.0,            0.0,          -mu    ],
    ])
    pi = _solve_markov(Q)
    pfd = float(pi[2] + pi[3])
    return pfd, _calc_pfh(Q, pi, [2, 3]), MarkovDetails(
        states=["Alla OK", "1 DU-fel", "2 DU-fel (SIF-fel)", "3 DU-fel (SIF-fel)"],
        steady_state=pi.tolist(), pfd_states=[2, 3])


_ARCH_FUNCS = {
    Architecture.OO1:   _pfd_1oo1,
    Architecture.OO2:   _pfd_1oo2,
    Architecture.OO2_D: _pfd_1oo2D,
    Architecture.OO2_2: _pfd_2oo2,
    Architecture.OO3:   _pfd_1oo3,
    Architecture.OO3_2: _pfd_2oo3,
}


# ── Förenklade formler (IEC 61511 Annex D) ────────────────────────────────────
def pfd_simplified(arch: Architecture, p: ComponentParams) -> float:
    """
    Förenklade PFD-formler anpassade för att matcha exida exSILentia-metodologi.

    Implementerade förbättringar:
      A1 PTD  — Proof Test Duration: PFDavg += PTD/TI (vid online-provtest)
      A2 PIF  — Probability of Initial Failure: PFD = PIF + (1-PIF)*formel
      A3 DTI  — Diagnostic Test Interval: MTTR_DD_eff = MTTR + DTI/2
      A4      — Separata MTTR_DD och MTTR_DU (MTTR_DU = MTTR som standard)
      A6 SSI  — Site Safety Index: λDU *= SSI_multiplikator
      PST     — Partial Stroke Test: λDU_eff = (1-c_pst)·λDU (exSILentia-metod)

    Formelkärna pfd1(l_du, l_dd) = exida Eq.8:
      λDD_eff·MTTR_DD + CPT·λDU·(TI/2+MTTR) + (1-CPT)·λDU·MT/2

    Arkitekturer:
      1oo1  = pfd1(λDU, λDD)
      1oo2  = βD·λDD·MTTR_DD + [(1-β)·λDU]²·TI²/3 + β·pfd1(λDU, 0)
      2oo2  = (2−β)·pfd1(λDU, λDD)
      2oo3  = βD·λDD·MTTR_DD + 3·[(1-β)·λDU]²·TI²/3 + β·pfd1(λDU, 0)
    """
    ldu  = p.lambda_du
    ldd  = p.lambda_dd
    b    = p.beta
    bd   = p.beta_d
    TI   = p.ti
    MTTR = p.mttr
    MT   = p.mission_time
    CPT  = p.ptc

    # A8: Weibull slitagemodell — ökar λDU efter nyttjoliv (useful_life > 0)
    # Källa: exida_sil_core.txt — shape factor β_w=2.5 (wear-out dominant)
    UL = p.useful_life
    if UL > 0 and MT > UL:
        wear_avg = ((MT / UL) ** 3.5 - 1.0) * UL / (3.5 * (MT - UL))
        fraction_worn = (MT - UL) / MT
        ldu = ldu * (1.0 - fraction_worn + fraction_worn * wear_avg)

    # A6: SSI — justerar λDU för anläggningens säkerhetskultur
    # SSI 0=svag(×2.0), 1=under genomsnitt(×1.5), 2=standard(×1.0), 3=bra(×0.7), 4=perfekt(×0.5)
    _SSI_MULT = {0: 2.0, 1: 1.5, 2: 1.0, 3: 0.7, 4: 0.5}
    ssi_mult = _SSI_MULT.get(int(p.ssi), 1.0)
    if ssi_mult != 1.0:
        ldu = ldu * ssi_mult

    # PST — reklassificerar c_pst av λDU till λDD (exSILentia-metodologi)
    pst_c = p.pst_coverage
    if pst_c > 0:
        ldd = ldd + pst_c * ldu
        ldu = (1.0 - pst_c) * ldu

    # A3: DTI — lägger till diagnostisk scanntid till MTTR_DD
    # MDT_DD = DTI/2 + MRT_DD; om DTI=0 (kontinuerlig diagnostik) ingen extra tid
    mttr_dd = MTTR + (p.dti / 2.0 if p.dti > 0 else 0.0)

    def pfd1(l_du: float, l_dd: float) -> float:
        """Exida Eq.8 — full 1oo1-formel för en enskild kanal."""
        return l_dd * mttr_dd + CPT * l_du * (TI / 2 + MTTR) + (1 - CPT) * l_du * MT / 2

    # ── Arkitekturspecifika formler ────────────────────────────────────────────
    if arch == Architecture.OO1:
        pfd_core = pfd1(ldu, ldd)

    elif arch == Architecture.OO2:  # 1oo2 parallell
        indep    = ((1 - b) * ldu) ** 2 * TI ** 2 / 3
        ccf_du   = b * pfd1(ldu, 0)
        ccf_dd   = bd * ldd * mttr_dd
        pfd_core = ccf_dd + indep + ccf_du

    elif arch == Architecture.OO2_D:  # 1oo2D korsdiagnostik
        indep    = ((1 - b) * ldu) ** 2 * TI ** 2 / 3
        ccf_du   = b ** 2 * pfd1(ldu, 0)
        ccf_dd   = bd ** 2 * ldd * mttr_dd
        pfd_core = ccf_dd + indep + ccf_du

    elif arch == Architecture.OO2_2:  # 2oo2 serie
        pfd_core = (2 - b) * pfd1(ldu, ldd)

    elif arch == Architecture.OO3:  # 1oo3 trippel parallell
        indep    = ((1 - b) * ldu) ** 3 * TI ** 3 / 4
        ccf_du   = b * pfd1(ldu, 0)
        ccf_dd   = bd * ldd * mttr_dd
        pfd_core = ccf_dd + indep + ccf_du

    elif arch == Architecture.OO3_2:  # 2oo3 majoritetsröstning
        indep    = 3 * ((1 - b) * ldu) ** 2 * TI ** 2 / 3
        ccf_du   = b * pfd1(ldu, 0)
        ccf_dd   = bd * ldd * mttr_dd
        pfd_core = ccf_dd + indep + ccf_du

    else:
        pfd_core = pfd1(ldu, ldd)

    # A1: PTD — bypasstid under provtest (exida white paper 2018, §7.5)
    # Gäller ENBART vid online-provtest (pt_online=True): process igång, SIF bypassad.
    # PTD bidrar INTE vid offline-provtest (process nedstängd) — exida white paper sidan 8.
    # Formel: PFDavg += PTD/TI  (konservativ approximation)
    ptd_term = (p.ptd / TI) if (p.pt_online and p.ptd > 0 and TI > 0) else 0.0

    # A2: PIF — sannolikhet att instrumentet var trasigt redan vid driftsättning
    # PFD_total = PIF + (1-PIF)·(PFD_normal + PTD_bidrag)
    pif = p.pif
    if pif > 0:
        return pif + (1.0 - pif) * (pfd_core + ptd_term)
    return pfd_core + ptd_term


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
    }


# ── Heterogena kanalmodeller ──────────────────────────────────────────────────
def _pfd_1oo2_mixed(ch_a: ComponentParams,
                    ch_b: ComponentParams) -> tuple[float, float, MarkovDetails]:
    """
    4-tillstånds Markov för 1oo2 med två olika kanaler.
      State 0: båda OK
      State 1: kanal A DU-fel, B OK
      State 2: kanal A OK, B DU-fel
      State 3: båda DU-fel (SIF-fel)
    """
    ldu_a = ch_a.lambda_du
    ldu_b = ch_b.lambda_du
    mu_a  = ch_a.mu_du
    mu_b  = ch_b.mu_du

    beta_eff = (ch_a.beta + ch_b.beta) / 2.0
    ldu_ccf  = beta_eff * (ldu_a + ldu_b) / 2.0

    ldu_ai = ldu_a * (1.0 - ch_a.beta)
    ldu_bi = ldu_b * (1.0 - ch_b.beta)

    mu3 = max(mu_a, mu_b)  # reparationsfrekvens när båda är felaktiga

    # Tillståndsövergångar (rader = från-tillstånd, kolumner = till-tillstånd)
    # Diagonalen = negativ summa av utgående flöden
    Q = np.array([
        [-(ldu_ai + ldu_bi + ldu_ccf),  ldu_ai,        ldu_bi,        ldu_ccf],
        [mu_a,                          -(mu_a + ldu_bi), ldu_bi,       0.0   ],
        [mu_b,                           ldu_ai,        -(mu_b + ldu_ai), 0.0 ],
        [mu3,                            0.0,            0.0,           -mu3  ],
    ])
    pi = _solve_markov(Q)
    pfd = float(pi[3])
    pfh = _calc_pfh(Q, pi, [3])
    return pfd, pfh, MarkovDetails(
        states=["Båda OK", "A DU-fel", "B DU-fel", "Båda DU-fel (SIF-fel)"],
        steady_state=pi.tolist(), pfd_states=[3])


def _pfd_2oo3_mixed(ch_a: ComponentParams,
                    ch_b: ComponentParams,
                    ch_c: ComponentParams) -> tuple[float, float, MarkovDetails]:
    """
    Approximation för 2oo3 med heterogena kanaler:
    Beräknar geometriskt medel av lambda_DU och genomsnittlig beta,
    sedan normal 2oo3 Markov med dessa värden.
    """
    ldu_geo  = (ch_a.lambda_du * ch_b.lambda_du * ch_c.lambda_du) ** (1.0 / 3.0)
    beta_avg = (ch_a.beta + ch_b.beta + ch_c.beta) / 3.0
    mu_avg   = (ch_a.mu_du + ch_b.mu_du + ch_c.mu_du) / 3.0

    # Bygg ett syntetiskt ComponentParams med de sammanvägda värdena
    p_eff = ComponentParams(
        name="mixed-2oo3",
        lambda_d=ldu_geo,    # vi låter dc=0 så att lambda_du = lambda_d
        dc=0.0,
        beta=beta_avg,
        ti=ch_a.ti,
        mttr=ch_a.mttr,
        ptc=1.0,
        mission_time=ch_a.mission_time,
    )
    # Åsidosätt mu_du-beräkningen genom att patcha ptc/ti
    # (lättast: sätt ptc=1 och ti = 1/mu_avg så att ptc/ti = mu_avg)
    if mu_avg > 0:
        p_eff.ptc = 1.0
        p_eff.ti  = 1.0 / mu_avg

    return _pfd_2oo3(p_eff)


# ── Huvud-API ─────────────────────────────────────────────────────────────────
def calc_subsystem(sub: SubsystemParams) -> SubsystemResult:
    arch = sub.architecture
    p    = sub.component

    # Välj beräkningsväg baserat på heterogena kanaler
    use_mixed = len(sub.channels) > 1

    if use_mixed and arch == Architecture.OO2 and len(sub.channels) >= 2:
        pfd, pfh, details = _pfd_1oo2_mixed(sub.channels[0], sub.channels[1])
        method = "markov"
    elif use_mixed and arch == Architecture.OO3_2 and len(sub.channels) >= 3:
        pfd, pfh, details = _pfd_2oo3_mixed(sub.channels[0], sub.channels[1], sub.channels[2])
        method = "markov"
    else:
        pfd, pfh, details = _ARCH_FUNCS[arch](p)
        method = "markov"

    # Förenklade formler för PFD (PFH och STR alltid via Markov)
    if sub.calc_method == "simplified":
        pfd = pfd_simplified(arch, p)
        method = "simplified"

    sil_pfd = sil_from_pfd(pfd)
    sil_hft = check_hft_sff(arch, p.sff, p.comp_type)

    # SIL Claim Limit-kontroll
    sil_sc = p.sc if p.sc > 0 else 0

    # Kombinera begränsningar: ta minsta aktiverade
    sil = sil_pfd
    if sil_hft > 0:
        sil = min(sil, sil_hft)
    if sil_sc > 0:
        sil = min(sil, sil_sc)

    str_rate = _calc_str(arch, p.lambda_s, p.mttr)

    return SubsystemResult(
        name=sub.name,
        architecture=arch.value,
        pfd=pfd, pfh=pfh, str_rate=str_rate,
        sil_pfd=sil_pfd, sil_hft=sil_hft, sil=sil,
        markov=details,
        sil_sc=sil_sc,
        calc_method=method,
    )


def calc_sif(name: str, sensor: SubsystemParams, logic: SubsystemParams,
             final_element: SubsystemParams, sil_required: int = 2) -> SIFResult:
    r_s  = calc_subsystem(sensor)
    r_l  = calc_subsystem(logic)
    r_fe = calc_subsystem(final_element)

    pfd_total = 1.0 - (1.0 - r_s.pfd) * (1.0 - r_l.pfd) * (1.0 - r_fe.pfd)
    pfh_total = r_s.pfh + r_l.pfh + r_fe.pfh
    str_total = r_s.str_rate + r_l.str_rate + r_fe.str_rate
    mttfs = 1.0 / str_total if str_total > 0 else float("inf")
    sil = sil_from_pfd(pfd_total)

    return SIFResult(
        name=name,
        sensor=r_s, logic=r_l, final_element=r_fe,
        pfd_total=pfd_total, pfh_total=pfh_total,
        str_total=str_total, mttfs=mttfs,
        sil_achieved=sil, sil_required=sil_required,
        passed=(sil >= sil_required),
    )


def validate_component(p: ComponentParams) -> list[str]:
    """Returnerar lista med varningsmeddelanden. Tom lista = OK."""
    w = []
    if not (0 <= p.dc <= 1):
        w.append(f"DC måste vara 0–1 (du angav {p.dc})")
    if not (0 <= p.beta <= 1):
        w.append(f"β måste vara 0–1 (du angav {p.beta})")
    if not (0 <= p.sff <= 1):
        w.append(f"SFF måste vara 0–1 (du angav {p.sff})")
    if not (0 < p.ptc <= 1):
        w.append(f"PTC måste vara 0–1 (du angav {p.ptc})")
    if p.beta > 0.10:
        w.append(f"β={p.beta:.2f} är högt. Typvärde 0.02–0.10")
    if p.dc > 0.99:
        w.append(f"DC={p.dc:.3f} > 0.99 är ovanligt utan specialcertifiering")
    if p.ptc < 0.5:
        w.append(f"PTC={p.ptc:.2f} är lågt. Kontrollera provtestprocedur")
    if p.lambda_d < 1e-10:
        w.append(f"λ_D={p.lambda_d:.2e} är extremt låg. Enhet = 1/h?")
    if p.lambda_d > 1e-3:
        w.append(f"λ_D={p.lambda_d:.2e} är extremt hög. Enhet = 1/h?")
    if p.ti < 100:
        w.append(f"TI={p.ti:.0f} h (<100 h) är ovanligt kort provtestintervall")
    if p.ti > 200000:
        w.append(f"TI={p.ti:.0f} h (>{p.ti/8760:.0f} år) är ovanligt lång")
    if p.mttr < 1:
        w.append(f"MTTR={p.mttr:.1f} h är optimistisk. Typvärde 8–24 h")
    # Nya valideringar
    if p.pst_coverage > 0 and p.pst_interval <= 0:
        w.append("PST-intervall måste vara > 0")
    if p.pst_coverage > 0 and p.pst_interval >= p.ti:
        w.append("PST-intervall bör vara kortare än TI")
    if p.sc > 0 and p.sc > 4:
        w.append(f"SIL Claim Limit > 4 är ogiltigt (du angav sc={p.sc})")
    if p.st > p.ti:
        w.append(f"ST (self-test) är längre än TI — kontrollera (ST={p.st:.0f} h, TI={p.ti:.0f} h)")
    return w


if __name__ == "__main__":
    # ── Grundtest: befintligt scenario ────────────────────────────────────────
    sensor = SubsystemParams("PT-101", Architecture.OO2,
        ComponentParams("Rosemount 3051", 7.5e-7, 0.65, 0.02, 8760, 8, 1.0, 0.70, "B"))
    logic = SubsystemParams("SIS-PLC", Architecture.OO1,
        ComponentParams("ABB AC700F", 2.5e-8, 0.99, 0.02, 8760, 8, 1.0, 0.99, "B"))
    fe = SubsystemParams("XV-101", Architecture.OO1,
        ComponentParams("Fjäderventil NC", 8.0e-6, 0.0, 0.02, 8760, 8, 1.0, 0.10, "A"))

    r = calc_sif("Test SIF", sensor, logic, fe, sil_required=2)
    print(f"\nSIF: {r.name}")
    print(f"  PFD totalt:  {r.pfd_total:.3e}  SIL {r.sil_achieved}  {'OK' if r.passed else 'EJ OK'}")
    print(f"  PFH totalt:  {r.pfh_total:.3e}")
    print(f"  STR totalt:  {r.str_total:.3e} [1/h]  MTTFS={r.mttfs/8760:.1f} år")

    # ── Test: PST-modell ───────────────────────────────────────────────────────
    print("\n--- PST-test ---")
    ventil_pst = ComponentParams(
        "Ventil med PST", lambda_d=8.0e-6, dc=0.0, beta=0.02,
        ti=8760, mttr=8, ptc=1.0, sff=0.10, comp_type="A",
        pst_coverage=0.5, pst_interval=720.0,
    )
    ventil_utan = ComponentParams(
        "Ventil utan PST", lambda_d=8.0e-6, dc=0.0, beta=0.02,
        ti=8760, mttr=8, ptc=1.0, sff=0.10, comp_type="A",
    )
    print(f"  mu_du med PST:    {ventil_pst.mu_du:.4e}")
    print(f"  mu_du utan PST:   {ventil_utan.mu_du:.4e}")

    sub_pst   = SubsystemParams("Ventil PST",  Architecture.OO1, ventil_pst)
    sub_ingen = SubsystemParams("Ventil ingen", Architecture.OO1, ventil_utan)
    res_pst   = calc_subsystem(sub_pst)
    res_ingen = calc_subsystem(sub_ingen)
    print(f"  PFD med PST:      {res_pst.pfd:.3e}")
    print(f"  PFD utan PST:     {res_ingen.pfd:.3e}")

    # ── Test: förenklad beräkning ──────────────────────────────────────────────
    print("\n--- Simplified-test ---")
    sub_simplified = SubsystemParams(
        "Sensor simplified", Architecture.OO2,
        ComponentParams("Sensor", 7.5e-7, 0.65, 0.02, 8760, 8),
        calc_method="simplified",
    )
    res_simp = calc_subsystem(sub_simplified)
    print(f"  PFD (simplified): {res_simp.pfd:.3e}  metod={res_simp.calc_method}")

    # ── Test: heterogena kanaler 1oo2 ─────────────────────────────────────────
    print("\n--- Heterogen 1oo2-test ---")
    ch_a = ComponentParams("Sensor A", lambda_d=1e-6, dc=0.5, beta=0.02, ti=8760)
    ch_b = ComponentParams("Sensor B", lambda_d=5e-7, dc=0.7, beta=0.03, ti=8760)
    sub_mixed = SubsystemParams(
        "Mixad 1oo2", Architecture.OO2,
        ComponentParams(),   # används ej vid mixed
        channels=[ch_a, ch_b],
    )
    res_mixed = calc_subsystem(sub_mixed)
    print(f"  PFD (mixed 1oo2): {res_mixed.pfd:.3e}")

    # ── Test: SIL Claim Limit ──────────────────────────────────────────────────
    print("\n--- SIL Claim Limit-test ---")
    p_sc = ComponentParams("Komp SC2", lambda_d=1e-7, dc=0.9, beta=0.02, ti=8760, sc=2)
    sub_sc = SubsystemParams("SC2 subsystem", Architecture.OO2, p_sc)
    res_sc = calc_subsystem(sub_sc)
    print(f"  SIL PFD={res_sc.sil_pfd}  SIL SC={res_sc.sil_sc}  SIL final={res_sc.sil}")

    # ── Test: validate_component med nya varningar ─────────────────────────────
    print("\n--- Valideringstest (PST/SC/ST) ---")
    p_bad = ComponentParams(
        "Testkomp", lambda_d=1e-6, ti=8760,
        pst_coverage=0.5, pst_interval=9000.0,  # PST >= TI
        sc=5,                                    # SC > 4
        st=10000.0,                              # ST > TI
    )
    warnings = validate_component(p_bad)
    for w in warnings:
        print(f"  VARNING: {w}")
