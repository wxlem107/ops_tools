# -*- coding: utf-8 -*-
"""反证脚本（临时）：把 json_bytes/json_safe 改回旧实现，确认新增检查确实会 FAIL。

旧实现 = `to_bytes(json.dumps(obj, ensure_ascii=False))`，没有 json_safe 归一。
用法：python .testlogs/redo_json_mixed.py
"""
from __future__ import print_function

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opslib import compat, store, apibase      # noqa: E402
import verify_py2_compat as V      # noqa: E402


def old_json_bytes(obj, **kwargs):
    kwargs.setdefault('ensure_ascii', False)
    return compat.to_bytes(json.dumps(obj, **kwargs))


# store / apibase 是 `from .compat import json_bytes` 直接绑定，必须一起替换
compat.json_bytes = old_json_bytes
store.json_bytes = old_json_bytes
apibase.json_bytes = old_json_bytes
compat.json_safe = lambda value: value          # 旧实现：不做归一


def _call(fn):
    try:
        return bool(fn())
    except Exception as exc:                     # noqa: BLE001
        print('      抛出 %s: %s' % (type(exc).__name__, exc))
        return False


print('解释器: Python %s' % sys.version.split()[0])
r1 = _call(V._json_safe_removes_bytes)
r2 = _call(V._order_detail_json_ok)
r3 = _call(lambda: V._order_store_roundtrip_ok(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'redo', 'orders.json')))
print('  json_safe 归一后不再有字节串 -> %s (期望 False)' % r1)
print('  发布单详情 JSON             -> %s (期望 False)' % r2)
print('  发布单落盘往返              -> %s (期望 False)' % r3)
if r1 or r2 or r3:
    print('反证失败 ❌ 旧实现竟然通过，说明这些检查是假测试')
    sys.exit(1)
print('反证成立 ✅ 旧实现下三项检查全部 FAIL，新检查有效')
