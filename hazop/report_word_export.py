"""Swedish HAZOP report, using the application's canonical Office exports.

The standard report replaces the historical customer-specific report text.
Project prose is taken from named Project / Egna fält entries; no engineering
assumptions, approvals or missing assessments are inferred by the exporter.
"""

from collections import Counter
from contextlib import contextmanager
from copy import copy, deepcopy
from pathlib import Path
import os
import re
import sqlite3
import tempfile

import database
from worksheet_export import _worksheet_rows
from worksheet_word_export import (
    PAPER_SIZES_MM, _add_node_table, _group_rows, _set_cell_borders,
    _set_cell_shading, _set_repeat_table_header,
)
from recommendations_word_export import _add_recommendation_table
from report_branding import new_report_document, apply_report_fonts


# These labels are also the supported field names in Projekt > Egna fält.
REPORT_FIELDS = (
    'Rapportnummer', 'Rapportdatum', 'Rapportrevision', 'Rapportstatus',
    'Distribution', 'Uppdragsansvarig', 'Kontaktperson kund', 'Kontaktuppgifter kund',
    'Kontaktperson', 'Kontaktuppgifter', 'Utfärdad av', 'Granskad av',
    'Framtagen av', 'Kvalitetsgranskad av', 'Godkänd av',
    'Kontorsadress ProSa', 'Kontaktuppgifter ProSa', 'Kundadress',
    'Bakgrund', 'Syfte', 'Omfattning', 'Avgränsningar', 'Driftfall',
    'Analysförutsättningar', 'Övriga referensdokument', 'Metodreferens',
    'Riskacceptanskriterier', 'Frekvensunderlag', 'Barriärunderlag',
    'Resultat och slutsatser', 'Uppföljning',
)
MISSING_PREFIX = '[KOMPLETTERA: '


def missing(label):
    return f'{MISSING_PREFIX}{label}]'


def _value(value, label):
    return str(value).strip() if value is not None and str(value).strip() else missing(label)


@contextmanager
def _report_snapshot(db):
    """Stable read-only snapshot; no migrations, commits or source DB changes.

The existing Office exporters use the process-wide matrix cache. Bind it to
this snapshot for the synchronous export and restore the previous binding
even on failure. Never process Qt events while this context is active.
"""
    if db.conn.in_transaction:
        raise ValueError('Avsluta pågående databasändring innan rapporten exporteras.')
    snapshot = copy(db)
    snapshot.conn = sqlite3.connect(':memory:')
    db.conn.backup(snapshot.conn)
    snapshot.conn.row_factory = sqlite3.Row
    snapshot.conn.execute('PRAGMA query_only=ON')
    cache = database._risk_matrix_cache
    old_db, old_matrix = cache._db, cache._current_matrix
    try:
        database.load_matrix(snapshot)
        yield snapshot
    finally:
        cache._db, cache._current_matrix = old_db, old_matrix
        snapshot.conn.close()


def _worksheet_references(rows):
    """Use displayed worksheet numbers, including grouped deviation numbers.

The older recommendation exporter enumerates raw deviation rows. That can
disagree with Worksheet when two DB deviations have the same guide word.
Reading the canonical worksheet avoids inventing a third numbering scheme.
"""
    result = {}
    for row in rows:
        consequence_id = row['merge_key'][3]
        if consequence_id is None or consequence_id in result:
            continue
        numbers = [re.match(r'^(\d+)\.', row['values'][column] or '')
                   for column in (0, 1, 2, 4)]
        if all(numbers):
            result[consequence_id] = '1.' + '.'.join(m.group(1) for m in numbers)
    return result


def collect_report_data(db):
    """Return source values and canonical rows; never treat blanks as approval."""
    fields = {}
    custom = db.project_custom_fields()
    for item in custom:
        key = (item['name'] or '').strip().casefold()
        if key:
            fields.setdefault(key, []).append(str(item['value'] or '').strip())

    def field(label):
        values = list(dict.fromkeys(v for v in fields.get(label.casefold(), []) if v))
        if len(values) > 1:
            return missing(f'{label} har flera värden: ' + ' / '.join(values))
        return values[0] if values else ''

    rows = list(_worksheet_rows(db))
    refs = _worksheet_references(rows)
    recommendations = [dict(r) for r in db.all_recommendations()]
    recommendation_rows = []
    for rec in recommendations:
        ids = db.consequences_for_recommendation(rec['id'])
        reference_values = [refs[cid] if cid in refs else missing('kontrollera scenariolänk')
                            for cid in ids]
        recommendation_rows.append([
            f"{int(rec['display_number']):03d}",
            str(rec.get('description') or ''),
            str(rec.get('responsible') or ''),
            str(rec.get('due_date') or ''),
            str(rec.get('status') or ''),
            ', '.join(reference_values) or missing('scenariokoppling eller fristående åtgärd'),
        ])

    nodes = [dict(r) for r in db.nodes()]
    consequences = {row['merge_key'][3] for row in rows
                    if row['merge_key'][3] is not None}
    described = {row['merge_key'][3] for row in rows
                 if row['merge_key'][3] is not None and row['values'][4].strip()}
    assessed = {row['merge_key'][3] for row in rows if row['risk_before']}
    revisions = db.project_revisions()
    sessions = [dict(s) for s in db.list_analysis_sessions()]
    return {
        'field': field, 'custom': custom, 'nodes': nodes, 'rows': rows,
        'sessions': sessions,
        'refs': refs, 'recommendations': recommendations,
        'recommendation_rows': recommendation_rows,
        'consequence_count': len(consequences), 'described_count': len(described),
        'assessed_count': len(assessed), 'revisions': revisions,
        'latest_revision': revisions[-1] if revisions else {},
        'matrix': database._normalise_matrix(deepcopy(database.get_matrix())),
    }


def _highlight(paragraph):
    """Highlight explicit missing-value tokens, without coloring risk cells."""
    from docx.enum.text import WD_COLOR_INDEX
    for run in list(paragraph.runs):
        if MISSING_PREFIX not in run.text and '[?]' not in run.text:
            continue
        parts = re.split(r'(\[KOMPLETTERA: [^\]]*\]|\[\?\])', run.text)
        # Our generated paragraphs/cells have plain text runs only.
        run.text = parts[0]
        previous = run._r
        for part in parts[1:]:
            if not part:
                continue
            new_run = paragraph.add_run(part)
            if run._r.rPr is not None:
                new_run._r.insert(0, deepcopy(run._r.rPr))
            previous.addnext(new_run._r)
            previous = new_run._r
            if part.startswith(MISSING_PREFIX) or part == '[?]':
                new_run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _highlight_document(document):
    # Include runs inside retained content controls and header text boxes.
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    roots = [document.element]
    roots += [p.element for p in document.part.package.parts
              if ('header' in str(p.partname) or 'footer' in str(p.partname))
              and hasattr(p, 'element')]
    for root in roots:
        for run in list(root.iter(qn('w:r'))):
            texts = list(run.iter(qn('w:t')))
            content = ''.join(t.text or '' for t in texts)
            if MISSING_PREFIX not in content and '[?]' not in content:
                continue
            parts = re.split(r'(\[KOMPLETTERA: [^\]]*\]|\[\?\])', content)
            parent, index = run.getparent(), run.getparent().index(run)
            for part in parts:
                if not part: continue
                new_run = OxmlElement('w:r')
                props = run.find(qn('w:rPr'))
                props = deepcopy(props) if props is not None else OxmlElement('w:rPr')
                if part.startswith(MISSING_PREFIX) or part == '[?]':
                    highlight = OxmlElement('w:highlight')
                    highlight.set(qn('w:val'), 'yellow')
                    props.append(highlight)
                new_run.append(props)
                text = OxmlElement('w:t')
                text.set(qn('xml:space'), 'preserve'); text.text = part
                new_run.append(text); parent.insert(index, new_run); index += 1
            parent.remove(run)


