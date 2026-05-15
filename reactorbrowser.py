# reactorbrowser.py
# ReactorNet Browser v3 - с поддержкой RLF (React Lang Files)

import sys
import json
import threading
import re
import requests
import websocket
import os
from urllib.parse import unquote, quote

from PyQt5.QtCore import (
    pyqtSignal,
    QObject,
    QUrl,
    QProcess
)

from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLineEdit,
    QMessageBox,
    QInputDialog,
    QTabWidget,
    QStatusBar
)

from PyQt5.QtWebEngineWidgets import (
    QWebEngineView,
    QWebEnginePage
)

# =========================================================
# RLF Parser (React Lang Files)
# =========================================================

class RLFParser:
    """
    Парсер RLF - упрощённого языка разметки для ReactorNet
    
    Синтаксис:
    ---
    #title Заголовок страницы
    
    @style
    body { background: #0f172a; color: white; }
    h1 { color: #22c55e; }
    @end
    
    @component header
    <div style="padding:20px; background:#1e293b;">
        <h1>Мой сайт</h1>
    </div>
    @end
    
    @content
    <div class="container">
        <h1>#title</h1>
        <p>Привет мир!</p>
        <button onclick="alert('Clicked!')">Нажми</button>
        <link>/page2</link>
    </div>
    @end
    
    @variables
    title = "ReactorNet Site"
    @end
    ---
    
    Команды:
    #title - заголовок страницы
    @style ... @end - CSS стили
    @component name ... @end - компонент (можно использовать повторно)
    @content ... @end - основное содержимое
    @variables ... @end - переменные
    @import url - импорт другого RLF файла
    @include component - включение компонента
    """
    
    @staticmethod
    def parse(rlf_content, base_url=""):
        """Парсит RLF в HTML"""
        
        if not rlf_content or not isinstance(rlf_content, str):
            return RLFParser._error_page("Empty RLF content")
        
        # Удаляем BOM если есть
        if rlf_content.startswith('\ufeff'):
            rlf_content = rlf_content[1:]
        
        # Результаты парсинга
        title = "ReactorNet Page"
        styles = []
        components = {}
        content = ""
        variables = {}
        
        # Текущий режим парсинга
        current_mode = None
        current_component_name = None
        current_buffer = []
        
        lines = rlf_content.split('\n')
        
        for i, line in enumerate(lines):
            line = line.rstrip('\r')
            
            # Пропускаем пустые строки вне режимов
            if not line.strip() and current_mode is None:
                continue
            
            # Заголовок страницы
            if line.startswith('#title'):
                title = line[6:].strip()
                continue
            
            # Начало блока стилей
            if line.strip().startswith('@style'):
                current_mode = 'style'
                current_buffer = []
                continue
            
            # Начало компонента
            if line.strip().startswith('@component'):
                parts = line.strip().split()
                if len(parts) >= 2:
                    current_mode = 'component'
                    current_component_name = parts[1]
                    current_buffer = []
                continue
            
            # Начало контента
            if line.strip().startswith('@content'):
                current_mode = 'content'
                current_buffer = []
                continue
            
            # Начало переменных
            if line.strip().startswith('@variables'):
                current_mode = 'variables'
                current_buffer = []
                continue
            
            # Импорт
            if line.strip().startswith('@import'):
                import_path = line.strip()[7:].strip().strip('"\'')
                try:
                    imported = RLFParser._load_import(import_path, base_url)
                    if imported:
                        # Рекурсивно парсим импортированный контент
                        imported_html = RLFParser.parse(imported, base_url)
                        # Извлекаем основное содержимое из импортированного HTML
                        import_match = re.search(r'<div class="rlf-content">(.*?)</div>', imported_html, re.DOTALL)
                        if import_match:
                            current_buffer.append(import_match.group(1))
                except Exception as e:
                    current_buffer.append(f"<div class='error'>Failed to import: {import_path} - {e}</div>")
                continue
            
            # Включение компонента
            if line.strip().startswith('@include'):
                comp_name = line.strip()[8:].strip()
                if comp_name in components:
                    current_buffer.append(components[comp_name])
                else:
                    current_buffer.append(f"<div class='error'>Component not found: {comp_name}</div>")
                continue
            
            # Конец блока
            if line.strip().startswith('@end'):
                content_block = '\n'.join(current_buffer)
                
                if current_mode == 'style':
                    styles.append(content_block)
                elif current_mode == 'component' and current_component_name:
                    components[current_component_name] = content_block
                elif current_mode == 'content':
                    content = content_block
                elif current_mode == 'variables':
                    # Парсим переменные
                    for var_line in current_buffer:
                        if '=' in var_line:
                            var_parts = var_line.split('=', 1)
                            var_name = var_parts[0].strip()
                            var_value = var_parts[1].strip().strip('"\'')
                            variables[var_name] = var_value
                
                current_mode = None
                current_component_name = None
                current_buffer = []
                continue
            
            # Добавляем строку в текущий буфер
            if current_mode is not None:
                current_buffer.append(line)
            elif content is None:
                # Если нет активного режима и это не пустая строка - добавляем как простой текст
                if content is None:
                    content = ""
                content += line + "\n"
        
        # Заменяем переменные в контенте
        for var_name, var_value in variables.items():
            content = content.replace(f"{{{var_name}}}", str(var_value))
            content = content.replace(f"${{{var_name}}}", str(var_value))
            title = title.replace(f"{{{var_name}}}", str(var_value))
        
        # Заменяем ссылки .reactor на полные URL
        content = RLFParser._process_links(content, base_url)
        
        # Собираем финальный HTML
        html = RLFParser._build_html(title, styles, content, variables)
        
        return html
    
    @staticmethod
    def _load_import(path, base_url):
        """Загружает импортированный RLF файл"""
        try:
            if path.startswith('http'):
                response = requests.get(path, timeout=10)
                if response.status_code == 200:
                    return response.text
            else:
                # Локальный файл
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        return f.read()
            return None
        except Exception:
            return None
    
    @staticmethod
    def _process_links(content, base_url):
        """Обрабатывает ссылки в специальном формате"""
        # Обработка <link>/path</link>
        content = re.sub(
            r'<link>(.*?)</link>',
            r'<a href="\1" class="rlf-link">\1</a>',
            content
        )
        
        # Обработка [text](/path)
        content = re.sub(
            r'\[(.*?)\]\((.*?)\)',
            r'<a href="\2" class="rlf-link">\1</a>',
            content
        )
        
        return content
    
    @staticmethod
    def _build_html(title, styles, content, variables):
        """Собирает финальный HTML документ"""
        
        style_block = "\n".join(styles) if styles else """
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                line-height: 1.6;
                padding: 20px;
            }
            .container { max-width: 1200px; margin: 0 auto; }
            .rlf-link { color: #22c55e; text-decoration: none; cursor: pointer; }
            .rlf-link:hover { text-decoration: underline; }
            .error { color: #ef4444; padding: 10px; background: #7f1d1d; border-radius: 5px; margin: 10px 0; }
            button, .rlf-button {
                background: #22c55e;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 5px;
                cursor: pointer;
                font-size: 14px;
            }
            button:hover, .rlf-button:hover { background: #16a34a; }
            input, textarea, select {
                padding: 8px;
                border: 1px solid #334155;
                border-radius: 5px;
                background: #1e293b;
                color: white;
            }
        """
        
        # Добавляем базовый стиль если нет своих
        if not styles:
            style_block = """
            body { background: #0f172a; color: #e2e8f0; }
            h1, h2, h3 { color: #22c55e; }
            a { color: #34d399; }
            """ + style_block
        
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>{style_block}</style>
    <script>
    // Встроенный JavaScript для RLF
    document.addEventListener('DOMContentLoaded', function() {{
        // Обработка кликов по ссылкам
        document.querySelectorAll('.rlf-link, a[href]').forEach(function(link) {{
            link.addEventListener('click', function(e) {{
                var href = this.getAttribute('href');
                if (href && !href.startsWith('http') && !href.startsWith('#')) {{
                    e.preventDefault();
                    if (window.reactorbrowser && window.reactorbrowser.navigate) {{
                        window.reactorbrowser.navigate(href);
                    }}
                }}
            }});
        }});
        
        // Простой обработчик для RLF кнопок
        document.querySelectorAll('[data-rlf-action]').forEach(function(btn) {{
            btn.addEventListener('click', function() {{
                var action = this.getAttribute('data-rlf-action');
                if (action === 'back' && window.history.back) window.history.back();
                if (action === 'forward' && window.history.forward) window.history.forward();
            }});
        }});
    }});
    </script>
