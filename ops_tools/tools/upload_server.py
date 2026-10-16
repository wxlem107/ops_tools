#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
零依赖文件/文件夹上传服务器 (完美支持拖拽文件夹 + 实时进度 + 文件列表)
适用于麒麟/Linux服务器，仅使用 Python 2.7.5+ / 3.7.5+ 标准库。

【优化点】
1. 支持命令行参数: --host / --port / --upload-dir / --token / --admin-token / --pidfile
   / --max-upload-mb / --chunk-size-mb / --chunk-write-mode / --sock-buf-mb / --allow-client-dir
   （完整用法、默认值与等价环境变量见文末【用法示例】）
2. 令牌认证: 设置 --token 后，自带页面需先输入访问令牌登录，上传/列表/删除/下载均需携带 X-Auth-Token 头
3. 管理接口: POST /api/control 可远程 stop / restart (需 --admin-token)
4. 文件接口: GET /api/download 下载、DELETE /api/file 删除
5. 状态接口: GET /api/status 供管理平台巡检
6. 修复目录穿越漏洞，使用规范化路径 + 前缀校验
7. 支持 PID 文件，便于外部工具启停
8. 优雅停机: 收到停止指令或 Ctrl+C 时清理 PID 文件
9. CORS 支持: 允许浏览器跨域直连上传/下载/删除（供管理平台直连上传）
10. 登录鉴权: 与集中管理平台一致，设置 --token 后先显示登录页，输入令牌验证通过再进入
11. 每文件耗时: 上传队列实时显示每个文件的状态与耗时，结束后显示总耗时
12. 磁盘容量监控: /api/status 返回磁盘总/已用/可用容量与使用率，页面顶部展示磁盘占用条
13. 文件搜索过滤: 文件列表支持关键字实时过滤（前端过滤 + /api/list 支持 q 参数）
14. 上传速度列: 上传队列实时显示每个文件的上传速度
15. 下载断连处理: 下载支持 Range 断点续传，客户端断开时静默结束不刷错误日志
16. 分片上传/断点续传: 前端按片上传 (默认 16MB/片)，分片失败自动重试；支持 resume_key 续传，
    重传同文件时自动跳过已上传分片，提升大文件上传性能与稳定性
17. 批量操作 API: POST /api/batch/delete 批量删除、GET/POST /api/batch/download 批量打包下载 (zip)
18. 页面批量操作: 文件列表支持勾选后批量下载/批量删除
19. 分片合并加速 (性能关键): Linux 下走 os.sendfile 内核态直传，去掉 read/write 两次用户态
    拷贝；单分片文件直接 os.replace 落地，完全跳过拼接；超大文件建议改用单请求直传
    (见 www_form 阈值 SMALL_UPLOAD_LIMIT_MB)，可把磁盘落地从 3 份降到 1 份
20. 页面与代码分离: 前端拆到同级 templates/upload_server.html，改完刷新浏览器即生效
21. 免合并落地 (v3.1.1): 分片按 offset 直接写入暂存文件 (<cdir>/data)，用 <cdir>/__done
    记录完成位；complete 时仅一次 rename 即落地，省掉「全量读 + 全量写」的拼接开销
    (磁盘 I/O 从约 2× 文件大小降到 1×)
22. 分片请求 URL 恒定 (v3.1.1): upload_id / index 改由 X-Upload-Id / X-Chunk-Index 请求头传递，
    URL 保持 /api/chunk/upload 不变 —— CORS 预检缓存键包含完整 URL，URL 恒定后整轮上传
    只付一次 OPTIONS 预检（query 传参方式仍兼容）
23. 单片三合一接口 (v3.1.1): POST /api/chunk/put 一次请求完成 init+upload+complete，
    消除小文件「3 次往返」的开销
24. 并发落盘健壮性 (v3.2.2): 落盘临时文件名带随机后缀、最终名在锁内「查重+改名」，
    修掉并发写同名文件/同分片重传时的 500（Windows WinError 5/32）；os.replace 增加
    瞬时占用重试；relative_path 只给目录时不再把目录名当文件名
25. 上传速率可观测 (v3.2.2): 访问日志对上传请求追加「本片字节数 + 服务端实测速率」
    （如 [16.0MB 104.6MB/s]），用于区分「客户端推不动」与「服务端落盘慢」
26. py2 读缓冲 (v3.2.2): rbufsize=1MB，py2 下 socket.makefile 默认 8KB 缓冲导致读满
    1MB 分片要 128 次 recv，并发分片时系统调用/GIL 开销明显
27. TCP 缓冲可调 (v3.2.2): --sock-buf-mb（默认 0=内核自适应），仅高时延链路/容器内
    rmem_max 偏小/无线导致接收窗口成为瓶颈时才需要显式放大
28. 上传响应返回真实落盘路径 (v3.2.3): /api/upload、/api/chunk/put、/api/chunk/complete
    的响应新增 path / dir / upload_dir / relative_path。调用方不必再拿「页面上配置的目录
    + 文件名」去猜文件在哪 —— 目标机的存储根是它自己的 --upload-dir（默认 ./uploads，
    相对**进程工作目录**），与调用方配置可以不一致；靠猜会把 ${package} 指到不存在的路径
    （用户报的「配了 /home/xpe/app_temp 却进了 uploads/」即是此因）。
29. 请求级存储根覆盖 (v3.3.0): 启动加 --allow-client-dir（或环境变量 UP_ALLOW_CLIENT_DIR=1）
    后，/api/upload（表单字段 dir）、/api/chunk/put（query dir）、/api/chunk/init 与
    /api/chunk/complete（JSON 字段 dir）可携带绝对路径 dir 逐请求覆盖存储根 ——
    release_center 页面配置的「远端上传目录」即时生效，改目录无需重启本服务。
    dir 必须是绝对路径，否则 400；开关未开时 dir 被静默忽略（响应仍带真实落盘路径）。
    分片任务在 init 时把 base_dir 记进任务元信息，complete 强制复用，同一次上传不跑偏。
    /api/status 新增 allow_client_dir 字段供调用方探测。
