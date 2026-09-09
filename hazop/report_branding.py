"""Source-backed ProSa report furniture, independent of Qt and study writes."""
from copy import deepcopy
import re

from constants import _bundle_dir


def new_report_document(values, revisions):
    from docx import Document
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
        for text in root.iter(qn('w:t')):
            text.text = re.sub(r'\{\{([A-Z_]+)\}\}',
                               lambda m: str(values[m.group(1)]), text.text or '')
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
