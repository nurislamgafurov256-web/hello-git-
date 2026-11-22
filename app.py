import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from string import Template
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DOORS = ('Секционные', 'Рулонные', 'Распашные')
ACCEPTED = 'Спасибо! Заявка принята. Специалист скоро вам позвонит.'
DUPLICATE = 'Заявка с этим номером уже принята. Мы скоро позвоним.'
FORM_CONTENT_TYPE = 'application/x-www-form-urlencoded'


def normalize_phone(value):
    value = re.sub(r'^(?:тел\.?|телефон|tel|phone)\s*:\s*', '', value.strip(), flags=re.I)
    if not re.fullmatch(r'\+?[0-9().\s-]+', value):
        return None
    digits = re.sub(r'[^0-9]', '', value)
    if value.startswith('+'):
        return '+' + digits if re.fullmatch(r'7[0-9]{10}', digits) else None
    if len(digits) == 10:
        return '+7' + digits
    if re.fullmatch(r'[78][0-9]{10}', digits):
        return '+7' + digits[1:]


def create_app(db_path=None):
    app = FastAPI(debug=False, docs_url=None, redoc_url=None, openapi_url=None)
    secret = os.getenv('SECRET_KEY') or secrets.token_hex(32)
    database = str(db_path or os.getenv('LEADS_DB', ROOT / 'leads.sqlite3'))
    lock = threading.Lock()
    attempts = {}
    token_attempts = {}
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, door TEXT, comment TEXT, source TEXT, created REAL, status TEXT)')

    def signature(stamp):
        return hmac.new(secret.encode(), stamp.encode(), hashlib.sha256).hexdigest()

    def new_token():
        stamp = str(time.time())
        return f'{stamp}.{signature(stamp)}'

    def render_form(values=None, message='', code=200, field='', form_token=None):
        values = values or {}
        fields = {key: html.escape(values.get(key, '')) for key in ('name', 'phone', 'comment')}
        fields.update({f'{key}_error': 'aria-invalid="true" aria-describedby="form-status" autofocus' if key == field else ''
                       for key in ('name', 'phone', 'door', 'comment', 'consent')})
        fields.update({f'door_{index}': 'selected' if values.get('door') == door else ''
                       for index, door in enumerate(DOORS, 1)})
        page = Template((ROOT / 'index.html').read_text(encoding='utf-8'))
        return HTMLResponse(page.substitute(
            fields, message=html.escape(message), form_token=html.escape(form_token or new_token()),
            consent='checked' if values.get('consent') is True else '',
        ), status_code=code)

    def is_form(request):
        return request.headers.get('content-type', '').split(';', 1)[0].strip().lower() == FORM_CONTENT_TYPE

    def client_ip(request):
        direct = request.client.host if request.client else 'unknown'
        if os.getenv('TRUST_PROXY') != '1':
            return direct
        try:
            peer = ipaddress.ip_address(direct)
            configured = os.getenv('TRUSTED_PROXY_IPS', '127.0.0.1/32,::1/128').split(',')
            networks = [ipaddress.ip_network(network.strip()) for network in configured if network.strip()]
        except ValueError:
            return direct
        if not any(peer in network for network in networks):
            return direct

        addresses = []
        for header in request.headers.getlist('x-forwarded-for'):
            for value in header.split(','):
                try:
                    address = ipaddress.ip_address(value.strip())
                except ValueError:
                    continue
                if getattr(address, 'scope_id', None) is None:
                    addresses.append(getattr(address, 'ipv4_mapped', None) or address)
        if addresses and addresses[-1].is_global:
            return str(addresses[-1])
        return next((str(address) for address in addresses if address.is_global), direct)

    def recent_attempts(buckets, ip, now):
        for key in list(buckets):
            buckets[key] = [stamp for stamp in buckets[key] if now - stamp < 600]
            if not buckets[key]:
                del buckets[key]
        return buckets.setdefault(ip, [])

    def has_disallowed_controls(value, allowed=''):
        return any(unicodedata.category(char) in ('Cc', 'Cs', 'Zl', 'Zp') and char not in allowed for char in value)

    @app.middleware('http')
    async def protect(request, call_next):
        native = is_form(request)
        limit = 12288 if native else 8192
        body = b''
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                if native and request.method == 'POST' and request.url.path == '/api/lead':
                    response = render_form(message='Заявка слишком большая. Заполните форму ещё раз и сократите длинные поля.', code=413)
                else:
                    response = JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
                break
            body += chunk
        else:
            request._body = body
            response = await call_next(request)
        response.headers.update({
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Strict-Transport-Security': 'max-age=31536000',
            'Cache-Control': 'no-store' if request.url.path.startswith('/api/') else 'no-cache',
        })
        return response

    @app.get('/api/form-token')
    def token(request: Request):
        with lock:
            recent = recent_attempts(token_attempts, client_ip(request), time.time())
            if len(recent) >= 30:
                return JSONResponse({'detail': 'Слишком много запросов. Попробуйте через 10 минут'}, status_code=429)
            recent.append(time.time())
        return {'token': new_token()}

    @app.post('/api/lead')
    async def lead(request: Request):
        native = is_form(request)
        data = {}

        def error(message, code=400, *, field='', retry_token=None, form_token=None):
            if native:
                return render_form(data, message, code, field, retry_token or form_token)
            result = {'detail': message}
            if field:
                result['field'] = field
            if retry_token:
                result['retry_token'] = retry_token
            return JSONResponse(result, status_code=code)

        def accepted():
            if native:
                return RedirectResponse('/?lead=accepted#lead', status_code=303)
            return {'ok': True}

        content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if not content_type:
            return error('Некорректный запрос')
        if not native and content_type != 'application/json':
            return error('Ожидается запрос в формате JSON', 415)
        try:
            if native:
                values = parse_qs((await request.body()).decode(), keep_blank_values=True)
                data = {key: value[-1] for key, value in values.items()}
                data['consent'] = data.get('consent') == 'on'
            else:
                data = json.loads((await request.body()).decode('utf-8'))
        except (ValueError, RecursionError):
            return error('Некорректный запрос')
        if not isinstance(data, dict):
            return error('Некорректный запрос')
        if data.get('website'):
            return accepted()
        name, phone, comment, door = (data.get(key, '') for key in ('name', 'phone', 'comment', 'door'))
        if not all(isinstance(v, str) for v in (name, phone, comment, door)):
            return error('Проверьте поля формы')
        if native:
            comment = comment.replace('\r\n', '\n')
        if any(has_disallowed_controls(value) for value in (name, phone, door)) or has_disallowed_controls(comment, '\t\n'):
            return error('Проверьте поля формы')
        name = name.strip()
        if not name:
            return error('Укажите ваше имя.', field='name', form_token=data.get('token'))
        if not 2 <= len(name) <= 250:
            return error('Введите имя от 2 до 250 символов.', field='name', form_token=data.get('token'))
        normalized = normalize_phone(phone)
        if not normalized:
            return error('Укажите российский номер телефона, например +7 (999) 123-45-67.', field='phone', form_token=data.get('token'))
        phone = normalized
        if len(comment) > 500:
            return error('Сократите комментарий до 500 символов.', field='comment', form_token=data.get('token'))
        if door and door not in DOORS:
            return error('Выберите тип ворот из списка.', field='door', form_token=data.get('token'))
        if data.get('consent') is not True:
            return error('Подтвердите согласие на обработку персональных данных.', field='consent', form_token=data.get('token'))
        try:
            stamp, digest = data['token'].rsplit('.', 1)
            age = time.time() - float(stamp)
            if not hmac.compare_digest(signature(stamp), digest) or not 0 <= age <= 7200:
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            return error('Срок действия формы истёк. Подождите несколько секунд и отправьте заявку ещё раз.', retry_token=new_token())
        if age < 3:
            return error('Подождите несколько секунд и отправьте заявку ещё раз.', retry_token=data['token'])
        now = time.time()
        ip = client_ip(request)
        with lock, sqlite3.connect(database) as db:
            recent = recent_attempts(attempts, ip, now)
            if len(recent) >= 3:
                return error('Слишком много заявок. Попробуйте через 10 минут.', 429, form_token=data['token'])
            if db.execute('SELECT 1 FROM leads WHERE phone=? AND created>?', (phone, now - 600)).fetchone():
                if native:
                    return RedirectResponse('/?lead=duplicate#lead', status_code=303)
                return error(DUPLICATE, 409)
            recent.append(now)
            source = '/'
            cursor = db.execute('INSERT INTO leads(name,phone,door,comment,source,created,status) VALUES(?,?,?,?,?,?,?)', (name, phone, door, comment, source, now, 'pending'))
            lead_id = cursor.lastrowid
        esc = html.escape
        message = (f'🔔 <b>Новая заявка</b>\nИмя: {esc(name)}\n'
                   f'Телефон: <a href="tel:{phone}">{phone}</a>\nТип ворот: {esc(door or "Не выбран")}\n'
                   f'Комментарий: {esc(comment or "—")}\n'
                   f'Время: {datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")} (МСК)\nИсточник: {source}')
        status = 'failed'
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(f'{os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")}/bot{os.getenv("TELEGRAM_BOT_TOKEN", "")}/sendMessage', json={'chat_id': os.getenv('TELEGRAM_CHAT_ID', ''), 'text': message, 'parse_mode': 'HTML'})
                if response.is_success and response.json().get('ok') is True:
                    status = 'sent'
        except (httpx.HTTPError, ValueError):
            pass
        with sqlite3.connect(database) as db:
            db.execute('UPDATE leads SET status=? WHERE id=?', (status, lead_id))
        return accepted()

    @app.get('/')
    def index(lead: str = ''):
        return render_form(message={'accepted': ACCEPTED, 'duplicate': DUPLICATE}.get(lead, ''))

    @app.get('/privacy')
    def privacy():
        return FileResponse(ROOT / 'static/privacy.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


app = create_app()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '8090')), proxy_headers=False)
