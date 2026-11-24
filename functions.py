import time
import os
import re
import html
from typing import NamedTuple, Optional

import requests
import gspread
from gspread.utils import ValueInputOption
from tg_logger import (
    logger,
    token as TG_TOKEN,
    chat_id_1 as CHAT_OWNER,
    chat_id_3 as CHAT_BRAINS,
    chat_id_5 as CHAT_FREELANCE,
)
from env_loader import SECRETS_PATH
from currency_rates import convert as convert_currency
from translator import translate_if_needed
from sent_state import order_key
from delivery import age_hours

# Получаем путь к файлу из переменных окружения
SERVICE_ACCOUNT_FILE = os.path.join(SECRETS_PATH, 'service_account.json')
gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)

# После релиза freelance.kz/free.uz старый endpoint /api/v2/applications/manager/list/
# перестал отдавать свежие записи, а у новых записей post_date/created заметно отличаются
# от того, что хранится в нашей таблице. Поэтому дедуп через старые ключи не сработает —
# жёстко обрезаем по времени: всё, что создано после этой отметки, считаем потенциально новым.
# Значение выбрано между created последнего записанного проекта (15.06 11:03:53.634481)
# и created первого пост-релизного проекта (15.06 14:04:58).
NEW_ENDPOINT_CUTOFF = '2026-06-15T12:00:00+03:00'


# freelance.kz и free.uz после релиза обязательно требуют префикс /ru/ в API.
# Без него отдают 302 на /ru/..., и при follow-redirect POST превращается в GET
# → платформа отвечает «Метод GET не разрешен».
_LOCALIZED_SITES = ('freelance.kz', 'free.uz')


def api_base(site):
    if site in _LOCALIZED_SITES:
        return f'https://{site}/ru'
    return f'https://{site}'


def get_sheet_range(spread, incom_sheet, incom_range):
    sh = gc.open(spread)
    data = sh.worksheet(incom_sheet).get(incom_range)
    return data


def write_spread_range(spread, sheet, range, value):
    sh = gc.open(spread)
    worksheet = sh.worksheet(sheet)
    range_to_clear = worksheet.range(range)
    for cell in range_to_clear:
        cell.value = value
    worksheet.update_cells(range_to_clear)


def add_report_to_sheet(spread, sheet, report):
    sh = gc.open(spread)
    worksheet = sh.worksheet(sheet)
    worksheet.append_rows(report, value_input_option=ValueInputOption.user_entered)
    print("Отчет добавлен")


def get_orders_from_sites(auth_token):
    headers = {'authorization': f'Bearer {auth_token}'}
    params = {'size': '100', 'page': '1'}

    orders = []
    tokens_by_site = {}

    # smartbrain.io — англоязычный клон rubrain.com, тот же бэкенд/БД.
    # Переехали с rubrain.com, т.к. он периодически недоступен и рушит весь цикл.
    for site in ['smartbrain.io', 'junbrain.com', 'engibrain.com', 'freelance.kz', 'free.uz']:
        url = f'{api_base(site)}/api/v2/project/manager/control/list/'
        site_token = auth_token
        try:
            response = requests.get(url, params=params, headers=headers)

            # freelance.kz / free.uz могут требовать токен, выданный именно их доменом.
            if response.status_code in (401, 403) and site in ('freelance.kz', 'free.uz'):
                from get_tokens import get_tokens

                site_token = get_tokens(
                    username=os.getenv('SITE_USERNAME'),
                    password=os.getenv('SITE_PASSWORD'),
                    url=f'{api_base(site)}/api/auth/login/?active_lang=ru',
                )['access']

                response = requests.get(
                    url,
                    params=params,
                    headers={'authorization': f'Bearer {site_token}'},
                )

            response.raise_for_status()

        except requests.RequestException as e:
            logger.error(f'Ошибка при запросе заявок с {site}: {e}')
            continue

        tokens_by_site[site] = site_token
        payload = response.json()
        results = payload.get('results') or []
        for item in results:
            item['site'] = site
            item['order_url'] = f'https://{site}/account/manager-projects/project/{item["id"]}'
            orders.append(item)
        print(f'{site} ok!')
    return orders, tokens_by_site