30. 安全与健壮性加固 (v3.3.1，配合 CODE_REVIEW 整改):
    * 任意目录写收敛: is_forbidden_dir() 拒绝根目录(/ 与盘符根)、系统目录(/etc、/proc、
      /sys、/root、/boot、Windows 目录 等)、UNC 共享(形如 //host/share) 与 Win32 长路径前缀 —— 原先
      --allow-client-dir 下 dir 只要「绝对路径」就放行，/etc 也能当存储根。
      分片落点再做 is_within(target_dir, root) 兜底，杜绝穿越。
    * 状态接口脱敏: /api/status 未授权时只回基础字段(version/uptime/allow_client_dir/
      token_required/server_time)，不再泄露上传目录/PID/磁盘/文件数；仍回 200 以保证
      「200=在线」的探活语义。
    * keep-alive 残留: _send_json 在 status>=400 且 Content-Length>0 时置
      close_connection=True —— 错误分支(401/400)不排空请求体，残留 body 会被当下一
      请求解析。
    * 下载 Range: 支持 bytes=-N 后缀区间(取最后 N 字节)；错误分支用 started 标志区分
      「已发头则 close_connection，否则 500」，避免半截响应被复用。
    * 分片索引上界: handle_chunk_upload 校验 total_chunks，offset 模式不再能被
      seek(9999*chunk_size) 分配 TB 级空洞文件。
    * _save_chunk_legacy 增加写入长度校验与 tmp 清理。
    * /api/control 限制请求体 ≤1MB；批量打包下载 os.walk 跳过隐藏文件。
【兼容性】
    Python 2.7.5+ 与 Python 3.7.5+ 均可直接运行，无需任何第三方库。
    Python 2.7 下走 cgi.FieldStorage；Python 3.13+ 因 cgi 被移除，普通表单直传会给出提示，
    可使用分片上传接口（默认路径）替代。

【分片上传接口】
    relative_path 两种写法都支持（v3.2.2 起）：
      * 含文件名的整路径 —— 'a/b/c.txt'（页面上传/文件夹上传走这种）
      * 只给目录        —— 'a/b' + filename='c.txt'（脚本式调用）
    POST /api/chunk/init     JSON: {filename, relative_path?, size, total_chunks, resume_key?}
                             -> {upload_id, chunk_size, uploaded:[已传分片索引], filename}
    POST /api/chunk/upload?upload_id=xxx&index=N   body 为分片原始二进制
        亦可：POST /api/chunk/upload  请求头 X-Upload-Id / X-Chunk-Index（URL 恒定，预检仅一次）
    GET  /api/chunk/status?upload_id=xxx&total_chunks=N  -> {uploaded:[...]}
    POST /api/chunk/complete JSON: {upload_id, filename?, relative_path?, total_chunks, size, resume_key?}
    POST /api/chunk/put?filename=xxx&relative_path=&size=N   body 为整个文件（单片三合一）
    GET  /api/health -> {alive, version, chunk_size, chunk_write_mode, token_required}
        客户端/页面据此探测服务端分片大小（页面按它切片，--chunk-size-mb 才能真正生效）
【批量操作接口】
    POST /api/batch/delete   JSON: {paths:["a.txt","dir/b"]} -> {success:[],failed:[{path,error}]}
    GET/POST /api/batch/download?paths=a&paths=b   -> 打包 zip 流式返回

【v3.2.0 新增接口】
    GET /api/types                                  -> {types:[dir,archive,jar,excel,doc,image,log,text,binary]}
    GET /api/tree?path=&depth=2                     -> {path, dirs:[{name,path,has_children}], files_count}
    GET /api/list?path=&q=&sort=name|size|mtime|type&order=asc|desc&page=1&size=50&type=&time_from=&time_to=
        -> {files:[{name,size,mtime,type,is_dir}], total, page, size, pages, path, dirs:[]}
        不带任何结构化参数时仍返回旧结构 {files:[...], count}，兼容旧版 ops_Platform 客户端

【用法示例】
    # 1) 最简启动：默认监听 0.0.0.0:8089，落盘 ./uploads，无鉴权
    python upload_server.py

    # 2) 指定监听地址/端口与落盘目录（多用绝对路径；相对路径基于「进程工作目录」）
    python upload_server.py --host 0.0.0.0 --port 8089 --upload-dir /data/uploads

    # 3) 生产推荐：鉴权 + 管理令牌 + PID + 全部显式参数（单行，可直接复制）
    python upload_server.py --host 0.0.0.0 --port 8089 --upload-dir /data/uploads --token mytoken --admin-token admintoken --pidfile /data/upload.pid --max-upload-mb 0 --chunk-size-mb 16 --chunk-write-mode chunkfile --sock-buf-mb 0

    # 4) 允许上传请求携带 dir（绝对路径）逐请求覆盖存储根 —— 配合管理平台「远端上传目录」，改目录免重启
    #    开启后 /api/upload（表单 dir）、/api/chunk/put（query dir）、/api/chunk/init|complete（JSON dir）才接受 dir
    python upload_server.py --port 8089 --token mytoken --allow-client-dir
    #    等价环境变量： UP_ALLOW_CLIENT_DIR=1 python upload_server.py --port 8089

    # 5) 大文件 / 高时延网络调优
    #    --chunk-size-mb 32   大文件分片调大，减少请求数（页面按 /api/health 探测值切片）
    #    --sock-buf-mb 8      仅当容器内 rmem_max 偏小 / 长肥管道 / 无线导致接收窗口受限时才调大
    python upload_server.py --port 8089 --chunk-size-mb 32 --sock-buf-mb 8

    # 6) 切换分片落盘模式（默认 chunkfile 各文件系统上最稳；offset 仅当实测更快再用）
    python upload_server.py --port 8089 --chunk-write-mode offset
    #    选型建议：先跑 python verify_upload_perf.py 实测，谁快用谁

    # 7) 自定义前端模板目录（默认脚本同级 templates/，其次当前工作目录 templates/）
    UP_TEMPLATE_DIR=/opt/upload_templates python upload_server.py --port 8089

    # 8) 后台常驻（nohup + PID 文件，便于外部启停）
    nohup python -u upload_server.py --port 8089 --token mytoken --pidfile /data/upload.pid > /data/upload_server.log 2>&1 &

    # 9) 查看全部参数与默认值
    python upload_server.py --help

【全部参数】（括号内为默认值）
    --host             监听地址              (0.0.0.0)          环境变量 UP_HOST
    --port             监听端口              (8089)             环境变量 UP_PORT
    --upload-dir       文件保存目录          (./uploads)        环境变量 UP_DIR
    --token            访问令牌              (空 = 不鉴权)       环境变量 UP_TOKEN
    --admin-token      管理令牌              (空 = control 免验) 环境变量 UP_ADMIN_TOKEN
    --pidfile          PID 文件路径          (空 = 不写)         无环境变量
    --max-upload-mb    单次上传大小上限 MB    (0 = 不限)          环境变量 UP_MAX_MB
    --chunk-size-mb    分片单片大小 MB        (16)               环境变量 UP_CHUNK_MB
    --chunk-write-mode 分片落盘模式          (chunkfile)        环境变量 UP_WRITE_MODE
                       chunkfile=按片独立文件+complete 拼接；offset=稀疏文件按 offset 直写
    --sock-buf-mb      TCP 收发缓冲 MB        (0 = 内核自适应)    环境变量 UP_SOCK_BUF_MB
    --allow-client-dir 允许请求级 dir 覆盖存储根 (关)            环境变量 UP_ALLOW_CLIENT_DIR
    --help, -h         打印帮助后退出        无环境变量
    仅有环境变量的项：
      UP_TEMPLATE_DIR  自定义前端模板目录（默认脚本同级 templates/ → 当前工作目录 templates/）
"""
from __future__ import print_function

import os
import sys

# 引入公共层 opslib（脚本与 opslib 同级，详见 opslib/README.md 第 1 节）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
from opslib import compat, server as opsserver

import argparse
import datetime
import json
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

# ================= Python 2.7.5+ / 3.7.5+ 兼容层 =================
PY2 = sys.version_info[0] == 2

if PY2:
    import BaseHTTPServer as _http_server
    import SocketServer as _sock_server
    import socket as _socket
    import urlparse as _urlparse
    from urllib import quote as _url_quote
    text_type = unicode            # noqa: F821  py2 内置
    string_types = (basestring,)   # noqa: F821  py2 内置
    # py2 没有 BrokenPipeError / ConnectionResetError / TimeoutError
    CLIENT_GONE = (_socket.error, IOError)
else:
    import http.server as _http_server
    import socketserver as _sock_server
    import urllib.parse as _urlparse
    from urllib.parse import quote as _url_quote
    text_type = str
    string_types = (str,)
    CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)

BaseHTTPRequestHandler = _http_server.BaseHTTPRequestHandler
urlparse = _urlparse.urlparse
parse_qs = _urlparse.parse_qs
quote = _url_quote

try:
    import cgi  # Python 2.7 ~ 3.12 标准库
except ImportError:  # Python 3.13+ 已移除 cgi
    cgi = None


# ---- 复用 opslib.compat 的通用兼容函数（公共层单一来源，避免重复实现）----
# 注意：is_within 保留本地实现，使用 realpath 归一化，行为与原版一致，不替换。
to_bytes = compat.to_bytes
to_text = compat.to_text
json_bytes = compat.json_bytes
html_escape = compat.html_escape
open_text = compat.open_text
log = compat.log
makedirs = compat.makedirs
os_replace = compat.os_replace
fullmatch = compat.fullmatch
disk_usage = compat.disk_usage
ThreadingHTTPServer = opsserver.OpsHTTPServer
start_daemon = compat.start_daemon


def is_within(path, root):
    """判断 path 是否位于 root 目录内（py2.7 无 os.path.commonpath；保留 realpath 行为）"""
    real_path = os.path.realpath(path)
    real_root = os.path.realpath(root)
    if real_path == real_root:
        return True
    return real_path.startswith(real_root.rstrip('\\/') + os.sep)


# ================= 默认配置 =================
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 8089
DEFAULT_UPLOAD_DIR = './uploads'
DEFAULT_MAX_UPLOAD_MB = 0  # 0 表示不限制单次请求体大小
DEFAULT_CHUNK_SIZE_MB = 16  # 分片上传单片大小
# 显式 TCP 收发缓冲（MB）。默认 0 = 不设置，交给内核自适应：Linux 的 tcp_rmem/wmem
# 自动调优在千兆内网已经是满速配置，显式设置反而会关掉该 socket 的自动调优。
# 只有在「高时延链路 / 容器里 rmem_max 被调小 / 无线」导致接收窗口成为瓶颈时才调大。
DEFAULT_SOCK_BUF_MB = 0
STALE_CHUNK_SECONDS = 86400  # 分片/断点任务超过 24h 未活动则清理
VERSION = '3.3.1'
START_TIME = time.time()

# 分片落盘模式：
#   chunkfile —— 每片写独立文件（片内顺序写）→ complete 时一次顺序拼接。
#                各文件系统上最快的写入模式，且并发分片之间互不争抢文件内的块分配。
#   offset    —— （v3.1.1 引入）预分配单个稀疏文件，各片按 offset 直写，
#                complete 只需 rename。省掉拼接，但代价是「并发随机偏移写 +
#                ftruncate 预分配」，在 NTFS / NFS / 部分 ext4 配置上会慢一个数量级
#                （实测 256MB：chunkfile 336MB/s vs offset 40MB/s，串行 offset 仅 7.5MB/s）。
# 默认 chunkfile；确需零拼接且环境验证确实更快时再切 offset。
WRITE_MODE_CHUNKFILE = 'chunkfile'
WRITE_MODE_OFFSET = 'offset'
WRITE_MODES = (WRITE_MODE_CHUNKFILE, WRITE_MODE_OFFSET)
DEFAULT_WRITE_MODE = WRITE_MODE_CHUNKFILE
# ===========================================


def parse_args():
    p = argparse.ArgumentParser(description='零依赖文件/文件夹上传服务器 v%s' % VERSION)
    p.add_argument('--host', default=os.environ.get('UP_HOST', DEFAULT_HOST), help='监听地址 (默认 0.0.0.0)')
    p.add_argument('--port', type=int, default=int(os.environ.get('UP_PORT', DEFAULT_PORT)), help='监听端口 (默认 %d)' % DEFAULT_PORT)
    p.add_argument('--upload-dir', default=os.environ.get('UP_DIR', DEFAULT_UPLOAD_DIR), help='文件保存目录 (默认 ./uploads)')
    p.add_argument('--token', default=os.environ.get('UP_TOKEN', ''), help='访问令牌，设置后上传/列表/删除/下载需携带 X-Auth-Token')
    p.add_argument('--admin-token', default=os.environ.get('UP_ADMIN_TOKEN', ''), help='管理令牌，设置后 POST /api/control 需携带')
    p.add_argument('--pidfile', default='', help='PID 文件路径，用于外部启停管理')
    p.add_argument('--max-upload-mb', type=int, default=int(os.environ.get('UP_MAX_MB', DEFAULT_MAX_UPLOAD_MB)), help='单次上传大小上限(MB)，0 不限制')
    p.add_argument('--chunk-size-mb', type=int, default=int(os.environ.get('UP_CHUNK_MB', DEFAULT_CHUNK_SIZE_MB)), help='分片上传单片大小(MB) (默认 %d)' % DEFAULT_CHUNK_SIZE_MB)
    p.add_argument('--chunk-write-mode',
                   default=os.environ.get('UP_WRITE_MODE', DEFAULT_WRITE_MODE),
                   choices=list(WRITE_MODES),
                   help='分片落盘模式：chunkfile=按片独立文件+complete 拼接(默认，各文件系统上最快)；'
                        'offset=预分配稀疏文件按 offset 直写(complete 零拷贝，但并发随机写慢)')
    p.add_argument('--sock-buf-mb', type=int,
                   default=int(os.environ.get('UP_SOCK_BUF_MB', DEFAULT_SOCK_BUF_MB)),
                   help='显式设置 TCP 收发缓冲(MB)，0=不干预交给内核自适应(默认)。'
                        '仅在高时延链路/容器内 rmem_max 偏小/无线导致窗口受限时才需要')
    p.add_argument('--allow-client-dir',
                   action='store_true',
                   default=os.environ.get('UP_ALLOW_CLIENT_DIR', '').lower() in ('1', 'true', 'yes', 'on'),
                   help='允许上传请求携带 dir 参数（绝对路径）逐请求覆盖存储根。'
                        '开启后 release_center 页面配置的「远端上传目录」即时生效，'
                        '改目录无需重启本服务；接口有 token 鉴权，权限制约与 web_shell 一致')
    return p.parse_args()


ARGS = parse_args()
UPLOAD_DIR = os.path.abspath(ARGS.upload_dir)
PORT = ARGS.port
HOST = ARGS.host
TOKEN = ARGS.token
ADMIN_TOKEN = ARGS.admin_token
PIDFILE = os.path.abspath(ARGS.pidfile) if ARGS.pidfile else ''
MAX_UPLOAD_BYTES = ARGS.max_upload_mb * 1024 * 1024
CHUNK_SIZE_BYTES = max(1, ARGS.chunk_size_mb) * 1024 * 1024
CHUNK_WRITE_MODE = ARGS.chunk_write_mode
SOCK_BUF_BYTES = max(0, ARGS.sock_buf_mb) * 1024 * 1024
CHUNK_TMP_DIR = os.path.join(UPLOAD_DIR, '.tmp', 'chunks')   # 分片临时目录（.tmp 不参与列表）
RESUME_TMP_DIR = os.path.join(UPLOAD_DIR, '.tmp', 'resume')  # 断点续传映射目录
ALLOW_CLIENT_DIR = bool(ARGS.allow_client_dir)               # 请求级 dir 覆盖总开关



# ---- 性能相关（v3.1.0）----
# 巡检/列表会全量遍历上传目录，文件多时开销明显：做短 TTL 缓存 + 写入时主动失效
STATUS_CACHE_TTL = 5
LIST_CACHE_TTL = 5
# 分片过期清理是 O(分片任务数) 的目录扫描，高频 init 下会成为瓶颈：限流
CLEANUP_MIN_INTERVAL = 600
READ_BUFSIZE = 1024 * 1024    # 分片/下载读缓冲，减少系统调用次数
_status_cache = {'ts': 0, 'payload': None}
_list_cache = {'ts': 0, 'payload': None}
_last_cleanup_ts = 0.0
_cache_lock = threading.Lock()

makedirs(UPLOAD_DIR)


def safe_rel_path(rel):
    """规范化相对路径并阻止目录穿越；返回去除前导分隔符的路径，非法时返回 None"""
    if not rel:
        return None
    norm = os.path.normpath(rel.replace('\\', '/'))
    # 去掉所有前导的 '/' 或 '../' 等穿越部分
    parts = [p for p in norm.split(os.sep) if p not in ('', '.', '..')]
    if not parts:
        return None
    return os.sep.join(parts)


def resolve_upload_path(rel):
    """把相对路径解析到 UPLOAD_DIR 内，越界返回 None"""
    safe = safe_rel_path(rel)
    if safe is None:
        return None
    target = os.path.join(UPLOAD_DIR, safe)
    if not is_within(target, UPLOAD_DIR):
        return None
    return target


# ================= 文件类型分类（v3.2.0） =================
# 类型枚举顺序（GET /api/types 返回；也用于页面类型下拉）
TYPE_ORDER = ["dir", "archive", "jar", "excel", "doc", "image", "log", "text", "binary"]

# 扩展名 -> 类型 映射（小写匹配）
_TYPE_EXT = {
    "archive": ("zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar"),
    "jar": ("jar", "war", "ear"),
    "excel": ("xlsx", "xlsm", "csv"),
    "doc": ("docx", "doc", "pdf", "pptx", "ppt", "txt", "md"),
    "image": ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"),
    "log": ("log", "out"),
    "text": ("sh", "py", "conf", "cfg", "ini", "yml", "yaml", "json", "xml",
             "properties", "sql"),
}


def classify_file_type(name, is_dir=False):
    """按扩展名归类文件类型（v3.2.0 新增，供 /api/list 与 /api/types 复用）"""
    if is_dir:
        return "dir"
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if not ext:
        return "binary"
    for t, exts in _TYPE_EXT.items():
        if ext in exts:
            return t
    return "binary"


# ================= 分片上传辅助 =================
def unique_filename(save_dir, filename):
    """返回不冲突的文件名（重名时自动追加 _1/_2/...）"""
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.exists(os.path.join(save_dir, candidate)):
        candidate = "%s_%d%s" % (base, counter, ext)
        counter += 1
    return candidate


# 落盘收尾（v3.2.2）：临时文件名唯一 + 「查重改名」持锁原子化。
# 早期版本所有请求共用「<目标名>.part」，且 unique_filename 只在 rename 前查一次，
# 于是并发写同名文件（或同一分片重传）会互相占用临时文件、抢同一个目标名，
# Windows 报 WinError 32 / Linux 后到者覆盖先到者，最终返回 500。
_save_name_lock = threading.Lock()


def temp_sibling(path):
    """为落盘临时文件取「同目录 + 唯一」的名字。

    必须同目录（rename 才是同文件系统内的元数据操作，不会退化成跨设备拷贝）；
    随机后缀保证并发请求各写各的临时文件，互不干扰。
    """
    return '%s.part-%s' % (path, uuid.uuid4().hex[:8])


def is_temp_artifact(name):
    """临时文件（<名>.part / <名>.part-xxxxxxxx）判定，列表接口据此过滤"""
    return bool(re.search(r'\.part(-[0-9a-f]{8})?$', name or ''))


def hidden_entry(name):
    """列表/树接口需要跳过的条目：服务端内部临时目录 + 正在落盘的临时文件"""
    return name in ('.tmp', '__pycache__') or is_temp_artifact(name)


def finalize_file(tmp_path, save_dir, base_name):
    """在锁内选唯一文件名并改名落地，返回 (final_name, save_path)。

    「查重 + rename」必须原子：两个并发请求各自查到「名字可用」后会争抢同一个名字，
    后到者要么覆盖先到者的文件、要么直接报错。持锁后第二个请求能看见第一个已落地的
    文件，自动改名为 name_1 继续。
    """
    with _save_name_lock:
        final_name = unique_filename(save_dir, base_name)
        save_path = os.path.join(save_dir, final_name)
        replace_retry(tmp_path, save_path)
    return final_name, save_path


def replace_retry(src, dst, attempts=4, delay=0.05):
    """os.replace 的可重试版本（并发改名 / 杀软瞬时占用导致的偶发失败）。

    Windows 上把文件改名到「同一目标」或目标正被杀软/索引服务扫描时会抛
    WinError 5(拒绝访问) / 32(文件被占用)，Linux 上偶发 EACCES 同理；
    这些都只是瞬时占用，短暂退避重试即可成功，不该把整片上传判失败。
    源文件已消失时说明不是竞争问题，直接抛出。
    """
    last = None
    for i in range(max(1, attempts)):
        try:
            os_replace(src, dst)
            return
        except (OSError, IOError) as e:
            last = e
            if not os.path.exists(src):
                raise
            time.sleep(delay * (i + 1))
    raise last


def new_upload_id():
    return uuid.uuid4().hex


def validate_upload_id(upload_id):
    return bool(upload_id) and fullmatch(r'[0-9a-f]{32}', upload_id or '')


def chunk_dir_for(upload_id):
    return os.path.join(CHUNK_TMP_DIR, upload_id)


_utime_ts = {}   # cdir -> 上次刷新 mtime 的时间，避免每片都做一次元数据写


def touch_cdir(cdir, interval=10):
    """节流版 os.utime：目录 mtime 只用于过期清理判定，无需每片刷新"""
    now = time.time()
    if now - _utime_ts.get(cdir, 0) < interval:
        return
    _utime_ts[cdir] = now
    if len(_utime_ts) > 512:
        _utime_ts.clear()
    try:
        os.utime(cdir, None)
    except (OSError, IOError):
        pass


def concat_chunks(cdir, save_path, total_chunks, bufsize=8 * 1024 * 1024):
    """把分片文件按序拼接为最终文件（chunkfile 模式的 complete 阶段）。

    首选系统 cat：coreutils 内部走 copy_file_range/sendfile，数据在内核态搬运，
    不占 Python 内存也不做用户态拷贝；NFS 上还有机会变成服务端复制。
    py2 没有 os.sendfile，因此这是 Linux 上最快的拼接路径。
    失败（命令不存在/非 POSIX/文件缺失）时回退用户态大缓冲拷贝。
    """
    paths = [os.path.join(cdir, str(i)) for i in range(total_chunks)]
    if _cat_concat(paths, save_path):
        return
    with open(save_path, 'wb') as out:
        for p in paths:
            with open(p, 'rb') as cf:
                shutil.copyfileobj(cf, out, bufsize)


def _cat_concat(paths, out_path):
    """用系统 cat 拼接；成功返回 True，不可用/失败返回 False（由调用方回退）。"""
    if not paths or compat.IS_WIN:
        return False
    cat = compat.which('cat')
    if not cat:
        return False
    try:
        with open(out_path, 'wb') as out:
            devnull = open(os.devnull, 'wb')
            try:
                # 传 argv 列表（不经过 shell），路径含空格/中文均安全
                rc = subprocess.call([cat] + paths, stdout=out.fileno(), stderr=devnull)
            finally:
                devnull.close()
    except (OSError, IOError, ValueError):
        return False
    return rc == 0


# ---- 分片暂存（v3.1.1）：按 offset 直写，complete 只需 rename ----
DONE_MARK_NAME = '__done'   # 完成标记：total_chunks 字节，第 i 字节为 \x01 表示分片 i 已落盘
DATA_FILE_NAME = 'data'     # 暂存数据：整文件按 offset 稀疏预分配，分片原地写入
META_FILE_NAME = '__meta'   # 任务元信息：{chunk_size, size, total_chunks}
MAX_CHUNK_SIZE_BYTES = 256 * 1024 * 1024   # 客户端可声明的单片硬上限（防异常值）


def done_mark_path(cdir):
    return os.path.join(cdir, DONE_MARK_NAME)


def data_file_path(cdir):
    return os.path.join(cdir, DATA_FILE_NAME)


def read_chunk_meta(cdir):
    """读取任务元信息；不存在（升级前遗留任务）返回 None，调用方据此走 legacy 流程"""
    try:
        with open_text(os.path.join(cdir, META_FILE_NAME), 'r') as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else None
    except (OSError, IOError, ValueError):
        return None


def chunk_write_mode_of(meta, cdir):
    """判定分片任务用的落盘模式。

    meta 显式记录 write_mode 的按记录走；未记录（v3.1.1~v3.2.0 建立的任务）则按
    是否存在 data 暂存文件判定：有 data 即 offset 布局，否则按片文件布局。
    """
    mode = (meta or {}).get('write_mode')
    if mode in WRITE_MODES:
        return mode
    return WRITE_MODE_OFFSET if os.path.isfile(data_file_path(cdir)) else WRITE_MODE_CHUNKFILE


def write_chunk_meta(cdir, meta):
    path = os.path.join(cdir, META_FILE_NAME)
    tmp = path + '.tmp'
    with open_text(tmp, 'w') as f:
        f.write(to_text(json.dumps(meta)))
    os_replace(tmp, path)


def ensure_done_mark(cdir, total_chunks):
    """创建/校正 __done 标记文件（total_chunks 字节，第 i 字节 1 表示片 i 已落盘）"""
    done_path = done_mark_path(cdir)
    if os.path.exists(done_path):
        return
    with open(done_path, 'wb') as f:
        f.truncate(total_chunks)


def mark_done(cdir, index):
    """置位分片完成标记。单字节写入本身是原子的：只在数据落盘后才置位，
    中断则该片会被重传覆盖。"""
    with open(done_mark_path(cdir), 'r+b') as f:
        f.seek(index)
        f.write(b'\x01')


def prealloc_data(data_path, size):
    """offset 模式下的一次性稀疏预分配。

    只在文件尚未建立时调用：对同一文件重复 ftruncate 在 NFS / Windows 上代价很高
    （NTFS 会真实填零，NFS 可能触发 COMMIT），绝不可放在每片请求的热路径上。
    """
    if not size:
        return
    try:
        f = open(data_path, 'r+b')
    except (OSError, IOError):
        f = open(data_path, 'wb')
    with f:
        f.truncate(size)


def ensure_upload_files(cdir, total_chunks, size, mode=DEFAULT_WRITE_MODE):
    """按落盘模式准备暂存文件：两种模式都需要 __done；data 预分配仅 offset 模式需要"""
    done_path = done_mark_path(cdir)
    data_path = data_file_path(cdir)
    size = size or 0
    if mode == WRITE_MODE_OFFSET and size:
        cur = os.path.getsize(data_path) if os.path.exists(data_path) else 0
        if cur > size:
            # 暂存比目标大（正常不会发生：resume_key 已含文件大小）——重来，避免分片错位
            for p in (done_path, data_path):
                try:
                    os.remove(p)
                except (OSError, IOError):
                    pass
    ensure_done_mark(cdir, total_chunks)
    if mode == WRITE_MODE_OFFSET and size:
        cur = os.path.getsize(data_path) if os.path.exists(data_path) else 0
        if cur < size:
            prealloc_data(data_path, size)


def uploaded_indexes(cdir, total_chunks):
    """统计已传分片：合并 __done 标记与独立分片文件两种来源。

    取并集的原因：v3.1.1 之前只有分片文件、之后才有 __done 标记，中间升级过的任务
    两种来源会各有一部分已完成的分片；只认一种会误判缺片，导致续传重复上传或
    complete 失败。正常情况下（标记齐全）不会做 listdir，开销不变。
    """
    try:
        with open(done_mark_path(cdir), 'rb') as f:
            flags = f.read()
    except (OSError, IOError):
        flags = None
    got = set()
    if flags:
        for i in range(min(total_chunks, len(flags))):
            if flags[i:i + 1] == b'\x01':
                got.add(i)
    if flags is None or len(got) < total_chunks:
        try:
            names = os.listdir(cdir)
        except (OSError, IOError):
            names = []
        for n in names:
            if n.isdigit():          # __blob / __meta / data 等非索引名自动排除
                i = int(n)
                if 0 <= i < total_chunks:
                    got.add(i)
    return [i for i in range(total_chunks) if i in got]


def invalidate_caches():
    """文件发生变化后清空目录扫描缓存，保证列表/状态立即反映最新结果"""
    with _cache_lock:
        _status_cache['payload'] = None
        _list_cache['payload'] = None


def maybe_cleanup_stale_chunks(force=False):
    """限流版清理：默认最短间隔 CLEANUP_MIN_INTERVAL 秒，避免每次 init 都全量扫目录"""
    global _last_cleanup_ts
    now = time.time()
    with _cache_lock:
        if not force and now - _last_cleanup_ts < CLEANUP_MIN_INTERVAL:
            return
        _last_cleanup_ts = now
    cleanup_stale_chunks()


def cleanup_stale_chunks(max_age=STALE_CHUNK_SECONDS):
    """清理超过 max_age 秒未活动的分片目录，及指向已失效分片的断点续传映射"""
    try:
        if os.path.isdir(CHUNK_TMP_DIR):
            now = time.time()
            for name in os.listdir(CHUNK_TMP_DIR):
                d = os.path.join(CHUNK_TMP_DIR, name)
                try:
                    if os.path.isdir(d) and now - os.path.getmtime(d) > max_age:
                        shutil.rmtree(d, ignore_errors=True)
                except (OSError, IOError):
                    pass
        if os.path.isdir(RESUME_TMP_DIR):
            for rname in os.listdir(RESUME_TMP_DIR):
                rp = os.path.join(RESUME_TMP_DIR, rname)
                try:
                    with open_text(rp, 'r') as f:
                        rid = json.load(f).get('upload_id', '')
                    if not (validate_upload_id(rid) and os.path.isdir(chunk_dir_for(rid))):
                        os.remove(rp)
                except Exception:
                    try:
                        os.remove(rp)
                    except (OSError, IOError):
                        pass
    except (OSError, IOError):
        pass


# 请求级 dir 覆盖时禁止写入的敏感系统目录（P0：否则等于「任意路径写文件」）
_FORBIDDEN_DIR_PREFIXES = (
    '/etc', '/bin', '/sbin', '/boot', '/dev', '/proc', '/sys', '/lib', '/lib64',
    '/root', '/usr/bin', '/usr/sbin', '/usr/lib', '/usr/lib64', '/var/lib',
    '/vol', '/System',
)


def is_forbidden_dir(d):
    """判断请求级 dir 是否落在系统目录 / 根目录 / UNC / Windows 盘符根上。"""
    norm = (d or '').replace('\\', '/').rstrip('/')
    if not norm:
        return True
    if re.match(r'^[A-Za-z]:$', norm):
        return True
    up = norm.upper()
    if norm.startswith('//') or up.startswith('//?/') or up.startswith('//./'):
        return True
    low = norm.lower()
    for p in _FORBIDDEN_DIR_PREFIXES:
        if low == p or low.startswith(p + '/'):
            return True
    return False


def resolve_base_dir(client_dir):
    """请求级存储根覆盖（v3.3.0，需启动时加 --allow-client-dir）。

    返回 (base_dir, error)：
      * client_dir 为空        → (UPLOAD_DIR, None)，行为与旧版完全一致；
      * 开关未开但带了 dir     → (UPLOAD_DIR, None) —— 静默忽略而非报错：
        老调用方/未升级部署不受影响，响应里始终带真实落盘路径，调用方据此提示；
      * dir 不是绝对路径       → (None, 错误信息)，这是配置错误，静默落到别处更难查；
      * dir 指向 / 或系统目录  → (None, 错误信息)，否则等于「任意路径写文件」；
      * 其余                   → (normpath 后的绝对路径, None)，目录在落盘前按需创建。
    """
    d = (client_dir or '').strip()
    if not d or not ALLOW_CLIENT_DIR:
        return UPLOAD_DIR, None
    d = os.path.normpath(d)
    if not os.path.isabs(d):
        # py2 下 u'' 格式串必须配 unicode 参数，否则中文提示会触发隐式 ascii 解码错误
        return None, u'dir 必须是绝对路径（收到: %s）' % compat.to_text(client_dir)
    if is_forbidden_dir(d):
        return None, u'dir 指向系统目录或根目录，不允许写入: %s' % compat.to_text(client_dir)
    return d, None


def resolve_chunk_save_path(relative_path, filename, base_dir=None):
    """根据相对路径/文件名计算分片合并后的最终保存目录与文件名（不写盘）。

    兼容两种客户端传参风格：
      * relative_path 是「含文件名的整路径」（页面/文件夹上传：webkitRelativePath
        = 'a/b/c.txt'）—— 直接用其目录部分；
      * relative_path 只给「目录」（脚本式调用：relative_path='a/b' + filename='c.txt'）
        —— 目录下存 filename。
    判定依据：相对路径的最后一段与 filename 同名即视为含文件名的整路径，否则视为目录。

    base_dir：请求级存储根（--allow-client-dir 开启时由 dir 参数决定），默认 UPLOAD_DIR。

    v3.2.2 修正：早期版本一律把相对路径最后一段当文件名，于是「只给目录」的调用会把
    文件全部存成目录名（并发同名还会互相覆盖、报 500）。页面调用风格不受影响。
    """
    root = base_dir or UPLOAD_DIR
    fname = os.path.basename((filename or '').replace('\\', '/').rstrip('/'))
    if not fname:
        fname = 'unnamed_file'
    rel = (relative_path or '').replace('\\', '/').strip('/')
    if not rel:
        return root, fname
    safe = safe_rel_path(rel)
    if safe is None:
        return None, None
    if safe.rsplit(os.sep, 1)[-1] == fname:
        # 相对路径已含文件名
        target_dir = os.path.join(root, os.path.dirname(safe))
    else:
        # 相对路径只是目录：文件落到该目录下
        target_dir = os.path.join(root, safe)
    # 兜底校验真实落点仍在存储根内：Windows 下 join(root,'C:/x') 会被绝对路径覆盖，
    # 符号链接目录也可能把词法上「在根内」的路径引到根外。
    if not is_within(target_dir, root):
        return None, None
    return target_dir, fname


def saved_meta(save_path, base_dir=None):
    """上传成功时随响应返回的「真实落盘信息」。

    调用方（release_center 推包、平台批量上传）**不要靠配置去猜**文件落在哪：
    目标机 upload_server 的存储根是它自己的 `--upload-dir`（默认 ./uploads，
    相对**进程工作目录**），和调用方在页面上填的目录是两回事。早期调用方拿
    `upload_dir + 文件名` 拼远端路径，一旦两者不一致，${package} 就指向一个
    不存在的文件，而后面的 `md5sum` 取不到摘要又被静默放过 —— 直到某一步 tar 才炸，
    现场很难定位（用户报的「配置了 /home/xpe/app_temp 却进了 uploads/」就是这个）。
    统一返回真实路径，调用方优先采用，配置只作兜底。

    base_dir：本次落盘实际使用的存储根（请求级 dir 覆盖时与 UPLOAD_DIR 不同），
    relative_path 相对它计算；算出来越界（..）时退化为文件名，避免误导调用方。
    """
    root = base_dir or UPLOAD_DIR
    try:
        rel = os.path.relpath(save_path, root).replace('\\', '/')
        if rel == '..' or rel.startswith('../'):
            rel = os.path.basename(save_path)
    except (ValueError, OSError):
        rel = os.path.basename(save_path)
    return {'path': save_path,
            'dir': os.path.dirname(save_path),
            'upload_dir': UPLOAD_DIR,
            'relative_path': rel}


# ================= 前端页面模板（与后端分离） =================
# 页面已拆到脚本同级目录，改完刷新浏览器即生效（按 mtime 缓存，无需重启服务）：
#   templates/upload_server.html   页面骨架，含 {{upload_dir}} / {{version}} 两个占位符
# 可用环境变量 UP_TEMPLATE_DIR 指定其它模板目录。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_NAME = 'upload_server.html'
_TEMPLATE_DIRS = []
_TEMPLATE_CACHE = {'path': None, 'mtime': 0, 'text': None}


def find_template(name):
    """按优先级查找模板：UP_TEMPLATE_DIR -> 脚本同级 templates/ -> 当前工作目录 templates/"""
    if not _TEMPLATE_DIRS:
        env_dir = os.environ.get('UP_TEMPLATE_DIR', '')
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
            '<p>期望路径：<code>templates/%s</code>（与 upload_server.py 同级），'
            '或用环境变量 UP_TEMPLATE_DIR 指定模板目录。</p></body></html>' % TEMPLATE_NAME)
    page = text.replace('{{upload_dir}}', html_escape(UPLOAD_DIR))
    page = page.replace('{{version}}', VERSION)
    if TOKEN:
        page = page.replace('window.needLogin = false;', 'window.needLogin = true;')
    return to_bytes(page)



def build_status_payload():
    """统计上传目录文件数/总大小 + 磁盘容量（结果可缓存）"""
    total = 0
    count = 0
    for root, _, files in os.walk(UPLOAD_DIR):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
                count += 1
            except (OSError, IOError):
                pass
    try:
        disk_total, disk_used, disk_free = disk_usage(UPLOAD_DIR)
        disk_pct = round(disk_used * 100.0 / disk_total, 1) if disk_total else 0
    except (OSError, IOError, AttributeError, ValueError):
        disk_total = disk_used = disk_free = 0
        disk_pct = 0
    return {
        "version": VERSION,
        "uptime": int(time.time() - START_TIME),
        "upload_dir": UPLOAD_DIR,
        "allow_client_dir": ALLOW_CLIENT_DIR,
        "file_count": count,
        "total_size": total,
        "token_required": bool(TOKEN),
        "pid": os.getpid(),
        "server_time": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "disk_pct": disk_pct,
    }


def scan_uploaded_files():
    """遍历上传目录返回文件列表（按修改时间倒序），结果可缓存"""
    files_info = []
    for root, dirs, files in os.walk(UPLOAD_DIR):
        dirs[:] = [d for d in dirs if not hidden_entry(d)]
        for name in files:
            if hidden_entry(name):
                continue          # 正在落盘的 <名>.part-xxxxxxxx 不进列表
            filepath = os.path.join(root, name)
            rel_path = os.path.relpath(filepath, UPLOAD_DIR).replace('\\', '/')
            try:
                st = os.stat(filepath)
                mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                files_info.append({'name': rel_path, 'size': st.st_size, 'mtime': mtime})
            except (OSError, IOError):
                continue
    files_info.sort(key=lambda x: x['mtime'], reverse=True)
    return files_info


class ThreadedHTTPServer(ThreadingHTTPServer):
    """并发 HTTP 服务：放大 accept 队列，避免并发分片连接被丢弃后重传导致的速度抖动"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_bind(self):
        # --sock-buf-mb 生效点：在监听套接字上设置，accept 出来的连接继承该值（Linux）。
        # 默认 0 不设置，保留内核自动调优（内网通常已满速）。
        if SOCK_BUF_BYTES:
            for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, opt, SOCK_BUF_BYTES)
                except (OSError, IOError):
                    pass
        ThreadingHTTPServer.server_bind(self)


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    disable_nagle_algorithm = True   # 小响应（分片确认/预检）立即发出，减少卡顿
    # py2：socket.makefile 默认只给 8KB 缓冲，读满 1MB 分片要 128 次 recv，
    # 并发分片时系统调用与 GIL 切换开销明显。给足 1MB 缓冲即可一次读一大块。
    # py3 忽略该属性（内部用 BufferedReader，read(n) 本身按 n 分配缓冲）。
    rbufsize = READ_BUFSIZE

    # ---------- CORS：允许浏览器/管理平台跨域直连 ----------
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        # X-Upload-Id / X-Chunk-Index 用于分片上传：把变化的参数放进请求头，
        # 使分片请求 URL 保持不变——CORS 预检缓存键包含完整 URL，
        # URL 恒定才能让整轮上传只付一次 OPTIONS 预检，而不是每片一次。
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, X-Auth-Token, X-Upload-Id, X-Chunk-Index')
        # 缓存预检结果：否则每片一次 OPTIONS 预检，等于每段数据多一个 RTT
        self.send_header('Access-Control-Max-Age', '86400')
        BaseHTTPRequestHandler.end_headers(self)

    def do_OPTIONS(self):
        """CORS 预检请求"""
        self.send_response(204)
        self.end_headers()

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):
        if self.path.startswith('/api/list') or self.path.startswith('/api/status'):
            return  # 巡检/轮询请求不刷日志
        extra = ''
        # 上传类请求附上「本片字节数 + 服务端实测速率」：用来区分「客户端推不动」和
        # 「服务端落盘慢」——客户端显示 15MB/s 而这里记到 100MB/s，就是链路/客户端侧。
        t0 = getattr(self, '_req_t0', None)
        nbytes = getattr(self, '_req_bytes', 0) or 0
        if t0 and nbytes:
            dt = max(1e-6, time.time() - t0)
            rate = nbytes / dt
            # 小包用 KB 显示，避免出现 [0.0MB 0.0MB/s] 这种没信息量的输出
            size_txt = ('%dKB' % (nbytes // 1024)) if nbytes < 1048576 else ('%.1fMB' % (nbytes / 1048576.0))
            rate_txt = ('%.0fKB/s' % (rate / 1024.0)) if rate < 1048576 else ('%.1fMB/s' % (rate / 1048576.0))
            extra = '  [%s %s]' % (size_txt, rate_txt)
        log("[%s] %s - %s%s" % (self.log_date_time_string(), self.client_address[0],
                                fmt % args, extra))

    def _auth_ok(self, require_admin=False):
        """只做令牌判断，不写响应（供需要「已授权则多返回字段」的接口复用）。"""
        token = ADMIN_TOKEN if require_admin else TOKEN
        if not token:
            return True
        return self.headers.get('X-Auth-Token') == token

    def _auth(self, require_admin=False):
        """校验令牌；未配置 token 时放行。返回 True 通过。"""
        if self._auth_ok(require_admin):
            return True
        self._send_json(401, "error", "未授权或令牌错误")
        return False

    def _send_json(self, status_code, status, message_or_data):
        if isinstance(message_or_data, dict):
            payload = {"status": status}
            payload.update(message_or_data)
        else:
            payload = {"status": status, "message": message_or_data}
        body = json_bytes(payload)
        # 带请求体的请求若在读完 body 之前就返回错误（401/400/404…），残留的 body
        # 会在 HTTP/1.1 keep-alive 下被当成下一个请求，造成偶发解析错乱；
        # 直接断开连接（错误是低频路径）。不带 body 的请求不受影响。
        try:
            clen = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            clen = 0
        if status_code >= 400 and clen > 0:
            self.close_connection = True
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_mb=16):
        """读取 JSON 请求体，超限抛 ValueError"""
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > max_mb * 1024 * 1024:
            raise ValueError('请求体大小不合法')
        raw = self.rfile.read(length)
        return json.loads(raw.decode('utf-8') or '{}')

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/list':
            if not self._auth():
                return
            self.handle_list_files(parse_qs(parsed.query))
            return
        if path == '/api/status':
            self.handle_status()
            return
        if path == '/api/tree':
            if not self._auth():
                return
            self.handle_tree(parse_qs(parsed.query))
            return
        if path == '/api/types':
            if not self._auth():
                return
            self._send_json(200, "success", {"types": TYPE_ORDER})
            return
        if path == '/api/download':
            if not self._auth():
                return
            self.handle_download(parse_qs(parsed.query))
            return
        if path == '/api/chunk/status':
            if not self._auth():
                return
            self.handle_chunk_status(parse_qs(parsed.query))
            return
        if path == '/api/batch/download':
            if not self._auth():
                return
            self.handle_batch_download(parse_qs(parsed.query))
            return
        if path == '/api/health':
            # 供页面/客户端探测服务端能力：分片大小、落盘模式（页面据此切片，避免
            # 「页面 16MB / 服务端 64MB」不一致导致上传直接报错）
            self._send_json(200, "success", {
                "alive": True,
                "version": VERSION,
                "chunk_size": CHUNK_SIZE_BYTES,
                "chunk_write_mode": CHUNK_WRITE_MODE,
                "token_required": bool(TOKEN),
            })
            return

        # 默认返回页面（前端模板见 templates/upload_server.html）
        body = render_page()
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_status(self):
        now = time.time()
        with _cache_lock:
            cached = _status_cache['payload']
            if cached is not None and now - _status_cache['ts'] < STATUS_CACHE_TTL:
                payload = cached
            else:
                payload = build_status_payload()
                _status_cache['ts'] = now
                _status_cache['payload'] = payload
        # 运行时信息即时刷新，不缓存
        payload = dict(payload)
        payload['uptime'] = int(time.time() - START_TIME)
        payload['server_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if not self._auth_ok():
            # 未授权时不泄露上传目录 / PID / 磁盘容量 / 文件数（/api/list、/api/tree
            # 都要求鉴权，这里保持一致）；仍回 200，保证「200=在线」的探测语义不变。
            payload = {k: payload.get(k) for k in
                       ('version', 'uptime', 'allow_client_dir',
                        'token_required', 'server_time')}
        self._send_json(200, "success", payload)

    def handle_list_files(self, params=None):
        """文件列表：根据是否带结构化查询参数，返回兼容旧客户端的结构或分页结构"""
        params = params or {}
        new_keys = ('path', 'q', 'sort', 'order', 'page', 'size', 'type',
                    'time_from', 'time_to')
        if any(k in params for k in new_keys):
            self._handle_list_paged(params)
        else:
            self._handle_list_legacy(params)

    def _handle_list_legacy(self, params):
        """旧结构：全量遍历上传目录，返回 {files:[...], count}（兼容未升级的 ops_Platform）"""
        keyword = (params.get('q', [''])[0] or '').strip().lower() if params else ''
        now = time.time()
        with _cache_lock:
            cached = _list_cache['payload']
            if cached is None or now - _list_cache['ts'] >= LIST_CACHE_TTL:
                cached = scan_uploaded_files()
                _list_cache['ts'] = now
                _list_cache['payload'] = cached
            files_info = cached
        if keyword:
            files_info = [f for f in files_info if keyword in f['name'].lower()]
        self._send_json(200, "success", {"files": files_info, "count": len(files_info)})

    def _handle_list_paged(self, params):
        """新结构：支持 path / q / sort / order / page / size / type / time_from / time_to 筛选排序分页"""
        req_path = (params.get('path', [''])[0] or '').strip()
        if req_path:
            target = resolve_upload_path(req_path)
            if not target or not os.path.isdir(target):
                self._send_json(400, "error", "目录不存在或路径非法")
                return
        else:
            target = UPLOAD_DIR
            req_path = ''

        keyword = (params.get('q', [''])[0] or '').strip().lower()
        sort = (params.get('sort', [''])[0] or '').strip()
        order = (params.get('order', ['asc'])[0] or 'asc').strip().lower()
        if order not in ('asc', 'desc'):
            order = 'asc'
        type_filter = (params.get('type', [''])[0] or '').strip().lower()
        page = compat.to_int(params.get('page', ['1'])[0], 1, 1, 100000)
        size = compat.to_int(params.get('size', ['50'])[0], 50, 1, 500)
        time_from = compat.to_int(params.get('time_from', [''])[0], 0, 0, None)
        time_to = compat.to_int(params.get('time_to', [''])[0], 0, 0, None)

        # ---- 列举当前目录条目（含子目录，目录优先展示）----
        entries = []
        dirs_list = []
        try:
            names = sorted(os.listdir(target))
        except (OSError, IOError):
            names = []
        for name in names:
            if hidden_entry(name):
                continue
            fp = os.path.join(target, name)
            rel = (req_path + '/' + name) if req_path else name
            if os.path.isdir(fp):
                try:
                    dst = os.stat(fp).st_mtime
                except (OSError, IOError):
                    dst = 0
                dirs_list.append(rel)
                entries.append({
                    'name': rel, 'type': 'dir', 'size': 0,
                    'mtime': (datetime.datetime.fromtimestamp(dst).strftime('%Y-%m-%d %H:%M:%S') if dst else '-'),
                    'mtime_ts': dst, 'is_dir': True,
                })
            else:
                try:
                    st = os.stat(fp)
                except (OSError, IOError):
                    continue
                entries.append({
                    'name': rel,
                    'type': classify_file_type(name, False),
                    'size': st.st_size,
                    'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'mtime_ts': st.st_mtime,
                    'is_dir': False,
                })

        # ---- 关键字 / 类型 / 时间范围 筛选 ----
        if keyword:
            entries = [e for e in entries if keyword in e['name'].lower()]
        if type_filter:
            entries = [e for e in entries if e['type'] == type_filter]
        if time_from > 0 or time_to > 0:
            entries = [e for e in entries
                       if (time_from <= 0 or e['mtime_ts'] >= time_from)
                       and (time_to <= 0 or e['mtime_ts'] <= time_to)]

        # ---- 排序（指定字段时按字段排；未指定排序时目录始终在前）----
        if sort in ('name', 'size', 'mtime', 'type'):
            def sort_key(e):
                if sort == 'name':
                    return e['name'].lower()
                if sort == 'size':
                    return e['size']
                if sort == 'mtime':
                    return e['mtime_ts']
                return (e['type'], e['name'].lower())
            entries.sort(key=sort_key)
            if order == 'desc':
                entries.reverse()
        else:
            entries.sort(key=lambda e: (0 if e['is_dir'] else 1, e['mtime_ts']), reverse=True)

        # ---- 分页 ----
        total = len(entries)
        pages = (total + size - 1) // size if size > 0 else 1
        if page > pages and pages > 0:
            page = pages
        start = (page - 1) * size
        page_items = entries[start:start + size]
        self._send_json(200, "success", {
            "files": page_items,
            "total": total,
            "page": page,
            "size": size,
            "pages": pages,
            "path": req_path,
            "dirs": dirs_list,
        })

    def handle_tree(self, params):
        """目录树浏览：从指定 path 展开到 depth 层，返回子目录清单与当前目录文件数"""
        params = params or {}
        req_path = (params.get('path', [''])[0] or '').strip()
        if req_path:
            target = resolve_upload_path(req_path)
            if not target or not os.path.isdir(target):
                self._send_json(400, "error", "目录不存在或路径非法")
                return
        else:
            target = UPLOAD_DIR
            req_path = ''
        depth = compat.to_int(params.get('depth', ['2'])[0], 2, 1, 10)

        base_rel = os.path.relpath(target, UPLOAD_DIR).replace('\\', '/')
        if base_rel in ('.', './'):
            base_rel = ''
        dirs = []
        files_count = 0
        try:
            for n in sorted(os.listdir(target)):
                if hidden_entry(n):
                    continue
                fp = os.path.join(target, n)
                if os.path.isfile(fp):
                    files_count += 1
        except (OSError, IOError):
            pass

        def recurse(d, cur_rel, left):
            try:
                names = sorted(os.listdir(d))
            except (OSError, IOError):
                return
            for name in names:
                if hidden_entry(name):
                    continue
                fp = os.path.join(d, name)
                if not os.path.isdir(fp):
                    continue
                sub_rel = (cur_rel + '/' + name) if cur_rel else name
                dirs.append({'name': name, 'path': sub_rel,
                             'has_children': self._dir_has_subdir(fp)})
                if left > 1:
                    recurse(fp, sub_rel, left - 1)

        recurse(target, base_rel, depth)
        self._send_json(200, "success", {
            "path": base_rel,
            "dirs": dirs,
            "files_count": files_count,
        })

    def _dir_has_subdir(self, d):
        """判断目录是否还包含子目录（供树形 has_children 标记）"""
        try:
            for name in os.listdir(d):
                if hidden_entry(name):
                    continue
                if os.path.isdir(os.path.join(d, name)):
                    return True
        except (OSError, IOError):
            pass
        return False

    def handle_download(self, params):
        rel = params.get('path', [''])[0]
        target = resolve_upload_path(rel)
        if not target or not os.path.isfile(target):
            self._send_json(404, "error", "文件不存在")
            return
        filename = os.path.basename(target)
        started = False
        try:
            size = os.path.getsize(target)
            start, end = 0, size - 1
            is_range = False
            range_header = self.headers.get('Range')
            if range_header:
                m = re.match(r'bytes=(\d*)-(\d*)', range_header.strip())
                if m:
                    rs, re_ = m.group(1), m.group(2)
                    if not rs and re_:
                        # 后缀区间 `bytes=-N`：取最后 N 字节（旧实现当成 0~N，下载内容错）
                        n = int(re_)
                        start = max(0, size - n)
                        end = size - 1
                    else:
                        if rs:
                            start = int(rs)
                        if re_:
                            end = min(int(re_), size - 1)
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header('Content-Range', 'bytes */%d' % size)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    is_range = True
            self.send_response(206 if is_range else 200)
            if is_range:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            self.send_header('Content-type', 'application/octet-stream')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % quote(filename))
            self.send_header('Accept-Ranges', 'bytes')
            chunk_len = end - start + 1
            self.send_header('Content-Length', str(chunk_len))
            self.end_headers()
            started = True
            with open(target, 'rb') as f:
                f.seek(start)
                remaining = chunk_len
                while remaining > 0:
                    chunk = f.read(min(READ_BUFSIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except CLIENT_GONE:
            pass  # 客户端断开，静默结束不刷错误日志
        except (OSError, IOError) as e:
            if started:
                # 响应头与部分 body 已发出，绝不能再补写第二个响应
                # （浏览器会报 ERR_INVALID_RESPONSE）；断开连接即可。
                self.close_connection = True
            else:
                self._send_json(500, "error", "读取失败: %s" % e)

    # ---------- POST ----------
    def do_POST(self):
        # 记录请求起点：访问日志据此打印「本片字节数 + 服务端实测速率」
        self._req_t0 = time.time()
        self._req_bytes = 0
        parsed = urlparse(self.path)
        if parsed.path == '/api/control':
            if not self._auth(require_admin=True):
                return
            self.handle_control()
            return

        # 分片上传与批量操作接口（兼容 Python 3.13+，不依赖 cgi）
        if parsed.path == '/api/chunk/init':
            if not self._auth():
                return
            self.handle_chunk_init()
            return
        if parsed.path == '/api/chunk/upload':
            if not self._auth():
                return
            self.handle_chunk_upload(parse_qs(parsed.query))
            return
        if parsed.path == '/api/chunk/complete':
            if not self._auth():
                return
            self.handle_chunk_complete()
            return
        if parsed.path == '/api/chunk/put':
            # 单片文件免分片直传：一次请求完成 init+upload+complete
            if not self._auth():
                return
            self.handle_chunk_put(parse_qs(parsed.query))
            return
        if parsed.path == '/api/batch/delete':
            if not self._auth():
                return
            self.handle_batch_delete()
            return
        if parsed.path == '/api/batch/download':
            if not self._auth():
                return
            self.handle_batch_download()
            return

        if parsed.path != '/':
            self._send_json(404, "error", "接口不存在")
            return
        if not self._auth():
            return
        self.handle_upload()

    def handle_control(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > 1024 * 1024:
                # 控制指令请求体很小；不设上限会让超大请求体直接吃满内存
                raise ValueError('请求体大小不合法')
            data = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except Exception:
            self._send_json(400, "error", "请求体格式错误")
            return
        action = data.get('action', '')
        if action not in ('stop', 'restart'):
            self._send_json(400, "error", "action 仅支持 stop / restart")
            return
        self._send_json(200, "success", {"message": "服务即将%s" % ("停止" if action == "stop" else "重启"), "action": action})
        # 后台延迟 200ms 执行，确保响应先返回
        threading.Timer(0.2, shutdown_server, args=(action,)).start()

    def handle_upload(self):
        content_type = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type:
            self._send_json(400, "error", "请求格式错误")
            return

        if MAX_UPLOAD_BYTES:
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_UPLOAD_BYTES:
                self._send_json(413, "error", "上传内容超过大小限制 (%d MB)" % ARGS.max_upload_mb)
                return

        if cgi is None:
            # Python 3.13+ 简化处理：不支持，提示使用 Python 3.7-3.12
            self._send_json(500, "error", "当前 Python 版本不再支持 cgi，请使用 Python 3.7~3.12")
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type}
        )

        if 'file' not in form:
            self._send_json(400, "error", "未检测到文件字段")
            return
        fileitem = form['file']
        if not fileitem.filename:
            self._send_json(400, "error", "您没有选择任何文件")
            return

        base_dir, dir_err = resolve_base_dir(form.getvalue('dir', '') or '')
        if dir_err:
            self._send_json(400, "error", dir_err)
            return
        relative_path = form.getvalue('relative_path', '')
        if relative_path:
            safe = safe_rel_path(relative_path)
            if safe is None:
                self._send_json(400, "error", "非法路径")
                return
            save_dir = os.path.join(base_dir, os.path.dirname(safe))
            makedirs(save_dir)
            filename = os.path.basename(safe)
        else:
            filename = os.path.basename(fileitem.filename)
            save_dir = base_dir

        if not filename:
            filename = "unnamed_file"

        save_path = os.path.join(save_dir, filename)
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(save_path):
            filename = "%s_%d%s" % (base, counter, ext)
            save_path = os.path.join(save_dir, filename)
            counter += 1

        try:
            with open(save_path, 'wb') as f:
                while True:
                    chunk = fileitem.file.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            invalidate_caches()
            payload = {"filename": filename,
                       "message": "文件 '%s' 上传成功！" % html_escape(filename)}
            payload.update(saved_meta(save_path, base_dir))
            self._send_json(200, "success", payload)
        except Exception as e:
            self._send_json(500, "error", "保存失败: %s" % html_escape(str(e)))

    # ---------- DELETE ----------
    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != '/api/file':
            self._send_json(404, "error", "接口不存在")
            return
        if not self._auth():
            return
        rel = parse_qs(parsed.query).get('path', [''])[0]
        target = resolve_upload_path(rel)
        if not target or not os.path.exists(target):
            self._send_json(404, "error", "文件不存在")
            return
        try:
            if os.path.isdir(target):
                import shutil
                shutil.rmtree(target)
            else:
                os.remove(target)
            invalidate_caches()
            self._send_json(200, "success", "已删除: %s" % rel)
        except (OSError, IOError) as e:
            self._send_json(500, "error", "删除失败: %s" % e)

    # ---------- 分片上传 / 断点续传 ----------
    def handle_chunk_init(self):
        """初始化分片上传任务。支持 resume_key 断点续传：返回已上传的分片索引"""
        try:
            data = self._read_json_body()
        except Exception:
            self._send_json(400, "error", "请求体格式错误")
            return
        filename = (data.get('filename') or '').strip()
        relative_path = data.get('relative_path') or ''
        try:
            size = int(data.get('size') or 0)
            total_chunks = int(data.get('total_chunks') or 0)
            chunk_size = int(data.get('chunk_size') or 0)
        except (TypeError, ValueError):
            self._send_json(400, "error", "size/total_chunks 必须为数字")
            return
        # 分片大小以客户端声明为准（客户端按它切片），据此刻算每片写入偏移；
        # 必须原样采纳，否则客户端切片与服务端 offset 会错位导致文件损坏。
        # 未声明或不合法时回退服务端配置值，另设一个硬上限防异常值。
        if chunk_size <= 0:
            chunk_size = CHUNK_SIZE_BYTES
        chunk_size = min(chunk_size, MAX_CHUNK_SIZE_BYTES)
        resume_key = (data.get('resume_key') or '').strip()
        if not filename or total_chunks <= 0 or total_chunks > 10000:
            self._send_json(400, "error", "参数不合法: filename/total_chunks")
            return
        if size < 0:
            self._send_json(400, "error", "参数不合法: size")
            return
        if resume_key and not fullmatch(r'[0-9a-f]{16,32}', resume_key):
            self._send_json(400, "error", "非法 resume_key")
            return
        if MAX_UPLOAD_BYTES and size > MAX_UPLOAD_BYTES:
            self._send_json(413, "error", "上传文件超过大小限制 (%d MB)" % ARGS.max_upload_mb)
            return

        base_dir, dir_err = resolve_base_dir(data.get('dir') or '')
        if dir_err:
            self._send_json(400, "error", dir_err)
            return
        save_dir, base_name = resolve_chunk_save_path(relative_path, filename, base_dir)
        if save_dir is None:
            self._send_json(400, "error", "非法路径")
            return

        maybe_cleanup_stale_chunks()

        # 断点续传：若 resume_key 已存在且分片目录有效，则复用原 upload_id
        upload_id = None
        if resume_key:
            rp = os.path.join(RESUME_TMP_DIR, resume_key)
            try:
                with open_text(rp, 'r') as f:
                    rid = json.load(f).get('upload_id', '')
                if validate_upload_id(rid) and os.path.isdir(chunk_dir_for(rid)):
                    upload_id = rid
            # py2 中 IOError 与 OSError 是两个独立类（仅同源于 EnvironmentError），
            # 文件不存在/不可读抛的是 IOError，必须显式捕获，否则异常会逃逸到
            # HTTPServer 打出 traceback。py3 下 IOError 是 OSError 别名，兼容。
            except (OSError, IOError, ValueError):
                pass
        created_new = upload_id is None
        if created_new:
            upload_id = new_upload_id()
            makedirs(chunk_dir_for(upload_id))
            if resume_key:
                makedirs(RESUME_TMP_DIR)
                tmp = os.path.join(RESUME_TMP_DIR, resume_key + '.tmp')
                with open_text(tmp, 'w') as f:
                    f.write(to_text(json.dumps({'upload_id': upload_id, 'created': time.time()})))
                os_replace(tmp, os.path.join(RESUME_TMP_DIR, resume_key))

        cdir = chunk_dir_for(upload_id)
        os.utime(cdir, None)   # 续传任务也算活跃，避免被清理逻辑误删
        if created_new:
            # 新建任务：记录落盘模式（默认 chunkfile：按片独立顺序写，complete 时拼接）
            # 与请求级存储根（base_dir）：complete 必须落到 init 定的同一目录，
            # 即使 complete 请求忘了带 dir（或续传任务的调用方换了参数）也不跑偏。
            # 续传任务不重建，保持原布局与模式。
            write_chunk_meta(cdir, {'chunk_size': chunk_size, 'size': size,
                                    'total_chunks': total_chunks,
                                    'write_mode': CHUNK_WRITE_MODE,
                                    'base_dir': base_dir})
            ensure_upload_files(cdir, total_chunks, size, CHUNK_WRITE_MODE)
        meta = read_chunk_meta(cdir)
        uploaded = uploaded_indexes(cdir, total_chunks)
        final_name = unique_filename(save_dir, base_name)
        rel_dir = os.path.dirname(relative_path).replace('\\', '/') if relative_path else ''
        self._send_json(200, "success", {
            "upload_id": upload_id,
            "chunk_size": (meta or {}).get('chunk_size') or chunk_size,
            "uploaded": uploaded,
            "filename": final_name,
            "save_path": rel_dir,
        })

    def handle_chunk_upload(self, params):
        """接收单个分片（请求体为分片原始二进制）

        upload_id / index 优先从请求头 X-Upload-Id / X-Chunk-Index 读取
        （URL 保持恒定，使 CORS 预检只发生一次），回退兼容 query 参数。
        """
        upload_id = self.headers.get('X-Upload-Id') or params.get('upload_id', [''])[0]
        if not validate_upload_id(upload_id):
            self._send_json(400, "error", "非法 upload_id")
            return
        try:
            index = int(self.headers.get('X-Chunk-Index') or params.get('index', ['-1'])[0])
        except (TypeError, ValueError):
            self._send_json(400, "error", "index 不合法")
            return
        # 允许 0 字节分片（空文件也走分片通道）；具体上限在读到任务元信息后判定
        length = int(self.headers.get('Content-Length') or 0)
        if length < 0:
            self._send_json(400, "error", "分片大小不合法")
            return
        if index < 0 or index >= 10000:
            self._send_json(400, "error", "分片索引越界")
            return
        cdir = chunk_dir_for(upload_id)
        if not os.path.isdir(cdir):
            self._send_json(404, "error", "上传任务不存在或已过期")
            return

        meta = read_chunk_meta(cdir)
        # 索引上界以任务声明的 total_chunks 为准：否则对 total_chunks=2 的任务上传
        # index=9999，offset 模式会 seek(9999*chunk_size) 真实分配出 TB 级空洞文件。
        total_chunks = int((meta or {}).get('total_chunks') or 0)
        if total_chunks and index >= total_chunks:
            self._send_json(400, "error", "分片索引越界")
            return
        # 长度上限：新任务以「声明的单片大小」为准（末片更小），legacy 任务放宽到 4×
        limit = (meta.get('chunk_size') or CHUNK_SIZE_BYTES) if meta else CHUNK_SIZE_BYTES * 4
        if length > limit:
            self._send_json(400, "error", "分片大小不合法")
            return
        try:
            if meta is None:
                self._save_chunk_legacy(cdir, index, length)
            else:
                mode = chunk_write_mode_of(meta, cdir)
                if mode == WRITE_MODE_OFFSET:
                    self._save_chunk_offset(cdir, meta, index, length)
                else:
                    self._save_chunk_file(cdir, index, length)
            touch_cdir(cdir)       # 刷新目录 mtime（内部节流），防止被清理逻辑误删
            self._req_bytes = length          # 供访问日志打印服务端实测速率
            self._send_json(200, "success", {"index": index, "received": length})
        except Exception as e:
            self._send_json(500, "error", "分片保存失败: %s" % html_escape(str(e)))

    def _read_chunk_body(self, dest, offset, length):
        """把请求体写入 dest 的 offset 处，返回实际写入字节数"""
        written = 0
        with open(dest, 'r+b') as f:
            f.seek(offset)
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(READ_BUFSIZE, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
                written += len(chunk)
        return written

    def _save_chunk_file(self, cdir, index, length):
        """chunkfile 模式：每片写独立文件。

        片内是纯顺序写（各文件系统上最快的写入模式），并发分片之间不争抢
        同一个文件内的块分配，也不做任何预分配。写 .part 后原子改名。
        """
        path = os.path.join(cdir, str(index))
        tmp_path = temp_sibling(path)
        written = 0
        try:
            with open(tmp_path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(READ_BUFSIZE, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
            if written != length:
                raise IOError('分片数据不完整 (%d/%d)' % (written, length))
            # 同名目标串行改名：客户端超时重传时同一分片会被并发写两次，
            # 并发 os.replace 到同一目标在 Windows 上会直接 WinError 5。
            with _save_name_lock:
                replace_retry(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except (OSError, IOError):
                pass
            raise
        mark_done(cdir, index)

    def _save_chunk_offset(self, cdir, meta, index, length):
        """offset 模式（v3.1.1 起可选）：分片按 offset 直写暂存文件，完成后仅置位 __done。
        整文件在 complete 时只需一次 rename，省掉 merge 的全量读+写。

        注意：并发随机偏移写 + 稀疏预分配在 NTFS/NFS 上会显著变慢，默认已不启用；
        确需使用时请先用 verify_upload_perf.py 在本机实测两种模式。
        """
        chunk_size = meta.get('chunk_size') or CHUNK_SIZE_BYTES
        data_path = data_file_path(cdir)
        ensure_done_mark(cdir, meta.get('total_chunks') or (index + 1))
        # 仅首次建立时预分配，避免每片 ftruncate（NFS/Windows 上代价极高）
        if not os.path.exists(data_path) or os.path.getsize(data_path) == 0:
            prealloc_data(data_path, meta.get('size') or 0)
        written = self._read_chunk_body(data_path, index * chunk_size, length)
        if written != length:
            raise IOError('分片数据不完整 (%d/%d)' % (written, length))
        mark_done(cdir, index)

    def _save_chunk_legacy(self, cdir, index, length):
        """升级前遗留任务（无 __meta）的按片文件布局：写 .part 后 rename，并补记完成标记"""
        chunk_path = os.path.join(cdir, str(index))
        tmp_path = temp_sibling(chunk_path)
        written = 0
        try:
            with open(tmp_path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(READ_BUFSIZE, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
            # 与 chunkfile 分支一致地校验写入长度：截断的分片不得被记为「已完成」，
            # 否则 complete 会拼出损坏文件。
            if written != length:
                raise IOError('分片数据不完整 (%d/%d)' % (written, length))
            with _save_name_lock:
                replace_retry(tmp_path, chunk_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except (OSError, IOError):
                pass
            raise
        if os.path.exists(done_mark_path(cdir)):
            mark_done(cdir, index)

    def handle_chunk_status(self, params):
        """查询已上传的分片索引，供断点续传客户端恢复"""
        upload_id = params.get('upload_id', [''])[0]
        if not validate_upload_id(upload_id):
            self._send_json(400, "error", "非法 upload_id")
            return
        try:
            total_chunks = int(params.get('total_chunks', ['0'])[0] or 0)
        except ValueError:
            total_chunks = 0
        cdir = chunk_dir_for(upload_id)
        if not os.path.isdir(cdir):
            self._send_json(404, "error", "上传任务不存在或已过期")
            return
        uploaded = uploaded_indexes(cdir, total_chunks)
        self._send_json(200, "success", {"uploaded": uploaded})

    def handle_chunk_complete(self):
        """收尾：暂存文件已是完整内容，直接 os.replace 重命名为最终文件（零拷贝）"""
        try:
            data = self._read_json_body()
        except Exception:
            self._send_json(400, "error", "请求体格式错误")
            return
        upload_id = (data.get('upload_id') or '').strip()
        if not validate_upload_id(upload_id):
            self._send_json(400, "error", "非法 upload_id")
            return
        try:
            total_chunks = int(data.get('total_chunks') or 0)
        except (TypeError, ValueError):
            self._send_json(400, "error", "total_chunks 必须为数字")
            return
        relative_path = data.get('relative_path') or ''
        filename = (data.get('filename') or '').strip()
        resume_key = (data.get('resume_key') or '').strip()
        try:
            expect_size = int(data.get('size') or 0)
        except (TypeError, ValueError):
            expect_size = 0
        if resume_key and not fullmatch(r'[0-9a-f]{16,32}', resume_key):
            self._send_json(400, "error", "非法 resume_key")
            return
        if total_chunks <= 0 or total_chunks > 10000:
            self._send_json(400, "error", "total_chunks 不合法")
            return
        cdir = chunk_dir_for(upload_id)
        if not os.path.isdir(cdir):
            self._send_json(404, "error", "上传任务不存在或已过期")
            return
        done = set(uploaded_indexes(cdir, total_chunks))
        missing = [i for i in range(total_chunks) if i not in done]
        if missing:
            self._send_json(409, "error", "缺少分片: %s" % (missing[:20],))
            return

        # 存储根以 init 时定下的为准（记录在任务元信息里）；complete 自带的 dir
        # 只作兜底（老任务元信息没有 base_dir 字段时），保证同一次上传不跑偏。
        meta0 = read_chunk_meta(cdir) or {}
        base_dir, dir_err = resolve_base_dir(meta0.get('base_dir') or data.get('dir') or '')
        if dir_err:
            self._send_json(400, "error", dir_err)
            return
        save_dir, base_name = resolve_chunk_save_path(relative_path, filename, base_dir)
        if save_dir is None:
            self._send_json(400, "error", "非法路径")
            return
        try:
            makedirs(save_dir)
            # 目标名在 finalize_file 内持锁决定：并发上传同名文件时后到者自动拿到 _1 后缀，
            # 不会互相覆盖或报 500。这里只用 base_name 占一个「同目录唯一临时名」。
            data_path = data_file_path(cdir)
            if os.path.isfile(data_path):
                # offset 模式：分片已按 offset 直写进 data，直接 rename 落地 —— 零拷贝
                actual = os.path.getsize(data_path)
                if expect_size and actual != expect_size:
                    raise IOError('暂存文件大小与预期不符 (%d != %d)' % (actual, expect_size))
                final_name, save_path = finalize_file(data_path, save_dir, base_name)
            elif total_chunks == 1:
                # 单分片：无需拼接，直接重命名落地
                one = os.path.join(cdir, '0')
                actual = os.path.getsize(one) if os.path.isfile(one) else -1
                if expect_size and actual != expect_size:
                    raise IOError('分片大小与预期不符 (%d != %d)' % (actual, expect_size))
                final_name, save_path = finalize_file(one, save_dir, base_name)
            else:
                # chunkfile 模式：顺序拼接。先写临时文件，校验大小后再原子改名，
                # 避免中途失败在最终路径留下半截文件。
                tmp_out = temp_sibling(os.path.join(save_dir, base_name))
                try:
                    concat_chunks(cdir, tmp_out, total_chunks)
                    actual = os.path.getsize(tmp_out)
                    if expect_size and actual != expect_size:
                        raise IOError('拼接后大小与预期不符 (%d != %d)' % (actual, expect_size))
                    final_name, save_path = finalize_file(tmp_out, save_dir, base_name)
                except Exception:
                    try:
                        if os.path.exists(tmp_out):
                            os.remove(tmp_out)
                    except (OSError, IOError):
                        pass
                    raise
            shutil.rmtree(cdir, ignore_errors=True)
            _utime_ts.pop(cdir, None)
            if resume_key:
                try:
                    os.remove(os.path.join(RESUME_TMP_DIR, resume_key))
                except (OSError, IOError):
                    pass
            invalidate_caches()
            payload = {"filename": final_name,
                       "message": "文件 '%s' 上传成功！" % html_escape(final_name)}
            payload.update(saved_meta(save_path, base_dir))
            self._send_json(200, "success", payload)
        except Exception as e:
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)  # 合并失败清理残留
            except (OSError, IOError, UnboundLocalError):
                pass
            self._send_json(500, "error", "合并失败: %s" % html_escape(str(e)))

    # ---------- 单片直传（init+upload+complete 三合一） ----------
    def handle_chunk_put(self, params):
        """单片文件免分片直传：一次请求即可完成上传。

        省掉 init / complete 两次往返：对「大量小文件」场景约 3× 提速；
        落盘走 .part + rename，原子且无需合并。
        参数（query）：filename / relative_path? / size? / dir?（绝对路径，需 --allow-client-dir）
        """
        filename = (params.get('filename', [''])[0] or '').strip()
        relative_path = params.get('relative_path', [''])[0] or ''
        client_dir = params.get('dir', [''])[0] or ''
        try:
            size = int(params.get('size', ['0'])[0] or 0)
        except (TypeError, ValueError):
            size = 0
        if not filename:
            self._send_json(400, "error", "缺少 filename")
            return
        length = int(self.headers.get('Content-Length') or 0)
        if length < 0 or length > MAX_CHUNK_SIZE_BYTES:
            self._send_json(400, "error", "文件大小不合法")
            return
        if size and size != length:
            self._send_json(400, "error", "size 与请求体长度不一致")
            return
        if MAX_UPLOAD_BYTES and length > MAX_UPLOAD_BYTES:
            self._send_json(413, "error", "上传文件超过大小限制 (%d MB)" % ARGS.max_upload_mb)
            return
        base_dir, dir_err = resolve_base_dir(client_dir)
        if dir_err:
            self._send_json(400, "error", dir_err)
            return
        save_dir, base_name = resolve_chunk_save_path(relative_path, filename, base_dir)
        if save_dir is None:
            self._send_json(400, "error", "非法路径")
            return
        tmp_path = None
        try:
            makedirs(save_dir)
            # 临时名唯一（并发同名文件各写各的），最终名在 finalize_file 内持锁决定
            tmp_path = temp_sibling(os.path.join(save_dir, base_name))
            written = 0
            with open(tmp_path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(READ_BUFSIZE, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
            if written != length:
                raise IOError('请求体不完整 (%d/%d)' % (written, length))
            final_name, save_path = finalize_file(tmp_path, save_dir, base_name)
            invalidate_caches()
            self._req_bytes = written         # 供访问日志打印服务端实测速率
            payload = {"filename": final_name, "size": written, "received": written,
                       "message": "文件 '%s' 上传成功！" % html_escape(final_name)}
            payload.update(saved_meta(save_path, base_dir))
            self._send_json(200, "success", payload)
        except Exception as e:
            if tmp_path:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except (OSError, IOError):
                    pass
            self._send_json(500, "error", "保存失败: %s" % html_escape(str(e)))

    # ---------- 批量操作 ----------
    def handle_batch_delete(self):
        """批量删除：JSON {paths:[...]} -> {success:[], failed:[{path,error}]}"""
        try:
            data = self._read_json_body()
        except Exception:
            self._send_json(400, "error", "请求体格式错误")
            return
        paths = data.get('paths')
        if not isinstance(paths, list) or not paths:
            self._send_json(400, "error", "paths 不能为空")
            return
        if len(paths) > 1000:
            self._send_json(400, "error", "单次最多删除 1000 个路径")
            return
        success, failed = [], []
        for p in paths:
            if not isinstance(p, string_types):
                failed.append({'path': str(p), 'error': '路径类型不合法'})
                continue
            target = resolve_upload_path(p)
            if not target:
                failed.append({'path': p, 'error': '非法路径'})
            elif not os.path.exists(target):
                failed.append({'path': p, 'error': '文件不存在'})
            else:
                try:
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    else:
                        os.remove(target)
                    success.append(p)
                except (OSError, IOError) as e:
                    failed.append({'path': p, 'error': str(e)})
        if success:
            invalidate_caches()
        self._send_json(200, "success", {"success": success, "failed": failed})

    def handle_batch_download(self, params=None):
        """批量下载：收集 paths 指向的文件（目录递归）打包 zip 流式返回"""
        if params:
            paths = params.get('paths') or params.get('path') or []
        else:
            try:
                data = self._read_json_body(16)
                paths = data.get('paths') or []
            except Exception:
                self._send_json(400, "error", "请求体格式错误")
                return
        if not isinstance(paths, list) or not paths:
            self._send_json(400, "error", "paths 不能为空")
            return
        if len(paths) > 1000:
            self._send_json(400, "error", "单次最多下载 1000 个路径")
            return

        files = []  # (绝对路径, zip内相对路径)
        seen = set()  # paths 去重保序：同一文件/目录只打包一次
        for p in paths:
            target = resolve_upload_path(p)
            if not target or target in seen:
                continue
            seen.add(target)
            if os.path.isfile(target):
                files.append((target, p.replace('\\', '/')))
            elif os.path.isdir(target):
                for root, _, names in os.walk(target):
                    for n in names:
                        # 过滤 .tmp/.part/__done 等内部文件：否则会把未合并的分片、
                        # 断点记录一起打包下载出去。
                        if hidden_entry(n):
                            continue
                        fp = os.path.join(root, n)
                        if fp in seen:
                            continue
                        seen.add(fp)
                        rel = os.path.relpath(fp, UPLOAD_DIR).replace('\\', '/')
                        files.append((fp, rel))
        if not files:
            self._send_json(404, "error", "没有可下载的文件")
            return

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        try:
            with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_STORED) as zf:
                for fp, arc in files:
                    try:
                        zf.write(fp, arc)
                    except (OSError, IOError, RuntimeError):
                        continue
            size = os.path.getsize(tmp.name)
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            zip_name = 'batch_%s.zip' % stamp
            self.send_response(200)
            self.send_header('Content-type', 'application/zip')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % quote(zip_name))
            self.send_header('Content-Length', str(size))
            self.end_headers()
            with open(tmp.name, 'rb') as f:
                shutil.copyfileobj(f, self.wfile, READ_BUFSIZE)
        except CLIENT_GONE:
            pass  # 客户端断开，静默结束
        except Exception as e:
            try:
                self._send_json(500, "error", "打包失败: %s" % html_escape(str(e)))
            except CLIENT_GONE:
                pass
        finally:
            try:
                os.remove(tmp.name)
            except (OSError, IOError):
                pass


def write_pidfile():
    if PIDFILE:
        try:
            makedirs(os.path.dirname(PIDFILE))
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


_httpd = None


def shutdown_server(action='stop'):
    """stop: 退出进程；restart: 重新拉起自身"""
    remove_pidfile()
    if action == 'restart':
        # 在新进程启动后再退出当前进程
        if sys.executable:
            import subprocess
            devnull = open(os.devnull, 'wb')
            popen_kw = {'cwd': os.getcwd(), 'stdout': devnull, 'stderr': devnull}
            if PY2:
                # py2 既无 subprocess.DEVNULL 也无 start_new_session
                if hasattr(os, 'setsid'):
                    popen_kw['preexec_fn'] = os.setsid
            else:
                popen_kw['start_new_session'] = True
            subprocess.Popen([sys.executable] + sys.argv, **popen_kw)
    if _httpd:
        start_daemon(_httpd.shutdown)


def _signal_handler(signum, frame):
    log("\n收到信号 %s，服务器停止。" % signum)
    remove_pidfile()
    if _httpd:
        # shutdown() 必须从 serve_forever() 之外的线程调用，否则会死锁
        start_daemon(_httpd.shutdown)


def main():
    global _httpd
    write_pidfile()
    cleanup_stale_chunks()  # 清理上次运行遗留的过期分片/断点映射

    server_address = (HOST, PORT)
    httpd = ThreadedHTTPServer(server_address, UploadHandler)
    _httpd = httpd

    log("=" * 60)
    log("[上传服务器] 零依赖文件/文件夹上传服务器 v%s 已启动！" % VERSION)
    log("请在浏览器访问: http://<服务器IP>:%d" % PORT)
    log("文件保存至: %s" % UPLOAD_DIR)
    log("页面登录: %s" % ("已开启 (访问页面需先输入访问令牌)" if TOKEN else "未开启 (访问页面无需登录)"))
    log("管理接口: %s" % ("已开启 (--admin-token)" if ADMIN_TOKEN else "未开启"))
    log("分片落盘: %s (单片上限 %dMB)" % (
        'chunkfile 按片文件+拼接' if CHUNK_WRITE_MODE == WRITE_MODE_CHUNKFILE
        else 'offset 稀疏文件直写', ARGS.chunk_size_mb))
    if SOCK_BUF_BYTES:
        log("网络缓冲: 显式 %dMB（收发各一）" % ARGS.sock_buf_mb)
    if PIDFILE:
        log("PID 文件: %s" % PIDFILE)
    log("=" * 60)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        remove_pidfile()
        log("\n[上传服务器] 服务器已停止。")


if __name__ == '__main__':
    main()
