"""Small shared HarmonyOS adapter for the mini environment, without case controllers."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from .harmony_build import build_command, build_environment, dependency_command, package_metadata, project_modules
from .harmony_device import Device, Blocked, ScreenLocked, TargetNotVisible, nodes, attributes, select_nodes, center, application_tree
from .validation import execute, EXECUTION_ENV, SCRIPT_FINALIZATION_SECONDS
from .workspace import confined, read_json, save_json
from .tool_schema import LAYOUT_LIMIT


def build_diagnostics(lines):
    """Prioritize recorded errors, retaining their line numbers and full log elsewhere."""
    errors = [i for i, line in enumerate(lines)
              if re.search(r'\berror\b|\bfailed\b|\bfatal\b|\bENOENT\b|\bEACCES\b', line, re.I)]
    warnings = [i for i, line in enumerate(lines) if re.search(r'\bwarn(?:ing)?\b', line, re.I)
                and i not in errors]
    # Error lines first; one following context line per error then fits any remaining slots.
    context = [i + 1 for i in errors if i + 1 < len(lines) and lines[i + 1].strip()
               and i + 1 not in errors and i + 1 not in warnings]
    indices = list(dict.fromkeys(errors + context)) if errors else warnings
    chosen = indices[:12]
    return {'diagnostics': [lines[i][:500] for i in chosen],
            'diagnostic_lines': [i + 1 for i in chosen],
            'diagnostics_truncated': len(indices) > 12 or any(len(lines[i]) > 500 for i in chosen),
            'warnings': [lines[i][:500] for i in warnings[:6]],
            'warning_count': len(warnings),
            'warnings_truncated': len(warnings) > 6 or any(len(lines[i]) > 500 for i in warnings[:6])}


def load_host(path):
    data = read_json(path)
    fields = {'python', 'deveco', 'device', 'dataset', 'workspaces', 'reports', 'screen_lock_profile'}
    if not isinstance(data, dict) or set(data) - fields:
        raise ValueError('Unknown host fields')
    if not isinstance(data.get('deveco'), str) or not data['deveco'].strip():
        raise ValueError('HarmonyOS host requires deveco')
    serial = data.get('device', '')
    if not isinstance(serial, str):
        raise ValueError('device must be a string')
    if 'screen_lock_profile' in data and data['screen_lock_profile'] != 'swipe_only':
        raise ValueError('screen_lock_profile must be swipe_only when provided')
    return {'deveco': str((path.resolve().parent / data['deveco']).resolve()), 'device': serial,
            **({'screen_lock_profile': data['screen_lock_profile']} if 'screen_lock_profile' in data else {})}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
        raise ValueError('Expected a bundle/module/product/target/ability identifier')
    return value


def layout_page(tree, offset=0, limit=40, selector=None):
    """Project a captured tree into bounded, valid JSON; never cut a JSON prefix."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= LAYOUT_LIMIT:
        raise ValueError('layout offset must be nonnegative; limit must be 1..80')
    fields = ('accessibilityId', 'id', 'key', 'type', 'text', 'originalText', 'description',
              'bounds', 'visible', 'enabled', 'clickable', 'longClickable', 'scrollable',
              'checkable', 'checked', 'selected', 'focused')
    projected, raw_count = [], 0
    matching = {id(n) for n in select_nodes(tree, selector)} if selector is not None else None
    for index, node in enumerate(nodes(tree)):
        raw_count += 1
        attrs = attributes(node)
        if matching is not None and id(node) not in matching:
            continue
        if matching is None and not (any(attrs.get(k) for k in ('text', 'originalText', 'description'))
                or any(attrs.get(k) in (True, 'true') for k in ('clickable', 'scrollable', 'checkable'))):
            continue
        item = {'index': index, **{k: attrs[k] for k in fields if k in attrs and attrs[k] not in ('', None)}}
        if item.get('originalText') == item.get('text'):
            item.pop('originalText', None)
        for key in ('visible', 'enabled', 'clickable', 'longClickable', 'scrollable', 'checkable', 'checked', 'selected', 'focused'):
            if item.get(key) in ('true', 'false'):
                item[key] = item[key] == 'true'
        shortened = [k for k, value in item.items() if isinstance(value, str) and len(value) > 500]
        for k in shortened:
            item[k] = item[k][:500]
        if shortened:
            item['truncated_fields'] = shortened
        projected.append(item)
    selected = []
    for item in projected[offset:offset + limit]:
        if selected and len(json.dumps(selected + [item], ensure_ascii=False)) > 8000:
            break
        selected.append(item)
    next_offset = offset + len(selected)
    more = next_offset < len(projected)
    return {'status': 'OBSERVED', 'nodes': selected, 'raw_node_count': raw_count,
            'total_nodes': len(projected), 'total_matches': len(projected), 'offset': offset,
            'next_offset': next_offset if more else None, 'truncated': more,
            'projection': 'full-tree selector or text/action nodes; full tree at layout path'}


