#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上传链路测速诊断（py2.7+ / py3.7+ 通用，仅标准库，客户端即可运行）。

用途：定位「8089 上传只有 xx MB/s」到底卡在哪一段 —— 链路单流带宽、往返延迟、
服务端并发能力，还是「大量小文件」的每文件往返开销。

建议**在客户端机器上跑**，直接打真实服务器，服务器上无需装任何东西::

    python verify_upload_speed.py --host 10.0.0.5 --port 8089 --token 你的令牌
    python verify_upload_speed.py --host 10.0.0.5 --size-mb 32 --conc 1,2,4,8   # 小流量档
    python verify_upload_speed.py                # 不带 --host：本机起临时服务，看代码上限

测四组数据：
  [1] 连通与往返延迟 —— RTT 大（>5ms）时单流大分片也会慢，属链路特征
  [2] 并发梯度 1/2/4/8 —— 找拐点。只有 1 路慢、加并发线性上升 ⇒ 单流受窗口/RTT 限制；
      加到 4 路以上不再涨 ⇒ 链路或服务端已饱和
  [3] 单流大分片 —— 分片放大到整文件，测「每片建链+收尾」开销占比
  [4] 小文件批量（/api/chunk/put）—— 出「文件/秒」，评估大量小文件的真实瓶颈

链路类型参考上限：千兆网≈110MB/s，百兆网≈11MB/s，WiFi-5G 实测≈30~60MB/s，
WiFi-2.4G≈5~8MB/s，跨机房/VPN 视专线带宽而定。

