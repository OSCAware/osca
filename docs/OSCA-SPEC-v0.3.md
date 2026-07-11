> 本规范文本以 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh) 开放：可自由转载与改编，须署名并注明出处。
> 本仓库中的代码与样例以 Apache-2.0 授权。/ Specification text: CC BY 4.0. Code & examples: Apache-2.0.

# OSCA 包格式规范 v0.3

> 一个 agent = 一个 `.osca` 文件夹 = 一个 git 仓库。
> 全部纯文本（Markdown + YAML）。包是可交付、可审计、可打印的资产。
> 铁律：包内不得出现任何密钥、连接串、可连接端点（精确边界见 §13）。
>
> v0.3 由 v0.2 与参考实现（`osca lint / pack / load`）互证喂养：补齐了 v0.2 缺失的
> 四类文件规范（osca.yaml / structure / judgment / case），定稿了五处规范与样例的分歧。
> 变更全录见文末附录。

---

## 0. 目录树（v0.3）

```
<agent名>.osca/
├── osca.yaml                     # 身份证:包名/版本/依赖(§1) ← v0.3 补入目录树
├── AGENT.md                      # 入口:身份/目标/边界(劝告层,模型读)
├── policy.yaml                   # 笼子(强制层,运行时读,模型不读)
├── structure.yaml                # 组合骨架(薄,§5)
├── objects/
│   └── OBJ-002-费用异动报警.yaml
├── connectors/
│   └── CON-001-财务系统.yaml      # 仅 manifest,无密钥
├── aware/
│   └── AW-001-月度扫描.yaml
├── judgments/
│   ├── J-0417.yaml               # >200条后按 object 分目录:judgments/OBJ-002/
│   └── J-0423.yaml
├── cases/
│   └── C-0091.yaml               # 元数据+指针;大报文外置(§10)
├── sql/ …                        # 可选:connector 的包内 impl(§6 层3)
├── indexes/                      # 机器生成的缓存,人不手写,坏了重建(公理 A4)
│   ├── checksums.txt             # osca pack 生成的完整性清单(§14)
│   ├── judgments.index.yaml      # 签名表(硬过滤用),osca load 重建
│   └── judgments.emb.parquet     # 向量(语义排序用)
└── bindings.example.yaml         # 部署绑定模板;真实 binding 在部署环境,永不进包
```

---

## 1. osca.yaml（身份证）

```yaml
format: osca                      # 固定值
format_version: "0.3"             # 本规范版本
package_id: demo-group-oper-diagnosis   # 仅小写字母/数字/连字符;交付件以此命名
name: 示例集团经营诊断
entry: AGENT.md                   # 入口文件,必须存在
requires:
  runtime: ">=0.2"                # 运行框架最低版本
  bindings: [FINANCE_DB]          # 部署环境必须注入的 binding;与各 connector
                                  # binding_ref 的并集一致(lint 校验),loader 缺失即报错
integrity: indexes/checksums.txt  # osca pack 生成;交付件完整性清单,可对包签名
```

---

## 2. 双平面架构（运行时契约）

**控制平面（Host，确定性，常驻，无 LLM）**
装载包时：
1. 解析 `aware/` → 把 `triggers` 逐条编译为 watcher（定时器/事件订阅/轮询器）注册进调度器
2. 解析 `policy.yaml` → 装载拦截规则
3. 校验 `connectors/` manifest 与部署环境 binding 的匹配

**认知平面（Episode，LLM，按需唤醒，短命）**
watcher 命中且闸门放行 → 运行时组装一次性上下文：
```
AGENT.md + structure.yaml
+ 命中 Aware 的 discretion 段
+ 该 Aware 引用的 objects
+ 判断检索结果(见 §11):top 3–7 条判断,各带 1 个代表 case
```
剧集跑完 pipeline 即终止。**不存在持续运行的模型。**

**三级停**
- 剧集停：pipeline 完成 / 触发 Aware 的 `budget` 硬顶（步数/token/时长）
- 触发器停：单个 Aware 文件 `enabled: false`
- 包停：注销全部 watcher

