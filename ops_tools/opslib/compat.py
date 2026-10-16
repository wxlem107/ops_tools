#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.compat —— Python 2.7.5+ / 3.7.5+ 兼容层与通用小工具。

本模块是整个 opslib 的地基：把原先散落在 4 个脚本里重复的兼容代码集中到一处。
所有函数均只依赖标准库，且在 Python 2.7.5 与 Python 3.7.5 上行为一致。
"""
from __future__ import print_function

import hashlib
import io
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

PY2 = sys.version_info[0] == 2
IS_WIN = os.name == 'nt'
DEVNULL = open(os.devnull, 'wb')

# ---------------------------------------------------------------- 模块导入分叉
if PY2:
    import Queue as _queue_mod
    import BaseHTTPServer as _http_server
    import SocketServer as _sock_server
    import httplib as http_client
    import urlparse as _urlparse
    import urllib as _urllib
    import urllib2 as urllib_request
    import urllib2 as urllib_error

    text_type = unicode            # noqa: F821  (py2 内置)
    string_types = (basestring,)   # noqa: F821  (py2 内置)
    int_types = (int, long)        # noqa: F821  (py2 内置)
    CLIENT_GONE = (socket.error, IOError)
    SOCKET_ERRORS = (socket.error, IOError)
else:
    import queue as _queue_mod
    import http.server as _http_server
    import socketserver as _sock_server
    import http.client as http_client
    import urllib.parse as _urlparse
    import urllib.parse as _urllib
    import urllib.request as urllib_request
    import urllib.error as urllib_error

    text_type = str
    string_types = (str,)
    int_types = (int,)
    CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
    SOCKET_ERRORS = (OSError,)

Queue = _queue_mod.Queue
Empty = _queue_mod.Empty
BaseHTTPRequestHandler = _http_server.BaseHTTPRequestHandler
HTTPServer = _http_server.HTTPServer
HTTPConnection = http_client.HTTPConnection

# 「原生串」类型：py2 下是字节串（str），py3 下是 str。
# 凡是「与含中文的字节串字面量做 % 格式化 / 拼接」的值，都必须是这个类型，
# 否则 py2 会按 ascii 解码字面量并抛 UnicodeDecodeError（见 to_native / safe_str）。
NATIVE_STR = str


# ---------------------------------------------------------------- 文本 / 字节
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


def to_text(raw, encoding='utf-8'):
    """转成当前版本的原生文本类型"""
    if isinstance(raw, text_type):
        return raw
    if isinstance(raw, bytes):
        return raw.decode(encoding, 'replace')
    return text_type(raw)


def to_native(value):
    """把值归一为「原生字符串 / 原生结构」。

    Python 2 下 json 解析、正则等都会产出 unicode，而本项目大量使用
    「含中文的字节串字面量」做 % 格式化（如 '服务器不存在: %s' % name）。
    py2 在 str % unicode 时会尝试按 ascii 解码该字面量，中文随即抛
    UnicodeDecodeError（'ascii' codec can't decode byte 0xe5 ...），
    这类崩溃与业务无关、只与编码有关，因此这里在数据入口把 unicode
    统一归一为 UTF-8 字节串，从源头消除隐患。

    Python 3 下原生字符串本身就是 str，直接原样返回，行为完全不变。
    """
    if not PY2:
        return value
    if isinstance(value, text_type):
        try:
            return value.encode('utf-8')
        except UnicodeEncodeError:
            return value.encode('utf-8', 'replace')
    if isinstance(value, dict):
        return dict((to_native(k), to_native(v)) for k, v in value.items())
    if isinstance(value, list):
        return [to_native(v) for v in value]
    if isinstance(value, tuple):
        return tuple(to_native(v) for v in value)
    return value


def safe_str(value):
    """把任意值转成「原生字符串」。

    Python 2 下原生字符串是字节串、py3 是 str；本项目其余代码大量使用
    含中文的字节串字面量做 % 格式化，因此统一以字节串（UTF-8）为基准，
    避免 str % unicode 触发的隐式 ascii 解码错误。
    需要文本类型（与 u'' 字面量拼接）请显式用 to_text()。
    """
    if value is None:
        return ''
    return to_native(to_text(value))


def _decode_text(raw):
    """把字节串尽力解码成文本（utf-8 优先，其次 gb18030，最后用替换符兜底）"""
    for enc in ('utf-8', 'gb18030'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', 'replace')


def json_safe(value):
    """递归把值归一为「纯文本结构」，供 JSON 序列化使用。

    为什么必须在序列化前归一：py2 的 `json.dumps(obj, ensure_ascii=False)` 对
    **字符串值**是「输入 unicode 产出 unicode 片段、输入 str（字节串）产出字节
    片段」，最后 `json/encoder.py` 里 `''.join(chunks)` 把两类片段混在一起 ——
    只要有非 ASCII，str.join 就会走 PyUnicode_Join 并把字节片段按 **ascii** 解码：

        UnicodeDecodeError: 'ascii' codec can't decode byte 0xe5 in position 1
        （position 1 = 字符串片段开引号后的第一个字节，正是中文首字节）

    本项目的数据天然是混的：发布单里 `step['message']` 由 u'' 字面量拼出（unicode），
    而 `step['logs']` 经 safe_str 落成 UTF-8 字节串，两者同处一个 dict —— 于是
    「执行发布后打开详情 / 落盘 / 推 SSE」都会 500，且**只在 py2 线上发作**。

    这里统一把字节串解码成文本（py2: str→unicode，py3: bytes→str），键同样处理；
    py3 下顺带消掉 `TypeError: Object of type bytes is not JSON serializable`。

    与 to_native() 的分工 —— 两者取向相反，不要互换：
      * to_native  → 面向「与含中文的字节串字面量做 % 格式化」的数据；
      * json_safe  → 面向「交给 json 序列化 / 落盘」的数据。
    """
    if isinstance(value, text_type):        # py3: str   | py2: unicode
        return value
    if isinstance(value, bytes):            # py3: bytes | py2: str（可能是 UTF-8 中文）
        return _decode_text(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[json_safe(key)] = json_safe(item)
        return out
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def json_bytes(obj, **kwargs):
    """json 序列化并转 bytes；任何版本、任何混排数据都不会崩。

    两道保险：
      1. 序列化前先 json_safe() 归一：消除 py2 下 unicode 与字节串混排时
         `''.join(chunks)` 的隐式 ascii 解码（根因详见 json_safe 的 docstring）；
      2. 兜底：万一还有不认识的类型（set / datetime / 自定义对象）或意外的编码
         问题，退化为「ensure_ascii=True + 未知类型按文本处理」——宁可少一点
         可读性，也不能让整个接口变成 500、把真正的错误信息埋掉。

    统一走这个函数，不要在业务代码里裸写 `json.dumps(..., ensure_ascii=False)`
    （opslib/selftest 有静态兜底扫描）。
    """
    kwargs.setdefault('ensure_ascii', False)
    obj = json_safe(obj)
    try:
        return to_bytes(json.dumps(obj, **kwargs))
    except (TypeError, ValueError, UnicodeDecodeError, UnicodeEncodeError):
        kwargs['ensure_ascii'] = True
        kwargs.setdefault('default', safe_str)
        return to_bytes(json.dumps(obj, **kwargs))


def json_loads(raw, default=None):
    """宽松解析 JSON：入参可以是 bytes / str / 空，失败返回 default。

    解析结果统一经 to_native 归一（py2 下 unicode → UTF-8 字节串），
    避免调用方把 JSON 里的中文值与中文字面量做 % 格式化时崩溃。
    """
    if raw is None:
        return default
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'ignore')
        if isinstance(raw, text_type) and not raw.strip():
            return default
        return to_native(json.loads(raw))
    except Exception:
        return default


def html_escape(text):
    """HTML 转义"""
    return (safe_str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))


def log(msg):
    """跨版本安全输出：py2 终端编码非 UTF-8 时也不会抛 UnicodeEncodeError。

    连 `'%s\\n' % msg` 这一步都必须在 try 里：log() 大量用在「异常路径」上
    （发送 500 前的提示、任务失败回显），它自己再抛错会把真正的错误信息盖掉，
    客户端只看到连接被重置，排查成本翻倍。
    """
    try:
        text = '%s\n' % (msg,)
    except Exception:                                         # noqa: BLE001
        try:
            text = '%s\n' % (safe_str(msg),)
        except Exception:                                     # noqa: BLE001
            return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            sys.stdout.write(to_bytes(text))
        except Exception:                                     # noqa: BLE001
            pass
    except Exception:                                         # noqa: BLE001
        pass


def eprint(msg):
    """错误输出（同样保证自身不抛错）"""
    try:
        text = '%s\n' % (msg,)
    except Exception:                                         # noqa: BLE001
        try:
            text = '%s\n' % (safe_str(msg),)
        except Exception:                                     # noqa: BLE001
            return
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:                                         # noqa: BLE001
        try:
            sys.stderr.write(to_bytes(text))
        except Exception:                                     # noqa: BLE001
            pass


# ---------------------------------------------------------------- 文件 / 目录
def open_text(path, mode='r', encoding='utf-8', errors=None):
    """带编码的文本读写：py2 内置 open 不支持 encoding，用 io.open 替代。

    errors：解码容错策略。读日志类文件时建议传 'replace' —— Windows 上
    CPython 的系统异常消息（如 ConnectionResetError）以本地编码（GBK）落盘，
    直接 utf-8 解码会抛 UnicodeDecodeError。
    """
    if PY2:
        return io.open(path, mode, encoding=encoding, errors=errors)
    return open(path, mode, encoding=encoding, errors=errors)


def makedirs(path):
    """递归建目录（已存在不报错）"""
    if not path:
        return
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            if not os.path.isdir(path):
                raise


def os_replace(src, dst):
    """原子替换（py2.7 无 os.replace；Windows 下 os.rename 不允许目标已存在）"""
    if hasattr(os, 'replace'):
        os.replace(src, dst)
        return
    if os.name == 'nt' and os.path.exists(dst):
        os.remove(dst)
    os.rename(src, dst)


def is_within(path, root):
    """判断 path 是否位于 root 之内（含自身）。

    必须用 realpath 而非 abspath：abspath 只做词法归一，存储根内若存在指向外部的
    符号链接目录，词法层看起来「仍在 root 内」，实际写入却落在 root 之外。
    """
    try:
        path = os.path.realpath(path)
        root = os.path.realpath(root)
    except Exception:
        return False
    if path == root:
        return True
    return path.startswith(root + os.sep)


def disk_usage(path):
    """替代 py3 的 shutil.disk_usage，返回 (total, used, free) 字节；失败返回 (0,0,0)"""
    if hasattr(shutil, 'disk_usage'):
        try:
            u = shutil.disk_usage(path)
            return u.total, u.used, u.free
        except Exception:
            pass
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return total, total - free, free
    except Exception:
        return 0, 0, 0


def fullmatch(pattern, text):
    """替代 py3 的 re.fullmatch"""
    if hasattr(re, 'fullmatch'):
        return re.fullmatch(pattern, text)
    return re.match('(?:%s)\\Z' % pattern, text)


def which(cmd):
    """替代 py3 的 shutil.which；返回可执行文件路径或 None"""
    if hasattr(shutil, 'which'):
        return shutil.which(cmd)
    paths = os.environ.get('PATH', '').split(os.pathsep)
    exts = [''] if not IS_WIN else (os.environ.get('PATHEXT', '.EXE;.BAT;.CMD').split(';') + [''])
    for d in paths:
        for ext in exts:
            p = os.path.join(d, cmd + ext)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def fmt_size(num):
    """人类可读的容量字符串"""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return '-'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if abs(num) < 1024.0 or unit == 'PB':
            return ('%.0f %s' % (num, unit)) if unit == 'B' else ('%.1f %s' % (num, unit))
        num /= 1024.0


def fmt_time(ts=None, pattern='%Y-%m-%d %H:%M:%S'):
    return time.strftime(pattern, time.localtime(ts if ts is not None else time.time()))


def fmt_duration(seconds):
    """秒 -> 1d2h3m4s 形式"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return '-'
    if seconds < 0:
        seconds = 0
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    out = []
    if d:
        out.append('%dd' % d)
    if h or d:
        out.append('%dh' % h)
    if m or h or d:
        out.append('%dm' % m)
    out.append('%ds' % s)
    return ''.join(out)


