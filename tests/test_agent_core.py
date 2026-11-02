import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dfx_agent.agent import make_agent
from dfx_agent.model import MiniModel
from dfx_agent.tools import ProjectTools


class CoreTests(unittest.TestCase):
    def test_exec_git_does_not_discover_parent_repo(self):
        if not shutil.which('git'):
            self.skipTest('Git is required for the real repository-discovery check')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'host-only.txt').write_text('outside project', encoding='utf-8')
            workspace = root / 'run' / 'workspace'
            (workspace / 'subdir').mkdir(parents=True)
            tools = ProjectTools(workspace, root / 'tools')
            for cwd in ('.', 'subdir'):
                observation = tools.execute({
                    'action': 'exec', 'cwd': cwd,
                    'command': 'git status --porcelain; exit $LASTEXITCODE'})
                self.assertNotEqual(observation['returncode'], 0)
                self.assertIn('not a git repository', observation['output'])
                self.assertNotIn('host-only.txt', observation['output'])
            # A repository created inside the project remains usable.
            subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
            (workspace / 'project.txt').write_text('inside', encoding='utf-8')
            observation = tools.execute({'action': 'exec', 'cwd': 'subdir',
                                         'command': 'git status --porcelain; exit $LASTEXITCODE'})
            self.assertEqual(observation['returncode'], 0)
            self.assertIn('project.txt', observation['output'])
            self.assertNotIn('host-only.txt', observation['output'])

    def test_exec_reads_and_rewrites_utf8_chinese_without_mojibake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            text = "日期间隔：中文注释\nexport const label = '查询';\n"
            (workspace / 'sample.ets').write_text(text, encoding='utf-8')
            tools = ProjectTools(workspace, root / 'tools')
            observation = tools.execute({
                'action': 'exec',
                'command': ("$s=Get-Content -Raw sample.ets; "
                            "Set-Content -LiteralPath copy.ets -Value $s -NoNewline -Encoding utf8; "
                            "Get-Content sample.ets")})
            self.assertEqual(observation['returncode'], 0)
            self.assertIn('日期间隔：中文注释', observation['output'])
            self.assertEqual((workspace / 'copy.ets').read_text(encoding='utf-8-sig'), text)

    def test_upstream_loop_observes_real_powershell_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            model = MiniModel(root / 'model', responses=[
                {'action': 'exec', 'command': "Set-Content -LiteralPath proof.txt -Value 'real-tool'; Get-Content proof.txt"},
                {'action': 'finish', 'summary': 'offline core check'},
            ])
            tools = ProjectTools(workspace, root / 'tools')
            agent = make_agent(model, tools, root, 'Return a JSON action.')
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')):
                result = agent.run('Write a local proof file')
            from minisweagent.agents.default import DefaultAgent
            self.assertIsInstance(agent, DefaultAgent)
            self.assertIs(type(agent).run, DefaultAgent.run)
            self.assertIs(type(agent).step, DefaultAgent.step)
            self.assertEqual(result['exit_status'], 'Submitted')
            self.assertEqual((workspace / 'proof.txt').read_text().strip(), 'real-tool')
            second = json.loads((root / 'model/0002/request.json').read_text())
            self.assertIn('real-tool', second[-2]['content'])
            self.assertIn('returncode', second[-2]['content'])
            self.assertEqual(model.api_calls, 0)
            trajectory = json.loads((root / 'trajectory.json').read_text())
            self.assertEqual(trajectory['info']['model_stats']['api_calls'], 0)