def _field_elements(instruction, cached='1', run_properties=None):
    """Build a complex Word field with a visible cached result.

    Keeping the result in an ordinary ``w:r`` makes captions and references
    readable both before Word updates the fields and through python-docx.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    begin_run = OxmlElement('w:r')
    begin = OxmlElement('w:fldChar')
    begin.set(qn('w:fldCharType'), 'begin')
    begin.set(qn('w:dirty'), 'true')
    begin_run.append(begin)

    instruction_run = OxmlElement('w:r')
    instruction_text = OxmlElement('w:instrText')
    instruction_text.set(qn('xml:space'), 'preserve')
    instruction_text.text = instruction
    instruction_run.append(instruction_text)

    separate_run = OxmlElement('w:r')
    separate = OxmlElement('w:fldChar')
    separate.set(qn('w:fldCharType'), 'separate')
    separate_run.append(separate)

    result_run = OxmlElement('w:r')
    if run_properties is not None:
        result_run.append(deepcopy(run_properties))
    text = OxmlElement('w:t')
    text.set(qn('xml:space'), 'preserve')
    text.text = cached
    result_run.append(text)

    end_run = OxmlElement('w:r')
    end = OxmlElement('w:fldChar')
    end.set(qn('w:fldCharType'), 'end')
    end_run.append(end)
    return [begin_run, instruction_run, separate_run, result_run, end_run]


def _field_run(paragraph, instruction, cached='1'):
    for element in _field_elements(instruction, cached):
        paragraph._p.append(element)


def _table_bookmark_name(number):
    normalized = re.sub(r'[^A-Za-z0-9]+', '_', str(number).replace('.', '-')).strip('_')
    return f'tabell_{normalized}'


def _next_bookmark_id(paragraph):
    from docx.oxml.ns import qn
    values = []
    for element in paragraph.part.element.iter(qn('w:bookmarkStart')):
        try:
            values.append(int(element.get(qn('w:id'))))
        except (TypeError, ValueError):
            continue
    return max(values, default=0) + 1


def _append_bookmarked_table_number(paragraph, number):
    """Add an editable caption number and bookmark it for Word REF fields."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    normalized = str(number).replace('.', '-')
    match = re.match(r'^(.*?)(\d+)([A-Za-z]?)$', normalized)
    if not match:
        raise ValueError(f'Ogiltigt tabellnummer: {number}')
    prefix, ordinal, suffix = match.groups()
    bookmark_id = str(_next_bookmark_id(paragraph))
    start = OxmlElement('w:bookmarkStart')
    start.set(qn('w:id'), bookmark_id)
    start.set(qn('w:name'), _table_bookmark_name(normalized))
    paragraph._p.append(start)
    paragraph.add_run(prefix)
    _field_run(paragraph, f'SEQ Tabell \\r {ordinal} \\* ARABIC', ordinal)
    if suffix:
        paragraph.add_run(suffix)
    end = OxmlElement('w:bookmarkEnd')
    end.set(qn('w:id'), bookmark_id)
    paragraph._p.append(end)


_TABLE_REFERENCE_RE = re.compile(r'\b([Tt]abell)(\s+)((?:B\d+|\d+)-\d+[A-Za-z]?)\b')


def _replace_table_references(document):
    """Replace visible table-number text in prose with bookmarked REF fields."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    bookmarks = {
        element.get(qn('w:name'))
        for element in document.element.iter(qn('w:bookmarkStart'))
    }
    for paragraph in document.paragraphs:
        if paragraph.style.name == 'Caption':
            continue
        for run in list(paragraph._p.findall(qn('w:r'))):
            texts = list(run.iter(qn('w:t')))
            content = ''.join(text.text or '' for text in texts)
            matches = [
                match for match in _TABLE_REFERENCE_RE.finditer(content)
                if _table_bookmark_name(match.group(3)) in bookmarks
            ]
            if not matches:
                continue
            parent = run.getparent()
            index = parent.index(run)
            properties = run.find(qn('w:rPr'))
            cursor = 0

            def insert_text(value):
                nonlocal index
                if not value:
                    return
                new_run = OxmlElement('w:r')
                if properties is not None:
                    new_run.append(deepcopy(properties))
                text = OxmlElement('w:t')
                text.set(qn('xml:space'), 'preserve')
                text.text = value
                new_run.append(text)
                parent.insert(index, new_run)
                index += 1

            for match in matches:
                insert_text(content[cursor:match.start(3)])
                number = match.group(3)
                for element in _field_elements(
                        f'REF {_table_bookmark_name(number)} \\h',
                        number,
                        properties):
                    parent.insert(index, element)
                    index += 1
                cursor = match.end(3)
            insert_text(content[cursor:])
            parent.remove(run)


def _page_setup(section, landscape=False, paper='A4'):
    from docx.enum.section import WD_ORIENT
    from docx.shared import Mm
    width, height = PAPER_SIZES_MM[paper] if landscape else (210, 297)
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = Mm(width), Mm(height)
    margin = 12 if landscape else 25
    section.left_margin = section.right_margin = Mm(margin)
    section.top_margin, section.bottom_margin = Mm(40), Mm(20)
    section.header_distance = section.footer_distance = Mm(9)
    return width, margin


def _table(document, headers, rows, widths=None):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt
    table = document.add_table(rows=1, cols=len(headers))
    table.autofit = False
    if widths is None:
        widths = [160 / len(headers)] * len(headers)
    for column, width in zip(table.columns, widths):
        column.width = Mm(width)
    for index, values in enumerate([headers] + list(rows)):
        cells = table.rows[0].cells if index == 0 else table.add_row().cells
        for col, (cell, value) in enumerate(zip(cells, values)):
            cell.width = Mm(widths[col])
            cell.text = str(value)
            cell.vertical_alignment = 1
            _set_cell_borders(cell, 'D9D9D9')
            if index == 0:
                _set_cell_shading(cell, 'EEECE1')
            for p in cell.paragraphs:
                p.paragraph_format.left_indent = None
                p.paragraph_format.first_line_indent = None
                p.paragraph_format.right_indent = None
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(3)
                p.paragraph_format.keep_with_next = False
                for run in p.runs:
                    run.font.size = Pt(9)
                    run.bold = index == 0
    _set_repeat_table_header(table.rows[0])
    document.add_paragraph().paragraph_format.space_after = Pt(0)
    return table


def _caption(document, number, title):
    number = str(number).replace('.', '-')
    paragraph = document.add_paragraph(style='Caption')
    paragraph.add_run('Tabell ')
    _append_bookmarked_table_number(paragraph, number)
    paragraph.add_run(f' {title}')
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_before = Pt(3)
    paragraph.paragraph_format.space_after = Pt(3)
    paragraph.paragraph_format.left_indent = None
    paragraph.paragraph_format.first_line_indent = None
    paragraph.paragraph_format.right_indent = None
    paragraph.paragraph_format.keep_with_next = True
    return paragraph


PROSE_INTROS = {
    'Bakgrund': 'Detta avsnitt beskriver bakgrunden till studien och de förhållanden som gjorde analysen aktuell. Informationen ger läsaren det sammanhang som behövs för att förstå studiens inriktning och resultat.',
    'Syfte': 'Detta avsnitt anger vad studien skulle uppnå och hur resultatet är avsett att användas i det fortsatta arbetet.',
    'Omfattning': 'Detta avsnitt anger vilka systemdelar, funktioner och gränssnitt som ingick i studien. Omfattningen ska läsas tillsammans med nodindelningen och de ritningar som anges i rapporten.',
    'Avgränsningar': 'Detta avsnitt beskriver de avgränsningar och antaganden som gällde under studien. De behöver beaktas när resultaten används i projektering, drift eller fortsatt riskhantering.',
    'Övriga referensdokument': 'Utöver ritningsunderlaget användes de beskrivningar, instruktioner och övriga dokument som anges nedan. Förteckningen visar vilket underlag som var tillgängligt när studien genomfördes.',
    'Riskacceptanskriterier': 'Acceptanskriterierna beskriver hur risknivåerna ska tolkas och hanteras efter studien.',
    'Frekvensunderlag': 'Frekvensbedömningen grundades på analysgruppens gemensamma bedömning av hur ofta den aktuella orsaken eller händelsen kan inträffa.',
    'Barriärunderlag': 'Detta avsnitt beskriver de principer som användes när barriärer och enablers beaktades i riskbedömningen.',
    'Resultat och slutsatser': 'Detta avsnitt sammanfattar studiens viktigaste resultat och de frågor som behöver hanteras vidare. Den fullständiga redovisningen finns i HAZOP-protokollet och rekommendationsregistret.',
    'Uppföljning': 'Detta avsnitt beskriver hur rekommendationerna ska behandlas efter studien. Ansvar, beslut, tidplan och underlag för verifierad stängning bör dokumenteras för varje rekommendation.',
    'Metodreferens': 'Särskilda projektinstruktioner eller andra metodreferenser som användes under studien anges nedan.',
}


def _prose(document, data, label, heading=None, level=2):
    document.add_heading(heading or label, level)
    if label in PROSE_INTROS:
        document.add_paragraph(PROSE_INTROS[label])
    for text in _value(data['field'](label), label).split('\n'):
        document.add_paragraph(text)


def _chapter(document, title):
    """Start a top-level chapter on a normal body page.

    The source header contains anchored artwork.  A new linked section is more
    reliable than a paragraph-level page break: Word otherwise occasionally
    draws a generated chapter's first page in the header area.
    """
    from docx.enum.section import WD_SECTION_START
    document.add_section(WD_SECTION_START.NEW_PAGE)
    return document.add_heading(title, 1)


def _numbered_list(document, items):
    """Add an independent numbered list that always starts at one."""
    from docx.shared import Pt
    style_num_id = document.styles['List Number']._element.pPr.numPr.numId.val
    numbering = document.part.numbering_part.element
    abstract_num_id = numbering.num_having_numId(style_num_id).abstractNumId.val
    sequence = numbering.add_num(abstract_num_id)
    sequence.add_lvlOverride(0).add_startOverride(1)
    paragraphs = []
    for item in items:
        paragraph = document.add_paragraph(item, 'List Number')
        paragraph.paragraph_format.space_after = Pt(4)
        for run in paragraph.runs:
            run.font.size = Pt(11)
        num_pr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
        num_pr.get_or_add_numId().val = sequence.numId
        paragraphs.append(paragraph)
    return paragraphs


def _matrix_display_values(matrix):
    """Return headers, row values and source indexes in the GUI's orientation."""
    # Data is always stored as [consequence][frequency]. The two direction
    # flags describe the visual axes: reversed X puts the high value at the
    # left, while reversed Y puts the low value at the top. This is the same
    # convention used by both matrix editors and the scenario popup.
    frequency_indices = list(range(matrix['cols']))
    consequence_indices = list(range(matrix['rows']))
    x_frequency = matrix.get('x_axis', 'frequency') == 'frequency'
    horizontal = frequency_indices[:] if x_frequency else consequence_indices[:]
    vertical = consequence_indices[:] if x_frequency else frequency_indices[:]
    if matrix.get('x_reversed'):
        horizontal.reverse()
    if not matrix.get('y_reversed'):
        vertical.reverse()
    x_codes, y_codes = matrix['x_codes'], matrix['y_codes']
    headers = [('Konsekvens / frekvens' if x_frequency else 'Frekvens / konsekvens')]
    headers += [(x_codes if x_frequency else y_codes)[i] for i in horizontal]
    rows = []
    for vi in vertical:
        values = [(y_codes if x_frequency else x_codes)[vi]]
        for hi in horizontal:
            ci, fi = (vi, hi) if x_frequency else (hi, vi)
            values.append(matrix['cell_labels'][ci][fi])
        rows.append(values)
    return headers, rows, horizontal, vertical, x_frequency


