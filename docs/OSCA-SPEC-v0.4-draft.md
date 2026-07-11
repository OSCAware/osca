> 本规范文本以 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh) 开放：可自由转载与改编，须署名并注明出处。

# OSCA 包格式规范 v0.4（草案 · 增量）

> 状态：**draft**。基于 v0.3（tag `spec-v0.3`），本文只记录变更；未提及的章节沿用 v0.3 全文。
> 变更由运行框架 Host（M2）触发表/闸门实现互证喂养——机器布防不了的语法，就不配进规范。
> 定稿时并入 v0.3 全文成 v0.4 正式版。

---

## 1. §5 Aware 触发原语：受限语法（自由文本废止）

v0.3 样例中 `schedule: "每月9日 09:00"` 是自由文本，机器不可解析、跨实现不可移植，**废止**。
v0.4 起触发原语全部采用受限语法；不在字段集内的键一律报错（lint 规则 OSCA041，
参考实现 `osca_cli.triggers` 同时供 lint 与 Host 编译期共用——语法只定义一次）。

### 1.1 时长语法（duration）

```
<正整数><单位>    单位 ∈ s | m | h | d（秒/分/时/天）
```

例：`24h`、`72h`、`30m`。`0` 值非法；不接受小数、复合写法（`1h30m`）与其他单位。
适用字段：`watch.every`、`gate.debounce`。

### 1.2 schedule（定时器）

```yaml
- id: T1
  kind: schedule
  schedule: {every: month, day: 9, time: "09:00"}   # 结构化字段
  note: 财务关账次日                                  # 自由文本注释，机器不读
```

| 字段 | 必填 | 约束 |
|---|---|---|
| `every` | 是 | `day` \| `week` \| `month` |
| `day` | every=month/week 时必填 | month：整数 1..31；week：`mon`..`sun`；every=day 时**不得给** |
| `time` | 是 | 24 小时制 `"HH:MM"` |
| `tz` | 否 | IANA 时区名（如 `Asia/Shanghai`）；缺省取 Host 部署环境时区 |

语义定稿：`day` 超出当月天数时**取当月最后一天**（与主流调度器一致，如 `day: 31` 在 2 月触发于月末）。

### 1.3 watch（轮询器）

| 字段 | 必填 | 约束 |
|---|---|---|
| `uses` | 是 | Connector 接口引用（`CON-xxx.接口名`） |
| `every` | 是 | 时长语法（§1.1） |
| `state_key` | 否 | 状态比对键 |
| `emit_when` | 否 | 发射条件表达式（`old.*` / `new.*`）；求值语义由运行框架定义 |

### 1.4 event（事件）

| 字段 | 必填 | 约束 |
|---|---|---|
| `source` | 是 | 触发来源说明（自由文本）；运行时由操作者通道人工发射 |

### 1.5 通用

每条触发原语必有 `id`（包内 Aware 级唯一，如 `T1`）与 `kind`；全局引用形如 `AW-001/T1`。
各 kind 的允许字段集之外出现任何键即报错（受限语法的含义：宁可拒绝，不可猜测）。

## 2. §5 闸门：编译期矛盾检查（装载时执行）

gate 允许字段集：`combine` / `precondition` / `debounce` / `on_fail`。

- `combine` ∈ `any` | `all` | `sequence`，缺省 `any`；
  **`all` / `sequence` 要求 ≥2 条触发原语**，否则为编译期矛盾，装载拒绝。
- `debounce` 必须是合法时长语法。
- `precondition` / `on_fail` 为声明性文本，求值与执行语义由运行框架定义。

## 3. 组合语义（运行框架约定，入规范以保可移植）

- `any`：任一触发命中 → 过闸门。
- `all`：自上次唤醒起，全部触发原语各至少命中一次 → 过闸门并重置。
- `sequence`：按声明顺序依次命中 → 过闸门并重置；乱序命中即重置（若乱序命中的恰是首位，视为新序列开始）。
- `debounce`：唤醒后的抑制窗口，窗口内再次过闸门只计数不唤醒。
- `enabled: false` 的 Aware 不布防触发原语（三级停之「触发器停」）。

## 4. 运行时求值参考语义（precondition / emit_when / kill_switch）

v0.3 中这三处均为声明性文本。参考实现给出**可求值受限形式**；不合形式的声明
不报错、不生效——保守默认（precondition 放行、emit_when 不发射、kill_switch 不触发）并留痕。

- **precondition**（闸门前置条件）：`CON-xxx.接口名(参数) 返回非空`。
  经 Connector 代理真调用：返回空或取数失败 → 拦截唤醒并复述 `on_fail` 声明
  （顺延重试的执行属对账/重试机制，后续版本落地）。
- **emit_when**（watch 发射条件）：以 `&&` 连接的比较子句，字段取自 `old.*` / `new.*`，
  比较符 `==` / `!=`，字面量 true/false/null、数字，其余按字符串比对。
  例：`old.已关账 == false && new.已关账 == true`。**无 emit_when 时按状态变化发射**；
  首轮建立基线不发射。
