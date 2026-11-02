"""Local-only inventory and preview of the exact model request projection."""
from collections import Counter
from pathlib import Path
import re

from .evidence import Evidence
from .model import INSTRUCTIONS, SINGLE_PATCH_INSTRUCTIONS
from .outbound import POLICY_VERSION, project_request
from .workspace import file_manifest, load_case, save_json, range_allowed

PATTERNS = {
    'credential_marker': re.compile(r'(?i)(api[_-]?key|secret|password|passwd|bearer\s+|BEGIN .*PRIVATE KEY|access[_-]?token)'),
    'external_url': re.compile(r'https?://[^\s\x27\x22]+'),
    'host_path': re.compile(r'[A-Za-z]:[\\/]|/(?:Users|home)/'),
    'answer_hint': re.compile(r'故障注入|根因|参考修复|标准答案|TC_JSError_'),
}


def verify_approval(config):
    """Check the recorded scope and explicit case revision, not file fingerprints."""
    approved = config.get('approved_context')
    if not isinstance(approved, dict):
        raise ValueError('No context scope approval is recorded')
    if approved.get('source_version') != config['source_version']:
        raise ValueError('Source version differs from approved context')
    for key in ('visible_files', 'editable_files'):
        if sorted(approved.get(key, [])) != sorted(config[key]):
            raise ValueError(f'{key} differs from approved context')
    for key in ('visible_ranges', 'editable_ranges'):
        if approved.get(key, {}) != config.get(key, {}):
            raise ValueError(f'{key} differs from approved context')
    if sorted(approved.get('log_names', [])) != sorted(Path(p).name for p in config['logs']):
        raise ValueError('Log scope differs from approved context')
    if config.get('include_build_diagnostics', False) != approved.get('include_build_diagnostics', False):
        raise ValueError('Build diagnostic feedback differs from approved context')
    if config.get('include_list_diagnostics', False) != approved.get('include_list_diagnostics', False):
        raise ValueError('List diagnostic feedback differs from approved context')


