"""Task input and persistent copies, separate from protocol-2 case preparation."""
import difflib
import fnmatch
import os
from pathlib import Path
import shutil
import subprocess

from .workspace import is_link, read_json, save_json

IGNORED = {'.git', '.venv', '.idea', '__pycache__', '.hvigor', '.cxx', 'node_modules', 'oh_modules', 'build'}
PRIVATE = ['.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx']


def matches(name, patterns):
    # On Windows a differently cased spelling still addresses the same file.
    name = os.path.normcase(name).replace('\\', '/')
    patterns = [os.path.normcase(p).replace('\\', '/') for p in patterns]
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(Path(name).name, p)
               or name == p.rstrip('/') or name.startswith(p.rstrip('/') + '/') for p in patterns)


def files(root: Path, exclude=()):
    if not root.is_dir() or is_link(root):
        raise ValueError('Expected an ordinary project directory')
    result = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in IGNORED and
                         not matches((base / d).relative_to(root).as_posix(), exclude))
        for name in dirs + names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if matches(relative, exclude):
                continue
            if is_link(path):
                raise ValueError(f'Linked input unsupported: {relative}')
            if path.is_file():
                result.append(relative)
    return sorted(result)


def prepare_task(repo: Path, output: Path, task: str, *, scope=None, logs=None, live=False):
    repo, output = repo.resolve(), output.resolve()
    if not task.strip():
        raise ValueError('Task description is empty')
    if output.exists() or output.is_relative_to(repo) or repo.is_relative_to(output):
        raise ValueError('Output must be new and outside the project, without overlapping it')
    policy = read_json(scope) if scope else {}
    if not isinstance(policy, dict) or set(policy) - {'repo', 'outbound_approved', 'exclude', 'read_only'}:
        raise ValueError('Scope accepts repo/outbound_approved/exclude/read_only')
    if scope and Path(policy.get('repo', '')).resolve() != repo:
        raise ValueError('Scope repo must match the requested absolute project path')
    if live and policy.get('outbound_approved') is not True:
        raise ValueError('Live solve requires an approved project scope; no API submitted')
    for key in ('exclude', 'read_only'):
        value = policy.get(key, [])
        if not isinstance(value, list) or any(not isinstance(p, str) or not p for p in value):
            raise ValueError(f'Scope {key} must be a list of path patterns')
    exclude = PRIVATE + policy.get('exclude', [])
    names = files(repo, exclude)
    if not names:
        raise ValueError('No project files in scope')
    git = shutil.which('git')
    if not git:
        raise ValueError('Git is required to prepare an independent task workspace')
    log_names = files(logs.resolve(), PRIVATE) if logs else []
    # Resolve all input paths before creating output (no recursive self-copy).
    if logs and (output.is_relative_to(logs.resolve()) or logs.resolve().is_relative_to(output)):
        raise ValueError('Output overlaps log input')
    output.mkdir(parents=True)
    original, workspace = output / 'original', output / 'workspace'
    original.mkdir()
    workspace.mkdir()
    for name in names:
        for destination in (original, workspace):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / name, target)
    (output / 'input-logs').mkdir()
    for name in log_names:
        target = output / 'input-logs' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(logs / name, target)
    def run_git(*args):
        result = subprocess.run([git, '-C', str(workspace), *args], capture_output=True,
                                encoding='utf-8', errors='replace', timeout=180)
        if result.returncode:
            raise ValueError('Workspace Git preparation failed: ' + result.stderr)
        return result.stdout.strip()
    run_git('init', '--template=')
    for key, value in {'core.autocrlf': 'false', 'core.safecrlf': 'false',
                       'commit.gpgsign': 'false', 'core.hooksPath': str(workspace / '.git/hooks'),
                       'user.name': 'DFX workspace', 'user.email': 'workspace@dfx.local'}.items():
        run_git('config', '--local', key, value)
    (workspace / '.git/info').mkdir(exist_ok=True)
    (workspace / '.git/hooks').mkdir(exist_ok=True)
    (workspace / '.git/info/exclude').write_text(
        '\n'.join(name + '/' for name in sorted(IGNORED - {'.git'})) + '\n', encoding='utf-8')
    run_git('add', '--force', '--', '.')
    run_git('commit', '-m', 'Original task input')
    record = {'format': 'dfx-task-1', 'repo': str(repo), 'task': task, 'files': names,
              'logs': log_names, 'exclude': exclude, 'read_only': policy.get('read_only', []),
              'outbound_approved': policy.get('outbound_approved', False)}
    record['git_baseline'] = run_git('rev-parse', 'HEAD')
    save_json(output / 'task.json', record)
    return record


def export_diff(output: Path):
    """Text patch including added/deleted files; unsupported binary edits are explicit."""
    old, new = output / 'original', output / 'workspace'
    excluded = PRIVATE + read_json(output / 'task.json')['exclude']
    names = sorted(set(files(old)) | set(files(new)))
    chunks, changes, binary, excluded_new = [], [], [], []
    for name in names:
        before, after = old / name, new / name
        a = before.read_bytes() if before.exists() else None
        b = after.read_bytes() if after.exists() else None
        if a == b:
            continue
        # Input scope also applies to newly created build outputs. Keep changes to
        # original files visible; a filename alone is not proof of generation.
        if a is None and matches(name, excluded):
            excluded_new.append(name)
            continue
        changes.append(name)
        try:
            left, right = (a or b'').decode('utf-8'), (b or b'').decode('utf-8')
            if '\0' in left or '\0' in right:
                raise UnicodeError('binary')
        except UnicodeError:
            binary.append(name)
            continue
        chunks.append(f'diff --git a/{name} b/{name}\n')
        if a is None:
            chunks.append('new file mode 100644\n')
        elif b is None:
            chunks.append('deleted file mode 100644\n')
        for line in difflib.unified_diff(left.splitlines(keepends=True), right.splitlines(keepends=True),
                                         fromfile=f'a/{name}' if a is not None else '/dev/null',
                                         tofile=f'b/{name}' if b is not None else '/dev/null'):
            chunks.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    with (output / 'patch.diff').open('w', encoding='utf-8', newline='') as stream:
        stream.write(''.join(chunks))
    return {'changed_files': changes, 'binary_changes_not_in_patch': binary,
            'dependency_lock_files': [name for name in changes if Path(name).name == 'oh-package-lock.json5'],
            'excluded_new_files': excluded_new}
