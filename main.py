"""
Скрипт для отслеживания новых заявок на сайтах компании
"""

import os
import time
from datetime import datetime as dt

from tg_logger import logger
from get_tokens import get_tokens
import functions as f
from env_loader import SECRETS_PATH
from sent_state import SentState, order_key
from delivery import Outbox, DeliveryJournal, write_health


class OrdersManager:
    def __init__(self):
        self.old_report = None
        self.spread = 'Анализ каналов продаж'
        self.sheet = 'SiteOrders'
        self.username = os.getenv('SITE_USERNAME')
        self.password = os.getenv('SITE_PASSWORD')
        self.auth_token = None
        self.state = SentState()
        self.outbox = Outbox()
        self.journal = DeliveryJournal()
        self.seed_from_sites = self.state.fresh  # файла состояния ещё не было

    def load_old_report(self):
        """Загрузка отчета из гугл-таблицы при запуске/перезапуске скрипта"""
        # Загружаем отчет из Google Таблицы только при первом запуске
        if self.old_report is None:
            print("Загрузка отчета из Google Таблицы...")
            self.old_report = f.get_sheet_range(self.spread, self.sheet, 'A2:S')
            print(f"Загружено {len(self.old_report)} строк")
            if self.state.fresh:
                # Первый запуск с файлом состояния: всё, что уже есть в таблице, считаем отправленным
                self.state.seed(order_key(r) for r in self.old_report if len(r) > 12)
                print(f"Файл состояния создан, добавлено {len(self.state.entries)} ключей из таблицы")

    def get_tokens(self):
        """Получение токена авторизации"""
        # Получаем токен авторизации
        self.auth_token = get_tokens(username=self.username, password=self.password)['access']

    def process_orders(self):
        """Обработка заявок - за одно обращение к платформе"""
        # Получаем последние заявки с сайта
        print("Получаем последние 100 заявок с сайтов...")
        orders, tokens_by_site = f.get_orders_from_sites(self.auth_token)
        report = f.create_report(orders)

        if self.seed_from_sites:
            # Состояния ещё не было: всё, что сейчас отдают сайты, уже существует — не уведомляем о нём
            self.state.seed(order_key(r) for r in report[1:])
            self.seed_from_sites = False

        # Сравниваем с имеющимся отчетом
        new_rows = f.get_new_report_rows(self.old_report, report)

        # Общий бюджет на ретраи этого цикла: новые заявки и очередь досылки
        deadline = time.monotonic() + f.CYCLE_RETRY_BUDGET
        failed = []

        if new_rows:
            print(f"Найдено {len(new_rows)} новых заявок")

            # Подтягиваем контакты клиента (email/phone) из detail-эндпоинта
            f.enrich_with_contacts(new_rows, tokens_by_site)

            # Добавляем новые строки в Google Таблицу
            f.add_report_to_sheet(self.spread, self.sheet, new_rows)

            # Отправляем уведомления: отметка «отправлено» ставится только при успехе,
            # при неудаче заявка остаётся в outbox и повторяется в следующих циклах
            try:
                sent_info = f.take_notifications(
                    new_rows, self.state,
                    outbox=self.outbox, journal=self.journal, deadline=deadline,
                )
                failed.extend(sent_info['failed'])
            except Exception as e:
                logger.critical(f'❌ SiteOrdersParser: сбой отправки уведомлений: {e}')

            # Обновляем локальный отчет, добавляя новые строки
            self.old_report.extend(new_rows)
            print(f"Локальный отчет обновлен. Теперь в нем {len(self.old_report)} строк")

        # Досылаем то, что не ушло в предыдущих циклах
        try:
            flushed = f.flush_outbox(
                self.outbox, self.state, self.journal, deadline=deadline,
            )
            failed.extend(flushed['failed'])
        except Exception as e:
            logger.critical(f'❌ SiteOrdersParser: сбой досылки очереди: {e}')

        unsent = len(self.outbox)
        if unsent or failed:
            f.notify_undelivered(failed, unsent)
            print(f"Не доставлено уведомлений: {unsent} (проблемные в этом цикле: {failed})")

        write_health(
            unsent=unsent,
            sent_total=self.state.delivered_count(),
            last_error=None,
        )

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
                logger.info(f"\n✅ SiteOrdersParser успешно {prefix}запустился!")
            time.sleep(60 * 10)  # Ожидание 10 минут
            iteration_count += 1

        except Exception as e:
            print(f"Ошибка: {e}")
            logger.critical(f"\n❌️ SiteOrdersParser ошибка в основном цикле: {str(e)}")
            try:
                write_health(
                    unsent=len(orders_manager.outbox),
                    sent_total=orders_manager.state.delivered_count(),
                    last_error=str(e)[:300],
                )
            except Exception as health_error:
                print(f"health.json не записан: {health_error}")
            time.sleep(60)  # Задержка перезапуска
            start_count += 1
            iteration_count = 1


if __name__ == '__main__':
    main()