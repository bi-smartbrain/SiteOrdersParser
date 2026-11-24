"""Доставка уведомлений: журнал попыток, очередь неотправленного, health-файл.

Зачем отдельный модуль: state (sent_state.py) отвечает только за «что уже
доставлено», а здесь — «что ещё нет» и «что реально произошло при отправке».
Журнал append-only, очередь и health пишутся атомарно (tmp + os.replace),
поэтому крэш в момент записи не оставляет битый JSON.
"""

import json
import os
from datetime import datetime as dt

from sent_state import STATE_PATH

LOGS_DIR = os.path.dirname(STATE_PATH) or '.'


def _now():
    return dt.now().astimezone()


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class DeliveryJournal:
    """Одна JSON-строка на каждую попытку отправки, файл на сутки.

    Поля: ts, order, project, site, chat_id, chat_id_num, message_id, attempt,
    result, error_class, possible_duplicate, error. По ним можно сверить
    фактический чат (message_id + время) и увидеть историю попыток, а не
    только финальное состояние.
    """

    def __init__(self, dir_path=LOGS_DIR):
        self.dir = dir_path

    def path(self, when=None):
        when = when or _now()
        return os.path.join(self.dir, f'delivery-{when:%Y-%m-%d}.jsonl')

    def record(self, **fields):
        entry = {'ts': _now().isoformat(timespec='seconds')}
        entry.update(fields)
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        return entry


class Outbox:
    """Очередь заявок, которые ещё не доставлены.

    Запись создаётся ДО первой попытки отправки (status='pending'): крэш между
    отправкой и записью результата не теряет заявку, максимум даёт дубль,
    который видно в журнале. Текст хранится готовым, чтобы повтор не зависел
    от перевода, обогащения контактами и дедупа по Google-таблице.
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(LOGS_DIR, 'outbox.json')
        self.entries = {}
        if os.path.exists(self.path):
            with open(self.path, encoding='utf-8') as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.entries = loaded

    def add(self, key, text, chat_id, project=None, site=None):
        entry = {
            'order': key,
            'chat_id': chat_id,
            'text': text,
            'project': project,
            'site': site,
            'created': _now().isoformat(timespec='seconds'),
            'attempts': 0,
            'ambiguous': 0,
            'needs_review': False,
            'last_error': None,
            'status': 'pending',
        }
        self.entries[key] = entry
        self.save()
        return entry

    def get(self, key):
        return self.entries.get(key)

    def keys(self):
        return list(self.entries)

    def remove(self, key):
        if key in self.entries:
            del self.entries[key]
            self.save()

    def save(self):
        _atomic_write(self.path, self.entries)

    def __len__(self):
        return len(self.entries)

    def __contains__(self, key):
        return key in self.entries


def age_hours(entry):
    """Возраст заявки в часах по полю created (ISO со смещением)."""
    created = entry.get('created')
    if not created:
        return 0.0
    try:
        started = dt.fromisoformat(created)
    except ValueError:
        return 0.0
    return (_now() - started).total_seconds() / 3600


def write_health(unsent, sent_total, last_error=None, dir_path=LOGS_DIR):
    """health.json для ежедневного чекапа: число неотправленных и последняя ошибка."""
    payload = {
        'updated_at': _now().isoformat(timespec='seconds'),
        'unsent': int(unsent),
        'sent_total': int(sent_total),
        'last_error': last_error,
    }
    _atomic_write(os.path.join(dir_path, 'health.json'), payload)
    return payload
