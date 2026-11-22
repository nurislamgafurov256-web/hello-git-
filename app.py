import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent


def create_app(db_path=None):
    app = FastAPI(debug=False, docs_url=None, redoc_url=None, openapi_url=None)
    secret = os.getenv('SECRET_KEY') or secrets.token_hex(32)
    database = str(db_path or os.getenv('LEADS_DB', ROOT / 'leads.sqlite3'))
    lock = threading.Lock()
    attempts = {}
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, door TEXT, comment TEXT, source TEXT, created REAL, status TEXT)')

    def signature(stamp):
        return hmac.new(secret.encode(), stamp.encode(), hashlib.sha256).hexdigest()

    @app.middleware('http')
    async def protect(request, call_next):
        body = b''
        async for chunk in request.stream():
            body += chunk
            if len(body) > 8192:
                return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
        request._body = body
        response = await call_next(request)
        response.headers.update({
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Cache-Control': 'no-store' if request.url.path.startswith('/api/') else 'no-cache',
        })
        return response

    @app.get('/api/form-token')
    def token():
        stamp = str(time.time())
        return {'token': f'{stamp}.{signature(stamp)}'}

    @app.post('/api/lead')
    async def lead(request: Request):
        def error(message, code=400):
            return JSONResponse({'detail': message}, status_code=code)
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            return error('Некорректный запрос')
        if not isinstance(data, dict):
            return error('Некорректный запрос')
        if data.get('website'):
            return {'ok': True}
        try:
            stamp, digest = data['token'].rsplit('.', 1)
            age = time.time() - float(stamp)
            if not hmac.compare_digest(signature(stamp), digest) or not 3 <= age <= 7200:
                return error('Обновите форму и подождите несколько секунд')
        except (KeyError, ValueError, TypeError, AttributeError):
            return error('Обновите форму и попробуйте ещё раз')
        name, phone, comment, door = (data.get(key, '') for key in ('name', 'phone', 'comment', 'door'))
        if not all(isinstance(v, str) for v in (name, phone, comment, door)):
            return error('Проверьте поля формы')
        name = name.strip()
        digits = re.sub(r'[^0-9]', '', phone)
        if len(digits) == 11 and digits[0] == '8':
            digits = '7' + digits[1:]
        if not re.fullmatch(r'7[0-9]{10}', digits) or re.search(r'[^0-9+()\s-]', phone):
            return error('Укажите российский номер телефона')
        phone = '+' + digits
        if not 2 <= len(name) <= 60 or len(comment) > 500 or door not in ('', 'Секционные', 'Рулонные', 'Распашные') or data.get('consent') is not True:
            return error('Проверьте имя, комментарий и согласие на обработку данных')
        now = time.time()
        # Only enable this behind a proxy that overwrites X-Forwarded-For.
        ip = request.headers.get('x-forwarded-for', '').split(',')[0].strip() if os.getenv('TRUST_PROXY') == '1' else ''
        ip = ip or (request.client.host if request.client else 'unknown')
        with lock, sqlite3.connect(database) as db:
            for key in list(attempts):
                attempts[key] = [t for t in attempts[key] if now - t < 600]
                if not attempts[key]:
                    del attempts[key]
            recent = attempts.setdefault(ip, [])
            if len(recent) >= 3:
                return error('Слишком много заявок. Попробуйте через 10 минут', 429)
            recent.append(now)
            if db.execute('SELECT 1 FROM leads WHERE phone=? AND created>?', (phone, now - 600)).fetchone():
                return error('Заявка с этим номером уже принята. Мы скоро позвоним', 409)
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
        return {'ok': True}

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/privacy')
    def privacy():
        return FileResponse(ROOT / 'static/privacy.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


app = create_app()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '8090')))