def _html_to_text(text):
    """Превращает HTML в plain-text, сохраняя структуру: абзацы и переносы
    строк становятся '\\n', элементы списка — '• ', прочие теги вычищаются.
    Результат пригоден и для Google Sheet, и для Telegram (HTML parse_mode
    не парсит то, чего тут больше нет)."""
    if not text:
        return ''
    s = text
    # Перенос строки и закрывающие структурные теги → \n
    s = re.sub(r'<\s*br\s*/?\s*>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<\s*/\s*(p|div|h[1-6]|tr)\s*>', '\n', s, flags=re.IGNORECASE)
    # Элементы списка → маркер с новой строки
    s = re.sub(r'<\s*li[^>]*>', '\n• ', s, flags=re.IGNORECASE)
    s = re.sub(r'<\s*/\s*(ul|ol)\s*>', '\n', s, flags=re.IGNORECASE)
    # Всё остальное — удалить
    s = re.sub(r'<[^>]+>', '', s)
    # HTML-сущности (&nbsp;, &amp; и т.п.)
    s = html.unescape(s)
    # Схлопнуть лишние пробелы и пустые строки
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _truncate_words(text, max_len=500):
    """Обрезает текст до max_len символов по границе слова. Если резалось —
    в конце явный маркер '[…] ⤵', указывающий на ссылку под описанием."""
    if not text or len(text) <= max_len:
        return text or ''
    cut = text[:max_len].rsplit(' ', 1)[0].rstrip(' ,;:.-\n')
    return f'{cut} […] ⤵'


CONTACT_TIMEOUT_PLACEHOLDER = 'тайм-аут запроса'


def enrich_with_contacts(rows, tokens_by_site):
    """Для каждой новой строки тянет email, phone, budget и currency из
    detail-эндпоинта /api/projects/{id}/ — в списочной выдаче их нет.
    При таймауте/ошибке пишет placeholder в email/phone и логирует только
    в stdout, чтобы не засорять личный чат менеджера."""
    for row in rows:
        site = row[12]
        project_id = row[3]
        token = tokens_by_site.get(site)
        if not token or project_id in (None, ''):
            continue
        try:
            r = requests.get(
                f'{api_base(site)}/api/projects/{project_id}/',
                headers={'authorization': f'Bearer {token}'},
                timeout=15,
            )
            if r.status_code != 200:
                print(f'enrich_with_contacts: проект {project_id} ({site}) status={r.status_code}')
                row[10] = CONTACT_TIMEOUT_PLACEHOLDER
                row[16] = CONTACT_TIMEOUT_PLACEHOLDER
                continue
            data = r.json()
            customer = data.get('customer') or {}
            row[10] = customer.get('email') or ''
            phone = customer.get('phone') or ''
            row[16] = phone.lstrip('+') if phone else ''
            row[18] = data.get('budget') if data.get('budget') is not None else ''
            row[19] = data.get('currency') or ''
        except Exception as e:
            print(f'enrich_with_contacts: проект {project_id} ({site}) error: {e}')
            row[10] = CONTACT_TIMEOUT_PLACEHOLDER
            row[16] = CONTACT_TIMEOUT_PLACEHOLDER


