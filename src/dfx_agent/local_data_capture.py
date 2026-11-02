"""Device-backed independent observation adapter; never calls the repair model.

Call on the indicated page. Page entry, synthetic data creation and restart are
separate acceptance-driver actions. Statistics may scroll its outer view a fixed
number of times; it never changes accounts, edits data or retries HDC failures.
"""
from .harmony_device import attributes
from .harmony_runtime import Blocked, CheckFailed
from .local_data_ui import (
    OutsideViewport, kind, one, rect, visible, read_note_editor, read_note_index,
    read_ledger, read_ledger_page, statistics_top, read_daily_report, read_month, read_transactions,
    text_nodes, text, viewport, within,
)
from .workspace import save_json


BUNDLES = {'notes': 'bjyy.1.xxxxxx', 'accounting': 'jzyy.1.xxxxxx'}


class LocalDataCapture:
    def __init__(self, device):
        if device.bundle not in BUNDLES.values():
            raise ValueError('A target-bound Notes1 or Accounting Device is required')
        self.device = device

    def _capture(self, label):
        tree = self.device.layout(label)
        path = self.device.output / f'{self.device.counter:04d}-{label}.json'
        return tree, path.name

    def _receipt(self, kind, values, layouts):
        receipt = {'status': 'OBSERVED', 'kind': kind, 'bundle': self.device.bundle,
                   'sequence': self.device.counter, 'evidence_directory': str(self.device.output),
                   'layouts': layouts, 'values': values, 'business_acceptance': 'NOT_EVALUATED'}
        save_json(self.device.output / f'{self.device.counter:04d}-{kind}-observation.json', receipt)
        return receipt

    def observe(self, mode, *, prefix=None, max_scrolls=4):
        notes = mode in {'note-editor', 'note-index'}
        expected = BUNDLES['notes' if notes else 'accounting']
        if self.device.bundle != expected:
            raise ValueError('Observation mode does not match the target bundle')
        if notes and (not isinstance(prefix, str) or not prefix):
            raise ValueError('A nonempty synthetic note prefix is required')
        if mode == 'statistics':
            return self._statistics(max_scrolls)
        if mode == 'ledger-index':
            return self._ledger_index(max_scrolls)
        if mode == 'transactions':
            return self._transactions(max_scrolls)
        readers = {'note-editor': lambda t: read_note_editor(t, prefix=prefix),
                   'note-index': lambda t: read_note_index(t, prefix=prefix),
                   'ledger': read_ledger}
        if mode not in readers:
            raise ValueError('Unknown observation mode')
        tree, path = self._capture(mode)
        return self._receipt(mode, readers[mode](tree), [path])

    def _transactions(self, max_scrolls):
        if type(max_scrolls) is not int or not 0 <= max_scrolls <= 4:
            raise ValueError('Transactions supports zero to four explicit outer-view scrolls')
        tree, path = self._capture('transactions')
        month, layouts = read_month(tree), [path]
        for step in range(max_scrolls + 1):
            if read_month(tree) != month:
                raise CheckFailed('MONTH_CHANGED_DURING_TRANSACTION_CAPTURE')
            try:
                result = read_transactions(tree)
            except OutsideViewport:
                if step == max_scrolls:
                    raise Blocked('Transactions not fully visible within the fixed scroll allowance')
                self._scroll_outer(tree, 'independent_transaction_scroll', step+1)
                tree, path = self._capture('transactions-scrolled')
                layouts.append(path)
                continue
            return self._receipt('transactions', {**result,
                'completeness_basis': result.get('empty_evidence',
                    'single_frame_full_groups_and_source_footer')}, layouts)
        raise AssertionError('Unreachable transaction collection state')

    def _scroll_outer(self, tree, action, step, *, toward_top=False):
        scroll = one([n for n in visible(tree) if kind(n) == 'Scroll'
                      and attributes(n).get('scrollable') in (True, 'true')], 'Accounting outer scroll')
        left, upper, right, lower = rect(scroll)
        x = (left + right) // 2
        y1, y2 = upper + (lower-upper)*3//4, upper + (lower-upper)//4
        if toward_top:
            y1, y2 = y2, y1
        self.device.action(action, step=step, x=x, start_y=y1, end_y=y2)
        self.device.command('shell', 'uitest', 'uiInput', 'swipe', x, y1, x, y2, 600)

    def _ledger_index(self, max_scrolls):
        """Bounded top-to-bottom normal-UI inventory. No names-only deduplication."""
        if type(max_scrolls) is not int or not 1 <= max_scrolls <= 4:
            raise ValueError('Ledger inventory supports one to four scrolls per direction')
        tree, path = self._capture('ledger-index')
        page, layouts = read_ledger_page(tree), [path]

        def scroll(previous, direction, step):
            left, top, right, bottom = previous['viewport']
            x = (left + right) // 2
            # Short movement retains overlap between complete cards.
            middle = (top + bottom) // 2
            distance = (bottom - top) // 4
            end = middle + distance if direction == 'top' else middle - distance
            self.device.action('independent_ledger_scroll', direction=direction, step=step)
            self.device.command('shell', 'uitest', 'uiInput', 'swipe', x, middle, x, end, 600)
            captured, name = self._capture('ledger-index-' + direction)
            layouts.append(name)
            return read_ledger_page(captured)

        for step in range(1, max_scrolls + 1):
            previous, page = page, scroll(page, 'top', step)
            if page == previous:
                if not page['top_visible']:
                    raise Blocked('Ledger top boundary not observed')
                break
        else:
            raise Blocked('Ledger top not reached within fixed scroll allowance')
        rows = list(page['rows'])
        for step in range(1, max_scrolls + 1):
            previous, page = page, scroll(page, 'bottom', step)
            if page == previous:
                if not page['bottom_visible']:
                    raise Blocked('Ledger bottom boundary not observed')
                defaults = [r['name'] for r in rows if r['group'] == '默认账本']
                previous_default = one(defaults, 'Previous default ledger')
                return self._receipt('ledger-index', {'rows': rows, 'previous_default': previous_default,
                    'complete_list': True, 'completeness_basis': 'bounded_UI_edges_and_card_overlap'}, layouts)
            current = page['rows']
            overlaps = [n for n in range(1, min(len(rows), len(current)) + 1)
                        if rows[-n:] == current[:n]]
            if not overlaps:
                raise Blocked('Ledger pages lack matching complete-card overlap')
            added = current[max(overlaps):]
            if any(r['name'] in [v['name'] for v in rows] for r in added):
                raise Blocked('Ledger duplicate or changed order during inventory')
            rows.extend(added)
        raise Blocked('Ledger bottom not reached within fixed scroll allowance')

    def statistics_top_visible(self, *, control=None, max_scrolls=4):
        """Explicitly reveal summary/optional control; never restart or retry a command."""
        if type(max_scrolls) is not int or not 0 <= max_scrolls <= 4:
            raise ValueError('Statistics supports zero to four explicit outer-view scrolls')
        tree, path = self._capture('statistics-top')
        month, layouts = read_month(tree), [path]
        labels = ['总支出', '总收入'] + ([control] if control else [])
        previous = None
        for step in range(max_scrolls + 1):
            if read_month(tree) != month:
                raise CheckFailed('MONTH_CHANGED_DURING_STATISTICS_TOP_RECOVERY')
            reports = [n for n in visible(tree) if kind(n) == 'Select'
                       and (attributes(n).get('text') == '报表'
                            or any(text(t) == '报表' for t in text_nodes(n)))]
            one(reports, 'Statistics report selector')
            missing = []
            for label in labels:
                found = [n for n in text_nodes(tree) if text(n) == label]
                if not found:
                    missing.append(label)
                    continue
                target = one(found, f'Statistics control {label}')
                try:
                    within(target, viewport(tree, target))
                except OutsideViewport:
                    missing.append(label)
            if not missing:
                return tree, statistics_top(tree), layouts
            # Ignore volatile IDs; compare visible content and position directly.
            position = [(kind(n), attributes(n).get('text'), attributes(n).get('bounds'))
                        for n in visible(tree)]
            if position == previous:
                raise Blocked(f'Statistics top recovery made no progress: {missing}')
            if step == max_scrolls:
                raise Blocked(f'Statistics top not visible within fixed scroll allowance: {missing}')
            previous = position
            self._scroll_outer(tree, 'independent_statistics_top_scroll', step+1, toward_top=True)
            tree, path = self._capture('statistics-top-restored')
            layouts.append(path)

    def _statistics(self, max_scrolls):
        if type(max_scrolls) is not int or not 0 <= max_scrolls <= 4:
            raise ValueError('Statistics supports zero to four explicit outer-view scrolls')
        tree, top, layouts = self.statistics_top_visible(max_scrolls=max_scrolls)
        for step in range(max_scrolls + 1):
            if read_month(tree) != top['month']:
                raise CheckFailed('MONTH_CHANGED_DURING_REPORT_CAPTURE')
            try:
                report = read_daily_report(tree)
            except OutsideViewport:
                if step == max_scrolls:
                    raise Blocked('Daily report not fully visible within the fixed scroll allowance')
                self._scroll_outer(tree, 'independent_report_scroll', step+1)
                tree, path = self._capture('statistics-report')
                layouts.append(path)
                continue
            return self._receipt('statistics', {**top, **report,
                                 'ledger': None, 'ledger_binding': 'REQUIRES_BRACKETING_LEDGER_CAPTURES'}, layouts)
        raise AssertionError('Unreachable report collection state')
