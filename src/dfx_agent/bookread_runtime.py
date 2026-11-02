"""BookRead2's approved local list contract. No model or application edits."""
from collections import Counter
import re
import time

from .harmony_runtime import Blocked, CheckFailed, attributes, nodes, text_nodes
from .workspace import save_json


def visible(tree):
    if attributes(tree).get('visible') == 'false':
        return
    yield tree
    for child in tree.get('children', []):
        yield from visible(child)


def bounds(node):
    values = tuple(int(x) for x in re.findall(r'-?\d+', attributes(node).get('bounds', '')))
    if len(values) != 4 or values[2] <= values[0] or values[3] <= values[1]:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    return values


def texts(tree, value):
    return [n for n in visible(tree) if attributes(n).get('type') == 'Text'
            and attributes(n).get('text') == value]


def consent(tree):
    if any(attributes(n).get('text') in {'同意','同意并继续'} for n in visible(tree)):
        raise Blocked('User agreement requires user action')


def open_store(device, config):
    device.command('shell', 'aa', 'force-stop', config['bundle'])
    response = device.command('shell', 'aa', 'start', '-m', config['module'],
                              '-a', config['ability'], '-b', config['bundle'])
    if 'start ability successfully' not in response:
        raise Blocked('Application launch was not confirmed')
    for _ in range(25):
        tree = device.layout('store-startup')
        consent(tree)
        if texts(tree, '推荐') and texts(tree, '书城'):
            return tree
        tabs = texts(tree, '书城')
        if len(tabs) == 1:
            device.click_node(tabs[0])
        time.sleep(.5)
    raise Blocked('Store recommendation channel unavailable')


def swipe(device, viewport):
    x1, y1, x2, y2 = bounds(viewport)
    x = (x1 + x2) // 2
    start, end = y1 + (y2-y1)*3//4, y1 + (y2-y1)//4
    device.command('shell', 'uitest', 'uiInput', 'swipe', x, start, x, end, 600)
    device.action('scroll_list', start_y=start, end_y=end)
    time.sleep(.6)


def more_button(tree, section):
    candidates = [n for n in visible(tree) if attributes(n).get('type') == 'Row'
                  and texts(n, section) and texts(n, '更多')]
    if not candidates:
        return None
    bar = min(candidates, key=lambda n: sum(1 for _ in nodes(n)))
    choices = texts(bar, '更多')
    if len(choices) != 1 or len(texts(bar, section)) != 1:
        raise Blocked('Section more control is ambiguous')
    return choices[0]


def open_section(device, config, section):
    for _ in range(12):
        tree = device.layout('find-section')
        consent(tree)
        if not texts(tree, '推荐') or not texts(tree, '书城'):
            raise Blocked('Store recommendation input state is absent')
        button = more_button(tree, section)
        if button is not None:
            break
        lists = [n for n in visible(tree) if attributes(n).get('type') == 'List'
                 and attributes(n).get('scrollable') == 'true']
        if not lists:
            raise Blocked('Store scroll container is absent')
        # The main vertical store list encloses its horizontal child lists.
        swipe(device, max(lists, key=lambda n: bounds(n)[3]-bounds(n)[1]))
    else:
        raise Blocked('Requested section was not reached')
    pid = device.pid(config['bundle'])
    if not pid:
        raise Blocked('No single application PID before trigger')
    before = device.faults(config['bundle'])
    device.screenshot('before-section-' + str(device.counter))
    device.click_node(button)
    device.action('open_section', section=section, pid=pid)
    time.sleep(config['observation_seconds'])
    tree = device.layout('section-result')
    evidence = {'section': section, 'pid': pid, 'pid_after': device.pid(config['bundle']),
                'fault_files_before': sorted(before),
                'faults': device.collect_faults(before, config['bundle'], pid, config['fault_signature']),
                'page_reached': bool(texts(tree, section)) and not bool(texts(tree, '书城'))}
    save_json(device.output / ('section-' + str(device.counter) + '.json'), evidence)
    device.screenshot('after-section-' + str(device.counter))
    return tree, evidence


