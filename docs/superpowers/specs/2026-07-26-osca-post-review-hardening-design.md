# OSCA 架构深化实施后加固设计

**日期：** 2026-07-26

**状态：** 已实施并验证

**前置设计：** `2026-07-26-osca-correctness-architecture-deepening-design.md`

**兼容性约束：** 保持现有 CLI、Host 对外接口、CLI 人类可读输出与 `.osca` 包格式兼容

## 1. 背景

架构深化实现已经合入并通过 CLI、Host 与下游 oscapipe 的全量验证。实施后 Code Review 发现两个需要补齐的运行时问题，以及三个可在同一边界内消除的实现分叉：

1. `PackageSnapshot.capture()` 以阻塞模式打开目录条目；FIFO 没有写端时，`open()` 永久等待，后续普通文件判定无法执行。
2. policy 审计只把完整记录放在 `LogRecord.osca_audit` 扩展属性中，默认 `%(message)s` formatter 只留下字面量 `policy audit`。
3. Host 已持有已验证的 `loaded.pack`，bindings 二次校验却再次从目录捕获。
4. 挂起索引仍由 Host 直接写入，没有经过已有的 `EpisodeLifecycle.suspend()`。
5. 快照迁移后 `symlink_entries()` 已无调用方。

## 2. 目标

- 非普通文件不得造成捕获、lint、pack 或 Host 装载永久阻塞。
- 非普通文件拒绝信息准确，不被错误包装成“读取失败”。
- 默认生产日志本身包含完整、可持久化的审计记录，同时保留结构化 formatter 的扩展字段。
- 同一次 Host 装载的 bindings 校验只消费已验证的包代际，不再次捕获目录。
- 挂起索引写入统一经过 Episode 生命周期模块。
- 删除快照架构迁移后确认无调用方的旧符号链接扫描实现。

## 3. 非目标

- 不改变符号链接、非常规文件和超限文件的 fail-closed 安全策略。
- 不改变 policy 内存审计记录的字段、保留数量或 `audit_tail` 形状。
- 不改变 Episode 状态名、挂起恢复时序、预算释放时机或 L2 格式。
- 除纠正非常规文件的误导性内部错误文案外，不改变 bindings 文件格式、既有错误语义或公开函数签名。
- 不引入新的日志后端、formatter 或部署依赖。

## 4. 设计

### 4.1 非阻塞捕获与准确错误

打开目录条目时使用：

```python
os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
```

`O_NONBLOCK` 对普通磁盘文件读取没有行为影响；对没有写端的 FIFO，`open()` 会立即返回，使已有的 `fstat()` 普通文件检查能够稳定拒绝该条目。`fstat()` 保留在打开之后，继续承担打开对象身份的最终判定，避免只依赖打开前的路径状态。

内层异常适配按以下优先级处理：

1. `SnapshotError` 原样抛出；
2. 其他 `OSError` 包装成稳定的“包文件读取失败”；
3. 外层目录遍历错误继续包装成“包目录遍历失败”。

因此 FIFO、设备文件等非常规条目统一得到“包内不是普通文件”的准确错误，不会泄漏实现异常类型，也不会被误报为读取失败。

### 4.2 审计日志双通道

每条 policy 审计记录继续：

- 追加到有界 `policy.audit`；
- 作为 `extra={"osca_audit": record}` 提供给结构化 formatter；
- 同时使用 `json.dumps(record, ensure_ascii=False)` 作为实际日志 message。

默认 `%(message)s` formatter 因而得到完整 JSON；部署方已有的结构化 formatter 仍可直接读取 `osca_audit`。JSON 内容仅来自既有审计字段，不扩大敏感信息面。

### 4.3 Host 装载代际复用

Host 在 `load_for_host()` 成功后已经得到同一代 `loaded.pack`。bindings 二次校验改为：

```python
required_bindings(loaded.root, loaded.pack)
```

这样 required bindings 来自已通过校验的快照代际，不触发第二次目录捕获，也不会引入新的 TOCTOU 窗口或把 `SnapshotError` 泄漏到控制通道。

### 4.4 Episode 挂起写入统一

`Host._register_suspension()` 完成 challenge id 和 Episode 台账身份校验后，通过：

```python
self._episode_lifecycle.suspend(episode, challenge_id)
```

写入状态与挂起索引。决定先到的自愈复查、恢复调度与 L2 持久化时序保持不变。已有 `EpisodeLifecycle.suspend()` 因而成为生产路径，不再与 Host 直接写索引形成双轨。

### 4.5 删除死代码

删除无调用方的 `symlink_entries()`，并修正仍引用旧函数名的注释。装载门禁继续使用 `load_symlink_entries()`；快照捕获继续独立拒绝所有包内符号链接。

## 5. 测试

### 5.1 CLI

- 在包目录创建 FIFO，断言 `PackageSnapshot.capture()` 在测试超时预算内返回错误而非挂死。
- 断言错误包含“包内不是普通文件”和 FIFO 相对路径，不包含误导性的 `SnapshotError` 包装文字。
- 常规快照、符号链接、大小上限、lint 与 pack 回归继续通过。

### 5.2 Host

- 触发一条 policy 审计，断言默认日志 message 可解析为 JSON，且字段与内存 audit 记录一致。
- 保留 `osca_audit` 扩展属性断言，确认结构化 formatter 兼容。
- 带 bindings 装载时记录快照捕获次数，断言 Host 不为 required bindings 额外捕获。
- 既有挂起、决定先到、自愈恢复、预算保留与生命周期测试继续通过。

### 5.3 全量兼容性

- CLI 全量 pytest、ruff check、ruff format check。
- Host 全量 pytest、ruff check、ruff format check。
- 两个官方示例包 lint：25 条规则、0 错误、0 警告。
- oscapipe 刷新 `osca-cli` lock 后跑全量 pytest 与 ruff check；既有纯格式漂移不混入本补丁。

## 6. 实施顺序

1. FIFO 回归 RED → 非阻塞打开与异常分层 GREEN。
2. 审计 message 回归 RED → JSON message GREEN。
3. Host 单次捕获回归 RED → 复用 `loaded.pack` GREEN。
4. 在绿色测试保护下路由生命周期挂起并删除死代码。
5. 跑全量兼容性验证，提交、合回 `main`，同步 GitHub、ECS 与 oscapipe 锁。

## 7. 验收标准

- FIFO 不再挂死，且得到准确稳定的非常规文件错误。
- 默认 policy 审计日志包含可解析的完整 JSON 记录。
- 带 bindings 的 Host 装载没有第二次包目录捕获。
- 生产挂起登记经过 `EpisodeLifecycle.suspend()`。
- `symlink_entries()` 不再存在且没有行为回归。
- CLI、Host、样例包和 oscapipe 验证达到实施前基线。
