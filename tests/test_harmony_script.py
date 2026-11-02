"""Real child processes using an explicitly local HDC substitute, no devices."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from dfx_agent.harmony_tools import HarmonyTools
from dfx_agent.tools import ProjectTools
from dfx_agent.harmony_device import Device, ScriptDeadline


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.adapter = HarmonyTools(self.repo, self.root / 'tools',
            {'deveco': str(self.root / 'absent-sdk'), 'device': 'local-test-only'}, allow_device=True)
        self.adapter.target_bundle = 'example.app'
        self.tools = ProjectTools(self.repo, self.root / 'tools', harmony=self.adapter)
        fake = self.root / 'fake_hdc.py'
        fake.write_text("""import sys,json
from pathlib import Path
args=sys.argv[1:]
if args[2:4]==['file','recv']:
    Path(args[-1]).write_text(json.dumps({'attributes':{},'children':[{'attributes':{'bundleName':'example.app'},'children':[
      {'attributes':{'text':'设置','id':'settings','visible':True,'enabled':True,'bounds':'[0,0][20,20]'}}]}]}),encoding='utf-8')
elif 'dumpLayout' in args: print('DumpLayout saved to: fake')
else: print('local fixture action')
""", encoding='utf-8')
        self.adapter.hdc = self.root / 'hdc-fixture.cmd'
        self.adapter.hdc.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding='utf-8')

    def run_script(self, content):
        (self.repo / 'check.py').write_text(content, encoding='utf-8')
        return self.tools.execute({'action': 'harmony', 'operation': 'run_script', 'script': 'check.py'})

    def test_actual_runner_and_driver_roundtrip_then_assertion_failure(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'DO_NOT_INHERIT'}):
            result = self.run_script(
                "import os\n"
                "def main(device):"
                "\n    assert 'DEEPSEEK_API_KEY' not in os.environ"
                "\n    device.tap(text='设置')"
                "\n    assert device.wait(text='设置', timeout=3)['matches']"
                "\n    device.input('中文', id='settings')"
                "\n    device.key('Back')"
                "\n    assert device.layout(text='设置', offset=0, limit=80)['total_matches'] == 1\n")
        self.assertEqual(result['status'], 'EXECUTED', result)
        lines = (self.root / 'tools/harmony-0001/device/commands.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertGreaterEqual(len(lines), 8)
        self.assertTrue(any('inputText' in row for row in lines))
        result = self.run_script("def main(device):\n    assert False, 'specific check failed'\n")
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('specific check failed', result['output'])
        self.assertEqual(self.run_script('def main(:\n')['status'], 'TOOL_ERROR')

    def test_one_script_reuses_one_capture_for_several_assertions(self):
        result = self.run_script(
            "def main(device):\n"
            "    state = device.layout(text='设置')\n"
            "    assert state['status'] == 'OBSERVED'\n"
            "    assert state['total_matches'] == 1\n"
            "    assert state['next_offset'] is None\n"
            "    assert state['nodes'][0]['text'] == '设置'\n"
            "    print('one-state assertions completed')\n")
        self.assertEqual(result['status'], 'EXECUTED', result)
        commands = [json.loads(line) for line in
                    (self.root/'tools/harmony-0001/device/commands.jsonl').read_text('utf-8').splitlines()]
        self.assertEqual(sum('dumpLayout' in json.dumps(c) for c in commands), 1)
        self.assertEqual(len(commands), 2)  # One dump and one receive; no real device.

    def test_navigation_permission_reaches_real_child_runner(self):
        (self.root / 'fake_hdc.py').write_text("""import sys,json
from pathlib import Path
args=sys.argv[1:]
if args[2:4]==['file','recv']:
    Path(args[-1]).write_text(json.dumps({'children':[]}),encoding='utf-8')
