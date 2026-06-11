import time
import os
import re
import requests
import gspread
from gspread.utils import rowcol_to_a1, ValueInputOption
from tg_logger import logger
from env_loader import SECRETS_PATH

# Получаем путь к файлу из переменных окружения
SERVICE_ACCOUNT_FILE = os.path.join(SECRETS_PATH, 'service_account.json')
gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)


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
    params = {'size': '100'}

    orders = []
    tokens_by_site = {}

    def build_order_url(site, item):
        return f'https://{site}/account/manager-projects/project/{item["project"]}'

    for site in ['rubrain.com', 'junbrain.com', 'engibrain.com', 'freelance.kz', 'free.uz']:
        page = 1
        # flag = True
        # while flag:
        params['page'] = str(page)
        url = f'https://{site}/api/v2/applications/manager/list/?requestType=site'
        site_token = auth_token
        try:
            response = requests.get(url, params=params, headers=headers)

            # Некоторые сайты (freelance.kz, free.uz) могут требовать токен, выданный именно их доменом.
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
        orders.extend(results)
        for item in results:
            item['site'] = site
            item['order_url'] = build_order_url(site, item)
        page += 1
        print(f'{site} ok!')
            # if not object['next']:
            #     flag = False
    return orders, tokens_by_site


def _normalize(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def combine_message_and_descr(message, descr):
    """Объединяет короткий message заявки и descr проекта.

    Правила: если descr пустой — берём message; если message пустой или совпадает
    с descr (после нормализации пробелов) либо содержится в нём — берём descr;
    иначе склеиваем оба через разделитель.
    """
    m = (message or '').strip()
    d = (descr or '').strip()
    if not d:
        return m
    if not m:
        return d
    nm, nd = _normalize(m), _normalize(d)
    if nm == nd or nm in nd:
        return d
    return f'{m}\n---\n{d}'


def fetch_project_descr(site, project_id, token):
    """Тянет поле descr из /api/projects/{id}/. Возвращает строку или None."""
    if project_id in (None, ''):
        return None
    try:
        r = requests.get(
            f'https://{site}/api/projects/{project_id}/',
            headers={'authorization': f'Bearer {token}'},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get('descr')
        logger.warning(f'descr проекта {project_id} с {site}: статус {r.status_code}')
    except Exception as e:
        logger.warning(f'descr проекта {project_id} с {site}: {e}')
    return None


def enrich_with_project_descr(rows, tokens_by_site):
    """Для каждой строки отчёта подтягивает descr связанного проекта и
    заменяет колонку с текстом заявки на объединённый текст."""
    for row in rows:
        site = row[12]
        project_id = row[3]
        token = tokens_by_site.get(site)
        if not token:
            continue
        descr = fetch_project_descr(site, project_id, token)
        row[11] = combine_message_and_descr(row[11], descr)


def create_report(orders):
    report = [['id', 'post_date', 'source', 'project', 'company', 'creator', 'sta0tus', 'type', 'first_name', 'last_name', 'email', 'mesage', 'site', 'order_url', 'post_dtime', 'dt_id', 'phone']]
    for order in orders:
        report_row = []
        phone = order.get('phone', '')
        phone = phone.replace('+', '') if phone else ''
        report_row.append(order['id'])
        report_row.append(order['post_date'][:10])
        report_row.append(order['source'])
        report_row.append(order['project'])
        report_row.append(order['company'])
        report_row.append(order['creator'])
        report_row.append(order['status'])
        report_row.append(order['type'])
        report_row.append(order['first_name'])
        report_row.append(order['last_name'])
        email = order['email'] if order['email'][0] != '+' else order['email'][1:]
        report_row.append(email)
        report_row.append(order['message'])
        report_row.append(order['site'])
        report_row.append(order['order_url'])
        report_row.append(order['post_date'][:16].replace('T', ' '))
        report_row.append(order['post_date'])
        report_row.append(phone)
        report.append(report_row)
    return report


def get_new_report_rows(old_report, report):
    ordersID = []
    for old_row in old_report:
        ordersID.append(old_row[15])
    new_rows = []
    for row in report[1:]:
        if row[15] not in ordersID:
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
