# opslib 公共层使用说明

> 位置：`ops_tools/opslib/`。所有工具（`tools/*.py`、`platform_console/*.py`）统一从它引入基础能力，
> **不要再各自复制兼容层**。运行环境：Python 2.7.5+ / 3.7.5+，仅标准库。

自检：`python opslib/selftest.py`（114 项检查，全部通过才算公共层正常）。

---

## 1. 引入方式

脚本位于 `opslib/` 的下一级目录（`tools/` 或 `platform_console/`）时，在文件顶部写：

```python
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from opslib import compat, server, apibase, auth as opsauth, sse, httpx, probe, audit, task, notify, crypto
from opslib.apibase import ok, err, ApiError
```

---

## 2. 最小可运行服务骨架

```python
app = server.HttpApp(
    name='web_shell', version='1.2.0',
    base_dir=os.path.dirname(os.path.abspath(__file__)),
    template_dir='templates',                 # 页面模板目录（可给多个候选）
    data_dir=os.path.join(BASE_DIR, 'data'),  # 数据落盘目录
    auth=opsauth.Auth(data_dir, legacy_token=ARGS.token, require_login=bool(ARGS.token)),
    desc='浏览器远程命令执行',
)

@app.get('/api/ping', auth=False, summary='存活探测')
def ping(req):
    return ok({'version': app.version})

@app.post('/api/exec', perm='cmd.exec', summary='执行命令')
def execute(req):
    data = req.json()                     # 自动解析 JSON，非法时抛 ApiError
    cmd = data.get('cmd', '').strip()
    if not cmd:
        raise ApiError('命令不能为空', 400, 'bad_request')
    return ok({'output': '...'})

@app.add_page('/', 'web_shell.html')      # 首页：渲染 templates/web_shell.html
if __name__ == '__main__':
    app.run('0.0.0.0', 9100)
```

### 路由处理器约定

处理器签名 `func(req)`，返回值三种：

| 返回 | 效果 |
|---|---|
| `dict` | 自动 JSON 序列化，HTTP 200 |
| `str` | 作为 HTML 返回 |
| `None` | 表示已自行写响应（文件流 / SSE / 自定义状态码） |

抛 `ApiError(message, status=400, code='...')` → 自动转成 `{"status":"error","code":...,"message":...}`。

### 注册参数

```python
@app.get('/api/x', perm='server.view', auth=True, summary='说明文字')
@app.post('/api/y', perm='cmd.exec', summary='…')
```

- `auth=False`：免登录（`/api/ping`、`/api/health`、`/api/openapi.json`、页面）
- `perm`：需要的权限点，见 `opsauth.PERMS`；`None` 表示只要登录即可
- `summary`：会出现在 `/api/openapi.json` 里

### 权限点

`platform.view`、`server.view`、`server.edit`、`file.read`、`file.write`、`file.delete`、
`cmd.exec`、`release.run`、`task.run`、`cmdb.edit`、`audit.view`、`user.admin`、`settings.edit`

角色：`admin`（`*`）、`ops`（除 user.admin 外全部）、`viewer`（只读四项）。

---

## 3. Request 对象速查

```python
req.user            # 当前用户名（未登录为 ''）
req.role            # admin / ops / viewer
req.session         # {'username','role','servers',...}
req.client_ip       # 已处理 X-Forwarded-For
req.params          # 路径参数，如 /api/servers/<name> → {'name': 'web-01'}
req.query / req.q('k', 'default')          # 查询串（单值）
req.query_list                             # 查询串（多值 dict→list）
req.qi('page', 1, 1, 1000)                 # 取整数并夹紧范围
req.qb('force', False)                     # 取布尔
req.json() / req.raw_body() / req.form()   # 请求体
req.respond(status, body, ctype, headers)  # 自定义响应
req.json_out(obj, status=200)
req.text_out('...', ctype='text/plain; charset=utf-8')
req.stream(iterator, content_type, extra_headers)   # 分块流式输出（zip / 大文件）
req.sse()                                  # 升级为 SSE 流
```

---

## 4. SSE 实时推送

```python
@app.get('/api/log/stream', perm='server.view', summary='日志实时流')
def log_stream(req):
    stream = req.sse()
    stream.ready()
    proc = compat.popen_stream(['tail', '-F', '/var/log/app.log'])
    try:
        for line in iter(proc.stdout.readline, b''):
            if stream.closed:
                break
            stream.send(compat.to_text(line), event='log')
    finally:
        compat.kill_process_tree(proc)
        stream.close()
```

