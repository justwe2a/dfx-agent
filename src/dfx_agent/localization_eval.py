"""Local, explicit-version development diagnostics; never used in model prompts."""
from __future__ import annotations

from pathlib import Path

from .workspace import confined, read_json

KINDS = ('root_cause', 'crash_location', 'edit_location')


def evaluate(run: Path, labels_path: Path, attempt: int = 1) -> dict:
    if type(attempt) is not int or attempt < 1:
        raise ValueError('Attempt must be a positive integer')
    labels = read_json(labels_path)
    state, snapshot = read_json(run/'state.json'), read_json(run/'snapshot.json')
    if (labels.get('schema_version') != 2 or labels.get('case_id') != state['case_id'] or
            labels.get('snapshot_id') != state['snapshot_id'] or
            labels.get('snapshot_id') != snapshot['snapshot_id']):
        raise ValueError('Labels do not match case and original snapshot')
    result = {'schema_version': 2, 'run_id': state['run_id'], 'case_id': state['case_id'],
              'snapshot_id': state['snapshot_id'], 'attempt': attempt,
              'label_version': labels['label_version'],
              'evaluation_scope': state.get('evaluation_scope', 'unknown'),
              'model_kind': state['model_kind'], 'official_score': False,
              'aggregate_accuracy': None, 'locations': {}}
    proposal_path = confined(run, f'attempts/{attempt:03d}/proposal.json')
    proposal = read_json(proposal_path) if proposal_path.is_file() else None
    predictions = proposal.get('localization', {}) if isinstance(proposal, dict) else {}
    if not isinstance(predictions, dict):
        predictions = {}
    for kind in KINDS:
        label = labels.get('locations', {}).get(kind, {})
        status = label.get('status', 'UNLABELED')
        row = {'label_status': status, 'status': 'NOT_SCORED',
               'exact_line_hit': None, 'file_hit': None}
        result['locations'][kind] = row
        if status != 'CONFIRMED':
            row['reason'] = 'Label is not confirmed; crash frames do not establish root cause'
            continue
        points = label.get('points', [])
        if not points or not label.get('reviewer') or not label.get('provenance'):
            raise ValueError('Confirmed labels require points, reviewer and provenance')
        for point in points:
            name, line = point.get('path'), point.get('line')
            if name not in snapshot['files'] or type(line) is not int or line < 1:
                raise ValueError('Label point must refer to original source')
            raw = confined(run/'baseline', name).read_bytes()
            if line > len(raw.decode('utf-8-sig').splitlines()):
                raise ValueError('Label line outside original source')
        prediction = predictions.get(kind)
        if proposal is None:
            row.update(status='NOT_PREDICTED', reason='No proposal for requested attempt')
            continue
        if (not isinstance(prediction, dict) or not isinstance(prediction.get('path'), str) or
                type(prediction.get('line')) is not int or prediction['line'] < 1):
            row.update(status='INVALID_PREDICTION', reason='Expected a source path and positive integer line')
            continue
        row.update(status='SCORED',
                   file_hit=any(prediction['path'] == p['path'] for p in points),
                   exact_line_hit=any(prediction['path'] == p['path'] and prediction['line'] == p['line']
                                      for p in points))
    return result
