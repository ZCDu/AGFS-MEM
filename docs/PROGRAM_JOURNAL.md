# 记忆图谱后端 — 项目日志

`C:\memory_backend` 的活历史：它的来龙去脉、做过哪些决定及原因、现在走到哪一步。条目按日期标注，源自项目自身记录与代码本身。本文件讲"为什么"；`INSTRUCTION_MANUAL.md` 讲"怎么做"。

---

## 起源

- **目标：** 构建个人记忆系统中的 wiki/图谱（长期记忆）层。设计意图：**云原生（S3）、LLM 驱动、可审计——但不要变成合规表演。**
- 最初范围刻意收窄：实体存为 **OKF（Open Knowledge Format）** 带 YAML front-matter 的 Markdown 文件、一个避免目录扫描的 manifest、一份 `_ops` 审计日志。明确*不在*范围内：Redis 会话层、journals/raw/源摄取管线、持久性分类器、每日任务、TTL 清理。
- 存储是对象存储（S3 / S3 兼容），**不是**数据库——也刻意**不用** Neo4j/RDF。一个实体 = 一个文件；图谱就是文件加它们的关系。

### Git 历史 → 项目形态

```
fcb0cd0  Initial commit: OKF v0.2 knowledge graph with cloud storage
c2bd98c  Fix file upload, add chat attachments UI, bootstrap admin account
49e4556  Rewrite README as supervisor guide; QUICKSTART with admin bootstrap + chat UI
fb3f52f  Translate README to Chinese
14032f4  Translate GUI to Chinese
c15f163  fix: 22 tests pass, add Docker support
5907778  chore: move .ps1 scripts to scripts/
3f45152  tidied the program
bc39700  i18n: translate chat UI to Chinese
d8b8e64  feat: multi-strategy entity retrieval + merge + keyword index
2ab42aa  feat: show and delete saved conversations in chat UI
c1f9371  feat: shared multi-user wikis with explicit access + auto-create on write
         (HEAD，分支 fujunxiang；2026-08-14)
```

从 `c1f9371` 之后的所有开发都在该基线上进行，目前**均未提交**（工作树）——包括下面描述的那些规模不小新模块（auth/users、autocapture、notes、intents、conflicts、sessions、verify）。截至 2026-08-28，工作树领先 `origin/master` 9 个提交，并带着一大批未暂存改动外加许多未跟踪文件。

---

## 关键设计决策（大致按时间顺序）

### 1. 用 OKF 作为存储格式
实体是 OKF 索引卡：Markdown 正文（人/代理可读）+ YAML front-matter（严格结构化、机器可解析——事实、关系、元数据、状态）。刻意拆分：**front-matter 供程序化过滤/重写；正文供快速浏览和喂给 LLM。** 实体文件是唯一事实来源；manifest/操作日志是派生的、可重建的。

### 2. 写后缓冲 + 日志结构化存储（S3 成本修复）
对象存储按请求计费；一次 PUT 约是 GET 的 12.5 倍；而且没有追加——重写一个不断变大的文件是平方级成本。两个修复：
1. **写后缓冲**（manifest/ops 存内存，按定时器刷新）。
2. **两个派生文件都用日志结构化存储**（ops 按每次刷新分段；manifest = 快照 + 增量链）。结果：manifest 写入字节随实体数翻倍从约 3.6× 降到约 2.0×（线性）。

实测：`upsert_entity` 从 3 次 PUT → 1 次；读从 1 次写 → 0（`last_accessed` 移进了 manifest）。

**接受了的权衡：** 硬崩溃可能丢失最多 `FLUSH_INTERVAL_SECONDS` 的 manifest 更新（陈旧、可恢复）和审计记录（真正丢失）。优雅关闭会排空缓冲。这是刻意的持久性 vs 成本权衡——见 `writebehind.py`。

### 3. 存储后端：只用 mirage-ai Workspace
存储**只**经一个挂载在 `/s3` 的 mirage Workspace 运行（生产 `S3Resource`，开发/测试 `DiskResource`）。运行中的应用里没有原始 boto3 后端。理由：复用原仓库的桶/凭据、经 `MIRAGE_S3_ENDPOINT_URL` 支持 S3 兼容网关（七牛 Kodo）、并在本地挂载同一条代码。`LocalFSBackend` 沦为测试替身。

**发现并记录下来的尖锐边缘：**
- `list_keys` 必须**递归遍历，不能 readdir**（mirage 的 readdir 是 POSIX 风格的；天真地返回会静默得到空图谱）。
- 关闭顺序重要：先排空缓冲、再停事件循环，否则死锁。由 FastAPI lifespan 做，不用 atexit（CPython 会在跑 atexit 前先杀线程池）。
- **`MIRAGE_INDEX_TTL_SECONDS` 必须是 0**——`_rebuild_manifest` 期间若列表陈旧，可能删除仍存在实体的条目。
- 条件写是尽力而为（mirage 没有 CAS）；单进程内安全、多副本间不安全。
- 连接复用：mirage 默认每操作新建一条 TLS 连接（远端约慢 10 倍）。`MIRAGE_REUSE_CONNECTIONS=true` 打补丁 `async_session` 使用长连接。

