# osca lint 规则清单 v0.3

> 本文档以 CC BY 4.0 开放。lint 是账本纪律的机器化：每条规则注明依据（SPEC 指 v0.3 章节）。
> 错误（ERROR）挡住通过；警告（WARN）提示但不挡。实现：`cli/src/osca_cli/rules.py`，与本清单一一对应。

## 包结构

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA001 | 错误 | 必备文件存在：osca.yaml、AGENT.md、policy.yaml、structure.yaml | SPEC §0 |
| OSCA002 | 警告 | 标准目录布局：objects/ connectors/ aware/ judgments/ cases/ | SPEC §0 |
| OSCA003 | 错误 | 所有 YAML 可解析 | — |
| OSCA004 | 错误 | osca.yaml 身份证完整（format=osca、format_version、package_id、name）；entry 文件存在 | SPEC §1 |

## 命名与 ID

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA010 | 错误 | 文件名 `<ID>[-<中文名>].yaml`，ID 前缀与所在目录匹配 | SPEC §3 |
| OSCA011 | 错误 | 文件内 ID 字段存在且与文件名 ID 一致 | SPEC §3 |
| OSCA012 | 错误 | ID 包内唯一，永不复用 | SPEC §3 |

## 引用与一致性

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA020 | 错误 | 正文出现的每个 ID 形状 token（OBJ/STR/CON/AW/J/C-编号）必须在包内可解析 | SPEC §3 |
| OSCA021 | 错误 | connector 必有 binding_ref，且 bindings.example.yaml 有同名键 | SPEC §6 |
| OSCA022 | 警告 | osca.yaml requires.bindings 与各 connector 的 binding_ref 集合一致 | SPEC §1 |
| OSCA023 | 警告 | policy 权限表的 step 名必须存在于 structure pipeline | SPEC §8 |
| OSCA024 | 警告 | connector 接口声明的 impl 路径真实存在 | SPEC §6 层3 |

## 账本纪律

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA030 | 错误 | 每条判断 ≥1 条出生证据，且必须是包内存在的 case（C-xxxx，别的 ID 类型不算证据） | 纪律 2 |
| OSCA031 | 错误 | supersedes 双向一致、无环、无分叉：新判断指向的旧判断必须 status=superseded；superseded 的判断必须被指向；自指与环（互相取代）报错；同一旧判断被多条判断取代（分叉）报错 | 纪律 1 |
| OSCA032 | 错误 | trust 由计数驱动（active 判断：confirmed≥5 且 overruled=0 ⇔ high）；superseded 冻结不查 | 纪律 4 |
| OSCA033 | 错误 | status ∈ {active, superseded, review} | SPEC §9 |
| OSCA034 | 错误 | 每条判断自带 replay 回放断言（＝单元测试） | 纪律 4 |
| OSCA035 | 警告 | 判断应声明 expiry 失效条件（防腐烂） | SPEC §9 |
| OSCA036 | 错误 | case 的 input 必存「当时生效判断集」（无此字段回放不可信） | SPEC §10 |

## 必填字段

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA040 | 错误 | 各类文件必填字段：object（name/kind/version/definition，负样例必带 why；kind=objective 必填 optimize: maximize\|minimize）；connector（name/kind/interfaces/permissions.write）；aware（name/enabled 布尔/then/budget/≥1 触发原语）；judgment（status/signature 三件/body/meta 计数）；case（captured_at/capture_source/input）；policy（policy_version）。**嵌套形状约束**：运行时按键取值的字段（examples/permissions/budget/gate/triggers/meta/replay/policy 各段/pipeline 及其步骤项）必须是声明的 mapping/list 形状；**叶子与元素**：policy 运行时消费的叶子字段（data.redact/egress.allow_domains/permissions[].allow 为字符串列表，permissions[]/approvals[]/kill_switch[] 元素为 mapping）、序列元素（triggers[]/replay[]/negative[]/pipeline[] 为 mapping）、计数字段排除 bool——lint 是总函数，任意 YAML 形状只报错、不崩溃（规则自带类型防御 + run_all 兜底），形状错误不得静默改变笼子语义 | SPEC §4–§10 + v0.4 §8 |

