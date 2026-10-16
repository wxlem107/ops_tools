# 运维工具集（运维tools）v2

一套**零依赖 / 低依赖、内网可用**的运维工具集合，包含五个可独立运行、也可组合使用的子系统：

| 组件 | 版本 | 默认端口 | 依赖 | 一句话定位 |
|---|---|---|---|---|
| `ops_Platform.py` | v2.1.0 | 8095 | 仅标准库 | 集成管理平台：服务器墙 / 文件管理 / 终端 / 监控 / 台账 / 审计 / 任务 / 发布入口 |
| `release_center.py` | v1.3.3 | 8094 | 仅标准库 | 发布中心：带自动回滚的发布流水线（自定义步骤链 + 发布参数）与发布单审计 |
| `upload_server.py` | v3.3.1 | 8089 | 仅标准库 | 目标机文件服务：分片上传 / 断点续传 / 目录树 / 打包下载 / 请求级目录覆盖 |
| `web_shell.py` | v1.3.1 | 8088 | 仅标准库 | 目标机命令服务：单次执行（SSE）+ 持久 Shell 会话 |
| `diff_file_dir_jar_excel.py` | v2.1 | 8087 | Flask + openpyxl | 比对工具：目录 / 文本 / Jar / Excel / docx / pptx，三向比对与报告导出 |

所有脚本均同时兼容 **Python 2.7.5+ 与 3.7.5+**。公共能力统一沉淀在 `opslib/`（12 个模块 + 114 项自检），前端页面全部与后端分离，**改页面刷新即生效，无需重启服务**。接口契约见 `API_CONTRACT.md`。

---

## 一、目录结构

```
ops_tools/
├── README.md                                本文件
├── API_CONTRACT.md                          五个服务的接口契约（前后端唯一约定）
├── opslib/                                  公共层（compat/store/auth/apibase/server/sse/
│   │                                        httpx/probe/audit/task/notify/crypto）
│   ├── README.md                            公共层用法
│   └── selftest.py                          自检：python opslib/selftest.py（114 项）
├── tools/                                   部署在「目标服务器」上的轻量服务
│   ├── upload_server.py          v3.3.1     文件/文件夹上传服务器
│   ├── web_shell.py              v1.3.1     浏览器远程命令执行
│   ├── diff_file_dir_jar_excel.py v2.1      文件比对工具
│   ├── requirements.txt                     diff 工具依赖（仅此一项需要 pip）
│   └── templates/                           前端页面（改完刷新即生效）
├── platform_console/                        部署在「管理机」上的集中控制台
│   ├── ops_Platform.py           v2.1.0     运维工具集成平台
│   ├── release_center.py         v1.3.3     发布中心
│   ├── static/                              平台 SPA（index.html / app.css / app.js）
│   ├── templates/release_center.html        发布中心独立页面
│   └── data/                                运行数据（配置/审计/台账，首启自动生成）
├── verify_integration.py                    四服务端到端集成验证脚本（212 项）
├── verify_upload_perf.py                    上传性能现场自测（磁盘基线 + 落盘模式对比）
├── verify_upload_speed.py                   上传链路测速诊断（客户端跑，定位链路/服务端/小文件瓶颈）
├── verify_py2_excepts.py                    py2 异常捕获审查（except 漏写 IOError 静态扫描）
└── verify_py2_compat.py                     py2 运行时语义回归（摘要/落盘/修复/路径编码/配置合并/上游提示串，25 项）
```

---

## 二、架构与部署模型

```
                    ┌────────────────────────────────────────────────┐
  运维人员浏览器 ──► │ 管理机                                          │
                    │  ops_Platform.py :8095（平台）                   │
                    │  release_center.py :8094（发布中心）              │
                    └──────┬──────────────────────────┬───────────────┘
                           │ direct / proxy / auto     │ 发布流水线
                           ▼                           ▼
      ┌────────────────────────────┐   ┌────────────────────────────┐
      │ 目标服务器                   │   │ 目标服务器                   │
      │ ├ upload_server.py :8089   │   │ ├ upload_server.py :8089   │
      │ └ web_shell.py     :8088   │   │ └ web_shell.py     :8088   │
      └────────────────────────────┘   └────────────────────────────┘
```

- **访问模式**：`auto / direct / proxy` 三选一，全局默认 + 每台服务器可单独覆盖。
  - `direct` 直连：浏览器直达目标（需目标服务开 CORS），链路最短；
  - `proxy` 代理：浏览器 → 平台 → 目标，适用于浏览器不可达目标（跨网段 / 防火墙 / HTTPS 页面访问 HTTP 目标）；
  - `auto` 自动：先探测直连，不通自动降级代理。
- **实时通道全部走 SSE**：命令输出（`/api/servers/<name>/exec-stream`）、监控曲线（`metrics-stream`）、任务与发布进度（`/api/events`）、上传进度，替代旧版轮询。
- **独立可用**：目标机上的 upload_server / web_shell 各自带完整页面，不依赖平台即可单独使用；发布中心也可独立运行（自带页面与登录）。

---

## 三、快速开始

### 1. 目标服务器（被纳管机器）

```bash
# 文件上传服务（默认 :8089，文件保存到 ./uploads）
nohup python -u upload_server.py --port 8089 --token up123 > upload_server.log 2>&1 &

# 远程命令执行（默认 :8088）
nohup python -u  web_shell.py --port 8088 --token shell123 > web_shell.log 2>&1 &

# 文件比对工具（默认 :8087，需要 Flask + openpyxl）
python  -m pip install -r requirements.txt
nohup python -u  diff_file_dir_jar_excel.py > diff_file_dir_jar_excel.log 2>&1 &
```

### 2. 管理机（平台 + 发布中心）

```bash
# 集成平台（:8095）
nohup python -u  ops_Platform.py --port 8095 --admin-password <管理员口令> > ops_Platform.log 2>&1 &

# 发布中心（:8094，可选；平台「发布中心」视图会透传到它）
nohup python -u  release_center.py --port 8094 --admin-password <管理员口令> \
       --platform-url http://127.0.0.1:8095 --platform-token <平台令牌> > release_center.log 2>&1 &

# CI/CD 开放接口令牌（等价管理员，用于脚本调用）
nohup python -u  ops_Platform.py --token <开放令牌> > ops_Platform.log 2>&1 &
```

浏览器打开 `http://<管理机IP>:8095`，用管理员口令登录后在「服务器墙 → ＋ 添加服务器」录入目标机器。平台会把目标机清单共享给发布中心（发布中心侧也可用 `POST /api/servers/import` 拉取合并）。

### 3. 自检与验证

```bash
python opslib/selftest.py          # 公共层 115 项自检
python verify_integration.py       # 平台 + 发布中心 + 上传 + 终端 四服务端到端验证（212 项）
python verify_py2_excepts.py       # py2 异常捕获审查：找出 except 漏写 IOError 的位置
python verify_py2_compat.py        # py2 运行时语义回归：摘要落盘/校验/损坏修复/路径编码/配置合并/上游提示串/转发查询串（31 项）
python verify_upload_perf.py       # 上传性能现场自测：磁盘基线 + 两种落盘模式对比（可指定真实盘位）
python verify_upload_speed.py      # 上传链路测速诊断：并发梯度/大分片/小文件批量（可在客户端直接打真实 8089）
```

---

## 四、组件详解

### 4.1 ops_Platform.py —— 运维工具集成平台 v2.1.0

| 项目 | 说明 |
|---|---|
| 默认端口 | `8095` |
| 页面 | `static/index.html` + `app.css` + `app.js`（SPA，hash 路由 9 个视图） |
| 运行数据 | `--data-dir`（默认脚本同级 `data/`，配置/审计/台账均在此；旧版同级 `ops_platform_config.json` 首启自动迁移） |
| 鉴权 | 多用户（admin / ops / viewer 三角色）+ 按服务器授权 + `--token` 开放接口令牌 |

**功能视图**

- **仪表盘**：在线数、任务、审计概览卡片，监控迷你曲线（Canvas）。
- **服务器墙**：卡片/表格双态；每台显示在线状态、版本、运行时长、磁盘、**分组 / 环境（生产·测试·开发）/ 标签**徽章；按分组/环境/标签/关键字筛选后可**批量操作**：批量改标签分组环境、批量执行命令（bulk-exec）、批量探测、批量删文件。
  - **批量导入 / 导出（v2.1.0）**：工具栏「导出 JSON / 导出 CSV / 批量导入」。JSON 含目标机令牌（需服务器编辑权限，用于备份与跨环境迁移），CSV 为明文表（只读账号可用，不含令牌）；导入支持选择导出的 JSON 文件或直接粘贴，同名可选**覆盖更新**或**跳过**，坏条目逐条列出不整体失败，单次上限 500 台，全过程写审计 `server.import`。
  - **远端上传目录 upload_dir（v2.1.0）**：服务器新增/编辑弹层可配置（留空用远端默认）。文件上传（单片与分片）会以 `dir` 参数传给目标机 upload_server；**目标机需 v3.3.0+ 并以 `--allow-client-dir` 启动**，否则远端静默回落其默认目录——平台会按上传响应里的真实落盘路径校验，配置未生效时每台提示一次。文件管理页服务器徽章会显示当前生效的上传目录。
