"""Offline run ledger. Synthetic/fixture results never become competition scores."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from .workspace import confined, read_json, save_json
from .validation import STAGES


def write_solve_report(output, result, events):
    """A submitted patch is not an independently verified repair."""
    save_json(output / 'result.json', result)
    lines = ['# Agent task result', '', f"Status: {result['status']}",
             f"Model mode: {result['evaluation_scope']}",
             f"Action protocol: {result.get('model_protocol', 'json-actions')}",
             f"Model configuration: {json.dumps(result.get('model_configuration'), ensure_ascii=False)}",
             f"Core: {result['core']}; engine: {result['engine_version']}",
             f"API attempts: {result['api_calls']}; model turns: {result['model_calls']}",
             f"Tool events (including submission): {len(events)}",
             'Independent verification: NOT_RUN; repair success is not certified.', '',
             '## Original submission', '', result['submission'] or '(none)', '',
             '## Actual commands', '', '| Command | Exit | Status | Log |', '|---|---|---|---|']
    for event in events:
        kind = event['action'].get('action')
        if kind not in {'exec', 'harmony'}:
            continue
        command = str(event['action'].get('command', '') if kind == 'exec'
                      else 'harmony ' + event['action'].get('operation', '')).replace('|', '\\|').replace('\n', ' ')
        observed = event['result']
        lines.append(f"| {command} | {observed.get('returncode', 'unknown')} | "
                     f"{observed.get('status', 'EXECUTED')} | {observed.get('log', '')} |")
    lines += ['', '## Changes', '', *[f'- {p}' for p in result['changed_files']]]
    if result.get('dependency_lock_files'):
        lines += ['', 'Dependency lock files (also retained in changed_files and patch.diff; not business source repairs):',
                  *[f'- {p}' for p in result['dependency_lock_files']]]
    if result.get('excluded_new_files'):
        lines += ['', 'New files excluded by the task scope (retained in workspace, not in patch.diff):',
                  *[f'- {p}' for p in result['excluded_new_files']]]
    if result['binary_changes_not_in_patch']:
        lines += ['', 'Binary changes retained in workspace, not encoded in patch.diff:',
                  *[f'- {p}' for p in result['binary_changes_not_in_patch']]]
    if result['error']:
        lines += ['', '## Execution error', '', result['error']]
    if result.get('environment_preflight'):
        preflight = result['environment_preflight']
        lines += ['', '## Pre-model environment preparation', '',
                  'Infrastructure preparation, not an Agent repair or independent validation.',
                  'Status: ' + preflight['status'],
                  'Evidence: tools/environment-preflight/result.json and command.log',
                  'Automatic dependency changes: ' + ', '.join(preflight.get('dependency_changes', [])),
                  'Original inputs and previous attempts are preserved.']
    if result.get('model_errors'):
        lines += ['', '## Model response errors', '', *[
            f"- Turn {e['call']}: {e['kind']} — {e['error']}" for e in result['model_errors']]]
    if result.get('segments'):
        lines += ['', '## Execution segments', '', 'Counts are cumulative; continuation does not reset allowances.',
                  '', '| Segment | Status | Model calls (start→end) | Tools (start→end) | Seconds |',
                  '|---|---|---|---|---|']
        for segment in result['segments']:
            lines.append(f"| {segment['number']} | {segment['status']} | "
                         f"{segment['model_calls_start']}→{segment['model_calls_end']} | "
                         f"{segment['tools_start']}→{segment['tools_end']} | {segment['seconds']:.2f} |")
        if len(result['segments']) > 1:
            lines += ['', 'Previous result/trajectory records are preserved under continuations/.']
    lines += ['', '## Configured limits', '', '```json',
              json.dumps(result['limits'], ensure_ascii=False, indent=2), '```',
              'Per-request context.json contains local token estimates; provider usage remains authoritative.']
    (output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_agent_delivery(run: Path, output: Path, *, evaluation: Path | None = None):
    """Reference saved evidence without rewriting declarations or certifying autonomy."""
    from .task import files
    run, output = run.resolve(), output.resolve()
    evaluation = evaluation.resolve() if evaluation is not None else None
    if output.exists() or any(output.is_relative_to(p) or p.is_relative_to(output)
                              for p in (run, evaluation) if p is not None):
        raise ValueError('Delivery output must be new and outside source records')
    result = read_json(run / 'result.json')
    if result.get('format') != 'dfx-solve-1':
        raise ValueError('Expected a saved solve result')
    independent = {'status': 'NOT_RUN', 'source': None}
    if evaluation is not None:
        value = read_json(evaluation / 'result.json')
        binding = value['binding']
        if (value.get('mode') != 'posthoc' or Path(value['source_solve_run']).resolve() != run
                or Path(value['source']).resolve() != run / 'workspace'
                or binding['patch_id'] != str(run) + '/final'
                or binding['run_id'] != evaluation.name or binding['attempt_id'] != 'posthoc'
                or binding['snapshot_id'] != evaluation.name + '/input'
                or value['checks']['binding'] != binding
                or value.get('source_result_status', result['status']) != result['status']):
            raise ValueError('Independent evaluation binding differs from this solve result')
        stages = value['checks']['stages']
        if (value['required_stages'] != list(STAGES)
                or (value['status'] == 'PASS' and any(stages[s]['status'] != 'PASS' for s in STAGES))):
            raise ValueError('Independent evaluation stages do not support its status')
        # The existing evaluation copy is the candidate reference. Compare directly;
        # no hashes, new snapshot store, validation execution or result backfill.
        names = files(run / 'workspace', read_json(run / 'task.json')['exclude'])
        if names != sorted(value['files']):
            raise ValueError('Candidate file list differs from the evaluation input')
        for name in names:
            if (confined(run / 'workspace', name).read_bytes() !=
                    confined(evaluation / 'workspace', name).read_bytes()):
                raise ValueError('Candidate differs from evaluation copy: ' + name)
        independent = {'status': value['status'], 'source': str(evaluation / 'result.json'),
                       'case_id': value['case_id'], 'source_version': value['source_version'],
                       'binding': binding, 'candidate_comparison': 'MATCH',
                       'stages': {s: stages[s]['status'] for s in STAGES}}
    declaration = result.get('declaration')
    summary = {'format': 'dfx-agent-delivery-1', 'source_run': str(run),
        'engine_version': result['engine_version'], 'evaluation_scope': result['evaluation_scope'],
        'agent_status': result['status'], 'agent_submitted': result['status'] == 'Submitted',
        'original_submission': result['submission'], 'original_declaration': declaration,
        'changed_files': result['changed_files'],
        'unverified_declared_by_model': declaration.get('unverified', []) if declaration else None,
        'independent': independent, 'autonomous_completion': 'NOT_AUTOMATICALLY_ASSESSED',
        'resources': {key: result.get(key) for key in
                      ('api_calls', 'model_calls', 'tool_calls', 'usage', 'actual_cost', 'seconds')},
        'evidence': {name: str(run / name) for name in
                     ('result.json', 'patch.diff', 'report.md', 'tools/events.jsonl', 'trajectory.json')},
        'source_records_modified': False}
    output.mkdir(parents=True)
    save_json(output / 'delivery-summary.json', summary)
    lines = ['# Agent delivery summary', '',
             f"Engine: {summary['engine_version']}; evidence kind: {summary['evaluation_scope']}",
             f"Original agent status: {summary['agent_status']}",
             f"Agent submitted: {summary['agent_submitted']}",
             f"Independent candidate validation: {independent['status']}",
             'Autonomous completion: not automatically assessed.', '',
             'Independent PASS does not replace the original submission or prove complete agent self-checks.',
             '', '## Original model declaration', '', result['submission'] or '(none)',
             '', 'Unverified items declared by model (empty is not proof of completeness):',
             json.dumps(summary['unverified_declared_by_model'], ensure_ascii=False),
             '', '## Independent evidence', '', '```json',
             json.dumps(independent, ensure_ascii=False, indent=2), '```',
             '', '## Resources', '', f"API attempts: {result['api_calls']}; model turns: {result['model_calls']}; "
             f"tools: {result['tool_calls']}; agent seconds: {result['seconds']:.2f}",
             f"Actual cost: {result.get('actual_cost') if result.get('actual_cost') is not None else 'unknown'}",
             'Raw provider usage is retained in delivery-summary.json; missing usage remains unknown.',
             '', '## Evidence references', '', *[f'- {name}: {path}' for name, path in summary['evidence'].items()],
             '', 'Source records were not modified.']
    (output / 'delivery-summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


def selected_validation(value):
    attempts = value['attempts']
    selected = value.get('validated_attempt')
    chosen = (next((a for a in attempts if a.get('number') == selected), {}) if selected is not None
              else attempts[-1] if attempts else {})
    rechecks = chosen.get('revalidations', [])
    return chosen, (rechecks[-1].get('validation', {}) if rechecks else chosen.get('validation', {}))


def summarize_runs(root: Path):
    if not root.is_dir():
        raise ValueError('Run root does not exist')
    rows, issues, calls = [], [], []
    for directory in sorted(root.glob('run-*')):
        if not directory.is_dir():
            continue
        try:
            value = read_json(directory / 'result.json')
            scope = value.get('evaluation_scope', 'unknown')
            api_attempts = value.get('paid_api_calls', 0)
            model_calls = value.get('model_calls', 0)
            if (not isinstance(scope, str) or type(api_attempts) is not int or api_attempts < 0
                    or type(model_calls) is not int or model_calls < 0):
                raise ValueError('Invalid scope or call counters')
            fixture = value.get('offline_fixture', False) or value.get('fixture_assisted', False)
            group = 'offline_fixture' if fixture else 'synthetic' if scope == 'synthetic' else scope
            attempts = value['attempts']
            chosen, last_checks = selected_validation(value)
            passed = all(last_checks.get('stages', {}).get(s, {}).get('status') == 'PASS'
                         for s in ('build','deploy','replay','functional','regression'))
            verified = (not fixture and scope.startswith('competition') and value['status'] == 'VALIDATED'
                        and value.get('model_kind') == 'deepseek' and api_attempts > 0
                        and value.get('repair_validated') is True and value.get('baseline_status') == 'REPRODUCED'
                        and passed)
            if value.get('repair_validated') and not verified:
                issues.append({'run': directory.name, 'issue': 'Repair flag lacks consistent real-case evidence'})
            provider_calls = value.get('provider_calls', [])
            if not isinstance(provider_calls, list) or any(not isinstance(c, dict) for c in provider_calls):
                raise ValueError('provider_calls must be a list')
            calls.extend(c for c in provider_calls if c.get('submitted') is not False)
            rows.append({'run_id': directory.name, 'case_id': value['case_id'], 'group': group,
                         'status': value['status'], 'baseline_status': value.get('baseline_status', 'NOT_RUN'),
                         'attempts': len(attempts), 'model_calls': model_calls,
                         'generation_requests_attempted': api_attempts,
                         'verified_competition_development_repair': verified,
                         'result_path': str(directory / 'result.json')})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            issues.append({'run': directory.name, 'issue': f'Unreadable/incomplete result: {type(exc).__name__}'})
    def tokens(key):
        values=[c.get('usage',{}).get(key) if isinstance(c.get('usage'),dict) else None for c in calls]
        if not calls:
            return 0 if not any(r['generation_requests_attempted'] for r in rows) else None
        if len(calls)!=sum(r['generation_requests_attempted'] for r in rows):
            return None
        return sum(values) if all(type(v) is int and v>=0 for v in values) else None
    return {'runs': rows, 'issues': issues, 'status_counts': dict(Counter(r['status'] for r in rows)),
            'group_counts': dict(Counter(r['group'] for r in rows)),
            'verified_competition_development_repairs': sum(r['verified_competition_development_repair'] for r in rows),
            'generation_requests_attempted': sum(r['generation_requests_attempted'] for r in rows),
            'provider_reported_tokens': {'input': tokens('prompt_tokens'), 'output': tokens('completion_tokens'),
                                        'total': tokens('total_tokens')}, 'actual_cost': None,
            'note': 'Local run ledger only; not official localization/repair scores. API attempts are not confirmed charges. Missing usage stays unknown.'}


def describe_checks(checks):
    rows = {}
    for stage in STAGES:
        item = checks.get('stages', {}).get(stage, {})
        details = item.get('details', {})
        assertions = details.get('assertions')
        rows[stage] = {'status': item.get('status', 'NOT_RECORDED'),
                       'reason': item.get('reason') or details.get('reason'),
                       'seconds': item.get('command', {}).get('seconds'),
                       'assertion_count': len(assertions) if isinstance(assertions, list) else None,
                       'spec_version': details.get('spec_version')}
    return rows


def export_run(run: Path, output: Path):
    """Read saved evidence; never rerun commands, infer labels or change results."""
    run, output = run.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(run) or run.is_relative_to(output):
        raise ValueError('Report output must be a new directory outside the run')
    value = read_json(run / 'result.json')
    issues, patches, attempts = [], {}, []
    proof_path = run / 'baseline-proof.json'
    proof = read_json(proof_path) if proof_path.is_file() else {}
    if value.get('baseline_status') == 'REPRODUCED' and (
            proof.get('status') != 'REPRODUCED' or proof.get('case_id') != value['case_id']
            or proof.get('snapshot_id') != value.get('snapshot_id')):
        issues.append('Confirmed baseline lacks matching saved proof')
    chosen, checks = selected_validation(value)
    for attempt in value['attempts']:
        directory = confined(run, attempt['directory'])
        proposal_path, patch_path = directory / 'proposal.json', directory / 'patch.diff'
        proposal = read_json(proposal_path) if proposal_path.is_file() else None
        if proposal is None:
            issues.append(f'Missing proposal: {attempt["directory"]}')
        patch_name = f'attempt-{attempt["number"]:03d}.diff'
        if patch_path.is_file():
            patches[patch_name] = patch_path.read_bytes()
        else:
            issues.append(f'Missing diff: {attempt["directory"]}')
        attempts.append({'number': attempt['number'], 'patch_id': attempt.get('patch_id'),
            'localization_prediction': proposal.get('localization') if proposal else None,
            'edited_files': sorted({e['path'] for e in proposal.get('edits', [])}) if proposal else [],
            'diff': patch_name if patch_name in patches else None,
            'original_validation': describe_checks(attempt.get('validation', {})),
            'revalidations': [{k: recheck.get(k) for k in ('number', 'directory', 'reason', 'engine_version', 'status')}
                             | {'validation': describe_checks(recheck.get('validation', {}))}
                             for recheck in attempt.get('revalidations', [])]})

    trajectory = run / 'trajectory.jsonl'
    events = [json.loads(line) for line in trajectory.read_text(encoding='utf-8').splitlines() if line.strip()] if trajectory.is_file() else []
    if not trajectory.is_file():
        issues.append('Missing trajectory.jsonl; event timings unavailable')
    first_call = next((e for e in events if e['event'] == 'model_call_started'), None)
    first_proposal = next((e for e in events if e['event'] == 'response_processed' and e.get('action') == 'propose'), None)
    terminal_names = {'finished', 'limit_reached', 'validation_blocked', 'adapter_error', 'model_error', 'duplicate_patch', 'baseline_required'}
    terminal = None
    for index, event in enumerate(events):
        if not first_call or event['at'] < first_call['at'] or event['event'] not in terminal_names:
            continue
        following = events[index + 1:index + 3]
        # The comparison driver can handle a complete format error within the
        # same authorized workflow. A paused transport error remains a boundary.
        if (event['event'] == 'model_error' and len(following) == 2
                and following[0]['event'] == 'response_rejected'
                and following[0].get('reason') == 'Completed response has invalid structured content'
                and following[1]['event'] == 'model_call_started'):
            continue
        terminal = event
        break
    def elapsed(start, end):
        return round((datetime.fromisoformat(end['at']) - datetime.fromisoformat(start['at'])).total_seconds(), 3) if start and end else None
    recheck_times, pending = [], None
    for event in events:
        if event['event'] == 'revalidation_started':
            pending = event
        elif event['event'] == 'revalidation_finished':
            recheck_times.append({'attempt': event.get('attempt'), 'seconds': elapsed(pending, event)})
            pending = None
    if pending:
        recheck_times.append({'attempt': pending.get('attempt'), 'seconds': None})
    passed = all(checks.get('stages', {}).get(s, {}).get('status') == 'PASS' for s in STAGES)
    fixture = value.get('offline_fixture', False) or value.get('fixture_assisted', False)
    verified = (value['status'] == 'VALIDATED' and value.get('repair_validated') is True
                and value.get('baseline_status') == 'REPRODUCED' and passed and not fixture
                and value.get('model_kind') == 'deepseek' and value.get('paid_api_calls', 0) > 0
                and value.get('evaluation_scope', '').startswith('competition'))
    if value.get('repair_validated') and not verified:
        issues.append('Saved success flag lacks consistent real-case evidence')
    summary = {'run_id': value.get('run_id', run.name), 'case_id': value['case_id'],
        'source_version': value.get('snapshot_id'), 'saved_status': value['status'], 'reason': value.get('reason'),
        'evaluation_scope': value.get('evaluation_scope'), 'model_kind': value.get('model_kind'),
        'baseline_status': value.get('baseline_status', 'NOT_RECORDED'),
        'baseline_proof': {'run_id': proof.get('run_id'), 'status': proof.get('status'),
                           'reused': proof.get('run_id') not in (None, value.get('run_id', run.name)),
                           'checks': describe_checks(proof.get('checks', {}))},
        'verified_saved_repair': verified and not issues, 'official_score': False,
        'reported_attempt': chosen.get('number'),
        'selection': 'validated_attempt' if value.get('validated_attempt') is not None else 'last_attempt (historical/default)',
        'selected_validation': describe_checks(checks), 'attempts': attempts,
        'model_calls': value.get('model_calls'), 'generation_requests_attempted': value.get('paid_api_calls'),
        'candidate_count': len(attempts),
        'first_candidate_initial_passed': all(attempts[0]['original_validation'][s]['status'] == 'PASS' for s in STAGES) if attempts else False,
        'usage': value.get('usage'), 'actual_cost': None,
        'timing': {'first_localization_output_seconds': elapsed(first_call, first_proposal),
                   'initial_workflow_seconds': elapsed(first_call, terminal), 'revalidations': recheck_times},
        'issues': issues, 'source_run': str(run),
        'note': 'Saved evidence only, not a fresh validation. Localization is a model prediction, not ground truth. Timings do not imply successful repair or include all human work. Unknown values remain null.'}
    def cell(item):
        return str(item if item is not None else '未知').replace('|', '\\|').replace('\n', ' ')
    lines = [f'# 运行报告：{summary["run_id"]}', '',
             f'案例：{summary["case_id"]}；输入版本：{summary["source_version"]}', '',
             f'保存状态：**{summary["saved_status"]}**；原始基线：{summary["baseline_status"]}；依据保存证据确认修复：{summary["verified_saved_repair"]}', '',
             '这是历史证据导出，不代表重新验证；定位是模型预测，不是独立真值或官方成绩。', '',
             f'原因：{cell(summary["reason"])}', '',
             f'基线证明来源：{cell(proof.get("run_id"))}；复用：{summary["baseline_proof"]["reused"]}。基线 PASS 仅表示原故障确认。', '',
             '基线检查：' + ' / '.join(f'{s}={summary["baseline_proof"]["checks"][s]["status"]}' for s in ('build', 'deploy', 'replay')), '',
             f'报告候选：{cell(summary["reported_attempt"])}（{summary["selection"]}）', '',
             '| 候选/验证 | build | deploy | replay | functional | regression |', '|---|---|---|---|---|---|']
    for a in attempts:
        lines.append('| ' + str(a['number']) + ' 首次 | ' + ' | '.join(a['original_validation'][s]['status'] for s in STAGES) + ' |')
        for recheck in a['revalidations']:
            lines.append('| ' + f'{a["number"]} 复验 {recheck["number"]}' + ' | ' + ' | '.join(recheck['validation'][s]['status'] for s in STAGES) + ' |')
    for a in attempts:
        lines += ['', f'## 候选 {a["number"]}', '']
        pred = a['localization_prediction'] or {}
        for key in ('root_cause', 'crash_location', 'edit_location'):
            point = pred.get(key, {})
            lines.append(f'- {key}：{point.get("path", "未记录")}:{point.get("line", "未知")}')
        lines += ['', cell(pred.get('explanation')), '', f'修改文件：{", ".join(a["edited_files"]) or "未记录"}', '',
                  f'[查看 diff]({a["diff"]})' if a['diff'] else 'diff 缺失。']
        for label, stages in [('首次验证', a['original_validation'])] + [(f'复验 {r["number"]}：{r["reason"]}', r['validation']) for r in a['revalidations']]:
            lines += ['', label, '']
            for stage, row in stages.items():
                lines.append(f'- {stage}：{row["status"]}；断言 {cell(row["assertion_count"])}；耗时 {cell(row["seconds"])} 秒；规格 {cell(row["spec_version"])}' + (f'；{cell(row["reason"])}' if row['reason'] else ''))
    lines += ['', '## 用量与时间', '', f'模型交互：{cell(summary["model_calls"])}；外部生成请求：{cell(summary["generation_requests_attempted"])}；实际金额未知。', '',
              '```json', json.dumps({'usage': summary['usage'], 'timing': summary['timing']}, ensure_ascii=False, indent=2), '```', '',
              '首次定位输出耗时不证明定位正确；首次终态可能失败，复验单独计时，不据此声称全自动平均修复时间。', '',
              '## 记录完整性与来源', '', *(f'- {issue}' for issue in issues),
              f'原始记录：{run.as_posix()}。完整日志、请求/响应和设备证据仍在原运行目录，未随报告整包复制。', '']
    output.mkdir(parents=True)
    for name, patch in patches.items():
        (output / name).write_bytes(patch)
    save_json(output / 'report.json', summary)
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'output': str(output), 'status': value['status'], 'issues': len(issues)}
