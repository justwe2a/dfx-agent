"""Read-only delivery aggregation over real local solve/evaluation records."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from dfx_agent.agent import solve
from dfx_agent.agent_evaluation import evaluate
from dfx_agent.cli import main
from dfx_agent.run_report import write_agent_delivery
from dfx_agent.validation import STAGES
from dfx_agent.workspace import read_json, save_json


class AgentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'project'
        self.repo.mkdir()
        (self.repo/'source.ets').write_text('original', encoding='utf-8')
        self.run = self.root/'run'
        solve(self.repo, 'Local record fixture', self.run, responses=[
            {'action': 'edit', 'path': 'source.ets', 'search': 'original', 'replace': 'candidate'}],
            max_steps=1)
        self.original = (self.run/'result.json').read_bytes()
        driver = self.root/'local-check.py'
        driver.write_text('''import json,os
from pathlib import Path
assert Path('source.ets').read_text() == 'candidate'
if os.environ['DFX_STAGE']=='build':
    Path('out.hap').write_bytes(b'fixture only')
else:
    receipt={k[4:].lower():v for k,v in os.environ.items() if k in
      ['DFX_RUN_ID','DFX_ATTEMPT_ID','DFX_SNAPSHOT_ID','DFX_PATCH_ID','DFX_ARTIFACT_ID','DFX_STAGE']}
    receipt['status']='PASS'
    Path(os.environ['DFX_REPORT_PATH']).write_text(json.dumps(receipt))
''', encoding='utf-8')
        stages = {s: {'argv': [sys.executable, str(driver)]} for s in STAGES}
        stages['build']['artifact'] = 'out.hap'
        self.config = {'case_id': 'local-fixture', 'source_version': 'local-v1', 'validation': stages}

    def evaluation(self):
        out = self.root/'evaluation'
        evaluate(self.run/'workspace', out, self.config, mode='posthoc', source_solve_run=self.run)
        return out

    def test_no_evaluation_does_not_infer_failure_or_autonomy(self):
        report = write_agent_delivery(self.run, self.root/'summary')
        self.assertEqual(report['independent']['status'], 'NOT_RUN')
        self.assertFalse(report['agent_submitted'])
        self.assertIsNone(report['unverified_declared_by_model'])
        self.assertEqual(report['autonomous_completion'], 'NOT_AUTOMATICALLY_ASSESSED')
        self.assertEqual((self.run/'result.json').read_bytes(), self.original)

    def test_cli_unsubmitted_pass_preserves_original_records_and_usage(self):
        evaluation = self.evaluation()
        old_evaluation = (evaluation/'result.json').read_bytes()
        out = self.root/'report'
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(['report-agent', '--run', str(self.run), '--evaluation', str(evaluation), '--out', str(out)])
        self.assertEqual(code, 0)
        report = read_json(out/'delivery-summary.json')
        self.assertEqual(report['agent_status'], 'LimitsExceeded')
        self.assertEqual(report['independent']['status'], 'PASS')
        self.assertFalse(report['agent_submitted'])
        self.assertEqual(report['resources']['actual_cost'], None)
        self.assertEqual((self.run/'result.json').read_bytes(), self.original)
        self.assertEqual((evaluation/'result.json').read_bytes(), old_evaluation)
        self.assertIn('not automatically assessed', (out/'delivery-summary.md').read_text('utf-8'))

    def test_another_run_cannot_lend_its_validation(self):
        evaluation = self.evaluation()
        value = read_json(evaluation/'result.json')
        value['source_solve_run'] = str(self.root/'different-run')
        save_json(evaluation/'result.json', value)
        with self.assertRaisesRegex(ValueError, 'binding differs'):
            write_agent_delivery(self.run, self.root/'wrong', evaluation=evaluation)
        self.assertFalse((self.root/'wrong').exists())

    def test_later_edit_cannot_reuse_old_pass(self):
        evaluation = self.evaluation()
        (self.run/'workspace/source.ets').write_text('changed after checks', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Candidate differs'):
            write_agent_delivery(self.run, self.root/'stale', evaluation=evaluation)
        self.assertFalse((self.root/'stale').exists())

    def test_inconsistent_stage_result_cannot_claim_pass(self):
        evaluation = self.evaluation()
        value = read_json(evaluation/'result.json')
        value['checks']['stages']['functional']['status'] = 'FAIL'
        save_json(evaluation/'result.json', value)
        with self.assertRaisesRegex(ValueError, 'stages do not support'):
            write_agent_delivery(self.run, self.root/'inconsistent', evaluation=evaluation)

    def test_submitted_does_not_certify_business_success(self):
        run = self.root/'submitted'
        solve(self.repo, 'Local fixture', run, responses=[{'action': 'finish', 'summary': 'Unchecked',
                                                       'unverified': ['behavior']}])
        report = write_agent_delivery(run, self.root/'submitted-summary')
        self.assertTrue(report['agent_submitted'])
        self.assertEqual(report['independent']['status'], 'NOT_RUN')
        self.assertEqual(report['unverified_declared_by_model'], ['behavior'])


if __name__ == '__main__':
    unittest.main()
