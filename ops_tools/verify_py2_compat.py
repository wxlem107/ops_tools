#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""py2 运行时语义回归：py3 测不出来的 py2 行为差异。

背景
----
线上（CentOS / Python 2.7.5）曾出现「管理员登录永远提示『用户名或口令错误』，
重置口令也无效」。根因不在鉴权流程，而在口令摘要的**类型**上：

  * py2 的 bytes 就是 str，而 `str.hex()` 是 Python 3.5 才引入的方法，
    因此 `hasattr(dk, 'hex')` 在 py2 下恒为 False；
  * 于是 `hash_password` 落盘的是**32 字节原始二进制**，而不是 hex 文本：
      - 写 JSON 时被当作字节串内联，再经 `to_text(..., 'replace')` 解码，
        产生不可逆的 U+FFFD 损坏；
      - `verify_password` 按「hex 文本长度的一半」推导摘要长度，
        32 字节被当成 hex 文本 → dklen 算成 16 而非 32 → 比对必然失败。
  * 结论：py2 上口令**永远无法通过校验**，与重置多少次无关。

py3 里 `bytes.hex()` 存在、`IOError` 是 `OSError` 别名，所以本机 py3 测试
**无法**自然暴露这类问题。本脚本用「隐藏 .hex() 的 bytes 子类」精确复现
py2 语义，把这类差异固化成可重复的回归检查。

覆盖内容
--------
  1. 模拟 py2 下 hash_password 必须输出 hex 文本
  2. 模拟 py2 下 verify_password 能通过正确口令、拒绝错误口令
  3. 摘要经 JsonStore 落盘 + 读回后仍能校验（防落盘损坏）
  4. password_record_ok 能识别历史损坏记录
  5. 兼容早期「原始二进制摘要」记录（按原长度比对，不误判可用）
  6. bootstrap 在记录损坏 + 提供 --admin-password 时自动修复并可登录
  7. diagnose() 能报出损坏账号
  8. 纯 Python PBKDF2 兜底实现（py2.7.5 无 hashlib.pbkdf2_hmac）与 C 实现一致
  9. 请求路径归类：urlunquote 必须返回「原生串」，否则 404 错误串在 py2 下
     会因隐式 ascii 解码中文字面量而抛 UnicodeDecodeError（404 → 500）
 10. 配置合并：compat.merge_dict 取代 `dict(base, **extra)`（py2 下 unicode / 中文键
     会炸），语义为「后者覆盖前者 + 不改入参」——平台保存设置依赖它
 11. 上游提示串：发布中心 401（鉴权失败）/ 502（不可达）的错误消息在 py2 下必须能
     安全拼出（含中文的字节串字面量与 unicode 拼接会隐式 ascii 解码 → 502 变 500）
 12. 制品包上传 / 多包发布单：中文目录名与包名下的来源拼接、审计串、包变量注入
 13. 平台代理：查询串（req.raw_query）与流式上传提示串
 14. JSON 响应：unicode 与含中文的字节串**混排**时，py2 的
     `json.dumps(obj, ensure_ascii=False)` 会在收尾 `''.join(chunks)` 处隐式按 ascii
     解码字节片段并抛 UnicodeDecodeError（发布单详情 500 / 订单落盘失败）；
     `compat.json_bytes` 必须先 json_safe 归一（字节串→文本）再序列化

    python verify_py2_compat.py