- **文件管理**：远端文件**分页 / 按名称·大小·时间·类型排序 / 类型筛选 / 关键字搜索**；多选批量下载 zip、批量删除；分片上传（并发池、自适应分片、失败重试 3 次、进度 SSE）。
- **命令终端**：单机 SSE 实时输出（start/out/err/exit），↑↓ 历史、快捷命令、多选服务器经 bulk-exec 分组回显；结果可导出。
- **发布中心**：应用 / 制品包 / 发布单管理，流水线步骤进度实时滚动（透传 release_center :8094），一键回滚（二次确认）。
  - **制品包上传**：拖拽（含**整个文件夹**递归展开，`webkitGetAsEntry`）、一次多选、整目录选择；上传队列**默认 2 路并发**（1~4 可选）+ 逐文件进度 + 可停止；自动忽略 `.DS_Store`/`Thumbs.db`；内容相同的包按 MD5 跳过（`skip_dup=1`），同名不同内容自动改名；来源相对路径落库便于溯源。也可直接把文件/文件夹**拖到左侧「制品包」面板**上传。
  - **创建发布单支持多选制品包**：筛选 / 全选 / 清空 / 「只看所选应用」（切应用自动勾上该应用的包），上限 32 个；提交 `packages` 数组（`package` 保留第一个以兼容旧模板），`@upload` 按序全部推送并逐个校验远端 MD5。
- **资产台账（CMDB）**：一键并发采集（OS/内核/CPU/内存/磁盘/IP 等），人工字段（负责人/业务/等级/机房）行内编辑，CSV 导出，汇总统计。
- **审计**：全量写操作哈希链留痕，分页查询、统计、链校验、CSV 导出。
- **任务中心**：bulk-exec / 采集等异步任务的状态、进度、日志、取消。
- **设置**：访问模式、上传并发、分片大小、监控采样间隔、**发布中心地址 / 令牌 / 账号 / 口令**、通知 webhook、主题（亮/暗/跟随系统）。发布中心地址支持粘贴 `172.19.196.130:8094` 这类简写（自动补 `http://`、去末尾斜杠），输入框下方实时显示**实际可用地址**与**授权状态**（在线 / 未授权 401 / 离线）；地址被启动参数 `--release-url` 覆盖时会显式提示（真实来源始终可从 `GET /api/settings` 的 `locked` 字段判断）。发布中心凭据两种填法二选一：**令牌**（与 release_center 的 `--token` 同值）或**账号+口令**（管理员账号，平台自动登录并缓存会话，无需重启任何服务）。
- **日志降噪**：客户端提前断开（关页签 / SSE 断流 / curl 取消）只留一行限流提示，不再往 stderr 打整段 traceback；真实异常仍保留完整堆栈。

**界面增强**：暗色主题、移动端适配（<768px 抽屉侧栏）、`Ctrl+K` 命令面板、`g d/s/f/t/r` 视图跳转、Toast 与二次确认。

**命令行参数**

| 参数 | 说明 | 默认 |
|---|---|---|
| `--host` / `--port` | 监听地址 / 端口 | `0.0.0.0` / `8095` |
| `--config` | 配置文件路径 | `data/ops_platform_config.json` |
| `--token` | 开放接口令牌（CI/CD，等价管理员） | 空 |
| `--admin-password` | 初始管理员口令（不传则自动生成并打印）。**仅首次启动生效**；用户库已存在时不生效，但若 admin 的口令记录已损坏（早期 py2 版本写入原始二进制摘要）则会自动修复为该口令 | 自动 |
| `--reset-admin-password` | 启动时把 admin 口令重置为 `--admin-password`（未提供则生成随机口令并打印） | 关 |
| `--no-login` | 关闭登录要求（内网可信环境） | 关 |
| `--data-dir` | 运行数据目录 | 同级 `data/` |
| `--release-url` / `--release-token` | 发布中心地址 / 令牌 | `http://127.0.0.1:8094` |
| `--static-dir` | 静态页面目录 | 同级 `static/` |

**环境变量**：`OPS_HOST`、`OPS_PORT`、`OPS_CONFIG`、`OPS_TOKEN`、`OPS_STATIC`

**主要接口**（完整契约见 `API_CONTRACT.md`；`GET /api/openapi.json` 可机读）

| 分组 | 代表接口 |
|---|---|
| 会话 | `POST /api/login`、`/api/logout`、`GET /api/me`、`/api/roles`、`POST /api/password`、`/api/users` CRUD |
| 服务器 | `/api/servers` CRUD、`GET /api/meta`（分组/环境/标签聚合）、`POST /api/servers/batch-update`、`bulk-exec`、`bulk-delete-files`、`batch-probe` |
| 实时 | `GET /api/events?topics=`（统一 SSE）、`/api/servers/<name>/exec-stream`（GET 券 / POST）、`POST /api/servers/<name>/exec-ticket`（换取一次性执行券）、`metrics-stream`、`POST /api/events/push` |
| 监控/台账 | `/api/servers/<name>/metrics`、`sysinfo`、`/api/monitor/overview`、`/api/cmdb`（采集/编辑/导出/汇总） |
| 文件 | `/api/servers/<name>/files`（分页/筛选/排序）、`download`、`batch-download`、`batch-delete`、`ANY /api/servers/<name>/r/<kind>/<path>` 通用转发 |
| 任务/审计 | `/api/tasks`（列表/详情/取消/流）、`/api/audit`（查询/统计/链校验/CSV） |
| 发布 | `/api/release/*` 全部透传 release_center（离线时优雅提示） |
| 通知 | `/api/notify/config`（脱敏）、`/api/notify/test` |

**保留的实测调优**：代理上传全流式转发、上游 keep-alive 连接复用（含死连接探活剔除）、分片 URL 固定降 CORS 预检、进度渲染节流。

**v2.0.6 ~ v2.0.8 变更**

- **命令转发改走 POST + 执行券（v2.0.7）**：平台转发到 web_shell 时命令放请求体、令牌走 `X-Auth-Token`，URI 上不再出现 `cmd=`——消除被安全设备判为 webshell 的误报，同时令牌不落日志。新增 `POST /api/servers/<name>/exec-ticket` 换取一次性执行券，前端凭券 `GET .../exec-stream?ticket=` 取流（解决 `EventSource` 无法自定义请求头的老问题）。
- **服务器列表探测并发化 + TTL 缓存（v2.0.8）**：修「点击编辑、改完保存不上」。真因**不在保存**（保存一直成功），而在**保存后的自动刷新**——列表 / 详情对每台机器**串行实时探测**（离线机每台最多 3 次 × 2s 超时），多台离线机时刷新接口长时间不返回，界面就成了「保存没生效」。现改为 `probe_many` 有界并发（8 线程）+ 15s TTL 缓存（增删改自动失效）：6 台离线机列表冷启动约 6s、二次 0.00s。
- **上游连接池超时复用修复**：`http.client` 的 socket 超时在**建连时**设定，keep-alive 复用时不刷新——短超时（探测 2s）建的连接被长请求复用会莫名读超时（表现为 `status=0`）。现每次取用连接都重设超时。

**v2.1.0 变更（服务器批量维护 + 远端上传目录）**

- **批量导入 / 导出**：`GET /api/servers/export`（`format=json` 含目标机令牌、需 `server.edit`；`format=csv` 明文表、`server.view` 即可）与 `POST /api/servers/import`（`{"servers":[...],"mode":"merge|skip"}`，也接受裸数组）。逐条 `normalize_server` 校验归一，坏条目记入 `errors` 继续处理，单次上限 500 台；同名 `merge` 覆盖 / `skip` 跳过，落库保持原有顺序、新机追加在后，写审计 `server.import`。前端服务器墙工具栏新增「导出 JSON / 导出 CSV / 批量导入」，导入弹层支持选文件或直接粘贴。
- **远端上传目录 `upload_dir`**：服务器配置新增字段（弹层可填，导入 / 导出自动携带，CSV 加「上传目录」列）。上传时作为 `dir` 参数传给 upload_server v3.3.0+（单片走 `chunk/put` 查询串，分片走 `init` / `complete` JSON 体）；目标机需 `--allow-client-dir` 启动，否则远端静默回落默认目录——前端按响应里的真实落盘路径校验，不一致时每台提示一次。

**v2.0.9 变更（安全加固 + 事件流健壮性）**

