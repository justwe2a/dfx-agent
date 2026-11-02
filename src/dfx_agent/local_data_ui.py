"""Independent Notes/Accounting UI readers, based on the original ArkUI trees.

No repair logic, database access, expected-value substitution or hidden retries.
Real layout calibration is still required. Complete small lists only: a clipped
or independently scrollable list is not declared complete from its visible rows.
"""
from collections import Counter
from datetime import date
import re

from .harmony_device import attributes, nodes, validate_layout
from .harmony_runtime import Blocked, CheckFailed
from .local_data_checks import check_notes, check_statistics, money


class OutsideViewport(Blocked):
    """The statistics reader may explicitly scroll to bring this section in view."""


def visible(tree):
    if attributes(tree).get('visible') in (False, 'false'):
        return
    yield tree
    for child in tree.get('children', []):
        yield from visible(child)


def one(items, description):
    if len(items) != 1:
        raise Blocked(f'{description}: expected one control, observed {len(items)}')
    return items[0]


def kind(node):
    return attributes(node).get('type')


def rect(node):
    values = tuple(map(int, re.findall(r'-?\d+', str(attributes(node).get('bounds', '')))))
    if len(values) != 4 or values[2] <= values[0] or values[3] <= values[1]:
        raise Blocked('UI bounds unavailable')
    return values


def within(node, viewport):
    a, b, c, d = rect(node)
    x, y, z, w = viewport
    if a < x or b < y or c > z or d > w:
        raise OutsideViewport('Required content is clipped by its viewport')


def viewport(tree, target, *, include_target=True):
    """Intersect the window and every scroll viewport on the path to target."""
    def visit(node, clip=None):
        if attributes(node).get('visible') in (False, 'false'):
            return None
        if node is target and not include_target:
            if clip is None:
                raise Blocked('Target ancestor window bounds unavailable')
            return clip
        if attributes(node).get('bundleName') or kind(node) in {'Scroll', 'List', 'WaterFlow'}:
            box = rect(node)
            clip = box if clip is None else (max(clip[0], box[0]), max(clip[1], box[1]),
                                             min(clip[2], box[2]), min(clip[3], box[3]))
        if node is target:
            if clip is None:
                raise Blocked('Target window bounds unavailable')
            return clip
        for child in node.get('children', []):
            result = visit(child, clip)
            if result is not None:
                return result
        return None
    return visit(tree)


def text(node):
    """Use a Text value, or its explicit Span children; never count both."""
    value = attributes(node).get('text')
    spans = [n for n in visible(node) if n is not node and kind(n) == 'Span']
    if spans:
        if any(not isinstance(attributes(n).get('text'), str) for n in spans):
            raise Blocked('Span text unavailable')
        joined = ''.join(attributes(n)['text'] for n in spans)
        if value not in (None, '', joined):
            raise Blocked('Parent text and spans disagree')
        return joined
    if not isinstance(value, str):
        raise Blocked('Text value unavailable')
    return value


def text_nodes(tree):
    return [n for n in visible(tree) if kind(n) == 'Text']


def texts(tree):
    return [text(n) for n in text_nodes(tree)]


def minimal(items):
    # A unique innermost semantic container, not an arbitrary shortest match.
    return [n for n in items if not any(other is not n and any(c is other for c in visible(n))
                                      for other in items)]


def ready(tree):
    validate_layout(tree)
    if not any(attributes(n).get('bundleName') for n in visible(tree)):
        raise Blocked('Identified target window required')


def read_new_ledger_form(tree, name):
    """Read the same unsaved sheet; missing controls never imply keyboard state."""
    ready(tree)
    sheet = one([n for n in visible(tree) if kind(n) == 'SheetPage'], 'New ledger sheet')
    field = one([n for n in visible(sheet) if kind(n) == 'TextInput'], 'Ledger name input')
    if attributes(field).get('text') != name:
        raise Blocked('New ledger input changed or was not observed')
    within(field, viewport(tree, field))
    day = one([n for n in text_nodes(sheet) if text(n) == '01日'], 'Ledger start day 01')
    within(day, viewport(tree, day))
    result = {'missing': []}
    for label, control_type, value in [('switch', 'Toggle', None), ('save', 'Button', '保存')]:
        matches = [n for n in visible(sheet) if kind(n) == control_type
                   and (value is None or attributes(n).get('text') == value)]
        if len(matches) > 1:
            raise Blocked(f'Ambiguous ledger form {label}')
        control = matches[0] if matches else None
        if control is not None:
            if attributes(control).get('enabled') in (False, 'false'):
                raise Blocked(f'Ledger form {label} is disabled')
            try:
                within(control, viewport(tree, control))
            except OutsideViewport:
                control = None
        result[label] = control
        if control is None:
            result['missing'].append(label)
    if result['switch'] is not None:
        checked = attributes(result['switch']).get('checked')
        if checked not in (True, False, 'true', 'false'):
            raise Blocked('Default switch state unavailable')
        result['default'] = checked in (True, 'true')
    return result


