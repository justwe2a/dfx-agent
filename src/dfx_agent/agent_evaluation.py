"""Post-session evaluation bridge; no model calls or business repair steps."""
from pathlib import Path
import shutil

from .task import files, PRIVATE
from .validation import validate, STAGES
from .workspace import read_json, save_json


def evaluate(source: Path, output: Path, config: dict, *, mode: str, exclude=(), source_solve_run=None,
             reviewed_blocked=False):
    source, output = source.resolve(), output.resolve()
    if mode not in {'baseline', 'posthoc'}:
        raise ValueError('Expected baseline or posthoc')
    if reviewed_blocked and mode != 'posthoc':
        raise ValueError('Reviewed BLOCKED evaluation is posthoc only')
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Evaluation output must be new and outside its source')
    if mode == 'posthoc':
        if source_solve_run is None:
            raise ValueError('Posthoc requires the actual source solve run')
        run = Path(source_solve_run).resolve()
        result = read_json(run / 'result.json')
        if source != run / 'workspace':
            raise ValueError('Evaluate the final workspace of the stated solve run')
        if result['status'] in {'BLOCKED', 'Interrupted', 'Error'} and not (
                result['status'] == 'BLOCKED' and reviewed_blocked):
            raise ValueError('Environment/interruption/internal failure requires review before evaluation')
        if not any(name.endswith('.ets') for name in result['changed_files']):
            raise ValueError('No ArkTS change to evaluate; record NOT_RUN')
    names = files(source, PRIVATE + list(exclude))
    output.mkdir(parents=True)
    workspace = output / 'workspace'
    workspace.mkdir()
    for name in names:
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    # These are the independent evaluation's own binding IDs, not a fabricated
    # legacy candidate. Deployment creates its real receipt during validate().
    binding = {'run_id': output.name, 'attempt_id': mode,
               'snapshot_id': output.name + '/input',
               'patch_id': 'unmodified-input' if mode == 'baseline' else str(run) + '/final'}
    provenance = {'mode': mode, 'source': str(source),
                  'source_solve_run': str(run) if mode == 'posthoc' else None,
                  'source_result_status': result['status'] if mode == 'posthoc' else None,
                  'reviewed_blocked': bool(mode == 'posthoc' and result['status'] == 'BLOCKED'
                                           and reviewed_blocked),
                  'source_version': config['source_version'], 'case_id': config['case_id'],
                  'files': names, 'binding': binding, 'model_calls': 0}
    save_json(output / 'provenance.json', provenance)
    stages = config['baseline_validation' if mode == 'baseline' else 'validation']
    checks = validate(stages, workspace, output / 'validation', binding)
    required = ('build', 'deploy', 'replay') if mode == 'baseline' else STAGES
    states = [checks['stages'][name]['status'] for name in required]
    status = ('REPRODUCED' if mode == 'baseline' else 'PASS') if all(s == 'PASS' for s in states) else (
        'BLOCKED' if 'BLOCKED' in states else 'FAIL' if 'FAIL' in states else 'INCONCLUSIVE')
    summary = {**provenance, 'status': status, 'required_stages': list(required),
               'checks': checks, 'source_result_modified': False}
    save_json(output / 'result.json', summary)
    return summary
