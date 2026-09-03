# 记忆图谱后端 — 操作手册

`C:\memory_backend` 的操作指南：一个 FastAPI 服务，把个人/组织的**知识图谱以纯文件形式存在 S3 上**（无数据库）。2026-08-28 依据当前代码编写，2026-09-03 更新（实体合并、共享链接、TLS 信任库修复等，见 `PROGRAM_JOURNAL.md` §17-22）。若与 `README.md` 或 `QUICKSTART.md` 冲突，本文件反映的是代码当前的运行状态。

> **如果你是新手，先读这里。** 本文件讲"怎么操作"；`README.md` 是深度设计参考（OKF 格式、写入成本模型、mirage 内部机制）；`PROGRAM_JOURNAL.md` 是历史/决策记录。三者互补；`QUICKSTART.md` 仍旧是最短的"跑起来"路径。

---

## 1. 这个程序是什么

一个 Python/FastAPI 服务，把**对话转化为持久、可查询的知识图谱**。每个实体是一个带 YAML front-matter 的 Markdown 文件（一张 OKF"索引卡"）；"图谱"就是实体及其关系，作为同级文件存于 S3 兼容存储桶（开发时存在本地磁盘目录）。

```
{user_id}/wiki/{type}/{slug}.okf.md        实体（文件名 => 身份）
{user_id}/wiki/_manifest/snapshot.json     所有实体的索引（+ 增量）
{user_id}/wiki/_ops/{date}/*.jsonl         审计写入日志
{user_id}/raw-facts/{date}.jsonl           原始对话事实日志
{user_id}/sessions/...                     记录的聊天记录
{user_id}/notes/{note_id}.json             中期记忆便签
```

**实体文件是唯一事实来源。** 清单（manifest）、操作日志、邻接索引都是从它派生的、可重建的。

### 存储模型（两个词：mirage + 对象存储）

存储始终经由一个 **mirage-ai Workspace** 挂载在 `/s3`：

| `STORAGE_BACKEND` | 挂载 | 用途 |
|---|---|---|
| `mirage`（默认） | `S3Resource` | 真实部署（需 `MIRAGE_S3_BUCKET`） |
| `disk` | `DiskResource` | 离线开发/测试（写入 `./local_bucket/`） |

两者是**同一条代码路径**（`MirageBackend`），只是挂载资源不同，所以本地跑的就是要上线的。`s3` 被当作 `mirage` 的别名，`local` 当作 `disk` 的别名，旧配置仍可用。

### 它不是什么

- **不是数据库。** 没有行/表；存储是对象读写。
- **不是向量库。** 本仓库没有 embedding/向量索引。检索只靠关键词 + 图谱遍历（见 §8）。
- **chat /extract 不会隐式写入。** 写入走显式的、可人工复核的两步路径（plan → apply），或通过可选开启的自动捕获（§6）。

---

## 2. 架构图（运行中的代码）

**正在运行的应用程序在 `app/` 下。** `app/main.py` 构建 FastAPI 应用；`app/deps.py` 装配单例。

> 此前存在一个未接入运行中应用的孤立重写（`core/`、`api/`、`clients/`），已于 2026-09-03 确认零依赖后移除（见 `PROGRAM_JOURNAL.md`"孤立的重写——已移除"）。运行中的代码 100% 在 `app/` 下。

