import os
import json
from groq import Groq

# 'dummy' fallback allows module import and test patching when key is absent;
# in production the key is always present via /opt/secrets/.env.
groq_client = Groq(api_key=os.getenv('GROQ_API_KEY') or 'dummy')

_PROMPT_TEMPLATE = """\
Определи язык текста и переведи на русский.
Верни только JSON без пояснений:
{{"lang": "RU|EN|KZ|UZ|...", "name_ru": "...", "descr_ru": "..."}}
Если язык уже русский — верни исходные тексты без изменений, lang="RU".

name: {name}
descr: {descr}\
"""


def translate_if_needed(name: str, descr: str) -> tuple[str, str, str]:
    """Определяет язык и при необходимости переводит name и descr на русский.

    Возвращает (lang, name_ru, descr_ru).
    При ошибке — ('RU', name, descr), чтобы уведомление ушло без перевода.
    """
    try:
        response = groq_client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[{
                'role': 'user',
                'content': _PROMPT_TEMPLATE.format(name=name or '', descr=descr or ''),
            }],
            temperature=0,
            max_tokens=1024,
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        lang = str(data.get('lang', 'RU')).upper()
        name_ru = str(data.get('name_ru', name or ''))
        descr_ru = str(data.get('descr_ru', descr or ''))
        return lang, name_ru, descr_ru
    except Exception as e:
        print(f'[translator] fallback: {e}')
        return 'RU', name or '', descr or ''
