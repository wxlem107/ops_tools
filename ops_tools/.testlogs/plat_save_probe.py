# -*- coding: utf-8 -*-
"""平台「服务器 编辑→保存」探针：用前端 saveServer()/setServerMode() 的真实 payload 打真接口。

关键怀疑点：saveServer 是全量 payload（含 tags/group/env/access_mode），而
setServerMode / 批量改属性 是**部分** payload —— 若后端 PUT 用部分字段覆盖整条记录，
多字段保存就会「看起来成功、实际丢字段」。这里把两种形态都打一遍并重启复核。
"""
from __future__ import print_function

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opslib import compat, httpx                                                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PORT = 18092
TOKEN = 'PLATSAVE'
URL = 'http://127.0.0.1:%d' % PORT
DATA = os.path.join('C:\\Windows\\Temp', 'plat_save_probe')
LOG = os.path.join('C:\\Windows\\Temp', 'plat_save_probe.log')
HDR = {'X-Auth-Token': TOKEN}
results = []


def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print('%s %s%s' % ('[PASS]' if cond else '[FAIL]', name,
                       ('  %s' % extra) if extra and not cond else ''))


def start_plat():
    proc = subprocess.Popen([PY, '-u', os.path.join(BASE, 'platform_console', 'ops_Platform.py'),
                             '--host', '127.0.0.1', '--port', str(PORT),
                             '--data-dir', DATA,
                             '--config', os.path.join(DATA, 'cfg.json'),
                             '--token', TOKEN, '--admin-password', 'Admin12345'],
                            stdout=open(LOG, 'ab'), stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        try:
            if httpx.DEFAULT.get(URL + '/api/ping', headers=HDR, timeout=3).ok:
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
    time.sleep(0.6)


def get_srv(name):
    r = httpx.DEFAULT.get(URL + '/api/servers/' + compat.urlquote(name), headers=HDR, timeout=10)
    return (r.json() or {}).get('server') or {}


def main():
    # 每次从干净数据目录开始（否则上一轮留下的 web-01 会让新增报 409）
    if os.path.isdir(DATA):
        import shutil
        shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA)
    proc = start_plat()
    try:
        # 前端 saveServer() 新增时的 payload（index.html 弹层全字段）
        add = {'name': 'web-01', 'host': '10.1.1.1', 'type': 'both',
               'upload_port': 8089, 'shell_port': 8088, 'upload_token': 'u1',
               'shell_token': 's1', 'group': 'g1', 'env': 'prod',
               'access_mode': 'auto', 'note': 'first', 'tags': ['java', 'nginx']}
        r = httpx.DEFAULT.post(URL + '/api/servers', json_body=add, headers=HDR, timeout=10)
        check('新增服务器', r.ok, 'status=%s body=%s' % (r.status, r.text[:160]))

        # ---- 编辑：前端 saveServer() 编辑时的全量 payload ----
        edit = dict(add, host='10.2.2.2', upload_port=9089, shell_port=9088,
                    upload_token='u2', shell_token='s2', group='g2', env='test',
                    note='second', tags=['k8s'], access_mode='proxy')
        r = httpx.DEFAULT.put(URL + '/api/servers/web-01', json_body=edit, headers=HDR, timeout=10)
        check('PUT 返回成功', r.ok, 'status=%s body=%s' % (r.status, r.text[:200]))
        s = get_srv('web-01')
        for fld, want in (('host', '10.2.2.2'), ('upload_port', 9089), ('shell_port', 9088),
                          ('upload_token', 'u2'), ('shell_token', 's2'), ('group', 'g2'),
                          ('env', 'test'), ('note', 'second'), ('access_mode', 'proxy')):
            check('编辑后 %s = %r' % (fld, want), s.get(fld) == want, 'got=%r' % (s.get(fld),))
        check('编辑后 tags = [k8s]', s.get('tags') == ['k8s'], 'got=%r' % (s.get('tags'),))
        check('编辑未丢 type', s.get('type') == 'both', 'got=%r' % (s.get('type'),))

        # ---- 部分更新（setServerMode 只发 name + access_mode）：不得清掉其它字段 ----
        r = httpx.DEFAULT.put(URL + '/api/servers/web-01',
                              json_body={'name': 'web-01', 'access_mode': 'direct'},
                              headers=HDR, timeout=10)
        s = get_srv('web-01')
        check('部分更新后 access_mode 生效', s.get('access_mode') == 'direct',
              'got=%r' % (s.get('access_mode'),))
        check('部分更新未清掉 host/端口/令牌/标签',
              s.get('host') == '10.2.2.2' and s.get('upload_port') == 9089
              and s.get('shell_port') == 9088 and s.get('upload_token') == 'u2'
              and s.get('tags') == ['k8s'],
              'got=%r' % ({k: s.get(k) for k in ('host', 'upload_port', 'shell_port',
                                                 'upload_token', 'tags')},))

        # ---- 重启复核（真落盘） ----
        stop(proc)
        proc = start_plat()
        s = get_srv('web-01')
        check('重启后编辑仍在（真落盘）',
              s.get('host') == '10.2.2.2' and s.get('access_mode') == 'direct'
              and s.get('tags') == ['k8s'], 'got=%r' % (s,))
    finally:
        stop(proc)

    failed = [n for n, okk in results if not okk]
    print('\n结果：%d/%d 通过' % (len(results) - len(failed), len(results)))
    if failed:
        print('失败项：%s' % '; '.join(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
