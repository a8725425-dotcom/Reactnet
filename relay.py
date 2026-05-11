#!/usr/bin/env python3
"""
ReactorNet Relay Server
Хостится на Render/VPS с белым IP
Обеспечивает связь между хостами и посетителями
"""

import json
import logging
import time
from threading import Lock
from flask import Flask, request, jsonify
from flask_sock import Sock

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
sock = Sock(app)

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

@sock.route('/host/<site_id>')
def host_mode(ws, site_id):
    """
    Режим хостера - компьютер с сайтом
    site_id: уникальный идентификатор сайта (например, 'mysite')
    """
    # Проверяем, не занято ли имя
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
        # Отправляем подтверждение хостеру
        ws.send(json.dumps({
            'type': 'registered',
            'site_id': site_id,
            'message': f'Your site "{site_id}.reactor" is now live!'
        }))
        
        while True:
            # Ждём сообщения от хостера
            message = ws.receive()
            data = json.loads(message)
            
            if data['type'] == 'response':
                # Ответ для посетителя
                request_id = data['request_id']
                with pending_lock:
                    if request_id in pending_requests:
                        callback = pending_requests[request_id]
                        callback(data['response'])
                        del pending_requests[request_id]
                        logger.debug(f"📤 Response sent for {request_id}")
            
            elif data['type'] == 'ping':
                # Keep-alive
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
            # Получаем запрос от браузера
            message = ws.receive()
            data = json.loads(message)
            
            if data['type'] == 'request':
                # Ищем хостера
                with hosts_lock:
                    if site_id not in hosts:
                        ws.send(json.dumps({
                            'type': 'error',
                            'message': f'Site "{site_id}.reactor" is offline'
                        }))
                        continue
                    host_ws = hosts[site_id]
                
                # Готовим запрос к хостеру
                request_id = ReactorNetRelay.generate_request_id()
                
                # Создаём событие для ожидания ответа
                import threading
                response_event = threading.Event()
                response_data = {'html': '', 'headers': {}}
                
                def set_response(response):
                    nonlocal response_data
                    response_data = response
                    response_event.set()
                
                with pending_lock:
                    pending_requests[request_id] = set_response
                
                # Отправляем запрос хостеру
                host_ws.send(json.dumps({
                    'type': 'request',
                    'request_id': request_id,
                    'url': data.get('url', '/'),
                    'method': data.get('method', 'GET'),
                    'headers': data.get('headers', {}),
                    'body': data.get('body', '')
                }))
                
                # Ждём ответ (таймаут 30 секунд)
                timeout = 30
                if response_event.wait(timeout):
                    ws.send(json.dumps({
                        'type': 'response',
                        'html': response_data.get('html', ''),
                        'status': response_data.get('status', 200),
                        'headers': response_data.get('headers', {})
                    }))
                else:
                    ws.send(json.dumps({
                        'type': 'error',
                        'message': 'Request timeout (host took too long to respond)'
                    }))
                    
    except Exception as e:
        logger.error(f"❌ Visit error: {e}")
        try:
            ws.send(json.dumps({'type': 'error', 'message': str(e)}))
        except:
            pass

@app.route('/api/sites')
def list_sites():
    """API для получения списка активных сайтов"""
    with hosts_lock:
        sites = list(hosts.keys())
    return jsonify({'sites': [f"{s}.reactor" for s in sites]})

if __name__ == '__main__':
    # Render даёт порт через переменную окружения, по умолчанию 10000
    import os
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