def complete_list(tree, container, item_type='ListItem'):
    if attributes(container).get('scrollable') not in (False, 'false'):
        raise Blocked('List completeness unverified: independently scrollable or unknown')
    clip = viewport(tree, container)
    within(container, clip)
    items = container.get('children', [])
    if any(kind(n) != item_type for n in items):
        raise Blocked('Unexpected list structure; calibrate the UI adapter')
    for item in items:
        if attributes(item).get('visible') in (False, 'false'):
            raise OutsideViewport('List contains hidden rows')
        within(item, clip)
        for n in text_nodes(item):
            within(n, clip)
    return items


def collection_in_view(tree, container, *, footer=None):
    """Reject bounds clamped to an ancestor scroll edge, not just outside it.

    These small contracts require one complete frame; no partial-row merging.
    A source-defined final footer can prove the lower end of the bill list.
    """
    clip = viewport(tree, container, include_target=False)
    within(container, clip)
    top, bottom = rect(container)[1], rect(container)[3]
    if top <= clip[1]:
        raise OutsideViewport('Collection beginning may be clipped by an ancestor viewport')
    if footer is None:
        if bottom >= clip[3]:
            raise OutsideViewport('Collection end may be clipped by an ancestor viewport')
    else:
        within(footer, clip)
        if rect(footer)[1] <= clip[1] or rect(footer)[3] >= clip[3]:
            raise OutsideViewport('Collection footer touches an ancestor viewport edge')


def read_note_editor(tree, *, prefix):
    ready(tree)
    editor = one([n for n in visible(tree) if kind(n) == 'RichEditor'
                  and attributes(n).get('id') == 'RICH_ID'], 'Note body')
    title = one([n for n in visible(tree) if kind(n) == 'TextInput'], 'Note title')
    name = attributes(title).get('text')
    if not prefix or not isinstance(name, str) or not name.startswith(prefix):
        raise Blocked('Editor is not a designated synthetic note')
    for n in (title, editor):
        within(n, viewport(tree, n))
    # RichEditor accessibility differs by SDK. First version uses its own text
    # attribute only; no preview, child-guessing, clipboard or source fallback.
    body = attributes(editor).get('text')
    if not isinstance(body, str) or not body or body == '开始书写你的笔记':
        raise Blocked('RichEditor full text unavailable; device calibration required')
    if attributes(editor).get('scrollable') not in (False, 'false') or '…' in body:
        raise Blocked('RichEditor content completeness unverified')
    return {'title': name, 'body': body, 'body_source': 'RichEditor[RICH_ID].text'}


def read_note_index(tree, *, prefix):
    ready(tree)
    if not isinstance(prefix, str) or not prefix:
        raise ValueError('Synthetic note prefix required')
    candidates = [n for n in visible(tree) if kind(n) in {'List', 'WaterFlow'}
                  and any(t.startswith(prefix) for t in texts(n))]
    container = one(minimal(candidates), 'Synthetic note list')
    items = complete_list(tree, container, 'FlowItem' if kind(container) == 'WaterFlow' else 'ListItem')
    titles = []
    for item in items:
        title_nodes = [n for n in nodes(item) if kind(n) == 'Text']
        if not title_nodes or not any(n is title_nodes[0] for n in visible(item)):
            raise Blocked('Note card title unavailable')
        title = text(title_nodes[0])
        if title.startswith(prefix):
            if '…' in title:
                raise Blocked('Note card title truncated')
            titles.append(title)
    return {'titles': titles, 'complete_list': True, 'body_read': False}