def _add_matrix(document, db, data):
    matrix = data['matrix']
    document.add_page_break()
    document.add_heading('4 Riskbedömning', 1)
    document.add_paragraph(
        'Analysgruppen använde studiens riskmatris för att bedöma de scenarier '
        'som identifierades. Matrisen gav en gemensam grund för att väga '
        'samman bedömd frekvens och konsekvens. Resultaten i HAZOP-protokollet '
        'ska läsas tillsammans med skalorna och definitionerna i detta kapitel.')
    document.add_paragraph(
        'Protokollet redovisar bedömningen före och efter tillgodoräknade '
        'barriärer samt de enablers som har beaktats. Färgen i matrisen visar '
        'risknivån, och tabell 4-2 beskriver hur respektive nivå ska hanteras.')
    document.add_heading('4.1 Riskmatris och acceptanskriterier', 2)
    document.add_paragraph(
        'Tabell 4-1 visar den riskmatris som användes i studien. Axelriktning, '
        'färger och nivånamn framgår av tabellen.')
    _caption(document, '4-1', 'Riskmatris')
    headers, rows, horizontal, vertical, x_frequency = _matrix_display_values(matrix)
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt
    table = document.add_table(rows=2, cols=len(headers))
    table.autofit = False
    widths = [32] + [18] * (len(headers) - 1)
    for col, width in zip(table.columns, widths):
        col.width = Mm(width)
    table.cell(0, 0).text = ''
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 0).text = 'Konsekvens' if x_frequency else 'Frekvens'
    table.cell(0, 1).merge(table.cell(0, len(headers) - 1))
    table.cell(0, 1).text = 'Frekvens' if x_frequency else 'Konsekvens'
    for col, value in enumerate(headers[1:], 1):
        table.cell(1, col).text = str(value)
    for row_index, values in enumerate(rows, 2):
        cells = table.add_row().cells
        cells[0].text = str(values[0])
        for col, value in enumerate(values[1:], 1):
            cells[col].text = str(value)
            vi, hi = vertical[row_index - 2], horizontal[col - 1]
            ci, fi = (vi, hi) if x_frequency else (hi, vi)
            color = (matrix.get('cell_colors') or [])[ci][fi]
            _set_cell_shading(cells[col], str(color).lstrip('#'))
            fg = (matrix.get('cell_fg_colors') or [])[ci][fi]
            for p in cells[col].paragraphs:
                for run in p.runs:
                    if fg:
                        from docx.shared import RGBColor
                        run.font.color.rgb = RGBColor.from_string(str(fg).lstrip('#'))
        for cell in cells:
            _set_cell_borders(cell, 'D9D9D9')
            cell.vertical_alignment = 1
            for p in cell.paragraphs:
                p.paragraph_format.left_indent = None
                p.paragraph_format.first_line_indent = None
                p.paragraph_format.alignment = 1
                p.paragraph_format.space_before = Pt(2)
                p.paragraph_format.space_after = Pt(2)
                for run in p.runs:
                    run.font.size = Pt(8)
    for row in table.rows[:2]:
        for cell in row.cells:
            _set_cell_shading(cell, 'EEECE1')
            _set_cell_borders(cell, 'D9D9D9')
            for p in cell.paragraphs:
                p.paragraph_format.alignment = 1
                for run in p.runs:
                    run.bold = True
                    run.font.size = Pt(8)
    # Add a table-level frame as well as per-cell borders. This keeps the
    # outside line continuous where the axis header cells are merged.
    tbl_pr = table._tbl.tblPr
    tbl_borders = tbl_pr.first_child_found_in('w:tblBorders')
    if tbl_borders is None:
        tbl_borders = OxmlElement('w:tblBorders')
        tbl_pr.append(tbl_borders)
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        element = tbl_borders.find(qn('w:' + edge))
        if element is None:
            element = OxmlElement('w:' + edge)
            tbl_borders.append(element)
        element.set(qn('w:val'), 'single')
        element.set(qn('w:sz'), '4')
        element.set(qn('w:space'), '0')
        element.set(qn('w:color'), 'D9D9D9')
    _set_repeat_table_header(table.rows[1])
    document.add_paragraph().paragraph_format.space_after = Pt(0)
    level_defs = matrix.get('risk_level_definitions') or []
    if not level_defs:
        derived = {}
        for row_colors, row_labels in zip(matrix.get('cell_colors', []), matrix.get('cell_labels', [])):
            for color, label in zip(row_colors, row_labels):
                color, label = str(color or '').strip(), str(label or '').strip()
                if color and label and color not in derived:
                    derived[color] = label
        level_defs = [{'color': color, 'label': label, 'definition': ''}
                      for color, label in derived.items()]
    if level_defs:
        document.add_paragraph(
            'Tabell 4-2 beskriver acceptanskriterierna för matrisens '
            'risknivåer. Nivånamn och färger är desamma som i tabell 4-1.')
        _caption(document, '4-2', 'Acceptanskriterier')
        level_table = _table(document, ['Risknivå', 'Definition'], [
            [item.get('label', ''), item.get('definition') or missing('risknivådefinition')]
            for item in level_defs], [70, 100])
        for row, item in zip(level_table.rows[1:], level_defs):
            color = str(item.get('color') or '')
            if color:
                cell = row.cells[0]
                _set_cell_shading(cell, color.lstrip('#'))
                try:
                    rgb = color.lstrip('#')
                    red, green, blue = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
                    foreground = '000000' if (0.299 * red + 0.587 * green + 0.114 * blue) > 160 else 'FFFFFF'
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            from docx.shared import RGBColor
                            run.font.color.rgb = RGBColor.from_string(foreground)
                except (ValueError, TypeError):
                    pass
    x_codes, y_codes = matrix['x_codes'], matrix['y_codes']
    document.add_heading('4.2 Frekvensskala', 2)
    document.add_paragraph(
        'Tabell 4-3 redovisar de frekvensnivåer som användes i '
        'scenarioanalysen. Definitionerna anger hur ofta en händelse bedöms '
        'kunna inträffa och ger stöd för jämförbara bedömningar genom studien.')
    _caption(document, '4-3', 'Frekvensnivåer och definitioner')
    _table(document, ['Nivå', 'Definition'],
           [[code, _value(label, 'frekvensdefinition')]
            for code, label in zip(x_codes, matrix['x_labels'])], [25, 135])
    if data['field']('Frekvensunderlag'):
        document.add_paragraph(data['field']('Frekvensunderlag'))
    from docx.enum.section import WD_SECTION_START
    landscape_section = document.add_section(WD_SECTION_START.NEW_PAGE)
    _page_setup(landscape_section, landscape=True, paper='A4')
    document.add_heading('4.3 Konsekvensdefinitioner', 2)
    document.add_paragraph(
        'Konsekvenserna bedömdes separat för de kategorier som ingick i '
        'studien. Tabell 4-4 redovisar benämning och definition för varje '
        'konsekvensnivå inom person, miljö och ekonomi, i den mån dessa '
        'kategorier har använts.')
    categories = [dict(c) for c in db.consequence_categories()]
    definitions = db.get_severity_definitions()
    # Present one row per consequence level and one column per consequence
    # category.  Empty categories are omitted so the table stays useful for
    # projects that only assess (for example) person and environment.
    active_categories = []
    for category in categories:
        values = [definitions.get(i + 1, {}).get(category['id']) for i in range(len(y_codes))]
        if any((value or '').strip() for value in values):
            active_categories.append(category)
    consequence_rows = []
    for i, code in enumerate(y_codes):
        values = [definitions.get(i + 1, {}).get(category['id'], '') for category in active_categories]
        if any((value or '').strip() for value in values):
            consequence_rows.append([
                code, matrix['y_labels'][i],
                *[_value(value, f"definition {category['name']} {code}")
                  for category, value in zip(active_categories, values)],
            ])
    if consequence_rows:
        _caption(document, '4-4', 'Konsekvensdefinitioner')
        headers = ['Nivå', 'Benämning'] + [category['name'] for category in active_categories]
        widths = [16, 30] + [max(35, int(204 / max(1, len(active_categories))))] * len(active_categories)
        _table(document, headers, consequence_rows, widths)
    else:
        document.add_paragraph(missing('konsekvenskategorier'))
    portrait_section = document.add_section(WD_SECTION_START.NEW_PAGE)
    _page_setup(portrait_section, landscape=False, paper='A4')
    document.add_heading('4.4 Barriärer och enablers', 2)
    document.add_paragraph(
        'Barriärer redovisas under den konsekvens där analysgruppen bedömde att '
        'de gav ett relevant skydd. Enablers används för att beskriva '
        'förhållanden som påverkar sannolikheten för att händelseförloppet ska '
        'utvecklas till den angivna konsekvensen. Tabellen nedan sammanställer '
        'de typer av enablers och RRF-värden som användes i studien.')
    enabler_rrfs = {}
    for row in data['rows']:
        for rf in db.reduction_factors(row['merge_key'][3]):
            rf = dict(rf)
            description = (rf.get('description') or '').strip()
            if not description or not rf.get('active', 1):
                continue
            enabler_rrfs.setdefault(description, set()).add(
                '' if rf.get('rrf') is None else f"{float(rf['rrf']):g}")
    if enabler_rrfs:
        _caption(document, '4-5', 'Typer av använda enablers och RRF')
        _table(document, ['Typ av enabler', 'RRF'], [
            [description, ', '.join(sorted(values, key=lambda v: (v == '', float(v) if v else 0))) or missing('RRF')]
            for description, values in sorted(enabler_rrfs.items())
        ], [130, 30])
    elif data['field']('Barriärunderlag'):
        document.add_paragraph(data['field']('Barriärunderlag'))


