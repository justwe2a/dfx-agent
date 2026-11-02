import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.comparison import paired_summary
from dfx_agent.workspace import read_json, save_json
from dfx_agent.model import DeepSeekModel, ModelError


def batch_rows():
    return {'batch_id':'test','status':'RUNNING','execution_commit':'frozen','trials':[
        {'index':1,'pair':1,'repeat':1,'case':'Calendar1','arm':'B0','outcome':'MODEL_FAILURE','measurement':{'requests':1,'tokens':10}},
        {'index':2,'pair':1,'repeat':1,'case':'Calendar1','arm':'B1','outcome':'NOT_RUN'}]}


class ComparisonTests(unittest.TestCase):
    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'TEST_SECRET'})
    @patch('urllib.request.build_opener')
    def test_actual_client_retains_invalid_content_locally_without_feedback_or_extra_retries(self, opener):
        spec=importlib.util.spec_from_file_location('comparison_script',Path(__file__).resolve().parents[1]/'scripts/run-comparison.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        content='未闭合的 JSON\r\n{"action":"propose", LOCAL_ONLY_SENTINEL'
        for mode,finish in [('single_patch','stop'),('workflow','stop'),('workflow','length')]:
            with self.subTest(mode=mode,finish=finish), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'source';source.mkdir();(source/'main.js').write_text('return 0;')
                log=root/'crash.log';log.write_text('Error name:TypeError\nStacktrace:\n at x module (main.js:1:1)\n')
                config={'schema_version':2,'source_version':'v1','case_id':'synthetic/test','evaluation_scope':'synthetic',
                        'source_root':str(source),'logs':[str(log)],'visible_files':['main.js'],'editable_files':['main.js'],
                        'context_review':'APPROVED','workflow_mode':mode,'max_attempts':1,'max_model_calls':1 if mode=='single_patch' else 2}
                path=root/'case.json';save_json(path,config);run=script.runner.prepare(path,root/'runs')
                def envelope(text,stop):
                    return {'id':'test-id','model':'deepseek-flash','usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},
                            'choices':[{'finish_reason':stop,'message':{'content':text,'reasoning_content':'DO_NOT_SAVE'}}]}
                opener.reset_mock()
                opener.return_value.open.side_effect=[
                    io.BytesIO(json.dumps(envelope(content,finish)).encode()),
                    io.BytesIO(json.dumps(envelope('{"action":"invalid"}','stop')).encode())]
                state=script.drive_comparison(run,DeepSeekModel('deepseek-flash', thinking='disabled'))
                self.assertEqual((run/'model/001/rejected-content.txt').read_bytes(),content.encode('utf-8'))
                error=read_json(run/'model/001/error.json')
                self.assertEqual(error['response_content_file'],'rejected-content.txt')
                expected=2 if mode=='workflow' and finish=='stop' else 1
                self.assertEqual(opener.return_value.open.call_count,expected)
                self.assertEqual(state['paid_api_calls'],expected)
                self.assertEqual(len(state['attempts']),0)
                self.assertFalse((run/'attempts').exists())
                self.assertEqual(state['status'],'WAITING_MODEL' if finish=='length' else 'EXHAUSTED')
                for request_call in opener.return_value.open.call_args_list:
                    self.assertNotIn('LOCAL_ONLY_SENTINEL',request_call.args[0].data.decode())
                self.assertNotIn('LOCAL_ONLY_SENTINEL',json.dumps(state))
                saved=''.join(p.read_text(encoding='utf-8') for p in run.rglob('*.json'))
                self.assertNotIn('TEST_SECRET',saved)
                self.assertNotIn('DO_NOT_SAVE',saved)

    def test_actual_driver_counts_complete_format_error_but_never_retries_transport(self):
        spec=importlib.util.spec_from_file_location('comparison_script',Path(__file__).resolve().parents[1]/'scripts/run-comparison.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        for mode,transport in [('single_patch',False),('workflow',False),('workflow',True)]:
            with self.subTest(mode=mode,transport=transport), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'source';source.mkdir();(source/'main.js').write_text('return 0;')
                log=root/'crash.log';log.write_text('Error name:TypeError\nStacktrace:\n at x module (main.js:1:1)\n')
                config={'schema_version':2,'source_version':'v1','case_id':'synthetic/test','evaluation_scope':'synthetic',
                        'source_root':str(source),'logs':[str(log)],'visible_files':['main.js'],'editable_files':['main.js'],
                        'context_review':'APPROVED','workflow_mode':mode,'max_attempts':1,'max_model_calls':1 if mode=='single_patch' else 2}
                path=root/'case.json';save_json(path,config);run=script.runner.prepare(path,root/'runs')
                class Provider:
                    kind='deepseek'
                    def __init__(self):self.requests=[]
                    def query(self,request):
                        self.requests.append(request)
                        if len(self.requests)==1:
                            meta={'submitted':True,'requested_model':'deepseek-flash','returned_model':'deepseek-flash',
                                  'finish_reason':'stop','error_type':'JSONDecodeError'}
                            if transport:meta={'submitted':True,'error_type':'TimeoutError'}
                            raise ModelError('test failure',meta)
                        return {'action':'invalid','_provider':{'submitted':True}}
                model=Provider();state=script.drive_comparison(run,model)
                self.assertEqual(len(model.requests),1 if transport or mode=='single_patch' else 2)
                self.assertEqual(state['paid_api_calls'],len(model.requests))
                self.assertEqual(state['status'],'WAITING_MODEL' if transport else 'EXHAUSTED')
                self.assertTrue((run/'model/001/error.json').exists())
                if transport:
                    self.assertFalse((run/'model/001/rejected-content.txt').exists())
                self.assertEqual(len(state['attempts']),0)
                if len(model.requests)==2:
                    self.assertEqual(model.requests[1]['feedback'][0]['error'],'RESPONSE_REJECTED')

    def test_explicit_resume_preserves_blocked_prefix_and_only_runs_remaining_trial(self):
        spec=importlib.util.spec_from_file_location('comparison_script',Path(__file__).resolve().parents[1]/'scripts/run-comparison.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);old=folder/'old';run=folder/'new';base=folder/'base'
            b=batch_rows();b.update(status='PAUSED',baselines={'Calendar1':str(base)},plan={
                'model':{'name':'deepseek-flash','max_output_tokens':4096,'thinking':'disabled'},'arms':{
                    'B1':{'mode':'workflow','max_model_calls':12,'max_attempts':3}}})
            b['trials'][0].update(outcome='BLOCKED',run_path=str(old),reason='standby')
            prefix=copy.deepcopy(b['trials'][0]);save_json(folder/'batch.json',b)
            saved={'paid_api_calls':1,'attempts':[{'number':1,'revalidations':[{
                'status':'PASS','directory':'attempts/001/revalidations/001','validation':{'all_passed':True}}]}]}
            save_json(old/'state.json',saved)
            approval={'approved':True,'batch_id':'test','original_execution_commit':'frozen','resume_commit':'new',
                      'preserve_through':1,'max_new_requests':12,'auxiliary_trial':1,'auxiliary_attempt':1}
            path=folder/'approval.json';save_json(path,approval)
            with patch.object(script,'commit',return_value='new'):
                for changed in ({'approved':False},{'max_new_requests':13},{'resume_commit':'other'}):
                    save_json(path,{**approval,**changed})
                    with self.assertRaises(ValueError):script.resume_start(folder,copy.deepcopy(b),path)
                save_json(path,approval);saved['attempts'][0]['revalidations'][0]['status']='NOT_PASSED'
                save_json(old/'state.json',saved)
                with self.assertRaises(ValueError):script.resume_start(folder,copy.deepcopy(b),path)
            saved['attempts'][0]['revalidations'][0]['status']='PASS';save_json(old/'state.json',saved)
            save_json(base/'case.json',{});save_json(run/'case.json',{});save_json(run/'state.json',{})
            save_json(folder/'initial-evidence/Calendar1.json',{'case_id':'same'})
            with (patch.object(script,'commit',return_value='new'),patch.object(script,'DeepSeekModel'),
                  patch.object(script.runner,'prepare',return_value=run) as prepare,
                  patch.object(script.runner,'request_for',return_value={'instructions':'workflow','case_id':'same'}),
                  patch.object(script,'restore_original',return_value=True),
                  patch.object(script.runner,'drive',return_value={'status':'VALIDATED','paid_api_calls':1}) as drive,
                  patch.object(script,'measurement',return_value={'requests':1,'tokens':20})):
                self.assertEqual(script.run_batch(folder,path),0)
                self.assertEqual(prepare.call_count,1);self.assertEqual(drive.call_count,1)
            result=read_json(folder/'batch.json')
            self.assertEqual(result['trials'][0],prefix)
            self.assertEqual(result['status'],'FINISHED_WITH_BLOCKS')
            self.assertEqual(read_json(folder/'continuation/batch-before.json'),b)
            self.assertEqual(read_json(run/'comparison-binding.json')['execution_commit'],'new')
            self.assertEqual(read_json(folder/'comparison.json')['pair_counts'],{'INCOMPLETE':1})
            b['continuation']=result['continuation'];b['continuations']=result['continuations']
            with patch.object(script,'commit',return_value='new'):
                script.resume_start(folder,copy.deepcopy(b),path)
            self.assertEqual(read_json(folder/'continuation-002/batch-before.json'),b)
            self.assertNotIn('continuation',read_json(folder/'continuation/batch-before.json'))

    def test_second_environment_resume_keeps_both_blocked_rows_and_prior_recheck(self):
        spec=importlib.util.spec_from_file_location('comparison_script',Path(__file__).resolve().parents[1]/'scripts/run-comparison.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);base=folder/'base';new=folder/'new'
            b=batch_rows();b.update(status='PAUSED',baselines={'Calendar1':str(base)},plan={
                'model':{'name':'deepseek-flash','max_output_tokens':4096,'thinking':'disabled'},'arms':{
                    'B0':{'mode':'single_patch','max_model_calls':1,'max_attempts':1},
                    'B1':{'mode':'workflow','max_model_calls':12,'max_attempts':3}}},
                continuations=[{'auxiliary_trial':1,'auxiliary_attempt':1,'resume_commit':'previous',
                                'preserve_through':1,'max_new_requests':13,'auxiliary_recheck':'old proof'}])
            saved={'paid_api_calls':1,'attempts':[{'number':1,'revalidations':[{
                'status':'PASS','directory':'attempts/001/revalidations/001','validation':{'all_passed':True}}]}]}
            for row in b['trials']:
                row.update(outcome='BLOCKED',run_path=str(folder/str(row['index'])),measurement={'requests':1,'tokens':10})
                save_json(Path(row['run_path'])/'state.json',saved)
            b['trials'].append({'index':3,'pair':2,'repeat':2,'case':'Calendar1','arm':'B0',
                                'outcome':'MODEL_FAILURE','measurement':{'requests':1,'tokens':10}})
            prefix=copy.deepcopy(b['trials'])
            b['trials'].append({'index':4,'pair':2,'repeat':2,'case':'Calendar1','arm':'B1','outcome':'NOT_RUN'})
            approval={'approved':True,'batch_id':'test','original_execution_commit':'frozen','resume_commit':'new',
                      'preserve_through':3,'max_new_requests':12,'auxiliary_trial':2,'auxiliary_attempt':1}
            path=folder/'approval.json';save_json(path,approval)
            with patch.object(script,'commit',return_value='new'):
                missing=copy.deepcopy(b);missing['continuations']=[]
                with self.assertRaises(ValueError):script.resume_start(folder,missing,path)
                for changed in ({'paid_api_calls':2},{'attempts':[{'number':1,'revalidations':[]}]}):
                    save_json(folder/'1/state.json',{**saved,**changed})
                    with self.assertRaises(ValueError):script.resume_start(folder,copy.deepcopy(b),path)
                save_json(folder/'1/state.json',saved)
            save_json(folder/'batch.json',b);save_json(base/'case.json',{})
            save_json(new/'case.json',{});save_json(new/'state.json',{})
            save_json(folder/'initial-evidence/Calendar1.json',{'case_id':'same'})
            with (patch.object(script,'commit',return_value='new'),patch.object(script,'DeepSeekModel'),
                  patch.object(script.runner,'prepare',return_value=new) as prepare,
                  patch.object(script.runner,'request_for',return_value={'instructions':'single_patch','case_id':'same'}),
                  patch.object(script,'restore_original',return_value=True),
                  patch.object(script.runner,'drive',return_value={'status':'VALIDATED','paid_api_calls':1}) as drive,
                  patch.object(script,'measurement',return_value={'requests':1,'tokens':20})):
                self.assertEqual(script.run_batch(folder,path),0)
            result=read_json(folder/'batch.json')
            self.assertEqual(result['trials'][:3],prefix)
            self.assertEqual(result['status'],'FINISHED_WITH_BLOCKS')
            self.assertEqual(prepare.call_count,1);self.assertEqual(drive.call_count,1)
            self.assertEqual(result['continuations'][0],b['continuations'][0])
            self.assertEqual(result['continuation']['auxiliary_recheck'],str(folder/'2/attempts/001/revalidations/001'))
            self.assertEqual(read_json(folder/'continuation-002/batch-before.json'),b)

    def test_real_trial_preparation_copies_frozen_baseline_without_storage_overlap(self):
        spec=importlib.util.spec_from_file_location('comparison_script',Path(__file__).resolve().parents[1]/'scripts/run-comparison.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        for first_arm in ('B0', 'B1'):
            with self.subTest(first_arm=first_arm), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);folder=root/'reports/batch';source=root/'source';source.mkdir()
                (source/'main.js').write_text('function calculate() { return 0; }\n',encoding='utf-8')
                log=root/'crash.log'
                log.write_text('Error name:TypeError\nError message:bad\nStacktrace:\n at calculate module (main.js:1:2)\n',encoding='utf-8')
                config={'schema_version':2,'source_version':'test-v1','case_id':'synthetic/demo',
                        'source_root':str(source),'logs':[str(log)],'visible_files':['main.js'],
                        'editable_files':['main.js'],'context_review':'APPROVED'}
                path=root/'case.json';save_json(path,config)
                base=script.runner.prepare(path,root/'runs/workflow')
                save_json(base/'baseline-proof.json',{'case_id':'synthetic/demo','snapshot_id':'test-v1','status':'REPRODUCED'})
                initial=script.runner.request_for(base,read_json(base/'case.json'),read_json(base/'state.json'))
                initial.pop('instructions');save_json(folder/'initial-evidence/Calendar1.json',initial)
                b=batch_rows()
                for row in b['trials']:
                    row['outcome']='NOT_RUN';row.pop('measurement',None)
                if first_arm=='B1': b['trials'].reverse()
                b.update(status='READY',baselines={'Calendar1':str(base)},plan={
                    'model':{'name':'deepseek-flash','max_output_tokens':4096,'thinking':'disabled'},'arms':{
                        'B0':{'mode':'single_patch','max_model_calls':1,'max_attempts':1},
                        'B1':{'mode':'workflow','max_model_calls':12,'max_attempts':3}}})
                save_json(folder/'batch.json',b)
                with (patch.object(script,'REPO',root),patch.object(script,'commit',return_value='frozen'),
                      patch.object(script,'DeepSeekModel') as model,
                      patch.object(script,'restore_original',return_value=False),
                      patch.object(script,'measurement',return_value={'requests':0,'tokens':0})):
                    self.assertEqual(script.run_batch(folder),3)
                    model.return_value.query.assert_not_called()
                    model.assert_called_once_with('deepseek-flash',4096,thinking='disabled',reasoning_effort='high')
                result=read_json(folder/'batch.json');trial=Path(result['trials'][0]['run_path'])
                self.assertEqual(trial.parent,root/'runs/comparison/batch')
                self.assertEqual((trial/'baseline/main.js').read_bytes(),(base/'baseline/main.js').read_bytes())
                self.assertEqual(read_json(trial/'state.json')['baseline_status'],'REPRODUCED')
                self.assertEqual(read_json(trial/'state.json')['paid_api_calls'],0)
                self.assertTrue(read_json(trial/'comparison-binding.json')['initial_payload_equal'])
                self.assertEqual([r['outcome'] for r in result['trials']],['BLOCKED','NOT_RUN'])

    def test_pending_arm_never_becomes_a_win_or_disappears_from_denominator(self):
        b=batch_rows();s=paired_summary(b)
        self.assertEqual(s['pairs'][0]['outcome'],'INCOMPLETE')
        self.assertEqual(s['arms']['B1']['registered'],1)
        self.assertEqual(s['arms']['B1']['completed'],0)
        b['trials'][1].update(outcome='PASS',measurement={'requests':3,'tokens':50,'first_candidate_initial_passed':True,'first_response_passed':False})
        s=paired_summary(b)
        self.assertEqual(s['pairs'][0]['outcome'],'B1_ONLY')
        self.assertEqual(s['arms']['B1']['first_response_passed'],0)
        self.assertEqual(s['arms']['B1']['first_candidate_initial_passed'],1)

    def test_missing_usage_stays_unknown_and_both_fail_is_preserved(self):
        b=batch_rows();b['trials'][1].update(outcome='MODEL_FAILURE',measurement={'requests':1,'tokens':None})
        s=paired_summary(b)
        self.assertEqual(s['pairs'][0]['outcome'],'BOTH_FAIL')
        self.assertIsNone(s['arms']['B1']['tokens'])
        self.assertEqual(s['arms']['B0']['requests'],1)

    def test_blocked_precondition_stops_batch_before_any_model_call(self):
        spec=importlib.util.spec_from_file_location('comparison_script',Path(__file__).resolve().parents[1]/'scripts/run-comparison.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);run=folder/'run';run.mkdir();base=folder/'base';base.mkdir()
            b=batch_rows();b.update(status='READY',baselines={'Calendar1':str(base)},plan={'model':{'name':'deepseek-flash','max_output_tokens':4096,'thinking':'disabled'},'arms':{'B0':{'mode':'single_patch','max_model_calls':1,'max_attempts':1}}})
            b['trials'][0]['outcome']='NOT_RUN';b['trials'][0].pop('measurement')
            save_json(folder/'batch.json',b);save_json(base/'case.json',{})
            save_json(run/'case.json',{});save_json(run/'state.json',{'status':'PREPARED','paid_api_calls':0})
            save_json(folder/'initial-evidence/Calendar1.json',{'case_id':'same'})
            with (patch.object(script,'commit',return_value='frozen'),
                  patch.object(script,'DeepSeekModel') as model,
                  patch.object(script.runner,'prepare',return_value=run) as prepare,
                  patch.object(script.runner,'request_for',return_value={'instructions':'mode','case_id':'same'}),
                  patch.object(script.runner,'persist'),
                  patch.object(script.runner,'drive') as drive,
                  patch.object(script,'restore_original',return_value=False),
                  patch.object(script,'measurement',return_value={'requests':0,'tokens':0})):
                self.assertEqual(script.run_batch(folder),3)
                drive.assert_not_called();model.return_value.query.assert_not_called()
                self.assertEqual(prepare.call_count,1)
            result=read_json(folder/'batch.json')
            self.assertEqual(result['status'],'PAUSED')
            self.assertEqual([r['outcome'] for r in result['trials']],['BLOCKED','NOT_RUN'])


if __name__=='__main__':unittest.main()
