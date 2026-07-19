> 本规范文本以 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh) 开放：可自由转载与改编，须署名并注明出处。
> 本仓库中的代码与样例以 Apache-2.0 授权。/ Specification text: CC BY 4.0. Code & examples: Apache-2.0.

# OSCA 包格式规范 v0.4

> 一个 agent = 一个 `.osca` 文件夹 = 一个 git 仓库。
> 全部纯文本（Markdown + YAML）。包是可交付、可审计、可打印的资产。
> 铁律：包内不得出现任何密钥、连接串、可连接端点（精确边界见 §13）。
>
> **v0.4 = v0.3 全文 + 参考实现互证的增量定稿。** v0.3（tag `spec-v0.3`）之后，运行框架 Host（M2）
> 与蒸馏管道（M3）的参考实现反过来喂养规范：机器布防不了的语法就不配进规范。v0.4 把这些增量
> 并入全文——受限触发语法、运行时求值参考语义、剧集执行语义、objective 第五型、判断分层权属三字段、
> case kind 引用、以及企业系统对接约定附录。变更全录见文末 **附录 D**。
>
> **本文状态：定稿。** M2/M3 增量（§2–§10 + 附录 A）、企业系统对接约定（附录 B）、判断库包
> 变体规范（附录 C，M6-W3 定稿）均已并入，已取代 `OSCA-SPEC-v0.4-draft.md`；变更全录见附录 D。

---

## 0. 目录树（v0.4）

```
<agent名>.osca/
├── osca.yaml                     # 身份证:包名/版本/依赖(§1)
├── AGENT.md                      # 入口:身份/目标/边界(劝告层,模型读)
├── policy.yaml                   # 笼子(强制层,运行时读,模型不读)
├── structure.yaml                # 组合骨架(薄,§5)
├── objects/
│   └── OBJ-002-费用异动报警.yaml
├── connectors/
│   └── CON-001-财务系统.yaml      # 仅 manifest,无密钥(§6 + 附录 B)
├── aware/
│   └── AW-001-月度扫描.yaml       # 受限触发语法(§7)
├── judgments/
│   ├── J-0417.yaml               # 五段解剖 + 分层三字段(§9);>200条后按 object 分目录
│   └── J-0423.yaml
├── cases/
│   └── C-0091.yaml               # 元数据+指针;大报文外置(§10)
├── sql/ …                        # 可选:connector 的包内 impl(§6 层3)
├── indexes/                      # 机器生成的缓存,人不手写,坏了重建(公理 A4)
│   ├── checksums.txt             # osca pack 生成的完整性清单(§14)
│   ├── judgments.index.yaml      # 签名表(硬过滤用),osca load 重建
│   ├── judgments.vectors.json    # 向量 flat 索引(语义排序用,可选;嵌入未配则只建签名表)
│   └── replay-health.json        # 回放器整本体检档案(kill switch 数据源,附录 A)
└── bindings.example.yaml         # 部署绑定模板;真实 binding 在部署环境,永不进包
```

---

## 1. osca.yaml（身份证）

```yaml
format: osca                      # 固定值
format_version: "0.4"             # 本规范版本
package_id: demo-group-oper-diagnosis   # 仅小写字母/数字/连字符;交付件以此命名
name: 示例集团经营诊断
entry: AGENT.md                   # 入口文件,必须存在
requires:
  runtime: ">=0.2"                # 运行框架最低版本
  bindings: [FINANCE_DB]          # 部署环境必须注入的 binding;与各 connector
                                  # binding_ref 的并集一致(lint 校验),loader 缺失即报错
integrity: indexes/checksums.txt  # osca pack 生成;交付件完整性清单,可对包签名

layering:                         # 可选:包级分层权属默认段(§9);蒸馏 confirm 出生判断按此填三字段
  scope: org                      # 缺此段则新生判断永远缺三字段,带 OSCA060 警告
  provenance: {origin: client-derived, source: demo-group, rights: client-owned}
  classification: internal
```

**`layering` 包级默认段（可选，OSCA061 校验）：** 为本包新生判断提供 scope / provenance /
classification 的默认值——蒸馏 `confirm` 出生一条判断时，若判断自身未带这三字段（当前蒸馏候选
不产它们），即从本段填入（污染不可逆，出生即标，§9.1）。可部分声明；present 即按 §9 洁净室判据
校验（错的默认在 osca.yaml 源头即拦，比逐条 judgment 报早）。缺此段合法——判断缺字段自有 OSCA060 警告。

**`package_kind` 包类型（可选，缺省 `agent`）：** `agent`（可运行 agent 包，本规范 §0–§14 全适用）
或 `library`（判断库包变体——只装判断货架、无运行面，免 pipeline/Aware）。库包变体的完整规范见**附录 C**
（规范语义定稿，cli/host 实现推 Phase 1）。

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
剧集跑完 pipeline 即终止。**不存在持续运行的模型。** 剧集执行的 performer 受限集、预算记法与
三种终态（completed / stopped / failed）见 **附录 A**。

**三级停**
- 剧集停：pipeline 完成 / 到达 human 采集点 / 触发 Aware 的 `budget` 硬顶（步数/token/时长）/ 步骤失败
- 触发器停：单个 Aware 文件 `enabled: false`
- 包停：注销全部 watcher

---

## 3. 命名与 ID 规则

- 文件名格式：`<ID>-<中文名>.yaml`，如 `OBJ-002-费用异动报警.yaml`
  （judgments/cases 数量大，允许省略中文名，仅 `J-0417.yaml`）
- ID 格式：类型前缀 + 包内自增。`OBJ- / STR- / CON- / AW- / J- / C-`
- ID 一经分配**永不修改、永不复用**；中文名可随时改
- **跨文件引用只允许用 ID**，禁止用文件名或中文名
- **跨包引用用限定形式 `<package_id>/<judgment_id>`**（package_id 不含 `/`，无歧义）。
  judgment_id 保持包内局部形式（如 `J-0417`），**不带层前缀**——ID 语法与「文件名=ID」纪律
  （OSCA010/011）不因分层而变（§9）。现行五段中没有跨包引用字段（`supersedes` 限同包同层）；
  规划中的 `overrides` 与 Manifest `dependencies` 将使用限定形式（语法本版钉死，机制见附录 C）。

