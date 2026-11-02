import stat
import tempfile
from pathlib import Path
import unittest
import warnings
import zipfile
from unittest.mock import patch

from dfx_agent.log_import import import_log
from dfx_agent.workspace import read_json, save_json
from dfx_agent.runner import prepare, request_for


class LogImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.dataset = self.root / 'dataset'; self.dataset.mkdir()
        self.archive = self.dataset / 'fault.zip'; self.out = self.root / 'imported'
        self.member = 'case/faultlogger/jscrash-app.log'
        self.raw = b'Error name:SyntaxError\nError message:bad\nStacktrace:\n at f (a.ets:3:4)\n'

    def write(self, entries=None):
        with warnings.catch_warnings(), zipfile.ZipFile(self.archive, 'w', zipfile.ZIP_DEFLATED) as z:
            warnings.simplefilter('ignore', UserWarning)
            for name, raw in entries or [(self.member, self.raw)]: z.writestr(name, raw)

    def run_import(self, **kw):
        return import_log(self.archive, kw.get('member', self.member), kw.get('out', self.out), self.dataset)

    def test_exact_bytes_receipt_and_no_other_members_extracted(self):
        self.write([(self.member, self.raw), ('../untrusted.py', b'raise Exception()')])
        before = self.archive.read_bytes()
        with patch('urllib.request.build_opener', side_effect=AssertionError('offline')):
            result = self.run_import()
        self.assertEqual((self.out / 'crash.log').read_bytes(), self.raw)
        self.assertEqual(result['archive_bytes'], len(before))
        self.assertEqual(result['member_bytes'], len(self.raw))
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(set(p.name for p in self.out.iterdir()), {'crash.log', 'provenance.json'})
        self.assertEqual(read_json(self.out/'provenance.json')['context_review'], 'PENDING')

    def test_traversal_and_nonlog_selection_rejected(self):
        self.write()
        for member in ('../jscrash-a.log', 'C:/jscrash-a.log', 'case/test.py', 'case\\jscrash-a.log'):
            with self.subTest(member=member), self.assertRaises(ValueError): self.run_import(member=member)
        self.assertFalse(self.out.exists())

    def test_missing_duplicate_and_case_alias_rejected(self):
        for entries in ([('other', self.raw)], [(self.member, self.raw)]*2,
                        [(self.member, self.raw), (self.member.upper(), self.raw)]):
            self.write(entries)
            with self.assertRaises(ValueError): self.run_import()

    def test_dataset_and_existing_output_protected(self):
        self.write()
        with self.assertRaises(ValueError): self.run_import(out=self.dataset/'imported')
        with self.assertRaises(ValueError): self.run_import(out=self.root)
        self.out.mkdir(); (self.out/'keep').write_text('keep')
        with self.assertRaises(ValueError): self.run_import()
        self.assertEqual((self.out/'keep').read_text(), 'keep')

    def test_link_member_rejected(self):
        info = zipfile.ZipInfo(self.member); info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.write([(info, self.raw)])
        with self.assertRaises(ValueError): self.run_import()

    def test_limits_reject_before_creating_output(self):
        self.write()
        for name, value in [('MAX_LOG', 10), ('MAX_ARCHIVE', 10), ('MAX_MEMBERS', 0), ('MAX_RATIO', 0)]:
            with self.subTest(name=name), patch('dfx_agent.log_import.'+name, value):
                with self.assertRaises(ValueError): self.run_import()
        self.assertFalse(self.out.exists())

    def test_corrupt_archive_and_noncrash_content_rejected(self):
        self.archive.write_bytes(b'not a zip')
        with self.assertRaises(ValueError): self.run_import()
        self.write([(self.member, b'arbitrary instructions')])
        with self.assertRaises(ValueError): self.run_import()
        self.assertFalse(self.out.exists())

    def test_archive_outside_dataset_rejected(self):
        self.write()
        with self.assertRaises(ValueError):
            import_log(self.archive, self.member, self.out, self.root/'elsewhere')

    def test_imported_log_prepares_central_layout_without_test_or_labels_in_request(self):
        self.write(); self.run_import()
        source = self.dataset/'App/demo'; source.mkdir(parents=True)
        (source/'a.ets').write_text('line1\nline2\nline3\n', encoding='utf-8')
        tests = self.dataset/'HypiumProject'; tests.mkdir()
        (tests/'test.py').write_text('ANSWER_NOT_MODEL_VISIBLE', encoding='utf-8')
        labels = self.root/'evaluation/label.json'; save_json(labels, {'answer':'PRIVATE_LABEL'})
        case = self.root/'case.json'
        save_json(case, {'schema_version':2, 'source_version':'test-source-v1', 'case_id':'central/demo', 'source_root':str(source),
                        'logs':[str(self.out/'crash.log')], 'visible_files':['a.ets'],
                        'editable_files':[], 'context_review':'PENDING', 'live_model_enabled':False})
        with patch('urllib.request.build_opener', side_effect=AssertionError('offline')):
            run = prepare(case, self.root/'runs')
            state = read_json(run/'state.json')
            request = request_for(run, read_json(run/'case.json'), state)
        self.assertEqual(state['baseline_status'], 'NOT_RUN')
        self.assertEqual(state['paid_api_calls'], 0)
        self.assertEqual(request['editable_files'], [])
        self.assertNotIn('PRIVATE_LABEL', str(request))
        self.assertNotIn('ANSWER_NOT_MODEL_VISIBLE', str(request))
        self.assertEqual(read_json(run/'evidence.json')['path_mappings'][0]['status'], 'RESOLVED')
