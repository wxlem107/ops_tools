#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ops_Platform.py —— 运维工具集成平台 (Ops Integrated Platform) v2.0

聚合两大子系统：
  1. 文件上传管理  —— 对接多台服务器上的 upload_server.py (v3.x)
  2. 命令执行管理  —— 对接多台服务器上的 web_shell.py   (v1.2+)
  3. 监控 / 资产台账 / 发布中心视图 / 多用户与审计（v2.0 新增）

v2.0 升级要点：
  * 后端用 opslib.server.HttpApp 声明式路由重构，所有接口带 perm 权限点与 summary；
  * 复用 opslib.auth（多用户/角色）、opslib.sse（SSE 实时通道）、opslib.task（异步任务）、
    opslib.audit（哈希链审计）、opslib.notify（通知）、opslib.probe（指标采集）；
  * 通用转发入口 + 连接池复用 + 上传性能优化（relay_request/_take_conn/_give_conn/
    _conn_usable/HOP_BY_HOP）原样保留，仅做适配（兼容层改走 opslib.compat）；
  * 运行数据统一落到 <脚本目录>/data/（配置、用户、审计、任务、台账、通知）。

v2.1.0：
  * 服务器墙批量维护：新增 GET /api/servers/export（format=json 含目标机令牌、
    需 server.edit；format=csv 明文表、server.view 即可）与
    POST /api/servers/import（merge 覆盖 / skip 跳过，逐条 normalize 校验，
    坏条目记入 errors 不整体失败，单次上限 500 台，写审计 server.import）。
    前端工具栏新增「导出 JSON / 导出 CSV / 导入」与导入弹层（选文件或粘贴 JSON）。
  * 服务器配置新增 upload_dir（远端上传目录）：新增/编辑弹层可填，normalize_server
    归一（去空白与尾部分隔符）；导入/导出自动携带，CSV 导出加「上传目录」列。
    上传时（chunk/put 查询串、chunk/init、chunk/complete JSON 体）作为 dir 参数
    传给 upload_server v3.3.0+（需目标机 --allow-client-dir 启动，否则远端静默
    回落其默认目录）；前端按响应里的真实落盘路径校验配置是否生效，不一致时
    每台服务器提示一次，避免「以为传到了配置目录」。
v2.0.9：
  * 安全加固（配合 CODE_REVIEW 整改）：
    - 横向越权（IDOR）：新增 require_server(req, name)，服务器详情/状态/改删/文件浏览/
      转发/终端执行等所有「按名接口」都先校验 can_access_server，受限账号不再能操作
      未授权服务器；批量删文件改为逐台校验并跳过无权目标。
    - 凭据脱敏：新增 redact_server(req, item)，无 server.edit 权限者读取服务器列表/详情
      时不再下发目标机 upload_token / shell_token；settings 视图无 settings.edit 时同时
      清空 release_pass 与 release_token。
    - 命令注入面收敛：转发给上游的 POST 体统一走 compat.json_bytes（py3 必须 bytes，
      py2 非 ASCII 也不崩）；POST 通道真正可用后，URI 不再退化出 `?cmd=`。
  * 连接与事件流健壮性：
    - raw_sse_proxy 改为三态返回 'ok'/'nogood'/'partial'，只有「未写出任何事件」才允许
      GET / 同步降级 —— 防止已开跑的命令被重放（发布/执行只跑一遍）。
    - 上游 EOF 后立即 break，消除空 read 导致的 sleep+continue 死循环（线程/连接泄漏）。
    - _take_conn 复用连接时 _apply_conn_timeout 同步改 conn.timeout 与 sock.settimeout，
      对齐 opslib.httpx v1.0.5 修复。
  * 其它：csv_response 分 py2(BytesIO)/py3(StringIO)；新增 maybe_json(req)（'0' 视为空体）
    与 opt_float(value, default)，修复批量探测/通知测试/审计区间在空体与非法参数下的 500。
v2.0.8：
  * 服务器列表/详情/状态接口的在线探测改为「并发 + TTL 缓存」（probe_many /
    probe_cached，TTL 15s；增删改清缓存，batch-probe 强刷并回填）。原先每次请求都对
    每台机器串行实时探测（每台最多 3 个请求 × timeout=2s），离线机器让列表请求等
    N×2~6s —— 用户「编辑后点保存」时保存本身已成功，但随后的自动刷新长时间不返回，
    界面表现就是「改了保存不了」。并发后冷启动也只等约单台耗时，命中缓存则 <10ms。
v2.0.7：
  * 终端执行不再把命令写进 URL：新增 POST /api/servers/<name>/exec-ticket 换取
    一次性执行券（命令在请求体、令牌在请求头），前端用
    GET …/exec-stream?ticket=xxx 取流。原因是 URL 上出现 `?cmd=whoami&token=...`
    会被态势感知/IDS 的「webshell 命令执行」特征规则命中（现场实际误报）。
    券 60 秒有效、用后即焚、绑定来源 IP；旧的 `?cmd=` 形态保留兼容。
  * 转发到 web_shell 上游优先走 POST /api/exec/stream（命令在请求体），
    上游为老版本时自动退回查询串形态，再不行才走同步降级。

运行时：Python 2.7.5+ 与 Python 3.7.5+ 均可直接运行，只用标准库。

【用法示例】
    # 1) 最简启动：默认 0.0.0.0:8095，首启自动创建管理员 admin（随机口令，见启动横幅）
    python ops_Platform.py

    # 2) 指定地址/端口 + 开放接口令牌（CI/CD 等价管理员）
    python ops_Platform.py --host 0.0.0.0 --port 8095 --token ci_token_123

    # 3) 生产推荐：全部显式参数（单行，可直接复制）
    python ops_Platform.py --host 0.0.0.0 --port 8095 --token ci_token_123 --config /data/ops/ops_platform_config.json --data-dir /data/ops --static-dir /opt/ops/static --admin-password Admin_123! --release-url http://127.0.0.1:8094 --release-token rc_token_123

    # 4) 指定首启管理员口令 / 忘记口令时重置（未给 --admin-password 则随机生成并打印在横幅）
    python ops_Platform.py --port 8095 --admin-password Admin_123!
    python ops_Platform.py --port 8095 --reset-admin-password --admin-password NewPass_456!

    # 5) 内网演示：关闭登录要求（全开放，慎用）
    python ops_Platform.py --port 8095 --no-login

    # 6) 联动发布中心（默认 http://127.0.0.1:8094）
    python ops_Platform.py --port 8095 --release-url http://10.0.0.9:8094 --release-token rc_token_123

    # 7) 后台常驻（nohup）
    nohup python -u ops_Platform.py --port 8095 --token ci_token_123 > /data/ops_Platform.log 2>&1 &

    # 8) 查看全部参数与默认值
    python ops_Platform.py --help

