"""Submission fixes through real tools/export; provider and device are never called."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from dfx_agent.harmony_tools import HarmonyTools
from dfx_agent.environment_check import run_preflight
from dfx_agent.member_tasks import MemberTasks
from dfx_agent.tool_schema import tool_definitions, validate_arguments
from dfx_agent.tools import ProjectTools


class SubmissionFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_errors_visible_after_many_warnings_with_original_log_preserved(self):
        repo = self.root / 'repo'; repo.mkdir()
        adapter = HarmonyTools(repo, self.root / 'tools', {'deveco': str(self.root / 'deveco'), 'device': ''})
        ProjectTools(repo, self.root / 'tools', harmony=adapter)
        raw = '\n'.join(['warning duplicate resource'] * 30 +
                        ['spawn java ENOENT', 'java executable not found', 'BUILD FAILED'])
        def execute(argv, cwd, env, timeout, log):
            log.write_text(raw, encoding='utf-8')
            return {'returncode': 1}
        with patch('dfx_agent.harmony_tools.execute', side_effect=execute):
            result = adapter.execute({'operation': 'build', 'module': 'entry', 'product': 'default',
                                      'target': 'default', 'artifact': 'entry.hap'}, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['diagnostics'][0], 'spawn java ENOENT')
        self.assertEqual(result['diagnostic_lines'][0], 31)
        self.assertIn('java executable not found', result['diagnostics'])
        self.assertEqual(result['warning_count'], 30)
        self.assertTrue(result['warnings_truncated'])
        self.assertEqual((adapter.output / result['command_log']).read_text(encoding='utf-8'), raw)

    def test_resume_receipts_are_exported_without_temporary_or_unrelated_files(self):
        job = self.root / 'jobs' / 'offline'; run = job / 'run'; run.mkdir(parents=True)
        (self.root / 'prepared' / 'offline').mkdir(parents=True)
        names = ['command.json', 'exit.json', 'resume-0001-command.json', 'resume-0001-exit.json',
                 'resume-0002-command.json', 'resume-0002-exit.json']
        for name in names: (job / name).write_text('{}', encoding='utf-8')
        (job / 'resume-private.json').write_text('{}', encoding='utf-8')
        (run / 'result.json').write_text('{"changed_files":[]}', encoding='utf-8')
        manager = MemberTasks(self.root)
        manager.list_tasks = lambda runs: {'settings': {'member': 'member1'}, 'members': ['member1'],
            'tasks': [{'order': 1, 'task_id': 'synthetic/check', 'title': 'check', 'attempts': [], 'feedback': {}}]}
        path = manager.export([{'id': 'offline', 'status': 'Submitted', 'assignment_id': 'synthetic/check',
                                'job': str(job), 'run': str(run), 'prepared_id': 'offline'}])
        with zipfile.ZipFile(path) as archive:
            for name in names: self.assertIn('tasks/01/offline/job/' + name, archive.namelist())
            self.assertFalse(any('private' in n for n in archive.namelist()))

    def test_schema_and_runtime_search_and_read_boundaries_agree(self):
        repo = self.root / 'repo'; repo.mkdir()
        (repo / 'sample.txt').write_text('\n'.join(['needle'] * 220), encoding='utf-8')
        tools = ProjectTools(repo, self.root / 'tools')
        definitions = tool_definitions(harmony=True)
        for name, args in [('search', {'term': 'needle', 'limit': 50}),
                           ('read', {'path': 'sample.txt', 'start': 1, 'end': 200})]:
            validate_arguments(name, args, definitions)
            self.assertNotEqual(tools.file_action({'action': name, **args}).get('status'), 'TOOL_ERROR')
        for name, args in [('search', {'term': 'needle', 'limit': 51}),
                           ('read', {'path': 'sample.txt', 'start': 1, 'end': 201})]:
            with self.assertRaises(ValueError): validate_arguments(name, args, definitions)
            with self.assertRaises(ValueError): tools.file_action({'action': name, **args})
        validate_arguments('harmony', {'operation': 'layout', 'limit': 80}, definitions)
        with self.assertRaises(ValueError): validate_arguments('harmony', {'operation': 'layout', 'limit': 81}, definitions)

    def test_invalid_workspace_stops_before_vendor_or_device_operations(self):
        repo = self.root / '中文运行目录'; repo.mkdir()
        adapter = HarmonyTools(repo, self.root / 'tools', {'deveco': str(self.root / 'deveco'), 'device': ''})
        tools = ProjectTools(repo, self.root / 'tools', harmony=adapter)
        with patch('dfx_agent.environment_check.os.name', 'nt'), \
             patch('dfx_agent.member_tasks.check_deveco', side_effect=AssertionError('vendor must not run')), \
             patch('dfx_agent.environment_check.execute', side_effect=AssertionError('device must not run')):
            result = run_preflight(adapter, tools)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(result['model_dispatched'])
        self.assertEqual(result['commands'], [])
        self.assertIn('中文运行目录', result['error'])


if __name__ == '__main__': unittest.main()
