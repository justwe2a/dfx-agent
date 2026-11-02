"""Exercise the actual local HTTP adapter, CLI child and command process tree."""
import ctypes
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from dfx_agent.workbench import Workbench, create_app, total_tokens


def native(actions):
    return [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': f'call_{i}', 'type': 'function', 'function': {'name': a['action'],
                        'arguments': json.dumps({k: v for k, v in a.items() if k != 'action'})}}]}}
        for i, a in enumerate(actions)]


class WorkbenchTests(unittest.TestCase):
    def test_provider_usage_list_is_complete_or_unknown(self):
        self.assertEqual(total_tokens({'api_calls': 2, 'usage': [
            {'total_tokens': 7}, {'total_tokens': 9}]}), 16)
        self.assertEqual(total_tokens({'api_calls': 0, 'usage': []}), 0)
        for result in (None, {}, {'api_calls': 2, 'usage': [{'total_tokens': 7}]},
                       {'api_calls': 1, 'usage': [None]},
                       {'api_calls': 1, 'usage': [{'total_tokens': -1}]}):
            self.assertIsNone(total_tokens(result))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dfx-ui-中文 ')
        self.root = Path(self.temp.name)
        self.app = create_app(self.root / '工作台 records')
        self.manager = self.app.state.manager
        self.client = TestClient(self.app, base_url='http://127.0.0.1',
                                 headers={'Origin': 'http://127.0.0.1'})
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for child in self.manager.children.values():
            child.wait(timeout=45)
        # Let the observer finish writing the index, not just the child exit.
        with self.manager.lock:
            pass
        self.temp.cleanup()

    def post(self, path, value):
        response = self.client.post('/api' + path, json=value)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_result(self, key, seconds=25):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            response = self.client.get('/api/runs/' + key)
            self.assertEqual(response.status_code, 200, response.text)
            row = response.json()
            if row['status'] not in ('Running', 'Stopping'):
                return row
            time.sleep(.1)
        self.fail('Task did not reach terminal state')

    def demo(self):
        return self.post('/demo', {})

    def test_real_native_roundtrip_results_export_and_restart(self):
        task = self.demo()
        original = (Path(task['repo']) / 'calc.py').read_bytes()
        key = self.post('/runs', task)['id']
        row = self.wait_result(key)
        self.assertEqual(row['status'], 'Submitted')
        self.assertEqual(row['result']['api_calls'], 0)
        self.assertEqual(row['independent']['status'], 'NOT_RUN')
        self.assertFalse(row['result']['repair_validated'])
        self.assertEqual((Path(task['repo']) / 'calc.py').read_bytes(), original)
        events = self.client.get(f'/api/runs/{key}/events').json()
        self.assertEqual([e['result']['returncode'] for e in events['events']
                          if e['action']['action'] == 'exec'], [1, 0])
        self.assertEqual(self.client.get(f'/api/runs/{key}/events?cursor={events["cursor"]}').json()['events'], [])
        diff = self.client.get(f'/api/runs/{key}/file', params={'path': 'patch.diff'}).json()['text']
        self.assertIn('+    return a + b', diff)
        summary = self.post(f'/runs/{key}/export', {})
        self.assertEqual(summary['independent']['status'], 'NOT_RUN')
        self.assertEqual(self.client.get(f'/api/runs/{key}/summary').status_code, 200)
        fresh = Workbench(self.manager.root)
        self.assertEqual(fresh.list_runs()[0]['status'], 'Submitted')
        self.assertEqual(len(fresh.rows), 1)
        command = json.loads((Path(row['job']) / 'command.json').read_text(encoding='utf-8'))
        self.assertEqual(command[command.index('--thinking') + 1], 'enabled')
        self.assertEqual(command[command.index('--reasoning-effort') + 1], 'high')
        self.assertNotIn('--provider', command)
        self.assertEqual(len(self.manager.children), 1)
        self.assertEqual(self.post('/import', {'path': row['run']})['id'], key)

    @unittest.skipUnless(os.name == 'nt', 'Windows child-process cleanup')
    def test_stop_active_command_kills_descendant_and_does_not_replay(self):
        task = self.demo()
        source = Path(task['repo'])
        (source / 'long.py').write_text('import subprocess, sys, time\nfrom pathlib import Path\n'
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(45)"])\n'
            'Path("child.txt").write_text(str(child.pid))\n'
            'time.sleep(45)\nPath("unexpected.txt").write_text("finished")\n', encoding='utf-8')
        command = "& '" + sys.executable.replace("'", "''") + "' long.py; exit $LASTEXITCODE"
        Path(task['responses']).write_text(json.dumps(native([
            {'action': 'exec', 'command': command},
            {'action': 'finish', 'summary': 'Must not execute after stop'}])), encoding='utf-8')
        key = self.post('/runs', task)['id']
        row = self.manager.row(key)
        child_file = Path(row['run']) / 'workspace/child.txt'
        until = time.monotonic() + 20
        while not child_file.exists() and time.monotonic() < until:
            time.sleep(.1)
        self.assertTrue(child_file.exists())
        self.assertEqual(self.client.post('/api/runs', json=task).status_code, 409)
        pid = int(child_file.read_text())
        self.post(f'/runs/{key}/stop', {})
        done = self.wait_result(key, seconds=20)
        self.assertEqual(done['status'], 'Interrupted', done)
        self.assertEqual(done['result']['api_calls'], 0)
        self.assertFalse((Path(row['run']) / 'workspace/unexpected.txt').exists())
        events = self.client.get(f'/api/runs/{key}/events').json()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['result']['status'], 'INTERRUPTED')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0, 'Descendant still alive')
            finally:
                kernel.CloseHandle(handle)

    def test_offline_and_device_boundaries_and_input_errors(self):
        task = self.demo()
        for change in ({'mode': 'deepseek'}, {'allow_device': True}):
            self.assertEqual(self.client.post('/api/runs', json={**task, **change}).status_code, 409)
        self.assertEqual(self.client.post('/api/runs', json={**task, 'task': '  '}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json={**task, 'repo': 'relative'}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json={**task, 'max_steps': 0}).status_code, 422)
        self.assertEqual(len(self.manager.children), 0)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'never-send-this-secret'}):
            response = self.client.get('/api/config')
            self.assertTrue(response.json()['api_key_configured'])
            self.assertNotIn('never-send-this-secret', response.text)

    def test_origin_host_and_artifact_boundary(self):
        self.assertEqual(self.client.post('/api/demo', json={}, headers={'Origin': 'https://elsewhere.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/config', headers={'Host': 'elsewhere.invalid'}).status_code, 403)
        key = self.post('/runs', self.demo())['id']
        self.wait_result(key)
        response = self.client.get(f'/api/runs/{key}/file', params={'path': '../task.txt'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(f'/api/runs/{key}/tree').status_code, 200)

    def test_partial_events_kept_until_newline_and_bad_record_not_hidden(self):
        run = self.root / 'saved'
        (run / 'tools').mkdir(parents=True)
        self.manager.rows.append({'id': 'sample', 'run': str(run)})
        path = run / 'tools/events.jsonl'
        path.write_bytes('{"内容":"中文"}'.encode())
        self.assertEqual(self.manager.events('sample', 0),
                         {'events': [], 'cursor': 0, 'pending': True, 'has_more': False})
        with path.open('ab') as f:
            f.write(b'\n')
        self.assertEqual(self.manager.events('sample', 0)['events'], [{'内容': '中文'}])
        path.write_bytes(b'{broken}\n')
        self.assertEqual(self.client.get('/api/runs/sample/events').status_code, 400)

    def test_terminal_events_pagination_keeps_every_record_once(self):
        run = self.root / 'terminal'
        (run / 'tools').mkdir(parents=True)
        self.manager.rows.append({'id': 'terminal', 'run': str(run)})
        path = run / 'tools/events.jsonl'
        for count in (0, 50, 75, 100, 125):
            with self.subTest(count=count):
                path.write_text(''.join(json.dumps({'number': n, 'text': '中文'}, ensure_ascii=False) + '\n'
                                        for n in range(count)), encoding='utf-8')
                cursor, received, pages = 0, [], 0
                while True:
                    response = self.client.get(f'/api/runs/terminal/events?cursor={cursor}')
                    self.assertEqual(response.status_code, 200)
                    batch = response.json()
                    received.extend(e['number'] for e in batch['events'])
                    self.assertFalse(batch['pending'])
                    pages += 1
                    if not batch['has_more']:
                        break
                    self.assertGreater(batch['cursor'], cursor)
                    cursor = batch['cursor']
                    self.assertLess(pages, 4)
                self.assertEqual(received, list(range(count)))
                self.assertEqual(pages, max(1, (count + 49) // 50))

    def test_partial_or_malformed_tail_after_full_page_does_not_loop_or_disappear(self):
        run = self.root / 'tail'
        (run / 'tools').mkdir(parents=True)
        self.manager.rows.append({'id': 'tail', 'run': str(run)})
        path = run / 'tools/events.jsonl'
        prefix = b'{"number":1}\n' * 50
        path.write_bytes(prefix + '{"text":"中文"}'.encode('utf-8'))
        first = self.client.get('/api/runs/tail/events').json()
        self.assertTrue(first['has_more'])
        tail_url = f'/api/runs/tail/events?cursor={first["cursor"]}'
        tail = self.client.get(tail_url).json()
        self.assertTrue(tail['pending'])
        self.assertFalse(tail['has_more'])
        self.assertEqual(tail['cursor'], len(prefix))
        self.assertEqual(tail['events'], [])
        path.write_bytes(prefix + b'{broken}\n')
        self.assertEqual(self.client.get(tail_url).status_code, 400)

    def test_missing_result_is_not_success_and_unknown_worker_blocks_dispatch(self):
        job = self.root / 'gone'
        job.mkdir()
        row = {'id': 'gone', 'run': str(job / 'run'), 'job': str(job)}
        self.manager.rows.append(row)
        self.assertEqual(self.manager.status(row), 'Unobserved')
        self.assertEqual(self.client.post('/api/runs', json=self.demo()).status_code, 409)
        (job / 'exit.json').write_text('{"returncode": 2}', encoding='utf-8')
        self.assertEqual(self.manager.status(row), 'ExitedWithoutResult')
        (job / 'exit.json').unlink()
        row['returncode'] = 2
        self.assertEqual(self.manager.status(row), 'ExitedWithoutResult')
        self.assertEqual(self.client.post('/api/runs/gone/stop', json={}).status_code, 409)

    def test_mismatched_evaluation_does_not_override_result(self):
        key = self.post('/runs', self.demo())['id']
        self.wait_result(key)
        evaluation = self.root / 'evaluation'
        evaluation.mkdir()
        (evaluation / 'result.json').write_text(json.dumps({'binding': {}, 'mode': 'wrong'}), encoding='utf-8')
        self.assertEqual(self.client.post(f'/api/runs/{key}/export',
                                         json={'evaluation': str(evaluation)}).status_code, 400)
        row = self.client.get('/api/runs/' + key).json()
        self.assertEqual(row['independent']['status'], 'NOT_RUN')
        self.assertEqual(row['result']['status'], 'Submitted')

    def test_static_assets_and_scope_preview(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/app.js').status_code, 200)
        scope = self.root / 'scope.json'
        scope.write_text('{"repo":"example","outbound_approved":false,"read_only":["data/**"]}', encoding='utf-8')
        self.assertFalse(self.post('/scope', {'path': str(scope)})['outbound_approved'])

    def test_wait_observes_one_total_deadline(self):
        from dfx_agent.validation import wait_interruptibly
        clock, waits = [0.0], []
        process = Mock(args=['fixture'])
        def wait(timeout):
            waits.append(timeout)
            clock[0] += timeout
            raise subprocess.TimeoutExpired(process.args, timeout)
        process.wait.side_effect = wait
        with patch('dfx_agent.validation.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(subprocess.TimeoutExpired) as failure:
                wait_interruptibly(process, 1.0)
        self.assertEqual(failure.exception.timeout, 1.0)
        self.assertAlmostEqual(sum(waits), 1.0)
        self.assertTrue(all(0 <= interval <= .2 for interval in waits))

    def test_partial_live_result_is_explicit_but_corrupt_terminal_is_error(self):
        run = self.root / 'partial'
        run.mkdir()
        (run / 'result.json').write_text('{', encoding='utf-8')
        self.manager.rows.append({'id': 'partial', 'run': str(run)})
        child = Mock()
        child.poll.return_value = None
        self.manager.children['partial'] = child
        try:
            value = self.client.get('/api/runs/partial').json()
            self.assertEqual(value['records_being_written'], ['result.json'])
            self.assertIsNone(value['result'])
            child.poll.return_value = 1
            self.assertEqual(self.client.get('/api/runs/partial').status_code, 400)
        finally:
            del self.manager.children['partial']


if __name__ == '__main__':
    unittest.main()
