#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.server —— 统一 HTTP 服务基座。

一个 HttpApp = 路由 + 鉴权 + 静态资源 + 页面模板 + 统一错误处理 + OpenAPI 描述。
各工具只需声明路由，其余（CORS、鉴权、静态缓存、错误格式、优雅停机）全部复用这里。

兼容：Python 2.7.5+ / 3.7.5+，仅标准库。
"""
from __future__ import print_function

import os
import socket
import sys
import threading
import time
import traceback

from . import compat
from .apibase import ApiError, Router, Request, err, ok

# 「客户端提前断开」的 errno（py2 下 CLIENT_GONE 是宽泛的 socket.error，需要按 errno 收窄）
_ABORT_ERRNOS = (32, 104, 103, 110, 10053, 10054, 10060)
_ABORT_LOG_INTERVAL = 30.0


def is_client_abort(exc):
    """判断异常是否只是「客户端先走了」（关页签 / 断流 / 超时 / curl 取消）。

    这类异常在 socketserver 里会打整段 traceback 到 stderr，浏览器一关页面
    就刷一片，真正的故障反而被淹没。识别出来后可只留一行日志。
    """
    if not isinstance(exc, compat.CLIENT_GONE):
        return False
    if compat.PY2:
        return getattr(exc, 'errno', None) in _ABORT_ERRNOS
    return True


class OpsHTTPServer(compat.ThreadingHTTPServer):
    """多线程 HTTP 服务：把「客户端提前断开」降级为一行（同类日志限流）。"""

    _last_abort_log = 0.0

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if is_client_abort(exc):
            now = time.time()
            if now - OpsHTTPServer._last_abort_log > _ABORT_LOG_INTERVAL:
                OpsHTTPServer._last_abort_log = now
                compat.eprint('[server] 客户端提前断开已忽略（%s），'
                              '同类日志 %d 秒内只提示一次'
                              % (exc.__class__.__name__, int(_ABORT_LOG_INTERVAL)))
            return
        compat.ThreadingHTTPServer.handle_error(self, request, client_address)


STATIC_MIME = {'.html': 'text/html; charset=utf-8',
               '.css': 'text/css; charset=utf-8',
               '.js': 'application/javascript; charset=utf-8',
               '.mjs': 'application/javascript; charset=utf-8',
               '.json': 'application/json; charset=utf-8',
               '.map': 'application/json; charset=utf-8',
               '.svg': 'image/svg+xml',
               '.png': 'image/png',
               '.jpg': 'image/jpeg',
               '.jpeg': 'image/jpeg',
               '.gif': 'image/gif',
               '.webp': 'image/webp',
               '.ico': 'image/x-icon',
               '.woff': 'font/woff',
               '.woff2': 'font/woff2',
               '.ttf': 'font/ttf',
               '.txt': 'text/plain; charset=utf-8',
               '.md': 'text/plain; charset=utf-8',
               '.csv': 'text/csv; charset=utf-8',
               '.wasm': 'application/wasm'}

_TEXT_EXTS = ('.html', '.css', '.js', '.json', '.map', '.svg', '.md', '.txt')
_TEXT_CACHE = {}
_CACHE_LIMIT = 96


def read_text_file(path, encoding='utf-8'):
    """按 mtime 缓存读取文本文件；改动后无需重启即生效"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _TEXT_CACHE.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        with compat.open_text(path, 'r', encoding=encoding) as f:
            text = f.read()
    except (OSError, IOError, UnicodeDecodeError):
        return None
    if len(_TEXT_CACHE) > _CACHE_LIMIT:
        _TEXT_CACHE.clear()
    _TEXT_CACHE[path] = (st.st_mtime, st.st_size, text)
    return text


def render(text, mapping=None):
    """替换 {{key}} 占位符"""
    if not text:
        return text
    for key, value in (mapping or {}).items():
        text = text.replace('{{%s}}' % key, compat.safe_str(value))
    return text


def guess_mime(path):
    return STATIC_MIME.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')


def find_template(template_dir, name):
    """在模板目录里找模板文件，返回绝对路径或 None"""
    for base in ([template_dir] if isinstance(template_dir, str) else list(template_dir or [])):
        if not base:
            continue
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
        # 允许 template_dir 为多个候选目录
    return None


