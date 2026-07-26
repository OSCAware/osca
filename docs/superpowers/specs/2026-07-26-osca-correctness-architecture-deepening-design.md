# OSCA 正确性修复与架构深化设计

**日期：** 2026-07-26

**状态：** 已实施并验证

**兼容性约束：** 保持现有 CLI、Host 对外行为与 `.osca` 包格式兼容

## 1. 背景

本轮整体 Code Review 发现七项需要处理的问题：

1. `pack` 在 lint 后再次读取源目录，目录并发变更可把未经过 lint 的内容写入归档。
2. Host 从开发态目录装载时，校验与运行时解析再次读盘，可能消费不同代内容。
3. 挂起快照的包版本戳错误排除了任意层级名为 `indexes` 的目录，且读取失败被静默忽略。
4. 共享 watcher 串行等待订阅方，慢订阅会阻塞同一 watcher 的其他订阅及后续调度。
5. Policy 的审计记录和剧集预算表没有生命周期上界。
6. 月度 schedule 把 `bool` 当作合法整数日期。
7. CLI README 声称有 22 条规则，实际为 25 条。

前三项具有相同根因：同一项包操作缺少明确的“内容代际”。第四、第五项则暴露出运行时状态的所有权和生命周期分散。本设计因此不只做局部修补，而是增加两个内部深层 Module：

- **Package Snapshot Module**：定义一代包内容是什么。
- **Episode Lifecycle Module**：定义一段剧集状态由谁迁移、资源何时释放。

## 2. 目标与非目标

### 2.1 目标

- lint、checksum、归档和 Host 首次解析消费同一份不可变字节快照。
- 任意包文件读取失败或快照不完整时 fail-closed。
- 挂起版本戳准确表示被装载并执行的那一代包内容。
- 自动 watcher 的订阅方故障域相互隔离。
- 剧集进入终态后，其临时预算状态得到确定释放。
- 长驻 Host 的 Policy 审计内存有明确上界。
- 所有修复均有能复现原问题的回归测试。

### 2.2 非目标

- 不改变 CLI 命令、参数、退出码或人类可读输出的既有语义；唯一安全收紧是 `osca lint`
  对符号链接目录明确返回稳定失败，不再透过链接读取内容。
- 不改变 Host 控制协议、Episode 状态名或 `.osca.zip` 内容格式。
- 不引入新的第三方运行时依赖。
- 不把账本目录改成只读；`cases/` 等运行期账本仍可按现有事务规则写入。
- 不在本轮重写完整 Host 状态机或 Connector 扩展协议。

## 3. 总体方案

采用分阶段深化方案：

1. 引入 Package Snapshot Module，以一次捕获的字节作为 lint、pack、load 和版本戳的共同真相。
2. 引入 Episode Lifecycle Module，集中终态迁移和资源释放。
3. 为 watcher 引入受控的异步派发 Implementation。
4. 完成 schedule 类型判断与文档修正。

设计遵循以下原则：

- **Depth**：调用方只表达“捕获包”或“结束剧集”，复杂的安全细节藏在 Module 内。
- **Leverage**：一个 Interface 同时消除多个读盘竞态和多个清理遗漏点。
- **Locality**：包身份规则集中在快照 Module；剧集资源规则集中在生命周期 Module。
- **Seam**：保留现有入口，通过 Adapter 逐步替换内部的直接文件系统访问与分散状态写入。

## 4. Package Snapshot Module

### 4.1 Problem

当前 `lint_package(root)`、`pack_package(root)`、`load_for_host(root)` 与 `_pack_stamp(loaded)` 都能独立遍历和读取目录。因此，“校验通过的包”“进入归档的包”“Host 实际执行的包”和“挂起快照绑定的包”可能不是同一代内容。

此外，当前版本戳：

- 用 `if "indexes" in parts` 排除了所有层级的 `indexes`，会漏掉业务目录中的真实文件；
- 用 `contextlib.suppress(OSError)` 忽略读取失败，可能让不同内容得到相同或误导性的戳。

