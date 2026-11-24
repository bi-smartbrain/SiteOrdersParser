"""Файл состояния: какие заявки уже отправлялись в чат (и id сообщений в Telegram).

Google-таблица остаётся отчётом, но источником правды для дедупликации
уведомлений служит этот файл — он не зависит от ручных правок таблицы.
"""

import json
import os
from datetime import datetime as dt

STATE_PATH = os.getenv('SENT_STATE_PATH', os.path.join('logs', 'sent_state.json'))


def order_key(row):
    """Ключ заявки: сайт + id проекта (id уникален только в пределах сайта)."""
    return f'{row[12]}:{row[0]}'


class SentState:
    def __init__(self, path=STATE_PATH):
        self.path = path
        self.fresh = not os.path.exists(path)
        self.entries = {}
        if not self.fresh:
            with open(path, encoding='utf-8') as fh:
                self.entries = json.load(fh)

    def has(self, key):
        """Считается отправленной только реально доставленная заявка.

        Запись без message_id (упавшая отправка по старому коду) доставкой не
        является, иначе такая заявка заблокирована навсегда. Seed/suppressed —
        отдельные состояния: их не отправляли осознанно.
        """
        entry = self.entries.get(key)
        if entry is None:
            return False
        if entry.get('seed') or entry.get('suppressed'):
            return True
        return entry.get('message_id') is not None

    def seed(self, keys):
        """Помечает ключи как уже виденные (без уведомления). Одна запись на диск."""
        now = dt.now().isoformat(timespec='seconds')
        for key in keys:
            self.entries.setdefault(key, {'ts': now, 'seed': True})
        self.save()

    def mark(self, key, chat_id=None, message_id=None, suppressed=False, attempts=None):
        entry = {'ts': dt.now().isoformat(timespec='seconds'),
                 'chat_id': chat_id, 'message_id': message_id}
        if suppressed:
            entry['suppressed'] = True
        if attempts:
            entry['attempts'] = attempts
        self.entries[key] = entry
        self.save()

    def unsent_count(self):
        """Сколько записей отмечены, но не доставлены (для чекапа и алертов)."""
        return sum(1 for key in self.entries if not self.has(key))

    def delivered_count(self):
        return sum(1 for key in self.entries if self.has(key))

    def save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.entries, fh, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.fresh = False