### 4. 实体图谱改进
- **抽取时近重复去重**（`_is_near_duplicate`）：包含（不是 Jaccard）、数字精确比较、否定词永不当填充词。重复只是邋遢；被静默吞掉的更正才是错误。
- **slug 冲突被暴露而非静默。** 两个不同标题可能 slug 成同一个 `wiki_id`（`C++` vs `C#`）。现在：变体拼写合并并记录一个别名；真正不同的标题 → `SlugConflictError` → **HTTP 409** 并带建议 `wiki_id`；`on_conflict` ∈ {error, disambiguate, merge}。
- **manifest 里的邻接索引**：`traverse()` 用零文件读取回答图谱跳（manifest 在内存）。深度 2 遍历：31 次存储读取 → 0。
- **两种视图、两个端点**：平面可分页目录（`GET /wiki`）vs 封闭邻域（`POST /wiki/subgraph`）——按索引给图谱分页正是"没有分页"背后的真实缺陷。
- **`delete_entity` 是墓碑优先**：一次原子状态翻转让实体立即处处不可见（没有读者能看到半删的实体）；级联 + `_reconcile` 有弹性（逐实体失败继续）。布局坐标经 `POST /wiki/layout` 持久化（拖拽结束时写入、合并）。

### 5. 原始文件与会话
- 文件按生成的 id 忠实存储（绝不用文件名）；文本读取时抽取，所以对旧上传的改进也能生效。二进制/报告记为"不可读"而非解码成乱码。
- 会话是**每段对话一个文件**（不是每天）——N 消息的日文件追加是平方级，而每会话文件同时充当**证据指针**（`session:{date}:{session_id}` 一次读取解析）。

### 6. 认证（是人，不只是服务）
在静态 token 之外加了真实的用户名/密码认证：
- 静态 token → 服务/脚本。会话 token → 人（用户名 + 密码登录）。
- 密码：**scrypt**（内存密集、每用户盐、参数随哈希一起存）、只限长度（不做组合规则——那些实证无益）。
- 会话 token **无状态**（HMAC 签名、内嵌过期）刻意为之：撤销列表意味着每请求一次存储读取。权衡：单个 token 无法提前撤销；轮换 `AUTH_SECRET` 一次性全部失效。
- 账号存对象存储里的 `_auth/users.json`（无新基础设施、跨重启存活），仅登录时读取。
- 登录节流（8 失败 / 5 分钟）——scrypt 保护哈希，但保护不了端点。
- **默认关闭（fail closed）：** 没配认证就拒绝启动，所以不带认证的 API 不可能被意外上线。`AUTH_MODE=off` 仅限开发。

### 7. wiki 心智模型（重大重构——2026-08-13/14）
> **Miguel 起初搞错的地方：** 把 wiki 当用户所有、然后倾倒进个人页面。

修正后的模型（已固化进代码与路由逻辑）：
- **Wiki = 组织级知识图谱*作用域***（会议、项目、组织、工作区），经显式访问授权共享——**不属于某个用户**。
- **实体 = wiki *内部*的一个节点**，类型为 person|organization|project|event|concept|artifact|preference|decision。
- 像 "Acme Corp" 这样的**组织**是 wiki **内部**的一个**实体**——写"Alice 在 Acme Corp"会在*当前 wiki 内*创建 `person/alice-chen` + `organization/acme-corp`，而不是新建一个 "Acme Corp" wiki。

### 8. 路由：确定性、无 LLM（"归哪个 wiki"的问题）
`app/wikis/router.py` 对每个可达 wiki 相对文本打分（实体标题/别名/摘要——一次 registry 读、零 LLM 调用、零实体读）。阶梯：明确赢家 → `use`；无命中 → `create`/`none`；接近平局（0.10 内）→ `ambiguous`。

**读自由路由、写需确认。** `route()` 绝不自动选平局。聊天只在 `action == "use"` 时用 wiki；抽取在歧义时抛 409 并附候选——"读错是安全地答错；写错会腐蚀图谱"。

### 9. "去他妈的 demo wiki"——不再有个人/主页 wiki 倾倒（2026-08-18）
**Miguel 的指令：** 程序自动分配到一个以用户名命名的 "demo" 页面是不可接受的。它必须**判定何时创建新 wiki、给它命名、决定改动、并把记忆插入**——**按主题**。任何东西都不该落到个人/demo 倾倒页。

在 `routes_extract.py` 实现：移除 `ensure_personal` 首次使用兜底。每个用户的持久内容现在都属于一个主题 wiki（"Office Coffee Station"、"Snowflake Migration"…），绝不是一个用户命名的倾倒池。首次使用时 `route(allow_create=True)` 返回 `create` → 临时 → 应用时 LLM 命名主题 wiki。同时修了一个 slug 冲突导致的 500（`_materialize_new_wiki` 用 `provisional_title`、捕获 `WikiError`、重用已有 slug 而非分裂/崩溃）。

干净环境实测：两个无关的持久主题 → 两个命名正确的 wiki；延续正确复用原 wiki。

### 10. "query" 动作——消灭"无关内容被吸进一个 wiki"（2026-08-18）
路由器新增 `topical` 标志：文本是否与命中实体的**紧凑摘要**共享 **≥2 个内容词**（标题/别名排除——那些正是导致误报的名称引用）。强名称匹配但无主题重叠 → 返回新动作 `action="query"`（把被引用的 `wiki_id` 当兜底）而非盲目 `use`。要求 ≥2 词，因为 1 词会在通用动词（"wants"）上误报。`query` 是**写路径守卫**；带被引用 wiki 的读会直接短路到该 wiki。

### 11. 多主题会话吸收——已修复（2026-08-25）
同一个聊天会话里粘贴两个无关的事实倾倒，被当**一个**路由 blob 自动捕获 → 整个会话路由到单一最佳 wiki，抽取器把所有实体都倒进去。根因：调度器把整个会话当一个单元捕获，一个长主导主题吞掉了后面短小无关的。

修复（`app/autocapture/scheduler.py::_capture_one`）：把会话切分为连续的**主题段**（`_topic_runs`，基于专有名词、无 LLM），每段各进各的 wiki。回归测试：`test_two_unrelated_topics_IN_ONE_SESSION_get_two_wikis`。线上实测：银行倾倒 ↔ `financial-institutions-and-market-events`（40 实体），Q4 干净（35、零银行残留）。

