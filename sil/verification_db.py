"""
verification_db.py — Verifieringsdatabas för ProSa SIL-kalkylator
ProSa Process Safety Consulting AB | IEC 61511 / IEC 61508

Referensvärden från:
  1. SIF-001 (SIL_Verification_SIF-001_20260530.pdf) — fullständig formelverifiering
     med alla 4 arkitekturer per delsystem.
  2. Hybrit Pilot Plant SILver Summary Report (exida exSILentia®, 2022-04-24)
     SIF-001 t.o.m. SIF-015 — output-verifiering (total PFD, subsystem PFD, SIL, RRF).

Formelkällor:
  - exida: "The Key Variables Needed for PFDavg Calculation" (2018)
  - IEC 61508-6 informative simplified equations
  - SIF Verification Report, ProSa (2026-05-30)
  - Hybrit Pilot Plant SILver Summary Report (4-24-2022)
"""

import sqlite3
from pathlib import Path
from typing import Optional

VERIF_DB  = Path(__file__).parent / "verification.db"
TOLERANCE = 0.03   # 3% relativ tolerans mot referensvärden

# ─────────────────────────────────────────────────────────────────────────────
# Referensdata: SIF-001 (från SIL_Verification_SIF-001_20260530.pdf)
# ─────────────────────────────────────────────────────────────────────────────
REFERENCE_CASES = [
    {
        "case_id":          "SIF-001",
        "description":      "SIF-001 — Verifiering PFDavg (2026-05-30)",
        "target_sil":       2,
        "total_pfd_ref":    8.67e-4,
        "sil_achieved_ref": 3,
        "rrf_ref":          1153,
        "subsystems": [
            {
                "subsystem_key":   "sensor",
                "label":           "Sensor",
                "arch":            "2oo2",
                "lambda_du_fit":   57.0,
                "lambda_dd_fit":   17.0,
                "beta":            0.10,
                "beta_d":          0.05,
                "ti":              8760.0,
                "mttr":            8.0,
                "mission_time":    87600.0,
                "cpt":             0.90,
                "pfd_1oo1_ref":    4.74e-4,
                "pfd_1oo2_ref":    2.51e-5,
                "pfd_2oo2_ref":    5.00e-4,
                "pfd_2oo3_ref":    2.54e-5,
                "pfd_selected_ref":5.00e-4,
                "contribution_pct":57.6,
            },
            {
                "subsystem_key":   "logic",
                "label":           "Logic Solver",
                "arch":            "1oo1",
                "lambda_du_fit":   18.0,
                "lambda_dd_fit":   17.0,
                "beta":            0.05,
                "beta_d":          0.025,
                "ti":              8760.0,
                "mttr":            8.0,
                "mission_time":    87600.0,
                "cpt":             0.90,
                "pfd_1oo1_ref":    1.50e-4,
                "pfd_1oo2_ref":    3.96e-6,
                "pfd_2oo2_ref":    1.58e-4,
                "pfd_2oo3_ref":    3.99e-6,
                "pfd_selected_ref":1.50e-4,
                "contribution_pct":17.3,
            },
            {
                "subsystem_key":   "fe",
                "label":           "Final Element",
                "arch":            "1oo1",
                "lambda_du_fit":   26.0,
                "lambda_dd_fit":   57.0,
                "beta":            0.10,
                "beta_d":          0.05,
                "ti":              8760.0,
                "mttr":            24.0,
                "mission_time":    87600.0,
                "cpt":             0.90,
                "pfd_1oo1_ref":    2.18e-4,
                "pfd_1oo2_ref":    1.15e-5,
                "pfd_2oo2_ref":    2.30e-4,
                "pfd_2oo3_ref":    1.15e-5,
                "pfd_selected_ref":2.18e-4,
                "contribution_pct":25.1,
            },
        ],
    }
]