def export_review(case_path: Path, output: Path, host: Path | None = None):
    config = load_case(case_path, host)
    source = Path(config['source_root'])
    output = output.resolve()
    if output == source or output.is_relative_to(source) or output in source.parents:
        raise ValueError('Review output must not overlap source')
    if output.exists():
        raise ValueError('Use a new review directory; previous review evidence is immutable')
    snapshot = file_manifest(source)
    evidence = Evidence(source, config['visible_files'], config.get('visible_ranges'))
    initial = evidence.initial(config['logs'])
    instructions = SINGLE_PATCH_INSTRUCTIONS if config.get('workflow_mode') == 'single_patch' else INSTRUCTIONS
    request = project_request({'instructions': instructions, 'case_id': config['case_id'],
        'snapshot_id': config['source_version'], 'task_description': config.get('task_description',
            'Repair the observed failure while preserving existing behavior.'),
        'evidence': initial, 'retrieved_snippets': list(evidence.items.values()),
        'visible_files': config['visible_files'], 'editable_files': config['editable_files'],
        'feedback': [], 'attempt': 1,
        'visible_ranges': config.get('visible_ranges', {}), 'editable_ranges': config.get('editable_ranges', {}),
        'include_build_diagnostics': config.get('include_build_diagnostics', False),
        'include_list_diagnostics': config.get('include_list_diagnostics', False)})
    rows, flags = [], []
    for name in config['visible_files']:
        raw = (source / name).read_bytes()
        rows.append({'path': name, 'bytes': len(raw),
                     'lines': len(raw.decode('utf-8-sig').splitlines()), 'editable': name in config['editable_files'], 'read_ranges': config.get('visible_ranges', {}).get(name, 'all'), 'edit_ranges': config.get('editable_ranges', {}).get(name, 'all' if name in config['editable_files'] else [])})
        for number, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
            if not range_allowed(config.get('visible_ranges'), name, number, number):
                continue
            for category, pattern in PATTERNS.items():
                if pattern.search(line):
                    flags.append({'path': name, 'line': number, 'category': category})
    excluded = sorted(set(snapshot['files']) - set(config['visible_files']))
    manifest = {'schema_version': 2, 'case_id': config['case_id'], 'context_review': config.get('context_review'),
        'live_model_enabled': config.get('live_model_enabled', True), 'policy': POLICY_VERSION,
        'snapshot_id': config['source_version'], 'source_files': rows,
        'visible_ranges': config.get('visible_ranges', {}), 'editable_ranges': config.get('editable_ranges', {}),
        'log_files': [{'name': Path(p).name, 'bytes': Path(p).stat().st_size} for p in config['logs']],
        'initial_snippets': [{k:s[k] for k in ('path','start','end','id')} for s in initial['snippets']],
        'excluded_source_snapshot_files': excluded, 'scan_flags': flags,
        'scan_limit': 'Heuristic review assistance only; zero flags does not prove absence of secrets.',
        'include_build_diagnostics': config.get('include_build_diagnostics', False),
        'include_list_diagnostics': config.get('include_list_diagnostics', False),
        'paid_model_calls': 0, 'approval_granted': False}
    save_json(output / 'manifest.json', manifest)
    save_json(output / 'initial-request.preview.json', request)
    lines = ['# 模型可见资料清单（待确认）', '',
        f"案例：{config['case_id']}。策略：{POLICY_VERSION}。API 调用 0，不修改批准状态。", '',
        f"允许按需读取 {len(rows)} 个源码文件；初始发送 {len(initial['snippets'])} 个片段。注释也属于源码内容。", '',
        '| 相对路径 | 总行数 | 可读行 | 可编辑行 |', '|---|---:|---|---|']
    lines += [f"| {r['path']} | {r['lines']} | {r['read_ranges']} | {r['edit_ranges']} |" for r in rows]
    lines += ['', '## 初始片段', ''] + [f"- `{s['path']}`：{s['start']}–{s['end']} 行。" for s in initial['snippets']]
    lines += ['', '## 排除与人工复核', '',
        f'快照中排除 {len(excluded)} 个文件，完整列表在 manifest.json。测试/运行记录等未进入快照的内容也不发送。',
        '日志只发送错误类型、错误消息、栈帧位置及日志编号；不发送原始日志正文、进程名、时间戳、PID、HiLog。',
        '验证只发送阶段、状态、受控失败类型、可解析的日期预期/实际数值及上一候选 diff。',
        '完整构建输出、命令、设备/安装元信息、绝对路径、截图、UI 布局、测试源码及验收规格保留本地。',
        '当前不自动转发构建错误正文；必要诊断须单独审核后加入协议，可能降低构建失败时的自动重试能力。',
        f'启发式扫描命中 {len(flags)} 处：{dict(Counter(f["category"] for f in flags))}。只记录文件/行/类别，不回显命中值。',
        '零命中不证明完全不存在敏感信息。批准前应审阅源码原文与 initial-request.preview.json。',
        '范围或案例版本变化后应重新导出审阅，不把清单存在视为用户授权。', '']
    if config.get('include_build_diagnostics', False):
        lines += ['已启用经单独批准的 ArkTS 编译摘要：仅批准文件的相对路径、行列、错误码和规则名；不发送错误正文。',
                  '摘要从本地日志尾部提取，可能不完整；未提取到时明确标记 NO_MATCHING_APPROVED_DIAGNOSTICS。', '']
    if config.get('include_list_diagnostics', False):
        lines += ['已启用经批准的列表失败类别：标题、内容、顺序、UI 不可唯一读取；不发送书目答案、观察序列或数量。', '']
    (output / 'README.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'output': str(output), 'source_files': len(rows), 'scan_flags': len(flags),
            'context_review': config.get('context_review'), 'paid_model_calls': 0}