```
app/
  main.py                 应用工厂、lifespan（自动捕获定时器 + 关闭时排空）
  config.py               基于 env 的 Settings（见 §3）
  deps.py                 依赖注入：backend、图谱存储、raw log、notes log、LLM、extractor
  auth.py                 bearer token 认证（静态 token + AUTH_MODE=off）
  users.py                用户名/密码账号 + 无状态会话 token
  storage/
    backend.py            StorageBackend 接口（+LocalFSBackend 测试替身）
    mirage_backend.py     唯一的真实后端（mirage Workspace）
    writebehind.py        缓冲的 manifest/ops 刷新
  graph/
    store.py              EntityGraphStore：OKF 实体、事实、关系
    manifest.py           WikiManifest（索引、缓冲、增量链）
    ops_log.py            WikiOpsLog（分段审计日志）
    title_resolver.py     去重/合并 + inbox
    conflicts.py          Semantica 支撑的冲突记录（仅标记）
    semantica_wrap.py     冲突辅助
  wikis/
    registry.py           多用户 wiki + 显式访问授权
    router.py             WikiRouter：确定性、无 LLM 的 wiki 路由
  rawlog/
    log.py                RawFactLog（分片 JSONL）
    sessions.py           记录的会话记录
    notes.py              中期记忆便签
  extract/
    extractor.py          ConversationExtractor（assess → LLM plan → apply ops）
    llm.py                LLM 客户端（OpenAI 兼容，默认 DeepSeek）
  verify/
    assessor.py           非 LLM 的"这段值不值得记？"打分
  autocapture/
    scheduler.py          定时驱动的每日捕获（与 /extract 同一条管线）
    timer.py              后台定时器（除非 AUTO_CAPTURE_ENABLED，否则关闭）
  intents/
    executor.py           LLM 发布的结构化 CRUD 意图
    retract.py, semantica_crud.py
  shares/
    store.py              ShareLinkStore（扁平全局键 `_shares/{id}.json`）
    scope.py              连通分量计算 + 抽取计划的范围收窄（见 §6.4）
  api/
    routes_*.py           每个功能组一个 router（见 §7）
  static/
    auth.js, chat.html, gui.html, shared.html    浏览器 UI（含 guest 专用页）
```

---

## 3. 配置（`.env`）

一切都在 `.env` 里。应用启动时自动加载（`app/main.py` 顶部的 `python-dotenv`）。当前线上 `.env` 的键（非秘密值）：

| 键 | 用途 |
|---|---|
| `STORAGE_BACKEND` | `mirage`（生产）或 `disk`（开发） |
| `MIRAGE_S3_BUCKET` | 存储桶名（生产） |
| `MIRAGE_S3_REGION` / `MIRAGE_S3_ENDPOINT_URL` | 区域 + 网关（如七牛 Kodo） |
| `MIRAGE_S3_ACCESS_KEY_ID` / `MIRAGE_S3_SECRET_ACCESS_KEY` | 凭据 |
| `MIRAGE_S3_PATH_STYLE` | 非 AWS 网关设为 `true` |
| `MIRAGE_S3_KEY_PREFIX` | 默认 `memory_backend/` |
| `MIRAGE_INDEX_TTL_SECONDS` | **必须保持 `0`**（见 §11） |
| `MIRAGE_REUSE_CONNECTIONS` | 保持 `true` |
| `AUTH_MODE` | `token`（生产）或 `off`（仅开发） |
| `AUTH_SECRET`、`AUTH_SESSION_HOURS` | 密码登录的会话 token 签名 |
| `DEEPSEEK_API_KEY`、`LLM_MODEL`、`LLM_MAX_TOKENS`、`LLM_MIN_DECISION` | LLM 抽取 |
| `FLUSH_INTERVAL_SECONDS`、`FLUSH_MAX_PENDING` | 写后缓冲的持久性窗口 |
| `OKF_MODE` | `companion`（默认）或 `frontmatter` |
| `AUTO_CAPTURE_ENABLED`、`AUTO_CAPTURE_TIME`、`AUTO_CAPTURE_TZ`、`AUTO_CAPTURE_LOOKBACK_DAYS`、`AUTO_CAPTURE_BACKFILL_DAYS` | 自动捕获调度。`LOOKBACK_DAYS=0`（当前默认）= 当晚捕获当天的日志；`=1` 才是"捕获昨天" |
| `WIKI_CREATE_REQUIRES_ADMIN`、`CONFLICT_CHECK_ENABLED`、`CONFLICT_RESOLUTION_STRATEGY` | 开关标志 |