---

## 4. Object 规范（五型）

公共字段：
```yaml
object_id: OBJ-xxx
name: <中文名>
kind: entity | artifact | metric | composite | objective
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

**kind: artifact** —— 产出物（含中间产物）
```yaml
schema: {...}
medium: document | message | table | payload   # payload = 结构化中间物
delivery: 飞书文档 | 对话回复 | 邮件 | API回传 | internal   # internal = 流程内部流转,不对外交付
quality_bar: |            # 产得好的标准,专家验收的依据
```
> 步骤间流转的中间产物（如报警候选）归 artifact，标 `medium: payload` + `delivery: internal`，不新增专门型。

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

**kind: objective** —— 寻优目标（v0.4 收编第五型）
§5 的 optimizer performer 与附录 A 的 settle 对账均以 objective 型对象为锚点；机器可执行的语义
必须配套词表，否则规范自相矛盾。
```yaml
object_id: OBJ-xxx
name: <中文名>
kind: objective
version: <int>
definition: |               # 这个目标为什么值得追(自然语言,必填)
optimize: maximize | minimize   # 寻优方向,必填——optimizer 的排序依据
constraints:                # 约束声明,自由文本列表——留档给人审,机器不解析
  - <约束一句话>
settle: {uses: CON-xxx.接口名, when: <自由文本>}   # 对账声明,可选(附录 A 受限形式)
```
- `optimize` 之外的数值语义（约束求解、bandit）属部署侧演进，规范只锚定方向；
- optimizer 步骤要求剧集上下文中**恰好一个** objective 对象（多于一个时以步骤字段 `objective: OBJ-xxx` 指定）。

---

## 5. Structure 规范（组合骨架）

刻意保持薄。纪律：**流程只描述「什么喂给什么」，一切「怎么裁」留给判断层。**
想在这里写 if/else 的那一刻，它就是一条该进 `judgments/` 的判断（账本纪律第 5 条）。

```yaml
structure_id: STR-001
pipeline:
  - step: <步骤名>          # 包内唯一;policy.yaml 权限表以此为键
    performer: agent | connector | optimizer | human | runtime   # 受限集,组合写法如 "agent + judgments"、"human(王工)"
    uses: CON-xxx[.<接口名>]  # performer 为 connector 时必填
    input: <见衔接约定>
    produces: <见衔接约定>
    process: <自然语言,可选>
    note: <自然语言,可选>
budget: {max_steps: 40, max_minutes: 15, max_tokens: 200k}   # 亦可在 Aware 上声明;剧集硬顶(附录 A)
```

**衔接约定（宽松制）：**
- 步骤间的中间流转，`input` / `produces` 允许**裸字符串**（自然语言名，如「原始费用明细集」）——保住骨架可读性与「薄」；
- 凡是**进入判断签名**（judgment.signature.object 引用的对象）或**对外交付**的产物，必须写 `{ref: OBJ-xxx}`（可附 `cardinality`），接受 lint 校验；
- `performer: human(<人名>)` 的步骤是飞轮采集点，产出的 diff 由采集器自动落 `cases/`。

**嵌套预留：** 子任务拆为独立 agent 时，以 `sub_agent: <包名>` 引用其独立 `.osca` 包，本文件不膨胀。

> performer 各角色的执行参考语义（connector 取数纪律、agent 归属标注、optimizer 受限输入、
> human 采集点、runtime 移交对账）与预算数量记法、剧集三终态，见 **附录 A**。

---

## 6. Connector 规范（三层分离）

**层1 Manifest（本文件，进包）** —— 类型与纪律
```yaml
connector_id: CON-xxx
name: <中文名>
kind: mcp | openapi | sql_readonly | code
binding_ref: FINANCE_DB   # 必填:指向部署环境 binding 的名字,不是值
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

**层3 Impl** —— 优先级：现成 MCP server > OpenAPI 描述 > 包内代码(`impl: sql/xxx.sql`)。自研代码是最后手段。

> 取数走固定具名接口、执行器分派约定、真实 sql_readonly/openapi 契约、read-only enforcement、
> secret 解析、以及 `permissions.write` 的写路径（审批门 + 挂起-等批-恢复消费语义）见
> **附录 B · 企业系统对接约定**。

---

## 7. Aware 规范（受限触发语法 + 闸门）

v0.3 样例中 `schedule: "每月9日 09:00"` 是自由文本，机器不可解析、跨实现不可移植，**废止**。
v0.4 起触发原语全部采用受限语法；不在字段集内的键一律报错（lint 规则 OSCA041，参考实现
`osca_cli.triggers` 同时供 lint 与 Host 编译期共用——语法只定义一次）。

```yaml
aware_id: AW-xxx
name: <中文名>
enabled: true             # 三级停的「触发器停」靠它

triggers:                 # ≥1 条;每条编译注册进运行时触发表
  - id: T1
    kind: schedule
    schedule: {every: month, day: 9, time: "09:00"}   # 结构化字段
    note: 财务关账次日                                  # 自由文本注释,机器不读
  - id: T2
    kind: watch           # 轮询 + 差分谓词,合成事件
    uses: CON-xxx.<接口名>  # 观察谁
    every: 4h             # 时长语法
    state_key: <缓存哪个字段作为状态>
    emit_when: "new.状态 != old.状态 && new.状态 == '停机'"
  - id: T3
    kind: event
    source: webhook | 飞书消息 | 人工触发   # 自由文本;运行时由操作者通道人工发射

gate:                     # 触发命中 ≠ 唤醒;此处裁决。装载时做编译期矛盾检查
  combine: any | all | sequence   # 缺省 any;all/sequence 要求 ≥2 条触发原语(否则编译期矛盾,装载拒绝)
  precondition: <可求值受限形式,见附录 A>
  debounce: <时长语法,如 72h>
  on_fail: <重试/通知策略,声明性文本>

then: <STR-xxx 或其某 step>       # 醒来干什么
budget: {max_steps: 40, max_minutes: 15, max_tokens: 200k}   # 剧集硬顶
discretion: |             # 自然语言余量,唤醒后注入剧集上下文(有界主动)
```

