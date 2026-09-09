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


def _field_run(paragraph, instruction, cached='1'):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), instruction)
    field.set(qn('w:dirty'), 'true')
    run = OxmlElement('w:r')
    text = OxmlElement('w:t')
    text.text = cached
    run.append(text)
    field.append(run)
    paragraph._p.append(field)


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
    paragraph = document.add_paragraph(f'Tabell {number} {title}', 'Caption')
    paragraph.paragraph_format.left_indent = None
    paragraph.paragraph_format.first_line_indent = None
    paragraph.paragraph_format.right_indent = None
    paragraph.paragraph_format.keep_with_next = True
    return paragraph


PROSE_INTROS = {
    'Bakgrund': 'Bakgrunden ger läsaren sammanhanget till studien och beskriver den verksamhet eller förändring som har gjort analysen aktuell. Den hjälper också till att förklara vilka frågor som varit särskilt viktiga under genomgången.',
    'Syfte': 'Syftet beskriver vad studien ska bidra med och vilken användning resultatet är avsett för. Det ger en gemensam utgångspunkt för både analysgruppen och den fortsatta hanteringen av identifierade frågor.',
    'Omfattning': 'Omfattningen anger vilka system, delar och gränssnitt som har ingått. Tillsammans med nodindelningen ger den läsaren en tydlig bild av vilket analysobjekt rapportens resultat gäller.',
    'Avgränsningar': 'Avgränsningarna förtydligar sådant som medvetet har lämnats utanför studien eller behandlats på annat sätt. De är viktiga när resultatet senare används i projektering, drift eller fortsatt riskhantering.',
    'Övriga referensdokument': 'Utöver ritningsunderlaget kan studien ha baserats på exempelvis beskrivningar, instruktioner eller tidigare analyser. Dessa underlag anges här så att läsaren kan följa vilka uppgifter som fanns tillgängliga när analysen genomfördes.',
    'Riskacceptanskriterier': 'Riskacceptanskriterierna beskriver hur risknivåerna ska förstås och användas i den fortsatta hanteringen. Avsnittet bör även tydliggöra vem som kan bedöma eller acceptera en kvarvarande risk.',
    'Frekvensunderlag': 'Frekvensbedömningen bygger på analysgruppens gemensamma värdering av hur ofta en orsak eller händelse kan uppstå.',
    'Barriärunderlag': 'Barriärer och enablers påverkar hur ett scenario utvecklas och hur den slutliga risknivån bedöms. Här förklaras vilka typer av skydd som har tillgodoräknats och vilka krav som ställts för att de ska betraktas som tillgängliga och relevanta.',
    'Resultat och slutsatser': 'Avsnittet lyfter fram de viktigaste iakttagelserna från studien och sätter dem i ett sammanhang. Det bör ge läsaren en samlad förståelse av vad som behöver tas vidare, utan att ersätta den mer detaljerade redovisningen i bilagorna.',
    'Uppföljning': 'Här beskrivs hur rekommendationerna ska tas om hand efter avslutad analys. Ansvar, tidplan och vilket underlag som krävs för att kunna avsluta en åtgärd bör framgå så att uppföljningen blir tydlig och spårbar.',
    'Metodreferens': 'Om projektet har använt en särskild instruktion, standard eller kundanpassad metodbeskrivning anges den här. Referensen kompletterar den praktiska arbetsgång som beskrivs i följande avsnitt.',
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
        'Riskbedömningen ger analysgruppen ett gemensamt sätt att värdera de '
        'scenarier som identifieras under studien. Riskbedömningarna har '
        'genomförts med den riskmatris och de skalor som anges för studien. '
        'Resultaten i protokollet ska tolkas tillsammans med frekvens- och '
        'konsekvensdefinitionerna i detta kapitel.')
    document.add_paragraph(
        'I protokollet redovisas riskbedömningar före och efter barriärer samt '
        'de enablers som har beaktats. Risknivåerna ska läsas tillsammans med '
        'de definitioner och kriterier som anges i detta kapitel.')
    document.add_heading('4.1 Riskmatris', 2)
    document.add_paragraph(
        'Riskmatrisen har inte återgetts som en separat kopia i slutrapporten. '
        'Bedömningarna ska tolkas mot den generella riskmatris som gäller för '
        'studien och mot definitionerna i följande avsnitt.')
    x_codes, y_codes = matrix['x_codes'], matrix['y_codes']
    document.add_heading('4.2 Frekvensskala', 2)
    document.add_paragraph(
        'Frekvensnivåerna som används vid scenarioanalysen redovisas i tabell '
        '4.2. Definitionerna ger stöd för en konsekvent bedömning mellan olika '
        'noder och analystillfällen.')
    _caption(document, '4.2', 'Frekvensnivåer och definitioner')
    _table(document, ['Nivå', 'Definition'],
           [[code, _value(label, 'frekvensdefinition')]
            for code, label in zip(x_codes, matrix['x_labels'])], [25, 135])
    document.add_heading('4.3 Konsekvensdefinitioner', 2)
    document.add_paragraph(
        'Konsekvenserna bedöms inom de kategorier som är registrerade i '
        'projektet. Tabellerna 4.3.1 och framåt visar definitionerna för varje '
        'kategori och nivå.')
    categories = [dict(c) for c in db.consequence_categories()]
    definitions = db.get_severity_definitions()
    category_index = 0
    for category in categories:
        category_values = [definitions.get(i + 1, {}).get(category['id']) for i in range(len(y_codes))]
        if not any((v or '').strip() for v in category_values):
            continue
        category_index += 1
        document.add_heading(category['name'], 3)
        _caption(document, f'4.3.{category_index}', 'Konsekvensdefinitioner för ' + category['name'])
        _table(document, ['Nivå', 'Benämning', 'Definition'], [
            [code, matrix['y_labels'][i],
             _value(definitions.get(i + 1, {}).get(category['id']),
                    f"definition {category['name']} {code}")]
            for i, code in enumerate(y_codes)], [16, 32, 112])
    if not category_index:
        document.add_paragraph(missing('konsekvenskategorier'))
    document.add_heading('4.4 Riskacceptanskriterier', 2)
    document.add_paragraph('Riskacceptanskriterierna har använts för att tolka de nivåer som har valts i riskmatrisen. De kriterier som har definierats för projektet redovisas i tabell 4.4.')
    criteria = [(str(i), db.get_config(f'risk_acceptance_{i}', '')) for i in range(1, 6)]
    criteria = [(level, value) for level, value in criteria if value.strip()]
    if not criteria:
        criteria = [(str(item.get('label') or item.get('color') or 'Risknivå'), item.get('definition', ''))
                    for item in matrix.get('risk_level_definitions', []) if (item.get('definition') or '').strip()]
    if criteria:
        _caption(document, '4.4', 'Definierade riskacceptanskriterier')
        _table(document, ['Nivå', 'Kriterium'], criteria, [25, 135])
    elif data['field']('Riskacceptanskriterier'):
        document.add_paragraph(data['field']('Riskacceptanskriterier'))
    else:
        document.add_paragraph(missing('riskacceptanskriterier'))
    _prose(document, data, 'Frekvensunderlag', '4.5 Underlag för frekvenser')
    document.add_heading('4.6 Underlag för barriärer och enablers', 2)
    document.add_paragraph('Barriärer och enablers som har registrerats i analysen har sammanställts för att tydliggöra vilka skydd och påverkande förhållanden som har beaktats.')
    enablers = sorted({(rf['description'] or '').strip() for row in data['rows'] for rf in db.reduction_factors(row['merge_key'][3]) if (rf['description'] or '').strip() and dict(rf).get('active', 1)})
    rrf_values = sorted({float(rf['rrf']) for row in data['rows'] for rf in db.reduction_factors(row['merge_key'][3]) if (rf['description'] or '').strip() and dict(rf).get('active', 1) and rf['rrf'] is not None})
    if rrf_values:
        rrf_text = f'{rrf_values[0]:g}' if len(rrf_values) == 1 else f'{rrf_values[0]:g}–{rrf_values[-1]:g}'
        document.add_paragraph(f'RRF för använda enablers har satts till {rrf_text}.')
    if enablers:
        _caption(document, '4.6', 'Typer av använda enablers')
        _table(document, ['Enabler', 'Beskrivning'], [[e, 'Registrerad enabler i analysen'] for e in enablers], [55, 105])
    elif data['field']('Barriärunderlag'):
        document.add_paragraph(data['field']('Barriärunderlag'))


