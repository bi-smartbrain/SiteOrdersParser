from unittest.mock import patch

from . import test_notifications  # noqa: F401  (заглушки внешних модулей + import functions)
import functions
from sent_state import SentState, order_key


def _row(project_id, site='freelance.kz'):
    item = test_notifications._make_item()
    item[0] = str(project_id)
    item[12] = site
    return item


def test_state_persists_between_instances(tmp_path):
    path = str(tmp_path / 'state.json')
    s = SentState(path)
    assert s.fresh
    s.mark('freelance.kz:1', chat_id='c', message_id=42)
    s2 = SentState(path)
    assert not s2.fresh
    assert s2.has('freelance.kz:1')
    assert s2.entries['freelance.kz:1']['message_id'] == 42


def test_seed_marks_keys_without_notification(tmp_path):
    s = SentState(str(tmp_path / 'state.json'))
    s.seed(['a:1', 'b:2'])
    assert s.has('a:1') and s.has('b:2')


def test_already_sent_rows_are_skipped(tmp_path):
    s = SentState(str(tmp_path / 'state.json'))
    s.seed([order_key(_row(1))])
    with patch('functions._send_telegram_message', return_value=(True, 'ok', 7)) as send, patch('time.sleep'):
        functions.take_notifications([_row(1), _row(2)], s)
    assert send.call_count == 1
    assert s.entries[order_key(_row(2))]['message_id'] == 7


def test_burst_is_suppressed_and_marked(tmp_path):
    s = SentState(str(tmp_path / 'state.json'))
    rows = [_row(i) for i in range(functions.MAX_BURST + 1)]
    with patch('functions._send_telegram_message') as send, patch('time.sleep'):
        functions.take_notifications(rows, s)
    send.assert_not_called()
    assert all(s.entries[order_key(r)].get('suppressed') for r in rows)


def test_failed_send_is_still_marked(tmp_path):
    s = SentState(str(tmp_path / 'state.json'))
    with patch('functions._send_telegram_message', return_value=(False, 'boom', None)), patch('time.sleep'):
        functions.take_notifications([_row(5)], s)
    assert s.has(order_key(_row(5)))
