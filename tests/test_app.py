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


@pytest.fixture
def proxied_site(tmp_path, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'test-secret')
    monkeypatch.setenv('TRUST_PROXY', '1')
    monkeypatch.setenv('TRUSTED_PROXY_IPS', '192.0.2.12/32')
    path = tmp_path / 'leads.sqlite3'
    with TestClient(create_app(path), client=('192.0.2.12', 50000)) as client:
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
    assert 'aria-invalid="true" aria-describedby="lead-phone-error lead-phone-hint" autofocus' in invalid.text
    assert 'id="lead-phone-error" >Укажите российский номер телефона' in invalid.text
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


def test_native_comment_counts_crlf_as_one_character(site, telegram):
    client, db_path = site
    comment = 'а' * 249 + '\r\n' + 'б' * 250
    data = {**payload(), 'comment': comment, 'consent': 'on'}

    too_long = client.post('/api/lead', data={**data, 'comment': comment + '!'})
    assert too_long.status_code == 400
    assert 'Сократите комментарий до 500 символов.' in too_long.text

    success = client.post('/api/lead', data=data, follow_redirects=False)
    assert success.status_code == 303
    assert success.headers['location'] == '/?lead=accepted#lead'
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT comment FROM leads').fetchall() == [(comment.replace('\r\n', '\n'),)]
    telegram.assert_called_once()


def test_rate_limit(site, telegram):
    for i in range(4):
        data = payload()
        data['phone'] = f'+7999123456{i}'
        assert site[0].post('/api/lead', json=data, headers={'X-Forwarded-For': f'1.1.1.{i}'}).status_code == (200 if i < 3 else 429)


def test_forwarded_for_spoof_does_not_bypass_limit(site, telegram, monkeypatch):
    monkeypatch.setenv('TRUST_PROXY', '1')
    for i in range(4):
        data = payload()
        data['phone'] = f'+79991234{i:03d}'
        assert site[0].post('/api/lead', json=data, headers={'X-Forwarded-For': f'198.51.100.{i}'}).status_code == (200 if i < 3 else 429)


def test_form_token_rate_limit(site, monkeypatch):
    monkeypatch.setenv('TRUST_PROXY', '1')
    responses = [site[0].get('/api/form-token') for _ in range(31)]
    assert [response.status_code for response in responses[:30]] == [200] * 30
    assert responses[-1].status_code == 429


@pytest.mark.parametrize('chain,same_ip,other_ip', [
    (['8.8.8.8, 1.1.1.1'], ['8.8.4.4, 1.1.1.1'], ['8.8.8.8, 1.0.0.1']),
    (['1.1.1.1, 8.8.8.8, 10.0.0.1'], ['1.1.1.1'], ['8.8.8.8, 10.0.0.1']),
    (['8.8.8.8', 'unknown, ::ffff:1.1.1.1'], ['1.1.1.1'], ['1.0.0.1']),
    (['10.0.0.1'], [], ['1.1.1.1']),
])
def test_configured_proxy_preserves_separate_client_limits(proxied_site, chain, same_ip, other_ip):
    client, _ = proxied_site

    def issue_token(values):
        headers = [('X-Forwarded-For', value) for value in values]
        return client.get('/api/form-token', headers=headers).status_code

    assert all(issue_token(chain) == 200 for _ in range(30))
    assert issue_token(same_ip) == 429
    assert issue_token(other_ip) == 200


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
    assert site[0].post('/api/lead', json=data).status_code == 429
    assert telegram.call_count == 3


