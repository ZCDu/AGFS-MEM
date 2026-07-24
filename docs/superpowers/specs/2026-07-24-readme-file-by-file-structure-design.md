# README 逐文件项目结构与文档精简设计

## 目标

重写根目录 `README.md` 的“项目结构”章节，使读者不打开源码也能理解 DREAM 每个源码文件的职责；同时把历史设计、实施计划和专项验证文档整理为两份面向老师和接入者的功能说明。

## 范围

- 对 `src/dream/` 下当前实际存在的每个 Python 文件和资源文件逐项说明。
- 保留分层目录结构，目录行说明该层的整体职责，文件行说明该文件的具体职责。
- 对 `tests/` 不展开文件树，只用一行说明其包含单元、集成和端到端测试。
- 将 `docs/` 精简为两份不带日期的功能文档，并在项目结构中逐项说明。
- 根目录中的 `.env.example`、`pyproject.toml` 和 `README.md` 继续保留简要说明。
- 不修改功能代码、导入路径、测试逻辑或目录结构。

## 文档精简

最终只保留：

```text
docs/
├── ai-evolution-and-user-persona.md
└── dream-mechanism.md
```

`ai-evolution-and-user-persona.md` 介绍：

- 用户画像从提取、规范化到新增、更新、合并和去重；
- `USER.md` 与 `USER_PERSONA.md` 的职责；
- Decision Cards 与 `DECISION_RULES.md` 的形成和持续更新；
- 下一任务如何使用已经激活的画像与 AI 决策经验。

`dream-mechanism.md` 介绍：

- 做梦机制的自适应触发；
- Agnes 每批一次正常调用及最多一次非法输出修复；
- 确定性 Curator、可选语义 Curator 和自动治理；
- 快照、版本、写回、回滚、300 秒截止时间与安全失败。

完成新文档并确认有效内容已经迁移后，删除：

- `docs/api/`
- `docs/design/`
- `docs/validation/`
- `docs/superpowers/`

被删除文档仍可从 Git 历史恢复。

## 表达方式

源码采用带行尾注释的树形结构：

```text
├── provider_adapter.py  # 解包并规范化外部模型输出，生成 DREAM 内部知识提案
```

每个说明应：

1. 使用具体职责描述，不只重复文件名；
2. 区分调用入口、领域模型、编排、存储和测试；
3. 与当前源码实现一致；
4. 避免宣称尚未实现的能力。

测试目录只保留简述：

```text
├── tests/  # 单元、集成和端到端测试
```

## 验收标准

- `src/dream/` 中每个非缓存文件都出现在项目结构章节。
- 所有列出的路径在工作区真实存在。
- `docs/` 最终只包含两份新的功能说明。
- `tests/` 不展开内部文件。
- README 其他章节内容保持不变。
- Markdown 结构清晰，`git diff --check` 通过。