def read_ledger(tree):
    ready(tree)
    if '账本详情' not in texts(tree):
        raise Blocked('Ledger details page absent')
    cards = [n for n in visible(tree) if kind(n) == 'Row'
             and any(re.fullmatch(r'共\d+笔账单', t) for t in texts(n)) and '结余' in texts(n)]
    card = one(minimal(cards), 'Ledger identity card')
    values = texts(card)
    if (len(values) != 6 or not re.fullmatch(r'共\d+笔账单', values[1])
            or not values[2].startswith('支 ') or not values[3].startswith('/收 ') or values[4] != '结余'):
        raise Blocked('Ledger card structure changed')
    days = [n for n in visible(tree) if kind(n) == 'Row' and '每月开始日' in texts(n)]
    day_row = one(minimal(days), 'Settlement day')
    day = one([t for t in texts(day_row) if re.fullmatch(r'\d{1,2}日', t)], 'Settlement day value')
    switches = [n for n in visible(tree) if kind(n) == 'Row' and '设置为默认账本' in texts(n)]
    switch_row = one(minimal(switches), 'Default ledger setting')
    switch = one([n for n in visible(switch_row) if kind(n) in {'Toggle', 'Switch'}], 'Default ledger switch')
    checked = attributes(switch).get('checked')
    if checked not in (True, False, 'true', 'false'):
        raise Blocked('Default ledger switch state unavailable')
    for node in [card, day_row, switch_row]:
        within(node, viewport(tree, node))
    return {'name': values[0], 'count': int(re.fullmatch(r'共(\d+)笔账单', values[1])[1]),
            'expense': str(money(values[2][2:])), 'income': str(money(values[3][3:])),
            'balance': str(money(values[5])), 'settlement_day': int(day[:-1]),
            'default': checked in (True, 'true')}


def read_ledger_page(tree):
    """Visible complete cards, with geometry; one page is never a full inventory.

    AccountBookPage uses eager ForEach groups and always-enabled spring edges.
    Group identity comes from its direct header, not a descendant ledger name.
    """
    ready(tree)
    container = one([n for n in visible(tree) if kind(n) == 'List'], 'Ledger list')
    clip = viewport(tree, container)
    groups = container.get('children', [])
    rows, positions, headers = [], [], []
    for group in groups:
        if kind(group) != 'ListItemGroup':
            raise Blocked('Ledger list structure changed')
        children = group.get('children', [])
        if not children or kind(children[0]) != 'Text':
            raise Blocked('Ledger group header unavailable')
        label = text(children[0])
        if label not in ('默认账本', '其它账本'):
            raise Blocked('Unknown ledger group')
        headers.append((label, rect(group), rect(children[0])))
        for item in children[1:]:
            if kind(item) != 'ListItem':
                raise Blocked('Ledger item structure changed')
            try:
                within(item, clip)
                if attributes(group).get('visible') in (False, 'false') or attributes(item).get('visible') in (False, 'false'):
                    raise Blocked('Ledger card hidden inside viewport')
                for n in text_nodes(item):
                    within(n, clip)
            except OutsideViewport:
                continue  # Partial cards must be observed on another page.
            values = texts(item)
            if (len(values) != 6 or not re.fullmatch(r'共\d+笔账单', values[1]) or
                    not values[2].startswith('支 ') or not values[3].startswith('/收 ') or values[4] != '结余'):
                raise Blocked('Ledger card structure changed')
            rows.append({'name': values[0], 'group': label,
                         'count': int(re.fullmatch(r'共(\d+)笔账单', values[1])[1]),
                         'expense': str(money(values[2][2:])), 'income': str(money(values[3][3:])),
                         'balance': str(money(values[5]))})
            positions.append(rect(item))
    if not rows:
        raise Blocked('No complete ledger cards observable')
    if len({r['name'] for r in rows}) != len(rows):
        raise Blocked('Duplicate ledger names cannot be navigated unambiguously')
    return {'rows': rows, 'positions': positions, 'headers': headers, 'viewport': clip,
            'top_visible': bool(headers and headers[0][0] == '默认账本' and
                                headers[0][2][1] >= clip[1]),
            'bottom_visible': bool(headers and headers[-1][1][3] <= clip[3])}


def read_month(tree):
    node = one([n for n in text_nodes(tree) if re.fullmatch(r'\d{4}年\d{2}月', text(n))], 'Displayed month')
    within(node, viewport(tree, node))
    year, month = map(int, re.findall(r'\d+', text(node)))
    try:
        return date(year, month, 1).strftime('%Y-%m')
    except ValueError as error:
        raise Blocked('Invalid displayed month') from error


def check_ledger_preservation(before, after, *, added_name):
    """Compare UI inventories after restoring default; order is not a contract."""
    if not before.get('complete_list') or not after.get('complete_list'):
        raise Blocked('Complete ledger inventories required')
    original, current = before['rows'], after['rows']
    if any(row['name'] == added_name for row in original):
        raise Blocked('New ledger existed in the original inventory')
    if len([row for row in current if row['name'] == added_name]) != 1:
        raise CheckFailed('SYNTHETIC_LEDGER_COUNT_CHANGED')
    remaining = [row for row in current if row['name'] != added_name]
    if sorted(original, key=lambda r: r['name']) != sorted(remaining, key=lambda r: r['name']):
        raise CheckFailed('EXISTING_LEDGER_SUMMARIES_CHANGED')
    if before['previous_default'] != after['previous_default']:
        raise CheckFailed('PREVIOUS_DEFAULT_NOT_RESTORED')


