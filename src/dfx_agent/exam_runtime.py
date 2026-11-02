"""Exam1's approved finite-list UI contract; no application edits or model calls."""
from collections import Counter
import time

from .bookread_runtime import visible, bounds, texts, consent
from .harmony_runtime import attributes, Blocked, CheckFailed, evaluate_replay
from .workspace import save_json


def assert_sequence(observed, expected):
    if observed != expected:
        kind = 'LIST_ORDER_MISMATCH' if Counter(observed) == Counter(expected) else 'LIST_CONTENT_MISMATCH'
        raise CheckFailed(kind)
    return observed


def category_rows(tree, breadcrumb):
    """Read the content Column, excluding breadcrumb List rows and unrelated text."""
    headers = [n for n in visible(tree) if attributes(n).get('type') == 'List']
    if len(headers) != 1:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    observed = [attributes(n).get('text', '') for n in visible(headers[0])
                if attributes(n).get('type') == 'Text']
    if observed != breadcrumb:
        raise CheckFailed('TITLE_MISMATCH')
    candidates = []
    for column in visible(tree):
        if attributes(column).get('type') != 'Column':
            continue
        children = [n for n in column.get('children', []) if attributes(n).get('visible') != 'false']
        if not children or any(attributes(n).get('type') != 'Row' for n in children):
            continue
        rows = []
        for row in children:
            parts = [n for n in row.get('children', []) if attributes(n).get('visible') != 'false']
            if [attributes(n).get('type') for n in parts] != ['Text', 'Image']:
                break
            rows.append((row, parts[0]))
        else:
            candidates.append((column, rows))
    if len(candidates) != 1:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    column, rows = candidates[0]
    left, top, right, bottom = bounds(column)
    for row, title in rows:
        x1, y1, x2, y2 = bounds(title)
        if x1 < left or x2 > right or y1 < top or y2 > bottom:
            raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    rows.sort(key=lambda pair: bounds(pair[0])[1])
    return rows


def read_category(tree, breadcrumb, expected):
    rows = category_rows(tree, breadcrumb)
    observed = [attributes(title).get('text', '') for _, title in rows]
    assert_sequence(observed, expected)
    return {'breadcrumb': breadcrumb, 'observed': observed, 'status': 'PASS'}, rows


def home_select(tree):
    if not texts(tree, '练习模式'):
        return None
    selects = [n for n in visible(tree) if attributes(n).get('type') == 'Select']
    if len(selects) != 1:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    return selects[0]


def assert_selection(tree, title):
    select = home_select(tree)
    if select is None:
        raise CheckFailed('TITLE_MISMATCH')
    # The expected title must be inside the homepage Select, not elsewhere on screen.
    values = [attributes(n).get('text') for n in visible(select) if attributes(n).get('text')]
    if title not in values or any(value != title for value in values):
        raise CheckFailed('TITLE_MISMATCH')
    return {'case': 'selection_title', 'observed': title, 'status': 'PASS', 'internal_id_observed': False}


def open_home(device, config):
    device.command('shell', 'aa', 'force-stop', config['bundle'])
    response = device.command('shell', 'aa', 'start', '-m', config['module'],
                              '-a', config['ability'], '-b', config['bundle'])
    if 'start ability successfully' not in response:
        raise Blocked('Application launch was not confirmed')
    for _ in range(25):
        tree = device.layout('exam-startup')
        consent(tree)
        if home_select(tree) is not None:
            return tree
        time.sleep(.5)
    raise Blocked('Practice homepage unavailable')


def enter_category(device, config, baseline=False):
    tree = open_home(device, config)
    device.click_node(home_select(tree))
    for _ in range(12):
        tree = device.layout('exam-bank')
        consent(tree)
        if texts(tree, '题库') and texts(tree, '建造师'):
            break
        time.sleep(.5)
    else:
        raise Blocked('Question bank category entry unavailable')
    pid = device.pid(config['bundle'])
    if not pid:
        raise Blocked('No single application PID before trigger')
    before = device.faults(config['bundle'])
    device.screenshot('before-category')
    device.click(tree, '建造师')
    time.sleep(config['observation_seconds'])
    tree = device.layout('exam-category-result')
    evidence = {'pid': pid, 'pid_after': device.pid(config['bundle']),
                'fault_files_before': sorted(before),
                'faults': device.collect_faults(before, config['bundle'], pid, config['fault_signature']),
                'page_reached': bool(texts(tree, '一级建造师')) and bool(texts(tree, '二级建造师'))}
    save_json(device.output / 'category-replay.json', evidence)
    device.screenshot('after-category')
    evaluate_replay(evidence, baseline)
    if not baseline:
        read_category(tree, config['breadcrumb'], [x['section'] for x in config['tests']])
    return evidence


def open_subjects(device, config, sample):
    tree = device.layout('exam-level-two')
    _, rows = read_category(tree, config['breadcrumb'], [x['section'] for x in config['tests']])
    selected = [row for row, title in rows if attributes(title).get('text') == sample['section']]
    if len(selected) != 1:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    device.click_node(selected[0])
    time.sleep(.6)
    tree = device.layout('exam-subjects')
    result, rows = read_category(tree, config['breadcrumb'] + [sample['section']], sample['expected_titles'])
    device.screenshot('subjects-' + str(device.counter))
    return result, rows


def return_category(device, config):
    device.command('shell', 'uitest', 'uiInput', 'keyEvent', 'Back')
    time.sleep(.6)
    tree = device.layout('exam-back')
    read_category(tree, config['breadcrumb'], [x['section'] for x in config['tests']])


def select_subject(device, config, sample, rows):
    device.click_node(rows[sample['select_index']][0])
    for _ in range(12):
        tree = device.layout('exam-selection')
        if home_select(tree) is not None:
            return assert_selection(tree, sample['expected_titles'][sample['select_index']])
        time.sleep(.5)
    raise CheckFailed('TITLE_MISMATCH')