### 4.2 Interface

在 CLI 包层增加不可变快照抽象，名称以实现时的既有命名风格为准，职责固定为：

```python
PackageSnapshot.capture(root) -> PackageSnapshot
snapshot.files                  # 相对路径 -> bytes，只读视图
snapshot.directories            # 捕获时存在的相对目录集合
snapshot.exists(relpath)
snapshot.read_bytes(relpath)
snapshot.read_text(relpath, ...)
snapshot.fingerprint(...)
```

捕获失败使用明确异常，例如 `SnapshotError`，由 CLI/Host Adapter 转成现有 `OpResult` 或 fail-closed 日志，不把 traceback 暴露给正常用户。

`OscaPackage` 增加对快照的引用，并提供规则所需的文件、目录和文本查询。规则不再直接通过 `pkg.root` 读取已捕获的只读资产。

### 4.3 Capture Implementation

捕获过程遵守：

1. 只纳入现有 `package_files` 口径允许进入交付件的文件。
2. 每个文件的内容只读取一次，随后只消费内存中的 `bytes`。
3. 符号链接沿用现有安全策略并 fail-closed。
4. 枚举或读取中出现文件消失、权限错误、特殊文件等情况，整次捕获失败。
5. 相对路径必须规范化且留在包根内。
6. 快照对象发布后不可变；排序只影响输出顺序，不影响内容。
7. 目录捕获复用 zip 路径的 `MAX_MEMBER_BYTES` 与 `MAX_TOTAL_BYTES`：单文件或总字节数超限时
   稳定失败，避免开发目录中的误放 dump 形成 OOM 面。

`osca lint` 当前会透过符号链接读取 YAML 和扫描文本，而 `pack`/`load` 会拒绝符号链接。快照
接入后明确统一为拒绝：lint Adapter 把 `SnapshotError` 转成与 pack 同风格、带链接相对路径的
稳定失败信息。这是有意的安全语义收紧，并用回归测试固定，不让异常 traceback 或偶然文案成为接口。

目录文件系统本身不提供通用原子快照。本 Module 的安全保证不是“源目录瞬间静止”，而是：

> 一旦捕获成功，后续校验、打包、解析和指纹均只消费已捕获的同一份字节；捕获之后的目录变化不会混入当前操作。

### 4.4 Lint 与 pack 数据流

```text
source directory
      |
      v
PackageSnapshot.capture
      |
      +--> OscaPackage parse --> lint rules
      |
      +--> checksum manifest
      |
      +--> deterministic zip
      |
      +--> package_id / output name
```

`pack_package` 的 Adapter 先捕获快照，再对该快照 lint；只有同一快照 lint 通过后，才用它生成 checksum 和 zip。源目录不再在 lint 后被重新读取。

现有 `lint_package(path)` 入口保留；它内部捕获一次快照并调用新的快照 lint Interface。

### 4.5 Host directory load 数据流

开发态目录装载仍返回原有 `LoadedPackage.root`，不改变调用方观察到的路径。内部流程改为：

```text
directory gate
      |
      v
PackageSnapshot.capture
      |
      +--> lint / runtime contract / aware parse
      |
      +--> LoadedPackage.pack
      |
      +--> LoadedPackage package generation fingerprint
```

装载门禁和运行时结构不再分别调用 `load_package(root)`。zip 装载在临时目录完成完整性、lint、
binding 和索引校验后，必须在原子切换 `dest` **之前**从该临时目录捕获快照。切换成功后，
`LoadedPackage.root` 仍指向 `dest`，而内部解析直接消费切换前捕获并随装载结果传递的同一份
快照；不得在切换后重新遍历 `dest`，否则会重新打开本设计要消除的读盘窗口。

账本刷新仍在现有 `ledger_lock` 内执行；刷新成功时创建一份新快照，完成 lint、索引计算与 kill-switch 计算后，再原子替换 `loaded.pack` 和对应版本指纹。失败时保留旧代。

