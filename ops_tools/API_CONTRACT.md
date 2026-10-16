# 接口契约（v2.0）

> 本文件是**前后端与各服务之间的唯一接口约定**，改动前先改这里。
> 所有接口统一返回 `{"status":"success"|"error", ...}`，失败时带 `message` 与 `code`。
> 鉴权：请求头 `X-Auth-Token`（会话令牌或开放接口令牌）或 `?token=`。
> 角色：`admin` / `ops` / `viewer`；权限点见 `opslib/auth.py` 的 `PERMS`。

---

## 一、运维工具集成平台 `ops_Platform.py` v2.0（管理机，默认 :9500）

### 1.1 会话与身份

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/` `/login` | 免鉴权 | 页面（SPA，登录态由前端按 `/api/me` 判断） |
| GET | `/api/ping` | 免鉴权 | 存活探测，返回 `{version}` |
| GET | `/api/health` | 免鉴权 | 存活 + 版本 + 运行时长 + PID + 已纳管台数 |
| GET | `/api/openapi.json` | 免鉴权 | OpenAPI 3.0 描述（CI/CD 对接用） |
| POST | `/api/login` | 免鉴权 | 入参 `{username, password}` → `{token, username, role, servers, perms}`；失败 401 |

> `perms` 为角色原始权限列表（管理员即 `['*']`，不展开为明细清单）。前端 `can()` 支持 `*` 通配；展开清单会因目标机 `opslib/auth.py` 版本落后而缺新权限点，导致前端误隐按钮，故 v2.0.3 起不再展开。
| POST | `/api/logout` | 需登录 | 注销当前会话 |
| GET | `/api/me` | 免鉴权 | 未登录返回 `{need_login:true}`；已登录返回 `{username, role, role_label, servers, perms, need_login:false, version}` |
| GET | `/api/roles` | 需登录 | 角色与权限点清单（供用户管理界面渲染） |
| POST | `/api/password` | 需登录 | 修改自己的口令 `{old_password, new_password}` |

**开放接口令牌**：启动参数 `--token` 设置后，该令牌等价管理员身份，可直接调用任意 `/api/*`
（CI/CD、脚本对接用）。未设置 `--token` 且没有用户时：若 `--no-login` 则全开放，否则仍需登录
（默认账号 `admin`，首次启动时在控制台打印随机口令；可用 `--admin-password` 指定）。

### 1.2 用户管理（`user.admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/users` | 用户列表（不含口令摘要） |
| POST | `/api/users` | `{username, password, role, servers:[], note}` |
| PUT | `/api/users/<name>` | 改角色 / 授权服务器 / 启用状态 / 备注 / 改密（`password` 可选） |
| DELETE | `/api/users/<name>` | 删除（必须保留至少一个管理员） |
| POST | `/api/users/<name>/reset-password` | 重置口令，返回新口令一次（不回显旧口令） |

`servers` 为服务器名数组，`["*"]` 表示全部；非管理员用户只能看到被授权的服务器。

### 1.3 服务器与标签

服务器条目字段：

```json
{
  "name": "web-01", "host": "10.0.0.11",
  "upload_port": 8080, "shell_port": 9100, "metrics_port": 8040,
  "upload_token": "", "shell_token": "",
  "type": "upload | shell | both",
  "access_mode": "auto | direct | proxy",
  "upload_dir": "/home/xpe/app_temp",  // 远端上传目录（v2.1.0，可空；上传时作为 dir 参数，需目标机 --allow-client-dir）
  "group": "核心交易组",          // 分组
  "env": "prod | test | dev",     // 环境
  "tags": ["java", "nginx", "重要"],  // 标签（任意字符串）
  "note": "备注"
}
```

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/servers` | `server.view` | 列表，支持 `?group=&env=&tag=&kw=&probe=0/1`；默认并行探测状态 |
| GET | `/api/meta` | `server.view` | 聚合出 `{groups:[], envs:[], tags:[], types:[]}` 供筛选下拉 |
| POST | `/api/servers` | `server.edit` | 新增 |
| PUT | `/api/servers/<name>` | `server.edit` | 修改（支持改名） |
| DELETE | `/api/servers/<name>` | `server.edit` | 删除 |
| GET | `/api/servers/export?format=json\|csv` | `server.view` | **批量导出（v2.1.0）**：`json` 含令牌（**需 `server.edit`**，403 拒绝只读账号）、附件下载，可用于备份/迁移后回导；`csv` 明文表（不含令牌） |
| POST | `/api/servers/import` | `server.edit` | **批量导入（v2.1.0）**：`{"servers":[…], "mode":"merge\|skip"}`（也接受裸数组）；逐条校验归一，坏条目记入 `errors:[{index,name,error}]` 继续处理；返回 `{added, updated, skipped, error_total, errors[:20]}`；单次 ≤500 台；写审计 `server.import` |
| POST | `/api/servers/batch-update` | `server.edit` | **按标签/分组/环境批量改属性**：`{selector:{tags:[],env,names:[]}, set:{group,env,tags_add:[],tags_remove:[]}}` |
| POST | `/api/servers/batch-probe` | `server.view` | 批量探测在线状态 |
| POST | `/api/servers/bulk-exec` | `cmd.exec` | **按标签批量执行命令**：`{selector:{...}, cmd, timeout}` → 返回任务 id，逐个结果进任务日志 |
| GET | `/api/servers/<name>/status` | `server.view` | 单台状态（upload/shell/metrics 三路探测结果） |
| GET | `/api/servers/<name>/metrics` | `server.view` | 单台实时指标（见 1.6） |
| GET | `/api/servers/<name>/metrics-stream` | `server.view` | SSE，`?interval=3` |
| GET | `/api/servers/<name>/sysinfo` | `server.view` | 资产画像（探针 sys_info，用于台账采集） |

### 1.4 文件管理

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/servers/<name>/files?path=&q=&sort=&order=&page=&size=` | `file.read` | 远端文件列表；`sort` 支持 `name/size/mtime/type`，`order=asc/desc`；返回 `{files:[…], total, page, size, pages, dirs:[]}` |
| GET | `/api/servers/<name>/download?path=` | `file.read` | 代理下载单文件（流式，支持 Range 透传） |
| GET | `/api/servers/<name>/batch-download?paths=&paths=` | `file.read` | 代理打包下载 zip |
| DELETE | `/api/servers/<name>/files?path=` | `file.delete` | 删除远端文件/目录 |
| POST | `/api/servers/<name>/batch-delete` | `file.delete` | `{paths:[…]}` |
| POST | `/api/servers/bulk-delete-files` | `file.delete` | `{targets:[{name, path}], selector:{...}}` 按标签批量删文件 |
| ANY | `/api/servers/<name>/r/<upload\|shell>/<远端路径>` | 按 kind 判权 | **通用转发入口**（GET/POST/PUT/DELETE），请求体流式转发，保留 `Range`、`X-Upload-Id` 等头 |

上传走 `/api/servers/<name>/r/upload/api/chunk/init|upload|complete|put`，与 upload_server v3.x 一致。

`relative_path` 两种写法都支持（v3.2.2 起）：含文件名的整路径（`a/b/c.txt`，页面/文件夹上传）或只给目录（`a/b` + `filename=c.txt`，脚本式调用）。
落盘同名冲突会自动加 `_1/_2` 后缀，并发同名上传不会互相覆盖（也不再返回 500）。
`GET /api/health` 返回 `{alive, version, chunk_size, chunk_write_mode, token_required}`，客户端应先读它再决定分片大小。

**请求级上传目录覆盖（v3.3.0）**：目标机以 `--allow-client-dir` 启动后（`GET /api/status` 的 `allow_client_dir=true`），
`/api/chunk/put?dir=`、`/api/chunk/init`（JSON `dir`，`complete` 未带 dir 时沿用 init 记录）、`/api/chunk/complete`、`/api/upload`（表单 `dir`）
可传**绝对路径** `dir` 覆盖本次落盘目录；相对路径直接 **400**（不静默回退到 upload-dir）。
上传响应统一带回真实落盘路径（`path` / `dir` / `upload_dir`），调用方可据此校验「到底存哪了」。未开开关时 `dir` 一律忽略，行为与旧版本一致。

### 1.5 命令执行与实时输出

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/servers/<name>/exec` | `cmd.exec` | 同步执行 `{cmd, timeout}`（沿用旧接口，兼容已有前端） |
| POST | `/api/servers/<name>/exec-stream` | `cmd.exec` | **SSE 实时输出（首选）**：命令放 JSON 体 `{cmd, timeout}`、令牌走 `X-Auth-Token`，URI 上无命令 |
| POST | `/api/servers/<name>/exec-ticket` | `cmd.exec` | 换取**一次性执行券**：`{cmd, timeout}` → `{status, ticket, ttl, timeout}`（60s 有效、用后即焚、绑定来源 IP） |
| GET | `/api/servers/<name>/exec-stream?ticket=<券>` | —（凭券） | 凭券取流（SSE 事件同下）；浏览器 `EventSource` 无法自定义请求头，故用券 |
| GET | `/api/servers/<name>/exec-stream?cmd=&timeout=` | `cmd.exec` | **旧式（兼容保留）**：命令在查询串，与 webshell 特征同形、易被安全设备误报，新前端不再使用 |
| POST | `/api/servers/<name>/exec-async` | `cmd.exec` | 提交为后台任务，返回任务 id（长命令用） |

SSE 事件：`start` / `out`（stdout 增量）/ `err` / `exit`（含 returncode、耗时）。

服务端实现建议：先尝试目标 `web_shell` 的 `POST /api/exec/stream`（v1.3 起，命令在 body、SSE 透传）；
失败退回 `GET /api/exec/stream?cmd=`（兼容老版本），再失败降级为同步 `/api/exec`。
**退避链必须防重放**：仅在「建连失败且未收到任何事件」时才重试，否则「重启服务」这类命令会被执行两次。
web_shell 侧对应入口：`POST /api/exec/stream`、`POST /api/exec/start`（换券）、`GET /api/exec/stream?ticket=`、`POST /api/sessions/<id>/stream`。

### 1.6 监控（SSE 曲线）

`GET /api/servers/<name>/metrics` 返回：

```json
{"status":"success","metrics":{
  "ts":1690000000,"cpu_percent":12.3,"cpu_cores":8,"load":[0.5,0.6,0.7],
  "mem":{"total":0,"used":0,"percent":0},"swap":{"total":0,"used":0,"percent":0},
  "disks":[{"mount":"/","total":0,"used":0,"percent":0}],
  "net":{"rx":0,"tx":0,"rx_rate":0,"tx_rate":0},
  "disk_io":{"read":0,"write":0,"read_rate":0,"write_rate":0},
  "uptime":0,"processes":0,"connections":0}}
```

数据来源优先级：目标机 `host_monitor` 探针（`metrics_port`，默认 8040，`GET /api/metrics`）
→ 无探针时用 `web_shell` 执行采集命令降级 → 都不可用时返回 `online:false`。

`GET /api/servers/<name>/metrics-stream?interval=3`：每 `interval` 秒推一个 `metrics` 事件。
`GET /api/monitor/overview`：一次采样返回所有（或有权限的）服务器指标，供总览卡片。

### 1.7 发布中心视图（对接 release_center）

平台不自己实现发布逻辑，而是把请求转发给 release_center（地址在设置里配置，
默认 `http://127.0.0.1:8090`，令牌 `release_token`）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/release/config` | `server.view` | 返回 `{url, online, version, authorized, auth_mode}`（在线 + 授权探测） |
| GET | `/api/release/apps` | `server.view` | 应用定义列表（透传） |
| GET | `/api/release/packages?app=` | `server.view` | 制品包列表（透传，`?app=` 过滤） |
| POST | `/api/release/packages/upload?filename=&app=&version=&src=&skip_dup=` | `release.run` | **流式透传**上传制品包（原始二进制 body，见下方说明） |
| GET | `/api/release/orders` | `server.view` | 发布单列表（透传，支持 `?limit=&state=`） |
| POST | `/api/release/orders` | `release.run` | 创建发布单（透传） |
| GET | `/api/release/orders/<id>` | `server.view` | 发布单详情 |
| POST | `/api/release/orders/<id>/run` | `release.run` | 执行发布 |
| POST | `/api/release/orders/<id>/rollback` | `release.run` | 回滚 |
| GET | `/api/release/orders/<id>/stream` | `server.view` | SSE 透传发布进度 |

`/api/release/*` 的**错误码约定**（v2.0.4 起，前端据此区分「谁的 401」）：

| 状态 | `code` | 含义 | 前端行为 |
|---|---|---|---|
| 502 | `upstream` | 发布中心不可达 | 提示 + 引导改地址 |
| 502 | `upstream_unauthorized` | 平台→发布中心**没带凭据或凭据失效** | 提示 + 引导去设置填令牌/账号口令 |
| 502/504 | 上游其它状态 | — | 提示 |

> **上游的 401/403 绝不透传**：前端 `api()` 见到 401 会判定「平台会话失效」并弹回登录页，
> 透传上游 401 会造成「登录成功→发布视图一加载就被退出」的死循环（v2.0.4 修复）。
> 凭据来源优先级：`--release-token` > 设置页 `release_token` > 设置页 `release_user`/`release_pass` 自动登录（会话缓存于 `data/release_session.json`，失效自动重登一次）。`auth_mode` 即该优先级的体现（`token` / `login` / `none`）。

**v2.0.5 起的两条透传约定**（都是「静默出错」型缺陷的根治）：

1. **查询串必须用 `req.raw_query`**。`Request.parsed` 是 `urlparse(handler.path[:qpos])`，
   dispatch 为了路由匹配已经把 `?query` 剥掉，所以 `req.parsed.query` **恒为空串**。
   转发类接口（发布中心透传、`/api/servers/<n>/r/...` 转发到目标机）原先用它拼 URL，
   查询串被静默丢弃 —— 表现为 `?app=`「只看所选应用」、分页 `?page=` 在另一端「设置了没反应」，
   两端都不报错。统一走 `req_query(req)`（= `to_text(raw_query)`）。
   自检里有静态兜底：生产代码出现 `req.parsed.query` 直接失败。
2. **制品包上传走 `release_upload_proxy` 流式透传**。普通透传用 `req.raw_body()`，
   默认 **32MB 上限**（稍大的包直接 413），且整包读进内存（并发上传会打爆平台）。
   上传路径改为手工建连、64KB 一片边收边发，内存占用与包大小无关（实测 34MB 直传 0.4s），
   超时单列 `RELEASE_UPLOAD_TIMEOUT=3600`。该路径同样把上游 401/403 翻成 502 `upstream_unauthorized`。

### 1.8 资产台账视图

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/cmdb?kw=&env=&group=&tag=` | `server.view` | 台账列表：自动采集字段 + 人工字段合并 |
| GET | `/api/cmdb/<name>` | `server.view` | 单台详情（含采集历史） |
| POST | `/api/cmdb/collect` | `task.run` | `{selector:{...}}` 或 `{names:[]}`：并发采集并写台账，返回任务 id |
| PUT | `/api/cmdb/<name>` | `cmdb.edit` | 人工字段：`{owner, business, level, idc, note, asset_no}` |
| GET | `/api/cmdb/export?format=csv` | `server.view` | 导出台账（CSV，UTF-8 BOM） |
| GET | `/api/cmdb/summary` | `server.view` | 汇总：按环境/分组/OS/内核统计台数与配置分布 |

台账落盘：`data/cmdb.json`，结构 `{"items": {name: {"auto": {...}, "manual": {...}, "updated": ts}}}`。

### 1.9 审计 / 任务 / 通知 / 设置

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/audit?user=&action=&keyword=&result=&start=&end=&limit=&offset=` | `audit.view` | 审计分页查询 |
| GET | `/api/audit/stats?days=7` | `audit.view` | 概览统计 |
| GET | `/api/audit/verify` | `audit.view` | 校验哈希链 `{ok, message, checked}` |
| GET | `/api/audit/export.csv` | `audit.view` | 导出 CSV |
| GET | `/api/tasks?state=&kind=&limit=` | `server.view` | 任务列表 |
| GET | `/api/tasks/<id>` | `server.view` | 任务详情（含日志） |
| POST | `/api/tasks/<id>/cancel` | `task.run` | 取消任务 |
| GET | `/api/tasks/stats` | `server.view` | 任务统计 |
| GET | `/api/notify/config` | `settings.edit` | 通知渠道配置（口令脱敏） |
| POST | `/api/notify/config` | `settings.edit` | 保存通知渠道 |
| POST | `/api/notify/test` | `settings.edit` | 渠道连通性自测 |
| GET | `/api/settings` | `server.view` | 全局设置（含 `locked`：哪些项被启动参数钉死） |
| POST | `/api/settings` | `settings.edit` | 保存全局设置（部分字段更新，未提交的字段保持原值） |

全局设置字段：`access_mode`、`upload_concurrency`、`chunk_mb`、`release_url`、`release_token`、
`release_user`、`release_pass`、`metrics_interval`、`server_probe_interval`、`theme`（`light`/`dark`/`auto`）。

`GET /api/settings` 响应：

```json
{ "status": "success",
  "settings": { "release_url": "http://172.19.196.130:8094", "…": "…" },
  "locked":   { "release_url": false, "release_token": false, "release_login": false } }
```

- `settings.release_url` 为**归一化后**的地址：缺 `http://` 会自动补齐（`172.19.196.130:8094`
  → `http://172.19.196.130:8094`）、去掉末尾斜杠；留空则回退默认 `http://127.0.0.1:8094`。
- `locked.release_url=true` 表示进程启动时带了 `--release-url`，其优先级高于设置页，改了不生效
  （`--release-token` 同理）。前端据此在设置页给出提示。
- `locked.release_login=true` 表示已配好发布中心令牌（`--release-token` 或设置页令牌），
  此时 `release_user` / `release_pass` 的自动登录不会被使用（令牌优先）。
- `release_user` + `release_pass` 为**可选**的发布中心管理员凭据：未配置令牌时，平台用它
  `POST <release_url>/api/login` 换取会话令牌，缓存于 `data/release_session.json`（按 base 存），
  上游 401 会自动重登一次。`release_pass` 只下发给持有 `settings.edit` 的会话，其他角色读到空串。
- 表单为**部分更新**：只提交需要改的字段即可，其余字段保持原值（不会被打回默认值）。

### 1.10 统一事件流（SSE）

`GET /api/events?topics=task,metrics,alert&interval=0` → SSE，事件名即主题名，`data` 为 JSON。
- `task`：任务进度（来源 `task.TaskManager` 的 bus）
- `metrics`：监控曲线（服务端按 `metrics_interval` 定时采样后广播）
- `alert`：告警（预留）
- `release`：发布进度（由 release_center 主动 POST 到 `/api/events/push` 触发广播，见下）

`POST /api/events/push`（`release.run` 权限）：`{topic, data}` → 广播给所有订阅者。
供 release_center 把发布进度实时推给平台前端。

---

## 二、发布中心 `release_center.py` v1.2（管理机，默认 :8094）

### 2.1 定位

管理机侧的发布编排服务：默认**包上传 → 备份旧版 → 停服 → 替换 → 启服 → 健康检查 → 失败自动回滚**。
v1.1 起支持**应用级自定义步骤链**（数量、顺序、内容任意编排），自身不落地业务文件，
而是调用目标机上的 `upload_server`（:8089）与 `web_shell`（:8088）完成动作。

### 2.2 数据模型

```json
// 应用定义 apps
{"name":"app-web","version_file":null,"biz":"交易",
 "target_dir":"/opt/app","package":"app-web-${version}.jar",
 "backup_dir":"/opt/app/backup","backup_keep":5,
 "release_cmd":"cp ${package} ${target_dir}/app.jar",
 "stop_cmd":"sh /opt/app/stop.sh","start_cmd":"sh /opt/app/start.sh",
 "health_type":"port|http|cmd|none","health_target":"8080 / http://127.0.0.1:8080/health / test -f /tmp/ok",
 "health_retry":10,"health_interval":3,
 "rollback_cmd":"",
 "param_defs":[{"key":"Version_All","label":"版本号","default":"","required":true},
               {"key":"NPkg","label":"包序号","default":"","required":true}],
 "steps":[{"name":"fetch","label":"拉取版本包","cmd":"scp ${src_host}:~/${pkg_dir}/* ~/app_temp/",
           "continue_on_fail":false},
          {"name":"stop","label":"停服","cmd":"cd ~/bin/dist_shell/ && sh app_stop.sh"},
          {"name":"health","label":"健康检查","cmd":"@health"}],
 "vars":{"src_host":"xpe_138","pkg_dir":"deploy/pkg","Version_All":"3.2.0","NPkg":"01"}}
// 发布单 orders
{"id":"r-20260920-xxxx","app":"app-web","version":"1.2.3","package":"app-web-1.2.3.jar",
 "servers":["web-01","web-02"],"strategy":"serial|parallel","user":"admin",
 "state":"pending|running|success|failed|rolled_back|canceled",
 "steps":[{"name":"上传包","state":"success","message":"","elapsed":1.2,
           "logs":["…"]}, …],
 "created":1690000000,"started":null,"finished":null,
 "result":{"succeeded":["web-01"],"failed":[]}}
```

步骤链（v1.1）：应用未配置 `steps` 时使用固定六步链；配置后按自定义链执行
（每台服务器单独执行一遍，`strategy` 决定逐台还是并发）。固定六步链：
1. `上传包`（upload_server `/api/chunk/put` 或分片接口）
2. `备份旧版`（web_shell 执行：把 `target_dir` 下现网包/目录复制到 `backup_dir/<时间戳>`，并按 `backup_keep` 清理）
3. `停服`（`stop_cmd`）
4. `替换`（`release_cmd`）
5. `启服`（`start_cmd`）
6. `健康检查`（按 `health_type` 重试 `health_retry` 次）
7. 失败时：`回滚`（恢复最近一份备份 → `start_cmd` → 再健康检查），发布单状态置 `rolled_back`

**发布参数（v1.2）**：应用可定义 `param_defs`（[{key,label,default,required}]，≤16 个，
key 为字母开头的字母/数字/下划线）；创建发布单（POST /api/orders）时可携带
`params` 对象逐单填值——缺必填或含未定义键返回 400；执行时按
「内置变量 > order.params > app.vars」合并注入所有步骤命令的 `${key}`。
`GET /api/presets` 的 script_chain 模板已带 `Version_All`/`NPkg` 两个参数定义。

**自定义步骤链规则（v1.1）**：
- `steps` 为数组，每项 `{name, label?, cmd, continue_on_fail?}`；`name` 限字母/数字/`_`/`-`（1~32 字符），最多 16 步。
- `cmd` 以 `@` 开头为内置动作：`@upload`（管理机推包，之后 `${package}` 可用）、`@backup`（内置备份，之后回滚有备份点）、`@stop`/`@replace`/`@start`（分别执行应用的 stop_cmd/release_cmd/start_cmd）、`@health`（内置健康检查）。其余内容作为 shell 命令在目标机原样执行。
- 命令先做 `${var}` 替换：内置变量 `version / target_dir / backup_dir / app / package`，加上应用级 `vars`（键值对，如 `Version_All`、`NPkg`）。
- `continue_on_fail: true` 的步骤失败只标记该步失败，继续执行后续步骤（软失败，不触发回滚）。
- 未内置 `@backup` 的链（如用自定义 version_backup.sh 备份）手动回滚时无内置备份点，可配置 `rollback_cmd`。
- `POST /api/orders` 请求体新增可选 `params` 对象（按应用 `param_defs` 校验）。
- `GET /api/presets` 返回内置模板：`script_chain`（拉包→停服→备份→发布前→解包→环境配置→发布后→启服→健康检查，前端一键载入）与 `standard`（默认六步）。

### 2.3 接口

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/` | 免鉴权 | 页面（`templates/release_center.html`） |
| GET | `/api/ping` `/api/health` | 免鉴权 | 存活 / 状态 |
| GET | `/api/openapi.json` | 免鉴权 | OpenAPI 描述 |
| GET/POST | `/api/servers` | `server.view`/`server.edit` | 纳管目标机（与平台同结构：name/host/upload_port/shell_port/tokens） |
| PUT/DELETE | `/api/servers/<name>` | `server.edit` | 修改 / 删除 |
| POST | `/api/servers/import` | `server.edit` | 从平台导入：`{url, token}` → 拉取平台 `/api/servers` 并合并 |
| GET | `/api/presets` | `server.view` | 步骤链模板列表（script_chain / standard） |
| GET/POST | `/api/apps` | `server.view`/`server.edit` | 应用定义列表 / 新增（含 `steps` `vars`） |
| PUT/DELETE | `/api/apps/<name>` | `server.edit` | 修改 / 删除 |
| GET | `/api/packages?app=` | `server.view` | 制品包列表（`data/packages/`，含大小、上传时间、MD5，目录上传另含 `src` 来源相对路径） |
| POST | `/api/packages/upload?filename=&app=&version=&src=&skip_dup=` | `release.run` | 上传制品包（原始二进制 body，流式落盘；`skip_dup=1` 时同 MD5 直接复用已有包并返回 `duplicate:true`） |
| DELETE | `/api/packages/<name>` | `release.run` | 删除制品包 |
| GET/POST | `/api/orders` | `server.view`/`release.run` | 发布单列表 / 创建（创建后状态 `pending`） |
| GET | `/api/orders/<id>` | `server.view` | 详情（含步骤与日志） |
| POST | `/api/orders/<id>/run` | `release.run` | 执行（异步任务，立即返回 `{task_id}`） |
| POST | `/api/orders/<id>/rollback` | `release.run` | 手动回滚 |
| POST | `/api/orders/<id>/cancel` | `release.run` | 取消 |
| GET | `/api/orders/<id>/stream` | `server.view` | SSE：步骤进度 + 日志增量 |
| GET | `/api/events` | `server.view` | SSE 全局事件流（所有发布单） |
| GET | `/api/audit?…` | `audit.view` | 发布审计 |
| GET/POST | `/api/settings` | `settings.edit` | 设置：`platform_url`、`platform_token`（用于回推进度）、发布并发、备份保留 |

### 2.4 与平台的联动

- release_center 配置 `platform_url` + `platform_token` 后，每个步骤状态变化会
  `POST <platform_url>/api/events/push`，body `{topic:"release", data:{order_id, app, version, state, step, percent}}`，
  让平台前端无需轮询即可显示发布进度。
- 平台侧通过 `/api/release/*` 透传访问 release_center；两边的服务器清单可用
  `POST /api/servers/import` 一键同步。

---

## 三、目标机探针（`tools/`，被 release_center 与平台共同调用）

| 服务 | 端口 | 平台用到的接口 |
|---|---|---|
| `upload_server.py` v3.3.1 | 8089 | `/api/status` `/api/health`(版本/分片大小/落盘模式) `/api/list`(分页/筛选) `/api/chunk/*` `/api/download` `/api/batch/*` `/api/file` `/api/tree` |
| `web_shell.py` v1.3.1 | 8088 | `/api/status` `/api/exec` `/api/exec/stream`(SSE) `/api/sessions`(持久会话) |
| `host_monitor.py`（可选，本批次以平台内置采集为主） | 8040 | `/api/metrics` `/api/sysinfo` `/api/health` |
| `diff_file_dir_jar_excel.py` v2.1 | 8087 | `/api/compare/*` `/api/history` `/api/export` |

**跨服务令牌**：所有目标机服务的令牌统一放在服务器条目的 `upload_token` / `shell_token` 里，
请求时以 `X-Auth-Token` 头携带。
