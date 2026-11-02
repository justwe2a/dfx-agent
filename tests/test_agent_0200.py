"""Regression evidence through the real mini/client/tools and HTTP workbench; no live API/device."""
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from dfx_agent.agent import solve, resume
from dfx_agent.harmony_script import ScriptDevice
from dfx_agent.harmony_tools import HarmonyTools
from dfx_agent.model import DeepSeekModel
from dfx_agent.tools import ProjectTools
from dfx_agent.workbench import create_app
from dfx_agent.workspace import read_json, save_json


def call(name, args, identity):
    return {'id': identity, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}


def response(*calls):
    return {'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'reasoning_content': '保留完整思考', 'tool_calls': list(calls)}}


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'value.txt').write_text('before', encoding='utf-8')
        self.out = self.root / 'run'

    def test_build_profile_newlines_only_preserved_not_broad_ignore(self):
        module = self.repo / 'entry'
        module.mkdir()
        (module / 'build-profile.json5').write_text('{}')
        profile = module / 'BuildProfile.ets'
        profile.write_bytes(b'export const VERSION = 1;\r\n')
        business = module / 'Business.ets'
        business.write_bytes(b'let value = 1;\r\n')
        tool = ProjectTools(self.repo, self.root / 'tools', read_only=['entry/*'])
        profile.write_bytes(b'export const VERSION = 1;\n')
        result = tool.check_read_only({'returncode': 0})
        self.assertEqual(result['generated_profile_newline_changes'], ['entry/BuildProfile.ets'])
        self.assertNotIn('scope_blocked', result)
        self.assertEqual(profile.read_bytes(), b'export const VERSION = 1;\n')
        business.write_bytes(b'let value = 1;\n')
        self.assertEqual(tool.check_read_only({})['scope_changes'], ['entry/Business.ets'])
        profile.write_bytes(b'export const VERSION = 2;\n')
        self.assertIn('entry/BuildProfile.ets', tool.check_read_only({})['scope_changes'])

    def test_script_layout_matches_direct_saved_page_without_device(self):
        output = self.root / 'tools'
        adapter = HarmonyTools(self.repo, output, {'deveco': str(self.root / 'absent'), 'device': ''})
        adapter.target_bundle = 'example.app'
        save_json(output / 'layout.json', {'children': [{'attributes': {'bundleName': 'example.app'}, 'children': [
            {'attributes': {'type': 'Text', 'text': str(i)}} for i in range(90)]}]})
        class LocalDevice:
            def action(self, *args, **kwargs):
                pass
        script = ScriptDevice(adapter, LocalDevice())
        with patch('dfx_agent.harmony_device.execute', side_effect=AssertionError('No HDC allowed')):
            expected = adapter.execute({'operation': 'layout', 'saved_layout': 'layout.json',
                'offset': 10, 'limit': 80, 'selector': {'type': 'Text', 'match': 'exact'}}, 1)
            actual = script.layout(saved_layout='layout.json', offset=10, limit=80, type='Text', match='exact')
            for field in ('nodes', 'total_matches', 'next_offset', 'fresh_capture'):
                self.assertEqual(actual[field], expected[field])
            self.assertEqual(len(actual['nodes']), 80)
            with self.assertRaisesRegex(ValueError, '1..80'):
                script.layout(saved_layout='layout.json', limit=81)

    def http_run(self, replies, *, out=None, **options):
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'outbound_approved': True})
        self.sent = []
        remaining = iter(replies)
        def serve(request, timeout):
            self.sent.append(json.loads(request.data))
            reply = next(remaining)
            if hasattr(reply, 'read'):
                return reply
            return io.BytesIO(json.dumps({'id': 'offline', 'model': 'deepseek-flash',
                                         'choices': [reply]}).encode())
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_OFFLINE_KEY'}), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = serve
            return solve(self.repo, 'Only the public task', out or self.out, provider=DeepSeekModel('deepseek-flash'),
                         model_protocol='tool-calls', scope=scope, **options)

    def test_native_read_group_sequential_error_receipt_and_stable_prefix(self):
        result = self.http_run([
            response(call('read', {'path': 'missing.txt'}, 'one'), call('read', {'path': 'value.txt'}, 'two')),
            response(call('search', {'term': 'before'}, 'three')),
            response(call('finish', {'summary': 'offline only'}, 'done'))])
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual((result['model_calls'], result['tool_calls']), (3, 4))
        self.assertEqual(result['model_errors'], [])
        second = self.sent[1]['messages']
        receipts = [m for m in second if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in receipts], ['one', 'two'])
        self.assertEqual(json.loads(receipts[0]['content'])['status'], 'TOOL_ERROR')
        self.assertIn('before', receipts[1]['content'])
        for previous, current in zip(self.sent, self.sent[1:]):
            self.assertEqual(previous['messages'][0], current['messages'][0])
            prefix = previous['messages'][:-1]
            self.assertEqual(prefix, current['messages'][:len(prefix)])
        self.assertEqual(second[-1]['role'], 'system')
        self.assertIsNone(result['usage'][0])
        self.assertFalse(result['repair_validated'])

    def test_invalid_read_group_and_mixed_mutation_execute_nothing(self):
        for index, group in enumerate([
            [call('read', {'path': 'value.txt'}, 'a'), call('read', {'start': True}, 'b')],
            [call('read', {'path': 'value.txt'}, 'a'), call('edit', {'path': 'value.txt', 'search': 'before', 'replace': 'after'}, 'b')]
        ]):
            result = self.http_run([response(*group), response(call('finish', {'summary': 'unverified'}, 'end'))],
                                   out=self.root / str(index))
            self.assertEqual(result['tool_calls'], 1)
            self.assertEqual(result['changed_files'], [])
            receipts = [m for m in self.sent[1]['messages'] if m['role'] == 'tool']
            self.assertEqual([m['tool_call_id'] for m in receipts], ['a', 'b'])
            self.assertTrue(all(not json.loads(m['content'])['executed'] for m in receipts))

    def test_incomplete_read_preserves_raw_fragment_and_never_executes_or_retries(self):
        fragment = b'{"choices":[{"message":{"tool_calls":[' + '中文'.encode()[:-1]
        class BrokenBody(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(fragment, 100)
        result = self.http_run([BrokenBody()])
        self.assertEqual((result['status'], result['api_calls'], result['tool_calls']), ('Error', 1, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(result['usage'], [None])
        self.assertEqual(read_json(self.out / 'model/0001/error.json')['error_type'], 'IncompleteRead')
        self.assertEqual((self.out / 'model/0001/partial-response.bin').read_bytes(), fragment)
        with self.assertRaises(ValueError):
            resume(self.out, user_action_done=True)

    def test_wait_requires_ack_and_remaining_allowance_not_reset(self):
        result = solve(self.repo, 'offline', self.out, model_protocol='tool-calls', max_steps=3, closing_requests=1,
            responses=[response(call('edit', {'path': 'value.txt', 'search': 'before', 'replace': 'after'}, 'edit')),
                       response(call('request_user_action', {'reason': 'Agreement', 'request': 'Handle locally'}, 'wait')),
                       response(call('finish', {'summary': 'offline, business unverified'}, 'finish'))])
        self.assertEqual((result['status'], result['model_calls']), ('Paused', 2))
        old = (self.out / 'result.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'user-action-done'):
            resume(self.out)
        self.assertEqual((self.out / 'result.json').read_bytes(), old)
        result = resume(self.out, user_action_done=True)
        self.assertEqual((result['status'], result['model_calls'], result['tool_calls']), ('Submitted', 3, 3))
        self.assertEqual((self.out / 'continuations/0001/result.json').read_bytes(), old)
        payload = read_json(self.out / 'model/0003/request.json')
        self.assertTrue(any('Operator confirmed' in (m.get('content') or '') for m in payload))
        self.assertEqual((self.out / 'workspace/value.txt').read_text(), 'after')
        self.assertEqual((self.repo / 'value.txt').read_text(), 'before')

    def test_http_workbench_pause_resume_same_cli_task_export_and_duplicate_click(self):
        fixtures = self.root / 'responses.json'
        save_json(fixtures, [
            response(call('request_user_action', {'reason': '目标应用内首次隐私协议',
                'request': '请阅读并在本地应用处理当前协议；未处理请保持暂停', 'unverified': ['业务']}, 'wait')),
            response(call('read', {'path': 'value.txt'}, 'read')),
            response(call('finish', {'summary': '零API接口验证，业务未验'}, 'end'))])
        app = create_app(self.root / 'ui')
        manager = app.state.manager
        def wait_result(client, key):
            for _ in range(200):
                row = client.get('/api/runs/' + key).json()
                if row['status'] not in ('Running', 'Stopping'):
                    return row
                time.sleep(.1)
            self.fail('CLI did not complete')
        try:
            with TestClient(app, base_url='http://127.0.0.1', headers={'Origin': 'http://127.0.0.1'}) as client:
                started = client.post('/api/runs', json={'repo': str(self.repo), 'task': 'offline task',
                    'responses': str(fixtures), 'max_steps': 3})
                self.assertEqual(started.status_code, 200, started.text)
                key = started.json()['id']
                row = wait_result(client, key)
                self.assertEqual(row['status'], 'Paused')
                self.assertTrue(row['can_resume'])
                self.assertEqual(row['result']['waiting_user']['reason'], '目标应用内首次隐私协议')
                self.assertEqual(row['result']['tool_calls'], 1)
                launch = read_json(Path(row['run']) / 'launch.json')
                self.assertIn('INSIDE the target application', launch['instructions'])
                self.assertIn('Authorization to test, repair, install or operate the device is NOT consent',
                              launch['instructions'])
                self.assertIn('do not replay an acceptance click', launch['instructions'])
                # HTTP owns the same lock; no second process is dispatched on a repeated click.
                first = client.post('/api/runs/' + key + '/resume', json={'waiting_tool': 1})
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(client.post('/api/runs/' + key + '/resume', json={'waiting_tool': 1}).status_code, 409)
                row = wait_result(client, key)
                self.assertEqual(row['status'], 'Submitted')
                self.assertEqual((row['result']['model_calls'], row['result']['api_calls']), (3, 0))
                self.assertEqual(len(manager.rows), 1)
                continued = read_json(Path(row['run']) / 'model/0002/request.json')
                self.assertTrue(any('Operator confirmed' in (m.get('content') or '') for m in continued))
                events = [json.loads(line) for line in
                          (Path(row['run']) / 'tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
                self.assertEqual([e['action']['action'] for e in events], ['request_user_action', 'read', 'finish'])
                export = client.post('/api/runs/' + key + '/export', json={})
                self.assertEqual(export.status_code, 200, export.text)
                self.assertEqual(export.json()['independent']['status'], 'NOT_RUN')
                self.assertEqual(client.get('/api/runs/' + key + '/summary').status_code, 200)
        finally:
            manager.close()
            for child in manager.children.values():
                child.wait(timeout=15)

    def test_stale_confirmation_cannot_acknowledge_a_later_wait(self):
        from dfx_agent.workbench import Workbench
        fixtures = [response(call('request_user_action', {'reason': 'first', 'request': 'first action'}, 'a')),
                    response(call('request_user_action', {'reason': 'second', 'request': 'different action'}, 'b')),
                    response(call('finish', {'summary': 'offline'}, 'end'))]
        solve(self.repo, 'offline', self.out, responses=fixtures, model_protocol='tool-calls')
        resume(self.out, user_action_done=True)
        manager = Workbench(self.root / 'ui')
        manager.rows.append({'id': 'local', 'run': str(self.out), 'job': str(self.root), 'mode': 'fixture'})
        from fastapi import HTTPException
        with patch('dfx_agent.workbench.subprocess.Popen', side_effect=AssertionError('No dispatch')):
            with self.assertRaises(HTTPException) as caught:
                manager.resume('local', waiting_tool=1)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(read_json(self.out / 'result.json')['waiting_user']['reason'], 'second')

    def test_failed_resume_worker_is_visible_without_overwriting_paused_result(self):
        from dfx_agent.workbench import Workbench
        solve(self.repo, 'offline', self.out, model_protocol='tool-calls', responses=[
            response(call('request_user_action', {'reason': 'environment', 'request': 'handle locally'}, 'wait'))])
        before = (self.out / 'result.json').read_bytes()
        job = self.root / 'job'
        job.mkdir()
        save_json(job / 'resume-exit.json', {'returncode': 2})
        (job / 'worker.log').write_text('Permission denied: local/path', encoding='utf-8')
        manager = Workbench(self.root / 'ui')
        manager.rows.append({'id': 'local', 'run': str(self.out), 'job': str(job), 'mode': 'fixture',
                             'worker_receipt': str(job / 'resume-exit.json')})
        detail = manager.detail('local')
        self.assertEqual(detail['status'], 'Paused')
        self.assertIn('Permission denied: local/path', detail['startup_error'])
        self.assertEqual((self.out / 'result.json').read_bytes(), before)
