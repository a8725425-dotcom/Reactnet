#!/usr/bin/env python3
"""
ReactorNet Browser
Пользовательский браузер для просмотра .reactor сайтов
"""

import sys
import json
import threading
import websocket
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, 
    QHBoxLayout, QLineEdit, QPushButton, QTabWidget,
    QStatusBar, QProgressBar, QMessageBox, QMenuBar,
    QAction, QInputDialog
)
from PyQt5.QtCore import QUrl, Qt, pyqtSignal, QObject
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage
from PyQt5.QtGui import QIcon

class ReactorNetClient(QObject):
    """Клиент для связи с ретранслятором"""
    
    page_received = pyqtSignal(str, str, int)  # url, html, status
    error_occurred = pyqtSignal(str, str)       # url, error_message
    status_changed = pyqtSignal(str)            # status text
    
    def __init__(self, relay_url="wss://reactornet.onrender.com"):
        super().__init__()
        self.relay_url = relay_url
        self.ws = None
        self.ws_thread = None
        self.running = False
        self.pending_requests = {}  # {request_id: callback}
        
    def connect(self):
        """Подключаемся к ретранслятору"""
        self.running = True
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()
        
    def _run_websocket(self):
        """Запускаем WebSocket в отдельном потоке"""
        try:
            self.ws = websocket.WebSocketApp(
                self.relay_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close
            )
            self.ws.run_forever()
        except Exception as e:
            self.error_occurred.emit("connection", str(e))
            
    def _on_open(self, ws):
        self.status_changed.emit("Connected to ReactorNet Relay")
        print("✅ Connected to ReactorNet Relay")
        
    def _on_message(self, ws, message):
        """Обрабатываем ответы от ретранслятора"""
        try:
            data = json.loads(message)
            
            if data['type'] == 'response':
                # Ответ на наш запрос
                if 'request_id' in data:
                    # Это формат от старого кода, адаптируем
                    pass
                    
            elif data['type'] == 'error':
                self.error_occurred.emit("relay", data['message'])
                
        except Exception as e:
            print(f"Error parsing message: {e}")
            
    def _on_error(self, ws, error):
        print(f"WebSocket error: {error}")
        self.error_occurred.emit("connection", str(error))
        
    def _on_close(self, ws, close_status_code, close_msg):
        print("Disconnected from relay")
        self.status_changed.emit("Disconnected from relay")
        
    def fetch_page(self, site_id, url_path="/", callback=None):
        """
        Запрашиваем страницу у хостера
        site_id: например 'mysite'
        url_path: '/page.html'
        """
        if not self.ws:
            self.error_occurred.emit(site_id, "Not connected to relay")
            if callback:
                callback("", 500)
            return
            
        # Отправляем запрос через WebSocket
        # Но так как у нас visit_mode ожидает особый формат,
        # временно используем прямой подход - переподключимся как visit
        
        def fetch_in_thread():
            try:
                # Создаём временное соединение в режиме visit
                visit_ws = websocket.WebSocket()
                visit_ws.connect(f"{self.relay_url}/visit/{site_id}")
                
                # Отправляем запрос
                request = json.dumps({
                    'type': 'request',
                    'url': url_path,
                    'method': 'GET',
                    'headers': {},
                    'body': ''
                })
                visit_ws.send(request)
                
                # Ждём ответ
                response = json.loads(visit_ws.recv())
                visit_ws.close()
                
                if response['type'] == 'response':
                    html = response.get('html', '<h1>Empty response</h1>')
                    status = response.get('status', 200)
                    if callback:
                        callback(html, status)
                elif response['type'] == 'error':
                    if callback:
                        callback(f"<h1>Error</h1><p>{response['message']}</p>", 404)
                        
            except Exception as e:
                print(f"Fetch error: {e}")
                if callback:
                    callback(f"<h1>Error</h1><p>Cannot reach host: {e}</p>", 500)
                    
        thread = threading.Thread(target=fetch_in_thread, daemon=True)
        thread.start()
        
    def host_site(self, site_id, local_port=8080):
        """
        Режим хостера - запускаем локальный сервер
        """
        def host_in_thread():
            try:
                ws = websocket.WebSocket()
                ws.connect(f"{self.relay_url}/host/{site_id}")
                
                print(f"🎉 Hosting {site_id}.reactor")
                
                while True:
                    message = ws.recv()
                    data = json.loads(message)
                    
                    if data['type'] == 'request':
                        # Получили запрос от посетителя
                        import requests
                        
                        # Делаем запрос к локальному серверу
                        try:
                            resp = requests.get(
                                f"http://localhost:{local_port}{data['url']}",
                                timeout=10
                            )
                            html = resp.text
                            status = resp.status_code
                        except Exception as e:
                            html = f"<h1>Local server error</h1><p>{e}</p>"
                            status = 500
                            
                        # Отправляем ответ
                        ws.send(json.dumps({
                            'type': 'response',
                            'request_id': data['request_id'],
                            'response': {
                                'html': html,
                                'status': status,
                                'headers': {'Content-Type': 'text/html'}
                            }
                        }))
                        
            except Exception as e:
                print(f"Hosting error: {e}")
                
        thread = threading.Thread(target=host_in_thread, daemon=True)
        thread.start()
        return thread