随后**升级为方案 A 身份路由（16:45）：** 每个 wiki 首次写入时锁定 `identity_entities`；此后只有内容触及身份实体时才允许继续，否则新建。干净环境实测（16:56）：两个无关倾倒 → 两个独立 wiki（身份已锁定）；触及身份的延续 → 并入已有 wiki、不新建。所有 6 个测试/碎屑 wiki 清理到干净 registry。

### 12. 冲突检测（企业计划的 WS-C）
当 `CONFLICT_CHECK_ENABLED=true` 时，实体写入会跑 Semantica 支撑的冲突检测；矛盾存为 **一等 OKF 冲突记录**，位于 `wikis/{wiki_id}/_conflicts/`，写入仍会落地（仅标记），除非设置了 `CONFLICT_RESOLUTION_STRATEGY`（most_recent|highest_confidence|voting|credibility_weighted|first_seen）。冲突刻意**不是**实体上的字段——冲突是来源之间*关于什么是真的*的分歧，而非什么是真的。解析在复核 UI 里做（keep-new / keep-old / merge）。

### 13. 便签——中期记忆层（贴纸）
介于短期（工作上下文）与长期（wiki）之间，一个中间层：廉价的单次 PUT 便签，带**标签**（不是图谱作用域）、可选**`expires_at`**。按标签查；`/notes/expired` 重新浮出到期待办。带来源链接，可日后提升进图谱。这是待办/临时决定/提醒的预期归属。

### 14. 代理 CRUD 意图（让 LLM 安全地做全量 CRUD）
`POST /v1/users/{u}/agent/act` — 模型发出**显式结构化意图**（JSON），后端通过与 REST 相同的 store API + 权限检查 + 审计执行。调和"用户希望 LLM 增删改查"与"聊天不得隐式写入"：模型精确声明要做什么；没有隐式。

### 15. 修正写入路径的两个真实缺陷（2026-08-28 下午）
两个 bug 都在"用户纠正图谱"时暴露，根因都不是 LLM 幻觉本身，而是**回退到非纠错路径**。

**A. 幻影删除被误报为成功。** 抽取对一次纠正（"她不是黑皮肤，她是拉丁裔"）幻觉出一个字面的 `person/user` 实体（把 "i asked pamela..." 误解析成 "user said"），并对它发出 `delete_entity`。`person/user` 从来不存在，apply 把删除当 no-op 跑完，却仍然无条件地把 `op.status` 标成 `"applied"`——于是 UI 显示一个"成功"，其实什么都没改。修复（`extractor.apply`）：`delete_entity` 返回 not-found 时现在置 `op.status="skipped"`、detail "entity not found; nothing to delete"、`continue`——幻影移除是 no-op，不是成功。

**B. 撤回被路由到新 wiki，导致主体根本找不到。** "移除 Pamela 是黑皮肤的事实，她其实是拉丁裔"是一个真正的撤回（`is_retraction=True`）。但 `routes_extract._resolve_target` 在抽取器的撤回短路（`_plan_removal`）**之前**就跑了 `WikiRouter(allow_create=True)`——路由器判定"新主题 `pamela-black-latino`"→ `_plan_removal` 在那个**空的新 wiki** 里找 Pamela → 找不到 → 返回 None → 掉进创建路径 → 出现垃圾临时 wiki，且"Pamela 是黑皮肤"这条错误事实（fact_0004）原样保留。修复（`routes_extract._resolve_target`）：在 `requested` 分支之后、路由器之前，若 `is_retraction(text) or is_delete_request(text)`，则用 `_find_entity_by_text`（从 `routes_chat` 导入，无循环依赖）**跨 wiki** 查主体；找到且可写就把目标**锁定到持有该主体的既有 wiki**（返回 `(wid, "deterministic retraction/delete target", None)`），`_plan_removal` 于是找到它并构造正确的删除 op。查不到才回退到正常路由。

**已知缺口（未实现）：** 撤回路径只移除被矛盾的事实（`delete_fact`），**不**同时补上替代事实（"是拉丁裔"）。一次完整"替换"（删错的 + 加对的）尚未做成一步——当前一次纠正需要删除与补录作为独立意图（或一条"Pamela 不是黑皮肤、她是拉丁裔"的消息现在会经撤回删掉 Black，而拉丁裔由正常抽取器在肯定措辞下补录）。潜在未来：单计划纠错同时发出 `delete_fact(contradicted)` + `add_fact(replacement)`。

### 16. CJK（中文）盲区——assessor/抽取对中文视而不见（2026-08-28 下午）
用户贴了三份**中文**会议纪要（建筑安全整改会议、人力优化会议、新媒体营销复盘会议），抽取却说"跳过/无命名实体"——即使文本里满是专有名词（张磊、李彤、陈静、周凯、土建班组、技术部）。根因是**整个 assessor 是英文/拉丁语系中心的，对中文彻底失明**：

- `_PROPER` 正则 `\b([A-Z][a-z0-9''-]+...)` 只匹配**大写拉丁词** → 中文专有名词不可见 → 任何中文文本 `propers=[]`。
- `_WORD` 分词器 `[A-Za-z0-9][A-Za-z0-9''-]*` → 中文分词为空（只剩 2025 这类拉丁 token）→ `min_words` 长度门槛 + 密度在中文上崩坏。
- `IMPORTANCE_MARKERS` 全是英文动词（"works at"、"prefer"、"i am"、"decided"）→ 中文"目标/决策/负责人/整改/预算"永不触发 → `importance=0.0`。
- 于是跳过门槛 `if not propers and importance==0.0` 对每条中文消息都触发 → **中文内容永远不入库**。

