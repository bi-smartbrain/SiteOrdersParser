# Translation Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить автоперевод названия и описания заявки на русский через Groq AI, с маркером исходного языка в шапке Telegram-уведомления.

**Architecture:** Новый модуль `translator.py` делает один запрос к Groq и возвращает `(lang, name_ru, descr_ru)`. `take_notifications()` вызывает его перед сборкой уведомления и при необходимости подставляет переведённые поля в локальную копию строки. `build_notification()` получает параметр `lang` и добавляет маркер `🌐 XX→RU` в шапку.

**Tech Stack:** Python 3.11, `groq` SDK (PyPI), модель `llama-3.1-8b-instant`, существующий `env_loader.py` для чтения `GROQ_API_KEY`.

## Global Constraints

- Не изменять данные в Google Sheets — перевод только для уведомления, в таблицу пишется оригинал.
- При любой ошибке Groq — fallback: уведомление уходит без перевода, без критикал-лога в Telegram.
- `item` в `take_notifications` не мутировать — работать с `list(item)`.
- Groq модель: `llama-3.1-8b-instant`.
- `GROQ_API_KEY` читается из `.env` через `os.getenv`.

---

## File Map

| Файл | Действие | Ответственность |
|---|---|---|
| `translator.py` | Создать | Запрос к Groq, парсинг JSON, fallback |
| `functions.py` | Изменить | Вызов переводчика в `take_notifications`, `lang` в `build_notification` |
| `requirements.txt` | Изменить | Добавить `groq` |

---

### Task 1: Модуль `translator.py` с тестом

**Files:**
- Create: `translator.py`
- Create: `tests/test_translator.py`

**Interfaces:**
- Produces: `translate_if_needed(name: str, descr: str) -> tuple[str, str, str]`
  - Возвращает `(lang, name_ru, descr_ru)`
  - `lang` — строка типа `'RU'`, `'EN'`, `'KZ'`, `'UZ'` и т.п.
  - При ошибке возвращает `('RU', name, descr)`

- [ ] **Шаг 1: Написать падающий тест (мок Groq)**

Создать `tests/test_translator.py`:

```python
import json
from unittest.mock import patch, MagicMock
import pytest


def _mock_groq_response(content: str):
    """Хелпер: создаёт mock-ответ Groq с нужным content."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_translate_english():
    payload = json.dumps({"lang": "EN", "name_ru": "Дизайн сайта", "descr_ru": "Нужен дизайнер"})
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.return_value = _mock_groq_response(payload)
        from translator import translate_if_needed
        lang, name_ru, descr_ru = translate_if_needed("Website design", "Need a designer")
    assert lang == "EN"
    assert name_ru == "Дизайн сайта"
    assert descr_ru == "Нужен дизайнер"


def test_russian_passthrough():
    payload = json.dumps({"lang": "RU", "name_ru": "Дизайн сайта", "descr_ru": "Нужен дизайнер"})
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.return_value = _mock_groq_response(payload)
        from translator import translate_if_needed
        lang, name_ru, descr_ru = translate_if_needed("Дизайн сайта", "Нужен дизайнер")
    assert lang == "RU"


def test_fallback_on_invalid_json():
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.return_value = _mock_groq_response("not json at all")
        from translator import translate_if_needed
        lang, name_ru, descr_ru = translate_if_needed("Test", "Test descr")
    assert lang == "RU"
    assert name_ru == "Test"
    assert descr_ru == "Test descr"


def test_fallback_on_groq_exception():
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.side_effect = Exception("connection error")
        from translator import translate_if_needed
        lang, name_ru, descr_ru = translate_if_needed("Test", "Test descr")
    assert lang == "RU"
    assert name_ru == "Test"
    assert descr_ru == "Test descr"
```

- [ ] **Шаг 2: Запустить тест — убедиться что падает**

```
python -m pytest tests/test_translator.py -v
```

Ожидаем: `ImportError: No module named 'translator'` или аналог.

- [ ] **Шаг 3: Добавить `groq` в `requirements.txt`**

Добавить строку в конец `requirements.txt`:
```
groq
```

Установить локально:
```
pip install groq
```

- [ ] **Шаг 4: Создать `translator.py`**

