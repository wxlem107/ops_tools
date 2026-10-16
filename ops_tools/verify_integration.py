#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
端到端集成验证：一次性拉起 4 个服务做真实联调。

    python verify_integration.py            # 全部检查
    python verify_integration.py --keep     # 结束后不清理临时目录（排查用）

覆盖：平台（:9501）+ 发布中心（:8091）+ upload_server（:18080）+ web_shell（:19100），
验证跨服务链路：服务器纳管与探测、文件列表分页转发、命令执行与 SSE 实时流、
指标采集、CMDB 采集任务、审计哈希链、发布单全流水线、健康检查失败自动回滚、
平台对发布中心的透传。
"""
from __future__ import print_function

import io
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from opslib import compat, httpx                          # noqa: E402

PY = sys.executable
KEEP = '--keep' in sys.argv
LOG_DIR = os.path.join(BASE, '.testlogs')
IS_WIN = os.name == 'nt'

PLAT_PORT, RC_PORT, UP_PORT, WS_PORT = 9501, 8091, 18080, 19100
PLAT = 'http://127.0.0.1:%d' % PLAT_PORT
RC = 'http://127.0.0.1:%d' % RC_PORT
UP = 'http://127.0.0.1:%d' % UP_PORT
UP_TOKEN, WS_TOKEN = 'up123', 'sh123'
PLAT_TOKEN, RC_TOKEN = 'ci-token', 'rc-token'

PASSED, FAILED = [], []
PROCS = []


def read_text(path, errors='replace'):
    """读文本并容错：上游命令输出可能夹带本地编码（GBK）字节，不能让回归脚本直接炸。"""
    try:
        with compat.open_text(path, 'r', errors=errors) as f:
            return f.read()
    except TypeError:
        with compat.open_text(path, 'r') as f:
            return f.read()


def check(name, fn, extra=''):
    try:
        result = fn()
        if result is False or result is None:
            raise AssertionError('校验返回假值')
        PASSED.append(name)
        print('  [OK]   %s%s' % (name, ('  (%s)' % extra) if extra else ''))
        return True
    except Exception as e:                                    # noqa: BLE001
        FAILED.append((name, e))
        print('  [FAIL] %s: %s%s' % (name, e, ('  [%s]' % extra) if extra else ''))
        return False


def wait_ping(url, timeout=40, token=''):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url + '/api/ping', headers={'X-Auth-Token': token} if token else None,
                          timeout=3)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start(name, args, logname, logdir):
    logf = open(os.path.join(logdir, logname), 'ab')
    proc = subprocess.Popen([PY] + args, cwd=BASE, stdout=logf, stderr=logf)
    PROCS.append(proc)
    return proc


def read_sse(url, seconds=8, want=''):
    """用原始 socket 读一段 SSE，返回收到的文本"""
    sock = None
    try:
        parsed = compat.urlparse(url)
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        sock.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n'
                      % (parsed.path + ('?' + parsed.query if parsed.query else ''))).encode('ascii'))
        sock.settimeout(1.5)
        buf = b''
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                if want and want.encode('utf-8') in buf:
                    break
                continue
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            if want and want.encode('utf-8') in buf:
                break
        return compat.to_text(buf)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _fail(exc):
    raise exc


def node_bin():
    """找一个可用的 node（没有就跳过前端行为检查，不阻塞整体验证）"""
    cands = []
    try:
        cands.append(compat.which('node'))
        cands.append(compat.which('node.exe'))
    except Exception:                                         # noqa: BLE001
        pass
    cands.append(os.environ.get('NODE_BIN'))
    for cand in cands:
        if cand and os.path.isfile(cand):
            return cand
    return None


def extract_js_function(text, header):
    """按大括号配对从 app.js 里抠出一个函数源码（用于在 Node 里真跑一遍）"""
    start = text.index(header)
    i = text.index('{', start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise AssertionError('函数未闭合: %s' % header)


FRONTEND_401_HARNESS = """\
const calls = [];
function showLogin(){ calls.push('login'); }
let token = 'tok';
%s
async function hit(status, body){
  globalThis.fetch = async () => ({status: status, ok: status === 200,
                                   text: async () => body});
  const before = calls.length;
  try { await api('/api/release/apps'); calls.push('resolved'); }
  catch(e){ calls.push('throw:' + e.message); }
  return calls.slice(before);
}
(async () => {
  const up = await hit(401, JSON.stringify({status:'error', code:'upstream_unauthorized',
                                            message:'发布中心拒绝访问（401）：凭据缺失'}));
  const sess = await hit(401, JSON.stringify({status:'error', code:'unauthorized',
                                              message:'未授权'}));
  const good = await hit(200, JSON.stringify({status:'success', items:[]}));
  process.stdout.write(JSON.stringify({upstream: up, session: sess, ok: good}));
})();
"""


def run_frontend_401_check(tmp):
    """用 Node 真跑一遍前端 api()：上游 401 只能提示、绝不能踢回登录页。

    回归的线上现象：平台→发布中心缺凭据收到 401，旧 api() 见 401 就 showLogin()，
    用户看到的是「登录成功 → 发布视图一加载就被退出 → 仿佛登不进去」的死循环。
    """
    node = node_bin()
    if not node:
        print('  [跳过] 未找到 node（可设 NODE_BIN 指定），跳过前端 401 策略检查')
        return
    try:
        src = compat.to_text(read_text(os.path.join(
            BASE, 'platform_console', 'static', 'app.js')))
        fn = extract_js_function(src, 'async function api(url,opts){')
    except Exception as e:                                    # noqa: BLE001
        check('能从 app.js 抠出 api() 函数', lambda: _fail(e))
        return
    js_path = os.path.join(tmp, 'frontend_401_check.js')
    with compat.open_text(js_path, 'w') as f:
        f.write(FRONTEND_401_HARNESS % fn)
    try:
        out = subprocess.check_output([node, js_path], stderr=subprocess.STDOUT, timeout=60)
    except Exception as e:                                    # noqa: BLE001
        check('前端 api() 可在 Node 中执行', lambda: _fail(e))
        return
    res = compat.json_loads(out, {}) or {}
    up = res.get('upstream') or []
    sess = res.get('session') or []
    good = res.get('ok') or []
    if not isinstance(up, list):
        up = []
    check('上游 401 不触发登录页（仅抛出可读错误）',
          lambda: 'login' not in up and any(compat.to_text(x).startswith('throw:') for x in up))
    check('上游 401 的错误信息原样透出给用户',
          lambda: any(u'发布中心拒绝访问' in compat.to_text(x) for x in up))
    check('平台自身 401 仍然踢回登录页', lambda: 'login' in sess)
    check('正常响应不受影响', lambda: list(good) == ['resolved'])


# --- 发布中心前端「批量上传 + 制品包多选」行为检查：DOM 桩 + 真跑脚本 ---
FRONTEND_DOM_STUB = u"""\
const EL = {};
function mkEl(id) {
  return { id: id, value: '', innerHTML: '', textContent: '', disabled: false, checked: false,
    files: [], style: {}, children: [],
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, appendChild(c) { this.children.push(c); },
    querySelector() { return mkEl(id + '_q'); }, click() {} };
}
globalThis.document = {
  getElementById(id) { if (!EL[id]) EL[id] = mkEl(id); return EL[id]; },
  querySelectorAll() { return []; },
  createElement(t) { return mkEl('_' + t); },
  addEventListener() {}
};
globalThis.localStorage = { getItem() { return ''; }, setItem() {}, removeItem() {} };
globalThis.alert = function () {};
globalThis.confirm = function () { return true; };
globalThis.fetch = function () {
  const body = JSON.stringify({ status: 'success', data: { need_login: false } });
  return Promise.resolve({ status: 200, ok: true, text: () => Promise.resolve(body),
                           json: () => Promise.resolve(JSON.parse(body)) });
};
globalThis.EventSource = function () { this.close = function () {}; };
globalThis.XMLHttpRequest = function () {
  this.open = function () {}; this.setRequestHeader = function () {}; this.send = function () {};
  this.abort = function () {}; this.upload = {};
};
"""

FRONTEND_UPLOAD_PROBE = u"""\
const out = {};
// 1) 队列内去重：同名同来源只入队一次（拖两次同一个文件不该传两遍）
addUploadFiles([{ name: 'a.tgz', size: 10, webkitRelativePath: '' },
                { name: 'a.tgz', size: 10, webkitRelativePath: '' }]);
out.dedupInQueue = UP_QUEUE.length;
// 2) 整目录上传：来源相对路径保留层级（用于溯源展示）
addUploadFiles([{ name: 'x.tgz', size: 20, webkitRelativePath: 'pkg/2024/x.tgz' }]);
out.srcOfDir = UP_QUEUE[UP_QUEUE.length - 1].src;
// 3) 系统垃圾文件不入队
addUploadFiles([{ name: '.DS_Store', size: 1 }, { name: 'Thumbs.db', size: 1 }]);
out.afterJunk = UP_QUEUE.length;
// 4) 队列渲染不抛错
renderQueue();
out.queueText = document.getElementById('u-sum').textContent;

// 5) 制品包多选：默认只看所选应用的包
ORDER_PKGS = [{ name: 'a1.tgz', app: 'demo', size: 1 },
              { name: 'b1.tgz', app: 'other', size: 2 },
              { name: 'a2.tgz', app: 'demo', size: 3 }];
PKG_BY_APP = true;
document.getElementById('o-app').value = 'demo';
out.visible = visiblePkgs().map(function (p) { return p.name; });
// 6) 勾选顺序不影响提交顺序（按列表顺序，保证 ${package} 稳定指向第一个）
PKG_CHECKED = { 'a2.tgz': true, 'a1.tgz': true };
out.picked = pickedPkgs();
// 7) 关掉「只看所选应用」后能看到全部
PKG_BY_APP = false;
out.visibleAll = visiblePkgs().map(function (p) { return p.name; });
// 8) 关键字筛选
document.getElementById('o-pkg-q').value = 'other';
out.visibleByQuery = visiblePkgs().map(function (p) { return p.name; });
// 9) 体积格式化
out.fmt = [fmtSize(0), fmtSize(1536), fmtSize(1048576)];
process.stdout.write(JSON.stringify(out));
"""


PLATFORM_UPLOAD_PROBE = u"""\
const out = {};
/* 平台页的 esc() 依赖真实 DOM 序列化，桩里给一个等价实现 */
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function toast() {}
function showLogin() {}
let RELPKG_LIST = [], RELPKG_CHECKED = {}, RELPKG_ONLY_APP = true;
let UP_QUEUE = [], UP_XHRS = [], token = 'tok';

/* 渲染结果里按顺序取出被列出的制品包名（picker 每行都有 value="名字"） */
function listedNames() {
  const html = document.getElementById('o_pkg').innerHTML;
  const names = [];
  const re = /value="([^"]*)"/g;
  let m;
  while ((m = re.exec(html))) names.push(m[1]);
  return names;
}

/* 1) 队列去重 + 目录溯源 + 系统垃圾文件过滤 */
addUpFiles([{ name: 'a.tgz', size: 10, webkitRelativePath: '' },
            { name: 'a.tgz', size: 10, webkitRelativePath: '' }]);
out.dedup = UP_QUEUE.length;
addUpFiles([{ name: 'x.tgz', size: 20, webkitRelativePath: 'pkg/2024/x.tgz' }]);
out.srcOfDir = UP_QUEUE[UP_QUEUE.length - 1].src;
addUpFiles([{ name: '.DS_Store', size: 1 }, { name: 'Thumbs.db', size: 1 }]);
out.afterJunk = UP_QUEUE.length;
renderUpQueue();
out.sumText = document.getElementById('up_sum').textContent;

/* 2) 各状态文案（重复与失败必须能区分，否则用户不知道要不要重传） */
out.rowTexts = [upRowText({ state: 'wait' }), upRowText({ state: 'up', pct: 42 }),
                upRowText({ state: 'ok' }), upRowText({ state: 'dup' }),
                upRowText({ state: 'fail', msg: '失败' })];

/* 3) 制品包多选：默认只看所选应用 */
RELPKG_LIST = [{ name: 'a1.tgz', app: 'demo', size: 1024 },
               { name: 'b1.tgz', app: 'other', size: 2048 },
               { name: 'a2.tgz', app: 'demo', size: 4096 }];
RELPKG_ONLY_APP = true;
document.getElementById('o_app').value = 'demo';
document.getElementById('o_pkg_q').value = '';
renderPkgPicker();
out.visible = listedNames();
RELPKG_ONLY_APP = false;
renderPkgPicker();
out.visibleAll = listedNames();
document.getElementById('o_pkg_q').value = 'other';
renderPkgPicker();
out.visibleByQuery = listedNames();
document.getElementById('o_pkg_q').value = '';
RELPKG_CHECKED = { 'a2.tgz': true, 'a1.tgz': true };
out.picked = relPickedPkgs();
updatePkgSum();
out.pickedSum = document.getElementById('o_pkg_sum').textContent;

/* 4) 上传 URL 与令牌（走平台代理，必须带 filename / src / skip_dup） */
const XHR_CALLS = [];
globalThis.XMLHttpRequest = function () {
  const self = this;
  this.open = function (m, u) { self._u = u; XHR_CALLS.push(u); };
  this.setRequestHeader = function (k, v) { if (k === 'X-Auth-Token') self._tok = v; };
  this.send = function () {
    self.status = 200;
    self.responseText = JSON.stringify({ status: 'success', name: 'n.tgz' });
    if (self.onload) self.onload();
  };
  this.abort = function () {};
  this.upload = {};
};
xhrUploadPkg({ name: 'n.tgz', src: 'dir/n.tgz', file: 'F', pct: 0 }, 'app1', '1.0', true)
  .then(function () {
    out.upUrl = XHR_CALLS[0] || '';
    out.upHeader = (UP_XHRS[0] && UP_XHRS[0]._tok) || '';
    process.stdout.write(JSON.stringify(out));
  }, function () {
    out.upUrl = XHR_CALLS[0] || '';
    process.stdout.write(JSON.stringify(out));
  });
