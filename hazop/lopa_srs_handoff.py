"""
LOPA / SRS module — handoff spec for integration into the HAZOP program.
=========================================================================

Purpose
-------
This file is a structural + business-logic spec (not a standalone app) for
implementing two new tabs in the existing HAZOP program:

  1. LOPA  (Layer Of Protection Analysis / Skyddsbarriäranalys)
  2. SRS   (Safety Requirement Specification / Säkerhetskravspecifikation)

Implement LOPA first; SRS depends on it (see "SRS depends on LOPA" below)
and can be added as a second pass.

Integration model
------------------
- The user right-clicks a barrier/safeguard cell in a HAZOP node/cause-row
  and selects "Koppla till LOPA" ("Link to LOPA"). This should:
    a) create a new LopaRecord (or attach to an existing one for that SIF),
    b) push a BarrierRow into `LopaRecord.rows` pre-filled with:
         - orsak       <- the HAZOP cause text
         - hazop_ref   <- a stable reference to the HAZOP node/cause,
                          e.g. "(28.4.1)" (node.cause numbering used in the
                          existing HAZOP export — reuse whatever id scheme
                          the HAZOP module already has)
    c) if the HAZOP row has one or more consequences of type S/M/E tagged,
       push matching HazopConsequence entries into
       `LopaRecord.hazop_consequences` (see below) so they show up,
       checkable, in the LOPA "Scenario" card.
- A HAZOP node can link to >1 LOPA (one per independent SIF); a LOPA can
  pull consequences from >1 HAZOP row.
- Store the originating HAZOP ids (node id, cause id, consequence id) on
  each linked object so re-sync / "jump to HAZOP" navigation is possible
  later — fields are provided below (`hazop_node_id`, `hazop_cause_id`,
  `hazop_consequence_id`).

Tab 1 — LOPA
------------
One LOPA record = one safety instrumented function (SIF), independent of
revision. Each record carries a small set of named revisions (default
"00"); editing always happens on the currently selected revision, and a
new revision starts as a copy of the previous one (see `Revision`).

Sections, top to bottom:
  - Header: LOPA-nr/SIF-nr (`dok`), SIF beteckning/namn (`sif`), SIS,
    revision picker, datum, "Utförd av" (multi-select of Participant),
    "Godk".
  - Givardel (sensor side) — one or more `VotingGroup`s, each with its
    own M-out-of-N voting value and a list of `GivardelObject`
    (tag + trip/suffix, e.g. "QIZ-24044" / "HH"). Voting is edited via a
    MooN control: presets 1oo1/1oo2/2oo2/1oo3/2oo3/2oo4 plus true custom
    free text. Changing voting to "NooM" resizes the object list to N
    rows (padding/truncating), keeping existing values where possible.
  - Manöverdel (actuator side) — mirrors givardel: `VotingGroup`s of
    `ManoverdelObject` (tag + action, e.g. "closes on detected CO").
  - Scenario — one free-text field: "vad händer i processen".
  - Konsekvenser (från HAZOP) — a checklist of `HazopConsequence`
    pulled from the HAZOP; each row is checkable, editable, and tagged
    with its HAZOP-derived category (S/M/E) and Kat (1-5, project risk
    matrix). Uncheck to exclude from the LOPA without deleting it; "+
    Lägg till egen" adds a manually authored one (marked `custom=True`).
  - "Vilken skada (konsekvens) är värsta representativa?" — READ-ONLY,
    computed: for each of S/M/E, take the highest Kat among *checked*
    consequences of that type; look up its TEL from the project risk
    matrix. See `worst_case_consequences()`.
  - Oberoende barriärer (independent barriers) — a table of
    `BarrierRow`s. Columns: Orsak till anrop (+ HAZOP ref), Felfrekvens
    (fr/år + larm), then repeating (Beskrivning, RRF) column pairs for
    barrier *types*: Förregling, Annan SIF, Mekanisk, Manuell/admin,
    Annat skydd — this set of barrier types must be extensible (the user
    asked for "flexibility to add other types of independent barriers");
    model it as `BarrierRow.barriers: dict[str, BarrierEntry]` keyed by a
    project-configurable list of barrier-type names, not fixed columns.
    Kvarvarande frekvens (remaining frequency) per row is computed, see
    `remaining_frequency()`. Footer: kontrollfrekvens, förutsättning %,
    motivering, summa kvarvarande frekvens, behovsfrekvens.
  - Eskalering (escalation) — one row per consequence type (S/M/E):
    kommentar, konsekvenstyp, and three (motivering, %) pairs
    (antändning/farlig atmosfär, sannolikhet att närvara, sannolikhet
    att skadas), plus computed eskaleringsfaktor, total olycksfrekvens,
    återstående RRF (see `escalation_row_compute()`). Flags when
    additional risk reduction is required for any type.
  - Dimensionerande kriterium (Säkerhet/Miljö/Ekonomi) + computed SIL
    result (see `compute_sil_and_demand_rate()`).
  - Fritext: "andra åtgärder vid anrop" (icke säkerhetskritiska),
    "andra specifika säkerhetskrav", processäkerhetstid (sek).
  - Kommentarer/anteckningar — freeform threaded notes (author,
    timestamp, text).

Tab 2 — SRS  (depends on LOPA)
-------------------------------
One SRS record = one SIF's safety requirement specification, built
largely from its LOPA. Sections A–G (see `SRS_FIELD_DEFS` for the exact
field list, labels, and which fields are auto-derived from LOPA vs.
directly editable):

  DOK - document info (SIF-nr auto from LOPA header.dok; tillhör
        anläggning/SIS; revision history table: rev/datum/utförare/
        granskad av/godkänd av).
  A   - SIF description: several fields auto-pulled from LOPA (function,
        consequence, cause, process variables, safe state, non-safety
        actions), plus editable driftfall / undantagna driftfall /
        övriga krav.
  B   - Integrity & time requirements: SIL and anropsfrekvens auto from
        LOPA's computed result; demand mode (hög/låg) is a two-way pick.
  C   - Givardel SIF: one card per LOPA `VotingGroup` → per-object
        `GivardelDetail` (mätområde, enhet, processmedia, anrop vid,
        givarfel min/max, signalbehandling, villkorsstyrning, åtgärd
        vid fel) plus group-level ATEX / hårdtrådat interface / övrigt.
  D   - Logikdel SIF: free text.
  E   - Manöverdel SIF: mirrors C for actuator objects
        (`ManoverdelDetail`: processäkert läge, signalkoppling,
        processmedia, detektering, brandklass, tät avstängning,
        manövreringsfrekvens, fördröjd manöver) + ATEX/hårdtrådat/övrigt.
  F1  - svarstid & utlösning (max svarstid, utlösning, manuell
        utlösning, återställning).
  F2  - fel & förbikoppling (falska utlösningar, felhantering,
        förbikoppling, kompenserande åtgärder).
  F3  - underhåll & miljö (MPRT, provningsintervall, omgivningsmiljö,
        strömförsörjning, övrigt).
  G   - Interface och övriga system + övrigt.

Every non-auto SRS field that has a fixed option set is a dropdown, not
free text (see `OPT` below) — this mirrors a checkbox-style Word
template the SRS format was derived from. Any option whose label
contains "ange" (Swedish for "specify") — e.g. "Annat (ange nedan)",
"Ange antal per år" — must reveal a companion free-text input directly
below/after the dropdown to capture the specific value; see
`needs_detail_input()`.

There is also a "SRS worksheet" view: an Excel-style grid with active
SIFs as rows (or, when many fields, as the header row with SIFs listed
along the top and one selected field-category (DOK/A/B/C/D/E/F1/F2/F3/G)
shown at a time) — same field defs and dropdown/auto rules as the form
view, just laid out for scanning several SIFs at once with wrapped
cells.

Data model
----------
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import math


# ---------------------------------------------------------------------------
# Shared enums / option sets
# ---------------------------------------------------------------------------

class ConsequenceType(str, Enum):
    SAFETY = "S"       # Person / säkerhet
    ENVIRONMENT = "M"  # Miljö
    ECONOMY = "E"      # Ekonomi


MOON_PRESETS = ["1oo1", "1oo2", "2oo2", "1oo3", "2oo3", "2oo4"]


def moon_n(moon: str) -> int:
    """Number of objects implied by a MooN voting string, e.g. '2oo3' -> 3."""
    try:
        return int(moon.rsplit("oo", 1)[-1])
    except (ValueError, IndexError):
        return 1


# Fixed dropdown option sets used throughout SRS (Section F/G, actuator and
# sensor detail cards). "Ange ..." / "... (ange nedan)" options must show a
# companion free-text field when selected — see needs_detail_input().
OPT: dict[str, list[str]] = {
    "SRS_OR_OTHER": ["Enligt SRS för SIS", "Annat (ange nedan)"],
    "JA_NEJ": ["Ja", "Nej"],
    "ANROP_VID": ["Hög nivå", "Låg nivå"],
    "ATGARD_FEL": [
        "Utlösning",
        "Krav på redundans och reparation under MPRT",
        "Reparation under MPRT med särskild åtgärd",
    ],
    "ATEX": ["Nej", "Enligt SRS för SIS", "Ja (ange nedan)"],
    "PROCESSAKERT_LAGE": ["Öppen", "Stängd"],
    "SIGNALKOPPLING": ["Vilostationskoppling", "Arbetsströmkoppling"],
    "PROCESSMEDIA_VENTIL": ["Samma som givardelen", "Annat (ange nedan)"],
    "BRANDKLASS": [
        "Enligt SRS för SIS",
        "Täthetsklass (ange)",
        "Brandklass (ange)",
    ],
    "MANOVERFREKVENS": ["Dagligen", "Veckovis", "Månadsvis", "Sällan/aldrig"],
    "FORDROJD": ["Inte relevant", "Ange tid (sek)"],
    "MAX_SVARSTID": [
        "Enligt SRS för SIS",
        "Ange % av processäkerhetstiden",
        "Max X sek (ange)",
    ],
    "UTLOSNING": ["Automatisk utlösning", "Annat (ange nedan)"],
    "FALSKA_UTLOSNINGAR": ["Enligt SRS för SIS", "Ange antal per år"],
    "FORBIKOPPLING": [
        "Förbikoppling tillåten under drift med villkor",
        "Förbikoppling inte tillåten under drift",
    ],
    "MPRT_OPTS": ["Enligt SRS för SIS", "Ange timmar"],
    "DEMAND_MODE": ["hog", "lag"],
}


def needs_detail_input(selected_value: str) -> bool:
    """True if the selected dropdown option requires a companion free-text
    field (any option containing 'ange', case-insensitive)."""
    return "ange" in (selected_value or "").lower()


# Default project risk matrix (Kat 1-5 -> TEL). Editable per project; TEL
# is looked up per consequence category, not per S/M/E type directly.
DEFAULT_RISK_MATRIX = [
    {"kat": 1, "tel": 1.00e-02, "desc": "Lindriga obehag, 1:a hjälpen"},
    {"kat": 2, "tel": 1.00e-03, "desc": "Enstaka skadade eller varaktiga obehag"},
    {"kat": 3, "tel": 1.00e-04, "desc": "Enstaka svårt skadade, bestående men"},
    {"kat": 4, "tel": 1.00e-05, "desc": "Ett dödsfall eller flera svårt skadade"},
    {"kat": 5, "tel": 1.00e-06, "desc": "Flera dödsfall eller 10-tals svårt skadade"},
]


# ---------------------------------------------------------------------------
# LOPA data model
# ---------------------------------------------------------------------------

@dataclass
class Participant:
    id: int
    name: str


@dataclass
class GivardelObject:
    """One sensor/instrument in a givardel voting group."""
    id: int
    tag: str = ""            # e.g. "QIZ-24044"
    suffix: str = ""         # trip level, e.g. "HH"
    hazop_instrument_id: Optional[str] = None  # link back to HAZOP instrument, if pulled from there


@dataclass
class ManoverdelObject:
    """One actuator in a manöverdel voting group."""
    id: int
    name: str = ""           # tag, e.g. "XVZ-24135"
    action: str = ""          # e.g. "closes on detected CO"


@dataclass
class VotingGroup:
    """A MooN-voted group of either sensors or actuators.
    `kind` distinguishes givardel vs manöverdel so the same class can back
    both sides; keep them as two separate lists on LopaRecord regardless.
    """
    id: int
    moon: str = "1oo2"                 # preset from MOON_PRESETS, or free text
    givardel_objects: list[GivardelObject] = field(default_factory=list)
    manoverdel_objects: list[ManoverdelObject] = field(default_factory=list)

    def resize_to_moon(self) -> None:
        """Call after changing `moon` to a preset: pad/truncate the object
        list to match the N in MooN, keeping existing entries where possible."""
        n = moon_n(self.moon)
        if self.givardel_objects:
            objs = self.givardel_objects[:n]
            while len(objs) < n:
                objs.append(GivardelObject(id=_next_id()))
            self.givardel_objects = objs
        if self.manoverdel_objects:
            objs = self.manoverdel_objects[:n]
            while len(objs) < n:
                objs.append(ManoverdelObject(id=_next_id()))
            self.manoverdel_objects = objs


@dataclass
class HazopConsequence:
    """A consequence pulled from (or manually added alongside) the HAZOP.
    kat_by_type holds the HAZOP's severity category per axis, e.g.
    {'S': 3, 'M': None, 'E': 1} — a single HAZOP line can carry more than
    one consequence axis at once.
    """
    id: int
    text: str = ""
    ref: str = ""                       # HAZOP display ref, e.g. "(28.4.1)"
    kat_by_type: dict[str, Optional[int]] = field(default_factory=lambda: {"S": None, "M": None, "E": None})
    checked: bool = True                 # included in "worst case" computation
    custom: bool = False                 # True if authored directly in LOPA, not pulled from HAZOP
    hazop_node_id: Optional[str] = None
    hazop_cause_id: Optional[str] = None
    hazop_consequence_id: Optional[str] = None


@dataclass
class BarrierRow:
    """One independent-barrier row. `barriers` is intentionally an open
    dict keyed by barrier-type name so new barrier types can be added at
    the project level without a schema change — the base template ships
    with ['Förregling', 'Annan SIF', 'Mekanisk', 'Manuell/admin',
    'Annat skydd'] but the UI must let the user add more (each new type
    becomes one more (Beskrivning, RRF) column pair in the table).
    """
    nr: int
    orsak: str = ""
    hazop_ref: str = ""                  # e.g. "(28.4.1)" — link to HAZOP cause
    hazop_node_id: Optional[str] = None
    hazop_cause_id: Optional[str] = None
    frekvens: float = 0.0                 # fr/år
    larm: str = ""
    barriers: dict[str, "BarrierEntry"] = field(default_factory=dict)

    def rrf_product(self) -> float:
        product = 1.0
        for entry in self.barriers.values():
            product *= max(entry.rrf, 1.0)
        return product

    def remaining_frequency(self) -> float:
        return self.frekvens / self.rrf_product()


@dataclass
class BarrierEntry:
    beskrivning: str = ""
    rrf: float = 1.0


# The default set of barrier-type column names; store the live list on the
# project/settings level so users can append custom types.
DEFAULT_BARRIER_TYPES = ["Förregling", "Annan SIF", "Mekanisk", "Manuell/admin", "Annat skydd"]


@dataclass
class EscalationRow:
    type: ConsequenceType
    comment: str = ""
    antandning_motivering: str = ""
    antandning_pct: float = 100.0
    narvara_motivering: str = ""
    narvara_pct: float = 100.0
    skadas_motivering: str = ""
    skadas_pct: float = 100.0


@dataclass
class Comment:
    author: str
    timestamp: str
    text: str


@dataclass
class Header:
    sif: str = ""            # SIF beteckning/namn
    sis: str = ""
    dok: str = ""             # LOPA-nr / SIF-nr
    godk: str = ""
    datum: str = ""


@dataclass
class Revision:
    label: str = "00"


@dataclass
class LopaData:
    """The editable content of one LOPA revision."""
    header: Header = field(default_factory=Header)
    givardel_groups: list[VotingGroup] = field(default_factory=list)
    manoverdel_groups: list[VotingGroup] = field(default_factory=list)
    participant_ids: list[int] = field(default_factory=list)   # "Utförd av"
    scenario: str = ""
    hazop_consequences: list[HazopConsequence] = field(default_factory=list)
    hazop_only_filter: bool = False
    barrier_rows: list[BarrierRow] = field(default_factory=list)
    barrier_types: list[str] = field(default_factory=lambda: list(DEFAULT_BARRIER_TYPES))
    kontrollfrekvens: str = ""
    forutsattning_pct: float = 100.0
    motivering: str = ""
    criterion: str = "Säkerhet"          # Säkerhet | Miljö | Ekonomi
    escalation: list[EscalationRow] = field(default_factory=list)
    additional_actions: str = ""
    additional_requirements: str = ""
    process_safety_time_sec: float = 60.0
    comments: list[Comment] = field(default_factory=list)


@dataclass
class LopaRecord:
    """One SIF's LOPA, across all its revisions."""
    id: int
    current_rev: str = "00"
    revisions: dict[str, LopaData] = field(default_factory=lambda: {"00": LopaData()})

    def current(self) -> LopaData:
        return self.revisions[self.current_rev]

    def new_revision(self, label: str) -> None:
        """Start a new revision as a copy of the current one, then switch to it."""
        import copy
        self.revisions[label] = copy.deepcopy(self.current())
        self.current_rev = label