**修复（三处）：**
- `assessor.py`：`_PROPER` 也匹配 `[\u3400-\u9fff]{2,8}` CJK 连写（人名/组织/团队名）；`_WORD` 也匹配单个 CJK 字符；`IMPORTANCE_MARKERS` 增加中文标记（decision：决定/敲定/明确/决策/确定/达成；commitment：计划/排期/前完成/截止/预算/目标/时限/整改方案；identity：总监/经理/主管/专员/组长/负责人/主持；problem：问题/隐患/短板/风险/违规/缺口，以及偏好/纠正）。中文名字在句首**不**被剥离（与拉丁不同——`_WORD` 把单个 CJK 字符匹配进 `starts`，而名字是多元长的）。
- `store.py`：`_slugify` 把纯中文名坍缩成字面 `"entity"` → 每个中文实体都撞在 `person/entity` 上。现用确定性的 `_cjk_slug()` 兜底：`cjk-<hex-codepoints>-<sha1-8>`；`has_usable_slug` 接受 2+ 个 CJK 字符（之前把中文标题判为"空/不可用标题"）。
- `extractor.py`：`_TOKEN` 从仅 ASCII `[a-z0-9]` 扩展为也匹配 `[\u3400-\u9fff]+`（否则中文事实的 `_fact_signature` 对中文为空 → 所有共享一个数字的中文事实被误判为近重复被丢弃）；SYSTEM prompt 告诉模型会议纪要/计划/承诺**是**持久内容（之前把 KPI/排期/截止当作"一次性/历史/短期"丢弃）。

**实测：** 对中文 HR 纪要，`decision` 从 `skip` 变 `store`、`importance=0.75`、`density=1.0`，完整 LLM 计划 **44 ops**，wiki 命名 `sme-annual-recruitment-and-hr-optimization`，无误删重。英文不受影响（assessor 17 测试通过）。三份中文会议纪要各得 60+ ops（修复前会议-1 只得 3）。

### 17. 实体合并——去重的收尾工具（2026-09-02/03）

`upsert_entity` 的模糊标题匹配只能防住**写入时**的重复；一个跟真实姓名毫无字面重叠的昵称，或匹配逻辑上线前就存在的重复，只能事后发现、事后修。新增 `EntityGraphStore.merge_entities()`：把 source 的事实/别名/摘要/关系并入 target，**重定向**（而非直接砍断）其他实体对 source 的关系指向，source 打墓碑并把早已存在但从未被赋值过的 `merged_into` 字段设为 target。GUI 实体详情页新增"Merge duplicate"面板。

**自查阶段揪出的 3 个真实 bug（不是我引入的新逻辑本身错，是没想全的边角）：**
- **自环泄漏：** 若 target 早就有一条指向 source 自己的关系（两个重复体互相链接过），合并后这条关系会指向一个已经墓碑化的 id，变成死链。修复：合并前先剥离 target 上所有指向 source 的关系。
- **竞态导致 500：** source/target 在初始存在性检查之后、真正写入之前被并发删除，会让底层 `ValueError` 未捕获地冒出来。改为捕获后返回结构化失败结果，而不是让调用方收到裸 500。
- **允许跨类型合并：** 没有任何限制会阻止把一个 `person` 合并进 `project`——加了类型必须一致的检查（一致地照搬本文件里"type 在创建时锁定"的既有原则），API 层面返回 422 而不是误导性的 404。

10 个新单测（`test_merge.py`）覆盖含上面 3 个 bug 在内的场景；每个修复都先临时还原到 bug 状态确认测试真的会红，再改回来确认转绿——避免"写了个总是绿的测试"。

### 18. decay_score 从摆设变成真正的信号；significance 第一次被真正赋值（2026-09-02/03）

**发现（不是猜的，是查代码查出来的）：** `Entity.decay_score()`——一个真实的"多久没碰、还剩多少权重"公式——除了在 API 响应里当一个展示字段之外，**从未被用在搜索排序或任何工作流里**。而它公式里的另一半，`significance`，**从未被抽取流程赋过值**——每个实体永远是默认的 0.5，等于 decay_score 实际上只反映时间、不反映"这事到底重不重要"。

**两处修复：**
1. `decay_score` 公式抽成独立函数（`store.py` 顶层 `decay_score()`），`search.py` 的 `rank_entries()` 用它在**同分**结果之间做平局判定——绝不允许它凌驾于已有的分层之上（子串命中永远赢描述命中，这条不变式不能因为"更新鲜"就被打破），只在真正打平时让更近期touch过的排前面。
2. 抽取 SYSTEM PROMPT 新增 `significance` 字段（0-1，带具体锚点：用户自己的常态承诺/核心项目给 0.9-1.0，一般命名实体 0.6-0.8，边缘/一次性给 0.1-0.5），`_build_plan` 解析并夹到 [0,1]。**对已存在的实体，新判断只能把 significance 往上抬，不能往下压**——避免某一次对话的片面视角悄悄削弱一个已经确立重要性的实体。

**自查阶段揪出的 1 个真实 bug：** 判断"是否允许写入 significance"时最初用的条件是 `existing is not None`，但决定"匹配还是新建"用的是更严格的 `existing is not None and existing.type == type_`（标题撞车但类型不同时会走"新建"分支）。两个条件不一致导致：一个因为类型碰撞而被当作"新建"的实体，会被错误地拿一个毫不相关的同名异类实体的 significance 值当下限。统一成同一个 `matched` 布尔值，加回归测试锁死。

### 19. 时区/日期边界 bug 家族——`date.today()` 撞上 UTC 分片（2026-09-02/03）