def _add_participants(document, db, data, *, standard_template=False):
    _chapter(document, '3 Genomförande')
    document.add_paragraph(
        'HAZOP-studien genomfördes som en gemensam, tvärdisciplinär genomgång. '
        'Deltagarna bidrog med kunskap om systemets konstruktion, process, '
        'drift och underhåll. Detta kapitel beskriver hur analysen genomfördes '
        'och hur analysgruppen var sammansatt.')
    document.add_heading('3.1 Metod och arbetssätt', 2)
    document.add_paragraph(
        'Analysobjektet delades in i noder med tydliga gränser. Innan en nod '
        'analyserades bekräftade gruppen dess avsedda funktion, relevanta '
        'driftfall och kopplingar till angränsande system. Nodindelningen gav '
        'en gemensam utgångspunkt för diskussionen och minskade risken för att '
        'viktiga gränssnitt skulle förbises.')
    document.add_paragraph(
        'Under genomgången kombinerade gruppen processparametrar med HAZOP-ledord '
        'för att formulera meningsfulla avvikelser. För varje avvikelse '
        'identifierades trovärdiga orsaker och möjliga konsekvenser. Gruppen '
        'dokumenterade därefter de barriärer och enablers som bedömdes påverka '
        'scenariot.')
    document.add_paragraph('Arbetsgången har varit följande:')
    _numbered_list(document, (
        'Nodens avsedda funktion, gränser och relevanta driftfall har bekräftats.',
        'Relevanta avvikelser har formulerats med hjälp av parametrar och ledord.',
        'Trovärdiga orsaker och möjliga konsekvenser har identifierats och beskrivits.',
        'Befintliga barriärer och andra förhållanden som påverkar händelseförloppet har dokumenterats.',
        'Frekvens och konsekvens har bedömts i protokollets riskkolumner för de konsekvenskategorier som har berörts.',
        'Rekommendationer har formulerats när ytterligare utredning, verifiering eller åtgärd har bedömts behövas.',
    ))
    document.add_paragraph(
        'Riskbedömningarna genomfördes med studiens riskmatris. Gruppen bedömde '
        'först scenariot före tillgodoräknade skydd och därefter den risk som '
        'återstod när relevanta barriärer och enablers hade beaktats. Valda '
        'nivåer dokumenterades i protokollet tillsammans med scenarioinformationen.')
    document.add_paragraph(
        'Innan varje analystillfälle avslutades gick gruppen igenom de '
        'rekommendationer som hade formulerats. Genomgången användes för att '
        'förtydliga rekommendationernas innebörd och kontrollera att fortsatt '
        'hantering kunde följas upp.')
    sessions = [dict(s) for s in db.list_analysis_sessions()]
    document.add_heading('3.2 Analystillfällen', 2)
    document.add_paragraph(
        'Datum, tid och plats för genomförda analystillfällen redovisas i tabell 3-1.')
    session_rows = [[str(i), _value(s.get('date'), 'datum'),
                     _value(s.get('start_time'), 'starttid') + '–' +
                     _value(s.get('end_time'), 'sluttid'),
                     _value(s.get('location'), 'plats')]
                    for i, s in enumerate(sessions, 1)]
    _caption(document, '3.1', 'Analystillfällen')
    _table(document, ['Tillfälle', 'Datum', 'Tid', 'Plats'], session_rows or [
        ['1', missing('analystillfälle'), '', '']], [17, 28, 43, 72])
    document.add_heading('3.3 Analysgrupp och närvaro', 2)
    document.add_paragraph(
        'De personer som deltog i studien redovisas i tabell 3-2 tillsammans '
        'med registrerade roller och övriga deltagaruppgifter.')
    participants = [dict(p) for p in db.list_participants()]
    columns = db.list_participant_columns()
    values = db.get_participant_column_values()
    # Long-form optional attributes prevent a dozen custom columns from
    # squeezing names/attendance into unreadable portrait-table cells.
    participant_rows = []
    for p in participants:
        details = [f"{c['name']}: {_value(values.get((p['id'], c['id'])), c['name'])}"
                   for c in columns]
        if p.get('role'):
            details.insert(0, 'Roll: ' + p['role'])
        participant_rows.append([
            _value(p.get('first_name'), 'förnamn'), _value(p.get('last_name'), 'efternamn'),
            '\n'.join(details) or missing('företag och roll eller disciplin'),
        ])
    _caption(document, '3.2', 'Deltagare och roller')
    _table(document, ['Förnamn', 'Efternamn', 'Deltagaruppgifter'],
           participant_rows or [[missing('deltagare'), '', '']], [32, 40, 88])
    document.add_paragraph(
        'Deltagarnas närvaro vid respektive analystillfälle redovisas i tabell 3-3. '
        'Anteckningar kan användas för att ange om någon endast deltog under '
        'en del av genomgången.')
    attendance = db.get_attendance_details()
    attendance_rows = []
    short_status = len(sessions) > 5
    for p in participants:
        name = _value(' '.join(filter(None, [p['first_name'], p['last_name']])), 'deltagarnamn')
        statuses = []
        for session in sessions:
            state = attendance.get((p['id'], session['id']))
            if state:
                status = ('N' if state[0] else 'F') if short_status else (
                    'Närvarande' if state[0] else 'Frånvarande')
                note = str(state[1] or '').strip() if len(state) > 1 else ''
                statuses.append(status + ('\n' + note if note else ''))
            else:
                statuses.append(missing('närvaro'))
        attendance_rows.append([name] + statuses)
    attendance_headers = ['Deltagare'] + [s.get('date') or f'Tillfälle {i}' for i, s in enumerate(sessions, 1)]
    _caption(document, '3.3', 'Närvaro per analystillfälle')
    if attendance_rows and sessions:
        _table(document, attendance_headers, attendance_rows, [55] + [105 / len(sessions)] * len(sessions))
    elif standard_template:
        _table(
            document,
            ['Deltagare', 'Datum / analystillfälle'],
            [[missing('deltagare'), missing('närvaro')]],
            [55, 105],
        )