### 7.1 时长语法（duration）
`<正整数><单位>`，单位 ∈ `s | m | h | d`（秒/分/时/天）。例：`24h`、`72h`、`30m`。
`0` 值非法；不接受小数、复合写法（`1h30m`）与其他单位。适用字段：`watch.every`、`gate.debounce`。

### 7.2 schedule（定时器）
| 字段 | 必填 | 约束 |
|---|---|---|
| `every` | 是 | `day` \| `week` \| `month` |
| `day` | every=month/week 时必填 | month：整数 1..31；week：`mon`..`sun`；every=day 时**不得给** |
| `time` | 是 | 24 小时制 `"HH:MM"` |
| `tz` | 否 | IANA 时区名（如 `Asia/Shanghai`）；缺省取 Host 部署环境时区 |

语义定稿：`day` 超出当月天数时**取当月最后一天**（与主流调度器一致，如 `day: 31` 在 2 月触发于月末）。

### 7.3 watch（轮询器）
| 字段 | 必填 | 约束 |
|---|---|---|
| `uses` | 是 | Connector 接口引用（`CON-xxx.接口名`） |
| `every` | 是 | 时长语法（§7.1） |
| `state_key` | 否 | 状态比对键 |
| `emit_when` | 否 | 发射条件表达式（`old.*` / `new.*`）；求值语义见附录 A |

### 7.4 event（事件）
| 字段 | 必填 | 约束 |
|---|---|---|
| `source` | 是 | 触发来源说明（自由文本）；运行时由操作者通道人工发射 |

### 7.5 闸门与组合语义
gate 允许字段集：`combine` / `precondition` / `debounce` / `on_fail`；集外任何键报错。
- `combine` ∈ `any` | `all` | `sequence`，缺省 `any`；**`all` / `sequence` 要求 ≥2 条触发原语**，否则编译期矛盾、装载拒绝。
- `debounce` 必须是合法时长语法。
- `precondition` / `on_fail` 为声明性文本，求值与执行语义见附录 A。

组合语义（运行框架约定，入规范以保可移植）：
- `any`：任一触发命中 → 过闸门。
- `all`：自上次唤醒起，全部触发原语各至少命中一次 → 过闸门并重置。
- `sequence`：按声明顺序依次命中 → 过闸门并重置；乱序命中即重置（若乱序命中的恰是首位，视为新序列开始）。
- `debounce`：唤醒后的抑制窗口，窗口内再次过闸门只计数不唤醒。
- `enabled: false` 的 Aware 不布防触发原语（三级停之「触发器停」）。

每条触发原语必有 `id`（包内 Aware 级唯一，如 `T1`）与 `kind`；全局引用形如 `AW-001/T1`。
各 kind 的允许字段集之外出现任何键即报错（受限语法的含义：宁可拒绝，不可猜测）。

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
approvals:                # 人批关卡(高危写动作;审批门语义见附录 B)
  - action: 批量改价
    approver: 店长
budgets:
  per_episode: {max_tokens: 200k, max_tool_calls: 30}
kill_switch:              # 账本健康度当安全信号(可求值形式见附录 A)
  - when: 近30天 overruled/confirmed > 0.3
    do: 挂起全部 watcher 并通知操作者
  - when: 回放红灯率 > 20%
    do: 同上
```

原则：AGENT.md 的边界 = 劝告层（塑造行为）；policy.yaml = 强制层（兜底越权）。
模型被注入/被替换时，笼子仍然有效（公理 A5：劝告与笼子分离，模型永不读 policy.yaml）。

---

## 9. Judgment 文件规范（五段解剖 + 分层权属三字段）

```yaml
judgment_id: J-xxxx
status: active | superseded | review
supersedes: J-xxxx | null # 推翻旧判断时填;被指向者 status 必须改 superseded(双向,lint 校验)

# ── 分层与权属(v0.4 §9;新生判断必填,存量包过渡期为 lint 警告) ──
scope: org                      # commons | org
provenance:
  origin: client-derived        # own-ops | public-standard | client-derived | licensed
  source: demo-group            # 从谁的边界里长出来:客户代号 / 标准编号 / 自营业务名
  rights: client-owned          # 权属结论——应镜像到合同条款;缺合同映射的权属字段是装饰品
classification: internal        # public | internal | restricted(密级)

signature:                # ① 签名:我对什么生效(类型系统的落地处)
  object: OBJ-xxx
  aware: AW-xxx
  guard: <「可求值风格」自由文本命中条件,如 "费用科目 == 差旅费 && 环比涨幅 > 30"；v0.4 无受限求值语法,不参与硬过滤(§11)>

body: |                   # ② 函数体:判断本身(1-3句自然语言,带除非子句)

evidence:                 # ③ 出生证据:≥1 条,引用 cases/,不内联
  - C-xxxx

meta:                     # ④ 元数据:信任分
  author: <专家名>
  distilled_by: <蒸馏批次>
  confirmed_at: <日期>
  confirmed: <int>        # 被默认接受次数,采集器自动累加
  overruled: <int>        # 被推翻次数
  trust: provisional | high | review    # confirmed≥5 且 overruled==0 → high;计数自动驱动,superseded 冻结

expiry:                   # ⑤ 失效条件:防腐烂(建议必写,lint 记警告)
  - <什么变了本判断就该重审>

replay:                   # 回放断言 = 本判断的单元测试,≥1 条(机器判据见附录 A)
  - given: C-xxxx.input
    with_this_judgment: <期望行为>
    without_this_judgment: <可选:对照行为>
