"""Bounded jscrash parsing and textual retrieval over explicitly reviewed files."""
from __future__ import annotations

from pathlib import Path
import re

from .workspace import confined, range_allowed

FRAME = re.compile(r'^\s*at\s+(?P<function>.+?)\s*\((?P<path>[^()]+\.(?:ets|ts|js)):(?P<line>\d+):(?P<column>\d+)\)\s*$')
FIELDS = {'Module name': 'bundle', 'Process name': 'process', 'Error name': 'error_type',
          'Error message': 'message', 'Timestamp': 'timestamp'}


def parse_jscrash(text: str) -> dict:
    result = {'frames': []}
    in_stack = False
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if key in FIELDS and sep:
            result[FIELDS[key]] = value.strip()[:2000]
        if line.strip() == 'Stacktrace:':
            in_stack = True
            continue
        if in_stack and line.strip() in {'HybridStack:', 'HiLog:'}:
            in_stack = False
        if in_stack and (match := FRAME.match(line)) and len(result['frames']) < 30:
            frame = match.groupdict()
            frame['line'], frame['column'] = int(frame['line']), int(frame['column'])
            result['frames'].append(frame)
    if not result.get('error_type') or not result['frames']:
        raise ValueError('Unsupported/incomplete jscrash: error and application stack required')
    return result


class Evidence:
    def __init__(self, root: Path, visible: list[str], ranges=None):
        self.root, self.visible = root, set(visible)
        self.ranges = ranges or {}
        self.items: dict[str, dict] = {}

    def read(self, path: str, start: int, end: int) -> dict:
        if path not in self.visible:
            raise ValueError('File is outside the model allowlist')
        if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start >= 160:
            raise ValueError('Read range must contain 1..160 lines')
        if not range_allowed(self.ranges, path, start, end):
            raise ValueError('Read is outside approved source lines')
        raw = confined(self.root, path).read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError('Source file exceeds context limit')
        lines = raw.decode('utf-8-sig').splitlines()
        if start > len(lines):
            raise ValueError('Start line is outside the file')
        end = min(end, len(lines))
        content = '\n'.join(f'{i}: {lines[i-1]}' for i in range(start, end + 1))
        if len(content) > 24_000:
            raise ValueError('Snippet exceeds context limit')
        item = {'path': path, 'start': start, 'end': end, 'content': content}
        eid = f'{path}:{start}-{end}'
        item['id'] = eid
        if eid not in self.items and (len(self.items) >= 20 or sum(len(i['content']) for i in self.items.values()) + len(content) > 100_000):
            raise ValueError('Cumulative evidence budget reached')
        self.items[eid] = item
        return item

    def search(self, term: str) -> dict:
        if not isinstance(term, str) or not 1 <= len(term) <= 120:
            raise ValueError('Search term must contain 1..120 characters')
        matches = []
        for name in sorted(self.visible):
            file = confined(self.root, name)
            if file.stat().st_size > 1_000_000:
                continue
            for number, line in enumerate(file.read_text(encoding='utf-8-sig').splitlines(), 1):
                if range_allowed(self.ranges, name, number, number) and term in line:
                    matches.append({'path': name, 'line': number, 'text': line[:500]})
                    if len(matches) == 40:
                        return {'matches': matches, 'truncated': True}
        return {'matches': matches, 'truncated': False}

    def initial(self, logs: list[str]) -> dict:
        parsed, mappings, seen = [], [], set()
        for filename in logs:
            raw = Path(filename).read_bytes()
            if filename in seen:
                continue
            seen.add(filename)
            crash = parse_jscrash(raw.decode('utf-8-sig', errors='replace'))
            parsed.append({'log_id': f'log-{len(parsed)+1:03d}', **crash})
            for frame in crash['frames']:
                path = frame['path'].replace('\\', '/')
                exact = [path] if path in self.visible else []
                candidates = exact or [p for p in sorted(self.visible) if p.endswith('/' + path)]
                mappings.append({'frame': frame, 'candidates': candidates,
                                 'status': 'RESOLVED' if len(candidates) == 1 else 'UNRESOLVED'})
                if len(candidates) == 1 and len(self.items) < 8:
                    try:
                        self.read(candidates[0], max(1, frame['line'] - 12), frame['line'] + 12)
                    except ValueError as exc:
                        mappings[-1]['read_error'] = str(exc)
        return {'crashes': parsed, 'path_mappings': mappings, 'snippets': list(self.items.values()),
                'note': 'Crash locations are evidence, not automatically root-cause labels.'}
