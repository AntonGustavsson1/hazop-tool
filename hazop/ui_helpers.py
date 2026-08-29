#!/usr/bin/env python3
"""Shared Qt-dependent (but widget-independent) helper functions — split out
of hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer". Used by both tree_panel.py and hazop.py's remaining panels, so
this sits below both in the import layer graph."""

import json
import math
import re

from PyQt6.QtWidgets import QCompleter, QMessageBox, QInputDialog, QLineEdit
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QFont, QFontMetrics, QTextLayout, QTextOption, QTextCharFormat

from database import get_matrix, freq_to_f_level
from pid_viewer import COMPONENT_TYPES, _equip_prefix_from_tag, _obj_type_matches


def freq_axis_label(f_val: int) -> str:
    """Short configured label (first word only) for a frequency value (-1..5)."""
    cfg  = get_matrix()
    cols = cfg.get('cols', 7)
    idx  = max(0, min(int(f_val) + 1, cols - 1))
    lbls = cfg.get('x_labels', [])
    full = lbls[idx] if idx < len(lbls) else f'F={f_val}'
    return full.split()[0] if full.strip() else f'F={f_val}'


def freq_axis_label_full(f_val: int) -> str:
    """Full configured label for a frequency value (-1..5), e.g. 'F3 – Möjlig (1/100 år)'."""
    cfg  = get_matrix()
    cols = cfg.get('cols', 7)
    idx  = max(0, min(int(f_val) + 1, cols - 1))
    lbls = cfg.get('x_labels', [])
    return lbls[idx] if idx < len(lbls) else f'F={f_val}'


def cons_axis_label(c_val: int) -> str:
    """Short configured label for a consequence value (1..5). y_labels always stores cons labels."""
    cfg  = get_matrix()
    rows = cfg.get('rows', 5)
    idx  = max(0, min(int(c_val) - 1, rows - 1))
    lbls = cfg.get('y_labels', [])
    full = lbls[idx] if idx < len(lbls) else f'C={c_val}'
    return full.split()[0] if full.strip() else f'C={c_val}'


_EQ_TYPE_ITEMS = [''] + sorted(COMPONENT_TYPES.keys()) + ['Rörledning', 'Övrigt / Okänd']


def _equipment_type_options(db):
    """Object-type dropdown options for every place the user assigns an
    equipment type -- including defining a new object on the P&ID
    (EquipmentPlacementPopup/EquipmentDeviationBar, pid_panel_mod.py) --
    sourced from the Standardobjekt list (`standard_objects`, the same
    admin-managed catalogue behind Inställningar → Standardobjekt and the
    cause-suggestion forms) instead of a separate, larger ISA-style list.

    2026-08-26 (Anton: "Objektlistan som används när objekt definieras på
    P&ID ska vara samma som programmets standardobjektlista... använda
    standardlistan som källa"): previously this unioned _EQ_TYPE_ITEMS
    (COMPONENT_TYPES' ~90 ISA prefixes) together with standard_objects,
    so the two lists only "pratade med varandra" instead of the P&ID one
    actually BEING the standard one. standard_objects already covers the
    same ground its old "Rörledning"/"Övrigt / Okänd" catch-alls did
    ("Rörledning / slang", "Övrigt"), so nothing is lost by dropping
    _EQ_TYPE_ITEMS as the base here.

    Any equipment_type string already used in the catalog that ISN'T a
    current standard object still gets appended at the end (2026-08-13:
    "vill kunna lägga till nya typer av objekt som inte redan finns i
    listan") -- legacy/custom data must stay visible/selectable, same
    rationale as before, just against the new, standard_objects-first
    baseline instead of the old ISA one."""
    standard = [o['name'] for o in db.standard_objects()]
    rows = db.conn.execute(
        "SELECT DISTINCT equipment_type FROM equipment_catalog "
        "WHERE equipment_type IS NOT NULL AND equipment_type != ''").fetchall()
    known = set(standard)
    extra = sorted({r[0] for r in rows} - known)
    return [''] + standard + extra


def _tag_letter_prefix(tag: str) -> str:
    """Extract the instrument-code letter prefix from a P&ID tag.
    Delegates to _equip_prefix_from_tag for compound-tag handling.
    'E1.M1.PU101' → 'PU', 'E1' → 'E', 'PCV-101' → 'PCV', '20-FT-201' → 'FT'
    """
    return _equip_prefix_from_tag(tag) if tag else ''


