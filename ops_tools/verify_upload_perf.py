# -*- coding: utf-8 -*-
"""上传性能现场自测（py2.7+ / py3.7+ 通用，仅标准库）。

用途：在**真实服务器**上量化「分片落盘模式」与「磁盘/网络」的瓶颈位置，用于判断
上传慢到底是代码、文件系统还是网络造成的。脚本自带一个临时 upload_server 实例
（独立端口 + 独立临时目录，不碰现网数据），跑完自动清理。

用法::

    python verify_upload_perf.py                    # 默认 256MB，两种模式对比
    python verify_upload_perf.py --size-mb 512     # 加大负载
    python verify_upload_perf.py --chunk-mb 8 --conc 6
    python verify_upload_perf.py --upload-dir /data/up   # 指定落盘目录（测真实盘位！）

结论怎么看：
  * 「磁盘_顺序写」远高于「分片上传」→ 瓶颈在代码/网络，看两种模式谁的差距小；
  * 两种模式都远低于磁盘顺序写，且磁盘本身很低 → 落盘盘位（NFS/网络盘）是主因；
  * chunkfile 与 offset 差距明显 → 用快的那种启动（--chunk-write-mode）。

退出码：0 全部成功；1 有失败项；2 服务未能启动。
"""
import os
import sys
import json
import time
import shutil
import socket
import argparse
import threading
import subprocess
import tempfile
import hashlib

try:
    import http.client as http_client
except ImportError:                                  # py2
    import httplib as http_client                    # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == 'nt'
MB = 1024 * 1024
TOKEN = 'perf-token-0123456789'
DEFAULT_SIZE_MB = 256
DEFAULT_CHUNK_MB = 16
DEFAULT_CONC = 6


def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _post(port, path, obj, timeout=600):
    conn = http_client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request('POST', path, json.dumps(obj).encode('utf-8'),
                     {'Content-Type': 'application/json', 'X-Auth-Token': TOKEN})
        resp = conn.getresponse()
        return json.loads(resp.read().decode('utf-8') or '{}')
    finally:
        conn.close()


