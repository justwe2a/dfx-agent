"""Zero-API native-tool fixture; two CLI processes and real local tests."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from dfx_agent.workspace import read_json, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    out = (args.out or repo / 'reports' / ('runtime-demo-' + datetime.now().strftime('%Y%m%d-%H%M%S'))).resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = out / 'input'
    source.mkdir()
    (source / 'arithmetic.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    (source / 'check.py').write_text('from arithmetic import add\nassert add(2, 3) == 5\nprint("checked")\n', encoding='utf-8')
    command = "& '" + sys.executable.replace("'", "''") + "' check.py; exit $LASTEXITCODE"
    actions = [
        ('read', {'path': 'arithmetic.py'}),
        ('exec', {'command': command}),
        ('edit', {'path': 'arithmetic.py', 'search': 'a - b', 'replace': 'a + b'}),
        ('exec', {'command': command}),
        ('finish', {'summary': 'Offline fixture: observed a real failed test, resumed, edited and checked.'})]
    responses = [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
        'reasoning_content': '离线夹具推理字段，用于原样保存检查。',
        'tool_calls': [{'id': f'fixture-{i}', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}}
        for i, (name, arguments) in enumerate(actions, 1)]
    save_json(out / 'responses.json', responses)
    env = dict(os.environ, PYTHONPATH=str(repo / 'src'), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    env.pop('DEEPSEEK_API_KEY', None)
    commands = [
        ['solve', '--repo', str(source), '--task', 'Repair the arithmetic example using its check.',
         '--responses', str(out / 'responses.json'), '--out', str(out / 'run'),
         '--model-protocol', 'tool-calls', '--pause-after-tools', '2', '--max-steps', '5'],
        ['resume', '--run', str(out / 'run')]]
    for number, arguments in enumerate(commands, 1):
        result = subprocess.run([sys.executable, '-m', 'dfx_agent', *arguments],
            cwd=repo, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
        (out / f'cli-{number}.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode != (3 if number == 1 else 0):
            raise RuntimeError(f'Fixture CLI {number} failed; see cli-{number}.log')
    run = out / 'run'
    result = read_json(run / 'result.json')
    events = [json.loads(line) for line in (run/'tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
    exits = [e['result']['returncode'] for e in events if e['action']['action'] == 'exec']
    if (result['model_calls'], result['tool_calls'], result['api_calls'], exits) != (5, 5, 0, [1, 0]):
        raise RuntimeError('Unexpected cumulative counts or test results')
    summary = {'scope': 'offline_fixture', 'status': result['status'], 'api_calls': 0,
               'model_calls': 5, 'tool_events': 5, 'execution_segments': 2,
               'real_command_exits': exits, 'independent_acceptance': 'NOT_RUN',
               'previous_result': 'run/continuations/0001/result.json',
               'report': 'run/report.md'}
    save_json(out / 'summary.json', summary)
    print(json.dumps({'output': str(out), **summary}, ensure_ascii=False))


if __name__ == '__main__':
    main()
