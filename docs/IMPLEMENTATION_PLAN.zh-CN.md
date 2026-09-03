# 实施计划 — memory_backend 的企业级项目跟踪

状态：草稿（待批准，尚未编写任何代码）
日期：2026-08-20
范围：`C:\memory_backend`（FastAPI + mirage/S3 + LLM 抽取）

## 0. 目标

把当前扁平的"主题 wiki"图谱改造成**企业级项目跟踪图谱**，用于会议 / 项目 / 项目群（program），同时保持云原生（S3）、LLM 驱动，并且——至关紧要——**可审计但不沦为合规表演**。

三个工作流（相互独立，可以任意顺序交付，都可选）：

- **WS-A：项目/项目群/会议层级**（"形状"）
- **WS-B：精简决策生命周期**（叠加在该形状之上的"记录"）
- **WS-C：冲突检测**（数据质量护栏）

每一块都做了规模与范围界定，以便一次落地一块并上线验证。

---

## 1. 当前状态（依据——已在代码中核实）

- 实体类型（`VALID_TYPES`）：`person, organization, project, event, concept, artifact, preference, decision`。**目前还没有 `program` 或 `meeting` 类型。**
- 关系类别：`related_to, contradicts, refines, causes, temporal_before, temporal_after`。
- 存储：`wikis/{scope}/{type}/{slug}.okf.{md,json}`（mirage S3 上，`app/graph/keys.py::wiki_key`）。Wiki = 组织级、带显式授权（`WikiRegistry`），`access: {user: role}`。
- 抽取流程：`POST /extract`（评估 → LLM 计划，不写入）→ `POST /extract/apply`（落地）。两步设计（反幻觉）。
- 聊天 `/chat` 对记忆只读（不自动写入）。
- 决策**已经是**一种实体类型；每个"决策"实体携带一个 fact。

## 2. WS-A — Program → Project → Meeting 层级

### 2.1 问题
如今一个 wiki 是单一扁平主题。一个企业级项目群（如"Q3 云迁移项目群"）包含多个项目（"计费引擎"、"数据仓库"），而每个项目由会议纪要喂养。没有父子关系——全都是平级 wiki。
想法（源自 Youtu-GraphRAG）是*基于 schema 指导的层级结构*：预先施加包含关系，使图谱可按"program → projects → meetings"导航。

### 2.2 设计
**新增两种实体类型：** `program` 和 `meeting`。

- `meeting` = 单次会议（有与会者、日期、议程、决策、行动项）。
- `program` = 组织级伞形概念，own 一组 `project`/`subprogram` 实体。

**关系（复用现有类别，必要时新增）：**
- `program contains project`  → 新增类别 `contains`（或复用 `related_to` 加 label="contains"；更倾向显式 `contains`）。
- `project belongs_to program`
- `meeting belongs_to project`
- `meeting refines project`（一次更新）——复用 `refines`。
- `decision influences project` —— 经由 WS-B。

**层级在哪里被声明（而非图谱）：** 在 wiki *元数据*里，不只是实体边。`WikiMeta` 增加：
```
"parents": ["<wiki_id>"]   // 如 program wiki 列出其 project wikis
"children": ["<wiki_id>"]  // 派生的/反向索引
"kind": "program" | "project" | "meeting" | "topic"
```
理由："program wiki"和"project wiki"是真实的一级 wiki，各有自己的授权；层级把它们串起来。会议不需要成为自己的 wiki（粒度太细）——见 2.3。

### 2.3 会议是什么
两个选项，需要你做决定:

- **(A) 会议 = 项目/program wiki 内的实体**（轻量，推荐）。在项目 wiki 里放 `meeting/2026-08-20-logistics-review` 实体。
- **(B) 会议 = program 下自己的 wiki**。仅当你想要把每次会议的授权/历史作为一等 wiki。

推荐 **(A)** —— 会议是叶子，不是容器。

### 2.4 抽取改动
- 扩展 `SYSTEM_PROMPT`：实体可标为 `program`、`meeting`；发出 `contains`/`belongs_to`/`refines` 链接，让层级自然地从 LLM 里出来。
- 扩展 `VALID_TYPES` + `VALID_RELATION_CATEGORIES`。
- 扩展 GUI，在现有力导向图旁渲染一个**树/层级视图**（program → projects → meetings）。（UI 阶段；先做后端。）

### 2.5 存储/迁移
- 新 `WikiMeta` 字段是增量的；现有 wiki 默认 `kind: "topic"`、空 `parents/children` → **无需迁移**（向后兼容）。
- 新实体类型只是 wiki 下新增 `{type}/{slug}` 文件夹——增量。

### 2.6 验收
- 能创建一个 `program`，把 `project` wikis 挂为子节点，把 `meeting` 实体挂到某项目，并在 GUI 看到树。
- 抽取一段会议纪要能正确产生 `meeting` 实体 + `contains`/`refines` 链接。
- 现有扁平 wikis 仍正常工作（无回归）。

---

## 3. WS-B — 精简决策生命周期

### 3.1 问题
你已经存储带证据的 `decision` 实体。缺的是（Semantica 决策智能中有用的部分，去掉审计膨胀）：

- **谁/何时/理由/结果/置信度作为决策上的结构化字段。**
- **因果排序**："这个决策导致了那个行动/决策"。
- **先例搜索**："这个话题上次我们决定过什么"。
- **汇总（Rollup）**：按 program/project，"上次会议以来的决策"。

