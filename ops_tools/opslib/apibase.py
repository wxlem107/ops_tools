#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.apibase —— 统一响应格式、路由与错误处理。

约定（所有工具、所有接口一致）：
  成功：{"status":"success", ...业务字段}
  失败：{"status":"error", "message":"...", "code":"..."}
  HTTP 状态码与 status 字段保持语义一致；需要逐字回传远端 body 的场景走 raw 响应。

路由支持 <name> 占位符：/api/servers/<name>/files
"""
from __future__ import print_function

import re

from .compat import json_bytes, json_loads, safe_str, string_types, to_bytes, urlparse, urlparse_qs

STATUS_BY_CODE = {
    'ok': 200, 'bad_request': 400, 'unauthorized': 401, 'forbidden': 403,
    'not_found': 404, 'conflict': 409, 'payload_too_large': 413,
    'timeout': 504, 'upstream': 502, 'error': 500,
}


class ApiError(Exception):
    """业务异常：在路由处理器里 raise，由框架转换成统一 JSON 响应"""

    def __init__(self, message, status=400, code='bad_request'):
        Exception.__init__(self, message)
        self.message = safe_str(message)
        self.status = int(status)
        self.code = code

    def to_body(self):
        return {'status': 'error', 'code': self.code, 'message': self.message}


def ok(data):
    """统一的成功响应体"""
    body = {'status': 'success'}
    if isinstance(data, dict):
        body.update(data)
    elif data is not None:
        body['data'] = data
    return body


def err(message, code='error'):
    """统一的失败响应体"""
    return {'status': 'error', 'code': code, 'message': safe_str(message)}


class Router(object):
    """极简路由表：method + 路径模板 -> 处理器"""

    def __init__(self):
        self.routes = []

    def add(self, method, pattern, func, perm=None, auth=True, summary='', raw=False, tags=None):
        regex = '^' + re.sub(r'<([a-zA-Z_][a-zA-Z0-9_]*)>', r'(?P<\1>[^/]+)', pattern) + '$'
        self.routes.append({
            'method': method.upper(),
            'pattern': pattern,
            'regex': re.compile(regex),
            'func': func,
            'perm': perm,
            'auth': auth,
            'summary': summary,
            'raw': raw,
            'tags': tags or [],
        })
        return func

    def match(self, method, path):
        """返回 (route, params) 或 (None, None)；先精确匹配，再按注册顺序找第一条命中"""
        method = method.upper()
        for route in self.routes:
            if route['method'] != method:
                continue
            m = route['regex'].match(path)
            if m:
                return route, m.groupdict()
        return None, None

    def methods_for(self, path):
        out = []
        for route in self.routes:
            if route['regex'].match(path):
                out.append(route['method'])
        return out

    # 装饰器形式（便于第三方/新工具以声明式注册）
    def get(self, pattern, **kw):
        return lambda fn: self.add('GET', pattern, fn, **kw)

    def post(self, pattern, **kw):
        return lambda fn: self.add('POST', pattern, fn, **kw)

    def put(self, pattern, **kw):
        return lambda fn: self.add('PUT', pattern, fn, **kw)

    def delete(self, pattern, **kw):
        return lambda fn: self.add('DELETE', pattern, fn, **kw)


class Request(object):
    """一次请求的上下文：把 BaseHTTPRequestHandler 的底层细节藏起来。

    路由处理器统一接收 Request，返回 dict / str / None：
      * 返回 dict  -> 自动 JSON 序列化，HTTP 200；
      * 返回 None  -> 表示处理器已自行写响应（如文件流、SSE）；
      * 抛 ApiError -> 自动转成对应状态码与统一错误体。
    """

    def __init__(self, app, handler, method, path, params=None):
        self.app = app
        self.handler = handler
        self.method = method
        self.path = path
        self.params = params or {}
        # 查询串必须从 handler.path（未解码的原始路径）里取：
        # dispatch 传入的 path 已经把 ?query 剥掉用于路由匹配，这里不能丢
        raw = safe_str(getattr(handler, 'path', '') or path)
        qpos = raw.find('?')
        raw_query = raw[qpos + 1:] if qpos >= 0 else ''
        self.parsed = urlparse(raw[:qpos] if qpos >= 0 else path)
        self.raw_query = raw_query
        self.query = {}
        self.query_list = {}
        for key, values in (urlparse_qs(raw_query) or {}).items():
            self.query_list[key] = values
            self.query[key] = values[0] if values else ''
        self.headers = dict(handler.headers.items())
        self.session = None
        self._body = None
        self._body_read = False

    # ------------------------------------------------------------ 基础属性
    @property
    def client_ip(self):
        fwd = self.headers.get('X-Forwarded-For') or self.headers.get('x-forwarded-for')
        if fwd:
            return fwd.split(',')[0].strip()
        real = self.headers.get('X-Real-IP')
        if real:
            return real.strip()
        try:
            return self.handler.client_address[0]
        except Exception:
            return ''

    @property
    def user(self):
        return (self.session or {}).get('username', '')

    @property
    def role(self):
        return (self.session or {}).get('role', '')

    def q(self, key, default=''):
        return self.query.get(key, default)

    def qi(self, key, default=0, minimum=None, maximum=None):
        try:
            val = int(self.query.get(key) or default)
        except (TypeError, ValueError):
            val = default
        if minimum is not None:
            val = max(minimum, val)
        if maximum is not None:
            val = min(maximum, val)
        return val

    def qb(self, key, default=False):
        raw = (self.query.get(key) or '').lower()
        if not raw:
            return default
        return raw in ('1', 'true', 'yes', 'on')

    # ------------------------------------------------------------ 请求体
    def raw_body(self, max_mb=32):
        if self._body is None:
            length = int(self.handler.headers.get('Content-Length') or 0)
            if length > max_mb * 1024 * 1024:
                raise ApiError('请求体过大（上限 %dMB）' % max_mb, 413, 'payload_too_large')
            self._body = self.handler.rfile.read(length) if length > 0 else b''
            self._body_read = True
        return self._body

    def stream_body(self, chunk_cb, max_mb=4096):
        """流式读取请求体：边读边把分片交给 chunk_cb，避免大文件占用内存。

        处理器用本方法消费请求体后，框架不再做排空（见 drain），
        因此 keep-alive 连接可以正常复用。返回实际读取字节数。
        """
        if self._body is not None:                             # 已整块读过
            if self._body:
                chunk_cb(self._body)
            return len(self._body)
        try:
            length = int(self.handler.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        if length > max_mb * 1024 * 1024:
            raise ApiError('请求体过大（上限 %dMB）' % max_mb, 413, 'payload_too_large')
        total = 0
        remaining = length
        while remaining > 0:
            buf = self.handler.rfile.read(min(65536, remaining))
            if not buf:
                break
            chunk_cb(buf)
            total += len(buf)
            remaining -= len(buf)
        self._body_read = True
        return total

    def mark_body_consumed(self):
        """声明请求体已由处理器自行读取（如流式转发），框架无需再排空。"""
        self._body_read = True

    def drain(self, max_drain=4 * 1024 * 1024, wait=0.25):
        """请求体未被处理器消费时的收尾。

        返回 True 表示请求体已被消费（连接可复用）；False 表示仍有未读字节，
        调用方应关闭连接。**本方法绝不长时间阻塞**：等待客户端补发数据的上限
        为 wait 秒，超时即放弃复用连接（宁可断连，也不能卡住响应）。
        """
        if self._body_read or self._body is not None:
            return True
        try:
            length = int(self.handler.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return True
        if length > max_drain:
            return False
        conn = getattr(self.handler, 'connection', None)
        old_timeout = None
        if conn is not None:
            try:
                old_timeout = conn.gettimeout()
                conn.settimeout(wait)
            except Exception:
                old_timeout = None
        try:
            remaining = length
            while remaining > 0:
                buf = self.handler.rfile.read(min(65536, remaining))
                if not buf:
                    break
                remaining -= len(buf)
            return remaining <= 0
        except Exception:
            return False                                       # 超时/异常 → 关闭连接
        finally:
            if conn is not None and old_timeout is not None:
                try:
                    conn.settimeout(old_timeout)
                except Exception:
                    pass

    def json(self, max_mb=32, required=True):
        raw = self.raw_body(max_mb)
        data = json_loads(raw, None)
        if data is None:
            if required:
                raise ApiError('请求体不是合法 JSON')
            return {}
        if not isinstance(data, dict):
            raise ApiError('请求体必须是 JSON 对象')
        return data

    def form(self, max_mb=32):
        """解析 application/x-www-form-urlencoded"""
        raw = self.raw_body(max_mb)
        out = {}
        for key, values in (urlparse_qs(raw.decode('utf-8', 'ignore')) or {}).items():
            out[key] = values[0] if values else ''
        return out

    # ------------------------------------------------------------ 响应
    def respond(self, status, body, content_type='application/json; charset=utf-8',
                extra_headers=None):
        self.handler.send_payload(status, body, content_type, extra_headers)

    def json_out(self, data, status=200, extra_headers=None):
        self.respond(status, json_bytes(data), extra_headers=extra_headers)
        return None

    def text_out(self, text, status=200, content_type='text/plain; charset=utf-8',
                 extra_headers=None):
        self.respond(status, to_bytes(text), content_type, extra_headers)
        return None

    def redirect(self, location, status=302):
        self.handler._responded = True
        self.handler.send_response(status)
        self.handler.send_header('Location', location)
        self.handler.send_header('Content-Length', '0')
        self.handler.end_headers()
        return None

    # ------------------------------------------------------------ 流式响应
    def stream(self, iterator, content_type='application/octet-stream', status=200,
               extra_headers=None, chunked=True):
        """分块流式输出（用于 zip 打包下载、大文件转发等）"""
        handler = self.handler
        handler._responded = True
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        for key, value in (extra_headers or {}).items():
            handler.send_header(key, value)
        if chunked:
            handler.send_header('Transfer-Encoding', 'chunked')
        handler.end_headers()
        try:
            for chunk in iterator:
                if not chunk:
                    continue
                if chunked:
                    handler.wfile.write(('%x\r\n' % len(chunk)).encode('ascii'))
                    handler.wfile.write(chunk if isinstance(chunk, bytes) else to_bytes(chunk))
                    handler.wfile.write(b'\r\n')
                else:
                    handler.wfile.write(chunk if isinstance(chunk, bytes) else to_bytes(chunk))
            if chunked:
                handler.wfile.write(b'0\r\n\r\n')
        except Exception:
            # 客户端提前断开（下载取消）属正常情况，静默处理
            handler.close_connection = True
        return None

    def sse(self):
        """把当前连接升级为 SSE 流；返回 SSEStream 对象"""
        from .sse import SSEStream
        return SSEStream(self.handler)