- **横向越权（IDOR）收敛**：新增 `require_server(req, name)`，服务器详情 / 状态 / 增删改 / 文件浏览 / 通用转发 / 终端执行等**所有「按名接口」**先校验 `can_access_server`；受限账号不再能操作未授权服务器，批量删文件改为逐台校验并跳过无权目标。
- **凭据脱敏**：新增 `redact_server(req, item)`，无 `server.edit` 权限者读服务器列表 / 详情时**不再下发目标机 `upload_token` / `shell_token`**；`/api/settings` 对无 `settings.edit` 者同时清空 `release_pass` 与 `release_token`。
- **POST 体必须是 bytes**：转发给上游的 POST 体统一走 `compat.json_bytes`（py3 下 `conn.send(str)` 会抛 `TypeError`；py2 下非 ASCII 也失败）——修复后 v2.0.7 的「URI 不再出现 `cmd=`」才真正生效。
- **事件流防重放**：`raw_sse_proxy` 改为三态返回 `'ok'/'nogood'/'partial'`，只有「未写出任何事件」才允许 GET / 同步降级，避免「重启服务」这类非幂等命令被重复执行；上游 EOF 立即退出，消除空 read 导致的 sleep+continue 死循环（线程 / 连接泄漏）。
- **其它**：复用连接时重设 socket 超时；`csv_response` 分 py2(`BytesIO`)/py3(`StringIO`)；新增 `maybe_json`（`'0'` 视为空体）与 `opt_float`，修复批量探测 / 通知自测 / 审计区间在空体与非法参数下的 500。

---

### 4.2 release_center.py —— 发布中心 v1.3.3

管理机侧独立服务，页面 `templates/release_center.html`，数据目录 `data/release/`（apps / packages / orders / audit）。

**v1.3.3 变更（安全加固 + 流水线健壮性）**

- **参数注入收敛**：发布单参数值新增白名单 `PARAM_VALUE_RE`（`^[A-Za-z0-9._/@:+=,~ -]{0,256}$`）——参数原样拼进目标机命令的 `${key}` 占位符，此前填 `3.2.0; rm -rf ~/data` 可注入；现建单即拒可疑值。
- **SSRF 收敛**：制品导入 URL 走 `safe_import_url`，仅允许 http/https 且拒云元数据地址（`169.254.*`、`metadata.google.internal`）。
- **越权收敛**：新增 `require_targets(req, names)`，建单 / 执行 / 回滚都校验操作者对各目标机的 `can_access_server`；服务器列表用 `filter_servers` 过滤。
- **备份退出码归一（Windows）**：robocopy 的「成功」码是 1~7（非 0），此前整条 `&` 链会以 1~7 作为进程退出码 → 备份成功却被判失败、触发无谓回滚；现链尾显式 `exit /b 0`（errorlevel ≥ 8 仍先 `exit /b 1`）。
- **备份不再吞错（POSIX）**：去掉 `cp -a … 2>/dev/null || true`，改为「目标目录不存在才视为无需备份，其余 cp 失败一律 exit 1」，避免「备份为空却记成功、回滚时才知无可用备份」。
- **发布单不再卡 running**：`process()` 收口所有非 `StepFailed` 的意外异常（如 `os.path.getsize` 抛 `OSError`），把 run/步骤置为 failed，避免异常逃出任务线程导致发布单永久停在 `running`（此后 409 无法重跑）。
- **连点不再跑两遍**：`api_orders_run` 的「running 检查」与「置 running」放进同一临界区（`ORDER_LOCK`），连点两次「执行」不再并发跑两遍。
- **py2 崩溃修复**：`normalize_vars` / `validate_order_params` / `step_named_cmd` 里 `u'…' % …` 的参数统一 `compat.to_text` 包裹，避免中文标签在 py2 隐式 ascii 解码崩溃。

**v1.3.2 变更**

- **流式执行改走 POST（与 web_shell v1.3.0 配套）**：发布步骤执行命令改 `POST /api/exec/stream`（命令在 body、`X-Auth-Token` 走请求头），URI 上不再带 `cmd=`；上游不支持 POST 时自动退回旧 `GET` 查询串，再不行退同步 `/api/exec`。**退避链带防重放**：仅当「建连失败且**一个事件都没收到**」时才重试 GET，否则「重启服务」这类命令会被执行两次。
- **上传目录按请求指定**：`/api/chunk/put|init|complete`、`/api/upload` 支持传 `dir`（绝对路径），配合 upload_server v3.3.0 的 `--allow-client-dir` 按次落盘；步骤首包落地后会比对「声明目录 vs 真实目录」，不一致时给出「开 `--allow-client-dir` / 重启 `--upload-dir`」双解法提示。

**使用流程（从零到一次上线）**

```
① 启动        python release_center.py --port 8094 --token <T> --platform-url http://<平台IP>:8095 --platform-token <PT>
              （--token 供平台/CI 免登录调用；--platform-url 用于把步骤状态推回平台）
① 平台侧      「设置」填 发布中心地址 + 上面那个 <T>（或改填管理员账号/口令，平台自动登录）
② 纳管目标机  平台「服务器墙」录入目标机（web_shell:8088 负责执行命令、upload_server:8089 负责收包）
③ 定义应用    「应用」里配 target_dir / 包名模板 / 备份目录与保留份数 / 停启替换命令 / 健康检查
              （标准九步部署可直接载入 GET /api/presets 的 script_chain 模板，再按现场改路径）
④ 声明参数    应用 param_defs 里声明 key/label/default/required（如 Version_All、NPkg），命令里用 ${key} 引用
⑤ 传制品包    拖拽 / 多选 / 整目录上传（并发队列 + 逐个进度，可按 MD5 跳过重复包），或从目标机上传服务器补齐
⑥ 建发布单    选 应用 + 版本 + 制品包（可多选）+ 目标服务器（可多台）+ 串行/并行 + 填参数
⑦ 执行        平台「发布中心」点「▶ 执行发布」；页面按步骤实时显示状态/耗时/日志（SSE）
⑧ 失败处理    任一步硬失败 ⇒ 自动回滚最近备份 → 启服 → 复检（状态 rolled_back）；也可手动「↺ 回滚」
```

**默认发布流水线**（逐台执行，串行/并行可选）：

```
上传包(可多个包) → 校验远端 MD5 → 备份旧版(时间戳目录, 按 backup_keep 清理) → 停服
      → 替换(release_cmd) → 启服 → 健康检查(port/http/cmd, 可重试)
      └─ 任一步失败 ⇒ 自动回滚最近备份 → 启服 → 复检 ⇒ 状态 rolled_back
```

**自定义步骤链（v1.1+）**：应用定义里可用 `steps` 完全替换默认流水线，每步：

```json
{"name": "backup", "label": "自定义备份", "cmd": "./version_backup.sh",
 "continue_on_fail": false, "timeout": 600, "cwd": "/home/xpe/app"}
```

- `cmd` 支持 `@upload` `@stop` `@backup` `@replace` `@start` `@health` 六个内置动作（占位到框架原生能力），其余按 shell 命令经目标机 web_shell 执行；
- `continue_on_fail: true` 为**软失败**（步骤标 failed 但流水线继续），硬失败仍自动回滚；
- `GET /api/presets` 提供可直接载入的模板，含 **九步标准部署链**（拉版本包 → `app_stop.sh` → `version_backup.sh` → `update_before_dist.sh` → tar 解包 → `xpe_pre_install_ccbh.sh` → `update_after_dist.sh app` → `app_start.sh`）。

**发布参数（v1.2+）**：应用定义 `param_defs`（`key / label / default / required`）声明参数，创建发布单时传 `params` 覆盖；命令里的 `${key}` 会被替换。变量合并优先级：**内置变量 > 发布单 params > 应用 vars**；缺必填或传未知键均返回 400。

**多包发布（v1.3+）**：一个发布单可挂多个制品包（`packages` 数组，上限 32 个；`package` 保留为第一个包以兼容旧模板）。`@upload` 会把它们全部推到目标机，并注入：

| 变量 | 含义 |
|---|---|
| `${package}` | 第一个包的远端路径（兼容单包模板） |
| `${packages}` | 全部包的远端路径，空格分隔 |
| `${package1}` … `${packageN}` | 逐个点名引用（如主程序包 + 配置包） |
| `${package_count}` | 包数量 |

**批量上传（v1.3+）**：前端支持拖拽（含文件夹递归）、多选文件、整目录上传；后端按「先落 `.part` 临时文件 → 算 MD5 → 再决定去留」处理，因此中断上传不会留下半个脏包，`skip_dup=1` 可跳过内容相同的包（整目录重复上传不会堆出 `a_1.tgz`、`a_2.tgz` 副本），并记录来源相对路径 `src` 便于溯源。

- **应用定义**：target_dir、包名模板（`${version}`）、备份目录/保留份数、停/启/替换/回滚命令、健康检查（port / http / cmd + 重试次数与间隔）。
- **制品包**：流式上传落盘（不占内存）、自动 MD5（兼容 GNU `md5sum` 的反斜杠转义行）、按应用列表、删除。
- **发布单**：pending→running→success / failed / rolled_back / canceled；每步骤含状态、消息、耗时、日志；`GET /api/orders/<id>/stream` SSE 实时推送；手动回滚幂等（无备份时明确报错）。
- **联动**：每步骤状态变化 `POST <platform_url>/api/events/push` 推给平台（失败静默重试一次）。
- **鉴权与审计**：admin/ops/viewer 三角色 + `--token` 开放令牌；全量写操作哈希链审计（`/api/audit`、`/verify`、`/export.csv`）。