### 4.6 路径规则与 Adapter

当前规则中的以下直接路径访问迁移到 `OscaPackage`/snapshot Interface：

- 必备文件存在；
- 标准目录存在；
- manifest `entry` 存在且不越界；
- Connector `impl` 存在且不越界；
- OSCA050 全文本秘密扫描。

`LoadedPackage.root` 继续承担可变账本写入和现有部署 Adapter 的路径兼容职责。内置只读资产消费优先从快照读取；若某个现有内部接口必须接收 `Path`，使用窄 Adapter，不让它重新定义包身份。

### 4.7 Fingerprint

版本指纹由快照生成，算法保持当前可识别的 `fp:<sha256>` 形式：

```text
sha256(sorted(relative_path + NUL + bytes + NUL))
```

排除规则：

- 排除包根下 `.git/`；
- 排除包根下生成缓存 `indexes/`；
- 不排除其他层级名为 `indexes` 的目录，例如 `judgments/indexes/`。

任何应计入指纹的文件无法读取时，快照捕获已经失败，不存在“忽略错误继续算戳”的路径。

指纹在 `PackageSnapshot` 构造时预计算。剧集装配时，把**装配所消费的那一代**
`loaded.pack.snapshot.fingerprint` 钉到 Episode 的内部字段，例如 `package_fingerprint`。
该字段随 Episode dump 进入 L2 快照。persist 必须直接写 Episode 已钉住的指纹，不得再次读取
`loaded.pack`：后者可能已被同包另一触发器的账本刷新从 G1 换成 G2，而当前剧集的上下文、payload
和 `write_params` 仍来自 G1。

重挂时，以新装载代的快照指纹与 Episode 钉住的指纹比较；不相等即按现有语义 fail-closed
丢弃旧快照。这样版本戳准确表示剧集实际装配并执行的内容代际，而不是落盘时碰巧在注册表中的代际。

### 4.8 升级迁移行为

新旧指纹口径并不完全相同：新算法会计入嵌套 `indexes/` 业务文件、排除 `.DS_Store`，且不再
忽略读取失败。因此，升级前已经落盘、且内容受这些口径差异影响的 L2 挂起快照，在升级后的首次
重挂中会因指纹不相等而被 fail-closed 丢弃。

这是安全侧的预期行为，不做旧算法兼容回退，避免把无法证明同代的高风险写剧集重新挂起。实现必须：

- 在重挂日志中明确记录“指纹算法/内容代际不匹配，旧挂起快照已拒绝”；
- 增加旧口径快照升级后被拒绝的回归测试；
- 在 `CHANGELOG.md` 记录这一可观察的升级行为。

## 5. Episode Lifecycle Module

### 5.1 Problem

Episode 的状态写入分布在 Runner、Host 执行异常、挂起登记失败、恢复、unload、shutdown 等路径。Policy 的 `_tool_calls`、`_tokens` 以 `episode_id` 为键，却没有统一的释放时机。`audit` 也是无限增长的 list，而控制面只展示尾部 20 条。

### 5.2 Interface

新增内部生命周期协调对象或等价 Module，提供单一状态迁移 Interface：

```python
lifecycle.suspend(episode, challenge_id)
lifecycle.resume(episode)
lifecycle.finish(episode, status, reason=None)
lifecycle.evict(episode_id)
lifecycle.stop_package(package_id, reason)
```

最终命名可按现有代码风格调整，但所有终态写入必须经过共同的 Implementation。合法终态保持：

- `completed`
- `failed`
- `stopped`

挂起态保持 `suspended_pending_approval`，不改控制面协议。

### 5.3 Ownership

资源所有权明确如下：

