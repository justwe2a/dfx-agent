"""Independent normal-UI acceptance flows. Never imported into repair prompts.

Source-derived selectors are deliberately observable and must be calibrated on
the real UI. No database access, application patching or expected-value fallback.
"""
from datetime import date
import re
import shlex
import time

from .harmony_device import TargetNotVisible, attributes, center
from .harmony_runtime import Blocked, CheckFailed, evaluate_replay
from .local_data_capture import LocalDataCapture
from .local_data_checks import check_transactions
from .local_data_ui import (visible, kind, one, minimal, texts as data_texts, text, text_nodes,
                            complete_list, read_month, read_ledger, require_isolated_ledger,
                            check_note_capture, bind_statistics, rect, check_ledger_preservation,
                            read_new_ledger_form, check_existing_inventory)
from .workspace import save_json


SYNTHETIC_PREFIXES = ('校准1023甲', '自主1023甲', '验收1022甲')


def texts(tree):
    """Navigation includes native Button labels; data readers retain Text semantics."""
    return data_texts(tree) + [attributes(n)['text'] for n in visible(tree)
                              if kind(n) == 'Button' and attributes(n).get('text')]


class UI:
    def __init__(self, device, spec):
        self.device, self.spec = device, spec
        self.capture = LocalDataCapture(device)

    def wait(self, predicate, label, *, allow_target_absent=True, description=None):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                tree = self.device.layout(label)
            except TargetNotVisible:
                if not allow_target_absent:
                    raise
                time.sleep(.3)
                continue
            values = texts(tree)
            if '同意' in values and any('隐私' in v or '协议' in v for v in values):
                raise Blocked('New privacy agreement requires an explicit user decision')
            if predicate(tree):
                return tree
            time.sleep(.3)
        raise Blocked(f'Page/control not observed within 30 seconds: {description or label}')

    def node(self, *, value=None, type=None, id=None, tree=None):
        def matches(t):
            return [n for n in visible(t) if (type is None or kind(n) == type)
                    and (value is None or attributes(n).get('text') == value)
                    and (id is None or attributes(n).get('id') == id)]
        tree = tree if tree is not None else self.wait(
            lambda t: bool(matches(t)), 'control', description=f'Control {value or id or type}')
        return one(matches(tree), f'Control {value or id or type}')

    def click(self, value):
        self.device.click_node(self.node(value=value))

    def key(self, key):
        self.device.action('independent_key', key=key)
        self.device.command('shell', 'uitest', 'uiInput', 'keyEvent', key)

    def fill(self, node, value):
        x, y = center(node)
        self.device.action('independent_input', type=kind(node), value=value)
        self.device.command('shell', shlex.join(['uitest', 'uiInput', 'inputText', str(x), str(y), value]))

    def launch(self):
        s = self.spec
        self.device.command('shell', 'aa', 'force-stop', s['bundle'])
        result = self.device.command('shell', 'aa', 'start', '-m', s['module'], '-a', s['ability'], '-b', s['bundle'])
        if 'start ability successfully' not in result:
            raise Blocked('Target application launch not confirmed')
        self.device.action('independent_restart', bundle=s['bundle'], clear_data=False)

    def row(self, tree, label):
        return one(minimal([n for n in visible(tree) if kind(n) == 'Row' and label in texts(n)]), label)

    def right_image(self, row):
        images = sorted([n for n in visible(row) if kind(n) == 'Image'], key=lambda n: rect(n)[0])
        if not images:
            raise Blocked('Expected row action image absent; calibrate selector')
        self.device.click_node(images[-1])

    def trigger(self, action, reached, *, baseline):
        d, s = self.device, self.spec
        pid = d.pid(s['bundle'])
        if not pid:
            raise Blocked('No single application PID before trigger')
        before = d.faults(s['bundle'])
        action()
        time.sleep(3)
        faults = d.collect_faults(before, s['bundle'], pid, s['fault_signature'])
        evidence = {'pid': pid, 'pid_after': d.pid(s['bundle']), 'faults': faults,
                    'fault_files_before': sorted(before), 'page_reached': False}
        # An expected original crash is a baseline outcome, not a layout failure.
        if not faults:
            try:
                self.wait(reached, 'trigger-result')
                evidence['page_reached'] = True
            except TargetNotVisible:
                pass
            except Blocked as exc:
                # Preserve observation failure; never turn a transport fault into
                # a reproduced app error. evaluate_replay still requires evidence.
                evidence['observation_error'] = str(exc)
                save_json(d.output / 'trigger-evidence.json', evidence)
                raise
        save_json(d.output / 'trigger-evidence.json', evidence)
        evaluate_replay(evidence, baseline=baseline)
        return evidence


