import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from docx import Document
from docx.oxml.ns import qn
import database
from database import Database, load_matrix
from report_word_export import (
    export_report_word, _matrix_display_values, _report_snapshot,
    collect_report_data, _worksheet_references,
)
from worksheet_export import _worksheet_rows
from worksheet_word_export import export_worksheet_word
from recommendations_word_export import (
    _build_position_maps, _reference_for_consequence, export_recommendations_word,
)


class ReportWordExportTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'report.docx'
        self.node = self.db.nodes()[0]['id']
        self.deviation = self.db.deviations(self.node)[0]['id']
        self.cause = self.db.add_cause(self.deviation)
        self.db.update_cause(self.cause, description='Ventil felar öppen', likelihood=2)
        self.cons = self.db.add_consequence(self.cause)
        self.db.update_consequence(self.cons, 'Överfyllning', 3)
        self.db.set_config('project_name', 'Projekt ÅÄÖ')
        self.db.set_config('project_client', 'Testkund')
        self.rec = self.db.add_recommendation('Kontrollera ventilen', responsible='Drift',
                                             due_date='2026-10-01', status='Öppen')
        self.db.link_recommendation_to_consequence(self.rec, self.cons)

    def tearDown(self):
        self.db.conn.close()
        self.folder.cleanup()

    def export(self, **kwargs):
        ok, error = export_report_word(self.db, self.path, **kwargs)
        self.assertTrue(ok, error)
        return Document(self.path)

    def test_complete_report_uses_project_and_signoff_fields_without_legacy_customer(self):
        self.db.add_project_custom_field('Framtagen av', 'Författare')
        self.db.add_project_custom_field('Rapportnummer', '262054-Report-02')
        self.db.add_project_custom_field('Omfattning', 'Tank och lastning')
        self.db.add_project_custom_field('Eget fält', 'Eget värde')
        self.export()
        with ZipFile(self.path) as archive:
            xml = '\n'.join(archive.read(n).decode('utf-8') for n in archive.namelist()
                            if n.endswith('.xml'))
        for value in ('Projekt ÅÄÖ', 'Testkund', 'Författare', 'Tank och lastning',
                      '262054-Report-02', 'Bilaga 4', '5.2 Fortsatt hantering'):
            self.assertIn(value, xml)
        for value in ('Mondi', 'Aurora', 'HEAD Engineering', '53 åtgärder', 'SIL 3'):
            self.assertNotIn(value, xml)
        self.assertIn('w:updateFields', xml)
        self.assertIn('TOC ', xml)

    def test_cover_metadata_labels_have_a_space_before_their_values(self):
        self.db.add_project_custom_field('Distribution', 'Enligt kundens anvisning')
        doc = self.export()
        metadata = [
            paragraph.text
            for row in doc.tables[0].rows
            for cell in row.cells
            for paragraph in cell.paragraphs
        ]
        self.assertTrue(any(text.startswith('Titel: HAZOP för ') for text in metadata))
        self.assertTrue(any(text.startswith('Datum: ') for text in metadata))
        self.assertTrue(any(text.startswith('Distribution: Enligt ') for text in metadata))

    def test_source_backed_prosa_furniture_and_descriptive_chapter_text_are_preserved(self):
        doc = self.export()
        self.assertEqual(doc.styles['Normal'].font.name, 'Aptos')
        self.assertEqual(str(doc.styles['Heading 1'].font.color.rgb), '509628')
        self.assertEqual(str(doc.styles['Heading 2'].font.color.rgb), '509628')
        self.assertTrue(doc.sections[0].different_first_page_header_footer)
        self.assertTrue(all(not section.header.is_linked_to_previous
                            for section in doc.sections[2:]))
        for section in doc.sections[2:]:
            self.assertIsNotNone(next(
                section.header._element.iter(qn('a:blip')), None))
            self.assertIsNotNone(next(
                section.first_page_header._element.iter(qn('a:blip')), None))
        body_text = '\n'.join(paragraph.text for paragraph in doc.paragraphs)
        for text in (
            'P&ID-ritningarna och övriga registrerade dokument för riskanalysen redovisas i tabell 2-1',
            'Datum, tid och plats för genomförda analystillfällen redovisas i tabell 3-1',
            'Tabell B4-1 redovisar varje rekommendation',
            'HAZOP-studien genomfördes som en gemensam, tvärdisciplinär genomgång',
            'Resultatet ska användas som underlag för fortsatt riskhantering av systemet.',
            'Resultatet har dokumenterats i ett gemensamt protokoll som kan användas vid fortsatt riskhantering.',
            'Tabell 4-1 visar den riskmatris som användes i studien. Axelriktning, färger och nivånamn framgår av tabellen.',
            'vid utfärdande av rapport.',
            '3.1 Metod och arbetssätt',
            'Arbetsgången har varit följande',
            'Bilaga 1 Avvikelser',
            '5.1 Resultat',
            '5.2 Fortsatt hantering',
            'Risk reduction through inherently safer design',
            'Risk reduction through procedures',
            'Bilaga 5 Nodmarkeringar',
        ):
            self.assertIn(text, body_text)
        self.assertNotIn('Bilaga 1 HAZOP metodik', body_text)
        self.assertNotIn('B1.1 Registrerade avvikelser', body_text)
        self.assertNotIn('B4.1 Rekommendationsregister', body_text)
        with ZipFile(self.path) as archive:
            header_xml = archive.read('word/header4.xml').decode('utf-8')
        self.assertIn('HAZOP f', header_xml)

    def test_missing_values_are_yellow_while_zero_is_preserved(self):
        self.db.add_project_custom_field('Rapportrevision', '0')
        doc = self.export()
        highlights = doc.element.xpath('.//w:r[w:rPr/w:highlight[@w:val="yellow"]]')
        text = '\n'.join(''.join(r.itertext()) for r in highlights)
        self.assertIn('[KOMPLETTERA: revisionsdatum]', text)
        self.assertIn('[KOMPLETTERA: granskad av]', text)
        self.assertNotIn('[KOMPLETTERA: rapportrevision]', text)

    def test_snapshot_preserves_database_and_matrix_binding_even_on_failure(self):
        other = Database(':memory:')
        try:
            load_matrix(other)
            matrix_before = database.get_matrix()
            before = self.db.conn.serialize()
            undo_before = list(self.db._undo_stack)
            with patch('report_word_export.build_report', side_effect=ValueError('forced')):
                ok, error = export_report_word(self.db, self.path)
            self.assertFalse(ok)
            self.assertEqual(error, 'forced')
            self.assertEqual(self.db.conn.serialize(), before)
            self.assertEqual(self.db._undo_stack, undo_before)
            self.assertIs(database._risk_matrix_cache._db, other)
            self.assertIs(database.get_matrix(), matrix_before)
        finally:
            other.conn.close()

    def test_export_from_wal_source_is_read_only_and_not_a_migration(self):
        path = Path(self.folder.name) / 'project.db'
        disk = Database(path)
        try:
            before = disk.conn.serialize()
            ok, error = export_report_word(disk, self.path)
            self.assertTrue(ok, error)
            self.assertEqual(before, disk.conn.serialize())
        finally:
            disk.conn.close()

    def test_attendance_distinguishes_missing_absent_and_present_with_notes(self):
        first = self.db.add_participant('Anna', 'Andersson')
        second = self.db.add_participant('Bo', 'Berg')
        third = self.db.add_participant('Carl', 'Carlsson')
        session = self.db.add_analysis_session('Dag 1', '2026-09-08', 'Testplats')
        self.db.set_attendance(first, session, False)
        self.db.set_attendance(second, session, True)
        self.db.set_attendance_note(second, session, 'Del av dagen')
        doc = self.export()
        table = next(t for t in doc.tables if t.cell(0, 0).text == 'Deltagare')
        self.assertEqual(table.cell(1, 1).text, 'Frånvarande')
        self.assertEqual(table.cell(2, 1).text, 'Närvarande\nDel av dagen')
        self.assertEqual(table.cell(3, 1).text, '[KOMPLETTERA: närvaro]')

    def test_duplicate_guide_words_follow_worksheet_numbering_in_both_reports(self):
        equipment = self.db.add_equipment_item('V-200', 'V-200', 'V', 0, 'Ventil', '', False)
        deviation = self.db.conn.execute(
            'INSERT INTO deviations(node_id,description,equipment_id) VALUES (?,?,?)',
            (self.node, self.db.deviations(self.node)[0]['description'], equipment)).lastrowid
        self.db.conn.commit()
        cause = self.db.add_cause(deviation)
        self.db.update_cause(cause, 'Andra orsaken', 2)
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Andra konsekvensen', 2)
        self.db.link_recommendation_to_consequence(self.rec, consequence)
        with _report_snapshot(self.db) as snap:
            data = collect_report_data(snap)
            reference = data['refs'][consequence]
            maps = _build_position_maps(snap)
            self.assertEqual(reference, _reference_for_consequence(consequence, maps))
            self.assertIn(reference, data['recommendation_rows'][0][5])

    def test_appendix_reuses_all_worksheet_values_and_recommendation_numbers(self):
        with _report_snapshot(self.db) as snap:
            rows = list(_worksheet_rows(snap))
        doc = self.export()
        worksheet_tables = [t for t in doc.tables if t.cell(0, 0).text == 'Nod']
        all_values = '\n'.join(c.text for t in worksheet_tables for r in t.rows for c in r.cells)
        for row in rows:
            for value in row['values']:
                if value:
                    self.assertIn(value, all_values)
        rec_table = next(t for t in doc.tables if t.cell(0, 0).text == 'Rek. nr')
        self.assertEqual(len(rec_table.rows), 2)
        self.assertEqual(rec_table.cell(1, 0).text, '001')
        self.assertIn('w:tblHeader', rec_table.rows[0]._tr.xml)
        self.assertAlmostEqual(doc.sections[-1].page_width.mm, 420, places=1)

    def test_a4_landscape_and_unknown_format_does_not_overwrite_existing_file(self):
        doc = self.export(paper_size='A4')
        self.assertAlmostEqual(doc.sections[0].page_width.mm, 210, delta=0.2)
        self.assertAlmostEqual(doc.sections[-1].page_width.mm, 297, places=1)
        original = self.path.read_bytes()
        ok, _ = export_report_word(self.db, self.path, 'A2')
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), original)

    def test_empty_template_has_no_demo_risk_matrix_or_sil_ratings(self):
        self.db.conn.execute('DELETE FROM nodes')
        self.db.conn.commit()
        doc = self.export(standard_template=True)
        body_text = '\n'.join(p.text for p in doc.paragraphs)
        table_text = '\n'.join(c.text for t in doc.tables for r in t.rows for c in r.cells)
        self.assertFalse(any(t.cell(0, 0).text == 'Konsekvens / frekvens' for t in doc.tables))
        self.assertTrue(any('studiens riskmatris' in p.text for p in doc.paragraphs))
        self.assertNotIn('noder och designavsikt', body_text)
        self.assertIn('Datum / analystillfälle', table_text)
        self.assertIn('[KOMPLETTERA: antal analystillfällen, noder och rekommendationer]', body_text)
        self.assertNotIn('Sammanställning av huvudresultat', body_text)

    def test_summary_reports_sessions_and_lists_nodes_in_numbered_style(self):
        self.db.add_analysis_session('Tillfälle 1', '2026-09-09', 'Kontoret')
        doc = self.export()
        body_text = '\n'.join(p.text for p in doc.paragraphs)
        self.assertIn('Studien genomfördes vid 1 analystillfälle', body_text)
        self.assertIn('Studerade noder', body_text)
        self.assertIn('Analysen omfattade 1 nod.', body_text)
        self.assertNotIn('Analysgruppen dokumenterade', body_text)
        node_names = {row['name'] for row in self.db.nodes()}
        numbered = [p for p in doc.paragraphs if p.style.name == 'List Number']
        self.assertTrue(node_names.issubset({p.text for p in numbered}))
        node_sequence = next(p for p in numbered if p.text in node_names)
        method_sequence = next(p for p in numbered if p.text.startswith('Nodens avsedda'))
        self.assertNotEqual(node_sequence._p.pPr.numPr.numId.val,
                            method_sequence._p.pPr.numPr.numId.val)

    def test_report_structure_is_compact_and_appendix_nodes_are_explicit(self):
        doc = self.export()
        body_text = '\n'.join(p.text for p in doc.paragraphs)
        headings = [p.text for p in doc.paragraphs
                    if p.style.name in ('Heading 1', 'Heading 2')]
        self.assertIn('3 Genomförande', headings)
        self.assertIn('3.3 Analysgrupp och närvaro', headings)
        self.assertNotIn('3.4 Närvaro', headings)
        self.assertNotIn('2.1 Ritningsunderlag', headings)
        self.assertNotIn('B1.1 Registrerade avvikelser', headings)
        self.assertNotIn('B4.1 Rekommendationsregister', headings)
        self.assertIn('B3.1 Ny nod', body_text)
        node_heading = next(p for p in doc.paragraphs if p.text == 'B3.1 Ny nod')
        self.assertEqual(node_heading.style.name, 'Appendix Node Heading')

    def test_protocol_starts_each_node_after_the_first_on_a_new_page(self):
        second_node = self.db.add_node()
        self.db.update_node(second_node, 'Nod två', '', '')
        second_deviation = self.db.deviations(second_node)[0]['id']
        second_cause = self.db.add_cause(second_deviation)
        self.db.update_cause(second_cause, description='Pump stannar', likelihood=2)
        second_consequence = self.db.add_consequence(second_cause)
        self.db.update_consequence(second_consequence, 'Processavbrott', 3)
        self.db.commit()

        doc = self.export()
        paragraphs = list(doc.paragraphs)
        second_heading_index = next(
            index for index, paragraph in enumerate(paragraphs)
            if paragraph.text == 'B3.2 Nod två')

        self.assertIn('w:type="page"', paragraphs[second_heading_index - 1]._p.xml)

    def test_table_captions_and_prose_references_use_word_fields(self):
        doc = self.export()
        captions = [p for p in doc.paragraphs
                    if p.style.name == 'Caption' and p.text.startswith('Tabell ')]
        self.assertIn('Tabell 4-1 Riskmatris', [p.text for p in captions])
        for paragraph in captions:
            instructions = [
                field.text or ''
                for field in paragraph._p.iter(qn('w:instrText'))
            ]
            self.assertTrue(any(instruction.startswith('SEQ Tabell ') for instruction in instructions),
                            paragraph.text)
            self.assertTrue(any(True for _ in paragraph._p.iter(qn('w:bookmarkStart'))),
                            paragraph.text)

        bookmark_names = {
            bookmark.get(qn('w:name'))
            for bookmark in doc.element.iter(qn('w:bookmarkStart'))
        }
        ref_instructions = [
            field.text or ''
            for field in doc.element.iter(qn('w:instrText'))
            if (field.text or '').startswith('REF tabell_')
        ]
        self.assertTrue(ref_instructions)
        self.assertIn('REF tabell_4_1 \\h', ref_instructions)
        self.assertNotIn('REF tabell_5_1 \\h', ref_instructions)
        for instruction in ref_instructions:
            self.assertIn(instruction.split()[1], bookmark_names)
        self.assertFalse(doc.styles['Appendix Node Heading'].paragraph_format.page_break_before)
        headings = [p.text for p in doc.paragraphs
                    if p.style.name in ('Heading 1', 'Heading 2')]
        self.assertIn('Bilaga 5 Nodmarkeringar', headings)
        with ZipFile(self.path) as archive:
            document_xml = archive.read('word/document.xml').decode('utf-8')
        self.assertIn('TOC \\o "1-2"', document_xml)

    def test_document_subheadings_are_added_when_other_documents_exist(self):
        self.db.add_project_custom_field('Övriga referensdokument', 'Driftinstruktion 100')
        doc = self.export()
        headings = [p.text for p in doc.paragraphs if p.style.name == 'Heading 2']
        self.assertIn('2.1 Ritningsunderlag', headings)
        self.assertIn('2.2 Övrigt dokumentunderlag', headings)

    def test_report_matrix_uses_same_axis_directions_as_program(self):
        matrix = {
            'rows': 2, 'cols': 3,
            'x_codes': ['F0', 'F1', 'F2'],
            'y_codes': ['C1', 'C2'],
            'cell_labels': [['00', '01', '02'], ['10', '11', '12']],
            'x_axis': 'consequence', 'x_reversed': False, 'y_reversed': False,
        }
        headers, rows, horizontal, vertical, x_frequency = _matrix_display_values(matrix)
        self.assertFalse(x_frequency)
        self.assertEqual(headers, ['Frekvens / konsekvens', 'C1', 'C2'])
        self.assertEqual(vertical, [2, 1, 0])
        self.assertEqual(rows, [
            ['F2', '02', '12'], ['F1', '01', '11'], ['F0', '00', '10'],
        ])

        matrix.update(x_axis='frequency', x_reversed=True, y_reversed=True)
        headers, rows, horizontal, vertical, x_frequency = _matrix_display_values(matrix)
        self.assertTrue(x_frequency)
        self.assertEqual(headers, ['Konsekvens / frekvens', 'F2', 'F1', 'F0'])
        self.assertEqual(vertical, [0, 1])
        self.assertEqual(rows, [['C1', '02', '01', '00'], ['C2', '12', '11', '10']])