elif 'dumpLayout' in args: print('DumpLayout saved to: fake')
else: print('local fixture action')
""", encoding='utf-8')
        content = "def main(device):\n    r=device.perform('navigate', key='Back')\n    assert r['target_state_after']=='NOT_CHECKED'\n"
        self.assertEqual(self.run_script(content)['status'], 'TOOL_ERROR')
        self.adapter.allow_system_navigation = True
        result = self.run_script(content)
        self.assertEqual(result['status'], 'EXECUTED', result)
        rows = [json.loads(line) for line in (self.root / 'tools/harmony-0002/device/commands.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['argv'], ['shell', 'uitest uiInput keyEvent Back'])
        actions = json.loads((self.root / 'tools/harmony-0002/device/actions.json').read_text(encoding='utf-8'))
        self.assertEqual(actions[0]['action'], 'navigate')
        self.assertFalse(actions[0]['result']['target_visible_before'])

    def test_real_child_lock_partial_effects_then_explicit_screen_recovery(self):
        self.adapter.host['screen_lock_profile'] = 'swipe_only'
        self.adapter.allow_screen_unlock = True
        (self.root / 'fake_hdc.py').write_text('''import sys,json
from pathlib import Path
args=sys.argv[1:]
base=Path(__file__).parent
state=base/'screen-state.txt'
phase=state.read_text() if state.exists() else 'app'
with (base/'calls.jsonl').open('a') as stream: stream.write(json.dumps(args)+'\\n')
if args[2:4]==['file','recv']:
    attrs={'bundleName':'example.app','text':'首页'}
    if phase=='lock': attrs={'bundleName':'com.ohos.sceneboard','id':'ScreenLockRootComponent','bounds':'[0,0][100,200]','text':'SECRET_NOTIFICATION'}
    if phase=='pin': attrs={'bundleName':'com.ohos.sceneboard','type':'PasswordInput','text':'SECRET_PIN_PROMPT'}
    Path(args[-1]).write_text(json.dumps({'children':[{'attributes':attrs}]}),encoding='utf-8')
elif 'dumpLayout' in args: print('DumpLayout saved to: fixture')
elif any('keyEvent Back' in arg for arg in args): state.write_text('lock')
elif 'swipe' in args: state.write_text('app')
else: print('local fixture action')
''', encoding='utf-8')
        result = self.run_script("def main(device):\n    device.key('Back')\n    device.layout()\n    raise AssertionError('MUST_NOT_CONTINUE')\n")
        self.assertEqual(result['status'], 'SCREEN_LOCKED', result)
        self.assertFalse(result['script_completed'])
        self.assertTrue(result['partial_side_effects_possible'])
        self.assertNotIn('MUST_NOT_CONTINUE', result['output'])
        result = self.run_script("def main(device):\n    r=device.perform('screen', mode='unlock')\n    assert r['screen_state']=='UNLOCKED'\n    assert device.layout(text='首页')['total_matches']==1\n")
        self.assertEqual(result['status'], 'EXECUTED', result)
        calls = (self.root / 'calls.jsonl').read_text().splitlines()
        self.assertEqual(sum('keyEvent Back' in row for row in calls), 1)
        self.assertEqual(sum('swipe' in row for row in calls), 1)
        (self.root / 'screen-state.txt').write_text('pin')
        from minisweagent.exceptions import InterruptAgentFlow
        with self.assertRaises(InterruptAgentFlow):
            self.run_script("def main(device):\n    device.perform('screen', mode='observe')\n    print('MUST_NOT_CONTINUE')\n")
        self.assertEqual(self.tools.events[-1]['result']['failure_kind'], 'credential_required')
        self.assertNotIn('MUST_NOT_CONTINUE', self.tools.events[-1]['result']['output'])
        for file in (self.root / 'tools').rglob('*'):
            if file.is_file():
                self.assertNotIn('SECRET_', file.read_text(encoding='utf-8'))

    def test_paused_or_excluded_scripts_do_not_launch(self):
        self.adapter.allow_device = False
        with patch('dfx_agent.harmony_tools.execute', side_effect=AssertionError('Must not launch')):
            self.assertEqual(self.run_script('def main(device): pass')['status'], 'TOOL_ERROR')
            self.adapter.allow_device = True
            self.tools.exclude = ['check.py']
            self.assertEqual(self.run_script('def main(device): pass')['status'], 'TOOL_ERROR')

    def test_target_absent_ends_actual_script_without_replaying_completed_action(self):
        fake = self.root / 'fake_hdc.py'
        fake.write_text("""import sys,json
from pathlib import Path
args=sys.argv[1:]
counter=Path(__file__).with_name('captures.txt')
if 'dumpLayout' in args:
    n=int(counter.read_text())+1 if counter.exists() else 1
    counter.write_text(str(n))
    print('DumpLayout saved to: fake')
elif args[2:4]==['file','recv']:
    window={'attributes':{'bundleName':'example.app'},'children':[]}
    tree={'children':[window]} if counter.read_text()=='1' else {'children':[]}
    Path(args[-1]).write_text(json.dumps(tree),encoding='utf-8')
