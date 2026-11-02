from pathlib import Path
import tempfile
import unittest

from dfx_agent.run_report import summarize_runs
from dfx_agent.workspace import save_json


class RunReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def write(self,name,**changes):
        value={'case_id':'case','evaluation_scope':'competition_development','status':'VALIDATED',
            'baseline_status':'REPRODUCED','repair_validated':True,'paid_api_calls':1,'model_kind':'deepseek',
            'attempts':[{'validation':{'stages':{s:{'status':'PASS'} for s in
                ('build','deploy','replay','functional','regression')}}}]}
        value.update(changes);save_json(self.root/name/'result.json',value)

    def test_fixtures_synthetic_and_incomplete_results_cannot_inflate_real_repairs(self):
        self.write('run-real')
        self.write('run-fixture',fixture_assisted=True)
        self.write('run-synthetic',evaluation_scope='synthetic')
        self.write('run-missing',attempts=[])
        report=summarize_runs(self.root)
        self.assertEqual(report['verified_competition_development_repairs'],1)
        self.assertEqual(len(report['issues']),3)
        self.assertEqual(report['group_counts']['offline_fixture'],1)

    def test_missing_usage_is_unknown_and_failed_run_is_retained(self):
        self.write('run-failed',status='EXHAUSTED',repair_validated=False,paid_api_calls=1,
                   provider_calls=[{'submitted':True,'usage':None}])
        report=summarize_runs(self.root)
        self.assertIsNone(report['provider_reported_tokens']['total'])
        self.assertEqual(report['status_counts']['EXHAUSTED'],1)

    def test_known_usage_sums_and_preflight_rejections_are_not_billable_attempts(self):
        self.write('run-usage',paid_api_calls=1,provider_calls=[{'submitted':False,'usage':None},
                   {'submitted':True,'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}])
        report=summarize_runs(self.root)
        self.assertEqual(report['provider_reported_tokens']['total'],12)
        self.assertIsNone(report['actual_cost'])

    def test_missing_result_remains_visible_as_issue(self):
        (self.root/'run-interrupted').mkdir()
        report=summarize_runs(self.root)
        self.assertEqual(len(report['issues']),1)
        self.assertEqual(report['runs'],[])

    def test_success_flag_without_live_model_is_not_counted(self):
        self.write('run-no-api',paid_api_calls=0,model_kind='unconfigured')
        report=summarize_runs(self.root)
        self.assertEqual(report['verified_competition_development_repairs'],0)
        self.assertEqual(len(report['issues']),1)

    def test_revalidated_earlier_candidate_is_counted_without_erasing_later_failure(self):
        checks = {'stages': {s: {'status':'PASS'} for s in ('build','deploy','replay','functional','regression')}}
        attempts = [{'number':2,'validation':{'stages':{'functional':{'status':'FAIL'}}},
                     'revalidations':[{'validation':checks}]},
                    {'number':3,'validation':{'stages':{'build':{'status':'FAIL'}}}}]
        self.write('run-rechecked',attempts=attempts,validated_attempt=2)
        self.assertEqual(summarize_runs(self.root)['verified_competition_development_repairs'],1)
        self.write('run-rechecked',attempts=attempts,validated_attempt=99)
        self.assertEqual(summarize_runs(self.root)['verified_competition_development_repairs'],0)


if __name__=='__main__':unittest.main()