【全部参数】（括号内为默认值）
    --host                监听地址               (0.0.0.0)                       环境变量 OPS_HOST
    --port                监听端口               (8095)                          环境变量 OPS_PORT
    --config              配置文件路径           (data/ops_platform_config.json) 环境变量 OPS_CONFIG
    --token               开放接口令牌           (空 = 不校验，CI/CD 等价管理员)  环境变量 OPS_TOKEN
    --static-dir          静态页面目录           (脚本同级 static/)              环境变量 OPS_STATIC
    --data-dir            运行数据目录           (脚本同级 data/)                环境变量 OPS_DATA_DIR
    --admin-password      首启管理员初始口令      (空 = 随机生成，见启动横幅)      环境变量 OPS_ADMIN_PASSWORD
    --reset-admin-password 启动时重置 admin 口令  (关)                            无环境变量
    --no-login            关闭登录要求（全开放）  (关)                            无环境变量
    --release-url         发布中心地址           (http://127.0.0.1:8094)         环境变量 OPS_RELEASE_URL
    --release-token       发布中心令牌           (空)                            环境变量 OPS_RELEASE_TOKEN
    --help, -h            打印帮助后退出         无环境变量
    说明：--admin-password 仅在首次启动（用户库不存在）时生效；重置请配合 --reset-admin-password。
"""
from __future__ import print_function

import argparse
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from collections import deque

# ================= 引入 opslib 公共层（与脚本同级目录） =================
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from opslib import compat, server, apibase, auth as opsauth, sse, httpx, probe, audit, task, notify
from opslib.apibase import ok, err, ApiError
from opslib.sse import BUS, stream_events
from opslib.store import JsonStore

VERSION = '2.1.0'
DEFAULT_SETTINGS = {
    'access_mode': 'auto', 'upload_concurrency': 6, 'chunk_mb': 0,
    'release_url': 'http://127.0.0.1:8094', 'release_token': '',
    'release_user': '', 'release_pass': '',
    'metrics_interval': 10, 'server_probe_interval': 30, 'theme': 'auto',
}


# ================= 命令行参数 =================
def parse_args():
    p = argparse.ArgumentParser(description='运维工具集成平台 v%s' % VERSION)
    p.add_argument('--host', default=os.environ.get('OPS_HOST'), help='监听地址 (默认 0.0.0.0)')
    p.add_argument('--port', type=int, default=os.environ.get('OPS_PORT'), help='监听端口 (默认 8095)')
    p.add_argument('--config', default=os.environ.get('OPS_CONFIG'),
                   help='配置文件路径（默认 data/ops_platform_config.json）')
    p.add_argument('--token', default=os.environ.get('OPS_TOKEN', ''),
                   help='开放接口令牌（CI/CD 等价管理员），设置后需携带方可调用 /api/*')
    p.add_argument('--static-dir', default=os.environ.get('OPS_STATIC', ''),
                   help='静态页面目录（默认与 ops_Platform.py 同级的 static）')
    p.add_argument('--data-dir', default=os.environ.get('OPS_DATA_DIR', ''),
                   help='运行数据目录（默认 <脚本目录>/data）')
    p.add_argument('--admin-password', default=os.environ.get('OPS_ADMIN_PASSWORD', ''),
                   help='首次启动管理员 admin 的初始口令（用户库已存在时不生效，'
                        '重置请配合 --reset-admin-password）')
    p.add_argument('--reset-admin-password', action='store_true',
                   help='启动时把 admin 口令重置为 --admin-password（未提供则生成随机口令）')
    p.add_argument('--no-login', action='store_true',
                   help='关闭登录要求（全开放，供内网演示）')
    p.add_argument('--release-url', default=os.environ.get('OPS_RELEASE_URL', ''),
                   help='发布中心地址（默认 http://127.0.0.1:8094）')
    p.add_argument('--release-token', default=os.environ.get('OPS_RELEASE_TOKEN', ''),
                   help='发布中心令牌（作为 X-Auth-Token 转发）')
    return p.parse_args()


ARGS = parse_args()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(ARGS.data_dir or os.path.join(BASE_DIR, 'data'))
STATIC_DIR = os.path.abspath(ARGS.static_dir or os.path.join(BASE_DIR, 'static'))
CONFIG_PATH = os.path.abspath(
    ARGS.config or os.path.join(DATA_DIR, 'ops_platform_config.json'))

compat.makedirs(DATA_DIR)


# ================= 旧配置迁移（只做一次，优雅失败） =================
def migrate_legacy_config():
    old = os.path.join(BASE_DIR, 'ops_platform_config.json')
    if os.path.exists(old) and not os.path.exists(CONFIG_PATH):
        try:
            with compat.open_text(old, 'r') as f:
                data = compat.json_loads(f.read(), None)
            if isinstance(data, dict):
                CONFIG.save(data)
                try:
                    os.remove(old)
                except Exception:
                    pass
                compat.log('[config] 已把旧配置迁移到 %s' % CONFIG_PATH)
        except Exception as e:
            compat.log('[warn] 旧配置迁移失败（已跳过）: %s' % e)


# ================= 配置 / 数据落地 =================
CONFIG = JsonStore(CONFIG_PATH, {'servers': [], 'settings': dict(DEFAULT_SETTINGS)})
AUTH = opsauth.Auth(DATA_DIR, legacy_token=ARGS.token, require_login=(not ARGS.no_login),
                    admin_password=ARGS.admin_password or None)
if ARGS.reset_admin_password:
    _ok, _msg, _created = AUTH.admin_reset_password(ARGS.admin_password or None)
    if _ok:
        compat.log('[auth] admin 口令已%s：%s' % (
            '创建' if _created else '重置', _msg))
    else:
        compat.log('[auth] admin 口令重置失败：%s' % _msg)
AUDIT = audit.AuditLog(os.path.join(DATA_DIR, 'audit.jsonl'))
TM = task.TaskManager(os.path.join(DATA_DIR, 'tasks.json'), max_workers=4, bus=BUS)
NOTIFIER = notify.Notifier(os.path.join(DATA_DIR, 'notify.json'))
CMDB = JsonStore(os.path.join(DATA_DIR, 'cmdb.json'), {'items': {}})
# 平台→发布中心的会话令牌缓存（按 base 存，避免平台每重启一次都要重新登录；
# 发布中心重启后令牌会失效，靠 release_proxy 里的 401 重登兜住）
RC_SESSION = JsonStore(os.path.join(DATA_DIR, 'release_session.json'), {'items': {}})

app = server.HttpApp('ops_Platform', VERSION, BASE_DIR, static_dir=STATIC_DIR,
                     data_dir=DATA_DIR, auth=AUTH, desc='运维工具集成平台')


# ================= 配置 / 工具函数 =================
def current_settings():
    cfg = CONFIG.load()
    st = dict(DEFAULT_SETTINGS)
    st.update(cfg.get('settings') or {})
    return st


def normalize_settings(raw):
    st = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        st.update(raw)
    st['access_mode'] = raw.get('access_mode') if isinstance(raw, dict) else st['access_mode']
    if not st['access_mode'] in ('auto', 'direct', 'proxy'):
        st['access_mode'] = 'auto'
    try:
        st['upload_concurrency'] = min(max(int(st.get('upload_concurrency') or 6), 1), 16)
    except (TypeError, ValueError):
        st['upload_concurrency'] = 6
    try:
        st['chunk_mb'] = min(max(int(st.get('chunk_mb') or 0), 0), 256)
    except (TypeError, ValueError):
        st['chunk_mb'] = 0
    try:
        st['metrics_interval'] = min(max(int(st.get('metrics_interval') or 10), 1), 600)
    except (TypeError, ValueError):
        st['metrics_interval'] = 10
    try:
        st['server_probe_interval'] = min(max(int(st.get('server_probe_interval') or 30), 1), 600)
    except (TypeError, ValueError):
        st['server_probe_interval'] = 30
    if st.get('theme') not in ('light', 'dark', 'auto'):
        st['theme'] = 'auto'
    st['release_url'] = normalize_base_url(st.get('release_url'))
    st['release_token'] = compat.safe_str(st.get('release_token') or '').strip()
    st['release_user'] = compat.safe_str(st.get('release_user') or '').strip()
    st['release_pass'] = compat.safe_str(st.get('release_pass') or '')
    return st


def load_servers():
    return CONFIG.load().get('servers', [])


def find_server(name):
    name = compat.safe_str(name)
    for s in load_servers():
        if s.get('name') == name:
            return s
    return None


def require_server(req, name):
    """按名取服务器并校验当前用户对该服务器的访问授权。

    「按名接口」（exec/download/转发等）原先只做 find_server，从不校验
    Auth.can_access_server —— 于是一个被限制到 ['web-01'] 的账号可以直接请求
    /api/servers/db-02/exec 执行命令、下载/删除文件（横向越权）。
    所有按名接口都必须走这里。
    """
    found = find_server(name)
    if not found:
        raise ApiError('服务器不存在: %s' % compat.safe_str(name), 404, 'not_found')
    session = getattr(req, 'session', None)
    if session is not None and app.auth is not None \
            and not app.auth.can_access_server(session, compat.safe_str(name)):
        raise ApiError('无权访问该服务器: %s' % compat.safe_str(name), 403, 'forbidden')
    return found


def has_upload(s):
    return s.get('type') in ('upload', 'both')


def has_shell(s):
    return s.get('type') in ('shell', 'both')


def upload_auth_headers(srv):
    t = srv.get('upload_token')
    return {'X-Auth-Token': t} if t else {}


def shell_auth_headers(srv):
    t = srv.get('shell_token')
    return {'X-Auth-Token': t} if t else {}


def redact_server(req, item):
    """无 server.edit 权限时剔除目标机凭据字段。

    upload_token / shell_token 是目标机服务的直连令牌，拿到即可绕过平台。
    列表与详情接口对外只读（server.view），绝不能把凭据下发给只读账号。
    """
    if app.auth is not None and app.auth.has_perm(req.session, 'server.edit'):
        return item
    for key in ('upload_token', 'shell_token'):
        if item.get(key):
            item[key] = ''
    return item


def clamp_timeout(value, default=30):
    try:
        return min(max(int(value or default), 1), 600)
    except (TypeError, ValueError):
        return default


def maybe_json(req):
    """优雅地读取「可有可无」的 JSON 请求体。

    `req.headers.get('Content-Length')` 在空体时是字符串 '0'——它是真值，
    会让 `req.json()` 去解析空 body 并报 400（批量探测 / 通知自测都踩过）。
    """
    cl = (req.headers.get('Content-Length') or '').strip()
    if cl and cl != '0':
        return req.json()
    return {}


def opt_float(value, default=None):
    """把查询参数安全转成 float；非法值返回 default 而不是抛 500。"""
    if value in (None, ''):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def select_servers(selector):
    selector = selector or {}
    servers = load_servers()
    names = selector.get('names') or []
    tags = selector.get('tags') or []
    env = compat.safe_str(selector.get('env') or '')
    group = compat.safe_str(selector.get('group') or '')
    if not (names or tags or env or group):
        return list(servers)
    out = []
    for s in servers:
        if names and s.get('name') in names:
            out.append(s)
            continue
        ok_env = (not env) or s.get('env') == env
        ok_group = (not group) or s.get('group') == group
        stags = s.get('tags') or []
        ok_tags = (not tags) or any(t in stags for t in tags)
        if ok_env and ok_group and ok_tags:
            out.append(s)
    return out


def role_perms_for_client(role):
    """返回给前端的权限列表：保留原始 '*' 通配。

    此前这里返回展开后的 opsauth.PERMS 清单，一旦目标机上的 opslib/auth.py
    是旧版（PERMS 缺少新权限点，如 settings.edit），后端按 '*' 放行、
    前端却拿不到该权限点 → 「保存设置」等按钮被 applyPerms 隐藏。
    前端 can() 本身就支持 '*'，直接回原始列表即可，不再依赖清单完整性。
    """
    return list(AUTH.role_perms(role))


# ================= 通用转发（连接池复用，原样保留行为） =================
CONN_POOL = {}
CONN_POOL_LOCK = threading.Lock()
CONN_IDLE_SECONDS = 60
CONN_POOL_MAX = 8
RELAY_BUFSIZE = 1024 * 1024


def _conn_usable(conn):
    sock = getattr(conn, 'sock', None)
    if sock is None:
        return False
    try:
        readable, _, _ = compat.select.select([sock], [], [], 0)
    except Exception:
        return False
    if not readable:
        return True
    try:
        peek = sock.recv(1, getattr(compat.socket, 'MSG_PEEK', 2))
        return len(peek) > 0
    except Exception:
        return False


def _apply_conn_timeout(conn, timeout):
    """复用池中连接时重设 socket 超时。

    池里的连接是「创建它的那次请求」按自己的 timeout 建的；keep-alive 复用后
    http.client 不会刷新 socket 超时，于是大文件上传（timeout=3600）复用一次
    删除请求（timeout=30）留下的连接时，可能在上游 hang 时被 30s 读超时打断，
    反之亦然。opslib/httpx 已修复同类问题，此处对齐。
    """
    try:
        conn.timeout = timeout
    except Exception:                                            # noqa: BLE001
        pass
    sock = getattr(conn, 'sock', None)
    if sock is not None:
        try:
            sock.settimeout(timeout)
        except Exception:                                        # noqa: BLE001
            pass


def _take_conn(host, port, timeout):
    key = '%s:%d' % (host, port)
    with CONN_POOL_LOCK:
        q = CONN_POOL.get(key)
        while q:
            conn, ts = q.popleft()
            if time.time() - ts > CONN_IDLE_SECONDS or not _conn_usable(conn):
                _close_conn(conn)
                continue
            _apply_conn_timeout(conn, timeout)
            return conn
    return compat.http_client.HTTPConnection(host, port, timeout=timeout)


def _give_conn(host, port, conn):
    key = '%s:%d' % (host, port)
    with CONN_POOL_LOCK:
        q = CONN_POOL.setdefault(key, deque())
        if len(q) >= CONN_POOL_MAX:
            try:
                conn.close()
            except Exception:
                pass
            return
        q.append((conn, time.time()))


def _new_deque():
    return deque()


def _close_conn(conn):
    try:
        conn.close()
    except Exception:
        pass


def relay_request(srv, kind, method, path, headers=None, body=None,
                  src=None, src_len=0, timeout=60):
    """把请求转发到远端 upload_server / web_shell。

    返回 (status, headers_dict, body_bytes)；status=0 表示转发失败（body 为错误说明）。
    src 非空时从 src（通常是 rfile）流式读取并即时转发。
    """
    host = srv.get('host')
    port = int(srv.get(kind + '_port') or (8089 if kind == 'upload' else 8088))
    hdrs = {'User-Agent': 'ops_Platform.py/%s' % VERSION, 'Accept': '*/*'}
    hdrs.update(upload_auth_headers(srv) if kind == 'upload' else shell_auth_headers(srv))
    if headers:
        hdrs.update(headers)
    if src is not None:
        hdrs['Content-Length'] = str(src_len)
    elif body is not None:
        hdrs['Content-Length'] = str(len(body))

    last_err = None
    for attempt in (0, 1):
        conn = _take_conn(host, port, timeout) if attempt == 0 else \
            compat.http_client.HTTPConnection(host, port, timeout=timeout)
        remaining = src_len
        sent = False
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()
            if src is not None:
                while remaining > 0:
                    buf = src.read(min(RELAY_BUFSIZE, remaining))
                    if not buf:
                        break
                    conn.send(buf)
                    sent = True
                    remaining -= len(buf)
            elif body:
                conn.send(body)
                sent = True
            resp = conn.getresponse()
            raw = resp.read()
            status, rheaders = resp.status, dict(resp.getheaders())
            if 200 <= status < 500 and conn.sock is not None:
                _give_conn(host, port, conn)
            else:
                _close_conn(conn)
            return status, rheaders, raw
        except Exception as e:
            last_err = e
            _close_conn(conn)
            # 已从 src 读走的字节数：只要 > 0 就无法安全重放整个请求体。
            # 特别注意：**不要**在此排空 src —— 那会把剩余请求体读掉，
            # 下一次（重试的）尝试就会发出「声明了 Content-Length 却 0 字节」的
            # 畸形请求，导致上游挂起、上传静默损坏。
            consumed = src_len - remaining
            if sent or consumed > 0 or src is None or attempt == 1:
                break
    return 0, {}, compat.to_bytes('PROXY_ERROR: %s' % last_err)


class _RelayBody(object):
    """上游响应体的惰性迭代器：边读边转发，管理机不全量缓冲。

    兼容 py2/py3 迭代协议（next / __next__）；读完或出错都关闭连接。
    """

    def __init__(self, conn, resp, chunk=256 * 1024):
        self._conn = conn
        self._resp = resp
        self._chunk = chunk
        self._done = False

    def close(self):
        self._done = True
        try:
            self._conn.close()
        except Exception:                                     # noqa: BLE001
            pass

    def __iter__(self):
        return self

    def next(self):                                           # py2
        return self.__next__()

    def __next__(self):                                       # py3
        if self._done:
            raise StopIteration()
        try:
            buf = self._resp.read(self._chunk)
        except Exception:
            self.close()
            raise
        if not buf:
            self.close()
            raise StopIteration()
        return buf


def relay_stream(srv, kind, method, path, headers=None, body=None, timeout=60):
    """转发请求，返回 (status, headers_dict, body迭代器)；status=0 表示转发失败。

    与 relay_request 的区别：不把上游响应体读进内存，而是交给调用方边收边发，
    大包（批量下载 zip 等）经过管理机时只占一块缓冲，避免 py2 下大文件把
    管理机内存打爆后连接中途断掉（浏览器报 ERR_INVALID_RESPONSE）。
    """
    host = srv.get('host')
    port = int(srv.get(kind + '_port') or (8089 if kind == 'upload' else 8088))
    hdrs = {'User-Agent': 'ops_Platform.py/%s' % VERSION, 'Accept': '*/*'}
    hdrs.update(upload_auth_headers(srv) if kind == 'upload' else shell_auth_headers(srv))
    if headers:
        hdrs.update(headers)
    if body is not None:
        hdrs['Content-Length'] = str(len(body))
    conn = None
    try:
        conn = compat.http_client.HTTPConnection(host, port, timeout=timeout)
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), _RelayBody(conn, resp)
    except Exception as e:                                    # noqa: BLE001
        if conn is not None:
            try:
                conn.close()
            except Exception:                                 # noqa: BLE001
                pass
        return 0, {}, _RelayBody(None, None)                  # 空迭代器占位


def relay_header(headers, name, default=''):
    """从上游响应头字典里取值（大小写不敏感；http.client 保留服务端原始大小写，
    例如 upload_server 写的是 `Content-type`，用 rh.get('Content-Type') 会取空）。"""
    lname = compat.to_text(name).lower()
    for k, v in (headers or {}).items():
        if compat.to_text(k).lower() == lname:
            return compat.to_text(v)
    return default


HOP_BY_HOP = {'host', 'content-length', 'connection', 'keep-alive', 'transfer-encoding',
              'upgrade', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
              'accept-encoding', 'content-encoding', 'x-auth-token'}


def relay_headers(src_headers):
    out = {}
    for k, v in src_headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    return out


# ================= 指标 / 资产采集 =================
def shell_exec(srv, cmd, timeout=15):
    """经 web_shell 执行命令，返回 (ok, output)

    兼容两代 web_shell 响应：v1.2 返回 {stdout, stderr, code, ...}，
    v1.1 返回 {output, ...}，这里统一取 stdout（缺省回退 output）。
    """
    url = httpx.join_url(srv['host'], srv.get('shell_port', 8088), '/api/exec')
    resp = httpx.DEFAULT.post(url, json_body={'cmd': cmd, 'timeout': timeout},
                              headers=shell_auth_headers(srv), timeout=timeout + 5)
    if resp.status == 0:
        return False, ''
    data = resp.json({}) or {}
    if data.get('status') == 'success':
        out = data.get('stdout', data.get('output', ''))
        return True, compat.to_text(out)
    msg = (data.get('stderr') or data.get('message') or data.get('stdout') or '')
    return False, compat.to_text(msg)


def shell_metrics(srv):
    """无 host_monitor 探针时，用 web_shell 执行采集命令降级出监控指标"""
    m = {'online': True, 'ts': time.time()}
    ok, out = shell_exec(srv, 'cat /proc/loadavg', 5)
    load = [0.0, 0.0, 0.0]
    if ok:
        try:
            load = [float(x) for x in out.split()[:3]]
        except Exception:
            pass
    m['load'] = load
    ok, out = shell_exec(srv, 'free -b', 5)
    mem_total = mem_used = mem_free = swap_total = swap_used = 0
    if ok:
        for line in out.splitlines():
            fs = line.split()
            if len(fs) >= 3 and fs[0].startswith('Mem:'):
                try:
                    mem_total, mem_used, mem_free = int(fs[1]), int(fs[2]), int(fs[3])
                except Exception:
                    pass
            elif len(fs) >= 3 and fs[0].startswith('Swap:'):
                try:
                    swap_total, swap_used = int(fs[1]), int(fs[2])
                except Exception:
                    pass
    m['mem'] = {'total': mem_total, 'used': mem_used, 'free': mem_free,
               'percent': round(100.0 * mem_used / mem_total, 1) if mem_total else 0.0}
    m['swap'] = {'total': swap_total, 'used': swap_used,
                 'percent': round(100.0 * swap_used / swap_total, 1) if swap_total else 0.0}
    ok, out = shell_exec(srv, 'df -P -B1', 5)
    disks = []
    if ok:
        for line in out.splitlines()[1:]:
            fs = line.split()
            if len(fs) >= 6:
                try:
                    total, used = int(fs[1]), int(fs[2])
                    disks.append({'mount': fs[5], 'total': total, 'used': used,
                                  'free': total - used,
                                  'percent': round(100.0 * used / total, 1) if total else 0.0})
                except Exception:
                    pass
    m['disks'] = disks
    ok, out = shell_exec(srv, 'nproc', 5)
    cores = 1
    if ok:
        try:
            cores = int(out.strip()) or 1
        except Exception:
            pass
    m['cpu_cores'] = cores
    m['cpu_percent'] = 0.0
    ok, out = shell_exec(srv, 'cat /proc/net/dev', 5)
    rx = tx = 0
    if ok:
        for line in out.splitlines():
            if ':' not in line:
                continue
            iface, rest = line.split(':', 1)
            if iface.strip() == 'lo':
                continue
            f = rest.split()
            if len(f) >= 9:
                try:
                    rx += int(f[0])
                    tx += int(f[8])
                except Exception:
                    pass
    m['net'] = {'rx': rx, 'tx': tx, 'rx_rate': 0.0, 'tx_rate': 0.0}
    m['disk_io'] = {'read': 0, 'write': 0, 'read_rate': 0.0, 'write_rate': 0.0}
    m['uptime'] = 0.0
    m['processes'] = 0
    m['connections'] = 0
    return m


def collect_metrics(srv):
    """优先 host_monitor 探针 -> 失败降级 web_shell -> 都不可用 online:false。
    本机（127.0.0.1）直接用 opslib.probe，便于单机演示。"""
    if srv.get('host') in ('127.0.0.1', 'localhost', '::1'):
        try:
            m = probe.metrics()
            m['online'] = True
            return m
        except Exception:
            pass
    url = httpx.join_url(srv['host'], srv.get('metrics_port', 8040), '/api/metrics')
    r = httpx.DEFAULT.get(url, timeout=5)
    if r.status == 200:
        d = r.json({}) or {}
        if isinstance(d, dict) and d:
            d.setdefault('online', True)
            return d
    if has_shell(srv):
        return shell_metrics(srv)
    return {'online': False}


def collect_sysinfo(srv):
    if srv.get('host') in ('127.0.0.1', 'localhost', '::1'):
        try:
            return probe.sys_info()
        except Exception:
            pass
    url = httpx.join_url(srv['host'], srv.get('metrics_port', 8040), '/api/sysinfo')
    r = httpx.DEFAULT.get(url, timeout=5)
    if r.status == 200:
        d = r.json({}) or {}
        if isinstance(d, dict) and d:
            return d
    if has_shell(srv):
        info = {}
        ok, out = shell_exec(srv,
                             'hostname; uname -srm; cat /etc/os-release 2>/dev/null | head -4; nproc', 10)
        if ok:
            lines = out.splitlines()
            if lines:
                info['hostname'] = lines[0].strip()
            info['raw'] = out
        info['collected_at'] = time.time()
        return info
    return {'online': False, 'collected_at': time.time()}


def _probe(srv):
    result = {'upload': None, 'shell': None, 'metrics': None, 'online': False}
    if has_upload(srv):
        code = httpx.DEFAULT.get(httpx.join_url(srv['host'], srv.get('upload_port', 8089), '/api/status'),
                                  headers=upload_auth_headers(srv), timeout=2).status
        result['upload'] = {'online': code == 200}
    if has_shell(srv):
        code = httpx.DEFAULT.get(httpx.join_url(srv['host'], srv.get('shell_port', 8088), '/api/status'),
                                  headers=shell_auth_headers(srv), timeout=2).status
        result['shell'] = {'online': code == 200}
    if srv.get('host') in ('127.0.0.1', 'localhost', '::1'):
        result['metrics'] = {'online': True}
    else:
        code = httpx.DEFAULT.get(httpx.join_url(srv['host'], srv.get('metrics_port', 8040), '/api/metrics'),
                                 timeout=2).status
        result['metrics'] = {'online': code == 200}
    result['online'] = bool((result['upload'] or {}).get('online')
                             or (result['shell'] or {}).get('online')
                             or (result['metrics'] or {}).get('online'))
    return result


# ---- 探测结果缓存（v2.0.8）----
# _probe() 对每台机器最多发 3 个请求、每个 timeout=2s。列表/详情接口若每次都实时探测，
# 遇到离线目标机就要等 N×2~6s：页面「保存后自动刷新」长时间不返回，用户看到的现象就是
# 「改了保存不了」。这里给探测结果加 TTL 缓存，高频读接口用缓存值；需要真值走
# /api/servers/batch-probe（ttl=0 强刷）或定时采集。
_PROBE_CACHE = {}
_PROBE_CACHE_LOCK = threading.Lock()
PROBE_CACHE_TTL = 15


def _probe_key(srv):
    return '%s|%s|%s|%s|%s' % (srv.get('name'), srv.get('host'), srv.get('type'),
                               srv.get('upload_port'), srv.get('shell_port'))


def probe_cached(srv, ttl=None):
    """带 TTL 的探测结果缓存（ttl=0 表示强制刷新并回填缓存）。

    时间戳必须取**探测完成之后**的：探测本身要几秒（离线机每个请求都等到超时），
    若用探测开始前的时间记账，多台机器时第一条记录在写下时已接近过期，缓存永远
    不命中 —— 表现为列表每次还是 N×6s（这个坑已用真实计时发现并回归）。
    """
    key = _probe_key(srv)
    ttl = PROBE_CACHE_TTL if ttl is None else ttl
    if ttl > 0:
        with _PROBE_CACHE_LOCK:
            hit = _PROBE_CACHE.get(key)
            if hit and (time.time() - hit[0]) < ttl:
                return dict(hit[1])
    result = _probe(srv)
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[key] = (time.time(), result)
    return dict(result)


def probe_many(servers, ttl=None, workers=8):
    """并发探测多台服务器，返回 {名称: status}。

    _probe() 对离线目标机的每个请求都要等到超时（最多 3 个 × 2s）。串行遍历时 N 台
    离线机就是 N×6s —— 列表页长时间转圈，用户会当成「改了没保存」。这里用有界并发把
    总耗时压到约「单台耗时 × ⌈N/并发数⌉」，同时不淹没被探测的机器。
    """
    items = [s for s in servers]
    results = {}
    if not items:
        return results
    workers = max(1, min(int(workers or 8), len(items), 16))
    cursor = {'i': 0}
    lock = threading.Lock()

    def _worker():
        while True:
            with lock:
                i = cursor['i']
                if i >= len(items):
                    return
                cursor['i'] = i + 1
            srv = items[i]
            try:
                results[srv.get('name')] = probe_cached(srv, ttl=ttl)
            except Exception:                                    # noqa: BLE001
                results[srv.get('name')] = {'upload': None, 'shell': None,
                                            'metrics': None, 'online': False}

    threads = []
    for _ in range(workers):
        t = threading.Thread(target=_worker)
        t.daemon = True
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


def invalidate_probe(name=None):
    """服务器增删改后清掉对应缓存（改了端口/地址后不该继续显示旧状态）。"""
    with _PROBE_CACHE_LOCK:
        if name is None:
            _PROBE_CACHE.clear()
            return
        for key in [k for k in _PROBE_CACHE if k.split('|', 1)[0] == compat.safe_str(name)]:
            _PROBE_CACHE.pop(key, None)


def normalize_server(data, defaults=None):
    d = dict(defaults or {})
    for k in ['name', 'host', 'type', 'upload_token', 'shell_token', 'note',
              'access_mode', 'group', 'env', 'upload_dir']:
        if k in data:
            d[k] = data[k]
    if d.get('type') not in ('upload', 'shell', 'both'):
        d['type'] = 'both'
    d['access_mode'] = normalize_mode(d.get('access_mode'))
    for pk, dv in (('upload_port', 8089), ('shell_port', 8088), ('metrics_port', 8040)):
        try:
            d[pk] = int(data.get(pk) or d.get(pk) or dv)
        except (TypeError, ValueError):
            d[pk] = dv
    d['tags'] = list(data.get('tags') or d.get('tags') or [])
    if d.get('env') not in ('prod', 'test', 'dev'):
        d['env'] = d.get('env') or ''
    d['group'] = compat.safe_str(d.get('group') or '')
    # 远端上传目录（v2.1.0）：上传请求会以 dir 参数传给 upload_server（v3.3.0+，
    # 需目标机 --allow-client-dir 开启；未开启时远端静默回落其 UPLOAD_DIR）。
    # 归一只做去空白与去尾分隔符，不做存在性校验（目录由远端按需创建）。
    d['upload_dir'] = compat.safe_str(d.get('upload_dir') or '').strip().rstrip('/\\')
    return d


def normalize_mode(value):
    v = compat.safe_str(value or '').strip().lower()
    return v if v in ('auto', 'direct', 'proxy') else 'auto'


# ================= 发布中心透传 =================
def normalize_base_url(value, default='http://127.0.0.1:8094'):
    """把用户填写的「发布中心地址」归一成可用的 base URL。

    设置页允许粘贴 `172.19.196.130:8094`、`http://host:8094/`、` host:8094 `
    这类写法，这里统一补 scheme、去空格、去末尾斜杠。否则
    `urlparse('172.19.196.130:8094')` 会把主机名当成路径（scheme 为空、
    hostname 为空），透传时会直接打到 127.0.0.1:80 而报不可达。
    """
    url = compat.safe_str(value or '').strip().strip('"').strip("'").strip()
    if not url:
        return default
    scheme, rest = 'http', url
    if '://' in url:
        head, tail = url.split('://', 1)
        if head.lower() in ('http', 'https'):
            scheme, rest = head.lower(), tail
    rest = rest.rstrip('/').strip()
    if not rest:
        return default
    return '%s://%s' % (scheme, rest)


def split_base_url(url):
    parsed = compat.urlparse(url)
    scheme = parsed.scheme or 'http'
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or (443 if scheme == 'https' else 80)
    return scheme, host, port, (parsed.path or '')


def release_full(subpath, query=''):
    url = release_base_candidates()[0]
    scheme, host, port, base_path = split_base_url(url)
    path = base_path.rstrip('/') + '/api/' + subpath
    if query:
        path += '?' + query
    return '%s://%s:%d%s' % (scheme, host, port, path)


_LAN_IPS = {'ts': 0.0, 'ips': []}


def local_lan_ips():
    """本机局域网 IPv4 列表（60 秒缓存）。

    用于「release_url 配置成 127.0.0.1 但 release_center 绑定在具体网卡 /
    与平台不同机」时的自动回退探测。UDP connect 不真正发包，只让内核选路由源地址。
    """
    now = time.time()
    if now - _LAN_IPS['ts'] > 60:
        ips = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('10.255.255.255', 1))
                ips.append(s.getsockname()[0])
            finally:
                s.close()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip not in ips:
                    ips.append(ip)
        except Exception:                                     # noqa: BLE001
            pass
        _LAN_IPS['ips'] = [ip for ip in ips if ip not in ('127.0.0.1', '0.0.0.0')]
        _LAN_IPS['ts'] = now
    return list(_LAN_IPS['ips'])


def release_base_candidates():
    """发布中心候选地址：配置地址优先；指向回环时追加本机各网卡 IP（同端口同路径）。"""
    if ARGS.release_url:
        url = normalize_base_url(ARGS.release_url)
    else:
        url = normalize_base_url(current_settings().get('release_url'))
    cands = [url]
    scheme, host, port, path = split_base_url(url)
    if host in ('127.0.0.1', 'localhost', '::1'):
        for ip in local_lan_ips():
            cands.append('%s://%s:%d%s' % (scheme, ip, port, path))
    return cands


_RC_BASE_OK = {'url': '', 'ts': 0.0}


def req_query(req):
    """取原始查询串（转发类接口必须用它，不要用 Request.parsed）。

    Request.parsed = urlparse(handler.path[:qpos]) —— 只含路径。dispatch 把
    query 剥掉是为了路由匹配，所以 parsed 里的查询串恒为空；真正的查询串在
    req.raw_query。用错的表现是参数被静默丢弃：发布中心的 ?app= 过滤、服务器
    转发到目标机的分页 ?page= 在那一端「不生效」，且不报任何错。
    """
    return compat.to_text(compat.safe_str(getattr(req, 'raw_query', '') or ''))


def _rc_pick_base():
    """返回当前可用的发布中心 base（60 秒内复用上次探测成功的地址）。"""
    now = time.time()
    cands = release_base_candidates()
    if _RC_BASE_OK['url'] and now - _RC_BASE_OK['ts'] < 60 and _RC_BASE_OK['url'] in cands:
        return _RC_BASE_OK['url']
    for url in cands:
        try:
            r = httpx.DEFAULT.get(url.rstrip('/') + '/api/health', timeout=3)
            if r.status == 200:
                _RC_BASE_OK['url'] = url
                _RC_BASE_OK['ts'] = now
                return url
        except Exception:                                     # noqa: BLE001
            pass
    return cands[0]


# ================= 发布中心鉴权（令牌 / 账号口令自动登录） =================
def rc_static_token():
    """显式配置的发布中心令牌（--release-token 优先于设置页）"""
    return compat.safe_str(ARGS.release_token
                           or current_settings().get('release_token') or '').strip()


def rc_credentials():
    """设置页里的发布中心账号 / 口令；留空表示不自动登录"""
    st = current_settings()
    return (compat.safe_str(st.get('release_user') or '').strip(),
            compat.safe_str(st.get('release_pass') or ''))


def rc_session_get(base):
    return compat.safe_str(((RC_SESSION.load().get('items') or {}).get(base) or {}).get('token', ''))


def rc_session_put(base, token):
    def _mut(d):
        items = d.setdefault('items', {})
        if token:
            items[base] = {'token': token, 'ts': time.time()}
        else:
            items.pop(base, None)
        return d
    RC_SESSION.update(_mut)


def rc_login(base):
    """用设置页填写的账号/口令登录发布中心换取会话令牌（仅在未配置令牌时走这条路）"""
    user, pwd = rc_credentials()
    if not (user and pwd):
        return ''
    try:
        r = httpx.DEFAULT.post(base.rstrip('/') + '/api/login',
                               json_body={'username': user, 'password': pwd}, timeout=30)
    except Exception:                                         # noqa: BLE001
        return ''
    token = ''
    if r.status == 200:
        token = compat.safe_str((r.json({}) or {}).get('token', ''))
    rc_session_put(base, token)
    return token


def rc_token(base, force=False):
    """返回发给发布中心的 X-Auth-Token；无令牌则退化为「账号口令自动登录」"""
    static = rc_static_token()
    if static:
        return static
    if force:
        rc_session_put(base, '')
    return rc_session_get(base) or rc_login(base)


def rc_headers(base, force=False):
    token = rc_token(base, force=force)
    return {'X-Auth-Token': token} if token else {}


def rc_auth_mode():
    if rc_static_token():
        return 'token'
    user, pwd = rc_credentials()
    if user and pwd:
        return 'login'
    return 'none'


def rc_unauth_message():
    """上游 401 时的可操作提示（全 unicode 再归一，避开 py2 的 str+unicode 陷阱）"""
    user, pwd = rc_credentials()
    if user and pwd:
        hint = (u'设置页填写的账号「%s」登录被拒，请确认口令是否正确、该账号是否被停用。'
                % compat.to_text(user))
    elif user or pwd:
        hint = u'设置页的发布中心账号与口令需要成对填写。'
    else:
        hint = (u'尚未配置访问发布中心的凭据。到「设置」填写发布中心账号与口令'
                u'（管理员账号，平台会自动登录，无需重启服务）；'
                u'或给 release_center 启动参数加 --token X，并把 X 填到「发布中心令牌」。')
    return compat.to_native(u'发布中心拒绝访问（401）：凭据缺失或已失效。%s' % hint)


def release_proxy(req, subpath):
    base = _rc_pick_base()
    url = base.rstrip('/') + '/api/' + subpath
    if req_query(req):
        url += '?' + req_query(req)
    method = req.method
    body = req.raw_body() if method in ('POST', 'PUT') else None
    timeout = 60 if body is not None else 15
    resp = httpx.DEFAULT.request(method, url, body=body,
                                 headers=rc_headers(base), timeout=timeout)
    # 上游 401/403 属于「平台→发布中心」的凭据问题，绝不能原样透传：
    # 前端把任意 401 都当作「平台会话失效」并立刻踢回登录页，
    # 表现为「登录成功 → 发布视图一加载就退出 → 看起来像登不进去」。
    if resp.status in (401, 403) and not rc_static_token():
        again = httpx.DEFAULT.request(method, url, body=body,
                                      headers=rc_headers(base, force=True), timeout=timeout)
        if again.status not in (401, 403):
            resp = again
    if resp.status in (401, 403):
        return req.json_out({'status': 'error', 'code': 'upstream_unauthorized',
                             'message': rc_unauth_message()}, 502)
    if resp.status == 0:
        hint = u''
        if split_base_url(base)[1] in ('127.0.0.1', 'localhost', '::1'):
            hint = (u'；本机网卡地址也已尝试。请确认 release_center 与平台在同一台机器，'
                    u'或到「设置」把发布中心地址改为其真实地址（如 http://172.19.196.130:8094）')
        # py2 陷阱：含中文的字节串字面量与 unicode 拼接（str + unicode）会触发
        # 隐式 ascii 解码 → UnicodeDecodeError（502 变 500）。全程走 unicode 再归一。
        msg = compat.to_native(u'release_center 不可达: %s%s'
                               % (compat.to_text(resp.error), hint))
        return req.json_out({'status': 'error', 'code': 'upstream',
                             'message': msg}, 502)
    ctype = resp.header('Content-Type') or 'application/json; charset=utf-8'
    req.respond(resp.status, resp.body, ctype, {})
    return None


# 制品包上传的透传超时（秒）。整包直传，不能按普通接口的 60 秒算。
RELEASE_UPLOAD_TIMEOUT = 3600


def release_upload_proxy(req, subpath):
    """制品包上传专用透传：把请求体**流式**转发给发布中心。

    为什么不直接复用 release_proxy：
      1) `req.raw_body()` 默认 32MB 上限 —— 稍大的包直接 413，而制品包动辄几百 MB；
         且整包读进内存会在并发上传时把平台打爆。
      2) `httpx` 只支持整块 body，没法边收边转。
    这里手工建连，一边读客户端一边写上游（每片 64KB），内存占用与包大小无关。
    上游 401/403 仍然翻成 502 `upstream_unauthorized`，与 release_proxy 保持一致，
    避免前端把上游 401 误判成「平台会话失效」而踢回登录页。
    """
    base = _rc_pick_base()
    url = base.rstrip('/') + '/api/' + subpath
    if req_query(req):
        url += '?' + req_query(req)
    parsed = compat.urlparse(url)
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    try:
        length = int(req.handler.headers.get('Content-Length') or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        raise ApiError('请求体为空（缺少 Content-Length）', 400)
    # 先取凭据：rc_token 在没有会话时会自动用设置页的账号口令登录，
    # 保证「刚开始上传就 401」这种情况不出现（body 只能消费一次，没法重试）。
    headers = dict(rc_headers(base))
    headers['Content-Type'] = req.handler.headers.get('Content-Type') or 'application/octet-stream'
    conn = None
    status, body, ctype = 0, b'', ''
    try:
        if parsed.scheme == 'https':
            ctx = None
            if hasattr(ssl, 'create_default_context'):
                ctx = ssl.create_default_context()
            conn = compat.http_client.HTTPSConnection(host, port,
                                                      timeout=RELEASE_UPLOAD_TIMEOUT, context=ctx)
        else:
            conn = compat.http_client.HTTPConnection(host, port, timeout=RELEASE_UPLOAD_TIMEOUT)
        conn.putrequest('POST', path)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.putheader('Content-Length', str(length))
        conn.endheaders()
        sent = req.stream_body(lambda buf: conn.send(buf), max_mb=8192)
        if sent < length:
            raise ApiError('上传中断：只收到 %d / %d 字节，请重试' % (sent, length), 400)
        resp = conn.getresponse()
        status = resp.status
        ctype = resp.getheader('Content-Type') or 'application/json; charset=utf-8'
        body = resp.read()
    except ApiError:
        raise
    except Exception as e:                                    # noqa: BLE001
        # 含中文的字节串字面量 + unicode 拼接在 py2 下会隐式 ascii 解码炸成 500，
        # 全程走 unicode 再归一（与 release_proxy 同一坑）。
        msg = compat.to_native(u'release_center 不可达: %s' % compat.to_text(e))
        return req.json_out({'status': 'error', 'code': 'upstream', 'message': msg}, 502)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:                                     # noqa: BLE001
            pass
    if status in (401, 403):
        return req.json_out({'status': 'error', 'code': 'upstream_unauthorized',
                             'message': rc_unauth_message()}, 502)
    req.respond(status, body, ctype, {})
    return None


def release_config(req):
    base = _rc_pick_base()
    r = httpx.DEFAULT.get(base.rstrip('/') + '/api/health', timeout=4)
    online = r.status == 200
    version = ''
    authorized = False
    if online:
        version = compat.safe_str((r.json({}) or {}).get('version', ''))
        probe_apps = httpx.DEFAULT.get(base.rstrip('/') + '/api/apps',
                                       headers=rc_headers(base), timeout=8)
        authorized = probe_apps.status == 200
    return ok({'url': base, 'online': online, 'version': version,
               'authorized': authorized, 'auth_mode': rc_auth_mode()})


# ================= SSE 透传（发布进度 / 命令输出） =================
def raw_sse_proxy(url, method, headers, body, stream, default_event='message'):
    """打开到上游的 SSE 连接，逐行读取并原样转发事件。

    返回三态（调用方据此决定能否降级重试）：
      'ok'      —— 正常建立并转发完成 / 上游 EOF 正常收尾
      'nogood'  —— 在向客户端写出任何事件之前就失败，降级重试是安全的
      'partial' —— 已向客户端写过事件后才失败，**禁止**再降级
                   （否则同一条命令会被执行两次，重启/迁移类步骤会出事故）
    """
    parsed = compat.urlparse(url)
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    conn = compat.http_client.HTTPConnection(host, port, timeout=120)
    wrote = False
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in headers.items():
            if k.lower() in ('host', 'content-length', 'connection', 'transfer-encoding'):
                continue
            conn.putheader(k, v)
        if body is not None:
            # body 必须是 bytes：http.client 在 py3 下 send(str) 会直接抛错，
            # py2 下 unicode/含非 ASCII 的命令也会隐式编码失败——那会让 POST 通道
            # 实际不可用、上游最终退化回 `GET ...?cmd=` 的 webshell 特征形态。
            payload = body if isinstance(body, bytes) else compat.to_bytes(body)
            conn.putheader('Content-Length', str(len(payload)))
            conn.endheaders()
            conn.send(payload)
        else:
            conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            # 明确的「未执行」（404/405/403 等）→ 可安全降级
            return 'nogood'
        cur_event = default_event
        while not stream.closed:
            raw = resp.fp.readline()
            if not raw:
                # 上游 EOF：本次流正常结束。必须 break —— 否则会在
                # `sleep(0.05); continue` 里以 20Hz 永久空转，泄漏线程与连接。
                break
            line = compat.to_text(raw).rstrip('\r').rstrip('\n')
            if line.startswith('event:'):
                cur_event = line[6:].strip() or default_event
            elif line.startswith('data:'):
                stream.send(line[5:].strip(), event=cur_event)
                wrote = True
                cur_event = default_event
            elif line.startswith(':'):
                pass
        return 'ok'
    except Exception:
        return 'partial' if wrote else 'nogood'
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ================= CSV 导出 =================
def csv_response(req, rows, filename):
    import io
    import csv
    if compat.PY2:
        # py2 的 _io.StringIO.write 只接受 unicode，而 csv.writer 写的是字节串
        # → 用 BytesIO 承接（py2 的 csv 本就产出字节）。
        buf = io.BytesIO()
        w = csv.writer(buf)
        for row in rows:
            w.writerow([compat.safe_str(c) for c in row])
        data = buf.getvalue()
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        for row in rows:
            w.writerow([compat.safe_str(c) for c in row])
        data = compat.to_bytes(buf.getvalue())
    body = b'\xef\xbb\xbf' + data
    req.respond(200, body, 'text/csv; charset=utf-8',
                {'Content-Disposition': 'attachment; filename=%s' % filename})
    return None


# ================= 首页渲染（替换占位符） =================
def need_login_value():
    return 'false' if ARGS.no_login else 'true'


def render_home():
    text = server.read_text_file(os.path.join(STATIC_DIR, 'index.html'))
    if text is None:
        return (u'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                u'<title>运维工具集成平台</title></head><body style="font-family:sans-serif">'
                u'<h3>未找到页面模板</h3><p>期望路径：<code>%s/index.html</code></p>'
                u'</body></html>' % compat.to_text(STATIC_DIR))
    page = text.replace('{{version}}', VERSION).replace('{{need_login}}', need_login_value())
    page = page.replace('{{username}}', '')
    return page


# ================= 路由：首页与基础接口 =================
@app.get('/', auth=False, summary='首页（SPA）')
def page_index(req):
    return render_home()


@app.get('/login', auth=False, summary='登录页（SPA）')
def page_login(req):
    return render_home()


@app.get('/api/ping', auth=False, summary='存活探测')
def api_ping(req):
    return ok({'version': VERSION})


@app.get('/api/health', auth=False, summary='存活+运行时信息')
def api_health(req):
    return ok({'version': VERSION, 'uptime': round(time.time() - app.started_at, 1),
              'pid': os.getpid(), 'servers': len(load_servers()), 'data_dir': DATA_DIR})


@app.get('/api/openapi.json', auth=False, summary='OpenAPI 描述')
def api_openapi(req):
    return app.openapi()


# ================= 路由：会话与身份 =================
@app.post('/api/login', auth=False, summary='登录')
def api_login(req):
    data = req.json()
    username = compat.safe_str(data.get('username', ''))
    password = data.get('password', '')
    res = AUTH.login(username, password, req.client_ip)
    if not res.get('ok'):
        AUDIT.write(req.user or '-', req.client_ip, 'auth.fail', target=username,
                    detail=res.get('message', ''), result='fail')
        raise ApiError(res.get('message', '登录失败'), 401, 'unauthorized')
    AUDIT.write_req(req, 'auth.login', target=username)
    return ok({'token': res['token'], 'username': res['username'], 'role': res['role'],
              'servers': res['servers'], 'perms': role_perms_for_client(res['role']),
              'expires': res.get('expires')})


@app.post('/api/logout', auth=True, summary='注销')
def api_logout(req):
    token = req.headers.get('X-Auth-Token') or req.q('token', '')
    AUTH.logout(token)
    return ok({'message': '已注销'})


@app.get('/api/me', auth=False, summary='当前会话信息')
def api_me(req):
    session = AUTH.resolve(req.headers, req.query)
    if not session:
        return ok({'need_login': True, 'version': VERSION})
    return ok({'username': session.get('username'), 'role': session.get('role'),
              'role_label': opsauth.ROLES.get(session.get('role'), {}).get('label', session.get('role')),
              'servers': session.get('servers') or ['*'],
              'perms': role_perms_for_client(session.get('role')),
              'need_login': False, 'version': VERSION})


@app.get('/api/roles', auth=True, summary='角色与权限点清单')
def api_roles(req):
    roles = {}
    for name, cfg in opsauth.ROLES.items():
        roles[name] = {'label': cfg.get('label'), 'desc': cfg.get('desc'),
                       'perms': cfg.get('perms')}
    return ok({'roles': roles, 'perms': list(opsauth.PERMS)})


@app.post('/api/password', auth=True, summary='修改自己的口令')
def api_password(req):
    data = req.json()
    old = data.get('old_password', '')
    new = data.get('new_password', '')
    user = AUTH.user_get(req.user)
    if not user or not opsauth.verify_password(old, user.get('password')):
        raise ApiError('原口令错误', 400, 'bad_request')
    ok_u, msg = AUTH.user_update(req.user, {'password': new})
    if not ok_u:
        raise ApiError(msg, 400, 'bad_request')
    AUDIT.write_req(req, 'user.update', target=req.user, detail='修改口令')
    return ok({'message': '口令已修改'})


# ================= 路由：用户管理 =================
@app.get('/api/users', perm='user.admin', summary='用户列表')
def api_users(req):
    return ok({'users': AUTH.public_users()})


@app.post('/api/users', perm='user.admin', summary='新增用户')
def api_users_add(req):
    data = req.json()
    username = compat.safe_str(data.get('username', ''))
    password = data.get('password', '')
    role = data.get('role', 'viewer')
    servers = data.get('servers') or ['*']
    note = data.get('note', '')
    ok_u, msg = AUTH.user_add(username, password, role, servers=servers, note=note)
    if not ok_u:
        raise ApiError(msg, 400, 'bad_request')
    AUDIT.write_req(req, 'user.add', target=username, detail='角色=%s' % role)
    return ok({'message': msg})


@app.put('/api/users/<name>', perm='user.admin', summary='修改用户')
def api_users_put(req):
    name = compat.urlunquote(req.params['name'])
    data = req.json()
    ok_u, msg = AUTH.user_update(name, data)
    if not ok_u:
        raise ApiError(msg, 400, 'bad_request')
    AUDIT.write_req(req, 'user.update', target=name)
    return ok({'message': msg})


@app.delete('/api/users/<name>', perm='user.admin', summary='删除用户')
def api_users_del(req):
    name = compat.urlunquote(req.params['name'])
    ok_u, msg = AUTH.user_delete(name)
    if not ok_u:
        raise ApiError(msg, 400, 'bad_request')
    AUDIT.write_req(req, 'user.delete', target=name)
    return ok({'message': msg})


@app.post('/api/users/<name>/reset-password', perm='user.admin', summary='重置用户口令')
def api_users_reset(req):
    name = compat.urlunquote(req.params['name'])
    ok_u, msg, new_pwd = AUTH.user_reset_password(name)
    if not ok_u:
        raise ApiError(msg, 400, 'bad_request')
    AUDIT.write_req(req, 'user.update', target=name, detail='重置口令')
    return ok({'message': msg, 'new_password': new_pwd})


# ================= 路由：服务器与标签 =================
@app.get('/api/servers', perm='server.view', summary='服务器列表')
def api_servers(req):
    servers = AUTH.filter_servers(req.session, load_servers())
    group = req.q('group')
    env = req.q('env')
    tag = req.q('tag')
    kw = req.q('kw').lower()
    if group:
        servers = [s for s in servers if s.get('group') == group]
    if env:
        servers = [s for s in servers if s.get('env') == env]
    if tag:
        servers = [s for s in servers if tag in (s.get('tags') or [])]
    if kw:
        servers = [s for s in servers
                   if kw in compat.safe_str(s.get('name', '')).lower()
                   or kw in compat.safe_str(s.get('host', '')).lower()]
    probe_on = req.q('probe', '1') not in ('0', 'false', 'no')
    out = [redact_server(req, dict(s)) for s in servers]
    if probe_on:
        # 并发 + TTL 缓存：离线机不再让每次列表都卡 N×2~6s（否则页面「保存后刷新」
        # 迟迟不返回，被当成「保存没生效」）。要强制刷新用 batch-probe。
        statuses = probe_many(servers)
        empty = {'upload': None, 'shell': None, 'metrics': None, 'online': False}
        for item in out:
            item['status'] = statuses.get(item.get('name')) or dict(empty)
    return ok({'servers': out})


@app.post('/api/servers', perm='server.edit', summary='新增服务器')
def api_servers_add(req):
    data = req.json()
    name = compat.safe_str(data.get('name', '')).strip()
    if not name:
        raise ApiError('名称不能为空', 400, 'bad_request')
    host = compat.safe_str(data.get('host', '')).strip()
    if not host:
        raise ApiError('主机IP不能为空', 400, 'bad_request')
    if find_server(name):
        raise ApiError('名称已存在', 409, 'conflict')
    srv = normalize_server(data)
    srv['name'] = name
    srv['host'] = host

    def _mut(d):
        d.setdefault('servers', []).append(srv)
        return d

    CONFIG.update(_mut)
    invalidate_probe(name)
    AUDIT.write_req(req, 'server.add', target=name, detail='host=%s' % host)
    return ok({'message': '已添加 %s' % name, 'server': srv})


@app.get('/api/meta', perm='server.view', summary='分组/环境/标签/类型聚合')
def api_meta(req):
    servers = AUTH.filter_servers(req.session, load_servers())
    groups = set()
    envs = set()
    tags = set()
    types = set()
    for s in servers:
        if s.get('group'):
            groups.add(s['group'])
        if s.get('env'):
            envs.add(s['env'])
        for t in (s.get('tags') or []):
            tags.add(t)
        types.add(s.get('type', 'both'))
    return ok({'groups': sorted(groups), 'envs': sorted(envs), 'tags': sorted(tags),
              'types': sorted(types)})


@app.post('/api/servers/batch-update', perm='server.edit', summary='按选择批量改属性')
def api_servers_batch_update(req):
    data = req.json()
    selector = data.get('selector') or {}
    sett = data.get('set') or {}
    targets = select_servers(selector)
    names = []
    for s in targets:
        name = s['name']

        def _mut(d):
            for srv in d.get('servers', []):
                if srv.get('name') != name:
                    continue
                if 'group' in sett:
                    srv['group'] = compat.safe_str(sett['group'])
                if 'env' in sett:
                    srv['env'] = sett['env']
                if 'tags_add' in sett:
                    cur = set(srv.get('tags') or [])
                    cur.update(sett['tags_add'])
                    srv['tags'] = list(cur)
                if 'tags_remove' in sett:
                    cur = set(srv.get('tags') or [])
                    cur.difference_update(sett['tags_remove'])
                    srv['tags'] = list(cur)
            return d

        CONFIG.update(_mut)
        names.append(name)
    for n in names:
        invalidate_probe(n)
    AUDIT.write_req(req, 'server.update', target=','.join(names), detail='批量更新')
    return ok({'updated': names, 'count': len(names)})


@app.get('/api/servers/export', perm='server.view', summary='导出服务器配置（json 含令牌 / csv 明文表）')
def api_servers_export(req):
    """批量导出：json 用于备份/迁移（含 upload_token / shell_token），csv 用于查看。

    权限分级：csv 不含任何凭据，server.view 即可；json 带目标机令牌（拿到即可
    绕过平台直连目标机服务），必须显式持有 server.edit —— 与 redact_server 的
    「无 server.edit 不下发凭据」同一防线，避免只读账号借导出功能提权。
    """
    fmt = req.q('format', 'json')
    servers = AUTH.filter_servers(req.session, load_servers())
    if fmt == 'csv':
        rows = [['名称', '主机', '类型', '上传端口', '终端端口', '分组', '环境',
                 '标签', '访问模式', '上传目录', '备注']]
        for s in servers:
            rows.append([s.get('name'), s.get('host'), s.get('type', 'both'),
                         s.get('upload_port'), s.get('shell_port'),
                         s.get('group'), s.get('env'),
                         ','.join(s.get('tags') or []),
                         s.get('access_mode', 'auto'),
                         s.get('upload_dir', ''), s.get('note', '')])
        return csv_response(req, rows, 'servers.csv')
    if fmt != 'json':
        raise ApiError('仅支持 format=json / csv', 400, 'bad_request')
    if app.auth is not None and not app.auth.has_perm(req.session, 'server.edit'):
        raise ApiError('导出 JSON 含目标机令牌，需要「服务器编辑」权限；只读账号请用 format=csv',
                       403, 'forbidden')
    body = compat.json_bytes({'version': VERSION,
                              'exported_at': int(time.time()),
                              'servers': servers})
    req.respond(200, body, 'application/json; charset=utf-8',
                {'Content-Disposition': 'attachment; filename=servers.json'})
    return None


@app.post('/api/servers/import', perm='server.edit', summary='批量导入服务器配置')
def api_servers_import(req):
    """批量导入：请求体 {"servers":[...], "mode":"merge|skip"}，也接受裸数组。

    - merge（默认）：同名覆盖更新，新名称追加；
    - skip：同名跳过，只补新增。
    每条都过 normalize_server 归一（端口/类型/模式非法值回落默认），名称或主机
    缺失的条目记入 errors 并继续处理其余条目，不整体失败；单次上限 500 条。
    """
    data = req.json()
    if isinstance(data, dict):
        items = data.get('servers')
        mode = compat.safe_str(data.get('mode') or 'merge')
    else:
        items = data
        mode = 'merge'
    if not isinstance(items, list):
        raise ApiError('请求体应为 {"servers": [...]} 或服务器数组', 400, 'bad_request')
    if len(items) > 500:
        raise ApiError('单次最多导入 500 台，请分批导入', 400, 'bad_request')
    if mode not in ('merge', 'skip'):
        mode = 'merge'

    existing = dict((compat.safe_str(s.get('name')), s) for s in load_servers())
    imported = []
    added, updated, skipped = [], [], []
    errors = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append({'index': idx, 'name': '', 'error': '条目不是对象'})
            continue
        name = compat.safe_str(raw.get('name', '')).strip()
        host = compat.safe_str(raw.get('host', '')).strip()
        if not name or not host:
            errors.append({'index': idx, 'name': name,
                           'error': '名称或主机为空' if not name else '主机为空'})
            continue
        try:
            srv = normalize_server(raw)
        except Exception as e:                                     # noqa: BLE001
            errors.append({'index': idx, 'name': name, 'error': compat.safe_str(e)})
            continue
        srv['name'] = name
        srv['host'] = host
        if name in existing:
            if mode == 'skip':
                skipped.append(name)
                continue
            updated.append(name)
        else:
            added.append(name)
        imported.append(srv)
        existing[name] = srv

    if imported:
        def _mut(d):
            cur = dict((compat.safe_str(s.get('name')), s) for s in d.get('servers', []))
            for srv in imported:
                cur[compat.safe_str(srv['name'])] = srv
            out = []
            seen = set()
            for s in d.get('servers', []):
                n = compat.safe_str(s.get('name'))
                if n in cur and n not in seen:
                    out.append(cur[n])
                    seen.add(n)
            for n, s in cur.items():
                if n not in seen:
                    out.append(s)
            d['servers'] = out
            return d

        CONFIG.update(_mut)
    for n in added + updated:
        invalidate_probe(n)
    AUDIT.write_req(req, 'server.import',
                    target=','.join(added + updated)[:200] or '-',
                    detail='导入：新增 %d 更新 %d 跳过 %d 失败 %d'
                           % (len(added), len(updated), len(skipped), len(errors)))
    return ok({'added': len(added), 'updated': len(updated),
               'skipped': len(skipped), 'error_total': len(errors),
               'errors': errors[:20]})


@app.post('/api/servers/batch-probe', perm='server.view', summary='批量探测在线状态')
def api_servers_batch_probe(req):
    data = maybe_json(req)
    targets = select_servers((data or {}).get('selector') or {})
    # 「用户主动刷新」入口：ttl=0 强制真探测（并发），同时回填缓存给列表/详情复用
    results = probe_many(targets, ttl=0)
    return ok({'results': results})


def bulk_exec_job(ctx, selector=None, cmd='', timeout=30):
    servers = select_servers(selector)
    total = len(servers)
    done = 0
    for s in servers:
        ctx.check_canceled()
        ctx.log('=== %s ===' % s['name'])
        if not has_shell(s):
            ctx.log('未启用命令执行，跳过', 'warn')
        else:
            ok_e, out = shell_exec(s, cmd, timeout)
            ctx.log(out)
        done += 1
        ctx.progress(int(100.0 * done / total) if total else 100, '已执行 %d/%d' % (done, total))
    ctx.set(total=total)
    return {'total': total}


@app.post('/api/servers/bulk-exec', perm='cmd.exec', summary='按标签批量执行命令')
def api_servers_bulk_exec(req):
    data = req.json()
    selector = data.get('selector') or {}
    cmd = compat.safe_str(data.get('cmd', '')).strip()
    if not cmd:
        raise ApiError('命令不能为空', 400, 'bad_request')
    timeout = clamp_timeout(data.get('timeout'))
    t = TM.submit('批量执行', 'bulk_exec', bulk_exec_job,
                  params={'selector': selector, 'cmd': cmd, 'timeout': timeout},
                  user=req.user)
    AUDIT.write_req(req, 'cmd.exec', target='批量', detail=cmd)
    return ok({'task_id': t['id']})


@app.post('/api/servers/bulk-delete-files', perm='file.delete', summary='按标签批量删文件')
def api_servers_bulk_delete_files(req):
    data = req.json()
    targets = data.get('targets') or []
    deleted = 0
    for t in targets:
        name = compat.safe_str(t.get('name', ''))
        path = compat.safe_str(t.get('path', ''))
        srv = find_server(name)
        # 批量删按目标逐台过滤：无权访问的直接跳过（而不是让整批 403）
        if not srv or not path or not has_upload(srv):
            continue
        if app.auth is not None and not app.auth.can_access_server(req.session, name):
            continue
        code, _, _ = relay_request(srv, 'upload', 'DELETE',
                                   '/api/file?path=' + compat.urlquote(path),
                                   headers=upload_auth_headers(srv), timeout=30)
        if code == 200:
            deleted += 1
            AUDIT.write_req(req, 'file.delete', target=name, detail=path)
    return ok({'deleted': deleted, 'total': len(targets)})


@app.get('/api/servers/<name>', perm='server.view', summary='单台详情')
def api_server_get(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    item = redact_server(req, dict(srv))
    item['status'] = probe_cached(srv)      # 缓存：编辑页打开/保存后刷新秒回
    return ok({'server': item})


@app.put('/api/servers/<name>', perm='server.edit', summary='修改服务器')
def api_server_put(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    data = req.json()
    new_name = compat.safe_str(data.get('name', '')).strip()
    if new_name and new_name != name:
        if find_server(new_name):
            raise ApiError('名称已存在', 409, 'conflict')
        name = new_name
    data.pop('name', None)
    updated = normalize_server(data, defaults=srv)
    updated['name'] = name

    def _mut(d):
        d['servers'] = [updated if s.get('name') == srv['name'] else s for s in d.get('servers', [])]
        return d

    CONFIG.update(_mut)
    # 改了地址/端口后清掉旧探测结果，避免列表继续显示过时状态
    invalidate_probe(srv['name'])
    invalidate_probe(name)
    AUDIT.write_req(req, 'server.update', target=name)
    return ok({'message': '已更新 %s' % name, 'server': updated})


@app.delete('/api/servers/<name>', perm='server.edit', summary='删除服务器')
def api_server_del(req):
    name = compat.urlunquote(req.params['name'])
    before = len(load_servers())

    def _mut(d):
        d['servers'] = [s for s in d.get('servers', []) if s.get('name') != name]
        return d

    CONFIG.update(_mut)
    if len(load_servers()) == before:
        raise ApiError('服务器不存在', 404, 'not_found')
    invalidate_probe(name)
    AUDIT.write_req(req, 'server.delete', target=name)
    return ok({'message': '已删除 %s' % name})


@app.get('/api/servers/<name>/status', perm='server.view', summary='单台状态')
def api_server_status(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    return ok({'status': probe_cached(srv)})


@app.get('/api/servers/<name>/files', perm='file.read', summary='远端文件列表（分页/筛选/排序）')
def api_server_files(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    if not has_upload(srv):
        raise ApiError('该服务器未启用文件上传', 400, 'bad_request')
    params = []
    for k in ('path', 'q', 'sort', 'order', 'page', 'size'):
        v = req.q(k)
        if v:
            params.append('%s=%s' % (k, compat.urlquote(v)))
    query = '&'.join(params)
    url = httpx.join_url(srv['host'], srv.get('upload_port', 8089), '/api/list')
    if query:
        url += '?' + query
    resp = httpx.DEFAULT.get(url, headers=upload_auth_headers(srv), timeout=15)
    if resp.status != 200:
        return req.json_out({'status': 'error', 'code': 'upstream',
                            'message': '远端不可达 (HTTP %d)' % resp.status}, 502)
    data = resp.json({}) or {}
    files = data.get('files')
    if files is None:
        files = []
    out = {'files': files,
           'total': data.get('total', len(files)),
           'page': data.get('page', 1),
           'size': data.get('size', len(files)),
           'pages': data.get('pages', 1),
           'dirs': data.get('dirs', [])}
    return ok(out)


@app.delete('/api/servers/<name>/files', perm='file.delete', summary='删除远端文件')
def api_server_file_del(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    path = req.q('path', '')
    if not path:
        raise ApiError('缺少 path 参数', 400, 'bad_request')
    code, _, raw = relay_request(srv, 'upload', 'DELETE',
                                 '/api/file?path=' + compat.urlquote(path),
                                 headers=upload_auth_headers(srv), timeout=30)
    if code == 0:
        return req.json_out({'status': 'error', 'code': 'upstream', 'message': '转发失败'}, 502)
    ctype = raw and 'application/json' or 'application/json'
    try:
        body = compat.json_loads(raw, {'status': 'success' if code == 200 else 'error'})
    except Exception:
        body = {'status': 'success' if code == 200 else 'error'}
    AUDIT.write_req(req, 'file.delete', target=name, detail=path)
    return req.json_out(body, code if code else 502)


@app.get('/api/servers/<name>/download', perm='file.read', summary='代理下载单文件')
def api_server_download(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    rel = req.q('path', '')
    if not rel:
        raise ApiError('缺少 path 参数', 400, 'bad_request')
    url = httpx.join_url(srv['host'], srv.get('upload_port', 8089), '/api/download')
    url += '?path=' + compat.urlquote(rel)
    hdrs = upload_auth_headers(srv)
    rng = req.headers.get('Range')
    if rng:
        hdrs['Range'] = rng
    code, rh, body_iter = relay_stream(srv, 'upload', 'GET',
                                       '/api/download?path=' + compat.urlquote(rel),
                                       headers=hdrs, timeout=3600)
    if code == 0:
        return req.json_out({'status': 'error', 'code': 'upstream', 'message': '下载失败'}, 502)
    ctype = relay_header(rh, 'Content-Type', 'application/octet-stream')
    if code >= 400 or 'json' in compat.to_text(ctype):
        data = b''
        try:
            for chunk in body_iter:
                data += chunk
        except Exception:                                     # noqa: BLE001
            pass
        return req.respond(code, data, ctype)
    extra = {}
    cdisp = relay_header(rh, 'Content-Disposition')
    if cdisp:
        extra['Content-Disposition'] = cdisp
    for h in ('Content-Range', 'Accept-Ranges'):
        v = relay_header(rh, h)
        if v:
            extra[h] = v
    # 流式转发：大文件不在管理机全量缓冲（避免 py2 内存打爆后连接中断）
    req.stream(body_iter, content_type=ctype, status=code, extra_headers=extra, chunked=True)
    return None


@app.get('/api/servers/<name>/batch-download', perm='file.read', summary='代理打包下载')
def api_server_batch_download(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    paths = req.query_list.get('paths') or []
    # 去重保序：前端勾选重复行时（截图案例同一文件出现两次）不再生成重复 zip 条目
    seen = set()
    uniq = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    if not uniq:
        raise ApiError('缺少 paths 参数', 400, 'bad_request')
    body = compat.json_bytes({'paths': uniq})
    hdrs = {'Content-Type': 'application/json; charset=utf-8'}
    code, rh, body_iter = relay_stream(srv, 'upload', 'POST', '/api/batch/download',
                                       headers=hdrs, body=body, timeout=3600)
    if code == 0:
        return req.json_out({'status': 'error', 'code': 'upstream', 'message': '打包失败'}, 502)
    ctype = relay_header(rh, 'Content-Type', 'application/zip')
    if code >= 400 or 'json' in compat.to_text(ctype):
        # 上游报错（如没有可下载的文件）：小体量，读全量后原样透传 JSON
        data = b''
        try:
            for chunk in body_iter:
                data += chunk
        except Exception:                                     # noqa: BLE001
            pass
        return req.respond(code, data, ctype)
    extra = {}
    cdisp = relay_header(rh, 'Content-Disposition')
    if cdisp:
        extra['Content-Disposition'] = cdisp
    # 流式管道：上游 zip 边收边发给浏览器，管理机不落盘、不全量缓冲
    req.stream(body_iter, content_type=ctype, extra_headers=extra, chunked=True)
    return None


@app.get('/api/servers/<name>/metrics', perm='server.view', summary='单台实时指标')
def api_server_metrics(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    return ok({'metrics': collect_metrics(srv)})


@app.get('/api/servers/<name>/metrics-stream', perm='server.view', summary='监控曲线推送')
def api_server_metrics_stream(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    interval = req.qi('interval', 3, 1, 300)
    stream = req.sse()
    stream.ready()
    try:
        waited = 0
        while not stream.closed:
            if waited >= interval:
                stream.json(collect_metrics(srv), event='metrics')
                waited = 0
            time.sleep(0.5)
            waited += 0.5
    finally:
        stream.close()
    return None


@app.get('/api/servers/<name>/sysinfo', perm='server.view', summary='资产画像')
def api_server_sysinfo(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    return ok({'sysinfo': collect_sysinfo(srv)})


@app.post('/api/servers/<name>/exec', perm='cmd.exec', summary='同步执行命令')
def api_server_exec(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    if not has_shell(srv):
        raise ApiError('该服务器未启用命令执行', 400, 'bad_request')
    data = req.json()
    cmd = compat.safe_str(data.get('cmd', '')).strip()
    if not cmd:
        raise ApiError('命令不能为空', 400, 'bad_request')
    timeout = clamp_timeout(data.get('timeout'))
    url = httpx.join_url(srv['host'], srv.get('shell_port', 8088), '/api/exec')
    resp = httpx.DEFAULT.post(url, json_body={'cmd': cmd, 'timeout': timeout},
                              headers=shell_auth_headers(srv), timeout=timeout + 5)
    AUDIT.write_req(req, 'cmd.exec', target=name, detail=cmd[:200])
    if resp.status == 0:
        return req.json_out({'status': 'error', 'code': 'upstream', 'message': '远端不可达'}, 502)
    return req.respond(resp.status, resp.body,
                       resp.header('Content-Type') or 'application/json', {})


# ---- 命令执行券（v2.0.7）----
# 浏览器 EventSource 无法自定义请求头，终端页面只能把命令与令牌写进 URL，于是链路上
# 出现 `GET .../exec-stream?cmd=whoami&token=...` —— 这正是安全设备「URI cmd=xxx
# 命令执行请求」这类 webshell 特征规则的命中形态（现场态势感知误报来源）。
# 改为两步：POST /api/servers/<name>/exec-ticket 换券（命令在请求体、令牌在请求头、
# 权限与审计照旧），再 GET .../exec-stream?ticket=xxx 取流 —— URL 上只剩一次性随机串。
# 券绑定来源 IP、60 秒有效、用后即焚（弹出即失效，重放拿不到第二次）。
EXEC_TICKETS = {}
EXEC_TICKETS_LOCK = threading.Lock()
EXEC_TICKET_TTL = 60
MAX_EXEC_TICKETS = 300


def new_exec_ticket(server_name, cmd, timeout, client_ip):
    """登记一条待执行命令，返回一次性券号。"""
    ticket = compat.rand_hex(16)
    now = time.time()
    with EXEC_TICKETS_LOCK:
        for key in [k for k, v in EXEC_TICKETS.items() if now - v['ts'] > EXEC_TICKET_TTL]:
            EXEC_TICKETS.pop(key, None)
        if len(EXEC_TICKETS) >= MAX_EXEC_TICKETS:
            stale = sorted(EXEC_TICKETS.items(), key=lambda kv: kv[1]['ts'])
            for key, _ in stale[:MAX_EXEC_TICKETS // 4]:
                EXEC_TICKETS.pop(key, None)
        EXEC_TICKETS[ticket] = {'server': server_name, 'cmd': cmd, 'timeout': timeout,
                                'ip': client_ip, 'ts': now}
    return ticket


def take_exec_ticket(ticket, client_ip):
    """取券（一次性）：校验过期与来源 IP，返回记录或 None。"""
    if not ticket:
        return None
    with EXEC_TICKETS_LOCK:
        item = EXEC_TICKETS.pop(ticket, None)
    if item is None or (time.time() - item['ts']) > EXEC_TICKET_TTL:
        return None
    if item['ip'] != client_ip:
        return None
    return item


@app.post('/api/servers/<name>/exec-ticket', perm='cmd.exec', summary='换取命令执行券')
def api_server_exec_ticket(req):
    """命令放请求体换一次性的取流券（终端页面用，避免命令与令牌出现在 URL 上）。"""
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    if not has_shell(srv):
        raise ApiError('该服务器未启用命令执行', 400, 'bad_request')
    data = req.json()
    cmd = compat.safe_str(data.get('cmd', '')).strip()
    if not cmd:
        raise ApiError('命令不能为空', 400, 'bad_request')
    timeout = clamp_timeout(data.get('timeout'))
    ticket = new_exec_ticket(name, cmd, timeout, req.client_ip)
    return ok({'ticket': ticket, 'ttl': EXEC_TICKET_TTL, 'timeout': timeout})


@app.get('/api/servers/<name>/exec-stream', perm='cmd.exec', summary='命令实时输出')
def api_server_exec_stream(req):
    ticket = req.q('ticket', '').strip()
    cmd = req.q('cmd', '').strip()
    timeout = clamp_timeout(req.qi('timeout', 30))
    name = compat.urlunquote(req.params['name'])
    if ticket:
        # 凭券取流：命令来自券记录，URL 上没有任何命令关键字
        item = take_exec_ticket(ticket, req.client_ip)
        if not item:
            raise ApiError('执行券无效或已过期', 403, 'forbidden')
        name = item['server']
        cmd = item['cmd']
        timeout = item['timeout']
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    if not has_shell(srv):
        raise ApiError('该服务器未启用命令执行', 400, 'bad_request')
    if not cmd:
        raise ApiError('命令不能为空', 400, 'bad_request')
    stream = req.sse()
    stream.ready()
    headers = dict(shell_auth_headers(srv))
    headers.setdefault('Content-Type', 'application/json; charset=utf-8')
    upstream = httpx.join_url(srv['host'], srv.get('shell_port', 8088), '/api/exec/stream')
    # 优先 POST（命令在请求体）：上游 URI 上不再出现命令关键字；
    # web_shell v1.3.0+ 支持；老版本返回非 200 → 退回 GET 查询串形态再试；
    # 都没有该接口时走同步降级。
    # 降级链只在 raw_sse_proxy 返回 'nogood'（未写出任何事件）时进行：
    # 一旦返回 'partial'（已推出过事件），绝不能再 GET/同步重发，否则同一条命令
    # 会被执行两次。
    body = compat.json_bytes({'cmd': cmd, 'timeout': timeout})
    res = raw_sse_proxy(upstream, 'POST', headers, body, stream, default_event='out')
    if res == 'nogood':
        legacy = upstream + '?cmd=%s&timeout=%d' % (compat.urlquote(cmd, safe=''), timeout)
        res = raw_sse_proxy(legacy, 'GET', headers, None, stream, default_event='out')
    if res == 'nogood':
        stream.send('start', event='start')
        ok_e, out = shell_exec(srv, cmd, timeout)
        for line in out.splitlines():
            stream.send(line, event='out')
        stream.send(compat.to_text('returncode=0' if ok_e else 'returncode=1'), event='exit')
    stream.close()
    return None


def exec_async_job(ctx, srv=None, cmd='', timeout=30):
    ctx.check_canceled()
    ctx.log('=== 执行命令 ===')
    ok_e, out = shell_exec(srv, cmd, timeout)
    ctx.log(out)
    ctx.set(output=out, ok=ok_e)
    return {'ok': ok_e}


@app.post('/api/servers/<name>/exec-async', perm='cmd.exec', summary='异步执行命令')
def api_server_exec_async(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    if not has_shell(srv):
        raise ApiError('该服务器未启用命令执行', 400, 'bad_request')
    data = req.json()
    cmd = compat.safe_str(data.get('cmd', '')).strip()
    if not cmd:
        raise ApiError('命令不能为空', 400, 'bad_request')
    timeout = clamp_timeout(data.get('timeout'))
    srv_ref = srv
    cmd_ref = cmd
    timeout_ref = timeout

    def job(ctx):
        return exec_async_job(ctx, srv=srv_ref, cmd=cmd_ref, timeout=timeout_ref)

    t = TM.submit('命令执行 %s' % name, 'exec', job, user=req.user, target=name)
    AUDIT.write_req(req, 'cmd.exec', target=name, detail=cmd[:200])
    return ok({'task_id': t['id']})


@app.post('/api/servers/<name>/batch-delete', perm='file.delete', summary='批量删除远端文件')
def api_server_batch_delete(req):
    name = compat.urlunquote(req.params['name'])
    srv = require_server(req, name)
    if not srv:
        raise ApiError('服务器不存在', 404, 'not_found')
    data = req.json()
    paths = data.get('paths')
    if not isinstance(paths, list) or not paths:
        raise ApiError('paths 不能为空', 400, 'bad_request')
    code, resp = relay_request(srv, 'upload', 'POST', '/api/batch/delete',
                               body=compat.json_bytes({'paths': paths}),
                               headers=upload_auth_headers(srv), timeout=120)
    AUDIT.write_req(req, 'file.delete', target=name, detail='批量删除 %d 个' % len(paths))
    if code == 0:
        return req.json_out({'status': 'error', 'code': 'upstream', 'message': '转发失败'}, 502)
    try:
        body = compat.json_loads(resp, {'status': 'success' if code == 200 else 'error'})
    except Exception:
        body = {'status': 'success' if code == 200 else 'error'}
    return req.json_out(body, code if code else 502)


# ================= 通用转发入口（手动注册，多段路径） =================
def relay_handler(req):
    name = compat.urlunquote(req.params['name'])
    rest = req.params.get('rest', '')
    segs = [s for s in rest.split('/') if s]
    if not segs:
        raise ApiError('转发路径不完整', 400, 'bad_request')
    kind = segs[0]
    if kind not in ('upload', 'shell'):
        raise ApiError('转发类型仅支持 upload / shell', 400, 'bad_request')
    perm = 'file.write' if kind == 'upload' else 'cmd.exec'
    if app.auth and not app.auth.has_perm(req.session, perm):
        raise ApiError('当前角色无此权限（缺少 %s）' % perm, 403, 'forbidden')
    srv = require_server(req, name)
    if kind == 'upload' and not has_upload(srv):
        raise ApiError('该服务器未启用文件上传', 400, 'bad_request')
    if kind == 'shell' and not has_shell(srv):
        raise ApiError('该服务器未启用命令执行', 400, 'bad_request')
    remote_path = '/' + '/'.join(segs[1:]) if len(segs) > 1 else '/'
    if req_query(req):
        remote_path += '?' + req_query(req)
    length = int(req.handler.headers.get('Content-Length') or 0)
    timeout = 3600 if ('chunk/upload' in remote_path or 'chunk/put' in remote_path) else 60
    fwd = relay_headers(req.headers)
    if length > 0:
        # 请求体由 relay_request 直接从 rfile 流式读走并转发；这里显式声明
        # 已消费，避免框架在响应前再做一次排空而把连接挂住。
        req.mark_body_consumed()
        code, rh, raw = relay_request(srv, kind, req.method, remote_path, headers=fwd,
                                      src=req.handler.rfile, src_len=length, timeout=timeout)
    else:
        code, rh, raw = relay_request(srv, kind, req.method, remote_path, headers=fwd, timeout=timeout)
    AUDIT.write_req(req, 'proxy.relay', target=name, detail='%s %s' % (kind, remote_path))
    if code == 0:
        return req.json_out({'status': 'error', 'code': 'upstream',
                            'message': '代理转发失败: ' + compat.safe_str(raw)}, 502)
    ctype = rh.get('Content-Type') or rh.get('content-type') or 'application/octet-stream'
    extra = {}
    cdisp = rh.get('Content-Disposition') or rh.get('content-disposition')
    if cdisp:
        extra['Content-Disposition'] = cdisp
    req.respond(code, raw, ctype, extra)
    return None


_RELAY_REGEX = re.compile(r'^/api/servers/(?P<name>[^/]+)/r/(?P<rest>.+)$')
for _m in ('GET', 'POST', 'PUT', 'DELETE'):
    app.router.routes.append({
        'method': _m, 'pattern': '/api/servers/<name>/r/<rest>',
        'regex': _RELAY_REGEX, 'func': relay_handler, 'perm': None,
        'auth': True, 'summary': '通用转发入口', 'raw': False, 'tags': ['proxy'],
    })


# ================= 路由：监控总览 =================
@app.get('/api/monitor/overview', perm='server.view', summary='所有服务器指标总览')
def api_monitor_overview(req):
    servers = AUTH.filter_servers(req.session, load_servers())
    items = []
    for s in servers:
        items.append({'name': s['name'], 'metrics': collect_metrics(s)})
    return ok({'items': items})


# ================= 路由：资产台账 =================
def cmdb_raw_list():
    servers = load_servers()
    items = CMDB.load().get('items', {})
    out = []
    for s in servers:
        name = s['name']
        rec = items.get(name) or {}
        out.append({'name': name, 'group': s.get('group'), 'env': s.get('env'),
                    'tags': s.get('tags') or [], 'auto': rec.get('auto') or {},
                    'manual': rec.get('manual') or {}, 'updated': rec.get('updated')})
    return out


def cmdb_collect_job(ctx, selector=None, names=None):
    sel = selector or {}
    if names:
        sel = compat.merge_dict(sel, {'names': names})
    servers = select_servers(sel)
    total = len(servers)
    done = 0
    for s in servers:
        ctx.check_canceled()
        ctx.log('采集 %s ...' % s['name'])
        auto = {'sysinfo': collect_sysinfo(s), 'metrics': collect_metrics(s),
                'collected_at': time.time()}

        def _mut(d, name=s['name'], auto=auto):
            recs = d.setdefault('items', {})
            rec = recs.setdefault(name, {})
            rec['auto'] = auto
            rec['updated'] = time.time()
            return d

        CMDB.update(_mut)
        done += 1
        ctx.progress(int(100.0 * done / total) if total else 100, '已采集 %d/%d' % (done, total))
    ctx.set(count=done)
    return {'count': done}


@app.get('/api/cmdb', perm='server.view', summary='资产台账列表')
def api_cmdb(req):
    kw = req.q('kw').lower()
    env = req.q('env')
    group = req.q('group')
    tag = req.q('tag')
    out = []
    for it in cmdb_raw_list():
        if env and it.get('env') != env:
            continue
        if group and it.get('group') != group:
            continue
        if tag and tag not in (it.get('tags') or []):
            continue
        if kw and kw not in it.get('name', '').lower() and kw not in compat.safe_str(
                (it.get('auto') or {}).get('hostname', '')).lower():
            continue
        out.append(it)
    return ok({'items': out, 'total': len(out)})


@app.post('/api/cmdb/collect', perm='task.run', summary='并发采集台账')
def api_cmdb_collect(req):
    data = req.json()
    t = TM.submit('台账采集', 'cmdb_collect', cmdb_collect_job,
                  params={'selector': data.get('selector'), 'names': data.get('names')},
                  user=req.user)
    AUDIT.write_req(req, 'task.run', target='cmdb', detail='采集台账')
    return ok({'task_id': t['id']})


@app.put('/api/cmdb/<name>', perm='cmdb.edit', summary='编辑人工字段')
def api_cmdb_put(req):
    name = compat.urlunquote(req.params['name'])
    if not find_server(name):
        raise ApiError('服务器不存在', 404, 'not_found')
    data = req.json()
    manual = {}
    for k in ('owner', 'business', 'level', 'idc', 'note', 'asset_no'):
        if k in data:
            manual[k] = data[k]

    def _mut(d):
        recs = d.setdefault('items', {})
        rec = recs.setdefault(name, {})
        rec['manual'] = compat.merge_dict(rec.get('manual') or {}, manual)
        rec['updated'] = time.time()
        return d

    CMDB.update(_mut)
    AUDIT.write_req(req, 'cmdb.update', target=name, detail=','.join(manual.keys()))
    return ok({'manual': manual})


@app.get('/api/cmdb/export', perm='server.view', summary='导出台账 CSV')
def api_cmdb_export(req):
    fmt = req.q('format', 'csv')
    if fmt != 'csv':
        raise ApiError('仅支持 csv', 400, 'bad_request')
    rows = [['名称', '分组', '环境', '标签', '主机名', 'OS', '内核', 'CPU', '内存',
             '责任人', '业务', '等级', '机房', '资产编号', '备注']]
    for it in cmdb_raw_list():
        auto = it.get('auto') or {}
        sysinfo = auto.get('sysinfo') or {}
        manual = it.get('manual') or {}
        rows.append([it.get('name'), it.get('group'), it.get('env'),
                     ','.join(it.get('tags') or []), sysinfo.get('hostname', ''),
                     sysinfo.get('os_name', ''), sysinfo.get('kernel', ''),
                     sysinfo.get('cpu_model', ''), sysinfo.get('mem_total', ''),
                     manual.get('owner', ''), manual.get('business', ''),
                     manual.get('level', ''), manual.get('idc', ''),
                     manual.get('asset_no', ''), manual.get('note', '')])
    return csv_response(req, rows, 'cmdb.csv')


@app.get('/api/cmdb/summary', perm='server.view', summary='台账汇总')
def api_cmdb_summary(req):
    by_env = {}
    by_group = {}
    by_os = {}
    by_kernel = {}
    for it in cmdb_raw_list():
        auto = it.get('auto') or {}
        env = it.get('env') or '未知'
        by_env[env] = by_env.get(env, 0) + 1
        group = it.get('group') or '未分组'
        by_group[group] = by_group.get(group, 0) + 1
        by_os[compat.safe_str(auto.get('os_name', '未知'))] = by_os.get(
            compat.safe_str(auto.get('os_name', '未知')), 0) + 1
        by_kernel[compat.safe_str(auto.get('kernel', '未知'))] = by_kernel.get(
            compat.safe_str(auto.get('kernel', '未知')), 0) + 1
    return ok({'by_env': by_env, 'by_group': by_group, 'by_os': by_os,
              'by_kernel': by_kernel, 'total': len(cmdb_raw_list())})


@app.get('/api/cmdb/<name>', perm='server.view', summary='单台台账')
def api_cmdb_get(req):
    name = compat.urlunquote(req.params['name'])
    rec = CMDB.load().get('items', {}).get(name)
    if rec is None:
        raise ApiError('台账不存在', 404, 'not_found')
    return ok({'name': name, 'auto': rec.get('auto') or {}, 'manual': rec.get('manual') or {},
              'updated': rec.get('updated')})


# ================= 路由：审计 =================
@app.get('/api/audit', perm='audit.view', summary='审计分页查询')
def api_audit(req):
    data = AUDIT.query(user=req.q('user'), action=req.q('action'), target=req.q('target'),
                       keyword=req.q('keyword'), result=req.q('result'),
                       start=opt_float(req.q('start')),
                       end=opt_float(req.q('end')),
                       limit=req.qi('limit', 100, 1, 1000), offset=req.qi('offset', 0, 0))
    return ok(data)


@app.get('/api/audit/stats', perm='audit.view', summary='审计概览')
def api_audit_stats(req):
    return ok(AUDIT.stats(days=req.qi('days', 7, 1, 365)))


@app.get('/api/audit/verify', perm='audit.view', summary='校验哈希链')
def api_audit_verify(req):
    ok_v, msg, checked, broken = AUDIT.verify()
    return ok({'ok': ok_v, 'message': msg, 'checked': checked})


@app.get('/api/audit/export.csv', perm='audit.view', summary='导出审计 CSV')
def api_audit_export(req):
    rows = AUDIT.export_rows()
    return csv_response(req, rows, 'audit.csv')


# ================= 路由：任务 =================
@app.get('/api/tasks', perm='server.view', summary='任务列表')
def api_tasks(req):
    return ok({'tasks': TM.list(state=req.q('state'), kind=req.q('kind'),
                                limit=req.qi('limit', 50, 1, 500))})


@app.get('/api/tasks/stats', perm='server.view', summary='任务统计')
def api_tasks_stats(req):
    return ok(TM.stats())


@app.get('/api/tasks/<id>', perm='server.view', summary='任务详情')
def api_task_get(req):
    tid = req.params['id']
    task = TM.get(tid)
    if not task:
        raise ApiError('任务不存在', 404, 'not_found')
    return ok({'task': task})


@app.post('/api/tasks/<id>/cancel', perm='task.run', summary='取消任务')
def api_task_cancel(req):
    tid = req.params['id']
    ok_c, msg = TM.cancel(tid)
    if not ok_c:
        raise ApiError(msg, 400, 'bad_request')
    return ok({'message': msg})


@app.get('/api/tasks/<id>/stream', perm='server.view', summary='任务进度流')
def api_task_stream(req):
    tid = req.params['id']
    stream_events(req, ['task:%s' % tid])
    return None


# ================= 路由：通知 =================
@app.get('/api/notify/config', perm='settings.edit', summary='通知配置（脱敏）')
def api_notify_config(req):
    return ok(NOTIFIER.public_config())


@app.post('/api/notify/config', perm='settings.edit', summary='保存通知配置')
def api_notify_config_save(req):
    data = req.json()
    NOTIFIER.update(data)
    AUDIT.write_req(req, 'settings.update', target='notify', detail='保存通知配置')
    return ok(NOTIFIER.public_config())


@app.post('/api/notify/test', perm='settings.edit', summary='通知连通性自测')
def api_notify_test(req):
    data = maybe_json(req)
    out = NOTIFIER.test(data.get('channel_id'))
    return ok({'results': out})


# ================= 路由：设置 =================
def settings_locked():
    """哪些设置被「启动参数」钉死了（设置页改了也不生效，需在 UI 明确告知）。

    `--release-url` / `--release-token` / `--token` 的优先级高于设置页，
    否则运维会陷入「地址改了却没变化」的困惑。
    """
    locked = {'release_url': bool(ARGS.release_url),
              'release_token': bool(ARGS.release_token)}
    # 已配置令牌时「账号/口令自动登录」这条路不会被走到（令牌优先）
    locked['release_login'] = bool(rc_static_token())
    return locked


def settings_view(req, st):
    """回给前端的设置：无 settings.edit 权限者不下发发布中心凭据（其本就无权修改）"""
    out = dict(st)
    if not (app.auth and app.auth.has_perm(req.session, 'settings.edit')):
        # release_token 等价于发布中心管理员令牌、release_pass 为登录口令，
        # 一并脱敏，避免任意登录用户 GET /api/settings 就能拿到。
        for secret in ('release_pass', 'release_token'):
            if out.get(secret):
                out[secret] = ''
    return out


@app.get('/api/settings', perm='server.view', summary='全局设置')
def api_settings(req):
    return ok({'settings': settings_view(req, current_settings()),
               'locked': settings_locked()})


@app.post('/api/settings', perm='settings.edit', summary='保存全局设置')
def api_settings_save(req):
    data = req.json()
    new_settings = normalize_settings(compat.merge_dict(current_settings(), data))

    def _mut(d):
        d['settings'] = new_settings
        return d

    CONFIG.update(_mut)
    AUDIT.write_req(req, 'settings.update', target='settings', detail='保存全局设置')
    return ok({'settings': settings_view(req, new_settings), 'locked': settings_locked()})


# ================= 路由：发布中心视图（手动注册） =================
def release_handler(req):
    rest = req.params['rest']
    if req.method == 'POST' and app.auth and not app.auth.has_perm(req.session, 'release.run'):
        raise ApiError('无发布权限（缺少 release.run）', 403, 'forbidden')
    if rest == 'config':
        return release_config(req)
    if rest.endswith('/stream'):
        stream = req.sse()
        stream.ready()
        url = _rc_pick_base().rstrip('/') + '/api/' + rest
        if req_query(req):
            url += '?' + req_query(req)
        raw_sse_proxy(url, 'GET', rc_headers(_rc_pick_base()), None, stream, default_event='release')
        stream.close()
        return None
    # 制品包上传改走流式透传：raw_body() 的 32MB 上限会把大包挡掉，也不能整包进内存
    if req.method == 'POST' and rest.split('?')[0].rstrip('/').endswith('packages/upload'):
        return release_upload_proxy(req, rest)
    return release_proxy(req, rest)


_RELEASE_REGEX = re.compile(r'^/api/release/(?P<rest>.+)$')
for _m in ('GET', 'POST'):
    app.router.routes.append({
        'method': _m, 'pattern': '/api/release/<rest>',
        'regex': _RELEASE_REGEX, 'func': release_handler, 'perm': None,
        'auth': True, 'summary': '发布中心透传', 'raw': False, 'tags': ['release'],
    })


# ================= 路由：统一事件流 =================
@app.get('/api/events', perm='server.view', summary='统一事件流（SSE）')
def api_events(req):
    topics = [t for t in req.q('topics', 'task,metrics,alert').split(',') if t]
    stream_events(req, topics)
    return None


@app.post('/api/events/push', perm='release.run', summary='外部回推进度')
def api_events_push(req):
    data = req.json()
    topic = compat.safe_str(data.get('topic', ''))
    if not topic:
        raise ApiError('缺少 topic', 400, 'bad_request')
    BUS.publish(topic, data.get('data'))
    AUDIT.write_req(req, 'release.run', target=topic, detail='事件回推')
    return ok({'published': 1})


# ================= 后台：指标采样广播 =================
def metrics_sampler():
    while True:
        try:
            interval = current_settings().get('metrics_interval') or 10
            time.sleep(interval)
            for s in load_servers():
                try:
                    BUS.publish('metrics', {'name': s['name'], 'metrics': collect_metrics(s)})
                except Exception:
                    pass
        except Exception:
            time.sleep(10)


# ================= 启动横幅 =================
def _banner_line(fmt, *args):
    """安全拼装一行横幅。

    Python 2 下「含中文的字节串字面量」与 unicode 值（如从 JSON 读出的配置项）
    做 % 格式化时，会隐式按 ascii 解码字面量并抛 UnicodeDecodeError；
    这里把字面量与参数统一归一为文本类型后再拼接，py2/py3 行为一致。
    """
    return compat.to_text(fmt) % tuple(compat.to_text(a) for a in args)


def build_banner(host, port):
    lines = []
    lines.append('=' * 66)
    lines.append(_banner_line('[ops_Platform] 运维工具集成平台 v%s 已启动', VERSION))
    lines.append(_banner_line('浏览器访问  : http://<本机IP>:%s', port))
    lines.append(_banner_line('监听地址    : %s:%s', host, port))
    lines.append(_banner_line('数据目录    : %s', DATA_DIR))
    lines.append(_banner_line('访问令牌    : %s',
                              '已开启（CI/CD 等价管理员）' if ARGS.token else '未开启'))
    if ARGS.no_login:
        lines.append('登录要求    : 已关闭（全开放）')
    stat = AUTH.stat()
    lines.append(_banner_line('用户数      : %s（管理员账号 %s）', stat['users'], 'admin'))
    if AUTH.default_admin_password:
        lines.append(_banner_line('首启口令    : %s（请尽快修改）', AUTH.default_admin_password))
    elif ARGS.admin_password and not ARGS.no_login:
        lines.append('提示        : 用户库已存在，--admin-password 未生效；'
                     '重置口令请加 --reset-admin-password')
    for problem in AUTH.diagnose():
        lines.append(_banner_line('[!] %s', problem))
    rel = normalize_base_url(ARGS.release_url or current_settings().get('release_url'))
    lines.append(_banner_line('发布中心    : %s', rel))
    lines.append(_banner_line('运行环境    : Python %s', sys.version.split()[0]))
    lines.append('=' * 66)
    return lines


# ================= 入口 =================
def main():
    migrate_legacy_config()
    host = ARGS.host or '0.0.0.0'
    port = int(ARGS.port or 8095)
    compat.start_daemon(metrics_sampler)
    app.run(host, port, banner=build_banner(host, port))


if __name__ == '__main__':
    main()