# ---------------------------------------------------------------------------
# LOPA computed fields
# ---------------------------------------------------------------------------

def worst_case_consequences(data: LopaData, risk_matrix: list[dict] = DEFAULT_RISK_MATRIX) -> dict[str, dict]:
    """For each of S/M/E, the highest Kat among checked consequences of
    that type, its description, and the TEL looked up from the risk
    matrix. Mirrors the read-only 'Vilken skada...' card."""
    tel_by_kat = {m["kat"]: m["tel"] for m in risk_matrix}
    result = {}
    for t in ("S", "M", "E"):
        checked = [c for c in data.hazop_consequences if c.checked and c.kat_by_type.get(t) is not None]
        if checked:
            top = max(checked, key=lambda c: c.kat_by_type[t])
            kat = top.kat_by_type[t]
            desc = top.text or "—"
        else:
            kat, desc = 1, "—"
        result[t] = {"kat": kat, "desc": desc, "tel": tel_by_kat.get(kat)}
    return result


def escalation_row_compute(row: EscalationRow, behovsfrekvens: float, criterion_tel: float) -> dict:
    esk_faktor = (row.antandning_pct / 100) * (row.narvara_pct / 100) * (row.skadas_pct / 100)
    total = behovsfrekvens * esk_faktor
    ratio = total / criterion_tel if criterion_tel > 0 else 0
    rrf_required = math.ceil(ratio) if ratio > 1 else 0
    return {"eskaleringsfaktor": esk_faktor, "total_olycksfrekvens": total, "rrf_required": rrf_required}


