"""Sequential, checkpointed workflow with offline model fixtures and evidence gates."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import uuid

from . import __version__
from .evidence import Evidence
from .model import INSTRUCTIONS, SINGLE_PATCH_INSTRUCTIONS, PendingModel, ModelError
from .outbound import project_request
from .repair import apply_proposal, validate_proposal
from .validation import validate
from .workspace import load_case, read_json, save_json, snapshot


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextmanager
def execution_lock(root: Path):
    # One validation process per run storage root, including multiple CLI instances.
    path = root / '.execution.lock'
    try:
        stream = path.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('Execution lock exists; verify no active process before manual recovery') from exc
    try:
        with stream:
            stream.write(now())
        yield
    finally:
        path.unlink()


def persist(run: Path, state: dict, event: str, **details):
    state['updated_at'] = now()
    with (run / 'trajectory.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'at': now(), 'event': event, **details}, ensure_ascii=False) + '\n')
    save_json(run / 'state.json', state)
    result = {k: state[k] for k in ('run_id', 'case_id', 'status', 'snapshot_id', 'attempts', 'model_calls',
                                   'model_kind', 'baseline_status', 'paid_api_calls')}
    result['repair_validated'] = state['status'] == 'VALIDATED'
    result['evaluation_scope'] = state.get('evaluation_scope', 'development')
    result['workflow_mode'] = state.get('workflow_mode', 'workflow')
    result['validation_passed'] = state['status'] in {'VALIDATED', 'SYNTHETIC_VALIDATED', 'FIXTURE_PASSED'}
    result['offline_fixture'] = state['model_kind'] == 'offline_fixture'
    result['fixture_assisted'] = state.get('fixture_used', False)
    result['provider_calls'] = state.get('provider_calls', [])
    result['validated_attempt'] = state.get('validated_attempt')
    calls = result['provider_calls']
    def token_sum(key):
        values = [c.get('usage', {}).get(key) if isinstance(c.get('usage'), dict) else None for c in calls]
        return sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None
    result['usage'] = {'input_tokens': token_sum('prompt_tokens'), 'output_tokens': token_sum('completion_tokens'),
                       'total_tokens': token_sum('total_tokens'), 'actual_cost': None,
                       'note': 'Provider-reported tokens only; actual monetary cost is not inferred'}
    result['reason'] = state.get('reason')
    save_json(run / 'result.json', result)


def prepare(case_path: Path, storage: Path, host: Path | None = None) -> Path:
    config = load_case(case_path, host)
    source = Path(config['source_root'])
    storage = storage.resolve()
    if storage.is_relative_to(source) or source.is_relative_to(storage):
        raise ValueError('Run storage and source must not overlap')
    storage.mkdir(parents=True, exist_ok=True)
    run = storage / ('run-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    run.mkdir()
    save_json(run / 'case.json', config)
    manifest = snapshot(source, run / 'baseline', config['source_version'])
    save_json(run / 'snapshot.json', manifest)
    evidence = Evidence(run / 'baseline', config['visible_files'], config.get('visible_ranges'))
    context = evidence.initial(config['logs'])
    save_json(run / 'evidence.json', context)
    baseline_status = 'NOT_RUN'
    if config.get('baseline_report'):
        proof = read_json(Path(config['baseline_report']))
        if (proof.get('case_id') == config['case_id'] and proof.get('snapshot_id') == manifest['snapshot_id']
                and proof.get('status') == 'REPRODUCED'):
            baseline_status = 'REPRODUCED'
        else:
            baseline_status = 'INCONCLUSIVE'
        save_json(run / 'baseline-proof.json', proof)
    state = {'schema_version': 2, 'engine_version': __version__, 'run_id': run.name, 'case_id': config['case_id'],
             'status': 'PREPARED', 'snapshot_id': manifest['snapshot_id'], 'attempts': [],
             'model_calls': 0, 'model_kind': 'unconfigured', 'paid_api_calls': 0,
             'baseline_status': baseline_status, 'created_at': now(), 'feedback': [],
             'evaluation_scope': config.get('evaluation_scope', 'development'),
               'workflow_mode': config['workflow_mode'],
             'evidence': evidence.items, 'pending': None}
    persist(run, state, 'prepared')
    return run


def request_for(run: Path, config: dict, state: dict) -> dict:
    instructions = SINGLE_PATCH_INSTRUCTIONS if config.get('workflow_mode') == 'single_patch' else INSTRUCTIONS
    return project_request({'instructions': instructions, 'case_id': state['case_id'], 'snapshot_id': state['snapshot_id'],
            'task_description': config.get('task_description', 'Repair the observed failure while preserving existing behavior.'),
            'evidence': read_json(run / 'evidence.json'), 'retrieved_snippets': list(state['evidence'].values()),
            'visible_files': config['visible_files'], 'editable_files': config['editable_files'],
            'visible_ranges': config.get('visible_ranges', {}), 'editable_ranges': config.get('editable_ranges', {}),
            'feedback': state['feedback'][-6:], 'attempt': len(state['attempts']) + 1,
            'include_build_diagnostics': config.get('include_build_diagnostics', False),
            'include_list_diagnostics': config.get('include_list_diagnostics', False)})


def run_baseline(run: Path) -> dict:
    """Build/deploy/reproduce an original snapshot without contacting a model."""
    run = run.resolve()
    with execution_lock(run.parent):
        state = read_json(run / 'state.json')
        if state['status'] != 'PREPARED' or state['attempts']:
            raise ValueError('Baseline verification requires a fresh prepared run')
        verify_checkpoint(run, state)
        config = read_json(run / 'case.json')
        baseline_config = config.get('baseline_validation', {})
        if any(key not in baseline_config for key in ('build', 'deploy', 'replay')):
            raise ValueError('Baseline requires trusted build, deploy and replay adapters')
        target = run / 'original-verification'
        snapshot(run / 'baseline', target / 'workspace')
        state['status'] = 'VERIFYING_BASELINE'
        persist(run, state, 'baseline_started')
        try:
            checks = validate(baseline_config, target / 'workspace', target / 'validation', {
                'run_id': state['run_id'], 'attempt_id': 'baseline', 'snapshot_id': state['snapshot_id'],
                'patch_id': 'NONE_BASELINE'})
            replay = checks['stages']['replay'].get('details', {})
            reproduced = (all(checks['stages'][s]['status'] == 'PASS' for s in ('build', 'deploy', 'replay'))
                          and replay.get('baseline_status') == 'REPRODUCED'
                          and replay.get('case_id') == state['case_id'])
            proof = {'case_id': state['case_id'], 'snapshot_id': state['snapshot_id'],
                     'status': 'REPRODUCED' if reproduced else 'INCONCLUSIVE',
                     'run_id': state['run_id'], 'artifact_id': checks['artifact_id'],
                     'checks': checks, 'repair_validated': False}
            save_json(run / 'baseline-proof.json', proof)
            state.update(baseline_status=proof['status'], status='PREPARED' if reproduced else 'BLOCKED',
                         reason='Original snapshot reproduced' if reproduced else 'Baseline verification did not pass')
        except (OSError, ValueError, TypeError) as exc:
            state.update(status='BLOCKED', baseline_status='INCONCLUSIVE', reason=f'Baseline adapter error: {exc}')
        persist(run, state, 'baseline_finished')
        return state


def verify_checkpoint(run: Path, state: dict):
    if state.get('schema_version') != 2:
        raise ValueError('Legacy run protocol; preserve this record and prepare a new run')
    for name in ('case.json', 'evidence.json', 'snapshot.json'):
        if not (run / name).is_file():
            raise ValueError(f'Missing run record: {name}')


def revalidate(run: Path, attempt_number: int, reason: str) -> dict:
    """Explicitly rerun trusted checks on a saved model candidate, without model calls."""
    run = run.resolve()
    with execution_lock(run.parent):
        state = read_json(run / 'state.json')
        verify_checkpoint(run, state)
        if state['status'] not in {'BLOCKED', 'EXHAUSTED', 'VALIDATED', 'SYNTHETIC_VALIDATED',
                                    'FIXTURE_PASSED', 'DUPLICATE_PATCH'}:
            raise ValueError('Revalidation requires a completed or blocked run')
        if state['baseline_status'] != 'REPRODUCED' or not reason.strip():
            raise ValueError('Revalidation requires confirmed baseline and an explicit reason')
        matches = [a for a in state['attempts'] if a['number'] == attempt_number]
        if len(matches) != 1:
            raise ValueError('Saved candidate does not exist')
        attempt = matches[0]
        config = read_json(run / 'case.json')
        number = len(attempt.get('revalidations', [])) + 1
        target = run / attempt['directory'] / 'revalidations' / f'{number:03d}'
        snapshot(run / 'baseline', target / 'workspace')
        proposal = read_json(run / attempt['directory'] / 'proposal.json')
        patch = apply_proposal(proposal, run / 'baseline', target / 'workspace',
                               config['editable_files'], state['evidence'], config.get('editable_ranges'))
        if patch['diff'] != (run / attempt['directory'] / 'patch.diff').read_text(encoding='utf-8'):
            raise ValueError('Revalidation must use the unchanged saved model candidate')
        save_json(target / 'result-before.json', read_json(run / 'result.json'))
        record = {'number': number, 'directory': target.relative_to(run).as_posix(),
                  'reason': reason, 'engine_version': __version__, 'status': 'RUNNING'}
        attempt.setdefault('revalidations', []).append(record)
        state.update(status='REVALIDATING', validated_attempt=None)
        persist(run, state, 'revalidation_started', attempt=attempt_number, reason=reason)
        try:
            checks = validate(config['validation'], target / 'workspace', target / 'validation', {
                'run_id': state['run_id'], 'attempt_id': f'{attempt_number}-revalidate-{number}',
                'snapshot_id': state['snapshot_id'], 'patch_id': attempt['patch_id']})
            record['validation'] = checks
            record['status'] = 'PASS' if checks['all_passed'] else 'NOT_PASSED'
            if checks['all_passed']:
                status = ('FIXTURE_PASSED' if state.get('fixture_used', False) else
                          'SYNTHETIC_VALIDATED' if state['evaluation_scope'] == 'synthetic' else 'VALIDATED')
                state.update(status=status, validated_attempt=attempt_number,
                             reason='Saved candidate passed explicit revalidation; earlier results retained')
            else:
                state.update(status='BLOCKED', reason='Explicit revalidation did not pass; inspect recorded checks')
        except (OSError, ValueError, TypeError) as exc:
            record.update(status='BLOCKED', reason=str(exc))
            state.update(status='BLOCKED', reason=f'Revalidation adapter error: {exc}')
        persist(run, state, 'revalidation_finished', attempt=attempt_number, status=record['status'])
        return state


def drive(run: Path, model=None, execute_commands=False) -> dict:
    run = run.resolve()
    model = model or PendingModel()
    with execution_lock(run.parent):
        state = read_json(run / 'state.json')
        if state['status'] not in {'PREPARED', 'WAITING_MODEL', 'WAITING_VALIDATION'}:
            raise ValueError('Only prepared or clean waiting checkpoints can continue; start a fresh run otherwise')
        verify_checkpoint(run, state)
        config = read_json(run / 'case.json')
        if config.get('context_review') != 'APPROVED':
            state.update(status='WAITING_MODEL', reason='Model-visible materials are not approved')
            persist(run, state, 'context_review_required')
            return state
        if model.kind == 'deepseek' and config.get('live_model_enabled') is False:
            state.update(status='WAITING_MODEL', reason='Live provider calls are disabled for this case')
            persist(run, state, 'provider_disabled')
            return state
        if model.kind == 'deepseek' and config.get('evaluation_scope', '').startswith('competition'):
            from .context_review import verify_approval
            try:
                verify_approval(config)
            except ValueError as exc:
                state.update(status='WAITING_MODEL', reason=str(exc))
                persist(run, state, 'context_approval_changed')
                return state
        if model.kind != 'unconfigured':
            state['model_kind'] = model.kind
        evidence = Evidence(run / 'baseline', config['visible_files'], config.get('visible_ranges'))
        evidence.items = state['evidence']
        while True:
            if state['pending']:
                if not execute_commands:
                    state.update(status='WAITING_VALIDATION', reason='Candidate ready; trusted commands not enabled')
                    persist(run, state, 'validation_pending')
                    return state
                attempt = state['attempts'][-1]
                directory = run / attempt['directory']
                state['status'] = 'VALIDATING'
                persist(run, state, 'validation_started', attempt=attempt['number'])
                binding = {'run_id': state['run_id'], 'attempt_id': str(attempt['number']),
                           'snapshot_id': state['snapshot_id'], 'patch_id': attempt['patch_id']}
                try:
                    checks = validate(config['validation'], directory / 'workspace', directory / 'validation', binding)
                except (OSError, ValueError, TypeError) as exc:
                    state.update(status='BLOCKED', reason=f'Validation adapter error: {exc}')
                    persist(run, state, 'adapter_error')
                    return state
                attempt['validation'] = checks
                state['pending'] = None
                if checks['all_passed'] and state['baseline_status'] == 'REPRODUCED':
                    status = ('FIXTURE_PASSED' if state.get('fixture_used', False) else
                              'SYNTHETIC_VALIDATED' if state['evaluation_scope'] == 'synthetic' else 'VALIDATED')
                    state.update(status=status, validated_attempt=attempt['number'],
                                 reason='All registered checks passed against confirmed baseline')
                    persist(run, state, 'finished')
                    return state
                failing = [(s, c) for s, c in checks['stages'].items() if c['status'] != 'PASS']
                if not failing:
                    state.update(status='INCONCLUSIVE', reason='Checks passed but original fault not confirmed for this snapshot')
                    persist(run, state, 'baseline_required')
                    return state
                stage, failure = failing[0]
                state['feedback'].append({'stage': stage, 'result': failure,
                                          'previous_diff': (directory / 'patch.diff').read_text(encoding='utf-8')[:24_000]})
                if failure['status'] != 'FAIL':
                    state.update(status='BLOCKED', reason=f'{stage}: {failure["status"]}')
                    persist(run, state, 'validation_blocked')
                    return state
                persist(run, state, 'validation_failed', stage=stage)
            if len(state['attempts']) >= config['max_attempts'] or state['model_calls'] >= config['max_model_calls']:
                state.update(status='EXHAUSTED', reason='Configured attempt/model-call limit reached')
                persist(run, state, 'limit_reached')
                return state
            request = request_for(run, config, state)
            save_json(run / 'next-request.json', request)
            live = model.kind == 'deepseek'
            if live:
                state['model_calls'] += 1
                state['paid_api_calls'] += 1
                state['status'] = 'QUERYING_MODEL'
                call = run / 'model' / f'{state["model_calls"]:03d}'
                save_json(call / 'request.json', request)
                persist(run, state, 'model_call_started', provider=model.kind)
            try:
                response = model.query(request)
            except ModelError as exc:
                state.setdefault('provider_calls', []).append(exc.metadata)
                if exc.metadata.get('submitted') is False:
                    state['paid_api_calls'] -= 1
                state.update(status='WAITING_MODEL', reason=str(exc))
                error = {'reason': str(exc), 'metadata': exc.metadata}
                if exc.response_content is not None:
                    (call / 'rejected-content.txt').write_bytes(exc.response_content.encode('utf-8'))
                    error['response_content_file'] = 'rejected-content.txt'
                save_json(call / 'error.json', error)
                persist(run, state, 'model_error')
                return state
            if response is None:
                state.update(status='WAITING_MODEL', reason='No model response available; request saved')
                persist(run, state, 'model_required')
                return state
            if not live:
                state['model_calls'] += 1
                if model.kind == 'offline_fixture':
                    state['fixture_used'] = True
            call = run / 'model' / f'{state["model_calls"]:03d}'
            save_json(call / 'request.json', request)
            save_json(call / 'response.json', response)
            if live:
                state.setdefault('provider_calls', []).append(response.get('_provider', {}))
            state['status'] = 'PREPARED'
            try:
                action = response.get('action')
                if config.get('workflow_mode') == 'single_patch' and action != 'propose':
                    raise ValueError('single_patch accepts only propose; retrieval is disabled')
                if action == 'read':
                    item = evidence.read(response['path'], response['start'], response['end'])
                    state['feedback'].append({'tool': 'read', 'result': item})
                elif action == 'search':
                    state['feedback'].append({'tool': 'search', 'result': evidence.search(response['term'])})
                elif action == 'propose':
                    validate_proposal(response, run / 'baseline', config['editable_files'], evidence.items, config.get('editable_ranges'))
                    directory = run / 'attempts' / f'{len(state["attempts"]) + 1:03d}'
                    snapshot(run / 'baseline', directory / 'workspace')
                    patch = apply_proposal(response, run / 'baseline', directory / 'workspace',
                                           config['editable_files'], evidence.items, config.get('editable_ranges'))
                    save_json(directory / 'proposal.json', response)
                    (directory / 'patch.diff').write_bytes(patch['diff'].encode('utf-8'))
                    attempt = {'number': len(state['attempts']) + 1, 'patch_id': patch['patch_id'],
                               'directory': str(directory.relative_to(run)).replace('\\', '/')}
                    state['attempts'].append(attempt)
                    if any((run / old['directory'] / 'patch.diff').read_text(encoding='utf-8') == patch['diff']
                           for old in state['attempts'][:-1]):
                        state.update(status='DUPLICATE_PATCH', reason='Identical candidate already attempted')
                        persist(run, state, 'duplicate_patch')
                        return state
                    state['pending'] = attempt['number']
                else:
                    raise ValueError('Unsupported action; only read/search/propose are allowed')
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                state['feedback'].append({'stage': 'proposal_or_retrieval', 'error': str(exc),
                                          'rejected_response': {k:v for k,v in response.items() if k != '_provider'}})
                persist(run, state, 'response_rejected', reason=str(exc))
                continue
            persist(run, state, 'response_processed', action=action)