完整文档（含默认值与说明）在 `app/config.py` 顶部的 docstring — 改任何东西前先读它。

---

## 4. 安装与首次运行

```powershell
cd C:\memory_backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # 若被阻止：Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
pip install pytest httpx
```

**启动服务器（保持运行）：**
```powershell
.\run.ps1       # uvicorn app.main:app --reload --reload-dir app --port 8000
```

检查是否就绪：`http://127.0.0.1:8000/healthz`（不碰存储）。**`/docs`** 是内置的交互式 API 文档（Swagger UI）——不用装任何东西、不用记任何自定义命令，就能直接在浏览器里试每个端点、看请求/响应的确切形状。两个浏览器应用：

- **`/gui`** — 可视化图谱编辑器
- **`/chat`** — 带抽取复核的对话 UI

**运行测试套件：**
```powershell
python -m pytest tests/ -q
```
> **当前状态（2026-09-03）：** `466 passed, 20 failed, 1 skipped`。
> 见 §12 — 失败多为测试间共享状态的顺序相关性、或 `semantica` 包未完整安装，不是主路径问题。
> 新增的实体合并（`test_merge.py`）、搜索平局判定（`test_search.py`）、共享链接（`test_shares.py` + `test_shares_api.py`，含显式验证过的"升级攻击"安全测试）全绿。

---

## 5. 认证

API 要求 bearer token，并且**没有配置 token 就拒绝启动**（除非本地开发用 `AUTH_MODE=off`）。两种凭据、同一个头 `Authorization: Bearer <token>`：

| 种类 | 给谁 | 怎么拿 |
|---|---|---|
| 静态 token | 服务/脚本/代理 | 在 `.env` 里 `AUTH_TOKENS=<token>:<user>` |
| 会话 token | 人 | `POST /v1/auth/login`（用户名 + 密码） |

**密码账号（人）：** 设置 `AUTH_SECRET`，然后用 CLI 建账号。密码是提示输入的，绝不作为参数传入（参数会进 shell 历史和进程列表）：

```powershell
python scripts\manage_users.py add alice
python scripts\manage_users.py add ops --admin
python scripts\manage_users.py list
```

账号存在同一个对象存储的 `_auth/users.json` 里，用 **scrypt** 哈希（每用户盐、内存密集）。登录有节流（默认 8 次失败 / 5 分钟 / 用户名+客户端）。

**会话 token 是无状态的**（`v1.<payload>.<sig>`，HMAC-SHA256）。后果：单个 token 无法提前撤销；轮换 `AUTH_SECRET` 会让所有会话同时失效。把 `AUTH_SESSION_HOURS` 设短。

快速诊断：`GET /v1/auth/me` 告诉你当前凭据能访问什么（诊断 403 最快的方法）。`POST /v1/auth/password` 改自己的密码。

---

## 6. 写入管线（记忆是怎么进去的）

这是设计核心。数据进入图谱有**三种**方式，全都经过同一套"先计划后应用"的机制。

### 6.1 两步防护（反幻觉）

任何东西都不会被隐式写入。流程是：

1. **评估（Assess）** — `app/verify/assessor.py`，确定性、**无 LLM**。给文本打相关性/新颖性/重要性/特异性分，匹配已有实体，判断是否值得调用模型。**中英文都认。** 评估器同时识别拉丁专有名词与 CJK（中文/日文）连写专有名词（人名/组织/团队名如"张磊、技术部"），按单字切分中文字符作词数/密度，并内置**中文重要性标记**（决定/敲定/预算/目标/负责人/整改方案/隐患/缺口…）——否则任何中文文本都会因"无命名实体、无重要性"被跳过、永远不入库。
2. **计划（Plan）** — 若通过，LLM 提出一组**结构化操作**（upsert 实体 / 加事实 / 连关系）。什么都不写。
3. **路由（Route）** — `app/wikis/router.py` 决定内容属于哪个 wiki。
4. **应用（Apply）** — 操作通过与 REST 层相同的 store API 执行（含 wiki 权限检查 + 操作日志审计）。