def _node_appendix(document, db, data):
    from docx.enum.section import WD_SECTION_START
    section = document.add_section(WD_SECTION_START.NEW_PAGE)
    _page_setup(section, landscape=True, paper='A4')
    heading = document.add_heading('Bilaga 2 HAZOP-noder', 1)
    heading.paragraph_format.page_break_before = False
    document.add_paragraph(
        'Denna bilaga redovisar den nodindelning som användes i studien. '
        'Indelningen visar vilka delar av systemet som behandlades tillsammans '
        'och ger en referens till motsvarande avsnitt i HAZOP-protokollet.')
    document.add_paragraph(
        'Nodnumreringen följer protokollets ordning. Nodernas gränser redovisas '
        'på P&ID-ritningarna i bilaga 5 när dessa markeringar har infogats.')
    sheets = {s['physical_page']: dict(s) for s in db.get_sheets()}
    rows = []
    for number, node in enumerate(data['nodes'], 1):
        pages = db.analysis_pages_for_node(node['id'])
        references = []
        for page in pages:
            sheet = sheets.get(page, {})
            references.append(
                f"PDF-sida {page + 1}: " + _value(sheet.get('drawing_number'), 'ritningsnummer') +
                ', rev. ' + _value(sheet.get('drawing_revision'), 'ritningsrevision'))
        if node.get('pid_ref'):
            references.insert(0, node['pid_ref'])
        rows.append({'Nod': f'{number} {node["name"]}',
                     'P&ID referens': '\n'.join(references),
                     'Media': node.get('media') or '',
                     'Tryck': node.get('pressure') or '',
                     'Temperatur': node.get('temperature') or ''})
    columns = ['Nod', 'P&ID referens']
    for key in ('Media', 'Tryck', 'Temperatur'):
        if any(str(row[key]).strip() for row in rows):
            columns.append(key)
    shown_fields = [name.lower() for name in columns[2:]]
    if shown_fields:
        if len(shown_fields) == 1:
            conditions = shown_fields[0]
        else:
            conditions = ', '.join(shown_fields[:-1]) + ' och ' + shown_fields[-1]
        document.add_paragraph(
            'Tabell B2-1 redovisar nodernas P&ID-referenser samt registrerade '
            f'uppgifter om {conditions}.')
    else:
        document.add_paragraph(
            'Tabell B2-1 redovisar noderna och deras P&ID-referenser.')
    _caption(document, 'B2-1', 'Nod- och processuppgifter')
    if len(columns) > 2:
        remaining = 267 - 45 - 105
        widths = [45, 105] + [remaining / (len(columns) - 2)] * (len(columns) - 2)
    else:
        widths = [60, 207]
    _table(document, columns, [[row[key] or missing(key.lower()) for key in columns] for row in rows] or [[missing('noder'), '']], widths)


def _annotated_worksheet_rows(db, rows):
    """Return protocol rows without editorial completion markers."""
    return deepcopy(rows)


