import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.context_review import export_review, verify_approval
from dfx_agent.model import INSTRUCTIONS
from dfx_agent.outbound import project_request
from dfx_agent.runner import prepare, drive, request_for
from dfx_agent.workspace import load_case, read_json, save_json


class ContextReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'src';self.source.mkdir()
        (self.source/'a.js').write_text('function f() { throw Error("bad"); }\n',encoding='utf-8')
        (self.source/'private.txt').write_text('LOCAL_ONLY_SECRET',encoding='utf-8')
        log=self.root/'fault.log'
        log.write_text('Module name:LOCAL_ONLY_BUNDLE\nTimestamp:LOCAL_ONLY_TIME\nError name:TypeError\n'
                       'Error message:bad\nStacktrace:\n at f (a.js:1:2)\nHiLog:\nLOCAL_ONLY_HILOG',encoding='utf-8')
        self.case=self.root/'case.json'
        self.config={'schema_version':2, 'source_version':'test-source-v1','case_id':'demo','source_root':str(self.source),'logs':[str(log)],
                     'visible_files':['a.js'],'editable_files':['a.js'],'context_review':'PENDING',
                     'evaluation_scope':'competition_development','live_model_enabled':False}
        save_json(self.case,self.config)
        self.run=prepare(self.case,self.root/'runs')

    def request(self, feedback=()):
        state=read_json(self.run/'state.json');state['feedback']=list(feedback)
        return request_for(self.run,read_json(self.run/'case.json'),state)

    def approve(self):
        cfg=load_case(self.case); snapshot=read_json(self.run/'snapshot.json');evidence=read_json(self.run/'evidence.json')
        cfg['approved_context']={'source_version':cfg['source_version'],'visible_files':['a.js'],
                                 'editable_files':['a.js'],'log_names':['fault.log']}
        return cfg,snapshot,evidence

    def test_preview_omits_raw_log_metadata_and_private_files(self):
        request=self.request();raw=json.dumps(request)
        for secret in ['LOCAL_ONLY_BUNDLE','LOCAL_ONLY_TIME','LOCAL_ONLY_HILOG','LOCAL_ONLY_SECRET']:
            self.assertNotIn(secret,raw)
        self.assertNotIn(str(self.root),raw)
        self.assertIn('throw Error',raw)
        self.assertEqual(request['evidence']['crashes'][0]['error_type'],'TypeError')

    def test_validation_metadata_and_log_tail_do_not_reach_model(self):
        feedback={'stage':'build','result':{'status':'FAIL','reason':'LOCAL_SECRET',
            'command':{'output_tail':'LOCAL_SECRET','argv':['C:/private/tool.exe']},
            'details':{'device':'LOCAL_SECRET','reason':'LOCAL_SECRET','screenshot':'LOCAL_SECRET'}},
            'previous_diff':'--- a/a.js\n+++ b/a.js\n@@\n-approved source\n+candidate'}
        result=self.request([feedback])
        self.assertNotIn('LOCAL_SECRET',json.dumps(result))
        self.assertNotIn('C:/private',json.dumps(result))
        self.assertEqual(result['feedback'][0]['result']['failure_kind'],'BUILD_NOT_PASSED')
        self.assertIn('candidate',result['feedback'][0]['previous_diff'])

    def test_numeric_business_failure_is_preserved_without_full_report(self):
        item={'stage':'functional','result':{'status':'FAIL','details':{
            'reason':'Expected 间隔2天, observed 间隔0天','device':'LOCAL_SECRET'}}}
        result=self.request([item])['feedback'][0]['result']
        self.assertEqual(result['expected_days'],2);self.assertEqual(result['observed_days'],0)
        self.assertNotIn('LOCAL_SECRET',str(result))

    def test_build_diagnostics_are_opt_in_and_exclude_unapproved_files_and_body(self):
        tail = ('\x1b[31m10605038 ArkTS Compiler Error\n'
                'Error Message: LOCAL_SECRET (arkts-no-untyped-obj-literals) At File: C:/host/a.js:163:40\n'
                '10605008 ArkTS Compiler Error\n'
                'Error Message: PRIVATE_BODY (arkts-no-any-unknown) At File: C:/host/private.ets:1:2\n')
        item = {'stage': 'build', 'result': {'status': 'FAIL', 'command': {'output_tail': tail}}}
        self.assertNotIn('diagnostics', self.request([item])['feedback'][0]['result'])
        config = read_json(self.run/'case.json');config['include_build_diagnostics'] = True
        save_json(self.run/'case.json', config)
        result = self.request([item])['feedback'][0]['result']
        self.assertEqual(result['diagnostics'], [{'path': 'a.js', 'line': 163, 'column': 40,
            'compiler_code': '10605038', 'rule': 'arkts-no-untyped-obj-literals'}])
        for excluded in ['LOCAL_SECRET', 'PRIVATE_BODY', 'C:/host', 'private.ets']:
            self.assertNotIn(excluded, json.dumps(result))

    def test_missing_build_diagnostics_are_explicit(self):
        config = read_json(self.run/'case.json');config['include_build_diagnostics'] = True
        save_json(self.run/'case.json', config)
        item = {'stage':'build','result':{'status':'FAIL','command':{'output_tail':'unknown compiler error'}}}
        result = self.request([item])['feedback'][0]['result']
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual(result['diagnostics_status'], 'NO_MATCHING_APPROVED_DIAGNOSTICS')

    def test_diagnostic_expansion_requires_separate_approval(self):
        config, _, _ = self.approve();config['include_build_diagnostics'] = True
        with self.assertRaises(ValueError): verify_approval(config)
        config['approved_context']['include_build_diagnostics'] = True
        verify_approval(config)

    def test_rejected_response_and_arbitrary_tool_fields_not_echoed(self):
        result=self.request([{'stage':'proposal_or_retrieval','error':'LOCAL_SECRET',
                              'rejected_response':{'instructions':'LOCAL_SECRET'}}])
        self.assertNotIn('LOCAL_SECRET',json.dumps(result))
        self.assertEqual(result['feedback'][0]['error'],'RESPONSE_REJECTED')

    def test_unapproved_snippet_is_rejected(self):
        request=self.request();request['retrieved_snippets']=[{'path':'private.txt'}]
        with self.assertRaises(ValueError):project_request(request)

    def test_read_and_search_only_expose_allowed_fields(self):
        snippet=self.request()['evidence']['snippets'][0]
        feedback=[{'tool':'read','result':{**snippet,'extra':'LOCAL_SECRET'}},
            {'tool':'search','result':{'matches':[{'path':'a.js','line':1,'text':'approved','extra':'LOCAL_SECRET'},
                                                {'path':'private.txt','line':1,'text':'LOCAL_SECRET'}],
                                     'truncated':False,'extra':'LOCAL_SECRET'}}]
        request=self.request(feedback)
        self.assertNotIn('LOCAL_SECRET',json.dumps(request))
        self.assertEqual(len(request['feedback'][1]['result']['matches']),1)

    def test_export_is_offline_and_never_approves_or_overwrites(self):
        with patch('urllib.request.build_opener',side_effect=AssertionError('No network allowed')):
            result=export_review(self.case,self.root/'review')
        self.assertEqual(result['paid_model_calls'],0)
        self.assertEqual(read_json(self.case)['context_review'],'PENDING')
        manifest=read_json(self.root/'review/manifest.json')
        self.assertEqual(manifest['source_files'][0]['path'],'a.js')
        self.assertIn('private.txt',manifest['excluded_source_snapshot_files'])
        with self.assertRaises(ValueError):export_review(self.case,self.root/'review')
        with self.assertRaises(ValueError):export_review(self.case,self.source/'review')

    def test_approval_records_scope_and_explicit_version(self):
        cfg,snapshot,evidence=self.approve();verify_approval(cfg)
        for key,value in [('source_version','v2'),('visible_files',['private.txt']),
                          ('editable_files',[]),('log_names',['other.log'])]:
            changed=copy.deepcopy(cfg);changed['approved_context'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):verify_approval(changed)

    def test_paused_provider_cannot_call_even_with_context_approved(self):
        self.config['context_review']='APPROVED';save_json(self.case,self.config)
        run=prepare(self.case,self.root/'runs')
        class NeverCall:
            kind='deepseek'
            def query(self,request):raise AssertionError('Network/model should not run')
        state=drive(run,NeverCall(),True)
        self.assertEqual(state['status'],'WAITING_MODEL')
        self.assertEqual(state['paid_api_calls'],0)
        self.assertFalse((run/'model').exists())

    def test_enabled_competition_call_requires_version_bound_approval(self):
        self.config.update(context_review='APPROVED',live_model_enabled=True);save_json(self.case,self.config)
        run=prepare(self.case,self.root/'runs')
        class NeverCall:
            kind='deepseek'
            def query(self,request):raise AssertionError('Network/model should not run')
        state=drive(run,NeverCall())
        self.assertEqual(state['status'],'WAITING_MODEL')
        self.assertEqual(state['paid_api_calls'],0)
        self.assertIn('approval',state['reason'])

    def test_pause_flag_cannot_be_misspelled_as_string(self):
        self.config['live_model_enabled']='false';save_json(self.case,self.config)
        with self.assertRaises(ValueError):load_case(self.case)


if __name__=='__main__':unittest.main()
