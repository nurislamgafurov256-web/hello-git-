import hashlib
import hmac
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