def _lookup_comp_type_for_tag(tag: str, db) -> str:
    """Return the component type the user has taught for this tag's prefix.

    ONLY uses study_tag_memory — the smart recognition table that is built
    exclusively from explicit user confirmations via rubber-band markup.
    Numbers are ignored; the letter prefix is the key (321HV3333 → HV).
    Returns '' if the prefix has not been taught or smart recognition is off.
    """
    if not tag:
        return ''
    if hasattr(db, 'get_config'):
        if db.get_config('smart_recognition_enabled', '1') != '1':
            return ''
    pfx = _tag_letter_prefix(tag)
    if not pfx:
        return ''
    try:
        return db.get_prefix_memory(pfx) if hasattr(db, 'get_prefix_memory') else ''
    except Exception:
        return ''


# _obj_type_matches now lives in pid_viewer.py (imported above) — EquipmentDeviationBar
# needs it too, and pid_viewer.py can't import back from hazop.py.


def _make_tag_completer(db, parent):
    """Build a QCompleter of known equipment tags for a Tag-ID QLineEdit.
    Returns None (leaving the field plain) if the catalog can't be read.
    """
    try:
        tags = [r[0] for r in db.conn.execute(
            "SELECT DISTINCT tag FROM equipment_catalog ORDER BY tag").fetchall()]
        comp = QCompleter(tags, parent)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return comp
    except Exception:
        return None


_NATSORT_RE = re.compile(r'(\d+)')


def _natural_sort_key(s):
    """Split 's' into alternating text/number runs so a plain sort puts
    'O2-PI123' before 'O10-PI123' (numeric order) instead of after it
    (plain string order, where '1' < '2' makes "O10" < "O2"). Digit runs
    compare as ints; everything else compares as lowercased text."""
    return [int(part) if part.isdigit() else part.lower()
            for part in _NATSORT_RE.split(s or '')]


def _equipment_tags_for_types(db):
    """Every distinct equipment_catalog tag, naturally/numerically
    sorted (2026-08-26, see NOTES.md "Gör om safeguard-valet" — Anton:
    "sorterade numeriskt") so e.g. 'O2-PI123' sorts before 'O10-PI123'
    instead of after it. Feeds the safeguard object-picker dropdown
    (SafeguardObjectPopup)."""
    try:
        rows = db.conn.execute("SELECT DISTINCT tag FROM equipment_catalog").fetchall()
        return sorted((r[0] for r in rows), key=_natural_sort_key)
    except Exception:
        return []


def _resolve_comp_type_for_tag(db, tag):
    """Exact (case-insensitive) equipment_catalog lookup: what type is
    THIS tag actually catalogued as? Used to auto-fill comp_type when a
    safeguard's object is picked/typed via the object-picker dropdown —
    free text with no catalog match returns ''."""
    if not tag:
        return ''
    try:
        row = db.conn.execute(
            "SELECT equipment_type FROM equipment_catalog WHERE tag=? COLLATE NOCASE LIMIT 1",
            (tag,)).fetchone()
        return (row[0] or '') if row else ''
    except Exception:
        return ''


def _resolve_std_deviation_id(db, deviation_description):
    """Look up the standard_deviations.id matching a node deviation's
    description text, or None if it doesn't match a standard deviation
    (e.g. a free-typed deviation name).
    """
    if not deviation_description:
        return None
    row = db.conn.execute(
        "SELECT id FROM standard_deviations WHERE description=? AND active=1 COLLATE NOCASE LIMIT 1",
        (deviation_description,)).fetchone()
    return row[0] if row else None


def standard_cause_options(db, deviation_description, comp_type):
    """Return the standard-cause options for one deviation/type context.

    Every route into an Orsak cell (tree, P&ID drop, object picker and
    ordinary inline text) must get the same result.  Keep the lookup cascade
    here rather than letting separate UI paths maintain subtly different
    copies:

    1. exact standard deviation + standard object;
    2. component type within the current deviation;
    3. component type anywhere in the standard-cause library.

    Returns ``(standard_deviation_id, standard_object_id, rows)``.  An empty
    type deliberately returns no button-list rows; the caller may still offer
    its own broader type-ahead fallback while the user is typing.
    """
    comp_type = (comp_type or '').strip()
    std_dev_id = _resolve_std_deviation_id(db, deviation_description)
    obj_id = None
    if comp_type:
        try:
            for obj in db.standard_objects():
                if _obj_type_matches(comp_type, obj['name']):
                    obj_id = obj['id']
                    break
        except Exception:
            obj_id = None

    rows = []
    if comp_type:
        if std_dev_id is not None and obj_id is not None:
            rows = db.standard_causes_for_object(std_dev_id, obj_id)
        if not rows:
            rows = db.standard_causes_for_comp_type(
                comp_type, deviation_description)
        if not rows:
            rows = db.standard_causes_for_comp_type(comp_type)
    return std_dev_id, obj_id, rows


