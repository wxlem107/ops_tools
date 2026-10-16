#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opslib.notify —— 通知渠道（企业微信 / 钉钉 / 飞书 / 通用 webhook / SMTP 邮件）。

设计原则：全部用标准库实现，不引入 requests；
渠道配置存在 notify.json 里，告警中心、发布中心、任务中心共用同一份配置。
"""
from __future__ import print_function

import smtplib
import time

from . import compat, httpx
from .store import JsonStore

DEFAULT_CONFIG = {
    'channels': [],          # [{id, name, kind, url, enabled, secret}]
    'mail': {                # 邮件渠道（SMTP）
        'enabled': False, 'host': '', 'port': 465, 'ssl': True,
        'user': '', 'password': '', 'sender': '', 'receivers': [],
    },
    'events': {
        'alert': True, 'release': True, 'task_failed': True, 'disk': True,
    },
}

KINDS = {
    'wecom': '企业微信机器人',
    'dingtalk': '钉钉机器人',
    'feishu': '飞书机器人',
    'slack': 'Slack',
    'raw': '通用 JSON webhook',
}


def guess_kind(url):
    """按 URL 猜渠道类型"""
    url = compat.safe_str(url)
    if 'qyapi.weixin.qq.com' in url:
        return 'wecom'
    if 'oapi.dingtalk.com' in url:
        return 'dingtalk'
    if 'open.feishu.cn' in url or 'feishu' in url:
        return 'feishu'
    if 'hooks.slack.com' in url:
        return 'slack'
    return 'raw'


def build_payload(kind, title, text):
    """把标题+正文转换成各渠道要求的报文结构"""
    title = compat.safe_str(title)
    text = compat.safe_str(text)
    content = ('**%s**\n%s' % (title, text)) if title else text
    if kind == 'wecom':
        return {'msgtype': 'markdown', 'markdown': {'content': content}}
    if kind == 'dingtalk':
        return {'msgtype': 'markdown', 'markdown': {'title': title or '运维通知', 'text': content}}
    if kind == 'feishu':
        return {'msg_type': 'text', 'content': {'text': ('%s\n%s' % (title, text)).strip()}}
    if kind == 'slack':
        return {'text': ('*%s*\n%s' % (title, text)).strip()}
    return {'title': title, 'text': text, 'ts': time.time()}


def send_webhook(url, title, text, kind=None, timeout=8):
    """向 webhook 推送一条消息；返回 (ok, message)"""
    url = compat.safe_str(url).strip()
    if not url:
        return False, '未配置 webhook 地址'
    kind = kind or guess_kind(url)
    payload = build_payload(kind, title, text)
    resp = httpx.DEFAULT.post(url, json_body=payload, timeout=timeout, retries=1)
    if resp.status == 0:
        return False, '推送失败: %s' % (resp.error or '网络不可达')
    if not resp.ok:
        return False, 'HTTP %d: %s' % (resp.status, resp.text[:200])
    data = resp.json({}) or {}
    # 企业微信/钉钉/飞书都会在 200 里带错误码
    if isinstance(data, dict):
        if data.get('errcode') not in (None, 0):
            return False, '渠道返回错误: %s %s' % (data.get('errcode'), data.get('errmsg'))
        if data.get('code') not in (None, 0):
            return False, '渠道返回错误: %s %s' % (data.get('code'), data.get('msg'))
    return True, '已推送'


def send_mail(cfg, subject, body, receivers=None, html=False, timeout=15):
    """SMTP 发信；cfg 见 DEFAULT_CONFIG['mail']"""
    cfg = cfg or {}
    if not cfg.get('enabled', True):
        return False, '邮件渠道未启用'
    host = compat.safe_str(cfg.get('host')).strip()
    if not host:
        return False, '未配置 SMTP 服务器'
    port = compat.to_int(cfg.get('port'), 465, 1, 65535)
    sender = compat.safe_str(cfg.get('sender') or cfg.get('user')).strip()
    to_list = receivers or cfg.get('receivers') or []
    if isinstance(to_list, compat.string_types):
        to_list = [x.strip() for x in to_list.replace(';', ',').split(',') if x.strip()]
    if not to_list:
        return False, '未配置收件人'
    subtype = 'html' if html else 'plain'
    from email.mime.text import MIMEText
    from email.header import Header
    from email.utils import formatdate
    msg = MIMEText(body, subtype, 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = sender
    msg['To'] = ', '.join(to_list)
    msg['Date'] = formatdate(localtime=True)
    server = None
    try:
        if cfg.get('ssl', True):
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            try:
                server.starttls()
            except Exception:
                pass
        if cfg.get('user'):
            server.login(cfg['user'], cfg.get('password', ''))
        server.sendmail(sender, to_list, msg.as_string())
        return True, '已发送至 %s' % ', '.join(to_list)
    except Exception as e:                                    # noqa: BLE001
        return False, '发送失败: %s' % compat.safe_str(e)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


class Notifier(object):
    """统一通知入口：按事件类型决定推送到哪些渠道"""

    def __init__(self, path=None, config=None):
        self.store = JsonStore(path, config or dict(DEFAULT_CONFIG), file_mode=0o600)

    # ------------------------------------------------------------ 配置
    def config(self):
        data = self.store.load()
        for key, value in DEFAULT_CONFIG.items():
            data.setdefault(key, value if not isinstance(value, dict) else dict(value))
        return data

    def update(self, data):
        def _mut(cfg):
            for key in ('channels', 'mail', 'events'):
                if key in data:
                    if key == 'mail' and isinstance(data['mail'], dict):
                        cfg.setdefault('mail', {}).update(data['mail'])
                    else:
                        cfg[key] = data[key]
            return cfg
        return self.store.update(_mut)

    def public_config(self):
        cfg = self.config()
        mail = dict(cfg.get('mail') or {})
        if mail.get('password'):
            mail['password_set'] = True
            mail['password'] = '******'
        return {'channels': cfg.get('channels') or [], 'mail': mail,
                'events': cfg.get('events') or {}, 'kinds': KINDS}

    # ------------------------------------------------------------ 推送
    def send(self, title, text, event=None, html=False):
        cfg = self.config()
        events = cfg.get('events') or {}
        if event and not events.get(event, True):
            return {'sent': 0, 'results': [{'channel': '事件已关闭', 'ok': False}]}
        results = []
        sent = 0
        for channel in cfg.get('channels') or []:
            if not channel.get('enabled', True):
                continue
            ok, msg = send_webhook(channel.get('url'), title, text, channel.get('kind'))
            results.append({'channel': channel.get('name') or channel.get('kind'), 'ok': ok,
                            'message': msg})
            if ok:
                sent += 1
        mail = cfg.get('mail') or {}
        if mail.get('enabled'):
            ok, msg = send_mail(mail, title, text, html=html)
            results.append({'channel': '邮件', 'ok': ok, 'message': msg})
            if ok:
                sent += 1
        return {'sent': sent, 'results': results}

    def test(self, channel_id=None):
        """渠道连通性自测"""
        cfg = self.config()
        out = []
        for channel in cfg.get('channels') or []:
            if channel_id and channel.get('id') != channel_id:
                continue
            ok, msg = send_webhook(channel.get('url'), '运维平台连通性测试',
                                   '这是一条测试消息，收到即表示渠道配置正确。',
                                   channel.get('kind'))
            out.append({'name': channel.get('name'), 'ok': ok, 'message': msg})
        mail = cfg.get('mail') or {}
        if mail.get('enabled') and not channel_id:
            ok, msg = send_mail(mail, '运维平台连通性测试',
                                '这是一条测试邮件，收到即表示 SMTP 配置正确。')
            out.append({'name': '邮件', 'ok': ok, 'message': msg})
        return out
