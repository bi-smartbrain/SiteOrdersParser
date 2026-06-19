import time
import os
import re
import html
import requests
import gspread
from gspread.utils import rowcol_to_a1, ValueInputOption
from tg_logger import logger, token as TG_TOKEN, chat_id_3 as CHAT_BRAINS, chat_id_5 as CHAT_FREELANCE
from env_loader import SECRETS_PATH
from currency_rates import convert as convert_currency

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

    # Получить размеры отчета (количество строк и столбцов)
    num_rows = len(report)
    num_cols = len(report[0])

    # Получить диапазон для записи данных
    q_rows = len(worksheet.get_all_values())  # узнаем кол-во уже заполненных на листе строк

    start_cell = rowcol_to_a1(q_rows + 1, 1)
    end_cell = rowcol_to_a1(q_rows + num_rows, num_cols)

    # Записать значения в диапазон
    cell_range = f"{start_cell}:{end_cell}"
    worksheet.update(cell_range, report, value_input_option=ValueInputOption.user_entered)

    print("Отчет добавлен")


def get_orders_from_sites(auth_token):
    headers = {'authorization': f'Bearer {auth_token}'}
    params = {'size': '100', 'page': '1'}

    orders = []
    tokens_by_site = {}

    for site in ['rubrain.com', 'junbrain.com', 'engibrain.com', 'freelance.kz', 'free.uz']:
        url = f'https://{site}/api/v2/project/manager/control/list/'
        site_token = auth_token
        try:
            response = requests.get(url, params=params, headers=headers)

            # freelance.kz / free.uz могут требовать токен, выданный именно их доменом.
            if response.status_code in (401, 403) and site in ('freelance.kz', 'free.uz'):
                from get_tokens import get_tokens

                site_token = get_tokens(
                    username=os.getenv('SITE_USERNAME'),
                    password=os.getenv('SITE_PASSWORD'),
                    url=f'https://{site}/api/auth/login/?active_lang=ru',
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
                f'https://{site}/api/projects/{project_id}/',
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


def build_notification(item):
    """Собирает текст уведомления в формате HTML (parse_mode=HTML)."""
    freelance_regions = {'freelance.kz': 'КАЗАХСТАН', 'free.uz': 'УЗБЕКИСТАН'}
    site = item[12]
    descr_short = _truncate_words(item[11] or '', 500)

    lines = [
        '',
        '🔔️ Новая заявка на проект',
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


def _send_telegram_message(chat_id, message):
    """Прямой POST в Bot API. Возвращает (ok: bool, info: str с диагностикой)."""
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            data={
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': 'true',
            },
            timeout=30,
        )
    except Exception as e:
        return False, f'exception: {e}'
    if r.status_code == 200:
        return True, 'ok'
    return False, f'status {r.status_code}: {r.text[:300]}'


def take_notifications(new_rows):
    """Шлёт уведомление по каждой новой заявке в общий чат напрямую через Bot API.
    При неудаче пишет critical-лог в личку, но не прерывает обработку остальных."""
    for item in new_rows:
        site = item[12]
        project_id = item[0]
        target_chat = CHAT_FREELANCE if site in ('freelance.kz', 'free.uz') else CHAT_BRAINS
        message = build_notification(item)
        ok, info = _send_telegram_message(target_chat, message)
        if not ok:
            logger.critical(
                f'❌ Не удалось отправить уведомление о проекте {project_id} ({site}) '
                f'в чат {target_chat}: {info}'
            )
        time.sleep(3)