---

## 3. 命名与 ID 规则

- 文件名格式：`<ID>-<中文名>.yaml`，如 `OBJ-002-费用异动报警.yaml`
  （judgments/cases 数量大，允许省略中文名，仅 `J-0417.yaml`）
- ID 格式：类型前缀 + 包内自增。`OBJ- / STR- / CON- / AW- / J- / C-`
- ID 一经分配**永不修改、永不复用**；中文名可随时改
- **跨文件引用只允许用 ID**，禁止用文件名或中文名
- 跨包引用预留语法：`<包名>::OBJ-002`（行业判断底座阶段启用）

---

## 4. Object 规范（四型）

公共字段：
```yaml
object_id: OBJ-xxx
name: <中文名>
kind: entity | artifact | metric | composite
version: <int>            # 结构变更时递增
definition: |             # 自然语言定义,必填,比 schema 重要
examples:                 # 正反样例,LLM 对齐的主要手段
  positive: [...]
  negative: [...]         # 每条负样例必须带 why
```

**kind: entity** —— 领域实体（下属单位、客户、工单）
```yaml
schema: {字段: {type, ref?, desc?}}
identity: <怎么判断两条记录是同一个实体>
```

**kind: artifact** —— 产出物（含中间产物，v0.3 扩展）
```yaml
schema: {...}
medium: document | message | table | payload   # payload = 结构化中间物
delivery: 飞书文档 | 对话回复 | 邮件 | API回传 | internal   # internal = 流程内部流转,不对外交付
quality_bar: |            # 产得好的标准,专家验收的依据
```
> v0.3 定稿：步骤间流转的中间产物（如报警候选）归 artifact，
> 标 `medium: payload` + `delivery: internal`，不新增第五型。

**kind: metric** —— 量化指标
```yaml
unit: "%"
direction: higher_better | lower_better | target_band
window: 月度 | 滚动四季度 | ...
source: CON-xxx.<接口名>   # 数据从哪来,必须可回溯
target: {value: 65, band: ±5}      # 可选
```

**kind: composite** —— 组合指标
```yaml
formula: "0.5*OBJ-011 + 0.3*OBJ-012 + 0.2*(1-OBJ-013)"   # 只引用 metric 的 ID
rationale: |              # 权重为什么这么定(自然语言)
weights_governed_by: J-xxxx    # 权重调整走判断账本,不许随手改
```

---

## 5. Structure 规范（组合骨架，v0.3 新增）

刻意保持薄。纪律：**流程只描述「什么喂给什么」，一切「怎么裁」留给判断层。**
想在这里写 if/else 的那一刻，它就是一条该进 `judgments/` 的判断（账本纪律第 5 条）。

```yaml
structure_id: STR-001
pipeline:
  - step: <步骤名>          # 包内唯一;policy.yaml 权限表以此为键
    performer: agent | connector | optimizer | human | runtime | agent + judgments
    uses: CON-xxx[.<接口名>]  # performer 为 connector 时必填
    input: <见衔接约定>
    produces: <见衔接约定>
    process: <自然语言,可选>
    note: <自然语言,可选>
```

**衔接约定（v0.3 定稿，宽松制）：**
- 步骤间的中间流转，`input` / `produces` 允许**裸字符串**（自然语言名，如「原始费用明细集」）——保住骨架的可读性与「薄」；
- 凡是**进入判断签名**（judgment.signature.object 引用的对象）或**对外交付**的产物，必须写 `{ref: OBJ-xxx}`（可附 `cardinality`），接受 lint 校验；
- `performer: human(<人名>)` 的步骤是飞轮采集点，产出的 diff 由采集器自动落 `cases/`。

**嵌套预留：** 子任务拆为独立 agent 时，以 `sub_agent: <包名>` 引用其独立 `.osca` 包，本文件不膨胀。

---

## 6. Connector 规范（三层分离）