class NotesFlow(UI):
    def home(self, restart=False):
        if restart:
            self.launch()
        return self.wait(lambda t: any(kind(n) == 'Search' for n in visible(t)), 'notes-home')

    def ensure_unused(self):
        tree = self.home()
        # Only inspect home metadata; never open existing notes for isolation.
        containers = [n for n in visible(tree) if kind(n) in {'List', 'WaterFlow'}]
        for container in minimal(containers):
            for row in complete_list(tree, container, 'FlowItem' if kind(container) == 'WaterFlow' else 'ListItem'):
                labels = texts(row)
                if not labels:
                    raise Blocked('Unknown note row during isolation')
                title = labels[0]
                if not title.startswith(SYNTHETIC_PREFIXES):
                    raise Blocked('Existing non-synthetic note; do not open or expose it')
                if title.startswith(self.spec['data_policy']['prefix']):
                    raise Blocked('Synthetic note namespace already exists')
        self.device.action('note_namespace_checked', prefix=self.spec['data_policy']['prefix'])

    def edit(self, sample, *, new=False, edited=False):
        if new:
            tree = self.home()
            self.device.click_node(self.node(type='SymbolGlyph', tree=tree))
        else:
            self.search()
            self.click(sample['title'])
        self.wait(lambda t: any(kind(n) == 'RichEditor' for n in visible(t)), 'note-editor')
        self.fill(self.node(type='TextInput'), sample['title'])
        self.fill(self.node(type='RichEditor', id='RICH_ID'),
                  sample.get('edited_body', sample['body']) if edited else sample['body'])
        observation = self.capture.observe('note-editor', prefix=self.spec['data_policy']['prefix'])
        wanted = sample.get('edited_body', sample['body']) if edited else sample['body']
        if observation['values']['title'] != sample['title'] or observation['values']['body'] != wanted:
            raise Blocked('Typed content not completely observed before save')
        return observation

    def save(self):
        tree = self.device.layout('note-save-toolbar')
        # Source toolbar is one Row with exactly undo/redo/save image children.
        rows = [n for n in visible(tree) if kind(n) == 'Row' and
                len(n.get('children', [])) == 3 and all(kind(c) == 'Image' for c in n['children'])]
        toolbar = one(rows, 'Undo/redo/save toolbar')
        self.device.click_node(toolbar['children'][2])

    def save_and_return(self):
        self.save()
        time.sleep(.3)
        try:
            tree = self.device.layout('after-save')
        except TargetNotVisible:
            return  # trigger() will collect crash evidence; not success.
        if any(kind(n) == 'RichEditor' for n in visible(tree)):
            self.key('Back')

    def search(self):
        self.home()
        prefix = self.spec['data_policy']['prefix']
        self.fill(self.node(type='Search'), prefix)
        self.key('Enter')
        tree = self.wait(lambda t: any(kind(n) == 'Search' and attributes(n).get('text') == prefix
                                     for n in visible(t)), 'note-filter')
        self.node(type='Search', value=prefix, tree=tree)
        return tree

    def read_all(self, *, edited, samples=None):
        samples = samples if samples is not None else self.spec['samples']
        self.search()
        prefix = self.spec['data_policy']['prefix']
        index = self.capture.observe('note-index', prefix=prefix)
        editors = []
        for sample in samples:
            self.click(sample['title'])
            self.wait(lambda t: any(kind(n) == 'RichEditor' for n in visible(t)), 'saved-note')
            editors.append(self.capture.observe('note-editor', prefix=prefix))
            self.key('Back')
            self.search()
        check_note_capture(index, editors, samples=samples, edited=edited)
        self.device.action('notes_verified', edited=edited, titles=index['values']['titles'])

    def run(self, mode, state):
        self.home(restart=True)
        a, b = self.spec['samples']
        if mode in {'baseline', 'replay'}:
            self.ensure_unused()
            self.edit(a, new=True)
            # Fault may occur on save before Back; retain the exact trigger.
            evidence = self.trigger(self.save_and_return, lambda t: any(kind(n) == 'Search' for n in visible(t)),
                                    baseline=mode == 'baseline')
            if mode == 'baseline':
                return {'baseline_status': 'REPRODUCED', 'replay': evidence}
            self.read_all(edited=False, samples=[a])
            state['first_note_saved'] = True
            return {'replay': evidence}
        if not state.get('first_note_saved'):
            raise Blocked('Replay dataset has not been established')
        if mode == 'functional':
            self.edit(b, new=True)
            self.save(); self.key('Back'); self.home()
            self.read_all(edited=False)
            self.edit(a, edited=True)
            self.save(); self.key('Back'); self.home()
            self.read_all(edited=True)
            state['edited_pair_verified'] = True
        else:
            if not state.get('edited_pair_verified'):
                raise Blocked('Functional note stage incomplete')
            for _ in range(2):
                self.read_all(edited=True)
            self.home(restart=True)
            self.read_all(edited=True)
        return {'assertions': ['complete_body_pairing', 'no_duplicate_titles',
                               'edit_isolation' if mode == 'functional' else 'repeat_and_restart_readback']}


