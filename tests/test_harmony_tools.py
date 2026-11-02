import contextlib
import io
import json
import zipfile
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent import solve, core_class
from dfx_agent.cli import main
from dfx_agent.harmony_build import build_command, build_environment
from dfx_agent.harmony_device import Blocked, Device
from dfx_agent.harmony_runtime import Device as LegacyDevice
from dfx_agent.harmony_tools import HarmonyTools, load_host
from dfx_agent.tools import ProjectTools
from minisweagent.exceptions import InterruptAgentFlow


class HarmonyToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        core_class(self.root)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'source.ets').write_text('before', encoding='utf-8')
        self.output = self.root / 'tools'
        self.host = {'deveco': str(self.root / 'tool chain'), 'device': 'fixture-only'}
        self.adapter = HarmonyTools(self.repo, self.output, self.host)
        self.adapter.target_bundle = 'example.app'
        self.tools = ProjectTools(self.repo, self.output, harmony=self.adapter)

    def action(self, op, **values):
        return self.tools.execute({'action': 'harmony', 'operation': op, **values})

    def build_action(self):
        return {'module': 'entry', 'product': 'default', 'target': 'default', 'artifact': 'entry/app.hap'}

    def test_disabled_capabilities_never_dispatch(self):
        with patch('dfx_agent.harmony_tools.execute', side_effect=AssertionError('build forbidden')), \
             patch('dfx_agent.harmony_device.execute', side_effect=AssertionError('HDC forbidden')):
            self.assertFalse(self.action('info')['device_enabled'])
            for op in ('start', 'layout', 'faults', 'input'):
                self.assertEqual(self.action(op)['status'], 'TOOL_ERROR')
            self.assertEqual(self.action('dependencies')['status'], 'TOOL_ERROR')

    def test_build_output_must_be_new_and_failure_is_preserved(self):
        artifact = self.repo / 'entry/app.hap'
        artifact.parent.mkdir()
        artifact.write_bytes(b'old')
        def absent(argv, cwd, env, timeout, log):
            log.write_text('no new artifact', encoding='utf-8')
            self.assertEqual(timeout, 600)
            return {'returncode': 0, 'seconds': 0.1}
        with patch('dfx_agent.harmony_tools.execute', side_effect=absent):
            result = self.action('build', **self.build_action())
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual((self.output / 'harmony-0001/previous.hap').read_bytes(), b'old')
        self.assertFalse(self.adapter.built)
        def failed(argv, cwd, env, timeout, log):
            log.write_text('compiler failure', encoding='utf-8')
            return {'returncode': 9, 'seconds': 0.1}
        with patch('dfx_agent.harmony_tools.execute', side_effect=failed):
            self.assertEqual(self.action('build', **self.build_action())['returncode'], 9)

    def test_install_requires_current_build_and_positive_receipt(self):
        self.adapter.allow_device = True
        artifact = self.repo / 'entry/app.hap'
        def build(argv, cwd, env, timeout, log):
            artifact.parent.mkdir(exist_ok=True)
            with zipfile.ZipFile(artifact, 'w') as archive:
                archive.writestr('module.json', json.dumps({'app': {'bundleName': 'example.app'}, 'module': {'name': 'entry'}}))
            log.write_text('fixture build', encoding='utf-8')
            return {'returncode': 0}
        with patch('dfx_agent.harmony_tools.execute', side_effect=build):
            self.assertEqual(self.action('build', **self.build_action())['status'], 'EXECUTED')
        with patch.object(Device, 'command', return_value='install failed') as command:
            with self.assertRaises(InterruptAgentFlow):
                self.action('install', artifact='entry/app.hap')
            self.assertEqual(command.call_count, 1)
        with patch.object(Device, 'command', return_value='install bundle successfully') as command, \
             patch('dfx_agent.harmony_tools.time.sleep') as settle:
            self.assertEqual(self.action('install', artifact='entry/app.hap')['status'], 'EXECUTED')
            command.assert_called_once()
            settle.assert_called_once_with(5)
        self.tools.execute({'action': 'edit', 'path': 'source.ets', 'search': 'before', 'replace': 'after'})
        with patch.object(Device, 'command', side_effect=AssertionError('stale install')):
            self.assertEqual(self.action('install', artifact='entry/app.hap')['status'], 'TOOL_ERROR')

    def test_exec_keeps_build_record_and_timeout_still_stops(self):
        self.adapter.built.add(self.repo / 'entry/app.hap')
        with patch('dfx_agent.tools.execute') as command:
            def local(argv, cwd, env, timeout, log):
                log.write_text('timeout', encoding='utf-8')
                return {'status': 'BLOCKED', 'error': 'timed out'}
            command.side_effect = local
            with self.assertRaises(InterruptAgentFlow):
                self.tools.execute({'action': 'exec', 'command': 'not actually run'})
        self.assertEqual(self.adapter.built, {self.repo / 'entry/app.hap'})
        with patch('dfx_agent.harmony_tools.execute', side_effect=local):
            with self.assertRaises(InterruptAgentFlow):
                self.action('build', **self.build_action())

    def test_real_inspection_command_does_not_force_rebuild(self):
        self.adapter.allow_device = True
        artifact = self.repo / 'entry/app.hap'
        def build(argv, cwd, env, timeout, log):
            artifact.parent.mkdir()
            with zipfile.ZipFile(artifact, 'w') as archive:
                archive.writestr('module.json', json.dumps({'app': {'bundleName': 'example.app'}, 'module': {'name': 'entry'}}))
            log.write_text('fixture build', encoding='utf-8')
            return {'returncode': 0}
        with patch('dfx_agent.harmony_tools.execute', side_effect=build) as compiler:
            self.action('build', **self.build_action())
        command = self.tools.execute({'action': 'exec', 'command': 'Get-ChildItem entry | Select-Object Name'})
        self.assertEqual(command['returncode'], 0)
        self.assertIn('app.hap', command['output'])
        with patch.object(Device, 'command', return_value='install bundle successfully') as install, \
             patch('dfx_agent.harmony_tools.time.sleep'):
            self.assertEqual(self.action('install', artifact='entry/app.hap')['status'], 'EXECUTED')
        self.assertEqual(compiler.call_count, 1)
        install.assert_called_once()
        self.tools.execute({'action': 'edit', 'path': 'source.ets', 'search': 'no-match', 'replace': 'x'})
        self.assertIn(artifact, self.adapter.built)
        self.tools.execute({'action': 'edit', 'path': 'source.ets', 'search': 'before', 'replace': 'changed'})
        self.assertFalse(self.adapter.built)

    def test_compact_build_preserves_failure_diagnostics_and_full_log(self):
        text = 'error: mock ArkTS diagnostic at source.ets:8\n' + ('progress ' * 50 + '\n') * 100 + 'BUILD FAILED\n'
        def build(argv, cwd, env, timeout, log):
            log.write_text(text, encoding='utf-8')
            return {'returncode': 9, 'seconds': 0.5}
        with patch('dfx_agent.harmony_tools.execute', side_effect=build):
            result = self.action('build', **self.build_action())
        self.assertEqual((result['status'], result['returncode']), ('FAILED', 9))
        self.assertIn('source.ets:8', result['diagnostics'][0])
        self.assertNotIn('output', result)
        self.assertLess(len(json.dumps(result)), 4000)
        self.assertTrue(result['truncated'])
        self.assertEqual((self.output / result['command_log']).read_text(encoding='utf-8'), text)
        saved = json.loads((self.output / result['log']).read_text(encoding='utf-8'))
        self.assertEqual(saved['output'], text)

    def test_layout_pages_saved_capture_without_more_hdc_and_retains_raw_fields(self):
        self.adapter.allow_device = True
        tree = {'attributes': {}, 'children': [
            {'attributes': {'text': '门店' + str(i), 'type': 'Button', 'id': str(i),
                            'bounds': '[0,0][100,100]', 'clickable': 'true', 'enabled': 'false',
                            'backgroundImage': 'large-style-' * 100}, 'children': []}
            for i in range(100)]}
        tree['children'][0]['attributes']['text'] = '长文本' * 200
        tree = {'attributes': {}, 'children': [{'attributes': {'bundleName': 'example.app'}, 'children': tree['children']}]}
        def hdc(argv, cwd, env, timeout, log):
            args = argv[3:]
            if args[:3] == ['shell', 'uitest', 'dumpLayout']:
                text = 'DumpLayout saved to: fixture'
            elif args[:2] == ['file', 'recv']:
                Path(args[3]).write_text(json.dumps(tree, ensure_ascii=False), encoding='utf-8')
                text = 'received'
            else:
                raise AssertionError(args)
            log.write_text(text, encoding='utf-8')
            return {'returncode': 0}
        with patch('dfx_agent.harmony_device.execute', side_effect=hdc) as commands:
            first = self.action('layout', limit=10)
            page = first
            seen = []
            while True:
                seen.extend(n['id'] for n in page['nodes'])
                self.assertLess(len(json.dumps(page, ensure_ascii=False)), 9500)
                if page['next_offset'] is None:
                    break
                page = self.action('layout', saved_layout=first['layout'], offset=page['next_offset'], limit=10)
                self.assertFalse(page['fresh_capture'])
            self.assertEqual(commands.call_count, 2)
        self.assertEqual(seen, [str(i) for i in range(100)])
        self.assertNotIn('backgroundImage', first['nodes'][0])
        self.assertIs(first['nodes'][0]['enabled'], False)
        self.assertEqual(first['nodes'][0]['truncated_fields'], ['text'])
        self.assertEqual(json.loads((self.output / first['layout']).read_text(encoding='utf-8')), tree)
        raw = (self.output / first['raw_layout']).read_text(encoding='utf-8')
        self.assertEqual(json.loads(raw), tree)
        readable = (self.output / first['layout']).read_text(encoding='utf-8')
        line = next(i for i, value in enumerate(readable.splitlines(), 1) if '门店99' in value)
        last = self.tools.execute({'action': 'read', 'source': 'tool-log', 'path': first['layout'],
                                  'start': line, 'end': line + 5})
        self.assertIn('门店99', last['content'])
        with patch('dfx_agent.harmony_device.execute', side_effect=AssertionError('No HDC for invalid pagination')):
            self.assertEqual(self.action('layout', offset=-1)['status'], 'TOOL_ERROR')

    def test_start_error_zero_exit_is_blocked_without_retry(self):
        self.adapter.allow_device = True
        with patch.object(Device, 'command', return_value='error: 10108101') as command:
            with self.assertRaises(InterruptAgentFlow):
                self.action('start', bundle='example.app', module='entry', ability='EntryAbility')
        result = self.tools.events[-1]['result']
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('10108101', result['error'])
        self.assertEqual(command.call_count, 1)

    def test_start_locked_is_returned_to_agent_without_hidden_recovery(self):
        self.adapter.allow_device = True
        self.adapter.allow_screen_unlock = True
        self.adapter.target_bundle = None
        response = 'error: failed to start ability. Error Code:10106102 Error Message:screen is locked'
        with patch.object(Device, 'command', return_value=response) as command:
            result = self.action('start', bundle='example.app', module='entry', ability='EntryAbility')
        self.assertEqual(result['status'], 'SCREEN_LOCKED')
        self.assertFalse(result['environment_blocked'])
        self.assertFalse(result['fresh_capture'])
        self.assertFalse(result['wake_sent'])
        self.assertFalse(result['swipe_sent'])
        self.assertIsNone(self.adapter.target_bundle)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(len(self.tools.events), 1)

    def test_start_locked_without_permission_still_stops(self):
        self.adapter.allow_device = True
        with patch.object(Device, 'command', return_value='Error Code:10106102') as command:
            with self.assertRaises(InterruptAgentFlow):
                self.action('start', bundle='example.app', module='entry', ability='EntryAbility')
        self.assertEqual(self.tools.events[-1]['result']['status'], 'BLOCKED')
        self.assertEqual(command.call_count, 1)

    def test_input_chinese_and_shell_punctuation_are_one_argument(self):
        self.adapter.allow_device = True
        value = "中文 空格 ' ; $(echo bad) [*?]"
        with patch.object(Device, 'layout', return_value={}), patch.object(Device, 'command', return_value='ok') as command:
            self.assertEqual(self.action('input', x=10, y=20, text=value)['status'], 'EXECUTED')
        self.assertEqual(command.call_args.args[0], 'shell')
        parsed = shlex.split(command.call_args.args[1])
        self.assertEqual(parsed, ['uitest', 'uiInput', 'inputText', '10', '20', value])

    def test_layout_and_fault_file_observations_are_readable(self):
        self.adapter.allow_device = True
        def command(device, *argv, **kwargs):
            if argv[:3] == ('shell', 'uitest', 'dumpLayout'):
                return 'DumpLayout saved to: fixture'
            if argv[:2] == ('file', 'recv'):
                local = Path(argv[3])
                local.write_text(json.dumps({'attributes': {}, 'children': [{'attributes': {'text': '观察', 'bundleName': 'example.app'}}]})
                                 if local.suffix == '.json' else 'Pid: 9\nfixture fault', encoding='utf-8')
                return 'ok'
            if argv[:2] == ('shell', 'ls'):
                return 'jscrash-example.app-1.log\njscrash-other.app-2.log'
            raise AssertionError(argv)
        with patch.object(Device, 'command', command):
            result = self.action('layout')
            self.assertEqual(result['nodes'][0]['text'], '观察')
            self.assertTrue((self.output / result['layout']).is_file())
            self.assertEqual(self.action('faults', bundle='example.app')['files'], ['jscrash-example.app-1.log'])
            result = self.action('fault', bundle='example.app', name='jscrash-example.app-1.log')
            self.assertIn('Pid: 9', result['content'])
            self.assertEqual(self.action('fault', bundle='example.app', name='../other')['status'], 'TOOL_ERROR')
        read = self.tools.execute({'action': 'read', 'source': 'tool-log', 'path': result['log']})
        self.assertIn('fixture fault', read['content'])

    def test_shared_device_records_failure_timeout_and_filters_keys(self):
        self.assertTrue(issubclass(LegacyDevice, Device))
        device = Device(Path('unused-hdc'), 'fixture', self.root / 'device')
        def failed(argv, cwd, env, timeout, log):
            self.assertNotIn('DEEPSEEK_API_KEY', env)
            log.write_text('[Fail] transport', encoding='utf-8')
            return {'returncode': 0}
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fixture-secret'}), \
             patch('dfx_agent.harmony_device.execute', side_effect=failed):
            with self.assertRaises(Blocked):
                device.command('shell', 'something')
        self.assertEqual(device.commands[0]['returncode'], 0)
        self.assertTrue((device.output / 'commands.jsonl').is_file())
        def timeout(argv, cwd, env, limit, log):
            log.write_text('partial', encoding='utf-8')
            return {'status': 'BLOCKED', 'error': 'Command timed out; process tree terminated'}
        with patch('dfx_agent.harmony_device.execute', side_effect=timeout):
            with self.assertRaisesRegex(Blocked, 'timed out'):
                device.command('shell', 'wait')
        self.assertEqual(len(device.commands), 2)

    def test_relative_evidence_directory_passes_absolute_receive_path(self):
        local = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(local.cleanup)
        destination = Path(local.name) / 'relative-output'
        relative = Path(os.path.relpath(destination, Path.cwd()))
        device = Device(Path('fixture-hdc'), 'fixture', relative)
        def command(argv, cwd, env, timeout, log):
            args = argv[3:]
            self.assertEqual(cwd, destination.resolve())
            if args[:3] == ['shell', 'uitest', 'dumpLayout']:
                response = 'DumpLayout saved to: fixture'
            elif args[:2] == ['file', 'recv']:
                self.assertTrue(Path(args[3]).is_absolute())
                self.assertEqual(Path(args[3]).parent, destination.resolve())
                Path(args[3]).write_text('{"attributes":{"text":"layout"},"children":[]}', encoding='utf-8')
                response = 'received'
            else:
                raise AssertionError(args)
            log.write_text(response, encoding='utf-8')
            return {'returncode': 0}
        with patch('dfx_agent.harmony_device.execute', side_effect=command) as commands:
            self.assertEqual(device.layout()['attributes']['text'], 'layout')
        self.assertEqual(commands.call_count, 2)

    def test_config_paths_commands_and_path_boundary(self):
        config = self.root / 'host.json'
        config.write_text(json.dumps({'deveco': './sdk path', 'device': 'fixture'}))
        self.assertEqual(load_host(config)['deveco'], str(self.root / 'sdk path'))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret'}):
            self.assertNotIn('DEEPSEEK_API_KEY', build_environment(Path('sdk')))
        argv = build_command(Path('sdk'), 'library', 'phone', 'debug')
        self.assertIn('module=library@debug', argv)
        self.assertIn('product=phone', argv)
        with patch('dfx_agent.harmony_tools.execute', side_effect=AssertionError('must not run')):
            self.assertEqual(self.action('build', **{**self.build_action(), 'artifact': '../escape.hap'})['status'], 'TOOL_ERROR')

    def test_mini_cli_receives_skill_and_actual_local_build_feedback(self):
        # A clearly labelled local batch stand-in runs through the real process runner.
        # It is not a vendor build, device operation or model capability result.
        tool = Path(self.host['deveco']) / 'tools/hvigor/bin/hvigorw.bat'
        tool.parent.mkdir(parents=True)
        with zipfile.ZipFile(tool.parent / 'fixture.hap', 'w') as archive:
            archive.writestr('module.json', json.dumps({'app': {'bundleName': 'example.app'}, 'module': {'name': 'entry'}}))
        tool.write_text('@echo off\necho %* | findstr /L /C:"module=broken@default" >nul\nif not errorlevel 1 (\n'
                        'echo fixture compiler failure\nexit /b 7\n)\n'
                        'if not exist entry mkdir entry\n'
                        f'copy /y "{tool.parent / "fixture.hap"}" entry\\app.hap >nul\n'
                        'echo fixture build complete\nexit /b 0\n', encoding='ascii')
        config = self.root / 'host.json'
        config.write_text(json.dumps(self.host), encoding='utf-8')
        actions = [
            {'action': 'harmony', 'operation': 'build', **{**self.build_action(), 'module': 'broken'}},
            {'action': 'harmony', 'operation': 'build', **self.build_action()},
            {'action': 'harmony', 'operation': 'install', 'artifact': 'entry/app.hap'},
            {'action': 'finish', 'summary': 'Local interface fixture; device unverified'},
        ]
        responses = self.root / 'responses.json'
        responses.write_text(json.dumps(actions))
        run = self.root / 'run'
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')), \
             patch('dfx_agent.harmony_device.execute', side_effect=AssertionError('HDC forbidden')), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(['solve', '--repo', str(self.repo), '--task', 'Local interface test',
                         '--responses', str(responses), '--host', str(config), '--out', str(run)])
        self.assertEqual(code, 0)
        events = [json.loads(s) for s in (run / 'tools/events.jsonl').read_text().splitlines()]
        self.assertEqual([e['result']['status'] for e in events], ['FAILED', 'EXECUTED', 'TOOL_ERROR', 'SUBMITTED'])
        request = json.loads((run / 'model/0002/request.json').read_text())
        self.assertIn('harmony-debug', request[0]['content'])
        self.assertIn('fixture compiler failure', request[-2]['content'])
        self.assertEqual(json.loads((run / 'result.json').read_text())['api_calls'], 0)
        self.assertIn('harmony build', (run / 'report.md').read_text(encoding='utf-8'))
        self.assertFalse((self.repo / 'entry').exists())

    def test_no_host_does_not_load_skill_and_rejects_execution_flags(self):
        result = solve(self.repo, 'No host', self.root / 'plain', responses=[
            {'action': 'harmony', 'operation': 'info'}, {'action': 'finish', 'summary': 'unavailable'}])
        self.assertFalse(result['harmony']['configured'])
        self.assertFalse((self.root / 'plain/harmony-skill.md').exists())
        with self.assertRaisesRegex(ValueError, 'require --host'):
            solve(self.repo, 'No host', self.root / 'invalid', responses=[], allow_device=True)

    def test_mini_observes_device_layout_through_mocked_process_boundary(self):
        config = self.root / 'host.json'
        config.write_text(json.dumps(self.host))
        run = self.root / 'device-loop'
        def hdc(argv, cwd, env, timeout, log):
            args = argv[3:]
            if args[:3] == ['shell', 'aa', 'start']:
                output = 'start ability successfully'
            elif args[:3] == ['shell', 'uitest', 'dumpLayout']:
                output = 'DumpLayout saved to: mock'
            elif args[:2] == ['file', 'recv']:
                Path(args[3]).write_text(json.dumps({'attributes': {}, 'children': [{'attributes': {'text': '模拟页面', 'bundleName': 'example.app'}}]}), encoding='utf-8')
                output = 'received'
            else:
                raise AssertionError(args)
            log.write_text(output, encoding='utf-8')
            return {'returncode': 0}
        with patch('dfx_agent.harmony_device.execute', side_effect=hdc) as commands, \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')):
            result = solve(self.repo, 'Mocked device interface only', run, host=config, allow_device=True, responses=[
                {'action': 'harmony', 'operation': 'start', 'bundle': 'example.app', 'module': 'entry', 'ability': 'EntryAbility'},
                {'action': 'harmony', 'operation': 'layout'},
                {'action': 'finish', 'summary': 'Mock observation, no device result'},
            ])
        self.assertEqual(commands.call_count, 3)
        self.assertEqual(result['api_calls'], 0)
        self.assertFalse(result['repair_validated'])
        request = json.loads((run / 'model/0003/request.json').read_text(encoding='utf-8'))
        self.assertIn('模拟页面', request[-2]['content'])


if __name__ == '__main__':
    unittest.main()