# ─────────────────────────────────────────────────────────────────────────────
# Databasinitiering
# ─────────────────────────────────────────────────────────────────────────────
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(VERIF_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Skapar databas och fyller med referensdata vid första körning."""
    conn = get_connection()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS verif_cases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id         TEXT    NOT NULL UNIQUE,
            description     TEXT,
            target_sil      INTEGER,
            total_pfd_ref   REAL,
            sil_achieved_ref INTEGER,
            rrf_ref         REAL
        );

        CREATE TABLE IF NOT EXISTS verif_subsystems (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id         TEXT    NOT NULL,
            subsystem_key   TEXT    NOT NULL,
            label           TEXT,
            arch            TEXT,
            lambda_du_fit   REAL,
            lambda_dd_fit   REAL,
            beta            REAL,
            beta_d          REAL,
            ti              REAL,
            mttr            REAL,
            mission_time    REAL,
            cpt             REAL,
            pfd_1oo1_ref    REAL,
            pfd_1oo2_ref    REAL,
            pfd_2oo2_ref    REAL,
            pfd_2oo3_ref    REAL,
            pfd_selected_ref REAL,
            contribution_pct REAL,
            UNIQUE(case_id, subsystem_key)
        );
    """)

    # Fyll med referensdata om tabellen är tom
    for case in REFERENCE_CASES:
        c.execute("INSERT OR IGNORE INTO verif_cases "
                  "(case_id,description,target_sil,total_pfd_ref,sil_achieved_ref,rrf_ref) "
                  "VALUES (?,?,?,?,?,?)",
                  (case["case_id"], case["description"], case["target_sil"],
                   case["total_pfd_ref"], case["sil_achieved_ref"], case["rrf_ref"]))
        for sub in case["subsystems"]:
            c.execute(
                "INSERT OR IGNORE INTO verif_subsystems "
                "(case_id,subsystem_key,label,arch,lambda_du_fit,lambda_dd_fit,"
                "beta,beta_d,ti,mttr,mission_time,cpt,"
                "pfd_1oo1_ref,pfd_1oo2_ref,pfd_2oo2_ref,pfd_2oo3_ref,"
                "pfd_selected_ref,contribution_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case["case_id"], sub["subsystem_key"], sub["label"], sub["arch"],
                 sub["lambda_du_fit"], sub["lambda_dd_fit"],
                 sub["beta"], sub["beta_d"], sub["ti"], sub["mttr"],
                 sub["mission_time"], sub["cpt"],
                 sub["pfd_1oo1_ref"], sub["pfd_1oo2_ref"],
                 sub["pfd_2oo2_ref"], sub["pfd_2oo3_ref"],
                 sub["pfd_selected_ref"], sub["contribution_pct"]))

    conn.commit()
    conn.close()


def get_case_ids() -> list:
    conn = get_connection()
    rows = conn.execute("SELECT case_id FROM verif_cases ORDER BY case_id").fetchall()
    conn.close()
    return [r["case_id"] for r in rows]


def get_case(case_id: str) -> Optional[dict]:
    conn = get_connection()
    case_row = conn.execute(
        "SELECT * FROM verif_cases WHERE case_id=?", (case_id,)).fetchone()
    if not case_row:
        conn.close()
        return None
    subs = conn.execute(
        "SELECT * FROM verif_subsystems WHERE case_id=? ORDER BY id",
        (case_id,)).fetchall()
    conn.close()
    result = dict(case_row)
    result["subsystems"] = [dict(s) for s in subs]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Verifieringsfunktion
# ─────────────────────────────────────────────────────────────────────────────
def _rel_diff(calc: float, ref: float) -> float:
    """Relativ avvikelse i procent."""
    if ref == 0:
        return 0.0
    return abs(calc - ref) / abs(ref) * 100.0


def _pass(calc: float, ref: float, tol: float = TOLERANCE) -> bool:
    if ref == 0:
        return calc == 0
    return abs(calc - ref) / abs(ref) <= tol


VerifRow = dict  # {label, calc, ref, diff_pct, ok}


