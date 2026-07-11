# osca-host（开发中，M2）

OSCA 运行框架 Host 参考实现——**确定性常驻进程，无 LLM**。
把 `.osca` 包从静态资产变成能自己醒来干活的 agent。

## 当前进度（诚实标注）

| 组件（架构 §4） | 状态 |
|---|---|
| 1. Loader + Linter | ✅ 复用 cli 装载核心（完整性 / lint / binding 比对 / 索引重建）+ 运行时结构解析 |
| 2. 触发表 | ✅ 定时器 / 轮询器编译布防，哈希去重共享（引用计数）；event 由控制通道人工发射。轮询的 emit_when 求值待 W4 Connector 代理，本周只计 tick 不发射 |
| 3. 闸门 gate | ✅ combine（any/all/sequence）+ debounce + enabled，语义见 SPEC v0.4 草案 §3；precondition 求值待 W4，暂记录声明、默认放行。编译期矛盾检查在 lint（OSCA041）与装载时共用 `osca_cli.triggers` |
| 4. 剧集装配器 | ✅ 唤醒 → 一次性上下文（AGENT.md + structure + discretion + 引用 objects + 判断 top3–7 各带代表 case）进剧集台账；检索 = 签名表硬过滤 + trust/confirmed 排序（语义排序归 M3 索引器）；policy.yaml 刻意不入上下文（公理 A5）。执行属 W5 |
| 5. Policy 拦截器 | ⬜ W4 |
| 6. Connector 代理 | ⬜ W4 |
| 7. 对账器 settle | ⬜ W5 |

已可演示：Host 起停、包装载 / 注销、定时布防（status 可见 next_fire）、人工发射 event 穿透闸门唤醒、
三级停之**包停**（unload）与**触发器停**（disable 单 Aware，撤防全部 watcher）。

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