def check_existing_inventory(before, after, *, default):
    """Existing data: only the explicitly selected default role may change."""
    if not before.get('complete_list') or not after.get('complete_list'):
        raise Blocked('Complete existing-ledger inventories required')
    def values(inventory):
        rows = inventory['rows']
        if len({r['name'] for r in rows}) != len(rows):
            raise Blocked('Duplicate existing ledger names')
        return sorted([{k: v for k, v in r.items() if k != 'group'} for r in rows], key=lambda r: r['name'])
    if values(before) != values(after):
        raise CheckFailed('EXISTING_LEDGER_SUMMARIES_CHANGED')
    if after['previous_default'] != default or any(
            r['group'] != ('默认账本' if r['name'] == default else '其它账本') for r in after['rows']):
        raise CheckFailed('EXISTING_LEDGER_DEFAULT_MISMATCH')


def read_summary(tree):
    values = {}
    for label, field in [('总支出', 'expense'), ('总收入', 'income')]:
        column = one(minimal([n for n in visible(tree) if kind(n) == 'Column'
                              and label in texts(n)]), label)
        pair = texts(column)
        if len(pair) != 2 or pair[0] != label:
            raise Blocked('Summary label/value pairing unavailable')
        within(column, viewport(tree, column))
        values[field] = str(money(pair[1]))
    return values


def statistics_top(tree):
    ready(tree)
    reports = [n for n in visible(tree) if kind(n) == 'Select'
               and (attributes(n).get('text') == '报表' or '报表' in texts(n))]
    if '统计' not in texts(tree) or len(reports) != 1:
        raise Blocked('Statistics report page absent')
    return {'month': read_month(tree), **read_summary(tree)}


def display_date(value, month, pattern):
    found = re.fullmatch(pattern, value)
    if not found:
        raise Blocked('Displayed daily date unreadable')
    try:
        result = date(int(month[:4]), int(found[1]), int(found[2])).isoformat()
    except ValueError as error:
        raise Blocked('Invalid daily date') from error
    if result[:7] != month:
        raise CheckFailed('DAILY_ROW_OUTSIDE_DISPLAYED_MONTH')
    return result


def read_daily_report(tree):
    ready(tree)
    month = read_month(tree)
    sections = [n for n in visible(tree) if kind(n) == 'Column' and '报表统计' in texts(n)
                and any(kind(child) == 'List' for child in visible(n))]
    if not sections:
        raise OutsideViewport('Daily report section is not visible')
    section = one(minimal(sections), 'Daily report section')
    table = one([n for n in visible(section) if kind(n) == 'List'], 'Daily report table')
    collection_in_view(tree, table)
    rows = complete_list(tree, table)
    values = [texts(row) for row in rows]
    if values == [['暂无内容']]:
        return {'month': month, 'daily': [], 'empty_marker': '暂无内容', 'complete_list': True}
    if not values or sorted(values[0]) != sorted(['日期', '收入', '支出']):
        raise Blocked('Daily table headers absent or changed')
    columns = {label: index for index, label in enumerate(values[0])}
    result = []
    for row in values[1:]:
        if len(row) != 3:
            raise Blocked('Incomplete daily table row')
        result.append({'date': display_date(row[columns['日期']], month, r'(\d{2})-(\d{2})'),
                       'income': str(money(row[columns['收入']])), 'expense': str(money(row[columns['支出']]))})
    if not result:
        raise Blocked('Header-only table is not proof of an empty month')
    return {'month': month, 'daily': result, 'complete_list': True}