</head>
<body>
    <div class="container rlf-content">
        {content}
    </div>
</body>
</html>"""
    
    @staticmethod
    def _error_page(message):
        """Генерирует страницу ошибки"""
        return f"""<!DOCTYPE html>
<html>
<head><title>RLF Error</title>
<style>
    body {{ background: #0f172a; color: #e2e8f0; font-family: Arial; padding: 40px; text-align: center; }}
    .error {{ background: #7f1d1d; padding: 20px; border-radius: 10px; margin: 20px auto; max-width: 600px; }}
    h1 {{ color: #ef4444; }}
</style>
</head>
<body>
    <div class="error">
        <h1>⚠ RLF Parse Error</h1>
        <p>{message}</p>
    </div>
</body>
</html>"""


# =========================================================
# RLF Page для WebEngine
# =========================================================

class RLFPage(QWebEnginePage):
    """Страница с поддержкой RLF навигации"""
    
    navigate_to = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setZoomFactor(1.0)
    
    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        url_str = url.toString()
        
        # data: URLs - это наши сгенерированные страницы
        if url_str.startswith("data:"):
            return False
        
        # .reactor ссылки
        if ".reactor" in url_str or url_str.startswith("/"):
            self.navigate_to.emit(url_str)
            return False
        
        # Внешние ссылки (можно открыть в системном браузере)
        if url_str.startswith("http"):
            import webbrowser
            webbrowser.open(url_str)
            return False
        
        return True
    
    def javaScriptAlert(self, url, msg):
        """Переопределяем alert для RLF"""
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.information(None, "RLF Alert", msg)


# =========================================================
# Browser Tab с RLF поддержкой
# =========================================================

class BrowserTab(QWebEngineView):
    
    def __init__(self, browser):
        super().__init__()
        self.browser = browser
        self.current_rlf_url = ""
        self.current_site_id = ""
        
        # Создаём RLF страницу
        self.rlf_page = RLFPage(self)
        self.rlf_page.navigate_to.connect(self.browser.handle_navigation)
        self.setPage(self.rlf_page)
        
        # Проксируем JavaScript bridge
        self.page().runJavaScript = self.runJavaScript
    
    def runJavaScript(self, script, callback=None):
        """Выполняет JavaScript на странице"""
        if callback:
            super().page().runJavaScript(script, callback)
        else:
            super().page().runJavaScript(script)


# =========================================================
# ReactorNet Client с RLF поддержкой
# =========================================================

class ReactorNetClient(QObject):
    
    page_loaded = pyqtSignal(str, str, str)  # html, url, site_id
    error_occurred = pyqtSignal(str)
    search_results_loaded = pyqtSignal(str, str)
    
    def __init__(self):
        super().__init__()
        self.relay_url = "wss://reactnet.onrender.com"
        self.search_api_base = "https://reactnet.onrender.com"
        self.hosting_active = False
    
    def fetch_page(self, site_id, path="/"):
        """Загружает страницу через релей сервер"""
        
        def worker():
            try:
                ws = websocket.WebSocket()
                ws.connect(f"{self.relay_url}/visit/{site_id}")
                
                ws.send(json.dumps({
                    "type": "request",
                    "url": path,
                    "method": "GET",
                    "headers": {},
                    "body": ""
                }))
                
                response = json.loads(ws.recv())
                ws.close()
                
                if response.get("type") == "response":
                    content = response.get("html", "")
                    
                    # Определяем тип контента
                    if content.strip().startswith("---") or "@content" in content or "#title" in content:
                        # Это RLF файл
                        html = RLFParser.parse(content, f"http://{site_id}.reactor")
                    else:
                        # Обычный HTML или что-то ещё
                        html = content
                    
                    self.page_loaded.emit(
                        html,
                        f"http://{site_id}.reactor{path}",
                        site_id
                    )
                else:
                    self.error_occurred.emit(response.get("message", "Unknown error"))
                    
            except Exception as e:
                self.error_occurred.emit(str(e))
        
        threading.Thread(target=worker, daemon=True).start()
    
    def fetch_rlf_file(self, site_id, filename):
        """Загружает конкретный RLF файл"""
        path = f"/{filename}" if not filename.startswith("/") else filename
        if not filename.endswith(".rlf"):
            path = path + ".rlf"
        self.fetch_page(site_id, path)
    
    def fetch_reactorgo_search(self, query):
        """Поиск через reactorGO"""
        def worker():
            try:
                q = (query or "").strip()
                if not q:
                    self.search_results_loaded.emit(
                        self._render_search_empty(),
                        "reactorGO://empty"
                    )
                    return
                
                url = f"{self.search_api_base}/api/search/reactorGO"
                resp = requests.get(url, params={"query": q}, timeout=15)
                
                if resp.status_code != 200:
                    self.search_results_loaded.emit(
                        self._render_search_error(resp.text),
                        f"reactorGO://error/{q}"
                    )
                    return
                
                data = resp.json()
                results = data.get("results", [])
                self.search_results_loaded.emit(
                    self._render_search_results(q, results),
                    f"reactorGO://search?q={q}"
                )
            except Exception as e:
                self.search_results_loaded.emit(
                    self._render_search_error(str(e)),
                    "reactorGO://error"
                )
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _render_search_empty(self):
        return RLFParser.parse("""
#title reactorGO Search

@content
<div style="text-align:center; padding:60px 20px;">
    <h1>🔎 reactorGO</h1>
    <p>Введите запрос для поиска сайтов в сети ReactorNet</p>
    <p style="opacity:0.7; margin-top:20px;">Доступные сайты: wiki, blog, forum, и другие</p>
</div>
@end
""")
    
    def _render_search_error(self, message):
        return RLFParser.parse(f"""
#title Search Error

@content
<div style="text-align:center; padding:60px 20px;">
    <h1>⚠ Ошибка поиска</h1>
    <p>{message}</p>
    <button onclick="history.back()">Вернуться</button>
</div>
@end
""")
    
    def _render_search_results(self, query, results):
        items_html = ""
        for r in results[:50]:
            name = r.get("name", "")
            site = r.get("site", "")
            site_id = site.replace("http://", "").replace(".reactor", "")
            items_html += f"""
            <div style="margin:15px 0; padding:15px 20px; border:2px solid #22c55e; border-radius:10px; background:#0b3b1f;">
                <div style="font-size:18px; font-weight:700;">{name}</div>
                <div style="font-size:13px; opacity:0.8; margin:5px 0;">{site}</div>
                <div style="margin-top:10px;">
                    <a href="http://{site_id}.reactor" class="rlf-button" style="background:#22c55e; color:white; padding:8px 16px; border-radius:5px; text-decoration:none;">Открыть сайт</a>
                </div>
            </div>
            """
        
        if not items_html:
            items_html = """
            <div style="margin:20px 0; padding:20px; border:2px solid #f59e0b; border-radius:10px; background:#3b2a08;">
                <div style="font-size:16px; font-weight:700;">Ничего не найдено</div>
                <div>Попробуйте другой запрос или проверьте доступность сайтов</div>
            </div>
            """
        
        return RLFParser.parse(f"""
#title reactorGO - {query}

@content
<div style="max-width:800px; margin:0 auto; padding:20px;">
    <h1 style="text-align:center;">🔎 reactorGO</h1>
    <p style="text-align:center;">Результаты поиска: "{query}"</p>
    
    <div style="margin-top:30px;">
        {items_html}
    </div>
    
    <div style="margin-top:40px; text-align:center; opacity:0.7; font-size:12px;">
        <p>Поиск по активным сайтам ReactorNet</p>
    </div>
</div>
@end
""")
    
    def host_site(self, site_id, local_port=8080):
        """Хостинг сайта через релей сервер"""
        
        self.hosting_active = True
        
        def worker():
            try:
                ws = websocket.WebSocket()
                ws.connect(f"{self.relay_url}/host/{site_id}")
                
                while self.hosting_active:
                    message = ws.recv()
                    data = json.loads(message)
                    
                    if data.get("type") == "request":
                        try:
                            url_path = data.get('url', '/')
                            local_url = f"http://127.0.0.1:{local_port}{url_path}"
                            
                            response = requests.get(local_url, timeout=10)
                            
                            # Проверяем, не RLF ли файл
                            content = response.text
                            if url_path.endswith('.rlf') or (not '.' in url_path.split('/')[-1] and url_path != '/'):
                                # Отдаём как RLF - пусть клиент парсит
                                pass
                            
                            ws.send(json.dumps({
                                "type": "response",
                                "request_id": data["request_id"],
                                "response": {
                                    "html": content,
                                    "status": response.status_code,
                                    "headers": dict(response.headers)
                                }
                            }))
                        except Exception as e:
                            error_rlf = f"""
#title Local Server Error

@content
<div style="text-align:center; padding:60px 20px;">
    <h1>⚠ Ошибка локального сервера</h1>
    <p>{e}</p>
    <p>Запустите локальный сервер:</p>
    <code style="background:#1e293b; padding:10px; display:inline-block; border-radius:5px;">
        python -m http.server {local_port}
    </code>
    <p style="margin-top:20px;">Или создайте файл index.rlf в текущей директории</p>
</div>
@end
"""
                            ws.send(json.dumps({
                                "type": "response",
                                "request_id": data["request_id"],
                                "response": {
                                    "html": error_rlf,
                                    "status": 500,
                                    "headers": {"Content-Type": "text/html"}
                                }
                            }))
                            
            except Exception as e:
                self.error_occurred.emit(str(e))
        
        threading.Thread(target=worker, daemon=True).start()
    
    def stop_hosting(self):
        self.hosting_active = False


# =========================================================
# Main Browser Window
# =========================================================

class ReactorBrowser(QMainWindow):
    
    def __init__(self):
        super().__init__()
        
        self.client = ReactorNetClient()
        
        self.client.page_loaded.connect(self.display_page)
        self.client.search_results_loaded.connect(self.display_search_page)
        self.client.error_occurred.connect(self.show_error)
        
        self.setup_ui()
    
    def setup_ui(self):
        self.setWindowTitle("ReactorNet Browser - RLF Edition")
        self.resize(1400, 900)
        
        central = QWidget()
        self.setCentralWidget(central)
        
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(5)
        toolbar.setContentsMargins(5, 5, 5, 5)
        
        self.back_btn = QPushButton("←")
        self.forward_btn = QPushButton("→")
        self.reload_btn = QPushButton("⟳")
        self.home_btn = QPushButton("🏠")
        
        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Enter .reactor address or search (e.g., wiki.reactor, blog)")
        self.url_bar.returnPressed.connect(self.navigate)
        
        self.go_btn = QPushButton("Go")
        self.go_btn.clicked.connect(self.navigate)
        
        self.new_tab_btn = QPushButton("+")
        self.host_btn = QPushButton("🎭 Host")
        
        for btn in [self.back_btn, self.forward_btn, self.reload_btn, self.home_btn]:
            btn.setFixedSize(35, 30)
        
        self.new_tab_btn.setFixedSize(35, 30)
        self.host_btn.setMinimumWidth(70)
        self.go_btn.setMinimumWidth(50)
        
        toolbar.addWidget(self.back_btn)
        toolbar.addWidget(self.forward_btn)
        toolbar.addWidget(self.reload_btn)
        toolbar.addWidget(self.home_btn)
        toolbar.addWidget(self.url_bar, 1)
        toolbar.addWidget(self.go_btn)
        toolbar.addWidget(self.new_tab_btn)
        toolbar.addWidget(self.host_btn)
        
        layout.addLayout(toolbar)
        
        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        layout.addWidget(self.tabs)
        
        # Status Bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready - RLF (React Lang Files) supported")
        
        # Connect buttons
        self.new_tab_btn.clicked.connect(self.new_tab)
        self.host_btn.clicked.connect(self.host_site)
        self.reload_btn.clicked.connect(self.reload_page)
        self.back_btn.clicked.connect(self.go_back)
        self.forward_btn.clicked.connect(self.go_forward)
        self.home_btn.clicked.connect(self.go_home)
        
        # First tab
        self.new_tab()
        
        # Load home page
        self.go_home()
    
    def new_tab(self):
        browser = BrowserTab(self)
        
        # Показываем приветствие на RLF
        welcome_rlf = """
#title ReactorNet Browser

@style
body { background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%); }
.feature-box { 
    background: rgba(34, 197, 94, 0.1); 
    border: 1px solid #22c55e; 
    border-radius: 10px; 
    padding: 20px; 
    margin: 15px; 
    display: inline-block; 
    width: 250px;
    transition: transform 0.2s;
}
.feature-box:hover { transform: translateY(-5px); }
@end

@content
<div style="text-align:center; padding:40px 20px;">
    <h1 style="font-size:48px;">🌐 ReactorNet Browser</h1>
    <p style="font-size:20px; opacity:0.9;">Добро пожаловать в децентрализованный интернет!</p>
    
    <div style="margin-top:50px;">
        <div class="feature-box">
            <h2>🚀 RLF Support</h2>
            <p>React Lang Files - простой и быстрый язык разметки</p>
        </div>
        <div class="feature-box">
            <h2>🔗 Direct Hosting</h2>
            <p>Хостите сайты прямо с вашего компьютера</p>
        </div>
        <div class="feature-box">
            <h2>🔎 reactorGO</h2>
            <p>Поиск по всем активным сайтам сети</p>
        </div>
    </div>
    
    <div style="margin-top:60px; padding:20px; background:#1e293b; border-radius:15px; max-width:500px; margin-left:auto; margin-right:auto;">
        <h3>🎯 Быстрый старт</h3>
        <p>1. wiki.reactor - документация</p>
        <p>2. blog.reactor - блог платформа</p>
        <p>3. Нажми "Host" чтобы создать свой сайт</p>
    </div>
    
    <div style="margin-top:40px; opacity:0.7;">
        <p>RLF: используйте #title, @style, @content, @component, @variables</p>
    </div>
</div>
@end
"""
        welcome_html = RLFParser.parse(welcome_rlf)
        browser.setHtml(welcome_html)
        
        index = self.tabs.addTab(browser, "New Tab")
        self.tabs.setCurrentIndex(index)
    
    def current_browser(self):
        return self.tabs.currentWidget()
    
    def close_tab(self, index):
        if self.tabs.count() > 1:
            widget = self.tabs.widget(index)
            widget.deleteLater()
            self.tabs.removeTab(index)
    
    def navigate(self):
        url = self.url_bar.text().strip()
        self.handle_navigation(url)
    
    def handle_navigation(self, url):
        if not url:
            return
        
        # Поиск через reactorGO
        if ".reactor" not in url and not url.startswith("http"):
            # Это поисковый запрос
            self.status.showMessage(f"Searching: {url}...")
            self.client.fetch_reactorgo_search(url)
            return
        
        # Обработка относительных ссылок
        if url.startswith("/"):
            current_browser = self.current_browser()
            if current_browser.current_site_id:
                url = f"http://{current_browser.current_site_id}.reactor{url}"
        
        # Добавляем .reactor если нужно
        if ".reactor" not in url and not url.startswith("http"):
            url += ".reactor"
        
        # Добавляем протокол
        if not url.startswith("http"):
            url = "http://" + url
        
        self.url_bar.setText(url)
        
        # Парсим site_id
        match = re.search(r'([a-zA-Z0-9_-]+)\.reactor', url)
        if not match:
            self.show_error("Invalid reactor URL format")
            return
        
        site_id = match.group(1)
        
        # Парсим путь
        path = "/"
        parts = url.split("/", 3)
        if len(parts) > 3:
            path = "/" + parts[3]
        
        # Проверяем, не RLF ли файл запрашивается
        if not path.endswith('.rlf') and path != "/" and '.' not in path.split('/')[-1]:
            # Если путь без расширения, пробуем index.rlf или просто путь
            if path == "/":
                path = "/index.rlf"
            else:
                path = path.rstrip('/') + ".rlf"
        
        browser = self.current_browser()
        browser.current_site_id = site_id
        
        self.status.showMessage(f"Loading {site_id}.reactor{path}...")
        self.client.fetch_page(site_id, path)
    
    def reload_page(self):
        browser = self.current_browser()
        if browser.current_site_id:
            self.client.fetch_page(browser.current_site_id, "/")
    
    def go_back(self):
        # Простая навигация - можно улучшить с историей
        self.status.showMessage("Use browser back button or history", 2000)
    
    def go_forward(self):
        self.status.showMessage("Use browser forward button", 2000)
    
    def go_home(self):
        welcome_rlf = """
#title ReactorNet Home

@content
<div style="text-align:center; padding:60px 20px;">
    <h1>🏠 ReactorNet Browser</h1>
    <p>Популярные сайты сети:</p>
    <div style="margin:30px auto; max-width:400px;">
        <div style="margin:10px;"><a href="wiki.reactor" class="rlf-link">📚 wiki.reactor</a> - документация</div>
        <div style="margin:10px;"><a href="blog.reactor" class="rlf-link">✍️ blog.reactor</a> - блоги</div>
        <div style="margin:10px;"><a href="forum.reactor" class="rlf-link">💬 forum.reactor</a> - форум</div>
    </div>
</div>
@end
"""
        html = RLFParser.parse(welcome_rlf)
        browser = self.current_browser()
        browser.current_site_id = ""
        browser.setHtml(html)
        self.url_bar.clear()
        self.tabs.setTabText(self.tabs.currentIndex(), "Home")
    
    def display_page(self, html, url, site_id):
        browser = self.current_browser()
        browser.current_rlf_url = url
        browser.current_site_id = site_id
        
        if url.startswith("http"):
            self.url_bar.setText(url)
        
        browser.setHtml(html)
        
        # Обновляем заголовок вкладки
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        if title_match:
            tab_title = title_match.group(1)[:30]
        else:
            tab_title = f"{site_id}.reactor"
        
        index = self.tabs.currentIndex()
        self.tabs.setTabText(index, tab_title)
        
        self.status.showMessage(f"Loaded: {site_id}.reactor", 3000)
    
    def display_search_page(self, html, virtual_url):
        browser = self.current_browser()
        browser.current_rlf_url = virtual_url
        browser.current_site_id = ""
        browser.setHtml(html)
        
        # Извлекаем поисковый запрос для отображения
        q_match = re.search(r'reactorGO://search\?q=(.*)$', virtual_url)
        if q_match:
            q = unquote(q_match.group(1))
            self.url_bar.setText(q)
        
        index = self.tabs.currentIndex()
        self.tabs.setTabText(index, "Search Results")
        self.status.showMessage("Search completed", 2000)
    
    def show_error(self, message):
        error_rlf = f"""
#title Error

@content
<div style="text-align:center; padding:60px 20px;">
    <h1>⚠ Ошибка</h1>
    <p>{message}</p>
    <button onclick="history.back()" class="rlf-button">Вернуться</button>
</div>
@end
"""
        html = RLFParser.parse(error_rlf)
        self.current_browser().setHtml(html)
        self.status.showMessage(f"Error: {message}", 5000)
    
    def host_site(self):
        site_id, ok = QInputDialog.getText(
            self,
            "Host Site on ReactorNet",
            "Enter site ID (e.g., 'mysite'):\n\nYour site will be available at:\nhttp://mysite.reactor"
        )
        
        if not ok or not site_id:
            return
        
        # Очищаем ID от недопустимых символов
        site_id = re.sub(r'[^a-zA-Z0-9_-]', '', site_id)
        
        if not site_id:
            self.show_error("Invalid site ID")
            return
        
        port, ok = QInputDialog.getInt(
            self,
            "Local Server Port",
            "Enter local server port (default 8080):",
            8080, 1, 65535
        )
        
        if not ok:
            return
        
        self.client.host_site(site_id, port)
        
        # Создаём пример RLF файла для пользователя
        example_rlf = """---
#title My ReactorNet Site

@style
body {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    font-family: Arial, sans-serif;
    min-height: 100vh;
}
.container {
    max-width: 800px;
    margin: 0 auto;
    padding: 40px;
}
.card {
    background: white;
    border-radius: 20px;
    padding: 30px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.2);
}
@end

@content
<div class="container">
    <div class="card">
        <h1>#title</h1>
        <p>Добро пожаловать на мой сайт в ReactorNet!</p>
        <p>Этот сайт работает на RLF (React Lang Files)</p>
        
        <h2>Примеры ссылок:</h2>
        <ul>
            <li><a href="/about.rlf" class="rlf-link">О сайте</a></li>
            <li><a href="/contact.rlf" class="rlf-link">Контакты</a></li>
        </ul>
        
        <button onclick="alert('Привет из RLF!')">Нажми меня</button>
    </div>
</div>
@end
"""
        
        msg = QMessageBox(self)
        msg.setWindowTitle("Hosting Started")
        msg.setText(f"""
✅ Ваш сайт запущен!

🌐 Адрес в сети: http://{site_id}.reactor
🔌 Локальный порт: {port}

📝 Инструкция:
1. Создайте файл index.rlf в папке с сайтом
2. Запустите локальный сервер:
   python -m http.server {port}
3. Или используйте любой другой HTTP сервер

💡 Пример RLF файла сохранён в example.rlf
        """)
        
        msg.setDetailedText(example_rlf)
        msg.exec_()
    
    def closeEvent(self, event):
        self.client.stop_hosting()
        event.accept()


# =========================================================
# Main
# =========================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("ReactorNet Browser RLF")
    
    # Создаём example.rlf если нужно
    if not os.path.exists("example.rlf"):
        with open("example.rlf", "w", encoding="utf-8") as f:
            f.write("""#title My ReactorNet Site

@style
body {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    font-family: Arial, sans-serif;
    min-height: 100vh;
}
.container {
    max-width: 800px;
    margin: 0 auto;
    padding: 40px;
}
.card {
    background: white;
    border-radius: 20px;
    padding: 30px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.2);
}
@end

@content
<div class="container">
    <div class="card">
        <h1>#title</h1>
        <p>Добро пожаловать на мой сайт в ReactorNet!</p>
        <p>Этот сайт работает на RLF (React Lang Files)</p>
        
        <h2>Синтаксис RLF:</h2>
        <ul>
            <li>#title - заголовок страницы</li>
            <li>@style ... @end - CSS стили</li>
            <li>@content ... @end - содержимое страницы</li>
            <li>@component name ... @end - компоненты</li>
            <li>@variables ... @end - переменные</li>
            <li>@import url - импорт других RLF</li>
            <li>@include component - использование компонента</li>
        </ul>
        
        <button onclick="alert('Привет из RLF!')">Нажми меня</button>
    </div>
</div>
@end
""")
    
    window = ReactorBrowser()
    window.show()
    sys.exit(app.exec_())
