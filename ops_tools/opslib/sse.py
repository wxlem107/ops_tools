#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.sse —— Server-Sent Events 实时通道。

用途：把原先靠前端轮询的场景（日志输出、命令回显、上传进度、监控曲线、任务进度）
改成服务端推送，一次连接持续接收。

实现要点：
  * 采用 chunked 编码分帧，兼容 HTTP/1.1 与常见反向代理；
  * 心跳注释行避免中间设备（Nginx/负载均衡）因空闲超时断链；
  * 客户端断开时静默结束，不刷错误日志；
  * EventBus 提供「多订阅者广播」，适合监控曲线/任务进度这类一对多推送。
"""
from __future__ import print_function

import socket as _socket
import time

from . import compat

SSE_HEADERS = {
    'Cache-Control': 'no-cache, no-store, no-transform',
    'X-Accel-Buffering': 'no',          # Nginx 不缓冲
    'Connection': 'keep-alive',
}


class SSEStream(object):
    """单条 SSE 连接。

    用法::

        stream = req.sse()
        stream.ready()
        for line in follow_log():
            stream.send(line, event='log')
        stream.close()
    """

    def __init__(self, handler, retry=3000):
        self.handler = handler
        self.retry = retry
        self._ready = False
        self._closed = False
        self._seq = 0

    # ------------------------------------------------------------ 生命周期
    def ready(self):
        if self._ready:
            return
        handler = self.handler
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        for key, value in SSE_HEADERS.items():
            handler.send_header(key, value)
        handler.send_header('Transfer-Encoding', 'chunked')
        handler._cors()
        handler.end_headers()
        self._ready = True
        self._write('retry: %d\n\n' % self.retry)

    def _write(self, text):
        """按 chunked 分帧写出一段文本；失败（客户端断开）时标记关闭"""
        if self._closed:
            return False
        try:
            data = compat.to_bytes(text)
            self.handler.wfile.write(('%x\r\n' % len(data)).encode('ascii'))
            self.handler.wfile.write(data)
            self.handler.wfile.write(b'\r\n')
            try:
                self.handler.wfile.flush()
            except Exception:
                pass
            return True
        except compat.CLIENT_GONE:
            self._closed = True
            return False
        except Exception:
            self._closed = True
            return False

    # ------------------------------------------------------------ 发送
    def send(self, data, event=None, id=None, raw=False):
        """发送一个事件；data 为多行文本时会自动拆成多个 data: 行"""
        if not self._ready:
            self.ready()
        buf = []
        if id is not None:
            buf.append('id: %s\n' % compat.safe_str(id))
        if event:
            buf.append('event: %s\n' % compat.safe_str(event))
        text = data if raw else compat.safe_str(data)
        for line in compat.safe_str(text).split('\n'):
            buf.append('data: %s\n' % line)
        buf.append('\n')
        return self._write(''.join(buf))

    def json(self, obj, event=None, id=None):
        """发送 JSON 事件（前端 JSON.parse 后可直接用）"""
        return self.send(compat.to_text(compat.json_bytes(obj)), event=event, id=id)

    def comment(self, text=''):
        """发送注释行（心跳 / 调试标记）"""
        return self._write(': %s\n\n' % compat.safe_str(text))

    def ping(self):
        return self.comment('ping %s' % int(time.time()))

    def close(self):
        if self._closed:
            return
        # 先写收尾事件、再置关闭位：_write() 见到 _closed 会直接返回，
        # 顺序反过来会导致 'close' 事件永远发不出去（前端 addEventListener('close') 收不到）。
        # 回归用例见 opslib/selftest.py::test_sse_consume。
        try:
            self._write('event: close\ndata: bye\n\n')
        except Exception:
            pass
        self._closed = True
        try:
            self.handler.wfile.write(b'0\r\n\r\n')
            self.handler.wfile.flush()
        except Exception:
            pass
        try:
            self.handler.close_connection = True
        except Exception:
            pass

    @property
    def closed(self):
        return self._closed

    # ------------------------------------------------------------ 泵
    def pump(self, generator, heartbeat=15, event=None, stop_flag=None):
        """把生成器/可迭代对象的内容持续推给客户端，自动插入心跳。

        generator 每产出一项：
          * dict  -> 作为 JSON 事件发送；
          * str   -> 作为文本事件发送；
          * (event_name, payload) 二元组 -> 指定事件名发送；
          * None  -> 仅发送心跳（可用于「暂时无数据」）。
        stop_flag 为可调用对象时，返回真值即结束。
        """
        self.ready()
        last_beat = time.time()
        try:
            for item in generator:
                if self._closed:
                    break
                if stop_flag is not None and stop_flag():
                    break
                if item is None:
                    if time.time() - last_beat >= heartbeat:
                        if not self.ping():
                            break
                        last_beat = time.time()
                    continue
                if isinstance(item, tuple) and len(item) == 2:
                    name, payload = item
                    if isinstance(payload, dict):
                        self.json(payload, event=name)
                    else:
                        self.send(payload, event=name)
                elif isinstance(item, dict):
                    self.json(item, event=event)
                else:
                    self.send(item, event=event)
                if time.time() - last_beat >= heartbeat:
                    self.ping()
                    last_beat = time.time()
        except compat.CLIENT_GONE:
            pass
        except Exception as e:                                # noqa: BLE001
            try:
                self.json({'status': 'error', 'message': compat.safe_str(e)}, event='error')
            except Exception:
                pass
        finally:
            self.close()


def consume(url, headers=None, on_event=None, timeout=180, idle_timeout=None, stop_flag=None,
            method='GET', body=None, content_type=None):
    """以 SSE 客户端身份消费上游事件流，逐事件回调 `on_event(name, data)`。

    默认 GET；传 method='POST' + body（命令放请求体）时上游 URI 上不会出现命令
    关键字，避免被安全设备的「URI 命令执行」特征规则误判（web_shell v1.3.0+）。

    用途：发布中心把目标机 web_shell 的 `/api/exec/stream`（start/out/exit）输出
    边收边转成步骤日志 —— 页面上就能看到「正在执行的脚本返回信息」，而不是等命令
    跑完才一次性出现。

    实现要点：
      * 手工建连 + `resp.fp.readline()` 逐行读（与平台侧 raw_sse_proxy 同一套做法）；
        上游是 chunked 响应时，分块长度行（形如 `1a`）不匹配任何前缀，自然被忽略；
      * 按 SSE 规范以**空行**为一个事件的结束，多条 `data:` 行用 `\\n` 拼回，
        因此 `stream.send('多行文本')` 这类载荷不会被拆成多个事件；
      * 上游 EOF / 主动关流都算正常结束（返回 ok=True）；
      * `idle_timeout` 秒内没有任何字节则主动结束，避免半死连接把任务线程挂住。

    返回 `(ok, error)`：ok=False 表示连不上或上游非 200（调用方可据此降级到同步接口）。
    """
    parsed = compat.urlparse(url)
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else compat.to_bytes(body)
    # 套接字超时：既要有总超时兜底，也要让 idle_timeout 真正生效 —— 只靠循环里判断
    # 是不够的，因为 readline() 会一直阻塞（上游静默时形同虚设）。这里把读超时压到
    # idle_timeout 级别，读超时后回到循环里判断是否已达空闲上限。
    sock_timeout = max(5, timeout)
    if idle_timeout:
        sock_timeout = max(1, min(sock_timeout, int(idle_timeout) or 1))
    conn = None
    try:
        conn = compat.http_client.HTTPConnection(host, port, timeout=sock_timeout)
        conn.putrequest(method, path, skip_accept_encoding=True)
        conn.putheader('Accept', 'text/event-stream')
        conn.putheader('Cache-Control', 'no-cache')
        if payload is not None:
            conn.putheader('Content-Type', content_type or 'application/json; charset=utf-8')
            conn.putheader('Content-Length', str(len(payload)))
        for key, value in (headers or {}).items():
            if key.lower() in ('host', 'content-length', 'connection', 'transfer-encoding',
                               'content-type'):
                continue
            conn.putheader(key, value)
        conn.endheaders(payload)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, 'HTTP %d' % resp.status
        name = 'message'
        data_lines = []
        last_byte = time.time()
        while True:
            if stop_flag is not None and stop_flag():
                return True, 'stopped'
            if idle_timeout and (time.time() - last_byte) >= idle_timeout:
                return True, 'idle-timeout'
            try:
                raw = resp.fp.readline()
            except _socket.timeout:
                # 读超时（上游静默）：回到循环顶部按 idle_timeout 判断，未到就继续等
                continue
            if not raw:
                break                    # 上游关流：正常结束
            last_byte = time.time()
            line = compat.to_text(raw).rstrip('\r').rstrip('\n')
            if not line:
                if data_lines:
                    payload = '\n'.join(data_lines)
                    event_name = name
                    name, data_lines = 'message', []
                    if on_event is not None:
                        on_event(event_name, payload)
                continue
            if line.startswith(':'):     # 心跳注释
                continue
            if line.startswith('event:'):
                name = line[6:].strip() or 'message'
            elif line.startswith('data:'):
                data_lines.append(line[5:][1:] if line[5:6] == ' ' else line[5:])
        return True, ''
    except Exception as e:                                       # noqa: BLE001
        return False, compat.safe_str(e)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


class Subscriber(object):
    """事件总线的一个订阅者（内部是一个有界队列）"""

    def __init__(self, topic, maxsize=200):
        self.topic = topic
        self.queue = compat.Queue(maxsize=maxsize)
        self.closed = False

    def put(self, item):
        if self.closed:
            return
        try:
            self.queue.put_nowait(item)
        except Exception:
            # 队列满：丢弃最旧的一条，保证订阅者不被慢客户端拖死
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(item)
            except Exception:
                pass

    def get(self, timeout=1.0):
        try:
            return self.queue.get(timeout=timeout)
        except Exception:
            return None

    def iter_items(self, timeout=1.0):
        while not self.closed:
            item = self.get(timeout)
            if item is not None:
                yield item

    def close(self):
        self.closed = True
        while True:
            try:
                self.queue.get_nowait()
            except Exception:
                break


class EventBus(object):
    """进程内发布订阅：topic -> 多个 Subscriber。

    典型用途：监控曲线推送（topic='metrics'）、任务进度（topic='task:<id>'）、
    告警直推（topic='alert'）。
    """

    def __init__(self):
        self._topics = {}
        self._lock = compat.threading.Lock()

    def subscribe(self, topic, maxsize=200, history=0):
        sub = Subscriber(topic, maxsize)
        with self._lock:
            bucket = self._topics.setdefault(topic, {'subs': [], 'history': []})
            bucket['subs'].append(sub)
            replay = bucket['history'][-history:] if history else []
        for item in replay:
            sub.put(item)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            bucket = self._topics.get(sub.topic)
            if bucket and sub in bucket['subs']:
                bucket['subs'].remove(sub)
            if bucket and not bucket['subs'] and not bucket['history']:
                self._topics.pop(sub.topic, None)
        sub.close()

    def publish(self, topic, data, keep_history=0):
        item = {'ts': time.time(), 'seq': compat.rand_hex(3), 'data': data}
        with self._lock:
            bucket = self._topics.get(topic)
            if bucket is None:
                if keep_history <= 0:
                    return 0
                bucket = {'subs': [], 'history': []}
                self._topics[topic] = bucket
            if keep_history > 0:
                bucket['history'].append(item)
                if len(bucket['history']) > keep_history:
                    del bucket['history'][:len(bucket['history']) - keep_history]
            subs = list(bucket['subs'])
        for sub in subs:
            sub.put(item)
        return len(subs)

    def subscriber_count(self, topic=None):
        with self._lock:
            if topic:
                return len((self._topics.get(topic) or {}).get('subs', []))
            return sum(len(b['subs']) for b in self._topics.values())


BUS = EventBus()


def stream_events(req, topics, heartbeat=15, timeout=0, stop_flag=None):
    """为 HTTP 请求建立一条多主题订阅流；每个事件带 topic 字段。

    topics: 主题名列表；timeout>0 表示多少秒无事件后自动结束连接。
    """
    stream = req.sse()
    stream.ready()
    subs = [(t, BUS.subscribe(t)) for t in topics]
    # 先合并成一个可迭代对象
    import threading

    box = compat.Queue(maxsize=500)

    def pump_one(topic, sub):
        while not sub.closed:
            item = sub.get(1.0)
            if item is not None:
                try:
                    box.put_nowait({'topic': topic, 'ts': item['ts'], 'data': item['data']})
                except Exception:
                    pass
        try:
            box.put_nowait(None)
        except Exception:
            pass

    threads = [compat.start_daemon(pump_one, (t, s)) for t, s in subs]
    # 用可变容器承载 last：gen() 内会写入它，而 py2 没有 nonlocal，
    # 直接对闭包变量赋值会被当成局部变量 → 首次空闲窗口即 UnboundLocalError。
    state = {'last': time.time()}

    def gen():
        while True:
            try:
                item = box.get(timeout=1.0)
            except Exception:
                item = None
            if item is None:
                if timeout and time.time() - state['last'] > timeout:
                    return
                yield None
                continue
            state['last'] = time.time()
            yield ('message', item)

    try:
        stream.pump(gen(), heartbeat=heartbeat, stop_flag=stop_flag)
    finally:
        for _, sub in subs:
            BUS.unsubscribe(sub)
