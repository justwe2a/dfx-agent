import copy
import json
from pathlib import Path
import tempfile
import unittest

from dfx_agent.evidence import Evidence
from dfx_agent.context_review import verify_approval, export_review
from dfx_agent.outbound import project_request, build_diagnostics
from dfx_agent.repair import apply_proposal, validate_proposal
from dfx_agent.runner import prepare, drive, request_for
from dfx_agent.workspace import save_json, read_json, load_case, snapshot


class SourceRangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'main.js').write_text('public marker\nreturn 0;\nPRIVATE_ANSWER marker\n', encoding='utf-8')
        self.log = self.root / 'crash.log'
        self.log.write_text('Error name:TypeError\nError message:bad\nStacktrace:\n at f (main.js:2:1)\n', encoding='utf-8')
        self.case = self.root / 'case.json'
        self.config = {'schema_version': 2, 'source_version': 'range-fixture-v1', 'case_id': 'synthetic/ranges',
                       'source_root': str(self.source), 'logs': [str(self.log)], 'visible_files': ['main.js'],
                       'editable_files': ['main.js'], 'visible_ranges': {'main.js': [[1, 2]]},
                       'editable_ranges': {'main.js': [[2, 2]]}, 'context_review': 'APPROVED',
                       'max_attempts': 1, 'max_model_calls': 8}
        self.config['approved_context'] = {k: copy.deepcopy(self.config[k]) for k in
            ('source_version', 'visible_files', 'editable_files', 'visible_ranges', 'editable_ranges')}
        self.config['approved_context']['log_names'] = ['crash.log']
        save_json(self.case, self.config)
        self.evidence = Evidence(self.source, ['main.js'], self.config['visible_ranges'])
        self.item = self.evidence.read('main.js', 1, 2)

    def proposal(self, search='return 0;', replace='return 1;'):
        point = {'path': 'main.js', 'line': 2}
        return {'action': 'propose', 'localization': {'root_cause': point, 'crash_location': point,
                'edit_location': point, 'explanation': 'Synthetic range fixture', 'evidence_refs': [self.item['id']]},
                'edits': [{'path': 'main.js', 'search': search, 'replace': replace}]}

    def test_read_search_initial_and_manifest_honor_ranges(self):
        for start, end in [(2, 3), (3, 3), (1, 20)]:
            with self.assertRaises(ValueError): self.evidence.read('main.js', start, end)
        self.assertEqual(len(self.evidence.search('marker')['matches']), 1)
        initial = Evidence(self.source, ['main.js'], self.config['visible_ranges']).initial([str(self.log)])
        self.assertEqual(initial['snippets'], [])
        self.assertIn('read_error', initial['path_mappings'][0])
        export_review(self.case, self.root / 'review')
        self.assertEqual(read_json(self.root/'review/manifest.json')['source_files'][0]['read_ranges'], [[1, 2]])
        self.assertNotIn('PRIVATE_ANSWER', (self.root/'review/initial-request.preview.json').read_text())

    def test_approval_and_configuration_reject_scope_expansion(self):
        verify_approval(load_case(self.case))
        changed = copy.deepcopy(self.config); changed['visible_ranges']['main.js'] = [[1, 3]]
        with self.assertRaises(ValueError): verify_approval(changed)
        for key, value in [('visible_ranges', {'main.js': [[0, 2]]}),
                           ('editable_ranges', {'main.js': [[2, 3]]}), ('editable_ranges', {})]:
            changed = copy.deepcopy(self.config); changed[key] = value; save_json(self.case, changed)
            with self.assertRaises(ValueError): load_case(self.case)

    def test_edit_boundary_and_zero_context_diff(self):
        for old in ['public marker', 'return 0;\nPRIVATE_ANSWER', 'PRIVATE_ANSWER']:
            with self.assertRaises(ValueError):
                validate_proposal(self.proposal(old, 'changed'), self.source, ['main.js'], self.evidence.items,
                                  self.config['editable_ranges'])
        target = self.root / 'candidate'; snapshot(self.source, target)
        result = apply_proposal(self.proposal('return 0;\n', 'return 1;\n'), self.source, target, ['main.js'],
                                self.evidence.items, self.config['editable_ranges'])
        self.assertNotIn('PRIVATE_ANSWER', result['diff'])
        self.assertNotIn('public marker', result['diff'])
        self.assertIn('PRIVATE_ANSWER marker', (target/'main.js').read_text())

    def test_projection_rejects_cached_private_snippets_and_filters_search_diagnostics(self):
        run = prepare(self.case, self.root/'runs'); state = read_json(run/'state.json')
        state['feedback'] = [{'tool': 'search', 'result': {'matches': [
            {'path': 'main.js', 'line': 3, 'text': 'PRIVATE_ANSWER'}], 'truncated': False}}]
        request = request_for(run, read_json(run/'case.json'), state)
        self.assertNotIn('PRIVATE_ANSWER', json.dumps(request))
        request['retrieved_snippets'] = [{**self.item, 'start': 3, 'end': 3, 'content': 'PRIVATE_ANSWER'}]
        with self.assertRaises(ValueError): project_request(request)
        tail = '10605038 ArkTS Compiler Error\nError Message: secret (arkts-no-any-unknown) At File: C:/host/main.js:3:1'
        self.assertEqual(build_diagnostics(tail, ['main.js'], self.config['visible_ranges']), [])

    def test_actual_runner_rejects_private_read_edit_then_applies_allowed_candidate(self):
        run = prepare(self.case, self.root/'runs')
        responses = [{'action': 'read', 'path': 'main.js', 'start': 1, 'end': 2},
                     {'action': 'read', 'path': 'main.js', 'start': 3, 'end': 3},
                     {'action': 'search', 'term': 'marker'}, self.proposal('PRIVATE_ANSWER', 'bad'), self.proposal()]
        class Fixture:
            kind = 'offline_fixture'
            def query(inner, request):
                self.assertNotIn('PRIVATE_ANSWER', json.dumps(request))
                return responses.pop(0) if responses else None
        state = drive(run, Fixture(), False)
        self.assertEqual(state['model_calls'], 5)
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['paid_api_calls'], 0)
        self.assertEqual((run/'attempts/001/workspace/main.js').read_text(),
                         'public marker\nreturn 1;\nPRIVATE_ANSWER marker\n')


if __name__ == '__main__': unittest.main()