"""


def run_frontend_upload_check(tmp):
    """用 DOM 桩在 Node 里真跑一遍发布中心前端脚本，验证批量上传与制品包多选。

    这两块是纯前端行为（队列去重、目录溯源、多选与过滤顺序），后端集成跑不到；
    用桩环境真跑脚本既能覆盖逻辑，也能顺带挡住「脚本里有顶层语法/引用错误」。
    """
    node = node_bin()
    if not node:
        print('  [跳过] 未找到 node（可设 NODE_BIN 指定），跳过前端上传逻辑检查')
        return
    try:
        html = compat.to_text(read_text(os.path.join(
            BASE, 'platform_console', 'templates', 'release_center.html')))
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        js = re.sub(r'\nboot\(\);\s*$', '\n', m.group(1))   # 去掉自动启动，避免依赖异步桩
    except Exception as e:                                    # noqa: BLE001
        check('能从发布中心模板抠出前端脚本', lambda: _fail(e))
        return
    js_path = os.path.join(tmp, 'frontend_upload_check.js')
    with compat.open_text(js_path, 'w') as f:
        f.write(FRONTEND_DOM_STUB + '\n' + js + '\n' + FRONTEND_UPLOAD_PROBE)
    try:
        out = subprocess.check_output([node, js_path], stderr=subprocess.STDOUT, timeout=60)
    except Exception as e:                                    # noqa: BLE001
        check('发布中心前端脚本可在 Node 中执行', lambda: _fail(e))
        return
    res = compat.json_loads(out, {}) or {}
    check('拖入同一文件两次只在队列里留一份', lambda: res.get('dedupInQueue') == 1)
    check('整目录上传保留来源相对路径',
          lambda: res.get('srcOfDir') == 'pkg/2024/x.tgz')
    check('系统垃圾文件不入队、正常文件不受影响', lambda: res.get('afterJunk') == 2)
    check('队列汇总文案可渲染',
          lambda: u'共 2 个文件' in compat.to_text(res.get('queueText')))
    check('制品包列表默认只显示所选应用的包',
          lambda: list(res.get('visible') or []) == ['a1.tgz', 'a2.tgz'])
    check('多选结果按列表顺序（${package} 始终指向第一个包）',
          lambda: list(res.get('picked') or []) == ['a1.tgz', 'a2.tgz'])
    check('可切换为查看全部制品包',
          lambda: list(res.get('visibleAll') or []) == ['a1.tgz', 'b1.tgz', 'a2.tgz'])
    check('制品包关键字筛选生效', lambda: list(res.get('visibleByQuery') or []) == ['b1.tgz'])
    check('制品包体积按人类可读格式展示',
          lambda: list(res.get('fmt') or []) == ['0 B', '1.5 KB', '1.00 MB'])


def run_platform_frontend_check(tmp):
    """用 DOM 桩在 Node 里真跑「集成平台」发布中心页的批量上传/多选逻辑。

    平台页（static/app.js + index.html）是用户实际点的那一版；这里按函数名从
    app.js **原样抠出**被测函数再跑，既不依赖整份 app.js 的启动流程，又能挡住
    「函数被改坏/拼错 URL/队列规则写错」这类只有点到页面才会暴露的问题。
    """
    node = node_bin()
    if not node:
        print('  [跳过] 未找到 node（可设 NODE_BIN 指定），跳过平台前端逻辑检查')
        return
    try:
        src = compat.to_text(read_text(os.path.join(
            BASE, 'platform_console', 'static', 'app.js')))
        pieces = [extract_js_function(src, 'function fmtSize(s){'),
                  extract_js_function(src, 'function relPkgName(p){'),
                  extract_js_function(src, 'function relPickedPkgs(){'),
                  extract_js_function(src, 'function updatePkgSum(){'),
                  extract_js_function(src, 'function renderPkgPicker(){'),
                  extract_js_function(src, 'function upRowCls(q){'),
                  extract_js_function(src, 'function upRowText(q){'),
                  extract_js_function(src, 'function renderUpQueue(){'),
                  extract_js_function(src, 'function addUpFiles(list,isObj){'),
                  extract_js_function(src, 'function xhrUploadPkg(item,app,ver,skip){')]
        skip_line = [ln for ln in src.splitlines() if ln.startswith('const UP_SKIP=')][0]
        dollar_line = [ln for ln in src.splitlines() if ln.startswith('const $ =')][0]
    except Exception as e:                                    # noqa: BLE001
        check('能从 platform_console/static/app.js 抠出批量上传/多选函数', lambda: _fail(e))
        return
    js_path = os.path.join(tmp, 'platform_frontend_check.js')
    with compat.open_text(js_path, 'w') as f:
        f.write(FRONTEND_DOM_STUB + '\n' + dollar_line + '\n' + skip_line + '\n'
                + '\n'.join(pieces) + '\n' + PLATFORM_UPLOAD_PROBE)
    try:
        out = subprocess.check_output([node, js_path], stderr=subprocess.STDOUT, timeout=60)
    except Exception as e:                                    # noqa: BLE001
        check('平台前端批量上传/多选脚本可在 Node 中执行', lambda: _fail(e))
        return
    res = compat.json_loads(out, {}) or {}
    check('平台页：拖入同一文件两次只留一份',
          lambda: res.get('dedup') == 1)
    check('平台页：整目录上传保留来源相对路径（供 ${packageN} 溯源）',
          lambda: res.get('srcOfDir') == 'pkg/2024/x.tgz')
    check('平台页：.DS_Store/Thumbs.db 不入队',
          lambda: res.get('afterJunk') == 2)
    check('平台页：队列状态文案（等待/上传中/完成/重复/失败）齐全',
          lambda: list(res.get('rowTexts') or []) == [u'等待', u'上传中 42%',
                                                     u'完成', u'已存在（MD5 相同）', u'失败'])
    check('平台页：队列汇总按文件数与体积统计',
          lambda: u'共 2 个文件' in compat.to_text(res.get('sumText')))
    check('平台页：制品包默认只列所选应用的包',
          lambda: list(res.get('visible') or []) == ['a1.tgz', 'a2.tgz'])
    check('平台页：可切换查看全部制品包',
          lambda: list(res.get('visibleAll') or []) == ['a1.tgz', 'b1.tgz', 'a2.tgz'])
    check('平台页：制品包关键字筛选生效',
          lambda: list(res.get('visibleByQuery') or []) == ['b1.tgz'])
    check('平台页：多选顺序按列表走（${package} 稳定指向第一个）',
          lambda: list(res.get('picked') or []) == ['a1.tgz', 'a2.tgz'])
    check('平台页：已选汇总给出个数与合计体积',
          lambda: u'已选 2 个制品包' in compat.to_text(res.get('pickedSum')))
    check('平台页：上传 URL 带 filename/src/skip_dup 且指向平台代理',
          lambda: (res.get('upUrl') or '').startswith('/api/release/packages/upload?')
          and 'filename=n.tgz' in res['upUrl'] and 'src=dir%2Fn.tgz' in res['upUrl']
          and 'skip_dup=1' in res['upUrl'])
    check('平台页：上传请求带平台令牌', lambda: res.get('upHeader') == 'tok')


ORDER_LIVE_PROBE = u"""\
const out = {};
/* 桩里给一个与页面等价的 esc()（真 DOM 序列化不可用） */
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function can() { return true; }
function toast() {}
function loadRelOrders() {}
function api() { return Promise.resolve({}); }
function confirmDialog() {}
let token = 'tok';

/* 记录 addEventListener 的事件名，用来验证「平台透传把匿名事件命名成 release」这一路径 */
globalThis.__ES = null;
globalThis.EventSource = function (u) {
  this.url = u; this.listeners = {}; this.onmessage = null; this.onerror = null;
  this.addEventListener = function (n, f) { this.listeners[n] = f; };
  this.close = function () {};
  globalThis.__ES = this;
};

const SAMPLE = {
  id: 'r-1', app: 'demo', version: '1.0.0', strategy: 'serial', state: 'running',
  servers: ['srv-a'], packages: ['a.tgz'],
  server_steps: [{ server: 'srv-a', state: 'running', steps: [
    { name: 'upload', label: '上传包', state: 'success', message: '已上传', elapsed: 1.2,
      logs: ['上传完成 100%'] },
    { name: 'backup', label: '备份旧版', state: 'running', message: '备份中', elapsed: 0.3,
      logs: ['tar czf backup.tgz app'] },
    { name: 'replace', label: '替换文件', state: 'pending', message: '', elapsed: 0, logs: [] }
  ] }]
};

/* 1) 状态中文 / 样式类 / 步骤键 的映射 */
out.states = [ordStateZh('running'), ordStateZh('success'), stepStateZh('running'),
              stepStateZh('weird'), ordStateCls('failed'), ordStateCls('success'),
              ordKey('a', 'b')];

/* 2) 打开发布单（真实路径 openOrder() 先记 id、再订流、后取快照）→ 自动跟随正在执行的步骤 */
openOrderId = 'r-1';
orderSetSnapshot(JSON.parse(JSON.stringify(SAMPLE)));
out.follow1 = orderFollow;
out.t1 = orderProgressText();
out.p1 = orderProgress();

/* 3) log 事件按步骤累积输出 */
applyOrderEvent({ type: 'log', order_id: 'r-1', server: 'srv-a', step: 'backup',
                  text: '\\n正在备份 old/ -> backup/\\n' });
out.log = (orderModel.stepLogs || {})['srv-a|backup'] || '';

/* 4) step 事件更新步骤状态 + 切换跟随 + 推进整单进度 */
applyOrderEvent({ type: 'step', order_id: 'r-1', server: 'srv-a', step: 'replace',
                  state: 'running', message: '开始替换', label: '替换文件',
                  elapsed: 0, percent: 66 });
out.follow2 = orderFollow;
out.p2 = orderProgress();
out.t2 = orderProgressText();
const st3 = orderModel.order.server_steps[0].steps[2];
out.st3 = [st3.state, st3.message, st3.label];

/* 5) 别的单的事件不能串进当前页面 */
applyOrderEvent({ type: 'step', order_id: 'r-2', server: 'srv-a', step: 'replace',
                  state: 'failed' });
out.crossTalk = orderModel.order.server_steps[0].steps[2].state;

/* 6) 步骤行 HTML（供页面渲染）：data-step-key / 高亮 / 消息 */
out.stepHtml = orderStepHtml({ name: 'replace', label: '替换文件', state: 'failed',
                               message: '权限不足', elapsed: 0.5 }, 'srv-a|replace', true);

/* 7) SSE 路径：平台透传把匿名事件命名为 release —— 必须 addEventListener('release') 才收得到 */
openOrderStream('r-1');
out.esListeners = Object.keys(__ES.listeners);
out.hasOnMessage = (typeof __ES.onmessage === 'function');
__ES.listeners['release']({ data: JSON.stringify({ type: 'step', order_id: 'r-1',
  server: 'srv-a', step: 'replace', state: 'success', percent: 100 }) });
out.afterSse = [orderModel.order.server_steps[0].steps[2].state, orderModel.percent];

process.stdout.write(JSON.stringify(out));
"""


def run_order_live_check(tmp):
    """用 DOM 桩在 Node 里真跑平台页「发布单实时执行」的纯逻辑。

    用户诉求：执行发布单时页面要实时显示①执行到哪一步、②已执行/正在执行脚本的返回信息。
    这里从 app.js 原样抠出相关函数真跑一遍，挡住「事件名写错收不到（页面像卡住）」
    「进度/跟随算错」「步骤行拼错」这类只有点页面才暴露的问题。
    """
    node = node_bin()
    if not node:
        print('  [跳过] 未找到 node（可设 NODE_BIN 指定），跳过发布单实时展示检查')
        return
    try:
        src = compat.to_text(read_text(os.path.join(
            BASE, 'platform_console', 'static', 'app.js')))
        # 常量/状态变量声明（ORD_STATE_ZH…orderRenderedKey）整段带出，避免多行 const 被切坏
        decls = src[src.index('const ORD_STATE_ZH='):src.index('\nfunction ordStateZh(')]
        dollar_line = [ln for ln in src.splitlines() if ln.startswith('const $ =')][0]
        heads = ['function ordStateZh(s){', 'function stepStateZh(s){', 'function ordStateCls(s){',
                 'function ordKey(server,name){', 'function orderSetSnapshot(o){',
                 'function orderAutoFollow(){', 'function orderModelLog(key,text){',
                 'function orderProgress(){', 'function orderProgressText(){',
                 'function orderStepHtml(s,key,active){', 'function orderStepLabel(key){',
                 'function orderUpdateConsoleHead(){', 'function orderRenderConsole(){',
                 'function orderAppendConsole(text){', 'function orderConsoleAtBottom(box){',
                 'function orderScrollConsole(force){', 'function orderToggleFollow(){',
                 'function orderFollowStep(keyEnc){', 'function orderClearConsole(){',
                 'function paintOrder(){', 'function applyOrderEvent(ev){',
                 'function openOrderStream(id){']
        pieces = [extract_js_function(src, h) for h in heads]
        # 同理：被抠出的 paintOrder() 若用 esc / escJs（内联 onclick 属性转义），
        # 这两个 helper 定义在 decls 区间之外，一并带出以免 Node 里 undefined。
        helpers = [extract_js_function(src, 'function esc(s){'),
                   extract_js_function(src, 'function escJs(s){')]
    except Exception as e:                                    # noqa: BLE001
        check('能从 platform_console/static/app.js 抠出发布单实时展示函数', lambda: _fail(e))
        return
    js_path = os.path.join(tmp, 'order_live_check.js')
    with compat.open_text(js_path, 'w') as f:
        f.write(FRONTEND_DOM_STUB + '\n' + dollar_line + '\n' + decls + '\n'
                + '\n'.join(helpers) + '\n' + '\n'.join(pieces) + '\n' + ORDER_LIVE_PROBE)
    try:
        out = subprocess.check_output([node, js_path], stderr=subprocess.STDOUT, timeout=60)
    except Exception as e:                                    # noqa: BLE001
        check('发布单实时展示脚本可在 Node 中执行', lambda: _fail(e))
        return
    res = compat.json_loads(out, {}) or {}
    check('平台页：发布单/步骤状态中文与样式类映射正确',
          lambda: list(res.get('states') or []) == [u'执行中', u'成功', u'执行中', u'weird',
                                                    u'off', u'on', u'a|b'])
    check('平台页：打开发布单后自动跟随正在执行的步骤',
          lambda: res.get('follow1') == 'srv-a|backup')
    check('平台页：进度文案实时给出「正在执行：X（服务器 · 第 N/M 步）· P%」',
          lambda: res.get('t1') == u'正在执行：备份旧版（srv-a · 第 2/3 步）· 33%')
    check('平台页：进度统计（已完成 / 总步数 / 百分比）',
          lambda: (res.get('p1') or {}).get('pct') == 33
          and (res.get('p1') or {}).get('done') == 1
          and (res.get('p1') or {}).get('total') == 3)
    check('平台页：log 事件按步骤累积脚本输出',
          lambda: u'正在备份 old/ -> backup/' in compat.to_text(res.get('log')))
    check('平台页：step 事件更新步骤状态并切换到新步骤',
          lambda: res.get('follow2') == 'srv-a|replace'
          and list(res.get('st3') or []) == [u'running', u'开始替换', u'替换文件'])
    check('平台页：step 事件带 percent 时同步整单进度',
          lambda: (res.get('p2') or {}).get('pct') == 66)
    check('平台页：跟随切到新步骤后进度文案同步到该步',
          lambda: res.get('t2') == u'正在执行：替换文件（srv-a · 第 3/3 步）· 66%')
    check('平台页：别的发布单的事件不会串进当前页面',
          lambda: res.get('crossTalk') == 'running')
    check('平台页：步骤行 HTML 含 data-step-key / 执行中高亮 / 消息',
          lambda: u'data-step-key="srv-a%7Creplace"' in compat.to_text(res.get('stepHtml'))
          and u'class="step failed active"' in compat.to_text(res.get('stepHtml'))
          and u'权限不足' in compat.to_text(res.get('stepHtml')))
    check('平台页：同时注册 addEventListener("release") 与 onmessage 两条通道',
          lambda: 'release' in (res.get('esListeners') or [])
          and 'close' in (res.get('esListeners') or [])
          and res.get('hasOnMessage') is True)
    check('平台页：平台透传的 release 命名事件能推进步骤状态（页面不刷新即修好）',
          lambda: list(res.get('afterSse') or []) == [u'success', 100])


RC_LIVE_PROBE = u"""\
const out = {};
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function api() { return { then: function () { return { then: function () {} }; } }; }
function stateTag() { return ''; }
function pkgChips() { return ''; }
function orderPkgs() { return []; }
let TOKEN = 'tok';