class BaseHandler(compat.BaseHTTPRequestHandler):
    """所有工具的请求处理器：把请求交给 app.dispatch，并负责底层读写与 CORS"""

    protocol_version = 'HTTP/1.1'
    disable_nagle_algorithm = True
    app = None                      # 由 HttpApp.install() 注入
    server_version = 'opslib'
    sys_version = ''
    _responded = False              # 本连接是否已写出响应（防止异常分支二次响应污染连接）

    # ------------------------------------------------------------ 日志
    _QUIET_PREFIX = ('/api/ping', '/api/health', '/static/', '/favicon.ico', '/api/events')

    def log_message(self, fmt, *args):
        if self.app is not None and self.app.quiet:
            return
        path = getattr(self, 'path', '') or ''
        for prefix in self._QUIET_PREFIX:
            if path.startswith(prefix):
                return
        compat.log('[%s] %s - %s' % (self.log_date_time_string(), self.client_host(), fmt % args))

    def client_host(self):
        try:
            return self.client_address[0]
        except Exception:
            return '-'

    # ------------------------------------------------------------ 发送
    def _cors(self):
        app = self.app
        if app is None or not app.cors:
            return
        origin = self.headers.get('Origin') if self.headers else None
        self.send_header('Access-Control-Allow-Origin', origin or '*')
        if origin:
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, X-Auth-Token, X-Requested-With, X-Upload-Id, '
                         'X-Chunk-Index, X-Compare-Token, Range')
        self.send_header('Access-Control-Expose-Headers',
                         'Content-Length, Content-Disposition, X-Total-Count, X-Request-Id')
        self.send_header('Access-Control-Max-Age', '86400')

    def send_payload(self, status, body, content_type='application/json; charset=utf-8',
                     extra_headers=None):
        if isinstance(body, compat.text_type):
            body = compat.to_bytes(body)
        elif body is None:
            body = b''
        self._responded = True
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Request-Id', compat.rand_hex(6))
        self._cors()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_json(self, status, obj, extra_headers=None):
        self.send_payload(status, compat.json_bytes(obj), extra_headers=extra_headers)

    # ------------------------------------------------------------ 入口
    def do_OPTIONS(self):
        self._responded = True
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.send_header('Allow', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_PATCH(self):
        self._dispatch('PATCH')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def _dispatch(self, method):
        # 每个请求（同一条 keep-alive 连接上的第 N 个）都必须在开始时重置本标志：
        # 否则第一个请求正常响应后 _responded 恒为 True，第二个请求若异常会直接
        # 断连而不回 500，客户端拿到空响应。
        self._responded = False
        try:
            self.app.dispatch(self, method)
        except compat.CLIENT_GONE:
            self.close_connection = True
        except Exception:                                     # noqa: BLE001
            # 异常路径上的日志绝不能二次抛错：否则连 500 响应都发不出去，
            # 客户端只会看到连接被重置（ERR_EMPTY_RESPONSE / ERR_INVALID_RESPONSE）。
            try:
                compat.eprint('[handler] 未捕获异常:\n%s' % traceback.format_exc())
            except Exception:                                 # noqa: BLE001
                compat.eprint('[handler] 未捕获异常（日志格式化失败，已忽略）')
            if self._responded:
                # 响应已经发出（可能只发了一半）。绝不能再往同一连接补写第二个
                # HTTP 响应——浏览器会把它当成脏数据，报 ERR_INVALID_RESPONSE。
                self.close_connection = True
                return
            try:
                self.send_json(500, err('服务内部错误'))
            except Exception:
                self.close_connection = True


class HttpApp(object):
    """一个可独立运行的小型 Web 应用。

    典型用法::

        app = HttpApp('web_shell', '1.2.0', base_dir, template_dir='templates')
        app.auth = Auth(data_dir, legacy_token=token)

        @app.get('/api/status', perm='server.view', summary='主机与运行状态')
        def status(req):
            return ok({'version': app.version})

        app.run(host, port)
    """

    def __init__(self, name, version, base_dir, static_dir=None, template_dir=None,
                 data_dir=None, auth=None, cors=True, quiet=False, desc='', toc=None):
        self.name = name
        self.version = version
        self.base_dir = os.path.abspath(base_dir or '.')
        self.static_dir = os.path.abspath(static_dir) if static_dir else None
        self.template_dirs = []
        for d in ((template_dir,) if isinstance(template_dir, str) else (template_dir or ())):
            if d:
                self.template_dirs.append(os.path.abspath(d))
        self.template_dirs.append(os.path.join(self.base_dir, 'templates'))
        self.data_dir = os.path.abspath(data_dir) if data_dir else os.path.join(self.base_dir, 'data')
        self.auth = auth
        self.cors = cors
        self.quiet = quiet
        self.desc = desc
        self.router = Router()
        self.started_at = time.time()
        self.extra_state = {}
        self._server = None
        self.on_startup = []
        self.on_shutdown = []
        self._pre_routes = []
        self._super_token_perm = 'platform.view'

    # ------------------------------------------------------------ 注册
    def route(self, method, pattern, perm=None, auth=True, summary='', template=None,
              tags=None, raw=False):
        def decorator(func):
            self.router.add(method, pattern, func, perm=perm, auth=auth, summary=summary,
                            tags=tags, raw=raw)
            self.router.routes[-1]['template'] = template
            return func
        return decorator

    def get(self, pattern, **kw):
        return self.route('GET', pattern, **kw)

    def post(self, pattern, **kw):
        return self.route('POST', pattern, **kw)

    def put(self, pattern, **kw):
        return self.route('PUT', pattern, **kw)

    def delete(self, pattern, **kw):
        return self.route('DELETE', pattern, **kw)

    def page(self, pattern, template, auth=False, summary='页面'):
        """注册一个 HTML 页面路由（页面自身在浏览器端做登录态渲染）"""
        def decorator(func):
            self.router.add('GET', pattern, func, perm=None, auth=auth, summary=summary,
                            tags=['page'])
            self.router.routes[-1]['template'] = template
            return func
        return decorator

    def add_page(self, pattern, template, mapping=None, auth=False):
        """免写处理函数的页面注册：直接返回渲染后的模板"""
        def _handler(req):
            return self.render(template, mapping or {})
        self.router.add('GET', pattern, _handler, perm=None, auth=auth, summary='页面', tags=['page'])
        self.router.routes[-1]['template'] = template
        return _handler

    # ------------------------------------------------------------ 模板
    @property
    def template_dir(self):
        return self.template_dirs[0] if self.template_dirs else self.base_dir

    def template_path(self, name):
        for base in self.template_dirs:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
        return None

    def render(self, name, mapping=None, fallback=None):
        path = self.template_path(name)
        text = read_text_file(path) if path else None
        if text is None:
            if fallback is not None:
                return fallback
            return (u'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                    u'<title>模板缺失</title></head><body style="font-family:sans-serif;padding:24px">'
                    u'<h3>未找到页面模板 %s</h3><p>已查找目录：</p><ul>%s</ul>'
                    u'</body></html>') % (
                compat.to_text(compat.html_escape(name)),
                compat.to_text(''.join('<li><code>%s</code></li>' % compat.html_escape(d)
                                       for d in self.template_dirs)))
        data = {'version': self.version, 'app_name': self.name}
        data.update(mapping or {})
        return render(text, data)

    # ------------------------------------------------------------ 静态资源
    def serve_static(self, handler, req, rel):
        rel = compat.to_native(compat.urlunquote(rel)).lstrip('/')
        path = compat.safe_join(self.static_dir or self.base_dir, rel) if rel else None
        if not path or not os.path.isfile(path):
            handler.send_json(404, err('资源不存在: %s' % compat.safe_str(rel), 'not_found'))
            return
        try:
            st = os.stat(path)
        except OSError:
            handler.send_json(404, err('资源不存在', 'not_found'))
            return
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if req.headers.get('If-None-Match') == etag:
            handler.send_response(304)
            handler.send_header('ETag', etag)
            handler.send_header('Content-Length', '0')
            handler._cors()
            handler.end_headers()
            return
        ext = os.path.splitext(path)[1].lower()
        mime = guess_mime(path)
        cache = {'Cache-Control': 'no-cache', 'ETag': etag}
        if ext in _TEXT_EXTS:
            text = read_text_file(path)
            if text is None:
                handler.send_json(500, err('静态资源读取失败'))
                return
            handler.send_payload(200, text, mime, cache)
            return
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except (OSError, IOError):
            handler.send_json(500, err('静态资源读取失败'))
            return
        handler.send_payload(200, data, mime, cache)

    # ------------------------------------------------------------ 分发
    def resolve_session(self, req):
        if self.auth is None:
            return {'username': 'anonymous', 'role': 'admin', 'servers': ['*'], 'via': 'none'}
        return self.auth.resolve(req.headers, req.query)

    def dispatch(self, handler, method):
        # 路径归一到「原生串」（py2 下是字节串）：它贯穿路由匹配、错误响应与
        # 路由参数（<name> 会原样交给处理器做 % 格式化），绝不能让 unicode 漏进来。
        req = Request(self, handler, method,
                      compat.to_native(compat.urlunquote(compat.urlparse(handler.path).path)))
        path = req.path

        def fail(status, body):
            """错误响应统一出口：先排空未读请求体再响应。

            鉴权/权限/参数错误可能在处理器读取请求体之前就返回，若把未读的
            body 留在 keep-alive 连接里，下一个请求会被这段残留数据污染
            （表现为同一条连接上出现偶发的解析错乱）。排不掉就直接断开连接。
            """
            if not req.drain():
                handler.close_connection = True
            handler.send_json(status, body)

        # 1) 静态资源
        if path.startswith('/static/'):
            self.serve_static(handler, req, path[len('/static/'):])
            return

        # 2) 路由匹配
        route, params = self.router.match(method, path)
        if route is None:
            allowed = self.router.methods_for(path)
            if allowed:
                fail(405, err('方法不允许，支持: %s' % ', '.join(allowed)))
                return
            if method == 'GET' and self.static_dir and not path.startswith('/api/'):
                # 单页应用的兜底：非 /api 的未知 GET 交给 static/index.html
                index = os.path.join(self.static_dir, 'index.html')
                if os.path.isfile(index):
                    handler.send_payload(200, read_text_file(index), 'text/html; charset=utf-8',
                                         {'Cache-Control': 'no-cache'})
                    return
            fail(404, err('接口不存在: %s' % compat.safe_str(path), 'not_found'))
            return

        req.params = params or {}

        # 3) 鉴权
        if route['auth']:
            session = self.resolve_session(req)
            if not session:
                fail(401, {'status': 'error', 'code': 'unauthorized',
                           'message': '未登录或登录已过期', 'need_login': True})
                return
            req.session = session
            perm = route['perm']
            if perm and self.auth is not None and not self.auth.has_perm(session, perm):
                fail(403, {'status': 'error', 'code': 'forbidden',
                           'message': '当前角色无此权限（缺少 %s）' % perm})
                return

        # 4) 调用处理器
        try:
            result = route['func'](req)
        except ApiError as e:
            fail(e.status, e.to_body())
            return
        except compat.CLIENT_GONE:
            handler.close_connection = True
            return
        except Exception as e:                                # noqa: BLE001
            compat.eprint('[route] %s %s 处理失败:\n%s'
                          % (method, compat.safe_str(path), traceback.format_exc()))
            fail(500, err('处理失败: %s' % compat.safe_str(e)))
            return

        if result is None:
            return                                            # 处理器已自行响应
        # 响应前排空未读的请求体，防止污染 keep-alive 连接上的下一个请求
        if not req.drain():
            handler.close_connection = True
        if isinstance(result, dict):
            handler.send_json(200, result)
            return
        if isinstance(result, compat.string_types):
            handler.send_payload(200, result, 'text/html; charset=utf-8')
            return
        handler.send_json(200, ok({'data': result}))

    # ------------------------------------------------------------ OpenAPI
    def openapi(self, server_url='', title=None):
        """根据已注册路由生成 OpenAPI 3.0 描述（供 CI/CD 与外部系统对接）"""
        paths = {}
        for route in self.router.routes:
            if route['tags'] and 'page' in route['tags']:
                continue
            item = paths.setdefault(route['pattern'], {})
            params = []
            for name in compat.re.findall(r'<([a-zA-Z_][a-zA-Z0-9_]*)>', route['pattern']):
                params.append({'name': name, 'in': 'path', 'required': True,
                               'schema': {'type': 'string'}})
            auth_flag = bool(route['auth'])
            op = {
                'summary': route['summary'] or route['pattern'],
                'operationId': '%s_%s' % (route['method'].lower(),
                                          compat.re.sub(r'[^a-zA-Z0-9]+', '_', route['pattern']).strip('_')),
                'responses': {
                    '200': {'description': '成功',
                            'content': {'application/json': {
                                'schema': {'$ref': '#/components/schemas/Ok'}}}},
                    '401': {'description': '未登录'},
                    '403': {'description': '无权限'},
                },
                'parameters': params,
            }
            if auth_flag:
                op['security'] = [{'ApiToken': []}, {'SessionToken': []}]
            op['tags'] = [self.name]
            if route['method'] in ('POST', 'PUT', 'PATCH'):
                op['requestBody'] = {
                    'required': True,
                    'content': {'application/json': {'schema': {'type': 'object'}}},
                }
            item[route['method'].lower()] = op
        spec = {
            'openapi': '3.0.3',
            'info': {'title': title or ('%s v%s' % (self.name, self.version)),
                     'version': self.version, 'description': self.desc},
            'servers': [{'url': server_url or ('http://%s' % self._bind_hint())}],
            'paths': paths,
            'components': {
                'securitySchemes': {
                    'ApiToken': {'type': 'apiKey', 'in': 'header', 'name': 'X-Auth-Token',
                                 'description': '开放接口令牌（CI/CD 用），等价管理员身份'},
                    'SessionToken': {'type': 'apiKey', 'in': 'header', 'name': 'X-Auth-Token',
                                     'description': '登录后获得的会话令牌'},
                },
                'schemas': {
                    'Ok': {'type': 'object', 'properties': {
                        'status': {'type': 'string', 'example': 'success'}}},
                },
            },
        }
        return spec

    def _bind_hint(self):
        return getattr(self, 'bind_host_port', '127.0.0.1:8000')

    # ------------------------------------------------------------ 运行
    def install(self, handler_cls=None):
        cls = handler_cls or BaseHandler
        cls.app = self
        return cls

    def start(self, host, port, handler_cls=None):
        cls = self.make_handler(handler_cls)
        return cls, OpsHTTPServer((host, port), cls)

    def make_handler(self, handler_cls=None):
        app = self

        class _Handler(handler_cls or BaseHandler):
            pass
        _Handler.app = app
        return _Handler

    def run(self, host, port, banner=None, pidfile=''):
        self.bind_host_port = '%s:%d' % (host, port)
        cls = self.make_handler()
        try:
            httpd = OpsHTTPServer((host, port), cls)
        except compat.SOCKET_ERRORS as e:
            compat.log('[%s] 启动失败：无法监听 %s:%d' % (self.name, host, port))
            compat.log('    %s' % e)
            if getattr(e, 'errno', None) in (98, 48, 10048):
                compat.log('    端口被占用，可排查: ss -tlnp | grep :%d' % port)
            sys.exit(1)
        httpd.daemon_threads = True
        self._server = httpd
        compat.write_pidfile(pidfile)
        for fn in self.on_startup:
            try:
                fn()
            except Exception as e:                            # noqa: BLE001
                compat.eprint('[%s] 启动钩子失败: %s' % (self.name, e))
        for line in (banner or self.default_banner(host, port)):
            compat.log(line)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for fn in self.on_shutdown:
                try:
                    fn()
                except Exception:
                    pass
            try:
                httpd.server_close()
            except Exception:
                pass
            compat.remove_pidfile(pidfile)
        compat.log('\n[%s] 已停止。' % self.name)

    def default_banner(self, host, port):
        return [
            '=' * 66,
            '[%s] %s v%s 已启动' % (self.name, self.name, self.version),
            '浏览器访问: http://<本机IP>:%d' % port,
            '监听地址  : %s:%d' % (host, port),
            '数据目录  : %s' % self.data_dir,
            '运行环境  : Python %s' % sys.version.split()[0],
            '=' * 66,
        ]

    def stop(self):
        if self._server is not None:
            compat.start_daemon(self._server.shutdown)

    # ------------------------------------------------------------ 自检
    def probe_self(self, host='127.0.0.1', port=None):
        """自检：尝试连接自己的端口，用于启动后健康检查"""
        if port is None:
            return False
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=2)
            return True
        except Exception:
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
