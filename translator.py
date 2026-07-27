import os
import json
from groq import Groq

# 'dummy' fallback allows module import and test patching when key is absent;
# in production the key is always present via /opt/secrets/.env.
groq_client = Groq(api_key=os.getenv('GROQ_API_KEY_BI') or 'dummy')

_PROMPT_TEMPLATE = """\
Ты переводчик. Определи язык текста и переведи на русский.

Язык определяй по полю descr (оно надёжнее, чем name).
Верни ТОЛЬКО валидный JSON, без пояснений и markdown:
{{"lang": "<код языка: RU/EN/KZ/UZ/DE/...>", "name_ru": "...", "descr_ru": "..."}}

Правила:
- Если язык русский: lang="RU", скопируй тексты дословно.
- Если язык НЕ русский: переведи оба поля на русский. URL, имена, числа оставь как есть.

name: {name}
descr: {descr}\
"""


def translate_if_needed(name: str, descr: str) -> tuple[str, str, str]:
    """Определяет язык и при необходимости переводит name и descr на русский.

    Возвращает (lang, name_ru, descr_ru).
    lang — код языка оригинала (RU если перевод не нужен или при ошибке).
    При ошибке — ('RU', name, descr), чтобы уведомление ушло без перевода.
    """
    try:
        response = groq_client.chat.completions.create(
            model='openai/gpt-oss-120b',
            messages=[{
                'role': 'user',
                'content': _PROMPT_TEMPLATE.format(name=name or '', descr=descr or ''),
            }],
            temperature=0,
            max_tokens=4096,
        )
        raw = response.choices[0].message.content.strip()
        # Снимаем markdown-обёртку если модель всё же добавила ```json ... ```
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        data = json.loads(raw)
        lang = str(data.get('lang', 'RU')).upper()
        name_ru = str(data.get('name_ru', name or ''))
        descr_ru = str(data.get('descr_ru', descr or ''))
        # Если модель ошибочно вернула lang=RU, но текст реально перевела —
        # доверяем переводу, а не коду языка.
        actually_translated = (name_ru != (name or '')) or (descr_ru != (descr or ''))
        if lang == 'RU' and actually_translated:
            lang = 'EN'  # консервативное предположение: скорее всего английский
        return lang, name_ru, descr_ru
    except Exception as e:
        print(f'[translator] fallback: {e}')
        return 'RU', name or '', descr or ''