```

负判断（压制动作的判断）与正判断同权。判断只能从专家真实编辑行为蒸馏而来；专家主动口述的规则
也要落一条 `kind: 口述` 的 case 作为出生证据。

### 9.1 判断分层命名空间（commons / org）

判断资产存在两层命名空间：**行业公共层**（`commons`——可迁移、无密级、跨包授权复用的判断，
如公文规范类、外呼合规类）与**企业私有层**（`org`——留在客户边界内的判断，如组织偏好类）。
分层的前提是权属血统可证，而**血统无法事后重建**——若干月后没人能证明一条判断究竟从谁的边界
里长出来，未标注的存量只能全部按最严格权属处理。故三字段随判断出生落盘，lint 机器布防（OSCA060）。

**洁净室规则（OSCA060 机器布防，error 级）：**
- `origin: client-derived` 的判断出生即 `scope: org`，**永不静默迁移**——`scope: commons` 且 `origin: client-derived` 为 lint 错误；
- 进入 commons 只有三个合法入口：自营业务判断（`own-ops`）、公共标准编纂（`public-standard`）、合同明确授权的贡献（`licensed`）；
- commons 层定义 = 可迁移**且**无密级：`scope: commons` 要求 `classification: public`；
- 自营判断同样逐条标注——运营主体自己的账本里也混着带客户方言的 `client-derived` 条目与行业通用的 `own-ops` 条目，自营不等于全部可公共化。

**出生即标的落点（confirm）：** 蒸馏 `confirm` 出生一条判断时按包级 `layering` 默认段（§1，OSCA061 校验）
填三字段（判断自带的优先，缺则由默认段填，再缺则留空 → OSCA060 警告）。造包器（Creator）生成的
osca.yaml 默认段取**最严档**（`org` / `client-derived` / `internal`）——血统不可逆，错过按最严处理（§9.1），
own-ops / public-standard 包须显式改宽。

**晋升是转世，不是复制**：org 判断晋升 commons 时，其出生证据（含客户报文的 cases）留在原边界内
不随行；晋升后的判断须在 commons 层重新积累 evidence 与信任计数（从零挣）。这是晋升的真实成本，
也是它的天然刹车。晋升管线本身不在本版规范。

### 9.2 引用语法与规划字段

- `judgment_id` 保持包内局部形式，**不带层前缀**；跨包引用一律用限定形式 `<package_id>/<judgment_id>`（§3）。
- 现行五段中没有跨包引用字段（`supersedes` 限同包同层：同层版本继承，旧判断退役、历史计数保留）。
  规划中的两处使用限定形式，本版仅钉语法防歧义、不定义机制（机制见附录 C）：
  - `overrides: <package_id>/<judgment_id>`——跨层遮蔽：私有判断在本包内压掉一条公共判断；被遮蔽者
    不死、不迁移状态，仅从本包检索候选中剔除。与 supersedes 是两种关系，不得混用同一字段（版税与信任统计口径不同）；
  - Manifest `dependencies:`——引用判断库包（仅含 judgments 与 cases 指针、无 pipeline/Aware 的 `.osca`
    变体）须锁版本并带完整性哈希：有效账本 = 本地层 + 钉死版本的公共层，组合必须仍可复现、可打印、可审计（§13 铁律的延伸）。
- 公共层遥测原则先行钉死：跨边界只回传**条目级计数**（引用次数 / override 事件 / 回放红绿灯），不回传情境与输出内容。

---

## 10. Case 文件规范与存储契约

```yaml
case_id: C-xxxx
captured_at: <时间>
capture_source: <采集点,如 "报告终审界面 diff 监听" / "建包访谈（口述转录）" / "标准编纂">
kind: 口述 | 引用          # 可选;非 diff/outcome 的证据物种必标(见下)
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

**case kind 词表：** case 的出生形态分四种——
- **diff**（默认，不标）：专家 diff（`agent_draft` + `expert_final`），判断厚场景的主粮；**唯一可 A/B 回放**（附录 A）；
- **outcome**（不标，由对账器落）：decision vs reality 对账（闭环场景，附录 A settle）；
- **口述**（`kind: 口述`）：专家主动口述的规则须落一条口述 case 作为出生证据（账本纪律 3）；
- **引用**（`kind: 引用`，v0.4 收编）：条文/标准引用型 case——公共标准编纂类判断（`provenance.origin:
  public-standard`）的天然出生证据形态。`input` 载条文依据与（可为合成的）反例摘录；其 replay 断言
  基于合成反例、非真实 diff，机器判据在无真实 diff 时的退化口径见附录 C（库包 replay）。

**存储契约：**
- cases 只经判断的 `evidence` 引用到达；不进语义索引，不被扫描；
- 大报文外置对象存储：YAML 内存**逻辑指针** `{content_hash, store: <binding名>, key: <对象键>}`，
  内容寻址防篡改（完整 URL 违反 §13 铁律；存储端点走部署环境 binding 解析）；
- 蒸馏后未产出判断的 cases 按季度归档出仓（`distillation.status: archived`）。

---

## 11. 判断检索契约（两段式）

1. **类型硬过滤**（运行时，确定性）：用当前情境的 **object × aware 合取**扫
   `indexes/judgments.index.yaml` 签名表 → 候选集（通常 <30 条）。**guard 不参与硬过滤**：
   它是自由文本「可求值风格」命中条件（附录 A 受限可求值形式**不含** guard，其变量在
   装配时刻尚未绑定）——随判断注入后由模型在语境中应用，事后由回放判据（附录 A）体检；
   guard 受限语法与检索前确定性求值属后续版本（机器布防不了的语法不进确定性契约）。
   **提示词契约**：运行框架注入判断时须明示「guard 未判定」，并要求模型应用前逐条判定——
   guard 不命中或无法判断的判断不得应用、不得标注其 ID（归属计数由此不被未判定注入污染）
2. **语义排序**（embedding）：仅在候选桶内 → top 3–7 注入，各带 1 个代表 case
3. 永不全库向量检索；永不整库注入

签名表由 `osca load` 与 commit 钩子重建，人不手写。judgments >200 条后按 object 分目录。
退化纪律（无 query / 无索引 / 嵌入未配置 / 索引模型不符）→ 回退 trust/confirmed 排序，全留痕。

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

## 13. 安全铁律

**绝对禁止（任何包内文件，含 .md / .sql）：**
- 密钥、token、私钥及其特征（AccessKey、`-----BEGIN PRIVATE KEY-----` 等）
- 任何形态的**连接串与可连接端点**：`jdbc:`、`mysql://`、`postgres://`、`ssh://`、`redis://`、`mongodb://` 等协议，含凭据的 URL，IP+端口
- 真实部署绑定文件 `bindings.yaml`（`osca pack` 检测到即拒绝打包）

**有限允许：**
- 指向**公开文档**的 `https` 链接（许可证、规范出处等），以 lint 维护的白名单域名为准；白名单之外一律报错。`http://` 明文链接一律禁止。
- 大报文外置引用必须用 §10 的逻辑指针形态，不得内联完整 URL。

