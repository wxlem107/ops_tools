#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
release_center.py —— 发布中心 v1.3（管理机侧，默认 :8094）

定位：编排「包上传 → 备份旧版 → 停服 → 替换 → 启服 → 健康检查 → 失败自动回滚」。
v1.1 起应用可自定义步骤链（app.steps，@upload/@backup/@stop/@replace/@start/@health
为内置动作，其余 cmd 在目标机执行）与失败继续（软失败）；v1.2 起应用可定义参数
（app.param_defs），创建发布单时逐单填写（order.params），执行时覆盖同名 vars
注入到所有步骤命令的 ${key} 占位符。
v1.3 起制品包支持批量上传：前端拖拽 / 多选 / 整目录上传，服务端按「先落临时文件、
算好 MD5 再决定去留」的方式处理，可跳过内容重复的包（skip_dup=1）并记录来源相对
路径（src）便于溯源；发布单可一次挂多个制品包（order.packages），上传步骤会把它们
全部推到目标机并注入 ${package}（第一个，兼容旧模板）、${packages}（全部，空格分隔）
与 ${package1}…${packageN}。
自身不落地业务文件，而是调用目标机上的 upload_server（:8089）与 web_shell（:8088）完成动作。

依赖：opslib 公共层（仅标准库，Python 2.7.5+ / 3.7.5+ 双版本兼容）。请勿直接修改 opslib/。

约束（硬性）：不使用 f-string / pathlib / {**a} / os.replace / shutil.which /
shutil.disk_usage / re.fullmatch / subprocess.DEVNULL / Popen.wait(timeout=) /
Thread(daemon=True) / concurrent.futures，统一用 opslib.compat 替代。

【用法示例】
    # 1) 最简启动：默认 0.0.0.0:8094，首启自动创建管理员 admin（随机口令，见启动横幅）
    python release_center.py

    # 2) 生产推荐：全部显式参数（单行，可直接复制）
    python release_center.py --host 0.0.0.0 --port 8094 --token rc_token_123 --data-dir /data/release --pidfile /data/release_center.pid --admin-password Admin_123!

    # 3) 与运维平台联动（本服务直连平台地址与令牌）
    python release_center.py --port 8094 --token rc_token_123 --platform-url http://127.0.0.1:8095 --platform-token ci_token_123

    # 4) 忘记口令时重置管理员（未给 --admin-password 则随机生成并打印在横幅）
    python release_center.py --port 8094 --reset-admin-password --admin-password NewPass_456!

    # 5) 内网演示：关闭登录要求（全开放，慎用）
    python release_center.py --port 8094 --no-login

    # 6) 后台常驻（nohup + PID 文件）
    nohup python -u release_center.py --port 8094 --token rc_token_123 --pidfile /data/release_center.pid > /data/release_center.log 2>&1 &

    # 7) 查看全部参数与默认值
    python release_center.py --help

【全部参数】（括号内为默认值）
    --host           监听地址          (0.0.0.0)               环境变量 RC_HOST
    --port           监听端口          (8094)                  环境变量 RC_PORT
    --token          开放接口令牌       (空 = 不校验)            环境变量 RC_TOKEN
    --data-dir       数据目录          (脚本同级 data/release/)  环境变量 RC_DATA
    --pidfile        PID 文件          (空 = 不写)              环境变量 RC_PIDFILE
    --platform-url   联动平台地址       (空)                     环境变量 RC_PLATFORM_URL
    --platform-token 联动平台令牌       (空)                     环境变量 RC_PLATFORM_TOKEN
    --admin-password 首启管理员初始口令  (空 = 随机生成，见横幅)   环境变量 RC_ADMIN_PASSWORD
    --reset-admin-password 启动时重置 admin 口令 (关)             无环境变量
    --no-login       关闭登录要求（全开放） (关)                 无环境变量
    --help, -h       打印帮助后退出     无环境变量
    说明：--admin-password 仅在首次启动（用户库不存在）时生效；重置请配合 --reset-admin-password。
