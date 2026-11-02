"""Explicit navigation through the shared driver; no real device or provider."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent import solve
from dfx_agent.harmony_tools import HarmonyTools
from dfx_agent.tool_schema import tool_definitions, validate_arguments
from dfx_agent.tools import ProjectTools


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'workspace'
        self.repo.mkdir()
        self.output = self.root / 'tools'
        self.adapter = HarmonyTools(self.repo, self.output, {'deveco': 'absent', 'device': 'fixture'},
                                   allow_device=True, allow_system_navigation=True)
        self.adapter.target_bundle = 'example.app'
        self.tools = ProjectTools(self.repo, self.output, harmony=self.adapter)
        self.tree = {'children': []}
        self.keys = []
        self.failure = None

    def hdc(self, argv, cwd, env, timeout, log):
        args = argv[3:]
        if self.failure:
            log.write_text('[Fail] disconnected', encoding='utf-8')
            return {'returncode': 1}
        if args[:3] == ['shell', 'uitest', 'dumpLayout']:
            self.assertNotIn('-b', args)
            output = 'DumpLayout saved to: fixture'
        elif args[:2] == ['file', 'recv']:
            self.assertFalse(Path(args[-1]).is_relative_to(self.output))
            Path(args[-1]).write_text(json.dumps(self.tree), encoding='utf-8')
            output = 'received'
        elif args[0] == 'shell' and args[1].startswith('uitest uiInput keyEvent '):
            self.keys.append(args[1].split()[-1])
            output = 'No Error'
        else:
            raise AssertionError(args)
        log.write_text(output, encoding='utf-8')
        return {'returncode': 0}

    def action(self, operation='navigate', key='Back'):
        return self.tools.execute({'action': 'harmony', 'operation': operation, 'key': key})

    def test_explicit_back_and_home_outside_target_dispatch_once_without_success_claim(self):
        with patch('dfx_agent.harmony_device.execute', side_effect=self.hdc) as commands:
            for key in ('Back', 'Home'):
                result = self.action(key=key)
                self.assertEqual(result['status'], 'EXECUTED')
                self.assertFalse(result['target_visible_before'])
                self.assertEqual(result['target_state_after'], 'NOT_CHECKED')
            self.assertEqual(commands.call_count, 6)  # Two captures + exactly two keys.
        self.assertEqual(self.keys, ['Back', 'Home'])

    def test_default_disabled_paused_and_unbound_dispatch_nothing(self):
        for state in ('disabled', 'paused', 'unbound'):
            with self.subTest(state=state):
                self.adapter.allow_device = state != 'paused'
                self.adapter.allow_system_navigation = state != 'disabled'
                self.adapter.target_bundle = None if state == 'unbound' else 'example.app'
                with patch('dfx_agent.harmony_device.execute') as commands:
                    self.assertEqual(self.action()['status'], 'TOOL_ERROR')
                    commands.assert_not_called()
        default = HarmonyTools(self.repo, self.output, self.adapter.host, allow_device=True)
        self.assertFalse(default.capabilities()['system_navigation_enabled'])

    def test_confirm_key_rejected_at_native_contract_and_shared_driver(self):
        with self.assertRaisesRegex(ValueError, 'Back or Home'):
            validate_arguments('harmony', {'operation': 'navigate', 'key': 'Enter'}, tool_definitions(True))
        with patch('dfx_agent.harmony_device.execute') as commands:
            self.assertEqual(self.action(key='Enter')['status'], 'TOOL_ERROR')
            commands.assert_not_called()

    def test_original_key_and_coordinate_permissions_are_not_widened(self):
        with patch('dfx_agent.harmony_device.execute', side_effect=self.hdc):
            self.assertEqual(self.action(operation='key')['status'], 'NOT_VISIBLE')
            result = self.tools.execute({'action': 'harmony', 'operation': 'tap', 'x': 10, 'y': 10})
            self.assertEqual(result['status'], 'NOT_VISIBLE')
        self.assertEqual(self.keys, [])

    def test_lock_invalid_capture_unknown_owner_and_hdc_fault_still_stop(self):
        from minisweagent.exceptions import InterruptAgentFlow
        cases = [({'children': [{'attributes': {'type': 'ScreenLockRootComponent'}}]}, 'screen_locked'),
                 ({'children': [{}]}, 'application_identity'),
                 ({'children': 'invalid'}, 'layout_format')]
        for tree, kind in cases:
            with self.subTest(kind=kind):
                self.tree = tree
                with patch('dfx_agent.harmony_device.execute', side_effect=self.hdc):
                    with self.assertRaises(InterruptAgentFlow):
                        self.action()
                result = self.tools.events[-1]['result']
                self.assertEqual((result['status'], result['failure_kind']), ('BLOCKED', kind))
        self.failure = True
        with patch('dfx_agent.harmony_device.execute', side_effect=self.hdc) as commands:
            with self.assertRaises(InterruptAgentFlow):
                self.action()
            self.assertEqual(self.tools.events[-1]['result']['status'], 'BLOCKED')
            commands.assert_called_once()
        self.assertEqual(self.keys, [])

    def test_foreign_body_is_not_retained_or_returned(self):
        self.tree = {'children': [{'attributes': {'bundleName': 'foreign.app', 'text': 'FOREIGN_BODY'}}]}
        with patch('dfx_agent.harmony_device.execute', side_effect=self.hdc):
            result = self.action()
        self.assertFalse(result['target_visible_before'])
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertNotIn('FOREIGN_BODY', path.read_text(encoding='utf-8'))
                self.assertNotIn('foreign.app', path.read_text(encoding='utf-8'))

    def test_navigation_option_requires_device_execution(self):
        with self.assertRaisesRegex(ValueError, 'requires --allow-device'):
            solve(self.repo, 'fixture', self.root / 'run', responses=[], allow_system_navigation=True)

