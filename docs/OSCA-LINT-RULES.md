# osca lint 规则清单 v0.2

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
| OSCA030 | 错误 | 每条判断 ≥1 条出生证据，且引用的 case 存在 | 纪律 2 |
| OSCA031 | 错误 | supersedes 双向一致：新判断指向的旧判断必须 status=superseded；superseded 的判断必须被指向 | 纪律 1 |
| OSCA032 | 错误 | trust 由计数驱动（active 判断：confirmed≥5 且 overruled=0 ⇔ high）；superseded 冻结不查 | 纪律 4 |
| OSCA033 | 错误 | status ∈ {active, superseded, review} | SPEC §9 |
| OSCA034 | 错误 | 每条判断自带 replay 回放断言（＝单元测试） | 纪律 4 |
| OSCA035 | 警告 | 判断应声明 expiry 失效条件（防腐烂） | SPEC §9 |
| OSCA036 | 错误 | case 的 input 必存「当时生效判断集」（无此字段回放不可信） | SPEC §10 |

## 必填字段

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA040 | 错误 | 各类文件必填字段：object（name/kind/version/definition，负样例必带 why）；connector（name/kind/interfaces/permissions.write）；aware（name/enabled 布尔/then/budget/≥1 触发原语）；judgment（status/signature 三件/body/meta 计数）；case（captured_at/capture_source/input）；policy（policy_version） | SPEC §4–§10 |

## 安全铁律

| 规则 | 级别 | 内容 | 依据 |
|---|---|---|---|
| OSCA050 | 错误 | 扫描包内**全部文本文件**（含 .md/.sql）：连接串（jdbc/mysql/ssh/…）与密钥特征（AccessKey/token/私钥）绝对禁止；`http://` 明文链接一律禁止；`https://` 链接仅公开文档白名单域放行（creativecommons.org、apache.org、opensource.org、oscaware.com、github.com） | SPEC §13 |

## v0.2 已知边界（下一批处理）

- 引用「只许用 ID、禁止用文件名/中文名」的反向检查（发现疑似中文名引用）未做；AGENT.md 正文中的 ID 引用不校验。
- §5 衔接约定的「进判断签名或对外交付必须 {ref}」方向性检查未做（当前仅裸字符串放行）。
- kind 特定必填字段（metric 的 unit/direction/source、composite 的 formula 等）未逐一校验。
- guard 表达式的语法校验（可求值性）留给运行框架 M2。
- cases 大报文外置逻辑指针（{content_hash, store, key}）的形态校验未做。

## 变更记录

- **v0.2**（2026-07-11）：OSCA050 按 SPEC v0.3 §13 精确化——扫描范围扩到全部文本文件；区分连接串（绝对禁止）与文档链接（https 白名单放行、http 一律禁止）。依据栏改指 SPEC v0.3 章节。
- **v0.1**（2026-07-11）：首版 21 条规则。
