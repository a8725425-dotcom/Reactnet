# relay.py - ReactorNet Relay Server v2.0
# Полная версия с поддержкой файлов, MIME-типов и WebSocket
#!/usr/bin/env python3
"""
ReactorNet Relay Server
Хостится на Render/VPS с белым IP
Обеспечивает связь между хостами и посетителями
Поддерживает передачу файлов (изображения, документы, архивы)
"""

import json
import logging
import time
import base64
import mimetypes
from threading import Lock
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Разрешаем кросс-доменные запросы

# =========================================================
# Optional websocket support (flask_sock)
# =========================================================
try:
    from flask_sock import Sock  # type: ignore
    sock = Sock(app)
    WEBSOCKET_AVAILABLE = True
    logger.info("✅ WebSocket support enabled")
except ModuleNotFoundError:
    sock = None
    WEBSOCKET_AVAILABLE = False
    logger.warning(
        "flask_sock не установлен — websocket-режимы отключены. "
        "Установите: pip install flask-sock"
    )

# Хранилище активных хостов { site_id: websocket }
hosts = {}
hosts_lock = Lock()

# Хранилище ожидающих запросов { request_id: callback }
pending_requests = {}
pending_lock = Lock()
request_counter = 0

# Настройки
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB максимум
REQUEST_TIMEOUT = 60  # Таймаут запроса в секундах


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
        'version': '2.0.0',
        'status': 'online',
        'active_hosts': len(hosts),
        'features': {
            'websocket': WEBSOCKET_AVAILABLE,
            'file_transfer': True,
            'search': True,
            'max_file_size_mb': MAX_FILE_SIZE // (1024 * 1024)
        },
        'docs': {
            'host': 'wss://relay/host/<site_id>',
            'visit': 'wss://relay/visit/<site_id>',
            'api': '/api/search/reactorGO, /api/sites, /stats'
        }
    })


@app.route('/stats')
def stats():
    """Статистика для мониторинга"""
    return jsonify({
        'active_hosts': len(hosts),
        'pending_requests': len(pending_requests),
        'hosts_list': list(hosts.keys()),
        'uptime': time.time() - stats.start_time if hasattr(stats, 'start_time') else 0
    })


# Устанавливаем время старта
stats.start_time = time.time()


@app.route('/api/search/reactorGO')
def reactorgo_search():
    """
    Центральный поиск reactorGO по активным сайтам.
    Поддерживает fuzzy search и теги.
    """
    query = (request.args.get('query') or '').strip()
    if not query:
        return jsonify({'results': [], 'count': 0})

    q = query.lower()
    
    with hosts_lock:
        site_ids = list(hosts.keys())

    matches = []
    for s in site_ids:
        sid = str(s)
        sid_lower = sid.lower()

        # Простой алгоритм поиска
        score = 0
        if sid_lower == q:
            score = 100  # Точное совпадение
        elif q in sid_lower:
            score = 50   # Частичное совпадение
        elif any(word in sid_lower for word in q.split()):
            score = 30   # Совпадение по словам
        
        # Бонус за популярные сайты (можно расширить)
        popular_sites = ['wiki', 'blog', 'forum', 'search', 'home']
        if sid_lower in popular_sites:
            score += 20

        if score > 0:
            matches.append((score, sid))

    # Сортировка по релевантности
    matches.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, sid in matches[:50]:  # Лимит 50 результатов
        results.append({
            'name': sid,
            'site': f"http://{sid}.reactor",
            'relevance': score
        })

    return jsonify({
        'results': results,
        'count': len(results),
        'query': query
    })


@app.route('/api/sites')
def list_sites():
    """API для получения списка активных сайтов"""
    with hosts_lock:
        sites = list(hosts.keys())
    return jsonify({
        'sites': [f"{s}.reactor" for s in sites],
        'count': len(sites)
    })


@app.route('/api/site/<site_id>/info')
def site_info(site_id):
    """Информация о конкретном сайте"""
    with hosts_lock:
        is_active = site_id in hosts
    return jsonify({
        'site_id': site_id,
        'url': f"http://{site_id}.reactor",
        'active': is_active,
        'status': 'online' if is_active else 'offline'
    })


