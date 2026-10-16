# -*- coding: utf-8 -*-
"""release_center.exec_on_target_stream 的降级链探针（真实起 web_shell 与桩服务）。

场景：
A. 目标机是 web_shell v1.3.0（支持 POST /api/exec/stream）
      → 走 POST，streamed=True，命令不落在 URI 上（桩内记录收到的请求行）
B. 目标机是老 web_shell（只有 GET 查询串）
      → POST 拿不到 200 → 自动退回 GET 查询串，streamed=True
C. 目标机两样都没有（连不上流式接口）
      → 退回同步 /api/exec，且**命令只被执行一次**（不允许重放）
"""
from __future__ import print_function

import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opslib import compat, sse                                              # noqa: E402
from platform_console import release_center as RC                           # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
results = []
seen = {'post_stream': 0, 'get_stream': 0, 'sync': 0}


def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print('%s %s%s' % ('[PASS]' if cond else '[FAIL]', name,
                       ('  %s' % extra) if extra and not cond else ''))


class StubHandler(compat.BaseHTTPRequestHandler):
    """按 MODE 模拟不同版本的目标机 web_shell。"""
    protocol_version = 'HTTP/1.1'
    MODE = 'old'          # new / old / broken

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = compat.json_bytes(obj)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        """SSEStream.ready() 会调用它补充 CORS 头，桩里空实现即可。"""
        pass

    def _sse(self, cmd):
        # 注意：不要自己 send_response/send_header —— SSEStream.ready() 会负责发
        # 状态行与分块头，重复发送会写出两个状态行，客户端解析直接失败。
        stream = sse.SSEStream(self)
        stream.ready()
        stream.json({'cmd': cmd, 'timeout': 5}, event='start')
        stream.send('stream-ok:%s\n' % cmd, event='out')
        stream.json({'returncode': 0, 'elapsed': 0.01, 'truncated': False,
                     'timed_out': False}, event='exit')
        stream.close()

    def do_POST(self):
        path = compat.urlparse(self.path).path
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        if path == '/api/exec/stream':
            seen['post_stream'] += 1
            if self.MODE == 'new':
                cmd = (compat.json_loads(compat.to_text(raw), {}) or {}).get('cmd', '')
                self._sse(cmd)
            else:
                self._json(404, {'status': 'error', 'message': '接口不存在'})
            return
        if path == '/api/exec':
            seen['sync'] += 1
            self._json(200, {'status': 'success', 'code': 0, 'stdout': 'sync-ok\n',
                             'stderr': '', 'duration': 0.01,
                             'timed_out': False, 'truncated': False})
            return
        self._json(404, {'status': 'error', 'message': '接口不存在'})

    def do_GET(self):
        parsed = compat.urlparse(self.path)
        path = parsed.path
        if path == '/api/exec/stream':
            seen['get_stream'] += 1
            if self.MODE == 'old':
                qs = compat.urlparse_qs(parsed.query)
                cmd = (qs.get('cmd') or [''])[0]
                self._sse(cmd)
            else:
                self._json(404, {'status': 'error', 'message': '接口不存在'})
            return
        self._json(404, {'status': 'error', 'message': '接口不存在'})


def start_stub(mode, port):
    StubHandler.MODE = mode
    httpd = compat.ThreadingHTTPServer(('127.0.0.1', port), StubHandler)
    compat.start_daemon(httpd.serve_forever)
    return httpd


def main():
    # ---------- A. 真 web_shell v1.3.0 ----------
    port_ws = 18097
    tmp = os.path.join(BASE, '.testlogs', 'rc_stream_tmp')
    if not os.path.isdir(tmp):
        os.makedirs(tmp)
    proc = subprocess.Popen([PY, '-u', os.path.join(BASE, 'tools', 'web_shell.py'),
                             '--host', '127.0.0.1', '--port', str(port_ws),
                             '--token', 'T', '--cwd', tmp],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            time.sleep(0.25)
            try:
                r = __import__('opslib.httpx', fromlist=['x']).DEFAULT.get(
                    'http://127.0.0.1:%d/api/ping' % port_ws,
                    headers={'X-Auth-Token': 'T'}, timeout=3)
                if r.ok:
                    break
            except Exception:
                continue
        got = []

        def on_out(t):
            got.append(t)

        res = RC.exec_on_target_stream(
            {'host': '127.0.0.1', 'shell_port': port_ws, 'shell_token': 'T'},
            'echo rc-stream-ok', 15, on_output=on_out)
        check('A. 新版 web_shell：走流式且拿到输出',
              res.get('streamed') and res.get('code') == 0
              and 'rc-stream-ok' in ''.join(got),
              'res=%s out=%s' % ({k: res.get(k) for k in ('streamed', 'code')}, ''.join(got)))
    finally:
        try:
            proc.terminate()
            proc.wait()
        except Exception:
            pass

    # ---------- B. 老 web_shell（只有 GET 查询串） ----------
    port_old = 18096
    httpd_old = start_stub('old', port_old)
    try:
        got = []
        res = RC.exec_on_target_stream(
            {'host': '127.0.0.1', 'shell_port': port_old, 'shell_token': 'T'},
            'echo legacy', 15, on_output=got.append)
        check('B. 老版 web_shell：POST 失败后自动退回 GET 查询串',
              res.get('streamed') and 'stream-ok:echo legacy' in ''.join(got),
              'res=%s out=%s post=%d get=%d' % ({k: res.get(k) for k in ('streamed', 'code')},
                                                ''.join(got), seen['post_stream'],
                                                seen['get_stream']))
        check('B. 确实先试了 POST、再退 GET',
              seen['post_stream'] == 1 and seen['get_stream'] == 1,
              'post=%d get=%d' % (seen['post_stream'], seen['get_stream']))
    finally:
        httpd_old.shutdown()

    # ---------- C. 两种流式都不可用 → 同步降级且只执行一次 ----------
    port_bad = 18095
    httpd_bad = start_stub('broken', port_bad)
    try:
        base_post, base_get, base_sync = (seen['post_stream'], seen['get_stream'],
                                          seen['sync'])
        got = []
        res = RC.exec_on_target_stream(
            {'host': '127.0.0.1', 'shell_port': port_bad, 'shell_token': 'T'},
            'echo sync-only', 15, on_output=got.append)
        check('C. 流式不可用时退回同步接口',
              res.get('streamed') is False and res.get('code') == 0
              and 'sync-ok' in ''.join(got),
              'res=%s out=%s' % ({k: res.get(k) for k in ('streamed', 'code', 'stream_error')},
                                 ''.join(got)))
        check('C. 命令只被执行一次（同步 1 次，未重放）',
              seen['sync'] - base_sync == 1,
              'sync=%d post=%d get=%d' % (seen['sync'] - base_sync,
                                          seen['post_stream'] - base_post,
                                          seen['get_stream'] - base_get))
    finally:
        httpd_bad.shutdown()

    failed = [n for n, okk in results if not okk]
    print('\n结果：%d/%d 通过' % (len(results) - len(failed), len(results)))
    if failed:
        print('失败项：%s' % '; '.join(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
