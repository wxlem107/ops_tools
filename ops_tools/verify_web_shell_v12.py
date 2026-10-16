# -*- coding: utf-8 -*-
"""web_shell.py v1.2.0 验证脚本（仅标准库）。
用法: python verify_web_shell_v12.py <port> <token> [mode]
mode: full(默认) / whitelist / ttl
"""
from __future__ import print_function
import sys
import os
import re
import json
import time
import urllib.parse
import http.client

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19100
TOKEN = sys.argv[2] if len(sys.argv) > 2 else 'admin123'
MODE = sys.argv[3] if len(sys.argv) > 3 else 'full'
HOST = '127.0.0.1'


def _local_version():
    """从本仓库 tools/web_shell.py 读取 VERSION，避免每次升级都要手改这里。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'web_shell.py')
    try:
        with open(p, 'rb') as f:
            m = re.search(rb"VERSION\s*=\s*'([^']+)'", f.read())
        return m.group(1).decode('ascii') if m else ''
    except Exception:                                        # noqa: BLE001
        return ''


EXPECT_VER = _local_version() or '1.3.1'

PASS = 0
FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  [PASS] %s %s' % (name, extra))
    else:
        FAIL += 1
        print('  [FAIL] %s %s' % (name, extra))


def req(method, path, body=None, ctype='application/json', timeout=30):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    headers = {'X-Auth-Token': TOKEN}
    data = None
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            data = body.encode('utf-8')
        else:
            data = body
        headers['Content-Type'] = ctype
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    finally:
        conn.close()


def jreq(method, path, body=None):
    status, raw = req(method, path, body)
    try:
        return status, json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return status, {'_raw': raw.decode('utf-8', 'replace')}


def read_sse(path, timeout=20):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout + 10)
    conn.request('GET', path, headers={'X-Auth-Token': TOKEN})
    resp = conn.getresponse()
    events = []
    cur_event = ''
    cur_data = []
    t0 = time.time()
    while True:
        if time.time() - t0 > timeout:
            break
        line = resp.readline()
        if not line:
            break
        line = line.decode('utf-8', 'replace')
        if line in ('\n', '\r\n', ''):
            if cur_event or cur_data:
                events.append((cur_event or 'message', '\n'.join(cur_data)))
                cur_event = ''
                cur_data = []
            continue
        if line.startswith('event:'):
            cur_event = line[6:].strip()
        elif line.startswith('data:'):
            d = line[5:]
            if d.startswith(' '):
                d = d[1:]
            cur_data.append(d.rstrip('\n'))
    conn.close()
    return events


def q(s):
    return urllib.parse.quote(s, safe='')


# ====================== FULL 模式 ======================
def run_full():
    print('\n=== 基础接口 ===')
    st, body = jreq('GET', '/api/health')
    check('health 200', st == 200, str(body.get('version')))
    check('health version=%s' % EXPECT_VER, body.get('version') == EXPECT_VER)
    st, body = jreq('GET', '/api/ping')
    check('ping 200', st == 200)

    print('\n=== 旧接口 /api/exec（阻塞） ===')
    st, body = jreq('POST', '/api/exec', {'cmd': 'echo hello_v12', 'timeout': 10})
    check('exec 200', st == 200)
    check('exec 输出包含 hello_v12', 'hello_v12' in body.get('stdout', ''), repr(body.get('stdout', ''))[:80])

    print('\n=== SSE /api/exec/stream ===')
    evs = read_sse('/api/exec/stream?cmd=%s&timeout=10' % q('echo stream_ok'))
    names = [e[0] for e in evs]
    check('stream 收到 start', 'start' in names, str(names))
    check('stream 收到 out', 'out' in names)
    check('stream 收到 exit', 'exit' in names)
    exit_ev = [e for e in evs if e[0] == 'exit']
    if exit_ev:
        try:
            m = json.loads(exit_ev[-1][1])
            check('stream exit.returncode=0', m.get('returncode') == 0, str(m))
        except Exception as e:
            check('stream exit 可解析', False, str(e))

    print('\n=== 黑名单（默认启用） ===')
    st, body = jreq('POST', '/api/exec', {'cmd': 'rm -rf /', 'timeout': 10})
    check('危险命令被拒绝(403)', st == 403, str(body.get('message')))

    print('\n=== 无令牌应 401 ===')
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    conn.request('GET', '/api/health')
    r = conn.getresponse(); conn.close()
    check('无令牌 health 401', r.status == 401, 'status=%d' % r.status)

    print('\n=== 持久会话 ===')
    st, body = jreq('POST', '/api/sessions', {})
    check('创建会话 200', st == 200, str(body.get('session_id')))
    sid = body.get('session_id')
    check('会话返回 cwd', bool(body.get('cwd')), body.get('cwd'))
    cwd0 = body.get('cwd')

    # cd 到临时目录，再 cd（打印当前目录）确认持久
    tmpdir = 'C:\\Users\\Administrator'
    st, body = jreq('POST', '/api/sessions/%s/exec' % sid, {'cmd': 'cd ' + tmpdir, 'timeout': 10})
    check('会话 cd 200', st == 200, str(body.get('returncode')))
    st, body = jreq('POST', '/api/sessions/%s/exec' % sid, {'cmd': 'cd', 'timeout': 10})
    check('会话 cd 打印持久目录', tmpdir.lower() in (body.get('output', '') or '').lower(), repr(body.get('output'))[:80])
    check('会话 cwd 已更新', (body.get('cwd') or '').lower() == tmpdir.lower(), body.get('cwd'))

    # 环境变量持久
    st, body = jreq('POST', '/api/sessions/%s/exec' % sid, {'cmd': 'set MYVAR=hello123', 'timeout': 10})
    check('会话 set 200', st == 200)
    st, body = jreq('POST', '/api/sessions/%s/exec' % sid, {'cmd': 'echo %MYVAR%', 'timeout': 10})
    check('会话 环境变量持久', 'hello123' in (body.get('output', '') or ''), repr(body.get('output'))[:80])

    # 会话列表
    st, body = jreq('GET', '/api/sessions')
    check('会话列表 200', st == 200)
    ids = [s['session_id'] for s in body.get('sessions', [])]
    check('列表含本会话', sid in ids, str(ids))

    # 会话 SSE 实时
    evs = read_sse('/api/sessions/%s/stream?cmd=%s&timeout=10' % (sid, q('echo sess_stream')))
    names = [e[0] for e in evs]
    check('会话stream start', 'start' in names, str(names))
    check('会话stream out', 'out' in names)
    check('会话stream exit', 'exit' in names)

    # 会话内超时保护
    print('\n=== 会话超时保护（timeout=1 跑长命令） ===')
    t0 = time.time()
    st, body = jreq('POST', '/api/sessions/%s/exec' % sid, {'cmd': 'ping -n 3 127.0.0.1', 'timeout': 1})
    el = time.time() - t0
    check('会话超时返回', st == 200 and body.get('timed_out') is True, 'elapsed=%.1f returncode=%s' % (el, body.get('returncode')))

    # 关闭会话
    st, body = jreq('DELETE', '/api/sessions/%s' % sid)
    check('关闭会话 200', st == 200, str(body))
    st, body = jreq('GET', '/api/sessions')
    ids = [s['session_id'] for s in body.get('sessions', [])]
    check('会话已从列表移除', sid not in ids)

    print('\n=== 导出 /api/export ===')
    st, raw = req('GET', '/api/export?cmd=%s&timeout=10' % q('echo export_ok'))
    check('export 200', st == 200, 'len=%d' % len(raw))
    check('export 含内容', 'export_ok' in raw.decode('utf-8', 'replace'))


# ====================== WHITELIST 模式 ======================
def run_whitelist():
    print('\n=== 白名单（仅 echo） ===')
    st, body = jreq('POST', '/api/exec', {'cmd': 'echo allowed', 'timeout': 10})
    check('白名单内命令放行', st == 200, str(body.get('stdout'))[:40])
    st, body = jreq('POST', '/api/exec', {'cmd': 'cd C:\\\\Users', 'timeout': 10})
    check('白名单外命令拒绝(403)', st == 403, str(body.get('message')))
    # 会话内同样校验
    st, body = jreq('POST', '/api/sessions', {})
    sid = body.get('session_id')
    st, body = jreq('POST', '/api/sessions/%s/exec' % sid, {'cmd': 'cd C:\\\\Users', 'timeout': 10})
    check('会话内白名单外拒绝(403)', st == 403, str(body.get('message')))
    # 安全修复回归：白名单模式下必须拒绝命令替换（$() 与反引号），
    # 否则 `echo $(whoami)` 一类会把替换内容藏进占位符而绕过白名单。
    st, body = jreq('POST', '/api/exec', {'cmd': 'echo $(whoami)', 'timeout': 10})
    check('白名单模式拒绝 $() 命令替换(403)', st == 403, str(body.get('message')))
    st, body = jreq('POST', '/api/exec', {'cmd': 'echo `whoami`', 'timeout': 10})
    check('白名单模式拒绝反引号替换(403)', st == 403, str(body.get('message')))
    if sid:
        jreq('DELETE', '/api/sessions/%s' % sid)


# ====================== TTL 模式 ======================
def run_ttl():
    print('\n=== 会话空闲回收 (ttl=3s) ===')
    st, body = jreq('POST', '/api/sessions', {})
    sid = body.get('session_id')
    check('创建会话', st == 200, sid)
    time.sleep(10)
    st, body = jreq('GET', '/api/sessions')
    ids = [s['session_id'] for s in body.get('sessions', [])]
    check('空闲会话被回收', sid not in ids, 'remaining=%s' % ids)


if __name__ == '__main__':
    if MODE == 'whitelist':
        run_whitelist()
    elif MODE == 'ttl':
        run_ttl()
    else:
        run_full()
    print('\n==== 结果: PASS=%d FAIL=%d ====' % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