def create_report(orders):
    report = [['id', 'post_date', 'source', 'project', 'company', 'creator', 'sta0tus', 'type', 'first_name', 'last_name', 'email', 'mesage', 'site', 'order_url', 'post_dtime', 'dt_id', 'phone', 'name', 'budget', 'currency']]
    for order in orders:
        customer = order.get('customer') or {}
        created = order.get('created') or ''
        project_id = order.get('id')
        report_row = [
            project_id,                                # A id (в новой модели = id проекта)
            created[:10],                              # B post_date (только дата)
            '',                                        # C source (поле потеряно в новой модели)
            project_id,                                # D project (тот же id проекта)
            customer.get('company') or '',             # E company
            '',                                        # F creator (поле потеряно)
            order.get('manager_status') or '',         # G sta0tus
            order.get('project_type') or '',           # H type
            customer.get('first_name') or '',          # I first_name
            customer.get('last_name') or '',           # J last_name
            '',                                        # K email — обогащается из detail
            _html_to_text(order.get('descr')),         # L mesage (текст проекта без HTML)
            order.get('site'),                         # M site
            order.get('order_url'),                    # N order_url
            created[:16].replace('T', ' '),            # O post_dtime
            created,                                   # P dt_id (для дедупа)
            '',                                        # Q phone — обогащается из detail
            order.get('name') or '',                   # R name (название проекта)
            '',                                        # S budget — обогащается из detail
            '',                                        # T currency — обогащается из detail
        ]
        report.append(report_row)
    return report


def get_new_report_rows(old_report, report):
    """Новой считается строка, у которой created строго больше cutoff-времени
    релиза И её timestamp ещё не записан в таблицу (in-memory защита от повторов
    внутри одного запуска)."""
    old_keys = set(row[15] for row in old_report)
    new_rows = []
    for row in report[1:]:
        created = row[15] or ''
        if created <= NEW_ENDPOINT_CUTOFF:
            continue
        if created in old_keys:
            continue
        new_rows.append(row)
    return new_rows


FREELANCE_MENTIONS = ['@karyushka', '@katrinkee', '@TsaritsaPolei', '@olya_smartbrain', '@Softek']


def _html_escape(text):
    """Эскейпит `<`, `>`, `&` для безопасной вставки в HTML parse_mode."""
    if text in (None, ''):
        return ''
    return html.escape(str(text), quote=False)


def _format_budget(budget, currency):
    """Форматирует бюджет с валютой и конвертацией в USD/RUB.
    Если бюджета нет — 'По договоренности'."""
    if not budget:
        return 'По договоренности'
    try:
        amount = float(budget)
    except (TypeError, ValueError):
        return str(budget)

    cur = (currency or '').upper()
    formatted = f'{int(amount):,}'.replace(',', ' ')
    base = f'{formatted} {cur}'.strip()

    converted = []
    if cur != 'USD':
        usd = convert_currency(amount, cur, 'USD')
        if usd is not None:
            converted.append(f'~${int(round(usd))}')
    if cur != 'RUB':
        rub = convert_currency(amount, cur, 'RUB')
        if rub is not None:
            converted.append(f'~₽{int(round(rub))}')
    if converted:
        return f'{base} ({", ".join(converted)})'
    return base


def _format_mentions():
    """Собирает строку упоминаний, каждый тэг целиком курсивом."""
    return ' '.join(f'<i>{m}</i>' for m in FREELANCE_MENTIONS)


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
        lines.append('')  # пустая строка-отступ перед списком упоминаний
        lines.append(_format_mentions())
    return '\n'.join(lines)


class SendResult(NamedTuple):
    """Результат одной попытки отправки.

    error_class:
      none      — доставлено, message_id получен;
      definite  — точно не доставлено (соединение не установилось, 4xx кроме 429,
                  429 с retry_after): повторять безопасно;
      ambiguous — неизвестно, создал ли Telegram сообщение (reset, read timeout,
                  RemoteDisconnected, 5xx, 200 без message_id): немедленный повтор
                  запрещён, иначе получаем дубль;
      permanent — повтор бессмысленен (400/401/403/404): в needs_review и алерт;
      deferred  — попытку не делали: не хватает бюджета цикла.
    """
    ok: bool
    message_id: Optional[int]
    error_class: str
    detail: str
    retry_after: Optional[int] = None


