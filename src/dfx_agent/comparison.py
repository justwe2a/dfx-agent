"""Paired development comparison tables; no model or device execution."""
from collections import Counter
from pathlib import Path
from .workspace import read_json, save_json

COMPLETED = {'PASS', 'MODEL_FAILURE'}

def paired_summary(batch):
    rows = batch['trials']
    arms = {}
    for arm in ('B0', 'B1'):
        selected = [r for r in rows if r['arm'] == arm]
        measured = [r.get('measurement', {}) for r in selected]
        def total(key):
            values = [m.get(key, 0) for m in measured]
            return sum(values) if all(v is not None for v in values) else None
        arms[arm] = {'registered': len(selected),
                     'completed': sum(r['outcome'] in COMPLETED for r in selected),
                     'passed': sum(r['outcome'] == 'PASS' for r in selected),
                     'outcomes': dict(Counter(r['outcome'] for r in selected)),
                     'requests': total('requests'), 'tokens': total('tokens'),
                     'first_candidate_initial_passed': sum(m.get('first_candidate_initial_passed', False) for m in measured),
                     'first_response_passed': sum(m.get('first_response_passed', False) for m in measured)}
    pairs = []
    for pair in sorted({r['pair'] for r in rows}):
        members = {r['arm']: r for r in rows if r['pair'] == pair}
        if set(members) != {'B0', 'B1'}:
            raise ValueError('Each registered pair must contain B0 and B1')
        a, b = members['B0'], members['B1']
        label = ('INCOMPLETE' if any(r['outcome'] not in COMPLETED for r in (a, b)) else
                 'BOTH_PASS' if a['outcome'] == b['outcome'] == 'PASS' else
                 'B0_ONLY' if a['outcome'] == 'PASS' else
                 'B1_ONLY' if b['outcome'] == 'PASS' else 'BOTH_FAIL')
        pairs.append({'pair': pair, 'repeat': a['repeat'], 'case': a['case'], 'outcome': label,
                      'B0': a['outcome'], 'B1': b['outcome']})
    return {'batch_id': batch['batch_id'], 'status': batch['status'], 'execution_commit': batch['execution_commit'],
            'continuation': batch.get('continuation'),
            'continuations': batch.get('continuations', []),
            'arms': arms, 'pairs': pairs, 'pair_counts': dict(Counter(p['outcome'] for p in pairs)),
            'trials': rows, 'actual_cost': None, 'official_score': False,
            'note': 'Fixed registered denominator; incomplete pairs are not losses. Development workflow comparison, not equal-budget or unseen-case evidence.'}

def write_comparison(batch_dir: Path):
    batch = read_json(batch_dir / 'batch.json')
    result = paired_summary(batch)
    save_json(batch_dir / 'comparison.json', result)
    lines = ['# 三例配对结果', '', f"批次：{result['batch_id']}；状态：{result['status']}；执行提交：{result['execution_commit']}", '',
             '开发对照，非等预算或未见集评测。通过数分母为全部预登记 trial；未完成项单列。', '',
             '| 组 | 已完成/登记 | 通过/登记 | 请求 | tokens | 首候选首次通过 | 首响应直接通过 |',
             '|---|---|---|---|---|---|---|']
    for arm, a in result['arms'].items():
        lines.append(f"| {arm} | {a['completed']}/{a['registered']} | {a['passed']}/{a['registered']} | {a['requests']} | {a['tokens']} | {a['first_candidate_initial_passed']} | {a['first_response_passed']} |")
    lines += ['', '| 对 | 重复 | 案例 | B0 | B1 | 配对结果 |', '|---|---|---|---|---|---|']
    lines += [f"| {p['pair']} | {p['repeat']} | {p['case']} | {p['B0']} | {p['B1']} | {p['outcome']} |" for p in result['pairs']]
    lines += ['', '## 逐次记录', '', '| 序号 | 案例/组 | 状态 | run_id | 原因 |', '|---|---|---|---|---|']
    for r in result['trials']:
        reason = str(r.get('reason', '')).replace('|', '/').replace('\n', ' ')
        lines.append(f"| {r['index']} | {r['case']}/{r['arm']} | {r['outcome']} | {r.get('run_id', '')} | {reason} |")
    lines += ['', '首次失败不回填，额外复验不计入本表首次自动结果。完整阶段、断言、时间、调用及定位诊断见 comparison.json 的 measurement 和各 trial 报告。实际费用未知。', '']
    for c in (result['continuations'] or ([result['continuation']] if result['continuation'] else [])):
        lines += [f"显式续跑提交：{c['resume_commit']}；保留前 {c['preserve_through']} 项原结果；剩余请求上限 {c['max_new_requests']}。",
                  f"辅助复验（不计入首次自动成绩）：{c['auxiliary_recheck']}", '']
    (batch_dir / 'comparison.md').write_text('\n'.join(lines), encoding='utf-8')
    return result
