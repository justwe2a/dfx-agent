"""DFX function definitions and their small, shared parameter contract."""
import copy


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


STRING = {'type': 'string'}
INTEGER = {'type': 'integer'}
READ_LINE_LIMIT = 200
SEARCH_LIMIT = 50
LAYOUT_LIMIT = 80
HARMONY_FIELDS = {
    'info': (), 'dependencies': (), 'build': ('module', 'product', 'target', 'artifact'),
    'install': ('artifact',), 'start': ('bundle', 'module', 'ability'),
    'stop': ('bundle',), 'layout': (), 'tap': ('x', 'y'), 'input': ('x', 'y', 'text'),
    'swipe': ('x1', 'y1', 'x2', 'y2', 'duration_ms'), 'key': ('key',), 'navigate': ('key',),
    'package': ('bundle',), 'pid': ('bundle',), 'faults': ('bundle',),
    'fault': ('bundle', 'name'), 'screenshot': (),
    'tap_text': ('selector',), 'input_text': ('selector', 'text'), 'wait': ('selector',),
    'run_script': ('script',), 'screen': ('mode',),
}
SCHEMAS = {
    'read': obj({'path': STRING, 'source': {'type': 'string', 'enum':
                  ['workspace', 'original', 'logs', 'tool-log']},
                 'start': {'type': 'integer', 'minimum': 1},
                 'end': {'type': 'integer', 'minimum': 1},
                 'column': {'type': 'integer', 'minimum': 0}}),
    'search': obj({'term': STRING, 'glob': STRING, 'path': STRING,
                   'source': {'type': 'string', 'enum': ['workspace', 'original', 'logs', 'tool-log']},
                   'offset': {'type': 'integer', 'minimum': 0},
                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': SEARCH_LIMIT}}, ['term']),
    'edit': obj({k: STRING for k in ('path', 'search', 'replace')}, ['path', 'search', 'replace']),
    'write': obj({'path': STRING, 'content': STRING}, ['path', 'content']),
    'delete': obj({'path': STRING}, ['path']),
    'exec': obj({'command': STRING, 'cwd': STRING}, ['command']),
    'request_user_action': obj({'reason': {'type': 'string', 'minLength': 1},
                               'request': {'type': 'string', 'minLength': 1},
                               'unverified': {'type': 'array', 'items': STRING}}, ['reason', 'request']),
    'finish': obj({'summary': {'type': 'string', 'minLength': 1},
                   'root_cause': obj({'path': STRING, 'line': INTEGER}, ['path', 'line']),
                   'unverified': {'type': 'array', 'items': STRING}}, ['summary']),
    'harmony': obj({
        'operation': {'type': 'string', 'enum': list(HARMONY_FIELDS)},
        'mode': {'type': 'string', 'enum': ['observe', 'unlock']},
        **{k: STRING for k in ('cwd', 'module', 'product', 'target', 'artifact',
                              'bundle', 'ability', 'text', 'name', 'saved_layout', 'script')},
        **{k: INTEGER for k in ('x', 'y', 'x1', 'y1', 'x2', 'y2', 'duration_ms')},
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': LAYOUT_LIMIT},
        'key': {'type': 'string', 'enum': ['Back', 'Home', 'Enter']},
        'selector': obj({'text': STRING, 'id': STRING, 'type': STRING,
                         'match': {'type': 'string', 'enum': ['exact', 'contains']}}),
        'timeout_seconds': INTEGER,
    }, ['operation']),
}
DESCRIPTIONS = {
    'read': 'Read UTF-8 files (at most 200 lines, 1-based inclusive start/end; column is 0-based) or list a directory; path defaults to ".". Follow next_read for truncated content.',
    'search': 'Literal search in workspace/original/logs/tool-log; optional path, glob, offset>=0 and limit 1..50. Follow next_offset for more matches.',
    'edit': 'Replace exactly one occurrence of search in the current file with replace.',
    'write': 'Create a NEW UTF-8 workspace file; use edit to modify an existing file.',
    'delete': 'Delete a workspace file.',
    'exec': 'Run a PowerShell command in workspace cwd (default "."); returns real output and exit code.',
    'request_user_action': 'Pause this task BEFORE accepting new terms/privacy/service consent, including dialogs inside the target app. Ask the operator to read and handle that specific decision locally. Test/device authorization is not consent. Preserve work and allowance; do not ask for repair steps.',
    'finish': 'Submit cause, modifications, executed checks and remaining limits; ends the session, not independent verification.',
    'harmony': 'Shared HarmonyOS operations. Read the supplied Skill for per-operation fields and permissions.',
}


def tool_definitions(harmony=False):
    return [{'type': 'function', 'function': {'name': name, 'description': DESCRIPTIONS[name],
             'parameters': copy.deepcopy(schema)}} for name, schema in SCHEMAS.items()
            if name != 'harmony' or harmony]


def _check(value, schema, path):
    kind = schema['type']
    valid = {'object': dict, 'string': str, 'integer': int, 'array': list}[kind]
    if type(value) is not valid:
        raise ValueError(f'{path} must be {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path} must be one of {schema["enum"]}')
    if kind == 'integer':
        if 'minimum' in schema and value < schema['minimum']:
            raise ValueError(f'{path} must be >= {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            raise ValueError(f'{path} must be <= {schema["maximum"]}')
    if kind == 'object':
        missing = set(schema['required']) - value.keys()
        extra = value.keys() - schema['properties'].keys()
        if missing or extra:
            raise ValueError(f'{path}: missing {sorted(missing)}, unexpected {sorted(extra)}')
        for name, child in value.items():
            _check(child, schema['properties'][name], f'{path}.{name}')
    elif kind == 'array':
        for index, child in enumerate(value):
            _check(child, schema['items'], f'{path}[{index}]')
    elif kind == 'string' and schema.get('minLength') and not value.strip():
        raise ValueError(f'{path} must be nonempty')


def validate_arguments(name, arguments, definitions):
    schema = next((d['function']['parameters'] for d in definitions if d['function']['name'] == name), None)
    if schema is None:
        raise ValueError(f'Unknown or unavailable tool: {name}')
    _check(arguments, schema, name)
    if name == 'read':
        start = arguments.get('start', 1)
        end = arguments.get('end', start + READ_LINE_LIMIT - 1)
        if end < start or end - start >= READ_LINE_LIMIT:
            raise ValueError('read range must contain 1 to 200 lines')
    if name == 'harmony':
        if arguments['operation'] == 'navigate' and arguments.get('key') not in {'Back', 'Home'}:
            raise ValueError('harmony.navigate needs Back or Home')
        missing = set(HARMONY_FIELDS[arguments['operation']]) - arguments.keys()
        if missing:
            raise ValueError(f'harmony.{arguments["operation"]}: missing {sorted(missing)}')