def compute_sil_and_demand_rate(data: LopaData, risk_matrix: list[dict] = DEFAULT_RISK_MATRIX) -> dict:
    """Mirrors computeSilAndBehov() in the prototype: sums remaining
    frequency across barrier rows, adjusts by "förutsättning %", then
    finds the max required RRF across escalation rows (against each
    row's own TEL) to land on a SIL band."""
    tel_by_kat = {m["kat"]: m["tel"] for m in risk_matrix}
    worst = worst_case_consequences(data, risk_matrix)
    crit_by_type = {t: (worst[t]["tel"] or 0.01) for t in ("S", "M", "E")}

    sum_kvarvarande = sum(row.remaining_frequency() for row in data.barrier_rows)
    forutsattning = data.forutsattning_pct or 100
    behovsfrekvens = sum_kvarvarande / (forutsattning / 100)

    max_rrf = 0
    for row in data.escalation:
        criterion_tel = crit_by_type.get(row.type.value if isinstance(row.type, ConsequenceType) else row.type, 0.01)
        result = escalation_row_compute(row, behovsfrekvens, criterion_tel)
        max_rrf = max(max_rrf, result["rrf_required"])

    if max_rrf <= 10:
        sil = "a"
    elif max_rrf <= 30:
        sil = "1"
    elif max_rrf <= 100:
        sil = "2"
    elif max_rrf <= 300:
        sil = "3"
    else:
        sil = "4"
    return {"sil": sil, "behovsfrekvens_per_year": behovsfrekvens, "max_rrf_required": max_rrf}