退出码：0 全部成功；1 有失败项；2 无法连接服务器。
"""
import os
import re
import sys
import json
import time
import shutil
import socket
import hashlib
import argparse
import threading
import subprocess
import tempfile

try:
    import http.client as http_client
except ImportError:                                  # py2
    import httplib as http_client                    # noqa: F401

_PY2 = sys.version_info[0] == 2
if _PY2:
    def T(u):
        """unicode -> utf-8 字节串，避免 py2 下 print 混排触发 ascii 编码错误"""
        return u.encode('utf-8') if isinstance(u, unicode) else u  # noqa: F821
else:
    def T(u):
        return u

HERE = os.path.dirname(os.path.abspath(__file__))
MB = 1024 * 1024
DEFAULT_PORT = 8089
DEFAULT_SIZE_MB = 64
DEFAULT_CHUNK_MB = 8
DEFAULT_CONC_LIST = '1,2,4,8'
DEFAULT_SMALL_COUNT = 200
DEFAULT_SMALL_MB = 1
DEFAULT_SMALL_CONC = 4
TOKEN_LOCAL = 'speedtest-token'


# ================= 基础工具 =================
def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _fmt_rate(size_mb, seconds):
    if seconds <= 0:
        return 0.0
    return size_mb / seconds


def _line(tag, seconds, size_mb, extra=''):
    print('  %-24s %7.2fs   %8.1f MB/s   %s'
          % (tag, seconds, _fmt_rate(size_mb, seconds), extra))


class Conn(object):
    """可复用的 HTTP/1.1 长连接：避免把 TCP 建链开销算进速率；
    浏览器 XHR 走连接池，这里等价。断线自动重连。"""

    def __init__(self, host, port, token, timeout=900):
        self.host, self.port, self.token, self.timeout = host, port, token, timeout
        self.conn = None

    def connect(self):
        self.close()
        self.conn = http_client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def request(self, method, path, body=None, headers=None, retry=1):
        hdrs = {}
        if self.token:
            hdrs['X-Auth-Token'] = self.token
        if headers:
            hdrs.update(headers)
        last = None
        for attempt in range(retry + 1):
            if self.conn is None:
                self.connect()
            try:
                self.conn.request(method, path, body, hdrs)
                resp = self.conn.getresponse()
                data = resp.read()
                return resp.status, data
            except Exception as e:                    # 连接被对端关闭/超时 → 重连重试
                last = e
                self.close()
                if attempt >= retry:
                    break
                time.sleep(0.2)
        raise last

    def json(self, method, path, obj=None, headers=None, retry=1):
        body = None
        hdrs = dict(headers or {})
        if obj is not None:
            body = json.dumps(obj).encode('utf-8')
            hdrs['Content-Type'] = 'application/json'
        st, data = self.request(method, path, body, hdrs, retry=retry)
        try:
            return st, json.loads(data.decode('utf-8') or '{}')
        except ValueError:
            return st, {}


# ================= 单次分片上传 =================
def upload_once(host, port, token, tag, size_mb, chunk_mb, conc):
    """init → conc 路并发分片 → complete，返回 (秒数, 错误列表, 分片数)"""
    total = max(1, size_mb) * MB
    chunk = max(1, chunk_mb) * MB
    n = max(1, (total + chunk - 1) // chunk)
    last_len = total - (n - 1) * chunk
    blob = b'\x5a' * chunk
    rk = hashlib.md5(('%s|%d|%d|%d|%s' % (tag, size_mb, chunk_mb, conc, time.time()))
                     .encode('utf-8')).hexdigest()[:16]

    main = Conn(host, port, token)
    try:
        st, d = main.json('POST', '/api/chunk/init', {
            'filename': '%s.bin' % tag, 'relative_path': '%s/%s.bin' % (_TEST_DIR, tag),
            'size': total, 'total_chunks': n, 'chunk_size': chunk, 'resume_key': rk})
        if st != 200 or d.get('status') != 'success':
            return None, ['init 失败: HTTP %s %s' % (st, T(d.get('message')))], n
        uid = d.get('upload_id')
        if d.get('chunk_size') and int(d['chunk_size']) != chunk:
            return None, ['服务端分片大小被改为 %s（检查 --chunk-size-mb 上限）' % d['chunk_size']], n

        lock = threading.Lock()
        nxt = [0]
        errs = []

        def worker():
            c = Conn(host, port, token)
            try:
                while True:
                    with lock:
                        i = nxt[0]
                        nxt[0] += 1
                    if i >= n:
                        return
                    ln = chunk if i < n - 1 else last_len
                    payload = blob if ln == chunk else blob[:ln]
                    try:
                        st2, _ = c.request('POST', '/api/chunk/upload', payload, {
                            'Content-Type': 'application/octet-stream',
                            'X-Upload-Id': uid, 'X-Chunk-Index': str(i)})
                        if st2 != 200:
                            raise IOError('HTTP %s' % st2)
                    except Exception as e:
                        with lock:
                            errs.append('片 %d: %s' % (i, e))
                        return
            finally:
                c.close()

        t0 = time.time()
        ts = [threading.Thread(target=worker) for _ in range(max(1, conc))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        dt = time.time() - t0

        st, d = main.json('POST', '/api/chunk/complete', {
            'upload_id': uid, 'filename': '%s.bin' % tag,
            'relative_path': '%s/%s.bin' % (_TEST_DIR, tag),
            'total_chunks': n, 'size': total, 'resume_key': rk}, retry=2)
        if st != 200 or d.get('status') != 'success':
            errs.append('complete 失败: HTTP %s %s' % (st, d.get('message')))
        return dt, errs, n
    finally:
        main.close()


_TEST_DIR = ''


# ================= 小文件批量（三合一接口） =================
def put_many(host, port, token, tag, count, size_mb, conc):
    size = max(1, size_mb) * MB
    blob = b'\x7a' * size
    lock = threading.Lock()
    nxt = [0]
    errs = []
    names = []

    def worker(wid):
        c = Conn(host, port, token)
        try:
            while True:
                with lock:
                    i = nxt[0]
                    nxt[0] += 1
                if i >= count:
                    return
                name = '%s_%04d.bin' % (tag, i)
                path = ('/api/chunk/put?filename=%s&relative_path=%s&size=%d'
                        % (name, _TEST_DIR + '/' + name, size))
                try:
                    st, data = c.request('POST', path, blob,
                                         {'Content-Type': 'application/octet-stream'})
                    if st != 200:
                        # 重试一次（并发同名/瞬时 500 都能靠重试救回），仍失败则记下并继续
                        st, data = c.request('POST', path, blob,
                                             {'Content-Type': 'application/octet-stream'})
                    if st != 200:
                        with lock:
                            errs.append('文件 %d: HTTP %s %s'
                                        % (i, st, data[:120].decode('utf-8', 'replace')))
                        continue
                    with lock:
                        names.append('%s/%s' % (_TEST_DIR, name))
                except Exception as e:
                    with lock:
                        errs.append('文件 %d: %s' % (i, e))
        finally:
            c.close()

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(max(1, conc))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.time() - t0, errs, names


# ================= 服务器自检与环境 =================
def probe_server(host, port, token):
    """返回 (版本, 分片大小, 落盘模式, 最小往返延迟秒)。老版本没有这些字段时返回 None 占位。"""
    c = Conn(host, port, token, timeout=15)
    try:
        rtts = []
        info = {}
        for _ in range(5):
            t0 = time.time()
            st, d = c.json('GET', '/api/health', retry=1)
            rtts.append(time.time() - t0)
            if st == 200:
                info = d
        # 老版本 /api/health 只返回 alive
        if 'version' not in info:
            try:
                st, html = c.request('GET', '/', retry=1)
                # 注意：不能用 rb'' 前缀（py3.3+ 语法）；b'' 在 py2/py3 下都合法
                m = re.search(b'v(\\d+\\.\\d+\\.\\d+)', html or b'')
                if m:
                    info['version'] = m.group(1).decode('ascii')
            except Exception:
                pass
        return info, min(rtts)
    finally:
        c.close()


def cleanup(host, port, token):
    c = Conn(host, port, token)
    try:
        st, d = c.json('POST', '/api/batch/delete', {'paths': [_TEST_DIR]}, retry=1)
        return d.get('status') == 'success'
    except Exception:
        return False
    finally:
        c.close()


# ================= 本地临时服务（无 --host 时） =================
def start_local_server(upload_dir, port, token, chunk_mb, write_mode):
    logpath = os.path.join(upload_dir, '..', 'server.log')
    logf = open(logpath, 'wb')
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    cmd = [sys.executable, '-u', os.path.join(HERE, 'tools', 'upload_server.py'),
           '--host', '127.0.0.1', '--port', str(port),
           '--upload-dir', upload_dir, '--token', token,
           '--chunk-write-mode', write_mode,
           '--chunk-size-mb', str(chunk_mb)]
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=HERE, env=env)
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            c = Conn('127.0.0.1', port, token, timeout=2)
            try:
                st, _ = c.json('GET', '/api/health', retry=0)
                if st == 200:
                    return proc, logf
            finally:
                c.close()
        except Exception:
            time.sleep(0.3)
    try:
        proc.terminate()
    except Exception:
        pass
    logf.close()
    with open(logpath, 'rb') as f:
        print(f.read().decode('utf-8', 'replace')[-2000:])
    return None, None


# ================= 主流程 =================
def main():
    global _TEST_DIR
    ap = argparse.ArgumentParser(description='上传链路测速诊断')
    ap.add_argument('--host', default='', help='目标服务器 IP/域名；留空则在本机起临时服务')
    ap.add_argument('--port', type=int, default=0, help='端口（默认 8089；本地模式自动选空闲端口）')
    ap.add_argument('--token', default='', help='访问令牌 X-Auth-Token（服务端设了 --token 时必填）')
    ap.add_argument('--size-mb', type=int, default=DEFAULT_SIZE_MB, help='并发梯度每个档位的负载大小 (默认 %dMB)' % DEFAULT_SIZE_MB)
    ap.add_argument('--chunk-mb', type=int, default=DEFAULT_CHUNK_MB, help='分片大小 (默认 %dMB；梯度测试建议偏小以吃满并发)' % DEFAULT_CHUNK_MB)
    ap.add_argument('--conc', default=DEFAULT_CONC_LIST, help='并发档位，逗号分隔 (默认 %s)' % DEFAULT_CONC_LIST)
    ap.add_argument('--sweep-mb', type=int, default=0, help='单流大分片测试的整文件大小 (默认取 --size-mb)')
    ap.add_argument('--small-count', type=int, default=DEFAULT_SMALL_COUNT, help='小文件测试个数 (默认 %d，0 跳过)' % DEFAULT_SMALL_COUNT)
    ap.add_argument('--small-mb', type=int, default=DEFAULT_SMALL_MB, help='小文件测试单文件大小 MB (默认 %d)' % DEFAULT_SMALL_MB)
    ap.add_argument('--small-conc', type=int, default=DEFAULT_SMALL_CONC, help='小文件并发 (默认 %d)' % DEFAULT_SMALL_CONC)
    ap.add_argument('--upload-dir', default='', help='本地模式的落盘目录（建议指向真实盘位）')
    ap.add_argument('--chunk-write-mode', default='chunkfile', help='本地模式的落盘模式 (默认 chunkfile)')
    ap.add_argument('--keep', action='store_true', help='保留测试文件（默认测完自动删除）')
    args = ap.parse_args()

    conc_list = []
    for part in str(args.conc).split(','):
        part = part.strip()
        if part:
            try:
                conc_list.append(max(1, int(part)))
            except ValueError:
                pass
    if not conc_list:
        conc_list = [1, 2, 4, 8]

    _TEST_DIR = '_speedtest_%s' % time.strftime('%Y%m%d_%H%M%S')

    proc = logf = tmp_root = None
    local = not args.host
    host = args.host or '127.0.0.1'
    port = args.port
    token = args.token
    try:
        if local:
            token = args.token or TOKEN_LOCAL
            port = port or _free_port()
            if args.upload_dir:
                tmp_root = os.path.abspath(args.upload_dir)
                if not os.path.isdir(tmp_root):
                    os.makedirs(tmp_root)
            else:
                tmp_root = tempfile.mkdtemp(prefix='speed_up_')
            updir = os.path.join(tmp_root, 'uploads')
            if not os.path.isdir(updir):
                os.makedirs(updir)
            proc, logf = start_local_server(updir, port, token,
                                            args.chunk_mb, args.chunk_write_mode)
            if not proc:
                print('本地服务启动失败')
                return 2
        else:
            port = port or DEFAULT_PORT

        print('=' * 78)
        print('上传链路测速诊断  %s:%d' % (host, port))
        print('本机 Python: %s   模式: %s' % (sys.version.split()[0],
                                            '本机临时服务(看代码上限)' if local else '远程服务器(看真实链路)'))
        print('=' * 78)

        info, rtt = probe_server(host, port, token)
        ver = T(info.get('version')) or '未知'
        srv_chunk = info.get('chunk_size')
        srv_mode = T(info.get('chunk_write_mode') or info.get('write_mode')) or '未知'
        print('\n[1/4] 服务器与链路往返')
        print('  服务端版本 : %s' % ver)
        print('  服务端分片 : %s' % (('%dMB' % (srv_chunk // MB)) if srv_chunk else '未上报'))
        print('  落盘模式   : %s' % srv_mode)
        print('  往返延迟   : %.2f ms（5 次最小）' % (rtt * 1000))
        if srv_chunk and srv_chunk != args.chunk_mb * MB:
            print('  提示: 用 --chunk-mb %d 才能真正反映服务端配置的分片大小'
                  % max(1, srv_chunk // MB))
        if rtt * 1000 > 10:
            print('  提示: RTT 偏大，单流吞吐会被窗口限制，靠并发补（见下一组数据）')

        failures = 0

        print('\n[2/4] 并发梯度（每档 %dMB，分片 %dMB）' % (args.size_mb, args.chunk_mb))
        rates = {}
        for conc in conc_list:
            dt, errs, n = upload_once(host, port, token, '%s_c%d' % (_TEST_DIR, conc),
                                      args.size_mb, args.chunk_mb, conc)
            if dt is None:
                failures += 1
                print('  %-24s 失败: %s' % ('并发 %d' % conc, errs[:2]))
                continue
            if errs:
                failures += 1
            r = _fmt_rate(args.size_mb, dt)
            rates[conc] = r
            _line('并发 %d (%d 片)' % (conc, n), dt, args.size_mb,
                  '' if not errs else '错误 %d 个: %s' % (len(errs), errs[0]))

        print('\n[3/4] 单流大分片（1 片 = 整文件，测每片建链/收尾开销）')
        sweep_mb = args.sweep_mb or args.size_mb
        dt, errs, n = upload_once(host, port, token, '%s_big' % _TEST_DIR,
                                  sweep_mb, sweep_mb, 1)
        if dt is None:
            failures += 1
            print('  失败: %s' % errs[:2])
        else:
            if errs:
                failures += 1
            _line('单流 %dMB 单片' % sweep_mb, dt, sweep_mb,
                  '' if not errs else '错误: %s' % errs[0])

        if args.small_count > 0:
            print('\n[4/4] 小文件批量（%d 个 × %dMB，并发 %d，走 /api/chunk/put）'
                  % (args.small_count, args.small_mb, args.small_conc))
            dt, errs, names = put_many(host, port, token, '%s_small' % _TEST_DIR,
                                       args.small_count, args.small_mb, args.small_conc)
            done = len(names)
            total_mb = done * args.small_mb
            if errs:
                failures += 1
            _line('小文件 %d 个' % done, dt, total_mb,
                  '%6.1f 文件/秒%s' % (done / dt if dt > 0 else 0,
                                       '' if not errs else '   错误: %s' % errs[0]))
        else:
            print('\n[4/4] 小文件批量：已跳过（--small-count 0）')

        # ---------- 结论 ----------
        print('\n' + '-' * 78)
        print('结论参考')
        if rates:
            best_conc = max(rates, key=lambda k: rates[k])
            best = rates[best_conc]
            single = rates.get(1)
            if single:
                print('  * 单流 %.1f MB/s → %d 路并发 %.1f MB/s（放大 %.1f 倍）'
                      % (single, best_conc, best, best / single if single else 0))
                if best / single > 2.5:
                    print('    ⇒ 单流受限（带宽时延积/无线/VPN），并发能有效补偿：')
                    print('      页面已用 6 路并发，属于正常利用方式；单文件提速有限。')
                else:
                    print('    ⇒ 并发几乎无增益：瓶颈在链路或服务端整体吞吐，'
                          '不是单流限制。')
            print('  * 实测峰值 %.1f MB/s，对照链路理论上限：' % best)
            print('      千兆≈110  百兆≈11  WiFi5G≈30~60  WiFi2.4G≈5~8  (MB/s)')
            print('      接近哪一档 ⇔ 瓶颈就在那一档，代码/磁盘都不是主因。')
        print('  * [3] 若单流大分片明显高于 [2] 的小分片 ⇒ 每片请求往返有开销，'
              '可加大 --chunk-size-mb。')
        print('  * [4] 若小文件 MB/s 远低于大分片 ⇒ 大量小文件受「每文件请求数」限制，'
              '属预期，提高并发或打包上传更划算。')
        print('-' * 78)

        if not args.keep:
            if cleanup(host, port, token):
                print('测试文件已清理（服务器端 %s/）' % _TEST_DIR)
            else:
                print('测试文件清理失败，请手动删除服务器上的 %s/ 目录' % _TEST_DIR)

        print('\n%s' % ('全部通过 ✅' if not failures else '存在 %d 项失败 ❌' % failures))
        return 0 if not failures else 1
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait()
            except Exception:
                pass
        if logf is not None:
            try:
                logf.close()
            except Exception:
                pass
        if local and tmp_root and not args.upload_dir:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