"""
from __future__ import print_function

import argparse
import hashlib
import os
import re
import socket
import sys
import time

# 把脚本上级目录（ops_tools/）加入 sys.path，以便从 opslib 引入公共能力
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from opslib import compat, server, apibase, auth as opsauth, sse, httpx, audit, task
from opslib.apibase import ok, err, ApiError

VERSION = '1.3.3'
# 1.3.3  安全加固 + 双版本兼容修复（配合 CODE_REVIEW 整改）：
#        * 参数值白名单 PARAM_VALUE_RE（^[A-Za-z0-9._/@:+=,~ -]{0,256}$）—— 发布单参数
#          原先原样拼进目标机命令的 ${key} 占位符，可注入 `;rm -rf`；现建单即拒可疑值。
#        * ssrf 收敛：制品导入 URL 走 safe_import_url，仅允许 http/https 且拒云元数据地址
#          （169.254.*、metadata.google.internal）。
#        * 越权：新增 require_targets(req, names)，建单/执行/回滚都校验操作者对各目标机的
#          can_access_server；服务器列表用 filter_servers 过滤。
#        * py2 崩溃：normalize_vars/validate_order_params/step_named_cmd 里 u'...' % ... 的
#          参数统一 compat.to_text 包裹（中文标签在 py2 隐式 ascii 解码必崩）。
#        * 事件流防重放：exec_on_target_stream 的同步降级加 seen['any'] 保护，已开跑不再重放。
# 1.3.2  推包流式执行改 POST（命令放请求体、token 走请求头），链路上 URI 不再出现
#        cmd=... —— 避免被态势感知/IDS 的「URI 命令执行」特征规则误报；老版本
#        web_shell（只有 GET 查询串）自动降级，已开跑的命令不会重放。
#        推包时把页面配置的「远端上传目录」随请求带给 upload_server v3.3.0+
#        （--allow-client-dir），改目录即时生效；不一致时在步骤日志里给出提示。

# ---------------------------------------------------------------- 常量
CHUNK_PUT_LIMIT = 16 * 1024 * 1024      # 小于等于该体积走单片直传接口
UPLOAD_TIMEOUT = 300                    # 上传到目标机的超时（秒）
DEFAULT_UPLOAD_DIR = '/opt/upload'      # 目标机 upload_server 默认落盘目录
MAX_ORDER_PACKAGES = 32                 # 单个发布单最多挂几个制品包
UPLOAD_PART_SUFFIX = '.part'            # 上传中的临时文件后缀（不写入索引）
# 实时输出：脚本 stdout/stderr 攒到「这么多字节」或「这么久」就推一批步骤日志
# （step_log 会立刻 emit 到页面，见 StreamLogger / exec_on_target_stream）
STREAM_FLUSH_BYTES = 4096
STREAM_FLUSH_SECONDS = 0.25
STREAM_LOG_LIMIT = 384 * 1024           # 单个步骤最多回传多少字节输出
# 默认步骤链：cmd 以 @ 开头表示内置动作（upload/backup/stop/replace/start/health），
# 自定义步骤链里同样可用这些关键字，非关键字内容则作为 shell 命令在目标机执行。
STEP_DEFS = [
    {'name': 'upload', 'label': u'上传包', 'cmd': '@upload', 'continue_on_fail': False},
    {'name': 'backup', 'label': u'备份旧版', 'cmd': '@backup', 'continue_on_fail': False},
    {'name': 'stop', 'label': u'停服', 'cmd': '@stop', 'continue_on_fail': False},
    {'name': 'replace', 'label': u'替换', 'cmd': '@replace', 'continue_on_fail': False},
    {'name': 'start', 'label': u'启服', 'cmd': '@start', 'continue_on_fail': False},
    {'name': 'health', 'label': u'健康检查', 'cmd': '@health', 'continue_on_fail': False},
]
BUILTIN_ACTIONS = ('upload', 'backup', 'stop', 'replace', 'start', 'health')
MAX_CUSTOM_STEPS = 16
STEP_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,32}$')

# 步骤链模板：前端「载入模板」一键填充。script_chain 即现网常见的
# 「拉包→停服→备份→发布前→解包→环境配置→发布后→启服→健康检查」九步流水线。
PRESETS = [
    {
        'name': 'script_chain',
        'label': u'脚本链发布（拉包→停服→备份→发布前后脚本→启服）',
        'vars': {
            'src_host': 'xpe_138',
            'pkg_dir': 'deploy/package-ccbh/zip_for_ccbh_end/CPMJW',
            'pkg_prefix': 'N-CPMCT_CPMJW',
            'preconf_dir': '~/test/wuxile/ccbh',
        },
        'param_defs': [
            {'key': 'Version_All', 'label': u'版本号(如 3.2.0)', 'default': '', 'required': True},
            {'key': 'NPkg', 'label': u'包序号(如 01)', 'default': '', 'required': True},
        ],
        'steps': [
            {'name': 'fetch', 'label': u'拉取版本包',
             'cmd': 'rm -rf ~/app_temp/ && mkdir -p ~/app_temp/ && '
                    'scp ${src_host}:~/${pkg_dir}/${pkg_prefix}* ~/app_temp/'},
            {'name': 'stop', 'label': u'应用停止', 'cmd': 'cd ~/bin/dist_shell/ && sh app_stop.sh'},
            {'name': 'backup', 'label': u'自定义备份', 'cmd': 'cd ~/bin/dist_shell/ && sh version_backup.sh'},
            {'name': 'before', 'label': u'发布前脚本(清理jar)',
             'cmd': 'cd ~/bin/dist_shell/ && sh update_before_dist.sh'},
            {'name': 'release', 'label': u'版本发布(解包)',
             'cmd': 'tar xf ~/app_temp/${pkg_prefix}*_${Version_All}-${NPkg}.tgz -C ~/'},
            {'name': 'preconf', 'label': u'升级前环境配置',
             'cmd': 'cd ${preconf_dir} && sh xpe_pre_install_ccbh.sh'},
            {'name': 'after', 'label': u'发布后应用升级',
             'cmd': 'cd ~/bin/dist_shell/ && sh update_after_dist.sh app'},
            {'name': 'start', 'label': u'应用启动', 'cmd': 'cd ~/bin/dist_shell/ && sh app_start.sh'},
            {'name': 'health', 'label': u'健康检查', 'cmd': '@health'},
        ],
    },
    {
        'name': 'standard',
        'label': u'标准发布（管理机推包→内置备份/替换/健康检查）',
        'vars': {},
        'steps': [dict(s) for s in STEP_DEFS],
    },
]
FINAL_STATES = ('success', 'failed', 'rolled_back', 'canceled')


class StepFailed(Exception):
    """流水线某一步失败，触发回滚。"""
    pass


# ---------------------------------------------------------------- 步骤链与变量
def normalize_steps(raw):
    """校验并规范化应用自定义步骤链，非法输入抛 ApiError(400)。

    每项 {name, label?, cmd, continue_on_fail?}；cmd 为内置动作关键字
    （可带 @ 前缀：@upload/@backup/@stop/@replace/@start/@health）时执行
    内置动作，其余内容作为 shell 命令在目标机上原样执行（先做 ${var} 替换）。
    """
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise ApiError(u'steps 必须是数组', 400)
    if len(raw) > MAX_CUSTOM_STEPS:
        raise ApiError(u'步骤数不能超过 %d' % MAX_CUSTOM_STEPS, 400)
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ApiError(u'第 %d 个步骤必须是对象' % (i + 1), 400)
        name = compat.safe_str(item.get('name', '')).strip()
        if not STEP_NAME_RE.match(name):
            raise ApiError(u'第 %d 个步骤的 name 只能含字母/数字/_/-，长度 1~32' % (i + 1), 400)
        cmd = compat.safe_str(item.get('cmd', '')).strip()
        if not cmd:
            raise ApiError(u'步骤 %s 缺少 cmd' % name, 400)
        label = compat.safe_str(item.get('label', '')).strip() or name
        out.append({
            'name': name,
            'label': label,
            'cmd': cmd,
            'continue_on_fail': bool(item.get('continue_on_fail')),
        })
    return out


def normalize_vars(raw):
    """校验应用级变量：仅接受字符串键值对（值统一转成文本）。"""
    if raw in (None, ''):
        return {}
    if not isinstance(raw, dict):
        raise ApiError(u'vars 必须是对象', 400)
    out = {}
    for k, v in raw.items():
        key = compat.safe_str(k).strip()
        if not key or len(key) > 64 or '${' in key:
            # 必须用 to_text：u'' 格式串里插字节串，py2 会按 ascii 隐式解码，
            # 中文变量名必崩（本应 400 的参数错误变成 500）。
            raise ApiError(u'变量名不合法: %s' % compat.to_text(k)[:64], 400)
        out[key] = compat.safe_str(v)
    return out


MAX_PARAM_DEFS = 16
PARAM_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
# 发布参数值的安全字符集（会被 subst 拼进目标机命令，须阻断 ; | & ` $ ( ) < > 引号 与换行）
PARAM_VALUE_RE = re.compile(r'^[A-Za-z0-9._/@:+=,~ -]{0,256}$')


def normalize_param_defs(raw):
    """校验应用参数定义：[{key, label?, default?, required?}]，供创建发布单时填值。

    与 vars 的区别：vars 是随应用固定的「常量」；param_defs 定义的是每次发布
    都可能变化的「参数」（如版本号、包序号），创建发布单时逐单填写，执行时
    覆盖同名 vars 注入到所有步骤命令的 ${key} 占位符。
    """
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise ApiError(u'param_defs 必须是数组', 400)
    if len(raw) > MAX_PARAM_DEFS:
        raise ApiError(u'参数定义不能超过 %d 个' % MAX_PARAM_DEFS, 400)
    out = []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ApiError(u'第 %d 个参数定义必须是对象' % (i + 1), 400)
        key = compat.safe_str(item.get('key', '')).strip()
        if not PARAM_KEY_RE.match(key):
            raise ApiError(u'第 %d 个参数的 key 只能是字母开头的字母/数字/下划线（≤64 字符）' % (i + 1), 400)
        if key in seen:
            raise ApiError(u'参数 key 重复: %s' % key, 400)
        seen.add(key)
        out.append({
            'key': key,
            'label': compat.safe_str(item.get('label', '')).strip() or key,
            'default': compat.safe_str(item.get('default', '')),
            'required': bool(item.get('required')),
        })
    return out


def validate_order_params(app, raw_params):
    """按应用参数定义校验发布单参数：未知键拒绝、必填缺失拒绝，返回规范化 dict。"""
    defs = app.get('param_defs') or []
    if raw_params in (None, ''):
        params = {}
    elif isinstance(raw_params, dict):
        params = dict(raw_params)
    else:
        raise ApiError(u'params 必须是对象', 400)
    def_keys = set(d['key'] for d in defs)
    for k in params:
        if k not in def_keys:
            raise ApiError(u'未知参数: %s（应用的参数定义为 %s）'
                           % (compat.to_text(k)[:64],
                              u', '.join(compat.to_text(x) for x in sorted(def_keys)) or u'无'), 400)
    out = {}
    for d in defs:
        val = compat.safe_str(params.get(d['key'], d.get('default', ''))).strip()
        if d.get('required') and not val:
            raise ApiError(u'缺少必填参数: %s（%s）' % (compat.to_text(d['key']), compat.to_text(d['label'])), 400)
        if val and not PARAM_VALUE_RE.match(val):
            # 发布参数会被 subst() 原样拼进目标机命令，必须限制在安全字符集内，
            # 否则 `ver=3.2.0; rm -rf ~/data` 之类可直接注入任意命令。
            raise ApiError(u'参数 %s 含非法字符（仅允许字母/数字/._-/@:+=,~ 与空格）'
                           % compat.to_text(d['key']), 400)
        out[d['key']] = val
    return out


def app_step_defs(app):
    """应用自定义步骤链；未配置（或历史数据为空）时回落到默认六步。"""
    try:
        steps = normalize_steps(app.get('steps'))
    except ApiError:
        steps = []
    if steps:
        return steps
    return [dict(s) for s in STEP_DEFS]


# ---------------------------------------------------------------- 路径与存储
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def make_release_dir(data_dir):
    rd = data_dir
    compat.makedirs(rd)
    compat.makedirs(os.path.join(rd, 'packages'))
    return rd


# 以下存储对象在 main() 里依据 --data-dir 初始化（模块级占位，便于路由闭包引用）
APPS_FILE = None
SERVERS_FILE = None
ORDERS_FILE = None
PACKAGES_FILE = None
SETTINGS_FILE = None
AUDIT_FILE = None
TASKS_FILE = None
PACKAGES_DIR = None

apps = None          # JsonStore
servers = None       # JsonStore
orders = None        # JsonStore
packages = None      # JsonStore（制品包元数据索引）
settings = None      # JsonStore
audit_log = None     # AuditLog
tm = None            # TaskManager

# 每个发布单的事件历史（用于 SSE 重放），与事件总线互补
ORDER_EVENTS = {}
ORDER_EVENTS_LOCK = compat.threading.Lock()
ORDER_LOCK = compat.threading.Lock()
# 制品包名分配 + 临时文件转正需要互斥：批量上传是多请求并发打进来的，
# 两个请求可能同时选中同一个「重名后缀」，先到者 rename 成功后后者会覆盖前者的包。
PACKAGES_LOCK = compat.threading.Lock()


# ---------------------------------------------------------------- 小工具
def sh_quote(text):
    """给 shell 命令参数加单引号（兼容含空格路径）。"""
    text = compat.to_text(text)
    return "'" + text.replace("'", "'\\''") + "'"


def _remove_quietly(path):
    """删除文件，失败不抛异常（清理上传临时文件用）。

    py2 下文件类错误是 IOError、py3 是 OSError，所以统一兜 Exception，
    避免漏掉其中一支导致「失败在清理环节再炸一次」。
    """
    try:
        os.remove(path)
    except Exception:
        pass


def join_url_path(base, rel):
    base = compat.to_text(base).rstrip('/')
    rel = compat.to_text(rel).lstrip('/')
    return base + '/' + rel


def join_remote(a, b):
    """用正斜杠拼接远端路径，避免 Windows 反斜杠污染目标机命令。"""
    a = compat.to_text(a).rstrip('/')
    b = compat.to_text(b).lstrip('/')
    return a + '/' + b


def safe_import_url(url):
    """校验「从平台导入」的地址，阻断非 http(s) 与云元数据地址（SSRF 收敛）。

    完整的 SSRF 防护会与本工具「在局域网内拉取平台列表」的用途冲突，
    这里至少挡掉最危险的云实例元数据地址与非常规协议。
    """
    url = compat.to_text(url).strip()
    parsed = compat.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ApiError('仅支持 http/https 地址', 400)
    host = (parsed.hostname or '').lower()
    if not host:
        raise ApiError('平台地址不合法', 400)
    if host.startswith('169.254.') or host in ('metadata.google.internal',):
        raise ApiError('不允许访问云元数据地址', 400)
    return url


def subst(mapping, text):
    """替换命令模板里的 ${key}。"""
    text = compat.to_text(text)
    for key, value in (mapping or {}).items():
        text = text.replace('${%s}' % key, compat.to_text(value))
    return text


def to_float(value, default=0.0):
    """把值安全转成浮点（兼容两版本）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_md5(stdout):
    """从 md5sum/sha256sum 输出里取哈希串。

    GNU coreutils 的 md5sum 在「文件名含反斜杠或换行」时会按转义约定在整行行首加一个
    `\\`（并把文件名里的反斜杠写成 `\\\\`）——Windows 目标机上的远端路径必然触发，
    于是第一个 token 变成 `\\7a5a...`，直接和本地摘要比较永远不等，发布单会在
    「远端 MD5 校验」这一步白白失败。这里统一剥掉转义标记，只保留十六进制字符。
    """
    line = compat.to_text(stdout).strip()
    if not line:
        return ''
    token = line.split()[0].strip().lower().lstrip('\\')
    # 只保留十六进制字符：兼容 `MD5 (x) = abc...`、`abc... *file` 等各家格式差异
    match = re.search(r'[0-9a-f]{16,}', token)
    if match:
        return match.group(0)
    return re.sub(r'[^0-9a-f]', '', token)


def load_settings():
    data = settings.load()
    out = {
        'platform_url': data.get('platform_url', ''),
        'platform_token': data.get('platform_token', ''),
        'release_concurrency': compat.to_int(data.get('release_concurrency'), 4, 1, 32),
        'cmd_timeout': compat.to_int(data.get('cmd_timeout'), 120, 1, 600),
        'backup_keep_default': compat.to_int(data.get('backup_keep_default'), 5, 0, 999),
    }
    return out


def save_settings(data):
    def _mut(d):
        d.update(data)
        return d
    settings.update(_mut)


def get_app(name):
    for a in apps.load().get('apps', []):
        if a.get('name') == name:
            return a
    return None


def get_server(name):
    for s in servers.load().get('servers', []):
        if s.get('name') == name:
            return s
    return None


def load_order(oid):
    for o in orders.load().get('orders', []):
        if o.get('id') == oid:
            return o
    return None


def server_of(run):
    """由 run 记录还原目标机条目（找不到时用名字兜底）。"""
    name = run.get('server', '')
    return get_server(name) or {'name': name, 'host': name}


def package_names():
    return [p.get('name') for p in packages.load().get('items', [])]


def get_package_md5(name):
    for p in packages.load().get('items', []):
        if p.get('name') == name:
            return p.get('md5', '')
    return ''


def find_package_by_md5(digest, size=None):
    """按内容查重：返回同 MD5（给了 size 则同时比对体积）的已有制品包，没有则 None。

    用于「上传目录 / 批量拖拽」场景：同一个包被重复拖进来时直接复用已有条目，
    不再产生 a.tgz / a_1.tgz / a_2.tgz 一串内容完全相同的副本。
    """
    digest = compat.safe_str(digest or '').strip().lower()
    if not digest:
        return None
    for p in packages.load().get('items', []):
        if compat.safe_str(p.get('md5', '')).lower() != digest:
            continue
        if size is not None and compat.to_int(p.get('size'), -1) != compat.to_int(size, -2):
            continue
        return p
    return None


def alloc_package_name(filename):
    """在制品包目录里分配一个不冲突的文件名（重名时追加 _1 / _2…）。

    调用方必须持有 PACKAGES_LOCK：分配与 rename 之间不能有别的请求插进来，
    否则两个并发上传会拿到同一个名字。
    """
    name = os.path.basename(compat.safe_str(filename).strip().replace('\\', '/'))
    if not name or name in ('.', '..'):
        raise ApiError('文件名非法', 400)
    dest = os.path.join(PACKAGES_DIR, name)
    if not os.path.exists(dest):
        return name
    base, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(PACKAGES_DIR, '%s_%d%s' % (base, i, ext))):
        i += 1
    return '%s_%d%s' % (base, i, ext)


def order_packages(order):
    """取发布单挂的制品包列表（兼容只有单个 package 的旧发布单）。"""
    items = []
    for raw in (order.get('packages') or []):
        s = compat.safe_str(raw).strip()
        if s and s not in items:
            items.append(s)
    single = compat.safe_str(order.get('package') or '').strip()
    if single and single not in items:
        items.insert(0, single)
    return items


def normalize_order_packages(data):
    """从发布单入参里归一化制品包列表（新字段 packages + 兼容旧字段 package）。"""
    raw = data.get('packages')
    if isinstance(raw, compat.string_types):
        raw = raw.replace('\n', ',').split(',')
    items = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            s = compat.safe_str(x).strip()
            if s and s not in items:
                items.append(s)
    single = compat.safe_str(data.get('package', '')).strip()
    if single and single not in items:
        items.insert(0, single)
    if len(items) > MAX_ORDER_PACKAGES:
        raise ApiError('单个发布单最多挂 %d 个制品包' % MAX_ORDER_PACKAGES, 400)
    return items


