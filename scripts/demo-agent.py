"""Zero-API integration demonstration. Actions are fixtures, not model decisions."""
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-protocol', choices=['json-actions', 'tool-calls'], default='json-actions')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = repo / 'reports' / ('agent-demo-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    source = output / 'input-project'
    source.mkdir(parents=True)
    (source / 'arithmetic.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    (source / 'check.py').write_text('from arithmetic import add\nassert add(2, 3) == 5\nprint("checks passed")\n', encoding='utf-8')
    command = "& '" + sys.executable.replace("'", "''") + "' check.py; exit $LASTEXITCODE"
    actions = [
        {'action': 'read', 'path': '.'},
        {'action': 'search', 'term': 'def add'},
        {'action': 'read', 'path': 'arithmetic.py'},
        {'action': 'edit', 'path': 'arithmetic.py', 'search': 'a - b', 'replace': 'a * b'},
        {'action': 'exec', 'command': command},
        {'action': 'read', 'path': 'arithmetic.py'},
        {'action': 'edit', 'path': 'arithmetic.py', 'search': 'a * b', 'replace': 'a + b'},
        {'action': 'exec', 'command': command},
        {'action': 'finish', 'summary': 'Offline fixture exercised search, persistent edit, real failure and revision.',
         'root_cause': {'path': 'arithmetic.py', 'line': 2}, 'unverified': ['real model autonomy', 'HarmonyOS']},
    ]
    if args.model_protocol == 'tool-calls':
        # Explicitly authored offline fixtures, never conversions of a live response.
        actions = [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': f'fixture_{index}', 'type': 'function', 'function': {
                        'name': action['action'], 'arguments': json.dumps(
                            {k: v for k, v in action.items() if k != 'action'}, ensure_ascii=False)}}]}}
                   for index, action in enumerate(actions, 1)]
    fixture = output / 'actions.json'
    fixture.write_text(json.dumps(actions, indent=2), encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(repo / 'src'), PYTHONUTF8='1')
    # No key is necessary in this fixture process or its tools.
    env.pop('DEEPSEEK_API_KEY', None)
    process = subprocess.run([sys.executable, '-m', 'dfx_agent', 'solve', '--repo', str(source),
        '--task', 'Fix arithmetic behavior using existing checks.', '--responses', str(fixture),
        '--model-protocol', args.model_protocol,
        '--out', str(output / 'run')], cwd=repo, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
    (output / 'cli.stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'cli.stderr.txt').write_text(process.stderr, encoding='utf-8')
    if process.returncode:
        raise RuntimeError(f'CLI failed ({process.returncode}); see {output}')
    result = json.loads((output / 'run/result.json').read_text(encoding='utf-8'))
    events = [json.loads(line) for line in (output / 'run/tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
    codes = [e['result'].get('returncode') for e in events if e['action']['action'] == 'exec']
    if codes != [1, 0] or result['api_calls'] != 0 or result['repair_validated']:
        raise AssertionError('Expected real failure then success, zero API, no repair certification')
    if args.model_protocol == 'tool-calls':
        messages = json.loads((output / 'run/trajectory.json').read_text(encoding='utf-8'))['messages']
        if [m['role'] for m in messages[-3:]] != ['assistant', 'tool', 'exit']:
            raise AssertionError('Native finish must leave a matching tool receipt before exit')
        if messages[-2]['tool_call_id'] != messages[-3]['tool_calls'][0]['id']:
            raise AssertionError('Finish receipt ID does not match')
    summary = {'output': str(output), 'status': 'MECHANISM_CHECK_PASSED', 'evaluation_scope': 'offline_fixture',
               'model_protocol': args.model_protocol,
               'api_calls': 0, 'command_exit_codes': codes, 'model_turns': result['model_calls'],
               'real_model_autonomy': 'NOT_TESTED', 'device': 'NOT_RUN'}
    (output / 'check.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == '__main__':
    main()
