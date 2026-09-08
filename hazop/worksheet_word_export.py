"""Word export for the complete HAZOP worksheet.

The exporter deliberately reuses ``worksheet_export._worksheet_rows`` and
its Office merge keys so Word follows the same physical row grid as the
Worksheet and Ctrl+C/Ctrl+V export paths.
"""

from database import risk_info
from worksheet_export import _worksheet_rows


PAPER_SIZES_MM = {
    'A4': (297, 210),
    'A3': (420, 297),
}

_HEADERS = [
    'Nod', 'Avvikelse', 'Orsak', 'Frekvens', 'Konsekvens',
    'Riskklass före barriärer', 'Barriär', 'RRF', 'Enablers',
    'Riskklass efter barriärer', 'Rekommendation',
]
_WIDTH_RATIOS = [17, 23, 36, 10, 54, 19, 46, 9, 14, 19, 53]


def _hex_colour(value, fallback='FFFFFF'):
    text = str(value or fallback).strip().lstrip('#')
    return text.upper() if len(text) in (6, 8) else fallback


def _set_cell_shading(cell, colour):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn('w:shd'))
    if shading is None:
        shading = OxmlElement('w:shd')
        properties.append(shading)
    shading.set(qn('w:fill'), _hex_colour(colour))


def _set_cell_borders(cell, colour='CBD5E1'):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = cell._tc.get_or_add_tcPr()
    borders = properties.first_child_found_in('w:tcBorders')
    if borders is None:
        borders = OxmlElement('w:tcBorders')
        properties.append(borders)
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        tag = 'w:' + edge
        element = borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(qn('w:val'), 'single')
        element.set(qn('w:sz'), '4')
        element.set(qn('w:space'), '0')
        element.set(qn('w:color'), _hex_colour(colour))


def _set_repeat_table_header(row):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = row._tr.get_or_add_trPr()
    header = OxmlElement('w:tblHeader')
    header.set(qn('w:val'), 'true')
    properties.append(header)


def _prevent_row_split(row):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = row._tr.get_or_add_trPr()
    cant_split = OxmlElement('w:cantSplit')
    cant_split.set(qn('w:val'), 'true')
    properties.append(cant_split)


def _set_cell_text(cell, value, *, bold=False, colour='17191C', size=7.5,
                   align='left'):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    cell.text = ''
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    paragraph.alignment = {
        'left': WD_ALIGN_PARAGRAPH.LEFT,
        'center': WD_ALIGN_PARAGRAPH.CENTER,
    }.get(align, WD_ALIGN_PARAGRAPH.LEFT)
    run = paragraph.add_run(str(value or ''))
    run.font.name = 'Arial'
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(_hex_colour(colour))
    cell.vertical_alignment = 1  # WD_CELL_VERTICAL_ALIGNMENT.TOP


def _group_rows(rows):
    groups = []
    current_node = object()
    current = []
    for row in rows:
        node_id = (row.get('merge_key') or (None,))[0]
        if current and node_id != current_node:
            groups.append(current)
            current = []
        current_node = node_id
        current.append(row)
    if current:
        groups.append(current)
    return groups


def _merge_vertical_cells(table, rows):
    """Merge contiguous non-empty cells using the Office export identities."""
    for column in range(len(_HEADERS)):
        start = 1  # row 0 is the repeated table header
        while start < len(rows) + 1:
            row_index = start - 1
            values = rows[row_index].get('values') or []
            keys = rows[row_index].get('office_merge_keys') or ()
            value = values[column] if column < len(values) else ''
            key = keys[column] if column < len(keys) else None
            end = start + 1
            while end <= len(rows):
                next_values = rows[end - 1].get('values') or []
                next_keys = rows[end - 1].get('office_merge_keys') or ()
                next_value = (next_values[column]
                              if column < len(next_values) else '')
                next_key = (next_keys[column]
                            if column < len(next_keys) else None)
                if key is None or not value or next_key != key or not next_value:
                    break
                end += 1
            if end - start > 1 and value:
                # Continuation cells are visual placeholders in the worksheet
                # grid. Clear them before merging so python-docx does not
                # concatenate the same value repeatedly in the merged cell.
                for continuation in range(start + 1, end):
                    table.cell(continuation, column).text = ''
                table.cell(start, column).merge(table.cell(end - 1, column))
            start = end


def _add_node_table(document, rows, page_width_mm, margin_mm):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Mm, Pt

    table = document.add_table(rows=1, cols=len(_HEADERS))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    usable_width = page_width_mm - 2 * margin_mm
    total_ratio = sum(_WIDTH_RATIOS)
    widths = [usable_width * ratio / total_ratio for ratio in _WIDTH_RATIOS]

    for column, header in enumerate(_HEADERS):
        cell = table.cell(0, column)
        cell.width = Mm(widths[column])
        _set_cell_shading(cell, 'EEECE1')
        _set_cell_borders(cell)
        _set_cell_text(cell, header, bold=True, size=7.5, align='center')
    _set_repeat_table_header(table.rows[0])
    _prevent_row_split(table.rows[0])

    for row_data in rows:
        cells = table.add_row().cells
        values = row_data.get('values') or [''] * len(_HEADERS)
        for column, cell in enumerate(cells):
            cell.width = Mm(widths[column])
            value = values[column] if column < len(values) else ''
            background = 'FFFFFF'
            foreground = '17191C'
            bold = column in (5, 8, 9)
            if column == 8:
                background = 'F3F4F6'
            if column == 5 and row_data.get('risk_before'):
                _, background, foreground = risk_info(
                    row_data['risk_before'][1], row_data['risk_before'][2])
            elif column == 9 and row_data.get('risk_after'):
                _, background, foreground = risk_info(
                    row_data['risk_after'][1], row_data['risk_after'][2])
            _set_cell_shading(cell, background)
            _set_cell_borders(cell)
            _set_cell_text(
                cell, value, bold=bold, colour=foreground,
                size=7.2, align='center' if column in (3, 5, 7, 8, 9) else 'left')
        _prevent_row_split(table.rows[-1])

    _merge_vertical_cells(table, rows)
    document.add_paragraph().paragraph_format.space_after = Pt(0)


def export_worksheet_word(db, filepath, paper_size='A4'):
    """Export the full Worksheet to a landscape A3/A4 Word document."""
    try:
        from docx import Document
        from docx.enum.section import WD_ORIENT
        from docx.shared import Mm
    except ImportError:
        return False, 'python-docx saknas.\nKör: pip install python-docx'

    paper_size = str(paper_size or 'A4').upper()
    if paper_size not in PAPER_SIZES_MM:
        return False, f'Okänt pappersformat: {paper_size}'
    try:
        rows = list(_worksheet_rows(db))
        if not rows:
            return False, 'Worksheeten innehåller inga rader att exportera.'
        page_width, page_height = PAPER_SIZES_MM[paper_size]
        margin = 8 if paper_size == 'A3' else 7

        document = Document()
        section = document.sections[0]
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = Mm(page_width)
        section.page_height = Mm(page_height)
        section.left_margin = Mm(margin)
        section.right_margin = Mm(margin)
        section.top_margin = Mm(margin)
        section.bottom_margin = Mm(margin)
        section.header_distance = Mm(3)
        section.footer_distance = Mm(3)

        for index, node_rows in enumerate(_group_rows(rows)):
            if index:
                document.add_page_break()
            _add_node_table(document, node_rows, page_width, margin)

        document.save(filepath)
        return True, ''
    except Exception as error:
        return False, str(error)