`POST /extract` 返回*计划*；你复核后 `POST /extract/apply` 才会落地。没配 API key 时 `/extract` 返回 503。

**纠正与撤回（重要）：** 对"X 不是 Y，X 其实是 Z"这类消息，`_resolve_target` 会在路由**之前**跨 wiki 定位被提及的主体，并把目标**锁定到持有该主体的既有 wiki**——绝不会新开一个空 wiki 然后在那里找不到主体。这一点对抽取与聊天两条路径都成立。撤回只移除被矛盾的事实（`delete_fact`），不会一步补上替代事实（"是 Z"需由正常抽取在肯定措辞下另行补录）。另外，幻影删除（对不存在的实体发 `delete_entity`）在 apply 时会被标为 `skipped`、不报成功，UI 不会再把"什么都没改"误显示成"已应用"。

### 6.2 自动捕获（"无按钮"路径）

当 `AUTO_CAPTURE_ENABLED=true` 时，后台定时器（`app/autocapture/`）按计划运行**同一条管线**：读取每天记录的会话，对每个做 评估 → 路由 → 抽取 → 应用。

- **幂等。** 光标文件（`autocapture/cursor.json`）记录哪些 会话:消息数 已处理；只有增长的会话才会被重新扫描。启动时回填（`AUTO_CAPTURE_BACKFILL_DAYS`，默认 7 天）补上服务停机期间漏掉的。
- **多主题会话会被分段。** 若一段对话里有两个无关的事实倾倒，调度器会把它切分为连续的主题段（topic run），各进各的 wiki——一个占主导的主题无法吞掉它后面短小无关的内容。
- **身份锁定（方案 A）。** 一个 wiki 首次被写入内容时，它的 `identity_entities` 会被锁定。此后只有新内容**触及锁定的身份实体**时，才允许"继续进这个 wiki"；否则就创建一个新的、命名恰当的主题 wiki。这就是让旧"所有东西都倒进个人/demo wiki"这一失效模式变得不可能发生的机制。

### 6.3 代理 CRUD 意图

`POST /v1/users/{u}/agent/act` 让 LLM/代理**通过显式的结构化意图**在图谱上增删改查：

```
<intents>
[ {"op":"add_fact","wiki":"q3-azure-migration","entity":"person/sarah-kim",
   "text":"Prefers Tuesdays.","confidence":0.9},
  {"op":"delete_entity","wiki":"q3-azure-migration","entity":"person/maria"} ]
</intents>
```

每个意图精确声明要对什么、在哪里操作；执行器通过**与 REST 层相同的 store API + 权限检查 + 审计**来运行。没有隐式写入——模型必须声明意图。

### 6.4 共享链接（跨用户协作，不用 wiki 授权模型）

给一个实体生成一个**能力链接**（`POST .../wiki/{type}/{title}/share`），任何拿到链接 URL 的人都能在 `/shared/{share_id}` 打开一个不用登录的聊天页，跟 LLM 对话，其内容会**严格限定**写入那一片图——该实体加上所有已经跟它相连的实体（一个连通分量），既有的实体绝不会因为被新实体链接就被"拉"进范围（防止靠提一个不相关的已有人名把访问范围偷偷扩大）。删除操作在共享链接下**永远**被拒绝。链接本身就是凭证，不叠加 bearer token 认证；owner 侧可撤销（`DELETE .../shares/{share_id}`）或设过期时间。guest 的每一轮对话记在 **owner** 的 session 日志里并立刻标记 examined，防止自动捕获定时器之后用无范围限制的方式重新处理同一段文字。详见 `PROGRAM_JOURNAL.md` §21。

---

## 7. API 全景

除 `AUTH_MODE=off` 外所有路由都要求认证。用户作用域：`/v1/users/{user_id}/...`。Wiki 作用域：`/v1/wikis/...`（基于授权）。