> ⚠️ **Windows 目标机写自定义命令的坑**：cmd 里 `if 条件 cmd1 & cmd2` 的 cmd1/cmd2 **整行都属于这个 if 的语句体**，条件为假时后面的命令会被一起跳过（返回码仍是 0，静默失效）。所以务必给 if 加括号：
> - ✅ `(if not exist "${target_dir}" mkdir "${target_dir}") & copy /Y "${package}" "${target_dir}\app.bin"`
> - ❌ `if not exist "${target_dir}" mkdir "${target_dir}" & copy /Y ...`（目录已存在时不会替换）
>
> 发布中心自身的备份/回滚命令早期也踩过这个坑（二次发布不备份、回滚静默不还原），已在 v1.3.0 修正，并加了静态兜底扫描 + 集成回归（非首次发布、回滚内容还原）。

**命令行参数**：`--host --port(8094) --token --data-dir --pidfile --platform-url --platform-token --admin-password --no-login`；环境变量 `RC_HOST` `RC_PORT` `RC_TOKEN` `RC_DATA` `RC_PLATFORM_URL`。

**主要接口**：`/api/apps` CRUD、`/api/packages`（列表/上传/删除）、`/api/servers` CRUD + `import`、`/api/orders`（创建/执行/回滚/取消/详情/流）、`/api/events`、`/api/audit`。

---

### 4.3 upload_server.py —— 文件/文件夹上传服务器 v3.3.1

适用于麒麟 / Linux 服务器，**仅标准库**。页面自带登录、拖拽上传（文件+文件夹）、实时进度、目录树与磁盘监控。

**v3.3.1 变更（安全与健壮性加固）**

- **任意目录写收敛**：`is_forbidden_dir()` 拒绝根目录 / 盘符根 / 系统目录（`/etc`、`/proc`、`/sys`、`/root`、`/boot`、Windows 目录等）/ UNC 共享 / Win32 长路径前缀——此前 `--allow-client-dir` 下 `dir` 只要是绝对路径（含 `/etc`）就放行；分片落点再做 `is_within` 兜底。
- **`/api/status` 脱敏**：未授权时只回基础字段（version / uptime / allow_client_dir / token_required / server_time），不再泄露上传目录 / PID / 磁盘 / 文件数；仍回 200 以保留「200=在线」探活语义。
- **keep-alive 残留**：`_send_json` 在 `status≥400` 且 `Content-Length>0` 时置 `close_connection`，错误分支（401/400）不再把残留 body 当下一请求。
- **下载 Range**：支持 `bytes=-N` 后缀区间（取最后 N 字节）；错误分支用 `started` 标志区分「已发头则关连接，否则 500」。
- **分片索引上界**：校验 `total_chunks`，offset 模式不再能被 `seek(9999*chunk_size)` 分配 TB 级空洞文件；`_save_chunk_legacy` 增加写入长度校验与 tmp 清理；`/api/control` 限制请求体 ≤1MB；批量打包下载跳过隐藏文件。
- **py2 崩溃修复**：`resolve_base_dir` 的 `u'…' % …` 用 `compat.to_text` 包裹。

**v3.3.0 变更：请求级上传目录覆盖（`--allow-client-dir`）**

- 起因：`--upload-dir` 在启动时写死，运维 / 管理页无法按用户或按次改路径。
- 新增 `--allow-client-dir`（环境变量 `UP_ALLOW_CLIENT_DIR`）。**显式开启**后，以下入口接受请求级 `dir`（必须是**绝对路径**）覆盖本次落盘目录：`POST /api/chunk/put?dir=`、`POST /api/chunk/init`（JSON `dir`，`complete` 未带 dir 时沿用 init 记录）、`POST /api/chunk/complete`、`POST /api/upload`（表单 `dir`）。
- 相对路径 `dir` 直接 **400**，不会静默回退到 upload-dir——避免「配了目录却落到别处」的隐性坑。
- 所有上传响应都会带回**真实落盘路径**（`path` / `dir` / `upload_dir`），调用方可据此提示或校验「到底存哪了」。
- `GET /api/status` / `GET /api/health` 暴露 `allow_client_dir`，调用方先探测能力再决定是否传 `dir`。
- 关闭开关（默认）时 `dir` 一律忽略，行为与旧版本完全一致。

**v3.2.1 变更：分片落盘模式可选，默认改为 chunkfile（修上传慢）**

分片落盘有两种模式，用 `--chunk-write-mode`（或环境变量 `UP_WRITE_MODE`）切换：

| 模式 | 做法 | 特点 |
|---|---|---|
| **`chunkfile`（默认）** | 每片写独立文件（片内顺序写）→ `complete` 时按 index 顺序拼接，先写 `.part` 再原子改名 | **各文件系统上最快的写入模式**：并发分片之间不争抢同一文件内的块分配；拼接优先走系统 `cat`（内核态 `copy_file_range`/`sendfile`，NFS 上还可能变成服务端复制），不可用时回退 8MB 缓冲拷贝。代价是 complete 多一次顺序读+写 |
| `offset`（v3.1.1 引入，现为可选） | 预分配单个稀疏文件，各片按 offset 直写；`complete` 只需 rename | 省掉拼接，但**并发随机偏移写 + `ftruncate` 预分配**在 NTFS / NFS / 部分网络盘上会慢一个数量级 |

> 实测（256MB / 16MB 分片 / 6 并发，同一台机器）：`chunkfile` 上传 336~553 MB/s，`offset` 曾出现 40 MB/s、串行甚至 7.5 MB/s。因此默认切回 `chunkfile`。
> **不确定选哪个？在真实服务器上跑 `python verify_upload_perf.py`**，它会自带临时实例、给出磁盘基线与两种模式的对比和切换建议（脚本会清理现场，不动现网数据）。

**v3.2.2 变更：并发落盘健壮性 + 上传速率可观测**

- **修并发同名 / 同分片重传导致的 500**：落盘临时文件名带随机后缀（`<名>.part-xxxxxxxx`），最终名在锁内「查重 + 改名」，`os.replace` 增加瞬时占用重试。此前所有请求共用 `<目标名>.part`，批量小文件并发上传时会互相占用临时文件（Windows 报 `WinError 5 / 32`），部分文件直接 500、重试后仍失败。
- **修 `relative_path` 只给目录时的文件名错乱**：`relative_path` 写成目录（`a/b` + `filename=c.txt`）时按「目录 + filename」落盘；写成含文件名的整路径（`a/b/c.txt`，页面上传即此风格）行为不变。此前一律取相对路径最后一段当文件名，脚本式调用会把文件存成目录名并互相覆盖。
- **访问日志带服务端实测速率**：上传请求追加「本片字节数 + 速率」，如 `POST /api/chunk/upload ... 200 -  [16.0MB 104.6MB/s]`。客户端显示 15MB/s 而这里记到 100MB/s ⇒ 瓶颈在链路/客户端；两边都只有 15MB/s ⇒ 看服务端盘位与 CPU。
- **`GET /api/health` 上报能力**：返回 `version / chunk_size / chunk_write_mode / token_required`；页面据此决定分片大小，`--chunk-size-mb` 现在能真正生效（不必再改页面里的常量）。
- **py2 读缓冲**：`rbufsize=1MB`。py2 下 `socket.makefile` 默认只有 8KB 缓冲，读满 1MB 分片要 128 次 `recv`，并发分片时系统调用与 GIL 切换开销明显。
- **`--sock-buf-mb`**：显式设置 TCP 收发缓冲，默认 0 = 内核自适应（千兆内网通常已满速，显式设置反而会关掉自动调优）；仅高时延链路 / 容器内 `rmem_max` 偏小 / 无线导致接收窗口成为瓶颈时才调大。

**原 v3.2 新增**
- **目录树浏览**：`GET /api/tree?path=&depth=`，页面左侧树形侧栏 + 面包屑导航。
- **列表分页/筛选/排序**：`/api/list` 新增 `q / sort(name|size|mtime|type) / order / page / size / type / time_from / time_to`，返回 `total / page / pages`；每个文件带 `type` 分类（dir/archive/jar/excel/doc/image/log/text/binary），`GET /api/types` 返回枚举。**不带新参数时保持旧返回结构**，旧客户端不受影响。

**原有核心能力（保留）**
- 分片上传 / 断点续传：`init` 建 `__done` 位图，分片写盘并按片置位、`complete` 校验分片齐全后落地（`chunkfile` 拼接 / `offset` 改名）；单片小文件走 `/api/chunk/put` 一次往返。
- 批量删除、批量打包下载（zip 流式）、Range 断点续传下载、磁盘监控。
- 令牌认证、路径穿越防护、`POST /api/control` 远程启停（需 admin-token）。

**命令行参数**：`--host --port(8089) --upload-dir(./uploads) --token --admin-token --pidfile --max-upload-mb(0) --chunk-size-mb(16) --chunk-write-mode(chunkfile|offset) --sock-buf-mb(0) --allow-client-dir`（完整用法示例见 `tools/upload_server.py` 文件头【用法示例】，或 `--help`）；环境变量 `UP_HOST` `UP_PORT` `UP_DIR` `UP_TOKEN` `UP_ADMIN_TOKEN` `UP_MAX_MB` `UP_CHUNK_MB` `UP_WRITE_MODE` `UP_SOCK_BUF_MB` `UP_ALLOW_CLIENT_DIR` `UP_TEMPLATE_DIR`。

