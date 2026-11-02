"""Shared command execution and protocol-2 trusted validation adapters."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

from .workspace import confined, read_json, save_json

STAGES = ('build', 'deploy', 'replay', 'functional', 'regression')
STATUSES = {'PASS', 'FAIL', 'BLOCKED', 'NOT_RUN', 'INCONCLUSIVE'}
# Completion races and process cleanup are bounded separately from operation time.
COMPLETION_GRACE_SECONDS = 0.25
CLEANUP_WAIT_SECONDS = 15
SCRIPT_FINALIZATION_SECONDS = 35  # two cleanup waits, completion grace, receipt writing
EXECUTION_ENV = {'OS', 'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE',
                 'TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
                 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
                 'COMMONPROGRAMFILES', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
                 'LANG', 'LC_ALL', 'JAVA_HOME', 'DEVECO_SDK_HOME', 'OHOS_SDK_HOME',
                 'PYTHONIOENCODING', 'PYTHONUTF8'}


def wait_interruptibly(process, timeout):
    """Observe one process with one deadline; short waits let Python handle Ctrl+C."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0, deadline - time.monotonic())
        try:
            return process.wait(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)


def execute(argv: list[str], cwd: Path, env: dict, timeout: float, log: Path) -> dict:
    """Capture output; clean up on timeout/interruption; never use shell=True."""
    started = time.monotonic()
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with log.open('wb') as stream:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, **options)
        except OSError as exc:
            return {'status': 'BLOCKED', 'failure_kind': 'spawn', 'error': str(exc),
                    'seconds': time.monotonic() - started}
        try:
            code = wait_interruptibly(process, timeout)
            return {'returncode': code, 'seconds': time.monotonic() - started}
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            cleanup_details = {'trigger': type(exc).__name__, 'pid': process.pid,
                               'returncode_before_cleanup': process.poll()}
            # wait() can time out just as the child exits. Do not kill a PID
            # which has already completed (or claim we cleaned its descendants).
            if isinstance(exc, subprocess.TimeoutExpired) and cleanup_details['returncode_before_cleanup'] is not None:
                return {'returncode': cleanup_details['returncode_before_cleanup'],
                        'seconds': time.monotonic() - started,
                        'timeout_race': 'completed_before_cleanup', 'cleanup': cleanup_details}
            if isinstance(exc, subprocess.TimeoutExpired):
                cleanup_details['completion_grace_seconds'] = COMPLETION_GRACE_SECONDS
                try:
                    code = process.wait(timeout=COMPLETION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    # Observe natural completion before sending any termination.
                    # This does not certify descendant cleanup.
                    return {'returncode': code, 'seconds': time.monotonic() - started,
                            'timeout_race': 'completed_during_grace', 'cleanup': cleanup_details}
            try:
                if os.name == 'nt':
                    cleanup = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True, timeout=CLEANUP_WAIT_SECONDS)
                    cleanup_details.update(taskkill_returncode=cleanup.returncode,
                        taskkill_stdout=cleanup.stdout.decode(errors='replace')[-2000:],
                        taskkill_stderr=cleanup.stderr.decode(errors='replace')[-2000:],
                        returncode_after_taskkill=process.poll())
                    if cleanup.returncode != 0:
                        raise RuntimeError('taskkill did not confirm process-tree cleanup')
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                cleanup_details['returncode_after_cleanup'] = process.wait(timeout=CLEANUP_WAIT_SECONDS)
            except (OSError, subprocess.SubprocessError, RuntimeError) as cleanup_error:
                return {'status': 'BLOCKED', 'failure_kind': 'cleanup', 'cleanup_complete': False,
                        'error': str(cleanup_error), 'seconds': time.monotonic() - started,
                        'cleanup': cleanup_details}
            if isinstance(exc, KeyboardInterrupt):
                raise
            return {'status': 'BLOCKED', 'error': 'Command timed out; process tree terminated',
                    'failure_kind': 'timeout', 'cleanup_complete': True,
                    'seconds': time.monotonic() - started, 'cleanup': cleanup_details}