用户反馈"auto-capture 一直在跳过"，追下去牵出一整类此前没人发现的 bug：这个代码库里几乎所有按天分片的存储（`RawFactLog`、`WikiOpsLog`、`SessionLog`）都用 **UTC** 日期做分片键（`datetime.now(timezone.utc).date()`），但有两处路由用了裸的 `date.today()`（**本地**时区）：

- `routes_rawlog.py` 的 `POST /raw-facts`：写入后立刻用本地"今天"读回，在 UTC+8 这类时区里，一天中大约三分之一的时间会读到**昨天**那份空的分片——一次刚写完的追加显得凭空消失。
- `routes_wiki_entities.py` 的活动流端点：同样的本地/UTC 错位，会让"今天"的活动一段时间内看不到、trailing window 还多算进去一天。

两处都改成 `datetime.now(timezone.utc).date()`，和分片逻辑本身保持一致。顺带牵出并修好了 **8 个测试**里同款的硬编码/`date.today()` 断言（`test_api.py`、`test_mirage_backend.py`、`test_sessions.py`、`test_writebehind.py`）——这些测试本身也在用本地日期核对 UTC 分片的数据，纯靠"测试机器凑巧在 UTC 附近的时区"才长期没暴露。

同一次调查也解释了"auto-capture 老是 skip"表面症状的**根本原因**：`.env` 的 `AUTO_CAPTURE_LOOKBACK_DAYS=1` 是此前会话里刻意设的"捕获昨天"逻辑（"今天写的日志，明晚才变成图谱"），但用户希望的是当天写、当天捕获。改成 `0`（当晚 22:00 直接捕获当天）。

另发现并修复一个**无关但同类**的 stale-date 问题：`test_views.py` 的两个日记测试硬编码了 `day="2026-08-31"`（写测试那天的"今天"），随着沙箱模拟时间推进到 2026-09-03，`_seed()` 造的事实实际落在"今天"而不是硬编码的那个日期，导致 `dated_hits == 0` 稳定失败。改成动态算"今天"。

### 20. GUI 小 bug：admin 面板遗留的裸 `refreshWho()` 调用

`gui.html` 里 `load(false).then(refreshWho)` 把裸标识符当回调传，但 `refreshWho` 只作为 `MemAuth.refreshWho` 存在——每次刷新页面都在控制台炸一个 `refreshWho is not defined`。无实际功能影响（`MemAuth.install()` 自己内部已经会刷新"是否登录"那行文字），但清理掉了。

### 21. 共享链接——跨用户协作的最终方案（2026-09-03）

**需求演化过程（值得记下来，因为中间推翻过一次方向）：**
1. 最初提议：把多人参与的会议路由进一个**共享 wiki**（复用已有的 `WikiRegistry` 显式授权模型）。
2. **用户否决：** "我已经把架构简化成每个用户持有自己的 wiki、由多个独立图组成——共享 wiki 那套访问模型在搜索和写入时不好用。" 不改写入路径，不引入新的访问控制层。
3. 改进为：让用户能针对**一个实体**生成一个链接，其他人点开链接就能看到"那一片记忆图谱"。
4. **最终定型：** 不只是只读查看——持有链接的人能跟 LLM"严格对话"，**只**改动那一片图谱，谁的私人图谱、别人的其他图谱都碰不到、看不到。

**架构（结构性隔离，不是靠 prompt 约束）：**
- `app/shares/store.py`：`ShareLinkStore`，扁平的全局键 `_shares/{share_id}.json`（**不**挂在 owner 的 wiki 路径下——guest 手上只有 share_id，不知道也不该需要知道 owner 是谁；挂在 owner 路径下会让 guest 反过来无法解析自己手上的链接）。`share_id` 用 `secrets.token_urlsafe(24)` 生成——**token 本身就是凭证**，不是查找键；持有它直到被撤销或过期为止就等于有权限，跟 Google Docs"知道链接就能访问"是一个模型。
- `app/shares/scope.py`：`component_of()` 用已有的 `app/graph/components.py` 连通分量算法，取"分享的实体 + 已经跟它相连的一切"作为范围（这正是用户说的"一整片图"，不是单个节点）；`restrict_plan_to_scope()` 在抽取产出的操作计划送去 `apply()` 之前，把落在范围外的操作标记为 `status="rejected"`（`apply()` 本来就会跳过 `rejected` 状态的操作，白捡的复用）。
- **不对称传播规则（防升级攻击的核心）：** 一个**新建**的实体，如果被链接到已经在范围内的东西，会被并入范围（这是允许guest引入新人/新决定并让它们生效的必要机制）。但一个**已存在**的实体绝不会因为被新实体链接就被拉进范围——否则 guest 可以随手提一个 owner 图里毫不相关的已有人名并建一条关系，把那个人（以及递归地，一切连到那个人的东西）偷渡进自己的写入范围。专门写了回归测试复现这条攻击路径（新实体桥接到范围外已有实体），并在临时放宽规则后确认测试真的会红。
- **删除操作在共享链接下永远被拒绝**，不管在不在范围内——guest 能加东西，不能删 owner 的任何东西，这个决定留给 owner 自己的会话去做。
- `app/auth.py` 新增 `require_share`：guest 侧路由完全不用 bearer token 认证，URL 里的 share_id 本身就是全部凭证——这是"点开链接就能用"字面意义上的实现，不是"跳过认证"，而是"这条路径的认证机制本来就是持有 token"。
- guest 会话记录进 **owner** 的 session log（不是某个 guest 账号——guest 压根没有账号），并立刻调用 `mark_examined`，防止 auto-capture 定时器之后用**没有范围限制**的方式重新扫描同一段文字，把整个隔离设计绕过去。
- GUI：实体详情页新增"Share"面板（创建/复制/撤销链接）；独立的 guest 页面 `app/static/shared.html`（没有登录、没有会话列表、没有文件上传——那些概念对一个"只有链接"的访客都不适用），served at `/shared/{share_id}`。

