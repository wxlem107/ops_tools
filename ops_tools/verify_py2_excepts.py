#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
py2 异常捕获审查：找出 except 子句中「只写 OSError、漏写 IOError」的位置。

背景
----
Python 2 中 IOError 与 OSError 是**两个独立的类**（仅同源于 EnvironmentError）；
文件不存在 / 不可读 / 不可写抛的是 IOError。若 except 只写 OSError，在 py2 下
捕获不到，异常会一路逃逸到 HTTPServer，打出 traceback 并中断连接。

Python 3 把 IOError 变成 OSError 的别名，因此本机 py3 测试**无法**暴露此类问题
（线上 8089 上传报错即此因：`except (OSError, ValueError)` 漏了 IOError）。

本脚本对源码做静态扫描兜底，按 try 体内容给出启发式判定：
  - try 体内出现 open / open_text / os.remove / os.rename / os.replace /
    os.listdir / os.walk / shutil. / zipfile. 等易抛 IOError 的调用 → 需补 IOError
  - 仅 os.stat / os.makedirs / os.path.* 等 → 可接受（py2 这些也抛 OSError）

    python verify_py2_excepts.py            # 扫描
    python verify_py2_excepts.py --verbose  # 连可接受项一起列出

退出码：发现「需补 IOError」返回 1，否则 0（可直接用于 CI / 自检）。
"""
from __future__ import print_function

import ast
import io
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SCAN_DIRS = ['opslib', 'tools', os.path.join('platform_console')]

# Windows 控制台/管道默认常是 GBK，打印 ✅ 会抛 UnicodeEncodeError；强制 UTF-8。
# py3.7+ 有 reconfigure；py2 无此属性，走 hasattr 保护。
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 这些调用在 py2 下会抛 IOError（文件类错误），命中则判定「需补 IOError」
IO_RISKY = ('open', 'open_text', 'remove', 'rename', 'replace', 'rmtree',
            'listdir', 'walk', 'scandir', 'copyfileobj', 'copyfile', 'move',
            'getsize', 'stat_', 'mkstemp', 'NamedTemporaryFile', 'ZipFile')

# 只有「模块根」属于以下模块时，属性调用才按风险计（如 os.replace / os.path.getsize /
# shutil.rmtree / zipfile.ZipFile）。否则 s.replace(...) 这类**字符串方法**会被误判成
# 文件操作（str.replace 不会抛 IOError），使本门禁长期假红。
IO_MODULES = ('os', 'shutil', 'zipfile', 'tempfile', 'io', 'glob', 'codecs',
              'subprocess', 'sys', 'compat', 'gzip', 'tarfile', 'pathlib')

VERBOSE = '--verbose' in sys.argv

# py2 里 try/except 是 TryExcept 节点，py3 统一为 Try；try/finally 另计
TRY_NODES = tuple(cls for cls in (getattr(ast, n, None)
                                  for n in ('Try', 'TryExcept', 'TryFinally'))
                  if cls is not None)


def iter_py_files():
    seen = set()
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for cur, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith('.py'):
                    p = os.path.join(cur, name)
                    if p not in seen:
                        seen.add(p)
                        yield p
    for name in os.listdir(ROOT):
        if name.startswith('verify') and name.endswith('.py'):
            p = os.path.join(ROOT, name)
            if p not in seen:
                seen.add(p)
                yield p


def handler_names(node):
    """收集 except 子句声明的异常类名（支持 Name / Tuple / Attribute）"""
    names = set()

    def walk(expr):
        if isinstance(expr, ast.Name):
            names.add(expr.id)
        elif isinstance(expr, ast.Tuple):
            for elt in expr.elts:
                walk(elt)
        elif isinstance(expr, ast.Attribute):
            names.add(expr.attr)

    if node.type is not None:
        walk(node.type)
    return names


def _dotted(expr):
    """把纯属性链折成点号名：os.path.getsize -> 'os.path.getsize'。

    遇到调用/下标等非纯属性链（例如 `os.path.relpath(...).replace` 里的 x 是调用结果）
    立即返回 ''，从而不把字符串方法 `.replace` 误当文件操作。
    """
    parts = []
    cur = expr
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return '.'.join(parts)
    return ''


def risky_call_name(call):
    """判定一次调用是否属于「易抛 IOError」。是则返回展示用名，否则返回 ''。"""
    fn = call.func
    if isinstance(fn, ast.Name):
        # 裸函数调用：open(...) / open_text(...)
        return fn.id if fn.id in IO_RISKY else ''
    if isinstance(fn, ast.Attribute):
        if fn.attr not in IO_RISKY:
            return ''
        dotted = _dotted(fn)
        if not dotted:
            return ''            # 属性链根不是模块名（如 str.replace / obj.replace）
        if dotted.split('.', 1)[0] in IO_MODULES:
            return fn.attr
    return ''


def try_body_repr(try_node, lines):
    """取 try 体首行 + 命中 IO_RISKY 的调用名，用于判定与展示

    逐条语句 ast.walk，避免构造 ast.Module（py2 的 Module 签名无 type_ignores）。
    """
    start = try_node.body[0].lineno
    first = lines[start - 1].strip() if 0 < start <= len(lines) else ''
    hits = []
    for stmt in try_node.body:
        for child in ast.walk(stmt):
            if isinstance(child, ast.Call):
                name = risky_call_name(child)
                if name and name not in hits:
                    hits.append(name)
    return first, hits


def scan(path):
    try:
        text = io.open(path, 'r', encoding='utf-8').read()
    except Exception:
        return [], text
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [('SYNTAX', e.lineno or 0, str(e), [])], text
    lines = text.splitlines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, TRY_NODES):
            continue
        for h in getattr(node, 'handlers', []) or []:
            names = handler_names(h)
            if 'OSError' not in names:
                continue
            if 'IOError' in names or 'EnvironmentError' in names:
                continue
            first, hits = try_body_repr(node, lines)
            out.append((node.lineno, h.lineno, first, hits))
    return out, text


def main():
    risky, acceptable = [], []
    for path in sorted(iter_py_files()):
        items, _ = scan(path)
        rel = os.path.relpath(path, ROOT).replace('\\', '/')
        for lineno, hline, first, hits in items:
            rec = (rel, hline, first, hits)
            if hits:
                risky.append(rec)
            else:
                acceptable.append(rec)

    print('py2 异常捕获审查（漏写 IOError 的 except 子句）')
    print('扫描目录: %s' % ', '.join(SCAN_DIRS))
    print('-' * 72)

    if risky:
        print('\n[需补 IOError] %d 处：' % len(risky))
        for rel, hline, first, hits in risky:
            print('  %s:%d  try 首行: %s' % (rel, hline, first))
            print('      易抛 IOError 的调用: %s' % ', '.join(hits))
    if VERBOSE and acceptable:
        print('\n[可接受] %d 处（try 体仅 os.stat/os.makedirs 等）：' % len(acceptable))
        for rel, hline, first, _ in acceptable:
            print('  %s:%d  %s' % (rel, hline, first))

    print('\n' + '=' * 72)
    if risky:
        print('需补 IOError: %d 处，可接受: %d 处 —— 未通过 ❌' % (len(risky), len(acceptable)))
        return 1
    print('未发现漏写 IOError 的 except 子句（可接受项 %d 处）—— 通过 ✅' % len(acceptable))
    return 0


if __name__ == '__main__':
    sys.exit(main())