def save_order(order):
    oid = order.get('id')

    def _mut(d):
        lst = d.setdefault('orders', [])
        for i, o in enumerate(lst):
            if o.get('id') == oid:
                lst[i] = order
                return d
        lst.append(order)
        return d
    orders.update(_mut)


def append_run(order, run):
    """把单台服务器的执行结果合并进发布单（线程安全）。"""
    with ORDER_LOCK:
        lst = order.setdefault('server_steps', [])
        for i, r in enumerate(lst):
            if r.get('server') == run.get('server'):
                lst[i] = run
                save_order(order)
                return
        lst.append(run)
        save_order(order)


def new_run(server_name, step_defs=None):
    defs = step_defs or STEP_DEFS
    return {
        'server': server_name,
        'state': 'pending',
        'backup_path': '',
        'remote_package': '',
        'remote_packages': [],
        'steps': [{'name': d['name'], 'label': d['label'], 'state': 'pending', 'message': '',
                   'elapsed': 0.0, 'logs': []} for d in defs],
    }


def order_public(order):
    """去除内部字段后返回发布单。"""
    out = dict(order)
    out.pop('_rollback_ok', None)
    for run in out.get('server_steps', []):
        run.pop('_rollback_ok', None)
    return out


# ---------------------------------------------------------------- SSE / 平台联动
def emit(order_id, event):
    event = dict(event)
    event.setdefault('ts', time.time())
    with ORDER_EVENTS_LOCK:
        lst = ORDER_EVENTS.setdefault(order_id, [])
        lst.append(event)
        if len(lst) > 2000:
            lst[:] = lst[-2000:]
    sse.BUS.publish('release:' + order_id, event)
    sse.BUS.publish('release', dict(event, order_id=order_id))


def step_log(order, run, step, text):
    """向步骤追加一条日志，并把日志增量发布到 SSE（主题 release:<id> 与 release）。"""
    line = compat.safe_str(text)
    step['logs'].append(line)
    emit(order['id'], {'type': 'log', 'order_id': order['id'], 'server': run.get('server', ''),
                       'step': step.get('name', ''), 'text': line})


def push_to_platform(order, step, state, percent):
    """把发布进度推给平台（失败静默重试一次，不影响发布本身）。"""
    cfg = load_settings()
    url = cfg.get('platform_url')
    token = cfg.get('platform_token')
    if not url:
        return
    body = {
        'topic': 'release',
        'data': {
            'order_id': order.get('id'),
            'app': order.get('app'),
            'version': order.get('version'),
            'state': state,
            'step': step,
            'percent': percent,
        },
    }
    target = url.rstrip('/') + '/api/events/push'
    headers = {'X-Auth-Token': token} if token else {}

    def _post():
        try:
            resp = httpx.DEFAULT.post(target, json_body=body, headers=headers, timeout=5)
            return resp.ok
        except Exception:
            return False
    if not _post():
        _post()


# ---------------------------------------------------------------- 目标机调用
def exec_on_target(server, cmd, timeout):
    """调用目标机 web_shell 的 /api/exec，返回 {code,stdout,stderr,duration,timed_out}。"""
    host = server.get('host', '127.0.0.1')
    port = compat.to_int(server.get('shell_port'), 8088, 1, 65535)
    # 访问 web_shell 必须用 shell_token；upload_token 仅用于 upload_server，
    # 两者不同时若取错会直接 401。
    token = server.get('shell_token') or server.get('upload_token') or ''
    url = httpx.join_url(host, port, '/api/exec')
    headers = {'X-Auth-Token': token}
    try:
        resp = httpx.DEFAULT.post(url, json_body={'cmd': cmd, 'timeout': timeout},
                                  headers=headers, timeout=max(timeout, 10) + 10)
    except Exception as e:
        return {'code': -1, 'stdout': '', 'stderr': compat.safe_str(e), 'duration': 0, 'timed_out': False}
    if not resp.ok:
        return {'code': -1, 'stdout': '', 'stderr': 'HTTP %d' % resp.status, 'duration': 0, 'timed_out': False}
    data = resp.json() or {}
    return {
        'code': compat.to_int(data.get('code'), -1),
        'stdout': data.get('stdout', ''),
        'stderr': data.get('stderr', ''),
        'duration': to_float(data.get('duration'), 0.0),
        'timed_out': bool(data.get('timed_out')),
    }


class StreamLogger(object):
    """把脚本输出攒成块后写进步骤日志（step_log 会立刻 emit 到页面）。

    为什么攒块而不是逐行推送：一条 `tar -xzvf` 能刷出上千行，逐行 emit 会把 SSE
    连接和前端 DOM 打满；但攒太久又失去「实时」的意义 —— 取「最多 STREAM_FLUSH_BYTES
    字节 或 最多 STREAM_FLUSH_SECONDS 秒」两者先到者，兼顾实时与负载。
    超过 STREAM_LOG_LIMIT 后停止回传正文（只留最终结果），避免单步日志无限增长。
    """

    def __init__(self, order, run, step):
        self.order = order
        self.run = run
        self.step = step
        self.buf = ''
        self.last = time.time()
        self.sent = 0
        self.muted = False

    def push(self, text):
        if not text or self.muted:
            return
        self.sent += len(text)
        if self.sent > STREAM_LOG_LIMIT:
            self.muted = True
            self.flush(force=True)
            step_log(self.order, self.run, self.step,
                     u'[输出超过 %s，后续内容不再回传显示，最终结果仍会记录]'
                     % compat.fmt_size(STREAM_LOG_LIMIT))
            return
        self.buf += text
        self.flush()

    def flush(self, force=False):
        if not self.buf:
            return
        now = time.time()
        if not force and len(self.buf) < STREAM_FLUSH_BYTES \
                and (now - self.last) < STREAM_FLUSH_SECONDS:
            return
        text, self.buf = self.buf, ''
        self.last = now
        step_log(self.order, self.run, self.step, text.rstrip('\n'))


def exec_on_target_stream(server, cmd, timeout, on_output=None):
    """流式执行目标机命令：脚本输出边收边回传，返回结构与 exec_on_target 一致。

    走目标机 web_shell 的 `/api/exec/stream`（SSE：start / out / exit）。
    老版本 web_shell 没有这个接口（404 / 连不上 / 没有 exit 事件）时**自动降级**
    回同步 `/api/exec`，返回值的 'streamed' 字段标明走的哪条路。

    为什么值得这么做：同步接口要等命令结束才拿到 stdout，页面上「这一步在跑什么」
    长时间是一片空白；流式之后每 0.25s 就有一批输出落进步骤日志并 emit 到浏览器，
    长命令（解包、重启、健康检查）的进度肉眼可见。
    """
    host = server.get('host', '127.0.0.1')
    port = compat.to_int(server.get('shell_port'), 8088, 1, 65535)
    token = server.get('shell_token') or server.get('upload_token') or ''
    tmo = compat.to_int(timeout, 120, 1, 86400)
    # 命令放**请求体**、token 走 X-Auth-Token：URI 恒为 /api/exec/stream，链路上不会
    # 出现 `?cmd=whoami` 这种被安全设备当作「webshell 命令执行」的特征串（现场态势
    # 感知误报就是这个）。web_shell v1.3.0+ 支持；老版本只有 GET 查询串形态 → 降级。
    url = httpx.join_url(host, port, '/api/exec/stream')
    legacy_url = url + '?cmd=%s&timeout=%d' % (compat.urlquote(cmd, safe=''), tmo)
    body = compat.to_text(compat.json_bytes({'cmd': cmd, 'timeout': tmo}))
    started = time.time()
    buf = []
    state = {'code': None, 'elapsed': None, 'timed_out': False}
    seen = {'any': False}

    def on_event(name, data):
        seen['any'] = True
        if name == 'out':
            buf.append(data)
            if on_output is not None:
                on_output(data)
        elif name == 'exit':
            info = compat.json_loads(data, {}) or {}
            state['code'] = compat.to_int(info.get('returncode'), -1)
            state['elapsed'] = to_float(info.get('elapsed'), None)
            state['timed_out'] = bool(info.get('timed_out'))

    ok, err = sse.consume(url, headers={'X-Auth-Token': token}, on_event=on_event,
                          timeout=tmo + 30, idle_timeout=tmo + 60,
                          method='POST', body=body)
    if not ok and not seen['any']:
        # 建连层就失败且一个事件都没收到（<200 的老 web_shell / 反向代理不支持
        # POST SSE）→ 用旧查询串形态再试一次。这里只在「没收到任何事件」时才重试，
        # 已经开跑的命令绝不重放（重启服务之类的步骤不能被执行两次）。
        ok, err = sse.consume(legacy_url, headers={'X-Auth-Token': token}, on_event=on_event,
                              timeout=tmo + 30, idle_timeout=tmo + 60)
    if (not ok or state['code'] is None) and not seen['any']:
        # 只有「一个事件都没收到」时才允许同步降级：只要上游已经推出过任何事件
        # （start/out/exit），命令就已经在目标机跑起来了，再调同步 /api/exec 会把它
        # 执行第二次 —— 对 @stop/@replace/@start 这类重启步骤就是生产事故。
        res = exec_on_target(server, cmd, timeout)
        text = compat.to_text(res.get('stdout') or '') + compat.to_text(res.get('stderr') or '')
        if on_output is not None and text:
            on_output(text)
        res['streamed'] = False
        if not ok:
            res['stream_error'] = err
        return res
    return {
        'code': state['code'] if state['code'] is not None else -1,
        'stdout': ''.join(buf),
        'stderr': '',
        'duration': state['elapsed'] if state['elapsed'] is not None
        else round(time.time() - started, 2),
        'timed_out': state['timed_out'],
        'streamed': True,
    }


def exec_cmd(order, run, step, cmd, settings):
    """执行一条目标机命令，**把脚本输出实时回传**到步骤日志与页面，非零即抛 StepFailed。

    日志形态（页面「执行控制台」直接按顺序展示）：
        命令: <cmd>                      ← 先告诉用户在跑什么
        <脚本输出原文，边跑边追加>        ← StreamLogger 分块推送
        --- 返回码: 0  耗时: 1.23s ---   ← 收尾汇总
    """
    step_log(order, run, step, u'$ ' + compat.to_text(cmd))
    logger = StreamLogger(order, run, step)
    res = exec_on_target_stream(server_of(run), cmd, settings.get('cmd_timeout', 120),
                                on_output=logger.push)
    logger.flush(force=True)
    step_log(order, run, step,
             u'--- 返回码: %s  耗时: %ss%s%s ---'
             % (res['code'], res['duration'],
                u'  [超时]' if res.get('timed_out') else u'',
                u'' if res.get('streamed') else u'  [同步模式]'))
    if res.get('timed_out'):
        raise StepFailed(u'命令执行超时')
    if res['code'] != 0:
        raise StepFailed(u'命令返回非零退出码 %s' % res['code'])
    return res


