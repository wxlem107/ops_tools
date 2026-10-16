# -*- coding: utf-8 -*-
import socket
import sys
import time

sys.path.insert(0, 'C:/Users/Administrator/Desktop/git/wxl-gitee/shell/xpe/linux/python/workboddy/ops_tools')
from opslib import httpx

BASE = 'http://127.0.0.1:8095'
TOKEN = 'tok123'


def show(label, resp):
    body = resp.text[:300] if resp.body else ''
    print('[%s] status=%s body=%s' % (label, resp.status, body.replace('\n', ' ')))


def get(path, token=''):
    h = {'X-Auth-Token': token} if token else {}
    return httpx.DEFAULT.get(BASE + path, headers=h, timeout=5)


def post(path, body, token=''):
    h = {'X-Auth-Token': token} if token else {}
    return httpx.DEFAULT.post(BASE + path, json_body=body, headers=h, timeout=5)


print('=== 1. ping ===')
show('ping', get('/api/ping'))

print('=== 2. health ===')
show('health', get('/api/health'))

print('=== 3. openapi ===')
r = get('/api/openapi.json')
print('openapi status=%s len=%d' % (r.status, len(r.body)))

print('=== 4. /api/servers 未登录(应401) ===')
show('servers-noauth', get('/api/servers'))

print('=== 5. /api/servers 带token(应200) ===')
show('servers-token', get('/api/servers', TOKEN))

print('=== 6. /api/login ===')
r = post('/api/login', {'username': 'admin', 'password': 'secret123'})
show('login', r)
login_token = ''
if r.status == 200:
    login_token = (r.json({}) or {}).get('token', '')

print('=== 7. /api/me ===')
show('me', get('/api/me', login_token))

print('=== 8. /api/meta ===')
show('meta', get('/api/meta', TOKEN))

print('=== 9. /api/cmdb (空) ===')
show('cmdb', get('/api/cmdb', TOKEN))

print('=== 10. /api/audit ===')
show('audit', get('/api/audit', TOKEN))

print('=== 11. /api/tasks ===')
show('tasks', get('/api/tasks', TOKEN))

print('=== 12. /api/release/config (release未启动应 offline) ===')
show('release-config', get('/api/release/config', TOKEN))

print('=== 13. /api/events SSE 读取（先订阅再回推） ===')
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(('127.0.0.1', 8095))
req = 'GET /api/events?topics=alert HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Auth-Token: %s\r\nConnection: close\r\n\r\n' % TOKEN
s.sendall(req.encode('utf-8'))
time.sleep(0.3)
post('/api/events/push', {'topic': 'alert', 'data': {'test': 1}}, TOKEN)
buf = ''
deadline = time.time() + 3
while time.time() < deadline:
    try:
        chunk = s.recv(4096)
    except Exception:
        break
    if not chunk:
        break
    buf += chunk.decode('utf-8', 'ignore')
    if 'data:' in buf and 'test' in buf:
        break
s.close()
print('SSE 收到: %s' % buf[:400].replace('\n', ' | '))

print('=== 验证结束 ===')
