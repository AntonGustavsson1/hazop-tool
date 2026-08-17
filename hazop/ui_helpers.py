#!/usr/bin/env python3
"""Shared Qt-dependent (but widget-independent) helper functions — split out
of hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer". Used by both tree_panel.py and hazop.py's remaining panels, so
this sits below both in the import layer graph."""

from PyQt6.QtWidgets import QCompleter, QMessageBox, QInputDialog, QLineEdit
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QFont, QFontMetrics, QTextLayout, QTextOption, QTextCharFormat

from database import get_matrix, freq_to_f_level
from pid_viewer import COMPONENT_TYPES, _equip_prefix_from_tag


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
    """_EQ_TYPE_ITEMS plus any custom equipment_type string already used
    somewhere in the catalog (2026-08-13: 'vill kunna lägga till nya
    typer av objekt som inte redan finns i listan') PLUS every name in
    the Standardobjekt list (`standard_objects` — the admin-managed
    catalogue used by the cause-suggestion forms, Inställningar →
    Standardobjekt). The two lists must "prata med varandra" (2026-08-13
    follow-up): a type added here also becomes a standard object (see
    EquipmentTagPopup._add_new_type), and a standard object added via
    Inställningar shows up here too, without either side needing a
    special-cased import of the other."""
    rows = db.conn.execute(
        "SELECT DISTINCT equipment_type FROM equipment_catalog "
        "WHERE equipment_type IS NOT NULL AND equipment_type != ''").fetchall()
    known = set(_EQ_TYPE_ITEMS)
    extra = {r[0] for r in rows} - known
    extra |= {o['name'] for o in db.standard_objects()} - known
    return _EQ_TYPE_ITEMS[:-2] + sorted(extra) + _EQ_TYPE_ITEMS[-2:]


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


def _resolve_std_deviation_id(db, deviation_description):
    """Look up the standard_deviations.id matching a node deviation's
    description text, or None if it doesn't match a standard deviation
    (e.g. a free-typed deviation name).
    """
    if not deviation_description:
        return None
    row = db.conn.execute(
        "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
        (deviation_description,)).fetchone()
    return row[0] if row else None


def _create_cause_from_pick(db, deviation_id, description, frequency):
    """Create a new cause under deviation_id from a StandardCausesPickerPopup
    pick, applying the description/likelihood/frequency consistently —
    shared by every quick-add entry point so a freshly created cause always
    starts with real content instead of a blank placeholder.

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
            "SELECT id FROM standard_causes WHERE deviation_id=? AND description=?",
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


def _draw_text_with_bold_tags(painter, rect, text, tags, base_font, color, word_wrap):
    """Draw `text` inside `rect`, rendering any whole-word occurrence of a
    member of `tags` in bold (2026-08-09, see NOTES.md "fetmarkera
    objekttexten i konsekvensen så man ser att det är som ett objekt").
    Falls back to a single plain drawText call when there's nothing to
    bold — the common case for untouched free text — so this stays as
    cheap as the original code for every row that was never drag-tagged.
    `word_wrap=True` mirrors the KON column's multi-line wrapping;
    `word_wrap=False` mirrors the SG column's single-line elided text."""
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
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, y))
        y += line.height()
        if not word_wrap:
            break
    layout.endLayout()

    painter.setPen(color)
    layout.draw(painter, QPointF(rect.left(), rect.top()))

