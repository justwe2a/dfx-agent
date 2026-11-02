"""Zero-API shared-interface demo with a local fake build executable, no SDK/device."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


def main():
    repo = Path(__file__).resolve().parents[1]
    output = repo / 'reports' / ('harmony-tools-demo-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    source = output / 'input-project'
    source.mkdir(parents=True)
    (source / 'README.md').write_text('Local interface fixture, not a HarmonyOS application.\n', encoding='utf-8')
    toolchain = output / 'fixture toolchain'
    tool = toolchain / 'tools/hvigor/bin/hvigorw.bat'
    tool.parent.mkdir(parents=True)
    with zipfile.ZipFile(tool.parent / 'fixture.hap', 'w') as archive:
        archive.writestr('module.json', json.dumps({'app': {'bundleName': 'example.app'}, 'module': {'name': 'entry'}}))
    tool.write_text('@echo off\necho %* | findstr /L /C:"module=broken@default" >nul\n'
                    'if not errorlevel 1 (\necho fixture compiler failure\nexit /b 7\n)\n'
                    'if not exist entry\\build mkdir entry\\build\n'
                    f'copy /y "{tool.parent / "fixture.hap"}" entry\\build\\app.hap >nul\n'
                    'echo fixture build complete\nexit /b 0\n', encoding='ascii')
    host = output / 'host.json'
    host.write_text(json.dumps({'deveco': str(toolchain), 'device': 'fixture-not-a-device'}), encoding='utf-8')
    build = {'action': 'harmony', 'operation': 'build', 'module': 'entry', 'product': 'default',
             'target': 'default', 'artifact': 'entry/build/app.hap'}
    actions = [
        {'action': 'harmony', 'operation': 'info'},
        {**build, 'module': 'broken'}, build,
        {'action': 'harmony', 'operation': 'install', 'artifact': build['artifact']},
        {'action': 'finish', 'summary': 'Shared-interface fixture only; real SDK, device and model unverified',
         'unverified': ['vendor build', 'device behavior', 'real model autonomy']},
    ]
    responses = output / 'actions.json'
    responses.write_text(json.dumps(actions, indent=2), encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(repo / 'src'), PYTHONUTF8='1')
    env.pop('DEEPSEEK_API_KEY', None)
    process = subprocess.run([sys.executable, '-m', 'dfx_agent', 'solve', '--repo', str(source),
        '--task', 'Check local shared interfaces only; no device or network.', '--responses', str(responses),
        '--host', str(host), '--out', str(output / 'run')], cwd=repo, env=env,
        capture_output=True, encoding='utf-8', timeout=60)
    (output / 'cli.stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'cli.stderr.txt').write_text(process.stderr, encoding='utf-8')
    if process.returncode:
        raise RuntimeError(f'CLI failed; inspect {output}')
    result = json.loads((output / 'run/result.json').read_text(encoding='utf-8'))
    events = [json.loads(line) for line in (output / 'run/tools/events.jsonl').read_text(encoding='utf-8').splitlines()]
    codes = [e['result'].get('returncode') for e in events if e['action'].get('operation') == 'build']
    paused = events[3]['result']
    if (codes != [7, 0] or paused['status'] != 'TOOL_ERROR'
            or paused.get('error') != 'Device operations are paused; no HDC command dispatched'
            or events[0]['result']['device_enabled'] is not False or result['api_calls'] != 0):
        raise AssertionError('Unexpected local tool observations')
    summary = {'output': str(output), 'status': 'LOCAL_INTERFACE_CHECK_PASSED',
               'evaluation_scope': 'offline_fixture', 'build_executable': 'synthetic batch fixture',
               'command_exit_codes': codes, 'api_calls': result['api_calls'], 'device_commands': 0,
               'model_turns': result['model_calls'], 'real_model_autonomy': 'NOT_TESTED',
               'vendor_build': 'NOT_RUN', 'device': 'NOT_RUN', 'repair_validated': result['repair_validated']}
    (output / 'check.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