def validate(config: dict, workspace: Path, output: Path, binding: dict) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    stages, artifact_id = {}, None
    for stage in STAGES:
        if any(v['status'] != 'PASS' for v in stages.values()):
            stages[stage] = {'status': 'NOT_RUN', 'reason': 'Prerequisite did not pass'}
            continue
        spec = config.get(stage)
        if not spec:
            stages[stage] = {'status': 'NOT_RUN', 'reason': 'No trusted adapter registered'}
            continue
        if not isinstance(spec.get('argv'), list) or not spec['argv'] or any(not isinstance(x, str) for x in spec['argv']):
            raise ValueError(f'{stage}: argv must be a nonempty string array')
        timeout = spec.get('timeout_seconds', 120)
        if type(timeout) not in (float, int) or not 0 < timeout <= 1800:
            raise ValueError('Invalid command timeout')
        report = output / (stage + '-result.json')
        expected = {**binding, 'stage': stage}
        values = {'workspace': str(workspace), 'report': str(report), 'output': str(output),
                  'run_id': binding['run_id'], 'attempt_id': binding['attempt_id']}
        artifact = None
        if stage == 'build':
            if not spec.get('artifact'):
                raise ValueError('Build adapter requires a relative artifact path')
            artifact = confined(workspace, spec['artifact'])
            # A copied or leftover artifact must never count as a fresh build.
            if artifact.exists():
                stages[stage] = {'status': 'BLOCKED', 'reason': 'Artifact exists before build'}
                continue
        elif artifact_id:
            expected['artifact_id'] = artifact_id
        env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
        env.update({'DFX_' + k.upper(): str(v) for k, v in expected.items()})
        env['DFX_REPORT_PATH'] = str(report)
        env['DFX_WORKSPACE'] = str(workspace)
        argv = []
        for argument in spec['argv']:
            for key, value in values.items():
                argument = argument.replace('{' + key + '}', value)
            argv.append(argument)
        command = execute(argv, workspace, env, timeout, output / (stage + '.log'))
        log = output / (stage + '.log')
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 12_000))
            command['output_tail'] = stream.read().decode('utf-8', errors='replace')
        row = {'command': command, 'status': 'INCONCLUSIVE'}
        if command.get('status') == 'BLOCKED':
            row.update(status='BLOCKED', reason=command['error'])
        elif stage == 'build':
            if command['returncode'] != 0:
                row.update(status='FAIL', reason='Build command failed')
            elif not artifact.is_file() or artifact.stat().st_size == 0:
                row.update(status='INCONCLUSIVE', reason='No nonempty fresh artifact')
            else:
                artifact_id = f"{binding['run_id']}/{binding['attempt_id']}/build"
                row.update(status='PASS', artifact=str(artifact), artifact_id=artifact_id)
        elif not report.is_file():
            row.update(reason='Missing structured result; exit code alone is insufficient')
        else:
            try:
                data = read_json(report)
                if any(data.get(k) != v for k, v in expected.items()):
                    raise ValueError('Result is not bound to this run/attempt/patch/artifact')
                if data.get('status') not in STATUSES:
                    raise ValueError('Unknown check status')
                if data['status'] == 'PASS' and command['returncode'] != 0:
                    raise ValueError('PASS conflicts with nonzero command exit')
                row.update(status=data['status'], details=data.get('details', {}))
            except (ValueError, OSError, TypeError, AttributeError) as exc:
                row.update(status='INCONCLUSIVE', reason=str(exc))
        stages[stage] = row
        save_json(output / 'checks.json', {'binding': binding, 'stages': stages})
    result = {'binding': binding, 'stages': stages, 'artifact_id': artifact_id,
              'all_passed': all(stages[s]['status'] == 'PASS' for s in STAGES)}
    save_json(output / 'checks.json', result)
    return result

