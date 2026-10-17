from unittest.mock import patch
import sys
import types

# Stub modules with external dependencies (gspread, tg_logger, etc.)
# so functions.py can be imported without real environment setup.
# NOTE: 'translator' is intentionally NOT stubbed — let the real module load
# so that tests/test_translator.py (which also runs in the same pytest process)
# gets the genuine module and not a stub.
for mod in ['gspread', 'tg_logger', 'env_loader', 'currency_rates']:
    sys.modules.setdefault(mod, types.ModuleType(mod))

# tg_logger needs these attributes accessed at import time
_tg = sys.modules['tg_logger']
_tg.logger = types.SimpleNamespace(critical=lambda *a, **k: None)
_tg.token = 'fake-token'
_tg.chat_id_3 = 'fake-chat-brains'
_tg.chat_id_5 = 'fake-chat-freelance'

# env_loader needs SECRETS_PATH
_env = sys.modules['env_loader']
_env.SECRETS_PATH = '/tmp/fake-secrets'

# gspread.utils needs to be importable as a submodule
_gspread = sys.modules['gspread']
_gspread_utils = types.ModuleType('gspread.utils')
_gspread_utils.rowcol_to_a1 = lambda r, c: 'A1'
_gspread_utils.ValueInputOption = types.SimpleNamespace(user_entered='USER_ENTERED')
sys.modules['gspread.utils'] = _gspread_utils
_gspread.utils = _gspread_utils
_gspread.service_account = lambda filename: None

# currency_rates needs convert
_cr = sys.modules['currency_rates']
_cr.convert = lambda amount, from_cur, to_cur: None

import functions


def _make_item(name='Test project', descr='Test description'):
    """Minimal item with required fields."""
    item = [''] * 21
    item[0] = '123'       # id
    item[4] = 'ACME'      # company
    item[8] = 'John'      # first_name
    item[9] = 'Doe'       # last_name
    item[10] = 'j@d.com'  # email
    item[11] = descr      # descr
    item[12] = 'smartbrain.io'  # site
    item[13] = 'https://smartbrain.io/account/manager-projects/project/123'
    item[14] = '2026-07-02 10:00'  # post_dtime
    item[16] = '+71234567890'  # phone
    item[17] = name       # name
    item[18] = ''         # budget
    item[19] = ''         # currency
    return item


def test_build_notification_no_marker_for_ru():
    msg = functions.build_notification(_make_item(), lang='RU')
    assert '🌐' not in msg


def test_build_notification_marker_for_en():
    msg = functions.build_notification(_make_item(), lang='EN')
    assert '🌐 EN→RU' in msg


def test_build_notification_marker_for_kz():
    msg = functions.build_notification(_make_item(), lang='KZ')
    assert '🌐 KZ→RU' in msg


def test_take_notifications_calls_translate_and_passes_lang():
    """take_notifications should call translate_if_needed and pass lang to build_notification."""
    item = _make_item(name='Hello project', descr='Hello description')

    with patch('functions.translate_if_needed', return_value=('EN', 'Привет проект', 'Привет описание')) as mock_translate, \
         patch('functions._send_telegram_message', return_value=(True, 'ok', 111)) as mock_send, \
         patch('time.sleep'):
        functions.take_notifications([item])

    mock_translate.assert_called_once_with('Hello project', 'Hello description')
    # The message sent should contain the EN→RU marker
    sent_message = mock_send.call_args[0][1]
    assert '🌐 EN→RU' in sent_message
    # Translated text should appear in the message
    assert 'Привет проект' in sent_message


def test_take_notifications_ru_no_translation_replacement():
    """When lang is RU, item fields should not be replaced."""
    item = _make_item(name='Русский проект', descr='Русское описание')

    with patch('functions.translate_if_needed', return_value=('RU', 'Русский проект', 'Русское описание')) as mock_translate, \
         patch('functions._send_telegram_message', return_value=(True, 'ok', 111)) as mock_send, \
         patch('time.sleep'):
        functions.take_notifications([item])

    mock_translate.assert_called_once()
    sent_message = mock_send.call_args[0][1]
    assert '🌐' not in sent_message