# ---------------------------------------------------------------------------
# SRS data model  (depends on the LOPA it's built from)
# ---------------------------------------------------------------------------

@dataclass
class RevisionHistoryRow:
    rev: str
    datum: str = ""
    utforare: str = ""
    granskad: str = ""
    godkand: str = ""


@dataclass
class GivardelDetail:
    """SRS-only detail attached to one GivardelObject, keyed by that
    object's id."""
    matomrade: str = ""
    enhet: str = ""
    processmedia: str = ""
    anrop_vid: str = ""              # OPT["ANROP_VID"]
    givarfel: str = ""
    signalbehandling: str = ""
    villkorsstyrning: str = ""
    atgard_fel: str = ""              # OPT["ATGARD_FEL"]


@dataclass
class ManoverdelDetail:
    """SRS-only detail attached to one ManoverdelObject, keyed by that
    object's id."""
    processakert_lage: str = ""       # OPT["PROCESSAKERT_LAGE"]
    signalkoppling: str = ""          # OPT["SIGNALKOPPLING"]
    processmedia: str = ""            # OPT["PROCESSMEDIA_VENTIL"]
    detektering: str = ""
    brandklass: str = ""              # OPT["BRANDKLASS"]
    tat_avstangning: str = ""         # OPT["JA_NEJ"]
    manoverfrekvens: str = ""         # OPT["MANOVERFREKVENS"]
    fordrojd_manover: str = ""        # OPT["FORDROJD"]


