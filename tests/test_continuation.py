import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent import solve, resume
from dfx_agent.model import DeepSeekModel, MiniModel
from dfx_agent.workspace import save_json, read_json


def native(name, arguments, identity):
    return {'message': {'role': 'assistant', 'content': None, 'reasoning_content': '中文推理原样保留',
        'tool_calls': [{'id': identity, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]},
        'finish_reason': 'tool_calls'}


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'value.txt').write_text('before', encoding='utf-8')
        self.out = self.root / 'run'

    def test_fresh_cli_processes_preserve_history_edit_and_counters(self):
        fixtures = self.root / 'responses.json'
        save_json(fixtures, [
            native('edit', {'path': 'value.txt', 'search': 'before', 'replace': 'after'}, 'edit-1'),
            native('read', {'path': 'value.txt'}, 'read-2'),
            native('finish', {'summary': 'offline fixture resumed'}, 'finish-3')])
        command = [sys.executable, '-m', 'dfx_agent']
        first = subprocess.run(command + ['solve', '--repo', str(self.repo), '--task', 'Local fixture',
            '--out', str(self.out), '--responses', str(fixtures), '--model-protocol', 'tool-calls',
            '--pause-after-tools', '1'], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(first.returncode, 3, first.stderr)
        old = read_json(self.out / 'trajectory.json')['messages'][:-1]
        self.assertEqual(read_json(self.out / 'result.json')['status'], 'Paused')
        second = subprocess.run(command + ['resume', '--run', str(self.out)], capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        result = read_json(self.out / 'result.json')
        self.assertEqual((result['model_calls'], result['tool_calls'], result['api_calls']), (3, 3, 0))
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(len(result['segments']), 2)
        self.assertEqual(read_json(self.out / 'continuations/0001/result.json')['status'], 'Paused')
        current = read_json(self.out / 'trajectory.json')['messages']
        self.assertEqual(current[:len(old)], old)
        self.assertEqual((self.out / 'workspace/value.txt').read_text(), 'after')
        next_request = read_json(self.out / 'model/0002/request.json')
        self.assertEqual(next_request[-2]['tool_call_id'], 'edit-1')
        self.assertEqual(next_request[-3]['reasoning_content'], '中文推理原样保留')
        with self.assertRaisesRegex(ValueError, 'Only explicitly'):
            resume(self.out)

    def paused(self, **kwargs):
        return solve(self.repo, 'Local fixture', self.out, responses=[
            native('read', {'path': 'value.txt'}, 'one'),
            native('finish', {'summary': 'done'}, 'two')], model_protocol='tool-calls',
            pause_after_tools=1, **kwargs)

    def test_navigation_flag_is_bound_in_cli_launch_and_retained_on_resume(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'absent-sdk'), 'device': 'fixture'})
        fixtures = self.root / 'responses.json'
        save_json(fixtures, [native('harmony', {'operation': 'info'}, 'info'),
                             native('finish', {'summary': 'Offline only'}, 'done')])
        result = subprocess.run([sys.executable, '-m', 'dfx_agent', 'solve', '--repo', str(self.repo),
            '--task', 'fixture', '--out', str(self.out), '--responses', str(fixtures),
            '--model-protocol', 'tool-calls', '--host', str(host), '--allow-device',
            '--allow-system-navigation', '--pause-after-tools', '1'],
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 3, result.stderr)
        launch = read_json(self.out / 'launch.json')
        self.assertTrue(launch['options']['allow_system_navigation'])
        self.assertTrue(read_json(self.out / 'harmony-host.json')['system_navigation_enabled'])
        self.assertEqual(resume(self.out)['status'], 'Submitted')
        self.assertEqual(read_json(self.out / 'launch.json'), launch)

    def test_exhausted_allowance_cannot_reset(self):
        self.paused(max_steps=1)
        before = (self.out / 'result.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'allowance exhausted'):
            resume(self.out)
        self.assertEqual(before, (self.out / 'result.json').read_bytes())
        self.assertFalse((self.out / 'continuations').exists())

    def test_screen_permission_and_profile_bound_through_cli_and_resume(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'absent'), 'device': 'fixture', 'screen_lock_profile': 'swipe_only'})
        fixtures = self.root / 'screen-responses.json'
        save_json(fixtures, [native('harmony', {'operation': 'info'}, 'info'),
                             native('finish', {'summary': 'offline'}, 'done')])
        result = subprocess.run([sys.executable, '-m', 'dfx_agent', 'solve', '--repo', str(self.repo),
            '--task', 'fixture', '--out', str(self.out), '--responses', str(fixtures),
            '--model-protocol', 'tool-calls', '--host', str(host), '--allow-device',
            '--allow-screen-unlock', '--pause-after-tools', '1'],
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 3, result.stderr)
        launch = read_json(self.out / 'launch.json')
        self.assertTrue(launch['options']['allow_screen_unlock'])
        self.assertEqual(launch['host']['screen_lock_profile'], 'swipe_only')
        self.assertTrue(read_json(self.out / 'harmony-host.json')['screen_unlock_enabled'])
        from dfx_agent.continuation import load_continuation
        data = load_continuation(self.out)
        options = {**launch['options'], 'allow_screen_unlock': False}
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            solve(self.repo, 'fixture', self.out, responses=launch['responses'], _resume=data, **options)
        self.assertFalse((self.out / 'continuations').exists())
        # Resume uses frozen host, not a silently changed external host file.
        save_json(host, {'deveco': 'elsewhere', 'device': 'changed'})
        self.assertEqual(resume(self.out)['status'], 'Submitted')
        self.assertEqual(read_json(self.out / 'launch.json'), launch)

    def test_unknown_request_tail_and_started_tool_are_not_replayed(self):
        self.paused()
        (self.out / 'model/0002').mkdir()
        save_json(self.out / 'model/0002/request.json', [])
        with self.assertRaisesRegex(ValueError, 'Request tail'):
            resume(self.out)
        self.assertFalse((self.out / 'continuations').exists())
        # Separate case, no alteration of the failed run.
        self.out = self.root / 'started'
        self.paused()
        save_json(self.out / 'tools/started.json', {'number': 2})
        with self.assertRaisesRegex(ValueError, 'state unknown'):
            resume(self.out)

    def test_changed_definitions_refused_before_archive(self):
        self.paused()
        with patch('dfx_agent.agent.NATIVE_INSTRUCTIONS', 'different policy'):
            with self.assertRaisesRegex(ValueError, 'configuration changed'):
                resume(self.out)
        self.assertFalse((self.out / 'continuations').exists())

    def test_format_error_streak_survives_interruption(self):
        invalid = native('unknown-tool', {}, 'bad')
        query = MiniModel.query
        def interrupt(model, messages, **kw):
            if model.calls == 2:
                raise KeyboardInterrupt
            return query(model, messages, **kw)
        with patch.object(MiniModel, 'query', interrupt):
            result = solve(self.repo, 'fixture', self.out, responses=[invalid]*3, model_protocol='tool-calls')
        self.assertEqual(result['status'], 'Interrupted')
        self.assertEqual(read_json(self.out / 'checkpoint.json')['consecutive_format_errors'], 2)
        result = resume(self.out)
        self.assertEqual(result['status'], 'RepeatedFormatError')
        self.assertEqual(len(result['model_errors']), 3)
        self.assertEqual(result['model_calls'], 3)

    def test_usage_and_provider_configuration_stay_cumulative(self):
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'outbound_approved': True})
        def response(name, args, identity, usage):
            body = native(name, args, identity)
            return io.BytesIO(json.dumps({'model': 'deepseek-flash', 'usage': usage,
                'choices': [body]}).encode())
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_TEST_KEY'}), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [
                response('read', {'path': 'value.txt'}, 'a', {'total_tokens': 20}),
                response('finish', {'summary': 'mock'}, 'b', None)]
            solve(self.repo, 'fixture', self.out, provider=DeepSeekModel('deepseek-flash'), scope=scope,
                  model_protocol='tool-calls', pause_after_tools=1)
            result = resume(self.out)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(result['usage'], [{'total_tokens': 20}, None])
        self.assertEqual(result['api_calls'], 2)
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertEqual(payload['reasoning_effort'], 'high')
        self.assertEqual(payload['messages'][-2]['tool_call_id'], 'a')

    def test_unknown_internal_tool_error_keeps_started_marker(self):
        with patch('dfx_agent.tools.ProjectTools.file_action', side_effect=RuntimeError('driver defect')):
            result = solve(self.repo, 'fixture', self.out, responses=[{'action': 'read', 'path': '.'}])
        self.assertEqual(result['status'], 'Error')
        self.assertTrue((self.out / 'tools/started.json').exists())
        with self.assertRaises(ValueError):
            resume(self.out)

    def test_response_received_but_not_delivered_is_not_a_completed_boundary(self):
        query = MiniModel.query
        def interrupt_after_response(model, messages, **kw):
            query(model, messages, **kw)
            raise KeyboardInterrupt
        with patch.object(MiniModel, 'query', interrupt_after_response):
            result = solve(self.repo, 'fixture', self.out,
                           responses=[native('read', {'path': '.'}, 'not-delivered')],
                           model_protocol='tool-calls')
        self.assertEqual(result['model_calls'], 1)
        self.assertEqual(result['tool_calls'], 0)
        self.assertFalse((self.out / 'checkpoint.json').exists())
        with self.assertRaises((OSError, ValueError)):
            resume(self.out)

    def test_known_blocked_action_resumes_without_replaying_it(self):
        from dfx_agent.tools import ProjectTools
        original = ProjectTools.file_action
        def blocked_once(tools, action):
            if not tools.events:
                return {'status': 'BLOCKED', 'environment_blocked': True, 'error': 'local test environment'}
            return original(tools, action)
        with patch.object(ProjectTools, 'file_action', blocked_once):
            result = solve(self.repo, 'fixture', self.out, responses=[
                native('read', {'path': '.'}, 'blocked'),
                native('finish', {'summary': 'remaining checks unverified'}, 'end')],
                model_protocol='tool-calls')
        self.assertEqual(result['status'], 'BLOCKED')
        result = resume(self.out)
        self.assertEqual((result['model_calls'], result['tool_calls']), (2, 2))
        self.assertEqual(read_json(self.out / 'continuations/0001/result.json')['status'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
