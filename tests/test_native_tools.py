"""Offline integration: real HTTP client/mini/tools, only HTTP responses are substituted."""
import contextlib
import io
import json
import zipfile
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from dfx_agent.agent import solve
from dfx_agent.cli import main
from dfx_agent.model import DeepSeekModel, MiniModel, ModelError
from dfx_agent.tool_schema import tool_definitions
from dfx_agent.workspace import save_json


def call(name='read', arguments=None, identity='call_1'):
    return {'id': identity, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)}}


def envelope(calls=None, reason='tool_calls', content=None, usage=True, reasoning='离线协议夹具'):
    message = {'role': 'assistant', 'content': content, 'reasoning_content': reasoning}
    if calls is not None:
        message['tool_calls'] = calls
    data = {'id': 'offline-http-response', 'model': 'deepseek-flash',
            'choices': [{'finish_reason': reason, 'message': message}]}
    if usage:
        data['usage'] = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
    return data


def finish(identity='done'):
    return envelope([call('finish', {'summary': 'Offline mock checked; not a real model result',
                                     'unverified': ['real model', 'device']}, identity)])


class NativeToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / '中文 project'
        self.repo.mkdir()
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        (self.repo / 'check.py').write_text('from calc import add\nassert add(2, 3) == 5\nprint("checked")\n',
                                         encoding='utf-8')
        self.scope = self.root / 'scope.json'
        save_json(self.scope, {'repo': str(self.repo), 'outbound_approved': True,
                               'read_only': ['check.py'], 'exclude': ['oracle.txt']})
        self.command = "& '" + sys.executable.replace("'", "''") + "' check.py; exit $LASTEXITCODE"
        self.sent = []

    def run_http(self, responses, name='run', cli=False, **kwargs):
        out = self.root / name
        remaining = iter(responses)
        self.sent = []

        def respond(request, timeout):
            self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
            self.assertEqual(timeout, 180 if cli else 60)
            self.sent.append(json.loads(request.data))
            response = next(remaining)  # unexpected calls fail, no real network fallback
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response, ensure_ascii=False).encode('utf-8'))

        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE_NATIVE_TEST_KEY'}), \
             patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = respond
            if cli:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(['solve', '--repo', str(self.repo), '--out', str(out),
                                 '--task', 'Fix arithmetic and verify it',
                                 '--provider', 'deepseek', '--model', 'deepseek-flash',
                                 '--model-timeout', '180',
                                 '--scope', str(self.scope), '--model-protocol', 'tool-calls'])
                self.assertEqual(code, 0)
                result = self.read(out / 'result.json')
            else:
                result = solve(self.repo, 'Fix arithmetic and verify it', out,
                               provider=DeepSeekModel('deepseek-flash'), scope=self.scope,
                               model_protocol='tool-calls', **kwargs)
        self.assertEqual(result['api_calls'], len(self.sent))
        return result, out

    def read(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_screen_recovery_through_real_native_client_mini_and_shared_driver(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'sdk'), 'device': 'fixture',
                         'screen_lock_profile': 'swipe_only'})
        state = {'locked': True, 'swipes': 0, 'wakes': 0}
        def hdc(argv, cwd, env, timeout, log):
            args = argv[3:]
            output = 'success'
            if 'dumpLayout' in args:
                output = 'DumpLayout saved to: fixture'
            elif args[:2] == ['file', 'recv']:
                tree = {'children': [{'attributes': {'bundleName': 'com.ohos.sceneboard',
                    'id': 'ScreenLockRootComponent', 'bounds': '[0,0][100,200]', 'text': 'SECRET_NOTIFICATION'}}]} if state['locked'] else {
                    'children': [{'attributes': {'bundleName': 'example.app', 'text': '首页'}}]}
                save_json(Path(args[-1]), tree)
            elif args == ['shell', 'power-shell', 'wakeup']:
                state['wakes'] += 1
            elif 'swipe' in args:
                state['swipes'] += 1
                state['locked'] = False
            elif args[:3] == ['shell', 'aa', 'start']:
                output = 'start ability successfully.'
            else:
                raise AssertionError(args)
            log.write_text(output, encoding='utf-8')
            return {'returncode': 0}
        responses = [
            envelope([call('harmony', {'operation': 'start', 'bundle': 'example.app', 'module': 'entry', 'ability': 'MainAbility'}, 'start')]),
            envelope([call('harmony', {'operation': 'layout'}, 'locked')]),
            envelope([call('harmony', {'operation': 'screen', 'mode': 'unlock'}, 'unlock')]),
            envelope([call('harmony', {'operation': 'layout'}, 'observed')]), finish()]
        with patch('dfx_agent.harmony_device.execute', side_effect=hdc):
            result, out = self.run_http(responses, host=host, allow_device=True, allow_screen_unlock=True)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual((state['wakes'], state['swipes']), (1, 1))
        events = self.events(out)
        self.assertEqual(events[1]['result']['status'], 'SCREEN_LOCKED')
        self.assertEqual(events[2]['result']['screen_state'], 'UNLOCKED')
        self.assertEqual(events[3]['result']['status'], 'OBSERVED')
        self.assertNotIn('SECRET_NOTIFICATION', json.dumps(self.sent))
        for file in (out / 'tools').rglob('*'):
            if file.is_file():
                self.assertNotIn('SECRET_NOTIFICATION', file.read_text(encoding='utf-8'))

    def events(self, out):
        path = out / 'tools/events.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def test_real_device_block_stops_before_next_request_with_matching_receipt(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'sdk'), 'device': 'fixture'})
        def disconnected(argv, cwd, env, timeout, log):
            log.write_text('[Fail] device disconnected', encoding='utf-8')
            return {'returncode': 0}
        with patch('dfx_agent.harmony_device.execute', side_effect=disconnected) as device:
            result, out = self.run_http([
                envelope([call('harmony', {'operation': 'start', 'bundle': 'example.app',
                    'module': 'entry', 'ability': 'EntryAbility'}, 'blocked-call')])
            ], host=host, allow_device=True)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['api_calls'], 1)
        device.assert_called_once()
        messages = self.read(out / 'trajectory.json')['messages']
        self.assertEqual(messages[-2]['tool_call_id'], 'blocked-call')
        self.assertEqual(json.loads(messages[-2]['content'])['status'], 'BLOCKED')
        self.assertEqual(messages[-1]['extra']['exit_status'], 'BLOCKED')
        self.assertFalse(result['repair_validated'])

    def test_install_without_build_is_correctable_without_device_dispatch(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'sdk'), 'device': 'fixture'})
        with patch('dfx_agent.harmony_device.execute', side_effect=AssertionError('No HDC')):
            result, out = self.run_http([
                envelope([call('harmony', {'operation': 'install', 'artifact': 'entry/app.hap'}, 'premature')]),
                finish()
            ], host=host, allow_device=True)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['api_calls'], 2)
        self.assertEqual(self.events(out)[0]['result']['status'], 'TOOL_ERROR')
        self.assertIn('Build this HAP', self.sent[1]['messages'][-2]['content'])

    def test_missing_window_reaches_native_mini_then_explicit_recovery_or_finish(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'absent-sdk'), 'device': 'fixture'})
        start = {'operation': 'start', 'bundle': 'example.app', 'module': 'entry', 'ability': 'EntryAbility'}
        for recover in (False, True):
            with self.subTest(recover=recover):
                starts, commands = [], []
                def hdc(argv, cwd, env, timeout, log):
                    args = argv[3:]
                    commands.append(args)
                    if args[:3] == ['shell', 'aa', 'start']:
                        starts.append(args)
                        text = 'start ability successfully'
                    elif args[:3] == ['shell', 'uitest', 'dumpLayout']:
                        text = 'DumpLayout saved to: fixture'
                    elif args[:2] == ['file', 'recv']:
                        window = {'attributes': {'bundleName': 'example.app', 'text': 'Target page'}} if len(starts) == 2 else {
                            'attributes': {'bundleName': 'foreign.app', 'text': 'FOREIGN_WINDOW_BODY'}}
                        Path(args[-1]).write_text(json.dumps({'children': [window]}), encoding='utf-8')
                        text = 'received'
                    else:
                        raise AssertionError(args)
                    log.write_text(text, encoding='utf-8')
                    return {'returncode': 0}
                replies = [envelope([call('harmony', start, 'start')]),
                           envelope([call('harmony', {'operation': 'layout'}, 'missing')])]
                if recover:
                    replies.extend([envelope([call('harmony', start, 'restart')]),
                                    envelope([call('harmony', {'operation': 'layout'}, 'observed')])])
                replies.append(envelope([call('finish', {'summary': 'Offline fixture only',
                    'unverified': ['business behavior']}, 'done')]))
                with patch('dfx_agent.harmony_device.execute', side_effect=hdc):
                    result, out = self.run_http(replies, name='recover' if recover else 'report',
                                                host=host, allow_device=True)
                self.assertEqual(result['status'], 'Submitted')
                self.assertEqual(len(starts), 2 if recover else 1)
                self.assertEqual(len(commands), 6 if recover else 3)
                self.assertEqual(result['api_calls'], 5 if recover else 3)  # Local HTTP substitute only.
                receipt = self.sent[2]['messages'][-2]
                self.assertEqual(receipt['tool_call_id'], 'missing')
                self.assertEqual(json.loads(receipt['content'])['status'], 'NOT_VISIBLE')
                self.assertNotIn('FOREIGN_WINDOW_BODY', json.dumps(self.sent))
                self.assertNotIn('foreign.app', json.dumps(self.sent))
                self.assertFalse(result['repair_validated'])
                self.assertFalse((out/'tools/started.json').exists())
                if recover:
                    self.assertEqual(self.events(out)[-2]['result']['nodes'][0]['text'], 'Target page')

    def test_explicit_navigation_roundtrip_does_not_retry_or_assert_recovery(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'absent-sdk'), 'device': 'fixture'})
        for recovered in (True, False):
            with self.subTest(recovered=recovered):
                keys = []
                def hdc(argv, cwd, env, timeout, log):
                    args = argv[3:]
                    if args[:3] == ['shell', 'aa', 'start']:
                        text = 'start ability successfully'
                    elif args[:3] == ['shell', 'uitest', 'dumpLayout']:
                        self.assertNotIn('-b', args)
                        text = 'DumpLayout saved to: fixture'
                    elif args[:2] == ['file', 'recv']:
                        window = {'attributes': {'bundleName': 'example.app', 'text': 'Target page'}} if keys and recovered else {
                            'attributes': {'bundleName': 'foreign.app', 'text': 'FOREIGN_BODY'}}
                        Path(args[-1]).write_text(json.dumps({'children': [window]}), encoding='utf-8')
                        text = 'received'
                    elif args == ['shell', 'uitest uiInput keyEvent Back']:
                        keys.append('Back')
                        text = 'No Error'
                    else:
                        raise AssertionError(args)
                    log.write_text(text, encoding='utf-8')
                    return {'returncode': 0}
                replies = [envelope([call('harmony', action, identity)]) for identity, action in [
                    ('start', {'operation': 'start', 'bundle': 'example.app', 'module': 'entry', 'ability': 'EntryAbility'}),
                    ('missing', {'operation': 'layout'}),
                    ('back', {'operation': 'navigate', 'key': 'Back'}),
                    ('check', {'operation': 'layout'})]] + [finish()]
                with patch('dfx_agent.harmony_device.execute', side_effect=hdc):
                    result, out = self.run_http(replies, name='nav-' + str(recovered), host=host,
                                                allow_device=True, allow_system_navigation=True)
                self.assertEqual((result['status'], result['api_calls']), ('Submitted', 5))
                self.assertEqual(keys, ['Back'])
                events = self.events(out)
                self.assertEqual(events[1]['result']['status'], 'NOT_VISIBLE')
                self.assertEqual(events[2]['result']['target_state_after'], 'NOT_CHECKED')
                self.assertEqual(events[3]['result']['status'], 'OBSERVED' if recovered else 'NOT_VISIBLE')
                self.assertEqual(self.sent[3]['messages'][-2]['tool_call_id'], 'back')
                self.assertNotIn('FOREIGN_BODY', json.dumps(self.sent))
                self.assertNotIn('foreign.app', json.dumps(self.sent))
                self.assertTrue(self.read(out / 'launch.json')['options']['allow_system_navigation'])
                self.assertFalse(result['repair_validated'])

    def test_cli_client_mini_real_commands_revision_and_finish_receipt(self):
        chinese = '中文 "引号"\n第二行\\路径\n'
        replies = [
            envelope([call('read', {}, 'list')]),
            envelope([call('exec', {'command': self.command}, 'failing-check')]),
            envelope([call('edit', {'path': 'calc.py', 'search': 'a - b', 'replace': 'a + b'}, 'edit')]),
            envelope([call('exec', {'command': self.command}, 'passing-check')]),
            envelope([call('write', {'path': '中文.json', 'content': json.dumps({'说明': chinese}, ensure_ascii=False)}, 'write')]),
            envelope([call('read', {'path': '中文.json'}, 'read')]),
            finish(),
        ]
        result, out = self.run_http(replies, cli=True)
        self.assertEqual((result['status'], result['model_calls'], result['tool_calls']), ('Submitted', 7, 7))
        self.assertEqual(result['model_protocol'], 'tool-calls')
        self.assertEqual(result['model_errors'], [])
        self.assertFalse(result['repair_validated'])
        self.assertEqual(result['independent_verification'], 'NOT_RUN')
        self.assertEqual(self.read(out / 'workspace/中文.json'), {'说明': chinese})
        self.assertIn('a - b', (self.repo / 'calc.py').read_text(encoding='utf-8'))
        self.assertIn('a + b', (out / 'workspace/calc.py').read_text(encoding='utf-8'))
        events = self.events(out)
        self.assertNotEqual(events[1]['result']['returncode'], 0)
        self.assertEqual(events[3]['result']['returncode'], 0)
        self.assertIn('AssertionError', self.sent[2]['messages'][-2]['content'])
        for n, request in enumerate(self.sent, 1):
            self.assertNotIn('response_format', request)
            self.assertNotIn('parallel_tool_calls', request)
            self.assertEqual(request['tool_choice'], 'auto')
            self.assertEqual(request['thinking'], {'type': 'enabled'})
            self.assertEqual(request['reasoning_effort'], 'high')
            self.assertFalse(request['stream'])
            self.assertEqual(request['max_tokens'], 4096)
            self.assertEqual(request, self.read(out / f'model/{n:04d}/request.json'))
            self.assertNotIn('FAKE_NATIVE_TEST_KEY', json.dumps(request))
            for message in request['messages']:
                self.assertNotIn('extra', message)
            if n > 1:
                assistant, observation = request['messages'][-3:-1]
                self.assertEqual(assistant, replies[n - 2]['choices'][0]['message'])
                self.assertEqual(observation['role'], 'tool')
                self.assertEqual(observation['tool_call_id'], assistant['tool_calls'][0]['id'])
        messages = self.read(out / 'trajectory.json')['messages']
        self.assertEqual([m['role'] for m in messages[-3:]], ['assistant', 'tool', 'exit'])
        self.assertEqual(messages[-2]['tool_call_id'], 'done')
        self.assertEqual(json.loads(messages[-2]['content'])['status'], 'SUBMITTED')
        self.assertEqual(len(self.sent), 7)  # finish needs no extra generation
        self.assertIn('tool-calls', (out / 'report.md').read_text(encoding='utf-8'))
        self.assertEqual(result['model_configuration']['reasoning_effort'], 'high')
        self.assertEqual(result['model_configuration']['timeout_seconds'], 180)
        self.assertEqual(self.read(out / 'model/0001/response.json')['provider']['parameters']['thinking'], 'enabled')

    def test_invalid_arguments_feedback_and_zero_partial_execution(self):
        bad = [
            call('edit', {'path': 'calc.py'}), call('missing_tool'),
            call('read', {'start': True}), call('read', {'action': 'exec'}),
            call('read', {'tool_call_id': 'forged'}), call('finish', {'summary': '  '}),
            call('finish', {'summary': 'x', 'root_cause': {'path': 'a', 'line': 'two'}}),
            call('read', {'source': 'unknown'}),
        ]
        for text in ('{"path":', '[]', '{"path":"calc.py"}<DSML>', 'null'):
            item = call()
            item['function']['arguments'] = text
            bad.append(item)
        for n, item in enumerate(bad):
            with self.subTest(n=n):
                result, out = self.run_http([envelope([item]), finish()], f'bad-{n}')
                self.assertEqual(result['status'], 'Submitted')
                self.assertEqual(result['changed_files'], [])
                self.assertEqual([e['action']['action'] for e in self.events(out)], ['finish'])
                self.assertEqual(result['model_errors'][0]['kind'], 'invalid_arguments')
                messages = self.sent[1]['messages'][:-1]
                self.assertEqual(messages[-2], envelope([item])['choices'][0]['message'])
                self.assertEqual(messages[-1]['tool_call_id'], item['id'])
                self.assertFalse(json.loads(messages[-1]['content'])['executed'])
                self.assertEqual(self.read(out / 'model/0001/response.json')['body']['message']['tool_calls'][0], item)

    def test_multiple_calls_including_finish_are_all_rejected(self):
        calls = [call('finish', {'summary': 'must not end'}, 'first'),
                 call('edit', {'path': 'calc.py', 'search': 'a - b', 'replace': 'a + b'}, 'second')]
        result, out = self.run_http([envelope(calls), finish()])
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['model_errors'][0]['kind'], 'multiple_calls')
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual(result['changed_files'], [])
        feedback = self.sent[1]['messages'][-3:-1]
        self.assertEqual([m['tool_call_id'] for m in feedback], ['first', 'second'])
        self.assertTrue(all(not json.loads(m['content'])['executed'] for m in feedback))

    def test_malformed_ids_envelopes_and_finish_conflicts_stop(self):
        missing = call(); missing.pop('id')
        duplicate = [call(identity='same'), call(identity='same')]
        nonstring = call(); nonstring['function']['arguments'] = {}
        wrongtype = call(); wrongtype['type'] = 'other'
        replies = [envelope([missing]), envelope(duplicate), envelope([nonstring]),
                   envelope([wrongtype]), envelope([call()], reason='stop'), envelope([]),
                   envelope([call()], reason='length'), envelope([call()], reason='content_filter'),
                   envelope([call()], reason='aborted'),
                   {'model': 'deepseek-flash', 'choices': [{'message': [], 'finish_reason': 'tool_calls'}]},
                   {'model': 'deepseek-flash', 'choices': []}]
        for n, reply in enumerate(replies):
            with self.subTest(n=n):
                result, out = self.run_http([reply, finish()], f'stop-{n}')
                self.assertEqual(result['status'], 'Error')
                self.assertEqual((len(self.sent), result['tool_calls']), (1, 0))
                self.assertEqual(result['changed_files'], [])
                self.assertTrue((out / 'model/0001/error.json').exists())
                self.assertTrue((out / 'trajectory.json').exists())

    def test_text_does_not_submit_and_existing_format_step_limits_apply(self):
        text = envelope(reason='stop', content='Done, fixed everything.')
        result, out = self.run_http([text, finish()], 'text')
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual(result['model_errors'], [])
        self.assertEqual(self.sent[1]['messages'][-2]['role'], 'assistant')
        self.assertEqual(self.sent[1]['messages'][-2]['content'], 'Done, fixed everything.')
        blank = envelope(reason='stop', content='')
        result, out = self.run_http([blank, blank, blank, finish()], 'repeated')
        self.assertEqual(result['status'], 'RepeatedFormatError')
        self.assertEqual((len(self.sent), result['tool_calls']), (3, 0))
        result, out = self.run_http([text, finish()], 'steps', max_steps=1)
        self.assertEqual(result['status'], 'LimitsExceeded')
        self.assertEqual(len(self.sent), 1)

    def test_thinking_retained_across_prose_rejected_calls_and_real_tools(self):
        replies = [
            envelope(reason='stop', content='先检查工作副本。', reasoning='夹具：说明'),
            envelope([call('read', {'start': True}, 'invalid')], reasoning='夹具：参数错误'),
            envelope([call('read', {}, 'a'), call('read', {}, 'b')], reasoning='夹具：多调用'),
            envelope([call('read', {'path': 'calc.py'}, 'read')], reasoning='夹具：读源码'),
            envelope([call('exec', {'command': "Write-Output '真实本地输出'"}, 'exec')], reasoning='夹具：命令'),
            finish(),
        ]
        result, out = self.run_http(replies, 'thinking-history')
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['tool_calls'], 5)
        for number, request in enumerate(self.sent):
            past = [m for m in request['messages'] if m['role'] == 'assistant']
            self.assertEqual([m['reasoning_content'] for m in past],
                             [r['choices'][0]['message']['reasoning_content'] for r in replies[:number]])
            self.assertEqual(request['thinking'], {'type': 'enabled'})
            self.assertEqual(request['reasoning_effort'], 'high')
        self.assertIn('真实本地输出', self.sent[-1]['messages'][-2]['content'])
        self.assertEqual(self.events(out)[2]['action']['tool_call_id'], 'read')

    def test_reasoning_counts_toward_history_limit_and_explicit_disabled_is_recorded(self):
        from minisweagent.exceptions import InterruptAgentFlow
        adapter = MiniModel(self.root / 'reasoning-limit', responses=[], protocol='tool-calls',
                            max_context_chars=200)
        with self.assertRaises(InterruptAgentFlow):
            adapter.query([{'role': 'assistant', 'content': '', 'reasoning_content': 'r' * 500}])
        self.assertEqual(adapter.calls, 0)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE'}):
            provider = DeepSeekModel('deepseek-flash', thinking='disabled')
            payload = provider.payload([], tool_definitions())
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertEqual(payload['tool_choice'], 'required')
            self.assertNotIn('reasoning_effort', payload)
            self.assertIsNone(provider.configuration()['reasoning_effort'])

    def test_thinking_mini_build_inspect_install_and_saved_layout_round_trip(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'fixture-sdk'), 'device': 'fixture-device'})
        tree = {'attributes': {}, 'children': [
            {'attributes': {'text': f'合成按钮{i}', 'type': 'Button', 'bounds': '[0,0][10,10]',
                            'enabled': 'true', 'clickable': 'true', 'backgroundColor': 'style' * 100}}
            for i in range(50)]}
        tree = {'attributes': {}, 'children': [{'attributes': {'bundleName': 'example.app'}, 'children': tree['children']}]}
        def compiler(argv, cwd, env, timeout, log):
            artifact = cwd / 'entry/app.hap'
            artifact.parent.mkdir()
            with zipfile.ZipFile(artifact, 'w') as archive:
                archive.writestr('module.json', json.dumps({'app': {'bundleName': 'example.app'}, 'module': {'name': 'entry'}}))
            log.write_text('fixture warning\n' + 'fixture progress\n' * 1000, encoding='utf-8')
            return {'returncode': 0, 'seconds': 0.1}
        def hdc(argv, cwd, env, timeout, log):
            args = argv[3:]
            if args[0] == 'install':
                output = 'install bundle successfully'
            elif args[:3] == ['shell', 'aa', 'start']:
                output = 'start ability successfully'
            elif args[:3] == ['shell', 'uitest', 'dumpLayout']:
                output = 'DumpLayout saved to: fixture'
            elif args[:2] == ['file', 'recv']:
                Path(args[3]).write_text(json.dumps(tree, ensure_ascii=False), encoding='utf-8')
                output = 'received'
            else:
                raise AssertionError(args)
            log.write_text(output, encoding='utf-8')
            return {'returncode': 0}
        replies = [
            envelope([call('harmony', {'operation': 'build', 'module': 'entry', 'product': 'default',
                                      'target': 'default', 'artifact': 'entry/app.hap'}, 'build')]),
            envelope([call('exec', {'command': 'Get-ChildItem entry | Select-Object Name'}, 'inspect')]),
            envelope([call('harmony', {'operation': 'install', 'artifact': 'entry/app.hap'}, 'install')]),
            envelope([call('harmony', {'operation': 'start', 'bundle': 'example.app',
                                      'module': 'entry', 'ability': 'EntryAbility'}, 'start')]),
            envelope([call('harmony', {'operation': 'layout', 'limit': 10}, 'layout')]),
            envelope([call('harmony', {'operation': 'layout', 'limit': 10, 'offset': 10,
                                      'saved_layout': 'harmony-0005/layout-readable.json'}, 'page')]),
            finish(),
        ]
        with patch('dfx_agent.harmony_tools.execute', side_effect=compiler) as builds, \
             patch('dfx_agent.harmony_device.execute', side_effect=hdc) as device, \
             patch('dfx_agent.harmony_tools.time.sleep'):
            result, out = self.run_http(replies, 'shared-round-trip', host=host, allow_device=True)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['model_errors'], [])
        self.assertEqual(builds.call_count, 1)
        self.assertEqual(device.call_count, 4)
        observed_build = json.loads(self.sent[1]['messages'][-2]['content'])
        self.assertEqual(observed_build['artifact'], 'entry/app.hap')
        self.assertIn('fixture warning', observed_build['diagnostics'])
        self.assertLess(len(self.sent[1]['messages'][-2]['content']), 3000)
        page = json.loads(self.sent[6]['messages'][-2]['content'])
        self.assertEqual(page['nodes'][0]['text'], '合成按钮10')
        self.assertFalse(page['fresh_capture'])
        self.assertFalse(result['repair_validated'])

    def test_tool_runtime_error_and_scope_are_real_observations(self):
        (self.repo / 'oracle.txt').write_text('hidden-answer')
        replies = [
            envelope([call('read', {'path': 'missing'}, 'missing')]),
            envelope([call('edit', {'path': 'check.py', 'search': '== 5', 'replace': '== 9'}, 'readonly')]),
            envelope([call('exec', {'command': "if ($env:DEEPSEEK_API_KEY) { exit 9 }; Write-Output 'no key'"}, 'env')]),
            finish(),
        ]
        result, out = self.run_http(replies)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['model_errors'], [])
        self.assertEqual(self.events(out)[0]['result']['status'], 'TOOL_ERROR')
        self.assertIn('read-only', self.sent[2]['messages'][-2]['content'])
        self.assertEqual(self.events(out)[2]['result']['returncode'], 0)
        self.assertFalse((out / 'workspace/oracle.txt').exists())
        self.assertNotIn('FAKE_NATIVE_TEST_KEY', (out / 'tools/events.jsonl').read_text(encoding='utf-8'))

    def test_missing_usage_is_unknown_and_does_not_stop(self):
        reply = finish(); reply.pop('usage')
        result, out = self.run_http([reply])
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['usage'], [None])
        self.assertIsNone(result['actual_cost'])

    def test_http_read_failure_model_mismatch_stop_without_retry(self):
        wrong_model = finish(); wrong_model['model'] = 'unexpected'
        for n, problem in enumerate([urllib.error.HTTPError('https://api.deepseek.com', 503, 'offline', {}, None),
                                     urllib.error.URLError('offline interrupted'), TimeoutError('offline'),
                                     OSError('read interrupted'), wrong_model]):
            result, out = self.run_http([problem, finish()], f'network-{n}')
            self.assertEqual(result['status'], 'Error')
            self.assertEqual((len(self.sent), result['tool_calls']), (1, 0))
            self.assertTrue((out / 'model/0001/error.json').exists())
        # An actual read() exception, after open(), must also stop without replay.
        class BrokenRead(io.BytesIO):
            def read(self, *args):
                raise OSError('response body interrupted')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE'}), \
             patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = BrokenRead()
            result = solve(self.repo, 'Task', self.root / 'read-error', provider=DeepSeekModel('deepseek-flash'),
                           scope=self.scope, model_protocol='tool-calls')
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual(result['tool_calls'], 0)
            self.assertEqual(result['status'], 'Error')

    def test_tool_call_fields_count_toward_context_without_old_byte_gate(self):
        # The original role/content projection would omit this long argument.
        history = [{'role': 'assistant', 'content': None, 'tool_calls': [
            call('write', {'path': 'x', 'content': 'x' * 500})]}]
        adapter = MiniModel(self.root / 'adapter', responses=[], protocol='tool-calls', max_context_chars=200)
        from minisweagent.exceptions import InterruptAgentFlow
        with self.assertRaises(InterruptAgentFlow) as caught:
            adapter.query(history)
        self.assertEqual(caught.exception.messages[-1]['extra']['exit_status'], 'ContextLimit')
        self.assertEqual(adapter.calls, 0)
        result, _ = self.run_http([finish()], 'context', max_context_chars=1)
        self.assertEqual((result['status'], len(self.sent)), ('ContextLimit', 0))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FAKE'}), \
             patch('urllib.request.build_opener') as opener:
            provider = DeepSeekModel('deepseek-flash')
            payload = provider.payload([{'role': 'user', 'content': 'x' * 1_000_000}], tool_definitions())
            opener.return_value.open.return_value = io.BytesIO(json.dumps({
                'model': 'deepseek-flash', 'choices': [{'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'mock'}}]}).encode())
            provider.query_tool_messages(payload)
            opener.return_value.open.assert_called_once()

    def test_harmony_schema_mapping_and_device_pause_without_dispatch(self):
        host = self.root / 'host.json'
        save_json(host, {'deveco': str(self.root / 'missing-sdk'), 'device': 'offline-only'})
        replies = [envelope([call('harmony', {'operation': 'info'}, 'info')]),
                   envelope([call('harmony', {'operation': 'build'}, 'bad-build')]),
                   envelope([call('harmony', {'operation': 'tap', 'x': 1, 'y': 2}, 'tap')]),
                   finish()]
        with patch('dfx_agent.harmony_tools.execute', side_effect=AssertionError('No SDK')), \
             patch('dfx_agent.harmony_device.execute', side_effect=AssertionError('No HDC')):
            result, out = self.run_http(replies, host=host)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['tool_calls'], 3)
        self.assertEqual(self.events(out)[1]['result']['status'], 'TOOL_ERROR')
        self.assertIn('missing', self.sent[2]['messages'][-2]['content'])
        self.assertEqual([t['function']['name'] for t in self.sent[0]['tools']][-1], 'harmony')
        result, _ = self.run_http([envelope([call('harmony', {'operation': 'info'})]), finish()], 'no-host')
        self.assertEqual(result['model_errors'][0]['kind'], 'invalid_arguments')
        self.assertNotIn('harmony', [t['function']['name'] for t in self.sent[0]['tools']])

    def test_observation_mismatch_is_not_padded_or_relabelled(self):
        model = MiniModel(self.root / 'adapter', responses=[], protocol='tool-calls')
        with self.assertRaisesRegex(ValueError, 'one observation'):
            model.format_observation_messages({'extra': {'actions': [{'tool_call_id': 'x'}]}}, [])
        with self.assertRaises(KeyError):
            model.format_observation_messages({'extra': {'actions': [{}]}}, [{}])

    def test_native_fixture_is_explicit_and_legacy_default_unchanged(self):
        fixture = self.root / 'fixture.json'
        reply = finish()['choices'][0]
        save_json(fixture, [reply])
        out = self.root / 'fixture-run'
        with patch('urllib.request.build_opener', side_effect=AssertionError('No HTTP')), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(['solve', '--repo', str(self.repo), '--out', str(out), '--task', 'Fixture',
                         '--responses', str(fixture), '--model-protocol', 'tool-calls'])
        self.assertEqual(code, 0)
        result = self.read(out / 'result.json')
        self.assertEqual((result['evaluation_scope'], result['api_calls']), ('offline_fixture', 0))
        legacy = solve(self.repo, 'Legacy', self.root / 'legacy',
                       responses=[{'action': 'finish', 'summary': 'legacy', 'tool_call_id': 'not-a-native-call'}])
        self.assertEqual(legacy['model_protocol'], 'json-actions')
        messages = self.read(self.root / 'legacy/trajectory.json')['messages']
        self.assertEqual(messages[-2]['role'], 'assistant')
        self.assertEqual(messages[-1]['role'], 'exit')


if __name__ == '__main__':
    unittest.main()
