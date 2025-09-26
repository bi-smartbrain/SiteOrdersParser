import requests


def get_tokens(username, password,
        url="https://rubrain.com/api/auth/login/?active_lang=ru"):

    response = requests.post(url, json={
        'email': username,
        'password': password
    })

    if response.status_code == 200:
        return response.json()  # Возвращает access_token и refresh_token
    else:
        raise Exception('Не удалось получить токены: {}'.format(response.text))