def upload_to_target(server, local_path, filename, settings, step, on_progress=None):
    """把本地制品包传到目标机 upload_server，返回远端绝对路径。

    on_progress(text) 用于把大包分片进度回传到步骤日志（页面上实时可见）。

    远端路径**优先取 upload_server 返回的真实落盘路径**（v3.2.3+ 的 `path`）：
    服务器记录的「远端上传目录 upload_dir」只是运维填的期望值，真正落地在哪由目标机
    upload_server 的 `--upload-dir` 决定（默认 ./uploads，相对**进程工作目录**）。
    两者不一致时，早期版本用「配置目录 + 文件名」拼路径，${package} 就指向一个不存在的
    文件，而 md5sum 取不到摘要又被静默放过 —— 直到某一步 tar 才炸，极难定位
    （用户报的「配了 /home/xpe/app_temp 却进了 uploads/」）。
    """
    host = server.get('host', '127.0.0.1')
    port = compat.to_int(server.get('upload_port'), 8089, 1, 65535)
    token = server.get('upload_token') or server.get('shell_token') or ''
    size = os.path.getsize(local_path)
    base = httpx.join_url(host, port, '')
    headers = {'X-Auth-Token': token}
    real_dir = ''
    # 页面配置的「远端上传目录」随请求带给目标机：v3.3.0+ 且开了 --allow-client-dir
    # 的 upload_server 会直接把文件落到这个目录（改配置即时生效，无需重启目标机服务）；
    # 老版本/未开关的目标机会忽略该参数，此时以响应里的真实落盘路径为准（见下方 served）。
    want_dir = compat.to_text(server.get('upload_dir') or '').strip()
    if size <= CHUNK_PUT_LIMIT:
        url = join_url_path(base, '/api/chunk/put?filename=%s&size=%d'
                            % (compat.urlquote(filename), size))
        if want_dir:
            url += '&dir=' + compat.urlquote(want_dir)
        try:
            with open(local_path, 'rb') as f:
                resp = httpx.DEFAULT.request('POST', url, headers=headers, src=f, src_len=size,
                                             timeout=UPLOAD_TIMEOUT)
        except Exception as e:
            raise StepFailed(u'上传请求异常: ' + compat.to_text(e))
        if not resp.ok:
            raise StepFailed(u'上传失败 HTTP %d' % resp.status)
        data = resp.json() or {}
        if data.get('status') != 'success':
            raise StepFailed(u'上传失败: ' + compat.to_text(data.get('message')))
        remote_name = data.get('filename') or filename
        real_dir = compat.to_text(data.get('dir') or '')
        served = compat.to_text(data.get('path') or '')
    else:
        remote_name = chunked_upload(server, local_path, filename, settings, step, base, headers,
                                     on_progress=on_progress)
        served = ''
    if not served and remote_name.startswith('/'):
        # 分片 complete 响应（v3.2.3+）直接给的是真实绝对路径 —— 不能再拿它去拼
        # 声明目录，否则会拼出「/声明目录/真实路径」这种嵌套怪路径（md5sum 取不到、
        # ${package} 指向不存在的文件）。
        served = remote_name
    if served:
        return served
    declared = server.get('upload_dir') or DEFAULT_UPLOAD_DIR
    if real_dir:
        # 老 upload_server 只回了文件名也没关系：它回了真实目录就用它
        return join_remote(real_dir, remote_name)
    return join_remote(declared, remote_name)


def probe_upload_dir(server):
    """探测目标机 upload_server 的**真实**存储根（GET /api/status 的 upload_dir）。

    用于在配置目录与真实目录不一致时给出可执行的提示，而不是让发布单在后面的步骤
    莫名其妙地失败。探测失败返回 ''（不影响发布流程）。
    """
    host = server.get('host', '127.0.0.1')
    port = compat.to_int(server.get('upload_port'), 8089, 1, 65535)
    try:
        resp = httpx.DEFAULT.get(httpx.join_url(host, port, '/api/status'), timeout=8)
        if not resp.ok:
            return ''
        data = resp.json() or {}
        return compat.to_text(data.get('upload_dir') or '')
    except Exception:                                          # noqa: BLE001
        return ''


def chunked_upload(server, local_path, filename, settings, step, base, headers, on_progress=None):
    """大包走分片：init → 逐片 upload → complete。每传完 1/4 回传一次进度。"""
    size = os.path.getsize(local_path)
    chunk_size = 4 * 1024 * 1024
    total = (size + chunk_size - 1) // chunk_size
    want_dir = compat.to_text(server.get('upload_dir') or '').strip()
    init_body = {'filename': filename, 'size': size,
                 'total_chunks': total, 'chunk_size': chunk_size}
    if want_dir:
        init_body['dir'] = want_dir
    init_url = join_url_path(base, '/api/chunk/init')
    resp = httpx.DEFAULT.post(init_url, json_body=init_body,
                              headers=headers, timeout=UPLOAD_TIMEOUT)
    if not resp.ok:
        raise StepFailed(u'分片初始化失败 HTTP %d' % resp.status)
    data = resp.json() or {}
    upload_id = data.get('upload_id')
    if not upload_id:
        raise StepFailed(u'分片初始化未返回 upload_id')
    if on_progress is not None:
        on_progress(u'分片上传：共 %d 片，每片 %s' % (total, compat.fmt_size(chunk_size)))
    mark = 0
    with open(local_path, 'rb') as f:
        for index in range(total):
            buf = f.read(chunk_size)
            if not buf:
                break
            up_url = join_url_path(base, '/api/chunk/upload')
            h = dict(headers)
            h['X-Upload-Id'] = compat.safe_str(upload_id)
            h['X-Chunk-Index'] = compat.safe_str(index)
            r = httpx.DEFAULT.request('POST', up_url, headers=h, body=buf, timeout=UPLOAD_TIMEOUT)
            if not r.ok:
                raise StepFailed(u'分片 %d 上传失败 HTTP %d' % (index, r.status))
            percent = (index + 1) * 100 // total
            if on_progress is not None and percent >= mark + 25:
                mark = percent // 25 * 25
                on_progress(u'上传进度 %d%%（%s / %s）'
                            % (percent, compat.fmt_size(min(size, (index + 1) * chunk_size)),
                               compat.fmt_size(size)))
    comp_url = join_url_path(base, '/api/chunk/complete')
    comp_body = {'upload_id': upload_id, 'total_chunks': total,
                 'size': size, 'filename': filename}
    if want_dir:
        comp_body['dir'] = want_dir
    r = httpx.DEFAULT.post(comp_url, json_body=comp_body,
                           headers=headers, timeout=UPLOAD_TIMEOUT)
    if not r.ok:
        raise StepFailed(u'分片合并失败 HTTP %d' % r.status)
    data = r.json() or {}
    if data.get('status') != 'success':
        raise StepFailed(u'分片合并失败: ' + compat.to_text(data.get('message')))
    # v3.2.3+ 的 upload_server 会在 complete 响应里给出真实落盘路径，优先采用
    return compat.to_text(data.get('path') or data.get('filename') or filename)


# ---------------------------------------------------------------- 健康检查（直连）
def health_port(host, port, timeout):
    try:
        sock = socket.create_connection((host, port), timeout=max(1, min(timeout, 10)))
        sock.close()
        return True, u'端口 %s:%d 连通' % (compat.to_text(host), port)
    except Exception as e:
        return False, compat.safe_str(e)


def health_http(url, timeout):
    try:
        resp = httpx.DEFAULT.get(url, timeout=max(1, min(timeout, 10)))
        if 200 <= resp.status < 400:
            return True, u'HTTP %d' % resp.status
        return False, u'HTTP %d' % resp.status
    except Exception as e:
        return False, compat.safe_str(e)


# ---------------------------------------------------------------- 各步骤实现
def step_upload(app, server, order, run, step, settings, mapping):
    """把发布单挂的制品包逐个推到目标机，返回远端路径列表（顺序与发布单一致）。

    一个发布单可以挂多个包（order.packages）：有的现场是「主程序包 + 配置包」
    或「版本包 + 补丁包」一起发，因此这里改成循环上传，任一包失败即整步失败
    （照旧触发回滚）。返回值保持列表，调用方据此注入 ${package}/${packages}。
    """
    pkgs = order_packages(order)
    if not pkgs:
        raise StepFailed(u'发布单未指定制品包')
    remotes = []
    for pkg in pkgs:
        local = os.path.join(PACKAGES_DIR, pkg)
        if not os.path.exists(local):
            raise StepFailed(u'本地制品包不存在: ' + compat.to_text(pkg))
        size = os.path.getsize(local)
        step_log(order, run, step, u'开始上传 %s（%s）' % (
            compat.to_text(pkg), compat.fmt_size(size)))
        # 大包分片走 upload_to_target 的进度回调，页面上能看到「传到百分之几」
        remote_path = upload_to_target(server, local, pkg, settings, step,
                                       on_progress=lambda text: step_log(order, run, step, text))
        step_log(order, run, step, u'已上传 %s -> %s' % (compat.to_text(pkg),
                                                      compat.to_text(remote_path)))
        if not remotes:
            # 第一个包落地后核对一次：声明的 upload_dir 与真实落盘目录不一致时，
            # 发布本身用的是真实路径（能跑通），但用户常常以为文件在声明目录下找不到。
            declared = compat.to_text(server.get('upload_dir') or DEFAULT_UPLOAD_DIR)
            actual_dir = compat.to_text(remote_path).rsplit('/', 1)[0]
            if declared.rstrip('/') != actual_dir.rstrip('/'):
                step_log(order, run, step,
                         u'注意：服务器配置的远端上传目录是 %s，但目标机 upload_server '
                         u'实际落盘在 %s。发布流程按真实路径执行不受影响。若要求落到 %s：'
                         u'① 推荐 —— 目标机 upload_server 升级到 v3.3.0+ 并以 '
                         u'--allow-client-dir 启动，此后页面配置的目录即时生效、改目录'
                         u'无需再重启；② 或者在目标机用 --upload-dir %s 重启 '
                         u'upload_server。'
                         % (declared, actual_dir, declared, declared))
        local_md5 = get_package_md5(pkg)
        if local_md5:
            res = exec_on_target(server, 'md5sum ' + sh_quote(remote_path),
                                 settings.get('cmd_timeout', 120))
            remote_md5 = parse_md5(res.get('stdout'))
            step_log(order, run, step, u'远端 MD5 校验(%s): 本地 %s / 远端 %s%s' % (
                compat.to_text(pkg), local_md5, remote_md5 or u'(未取到)',
                u'  一致' if remote_md5 and remote_md5.lower() == local_md5.lower() else u''))
            if remote_md5 and remote_md5.lower() != local_md5.lower():
                raise StepFailed(u'远端 MD5 校验不一致（%s：本地 %s / 远端 %s）'
                                 % (compat.to_text(pkg), local_md5, remote_md5))
        remotes.append(remote_path)
    if len(remotes) == 1:
        step['message'] = u'已上传至 ' + compat.to_text(remotes[0])
    else:
        step['message'] = u'已上传 %d 个包至 %s' % (
            len(remotes),
            compat.to_text(server.get('upload_dir') or DEFAULT_UPLOAD_DIR))
    return remotes


def package_vars(remotes, os_kind):
    """把远端制品包路径列表转成步骤命令可用的模板变量。

    - ``${package}``  第一个包的路径 —— 兼容只发单包的历史应用定义与模板；
    - ``${packages}`` 全部包的路径，空格分隔（已按目标机系统换算分隔符）；
    - ``${package1}``…``${packageN}`` 按顺序单独引用（模板里要逐个点名时用）；
    - ``${package_count}`` 包数量。
    """
    paths = [remote_path(p, os_kind) for p in (remotes or [])]
    out = {'package': paths[0] if paths else '',
           'packages': ' '.join(paths),
           'package_count': len(paths)}
    for i, p in enumerate(paths):
        out['package%d' % (i + 1)] = p
    return out


# ---------------------------------------------------------------- 目标机系统识别
_TARGET_OS = {}


def target_os(server, settings=None):
    """识别目标机是 posix 还是 windows（按服务器缓存，只探测一次）。

    探测命令 `echo %OS%`：Windows 的 cmd 会展开为 Windows_NT；POSIX shell
    则原样回显 "%OS%" —— 一条命令同时适配两种系统，据此区分。
    识别失败按 posix 处理（麒麟 / Linux 为默认目标环境）。
    """
    key = server.get('name') or server.get('host') or '?'
    if key in _TARGET_OS:
        return _TARGET_OS[key]
    kind = 'posix'
    try:
        res = exec_on_target(server, 'echo %OS%', 15)
        if 'Windows_NT' in compat.to_text(res.get('stdout') or ''):
            kind = 'windows'
    except Exception:
        kind = 'posix'
    _TARGET_OS[key] = kind
    return kind


def win_path(path):
    """Windows 命令中的路径：统一反斜杠并加双引号（兼容含空格路径）。"""
    return '"' + compat.to_text(path).replace('/', '\\') + '"'


