"""Trusted case configuration and copy-based workspaces; not an OS sandbox."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import uuid

EXCLUDED = {'.git', '.idea', '.hvigor', '.cxx', 'oh_modules', 'node_modules', 'build',
            '__pycache__', 'reports', 'runs', 'testcases'}


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def range_allowed(ranges, path, start, end):
    """Optional original-line scope; files without a range retain whole-file scope."""
    return path not in (ranges or {}) or any(a <= start <= end <= b for a, b in ranges[path])


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Expected a relative POSIX file path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('Path traversal or noncanonical path')
    reserved = getattr(os.path, 'isreserved', lambda p: PureWindowsPath(p).is_reserved())
    if any(reserved(p) or p.endswith((' ', '.')) or any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in path.parts):
        raise ValueError('Reserved Windows path')
    return value


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def confined(root: Path, name: str) -> Path:
    relative_name(name)
    current = root
    if is_link(root):
        raise ValueError('Linked workspace root is not supported')
    for part in name.split('/'):
        current = current / part
        if current.exists() or current.is_symlink():
            if is_link(current):
                raise ValueError('Linked paths are not supported')
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path leaves workspace')
    return current


def source_files(root: Path) -> list[Path]:
    if not root.is_dir() or is_link(root):
        raise ValueError('Source must be an ordinary directory')
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('TC_')
                         and not (base == root and d == 'screenshot'))
        for name in dirs + names:
            path = base / name
            if is_link(path):
                raise ValueError(f'Linked input rejected: {path}')
        files.extend(base / n for n in sorted(names) if not n.startswith('TC_'))
    return sorted(files)


def file_manifest(root: Path) -> dict:
    return {'files': {p.relative_to(root).as_posix(): p.stat().st_size for p in source_files(root)}}


def snapshot(source: Path, destination: Path, version: str | None = None) -> dict:
    if destination.exists():
        raise ValueError('Snapshot destination already exists')
    before = file_manifest(source)
    destination.mkdir(parents=True)
    for name in before['files']:
        target = confined(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(confined(source, name), target)
    return {**before, 'snapshot_id': version or 'copy-' + uuid.uuid4().hex[:12]}


def resolve_host_paths(data: dict, host: Path | None) -> dict:
    """Resolve explicit host variables only in filesystem and command fields."""
    values = {'repo': str(Path(__file__).resolve().parents[2])}
    if host is not None:
        settings = read_json(host)
        path_keys = {'python', 'deveco', 'dataset', 'workspaces', 'reports'}
        if not isinstance(settings, dict) or set(settings) - (path_keys | {'device'}):
            raise ValueError('Host config accepts python/deveco/dataset/workspaces/reports/device only')
        for key, value in settings.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'Host value must be a nonempty string: {key}')
            values[key] = str((host.resolve().parent / value).resolve()) if key in path_keys else value

    def expand(value):
        def substitute(match):
            key = match.group(1)
            if key not in values:
                raise ValueError(f'Missing host variable: {key}; supply --host with this field')
            return values[key]
        return re.sub(r'\$\{([^}]+)\}', substitute, value)

    for key in ('source_root', 'baseline_report'):
        if data.get(key):
            data[key] = expand(data[key])
    data['logs'] = [expand(p) for p in data.get('logs', [])]
    for section in ('validation', 'baseline_validation'):
        for stage in data.get(section, {}).values():
            stage['argv'] = [expand(arg) for arg in stage.get('argv', [])]
    return data


def load_case(path: Path, host: Path | None = None) -> dict:
    data = read_json(path)
    if data.get('schema_version') != 2 or not isinstance(data.get('case_id'), str):
        raise ValueError('Case schema_version=2 and case_id are required')
    if not isinstance(data.get('source_version'), str) or not data['source_version'].strip():
        raise ValueError('An explicit source_version is required')
    data = resolve_host_paths(data, host)
    base = path.resolve().parent
    source = (base / data['source_root']).resolve()
    data['source_root'] = str(source)
    data['config_path'] = str(path.resolve())
    logs = [(base / p).resolve() for p in data.get('logs', [])]
    if not logs or any(not p.is_file() or p.stat().st_size > 8_000_000 for p in logs):
        raise ValueError('One or more bounded, readable crash logs are required')
    data['logs'] = [str(p) for p in logs]
    available = file_manifest(source)['files']
    visible = set(data.get('visible_files', []))
    for pattern in data.get('visible_globs', []):
        visible.update(n for n in available if fnmatch.fnmatchcase(n, pattern))
    for pattern in data.get('context_exclude_globs', []):
        visible.difference_update(n for n in list(visible) if fnmatch.fnmatchcase(n, pattern))
    editable = set(data.get('editable_files', []))
    for pattern in data.get('editable_globs', []):
        editable.update(n for n in visible if fnmatch.fnmatchcase(n, pattern))
    for name in visible | editable:
        relative_name(name)
        if name not in available:
            raise ValueError(f'Allowlisted file absent: {name}')
        if Path(name).suffix not in {'.ets', '.ts', '.js', '.py', '.cpp', '.h'}:
            raise ValueError('Only reviewed source files may enter model context')
    if not editable <= visible:
        raise ValueError('Editable files must be model-visible')
    data['visible_files'], data['editable_files'] = sorted(visible), sorted(editable)
    for key, files in [('visible_ranges', visible), ('editable_ranges', editable)]:
        ranges = data.get(key, {})
        if not isinstance(ranges, dict) or not set(ranges) <= files:
            raise ValueError(f'{key} must map approved files to line intervals')
        for name, spans in ranges.items():
            lines = len(confined(source, name).read_text(encoding='utf-8-sig').splitlines())
            if not isinstance(spans, list) or not spans:
                raise ValueError('Line intervals must not be empty')
            for span in spans:
                if (not isinstance(span, list) or len(span) != 2 or
                    any(type(n) is not int for n in span) or not 1 <= span[0] <= span[1] <= lines):
                    raise ValueError('Invalid original source line interval')
                if key == 'editable_ranges' and not range_allowed(data.get('visible_ranges'), name, *span):
                    raise ValueError('Editable lines must be visible')
    if any(name in data.get('visible_ranges', {}) and name not in data.get('editable_ranges', {}) for name in editable):
        raise ValueError('Partially visible editable files require an explicit edit range')
    for key, default, maximum in [('max_attempts', 3, 10), ('max_model_calls', 12, 100)]:
        value = data.setdefault(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'Invalid {key}')
    mode = data.setdefault('workflow_mode', 'workflow')
    if mode not in {'workflow', 'single_patch'}:
        raise ValueError('workflow_mode must be workflow or single_patch')
    if mode == 'single_patch' and (data['max_attempts'] != 1 or data['max_model_calls'] != 1):
        raise ValueError('single_patch requires exactly one model call and one candidate')
    if data.get('baseline_report'):
        data['baseline_report'] = str((base / data['baseline_report']).resolve())
    data.setdefault('validation', {})
    if 'live_model_enabled' in data and type(data['live_model_enabled']) is not bool:
        raise ValueError('live_model_enabled must be boolean')
    if 'include_build_diagnostics' in data and type(data['include_build_diagnostics']) is not bool:
        raise ValueError('include_build_diagnostics must be boolean')
    if 'include_list_diagnostics' in data and type(data['include_list_diagnostics']) is not bool:
        raise ValueError('include_list_diagnostics must be boolean')
    return data