一次性把生成器泵完（自动插心跳、自动结束）：

```python
stream.pump(my_generator(), heartbeat=15, event='metrics')
```

事件总线（一对多广播，适合监控曲线 / 任务进度）：

```python
from opslib.sse import BUS, stream_events

BUS.publish('metrics', {'server': 'web-01', 'cpu': 12.3})      # 发布方
@app.get('/api/events', auth=True)
def events(req):
    stream_events(req, ['metrics', 'task', 'alert'])            # 订阅方
```

---

## 5. 异步任务

```python
tm = task.TaskManager(os.path.join(data_dir, 'tasks.json'), max_workers=4, bus=BUS)

def job(ctx):
    ctx.progress(10, '准备中')
    ctx.log('开始拷贝文件')
    for i in range(1, 11):
        ctx.check_canceled()          # 被取消时抛 TaskCanceled
        ctx.progress(10 + i * 8)
        time.sleep(0.1)
    ctx.set(rows=42)                  # 写进 result
    return {'ok': True}

t = tm.submit('分发文件', 'distribute', job, params={'names': ['web-01']},
              user=req.user, target='web-01')
# tm.get(id) / tm.list() / tm.cancel(id) / tm.logs(id) / tm.wait(id, timeout)
```

进度会自动发布到 `BUS` 的 `task` 与 `task:<id>` 主题。

---

## 6. 审计

```python
al = audit.AuditLog(os.path.join(data_dir, 'audit.jsonl'))

al.write(req.user, req.client_ip, 'cmd.exec', target='web-01', detail='uptime', result='ok')
al.write_req(req, 'file.delete', target='web-01', detail='/opt/app/x.jar')   # 便捷写法
al.query(user='', action='', keyword='', start=None, end=None, limit=100, offset=0)
al.stats(days=7)          # 概览：总量/失败数/按动作/按人/按天
al.verify()               # (ok, message, checked, broken_seq) —— 校验哈希链是否被篡改
al.export_rows()          # 导出二维数组（可直接转 CSV）
```

有副作用的操作（写文件、删文件、执行命令、发布、改配置、用户管理）**必须**留一条审计。

---

## 7. 探针与指标

```python
info = probe.sys_info()       # 资产画像：主机名/OS/内核/CPU/内存/磁盘/序列号/虚拟化/Java…
m = probe.metrics()           # 实时指标；连续调用（间隔>0.2s）可算出速率与瞬时 CPU
probe.top_processes(10, sort='cpu')     # sort='cpu' | 'mem'
probe.process_list() / probe.find_process('java') / probe.kill_process(pid, force=False)
probe.listen_ports()          # [{proto, addr, port, pid, process, inode}]
probe.disk_io()               # [{device, read_bytes, write_bytes, …}]
probe.health_rules(m)         # 把指标转成异常项列表（巡检/告警复用）
```

---

## 8. 远程调用与转发

```python
# 目标机服务地址统一由 server 条目提供
url = httpx.join_url(srv['host'], srv.get('upload_port', 8080), '/api/status')
resp = httpx.DEFAULT.get(url, headers={'X-Auth-Token': token}, timeout=5)
resp.status / resp.ok / resp.json() / resp.text / resp.header('Content-Type')

# 流式转发（大文件上传，不在内存里堆整份 body）
httpx.DEFAULT.request(method, url, headers=fwd, src=req.handler.rfile, src_len=length)

# 下载到本地
httpx.DEFAULT.download(url, dest, headers=..., on_progress=lambda got, total: ...)
```

`httpx.Client` 自带连接池复用（含带 body 的分片请求），池内连接取用前做存活校验。

---

## 9. 加密与保险箱

```python
blob = crypto.encrypt('明文', '主口令')          # -> Base64 文本
plain = crypto.decrypt(blob, '主口令')           # 失败抛 ValueError（口令错/被篡改）

vault = crypto.Vault(data_dir)                   # 主口令来源：参数 > OPS_VAULT_KEY > .vault_key
safe = vault.seal({'host': '1.1.1.1', 'password': 'p@ss'}, ['password'])
raw = vault.open(safe, ['password'])             # 读出来才是明文
crypto.mask('abcdefgh')                          # 'ab***gh' 前端展示用
```

---

## 10. 通知

```python
nt = notify.Notifier(os.path.join(data_dir, 'notify.json'))
nt.send('发布完成', '应用 app-web 已在 web-01 升级到 v1.2.3', event='release')
nt.test()                                        # 渠道自测
```

---