# Таймаут одной попытки не меняем (30 с): согласовано в ревью, уменьшение
# увеличило бы долю неоднозначных ошибок и, значит, отложенных повторов.
SEND_TIMEOUT = 30
SEND_ATTEMPTS = 3            # отправок на одну заявку за один цикл
SEND_RETRY_DELAYS = (5, 15)  # паузы между попытками внутри цикла
MAX_AMBIGUOUS_SENDS = 2      # при неоднозначной ошибке — максимум две отправки вообще
MAX_RETRY_AGE_HOURS = 24     # старше суток автоматически не досылаем, отдаём человеку
CYCLE_RETRY_BUDGET = 90      # секунд на все ретраи одного цикла
FLUSH_MAX_ITEMS = 5          # сколько заявок из очереди пробуем за цикл
INTER_MESSAGE_DELAY = 3      # пауза между разными заявками после успешной отправки
ALERT_COOLDOWN = 1800        # антиспам алертов в личку

_AMBIGUOUS_EXC_NAMES = {
    'ReadTimeout',
    'ConnectionResetError',
    'BrokenPipeError',
    'RemoteDisconnected',
    'ProtocolError',
    'IncompleteRead',
}
_DEFINITE_EXC_NAMES = {'ConnectTimeout'}


def _walk_exceptions(exc):
    """Все исключения в цепочке: requests заворачивает ConnectionResetError в
    ConnectionError, поэтому класс ошибки надо искать и в args, и в cause."""
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
        for arg in getattr(cur, 'args', ()):
            if isinstance(arg, BaseException):
                stack.append(arg)


def _classify_exception(exc):
    """definite — соединение не установилось/запрос отвергнут; ambiguous — не знаем."""
    names = {type(e).__name__ for e in _walk_exceptions(exc)}
    if names & _AMBIGUOUS_EXC_NAMES:
        return 'ambiguous'
    if names & _DEFINITE_EXC_NAMES:
        return 'definite'
    # ConnectionError без признаков reset/read-timeout: отказ соединения, DNS,
    # прокси — запрос до Telegram не дошёл.
    return 'definite'


def _send_telegram_message(chat_id, message):
    """Один POST в Bot API, без ретраев. Возвращает SendResult."""
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            data={
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': 'true',
            },
            timeout=SEND_TIMEOUT,
        )
    except Exception as e:
        return SendResult(False, None, _classify_exception(e), f'{type(e).__name__}: {e}')

    if r.status_code == 200:
        try:
            message_id = r.json()['result']['message_id']
        except (ValueError, KeyError, TypeError):
            # 200 без id — не доказательство доставки
            return SendResult(False, None, 'ambiguous', '200 без message_id')
        return SendResult(True, message_id, 'none', 'ok')

    if r.status_code == 429:
        retry_after = None
        try:
            retry_after = int(r.json()['parameters']['retry_after'])
        except (ValueError, KeyError, TypeError):
            pass
        return SendResult(False, None, 'definite', '429 rate limit', retry_after)

    if 400 <= r.status_code < 500:
        return SendResult(False, None, 'permanent', f'status {r.status_code}: {r.text[:300]}')

    return SendResult(False, None, 'ambiguous', f'status {r.status_code}: {r.text[:300]}')


def _numeric_chat_id(chat_id):
    """Числовой id для журнала. В .env chat_id хранится с ярлыком после '#'.

    Только для журнала: в API уходит сырое значение, оно проверено и работает.
    """
    return str(chat_id).split('#')[0]


