import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.evidence import Evidence, parse_jscrash
from dfx_agent.model import TranscriptModel
from dfx_agent.repair import validate_proposal, apply_proposal
from dfx_agent.runner import drive, prepare, revalidate, request_for
from dfx_agent.validation import validate
from dfx_agent.workspace import confined, file_manifest, save_json, snapshot


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'main.js').write_text('function calculate() { return 0; }\n', encoding='utf-8')
        (self.source / 'private.txt').write_text('ANSWER_SECRET', encoding='utf-8')
        self.log = self.root / 'crash.log'
        self.log.write_text('Module name:demo\nError name:TypeError\nError message:bad\nStacktrace:\n'
                            ' at calculate module (main.js:1:2)\nHiLog:\nANSWER_SECRET', encoding='utf-8')
        self.case = self.root / 'case.json'
        self.config = {'schema_version': 2, 'source_version':'test-source-v1', 'case_id': 'synthetic/demo', 'source_root': str(self.source),
                       'logs': [str(self.log)], 'visible_files': ['main.js'], 'editable_files': ['main.js'],
                       'context_review': 'APPROVED', 'max_attempts': 3, 'max_model_calls': 8}
        save_json(self.case, self.config)

    def prepared(self):
        save_json(self.case, self.config)
        return prepare(self.case, self.root / 'runs')

    def proposal(self, run, replacement='return 1;'):
        evidence = json.loads((run / 'evidence.json').read_text(encoding='utf-8'))['snippets'][0]
        point = {'path': 'main.js', 'line': 1}
        return {'action': 'propose', 'localization': {'root_cause': point, 'crash_location': point,
                'edit_location': point, 'explanation': 'Synthetic fixture', 'evidence_refs': [evidence['id']]},
                'edits': [{'path': 'main.js', 'search': 'return 0;', 'replace': replacement}]}

    def model(self, responses):
        path = self.root / 'responses.json'
        save_json(path, responses)
        return TranscriptModel(path)

    def adapters(self, behavior='normal'):
        adapter = self.root / 'adapter.py'
        adapter.write_text('''import os, sys, json
from pathlib import Path
stage = sys.argv[1]
if stage == 'build':
    p=Path('out.bin'); p.write_bytes(Path('main.js').read_bytes())
else:
    data={k:os.environ['DFX_'+k.upper()] for k in ['run_id','attempt_id','snapshot_id','patch_id','stage','artifact_id']}
    data['status']='FAIL' if stage=='functional' and 'return 1;' in Path('main.js').read_text() else 'PASS'
    data['details']={'assertion':'synthetic expected candidate 2'}
    if sys.argv[2]=='stale': data['attempt_id']='stale'
    if sys.argv[2]!='missing': Path(os.environ['DFX_REPORT_PATH']).write_text(json.dumps(data))
''', encoding='utf-8')
        self.config['validation'] = {s: {'argv': [sys.executable, str(adapter), s, behavior], 'timeout_seconds': 10}
                                     for s in ['build', 'deploy', 'replay', 'functional', 'regression']}
        self.config['validation']['build']['artifact'] = 'out.bin'
        proof = self.root / 'proof.json'
        save_json(proof, {'case_id': self.config['case_id'], 'snapshot_id': self.config['source_version'],
                          'status': 'REPRODUCED'})
        self.config['baseline_report'] = str(proof)

    def test_jscrash_ignores_hilog_and_does_not_invent_root_cause(self):
        parsed = parse_jscrash(self.log.read_text())
        self.assertEqual(parsed['frames'][0]['line'], 1)
        self.assertNotIn('root_cause', parsed)
        self.assertNotIn('ANSWER_SECRET', str(parsed))

    def test_explicit_revalidation_preserves_failure_and_fixture_provenance(self):
        self.adapters();self.config['max_attempts'] = 1
        run = self.prepared()
        old = drive(run, self.model([self.proposal(run)]), True)
        self.assertEqual(old['status'], 'EXHAUSTED')
        before = (run/'attempts/001/workspace/main.js').read_bytes()
        adapter = self.root/'adapter.py'
        text = adapter.read_text(encoding='utf-8')
        adapter.write_text(text.replace("'FAIL' if stage=='functional' and 'return 1;' in Path('main.js').read_text() else 'PASS'", "'PASS'"), encoding='utf-8')
        new = revalidate(run, 1, 'Corrected trusted synthetic acceptance adapter')
        self.assertEqual(new['status'], 'FIXTURE_PASSED')
        self.assertEqual(new['model_calls'], old['model_calls'])
        self.assertEqual(new['paid_api_calls'], 0)
        self.assertEqual(new['attempts'][0]['validation']['stages']['functional']['status'], 'FAIL')
        self.assertTrue(new['attempts'][0]['revalidations'][0]['validation']['all_passed'])
        self.assertEqual((run/'attempts/001/revalidations/001/workspace/main.js').read_bytes(), before)
        self.assertTrue((run/'attempts/001/revalidations/001/result-before.json').exists())

    def test_failed_revalidation_cannot_create_success_or_call_model(self):
        self.adapters();self.config['max_attempts'] = 1
        run = self.prepared();old = drive(run, self.model([self.proposal(run)]), True)
        new = revalidate(run, 1, 'Check unchanged failing adapter')
        self.assertEqual(new['status'], 'BLOCKED')
        self.assertIsNone(new['validated_attempt'])
        self.assertEqual(new['model_calls'], old['model_calls'])
        with self.assertRaises(ValueError): revalidate(run, 99, 'No such candidate')

    def test_context_is_allowlisted_and_duplicate_logs_are_deduplicated(self):
        evidence = Evidence(self.source, ['main.js'])
        with self.assertRaises(ValueError):
            evidence.read('private.txt', 1, 1)
        context = evidence.initial([str(self.log), str(self.log)])
        self.assertEqual(len(context['crashes']), 1)
        self.assertNotIn('ANSWER_SECRET', str(context))

    def test_path_escape_ads_and_windows_aliases_rejected(self):
        for path in ['../outside', 'a/../main.js', 'C:/temp', 'main.js:secret', 'CON', 'main.js.', 'a\\b']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                confined(self.source, path)

    def test_ambiguous_stack_path_is_not_guessed(self):
        for name in ['a/shared.js', 'b/shared.js']:
            p = self.source / name; p.parent.mkdir(); p.write_text('hello')
        self.log.write_text('Error name:Error\nStacktrace:\n at f (shared.js:1:1)')
        result = Evidence(self.source, ['a/shared.js','b/shared.js']).initial([str(self.log)])
        self.assertEqual(result['path_mappings'][0]['status'], 'UNRESOLVED')
        self.assertEqual(result['snippets'], [])

    def test_invalid_patch_is_atomic_and_bound_to_original(self):
        run = self.prepared(); proposal = self.proposal(run)
        refs = json.loads((run/'state.json').read_text())['evidence']
        for mutation in [{'search': 'absent'}, {'path': 'private.txt'}]:
            bad = json.loads(json.dumps(proposal)); bad['edits'][0].update(mutation)
            with self.assertRaises(ValueError): validate_proposal(bad, run/'baseline', ['main.js'], refs)
        self.assertIn('return 0;', (self.source/'main.js').read_text())

    def test_no_provider_saves_request_and_waits_without_changing_source(self):
        run = self.prepared(); state = drive(run)
        self.assertEqual(state['status'], 'WAITING_MODEL')
        self.assertEqual(state['model_calls'], 0)
        self.assertTrue((run/'next-request.json').is_file())

    def test_pending_context_blocks_model_access(self):
        self.config['context_review'] = 'PENDING'
        run = self.prepared()
        state = drive(run, self.model([{'action': 'anything'}]))
        self.assertEqual(state['model_calls'], 0)
        self.assertFalse((run/'model').exists())

    def test_retry_uses_original_snapshot_and_receives_failed_check(self):
        self.adapters(); run = self.prepared()
        state = drive(run, self.model([self.proposal(run), self.proposal(run, 'return 2;')]), True)
        self.assertEqual(state['status'], 'FIXTURE_PASSED')
        self.assertEqual(len(state['attempts']), 2)
        request = json.loads((run/'model/002/request.json').read_text())
        self.assertEqual(request['feedback'][-1]['stage'], 'functional')
        self.assertEqual((self.source/'main.js').read_text(), 'function calculate() { return 0; }\n')
        self.assertFalse(json.loads((run/'result.json').read_text())['repair_validated'])

    def single_patch(self):
        self.config.update(workflow_mode='single_patch', max_model_calls=1, max_attempts=1)

    def test_single_patch_rejects_retrieval_without_reading_or_second_request(self):
        for action in ('read', 'search'):
            self.single_patch();run=self.prepared()
            response={'action':action,'path':'main.js','start':1,'end':1,'term':'calculate'}
            model=self.model([response,self.proposal(run)])
            with (patch('dfx_agent.evidence.Evidence.read',side_effect=AssertionError('No read')),
                  patch('dfx_agent.evidence.Evidence.search',side_effect=AssertionError('No search'))):
                state=drive(run,model)
            self.assertEqual(state['status'],'EXHAUSTED')
            self.assertEqual(model.index,1)
            self.assertEqual(state['attempts'],[])

    def test_single_patch_has_no_format_or_validation_retry(self):
        self.single_patch();run=self.prepared()
        state=drive(run,self.model([{'action':'propose'},self.proposal(run)]))
        self.assertEqual((state['status'],state['model_calls']),('EXHAUSTED',1))
        self.adapters();run=self.prepared()
        model=self.model([self.proposal(run),self.proposal(run,'return 2;')])
        state=drive(run,model,True)
        self.assertEqual(state['status'],'EXHAUSTED')
        self.assertEqual(state['attempts'][0]['validation']['stages']['functional']['status'],'FAIL')
        self.assertEqual(model.index,1)

    def test_single_patch_uses_same_payload_and_full_validation(self):
        self.adapters();a=self.prepared();self.single_patch();b=self.prepared()
        def request(run):
            return request_for(run,json.loads((run/'case.json').read_text()),json.loads((run/'state.json').read_text()))
        qa,qb=request(a),request(b)
        self.assertIn('READ:',qa.pop('instructions'))
        self.assertNotIn('READ:',qb.pop('instructions'))
        self.assertEqual(qa,qb)
        state=drive(b,self.model([self.proposal(b,'return 2;')]),True)
        self.assertEqual(state['status'],'FIXTURE_PASSED')
        self.assertTrue(state['attempts'][0]['validation']['all_passed'])

    def test_unknown_mode_or_increased_single_patch_budget_is_not_silently_changed(self):
        self.config['workflow_mode']='unknown'
        with self.assertRaises(ValueError):self.prepared()
        self.config['workflow_mode']='single_patch'
        with self.assertRaises(ValueError):self.prepared()

    def test_exit_zero_without_result_is_inconclusive(self):
        self.adapters('missing'); run=self.prepared()
        state=drive(run,self.model([self.proposal(run)]),True)
        self.assertEqual(state['status'],'BLOCKED')
        self.assertEqual(state['attempts'][0]['validation']['stages']['deploy']['status'],'INCONCLUSIVE')

    def test_stale_report_is_not_accepted(self):
        self.adapters('stale'); run=self.prepared()
        state=drive(run,self.model([self.proposal(run)]),True)
        self.assertEqual(state['status'],'BLOCKED')

    def test_duplicate_patch_stops_without_rerunning_validation(self):
        self.adapters(); run=self.prepared(); p=self.proposal(run)
        state=drive(run,self.model([p,p]),True)
        self.assertEqual(state['status'],'DUPLICATE_PATCH')
        self.assertFalse((run/'attempts/002/validation').exists())

    def test_continue_validation_checkpoint(self):
        self.adapters(); run=self.prepared()
        state=drive(run,self.model([self.proposal(run,'return 2;')]))
        self.assertEqual(state['status'],'WAITING_VALIDATION')
        state=drive(run,execute_commands=True)
        self.assertEqual(state['status'],'FIXTURE_PASSED')

    def test_missing_baseline_prevents_full_success(self):
        self.adapters(); self.config.pop('baseline_report'); run=self.prepared()
        state=drive(run,self.model([self.proposal(run,'return 2;')]),True)
        self.assertEqual(state['status'],'INCONCLUSIVE')

    def test_budget_and_unsupported_actions_cannot_execute_shell(self):
        self.config['max_model_calls']=2; run=self.prepared()
        state=drive(run,self.model([{'action':'shell','command':'danger'}, {'action':'read','path':'private.txt','start':1,'end':1}]))
        self.assertEqual(state['status'],'EXHAUSTED')
        self.assertEqual(len(state['attempts']),0)

    def test_timeout_is_environment_block_not_repair_failure(self):
        spec={'build':{'argv':[sys.executable,'-c','import time; time.sleep(5)'],
                       'artifact':'fresh.bin','timeout_seconds':0.1}}
        result=validate(spec,self.source,self.root/'check',{'run_id':'r','attempt_id':'1','patch_id':'p','snapshot_id':'s'})
        self.assertEqual(result['stages']['build']['status'],'BLOCKED')

    def test_existing_artifact_cannot_be_reused(self):
        (self.source/'old.bin').write_bytes(b'old')
        result=validate({'build':{'argv':[sys.executable,'-c','pass'],'artifact':'old.bin'}},self.source,
                        self.root/'check',{'run_id':'r','attempt_id':'1','patch_id':'p','snapshot_id':'s'})
        self.assertEqual(result['stages']['build']['status'],'BLOCKED')

    def test_malformed_localization_is_feedback_not_crash(self):
        run=self.prepared(); bad=self.proposal(run); bad['localization']=[]
        state=drive(run,self.model([bad]))
        self.assertEqual(state['status'],'WAITING_MODEL')
        self.assertIn('localization must be an object',state['feedback'][-1]['error'])

    def test_legacy_run_requires_explicit_new_prepare(self):
        run=self.prepared()
        state=json.loads((run/'state.json').read_text());state['schema_version']=1
        save_json(run/'state.json',state)
        with self.assertRaisesRegex(ValueError,'Legacy run'):drive(run)

    def test_missing_validation_stages_never_count_as_success(self):
        run=self.prepared();state=drive(run,self.model([self.proposal(run)]),True)
        self.assertEqual(state['status'],'BLOCKED')
        self.assertFalse(state['attempts'][0]['validation']['all_passed'])

    def test_crlf_patch_preserves_source_and_has_clean_diff(self):
        (self.source/'main.js').write_bytes(b'function calculate() { return 0; }\r\n')
        run=self.prepared(); proposal=self.proposal(run)
        snapshot(run/'baseline',run/'candidate')
        refs=json.loads((run/'state.json').read_text())['evidence']
        result=apply_proposal(proposal,run/'baseline',run/'candidate',['main.js'],refs)
        self.assertTrue((run/'candidate/main.js').read_bytes().endswith(b'\r\n'))
        self.assertNotIn('\r',result['diff'])

    @patch.dict('os.environ', {'DEEPSEEK_API_KEY':'DO_NOT_INHERIT', 'OS':'Windows_NT'})
    def test_validation_subprocess_does_not_inherit_model_credentials(self):
        code="import os; from pathlib import Path; assert 'DEEPSEEK_API_KEY' not in os.environ; assert os.environ['OS'] == 'Windows_NT'; Path('new.bin').write_bytes(b'pass')"
        result=validate({'build':{'argv':[sys.executable,'-c',code],'artifact':'new.bin'}},self.source,
                        self.root/'check',{'run_id':'r','attempt_id':'1','patch_id':'p','snapshot_id':'s'})
        self.assertEqual(result['stages']['build']['status'],'PASS')


if __name__ == '__main__':
    unittest.main()
