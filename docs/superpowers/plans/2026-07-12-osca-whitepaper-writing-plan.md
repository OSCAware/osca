# OSCA 开放规范白皮书 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 写成一份以 OSCA 开放规范为核心、以 Oscaware 为参考实现，能引导 AI Agent 产品与技术同行创建自己 OSCA Agent 的中文白皮书 v0.1。

**Architecture:** 全文采用“问题 → 原理 → 规范 → 运行 → 飞轮 → 参考实现 → 创建自己的 Agent”主线，用月度经营诊断 Agent 贯穿，以临期商品定价 Agent 补充 outcome 证据。正文分五批写作，每批独立完成事实核对、证据分级、用户红笔和一次提交，最后统一术语、链接、状态日期与文档导航。

**Tech Stack:** GitHub Flavored Markdown、Mermaid、OSCA SPEC v0.3/v0.4 draft、公开样例包、Oscaware 架构与开发计划、Git。

## Global Constraints

- 第一读者是关注 AI Agent 的产品与技术同行；首要行动目标是基于 OSCA 创建自己的 Agent。
- 主角始终是 OSCA 开放规范；Oscaware 只作为参考实现和机制证据。
- 开篇第一性假设必须保留原意：“AI Native 组织是一条认知工作流水线；AI 是稳定运转的流水线，专家是线上的判断节点，同时是线外的作者与所有者。”
- 必须回答“为什么关键位置要站人”：不是因为人天然比 AI 更会判断，而是人能把尚未被系统表达的外部变化带进流水线。
- 同时保留“现实是第二位专家”：可确定性观测的 outcome 也能把外部变化带回系统，不能把人的作用写成唯一传感渠道。
- 叙事采用问题—原理—规范—实现为主，案例贯穿为辅；案例不得限制规范的通用边界。
- 全文区分“设计原则 / 已实现 / 已演练 / 已验证”；P0 前不得把机制演练写成业务价值证明。
- 不使用“越用越聪明”“自主立法”“已证明复利”等无条件承诺。
- 白皮书不复制完整 Schema、Lint 规则、API 或逐文件代码说明；精确约束链接到 SPEC、Lint 文档和 README。
- 当前实现状态统一标注“截至 2026-07-12”，避免与永续设计混写。
- 正文中文文件使用 `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`；本计划暂不创建英文版。
- 不修改用户或其他代理正在编辑的无关代码；每次提交只包含本任务明确列出的文档文件。

---

## File Map

- Create: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md` — 中文白皮书正文、目录、图示、案例、附录。
- Modify: `README.md` — 最终验收后在文档导航中增加英文说明的中文白皮书链接。
- Modify: `README.zh-CN.md` — 最终验收后在文档导航中增加白皮书链接。
- Reference only: `docs/OSCA-SPEC-v0.3.md` — 已定稿包格式与账本纪律。
- Reference only: `docs/OSCA-SPEC-v0.4-draft.md` — Trigger/Gate、Episode、Settle、Replay 等增量运行语义。
- Reference only: `docs/OSCA-LINT-RULES.md` — 机器化纪律与已知边界。
- Reference only: `examples/oper-diagnosis.osca/` — 月度经营诊断贯穿案例的公开事实源。
- Reference only: `Core_docs/Oscaware-架构文档-v0_4.md` — 设计公理、系统分层、飞轮、度量与开放问题。
- Reference only: `Core_docs/Oscaware-1_0开发计划-v1_0.md` — 里程碑、P0 与开放边界。
- Reference only: `/Users/lay/Documents/Git/oscapipe/README.md` — 第一方蒸馏实现的能力与未完成项。

## Interfaces

- Consumes: 已批准的写作设计 `docs/superpowers/specs/2026-07-12-osca-whitepaper-design.md`。
- Produces: 可公开评审的白皮书正文 `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`。
- Produces: README 中稳定的白皮书入口。
- Review contract: 每批写作结束后用户先红笔；未经该批批准，不开始下一批正文。

---

### Task 1: 建立正文骨架，写摘要、开篇与第 1～2 章

**Files:**
- Create: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`
- Reference: `docs/superpowers/specs/2026-07-12-osca-whitepaper-design.md`
- Reference: `Core_docs/Oscaware-架构文档-v0_4.md`
- Reference: `README.zh-CN.md`

**Interfaces:**
- Consumes: 开篇第一性假设、目标读者、OSCA/Oscaware 主从关系和证据等级。
- Produces: 后续章节复用的核心定义、术语口径、贯穿案例起点和价值声明边界。

- [ ] **Step 1: 建立完整目录骨架**

在正文文件中写入标题、版本、摘要、前言、15 章标题、结语和附录标题。未写章节只保留标题，不能加入待办标记或伪装成正文的占位句。

