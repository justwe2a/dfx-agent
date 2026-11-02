"""Shared HDC and UI primitives; no application business assertions."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import time
import uuid

from .workspace import read_json, save_json
from .validation import EXECUTION_ENV, execute


class Blocked(RuntimeError):
    """Environment, interaction or evidence prerequisites are missing."""
    def __init__(self, message, *, failure_kind=None):
        super().__init__(message)
        self.failure_kind = failure_kind


class TargetNotVisible(Blocked):
    """A valid capture lacks the target; no foreign UI becomes an observation."""
    def __init__(self, system_dialog=False):
        super().__init__('Target application window is not visible; UI content withheld',
                         failure_kind='target_not_visible')
        self.system_dialog = system_dialog

    def observation(self):
        return {'status': 'NOT_VISIBLE', 'failure_kind': self.failure_kind,
                'error': str(self), 'ui_content_withheld': True,
                **({'system_dialog_present': True,
                    'note': 'A system permission dialog is visible. Use request_user_action for the operator decision; do not loop captures or infer approval.'}
                   if self.system_dialog else {})}


class ScriptDeadline(TimeoutError):
    """The shared operation deadline expired, distinct from an HDC failure."""


class ScreenLocked(Blocked):
    def __init__(self, state='LOCKED'):
        super().__init__('Device screen is locked; system UI content withheld',
                         failure_kind='credential_required' if state == 'CREDENTIAL_REQUIRED' else 'screen_locked')
        self.state = state

    def observation(self, allowed=False, *, fresh=True):
        recoverable = allowed and self.state == 'LOCKED'
        return {'status': 'SCREEN_LOCKED' if recoverable else 'BLOCKED',
                'screen_state': self.state, 'failure_kind': self.failure_kind,
                'environment_blocked': not recoverable, 'error': str(self),
                'target_visible': None, 'wake_sent': False, 'swipe_sent': False,
                'fresh_capture': fresh, 'ui_content_withheld': True}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def nodes(tree):
    yield tree
    for child in tree.get('children', []):
        yield from nodes(child)


def attributes(node):
    return node.get('attributes', {})


def text_nodes(tree, text):
    return [n for n in nodes(tree) if attributes(n).get('text') == text
            and attributes(n).get('visible') != 'false']


def center(node):
    values = [int(x) for x in re.findall(r'-?\d+', attributes(node).get('bounds', ''))]
    if len(values) != 4 or values[2] <= values[0] or values[3] <= values[1]:
        raise Blocked('Invalid UI bounds')
    return (values[0] + values[2]) // 2, (values[1] + values[3]) // 2


def select_nodes(tree, selector):
    if (not isinstance(selector, dict) or not selector or
            set(selector) - {'text', 'id', 'type', 'match'} or
            not any(k in selector for k in ('text', 'id', 'type')) or
            any(not isinstance(v, str) or not v for v in selector.values()) or
            selector.get('match', 'exact') not in {'exact', 'contains'}):
        raise ValueError('selector needs text/id/type and exact/contains matching')
    def match(node):
        attrs = attributes(node)
        for key in ('text', 'id', 'type'):
            if key not in selector:
                continue
            value = attrs.get(key, '')
            if key == 'text' and selector.get('match') == 'contains':
                if selector[key] not in value:
                    return False
            elif value != selector[key]:
                return False
        return True
    return [node for node in nodes(tree) if match(node)]


def visible_nodes(tree, selector):
    return [n for n in select_nodes(tree, selector) if attributes(n).get('visible') not in (False, 'false')]


def validate_layout(tree):
    if not isinstance(tree, dict) or not isinstance(tree.get('children'), list):
        raise Blocked('Invalid application layout structure', failure_kind='layout_format')
    pending = [tree]
    while pending:
        node = pending.pop()
        if (not isinstance(node, dict) or not isinstance(node.get('attributes', {}), dict)
                or not isinstance(node.get('children', []), list)):
            raise Blocked('Invalid application layout structure', failure_kind='layout_format')
        pending.extend(node.get('children', []))


def screen_classification(tree, bundle=None):
    """Structural, conservative classification. No UI text is returned or matched."""
    validate_layout(tree)
    visible = []
    pending = [(tree, None)]
    while pending:
        node, owner = pending.pop()
        attrs = attributes(node)
        if attrs.get('visible') in (False, 'false'):
            continue
        owner = attrs.get('bundleName', owner)
        visible.append((attrs, owner))
        pending.extend((child, owner) for child in node.get('children', []))
    # These explicit structural indicators are not a universal credential detector.
    # Unlock also requires the host's swipe_only declaration.
    locks = [a for a, _ in visible if 'ScreenLockRootComponent' in (a.get('id'), a.get('type'))]
    credential = any((locks or owner in {'com.ohos.sceneboard', 'com.ohos.systemui'}) and
                     (a.get('password') in (True, 'true') or
                      a.get('type') in {'PatternLock', 'PasswordInput'} or
                      a.get('id') in {'PasswordInput', 'PinInput', 'PatternLock'}) for a, owner in visible)
    if credential:
        return 'CREDENTIAL_REQUIRED', None, None
    if locks:
        bounds = [int(v) for v in re.findall(r'-?\d+', str(locks[0].get('bounds', '')))]
        return 'LOCKED', None, bounds if len(bounds) == 4 else None
    windows = [attributes(w) for w in tree['children']
               if attributes(w).get('visible') not in (False, 'false')]
    target = any(a.get('bundleName') == bundle for a in windows) if bundle else False
    # SceneBoard alone also owns the lock surface; it is not proof of a desktop.
    desktop = any(a.get('bundleName') == 'com.ohos.launcher' for a in windows)
    return ('UNLOCKED' if target or desktop else 'UNKNOWN'), (target if bundle else None), None


def application_tree(tree, bundle):
    """Keep only identified target windows; never guess ownership from UI text."""
    state, _, _ = screen_classification(tree, bundle)
    if state in {'LOCKED', 'CREDENTIAL_REQUIRED'}:
        raise ScreenLocked(state)
    windows = tree['children']
    selected = [window for window in windows if attributes(window).get('bundleName') == bundle]
    if not selected:
        if any(not isinstance(attributes(w).get('bundleName'), str)
               or not attributes(w)['bundleName'].strip() for w in windows):
            raise Blocked('Cannot identify application window ownership; UI content withheld',
                          failure_kind='application_identity')
        raise TargetNotVisible(system_dialog=any(attributes(w).get('bundleName') ==
                                                'com.ohos.notificationdialog' for w in windows))
    return {'attributes': {}, 'children': selected}


class Device:
    def __init__(self, hdc: Path, serial: str, output: Path, *, timeout=30, deadline=None, bundle=None):
        self.hdc, self.serial, self.output = hdc, serial, output.resolve()
        self.timeout = timeout
        self.deadline = deadline
        self.bundle = bundle
        self.commands, self.actions = [], []
        self.counter = 0
        self.command_counter = 0
        self.remote_prefix = '/data/local/tmp/dfx-' + uuid.uuid4().hex
        self.output.mkdir(parents=True, exist_ok=False)

    def command(self, *argv, timeout=None):
        remaining = self.deadline - time.monotonic() if self.deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise ScriptDeadline('Operation deadline reached; partial effects may exist')
        limit = self.timeout if timeout is None else timeout
        if remaining is not None:
            limit = min(limit, remaining)
        self.command_counter += 1
        log = self.output / f'command-{self.command_counter:04d}.log'
        try:
            result = execute([str(self.hdc), '-t', self.serial, *map(str, argv)], self.output,
                             {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV},
                             limit, log)
            output = log.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            raise Blocked(f'HDC evidence I/O failed: {log.name}: {exc}; '
                          'command may have executed; no automatic replay', failure_kind='evidence_io') from exc
        row = {'at': now(), 'argv': list(argv), **result, 'output': output, 'log': log.name}
        self.commands.append(row)
        with (self.output / 'commands.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        if (remaining is not None and limit == remaining and
                result.get('failure_kind') == 'timeout' and result.get('cleanup_complete')):
            raise ScriptDeadline('Operation deadline reached during HDC; process cleaned up')
        if result.get('status') == 'BLOCKED' or result.get('returncode') != 0 or '[Fail]' in output or '[E]' in output:
            raise Blocked(f'HDC failed: {argv}; {result.get("error", output[:2000])}; log={log.name}',
                          failure_kind=result.get('failure_kind'))
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ScriptDeadline('Operation completed after script deadline; side effects recorded')
        return output.strip()

    def action(self, name, **details):
        self.actions.append({'at': now(), 'action': name, **details})
        save_json(self.output / 'actions.json', self.actions)

    def screen_capture(self):
        """Read system UI only in a temporary directory outside tool-readable logs."""
        import tempfile
        self.counter += 1
        remote = self.remote_prefix + f'-screen-{self.counter}.json'
        result = self.command('shell', 'uitest', 'dumpLayout', '-p', remote)
        if 'DumpLayout saved to:' not in result:
            raise Blocked('Screen layout capture failed', failure_kind='layout_format')
        with tempfile.TemporaryDirectory(prefix='dfx-screen-') as temporary:
            received = Path(temporary) / 'capture.json'
            self.command('file', 'recv', remote, str(received))
            try:
                tree = read_json(received)
            except (ValueError, OSError) as exc:
                raise Blocked('Cannot decode screen layout', failure_kind='layout_format') from exc
            return screen_classification(tree, self.bundle)

    def screen(self, mode):
        if mode not in {'observe', 'unlock'}:
            raise ValueError('screen mode must be observe or unlock')
        previous_deadline = self.deadline
        self.deadline = min(previous_deadline, time.monotonic() + 60) if previous_deadline is not None else time.monotonic() + 60
        result = {'status': 'OBSERVED', 'screen_state': 'UNKNOWN', 'target_visible': None,
                  'wake_sent': False, 'swipe_sent': False, 'fresh_capture': False,
                  'ui_content_withheld': True, 'observations': []}
        def capture():
            state, visible, bounds = self.screen_capture()
            result.update(screen_state=state, target_visible=visible, fresh_capture=True)
            result['observations'].append({'screen_state': state, 'target_visible': visible, 'at': now()})
            return bounds
        try:
            if mode == 'unlock':
                if time.monotonic() >= self.deadline:
                    raise ScriptDeadline('Screen operation deadline reached before wake dispatch')
                result['wake_sent'] = True  # dispatched/attempted, not proof of success
                self.command('shell', 'power-shell', 'wakeup')
            bounds = capture()
            if mode == 'unlock' and result['screen_state'] == 'LOCKED':
                if not bounds or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                    result.update(status='UNKNOWN', failure_kind='lock_bounds_unknown')
                else:
                    x1, y1, x2, y2 = bounds
                    x, height = (x1 + x2) // 2, y2 - y1
                    swipe = [x, y1 + height * 8 // 10, x, y1 + height * 2 // 10]
                    if time.monotonic() >= self.deadline:
                        raise ScriptDeadline('Screen operation deadline reached before swipe dispatch')
                    result.update(swipe_sent=True, swipe=swipe, fresh_capture=False,
                                  screen_state='UNKNOWN', target_visible=None)
                    self.command('shell', 'uitest', 'uiInput', 'swipe', *map(str, swipe))
                    observation_end = min(self.deadline, time.monotonic() + 10)
                    self.deadline = observation_end
                    while time.monotonic() < observation_end:
                        capture()
                        if result['screen_state'] != 'LOCKED':
                            break
                        time.sleep(min(.5, max(0, observation_end - time.monotonic())))
            if result['screen_state'] == 'CREDENTIAL_REQUIRED':
                result.update(status='BLOCKED', failure_kind='credential_required', environment_blocked=True)
            elif result['screen_state'] == 'LOCKED' and result['status'] != 'UNKNOWN':
                result.update(status='SCREEN_LOCKED', failure_kind='screen_locked')
            elif result['screen_state'] == 'UNKNOWN':
                result['status'] = 'UNKNOWN'
        except ScriptDeadline as exc:
            result.update(status='TIMEOUT', failure_kind='timeout', error=str(exc),
                          partial_side_effects_possible=result['wake_sent'] or result['swipe_sent'])
        except Blocked as exc:
            result.update(status='BLOCKED', environment_blocked=True,
                          failure_kind=exc.failure_kind, error=str(exc),
                          partial_side_effects_possible=result['wake_sent'] or result['swipe_sent'])
        finally:
            self.deadline = previous_deadline
            self.action('screen', mode=mode, result=result)
        return result

    def layout(self, label='layout'):
        self.counter += 1
        stem = f'{self.counter:04d}-{label}'
        remote = self.remote_prefix + f'-{self.counter}.json'
        args = ['shell', 'uitest', 'dumpLayout', '-p', remote]
        result = self.command(*args)
        if 'DumpLayout saved to:' not in result:
            raise Blocked('UI layout capture failed')
        local = self.output / (stem + '.json')
        if self.bundle:
            # Receive outside the model-readable tool directory. Only the
            # identified application tree is retained as a tool observation.
            import tempfile
            with tempfile.TemporaryDirectory(prefix='dfx-ui-') as temporary:
                received = Path(temporary) / 'capture.json'
                self.command('file', 'recv', remote, str(received))
                try:
                    captured = read_json(received)
                except (ValueError, OSError) as exc:
                    raise Blocked('Cannot read or decode application layout', failure_kind='layout_format') from exc
                tree = application_tree(captured, self.bundle)
            save_json(local, tree)
        else:
            self.command('file', 'recv', remote, str(local))
            tree = read_json(local)
        if any(attributes(n).get('type') == 'ScreenLockRootComponent' or
               attributes(n).get('id') == 'ScreenLockRootComponent' for n in nodes(tree)):
            raise ScreenLocked()
        return tree

    def screenshot(self, label):
        remote = self.remote_prefix + '.png'
        self.command('shell', 'uitest', 'screenCap', '-p', remote)
        local = self.output / (label + '.png')
        self.command('file', 'recv', remote, str(local))
        if not local.is_file() or not local.stat().st_size:
            raise Blocked('Screenshot file missing or empty')

    def click_node(self, node):
        x, y = center(node)
        self.action('click', text=attributes(node).get('text'), x=x, y=y)
        self.command('shell', 'uitest', 'uiInput', 'click', str(x), str(y))
        time.sleep(.3)

    def click(self, tree, text):
        matches = text_nodes(tree, text)
        if len(matches) != 1:
            raise Blocked(f'Expected one visible control: {text!r}, found {len(matches)}')
        self.click_node(matches[0])

    def target(self, selector):
        matches = visible_nodes(self.layout(), selector)
        if len(matches) != 1:
            raise ValueError(f'Expected one visible control, found {len(matches)}: ' +
                             json.dumps([attributes(n) for n in matches[:10]], ensure_ascii=False))
        if attributes(matches[0]).get('enabled') in (False, 'false'):
            raise ValueError('Selected control is disabled')
        return matches[0]

    def wait_for(self, selector, timeout=5):
        if type(timeout) not in (int, float) or not 0 < timeout <= self.timeout:
            raise ValueError('wait timeout must be positive and within device timeout')
        original_deadline = self.deadline
        observation_deadline = time.monotonic() + timeout
        # The polling window decides whether to start/accept another sample.
        # It must not turn a 1s selector wait into a 1s kill of HDC. Each sample
        # remains bounded by the command limit and the enclosing script budget.
        self.deadline = original_deadline if original_deadline is not None else time.monotonic() + self.timeout
        try:
            while True:
                if time.monotonic() >= observation_deadline:
                    return {'status': 'TIMEOUT', 'matches': [], 'error': 'Control wait expired'}
                tree = self.layout()
                if original_deadline is not None and time.monotonic() >= original_deadline:
                    raise ScriptDeadline('Enclosing script deadline reached during observation')
                if time.monotonic() >= observation_deadline:
                    return {'status': 'TIMEOUT', 'matches': [], 'error': 'Control wait expired'}
                found = visible_nodes(tree, selector)
                if found:
                    return {'status': 'OBSERVED', 'matches': [attributes(n) for n in found]}
                remaining = observation_deadline-time.monotonic()
                if remaining <= 0:
                    raise ScriptDeadline('Control wait expired')
                time.sleep(min(.5, remaining))
        except ScriptDeadline:
            if original_deadline is not None and time.monotonic() >= original_deadline:
                raise
            return {'status': 'TIMEOUT', 'matches': [], 'error': 'Control wait expired'}
        finally:
            self.deadline = original_deadline

    def pid(self, bundle):
        value = self.command('shell', 'pidof', bundle)
        return value if value.isdigit() else ''

    def package(self, bundle):
        raw = self.command('shell', 'bm', 'dump', '-n', bundle)
        if any(marker in raw.lower() for marker in ('not installed', 'bundle not found', 'bundle does not exist', 'bundle name not found')):
            raise ValueError('Application is not installed; build and install it before querying its package')
        if raw.strip().lower() == 'error: failed to get information and the parameters may be wrong.':
            raise ValueError('Package query failed: check the bundle name and whether the application has been installed')
        try:
            data = json.loads(raw[raw.index('{'):])
            if data.get('name', data.get('applicationInfo', {}).get('bundleName')) != bundle:
                raise ValueError('Bundle mismatch')
            return {key: data.get(key) for key in ('name', 'versionCode', 'versionName', 'updateTime', 'uid')}
        except (ValueError, KeyError) as exc:
            raise Blocked('Cannot identify installed application') from exc

    def faults(self, bundle):
        raw = self.command('shell', 'ls', '/data/log/faultlog/faultlogger')
        if 'Permission denied' in raw or 'No such' in raw:
            raise Blocked('Fault log directory is unavailable')
        return set(re.findall(r'jscrash-' + re.escape(bundle) + r'-[^\s/]+\.log', raw))
