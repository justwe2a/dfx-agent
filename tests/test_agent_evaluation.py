"""Real local process checks of posthoc provenance; no SDK, device or model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent_evaluation import evaluate
from dfx_agent.workspace import save_json
from dfx_agent.validation import STAGES


class AgentEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'solve'
        self.source = self.run / 'workspace'
        self.source.mkdir(parents=True)
        (self.source / 'source.ets').write_text('model final', encoding='utf-8')
        save_json(self.run / 'result.json', {'status': 'Submitted', 'changed_files': ['source.ets'],
                                            'independent_verification': 'NOT_RUN'})
        self.driver = self.root / 'trusted.py'
        self.driver.write_text('''import os,sys,json
from pathlib import Path
stage=os.environ['DFX_STAGE']
assert 'DEEPSEEK_API_KEY' not in os.environ
assert Path('source.ets').read_text()=='model final'
if stage=='build':
    Path('out.hap').write_bytes(b'new-local-fixture')
else:
    assert Path('out.hap').read_bytes()==b'new-local-fixture'
    report={k.lower():v for k,v in ((k[4:],v) for k,v in os.environ.items() if k in
      ['DFX_RUN_ID','DFX_ATTEMPT_ID','DFX_SNAPSHOT_ID','DFX_PATCH_ID','DFX_ARTIFACT_ID','DFX_STAGE'])}
    report['status']='FAIL' if stage==sys.argv[1] else 'PASS'
    report['details']={'actual_stage':stage}
    Path(os.environ['DFX_REPORT_PATH']).write_text(json.dumps(report))
    sys.exit(1 if report['status']=='FAIL' else 0)
''', encoding='utf-8')
        self.stages = {s: {'argv': [sys.executable, str(self.driver), 'none'], 'timeout_seconds': 10}
                       for s in STAGES}
        self.stages['build']['artifact'] = 'out.hap'
        self.config = {'source_version': 'fixture-v1', 'case_id': 'fixture',
                       'validation': self.stages,
                       'baseline_validation': {s: self.stages[s] for s in STAGES[:3]}}

    def test_final_workspace_real_commands_receipts_and_no_result_backfill(self):
        before = (self.run / 'result.json').read_bytes()
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
            result = evaluate(self.source, self.root / 'evaluation', self.config,
                              mode='posthoc', source_solve_run=self.run)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['source_solve_run'], str(self.run.resolve()))
        self.assertEqual([v['status'] for v in result['checks']['stages'].values()], ['PASS'] * 5)
        self.assertEqual((self.run / 'result.json').read_bytes(), before)
        self.assertFalse((self.source / 'out.hap').exists())
        self.assertEqual((self.source / 'source.ets').read_text(), 'model final')
        with self.assertRaisesRegex(ValueError, 'new and outside'):
            evaluate(self.source, self.root / 'evaluation', self.config,
                     mode='posthoc', source_solve_run=self.run)

    def test_first_failure_skips_later_checks_without_retry(self):
        self.stages['replay']['argv'][-1] = 'replay'
        result = evaluate(self.source, self.root / 'failed', self.config,
                          mode='posthoc', source_solve_run=self.run)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['checks']['stages']['functional']['status'], 'NOT_RUN')
        self.assertFalse((self.root / 'failed/validation/functional.log').exists())

    def test_baseline_has_own_provenance_and_only_three_required_stages(self):
        result = evaluate(self.source, self.root / 'baseline', self.config, mode='baseline')
        self.assertEqual(result['status'], 'REPRODUCED')
        self.assertIsNone(result['source_solve_run'])
        self.assertEqual(result['binding']['patch_id'], 'unmodified-input')
        self.assertEqual(result['checks']['stages']['functional']['status'], 'NOT_RUN')

    def test_blocked_source_and_empty_suite_cannot_pass(self):
        save_json(self.run / 'result.json', {'status': 'BLOCKED', 'changed_files': ['source.ets']})
        with self.assertRaisesRegex(ValueError, 'requires review'):
            evaluate(self.source, self.root / 'not-created', self.config,
                     mode='posthoc', source_solve_run=self.run)
        self.assertFalse((self.root / 'not-created').exists())
        save_json(self.run / 'result.json', {'status': 'Submitted', 'changed_files': ['source.ets']})
        self.config['validation'] = {}
        result = evaluate(self.source, self.root / 'empty', self.config,
                          mode='posthoc', source_solve_run=self.run)
        self.assertEqual(result['status'], 'INCONCLUSIVE')

    def test_reviewed_blocked_runs_real_checks_without_backfilling_source(self):
        save_json(self.run / 'result.json', {'status': 'BLOCKED', 'changed_files': ['source.ets']})
        before = (self.run / 'result.json').read_bytes()
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
            result = evaluate(self.source, self.root / 'reviewed', self.config,
                              mode='posthoc', source_solve_run=self.run, reviewed_blocked=True)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['source_result_status'], 'BLOCKED')
        self.assertTrue(result['reviewed_blocked'])
        self.assertEqual((self.run / 'result.json').read_bytes(), before)
        self.assertEqual([v['status'] for v in result['checks']['stages'].values()], ['PASS'] * 5)

    def test_reviewed_blocked_does_not_allow_unreviewed_failures_or_baseline(self):
        for status in ('Interrupted', 'Error'):
            save_json(self.run / 'result.json', {'status': status, 'changed_files': ['source.ets']})
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'requires review'):
                evaluate(self.source, self.root / status, self.config, mode='posthoc',
                         source_solve_run=self.run, reviewed_blocked=True)
        with self.assertRaisesRegex(ValueError, 'posthoc only'):
            evaluate(self.source, self.root / 'baseline', self.config, mode='baseline',
                     reviewed_blocked=True)


if __name__ == '__main__':
    unittest.main()
