#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
文件比对工具 v2.1：文件夹 / 文本 / Jar(条目级内容差异) / Excel / 三向 / 批量 / docx·pptx 结构化
单文件后端（前端页面已拆分到同级 templates/diff_file_dir_jar_excel.html，改完刷新即生效）。

兼容 Python 2.7.5+ 与 Python 3.7.5+，依赖按版本自动选择（见 requirements.txt）：
    Python 3.8+ : flask 3.x   + openpyxl 3.x
    Python 3.7  : flask 2.2.x + openpyxl 3.0.x
    Python 2.7  : flask 1.1.x + openpyxl 2.6.x

相对 v1.0 新增能力：
  - 三向比对（基线 / 版本A / 版本B）：POST /api/compare/three
  - 差异导出：POST /api/export/report（自包含 HTML / Markdown）、POST /api/export/patch（unified diff）
  - .docx / .pptx 结构化内容比对（OOXML 解包，提取段落 / 文本流）
  - 忽略规则可视化配置：GET/POST /api/ignore/rules、POST /api/ignore/test（tools/data/ignore_rules.json）
  - 结果持久化与历史：tools/data/history.jsonl，GET/DELETE /api/history、GET /api/history/<id>
  - 批量目录比对：POST /api/compare/batch（>3 对走 opslib.task.TaskManager 异步 + SSE/轮询）

【用法示例】（本工具无命令行参数，全部配置走环境变量）
    # 1) 最简启动：监听 0.0.0.0:8087，无鉴权、不限可访问根目录（仅限可信内网）
    python diff_file_dir_jar_excel.py

    # 2) 指定端口 + 开启接口令牌（所有 /api/* 需 X-Compare-Token 头或 ?token=）
    PORT=8087 COMPARE_TOKEN=cmp123 python diff_file_dir_jar_excel.py

    # 3) 生产推荐：令牌 + 限定可访问根目录 + 调大上传上限（单行，可直接复制）
    PORT=8087 COMPARE_TOKEN=cmp123 COMPARE_ALLOW_ROOTS=/data,/opt/app COMPARE_MAX_UPLOAD_MB=500 python diff_file_dir_jar_excel.py

    # 4) 排障：临时开启 Flask debug
    FLASK_DEBUG=1 PORT=8087 python diff_file_dir_jar_excel.py

    # 5) 后台常驻（环境变量前置 + nohup）
    PORT=8087 COMPARE_TOKEN=cmp123 COMPARE_ALLOW_ROOTS=/data nohup python -u diff_file_dir_jar_excel.py > /data/diff.log 2>&1 &

    # 6) 依赖安装（按 Python 版本自动选择，见 requirements.txt）
    pip install -r requirements.txt