---

## 14. 工具链契约

- **`osca lint <包目录>`** —— 账本纪律与本规范的机器化。错误挡通过（退出码 1），警告不挡。
- **`osca pack <包目录>`** —— lint 不过不打包；排除 `indexes/`、`.git/`、系统垃圾文件；拦截真实
  `bindings.yaml`；生成 `indexes/checksums.txt` 完整性清单；**可复现打包**（同内容 → 同字节 → 同哈希），交付件可签名。
- **`osca load <zip|目录>`** —— 四步：完整性校验（防篡改，交付件必须带清单）→ lint → binding 与部署
  环境比对（缺失即报错）→ 重建 `indexes/judgments.index.yaml` 签名表。索引是缓存：坏了删掉重建，不备份（公理 A4）。
- **`osca replay <J-id>`** —— 单条判断 A/B 体检（发布凭据第三样；机器判据见附录 A）。

---

## 附录 A. 运行时求值参考语义

> v0.3 中 precondition / emit_when / kill_switch / settle 等均为声明性文本。参考实现给出**可求值受限
> 形式**；不合形式的声明不报错、不生效——保守默认（precondition 放行、emit_when 不发射、kill_switch
> 不触发、settle 不执行）并留痕。「不可求值记警告、不生效」只给**合法形状的自由文本条件**；配置形状
> 非法（非 list / 项非 mapping / when 非字符串）= 配置错误即停机（fail-closed）。

### A.1 precondition（闸门前置条件）
`CON-xxx.接口名(参数) 返回非空`。经 Connector 代理真调用：返回空或取数失败 → 拦截唤醒并复述 `on_fail`
声明（顺延重试的执行属对账/重试机制，后续版本落地）。

### A.2 emit_when（watch 发射条件）
以 `&&` 连接的比较子句，字段取自 `old.*` / `new.*`，比较符 `==` / `!=`，字面量 true/false/null、数字，
其余按字符串比对。例：`old.已关账 == false && new.已关账 == true`。**无 emit_when 时按状态变化发射**；
首轮建立基线不发射。**watch 去重域**：schedule 纯时间可跨包共享；watch 数据绑定在包上，去重共享只在包内。

### A.3 kill_switch（policy.yaml，账本健康即安全信号）
两种可求值形式：
- ① `overruled/confirmed > X`——计数口径：**现役（active）判断合计**（被取代判断的计数随取代冻结成历史）。
- ② `回放红灯率 > X%`——数据源是回放器整本体检生成的**健康档案缓存** `indexes/replay-health.json`
  （公理 A4：机器生成、坏了可重建、不进交付件）。契约：

  ```json
  {"generated_by": "…", "at": "<ISO 时间>", "model": "<体检所用模型>",
   "ledger_tree": "<体检针对的包内容 git tree OID>",
   "total": 9, "green": 7, "red": 1, "error": 1, "red_rate": 0.125,
   "judgments": {"J-0417": {"light": "green", "assertions": 2}}}
  ```

判定数据源是**整数计数**（`red × 100 > X × (green + red)`，Decimal 精确算术、无浮点舍入）；`red_rate`
与 `judgments` 为**必填**，逐项汇总必须与顶层计数对账。error（不可回放）单列不入分母；**可判数
0（green+red==0）= 健康不可判（unavailable）**——体检不发布档案，运行框架不得当 0% 处理。版本归属绑定
**包内容 tree OID**（子目录包不被无关提交作废）且要求包范围工作区干净（含 untracked 与 gitignored）。
kill switch 为**三态**：tripped / clear / unavailable——unavailable 保留既有安全状态、不清除已触发红灯、
也不新触发；进程重启即重评（持久化停机名单归部署侧运维面）。

### A.4 剧集执行参考语义（performer 受限集 / 预算 / 剧集停）
structure.pipeline 的 `performer` ∈ `agent | connector | optimizer | human | runtime`（含组合写法如
`agent + judgments`、`human(王工)`——按关键词识别）；受限集之外直接拒绝，不猜。
- **connector**：经 Connector 代理按名调用（详见附录 B）。任一接口取数失败即剧集失败——没有取数支撑的草稿是编造。
- **agent**：LLM 依一次性上下文出草稿；产出注入剧集台账前过 Policy 脱敏。LLM 通道由部署环境变量配置
  （OpenAI-compatible 线协议，温度恒 0），不锁定厂商、配置永不进包——与 binding 同一纪律。
  **归属纪律**：草稿中依据命中判断的段落须在段末标注该判断 ID（如 `（J-0417）`）——蒸馏管道按引用
  段落在专家终稿中的去留记 confirmed/overruled，段落级标注是账本计数的采集口径（没有标注，判断永远记 uncited）。
- **optimizer**：确定性寻优，LLM 不参与数值搜索（公理 A6）。初版贪心的可求值受限输入：候选为
  `list[dict]`、每项含数值 `value` 字段，按 objective 的 `optimize` 方向排序取最优；缺数值即拒。
- **human**：飞轮采集点，机器侧流水线到此为止（其后步骤待人工环节回执）。
- **runtime**：对账步，移交对账器（A.5），不在剧集内执行。

**预算数量记法**：`<正整数>[k]`（`200k` = 200000）；不可解析的预算 = **额度撤销**（按 0 处理，任何
调用即拒——fail-closed；lint OSCA040 在装载前即报错）。
**剧集停三种终态**：`completed`（pipeline 走完 / 到达 human 采集点）、`stopped`（budget 硬顶——aware.budget
与 policy per_episode 双重；tokens 为**止损顶**：超顶那次调用已发生，就地停）、`failed`（取数失败 /
LLM 不可用 / 声明不合受限形式 / 审批门驳回）。三种终态全部进剧集台账留痕。

### A.5 对账 settle（objective 型 → outcome case）
objective 型对象的 `settle` 声明可求值受限形式：`settle: {uses: CON-xxx.接口名, when: 闭店后}`（when 为
自由文本注释，机器不读）。剧集完成后对账器自动执行：decision（剧集最后一个产出）vs reality（经代理
取数、已脱敏），落一条 `kind: outcome` 的 case（编号顺延、`distillation.status: pending`），不消耗剧集
——现实是第二位专家（公理 A2）。自由文本 settle 不报错、不执行——保守默认留痕。「闭店后」的时刻语义
需要部署侧营业日历，参考实现在剧集完成后立即对账并把 when 留档。

