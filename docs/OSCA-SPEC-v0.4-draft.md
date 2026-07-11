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

---

## 变更记录

- **v0.4-draft**（2026-07-11）：§5 触发原语受限语法（时长 / schedule 结构化字段 / watch / event 字段集）；
  闸门编译期矛盾检查清单；组合语义定稿。废止自由文本 schedule。
  追加 §4 运行时求值参考语义（precondition / emit_when / kill_switch 的可求值受限形式与保守默认）。