@dataclass
class SrsData:
    tillhor_anlaggning: str = ""
    tillhor_sis: str = ""
    revision_history: dict[str, RevisionHistoryRow] = field(default_factory=dict)

    # Section A
    driftfall: str = ""
    undantagna_driftfall: str = ""
    ovrig_krav_a: str = ""

    # Section B
    demand_mode: str = "lag"          # OPT["DEMAND_MODE"]

    # Section C
    givardel_details: dict[int, GivardelDetail] = field(default_factory=dict)   # keyed by GivardelObject.id
    atex_givardel: str = ""           # OPT["ATEX"]
    hardtradat_givardel: str = ""     # OPT["SRS_OR_OTHER"]
    givardel_ovrigt: str = ""

    # Section D
    logik_beskrivning: str = ""

    # Section E
    manoverdel_details: dict[int, ManoverdelDetail] = field(default_factory=dict)  # keyed by ManoverdelObject.id
    atex_manoverdel: str = ""         # OPT["ATEX"]
    hardtradat_manoverdel: str = ""   # OPT["SRS_OR_OTHER"]
    manoverdel_ovrigt: str = ""

    # Section F1
    max_svarstid: str = ""            # OPT["MAX_SVARSTID"]
    utlosning_automatik: str = ""     # OPT["UTLOSNING"]
    manuell_utlosning: str = ""       # OPT["SRS_OR_OTHER"]
    aterstallning: str = ""           # OPT["SRS_OR_OTHER"]

    # Section F2
    falska_utlosningar: str = ""      # OPT["FALSKA_UTLOSNINGAR"]
    felhantering: str = ""            # OPT["SRS_OR_OTHER"]
    forbikoppling: str = ""           # OPT["FORBIKOPPLING"]
    kompenserande_atgarder: str = ""

    # Section F3
    mprt: str = ""                    # OPT["MPRT_OPTS"]
    provningsintervall: str = ""      # OPT["SRS_OR_OTHER"]
    omgivningsmiljo: str = ""         # OPT["SRS_OR_OTHER"]
    stromforsorjning: str = ""        # OPT["SRS_OR_OTHER"]
    ovrigt_f: str = ""

    # Section G
    interface_ovriga_system: str = ""  # OPT["SRS_OR_OTHER"]
    ovrigt_g: str = ""

    # Free-text companions for any dropdown value containing "ange" — keyed
    # by the same field name + "_detalj", e.g. "max_svarstid_detalj".
    detail_values: dict[str, str] = field(default_factory=dict)