**层1 Manifest（本文件，进包）** —— 类型与纪律
```yaml
connector_id: CON-xxx
name: <中文名>
kind: mcp | openapi | sql_readonly | code
binding_ref: FINANCE_DB   # 必填(v0.3 明确):指向部署环境 binding 的名字,不是值
rationale: |              # 为什么确定性执行
interfaces:
  - name: <接口中文名>
    params: {参数: {type, ref?, required}}
    returns: <类型或 OBJ 引用>
    impl: sql/xxx.sql          # 可选:包内实现,声明即必须存在(lint 校验)
    freshness: <数据新鲜度约定,如"每月8日关账后可用">
    born_reason: <可选:该接口因哪条判断而生>
permissions:
  write: forbidden | allowed_with_approval
  scope: <数据边界描述>
```

**层2 Binding（部署环境，不进包）** —— `bindings.yaml` 由运维在环境注入：
```yaml
FINANCE_DB:
  endpoint: <真实URL/连接串>
  secret_ref: FINANCE_DB_RO_KEY    # 密钥名,值在 secret manager
```
包内仅保留 `bindings.example.yaml`（同结构、占位值）作为模板。
**`osca pack` 检测到真实 `bindings.yaml` 会拒绝打包。**

**层3 Impl** —— 优先级：现成 MCP server > OpenAPI 描述 > 包内代码(`impl: sql/xxx.sql`)。
自研代码是最后手段。

---

## 7. Aware 规范（v0.3 重写：触发原语列表 + 闸门）

> v0.2 的「单 trigger + aware 级 kind」写法废止。定稿为样例包写法：
> 触发原语逐条注册（可跨 Aware 共享去重），触发 ≠ 唤醒，闸门裁决——
> 与运行框架的触发表/闸门设计一一对应。

```yaml
aware_id: AW-xxx
name: <中文名>
enabled: true             # 三级停的「触发器停」靠它

triggers:                 # ≥1 条;每条编译注册进运行时触发表
  - id: T1
    kind: schedule
    schedule: "每月9日 09:00"
  - id: T2
    kind: event
    source: webhook | 飞书消息 | 人工触发
    filter: <可求值谓词,如 "payload.单位 in 重点单位清单">
  - id: T3
    kind: watch           # 轮询 + 差分谓词,合成事件
    uses: CON-xxx.<接口名>  # 观察谁
    every: 4h             # 轮询周期
    state_key: <缓存哪个字段作为状态>
    emit_when: "new.状态 != old.状态 && new.状态 == '停机'"

gate:                     # 触发命中 ≠ 唤醒;此处裁决。装载时做编译期矛盾检查
  combine: any | all | sequence
  precondition: <可求值谓词>
  debounce: <冷却窗口,如 72h>    # v0.3 定稿:debounce 属于 gate,不在 aware 顶层
  on_fail: <重试/通知策略>

then: <STR-xxx 或其某 step>       # 醒来干什么
budget: {max_steps: 40, max_minutes: 15, max_tokens: 200k}   # 剧集硬顶
discretion: |             # 自然语言余量,唤醒后注入剧集上下文(有界主动)
```

watch 型由运行时维护 last_state；`emit_when` 为真 → 合成事件 → 走 event 通路。

---

## 8. Policy 规范（笼子，运行时强制，模型不读）

```yaml
policy_version: 1
permissions:              # 按 pipeline 步骤的工具白名单;step 名须存在于 structure
  - step: 取数
    allow: [CON-001.拉取费用明细, CON-001.拉取检修计划期]
  - step: 成文
    allow: []             # 成文步骤调不动任何 Connector
egress:
  allow_domains: []       # 默认全禁,白名单放行
data:
  redact: [身份证号, 手机号]     # 注入剧集前脱敏
  scope: 数据不出集团合并库
approvals:                # 人批关卡
  - action: 终稿发送管理层
    approver: 专家
budgets:
  per_episode: {max_tokens: 200k, max_tool_calls: 30}
kill_switch:              # 账本健康度当安全信号
  - when: 近30天 overruled/confirmed > 0.3
    do: 挂起全部 watcher 并通知操作者
  - when: 回放红灯率 > 20%
    do: 同上
```