def _deliver(entry, state, outbox, journal, deadline):
    """Отправляет одну заявку по политике ретраев. Заявка уже лежит в outbox
    (status='pending'), чтобы крэш не потерял её между попытками.

    Возвращает (ok, SendResult)."""
    key = entry['order']
    chat_id = entry['chat_id']
    message = entry['text']
    sends = 0
    result = None

    while sends < SEND_ATTEMPTS:
        if time.monotonic() >= deadline:
            result = SendResult(False, None, 'deferred', 'бюджет цикла исчерпан')
            break

        result = _send_telegram_message(chat_id, message)
        sends += 1
        entry['attempts'] = entry.get('attempts', 0) + 1
        entry['last_error'] = None if result.ok else result.detail
        entry['status'] = 'sent' if result.ok else 'failed'

        if journal is not None:
            journal.record(
                order=key,
                project=entry.get('project'),
                site=entry.get('site'),
                chat_id=chat_id,
                chat_id_num=_numeric_chat_id(chat_id),
                message_id=result.message_id,
                attempt=entry['attempts'],
                result=('sent' if result.ok else
                        'ambiguous' if result.error_class == 'ambiguous' else 'failed'),
                error_class=result.error_class,
                possible_duplicate=entry.get('ambiguous', 0) > 0,
                error=None if result.ok else result.detail,
            )

        if result.ok:
            if state is not None:
                state.mark(key, chat_id=chat_id, message_id=result.message_id,
                           attempts=entry['attempts'])
            if outbox is not None:
                outbox.remove(key)
            return True, result

        if result.error_class == 'permanent':
            entry['needs_review'] = True
            if outbox is not None:
                outbox.save()
            return False, result

        if result.error_class == 'ambiguous':
            # Повторять сразу нельзя: не знаем, создалось ли сообщение.
            # Откладываем до следующего цикла, не больше MAX_AMBIGUOUS_SENDS.
            entry['ambiguous'] = entry.get('ambiguous', 0) + 1
            if entry['ambiguous'] >= MAX_AMBIGUOUS_SENDS:
                entry['needs_review'] = True
            if outbox is not None:
                outbox.save()
            return False, result

        # definite: можно повторять, но не дольше бюджета цикла
        if outbox is not None:
            outbox.save()
        if sends >= SEND_ATTEMPTS:
            break
        if result.retry_after:
            delay = result.retry_after
        else:
            delay = SEND_RETRY_DELAYS[min(sends - 1, len(SEND_RETRY_DELAYS) - 1)]
        if time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)

    return False, result


_ALERT_LAST = {}


def alert_owner(text, key='delivery'):
    """Алерт владельцу в личку напрямую через Bot API (не через logger).

    У loguru-хендлера в tg_logger.py уровень DEBUG, то есть каждое сообщение
    лога уже уходит в этот же личный чат. Поэтому диагностика попыток идёт
    только в stdout контейнера, а сюда — один агрегированный алерт с антиспамом.
    """
    now = time.monotonic()
    if now - _ALERT_LAST.get(key, 0.0) < ALERT_COOLDOWN:
        print(f'alert_owner: подавлено антиспамом ({key})')
        return False
    _ALERT_LAST[key] = now
    res = _send_telegram_message(CHAT_OWNER, text)
    if not res.ok:
        print(f'alert_owner: не удалось отправить алерт в личку: {res.detail}')
    return res.ok


def notify_undelivered(failed_keys, unsent):
    """Один алерт за цикл: сколько не доставлено и по каким заявкам."""
    if not failed_keys and not unsent:
        return False
    keys = ', '.join(failed_keys[:10]) or '-'
    text = (
        f'⚠️ SiteOrdersParser: не доставлено уведомлений: {unsent}.\n'
        f'Проблемные заявки: {keys}\n'
        f'Повтор в следующем цикле. Заявки с постоянной ошибкой (4xx) и старше суток '
        f'автоматически не досылаются: нужна ручная проверка.\n'
        f'Журнал: logs/delivery-*.jsonl, очередь: logs/outbox.json'
    )
    return alert_owner(text)


# Если за один цикл «новых» заявок больше этого числа — это почти наверняка сбой
# дедупликации (перезапуск, правка таблицы), а не реальный поток: в чат не шлём.
MAX_BURST = 10


