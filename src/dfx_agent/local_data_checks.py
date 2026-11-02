"""Independent value checks for Notes1/Accounting; no app edits or device calls.

The future UI adapters must collect complete records and attach raw UI evidence.
These helpers alone do not constitute a device or five-stage acceptance result.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation

from .harmony_runtime import Blocked, CheckFailed


def check_notes(rows, samples, *, edited):
    """Rows are the complete, opened synthetic notes, never home-card previews."""
    try:
        observed = Counter((row['title'], row['body']) for row in rows)
        expected = Counter((row['title'], row.get('edited_body', row['body']) if edited else row['body'])
                           for row in samples)
    except (KeyError, TypeError) as error:
        raise Blocked('Incomplete note observations') from error
    if observed != expected:
        raise CheckFailed('NOTE_CONTENT_PAIR_OR_COUNT_MISMATCH')


def money(value):
    # Currency presentation stripping belongs to the UI adapter. Missing text is
    # not zero; the recorded normalized value must be an exact finite amount.
    if not isinstance(value, str) or not value.strip():
        raise Blocked('Missing normalized currency observation')
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise Blocked('Unreadable currency observation') from error
    if not result.is_finite():
        raise Blocked('Non-finite currency observation')
    return result


def check_transactions(rows, expected):
    try:
        actual = Counter((row['date'], row['direction'], money(row['amount'])) for row in rows)
        wanted = Counter((row['date'], row['direction'], money(row['amount'])) for row in expected)
        categories = [row['category'] for row in rows if row['direction'] == 'expense']
    except (KeyError, TypeError) as error:
        raise Blocked('Incomplete transaction observations') from error
    if actual != wanted:
        raise CheckFailed('TRANSACTION_CONTENT_OR_COUNT_MISMATCH')
    if len(categories) != 2 or not categories[0] or categories[0] != categories[1]:
        raise CheckFailed('EXPENSE_CATEGORY_MISMATCH')


def check_statistics(observation, *, ledger, month, expected):
    """Check labeled totals plus all daily report rows for one selected month."""
    try:
        if observation['ledger'] != ledger or observation['month'] != month:
            raise CheckFailed('LEDGER_OR_MONTH_MISMATCH')
        actual = (money(observation['expense']), money(observation['income']))
        wanted = (money(expected['expense']), money(expected['income']))
        daily = Counter((row['date'], money(row['expense']), money(row['income']))
                        for row in observation['daily'])
        wanted_daily = Counter((row['date'], money(row['expense']), money(row['income']))
                               for row in expected['daily'])
    except (KeyError, TypeError) as error:
        raise Blocked('Incomplete statistics observations') from error
    if actual != wanted:
        raise CheckFailed('STATISTICS_TOTAL_MISMATCH')
    if daily != wanted_daily:
        raise CheckFailed('STATISTICS_DAILY_MISMATCH')
