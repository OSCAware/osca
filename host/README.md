# osca-host（开发中，M2）

OSCA 运行框架 Host 参考实现——**确定性常驻进程，无 LLM**。
把 `.osca` 包从静态资产变成能自己醒来干活的 agent。

## 当前进度（诚实标注）

| 组件（架构 §4） | 状态 |
|---|---|
| 1. Loader + Linter | ✅ 复用 cli 装载核心（完整性 / lint / binding 比对 / 索引重建）+ 运行时结构解析 |
| 2. 触发表 | ✅ 定时器 / 轮询器编译布防，哈希去重共享（引用计数；watch 按包隔离）；轮询经 Connector 代理取数，emit_when 真比对（SPEC v0.4 §4），首轮基线、无 emit_when 按状态变化发射；event 由控制通道人工发射 |
| 3. 闸门 gate | ✅ combine（any/all/sequence）+ debounce + enabled + **precondition 真求值**（经代理取数，返回空/取数失败即拦截并复述 on_fail；不可求值保守放行留痕）。编译期矛盾检查在 lint（OSCA041）与装载时共用 `osca_cli.triggers` |
| 4. 剧集装配器 | ✅ 唤醒 → 一次性上下文（AGENT.md + structure + discretion + 引用 objects + 判断 top3–7 各带代表 case）进剧集台账；检索 = 签名表硬过滤 + trust/confirmed 排序（语义排序归 M3 索引器）；policy.yaml 刻意不入上下文（公理 A5）。执行属 W5 |
| 5. Policy 拦截器 | ✅ 按步骤工具白名单（默认拒绝）、审批门（一次性授予，M4 换审批卡）、预算硬顶（per-episode tool_calls；tokens 归 W5）、egress 默认全禁、数据脱敏（身份证号/手机号）、kill switch（现役账本 overruled/confirmed 比率，公理 A10）——全程审计留痕 |
| 6. Connector 代理 | ✅ manifest 契约校验（接口漂移当场爆炸）、binding/secret 解析（binding 永不进包，缺失即报错）、调用回执 + 注入前脱敏；内置 mock 执行器（`mock://` 固件目录），真实 sql/openapi 执行器属部署侧适配（M6） |
| 7. 对账器 settle | ⬜ W5 |

已可演示：Host 起停、包装载 / 注销、定时布防（status 可见 next_fire）、人工发射 event、
**precondition 经代理真求值**（有 binding 放行唤醒 / 无 binding 保守拦截）、审批门授予、
三级停之**包停**（unload）与**触发器停**（disable 单 Aware）；kill switch 触发时装载可、唤醒与调用全拒。

## 用法

```bash
cd host && uv sync

# 前台起 Host，启动即装载样例包
uv run osca-host run --load ../examples/oper-diagnosis.osca

# 另开终端：注册表快照 / 装载 / 包停 / 关停
uv run osca-host status
uv run osca-host load ../examples/oper-diagnosis.osca
uv run osca-host unload demo-group-oper-diagnosis
uv run osca-host stop

# 三级停之「触发器停」＋ 操作者人工触发（对应样例 T3）
uv run osca-host disable demo-group-oper-diagnosis AW-001
uv run osca-host enable demo-group-oper-diagnosis AW-001
uv run osca-host fire demo-group-oper-diagnosis AW-001/T3

# 剧集台账：唤醒装配的一次性上下文
uv run osca-host episodes
uv run osca-host episode EP-0001

# 审批门：对 policy.yaml approvals 里的动作授予一次性放行
uv run osca-host approve demo-group-oper-diagnosis 终稿发送管理层
```

## 部署 binding 与 mock 执行器

binding 永不进包——部署环境用 `--bindings` 注入（对照包内 `bindings.example.yaml` 模板）。
参考实现内置 mock 执行器做测试与全链路演练：endpoint 写 `mock://<目录>`，
目录里放 `<接口名>.yaml` 固件；真实 sql_readonly / openapi 执行器属部署侧适配（M6 对接约定）。

```yaml
# /etc/osca/bindings.yaml（示例）
FINANCE_DB:
  endpoint: mock:///opt/osca/fixtures    # 真实环境换成只读连接串
  secret_ref: FINANCE_DB_RO_KEY          # 密钥名；值在部署环境 secret manager
```

控制通道是本机 unix socket（默认 `~/.osca/host.sock`，`--socket` 可改），
JSON-lines 协议——本地管控，不是对外 API。

## 开发

```bash
cd host
uv sync
uv run pytest        # 测试（含控制通道端到端）
uv run ruff check .  # 代码检查
```