def _create_cause_from_pick(db, deviation_id, description, frequency):
    """Create a new cause under deviation_id, applying the
    description/likelihood/frequency consistently — shared by every
    quick-add entry point so a freshly created cause always starts with
    real content instead of a blank placeholder. description/frequency
    may both be None for a genuinely blank cause (TreePanel's
    "+ Orsak"/right-click/Enter entry points, 2026-08-24 — used to open a
    StandardCausesPickerPopup dialog here, removed at Anton's request in
    favor of creating directly, same as add_consequence()/add_safeguard()).

    Also creates one empty consequence for the new cause (2026-08-07, see
    NOTES.md "direkt konsekvensinmatning") — the same no-popup
    db.add_consequence() call TreePanel.add_consequence() already uses, so
    the HAZOP scenario table's KON cell is ready for inline typing the
    instant the cause exists, without a separate add-consequence step.
    Returns (cause_id, consequence_id).
    """
    new_id = db.add_cause(deviation_id)
    like = freq_to_f_level(frequency) if frequency is not None else 3
    db.update_cause(new_id, description=description or 'Ny orsak', likelihood=like)
    if frequency is not None:
        db.conn.execute("UPDATE causes SET base_frequency=? WHERE id=?", (frequency, new_id))
        db.commit()
    cons_id = db.add_consequence(new_id)
    return new_id, cons_id


def _maybe_save_as_standard_cause(parent, db, dev_id, obj_id, obj_name, description):
    """Ask if a free-typed cause should be promoted into the standard_causes
    library (with an optional frequency), so it's offered again next time.
    Shared by every cause-entry dialog that has a free-text fallback field.
    """
    if dev_id is None or obj_id is None or db is None or not description:
        return
    ans = QMessageBox.question(
        parent, "Spara som standardorsak?",
        f"Vill du spara\n\"{description}\"\nsom standardorsak för {obj_name or 'detta objekt'}?\n\n"
        "Den kommer då att finnas i listan nästa gång.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    if ans != QMessageBox.StandardButton.Yes:
        return

    freq_str, ok = QInputDialog.getText(
        parent, "Frekvens (valfritt)",
        "Ange typfrekvens i händelser/år (lämna tomt om okänd).\n"
        "Exempel: 0.01  (= 1e-2/år, ungefär vart 100:e år)",
        QLineEdit.EchoMode.Normal, '')
    freq = None
    if ok and freq_str.strip():
        try:
            freq = float(freq_str.strip().replace(',', '.'))
        except ValueError:
            pass

    try:
        existing = db.conn.execute(
                "SELECT id FROM standard_causes WHERE deviation_id=? AND description=? AND active=1",
            (dev_id, description)).fetchone()
        if not existing:
            db.conn.execute(
                "INSERT INTO standard_causes "
                "(deviation_id, description, sort_order, object_id, frequency)"
                " VALUES (?,?,?,?,?)",
                (dev_id, description,
                 (db.conn.execute(
                     "SELECT COALESCE(MAX(sort_order),0)+1 FROM standard_causes "
                     "WHERE deviation_id=?", (dev_id,)).fetchone()[0]),
                 obj_id, freq))
            db.commit()
    except Exception:
        pass
def find_tag_bold_ranges(text: str, tags: list) -> list:
    """Return sorted, non-overlapping (start, end) character ranges in
    `text` where a member of `tags` occurs as a whole word — not as a
    substring of a larger word/tag (e.g. tag "TA-1" must not match inside
    "TA-10") — used to bold drag-and-dropped equipment references within
    a free-text description (2026-08-09, see NOTES.md)."""
    ranges = []
    for tag in tags:
        tag = (tag or '').strip()
        if not tag:
            continue
        start = 0
        while True:
            idx = text.find(tag, start)
            if idx < 0:
                break
            end = idx + len(tag)
            before_ok = idx == 0 or not text[idx - 1].isalnum()
            after_ok = end == len(text) or not text[end].isalnum()
            if before_ok and after_ok:
                ranges.append((idx, end))
            start = idx + 1
    ranges.sort()
    merged = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def find_bold_tag_at_position(text, tags, rect, point, base_font,
                              word_wrap=True):
    """Return the bold tag occurrence hit at *point*, if any.

    The HAZOP table deliberately paints P&ID tags in bold inside otherwise
    ordinary free text.  A cell-level click test is too broad: it would turn
    normal editing into object editing.  This helper rebuilds the same
    ``QTextLayout`` used by :func:`_draw_text_with_bold_tags` and accepts a
    click only inside the actual bold glyph run.  The result contains the
    displayed tag and its character range in *text*, which lets callers
    replace the precise occurrence even when a tag appears more than once.
    """
    text = str(text or '')
    if not text or not tags or not rect.contains(point):
        return None
    ranges = find_tag_bold_ranges(text, tags)
    if not ranges:
        return None

    # A transient delegate can theoretically be called with no explicit
    # FontRole.  Painting itself has the same safe fallback below.
    font = QFont(base_font) if base_font is not None else QFont()
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap if word_wrap
                       else QTextOption.WrapMode.NoWrap)
    layout.setTextOption(option)

    bold_font = QFont(font)
    bold_font.setBold(True)
    bold_fmt = QTextCharFormat()
    bold_fmt.setFont(bold_font)
    formats = []
    for start, end in ranges:
        fmt_range = QTextLayout.FormatRange()
        fmt_range.start = start
        fmt_range.length = end - start
        fmt_range.format = bold_fmt
        formats.append(fmt_range)
    layout.setFormats(formats)

    layout.beginLayout()
    y = 0.0
    lines = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, y))
        lines.append(line)
        y += line.height()
        if not word_wrap:
            break
    layout.endLayout()

    x = point.x() - rect.left()
    y = point.y() - rect.top()
    for line in lines:
        line_top = line.position().y()
        if not (line_top <= y < line_top + line.height()):
            continue
        line_start = line.textStart()
        line_end = line_start + line.textLength()
        for start, end in ranges:
            start_in_line = max(start, line_start)
            end_in_line = min(end, line_end)
            if start_in_line >= end_in_line:
                continue
            # PyQt6 returns ``(x, cursor_position)`` from cursorToX().
            left = line.cursorToX(start_in_line)[0]
            right = line.cursorToX(end_in_line)[0]
            if min(left, right) <= x <= max(left, right):
                return {'tag': text[start:end], 'start': start, 'end': end}
    return None