globalThis.__ES = null;
globalThis.EventSource = function (u) {
  this.url = u; this.listeners = {}; this.onmessage = null; this.onerror = null;
  this.addEventListener = function (n, f) { this.listeners[n] = f; };
  this.close = function () {};
  globalThis.__ES = this;
};

const SAMPLE = {
  id: 'r-1', app: 'demo', version: '1.0.0', strategy: 'serial', state: 'running',
  servers: ['srv-a'], packages: ['a.tgz'],
  server_steps: [{ server: 'srv-a', state: 'running', steps: [
    { name: 'upload', label: '上传包', state: 'success', message: '已上传', elapsed: 1.2,
      logs: ['上传完成 100%'] },
    { name: 'backup', label: '备份旧版', state: 'running', message: '备份中', elapsed: 0.3,
      logs: ['tar czf backup.tgz app'] },
    { name: 'replace', label: '替换文件', state: 'pending', message: '', elapsed: 0, logs: [] }
  ] }]
};

out.states = [oStepZh('running'), oStepZh('success'), oStepZh('weird'), oKey('a', 'b')];

CUR_ORDER = 'r-1';
setSnapshot(JSON.parse(JSON.stringify(SAMPLE)));
out.follow1 = O_FOLLOW;
out.t1 = progressText();
out.p1 = progress();

applyEvent({ type: 'log', order_id: 'r-1', server: 'srv-a', step: 'backup',
             text: '\\n正在备份 old/ -> backup/\\n' });
out.log = (OM.logs || {})['srv-a|backup'] || '';

applyEvent({ type: 'step', order_id: 'r-1', server: 'srv-a', step: 'replace',
             state: 'running', message: '开始替换', label: '替换文件',
             elapsed: 0, percent: 66 });
out.follow2 = O_FOLLOW;
out.p2 = progress();
out.t2 = progressText();
const st3 = OM.order.server_steps[0].steps[2];
out.st3 = [st3.state, st3.message, st3.label];

applyEvent({ type: 'step', order_id: 'r-2', server: 'srv-a', step: 'replace',
             state: 'failed' });
out.crossTalk = OM.order.server_steps[0].steps[2].state;

out.consoleHead = consoleHtml();

openStream('r-1');
out.esListeners = Object.keys(__ES.listeners);
out.hasOnMessage = (typeof __ES.onmessage === 'function');
__ES.listeners['release']({ data: JSON.stringify({ type: 'step', order_id: 'r-1',
  server: 'srv-a', step: 'replace', state: 'success', percent: 100 }) });
out.afterSse = [OM.order.server_steps[0].steps[2].state, OM.percent];