@dataclass
class SrsRecord:
    """SRS is 1:1 with a LOPA record (same id, same SIF)."""
    lopa_id: int
    data: SrsData = field(default_factory=SrsData)


# Field definitions for both the SRS form view and the SRS worksheet grid.
# `auto=True` fields are computed from the linked LopaRecord (see
# compute_srs_auto_field below) and are read-only; others are directly
# editable SrsData fields. `options` names a key into OPT for dropdown
# fields; fields with no `options` entry are free text / textarea.
SRS_FIELD_DEFS: dict[str, list[dict]] = {
    "DOK": [
        {"key": "sifNr", "label": "SIF Nr/Beteckning", "auto": True},
        {"key": "tillhorAnlaggning", "label": "Tillhör anläggning", "auto": False},
        {"key": "tillhorSis", "label": "Tillhör SIS", "auto": False},
    ],
    "A": [
        {"key": "sif", "label": "Vad avsäkras/funktion?", "auto": True},
        {"key": "scenario", "label": "Konsekvens (skadehändelse)", "auto": True},
        {"key": "orsaker", "label": "Orsak till anrop", "auto": True},
        {"key": "processvariabler", "label": "Processvariabler", "auto": True},
        {"key": "sakertLage", "label": "Säkert läge", "auto": True},
        {"key": "ytterligareAtgarder", "label": "Ytterligare åtgärder", "auto": True},
        {"key": "driftfall", "label": "Gällande driftfall", "auto": False},
        {"key": "undantagnaDriftfall", "label": "Undantagna driftfall", "auto": False},
        {"key": "ovrigKravA", "label": "Övriga krav eller undantag", "auto": False},
    ],
    "B": [
        {"key": "sil", "label": "Integritetsnivå (SIL)", "auto": True},
        {"key": "processSafetyTime", "label": "Processäkerhetstid (sek)", "auto": True},
        {"key": "behovsfrekvens", "label": "Anropsfrekvens/år", "auto": True},
        {"key": "demandMode", "label": "Anropsmod", "auto": False, "options": "DEMAND_MODE"},
    ],
    "C": [
        {"key": "givardelVoting", "label": "Voting", "auto": True},
        {"key": "givardelSummary", "label": "Givare (tag/utlösningsnivå)", "auto": True},
        {"key": "atexGivardel", "label": "ATEX-klassning", "auto": False, "options": "ATEX"},
        {"key": "hardtradatGivardel", "label": "Hårdtrådat interface", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "givardelOvrigt", "label": "Övriga krav eller undantag", "auto": False},
    ],
    "D": [
        {"key": "logikBeskrivning", "label": "Beskrivning av logik", "auto": False},
    ],
    "E": [
        {"key": "manoverdelVoting", "label": "Voting", "auto": True},
        {"key": "manoverdelSummary", "label": "Manöverdel (objekt/action)", "auto": True},
        {"key": "atexManoverdel", "label": "ATEX-klassning", "auto": False, "options": "ATEX"},
        {"key": "hardtradatManoverdel", "label": "Hårdtrådat interface", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "manoverdelOvrigt", "label": "Övriga krav eller undantag", "auto": False},
    ],
    "F1": [
        {"key": "maxSvarstid", "label": "Max svarstid", "auto": False, "options": "MAX_SVARSTID"},
        {"key": "utlosningAutomatik", "label": "Utlösning av SIF", "auto": False, "options": "UTLOSNING"},
        {"key": "manuellUtlosning", "label": "Manuell utlösning", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "aterstallning", "label": "Återställning efter utlöst SIF", "auto": False, "options": "SRS_OR_OTHER"},
    ],
    "F2": [
        {"key": "falskaUtlosningar", "label": "Falska utlösningar", "auto": False, "options": "FALSKA_UTLOSNINGAR"},
        {"key": "felhantering", "label": "Felhantering vid fel", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "forbikoppling", "label": "Förbikoppling av SIF", "auto": False, "options": "FORBIKOPPLING"},
        {"key": "kompenserandeAtgarder", "label": "Kompenserande åtgärder", "auto": False},
    ],
    "F3": [
        {"key": "mprt", "label": "Max reparationstid (MPRT)", "auto": False, "options": "MPRT_OPTS"},
        {"key": "provningsintervall", "label": "Provningsintervall", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "omgivningsmiljo", "label": "Omgivningsmiljö", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "stromforsorjning", "label": "Strömförsörjning", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "ovrigtF", "label": "Övrigt", "auto": False},
    ],
    "G": [
        {"key": "interfaceOvrigaSystem", "label": "Interface och övriga system", "auto": False, "options": "SRS_OR_OTHER"},
        {"key": "ovrigtG", "label": "Övriga krav eller undantag", "auto": False},
    ],
}


