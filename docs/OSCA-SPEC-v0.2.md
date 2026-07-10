> 本规范文本以 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh) 开放：可自由转载与改编，须署名并注明出处。
> 本仓库中的代码与样例以 Apache-2.0 授权。/ Specification text: CC BY 4.0. Code & examples: Apache-2.0.

# OSCA 包格式规范 v0.2（草案）

> 一个 agent = 一个 `.osca` 文件夹 = 一个 git 仓库。
> 全部纯文本（Markdown + YAML）。包是可交付、可审计、可打印的资产。
> 铁律：包内不得出现任何密钥、连接串、真实 URL。

---

## 0. 目录树（v0.2）

```
<agent名>.osca/
├── AGENT.md                      # 入口:身份/目标/边界(劝告层,模型读)
├── policy.yaml                   # 笼子(强制层,运行时读,模型不读)
├── structure.yaml                # 组合骨架(薄)
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
│   └── C-0091.yaml               # 元数据+指针;大报文外置OSS(content_hash+uri)
├── indexes/                      # 机器生成,commit钩子重建,人不手写
│   ├── judgments.index.yaml      # 签名表(硬过滤用)
│   └── judgments.emb.parquet     # 向量(语义排序用)
└── bindings.example.yaml         # 部署绑定模板;真实 binding 在部署环境,不进包
```

---

## 1. 双平面架构（运行时契约）

**控制平面（Host，确定性，常驻，无 LLM）**
装载包时：
1. 解析 `aware/` → 把 `trigger` 段编译为 watcher（定时器/事件订阅/轮询器）注册进调度器
2. 解析 `policy.yaml` → 装载拦截规则
3. 校验 `connectors/` manifest 与部署环境 binding 的匹配

**认知平面（Episode，LLM，按需唤醒，短命）**
watcher 命中 → 运行时组装一次性上下文：
```
AGENT.md + structure.yaml
+ 命中 Aware 的 discretion 段
+ 该 Aware 引用的 objects
+ 判断检索结果(见 §7):top 3–7 条判断,各带 1 个代表 case
```
剧集跑完 pipeline 即终止。**不存在持续运行的模型。**

**三级停**
- 剧集停：pipeline 完成 / 触发 Aware 的 `budget` 硬顶（步数/token/时长）
- 触发器停：单个 Aware 文件 `enabled: false`
- 包停：注销全部 watcher

---

## 2. 命名与 ID 规则

- 文件名格式：`<ID>-<中文名>.yaml`，如 `OBJ-002-费用异动报警.yaml`
  （judgments/cases 数量大，允许省略中文名，仅 `J-0417.yaml`）
- ID 格式：类型前缀 + 包内自增。`OBJ- / STR- / CON- / AW- / J- / C-`
- ID 一经分配**永不修改、永不复用**；中文名可随时改
- **跨文件引用只允许用 ID**，禁止用文件名或中文名
- 跨包引用预留语法：`<包名>::OBJ-002`（行业判断底座阶段启用）

---

## 3. Object 规范（四型）

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

**kind: artifact** —— 产出物
```yaml
schema: {...}
medium: document | message | table | payload   # 文档还是对话气泡在此声明
delivery: 飞书文档 | 对话回复 | 邮件 | API回传
quality_bar: |            # 产得好的标准,专家验收的依据
```

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

## 4. Connector 规范（三层分离）

**层1 Manifest（本文件，进包）** —— 类型与纪律
```yaml
connector_id: CON-xxx
name: <中文名>
kind: mcp | openapi | sql_readonly | code
rationale: |              # 为什么确定性执行
interfaces:
  - name: <接口中文名>
    params: {参数: {type, ref?, required}}
    returns: <类型或 OBJ 引用>
    freshness: <数据新鲜度约定,如"每月8日关账后可用">
    born_reason: <可选:该接口因哪条判断而生>
permissions:
  write: forbidden | allowed_with_approval
  scope: <数据边界描述>
binding_ref: FINANCE_DB   # 指向部署环境 binding 的名字,不是值
```

**层2 Binding（部署环境，不进包）** —— `bindings.yaml` 由运维在环境注入：
```yaml
FINANCE_DB:
  endpoint: <真实URL/连接串>
  secret_ref: FINANCE_DB_RO_KEY    # 密钥名,值在 secret manager
```
包内仅保留 `bindings.example.yaml`（同结构、占位值）作为模板。

**层3 Impl** —— 优先级：现成 MCP server > OpenAPI 描述 > 包内代码(`impl: sql/xxx.sql`)。
自研代码是最后手段。

---

## 5. Aware 规范（三型）

公共字段：
```yaml
aware_id: AW-xxx
name: <中文名>
kind: schedule | event | watch
enabled: true
then: <STR-xxx 或其某 step>       # 醒来干什么
budget: {max_steps: 40, max_minutes: 15}   # 剧集硬顶
debounce: <冷却窗口,如 24h>       # 同情境窗口内只醒一次
discretion: |             # 自然语言余量,唤醒后注入剧集上下文
```

**kind: schedule**
```yaml
trigger: {schedule: "每月9日 09:00", precondition: <可求值谓词>, on_fail: <重试策略>}
```

**kind: event**
```yaml
trigger:
  source: webhook | 飞书消息 | 人工触发
  filter: <可求值谓词,如 "payload.单位 in 重点单位清单">
```

**kind: watch** —— 轮询 + 差分谓词，合成事件
```yaml
trigger:
  uses: CON-xxx.<接口名>          # 观察谁
  every: 4h                       # 轮询周期
  state_key: <缓存哪个字段作为状态>
  emit_when: "new.状态 != old.状态 && new.状态 == '停机'"   # 差分谓词
```
运行时维护 last_state；`emit_when` 为真 → 合成事件 → 走 event 通路。

---

## 6. Policy 规范（笼子，运行时强制，模型不读）

```yaml
policy_version: 1
permissions:              # 按 pipeline 步骤的工具白名单
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

## 7. 判断检索契约（两段式）

1. **类型硬过滤**（运行时，确定性）：用当前情境(object, aware, guard 变量)
   扫 `indexes/judgments.index.yaml` 签名表 → 候选集（通常 <30 条）
2. **语义排序**（embedding）：仅在候选桶内 → top 3–7 注入，各带 1 个代表 case
3. 永不全库向量检索；永不整库注入

索引由 commit 钩子重建，人不手写。judgments >200 条后按 object 分目录。

---

## 8. Cases 存储契约（冷证据）

- cases 只经判断的 `evidence` 引用到达；不进语义索引，不被扫描
- 大报文外置 OSS：YAML 内存 `{content_hash, uri}`，内容寻址防篡改
- 必存字段：`当时生效判断集`（无此字段回放不可信）
- 蒸馏后未产出判断的 cases 按季度归档出仓

---

## 9. 账本纪律（不变量）

1. judgments 只追加；推翻用 `supersedes`，被取代文件改 `status: superseded`，不删除
2. 每条判断必须有 ≥1 条 evidence（无出生证据的判断不准入账）
3. 判断只能从专家真实编辑行为蒸馏而来；禁止凭空手写"我觉得应该"型判断
   （专家主动口述的规则也要落一条 case: kind=口述,作为证据）
4. trust 升降由 confirmed/overruled 计数自动驱动，人不手改
5. structure 里不许写 if/else；想写的那一刻,它就是一条该进 judgments 的判断
```