- [ ] **Step 2: 写摘要与证据声明**

摘要必须在 500～800 字内回答：现有 Agent 缺什么、OSCA 是什么、专家为何在线上/线外同时存在、Oscaware 是什么、当前证据到哪一级。证据声明明确四级口径，并注明 P0 尚未完成。

- [ ] **Step 3: 写开篇“Agent 不是数字员工，而是认知工作流水线”**

开篇按以下论证顺序展开：

1. 数字员工隐喻的适用性与局限，避免树立稻草人；
2. OSCA 选择流水线隐喻，因为认知工作需要目标、结构、连接、触发与稳定运行；
3. 专家作为线上判断节点与线外作者/所有者；
4. 关键位置站人的原因是引入未被系统表达的外部变化，而非宣称人类认知永远优于 AI；
5. Outcome 是可机器观测变化的第二条回流路径；
6. 该组织观是设计假设，等待 P0 证明。

- [ ] **Step 4: 写第 1 章问题定义**

区分编排、知识与判断，使用“知识库是 Agent 去查它；判断账本是它自己生效”解释 Judgment Ledger。月度经营诊断案例在本章只讲第一次失败：检修期差旅报警被专家删除，但普通工作流没有让裁决自然进入下次运行。

- [ ] **Step 5: 写第 2 章 OSCA 核心主张与非目标**

给出 OSCA 定义、OSCA/Oscaware 关系，明确 OSCA 不是模型、RAG、Prompt 集、BPM 替代品或自主改写规则系统。将“进化”约束为可审计、可撤销、可回放的有证据适应。

- [ ] **Step 6: 运行第一批文字验收**

Run:

```bash
rg -n '数字员工|认知工作流水线|线上的判断节点|线外的作者与所有者|外面的世界|OSCA 是|Oscaware|设计原则|已实现|已演练|已验证' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
rg -n '越用越聪明|自主进化|已经证明|T[O]DO|T[B]D' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git diff --check -- docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
```

Expected: 第一条覆盖全部核心词；第二条无无条件承诺或占位内容；`git diff --check` 无输出。

- [ ] **Step 7: 用户红笔第一批正文**

提交前向用户展示摘要、开篇与第 1～2 章，重点确认流水线假设的锋利程度、对数字员工叙事是否公平、OSCA 定义是否准确。根据红笔修改后再次执行 Step 6。

- [ ] **Step 8: Commit 第一批正文**

```bash
git add docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git commit -m "docs: draft OSCA whitepaper thesis and problem statement"
```

---

### Task 2: 写第 3～5 章——定义、资产与运行模型

**Files:**
- Modify: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`
- Reference: `docs/OSCA-SPEC-v0.3.md`
- Reference: `docs/OSCA-SPEC-v0.4-draft.md`
- Reference: `examples/oper-diagnosis.osca/`

**Interfaces:**
- Consumes: Task 1 的核心定义和月度经营诊断第一次失败。
- Produces: O/S/C/A/J、`.osca` 包、双平面、Episode、Performer 与三级停的稳定术语，供飞轮章节引用。

- [ ] **Step 1: 写第 3 章 O/S/C/A/J**

每个字母回答一个问题、给出职责边界和经营诊断映射。明确 O/S/C/A 定义稳定工作结构，J 是之上的裁决层，不把 Judgment 写成第五种普通配置对象。

- [ ] **Step 2: 写第 4 章 `.osca` 包**

用简化目录树解释 osca.yaml、AGENT、Policy、Structure、Objects、Connectors、Aware、Judgments、Cases。解释开发态/交付态、劝告/笼子、文件/索引和客户资产所有权。

- [ ] **Step 3: 写第 5 章双平面运行**

用 Mermaid 时序或流程图展示 Trigger → Gate → Retrieve → Assemble → Pipeline → Human/Outcome。说明控制决策不依赖 LLM，但参考实现当前可在同一 Host 进程的独立线程中调用认知平面。介绍五类 Performer 与三级停。

- [ ] **Step 4: 核对规范术语**

Run:

```bash
rg -n '^##|^###|Object|Structure|Connector|Aware|Judgment|Episode|Trigger|Gate|Performer|三级停' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
rg -n 'trigger|gate|performer|settle|replay' docs/OSCA-SPEC-v0.4-draft.md
git diff --check -- docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
```

Expected: 白皮书术语与 SPEC v0.4 draft 对齐；无格式错误。

- [ ] **Step 5: 用户红笔第二批正文并提交**

重点确认五层定义是否易懂、Host 无 LLM 的逻辑/进程边界是否诚实、案例是否支持通用规范。修改通过后提交：

```bash
git add docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git commit -m "docs: explain OSCA package and runtime model"
```

---

### Task 3: 写第 6～9 章——账本、证据、飞轮、安全与迁移

**Files:**
- Modify: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`
- Reference: `docs/OSCA-LINT-RULES.md`
- Reference: `examples/oper-diagnosis.osca/judgments/`
- Reference: `examples/oper-diagnosis.osca/cases/`
- Reference: `/Users/lay/Documents/Git/oscapipe/README.md`

