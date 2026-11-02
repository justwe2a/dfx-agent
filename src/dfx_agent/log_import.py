"""Import one explicitly selected jscrash member; never extract an archive tree."""
from __future__ import annotations

import io
from pathlib import Path
import stat
import zipfile

from .evidence import parse_jscrash
from .workspace import is_link, relative_name, save_json

MAX_ARCHIVE = 64_000_000
MAX_LOG = 8_000_000
MAX_MEMBERS = 2048
MAX_RATIO = 200


def import_log(archive: Path, member: str, output: Path, dataset: Path) -> dict:
    """Create a new local folder with crash.log and local provenance."""
    relative_name(member)
    if not Path(member).name.startswith('jscrash-') or not member.endswith('.log'):
        raise ValueError('Select one jscrash-*.log member explicitly')
    for path in (archive, output, dataset):
        for part in (path.absolute(), *path.absolute().parents):
            if (part.exists() or part.is_symlink()) and is_link(part):
                raise ValueError('Linked input/output paths are not supported')
    archive, output, dataset = archive.resolve(), output.resolve(), dataset.resolve()
    if not dataset.is_dir() or not archive.is_relative_to(dataset):
        raise ValueError('Archive must be inside the declared dataset')
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError('Import output and original dataset must not overlap')
    if output.exists():
        raise ValueError('Import output must be a new directory')
    with archive.open('rb') as stream:
        blob = stream.read(MAX_ARCHIVE + 1)
    if len(blob) > MAX_ARCHIVE:
        raise ValueError('Archive exceeds bounded input size')
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            entries = z.infolist()
            if len(entries) > MAX_MEMBERS:
                raise ValueError('Too many archive members')
            matches = [i for i in entries if i.filename.casefold() == member.casefold()]
            if len(matches) != 1 or matches[0].filename != member:
                raise ValueError('Missing or ambiguous member name')
            info = matches[0]
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind not in (0, stat.S_IFREG) or info.flag_bits & 1:
                raise ValueError('Selected log must be an unencrypted regular file')
            if (info.file_size > MAX_LOG or info.file_size == 0 or
                    info.file_size > max(1, info.compress_size) * MAX_RATIO):
                raise ValueError('Selected log exceeds size/compression limits')
            with z.open(info) as stream:
                raw = stream.read(MAX_LOG + 1)
            if len(raw) != info.file_size or len(raw) > MAX_LOG:
                raise ValueError('Selected log size mismatch')
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Unreadable or unsupported ZIP log') from exc
    crash = parse_jscrash(raw.decode('utf-8-sig', errors='replace'))
    receipt = {'schema_version': 2, 'archive_path': archive.relative_to(dataset).as_posix(),
               'archive_bytes': len(blob), 'member': member,
               'member_bytes': len(raw), 'output_file': 'crash.log',
               'error_type': crash['error_type'], 'stack_frames': len(crash['frames']),
               'context_review': 'PENDING', 'paid_model_calls': 0,
               'note': 'Local import only. Log identity does not prove current-source reproduction.'}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'crash.log').write_bytes(raw)
    save_json(output / 'provenance.json', receipt)
    return receipt