def walk_files(root, with_dirs=False):
    """遍历目录，产出 (相对路径, 大小, mtime)；相对路径统一使用 / 分隔"""
    root = os.path.abspath(root)
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace('\\', '/')
            try:
                st = os.stat(full)
                yield rel, st.st_size, st.st_mtime
            except OSError:
                continue
        if with_dirs:
            for name in dirs:
                full = os.path.join(base, name)
                yield os.path.relpath(full, root).replace('\\', '/') + '/', -1, 0


def read_head(path, limit=65536, encoding='utf-8'):
    """读取文件头部若干字节并尝试解码（用于类型嗅探 / 文本预览）"""
    try:
        with open(path, 'rb') as f:
            raw = f.read(limit)
    except (OSError, IOError):
        return None
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        try:
            return raw.decode('gb18030')
        except Exception:
            return None


# ---------------------------------------------------------------- 摘要
def file_hash(path, algo='sha256', bufsize=1024 * 1024):
    """计算文件摘要，失败返回 None"""
    try:
        h = hashlib.new(algo)
    except Exception:
        return None
    try:
        with open(path, 'rb') as f:
            while True:
                buf = f.read(bufsize)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()
    except (OSError, IOError):
        return None


def sha256_text(text):
    return hashlib.sha256(to_bytes(text)).hexdigest()


