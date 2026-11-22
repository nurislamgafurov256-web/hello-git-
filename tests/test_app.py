import hashlib
import hmac
import re
import runpy
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from app import create_app


def test_entrypoint_disables_proxy_rewriting(tmp_path, monkeypatch):
    monkeypatch.setenv('LEADS_DB', str(tmp_path / 'entrypoint.sqlite3'))
    with patch('uvicorn.run') as run:
        runpy.run_module('app', run_name='__main__')
    assert run.call_args.kwargs['proxy_headers'] is False


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'test-secret')
    path = tmp_path / 'leads.sqlite3'
    with TestClient(create_app(path)) as client:
        yield client, path


def token(age=4):
    stamp = str(time.time() - age)
    return stamp + '.' + hmac.new(b'test-secret', stamp.encode(), hashlib.sha256).hexdigest()


def payload():
    return dict(name='Анна <b>', phone='+7 (999) 123-45-67', comment='<script>&', door='Секционные', consent=True, token=token())


@pytest.fixture
def telegram():
    with patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=httpx.Response(200, json={'ok': True})) as mock:
        yield mock


def test_valid(site, telegram):
    assert site[0].post('/api/lead', json=payload()).json() == {'ok': True}
    text = telegram.call_args.kwargs['json']['text']
    assert 'Анна &lt;b&gt;' in text and '&lt;script&gt;&amp;' in text
    assert 'tel:+79991234567' in text and '(МСК)' in text
    assert telegram.call_args.kwargs['json']['parse_mode'] == 'HTML'
    with sqlite3.connect(site[1]) as db:
        assert db.execute('SELECT status FROM leads').fetchone() == ('sent',)


def test_honeypot(site, telegram):
    assert site[0].post('/api/lead', json={'website': 'spam'}).json() == {'ok': True}
    telegram.assert_not_called()
    with sqlite3.connect(site[1]) as db:
        assert db.execute('SELECT count(*) FROM leads').fetchone()[0] == 0


@pytest.mark.parametrize('value', [token(0), token(7201), 'bad', None, token(-10)])
def test_token_rejected(site, telegram, value):
    data = payload()
    data['token'] = value
    assert site[0].post('/api/lead', json=data).status_code == 400
    telegram.assert_not_called()


def test_real_token_too_fast(site):
    data = payload()
    data['token'] = site[0].get('/api/form-token').json()['token']
    assert site[0].post('/api/lead', json=data).status_code == 400


@pytest.mark.parametrize('field,value', [('phone', '123'), ('phone', '+1 999 123 4567'), ('name', ''), ('name', 'А'), ('comment', 'x'*501), ('consent', False), ('door', 'unknown')])
def test_validation(site, telegram, field, value):
    data = payload()
    data[field] = value
    assert site[0].post('/api/lead', json=data).status_code == 400
    telegram.assert_not_called()


def test_optional_empty(site, telegram):
    data = payload()
    data.update(comment='', door='')
    assert site[0].post('/api/lead', json=data).status_code == 200


@pytest.mark.parametrize('phone,normalized', [
    ('+7 (999) 123-45-67', '+79991234567'),
    ('8 999 123 45 67', '+79991234567'),
    ('89991234567', '+79991234567'),
    ('79991234567', '+79991234567'),
    ('9991234567', '+79991234567'),
    ('8123456789', '+78123456789'),
    ('+7-999-123-45-67', '+79991234567'),
    (' 8(495) 123.45.67 ', '+74951234567'),
    ('+7\u00a0(999)\u202f123\u00a045\u00a067', '+79991234567'),
    ('Телефон: +7 (999) 123-45-67', '+79991234567'),
])
def test_russian_phone_formats(site, telegram, phone, normalized):
    data = payload()
    data['phone'] = phone
    assert site[0].post('/api/lead', json=data).status_code == 200
    with sqlite3.connect(site[1]) as db:
        assert db.execute('SELECT phone FROM leads').fetchone() == (normalized,)


@pytest.mark.parametrize('phone', ['+8 999 123 45 67', '+9991234567', '8 999 123 45 67 доб. 2',
                                  '8 999 123 45 67 / 8 495 123 45 67', '79991234+567'])
def test_phone_errors_do_not_silently_change_a_number(site, telegram, phone):
    response = site[0].post('/api/lead', json={**payload(), 'phone': phone})
    assert response.status_code == 400
    assert response.json()['field'] == 'phone'
    telegram.assert_not_called()


@pytest.mark.parametrize('name', ['  Алёна д’Арк-Смирнова 😀  ', "Zoë O'Neill", 'Ё' * 249 + '😀'])
def test_unusual_names_are_kept(site, telegram, name):
    assert site[0].post('/api/lead', json={**payload(), 'name': name}).status_code == 200
    with sqlite3.connect(site[1]) as db:
        assert db.execute('SELECT name FROM leads').fetchone() == (name.strip(),)


