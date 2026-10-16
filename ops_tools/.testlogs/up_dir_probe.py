# -*- coding: utf-8 -*-
"""upload_server v3.3.0「请求级存储根覆盖」冒烟测试（真实起两个实例）。

覆盖：
A. --allow-client-dir 开启：/api/chunk/put?dir= 与分片 init/complete 都落到请求指定目录
B. dir 传相对路径 → 400（配置错误不允许静默落到别处）
C. 未开开关：dir 被忽略，仍落 upload-dir，且响应带回真实路径（调用方可提示）
D. /api/status 暴露 allow_client_dir，供调用方探测能力
"""
from __future__ import print_function

import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opslib import compat, httpx                                            # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TMP = os.path.join('C:\\Windows\\Temp', 'up_dir_probe')
results = []


def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print('%s %s%s' % ('[PASS]' if cond else '[FAIL]', name,
                       ('  %s' % extra) if extra and not cond else ''))


def start(args, port, log):
    proc = subprocess.Popen([PY, '-u', os.path.join(BASE, 'tools', 'upload_server.py'),
                             '--host', '127.0.0.1', '--port', str(port)] + args,
                            stdout=open(log, 'ab'), stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.25)
        try:
            if httpx.DEFAULT.get('http://127.0.0.1:%d/api/status' % port,
                                 headers={'X-Auth-Token': 'T'}, timeout=3).ok:
                return proc
        except Exception:
            continue
    return proc


def stop(proc):
    try:
        proc.terminate()
        proc.wait()
    except Exception:
        pass


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    root = os.path.join(TMP, 'root')
    custom = os.path.join(TMP, 'custom', 'deep')
    os.makedirs(root)
    os.makedirs(custom)
    log = os.path.join(TMP, 'up.log')

    # ---------- A. 开 --allow-client-dir ----------
    port = 18085
    proc = start(['--upload-dir', root, '--token', 'T', '--allow-client-dir'], port, log)
    base = 'http://127.0.0.1:%d' % port
    H = {'X-Auth-Token': 'T'}
    try:
        st = httpx.DEFAULT.get(base + '/api/status', headers=H, timeout=10).json() or {}
        check('D. /api/status 暴露 allow_client_dir=true', st.get('allow_client_dir') is True,
              'got=%r' % (st.get('allow_client_dir'),))

        payload = b'hello-put-body'
        url = (base + '/api/chunk/put?filename=a.txt&size=%d&dir=%s'
               % (len(payload), compat.urlquote(custom, safe='')))
        r = httpx.DEFAULT.request('POST', url, headers=H, body=payload, timeout=30)
        data = r.json() or {}
        hit = os.path.join(custom, 'a.txt').replace('\\', '/')
        check('A1. chunk/put 落到请求指定目录',
              r.ok and data.get('status') == 'success'
              and compat.to_text(data.get('dir')).replace('\\', '/') == custom.replace('\\', '/')
              and os.path.isfile(os.path.join(custom, 'a.txt')),
              'status=%s body=%s' % (r.status, r.text[:200]))
        check('A1b. 响应 path 即真实落盘路径',
              compat.to_text(data.get('path')).replace('\\', '/') == hit,
              'path=%r' % (data.get('path'),))

        # 分片三连（init → upload → complete）带 dir
        blob = b'0123456789' * 10
        ini = httpx.DEFAULT.post(base + '/api/chunk/init',
                                 json_body={'filename': 'b.bin', 'size': len(blob),
                                            'total_chunks': 1, 'chunk_size': len(blob),
                                            'dir': custom}, headers=H, timeout=30)
        up_id = (ini.json() or {}).get('upload_id') or ''
        check('A2. 分片 init 接受 dir', ini.ok and bool(up_id),
              'status=%s body=%s' % (ini.status, ini.text[:160]))
        up = httpx.DEFAULT.request('POST', base + '/api/chunk/upload', headers=dict(
            H, **{'X-Upload-Id': up_id, 'X-Chunk-Index': '0'}), body=blob, timeout=30)
        comp = httpx.DEFAULT.post(base + '/api/chunk/complete',
                                  json_body={'upload_id': up_id, 'total_chunks': 1,
                                             'size': len(blob), 'filename': 'b.bin'},
                                  headers=H, timeout=30)
        cdata = comp.json() or {}
        check('A3. 分片 complete（未带 dir，用 init 记录）落到指定目录',
              up.ok and comp.ok and os.path.isfile(os.path.join(custom, 'b.bin'))
              and compat.to_text(cdata.get('dir')).replace('\\', '/') == custom.replace('\\', '/'),
              'up=%s comp=%s body=%s' % (up.status, comp.status, comp.text[:200]))

        # ---------- B. 相对路径 dir → 400 ----------
        r = httpx.DEFAULT.request('POST', base + '/api/chunk/put?filename=c.txt&size=3&dir=rel/dir',
                                  headers=H, body=b'abc', timeout=30)
        check('B. 相对路径 dir 被拒（400）', r.status == 400, 'status=%s' % r.status)
        check('B2. 相对路径 dir 未落盘', not os.path.exists(os.path.join(root, 'rel')),
              'root=%s' % os.listdir(root))
    finally:
        stop(proc)

    # ---------- C. 未开开关：dir 被忽略 ----------
    port2 = 18084
    root2 = os.path.join(TMP, 'root2')
    os.makedirs(root2)
    proc2 = start(['--upload-dir', root2, '--token', 'T'], port2, log)
    base2 = 'http://127.0.0.1:%d' % port2
    try:
        st2 = httpx.DEFAULT.get(base2 + '/api/status', headers=H, timeout=10).json() or {}
        check('D2. 未开开关时 allow_client_dir=false',
              st2.get('allow_client_dir') is False, 'got=%r' % (st2.get('allow_client_dir'),))
        r = httpx.DEFAULT.request('POST', base2 + '/api/chunk/put?filename=d.txt&size=3&dir=%s'
                                  % compat.urlquote(custom, safe=''),
                                  headers=H, body=b'abc', timeout=30)
        data = r.json() or {}
        check('C. 未开开关：dir 被忽略、按 upload-dir 落盘',
              r.ok and os.path.isfile(os.path.join(root2, 'd.txt')),
              'status=%s body=%s' % (r.status, r.text[:200]))
        check('C2. 响应仍带回真实落盘路径（调用方可据此提示）',
              compat.to_text(data.get('upload_dir')).replace('\\', '/') == root2.replace('\\', '/'),
              'upload_dir=%r' % (data.get('upload_dir'),))
    finally:
        stop(proc2)

    failed = [n for n, okk in results if not okk]
    print('\n结果：%d/%d 通过' % (len(results) - len(failed), len(results)))
    if failed:
        print('失败项：%s' % '; '.join(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
