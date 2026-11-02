"""Exact SEARCH/REPLACE edits, all preflighted before any write."""
from __future__ import annotations

import difflib
import uuid
from pathlib import Path
from .workspace import confined, range_allowed


def validate_proposal(proposal: dict, root: Path, editable: list[str], evidence: dict, ranges=None) -> dict[str, bytes]:
    location = proposal.get('localization', {})
    if not isinstance(location, dict):
        raise ValueError('localization must be an object')
    refs = location.get('evidence_refs', [])
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in evidence for r in refs) or not isinstance(location.get('explanation'), str) or not location['explanation'].strip():
        raise ValueError('Localization must cite known evidence and explain its inference')
    for key in ('root_cause', 'crash_location', 'edit_location'):
        point = location.get(key, {})
        if not isinstance(point, dict):
            raise ValueError(f'{key} must be an object')
        name, line = point.get('path'), point.get('line')
        if type(line) is not int or not any(evidence[r]['path'] == name and
                evidence[r]['start'] <= line <= evidence[r]['end'] for r in refs):
            raise ValueError(f'{key} must lie inside cited source evidence')
    edits = proposal.get('edits')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise ValueError('Expected 1..8 local edits')
    updates, seen = {}, set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError('Every edit must be an object')
        name = edit.get('path')
        if name not in editable or name in seen:
            raise ValueError('Edit path is forbidden or duplicated; use one edit per file')
        seen.add(name)
        raw = confined(root, name).read_bytes()
        old, new = edit.get('search'), edit.get('replace')
        if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
            raise ValueError('Search/replacement must describe a nonempty change')
        if len(old) + len(new) > 40_000:
            raise ValueError('Edit exceeds local patch limit')
        # Newline normalization is explicit; original newline convention is retained.
        text = raw.decode('utf-8').replace('\r\n', '\n')
        old, new = old.replace('\r\n', '\n'), new.replace('\r\n', '\n')
        if text.count(old) != 1:
            raise ValueError('SEARCH must match exactly once')
        start = text[:text.index(old)].count('\n') + 1
        end = start + old[:-1].count('\n')
        if not range_allowed(ranges, name, start, end):
            raise ValueError('Edit is outside approved original source lines')
        updated = text.replace(old, new, 1)
        if b'\r\n' in raw:
            updated = updated.replace('\n', '\r\n')
        updates[name] = updated.encode('utf-8')
    if location['edit_location']['path'] not in updates:
        raise ValueError('Suggested edit location must be included in patch')
    return updates


def apply_proposal(proposal: dict, original: Path, destination: Path,
                   editable: list[str], evidence: dict, ranges=None) -> dict:
    updates = validate_proposal(proposal, original, editable, evidence, ranges)
    chunks = []
    for name, updated in updates.items():
        old = confined(original, name).read_bytes()
        target = confined(destination, name)
        if target.read_bytes() != old:
            raise ValueError('Attempt workspace is not a fresh original copy')
        chunks.extend(difflib.unified_diff(old.decode('utf-8').replace('\r\n', '\n').splitlines(keepends=True),
                      updated.decode('utf-8').replace('\r\n', '\n').splitlines(keepends=True), fromfile='a/' + name, tofile='b/' + name,
                      n=0 if name in (ranges or {}) else 3))
    for name, updated in updates.items():
        confined(destination, name).write_bytes(updated)
    diff = ''.join(chunks)
    return {'patch_id': 'patch-' + uuid.uuid4().hex[:12], 'diff': diff,
            'files': list(updates)}