def remote_path(path, os_kind):
    """把远端路径转成目标机习惯的分隔符（供 ${package} 等模板变量注入）。

    POSIX 用正斜杠（保持原行为），Windows 用反斜杠 —— cmd 的 copy/robocopy
    等内置命令不认含正斜杠的路径。
    """
    text = compat.to_text(path)
    return text.replace('/', '\\') if os_kind == 'windows' else text


def step_backup(app, server, order, run, step, settings):
    backup_dir = app.get('backup_dir', '')
    target_dir = app.get('target_dir', '')
    keep = compat.to_int(app.get('backup_keep'), settings.get('backup_keep_default', 5), 0, 999)
    if keep < 1:
        keep = 1
    ts = time.strftime('%Y%m%d_%H%M%S')
    bak_dir = join_remote(backup_dir, ts)
    if target_os(server, settings) == 'windows':
        # Windows 目标机：robocopy 整目录镜像（退出码 < 8 视为成功），
        # 现网目录不存在时视为「首次发布、无需备份」。
        #
        # 每个 if 必须各自加括号再拼 `&`：cmd 里 `if cond cmd1 & cmd2` 的
        # cmd1/cmd2 **整行都属于这个 if 的语句体**，条件为假时后面的命令一并被跳过。
        # 二次发布时 backup_dir 已存在 → 旧写法会静默「不备份」，回滚时才发现无备份可用。
        cmd = ('(if not exist {b} mkdir {b}) & (if not exist {k} mkdir {k})'
               ' & (if exist {t} (robocopy {t} {k} /E /NFL /NDL /NJH /NJS /NP >NUL'
               ' & if errorlevel 8 exit /b 1))'
               # 链尾必须显式 exit /b 0：robocopy 的「成功」码是 1~7（非 0），
               # 若不重置，整条 `&` 链会以 1~7 作为进程退出码 → 备份成功却被判失败、
               # 触发无谓回滚。errorlevel>=8 时上面的 exit /b 1 已先行退出，不受影响。
               ' & exit /b 0').format(
            b=win_path(backup_dir), k=win_path(bak_dir), t=win_path(target_dir))
        exec_cmd(order, run, step, cmd, settings)
        # 清理最旧备份：尽力而为，失败只告警、不影响本次发布
        clean = ('for /f "skip=' + str(keep) + ' delims=" %d in (\'dir /b /ad /o-d '
                 + win_path(backup_dir) + ' 2^>nul\') do @rd /s /q '
                 + win_path(backup_dir) + '\\%d')
        res = exec_on_target(server, clean, settings.get('cmd_timeout', 120))
        if res.get('code') != 0:
            step_log(order, run, step,
                     u'旧备份清理未完成（不影响发布）: %s' % compat.to_text(res.get('stderr') or ''))
    else:
        # 不再用 `cp ... 2>/dev/null || true` 吞掉备份失败：那会让「备份为空/残缺」被记成
        # 成功，等真正需要回滚时才发现没有可用备份。仅「目标目录不存在」（首次发布）视为
        # 无需备份；其余 cp 失败一律 exit 1 → 本步失败，交回滚逻辑。
        cmd = ("mkdir -p %s; mkdir -p %s; "
               "if [ -d %s ]; then cp -a %s/. %s/ || { echo '备份失败，终止发布'; exit 1; }; "
               "else echo '(目标目录不存在，首次发布无需备份)'; fi") % (
            sh_quote(backup_dir), sh_quote(bak_dir), sh_quote(target_dir),
            sh_quote(target_dir), sh_quote(bak_dir))
        cmd += " && ls -1t %s 2>/dev/null | tail -n +%d | while read d; do rm -rf %s/$d; done" % (
            sh_quote(backup_dir), keep + 1, sh_quote(backup_dir))
        exec_cmd(order, run, step, cmd, settings)
    run['backup_path'] = bak_dir
    step_log(order, run, step, u'备份路径: ' + compat.to_text(bak_dir))
    step['message'] = u'备份至 ' + compat.to_text(bak_dir)


def step_named_cmd(server, cmd, order, run, step, settings, mapping, label):
    if not cmd or not compat.to_text(cmd).strip():
        step['message'] = u'未配置%s，跳过' % compat.to_text(label)
        step['state'] = 'success'
        return
    exec_cmd(order, run, step, subst(mapping, cmd), settings)


def step_health(app, server, order, run, step, settings, mapping, ctx):
    htype = app.get('health_type', 'none')
    target = app.get('health_target', '')
    retry = max(1, compat.to_int(app.get('health_retry'), 10, 1, 9999))
    interval = to_float(app.get('health_interval'), 3.0)
    if htype == 'none' or not compat.to_text(target).strip():
        step['message'] = u'未配置健康检查，跳过'
        step['state'] = 'success'
        return
    ok_h = False
    last_msg = ''
    for attempt in range(retry):
        if ctx is not None and ctx.canceled():
            break
        try:
            if htype == 'port':
                ok_h, last_msg = health_port(server.get('host', '127.0.0.1'),
                                             compat.to_int(target, 0), interval)
            elif htype == 'http':
                ok_h, last_msg = health_http(subst(mapping, target), interval)
            elif htype == 'cmd':
                res = exec_on_target(server, subst(mapping, target), settings.get('cmd_timeout', 120))
                ok_h = (res['code'] == 0)
                last_msg = u'退出码 %s' % res['code']
            else:
                ok_h, last_msg = True, u'未知类型'
        except Exception as e:
            ok_h, last_msg = False, compat.safe_str(e)
        step_log(order, run, step, u'第 %d 次检查: %s' % (attempt + 1, last_msg))
        if ok_h:
            break
        if attempt < retry - 1:
            time.sleep(interval)
    if not ok_h:
        raise StepFailed(u'健康检查失败: ' + compat.to_text(last_msg))
    step['message'] = u'健康检查通过'


def do_rollback(app, order, run, settings, mapping):
    """回滚单台服务器：恢复最近备份 → 启服 → 再健康检查。幂等：无备份时报「无可回滚备份」。"""
    server = server_of(run)
    rstep = {'name': 'rollback', 'label': u'回滚', 'state': 'running', 'message': '',
             'elapsed': 0.0, 'logs': []}
    run['steps'].append(rstep)
    started = time.time()
    try:
        if not run.get('backup_path'):
            raise StepFailed(u'无可回滚备份')
        emit(order['id'], {'type': 'step', 'order_id': order['id'], 'server': run.get('server', ''),
                           'step': 'rollback', 'state': 'running', 'message': u'回滚中'})
        push_to_platform(order, 'rollback', 'running', 0)
        if app.get('rollback_cmd'):
            exec_cmd(order, run, rstep, subst(mapping, app.get('rollback_cmd')), settings)
        elif target_os(server, settings) == 'windows':
            target_dir = app.get('target_dir', '')
            # 同样必须给 if 加括号：回滚时 target_dir 通常**已存在**，
            # 旧写法 `if not exist {t} mkdir {t} & robocopy ...` 会被整行跳过，
            # 表现为「回滚显示成功、文件其实没还原」。
            exec_cmd(order, run, rstep,
                     '(if not exist {t} mkdir {t}) & (robocopy {k} {t} /E /NFL /NDL /NJH /NJS /NP >NUL'
                     ' & if errorlevel 8 exit /b 1)'.format(
                         t=win_path(target_dir), k=win_path(run['backup_path'])),
                     settings)
        else:
            exec_cmd(order, run, rstep,
                     "cp -a %s/. %s/" % (sh_quote(run['backup_path']), sh_quote(app.get('target_dir', ''))),
                     settings)
        if app.get('start_cmd'):
            exec_cmd(order, run, rstep, subst(mapping, app.get('start_cmd')), settings)
        try:
            step_health(app, server, order, run, rstep, settings, mapping, None)
        except StepFailed:
            step_log(order, run, rstep, u'回滚后健康检查未通过（已尽力恢复）')
        rstep['state'] = 'success'
        rstep['message'] = u'已回滚'
        run['_rollback_ok'] = True
    except StepFailed as e:
        rstep['state'] = 'failed'
        rstep['message'] = compat.safe_str(e)
        run['_rollback_ok'] = False
    rstep['elapsed'] = round(time.time() - started, 2)
    emit(order['id'], {'type': 'step', 'order_id': order['id'], 'server': run.get('server', ''),
                       'step': 'rollback', 'state': rstep['state'], 'message': rstep['message']})
    push_to_platform(order, 'rollback', rstep['state'], 100 if rstep['state'] == 'success' else 0)
    return rstep


def emit_step(order, run, name, state, message, percent, index=0, total=0,
              label='', elapsed=0.0):
    """推一条步骤状态事件。

    页面靠它知道「现在执行到哪一步」：index/total 给出「第 x/y 步」，label 给中文名，
    percent 给整单进度条，elapsed 给耗时；正文输出则由 step_log 的 log 事件推送。
    """
    emit(order.get('id'), {
        'type': 'step', 'order_id': order.get('id'), 'server': run.get('server', ''),
        'step': name, 'label': label, 'state': state,
        'message': compat.safe_str(message), 'percent': percent,
        'index': index, 'total': total, 'elapsed': elapsed, 'ts': time.time(),
    })


def run_server_pipeline(app, server, order, run, ctx, settings, done, total_steps,
                        step_defs=None):
    """对单台服务器执行步骤链，失败则自动回滚。

    步骤链可由应用自定义（app.steps）：cmd 为内置动作关键字（@upload 等）
    时执行内置动作，否则作为 shell 命令在目标机执行（先做 ${var} 替换，
    变量来源 = 应用 vars + 发布单内置变量）。某一步配置了 continue_on_fail
    时失败不中断，只标记该步失败并继续。
    """
    step_defs = step_defs or STEP_DEFS
    os_kind = target_os(server, settings)
    app_vars = app.get('vars')
    mapping = dict(app_vars if isinstance(app_vars, dict) else {})
    order_params = order.get('params')
    if isinstance(order_params, dict):
        mapping.update(order_params)   # 发布单级参数覆盖应用同名 vars
    mapping.update({
        'version': order.get('version', ''),
        # 路径按目标机系统的分隔符注入，保证用户写的命令模板在两种系统上都能直接用
        'target_dir': remote_path(app.get('target_dir', ''), os_kind),
        'backup_dir': remote_path(app.get('backup_dir', ''), os_kind),
        'app': app.get('name', ''),
    })
    for idx, sdef in enumerate(step_defs):
        if ctx is not None and ctx.canceled():
            run['state'] = 'canceled'
            return
        step = run['steps'][idx]
        step['state'] = 'running'
        step['message'] = u'执行中'
        save_order(order)
        percent = done[0] * 100 // total_steps if total_steps else 0
        emit_step(order, run, sdef['name'], 'running', step['message'], percent,
                  index=idx + 1, total=len(step_defs), label=sdef.get('label', ''))
        push_to_platform(order, sdef['name'], 'running', percent)
        started = time.time()
        soft = bool(sdef.get('continue_on_fail'))
        try:
            action = compat.to_text(sdef.get('cmd', '')).strip()
            if action.startswith('@'):
                action = action[1:].strip()
            if action == 'upload':
                remotes = step_upload(app, server, order, run, step, settings, mapping)
                run['remote_package'] = remotes[0] if remotes else ''
                run['remote_packages'] = list(remotes)
                # 制品包变量只在「上传之后」才注入，避免模板里 ${package} 提前展开成空串；
                # 用 merge_dict 而不是 dict(mapping, **vars)：py2 下后者对非原生串键不安全
                mapping = compat.merge_dict(mapping, package_vars(remotes, os_kind))
            elif action == 'backup':
                step_backup(app, server, order, run, step, settings)
            elif action == 'stop':
                step_named_cmd(server, app.get('stop_cmd'), order, run, step, settings,
                               mapping, u'停服命令')
            elif action == 'replace':
                step_named_cmd(server, app.get('release_cmd'), order, run, step, settings,
                               mapping, u'替换命令')
            elif action == 'start':
                step_named_cmd(server, app.get('start_cmd'), order, run, step, settings,
                               mapping, u'启服命令')
            elif action == 'health':
                step_health(app, server, order, run, step, settings, mapping, ctx)
            elif action:
                exec_cmd(order, run, step, subst(mapping, action), settings)
            else:
                step['message'] = u'步骤未配置动作，跳过'
            step['state'] = 'success'
            if not step['message'] or step['message'] == u'执行中':
                step['message'] = u'完成'
        except StepFailed as e:
            step['state'] = 'failed'
            step['message'] = compat.safe_str(e)
            step['elapsed'] = round(time.time() - started, 2)
            save_order(order)
            percent = done[0] * 100 // total_steps if total_steps else 0
            emit_step(order, run, sdef['name'], 'failed', step['message'], percent,
                      index=idx + 1, total=len(step_defs), label=sdef.get('label', ''),
                      elapsed=step['elapsed'])
            push_to_platform(order, sdef['name'], 'failed', percent)
            if soft:
                step_log(order, run, step, u'该步骤配置了「失败继续」，跳过失败并继续执行后续步骤')
                done[0] += 1
                continue
            do_rollback(app, order, run, settings, mapping)
            run['state'] = 'rolled_back' if run.get('_rollback_ok') else 'failed'
            return
        step['elapsed'] = round(time.time() - started, 2)
        done[0] += 1
        percent = done[0] * 100 // total_steps if total_steps else 100
        save_order(order)
        emit_step(order, run, sdef['name'], 'success', step['message'], percent,
                  index=idx + 1, total=len(step_defs), label=sdef.get('label', ''),
                  elapsed=step['elapsed'])
        push_to_platform(order, sdef['name'], 'success', percent)
    run['state'] = 'success'


