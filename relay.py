#!/usr/bin/env python3
"""
ReactorNet Relay Server
Хостится на Render/VPS с белым IP
Обеспечивает связь между хостами и посетителями

Важно:
- websocket-часть зависит от flask_sock
- если flask_sock не установлен, сервер всё равно поднимет HTTP endpoint'ы (/api/search*, /api/sites, /stats)
"""

import json
import logging
import time
from threading import Lock

from flask import Flask, request, jsonify

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# =========================================================
# Optional websocket support (flask_sock)
# =========================================================
try:
    from flask_sock import Sock  # type: ignore
    sock = Sock(app)
except ModuleNotFoundError:
    sock = None
    logger.warning(
        "flask_sock не установлен — websocket-режимы /host/* и /visit/* отключены. "
        "HTTP API endpoint'ы продолжат работать."
    )

# Хранилище активных хостов { site_id: websocket }
hosts = {}
hosts_lock = Lock()

# Хранилище ожидающих запросов { request_id: callback }
pending_requests = {}
pending_lock = Lock()
request_counter = 0


class ReactorNetRelay:
    @staticmethod
    def generate_request_id():
        global request_counter
        with pending_lock:
            request_counter += 1
            return f"req_{int(time.time()*1000)}_{request_counter}"


@app.route('/')
def index():
    """Приветственная страница"""
    return jsonify({
        'name': 'ReactorNet Relay',
        'version': '1.0.0',
        'status': 'online',
        'active_hosts': len(hosts),
        'docs': 'wss://relay/reactor?site=SITE_ID - для хостеров'
    })


@app.route('/stats')
def stats():
    """Статистика для мониторинга"""
    return jsonify({
        'active_hosts': len(hosts),
        'pending_requests': len(pending_requests),
        'hosts_list': list(hosts.keys())
    })


@app.route('/api/search/reactorGO')
def reactorgo_search():
    """
    Центральный поиск reactorGO по активным сайтам.
    Текущая реализация: простой матч по site_id (актуально пока нет индекса/метаданных содержимого).

    Возвращает:
      { "results": [ { "name": "...", "site": "http://X.reactor" }, ... ] }
    """
    query = (request.args.get('query') or '').strip()
    if not query:
        return jsonify({'results': []})

    q = query.lower()

    with hosts_lock:
        site_ids = list(hosts.keys())

    matches = []
    for s in site_ids:
        sid = str(s)
        sid_lower = sid.lower()

        score = 0
        if sid_lower == q:
            score = 100
        elif sid_lower.startswith(q):
            score = 70
        elif q in sid_lower:
            score = 40

        if score > 0:
            matches.append((score, sid))

    matches.sort(key=lambda x: x[0], reverse=True)

    results = []
    for _, sid in matches[:50]:
        results.append({
            'name': sid,
            'site': f"http://{sid}.reactor"
        })

    return jsonify({'results': results})


@app.route('/api/sites')
def list_sites():
    """API для получения списка активных сайтов"""
    with hosts_lock:
        sites = list(hosts.keys())
    return jsonify({'sites': [f"{s}.reactor" for s in sites]})


# =========================================================
# Websocket routes (only if flask_sock is available)
# =========================================================
if sock is not None:

    @sock.route('/host/<site_id>')
    def host_mode(ws, site_id):
        """
        Режим хостера - компьютер с сайтом
        site_id: уникальный идентификатор сайта (например, 'mysite')
        """
        with hosts_lock:
            if site_id in hosts:
                ws.send(json.dumps({
                    'type': 'error',
                    'message': f'Site ID "{site_id}" already taken'
                }))
                return

            hosts[site_id] = ws
            logger.info(f"✅ Host registered: {site_id}")

        try:
            ws.send(json.dumps({
                'type': 'registered',
                'site_id': site_id,
                'message': f'Your site "{site_id}.reactor" is now live!'
            }))

            while True:
                message = ws.receive()
                data = json.loads(message)

                if data.get('type') == 'response':
                    request_id = data.get('request_id')
                    logger.info(f"📥 Host '{site_id}' got response for {request_id}")

                    with pending_lock:
                        if request_id in pending_requests:
                            callback = pending_requests[request_id]
                            callback(data.get('response', {}))
                            del pending_requests[request_id]
                            logger.info(f"✅ Routed response to visitor for {request_id}")

                        else:
                            logger.warning(
                                f"⚠️ No pending visitor request for request_id={request_id} (host={site_id})"
                            )

                elif data.get('type') == 'ping':
                    ws.send(json.dumps({'type': 'pong'}))

        except Exception as e:
            logger.warning(f"⚠️ Host disconnected: {site_id} - {e}")
        finally:
            with hosts_lock:
                hosts.pop(site_id, None)
            logger.info(f"❌ Host unregistered: {site_id}")

    @sock.route('/visit/<site_id>')
    def visit_mode(ws, site_id):
        """
        Режим посетителя - кто-то хочет открыть сайт
        """
        logger.info(f"👤 Visitor connected for: {site_id}")

        try:
            while True:
                message = ws.receive()
                data = json.loads(message)

                if data.get('type') == 'request':
                    with hosts_lock:
                        if site_id not in hosts:
                            ws.send(json.dumps({
                                'type': 'error',
                                'message': f'Site "{site_id}.reactor" is offline'
                            }))
                            logger.warning(f"⚠️ Visitor request for offline site '{site_id}'")
                            continue
                        host_ws = hosts[site_id]

                    request_id = ReactorNetRelay.generate_request_id()
                    logger.info(
                        f"🚀 Forward request visitor->{site_id}.reactor request_id={request_id} url={data.get('url','/')}"
                    )

                    import threading
                    response_event = threading.Event()
                    response_data = {'html': '', 'headers': {}}

                    def set_response(response):
                        nonlocal response_data
                        response_data = response
                        response_event.set()

                    with pending_lock:
                        pending_requests[request_id] = set_response

                    host_ws.send(json.dumps({
                        'type': 'request',
                        'request_id': request_id,
                        'url': data.get('url', '/'),
                        'method': data.get('method', 'GET'),
                        'headers': data.get('headers', {}),
                        'body': data.get('body', '')
                    }))

                    timeout = 30
                    if response_event.wait(timeout):
                        logger.info(f"📤 Sending response back to visitor for {request_id}")
                        ws.send(json.dumps({
                            'type': 'response',
                            'html': response_data.get('html', ''),
                            'status': response_data.get('status', 200),
                            'headers': response_data.get('headers', {})
                        }))
                    else:
                        logger.warning(f"⏰ Timeout waiting host response for {request_id}")
                        ws.send(json.dumps({
                            'type': 'error',
                            'message': 'Request timeout (host took too long to respond)'
                        }))

        except Exception as e:
            # Нормальное закрытие соединения (например code 1000) — не считаем ошибкой.
            msg = str(e)
            if "Connection closed: 1000" in msg or "1000" in msg:
                logger.info(f"🔌 Visit disconnected: {msg}")
                return

            logger.error(f"❌ Visit error: {e}")
            try:
                ws.send(json.dumps({'type': 'error', 'message': str(e)}))
            except:
                pass


if __name__ == '__main__':
    # Render даёт порт через переменную окружения, по умолчанию 10000
    import os
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