### A.6 回放机器判据（`osca replay`，单条体检）
单判断 A/B：同一 case 情境跑两臂，唯一差异 = 本判断在不在场（case 的「当时生效判断集」两臂共享）。
机器判据（确定性、模型无关）：

```
score(产出) = 相似度(产出, expert_final) − 相似度(产出, agent_draft)
绿灯 ⇔ score(注入) > score(不注入)
```

即「输出从改前移向改后」的字面落地：既奖励靠近专家改后，也奖励离开机器改前（删除类判断靠后一项
仍可判）。断言文本（with/without_this_judgment）是给人读的期望声明，机器不解析；判断 ID 是否被注入臂
引用作为提示信号报告，不作硬判据。**只有 diff 物种（有 `agent_draft` + `expert_final`）的 case 可 A/B
回放**；引用/合成物种的库包 replay 判据见附录 C。

---

## 附录 B. 企业系统对接约定（Manifest / Binding / Impl）

> M6 范围第 1 条：取数走固定接口、三层约定成文。本附录把 §6 的三层分离扩为部署可依据的正式约定，
> 并钉执行器分派、真实执行器契约与写路径语义。**措辞纪律：可求值语义与参考实现互证；纯部署侧契约
> （真实执行器的真系统落地）明标为部署侧适配，不与已落机制混写。**

### B.1 三层职责
- **Manifest（进包，§6 层1）**：类型与纪律。接口的输入输出全部引用 `objects/` 的类型；`impl` 声明
  包内实现（声明即必须存在，OSCA024）；`freshness` 声明数据新鲜度约定（见 B.5）；`permissions.write`
  声明写权限（见 B.4）。**接口漂移编译期爆炸**：调用未在 manifest 声明的接口即报错，不猜。
- **Binding（部署环境，§6 层2）**：`binding_ref` 指向部署环境注入的 binding 名字，**永不进包**；缺失
  即硬错误。binding 含 `endpoint` 与 `secret_ref`（密钥名，值在 secret manager）。
- **Impl（§6 层3）**：优先级 现成 MCP > OpenAPI > 包内代码。

### B.2 取数纪律（公理 A6）
取数是 LLM 最不可靠又最没有判断含量的一层（NL2SQL 硬骨头）。故查询逻辑全部固化为**具名接口**，
模型**按名调用、按类型消费结果，永不写 SQL、永不猜数**。任一接口取数失败即剧集失败——没有取数支撑的草稿是编造。

### B.3 执行器分派约定
Host 的 Connector 代理**按 `binding.endpoint` 的 scheme 选择执行器**（不按 manifest `kind` 分派——kind
是给读包的人看的意图声明）：
- **`mock://<目录>`**——参考实现内置的固件执行器：从 `<目录>/<接口名>.yaml` 读固件（`<接口名>` = 接口
  引用去掉 `CON-xxx.` 前缀），供测试与全链路演练。文件缺失即报错。
- **其余 scheme（真实执行器）**——`sql_readonly` / `openapi` / `mcp` 属**部署侧适配**。约定契约（即使参考
  实现内置执行器暂为占位，部署侧适配须遵守）：
  - **read-only enforcement**：`kind: sql_readonly` 的执行器只允许只读查询；写语义一律拒绝。写权限走
    B.4 的 `permissions.write` + 审批门，不在取数执行器里开写口。
  - **secret 解析**：执行器按 `binding.secret_ref` 向部署环境 secret manager 取值；**secret 值永不进包、
    永不进日志、永不进剧集上下文**。参考实现的 secret 解析到「名字」为止，取值属部署侧适配。
  - **egress 授权**：真实执行器发起外呼前须过 Policy 的 egress 白名单（默认全禁）。
- **注入前脱敏**：任何成功回执在注入剧集台账前过 Policy 脱敏（身份证号/手机号等），脱敏命中数记回执。

### B.4 写路径约定（`permissions.write != forbidden`）
- 写接口必经**审批门**：运行时拦下模型自决的高危写，向 policy 指定的 approver 发一张一次性授权卡
  （走企业 IM 机器人，与专家确认卡同抽象）。审批状态机绑 **approver / 剧集 id / payload 摘要 / 过期**
  （防转发、防重放、防偷梁换柱、防跨剧集串用、防陈旧授权）；`consume_or_raise` 单锁原子。
- **挂起-等批-恢复消费语义**：审批门拦下的写**在本剧集内挂起**（持久态），approve 事件到达后从审批步
  **恢复重试消费**；驳回则 agent 回落保守默认并上报。挑战绑剧集 id，故重试消费必须在同一剧集内兑现
  （重跑=新剧集，已批授权兑现不了）。
- **人类可读脱敏 payload**：审批卡须呈现脱敏后的**人类可读**写内容（如改价参数原文），供审批人拍板；
  只给哈希摘要 = 让人对不可读的哈希拍板，违背「拍板给人」。
- **TTL 按人审时延**：授权过期窗口按 IM 人审时延设定（机制默认值仅为占位口径，部署侧按实际人审节奏重估）。
- host 侧 **fail-closed**：审批配置非法即一律拒绝，绝不 fail-open 放行；一次性 token 授予用一次消费一次，无长期通行。
- 一卡两用：审批卡既是**安全闸门**（policy 写、运行时读、模型读不到绕不过——公理 A5），也是**飞轮采集点**
  （human=审批门与终审，「AI 提议 vs 人类终裁」的差异被采集成 case → 蒸馏成新判断）。

### B.5 freshness 约定
接口可声明数据新鲜度约定（如「每月8日财务关账后可用；8日前调用必须在报告中标注『未关账口径』」）。
新鲜度是给判断层与成文步的语义约束；「关账后/闭店后」的时刻语义需部署侧营业日历，参考实现按声明留档。

---

## 附录 C. 判断库包变体规范（规范语义定稿；实现推 Phase 1）

