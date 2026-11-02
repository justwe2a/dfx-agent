import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from dfx_agent.context_review import export_review
from dfx_agent.run_report import export_run
from dfx_agent.workspace import load_case, read_json, save_json


class PortableCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original location'
        source = self.original / 'data' / 'app'
        source.mkdir(parents=True)
        (source / 'main.js').write_text('function bad() { return 0; }', encoding='utf-8')
        (self.original / 'data' / 'crash.log').write_text(
            'Error name:TypeError\nError message:bad\nStacktrace:\n at bad (main.js:1:1)\n', encoding='utf-8')
        self.config = {'schema_version': 2, 'case_id': 'synthetic/portable', 'source_version': 'v1',
                       'source_root': '${workspaces}/app', 'logs': ['${workspaces}/crash.log'],
                       'visible_files': ['main.js'], 'editable_files': ['main.js'],
                       'task_description': 'Keep literal ${not_a_host_key} in task data',
                       'validation': {'build': {'argv': ['${python}', '${repo}/scripts/harmony-adapter.py',
                                                         '--device', '${device}']}}}
        save_json(self.original / 'case.json', self.config)
        save_json(self.original / 'host.json', {'workspaces': 'data', 'python': sys.executable,
                                                'device': 'test-device'})

    def test_relocation_preserves_model_preview_and_resolves_host_relative_paths(self):
        moved = self.root / 'moved location'
        shutil.copytree(self.original, moved)
        outputs = []
        for base in (self.original, moved):
            cfg = load_case(base / 'case.json', base / 'host.json')
            self.assertEqual(Path(cfg['source_root']), base / 'data' / 'app')
            self.assertEqual(cfg['validation']['build']['argv'][0], sys.executable)
            self.assertEqual(cfg['validation']['build']['argv'][-1], 'test-device')
            self.assertIn('${not_a_host_key}', cfg['task_description'])
            export_review(base / 'case.json', base / 'review', base / 'host.json')
            outputs.append(read_json(base / 'review' / 'initial-request.preview.json'))
        self.assertEqual(outputs[0], outputs[1])

    def test_missing_host_value_is_explicit_and_no_environment_fallback(self):
        with self.assertRaisesRegex(ValueError, 'Missing host variable: workspaces'):
            load_case(self.original / 'case.json')
        save_json(self.original / 'host.json', {'workspaces': 'data'})
        with self.assertRaisesRegex(ValueError, 'Missing host variable: python'):
            load_case(self.original / 'case.json', self.original / 'host.json')

    def test_host_is_only_paths_and_device_not_credentials_or_scope(self):
        save_json(self.original / 'host.json', {'visible_files': ['private.js']})
        with self.assertRaisesRegex(ValueError, 'Host config accepts'):
            load_case(self.original / 'case.json', self.original / 'host.json')


class ExportRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'run-example'
        self.stages = ('build', 'deploy', 'replay', 'functional', 'regression')
        self.passed = {'stages': {s: {'status': 'PASS', 'command': {'seconds': 1, 'output_tail': 'RAW_LOG_NOT_EXPORTED'}} for s in self.stages}}
        self.value = {'run_id': self.run.name, 'case_id': 'development/example', 'snapshot_id': 'v1',
                      'evaluation_scope': 'competition_development', 'status': 'VALIDATED',
                      'baseline_status': 'REPRODUCED', 'repair_validated': True,
                      'paid_api_calls': 2, 'model_calls': 2, 'model_kind': 'deepseek', 'validated_attempt': 1,
                      'attempts': []}
        for i in (1, 2):
            directory = f'attempts/{i:03d}'
            save_json(self.run / directory / 'proposal.json', {'localization': {'root_cause': {'path': 'app.js', 'line': 4},
                                                                             'explanation': 'model explanation'},
                                                             'edits': [{'path': 'app.js'}]})
            (self.run / directory / 'patch.diff').write_text(f'candidate {i}', encoding='utf-8')
            self.value['attempts'].append({'number': i, 'directory': directory,
                                          'validation': {'stages': {'build': {'status': 'FAIL', 'reason': 'compiler failed'}}}})
        self.value['attempts'][0]['revalidations'] = [{'number': 1, 'reason': 'adapter corrected', 'status': 'PASS',
                                                     'validation': self.passed}]
        self.write()
        save_json(self.run / 'baseline-proof.json', {'case_id': self.value['case_id'], 'snapshot_id': 'v1',
                                                    'status': 'REPRODUCED', 'run_id': 'run-original'})
        events = [{'at': '2026-10-15T00:00:00+00:00', 'event': 'model_call_started'},
                  {'at': '2026-10-15T00:00:05+00:00', 'event': 'response_processed', 'action': 'propose'},
                  {'at': '2026-10-15T00:01:00+00:00', 'event': 'limit_reached'},
                  {'at': '2026-10-15T00:10:00+00:00', 'event': 'revalidation_started', 'attempt': 1},
                  {'at': '2026-10-15T00:12:00+00:00', 'event': 'revalidation_finished', 'attempt': 1}]
        (self.run / 'trajectory.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')

    def write(self):
        save_json(self.run / 'result.json', self.value)

    def test_report_preserves_failed_attempts_and_selects_revalidated_candidate(self):
        before = (self.run / 'result.json').read_bytes()
        export_run(self.run, self.root / 'report')
        report = read_json(self.root / 'report' / 'report.json')
        self.assertTrue(report['verified_saved_repair'])
        self.assertEqual(report['reported_attempt'], 1)
        self.assertTrue(report['baseline_proof']['reused'])
        self.assertEqual(report['attempts'][1]['original_validation']['build']['status'], 'FAIL')
        self.assertEqual(report['selected_validation']['regression']['status'], 'PASS')
        self.assertEqual(report['timing']['initial_workflow_seconds'], 60)
        self.assertEqual(report['timing']['revalidations'][0]['seconds'], 120)
        self.assertIsNone(report['usage'])
        self.assertEqual((self.root / 'report' / 'attempt-001.diff').read_bytes(), b'candidate 1')
        self.assertEqual((self.run / 'result.json').read_bytes(), before)
        self.assertNotIn('RAW_LOG_NOT_EXPORTED', json.dumps(report))

    def test_missing_candidate_evidence_and_fixture_cannot_claim_real_repair(self):
        (self.run / 'attempts/001/patch.diff').unlink()
        export_run(self.run, self.root / 'missing')
        report = read_json(self.root / 'missing/report.json')
        self.assertFalse(report['verified_saved_repair'])
        self.assertTrue(report['issues'])
        self.value['fixture_assisted'] = True
        self.write()
        export_run(self.run, self.root / 'fixture')
        self.assertFalse(read_json(self.root / 'fixture/report.json')['verified_saved_repair'])

    def test_timing_includes_format_feedback_but_preserves_other_error_boundaries(self):
        for mode in ('format_continued', 'transport_paused', 'format_exhausted'):
            with self.subTest(mode=mode):
                events = [{'at': '2026-10-16T00:00:00+00:00', 'event': 'model_call_started'},
                          {'at': '2026-10-16T00:00:03+00:00', 'event': 'model_error'}]
                if mode != 'transport_paused':
                    events.append({'at': '2026-10-16T00:00:04+00:00', 'event': 'response_rejected',
                                   'reason': 'Completed response has invalid structured content'})
                if mode == 'format_exhausted':
                    events.append({'at': '2026-10-16T00:00:05+00:00', 'event': 'limit_reached'})
                else:
                    events.extend([{'at': '2026-10-16T00:00:05+00:00', 'event': 'model_call_started'},
                                   {'at': '2026-10-16T00:10:00+00:00', 'event': 'finished'}])
                trajectory = self.run / 'trajectory.jsonl'
                trajectory.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
                before = trajectory.read_bytes()
                export_run(self.run, self.root / mode)
                report = read_json(self.root / mode / 'report.json')
                self.assertEqual(report['timing']['initial_workflow_seconds'], 600 if mode == 'format_continued' else 3)
                self.assertEqual(trajectory.read_bytes(), before)

    def test_failed_run_with_no_candidate_stays_unverified_and_no_stage_is_invented(self):
        self.value.update(status='EXHAUSTED', attempts=[], validated_attempt=None, repair_validated=False)
        self.write()
        export_run(self.run, self.root / 'failed')
        report = read_json(self.root / 'failed/report.json')
        self.assertEqual(report['saved_status'], 'EXHAUSTED')
        self.assertFalse(report['verified_saved_repair'])
        self.assertEqual(report['selected_validation']['build']['status'], 'NOT_RECORDED')
        with self.assertRaises(ValueError):
            export_run(self.run, self.root / 'failed')
        with self.assertRaises(ValueError):
            export_run(self.run, self.run / 'report')


if __name__ == '__main__':
    unittest.main()