【全部参数】（均为环境变量；括号内为默认值）
    PORT                   监听端口                     (8087)
    FLASK_DEBUG            1 才开启 Flask debug         (0 = 关)
    COMPARE_TOKEN          接口令牌，设置后 /api/* 需    (空 = 不鉴权)
                           X-Compare-Token 头或 ?token=
    COMPARE_ALLOW_ROOTS    可访问根目录白名单(逗号分隔)  (空 = 不限制)
    COMPARE_MAX_UPLOAD_MB  上传上限(MB)                 (200)
"""
from __future__ import print_function

import difflib
import filecmp
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from xml.etree import ElementTree as ET

try:
    from itertools import zip_longest
except ImportError:                                # Python 2.7
    from itertools import izip_longest as zip_longest

from flask import (Flask, render_template, request, jsonify, Response,
                   stream_with_context, make_response)
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

# ================= 复用 opslib 公共层（路径白名单/任务中心/兼容工具） =================
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from opslib import compat, task  # noqa: E402

# 把与 opslib.compat 同名的工具做成别名，减少重复代码
which = compat.which

PY2 = sys.version_info[0] == 2

if PY2:
    text_type = unicode            # noqa: F821  py2 内置
else:
    text_type = str


def to_text(raw):
    """转成当前版本的原生文本类型"""
    if isinstance(raw, text_type):
        return raw
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return text_type(raw)


def safe_str(value):
    """把任意值转成原生字符串；py2 下对 unicode 直接 str() 会按 ascii 编码而报错"""
    if value is None:
        return ""
    return to_text(value)


def log(msg):
    """跨版本安全输出：py2 终端编码非 UTF-8 时也不会抛 UnicodeEncodeError"""
    text = "%s\n" % (msg,)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        try:
            sys.stdout.write(text.encode("utf-8"))
        except Exception:
            pass
    except Exception:
        pass


def is_within(path, root):
    """判断 path 是否位于 root 目录内（py2.7 无 os.path.commonpath）；用 realpath 解析符号链接"""
    real_path = os.path.realpath(path)
    real_root = os.path.realpath(root)
    if real_path == real_root:
        return True
    return real_path.startswith(real_root.rstrip("\\/") + os.sep)


def merge_dict(base, extra):
    """等价 {**base, **extra}，py2 不支持字典字面量解包"""
    out = dict(base)
    out.update(extra)
    return out


app = Flask(__name__)

# ========================= 配置 =========================
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("COMPARE_MAX_UPLOAD_MB", "200")) * 1024 * 1024
AUTH_TOKEN = os.getenv("COMPARE_TOKEN", "").strip()
ALLOW_ROOTS = [os.path.realpath(p) for p in os.getenv("COMPARE_ALLOW_ROOTS", "").split(",") if p.strip()]

MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024   # zip 炸弹防护：解压后总体积上限
MAX_ZIP_ENTRIES = 50000                          # zip 条目数上限
MAX_ENTRY_READ = 10 * 1024 * 1024                # 渲染内容差异时单条目读取上限
MAX_NESTED_READ = 100 * 1024 * 1024              # 嵌套归档整体读取上限
MAX_TEXT_LINES = 20000                           # 超过则降级为 unified diff
MAX_EXCEL_DIFFS = 1000                           # Excel 差异单元格截断阈值
MAX_DETAILS = 5000                               # 清单行数上限
SESSION_TTL = 30 * 60                            # 比对会话保留时长（秒）
MAX_CHAIN_DEPTH = 4                              # 嵌套归档最大展开深度

DEFAULT_JAR_IGNORES = [r"^META-INF/[^/]+\.(SF|RSA|DSA|EC)$"]  # 签名文件，重打包必变

_TEXT_EXTS = {".xml", ".yml", ".yaml", ".properties", ".json", ".sql", ".txt", ".mf",
              ".factories", ".md", ".csv", ".html", ".htm", ".js", ".css", ".tld",
              ".conf", ".cfg", ".ini", ".sh", ".bat", ".gitignore",
              ".py", ".java", ".log", ".env", ".ps1", ".tsv",
              ".c", ".h", ".cpp", ".go", ".rs", ".ts", ".vue"}

# ========================= 数据落盘目录 =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
compat.makedirs(DATA_DIR)
TASKS_PATH = os.path.join(DATA_DIR, "tasks.json")
IGNORE_PATH = os.path.join(DATA_DIR, "ignore_rules.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

# 异步批量任务中心（bus=None，进度通过轮询 / SSE 端点自行泵取）
TM = task.TaskManager(TASKS_PATH, max_workers=2, bus=None)

# ========================= 忽略规则（可视化配置） =========================
DEFAULT_RULES = [
    {"id": "r_log", "pattern": "*.log", "scope": "all", "comment": u"日志文件", "enabled": True},
    {"id": "r_git", "pattern": ".git/", "scope": "all", "comment": u"git 元数据目录", "enabled": True},
    {"id": "r_sign", "pattern": "META-INF/*.SF|*.DSA|*.RSA", "scope": "jar",
     "comment": u"jar 签名文件（重打包必变）", "enabled": True},
    {"id": "r_pyc", "pattern": "__pycache__/", "scope": "all", "comment": u"Python 缓存目录", "enabled": True},
    {"id": "r_thumb", "pattern": "Thumbs.db", "scope": "all", "comment": u"缩略图缓存", "enabled": True},
    {"id": "r_docprops", "pattern": "docProps/", "scope": "all",
     "comment": u"OOXML 文档属性（含易变时间戳）", "enabled": True},
]


def glob_to_regex(pat):
    """把忽略规则里的 glob 式模式（支持 * ? | 和结尾 / 表示目录）编译成正则。

    语义对齐 .gitignore：单段模式（如 *.log）可匹配任意层级目录下的同名条目；
    含 / 的多段模式（如 META-INF/*.SF）按相对路径精确匹配。
    """
    alts = pat.split("|")
    bodies = []
    for a in alts:
        a = a.strip()
        if not a:
            continue
        is_dir = a.endswith("/")
        if is_dir:
            a = a[:-1]
        segs = a.split("/")
        seg_re = []
        for s in segs:
            buf = []
            i = 0
            L = len(s)
            while i < L:
                c = s[i]
                if c == "*":
                    if i + 1 < L and s[i + 1] == "*":
                        buf.append(".*")
                        i += 2
                        continue
                    buf.append("[^/]*")
                    i += 1
                    continue
                if c == "?":
                    buf.append("[^/]")
                    i += 1
                    continue
                if c in ".+^${}()[]|\\":
                    buf.append("\\" + c)
                    i += 1
                    continue
                buf.append(c)
                i += 1
            seg_re.append("".join(buf))
        body = "/".join(seg_re)
        if is_dir:
            bodies.append("(?:^|/)" + body + "(?:/|$)")
        elif len(segs) == 1:
            # 单段模式：允许出现在任意层级目录里
            bodies.append("(?:^|/)" + body + "(?:/|$)")
        else:
            # 含 / 的多段模式：按相对路径精确匹配
            bodies.append("^" + body + "$")
    if not bodies:
        bodies = ["$.^"]  # 永远不匹配
    return re.compile("(?:%s)" % "|".join(bodies))


def load_ignore_rules():
    """读取忽略规则；文件缺失时落盘内置默认规则并返回。"""
    if not os.path.isfile(IGNORE_PATH):
        save_ignore_rules(DEFAULT_RULES)
        return [dict(r) for r in DEFAULT_RULES]
    try:
        with compat.open_text(IGNORE_PATH, "r") as f:
            data = json.load(f)
        rules = data.get("rules")
        if not isinstance(rules, list) or not rules:
            return [dict(r) for r in DEFAULT_RULES]
        return rules
    except Exception:
        return [dict(r) for r in DEFAULT_RULES]


def save_ignore_rules(rules):
    """原子保存忽略规则到 tools/data/ignore_rules.json。"""
    compat.makedirs(DATA_DIR)
    tmp = IGNORE_PATH + ".tmp"
    try:
        # 走 compat.json_bytes 而不是裸 json.dump(..., ensure_ascii=False)：
        # py2 下「unicode + 含中文的字节串」混在同一结构里会让 dumps 内部
        # ''.join(chunks) 隐式按 ascii 解码并抛 UnicodeDecodeError（详见 compat.json_safe）。
        with compat.open_text(tmp, "w") as f:
            f.write(compat.to_text(compat.json_bytes({"rules": rules}, indent=2)))
        compat.os_replace(tmp, IGNORE_PATH)
        return True
    except Exception:
        return False


def build_ignore_patterns(scope, temp_list, use_saved):
    """根据 scope 与临时规则构造已编译的正则列表 [(regex, label), ...]。"""
    out = []
    if use_saved:
        for r in load_ignore_rules():
            if not r.get("enabled"):
                continue
            if r.get("scope", "all") not in ("all", scope):
                continue
            try:
                rx = glob_to_regex(r["pattern"])
            except Exception:
                continue
            out.append((rx, r["pattern"]))
    for p in temp_list or []:
        p = p.strip()
        if not p:
            continue
        try:
            rx = glob_to_regex(p)
        except Exception:
            continue
        out.append((rx, p))
    return out


def match_ignore(patterns, rel_path):
    """返回命中的规则 pattern 字符串，未命中返回 None。"""
    for rx, label in patterns:
        if rx.search(rel_path):
            return label
    return None


def split_rules(text):
    """把逗号 / 换行分隔的规则文本拆成列表。"""
    if not text:
        return []
    out = []
    for part in re.split(r"[\n,]+", safe_str(text)):
        part = part.strip()
        if part:
            out.append(part)
    return out


# ========================= 通用工具 =========================
def fmt_size(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("{0:.0f} {1}".format(n, unit) if unit == "B"
                    else "{0:.1f} {1}".format(n, unit))
        n /= 1024.0


def file_md5(path, chunk_size=1024 * 1024):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_text_lines(path):
    """读文本，返回 (行列表, 编码名)。优先 UTF-8，其次 GB18030，最后强制替换。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc).splitlines(), enc
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace").splitlines(), "utf-8(replace)"


def _decode_bytes(b):
    for enc in ("utf-8", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="replace")


def esc_html(s):
    return (to_text(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _intra_spans(a, b):
    """行内字符级高亮：返回 (左html, 右html)，差异片段包 <span class="intra">。"""
    if len(a) > 2000 or len(b) > 2000:      # 超长行不做字符级比对
        return esc_html(a), esc_html(b)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out_a, out_b = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out_a.append(esc_html(a[i1:i2]))
            out_b.append(esc_html(b[j1:j2]))
        else:
            if i2 > i1:
                out_a.append('<span class="intra">%s</span>' % esc_html(a[i1:i2]))
            if j2 > j1:
                out_b.append('<span class="intra">%s</span>' % esc_html(b[j1:j2]))
    return "".join(out_a), "".join(out_b)


def _side_by_side_html(lines1, lines2, desc1=u"左", desc2=u"右", context=3):
    """自绘左右对照表：
    d-chg  改动行（红底）+ 行内 intra 高亮具体改动字符
    d-del  仅左侧（橙底）   d-add  仅右侧（绿底）
    d-skip 折叠的相同段     d-same 上下文行
    """
    sm = difflib.SequenceMatcher(None, lines1, lines2, autojunk=False)
    ops = sm.get_opcodes()
    rows = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            seg = i2 - i1
            first, last = (idx == 0), (idx == len(ops) - 1)
            if first and last:
                head, mid, tail = seg, 0, 0
            elif first:
                head, mid, tail = 0, max(0, seg - context), min(context, seg)
            elif last:
                head, mid, tail = min(context, seg), max(0, seg - context), 0
            elif seg <= context * 2:
                head, mid, tail = seg, 0, 0
            else:
                head, mid, tail = context, seg - 2 * context, context
            for k in range(head):
                rows.append({"t": "same", "i": i1 + k, "j": j1 + k})
            if mid:
                rows.append({"t": "skip", "count": mid})
            for k in range(tail):
                rows.append({"t": "same", "i": i2 - tail + k, "j": j2 - tail + k})
        elif tag == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                has_l, has_r = i1 + k < i2, j1 + k < j2
                rows.append({"t": "chg" if has_l and has_r else ("del" if has_l else "add"),
                             "i": i1 + k if has_l else None,
                             "j": j1 + k if has_r else None})
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append({"t": "del", "i": k, "j": None})
        else:  # insert
            for k in range(j1, j2):
                rows.append({"t": "add", "i": None, "j": k})

    out = ['<table class="sbs"><thead><tr>',
           '<th class="sbs-h" colspan="2">%s</th>' % esc_html(desc1),
           '<th class="sbs-h" colspan="2">%s</th>' % esc_html(desc2),
           '</tr></thead><tbody>']
    for r in rows:
        if r["t"] == "skip":
            out.append(u'<tr class="d-skip"><td colspan="4">… 折叠 %d 行相同内容 …</td></tr>'
                       % r["count"])
            continue
        la = lines1[r["i"]] if r["i"] is not None else None
        lb = lines2[r["j"]] if r["j"] is not None else None
        ln_l = str(r["i"] + 1) if r["i"] is not None else ""
        ln_r = str(r["j"] + 1) if r["j"] is not None else ""
        if r["t"] == "same":
            html_l = html_r = esc_html(la or "")
            cls = "d-same"
        elif r["t"] == "chg":
            html_l, html_r = _intra_spans(la, lb)
            cls = "d-chg"
        elif r["t"] == "del":
            html_l, html_r, cls = esc_html(la), "", "d-del"
        else:
            html_l, html_r, cls = "", esc_html(lb), "d-add"
        out.append('<tr class="%s"><td class="ln">%s</td>'
                   '<td class="code left">%s</td>'
                   '<td class="ln">%s</td>'
                   '<td class="code right">%s</td></tr>'
                   % (cls, ln_l, html_l, ln_r, html_r))
    out.append("</tbody></table>")
    return "".join(out)


def make_text_diff(lines1, lines2, desc1=u"文件1", desc2=u"文件2"):
    """生成文本差异：常规规模返回自绘左右对照 HTML（行级着色），过大时降级 unified 文本。"""
    if max(len(lines1), len(lines2)) > MAX_TEXT_LINES:
        diff_lines = list(difflib.unified_diff(lines1, lines2, fromfile=desc1, tofile=desc2, lineterm=""))
        text = "\n".join(diff_lines) or u"（行数超过阈值但无逐行差异，可能为换行符差异）"
        if len(text) > 500000:
            text = text[:500000] + u"\n……（内容过长已截断）"
        return {"mode": "text", "html": None, "text": text, "truncated": True}
    html = _side_by_side_html(lines1, lines2, desc1, desc2)
    return {"mode": "html", "html": html, "text": None, "truncated": False}


def get_all_files(base_dir):
    result = {}
    for root, _, files in os.walk(base_dir):
        for name in files:
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, base_dir).replace("\\", "/")
            result[rel_path] = full_path
    return result


def compare_two_files_content(file1, file2):
    if os.path.getsize(file1) != os.path.getsize(file2):
        return "different"
    return "same" if filecmp.cmp(file1, file2, shallow=False) else "different"


# ========================= 安全：鉴权 / 路径白名单 =========================
@app.before_request
def _auth():
    if not AUTH_TOKEN or not request.path.startswith("/api/"):
        return None
    token = request.headers.get("X-Compare-Token") or request.args.get("token")
    if token != AUTH_TOKEN:
        return jsonify({"success": False, "message": u"鉴权失败：缺少或错误的 token"}), 401


def check_pair_paths(left, right, tmp_dir):
    for p in (left, right):
        if tmp_dir and p.startswith(tmp_dir + os.sep):
            continue  # 上传文件，已清洗文件名，跳过白名单
        if not os.path.exists(p):
            return u"路径不存在：%s" % p
        if ALLOW_ROOTS:
            real = os.path.realpath(p)
            ok = False
            for root in ALLOW_ROOTS:
                if is_within(real, root):
                    ok = True
                    break
            if not ok:
                return u"路径不在允许访问的根目录内：%s" % p
    return None


# ========================= 输入收集 / 会话管理 =========================
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


def _upload_save_path(tmp_dir, fixed, original_filename):
    """上传文件基名固定（防 ../ 注入），但保留原始扩展名：
    openpyxl 等库按扩展名识别格式，.bin 后缀会导致打开失败。"""
    ext = os.path.splitext(original_filename or "")[1].lower()
    if not _EXT_RE.match(ext):          # 非法/空扩展名兜底
        ext = ".bin"
    return os.path.join(tmp_dir, fixed + ext)


def collect_pair(json_keys, form_keys, file_keys):
    """统一收集左右两侧输入。返回 (left, right, tmp_dir|None, err)。
    支持：JSON 路径 / 表单路径 / multipart 上传（上传优先）。"""
    left = right = None
    tmp_dir = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        left = safe_str(data.get(json_keys[0])).strip()
        right = safe_str(data.get(json_keys[1])).strip()
    else:
        f1 = request.files.get(file_keys[0])
        f2 = request.files.get(file_keys[1])
        if f1 and f1.filename:
            tmp_dir = tempfile.mkdtemp(prefix="cmp_")
            left = _upload_save_path(tmp_dir, "left", f1.filename)
            f1.save(left)
        if f2 and f2.filename:
            if not tmp_dir:
                tmp_dir = tempfile.mkdtemp(prefix="cmp_")
            right = _upload_save_path(tmp_dir, "right", f2.filename)
            f2.save(right)
        left = left or safe_str(request.form.get(form_keys[0])).strip()
        right = right or safe_str(request.form.get(form_keys[1])).strip()
    if not left or not right:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None, None, u"请提供两个有效的输入（路径或上传文件）"
    return left, right, tmp_dir, None


def collect_triple():
    """收集三向比对的 基线/左/右 三个输入（路径或上传）。返回 (base,left,right,tmp_dir,err)。"""
    base = left = right = None
    tmp_dir = None
    if request.is_json:
        d = request.get_json(silent=True) or {}
        base = safe_str(d.get("base_path") or d.get("base") or d.get("base_path")).strip()
        left = safe_str(d.get("left_path") or d.get("left")).strip()
        right = safe_str(d.get("right_path") or d.get("right")).strip()
    else:
        fb = request.files.get("base")
        fl = request.files.get("left")
        fr = request.files.get("right")
        if fb and fb.filename:
            tmp_dir = tempfile.mkdtemp(prefix="cmp3_")
            base = _upload_save_path(tmp_dir, "base", fb.filename)
            fb.save(base)
        if fl and fl.filename:
            if not tmp_dir:
                tmp_dir = tempfile.mkdtemp(prefix="cmp3_")
            left = _upload_save_path(tmp_dir, "left", fl.filename)
            fl.save(left)
        if fr and fr.filename:
            if not tmp_dir:
                tmp_dir = tempfile.mkdtemp(prefix="cmp3_")
            right = _upload_save_path(tmp_dir, "right", fr.filename)
            fr.save(right)
        base = base or safe_str(request.form.get("base_path") or request.form.get("base")).strip()
        left = left or safe_str(request.form.get("left_path") or request.form.get("left")).strip()
        right = right or safe_str(request.form.get("right_path") or request.form.get("right")).strip()
    if not (base and left and right):
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None, None, None, u"请提供基线/左/右三个输入（路径或上传）"
    return base, left, right, tmp_dir, None


def check_triple_paths(base, left, right, tmp_dir):
    for p in (base, left, right):
        if tmp_dir and p.startswith(tmp_dir + os.sep):
            continue
        if not os.path.exists(p):
            return u"路径不存在：%s" % p
        if ALLOW_ROOTS:
            real = os.path.realpath(p)
            ok = False
            for root in ALLOW_ROOTS:
                if is_within(real, root):
                    ok = True
                    break
            if not ok:
                return u"路径不在允许访问的根目录内：%s" % p
    return None


_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _cleanup_sessions():
    now = time.time()
    with _SESSIONS_LOCK:
        expired = [sid for sid, v in _SESSIONS.items() if now - v["created"] > SESSION_TTL]
        for sid in expired:
            info = _SESSIONS.pop(sid)
            if info.get("tmp_dir"):
                shutil.rmtree(info["tmp_dir"], ignore_errors=True)


def new_session(left, right, tmp_dir=None, **extra):
    _cleanup_sessions()
    sid = uuid.uuid4().hex
    info = {"left": left, "right": right, "tmp_dir": tmp_dir, "created": time.time()}
    info.update(extra)
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = info
    return sid


def get_session(sid):
    with _SESSIONS_LOCK:
        return _SESSIONS.get(sid)


def set_session_result(sid, result):
    """把完整比对结果存入会话（用于导出报告 / 补丁）。"""
    with _SESSIONS_LOCK:
        info = _SESSIONS.get(sid)
        if info is not None:
            info["result"] = result


def _input(key, default=None):
    if request.is_json:
        return (request.get_json(silent=True) or {}).get(key, default)
    return request.form.get(key, default)


def _bool_input(key, default=False):
    v = _input(key, default)
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return safe_str(v).strip().lower() in ("1", "true", "on", "yes")


def req_user():
    return getattr(request, "remote_addr", "") or ""


# ========================= 文件类型自动识别 =========================
_OOXML_SIGN = {"word/": "docx", "xl/": "xlsx", "ppt/": "pptx"}


def detect_kind(path):
    """返回 dir / xlsx / docx / pptx / jar / zip / image / text / binary"""
    if os.path.isdir(path):
        return "dir"
    with open(path, "rb") as f:
        head = f.read(8)
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return "binary"
        for sign, kind in _OOXML_SIGN.items():      # OOXML 也是 zip，必须先判
            if any(n.startswith(sign) for n in names):
                return kind
        if ("META-INF/MANIFEST.MF" in names or any(n.startswith(("BOOT-INF/", "WEB-INF/")) for n in names)
                or os.path.splitext(path)[1].lower() in (".jar", ".war", ".ear", ".apk")):
            return "jar"
        return "zip"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")):
        return "image"
    with open(path, "rb") as f:
        sample = f.read(8192)
    if not sample:
        return "text"
    if b"\x00" in sample:
        return "binary"
    # 关键修复：能按 UTF-8/GB18030 完整解码即判定为文本。
    decoded = False
    for enc in ("utf-8", "gb18030"):
        try:
            sample.decode(enc)
            decoded = True
            break
        except UnicodeDecodeError:
            continue
    if decoded:
        # 仅当控制字符（排除 \t \n \r）占比异常高时才判 binary，防可解码的二进制流误判
        ctrl = sum(1 for b in sample if b < 32 and b not in (9, 10, 13))
        return "binary" if ctrl > max(8, len(sample) // 100) else "text"
    # 解码失败：已知文本后缀兜底按文本处理（读取时用 replace 解码，仍可出差异）
    if os.path.splitext(path)[1].lower() in _TEXT_EXTS:
        return "text"
    return "binary"


def guess_entry_kind(name):
    lower = name.lower()
    if lower.endswith((".jar", ".war", ".zip", ".ear", ".apk")):
        return "nested-archive"
    if lower.endswith(".class"):
        return "class"
    if os.path.splitext(lower)[1] in _TEXT_EXTS:
        return "text"
    return "binary"


# ========================= 1. 文件夹递归比对 =========================
def compare_dirs(dir1, dir2, compare_content=True, only_diff=False, patterns=None):
    if not os.path.isdir(dir1):
        return {"success": False, "message": u"目录1不存在：%s" % dir1}
    if not os.path.isdir(dir2):
        return {"success": False, "message": u"目录2不存在：%s" % dir2}
    files1 = get_all_files(dir1)
    files2 = get_all_files(dir2)
    all_paths = sorted(set(files1) | set(files2))
    summary = {"same": 0, "different": 0, "only_in_left": 0, "only_in_right": 0, "ignored": 0}
    details = []
    ignored_list = []
    for rel_path in all_paths:
        if patterns and match_ignore(patterns, rel_path):
            summary["ignored"] += 1
            ignored_list.append(rel_path)
            continue
        lp, rp = files1.get(rel_path), files2.get(rel_path)
        item = {"path": rel_path, "status": "", "left_size": None, "right_size": None,
                "left_md5": None, "right_md5": None}
        if lp and not rp:
            item.update(status="only_in_left", left_size=os.path.getsize(lp), left_md5=file_md5(lp))
            summary["only_in_left"] += 1
        elif rp and not lp:
            item.update(status="only_in_right", right_size=os.path.getsize(rp), right_md5=file_md5(rp))
            summary["only_in_right"] += 1
        else:
            item["left_size"] = os.path.getsize(lp)
            item["right_size"] = os.path.getsize(rp)
            if compare_content:
                status = compare_two_files_content(lp, rp)
            else:
                status = "same" if item["left_size"] == item["right_size"] else "different"
            item["status"] = status
            summary[status] += 1
            if status == "different":
                item["left_md5"] = file_md5(lp)
                item["right_md5"] = file_md5(rp)
        details.append(item)
    if only_diff:
        details = [d for d in details if d["status"] != "same"]
    truncated = False
    if len(details) > MAX_DETAILS:
        details = details[:MAX_DETAILS]
        truncated = True
    return {"success": True, "summary": summary, "details": details,
            "ignored_list": ignored_list, "truncated": truncated}


# ========================= 2. 文本文件比对 =========================
def compare_text_files(file1, file2):
    if not os.path.isfile(file1):
        return {"success": False, "message": u"文件1不存在：%s" % file1}
    if not os.path.isfile(file2):
        return {"success": False, "message": u"文件2不存在：%s" % file2}
    try:
        lines1, enc1 = read_text_lines(file1)
        lines2, enc2 = read_text_lines(file2)
    except (IOError, OSError) as e:
        return {"success": False, "message": u"读取文件失败：%s" % to_text(e)}
    same = lines1 == lines2
    result = {"success": True, "same": same, "encodings": [enc1, enc2],
              "html": None, "text": None, "truncated": False}
    if not same:
        d = make_text_diff(lines1, lines2)
        result.update(html=d["html"], text=d["text"], truncated=d["truncated"])
    return result


# ========================= 3. 二进制摘要比对 =========================
def compare_binary_files(f1, f2):
    s1, s2 = os.path.getsize(f1), os.path.getsize(f2)
    m1, m2 = file_md5(f1), file_md5(f2)
    return {"success": True, "same": s1 == s2 and m1 == m2,
            "left": {"size": s1, "md5": m1}, "right": {"size": s2, "md5": m2}}


# ========================= 4. Jar / Zip 条目级比对 =========================
def open_zip_safe(path):
    if not zipfile.is_zipfile(path):
        raise ValueError(u"不是有效的 jar/zip 文件：%s" % to_text(path))
    z = zipfile.ZipFile(path)
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_ENTRIES:
        z.close()
        raise ValueError(u"条目数超过上限 %d" % MAX_ZIP_ENTRIES)
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_SIZE:
        z.close()
        raise ValueError(u"解压后总体积超过安全上限（疑似 zip 炸弹）")
    return z, {i.filename: i for i in infos}


def jar_ignore_patterns(ignore_sign=True, extra=None):
    pats = list(DEFAULT_JAR_IGNORES) if ignore_sign else []
    for p in (extra or []):
        p = p.strip()
        if p:
            pats.append(p)
    compiled = []
    for p in pats:
        try:
            compiled.append(re.compile(p))
        except re.error as e:
            raise ValueError(u"忽略规则正则非法：%s（%s）" % (p, to_text(e)))
    return compiled


def _compare_zip_infos(info1, info2, patterns, z1=None, z2=None):
    """核心比对循环：CRC32 + 大小只作快速筛选，命中后一律用 SHA-1 复核内容。

    仅凭 CRC32+size 判定「相同」存在碰撞风险（CRC32 只有 32 位，且可被人为构造），
    会把内容不同的条目静默判为「相同」—— 这是比对工具最危险的一类误判。因此在
    CRC/大小一致时再流式取 SHA-1 比对；两者相等才认定为 same。
    """
    summary = {"same": 0, "different": 0, "only_in_left": 0, "only_in_right": 0, "ignored": 0}
    details = []
    ignored_list = []
    for name in sorted(set(info1) | set(info2)):
        if any(p.search(name) for p in patterns):
            summary["ignored"] += 1
            ignored_list.append(name)
            continue
        a, b = info1.get(name), info2.get(name)
        if a and not b:
            summary["only_in_left"] += 1
            details.append({"path": name, "status": "only_in_left",
                            "left_size": a.file_size, "entry_kind": guess_entry_kind(name)})
        elif b and not a:
            summary["only_in_right"] += 1
            details.append({"path": name, "status": "only_in_right",
                            "right_size": b.file_size, "entry_kind": guess_entry_kind(name)})
        elif a.CRC == b.CRC and a.file_size == b.file_size:
            if z1 is not None and z2 is not None and _entry_sha1(z1, name) != _entry_sha1(z2, name):
                summary["different"] += 1
                details.append({"path": name, "status": "different",
                                "left_size": a.file_size, "right_size": b.file_size,
                                "entry_kind": guess_entry_kind(name)})
            else:
                summary["same"] += 1
        else:
            summary["different"] += 1
            details.append({"path": name, "status": "different",
                            "left_size": a.file_size, "right_size": b.file_size,
                            "entry_kind": guess_entry_kind(name)})
    truncated = False
    if len(details) > MAX_DETAILS:
        details = details[:MAX_DETAILS]
        truncated = True
    return summary, details, truncated, ignored_list


def compare_jar_entries(jar1, jar2, ignore_sign=True, extra_ignores=None, patterns=None):
    try:
        base_pats = jar_ignore_patterns(ignore_sign, extra_ignores)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if patterns:
        base_pats = base_pats + [rx for rx, _ in patterns]
    try:
        z1, info1 = open_zip_safe(jar1)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    try:
        z2, info2 = open_zip_safe(jar2)
    except ValueError as e:
        z1.close()
        return {"success": False, "message": str(e)}
    try:
        summary, details, truncated, ignored_list = _compare_zip_infos(info1, info2, base_pats, z1, z2)
        return {"success": True, "summary": summary, "details": details,
                "ignored_list": ignored_list, "truncated": truncated}
    finally:
        z1.close()
        z2.close()


def _get_info(z, name):
    try:
        return z.getinfo(name)
    except KeyError:
        return None


def resolve_zip_chain(zip_path, chain):
    """按 'a.jar!lib/b.jar!x.yml' 逐层定位。返回 (最内层 ZipFile, 目标条目名)；
    中途缺失返回 (None, None)。"""
    try:
        z, _ = open_zip_safe(zip_path)
    except ValueError:
        return None, None
    for name in chain[:-1]:
        info = _get_info(z, name)
        if info is None:
            z.close()
            return None, None
        if info.file_size > MAX_NESTED_READ:
            z.close()
            raise ValueError(u"嵌套归档过大，不支持展开：%s" % to_text(name))
        data = z.read(name)
        z.close()
        z = zipfile.ZipFile(io.BytesIO(data))
    return z, chain[-1]


def _entry_sha1(z, name):
    h = hashlib.sha1()
    with z.open(name) as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _file_sha1(path, chunk_size=1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _zip_sigs(path, names):
    """返回 {条目名: SHA-1}，仅对 names 里的条目计算（用于三向比对等值判定）。

    用 SHA-1 而非 CRC32：三向比对同样不能靠 32 位 CRC 判「左右同值」，
    否则碰撞会把「两边其实不同」误判成「无冲突」。
    """
    sigs = {}
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return sigs
    try:
        for n in names:
            if n in z.NameToInfo:
                try:
                    sigs[n] = _entry_sha1(z, n)
                except Exception:
                    pass
    finally:
        try:
            z.close()
        except Exception:
            pass
    return sigs


def _sheet_signatures(path):
    """返回 {sheet 名: 内容签名}，用于 Excel 三向比对判断左右 sheet 是否同值。"""
    sigs = {}
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception:
        return sigs
    try:
        for ws in wb.worksheets:
            h = hashlib.sha1()
            for row in ws.iter_rows(values_only=True):
                h.update(repr(row).encode("utf-8", "replace"))
                h.update(b"\n")
            sigs[ws.title] = h.hexdigest()
    except Exception:
        return sigs
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return sigs


def _binary_summary_rows(z1, name, i1, z2, i2):
    rows = [[u"左侧大小", fmt_size(i1.file_size)], [u"右侧大小", fmt_size(i2.file_size)],
            [u"左侧 CRC32", "%08x" % i1.CRC], [u"右侧 CRC32", "%08x" % i2.CRC]]
    if i1.file_size <= MAX_ENTRY_READ:
        rows.append([u"左侧 SHA-1", _entry_sha1(z1, name)])
    if i2.file_size <= MAX_ENTRY_READ:
        rows.append([u"右侧 SHA-1", _entry_sha1(z2, name)])
    return rows


def _javap_lines(class_bytes):
    """有 JDK 时反汇编 .class；否则返回 None。"""
    javap = which("javap")
    if not javap:
        return None
    tmp = tempfile.mkdtemp(prefix="cls_")
    try:
        p = os.path.join(tmp, "A.class")
        with open(p, "wb") as f:
            f.write(class_bytes)
        proc = subprocess.Popen([javap, "-p", "-c", p],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate()
        if proc.returncode != 0:
            return None
        return to_text(out).splitlines()
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ========================= 5. Excel 比对 =========================
def compare_excel_files(file1, file2, data_only=True):
    for p in (file1, file2):
        if os.path.splitext(p)[1].lower() == ".xls":
            return {"success": False,
                    "message": u"不支持老格式 .xls（openpyxl 仅支持 .xlsx/.xlsm/.xltx/.xltm），"
                               u"请先在 Excel 中另存为 .xlsx 或 .xlsm 后再比对"}
    if not os.path.isfile(file1):
        return {"success": False, "message": u"Excel 文件1不存在：%s" % file1}
    if not os.path.isfile(file2):
        return {"success": False, "message": u"Excel 文件2不存在：%s" % file2}
    try:
        wb1 = load_workbook(file1, data_only=data_only, read_only=True)
        wb2 = load_workbook(file2, data_only=data_only, read_only=True)
    except Exception as e:
        return {"success": False, "message": u"Excel 打开失败：%s" % to_text(e)}
    try:
        sheets1, sheets2 = wb1.sheetnames, wb2.sheetnames
        all_sheets = list(dict.fromkeys(sheets1 + [s for s in sheets2 if s not in sheets1]))
        summary = {"same_sheets": 0, "different_sheets": 0,
                   "only_in_left": 0, "only_in_right": 0, "diff_cell_count": 0}
        result_sheets = []
        truncated = False
        for sheet_name in all_sheets:
            if truncated:
                break
            if sheet_name not in sheets1:
                summary["only_in_right"] += 1
                result_sheets.append({"sheet_name": sheet_name, "status": "only_in_right", "diffs": []})
                continue
            if sheet_name not in sheets2:
                summary["only_in_left"] += 1
                result_sheets.append({"sheet_name": sheet_name, "status": "only_in_left", "diffs": []})
                continue
            ws1, ws2 = wb1[sheet_name], wb2[sheet_name]
            diffs = []
            for r, (row1, row2) in enumerate(zip_longest(
                    ws1.iter_rows(values_only=True), ws2.iter_rows(values_only=True)), start=1):
                row1, row2 = row1 or (), row2 or ()
                for c in range(max(len(row1), len(row2))):
                    v1 = row1[c] if c < len(row1) else None
                    v2 = row2[c] if c < len(row2) else None
                    if v1 != v2:
                        diffs.append({"cell": "%s%d" % (get_column_letter(c + 1), r),
                                      "left": safe_str(v1),
                                      "right": safe_str(v2)})
                        if len(diffs) >= MAX_EXCEL_DIFFS:
                            truncated = True
                            break
                if truncated:
                    break
            if diffs:
                summary["different_sheets"] += 1
                summary["diff_cell_count"] += len(diffs)
                result_sheets.append({"sheet_name": sheet_name, "status": "different", "diffs": diffs})
            else:
                summary["same_sheets"] += 1
                result_sheets.append({"sheet_name": sheet_name, "status": "same", "diffs": []})
        return {"success": True, "summary": summary, "sheets": result_sheets,
                "truncated": truncated, "data_only": data_only}
    finally:
        wb1.close()
        wb2.close()


# ========================= 6. .docx / .pptx 结构化比对 =========================
def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _docx_parts(zf):
    """从 word/document.xml 提取每个 <w:p> 段落的文本。"""
    data = zf.read("word/document.xml")
    root = ET.fromstring(data)
    parts = []
    idx = 0
    for p in root.iter():
        if _local(p.tag) == "p":
            texts = []
            for t in p.iter():
                if _local(t.tag) == "t":
                    texts.append(t.text or "")
            idx += 1
            parts.append({"id": u"段落%d" % idx, "text": "".join(texts)})
    return parts


def _pptx_parts(zf):
    """从 ppt/slides/slideN.xml 提取每张幻灯片的段落文本。"""
    names = zf.namelist()
    slide_files = sorted([n for n in names
                          if re.match(r"ppt/slides/slide\d+\.xml$", n)])
    parts = []
    for i, sf in enumerate(slide_files, 1):
        data = zf.read(sf)
        root = ET.fromstring(data)
        paras = []
        for p in root.iter():
            if _local(p.tag) == "p":
                texts = []
                for t in p.iter():
                    if _local(t.tag) == "t":
                        texts.append(t.text or "")
                paras.append("".join(texts))
        parts.append({"id": u"幻灯片%d" % i, "text": "\n".join(paras)})
    return parts


def extract_ooxml(path):
    """解开 OOXML，返回 {'kind','parts'} 或 {'error'}。忽略 docProps 等易变部分。"""
    if not zipfile.is_zipfile(path):
        return {"error": u"不是有效的 OOXML（zip）文件"}
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return {"error": u"zip 结构损坏"}
    try:
        names = zf.namelist()
        if any(n.startswith("word/") for n in names):
            return {"kind": "docx", "parts": _docx_parts(zf)}
        if any(n.startswith("ppt/slides/slide") for n in names):
            return {"kind": "pptx", "parts": _pptx_parts(zf)}
        return {"error": u"不是 Word/PPT 文档"}
    except ET.ParseError:
        return {"error": u"XML 解析失败（可能加密或损坏）"}
    except Exception as e:
        return {"error": u"解析失败：%s" % to_text(e)}
    finally:
        try:
            zf.close()
        except Exception:
            pass


def _paragraph_changes(parts1, parts2):
    """段落级差异：返回 [{type, id, left, right}]。"""
    seq1 = [p["text"] for p in parts1]
    seq2 = [p["text"] for p in parts2]
    sm = difflib.SequenceMatcher(None, seq1, seq2, autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            for x in range(max(i2 - i1, j2 - j1)):
                l = parts1[i1 + x]["text"] if i1 + x < i2 else None
                r = parts2[j1 + x]["text"] if j1 + x < j2 else None
                changes.append({"type": "changed", "id": "", "left": l, "right": r})
        elif tag == "delete":
            for x in range(i1, i2):
                changes.append({"type": "removed", "id": parts1[x]["id"],
                                "left": parts1[x]["text"], "right": None})
        else:  # insert
            for x in range(j1, j2):
                changes.append({"type": "added", "id": parts2[x]["id"],
                                "left": None, "right": parts2[x]["text"]})
    return changes


def compare_ooxml(left, right):
    """docx/pptx 结构化比对：提取段落文本流后做行级（段落级）比对。"""
    r1 = extract_ooxml(left)
    if r1.get("error"):
        rb = compare_binary_files(left, right)
        rb["note"] = u"结构化解析失败，已降级为二进制摘要：%s" % r1["error"]
        return rb
    r2 = extract_ooxml(right)
    if r2.get("error"):
        rb = compare_binary_files(left, right)
        rb["note"] = u"结构化解析失败，已降级为二进制摘要：%s" % r2["error"]
        return rb
    parts1, parts2 = r1["parts"], r2["parts"]
    lines1 = [p["text"] for p in parts1]
    lines2 = [p["text"] for p in parts2]
    diff = make_text_diff(lines1, lines2, u"左", u"右")
    changes = _paragraph_changes(parts1, parts2)
    summary = {"added": 0, "removed": 0, "changed": 0}
    for c in changes:
        summary[{"added": "added", "removed": "removed", "changed": "changed"}[c["type"]]] += 1
    return {"success": True, "same": not changes, "kind": r1["kind"],
            "html": diff["html"], "text": diff["text"], "truncated": diff["truncated"],
            "changes": changes, "summary": summary}


# ========================= 7. 三向比对（基线 / A / B） =========================
_DIR_TXT = {"same": u"无", "modified": u"修改", "added": u"新增", "deleted": u"删除", "absent": u"—"}


def _status_map_dir(res):
    """把 dir/jar 比对结果映射为 path -> {same,modified,added,deleted}。"""
    m = {}
    for d in res.get("details") or []:
        st = d.get("status")
        if st == "only_in_left":
            m[d["path"]] = "deleted"
        elif st == "only_in_right":
            m[d["path"]] = "added"
        elif st == "different":
            m[d["path"]] = "modified"
        elif st == "same":
            m[d["path"]] = "same"
    return m


def _status_map_sheet(res):
    """把 Excel 三向比对结果映射为 sheet -> {same,modified,added,deleted}。"""
    m = {}
    for sh in res.get("sheets") or []:
        st = sh.get("status")
        if st == "only_in_left":
            m[sh["sheet_name"]] = "deleted"
        elif st == "only_in_right":
            m[sh["sheet_name"]] = "added"
        elif st == "different":
            m[sh["sheet_name"]] = "modified"
        elif st == "same":
            m[sh["sheet_name"]] = "same"
    return m


def _classify(a, b):
    if a == "same" and b == "same":
        return "skip"
    if a == "same":
        return "b_only"
    if b == "same":
        return "a_only"
    if a == "absent":
        return "b_only"
    if b == "absent":
        return "a_only"
    if a == "deleted" and b == "deleted":
        return "base_only"
    return "common"


def _aggregate_three(kind, a_map, b_map, get_equal):
    keys = sorted(set(a_map) | set(b_map))
    summary = {"base_only": 0, "a_only": 0, "b_only": 0,
               "common": 0, "common_conflict": 0, "same": 0}
    items = []
    for k in keys:
        a = a_map.get(k, "absent")
        b = b_map.get(k, "absent")
        if a == "same" and b == "same":
            summary["same"] += 1
            continue
        cat = _classify(a, b)
        if cat == "skip":
            continue
        a_dir = _DIR_TXT[a]
        b_dir = _DIR_TXT[b]
        conflict = False
        if cat == "common":
            eq = get_equal(k)
            conflict = (eq is False)
            summary["common"] += 1
            if conflict:
                summary["common_conflict"] += 1
        elif cat == "base_only":
            summary["base_only"] += 1
        elif cat == "a_only":
            summary["a_only"] += 1
        else:
            summary["b_only"] += 1
        items.append({"path": k, "category": cat, "a_dir": a_dir, "b_dir": b_dir,
                      "a_status": a, "b_status": b, "conflict": conflict})
    truncated = False
    if len(items) > MAX_DETAILS:
        items = items[:MAX_DETAILS]
        truncated = True
    return {"summary": summary, "items": items, "truncated": truncated}


def compare_three_way(base, left, right, patterns=None):
    """先分别做「基线vs左」「基线vs右」，再聚合出 基线独有/A独有/B独有/共同 四类变更。"""
    kb, kl, kr = detect_kind(base), detect_kind(left), detect_kind(right)
    if kb == kl == kr:
        kind = kb
    elif {kb, kl, kr} <= {"jar", "zip"}:
        kind = "jar"
    elif {kb, kl, kr} <= {"dir"}:
        kind = "dir"
    else:
        return {"success": False,
                "message": u"三向比对要求三者类型一致（当前：基线=%s 左=%s 右=%s）" % (kb, kl, kr)}
    start = time.time()
    if kind == "dir":
        bl = compare_dirs(base, left, True, False, patterns)
        br = compare_dirs(base, right, True, False, patterns)
        a_map = _status_map_dir(bl)
        b_map = _status_map_dir(br)

        def get_eq(k):
            lp = os.path.join(left, k)
            rp = os.path.join(right, k)
            if os.path.isfile(lp) and os.path.isfile(rp):
                return file_md5(lp) == file_md5(rp)
            return False

        agg = _aggregate_three("dir", a_map, b_map, get_eq)
    elif kind in ("jar", "zip"):
        bl = compare_jar_entries(base, left, patterns=patterns)
        br = compare_jar_entries(base, right, patterns=patterns)
        a_map = _status_map_dir(bl)
        b_map = _status_map_dir(br)

        _common = set(a_map) & set(b_map)
        _sigl = _zip_sigs(left, _common)
        _sigr = _zip_sigs(right, _common)

        def get_eq(k):
            if k in _sigl and k in _sigr:
                return _sigl[k] == _sigr[k]
            return False

        agg = _aggregate_three("jar", a_map, b_map, get_eq)
    elif kind == "text":
        lb, _ = read_text_lines(base)
        ll, _ = read_text_lines(left)
        lr, _ = read_text_lines(right)
        a_status = ("same" if lb == ll else
                    "added" if (not lb and ll) else
                    "deleted" if (lb and not ll) else "modified")
        b_status = ("same" if lb == lr else
                    "added" if (not lb and lr) else
                    "deleted" if (lb and not lr) else "modified")
        a_map = {"__text__": a_status}
        b_map = {"__text__": b_status}

        def get_eq(k):
            return ll == lr

        agg = _aggregate_three("text", a_map, b_map, get_eq)
        agg["text_diff"] = make_text_diff(ll, lr, u"左(A)", u"右(B)")
    elif kind == "xlsx":
        bl = compare_excel_files(base, left)
        br = compare_excel_files(base, right)
        a_map = _status_map_sheet(bl)
        b_map = _status_map_sheet(br)

        _sigl = _sheet_signatures(left)
        _sigr = _sheet_signatures(right)

        def get_eq(k):
            # 原先恒返回 None → common_conflict 永远是 0，「共同(冲突)」永不显示。
            # 现按 sheet 内容签名判断左右是否同值：同值→无冲突，不同→冲突。
            if k in _sigl and k in _sigr:
                return _sigl[k] == _sigr[k]
            return None

        agg = _aggregate_three("xlsx", a_map, b_map, get_eq)
    else:
        rb = compare_binary_files(base, left)
        rr = compare_binary_files(base, right)
        return {"success": True, "kind": kind,
                "note": u"该类型不支持三向逐项聚合，已给出两侧相对基线的二进制摘要",
                "base_vs_left": rb, "base_vs_right": rr}

    agg["success"] = True
    agg["kind"] = kind
    agg["base_path"] = base
    agg["left_path"] = left
    agg["right_path"] = right
    agg["elapsed_ms"] = int((time.time() - start) * 1000)
    return agg


# ========================= 8. 自动比对 =========================
ARCHIVE_KINDS = {"jar", "zip"}


def compare_auto(left, right, patterns=None):
    k1, k2 = detect_kind(left), detect_kind(right)
    if k1 == k2:
        kind = k1
    elif k1 in ARCHIVE_KINDS and k2 in ARCHIVE_KINDS:
        kind = "jar"
    else:
        return {"success": False, "message": u"两侧类型不一致：左=%s，右=%s，无法自动比对" % (k1, k2)}
    if kind == "dir":
        return merge_dict({"success": True, "kind": kind},
                          compare_dirs(left, right, patterns=patterns))
    if kind == "xlsx":
        return merge_dict({"success": True, "kind": kind}, compare_excel_files(left, right))
    if kind in ARCHIVE_KINDS:
        return merge_dict({"success": True, "kind": "jar"},
                          compare_jar_entries(left, right, patterns=patterns))
    if kind == "text":
        return merge_dict({"success": True, "kind": kind}, compare_text_files(left, right))
    if kind in ("docx", "pptx"):
        r = compare_ooxml(left, right)
        return merge_dict({"success": True, "kind": kind}, r)
    r = compare_binary_files(left, right)
    return merge_dict({"success": True, "kind": kind}, r)


# ========================= 历史记录持久化 =========================
def load_history():
    if not os.path.isfile(HISTORY_PATH):
        return []
    out = []
    try:
        with compat.open_text(HISTORY_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def append_history(rec):
    try:
        with compat.open_text(HISTORY_PATH, "a") as f:
            # 历史记录里含中文文件名/提示，py2 下必须走 compat.json_bytes 归一后
            # 再序列化（裸 json.dumps(..., ensure_ascii=False) 会 UnicodeDecodeError）
            f.write(compat.to_text(compat.json_bytes(rec)) + "\n")
        return True
    except Exception:
        return False


def delete_history(id_):
    rows = [r for r in load_history() if r.get("id") != id_]
    tmp = HISTORY_PATH + ".tmp"
    try:
        with compat.open_text(tmp, "w") as f:
            for r in rows:
                f.write(compat.to_text(compat.json_bytes(r)) + "\n")
        compat.os_replace(tmp, HISTORY_PATH)
        return True
    except Exception:
        return False


def record_compare(type_name, left, right, summary, params, compare_id=""):
    rec = {"id": compat.rand_hex(8), "ts": compat.fmt_time(), "ts_unix": time.time(),
           "type": type_name, "left": left, "right": right,
           "summary": summary if summary is not None else {},
           "params": params or {}, "compare_id": compare_id}
    append_history(rec)
    return rec["id"]


# ========================= 报告 / 补丁导出 =========================
REPORT_CSS = (
    "body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;color:#1e293b;margin:24px}"
    "h1{font-size:20px;border-bottom:2px solid #2563eb;padding-bottom:8px}"
    "h3{font-size:15px;margin:18px 0 6px}"
    "table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}"
    "th,td{border:1px solid #e3e8ef;padding:6px 10px;text-align:left;vertical-align:top}"
    "th{background:#f1f5f9}.kv th{width:180px}"
    ".note{color:#92400e;background:#fef3c7;padding:8px 12px;border-radius:6px}"
    ".code{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;white-space:pre-wrap}"
    "tr.st-different td{background:#fef2f2}tr.st-only_in_left td,tr.st-only_in_right td{background:#fffbeb}"
)


def _summary_pairs(result):
    out = []
    s = result.get("summary")
    if isinstance(s, dict):
        label = {"same": u"相同", "different": u"不同", "only_in_left": u"仅左",
                 "only_in_right": u"仅右", "ignored": u"已忽略", "base_only": u"基线独有",
                 "a_only": u"A独有", "b_only": u"B独有", "common": u"共同",
                 "common_conflict": u"共同(冲突)", "added": u"新增", "removed": u"删除",
                 "changed": u"修改", "diff_cell_count": u"差异单元格",
                 "same_sheets": u"相同Sheet", "different_sheets": u"不同Sheet"}
        for k, v in s.items():
            out.append((label.get(k, k), v))
    elif "same" in result:
        out.append((u"是否相同", u"是" if result["same"] else u"否"))
    return out


def _report_dir_jar_html(result):
    details = result.get("details") or []
    if not details:
        return u'<p class="note">无差异项</p>'
    h = u'<table><thead><tr><th>路径</th><th>状态</th><th>左大小</th><th>右大小</th></tr></thead><tbody>'
    for d in details[:MAX_DETAILS]:
        h += "<tr class=\"st-%s\"><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc_html(d.get("status", "")), esc_html(d.get("path", "")),
            esc_html(d.get("status", "")), esc_html(fmt_size(d.get("left_size"))),
            esc_html(fmt_size(d.get("right_size"))))
    h += "</tbody></table>"
    if result.get("truncated"):
        h += u'<p class="note">差异条目过多，已截断</p>'
    return h


def _report_three_html(result):
    items = result.get("items") or []
    if not items:
        return u'<p class="note">三向无差异项</p>'
    cat_label = {"base_only": u"基线独有", "a_only": u"A独有", "b_only": u"B独有", "common": u"共同"}
    h = u'<table><thead><tr><th>路径/标识</th><th>分类</th><th>A方向</th><th>B方向</th></tr></thead><tbody>'
    for it in items:
        cl = cat_label.get(it["category"], it["category"])
        if it.get("conflict"):
            cl += u" (冲突)"
        h += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc_html(it["path"]), esc_html(cl), esc_html(it["a_dir"]), esc_html(it["b_dir"]))
    h += "</tbody></table>"
    return h


def render_report_html(result, meta):
    h = "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">" \
        u"<title>比对报告</title><style>%s</style></head><body>" % REPORT_CSS
    h += u"<h1>文件比对报告</h1>"
    h += u"<p>类型：<b>%s</b> ｜ 生成时间：%s</p>" % (esc_html(safe_str(meta.get("type"))),
                                                     esc_html(safe_str(meta.get("time", ""))))
    h += u"<p>左 / 基线：%s</p>" % esc_html(safe_str(meta.get("left")))
    h += u"<p>右：%s</p>" % esc_html(safe_str(meta.get("right")))
    sp = _summary_pairs(result)
    if sp:
        h += '<table class="kv"><tbody>'
        for k, v in sp:
            h += "<tr><th>%s</th><td>%s</td></tr>" % (esc_html(k), esc_html(v))
        h += "</tbody></table>"
    kind = result.get("kind")
    if kind in ("dir", "jar"):
        h += _report_dir_jar_html(result)
    elif kind == "text":
        if result.get("html"):
            h += '<div>%s</div>' % result["html"]
        elif result.get("text"):
            h += '<pre class="code">%s</pre>' % esc_html(result["text"])
    elif kind == "xlsx":
        for sh in result.get("sheets", []):
            h += u"<h3>%s — %s</h3>" % (esc_html(sh["sheet_name"]), esc_html(sh["status"]))
            if sh["diffs"]:
                h += u"<table><thead><tr><th>单元格</th><th>左</th><th>右</th></tr></thead><tbody>"
                for d in sh["diffs"]:
                    h += "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        esc_html(d["cell"]), esc_html(d["left"]), esc_html(d["right"]))
                h += "</tbody></table>"
    elif kind in ("docx", "pptx"):
        if result.get("changes"):
            h += u"<table><thead><tr><th>类型</th><th>位置</th><th>左</th><th>右</th></tr></thead><tbody>"
            for c in result["changes"]:
                h += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    esc_html(c["type"]), esc_html(c.get("id", "")),
                    esc_html(c.get("left") or ""), esc_html(c.get("right") or ""))
            h += "</tbody></table>"
        if result.get("text"):
            h += '<pre class="code">%s</pre>' % esc_html(result["text"])
    elif kind == "three":
        h += _report_three_html(result)
    if result.get("note"):
        h += '<p class="note">%s</p>' % esc_html(result["note"])
    h += "</body></html>"
    return h


def render_report_md(result, meta):
    lines = []
    lines.append(u"# 文件比对报告")
    lines.append("")
    lines.append(u"- 类型：%s" % safe_str(meta.get("type")))
    lines.append(u"- 生成时间：%s" % safe_str(meta.get("time", "")))
    lines.append(u"- 左 / 基线：%s" % safe_str(meta.get("left")))
    lines.append(u"- 右：%s" % safe_str(meta.get("right")))
    lines.append("")
    sp = _summary_pairs(result)
    if sp:
        lines.append(u"## 统计")
        for k, v in sp:
            lines.append(u"- %s：%s" % (k, v))
        lines.append("")
    kind = result.get("kind")
    if kind in ("dir", "jar"):
        lines.append(u"## 差异清单")
        for d in (result.get("details") or [])[:MAX_DETAILS]:
            lines.append("- `%s`  [%s]" % (d.get("path", ""), d.get("status", "")))
    elif kind == "text":
        lines.append(u"## 文本差异")
        if result.get("text"):
            lines.append("```")
            lines.append(result["text"])
            lines.append("```")
    elif kind == "xlsx":
        lines.append(u"## 工作表差异")
        for sh in result.get("sheets", []):
            if sh["diffs"]:
                lines.append("### %s (%s)" % (sh["sheet_name"], sh["status"]))
                for d in sh["diffs"]:
                    lines.append(u"- %s  左:%s  右:%s" % (d["cell"], d["left"], d["right"]))
    elif kind in ("docx", "pptx"):
        if result.get("changes"):
            lines.append(u"## 段落/幻灯片差异")
            for c in result["changes"]:
                lines.append(u"- [%s] %s  左:%s  右:%s" % (
                    c["type"], c.get("id", ""), c.get("left") or "", c.get("right") or ""))
    elif kind == "three":
        lines.append(u"## 三向分类")
        for it in result.get("items") or []:
            lines.append("- `%s`  %s  A:%s  B:%s%s" % (
                it["path"], it["category"], it["a_dir"], it["b_dir"],
                u" (冲突)" if it.get("conflict") else ""))
    if result.get("note"):
        lines.append("")
        lines.append(u"> 备注：%s" % result["note"])
    return "\n".join(lines)


# ========================= 页面 =========================
@app.route("/")
def index():
    """前端页面：templates/diff_file_dir_jar_excel.html（改完刷新浏览器即生效）"""
    return render_template("diff_file_dir_jar_excel.html")


# ========================= API =========================
@app.route("/api/compare/auto", methods=["POST"])
def api_compare_auto():
    left, right, tmp_dir, err = collect_pair(
        ("left_path", "right_path"), ("left_path", "right_path"), ("left", "right"))
    if err:
        return jsonify({"success": False, "message": err})
    err = check_pair_paths(left, right, tmp_dir)
    if err:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"success": False, "message": err})
    use_saved = _bool_input("use_saved_rules", True)
    temp = split_rules(safe_str(_input("ignore", "")))
    k1, k2 = detect_kind(left), detect_kind(right)
    scope = ("dir" if (k1 == "dir" and k2 == "dir")
             else "jar" if {k1, k2} <= {"jar", "zip"} else "all")
    patterns = build_ignore_patterns(scope, temp, use_saved)
    sid = new_session(left, right, tmp_dir)
    result = compare_auto(left, right, patterns=patterns)
    result["compare_id"] = sid
    set_session_result(sid, result)
    record_compare("auto", left, right, result.get("summary"),
                   {"left": left, "right": right, "ignore": safe_str(_input("ignore", "")),
                    "use_saved_rules": use_saved}, sid)
    return jsonify(result)


@app.route("/api/compare/dir", methods=["POST"])
def api_compare_dir():
    data = request.get_json(silent=True) or {}
    dir1 = safe_str(data.get("dir1")).strip()
    dir2 = safe_str(data.get("dir2")).strip()
    if not dir1 or not dir2:
        return jsonify({"success": False, "message": u"请输入两个目录路径"})
    err = check_pair_paths(dir1, dir2, None)
    if err:
        return jsonify({"success": False, "message": err})
    use_saved = _bool_input("use_saved_rules", True)
    temp = split_rules(safe_str(_input("ignore", "")))
    patterns = build_ignore_patterns("dir", temp, use_saved)
    result = compare_dirs(dir1, dir2,
                           compare_content=bool(data.get("compare_content", True)),
                           only_diff=bool(data.get("only_diff", False)),
                           patterns=patterns)
    if result.get("success"):
        sid = new_session(dir1, dir2, None)
        result["compare_id"] = sid
        set_session_result(sid, result)
        record_compare("dir", dir1, dir2, result.get("summary"),
                       {"dir1": dir1, "dir2": dir2,
                        "compare_content": bool(data.get("compare_content", True)),
                        "only_diff": bool(data.get("only_diff", False)),
                        "ignore": safe_str(_input("ignore", "")),
                        "use_saved_rules": use_saved}, sid)
    return jsonify(result)


@app.route("/api/compare/file", methods=["POST"])
def api_compare_file():
    left, right, tmp_dir, err = collect_pair(
        ("file1", "file2"), ("file1_path", "file2_path"), ("file1", "file2"))
    if err:
        return jsonify({"success": False, "message": err})
    err = check_pair_paths(left, right, tmp_dir)
    if err:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"success": False, "message": err})
    result = compare_text_files(left, right)
    if result.get("success"):
        sid = new_session(left, right, tmp_dir)  # 会话保留 tmp_dir，TTL 到期后自动清理
        set_session_result(sid, result)
        record_compare("file", left, right,
                       {"same": result.get("same")},
                       {"left": left, "right": right}, sid)
    return jsonify(result)


@app.route("/api/compare/jar", methods=["POST"])
def api_compare_jar():
    left, right, tmp_dir, err = collect_pair(
        ("jar1_path", "jar2_path"), ("jar1_path", "jar2_path"), ("jar1", "jar2"))
    if err:
        return jsonify({"success": False, "message": err})
    err = check_pair_paths(left, right, tmp_dir)
    if err:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"success": False, "message": err})
    ignore_sign = _bool_input("ignore_sign", True)
    raw = safe_str(_input("ignores", ""))
    ignores = [l.strip() for l in raw.splitlines() if l.strip()]
    use_saved = _bool_input("use_saved_rules", True)
    temp = split_rules(safe_str(_input("ignore", "")))
    patterns = build_ignore_patterns("jar", ignores + temp, use_saved)
    sid = new_session(left, right, tmp_dir,
                      jar_opts={"ignore_sign": ignore_sign, "ignores": ignores})
    result = compare_jar_entries(left, right, ignore_sign, None, patterns)
    result["compare_id"] = sid
    set_session_result(sid, result)
    record_compare("jar", left, right, result.get("summary"),
                   {"left": left, "right": right, "ignore_sign": ignore_sign,
                    "ignores": ignores, "ignore": safe_str(_input("ignore", "")),
                    "use_saved_rules": use_saved}, sid)
    return jsonify(result)


@app.route("/api/compare/ooxml", methods=["POST"])
def api_compare_ooxml():
    """docx / pptx 结构化内容比对（也可由自动比对内部调用）。"""
    left, right, tmp_dir, err = collect_pair(
        ("left_path", "right_path"), ("left_path", "right_path"), ("left", "right"))
    if err:
        return jsonify({"success": False, "message": err})
    err = check_pair_paths(left, right, tmp_dir)
    if err:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"success": False, "message": err})
    sid = new_session(left, right, tmp_dir)
    result = compare_ooxml(left, right)
    result["compare_id"] = sid
    set_session_result(sid, result)
    record_compare("ooxml", left, right, result.get("summary"),
                   {"left": left, "right": right}, sid)
    return jsonify(result)


@app.route("/api/compare/three", methods=["POST"])
def api_compare_three():
    base, left, right, tmp_dir, err = collect_triple()
    if err:
        return jsonify({"success": False, "message": err})
    err = check_triple_paths(base, left, right, tmp_dir)
    if err:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"success": False, "message": err})
    kb = detect_kind(base)
    scope = ("dir" if kb == "dir" else "jar" if kb in ("jar", "zip") else "all")
    use_saved = _bool_input("use_saved_rules", True)
    temp = split_rules(safe_str(_input("ignore", "")))
    patterns = build_ignore_patterns(scope, temp, use_saved)
    result = compare_three_way(base, left, right, patterns)
    if result.get("success"):
        sid = new_session(base, right, tmp_dir,
                          three_opts={"base": base, "left": left, "right": right,
                                      "ignore": safe_str(_input("ignore", "")),
                                      "use_saved_rules": use_saved})
        result["compare_id"] = sid
        set_session_result(sid, result)
        record_compare("three", base, right, result.get("summary"),
                       {"base": base, "left": left, "right": right,
                        "ignore": safe_str(_input("ignore", "")),
                        "use_saved_rules": use_saved}, sid)
    return jsonify(result)


@app.route("/api/compare/batch", methods=["POST"])
def api_compare_batch():
    data = request.get_json(silent=True) or {}
    pairs = data.get("pairs") or []
    if not isinstance(pairs, list) or not pairs:
        return jsonify({"success": False, "message": u"请提供 pairs 列表"})
    use_saved = _bool_input("use_saved_rules", True)
    temp = split_rules(safe_str(_input("ignore", "")))
    patterns = build_ignore_patterns("dir", temp, use_saved)
    norm = []
    for p in pairs:
        l = safe_str(p.get("left") if isinstance(p, dict) else p).strip()
        r = safe_str(p.get("right") if isinstance(p, dict) else "").strip()
        if not l or not r:
            return jsonify({"success": False, "message": u"存在空路径的配对"})
        err = check_pair_paths(l, r, None)
        if err:
            return jsonify({"success": False, "message": err})
        norm.append((l, r))

    def run_one(l, r):
        return {"left": l, "right": r,
                "result": compare_dirs(l, r, True, False, patterns)}

    if len(norm) > 3:
        def job(ctx):
            total = len(norm)
            results = []
            for i, (l, r) in enumerate(norm):
                ctx.check_canceled()
                results.append(run_one(l, r))
                ctx.progress(int((i + 1) * 100.0 / total), u"已比对 %d/%d" % (i + 1, total))
            ctx.set(results=results, matrix=_batch_matrix(results))
            return {"done": True}

        t = TM.submit(u"批量目录比对", "batch", job, user=req_user(), target="")
        record_compare("batch", norm[0][0], norm[-1][1], {"pairs": len(norm)},
                       {"pairs": [{"left": l, "right": r} for l, r in norm],
                        "ignore": safe_str(_input("ignore", "")),
                        "use_saved_rules": use_saved})
        return jsonify({"success": True, "async": True, "task_id": t["id"]})

    results = [run_one(l, r) for l, r in norm]
    record_compare("batch", norm[0][0], norm[-1][1], {"pairs": len(norm)},
                   {"pairs": [{"left": l, "right": r} for l, r in norm],
                    "ignore": safe_str(_input("ignore", "")),
                    "use_saved_rules": use_saved})
    return jsonify({"success": True, "async": False,
                    "results": results, "matrix": _batch_matrix(results)})


def _batch_matrix(results):
    matrix = []
    totals = {"same": 0, "different": 0, "only_in_left": 0, "only_in_right": 0, "ignored": 0}
    for r in results:
        res = r.get("result") or {}
        s = res.get("summary") or {}
        counts = {k: s.get(k, 0) for k in
                  ("same", "different", "only_in_left", "only_in_right", "ignored")}
        matrix.append({"left": r["left"], "right": r["right"], "counts": counts})
        for k in totals:
            totals[k] += counts.get(k, 0)
    return {"matrix": matrix, "totals": totals}


@app.route("/api/compare/jar/entry", methods=["POST"])
def api_jar_entry():
    """按需拉取单个条目的内容差异 / 展开嵌套归档。"""
    data = request.get_json(silent=True) or {}
    sid = safe_str(data.get("compare_id")).strip()
    path = safe_str(data.get("path")).strip()
    mode = data.get("mode") or "diff"
    sess = get_session(sid)
    if not sess:
        return jsonify({"success": False, "message": u"会话不存在或已过期，请重新发起比对"})
    chain = [c for c in path.split("!") if c]
    if not chain or len(chain) > MAX_CHAIN_DEPTH:
        return jsonify({"success": False, "message": u"条目路径非法或嵌套层级过深"})
    opts = sess.get("jar_opts") or {}
    try:
        z1, name = resolve_zip_chain(sess["left"], chain)
        z2, _ = resolve_zip_chain(sess["right"], chain)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)})
    if z1 is None or z2 is None:
        if z1:
            z1.close()
        if z2:
            z2.close()
        side = u"左侧" if z1 else u"右侧"
        return jsonify({"success": True, "status": "only_in_left" if z1 else "only_in_right",
                        "kind": guess_entry_kind(chain[-1]),
                        "note": u"该条目仅存在于%s" % side, "diff": None})
    try:
        i1, i2 = _get_info(z1, name), _get_info(z2, name)
        if i1 is None or i2 is None:
            side = u"左侧" if i1 else u"右侧"
            pz, pi = (z1, i1) if i1 else (z2, i2)
            resp = {"success": True, "status": "only_in_left" if i1 else "only_in_right",
                    "kind": guess_entry_kind(name), "note": u"该条目仅存在于%s" % side, "diff": None}
            if pi is not None and guess_entry_kind(name) == "text" and pi.file_size <= MAX_ENTRY_READ:
                try:
                    text = _decode_bytes(pz.read(name))
                    if len(text) > 200000:
                        text = text[:200000] + u"\n……（截断）"
                    resp["preview"] = text
                except Exception:
                    pass
            return jsonify(resp)

        kind = guess_entry_kind(name)

        # 展开嵌套归档
        if mode == "expand":
            if kind != "nested-archive":
                return jsonify({"success": False, "message": u"该条目不是嵌套归档"})
            if i1.file_size > MAX_NESTED_READ or i2.file_size > MAX_NESTED_READ:
                return jsonify({"success": False, "message": u"嵌套归档过大，不支持展开"})
            nz1 = zipfile.ZipFile(io.BytesIO(z1.read(name)))
            nz2 = zipfile.ZipFile(io.BytesIO(z2.read(name)))
            try:
                ninfo1 = {i.filename: i for i in nz1.infolist() if not i.is_dir()}
                ninfo2 = {i.filename: i for i in nz2.infolist() if not i.is_dir()}
                patterns = jar_ignore_patterns(opts.get("ignore_sign", True), opts.get("ignores"))
                summary, details, truncated, _ = _compare_zip_infos(ninfo1, ninfo2, patterns)
                return jsonify({"success": True, "mode": "expand", "summary": summary,
                                "details": details, "truncated": truncated})
            finally:
                nz1.close()
                nz2.close()

        # 内容差异
        if kind == "text":
            if i1.file_size > MAX_ENTRY_READ or i2.file_size > MAX_ENTRY_READ:
                return jsonify({"success": True, "status": "different", "kind": kind,
                                "diff": {"mode": "note", "text": u"文本条目过大，不提供内容级比对"},
                                "note": None})
            t1 = _decode_bytes(z1.read(name)).splitlines()
            t2 = _decode_bytes(z2.read(name)).splitlines()
            return jsonify({"success": True, "status": "different", "kind": kind,
                            "diff": make_text_diff(t1, t2, u"左侧", u"右侧"), "note": None})

        if kind == "class":
            if i1.file_size <= MAX_ENTRY_READ and i2.file_size <= MAX_ENTRY_READ:
                l1 = _javap_lines(z1.read(name))
                l2 = _javap_lines(z2.read(name))
                if l1 is not None and l2 is not None:
                    return jsonify({"success": True, "status": "different", "kind": kind,
                                    "diff": make_text_diff(l1, l2, u"左侧反汇编", u"右侧反汇编"),
                                    "note": u"javap -p -c 反汇编比对"})
            return jsonify({"success": True, "status": "different", "kind": kind,
                            "diff": {"mode": "summary",
                                     "rows": _binary_summary_rows(z1, name, i1, z2, name, i2)},
                            "note": u"未检测到 javap（JDK），无法反汇编比对；仅给出摘要。"
                                    u"字节码差异也可能由编译器版本/时间导致。"})

        note = u"嵌套归档建议点击「展开」查看内部差异" if kind == "nested-archive" else None
        return jsonify({"success": True, "status": "different", "kind": kind,
                        "diff": {"mode": "summary",
                                 "rows": _binary_summary_rows(z1, name, i1, z2, name, i2)},
                        "note": note})
    except zipfile.BadZipFile:
        return jsonify({"success": False, "message": u"嵌套归档损坏或不是有效的 zip"})
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        z1.close()
        z2.close()


@app.route("/api/compare/dir/entry", methods=["POST"])
def api_dir_entry_diff():
    """目录比对结果中点击某个差异文件：按需返回该文件的内容级差异。"""
    try:
        data = request.get_json(silent=True) or {}
        sid = safe_str(data.get("compare_id")).strip()
        rel = safe_str(data.get("path")).strip().replace("\\", "/")
        sess = get_session(sid)
        if not sess:
            return jsonify({"success": False, "message": u"会话不存在或已过期，请重新发起目录比对"})
        if not rel:
            return jsonify({"success": False, "message": u"缺少 path"})
        if not (os.path.isdir(sess["left"]) and os.path.isdir(sess["right"])):
            return jsonify({"success": False, "message": u"该会话不是目录比对会话"})
        left = os.path.normpath(os.path.join(sess["left"], rel))
        right = os.path.normpath(os.path.join(sess["right"], rel))
        # 防 ../ 逃逸出比对根目录
        for p, root in ((left, sess["left"]), (right, sess["right"])):
            if not is_within(p, root):
                return jsonify({"success": False, "message": u"非法路径"})

        e1, e2 = os.path.isfile(left), os.path.isfile(right)
        if not e1 and not e2:
            return jsonify({"success": False, "message": u"两侧均不存在该文件：%s" % rel})

        # 仅单侧存在：文本给内容预览，其余给摘要
        if not (e1 and e2):
            p = left if e1 else right
            side = u"左" if e1 else u"右"
            kind = detect_kind(p)
            resp = {"success": True, "status": "only_in_left" if e1 else "only_in_right",
                    "kind": kind, "diff": None, "note": u"该文件仅存在于%s侧" % side}
            if kind == "text" and os.path.getsize(p) <= MAX_ENTRY_READ:
                try:
                    text = "\n".join(read_text_lines(p)[0])
                    resp["preview"] = text[:200000] + (u"\n……（截断）" if len(text) > 200000 else "")
                except Exception:
                    pass
            else:
                resp["diff"] = {"mode": "summary", "rows": [
                    [u"%s侧大小" % side, fmt_size(os.path.getsize(p))],
                    [u"%s侧 MD5" % side, file_md5(p)]]}
            return jsonify(resp)

        # 两侧都存在：文本走行级 diff，其余走摘要
        force_text = bool(data.get("force_text", False))
        kind = "text" if force_text else detect_kind(left)
        if (kind == "text"
                and os.path.getsize(left) <= MAX_ENTRY_READ
                and os.path.getsize(right) <= MAX_ENTRY_READ):
            lines1, enc1 = read_text_lines(left)
            lines2, enc2 = read_text_lines(right)
            return jsonify({"success": True, "status": "different", "kind": "text",
                            "encodings": [enc1, enc2], "note": None,
                            "diff": make_text_diff(lines1, lines2, u"左 " + rel, u"右 " + rel)})
        rows = [[u"左侧大小", fmt_size(os.path.getsize(left))],
                [u"右侧大小", fmt_size(os.path.getsize(right))],
                [u"左侧 MD5", file_md5(left)],
                [u"右侧 MD5", file_md5(right)]]
        if os.path.getsize(left) <= MAX_ENTRY_READ:
            rows.append([u"左侧 SHA-1", _file_sha1(left)])
        if os.path.getsize(right) <= MAX_ENTRY_READ:
            rows.append([u"右侧 SHA-1", _file_sha1(right)])
        note = (u"二进制文件：展示大小/哈希摘要" if kind == "binary"
                else u"识别为 %s，不展开内容，展示摘要" % kind)
        return jsonify({"success": True, "status": "different", "kind": kind,
                        "diff": {"mode": "summary", "rows": rows}, "note": note})
    except Exception as e:
        import traceback
        traceback.print_exc()   # 完整堆栈打印到服务端控制台
        return jsonify({"success": False,
                        "message": u"服务端异常：%s: %s" % (type(e).__name__, to_text(e))}), 500


@app.route("/api/compare/excel", methods=["POST"])
def api_compare_excel():
    left, right, tmp_dir, err = collect_pair(
        ("excel1_path", "excel2_path"), ("excel1_path", "excel2_path"), ("excel1", "excel2"))
    if err:
        return jsonify({"success": False, "message": err})
    err = check_pair_paths(left, right, tmp_dir)
    if err:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"success": False, "message": err})
    new_session(left, right, tmp_dir)
    compare_formula = _bool_input("compare_formula", False)
    result = compare_excel_files(left, right, data_only=not compare_formula)
    if result.get("success"):
        sid = new_session(left, right, tmp_dir)
        set_session_result(sid, result)
        record_compare("excel", left, right, result.get("summary"),
                       {"left": left, "right": right,
                        "compare_formula": compare_formula}, sid)
    return jsonify(result)


# ========================= 忽略规则 API =========================
@app.route("/api/ignore/rules", methods=["GET"])
def api_ignore_get():
    return jsonify({"success": True, "rules": load_ignore_rules(), "defaults": DEFAULT_RULES})


@app.route("/api/ignore/rules", methods=["POST"])
def api_ignore_save():
    data = request.get_json(silent=True) or {}
    rules = data.get("rules")
    if not isinstance(rules, list):
        return jsonify({"success": False, "message": u"rules 必须是列表"})
    norm = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        norm.append({"id": safe_str(r.get("id") or compat.rand_hex(6)),
                     "pattern": safe_str(r.get("pattern", "")),
                     "scope": safe_str(r.get("scope", "all")) or "all",
                     "comment": safe_str(r.get("comment", "")),
                     "enabled": bool(r.get("enabled", True))})
    if save_ignore_rules(norm):
        return jsonify({"success": True, "rules": norm})
    return jsonify({"success": False, "message": u"保存失败"})


@app.route("/api/ignore/test", methods=["POST"])
def api_ignore_test():
    data = request.get_json(silent=True) or {}
    path = safe_str(data.get("path", "")).strip()
    scope = safe_str(data.get("scope", "all")).strip() or "all"
    temp = split_rules(safe_str(data.get("ignore", "")))
    use_saved = bool(data.get("use_saved_rules", True))
    patterns = build_ignore_patterns(scope, temp, use_saved)
    matched = [label for rx, label in patterns if rx.search(path)]
    return jsonify({"success": True, "path": path, "scope": scope,
                    "matched": matched, "ignored": len(matched) > 0})


# ========================= 历史 API =========================
@app.route("/api/history", methods=["GET"])
def api_history():
    limit = compat.to_int(request.args.get("limit", "50"), 50, 1, 500)
    offset = compat.to_int(request.args.get("offset", "0"), 0, 0, 100000)
    typ = safe_str(request.args.get("type", "")).strip()
    rows = load_history()
    rows.sort(key=lambda r: r.get("ts_unix", 0), reverse=True)
    if typ:
        rows = [r for r in rows if r.get("type") == typ]
    total = len(rows)
    page = rows[offset:offset + limit]
    return jsonify({"success": True, "total": total, "limit": limit,
                    "offset": offset, "items": page})


@app.route("/api/history/<hid>", methods=["GET"])
def api_history_detail(hid):
    for r in load_history():
        if r.get("id") == hid:
            return jsonify({"success": True, "record": r})
    return jsonify({"success": False, "message": u"未找到历史记录"})


@app.route("/api/history/<hid>", methods=["DELETE"])
def api_history_delete(hid):
    delete_history(hid)
    return jsonify({"success": True})


# ========================= 导出 API =========================
@app.route("/api/export/report", methods=["POST"])
def api_export_report():
    data = request.get_json(silent=True) or {}
    sid = safe_str(data.get("compare_id")).strip()
    fmt = safe_str(request.args.get("format", "html")).strip().lower()
    if fmt not in ("html", "md"):
        fmt = "html"
    sess = get_session(sid)
    if not sess:
        return jsonify({"success": False, "message": u"会话不存在或已过期"})
    result = sess.get("result")
    if not result:
        return jsonify({"success": False, "message": u"会话无可导出的结果"})
    meta = {"type": result.get("kind", "unknown"), "left": sess.get("left"),
            "right": sess.get("right"), "time": compat.fmt_time()}
    if fmt == "html":
        body = render_report_html(result, meta)
        ctype = "text/html; charset=utf-8"
        fname = "compare_report_%s.html" % sid
    else:
        body = render_report_md(result, meta)
        ctype = "text/markdown; charset=utf-8"
        fname = "compare_report_%s.md" % sid
    resp = make_response(body.encode("utf-8"))
    resp.headers["Content-Type"] = ctype
    resp.headers["Content-Disposition"] = "attachment; filename=\"%s\"" % fname
    return resp


@app.route("/api/export/patch", methods=["POST"])
def api_export_patch():
    data = request.get_json(silent=True) or {}
    sid = safe_str(data.get("compare_id")).strip()
    sess = get_session(sid)
    if not sess:
        return jsonify({"success": False, "message": u"会话不存在或已过期"})
    left, right = sess.get("left"), sess.get("right")
    if not (left and right):
        return jsonify({"success": False, "message": u"会话缺少比对路径"})
    result = sess.get("result") or {}
    if result.get("kind") != "text":
        kind = detect_kind(left)
        if kind != "text":
            return jsonify({"success": False, "message": u"仅文本类差异支持导出补丁"})
    try:
        lines1, _ = read_text_lines(left)
        lines2, _ = read_text_lines(right)
    except (IOError, OSError) as e:
        return jsonify({"success": False, "message": u"读取文件失败：%s" % to_text(e)})
    patch = "\n".join(difflib.unified_diff(
        lines1, lines2, fromfile=left, tofile=right, lineterm=""))
    resp = make_response((patch or u"(无差异)").encode("utf-8"))
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=\"diff_%s.patch\"" % sid
    return resp


# ========================= 异步任务 API（批量比对） =========================
@app.route("/api/tasks/<tid>", methods=["GET"])
def api_task_get(tid):
    t = TM.get(tid)
    if not t:
        return jsonify({"success": False, "message": u"任务不存在"})
    return jsonify({"success": True, "task": t})


@app.route("/api/tasks/<tid>/stream", methods=["GET"])
def api_task_stream(tid):
    def gen():
        while True:
            t = TM.get(tid)
            if t is None:
                yield u"event: error\ndata: 任务不存在\n\n"
                break
            # 任务快照含中文（fileName / message），必须走 compat.json_bytes 归一：
            # 旧写法 "data: %s\n\n" % json.dumps(t, ensure_ascii=False) 在 py2 下
            # 既可能因混排 UnicodeDecodeError，也可能因字节串模板 % unicode 而炸。
            yield "data: %s\n\n" % compat.safe_str(compat.to_text(compat.json_bytes(t)))
            if t["state"] in ("success", "failed", "canceled"):
                break
            time.sleep(0.5)

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


# ========================= 启动 =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8087"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    if not AUTH_TOKEN:
        log(u"[提示] 未设置 COMPARE_TOKEN，接口无鉴权，请仅在可信内网使用。")
    if not ALLOW_ROOTS:
        log(u"[提示] 未设置 COMPARE_ALLOW_ROOTS，可读取服务器任意路径。")
    log(u"[启动] 文件比对工具 v2.1  Python %s  http://0.0.0.0:%d  debug=%s"
        % (sys.version.split()[0], port, debug))
    app.run(host="0.0.0.0", port=port, debug=debug)