def compute_srs_auto_field(key: str, lopa: LopaRecord, risk_matrix: list[dict] = DEFAULT_RISK_MATRIX) -> str:
    """Resolves every `auto: True` SRS field from the linked LOPA."""
    data = lopa.current()
    if key == "sifNr":
        return data.header.dok or "—"
    if key == "sif":
        return data.header.sif or "—"
    if key == "scenario":
        return data.scenario or "—"
    if key == "orsaker":
        return "; ".join(r.orsak for r in data.barrier_rows if r.orsak) or "—"
    if key == "processvariabler":
        tags = [o.tag for g in data.givardel_groups for o in g.givardel_objects if o.tag]
        return ", ".join(tags) or "—"
    if key == "sakertLage":
        actions = [o.action for g in data.manoverdel_groups for o in g.manoverdel_objects if o.action]
        return "; ".join(actions) or "—"
    if key == "ytterligareAtgarder":
        return data.additional_actions or "—"
    if key == "processSafetyTime":
        return str(data.process_safety_time_sec) or "—"
    if key in ("sil", "behovsfrekvens"):
        result = compute_sil_and_demand_rate(data, risk_matrix)
        return result["sil"] if key == "sil" else f"{result['behovsfrekvens_per_year']:.3f}"
    if key == "givardelVoting":
        return " + ".join(g.moon for g in data.givardel_groups) or "—"
    if key == "givardelSummary":
        parts = [f"{o.tag} ({o.suffix})" if o.suffix else o.tag for g in data.givardel_groups for o in g.givardel_objects if o.tag]
        return ", ".join(parts) or "—"
    if key == "manoverdelVoting":
        return " + ".join(g.moon for g in data.manoverdel_groups) or "—"
    if key == "manoverdelSummary":
        parts = [f"{o.name} — {o.action}" if o.action else o.name for g in data.manoverdel_groups for o in g.manoverdel_objects if o.name]
        return "; ".join(parts) or "—"
    return "—"