**上线后发现的一个真实可用性缺口：** owner 一开始只能通过打开 guest 链接才能看到自己分享出去的这片图有多大（"1 个节点"还是"12 个节点"）。补上：owner 侧的 Share 面板直接调用跟 guest 预览同一个只读端点，把 `scope_size` 显示在自己的卡片上。

34 个新测试（`test_shares.py` + `test_shares_api.py`），含上述升级攻击场景在内的安全性场景全部显式验证过"改一行规则、测试真的会红"。

### 22. 会话"刷新后消失"——两层问题，根因比表面症状深得多（2026-09-03）

表面症状：`/chat` 刷新页面后，看起来在进行的对话不见了。挖出来是**两个独立问题叠在一起**：

**表层（真的是 UI bug）：** `chat.html` 从不把"当前活跃会话"跨刷新持久化——`messages`/`sessionId` 纯内存态，刷新即丢，即便侧边栏里那条记录在服务端好好地存着。修复：`sessionStorage` 记一个 `chat_active:{user}` 键，页面初始化时若存在就自动 `resumeSession()`。

**深层（真正的元凶）：** 直接查用户账号发现**过去一周一条会话记录都没有**——不是"刷新丢了"，是**从来没存上过**。追下去发现 `llm.complete()` 间歇性抛 `SSL: CERTIFICATE_VERIFY_FAILED ... Basic Constraints of CA cert not marked critical`，而 `/chat` 的会话落盘发生在 LLM 调用**成功之后**——调用一失败,那一轮对话在服务端根本不存在,刷新自然什么都恢复不出来。这个 SSL 报错本 session 出现过至少 5 次,此前一直被当作"环境限制,绕不开",这次真正查了根因：

- 直接用 Python `ssl` 模块复现，确认是 OpenSSL 3.x 对 DeepSeek 证书链一个真实但无害的合规瑕疵（Basic Constraints 扩展没标 critical）变严格拒绝的结果；`curl`（走 Windows 自己的信任验证）访问同一个地址完全正常——**不是**中间人攻击的迹象，是两套 TLS 校验实现在这条特定规则上宽严不一。
- 修复：引入 `truststore` 包，`app/main.py` 启动时 `truststore.inject_into_ssl()`，让 Python 的证书校验委托给 **操作系统自己的信任判断**——跟 curl/浏览器用的是同一个判断依据，而不是关掉校验本身（关校验才是真正的安全降级，这次特意避开）。
- **验证：** 修复前连续调用大约三到五成会失败；修复后连续 5/5 成功。此前长期"时好时坏"的 `test_extract_unrelated_topic_still_lands_in_the_same_wiki`、`test_interactive_apply_marks_session_examined_so_autocapture_skips` 两个测试，此后连续多轮稳定转绿。

这次的教训是：一个"UI 状态丢失"的报告，根因可能完全不在 UI 层——先查服务端到底有没有存上,再修前端状态管理。

---

## 从未（完全）落地的企业计划

`IMPLEMENTATION_PLAN.md`（2026-08-20，状态 DRAFT）提议把扁平主题图谱改造成**企业级项目跟踪图谱**：

- **WS-A：Program → Project → Meeting 层级**（新 `program`/`meeting` 类型 + `contains`/`belongs_to` 关系、WikiMeta 里的 `parents`/`children`/`kind`）。
- **WS-B：精简决策生命周期**（结构化决策字段、先例搜索、"上次会议以来的决策"汇总）。
- **WS-C：冲突检测**（已实现——见 §12 上）。

**状态：** WS-C（冲突）大体上实现了。WS-B 和 WS-A（program/project/meeting 层级与决策生命周期）**没有**构建——类型集仍是原始的 8 个，WikiMeta 没有 `parents/children/kind`。计划里的开放问题（会议作为实体还是 wiki、`contains` 类别、构建顺序）仍未回答/未构建。

---

## 孤立的重写——已移除（2026-09-03）

`core/`、`api/`、`clients/` 曾是一个**未跟踪的并行重写**（源头是 `scripts/refactor_layout.py`，一次"改成老师要求的标准目录结构"的尝试：extract/graph/journal/raw/verify 归 `core`、routes 归 `api`、storage 归 `clients`），import 的是 `core.*`、`api.*`、`clients.*`，且**从未有 main 入口接入运行中的服务**——线上应用完全在 `app/` 下运行。它甚至复制了一套 journal/summarizer 概念，与 `app/rawlog/sessions.py` 等并存，是实打实的维护隐患。

`python -c "import app.main"` 确认过它对运行中的应用零依赖之后，连同产生它的 `scripts/refactor_layout.py` 一并删除。同批清理掉的还有几个纯调试/一次性脚本：`_fact_probe.py`、`_speed_test.py`、`_stressgen.py`（根目录下的临时探针/压测脚本，从未被任何文档或代码引用）、`scripts/migrate_sessions_to_journal.py`（目标路径前缀 `journal/` 只有这套死代码会读，线上从未用过）、`scripts/migrate_wikis.py`（其迁移目标路径 `wikis/{user_id}/...` 与本 session 全程实测的实际存储路径 `{user_id}/wiki/...` 不符，任务大概率早已完成或被后续架构调整取代）。删除前逐一确认过没有其他文件引用。

---

## 现在走到哪一步（2026-09-03）