def run_verification(case_id: str,
                     sensor_all: dict, sensor_sel: float,
                     logic_all: dict,  logic_sel: float,
                     fe_all: dict,     fe_sel: float,
                     total_pfd: float,
                     sil_achieved: int) -> list:
    """
    Jämför beräknade PFD-värden mot referensdata för ett givet fall.

    sensor_all / logic_all / fe_all = dict{'1oo1','1oo2','2oo2','2oo3'} från pfd_all_architectures()
    sensor_sel / logic_sel / fe_sel = valt PFD för faktisk arkitektur

    Returnerar lista av rader som kan visas i en tabell.
    """
    init_db()
    case = get_case(case_id)
    if not case:
        return [{"label": f"Referensfall '{case_id}' saknas i databasen",
                 "calc": None, "ref": None, "diff_pct": None, "ok": False}]

    rows = []
    sub_map = {s["subsystem_key"]: s for s in case["subsystems"]}

    calc_map = {
        "sensor": {"all": sensor_all, "sel": sensor_sel},
        "logic":  {"all": logic_all,  "sel": logic_sel},
        "fe":     {"all": fe_all,     "sel": fe_sel},
    }

    arch_keys = ["1oo1", "1oo2", "2oo2", "2oo3"]

    for sk in ["sensor", "logic", "fe"]:
        sub_ref = sub_map.get(sk)
        if not sub_ref:
            continue
        label = sub_ref["label"]
        c_data = calc_map[sk]

        rows.append({"label": f"── {label} ({sub_ref['arch']}) ──",
                     "calc": None, "ref": None, "diff_pct": None, "ok": None})

        for ak in arch_keys:
            ref_key = f"pfd_{ak}_ref"
            ref_val = sub_ref.get(ref_key)
            calc_val = c_data["all"].get(ak)
            if ref_val is None or calc_val is None:
                continue
            diff = _rel_diff(calc_val, ref_val)
            rows.append({
                "label":    f"  PFD {ak}",
                "calc":     calc_val,
                "ref":      ref_val,
                "diff_pct": diff,
                "ok":       _pass(calc_val, ref_val),
            })

        ref_sel = sub_ref.get("pfd_selected_ref")
        sel_diff = _rel_diff(c_data["sel"], ref_sel) if ref_sel else None
        rows.append({
            "label":    f"  PFD vald (={sub_ref['arch']})",
            "calc":     c_data["sel"],
            "ref":      ref_sel,
            "diff_pct": sel_diff,
            "ok":       _pass(c_data["sel"], ref_sel) if ref_sel else None,
        })

    # Totalt
    rows.append({"label": "── TOTALT SIF ──",
                 "calc": None, "ref": None, "diff_pct": None, "ok": None})
    total_ref = case["total_pfd_ref"]
    rrf_calc  = 1.0/total_pfd if total_pfd > 0 else float("inf")
    rows.append({
        "label": "  PFD totalt",
        "calc": total_pfd, "ref": total_ref,
        "diff_pct": _rel_diff(total_pfd, total_ref),
        "ok": _pass(total_pfd, total_ref),
    })
    rows.append({
        "label": "  RRF",
        "calc": round(rrf_calc), "ref": case["rrf_ref"],
        "diff_pct": _rel_diff(rrf_calc, case["rrf_ref"]),
        "ok": _pass(rrf_calc, case["rrf_ref"]),
    })
    rows.append({
        "label": "  SIL uppnått",
        "calc": sil_achieved, "ref": case["sil_achieved_ref"],
        "diff_pct": None,
        "ok": sil_achieved == case["sil_achieved_ref"],
    })

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Hybrit Pilot Plant — SIL-verifiering (exida exSILentia® 2022-04-24)
# Källa: "Hybrit Pilot Plant 4-24-2022 SILver Summary Report.docx"
# Verifierar: total PFD = Σ(delsystem PFD), RRF = 1/PFD, SIL-klassificering
# ─────────────────────────────────────────────────────────────────────────────
HYBRIT_CASES = [
    # case_id, description, target_sil, mission_time_years,
    # total_pfd, rrf, sil, sensor_pfd, logic_pfd, fe_pfd
    ("SIF-001", "Low Pressure (254PI330|L1) nitrogen system, CO2 cleaning",
     1, 15, 1.46e-3, 684,   2, 1.37e-3, 7.31e-6, 8.25e-5),
    ("SIF-002", "Low differential pressure (254PI330/347PI306) Nitrogen system",
     1, 15, 2.69e-3, 371.9, 2, 2.60e-3, 7.31e-6, 8.25e-5),
    ("SIF-003", "High level (347LZ302) in Absorber (347YM100)",
     1, 15, 2.02e-3, 494,   2, 1.05e-3, 6.90e-6, 9.68e-4),
    ("SIF-004", "Low level (347LZ300) in Absorber (347YM100)",
     2, 15, 1.31e-3, 764.8, 2, 1.20e-3, 6.90e-6, 9.79e-5),
    ("SIF-005", "High level (347LS305) filter separator 347SE101 stop 346PU101",
     1, 15, 1.29e-3, 775.8, 2, 1.20e-3, 6.90e-6, 7.93e-5),
    ("SIF-006", "Differential pressure treated gas - sour gas (347XI308)",
     1, 15, 2.70e-3, 369.9, 2, 2.60e-3, 7.31e-6, 9.79e-5),
    ("SIF-007", "High Temperature (347TI338) after lean MEA cooler",
     2, 15, 4.21e-3, 237.2, 2, 3.25e-3, 7.31e-6, 9.66e-4),
    ("SIF-008", "High level (347LZ329 H1) in MEA reboiler (347HE201) close 347RV608",
     1, 15, 2.51e-2, 39.8,  1, 1.05e-3, 6.90e-6, 2.40e-2),
    ("SIF-009", "Low differential pressure 347PD339 Pump discharge and absorber",
     2, 15, 1.54e-3, 650.5, 2, 1.39e-3, 7.31e-6, 1.40e-4),
    ("SIF-011", "Gastryck toppgas (320PZ320-322|H2)",
     2, 10, 2.67e-3, 373.9, 2, 5.04e-5, 2.76e-6, 2.62e-3),
    ("SIF-012", "Gastryck toppgas (320PZ320-322|L2)",
     2, 10, 6.74e-3, 148.3, 2, 4.31e-5, 2.46e-6, 6.69e-3),
    ("SIF-015", "Temp toppgas (320TZ323-325|H2)",
     2, 15, 5.69e-3, 175.6, 2, 1.31e-4, 9.40e-6, 5.55e-3),
]