## 触发原语与闸门

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA041 | 错误 | 触发原语与闸门的受限语法：schedule 结构化字段 {every, day, time[, tz]}（自由文本废止）；watch 必有 uses + every（时长语法 `<整数><s\|m\|h\|d>`）；event 必有 source；各 kind 允许字段集外的键报错；gate 仅 combine/precondition/debounce/on_fail，combine=all/sequence 要求 ≥2 条触发原语（编译期矛盾）。解析器 `osca_cli.triggers` 与运行框架 Host 共用——lint 过 ＝ Host 编译期能布防 | SPEC v0.4 草案 §5 |

## 安全铁律

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA050 | 错误 | 扫描包内**全部文本文件**（含 .md/.sql）：连接串（jdbc/mysql/ssh/…）与密钥特征（AccessKey/token/私钥）绝对禁止；`http://` 明文链接一律禁止；`https://` 链接仅公开文档白名单域放行（creativecommons.org、apache.org、opensource.org、oscaware.com、github.com） | SPEC §13 |

## 分层与权属

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA060 | 错误/警告 | 判断分层权属三字段（scope/provenance/classification）：三字段缺失 = **警告**（v0.4 起新生判断必填，存量包过渡期不硬拦）；枚举非法、provenance 形状缺陷（缺 origin/source/rights）= 错误；**洁净室** `scope: commons` 且 `origin: client-derived` = 错误（进 commons 只有 own-ops / public-standard / licensed 三入口）；`scope: commons` 且 `classification != public` = 错误（commons 定义=可迁移且无密级） | SPEC v0.4 §9 |
| OSCA061 | 错误 | osca.yaml 包级分层默认段 `layering: {scope, provenance, classification}`（蒸馏 confirm 出生判断按此填三字段）：可选、可部分；present 即按 OSCA060 同一判据校验枚举/形状/洁净室（错的默认污染整包新生判断，在源头 osca.yaml 拦比逐条 judgment 早）；缺段合法 | SPEC v0.4 §1/§9 |

## v0.2 已知边界（下一批处理）

- 引用「只许用 ID、禁止用文件名/中文名」的反向检查（发现疑似中文名引用）未做；AGENT.md 正文中的 ID 引用不校验。
- §5 衔接约定的「进判断签名或对外交付必须 {ref}」方向性检查未做（当前仅裸字符串放行）。
- kind 特定必填字段（metric 的 unit/direction/source、composite 的 formula 等）未逐一校验。
- guard 表达式的语法校验（可求值性）留给运行框架 M2。
- cases 大报文外置逻辑指针（{content_hash, store, key}）的形态校验未做。
- **判断库包变体分支（SPEC v0.4 附录 C，Phase 1）**：`package_kind: library` 时 OSCA001（必备文件）豁免
  structure/aware/policy、OSCA040 跳过 pipeline/aware/policy 校验（judgment/case 纪律照常）；`dependencies`
  锁版本+完整性哈希与 `rebind` 再绑定校验。规范语义已定稿，lint 实现推 Phase 1。

## 变更记录

- **v0.4（M6-W2）**（2026-07-18）：新增 OSCA061——osca.yaml 包级分层默认段校验（SPEC v0.4 §1/§9，与 OSCA060 共用枚举/形状/洁净室判据），共 24 条规则。
- **v0.4-draft**（2026-07-18）：新增 OSCA060——判断分层权属三字段 + 洁净室机器布防（SPEC v0.4 §9），共 23 条规则。
- **v0.3**（2026-07-11）：新增 OSCA041——触发原语与闸门受限语法（SPEC v0.4 草案 §5），共 22 条规则。
- **v0.2**（2026-07-11）：OSCA050 按 SPEC v0.3 §13 精确化——扫描范围扩到全部文本文件；区分连接串（绝对禁止）与文档链接（https 白名单放行、http 一律禁止）。依据栏改指 SPEC v0.3 章节。
- **v0.1**（2026-07-11）：首版 21 条规则。
