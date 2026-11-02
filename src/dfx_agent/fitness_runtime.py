"""FitnessCenter's local UI contract; no application edits or model access."""
from collections import Counter
import re
import time

from .harmony_runtime import Blocked, CheckFailed, attributes, center, nodes, text_nodes
from .workspace import save_json


def visible_nodes(tree):
    if attributes(tree).get('visible') == 'false':
        return
    yield tree
    for child in tree.get('children', []):
        yield from visible_nodes(child)


def control(tree, kind, key=None):
    found = [n for n in visible_nodes(tree) if attributes(n).get('type') == kind
             and (key is None or attributes(n).get('id') == key)]
    if len(found) != 1:
        raise Blocked(f'Expected one {kind} {key or ""}, found {len(found)}')
    return found[0]


def wait_page(device, title):
    for _ in range(15):
        tree = device.layout('wait-page')
        if text_nodes(tree, '同意'):
            raise Blocked('User agreement requires user action')
        if text_nodes(tree, title):
            return tree
        time.sleep(.5)
    raise Blocked(f'Page not observed: {title}')


def hot_city(tree, city):
    sections = [n for n in visible_nodes(tree) if text_nodes(n, '热门城市')
                and text_nodes(n, city + '市')]
    if not sections:
        raise Blocked('Requested city is absent from hot cities')
    section = min(sections, key=lambda n: sum(1 for _ in nodes(n)))
    choices = [n for n in visible_nodes(section) if attributes(n).get('type') == 'Text'
               and attributes(n).get('text') == city + '市']
    if len(choices) != 1:
        raise Blocked('Hot city selection is ambiguous')
    return choices[0]


def open_search(device, config, city):
    """Select the city explicitly, including Beijing; a default label is not state proof."""
    bundle = config['bundle']
    device.command('shell', 'aa', 'force-stop', bundle)
    response = device.command('shell', 'aa', 'start', '-m', config['module'],
                              '-a', config['ability'], '-b', bundle)
    if 'start ability successfully' not in response:
        raise Blocked('Application launch was not confirmed')
    tree = wait_page(device, '首页')
    # BookMain initializes AppStorage.currentCity on first mount. Select the
    # intended city in that page after entering it, not in Home beforehand.
    tabs = [n for n in text_nodes(tree, '预约') if attributes(n).get('type') == 'Text']
    if len(tabs) != 1:
        raise Blocked('Reservation tab is ambiguous')
    device.click_node(tabs[0])
    tree = wait_page(device, '全市门店')
    labels = [n for n in visible_nodes(tree) if attributes(n).get('type') == 'Text'
              and attributes(n).get('text') in ('北京', '北京市', '南京', '南京市')]
    if len(labels) != 1:
        raise Blocked('Current city control is ambiguous')
    device.click_node(labels[0])
    tree = wait_page(device, '城市选择')
    device.click_node(hot_city(tree, city))
    tree = wait_page(device, '全市门店')
    if not text_nodes(tree, city + '市'):
        raise Blocked('Selected city was not reflected on reservation page')
    device.action('select_city', city=city)
    device.click(tree, '全市门店')
    tree = wait_page(device, '选择门店')
    device.click_node(control(tree, 'SearchField', '__SearchField__'))
    return wait_page(device, '门店搜索')


def submit_query(device, config, query):
    tree = wait_page(device, '门店搜索')
    field = control(tree, 'SearchField', '__SearchField__')
    # Clear through the observed cancel control, not a coordinate or assumed key sequence.
    if attributes(field).get('text'):
        cancel = control(tree, 'Button', '__SearchField__CancelButton__')
        device.click_node(cancel)
        tree = device.layout('cleared-input')
        field = control(tree, 'SearchField', '__SearchField__')
        if attributes(field).get('text'):
            raise Blocked('Search input did not clear')
    x, y = center(field)
    device.command('shell', 'uitest', 'uiInput', 'inputText', x, y, query)
    time.sleep(.5)
    tree = device.layout('typed-query')
    field = control(tree, 'SearchField', '__SearchField__')
    if attributes(field).get('text') != query:
        raise Blocked('Observed search input differs from requested query')
    pid = device.pid(config['bundle'])
    if not pid:
        raise Blocked('No single application PID before search')
    before = device.faults(config['bundle'])
    device.screenshot('before-search-' + str(device.counter))
    device.click_node(control(tree, 'Button', '__SearchField__Button__'))
    device.action('submit_query', query=query, pid=pid)
    time.sleep(config['observation_seconds'])
    faults = device.collect_faults(before, config['bundle'], pid, config['fault_signature'])
    tree = device.layout('search-result')
    evidence = {'pid': pid, 'pid_after': device.pid(config['bundle']), 'faults': faults,
                'fault_files_before': sorted(before), 'query': query,
                'page_reached': bool(text_nodes(tree, '门店搜索'))}
    save_json(device.output / ('search-evidence-' + str(device.counter) + '.json'), evidence)
    device.screenshot('after-search-' + str(device.counter))
    return tree, evidence


def observed_stores(tree, query):
    if not text_nodes(tree, '门店搜索'):
        raise Blocked('Search result page is absent')
    if attributes(control(tree, 'SearchField', '__SearchField__')).get('text') != query:
        raise Blocked('Result belongs to a different query')
    lists = [n for n in visible_nodes(tree) if attributes(n).get('type') == 'List']
    if len(lists) > 1:
        raise Blocked('Result list is ambiguous')
    if not lists:
        # The original screen shows search history for no matches; it has no empty-state text.
        if not text_nodes(tree, '搜索历史'):
            raise Blocked('Neither result list nor empty/history state was observed')
        return []
    rows = []
    # ForEach eagerly creates rows. Include off-screen rows from the dump too;
    # visible-only comparison could overlook extra results below the viewport.
    for item in nodes(lists[0]):
        if attributes(item).get('type') != 'ListItem':
            continue
        texts = [attributes(n).get('text', '') for n in nodes(item)
                 if attributes(n).get('type') == 'Text']
        if len(texts) != 3 or not re.fullmatch(r'\d+(?:\.\d+)?\s*(?:km|m)', texts[1]):
            raise Blocked('Incomplete or unrecognized store result row')
        rows.append({'name': texts[0], 'address': texts[2]})
    return rows


def assert_stores(tree, sample):
    observed = observed_stores(tree, sample['query'])
    pairs = lambda rows: Counter((r['name'], r['address']) for r in rows)
    if pairs(observed) != pairs(sample['expected_stores']):
        raise CheckFailed(f'Store results differ for {sample["city"]}/{sample["query"]}: {observed!r}')
    return {'city': sample['city'], 'query': sample['query'], 'observed': observed, 'status': 'PASS'}


def return_to_stores(device):
    tree = wait_page(device, '门店搜索')
    # Header's sole unnamed button above the search field; keyboard Back only clears input.
    field_y = center(control(tree, 'SearchField', '__SearchField__'))[1]
    buttons = [n for n in visible_nodes(tree) if attributes(n).get('type') == 'Button'
               and not attributes(n).get('text') and center(n)[1] < field_y]
    if len(buttons) != 1:
        raise Blocked('Search page back button is ambiguous')
    device.click_node(buttons[0])
    tree = wait_page(device, '选择门店')
    device.action('return_to_stores')
    device.click_node(control(tree, 'SearchField', '__SearchField__'))
    return wait_page(device, '门店搜索')