> **定位：** 判断库包是**仅装判断与出生证据、无自主运行面**的 `.osca` 变体——一架可被宿主包引用的
> 判断货架（如公文规范库、外呼合规库）。它承载「签约当天你的 agent 就带着整个行业的判断上岗」的公共层复用（§9.1）。
> **门控（措辞纪律）：** 本附录**定稿规范语义**；`cli/host` 实现（库包 lint 分支、`dependencies` 解析、
> 合并索引、签名再绑定、库包 replay 判据）**推 Phase 1**——签名再绑定的映射形状须拿**真实宿主包**校准
> 才设计得对，公共层价值 = 条目数 × **迁移率** × 调用频率，而迁移率**要测不要信**、须待第二家同垂直客户
> 出现方可实测（分类学不许跑在数据前面——不在数据到位前建行业中间层）。与 §9.2 overrides/dependencies「本版仅钉语法、
> 机制随后续落地」同口径。种子 pilot（`commons/gongwen-xingwen.osca`）撞出的四个规范缺口由本附录承接。

### C.1 库包 .osca 变体定义

osca.yaml 顶层加**可选**字段 `package_kind`（缺省 `agent`）：

```yaml
format: osca
format_version: "0.4"
package_id: commons-gongwen-xingwen
package_kind: library        # agent（缺省，可运行 agent 包）| library（判断货架，无运行面）
name: 公文行文规则判断库
entry: AGENT.md              # library 仍可带 AGENT.md 作人类可读说明（劝告层，非运行面）
```

库包**只含**：`osca.yaml`（`package_kind: library`）+ `judgments/` + `cases/`（+ 可选 `AGENT.md` 说明）。
**不含**运行面文件：无 `structure.yaml`（pipeline）、无 `aware/`（触发）、无 `policy.yaml`（笼子）、无
`connectors/`、无作为运行取数锚点的 `objects/`（判断签名引用的抽象占位对象见 C.3）。库包不被 Host 直接
`load` 运行（它没有 Aware 可布防、没有 pipeline 可跑）；它只经宿主包的 `dependencies`（C.4）被引用。

### C.2 库包免 pipeline（OSCA001 / OSCA040 库包分支约定，缺口③）

现行 lint 假定每包有 pipeline 与 Aware：种子库包今天只能**伪造**占位 `structure.yaml` 与 `aware/` 才过
`osca lint`（注释自招「占位骨架：判断库包本不应需要 pipeline」）。定稿约定 `package_kind: library` 时的
lint 分支（**实现推 Phase 1**）：

- **OSCA001（必备文件）库包分支**：`package_kind: library` 时必备文件 = `osca.yaml` + `judgments/` +
  `cases/`；豁免 `structure.yaml` / `aware/` / `policy.yaml`（无运行面则无笼子）。
- **OSCA040（pipeline / aware / policy 必填字段）库包分支**：库包无这些文件即无这些校验；judgment / case
  的字段校验（OSCA030–036 / OSCA060）**照常适用**——货架上的判断纪律一条不减。
- 库包**声明即豁免、不得夹带运行面**：`package_kind: library` 却带 `structure.yaml` / `aware/` 为 lint
  错误（防「半库半运行」的暧昧包）。

### C.3 抽象签名再绑定（缺口①）

库判断的 `signature.object` / `signature.aware` 引用的是**库本地的抽象占位 ID**（如种子 J-0004 绑库内
`OBJ-001` / `AW-001`）——它们意为「宿主里扮演某角色的那个 object / aware」，本身不指涉任何宿主实体。
宿主引用库时须在 `dependencies` 里声明**再绑定映射**，把抽象占位映射到宿主的具体 object / aware：

```yaml
dependencies:
  - package: commons-gongwen-xingwen
    version: <锁定版本>
    integrity: <完整性哈希>
    rebind:
      objects: {OBJ-001: OBJ-005}   # 库占位 → 宿主具体（宿主的「待签发公文」对象）
      aware:   {AW-001: AW-002}     # 库占位 → 宿主具体（宿主的「签发前」触发）
```

再绑定语义（**实现推 Phase 1**）：

- 检索时，库判断的抽象签名按 `rebind` 换成宿主具体锚点后，参与宿主的**签名硬过滤**（§11）；
- **保守默认**：某抽象占位在 `rebind` 中**未给映射**，则引用它的库判断在本宿主**不可检索**——不静默跨绑
  （错绑一条公文判断到错误对象，比漏检更危险）；
- **guard 变量兼容是宿主责任**：库判断 guard 引用的领域变量（如 `文种` / `主送机关数`）须能在宿主再绑定
  对象的 schema 里求值；不可求值即该判断对本情境**保守跳过并留痕**（不猜、不放行）。变量兼容的机器校验
  口径待 Phase 1 拿真实宿主定标。

### C.4 Manifest `dependencies`（锁版本 + 完整性哈希）

宿主包在 `osca.yaml` 声明对库包的依赖（C.3 示例）。语义（**实现推 Phase 1**）：

- **锁版本 + 完整性哈希**：`version` 钉死、`integrity` 为库包内容哈希；装载时校验，漂移即报错（守 §13
  「可打印可签名可审计」铁律）。
- **有效账本 = 本地层 + 钉死版本的公共层**：装载器解析依赖、验哈希、把库判断并入检索集（标 layer，C.5）；
  组合后账本仍须**可复现、可打印、可审计**——库包与其版本随宿主交付件一同可签名。
- 库包**只读引用**：宿主不改库判断（改库是库包自身的账本纪律事务）；宿主对库判断的本地压制走 C.6。

### C.5 合并索引（签名表加 layer 列）

宿主有库依赖时，签名索引（`indexes/`，机器生成、可重建）**加 `layer` 列**标每条判断的来源层
（本地 org / 依赖的 commons 库等）。检索器两段式（§11）跨「本地 + 引入库（经再绑定的签名）」硬过滤 +
桶内排序。**遥测边界（§9.2 钉死）：跨边界只回传条目级计数**（引用次数 / override 事件 / 回放红绿灯），
**永不回传情境与输出内容**。实现推 Phase 1。

### C.6 跨层遮蔽 `overrides`（与 supersedes 不混用）

宿主的私有判断可在本包内压掉一条公共库判断：

```yaml
# 宿主本地某判断的头部
overrides: commons-gongwen-xingwen/J-0004   # 跨层遮蔽：限定形式 <package_id>/<judgment_id>（§9.2）
```

