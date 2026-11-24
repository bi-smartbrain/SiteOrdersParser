from datetime import datetime as dt, timedelta
from pathlib import Path
from unittest.mock import patch

from . import test_notifications  # noqa: F401  (заглушки внешних модулей + import functions)
import functions
from delivery import DeliveryJournal, Outbox, age_hours, write_health
from sent_state import SentState, order_key


def _row(project_id, site='freelance.kz'):
    item = test_notifications._make_item()
    item[0] = str(project_id)
    item[12] = site
    return item


def _ok(message_id=500):
    return functions.SendResult(True, message_id, 'none', 'ok')


def _fail(error_class='definite', detail='boom', retry_after=None):
    return functions.SendResult(False, None, error_class, detail, retry_after)


def _journal_lines(journal):
    path = Path(journal.path())
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding='utf-8').strip().splitlines() if line]


def _setup(tmp_path):
    return (
        SentState(str(tmp_path / 'state.json')),
        Outbox(str(tmp_path / 'outbox.json')),
        DeliveryJournal(str(tmp_path)),
    )


def test_success_first_attempt_marks_state_and_journal(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message', return_value=_ok(42)) as send, \
         patch('functions.time.sleep'):
        result = functions.take_notifications([_row(1)], state, outbox=outbox, journal=journal)

    key = order_key(_row(1))
    assert send.call_count == 1
    assert result['sent'] == 1 and result['failed'] == []
    assert state.has(key) and state.entries[key]['message_id'] == 42
    assert key not in outbox
    lines = _journal_lines(journal)
    assert len(lines) == 1
    assert '"result": "sent"' in lines[0] and '"message_id": 42' in lines[0]


def test_definite_failure_then_success(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message',
               side_effect=[_fail('definite', 'status 502'), _ok(43)]) as send, \
         patch('functions.time.sleep') as sleep:
        result = functions.take_notifications([_row(2)], state, outbox=outbox, journal=journal)

    key = order_key(_row(2))
    assert send.call_count == 2
    assert sleep.call_args_list[0][0][0] == functions.SEND_RETRY_DELAYS[0]
    assert state.has(key) and state.entries[key]['message_id'] == 43
    assert key not in outbox
    assert len(_journal_lines(journal)) == 2


def test_final_failure_keeps_row_unsent_and_queued(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message', return_value=_fail()), \
         patch('functions.time.sleep') as sleep:
        result = functions.take_notifications([_row(3)], state, outbox=outbox, journal=journal)

    key = order_key(_row(3))
    assert not state.has(key)
    assert key in outbox
    assert outbox.get(key)['attempts'] == functions.SEND_ATTEMPTS
    assert result['failed'] == [key]
    delays = [call[0][0] for call in sleep.call_args_list]
    assert delays == list(functions.SEND_RETRY_DELAYS)


def test_recovery_flushes_outbox_next_cycle(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message', return_value=_fail()), \
         patch('functions.time.sleep'):
        functions.take_notifications([_row(4)], state, outbox=outbox, journal=journal)
    key = order_key(_row(4))
    assert key in outbox and not state.has(key)

    with patch('functions._send_telegram_message', return_value=_ok(77)), \
         patch('functions.time.sleep'):
        flushed = functions.flush_outbox(outbox, state, journal)

    assert flushed['sent'] == 1
    assert state.has(key) and state.entries[key]['message_id'] == 77
    assert key not in outbox


def test_ambiguous_error_is_not_retried_in_same_cycle(tmp_path):
    """ConnectionReset/read-timeout: повтор сразу запрещён, иначе дубль."""
    state, outbox, journal = _setup(tmp_path)
    ambiguous = _fail('ambiguous', "ConnectionError: ('Connection aborted.', ConnectionResetError(104, ...))")
    with patch('functions._send_telegram_message', return_value=ambiguous) as send, \
         patch('functions.time.sleep'):
        functions.take_notifications([_row(5)], state, outbox=outbox, journal=journal)

    key = order_key(_row(5))
    assert send.call_count == 1
    assert key in outbox and outbox.get(key)['ambiguous'] == 1
    assert not outbox.get(key)['needs_review']
    assert '"result": "ambiguous"' in _journal_lines(journal)[0]

    # следующий цикл: вторая неоднозначная отправка — дальше только человек
    with patch('functions._send_telegram_message', return_value=ambiguous) as send2, \
         patch('functions.time.sleep'):
        functions.flush_outbox(outbox, state, journal)

    assert send2.call_count == 1
    assert outbox.get(key)['ambiguous'] == functions.MAX_AMBIGUOUS_SENDS
    assert outbox.get(key)['needs_review']

    # третий цикл: needs_review не трогаем автоматически
    with patch('functions._send_telegram_message') as send3, patch('functions.time.sleep'):
        functions.flush_outbox(outbox, state, journal)
    send3.assert_not_called()


def test_ambiguous_retry_marks_possible_duplicate(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message', return_value=_fail('ambiguous')), \
         patch('functions.time.sleep'):
        functions.take_notifications([_row(6)], state, outbox=outbox, journal=journal)
    with patch('functions._send_telegram_message', return_value=_ok(9)), \
         patch('functions.time.sleep'):
        functions.flush_outbox(outbox, state, journal)
    last = _journal_lines(journal)[-1]
    assert '"possible_duplicate": true' in last
    assert state.has(order_key(_row(6)))


def test_permanent_error_goes_to_review_without_retry(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message',
               return_value=_fail('permanent', 'status 400: chat not found')) as send, \
         patch('functions.time.sleep'):
        functions.take_notifications([_row(7)], state, outbox=outbox, journal=journal)

    key = order_key(_row(7))
    assert send.call_count == 1
    assert outbox.get(key)['needs_review']


def test_rate_limit_retry_after_is_honoured(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message',
               side_effect=[_fail('definite', '429 rate limit', retry_after=7), _ok(11)]), \
         patch('functions.time.sleep') as sleep:
        functions.take_notifications([_row(8)], state, outbox=outbox, journal=journal)

    assert sleep.call_args_list[0][0][0] == 7
    assert state.has(order_key(_row(8)))


def test_cycle_budget_stops_attempts(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    with patch('functions._send_telegram_message') as send, patch('functions.time.sleep'):
        result = functions.take_notifications(
            [_row(9)], state, outbox=outbox, journal=journal, deadline=0,
        )
    send.assert_not_called()
    assert result['failed'] == [order_key(_row(9))]
    assert order_key(_row(9)) in outbox


def test_flush_limits_items_per_cycle(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    for i in range(3):
        outbox.add(f'freelance.kz:{100 + i}', 'text', 'chat', project=100 + i, site='freelance.kz')
    with patch('functions._send_telegram_message', return_value=_ok()) as send, \
         patch('functions.time.sleep'):
        functions.flush_outbox(outbox, state, journal, max_items=2)
    assert send.call_count == 2
    assert len(outbox) == 1


def test_flush_does_not_starve_queue_behind_needs_review(tmp_path):
    """Записи, отданные человеку, не должны блокировать остальную очередь."""
    state, outbox, journal = _setup(tmp_path)
    for i in range(5):
        entry = outbox.add(f'freelance.kz:{300 + i}', 'text', 'chat')
        entry['needs_review'] = True
    outbox.save()
    live_key = 'freelance.kz:400'
    outbox.add(live_key, 'text', 'chat')

    with patch('functions._send_telegram_message', return_value=_ok(5)), \
         patch('functions.time.sleep'):
        functions.flush_outbox(outbox, state, journal, max_items=1)

    assert state.has(live_key)
    assert live_key not in outbox


def test_flush_skips_already_delivered_and_too_old(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    delivered_key = 'freelance.kz:200'
    outbox.add(delivered_key, 'text', 'chat')
    state.mark(delivered_key, chat_id='chat', message_id=1)

    old_key = 'freelance.kz:201'
    entry = outbox.add(old_key, 'text', 'chat')
    entry['created'] = (dt.now().astimezone() - timedelta(hours=25)).isoformat(timespec='seconds')
    outbox.save()
    assert age_hours(entry) > 24

    with patch('functions._send_telegram_message') as send, patch('functions.time.sleep'):
        functions.flush_outbox(outbox, state, journal)

    send.assert_not_called()
    assert delivered_key not in outbox
    assert outbox.get(old_key)['needs_review']


def test_chat_id_with_label_is_sent_raw_and_logged_numeric(tmp_path):
    state, outbox, journal = _setup(tmp_path)
    item = _row(10)
    with patch('functions._send_telegram_message', return_value=_ok(12)) as send, \
         patch('functions.time.sleep'):
        functions.take_notifications([item], state, outbox=outbox, journal=journal)

    assert send.call_args[0][0] == functions.CHAT_FREELANCE
    line = _journal_lines(journal)[0]
    assert f'"chat_id": "{functions.CHAT_FREELANCE}"' in line
    assert '"chat_id_num": "fake-chat-freelance"' in line


def test_alert_owner_goes_to_private_chat_with_cooldown():
    functions._ALERT_LAST.clear()
    with patch('functions._send_telegram_message', return_value=_ok(1)) as send:
        assert functions.alert_owner('проблема')
        assert not functions.alert_owner('проблема снова')
    assert send.call_count == 1
    assert send.call_args[0][0] == functions.CHAT_OWNER


def test_health_file_reports_unsent(tmp_path):
    payload = write_health(unsent=3, sent_total=184, last_error=None, dir_path=str(tmp_path))
    assert payload['unsent'] == 3
    data = (tmp_path / 'health.json').read_text(encoding='utf-8')
    assert '"unsent": 3' in data and '"sent_total": 184' in data
