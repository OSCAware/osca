# osca-host（M2 七组件齐）

OSCA 运行框架 Host 参考实现——**控制平面确定性常驻，本体无 LLM**；
LLM 只活在短命的剧集（认知平面）里。把 `.osca` 包从静态资产变成能自己醒来干活的 agent。

## 当前进度（诚实标注）

| 组件（架构 §4） | 状态 |
|---|---|
| 1. Loader + Linter | ✅ 复用 cli 装载核心（完整性 / lint / binding 比对 / 索引重建）+ 运行时结构解析 |
| 2. 触发表 | ✅ 定时器 / 轮询器编译布防，哈希去重共享（引用计数；watch 按包隔离）；轮询经 Connector 代理取数，emit_when 真比对（SPEC v0.4 §4），首轮基线、无 emit_when 按状态变化发射；event 由控制通道人工发射 |
| 3. 闸门 gate | ✅ combine（any/all/sequence）+ debounce + enabled + **precondition 真求值**（经代理取数，返回空/取数失败即拦截并复述 on_fail；不可求值保守放行留痕）。编译期矛盾检查在 lint（OSCA041）与装载时共用 `osca_cli.triggers` |
| 4. 剧集装配器 + 执行器 | ✅ 唤醒 → 一次性上下文（AGENT.md + structure + discretion + 引用 objects + 判断 top3–7 各带代表 case）进剧集台账；检索 = 签名表硬过滤 + trust/confirmed 排序（语义排序归 M3 索引器）；policy.yaml 刻意不入上下文（公理 A5）。装配后即交剧集执行器（认知平面，独立线程）沿 pipeline 出草稿：performer 受限集 connector / agent / optimizer（初版贪心）/ human（飞轮采集点，机器到此为止）/ runtime（移交对账器）——SPEC v0.4 §5 |
| 5. Policy 拦截器 | ✅ 按步骤工具白名单（默认拒绝）、审批门（一次性授予，M4 换审批卡）、预算硬顶（per-episode tool_calls + **tokens 止损顶**，`200k` 数量记法）、egress 默认全禁、数据脱敏（身份证号/手机号，agent 产出同样过脱敏）、kill switch（公理 A10，两种可求值形式：现役账本 overruled/confirmed 比率；回放红灯率 > X%——数据源为回放器健康档案 `indexes/replay-health.json`，缺失即条件不生效留痕）——全程审计留痕 |
| 6. Connector 代理 | ✅ manifest 契约校验（接口漂移当场爆炸）、binding/secret 解析（binding 永不进包，缺失即报错）、调用回执 + 注入前脱敏；内置 mock 执行器（`mock://` 固件目录），真实 sql/openapi 执行器属部署侧适配（M6） |
| 7. 对账器 settle | ✅ 剧集完成后对 objective 型对象自动对账（受限形式 `settle: {uses: CON-xxx.接口名}`，SPEC v0.4 §6）：decision vs reality 落 `kind: outcome` 的 case（编号顺延、交蒸馏队列），不消耗剧集；自由文本 settle 保守不执行留痕。「闭店后」定时对账需部署侧营业日历，参考实现在剧集完成后立即对账 |

已可演示：Host 起停、包装载 / 注销、定时布防（status 可见 next_fire）、人工发射 event、
precondition 经代理真求值（有 binding 放行唤醒 / 无 binding 保守拦截）、审批门授予、
**唤醒 → 装配 → 沿 pipeline 出草稿**（`episodes` / `episode EP-xxxx` 可见步骤留痕、回执、tokens、草稿）、
对账落 outcome case；**三级停三级全可演示**：剧集停（pipeline 完成 / budget 硬顶 / 步骤失败）、
触发器停（disable 单 Aware）、包停（unload）；kill switch 触发时装载可、唤醒与调用全拒。
单条判断回放见 cli 的 `osca replay`（发布凭据第三样）。

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

# 剧集台账：唤醒装配 + 执行留痕（状态 / 步骤 / 回执 / tokens / 草稿）
uv run osca-host episodes
uv run osca-host episode EP-0001

# 审批门：对 policy.yaml approvals 里的动作授予一次性放行
uv run osca-host approve demo-group-oper-diagnosis 终稿发送管理层
```

## LLM 通道（剧集的 agent 步）

只放抽象接口 + 环境变量配置，不锁定厂商；配置属部署环境，永不进包（与 binding 同一纪律）：

```bash
export OSCA_LLM_URL=https://your-gateway.example/v1   # OpenAI-compatible 网关地址
export OSCA_LLM_MODEL=your-model                      # 模型名
export OSCA_LLM_API_KEY=...                           # 密钥（部署环境注入）

# 测试与全链路演练不联网：mock 固件目录，按调用 tag 读 <目录>/episode/<步骤名>.md
export OSCA_LLM_URL=mock:///opt/osca/llm-fixtures
```

未配置时剧集在第一个 agent 步以人话报错落 `failed`，取数等确定性步骤照常留痕。

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