| 资源 | 所有者 | 保留到 |
|---|---|---|
| Episode 台账项 | Host lifecycle | 台账淘汰 |
| Policy tool/token 计数 | Episode lifecycle | Episode 进入终态 |
| challenge → episode 索引 | Suspension lifecycle | 恢复、终态或作废 |
| L2 suspension 文件 | SuspensionStore | 已有持久化协议决定删除/保留 |
| Policy audit 内存窗口 | Policy | 包卸载或超过固定容量 |

挂起剧集不释放预算，因为重挂和恢复必须继续沿用已消费额度。只有进入确定终态后才调用 `policy.release_episode(episode_id)`。

### 5.4 Terminal Transition Implementation

共同终态函数负责：

1. 校验目标状态为合法终态。
2. 设置 `status`、`stop_reason`、`finished_at`。
3. 清除该剧集对应的 `_suspensions` 索引。
4. 释放 Policy 的 `_tool_calls` 和 `_tokens`。
5. 保持 Episode 自身的 `tokens_used`、步骤和结果，控制面历史不丢失。
6. 允许同一终态清理被重复调用，清理必须幂等。

Runner 内部的 `_finish` 通过窄 Seam 接入；Host 捕获异常、挂起登记失败、unload 和 shutdown 不再手写不同版本的终态迁移。

若现有同步 Runner 不适合直接持有 Host lifecycle，则使用终态结果 Adapter：Runner 只设置运行结果，Host 在事件循环侧统一提交生命周期和清理。不得让工作线程直接修改 Host 索引。

### 5.5 Audit Retention

保持 `policy.audit` 可迭代、可切片或通过兼容属性读取的既有使用方式，但底层限制容量。建议容量常量为 1,000 条，并继续让 `snapshot()` 只返回最近 20 条。

超出容量时淘汰最旧审计记录。每条新审计同时写入独立的 `osca-host.audit` 结构化日志（只记录当前审计字段，不记录 secret 值），使长期运维留痕交给部署侧日志保留策略，而不是依赖无限进程内存。容量作为内部常量，不增加对外配置面。

### 5.6 Eviction 与 unload

- 台账容量仍只淘汰终态 Episode，不淘汰运行中或挂起 Episode。
- 淘汰动作经过 lifecycle Interface；即使终态清理已执行，再次释放也必须安全。
- unload 撤销 Policy 后，挂起剧集通过同一终态路径进入 `stopped`。
- 包级 Policy 被移除时，其剩余预算表随对象释放；显式清理仍用于长驻包中的多批 Episode。

## 6. Watcher 派发

### 6.1 Problem

共享 watcher 的 `_fire` 逐个 `await` 订阅方。一个慢或挂死的订阅会延迟其他包，并把调度周期拖到该订阅完成之后。

### 6.2 Design

自动触发使用每订阅独立的受控派发 lane：

- 同一次 fire 立即把各订阅提交到各自 lane；
- 不等待某一订阅完成后才启动下一订阅；
- watcher 的 schedule/poll 循环不被订阅执行耗时串行阻塞；
- 同一订阅默认保持顺序，避免重入；
- lane 已在执行时，对重复 tick 使用有界单槽 pending/coalescing，防止无限任务堆积；
- 重复 tick 被合并时记录 watcher key、订阅标识和累计合并数，便于解释缺失的逐 tick 唤醒；
- 每个订阅异常独立记录，不终止 watcher；
- disable、unload 和 shutdown 会取消或收束对应 lane，不留下无主任务。

人工 `fire_manual` 保持当前确定性语义：等待该目标订阅完成后返回结果，不改现有控制面行为。

## 7. 小型兼容修复

### 7.1 月度日期

Python 中 `bool` 是 `int` 的子类。月度日期判断改为精确整数类型语义，确保 `true`/`false` 被拒绝，同时继续接受 1–31 的整数。

### 7.2 文档

`cli/README.md` 中两处 “22 条规则” 更新为 “25 条规则”。规则本身和 CLI 报告逻辑不变。

## 8. 错误处理

