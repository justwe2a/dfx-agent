"""Run workspace-authored main(device) checks; this is not an OS sandbox."""
import json
from pathlib import Path
import runpy
import sys
import time
import traceback

from .harmony_device import Device, Blocked, ScreenLocked, TargetNotVisible, ScriptDeadline
from .harmony_tools import HarmonyTools
from .workspace import read_json, save_json


class DriverFailure(Blocked):
    """An unclassified adapter failure cannot become a normal script error."""


class ScriptDevice:
    def __init__(self, adapter, device):
        self.adapter, self.device = adapter, device

    def perform(self, operation, **arguments):
        if operation not in {'layout', 'tap', 'input', 'tap_text', 'input_text', 'swipe', 'key', 'navigate',
                             'wait', 'package', 'pid', 'faults', 'fault', 'screenshot', 'screen'}:
            raise ValueError('Unsupported script observation/action')
        # The script is already bound to one application. Missing an optional
        # bundle must not become a KeyError classified as infrastructure failure.
        if operation in {'package', 'pid', 'faults', 'fault'}:
            arguments.setdefault('bundle', self.adapter.target_bundle)
        required = {'fault': ('name',), 'screen': ('mode',), 'tap': ('x', 'y'),
                    'swipe': ('x1', 'y1', 'x2', 'y2', 'duration_ms'),
                    'key': ('key',), 'navigate': ('key',), 'input': ('text', 'x', 'y'),
                    'tap_text': ('selector',), 'input_text': ('selector', 'text'), 'wait': ('selector',)}
        missing = [key for key in required.get(operation, ()) if key not in arguments]
        if missing:
            raise ValueError(f'{operation} requires: ' + ', '.join(missing))
        try:
            result = self.adapter.device_action(self.device, {'operation': operation, **arguments})
            self.device.action(operation, arguments=arguments, result=result)
            if result.get('status') == 'BLOCKED' and result.get('environment_blocked'):
                failure = Blocked(result.get('error', 'Screen recovery blocked'), failure_kind=result.get('failure_kind'))
                failure.result = result
                raise failure
            return result
        except ScreenLocked as exc:
            self.device.action(operation, arguments=arguments, result=exc.observation(self.adapter.allow_screen_unlock))
            raise
        except TargetNotVisible as exc:
            self.device.action(operation, arguments=arguments, result=exc.observation())
            raise
        except (ValueError, Blocked, ScriptDeadline):
            raise
        except Exception as exc:
            raise DriverFailure(f'Device driver failure: {type(exc).__name__}: {exc}') from exc

    def layout(self, *, offset=0, limit=40, saved_layout=None, **selector):
        return self.perform('layout', offset=offset, limit=limit,
                            **({'saved_layout': saved_layout} if saved_layout is not None else {}),
                            **({'selector': selector} if selector else {}))

    def tap(self, **selector):
        return self.perform('tap_text', selector=selector)

    def input(self, value, **selector):
        return self.perform('input_text', selector=selector, text=value)

    def wait(self, timeout=5, **selector):
        return self.perform('wait', selector=selector, timeout_seconds=timeout)

    def key(self, key):
        return self.perform('key', key=key)

    def swipe(self, x1, y1, x2, y2, duration_ms):
        return self.perform('swipe', x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms)


def run(config):
    out = Path(config['output'])
    adapter = HarmonyTools(Path(config['workspace']), Path(config['tools_output']), config['host'],
                           allow_device=True, device_timeout=config['timeout'],
                           allow_screen_unlock=config.get('allow_screen_unlock', False),
                           allow_system_navigation=config.get('allow_system_navigation', False))
    adapter.target_bundle = config['bundle']
    device = Device(Path(config['hdc']), config['host']['device'], out / 'device',
                    timeout=config['timeout'], deadline=config['deadline'], bundle=config['bundle'])
    try:
        namespace = runpy.run_path(config['script'], run_name='dfx_workspace_check')
        main = namespace.get('main')
        if not callable(main):
            raise ValueError('Workspace script must define main(device)')
        main(ScriptDevice(adapter, device))
        if time.monotonic() >= device.deadline:
            raise ScriptDeadline('Script returned after operation deadline')
        result = {'status': 'EXECUTED', 'note': 'Script completed; not independent acceptance'}
    except ScreenLocked as exc:
        result = {**exc.observation(adapter.allow_screen_unlock), 'script_completed': False,
                  'partial_side_effects_possible': True}
    except TargetNotVisible as exc:
        result = {**exc.observation(), 'script_completed': False,
                  'partial_side_effects_possible': True}
    except ScriptDeadline as exc:
        result = {'status': 'TIMEOUT', 'error': str(exc), 'partial_side_effects_possible': True}
    except DriverFailure as exc:
        result = {'status': 'BLOCKED', 'environment_blocked': True, 'failure_kind': 'driver', 'error': str(exc)}
    except Blocked as exc:
        result = {**getattr(exc, 'result', {}), 'status': 'BLOCKED', 'environment_blocked': True,
                  'script_completed': False, 'error': str(exc)}
        if exc.failure_kind:
            result['failure_kind'] = exc.failure_kind
    except AssertionError as exc:
        traceback.print_exc()
        result = {'status': 'FAILED', 'error': f'AssertionError: {exc}'}
    except Exception as exc:
        traceback.print_exc()
        result = {'status': 'TOOL_ERROR', 'error': f'{type(exc).__name__}: {exc}'}
    save_json(out / 'script-result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'EXECUTED' else 3


if __name__ == '__main__':
    raise SystemExit(run(read_json(Path(sys.argv[1]))))
