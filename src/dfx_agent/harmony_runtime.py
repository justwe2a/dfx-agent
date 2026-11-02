"""Local, trusted HarmonyOS UI acceptance helpers. No model or source editing."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import time

from .workspace import save_json
from .harmony_device import (Device as SharedDevice, Blocked, now, nodes, attributes, text_nodes, center)

BINDING = ('run_id', 'attempt_id', 'snapshot_id', 'patch_id', 'artifact_id')
DATE = re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$')


class CheckFailed(RuntimeError):
    """Observed application behavior violates a registered assertion."""


def date_value(text):
    match = DATE.fullmatch(text)
    if not match:
        raise Blocked(f'Unrecognized date label: {text!r}')
    return dt.date(*(int(n) for n in match.groups()))


def displayed_dates(tree):
    result = {}
    for label in ('日期1', '日期2'):
        # Smallest ancestor containing the label and exactly one date.
        candidates = []
        for parent in nodes(tree):
            if not text_nodes(parent, label):
                continue
            values = {attributes(n).get('text', '') for n in nodes(parent)
                      if DATE.fullmatch(attributes(n).get('text', ''))}
            if len(values) == 1:
                candidates.append(next(iter(values)))
        if not candidates or len(set(candidates)) != 1:
            raise Blocked(f'Missing or ambiguous date field: {label}')
        result[label] = date_value(candidates[0])
    return result


def assert_interval(tree, start=None, end=None):
    dates = displayed_dates(tree)
    if start is not None and dates['日期1'] != dt.date.fromisoformat(start):
        raise Blocked('Date 1 UI does not match requested input')
    if end is not None and dates['日期2'] != dt.date.fromisoformat(end):
        raise Blocked('Date 2 UI does not match requested input')
    expected = abs((dates['日期2'] - dates['日期1']).days)
    actual = {attributes(n).get('text', '') for n in nodes(tree)
              if re.fullmatch(r'间隔-?\d+天', attributes(n).get('text', ''))}
    if len(actual) != 1:
        raise CheckFailed('Expected exactly one interval result')
    observed = next(iter(actual))
    if observed != f'间隔{expected}天':
        raise CheckFailed(f'Expected 间隔{expected}天, observed {observed}')
    return {'start': dates['日期1'].isoformat(), 'end': dates['日期2'].isoformat(),
            'expected_days': expected, 'observed': observed, 'status': 'PASS'}


def fault_matches(text, bundle, pid, signature):
    return (bool(re.search(r'^Pid:\s*' + re.escape(pid) + r'\s*$', text, re.M))
            and bool(re.search(r'^(?:Process name|Module name):\s*' + re.escape(bundle) + r'\s*$', text, re.M))
            and all(value in text for value in signature))


def verify_deployment(receipt, binding, device, artifact):
    if receipt.get('status') != 'PASS' or receipt.get('stage') != 'deploy':
        raise Blocked('Deployment has not passed')
    if any(receipt.get(k) != binding[k] for k in BINDING):
        raise Blocked('Deployment receipt belongs to another run/candidate/artifact')
    if receipt.get('details', {}).get('device') != device:
        raise Blocked('Deployment receipt names a different device')
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise Blocked('Build artifact is missing or empty')


class Device(SharedDevice):
    def collect_faults(self, before, bundle, pid, signature):
        found = []
        for name in sorted(self.faults(bundle) - before):
            path = self.output / name
            self.command('file', 'recv', '/data/log/faultlog/faultlogger/' + name, str(path))
            content = path.read_text(encoding='utf-8', errors='replace')
            same_pid = bool(re.search(r'^Pid:\s*' + re.escape(pid) + r'\s*$', content, re.M))
            found.append({'path': str(path), 'pid_matches': same_pid,
                          'target_matches': fault_matches(content, bundle, pid, signature)})
        return found

    def launch_home(self, config):
        bundle = config['bundle']
        self.command('shell', 'aa', 'force-stop', bundle)
        launch = self.command('shell', 'aa', 'start', '-m', config['module'], '-a', config['ability'], '-b', bundle)
        if 'start ability successfully' not in launch:
            raise Blocked('App launch was not confirmed')
        self.action('launch_home')
        for _ in range(25):
            tree = self.layout('startup')
            if text_nodes(tree, '同意'):
                raise Blocked('Privacy consent requires user action')
            if text_nodes(tree, '万年历') and text_nodes(tree, '日期计算'):
                return tree
            time.sleep(.5)
        raise Blocked('Home page/navigation prerequisites unavailable')

    def open_interval(self, config):
        tree = self.launch_home(config)
        pid = self.pid(config['bundle'])
        if not pid:
            raise Blocked('No single app PID before trigger')
        self.screenshot('before-trigger')
        before = self.faults(config['bundle'])
        self.click(tree, '日期计算')
        self.action('trigger_date_interval', pid=pid)
        time.sleep(config.get('observation_seconds', 10))
        faults = self.collect_faults(before, config['bundle'], pid, config['fault_signature'])
        tree = self.layout('after-trigger')
        self.screenshot('after-trigger')
        evidence = {'pid': pid, 'pid_after': self.pid(config['bundle']), 'faults': faults,
                    'fault_files_before': sorted(before),
                    'page_reached': bool(text_nodes(tree, '查询两个日期的间隔天数'))}
        save_json(self.output / 'replay-evidence.json', evidence)
        return tree, evidence

    def choose_date(self, target: dt.date):
        # Selection uses only observed wheel labels and verifies the DatePicker value.
        # Year -> month -> day; month changes may clamp the day, which is set last.
        for index, suffix in enumerate(('年', '月', '日')):
            wanted = (target.year, target.month, target.day)[index]
            previous, stalled = None, 0
            for _ in range(135):
                tree = self.layout('picker')
                pickers = [n for n in nodes(tree) if attributes(n).get('type') == 'DatePicker']
                if len(pickers) != 1:
                    raise Blocked('Expected exactly one date picker')
                picker = pickers[0]
                current_date = date_value(attributes(picker).get('text', ''))
                current = (current_date.year, current_date.month, current_date.day)[index]
                if current == wanted:
                    break
                stalled = stalled + 1 if current == previous else 0
                if stalled >= 3:
                    raise Blocked('Date wheel did not respond to observed adjacent item clicks')
                previous = current
                step = current + (1 if wanted > current else -1)
                candidates = [n for n in text_nodes(picker, f'{step}{suffix}')
                              if attributes(n).get('type') == 'Text']
                if len(candidates) != 1:
                    raise Blocked('Adjacent date wheel item is not uniquely visible')
                self.click_node(candidates[0])
            else:
                raise Blocked('Date picker did not converge within bounded steps')
        tree = self.layout('picker-selected')
        picker = [n for n in nodes(tree) if attributes(n).get('type') == 'DatePicker']
        if len(picker) != 1 or date_value(attributes(picker[0]).get('text', '')) != target:
            raise Blocked('Date picker value differs from target')
        return tree

    def set_date(self, label, iso_date):
        tree = self.layout('date-field')
        self.click(tree, label)
        selected = self.choose_date(dt.date.fromisoformat(iso_date))
        self.click(selected, '确认')
        if displayed_dates(self.layout('date-confirmed'))[label] != dt.date.fromisoformat(iso_date):
            raise Blocked('Confirmed date did not reach the interval form')


def evaluate_replay(evidence, baseline=False):
    target = any(f['target_matches'] for f in evidence['faults'])
    if baseline:
        if not target:
            raise Blocked('Original target fault was not reproduced for this snapshot')
        return 'REPRODUCED'
    if any(f['pid_matches'] for f in evidence['faults']):
        raise CheckFailed('A new application crash was captured for the triggered PID')
    if evidence['pid_after'] != evidence['pid']:
        raise CheckFailed('Application exited or restarted after trigger')
    if not evidence['page_reached']:
        raise Blocked('Target page was not observed; no repair success inferred')
    return 'PASS'