class AccountingFlow(UI):
    def home(self, restart=False):
        if restart:
            self.launch()
        return self.wait(lambda t: '明细' in texts(t) and '统计' in texts(t), 'accounting-home')

    def books(self):
        tree = self.home()
        # CommonHeader has the title and its account-book action icon.
        header = one(minimal([n for n in visible(tree) if kind(n) == 'Row' and '明细' in texts(n)
                             and any(kind(c) == 'Image' for c in visible(n))
                             and '统计' not in texts(n)]), 'Home account-book header')
        self.right_image(header)
        return self.wait(lambda t: '新建账本' in texts(t), 'account-books')

    def ledger(self, name):
        self.books()
        self.click(name)
        self.wait(lambda t: '账本详情' in texts(t), 'ledger-details')
        return self.capture.observe('ledger')

    def back_home(self):
        self.key('Back')
        self.wait(lambda t: '新建账本' in texts(t), 'account-books')
        self.key('Back')
        self.home()

    def create_ledger(self, state):
        self.books()
        inventory = self.capture.observe('ledger-index')['values']
        cards = inventory['rows']
        names = [card['name'] for card in cards]
        name = self.spec['data_policy']['ledger_name']
        if name in names:
            raise Blocked('Synthetic ledger namespace already exists')
        for card in cards:
            if not card['name'].startswith(SYNTHETIC_PREFIXES) and (
                    card['count'] != 0 or card['expense'] != '0.00' or card['income'] != '0.00'):
                raise Blocked('Existing non-synthetic ledger contains data')
        state['previous_default'] = inventory['previous_default']
        state['initial_ledger_inventory'] = inventory
        self.click('新建账本')
        self.fill(self.node(type='TextInput'), name)
        self.save_new_ledger(name)
        self.wait(lambda t: '新建账本' in texts(t) and
                  not any(kind(n) == 'SheetPage' for n in visible(t)), 'created-ledger')
        self.click(name)
        self.wait(lambda t: '账本详情' in texts(t), 'created-ledger-details')
        observed = self.capture.observe('ledger')
        require_isolated_ledger(observed['values'], name=name, empty=True)
        self.back_home()

    def save_new_ledger(self, name):
        form = read_new_ledger_form(self.device.layout('new-ledger'), name)
        if form['missing']:
            # One explicit input-completion action, not a retry or a claim that
            # the keyboard was identified. Back can dismiss the sheet: recheck it.
            self.device.action('ledger_input_completion', missing=form['missing'], key='Back')
            self.key('Back')
            tree = self.wait(lambda t: not read_new_ledger_form(t, name)['missing'],
                             'ledger-input-ended', allow_target_absent=False)
            form = read_new_ledger_form(tree, name)
        if not form['default']:
            self.device.click_node(form['switch'])
        # Always read back before Save, including an already-selected switch.
        def ready_to_save(tree):
            observed = read_new_ledger_form(tree, name)
            return not observed['missing'] and observed['default']
        tree = self.wait(ready_to_save, 'ledger-before-save', allow_target_absent=False)
        form = read_new_ledger_form(tree, name)
        self.device.action('ledger_form_verified', ledger_name=name, start_day='01', default=True)
        self.device.click_node(form['save'])

    def select_month(self, month, *, statistics=False):
        tree = self.device.layout('month-current')
        if read_month(tree) == month:
            return
        label = read_month(tree).replace('-', '年')+'月'
        row = self.row(tree, label)
        if statistics:
            self.right_image(row)
        else:
            self.device.click_node(row)
        year, wanted = month.split('-')
        for expected in (year+'年', wanted+'月'):
            tree = self.wait(lambda t: any(kind(n) == 'TextPicker' for n in visible(t)), 'month-picker')
            picker = self.node(type='TextPicker', tree=tree)
            selected = re.fullmatch(r'(\d{4}年)(\d{2}月)', attributes(picker).get('text', ''))
            if selected is None:
                raise Blocked('Month picker selected value unavailable')
            if expected in selected.groups():
                continue
            labels = [n for n in text_nodes(picker) if text(n) == expected]
            if len(labels) != 1:
                raise Blocked('Requested month item not uniquely visible; calibrate month picker')
            self.device.click_node(labels[0])
        picked = self.device.layout('month-picker-selected')
        if attributes(self.node(type='TextPicker', tree=picked)).get('text') != year+'年'+wanted+'月':
            raise Blocked('Month picker did not select requested value')
        self.click('确定')
        if read_month(self.device.layout('month-selected')) != month:
            raise Blocked('Selected month not observed')

    def create_bill(self, sample):
        tree = self.home()
        # FloatingButton source: a square Column containing only the create icon.
        buttons = [n for n in visible(tree) if kind(n) == 'Column' and
                   len(n.get('children', [])) == 1 and kind(n['children'][0]) == 'Image' and
                   abs((rect(n)[2]-rect(n)[0])-(rect(n)[3]-rect(n)[1])) <= 2]
        self.device.click_node(one(buttons, 'Floating create-bill button'))
        self.click('支出' if sample['direction'] == 'expense' else '收入')
        tree = self.device.layout('bill-category')
        grids = [n for n in visible(tree) if kind(n) == 'Grid' and '分类管理' in texts(n)]
        grid = one(grids, 'Category grid')
        categories = [n for n in text_nodes(grid) if text(n) != '分类管理']
        if not categories:
            raise Blocked('No category available')
        # Fixed first visible category; both expense entries must later match.
        self.device.click_node(categories[0])
        self.click('今天')
        self.device.choose_date(date.fromisoformat(sample['date']))
        self.click('确定')
        tree = self.device.layout('bill-date-confirmed')
        if sample['date'][5:].replace('-', '/') not in texts(tree):
            raise Blocked('Bill date not confirmed')
        for digit in sample['amount'].removesuffix('.00'):
            self.device.click_node(self.node(type='Text', value=digit))
        self.click('完成')
        self.home()

    def statistics(self, *, empty=False, direction=None):
        s = self.spec
        before = self.ledger(s['data_policy']['ledger_name'])
        self.back_home()
        self.click('统计')
        self.wait(lambda t: any(kind(n) == 'Select' for n in visible(t)), 'statistics')
        month = s['data_policy']['empty_month' if empty else 'month']
        self.select_month(month, statistics=True)
        if direction:
            label = direction + '占比'
            tree, _, _ = self.capture.statistics_top_visible(control=label)
            self.device.click_node(self.node(value=label, tree=tree))
        observation = self.capture.observe('statistics')
        self.click('明细')
        after = self.ledger(s['data_policy']['ledger_name'])
        bound = bind_statistics(before, observation, after, spec=s, empty_month=empty)
        self.back_home()
        return bound

    def transactions(self):
        self.select_month(self.spec['data_policy']['month'])
        observation = self.capture.observe('transactions')
        check_transactions(observation['values']['transactions'], self.spec['transactions'])
        return observation

    def restore_default(self, state):
        prior = state['previous_default']
        receipt = self.ledger(prior)
        if not receipt['values']['default']:
            self.device.click_node(self.node(type='Toggle'))
        observed = self.capture.observe('ledger')
        if observed['values']['name'] != prior or not observed['values']['default']:
            raise Blocked('Previous default ledger restoration unconfirmed')
        self.back_home()
        self.books()
        inventory = self.capture.observe('ledger-index')['values']
        if self.spec['data_policy'].get('creation') == 'existing_read_only':
            check_existing_inventory(state['initial_ledger_inventory'], inventory, default=prior)
        else:
            check_ledger_preservation(state['initial_ledger_inventory'], inventory,
                                      added_name=self.spec['data_policy']['ledger_name'])
        state['final_ledger_inventory'] = inventory
        state['default_restored'] = True
        state['existing_ledgers_unchanged'] = True
        self.key('Back')
        self.home()

    def prepare_existing(self, state):
        contract = self.spec['existing_data']
        name = self.spec['data_policy']['ledger_name']
        self.books()
        inventory = self.capture.observe('ledger-index')['values']
        check_existing_inventory(contract['inventory'], inventory, default=contract['initial_default'])
        state.update(initial_ledger_inventory=inventory, previous_default=contract['restore_default'],
                     dataset_origin=contract['source_run'])
        self.click(name)
        self.wait(lambda t: '账本详情' in texts(t), 'existing-ledger-details')
        observed = self.capture.observe('ledger')['values']
        expected = one([r for r in inventory['rows'] if r['name'] == name], 'Existing synthetic ledger')
        if observed['name'] != name or observed['settlement_day'] != 1 or any(
                observed[k] != expected[k] for k in ('count', 'income', 'expense', 'balance')):
            raise CheckFailed('EXISTING_LEDGER_DETAIL_MISMATCH')
        if not observed['default']:
            if not contract['activate_target']:
                raise Blocked('Existing calibration ledger is no longer default')
            self.device.click_node(self.node(type='Toggle'))
            observed = self.capture.observe('ledger')['values']
        require_isolated_ledger(observed, name=name)
        state['existing_ledger_verified'] = True
        self.back_home()

    def run_existing(self, mode, state):
        if self.spec['data_policy']['creation'] != 'existing_read_only' or mode not in {
                'replay', 'functional', 'regression'}:
            raise Blocked('Explicit existing-ledger mode required')
        self.home(restart=True)
        if mode == 'replay':
            self.prepare_existing(state)
            self.transactions()
            evidence = self.trigger(lambda: self.click('统计'),
                                    lambda t: any(kind(n) == 'Select' for n in visible(t)), baseline=False)
            return {'replay': evidence, 'dataset_mode': 'existing_read_only',
                    'dataset_origin': state['dataset_origin'], 'new_data_created': False}
        if not state.get('existing_ledger_verified'):
            raise Blocked('Existing ledger has not been observed in this run')
        return self.read_checks(mode, state)

    def run(self, mode, state):
        self.home(restart=True)
        if mode in {'baseline', 'replay'}:
            self.create_ledger(state)
            for sample in self.spec['transactions']:
                self.create_bill(sample)
                state['created_transactions'] = state.get('created_transactions', 0) + 1
            self.transactions()
            evidence = self.trigger(lambda: self.click('统计'),
                                    lambda t: any(kind(n) == 'Select' for n in visible(t)),
                                    baseline=mode == 'baseline')
            if mode == 'baseline':
                # Preserve the fault before any normal UI restoration attempt.
                state['baseline_reproduced'] = True
                self.home(restart=True)
                self.restore_default(state)
                return {'baseline_status': 'REPRODUCED', 'replay': evidence}
            state['three_bills_created'] = True
            return {'replay': evidence}
        if not state.get('three_bills_created'):
            raise Blocked('Replay ledger has not been established')
        return self.read_checks(mode, state)

    def read_checks(self, mode, state):
        self.transactions()
        if mode == 'functional':
            observed = self.statistics()
            state['statistics_verified'] = True
            return {'statistics': observed}
        if not state.get('statistics_verified'):
            raise Blocked('Functional accounting stage incomplete')
        observed = [self.statistics(direction='收入'), self.statistics(direction='支出'),
                    self.statistics(empty=True), self.statistics()]
        self.home(restart=True)
        self.transactions()
        observed.append(self.statistics())
        self.restore_default(state)
        return {'statistics': observed, 'restart_and_default_restore': True}
