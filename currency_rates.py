"""Конвертация валют через ЦБ РФ (cbr-xml-daily.ru).

Источник отдаёт «1 единица иностранной валюты = X рублей» для всех нужных нам
валют (USD, KZT, UZS и т.д.). Конверсия между двумя любыми валютами идёт через
рубль как промежуточную. Курсы кэшируются на 6 часов в памяти, чтобы не
дёргать API на каждое уведомление.
"""
import time
import requests

_CBR_URL = 'https://www.cbr-xml-daily.ru/daily_json.js'
_TTL_SECONDS = 6 * 3600

_cache = {'rates': None, 'fetched_at': 0.0}


def _fetch_rates_to_rub():
    """Возвращает {ISO_CODE: rate_in_RUB_per_unit} или None при сетевой ошибке."""
    try:
        r = requests.get(_CBR_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f'currency_rates: не удалось получить курсы ЦБ РФ: {e}')
        return None

    rates = {'RUB': 1.0}
    for code, v in (data.get('Valute') or {}).items():
        value = v.get('Value')
        nominal = v.get('Nominal') or 1
        if value:
            rates[code] = value / nominal
    return rates


def _get_rates():
    """Возвращает кэшированные курсы. Перезапрашивает после истечения TTL."""
    now = time.time()
    cached = _cache['rates']
    if cached is None or (now - _cache['fetched_at']) > _TTL_SECONDS:
        fresh = _fetch_rates_to_rub()
        if fresh is not None:
            _cache['rates'] = fresh
            _cache['fetched_at'] = now
            return fresh
        # сеть пропала — отдаём то, что есть (даже устаревшее), либо None
    return _cache['rates']


def convert(amount, from_currency, to_currency):
    """Конвертирует amount из from_currency в to_currency через рубль.

    Возвращает float (сумма в to_currency) или None при ошибке — отсутствие
    одной из валют в справочнике ЦБ РФ либо проблемы с сетью.
    """
    if not amount:
        return None
    from_cur = (from_currency or '').upper()
    to_cur = (to_currency or '').upper()
    if not from_cur or not to_cur:
        return None
    if from_cur == to_cur:
        return float(amount)
    rates = _get_rates()
    if not rates:
        return None
    if from_cur not in rates or to_cur not in rates:
        return None
    amount_rub = float(amount) * rates[from_cur]
    return amount_rub / rates[to_cur]