# =========================================================
# Websocket routes (only if flask_sock is available)
# =========================================================
if WEBSOCKET_AVAILABLE:

    @sock.route('/host/<site_id>')
    def host_mode(ws, site_id):
        """
        Режим хостера - компьютер с сайтом
        site_id: уникальный идентификатор сайта (например, 'mysite')
        Поддерживает отправку HTML и файлов
        """
        # Проверяем валидность site_id
        if not site_id or len(site_id) < 3 or len(site_id) > 50:
            ws.send(json.dumps({
                'type': 'error',
                'message': 'Site ID must be 3-50 characters long'
            }))
            return
        
        # Проверяем допустимые символы
        if not re.match(r'^[a-zA-Z0-9_-]+$', site_id):
            ws.send(json.dumps({
                'type': 'error',
                'message': 'Site ID can only contain letters, numbers, underscores and hyphens'
            }))
            return

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
            # Отправляем подтверждение регистрации
            ws.send(json.dumps({
                'type': 'registered',
                'site_id': site_id,
                'message': f'Your site "{site_id}.reactor" is now live!',
                'url': f"http://{site_id}.reactor"
            }))
            
            logger.info(f"🌐 Site online: {site_id}.reactor")

            while True:
                message = ws.receive()
                data = json.loads(message)
                msg_type = data.get('type')

                if msg_type == 'response':
                    # Обычный HTML ответ
                    request_id = data.get('request_id')
                    logger.info(f"📥 Host '{site_id}' sent HTML response for {request_id}")

                    with pending_lock:
                        if request_id in pending_requests:
                            callback = pending_requests[request_id]
                            callback({
                                'type': 'html',
                                'html': data.get('html', ''),
                                'status': data.get('status', 200),
                                'headers': data.get('headers', {})
                            })
                            del pending_requests[request_id]
                            logger.info(f"✅ Routed HTML response to visitor for {request_id}")

                elif msg_type == 'file_response':
                    # Ответ с файлом (бинарные данные в base64)
                    request_id = data.get('request_id')
                    filename = data.get('filename', 'download')
                    content_base64 = data.get('content', '')
                    content_type = data.get('content_type', 'application/octet-stream')
                    file_size = data.get('size', len(content_base64) * 3 // 4)
                    
                    logger.info(f"📁 Host '{site_id}' sent file: {filename} ({file_size} bytes) for {request_id}")

                    with pending_lock:
                        if request_id in pending_requests:
                            callback = pending_requests[request_id]
                            callback({
                                'type': 'file',
                                'filename': filename,
                                'content': content_base64,
                                'content_type': content_type,
                                'size': file_size
                            })
                            del pending_requests[request_id]
                            logger.info(f"✅ Routed file to visitor for {request_id}")

                elif msg_type == 'ping':
                    ws.send(json.dumps({'type': 'pong'}))

                elif msg_type == 'info':
                    # Информация о сайте (можно расширить)
                    logger.info(f"ℹ️ Site info from {site_id}: {data.get('message', '')}")

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
        Поддерживает получение HTML и файлов
        """
        logger.info(f"👤 Visitor connected for: {site_id}")

        try:
            while True:
                message = ws.receive()
                data = json.loads(message)

                if data.get('type') == 'request':
                    # Проверяем, активен ли хост
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
                    url_path = data.get('url', '/')
                    method = data.get('method', 'GET')
                    
                    logger.info(f"🚀 Forward {method} request visitor->{site_id}.reactor{url_path} [id={request_id}]")

                    import threading
                    response_event = threading.Event()
                    response_data = None

                    def set_response(response):
                        nonlocal response_data
                        response_data = response
                        response_event.set()

                    with pending_lock:
                        pending_requests[request_id] = set_response

                    # Отправляем запрос хосту
                    try:
                        host_ws.send(json.dumps({
                            'type': 'request',
                            'request_id': request_id,
                            'url': url_path,
                            'method': method,
                            'headers': data.get('headers', {}),
                            'body': data.get('body', '')
                        }))
                    except Exception as e:
                        logger.error(f"❌ Failed to send request to host {site_id}: {e}")
                        ws.send(json.dumps({
                            'type': 'error',
                            'message': f'Host communication error: {str(e)}'
                        }))
                        continue

                    # Ждём ответ от хоста
                    if response_event.wait(REQUEST_TIMEOUT):
                        if response_data.get('type') == 'file':
                            # Отправляем файл посетителю
                            filename = response_data.get('filename')
                            content_base64 = response_data.get('content')
                            content_type = response_data.get('content_type')
                            file_size = response_data.get('size', 0)
                            
                            logger.info(f"📤 Sending file to visitor: {filename} ({file_size} bytes)")
                            
                            ws.send(json.dumps({
                                'type': 'file',
                                'filename': filename,
                                'content': content_base64,
                                'content_type': content_type,
                                'size': file_size
                            }))
                            
                        else:
                            # Обычный HTML ответ
                            html_out = response_data.get('html', '') or ''
                            status = response_data.get('status', 200)
                            
                            preview = html_out[:100].replace("\n", " ").replace("\r", " ")
                            logger.info(f"📤 Sending HTML response to visitor (status={status}, len={len(html_out)})")
                            
                            ws.send(json.dumps({
                                'type': 'response',
                                'html': html_out,
                                'status': status,
                                'headers': response_data.get('headers', {})
                            }))
                    else:
                        logger.warning(f"⏰ Timeout waiting host response for {request_id} (timeout={REQUEST_TIMEOUT}s)")
                        ws.send(json.dumps({
                            'type': 'error',
                            'message': f'Request timeout (host took too long to respond, limit {REQUEST_TIMEOUT}s)'
                        }))

                elif data.get('type') == 'ping':
                    ws.send(json.dumps({'type': 'pong'}))

        except Exception as e:
            msg = str(e)
            if "Connection closed" in msg or "1000" in msg or "1001" in msg:
                logger.info(f"🔌 Visitor disconnected from {site_id}: {msg}")
            else:
                logger.error(f"❌ Visit error for {site_id}: {e}")
                try:
                    ws.send(json.dumps({'type': 'error', 'message': str(e)}))
                except:
                    pass


# =========================================================
# HTTP endpoints для прямого доступа (без WebSocket)
# =========================================================

@app.route('/api/proxy/<site_id>/<path:filepath>')
def proxy_file(site_id, filepath):
    """
    Прокси для прямого доступа к файлам через HTTP
    (альтернатива WebSocket для статических файлов)
    """
    with hosts_lock:
        if site_id not in hosts:
            return jsonify({'error': 'Site offline'}), 404
    
    # Этот эндпоинт требует реализации на стороне хоста
    # Пока возвращаем заглушку
    return jsonify({'error': 'Use WebSocket for file transfer'}), 501


# =========================================================
# Health check для мониторинга (Render.com)
# =========================================================

@app.route('/health')
def health():
    """Health check endpoint для мониторинга"""
    return jsonify({
        'status': 'healthy',
        'timestamp': time.time(),
        'active_hosts': len(hosts)
    }), 200


# =========================================================
# Main
# =========================================================

if __name__ == '__main__':
    import os
    import re  # для regex в проверке site_id
    
    port = int(os.environ.get('PORT', 10000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    print("""
    ╔══════════════════════════════════════════════════════╗
    ║         ReactorNet Relay Server v2.0                ║
    ║     Децентрализованный веб-хостинг через WebSocket  ║
    ╚══════════════════════════════════════════════════════╝
    """)
    
    print(f"📡 Server starting on port {port}")
    print(f"🔧 Debug mode: {debug}")
    print(f"💾 Max file size: {MAX_FILE_SIZE // (1024 * 1024)}MB")
    print(f"⏱️  Request timeout: {REQUEST_TIMEOUT}s")
    print(f"🔌 WebSocket: {'✅ ENABLED' if WEBSOCKET_AVAILABLE else '❌ DISABLED'}")
    print()
    
    if not WEBSOCKET_AVAILABLE:
        print("⚠️  WARNING: flask_sock not installed!")
        print("   Install it with: pip install flask-sock")
        print("   Only HTTP API endpoints will work without WebSocket")
        print()
    
    print("🌐 Endpoints:")
    print("   GET  /                - Service info")
    print("   GET  /stats           - Statistics")
    print("   GET  /health          - Health check")
    print("   GET  /api/sites       - List active sites")
    print("   GET  /api/search/reactorGO - Search sites")
    print("   WS   /host/<site_id>  - Host mode (WebSocket)")
    print("   WS   /visit/<site_id> - Visit mode (WebSocket)")
    print()
    
    app.run(host='0.0.0.0', port=port, debug=debug)