- **kill_switch**（policy.yaml）：可求值形式 `overruled/confirmed > X`。
  计数口径：**现役（active）判断合计**——被取代判断的计数随取代冻结成历史，
  推翻→重审→蒸馏新判断是账本自愈，健康度看现役账本（时间窗随蒸馏管道的时间账收紧）。
- **watch 去重域**：schedule 纯时间可跨包共享；watch 数据绑定在包上，
  去重共享只在包内（不同包的同名 Connector 可能指向不同系统）。

## 5. 剧集执行参考语义（performer 受限集 / 预算 / 剧集停）

structure.pipeline 的 `performer` ∈ `agent` | `connector` | `optimizer` | `human` | `runtime`
（含组合写法如 `agent + judgments`、`human(王工)`——按关键词识别）；受限集之外直接拒绝，不猜。

- **connector**：经 Connector 代理按名调用。`uses` 写接口引用（`CON-xxx.接口名`）或
  裸 Connector ID（展开为 manifest 声明的全部接口）；任一接口取数失败即剧集失败——
  没有取数支撑的草稿是编造。
- **agent**：LLM 依一次性上下文出草稿；产出注入剧集台账前过 Policy 脱敏。
  LLM 通道由部署环境变量配置（`OSCA_LLM_URL` / `OSCA_LLM_MODEL` / `OSCA_LLM_API_KEY`，
  OpenAI-compatible 线协议，温度恒 0），不锁定厂商、配置永不进包——与 binding 同一纪律。
- **optimizer**：确定性寻优，LLM 不参与数值搜索（公理 A6）。初版贪心的可求值受限输入：
  候选为 `list[dict]`、每项含数值 `value` 字段，按 objective 的 `optimize` 方向排序取最优；
  缺数值即拒。数值约束求解与 bandit 属部署侧演进，约束声明留档给人审。
- **human**：飞轮采集点，机器侧流水线到此为止（其后步骤待人工环节回执）。
- **runtime**：对账步，移交对账器（§6），不在剧集内执行。

**预算数量记法**：`<正整数>[k]`（`200k` = 200000）；不可解析的预算记警告、硬顶不生效。
**剧集停（三级停之一）三种终态**：`completed`（pipeline 走完 / 到达 human 采集点）、
`stopped`（budget 硬顶——aware.budget 与 policy per_episode 双重；tokens 为**止损顶**：
用量由网关调用后回报，超顶那次调用已发生，就地停）、`failed`（取数失败 / LLM 不可用 /
声明不合受限形式）。三种终态全部进剧集台账留痕。

## 6. 对账 settle 受限形式（objective 型 → outcome case）

objective 型对象的 `settle` 声明可求值受限形式：

```yaml
settle: {uses: CON-xxx.接口名, when: 闭店后}   # when 为自由文本注释，机器不读
```

剧集完成后对账器自动执行：decision（剧集最后一个产出）vs reality（经代理取数、已脱敏），
落一条 `kind: outcome` 的 case（编号顺延现有最大号、`distillation.status: pending`），
不消耗剧集——现实是第二位专家（公理 A2）。自由文本 settle 不报错、不执行——保守默认留痕。
「闭店后/收盘后」的时刻语义需要部署侧营业日历，参考实现在剧集完成后立即对账并把 when 留档。

## 7. 回放机器判据（`osca replay`，单条体检）

单判断 A/B：同一 case 情境跑两臂，唯一差异 = 本判断在不在场
（case 的「当时生效判断集」两臂共享）。机器判据（确定性、模型无关）：

```
score(产出) = 相似度(产出, expert_final) − 相似度(产出, agent_draft)
绿灯 ⇔ score(注入) > score(不注入)
```

即「输出从改前移向改后」的字面落地：既奖励靠近专家改后，也奖励离开机器改前
（删除类判断靠后一项仍可判）。断言文本（with/without_this_judgment）是给人读的
期望声明，机器不解析；判断 ID 是否被注入臂引用作为提示信号报告，不作硬判据。
只有 diff 物种（有 `agent_draft` + `expert_final`）的 case 可 A/B 回放。

---

## 变更记录

- **v0.4-draft**（2026-07-11）：§5 触发原语受限语法（时长 / schedule 结构化字段 / watch / event 字段集）；
  闸门编译期矛盾检查清单；组合语义定稿。废止自由文本 schedule。
  追加 §4 运行时求值参考语义（precondition / emit_when / kill_switch 的可求值受限形式与保守默认）。
- **v0.4-draft 增补**（2026-07-11，M2-W5）：§5 剧集执行参考语义（performer 受限集 /
  预算数量记法 / 剧集停三终态）；§6 settle 受限形式（objective → outcome case）；
  §7 回放机器判据（A/B 移动判据，断言文本机器不解析）。
