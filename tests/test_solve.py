import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent import solve
from dfx_agent.cli import main
from dfx_agent.model import DeepSeekModel, MiniModel
from dfx_agent.tools import ProjectTools
from dfx_agent.workspace import save_json


class SolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / '中文 project'
        self.repo.mkdir()
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        (self.repo / 'check.py').write_text('from calc import add\nassert add(2, 3) == 5\nprint("checked")\n', encoding='utf-8')
        self.command = "& '" + sys.executable.replace("'", "''") + "' check.py; exit $LASTEXITCODE"

    def run_task(self, actions, **kwargs):
        return solve(self.repo, 'Fix arithmetic behavior using the project checks', self.root / 'run',
                     responses=actions, **kwargs)

    def test_cli_real_failure_then_revision_and_patch_apply(self):
        actions = [
            {'action': 'read', 'path': '.'},
            {'action': 'search', 'term': 'def add'},
            {'action': 'read', 'path': 'calc.py'},
            {'action': 'edit', 'path': 'calc.py', 'search': 'a - b', 'replace': 'a * b'},
            {'action': 'exec', 'command': self.command},
            {'action': 'read', 'path': 'calc.py'},
            {'action': 'edit', 'path': 'calc.py', 'search': 'a * b', 'replace': 'a + b'},
            {'action': 'write', 'path': 'notes.txt', 'content': '中文记录\n'},
            {'action': 'exec', 'command': self.command},
            {'action': 'finish', 'summary': 'Synthetic fixture: both checks executed; no real LLM used',
             'root_cause': {'path': 'calc.py', 'line': 2}, 'unverified': ['HarmonyOS']},
        ]
        fixtures = self.root / 'fixtures.json'
        save_json(fixtures, actions)
        with contextlib.redirect_stdout(io.StringIO()), patch('urllib.request.OpenerDirector.open',
                                                             side_effect=AssertionError('No network')):
            code = main(['solve', '--repo', str(self.repo), '--task', 'Fix arithmetic',
                         '--responses', str(fixtures), '--out', str(self.root / 'run')])
        self.assertEqual(code, 0)
        output = self.root / 'run'
        events = [json.loads(line) for line in (output / 'tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
        commands = [e['result'] for e in events if e['action']['action'] == 'exec']
        self.assertNotEqual(commands[0]['returncode'], 0)
        self.assertEqual(commands[1]['returncode'], 0)
        self.assertIn('a * b', events[5]['result']['content'])
        history = json.loads((output / 'model/0006/request.json').read_text(encoding='utf-8'))
        self.assertIn('AssertionError', history[-2]['content'])
        self.assertIn('a - b', (self.repo / 'calc.py').read_text())
        self.assertIn('a + b', (output / 'workspace/calc.py').read_text())
        self.assertFalse((output / 'workspace/__pycache__').exists())
        result = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['evaluation_scope'], 'offline_fixture')
        self.assertEqual(result['api_calls'], 0)
        self.assertFalse(result['repair_validated'])
        self.assertEqual(result['independent_verification'], 'NOT_RUN')
        patch_text = (output / 'patch.diff').read_text(encoding='utf-8')
        self.assertIn('new file mode', patch_text)
        self.assertNotIn('a * b', patch_text)
        check = subprocess.run(['git', 'apply', '--check', str(output / 'patch.diff')],
                               cwd=output / 'original', capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue((output / 'report.md').exists())

    def test_tool_errors_are_observations_and_next_action_runs(self):
        result = self.run_task([
            {'action': 'read', 'path': 'missing.py'},
            {'action': 'edit', 'path': 'calc.py', 'search': 'not-present', 'replace': 'x'},
            {'action': 'read', 'path': 'calc.py'},
            {'action': 'finish', 'summary': 'error observations checked'},
        ])
        self.assertEqual(result['status'], 'Submitted')
        request = json.loads((self.root / 'run/model/0003/request.json').read_text())
        self.assertIn('exactly once', request[-2]['content'])

    def test_timeout_real_child_is_stopped_and_evidence_preserved(self):
        command = "& '" + sys.executable.replace("'", "''") + "' -c \"import time,pathlib; time.sleep(3); pathlib.Path('late.txt').write_text('bad')\""
        result = self.run_task([{'action': 'exec', 'command': command}], command_timeout=1)
        self.assertEqual(result['status'], 'FixtureExhausted')
        event = json.loads((self.root / 'run/tools/events.jsonl').read_text())
        self.assertEqual(event['result']['status'], 'TIMEOUT')
        self.assertIn('timed out', event['result']['error'])
        import time
        time.sleep(3)
        self.assertFalse((self.root / 'run/workspace/late.txt').exists())
        self.assertFalse(result['repair_validated'])

    def test_step_context_and_format_limits_remain_explicit(self):
        actions = [{'action': 'read', 'path': 'calc.py'}, {'action': 'finish', 'summary': 'must not run'}]
        result = self.run_task(actions, max_steps=1)
        self.assertEqual(result['status'], 'LimitsExceeded')
        result = solve(self.repo, 'Task', self.root / 'context', responses=actions, max_context_chars=1)
        self.assertEqual(result['status'], 'ContextLimit')
        self.assertEqual(result['model_calls'], 0)
        result = solve(self.repo, 'Task', self.root / 'format', responses=[None, [], 'invalid'])
        self.assertEqual(result['status'], 'RepeatedFormatError')

    def test_interrupt_preserves_prior_patch_and_does_not_replay(self):
        original = MiniModel.query
        def interrupt(model, messages, **kwargs):
            if model.calls == 1:
                raise KeyboardInterrupt()
            return original(model, messages, **kwargs)
        with patch.object(MiniModel, 'query', interrupt):
            result = self.run_task([{'action': 'edit', 'path': 'calc.py', 'search': 'a - b', 'replace': 'a + b'}])
        self.assertEqual(result['status'], 'Interrupted')
        self.assertIn('calc.py', result['changed_files'])
        self.assertEqual(result['tool_calls'], 1)
        self.assertTrue((self.root / 'run/trajectory.json').exists())

    def test_scope_excludes_inputs_and_tools_do_not_inherit_model_key(self):
        (self.repo / '.env').write_text('not-a-real-secret')
        (self.repo / 'oracle.txt').write_text('hidden-answer')
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'exclude': ['oracle.txt'], 'read_only': ['check.py']})
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_TEST_KEY'}):
            result = self.run_task([
                {'action': 'edit', 'path': 'check.py', 'search': '== 5', 'replace': '== 9'},
                {'action': 'exec', 'command': "if ($env:DEEPSEEK_API_KEY) { exit 9 }; Write-Output 'no key'"},
                {'action': 'finish', 'summary': 'scope test'},
            ], scope=scope)
        self.assertEqual(result['status'], 'Submitted')
        self.assertFalse((self.root / 'run/workspace/oracle.txt').exists())
        self.assertFalse((self.root / 'run/workspace/.env').exists())
        events = (self.root / 'run/tools/events.jsonl').read_text()
        self.assertIn('read-only', events)
        self.assertNotIn('FAKE_TEST_KEY', events)
        self.assertIn('no key', events)

    def test_live_transport_history_and_missing_usage_with_fake_http(self):
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'outbound_approved': True})
        def envelope(action):
            return io.BytesIO(json.dumps({'id': 'fake', 'model': 'deepseek-flash',
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(action)}}]}).encode())
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_TEST_KEY'}), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [
                envelope({'action': 'exec', 'command': "Write-Output 'actual-tool-output'"}),
                envelope({'action': 'finish', 'summary': 'mock HTTP only'}),
            ]
            result = solve(self.repo, 'Task', self.root / 'run', scope=scope, provider=DeepSeekModel('deepseek-flash'))
            sent = json.loads(opener.return_value.open.call_args_list[1].args[0].data)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['usage'], [None, None])
        self.assertEqual(len(sent['messages']), 5)
        self.assertIn('actual-tool-output', sent['messages'][-2]['content'])
        self.assertNotIn('extra', sent['messages'][2])

    def test_live_rejected_before_transport_without_scope(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_TEST_KEY'}), patch('urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'approved project scope'):
                solve(self.repo, 'Task', self.root / 'run', provider=DeepSeekModel('deepseek-flash'))
        opener.assert_not_called()

    def test_large_permission_list_stays_local_and_file_tools_expose_restrictions(self):
        read_only = ['check.py'] + [f'resources/asset_{n}.txt' for n in range(500)]
        scope = self.root / 'scope-large.json'
        save_json(scope, {'repo': str(self.repo), 'outbound_approved': True, 'read_only': read_only})
        run = self.root / 'scope-run'
        result = solve(self.repo, 'Check permission observations', run, scope=scope, responses=[
            {'action': 'read', 'path': '.'},
            {'action': 'read', 'path': 'check.py'},
            {'action': 'edit', 'path': 'check.py', 'search': 'anything', 'replace': 'x'},
            {'action': 'read', 'source': 'tool-log', 'path': 'project-scope.json', 'start': 1, 'end': 10},
            {'action': 'finish', 'summary': 'Offline scope check'},
        ])
        self.assertEqual(result['status'], 'Submitted')
        first = json.loads((run / 'model/0001/request.json').read_text(encoding='utf-8'))[-1]['content']
        self.assertNotIn('asset_499', first)
        self.assertLess(len(first), 1500)
        policy = json.loads((run / 'tools/project-scope.json').read_text(encoding='utf-8'))
        self.assertEqual(policy['read_only'], read_only)
        events = [json.loads(s) for s in (run / 'tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIn('check.py', events[0]['result']['read_only_entries'])
        self.assertTrue(events[1]['result']['read_only'])
        self.assertEqual(events[2]['result']['status'], 'TOOL_ERROR')
        self.assertIn('check.py', events[3]['result']['content'])

    def test_original_and_command_logs_are_readable_without_polluting_patch(self):
        result = self.run_task([
            {'action': 'exec', 'command': "Write-Output 'log proof'"},
            {'action': 'read', 'source': 'tool-log', 'path': '0001.log'},
            {'action': 'read', 'source': 'original', 'path': 'calc.py'},
            {'action': 'finish', 'summary': 'read evidence'},
        ])
        self.assertEqual(result['changed_files'], [])
        history = (self.root / 'run/model/0003/request.json').read_text()
        self.assertIn('log proof', history)

    def test_unexpected_internal_error_is_not_a_success(self):
        with patch.object(ProjectTools, 'file_action', side_effect=RuntimeError('internal defect')):
            result = self.run_task([{'action': 'read', 'path': '.'}])
        self.assertEqual(result['status'], 'Error')
        self.assertIn('internal defect', result['error'])
        self.assertTrue((self.root / 'run/trajectory.json').exists())

    def test_deleted_empty_added_and_binary_files_are_accounted_for(self):
        (self.repo / 'remove.txt').write_text('remove this\n')
        result = self.run_task([
            {'action': 'delete', 'path': 'remove.txt'},
            {'action': 'write', 'path': 'empty.txt', 'content': ''},
            {'action': 'exec', 'command': "[IO.File]::WriteAllBytes((Join-Path (Get-Location) 'new.bin'), [byte[]](0,255))"},
            {'action': 'finish', 'summary': 'export checks'},
        ])
        self.assertEqual(result['binary_changes_not_in_patch'], ['new.bin'])
        patch_text = (self.root / 'run/patch.diff').read_text()
        self.assertIn('deleted file mode', patch_text)
        self.assertIn('new file mode', patch_text)
        self.assertIn('empty.txt', result['changed_files'])

    def test_edit_preserves_windows_line_endings(self):
        (self.repo / 'calc.py').write_bytes(b'def add(a, b):\r\n    return a - b\r\n')
        self.run_task([{'action': 'edit', 'path': 'calc.py', 'search': 'a - b', 'replace': 'a + b'}])
        self.assertEqual((self.root / 'run/workspace/calc.py').read_bytes(),
                         b'def add(a, b):\r\n    return a + b\r\n')

    def test_transport_format_feedback_is_preserved_and_not_retried_automatically(self):
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'outbound_approved': True})
        def envelope(content):
            return io.BytesIO(json.dumps({'id': 'fake', 'model': 'deepseek-flash',
                'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}).encode())
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_TEST_KEY'}), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [envelope('{broken'),
                envelope('{"action":"finish","summary":"mock format repair"}')]
            result = solve(self.repo, 'Task', self.root / 'run', scope=scope, provider=DeepSeekModel('deepseek-flash'))
            sent = json.loads(opener.return_value.open.call_args_list[1].args[0].data)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual((self.root / 'run/model/0001/rejected-content.txt').read_text(), '{broken')
        self.assertIn('Invalid JSON', sent['messages'][-2]['content'])

    def test_interrupted_command_is_recorded_and_process_cleanup_requested(self):
        from dfx_agent.task import prepare_task
        record = prepare_task(self.repo, self.root / 'run', 'offline interruption')
        with patch('dfx_agent.agent.prepare_task', return_value=record), \
             patch('dfx_agent.validation.subprocess.Popen') as popen, patch('dfx_agent.validation.subprocess.run') as kill:
            kill.return_value.returncode = 0
            popen.return_value.wait.side_effect = [KeyboardInterrupt(), 1]
            popen.return_value.pid = 123456
            result = self.run_task([{'action': 'exec', 'command': 'Write-Output interrupted'}])
        self.assertEqual(result['status'], 'Interrupted')
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0], ['taskkill.exe', '/PID', '123456', '/T', '/F'])
        event = json.loads((self.root / 'run/tools/events.jsonl').read_text())
        self.assertEqual(event['result']['status'], 'INTERRUPTED')
