import os
from loguru import logger
from notifiers.logging import NotificationHandler
from dotenv import load_dotenv
from env_loader import SECRETS_PATH


# Параметры для чатов 1-Андрей, 2-Таня, 3-Узкий круг
token = os.getenv("TG_TOKEN")
chat_id_1 = os.getenv("CHAT_ID_1")
chat_id_2 = os.getenv("CHAT_ID_2")
chat_id_3 = os.getenv("CHAT_ID_3")
chat_id_5 = os.getenv("CHAT_ID_5")

params_chat_1 = {
    "token": token,
    "chat_id": chat_id_1,
}
params_chat_2 = {
    "token": token,
    "chat_id": chat_id_2,
}
params_chat_3 = {
    "token": token,
    "chat_id": chat_id_3,
}

params_chat_5 = {
    "token": token,
    "chat_id": chat_id_5,
}

tg_handler_1 = NotificationHandler("telegram", defaults=params_chat_1)
tg_handler_2 = NotificationHandler("telegram", defaults=params_chat_2)
tg_handler_3 = NotificationHandler("telegram", defaults=params_chat_3)
tg_handler_5 = NotificationHandler("telegram", defaults=params_chat_5)

def not_success(record):
    return record["level"].name != "SUCCESS"

def only_success(record):
    return record["level"].name == "SUCCESS"


def only_success_freelance(record):
    return only_success(record) and record["extra"].get("site") == "freelance.kz"


def only_success_not_freelance(record):
    return only_success(record) and record["extra"].get("site") != "freelance.kz"

logger.add(tg_handler_1, level="DEBUG", filter=not_success)
# logger.add(tg_handler_2, level="INFO", filter=not_success)
logger.add(tg_handler_3, level="SUCCESS", filter=only_success_not_freelance)
logger.add(tg_handler_5, level="SUCCESS", filter=only_success_freelance)