**Interfaces:**
- Consumes: Task 2 的 Judgment、Episode、Human 与 Outcome 定义。
- Produces: Case/Candidate/Judgment、飞轮、Trust、Supersedes、Replay 与模型迁移的完整逻辑。

- [ ] **Step 1: 写第 6 章判断资产**

用对照表定义 Case、Candidate、Judgment；解释五段式、只追加、Supersedes、Trust、正负判断同权。明确专家确认 Candidate 不等于 `confirmed +1`，Trust 来自后续使用。

- [ ] **Step 2: 写第 7 章两种证据**

经营诊断案例解释 diff；临期定价侧栏解释 decision vs reality。说明人负责带入未建模的新语境，Outcome 负责带入已可观测的现实回声。

- [ ] **Step 3: 写第 8 章反馈飞轮**

逐步解释 Capture、Distill、Confirm、Ledger、Index、Retrieve、Replay。明确 AI 无权选择 Evidence/Meta、Candidate 在包外、专家拍板后才立法，并诚实标注 Outcome 蒸馏和整本 Replay 的当前缺口。

- [ ] **Step 4: 写第 9 章安全、审计与迁移**

比较文本 Judgment 与权重记忆，展示输出 → Judgment → Case → 作者 → Commit 追溯链，解释 Manifest/Binding/Executor、Policy、Kill switch 和换模型逐条 Replay。

- [ ] **Step 5: 核对账本纪律与证据措辞**

Run:

```bash
rg -n 'Case|Candidate|Judgment|Evidence|Supersedes|Trust|Capture|Distill|Confirm|Retrieve|Replay|第二位专家' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
rg -n '出生证据|supersedes|trust|replay|当时生效判断集' docs/OSCA-LINT-RULES.md
rg -n '已验证|已证明|完全自动|自主' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git diff --check -- docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
```

Expected: 账本纪律与 Lint 口径一致；价值表述符合证据等级；无格式错误。

- [ ] **Step 6: 用户红笔第三批正文并提交**

重点确认人的立法权、Outcome 的地位、私有蒸馏实现边界和模型迁移叙事。修改通过后提交：

```bash
git add docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git commit -m "docs: describe OSCA ledger flywheel and governance"
```

---

### Task 4: 写第 10～12 章——场景准入、创建 Agent 与参考实现

**Files:**
- Modify: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`
- Reference: `README.zh-CN.md`
- Reference: `cli/README.md`
- Reference: `host/README.md`
- Reference: `examples/oper-diagnosis.osca/README.md`

**Interfaces:**
- Consumes: 前三批建立的规范、运行与飞轮概念。
- Produces: 读者从判断场景到创建空账本 Agent 的可执行采用路径，以及 Oscaware 的准确参考实现定位。

- [ ] **Step 1: 写第 10 章场景准入**

给出重复性、判断密度、反馈闭环三问；说明高频、反馈快、错误可控的优先级和不适用场景。避免把所有 Agent 工作都纳入 OSCA。

- [ ] **Step 2: 写第 11 章创建方法**

按 Object → Structure → Connector → Aware → AGENT/Policy → lint/pack/load → Episode → capture/distill/confirm → index/replay 展开。明确第一版允许空账本，禁止凭想象手写专家 Judgment。

- [ ] **Step 3: 写第 12 章参考实现**

解释参考实现为何必要、公开/私有边界、SPEC 被实现反哺的实例，以及截至 2026-07-12 的能力状态。明确真实 Connector、Host 私有语义检索接入、Outcome 蒸馏、整本 Replay、M4/M5/M6 的未完成状态。

- [ ] **Step 4: 验证开发者路径链接目标存在**

Run:

```bash
test -f docs/OSCA-SPEC-v0.3.md
test -f docs/OSCA-SPEC-v0.4-draft.md
test -f docs/OSCA-LINT-RULES.md
test -f cli/README.md
test -f host/README.md
test -f examples/oper-diagnosis.osca/README.md
rg -n '空账本|osca lint|osca pack|osca load|osca replay|截至 2026-07-12' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git diff --check -- docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
```

Expected: 所有目标文件存在；采用路径与状态日期完整；无格式错误。

- [ ] **Step 5: 用户红笔第四批正文并提交**

重点让用户以首次接触 OSCA 的开发者视角检查：读完是否真的知道下一步去哪、怎样从空账本开始。修改通过后提交：

```bash
git add docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git commit -m "docs: add OSCA agent adoption and reference implementation guide"
```

---

### Task 5: 写第 13～15 章、结语与附录

**Files:**
- Modify: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`
- Reference: `Core_docs/Oscaware-架构文档-v0_4.md`
- Reference: `Core_docs/Oscaware-1_0开发计划-v1_0.md`

