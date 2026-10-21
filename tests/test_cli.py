import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
import uuid

from textbook_pdf.models import Book,Element
from textbook_pdf.store import Store
from textbook_pdf.cli import main

class CliStateTests(unittest.TestCase):
    def test_qa_preserves_custom_glossary_batches_and_live_agent_lease(self):
        base=Path(__file__).resolve().parent/'_tmp_cli';base.mkdir(exist_ok=True)
        directory=base/uuid.uuid4().hex;directory.mkdir()
        try:
            book=Book('fixture.pdf','fixture-sha','Fixture',1,[1],elements=[Element('one','paragraph','A unit. '*30,1),Element('two','paragraph','Another unit. '*25,1)])
            book.save(directory/'book.json')
            glossary=directory/'custom.json';glossary.write_text(json.dumps({'unit':'单元'}),encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                main(['--out',str(directory),'--stage','prepare','--target-chars','300','--glossary',str(glossary),'--no-legacy-import'])
            with Store(directory/'tasks.db') as store:
                before=[r[0] for r in store.db.execute("select id from tasks where state!='superseded' order by id")]
                task=store.claim('active-agent')
            with contextlib.redirect_stdout(io.StringIO()):
                main(['--out',str(directory),'--stage','qa','--no-legacy-import'])
            with Store(directory/'tasks.db') as store:
                after=[r[0] for r in store.db.execute("select id from tasks where state!='superseded' order by id")]
                self.assertEqual(before,after)
                self.assertEqual(store.glossary,{'unit':'单元'})
                row=store.db.execute('select state,owner from tasks where id=?',(task['id'],)).fetchone()
                self.assertEqual(tuple(row),('leased','active-agent'))
        finally:
            self.assertTrue(directory.resolve().is_relative_to(base.resolve()))
            shutil.rmtree(directory)

    def test_changed_translation_cannot_validate_an_old_pdf(self):
        import pymupdf
        from textbook_pdf.layout import rendering_binding
        from textbook_pdf.models import file_sha256
        from textbook_pdf.qa import verify
        base=Path(__file__).resolve().parent/'_tmp_cli';base.mkdir(exist_ok=True)
        directory=base/uuid.uuid4().hex;directory.mkdir()
        try:
            pdf=directory/'old.pdf'
            with pymupdf.open() as doc:
                doc.new_page(width=160,height=160).insert_text((15,40),'Old rendered text')
                doc.save(pdf)
            book=Book('fixture.pdf','fixture-sha','Fixture',1,[1],elements=[Element('one','paragraph','Original paragraph.',1)])
            translations={'one':'第一版译文。'}
            mapping={'elements':{'one':[{'page':1,'bbox':[15,20,140,50]}]},'assets':{},'pages':[{'page':1,'source_pages':[1]}],
                     'render_binding':rendering_binding(book,translations),'pdf_sha256':file_sha256(pdf)}
            current=verify(book,translations,directory,pdf_path=pdf,layout_map=mapping)
            self.assertEqual(current['render_revision'],'current')
            changed=verify(book,{'one':'修订后的译文。'},directory,pdf_path=pdf,layout_map=mapping)
            self.assertEqual(changed['render_revision'],'stale')
            self.assertFalse(changed['technical_checks_passed'])
            self.assertTrue(any(i['code']=='stale_rendered_pdf' for i in changed['issues']))
        finally:
            self.assertTrue(directory.resolve().is_relative_to(base.resolve()))
            shutil.rmtree(directory)

    def test_all_stops_before_paid_requests_on_known_source_error(self):
        from unittest.mock import patch
        base=Path(__file__).resolve().parent/'_tmp_cli';base.mkdir(exist_ok=True)
        directory=base/uuid.uuid4().hex;directory.mkdir()
        try:
            book=Book('fixture.pdf','fixture-sha','Fixture',1,[1],elements=[Element('one','paragraph','Original paragraph.',1)],
                      issues=[{'code':'source_atomicity','severity':'error','page':1,'detail':'Known unsafe source'}])
            book.save(directory/'book.json')
            with contextlib.redirect_stdout(io.StringIO()),patch('textbook_pdf.translation.run_translation',side_effect=AssertionError('Must not make a paid request')):
                result=main(['--out',str(directory),'--stage','all','--no-legacy-import'])
            self.assertEqual(result,2)
            report=json.loads((directory/'quality_report.json').read_text(encoding='utf-8'))
            self.assertFalse(report['technical_checks_passed'])
            self.assertTrue(any(i['code']=='composition_blocked' for i in report['issues']))
        finally:
            self.assertTrue(directory.resolve().is_relative_to(base.resolve()))
            shutil.rmtree(directory)

    def test_missing_protected_source_is_reported_before_task_creation(self):
        base=Path(__file__).resolve().parent/'_tmp_cli';base.mkdir(exist_ok=True)
        directory=base/uuid.uuid4().hex;directory.mkdir()
        try:
            book=Book('fixture.pdf','fixture-sha','Fixture',1,[1],elements=[Element('one','paragraph','Consider ⟦F:missing⟧.',1,asset_ids=['missing'])])
            book.save(directory/'book.json')
            with contextlib.redirect_stdout(io.StringIO()):
                result=main(['--out',str(directory),'--stage','qa','--no-legacy-import'])
            self.assertEqual(result,2)
            report=json.loads((directory/'quality_report.json').read_text(encoding='utf-8'))
            self.assertFalse(report['technical_checks_passed'])
            self.assertTrue(any(i['code']=='source_asset_reference_missing' for i in report['issues']))
        finally:
            self.assertTrue(directory.resolve().is_relative_to(base.resolve()))
            shutil.rmtree(directory)

    def test_legacy_migration_rechecks_changed_source_and_preserves_previous_report(self):
        from unittest.mock import patch
        base=Path(__file__).resolve().parent/'_tmp_cli';base.mkdir(exist_ok=True)
        directory=base/uuid.uuid4().hex;directory.mkdir()
        try:
            book=Book('fixture.pdf','fixture-sha','Fixture',1,[1],elements=[Element('one','paragraph','Original paragraph.',1)])
            book.save(directory/'book.json')
            legacy=directory/'legacy';legacy.mkdir()
            (legacy/'manifest.json').write_text('{}',encoding='utf-8')
            (legacy/'_cache.db').write_bytes(b'fixed read-only fixture')
            args=['--out',str(directory),'--stage','prepare','--import-legacy',str(legacy)]
            with contextlib.redirect_stdout(io.StringIO()),patch('textbook_pdf.migration.import_legacy',return_value={'imported':0}) as migrate:
                self.assertEqual(main(args),0)
                self.assertEqual(main(args),0)
                self.assertEqual(migrate.call_count,1)
                book.profile['body_size']=11
                book.save(directory/'book.json')
                self.assertEqual(main(args),0)
                self.assertEqual(migrate.call_count,1)
                book.elements[0].text='A corrected source paragraph.'
                book.save(directory/'book.json')
                self.assertEqual(main(args),0)
                self.assertEqual(migrate.call_count,2)
                self.assertEqual(len(list((directory/'migrations').glob('*.json'))),1)
                (legacy/'_cache.db').write_bytes(b'newly completed old translations')
                self.assertEqual(main(args),0)
                self.assertEqual(migrate.call_count,3)
        finally:
            self.assertTrue(directory.resolve().is_relative_to(base.resolve()))
            shutil.rmtree(directory)