def _add_participants(document, db, data):
    _chapter(document, '3 Genomförande och deltagare')
    document.add_paragraph(
        'HAZOP-arbetet har genomförts som en gemensam och tvärdisciplinär '
        'genomgång där deltagarnas kunskap om process, teknik, drift och '
        'underhåll har tagits till vara. Kapitlet beskriver det arbetssätt som '
        'har använts, hur studien har organiserats och vilka personer som har '
        'medverkat.')
    document.add_heading('3.1 Metod och arbetssätt', 2)
    document.add_paragraph(
        'Studien har genomförts som en strukturerad HAZOP-genomgång av möjliga '
        'avvikelser från anläggningens avsedda funktion. Analysobjektet har '
        'delats in i hanterbara noder med definierade gränser och en beskriven '
        'designavsikt. Indelningen har gjort det möjligt att behandla varje '
        'funktion sammanhållet och samtidigt uppmärksamma viktiga gränssnitt '
        'mot angränsande system.')
    document.add_paragraph(
        'Inför analystillfällena har relevant referensunderlag samlats in. Under '
        'genomgången har analysgruppen utgått från nodens funktion och aktuella '
        'driftfall. Parametrar och ledord har därefter använts för att formulera '
        'relevanta avvikelser och följa händelseförloppet från möjlig orsak till '
        'tänkbar konsekvens.')
    document.add_paragraph('Arbetsgången har varit följande:')
    _numbered_list(document, (
        'Nodens avsedda funktion, gränser och relevanta driftfall har bekräftats.',
        'Relevanta avvikelser har formulerats med hjälp av parametrar och ledord.',
        'Trovärdiga orsaker och möjliga konsekvenser har identifierats och beskrivits.',
        'Befintliga barriärer och andra förhållanden som påverkar händelseförloppet har dokumenterats.',
        'Frekvens och konsekvens har bedömts i protokollets riskkolumner för de konsekvenskategorier som har berörts.',
        'Rekommendationer har formulerats när ytterligare utredning, verifiering eller åtgärd har bedömts behövas; även ansvar, tidplan och uppföljningsbehov har beaktats.',
    ))
    document.add_paragraph(
        'Riskbedömningarna har gjorts med projektets sparade riskmatris. '
        'Analysgruppen har först bedömt det formulerade scenariot och därefter '
        'beaktat registrerade barriärer och enablers. Valda nivåer har '
        'dokumenterats tillsammans med scenarioinformationen för att '
        'bedömningens bakgrund ska kunna följas i rapporten.')
    document.add_paragraph(
        'Innan analystillfället avslutades gick gruppen gemensamt igenom '
        'identifierade rekommendationer och säkerställde att formulering, '
        'ansvar och uppföljningsbehov var tillräckligt tydliga.')
    sessions = [dict(s) for s in db.list_analysis_sessions()]
    document.add_heading('3.2 Analystillfällen', 2)
    document.add_paragraph(
        'Genomförda analystillfällen sammanställs i tabell 3.1 med datum, tid och plats.')
    session_rows = [[str(i), _value(s.get('date'), 'datum'),
                     _value(s.get('start_time'), 'starttid') + '–' +
                     _value(s.get('end_time'), 'sluttid'),
                     _value(s.get('location'), 'plats')]
                    for i, s in enumerate(sessions, 1)]
    _caption(document, '3.1', 'Analystillfällen')
    _table(document, ['Tillfälle', 'Datum', 'Tid', 'Plats'], session_rows or [
        ['1', missing('analystillfälle'), '', '']], [17, 28, 43, 72])
    document.add_heading('3.3 Deltagare', 2)
    document.add_paragraph(
        'Tabell 3.2 visar deltagarna och de roller eller övriga '
        'deltagaruppgifter som har registrerats för studien.')
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
    document.add_heading('3.4 Närvaro', 2)
    document.add_paragraph(
        'Närvaron vid respektive analystillfälle redovisas i tabell 3.3. '
        'Eventuella anteckningar kan exempelvis förklara om en deltagare '
        'medverkat under endast en del av mötet.')
    attendance = db.get_attendance_details()
    attendance_rows = []
    short_status = len(sessions) > 5
    for p in participants:
        name = _value(' '.join(filter(None, [p['first_name'], p['last_name']])), 'deltagarnamn')
        statuses = []
        for session in sessions:
            state = attendance.get((p['id'], session['id']))
            statuses.append(('N' if state and state[0] else 'F') if short_status else ('Närvarande' if state and state[0] else 'Frånvarande') if state else missing('närvaro'))
        attendance_rows.append([name] + statuses)
    attendance_headers = ['Deltagare'] + [s.get('date') or f'Tillfälle {i}' for i, s in enumerate(sessions, 1)]
    _caption(document, '3.3', 'Närvaro per analystillfälle')
    if attendance_rows and sessions:
        _table(document, attendance_headers, attendance_rows, [55] + [105 / len(sessions)] * len(sessions))