---

### 4.4 web_shell.py —— 浏览器远程命令执行 v1.3.1

**仅标准库**，终端风格。页面顶部「单次执行 / 会话终端」双 Tab。

**v1.3.1 变更（安全与 py2 兼容修复）**

- **命令替换不再绕过白名单**：白名单模式下直接拒绝 `$(...)` 与反引号（`__SUBCMD__` 占位符不再豁免）——此前 `echo $(whoami)` 一类会被替换成占位符跳过匹配，白名单形同虚设。黑名单改为「压缩空白后二次匹配」，防 `rm   -rf /` 空格绕过。
- **py2 崩溃修复**：`popen_stream` 前把 unicode 命令转 bytes（py2 下 `subprocess` 传 unicode 中文参数会 `UnicodeEncodeError`）。
- **会话返回码**：进程未退出时不再把 `None` 当返回码（`returncode if not None else -1`）。

**v1.3.0 变更：命令改走 POST + 令牌走请求头（消除「URI 上带命令」的误报与泄露）**

- 单次执行首选 `POST /api/exec/stream`：命令放 **JSON 请求体**、令牌放 **`X-Auth-Token` 请求头**，响应仍是 SSE（`start / out / err / exit`）。旧的 `GET /api/exec/stream?cmd=&token=` 保留兼容但不再首选。
- 会话执行新增 `POST /api/sessions/<id>/stream`，同样命令在 body。
- **执行券（ticket）旁路**：浏览器 `EventSource` 无法自定义请求头，故提供两步法——先 `POST /api/exec/start`（命令在 body、令牌在 header）换取一次性随机券，再 `GET /api/exec/stream?ticket=xxx` 取流；券 60s 有效、**用后即焚**、绑定来源 IP。
- 缘起：`GET .../exec/stream?cmd=whoami&token=...` 与 webshell 流量特征同形，会被态势感知 / IDS 误判为命令执行告警。改为 POST 后 **URI 不再携带命令与长期令牌**：既消除误报，也让令牌不再落进访问日志 / 代理 / 安全设备记录——是「消除误报」而非「对抗监控」。

**v1.2 新增**
- **单次命令 SSE 实时输出**：`GET /api/exec/stream?cmd=&timeout=`，事件 `start / out / (err) / exit`；客户端断开自动 kill 进程。
- **持久 Shell 会话**（解决 `cd`、`export` 每次丢失状态）：
  - `POST /api/sessions` 创建常驻 shell（Linux 用 bash/sh，Windows 用 cmd）；通过「回显随机结束标记」判定命令完成并解析退出码；
  - `POST /api/sessions/<id>/exec`（阻塞，返回 output/returncode/cwd）、`GET /api/sessions/<id>/stream`（SSE 版）、`GET /api/sessions` 列表、`DELETE /api/sessions/<id>` 关闭；
  - 空闲自动回收（`--session-ttl`，默认 30 分钟），会话数上限（`--max-sessions`，默认 8）；
  - **白/黑名单对会话内每条命令同样生效，且校验发生在发送给 shell 之前**。
- **结果导出**：`GET /api/export` 纯文本下载；页面「导出本次输出」按钮。
- 兼容：`/api/exec`、`/api/status`、`/api/ping`、`/api/health` 路径与返回不变。

**保留的安全能力**：`--token` 鉴权、`--allow` 白名单（按管道/控制符分段校验首个可执行词，展开 `$(...)`、反引号防嵌套绕过）、`--deny` 黑名单、超时 kill 进程组、输出 512KB 截断、并发上限 8、审计日志、CORS、PID 文件。

**命令行参数**：`--host --port(8088) --token --allow --deny --cwd --timeout(30) --pidfile --session-ttl(1800) --max-sessions(8)`；环境变量 `WS_HOST` `WS_PORT` `WS_TOKEN` `WS_TEMPLATE_DIR`。

---

### 4.5 diff_file_dir_jar_excel.py —— 文件比对工具 v2.1

单文件 Flask 后端 + 独立页面，依赖仅 Flask + openpyxl（`requirements.txt` 按 Python 版本自动选版）。

**v2.1 变更（正确性 + py2 兼容修复）**

- **Jar 条目比对不再只看 CRC32**：CRC32 + 大小命中后追加 **SHA-1 内容复核**（`_entry_sha1`）——CRC32 只有 32 位且可人为构造，此前内容不同的条目可能被静默判为「相同」（比对工具最危险的一类误判）。三向比对的 jar 等值判定同样从 CRC 改为 SHA-1。
- **Excel 三向 `common_conflict` 修复**：`get_eq` 原先恒返回 `None`，导致「共同(冲突)」永远为 0、永不显示；现按 sheet 内容签名（`_sheet_signatures`）判断左右是否同值。
- **py2 编码崩溃修复（D1~D3）**：本模块的 `to_text` 在 py2 返回 unicode，而大量错误提示 / 报告模板是**含中文的字节串字面量**，`'中文 %s' % unicode` 在 py2 会按 ascii 解码崩溃（文本 / jar / docx / pptx 比对与报告导出全部 500）。现已把格式化上下文中的中文字面量统一改为 unicode 字面量，并对异常对象 / zip 条目名 / 文件路径等**在 py2 必为字节串**的实参补 `to_text()` 归一。

**v2.0 新增**
- **三向比对**：`POST /api/compare/three`（基线 / A / B），聚合出基线独有、A 独有、B 独有、A/B 共同变更及差异方向；前端三列视图。
- **docx / pptx 结构化比对**：`POST /api/compare/ooxml`，解包 OOXML 提取 `word/document.xml` 段落与 `ppt/slides/slideN.xml` 文本流做段落级差异；解析失败（加密/损坏）自动降级二进制摘要。
- **批量目录比对**：`POST /api/compare/batch`，多对路径汇总矩阵；>3 对自动转异步任务（`/api/tasks/<id>` + SSE 进度）。
- **忽略规则可视化**：`/api/ignore/rules` 增删改查（scope: all/dir/jar/text，含内置默认规则如 `*.log`、`META-INF/*.SF`、`docProps/`），`/api/ignore/test` 试匹配；比对接口支持 `ignore=` 临时规则与 `use_saved_rules`，命中项计为 ignored 并单列清单。
- **历史持久化**：每次比对摘要写 `tools/data/history.jsonl`，`/api/history` 分页/筛选/详情/删除，过期会话可按原参数「重新执行」。
- **导出**：`POST /api/export/report`（自包含 HTML / Markdown 报告）、`POST /api/export/patch`（unified diff 补丁）。

**原有能力（保留）**：自动类型识别路由到 dir/text/jar/excel 比对器；Jar CRC32 条目级比对 + 嵌套归档展开 + `.class` javap 反汇编；Excel 逐单元格比对（data_only 可切）；文本行级对照 + 行内字符高亮；`COMPARE_TOKEN` 鉴权、`COMPARE_ALLOW_ROOTS` 路径白名单、zip 炸弹防护、上传体积上限、30 分钟结果会话。

**环境变量**：`PORT(8087)`、`FLASK_DEBUG`、`COMPARE_TOKEN`、`COMPARE_ALLOW_ROOTS`、`COMPARE_MAX_UPLOAD_MB(200)`。

---

## 五、opslib 公共层

四个原脚本各自复制的 py2/py3 兼容层已统一抽取为 `opslib/`，12 个模块全部仅标准库、双版本兼容：

| 模块 | 职责 |
|---|---|
| `compat` | py2/py3 兼容（分叉导入、to_bytes/to_text、ThreadingHTTPServer、start_daemon、run_cmd、safe_join 等） |
| `store` | JSON 落盘存储（原子写、节流保存、加载合并） |
| `auth` | 多用户 / 角色（admin/ops/viewer）/ 权限点 / 会话令牌 / 按服务器授权；口令摘要 PBKDF2-HMAC-SHA256，**恒以 hex 文本落盘**（py2 无 `bytes.hex()`，必须走 `binascii.hexlify`），`verify_password` 兼容历史原始二进制记录，`diagnose()` 启动自检损坏账号 |
| `apibase` | Request/Response 基类（查询串解析、JSON body、SSE 响应、统一错误） |
| `server` | HttpApp 声明式路由（`@app.get/post/...`，`perm=` 权限点、`summary=`）、静态资源、OpenAPI 生成 |
| `sse` | 进程内事件总线 + SSE 流（主题订阅、历史重放、断连清理） |
| `httpx` | 零依赖 HTTP 客户端（连接池、JSON/二进制、流式读取） |
| `probe` | 主机指标采集（/proc 解析为主，非 Linux 优雅降级） |
| `audit` | 哈希链防篡改审计日志（追加式 JSONL、链校验、CSV 导出） |
| `task` | 异步任务管理（队列、取消、日志增量、SSE 推送、持久化） |
| `notify` | Webhook 通知（事件订阅、脱敏、连通性自测） |
| `crypto` | 令牌/口令哈希、脱敏展示 |

