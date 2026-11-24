from pathlib import Path
from unittest.mock import patch

from . import test_notifications  # noqa: F401  (заглушки внешних модулей + import functions)
import functions
from delivery import DeliveryJournal, Outbox
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


def test_entry_without_message_id_is_not_delivered(tmp_path):
    """Запись без message_id (упавшая отправка по старому коду) не считается доставкой."""
    s = SentState(str(tmp_path / 'state.json'))
    s.mark('freelance.kz:819', chat_id='c', message_id=None)
    assert not s.has('freelance.kz:819')
    assert s.unsent_count() == 1
    assert s.delivered_count() == 0

    s.seed(['freelance.kz:1'])
    s.mark('freelance.kz:2', suppressed=True)
    assert s.has('freelance.kz:1') and s.has('freelance.kz:2')
    assert s.unsent_count() == 1


def test_already_sent_rows_are_skipped(tmp_path):
    s = SentState(str(tmp_path / 'state.json'))
    s.seed([order_key(_row(1))])
    with patch('functions._send_telegram_message', return_value=functions.SendResult(True, 7, 'none', 'ok')) as send, \
         patch('functions.time.sleep'):
        functions.take_notifications([_row(1), _row(2)], s)
    assert send.call_count == 1
    assert s.entries[order_key(_row(2))]['message_id'] == 7


def test_burst_is_suppressed_and_marked(tmp_path):
    s = SentState(str(tmp_path / 'state.json'))
    journal = DeliveryJournal(str(tmp_path))
    rows = [_row(i) for i in range(functions.MAX_BURST + 1)]
    with patch('functions._send_telegram_message') as send, patch('functions.time.sleep'):
        functions.take_notifications(rows, s, journal=journal)
    send.assert_not_called()
    assert all(s.entries[order_key(r)].get('suppressed') for r in rows)
    lines = Path(journal.path()).read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == functions.MAX_BURST + 1
    assert '"result": "suppressed"' in lines[0]


def test_failed_send_is_not_marked_and_goes_to_outbox(tmp_path):
    """Новое поведение: при финальной неудаче 'отправлено' не ставится, заявка
    остаётся в outbox и повторяется в следующем цикле."""
    s = SentState(str(tmp_path / 'state.json'))
    outbox = Outbox(str(tmp_path / 'outbox.json'))
    journal = DeliveryJournal(str(tmp_path))
    failure = functions.SendResult(False, None, 'definite', 'status 502: bad gateway')

    with patch('functions._send_telegram_message', return_value=failure), \
         patch('functions.time.sleep'):
        result = functions.take_notifications([_row(5)], s, outbox=outbox, journal=journal)

    key = order_key(_row(5))
    assert not s.has(key)
    assert key in outbox
    assert outbox.get(key)['attempts'] == functions.SEND_ATTEMPTS
    assert result['failed'] == [key]
    lines = Path(journal.path()).read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == functions.SEND_ATTEMPTS