def run_release(order_id, ctx):
    order = load_order(order_id)
    if not order:
        return {'error': 'order not found'}
    order['state'] = 'running'
    order['started'] = time.time()
    order.setdefault('server_steps', [])
    save_order(order)
    emit(order_id, {'type': 'start', 'order_id': order_id, 'state': 'running', 'percent': 0})
    push_to_platform(order, 'pipeline', 'running', 0)
    cfg = load_settings()
    app = get_app(order.get('app'))
    if not app:
        order['state'] = 'failed'
        save_order(order)
        emit(order_id, {'type': 'done', 'order_id': order_id, 'state': 'failed'})
        return {'state': 'failed'}
    server_names = order.get('servers', [])
    concurrency = cfg.get('release_concurrency', 4)
    step_defs = app_step_defs(app)
    total_steps = max(1, len(server_names) * len(step_defs))
    done = [0]

    def process(server_name):
        if ctx is not None and ctx.canceled():
            run = new_run(server_name, step_defs)
            run['state'] = 'canceled'
            append_run(order, run)
            return run
        server = get_server(server_name)
        run = new_run(server_name, step_defs)
        if not server:
            run['state'] = 'failed'
            for s in run['steps']:
                s['state'] = 'failed'
                s['message'] = u'目标机不存在'
            append_run(order, run)
            return run
        try:
            run_server_pipeline(app, server, order, run, ctx, cfg, done, total_steps, step_defs)
        except Exception as e:                                  # noqa: BLE001
            # 任何「非 StepFailed」的意外异常（如 os.path.getsize 抛 OSError、编码错误、
            # 目标机返回体解析失败）都必须在此收口：否则异常会逃出任务线程，run 与 order
            # 永久停在 running，页面一直转圈，之后 /run 又因 409 无法重跑。
            msg = u'执行异常：' + compat.safe_str(e)
            run['state'] = 'failed'
            for s in run['steps']:
                if s.get('state') in ('pending', 'running'):
                    s['state'] = 'failed'
                    s['message'] = msg
            if run['steps']:
                step_log(order, run, run['steps'][-1], msg)
            emit_step(order, run, 'pipeline', 'failed', msg, 0)
            push_to_platform(order, 'pipeline', 'failed', 0)
            compat.eprint('[release] 执行异常 order=%s server=%s: %s' % (
                order.get('id'), server_name, compat.safe_str(e)))
        append_run(order, run)
        return run

    if order.get('strategy') == 'parallel' and len(server_names) > 1:
        compat.parallel_map(process, list(server_names), workers=min(concurrency, len(server_names)))
    else:
        for sname in server_names:
            process(sname)

    runs = order.get('server_steps', [])
    succeeded = sum(1 for r in runs if r['state'] == 'success')
    rolled = sum(1 for r in runs if r['state'] == 'rolled_back')
    failed = sum(1 for r in runs if r['state'] == 'failed')
    canceled = sum(1 for r in runs if r['state'] == 'canceled')
    if canceled and succeeded == 0 and rolled == 0:
        order['state'] = 'canceled'
    elif failed > 0 and rolled == 0:
        order['state'] = 'failed'
    elif rolled > 0:
        order['state'] = 'rolled_back'
    elif succeeded == len(runs) and runs:
        order['state'] = 'success'
    else:
        order['state'] = 'failed'
    order['finished'] = time.time()
    order['result'] = {
        'succeeded': [r['server'] for r in runs if r['state'] == 'success'],
        'failed': [r['server'] for r in runs if r['state'] in ('failed', 'rolled_back')],
    }
    save_order(order)
    emit(order_id, {'type': 'done', 'order_id': order_id, 'state': order['state']})
    push_to_platform(order, 'done', order['state'], 100)
    audit_log.write(order.get('user', ''), '', 'release.run', order_id,
                    u'执行完成 state=%s' % order['state'])
    return {'order_id': order_id, 'state': order['state']}


def run_manual_rollback(order_id, ctx):
    order = load_order(order_id)
    if not order:
        return {'error': 'order not found'}
    cfg = load_settings()
    app = get_app(order.get('app'))
    order['state'] = 'running'
    save_order(order)
    emit(order_id, {'type': 'rollback_start', 'order_id': order_id})
    any_rolled = False
    any_failed = False
    if app:
        for run in order.get('server_steps', []):
            srv = server_of(run)
            os_kind = target_os(srv, cfg)
            remotes = run.get('remote_packages') or (
                [run.get('remote_package')] if run.get('remote_package') else [])
            mapping = {
                'version': order.get('version', ''),
                'target_dir': remote_path(app.get('target_dir', ''), os_kind),
                'backup_dir': remote_path(app.get('backup_dir', ''), os_kind),
                'app': app.get('name', ''),
            }
            # 与流水线保持同一套变量语义，回滚命令里写 ${package}/${packages} 也能拿到值
            mapping.update(package_vars([p for p in remotes if p], os_kind))
            do_rollback(app, order, run, cfg, mapping)
            save_order(order)
            st = run.get('_rollback_ok')
            if st is True:
                any_rolled = True
            elif st is False:
                any_failed = True
    if any_failed and not any_rolled:
        order['state'] = 'failed'
    elif any_rolled:
        order['state'] = 'rolled_back'
    elif not order.get('server_steps'):
        order['state'] = 'canceled'
    else:
        order['state'] = 'failed'
    order['finished'] = time.time()
    save_order(order)
    emit(order_id, {'type': 'done', 'order_id': order_id, 'state': order['state']})
    push_to_platform(order, 'rollback', order['state'], 100)
    audit_log.write(order.get('user', ''), '', 'release.rollback', order_id,
                    u'手动回滚 state=%s' % order['state'])
    return {'order_id': order_id, 'state': order['state']}