class HarmonyTools:
    def __init__(self, root, output, host, *, allow_device=False, allow_dependency_install=False,
                 allow_system_navigation=False, allow_screen_unlock=False,
                 build_timeout=600, device_timeout=60, script_timeout=120):
        self.root, self.output, self.host = root, output, host
        self.deveco = Path(host['deveco'])
        self.hdc = self.deveco / 'sdk/default/openharmony/toolchains/hdc.exe'
        self.allow_device, self.allow_dependency_install = allow_device, allow_dependency_install
        self.allow_system_navigation = allow_system_navigation
        if allow_screen_unlock and (not allow_device or host.get('screen_lock_profile') != 'swipe_only'):
            raise ValueError('Screen unlock requires device permission and swipe_only host profile')
        self.allow_screen_unlock = allow_screen_unlock
        self.build_timeout, self.device_timeout = build_timeout, device_timeout
        self.script_timeout = script_timeout
        self.built = set()
        self.package_sets = {}
        binding = self.output / 'target-application.json'
        self.target_bundle = identifier(read_json(binding)['bundle']) if binding.exists() else None

    def capabilities(self):
        return {'configured': True, 'device_enabled': self.allow_device,
                'system_navigation_enabled': self.allow_system_navigation,
                'screen_unlock_enabled': self.allow_screen_unlock,
                'dependency_install_enabled': self.allow_dependency_install,
                'build_timeout_seconds': self.build_timeout, 'device_timeout_seconds': self.device_timeout,
                'script_timeout_seconds': self.script_timeout,
                'script_finalization_seconds': SCRIPT_FINALIZATION_SECONDS}

    def execute(self, action, number):
        operation = action['operation']
        if operation == 'info':
            return self.capabilities()
        if operation == 'layout':
            offset, limit = action.get('offset', 0), action.get('limit', 40)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= LAYOUT_LIMIT:
                raise ValueError('layout offset must be nonnegative; limit must be 1..80')
        out = self.output / f'harmony-{number:04d}'
        out.mkdir()
        try:
            if operation in {'build', 'dependencies'}:
                result = self.build(action, out)
            elif operation == 'layout' and action.get('saved_layout'):
                result = self.saved_layout(action)
            else:
                if not self.allow_device:
                    raise ValueError('Device operations are paused; no HDC command dispatched')
                if not self.host['device']:
                    raise Blocked('No device configured')
                if operation == 'run_script':
                    result = self.run_script(action, out)
                else:
                    device = Device(self.hdc,
                                    self.host['device'], out / 'device', timeout=self.device_timeout,
                                    bundle=self.target_bundle)
                    result = self.device_action(device, action)
        except ScreenLocked as exc:
            result = exc.observation(self.allow_screen_unlock,
                                     fresh=operation != 'start' and not bool(action.get('saved_layout')))
        except TargetNotVisible as exc:
            result = {**exc.observation(), 'fresh_capture': not bool(action.get('saved_layout'))}
        except Blocked as exc:
            result = {'status': 'BLOCKED', 'error': str(exc), 'environment_blocked': True}
            if exc.failure_kind:
                result['failure_kind'] = exc.failure_kind
        save_json(out / 'result.json', result)
        if operation in {'build', 'dependencies'}:
            raw = result.get('output', '')
            lines = re.sub(r'\x1b\[[0-9;]*m', '', raw).splitlines()
            result = {k: v for k, v in result.items() if k not in {'output', 'argv'}}
            result.update(**build_diagnostics(lines),
                          output_tail='\n'.join(lines[-8:])[-2000:],
                          truncated=len(lines) > 8 or len('\n'.join(lines[-8:])) > 2000,
                          command_log=(out / 'command.log').relative_to(self.output).as_posix())
        else:
            # Ordinary text outputs remain text, with full originals in result.json.
            result = dict(result)
            for key in ('output', 'content'):
                if isinstance(result.get(key), str) and len(result[key]) > 8000:
                    result[key] = result[key][:8000]
                    result['truncated'] = True
        return {**result, 'evidence': out.relative_to(self.output).as_posix(),
                'log': (out / 'result.json').relative_to(self.output).as_posix(), 'log_source': 'tool-log'}

    def run_script(self, action, out):
        if not self.target_bundle:
            raise ValueError('Start the task application before running device checks')
        script = confined(self.root, action['script'])
        if script.suffix != '.py' or not script.is_file():
            raise ValueError('run_script requires an existing workspace Python file')
        config = {'workspace': str(self.root.resolve()), 'tools_output': str(self.output.resolve()),
                  'output': str(out.resolve()), 'host': self.host, 'script': str(script.resolve()),
                  'hdc': str(self.hdc),
                  'bundle': self.target_bundle,
                  'allow_system_navigation': self.allow_system_navigation,
                  'allow_screen_unlock': self.allow_screen_unlock,
                  'timeout': self.device_timeout, 'script_timeout': self.script_timeout,
                  'deadline': time.monotonic() + self.script_timeout}
        save_json(out / 'script-config.json', config)
        env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent), PYTHONUTF8='1',
                   PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                   GIT_CEILING_DIRECTORIES=str(self.root.resolve().parent))
        log = out / 'command.log'
        execution = execute([sys.executable, '-m', 'dfx_agent.harmony_script',
                             str((out / 'script-config.json').resolve())],
                            self.root, env, self.script_timeout + SCRIPT_FINALIZATION_SECONDS, log)
        if execution.get('failure_kind') == 'timeout' and execution.get('cleanup_complete'):
            result = {**execution, 'status': 'TIMEOUT', 'partial_side_effects_possible': True}
        elif execution.get('status') == 'BLOCKED':
            result = {**execution, 'environment_blocked': True}
        elif not (out / 'script-result.json').exists():
            result = {'status': 'BLOCKED', 'environment_blocked': True,
                      'failure_kind': 'unknown_script_exit',
                      'error': 'Script runner exited without a result; inspect command log', **execution}
        else:
            result = {**read_json(out / 'script-result.json'), **execution}
            if (result.get('status') == 'EXECUTED') != (execution['returncode'] == 0):
                result.update(status='BLOCKED', environment_blocked=True,
                              failure_kind='unknown_script_exit',
                              error='Script result and process exit disagree')
        result.update(output=log.read_text(encoding='utf-8', errors='replace'),
                      command_log=log.relative_to(self.output).as_posix(),
                      script_timeout_seconds=self.script_timeout,
                      finalization_seconds=SCRIPT_FINALIZATION_SECONDS)
        return result

    def build(self, action, out):
        directory = action.get('cwd', '.')
        cwd = self.root if directory == '.' else confined(self.root, directory)
        if not cwd.is_dir():
            raise ValueError('Build cwd must be a directory in the working copy')
        artifact = None
        if action['operation'] == 'dependencies':
            if not self.allow_dependency_install:
                raise ValueError('Dependency installation is not enabled')
            self.built.clear()
            argv = dependency_command(self.deveco)
        else:
            argv = build_command(self.deveco, identifier(action['module']),
                                 identifier(action['product']), identifier(action['target']))
            artifact = confined(self.root, action['artifact'])
            if artifact.suffix != '.hap':
                raise ValueError('artifact must name a .hap relative to workspace')
            self.built.discard(artifact)
            if artifact.exists():
                # Preserve the previous output; require this command to produce a new file.
                artifact.replace(out / 'previous.hap')
        log = out / 'command.log'
        started = time.monotonic()
        result = execute(argv, cwd, build_environment(self.deveco), self.build_timeout, log)
        result.update(argv=argv, output=log.read_text(encoding='utf-8', errors='replace'))
        if result.get('status') == 'BLOCKED':
            result['environment_blocked'] = True
        if result.get('status') != 'BLOCKED':
            result['status'] = 'EXECUTED' if result['returncode'] == 0 else 'FAILED'
            if artifact is not None and result['returncode'] == 0:
                if not artifact.is_file() or not artifact.stat().st_size:
                    result.update(status='FAILED', error='Build exited zero without a new nonempty HAP')
                else:
                    result = self.complete_packages(artifact, action, cwd, out, result,
                                                    started + self.build_timeout)
        return result

    def complete_packages(self, artifact, action, cwd, out, result, deadline):
        """One build per required HSP, then install dependencies before dependents."""
        receipts, ordered, visiting, done = [], [], set(), set()
        modules = None
        bundle = None

        def visit(path):
            nonlocal modules, bundle
            data = package_metadata(path)
            name = data['module']['name']
            current_bundle = data.get('app', {}).get('bundleName')
            if bundle is None:
                bundle = current_bundle
            if not bundle or current_bundle != bundle:
                raise ValueError('HSP bundle identity differs from entry HAP')
            if name in visiting:
                raise ValueError('Cyclic HSP dependency: ' + name)
            if name in done:
                return
            visiting.add(name)
            for dependency in data['module'].get('dependencies', []):
                dep = identifier(dependency['moduleName'])
                if dependency.get('bundleName', bundle) != bundle:
                    raise ValueError('External shared bundle requires separate installation: ' + dep)
                if dep in visiting:
                    raise ValueError('Cyclic HSP dependency: ' + dep)
                if dep in done:
                    continue
                if modules is None:
                    modules = project_modules(self.deveco, cwd)
                choices = [m for m in modules if m['module'] == dep and m['type'] == 'shared']
                if len(choices) != 1:
                    raise ValueError('Cannot identify one project shared module: ' + dep)
                module = choices[0]
                directory = (cwd / module['path']).resolve()
                if not directory.is_relative_to(cwd.resolve()):
                    raise ValueError('Shared module is outside project: ' + dep)
                product, target = identifier(action['product']), identifier(action['target'])
                dest = directory / 'build' / product / 'outputs' / target
                # Preserve previous packages and require a newly generated artifact.
                prior = sorted(dest.glob('*.hsp'))
                for i, previous in enumerate(prior):
                    previous.replace(out / f'previous-{dep}-{i}.hsp')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Blocked('Shared build reached the total build deadline', failure_kind='timeout')
                log = out / f'shared-{dep}.log'
                command = build_command(self.deveco, identifier(module['name']), product, target, 'assembleHsp')
                reply = execute(command, cwd, build_environment(self.deveco), remaining, log)
                output = log.read_text(encoding='utf-8', errors='replace')
                receipts.append({'module': dep, **reply, 'command_log': log.relative_to(self.output).as_posix()})
                if reply.get('status') == 'BLOCKED':
                    raise Blocked(f'HSP {dep}: {reply.get("error")}; log={log.name}',
                                  failure_kind=reply.get('failure_kind'))
                if reply.get('returncode') != 0:
                    raise ValueError(f'HSP {dep} build failed; log={log.name}\n' + output[-3000:])
                outputs = list(dest.glob('*-unsigned.hsp')) or list(dest.glob('*.hsp'))
                if len(outputs) != 1 or not outputs[0].stat().st_size:
                    raise ValueError('Expected one newly built HSP: ' + dep)
                if package_metadata(outputs[0])['module']['name'] != dep:
                    raise ValueError('HSP module identity differs from requested dependency: ' + dep)
                visit(outputs[0])
            visiting.remove(name)
            done.add(name)
            ordered.append(path)

        try:
            visit(artifact)
        except Blocked as exc:
            result.update(status='BLOCKED', environment_blocked=True, error=str(exc), failure_kind=exc.failure_kind)
        except subprocess.TimeoutExpired:
            result.update(status='BLOCKED', environment_blocked=True,
                          error='Project module reader timed out; no automatic retry', failure_kind='timeout')
        except (ValueError, KeyError, OSError) as exc:
            result.update(status='FAILED', error=str(exc))
        else:
            self.built.add(artifact)
            self.package_sets[artifact] = ordered
            result.update(artifact=artifact.relative_to(self.root).as_posix(),
                          packages=[p.relative_to(self.root).as_posix() for p in ordered])
        result['shared_builds'] = receipts
        return result

    def saved_layout(self, action):
        if not self.target_bundle:
            raise ValueError('Start the task application to bind UI observations first')
        saved = confined(self.output, action['saved_layout'])
        return {**layout_page(application_tree(read_json(saved), self.target_bundle),
                              action.get('offset', 0), action.get('limit', 40), action.get('selector')),
                'layout': saved.relative_to(self.output).as_posix(), 'fresh_capture': False}

    def device_action(self, device, action):
        operation = action['operation']
        if operation == 'layout' and action.get('saved_layout'):
            return self.saved_layout(action)
        if operation == 'screen':
            if not self.allow_device or not self.allow_screen_unlock or self.host.get('screen_lock_profile') != 'swipe_only':
                raise ValueError('Screen recovery is not enabled; no HDC command dispatched')
            return device.screen(action['mode'])
        if operation in {'layout', 'wait', 'tap_text', 'input_text', 'tap', 'input', 'swipe', 'key', 'navigate', 'screenshot'}:
            if not self.target_bundle:
                raise ValueError('Start the task application to bind UI observations first')
        if operation == 'navigate':
            if not self.allow_system_navigation:
                raise ValueError('System navigation is not enabled; no key dispatched')
            if action['key'] not in {'Back', 'Home'}:
                raise ValueError('navigate supports only Back or Home, not confirmation keys')
            visible = True
            try:
                device.layout()
            except TargetNotVisible:
                visible = False
            # One explicit model action, not a recovery loop. Other capture/HDC
            # failures still propagate. Do not expose or click a foreign window.
            result = self._ui_input(device, {'operation': 'key', 'key': action['key']})
            return {**result, 'key': action['key'], 'target_visible_before': visible,
                    'target_state_after': 'NOT_CHECKED',
                    'note': 'Navigation dispatched once; observe the target before claiming recovery'}
        if operation == 'install':
            artifact = confined(self.root, action['artifact'])
            if artifact not in self.built or not artifact.is_file() or not artifact.stat().st_size:
                raise ValueError('Build this HAP after the last file edit before installing')
            packages = self.package_sets.get(artifact, [artifact])
            from .environment_check import package_libraries, normalize_arch
            libraries = [(p, package_libraries(p)) for p in packages]
            if any(rows for _, rows in libraries):
                arch = normalize_arch(device.command('shell', 'uname', '-m'))
                if arch not in {'arm64-v8a', 'x86_64', 'armeabi-v7a', 'x86'}:
                    raise Blocked('Cannot identify device architecture before package installation')
                for path, rows in libraries:
                    bad = [r for r in rows if arch not in r['architectures']]
                    if bad:
                        raise Blocked(f'Packaged native libraries incompatible with {arch}: {path.name}: ' +
                                      ', '.join(r['library'] for r in bad), failure_kind='package_abi')
            installed = []
            for path in packages:
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError('Built package missing; build again: ' + path.name)
                response = device.command('install', str(path))
                installed.append({'artifact': path.relative_to(self.root).as_posix(), 'output': response})
                if 'install bundle successfully' not in response:
                    raise Blocked(f'Installation not confirmed for {path.name}: {response}; '
                                  f'{len(installed)-1} previous packages installed, no replay')
            # Same single settle window as the trusted adapter: PACKAGE_CHANGED can
            # otherwise close an app started immediately after installation.
            time.sleep(5)
            return {'status': 'EXECUTED', 'output': response, 'artifact': action['artifact'],
                    'installed_packages': installed,
                    'install_settle_seconds': 5,
                    'build_record': 'Successful shared build; exec side effects are not tracked'}
        if operation in {'start', 'stop', 'package', 'pid', 'faults', 'fault'}:
            bundle = identifier(action['bundle'])
            if self.target_bundle and bundle != self.target_bundle:
                raise ValueError('This task is bound to a different application')
            if operation == 'start':
                response = device.command('shell', 'aa', 'start', '-m', identifier(action['module']),
                                          '-a', identifier(action['ability']), '-b', bundle)
                if 'start ability successfully' not in response:
                    # aa can report the same ordinary lock observed by layout.
                    # Return that state to the Agent; never unlock or relaunch here.
                    if re.search(r'Error Code\s*:\s*10106102\b', response):
                        raise ScreenLocked()
                    raise Blocked('App launch not confirmed: ' + response)
                self.target_bundle = bundle
                save_json(self.output / 'target-application.json', {'bundle': bundle})
                return {'status': 'EXECUTED', 'output': response}
            if operation == 'stop':
                return {'status': 'EXECUTED', 'output': device.command('shell', 'aa', 'force-stop', bundle)}
            if operation == 'package':
                return {'status': 'OBSERVED', 'package': device.package(bundle)}
            if operation == 'pid':
                return {'status': 'OBSERVED', 'pid': device.pid(bundle)}
            names = sorted(device.faults(bundle))
            if operation == 'faults':
                return {'status': 'OBSERVED', 'files': names}
            name = action['name']
            if name not in names:
                raise ValueError('Select a fault name returned for this bundle')
            local = device.output / name
            device.command('file', 'recv', '/data/log/faultlog/faultlogger/' + name, str(local))
            return {'status': 'OBSERVED', 'name': name, 'content': local.read_text(encoding='utf-8', errors='replace')}
        if operation == 'layout':
            tree = device.layout()
            readable = device.output.parent / ('layout-readable.json' if device.counter == 1 else
                                               f'layout-{device.counter:04d}-readable.json')
            save_json(readable, tree)  # Only verified target windows are model-readable.
            return {**layout_page(tree, action.get('offset', 0), action.get('limit', 40), action.get('selector')),
                    'layout': readable.relative_to(self.output).as_posix(),
                    'raw_layout': (device.output / f'{device.counter:04d}-layout.json').relative_to(self.output).as_posix(),
                    'fresh_capture': True}
        if operation == 'screenshot':
            device.layout()  # Confirm target window before a local screenshot.
            device.screenshot('screen')
            return {'status': 'OBSERVED', 'image': (device.output / 'screen.png').relative_to(self.output).as_posix(),
                    'note': 'Saved locally; the text model has not seen these pixels'}
        if operation == 'wait':
            return device.wait_for(action['selector'], action.get('timeout_seconds', 5))
        if operation in {'tap_text', 'input_text'}:
            node = device.target(action['selector'])
            x, y = center(node)
            result = self._ui_input(device, {**action, 'operation': 'tap' if operation == 'tap_text' else 'input',
                                                'x': x, 'y': y})
            return {**result, 'target': attributes(node)}
        if operation in {'tap', 'input', 'swipe', 'key'}:
            device.layout()
            return self._ui_input(device, action)
        raise ValueError('Unknown HarmonyOS operation')

    def _ui_input(self, device, action):
        operation = action['operation']
        coordinates = {'tap': ('x', 'y'), 'input': ('x', 'y'),
                       'swipe': ('x1', 'y1', 'x2', 'y2', 'duration_ms'), 'key': ()}[operation]
        values = []
        for key in coordinates:
            value = action[key]
            if type(value) is not int or value < 0:
                raise ValueError(f'{key} must be a nonnegative integer')
            values.append(str(value))
        if operation == 'input':
            if not isinstance(action['text'], str):
                raise ValueError('input text must be a string')
            values.append(action['text'])
        if operation == 'key':
            if action['key'] not in {'Back', 'Home', 'Enter'}:
                raise ValueError('key must be Back, Home or Enter')
            values = [action['key']]
        verb = {'tap': 'click', 'input': 'inputText', 'swipe': 'swipe', 'key': 'keyEvent'}[operation]
        # hdc shell forwards to the device shell; preserve text as a single argument.
        response = device.command('shell', shlex.join(['uitest', 'uiInput', verb, *values]))
        return {'status': 'EXECUTED', 'output': response}
