import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.harmony_runtime import (Blocked, CheckFailed, Device, assert_interval,
    evaluate_replay, fault_matches, verify_deployment)
from dfx_agent.runner import prepare, run_baseline, verify_checkpoint
from dfx_agent.workspace import read_json, save_json


def node(text='', children=(), kind='Text'):
    return {'attributes': {'text': text, 'type': kind, 'visible': 'true', 'bounds': '[0,0][20,20]'},
            'children': list(children)}


def form(start, end, result):
    return node(children=[node(children=[node('日期1'), node(start)]),
                          node(children=[node('日期2'), node(end)]), node(result)])


class RuntimeTests(unittest.TestCase):
    def test_valid_dates_and_reversed_order_use_independent_oracle(self):
        for start, end, days in [('2024年02月28日','2024年02月28日',0),
                                 ('2024年02月28日','2024年02月29日',1),
                                 ('2024年02月28日','2024年03月01日',2),
                                 ('2024年03月01日','2024年02月28日',2)]:
            with self.subTest(start=start,end=end):
                self.assertEqual(assert_interval(form(start,end,f'间隔{days}天'))['expected_days'],days)

    def test_constant_result_and_swallowed_output_fail(self):
        for output in ['间隔0天', '间隔-2天', '', '计算失败']:
            with self.subTest(output=output), self.assertRaises(CheckFailed):
                assert_interval(form('2024年02月28日','2024年03月01日',output))

    def test_wrong_input_is_harness_block_not_application_failure(self):
        with self.assertRaises(Blocked):
            assert_interval(form('2024年02月28日','2024年03月01日','间隔2天'), '2024-02-29','2024-03-01')

    def test_duplicate_result_or_date_is_not_accepted(self):
        tree=form('2024年02月28日','2024年03月01日','间隔2天')
        tree['children'].append(node('间隔3天'))
        with self.assertRaises(CheckFailed): assert_interval(tree)
        tree['children'][0]['children'].append(node('2024年02月29日'))
        with self.assertRaises(Blocked): assert_interval(tree)

    def test_original_crash_passes_baseline_but_fails_repair_replay(self):
        evidence={'pid':'42','pid_after':'','page_reached':False,
                  'faults':[{'target_matches':True,'pid_matches':True}]}
        self.assertEqual(evaluate_replay(evidence,True),'REPRODUCED')
        with self.assertRaises(CheckFailed): evaluate_replay(evidence)

    def test_no_crash_is_not_enough_and_other_crash_fails(self):
        evidence={'pid':'42','pid_after':'42','page_reached':False,'faults':[]}
        with self.assertRaises(Blocked): evaluate_replay(evidence)
        with self.assertRaises(Blocked): evaluate_replay(evidence,True)
        evidence.update(page_reached=True,faults=[{'pid_matches':True,'target_matches':False}])
        with self.assertRaises(CheckFailed): evaluate_replay(evidence)

    def test_restart_without_crash_log_is_failure(self):
        with self.assertRaises(CheckFailed):
            evaluate_replay({'pid':'42','pid_after':'43','page_reached':True,'faults':[]})

    def test_fault_signature_requires_exact_pid_and_bundle(self):
        log='Pid:42\nModule name:app.demo\nError message:TARGET\nframe'
        self.assertTrue(fault_matches(log,'app.demo','42',['TARGET','frame']))
        self.assertFalse(fault_matches(log,'app.demo','4',['TARGET']))
        self.assertFalse(fault_matches(log,'app','42',['TARGET']))
        self.assertFalse(fault_matches(log,'app.demo','42',['missing']))

    def test_stale_install_or_missing_artifact_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact=Path(tmp)/'a.hap';artifact.write_bytes(b'current')
            binding=dict(run_id='r',attempt_id='1',snapshot_id='s',patch_id='p',artifact_id='build-1')
            receipt={**binding,'stage':'deploy','status':'PASS','details':{'device':'d'}}
            verify_deployment(receipt,binding,'d',artifact)
            for key,value in [('run_id','old'),('attempt_id','0'),('stage','replay'),('status','FAIL')]:
                changed={**receipt,key:value}
                with self.subTest(key=key),self.assertRaises(Blocked):verify_deployment(changed,binding,'d',artifact)
            with self.assertRaises(Blocked):verify_deployment(receipt,binding,'other',artifact)
            artifact.unlink()
            with self.assertRaises(Blocked):verify_deployment(receipt,binding,'d',artifact)

    def test_picker_requires_confirmed_observed_target(self):
        device=object.__new__(Device)
        device.layout=lambda label: node('2024年03月01日',kind='DatePicker')
        self.assertEqual(device.choose_date(dt.date(2024,3,1))['attributes']['text'],'2024年03月01日')
        # No adjacent wheel item is exposed: never fabricate successful input.
        with self.assertRaises(Blocked):device.choose_date(dt.date(2024,3,2))

    def test_unresponsive_picker_is_bounded(self):
        device=object.__new__(Device)
        device.layout=lambda label: node('2024年03月01日',[node('2日')],kind='DatePicker')
        clicks=[];device.click_node=lambda n: clicks.append(n)
        with self.assertRaises(Blocked):device.choose_date(dt.date(2024,3,2))
        self.assertEqual(len(clicks),3)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);src=root/'src';src.mkdir()
        (src/'a.js').write_text('throw Error("test");')
        log=root/'error.log';log.write_text('Error name:Error\nStacktrace:\n at f (a.js:1:1)')
        case=root/'case.json'
        save_json(case,{'schema_version':2, 'source_version':'test-source-v1','case_id':'fixture','source_root':str(src),
            'logs':[str(log)],'visible_files':['a.js'],'editable_files':['a.js'],
            'context_review':'PENDING','baseline_validation':{s:{} for s in ['build','deploy','replay']}})
        self.run=prepare(case,root/'runs')

    def checks(self, reproduced=True):
        return {'artifact_id':'fake-unit-fixture','stages':{
            'build':{'status':'PASS'},'deploy':{'status':'PASS'},
            'replay':{'status':'PASS','details':{'case_id':'fixture',
                'baseline_status':'REPRODUCED' if reproduced else 'NOT_REPRODUCED'}}}}

    def test_baseline_needs_no_model_approval_and_pins_proof(self):
        with patch('dfx_agent.runner.validate',return_value=self.checks()):
            state=run_baseline(self.run)
        self.assertEqual(state['baseline_status'],'REPRODUCED')
        self.assertEqual(state['status'],'PREPARED')
        self.assertEqual(state['paid_api_calls'],0)
        self.assertEqual(read_json(self.run/'case.json')['context_review'],'PENDING')
        verify_checkpoint(self.run,state)
        (self.run/'evidence.json').unlink()
        with self.assertRaisesRegex(ValueError,'Missing run record'):verify_checkpoint(self.run,state)

    def test_all_commands_pass_without_target_fault_does_not_prove_baseline(self):
        with patch('dfx_agent.runner.validate',return_value=self.checks(False)):
            state=run_baseline(self.run)
        self.assertEqual(state['status'],'BLOCKED')
        self.assertEqual(state['baseline_status'],'INCONCLUSIVE')

    def test_baseline_from_different_case_is_rejected(self):
        checks=self.checks();checks['stages']['replay']['details']['case_id']='other'
        with patch('dfx_agent.runner.validate',return_value=checks):
            self.assertEqual(run_baseline(self.run)['status'],'BLOCKED')

    def test_baseline_adapter_error_is_recorded(self):
        with patch('dfx_agent.runner.validate',side_effect=OSError('missing tool')):
            state=run_baseline(self.run)
        self.assertEqual(state['status'],'BLOCKED')
        self.assertEqual(state['baseline_status'],'INCONCLUSIVE')
        self.assertIn('missing tool',state['reason'])


if __name__=='__main__':
    unittest.main()
