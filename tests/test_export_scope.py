import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent import solve
from dfx_agent.task import prepare_task, export_diff
from dfx_agent.workspace import save_json


class ExportScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'app.ets').write_text('before\n', encoding='utf-8')

    def test_real_solve_command_scope_export_and_report(self):
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'exclude': ['BuildProfile.ets']})
        actions = [
            {'action': 'edit', 'path': 'app.ets', 'search': 'before', 'replace': 'after'},
            {'action': 'exec', 'command':
             "New-Item -ItemType Directory -Path module | Out-Null; "
             "Set-Content -Path module/BuildProfile.ets -Value 'generated'; "
             "Set-Content -Path diagnostic.ets -Value 'new diagnostic'"},
            {'action': 'finish', 'summary': 'Local fixture; no real model or SDK'},
        ]
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
            result = solve(self.repo, 'fixture', self.root / 'run', scope=scope, responses=actions)
        self.assertEqual(result['status'], 'Submitted')
        self.assertEqual(result['changed_files'], ['app.ets', 'diagnostic.ets'])
        self.assertEqual(result['excluded_new_files'], ['module/BuildProfile.ets'])
        self.assertNotIn('BuildProfile', (self.root / 'run/patch.diff').read_text('utf-8'))
        self.assertIn('module/BuildProfile.ets', (self.root / 'run/report.md').read_text('utf-8'))
        self.assertTrue((self.root / 'run/workspace/module/BuildProfile.ets').exists())
        events = [json.loads(x) for x in (self.root / 'run/tools/events.jsonl').read_text().splitlines()]
        self.assertEqual(events[1]['result']['returncode'], 0)
        self.assertEqual(result['api_calls'], 0)

    def test_same_name_source_is_not_globally_hidden(self):
        (self.repo / 'BuildProfile.ets').write_text('legitimate source\n', encoding='utf-8')
        out = self.root / 'run'
        prepare_task(self.repo, out, 'fixture')
        (out / 'workspace/BuildProfile.ets').write_text('changed source\n', encoding='utf-8')
        (out / 'workspace/app.ets').unlink()
        result = export_diff(out)
        self.assertEqual(result['changed_files'], ['BuildProfile.ets', 'app.ets'])
        self.assertEqual(result['excluded_new_files'], [])
        self.assertIn('deleted file mode', (out / 'patch.diff').read_text())

    def test_only_excluded_outputs_do_not_create_candidate(self):
        out = self.root / 'run'
        scope = self.root / 'scope.json'
        save_json(scope, {'repo': str(self.repo), 'exclude': ['BuildProfile.ets', 'tests']})
        prepare_task(self.repo, out, 'fixture', scope=scope)
        (out / 'workspace/BuildProfile.ets').write_text('generated\n')
        (out / 'workspace/.env').write_text('local-only-fixture')
        result = export_diff(out)
        self.assertEqual(result['changed_files'], [])
        self.assertEqual(result['excluded_new_files'], ['.env', 'BuildProfile.ets'])
        self.assertEqual((out / 'patch.diff').read_text(), '')

    def test_dependency_locks_are_classified_without_removing_patch_or_changes(self):
        (self.repo / 'oh-package-lock.json5').write_text('{"version":"old"}\n')
        out = self.root / 'run'
        prepare_task(self.repo, out, 'fixture')
        (out / 'workspace/oh-package-lock.json5').write_text('{"version":"changed"}\n')
        (out / 'workspace/module').mkdir()
        (out / 'workspace/module/oh-package-lock.json5').write_text('{"version":"new"}\n')
        result = export_diff(out)
        expected = ['module/oh-package-lock.json5', 'oh-package-lock.json5']
        self.assertEqual(result['dependency_lock_files'], expected)
        self.assertEqual(result['changed_files'], expected)
        for name in expected:
            self.assertIn(name, (out / 'patch.diff').read_text())