@pytest.mark.parametrize('native', [False, True])
def test_quota_does_not_disclose_recent_leads(proxied_site, telegram, native):
    client, _ = proxied_site
    other_ip = {'X-Forwarded-For': '1.1.1.1'}
    visitor_ip = {'X-Forwarded-For': '1.0.0.1'}
    recent_phone = '9991234560'
    unused_phone = '9991234564'
    assert client.post('/api/lead', json={**payload(), 'phone': recent_phone}, headers=other_ip).status_code == 200
    for i in (1, 2, 3):
        assert client.post('/api/lead', json={**payload(), 'phone': f'999123456{i}'}, headers=visitor_ip).status_code == 200
    probe = payload()

    def try_phone(phone):
        data = {**probe, 'phone': phone}
        fields = {'data': {**data, 'consent': 'on'}} if native else {'json': data}
        return client.post('/api/lead', **fields, headers=visitor_ip, follow_redirects=False)

    recent, unused = try_phone(recent_phone), try_phone(unused_phone)
    assert recent.status_code == unused.status_code == 429
    assert recent.headers.get('location') == unused.headers.get('location') is None
    if native:
        assert recent.text.replace(recent_phone, unused_phone) == unused.text
    else:
        assert recent.json() == unused.json()
    assert telegram.call_count == 4


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
    assert site[0].post('/api/lead', content='x'*8193).headers['x-content-type-options'] == 'nosniff'
    response = site[0].get('/')
    assert response.status_code == 200
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['permissions-policy'] == 'camera=(), microphone=(), geolocation=()'
    assert response.headers['strict-transport-security'] == 'max-age=31536000'
    assert 'unsafe-inline' not in response.headers['content-security-policy']
    assert site[0].post('/api/lead', content='[').status_code == 400
    assert site[0].post('/api/lead', json=[]).status_code == 400


def test_oversized_native_post_returns_a_retryable_form(site, telegram):
    client, _ = site
    oversized = client.post('/api/lead', data={**payload(), 'comment': '😀' * 1500, 'consent': 'on'})
    assert oversized.status_code == 413
    assert oversized.headers['content-type'].startswith('text/html')
    assert oversized.headers['cache-control'] == 'no-store'
    assert oversized.headers['x-frame-options'] == 'DENY'
    assert 'Заявка слишком большая.' in oversized.text
    assert 'method="post" action="/api/lead#lead"' in oversized.text
    form_token = re.search(r'name="token" value="([^"]+)"', oversized.text)[1]
    telegram.assert_not_called()

    stamp = float(form_token.rsplit('.', 1)[0])
    with patch('app.time.time', return_value=stamp + 4):
        retry = client.post('/api/lead', data={**payload(), 'consent': 'on', 'token': form_token}, follow_redirects=False)
    assert retry.status_code == 303
    assert retry.headers['location'] == '/?lead=accepted#lead'
    telegram.assert_called_once()


@pytest.mark.parametrize('field,value', [('name', 'A\x00B'), ('name', 'A\r\nB'), ('comment', 'A\x00B'), ('comment', 'A\r\nB'), ('comment', 'A\u2028B')])
def test_control_characters_rejected(site, telegram, field, value):
    data = payload()
    data[field] = value
    assert site[0].post('/api/lead', json=data).status_code == 400
    telegram.assert_not_called()


def test_native_form_uses_post(site):
    assert '<form id="lead-form" method="post" action="/api/lead#lead"' in site[0].get('/').text


def test_lead_requires_json_content_type(site):
    response = site[0].post('/api/lead', content='{}', headers={'Content-Type': 'text/plain'})
    assert response.status_code == 415


def test_json_parser_recursion_error_is_bad_request(site):
    with patch('app.json') as parser:
        parser.loads.side_effect = RecursionError
        response = site[0].post('/api/lead', content='{}', headers={'Content-Type': 'application/json'})
    assert response.status_code == 400


@pytest.mark.parametrize('path', ['/privacy', '/static/style.css', '/static/app.js'])
def test_page_assets_negotiate_compression(site, path):
    client = site[0]
    plain = client.get(path, headers={'Accept-Encoding': 'identity'})
    compressed = client.get(path, headers={'Accept-Encoding': 'gzip'})
    assert plain.status_code == compressed.status_code == 200
    assert 'content-encoding' not in plain.headers
    assert compressed.headers['content-encoding'] == 'gzip'
    assert compressed.headers['vary'] == 'Accept-Encoding'
    assert int(compressed.headers['content-length']) < int(plain.headers['content-length'])
    assert compressed.content == plain.content


def test_index_compresses_without_changing_cache_or_security_headers(site):
    response = site[0].get('/', headers={'Accept-Encoding': 'gzip'})
    assert response.status_code == 200
    assert response.headers['content-encoding'] == 'gzip'
    assert response.headers['vary'] == 'Accept-Encoding'
    assert response.headers['cache-control'] == 'no-store'
    assert 'unsafe-inline' not in response.headers['content-security-policy']
    assert 'name="token" value="' in response.text


def test_form_token_is_uncompressed_and_not_cached(site):
    response = site[0].get('/api/form-token', headers={'Accept-Encoding': 'gzip'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'content-encoding' not in response.headers