def _node_appendix(document, db, data):
    _chapter(document, 'Bilaga 2 HAZOP noder')
    document.add_paragraph(
        'Denna bilaga beskriver hur anläggningen har delats in för analysen. '
        'Nodernas funktion, gränser och normala förutsättningar ger den '
        'referens mot vilken avvikelserna i HAZOP-genomgången har bedömts.')
    document.add_paragraph(
        'Nodnumreringen följer samma ordning som i protokollet. En verifierad '
        'nodritning bör komplettera uppgifterna och tydligt visa de gränser '
        'som analysgruppen har använt.')
    systems = {s['id']: s['name'] for s in db.systems()}
    sheets = {s['physical_page']: dict(s) for s in db.get_sheets()}
    for number, node in enumerate(data['nodes'], 1):
        document.add_heading(f"Nod {number} {node['name']}", 2)
        description = document.add_paragraph(_value(node.get('description'), 'nodens funktion och designavsikt'))
        description.paragraph_format.keep_with_next = True
        pages = db.analysis_pages_for_node(node['id'])
        references = []
        for page in pages:
            sheet = sheets.get(page, {})
            references.append(
                f"PDF-sida {page + 1}: " + _value(sheet.get('drawing_number'), 'ritningsnummer') +
                ', rev. ' + _value(sheet.get('drawing_revision'), 'ritningsrevision'))
        if node.get('pid_ref'):
            references.insert(0, node['pid_ref'])
        document.add_paragraph(
            f"Tabell B2.{number} sammanställer registrerade förutsättningar och "
            f"ritningsreferenser för nod {number}.")
        _caption(document, f'B2.{number}', 'Noduppgifter för ' + node['name'])
        _table(document, ['Uppgift', 'Värde'], [
            ['System', systems.get(node.get('system_id'), 'Ogrupperad nod')],
            ['P&ID referenser', '\n'.join(references) or missing('nodens P&ID referenser')],
            ['Media', _value(node.get('media'), 'media')],
            ['Tryck', _value(node.get('pressure'), 'tryck inklusive enhet')],
            ['Temperatur', _value(node.get('temperature'), 'temperatur inklusive enhet')],
            ['Nodstatus', node.get('study_status') or missing('nodstatus')],
        ], [37, 123])
        document.add_paragraph(missing('infoga nodritning med verifierade nodgränser'))
    if not data['nodes']:
        document.add_paragraph(missing('noder och designavsikt'))


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

    data = collect_report_data(db)
    project = _value(db.get_config('project_name', ''), 'projektnamn')
    client = _value(db.get_config('project_client', ''), 'kund')
    field = data['field']
    project_number_raw = (db.get_config('project_number', '') or '').strip()
    suggested_report_number = f'{project_number_raw}-R-001' if project_number_raw else ''
    report_number = _value(field('Rapportnummer') or db.get_config('report_number', '') or suggested_report_number, 'rapportnummer')
    revision = _value(field('Rapportrevision') or data['latest_revision'].get('label'), 'rapportrevision')
    date = _value(field('Rapportdatum') or db.get_config('report_date', '') or data['latest_revision'].get('date'), 'rapportdatum')
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
        'STATUS': _value(field('Rapportstatus'), 'rapportstatus'),
        'DISTRIBUTION': _value(field('Distribution') or db.get_config('report_distribution', '') or 'Enligt kundens anvisning', 'distribution'),
        'AUTHOR': _value(issued_by, 'utfärdad av'),
        'REVIEWER': _value(reviewed_by, 'granskad av'),
        'PROSA_ADDRESS': _value(company_address, 'ProSa-adress'),
        'CLIENT_ADDRESS': field('Kundadress') or '',
        'MANAGER': field('Uppdragsansvarig') or '',
        'PROSA_CONTACT': company_contact or issued_by or '',
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
    for cell in document.tables[0].rows[3].cells:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.text = run.text.replace('Rev:', 'Revision:')
    styles = document.styles
    styles['Heading 1'].paragraph_format.page_break_before = False
    if 'TOC Heading' not in styles:
        styles.add_style('TOC Heading', WD_STYLE_TYPE.PARAGRAPH).base_style = styles['Heading 1']
    document.core_properties.title = values['TITLE']
    document.core_properties.author = issued_by or ''
    document.core_properties.subject = 'HAZOP analys utan SIL bedömning'
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
        f'HAZOP-studien avser {project} för {client}. Analysen har genomförts '
        'som en strukturerad genomgång av möjliga avvikelser från anläggningens '
        'avsedda funktion, med fokus på att skapa ett tydligt och spårbart '
        'underlag för fortsatt riskhantering.')
    if standard_template:
        document.add_paragraph(
            missing('antal analystillfällen, noder, konsekvensposter och rekommendationer'))
    else:
        session_count = len(data['sessions'])
        session_label = 'analystillfälle' if session_count == 1 else 'analystillfällen'
        document.add_paragraph(
            f"Arbetet omfattar {len(data['nodes'])} noder och har "
            f"genomförts vid {session_count} {session_label}. "
            f"Genomgången har resulterat i {data['consequence_count']} "
            f"dokumenterade konsekvensposter och {len(data['recommendations'])} "
            'rekommendationer för fortsatt hantering.')
        closed = {'stängd', 'implementerad', 'avslutad', 'ej implementerad'}
        open_count = sum(1 for r in data['recommendations'] if str(r.get('status') or '').strip().casefold() not in closed)
        node_counts = Counter()
        for rec in data['recommendation_rows']:
            for item in str(rec[5]).split(', '):
                parts = item.split('.')
                if len(parts) > 1 and parts[1].isdigit():
                    node_counts[int(parts[1])] += 1
        if data['recommendations']:
            most = ', '.join(f'Nod {n} ({count})' for n, count in node_counts.most_common(3))
            document.add_paragraph(f'{open_count} rekommendationer har ännu inte avslutats.' + (f' Flest rekommendationer berör {most}.' if most else ''))
        if data['sessions']:
            document.add_paragraph('Analysdatum:')
            _numbered_list(document, [s.get('date') or missing('analysdatum') for s in data['sessions']])
    _front_heading('Studerade noder')
    if data['nodes']:
        _numbered_list(document, [
            _value(node.get('name'), 'nodnamn') for node in data['nodes']
        ])
    else:
        _numbered_list(document, [missing('numrerad nodlista')])
    if field('Resultat och slutsatser'):
        document.add_paragraph(field('Resultat och slutsatser'))
    document.add_paragraph(
        'Den detaljerade redovisningen finns i HAZOP-protokollet och i '
        'rekommendationslistan i rapportens bilagor.')

    document.add_page_break()
    _front_heading('Förkortningar')
    document.add_paragraph('Följande förkortningar används återkommande i rapporten och i HAZOP-protokollet.')
    _table(document, ['Förkortning', 'Förklaring'], [
        ['HAZOP', 'Hazard and Operability Study – risk- och driftanalys'],
        ['P&ID', 'Piping and Instrumentation Diagram – rör- och instrumentdiagram'],
        ['RRF', 'Risk Reduction Factor – riskreduktionsfaktor'],
        ['BPCS', 'Basic Process Control System – ordinarie processtyrsystem'],
    ], [28, 132])
    document.add_page_break()
    document.add_paragraph('Innehåll', 'TOC Heading')
    _field_run(document.add_paragraph(), 'TOC \\o "1-3" \\h \\z',
               'Uppdatera innehållsförteckningen i Word med Ctrl+A och F9.')

    _chapter(document, '1 Inledning')
    document.add_paragraph(
        f'En HAZOP-studie har genomförts för {project} vid {client}. '
        'Kapitlet beskriver bakgrunden till arbetet, studiens syfte och omfattning '
        'samt de avgränsningar som har gällt för den genomförda analysen.')
    document.add_heading('1.1 Bakgrund', 2)
    document.add_paragraph(
        'Studien har genomförts som en strukturerad genomgång av möjliga avvikelser '
        'från systemets avsedda funktion. Rapporten sammanfattar genomförandet, '
        'eventuella avvikelser från planerat arbetssätt och de resultat som har '
        'dokumenterats i HAZOP-protokollet. HAZOP är en etablerad metod för att '
        'pröva hur en process eller anläggning kan avvika från sin avsedda funktion. '
        'Genom att kombinera ledord med analysgruppens erfarenhet har möjliga orsaker, '
        'konsekvenser och befintliga skydd identifierats. Arbetssättet har även '
        'synliggjort operabilitetsfrågor och gett ett gemensamt underlag för fortsatt '
        'riskhanteringsarbete, verifiering och riskreducerande åtgärder.')
    document.add_heading('1.2 Syfte och omfattning', 2)
    document.add_paragraph(
        'Syftet har varit att identifiera och värdera risker samt operabilitetsfrågor '
        'inom det analyserade systemet och att dokumentera rekommendationer där '
        'ytterligare åtgärder har bedömts motiverade.')
    if field('Syfte'):
        document.add_paragraph(field('Syfte'))
    if field('Omfattning'):
        document.add_paragraph(field('Omfattning'))
    document.add_paragraph('Analysen har omfattat följande noder:')
    _numbered_list(document, [_value(node.get('name'), 'nodnamn') for node in data['nodes']] or [missing('numrerad nodlista')])
    if field('Driftfall'):
        document.add_paragraph('De driftsituationer som har ingått har varit: ' + field('Driftfall'))
    document.add_heading('1.3 Avgränsning', 2)
    document.add_paragraph(
        'Bedömningen har avgränsats till de systemdelar, noder, ritningar och övriga '
        'dokument som anges i denna rapport.')
    if field('Avgränsningar'):
        document.add_paragraph(field('Avgränsningar'))
    if field('Analysförutsättningar'):
        document.add_paragraph('Analysen har utgått från följande dokumenterade förutsättningar: ' + field('Analysförutsättningar'))

    _chapter(document, '2 Dokumentunderlag')
    document.add_paragraph(
        'För att genomföra studien har ett dokumenterat underlag använts. Ritningar, '
        'revisionsuppgifter och övriga referensdokument har gjort det möjligt att '
        'avgränsa noderna och följa analysens resultat tillbaka till aktuella '
        'förutsättningar. Detta kapitel sammanställer underlaget som var tillgängligt '
        'för analysgruppen.')
    document.add_heading('2.1 Ritningsunderlag', 2)
    document.add_paragraph(
        'Det underlag som användes för HAZOP-studien omfattade P&ID-ritningar '
        'och övriga projektdokument enligt tabell 2.1. Uppgifterna identifierar '
        'det grafiska underlag som nodindelningen och scenarioanalysen baserades på.')
    sheets = [dict(s) for s in db.get_sheets()]
    _caption(document, '2.1', 'Dokumentunderlag')
    _table(document, ['Dokumentnummer', 'Dokumenttitel', 'Revision', 'Datum', 'PDF sida'], [
        [_value(s.get('drawing_number'), 'ritningsnummer'),
         _value(s.get('drawing_name'), 'ritningsnamn'),
         _value(s.get('drawing_revision'), 'revision'),
         _value(s.get('drawing_date'), 'ritningsdatum'), str(s['physical_page'] + 1)]
        for s in sheets] or [[missing('referensdokument'), '', '', '', '']], [34, 51, 23, 32, 20])
    if field('Övriga referensdokument'):
        _prose(document, data, 'Övriga referensdokument', '2.2 Övriga referensdokument')
    _add_participants(document, db, data)
    if standard_template:
        _chapter(document, '4 Riskbedömning')
        document.add_paragraph(
            'Riskbedömningen ger analysgruppen ett gemensamt sätt att värdera '
            'de scenarier som identifieras. Kapitlet ska redovisa den matris, '
            'de skalor och de bedömningsgrunder som används i studien, så att '
            'resultatet kan förstås och följas upp på ett enhetligt sätt.')
        document.add_heading('4.1 Riskmatris', 2)
        document.add_paragraph(
            'Tabell 4.1 återger samma axelval, visningsriktning, '
            'risknivåer och färger som i det aktuella HAZOP-projektet.')
        document.add_paragraph(missing('studiens riskmatris'))
        document.add_heading('4.2 Frekvensskala', 2)
        document.add_paragraph(
            'Tabell 4.2 återger de frekvensnivåer och definitioner som '
            'analysgruppen ska använda.')
        document.add_paragraph(missing('frekvensskala och definitioner'))
        document.add_heading('4.3 Konsekvensdefinitioner', 2)
        document.add_paragraph(
            'Tabellerna 4.3.1 och framåt återger konsekvensdefinitioner '
            'för studiens kategorier.')
        document.add_paragraph(missing('konsekvenskategorier och definitioner'))
        _prose(document, data, 'Riskacceptanskriterier', '4.4 Riskacceptanskriterier')
        _prose(document, data, 'Frekvensunderlag', '4.5 Underlag för frekvenser')
        _prose(document, data, 'Barriärunderlag', '4.6 Underlag för barriärer och enablers')
    else:
        _add_matrix(document, db, data)

    _chapter(document, '5 Resultat och uppföljning')
    document.add_paragraph(
        'Kapitlet samlar studiens övergripande resultat och beskriver hur de '
        'frågor som identifierats ska tas vidare. Syftet är att ge en tydlig '
        'övergång från analys till fortsatt riskhanteringsarbete, verifiering eller '
        'åtgärdshantering.')
    document.add_paragraph(
        'Slutsatserna bör läsas tillsammans med det detaljerade protokollet. '
        'Uppföljningen behöver säkerställa att varje rekommendation får en '
        'tydlig hantering och att eventuella kvarstående risker bedöms på '
        'avsedd beslutsnivå.')
    if field('Resultat och slutsatser'):
        _prose(document, data, 'Resultat och slutsatser', '5.1 Resultat och slutsatser')
    document.add_heading('5.2 Rekommendationernas status', 2)
    document.add_paragraph(
        'Tabell 5.1 ger en översikt över rekommendationernas registrerade '
        'status. Fullständig rekommendationstext, ansvarig, åtgärdsdatum och '
        'koppling till berörda scenarier redovisas i tabell B4.1.')
    status_counts = Counter(str(r.get('status') or 'Ej angiven') for r in data['recommendations'])
    _caption(document, '5.1', 'Rekommendationer per registrerad status')
    _table(document, ['Registrerad status', 'Antal'], sorted(status_counts.items())
           or ([[missing('status'), missing('antal')]] if standard_template
               else [['Inga rekommendationer registrerade', 0]]), [125, 35])
    document.add_paragraph(
        'Statusuppgifterna återges som registrerade. En stängd rekommendation '
        'innebär inte automatiskt att kvarvarande risk är bedömd eller accepterad.')
    document.add_heading('5.3 Rekommendationer', 2)
    document.add_paragraph(
        'Rekommendationerna har tagits fram gemensamt av analysgruppen utifrån '
        'de identifierade scenarierna och den samlade diskussionen under studien. '
        'För varje rekommendation ska en bedömning göras av om den ska '
        'implementeras eller inte. Oavsett vilket beslut som fattas ska beslutet '
        'och dess motivering dokumenteras, tillsammans med ansvar och eventuell '
        'tidplan för uppföljning.')
    if field('Uppföljning'):
        document.add_paragraph(field('Uppföljning'))

    _chapter(document, 'Bilaga 1 Avvikelser och förkortningar')
    document.add_paragraph(
        'Denna bilaga ger en överblick över de avvikelser som har behandlats i '
        'studien och förklarar de förkortningar som används i rapporten. '
        'Studiens metod och genomförande har beskrivits i avsnitt 3.1.')
    document.add_heading('B1.1 Registrerade avvikelser', 2)
    document.add_paragraph(
        'Tabell B1.1 visar de avvikelser som finns registrerade för studiens '
        'noder. Förteckningen ger en överblick över analysens frågeställningar '
        'men ersätter inte scenarioredovisningen i protokollet.')
    deviations = list(dict.fromkeys(
        (d['description'] or '').strip() for n in data['nodes'] for d in db.deviations(n['id'])))
    _caption(document, 'B1.1', 'Registrerade avvikelser')
    _table(document, ['Avvikelse'], [[v] for v in deviations if v]
           or [[missing('avvikelser och ledord')]])
    document.add_heading('B1.2 Förkortningar', 2)
    document.add_paragraph(
        'De förkortningar som används återkommande i rapporten förklaras i '
        'tabell B1.2.')
    _caption(document, 'B1.2', 'Förkortningar')
    _table(document, ['Förkortning', 'Förklaring'], [
        ['HAZOP', 'Hazard and Operability Study – risk- och driftanalys'],
        ['P&ID', 'Piping and Instrumentation Diagram – rör- och instrumentdiagram'],
        ['RRF', 'Risk Reduction Factor – riskreduktionsfaktor'],
        ['BPCS', 'Basic Process Control System – ordinarie processtyrsystem'],
    ], [28, 132])
    _node_appendix(document, db, data)

    section = document.add_section(WD_SECTION_START.NEW_PAGE)
    width, margin = _page_setup(section, True, paper_size)
    heading = document.add_heading('Bilaga 3 HAZOP protokoll', 1)
    heading.paragraph_format.page_break_before = False
    document.add_paragraph(
        'Protokollet är studiens detaljerade redovisning. Här kan läsaren följa '
        'resonemanget från avvikelse och orsak till konsekvens, befintliga '
        'barriärer, riskbedömning och eventuell rekommendation. Noderna '
        'redovisas i samma ordning som i sammanfattningen och nodbilagan.')
    document.add_paragraph(
        'Scenarioreferenserna följer ordningen studie.nod.avvikelse.orsak.konsekvens '
        'och redovisas tillsammans med de registrerade bedömningarna.')
    for index, group in enumerate(_group_rows(_annotated_worksheet_rows(db, data['rows']))):
        if index:
            document.add_page_break()
        node_label = group[0]['values'][0] if group and group[0].get('values') else ''
        document.add_paragraph(
            f"Tabell B3.{index + 1} redovisar HAZOP-protokollet för "
            f"{node_label or 'den aktuella noden'}. Rekommendationer i tabellen "
            'återfinns även i rekommendationslistan med referens tillbaka till '
            'berört scenario.')
        _caption(document, f'B3.{index + 1}', 'HAZOP protokoll per nod')
        _add_node_table(document, group, width, margin)
    if not data['rows']:
        document.add_paragraph(missing('HAZOP protokoll'))
    # Keep the final register in the landscape protocol section. This retains
    # its normal repeated header rather than introducing a new first-page
    # header solely for the final table.
    document.add_page_break()
    document.add_heading('Bilaga 4 Rekommendationslista', 1)
    document.add_paragraph(
        'Rekommendationslistan samlar de frågor som analysgruppen har bedömt '
        'behöver utredas, verifieras eller åtgärdas efter genomgången. Den är '
        'avsedd att fungera som ett spårbart underlag för ansvarsfördelning och '
        'fortsatt uppföljning.')
    document.add_heading('B4.1 Rekommendationsregister', 2)
    document.add_paragraph(
        'Tabell B4.1 redovisar rekommendationerna med ansvarig, åtgärdsdatum '
        'och registrerad status. Scenarioreferenserna visar var varje '
        'rekommendation hör hemma i bilaga 3 och gör det möjligt att följa '
        'åtgärden tillbaka till analysen.')
    _caption(document, 'B4.1', 'Rekommendationer och scenarioreferenser')
    if data['recommendation_rows']:
        _add_recommendation_table(document, data['recommendation_rows'], width, margin)
    else:
        document.add_paragraph(missing('rekommendationer eller bekräftelse att inga rekommendationer behövs'))

    _chapter(document, 'Bilaga 5 Node Markup')
    document.add_paragraph(
        'Denna bilaga redovisar node markup för de analyserade noderna. '
        'Markeringarna klipps in från respektive P&ID som en del av den slutliga '
        'rapportdokumentationen.')

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
    source_header = document.sections[2].header

    def copy_source_header(target_header):
        relationship_ids = {}
        for old_id, relationship in source_header.part.rels.items():
            if relationship.is_external:
                new_id = target_header.part.relate_to(
                    relationship.target_ref, relationship.reltype, is_external=True)
            else:
                new_id = target_header.part.relate_to(
                    relationship.target_part, relationship.reltype)
            relationship_ids[old_id] = new_id
        for child in list(target_header._element):
            target_header._element.remove(child)
        for child in deepcopy(list(source_header._element)):
            target_header._element.append(child)
        for element in target_header._element.iter():
            for attribute in (qn('r:embed'), qn('r:id'), qn('r:link')):
                old_id = element.get(attribute)
                if old_id in relationship_ids:
                    element.set(attribute, relationship_ids[old_id])

    for index, section in enumerate(document.sections[2:]):
        # Word treats the first body page of some generated sections as a
        # first-page-header even when titlePg is absent. Define that header
        # explicitly as well, rather than inheriting the cover's first header.
        section.different_first_page_header_footer = True
        section.first_page_header.is_linked_to_previous = True
        section.first_page_header.is_linked_to_previous = False
        copy_source_header(section.first_page_header)
        if index:
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
