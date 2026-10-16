# -*- coding: utf-8 -*-
"""release_center「编辑服务器/应用后保存」落盘验证（真实起服务 + 重启复核）。

用户反馈：「点击编辑，修改后无法保存」。本探针把前端 saveServer() 实际发送的
body 原样打给 PUT 接口，然后：
  1) 立刻 GET 核对每个字段；
  2) **重启 release_center** 再 GET —— 只有真落盘才会保持，排除「内存里改了但没写文件」。
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
PORT = 18093
TOKEN = 'RCSAVE'
URL = 'http://127.0.0.1:%d' % PORT
DATA = os.path.join('C:\\Windows\\Temp', 'rc_save_probe')
LOG = os.path.join('C:\\Windows\\Temp', 'rc_save_probe.log')
HDR = {'X-Auth-Token': TOKEN}
results = []


def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print('%s %s%s' % ('[PASS]' if cond else '[FAIL]', name,
                       ('  %s' % extra) if extra and not cond else ''))


def start_rc():
    proc = subprocess.Popen([PY, '-u', os.path.join(BASE, 'platform_console', 'release_center.py'),
                             '--host', '127.0.0.1', '--port', str(PORT),
                             '--data-dir', DATA, '--token', TOKEN,
                             '--admin-password', 'Admin12345'],
                            stdout=open(LOG, 'ab'), stderr=subprocess.STDOUT)
    for _ in range(60):
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


def main():
    if not os.path.isdir(DATA):
        os.makedirs(DATA)
    proc = start_rc()
    try:
        # ---------- 新增服务器 ----------
        body = {'name': 'srv-x', 'host': '10.0.0.1', 'upload_port': 8089,
                'shell_port': 8088, 'upload_token': 'ut1', 'shell_token': 'st1',
                'upload_dir': '/home/xpe/app_temp'}
        r = httpx.DEFAULT.post(URL + '/api/servers', json_body=body, headers=HDR, timeout=10)
        check('新增服务器成功', r.ok and (r.json() or {}).get('status') == 'success',
              'status=%s body=%s' % (r.status, r.text[:200]))

        # ---------- 编辑（前端 saveServer 的 body 形态） ----------
        edit = {'name': 'srv-x', 'host': '10.0.0.2', 'upload_port': 9091,
                'shell_port': 9092, 'upload_token': 'ut2', 'shell_token': 'st2',
                'upload_dir': '/data/uploads'}
        r = httpx.DEFAULT.put(URL + '/api/servers/srv-x', json_body=edit, headers=HDR, timeout=10)
        check('PUT 编辑接口返回成功', r.ok, 'status=%s body=%s' % (r.status, r.text[:200]))

        got = ((httpx.DEFAULT.get(URL + '/api/servers', headers=HDR, timeout=10).json() or {})
               .get('servers') or [])
        rec = [x for x in got if x.get('name') == 'srv-x']
        rec = rec[0] if rec else {}
        for fld, want in (('host', '10.0.0.2'), ('upload_port', 9091), ('shell_port', 9092),
                          ('upload_token', 'ut2'), ('shell_token', 'st2'),
                          ('upload_dir', '/data/uploads')):
            check('编辑后 %s = %s' % (fld, want), rec.get(fld) == want, 'got=%r' % (rec.get(fld),))

        # ---------- 重启后再看（真落盘） ----------
        stop(proc)
        proc = start_rc()
        got2 = ((httpx.DEFAULT.get(URL + '/api/servers', headers=HDR, timeout=10).json() or {})
                .get('servers') or [])
        rec2 = [x for x in got2 if x.get('name') == 'srv-x']
        rec2 = rec2[0] if rec2 else {}
        check('重启后编辑结果仍在（真落盘）',
              rec2.get('host') == '10.0.0.2' and rec2.get('upload_dir') == '/data/uploads'
              and rec2.get('shell_port') == 9092, 'got=%r' % (rec2,))

        # ---------- 应用编辑（同一 no-op 写回模式，一并回归） ----------
        app_body = {'name': 'app-y', 'biz': 'b1', 'target_dir': '/opt/app',
                    'package': 'p.tgz', 'backup_dir': '/opt/bak', 'release_cmd': 'echo r',
                    'stop_cmd': 'echo s', 'start_cmd': 'echo t',
                    'health_type': 'tcp', 'health_target': '127.0.0.1:80',
                    'backup_keep': 3, 'health_retry': 2, 'steps': [], 'vars': {},
                    'param_defs': []}
        httpx.DEFAULT.post(URL + '/api/apps', json_body=app_body, headers=HDR, timeout=10)
        app_edit = dict(app_body, biz='b2', target_dir='/opt/app2', backup_keep=7)
        r = httpx.DEFAULT.put(URL + '/api/apps/app-y', json_body=app_edit, headers=HDR, timeout=10)
        got3 = ((httpx.DEFAULT.get(URL + '/api/apps', headers=HDR, timeout=10).json() or {})
                .get('apps') or [])
        rec3 = [x for x in got3 if x.get('name') == 'app-y']
        rec3 = rec3[0] if rec3 else {}
        check('应用编辑生效（biz/target_dir/backup_keep）',
              rec3.get('biz') == 'b2' and rec3.get('target_dir') == '/opt/app2'
              and rec3.get('backup_keep') == 7, 'got=%r' % (rec3,))
        stop(proc)
        proc = start_rc()
        got4 = ((httpx.DEFAULT.get(URL + '/api/apps', headers=HDR, timeout=10).json() or {})
                .get('apps') or [])
        rec4 = [x for x in got4 if x.get('name') == 'app-y']
        rec4 = rec4[0] if rec4 else {}
        check('应用编辑重启后仍在（真落盘）', rec4.get('biz') == 'b2'
              and rec4.get('target_dir') == '/opt/app2', 'got=%r' % (rec4,))
    finally:
        stop(proc)

    failed = [n for n, okk in results if not okk]
    print('\n结果：%d/%d 通过' % (len(results) - len(failed), len(results)))
    if failed:
        print('失败项：%s' % '; '.join(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
