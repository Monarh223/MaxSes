import requests
from bs4 import BeautifulSoup
import os
import shutil
from urllib.parse import urljoin, urlparse
import time

TARGET = "https://max.ru"
OUTPUT_DIR = "max_ru_full"

# Очистка и создание папок
if os.path.exists(OUTPUT_DIR):
    shutil.rmtree(OUTPUT_DIR)
os.makedirs(OUTPUT_DIR)
os.makedirs(f"{OUTPUT_DIR}/css", exist_ok=True)
os.makedirs(f"{OUTPUT_DIR}/js", exist_ok=True)
os.makedirs(f"{OUTPUT_DIR}/images", exist_ok=True)
os.makedirs(f"{OUTPUT_DIR}/fonts", exist_ok=True)

# СЕССИЯ С МОБИЛЬНЫМ User-Agent
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ru-RU,ru;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Dest': 'document',
})

def download_asset(url, folder):
    """Скачивает ассет с реферером"""
    try:
        if not url.startswith('http'):
            url = urljoin(TARGET, url)
        
        # Пропускаем внешние CDN
        if 'max.ru' not in url and 'vk.com' not in url:
            return None
        
        filename = os.path.basename(urlparse(url).path.split('?')[0])
        if not filename or len(filename) < 3:
            filename = f"asset_{hash(url)%10000}"
        
        # Добавляем расширение если нет
        if '.' not in filename:
            if 'css' in url:
                filename += '.css'
            elif 'js' in url or 'javascript' in url:
                filename += '.js'
            else:
                filename += '.bin'
        
        filepath = f"{OUTPUT_DIR}/{folder}/{filename}"
        
        headers = {
            'Referer': TARGET,
            'Origin': TARGET,
        }
        
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            print(f"  ✅ {folder}/{filename} ({len(resp.content)} bytes)")
            return f"{folder}/{filename}"
        else:
            print(f"  ⚠️ Пропущен: {url} (статус: {resp.status_code}, размер: {len(resp.content)})")
            return None
    except Exception as e:
        print(f"  ❌ Ошибка: {url} — {str(e)[:50]}")
        return None

# ========== ЗАГРУЖАЕМ ГЛАВНУЮ ==========
print(f"[*] Загружаем {TARGET} как мобильное устройство...")

try:
    # РАЗРЕШАЕМ РЕДИРЕКТЫ и следуем за ними
    response = session.get(TARGET, allow_redirects=True, timeout=20)
    
    print(f"[*] Финальный URL после редиректов: {response.url}")
    print(f"[*] Статус: {response.status_code}")
    print(f"[*] Content-Type: {response.headers.get('Content-Type', 'неизвестно')}")
    print(f"[*] Размер страницы: {len(response.text)} символов\n")
    
    if response.status_code != 200:
        print(f"❌ Ошибка загрузки: HTTP {response.status_code}")
        exit(1)
    
    # Сохраняем сырой HTML для отладки
    with open(f'{OUTPUT_DIR}/debug_source.html', 'w', encoding='utf-8') as f:
        f.write(response.text)
    print("[*] Исходный HTML сохранён в debug_source.html")
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Ищем ВСЕ возможные ресурсы
    print("\n[*] Скачиваем ВСЕ ресурсы...")
    print("=" * 50)
    
    # CSS: link, style, @import
    for tag in soup.find_all(['link', 'style']):
        if tag.name == 'link' and tag.get('href'):
            href = tag['href']
            if any(x in href.lower() for x in ['.css', 'style', 'font']):
                local = download_asset(href, 'css' if '.css' in href else 'fonts')
                if local:
                    tag['href'] = local
    
    # JavaScript
    for tag in soup.find_all('script', src=True):
        local = download_asset(tag['src'], 'js')
        if local:
            tag['src'] = local
    
    # Изображения
    for tag in soup.find_all('img', src=True):
        local = download_asset(tag['src'], 'images')
        if local:
            tag['src'] = local
    
    # SVG и иконки
    for tag in soup.find_all(['use', 'object'], href=True):
        local = download_asset(tag['href'], 'images')
        if local:
            tag['href'] = local
    
    # Фоновые изображения в style атрибутах
    import re
    for tag in soup.find_all(style=True):
        urls = re.findall(r'url\([\'"]?([^\'")]+)[\'"]?\)', str(tag.get('style')))
        for url in urls:
            local = download_asset(url, 'images')
            if local:
                tag['style'] = str(tag['style']).replace(url, local)
    
    # ВСЕ ссылки делаем локальными
    for tag in soup.find_all(href=True):
        href = tag['href']
        if href.startswith('http') and 'max.ru' in href:
            tag['href'] = href.replace('https://max.ru', '').replace('http://max.ru', '')
    
    for tag in soup.find_all(src=True):
        src = tag['src']
        if src.startswith('http') and 'max.ru' in src:
            tag['src'] = src.replace('https://max.ru', '').replace('http://max.ru', '')
    
    # Сохраняем обработанный HTML
    output_file = f'{OUTPUT_DIR}/index.html'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('<!DOCTYPE html>\n')
        f.write('<html lang="ru">\n')
        f.write('<head>\n')
        f.write('<meta charset="UTF-8">\n')
        f.write('<meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
        f.write('<title>MAX</title>\n')
        f.write('<base href="/">\n')
        f.write('</head>\n')
        f.write('<body>\n')
        f.write(str(soup))
        f.write('\n</body>\n')
        f.write('</html>')
    
    # Статистика
    total_files = 0
    for root, dirs, files in os.walk(OUTPUT_DIR):
        total_files += len(files)
    
    print("\n" + "=" * 50)
    print(f"✅ КЛОНИРОВАНИЕ ЗАВЕРШЕНО!")
    print(f"📁 Папка: {OUTPUT_DIR}/")
    print(f"📄 Главная: {OUTPUT_DIR}/index.html")
    print(f"📦 Всего файлов: {total_files}")
    print(f"📊 Размер: {sum(os.path.getsize(os.path.join(root, name)) for root, dirs, files in os.walk(OUTPUT_DIR) for name in files) / 1024:.1f} KB")
    
except Exception as e:
    print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
    import traceback
    traceback.print_exc()
