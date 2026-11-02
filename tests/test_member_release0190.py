"""Offline product-boundary regressions from member feedback; no API or device."""
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from dfx_agent.harmony_build import build_environment
from dfx_agent.environment_check import check_java, package_libraries
from dfx_agent.harmony_device import Device, Blocked, application_tree, TargetNotVisible
from dfx_agent.harmony_script import ScriptDevice
from dfx_agent.harmony_tools import HarmonyTools
from dfx_agent.member_tasks import MemberTasks, environment_only_attempt
from dfx_agent.task import files, prepare_task, export_diff
from dfx_agent.workspace import save_json


def package(path, name='entry', dependencies=(), machine=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('module.json', json.dumps({'app': {'bundleName': 'fixture.app'},
            'module': {'name': name, 'dependencies': [{'moduleName': d} for d in dependencies]}}))
        if machine:
            header = bytearray(20); header[:4] = b'\x7fELF'; header[5] = 1
            header[18:20] = machine.to_bytes(2, 'little')
            archive.writestr('libs/arm64-v8a/example.so', header)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.output = self.root / 'tools'; self.output.mkdir()
        self.vendor = self.root / 'DevEco Studio'
        self.adapter = HarmonyTools(self.repo, self.output,
            {'deveco': str(self.vendor), 'device': 'offline-only'}, allow_device=True)
        self.artifact = self.repo / 'entry/build/default/outputs/default/entry-default-unsigned.hap'
        self.action = {'operation': 'build', 'module': 'entry', 'product': 'default',
                       'target': 'default', 'artifact': self.artifact.relative_to(self.repo).as_posix()}
        self.calls = []
        self.graph = {'entry': ['home', 'ticket'], 'home': ['ticket'], 'ticket': []}
        self.fail = None

    def compiler(self, argv, cwd, env, timeout, log):
        name = next(a for a in argv if a.startswith('module=')).split('=')[1].split('@')[0]
        self.calls.append(name)
        log.write_text('actual fixture diagnostic: EPERM' if name == self.fail else 'BUILD SUCCESSFUL', encoding='utf8')
        if name == self.fail:
            return {'returncode': 1, 'seconds': .1}
        suffix = 'hap' if name == 'entry' else 'hsp'
        p = self.repo / name / 'build/default/outputs/default' / f'{name}-default-unsigned.{suffix}'
        package(p, name, self.graph[name])
        return {'returncode': 0, 'seconds': .1}

    def build(self):
        mapping = [{'name': n, 'path': n, 'module': n, 'type': 'shared'} for n in self.graph if n != 'entry']
        with patch('dfx_agent.harmony_tools.execute', side_effect=self.compiler), \
             patch('dfx_agent.harmony_tools.project_modules', return_value=mapping):
            return self.adapter.execute(self.action, 1)

    def test_hsp_build_once_and_install_topological_order(self):
        result = self.build()
        self.assertEqual(result['status'], 'EXECUTED', result)
        self.assertEqual(self.calls, ['entry', 'home', 'ticket'])
        with patch.object(Device, 'command', return_value='install bundle successfully') as call, \
             patch('dfx_agent.harmony_tools.time.sleep'):
            self.adapter.execute({'operation': 'install', 'artifact': self.action['artifact']}, 2)
        self.assertEqual([Path(c.args[-1]).name for c in call.call_args_list],
                         ['ticket-default-unsigned.hsp', 'home-default-unsigned.hsp', 'entry-default-unsigned.hap'])

    def test_failed_shared_build_keeps_diagnostic_and_forbids_install(self):
        self.fail = 'home'
        result = self.build()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('EPERM', result['error'])
        self.assertEqual(self.calls, ['entry', 'home'])
        self.assertFalse(self.adapter.built)
        with patch.object(Device, 'command', side_effect=AssertionError('must not dispatch')):
            with self.assertRaisesRegex(ValueError, 'Build this HAP'):
                self.adapter.execute({'operation': 'install', 'artifact': self.action['artifact']}, 2)

    def test_cycle_does_not_fall_back_or_rebuild(self):
        self.graph['ticket'] = ['home']
        result = self.build()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('Cyclic', result['error'])
        self.assertEqual(self.calls, ['entry', 'home', 'ticket'])

    def test_no_hsp_means_no_extra_build(self):
        self.graph['entry'] = []
        self.assertEqual(self.build()['status'], 'EXECUTED')
        self.assertEqual(self.calls, ['entry'])

    def test_module_reader_timeout_stops_without_install_or_retry(self):
        with patch('dfx_agent.harmony_tools.execute', side_effect=self.compiler), \
             patch('dfx_agent.harmony_tools.project_modules',
                   side_effect=subprocess.TimeoutExpired('node', 30)) as reader:
            result = self.adapter.execute(self.action, 1)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['failure_kind'], 'timeout')
        self.assertEqual(self.calls, ['entry'])
        self.assertFalse(self.adapter.built)
        reader.assert_called_once()

    def test_actual_arm_package_blocks_before_install_without_replay(self):
        package(self.artifact, machine=183)
        self.adapter.built.add(self.artifact)
        with patch.object(Device, 'command', return_value='x86_64') as call:
            result = self.adapter.execute({'operation': 'install', 'artifact': self.action['artifact']}, 1)
        self.assertEqual(result['failure_kind'], 'package_abi')
        call.assert_called_once_with('shell', 'uname', '-m')
        self.assertEqual(package_libraries(self.artifact)[0]['architectures'], ['arm64-v8a'])

    def test_java_resolution_does_not_depend_on_user_path_or_change_it(self):
        exe = self.vendor / 'jbr/bin' / ('java.exe' if os.name == 'nt' else 'java')
        exe.parent.mkdir(parents=True); exe.write_bytes(b'fixture, not executable test')
        with patch.dict(os.environ, {'PATH': 'broken path', 'DEEPSEEK_API_KEY': 'never-export'}, clear=False):
            env = build_environment(self.vendor)
            self.assertEqual(env['PATH'].split(os.pathsep)[0], str(exe.parent))
            self.assertNotIn('DEEPSEEK_API_KEY', env)
            if os.name == 'nt': self.assertEqual(check_java(self.vendor), str(exe))
            self.assertEqual(os.environ['PATH'], 'broken path')
        exe.unlink()
        with self.assertRaises(ValueError): check_java(self.vendor)

    def test_script_default_bundle_and_missing_argument_are_not_driver_failure(self):
        self.adapter.target_bundle = 'fixture.app'
        device = Device(Path('unused'), 'offline', self.root / 'device')
        wrapper = ScriptDevice(self.adapter, device)
        with patch.object(device, 'faults', return_value=set()) as faults:
            self.assertEqual(wrapper.perform('faults')['files'], [])
            faults.assert_called_once_with('fixture.app')
        with self.assertRaisesRegex(ValueError, 'requires: name'):
            wrapper.perform('fault')

    def test_hdc_log_read_failure_never_reexecutes_command(self):
        device = Device(Path('unused'), 'offline', self.root / 'device')
        with patch('dfx_agent.harmony_device.execute', return_value={'returncode': 0}) as run, \
             patch.object(Path, 'read_text', side_effect=PermissionError('fixture lock')):
            with self.assertRaisesRegex(Blocked, 'no automatic replay'):
                device.command('shell', 'uitest', 'uiInput', 'click', '1', '2')
        self.assertEqual(run.call_count, 1)

    def test_package_absent_is_correctable_but_garbled_reply_is_blocked(self):
        device = Device(Path('unused'), 'offline', self.root / 'device')
        with patch.object(device, 'command', return_value='error: bundle name not found'):
            with self.assertRaisesRegex(ValueError, 'not installed'): device.package('fixture.app')
        with patch.object(device, 'command', return_value='error: failed to get information and the parameters may be wrong.'):
            with self.assertRaises(ValueError): device.package('fixture.app')
        with patch.object(device, 'command', return_value='unexpected truncated output'):
            with self.assertRaises(Blocked): device.package('fixture.app')

    def test_system_dialog_returns_state_without_text_or_click(self):
        tree = {'children': [{'attributes': {'bundleName': 'com.ohos.notificationdialog', 'text': 'private text'}}]}
        with self.assertRaises(TargetNotVisible) as caught: application_tree(tree, 'fixture.app')
        data = caught.exception.observation()
        self.assertTrue(data['system_dialog_present'])
        self.assertNotIn('private text', json.dumps(data))

    def test_scope_preserves_business_sessions_and_existing_generated_filename(self):
        from dfx_agent.member_tasks import INPUT_EXCLUDE
        names = ['components/trans/src/main/ets/sessions/Animation.ets', 'sessions/old.json',
                 'feature/src/main/module.json5', 'feature/BuildProfile.ets', '.cxx/cache.bin']
        for name in names:
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{}')
        selected = files(self.repo, INPUT_EXCLUDE)
        self.assertIn(names[0], selected)
        self.assertNotIn(names[1], selected)
        self.assertNotIn(names[-1], selected)
        self.assertIn(names[3], selected)

    def test_new_cxx_cache_does_not_become_candidate(self):
        (self.repo / 'source.ets').write_text('before')
        run = self.root / 'run'
        prepare_task(self.repo, run, 'offline fixture')
        generated = run / 'workspace/module/.cxx/native.o'
        generated.parent.mkdir(parents=True); generated.write_bytes(b'build output')
        self.assertEqual(export_diff(run)['changed_files'], [])

    def test_zero_call_worker_failure_can_recheck_but_unknown_or_called_cannot(self):
        run = self.root / 'run'; run.mkdir()
        row = {'run': str(run), 'status': 'ExitedWithoutResult'}
        self.assertTrue(environment_only_attempt(row))
        self.assertFalse(environment_only_attempt({**row, 'status': 'Unobserved'}))
        save_json(run / 'model/0001/request.json', {})
        self.assertFalse(environment_only_attempt(row))


if __name__ == '__main__': unittest.main()