原则：AGENT.md 的边界 = 劝告层（塑造行为）；policy.yaml = 强制层（兜底越权）。
模型被注入/被替换时，笼子仍然有效。

---

## 9. Judgment 文件规范（v0.3 新增：五段解剖定稿）

```yaml
judgment_id: J-xxxx
status: active | superseded | review
supersedes: J-xxxx | null # 推翻旧判断时填;被指向者 status 必须改 superseded(双向,lint 校验)

signature:                # ① 签名:我对什么生效(类型系统的落地处)
  object: OBJ-xxx
  aware: AW-xxx
  guard: <可求值谓词,如 "费用科目 == 差旅费 && 环比涨幅 > 30">

body: |                   # ② 函数体:判断本身(1-3句自然语言,带除非子句)

evidence:                 # ③ 出生证据:≥1 条,引用 cases/,不内联
  - C-xxxx

meta:                     # ④ 元数据:信任分
  author: <专家名>
  distilled_by: <蒸馏批次>
  confirmed_at: <日期>
  confirmed: <int>        # 被默认接受次数,采集器自动累加
  overruled: <int>        # 被推翻次数
  trust: provisional | high | review
                          # 规则:confirmed≥5 且 overruled==0 → high;
                          # 由计数自动驱动,人不手改;superseded 时冻结

expiry:                   # ⑤ 失效条件:防腐烂(建议必写,lint 记警告)
  - <什么变了本判断就该重审>

replay:                   # 回放断言 = 本判断的单元测试,≥1 条
  - given: C-xxxx.input
    with_this_judgment: <期望行为>
    without_this_judgment: <可选:对照行为>
```

负判断（压制动作的判断）与正判断同权。判断只能从专家真实编辑行为蒸馏而来；
专家主动口述的规则也要落一条 `kind: 口述` 的 case 作为出生证据。

---

## 10. Case 文件规范与存储契约（v0.3 新增/合并）

```yaml
case_id: C-xxxx
captured_at: <时间>
capture_source: <采集点,如 "报告终审界面 diff 监听" / "建包访谈（口述转录）">
kind: 口述                # 可选;口述证据必标
report: <可选:所属产出物上下文>

input:                    # 触发上下文,回放时复现用
  <情境字段>: ...
  当时生效判断集: [J-xxxx]  # 必存;无此字段回放不可信

# ── 证据两物种,至少居其一 ──
agent_draft: |            # 物种一:专家 diff(原始改前)
expert_final: |           #        (原始改后,一字不动地存)
expert_remark: <可选:终审界面备注框的嘟囔>
outcome: {...}            # 物种二:decision vs reality 对账(闭环场景,由对账器落)

distillation:             # 蒸馏状态
  status: pending | distilled | archived
  batch: <蒸馏批次>
  resulted_in: J-xxxx     # 产出了哪条判断
  note: <可选>
```

**存储契约：**
- cases 只经判断的 `evidence` 引用到达；不进语义索引，不被扫描
- 大报文外置对象存储：YAML 内存**逻辑指针** `{content_hash, store: <binding名>, key: <对象键>}`，
  内容寻址防篡改。（v0.3 修正：不再写完整 `uri`——完整 URL 违反 §13 铁律；
  存储端点走部署环境 binding 解析）
- 蒸馏后未产出判断的 cases 按季度归档出仓（`distillation.status: archived`）

---

## 11. 判断检索契约（两段式）

1. **类型硬过滤**（运行时，确定性）：用当前情境(object, aware, guard 变量)
   扫 `indexes/judgments.index.yaml` 签名表 → 候选集（通常 <30 条）
2. **语义排序**（embedding）：仅在候选桶内 → top 3–7 注入，各带 1 个代表 case
3. 永不全库向量检索；永不整库注入

签名表由 `osca load` 与 commit 钩子重建，人不手写。judgments >200 条后按 object 分目录。

---

## 12. 账本纪律（不变量）