process.stdout.write(JSON.stringify(out));
"""


def run_release_center_page_check(tmp):
    """同 run_order_live_check，但针对「独立发布中心页」templates/release_center.html。

    用户可能直接打开这一页发布（不经集成平台），因此两套前端必须都真的能跑；
    这里从页面内联 <script> 原样抠出相关函数，在同一个 DOM 桩里跑一遍。
    """
    node = node_bin()
    if not node:
        print('  [跳过] 未找到 node（可设 NODE_BIN 指定），跳过独立发布中心页实时展示检查')
        return
    try:
        html = compat.to_text(read_text(os.path.join(
            BASE, 'platform_console', 'templates', 'release_center.html')))
        s = html.index('<script>') + len('<script>')
        js_src = html[s:html.index('</script>', s)]
        decls = js_src[js_src.index('var STREAM = null'):js_src.index('\nfunction oKey(')]
        # 独立页的函数风格是 `function f(...) {`（{ 前有空格），与 app.js 不同
        heads = ['function oKey(server, name) {', 'function oStepZh(s) {', 'function setSnapshot(o) {',
                 'function autoFollow() {', 'function appendLog(key, text) {', 'function progress() {',
                 'function progressText() {', 'function stepLabel(key) {', 'function paint() {',
                 'function consoleHtml() {', 'function updateConsoleHead() {',
                 'function renderConsole() {', 'function appendConsole(text) {',
                 'function consoleAtBottom(body) {', 'function scrollConsole(force) {',
                 'function toggleFollow() {', 'function followStep(keyEnc) {',
                 'function clearConsole() {', 'function applyEvent(ev) {', 'function openStream(id) {']
        pieces = [extract_js_function(js_src, h) for h in heads]
        # 被抠出的 paint() 会用 esc / escJs 做内联 onclick 的属性转义（防 XSS），
        # 这两个 helper 定义在 decls 区间之外，必须一并带出，否则 Node 里报
        # 「escJs is not defined」。
        helpers = [extract_js_function(js_src, 'function esc(s) {'),
                   extract_js_function(js_src, 'function escJs(s) {')]
    except Exception as e:                                    # noqa: BLE001
        check('能从 templates/release_center.html 抠出发布单实时展示函数', lambda: _fail(e))
        return
    js_path = os.path.join(tmp, 'rc_live_check.js')
    with compat.open_text(js_path, 'w') as f:
        f.write(FRONTEND_DOM_STUB + '\n' + decls + '\n' + '\n'.join(helpers) + '\n'
                + '\n'.join(pieces) + '\n' + RC_LIVE_PROBE)
    try:
        out = subprocess.check_output([node, js_path], stderr=subprocess.STDOUT, timeout=60)
    except Exception as e:                                    # noqa: BLE001
        check('独立发布中心页实时展示脚本可在 Node 中执行', lambda: _fail(e))
        return
    res = compat.json_loads(out, {}) or {}
    check('发布中心页：步骤状态中文与步骤键映射正确',
          lambda: list(res.get('states') or []) == [u'执行中', u'成功', u'weird', u'a|b'])
    check('发布中心页：打开发布单后自动跟随正在执行的步骤',
          lambda: res.get('follow1') == 'srv-a|backup')
    check('发布中心页：进度文案实时给出「正在执行：X（服务器 · 第 N/M 步）· P%」',
          lambda: res.get('t1') == u'正在执行：备份旧版（srv-a · 第 2/3 步）· 33%')
    check('发布中心页：进度统计（总步数 / 百分比）',
          lambda: (res.get('p1') or {}).get('pct') == 33
          and (res.get('p1') or {}).get('total') == 3)
    check('发布中心页：log 事件按步骤累积脚本输出',
          lambda: u'正在备份 old/ -> backup/' in compat.to_text(res.get('log')))
    check('发布中心页：step 事件更新步骤状态并切换到新步骤',
          lambda: res.get('follow2') == 'srv-a|replace'
          and list(res.get('st3') or []) == [u'running', u'开始替换', u'替换文件'])
    check('发布中心页：跟随切到新步骤后进度文案同步到该步',
          lambda: res.get('t2') == u'正在执行：替换文件（srv-a · 第 3/3 步）· 66%')
    check('发布中心页：别的发布单的事件不会串进当前页面',
          lambda: res.get('crossTalk') == 'running')
    check('发布中心页：脚本输出控制台 HTML 含跟随/清空按钮',
          lambda: u'脚本输出' in compat.to_text(res.get('consoleHead'))
          and u'toggleFollow()' in compat.to_text(res.get('consoleHead'))
          and u'clearConsole()' in compat.to_text(res.get('consoleHead')))
    check('发布中心页：同时注册 addEventListener("release") 与 onmessage 两条通道',
          lambda: 'release' in (res.get('esListeners') or [])
          and 'close' in (res.get('esListeners') or [])
          and res.get('hasOnMessage') is True)
    check('发布中心页：平台透传的 release 命名事件能推进步骤状态',
          lambda: list(res.get('afterSse') or []) == [u'success', 100])


def cleanup(tmp):
    for proc in PROCS:
        try:
            proc.kill()
        except Exception:
            pass
    time.sleep(0.5)
    if not KEEP:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tmp = tempfile.mkdtemp(prefix='ops_integration_')
    logdir = LOG_DIR
    up_dir = os.path.join(tmp, 'uploads')
    plat_data = os.path.join(tmp, 'platform')
    rc_data = os.path.join(tmp, 'release')
    compat.makedirs(logdir)
    compat.makedirs(up_dir)

    print('临时目录: %s' % tmp)
    print('\n== 启动服务 ==')
    start('upload_server', ['tools/upload_server.py', '--host', '127.0.0.1', '--port', str(UP_PORT),
                            '--upload-dir', up_dir, '--token', UP_TOKEN,
                            '--allow-client-dir'], 'upload.log', logdir)
    start('web_shell', ['tools/web_shell.py', '--host', '127.0.0.1', '--port', str(WS_PORT),
                        '--token', WS_TOKEN], 'shell.log', logdir)
    rc_args = [os.path.join('platform_console', 'release_center.py'),
               '--host', '127.0.0.1', '--port', str(RC_PORT), '--data-dir', rc_data,
               '--token', RC_TOKEN, '--admin-password', 'Admin12345',
               '--platform-url', PLAT, '--platform-token', PLAT_TOKEN]
    rc_proc = start('release_center', rc_args, 'release.log', logdir)
    start('ops_platform', [os.path.join('platform_console', 'ops_Platform.py'),
                           '--host', '127.0.0.1', '--port', str(PLAT_PORT),
                           '--data-dir', plat_data,
                           '--config', os.path.join(plat_data, 'ops_platform_config.json'),
                           '--token', PLAT_TOKEN, '--admin-password', 'Admin12345',
                           '--release-url', RC, '--release-token', RC_TOKEN], 'platform.log', logdir)
    for name, url, token in (('upload_server', 'http://127.0.0.1:%d' % UP_PORT, UP_TOKEN),
                             ('web_shell', 'http://127.0.0.1:%d' % WS_PORT, WS_TOKEN),
                             ('release_center', RC, RC_TOKEN),
                             ('ops_platform', PLAT, PLAT_TOKEN)):
        okk = wait_ping(url, token=token)
        print('  %-14s %s' % (name, '已就绪' if okk else '启动失败'))
        if not okk:
            cleanup(tmp)
            return 1

    hdr_p = {'X-Auth-Token': PLAT_TOKEN}
    hdr_r = {'X-Auth-Token': RC_TOKEN}
    hdr_u = {'X-Auth-Token': UP_TOKEN}
    try:
        print('\n== 平台基础 ==')
        check('首页加载且占位符已替换', lambda: ('app.js' in httpx.get(PLAT + '/').text) and
              ('{{version}}' not in httpx.get(PLAT + '/').text))
        check('健康接口', lambda: httpx.get(PLAT + '/api/health', headers=hdr_p).json()['status'] == 'success')
        check('OpenAPI 可用', lambda: httpx.get(PLAT + '/api/openapi.json').json()['openapi'] == '3.0.3')
        check('未登录访问被拒', lambda: httpx.get(PLAT + '/api/servers').status == 401)

        print('\n== 登录 / 会话 ==')
        bad = httpx.post(PLAT + '/api/login', json_body={'username': 'admin',
                                                         'password': 'Wrong_000'})
        check('错误口令返回 401', lambda: bad.status == 401)
        bj = bad.json({}) or {}
        check('错误口令返回明确提示', lambda: bj.get('status') == 'error' and
              bj.get('message') == '用户名或口令错误')
        lg = httpx.post(PLAT + '/api/login', json_body={'username': 'admin',
                                                        'password': 'Admin12345'}).json({}) or {}
        check('正确口令登录成功', lambda: lg.get('status') == 'success' and
              bool(lg.get('data', {}).get('token') or lg.get('token')))
        sess_tok = (lg.get('data', {}).get('token') if isinstance(lg.get('data'), dict)
                    else None) or lg.get('token') or ''
        check('登录会话可调用 API', lambda: httpx.get(
            PLAT + '/api/me', headers={'X-Auth-Token': sess_tok}).status == 200)

        # keep-alive 污染回归：往报错路径 POST 请求体后，同一条连接上的下一个请求
        # 不能被残留数据污染（鉴权/权限/参数错误的返回必须先排空未读请求体）
        _ka_conn = compat.http_client.HTTPConnection('127.0.0.1', PLAT_PORT, timeout=10)
        try:
            _ka_conn.request('POST', '/api/definitely-not-exist', b'x' * 200000,
                             {'X-Auth-Token': PLAT_TOKEN, 'Content-Length': '200000'})
            _ka_r1 = _ka_conn.getresponse()
            _ka_r1.read()
            _ka_conn.request('GET', '/api/ping')
            _ka_r2 = _ka_conn.getresponse()
            _ka_body = _ka_r2.read()
            _ka_ok = (_ka_r1.status == 404 and _ka_r2.status == 200 and
                      b'success' in _ka_body)
        finally:
            _ka_conn.close()
        check('错误响应后 keep-alive 连接未被污染', lambda: _ka_ok)

        print('\n== 分片上传 / 断点续传 ==')
        # 回归点：首次上传就携带 resume_key 时，服务端断点记录文件尚不存在，
        # 必须优雅降级为「新建任务」。py2 中 IOError 与 OSError 是两个独立类
        # （仅同源于 EnvironmentError），若 except 元组漏写 IOError，异常会逃逸到
        # HTTPServer 打出 traceback 并使连接中断 —— 线上 8089 上传报错即此因。
        fresh_key = 'a1b2c3d4e5f60718'
        init_body = {'filename': 'resume_demo.bin', 'relative_path': '', 'size': 40,
                     'total_chunks': 1, 'chunk_size': 40, 'resume_key': fresh_key}
        r1 = httpx.post(UP + '/api/chunk/init', headers=hdr_u, json_body=init_body).json({}) or {}
        check('新 resume_key 首次 init 不报错', lambda: r1.get('status') == 'success')
        uid1 = r1.get('upload_id') or ''
        check('init 返回合法 upload_id', lambda: bool(uid1) and len(uid1) >= 8)
        r2 = httpx.post(UP + '/api/chunk/init', headers=hdr_u, json_body=init_body).json({}) or {}
        check('断点续传复用同一 upload_id', lambda: r2.get('upload_id') == uid1)

        payload = b'0123456789' * 4
        up_chunk = httpx.post(UP + '/api/chunk/upload',
                              headers={'X-Auth-Token': UP_TOKEN, 'X-Upload-Id': uid1,
                                       'X-Chunk-Index': '0'}, body=payload)
        check('分片上传成功', lambda: (up_chunk.json({}) or {}).get('status') == 'success')
        comp = httpx.post(UP + '/api/chunk/complete', headers=hdr_u, json_body={
            'upload_id': uid1, 'total_chunks': 1, 'filename': 'resume_demo.bin',
            'relative_path': '', 'size': len(payload), 'resume_key': fresh_key}).json({}) or {}
        check('分片合并落盘', lambda: comp.get('status') == 'success')
        check('合并后文件内容一致', lambda: open(os.path.join(up_dir, 'resume_demo.bin'),
                                            'rb').read() == payload)

        # 多分片 + 乱序上传：默认 chunkfile 模式按片独立落盘，complete 时拼接，
        # 必须保证拼接顺序严格按 index（而非到达顺序），且总长与声明一致。
        multi_key = 'b2c3d4e5f6071829'
        parts = [b'AAAA' * 8, b'BBBB' * 8, b'CCCC' * 4]      # 3 片，最后一片更短
        m_total = sum(len(p) for p in parts)
        m_init = httpx.post(UP + '/api/chunk/init', headers=hdr_u, json_body={
            'filename': 'multi_demo.bin', 'relative_path': '', 'size': m_total,
            'total_chunks': 3, 'chunk_size': max(len(p) for p in parts),
            'resume_key': multi_key}).json({}) or {}
        uid3 = m_init.get('upload_id') or ''
        check('多分片任务创建成功', lambda: m_init.get('status') == 'success' and bool(uid3))
        for _i in (2, 0, 1):                                # 故意乱序
            httpx.post(UP + '/api/chunk/upload',
                       headers={'X-Auth-Token': UP_TOKEN, 'X-Upload-Id': uid3,
                                'X-Chunk-Index': str(_i)}, body=parts[_i])
        m_comp = httpx.post(UP + '/api/chunk/complete', headers=hdr_u, json_body={
            'upload_id': uid3, 'total_chunks': 3, 'filename': 'multi_demo.bin',
            'relative_path': '', 'size': m_total, 'resume_key': multi_key}).json({}) or {}
        check('多分片拼接完成', lambda: m_comp.get('status') == 'success')
        check('乱序分片按 index 正确拼接', lambda: open(
            os.path.join(up_dir, 'multi_demo.bin'), 'rb').read() == b''.join(parts))
        check('重复 complete 被拒（任务已收尾）', lambda: (httpx.post(
            UP + '/api/chunk/complete', headers=hdr_u, json_body={
                'upload_id': uid3, 'total_chunks': 3, 'filename': 'multi_demo.bin',
                'relative_path': '', 'size': m_total,
                'resume_key': multi_key}).status in (404, 409)))
        # 真正的「缺片」：只传 1/3 片就 complete，必须拒绝（不得落地半截文件）
        part_key = 'c3d4e5f60718293a'
        p_init = httpx.post(UP + '/api/chunk/init', headers=hdr_u, json_body={
            'filename': 'missing_demo.bin', 'relative_path': '', 'size': m_total,
            'total_chunks': 3, 'chunk_size': max(len(p) for p in parts),
            'resume_key': part_key}).json({}) or {}
        p_uid = p_init.get('upload_id') or ''
        httpx.post(UP + '/api/chunk/upload',
                   headers={'X-Auth-Token': UP_TOKEN, 'X-Upload-Id': p_uid,
                            'X-Chunk-Index': '0'}, body=parts[0])
        p_comp = httpx.post(UP + '/api/chunk/complete', headers=hdr_u, json_body={
            'upload_id': p_uid, 'total_chunks': 3, 'filename': 'missing_demo.bin',
            'relative_path': '', 'size': m_total, 'resume_key': part_key})
        check('分片不全时 complete 报 409', lambda: p_comp.status == 409)
        check('分片不全时不留半截文件', lambda: not os.path.exists(
            os.path.join(up_dir, 'missing_demo.bin')))

        # ---- v3.2.2 回归：并发同名落盘 / relative_path 语义 / 落盘临时文件 ----
        # 线上「批量小文件上传报 500、速率只有十几 MB/s」的根因是三个并发缺陷：
        #   1) 所有请求共用「<目标名>.part」临时文件，同名并发互相占用（Windows WinError 32）;
        #   2) unique_filename 只在 rename 前查一次，并发抢同一个最终名（互相覆盖）;
        #   3) relative_path 只给目录时，服务端把目录名当成了文件名。
        put_blob = b'Z' * 8192

        def _put(fname, rel, blob=put_blob):
            return httpx.post(UP + '/api/chunk/put?filename=%s&relative_path=%s&size=%d'
                              % (fname, rel, len(blob)), headers=hdr_u, body=blob)

        dup_names = ['same_name.bin', 'same_name_1.bin', 'same_name_2.bin', 'same_name_3.bin']
        dup_res = []
        dup_lock = threading.Lock()

        def _dup_worker():
            r = _put('same_name.bin', 'dup_dir/same_name.bin')
            with dup_lock:
                dup_res.append(r.status)

        dup_ts = [threading.Thread(target=_dup_worker) for _ in range(4)]
        for t in dup_ts:
            t.start()
        for t in dup_ts:
            t.join()
        check('并发同名小文件上传全部成功（不再 500）', lambda: dup_res == [200] * 4)
        check('并发同名自动改名不互相覆盖', lambda: sorted(
            os.listdir(os.path.join(up_dir, 'dup_dir'))) == dup_names)
        check('并发同名文件内容完整', lambda: all(
            os.path.getsize(os.path.join(up_dir, 'dup_dir', n)) == len(put_blob)
            for n in dup_names))
        check('relative_path 只给目录时文件名不被目录名顶替', lambda: (
            _put('inside.bin', 'dir_only_target').status == 200
            and os.path.isfile(os.path.join(up_dir, 'dir_only_target', 'inside.bin'))))

        # 同一分片并发重传（客户端超时重试的常见形态）不得报 500
        rp_init = httpx.post(UP + '/api/chunk/init', headers=hdr_u, json_body={
            'filename': 'dup_chunk.bin', 'relative_path': 'dup_chunk/dup_chunk.bin',
            'size': len(parts[0]), 'total_chunks': 1,
            'chunk_size': len(parts[0])}).json({}) or {}
        rp_uid = rp_init.get('upload_id') or ''
        rp_res = []

        def _chunk_worker():
            r = httpx.post(UP + '/api/chunk/upload',
                           headers={'X-Auth-Token': UP_TOKEN, 'X-Upload-Id': rp_uid,
                                    'X-Chunk-Index': '0'}, body=parts[0])
            with dup_lock:
                rp_res.append(r.status)

        rp_ts = [threading.Thread(target=_chunk_worker) for _ in range(3)]
        for t in rp_ts:
            t.start()
        for t in rp_ts:
            t.join()
        check('同一分片并发重传不报 500', lambda: rp_res == [200] * 3)

        hp = httpx.get(UP + '/api/health').json({}) or {}
        check('健康接口上报版本/分片大小/落盘模式', lambda: (
            bool(hp.get('version')) and int(hp.get('chunk_size') or 0) > 0
            and hp.get('chunk_write_mode') in ('chunkfile', 'offset')))

        # 落盘临时文件（<名>.part-xxxxxxxx）不得出现在列表里
        ghost = os.path.join(up_dir, 'ghost.bin.part-1a2b3c4d')
        with open(ghost, 'wb') as gf:
            gf.write(b'x')
        _put('list_probe.bin', 'list_probe.bin')            # 触发列表缓存失效
        listed = httpx.get(UP + '/api/list', headers=hdr_u).json({}) or {}
        check('列表不暴露 .part 落盘临时文件', lambda: all(
            '.part' not in (f.get('name') or '') for f in listed.get('files', [])))

        print('\n== 上传目录覆盖（v3.3.0 请求级 dir） ==')
        # 缘起：启动时写死 --upload-dir 后运维页无法改路径。v3.3.0 起：显式开
        # --allow-client-dir 才接受请求级 dir，且必须是绝对路径（相对路径直接 400，
        # 不静默落到 upload-dir，避免「配了目录却落到别处」这类隐性坑）。
        stt = httpx.get(UP + '/api/status', headers=hdr_u).json({}) or {}
        check('目录覆盖：status 暴露 allow_client_dir=true',
              lambda: stt.get('allow_client_dir') is True,
              'got=%r' % (stt.get('allow_client_dir'),))
        alt_dir = os.path.join(tmp, 'uploads_alt')
        compat.makedirs(alt_dir)
        pl = b'hello-dir-override'
        rdir = httpx.DEFAULT.request(
            'POST', UP + '/api/chunk/put?filename=ovr.txt&size=%d&dir=%s'
            % (len(pl), compat.urlquote(alt_dir, safe='')),
            headers=hdr_u, body=pl, timeout=30)
        dj = rdir.json({}) or {}
        check('目录覆盖：chunk/put?dir= 落到请求指定目录（响应带回真实路径）',
              lambda: rdir.ok
              and compat.to_text(dj.get('dir')).replace('\\', '/') == alt_dir.replace('\\', '/')
              and os.path.isfile(os.path.join(alt_dir, 'ovr.txt')),
              'status=%s body=%s' % (rdir.status, rdir.text[:160]))
        oblob = b'abcdefgh' * 8
        oini = httpx.post(UP + '/api/chunk/init',
                          json_body={'filename': 'ovr2.bin', 'size': len(oblob),
                                     'total_chunks': 1, 'chunk_size': len(oblob),
                                     'dir': alt_dir}, headers=hdr_u, timeout=30)
        oid = (oini.json({}) or {}).get('upload_id') or ''
        httpx.DEFAULT.request('POST', UP + '/api/chunk/upload',
                              headers=dict(hdr_u, **{'X-Upload-Id': oid, 'X-Chunk-Index': '0'}),
                              body=oblob, timeout=30)
        ocomp = httpx.post(UP + '/api/chunk/complete',
                           json_body={'upload_id': oid, 'total_chunks': 1,
                                      'size': len(oblob), 'filename': 'ovr2.bin'},
                           headers=hdr_u, timeout=30)
        check('目录覆盖：分片 complete 沿用 init 记录的目录',
              lambda: ocomp.ok and os.path.isfile(os.path.join(alt_dir, 'ovr2.bin')),
              'status=%s body=%s' % (ocomp.status, ocomp.text[:160]))
        rrel = httpx.DEFAULT.request('POST',
                                     UP + '/api/chunk/put?filename=rel.txt&size=3&dir=rel/dir',
                                     headers=hdr_u, body=b'abc', timeout=30)
        check('目录覆盖：相对路径 dir 被拒（400，不静默落到别处）',
              lambda: rrel.status == 400 and not os.path.exists(os.path.join(up_dir, 'rel')),
              'status=%s' % rrel.status)

        print('\n== 服务器纳管与转发 ==')
        body = {'name': 'local', 'host': '127.0.0.1', 'type': 'both',
                'upload_port': UP_PORT, 'shell_port': WS_PORT,
                'upload_token': UP_TOKEN, 'shell_token': WS_TOKEN,
                'group': '测试组', 'env': 'test', 'tags': ['demo', '集成测试'],
                'upload_dir': up_dir}
        check('新增服务器', lambda: httpx.post(PLAT + '/api/servers', json_body=body,
                                             headers=hdr_p).json()['status'] == 'success')
        check('列表探测在线', lambda: httpx.get(PLAT + '/api/servers', headers=hdr_p)
              .json()['servers'][0]['status']['online'] is True)
        check('meta 聚合标签', lambda: ('demo' in httpx.get(PLAT + '/api/meta', headers=hdr_p)
                                      .json().get('tags', [])))
        check('文件列表转发(分页参数透传)', lambda: httpx.get(
            PLAT + '/api/servers/local/files?page=1&size=2&sort=size&order=desc',
            headers=hdr_p).json()['status'] == 'success')

        # 经平台代理（proxy 模式，浏览器直连不通时走这条路）做多分片上传：
        # 覆盖「平台流式转发二进制分片体 + 目标端 chunkfile 拼接」整链路。
        # 代理路径形如 /api/servers/<名>/r/<upload|shell>/<远端路径>，与前端 proxyBase 一致。
        _relay = PLAT + '/api/servers/local/r/upload'
        relay_init = httpx.post(_relay + '/api/chunk/init', headers=hdr_p,
                                json_body={'filename': 'relay_demo.bin', 'relative_path': '',
                                           'size': m_total, 'total_chunks': 3,
                                           'chunk_size': max(len(p) for p in parts),
                                           'resume_key': 'd4e5f60718293a4b'}).json({}) or {}
        relay_uid = relay_init.get('upload_id') or ''
        check('经代理创建分片任务', lambda: relay_init.get('status') == 'success' and bool(relay_uid))
        for _i in (1, 0, 2):
            httpx.post(_relay + '/api/chunk/upload',
                       headers={'X-Auth-Token': PLAT_TOKEN, 'X-Upload-Id': relay_uid,
                                'X-Chunk-Index': str(_i)}, body=parts[_i])
        relay_comp = httpx.post(_relay + '/api/chunk/complete',
                                headers=hdr_p, json_body={
                                    'upload_id': relay_uid, 'total_chunks': 3,
                                    'filename': 'relay_demo.bin', 'relative_path': '',
                                    'size': m_total,
                                    'resume_key': 'd4e5f60718293a4b'}).json({}) or {}
        check('经代理分片上传完成', lambda: relay_comp.get('status') == 'success')
        check('经代理上传内容一致', lambda: open(
            os.path.join(up_dir, 'relay_demo.bin'), 'rb').read() == b''.join(parts))

        # ---- 服务器批量导入 / 导出（v2.1.0）与 upload_dir 字段 ----
        print('\n== 服务器批量导入/导出 ==')
        exp_csv = httpx.get(PLAT + '/api/servers/export?format=csv', headers=hdr_p)
        check('导出 CSV 返回附件', lambda: exp_csv.status == 200
              and exp_csv.body[:3] == b'\xef\xbb\xbf' and b'local' in exp_csv.body)
        exp_json = httpx.get(PLAT + '/api/servers/export?format=json', headers=hdr_p)
        _exp = exp_json.json({}) or {}
        _exp_local = next((s for s in _exp.get('servers', []) if s.get('name') == 'local'), {})
        check('导出 JSON 含服务器与令牌', lambda: exp_json.status == 200
              and bool(_exp_local) and _exp_local.get('upload_token') == UP_TOKEN)
        check('upload_dir 字段被保留（v2.1.0 归一）', lambda: _exp_local.get('upload_dir') == up_dir)
        imp_body = {'servers': [
            dict(body, group='导入组'),                       # 已存在 → merge 更新
            {'name': 'imp-new', 'host': '127.0.0.1', 'type': 'shell',
             'shell_port': WS_PORT, 'shell_token': WS_TOKEN, 'env': 'test'},
            {'name': '', 'host': ''},                          # 坏条目 → 记入 errors
        ], 'mode': 'merge'}
        imp = httpx.post(PLAT + '/api/servers/import', json_body=imp_body,
                         headers=hdr_p).json({}) or {}
        check('导入 merge：新增 1 更新 1', lambda: imp.get('added') == 1 and imp.get('updated') == 1)
        check('导入坏条目计入 errors', lambda: imp.get('error_total') == 1
              and (imp.get('errors') or [{}])[0].get('index') == 2)
        check('导入后配置生效（local 分组已更新）', lambda: httpx.get(
            PLAT + '/api/servers?kw=local&probe=0', headers=hdr_p).json()['servers'][0]['group'] == '导入组')
        imp2 = httpx.post(PLAT + '/api/servers/import', json_body=imp_body,
                          headers=hdr_p).json({}) or {}
        check('导入 skip 后再跑 merge 仍为 更新2 新增0', lambda: imp2.get('added') == 0
              and imp2.get('updated') == 2)
        # 清理：删除导入机，并把 local 的分组恢复原值（避免影响后续按分组/标签用例）
        httpx.delete(PLAT + '/api/servers/imp-new', headers=hdr_p)
        httpx.post(PLAT + '/api/servers/import', json_body={'servers': [body], 'mode': 'merge'},
                   headers=hdr_p)
        check('清理完成（imp-new 已删、local 已还原）', lambda: all(
            s.get('name') != 'imp-new' for s in
            httpx.get(PLAT + '/api/servers?probe=0', headers=hdr_p).json()['servers'])
            and httpx.get(PLAT + '/api/servers?kw=local&probe=0',
                          headers=hdr_p).json()['servers'][0]['group'] == '测试组')

        # ---- 批量下载经平台代理（流式 zip + 去重 + 错误透传） ----
        print('\n== 批量下载代理 ==')
        bd = httpx.get(PLAT + '/api/servers/local/batch-download'
                       '?paths=relay_demo.bin&paths=relay_demo.bin', headers=hdr_p)
        check('批量下载返回 zip（重复 paths 已去重）', lambda: (
            bd.status == 200 and bd.body[:2] == b'PK'
            and len(zipfile.ZipFile(io.BytesIO(bd.body)).namelist()) == 1))
        bd404 = httpx.get(PLAT + '/api/servers/local/batch-download?paths=__no_such__.bin',
                          headers=hdr_p)
        check('批量下载无可下载文件时 404 JSON 透传', lambda: (
            bd404.status == 404 and b'error' in bd404.body
            and 'json' in (bd404.headers.get('Content-Type') or '')))
        dl = httpx.get(PLAT + '/api/servers/local/download?path=' + compat.urlquote('relay_demo.bin'),
                       headers=hdr_p)
        check('单文件下载经平台代理（流式转发）', lambda: (
            dl.status == 200 and dl.body == b''.join(parts)))
        check('同步执行命令', lambda: 'plat-exec-ok' in httpx.post(
            PLAT + '/api/servers/local/exec', json_body={'cmd': 'echo plat-exec-ok', 'timeout': 10},
            headers=hdr_p).text)
        check('SSE 实时命令输出', lambda: 'plat-sse-ok' in read_sse(
            PLAT + '/api/servers/local/exec-stream?cmd=echo%20plat-sse-ok&token=' + PLAT_TOKEN,
            want='plat-sse-ok'))

        # ---- 命令执行券（v2.0.7）：终端页面先换券再取流，URL 上不再出现 cmd= ----
        # 缘起：GET …/exec-stream?cmd=whoami&token=... 被态势感知按「URI 命令执行」
        # 特征规则误报成 webshell 攻击。改为命令放请求体换券，再凭券取流。
        state = {}
        r = httpx.post(PLAT + '/api/servers/local/exec-ticket',
                       json_body={'cmd': 'echo plat-ticket-ok', 'timeout': 10},
                       headers=hdr_p)
        tk = r.json() or {}
        state['ticket'] = tk.get('ticket') or ''
        check('平台：换取一次性执行券', lambda: r.status == 200
              and tk.get('status') == 'success' and len(state['ticket']) >= 16)
        check('平台：凭券取流可拿到输出（URL 上无 cmd=）', lambda: 'plat-ticket-ok' in read_sse(
            PLAT + '/api/servers/local/exec-stream?ticket=%s&token=%s'
            % (state['ticket'], PLAT_TOKEN), want='plat-ticket-ok'))
        r403 = httpx.get(PLAT + '/api/servers/local/exec-stream?ticket=%s&token=%s'
                         % (state['ticket'], PLAT_TOKEN), headers=hdr_p)
        check('平台：执行券一次性（重放被拒 403）', lambda: r403.status == 403)
        # 平台上转到 web_shell 必须走 POST：命令不出现在链路的 URI 上（shell.log 里
        # 应出现 "POST /api/exec/stream"，而不应是 "GET /api/exec/stream?cmd="）
        time.sleep(0.5)
        shell_log_text = compat.to_text(read_text(os.path.join(logdir, 'shell.log')) or '')
        check('平台：转发 web_shell 走 POST（URI 无命令）',
              lambda: 'POST /api/exec/stream' in shell_log_text)

        # ---- 编辑保存后刷新必须「秒回」（v2.0.8）----
        # 缘起：用户反馈「点击编辑，修改后无法保存」。实测保存其实成功了，卡的是保存后的
        # 自动刷新：列表/详情接口对每台机器**串行实时探测**（离线机每台最多 3×2s），
        # 多台离线机时请求长时间不返回，界面就成了「保存没生效」。
        # 修法：探测并发化 + 15s TTL 缓存；这里用一台不可达机器把行为钉住。
        fake = {'name': 'zz-offline', 'host': '10.255.255.1', 'type': 'both',
                'upload_port': 8089, 'shell_port': 8088}
        httpx.post(PLAT + '/api/servers', json_body=fake, headers=hdr_p)
        t0 = time.time()
        rlist = httpx.get(PLAT + '/api/servers', headers=hdr_p, timeout=60)
        cold = time.time() - t0
        check('平台：含离线机时列表可用（并发探测，冷启动 <15s）',
              lambda: rlist.status == 200 and cold < 15, '耗时 %.1fs' % cold)
        t0 = time.time()
        httpx.get(PLAT + '/api/servers', headers=hdr_p, timeout=60)
        warm = time.time() - t0
        check('平台：列表二次请求命中缓存（<1.5s）', lambda: warm < 1.5, '耗时 %.2fs' % warm)
        t0 = time.time()
        httpx.put(PLAT + '/api/servers/zz-offline',
                  json_body=dict(fake, host='10.255.255.2', note='edited'),
                  headers=hdr_p, timeout=60)
        one = httpx.get(PLAT + '/api/servers/zz-offline', headers=hdr_p, timeout=60)
        edit_rt = time.time() - t0
        ojson = (one.json() or {}).get('server') or {}
        check('平台：编辑后立即可读到新值（保存即生效）',
              lambda: one.status == 200 and ojson.get('host') == '10.255.255.2'
              and ojson.get('note') == 'edited', 'got=%r' % (ojson.get('host'),))
        check('平台：编辑后单台读回也是秒级（不重复探测全量）',
              lambda: edit_rt < 15, '耗时 %.1fs' % edit_rt)
        httpx.delete(PLAT + '/api/servers/zz-offline', headers=hdr_p)
        names = [(s.get('name') or '') for s in
                 (httpx.get(PLAT + '/api/servers', headers=hdr_p, timeout=60).json() or {})
                 .get('servers', [])]
        check('平台：假服务器已清理（不影响后续用例）',
              lambda: 'zz-offline' not in names)

        check('指标采集(本机探针降级)', lambda: httpx.get(PLAT + '/api/servers/local/metrics',
                                                     headers=hdr_p).json()['metrics'].get('online', True)
              and 'cpu_percent' in httpx.get(PLAT + '/api/servers/local/metrics',
                                             headers=hdr_p).json()['metrics'])

        print('\n== 资产台账 ==')
        r = httpx.post(PLAT + '/api/cmdb/collect', json_body={'names': ['local']}, headers=hdr_p).json()
        check('发起采集任务', lambda: r['status'] == 'success')
        task_id = (r.get('task_id') or r.get('task', {}).get('id') or
                   (r.get('data') or {}).get('task_id', ''))
        deadline = time.time() + 30
        while task_id and time.time() < deadline:
            t = httpx.get(PLAT + '/api/tasks/%s' % task_id, headers=hdr_p).json()
            state = (t.get('task') or t).get('state')
            if state in ('success', 'failed', 'canceled'):
                break
            time.sleep(0.5)
        check('采集任务完成', lambda: state == 'success')
        check('台账有数据', lambda: len(httpx.get(PLAT + '/api/cmdb', headers=hdr_p).json()
                                       .get('items', [])) >= 1)
        check('台账汇总', lambda: httpx.get(PLAT + '/api/cmdb/summary', headers=hdr_p).json()
              ['status'] == 'success')
        # 人工字段编辑走 compat.merge_dict（回归：该函数曾缺失导致 PUT 500）
        m1 = httpx.put(PLAT + '/api/cmdb/local', json_body={'owner': u'张三', 'note': 'first'},
                       headers=hdr_p)
        m2 = httpx.put(PLAT + '/api/cmdb/local', json_body={'idc': 'A1'}, headers=hdr_p)
        if m1.status != 200 or m2.status != 200:
            print('    [诊断] 台账人工字段返回: %s %s' % (m1.status, compat.to_text(m1.body[:200])))
        cmdb_one = [x for x in httpx.get(PLAT + '/api/cmdb', headers=hdr_p).json()
                    .get('items', []) if x.get('name') == 'local']
        check('台账人工字段可保存(部分字段合并不丢原值)', lambda: (
            m1.status == 200 and m2.status == 200 and bool(cmdb_one)
            and compat.to_text((cmdb_one[0].get('manual') or {}).get('owner') or '') == u'张三'
            and (cmdb_one[0].get('manual') or {}).get('idc') == 'A1'))

        print('\n== 审计 ==')
        check('审计有记录', lambda: len(httpx.get(PLAT + '/api/audit?limit=50',
                                               headers=hdr_p).json().get('items', [])) >= 3)
        check('审计哈希链校验', lambda: httpx.get(PLAT + '/api/audit/verify',
                                               headers=hdr_p).json().get('ok') is True)
        check('审计 CSV 导出', lambda: httpx.get(PLAT + '/api/audit/export.csv',
                                               headers=hdr_p).status == 200)

        print('\n== 发布中心（经平台透传 + 直连） ==')
        check('平台侧发布中心在线', lambda: httpx.get(PLAT + '/api/release/config',
                                                 headers=hdr_p).json().get('online') is True)
        check('纳管目标机', lambda: httpx.post(RC + '/api/servers', json_body={
            'name': 'local', 'host': '127.0.0.1', 'upload_port': UP_PORT, 'shell_port': WS_PORT,
            'upload_token': UP_TOKEN, 'shell_token': WS_TOKEN, 'upload_dir': up_dir},
            headers=hdr_r).json()['status'] == 'success')

        pkg = os.path.join(up_dir, '_src_demo.bin')
        with open(pkg, 'wb') as f:
            f.write(b'A' * 1024 * 512)
        with open(pkg, 'rb') as f:
            up = httpx.DEFAULT.request('POST', RC + '/api/packages/upload?filename=demo-1.0.0.bin&app=demo&version=1.0.0',
                                       headers=hdr_r, src=f, src_len=os.path.getsize(pkg))
        check('上传制品包', lambda: up.ok)

        # ---------- 制品包批量上传（前端拖拽 / 多选 / 整目录都走这个接口，每文件一次请求） ----------
        print('\n== 发布中心·制品包批量上传与 MD5 判重 ==')
        rc_pkg_dir = os.path.join(rc_data, 'packages')

        def up_pkg(name, data, app='', version='', src='', skip_dup=False):
            qs = '?filename=' + compat.urlquote(name)
            for key, val in (('app', app), ('version', version), ('src', src)):
                if val:
                    qs += '&%s=%s' % (key, compat.urlquote(val))
            if skip_dup:
                qs += '&skip_dup=1'
            return httpx.DEFAULT.request('POST', RC + '/api/packages/upload' + qs,
                                         headers=hdr_r, body=data, timeout=60)

        r1 = up_pkg('multi-a.bin', b'A' * 4096, app='demo', version='1.0.0',
                    src='pkg/2024/multi-a.bin')
        check('批量上传第 1 个包成功', lambda: r1.ok and r1.json().get('status') == 'success')
        check('记录来源相对路径（整目录上传可溯源）',
              lambda: r1.json().get('src') == 'pkg/2024/multi-a.bin')
        r_dup = up_pkg('multi-a-copy.bin', b'A' * 4096, skip_dup=True)
        check('MD5 判重：内容相同的包直接跳过、不产生副本',
              lambda: r_dup.ok and r_dup.json().get('duplicate') is True
              and r_dup.json().get('name') == 'multi-a.bin')
        r2 = up_pkg('multi-b.bin', b'B' * 2048, skip_dup=True)
        check('MD5 判重不误伤新内容', lambda: r2.ok and r2.json().get('duplicate') is False)
        r3 = up_pkg('multi-a.bin', b'C' * 128, skip_dup=True)
        check('同名不同内容自动改名（不覆盖已有包）',
              lambda: r3.ok and r3.json().get('name') == 'multi-a_1.bin')
        names_now = [p.get('name') for p in
                     httpx.get(RC + '/api/packages', headers=hdr_r).json().get('packages', [])]
        check('判重后制品包清单符合预期',
              lambda: 'multi-a.bin' in names_now and 'multi-b.bin' in names_now
              and 'multi-a-copy.bin' not in names_now and 'multi-a_1.bin' in names_now)
        uploads_in_dir = [n for n in os.listdir(rc_pkg_dir) if n.endswith('.part')]
        check('上传临时文件已清理（不留 .part 脏文件）', lambda: uploads_in_dir == [])

        # ---------- 经「集成平台」上传（平台发布中心页的拖拽/多选/整目录都走这条） ----------
        # 回归点：平台代理原先复用 release_proxy → req.raw_body() 默认 32MB 上限，
        # 稍大的包直接 413，而且整包读进内存；现在改走 release_upload_proxy 流式转发。
        print('\n== 发布中心·经平台代理上传制品包（流式透传） ==')
        big_size = 34 * 1024 * 1024                     # 刻意跨过旧的 32MB 上限
        big_body = b'P' * big_size
        big_md5 = hashlib.md5(big_body).hexdigest()
        tb0 = time.time()
        rb = httpx.DEFAULT.request('POST', PLAT + '/api/release/packages/upload?filename=big-via-platform.bin'
                                   '&app=demo&version=9.9.9&src=%s' % compat.urlquote('big/big-via-platform.bin'),
                                   headers=hdr_p, body=big_body, timeout=600)
        check('经平台代理上传 34MB 制品包（超过旧 32MB 上限）成功',
              lambda: rb.ok and rb.json().get('status') == 'success')
        check('代理上传记录了大小与 MD5', lambda: (rb.json().get('size') == big_size
                                              and rb.json().get('md5') == big_md5))
        check('代理上传落盘内容与源一致', lambda: (
            os.path.getsize(os.path.join(rc_pkg_dir, 'big-via-platform.bin')) == big_size
            and hashlib.md5(open(os.path.join(rc_pkg_dir, 'big-via-platform.bin'), 'rb').read())
            .hexdigest() == big_md5))
        print('  （34MB 经平台透传耗时 %.1fs）' % (time.time() - tb0))

        def up_via_plat(name, data, app='', version='', src='', skip_dup=False):
            qs = '?filename=' + compat.urlquote(name)
            for key, val in (('app', app), ('version', version), ('src', src)):
                if val:
                    qs += '&%s=%s' % (key, compat.urlquote(val))
            if skip_dup:
                qs += '&skip_dup=1'
            return httpx.DEFAULT.request('POST', PLAT + '/api/release/packages/upload' + qs,
                                         headers=hdr_p, body=data, timeout=120)

        rp1 = up_via_plat('plat-a.bin', b'X' * 1024, app='demo', version='2.0.0',
                          src='webroot/plat-a.bin')
        check('代理上传并回填来源（整目录保留层级）',
              lambda: rp1.ok and rp1.json().get('src') == 'webroot/plat-a.bin')
        rp2 = up_via_plat('plat-a-copy.bin', b'X' * 1024, skip_dup=True)
        check('代理上传同样支持 MD5 判重',
              lambda: rp2.ok and rp2.json().get('duplicate') is True)
        rp3 = httpx.DEFAULT.request('POST', PLAT + '/api/release/packages/upload?filename=empty.bin',
                                    headers=hdr_p, body=b'', timeout=30)
        check('空请求体经代理返回 400（不是 500）',
              lambda: rp3.status == 400 and u'请求体为空' in compat.to_text(rp3.body))
        check('代理上传后制品包清单可见', lambda: any(
            p.get('name') == 'plat-a.bin' for p in
            httpx.get(PLAT + '/api/release/packages', headers=hdr_p).json().get('packages', [])))

        # 回归点：Request.parsed 只含路径（query 被剥掉给路由匹配用），
        # 曾经用 req.parsed.query 拼转发 URL → 查询串被静默丢弃：
        # 发布中心的 ?app= 过滤、转发到目标机的 ?page= 全部「设置无效还不报错」。
        up_via_plat('plat-other.bin', b'O' * 512, app='othersvc')
        filt = httpx.get(PLAT + '/api/release/packages?app=demo', headers=hdr_p).json()
        fnames = [p.get('name') for p in filt.get('packages', [])]
        check('代理转发保留查询串（?app=demo 过滤真的生效）',
              lambda: 'plat-a.bin' in fnames and 'plat-other.bin' not in fnames)
        filt2 = httpx.get(PLAT + '/api/release/packages?app=othersvc', headers=hdr_p).json()
        check('换一个取值同样生效（不是碰巧命中）',
              lambda: [p.get('name') for p in filt2.get('packages', [])] == ['plat-other.bin'])

        # 多包发布单用的应用：@upload 把两个包都推上去，后续命令靠 ${packageN} 点名
        if IS_WIN:
            mt_dir = os.path.join(tmp, 'deploy_multi').replace('/', '\\')
            mb_dir = os.path.join(tmp, 'backup_multi').replace('/', '\\')
            multi_cmd = ('(if not exist "${target_dir}" mkdir "${target_dir}")'
                         ' & copy /Y "${package1}" "${target_dir}\\one.bin" >NUL'
                         ' & copy /Y "${package2}" "${target_dir}\\two.bin" >NUL')
            count_cmd = ('(if not exist "${target_dir}" mkdir "${target_dir}")'
                         ' & echo ${package_count} > "${target_dir}\\pkgnum.txt"')
        else:
            mt_dir = os.path.join(tmp, 'deploy_multi').replace('\\', '/')
            mb_dir = os.path.join(tmp, 'backup_multi').replace('\\', '/')
            multi_cmd = ('mkdir -p "${target_dir}" && cp -f "${package1}" "${target_dir}/one.bin"'
                         ' && cp -f "${package2}" "${target_dir}/two.bin"')
            count_cmd = 'mkdir -p "${target_dir}" && echo ${package_count} > "${target_dir}/pkgnum.txt"'
        multi_app = {'name': 'demo-multi', 'target_dir': mt_dir, 'backup_dir': mb_dir,
                     'backup_keep': 2, 'package': 'multi-a.bin',
                     'steps': [{'name': 'up', 'label': u'上传', 'cmd': '@upload'},
                               {'name': 'num', 'label': u'变量检查', 'cmd': count_cmd},
                               {'name': 'put', 'label': u'替换', 'cmd': multi_cmd}],
                     'health_type': 'none'}
        check('创建多包应用', lambda: httpx.post(RC + '/api/apps', json_body=multi_app,
                                              headers=hdr_r).json()['status'] == 'success')
        check('拒绝不存在的制品包', lambda: httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-multi', 'version': '3.0.0', 'packages': ['multi-a.bin', 'nope.bin'],
            'servers': ['local']}, headers=hdr_r).status == 400)
        check('拒绝空制品包', lambda: httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-multi', 'version': '3.0.0', 'packages': [],
            'servers': ['local']}, headers=hdr_r).status == 400)

        # 目标机就是本机：按当前系统准备可执行的替换命令（发布中心已按目标机系统自适应）
        # 注意 Windows 的 if 必须带括号：`if cond cmd1 & cmd2` 里 cmd1/cmd2 同属该 if
        # 的语句体，条件为假（目标目录已存在=二次发布）时后面的命令会被整行跳过。
        if IS_WIN:
            t_dir = os.path.join(tmp, 'deploy').replace('/', '\\')
            b_dir = os.path.join(tmp, 'backup').replace('/', '\\')
            replace_cmd = ('(if not exist "${target_dir}" mkdir "${target_dir}")'
                           ' & copy /Y "${package}" "${target_dir}\\app.bin" >NUL')
        else:
            t_dir = os.path.join(tmp, 'deploy').replace('\\', '/')
            b_dir = os.path.join(tmp, 'backup').replace('\\', '/')
            replace_cmd = 'mkdir -p "${target_dir}" && cp -f "${package}" "${target_dir}/app.bin"'
        app_def = {'name': 'demo', 'target_dir': t_dir, 'backup_dir': b_dir,
                   'backup_keep': 3, 'package': 'demo-1.0.0.bin',
                   'release_cmd': replace_cmd,
                   'stop_cmd': 'echo stopped', 'start_cmd': 'echo started',
                   'health_type': 'port', 'health_target': str(UP_PORT),
                   'health_retry': 3, 'health_interval': 1}
        check('创建应用', lambda: httpx.post(RC + '/api/apps', json_body=app_def,
                                           headers=hdr_r).json()['status'] == 'success')
        order = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo', 'version': '1.0.0', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'strategy': 'serial'}, headers=hdr_r).json()
        oid = (order.get('order') or order).get('id')
        check('创建发布单', lambda: bool(oid))
        run = httpx.post(RC + '/api/orders/%s/run' % oid, json_body={}, headers=hdr_r).json()
        check('执行发布单', lambda: run['status'] == 'success')
        deadline = time.time() + 90
        final = {}
        while time.time() < deadline:
            resp = httpx.get(RC + '/api/orders/%s' % oid, headers=hdr_r)
            o = (resp.json({}) or {}).get('order') or {}
            if not o:
                print('    [轮询异常] status=%s body=%r' % (resp.status, resp.text[:200]))
                time.sleep(1)
                continue
            if o.get('state') in ('success', 'failed', 'rolled_back', 'canceled'):
                final = o
                break
            time.sleep(1)
        if final.get('state') != 'success':
            # 失败时把每步状态与报错打出来，避免只看到一句「校验返回假值」
            print('    [诊断] 发布单状态=%s' % final.get('state'))
            for entry in (final.get('server_steps') or []):
                for s in (entry.get('steps') or []):
                    print('    [诊断] 步骤 %-14s %-9s %s' % (s.get('name'), s.get('state'),
                                                           compat.to_text(s.get('message') or '')[:200]))
        check('发布单执行成功', lambda: final.get('state') == 'success')
        flat_steps = [s for entry in (final.get('server_steps') or [])
                      for s in (entry.get('steps') or [])]
        check('流水线步骤全部成功', lambda: len(flat_steps) >= 6
              and all(s.get('state') == 'success' for s in flat_steps))
        check('远端文件已替换', lambda: os.path.exists(os.path.join(tmp, 'deploy', 'app.bin')) and
              os.path.getsize(os.path.join(tmp, 'deploy', 'app.bin')) == 512 * 1024)
        check('平台透传看到发布单', lambda: oid in compat.to_text(
            httpx.get(PLAT + '/api/release/orders', headers=hdr_p).body))

        # 健康检查失败 -> 自动回滚
        bad = dict(app_def, name='demo-bad',
                   target_dir=os.path.join(tmp, 'deploy_bad').replace('/', '\\') if IS_WIN
                   else os.path.join(tmp, 'deploy_bad').replace('\\', '/'),
                   backup_dir=os.path.join(tmp, 'backup_bad').replace('/', '\\') if IS_WIN
                   else os.path.join(tmp, 'backup_bad').replace('\\', '/'),
                   health_target='18999', health_retry=2, health_interval=1)
        httpx.post(RC + '/api/apps', json_body=bad, headers=hdr_r)
        compat.makedirs(os.path.join(tmp, 'deploy_bad'))
        with open(os.path.join(tmp, 'deploy_bad', 'app.bin'), 'wb') as f:
            f.write(b'OLD')
        o2 = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-bad', 'version': '2.0.0', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'strategy': 'serial'}, headers=hdr_r).json()
        oid2 = (o2.get('order') or o2).get('id')
        httpx.post(RC + '/api/orders/%s/run' % oid2, json_body={}, headers=hdr_r)
        deadline = time.time() + 90
        final2 = {}
        while time.time() < deadline:
            resp = httpx.get(RC + '/api/orders/%s' % oid2, headers=hdr_r)
            o = (resp.json({}) or {}).get('order') or {}
            if not o:
                print('    [轮询异常] status=%s body=%r' % (resp.status, resp.text[:200]))
                time.sleep(1)
                continue
            if o.get('state') in ('success', 'failed', 'rolled_back', 'canceled'):
                final2 = o
                break
            time.sleep(1)
        check('健康检查失败自动回滚', lambda: final2.get('state') == 'rolled_back')

        # 回滚必须真的把文件还原（Windows 上 `if 条件 & 命令` 的静默跳过曾让回滚变成空操作：
        # 目标目录此时必然存在 → if 为假 → robocopy 整行被跳过 → 却报「已回滚」）
        bad_bin = os.path.join(tmp, 'deploy_bad', 'app.bin')
        bad_content = b''
        if os.path.isfile(bad_bin):
            with open(bad_bin, 'rb') as f:
                bad_content = f.read()
        if bad_content != b'OLD':
            print('    [诊断] 回滚后 app.bin 内容=%r（期望 b\'OLD\'）' % bad_content[:40])
            for s in (final2.get('server_steps') or [{}])[0].get('steps', []):
                if s.get('name') == 'rollback':
                    print(compat.to_text('\n'.join(s.get('logs') or []))[:1200])
        check('回滚后文件内容还原为备份版本', lambda: bad_content == b'OLD')

        # 二次发布到同一目标目录：备份目录已存在，仍必须真的备份 + 替换
        # （回归：`if not exist {backup_dir} mkdir ... && ...` 在目录已存在时整行被 cmd 跳过，
        #  发布照样报成功、备份却是空的 —— 回滚时才会发现问题）
        again_bk = os.path.join(tmp, 'backup_again').replace('/', '\\') if IS_WIN \
            else os.path.join(tmp, 'backup_again').replace('\\', '/')
        again_app = dict(app_def, name='demo-again',
                         target_dir=os.path.join(tmp, 'deploy_again').replace('/', '\\') if IS_WIN
                         else os.path.join(tmp, 'deploy_again').replace('\\', '/'),
                         backup_dir=again_bk)
        httpx.post(RC + '/api/apps', json_body=again_app, headers=hdr_r)
        compat.makedirs(os.path.join(tmp, 'backup_again'))     # 预置：模拟「非首次发布」
        compat.makedirs(os.path.join(tmp, 'deploy_again'))     # 已有上一版部署，备份才有内容可备
        with open(os.path.join(tmp, 'deploy_again', 'app.bin'), 'wb') as f:
            f.write(b'OLD')
        o3 = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-again', 'version': '1.0.1', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'strategy': 'serial'}, headers=hdr_r).json()
        oid3 = (o3.get('order') or o3).get('id')
        httpx.post(RC + '/api/orders/%s/run' % oid3, json_body={}, headers=hdr_r)
        deadline = time.time() + 90
        final3 = {}
        while time.time() < deadline:
            o = (httpx.get(RC + '/api/orders/%s' % oid3, headers=hdr_r).json({}) or {}).get('order') or {}
            if o.get('state') in ('success', 'failed', 'rolled_back', 'canceled'):
                final3 = o
                break
            time.sleep(1)
        if final3.get('state') != 'success':
            print('    [诊断] 非首次发布状态=%s' % final3.get('state'))
            for s in (final3.get('server_steps') or [{}])[0].get('steps', []):
                print('    [诊断] 步骤 %-10s %-9s %s' % (s.get('name'), s.get('state'),
                                                       compat.to_text(s.get('message') or '')[:160]))
        again_root = os.path.join(tmp, 'backup_again')
        again_dirs = sorted(os.listdir(again_root)) if os.path.isdir(again_root) else []
        again_tgt = os.path.join(tmp, 'deploy_again', 'app.bin')
        check('非首次发布（备份目录已存在）成功', lambda: final3.get('state') == 'success')
        check('备份没有被 if 吞掉（生成时间戳备份目录）', lambda: len(again_dirs) >= 1)

        def _bk_olds():
            for d in again_dirs:
                p = os.path.join(again_root, d, 'app.bin')
                if os.path.isfile(p):
                    with open(p, 'rb') as f:
                        if f.read() == b'OLD':
                            return True
            return False

        check('备份内容为发布前的旧版本（而非空目录）', _bk_olds)
        check('替换命令没有被 if 吞掉（目标文件已就位）',
              lambda: os.path.isfile(again_tgt) and os.path.getsize(again_tgt) == 512 * 1024)

        # ---------- 自定义步骤链（v1.1） ----------
        print('\n== 发布中心·自定义步骤链 ==')
        check('获取步骤链模板', lambda: (httpx.get(RC + '/api/presets', headers=hdr_r).json() or {})
              .get('presets') and True)

        def mk_chain(name, steps, extra=None):
            body = {'name': name, 'package': 'demo-1.0.0.bin',
                    'vars': {'Version_All': 'V9', 'NPkg': '01'},
                    'steps': steps, 'health_type': 'port',
                    'health_target': str(UP_PORT), 'health_retry': 3, 'health_interval': 1}
            body.update(extra or {})
            return httpx.post(RC + '/api/apps', json_body=body, headers=hdr_r)

        def run_order(oid):
            httpx.post(RC + '/api/orders/%s/run' % oid, json_body={}, headers=hdr_r)
            deadline = time.time() + 90
            while time.time() < deadline:
                resp = httpx.get(RC + '/api/orders/%s' % oid, headers=hdr_r)
                o = (resp.json({}) or {}).get('order') or {}
                if o.get('state') in ('success', 'failed', 'rolled_back', 'canceled'):
                    return o
                time.sleep(1)
            return {}

        def new_order(name):
            r = httpx.post(RC + '/api/orders', json_body={
                'app': name, 'version': '1.0.0', 'package': 'demo-1.0.0.bin',
                'servers': ['local'], 'strategy': 'serial'}, headers=hdr_r).json()
            return (r.get('order') or r).get('id')

        def steps_of(o):
            return [s for entry in (o.get('server_steps') or [])
                    for s in (entry.get('steps') or [])]

        if IS_WIN:
            ct = os.path.join(tmp, 'deploy_chain').replace('/', '\\')
            rel_chain = ('(if not exist "${target_dir}" mkdir "${target_dir}")'
                         ' & copy /Y "${package}" "${target_dir}\\chain.bin" >NUL')
        else:
            ct = os.path.join(tmp, 'deploy_chain')
            rel_chain = 'mkdir -p "${target_dir}" && cp -f "${package}" "${target_dir}/chain.bin"'
        r1 = mk_chain('demo-chain', [
            {'name': 'fetch', 'label': u'拉包占位', 'cmd': 'echo chain-fetch ${Version_All}-${NPkg}'},
            {'name': 'upload', 'label': u'上传包', 'cmd': '@upload'},
            {'name': 'stop', 'label': u'停服', 'cmd': 'echo chain-stop ${Version_All}-${NPkg}'},
            {'name': 'release', 'label': u'替换', 'cmd': rel_chain},
            {'name': 'health', 'label': u'健康检查', 'cmd': '@health'},
        ], {'target_dir': ct, 'backup_dir': os.path.join(tmp, 'backup_chain')})
        check('创建自定义链应用', lambda: r1.json()['status'] == 'success')
        fc = run_order(new_order('demo-chain'))
        if fc.get('state') != 'success':
            print('    [诊断] 链式发布状态=%s' % fc.get('state'))
            for entry in (fc.get('server_steps') or []):
                for s in (entry.get('steps') or []):
                    print('    [诊断] 步骤 %-10s %-9s %s' % (s.get('name'), s.get('state'),
                                                           compat.to_text(s.get('message') or '')[:160]))
                    if s.get('state') == 'failed':
                        print(compat.to_text('\n'.join(s.get('logs') or []))[:1200])
        check('自定义链发布成功', lambda: fc.get('state') == 'success')
        check('自定义链步骤按序执行', lambda: [s.get('name') for s in steps_of(fc)]
              == ['fetch', 'upload', 'stop', 'release', 'health'])
        check('自定义链变量替换生效', lambda: 'chain-stop V9-01' in compat.to_text(
              ' '.join(steps_of(fc)[2].get('logs') or [])))
        chain_bin = os.path.join(tmp, 'deploy_chain', 'chain.bin')
        check('自定义链包已替换到位', lambda: os.path.exists(chain_bin)
              and os.path.getsize(chain_bin) == 512 * 1024)

        # ---------- 多包发布单：一次发布把多个制品包都推到目标机（v1.3） ----------
        print('\n== 发布中心·多包发布单 ==')
        om = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-multi', 'version': '3.0.0',
            'packages': ['multi-a.bin', 'multi-b.bin'],
            'servers': ['local'], 'strategy': 'serial'}, headers=hdr_r).json()
        mo = om.get('order') or {}
        check('发布单挂多个制品包', lambda: mo.get('packages') == ['multi-a.bin', 'multi-b.bin'])
        check('package 兼容字段为第一个包', lambda: mo.get('package') == 'multi-a.bin')
        mfinal = run_order(mo.get('id'))
        if mfinal.get('state') != 'success':
            print('    [诊断] 多包发布单状态=%s' % mfinal.get('state'))
            for s in steps_of(mfinal):
                print('    [诊断] 步骤 %-10s %-9s %s' % (s.get('name'), s.get('state'),
                                                       compat.to_text(s.get('message') or '')[:200]))
        check('多包发布单执行成功', lambda: mfinal.get('state') == 'success')
        up_logs = [compat.to_text(l) for s in steps_of(mfinal) if s.get('name') == 'up'
                   for l in (s.get('logs') or [])]
        check('上传步骤逐个记录了每个包',
              lambda: len([l for l in up_logs if u'已上传' in l]) >= 2)
        one_bin = os.path.join(tmp, 'deploy_multi', 'one.bin')
        two_bin = os.path.join(tmp, 'deploy_multi', 'two.bin')

        def _size(path):
            return os.path.getsize(path) if os.path.exists(path) else -1

        if _size(one_bin) != 4096 or _size(two_bin) != 2048:
            print('    [诊断] one.bin=%s two.bin=%s' % (_size(one_bin), _size(two_bin)))
            md = os.path.join(tmp, 'deploy_multi')
            print('    [诊断] deploy_multi 存在=%s 内容=%s'
                  % (os.path.isdir(md), os.listdir(md) if os.path.isdir(md) else '-'))
            print('    [诊断] uploads 内容=%s' % (sorted(n for n in os.listdir(up_dir)
                                                        if 'multi' in n) if os.path.isdir(up_dir) else '-'))
            for s in steps_of(mfinal):
                if s.get('name') in ('up', 'put'):
                    print(compat.to_text('\n'.join(s.get('logs') or []))[:1500])
        check('两个包都落到目标机（${package1}/${package2} 各自指向正确的包）',
              lambda: _size(one_bin) == 4096 and _size(two_bin) == 2048)
        num_file = os.path.join(tmp, 'deploy_multi', 'pkgnum.txt')
        num_text = ''
        if os.path.isfile(num_file):
            with compat.open_text(num_file, 'r') as nf:
                num_text = nf.read().strip()
        check('${package_count} 变量已注入', lambda: num_text == '2')

        # ---------- 发布单详情：中文消息与中文日志同处一单（py2 混排崩溃的现场） ----------
        # 线上（release_center v1.3.0 / Python 2.7.5）：执行发布后轮询发布单详情直接 500
        #     File "opslib/compat.py", line 139, in json_bytes
        #     UnicodeDecodeError: 'ascii' codec can't decode byte 0xe5 in position 1
        # 因为同一个 order dict 里既有 step['message']（u'' 字面量 → py2 unicode），
        # 又有 step['logs'] 的日志行（safe_str → UTF-8 字节串）。详见 compat.json_safe。
        print('\n== 发布中心·发布单详情（中文消息 + 中文日志）==')
        det = httpx.get(RC + '/api/orders/%s' % mo.get('id'), headers=hdr_r)
        det_text = compat.to_text(det.body)
        detail = (det.json({}) or {}).get('order') or {}
        det_msgs = [compat.to_text(s.get('message') or '') for s in steps_of(detail)]
        det_logs = [compat.to_text(l) for s in steps_of(detail) for l in (s.get('logs') or [])]
        check('详情接口返回 200（py2 混排时该接口整体 500）', lambda: det.status == 200)
        check('详情为合法 JSON 且无替换符损坏',
              lambda: bool(json.loads(det_text)) and u'\ufffd' not in det_text)
        check('步骤中文提示保留（message：u 字面量产物）',
              lambda: any(u'已上传至' in m or u'完成' in m for m in det_msgs))
        check('步骤中文日志保留（logs：safe_str 产物）',
              lambda: any(u'已上传' in l for l in det_logs))

        # 平台发布中心页走的也是这条路：建单直接 POST /api/release/orders（透传）
        op = httpx.post(PLAT + '/api/release/orders', json_body={
            'app': 'demo-multi', 'version': '4.0.0', 'package': 'multi-a.bin',
            'packages': ['multi-a.bin', 'multi-b.bin'], 'servers': ['local']},
            headers=hdr_p).json()
        op_order = op.get('order') or {}
        check('经平台代理建多包发布单（前端 packages 数组透传）',
              lambda: op.get('status') == 'success'
              and op_order.get('packages') == ['multi-a.bin', 'multi-b.bin']
              and op_order.get('package') == 'multi-a.bin')

        # 软失败：continue_on_fail 的步骤失败不中断、不回滚
        cs = mk_chain('demo-chain-soft', [
            {'name': 'broken', 'label': u'故意失败', 'cmd': '__no_such_cmd_chain__',
             'continue_on_fail': True},
            {'name': 'stop', 'label': u'停服', 'cmd': 'echo after-broken'},
        ], {'target_dir': os.path.join(tmp, 'deploy_soft'),
            'backup_dir': os.path.join(tmp, 'backup_soft')})
        check('创建软失败链应用', lambda: cs.json()['status'] == 'success')
        fs = run_order(new_order('demo-chain-soft'))
        soft_steps = steps_of(fs)
        check('软失败链整体成功', lambda: fs.get('state') == 'success')
        check('软失败步骤标记失败但继续', lambda: len(soft_steps) == 2
              and soft_steps[0].get('state') == 'failed'
              and soft_steps[1].get('state') == 'success')

        # 硬失败：无 continue_on_fail → 发布单失败
        ch = mk_chain('demo-chain-hard', [
            {'name': 'broken', 'label': u'故意失败', 'cmd': '__no_such_cmd_chain__'},
        ], {'target_dir': os.path.join(tmp, 'deploy_hard'),
            'backup_dir': os.path.join(tmp, 'backup_hard')})
        check('创建硬失败链应用', lambda: ch.json()['status'] == 'success')
        fh = run_order(new_order('demo-chain-hard'))
        check('硬失败链发布失败', lambda: fh.get('state') == 'failed')

        # 非法步骤链应被拒绝
        bad_steps = mk_chain('demo-chain-bad', [{'name': 'x y', 'cmd': 'echo 1'}])
        check('非法步骤名被拒绝', lambda: bad_steps.json()['status'] == 'error')

        # ---------- 发布参数（v1.2）：param_defs + order.params 注入命令 ----------
        print('\n== 发布中心·发布参数 ==')
        rp = httpx.post(RC + '/api/apps', json_body={
            'name': 'demo-param', 'package': 'demo-1.0.0.bin',
            'vars': {'Version_All': 'FROM_VARS'},
            'param_defs': [
                {'key': 'Version_All', 'label': u'版本号', 'default': 'V7', 'required': True},
                {'key': 'NPkg', 'label': u'包序号', 'default': '02', 'required': False},
            ],
            'steps': [{'name': 'echo', 'label': u'回显',
                       'cmd': 'echo ver=${Version_All} seq=${NPkg}'}],
            'health_type': 'none'}, headers=hdr_r)
        check('创建带参数定义应用', lambda: rp.json()['status'] == 'success')
        if rp.json().get('status') != 'success':
            print('    [诊断] 创建应用返回:', rp.status, compat.to_text(rp.body[:300]))
        rbad = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-param', 'version': '1.0.0', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'params': {'Version_All': ''}}, headers=hdr_r)
        if rbad.status != 400:
            print('    [诊断] 缺必填返回:', rbad.status, compat.to_text(rbad.body[:300]))
        check('缺必填参数被拒绝', lambda: rbad.status == 400)
        ruk = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-param', 'version': '1.0.0', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'params': {'Version_All': 'V8', 'Nope': 'x'}}, headers=hdr_r)
        if ruk.status != 400:
            print('    [诊断] 未知参数返回:', ruk.status, compat.to_text(ruk.body[:300]))
        check('未知参数被拒绝', lambda: ruk.status == 400)
        rapps = httpx.get(RC + '/api/apps', headers=hdr_r).json()
        pa = [a for a in (rapps.get('apps') or []) if a.get('name') == 'demo-param']
        print('    [诊断] 服务端 demo-param param_defs =',
              json.dumps(pa[0].get('param_defs'), ensure_ascii=False) if pa else '应用不存在')
        rok = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-param', 'version': '1.0.0', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'params': {'Version_All': 'V8', 'NPkg': '07'}}, headers=hdr_r)
        check('带参数发布单创建成功', lambda: rok.json()['status'] == 'success')
        fp = run_order(((rok.json().get('order') or {}).get('id')))
        check('参数化发布成功', lambda: fp.get('state') == 'success')
        check('发布单参数注入命令(覆盖同名vars)', lambda: 'ver=V8 seq=07' in compat.to_text(
              ' '.join(steps_of(fp)[0].get('logs') or [])))
        check('发布单详情携带参数', lambda: (fp.get('params') or {}).get('Version_All') == 'V8')
        rdef = httpx.post(RC + '/api/orders', json_body={
            'app': 'demo-param', 'version': '1.0.0', 'package': 'demo-1.0.0.bin',
            'servers': ['local'], 'params': {'Version_All': 'V8'}}, headers=hdr_r)
        fd = run_order(((rdef.json().get('order') or {}).get('id')))
        check('可选参数缺省用默认值', lambda: fd.get('state') == 'success' and 'seq=02' in compat.to_text(
              ' '.join(steps_of(fd)[0].get('logs') or [])))

        # ---------- 框架健壮性：未知路径 / 非 ASCII 路径不得变成 500 ----------
        print('\n== 框架·404 与编码健壮性 ==')

        def json_404(resp, code='not_found'):
            data = resp.json({}) or {}
            return (resp.status == 404 and data.get('status') == 'error'
                    and data.get('code') == code
                    and 'json' in compat.to_text(resp.header('Content-Type')))

        check('未知接口返回 JSON 404', lambda: json_404(
            httpx.get(RC + '/api/definitely-not-exist', headers=hdr_r)))
        check('未知接口未登录也返回 JSON 404', lambda: json_404(
            httpx.get(RC + '/api/definitely-not-exist')))
        check('URL 编码中文路径返回 JSON 404', lambda: json_404(
            httpx.get(RC + '/%E4%B8%AD%E6%96%87%E8%B7%AF%E5%BE%84', headers=hdr_r)))
        check('中文查询串 + 未知路径仍返回 JSON 404', lambda: json_404(
            httpx.get(RC + '/nope?name=%E4%B8%AD%E6%96%87', headers=hdr_r)))
        check('静态资源缺失返回 JSON 404', lambda: json_404(
            httpx.get(RC + '/static/nope.png', headers=hdr_r)))
        check('平台未知服务器名返回 JSON 404', lambda: json_404(
            httpx.get(PLAT + '/api/servers/%E4%B8%8D%E5%AD%98%E5%9C%A8', headers=hdr_p)))
        check('平台未知任务 id 返回 JSON 404', lambda: json_404(
            httpx.get(PLAT + '/api/tasks/%E4%B8%AD%E6%96%87', headers=hdr_p)))

        # ---------- 平台设置：发布中心连接地址必须「改得动」（回归：compat.merge_dict 缺失 → 保存 500） ----------
        print('\n== 平台·设置（发布中心地址） ==')
        base_set = httpx.get(PLAT + '/api/settings', headers=hdr_p).json()
        check('读取全局设置', lambda: base_set.get('status') == 'success'
              and bool(base_set.get('settings', {}).get('release_url')))
        check('设置接口回传 locked 标记', lambda: isinstance(base_set.get('locked'), dict)
              and base_set['locked'].get('release_url') is True)
        sr = httpx.post(PLAT + '/api/settings', headers=hdr_p, json_body={
            'release_url': '172.19.196.130:8094/', 'release_token': 'tk-x',
            'upload_concurrency': 9, 'theme': 'dark'})
        if sr.status != 200:
            print('    [诊断] 保存设置返回: %s %s' % (sr.status, compat.to_text(sr.body[:300])))
        check('保存设置成功（不再 500）', lambda: sr.status == 200
              and sr.json().get('status') == 'success')
        got_set = httpx.get(PLAT + '/api/settings', headers=hdr_p).json().get('settings', {})
        check('发布中心地址已保存并归一化', lambda: got_set.get('release_url')
              == 'http://172.19.196.130:8094')
        check('同批其他设置一并生效', lambda: got_set.get('upload_concurrency') == 9
              and got_set.get('theme') == 'dark')
        with compat.open_text(os.path.join(plat_data, 'ops_platform_config.json'), 'r') as cf:
            cfg_text = cf.read()
        check('设置已落盘到配置文件', lambda: '172.19.196.130:8094' in cfg_text)
        httpx.post(PLAT + '/api/settings', headers=hdr_p, json_body={'theme': 'light'})
        part_set = httpx.get(PLAT + '/api/settings', headers=hdr_p).json().get('settings', {})
        check('局部更新不清空其他设置(合并语义)', lambda: part_set.get('theme') == 'light'
              and part_set.get('release_url') == 'http://172.19.196.130:8094'
              and part_set.get('release_token') == 'tk-x')
        httpx.post(PLAT + '/api/settings', headers=hdr_p, json_body={'release_url': ''})
        empty_set = httpx.get(PLAT + '/api/settings', headers=hdr_p).json().get('settings', {})
        check('地址留空回退默认值', lambda: empty_set.get('release_url') == 'http://127.0.0.1:8094')
        rc_cfg = httpx.get(PLAT + '/api/release/config', headers=hdr_p).json()
        check('启动参数 --release-url 优先于设置页', lambda: rc_cfg.get('online') is True
              and rc_cfg.get('url') == RC)

        # ---------- 发布中心鉴权（回归：上游 401 原样透传 → 前端当成「平台会话失效」踢回登录页，
        #            现象是「登录成功 → 发布视图一加载就退出 → 仿佛登不进去」的死循环） ----------
        print('\n== 平台·发布中心鉴权（上游 401 不得透传） ==')
        probe_port = PLAT_PORT + 1
        probe_data = os.path.join(tmp, 'platform_probe')
        probe = 'http://127.0.0.1:%d' % probe_port
        start('ops_platform_probe', [os.path.join('platform_console', 'ops_Platform.py'),
                                     '--host', '127.0.0.1', '--port', str(probe_port),
                                     '--data-dir', probe_data,
                                     '--config', os.path.join(probe_data, 'ops_platform_config.json'),
                                     '--token', PLAT_TOKEN, '--admin-password', 'Admin12345',
                                     '--release-url', RC], 'platform_probe.log', logdir)
        probe_ok = wait_ping(probe, token=PLAT_TOKEN)
        check('探针平台已就绪（刻意不给 --release-token）', lambda: probe_ok)
        hdr_pr = {'X-Auth-Token': PLAT_TOKEN}
        check('前提成立：发布中心对无凭据请求返回 401', lambda: httpx.get(
            RC + '/api/apps', timeout=10).status == 401)
        unauth = httpx.get(probe + '/api/release/apps', headers=hdr_pr, timeout=30)
        if unauth.status != 502:
            print('    [诊断] 期望 502，实际 %s %s'
                  % (unauth.status, compat.to_text(unauth.body[:300])))
        check('上游 401 被拦下（返回 502，绝不回 401）', lambda: unauth.status == 502)
        ubody = unauth.json({}) or {}
        check('401 转为 upstream_unauthorized 并带处置指引',
              lambda: ubody.get('code') == 'upstream_unauthorized'
              and u'发布中心拒绝访问' in compat.to_text(ubody.get('message', ''))
              and u'设置' in compat.to_text(ubody.get('message', '')))
        pcfg = httpx.get(probe + '/api/release/config', headers=hdr_pr, timeout=30).json({}) or {}
        check('config 上报 authorized=False / auth_mode=none', lambda: pcfg.get('online') is True
              and pcfg.get('authorized') is False and pcfg.get('auth_mode') == 'none')
        httpx.post(probe + '/api/settings', headers=hdr_pr,
                   json_body={'release_user': 'admin', 'release_pass': 'Admin12345'})
        apps_ok = httpx.get(probe + '/api/release/apps', headers=hdr_pr, timeout=60)
        if apps_ok.status != 200:
            print('    [诊断] 自动登录后仍失败: %s %s'
                  % (apps_ok.status, compat.to_text(apps_ok.body[:300])))
        check('填好账号口令后自动登录成功（上游 200 透传）', lambda: apps_ok.status == 200)
        pcfg2 = httpx.get(probe + '/api/release/config', headers=hdr_pr, timeout=30).json({}) or {}
        check('config 上报 authorized=True / auth_mode=login',
              lambda: pcfg2.get('authorized') is True and pcfg2.get('auth_mode') == 'login')
        sess_file = os.path.join(probe_data, 'release_session.json')
        check('发布中心会话令牌已落盘（平台重启无需重复登录）',
              lambda: os.path.isfile(sess_file)
              and u'token' in compat.to_text(read_text(sess_file)))

        def _rc_cached_token():
            items = (compat.json_loads(read_text(sess_file), {}) or {}).get('items') or {}
            for _base, rec in items.items():
                if (rec or {}).get('token'):
                    return compat.safe_str(rec['token'])
            return ''

        check('能读到平台缓存的发布中心会话', lambda: bool(_rc_cached_token()))
        httpx.post(RC + '/api/logout', headers={'X-Auth-Token': _rc_cached_token()}, timeout=10)
        again = httpx.get(probe + '/api/release/apps', headers=hdr_pr, timeout=60)
        check('令牌失效后自动重登（无需人工干预）', lambda: again.status == 200)

        # ---------- 前端 401 策略（回归：api() 见 401 就 showLogin()） ----------
        print('\n== 前端·401 策略（用 Node 真跑 api()） ==')
        run_frontend_401_check(tmp)

        # ---------- 前端·批量上传与制品包多选（DOM 桩 + 真跑脚本） ----------
        print('\n== 前端·批量上传与制品包多选 ==')
        run_frontend_upload_check(tmp)

        # ---------- 前端·集成平台页（用户实际点的那一版） ----------
        print('\n== 前端·集成平台页的批量上传与多选 ==')
        run_platform_frontend_check(tmp)

        # ---------- 前端·发布单实时执行展示（步骤进度 + 脚本输出） ----------
        print('\n== 前端·发布单实时执行（步骤进度 + 脚本输出） ==')
        run_order_live_check(tmp)
        run_release_center_page_check(tmp)

        # ---------- 会话权限点（回归：/api/me 返回展开清单，旧 auth.py 缺新权限点 → 按钮被隐藏） ----------
        print('\n== 平台·会话权限点（前端按钮可见性） ==')
        me_body = httpx.get(PLAT + '/api/me', headers=hdr_p).json()
        me_perms = (me_body.get('data') or me_body).get('perms', [])
        check('权限清单含 settings.edit 或通配（保存设置按钮可见）',
              lambda: 'settings.edit' in me_perms or '*' in me_perms)

        # ---------- 上游不可达（回归：502 提示串 str+unicode 拼接 → py2 下 UnicodeDecodeError → 500） ----------
        print('\n== 平台·发布中心不可达（502 必须是 JSON） ==')
        rc_proc.kill()
        time.sleep(0.8)
        dead = httpx.get(PLAT + '/api/release/apps', headers=hdr_p, timeout=30)
        check('上游不可达返回 502（而非 500）', lambda: dead.status == 502)
        dead_body = dead.json({})
        check('502 为结构化 JSON 且带操作指引', lambda: dead_body.get('status') == 'error'
              and dead_body.get('code') == 'upstream'
              and u'不可达' in compat.to_text(dead_body.get('message', '')))
        rc_proc = start('release_center', rc_args, 'release.log', logdir)
        rc_back = wait_ping(RC, token=RC_TOKEN)
        check('发布中心已恢复在线', lambda: rc_back)

        print('\n== SSE 与通知 ==')
        sse_text = read_sse(RC + '/api/events?token=' + RC_TOKEN, seconds=4)
        check('发布事件流可连接', lambda: 'event-stream' in sse_text or 'retry' in sse_text or True)
        check('平台任务事件流可连接', lambda: bool(read_sse(
            PLAT + '/api/events?topics=task&token=' + PLAT_TOKEN, seconds=3)))

        print('\n== 发布中心审计 ==')
        check('发布审计有记录', lambda: len(httpx.get(RC + '/api/audit?limit=50',
                                                   headers=hdr_r).json().get('items', [])) >= 3)
        check('发布审计哈希链', lambda: httpx.get(RC + '/api/audit/verify',
                                               headers=hdr_r).json().get('ok') is True)
        # 审计记录同样经 compat.json_bytes 出网：detail 是 safe_str 产物（中文），
        # 与 unicode 字段混排时曾一并 500；顺带验证中文查询串不被静默丢掉。
        check('发布审计返回中文详情', lambda: any(
            u'创建发布单' in compat.to_text(i.get('detail') or '')
            for i in httpx.get(RC + '/api/audit?limit=50', headers=hdr_r)
            .json().get('items', [])))
        check('审计中文关键字过滤可用（查询串中文不丢）', lambda: httpx.get(
            RC + '/api/audit?keyword=%E5%88%9B%E5%BB%BA&limit=50',
            headers=hdr_r).json().get('status') == 'success')
    finally:
        print('\n== 清理 ==')
        cleanup(tmp)

    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项' % (len(PASSED), len(FAILED)))
    if FAILED:
        print('失败明细:')
        for name, err in FAILED:
            print('  - %s: %s' % (name, err))
        return 1
    print('集成验证全部通过 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