def _draw_text_with_bold_tags(painter, rect, text, tags, base_font, color, word_wrap):
    """Draw `text` inside `rect`, rendering any whole-word occurrence of a
    member of `tags` in bold (2026-08-09, see NOTES.md "fetmarkera
    objekttexten i konsekvensen så man ser att det är som ett objekt").
    Falls back to a single plain drawText call when there's nothing to
    bold — the common case for untouched free text — so this stays as
    cheap as the original code for every row that was never drag-tagged.
    `word_wrap=True` mirrors the KON column's multi-line wrapping;
    `word_wrap=False` mirrors the SG column's single-line elided text."""
    # ``QTableWidgetItem`` is allowed to return no FontRole.  A recent real
    # crash came from forwarding that ``None`` to QPainter.setFont() for an
    # empty recommendation cell.  The active painter still has the table's
    # font; a default QFont is the safe equivalent for this helper.
    base_font = QFont(base_font) if base_font is not None else QFont()
    flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    if word_wrap:
        flags |= Qt.TextFlag.TextWordWrap
    ranges = find_tag_bold_ranges(text, tags) if tags else []
    if not ranges:
        painter.setFont(base_font)
        painter.setPen(color)
        painter.drawText(rect, flags, text)
        return

    if not word_wrap:
        # Single-line elided mode (SG cell) — elide first; a bold range
        # that falls in the elided tail is simply lost, same information
        # loss the plain-text path already accepted for long descriptions.
        fm = QFontMetrics(base_font)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        ranges = find_tag_bold_ranges(text, tags)

    layout = QTextLayout(text, base_font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap if word_wrap
                     else QTextOption.WrapMode.NoWrap)
    layout.setTextOption(opt)

    bold_font = QFont(base_font)
    bold_font.setBold(True)
    bold_fmt = QTextCharFormat()
    bold_fmt.setFont(bold_font)
    formats = []
    for s, e in ranges:
        fr = QTextLayout.FormatRange()
        fr.start = s
        fr.length = e - s
        fr.format = bold_fmt
        formats.append(fr)
    layout.setFormats(formats)

    layout.beginLayout()
    y = 0.0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        # The line width must be assigned immediately after creation, before
        # positioning the line.  Assigning it afterwards leaves the first
        # line at its natural (unwrapped) width in Qt 6, which is especially
        # visible for grouped HAZOP causes where the explicit newline between
        # the two object rows could be painted on top of the first row.
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, y))
        y += line.height()
        if not word_wrap:
            break
    layout.endLayout()

    painter.setPen(color)
    layout.draw(painter, QPointF(rect.left(), rect.top()))