自检：`python opslib/selftest.py`（115 项，覆盖全部模块）。公共层用法详见 `opslib/README.md`。

> **v1.0.8 重要修复（Python 3.5~3.8 必读）**：`compat.urlunquote` 老实现对整个请求路径无条件 `to_bytes()` 再交给 `urllib.parse.unquote`，而该函数在 **Python 3.9 之前只接受 str**，传 bytes 会抛 `TypeError: Expected str, got bytes`。后果是 **py3.8 环境下每一个请求（含首页 `GET /`）都在 `server.dispatch` 阶段 500**，服务完全不可用——而 3.9+ 放宽了类型检查，本地/CI 不复现，极易被误判为「只有那台机器有问题」。升级只需同步 `opslib/compat.py` 并重启；`selftest` / `verify_py2_compat` 已新增固化用例（把 `unquote` 换成严格代理，在任何 py3 上拦截回归）。

---

## 六、前端页面与代码分离

| 后端 | 页面文件 | 占位符 |
|---|---|---|
| `ops_Platform.py` | `static/index.html` + `app.css` + `app.js` | `{{version}}`、`{{need_login}}` |
| `release_center.py` | `templates/release_center.html` | `{{version}}` |
| `upload_server.py` | `templates/upload_server.html` | `{{upload_dir}}`、`{{version}}` |
| `web_shell.py` | `templates/web_shell.html` | `{{version}}`、`{{default_chips}}` |
| `diff_file_dir_jar_excel.py` | `templates/diff_file_dir_jar_excel.html` | — |

页面按 mtime 缓存，**改完刷新即生效**。模板查找：环境变量（`UP_TEMPLATE_DIR` / `WS_TEMPLATE_DIR` / `OPS_STATIC`）→ 脚本同级目录 → 当前工作目录；缺失时返回友好提示页。所有前端均为**零第三方库**的原生 HTML/CSS/JS（平台用 Canvas 手绘图表）。

---

## 七、安全说明（重要）

1. **web_shell.py 与 upload_server.py 默认无鉴权**——务必只在受信内网使用，并始终设置 `--token`；web_shell 建议同时配 `--allow` 白名单收敛可执行命令。
2. **平台 / 发布中心默认开启登录**：首启自动生成管理员口令（启动横幅打印），请立即修改；`--no-login` 仅限完全可信内网。三角色最小权限：viewer 只读、ops 可操作、admin 管用户与设置；非管理员只能看到被授权的服务器。
3. **开放令牌（`--token`）等价管理员**，仅用于 CI/CD 脚本调用，妥善保管。
4. **发布流水线会执行停服/启服/替换命令**：命令经目标机 web_shell 执行并受其白/黑名单约束；发布前务必确认应用定义里的命令内容。
5. **审计与回滚**：平台与发布中心的写操作均有哈希链审计（`/api/audit/verify` 可校验是否被篡改）；回滚依赖备份目录，请确保 backup_dir 可写且磁盘充足。
6. **diff 工具**：非本机使用时设置 `COMPARE_TOKEN` 与 `COMPARE_ALLOW_ROOTS`。
7. 部署建议：仅监听内网地址或防火墙限源；反向代理叠加 HTTPS；`--pidfile` 纳入 systemd / supervisor。

---

## 八、兼容性说明（Python 2.7.5+ / 3.7.5+）

统一兼容层已收敛到 `opslib/compat`，主要处理：模块改名分叉导入、自建 ThreadingHTTPServer、`%` 格式化替代 f-string、`os_replace` / `open_text`（py2 带编码）、`to_bytes/to_text/safe_str`、`is_within` / `disk_usage` / `fullmatch` / `which` 等 API 缺失补齐、进程超时等待与进程组 kill、CLIENT_GONE 异常族、`start_daemon` 线程。

**写代码时务必注意 py2 的三个陷阱**（py3 下不复现，本地 py3 测试也测不出）：

1. **`str % unicode`**：py2 会先按 ascii 解码 str，含中文的字节串字面量随即抛 `UnicodeDecodeError`。规则：**任何进入框架的动态值都必须是原生串**——JSON 派生值经 `compat.json_loads`（内部已 `to_native`），URL/请求路径经 `compat.urlunquote`（同样归一为字节串），与 `u''` 字面量拼接处显式走 `compat.to_text()`。
   典型踩坑：`urlunquote` 若返回 unicode，则 `'接口不存在: %s' % path` 会让**任意 404 都变成 500**（日志刷 `'ascii' codec can't decode byte 0xe6 in position 0`），而且与路径里有没有中文无关——炸的是字面量本身。回归兜底：`opslib/selftest.py` 的「URL 解码返回原生串」与 `verify_py2_compat.py` 的「404 错误串可安全格式化」（后者用字节串模板 + unicode 参数显式复现 py2 语义，py3 上也能抓住回退）。
2. **`except OSError` 抓不到 `IOError`**：py2 中两者是**独立类**（仅同源于 `EnvironmentError`），文件不存在/不可读抛的是 `IOError`。凡是 try 体里可能做文件操作的，必须写成 `except (OSError, IOError, ...)`，否则异常会逃逸到 HTTPServer 打出 traceback 并中断连接。已提供静态审查脚本 `python verify_py2_excepts.py` 兜底。
3. **`bytes.hex()` 在 py2 不存在**：py2 的 `bytes` 就是 `str`，而 `str.hex()` 是 **Python 3.5** 才引入的方法，`hasattr(digest, 'hex')` 在 py2 下恒为 `False`。凡是「把二进制摘要转十六进制」的地方都必须显式用 `binascii.hexlify`，否则会返回原始二进制：
   - 写 JSON 时被当作字节串内联，再经 `to_text(..., 'replace')` 解码 → **不可逆的 U+FFFD 损坏**；
   - 按「hex 文本长度的一半」推导摘要长度会得到错误值（32 字节被算成 16）→ 比对必然失败。
   线上表现就是「管理员登录永远提示用户名或口令错误、重置口令也无效」。规则：摘要类字段一律用 `opslib.auth.hash_password` / `_digest_hex` 生成，`verify_password` 已兼容 hex 文本与历史原始二进制两种形态。已提供运行时回归脚本 `python verify_py2_compat.py` 兜底（用「隐藏 `.hex()` 的 bytes 子类」精确复现 py2 语义）。

**性能相关的第四个坑（与 py2/py3 无关，但同样容易踩）：文件写入模式比语言版本更决定上传速度。**
往**同一个文件的随机偏移**并发写（哪怕做了稀疏预分配）在 NTFS / NFS / 部分网络盘上会比「每片写独立文件、片内顺序写」慢一个数量级；`ftruncate` 预分配在 NTFS 上是真实填零、在 NFS 上可能触发 COMMIT，都绝不能放在分片请求的热路径上。规则：分片落盘保持「一片一文件 + 顺序写」，拼接放到 `complete` 一次完成（`cat` / sendfile）；需要改写入策略时，先用 `python verify_upload_perf.py` 在本机实测再改。

**依赖安装（仅 diff 工具）**：`python -m pip install -r tools/requirements.txt`（PEP 508 环境标记按版本自动选：py2.7 → Flask 1.1.4/openpyxl 2.6.4；py3.7 → 2.2.5/3.0.10；py3.8+ → 3.0.3/3.1.5）。

**已知限制**
- Python 3.13+ 移除了 `cgi`：upload_server 的「普通 multipart 表单直传」在 3.13+ 返回明确提示，请走分片上传接口（前端默认路径，不受影响）。
- diff 工具的 `.class` 反汇编需要目标机有 JDK（`javap`），否则降级摘要比对。
- 监控指标在目标机无 host_monitor 探针时，平台经 web_shell 执行 `/proc` 采集命令降级获取；Windows 目标机指标为有限支持。

---

## 九、端口与默认配置速查

| 组件 | 默认端口 | 数据 / 路径 | 令牌 |
|---|---|---|---|
| `ops_Platform.py` | 8095 | `data/`（配置/审计/台账） | `--admin-password` / `--token` |
| `release_center.py` | 8094 | `data/release/` | `--admin-password` / `--token` |
| `upload_server.py` | 8089 | 文件保存 `./uploads` | `--token` / `--admin-token` |
| `web_shell.py` | 8088 | 执行目录 = 脚本目录 | `--token` |
| `diff_file_dir_jar_excel.py` | 8087 | `tools/data/`（规则/历史） | `COMPARE_TOKEN` |

---

## 十、常见问题（FAQ）

**Q：页面打开后服务器墙全是「离线」？**
A：确认目标机 upload_server / web_shell 已启动且端口可达；浏览器无法直连目标网段时把访问模式改为 `proxy`。

**Q：登录页一直弹出来？**
A：v2 起平台默认要求登录。首启管理员口令在启动横幅里（或用 `--admin-password` 指定）；登录后右上角可修改口令。纯内网可信环境可加 `--no-login`。

**Q：用 `--admin-password` 指定的口令登录不上（提示「未授权，请重新登录」）？**
A：`--admin-password` **只在首次启动（`data/users.json` 不存在）时生效**。若之前已启动过（当时横幅打印过首启口令或随机口令），后补的 `--admin-password` 不会覆盖已存的口令。重置方法：重启时加 `--reset-admin-password`，即 `python ops_Platform.py --port 8095 --admin-password <新口令> --reset-admin-password`，启动日志会打印重置结果；重置后用新口令登录。