class ReactorWebPage(QWebEnginePage):
    """Кастомная страница для перехвата ссылок"""
    
    def __init__(self, parent=None, reactornet=None):
        super().__init__(parent)
        self.reactornet = reactornet
        
    def acceptNavigationRequest(self, url, _type, isMainFrame):
        """Перехватываем все переходы по ссылкам"""
        url_str = url.toString()
        
        if '.reactor' in url_str or url_str.startswith('reactor://'):
            # Обрабатываем через ReactorNet
            self.reactornet.load_reactor_url(url_str)
            return False
            
        # Обычные HTTP/HTTPS сайты
        return super().acceptNavigationRequest(url, _type, isMainFrame)

class ReactorBrowser(QMainWindow):
    """Главное окно браузера"""
    
    def __init__(self, relay_url="wss://reactornet-relay.onrender.com"):
        super().__init__()
        self.reactornet = ReactorNetClient(relay_url)
        self.reactornet.connect()
        self.current_site = None
        self.hosting_thread = None
        
        self.setWindowTitle("ReactorNet Browser")
        self.setGeometry(100, 100, 1200, 800)
        
        # Создаём интерфейс
        self.setup_ui()
        
        # Подключаем сигналы
        self.reactornet.error_occurred.connect(self.on_error)
        self.reactornet.status_changed.connect(self.status_bar.showMessage)
        
    def setup_ui(self):
        """Создаём UI"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        
        # Адресная строка
        url_layout = QHBoxLayout()
        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Enter .reactor address (e.g., mysite.reactor)")
        self.url_bar.returnPressed.connect(self.navigate_to_url)
        
        self.go_button = QPushButton("Go")
        self.go_button.clicked.connect(self.navigate_to_url)
        
        self.host_button = QPushButton("Host Site")
        self.host_button.clicked.connect(self.start_hosting)
        
        url_layout.addWidget(self.url_bar)
        url_layout.addWidget(self.go_button)
        url_layout.addWidget(self.host_button)
        
        layout.addLayout(url_layout)
        
        # Вкладки для страниц
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        layout.addWidget(self.tabs)
        
        # Добавляем первую вкладку
        self.add_new_tab()
        
        # Статусбар
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")
        
        # Меню
        menubar = self.menuBar()
        
        file_menu = menubar.addMenu("File")
        new_tab_action = QAction("New Tab", self)
        new_tab_action.triggered.connect(lambda: self.add_new_tab())
        file_menu.addAction(new_tab_action)
        
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        reactornet_menu = menubar.addMenu("ReactorNet")
        list_sites_action = QAction("List Active Sites", self)
        list_sites_action.triggered.connect(self.list_active_sites)
        reactornet_menu.addAction(list_sites_action)
        
        host_action = QAction("Start Hosting", self)
        host_action.triggered.connect(self.start_hosting)
        reactornet_menu.addAction(host_action)
        
        stop_host_action = QAction("Stop Hosting", self)
        stop_host_action.triggered.connect(self.stop_hosting)
        reactornet_menu.addAction(stop_host_action)
        
        help_menu = menubar.addMenu("Help")
        about_action = QAction("About ReactorNet", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
    def add_new_tab(self, url=""):
        """Добавляем новую вкладку"""
        browser = QWebEngineView()
        
        # Создаём кастомную страницу
        page = ReactorWebPage(browser, self)
        browser.setPage(page)
        
        # Устанавливаем страницу
        if url:
            browser.setHtml("<h1>Loading...</h1><p>Connecting to ReactorNet...</p>")
            self.load_reactor_url_in_browser(url, browser)
        else:
            browser.setHtml(self.get_start_page())
            
        index = self.tabs.addTab(browser, "New Tab")
        self.tabs.setCurrentIndex(index)
        
    def get_start_page(self):
        """Стартовая страница"""
        return """
        <html>
        <head>
            <title>ReactorNet Browser</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                    background: #f0f0f0;
                }
                h1 { color: #333; }
                .info {
                    background: white;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                }
                input {
                    width: 100%;
                    padding: 10px;
                    font-size: 16px;
                }
                .example {
                    margin-top: 20px;
                    color: #666;
                }
            </style>
        </head>
        <body>
            <div class="info">
                <h1>🌐 ReactorNet Browser</h1>
                <p>Welcome to the alternative internet!</p>
                <p>To visit a site, enter: <strong>mysite.reactor</strong> in the address bar</p>
                <div class="example">
                    <p>📡 To host your own site:</p>
                    <ul>
                        <li>First, run a local web server: <code>python -m http.server 8080</code></li>
                        <li>Then click "Host Site" and enter your site ID</li>
                        <li>Your site will be available at: <strong>your-id.reactor</strong></li>
                    </ul>
                </div>
                <p><small>Powered by ReactorNet - The people's internet</small></p>
            </div>
        </body>
        </html>
        """
        
    def navigate_to_url(self):
        """Обрабатываем ввод адреса"""
        url = self.url_bar.text().strip()
        
        if not url:
            return
            
        # Добавляем .reactor если нет домена
        if '.' not in url and '://' not in url:
            url = f"{url}.reactor"
            
        # Добавляем протокол если нет
        if not url.startswith('http') and not url.startswith('reactor://'):
            url = f"http://{url}"
            
        current_browser = self.tabs.currentWidget()
        if current_browser:
            self.load_reactor_url_in_browser(url, current_browser)
            
    def load_reactor_url_in_browser(self, url, browser):
        """Загружаем .reactor страницу через ReactorNet"""
        # Парсим site_id и путь
        import re
        
        # Извлекаем site_id (что-то.reactor)
        match = re.search(r'([a-zA-Z0-9_-]+)\.reactor', url)
        if not match:
            browser.setHtml(f"<h1>Invalid ReactorNet address</h1><p>{url}</p>", QUrl(url))
            return
            
        site_id = match.group(1)
        
        # Извлекаем путь
        path = '/'
        if '/' in url:
            parts = url.split('/', 3)
            if len(parts) > 3:
                path = '/' + parts[3]
                
        # Запрашиваем страницу
        self.status_bar.showMessage(f"Connecting to {site_id}.reactor...")
        
        def on_page_received(html, status):
            if status == 200:
                browser.setHtml(html, QUrl(f"http://{site_id}.reactor"))
                self.status_bar.showMessage(f"Loaded {site_id}.reactor", 3000)
                self.url_bar.setText(f"http://{site_id}.reactor")
            else:
                browser.setHtml(f"<h1>Error {status}</h1><p>{html}</p>", QUrl(url))
                self.status_bar.showMessage(f"Failed to load {site_id}.reactor")
                
        self.reactornet.fetch_page(site_id, path, on_page_received)
        
    def load_reactor_url(self, url_str):
        """Загружаем .reactor URL из принятого навигационного запроса"""
        current_browser = self.tabs.currentWidget()
        if current_browser:
            self.load_reactor_url_in_browser(url_str, current_browser)
            
    def start_hosting(self):
        """Запускаем режим хостера"""
        site_id, ok = QInputDialog.getText(
            self, "Host Site", 
            "Enter your site ID (e.g., mysite):\n\n"
            "First make sure you have a local web server running:\n"
            "python -m http.server 8080"
        )
        
        if ok and site_id:
            # Проверяем локальный сервер
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', 8080))
            sock.close()
            
            if result != 0:
                QMessageBox.warning(
                    self, "Local Server Not Found",
                    "No web server found on port 8080.\n\n"
                    "Please run: python -m http.server 8080\n"
                    "Then try again."
                )
                return
                
            self.hosting_thread = self.reactornet.host_site(site_id, 8080)
            self.status_bar.showMessage(f"🌐 Hosting {site_id}.reactor - Visit it from any ReactorNet browser!")
            
            QMessageBox.information(
                self, "Hosting Started",
                f"✅ Your site is now live at: {site_id}.reactor\n\n"
                f"Visit it from any ReactorNet browser!\n\n"
                f"To stop hosting, click 'Stop Hosting' in the menu."
            )
            
    def stop_hosting(self):
        """Останавливаем хостинг"""
        if self.hosting_thread:
            # WebSocket прервётся сам при закрытии
            self.hosting_thread = None
            self.status_bar.showMessage("Hosting stopped")
            
    def list_active_sites(self):
        """Показываем список активных сайтов"""
        import requests
        
        # Получаем список от ретранслятора
        relay_http = self.reactornet.relay_url.replace('wss://', 'https://')
        try:
            response = requests.get(f"{relay_http}/api/sites", timeout=5)
            sites = response.json().get('sites', [])
            
            if sites:
                html = "<h2>Active ReactorNet Sites</h2><ul>"
                for site in sites:
                    html += f"<li><a href='http://{site}'>{site}</a></li>"
                html += "</ul>"
            else:
                html = "<h2>No active sites</h2><p>Be the first to host a site!</p>"
                
        except:
            html = "<h2>Cannot fetch site list</h2><p>Relay might be offline</p>"
            
        # Открываем в новой вкладке
        current_browser = self.tabs.currentWidget()
        if current_browser:
            current_browser.setHtml(html)
            
    def on_error(self, source, message):
        """Обрабатываем ошибки"""
        self.status_bar.showMessage(f"Error: {message}")
        print(f"ReactorNet Error [{source}]: {message}")
        
    def close_tab(self, index):
        """Закрываем вкладку"""
        if self.tabs.count() > 1:
            self.tabs.removeTab(index)
        else:
            # Очищаем последнюю вкладку
            browser = self.tabs.widget(index)
            browser.setHtml(self.get_start_page())
            
    def show_about(self):
        """Показываем информацию о браузере"""
        QMessageBox.about(
            self, "About ReactorNet Browser",
            "<h3>ReactorNet Browser v1.0</h3>"
            "<p>A browser for the alternative internet.</p>"
            "<p>With ReactorNet, you can:<br>"
            "🌐 Visit .reactor websites<br>"
            "🏠 Host your own site from home without port forwarding<br>"
            "🔒 Decentralized and community-driven</p>"
            "<p><b>How to host:</b><br>"
            "1. Run <code>python -m http.server 8080</code><br>"
            "2. Click 'Host Site' in ReactorNet Browser<br>"
            "3. Your site is live!</p>"
            "<p><i>ReactorNet - The people's internet</i></p>"
        )
        
    def closeEvent(self, event):
        """Закрываем браузер"""
        event.accept()

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ReactorNet Browser")
    
    # Можно передать URL ретранслятора аргументом
    relay_url = "wss://reactornet-relay.onrender.com"
    if len(sys.argv) > 1:
        relay_url = sys.argv[1]
        
    browser = ReactorBrowser(relay_url)
    browser.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
