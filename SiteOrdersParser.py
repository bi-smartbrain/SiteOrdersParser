import os
import time
from datetime import datetime as dt
from sys import prefix

from tg_logger import logger
from get_tokens import get_tokens
import functions as f
from dotenv import load_dotenv

load_dotenv()


####################################################################
#### Скрипт для обновления данных о заявках с сайтов компании ######
####################################################################

class OrdersManager:
    def __init__(self):
        self.old_report = None
        self.spread = 'Анализ каналов продаж'
        self.sheet = 'SiteOrders'
        self.username = os.getenv('SITE_USERNAME')
        self.password = os.getenv('SITE_PASSWORD')
        self.auth_token = None

    def load_old_report(self):
        """Загрузка отчета из гугл-таблицы при запуске/перезапуске скрипта"""
        # Загружаем отчет из Google Таблицы только при первом запуске
        if self.old_report is None:
            print("Загрузка отчета из Google Таблицы...")
            self.old_report = f.get_sheet_range(self.spread, self.sheet, 'A2:S')
            print(f"Загружено {len(self.old_report)} строк")

    def get_tokens(self):
        """Получение токена авторизации"""
        # Получаем токен авторизации
        self.auth_token = get_tokens(username=self.username, password=self.password)['access']

    def process_orders(self):
        """Обработка заявок - за одно обращение к платформе"""
        # Получаем последние заявки с сайта
        print("Получаем последние 100 заявок с сайтов...")
        orders = f.get_orders_from_sites(self.auth_token)
        report = f.create_report(orders)

        # Сравниваем с имеющимся отчетом
        new_rows = f.get_new_report_rows(self.old_report, report)

        if new_rows:
            print(f"Найдено {len(new_rows)} новых заявок")

            # Добавляем новые строки в Google Таблицу
            f.add_report_to_sheet(self.spread, self.sheet, new_rows)

            # Отправляем уведомления
            f.take_notifications(new_rows)

            # Обновляем локальный отчет, добавляя новые строки
            self.old_report.extend(new_rows)
            print(f"Локальный отчет обновлен. Теперь в нем {len(self.old_report)} строк")


        # Обновляем время последнего обновления в таблице
        updated_at = dt.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write_spread_range(self.spread, self.sheet, "U1", updated_at)

        return len(new_rows) if new_rows else 0


def main():
    # Создаем менеджер заявок (сохраняет состояние между итерациями обращений к платформе)
    orders_manager = OrdersManager()

    # Загружаем данные из таблицы (только при первом запуске)
    orders_manager.load_old_report()

    # Основной цикл обработки
    start_count = 1
    iteration_count = 1
    while True:
        try:
            orders_manager.get_tokens()
            new_orders_count = orders_manager.process_orders()
            print(f"{dt.now()} - Обработка завершена. Новых заявок: {new_orders_count}")
            prefix = 'пере' if start_count > 1 else ''
            if iteration_count == 1:
                logger.debug(f"\n✅ {current_file} успешно {prefix}запустился!")
            time.sleep(60 * 10)  # Ожидание 10 минут
            iteration_count += 1

        except Exception as e:
            print(f"Ошибка: {e}")
            logger.critical(f"\n❌️ {current_file} ошибка в основном цикле: {str(e)}")
            time.sleep(5)  # Задержка перезапуска
            start_count += 1
            iteration_count = 1


if __name__ == '__main__':
    current_file = os.path.basename(__file__)
    main()