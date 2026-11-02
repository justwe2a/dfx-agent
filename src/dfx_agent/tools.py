"""Local tool observations for mini; a working directory is not an OS sandbox."""
import json
import os
from pathlib import Path
import shutil

from .validation import EXECUTION_ENV, execute
from .workspace import confined
from .task import files, matches
from .tool_schema import READ_LINE_LIMIT, SEARCH_LIMIT


class ProjectTools:
    def __init__(self, root: Path, output: Path, *, command_timeout=60, read_only=(), exclude=(), harmony=None,
                 native_calls=False, original=None):
        self.root, self.output = root.resolve(), output.resolve()
        self.command_timeout = command_timeout
        self.harmony = harmony
        self.native_calls = native_calls
        self.events = []
        self.read_only, self.exclude = read_only, exclude
        self.output.mkdir(parents=True, exist_ok=True)
        # solve supplies its frozen input explicitly; direct tool use snapshots its starting state.
        self.read_only_original = self.read_only_contents(original if original is not None else self.root)
        source = original if original is not None else self.root
        names = files(source, self.exclude)
        modules = {Path(n).parent for n in names if Path(n).name == 'build-profile.json5'}
        modules.update(Path(n).parents[2] for n in names if n.endswith('/src/main/module.json5'))
        self.generated_profiles = {(p / 'BuildProfile.ets').as_posix() for p in modules if p != Path('.')}

    def read_only_contents(self, root):
        if not self.read_only:
            return {}
        return {name: (root / name).read_bytes() for name in files(root, self.exclude)
                if matches(name, self.read_only)}

    def check_read_only(self, result):
        """Check persistent changes after external execution; do not undo effects."""
        try:
            current = self.read_only_contents(self.root)
            changed = sorted(name for name in self.read_only_original.keys() | current.keys()
                             if self.read_only_original.get(name) != current.get(name))
            newline_only = [name for name in changed if name in self.generated_profiles
                            and name in current and name in self.read_only_original
                            and current[name].replace(b'\r\n', b'\n') ==
                            self.read_only_original[name].replace(b'\r\n', b'\n')]
            if newline_only:
                result = {**result, 'generated_profile_newline_changes': newline_only}
                changed = [name for name in changed if name not in newline_only]
        except (OSError, ValueError) as exc:
            return {**result, 'status': 'BLOCKED', 'scope_blocked': True,
                    'operation_status': result.get('status'),
                    'operation_error': result.get('error'),
                    'failure_kind': result.get('failure_kind', 'scope_check'),
                    'error': f'Cannot verify read-only project paths: {exc}'}
        if not changed:
            return result
        if self.harmony:
            self.harmony.built.clear()
        return {**result, 'status': 'BLOCKED', 'scope_blocked': True,
                'operation_status': result.get('status'),
                'operation_error': result.get('error'),
                'failure_kind': result.get('failure_kind', 'scope_violation'),
                'scope_changes': changed,
                'error': 'Read-only project paths changed; effects retained, no retry or rollback'}

    def execute(self, action, cwd=''):
        from minisweagent.exceptions import Submitted, InterruptAgentFlow
        from .workspace import save_json
        number = len(self.events) + 1
        pending = self.output / 'started.json'
        save_json(pending, {'number': number, 'action': action})
        interrupted = False
        try:
            if action['action'] == 'request_user_action':
                if any(not isinstance(action.get(k), str) or not action[k].strip()
                       for k in ('reason', 'request')):
                    raise ValueError('request_user_action needs nonempty reason and request')
                result = {'status': 'WAITING_USER', 'reason': action['reason'],
                          'request': action['request'], 'unverified': action.get('unverified', [])}
            elif action['action'] == 'finish':
                summary = action['summary']
                if not isinstance(summary, str) or not summary.strip():
                    raise ValueError('finish requires a nonempty summary')
                result = {'status': 'SUBMITTED', 'summary': summary}
            elif action['action'] in {'read', 'search', 'edit', 'write', 'delete'}:
                result = self.file_action(action)
                if self.harmony and action['action'] in {'edit', 'write', 'delete'}:
                    self.harmony.built.clear()
            elif action['action'] == 'harmony':
                if self.harmony is None:
                    result = {'status': 'BLOCKED', 'error': 'HarmonyOS tools require --host'}
                else:
                    if action.get('operation') == 'run_script' and matches(action.get('script', ''), self.exclude):
                        raise ValueError('Script excluded by project scope')
                    result = self.harmony.execute(action, number)
            elif action['action'] == 'exec':
                command = action['command']
                if not isinstance(command, str) or not command.strip():
                    raise ValueError('exec requires a command string')
                shell = shutil.which('powershell.exe')
                if not shell:
                    raise OSError('PowerShell is required by this Windows adapter')
                directory = action.get('cwd', '.')
                directory = self.root if directory == '.' else confined(self.root, directory)
                log = self.output / f'{number:04d}.log'
                env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
                env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
                # Keep Git discovery within the task even if its repository is unavailable.
                env['GIT_CEILING_DIRECTORIES'] = str(self.root.parent)
                prefix = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                          "$OutputEncoding = [Console]::OutputEncoding; "
                          "$PSDefaultParameterValues['Get-Content:Encoding'] = 'utf8'; ")
                result = execute([shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', prefix + command],
                                 directory, env, self.command_timeout, log)
                if result.get('failure_kind') == 'timeout' and result.get('cleanup_complete'):
                    result.update(status='TIMEOUT', partial_side_effects_possible=True,
                                  instruction='Inspect current state before deciding the next action; no replay performed.')
                content = log.read_text(encoding='utf-8', errors='replace')
                preview = content if len(content) <= 8000 else content[:4000] + '\n[... see full log ...]\n' + content[-4000:]
                result.update(output=preview, truncated=len(content) > 8000,
                              log=log.name, log_source='tool-log')
            else:
                raise ValueError('Unknown action')
        except KeyboardInterrupt:
            interrupted = True
            result = {'status': 'INTERRUPTED', 'error': 'Tool interrupted; command is not replayed'}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result = {'status': 'TOOL_ERROR', 'error': str(exc)}
        if action['action'] in {'exec', 'finish'} or (
                action['action'] == 'harmony' and
                action.get('operation') in {'build', 'dependencies', 'run_script'}):
            result = self.check_read_only(result)
        event = {'number': number, 'action': action, 'result': result}
        self.events.append(event)
        with (self.output / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        if not interrupted and result.get('failure_kind') not in {'cleanup', 'driver', 'unknown_script_exit'}:
            pending.unlink()
        if interrupted:
            raise KeyboardInterrupt()
        if result.get('status') == 'WAITING_USER':
            receipt = {'role': 'tool' if self.native_calls else 'user',
                       'content': json.dumps(result, ensure_ascii=False)}
            if self.native_calls:
                receipt['tool_call_id'] = action['tool_call_id']
            raise InterruptAgentFlow(receipt, {'role': 'exit', 'content': result['reason'],
                'extra': {'exit_status': 'Paused', 'submission': ''}})
        if result.get('status') == 'BLOCKED' and (
                result.get('environment_blocked') or result.get('scope_blocked') or action['action'] == 'exec'):
            observation = {'role': 'tool' if self.native_calls else 'user',
                           'content': json.dumps(result, ensure_ascii=False)}
            if self.native_calls:
                observation['tool_call_id'] = action['tool_call_id']
            raise InterruptAgentFlow(observation, {
                'role': 'exit', 'content': result.get('error', 'Environment blocked; no retry'),
                'extra': {'exit_status': 'BLOCKED', 'submission': ''}})
        if result.get('status') == 'SUBMITTED':
            receipt = ([{'role': 'tool', 'tool_call_id': action['tool_call_id'],
                         'content': json.dumps(result, ensure_ascii=False)}] if self.native_calls else [])
            raise Submitted(*receipt, {'role': 'exit', 'content': result['summary'],
                                      'extra': {'exit_status': 'Submitted', 'submission': result['summary']}})
        return result

    def get_template_vars(self, **kwargs):
        return {}

    def file_action(self, action):
        operation = action['action']
        source = action.get('source', 'workspace')
        sources = {'workspace': self.root, 'original': self.root.parent / 'original',
                   'logs': self.root.parent / 'input-logs', 'tool-log': self.output}
        if source not in sources or (source != 'workspace' and operation not in {'read', 'search'}):
            raise ValueError('Only read/search may use original/logs/tool-log sources')
        root = sources[source]
        if operation == 'search':
            term = action['term']
            if not isinstance(term, str) or not term:
                raise ValueError('search requires a nonempty term')
            offset, limit = action.get('offset', 0), action.get('limit', SEARCH_LIMIT)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= SEARCH_LIMIT:
                raise ValueError('search offset >= 0, limit 1..50 required')
            name = action.get('path', '.')
            path = root if name == '.' else confined(root, name)
            if source in {'workspace', 'original'} and matches(name, self.exclude):
                raise ValueError('Path excluded by project scope')
            if not path.exists():
                raise ValueError('Search path does not exist')
            names = ([path.relative_to(root).as_posix()] if path.is_file() else
                     [p for p in files(root, self.exclude if source in {'workspace', 'original'} else ())
                      if name == '.' or (root / p).is_relative_to(path)])
            hits, skipped = [], []
            for name in names:
                if not matches(name, [action.get('glob', '*')]):
                    continue
                try:
                    lines = (root / name).read_text(encoding='utf-8-sig').splitlines()
                except UnicodeError:
                    skipped.append(name)
                    continue
                for number, text in enumerate(lines, 1):
                    if term in text:
                        column = text.index(term)
                        start = max(0, column-120)
                        hits.append({'path': name, 'line': number, 'column': column,
                                     'snippet_start': start, 'text': text[start:start+500]})
            from .workspace import save_json
            listing = self.output / f'search-{len(self.events)+1:04d}.json'
            save_json(listing, {'binary_skipped': skipped})
            selected = hits[offset:offset+limit]
            next_offset = offset + len(selected)
            return {'source': source, 'matches': selected, 'total_matches': len(hits),
                    'next_offset': next_offset if next_offset < len(hits) else None,
                    'truncated': next_offset < len(hits), 'binary_skipped_count': len(skipped),
                    'details': listing.name, 'details_source': 'tool-log'}
        name = action.get('path', '.')
        if source == 'workspace' and matches(name, self.exclude):
            raise ValueError('Path excluded by project scope')
        path = root if name == '.' else confined(root, name)
        if operation == 'read':
            if path.is_dir():
                entries = sorted(p.name + ('/' if p.is_dir() else '') for p in path.iterdir()
                                 if not matches(p.relative_to(root).as_posix(), self.exclude))
                return {'source': source, 'path': name, 'entries': entries[:200], 'truncated': len(entries) > 200,
                        'read_only_entries': [entry for entry in entries[:200]
                            if source != 'workspace' or matches(
                                (path / entry.rstrip('/')).relative_to(root).as_posix(), self.read_only)]}
            start = action.get('start', 1)
            if type(start) is not int:
                raise ValueError('read start must be an integer')
            end = action.get('end', start + READ_LINE_LIMIT - 1)
            if type(end) is not int or start < 1 or end < start or end-start >= READ_LINE_LIMIT:
                raise ValueError('read range must contain 1 to 200 lines')
            lines = path.read_text(encoding='utf-8-sig').splitlines()
            column = action.get('column', 0)
            if type(column) is not int or column < 0 or (start <= len(lines) and column > len(lines[start-1])):
                raise ValueError('Invalid read character column')
            parts, remaining, cursor = [], 12000, None
            for n in range(start, min(end, len(lines))+1):
                begin = column if n == start else 0
                prefix = f'{n}: '
                take = min(len(lines[n-1])-begin, max(0, remaining-len(prefix)-1))
                if remaining <= len(prefix)+1:
                    cursor = {'start': n, 'column': begin}
                    break
                parts.append(prefix + lines[n-1][begin:begin+take])
                remaining -= len(parts[-1])+1
                if begin+take < len(lines[n-1]):
                    cursor = {'start': n, 'column': begin+take}
                    break
            if cursor is None and end < len(lines):
                cursor = {'start': end+1, 'column': 0}
            return {'source': source, 'path': name, 'content': '\n'.join(parts),
                    'total_lines': len(lines), 'truncated': cursor is not None, 'next_read': cursor,
                    'read_only': source != 'workspace' or matches(name, self.read_only)}
        if name == '.' or matches(name, self.read_only):
            raise ValueError('Path is read-only')
        if operation == 'delete':
            path.unlink()
            return {'status': 'DELETED', 'path': name}
        newline = '\n'
        if operation == 'write':
            if path.exists():
                raise ValueError('write creates a new file; use edit for existing files')
            content = action['content']
        else:
            with path.open(encoding='utf-8', newline='') as stream:
                raw = stream.read()
            newline = '\r\n' if '\r\n' in raw else '\n'
            content = raw.replace('\r\n', '\n')
            search, replacement = action['search'], action['replace']
            if not isinstance(search, str) or not search or content.count(search) != 1:
                raise ValueError('edit search must match exactly once in current file')
            if not isinstance(replacement, str):
                raise ValueError('replace must be text')
            content = content.replace(search, replacement, 1)
        if not isinstance(content, str):
            raise ValueError('content must be text')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='') as stream:
            stream.write(content.replace('\r\n', '\n').replace('\n', newline))
        return {'status': 'EDITED', 'path': name}

    def serialize(self):
        return {'info': {'environment': 'dfx-windows', 'tool_calls': len(self.events)}}