1. judgments 只追加；推翻用 `supersedes`，被取代文件改 `status: superseded`，不删除
2. 每条判断必须有 ≥1 条 evidence（无出生证据的判断不准入账）
3. 判断只能从专家真实编辑行为蒸馏而来；禁止凭空手写"我觉得应该"型判断
   （专家主动口述的规则也要落一条 case: kind=口述,作为证据）
4. trust 升降由 confirmed/overruled 计数自动驱动，人不手改；每条判断自带回放断言
5. structure 里不许写 if/else；想写的那一刻,它就是一条该进 judgments 的判断

以上全部由 `osca lint` 机器执行（规则清单：`docs/OSCA-LINT-RULES.md`）。

---

## 13. 安全铁律（v0.3 精确化）

**绝对禁止（任何包内文件，含 .md / .sql）：**
- 密钥、token、私钥及其特征（AccessKey、`-----BEGIN PRIVATE KEY-----` 等）
- 任何形态的**连接串与可连接端点**：`jdbc:`、`mysql://`、`postgres://`、`ssh://`、
  `redis://`、`mongodb://` 等协议，含凭据的 URL，IP+端口
- 真实部署绑定文件 `bindings.yaml`（`osca pack` 检测到即拒绝打包）

**有限允许：**
- 指向**公开文档**的 `https` 链接（许可证、规范出处等），以 lint 维护的
  白名单域名为准；白名单之外一律报错。`http://` 明文链接一律禁止。
- 大报文外置引用必须用 §10 的逻辑指针形态，不得内联完整 URL。

---

## 14. 工具链契约（v0.3 新增）

- **`osca lint <包目录>`** —— 账本纪律与本规范的机器化。错误挡通过（退出码 1），警告不挡。
- **`osca pack <包目录>`** —— lint 不过不打包；排除 `indexes/`、`.git/`、系统垃圾文件；
  拦截真实 `bindings.yaml`；生成 `indexes/checksums.txt` 完整性清单；
  **可复现打包**（同内容 → 同字节 → 同哈希），交付件可签名。
- **`osca load <zip|目录>`** —— 四步：完整性校验（防篡改，交付件必须带清单）→ lint →
  binding 与部署环境比对（缺失即报错）→ 重建 `indexes/judgments.index.yaml` 签名表。
  索引是缓存：坏了删掉重建，不备份（公理 A4）。

---

## 附录｜v0.2 → v0.3 变更记录

**补齐（v0.2 缺失的规范，均以样例包 + 参考实现互证后定稿）：**
- §0 目录树补入 `osca.yaml`（v0.2 遗漏）与 `indexes/checksums.txt`
- §1 osca.yaml 身份证字段规范（新增）
- §5 structure.yaml 字段规范与衔接约定（新增）
- §9 judgment 文件五段解剖（新增；此前仅存在于架构文档）
- §10 case 文件规范（新增，并入原 §8 存储契约）
- §14 工具链契约（新增）

**定稿（规范与样例的分歧，五处）：**
- Aware 触发写法：以样例为准——`triggers` 列表 + `gate` 闸门块；v0.2 单 `trigger` 写法废止（§7）
- `debounce` 归属 `gate`，不在 aware 顶层（§7）
- 中间产物归 artifact：`medium: payload` + `delivery: internal`，不新增第五型（§4）
- structure 衔接宽松制：中间流转允许裸字符串；进判断签名或对外交付必须 `{ref: OBJ-xxx}`（§5）
- URL 铁律精确化：连接串/端点绝对禁止；公开文档 https 链接白名单放行（§13）

**修正：**
- connector manifest 的 `binding_ref` 明确为必填（§6；样例包曾遗漏，已由 lint 抓出）
- cases 大报文外置指针改逻辑形态 `{content_hash, store, key}`，废止完整 `uri`（§10，与 §13 铁律一致）

**v0.2 原文保留：** 双平面架构、命名与 ID、Object 四型框架、Connector 三层分离、
Policy 笼子、判断检索契约、账本纪律五条。
