import time
import os
import re
import html
import requests
import gspread
from gspread.utils import rowcol_to_a1, ValueInputOption
from tg_logger import logger
from env_loader import SECRETS_PATH

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
        url = f'https://{site}/api/v2/project/manager/control/mine/list/'
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


def _strip_html(text):
    """Чистит HTML-теги из текста и декодирует HTML-сущности."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def enrich_with_contacts(rows, tokens_by_site):
    """Для каждой новой строки тянет email и phone клиента из detail-эндпоинта
    /api/projects/{id}/ — в списочной выдаче их нет."""
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
                logger.warning(f'contacts проекта {project_id} с {site}: статус {r.status_code}')
                continue
            customer = (r.json().get('customer') or {})
            row[10] = customer.get('email') or ''
            phone = customer.get('phone') or ''
            row[16] = phone.lstrip('+') if phone else ''
        except Exception as e:
            logger.warning(f'contacts проекта {project_id} с {site}: {e}')


def create_report(orders):
    report = [['id', 'post_date', 'source', 'project', 'company', 'creator', 'sta0tus', 'type', 'first_name', 'last_name', 'email', 'mesage', 'site', 'order_url', 'post_dtime', 'dt_id', 'phone']]
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
            _strip_html(order.get('descr')),           # L mesage (текст проекта без HTML)
            order.get('site'),                         # M site
            order.get('order_url'),                    # N order_url
            created[:16].replace('T', ' '),            # O post_dtime
            created,                                   # P dt_id (для дедупа)
            '',                                        # Q phone — обогащается из detail
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


def take_notifications(new_rows):
    freelance_regions = {'freelance.kz': 'КАЗАХСТАН', 'free.uz': 'УЗБЕКИСТАН'}
    for item in new_rows:
        site = item[12]
        region_line = f'Регион: {freelance_regions[site]}\n' if site in freelance_regions else ''
        tg_message = (
            f'\n🔔️ Новая заявка\n'
            f'Дата: {item[14]}\n'
            f'{region_line}'
            f'Ссылка: {item[13]}\n'
            f'Текст: {item[11]}\n'
            f'Компания: {item[4]}\n'
            f'Имя: {item[8]} {item[9]}\n'
            f'📞 {item[16]}\n'
            f'✉️ {item[10]}'
        )

        if site in freelance_regions:
            tg_message += '\n@karyushka @aglaya_smartbrainio @katrinkee @TsaritsaPolei @olya_smartbrain'

        logger.bind(site=site).success(tg_message)
        time.sleep(3)
