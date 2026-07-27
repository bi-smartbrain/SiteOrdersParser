# Дизайн: автоперевод заявок в Telegram-уведомлениях

**Дата:** 2026-07-02
**Проект:** SiteOrdersParser

## Контекст

Заявки на сайтах компании поступают на разных языках: русский (основная масса с brain-сайтов), английский, казахский (freelance.kz), узбекский (free.uz). Менеджеры читают уведомления в Telegram — иноязычные заявки приходится переводить вручную. Шеф попросил добавить автоперевод на русский прямо в уведомление.

## Требования

- Переводить **название проекта** (`name`, поле R) и **описание** (`descr`, поле L).
- Переводить **только если язык не русский** — определение и перевод в одном запросе к AI.
- В шапке уведомления показывать **маркер языка** (`🌐 EN→RU`) — один раз, только для нерусских заявок.
- При недоступности AI — отправлять уведомление без перевода (graceful degradation), не прерывать основной цикл.

## Архитектура

### Новый модуль: `translator.py`

Единственная публичная функция:

```python
def translate_if_needed(name: str, descr: str) -> tuple[str, str, str]:
    """
    Возвращает (lang, name_ru, descr_ru).
    Если lang == 'RU' — name_ru и descr_ru совпадают с входными.
    При ошибке — возвращает ('RU', name, descr) и логирует в stdout.
    """
```

**Один вызов к Groq** с промптом:
```
Определи язык текста и переведи на русский.
Верни только JSON, без пояснений:
{"lang": "RU|EN|KZ|UZ|...", "name_ru": "...", "descr_ru": "..."}
Если язык уже русский — верни исходные тексты без изменений, lang="RU".

name: {name}
descr: {descr}
```

- Модель: `llama-3.1-8b-instant` (быстрая, бесплатный тир Groq)
- Ключ: `GROQ_API_KEY` из `.env` (секреты монтируются в контейнер `/secrets/.env`)
- Fallback: любое исключение → `return ('RU', name, descr)`, `print(warning)` в stdout

### Изменения в `functions.py`

**`take_notifications()`** — перед `build_notification`:

```python
from translator import translate_if_needed

lang, name_ru, descr_ru = translate_if_needed(item[17], item[11])
if lang != 'RU':
    item = list(item)   # копия, не мутируем оригинал
    item[17] = name_ru
    item[11] = descr_ru
```

Параметр `lang` передаётся в `build_notification(item, lang)`.

**`build_notification(item, lang='RU')`** — маркер в шапке:

```python
lang_marker = f'  🌐 {lang}→RU' if lang != 'RU' else ''
'🔔️ Новая заявка на проект' + lang_marker
```

### Изменения в `requirements.txt`

Добавить строку: `groq`

### Конфигурация сервера

Добавить в `/opt/secrets/.env`:
```
GROQ_API_KEY=<ключ>
```

## Поток данных

```
get_orders_from_sites()
    ↓
create_report() / enrich_with_contacts()
    ↓
take_notifications(new_rows)
    ↓ для каждой строки
    translate_if_needed(name, descr)   ← новый вызов
        → Groq API (если lang != RU)
        → fallback: вернуть оригинал
    ↓
    build_notification(item, lang)     ← маркер в шапке
    ↓
    _send_telegram_message(chat_id, message)
```

## Обработка ошибок

| Ситуация | Поведение |
|---|---|
| Groq недоступен / таймаут | Fallback: уведомление без перевода, `print` в stdout |
| Невалидный JSON от Groq | Fallback: то же |
| `lang == 'RU'` | Перевод не применяется, маркер не добавляется |
| Пустое описание или название | Передаём пустую строку, Groq вернёт пустую — без ошибок |

## Что не меняется

- Данные в Google Sheets — перевод только для уведомления, в таблицу пишется оригинал.
- Логика дедупликации, обогащения контактов, маршрутизации чатов.
- Структура `item` — мутируем только локальную копию внутри `take_notifications`.
