#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.store —— 轻量 JSON 持久化。

设计要点：
  * 原子写（先写 .tmp 再 os_replace），避免断电/崩溃写坏配置；
  * 进程内读写锁，多线程服务安全；
  * 未指定路径时退化为「纯内存」，方便单机临时使用；
  * 记录 mtime，外部手工改文件后可自动感知。
"""
from __future__ import print_function

import os
import threading
import time

from .compat import json_bytes, json_loads, log, to_text, open_text, os_replace, makedirs

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    with _LOCKS_GUARD:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[path] = lk
        return lk


def atomic_write_json(path, obj, mode=None):
    """原子写入 JSON 文件；mode 为 None 时使用 0o600 保护敏感文件"""
    makedirs(os.path.dirname(os.path.abspath(path)))
    tmp = path + '.tmp'
    with open_text(tmp, 'w') as f:
        f.write(to_text(json_bytes(obj, indent=2, sort_keys=False)))
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except Exception:
            pass
    os_replace(tmp, path)


def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open_text(path, 'r') as f:
            data = json_loads(f.read(), default)
        return default if data is None else data
    except Exception as e:                                     # noqa: BLE001
        log('[store] 读取 %s 失败: %s' % (path, e))
        return default


class JsonStore(object):
    """带锁的 JSON 文档存储。

    用法::

        st = JsonStore('/opt/ops/data.json', {'servers': []})
        st.update(lambda d: d['servers'].append(item) or d)
        st.save()
    """

    def __init__(self, path=None, default=None, indent=2, file_mode=0o600):
        self.path = os.path.abspath(path) if path else None
        self.default = default if default is not None else {}
        self.indent = indent
        self.file_mode = file_mode
        self._data = None
        self._mtime = 0
        self._lock = _lock_for(self.path or ('mem:%d' % id(self)))
        if self.path:
            makedirs(os.path.dirname(self.path))

    # ---------------------------------------------------------- 加载 / 保存
    def load(self, force=False):
        with self._lock:
            if self.path and os.path.exists(self.path):
                try:
                    mtime = os.stat(self.path).st_mtime
                except OSError:
                    mtime = 0
                if force or self._data is None or mtime != self._mtime:
                    raw = read_json(self.path, None)
                    if isinstance(raw, dict):
                        self._data = raw
                    elif raw is not None:
                        self._data = dict(self.default)
                        self._data['value'] = raw
                    else:
                        self._data = dict(self.default) if isinstance(self.default, dict) else self.default
                    self._mtime = mtime
            elif self._data is None:
                self._data = dict(self.default) if isinstance(self.default, dict) else self.default
            return self._data

    def save(self, data=None):
        with self._lock:
            if data is not None:
                self._data = data
            if self._data is None:
                self._data = dict(self.default) if isinstance(self.default, dict) else self.default
            if not self.path:
                return
            atomic_write_json(self.path, self._data, mode=self.file_mode)
            try:
                self._mtime = os.stat(self.path).st_mtime
            except OSError:
                self._mtime = time.time()

    @property
    def data(self):
        return self.load()

    def update(self, mutator):
        """在锁内执行 mutator(data) 并落盘；mutator 可原地修改也可返回新对象"""
        with self._lock:
            data = self.load()
            out = mutator(data)
            if out is not None:
                data = out
            self._data = data
            self.save()
            return data

    def get(self, key, default=None):
        return self.load().get(key, default)

    def set(self, key, value):
        def _mut(d):
            d[key] = value
            return d
        return self.update(_mut)


class JsonlLog(object):
    """追加写 JSONL（每行一个 JSON 对象），适合审计流水、操作记录等只增不改的场景。

    追加写是 O(1)，且天然自带时间顺序；读取时按行解析，坏行自动跳过。
    """

    def __init__(self, path=None, max_keep=200000):
        self.path = os.path.abspath(path) if path else None
        self.max_keep = max_keep
        self._lock = _lock_for(self.path or ('jsonl:%d' % id(self)))
        if self.path:
            makedirs(os.path.dirname(self.path))

    def append(self, obj):
        line = to_text(json_bytes(obj)) + '\n'
        with self._lock:
            if not self.path:
                return obj
            with open_text(self.path, 'a') as f:
                f.write(line)
        return obj

    def iter_all(self, reverse=False):
        """逐行产出对象；reverse=True 时从最新往回读（通过一次性载入行索引实现）"""
        if not self.path or not os.path.exists(self.path):
            return
        with self._lock:
            try:
                with open_text(self.path, 'r') as f:
                    lines = f.readlines()
            except Exception:
                lines = []
        if reverse:
            lines = list(reversed(lines))
        for line in lines:
            line = line.strip()
            if not line:
                continue
            obj = json_loads(line, None)
            if isinstance(obj, dict):
                yield obj

    def tail(self, n=100):
        return list(self.iter_all(reverse=True))[:n]

    def rewrite(self, objs):
        """全量重写（用于按保留策略裁剪历史）"""
        with self._lock:
            if not self.path:
                return
            tmp = self.path + '.tmp'
            with open_text(tmp, 'w') as f:
                for obj in objs:
                    f.write(to_text(json_bytes(obj)) + '\n')
            os_replace(tmp, self.path)

    def trim(self):
        """行数超过 max_keep 时保留最新的一半"""
        if not self.path or not self.max_keep:
            return 0
        total = 0
        with self._lock:
            try:
                with open_text(self.path, 'r') as f:
                    total = sum(1 for _ in f)
            except Exception:
                return 0
        if total > self.max_keep:
            keep = self.tail(self.max_keep // 2)
            self.rewrite(list(reversed(keep)))
            return total - len(keep)
        return 0
