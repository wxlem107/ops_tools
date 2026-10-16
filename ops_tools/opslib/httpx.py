#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.httpx —— 轻量 HTTP 客户端（带连接池复用）。

为什么不直接用 urllib：管理平台要作为代理，把浏览器请求转发给几十台目标机，
每个分片建一次 TCP 连接会带来握手 + 慢启动开销，实测是上传速率抖动的主因之一。
这里复用 http.client 并自行维护连接池，请求体支持「边读边发」的流式转发。

兼容：Python 2.7.5+ / 3.7.5+，仅标准库。
"""
from __future__ import print_function

import os
import ssl
import time
from collections import deque

from . import compat


class Response(object):
    __slots__ = ('status', 'headers', 'body', 'elapsed', 'url', 'error')

    def __init__(self, status=0, headers=None, body=b'', elapsed=0.0, url='', error=None):
        self.status = status
        self.headers = headers or {}
        self.body = body or b''
        self.elapsed = elapsed
        self.url = url
        self.error = error

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return compat.to_text(self.body)

    def json(self, default=None):
        return compat.json_loads(self.body, default)

    def header(self, name, default=''):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def __repr__(self):
        return '<Response %s %s %dB>' % (self.status, self.url, len(self.body))


class Client(object):
    def __init__(self, timeout=10, retries=0, pool_max=8, idle_seconds=60,
                 user_agent='opslib/1.0', verify_ssl=False):
        self.timeout = timeout
        self.retries = retries
        self.pool_max = pool_max
        self.idle_seconds = idle_seconds
        self.user_agent = user_agent
        self.verify_ssl = verify_ssl
        self._pool = {}
        self._lock = compat.threading.Lock()
        self._closed = False

    # ------------------------------------------------------------ 连接池
    def _conn_usable(self, conn):
        """池中连接是否仍可用：可读且读到 EOF 说明对端已关闭"""
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

    def _pool_key(self, host, port, scheme):
        return '%s://%s:%d' % (scheme, host, port)

    def _take_conn(self, key, host, port, scheme, timeout):
        with self._lock:
            bucket = self._pool.get(key)
            while bucket:
                conn, ts = bucket.popleft()
                if time.time() - ts > self.idle_seconds or not self._conn_usable(conn):
                    self._close(conn)
                    continue
                self._apply_timeout(conn, timeout)
                return conn
        return self._new_conn(host, port, scheme, timeout)

    @staticmethod
    def _apply_timeout(conn, timeout):
        """复用池中连接时**必须重设套接字超时**。

        池里的连接是「创建它的那次请求」按自己的 timeout 建的（例如平台巡检用的
        timeout=2）；keep-alive 复用后 http.client 不会刷新 socket 超时，
        于是一次长请求（上传/转发/拉取）会继承那个短超时，几秒后读超时——
        对外表现就是「莫名 status=0、请求失败」。现场反馈「编辑后保存/刷新失败」
        即由此放大：保存后的列表刷新要探测离线机（慢），却复用了探测用的短超时连接。
        """
        try:
            conn.timeout = timeout
        except Exception:                                        # noqa: BLE001
            pass
        sock = getattr(conn, 'sock', None)
        if sock is not None:
            try:
                sock.settimeout(timeout)
            except Exception:                                    # noqa: BLE001
                pass

    def _new_conn(self, host, port, scheme, timeout):
        if scheme == 'https':
            ctx = None
            if hasattr(ssl, 'create_default_context'):
                ctx = ssl.create_default_context()
                if not self.verify_ssl:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
            conn = compat.http_client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = compat.http_client.HTTPConnection(host, port, timeout=timeout)
        return conn

    def _give_conn(self, key, conn):
        with self._lock:
            bucket = self._pool.setdefault(key, deque())
            if len(bucket) >= self.pool_max:
                self._close(conn)
                return
            bucket.append((conn, time.time()))

    @staticmethod
    def _close(conn):
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def close(self):
        self._closed = True
        with self._lock:
            for bucket in self._pool.values():
                for conn, _ in bucket:
                    self._close(conn)
            self._pool.clear()

    def pool_stat(self):
        with self._lock:
            return {k: len(v) for k, v in self._pool.items() if v}

    # ------------------------------------------------------------ 拆解 URL
    @staticmethod
    def _split(url):
        parsed = compat.urlparse(url)
        scheme = (parsed.scheme or 'http').lower()
        host = parsed.hostname or '127.0.0.1'
        if parsed.port:
            port = parsed.port
        else:
            port = 443 if scheme == 'https' else 80
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        return scheme, host, port, path

    # ------------------------------------------------------------ 请求
    def request(self, method, url, headers=None, body=None, timeout=None, retries=None,
                src=None, src_len=0, max_body=64 * 1024 * 1024):
        """发起请求。

        body 为 bytes/str 时直接发送；src 为文件类对象时从 src 流式读取 src_len 字节发送
        （用于代理大文件上传，避免在内存里堆整份请求体）。
        """
        method = method.upper()
        scheme, host, port, path = self._split(url)
        attempt_total = (self.retries if retries is None else retries) + 1
        timeout = timeout or self.timeout
        last = Response(0, {}, b'', 0.0, url, 'unknown')
        for attempt in range(attempt_total):
            started = time.time()
            key = self._pool_key(host, port, scheme)
            conn = None
            remaining = src_len
            sent = False
            try:
                conn = self._take_conn(key, host, port, scheme, timeout)
                hdrs = {'User-Agent': self.user_agent, 'Accept': '*/*',
                        'Connection': 'keep-alive'}
                for k, v in (headers or {}).items():
                    if k.lower() in ('host', 'content-length', 'connection', 'transfer-encoding'):
                        continue
                    hdrs[k] = v
                if src is not None:
                    hdrs['Content-Length'] = str(src_len)
                elif body is not None:
                    payload = compat.to_bytes(body)
                    hdrs['Content-Length'] = str(len(payload))
                else:
                    payload = None

                conn.putrequest(method, path, skip_accept_encoding=True)
                for k, v in hdrs.items():
                    conn.putheader(k, v)
                conn.endheaders()
                if src is not None:
                    while remaining > 0:
                        buf = src.read(min(1024 * 1024, remaining))
                        if not buf:
                            break
                        conn.send(buf)
                        sent = True
                        remaining -= len(buf)
                elif payload:
                    conn.send(payload)
                    sent = True
                resp = conn.getresponse()
                truncated = False
                if max_body:
                    # 多读 1 字节用于判断是否被上限截断。若截断，连接里仍有未读
                    # body，绝不能放回池中复用（否则下次请求会读到上一个响应的残留
                    # 数据，甚至形成请求走私面）。_conn_usable 用 MSG_PEEK 看到残留
                    # 数据反而会判「可用」，因此这里必须显式关闭。
                    raw = resp.read(max_body + 1)
                    if len(raw) > max_body:
                        raw = raw[:max_body]
                        truncated = True
                else:
                    raw = resp.read()
                status = resp.status
                rheaders = dict(resp.getheaders())
                elapsed = time.time() - started
                if 200 <= status < 500 and not truncated and getattr(conn, 'sock', None) is not None:
                    self._give_conn(key, conn)
                else:
                    self._close(conn)
                return Response(status, rheaders, raw, elapsed, url)
            except Exception as e:                            # noqa: BLE001
                last = Response(0, {}, b'', time.time() - started, url, compat.safe_str(e))
                self._close(conn)
                # 排空未读请求体，避免污染后续请求
                while remaining > 0:
                    try:
                        buf = src.read(min(1024 * 1024, remaining))
                    except Exception:
                        break
                    if not buf:
                        break
                    remaining -= len(buf)
                # 已发出数据时无法安全重放，直接返回
                if sent or attempt == attempt_total - 1:
                    break
                time.sleep(0.15 * (attempt + 1))
        return last

    # ------------------------------------------------------------ 便捷方法
    def get(self, url, headers=None, timeout=None, **kw):
        return self.request('GET', url, headers=headers, timeout=timeout, **kw)

    def post(self, url, body=None, headers=None, timeout=None, json_body=None, **kw):
        if json_body is not None:
            body = compat.json_bytes(json_body)
            headers = dict(headers or {})
            headers.setdefault('Content-Type', 'application/json; charset=utf-8')
        return self.request('POST', url, headers=headers, body=body, timeout=timeout, **kw)

    def put(self, url, body=None, headers=None, timeout=None, json_body=None, **kw):
        if json_body is not None:
            body = compat.json_bytes(json_body)
            headers = dict(headers or {})
            headers.setdefault('Content-Type', 'application/json; charset=utf-8')
        return self.request('PUT', url, headers=headers, body=body, timeout=timeout, **kw)

    def delete(self, url, headers=None, timeout=None, json_body=None, **kw):
        body = compat.json_bytes(json_body) if json_body is not None else None
        headers = dict(headers or {})
        if json_body is not None:
            headers.setdefault('Content-Type', 'application/json; charset=utf-8')
        return self.request('DELETE', url, headers=headers, body=body, timeout=timeout, **kw)

    def head(self, url, headers=None, timeout=None, **kw):
        return self.request('HEAD', url, headers=headers, timeout=timeout, **kw)

    # ------------------------------------------------------------ 下载
    def download(self, url, dest, headers=None, timeout=120, chunk=512 * 1024,
                 on_progress=None, resume_from=0):
        """流式下载到本地文件，边收边写，不占内存。返回 (ok, message, size)"""
        scheme, host, port, path = self._split(url)
        hdrs = dict(headers or {})
        if resume_from:
            hdrs['Range'] = 'bytes=%d-' % resume_from
        conn = None
        try:
            conn = self._new_conn(host, port, scheme, timeout)
            hdrs.setdefault('User-Agent', self.user_agent)
            conn.putrequest('GET', path, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()
            resp = conn.getresponse()
            if resp.status >= 400:
                return False, 'HTTP %d' % resp.status, 0
            mode = 'ab' if resume_from and resp.status == 206 else 'wb'
            total = compat.to_int(resp.getheader('Content-Length') or 0, 0)
            got = 0 if mode == 'wb' else resume_from
            compat.makedirs(os.path.dirname(os.path.abspath(dest)))
            with open(dest, mode) as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
                    if on_progress:
                        try:
                            on_progress(got, total or got)
                        except Exception:
                            pass
            return True, 'ok', got
        except Exception as e:                                # noqa: BLE001
            return False, compat.safe_str(e), 0
        finally:
            self._close(conn)

    # ------------------------------------------------------------ 探测
    def probe(self, url, timeout=5, headers=None):
        """轻量存活探测：返回 (online, status, elapsed_ms, note)"""
        resp = self.get(url, headers=headers, timeout=timeout, retries=0)
        if resp.status == 0:
            return False, 0, resp.elapsed * 1000, resp.error or '连接失败'
        return resp.ok, resp.status, resp.elapsed * 1000, resp.header('Server')


DEFAULT = Client(user_agent='opslib/1.0')


def get(url, **kw):
    return DEFAULT.get(url, **kw)


def post(url, **kw):
    return DEFAULT.post(url, **kw)


def put(url, **kw):
    return DEFAULT.put(url, **kw)


def delete(url, **kw):
    return DEFAULT.delete(url, **kw)


def join_url(host, port, path='', scheme='http'):
    path = path or ''
    if not path.startswith('/'):
        path = '/' + path
    return '%s://%s:%d%s' % (scheme, host, int(port), path)
