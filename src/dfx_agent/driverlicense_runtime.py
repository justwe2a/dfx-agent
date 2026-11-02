"""TC014 literal-search UI acceptance. Oracles remain local; no source edits."""
import time

from .bookread_runtime import visible, bounds, texts, consent, swipe, merge_rows
from .exam_runtime import assert_sequence
from .fitness_runtime import control
from .harmony_runtime import attributes, center, Blocked, CheckFailed, evaluate_replay
from .workspace import save_json

HOME_SEARCH = '请输入关键字搜索'
EMPTY = '暂时没有搜索到您要找的内容呢'


def check_dialog(tree):
    consent(tree)
    if any(attributes(n).get('text') in {'允许', '仅在使用中允许', '使用时允许'} for n in visible(tree)):
        raise Blocked('Application permission requires user action')


def wait_search(device):
    for _ in range(12):
        tree = device.layout('driver-search-page')
        check_dialog(tree)
        if texts(tree, '搜索') and any(attributes(n).get('type') == 'SearchField' for n in visible(tree)):
            return tree
        time.sleep(.5)
    raise Blocked('Search page unavailable')


def open_search(device, config):
    device.command('shell', 'aa', 'force-stop', config['bundle'])
    response = device.command('shell', 'aa', 'start', '-m', config['module'], '-a', config['ability'], '-b', config['bundle'])
    if 'start ability successfully' not in response:
        raise Blocked('Application launch was not confirmed')
    configured = False
    for _ in range(30):
        tree = device.layout('driver-startup')
        check_dialog(tree)
        if texts(tree, HOME_SEARCH):
            if not texts(tree, '北京') or not texts(tree, '科目一'):
                raise Blocked('Approved city/subject not observed on homepage')
            device.click(tree, HOME_SEARCH)
            return wait_search(device)
        if texts(tree, '小汽车') and texts(tree, '科目一') and not configured:
            if not texts(tree, '北京'):
                raise Blocked('Approved Beijing setup not observed')
            device.click(tree, '小汽车')
            for _ in range(12):
                tree = device.layout('driver-license-selected')
                check_dialog(tree)
                if texts(tree, '科目一'):
                    break
                time.sleep(.5)
            else:
                raise Blocked('Guide subject control unavailable after license selection')
            device.click(tree, '科目一')
            configured = True
            device.action('configure_search_input', city='北京', license='小汽车', subject='科目一')
        if configured:
            tree = device.layout('driver-guide')
            if texts(tree, '完成'):
                device.click(tree, '完成')
            else:
                scrolls = [n for n in visible(tree) if attributes(n).get('type') == 'Scroll']
                if len(scrolls) != 1:
                    raise Blocked('Guide completion control unavailable')
                swipe(device, scrolls[0])
        time.sleep(.5)
    raise Blocked('Application startup/guide did not reach homepage')


def submit_query(device, config, query, keyboard=False, baseline=False):
    tree = wait_search(device)
    field = control(tree, 'SearchField', '__SearchField__')
    if attributes(field).get('text'):
        device.click_node(control(tree, 'Button', '__SearchField__CancelButton__'))
        tree = device.layout('driver-cleared')
        field = control(tree, 'SearchField', '__SearchField__')
        if attributes(field).get('text'):
            raise Blocked('Search input did not clear')
    x, y = center(field)
    device.command('shell', 'uitest', 'uiInput', 'inputText', x, y, query)
    time.sleep(.5)
    tree = device.layout('driver-typed')
    check_dialog(tree)
    if attributes(control(tree, 'SearchField', '__SearchField__')).get('text') != query:
        raise Blocked('Observed search input differs from requested query')
    pid = device.pid(config['bundle'])
    if not pid:
        raise Blocked('No single application PID before trigger')
    before = device.faults(config['bundle'])
    device.screenshot('driver-before-' + str(device.counter))
    if keyboard:
        device.command('shell', 'uitest', 'uiInput', 'keyEvent', 'Enter')
    else:
        device.click(tree, '搜索')
    device.action('submit_query', query=query, keyboard=keyboard, pid=pid)
    time.sleep(config['observation_seconds'])
    tree = device.layout('driver-result')
    evidence = {'pid': pid, 'pid_after': device.pid(config['bundle']), 'query': query,
                'fault_files_before': sorted(before),
                'faults': device.collect_faults(before, config['bundle'], pid, config['fault_signature']),
                'page_reached': bool(texts(tree, EMPTY) or texts(tree, '搜索结果'))}
    save_json(device.output / ('search-' + str(device.counter) + '.json'), evidence)
    device.screenshot('driver-after-' + str(device.counter))
    evaluate_replay(evidence, baseline)
    return evidence


def page_rows(tree, query):
    if attributes(control(tree, 'SearchField', '__SearchField__')).get('text') != query:
        raise Blocked('Result belongs to a different query')
    lists = [n for n in visible(tree) if attributes(n).get('type') == 'List']
    if texts(tree, EMPTY):
        if lists or texts(tree, '搜索结果'):
            raise Blocked('MISSING_OR_AMBIGUOUS_UI')
        return None, []
    if len(lists) != 1 or not texts(tree, '搜索结果'):
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    viewport = lists[0]
    # The result List may expand inside an outer Scroll. Use its visible parent viewport.
    scrolls = [n for n in visible(tree) if attributes(n).get('type') == 'Scroll'
               and any(child is viewport for child in visible(n))]
    if scrolls:
        viewport = min(scrolls, key=lambda n: bounds(n)[3] - bounds(n)[1])
    left, top, right, bottom = bounds(viewport)
    rows = []
    for item in visible(lists[0]):
        if attributes(item).get('type') != 'ListItem':
            continue
        parts = [n for n in visible(item) if attributes(n).get('type') == 'Text']
        if len(parts) != 2:
            raise Blocked('MISSING_OR_AMBIGUOUS_UI')
        title = parts[1]
        x1, y1, x2, y2 = bounds(title)
        if y1 <= top or y2 >= bottom or x1 < left or x2 > right:
            continue
        rows.append((y1, y2, attributes(title).get('text', '')))
    rows.sort(key=lambda row: row[0])
    return viewport, rows


def collect_results(device, sample):
    collected, previous, stationary = [], None, 0
    for _ in range(16):
        tree = device.layout('driver-result-list')
        viewport, rows = page_rows(tree, sample['query'])
        if viewport is None:
            assert_sequence([], sample['expected_titles'])
            return {'query': sample['query'], 'observed': [], 'status': 'PASS', 'empty_state': True}
        if not rows:
            raise Blocked('MISSING_OR_AMBIGUOUS_UI')
        if rows == previous:
            stationary += 1
            if stationary == 2:
                save_json(device.output / ('collected-' + str(device.counter) + '.json'),
                          {'query': sample['query'], 'observed': collected, 'bottom_stable_swipes': stationary})
                assert_sequence(collected, sample['expected_titles'])
                return {'query': sample['query'], 'observed': collected, 'status': 'PASS', 'bottom_stable_swipes': stationary}
        else:
            stationary = 0
            collected = merge_rows(collected, [r[2] for r in rows])
        previous = rows
        swipe(device, viewport)
    raise Blocked('List end was not observed within bounded scrolling')


def return_and_reenter(device):
    # Close an active keyboard first, then leave the search screen.
    for _ in range(3):
        device.command('shell', 'uitest', 'uiInput', 'keyEvent', 'Back')
        time.sleep(.5)
        tree = device.layout('driver-back-home')
        if texts(tree, HOME_SEARCH):
            device.click(tree, HOME_SEARCH)
            return wait_search(device)
    raise CheckFailed('Back navigation did not restore homepage')
