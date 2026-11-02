"""Explicit model-facing projection. Raw validation artifacts stay on the host."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from .workspace import range_allowed

POLICY_VERSION = 'minimal-context-v2'


def frame(value, allowed):
    path = str(value.get('path', '')).replace('\\', '/')
    candidates = [p for p in allowed if p == path or p.endswith('/' + path)]
    # An unresolved stack path is a basename, never a host absolute path.
    safe_path = candidates[0] if len(candidates) == 1 else PurePosixPath(path).name
    return {'function': str(value.get('function', ''))[:300], 'path': safe_path,
            'line': value.get('line'), 'column': value.get('column')}


def snippet(value, allowed, ranges=None):
    if not isinstance(value, dict) or value.get('path') not in allowed:
        raise ValueError('Outbound snippet is outside source allowlist')
    if not range_allowed(ranges, value['path'], value['start'], value['end']):
        raise ValueError('Outbound snippet is outside approved source lines')
    return {key: value[key] for key in ('id', 'path', 'start', 'end', 'content')}


def build_diagnostics(output, allowed, ranges=None):
    """Extract only compiler identifiers and locations in approved source files."""
    output = re.sub(r'\x1b\[[0-9;]*m', '', output)
    pattern = r'(\d+) ArkTS Compiler Error\s+Error Message: ([^\n]+) At File: ([^\n]+):(\d+):(\d+)'
    result = []
    for code, message, path, line, column in re.findall(pattern, output):
        matches = [p for p in allowed if path.replace('\\', '/').endswith('/' + p)]
        rule = re.search(r'\((arkts-[a-z0-9-]+)\)', message)
        if len(matches) == 1 and rule and range_allowed(ranges, matches[0], int(line), int(line)):
            result.append({'path': matches[0], 'line': int(line), 'column': int(column),
                           'compiler_code': code, 'rule': rule[1]})
    return result


def validation_feedback(value, allowed=(), include_build_diagnostics=False, include_list_diagnostics=False, ranges=None):
    """Never forward arbitrary details, exception text, commands or log tails."""
    stage = value['stage']
    result = value.get('result', {})
    details = result.get('details', {})
    feedback = {'stage': stage, 'result': {'status': result.get('status'),
                'failure_kind': 'VALIDATION_NOT_PASSED'}}
    if stage == 'build':
        feedback['result']['failure_kind'] = 'BUILD_NOT_PASSED'
        if include_build_diagnostics:
            diagnostics = build_diagnostics(result.get('command', {}).get('output_tail', ''), allowed, ranges)
            feedback['result']['diagnostics'] = diagnostics
            feedback['result']['diagnostics_status'] = ('EXTRACTED_FROM_LOG_TAIL' if diagnostics
                                                        else 'NO_MATCHING_APPROVED_DIAGNOSTICS')
    if any(f.get('pid_matches') for f in details.get('replay', {}).get('faults', [])):
        feedback['result']['failure_kind'] = 'APPLICATION_CRASH'
    if details.get('reason') in {'Application exited or restarted after trigger',
                                 'Application exited before stage completion'}:
        feedback['result']['failure_kind'] = 'APPLICATION_EXITED_OR_RESTARTED'
    reason = str(details.get('reason', ''))
    if include_list_diagnostics and stage in {'replay','functional','regression'}:
        kind = reason.removeprefix('Blocked: ')
        if kind in {'TITLE_MISMATCH','LIST_CONTENT_MISMATCH','LIST_ORDER_MISMATCH','MISSING_OR_AMBIGUOUS_UI'}:
            feedback['result']['failure_kind'] = kind
    mismatch = re.fullmatch(r'Expected 间隔(-?\d+)天, observed 间隔(-?\d+)天', reason)
    if mismatch:
        feedback['result'].update(failure_kind='INTERVAL_RESULT_MISMATCH',
                                 expected_days=int(mismatch[1]), observed_days=int(mismatch[2]))
    if reason == 'Expected exactly one interval result':
        feedback['result']['failure_kind'] = 'MISSING_OR_AMBIGUOUS_RESULT'
    # Diff is produced by the checked edit engine from approved source files.
    if 'previous_diff' in value:
        feedback['previous_diff'] = value['previous_diff'][:24_000]
    return feedback


def project_request(request):
    allowed = set(request['visible_files'])
    ranges = request.get('visible_ranges', {})
    evidence = request['evidence']
    crashes = [{'log_id': c['log_id'], 'error_type': c.get('error_type'),
                'message': c.get('message'), 'frames': [frame(f, allowed) for f in c['frames']]}
               for c in evidence.get('crashes', [])]
    mappings = [{'frame': frame(m['frame'], allowed),
                 'candidates': [p for p in m['candidates'] if p in allowed], 'status': m['status']}
                for m in evidence.get('path_mappings', [])]
    feedback = []
    for item in request.get('feedback', [])[-6:]:
        if item.get('tool') == 'read':
            feedback.append({'tool': 'read', 'result': snippet(item['result'], allowed, ranges)})
        elif item.get('tool') == 'search':
            matches = [{k: m[k] for k in ('path', 'line', 'text')} for m in item['result']['matches']
                       if m['path'] in allowed and range_allowed(ranges, m['path'], m['line'], m['line'])]
            feedback.append({'tool': 'search', 'result': {'matches': matches,
                                                        'truncated': item['result']['truncated']}})
        elif item.get('stage') in {'build', 'deploy', 'replay', 'functional', 'regression'}:
            feedback.append(validation_feedback(item, allowed, request.get('include_build_diagnostics', False),
                                                request.get('include_list_diagnostics', False), ranges))
        elif item.get('stage') == 'proposal_or_retrieval':
            feedback.append({'stage': 'proposal_or_retrieval', 'error': 'RESPONSE_REJECTED',
                'hint': 'Use the exact read/search/propose JSON schema, allowed paths, unique original search text and valid evidence ranges.'})
    return {k: request[k] for k in ('instructions', 'case_id', 'snapshot_id', 'task_description', 'attempt')} | {
        'outbound_policy': POLICY_VERSION, 'visible_files': sorted(allowed),
        **({k: request[k] for k in ('visible_ranges', 'editable_ranges') if request.get(k)}),
        'editable_files': sorted(set(request['editable_files']) & allowed),
        'evidence': {'crashes': crashes, 'path_mappings': mappings,
                     'snippets': [snippet(s, allowed, ranges) for s in evidence.get('snippets', [])]},
        'retrieved_snippets': [snippet(s, allowed, ranges) for s in request.get('retrieved_snippets', [])],
        'feedback': feedback}