class ReportMenuTests(unittest.TestCase):
    def test_report_menu_cancel_success_and_error(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import hazop
        fake = SimpleNamespace(db=object(), status_bar=Mock())
        with patch('hazop.QFileDialog.getSaveFileName', return_value=('', '')), \
                patch('report_word_export.export_report_word') as export:
            hazop.MainWindow._export_word_report(fake)
            export.assert_not_called()
        with patch('hazop.QFileDialog.getSaveFileName', return_value=('report', '')), \
                patch('hazop.QInputDialog.getItem', return_value=('A3', True)), \
                patch('hazop.QApplication.focusWidget', return_value=None), \
                patch('hazop.QMessageBox.information'), \
                patch('hazop.QMessageBox.critical') as critical, \
                patch('hazop.QDesktopServices.openUrl') as open_url, \
                patch('report_word_export.export_report_word', return_value=(True, '')) as export:
            hazop.MainWindow._export_word_report(fake)
            export.assert_called_once_with(fake.db, 'report.docx', 'A3')
            open_url.assert_called_once()
            export.return_value = (False, 'locked')
            open_url.reset_mock()
            hazop.MainWindow._export_word_report(fake)
            critical.assert_called_once_with(fake, 'Fel vid rapportexport', 'locked')
            open_url.assert_not_called()


if __name__ == '__main__':
    unittest.main()