**Interfaces:**
- Consumes: 全文规范、运行、飞轮、采用与参考实现内容。
- Produces: 生态接口、验证方法、开放问题、克制结语和自包含文档导航。

- [ ] **Step 1: 写第 13 章兼容与组合**

解释格式、Lint、Runtime、Replay 四层兼容；介绍 Agent 作为 Connector、MCP/企业能力层和未来规范治理。不得把尚未存在的认证体系写成当前能力。

- [ ] **Step 2: 写第 14 章评估方法**

定义被确认判断裁决数、返工量、推翻率、覆盖率、Candidate 确认率、Replay 红灯率与专家额外负担。说明 P0 是业务生死实验，不是组件验收。

- [ ] **Step 3: 写第 15 章开放问题**

保留蒸馏质量、Guard 表达力、多专家冲突、慢供料、Outcome 蒸馏、模型迁移、跨包复用、规范治理与商业边界，收束到 M3 工程收官和 P0 真账本。

- [ ] **Step 4: 写结语与附录**

结语回到“把人的裁决留在系统里”，邀请读者阅读 SPEC、运行样例和创建 Agent。附录写术语表、最小包目录、公理短版、文档导航和版本/证据声明。

- [ ] **Step 5: 用户红笔第五批正文并提交**

重点确认生态愿景没有越过当前证据、开放问题足够诚实、结语有行动号召但不营销过度。修改通过后提交：

```bash
git add docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
git commit -m "docs: complete OSCA whitepaper ecosystem and evaluation sections"
```

---

### Task 6: 全文编辑验收与发布入口

**Files:**
- Modify: `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Consumes: Task 1～5 用户已红笔的完整正文。
- Produces: 可公开发布的 v0.1 中文白皮书和仓库入口。

- [ ] **Step 1: 统一术语和大小写**

统一 OSCA、Oscaware、Object、Structure、Connector、Aware、Judgment、Case、Candidate、Episode、Runtime、Host、Policy、Replay、Outcome、Supersedes、Trust。首次出现提供中文解释，之后不随意换同义词。

- [ ] **Step 2: 检查叙事和案例连续性**

逐章确认经营诊断案例从第一次失败、Diff、Candidate、Confirm、下次命中到 Replay 连续；临期定价只承担 Outcome 侧栏，不抢主线。

- [ ] **Step 3: 检查证据与当前状态**

Run:

```bash
rg -n '设计原则|已实现|已演练|已验证|截至 2026-07-12|P0' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
rg -n '越用越聪明|完全自主|已经证明|行业领先|唯一|T[O]DO|T[B]D' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
```

Expected: 第一条覆盖状态与关键声明；第二条没有未经限定的营销话术或占位内容。

- [ ] **Step 4: 检查 Markdown、标题层级和本地链接**

Run:

```bash
git diff --check
rg -n '^# ' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
rg -n '^## |^### ' docs/OSCA-WHITEPAPER-v0.1.zh-CN.md
```

Expected: 仅一个一级标题；章节层级连续；无行尾空格或冲突标记。逐个点击正文内相对链接确认目标存在。

- [ ] **Step 5: 在双语 README 添加白皮书入口**

英文 README 使用 “OSCA Whitepaper (Chinese)” 描述并链接 `docs/OSCA-WHITEPAPER-v0.1.zh-CN.md`；中文 README 使用“OSCA 开放规范白皮书”。不在本任务翻译白皮书全文。

- [ ] **Step 6: 运行仓库文档相关门禁**

Run:

```bash
git diff --check
cd cli && uv run osca lint ../examples/oper-diagnosis.osca
git grep -n 'OSCA-WHITEPAPER-v0.1.zh-CN.md' -- README.md README.zh-CN.md
```

Expected: `git diff --check` 无输出；样例包 lint 通过；两个 README 均包含正确链接。

- [ ] **Step 7: 用户全文终审**

用户对标题、摘要、开篇命题、技术准确性、证据边界、开发者采用路径和公开状态做最终红笔。修改后重复 Step 1～6。

- [ ] **Step 8: Commit 发布版**

```bash
git add docs/OSCA-WHITEPAPER-v0.1.zh-CN.md README.md README.zh-CN.md
git commit -m "docs: publish OSCA open specification whitepaper v0.1"
```