def build_report(db, *, paper_size='A3', standard_template=False):
    """Build the report in memory. Caller owns the matrix/snapshot context."""
    from docx.enum.section import WD_SECTION_START
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    def count_phrase(count, singular, plural=None):
        return f"{count} {singular if count == 1 else (plural or singular + 'er')}"

    data = collect_report_data(db)
    closed_statuses = {'stängd', 'implementerad', 'avslutad', 'ej implementerad'}
    open_recommendation_count = sum(
        1 for recommendation in data['recommendations']
        if str(recommendation.get('status') or '').strip().casefold() not in closed_statuses)
    recommendation_nodes = Counter()
    for recommendation in data['recommendation_rows']:
        linked_nodes = set()
        for reference in str(recommendation[5]).split(', '):
            match = re.match(r'^\d+\.(\d+)\.', reference)
            if match:
                linked_nodes.add(int(match.group(1)))
        recommendation_nodes.update(linked_nodes)
    project = _value(db.get_config('project_name', ''), 'system')
    client = _value(db.get_config('project_client', ''), 'kund')
    field = data['field']
    project_number_raw = (db.get_config('project_number', '') or '').strip()
    suggested_report_number = f'{project_number_raw}-R-001' if project_number_raw else ''
    report_number = _value(field('Rapportnummer') or db.get_config('report_number', '') or suggested_report_number, 'rapportnummer')
    revision = _value(field('Rapportrevision') or data['latest_revision'].get('label'), 'rapportrevision')
    # The cover date is the date of the latest revision; a separate report
    # date can otherwise become inconsistent with the revision history.
    date = _value(data['latest_revision'].get('date'), 'revisionsdatum')
    issued_by = field('Utfärdad av') or field('Framtagen av') or db.get_config('report_prepared_by', '')
    reviewed_by = field('Granskad av') or field('Godkänd av') or field('Kvalitetsgranskad av') or db.get_config('report_reviewed_by', '') or db.get_config('report_approved_by', '')
    contact_person = field('Kontaktperson') or field('Kontaktperson kund') or db.get_config('report_contact_by', '')
    company_name = db.get_config('company_name', 'ProSa Process Safety Consulting AB') or 'ProSa Process Safety Consulting AB'
    company_address = field('Kontorsadress ProSa') or '\n'.join(v for v in (
        db.get_config('company_street', ''),
        ' '.join(v for v in (db.get_config('company_postal_code', ''), db.get_config('company_city', '')) if v),
        db.get_config('company_country', ''),) if (v or '').strip())
    company_contact = field('Kontaktuppgifter ProSa') or db.get_config('company_contact', '')
    client_person = field('Kontaktperson') or field('Kontaktperson kund') or db.get_config('report_contact_by', '')
    if client_person and client and client_person.casefold().startswith(client.casefold()):
        client_person = client_person[len(client):].lstrip(' :\n\r-')

    values = {
        'TITLE': 'HAZOP för ' + project, 'CLIENT': client,
        'REPORT_NUMBER': report_number, 'REVISION': revision, 'DATE': date,
        'STATUS': revision,
        'DISTRIBUTION': _value(field('Distribution') or db.get_config('report_distribution', '') or 'Enligt kundens anvisning', 'distribution'),
        'AUTHOR': _value(issued_by, 'utfärdad av'),
        'REVIEWER': _value(reviewed_by, 'granskad av'),
        'PROSA_ADDRESS': _value(company_address, 'ProSa-adress'),
        'CLIENT_ADDRESS': field('Kundadress') or '',
        'MANAGER': field('Uppdragsansvarig') or issued_by or contact_person,
        # The assignment/contact person is shown in the customer block and in
        # the running header; do not repeat an issuer name under ProSa.
        'PROSA_CONTACT': '',
        'CLIENT_PERSON': _value(client_person, 'kontaktperson'),
        'CLIENT_CONTACT': field('Kontaktuppgifter') or field('Kontaktuppgifter kund') or '',
    }
    revision_rows = [{
        'REVISION': _value(r.get('label'), 'revision'),
        'REVISION_DATE': _value(r.get('date'), 'revisionsdatum'),
        'REVISION_DESCRIPTION': _value(r.get('description'), 'revisionsbeskrivning'),
        'REVISION_AUTHOR': _value(r.get('performed_by'), 'utfört av för revision ' + str(r.get('label') or '')),
    } for r in data['revisions']] or [{
        'REVISION': revision, 'REVISION_DATE': missing('revisionsdatum'),
        'REVISION_DESCRIPTION': missing('revisionsbeskrivning'),
        'REVISION_AUTHOR': missing('utfört av'),
    }]
    document = new_report_document(values, revision_rows)
    # The final source section contains ProSa's complete running header:
    # round logo, wordmark, metadata table and green rule.  Retain its XML
    # and image relationships before generated sections change the document's
    # section indexes.
    source_header_part = document.sections[-1].header.part
    source_header_elements = deepcopy(list(source_header_part._element))
    source_header_relationships = tuple(source_header_part.rels.items())
    for text in document.element.iter(qn('w:t')):
        if text.text:
            text.text = text.text.replace('Version ', 'Revision ')
    for cell in document.tables[0].rows[3].cells:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.text = run.text.replace('Rev:', 'Revision:')
    for section in document.sections:
        for table in section.header.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.text = run.text.replace('Status\n', 'Revision\n')
    styles = document.styles
    styles['Heading 1'].paragraph_format.page_break_before = False
    if 'TOC Heading' not in styles:
        styles.add_style('TOC Heading', WD_STYLE_TYPE.PARAGRAPH).base_style = styles['Heading 1']
    if 'Appendix Node Heading' not in styles:
        node_heading_style = styles.add_style(
            'Appendix Node Heading', WD_STYLE_TYPE.PARAGRAPH)
        node_heading_style.base_style = styles['Heading 2']
        node_heading_style.paragraph_format.space_before = Pt(8)
        node_heading_style.paragraph_format.space_after = Pt(4)
        node_heading_style.paragraph_format.page_break_before = False
        outline_level = OxmlElement('w:outlineLvl')
        outline_level.set(qn('w:val'), '9')
        node_heading_style.element.get_or_add_pPr().append(outline_level)
    for style_name, size, after in (('TOC 1', 10, 1), ('TOC 2', 9, 0)):
        if style_name in styles:
            styles[style_name].font.size = Pt(size)
            styles[style_name].paragraph_format.space_before = Pt(0)
            styles[style_name].paragraph_format.space_after = Pt(after)
            styles[style_name].paragraph_format.line_spacing = 1
    document.core_properties.title = values['TITLE']
    document.core_properties.author = issued_by or ''
    document.core_properties.subject = 'HAZOP-analys utan SIL-bedömning'
    from docx.shared import Pt, RGBColor

    def _front_heading(text):
        """Front-matter heading excluded from the generated TOC."""
        paragraph = document.add_paragraph()
        paragraph.style = styles['Normal']
        paragraph.paragraph_format.space_before = Pt(8)
        paragraph.paragraph_format.space_after = Pt(6)
        run = paragraph.add_run(text)
        run.bold = False
        run.font.size = Pt(16)
        run.font.color.rgb = RGBColor(91, 145, 35)
        return paragraph

    _front_heading('Sammanfattning')
    document.add_paragraph(
        f'Denna rapport redovisar den HAZOP-studie som har genomförts för '
        f'{project} vid {client}. Studien har granskat hur avvikelser från '
        'systemets avsedda funktion kan uppstå, vilka konsekvenser de kan få '
        'och vilka befintliga skydd som påverkar händelseförloppet. Resultatet '
        'ska användas som underlag för fortsatt riskhantering av systemet.')
    if not standard_template:
        session_count = len(data['sessions'])
        session_label = 'analystillfälle' if session_count == 1 else 'analystillfällen'
        document.add_paragraph(
            f'Studien genomfördes vid {session_count} {session_label}.')
        if data['sessions']:
            document.add_paragraph(
                'HAZOP-studien genomfördes vid följande tillfälle:'
                if len(data['sessions']) == 1 else
                'HAZOP-studien genomfördes vid följande tillfällen:')
            session_lines = []
            for session in data['sessions']:
                date_text = session.get('date') or missing('analysdatum')
                location = session.get('location') or missing('plats')
                mode = 'digitalt' if session.get('is_digital') else ''
                time_text = '–'.join(v for v in (session.get('start_time'), session.get('end_time')) if v)
                details = f'{date_text}, {location}'
                if time_text:
                    details += f', kl. {time_text}'
                if mode:
                    details += f' ({mode})'
                session_lines.append(details)
            _numbered_list(document, session_lines)
    else:
        document.add_paragraph(missing('analystillfällen, datum och plats'))
    _front_heading('Studerade noder')
    document.add_paragraph(
        'Analysobjektet delades in i noder för att varje funktion skulle kunna '
        'granskas sammanhållet. För varje nod behandlade analysgruppen den '
        'avsedda funktionen, relevanta driftfall och gränssnitt mot angränsande '
        'system. Följande noder ingick i studien:')
    if data['nodes']:
        _numbered_list(document, [
            _value(node.get('name'), 'nodnamn') for node in data['nodes']
        ])
    else:
        _numbered_list(document, [missing('numrerad nodlista')])
    _front_heading('Huvudresultat')
    if standard_template:
        document.add_paragraph(missing('antal noder'))
    else:
        node_phrase = count_phrase(len(data['nodes']), 'nod')
        document.add_paragraph(f'Analysen omfattade {node_phrase}.')
        if data['recommendations']:
            most_items = [
                f'nod {number} ({count})'
                for number, count in recommendation_nodes.most_common(3)]
            if len(most_items) > 1:
                most = ', '.join(most_items[:-1]) + ' och ' + most_items[-1]
            else:
                most = most_items[0] if most_items else ''
            document.add_paragraph(
                'Rekommendationsregistret innehåller ' +
                (f'{open_recommendation_count} öppen rekommendation.'
                 if open_recommendation_count == 1 else
                 f'{open_recommendation_count} öppna rekommendationer.') +
                (f' De noder som har flest kopplade rekommendationer är {most}.'
                 if len(most_items) > 1 else
                 f' Den nod som har flest kopplade rekommendationer är {most}.'
                 if most else ''))
    if field('Resultat och slutsatser'):
        document.add_paragraph(field('Resultat och slutsatser'))
    document.add_paragraph(
        'Det fullständiga analysresultatet redovisas i HAZOP-protokollet och '
        'rekommendationsregistret i bilaga 3 och bilaga 4.')

    document.add_page_break()
    _front_heading('Förkortningar')
    document.add_paragraph('Följande förkortningar används återkommande i rapporten.')
    _table(document, ['Förkortning', 'Förklaring'], [
        ['BPCS', 'Basic Process Control System – ordinarie processtyrsystem'],
        ['HAZOP', 'Hazard and Operability Study – risk- och driftanalys'],
        ['P&ID', 'Piping and Instrumentation Diagram – rör- och instrumentdiagram'],
        ['RRF', 'Risk Reduction Factor – riskreduktionsfaktor'],
    ], [28, 132])
    document.add_page_break()
    document.add_paragraph('Innehåll', 'TOC Heading')
    _field_run(document.add_paragraph(), 'TOC \\o "1-2" \\h \\z',
               'Uppdatera innehållsförteckningen i Word med Ctrl+A och F9.')

    # The TOC field expands into several paragraphs when Word updates it. A
    # new-page section immediately after that field can leave an empty page.
    # Start the body section continuously; Word places the field-end paragraph
    # at the start of the following page when the TOC is updated.
    document.add_section(WD_SECTION_START.CONTINUOUS)
    document.add_heading('1 Inledning', 1)
    document.add_paragraph(
        f'{client} har låtit genomföra en HAZOP-studie av {project}. '
        f'{company_name} har fått i uppdrag att leda analysen och dokumentera '
        'resultatet. HAZOP är en systematisk metod för att undersöka hur en '
        'process eller ett system kan avvika från sin avsedda funktion. Under '
        'studien kombinerades fördefinierade ledord med analysgruppens kunskap '
        'om konstruktion, drift och underhåll. På så sätt kunde gruppen '
        'identifiera möjliga orsaker, konsekvenser och befintliga skydd samt '
        'uppmärksamma frågor som kan påverka säkerhet eller drift. Rapporten '
        'beskriver studiens förutsättningar, genomförande och resultat.')
    document.add_heading('1.1 Bakgrund', 2)
    document.add_paragraph(
        f'Bakgrunden till studien är den aktuella utformningen av {project}. '
        'Analysgruppen behövde därför gå igenom systemets funktioner, gränssnitt '
        'och planerade driftfall på ett sammanhållet sätt. Resultatet har '
        'dokumenterats i ett gemensamt protokoll som kan användas vid fortsatt '
        'riskhantering.')
    if field('Bakgrund'):
        document.add_paragraph(field('Bakgrund'))
    document.add_heading('1.2 Syfte och omfattning', 2)
    document.add_paragraph(
        'Syftet med studien var att identifiera risker och operabilitetsproblem '
        'inom det analyserade systemet. Analysgruppen bedömde de identifierade '
        'scenarierna och formulerade rekommendationer när ytterligare utredning, '
        'verifiering eller åtgärd bedömdes vara motiverad.')
    if field('Syfte'):
        document.add_paragraph(field('Syfte'))
    if field('Omfattning'):
        document.add_paragraph(field('Omfattning'))
    if data['nodes']:
        document.add_paragraph(
            f"Analysen har omfattat {count_phrase(len(data['nodes']), 'nod')}. "
            'Nodindelningen och tillhörande P&ID-referenser redovisas i bilaga 2.')
    else:
        document.add_paragraph(missing('omfattning och nodindelning'))
    if field('Driftfall'):
        document.add_paragraph('De driftsituationer som har ingått har varit: ' + field('Driftfall'))
    document.add_heading('1.3 Avgränsningar', 2)
    document.add_paragraph(
        'Studien omfattade de systemdelar och gränssnitt som framgår av '
        'nodindelningen. Bedömningarna grundades på de ritningar och övriga '
        'dokument som anges i rapporten.')
    if field('Avgränsningar'):
        document.add_paragraph(field('Avgränsningar'))
    if field('Analysförutsättningar'):
        document.add_paragraph('Analysen har utgått från följande dokumenterade förutsättningar: ' + field('Analysförutsättningar'))

    _chapter(document, '2 Dokumentunderlag')
    document.add_paragraph(
        'Studien genomfördes med stöd av de ritningar och den övriga dokumentation som '
        'var tillgängliga för analysgruppen. Underlaget användes för att '
        'fastställa nodernas gränser, beskriva systemets avsedda funktion och '
        'bedöma de scenarier som behandlades. Dokumentnummer och revisioner '
        'redovisas i detta kapitel så att analysens förutsättningar kan följas.')
    other_documents = field('Övriga referensdokument')
    if other_documents:
        document.add_heading('2.1 Ritningsunderlag', 2)
    document.add_paragraph(
        'P&ID-ritningarna och övriga registrerade dokument för riskanalysen redovisas i '
        'tabell 2-1. Dessa dokument utgjorde underlag för nodindelningen och '
        'den efterföljande scenarioanalysen.')
    sheets = [dict(s) for s in db.get_sheets()]
    _caption(document, '2.1', 'Dokumentunderlag')
    _table(document, ['Dokumentnummer', 'Dokumenttitel', 'Revision', 'Datum', 'PDF sida'], [
        [_value(s.get('drawing_number'), 'ritningsnummer'),
         _value(s.get('drawing_name'), 'ritningsnamn'),
         _value(s.get('drawing_revision'), 'revision'),
         _value(s.get('drawing_date'), 'ritningsdatum'), str(s['physical_page'] + 1)]
        for s in sheets] or [[missing('referensdokument'), '', '', '', '']], [34, 51, 23, 32, 20])
    if other_documents:
        document.add_heading('2.2 Övrigt dokumentunderlag', 2)
        document.add_paragraph(PROSE_INTROS['Övriga referensdokument'])
        for text in other_documents.split('\n'):
            document.add_paragraph(text)
    _add_participants(document, db, data, standard_template=standard_template)
    if standard_template:
        _chapter(document, '4 Riskbedömning')
        document.add_paragraph(
            'Riskbedömningen ger analysgruppen en gemensam grund för att värdera '
            'de scenarier som identifieras. Detta kapitel redovisar den matris, '
            'de skalor och de definitioner som ska användas i studien.')
        document.add_heading('4.1 Riskmatris och acceptanskriterier', 2)
        document.add_paragraph(
            'Tabell 4-1 ska redovisa studiens riskmatris med valda axlar, '
            'nivånamn och färger. Acceptanskriterierna för risknivåerna ska '
            'redovisas i tabell 4-2.')
        _caption(document, '4-1', 'Riskmatris')
        document.add_paragraph(missing('studiens riskmatris'))
        _caption(document, '4-2', 'Acceptanskriterier')
        document.add_paragraph(missing('acceptanskriterier för risknivåerna'))
        document.add_heading('4.2 Frekvensskala', 2)
        document.add_paragraph(
            'Tabell 4-3 ska redovisa de frekvensnivåer och definitioner som '
            'analysgruppen använder i studien.')
        _caption(document, '4-3', 'Frekvensnivåer och definitioner')
        document.add_paragraph(missing('frekvensskala och definitioner'))
        if field('Frekvensunderlag'):
            document.add_paragraph(field('Frekvensunderlag'))
        document.add_heading('4.3 Konsekvensdefinitioner', 2)
        document.add_paragraph(
            'Tabell 4-4 ska redovisa konsekvensdefinitionerna för de kategorier '
            'som ingår i studien.')
        _caption(document, '4-4', 'Konsekvensdefinitioner')
        document.add_paragraph(missing('konsekvenskategorier och definitioner'))
        document.add_heading('4.4 Barriärer och enablers', 2)
        document.add_paragraph(PROSE_INTROS['Barriärunderlag'])
        if field('Barriärunderlag'):
            document.add_paragraph(field('Barriärunderlag'))
        else:
            document.add_paragraph(missing('principer för barriärer och enablers'))
    else:
        _add_matrix(document, db, data)

    _chapter(document, '5 Resultat och uppföljning')
    document.add_paragraph(
        'Detta kapitel sammanfattar studiens resultat och beskriver hur '
        'rekommendationerna ska hanteras efter avslutad analys. Den detaljerade '
        'bakgrunden till varje rekommendation finns i HAZOP-protokollet.')
    document.add_heading('5.1 Resultat', 2)
    if standard_template:
        document.add_paragraph(missing('antal analystillfällen, noder och rekommendationer'))
    else:
        document.add_paragraph(
            f"Studien omfattade {count_phrase(len(data['sessions']), 'analystillfälle')} "
            f"och {count_phrase(len(data['nodes']), 'nod')}. Analysgruppen "
            f"registrerade {count_phrase(len(data['recommendations']), 'rekommendation')}, "
            f"varav {count_phrase(open_recommendation_count, 'öppen rekommendation', 'öppna rekommendationer')} "
            'vid utfärdande av rapport.')
    if field('Resultat och slutsatser'):
        document.add_paragraph(field('Resultat och slutsatser'))
    document.add_heading('5.2 Fortsatt hantering', 2)
    document.add_paragraph(
        'Efter studien ska varje öppen rekommendation tilldelas en ansvarig och '
        'föras till ett dokumenterat beslut. En rekommendation kan genomföras, '
        'avslås eller utredas vidare. Beslutet och dess grund ska dokumenteras '
        'tillsammans med planerat datum för genomförande eller fortsatt utredning. '
        'Rekommendationstext, ansvarig, åtgärdsdatum och koppling till berörda '
        'scenarier redovisas i tabell B4-1.')
    document.add_paragraph(
        'Alla HAZOP-rekommendationer baseras på de diskussioner och överväganden '
        'som gjordes under HAZOP-workshopen och speglar HAZOP-teamets kollektiva '
        'bedömning vid studietillfället. Även om enskilda rekommendationer inte '
        'nödvändigtvis följer beskrivningarna nedan kan de generellt prioriteras '
        'enligt följande ordning. Röda risker ska åtgärdas med högst prioritet, '
        'därefter gula och sist gröna:')
    _numbered_list(document, [
        'Risk reduction through inherently safer design',
        'Risk reduction using mechanical barriers',
        'Risk reduction through electrical systems',
        'Risk reduction through instrumented systems',
        'Risk reduction through procedures',
    ])
    document.add_paragraph(
        'En genomförd åtgärd ska verifieras innan rekommendationen stängs. '
        'Verifieringen ska visa att den beslutade åtgärden är införd och att '
        'den hanterar den fråga som identifierades i HAZOP-studien.')
    if field('Uppföljning'):
        document.add_paragraph(field('Uppföljning'))

    _chapter(document, 'Bilaga 1 Avvikelser')
    document.add_paragraph(
        'Denna bilaga sammanställer de avvikelser som användes i studien. '
        'Metoden och arbetsgången beskrivs i avsnitt 3.1.')
    document.add_paragraph(
        'Tabell B1-1 visar de avvikelser som användes för att utmana nodernas '
        'avsedda funktion. Alla avvikelser var inte relevanta för varje nod; '
        'den fullständiga tillämpningen framgår av HAZOP-protokollet.')
    deviations = list(dict.fromkeys(
        (d['description'] or '').strip() for n in data['nodes'] for d in db.deviations(n['id'])))
    _caption(document, 'B1.1', 'Registrerade avvikelser')
    _table(document, ['Avvikelse'], [[v] for v in deviations if v]
           or [[missing('avvikelser och ledord')]])
    _node_appendix(document, db, data)

    section = document.add_section(WD_SECTION_START.NEW_PAGE)
    width, margin = _page_setup(section, True, paper_size)
    heading = document.add_heading('Bilaga 3 HAZOP-protokoll', 1)
    heading.paragraph_format.page_break_before = False
    document.add_paragraph(
        'HAZOP-protokollet innehåller studiens detaljerade resultat. För varje '
        'behandlat scenario redovisas avvikelse, orsak, konsekvens, befintliga '
        'barriärer, riskbedömning och eventuell rekommendation. Noderna följer '
        'samma ordning som i sammanfattningen och bilaga 2.')
    document.add_paragraph(
        'Varje scenarioreferens följer ordningen '
        'studie.nod.avvikelse.orsak.konsekvens. Referensen används även i '
        'rekommendationsregistret för att koppla en rekommendation till rätt '
        'del av protokollet.')
    for index, group in enumerate(_group_rows(_annotated_worksheet_rows(db, data['rows']))):
        # Keep a complete node protocol together and let the next node start
        # on a fresh page.  Adding the break before nodes after the first
        # avoids both splitting a node and leaving a trailing blank page
        # before the following appendix.
        if index:
            document.add_page_break()
        node_label = group[0]['values'][0] if group and group[0].get('values') else ''
        node_name = re.sub(r'^\s*\d+\.\s*', '', node_label).strip()
        node_heading = document.add_paragraph(
            f"B3.{index + 1} {node_name or 'Aktuell nod'}",
            style='Appendix Node Heading')
        document.add_paragraph(
            f"Tabell B3-{index + 1} redovisar HAZOP-protokollet för "
            f"{node_name or 'den aktuella noden'}. Eventuella rekommendationer "
            'återfinns även i tabell B4-1 med hänvisning till berört scenario.')
        _caption(
            document, f'B3.{index + 1}',
            f"HAZOP-protokoll för {node_name or 'aktuell nod'}")
        _add_node_table(document, group, width, margin)
    if not data['rows']:
        document.add_paragraph(missing('HAZOP protokoll'))
    # Keep the final register in the landscape protocol section. This retains
    # its normal repeated header rather than introducing a new first-page
    # header solely for the final table.
    document.add_page_break()
    document.add_heading('Bilaga 4 Rekommendationslista', 1)
    document.add_paragraph(
        'Denna bilaga samlar de rekommendationer som analysgruppen formulerade '
        'under studien. Registret ska användas för att dokumentera ansvar, '
        'beslut, genomförande och verifierad stängning.')
    document.add_paragraph(
        'Tabell B4-1 redovisar varje rekommendation med ansvarig, åtgärdsdatum '
        'och registrerad status. Scenarioreferensen visar var frågan behandlades '
        'i bilaga 3.')
    _caption(document, 'B4.1', 'Rekommendationer och scenarioreferenser')
    if data['recommendation_rows']:
        _add_recommendation_table(document, data['recommendation_rows'], width, margin)
    else:
        document.add_paragraph(missing('rekommendationer eller bekräftelse att inga rekommendationer behövs'))

    _chapter(document, 'Bilaga 5 Nodmarkeringar')
    document.add_paragraph(
        'Denna bilaga redovisar nodindelningen markerad på aktuella P&ID-'
        'ritningar. Markeringarna infogas för respektive nod när rapporten '
        'färdigställs.')

    # Keep both original front-matter headers. Body and landscape sections
    # share the full source header; use continuous, refreshed page numbering.
    for section in document.sections:
        for setting in list(section._sectPr):
            if setting.tag == qn('w:pgNumType'):
                section._sectPr.remove(setting)
    # Do not rely on Word resolving a long chain of linked headers. Its
    # floating source artwork can disappear after generated section breaks.
    # Each generated section therefore receives its own relationship to the
    # same image parts and a copy of the source header XML.
    def copy_source_header(target_header):
        relationship_ids = {}
        for old_id, relationship in source_header_relationships:
            if relationship.is_external:
                new_id = target_header.part.relate_to(
                    relationship.target_ref, relationship.reltype, is_external=True)
            else:
                new_id = target_header.part.relate_to(
                    relationship.target_part, relationship.reltype)
            relationship_ids[old_id] = new_id
        for child in list(target_header._element):
            target_header._element.remove(child)
        for child in deepcopy(source_header_elements):
            target_header._element.append(child)
        for element in target_header._element.iter():
            for attribute in (qn('r:embed'), qn('r:id'), qn('r:link')):
                old_id = element.get(attribute)
                if old_id in relationship_ids:
                    element.set(attribute, relationship_ids[old_id])

    for section in document.sections[2:]:
        # Word treats the first body page of some generated sections as a
        # first-page-header even when titlePg is absent. Define that header
        # explicitly as well, rather than inheriting the cover's first header.
        section.different_first_page_header_footer = True
        section.first_page_header.is_linked_to_previous = True
        section.first_page_header.is_linked_to_previous = False
        copy_source_header(section.first_page_header)
        section.header.is_linked_to_previous = True
        section.header.is_linked_to_previous = False
        copy_source_header(section.header)
    footer_element = document.sections[2].footer._element
    for child in list(footer_element):
        footer_element.remove(child)
    footer_element.append(OxmlElement('w:p'))
    footer = document.sections[2].footer.paragraphs[0]
    footer.style = document.styles['Footer']
    footer.alignment = 2
    footer.add_run('Sida ')
    _field_run(footer, 'PAGE')
    footer.add_run(' av ')
    _field_run(footer, 'NUMPAGES')
    update = OxmlElement('w:updateFields')
    update.set(qn('w:val'), 'true')
    document.settings.element.append(update)
    _replace_table_references(document)
    apply_report_fonts(document)
    _highlight_document(document)
    return document


def export_report_word(db, filepath, paper_size='A3', *, standard_template=False):
    """Export atomically and return the existing (success, error) UI contract."""
    paper_size = str(paper_size or 'A3').upper()
    if paper_size not in PAPER_SIZES_MM:
        return False, f'Okänt pappersformat: {paper_size}'
    target = Path(filepath)
    temporary = None
    try:
        with _report_snapshot(db) as snapshot:
            document = build_report(snapshot, paper_size=paper_size, standard_template=standard_template)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.docx', delete=False) as file:
            temporary = Path(file.name)
        document.save(temporary)
        os.replace(temporary, target)
        return True, ''
    except ImportError:
        return False, 'python-docx saknas.\nKör: pip install python-docx'
    except Exception as error:
        return False, str(error)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
