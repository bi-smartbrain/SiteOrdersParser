import os
import time
from datetime import datetime as dt
from tg_logger import logger
from get_tokens import get_tokens
import functions as f
from dotenv import load_dotenv
load_dotenv()

####################################################################
#### Скрипт для обновления данных о заявках с сайтов компании ######
####################################################################


def main():
    spread = 'Анализ каналов продаж'
    sheet = 'SiteOrders'
    username = os.getenv('SITE_USERNAME')
    password = os.getenv('SITE_PASSWORD')
    auth_token = get_tokens(username=username, password=password)['access']
    orders = f.get_orders_from_sites(auth_token)
    report = f.create_report(orders)
    old_report = f.get_sheet_range(spread, sheet, 'A2:S')
    new_rows = f.get_new_report_rows(old_report, report)
    if new_rows:
        f.add_report_to_sheet(spread, sheet, new_rows)
        f.take_notifications(new_rows)
    else:
        print('Новых заявок нет')
    updated_at =  dt.now().strftime('%Y-%m-%d %H:%M:%S')
    f.write_spread_range(spread, sheet, "U1", updated_at)


if __name__ == '__main__':
    current_file = os.path.basename(__file__)
    logger.debug(f"скрипт {current_file} запущен")
    while True:
        try:
            main()
            print(dt.now())
            time.sleep(60 * 5)
        except Exception as e:
            print(e)
            logger.critical(f"{current_file}, ошибка: {str(e)}")
            time.sleep(60 * 2)  # задержка перезапуска скрипта
            logger.debug(f"перезапуск скрипта {current_file}")
