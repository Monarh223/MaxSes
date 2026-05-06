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

# ========== УНИВЕРСАЛЬНЫЙ КЛОНЕР ==========
class UniversalCloner:
    def __init__(self, target_url, task_id, chat_id, method='auto'):
        self.target_url = target_url.rstrip('/')
        self.task_id = task_id
        self.chat_id = chat_id
        self.method = method  # 'static', 'spa', 'auto'
        self.output_dir = f"/tmp/clones/{task_id}"
        self.domain = urlparse(target_url).netloc
        
        os.makedirs(self.output_dir, exist_ok=True)
        for folder in ['css', 'js', 'images', 'fonts', 'media', 'static']:
            os.makedirs(f"{self.output_dir}/{folder}", exist_ok=True)
        
        self.session = requests.Session()
    
    def get_smart_headers(self, url):
        """Подбирает правильные заголовки под сайт"""
        domain = urlparse(url).netloc
        
        # Мобильные заголовки
        mobile_headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        
        # Для русских сайтов добавляем
        if any(x in domain for x in ['.ru', '.рф', 'max.ru', 'vk.com']):
            mobile_headers['Accept-Language'] = 'ru-RU,ru;q=0.9'
        
        return mobile_headers
    
    def download_asset(self, url, folder, referer=None):
        """Скачивает любой ассет"""
        try:
            if not url.startswith('http'):
                url = urljoin(self.target_url, url)
            
            # Пропускаем data: URI
            if url.startswith('data:'):
                return None
            
            # Парсим имя файла
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path.split('?')[0])
            
            if not filename or len(filename) < 3:
                ext_map = {
                    'css': '.css', 'js': '.js', 'javascript': '.js',
                    'png': '.png', 'jpg': '.jpg', 'jpeg': '.jpg',
                    'svg': '.svg', 'ico': '.ico', 'woff': '.woff',
                    'woff2': '.woff2', 'ttf': '.ttf', 'eot': '.eot',
                    'json': '.json', 'xml': '.xml'
                }
                ext = '.bin'
                for key, val in ext_map.items():
                    if key in url.lower():
                        ext = val
                        break
                filename = f"asset_{hash(url) % 100000}{ext}"
            
            filepath = f"{self.output_dir}/{folder}/{filename}"
            
            # Заголовки
            headers = self.get_smart_headers(url)
            if referer:
                headers['Referer'] = referer
            headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
            
            resp = self.session.get(url, headers=headers, timeout=15, allow_redirects=True)
            
            if resp.status_code == 200 and len(resp.content) > 50:
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                size_kb = len(resp.content) / 1024
                print(f"  ✅ {filename} ({size_kb:.1f} KB)")
                return f"{folder}/{filename}"
            
        except Exception as e:
            print(f"  ❌ {url[:80]} — {str(e)[:40]}")
        return None
    
    def clone_static(self):
        """Клонирование статического сайта"""
        bot.send_message(self.chat_id, f"📥 Метод: Статический\n🔄 Загружаю {self.target_url}")
        
        headers = self.get_smart_headers(self.target_url)
        response = self.session.get(self.target_url, headers=headers, allow_redirects=True, timeout=20)
        
        if response.status_code != 200:
            bot.send_message(self.chat_id, f"❌ HTTP {response.status_code}")
            return None
        
        final_url = response.url
        bot.send_message(self.chat_id, f"📍 Финальный URL: {final_url}")
        
        soup = BeautifulSoup(response.text, 'html.parser')
        asset_count = 0
        
        # CSS
        for tag in soup.find_all('link', href=True):
            if any(x in tag['href'].lower() for x in ['.css', 'style', 'font', 'icon']):
                local = self.download_asset(tag['href'], 'css', final_url)
                if local:
                    tag['href'] = local
                    asset_count += 1
        
        # JavaScript
        for tag in soup.find_all('script', src=True):
            local = self.download_asset(tag['src'], 'js', final_url)
            if local:
                tag['src'] = local
                asset_count += 1
        
        # Изображения
        img_tags = soup.find_all(['img', 'picture', 'source', 'video', 'audio'])
        for tag in img_tags:
            for attr in ['src', 'srcset', 'data-src', 'poster']:
                if tag.get(attr):
                    urls = tag[attr].split(',')
                    new_urls = []
                    for u in urls:
                        u = u.strip().split(' ')[0]
                        local = self.download_asset(u, 'images', final_url)
                        if local:
                            new_urls.append(local)
                            asset_count += 1
                    if new_urls:
                        tag[attr] = ', '.join(new_urls)
        
        # Фоны в style
        for tag in soup.find_all(style=True):
            urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', str(tag['style']))
            for url in urls:
                local = self.download_asset(url, 'images', final_url)
                if local:
                    tag['style'] = str(tag['style']).replace(url, local)
                    asset_count += 1
        
        # Ссылки на внутренние страницы
        for tag in soup.find_all('a', href=True):
            href = tag['href']
            if href.startswith('/') or self.domain in href:
                if href.startswith('/'):
                    tag['href'] = href
                else:
                    tag['href'] = urlparse(href).path or '/'
        
        # Сохраняем
        with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html>\n<html lang="ru">\n<head>\n')
            f.write('<meta charset="UTF-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
            f.write(f'<base href="/">\n')
            f.write('</head>\n<body>\n')
            f.write(str(soup))
            f.write('\n</body>\n</html>')
        
        # Обработчик форм
        with open(f'{self.output_dir}/save.php', 'w') as f:
            f.write('<?php\n$d=$_POST;$d["ip"]=$_SERVER["REMOTE_ADDR"];\nfile_put_contents("logs.txt",json_encode($d)."\\n",FILE_APPEND);\nheader("Location: /");')
        
        return {
            'method': 'static',
            'final_url': final_url,
            'assets': asset_count
        }
    
    def clone_spa(self):
        """Клонирование SPA через рендеринг (Playwright)"""
        bot.send_message(self.chat_id, "📥 Метод: SPA (рендеринг)\n⏳ Это займёт 15-30 секунд...")
        
        try:
            from playwright.sync_api import sync_playwright
            
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
                context = browser.new_context(
                    viewport={'width': 390, 'height': 844},
                    user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'
                )
                page = context.new_page()
                
                # Ждём полной загрузки
                page.goto(self.target_url, wait_until='networkidle', timeout=30000)
                page.wait_for_timeout(5000)
                
                # Скриншот для проверки
                page.screenshot(path=f'{self.output_dir}/preview.png', full_page=True)
                
                # Сохраняем отрендеренный HTML
                html = page.content()
                
                # Сохраняем все ресурсы со страницы
                resources = page.evaluate('''() => {
                    const resources = [];
                    performance.getEntriesByType('resource').forEach(r => {
                        resources.push({url: r.name, type: r.initiatorType});
                    });
                    return resources;
                }''')
                
                for res in resources:
                    folder = 'js' if 'script' in res['type'] else 'css' if 'css' in res['type'] else 'images'
                    self.download_asset(res['url'], folder, self.target_url)
                
                browser.close()
            
            # Очищаем HTML от внешних ссылок
            soup = BeautifulSoup(html, 'html.parser')
            
            # Удаляем скрипты аналитики
            for script in soup.find_all('script', src=True):
                if any(x in script['src'] for x in ['google', 'yandex', 'analytics', 'metric']):
                    script.decompose()
            
            with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
                f.write(str(soup))
            
            return {'method': 'spa', 'assets': len(resources)}
            
        except ImportError:
            bot.send_message(self.chat_id, "⚠️ Playwright не установлен. Использую статический метод.")
            return self.clone_static()
        except Exception as e:
            bot.send_message(self.chat_id, f"❌ Ошибка SPA: {str(e)[:100]}")
            return None
    
    def clone(self):
        """Автовыбор метода и клонирование"""
        # Пробуем статический метод
        result = self.clone_static()
        
        if result:
            # Проверяем, не SPA ли это
            with open(f'{self.output_dir}/index.html', 'r', encoding='utf-8') as f:
                html = f.read()
            
            # Признаки SPA
            spa_indicators = ['<div id="root">', '<div id="app">', 'react', 'vue', 'angular', 
                            'webpack', 'chunk', 'bundle.js', '__NEXT', '__NUXT']
            
            is_spa = any(indicator in html.lower() for indicator in spa_indicators)
            
            if is_spa and len(html) < 5000:
                bot.send_message(self.chat_id, "🔍 Обнаружен SPA. Запускаю полный рендеринг...")
                result = self.clone_spa()
        
        return result

# ========== КОМАНДЫ БОТА ==========

@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    bot.reply_to(message, """
🚀 УНИВЕРСАЛЬНЫЙ КЛОНЕР САЙТОВ v2.0

Поддерживает:
📱 Мобильные сайты
⚡ SPA (React/Vue/Angular)
🌐 Статические сайты
🇷🇺 Российские сайты (max.ru, vk.com и др.)

Команды:
/clone URL — клонировать сайт
/clone_spa URL — SPA рендеринг
/clone_mobile URL — мобильная версия
/list — список готовых клонов
/get ID — скачать архив
/status — статус задач

Примеры:
/clone https://max.ru
/clone https://vk.com
/clone_spa https://example-react-site.com
""")

@bot.message_handler(commands=['clone', 'clone_spa', 'clone_mobile'])
def clone_site(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Укажи URL: /clone https://site.com")
        return
    
    url = args[1]
    if not url.startswith('http'):
        url = 'https://' + url
    
    # Определяем метод
    if message.text.startswith('/clone_spa'):
        method = 'spa'
    elif message.text.startswith('/clone_mobile'):
        method = 'mobile'
    else:
        method = 'auto'
    
    task_id = str(int(time.time()))[-8:]
    ACTIVE_TASKS[task_id] = {
        'url': url,
        'status': 'cloning',
        'chat_id': message.chat.id,
        'started': time.time(),
        'method': method
    }
    
    msg = bot.reply_to(message, f"🔔 Задача #{task_id}\n🎯 {url}\n⚙️ Метод: {method}\n⏳ Клонирование...")
    
    def process():
        cloner = UniversalCloner(url, task_id, message.chat.id, method)
        
        if method == 'spa':
            result = cloner.clone_spa()
        else:
            result = cloner.clone()
        
        if result:
            # Архивируем
            zip_path = f"/tmp/clones/{task_id}.zip"
            shutil.make_archive(f"/tmp/clones/{task_id}", 'zip', cloner.output_dir)
            
            if os.path.exists(zip_path):
                ACTIVE_TASKS[task_id]['status'] = 'done'
                ACTIVE_TASKS[task_id]['file'] = zip_path
                size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                
                caption = f"✅ #{task_id} ГОТОВ!\n🎯 {url}\n📦 {size_mb:.1f} MB\n🏷 Метод: {result.get('method', 'unknown')}\n📎 Ресурсов: {result.get('assets', 0)}"
                
                # Отправляем скриншот если есть
                preview_path = f"{cloner.output_dir}/preview.png"
                if os.path.exists(preview_path):
                    with open(preview_path, 'rb') as pf:
                        bot.send_photo(message.chat.id, pf, caption=caption)
                else:
                    bot.send_message(message.chat.id, caption)
                
                bot.send_message(message.chat.id, f"📥 Скачать: /get {task_id}")
            else:
                ACTIVE_TASKS[task_id]['status'] = 'failed'
                bot.send_message(message.chat.id, f"❌ #{task_id} Ошибка архивации")
        else:
            ACTIVE_TASKS[task_id]['status'] = 'failed'
            bot.send_message(message.chat.id, f"❌ #{task_id} Не удалось клонировать")
    
    threading.Thread(target=process).start()

@bot.message_handler(commands=['get'])
def get_file(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Укажи ID: /get 12345678")
        return
    
    task_id = args[1]
    task = ACTIVE_TASKS.get(task_id)
    
    if not task or task['status'] != 'done':
        bot.reply_to(message, "❌ Файл не готов")
        return
    
    if not os.path.exists(task.get('file', '')):
        bot.reply_to(message, "❌ Файл не найден на сервере")
        return
    
    with open(task['file'], 'rb') as f:
        bot.send_document(
            message.chat.id, 
            f, 
            visible_file_name=f"clone_{task_id}_{task['url'].replace('https://','').replace('/','_')}.zip",
            caption=f"🎯 {task['url']}\n📅 {time.ctime(task['started'])}"
        )

@bot.message_handler(commands=['list'])
def list_clones(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    done = {k: v for k, v in ACTIVE_TASKS.items() if v['status'] == 'done'}
    if not done:
        bot.reply_to(message, "📭 Нет готовых клонов")
        return
    
    text = "📦 ГОТОВЫЕ КЛОНЫ:\n\n"
    for tid, t in done.items():
        try:
            size = os.path.getsize(t['file']) / (1024 * 1024)
            text += f"#{tid} | {size:.1f}MB | {t['url']}\n/get {tid}\n\n"
        except:
            pass
    
    bot.reply_to(message, text)

@bot.message_handler(commands=['status'])
def status(message):
    if not ACTIVE_TASKS:
        bot.reply_to(message, "📭 Нет активных задач")
        return
    
    text = "📊 ЗАДАЧИ:\n\n"
    for tid, t in ACTIVE_TASKS.items():
        emoji = {'cloning': '⏳', 'done': '✅', 'failed': '❌'}.get(t['status'], '❓')
        text += f"{emoji} #{tid} | {t['method']} | {t['url']}\n"
    
    bot.reply_to(message, text)

# ========== ЗАПУСК ==========
def run_bot():
    bot.infinity_polling()

if __name__ == '__main__':
    print("🚀 Universal Site Cloner Bot v2.0 запущен!")
    threading.Thread(target=run_bot).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