def page_rows(tree, section):
    if len(texts(tree, section)) != 1 or texts(tree, '书城'):
        raise CheckFailed('TITLE_MISMATCH')
    lists = [n for n in visible(tree) if attributes(n).get('type') == 'List']
    if len(lists) != 1:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    viewport = lists[0]
    left, top, right, bottom = bounds(viewport)
    result = []
    for item in visible(viewport):
        if attributes(item).get('type') != 'ListItem':
            continue
        x1,y1,x2,y2 = bounds(item)
        # Partial rows are collected after scrolling; never interpret clipped text.
        if y1 < top or y2 > bottom or x1 < left or x2 > right:
            continue
        # The cover's free/paid badge precedes the book title in the UI tree.
        # The title is the direct Text in the heading Row beside its rating Row.
        headings = [n for n in visible(item) if attributes(n).get('type') == 'Row'
                    and any(attributes(c).get('type') == 'Row' for c in n.get('children', []))]
        titles = [c for n in headings for c in n.get('children', [])
                  if attributes(c).get('type') == 'Text' and attributes(c).get('visible') != 'false']
        authors = [n for n in visible(item) if attributes(n).get('type') == 'Text'
                   and attributes(n).get('text', '').startswith('作者：')]
        # dumpLayout clamps partially visible ListItem bounds to the viewport.
        # Missing fields are acceptable only at an edge; collect them after scrolling.
        if (not titles or not authors) and (y1 <= top or y2 >= bottom):
            continue
        if len(titles) != 1 or len(authors) != 1 or not attributes(titles[0]).get('text'):
            raise Blocked('MISSING_OR_AMBIGUOUS_UI')
        if any(bounds(n)[1] <= top or bounds(n)[3] >= bottom for n in (titles[0], authors[0])):
            continue
        result.append((y1, y2, {'name': attributes(titles[0])['text'],
                               'author': attributes(authors[0])['text'].removeprefix('作者：')}))
    result.sort(key=lambda row: row[0])
    return viewport, result


def merge_rows(accumulated, incoming):
    """Join overlapping viewports, preserving duplicate rows instead of set union."""
    if not accumulated:
        return list(incoming)
    overlaps = [n for n in range(1,min(len(accumulated),len(incoming))+1)
                if accumulated[-n:] == incoming[:n]]
    if len(overlaps) != 1:
        raise Blocked('MISSING_OR_AMBIGUOUS_UI')
    return accumulated + incoming[overlaps[0]:]


def assert_books(observed, sample):
    expected = sample['expected_books']
    if observed != expected:
        pairs = lambda rows: Counter((r['name'],r['author']) for r in rows)
        kind = 'LIST_ORDER_MISMATCH' if pairs(observed)==pairs(expected) else 'LIST_CONTENT_MISMATCH'
        raise CheckFailed(kind)
    return {'section':sample['section'],'observed':observed,'status':'PASS'}


def collect_books(device, sample):
    collected, previous, stationary = [], None, 0
    for _ in range(40):
        tree = device.layout('book-list')
        viewport, rows = page_rows(tree, sample['section'])
        current = [(a,b,r) for a,b,r in rows]
        if not rows:
            raise CheckFailed('LIST_CONTENT_MISMATCH')
        if current == previous:
            stationary += 1
            if stationary == 2:
                save_json(device.output/'collected-books.json', {'section':sample['section'],'books':collected,'bottom_stable_swipes':stationary})
                return assert_books(collected,sample)
        else:
            stationary = 0
            incoming = [r for _,_,r in rows]
            collected = merge_rows(collected,incoming)
        previous = current
        swipe(device,viewport)
    raise Blocked('List end was not observed within bounded scrolling')


def return_store(device):
    device.command('shell','uitest','uiInput','keyEvent','Back')
    for _ in range(12):
        tree = device.layout('return-store')
        if texts(tree,'书城') and texts(tree,'推荐'):
            return
        time.sleep(.5)
    raise CheckFailed('Store was not restored after back navigation')