### 实体 / 图谱（用户命名空间）
- `PUT /v1/users/{u}/wiki` — upsert 实体 `{type,title,aliases?,summary_append?,compact?,significance?}`
- `GET /v1/users/{u}/wiki?type=&include_deleted=` — 从 manifest 列列表
- `GET /v1/users/{u}/wiki/{type}/{title}` — 取单个（`?touch=false` 跳过时钟）
- `DELETE /v1/users/{u}/wiki/{type}/{title}` — 墓碑标记（+ 级联、幂等）
- 事实：`POST/PATCH/DELETE .../wiki/{type}/{title}/facts[/{fact_id}]`
- 关系：`POST/PATCH/DELETE .../wiki/{type}/{title}/relations[/{relation_id}]`
- `POST /v1/users/{u}/wiki/resolve` — 标题去重 → `matched|created|inbox`
- `POST /v1/users/{u}/wiki/{type}/{title}/merge_into` — 把当前实体合并进另一个（`{target_wiki_id}`），关系重定向、事实/别名并入，source 打墓碑
- inbox：`GET /wiki/_inbox`、`.../_inbox/{id}`、`.../_inbox/{id}/resolve`、`DELETE`
- 图谱：`POST /wiki/traverse`、`GET /wiki/_stats`、`POST /wiki/_subgraph`、`POST /wiki/layout`、`POST /wiki/_rebuild_manifest`、`POST /wiki/_compact_ops`、`POST /wiki/_reconcile`

### 共享链接（见 §6.4）
- `POST /v1/users/{u}/wiki/{type}/{title}/share` — 生成链接（可选 `{label, expires_in_days}`）
- `GET /v1/users/{u}/shares` — 列出自己创建的链接；`DELETE .../shares/{share_id}` 撤销
- `GET /v1/shared/{share_id}` — guest 预览（标题、类型、范围大小），不用认证
- `POST /v1/shared/{share_id}/chat` — guest 严格对话，返回 `{reply, applied, rejected}`

### 共享 wiki（基于授权）
- 通过 `routes_wikis.py` 的 wiki CRUD（共享、显式访问、写入时自动创建）
- **`/v1/wikis/{wiki_id}/entities...`**（`routes_wiki_entities.py`）— *指定 wiki 内*的实体 CRUD。授权来自 **bearer token，而非 URL**；URL 指明 wiki，token 指明调用者。平台管理员绕过 wiki 级授权。

### 对话与聊天
- `POST /v1/users/{u}/chat` — 只读检索 + 回复（自动记录两轮消息）
- `POST /v1/users/{u}/chat/aggregate` — 多会话上下文打包
- `GET /sessions/...` — 记录取回（`?as_transcript=true` 给出抽取器所需的精确输入形状）

### 抽取
- `POST /v1/users/{u}/extract` — 计划（`?apply=true` 直接落地）
- `POST /v1/users/{u}/extract/apply` — 提交复核后的操作
- `POST /v1/users/{u}/autocapture/run` — 手动触发一次捕获

### 便签（中期记忆贴纸）
- `POST/GET /v1/users/{u}/notes`、`GET /notes/expired`、`GET/DELETE /notes/{id}`

### 代理 + 意图
- `POST /v1/users/{u}/agent/act` — 来自 LLM 的结构化 CRUD 意图

### 原始事实日志、文件、verify、认证、健康
- `POST/GET /v1/users/{u}/raw-facts`、`GET .../raw-facts/range`
- `POST /v1/users/{u}/files`，聊天附件；以 `file:{date}:{id}` 引用
- `POST /v1/users/{u}/verify`（assessor/打分界面）
- `POST /v1/auth/login`、`GET /v1/auth/me`、`POST /v1/auth/password`
- `GET /healthz`

---

## 8. 检索模型（记忆怎么出来）