def effective_f_level(f_level, rrf):
    """Reduce F-level by floor(log10(rrf)) steps; minimum F=-1."""
    if rrf <= 1:
        return f_level
    reduction = int(math.log10(max(1, rrf)))
    return max(-1, f_level - reduction)


# Keep old names as aliases for backward compatibility
effective_frequency = effective_f_level
effective_likelihood = effective_f_level


def prob_to_reduction(prob_pct) -> int:
    """Convert probability % to frequency step reduction.

    10%  → 1 step  (≈ RRF 10)
    1%   → 2 steps (≈ RRF 100)
    0.1% → 3 steps (≈ RRF 1000)
    ≥100% or ≤0% → 0 steps
    """
    try:
        p = float(prob_pct)
    except (TypeError, ValueError):
        return 0
    if p <= 0 or p >= 100:
        return 0
    return int(math.floor(-math.log10(p / 100.0)))


def total_freq_reduction(base_f_level: int, safeguard_rrf: int,
                         fa_active: bool, fa_prob,
                         ignition_active: bool, ignition_prob,
                         extra_rfactors) -> tuple:
    """Return (final_f_level, total_equivalent_rrf, total_steps).

    fa_prob / ignition_prob: probability in % (10.0 = 10% = −1 step).
    extra_rfactors: iterable of dicts with 'rrf' (also treated as %) and 'active'.
    """
    # Safeguards reduce by RRF steps
    sg_steps    = int(math.log10(max(1, safeguard_rrf))) if safeguard_rrf > 1 else 0
    fa_steps    = prob_to_reduction(fa_prob)    if fa_active    else 0
    ign_steps   = prob_to_reduction(ignition_prob) if ignition_active else 0
    extra_steps = sum(
        prob_to_reduction(rf.get('rrf', 10))
        for rf in extra_rfactors
        if rf.get('active')
    )
    total_steps = sg_steps + fa_steps + ign_steps + extra_steps
    total_rrf   = 10 ** total_steps if total_steps > 0 else 1
    return max(-1, base_f_level - total_steps), total_rrf, total_steps


# ── Consequence chain definitions ────────────────────────────────────────────
# Each entry: (key, display_label, group_header_or_None)
CHAIN_ITEMS = [
    # Intermediate event
    ('loc',           'LOC — Utsläpp / läcka',                    'Intermediär händelse'),
    # Ignition outcomes
    ('fire',          'Brand (pool fire / jet fire)',              'Antändning / explosion'),
    ('flash_fire',    'Flash fire',                                None),
    ('explosion',     'Explosion (VCE / BLEVE)',                   None),
    # Toxic / environmental
    ('toxic',         'Toxisk exponering',                         'Toxisk / miljö'),
    ('environmental', 'Miljöutsläpp',                              None),
    # Human / asset
    ('personnel',     'Personskador',                              'Personell / tillgång'),
    ('fatality',      'Dödsfall',                                  None),
    ('equipment',     'Utrustningsskador',                         None),
    ('production',    'Driftstopp / produktionsbortfall',          None),
    # User-defined
    ('custom',        'Övrigt (se text)',                          'Övrigt'),
]
CHAIN_KEYS = [k for k, _, _ in CHAIN_ITEMS]


def build_consequence_text(base: str, chain: dict) -> str:
    """Build full consequence description from base event + chain selections."""
    parts = [base.strip()] if base.strip() else []
    for key, label, _ in CHAIN_ITEMS:
        if chain.get(key):
            # Use short label for the chain (without parenthetical detail)
            short = label.split('(')[0].strip().split(' — ')[-1].strip()
            parts.append(short)
    return ' → '.join(parts)


def parse_chain_from_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}
