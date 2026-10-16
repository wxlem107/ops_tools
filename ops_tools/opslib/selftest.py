#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib 自检脚本 —— 不依赖 pytest，直接跑：

    python -m opslib.selftest          # 全部检查
    python opslib/selftest.py          # 同上
    python opslib/selftest.py --verbose

覆盖：兼容层、存储、鉴权、加密、审计链、任务中心、探针、SSE 与 HTTP 框架端到端。
任何一项失败会以非 0 退出码结束，便于放进 CI。
"""
from __future__ import print_function

import os
import shutil
import sys
import tempfile
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from opslib import auth as opsauth            # noqa: E402
from opslib import compat, crypto, httpx, probe, server, sse, store, task   # noqa: E402
from opslib.apibase import ApiError, ok       # noqa: E402

VERBOSE = '--verbose' in sys.argv or '-v' in sys.argv
PASSED = []
FAILED = []


def _py():
    """当前解释器的绝对路径（跨平台写超时用例）"""
    return sys.executable or 'python'


def check(name, fn):
    try:
        result = fn()
        if result is False or result is None:
            raise AssertionError('校验返回假值 %r' % (result,))
        PASSED.append(name)
        print('  [OK]   %s%s' % (name, (' -> %s' % (result,)) if (VERBOSE and result) else ''))
        return True
    except Exception as e:                                     # noqa: BLE001
        import traceback
        FAILED.append((name, e))
        print('  [FAIL] %s: %s' % (name, e))
        if VERBOSE:
            print(traceback.format_exc())
        return False


def section(title):
    print('\n== %s ==' % title)


def tmpdir():
    return tempfile.mkdtemp(prefix='opslib_test_')


def _unquote_no_bytes_ok():
    """固化「py3 下不得向 urllib.parse.unquote 传 bytes」这一约束。

    py3.8 及以下 unquote 只接受 str，传 bytes 抛 TypeError('Expected str, got bytes')；
    老实现无条件 to_bytes() 导致**每个请求**都 500。这里把 unquote 包一层严格代理，
    只在 py3 生效（py2 本就该传字节串），从而在任何解释器上都拦截该类回归。
    """
    if compat.PY2:
        return True                      # py2 走字节串分支，语义不同
    orig = compat._urllib.unquote
    seen = []

    def strict(s, *a, **k):
        seen.append(type(s))
        if isinstance(s, bytes):
            raise TypeError('Expected str, got bytes')
        return orig(s, *a, **k)

    compat._urllib.unquote = strict
    try:
        got = compat.urlunquote('%E4%B8%AD%E6%96%87%2F%E8%B7%AF%E5%BE%84')
    finally:
        compat._urllib.unquote = orig
    return compat.to_text(got) == u'中文/路径' and bytes not in seen


# ---------------------------------------------------------------- 各模块检查
def test_compat():
    section('compat 兼容层')
    check('文本/字节互转', lambda: compat.to_text(compat.to_bytes('中文abc')) == '中文abc')
    check('安全字符串', lambda: compat.safe_str(None) == '')
    check('容量格式化', lambda: compat.fmt_size(1536) == '1.5 KB')
    check('时长格式化', lambda: compat.fmt_duration(3725) == '1h2m5s')
    check('磁盘容量', lambda: compat.disk_usage(os.getcwd())[0] > 0)
    check('路径越界判断', lambda: compat.is_within(os.getcwd(), os.getcwd()))
    check('安全拼接拒绝 ..', lambda: compat.safe_join(os.getcwd(), '../etc/passwd') is None)
    check('命令执行', lambda: compat.run_cmd('echo hello', timeout=5)['stdout'].strip() == 'hello')
    check('命令超时', lambda: compat.run_cmd('%s -c "import time; time.sleep(5)"' % _py(),
                                          timeout=1)['timeout'] is True)
    check('随机串长度', lambda: len(compat.rand_hex(8)) == 16)
    check('定时安全比较', lambda: compat.constant_time_eq('a' * 32, 'a' * 32) and
          not compat.constant_time_eq('a' * 32, 'b' * 32))
    check('JSON 宽松解析', lambda: compat.json_loads(b'{"a":1}')['a'] == 1)
    # 请求路径（urlunquote 产物）必须是「原生串」：py2 下若是 unicode，
    # '接口不存在: %s' % path 会先按 ascii 解码中文字面量并抛 UnicodeDecodeError，
    # 表现为任意 404（含 favicon / 静态资源缺失）都变成 500。
    check('URL 解码返回原生串', lambda: isinstance(
        compat.urlunquote('%E4%B8%AD%E6%96%87'), compat.NATIVE_STR))
    check('URL 解码内容正确', lambda: compat.to_text(
        compat.urlunquote('%E4%B8%AD%E6%96%87')) == u'中文')
    # py3.8 及以下专属：urllib.parse.unquote 只接受 str，老实现无条件 to_bytes()
    # 会抛 TypeError('Expected str, got bytes')，导致**每个请求**（含首页 GET /）
    # 在 dispatch 阶段 500（3.9+ 放宽了类型检查故不复现，极难察觉）。
    # 这里把 unquote 换成「严格版」以在任何 py3 上固化该约束。
    check('URL 解码不向 unquote 传 bytes（兼容 py3.8）', _unquote_no_bytes_ok)
    check('并行任务', lambda: sorted(x[1] for x in
                                    compat.parallel_map(lambda i: i * 2, [1, 2, 3], workers=3)) == [2, 4, 6])
    # merge_dict 是 {**a, **b} 的跨版本替代品（py2 的 dict(a, **b) 对 unicode 键不安全），
    # 平台「保存设置 / 台账人工字段合并」都依赖它；曾因该函数缺失导致保存设置 500。
    check('字典浅合并', lambda: compat.merge_dict({'a': 1, 'b': 2}, {'b': 3}) == {'a': 1, 'b': 3})
    def _merge_immutable():
        a, b = {'a': 1}, {'b': 2}
        compat.merge_dict(a, b)
        return a == {'a': 1} and b == {'b': 2}

    check('字典合并不改入参', _merge_immutable)
    check('字典合并容错空值', lambda: compat.merge_dict(None, None) == {}
          and compat.merge_dict(None, {'a': 1}) == {'a': 1}
          and compat.merge_dict({'a': 1}, None) == {'a': 1})
    check('字典合并支持 unicode 键值', lambda: compat.to_text(
        compat.merge_dict({}, {u'名称': u'中文'})[u'名称']) == u'中文')

    # 回归（线上级）：py2 的 `json.dumps(obj, ensure_ascii=False)` 对字符串值是
    # 「unicode 进 → unicode 片段、字节串进 → 字节片段」，收尾的 `''.join(chunks)`
    # 一旦混排就会按 ascii 解码字节片段：
    #     UnicodeDecodeError: 'ascii' codec can't decode byte 0xe5 in position 1
    # 发布单里 step['message']（u'' 字面量）与 step['logs']（safe_str 落成的 UTF-8
    # 字节串）正好同处一个 dict → 打开详情 500、订单落盘也写不进去。见 compat.json_safe。
    def _json_mixed_types_ok():
        payload = {
            'message': u'已上传至 /opt/发布/主程序.tgz',                     # py2: unicode
            'logs': [compat.safe_str(u'已上传 主程序.tgz -> /opt/发布/')],   # py2: 字节串
            'name': u'主程序.tgz'.encode('utf-8'),                          # 字节串形态
        }
        back = compat.json_loads(compat.json_bytes(payload), {}) or {}
        return (compat.to_text(back.get('message')) == u'已上传至 /opt/发布/主程序.tgz'
                and [compat.to_text(x) for x in (back.get('logs') or [])]
                == [u'已上传 主程序.tgz -> /opt/发布/']
                and compat.to_text(back.get('name')) == u'主程序.tgz')

    check('JSON 序列化前归一为文本（字节串/unicode 混排不炸）', _json_mixed_types_ok)
    # 兜底：不认识的对象（set / datetime / 自定义类）不再让整个接口 500
    check('JSON 序列化兜底未知类型', lambda: isinstance(
        compat.json_loads(compat.json_bytes({'items': set([1])}), {}).get('items'),
        compat.NATIVE_STR))


def test_store():
    section('store 持久化')
    d = tmpdir()
    try:
        js = store.JsonStore(os.path.join(d, 'data.json'), {'n': 0})
        js.update(lambda data: data.__setitem__('n', data['n'] + 1))
        check('原子写入与读取', lambda: store.read_json(os.path.join(d, 'data.json'))['n'] == 1)
        check('外部改动感知', lambda: (store.atomic_write_json(os.path.join(d, 'data.json'), {'n': 9}) or
                                  js.load(force=True)['n'] == 9))
        jl = store.JsonlLog(os.path.join(d, 'log.jsonl'))
        for i in range(5):
            jl.append({'i': i})
        check('追加日志读取', lambda: [e['i'] for e in jl.iter_all()] == [0, 1, 2, 3, 4])
        check('尾部读取', lambda: [e['i'] for e in jl.tail(2)] == [4, 3])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_auth():
    section('auth 用户与授权')
    d = tmpdir()
    try:
        au = opsauth.Auth(d, legacy_token='CI-TOKEN', admin_password='admin123456')
        check('默认管理员已创建', lambda: au.user_get('admin') is not None)
        check('口令摘要校验', lambda: opsauth.verify_password('admin123456', au.user_get('admin')['password']))
        check('错误口令拒绝', lambda: not opsauth.verify_password('wrong', au.user_get('admin')['password']))
        check('弱口令拒绝', lambda: au.user_add('u1', '123', 'viewer')[0] is False)
        check('创建只读用户', lambda: au.user_add('viewer1', 'viewer12345', 'viewer', ['web-01'])[0])
        check('用户重名拒绝', lambda: au.user_add('viewer1', 'viewer12345', 'viewer')[0] is False)
        login = au.login('viewer1', 'viewer12345', '10.0.0.9')
        check('登录成功', lambda: login['ok'] is True)
        sess = au.resolve({'X-Auth-Token': login['token']}, {})
        check('会话解析', lambda: sess and sess['username'] == 'viewer1')
        check('只读角色权限', lambda: au.has_perm(sess, 'server.view') and not au.has_perm(sess, 'cmd.exec'))
        check('服务器白名单', lambda: au.can_access_server(sess, 'web-01') and
              not au.can_access_server(sess, 'db-01'))
        check('API Token 等价管理员', lambda: au.resolve({'X-Auth-Token': 'CI-TOKEN'}, {})['role'] == 'admin')
        check('无效令牌拒绝', lambda: au.resolve({'X-Auth-Token': 'bad'}, {}) is None)
        check('登出后失效', lambda: (au.logout(login['token']),
                                au.resolve({'X-Auth-Token': login['token']}, {}) is None)[1])
        check('删用户保留管理员', lambda: au.user_delete('admin')[0] is False)
        check('改密踢会话', lambda: (au.login('viewer1', 'viewer12345', ''),
                                 au.user_update('viewer1', {'password': 'newpass12345'})[0] and
                                 au.resolve({'X-Auth-Token': 'x'}, {}) is None))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_crypto():
    section('crypto 加密')
    check('加解密往返', lambda: crypto.decrypt(crypto.encrypt('秘密-s3cret', 'pw123456'), 'pw123456') == '秘密-s3cret')
    check('错误口令报错', lambda: _raises(lambda: crypto.decrypt(crypto.encrypt('x', 'pw1'), 'pw2')))
    check('篡改可检测', lambda: _raises(lambda: _tamper_check()))
    check('空串处理', lambda: crypto.decrypt(crypto.encrypt('', 'pw'), 'pw') == '')
    d = tmpdir()
    try:
        vault = crypto.Vault(d, master_password='master-key')
        blob = vault.seal({'host': '1.1.1.1', 'password': 'p@ss'}, ['password'])
        check('保险箱加密字段', lambda: blob['password'].startswith('enc:'))
        check('保险箱解密', lambda: vault.open(blob, ['password'])['password'] == 'p@ss')
        check('脱敏展示', lambda: crypto.mask('abcdefgh') == 'ab****gh')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _tamper_check():
    import base64
    token = crypto.encrypt('important', 'pw')
    raw = bytearray(base64.b64decode(compat.to_bytes(token)))
    raw[-1] = raw[-1] ^ 0x01
    return crypto.decrypt(base64.b64encode(bytes(raw)).decode('ascii') if not compat.PY2
                          else base64.b64encode(bytes(raw)), 'pw')


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def test_audit():
    section('audit 审计链')
    d = tmpdir()
    try:
        log = audit_log(os.path.join(d, 'audit.jsonl'))
        log.write('admin', '10.0.0.1', 'cmd.exec', 'web-01', 'uptime')
        log.write('ops', '10.0.0.2', 'file.upload', 'web-01', 'app.jar')
        log.write('ops', '10.0.0.2', 'cmd.exec', 'web-02', 'df -h', result='failed')
        check('写入条数', lambda: log.query(limit=10)['total'] == 3)
        check('按动作过滤', lambda: log.query(action='cmd.exec')['total'] == 2)
        check('关键字搜索', lambda: log.query(keyword='app.jar')['total'] == 1)
        check('失败过滤', lambda: log.query(result='failed')['total'] == 1)
        okv, msg, count, _ = log.verify()
        check('哈希链完整', lambda: okv and count == 3)
        check('统计概览', lambda: log.stats()['total'] == 3)
        # 篡改中间一条，链应断裂
        path = os.path.join(d, 'audit.jsonl')
        with compat.open_text(path, 'r') as f:
            lines = f.readlines()
        lines[1] = lines[1].replace('app.jar', 'evil.jar')
        with compat.open_text(path, 'w') as f:
            f.writelines(lines)
        log2 = audit_log(path)
        okv2, msg2, _, _ = log2.verify()
        check('篡改可检出', lambda: okv2 is False)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def audit_log(path):
    from opslib.audit import AuditLog
    return AuditLog(path)


def test_task():
    section('task 任务中心')
    d = tmpdir()
    try:
        bus = sse.EventBus()
        tm = task.TaskManager(os.path.join(d, 'tasks.json'), max_workers=2, bus=bus)

        def job(ctx):
            for i in range(1, 4):
                ctx.check_canceled()
                ctx.progress(i * 33, 'step %d' % i)
                ctx.log('处理第 %d 步' % i)
                time.sleep(0.05)
            return {'rows': 3}

        submitted = tm.submit('测试任务', 'test', job, user='admin', target='web-01')
        check('任务已提交', lambda: submitted['state'] in ('pending', 'running'))
        finished = tm.wait(submitted['id'], timeout=10)
        check('任务执行成功', lambda: finished['state'] == 'success')
        check('结果回传', lambda: finished['result'].get('rows') == 3)
        check('进度已到 100', lambda: finished['percent'] == 100)
        check('日志已记录', lambda: len(tm.logs(submitted['id'])) >= 3)

        def boom(ctx):
            raise RuntimeError('故意失败')

        failed = tm.wait(tm.submit('失败任务', 'test', boom)['id'], timeout=10)
        check('失败被捕获', lambda: failed['state'] == 'failed' and '故意失败' in (failed['error'] or ''))

        def slow(ctx):
            for _ in range(30):
                ctx.check_canceled()
                time.sleep(0.1)

        slow_id = tm.submit('可取消任务', 'test', slow)['id']
        time.sleep(0.3)
        check('取消请求', lambda: tm.cancel(slow_id)[0])
        check('任务已取消', lambda: tm.wait(slow_id, timeout=10)['state'] == 'canceled')
        check('历史列表', lambda: len(tm.list(limit=10)) >= 3)
        check('统计', lambda: tm.stats()['total'] >= 3)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_probe():
    section('probe 主机探针')
    check('系统画像', lambda: bool(probe.sys_info().get('hostname')))
    check('内存容量可读', lambda: probe.sys_info().get('mem_total', 0) > 0)
    check('实时指标', lambda: 'cpu_percent' in probe.metrics())
    probe.metrics()
    time.sleep(0.25)
    m = probe.metrics()
    check('CPU 采样有效', lambda: 0 <= m['cpu_percent'] <= 100)
    check('磁盘列表', lambda: isinstance(m['disks'], list))
    check('网络累计值', lambda: m['net']['rx'] >= 0)
    check('TOP 进程', lambda: len(probe.top_processes(5)) > 0)
    check('进程列表', lambda: len(probe.process_list()) > 0)
    check('监听端口', lambda: isinstance(probe.listen_ports(), list))
    check('健康判定', lambda: isinstance(probe.health_rules(m), list))


def test_sse_bus():
    section('sse 事件总线')
    bus = sse.EventBus()
    sub = bus.subscribe('demo', history=1)
    check('发布/订阅', lambda: (bus.publish('demo', {'v': 1}) == 1, sub.get(1)['data']['v'] == 1)[1])
    check('订阅者计数', lambda: bus.subscriber_count('demo') == 1)
    bus.unsubscribe(sub)
    check('退订生效', lambda: bus.subscriber_count('demo') == 0)


def test_http_end_to_end():
    section('server 端到端')
    d = tmpdir()
    static_dir = os.path.join(d, 'static')
    tmpl_dir = os.path.join(d, 'templates')
    compat.makedirs(static_dir)
    compat.makedirs(tmpl_dir)
    with compat.open_text(os.path.join(static_dir, 'index.html'), 'w') as f:
        f.write('<!DOCTYPE html><html><body>INDEX-CONTENT-9.9.9</body></html>')
    with compat.open_text(os.path.join(static_dir, 'app.js'), 'w') as f:
        f.write('console.log(1)')
    with compat.open_text(os.path.join(tmpl_dir, 'hello.html'), 'w') as f:
        f.write('<h1>hello {{name}} v{{version}}</h1>')

    au = opsauth.Auth(os.path.join(d, 'auth'), legacy_token='TOKEN-1', admin_password='admin123456')
    au.user_add('ro', 'viewer12345', 'viewer')

    app = server.HttpApp('testapp', '9.9.9', d, static_dir=static_dir, template_dir=tmpl_dir,
                         data_dir=os.path.join(d, 'data'), auth=au, desc='自检用')
    app.add_page('/', 'hello.html', {'name': 'world'})

    @app.get('/api/ping', auth=False, summary='存活探测')
    def ping(req):
        return ok({'version': app.version})

    @app.get('/api/secret', perm='server.view', summary='需要登录')
    def secret(req):
        return ok({'user': req.user, 'ip': req.client_ip})

    @app.get('/api/boom', summary='抛错')
    def boom(req):
        raise ApiError('故意报错', 418, 'teapot')

    @app.get('/api/echo', summary='回显参数')
    def echo(req):
        return ok({'q': req.q('q'), 'n': req.qi('n', 0, 0, 10), 'params': req.params})

    @app.get('/api/stream', auth=False, summary='SSE 流')
    def stream(req):
        def gen():
            for i in range(3):
                yield {'i': i}
        req.sse().pump(gen(), heartbeat=5)

    @app.get('/api/openapi.json', auth=False, summary='接口描述')
    def spec(req):
        return app.openapi()

    cls = app.make_handler()
    httpd = compat.ThreadingHTTPServer(('127.0.0.1', 0), cls)
    port = httpd.server_address[1]
    compat.start_daemon(httpd.serve_forever)
    base = 'http://127.0.0.1:%d' % port
    try:
        check('页面模板渲染', lambda: 'hello world v9.9.9' in httpx.get(base + '/').text)
        check('静态资源', lambda: 'INDEX-CONTENT-9.9.9' in httpx.get(base + '/static/index.html').text)
        check('静态 JS 类型', lambda: 'javascript' in httpx.get(base + '/static/app.js').header('Content-Type'))
        check('静态路径穿越拦截', lambda: httpx.get(base + '/static/../opslib/__init__.py').status == 404)
        check('免鉴权接口', lambda: httpx.get(base + '/api/ping').json()['version'] == '9.9.9')
        check('未登录拒绝', lambda: httpx.get(base + '/api/secret').status == 401)
        check('API Token 通过', lambda: httpx.get(base + '/api/secret',
                                               headers={'X-Auth-Token': 'TOKEN-1'}).json()['user'] == '__api__')
        ro = au.login('ro', 'viewer12345', '')
        check('只读会话读取', lambda: httpx.get(base + '/api/secret',
                                             headers={'X-Auth-Token': ro['token']}).status == 200)
        check('业务异常状态码', lambda: httpx.get(base + '/api/boom',
                                              headers={'X-Auth-Token': 'TOKEN-1'}).status == 418)
        check('错误体格式', lambda: httpx.get(base + '/api/boom',
                                            headers={'X-Auth-Token': 'TOKEN-1'}).json()['code'] == 'teapot')
        check('未知接口 404', lambda: httpx.get(base + '/api/nope').status == 404)
        check('错误方法 405', lambda: httpx.post(base + '/api/ping').status == 405)
        check('OPTIONS 预检', lambda: httpx.DEFAULT.request('OPTIONS', base + '/api/ping').status == 204)
        check('CORS 头', lambda: httpx.get(base + '/api/ping').header('Access-Control-Allow-Origin') == '*')
        check('路径参数与查询', lambda: httpx.get(
            base + '/api/echo?q=abc&n=99',
            headers={'X-Auth-Token': 'TOKEN-1'}).json()['n'] == 10)
        check('SSE 事件流', lambda: _read_sse(base + '/api/stream'))
        spec_data = httpx.get(base + '/api/openapi.json').json()
        check('OpenAPI 生成', lambda: spec_data['openapi'] == '3.0.3' and
              '/api/secret' in spec_data['paths'])
        check('OpenAPI 安全方案', lambda: 'ApiToken' in spec_data['components']['securitySchemes'])
    finally:
        httpd.shutdown()
        shutil.rmtree(d, ignore_errors=True)


def _read_sse(url):
    """用原始 socket 读一段 SSE，确认事件能正常流出"""
    import socket as _socket
    parsed = compat.urlparse(url)
    sock = _socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(b'GET /api/stream HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n')
        sock.settimeout(3)
        buf = b''
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            if buf.count(b'data:') >= 3:
                break
        return b'event-stream' in buf and b'"i": 0' in buf
    finally:
        try:
            sock.close()
        except Exception:
            pass


class _SSESink(object):
    """把 SSEStream 写出的字节收进内存（用于在自检里拿到「服务端原样会发的帧」）"""

    def __init__(self):
        import io
        self.wfile = io.BytesIO()
        self.close_connection = False

    def send_response(self, *a, **kw):
        pass

    def send_header(self, *a, **kw):
        pass

    def end_headers(self):
        pass

    def _cors(self):
        pass


def _serve_raw(body, status=200, keep_open=0):
    """起一个只吐固定字节的 HTTP 服务，返回 (base, httpd)。用于消费端解析回归。

    keep_open>0：写完 body 后**挂住不关连接**（也不再发任何字节），用来模拟
    「半死连接」。注意早期版本这里无脑 close_connection=True —— 连接立刻被关，
    客户端收到的是 EOF 而不是空闲，idle_timeout 那条用例等于没测到。
    """

    class H(compat.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(status)
            if status == 200:
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            if status == 200:
                self.wfile.write(body)
            self.wfile.flush()
            if keep_open:
                try:
                    time.sleep(keep_open)     # 半死：连上但一直不说话
                except Exception:
                    pass
            self.close_connection = True

    httpd = compat.ThreadingHTTPServer(('127.0.0.1', 0), H)
    port = httpd.server_address[1]
    compat.start_daemon(httpd.serve_forever)
    return 'http://127.0.0.1:%d' % port, httpd


def test_sse_consume():
    section('SSE 客户端消费（上游 exec/stream 形状）')
    # 用真实的 SSEStream 产出帧（与目标机 web_shell 的 /api/exec/stream 同一套写法）：
    #   start(json) → out(多行中文) → 心跳注释 → exit(json) → close
    sink = _SSESink()
    stream = sse.SSEStream(sink)
    stream.ready()
    stream.json({'cwd': '/tmp', 'cmd': 'ls -l', 'timeout': 5, 'ts': 1}, event='start')
    stream.send(u'上传完成 100%\n正在解包 app/\n', event='out')
    stream.comment('ping 1')
    stream.json({'returncode': 0, 'elapsed': 1.25, 'truncated': False, 'timed_out': False},
                event='exit')
    stream.close()
    body = sink.wfile.getvalue()
    check('SSEStream 产出 chunked 分帧', lambda: b'\r\n' in body and b'event: start' in body)

    events = []
    base, httpd = _serve_raw(body)
    try:
        ok, err = sse.consume(base + '/api/exec/stream?cmd=ls&timeout=5',
                              headers={'X-Auth-Token': 'tok'},
                              on_event=lambda n, d: events.append((n, d)), timeout=5)
        check('sse.consume 正常读到流末', lambda: ok is True)
        check('sse.consume 事件顺序 start/out/exit/close',
              lambda: [n for n, _ in events] == ['start', 'out', 'exit', 'close'])
        check('sse.consume 解析 start 事件 JSON',
              lambda: compat.json_loads(
                  [d for n, d in events if n == 'start'][0], {}).get('cmd') == 'ls -l')
        check('sse.consume 把多行 data 拼回一个事件（脚本输出不被拆散）',
              lambda: [d for n, d in events if n == 'out'][0]
              == u'上传完成 100%\n正在解包 app/\n')
        check('sse.consume 中文不乱码',
              lambda: u'上传完成' in [d for n, d in events if n == 'out'][0])
        check('sse.consume 解析 exit 事件 JSON',
              lambda: compat.json_loads(
                  [d for n, d in events if n == 'exit'][0], {}).get('returncode') == 0)
    finally:
        httpd.shutdown()

    # 非 200：调用方据此降级到同步接口
    base2, httpd2 = _serve_raw(b'', status=404)
    try:
        bad = sse.consume(base2 + '/api/exec/stream', timeout=5)
        check('sse.consume 上游非 200 时返回 ok=False（触发降级）',
              lambda: bad[0] is False and bad[1] == 'HTTP 404')
    finally:
        httpd2.shutdown()

    # 半死连接：只发一个事件后不再有任何字节 → idle_timeout 主动结束，不把线程挂住
    sink2 = _SSESink()
    st2 = sse.SSEStream(sink2)
    st2.ready()
    st2.json({'cmd': 'hang'}, event='start')
    base3, httpd3 = _serve_raw(sink2.wfile.getvalue(), keep_open=4)
    try:
        t0 = time.time()
        idle = sse.consume(base3 + '/api/exec/stream', timeout=30, idle_timeout=1)
        check('sse.consume 半死连接由 idle_timeout 收尾',
              lambda: idle[0] is True and idle[1] == 'idle-timeout')
        check('sse.consume idle_timeout 及时返回（未等到 socket 超时）',
              lambda: time.time() - t0 < 5)
    finally:
        httpd3.shutdown()


def test_api_surface():
    section('公共层接口面（静态扫描）')
    # 回归：platform_console/ops_Platform.py 曾调用 compat.merge_dict，但该函数从未定义，
    # 只有走到「保存设置」这条冷路径才会 500（集成测试当时也没覆盖）。静态扫描更可靠。
    import ast
    import re

    # py3 的字符串字面量是 ast.Constant，py2 是 ast.Str；按版本二选一
    # （py3.12+ 访问 ast.Str 会发 DeprecationWarning，故不做两路拼装）
    _STR_NODES = (ast.Constant,) if hasattr(ast, 'Constant') else (ast.Str,)

    missing = {}

    def _scan(path):
        try:
            with compat.open_text(path, 'r') as f:
                text = f.read()
            tree = ast.parse(text)
        except Exception:                                      # noqa: BLE001
            return
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == 'compat' and not hasattr(compat, node.attr)):
                missing.setdefault(node.attr, set()).add(os.path.relpath(path, _BASE))

    for root, dirs, files in os.walk(_BASE):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.testlogs', '.git')]
        for fn in files:
            if fn.endswith('.py'):
                _scan(os.path.join(root, fn))

    def _no_missing():
        if missing:
            raise AssertionError('引用了 opslib.compat 上不存在的名字: %s'
                                 % dict((k, sorted(v)) for k, v in missing.items()))
        return True

    check('compat 引用均有定义', _no_missing)

    # 回归（线上级）：Windows 的 `if cond cmd1 & cmd2` 里 cmd1/cmd2 同属该 if 的语句体，
    # 条件为假时**后面的命令一并被跳过**。`if not exist {备份目录} mkdir ... & robocopy ...`
    # 在二次发布（目录已存在）时会静默不备份、回滚命令在目标目录已存在时静默不还原，
    # 却都报成功。修法是给每个 if 各自加括号。这里做静态兜底：命令模板字符串里凡是
    # `if not exist` 与 `&` 同行的，if 必须被括号包住。
    bad_if = []

    def _scan_cmd_strings(path):
        try:
            with compat.open_text(path, 'r') as f:
                tree = ast.parse(f.read())
        except Exception:                                      # noqa: BLE001
            return
        for node in ast.walk(tree):
            if not isinstance(node, _STR_NODES):
                continue
            for line in compat.to_text(getattr(node, 'value', '') or '').splitlines():
                if 'if not exist' not in line or '&' not in line:
                    continue
                if not re.search(r'\(\s*if not exist', line):
                    bad_if.append((os.path.relpath(path, _BASE), line.strip()[:110]))

    for root, dirs, files in os.walk(_BASE):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.testlogs', '.git')]
        for fn in files:
            if fn.endswith('.py'):
                _scan_cmd_strings(os.path.join(root, fn))

    def _no_swallowed_if():
        if bad_if:
            raise AssertionError('Windows 命令里的 if 未加括号（条件为假时后续命令会被 cmd 整行跳过）: %s'
                                 % bad_if)
        return True

    check('Windows 命令模板的 if 已加括号（不被 cmd 吞掉）', _no_swallowed_if)

    # 回归（线上级）：Request.parsed = urlparse(handler.path[:qpos]) 只含路径，
    # dispatch 把 query 剥掉是为了路由匹配 —— 所以 parsed 里的查询串**恒为空**，
    # 真正的查询串在 req.raw_query。转发类接口若用 req.parsed.query 拼 URL，
    # 查询串会被静默丢弃：发布中心的 ?app= 过滤、转发到目标机的 ?page= 全部
    # 「设置无效还不报错」，排查成本极高。这里做静态兜底。
    bad_q = []

    def _scan_parsed_query(path):
        try:
            with compat.open_text(path, 'r') as f:
                tree = ast.parse(f.read())
        except Exception:                                      # noqa: BLE001
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == 'query':
                inner = node.value
                if (isinstance(inner, ast.Attribute) and inner.attr == 'parsed'
                        and isinstance(inner.value, ast.Name) and inner.value.id == 'req'):
                    bad_q.append('%s:%s' % (os.path.relpath(path, _BASE), node.lineno))

    for root, dirs, files in os.walk(_BASE):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.testlogs', '.git')]
        for fn in files:
            if fn.endswith('.py'):
                _scan_parsed_query(os.path.join(root, fn))

    def _no_parsed_query():
        if bad_q:
            raise AssertionError('转发类接口用了 req.parsed.query（恒为空串，查询参数会被静默丢弃），'
                                 '请改用 req_query(req) / req.raw_query: %s' % bad_q)
        return True

    check('转发接口不使用 req.parsed.query（查询串不会被丢）', _no_parsed_query)

    # 回归（线上级）：平台代理原先复用 release_proxy → req.raw_body() 默认 32MB 上限，
    # 且整包读进内存。制品包上传必须走流式透传 release_upload_proxy。
    plat_src = ''
    try:
        with compat.open_text(os.path.join(_BASE, 'platform_console', 'ops_Platform.py'), 'r') as f:
            plat_src = f.read()
    except Exception:                                         # noqa: BLE001
        pass

    def _upload_streams():
        if 'def release_upload_proxy' not in plat_src:
            raise AssertionError('platform_console/ops_Platform.py 缺少 release_upload_proxy')
        if 'packages/upload' not in plat_src:
            raise AssertionError('未把 packages/upload 指向流式透传（大包会被 32MB 上限挡掉）')
        if 'max_mb=8192' not in plat_src:
            raise AssertionError('流式上传未放宽 stream_body 上限')
        return True

    check('制品包上传走流式透传（不受 32MB 上限约束）', _upload_streams)

    # 回归（线上级）：py2 的 `json.dumps(obj, ensure_ascii=False)` 在「unicode 与含中文的
    # 字节串混排」时必抛 UnicodeDecodeError（发布单详情 500 / 落盘失败），且**只在 py2 线上
    # 发作**、py3 测试完全测不出来。唯一安全入口是 compat.json_bytes（内部先 json_safe 归一）。
    # 这里做静态兜底：生产代码里不允许再出现裸的 json.dumps/json.dump + ensure_ascii=False。
    raw_dumps = []
    _PROD_DIRS = ('opslib', 'platform_console', 'tools')

    def _scan_raw_dumps(path):
        try:
            with compat.open_text(path, 'r') as f:
                tree = ast.parse(f.read())
        except Exception:                                      # noqa: BLE001
            return
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in ('dumps', 'dump')):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == 'json'):
                continue
            for kw in (node.keywords or []):
                if kw.arg == 'ensure_ascii' and getattr(kw.value, 'value', None) is False:
                    raw_dumps.append('%s:%d' % (os.path.relpath(path, _BASE), node.lineno))

    for _d in _PROD_DIRS:
        for root, dirs, files in os.walk(os.path.join(_BASE, _d)):
            dirs[:] = [x for x in dirs if x not in ('__pycache__', '.testlogs', '.git')]
            for fn in files:
                if fn.endswith('.py'):
                    _scan_raw_dumps(os.path.join(root, fn))

    def _no_raw_dumps():
        if raw_dumps:
            raise AssertionError('裸用 json.dumps/json.dump(ensure_ascii=False)（py2 下 '
                                 'unicode 与字节串混排必抛 UnicodeDecodeError），'
                                 '请改用 compat.json_bytes: %s' % raw_dumps)
        return True

    check('JSON 序列化统一走 compat.json_bytes（无裸 dumps）', _no_raw_dumps)


def test_server_robustness():
    section('server 日志降噪')
    # 「客户端提前断开」（关页签 / SSE 断流 / curl 取消）不是服务端故障，
    # 否则生产日志被整段 traceback 刷屏，真正的故障反而看不见。
    if compat.PY2:
        import socket as _socket
        aborts = [_socket.error(104, 'reset'), _socket.error(32, 'broken pipe')]
        normals = [_socket.error(2, 'no such file'), IOError('io boom')]
    else:
        aborts = [ConnectionResetError('reset'), ConnectionAbortedError('abort'),
                  BrokenPipeError('broken pipe')]
        normals = [ValueError('boom'), OSError(2, 'no such file')]
    check('识别客户端断开', lambda: all(server.is_client_abort(e) for e in aborts))
    check('真实错误不被降噪', lambda: not any(server.is_client_abort(e) for e in normals))


def test_http_pool_timeout():
    """连接池复用连接时必须重设套接字超时（v1.0.5 修复）。

    回归背景：池里的连接沿用「创建它那次请求」的 timeout。一次短超时探测（平台巡检
    用 timeout=2）建的 keep-alive 连接被后续长请求复用时，http.client 不会刷新 socket
    超时 —— 几秒后读超时，对外表现为**莫名 status=0 的请求失败**。现场「编辑后保存 /
    列表刷新失败」正是被它放大：刷新要探测离线机（慢），却复用了探测用的短超时连接。
    """
    section('httpx 连接池超时复用')

    class SlowHandler(compat.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith('/slow'):
                time.sleep(1.5)
            body = compat.to_bytes('ok')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = compat.ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
    compat.start_daemon(httpd.serve_forever)
    base = 'http://127.0.0.1:%d' % httpd.server_address[1]
    client = httpx.Client(timeout=0.4, pool_max=2, retries=0)
    try:
        r1 = client.get(base + '/fast')          # 建连时 socket 超时 = 0.4s
        check('短超时建连的首个请求成功', lambda: r1.ok)
        r2 = client.get(base + '/slow', timeout=10)   # 复用同一连接 → 必须重设超时
        check('复用连接的长请求不被旧短超时打断',
              lambda: r2.ok and r2.text == 'ok')
    finally:
        client.close()
        httpd.shutdown()


def main():
    print('opslib 自检 (Python %s)' % sys.version.split()[0])
    started = time.time()
    test_compat()
    test_store()
    test_auth()
    test_crypto()
    test_audit()
    test_task()
    test_probe()
    test_sse_bus()
    test_sse_consume()
    test_http_end_to_end()
    test_http_pool_timeout()
    test_api_surface()
    test_server_robustness()
    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项，用时 %.1fs' % (len(PASSED), len(FAILED), time.time() - started))
    if FAILED:
        print('\n失败明细:')
        for name, err in FAILED:
            print('  - %s: %s' % (name, err))
        return 1
    print('全部通过 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