聊天 `/chat` 对记忆**是只读的**：它取回上下文并注入，但从不存储。检索靠**身份 + 关键词 + 图谱**，不用向量：

- assessor 将问题与实体标题/别名/摘要匹配（确定性、无 LLM、无 embedding）。**中英文问题都能匹配**——中文实体有确定性 slug（`cjk-<hex>-<sha1>`），不会被无意义的 `entity` 撞名吞掉，也不被当成"空标题"拒绝。
- `traverse()`/邻接遍历走内存中的 manifest（manifest 热时零文件读取）。
- 关键词打分 + 多实体上下文打包（见 `routes_chat.py` / `test_chat_aggregate.py`）。

**测试信号：** 聊天响应带 `wiki_id`、`wiki_reason`、`memory_hit`、`context_used`；聊天 UI 显示 `wiki:` 和 `memory: <wiki_id>` 引用。

> **本仓库没有向量检索。** 若需要语义/向量召回，那层（SiliconFlow `BAAI/bge-m3` embedding 等）配置在 OpenClaw 记忆系统的上游——不在这里。

---

## 9. 常用操作

日常操作用 **`/docs`**（浏览器打开、点开对应端点、Try it out）最直接，不需要装/记任何东西。需要脚本化时，直接用 `Invoke-RestMethod`（PowerShell）或 `curl`，两个都不需要仓库里的任何自定义工具：

```powershell
$H = @{ Authorization = "Bearer <token>" }   # AUTH_MODE=off 时可省略
$base = "http://127.0.0.1:8000/v1/users/demo"

Invoke-RestMethod "$base/wiki/_stats" -Headers $H                                    # 实体/边计数
Invoke-RestMethod "$base/wiki" -Method Put -Headers $H -ContentType "application/json" `
  -Body (@{ type='person'; title='Alice Chen'; aliases=@('Alice') } | ConvertTo-Json)
Invoke-RestMethod "$base/wiki/person/alice-chen/facts" -Method Post -Headers $H -ContentType "application/json" `
  -Body (@{ text='Leads retrieval.'; confidence=0.9 } | ConvertTo-Json)
```

**维护 / 修复：**
```powershell
Invoke-RestMethod "$base/wiki/_rebuild_manifest" -Method Post -Headers $H   # 索引与存储文件不一致
Invoke-RestMethod "$base/wiki/_reconcile" -Method Post -Headers $H          # 清理悬挂关系
```

> `bench.ps1`、`seed_demo.ps1`、`crud_demo.ps1`、`env.ps1`、`memory.psm1` 已于 2026-09-03 清理时移除（demo/基准/PowerShell 终端便利工具，不影响任何实际功能；计时改用 `scripts\crud_timing_test.py`）。

**诊断（仅出问题时）：**
`scripts\diagnose_mirage.py`（绕过 FastAPI，S3 出问题）、`scripts\s3_latency_probe.py`（S3 慢）、`scripts\check_bucket.py --fix`（损坏实体文件）、`scripts\s3_preflight.py`（部署前验证存储桶）、`scripts\cost_benchmark.py`（写入成本模型）、`scripts\browse_bucket.py`（无法直接访问七牛时，绕过其他一切直接浏览/下载桶内容）。

---

## 10. UI

- **`/gui`** — 力导向图谱编辑器。启动时从连通度最高的大约 40 个节点开始（不是全图）；双击 `+N` 节点展开。拖动保存布局。在右侧面板就地编辑别名/重要度/摘要/事实/关系，另有 **Merge duplicate**（合并进另一实体）与 **Share**（生成/复制/撤销共享链接，显示该链接覆盖的连通分量大小）两个面板。边样式编码关系类别（虚线=refines、红色=contradicts、绿色=causes、点线=时序对）。
- **`/chat`** — 对话 UI。登录；聊天；"Review extraction"/"Apply" 按钮实现两步写入。可查看/删除已保存对话；**刷新页面会自动恢复你正在进行的会话**（`sessionStorage` 记的是当前活跃会话，不是靠服务端多存东西）。
- **`/shared/{share_id}`** — 共享链接打开的 guest 页面，没有登录、没有会话列表，纯粹一个聊天框；每轮回复下面会标出这轮说的哪些内容真的存进去了、哪些因为超出分享范围被拒绝。
- 均由应用提供（没有 CORS 中间件，所以 `file://` 不行）。`/gui`、`/chat` 把你的 token 粘贴进对应字段；`/shared/{share_id}` 不需要任何凭据。