def take_notifications(new_rows, state=None, outbox=None, journal=None, deadline=None):
    """Шлёт уведомление по каждой новой заявке в общий чат напрямую через Bot API.

    state (SentState) — файл отправленных заявок: уже доставленные пропускаются,
    при массовом всплеске (> MAX_BURST) уведомления подавляются.
    outbox (Outbox) — очередь недоставленного: запись создаётся ДО первой попытки,
    «отправлено» ставится только при успехе.
    journal (DeliveryJournal) — журнал попыток для сверки с фактическим чатом.
    deadline — момент (time.monotonic), после которого ретраи прекращаются.

    Возвращает {'sent': n, 'failed': [ключи]}.
    """
    if deadline is None:
        deadline = time.monotonic() + CYCLE_RETRY_BUDGET
    failed = []
    sent = 0

    if state is not None:
        new_rows = [r for r in new_rows if not state.has(order_key(r))]
        if len(new_rows) > MAX_BURST:
            logger.critical(
                f'⚠️ Подавлено {len(new_rows)} уведомлений: подозрение на сбой дедупликации '
                f'(порог {MAX_BURST}). Заявки помечены как виденные, в чат не отправлены.'
            )
            for r in new_rows:
                state.mark(order_key(r), suppressed=True)
                if journal is not None:
                    journal.record(
                        order=order_key(r), project=r[0], site=r[12],
                        chat_id=None, chat_id_num=None, message_id=None,
                        attempt=0, result='suppressed', error_class='suppressed',
                        possible_duplicate=False, error=f'MAX_BURST={MAX_BURST}',
                    )
            return {'sent': 0, 'failed': []}

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
        key = order_key(item)

        if outbox is not None:
            entry = outbox.add(key, message, target_chat, project=project_id, site=site)
        else:
            entry = {'order': key, 'chat_id': target_chat, 'text': message,
                     'project': project_id, 'site': site, 'attempts': 0,
                     'ambiguous': 0, 'needs_review': False}

        ok, result = _deliver(entry, state, outbox, journal, deadline)
        if ok:
            sent += 1
            if time.monotonic() + INTER_MESSAGE_DELAY < deadline:
                time.sleep(INTER_MESSAGE_DELAY)
        else:
            failed.append(key)
            detail = result.detail if result is not None else 'нет результата'
            print(f'❌ Не удалось отправить уведомление о проекте {project_id} ({site}) '
                  f'в чат {target_chat}: {detail}')

    return {'sent': sent, 'failed': failed}


def flush_outbox(outbox, state=None, journal=None, deadline=None, max_items=FLUSH_MAX_ITEMS):
    """Повторяет недоставленное из предыдущих циклов.

    Пропускает записи, которые уже доставлены (крэш между state.mark и удалением
    из outbox), отданные человеку (needs_review) и старше MAX_RETRY_AGE_HOURS.
    """
    result = {'sent': 0, 'failed': []}
    if outbox is None:
        return result
    if deadline is None:
        deadline = time.monotonic() + CYCLE_RETRY_BUDGET

    # Порядок — FIFO, но needs_review не занимает место в квоте: иначе пять
    # «отданных человеку» записей заблокировали бы очередь навсегда.
    processed = 0
    for key in outbox.keys():
        if processed >= max_items or time.monotonic() >= deadline:
            break
        entry = outbox.get(key)
        if entry is None:
            continue
        if entry.get('needs_review'):
            continue
        processed += 1
        if state is not None and state.has(key):
            outbox.remove(key)
            continue
        if age_hours(entry) > MAX_RETRY_AGE_HOURS:
            entry['needs_review'] = True
            entry['last_error'] = entry.get('last_error') or 'старше суток, авторетраж остановлен'
            outbox.save()
            result['failed'].append(key)
            continue
        ok, _ = _deliver(entry, state, outbox, journal, deadline)
        if ok:
            result['sent'] += 1
        else:
            result['failed'].append(key)
    return result
