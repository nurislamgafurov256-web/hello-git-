import html
import json
import os
import re
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI()
DB = os.getenv('MOCK_DB', 'mock.sqlite3')
with sqlite3.connect(DB) as db:
    db.execute('CREATE TABLE IF NOT EXISTS messages (payload TEXT)')


@app.post('/bot{token}/sendMessage')
async def receive(token: str, request: Request):
    payload = await request.json()
    with sqlite3.connect(DB) as db:
        db.execute('INSERT INTO messages VALUES (?)', (json.dumps(payload, ensure_ascii=False),))
    return {'ok': True, 'result': {'message_id': 1}}


@app.get('/messages')
def messages():
    with sqlite3.connect(DB) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM messages')]


@app.get('/', response_class=HTMLResponse)
def index():
    bubbles = ''.join('<article><small>БОТ · НОВАЯ ЗАЯВКА</small><pre>' + html.escape(html.unescape(re.sub(r'</?(?:b|a)(?:\s[^>]*)?>', '', m['text']))) + '</pre><time>доставлено ✓✓</time></article>' for m in messages())
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Заявки · Telegram mock</title><style>body{margin:0;background:#d9e5dc;font:16px system-ui;color:#20362e}header{background:#fff;padding:25px 12%;box-shadow:0 2px 10px #0001}header b{font-size:22px}main{max-width:850px;margin:45px auto}article{background:#fff;padding:28px;border-radius:18px;margin:22px 0;box-shadow:0 3px 10px #0001}pre{white-space:pre-wrap;font:18px/1.9 system-ui}small,time{color:#69877b}time{display:block;text-align:right}</style><header><b>◉ Ворота / заявки</b><br>Тестовый Telegram · сообщения от бота</header><main>' + (bubbles or 'Пока нет сообщений') + '</main></html>'


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.getenv('PORT', '8091')))