---

## 11. 运维警告（上线前必读）

这些是代码注释反复强调的尖锐边缘。跳过会导致令人困惑的故障。

1. **`MIRAGE_INDEX_TTL_SECONDS` 必须保持 `0`。** 高于 0 时，另一个进程写入后 mirage 会返回陈旧的目录列表。因为 `_rebuild_manifest` 会用列表*替换*索引，陈旧列表可能删除仍存在的实体的条目——恰恰在你试图修复时损坏状态。
2. **`MIRAGE_REUSE_CONNECTIONS` 应保持 `true`。** 设为 false → 每操作新建 TLS 连接 → 远程端点实测约慢 10 倍。
3. **写后缓冲有一个崩溃丢失窗口。** 硬崩溃可能丢失最多 `FLUSH_INTERVAL_SECONDS` 的 *manifest* 更新（陈旧，可经 `_rebuild_manifest` 恢复）和 *审计*记录（真正丢失）。若审计丢失不可接受，设 `OPS_LOG_WRITE_MODE=sync`。优雅关闭会排空（FastAPI lifespan 在事件循环停止前排空缓冲——颠倒顺序会死锁）。
4. **多 worker ⇒ `MANIFEST_WRITE_MODE=sync`。** 缓冲的 manifest 是进程内的；两个写者各持部分视图，后刷新的覆盖前者，丢掉对方条目。实体文件安全（条件写）；只有 manifest 受影响。
5. **S3 条件写需要较新的 boto3，以及实现了条件写的 S3 兼容存储。** AWS 原生 `If-None-Match`（2024-08）/ `If-Match` PutObject（2024-11）。R2 和较新的 MinIO 支持；旧 Ceph/MinIO 网关不支持。`s3_preflight.py` 会探测这两个头并大声报错。
6. **不要在没有生命周期规则的情况下开启存储桶版本控制**（manifest 会被频繁重写；非当前版本会累积成本）。若不长期保留审计，给 `*/wiki/_ops/` 加生命周期规则。
7. **`AUTH_MODE=off` 仅限开发。** 任何能访问该端口的客户端都能读写所有用户的记忆。启动时应用会大声警告。
8. **非 AWS 网关用路径风格寻址**（七牛等）。虚拟主机寻址需要存储桶子域的泛域名 DNS。
9. **`truststore` 是软依赖，但强烈建议装上。** 没装的话 LLM 调用退回 OpenSSL 默认校验，某些真实存在、被 OS/浏览器信任的证书链会被 OpenSSL 3.x 判定拒绝（见 §13 故障排查）——现象是间歇性 502，容易被误判成"网络不稳定"。
10. **共享链接的 `share_id` 就是全部凭证，没有额外的访问控制。** 撤销（`DELETE .../shares/{share_id}`）或设置 `expires_in_days` 是收回访问权限的唯一方式；链接一旦泄露给不该看到的人，唯一补救是立刻撤销。

---

## 12. 测试套件 — 当前状态与已知失败（2026-09-03）

运行 `python -m pytest tests/ -q`：**466 通过，20 失败，1 跳过。**

20 个失败分两类，都不是本轮新引入的问题（逐个复核过：临时还原相关改动、确认失败依旧存在）：