def init_hybrit_db() -> None:
    """Skapar och fyller Hybrit-referenstabellen."""
    conn = get_connection()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS verif_hybrit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id         TEXT    NOT NULL UNIQUE,
            description     TEXT,
            source          TEXT    DEFAULT 'Hybrit SILver 2022',
            target_sil      INTEGER,
            mission_time_y  REAL,
            total_pfd_ref   REAL,
            rrf_ref         REAL,
            sil_ref         INTEGER,
            sensor_pfd_ref  REAL,
            logic_pfd_ref   REAL,
            fe_pfd_ref      REAL
        );
    """)
    for row in HYBRIT_CASES:
        c.execute(
            "INSERT OR IGNORE INTO verif_hybrit "
            "(case_id,description,target_sil,mission_time_y,total_pfd_ref,rrf_ref,"
            "sil_ref,sensor_pfd_ref,logic_pfd_ref,fe_pfd_ref) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            row)
    conn.commit()
    conn.close()


def get_hybrit_cases() -> list:
    """Returnerar alla Hybrit-referensfall som lista av dict."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM verif_hybrit ORDER BY case_id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_hybrit_consistency_check() -> list:
    """
    Kontrollerar intern konsistens för alla Hybrit-referensfall:
      1. sensor_pfd + logic_pfd + fe_pfd ≈ total_pfd  (additiv modell)
      2. RRF = round(1 / total_pfd)
      3. SIL-klassificering stämmer med total_pfd

    Returnerar lista med VerifRow-dict per kontroll.
    """
    init_hybrit_db()
    cases = get_hybrit_cases()

    SIL_TABLE = {4: (1e-5, 1e-4), 3: (1e-4, 1e-3), 2: (1e-3, 1e-2), 1: (1e-2, 1e-1)}

    def sil_from_pfd(pfd: float) -> int:
        for sil in (4, 3, 2, 1):
            lo, hi = SIL_TABLE[sil]
            if lo <= pfd < hi:
                return sil
        return 0

    rows = []
    all_ok = True

    for case in cases:
        cid = case["case_id"]
        rows.append({"label": f"── {cid}: {case['description'][:55]} ──",
                     "calc": None, "ref": None, "diff_pct": None, "ok": None})

        pfd_s   = case["sensor_pfd_ref"]
        pfd_l   = case["logic_pfd_ref"]
        pfd_fe  = case["fe_pfd_ref"]
        pfd_sum = pfd_s + pfd_l + pfd_fe
        pfd_ref = case["total_pfd_ref"]
        rrf_ref = case["rrf_ref"]
        sil_ref = case["sil_ref"]

        # 1. Sum-check
        diff1 = _rel_diff(pfd_sum, pfd_ref)
        ok1   = _pass(pfd_sum, pfd_ref, tol=0.005)  # 0.5% — rounding i rapport
        if not ok1: all_ok = False
        rows.append({"label": f"  PFD = Σ(sensor+logic+FE)",
                     "calc": pfd_sum, "ref": pfd_ref,
                     "diff_pct": diff1, "ok": ok1})

        # 2. RRF-check
        rrf_calc = round(1.0 / pfd_ref) if pfd_ref > 0 else 0
        ok2 = _pass(float(rrf_calc), rrf_ref, tol=0.01)
        if not ok2: all_ok = False
        rows.append({"label": "  RRF = round(1/PFD)",
                     "calc": float(rrf_calc), "ref": rrf_ref,
                     "diff_pct": _rel_diff(float(rrf_calc), rrf_ref), "ok": ok2})

        # 3. SIL-check
        sil_calc = sil_from_pfd(pfd_ref)
        ok3 = sil_calc == sil_ref
        if not ok3: all_ok = False
        rows.append({"label": "  SIL-klassificering",
                     "calc": float(sil_calc), "ref": float(sil_ref),
                     "diff_pct": None, "ok": ok3})

    rows.append({"label": "── SAMMANFATTNING ──",
                 "calc": None, "ref": None, "diff_pct": None, "ok": None})
    n_ok   = sum(1 for r in rows if r.get("ok") is True)
    n_fail = sum(1 for r in rows if r.get("ok") is False)
    rows.append({"label": f"  {n_ok}/{n_ok+n_fail} kontroller godkänns",
                 "calc": None, "ref": None, "diff_pct": None,
                 "ok": all_ok})
    return rows