### 运行中 / 线上（在 `app/` 里）
- 认证：静态 token + 用户名/密码 + 无状态会话，默认关闭。
- 单用户一个 wiki，由多个独立连通分量（"图"）组成；wiki 作用域的实体 CRUD。
- 两步抽取（plan → apply）；assessor 把关 LLM（无 LLM 的闲聊闸门）。
- **CJK（中文）完整支持**（§16）：assessor 识别中文专有名词/重要性标记，store 给中文实体确定性 slug，抽取器对中文分词且把会议纪要/计划/承诺当持久内容。
- **纠正/撤回锁定到既有 wiki**（§15）：撤回与删除在路由**之前**跨 wiki 定位主体；幻影删除改成 `skipped` 而非误报成功。
- **实体合并**（§17）：`merge_entities()` 把事后发现的重复并入既有实体，关系重定向而非砍断。
- **decay_score 接入搜索排序，significance 由抽取真正赋值**（§18），带"只能上调不能下调"的强化规则。
- **按天分片存储统一用 UTC**（§19）：`raw-facts`、wiki 活动流、自动捕获时间窗口不再受本地时区影响；`AUTO_CAPTURE_LOOKBACK_DAYS=0`（当天捕获，不再拖到次日）。
- **共享链接**（§21）：capability-token 形式的跨用户协作——持有链接者可对着一个连通分量"严格对话"、只能增补该范围内的内容，删除操作恒被拒绝，范围外的一切结构性不可达。
- TLS 校验委托给 OS 信任库（`truststore`，§22）——修掉了间歇性的 LLM 502/证书错误，此前长期被误判为"环境限制"。
- 自动捕获调度器（主题段切分 + 方案 A 身份锁定）。
- 代理 CRUD 意图；便签（到期贴纸）；冲突记录；记录的会话；聊天聚合/检索。
- 存储：mirage Workspace（生产 S3、开发磁盘），写后缓冲 + 日志结构化 manifest/ops。
- 两个浏览器 UI：`/gui`（图谱编辑器，含 Merge/Share 面板）和 `/chat`（含刷新后自动恢复当前会话）；新增极简 guest 页面 `/shared/{share_id}`。

### 测试
`python -m pytest tests/ -q` → **466 通过，20 失败，1 跳过**（总量从 328 涨到 487，测试数增长主要来自 §17/§18/§21 的新覆盖）。20 个失败全部复核过，**没有一个是本轮改动引入的新问题**：
- 一部分是共享的 manifest/搜索排序状态在测试间残留导致的顺序相关性失败（单独跑会绿，整套跑偶尔红）——`test_api.py`、`test_chat.py`、`test_title_resolver.py`、`test_subgraph.py`、`test_slug_conflicts.py` 的部分用例。
- 一部分是 `semantica` 包在当前环境不完整安装导致的 `ModuleNotFoundError`——`test_conflicts.py`、`test_intents.py`。
> 修复 truststore 之前，这个列表里还长期挂着两个因 LLM 证书错误间歇性失败的测试（`test_extract_unrelated_topic_still_lands_in_the_same_wiki`、`test_interactive_apply_marks_session_examined_so_autocapture_skips`）；§22 修复后连续多轮稳定转绿，已不在失败列表里。

### 未提交 / 未提交风险
`c1f9371` 之后的全部开发**未提交**，本轮（§17-22）同样如此。在任何破坏性操作前，先提交 `app/` 当前一致的状态（并对 `core/`/`api/`/`clients/` 拍板），以免丢工作。

### 留待处理项
- WS-B（决策字段 + 先例 + 汇总）与 WS-A（层级）未构建。
- 方案 C（自动创建时 LLM 命名 wiki 并由用户确认）——代码现在已在 apply 时命名 wiki；早前笔记里记的"LLM 提议 + 写入前用户确认"的确认 UX 记为未开始，但该自动命名路径此后已由 router/apply 流程上游实现。
- 撤回路径仍然只删不补（§15 已知缺口，本轮未处理）。
- 共享链接目前只能"新增"，不支持 guest 把范围内的内容标记完成/编辑既有事实——如果协作场景需要，这是下一步自然的延伸。

### 文件夹清理（2026-09-03）
用户要求清掉不影响使用体验的杂物。移除：孤立的 `core/`/`api/`/`clients/` 重写（见上）、三个根目录调试脚本（`_fact_probe.py`/`_speed_test.py`/`_stressgen.py`）、两个已完成/目标路径已不存在的迁移脚本（`scripts/refactor_layout.py`、`scripts/migrate_sessions_to_journal.py`、`scripts/migrate_wikis.py`）。保留：`wipe_all.py`（用户明确选择保留——是刻意加了确认门槛的个人重置工具，不是杂物）。

**顺带查出一组重复的 `.ps1`：** 根目录和 `scripts/` 下各有一份 `bench.ps1`/`crud_demo.ps1`/`env.ps1`/`run.ps1`/`seed_demo.ps1`。diff 后发现根目录的才是真正在用的那份：
- `scripts/run.ps1` 实际上**跑不起来**——它 `--reload-dir core --reload-dir api --reload-dir clients`，正是刚删掉的那套死代码，是同一次"改成老师标准目录"重构的残留，从未针对现在的 `app/` 布局改对过。
- `scripts/env.ps1` 是个只设两个环境变量的桩函数，不定义 `mem`/`$U`/`$J`；根目录 `env.ps1` 才是接到 `memory.psm1`（只存在于根目录）的那份，跟 `INSTRUCTION_MANUAL.md` §4/§9 文档的工作流一致。
- `bench.ps1`/`crud_demo.ps1`/`seed_demo.ps1` 两份字节级相同，纯冗余。

确认没有任何文件引用 `scripts/*.ps1` 之后，删掉了 `scripts/` 下的全部 5 个，保留根目录的 5 个。全套测试清理前后对比无回归。

