# -*- coding: utf-8 -*-
"""web_shell v1.3.0 三种执行入口的端到端探针（真实起服务）。

覆盖：
1. POST /api/exec/stream（命令在请求体）—— release_center 走的路径
2. POST /api/exec/start 换券 + GET /api/exec/stream?ticket= 取流（券一次性）
3. GET /api/exec/stream?cmd=... 旧形态兼容
4. POST /api/sessions/<id>/stream
跑完打印 PASS/FAIL 明细。
"""
from __future__ import print_function

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opslib import compat, sse, httpx                                   # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PORT = 18099
TOKEN = 'WSPOST'
HOST = '127.0.0.1'
URL = 'http://%s:%d' % (HOST, PORT)

results = []


def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print('%s %s%s' % ('[PASS]' if cond else '[FAIL]', name,
                       ('  %s' % extra) if extra and not cond else ''))


def collect(url, **kw):
    """收流：返回 (events, ok, err)"""
    evs = []

    def on_event(name, data):
        evs.append((name, data))

    ok, err = sse.consume(url, headers={'X-Auth-Token': TOKEN}, on_event=on_event, **kw)
    return evs, ok, err


def out_text(evs):
    return ''.join(d for n, d in evs if n == 'out')


def main():
    tmp = os.path.join(BASE, '.testlogs', 'ws_post_probe_tmp')
    if not os.path.isdir(tmp):
        os.makedirs(tmp)
    proc = subprocess.Popen([PY, '-u', os.path.join(BASE, 'tools', 'web_shell.py'),
                             '--host', HOST, '--port', str(PORT), '--token', TOKEN,
                             '--cwd', tmp],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # 等服务就绪
        ready = False
        for _ in range(40):
            time.sleep(0.25)
            try:
                r = httpx.DEFAULT.get(URL + '/api/ping', headers={'X-Auth-Token': TOKEN},
                                      timeout=3)
                if r.ok:
                    ready = True
                    break
            except Exception:
                continue
        check('web_shell 启动并就绪', ready)
        if not ready:
            return

        # ---------- 1. POST /api/exec/stream（命令在请求体） ----------
        body = compat.to_text(compat.json_bytes({'cmd': 'echo post-body-ok', 'timeout': 10}))
        evs, ok, err = collect(URL + '/api/exec/stream', timeout=20,
                               method='POST', body=body)
        names = [n for n, _ in evs]
        check('POST /api/exec/stream 收到 start/out/exit',
              ok and 'start' in names and 'out' in names and 'exit' in names,
              'ok=%s err=%s names=%s' % (ok, err, names))
        check('POST /api/exec/stream 输出正确', 'post-body-ok' in out_text(evs),
              repr(out_text(evs)))

        # ---------- 2. 换券 + 凭券取流 ----------
        r = httpx.DEFAULT.post(URL + '/api/exec/start',
                               json_body={'cmd': 'echo ticket-ok', 'timeout': 10},
                               headers={'X-Auth-Token': TOKEN}, timeout=10)
        data = r.json() or {}
        ticket = data.get('ticket') or ''
        check('POST /api/exec/start 返回券', r.ok and data.get('status') == 'success'
              and len(ticket) >= 16, 'status=%s data=%s' % (r.status, data))
        evs, ok, err = collect(URL + '/api/exec/stream?ticket=' + ticket, timeout=20)
        check('凭券取流收到 out=ticket-ok', ok and 'ticket-ok' in out_text(evs),
              'ok=%s err=%s out=%s' % (ok, err, out_text(evs)))
        # 券一次性
        r2 = httpx.DEFAULT.get(URL + '/api/exec/stream?ticket=' + ticket, timeout=10)
        check('同一张券不能用第二次', r2.status == 403, 'status=%s' % r2.status)
        # 无 token 也不能凭空取流
        r3 = httpx.DEFAULT.get(URL + '/api/exec/stream?cmd=echo%20x&timeout=5', timeout=10)
        check('旧形态仍要求 token（401）', r3.status == 401, 'status=%s' % r3.status)

        # ---------- 3. 旧形态兼容（GET + 查询串） ----------
        evs, ok, err = collect(URL + '/api/exec/stream?cmd=echo%20legacy-ok&timeout=10',
                               timeout=20)
        check('GET 旧形态仍然可用', ok and 'legacy-ok' in out_text(evs),
              'ok=%s err=%s out=%s' % (ok, err, out_text(evs)))

        # ---------- 4. 会话流 POST ----------
        r = httpx.DEFAULT.post(URL + '/api/sessions', json_body={'cwd': tmp},
                               headers={'X-Auth-Token': TOKEN}, timeout=10)
        sdata = r.json() or {}
        sid = (sdata.get('session') or {}).get('session_id') or sdata.get('session_id') or ''
        check('创建会话', r.ok and bool(sid), 'data=%s' % sdata)
        if sid:
            sbody = compat.to_text(compat.json_bytes({'cmd': 'echo sess-body-ok',
                                                      'timeout': 10}))
            evs, ok, err = collect(URL + '/api/sessions/%s/stream' % sid, timeout=20,
                                   method='POST', body=sbody)
            check('POST 会话流收到输出', ok and 'sess-body-ok' in out_text(evs),
                  'ok=%s err=%s out=%s' % (ok, err, out_text(evs)))
    finally:
        try:
            proc.terminate()
            proc.wait()
        except Exception:
            pass

    failed = [n for n, okk in results if not okk]
    print('\n结果：%d/%d 通过' % (len(results) - len(failed), len(results)))
    if failed:
        print('失败项：%s' % '; '.join(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