else: print('local fixture action')
""", encoding='utf-8')
        result = self.run_script("from pathlib import Path\ndef main(device):\n"
            "    device.key('Back')\n    Path('completed.txt').write_text('one')\n"
            "    device.layout()\n    device.key('Back')\n"
            "    Path('incorrect-success.txt').write_text('bad')\n")
        self.assertEqual(result['status'], 'NOT_VISIBLE', result)
        self.assertFalse(result['script_completed'])
        self.assertEqual(result['returncode'], 3)
        self.assertNotIn('environment_blocked', result)
        self.assertEqual((self.repo/'completed.txt').read_text(), 'one')
        self.assertFalse((self.repo/'incorrect-success.txt').exists())
        folder = self.root/'tools/harmony-0001/device'
        commands = [json.loads(s) for s in (folder/'commands.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands), 5)
        self.assertEqual(sum('keyEvent' in ' '.join(c['argv']) for c in commands), 1)
        actions = json.loads((folder/'actions.json').read_text(encoding='utf-8'))
        self.assertEqual(actions[-1]['action'], 'layout')
        self.assertEqual(actions[-1]['result']['status'], 'NOT_VISIBLE')
        self.assertFalse((self.root/'tools/started.json').exists())

    def test_mini_fixture_writes_checks_observes_failure_and_revises(self):
        from dfx_agent.agent import solve
        from dfx_agent.workspace import save_json, read_json
        (self.repo / 'README.md').write_text('Local mock-device project')
        host = self.root / 'host.json'
        save_json(host, self.adapter.host)
        original = HarmonyTools.__init__
        fake_hdc = self.adapter.hdc
        def init(adapter, *args, **kwargs):
            original(adapter, *args, **kwargs)
            adapter.hdc = fake_hdc
            adapter.target_bundle = 'example.app'
        with patch.object(HarmonyTools, '__init__', init):
            result = solve(self.repo, 'Local scripted driver fixture', self.root/'agent',
                host=host, allow_device=True, responses=[
                    {'action': 'write', 'path': 'checks/ui.py', 'content':
                        "def main(device):\n    device.tap(text='设置')\n    assert False, 'fixture failing assertion'\n"},
                    {'action': 'harmony', 'operation': 'run_script', 'script': 'checks/ui.py'},
                    {'action': 'edit', 'path': 'checks/ui.py', 'search': "False, 'fixture failing assertion'",
                     'replace': "device.wait(text='设置')['matches']"},
                    {'action': 'harmony', 'operation': 'run_script', 'script': 'checks/ui.py'},
                    {'action': 'finish', 'summary': 'mock only; not device acceptance'}])
        self.assertEqual(result['status'], 'Submitted', result)
        events = [json.loads(s) for s in (self.root/'agent/tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([e['result']['status'] for e in events if e['action']['action']=='harmony'],
                         ['FAILED', 'EXECUTED'])
        self.assertIn('fixture failing assertion', read_json(self.root/'agent/model/0003/request.json')[-2]['content'])

    def test_script_timeout_cleans_child_and_does_not_repeat(self):
        self.adapter.script_timeout = 1
        with patch('dfx_agent.harmony_tools.SCRIPT_FINALIZATION_SECONDS', .5):
            result = self.run_script("import time\nfrom pathlib import Path\n"
                                    "def main(device):\n    time.sleep(4)\n    Path('late.txt').write_text('bad')\n")
        self.assertEqual(result['status'], 'TIMEOUT', result)
        self.assertTrue(result['cleanup_complete'])
        time.sleep(3)
        self.assertFalse((self.repo / 'late.txt').exists())

    def test_internal_deadline_returns_receipt_before_outer_cleanup(self):
        self.assertEqual(self.adapter.script_timeout, 120)
        self.assertEqual(self.adapter.device_timeout, 60)
        self.adapter.script_timeout = 1
        result = self.run_script("import time\nfrom pathlib import Path\n"
            "def main(device):\n    time.sleep(1.2)\n    device.key('Back')\n"
            "    Path('late-action.txt').write_text('bad')\n")
        self.assertEqual(result['status'], 'TIMEOUT', result)
        self.assertEqual(result['returncode'], 3)
        self.assertNotIn('cleanup', result)  # The child exits naturally with a receipt.
        self.assertFalse((self.root/'tools/harmony-0001/device/commands.jsonl').exists())
        self.assertFalse((self.repo/'late-action.txt').exists())
        config = json.loads((self.root/'tools/harmony-0001/script-config.json').read_text())
        self.assertEqual(config['timeout'], 60)
        self.assertEqual(config['script_timeout'], 1)
        self.assertEqual(result['finalization_seconds'], 35)

    def test_script_return_after_deadline_is_not_success(self):
        self.adapter.script_timeout = 1
        result = self.run_script('import time\ndef main(device):\n    time.sleep(1.2)\n')
        self.assertEqual(result['status'], 'TIMEOUT', result)
        self.assertIn('returned after operation deadline', result['error'])

    def test_inflight_local_hdc_deadline_has_one_cleanup_owner(self):
        self.adapter.script_timeout = 2
        fake = self.root/'fake_hdc.py'
        fake.write_text("import time\nfrom pathlib import Path\n"
                        "p=Path('dispatches.txt')\n"
                        "with p.open('a') as s: s.write('one\\n')\n"
                        "time.sleep(8)\nprint('too late')\n", encoding='utf-8')
        result = self.run_script("def main(device):\n    device.layout()\n    device.key('Back')\n")
        self.assertEqual(result['status'], 'TIMEOUT', result)
        self.assertEqual(result['returncode'], 3)
        self.assertNotIn('cleanup', result)  # Outer runner did not also kill the same tree.
        folder = self.root/'tools/harmony-0001/device'
        commands = [json.loads(s) for s in (folder/'commands.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0]['cleanup_complete'])
        self.assertEqual(commands[0]['failure_kind'], 'timeout')
        self.assertEqual((folder/'dispatches.txt').read_text().splitlines(), ['one'])

    def test_script_unknown_cleanup_stops_without_receipt_substitution(self):
        from minisweagent.exceptions import InterruptAgentFlow
        def failed(argv, cwd, env, timeout, log):
            self.assertEqual(timeout, 155)
            log.write_text('unconfirmed local fixture cleanup')
            return {'status': 'BLOCKED', 'failure_kind': 'cleanup', 'cleanup_complete': False,
                    'error': 'unknown child termination'}
        with patch('dfx_agent.harmony_tools.execute', side_effect=failed) as dispatch:
            with self.assertRaises(InterruptAgentFlow):
                self.run_script('def main(device): pass')
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.tools.events[-1]['result']['failure_kind'], 'cleanup')
        self.assertTrue((self.root/'tools/started.json').exists())

    def test_cli_freezes_separate_script_limit_without_device_or_api(self):
        from dfx_agent.workspace import save_json, read_json
        (self.repo/'README.md').write_text('offline CLI fixture')
        save_json(self.root/'host.json', self.adapter.host)
        save_json(self.root/'actions.json', [
            {'action': 'harmony', 'operation': 'info'},
            {'action': 'finish', 'summary': 'offline configuration fixture only'}])
        run = subprocess.run([sys.executable, '-m', 'dfx_agent', 'solve',
            '--repo', str(self.repo), '--task', 'offline configuration',
            '--host', str(self.root/'host.json'), '--responses', str(self.root/'actions.json'),
            '--script-timeout', '120', '--command-timeout', '60', '--out', str(self.root/'cli')],
            capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
        config = read_json(self.root/'cli/launch.json')
        self.assertEqual(config['options']['script_timeout'], 120)
        self.assertEqual(config['options']['command_timeout'], 60)
        result = read_json(self.root/'cli/result.json')
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(result['harmony']['script_timeout_seconds'], 120)
        self.assertFalse(result['harmony']['device_enabled'])

    def test_hdc_failure_blocks_and_driver_uses_remaining_deadline(self):
        self.adapter.hdc.write_text('@exit /b 9\n')
        from minisweagent.exceptions import InterruptAgentFlow
        with self.assertRaises(InterruptAgentFlow):
            self.run_script("def main(device):\n    device.layout()\n")
        self.assertEqual(self.tools.events[-1]['result']['status'], 'BLOCKED')
        device = Device(self.adapter.hdc, 'fake', self.root / 'deadline',
                        timeout=60, deadline=time.monotonic()+0.2)
        def fake_execute(argv, cwd, env, timeout, log):
            self.assertLessEqual(timeout, .2)
            log.write_text('local deadline')
            return {'status': 'BLOCKED', 'failure_kind': 'timeout', 'cleanup_complete': True}
        with patch('dfx_agent.harmony_device.execute', side_effect=fake_execute):
            with self.assertRaises(ScriptDeadline):
                device.command('shell', 'local-test')

    def test_failed_cleanup_remains_blocked_with_unknown_action_marker(self):
        from minisweagent.exceptions import InterruptAgentFlow
        def failed(argv, cwd, env, timeout, log):
            log.write_text('cleanup test')
            return {'status': 'BLOCKED', 'failure_kind': 'cleanup', 'cleanup_complete': False,
                    'error': 'Cannot confirm process termination'}
        with patch('dfx_agent.tools.execute', side_effect=failed):
            with self.assertRaises(InterruptAgentFlow):
                self.tools.execute({'action': 'exec', 'command': 'fixture only'})
        self.assertTrue((self.root / 'tools/started.json').exists())


if __name__ == '__main__':
    unittest.main()