# ================================================================ 路由
def build_app(data_dir):
    app = server.HttpApp(name='release_center', version=VERSION, base_dir=BASE_DIR,
                         template_dir='templates', data_dir=data_dir,
                         desc=u'发布中心：包上传 → 备份 → 停服 → 替换 → 启服 → 健康检查 → 回滚')
    app.auth = opsauth.Auth(data_dir, legacy_token=ARGS.token,
                            require_login=(not ARGS.no_login), admin_password=ARGS.admin_password or None)
    if ARGS.reset_admin_password:
        _ok, _msg, _created = app.auth.admin_reset_password(ARGS.admin_password or None)
        if _ok:
            compat.log(u'[auth] admin 口令已%s：%s' % (u'创建' if _created else u'重置', _msg))
        else:
            compat.log(u'[auth] admin 口令重置失败：%s' % _msg)

    def require_targets(req, names):
        """校验当前用户对目标机的授权（发布中心此前从不校验，受限账号可越权发布）。"""
        if app.auth is None:
            return
        denied = [n for n in names
                  if not app.auth.can_access_server(req.session, compat.safe_str(n))]
        if denied:
            raise ApiError(u'无权操作目标机: '
                           + compat.to_text(', '.join(compat.to_text(x) for x in denied)), 403)

    # ------- 基础接口
    @app.get('/api/ping', auth=False, summary='存活探测')
    def api_ping(req):
        return ok({'version': VERSION})

    @app.get('/api/health', auth=False, summary='运行状态')
    def api_health(req):
        return ok({
            'version': VERSION,
            'alive': True,
            'uptime': int(time.time() - app.started_at),
            'pid': os.getpid(),
            'data_dir': data_dir,
            'apps': len(apps.load().get('apps', [])),
            'servers': len(servers.load().get('servers', [])),
            'orders': len(orders.load().get('orders', [])),
            'packages': len(packages.load().get('items', [])),
        })

    @app.get('/api/openapi.json', auth=False, summary='OpenAPI 描述')
    def api_openapi(req):
        return app.openapi()

    @app.get('/', auth=False, summary='发布中心首页')
    def api_index(req):
        return app.render('release_center.html', {'version': VERSION})

    # ------- 鉴权（页面登录）
    @app.post('/api/login', auth=False, summary='登录')
    def api_login(req):
        data = req.json()
        res = app.auth.login(compat.safe_str(data.get('username', '')),
                              compat.safe_str(data.get('password', '')), req.client_ip)
        if not res.get('ok'):
            raise ApiError(res.get('message', '登录失败'), 401, 'unauthorized')
        perms = opsauth.ROLES.get(res['role'], {}).get('perms', [])
        return ok({'token': res['token'], 'username': res['username'], 'role': res['role'],
                   'role_label': opsauth.ROLES.get(res['role'], {}).get('label', res['role']),
                   'servers': res.get('servers', ['*']), 'perms': perms})

    @app.post('/api/logout', auth=True, summary='注销')
    def api_logout(req):
        token = req.headers.get('X-Auth-Token') or req.q('token')
        if token:
            app.auth.logout(token)
        return ok({'message': '已注销'})

    @app.get('/api/me', auth=False, summary='当前身份')
    def api_me(req):
        session = app.auth.resolve(req.headers, req.query)
        if not session:
            return ok({'need_login': True, 'version': VERSION})
        perms = opsauth.ROLES.get(session.get('role'), {}).get('perms', [])
        return ok({'need_login': False, 'username': session.get('username'),
                   'role': session.get('role'), 'servers': session.get('servers', ['*']),
                   'perms': perms, 'version': VERSION})

    @app.get('/api/roles', auth=True, summary='角色与权限')
    def api_roles(req):
        return ok({'roles': opsauth.ROLES, 'perms': opsauth.PERMS})

    # ------- 应用定义
    @app.get('/api/apps', perm='server.view', summary='应用定义列表')
    def api_apps_list(req):
        return ok({'apps': apps.load().get('apps', [])})

    @app.get('/api/presets', perm='server.view', summary='发布步骤链模板')
    def api_presets(req):
        return ok({'presets': PRESETS})

    @app.post('/api/apps', perm='server.edit', summary='新增应用')
    def api_apps_add(req):
        data = req.json()
        name = compat.safe_str(data.get('name', '')).strip()
        if not name:
            raise ApiError('应用名不能为空', 400)
        if get_app(name):
            raise ApiError('应用已存在', 409)
        rec = {
            'name': name,
            'biz': compat.safe_str(data.get('biz', '')),
            'target_dir': compat.safe_str(data.get('target_dir', '')),
            'package': compat.safe_str(data.get('package', '')),
            'backup_dir': compat.safe_str(data.get('backup_dir', '')),
            'backup_keep': compat.to_int(data.get('backup_keep'), 5, 0, 999),
            'release_cmd': compat.safe_str(data.get('release_cmd', '')),
            'stop_cmd': compat.safe_str(data.get('stop_cmd', '')),
            'start_cmd': compat.safe_str(data.get('start_cmd', '')),
            'health_type': compat.safe_str(data.get('health_type', 'none')),
            'health_target': compat.safe_str(data.get('health_target', '')),
            'health_retry': compat.to_int(data.get('health_retry'), 10, 1, 9999),
            'health_interval': to_float(data.get('health_interval'), 3.0),
            'rollback_cmd': compat.safe_str(data.get('rollback_cmd', '')),
            'steps': normalize_steps(data.get('steps')),
            'vars': normalize_vars(data.get('vars')),
            'param_defs': normalize_param_defs(data.get('param_defs')),
            'created': time.time(),
        }
        apps.update(lambda d: d.setdefault('apps', []).append(rec))
        audit_log.write_req(req, 'app.add', name, u'新增应用')
        return ok({'app': rec})

    @app.put('/api/apps/<name>', perm='server.edit', summary='修改应用')
    def api_apps_put(req):
        name = req.params['name']
        rec = get_app(name)
        if not rec:
            raise ApiError('应用不存在', 404)
        data = req.json()
        for fld in ('biz', 'target_dir', 'package', 'backup_dir', 'release_cmd', 'stop_cmd',
                   'start_cmd', 'health_type', 'health_target', 'rollback_cmd'):
            if fld in data:
                rec[fld] = compat.safe_str(data[fld])
        for fld in ('backup_keep', 'health_retry'):
            if fld in data:
                rec[fld] = compat.to_int(data[fld], rec.get(fld, 0), 0, 99999)
        if 'health_interval' in data:
            rec['health_interval'] = to_float(data['health_interval'], rec.get('health_interval', 3.0))
        if 'steps' in data:
            rec['steps'] = normalize_steps(data.get('steps'))
        if 'vars' in data:
            rec['vars'] = normalize_vars(data.get('vars'))
        if 'param_defs' in data:
            rec['param_defs'] = normalize_param_defs(data.get('param_defs'))
        apps.update(lambda d: d)  # 触发落盘
        audit_log.write_req(req, 'app.update', name, u'修改应用')
        return ok({'app': rec})

    @app.delete('/api/apps/<name>', perm='server.edit', summary='删除应用')
    def api_apps_del(req):
        name = req.params['name']
        if not get_app(name):
            raise ApiError('应用不存在', 404)
        apps.update(lambda d: d.update({'apps': [a for a in d.get('apps', []) if a.get('name') != name]}))
        audit_log.write_req(req, 'app.delete', name, u'删除应用')
        return ok({'message': '已删除'})

    # ------- 制品包
    @app.get('/api/packages', perm='server.view', summary='制品包列表')
    def api_packages_list(req):
        app = req.q('app')
        items = packages.load().get('items', [])
        if app:
            items = [p for p in items if p.get('app') == app]
        items = sorted(items, key=lambda p: p.get('uploaded_at', 0), reverse=True)
        return ok({'packages': items})

    @app.post('/api/packages/upload', perm='release.run', summary='上传制品包')
    def api_packages_upload(req):
        """上传单个制品包（批量/目录上传由前端按文件并发调用本接口）。

        处理顺序刻意改成「先落到隐藏的 .part 临时文件 → 算 MD5 → 再决定去留」：
          1. 请求中断 / 落盘失败时不会留下半个包污染制品包列表；
          2. 可以按 MD5 判重（skip_dup=1）直接跳过内容相同的包，
             整目录上传时不会堆出一串 a.tgz / a_1.tgz / a_2.tgz 副本；
          3. 重名改名与 rename 转正放在同一把锁里，避免并发上传互相覆盖。
        """
        filename = compat.safe_str(req.q('filename', '')).strip()
        app = compat.safe_str(req.q('app', '')).strip()
        version = compat.safe_str(req.q('version', '')).strip()
        # src = 目录上传时的来源相对路径，仅用于溯源展示（落盘仍是扁平文件名）
        src = compat.safe_str(req.q('src', '')).strip().replace('\\', '/')
        skip_dup = compat.to_int(req.q('skip_dup', 0), 0) == 1
        if not filename:
            raise ApiError('缺少 filename', 400)
        length = compat.to_int(req.handler.headers.get('Content-Length'), 0, 0, 4 * 1024 * 1024 * 1024)
        if length <= 0:
            raise ApiError('请求体为空', 400)
        tmp_path = os.path.join(PACKAGES_DIR,
                                '.upload-%s%s' % (compat.rand_hex(6), UPLOAD_PART_SUFFIX))
        md5 = hashlib.md5()

        def _sink(buf):
            f.write(buf)
            md5.update(buf)

        try:
            with open(tmp_path, 'wb') as f:
                # 走框架的流式接口：既不必把整包读进内存，也让框架知道
                # 请求体已被消费，避免响应前重复排空导致连接挂住。
                total = req.stream_body(_sink)
        except Exception as e:
            _remove_quietly(tmp_path)
            raise ApiError(u'落盘失败: ' + compat.to_text(e), 500)
        if total < length:
            _remove_quietly(tmp_path)
            raise ApiError('请求体不完整', 400)
        digest = md5.hexdigest()

        if skip_dup:
            dup = find_package_by_md5(digest, total)
            if dup:
                _remove_quietly(tmp_path)
                audit_log.write(req.user or '-', req.client_ip, 'file.upload', dup.get('name', ''),
                                u'制品包重复跳过 size=%d md5=%s' % (total, digest))
                return ok({'name': dup.get('name', ''), 'size': dup.get('size', total),
                           'md5': digest, 'app': dup.get('app', app),
                           'version': dup.get('version', version),
                           'duplicate': True,
                           'message': u'内容相同的制品包已存在，跳过'})

        with PACKAGES_LOCK:
            name = alloc_package_name(filename)
            dest = os.path.join(PACKAGES_DIR, name)
            try:
                os.rename(tmp_path, dest)
            except Exception as e:                             # 极端情况（跨设备/被占用）
                _remove_quietly(tmp_path)
                raise ApiError(u'保存失败: ' + compat.to_text(e), 500)
        meta = {'name': name, 'app': app, 'version': version, 'size': length,
                'md5': digest, 'uploaded_at': time.time()}
        if src and src != name:
            meta['src'] = src          # 目录上传的来源相对路径，便于事后溯源
        packages.update(lambda d: d.setdefault('items', []).append(meta))
        audit_log.write(req.user or '-', req.client_ip, 'file.upload', name,
                        u'制品包 %s 大小=%d md5=%s%s' % (compat.to_text(name), length, digest,
                                                      (u' 来源=' + compat.to_text(src)) if src else u''))
        return ok({'name': name, 'size': length, 'md5': digest, 'app': app,
                   'version': version, 'src': src, 'duplicate': False})

    @app.delete('/api/packages/<name>', perm='release.run', summary='删除制品包')
    def api_packages_del(req):
        name = req.params['name']
        found = None
        for p in packages.load().get('items', []):
            if p.get('name') == name:
                found = p
                break
        if not found:
            raise ApiError('制品包不存在', 404)
        path = os.path.join(PACKAGES_DIR, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                raise ApiError(u'删除文件失败: ' + compat.to_text(e), 500)
        packages.update(lambda d: d.update({'items': [p for p in d.get('items', []) if p.get('name') != name]}))
        audit_log.write_req(req, 'file.delete', name, u'删除制品包')
        return ok({'message': '已删除'})

    # ------- 服务器纳管
    def normalize_server(d):
        return {
            'name': compat.safe_str(d.get('name', '')).strip(),
            'host': compat.safe_str(d.get('host', '127.0.0.1')),
            'upload_port': compat.to_int(d.get('upload_port'), 8089, 1, 65535),
            'shell_port': compat.to_int(d.get('shell_port'), 8088, 1, 65535),
            'upload_token': compat.safe_str(d.get('upload_token', '')),
            'shell_token': compat.safe_str(d.get('shell_token', '')),
            'upload_dir': compat.safe_str(d.get('upload_dir', DEFAULT_UPLOAD_DIR)),
            'note': compat.safe_str(d.get('note', '')),
        }

    @app.get('/api/servers', perm='server.view', summary='服务器列表')
    def api_servers_list(req):
        data = servers.load().get('servers', [])
        if app.auth is not None:
            data = app.auth.filter_servers(req.session, data)
        return ok({'servers': data})

    @app.post('/api/servers', perm='server.edit', summary='新增服务器')
    def api_servers_add(req):
        data = req.json()
        rec = normalize_server(data)
        if not rec['name']:
            raise ApiError('服务器名不能为空', 400)
        if get_server(rec['name']):
            raise ApiError('服务器已存在', 409)
        servers.update(lambda d: d.setdefault('servers', []).append(rec))
        audit_log.write_req(req, 'server.add', rec['name'], u'新增目标机')
        return ok({'server': rec})

    @app.put('/api/servers/<name>', perm='server.edit', summary='修改服务器')
    def api_servers_put(req):
        name = req.params['name']
        rec = get_server(name)
        if not rec:
            raise ApiError('服务器不存在', 404)
        rec.update(normalize_server(dict(rec, **req.json())))
        servers.update(lambda d: d)
        audit_log.write_req(req, 'server.update', name, u'修改目标机')
        return ok({'server': rec})

    @app.delete('/api/servers/<name>', perm='server.edit', summary='删除服务器')
    def api_servers_del(req):
        name = req.params['name']
        if not get_server(name):
            raise ApiError('服务器不存在', 404)
        servers.update(lambda d: d.update({'servers': [s for s in d.get('servers', []) if s.get('name') != name]}))
        audit_log.write_req(req, 'server.delete', name, u'删除目标机')
        return ok({'message': '已删除'})

    @app.post('/api/servers/import', perm='server.edit', summary='从平台导入服务器')
    def api_servers_import(req):
        data = req.json()
        url = compat.safe_str(data.get('url', '')).strip()
        token = compat.safe_str(data.get('token', '')).strip()
        if not url:
            raise ApiError('缺少平台地址', 400)
        url = safe_import_url(url)
        resp = httpx.DEFAULT.get(url.rstrip('/') + '/api/servers',
                                 headers={'X-Auth-Token': token}, timeout=10)
        if not resp.ok:
            raise ApiError('拉取平台服务器失败 HTTP %d' % resp.status, 502)
        remote = (resp.json() or {}).get('data') or (resp.json() or {}).get('servers') or []
        local_map = {s['name']: s for s in servers.load().get('servers', [])}
        added = 0
        for r in remote:
            rname = compat.safe_str(r.get('name', '')).strip()
            if not rname:
                continue
            if rname in local_map:
                continue  # 冲突本地优先
            local_map[rname] = normalize_server(r)
            added += 1
        servers.update(lambda d: d.update({'servers': list(local_map.values())}))
        audit_log.write_req(req, 'server.add', url, u'从平台导入 %d 台' % added)
        return ok({'imported': added, 'servers': servers.load().get('servers', [])})

    # ------- 发布单
    @app.get('/api/orders', perm='server.view', summary='发布单列表')
    def api_orders_list(req):
        limit = req.qi('limit', 50, 1, 500)
        state = req.q('state')
        items = orders.load().get('orders', [])
        if state:
            items = [o for o in items if o.get('state') == state]
        items = sorted(items, key=lambda o: o.get('created', 0), reverse=True)
        return ok({'orders': items[:limit], 'total': len(items)})

    @app.post('/api/orders', perm='release.run', summary='创建发布单')
    def api_orders_add(req):
        data = req.json()
        app_name = compat.safe_str(data.get('app', '')).strip()
        version = compat.safe_str(data.get('version', '')).strip()
        pkgs = normalize_order_packages(data)
        sv = data.get('servers') or []
        if isinstance(sv, compat.string_types):
            sv = [s.strip() for s in sv.split(',') if s.strip()]
        strategy = compat.safe_str(data.get('strategy', 'serial'))
        if not app_name or not get_app(app_name):
            raise ApiError('应用不存在', 400)
        if not pkgs:
            raise ApiError('必须指定制品包', 400)
        known = package_names()
        missing = [p for p in pkgs if p not in known]
        if missing:
            raise ApiError(u'制品包不存在: ' + compat.to_text(', '.join(missing)), 400)
        if not sv:
            raise ApiError('至少选择一个目标服务器', 400)
        # 建单时即校验目标机授权，避免受限账号把未授权机器写进发布单
        require_targets(req, sv)
        app_def = get_app(app_name)
        params = validate_order_params(app_def, data.get('params'))
        oid = 'r-' + time.strftime('%Y%m%d') + '-' + compat.rand_hex(6)
        order = {
            'id': oid, 'app': app_name, 'version': version,
            # package 保留「第一个包」以兼容旧模板与旧前端；packages 是权威列表
            'package': pkgs[0], 'packages': list(pkgs),
            'servers': list(sv),
            'strategy': strategy if strategy in ('serial', 'parallel') else 'serial',
            'params': params,
            'user': req.user, 'state': 'pending', 'created': time.time(),
            'started': None, 'finished': None, 'server_steps': [],
            'result': {'succeeded': [], 'failed': []}, 'task_id': '',
        }
        save_order(order)
        audit_log.write_req(req, 'release.create', oid,
                           u'创建发布单 app=%s ver=%s 包=%s'
                           % (compat.to_text(app_name), compat.to_text(version),
                              compat.to_text(', '.join(pkgs))))
        return ok({'id': oid, 'order': order})

    @app.get('/api/orders/<id>', perm='server.view', summary='发布单详情')
    def api_orders_get(req):
        order = load_order(req.params['id'])
        if not order:
            raise ApiError('发布单不存在', 404)
        return ok({'order': order_public(order)})

    @app.post('/api/orders/<id>/run', perm='release.run', summary='执行发布')
    def api_orders_run(req):
        order = load_order(req.params['id'])
        if not order:
            raise ApiError('发布单不存在', 404)
        require_targets(req, order.get('servers') or [])
        # 「是否正在执行」的检查与「置为 running」必须在同一临界区内完成：否则连点两次
        # 「执行」时，两次都可能在任务线程置位之前通过检查，导致同一发布单并发跑两遍
        # （目标机侧表现为重跑 @stop/@replace/@start，副作用叠加）。
        with ORDER_LOCK:
            order = load_order(req.params['id'])
            if not order:
                raise ApiError('发布单不存在', 404)
            if order.get('state') in ('running',):
                raise ApiError('正在执行中', 409)
            order['state'] = 'running'
            order['started'] = time.time()
            save_order(order)
        try:
            t = tm.submit(u'发布 %s %s' % (compat.to_text(order['app']), compat.to_text(order['version'])), 'release',
                          lambda c: run_release(order['id'], c), user=req.user,
                          target=order['app'], meta={'order_id': order['id']})
        except Exception:                                       # noqa: BLE001
            order['state'] = 'failed'
            save_order(order)
            raise
        order['task_id'] = t['id']
        save_order(order)
        audit_log.write_req(req, 'release.run', order['id'], u'触发执行')
        return ok({'task_id': t['id']})

    @app.post('/api/orders/<id>/rollback', perm='release.run', summary='手动回滚')
    def api_orders_rollback(req):
        order = load_order(req.params['id'])
        if not order:
            raise ApiError('发布单不存在', 404)
        require_targets(req, order.get('servers') or [])
        t = tm.submit(u'回滚 %s' % compat.to_text(order['app']), 'release.rollback',
                      lambda c: run_manual_rollback(order['id'], c), user=req.user,
                      target=order['app'], meta={'order_id': order['id']})
        order['task_id'] = t['id']
        save_order(order)
        audit_log.write_req(req, 'release.rollback', order['id'], u'触发手动回滚')
        return ok({'task_id': t['id']})

    @app.post('/api/orders/<id>/cancel', perm='release.run', summary='取消发布')
    def api_orders_cancel(req):
        order = load_order(req.params['id'])
        if not order:
            raise ApiError('发布单不存在', 404)
        if order['state'] in FINAL_STATES:
            raise ApiError('已结束，无法取消', 409)
        if order.get('task_id'):
            tm.cancel(order['task_id'])
        order['state'] = 'canceled'
        order['finished'] = time.time()
        save_order(order)
        emit(order['id'], {'type': 'done', 'order_id': order['id'], 'state': 'canceled'})
        audit_log.write_req(req, 'release.run', order['id'], u'取消发布')
        return ok({'state': 'canceled'})

    # ------- SSE
    @app.get('/api/orders/<id>/stream', perm='server.view', summary='发布单进度流')
    def api_orders_stream(req):
        oid = req.params['id']
        order = load_order(oid)
        stream = req.sse()
        stream.ready()
        if order:
            stream.json({'type': 'snapshot', 'order': order_public(order)})
        with ORDER_EVENTS_LOCK:
            hist = list(ORDER_EVENTS.get(oid, []))
        for ev in hist:
            stream.json(ev)
        sub = sse.BUS.subscribe('release:' + oid)
        try:
            while not stream.closed:
                item = sub.get(1.0)
                if item:
                    stream.json(item['data'])
                else:
                    if stream.closed:
                        break
                    stream.ping()
        finally:
            sse.BUS.unsubscribe(sub)
        return None

    @app.get('/api/events', perm='server.view', summary='全局事件流')
    def api_events(req):
        sse.stream_events(req, ['release'])
        return None

    # ------- 审计
    @app.get('/api/audit', perm='audit.view', summary='发布审计')
    def api_audit_list(req):
        res = audit_log.query(user=req.q('user'), action=req.q('action'),
                               keyword=req.q('keyword'), result=req.q('result'),
                               limit=req.qi('limit', 100, 1, 1000),
                               offset=req.qi('offset', 0), order=req.q('order') or 'desc')
        return ok(res)

    @app.get('/api/audit/verify', perm='audit.view', summary='校验审计链')
    def api_audit_verify(req):
        ok_b, message, checked, broken = audit_log.verify()
        return ok({'ok': ok_b, 'message': message, 'checked': checked, 'broken_seq': broken})

    @app.get('/api/audit/export.csv', perm='audit.view', summary='导出审计CSV')
    def api_audit_csv(req):
        rows = audit_log.export_rows()
        out = '\n'.join(','.join(csv_cell(c) for c in row) for row in rows)
        return req.text_out(out, content_type='text/csv; charset=utf-8')

    # ------- 设置
    @app.get('/api/settings', perm='settings.edit', summary='读取设置')
    def api_settings_get(req):
        return ok(load_settings())

    @app.post('/api/settings', perm='settings.edit', summary='保存设置')
    def api_settings_post(req):
        data = req.json()
        cur = load_settings()
        for fld in ('platform_url', 'platform_token', 'release_concurrency', 'cmd_timeout', 'backup_keep_default'):
            if fld in data:
                cur[fld] = data[fld]
        cur['release_concurrency'] = compat.to_int(cur.get('release_concurrency'), 4, 1, 32)
        cur['cmd_timeout'] = compat.to_int(cur.get('cmd_timeout'), 120, 1, 600)
        cur['backup_keep_default'] = compat.to_int(cur.get('backup_keep_default'), 5, 0, 999)
        save_settings(cur)
        audit_log.write_req(req, 'settings.update', 'release_center', u'修改设置')
        return ok(cur)

    return app


def csv_cell(value):
    text = compat.to_text(value)
    if (',' in text) or ('"' in text) or ('\n' in text):
        return '"' + text.replace('"', '""') + '"'
    return text


# ================================================================ 入口
def parse_args():
    p = argparse.ArgumentParser(description=u'发布中心 release_center v%s' % VERSION)
    p.add_argument('--host', default=os.environ.get('RC_HOST', '0.0.0.0'), help='监听地址')
    p.add_argument('--port', type=int, default=int(os.environ.get('RC_PORT', 8094) or 8094), help='监听端口')
    p.add_argument('--token', default=os.environ.get('RC_TOKEN', ''), help='开放接口令牌')
    p.add_argument('--data-dir', default=os.environ.get('RC_DATA', ''), help='数据目录')
    p.add_argument('--pidfile', default=os.environ.get('RC_PIDFILE', ''), help='PID 文件')
    p.add_argument('--platform-url', default=os.environ.get('RC_PLATFORM_URL', ''), help='联动平台地址')
    p.add_argument('--platform-token', default=os.environ.get('RC_PLATFORM_TOKEN', ''), help='联动平台令牌')
    p.add_argument('--admin-password', default=os.environ.get('RC_ADMIN_PASSWORD', ''),
                   help='首次启动管理员 admin 的初始口令（用户库已存在时不生效，'
                        '重置请配合 --reset-admin-password）')
    p.add_argument('--reset-admin-password', action='store_true',
                   help='启动时把 admin 口令重置为 --admin-password（未提供则生成随机口令）')
    p.add_argument('--no-login', action='store_true', help='关闭登录要求（全开放，供内网演示）')
    return p.parse_args()


# ARGS 在 main 之前解析，便于 build_app 引用（auth 需要 token/admin_password）
ARGS = parse_args()


def main():
    global APPS_FILE, SERVERS_FILE, ORDERS_FILE, PACKAGES_FILE, SETTINGS_FILE, AUDIT_FILE, TASKS_FILE
    global PACKAGES_DIR, apps, servers, orders, packages, settings, audit_log, tm

    data_dir = ARGS.data_dir or os.path.join(BASE_DIR, 'data', 'release')
    make_release_dir(data_dir)
    PACKAGES_DIR = os.path.join(data_dir, 'packages')

    APPS_FILE = os.path.join(data_dir, 'apps.json')
    SERVERS_FILE = os.path.join(data_dir, 'servers.json')
    ORDERS_FILE = os.path.join(data_dir, 'orders.json')
    PACKAGES_FILE = os.path.join(data_dir, 'packages.json')
    SETTINGS_FILE = os.path.join(data_dir, 'settings.json')
    AUDIT_FILE = os.path.join(data_dir, 'audit.jsonl')
    TASKS_FILE = os.path.join(data_dir, 'tasks.json')

    apps = None  # 下面用 JsonStore 初始化
    from opslib.store import JsonStore
    apps = JsonStore(APPS_FILE, {'apps': []}, file_mode=0o600)
    servers = JsonStore(SERVERS_FILE, {'servers': []}, file_mode=0o600)
    orders = JsonStore(ORDERS_FILE, {'orders': []}, file_mode=0o600)
    packages = JsonStore(PACKAGES_FILE, {'items': []}, file_mode=0o600)
    settings = JsonStore(SETTINGS_FILE, {'platform_url': '', 'platform_token': '',
                                         'release_concurrency': 4, 'cmd_timeout': 120,
                                         'backup_keep_default': 5}, file_mode=0o600)
    audit_log = audit.AuditLog(AUDIT_FILE)
    tm = task.TaskManager(TASKS_FILE, max_workers=4, bus=sse.BUS)

    # 把 --platform-url / --platform-token 写入设置（仅当显式传入）
    cur = load_settings()
    changed = False
    if ARGS.platform_url:
        cur['platform_url'] = ARGS.platform_url
        changed = True
    if ARGS.platform_token:
        cur['platform_token'] = ARGS.platform_token
        changed = True
    if changed:
        save_settings(cur)

    app = build_app(data_dir)
    if app.auth.default_admin_password:
        compat.log(u'首启口令    : %s（管理员 admin，请尽快修改）' % app.auth.default_admin_password)
    for problem in app.auth.diagnose():
        compat.log(u'[!] %s' % compat.to_text(problem))
    if ARGS.no_login:
        compat.log(u'登录要求    : 已关闭（全开放模式）')
    app.run(ARGS.host, ARGS.port, pidfile=ARGS.pidfile)


if __name__ == '__main__':
    main()
