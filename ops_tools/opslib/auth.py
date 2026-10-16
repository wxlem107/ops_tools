#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.auth —— 多用户、角色与授权。

模型：
  * 用户：用户名 + 加盐 PBKDF2 口令摘要 + 角色 + 可访问服务器清单；
  * 角色：admin / ops / viewer，权限集合见 ROLES；
  * 会话：登录后发放随机 token，默认 12 小时有效，可持久化（服务重启不掉线）；
  * 兼容：仍支持单一「API Token」（原 --token）作为管理员身份的开放接口凭据，供 CI/CD 调用。

安全说明：
  * 口令摘要使用 PBKDF2-HMAC-SHA256，Python 2.7.5 无 hashlib.pbkdf2_hmac 时使用纯 Python 兜底实现；
  * 摘要比较使用定时安全比较，避免计时侧信道；
  * users.json / sessions.json 以 0600 权限落盘。
"""
from __future__ import print_function

import binascii
import hashlib
import hmac as _hmac
import os
import struct
import time

from .compat import (PY2, constant_time_eq, log, rand_hex, safe_str, string_types,
                     to_bytes, to_text)
from .store import JsonStore

# ---------------------------------------------------------------- 权限定义
PERMS = [
    'platform.view',    # 登录后查看控制台
    'server.view',      # 查看服务器列表 / 状态 / 文件
    'server.edit',      # 增删改服务器条目（含标签、分组、环境）
    'file.read',        # 下载 / 浏览文件
    'file.write',       # 上传文件
    'file.delete',      # 删除文件
    'cmd.exec',         # 下发命令 / 打开终端
    'release.run',      # 执行发布单
    'task.run',         # 触发脚本 / 定时任务
    'cmdb.edit',        # 编辑资产台账
    'audit.view',       # 查看审计流水
    'user.admin',       # 用户与角色管理
    'settings.edit',    # 修改全局设置
]

ROLES = {
    'admin': {
        'label': '管理员',
        'perms': ['*'],
        'desc': '全部权限，含用户管理与全局设置',
    },
    'ops': {
        'label': '运维',
        'perms': ['platform.view', 'server.view', 'server.edit', 'file.read', 'file.write',
                  'file.delete', 'cmd.exec', 'release.run', 'task.run', 'cmdb.edit',
                  'audit.view', 'settings.edit'],
        'desc': '可操作服务器、执行命令与发布，不可管理用户',
    },
    'viewer': {
        'label': '只读',
        'perms': ['platform.view', 'server.view', 'file.read', 'audit.view'],
        'desc': '仅查看，任何写操作与命令执行均被拒绝',
    },
}

ROLE_ORDER = ['admin', 'ops', 'viewer']
PBKDF2_ITERATIONS = 120000
MIN_PASSWORD_LEN = 6
SESSION_TTL = 12 * 3600
SESSION_MAX = 2000


# ---------------------------------------------------------------- 口令摘要
def _pbkdf2(password, salt, iterations, dklen):
    """PBKDF2-HMAC-SHA256；Python 2.7.5 没有 hashlib.pbkdf2_hmac，提供纯 Python 兜底"""
    if hasattr(hashlib, 'pbkdf2_hmac'):
        return hashlib.pbkdf2_hmac('sha256', to_bytes(password), to_bytes(salt), iterations, dklen)
    # RFC 2898 标准实现（仅需 hmac + hashlib）
    mac = _hmac.new(to_bytes(password), None, hashlib.sha256)
    blocks = []
    need = dklen
    counter = 1
    while need > 0:
        mac = _hmac.new(to_bytes(password), to_bytes(salt) + struct.pack('>I', counter), hashlib.sha256)
        u = mac.digest()
        out = bytearray(u)
        for _ in range(iterations - 1):
            mac = _hmac.new(to_bytes(password), u, hashlib.sha256)
            u = mac.digest()
            if PY2:
                for i in range(len(out)):
                    out[i] ^= ord(u[i])
            else:
                for i in range(len(out)):
                    out[i] ^= u[i]
        blocks.append(bytes(out) if not PY2 else ''.join(chr(b) for b in out))
        need -= len(out)
        counter += 1
    return (b''.join(blocks) if not PY2 else ''.join(blocks))[:dklen]


def _digest_hex(digest):
    """把 PBKDF2 输出统一为「十六进制文本」。

    坑（py2 独有）：Python 2 的 bytes 就是 str，而 str.hex() 是 Python 3.5
    才引入的方法，因此 py2 下 hasattr(dk, 'hex') 恒为 False。若直接返回原始
    二进制摘要会有两个后果：
      1) 落盘时 json 把它当字节串写进 JSON 文本，再经 to_text(..., 'replace')
         解码，产生不可逆的 U+FFFD 损坏；
      2) verify_password 按「hex 文本长度的一半」推导摘要长度，会得到 16 而非
         32 字节，口令永远校验失败。
    所以这里必须显式用 binascii.hexlify 转换，保证 py2/py3 落盘形态一致。
    """
    if hasattr(digest, 'hex'):
        return digest.hex()
    data = binascii.hexlify(to_bytes(digest))
    return data if PY2 else data.decode('ascii')


def _is_hex_text(raw):
    """判断 bytes 是否为「纯十六进制文本」（py2 的 str 与 py3 的 bytes 都适用）"""
    data = to_bytes(raw)
    if not data or len(data) % 2:
        return False
    try:
        binascii.unhexlify(data)
        return True
    except (TypeError, ValueError, binascii.Error):
        return False


def _digest_shape(stored):
    """从已存摘要反推 (dklen, 形态)；形态为 'hex'（文本）或 'raw'（原始二进制）"""
    data = to_bytes(stored)
    if _is_hex_text(data):
        return len(data) // 2, 'hex'
    return len(data), 'raw'


# 「可用口令记录」要求的最小 hex 摘要长度（16 字节摘要）
_DIGEST_HEX_MIN = 32


def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    """返回 {'salt','hash','iter'}，可直接塞进用户记录（hash 恒为 hex 文本）"""
    salt = salt or rand_hex(16)
    dk = _pbkdf2(password, salt, iterations, 32)
    return {'salt': salt, 'hash': _digest_hex(dk), 'iter': iterations}


def verify_password(password, record):
    """校验口令；record 为 hash_password 的产物。

    兼容两种已存形态：
      * hex 文本（当前实现，py2/py3 落盘一致）；
      * 原始二进制（早期 py2 版本因 str 无 .hex() 而写入，多数已在落盘时损坏，
        这里仍按原长度尝试比对，避免把「可救」的记录判死）。
    """
    if not isinstance(record, dict) or not record.get('salt') or not record.get('hash'):
        return False
    try:
        iterations = int(record.get('iter') or PBKDF2_ITERATIONS)
    except (TypeError, ValueError):
        iterations = PBKDF2_ITERATIONS
    stored = to_bytes(record['hash'])
    dklen, form = _digest_shape(stored)
    if dklen <= 0:
        return False
    dk = _pbkdf2(password, record['salt'], iterations, dklen)
    if form == 'hex':
        return constant_time_eq(_digest_hex(dk).lower(), stored.lower())
    return constant_time_eq(dk, stored)


def password_record_ok(record):
    """口令记录是否「可用」。

    摘要必须是 hex 文本且长度合理（至少 16 字节摘要）。早期 Python 2 版本写入的
    原始二进制摘要在落盘时已损坏，无法再校验，只能重置口令；仅有 6 位十六进制
    字符之类的历史明文记录同样判为不可用。
    """
    if not isinstance(record, dict) or not record.get('salt'):
        return False
    data = to_bytes(record.get('hash') or '')
    return len(data) >= _DIGEST_HEX_MIN and _is_hex_text(data)


def check_password_strength(password):
    if not isinstance(password, string_types) or len(password) < MIN_PASSWORD_LEN:
        return False, '口令长度至少 %d 位' % MIN_PASSWORD_LEN
    return True, ''


# ---------------------------------------------------------------- 授权主体
class Auth(object):
    def __init__(self, data_dir, legacy_token='', session_ttl=SESSION_TTL,
                 require_login=True, admin_password=None):
        self.data_dir = os.path.abspath(data_dir or '.')
        self.legacy_token = legacy_token or ''
        self.session_ttl = int(session_ttl)
        self.require_login = bool(require_login)
        self.users_path = os.path.join(self.data_dir, 'users.json')
        self.sessions_path = os.path.join(self.data_dir, 'sessions.json')
        self.users = JsonStore(self.users_path, {'users': []}, file_mode=0o600)
        self.sessions = JsonStore(self.sessions_path, {'sessions': {}}, file_mode=0o600)
        self._last_cleanup = 0.0
        self.default_admin_password = None
        self.bootstrap(admin_password)

    # -------------------------------------------------------------- 初始化
    def bootstrap(self, admin_password=None):
        """首次运行创建管理员账号；返回 (created, username, password)"""
        existing = self.user_get('admin')
        if existing:
            # 修复路径：早期 Python 2 版本写入的原始二进制摘要已在落盘时损坏，
            # 该账号再也无法登录；此时若显式提供了 --admin-password，就顺手修回来。
            if not password_record_ok(existing.get('password')):
                if admin_password:
                    ok, msg = self.user_update('admin', {'password': admin_password})
                    if ok:
                        self.default_admin_password = admin_password
                        return True, 'admin', admin_password
                    log('[auth] admin 口令记录已损坏，且 --admin-password 无法修复：%s' % msg)
                else:
                    log('[auth] admin 口令记录已损坏（早期 Python 2 版本写入的原始二进制摘要），'
                        '无法登录，请用 --reset-admin-password 重置')
            return False, 'admin', None
        password = admin_password or rand_hex(6)
        ok, msg = check_password_strength(password)
        if not ok:
            password = rand_hex(6)
        self.user_add('admin', password, 'admin', servers=['*'], note='首次运行自动创建')
        self.default_admin_password = password
        return True, 'admin', password

    def diagnose(self):
        """启动自检：返回需要提醒管理员的问题清单（供启动横幅打印）"""
        problems = []
        for u in self.user_list():
            if not password_record_ok(u.get('password')):
                problems.append(
                    '账号 %s 的口令摘要已损坏（早期 Python 2 版本写入的原始二进制摘要），'
                    '无法登录；请用 --reset-admin-password 重置' % safe_str(u.get('username')))
        return problems

    # -------------------------------------------------------------- 用户管理
    def user_list(self):
        return list(self.users.load().get('users', []))

    def user_get(self, username):
        username = safe_str(username).strip()
        for u in self.user_list():
            if u.get('username') == username:
                return u
        return None

    def user_add(self, username, password, role='viewer', servers=None, note=''):
        username = safe_str(username).strip()
        if not username:
            return False, '用户名不能为空'
        if self.user_get(username):
            return False, '用户名已存在'
        ok, msg = check_password_strength(password)
        if not ok:
            return False, msg
        if role not in ROLES:
            return False, '角色不合法'
        rec = {
            'username': username,
            'role': role,
            'servers': list(servers) if servers else ['*'],
            'enabled': True,
            'note': safe_str(note),
            'created': time.time(),
            'last_login': None,
            'password': hash_password(password),
            'pwd_changed': time.time(),
        }

        def _mut(d):
            d.setdefault('users', []).append(rec)
            return d
        self.users.update(_mut)
        return True, '已创建用户 %s' % username

    def user_update(self, username, data):
        """更新角色 / 服务器授权 / 启用状态 / 备注；password 字段存在则改密

        注意：user_get 返回的是**缓存对象的引用**，绝不能在其上就地修改——
        否则校验失败提前返回时改动会残留在内存，随后任何一次落盘都会把
        （本应被拒绝的）越权角色/授权持久化。因此这里先复制副本、全部校验
        通过后再写回。
        """
        username = safe_str(username).strip()
        target = self.user_get(username)
        if not target:
            return False, '用户不存在'
        new = dict(target)
        new['servers'] = list(target.get('servers') or ['*'])
        if 'role' in data:
            if data['role'] not in ROLES:
                return False, '角色不合法'
            new['role'] = data['role']
        if 'servers' in data:
            servers = data['servers']
            if isinstance(servers, string_types):
                servers = [s.strip() for s in servers.split(',') if s.strip()]
            new['servers'] = list(servers) if servers else ['*']
        if 'enabled' in data:
            new['enabled'] = bool(data['enabled'])
        if 'note' in data:
            new['note'] = safe_str(data['note'])
        if data.get('password'):
            ok, msg = check_password_strength(data['password'])
            if not ok:
                return False, msg
            new['password'] = hash_password(data['password'])
            new['pwd_changed'] = time.time()

        def _mut(d):
            d['users'] = [new if u.get('username') == username else u for u in d.get('users', [])]
            return d
        self.users.update(_mut)
        # 权限/密码变更后，踢掉该用户的全部会话
        self._drop_user_sessions(username)
        return True, '已更新用户 %s' % username

    def user_delete(self, username):
        username = safe_str(username).strip()
        if username == 'admin' and len([u for u in self.user_list() if u.get('role') == 'admin']) <= 1:
            return False, '必须保留至少一个管理员账号'
        before = len(self.user_list())

        def _mut(d):
            d['users'] = [u for u in d.get('users', []) if u.get('username') != username]
            return d
        self.users.update(_mut)
        if len(self.user_list()) == before:
            return False, '用户不存在'
        self._drop_user_sessions(username)
        return True, '已删除用户 %s' % username

    def user_reset_password(self, username, new_password=None):
        user = self.user_get(username)
        if not user:
            return False, '用户不存在', None
        password = new_password or rand_hex(6)
        ok, msg = self.user_update(username, {'password': password})
        return ok, msg, password

    def admin_reset_password(self, new_password=None):
        """强制重置 admin 口令（供 --reset-admin-password 启动项使用）。

        --admin-password 只在首次启动（用户库为空）时生效；用户库已存在时
        需通过本方法显式重置。返回 (ok, message_or_password, created)。
        """
        if not self.user_get('admin'):
            self.bootstrap(new_password or None)
            return True, self.default_admin_password, True
        password = new_password or rand_hex(6)
        ok, msg = self.user_update('admin', {'password': password})
        if not ok:
            return False, msg, False
        self.default_admin_password = password
        return True, password, False

    def public_users(self):
        out = []
        for u in self.user_list():
            out.append({
                'username': u.get('username'),
                'role': u.get('role'),
                'role_label': ROLES.get(u.get('role'), {}).get('label', u.get('role')),
                'servers': u.get('servers') or ['*'],
                'enabled': bool(u.get('enabled', True)),
                'note': u.get('note', ''),
                'created': u.get('created'),
                'last_login': u.get('last_login'),
            })
        return sorted(out, key=lambda x: (ROLE_ORDER.index(x['role']) if x['role'] in ROLE_ORDER else 9,
                                          x['username']))

    # -------------------------------------------------------------- 登录 / 会话
    def login(self, username, password, ip=''):
        username = safe_str(username).strip()
        user = self.user_get(username)
        if not user:
            return {'ok': False, 'message': '用户名或口令错误'}
        if not user.get('enabled', True):
            return {'ok': False, 'message': '该账号已被禁用'}
        if not verify_password(password, user.get('password')):
            return {'ok': False, 'message': '用户名或口令错误'}
        token = self._new_session(username, user.get('role'), user.get('servers') or ['*'], ip,
                                  via='password')

        def _mut(d):
            d['users'] = [dict(u, last_login=time.time()) if u.get('username') == username else u
                          for u in d.get('users', [])]
            return d
        self.users.update(_mut)
        return {'ok': True, 'token': token, 'username': username, 'role': user.get('role'),
                'servers': user.get('servers') or ['*'], 'expires': time.time() + self.session_ttl}

    def _new_session(self, username, role, servers, ip='', via='password'):
        token = rand_hex(24)
        now = time.time()
        rec = {'username': username, 'role': role, 'servers': list(servers or ['*']),
               'ip': safe_str(ip), 'created': now, 'last_seen': now,
               'expires': now + self.session_ttl, 'via': via}

        def _mut(d):
            sessions = d.setdefault('sessions', {})
            sessions[token] = rec
            if len(sessions) > SESSION_MAX:
                # 淘汰最早过期的会话
                ordered = sorted(sessions.items(), key=lambda kv: kv[1].get('expires') or 0)
                for key, _ in ordered[:len(sessions) - SESSION_MAX]:
                    sessions.pop(key, None)
            return d
        self.sessions.update(_mut)
        return token

    def resolve(self, headers=None, query=None):
        """从一个请求中解析出会话；headers/query 均为普通 dict（键名大小写不敏感一次）"""
        headers = headers or {}
        query = query or {}
        token = ''
        for key in ('X-Auth-Token', 'x-auth-token', 'X-Session-Token'):
            if headers.get(key):
                token = headers.get(key)
                break
        if not token:
            token = query.get('token') or query.get('session') or ''
        # 无鉴权模式：直接给一个虚拟管理员身份
        if not self.require_login and not self.legacy_token:
            return {'username': 'anonymous', 'role': 'admin', 'servers': ['*'], 'via': 'none'}
        if not token:
            return None
        # 1) 开放接口：单一 API Token 等价于管理员（供 CI/CD 使用）
        if self.legacy_token and constant_time_eq(token, self.legacy_token):
            return {'username': '__api__', 'role': 'admin', 'servers': ['*'], 'via': 'api_token'}
        # 2) 普通登录会话
        session = self.sessions.load().get('sessions', {}).get(token)
        if not session:
            return None
        now = time.time()
        if (session.get('expires') or 0) < now:
            self.logout(token)
            return None
        if now - (session.get('last_seen') or 0) > 60:
            def _mut(d):
                s = d.get('sessions', {}).get(token)
                if s:
                    s['last_seen'] = now
                return d
            self.sessions.update(_mut)
        session = dict(session)
        session['token'] = token
        return session

    def logout(self, token):
        if not token:
            return

        def _mut(d):
            d.get('sessions', {}).pop(token, None)
            return d
        self.sessions.update(_mut)

    def _drop_user_sessions(self, username):
        def _mut(d):
            d['sessions'] = {k: v for k, v in d.get('sessions', {}).items()
                             if v.get('username') != username}
            return d
        self.sessions.update(_mut)

    def cleanup_sessions(self):
        now = time.time()
        if now - self._last_cleanup < 300:
            return

        def _mut(d):
            d['sessions'] = {k: v for k, v in d.get('sessions', {}).items()
                             if (v.get('expires') or 0) > now}
            return d
        self._last_cleanup = now
        self.sessions.update(_mut)

    def stat(self):
        sessions = self.sessions.load().get('sessions', {})
        now = time.time()
        active = [s for s in sessions.values() if (s.get('expires') or 0) > now]
        return {'users': len(self.user_list()), 'sessions': len(active)}

    # -------------------------------------------------------------- 鉴权
    def role_perms(self, role):
        cfg = ROLES.get(role) or {}
        return cfg.get('perms') or []

    def has_perm(self, session, perm):
        """session 可以是 resolve() 的返回值，也可以是 None"""
        if not session:
            return False
        perms = self.role_perms(session.get('role'))
        return '*' in perms or perm in perms

    def can_access_server(self, session, server_name):
        if not session:
            return False
        servers = session.get('servers') or ['*']
        return '*' in servers or server_name in servers

    def filter_servers(self, session, servers):
        """按授权过滤服务器列表（保持原顺序）"""
        if not session:
            return []
        allowed = session.get('servers') or ['*']
        if '*' in allowed:
            return list(servers)
        return [s for s in servers if s.get('name') in allowed]


def default_data_dir(base_dir, name='data'):
    """约定：各服务的运行数据统一放在 <脚本目录>/data（可用 --work-dir 覆盖）"""
    return os.path.abspath(os.path.join(base_dir, name))


def perm_label(perm):
    return {
        'platform.view': '查看控制台', 'server.view': '查看服务器', 'server.edit': '编辑服务器',
        'file.read': '下载文件', 'file.write': '上传文件', 'file.delete': '删除文件',
        'cmd.exec': '执行命令', 'release.run': '执行发布', 'task.run': '运行任务',
        'cmdb.edit': '编辑台账', 'audit.view': '查看审计', 'user.admin': '用户管理',
        'settings.edit': '修改设置',
    }.get(perm, perm)


__all__ = ['Auth', 'ROLES', 'PERMS', 'ROLE_ORDER', 'hash_password', 'verify_password',
           'password_record_ok', 'check_password_strength', 'default_data_dir', 'perm_label',
           'SESSION_TTL']