```python
import os
import json
from groq import Groq

groq_client = Groq(api_key=os.getenv('GROQ_API_KEY'))

_PROMPT_TEMPLATE = """\
Определи язык текста и переведи на русский.
Верни только JSON без пояснений:
{{"lang": "RU|EN|KZ|UZ|...", "name_ru": "...", "descr_ru": "..."}}
Если язык уже русский — верни исходные тексты без изменений, lang="RU".

name: {name}
descr: {descr}\
"""


def translate_if_needed(name: str, descr: str) -> tuple[str, str, str]:
    """Определяет язык и при необходимости переводит name и descr на русский.

    Возвращает (lang, name_ru, descr_ru).
    При ошибке — ('RU', name, descr), чтобы уведомление ушло без перевода.
    """
    try:
        response = groq_client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[{
                'role': 'user',
                'content': _PROMPT_TEMPLATE.format(name=name or '', descr=descr or ''),
            }],
            temperature=0,
            max_tokens=1024,
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        lang = str(data.get('lang', 'RU')).upper()
        name_ru = str(data.get('name_ru', name or ''))
        descr_ru = str(data.get('descr_ru', descr or ''))
        return lang, name_ru, descr_ru
    except Exception as e:
        print(f'[translator] fallback: {e}')
        return 'RU', name or '', descr or ''
```

- [ ] **Шаг 5: Запустить тесты — убедиться что проходят**

```
python -m pytest tests/test_translator.py -v
```

Ожидаем: 4 теста `PASSED`.

- [ ] **Шаг 6: Коммит**

```bash
git add translator.py tests/test_translator.py requirements.txt
git commit -m "add translator module with Groq fallback"
```

---

### Task 2: Интеграция в `functions.py`

**Files:**
- Modify: `functions.py` — функции `build_notification` (строка 284) и `take_notifications` (строка 335)
- Create: `tests/test_notifications.py`

**Interfaces:**
- Consumes: `translate_if_needed(name: str, descr: str) -> tuple[str, str, str]` из `translator.py`
- Produces: `build_notification(item, lang='RU')` — новая сигнатура

- [ ] **Шаг 1: Написать падающий тест на маркер языка**

Создать `tests/test_notifications.py`:

```python
from unittest.mock import patch
import sys
import types

# Заглушка для модулей с внешними зависимостями (gspread, tg_logger и т.п.)
# чтобы можно было импортировать functions без реального окружения
for mod in ['gspread', 'tg_logger', 'env_loader', 'currency_rates']:
    sys.modules.setdefault(mod, types.ModuleType(mod))

import functions


def _make_item(name='Test project', descr='Test description'):
    """Минимальный item с нужными полями."""
    item = [''] * 21
    item[0] = '123'       # id
    item[4] = 'ACME'      # company
    item[8] = 'John'      # first_name
    item[9] = 'Doe'       # last_name
    item[10] = 'j@d.com'  # email
    item[11] = descr      # descr
    item[12] = 'smartbrain.io'  # site
    item[13] = 'https://smartbrain.io/account/manager-projects/project/123'
    item[14] = '2026-07-02 10:00'  # post_dtime
    item[16] = '+71234567890'  # phone
    item[17] = name       # name
    item[18] = ''         # budget
    item[19] = ''         # currency
    return item


def test_build_notification_no_marker_for_ru():
    msg = functions.build_notification(_make_item(), lang='RU')
    assert '🌐' not in msg


def test_build_notification_marker_for_en():
    msg = functions.build_notification(_make_item(), lang='EN')
    assert '🌐 EN→RU' in msg


def test_build_notification_marker_for_kz():
    msg = functions.build_notification(_make_item(), lang='KZ')
    assert '🌐 KZ→RU' in msg
```

- [ ] **Шаг 2: Запустить тест — убедиться что падает**

```
python -m pytest tests/test_notifications.py -v
```

Ожидаем: `TypeError: build_notification() takes 1 positional argument but 2 were given` или аналог.

- [ ] **Шаг 3: Изменить `build_notification` в `functions.py`**

Заменить сигнатуру и шапку:

