"""Execute one explicitly approved, fixed serial batch. Stops on a blocked trial; no automatic restart."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from dfx_agent import runner
from dfx_agent.comparison import write_comparison
from dfx_agent.context_review import verify_approval
from dfx_agent.localization_eval import evaluate
from dfx_agent.model import DeepSeekModel
from dfx_agent.run_report import export_run
from dfx_agent.validation import EXECUTION_ENV, execute, STAGES
from dfx_agent.workspace import load_case, read_json, save_json


def commit():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()


def store(folder, batch):
    save_json(folder / 'batch.json', batch)
    write_comparison(folder)


def completed_format_error(state):
    calls = state.get('provider_calls', [])
    last = calls[-1] if calls else {}
    return (state['status'] == 'WAITING_MODEL' and last.get('submitted') is True
            and last.get('finish_reason') == 'stop' and last.get('requested_model') == last.get('returned_model')
            and last.get('returned_model') is not None
            and last.get('error_type') in {'JSONDecodeError', 'ValueError'})


def drive_comparison(run, model):
    state = runner.drive(run, model, True)
    while completed_format_error(state):
        # A complete but malformed answer consumes the ordinary response opportunity.
        # B0 reaches its limit without another query; B1 uses its existing feedback/cap.
        state['feedback'].append({'stage': 'proposal_or_retrieval', 'error': 'Invalid structured model content'})
        runner.persist(run, state, 'response_rejected', reason='Completed response has invalid structured content')
        state = runner.drive(run, model, True)
    return state


def prepare(plan_path, host, folder):
    plan = read_json(plan_path)
    if plan['approval_status'] != 'APPROVED' or not plan['execution_authorized'] or plan['selected_repeats'] != 3:
        raise ValueError('This batch requires the recorded three-repeat approval')
    folder.mkdir(parents=True, exist_ok=False)
    batch = {'batch_id': folder.name, 'plan': plan, 'execution_commit': commit(), 'status': 'PREPARING',
             'started_at': runner.now(), 'baselines': {}, 'trials': []}
    for pair, (case, first, second) in enumerate(plan['pair_schedule'], 1):
        for arm in (first, second):
            batch['trials'].append({'index': len(batch['trials']) + 1, 'pair': pair, 'repeat': (pair - 1) // 3 + 1,
                                    'case': case, 'arm': arm, 'outcome': 'NOT_RUN'})
    store(folder, batch)
    for item in plan['cases']:
        config = load_case(REPO / item['config'], host)
        if config['source_version'] != item['source_version']:
            raise ValueError('Source version differs from approved plan')
        verify_approval(config)
        config.pop('baseline_report', None)
        path = folder / 'configs' / (item['name'] + '-baseline.json')
        save_json(path, config)
        run = runner.prepare(path, REPO / 'runs/workflow')
        batch['baselines'][item['name']] = str(run)
        store(folder, batch)
        state = runner.run_baseline(run)
        export_run(run, folder / 'baseline-reports' / item['name'])
        print('BASELINE', item['name'], run.name, state['baseline_status'], flush=True)
        if state['baseline_status'] != 'REPRODUCED':
            batch.update(status='PAUSED', reason='Baseline prerequisite blocked: ' + item['name'])
            store(folder, batch)
            return 3
        base_config = read_json(run / 'case.json')
        request = runner.request_for(run, base_config, state)
        request.pop('instructions')
        save_json(folder / 'initial-evidence' / (item['name'] + '.json'), request)
    batch['status'] = 'READY'
    store(folder, batch)
    return 0


def restore_original(base_run, trial_run, config):
    """Reuse the known original HAP; reset via the same baseline trigger before either arm."""
    output = trial_run / 'precondition'
    output.mkdir()
    proof = read_json(base_run / 'baseline-proof.json')
    workspace = base_run / 'original-verification/workspace'
    binding = {'run_id': trial_run.name, 'attempt_id': 'precondition', 'snapshot_id': config['source_version'],
               'patch_id': 'NONE_BASELINE', 'artifact_id': proof['artifact_id']}
    with runner.execution_lock(trial_run.parent):
        for stage in ('deploy', 'replay'):
            expected = {**binding, 'stage': stage}
            report = output / (stage + '-result.json')
            env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
            env.update({'DFX_' + k.upper(): str(v) for k, v in expected.items()})
            env.update(DFX_REPORT_PATH=str(report), DFX_WORKSPACE=str(workspace))
            spec = config['baseline_validation'][stage]
            command = execute(spec['argv'], workspace, env, spec['timeout_seconds'], output / (stage + '.log'))
            result = read_json(report) if report.exists() else {'status': 'BLOCKED', 'details': {'reason': 'Missing precondition report'}}
            save_json(output / (stage + '-command.json'), command)
            if command.get('returncode') != 0 or result.get('status') != 'PASS' or any(result.get(k) != v for k, v in expected.items()):
                return False
            if stage == 'replay' and result.get('details', {}).get('baseline_status') != 'REPRODUCED':
                return False
    return True


def measurement(run, report_dir, case):
    result = read_json(run / 'result.json')
    exported = export_run(run, report_dir)
    if exported['issues']:
        raise ValueError('Run report has inconsistent evidence')
    report = read_json(report_dir / 'report.json')
    events = [json.loads(line) for line in (run / 'trajectory.jsonl').read_text(encoding='utf-8').splitlines()]
    first_proposal_call, calls = None, 0
    for event in events:
        calls += event['event'] == 'model_call_started'
        if event['event'] == 'response_processed' and event.get('action') == 'propose':
            first_proposal_call = calls
            break
    actions = [e.get('action') for e in events if e['event'] == 'response_processed']
    providers = result['provider_calls']
    def usage_sum(key):
        values = [(p.get('usage') or {}).get(key) for p in providers]
        return sum(values) if all(type(v) is int for v in values) else None
    expected = {'Calendar1': {'replay': 1, 'functional': 5, 'regression': 5},
                'FitnessCenter': {'replay': 1, 'functional': 6, 'regression': 13},
                'BookRead2': {'replay': 1, 'functional': 2, 'regression': 5}}[case]
    details = copy.deepcopy(report['selected_validation'])
    for stage, count in expected.items():
        details[stage]['registered_assertions'] = count
        details[stage]['assertions_not_completed'] = count - (details[stage]['assertion_count'] or 0)
    measure = {'requests': result['paid_api_calls'], 'tokens': result['usage']['total_tokens'] if providers else 0,
               'input_tokens': result['usage']['input_tokens'], 'output_tokens': result['usage']['output_tokens'],
               'cached_input_tokens': usage_sum('prompt_cache_hit_tokens'), 'actual_cost': None,
               'candidate_count': len(result['attempts']), 'response_rejections': sum(e['event'] == 'response_rejected' for e in events),
               'read_actions': actions.count('read'), 'search_actions': actions.count('search'),
               'first_candidate_initial_passed': report['first_candidate_initial_passed'],
               'first_response_passed': first_proposal_call == 1 and report['first_candidate_initial_passed'],
               'timing': report['timing'], 'model_seconds': sum(p.get('seconds', 0) for p in providers),
               'validation_stage_executions': {s: sum(a.get('validation', {}).get('stages', {}).get(s, {}).get('status', 'NOT_RUN') != 'NOT_RUN' for a in result['attempts']) for s in STAGES},
               'selected_validation': details, 'report': str(report_dir / 'report.md'),
               'returned_models': [p.get('returned_model') for p in providers]}
    if case == 'FitnessCenter':
        labels = REPO / 'resources/evaluation/fitnesscenter.labels.json'
        measure['localization_first'] = evaluate(run, labels, 1)
        measure['localization_final'] = evaluate(run, labels, result.get('validated_attempt') or max(1, len(result['attempts'])))
    else:
        measure['localization'] = 'NOT_SCORED: no confirmed independent root-cause label'
    return measure


def resume_start(folder, batch, approval_path):
    """Explicit continuation after one approved zero-API recheck; primary rows stay unchanged."""
    approval = read_json(approval_path)
    if (batch['status'] != 'PAUSED' or approval.get('approved') is not True
            or approval.get('batch_id') != batch['batch_id']
            or approval.get('original_execution_commit') != batch['execution_commit']
            or approval.get('resume_commit') != commit()):
        raise ValueError('Resume requires matching approval, paused batch and frozen commits')
    start = approval['preserve_through']
    rows = batch['trials']
    if (type(start) is not int or not 0 < start < len(rows)
            or any(r['outcome'] not in {'PASS', 'MODEL_FAILURE', 'BLOCKED'} for r in rows[:start])
            or any(r['outcome'] != 'NOT_RUN' for r in rows[start:])):
        raise ValueError('Resume may only continue an untouched suffix of registered trials')
    limit = sum(batch['plan']['arms'][r['arm']]['max_model_calls'] for r in rows[start:])
    if approval['max_new_requests'] != limit:
        raise ValueError('Resume request limit must equal remaining original per-trial limits')
    history = batch.get('continuations', [batch['continuation']] if batch.get('continuation') else [])
    approved_checks = {c['auxiliary_trial']: c['auxiliary_attempt'] for c in history}
    approved_checks[approval['auxiliary_trial']] = approval['auxiliary_attempt']
    blocked = [r for r in rows[:start] if r['outcome'] == 'BLOCKED']
    if (not blocked or blocked[-1]['index'] != approval['auxiliary_trial']
            or any(r['index'] not in approved_checks for r in blocked)):
        raise ValueError('Every blocked trial needs its explicit auxiliary approval')
    for row in blocked:
        state = read_json(Path(row['run_path']) / 'state.json')
        attempt = next(a for a in state['attempts'] if a['number'] == approved_checks[row['index']])
        rechecks = attempt.get('revalidations', [])
        if (state['paid_api_calls'] != row['measurement']['requests'] or len(rechecks) != 1
                or rechecks[0]['status'] != 'PASS' or not rechecks[0]['validation']['all_passed']):
            raise ValueError('One successful same-candidate zero-API auxiliary recheck is required')
    archive = folder / ('continuation' if not history else f'continuation-{len(history)+1:03d}')
    archive.mkdir(exist_ok=False)
    save_json(archive / 'batch-before.json', batch)
    save_json(archive / 'approval.json', approval)
    batch['continuation'] = {**approval, 'started_at': runner.now(),
                             'auxiliary_recheck': str(Path(blocked[-1]['run_path']) / rechecks[0]['directory'])}
    batch['continuations'] = history + [batch['continuation']]
    batch.pop('reason', None)
    return start


def run_batch(folder, resume_approval=None):
    batch = read_json(folder / 'batch.json')
    if resume_approval is not None:
        start = resume_start(folder, batch, resume_approval)
    else:
        start = 0
        if batch['status'] != 'READY':
            raise ValueError('Only a READY batch may start; blocked batches require explicit review')
        if commit() != batch['execution_commit']:
            raise ValueError('Implementation commit changed after preflight')
    execution_commit = commit()
    model_config = batch['plan']['model']
    model = DeepSeekModel(model_config['name'], model_config['max_output_tokens'],
                          thinking=model_config['thinking'],
                          reasoning_effort=model_config.get('reasoning_effort', 'high'))
    batch['status'] = 'RUNNING'
    store(folder, batch)
    for row in batch['trials'][start:]:
        if row['outcome'] != 'NOT_RUN':
            raise ValueError('A trial was already started; no automatic retry')
        base_run = Path(batch['baselines'][row['case']])
        config = read_json(base_run / 'case.json')
        config.update(source_root=str(base_run / 'baseline'), baseline_report=str(base_run / 'baseline-proof.json'),
                      evaluation_scope='competition_development_comparison', **batch['plan']['arms'][row['arm']])
        config['workflow_mode'] = config.pop('mode')
        path = folder / 'configs' / f"trial-{row['index']:02d}.json"
        save_json(path, config)
        # Baseline snapshots live under runs/workflow; trial storage must be a sibling.
        run = runner.prepare(path, REPO / 'runs/comparison' / folder.name)
        row.update(run_id=run.name, run_path=str(run), outcome='RUNNING', started_at=runner.now(),
                   execution_commit=execution_commit)
        store(folder, batch)
        state = read_json(run / 'state.json')
        initial = runner.request_for(run, read_json(run / 'case.json'), state)
        initial.pop('instructions')
        if initial != read_json(folder / 'initial-evidence' / (row['case'] + '.json')):
            raise ValueError('Initial payload differs between arms')
        save_json(run / 'comparison-binding.json', {'batch_id': batch['batch_id'], 'execution_commit': execution_commit,
                   'index': row['index'], 'pair': row['pair'], 'repeat': row['repeat'], 'arm': row['arm'], 'initial_payload_equal': True})
        print('TRIAL', row['index'], row['case'], row['arm'], run.name, flush=True)
        if not restore_original(base_run, run, config):
            state.update(status='BLOCKED', reason='Original app precondition did not pass; no model call')
            runner.persist(run, state, 'comparison_precondition_blocked')
        else:
            state = drive_comparison(run, model)
        row.update(status=state['status'], reason=state.get('reason'), finished_at=runner.now())
        row['measurement'] = measurement(run, folder / 'trial-reports' / f"{row['index']:02d}", row['case'])
        row['outcome'] = ('PASS' if state['status'] == 'VALIDATED' else
                          'MODEL_FAILURE' if state['status'] in {'EXHAUSTED', 'DUPLICATE_PATCH'} else 'BLOCKED')
        print('RESULT', row['index'], row['outcome'], 'requests', state['paid_api_calls'], flush=True)
        if row['outcome'] == 'BLOCKED':
            batch.update(status='PAUSED', reason=f"Trial {row['index']} blocked; no further calls")
            store(folder, batch)
            return 3
        store(folder, batch)
    batch.update(status='FINISHED_WITH_BLOCKS' if any(r['outcome'] == 'BLOCKED' for r in batch['trials']) else 'COMPLETE',
                 finished_at=runner.now())
    store(folder, batch)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--host', type=Path, required=True)
    p.add_argument('--batch', type=Path, required=True)
    for name in ('execute', 'report'):
        p = sub.add_parser(name);p.add_argument('--batch', type=Path, required=True)
    p = sub.add_parser('resume', help='Explicitly approved continuation; never rewrites earlier trial results')
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--approval', type=Path, required=True)
    args = parser.parse_args()
    folder = args.batch.resolve()
    try:
        if args.command == 'prepare':return prepare(args.plan, args.host, folder)
        if args.command == 'report':write_comparison(folder);return 0
        if args.command == 'resume':return run_batch(folder, args.approval)
        return run_batch(folder)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # Preserve the incomplete trial; never continue to another request or pair.
        if (folder / 'batch.json').exists() and args.command != 'report':
            batch = read_json(folder / 'batch.json')
            batch.update(status='PAUSED', reason=f'{type(exc).__name__}: {exc}')
            store(folder, batch)
        print(type(exc).__name__, str(exc), file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