def hmac_sha256_hex(key, msg):
    import hmac as _hmac
    return _hmac.new(to_bytes(key), to_bytes(msg), hashlib.sha256).hexdigest()


def rand_hex(nbytes=16):
    """随机十六进制串（优先 os.urandom，无则用时间+PID 摘要兜底）"""
    try:
        raw = os.urandom(nbytes)
    except Exception:
        raw = hashlib.sha256(to_bytes('%s|%s|%s' % (time.time(), os.getpid(), id(object())))).digest()
        raw = (raw * (nbytes // len(raw) + 1))[:nbytes]
    if hasattr(raw, 'hex'):
        return raw.hex()
    return ''.join('%02x' % ord(c) for c in raw)


def constant_time_eq(a, b):
    """定时安全比较，避免摘要比较被计时攻击"""
    a, b = to_bytes(a), to_bytes(b)
    if len(a) != len(b):
        return False
    result = 0
    for i in range(len(a)):
        if PY2:
            result |= ord(a[i]) ^ ord(b[i])
        else:
            result |= a[i] ^ b[i]
    return result == 0


# ---------------------------------------------------------------- URL
def urlparse(url):
    return _urlparse.urlparse(url)


def urlparse_qs(query):
    return _urlparse.parse_qs(query or '')


def urlunparse(parts):
    return _urlparse.urlunparse(parts)


def urlquote(text, safe=''):
    """URL 编码。

    py2：`urllib.quote` 只接受字节串，必须先 to_bytes（否则遇到非 ASCII 直接
    抛 KeyError/UnicodeEncodeError）；编出来的结果本身就是 ASCII 字节串，
    正是「原生串」，无需再转。
    py3：`urllib.parse.quote` 只接受 str，必须传文本（传 bytes 在 3.8 及以下会
    抛 TypeError；3.9+ 虽兼容但不该依赖）；它默认已是 str，直接用。

    历史坑：老实现无条件 `to_bytes()` 再 quote，py3 下等于把 str 变成 bytes——
    在 3.8 环境里 `quote` 尚可容忍，但同一错误的兄弟函数 urlunquote 会直接炸
    （见下），故这里一并按版本分支写对。
    """
    if PY2:
        return _urllib.quote(to_bytes(text), safe=safe)
    return _urllib.quote(to_text(text), safe=safe)


def urlunquote(text):
    """URL 解码，返回值统一为「原生字符串」（py2 下是 UTF-8 字节串）。

    必须用 to_native 而不是 to_text：解码结果都是**请求路径**这类会立刻参与
    「含中文的字节串字面量」格式化的数据，例如框架里的

        '接口不存在: %s' % path

    py2 下一旦参数是 unicode，就会先按 ascii 解码这个字面量，
    紧接着抛 UnicodeDecodeError: 'ascii' codec can't decode byte 0xe6 ...
    —— 表现为**任意 404（含 favicon、静态资源缺失、不存在的接口）都变成 500**，
    且日志被 traceback 刷屏；请求路径本身是 ASCII 也一样中招（炸的是字面量）。
    归一到原生串，与 safe_str / JSON 入口保持一致，从源头消除这类崩溃。

    **py3.8 及以下专属崩溃（本函数曾长期踩中）**：老实现无条件
    `to_bytes(text)` 再交给 `_urllib.unquote`，而 py3 下 `_urllib` 是
    `urllib.parse`：其 `unquote()` 在 **Python 3.9 之前只接受 str**，传 bytes 会
    抛 `TypeError: Expected str, got bytes`。于是 py3.5~3.8 上**每一个请求**
    （包括首页 `GET /`）都会在 dispatch 阶段 500，服务完全不可用——而 3.9+
    因为放宽了类型检查反倒不复现，极具迷惑性。
    正确做法：py2 走「字节串 → 解码」；py3 直接把文本交给 unquote（它自带
    UTF-8 解码），最后统一 to_native 归一。
    """
    if PY2:
        return to_native(to_text(_urllib.unquote(to_bytes(text))))
    return to_native(_urllib.unquote(to_text(text)))


def urlencode(params):
    if hasattr(_urllib, 'urlencode'):
        return _urllib.urlencode(params)
    return params


# ---------------------------------------------------------------- HTTP 客户端薄封装
def make_request(url, data=None, headers=None, method=None):
    """构造请求对象；py2 的 Request 不支持 method 关键字，需要覆盖 get_method"""
    req = urllib_request.Request(url, data=data, headers=headers or {})
    if method:
        req.get_method = lambda: method
    return req


def urlopen(req, timeout=None):
    return urllib_request.urlopen(req, timeout=timeout)


def http_status(resp):
    """取响应状态码（py2 的 addinfourl 只有 code，没有 status）"""
    return getattr(resp, 'status', None) or getattr(resp, 'code', 0) or 0


# ---------------------------------------------------------------- 并发 / 线程
def start_daemon(target, args=(), name=None):
    """启动守护线程（py2 的 Thread 不支持 daemon 关键字参数）"""
    thread = threading.Thread(target=target, args=args)
    if name:
        thread.setName(name)
    thread.daemon = True
    thread.start()
    return thread


class ThreadingHTTPServer(_sock_server.ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务（py2 / py3 通用，替代 py3.7+ 才有的 ThreadingHTTPServer）"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


try:
    import concurrent.futures as _futures
except ImportError:
    _futures = None
HAS_FUTURES = _futures is not None


def parallel_map(func, items, workers=8, timeout=None):
    """并行 map：优先用 concurrent.futures（py3），否则退回自建线程池。
    返回 [(ok, result_or_exc)]，顺序与 items 一致。"""
    items = list(items)
    if not items:
        return []
    if _futures is not None:
        results = [None] * len(items)
        with _futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(func, it) for it in items]
            for idx, fut in enumerate(futs):
                try:
                    results[idx] = (True, fut.result(timeout=timeout))
                except Exception as e:                       # noqa: BLE001
                    results[idx] = (False, e)
        return results
    # py2 兜底：手写线程池
    results = [None] * len(items)
    q = Queue()
    for idx, it in enumerate(items):
        q.put((idx, it))

    def worker():
        while True:
            try:
                idx, it = q.get_nowait()
            except Empty:
                return
            try:
                results[idx] = (True, func(it))
            except Exception as e:                            # noqa: BLE001
                results[idx] = (False, e)
            finally:
                q.task_done()

    threads = [start_daemon(worker) for _ in range(max(1, min(workers, len(items))))]
    for t in threads:
        t.join(timeout)
    return results


# ---------------------------------------------------------------- 子进程
def _popen_kwargs(new_session=True):
    kw = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'stdin': DEVNULL}
    if IS_WIN:
        kw['creationflags'] = 0x00000200 if new_session else 0   # CREATE_NEW_PROCESS_GROUP
    elif new_session:
        kw['preexec_fn'] = os.setsid
    return kw


def kill_process_tree(proc):
    """尽力终止进程组 / 进程树"""
    if proc is None:
        return
    try:
        if IS_WIN:
            try:
                subprocess.call(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                                stdout=DEVNULL, stderr=DEVNULL)
            except Exception:
                proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_cmd(cmd, timeout=30, cwd=None, env=None, shell=True, max_output=512 * 1024):
    """执行命令并捕获输出。

    返回 dict: returncode / stdout / stderr / elapsed / timeout(bool) / error
    统一处理 py2 无 Popen.wait(timeout=) 与超时后杀进程组的问题。
    """
    started = time.time()
    result = {'returncode': -1, 'stdout': '', 'stderr': '', 'elapsed': 0.0,
              'timeout': False, 'error': None}
    proc = None
    try:
        proc = subprocess.Popen(cmd, shell=shell, cwd=cwd, env=env,
                                **_popen_kwargs(new_session=True))
    except Exception as e:                                    # noqa: BLE001
        result['error'] = safe_str(e)
        result['elapsed'] = round(time.time() - started, 3)
        return result

    chunks = {'out': [], 'err': []}

    def reader(stream, key):
        # 必须**持续**读到 EOF：只读一次会在输出超过上限时让管道缓冲区写满，
        # 子进程随后阻塞在 write 上、一直憋到超时才被杀（表现为「输出不全 + 白等满超时」）。
        # 用 os.read(fileno) 取「当前可用数据」，既不阻塞等待满 n 字节，也能排空管道。
        buf = []
        total = 0
        try:
            fd = stream.fileno()
        except Exception:
            fd = None
        try:
            if fd is not None:
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    if total <= max_output:
                        buf.append(data)
                        total += len(data)
            else:
                data = stream.read(max_output + 1)
                if data:
                    buf.append(data)
        except Exception:
            pass
        chunks[key].append(b''.join(buf))

    t_out = start_daemon(reader, (proc.stdout, 'out'))
    t_err = start_daemon(reader, (proc.stderr, 'err'))
    try:
        deadline = started + max(1, int(timeout))
        while True:
            if proc.poll() is not None:
                break
            if time.time() > deadline:
                result['timeout'] = True
                kill_process_tree(proc)
                break
            time.sleep(0.05)
    except Exception as e:                                    # noqa: BLE001
        result['error'] = safe_str(e)
    finally:
        try:
            proc.wait()
        except Exception:
            pass
        t_out.join(2)
        t_err.join(2)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

    result['returncode'] = proc.returncode if proc.returncode is not None else -1
    result['stdout'] = to_text(b''.join(chunks['out']))
    result['stderr'] = to_text(b''.join(chunks['err']))
    if len(result['stdout']) > max_output:
        result['stdout'] = result['stdout'][:max_output] + '\n...[输出已截断]'
    if len(result['stderr']) > max_output:
        result['stderr'] = result['stderr'][:max_output] + '\n...[输出已截断]'
    result['elapsed'] = round(time.time() - started, 3)
    return result


def popen_stream(cmd, cwd=None, env=None, shell=True):
    """启动长驻命令，返回 Popen（stdout=stderr=PIPE 合并），供 SSE 流式读取"""
    kw = _popen_kwargs(new_session=True)
    kw['stderr'] = subprocess.STDOUT
    return subprocess.Popen(cmd, shell=shell, cwd=cwd, env=env, **kw)


# ---------------------------------------------------------------- 命令行小工具
def arg_host_port(default_host='0.0.0.0', default_port=8000, env_prefix='', desc=''):
    """各工具统一的 host/port/token/pidfile/work-dir 参数解析辅助（供 argparse 复用）"""
    import argparse
    p = argparse.ArgumentParser(description=desc)
    p.add_argument('--host', default=os.environ.get(env_prefix + 'HOST') or default_host,
                   help='监听地址 (默认 %s)' % default_host)
    p.add_argument('--port', type=int, default=os.environ.get(env_prefix + 'PORT') or default_port,
                   help='监听端口 (默认 %d)' % default_port)
    p.add_argument('--token', default=os.environ.get(env_prefix + 'TOKEN', ''),
                   help='访问令牌，设置后需登录/携带')
    p.add_argument('--pidfile', default=os.environ.get(env_prefix + 'PIDFILE', ''),
                   help='PID 文件路径')
    p.add_argument('--work-dir', default=os.environ.get(env_prefix + 'WORK_DIR', ''),
                   help='工作目录（数据/配置落盘位置）')
    return p


def write_pidfile(path):
    if not path:
        return
    try:
        with open_text(path, 'w') as f:
            f.write(text_type(os.getpid()))
    except Exception as e:                                    # noqa: BLE001
        eprint('[warn] 写入 pidfile 失败: %s' % e)


def remove_pidfile(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def safe_join(root, rel):
    """把用户传入的相对路径安全地拼到 root 下；越界返回 None"""
    rel = (rel or '').replace('\\', '/')
    segs = [s for s in rel.split('/') if s not in ('', '.')]
    if '..' in segs:
        return None
    root = os.path.abspath(root)
    full = os.path.abspath(os.path.join(root, *segs)) if segs else root
    if not is_within(full, root):
        return None
    return full


def to_int(value, default=0, minimum=None, maximum=None):
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def clamp(value, low, high):
    return max(low, min(high, value))


def merge_dict(base, extra):
    """浅合并两个字典并返回新字典，即 `{**base, **extra}` 的跨版本替代品。

    为什么不用 `dict(base, **extra)`：py2 里 `**` 展开要求键必须是**原生 str**，
    而 JSON 解析、配置回填拿到的键可能是 unicode；更糟的是 py2 会尝试把
    含中文的键按 ascii 解码，直接抛 `UnicodeDecodeError`（与
    `compat.to_native` 里描述的同一类陷阱）。这里改为显式 `update`，
    py2 / py3 行为完全一致。

    非 dict 入参按「空字典」处理（`base`/`extra` 给 None 也安全），返回**新**对象，
    不修改任何入参。

    注意：py2 下 `b'release_url' == u'release_url'` 且哈希一致，字节键与
    unicode 键会自然合并成同一个键（后者覆盖前者），无需额外归一。
    """
    out = dict(base) if isinstance(base, dict) else {}
    if hasattr(extra, 'items'):
        out.update(extra)
    return out
