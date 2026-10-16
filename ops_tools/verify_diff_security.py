# -*- coding: utf-8 -*-
# 文件比对工具 v2.0 安全验证：COMPARE_TOKEN 鉴权 + COMPARE_ALLOW_ROOTS 路径白名单
# 用法：先启动带令牌的服务（COMPARE_TOKEN=sectok COMPARE_ALLOW_ROOTS=<root> PORT=18001 python tools/diff_file_dir_jar_excel.py），再 python verify_diff_security.py
import json
import os
import sys
import tempfile
import urllib.request

BASE = "http://127.0.0.1:18001"
PASS = 0
FAIL = 0
LINES = []


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        LINES.append("[PASS] %s %s" % (label, extra))
    else:
        FAIL += 1
        LINES.append("[FAIL] %s %s" % (label, extra))


def call(method, path, body=None, token=""):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Compare-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa
        return -1, "ERR:%s" % e


# ALLOW_ROOTS 指向 tools/data（存在且可控），越权访问用 C:/Windows
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "data")
OUTSIDE = "C:/Windows"

# 1. 无 token 访问 /api/* 应被拒绝
code, text = call("GET", "/api/history")
ok("无 token 访问 /api/history 被拒(401)", code == 401, "code=%d" % code)

# 2. 错误 token 被拒
code, text = call("GET", "/api/history", token="wrong")
ok("错误 token 被拒(401)", code == 401, "code=%d" % code)

# 3. 正确 token 通过
code, text = call("GET", "/api/history", token="sectok")
ok("正确 token 通过(200)", code == 200, "code=%d" % code)

# 4. 首页 / 不受 token 限制
code, text = call("GET", "/")
ok("首页无需 token", code == 200, "code=%d" % code)

# 5. 越权路径被拒（COMPARE_ALLOW_ROOTS 生效）
code, text = call("POST", "/api/compare/dir",
                  {"dir1": OUTSIDE, "dir2": OUTSIDE}, token="sectok")
try:
    j = json.loads(text)
except Exception:
    j = {}
ok("越权路径(根目录外)被拒", code == 200 and (not j.get("success", True))
   and ("不在允许" in j.get("message", "") or "根目录" in j.get("message", "")),
   "code=%d msg=%s" % (code, j.get("message", "")[:40]))

# 6. 白名单内路径放行
code, text = call("POST", "/api/compare/dir",
                  {"dir1": ROOT, "dir2": ROOT}, token="sectok")
try:
    j = json.loads(text)
except Exception:
    j = {}
ok("白名单内路径放行", code == 200 and bool(j.get("success")),
   "code=%d msg=%s" % (code, j.get("message", "")[:40]))

LINES.append("")
LINES.append("==== 安全验证结果 PASS=%d FAIL=%d ====" % (PASS, FAIL))
print("\n".join(LINES))
sys.exit(1 if FAIL else 0)
