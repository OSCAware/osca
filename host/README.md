# osca-host（开发中，M2）

OSCA 运行框架 Host 参考实现——**确定性常驻进程，无 LLM**。
把 `.osca` 包从静态资产变成能自己醒来干活的 agent。

## 当前进度（诚实标注）

| 组件（架构 §4） | 状态 |
|---|---|
| 1. Loader + Linter | ✅ 复用 cli 装载核心（完整性 / lint / binding 比对 / 索引重建）+ 运行时结构解析 |
| 2. 触发表 | ⬜ W2 — 槽位已登记（`declared`），定时器 / 轮询器待编译布防 |
| 3. 闸门 gate | ⬜ W2 — gate 声明已解析保留，编译期矛盾检查待做 |
| 4. 剧集装配器 | ⬜ W3 |
| 5. Policy 拦截器 | ⬜ W4 |
| 6. Connector 代理 | ⬜ W4 |
| 7. 对账器 settle | ⬜ W5 |

已可演示：Host 起停、包装载 / 注销、**三级停之「包停」**（注销 = 释放全部 watcher 槽位）。

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
