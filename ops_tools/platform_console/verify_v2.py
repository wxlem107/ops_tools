# -*- coding: utf-8 -*-
"""verify_v2.py —— ops_Platform v2.0 端到端验证脚本（仅标准库 + opslib）。

【用法示例】
    # 前置：先以固定令牌启动平台（脚本内 BASE/TOKEN 为常量，见「全部参数」）
    python ops_Platform.py --port 8095 --token tok123

    # 运行端到端验证（默认针对 http://127.0.0.1:8095，令牌 tok123）
    python verify_v2.py

    # 退出码：全部通过 0，有失败 1（可直接接入 CI）
    python verify_v2.py && echo "ALL PASS"

【全部参数】
    无命令行参数。目标地址与令牌为脚本顶部常量（如需改目标，改这两行）：
      BASE  = 'http://127.0.0.1:8095'   平台地址
      TOKEN = 'tok123'                  平台开放接口令牌（须与启动时 --token 一致）
"""
from __future__ import print_function

import os
import sys
import time
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # ops_tools/
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)

from opslib import compat
from opslib.httpx import Client

BASE = 'http://127.0.0.1:8095'
TOKEN = 'tok123'

PASS = 0
FAIL = 0


def check(name, ok, extra=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print('  [PASS] %s %s' % (name, extra))
    else:
        FAIL += 1
        print('  [FAIL] %s %s' % (name, extra))


def main():
    c = Client(timeout=8)

    # 1. ping
    r = c.request('GET', BASE + '/api/ping')
    check('GET /api/ping', r.ok, 'ver=%s' % r.json().get('version') if r.ok else r.status)

    # 2. health
    r = c.request('GET', BASE + '/api/health')
    check('GET /api/health', r.ok, 'status=%s' % (r.json().get('status') if r.ok else r.status))

    # 3. openapi (no auth)
    r = c.request('GET', BASE + '/api/openapi.json')
    check('GET /api/openapi.json', r.ok, 'len=%d' % len(r.body))

    # 4. servers no-auth -> 401
    r = c.request('GET', BASE + '/api/servers')
    check('GET /api/servers (no auth) -> 401', r.status == 401, 'status=%d' % r.status)

    # 5. servers with token -> 200
    r = c.request('GET', BASE + '/api/servers', headers={'X-Auth-Token': TOKEN})
    check('GET /api/servers (token) -> 200', r.ok, 'status=%d' % r.status)

    # 6. login -> token
    r = c.request('POST', BASE + '/api/login',
                  headers={'Content-Type': 'application/json'},
                  body=json.dumps({'username': 'admin', 'password': 'secret123'}))
    login_token = ''
    if r.ok:
        login_token = r.json().get('token') or ''
    check('POST /api/login -> token', r.ok and bool(login_token), 'token_len=%d' % len(login_token))

    # 7. me (legacy API token resolves to __api__ admin-equivalent)
    r = c.request('GET', BASE + '/api/me', headers={'X-Auth-Token': TOKEN})
    me = r.json() if r.ok else {}
    check('GET /api/me', r.ok and me.get('role') == 'admin', 'user=%s role=%s' % (me.get('username'), me.get('role')))

    # 8. meta
    r = c.request('GET', BASE + '/api/meta', headers={'X-Auth-Token': TOKEN})
    check('GET /api/meta', r.ok, 'groups=%s' % ((r.json().get('data') or {}).get('groups')))

    # 9. cmdb
    r = c.request('GET', BASE + '/api/cmdb', headers={'X-Auth-Token': TOKEN})
    check('GET /api/cmdb', r.ok, 'status=%s' % r.status)

    # 10. audit
    r = c.request('GET', BASE + '/api/audit', headers={'X-Auth-Token': TOKEN})
    check('GET /api/audit', r.ok, 'status=%s' % r.status)

    # 11. tasks
    r = c.request('GET', BASE + '/api/tasks', headers={'X-Auth-Token': TOKEN})
    check('GET /api/tasks', r.ok, 'status=%s' % r.status)

    # 12. release/config (offline proxy)
    r = c.request('GET', BASE + '/api/release/config', headers={'X-Auth-Token': TOKEN})
    jr = r.json() if r.ok else {}
    check('GET /api/release/config', r.ok and jr.get('status') == 'success',
          'url=%s online=%s' % (jr.get('url'), jr.get('online')))

    # 13. events SSE: subscribe then push then read event
    evt_ok = False
    evt_extra = ''
    try:
        parsed = compat.urlparse(BASE)
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port or 80
        conn = compat.http_client.HTTPConnection(host, port, timeout=5)
        conn.request('GET', '/api/events?topics=alert',
                     headers={'X-Auth-Token': TOKEN, 'Accept': 'text/event-stream'})
        resp = conn.getresponse()
        if resp.status == 200:
            # 先建立订阅，稍后推送（给服务端注册订阅者留足时间）
            time.sleep(0.6)
            pr = c.request('POST', BASE + '/api/events/push',
                           headers={'X-Auth-Token': TOKEN, 'Content-Type': 'application/json'},
                           body=json.dumps({'topic': 'alert', 'data': {'msg': 'verify'}}))
            if pr.ok:
                conn.sock.settimeout(5)
                deadline = time.time() + 5
                buf = ''
                while time.time() < deadline:
                    line = compat.to_text(resp.readline())
                    if not line:
                        break
                    buf += line
                    if 'verify' in buf:
                        evt_ok = True
                        break
            else:
                evt_extra = 'push_status=%s' % pr.status
        evt_extra = 'status=%s got=%s' % (resp.status, evt_ok)
        check('GET /api/events (SSE alert)', evt_ok, evt_extra)
        conn.close()
    except Exception as e:
        check('GET /api/events (SSE alert)', False, 'exc=%s' % e)

    print('\n==== RESULT: %d passed, %d failed ====' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
