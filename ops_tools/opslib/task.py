#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.task —— 异步任务中心（提交、进度、日志、取消、历史）。

用途：上传/分发/发布/备份/巡检等耗时动作都交给它执行，
前端通过 SSE 订阅进度，服务重启后历史仍可查。

兼容：Python 2.7.5+ / 3.7.5+，仅标准库（自建线程池与队列）。
"""
from __future__ import print_function

import time

from . import compat
from .store import JsonStore

STATE_PENDING = 'pending'
STATE_RUNNING = 'running'
STATE_SUCCESS = 'success'
STATE_FAILED = 'failed'
STATE_CANCELED = 'canceled'
FINAL_STATES = (STATE_SUCCESS, STATE_FAILED, STATE_CANCELED)
STATE_LABEL = {
    STATE_PENDING: '排队中',
    STATE_RUNNING: '执行中',
    STATE_SUCCESS: '成功',
    STATE_FAILED: '失败',
    STATE_CANCELED: '已取消',
}


class TaskCanceled(Exception):
    """任务内主动抛出以表示被取消"""


class TaskContext(object):
    """传给任务函数的上下文：上报进度、写日志、检查取消"""

    def __init__(self, manager, task_id):
        self._manager = manager
        self.id = task_id

    def progress(self, percent, message=None):
        self._manager.set_progress(self.id, percent, message)

    def log(self, line, level='info'):
        self._manager.append_log(self.id, line, level)

    def canceled(self):
        return self._manager.is_canceled(self.id)

    def check_canceled(self):
        if self.canceled():
            raise TaskCanceled('任务已取消')

    def set(self, **kw):
        """写入任意结果字段，最终会出现在任务详情的 result 里"""
        self._manager.merge_result(self.id, kw)


class TaskManager(object):
    def __init__(self, path=None, max_workers=4, max_history=300, bus=None,
                 topic='task', log_lines=200):
        self.store = JsonStore(path, {'tasks': []}, file_mode=0o600)
        self.max_workers = max(1, compat.to_int(max_workers, 4, 1, 32))
        self.max_history = max(20, compat.to_int(max_history, 300, 20, 5000))
        self.log_lines = compat.to_int(log_lines, 200, 20, 5000)
        self.bus = bus
        self.topic = topic
        self._queue = compat.Queue()
        self._lock = compat.threading.RLock()
        self._cancel = set()
        self._fns = {}
        self._last_save = 0.0
        for _ in range(self.max_workers):
            compat.start_daemon(self._worker)

    # ------------------------------------------------------------ 内部
    def _tasks(self):
        return self.store.load().setdefault('tasks', [])

    def _find(self, task_id):
        for task in self._tasks():
            if task.get('id') == task_id:
                return task
        return None

    def _save(self, force=False):
        now = time.time()
        if not force and now - self._last_save < 1.0:
            return
        self._last_save = now
        with self._lock:
            tasks = self._tasks()
            if len(tasks) > self.max_history:
                finished = [t for t in tasks if t.get('state') in FINAL_STATES]
                alive = [t for t in tasks if t.get('state') not in FINAL_STATES]
                finished.sort(key=lambda t: t.get('created') or 0, reverse=True)
                keep = alive + finished[:max(0, self.max_history - len(alive))]
                keep.sort(key=lambda t: t.get('created') or 0)
                self.store.set('tasks', keep)
            else:
                self.store.save()

    def _publish(self, task):
        if self.bus is None:
            return
        try:
            self.bus.publish(self.topic, self.public(task), keep_history=0)
        except Exception:
            pass
        try:
            self.bus.publish('%s:%s' % (self.topic, task['id']), self.public(task), keep_history=1)
        except Exception:
            pass

    @staticmethod
    def public(task):
        if not task:
            return None
        out = dict(task)
        out['state_label'] = STATE_LABEL.get(task.get('state'), task.get('state'))
        log = out.get('log') or []
        out['log_count'] = len(log)
        return out

    # ------------------------------------------------------------ 提交
    def submit(self, name, kind, fn, params=None, user='', target='', meta=None):
        task = {
            'id': compat.rand_hex(8),
            'name': compat.safe_str(name),
            'kind': compat.safe_str(kind),
            'state': STATE_PENDING,
            'percent': 0,
            'message': '排队中',
            'created': time.time(),
            'created_time': compat.fmt_time(),
            'started': None,
            'finished': None,
            'duration': None,
            'user': compat.safe_str(user),
            'target': compat.safe_str(target),
            'params': params or {},
            'meta': meta or {},
            'result': {},
            'error': '',
            'log': [],
        }
        with self._lock:
            self.store.update(lambda d: d.setdefault('tasks', []).append(task))
            self._fns[task['id']] = fn
        self._publish(task)
        self._queue.put(task['id'])
        return self.public(task)

    # ------------------------------------------------------------ 状态更新
    def set_progress(self, task_id, percent, message=None):
        with self._lock:
            task = self._find(task_id)
            if not task:
                return
            task['percent'] = compat.to_int(percent, 0, 0, 100)
            if message:
                task['message'] = compat.safe_str(message)
        self._publish(task)
        self._save()

    def append_log(self, task_id, line, level='info'):
        entry = {'ts': time.time(), 'time': compat.fmt_time(None, '%H:%M:%S'),
                 'level': level, 'text': compat.safe_str(line)}
        with self._lock:
            task = self._find(task_id)
            if not task:
                return
            log = task.setdefault('log', [])
            log.append(entry)
            if len(log) > self.log_lines:
                del log[:len(log) - self.log_lines]
        self._publish(task)
        self._save()

    def merge_result(self, task_id, data):
        with self._lock:
            task = self._find(task_id)
            if not task:
                return
            task.setdefault('result', {}).update(data or {})
        self._publish(task)
        self._save()

    def finish(self, task_id, ok=True, message=None, result=None, error=''):
        with self._lock:
            task = self._find(task_id)
            if not task:
                return
            if task_id in self._cancel and ok:
                ok = False
                error = error or '任务已取消'
            task['state'] = STATE_SUCCESS if ok else STATE_FAILED
            task['percent'] = 100 if ok else task.get('percent', 0)
            task['message'] = message or ('完成' if ok else (error or '失败'))
            task['error'] = compat.safe_str(error)
            task['finished'] = time.time()
            task['finished_time'] = compat.fmt_time()
            if task.get('started'):
                task['duration'] = round(task['finished'] - task['started'], 2)
            if result:
                task.setdefault('result', {}).update(result)
        self._cancel.discard(task_id)
        self._publish(task)
        self._save(force=True)
        return self.public(task)

    def cancel(self, task_id):
        with self._lock:
            task = self._find(task_id)
            if not task:
                return False, '任务不存在'
            if task.get('state') in FINAL_STATES:
                return False, '任务已结束，无法取消'
            self._cancel.add(task_id)
            if task.get('state') == STATE_PENDING:
                task['state'] = STATE_CANCELED
                task['message'] = '已取消（未执行）'
                task['finished'] = time.time()
        if task.get('state') == STATE_CANCELED:
            self._publish(task)
            self._save(force=True)
        return True, '已请求取消'

    def is_canceled(self, task_id):
        return task_id in self._cancel

    # ------------------------------------------------------------ 查询
    def get(self, task_id, with_log=True):
        # 注意：不要用 load(force=True) 从磁盘重读 —— 工作线程可能正在内存里追加日志，
        # 而日志落盘有 1 秒节流，强制重读会用旧磁盘内容覆盖内存，造成日志/进度丢失。
        task = self._find(task_id)
        if not task:
            return None
        out = self.public(task)
        if not with_log:
            out.pop('log', None)
        return out

    def logs(self, task_id, offset=0):
        task = self._find(task_id)
        if not task:
            return []
        log = task.get('log') or []
        return log[compat.to_int(offset, 0, 0):]

    def list(self, state='', kind='', user='', limit=50):
        tasks = sorted(self._tasks(), key=lambda t: t.get('created') or 0, reverse=True)
        out = []
        for task in tasks:
            if state and task.get('state') != state:
                continue
            if kind and task.get('kind') != kind:
                continue
            if user and task.get('user') != user:
                continue
            out.append(self.public(task))
            if len(out) >= compat.to_int(limit, 50, 1, 500):
                break
        return out

    def stats(self):
        tasks = self._tasks()
        counter = {}
        for task in tasks:
            counter[task.get('state')] = counter.get(task.get('state'), 0) + 1
        return {'total': len(tasks), 'by_state': counter,
                'labels': {k: STATE_LABEL.get(k, k) for k in counter}}

    def wait(self, task_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.get(task_id, with_log=False)
            if not task or task.get('state') in FINAL_STATES:
                return task
            time.sleep(0.2)
        return self.get(task_id, with_log=False)

    def clear_finished(self):
        with self._lock:
            self.store.set('tasks', [t for t in self._tasks()
                                     if t.get('state') not in FINAL_STATES])
        return True

    # ------------------------------------------------------------ 工作线程
    def _worker(self):
        while True:
            task_id = self._queue.get()
            try:
                self._run(task_id)
            except Exception as e:                            # noqa: BLE001
                compat.eprint('[task] 调度异常: %s' % e)
            finally:
                self._queue.task_done()

    def _run(self, task_id):
        with self._lock:
            task = self._find(task_id)
            if not task:
                return
            if task.get('state') == STATE_CANCELED:
                return
            task['state'] = STATE_RUNNING
            task['started'] = time.time()
            task['started_time'] = compat.fmt_time()
            task['message'] = '执行中'
            fn = self._fns.pop(task_id, None)
            params = dict(task.get('params') or {})
        if fn is None:
            self.finish(task_id, False, error='任务函数丢失（服务可能已重启）')
            return
        self._publish(task)
        self._save(force=True)
        ctx = TaskContext(self, task_id)
        try:
            # submit 时传入的 params 必须在执行时回传给任务函数；
            # 否则 bulk-exec / cmdb 采集这类「按参数执行」的任务会拿到空参数
            # （曾导致批量执行下发空命令、台账永远全量采集）。
            result = fn(ctx, **params) if params else fn(ctx)
            if not ctx.canceled():
                self.finish(task_id, True, '完成', result if isinstance(result, dict) else None)
        except TaskCanceled as e:
            with self._lock:
                task = self._find(task_id)
                if task:
                    task['state'] = STATE_CANCELED
                    task['message'] = '已取消'
                    task['finished'] = time.time()
            self._publish(task)
            self._save(force=True)
        except Exception as e:                                # noqa: BLE001
            import traceback
            ctx.log(traceback.format_exc(), 'error')
            self.finish(task_id, False, error=compat.safe_str(e))


def run_sync(fn, *args, **kw):
    """把可调用对象当作任务函数跑一次（不经过队列），便于复用进度上报逻辑"""
    class _Ctx(object):
        def __init__(self):
            self.logs = []

        def progress(self, percent, message=None):
            pass

        def log(self, line, level='info'):
            self.logs.append(line)

        def canceled(self):
            return False

        def set(self, **k):
            pass

    return fn(_Ctx())
