import os
from loguru import logger
from notifiers.logging import NotificationHandler
from env_loader import SECRETS_PATH

# Токен и chat_id остаются открытыми для импорта из functions.py — там
# уведомления о заявках отправляются напрямую через Bot API, минуя
# обёртку notifiers, чтобы исключить расхождения в валидации параметров.
token = os.getenv("TG_TOKEN")
chat_id_1 = os.getenv("CHAT_ID_1")  # личный чат: служебные сообщения, ошибки
chat_id_3 = os.getenv("CHAT_ID_3")  # общий чат: уведомления о заявках с brain-сайтов
chat_id_5 = os.getenv("CHAT_ID_5")  # общий чат: уведомления о заявках с фриланс-сайтов

# Через loguru + notifiers идут только служебные сообщения (старт/перезапуск,
# критические ошибки) в личный чат. Boring plain text, без parse_mode.
params_chat_1 = {
    "token": token,
    "chat_id": chat_id_1,
}
tg_handler_1 = NotificationHandler("telegram", defaults=params_chat_1)
logger.add(tg_handler_1, level="DEBUG")