- 快照捕获、解码或指纹所需文件读取失败：拒绝当前 lint/pack/load/reattach 操作。
- lint 发现秘密：归档使用同一快照，因此该内容绝不进入成功产物。
- 账本刷新失败：保留旧快照并拒绝本次唤醒，延续当前 fail-closed 语义。
- watcher 订阅失败：只影响该订阅，错误进入日志，其他订阅和 watcher 存活。
- lifecycle 重复收尾：幂等，不二次删除无关资源、不抛出 KeyError。

## 9. 测试策略

所有缺陷先增加失败测试，再实现修复。

### 9.1 Package Snapshot

- lint 完成后修改 `AGENT.md`，归档仍只包含已 lint 的旧快照，或安全失败；绝不包含未 lint 的新秘密。
- Host 目录 gate 后修改源文件，运行时结构仍来自 gate 使用的同一快照。
- zip 在临时目录校验后、原子切换前捕获；切换后即使 `dest` 被并发改动，首次运行时结构也不重读。
- 目录中单文件或总字节数超限时稳定失败，不分配无界内存。
- `osca lint` 遇到包内符号链接时返回稳定失败信息。
- `judgments/indexes/*.yaml` 改变会改变版本指纹。
- 根 `indexes/` 缓存改变不会改变版本指纹。
- 指纹所需文件读取失败会明确失败，不产生可比较的伪指纹。
- checksum、归档内容和 package id 都来自同一份 bytes。
- 剧集在 G1 装配、`loaded.pack` 刷新为 G2 后再挂起，持久化记录仍为 G1 指纹。
- 旧指纹口径生成的 L2 快照升级后按预期被拒绝，并留下可诊断日志。
- 路径逃逸、符号链接、YAML alias/深度保护等现有安全测试继续通过。

### 9.2 Episode Lifecycle

- 大量终态 Episode 后 `_tool_calls`、`_tokens` 不增长。
- 挂起 Episode 的计数仍保留，恢复后预算连续。
- failed、stopped、completed、异常收尾和 unload 都释放预算。
- 重复 finish/evict 幂等。
- audit 超过容量后长度固定、尾部顺序正确，`snapshot.audit_tail` 不变。

### 9.3 Watcher

- 慢订阅不会阻止快订阅开始。
- 慢订阅不会拖住下一次 watcher 调度。
- 同一订阅不会并发重入，pending 有界。
- coalescing 合并重复 tick 时有明确日志和累计数。
- 一个订阅抛异常不影响其他订阅及后续 tick。
- shutdown/unload 后无遗留派发任务。
- `fire_manual` 仍等待完成并返回错误。

### 9.4 Schedule 与文档

- `day: true`、`day: false` 均 lint 失败。
- `day: 1`、`day: 31` 通过，0 和 32 失败。
- README 规则数与 `len(RULES)` 一致。

### 9.5 全量验证

- CLI 全量测试。
- Host 全量测试。
- 格式化和静态检查。
- 官方 sample packs 全量 lint。
- 检查工作树仅包含本轮预期改动。

## 10. 实施顺序

1. Package Snapshot 失败测试与 Module。
2. pack/lint/Host load/ledger refresh/fingerprint 迁移。
3. Episode Lifecycle 失败测试与 Module。
4. 预算、挂起索引、终态路径和 audit retention 迁移。
5. watcher 独立派发。
6. schedule、README 与 CHANGELOG 修正。
7. 全量回归、差异审查和兼容性核对。

## 11. 验收标准

- 七项 Review 问题均有独立回归测试并通过。
- 同一包操作中，校验与消费不再跨代读盘。
- 挂起版本戳不漏业务文件，不吞读取错误。
- 每个 Episode 的挂起版本戳钉在其装配代际，不受随后账本刷新换代影响。
- 长驻运行时的 Policy 临时状态有明确上界。
- 慢订阅不形成共享 watcher 的全局背压。
- 现有 CLI、Host 外部契约和 `.osca` 包格式保持兼容。
- 原有测试与新增测试全部通过。