**第二轮——用户进一步要求"跟运行中的应用或用户说明书无关的一律不留"：**
- `seed_journal.py`（未被任何文件引用的旧 demo 会话生成脚本）、`seed_demo.ps1`、`crud_demo.ps1`（demo/示例专用，名字里就带"demo"）——删除，并同步清掉 `INSTRUCTION_MANUAL.md`/`QUICKSTART.md`/`PROGRAM_JOURNAL.md` 里对它们的引用。
- `env.ps1`、`memory.psm1`、`bench.ps1`——不被运行中的服务、GUI、chat 或任何测试使用，纯 PowerShell 终端便利工具（`mem` 命令），`/docs` 的 Swagger UI 已经覆盖同样的手动调试需求。删除后重写了 `INSTRUCTION_MANUAL.md` §4/§9 与 `QUICKSTART.md` 对应章节，把示例从 `mem GET ...` 改成直接的 `Invoke-RestMethod`，不再依赖仓库自带的任何自定义命令。
- `scripts/crud_bench.py`、`scripts/crud_report.py`——未被任何文档引用，跟已保留、且被 `README.md` 文档化的 `scripts/crud_timing_test.py` 功能重叠，删除。
- `demos/` 目录——只剩一个指向已经不存在的源文件（`merge_proto.py`）的 `__pycache__` 编译缓存，没有任何实际内容，删除整个目录。
- 顺带把之前会话新建但没写进文档的 `scripts/browse_bucket.py` 补进了 `INSTRUCTION_MANUAL.md`/`QUICKSTART.md` 的诊断脚本列表——判断标准统一成"要么被运行中的应用/测试用到，要么被用户说明书文档化，否则就删或者补文档"，不留没人知道用途的孤儿脚本。
- `README.md` 里一处指向已删除 `bench.ps1` 的悬空引用改成指向保留下来的 `scripts/crud_timing_test.py`。

删除前逐一 grep 确认没有其他文件引用；`python -c "import app.main"` 确认应用不受影响。

**第三轮——`run.sh`、`wipe_all.py`：** 用户直接问"这两个也需要吗"。`run.sh` 是 `run.ps1` 的 Mac/Linux 版本，但本项目全程只在 Windows/PowerShell 下开发使用，删除；`README.md` 顶部"Run it"一节原本用的是 `./run.sh`（bash 语法），一并改成 `.\run.ps1`（PowerShell），`run.ps1` 里"Windows PowerShell equivalent of run.sh"这句现在过时的注释也删掉。`wipe_all.py` 不被运行中的应用、任何文档化工作流用到，且是破坏性工具——放在仓库根目录风险大于它带来的便利，删除（真需要清测试数据时，现成的 `DELETE` 端点已经够用，或者随手写一个新脚本比维护一个吃灰的旧脚本更安全）。

### `.env.example` 修复（2026-09-03，创建 `final` 分支前的结构审查发现）

用户要求在建分支前先审查程序结构有没有问题。发现两处跟本 session 无关、但明显是历史遗留的真实问题：

1. **`.env.example` 内容被破坏性重复了三遍。** 认证配置那一整段（`AUTH_MODE`/`AUTH_TOKENS`/密码登录/`AUTH_SECRET`/登录节流）和 OKF 布局那一段，逐字重复了三次，中间还嵌进一段跟上下文无关的 `LLM_MAX_TOKENS` 注释碎片——像是之前某次编辑把内容错误地插入/追加了三次。任何人照着这份文件配置一个新的 `.env` 都会先被绕晕。
2. **`.env.example` 文档化了一条实际不存在的存储路径。** 整整一节 `STORAGE_BACKEND=s3`（配 `S3_BUCKET`、`S3_PREFIX`、IAM 策略说明），描述的是 `app/storage/backend.py` 里那个货真价实的 `S3Backend`（纯 boto3）类——但 `app/deps.py` 的 `get_storage_backend()` 从不会构造它：`"s3"` 只是 `"mirage"` 的别名，走的是同一条 mirage Workspace 路径，需要的是 `MIRAGE_S3_BUCKET` 而不是 `S3_BUCKET`。照着文档配置会在启动时直接崩，报一个跟你刚设置的变量名对不上的错误。`grep` 确认 `Settings.s3_bucket`/`s3_prefix` 这两个字段在 `config.py` 之外从未被读取过——是彻底的死配置面。`S3Backend` 类本身留着未删（有意保留，供以后真需要纯 boto3 路径时启用），但 `.env.example` 不该把它当作现在能用的选项来写。

**修复：** 重写整个 `.env.example`，按 `app/config.py` 顶部 docstring 逐项核对，去重、删掉死掉的 s3 配置块，补齐此前完全没写进模板的设置项（`AUTO_CAPTURE_*`、`MIRAGE_INDEX_TTL_SECONDS`、`MIRAGE_REUSE_CONNECTIONS`、`MIRAGE_VERIFY_CONDITIONAL_WRITES`、`CONFLICT_CHECK_ENABLED`/`CONFLICT_RESOLUTION_STRATEGY`、`FLUSH_INTERVAL_SECONDS`/`FLUSH_MAX_PENDING`、`EMBEDDING_*`）。

顺带核对模板内容时，发现 `app/config.py` 自己的 docstring 也有一处错误：`AUTO_CAPTURE_LOOKBACK_DAYS` 的注释写着"0 = 捕获昨天"，但实际代码（`app/autocapture/timer.py::_fire()`）是 `target = today_utc - lookback`——`0` 明明白白就是"今天"，`1` 才是"昨天"，跟 §19 这次会话验证过的真实行为一致。已改正注释使其与代码一致。