```python
def build_notification(item, lang='RU'):
    """Собирает текст уведомления в формате HTML (parse_mode=HTML)."""
    freelance_regions = {'freelance.kz': 'КАЗАХСТАН', 'free.uz': 'УЗБЕКИСТАН'}
    site = item[12]
    descr_short = _truncate_words(item[11] or '', 500)

    lang_marker = f'  🌐 {lang}→RU' if lang != 'RU' else ''
    lines = [
        '',
        f'🔔️ Новая заявка на проект{lang_marker}',
        '',
        f'<b>Дата:</b> {_html_escape(item[14])}',
    ]
    if site in freelance_regions:
        lines.append(f'<b>Регион:</b> {freelance_regions[site]}')
    lines.extend([
        f'<b>Проект {_html_escape(item[0])}:</b> {_html_escape(item[17])}',
        f'<b>Бюджет:</b> {_html_escape(_format_budget(item[18], item[19]))}',
        f'<b>Описание:</b> {_html_escape(descr_short)}',
        '',
        f'<b>Ссылка:</b> {_html_escape(item[13])}',
        f'<b>Компания:</b> {_html_escape(item[4])}',
        f'<b>Имя:</b> {_html_escape(item[8])} {_html_escape(item[9])}',
        f'📞 {_html_escape(item[16])}',
        f'✉️ {_html_escape(item[10])}',
    ])
    if site in freelance_regions:
        lines.append('')
        lines.append(_format_mentions())
    return '\n'.join(lines)
```

- [ ] **Шаг 4: Изменить `take_notifications` в `functions.py`**

Добавить импорт в начало файла (после существующих импортов):

```python
from translator import translate_if_needed
```

Заменить тело `take_notifications`:

```python
def take_notifications(new_rows):
    """Шлёт уведомление по каждой новой заявке в общий чат напрямую через Bot API.
    При неудаче пишет critical-лог в личку, но не прерывает обработку остальных."""
    for item in new_rows:
        site = item[12]
        project_id = item[0]
        target_chat = CHAT_FREELANCE if site in ('freelance.kz', 'free.uz') else CHAT_BRAINS

        lang, name_ru, descr_ru = translate_if_needed(item[17], item[11])
        if lang != 'RU':
            item = list(item)
            item[17] = name_ru
            item[11] = descr_ru

        message = build_notification(item, lang)
        ok, info = _send_telegram_message(target_chat, message)
        if not ok:
            logger.critical(
                f'❌ Не удалось отправить уведомление о проекте {project_id} ({site}) '
                f'в чат {target_chat}: {info}'
            )
        time.sleep(3)
```

- [ ] **Шаг 5: Запустить все тесты**

```
python -m pytest tests/ -v
```

Ожидаем: все тесты `PASSED`.

- [ ] **Шаг 6: Коммит**

```bash
git add functions.py tests/test_notifications.py
git commit -m "integrate translation into notifications"
```

---

### Task 3: Конфигурация сервера и деплой

**Files:**
- Modify: `/opt/secrets/.env` на сервере (вручную)

- [ ] **Шаг 1: Добавить `GROQ_API_KEY` в `.env` на сервере**

```bash
ssh root@bi.smartbrain.io "echo 'GROQ_API_KEY=<твой_ключ>' >> /opt/secrets/.env"
```

Проверить что ключ добавился:

```bash
ssh root@bi.smartbrain.io "grep GROQ_API_KEY /opt/secrets/.env"
```

Ожидаем: строка `GROQ_API_KEY=gsk_...`

- [ ] **Шаг 2: Запушить код — автодеплой пересоберёт контейнер**

```bash
git push origin main
```

- [ ] **Шаг 3: Дождаться деплоя и проверить логи**

```bash
ssh root@bi.smartbrain.io "docker logs --tail 30 site-orders-parser 2>&1"
```

Ожидаем: нет строк `[translator] fallback`, контейнер запущен без ошибок.

- [ ] **Шаг 4: Дождаться первой нерусской заявки и проверить уведомление**

В Telegram-уведомлении должна появиться строка:
```
🔔️ Новая заявка на проект  🌐 EN→RU
```
(или `KZ→RU`, `UZ→RU`)

Если все заявки русские — проверить вручную: временно поменять в `take_notifications` условие `if lang != 'RU'` на `if True` для одной итерации, задеплоить, откатить.