def _wait_up(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http_client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                conn.request('GET', '/api/health')
                resp = conn.getresponse()
                resp.read()
                if resp.status == 200:
                    return True
            finally:
                conn.close()
        except Exception:
            time.sleep(0.3)
    return False


def _rate(size_mb, seconds):
    return size_mb / seconds if seconds > 0 else 0.0


def _fmt(tag, seconds, size_mb, extra=''):
    print('  %-26s %7.2fs   %8.1f MB/s   %s' % (tag, seconds, _rate(size_mb, seconds), extra))


def disk_seq_write(path, size_mb, chunk_mb):
    """磁盘顺序写基线：单文件顺序写，衡量「这台机器这个盘位」能有多快"""
    blob = b'\x5a' * (chunk_mb * MB)
    target = os.path.join(path, 'disk_seq.bin')
    left = size_mb * MB
    t0 = time.time()
    with open(target, 'wb') as f:
        while left > 0:
            f.write(blob[:min(left, len(blob))])
            left -= len(blob)
    dt = time.time() - t0
    try:
        os.remove(target)
    except (OSError, IOError):
        pass
    return dt


def disk_parallel_write(path, size_mb, chunk_mb, conc):
    """并发独立文件顺序写：chunkfile 模式的理想上界"""
    blob = b'\x5a' * (chunk_mb * MB)
    d = os.path.join(path, 'disk_par')
    if not os.path.isdir(d):
        os.makedirs(d)
    total = size_mb * MB
    n = max(1, (total + len(blob) - 1) // len(blob))
    lock = threading.Lock()
    nxt = [0]

    def worker():
        while True:
            with lock:
                if nxt[0] >= n:
                    return
                i = nxt[0]
                nxt[0] += 1
            with open(os.path.join(d, 'part_%05d' % i), 'wb') as f:
                f.write(blob)
    t0 = time.time()
    ts = [threading.Thread(target=worker) for _ in range(conc)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.time() - t0
    shutil.rmtree(d, ignore_errors=True)
    return dt


def make_chunks(total, chunk):
    """每片内容不同，便于校验拼接顺序"""
    out = []
    n = (total + chunk - 1) // chunk
    for i in range(n):
        need = min(chunk, total - i * chunk)
        seed = ('%08d' % i).encode('ascii')
        out.append((seed * (need // 8 + 1))[:need])
    return out


def chunk_upload(port, updir, blobs, total, chunk, conc, tag, logf):
    """跑一次完整的分片上传：init → 并发分片 → complete，返回各阶段耗时与校验结果"""
    n = len(blobs)
    rk = hashlib.md5((tag + str(time.time())).encode('ascii')).hexdigest()[:16]
    t0 = time.time()
    init = _post(port, '/api/chunk/init', {
        'filename': tag + '.bin', 'relative_path': '', 'size': total,
        'total_chunks': n, 'chunk_size': chunk, 'resume_key': rk})
    t_init = time.time() - t0
    if init.get('status') != 'success':
        return None, 'init 失败: %s' % init.get('message')
    uid = init.get('upload_id')

    lock = threading.Lock()
    nxt = [0]
    errs = []

    def worker():
        while True:
            with lock:
                if nxt[0] >= n:
                    return
                i = nxt[0]
                nxt[0] += 1
            for attempt in (0, 1, 2):
                try:
                    conn = http_client.HTTPConnection('127.0.0.1', port, timeout=600)
                    try:
                        conn.request('POST', '/api/chunk/upload', blobs[i], {
                            'Content-Type': 'application/octet-stream',
                            'X-Auth-Token': TOKEN, 'X-Upload-Id': uid,
                            'X-Chunk-Index': str(i)})
                        resp = conn.getresponse()
                        resp.read()
                        if resp.status == 200:
                            break
                    finally:
                        conn.close()
                except Exception as e:
                    if attempt == 2:
                        errs.append('片%d: %s' % (i, e))
                    time.sleep(0.3)

    t0 = time.time()
    ts = [threading.Thread(target=worker) for _ in range(conc)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    t_up = time.time() - t0

    t0 = time.time()
    comp = _post(port, '/api/chunk/complete', {
        'upload_id': uid, 'filename': tag + '.bin', 'relative_path': '',
        'total_chunks': n, 'size': total, 'resume_key': rk})
    t_done = time.time() - t0

    content_ok = 'N/A'
    p = os.path.join(updir, tag + '.bin')
    if os.path.isfile(p):
        h = hashlib.md5()
        with open(p, 'rb') as f:
            while True:
                buf = f.read(4 * MB)
                if not buf:
                    break
                h.update(buf)
        expect = hashlib.md5(b''.join(blobs)).hexdigest()
        content_ok = '一致' if h.hexdigest() == expect else '**不一致**'
        try:
            os.remove(p)
        except (OSError, IOError):
            pass
    return {'init': t_init, 'upload': t_up, 'complete': t_done,
            'status': comp.get('status'), 'content': content_ok,
            'errs': errs, 'chunks': n}, None


def run_mode(mode, base_dir, size_mb, chunk_mb, conc, port):
    """在独立临时目录里起一个 upload_server，跑一次分片上传并报告各阶段耗时"""
    updir = os.path.join(base_dir, 'up_' + mode)
    if not os.path.isdir(updir):
        os.makedirs(updir)
    logpath = os.path.join(base_dir, mode + '.log')
    logf = open(logpath, 'wb')
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    proc = subprocess.Popen([sys.executable, '-u', os.path.join(HERE, 'tools', 'upload_server.py'),
                             '--host', '127.0.0.1', '--port', str(port),
                             '--upload-dir', updir, '--token', TOKEN,
                             '--chunk-write-mode', mode],
                            stdout=logf, stderr=subprocess.STDOUT, cwd=HERE, env=env)
    try:
        if not _wait_up(port):
            print('  %s: 服务启动失败（详见 %s）' % (mode, logpath))
            return None
        total = size_mb * MB
        chunk = chunk_mb * MB
        blobs = make_chunks(total, chunk)
        print('  --- 模式 %s ---' % mode)
        res, err = chunk_upload(port, updir, blobs, total, chunk, conc, 'perf_' + mode, logf)
        if res is None:
            print('  失败: %s' % err)
            return None
        total_t = res['init'] + res['upload'] + res['complete']
        print('    init %6.2fs | 分片 %7.2fs (%7.1f MB/s) | complete %6.2fs | 合计 %6.2fs | %s | %s' % (
            res['init'], res['upload'], _rate(size_mb, res['upload']),
            res['complete'], total_t,
            '成功' if res['status'] == 'success' else '失败', res['content']))
        return res
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            logf.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description='上传性能现场自测（含磁盘基线对比）')
    ap.add_argument('--size-mb', type=int, default=DEFAULT_SIZE_MB, help='单次负载大小 (默认 %dMB)' % DEFAULT_SIZE_MB)
    ap.add_argument('--chunk-mb', type=int, default=DEFAULT_CHUNK_MB, help='分片大小 (默认 %dMB)' % DEFAULT_CHUNK_MB)
    ap.add_argument('--conc', type=int, default=DEFAULT_CONC, help='并发分片数 (默认 %d)' % DEFAULT_CONC)
    ap.add_argument('--upload-dir', default='', help='落盘目录（默认临时目录；建议指向真实上传盘位）')
    args = ap.parse_args()

    if args.upload_dir:
        base = os.path.abspath(args.upload_dir)
        if not os.path.isdir(base):
            os.makedirs(base)
        cleanup_base = False
    else:
        base = tempfile.mkdtemp(prefix='perf_up_')
        cleanup_base = True

    print('=' * 74)
    print('上传性能自测：%dMB / 分片 %dMB / 并发 %d' % (args.size_mb, args.chunk_mb, args.conc))
    print('落盘目录: %s' % base)
    print('Python  : %s' % sys.version.split()[0])
    print('=' * 74)
    ok = True
    try:
        print('\n[1/3] 磁盘基线（衡量本机本盘位的能力上限）')
        dt = disk_seq_write(base, args.size_mb, args.chunk_mb)
        _fmt('磁盘_单文件顺序写', dt, args.size_mb)
        dt = disk_parallel_write(base, args.size_mb, args.chunk_mb, args.conc)
        _fmt('磁盘_%d路并发顺序写' % args.conc, dt, args.size_mb)

        print('\n[2/3] 分片上传（HTTP 全链路）')
        results = {}
        for mode in ('chunkfile', 'offset'):
            r = run_mode(mode, base, args.size_mb, args.chunk_mb, args.conc, _free_port())
            results[mode] = r
            if not r or r['status'] != 'success' or r['content'] not in ('一致', 'N/A'):
                ok = False

        print('\n[3/3] 结论')
        cf = results.get('chunkfile') or {}
        of = results.get('offset') or {}
        if cf and of:
            cf_t = cf['init'] + cf['upload'] + cf['complete']
            of_t = of['init'] + of['upload'] + of['complete']
            print('  chunkfile 合计 %.2fs（分片 %.2fs + 拼接 %.2fs）' % (cf_t, cf['upload'], cf['complete']))
            print('  offset    合计 %.2fs（预分配 %.2fs + 分片 %.2fs）' % (of_t, of['init'], of['upload']))
            if of_t and cf_t:
                faster = 'chunkfile' if cf_t < of_t else 'offset'
                print('  → 本机更快: %s（%.2fs vs %.2fs）' % (faster, min(cf_t, of_t), max(cf_t, of_t)))
                print('  → 如需切换: 启动 upload_server 加 --chunk-write-mode %s' % faster)
        print('\n  提示：若上传吞吐远低于「磁盘_并发顺序写」，瓶颈在磁盘/网络而非本代码；')
        print('        若两种模式差异明显，按上面的建议切换模式（NFS/网络盘建议 chunkfile）。')
    finally:
        if cleanup_base:
            shutil.rmtree(base, ignore_errors=True)
    print('\n%s' % ('全部通过 ✅' if ok else '存在失败项 ❌'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