**Q：重置口令后还是提示「用户名或口令错误」，命令终端也进不去（审计日志连续 `auth.fail`）？**
A：这是 **py2 专属的口令摘要损坏**，与口令是否正确、角色与权限都无关。早期版本用 `hasattr(digest, 'hex')` 判断摘要形态，而 py2 的 `bytes` 就是 `str`、`str.hex()` 要到 Python 3.5 才有，于是落盘的是 **32 字节原始二进制**：写 JSON 时损坏成 U+FFFD，校验时摘要长度又按 hex 的一半算成 16 字节 → **口令永远校验不通过，重置多少次都无效**。现版本已改为恒用 `binascii.hexlify` 输出 hex 文本，并在启动横幅自动自检提示损坏账号。

处置步骤（三步）：
```bash
# 1) 同步这两个文件到服务器（opslib/auth.py 必须更新）
#    opslib/auth.py、platform_console/ops_Platform.py（release_center.py 同理）
# 2) 重启（旧记录损坏时，带 --admin-password 即可自动修复，无需额外加 --reset-admin-password）
nohup python -u ops_Platform.py --port 8095 --admin-password Admin_123! > ./logs/ops_Platform.log 2>&1 &
# 3) 启动日志出现「首启口令 : Admin_123!」或「[auth] admin 口令已修复」即成功
```
若启动横幅出现 `[!] 账号 admin 的口令摘要已损坏…` 而口令未被修复，说明提供的新口令不满足强度要求（至少 6 位），或改用 `--reset-admin-password` 显式重置。修复后旧的损坏记录无法恢复，只能重置。可用 `python verify_py2_compat.py` 在任意机器上回归这套逻辑。

**Q：一直能正常用平台，点了「退出」后就再也登录不上了？**
A：九成是 FAQ 上一条的 py2 口令摘要损坏——**旧会话一直没过期**（`sessions.json` 跨重启保留、TTL 12 小时），所以损坏记录平时暴露不出来；一旦退出/会话过期被迫走真实登录，`verify_password` 永远失败，且用**旧版 `opslib/auth.py`** 反复 `--reset-admin-password` 也只会再写一份损坏摘要。30 秒定位：
```bash
grep -c hexlify opslib/auth.py        # 输出 0 ⇒ auth.py 是旧版，就是它
head -c 500 data/users.json           # "hash" 若是 64 位十六进制文本=正常；乱码/\ufffd=已损坏
```
处置：把**整个 `opslib/`**（至少 `auth.py`）同步到服务器，再用原命令重启一次；启动横幅应出现「首启口令」或不再有「摘要已损坏」提示，然后用 `--admin-password` 指定的口令登录。若 `grep` 输出非 0（auth.py 已是新版）仍登录失败：把登录页红字报错 + `tail -30 logs/ops_Platform.log` 发出来定位。另注意：py2.7.5 无 `hashlib.pbkdf2_hmac`，登录走纯 Python 兜底，单次校验需 **2~5 秒**，点「登录」后请等几秒，勿连点。


**Q：CI/CD 怎么调用？**
A：用 `--token` 生成的开放令牌，请求带 `X-Auth-Token` 头即可调用全部接口；`GET /api/openapi.json` 可导出机读契约给流水线用。

**Q：上传大文件慢或中断？**
A：按「服务端 → 链路 → 客户端」三步定位，别先改代码。

1. **在客户端机器上跑测速诊断**（服务器上不用装东西）：
   `python verify_upload_speed.py --host <服务器IP> --port 8089 --token <令牌>`
   它会依次打印：服务端版本/分片大小/落盘模式、往返延迟、**并发梯度 1/2/4/8 的 MB/s**、单流大分片、小文件批量（文件/秒）。
   - 峰值接近千兆≈110 / 百兆≈11 / WiFi5G≈30~60 / WiFi2.4G≈5~8（MB/s）中哪一档 ⇒ 瓶颈就在那一档，与代码和磁盘无关；
   - 只有 1 路慢、加并发线性上升 ⇒ 单流受带宽时延积限制（VPN/跨机房/无线），并发能补偿，页面已用 6 路；
   - 小文件 MB/s 远低于大分片 ⇒ 受「每文件请求数」限制，属预期，打包上传更划算。
2. **看服务端访问日志的速率后缀**（v3.2.2 起）：`... 200 -  [16.0MB 104.6MB/s]` 是服务端「收包 + 落盘」的实测速率。客户端低、服务端高 ⇒ 链路/客户端侧；两边都低 ⇒ 查盘位与 CPU。
3. **再查落盘模式与盘位**：v3.2.1 起默认 `chunkfile`（每片独立顺序写 + complete 时拼接），这是各文件系统上最稳的模式。在真实服务器上跑 `python verify_upload_perf.py` 可给出「磁盘顺序写基线」与两种落盘模式对比：若上传吞吐 ≈ 磁盘并发写 → 已是磁盘上限（NFS/网络盘/机械盘）；若两种模式差距大 → 用 `--chunk-write-mode <快的那个>` 启动。

补充：跨网段建议 proxy 模式（管理机全流式转发不占内存）；高时延链路可调大 `--chunk-size-mb`（页面会按 `/api/health` 自动跟随，无需改前端）；带宽很足但单流只有十几 MB/s 且确认是容器/低 `rmem_max` 环境时，可试 `--sock-buf-mb 4`。

**Q：上传时报 `IOError: [Errno 2] No such file or directory: .../.tmp/resume/<key>` 并刷一片 traceback？**
A：py2 专属问题：`handle_chunk_init` 读断点记录时 `except` 漏写 `IOError`（py2 中它与 `OSError` 非同类），首次上传即带 `resume_key` 时记录文件尚不存在，异常逃逸中断连接。现已修复并加集成用例回归；升级到当前版本即可。可用 `python verify_py2_excepts.py` 复查同类隐患。

**Q：服务日志每打开一次页面就刷一段 `UnicodeDecodeError: 'ascii' codec can't decode byte 0xe6 in position 0`，定位到 `server.py: dispatch → fail(404, err('接口不存在: %s' % path))`？**
A：py2 专属的编码崩溃（`opslib` v1.0.1 已修复）。`compat.urlunquote` 早先返回 unicode，而 py2 在 `字节串字面量 % unicode` 时会**先按 ascii 解码该字面量**——字面量里有中文，于是必然抛错。后果是**任何 404 都变成 500**（浏览器自动请求的 `/favicon.ico`、静态资源缺失、不存在的接口路径全中），日志被 traceback 刷屏，前端拿到的错误码也随之失真。
现版本把 `urlunquote`（以及 `dispatch` 的路径、静态资源相对路径）统一归一到原生串，并给错误串格式化的动态参数补了 `compat.safe_str`。升级只需同步 `opslib/compat.py`、`opslib/server.py` 两个文件并重启；同步 `opslib/selftest.py` 后可用 `python opslib/selftest.py` 看到「URL 解码返回原生串」一项。

**Q：平台里点「发布中心」打不开/登录报 `[Errno 111] Connection refused`，但单独访问 :8094 是正常的？**
A：平台默认用 `127.0.0.1` 回源发布中心（同机部署的常见配置）。若发布中心是用 `--host` 绑定了某块网卡（或跑在容器/另一台机器上），回环地址就连不上。现版本在回源前会枚举本机网卡地址并自动挑选可达的 base（`release_url` / `release_config` / SSE 透传统一走同一套候选），失败时的 502 响应会直接给出「改成哪个地址、加什么参数」的指引。手工指定：`ops_Platform.py --release-url http://<可达IP>:8094 --release-token <令牌>`。

**Q：在平台「设置」页改「发布中心地址」保存不了 / 地址改了却不生效？**
A：先看是哪种情况：
1）**保存直接报错**（HTTP 500，提示 `处理失败: module 'opslib.compat' has no attribute 'merge_dict'`）：v2.0.1 及更早版本的问题——保存设置用到的 `compat.merge_dict` 从未被定义（典型「只在冷路径触发」的缺陷）。同步 `opslib/` 到 v1.0.2 即可，并新增了静态扫描用例（`python opslib/selftest.py` 里的「compat 引用均有定义」）防止同类问题复发。
2）**保存成功但地址没变化**：启动时用了 `--release-url`，其优先级高于设置页。`GET /api/settings` 会回传 `locked` 标记，设置页会在地址下方明确提示「启动参数 --release-url 优先级更高」，启动横幅也会打印实际生效的地址。
3）**地址写成了 `172.19.196.130:8094`（缺 `http://`）或末尾带 `/`**：旧版会把主机名当成路径，最终打到 `127.0.0.1:80` 报不可达；现版本会自动补 `http://` 并去掉末尾斜杠（`/api/settings` 回传的即是归一化后的值）。
设置页「发布中心地址」下方现在会实时显示**实际使用中的地址**与**授权状态**（在线 / 已连通但未授权 401 / 离线），发布中心离线时页面上的「⚙ 修改发布中心地址」按钮可直接跳到设置页并聚焦到该输入框。

