"""Source-backed ProSa report furniture, independent of Qt and study writes."""
from copy import deepcopy
import re

from constants import _bundle_dir


def new_report_document(values, revisions):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    document = Document(_bundle_dir() / 'report_templates' / 'prosa_hazop.docx')
    revision_table = document.tables[1]
    template_row = deepcopy(revision_table.rows[1]._tr)
    revision_table._tbl.remove(revision_table.rows[1]._tr)
    for revision in revisions:
        row = deepcopy(template_row)
        for text in row.iter(qn('w:t')):
            for key, value in revision.items():
                text.text = (text.text or '').replace('{{'+key+'}}', str(value))
        revision_table._tbl.append(row)
    for part in document.part.package.parts:
        root = getattr(part, 'element', None)
        if root is None:
            root = getattr(part, '_element', None)
        if root is None:
            continue
        for text in list(root.iter(qn('w:t'))):
            replacement = re.sub(r'\{\{([A-Z_]+)\}\}',
                                 lambda m: str(values[m.group(1)]), text.text or '')
            if '\n' not in replacement:
                text.text = replacement
                continue
            parent = text.getparent()
            text.text = replacement.split('\n', 1)[0]
            previous = text
            for part in replacement.split('\n')[1:]:
                br = OxmlElement('w:br')
                previous.addnext(br)
                new_text = OxmlElement('w:t')
                new_text.text = part
                br.addnext(new_text)
                previous = new_text
    # The customer name is already shown in the cover heading and need not be
    # repeated in the contact-person block. Keep the run formatting intact.
    if document.tables and len(document.tables[0].rows) > 6:
        for paragraph in document.tables[0].rows[6].cells[2].paragraphs:
            for run in paragraph.runs:
                    run.text = run.text.replace(str(values.get('CLIENT') or ''), '')
    # Keep cover metadata values in regular body weight while emphasizing the
    # labels.  The template stores each metadata line in one run, so split the
    # run at the colon and retain the original order in the XML.
    if document.tables:
        for row in document.tables[0].rows[1:4]:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        text = run.text or ''
                        run.bold = False
                        match = re.match(r'^(Titel|Datum|Distribution|Rapport nr|Rev|Revision):', text)
                        if match:
                            label = match.group(0)
                            rest = text[len(label):]
                            run.text = label
                            run.bold = True
                            if rest:
                                clone = deepcopy(run._r)
                                rpr = clone.find(qn('w:rPr'))
                                if rpr is not None:
                                    bold = rpr.find(qn('w:b'))
                                    if bold is not None:
                                        rpr.remove(bold)
                                clone.find(qn('w:t')).text = rest
                                run._r.addnext(clone)
    # The running header uses a compact two-line table. Its first label is
    # intentionally Revision (the value is the report revision number).
    for part in document.part.package.parts:
        root = getattr(part, '_element', None)
        if root is None or 'header' not in str(part.partname):
            continue
        for text in root.iter(qn('w:t')):
            if (text.text or '').strip() == 'Status':
                text.text = (text.text or '').replace('Status', 'Revision')
    return document


def apply_report_fonts(document):
    """Canonical worksheet builders set Arial explicitly; use source body font.

    Compact table sizes remain intact; only their typeface changes. All
    source header, cover and heading run formatting is otherwise retained.
    """
    from docx.oxml.ns import qn
    for font in document.element.iter(qn('w:rFonts')):
        for key in ('ascii', 'hAnsi'):
            if font.get(qn('w:'+key)) == 'Arial':
                font.set(qn('w:'+key), document.styles['Normal'].font.name)