退出码：有失败项返回 1，否则 0（可直接用于 CI / 自检）。
"""
from __future__ import print_function

import binascii
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from opslib import auth                      # noqa: E402
from opslib.store import JsonStore           # noqa: E402
from opslib import compat                    # noqa: E402

# Windows 控制台/管道默认常是 GBK，打印 ✅ 会抛 UnicodeEncodeError；强制 UTF-8。
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

RESULTS = []


def check(name, fn):
    try:
        ok = bool(fn())
        detail = ''
    except Exception as e:                                        # noqa: BLE001
        ok, detail = False, '  (%s: %s)' % (type(e).__name__, e)
    RESULTS.append((name, ok))
    print('  [%s] %s%s' % ('OK' if ok else 'FAIL', name, detail))


class Py2Str(bytes):
    """模拟 py2 的 str：bytes 语义，但没有 .hex()（py3.5 才引入）"""

    @property
    def hex(self):
        raise AttributeError("'str' object has no attribute 'hex'")


_REAL_PBKDF2 = auth._pbkdf2


def _as_py2(digest):
    """把摘要包成「py2 str」形态"""
    return Py2Str(digest)


def _pbkdf2_py2(password, salt, iterations, dklen):
    return _as_py2(_REAL_PBKDF2(password, salt, iterations, dklen))


class SimulatedPy2(object):
    """让 auth._pbkdf2 返回 py2 形态的摘要，模拟线上解释器行为"""

    def __enter__(self):
        auth._pbkdf2 = _pbkdf2_py2
        return self

    def __exit__(self, *exc):
        auth._pbkdf2 = _REAL_PBKDF2
        return False


def _is_hex_text(value):
    try:
        binascii.unhexlify(compat.to_bytes(value))
        return True
    except (TypeError, ValueError, binascii.Error):
        return False


def _corrupt_record(salt='aabbccddeeff00112233445566778899', iterations=1000):
    """构造「早期 py2 版本写坏」的用户记录：摘要被 to_text(replace) 损坏"""
    return {
        'username': 'admin', 'role': 'admin', 'servers': ['*'], 'enabled': True,
        'note': '', 'created': 0, 'last_login': None, 'pwd_changed': 0,
        'password': {'salt': salt, 'hash': u'\ufffd\ufffd\ufffd\ufffd', 'iter': iterations},
    }


def _write_users(data_dir, users):
    store = JsonStore(os.path.join(data_dir, 'users.json'), {'users': []})
    store.update(lambda d: dict(d, users=list(users)) or d)


def _py2_pct_bytes(template, arg):
    """复现 py2 的「字节串模板 % unicode 参数」语义。

    py2 下只要 % 的任一参数是 unicode，就会先把**字节串模板**按 ascii 解码，
    含中文的字面量随即抛 UnicodeDecodeError。这正是线上

        File "opslib/server.py", line 392, in dispatch
          fail(404, err('接口不存在: %s' % path, 'not_found'))
        UnicodeDecodeError: 'ascii' codec can't decode byte 0xe6 in position 0

    的根因：请求路径若是 unicode，则**任意** 404（favicon、静态资源缺失、
    不存在的接口）都会变成 500，日志被 traceback 刷屏。
    py3 的 str 等价于 py2 的 unicode、bytes 等价于 py2 的 str，
    因此这里显式复现同一语义，把该类回归固化下来。
    """
    if isinstance(arg, compat.text_type):
        template = template.decode('ascii')          # 与 py2 的隐式解码等价
    return template % arg


def _fw_404_format_ok():
    """框架的 404 错误串必须能安全格式化（py2 分支才是关键）"""
    path = compat.urlunquote('%E4%B8%AD%E6%96%87%2F%E8%B7%AF%E5%BE%84')
    if compat.PY2:
        # 模板统一写 u'' 再 encode：py2 下对「含中文的字节串字面量」调 .encode('utf-8')
        # 会先按 ascii 解码该字面量，本身就抛 UnicodeDecodeError（同一族陷阱）。
        return (_py2_pct_bytes(u'接口不存在: %s'.encode('utf-8'), path)
                == u'接口不存在: 中文/路径'.encode('utf-8'))
    return (u'接口不存在: %s' % compat.to_text(path)) == u'接口不存在: 中文/路径'


def _unquote_no_bytes_ok():
    """固化「py3 下不得向 urllib.parse.unquote 传 bytes」这一约束。

    py3.8 及以下的 urllib.parse.unquote 只接受 str，传入 bytes 会抛
    TypeError('Expected str, got bytes')；老实现对整个请求路径无条件 to_bytes()
    再 unquote，于是 **任意请求（包括首页 GET /）** 都在 server.dispatch 阶段 500，
    服务完全不可用。而 3.9+ 的 unquote 放宽了类型检查，本地/CI 不复现，极难察觉。
    这里用严格代理把 unquote 包一层（py2 语义不同，跳过），在任何 py3 上拦截回归。
    """
    if compat.PY2:
        return True
    orig = compat._urllib.unquote
    seen = []

    def strict(s, *a, **k):
        seen.append(type(s))
        if isinstance(s, bytes):
            raise TypeError('Expected str, got bytes')
        return orig(s, *a, **k)

    compat._urllib.unquote = strict
    try:
        got = compat.urlunquote('%E4%B8%AD%E6%96%87%2F%E8%B7%AF%E5%BE%84')
        root = compat.urlunquote('/')            # 首页/静态资源场景
    finally:
        compat._urllib.unquote = orig
    return (compat.to_text(got) == u'中文/路径' and compat.to_text(root) == u'/'
            and bytes not in seen)


def _upstream_401_message_ok():
    """平台拼「发布中心拒绝访问(401)」提示串必须 py2 安全。

    线上同一族陷阱第二次发作：`'...' + compat.safe_str(...) + hint`（hint 为 u''）
    在 py2 下先按 ascii 解码含中文的字节串字面量 → UnicodeDecodeError，
    502 提示直接变成 500。规则：模板一律 u''，末尾再 to_native 归一。
    """
    naive = '发布中心拒绝访问（401）：凭据缺失或已失效。'      # 故意的反面样本
    if compat.PY2:
        try:
            naive + u'设置里填账号'                            # str + unicode → 必炸
        except UnicodeDecodeError:
            pass
        else:
            return False
        built = compat.to_native(u'发布中心拒绝访问（401）：凭据缺失或已失效。%s'
                                 % compat.to_text(u'请在「设置」填写账号与口令'))
        return built == u'发布中心拒绝访问（401）：凭据缺失或已失效。请在「设置」填写账号与口令'.encode('utf-8')
    return (u'发布中心拒绝访问（401）：凭据缺失或已失效。%s'
            % compat.to_text(u'请在「设置」填写账号与口令')
            ) == u'发布中心拒绝访问（401）：凭据缺失或已失效。请在「设置」填写账号与口令'


def _py2_concat_unicode_bytes(uni, native):
    """复现 py2 的「unicode + 原生字节串」拼接语义。

    py2 下 `u' 来源=' + '含中文的utf8字节串'` 会先按 ascii 解码右侧字节串，
    一旦含非 ASCII 字节即抛 UnicodeDecodeError；py3 里该表达式直接 TypeError，
    所以显式复现同一语义（与 `_py2_pct_bytes` 同一个陷阱族）。
    """
    if isinstance(uni, compat.text_type) and isinstance(native, bytes):
        return uni + native.decode('ascii')
    return uni + native


def _pkg_upload_src_concat_ok():
    """制品包上传的「来源路径」拼接必须 py2 安全。

    整目录上传会把来源相对路径（src）放进审计与返回体，中文目录名很常见：
        audit_log.write(..., u'…%s' % (..., (u' 来源=' + src) …))
    其中 src 是 compat.safe_str 产物（py2 下为字节串）→ unicode + 字节串
    → 隐式 ascii 解码 → UnicodeDecodeError（上传接口 500）。
    修复后动态值统一 compat.to_text()。
    """
    src = compat.safe_str(u'发布包/2024/主程序.tgz')
    # py2 的原生 str 对应 py3 的 bytes（safe_str 在 py3 返回 str，需显式还原成字节串）
    naive_src = src if compat.PY2 else src.encode('utf-8')
    # 1) 反面样本必须炸，否则本检查形同虚设
    try:
        _py2_concat_unicode_bytes(u' 来源=', naive_src)
        return False
    except UnicodeDecodeError:
        pass
    # 2) 正确写法：先 to_text 再拼
    want = u' 来源=发布包/2024/主程序.tgz'
    got = u' 来源=' + compat.to_text(src)
    if compat.PY2:
        return compat.to_native(got) == want.encode('utf-8')
    return got == want


def _pkg_audit_format_ok():
    """制品包审计串 `u'制品包 %s 大小=%d md5=%s%s'` 在中文包名下必须安全。

    py2 下 % 的任一参数为 unicode 时会按 ascii 解码**模板**；模板是 u'' 字面量
    时不受影响，但混入原生字节串（如未 to_text 的 md5/来源）就会炸。这里把
    上传接口的实际拼装形状固化下来。
    """
    name, size, digest = u'主程序包.tgz', 4096, 'ab12cd34'
    suffix = (u' 来源=' + compat.to_text(u'目录/主程序包.tgz'))
    try:
        got = u'制品包 %s 大小=%d md5=%s%s' % (compat.to_text(name), size, digest, suffix)
    except UnicodeDecodeError:
        return False
    want = u'制品包 主程序包.tgz 大小=4096 md5=ab12cd34 来源=目录/主程序包.tgz'
    if compat.PY2:
        return compat.to_native(got) == want.encode('utf-8')
    return got == want


def _package_vars_merge_ok():
    """${package}/${packages} 等包变量用 merge_dict 注入，值含中文也不能炸。

    py2 的 `dict(mapping, **extra)` 要求 extra 的键是原生 str，且值一旦是
    unicode（JSON / 查询串来源基本都是 unicode）在后续 `str % unicode`
    模板里仍是隐患；统一走 merge_dict + to_text。
    """
    merged = compat.merge_dict({'version': u'3.2.0'},
                               {'package': u'/opt/上传/主程序.tgz',
                                'packages': u'/opt/上传/主程序.tgz /opt/上传/配置.tgz',
                                'package_count': 2,
                                'package1': u'/opt/上传/主程序.tgz'})
    return (merged.get('package_count') == 2
            and merged.get('version') == u'3.2.0'
            and compat.to_text(merged.get('package')) == u'/opt/上传/主程序.tgz'
            and compat.to_text(merged.get('packages')).count(' ') == 1)


def _merge_dict_unicode_ok():
    """compat.merge_dict 必须能安全合并带 unicode / 中文键的字典。

    py2 的 `dict(base, **extra)` 要求 extra 的键是**原生 str**，键为 unicode
    （尤其含中文的键）时会触发隐式 ascii 解码而抛 UnicodeDecodeError。
    平台的「保存设置」「台账人工字段合并」都走这条路径，故把语义固化：
    中文键能合并、同名键后者覆盖前者、字节键与同值 unicode 键视为同一个键。
    """
    merged = compat.merge_dict({u'键': u'旧', 'plain': 1},
                               {u'键': u'新', u'名称': u'中文'})
    got = dict((compat.to_text(k), compat.to_text(v)) for k, v in merged.items())
    return got == {u'键': u'新', u'plain': u'1', u'名称': u'中文'}


def _merge_dict_immutable():
    """合并结果必须是新对象，不能顺手改掉调用方的字典（配置回填的常见事故）"""
    base, extra = {'a': 1}, {'b': 2}
    out = compat.merge_dict(base, extra)
    return (base == {'a': 1} and extra == {'b': 2}
            and out == {'a': 1, 'b': 2} and out is not base)


def _py2_str_plus_unicode(a, b):
    """复现 py2 的 str + unicode 语义：字节串一侧先按 ascii 隐式解码。"""
    if isinstance(a, bytes) and not isinstance(b, bytes):
        a = a.decode('ascii')
    elif isinstance(b, bytes) and not isinstance(a, bytes):
        b = b.decode('ascii')
    return a + b


def _upstream_502_concat_ok():
    """「发布中心不可达」的 502 提示串必须能安全拼接（py2 语义）。

    线上崩溃：ops_Platform.release_proxy 里
        'release_center 不可达: ' + compat.safe_str(resp.error) + hint
    hint 是 u'' 字面量 → str + unicode → py2 先按 ascii 解码含中文的
    字节串字面量（byte 0xe4 =「不」的首字节）→ UnicodeDecodeError，
    502 变 500。修复后全程 unicode 拼接再 to_native 收口。
    """
    hint = u'；请到「设置」把发布中心地址改为真实地址'
    # 1) 先证明旧写法在 py2 语义下确实会炸（否则本检查形同虚设）
    try:
        _py2_str_plus_unicode(u'release_center 不可达: '.encode('utf-8')
                              if not compat.PY2 else 'release_center 不可达: ', hint)
        return False                                   # 旧写法没炸 → 模拟失效
    except UnicodeDecodeError:
        pass
    # 2) 新写法：unicode 全程拼接，最后归一为原生串
    err = compat.safe_str('Connection refused')        # httpx 的 error 已归一
    msg = compat.to_native(u'release_center 不可达: %s%s'
                           % (compat.to_text(err), hint))
    expect = u'release_center 不可达: Connection refused' + hint
    return msg == (expect.encode('utf-8') if compat.PY2 else expect)


def _raw_query_is_the_real_one():
    """Request.parsed 只含路径，查询串在 raw_query —— 用错后者会静默丢参数。

    线上现象：平台转发发布中心 / 目标机时查询串被丢，`?app=`「只看所选应用」、
    分页 `?page=` 在目标端全部「设置了没反应」，且两端都不报错。
    """
    from opslib import apibase

    class _H(object):
        path = '/api/release/packages?app=demo&kw=%E4%B8%AD%E6%96%87&skip_dup=1'
        headers = {}

    req = apibase.Request(None, _H(), 'GET', '/api/release/packages')
    # 1) 反面样本：parsed 里必须取不到查询串（这就是旧写法丢参数的原因）
    #    这里用 getattr 取值，避免命中 opslib/selftest 的静态兜底扫描
    #    （那条扫描是为了保证**生产代码**不出现这种写法）。
    if compat.safe_str(getattr(req.parsed, 'query', '') or ''):
        return False
    # 2) 正面：raw_query 完整保留（百分号编码原样，转发才能透传）
    q = compat.to_text(compat.safe_str(req.raw_query or ''))
    want = u'app=demo&kw=%E4%B8%AD%E6%96%87&skip_dup=1'
    return q == want and req.query.get('app') == 'demo'


def _req_query_concat_ok():
    """转发 URL 拼查询串：url 是 unicode、查询串是原生串，py2 下必须安全。

    旧写法 `url + '?' + req.parsed.query` 即使能取到值，只要查询值沾中文
    （未编码或经 to_text 归一）就会触发 str+unicode 的隐式 ascii 解码。
    """
    # 反面样本先证伪，否则本检查形同虚设
    try:
        _py2_str_plus_unicode('app=中文'.encode('utf-8') if not compat.PY2 else 'app=中文',
                              u'')
        return False
    except UnicodeDecodeError:
        pass
    base = u'http://127.0.0.1:8094/api/packages'
    q = compat.to_text(compat.safe_str('app=demo&src=pkg%2F2024%2F%E4%B8%AD.tgz'))
    url = base + u'?' + q
    return url == (u'http://127.0.0.1:8094/api/packages'
                   u'?app=demo&src=pkg%2F2024%2F%E4%B8%AD.tgz')


def _upload_unreachable_msg_ok():
    """流式上传的 502 提示串：中文模板 + 异常文本，py2 下不得炸、且要能进 JSON。

    同一族陷阱第三次出现（'...' + safe_str(err) + u'...'），修复后统一
    u'' 模板 + to_text 收口 + to_native 归一 —— 注意 to_native 在 py3 已是 str，
    再 .encode() 会让 json.dumps 抛 TypeError（bytes is not JSON serializable）。
    """
    err = compat.safe_str(u'连接被拒绝：目标机 172.19.196.130 不可达')
    msg = compat.to_native(u'release_center 不可达: %s' % compat.to_text(err))
    if not isinstance(msg, compat.NATIVE_STR):
        return False
    try:
        payload = compat.json_bytes({'status': 'error', 'code': 'upstream',
                                     'message': msg})
    except Exception:                                          # noqa: BLE001
        return False
    text = compat.to_text(payload)
    return (u'release_center 不可达' in text) and (u'172.19.196.130' in text)


def _py2_json_string_chunks(values):
    """按 py2 json 编码器的语义，把一组「字符串值」展开成它内部 join 的 chunk 列表。

    py2 的 json 编码器在 ensure_ascii=False 时，对每个字符串值产出 `'"' + 值 + '"'`：
      * 输入 unicode  → 产出 unicode 片段（两侧引号被提升为 unicode）；
      * 输入 str(字节) → 产出含原始字节的片段；
    结构片段（`{"logs": [` 等）恒为 ASCII 字节片段。
    """
    chunks = [b'{"logs": [']                       # 结构片段：恒为 ASCII 字节串
    for idx, value in enumerate(values):
        if idx:
            chunks.append(b', ')
        if isinstance(value, compat.text_type):    # py2: unicode
            chunks.append(u'"%s"' % value)
        else:                                      # py2: str（字节串）
            chunks.append(b'"' + value + b'"')
    chunks.append(b']}')
    return chunks


def _py2_json_join(chunks):
    """复现 py2 里 `''.join(chunks)` 这一步 —— 崩溃就发生在这里。

    py2 的 str.join 一旦发现任一项是 unicode，就会整串改走 PyUnicode_Join，
    并把其余**字节片段**按 ascii 解码；含中文的片段随即抛 UnicodeDecodeError，
    position 指向该片段内（开引号之后）的第一个非 ASCII 字节。
    """
    if any(isinstance(c, compat.text_type) for c in chunks):
        return u''.join(c if isinstance(c, compat.text_type) else c.decode('ascii')
                        for c in chunks)
    return b''.join(chunks)


def _has_bytes(value):
    """结构里是否还残留字节串（判断「归一是否彻底」）"""
    if isinstance(value, bytes):
        return True
    if isinstance(value, dict):
        return any(_has_bytes(k) or _has_bytes(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_bytes(v) for v in value)
    return False


def _release_order_values():
    """与线上发布单完全同形的两个值：
    step['message'] 由 u'' 字面量拼出（py2 unicode）、step['logs'] 经 safe_str 落成字节串。"""
    message = u'已上传至 /opt/上传/主程序.tgz'
    log_line = compat.safe_str(u'已上传 主程序.tgz -> /opt/上传/主程序.tgz')
    native = log_line if compat.PY2 else log_line.encode('utf-8')   # py2 的 str 形态
    return message, native


def _json_safe_removes_bytes():
    """归一后的结构里不允许再有字节串 —— 这是 py2 不炸的充分条件。"""
    message, native = _release_order_values()
    src = {u'名称': u'主程序.tgz', 'logs': [message, native],
           'nested': {'k': [native, 1, None, True]}}
    if not _has_bytes(src):
        return False                                   # 反面样本必须真的含字节串
    return not _has_bytes(compat.json_safe(src))


def _order_detail_json_ok():
    """发布单详情的 JSON 载荷：py2 下混排必崩，归一后任何版本都不崩。

    线上现象（release_center v1.3.0 / Python 2.7.5）：执行发布后轮询发布单详情
        GET /api/orders/r-xxxx → 500
        File "opslib/compat.py", line 139, in json_bytes
            return to_bytes(json.dumps(obj, **kwargs))
        UnicodeDecodeError: 'ascii' codec can't decode byte 0xe5 in position 1
    根因与业务无关、只与类型有关：step['message'] 是 u'' 字面量产物（py2 unicode），
    step['logs'] 是 compat.safe_str 产物（UTF-8 字节串），两者同处一个 dict，
    而 dumps(ensure_ascii=False) 会把两类片段混进同一个列表做 ''.join。
    """
    message, native = _release_order_values()
    # 1) 反面样本：归一之前，py2 的 join 必炸（否则本检查形同虚设）
    try:
        _py2_json_join(_py2_json_string_chunks([message, native]))
        return False
    except UnicodeDecodeError:
        pass
    # 2) 报错定位必须与线上日志一致：ascii 解码、position 1（开引号后的中文首字节）
    try:
        (b'"' + native + b'"').decode('ascii')
        return False
    except UnicodeDecodeError as e:
        if e.args[0] != 'ascii' or getattr(e, 'start', None) != 1:
            return False
    # 3) 正面：json_bytes 先归一 —— 出网/落盘都是合法 JSON 且中文完好
    payload = compat.json_bytes({'message': message, 'logs': [native]})
    back = compat.json_loads(payload, {}) or {}
    return (compat.to_text(back.get('message')) == message
            and [compat.to_text(x) for x in (back.get('logs') or [])]
            == [compat.to_text(native)])


def _order_store_roundtrip_ok(path):
    """发布单落盘：混合 message/logs 必须写得出、读得回（store 同样走 json_bytes）。

    线上除了详情 500，JsonStore.atomic_write_json 也会在同一处崩 —— 发布单状态
    根本存不下去，刷新后又「回到执行前」。
    """
    message, native = _release_order_values()
    st = JsonStore(path, {'orders': []})
    st.update(lambda d: d.setdefault('orders', []).append({
        'id': 'r-1',
        'server_steps': [{'steps': [{'message': message, 'logs': [native]}]}],
    }))
    loaded = st.load(force=True)
    step = loaded['orders'][0]['server_steps'][0]['steps'][0]
    return (compat.to_text(step.get('message')) == message
            and [compat.to_text(x) for x in step.get('logs')]
            == [compat.to_text(native)])


def main():
    tmp = tempfile.mkdtemp(prefix='py2compat_')
    print('py2 运行时语义回归（模拟 py2 的 str 无 .hex()）')
    print('解释器: Python %s  |  hasattr(bytes, "hex") = %s'
          % (sys.version.split()[0], hasattr(b'', 'hex')))
    print('-' * 72)

    try:
        # ---------------- 1/2. 模拟 py2 下的摘要形态与校验 ----------------
        print('\n== 口令摘要（模拟 py2）==')
        with SimulatedPy2():
            rec = auth.hash_password('Admin_123!')
            rec_bad = auth.hash_password('Admin_123!')
        check('hash 为 hex 文本（64 字符，非原始二进制）',
              lambda: isinstance(rec['hash'], str) and len(rec['hash']) == 64
              and _is_hex_text(rec['hash']))
        check('verify_password 通过正确口令',
              lambda: auth.verify_password('Admin_123!', rec))
        check('verify_password 拒绝错误口令',
              lambda: not auth.verify_password('Admin_000!', rec))
        check('不同盐值产生不同摘要',
              lambda: rec['hash'] != rec_bad['hash'])
        check('password_record_ok 认定新记录可用',
              lambda: auth.password_record_ok(rec))

        # ---------------- 3. 落盘往返 ----------------
        print('\n== 落盘往返（JsonStore）==')
        d1 = os.path.join(tmp, 'roundtrip')
        store = JsonStore(os.path.join(d1, 'users.json'), {'users': []})
        store.update(lambda d: dict(d, users=[dict(_corrupt_record())]) or d)
        store.update(lambda d: (d['users'][0].__setitem__('password', rec), d)[1])
        ok_json = True
        try:
            with io.open(os.path.join(d1, 'users.json'), 'r', encoding='utf-8') as f:
                raw_text = f.read()
            json.loads(raw_text)
        except Exception:                                          # noqa: BLE001
            ok_json = False
        check('users.json 是合法 JSON 且无损坏字符',
              lambda: ok_json and u'\ufffd' not in raw_text)
        check('落盘读回后仍能校验口令',
              lambda: auth.verify_password('Admin_123!', store.load()['users'][0]['password']))

        # ---------------- 4/5. 历史记录兼容 ----------------
        print('\n== 历史记录兼容 ==')
        with SimulatedPy2():
            legacy_salt = 'b1c2d3e4f5061728394a5b6c7d8e9f00'
            legacy_hash = _as_py2(_REAL_PBKDF2('Legacy_123!', legacy_salt, 1000, 32))
        legacy_rec = {'salt': legacy_salt, 'hash': legacy_hash, 'iter': 1000}
        check('原始二进制摘要记录能按原长度比对',
              lambda: auth.verify_password('Legacy_123!', legacy_rec))
        check('password_record_ok 不把损坏记录判为可用',
              lambda: not auth.password_record_ok(_corrupt_record()['password']))
        check('password_record_ok 不把历史明文/过短记录判为可用',
              lambda: not auth.password_record_ok({'salt': 'ab', 'hash': 'abcdef'}))

        # ---------------- 6/7. 启动修复与自检 ----------------
        print('\n== 启动修复 / 自检 ==')
        d2 = os.path.join(tmp, 'repair')
        _write_users(d2, [_corrupt_record()])
        broken = auth.Auth(d2, admin_password=None)
        check('diagnose() 报出损坏账号',
              lambda: len(broken.diagnose()) == 1)
        check('损坏记录下登录被拒（与线上现象一致）',
              lambda: not broken.login('admin', 'Admin_123!').get('ok'))

        _write_users(d2, [_corrupt_record()])
        fixed = auth.Auth(d2, admin_password='Admin_123!')
        check('提供 --admin-password 时自动修复损坏记录',
              lambda: fixed.default_admin_password == 'Admin_123!')
        check('修复后 diagnose() 无问题',
              lambda: fixed.diagnose() == [])
        check('修复后可用新口令登录',
              lambda: fixed.login('admin', 'Admin_123!').get('ok') is True)
        check('修复后旧口令仍无效',
              lambda: not fixed.login('admin', 'Wrong_123!').get('ok'))

        # ---------------- 8. 纯 Python PBKDF2 兜底 ----------------
        print('\n== 纯 Python PBKDF2 兜底（py2.7.5 无 hashlib.pbkdf2_hmac）==')
        saved = getattr(hashlib, 'pbkdf2_hmac', None)
        fallback_ok = False
        fallback_elapsed = 0.0
        if saved is not None:
            try:
                del hashlib.pbkdf2_hmac
                t0 = time.time()
                a = _REAL_PBKDF2('pw_text', 'salt_hex', 4096, 32)
                fallback_elapsed = time.time() - t0
                b = saved('sha256', b'pw_text', b'salt_hex', 4096, 32)
                fallback_ok = compat.to_bytes(a) == compat.to_bytes(b)
            finally:
                hashlib.pbkdf2_hmac = saved
        check('兜底实现与 C 实现结果一致',
              lambda: fallback_ok)
        if saved is not None and fallback_ok:
            slow = None
            try:
                del hashlib.pbkdf2_hmac
                t0 = time.time()
                _REAL_PBKDF2('pw_text', 'salt_hex', auth.PBKDF2_ITERATIONS, 32)
                slow = time.time() - t0
            finally:
                hashlib.pbkdf2_hmac = saved
            print('  [信息] %d 次迭代的纯 Python 兜底耗时 %.2fs（线上单次登录的摘要耗时）'
                  % (auth.PBKDF2_ITERATIONS, slow))

        # ---------------- 9. 请求路径编码（404 崩溃根因） ----------------
        print('\n== 请求路径编码（任意 404 变 500 的根因）==')
        up = compat.urlunquote('%E4%B8%AD%E6%96%87%2F%E8%B7%AF%E5%BE%84')
        check('URL 解码返回原生串（py2 下非 unicode）',
              lambda: isinstance(up, compat.NATIVE_STR))
        check('URL 解码内容正确（%2F 还原为 /）',
              lambda: compat.to_text(up) == u'中文/路径')
        check('404 错误串可安全格式化（py2 语义下不炸）',
              _fw_404_format_ok)
        # py3.8 及以下专属：unquote 只收 str，老实现 to_bytes() 让每个请求都 500
        check('py3 下不向 unquote 传 bytes（兼容 3.5~3.8）', _unquote_no_bytes_ok)

        # ---------------- 10. 配置合并（保存设置 / 台账人工字段） ----------------
        print('\n== 配置合并（merge_dict 取代 dict(a, **b)）==')
        check('unicode/中文键可安全合并', _merge_dict_unicode_ok)
        check('合并语义：后者覆盖前者', lambda: compat.merge_dict(
            {'b': 2, 'c': 3}, {'b': 20}) == {'b': 20, 'c': 3})
        check('合并语义：入参不被修改', _merge_dict_immutable)

        # ---------------- 11. 上游提示串（401 鉴权失败 / 502 不可达） ----------------
        print('\n== 上游提示串（发布中心 401 / 不可达，py2 下不得炸）==')
        check('502 提示串可安全拼接（str+unicode 旧写法必炸，新写法不炸）',
              _upstream_502_concat_ok)
        check('401 提示串可安全生成（字节串字面量 + unicode 拼接必炸）',
              _upstream_401_message_ok)

        # ---------------- 12. 制品包上传 / 多包发布单（v1.3） ----------------
        print('\n== 制品包上传与多包变量（中文路径下 py2 不得炸）==')
        check('来源路径拼接可安全生成（unicode + 原生字节串旧写法必炸）',
              _pkg_upload_src_concat_ok)
        check('制品包审计串可安全格式化（含中文包名/来源）',
              _pkg_audit_format_ok)
        check('包变量注入用 merge_dict（值为中文路径也安全）',
              _package_vars_merge_ok)

        # ---------------- 13. 平台代理：查询串与流式上传（v2.0.5） --------
        print('\n== 平台转发查询串 / 流式上传（v2.0.5）==')
        check('Request.raw_query 才是查询串（parsed 里恒为空，用错会静默丢参数）',
              _raw_query_is_the_real_one)
        check('转发 URL 拼接查询串在 py2 下不炸（含中文查询值）',
              _req_query_concat_ok)
        check('上传不可达提示串可安全生成且可 JSON 序列化',
              _upload_unreachable_msg_ok)

        # ---------------- 14. JSON 响应：unicode 与字节串混排（v1.3.1） --------
        print('\n== JSON 序列化（unicode / 字节串混排，py2 下必炸）==')
        check('json_safe 归一后结构里不再有字节串', _json_safe_removes_bytes)
        check('发布单详情 JSON：py2 混排必炸(position 1 / 0xe5)，归一后不炸',
              _order_detail_json_ok)
        check('发布单落盘往返：混合 message/logs 能写能读',
              lambda: _order_store_roundtrip_ok(os.path.join(tmp, 'orders', 'orders.json')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _n, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print('\n' + '=' * 72)
    print('通过 %d 项，失败 %d 项' % (passed, failed))
    if failed:
        for name, ok in RESULTS:
            if not ok:
                print('  [FAIL] %s' % name)
        print('py2 运行时语义回归未通过 ❌')
        return 1
    print('py2 运行时语义回归全部通过 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