### 3.2 设计 — 丰富 `decision` 实体
扩展决策实体的 payload/schema，加入结构化字段：
```
decision / <slug>   (type=decision)
  - statement       (做出的决策)
  - decided_by      (person 实体)
  - decided_at      (日期)
  - reasoning       (简短理由)
  - outcome         (状态：proposed / accepted / rejected / deferred / done)
  - confidence      (0-1，来自抽取)
  - precedent_of    (→此决策取代/关联的先前决策实体)
  - source          (证据引用：session/file)
```
这是对现有 `.okf.json` 的增量，向后兼容安全。

### 3.3 先例搜索端点
`GET /v1/wikis/{id}/decisions?q=...&since=...` → 返回某 wiki/层级上的决策，排序、带 outcome + source。对 wiki 的 `decision/*` 实体做廉价查询（已可用 `store.list_entities` 列出）。

### 3.4 "上次会议以来的决策"汇总
`GET /v1/wikis/{id}/decisions/rollup` → 按 `decided_at` 分组的决策，可选用"距离某会议实体日期以来"过滤。按最新在前排序。

### 3.5 不构建（明确说明）
- W3C PROV-O 导出 / 监管格式——跳过（非受监管领域）。
- SHACL/OWL 策略门禁——跳过。
- 带下游影响分析因果链——先跳过；保持每个作用域内扁平的、有序的、可搜索的决策列表。

### 3.6 验收
- 抽取把决策记录为带结构化字段。
- 先例搜索返回某项目/program 的过往决策，带 outcome。
- 汇总显示某项目/program"上次会议以来的决策"。
- 现有决策实体/wikis 无回归。

---

## 4. WS-C — 冲突检测

### 4.1 问题
如今 `add_fact` **无条件追加**。冲突的事实（如"Alice 领导 Orion"然后是"Alice 被替换不再领导 Orion"）并排累积、毫无信号。在企业级项目图谱里，这会静默腐蚀建立在它之上的决策。

### 4.2 设计（Semantica 的"标记，而非静默覆盖"）
在 `POST /extract/apply` 上，写每个 `add_fact`/`link_entities` 之前：

1. 查找目标实体上已有的 facts（已在磁盘上）。
2. 对入站 fact 跑一次**廉价的语义矛盾检查**：
   - 同一实体 + 在共享谓词上极性相反（如"leads"vs"no longer leads"、"decided X"vs"decided Y against X"）；
   - 启发式歧义时，依赖 LLM 给三元判断（`conflicts | refines | compatible`）。
3. 结果记录在操作上：
   - `compatible` → 照旧追加。
   - `refines` → 追加并链接到先前 fact（Supersedes）。
   - `conflicts` → 在计划里**标记**：不要静默覆盖旧 fact；创建一条待处理的"冲突"复核项。人做解析（keep-new / keep-old / merge）。

存储：每实体一个小型 `_conflicts/{date}.jsonl` 操作日志，或 facts 上的一个 `conflict` 字段——保持增量。在 GUI 复核面板里呈现冲突。

### 4.3 置信/范围
- 只检查 wiki 中已存在的实体 facts（成本有界）。
- 给 LLM 冲突调用加门禁——仅当事实是新的且启发式存在歧义时才调用，以限制 token 开销。

### 4.4 验收
- 喂入矛盾主张会产生一个被标记的冲突，而非静默追加。
- 现有兼容 facts 仍正常追加（无误报）。
- 冲突出现在复核 UI 中，带 keep-new/keep-old/merge 解析。

---

## 5. 贯穿主题 / 非目标

### 要做
- 仅云原生（S3）；不用 Neo4j/RDF/本地图存储。
- 增量改动；每一块都与现有 wikis/entities 向后兼容。
- 一切都在现有两步复核之后（apply 前不写入）。
- 优先 prompt 级/元数据级改动；仅在必要时写代码。

### 不做（明确推迟或否决）
- PROV-O/OWL/SHACL/治理导出（WS-B 跳过）——除非你改口说"受监管"。
- 替换 S3 存储或加 Neo4j/FalkorDB（否决——你说过云 S3 是必须）。
- 聊天自动写记忆（保持只读）——不变。
- 同消息多 wiki 分段（独立待办项，不在此计划内）。

## 6. 建议构建顺序（每块可独立交付并可验证）
1. **WS-C 冲突检测** — 数据质量价值最高，自包含。
2. **WS-B 决策字段 + 先例 + 汇总** — 增量、低风险、单独就有用。
3. **WS-A 层级** — 改动最大的 UX/架构；放在 B 之后，好让决策有可汇总的作用域。

## 7. 写代码前留待你定的开放问题
1. **会议**：(A) 项目 wiki 内的实体，还是 (B) 自己的 wiki？（推荐 A）
2. **新关系类别** `contains`——可以加吗，还是复用 `related_to` + label？
3. **冲突检查范围**：只查同 wiki facts，还是也查层级中相连 wikis（一旦 WS-A 存在）？
4. **优先级/顺序**：同意 C → B → A，还是你要层级（A）先行，因为它改变你录入数据的方式？
5. **Program 粒度**："program"会再包含其他"program"吗（嵌套），还是严格 program→project→meeting？

---

*这是供你批准的草稿。我未修改任何代码。请审阅开放问题（尤其是 #1 和 #4），然后我会从你选定的第一个工作流开始。*