**A. 测试间共享状态导致的顺序相关性（单独跑会绿）：**
- `test_api.py` 的部分 `test_list_entities_*`、`test_chat.py` 的检索类测试、`test_title_resolver.py` 的模糊匹配测试、`test_subgraph.py::test_catalogue_pages_and_searches`、`test_slug_conflicts.py::test_api_returns_409_with_a_usable_alternative`。
- 根因是 manifest 写后缓冲/搜索排序依赖的共享进程状态在测试间残留；单独跑（`python -m pytest tests/test_chat.py -v`）稳定通过。

**B. `semantica` 包在当前环境未完整安装：**
- `test_conflicts.py`、`test_intents.py` 的多个用例，报 `ModuleNotFoundError`。补齐依赖（见 `requirements.txt` 里 `semantica==0.6.6` 那段注释）即可修复。

**已解决、不再出现在失败列表里：**
- 此前长期间歇性失败的 `test_extract_unrelated_topic_still_lands_in_the_same_wiki`、`test_interactive_apply_marks_session_examined_so_autocapture_skips`——根因是 LLM 调用因 TLS 证书校验过严随机失败，`truststore` 修复后连续多轮稳定转绿（见 `PROGRAM_JOURNAL.md` §22）。
- `test_views.py` 两个日记测试此前因硬编码的过期日期字符串失败，已改为动态取当前日期。

线上主路径端到端是通的（见本次新增的 `test_shares_api.py` 里对共享链接完整流程的端到端覆盖）。

---

## 13. 快速排障

| 症状 | 原因 / 修复 |
|---|---|
| 应用起不来，报"no tokens are configured" | 在 `.env` 设 `AUTH_TOKENS`，或开发用 `AUTH_MODE=off` |
| `401 Missing bearer token` | 发送 `Authorization: Bearer <token>` |
| `403 ... scoped to user 'x'` | token 属于别的用户；查 `/v1/auth/me` |
| `401 Invalid username or password` | 未知/禁用账号也返回这个（有意为之） |
| `429 Too many failed attempts` | 登录节流；等 `Retry-After` 值 |
| `503 Password login is not enabled` | 未设 `AUTH_SECRET` |
| `401 Session expired` | 重新登录 |
| `/extract` 返回 503 | 未配 `DEEPSEEK_API_KEY`（其余照常工作） |
| `/extract` 返回 502 | 模型不可达 / 没返回可用的东西 |
| 502 报 `SSL: CERTIFICATE_VERIFY_FAILED ... Basic Constraints of CA cert not marked critical` | OpenSSL 3.x 对某些真实证书链的合规瑕疵变严格；确认 `truststore` 已安装（`requirements.txt`）且 `app/main.py` 顶部的 `inject_into_ssl()` 正常执行（非 ImportError 静默跳过） |
| `/chat` 里的对话刷新页面后消失 | 先确认服务端到底存没存上（`GET /v1/users/{u}/sessions`）——若完全没有记录，是 LLM 调用失败导致会话从未落盘（见上一条），不是前端状态丢失；若服务端有记录但页面没自动恢复，检查浏览器 `sessionStorage` 是否被清空（隐私模式、跨标签页） |
| 共享链接返回 404 | 链接拼错，或已被 owner 撤销/过期；guest 拿到的错误信息与"从未存在过"一致，是有意为之，不额外泄露"曾经存在过"的信息 |
| 共享链接聊天里有些内容"没存上" | 正常行为，不是 bug——那部分内容落在分享范围（连通分量）之外；guest 页面的回复下面会显示具体是哪些、为什么 |
| 中文文本被跳过（"Nothing proposed"） | 老版本 assessor 对中文失明；确认 ".venv" 跑的是含 CJK 支持的新代码 |
| 中文实体全撞成 `person/entity` | 老 slug 逻辑；新代码对中文用确定性 `cjk-<hex>-<sha1>` slug |
| 一切 ~600ms / 慢 | 关闭了 `MIRAGE_REUSE_CONNECTIONS` |
| HTTP 422 "corrupt entity file" | `python scripts\check_bucket.py --fix` |
| `_stats` 显示 0 | 空 user id，不是错误 |