def read_transactions(tree):
    ready(tree)
    if '明细' not in texts(tree):
        raise Blocked('Bill details page absent')
    month = read_month(tree)
    groups = [n for n in visible(tree) if kind(n) == 'List' and any(
        re.fullmatch(r'\d{2}月\d{2}日', t) for t in texts(n))]
    if not groups:
        empty = one([n for n in visible(tree) if kind(n) == 'List'], 'Empty bill list')
        collection_in_view(tree, empty)
        if complete_list(tree, empty):
            raise Blocked('Bill date groups unavailable')
        if any(money(value) != 0 for value in read_summary(tree).values()):
            raise CheckFailed('EMPTY_BILL_LIST_WITH_NONZERO_TOTAL')
        return {'month': month, 'transactions': [], 'complete_list': True,
                'empty_evidence': 'visible_non_scrollable_empty_list_and_zero_totals'}
    outer = one(minimal(groups), 'Daily bill groups')
    footers = [n for n in text_nodes(outer) if text(n) == '下拉获取最新账单~']
    if not footers:
        raise OutsideViewport('Bill list end marker is not visible')
    collection_in_view(tree, outer, footer=one(footers, 'Bill list end marker'))
    result = []
    for group in complete_list(tree, outer):
        if texts(group) == ['下拉获取最新账单~']:
            continue  # Exact source-declared footer, never an unknown data row.
        when = one([t for t in texts(group) if re.fullmatch(r'\d{2}月\d{2}日', t)], 'Bill day')
        day = display_date(when, month, r'(\d{2})月(\d{2})日')
        bills = one([n for n in visible(group) if kind(n) == 'List'], 'Bills for day')
        for bill in complete_list(tree, bills):
            pairs = [n for n in visible(bill) if kind(n) == 'Row'
                     and len(texts(n)) == 2 and re.fullmatch(r'[+-]\d+\.\d{2}', texts(n)[1])]
            pair = one(minimal(pairs), 'Category and signed amount')
            category, amount = texts(pair)
            if not category or '…' in category:
                raise Blocked('Full category label unavailable')
            result.append({'date': day, 'category': category,
                           'direction': 'income' if amount[0] == '+' else 'expense',
                           'amount': str(money(amount[1:]))})
    return {'month': month, 'transactions': result, 'complete_list': True}


def require_isolated_ledger(value, *, name, empty=False):
    if value['name'] != name or not value['default'] or value['settlement_day'] != 1:
        raise CheckFailed('SYNTHETIC_DEFAULT_LEDGER_NOT_CONFIRMED')
    if empty and (value['count'] != 0 or any(money(value[k]) != 0 for k in ('expense', 'income', 'balance'))):
        raise Blocked('Synthetic ledger is not empty; do not clear or reuse it')


def check_note_capture(index, editors, *, samples, edited):
    """Index proves multiplicity; editor captures supply the full body values."""
    captures = [index, *editors]
    if (index['kind'] != 'note-index' or any(r['kind'] != 'note-editor' for r in editors)
            or any(r['bundle'] != 'bjyy.1.xxxxxx' for r in captures)
            or len({r['evidence_directory'] for r in captures}) != 1
            or any(a['sequence'] >= b['sequence'] for a, b in zip(captures, captures[1:]))):
        raise Blocked('Note index and editors are not one ordered capture sequence')
    if Counter(index['values']['titles']) != Counter(r['title'] for r in samples):
        raise CheckFailed('NOTE_INDEX_CONTENT_OR_COUNT_MISMATCH')
    check_notes([r['values'] for r in editors], samples, edited=edited)


def bind_statistics(before, observed, after, *, spec, empty_month=False):
    """Bind observed stats to bracketing ledger UI, not a caller-supplied label."""
    if (not before['sequence'] < observed['sequence'] < after['sequence'] or
            len({r['evidence_directory'] for r in (before, observed, after)}) != 1 or
            any(r['bundle'] != spec['bundle'] for r in (before, observed, after)) or
            [r['kind'] for r in (before, observed, after)] != ['ledger', 'statistics', 'ledger']):
        raise Blocked('Ledger/statistics observations are not one ordered capture sequence')
    for receipt in (before, after):
        require_isolated_ledger(receipt['values'], name=spec['data_policy']['ledger_name'])
    if before['values'] != after['values']:
        raise CheckFailed('LEDGER_CHANGED_DURING_STATISTICS')
    ledger = before['values']
    total_expense, total_income = (money(spec['expected'][k]) for k in ('expense', 'income'))
    if (ledger['count'] != len(spec['transactions']) or money(ledger['expense']) != total_expense
            or money(ledger['income']) != total_income or money(ledger['balance']) != total_income-total_expense):
        raise CheckFailed('LEDGER_CONTAINS_UNEXPECTED_DATA')
    expected = spec['expected']['empty_month'] if empty_month else spec['expected']
    month = spec['data_policy']['empty_month' if empty_month else 'month']
    value = {**observed['values'], 'ledger': ledger['name']}
    check_statistics(value, ledger=spec['data_policy']['ledger_name'], month=month, expected=expected)
    return value
