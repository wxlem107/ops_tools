#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib —— 运维工具集公共层
==========================

把原先散落在每个脚本里的 Python 2.7.5+ / 3.7.5+ 兼容代码、HTTP 框架、鉴权、
审计、任务、SSE、探针等基础能力集中到一处，新工具只需关注业务本身。

模块一览
--------
| 模块       | 职责                                                       |
|-----------|-----------------------------------------------------------|
| compat    | 跨版本兼容工具：字节/文本、原子替换、磁盘容量、命令执行、线程池 |
| store     | JSON / JSONL 持久化：原子写、读写锁、追加日志、按行读取        |
| auth      | 多用户、角色（admin/ops/viewer）、PBKDF2 口令、会话与授权     |
| apibase   | 统一响应格式、路由表、Request 上下文、ApiError               |
| server    | HttpApp：路由 + 鉴权 + 静态资源 + 模板 + 错误处理 + OpenAPI   |
| sse       | Server-Sent Events 流与进程内事件总线（EventBus）            |
| httpx     | 带连接池的 HTTP 客户端（流式转发、下载、探测）                |
| probe     | 主机画像与实时指标采集（读 /proc，零依赖）                    |
| audit     | 操作审计流水（追加写 + 哈希链防篡改）                         |
| task      | 异步任务中心（进度、日志、取消、历史）                        |
| notify    | 通知渠道（企业微信/钉钉/飞书/Slack/webhook/SMTP）            |
| crypto    | ChaCha20+HMAC 对称加密与凭据保险箱                            |

引入方式（tools/ 或 管理页面/ 下的脚本，与 opslib 同级目录）：:

    import os
    import sys

    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _BASE not in sys.path:
        sys.path.insert(0, _BASE)

    from opslib import compat, server, auth as opsauth, apibase, sse, httpx, probe, audit, task, notify
"""
from __future__ import print_function

import os
import sys

VERSION = '1.0.8'

# 版本历史（公共层改动会影响所有工具，请同步部署 opslib/）：
#   1.0.8  compat.urlunquote / urlquote 修复 **Python 3.5~3.8 专属** 崩溃：
#          老实现无条件 to_bytes() 再调 _urllib.unquote，而 py3 下 _urllib 是
#          urllib.parse，其 unquote() 在 3.9 之前只接受 str，传 bytes 会抛
#          TypeError: Expected str, got bytes —— 于是 **每一个请求（含首页 GET /）**
#          都在 server.dispatch 阶段 500，服务完全不可用；3.9+ 放宽了类型检查
#          故本地/CI 不复现，极具迷惑性。现按 PY2/PY3 分支：py2 走字节串解码，
#          py3 直接把文本交给 unquote，最后统一 to_native 归一。urlquote 同步修正
#          （py3 传 str、py2 传 bytes）。selftest / verify_py2_compat 增加固化用例。
#   1.0.7  一轮代码自查后的公共层修复：
#          - task.submit 的 params 现在会回传给任务函数（此前 bulk-exec 下发空命令、
#            cmdb 采集永远全量）；
#          - auth.user_update 改为「先复制副本、全部校验通过再写回」，避免校验失败时
#            已就地污染缓存对象导致的越权角色被后续落盘持久化；
#          - audit 落盘失败时不再推进 _last_hash（否则哈希链永久断裂）；
#          - httpx 响应体被 max_body 截断时不再把连接放回池（残留数据会污染下次请求）；
#          - compat.is_within 改用 realpath（防符号链接逃逸）；run_cmd 持续排空输出，
#            避免大输出把子进程憋到超时；
#          - sse.stream_events 的闭包变量改用可变容器（py2 无 nonlocal，原实现会
#            UnboundLocalError）；server._responded 每请求重置（keep-alive 第二请求异常
#            时不再回不了 500）。
#   1.0.6  compat.open_text 增加 errors 参数（默认 None，向后兼容）。读日志类文件时
#          传 'replace' 可容错：Windows 上 CPython 的系统异常消息（如
#          ConnectionResetError“远程主机强迫关闭了一个现有的连接。”）以本地编码
#          （GBK）落盘/重定向到日志，直接 utf-8 解码会抛 UnicodeDecodeError。
#   1.0.5  httpx 连接池复用连接时重设套接字超时。原先池里的连接沿用「创建它那次请求」
#          的 timeout：短超时（探测 2s）建的连接被长请求（上传/转发/列表刷新）复用后
#          几秒即读超时，对外表现为莫名 status=0（现场「编辑保存后刷新失败」被它放大）。
#   1.0.4  sse.consume 支持 method/body/content_type：命令放请求体走
#          POST /api/exec/stream，链路上 URI 不再出现 cmd=... —— 避免被安全设备
#          （态势感知/IDS）的「URI 命令执行」特征规则误判成 webshell 攻击。
#   1.0.3  compat.json_bytes 改为「先 json_safe 归一为文本、再序列化」：
#          py2 下 unicode 与含中文的字节串混排时，json.dumps(ensure_ascii=False)
#          会在 ''.join(chunks) 处隐式 ascii 解码并抛 UnicodeDecodeError
#          （发布单详情 500 / 订单落盘失败）；同时禁掉生产代码里的裸 dumps。

__all__ = ['compat', 'store', 'auth', 'apibase', 'server', 'sse', 'httpx', 'probe',
           'audit', 'task', 'notify', 'crypto', 'VERSION', 'bootstrap_path']


def bootstrap_path(anchor_file):
    """把 opslib 所在目录加入 sys.path，返回该目录。

    anchor_file 传调用方脚本的 __file__ 即可（脚本位于 opslib 的同级或下一级目录）。
    """
    base = os.path.dirname(os.path.abspath(anchor_file))
    for _ in range(2):
        if os.path.isdir(os.path.join(base, 'opslib')):
            if base not in sys.path:
                sys.path.insert(0, base)
            return base
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    return base