_id_counter = 1000


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter


# ---------------------------------------------------------------------------
# HAZOP link contract — implement against your existing HAZOP data model
# ---------------------------------------------------------------------------

def link_barrier_from_hazop(
    lopa: LopaRecord,
    hazop_node_id: str,
    hazop_cause_id: str,
    cause_text: str,
    hazop_ref_label: str,
    consequences: list[dict],
) -> BarrierRow:
    """Call this from the HAZOP "right click -> Koppla till LOPA" action.

    `consequences` is a list of dicts pulled from the HAZOP row, one per
    consequence axis actually filled in there, e.g.:
        [{"type": "S", "kat": 3, "text": "...", "consequence_id": "c1"}, ...]

    Returns the created BarrierRow (already appended to lopa.current()).
    Also merges/creates matching HazopConsequence entries so they appear,
    pre-checked, in the LOPA's "Konsekvenser (från HAZOP)" list.
    """
    data = lopa.current()
    row = BarrierRow(
        nr=len(data.barrier_rows) + 1,
        orsak=cause_text,
        hazop_ref=hazop_ref_label,
        hazop_node_id=hazop_node_id,
        hazop_cause_id=hazop_cause_id,
        frekvens=0.0,
        barriers={bt: BarrierEntry() for bt in data.barrier_types},
    )
    data.barrier_rows.append(row)

    for c in consequences:
        existing = next((hc for hc in data.hazop_consequences if hc.hazop_consequence_id == c.get("consequence_id")), None)
        if existing:
            existing.kat_by_type[c["type"]] = c["kat"]
            existing.checked = True
        else:
            data.hazop_consequences.append(HazopConsequence(
                id=_next_id(),
                text=c.get("text", ""),
                ref=hazop_ref_label,
                kat_by_type={"S": None, "M": None, "E": None, **{c["type"]: c["kat"]}},
                checked=True,
                custom=False,
                hazop_node_id=hazop_node_id,
                hazop_cause_id=hazop_cause_id,
                hazop_consequence_id=c.get("consequence_id"),
            ))
    return row
