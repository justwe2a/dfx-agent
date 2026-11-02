"""Completed-boundary checkpoints around upstream mini query/save hooks."""
from pathlib import Path
import shutil

from . import __version__
from .workspace import read_json, save_json


def complete_history(messages):
    pending = set()
    json_action_pending = False
    for message in messages:
        if message.get('role') == 'exit':
            return False
        if message.get('role') == 'assistant':
            if pending or json_action_pending:
                return False
            calls = message.get('tool_calls', [])
            ids = [c.get('id') for c in calls]
            if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                return False
            pending.update(ids)
            json_action_pending = not calls and bool(message.get('extra', {}).get('actions'))
        elif message.get('role') == 'tool':
            identity = message.get('tool_call_id')
            if identity not in pending:
                return False
            pending.remove(identity)
        elif pending:
            return False
        elif message.get('role') == 'user':
            json_action_pending = False
    return not pending and not json_action_pending


def runtime_class(base):
    class RuntimeAgent(base):
        """Keep upstream run/step/exception control flow unchanged."""
        def __init__(self, *args, resume_data=None, pause_after_tools=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.resume_data = resume_data
            self.pause_target = (len(self.env.events) + pause_after_tools
                                 if pause_after_tools is not None else None)

        def query(self):
            if self.resume_data is not None:
                previous = self.resume_data
                self.messages = previous['messages']
                self.n_calls = previous['checkpoint']['core_calls']
                self.n_consecutive_format_errors = previous['checkpoint']['consecutive_format_errors']
                if previous.get('user_action_ack'):
                    self.messages = [*self.messages, {'role': 'user', 'content': previous['user_action_ack']}]
                self.resume_data = None
            if self.pause_target is not None and len(self.env.events) >= self.pause_target:
                from minisweagent.exceptions import InterruptAgentFlow
                raise InterruptAgentFlow({'role': 'exit', 'content': 'Explicit tool-boundary pause',
                    'extra': {'exit_status': 'Paused', 'submission': ''}})
            return super().query()

        def save(self, path, *extra_dicts):
            data = super().save(path, *extra_dicts)
            status = data['info']['exit_status']
            messages = self.messages[:-1] if self.messages and self.messages[-1]['role'] == 'exit' else self.messages
            allowed = {'', 'Paused', 'Interrupted', 'BLOCKED', 'Submitted', 'LimitsExceeded',
                       'ContextLimit', 'RepeatedFormatError', 'FixtureExhausted'}
            last = self.model.output / f'{self.model.calls:04d}'
            response_complete = (self.model.calls == 0 or (last / 'response.json').exists() or
                                (last / 'rejected-content.txt').exists())
            if (path and status in allowed and complete_history(messages) and response_complete and
                    sum(m.get('role') == 'assistant' for m in messages) == self.model.calls and
                    not (self.env.output / 'started.json').exists()):
                save_json(Path(path).parent / 'checkpoint.json', {
                    'format': 'dfx-completed-boundary-1', 'engine_version': __version__,
                    'message_count': len(messages), 'model_calls': self.model.calls,
                    'api_calls': self.model.api_calls, 'tool_calls': len(self.env.events),
                    'core_calls': self.n_calls,
                    'consecutive_format_errors': self.n_consecutive_format_errors})
            return data
    return RuntimeAgent


def load_continuation(output):
    """Read and validate before writing archives or invoking a provider."""
    launch = read_json(output / 'launch.json')
    if launch.get('format') != 'dfx-agent-runtime-1' or launch.get('engine_version') != __version__:
        raise ValueError('Resume requires a task created by this runtime version; old experiments stay frozen')
    result = read_json(output / 'result.json')
    if result.get('status') not in {'Paused', 'Interrupted', 'BLOCKED'} or result.get('error'):
        raise ValueError('Only explicitly paused/interrupted/blocked completed tasks can resume')
    checkpoint = read_json(output / 'checkpoint.json')
    if checkpoint.get('format') != 'dfx-completed-boundary-1' or checkpoint.get('engine_version') != __version__:
        raise ValueError('Missing compatible completed boundary')
    trajectory = read_json(output / 'trajectory.json')
    messages = trajectory['messages']
    if messages and messages[-1]['role'] == 'exit':
        messages = messages[:-1]
    if len(messages) != checkpoint['message_count'] or not complete_history(messages):
        raise ValueError('Incomplete or unknown message tail; no replay')
    if sum(m.get('role') == 'assistant' for m in messages) != checkpoint['model_calls']:
        raise ValueError('Model response history differs from completed boundary')
    if (output / 'tools/started.json').exists():
        raise ValueError('Interrupted action state unknown; no replay')
    calls = sorted(p.name for p in (output / 'model').glob('*') if p.is_dir() and p.name.isdecimal())
    if calls != [f'{i:04d}' for i in range(1, checkpoint['model_calls']+1)]:
        raise ValueError('Request tail differs from completed boundary; no replay')
    import json
    event_path = output / 'tools/events.jsonl'
    events = [json.loads(line) for line in event_path.read_text(encoding='utf-8').splitlines()] if event_path.exists() else []
    if (len(events) != checkpoint['tool_calls'] or
            [e['number'] for e in events] != list(range(1, len(events)+1)) or
            any(e['result'].get('status') == 'INTERRUPTED' for e in events)):
        raise ValueError('Unknown or incomplete tool events; no replay')
    for key in ('model_calls', 'api_calls', 'tool_calls'):
        if result[key] != checkpoint[key]:
            raise ValueError('Result and completed boundary counts disagree')
    if checkpoint['core_calls'] >= launch['options']['max_steps']:
        raise ValueError('Original request allowance exhausted; resume does not reset limits')
    if checkpoint['consecutive_format_errors'] >= 3:
        raise ValueError('Consecutive format limit exhausted')
    return {'launch': launch, 'checkpoint': checkpoint, 'result': result,
            'messages': messages, 'events': events, 'trajectory': trajectory}


def archive_segment(output, data):
    directory = output / 'continuations'
    directory.mkdir(exist_ok=True)
    index = len(list(directory.iterdir())) + 1
    archive = directory / f'{index:04d}'
    archive.mkdir()
    for name in ('result.json', 'trajectory.json', 'checkpoint.json', 'report.md'):
        shutil.copy2(output / name, archive / name)
    save_json(archive / 'resume.json', {'source_status': data['result']['status'],
              'model_calls': data['checkpoint']['model_calls'], 'tool_calls': data['checkpoint']['tool_calls'],
              'user_action_ack': data.get('user_action_ack'),
              'mode': 'explicit full-history continuation; no replay'})
    return index