语义（**实现推 Phase 1**）：被遮蔽者**不死、不迁移状态**，仅从**本宿主**的检索候选中**显式指针剔除**。
`override ≠ supersede`——跨层遮蔽 vs 同层版本继承，版税与信任统计口径不同，**不得混用同一字段**。**不做**
guard 语义重叠的自动冲突检测（只认显式指针，不猜）。override 事件按条目级计数回传（C.5 遥测口径）——
一条公共判断被多少宿主 override，就是它迁移率的实测负信号（迁移率要测不要信——数据说话，不自嗨）。

### C.7 无宿主 replay 判据（缺口④）

单判断回放（附录 A.6）的机器判据「输出从改前移向改后」需要 diff 物种 case（有 `agent_draft` +
`expert_final`）。库判断的出生证据是 `kind: 引用` / 合成反例 case（种子 J-0004 的 C-0004：`反例摘录` 为
合成示意、无真实 diff），**无真实 diff 可 A/B**。故库判断的 replay 判据**退化**（口径本版定稿，机器实现
推 Phase 1）：

- 有 diff 物种 evidence 的库判断：照附录 A.6 的移动判据 A/B（口径不变）；
- **仅 `引用` / 合成 evidence 的库判断**：退化为**「输出含裁决要点」**——注入臂产出须命中该判断
  `replay[].with_this_judgment` 断言的裁决要点（关键词/要点覆盖），`without` 臂不命中；这是**弱判据**，
  证「判断在场使输出含其裁决」，不证「输出向真实专家终稿移动」。**诚实标注**：弱判据 red/green 不等价于
  diff 判据，健康档案须标 evidence 物种，不混池汇总红灯率。真实宿主接入产生真 diff 后升级为强判据。

### C.8 门控与随行债

- **实现推 Phase 1**：库包 lint 分支（C.2）、`dependencies` 解析与完整性（C.4）、合并索引 layer 列
  （C.5）、签名再绑定（C.3）、overrides 剔除（C.6）、库包 replay 判据（C.7）——触发条件 = 自营运营主体
  全面上机 / 第二家同垂直客户出现（拿真实宿主设计再绑定、迁移率可实测）。
- **落地时诚实标注**：库包变体实现落地 = 机制通，**迁移率未实测 / 早于 Phase-1 数据**——CHANGELOG /
  README 照实标，不冒充「已由第二场景验证」（诚实标注是本规范的立身之本）。
- **种子随行债**：现行 `commons/gongwen-xingwen.osca` 的占位 `structure.yaml` / `aware/` 待 C.2 lint 分支
  落地后删除；在此之前保留（占位过 lint，注释已自招）。

---

## 附录 D. 变更记录 v0.3 → v0.4

**并入的参考实现互证增量（M2/M3，此前存于 v0.4-draft）：**
- §4：Object 第五型 `kind: objective`——修复 optimizer/settle 引用 objective 型而词表无名分的规范矛盾。
- §7：Aware 触发原语受限语法（时长语法 / schedule 结构化字段 / watch / event 字段集）、闸门编译期矛盾
  检查、组合语义定稿。**废止自由文本 schedule。**
- §9：判断分层命名空间与权属三字段（scope / provenance / classification）+ 洁净室规则（OSCA060 机器
  布防）+ 限定引用语法（`<package_id>/<judgment_id>`，§3 同步）。
- 附录 A：运行时求值参考语义（precondition / emit_when / kill_switch 可求值形式 + 健康档案契约）、
  剧集执行参考语义（performer 受限集 / 预算数量记法 / 剧集停三终态 / 归属纪律）、settle 受限形式、回放机器判据。

**M6 新增：**
- §10：case kind 词表收编 `引用`（公共标准编纂类判断的天然出生证据形态）。
- 附录 B：企业系统对接约定（Manifest/Binding/Impl 三层职责 + 执行器分派 + 真实执行器契约 +
  read-only enforcement + secret 解析 + 写路径挂起-等批-恢复消费语义）。
- §1：osca.yaml 可选 `package_kind`（`agent` 缺省 | `library`）。
- 附录 C：判断库包变体规范**定稿**（库包 `.osca` 变体 `package_kind: library` / 库包免 pipeline
  OSCA001·040 分支约定 / 抽象签名再绑定 `rebind` / `dependencies` 锁版本+完整性哈希 / 合并索引 layer 列 /
  `overrides` 跨层遮蔽 / 无宿主 replay 退化判据）——**规范语义定稿，cli/host 实现推 Phase 1**（签名再绑定
  须真实宿主校准、迁移率待第二家同垂直客户实测——分类学不跑在数据前）。

**兼容性：** §0–§14 沿用 v0.3 编号与既有引用；`format_version` 升 `"0.4"`。存量包过渡期：分层三字段
缺失为 lint 警告（不硬拦），其余增量向后兼容。

### 引用交叉映射（草案编号 → 定稿位置）

v0.4-draft 曾用自己的增量编号（§1–§9）；参考实现（`host/src/*.py`、`host/README.md`、`docs/OSCA-LINT-RULES.md`、
host 测试）现引用的「SPEC v0.4 §3/§4/§5/§6」等指的是**草案原编号**。定稿全文按 v0.3 §0–§14 + 附录
重排，对应关系如下表；**引用文本的机械同步随 v0.4-draft 退休（tag v1.0 里程碑）一并完成**——在此之前
草案文件保留，旧引用仍可解析。

| v0.4-draft 原编号（现被引用） | 定稿位置 |
|---|---|
| §1 受限触发语法 | §7 + §7.1–§7.4 |
| §2 闸门编译期矛盾检查 | §7.5 |
| §3 组合语义 | §7.5 |
| §4 运行时求值（precondition / emit_when / kill_switch + 健康档案契约） | 附录 A.1–A.3 |
| §5 剧集执行（performer 受限集 / 预算记法 / 剧集停三终态 / 归属纪律） | 附录 A.4 |
| §6 settle 受限形式 | 附录 A.5 |
| §7 回放机器判据 | 附录 A.6 |
| §8 Object 第五型 kind: objective | §4 |
| §9 分层命名空间 + 权属三字段 + 限定引用 | §9.1–§9.2 |