def add_custom_case(case_id: str, description: str, target_sil: int,
                    total_pfd_ref: float, sil_achieved_ref: int, rrf_ref: float,
                    subsystems: list) -> None:
    """Lägger till ett anpassat referensfall i databasen."""
    init_db()
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO verif_cases "
              "(case_id,description,target_sil,total_pfd_ref,sil_achieved_ref,rrf_ref) "
              "VALUES (?,?,?,?,?,?)",
              (case_id, description, target_sil, total_pfd_ref, sil_achieved_ref, rrf_ref))
    for sub in subsystems:
        c.execute(
            "INSERT OR REPLACE INTO verif_subsystems "
            "(case_id,subsystem_key,label,arch,lambda_du_fit,lambda_dd_fit,"
            "beta,beta_d,ti,mttr,mission_time,cpt,"
            "pfd_1oo1_ref,pfd_1oo2_ref,pfd_2oo2_ref,pfd_2oo3_ref,"
            "pfd_selected_ref,contribution_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, sub.get("subsystem_key",""), sub.get("label",""),
             sub.get("arch","1oo1"),
             sub.get("lambda_du_fit",0.0), sub.get("lambda_dd_fit",0.0),
             sub.get("beta",0.02), sub.get("beta_d",0.01),
             sub.get("ti",8760.0), sub.get("mttr",8.0),
             sub.get("mission_time",87600.0), sub.get("cpt",1.0),
             sub.get("pfd_1oo1_ref",0.0), sub.get("pfd_1oo2_ref",0.0),
             sub.get("pfd_2oo2_ref",0.0), sub.get("pfd_2oo3_ref",0.0),
             sub.get("pfd_selected_ref",0.0), sub.get("contribution_pct",0.0)))
    conn.commit()
    conn.close()