@pytest.mark.parametrize('changes,field,message', [
    ({'name': '', 'phone': '', 'consent': False}, 'name', 'Укажите ваше имя.'),
    ({'name': ' \u00a0 '}, 'name', 'Укажите ваше имя.'),
    ({'name': 'ё' * 251}, 'name', 'Введите имя от 2 до 250 символов.'),
    ({'phone': ''}, 'phone', 'Укажите российский номер телефона'),
    ({'consent': False}, 'consent', 'Подтвердите согласие'),
])
def test_field_specific_errors(site, telegram, changes, field, message):
    response = site[0].post('/api/lead', json={**payload(), **changes})
    assert response.status_code == 400
    assert response.json()['field'] == field
    assert response.json()['detail'].startswith(message)
    telegram.assert_not_called()


def test_fresh_and_expired_tokens_can_be_retried_without_reloading(site, telegram):
    client = site[0]
    issued = client.get('/api/form-token').json()['token']
    early = client.post('/api/lead', json={**payload(), 'token': issued})
    assert early.status_code == 400
    assert early.json()['retry_token'] == issued
    expired = client.post('/api/lead', json={**payload(), 'token': token(7201)})
    assert expired.status_code == 400
    refreshed = expired.json()['retry_token']
    assert refreshed != issued
    stamp = float(refreshed.rsplit('.', 1)[0])
    with patch('app.time.time', return_value=stamp + 4):
        assert client.post('/api/lead', json={**payload(), 'token': refreshed}).json() == {'ok': True}
    telegram.assert_called_once()


def test_native_form_preserves_fields_and_redirects_after_success(site, telegram):
    client = site[0]
    page = client.get('/')
    assert 'method="post" action="/api/lead#lead"' in page.text
    form_token = re.search(r'name="token" value="([^"]+)"', page.text)[1]
    data = {**payload(), 'phone': 'неполный номер', 'consent': 'on', 'token': form_token}
    invalid = client.post('/api/lead', data=data)
    assert invalid.status_code == 400
    assert 'value="Анна &lt;b&gt;"' in invalid.text
    assert 'value="неполный номер"' in invalid.text
    assert '&lt;script&gt;&amp;</textarea>' in invalid.text
    assert '<option selected>Секционные' in invalid.text
    assert 'name="consent" required checked' in invalid.text
    assert 'aria-invalid="true" aria-describedby="form-status" autofocus' in invalid.text
    assert form_token in invalid.text
    telegram.assert_not_called()

    data['phone'] = '9991234567'
    stamp = float(form_token.rsplit('.', 1)[0])
    with patch('app.time.time', return_value=stamp + 4):
        success = client.post('/api/lead', data=data, follow_redirects=False)
        assert success.status_code == 303
        assert success.headers['location'] == '/?lead=accepted#lead'
        assert 'Заявка принята.' in client.get(success.headers['location']).text
        duplicate = client.post('/api/lead', data=data, follow_redirects=False)
        assert duplicate.status_code == 303
        assert duplicate.headers['location'] == '/?lead=duplicate#lead'
        assert 'заявка с этим номером уже принята' in client.get(duplicate.headers['location']).text.lower()
    telegram.assert_called_once()


def test_native_encoding_of_maximum_length_unicode_fields(site, telegram):
    data = {**payload(), 'name': '😀' * 250, 'comment': '😀' * 500, 'consent': 'on'}
    assert site[0].post('/api/lead', data=data, follow_redirects=False).status_code == 303
    telegram.assert_called_once()


def test_rate_limit(site, telegram):
    for i in range(4):
        data = payload()
        data['phone'] = f'+7999123456{i}'
        assert site[0].post('/api/lead', json=data, headers={'X-Forwarded-For': f'1.1.1.{i}'}).status_code == (200 if i < 3 else 429)


def test_dedupe(site, telegram):
    assert site[0].post('/api/lead', json=payload()).status_code == 200
    data = payload()
    data['phone'] = '89991234567'
    assert site[0].post('/api/lead', json=data).status_code == 409
    assert telegram.call_count == 1
    for _ in range(3):
        assert site[0].post('/api/lead', json=data).status_code == 409
    for phone in ('9991234568', '9991234569'):
        assert site[0].post('/api/lead', json={**data, 'phone': phone}).status_code == 200
    assert site[0].post('/api/lead', json=data).status_code == 409
    assert telegram.call_count == 3


@pytest.mark.parametrize('failure', ['network', 'api'])
def test_delivery_failure_stored(site, telegram, failure):
    if failure == 'network':
        telegram.side_effect = httpx.ConnectError('offline')
    else:
        telegram.return_value = httpx.Response(200, json={'ok': False})
    assert site[0].post('/api/lead', json=payload()).json() == {'ok': True}
    with sqlite3.connect(site[1]) as db:
        assert db.execute('SELECT phone,status FROM leads').fetchone() == ('+79991234567', 'failed')


def test_size_and_headers(site):
    assert site[0].post('/api/lead', content='x'*8193).status_code == 413
    response = site[0].get('/')
    assert response.status_code == 200
    assert response.headers['x-frame-options'] == 'DENY'
    assert 'unsafe-inline' not in response.headers['content-security-policy']
    assert site[0].post('/api/lead', content='[').status_code == 400
    assert site[0].post('/api/lead', json=[]).status_code == 400