**Q：登录成功后页面马上被退出、反复登不进去（尤其刚配好发布中心地址之后）？**
A：这是 **v2.0.4 修复的「上游 401 被透传」**。发生时日志里能看到：`POST /api/login 200` → `GET /api/me 200` → `GET /api/release/apps 401` 反复循环。
根因有两层：① 平台访问发布中心时**没有带凭据**（既没配「发布中心令牌」，release_center 启动时也没加 `--token`），发布中心按规范回 401；② 平台把这条 401 **原样透传**给浏览器，而前端 `api()` 的规则是「见到 401 = 平台会话失效」→ 立刻清理并弹回登录页。于是登录成功 → 发布视图一加载又被踢出，看起来就像「登不进去」。此前集成用例里平台始终带 `--release-token` 启动，这条分支从未被走到。
现版本两道防线：**平台侧**把上游 401/403 一律翻译成 `502 {code:"upstream_unauthorized"}` 并附处置指引，绝不回 401；**前端侧**只把「非 upstream 的 401」当作平台会话失效。凭据则二选一即可 ——
- **账号+口令（推荐，无需重启）**：设置页填「发布中心账号/口令」＝发布中心的管理员账号（如 `admin` / `Admin_123!`），平台自动登录并把会话缓存到 `data/release_session.json`，会话失效会自动重登；
- **令牌**：release_center 启动时加 `--token X`（或在设置页「发布中心令牌」填 X），适合 CI/CD 无人值守调用。
排查用：`curl -i -H 'X-Auth-Token: <平台令牌>' http://<平台IP>:8095/api/release/config` 会返回 `authorized` 与 `auth_mode`（`token` / `login` / `none`），一眼看出是地址问题、凭据问题还是发布中心没起。

**Q：安全设备 / 态势感知报「webshell 命令执行」，链路里看到 `GET .../exec/stream?cmd=whoami&token=...`？**
A：这是 **URI 命令特征误报**——该 URL 与 webshell 的「命令放查询串」特征同形，被判为命令执行。**正解是让 URL 不再携带命令与长期令牌，而不是去规避监控。** 现版本已按下述方式改造（web_shell v1.3.0 / 平台 v2.0.7 / 发布中心 v1.3.2 配套）：
- 单次执行 `POST /api/exec/stream`：命令在 **JSON 请求体**、令牌在 **`X-Auth-Token` 头**，URL 上只有路径；
- 浏览器 `EventSource` 不能自定义请求头，故加「执行券」两步法：先 `POST /api/exec/start` 换一次性券（60s、用后即焚、绑来源 IP），再 `GET .../exec-stream?ticket=...` 取流；
- 平台转发链路与发布中心调用**同样改 POST**（发布中心退避链带防重放：仅在「建连失败且未收到任何事件」时才重试旧 GET，避免「重启服务」被执行两次）。
副作用收益：长期令牌不再出现在访问日志 / 反向代理 / 安全设备记录里。旧 `GET ?cmd=` 仍兼容，但建议前端一并升级。

**Q：在服务器墙上点「编辑」，改完点保存，看起来「保存不了」（一直转圈 / 刷新后还是旧值）？**
A：这是 **保存后的自动刷新被拖垮，不是保存本身失败**（v2.0.8 修复）。保存接口一直成功；卡的是随后的列表 / 详情刷新——它**串行实时探测**每台机器，离线机每台最多 3 次 × 2s 超时，机器一多就长时间不返回，界面看起来像「没保存上」。
现版本：探测改为 `probe_many` 有界并发（8 线程）+ 15s TTL 缓存（增删改自动失效）。复现与验证：在服务器墙放一台不可达地址的机器，`GET /api/servers` 首次约 6s、二次 0.00s；编辑后立即读详情可拿到新值。集成回归含「含离线机时列表可用（冷启动 <15s）」「二次 <1.5s」「编辑后立即可读」等用例。
若升级后仍卡：看是否有上游 keep-alive 复用导致的读超时（旧版表现为 `status=0`），同步 `opslib/httpx.py`（v1.0.5 起每次取用连接都重设超时）即可。

**Q：上传时配置了 `/home/xpe/app_temp`，文件却落到了 `upload_server.py` 同级的 `uploads/`？**
A：多半是**路径没生效**，按情况分两种：
1）**启动参数写死 `--upload-dir`**（如 `nohup ... --upload-dir ./uploads`），会把落点固定在 `uploads/`，运维 / 管理页无法改。两种解法：① 升级到 upload_server **v3.3.0** 并加 `--allow-client-dir`，由调用方在**请求里带 `dir`（绝对路径）**按次覆盖；② 直接重启并把 `--upload-dir` 写成目标目录。
2）**调用方传的 `dir` 是相对路径**：v3.3.0 起直接 **400**（绝不静默回退到 upload-dir）。改成绝对路径即可。
排查：上传响应会带回**真实落盘路径**（`path` / `dir` / `upload_dir`）；`GET /api/status` 的 `allow_client_dir` 表明目标机是否已开目录覆盖。发布中心与平台的上传步骤也会在首包落地后比对「声明目录 vs 真实目录」，不一致时直接给出上面两条解法。

**Q：在平台页面上传大一点的制品包报 413 `请求体过大(上限 32MB)`？上传小包时「只看所选应用」之类的筛选又不起作用？**
A：两个都出在**平台到发布中心的透传层**（v2.0.5 修复），共同特征是**不报业务错、只在细节上悄悄不对**，很容易被当成「功能没做」。
1. **32MB 上限**：普通透传走 `req.raw_body()`，该方法默认封顶 32MB（且整包读进内存，多个大包并发会打爆平台）。制品包动辄几百 MB，于是直接被 413 挡掉。现改为 `release_upload_proxy` **流式透传**：手工建连、64KB 一片边收边发，内存占用与包大小无关（实测 34MB 直传 0.4s），单列超时 `RELEASE_UPLOAD_TIMEOUT=3600`。
2. **查询串被丢**：`Request.parsed` 是 `urlparse(handler.path[:qpos])`——`dispatch` 为了路由匹配已经把 `?query` 剥掉了，所以 `req.parsed.query` **恒为空串**，真正的查询串在 `req.raw_query`。转发层原先用它拼 URL，导致 `?app=`、`?page=`、`?limit=` 之类**在另一端完全收不到**：页面上「只看所选应用」看起来点了没反应、分页拿到的永远是第一页，两头日志都干干净净。
现版本统一走 `req_query(req)`，并有静态兜底（`python opslib/selftest.py` 里「转发接口不使用 req.parsed.query」——生产代码里一出现就失败）。升级只需同步 `platform_console/ops_Platform.py` + `platform_console/static/`（`app.js` / `index.html` / `app.css`）并重启平台，横幅应显示 **v2.0.5**。

**Q：设置页里根本没有「保存设置」按钮（或其它带权限的按钮消失）？**
A：按钮是按权限点显隐的（`data-perm="settings.edit"`）。v2.0.2 及更早版本 `/api/me`、登录接口返回的是**展开后的权限清单**——若目标机上的 `opslib/auth.py` 是旧版、清单里还没有 `settings.edit` 这个新权限点，后端按 `*` 放行（接口都能调通），前端却认为没权限，把按钮隐藏了。v2.0.3 起 `/api/me` 直接返回角色原始权限（管理员即 `['*']`，与发布中心行为一致），不再依赖清单完整性；`applyPerms` 也改为幂等（权限满足会恢复显示）。同步 `platform_console/ops_Platform.py`、`static/app.js` 后强刷即可。

**Q：发布中心离线时，平台日志刷 `UnicodeDecodeError: 'ascii' codec can't decode byte 0xe4`，定位到 `release_proxy → 'release_center 不可达: ' + ... + hint`？**
A：py2 同族编码崩溃（v2.0.3 已修复）：提示串用**含中文的字节串字面量**与 `u''` 的 hint 做 `+` 拼接，py2 遇 `str + unicode` 会先按 ascii 解码前者（0xe4 是「不」的首字节）→ 502 直接变 500，前端拿不到「发布中心不可达」的指引。现改为全程 unicode 拼接、`to_native` 收口；集成验证新增「杀掉发布中心后 `/api/release/*` 必须返回结构化 JSON 502」的回归用例。

**Q：命令终端里 `cd` 下一条又回去了？**
A：用目标机 web_shell v1.2 的「会话终端」（或平台终端窗口内保持会话），持久会话会保留 cwd 与环境变量。

**Q：发布失败后发生了什么？**
A：流水线在任一步骤失败时自动用最近一次备份回滚（恢复文件 → 启服 → 复检），发布单状态置 `rolled_back`，步骤日志里可查全过程；也可在发布单详情手动回滚。

**Q：改了页面文件为什么不生效？**
A：按 mtime 缓存，刷新即生效；仍不行就 Ctrl+F5 清浏览器缓存。

**Q：diff 提示「不支持老格式 .xls」？**
A：openpyxl 仅支持 `.xlsx / .xlsm` 系，请先另存为 `.xlsx`。
