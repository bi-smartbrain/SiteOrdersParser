import time
import os
import requests
import gspread
from gspread.utils import rowcol_to_a1
from tg_logger import logger


# Получаем путь к файлу из переменных окружения
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')

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
    worksheet.update(cell_range, report, value_input_option="user_entered")

    print("Отчет добавлен")


def get_orders_from_sites(auth_token):
    headers = {'authorization': f'Bearer {auth_token}'}
    params = {'size': '100'}

    orders = []
    for site in ['rubrain.com', 'junbrain.com', 'engibrain.com']:
        page = 1
        # flag = True
        # while flag:
        params['page'] = page
        url = f'https://{site}/api/v2/applications/manager/list/?requestType=site'
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        object = response.json()
        orders.extend(object['results'])
        for item in object['results']:
            item['site'] = site
            item['order_url'] = f'https://{site}/account/manager-projects/project/{item["project"]}'
        page += 1
        print(object)
            # if not object['next']:
            #     flag = False
    return orders


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
    for item in new_rows:
        tg_message = (
            f'\n🔔️ Новая заявка\n'
            f'Дата: {item[14]}\n'
            f'Ссылка: {item[13]}\n'
            f'Текст: {item[11]}\n'
            f'Компания: {item[4]}\n'
            f'Имя: {item[8]} {item[9]}\n'
            f'📞 {item[16]}\n'
            f'✉️ {item[10]}'
        )
        logger.success(tg_message)
        time.sleep(3)