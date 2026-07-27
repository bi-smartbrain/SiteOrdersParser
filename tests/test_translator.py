import json
from unittest.mock import patch, MagicMock
import pytest
from translator import translate_if_needed


def _mock_groq_response(content: str):
    """Хелпер: создаёт mock-ответ Groq с нужным content."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_translate_english():
    payload = json.dumps({"lang": "EN", "name_ru": "Дизайн сайта", "descr_ru": "Нужен дизайнер"})
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.return_value = _mock_groq_response(payload)
        lang, name_ru, descr_ru = translate_if_needed("Website design", "Need a designer")
    assert lang == "EN"
    assert name_ru == "Дизайн сайта"
    assert descr_ru == "Нужен дизайнер"


def test_russian_passthrough():
    payload = json.dumps({"lang": "RU", "name_ru": "Дизайн сайта", "descr_ru": "Нужен дизайнер"})
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.return_value = _mock_groq_response(payload)
        lang, name_ru, descr_ru = translate_if_needed("Дизайн сайта", "Нужен дизайнер")
    assert lang == "RU"
    assert name_ru == "Дизайн сайта"
    assert descr_ru == "Нужен дизайнер"


def test_fallback_on_invalid_json():
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.return_value = _mock_groq_response("not json at all")
        lang, name_ru, descr_ru = translate_if_needed("Test", "Test descr")
    assert lang == "RU"
    assert name_ru == "Test"
    assert descr_ru == "Test descr"


def test_fallback_on_groq_exception():
    with patch('translator.groq_client') as mock_client:
        mock_client.chat.completions.create.side_effect = Exception("connection error")
        lang, name_ru, descr_ru = translate_if_needed("Test", "Test descr")
    assert lang == "RU"
    assert name_ru == "Test"
    assert descr_ru == "Test descr"
