#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.audit —— 操作审计流水（追加写 + 哈希链防篡改）。

为什么用哈希链：审计日志本身是可读的 JSONL，任何人拿到文件都能改；
每条记录带上「前一条的哈希」，任何中间篡改都会让后续校验全部失败，
运维侧一眼就能看出流水被改过。

格式（每行一个 JSON）::

    {"seq":1,"ts":1690000000.1,"user":"admin","ip":"10.0.0.5","action":"cmd.exec",
     "target":"web-01","detail":"uptime","result":"ok","prev":"","hash":"..."}
"""
from __future__ import print_function

import hashlib
import time

from . import compat
from .store import JsonlLog

# 标准动作名（前端用于渲染中文标签与图标）
ACTIONS = {
    'auth.login': '登录',
    'auth.logout': '注销',
    'auth.fail': '登录失败',
    'user.add': '新增用户',
    'user.update': '修改用户',
    'user.delete': '删除用户',
    'server.add': '新增服务器',
    'server.update': '修改服务器',
    'server.delete': '删除服务器',
    'file.upload': '上传文件',
    'file.download': '下载文件',
    'file.delete': '删除文件',
    'file.edit': '编辑文件',
    'cmd.exec': '执行命令',
    'release.create': '创建发布单',
    'release.run': '执行发布',
    'release.rollback': '回滚发布',
    'task.run': '运行任务',
    'cmdb.update': '更新台账',
    'settings.update': '修改设置',
    'proxy.relay': '代理转发',
}


def action_label(action):
    return ACTIONS.get(action, action)


class AuditLog(object):
    """审计流水。写入为 O(1) 追加，读取支持过滤 + 分页。"""

    def __init__(self, path=None, max_entries=200000, key=''):
        self.log = JsonlLog(path, max_keep=max_entries)
        self.path = path
        self.key = key or 'opslib-audit'
        self._lock = compat.threading.Lock()
        self._seq = 0
        self._last_hash = ''
        self._seed()

    # ------------------------------------------------------------ 初始化
    def _seed(self):
        """从文件尾部恢复 seq 与最后一个哈希，保证重启后链不断"""
        if not self.path:
            return
        try:
            for entry in self.log.iter_all(reverse=True):
                self._seq = compat.to_int(entry.get('seq'), 0)
                self._last_hash = entry.get('hash') or ''
                return
        except Exception:
            pass

    # ------------------------------------------------------------ 哈希链
    def _digest(self, entry, prev):
        payload = '|'.join([
            compat.safe_str(entry.get('seq')),
            '%.6f' % (entry.get('ts') or 0),
            compat.safe_str(entry.get('user')),
            compat.safe_str(entry.get('ip')),
            compat.safe_str(entry.get('action')),
            compat.safe_str(entry.get('target')),
            compat.safe_str(entry.get('detail')),
            compat.safe_str(entry.get('result')),
            prev,
            self.key,
        ])
        return hashlib.sha256(compat.to_bytes(payload)).hexdigest()

    # ------------------------------------------------------------ 写入
    def write(self, user, ip, action, target='', detail='', result='ok', extra=None):
        with self._lock:
            self._seq += 1
            entry = {
                'seq': self._seq,
                'ts': time.time(),
                'time': compat.fmt_time(),
                'user': compat.safe_str(user) or '-',
                'ip': compat.safe_str(ip),
                'action': compat.safe_str(action),
                'label': action_label(action),
                'target': compat.safe_str(target),
                'detail': compat.safe_str(detail)[:2000],
                'result': compat.safe_str(result) or 'ok',
                'prev': self._last_hash,
            }
            if extra:
                entry['extra'] = extra
            entry['hash'] = self._digest(entry, self._last_hash)
            try:
                self.log.append(entry)
            except Exception as e:                            # noqa: BLE001
                # 落盘失败时**不得**推进 _last_hash：否则链头指向未持久化的记录，
                # 今后 verify() 会因为「上一条查不到/哈希对不上」而永久判定链断裂。
                entry['persisted'] = False
                compat.eprint('[audit] 写入失败: %s' % e)
            else:
                self._last_hash = entry['hash']
            return entry

    def write_req(self, req, action, target='', detail='', result='ok', extra=None):
        """便捷方法：直接从 Request 里取操作人和来源 IP"""
        return self.write(req.user or '-', req.client_ip, action, target, detail, result, extra)

    # ------------------------------------------------------------ 查询
    def query(self, user='', action='', target='', keyword='', result='',
              start=None, end=None, limit=100, offset=0, order='desc'):
        limit = compat.to_int(limit, 100, 1, 1000)
        offset = compat.to_int(offset, 0, 0)
        user = compat.safe_str(user)
        action = compat.safe_str(action)
        target = compat.safe_str(target)
        keyword = compat.safe_str(keyword).lower()
        result = compat.safe_str(result)
        items = []
        total = 0
        matched = 0
        iterator = self.log.iter_all(reverse=(order == 'desc'))
        for entry in iterator:
            if start and (entry.get('ts') or 0) < float(start):
                continue
            if end and (entry.get('ts') or 0) > float(end):
                continue
            if user and entry.get('user') != user:
                continue
            if action and entry.get('action') != action:
                continue
            if result and entry.get('result') != result:
                continue
            if target and target.lower() not in compat.safe_str(entry.get('target')).lower():
                continue
            if keyword:
                haystack = ' '.join([
                    compat.safe_str(entry.get('user')), compat.safe_str(entry.get('action')),
                    compat.safe_str(entry.get('label')), compat.safe_str(entry.get('target')),
                    compat.safe_str(entry.get('detail')), compat.safe_str(entry.get('ip')),
                ]).lower()
                if keyword not in haystack:
                    continue
            total += 1
            if matched < offset:
                matched += 1
                continue
            if len(items) < limit:
                items.append(entry)
            matched += 1
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def stats(self, days=7):
        """近 N 天概览：总量、按动作分布、按操作人分布、失败数"""
        since = time.time() - days * 86400
        by_action = {}
        by_user = {}
        total = 0
        failed = 0
        daily = {}
        for entry in self.log.iter_all():
            if (entry.get('ts') or 0) < since:
                continue
            total += 1
            by_action[entry.get('action')] = by_action.get(entry.get('action'), 0) + 1
            by_user[entry.get('user')] = by_user.get(entry.get('user'), 0) + 1
            if entry.get('result') not in ('ok', None, ''):
                failed += 1
            day = compat.fmt_time(entry.get('ts'), '%Y-%m-%d')
            daily[day] = daily.get(day, 0) + 1
        return {
            'days': days, 'total': total, 'failed': failed,
            'by_action': sorted([{'action': k, 'label': action_label(k), 'count': v}
                                 for k, v in by_action.items()],
                                key=lambda x: -x['count']),
            'by_user': sorted([{'user': k, 'count': v} for k, v in by_user.items()],
                              key=lambda x: -x['count']),
            'daily': sorted([{'day': k, 'count': v} for k, v in daily.items()],
                            key=lambda x: x['day']),
        }

    # ------------------------------------------------------------ 校验
    def verify(self, limit=0):
        """校验哈希链；返回 (ok, message, checked, broken_seq)"""
        prev = ''
        checked = 0
        for entry in self.log.iter_all():
            checked += 1
            expect_prev = entry.get('prev') or ''
            if expect_prev != prev:
                return False, '第 %s 条记录的 prev 不匹配（链路断裂）' % entry.get('seq'), checked, entry.get('seq')
            stored = entry.get('hash')
            entry_copy = dict(entry)
            entry_copy.pop('hash', None)
            calc = self._digest(entry_copy, expect_prev)
            if calc != stored:
                return False, '第 %s 条记录内容被修改（哈希不符）' % entry.get('seq'), checked, entry.get('seq')
            prev = stored
            if limit and checked >= limit:
                break
        return True, '审计链完整（共校验 %d 条）' % checked, checked, None

    def export_rows(self, **kwargs):
        """导出为二维数组（首行表头），供 CSV 使用"""
        data = self.query(limit=1000, **kwargs)
        rows = [['时间', '操作人', '来源IP', '动作', '对象', '详情', '结果']]
        for entry in data['items']:
            rows.append([entry.get('time') or compat.fmt_time(entry.get('ts')),
                         entry.get('user'), entry.get('ip'), entry.get('label') or entry.get('action'),
                         entry.get('target'), entry.get('detail'), entry.get('result')])
        return rows

    def trim(self):
        return self.log.trim()