## 11. 跨版本注意事项

写新代码时**不要**使用：f-string、`os.replace`、`shutil.which`、`shutil.disk_usage`、
`re.fullmatch`、`subprocess.DEVNULL`、`Popen.wait(timeout=)`、`Thread(daemon=True)`、
`concurrent.futures`、字典解包 `{**a}`、`pathlib`、`urllib.request`（直接用 `opslib.httpx`）。

对应的替代品全部在 `opslib.compat`：`os_replace` / `which` / `disk_usage` / `fullmatch` /
`DEVNULL` / `run_cmd` / `start_daemon` / `parallel_map` / `merge_dict`。

其中 **`merge_dict(base, extra)` 不要换成 `dict(base, **extra)`**：py2 的 `**` 展开要求键是
原生 `str`，JSON 解析 / 配置回填拿到 unicode 键时会抛 `UnicodeDecodeError`（同样只在冷路径
触发，容易漏测）。平台「保存设置」「台账人工字段合并」都依赖它，`opslib/selftest.py` 里
「compat 引用均有定义」一项会静态扫描全仓 `compat.<名>` 引用，防止再出现「调用了不存在的函数」。

---

## 12. 动态值与「原生串」规则（py2 必读）

py2 下 `字节串 % unicode` 会**先按 ascii 解码字节串模板**，含中文的字面量随即抛
`UnicodeDecodeError: 'ascii' codec can't decode byte 0xe6 in position 0`。
这与业务无关，却会让**任意 404 变成 500**（浏览器自动请求的 `/favicon.ico`、
静态资源缺失、不存在的接口全中），日志被 traceback 刷屏。注意：**与路径里有没有
中文无关**——炸的是字面量本身；所以请求路径是 `/favicon.ico` 也一样中招。

规则：**任何进入框架的动态值都必须是「原生串」**（`compat.NATIVE_STR`：
py2 下是字节串，py3 下是 str），与 `to_native` / `safe_str` 的释义一致：

| 数据入口 | 处理 | 说明 |
|---|---|---|
| JSON 请求体 | `req.json()` → `compat.json_loads` | 内部递归 `to_native` 归一 |
| URL / 请求路径 | `compat.urlunquote` | 返回原生串；`server.dispatch` 与静态资源分支再兜一层 `to_native`。**py3 分支直接把文本交给 `unquote`**，不得传 bytes（3.9 之前会抛 `TypeError`，见下） |
| 路由参数 `<name>` | 随路径一起归一 | 处理器可直接 `'服务器不存在: %s' % name` |
| 任意值 → 拼接 / 格式化 | `compat.safe_str(v)` | 统一 `None`→`''` 与类型归一 |
| 原生串 → unicode | `compat.to_text(v)` | 需要与 `u''` 字面量拼接时显式调用 |

自检兜底：`python opslib/selftest.py` 的「URL 解码返回原生串」，
以及 `python verify_py2_compat.py` 的「404 错误串可安全格式化」
（用「字节串模板 + unicode 参数」显式复现 py2 语义，在 py3 上也能抓住回退）。

### py3 专属陷阱：不得向 `urllib.parse.unquote` 传 bytes（v1.0.8 修复）

py2 与 py3 的 `unquote` **签名不同**：py2 的 `urllib.unquote` 只接受字节串，
py3 的 `urllib.parse.unquote` 只接受**文本**（str）。`urlunquote` 老实现为
照顾 py2 而统一 `to_bytes()` 后再调用，结果在 py3 下把 str 变成了 bytes：

* **Python 3.9+**：`unquote` 放宽了类型检查，bytes 也能过 —— 本地/CI **不复现**；
* **Python 3.5~3.8**：直接抛 `TypeError: Expected str, got bytes` —— 由于
  `urlunquote` 在 `server.dispatch` 的**每个请求**都会走，表现为
  **首页 `GET /` 都 500、服务完全不可用**。

正确写法是按版本分支（`compat.py` 现实现）：

```python
def urlunquote(text):
    if PY2:
        return to_native(to_text(_urllib.unquote(to_bytes(text))))   # py2：字节串
    return to_native(_urllib.unquote(to_text(text)))                 # py3：文本
```

自检已固化：`selftest` 的「URL 解码不向 unquote 传 bytes（兼容 py3.8）」与
`verify_py2_compat` 的「py3 下不向 unquote 传 bytes（兼容 3.5~3.8）」会把
`unquote` 换成严格代理（拒收 bytes），从而在**任何** py3 解释器上都能拦截该回归。
