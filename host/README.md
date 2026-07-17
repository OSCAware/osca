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
| 5. Policy 拦截器 | ✅ 按步骤工具白名单（默认拒绝）、审批门（M4-W3 绑定挑战：pending → 批/驳 → 一次性 consume；**闭环限定见「M4-W3 审批挑战」节**）、预算硬顶（per-episode tool_calls + **tokens 止损顶**，`200k` 数量记法）、egress 默认全禁、数据脱敏（身份证号/手机号，agent 产出同样过脱敏）、kill switch（公理 A10，两种可求值形式：现役账本 overruled/confirmed 比率；回放红灯率 > X%）。两种条件均为 Tripped / Clear / Unavailable 三态：ratio 的 0/0 = Unavailable、overruled>0 且 confirmed=0 = Tripped；回放档案 `indexes/replay-health.json` 需通过完整 schema 校验并绑定当前 `ledger_tree`。阈值采用整数精确比较；Unavailable 保留既有 Kill 状态，不清除 Tripped，也不把既有 Clear 新触发为停机；首次装载或重启没有既有状态时保持未触发并告警。LLM、Tool、预算与审批授权均在统一授权锁内复核——全程审计留痕 |
| 6. Connector 代理 | ✅ manifest 契约校验（接口漂移当场爆炸）、binding/secret 解析（binding 永不进包，缺失即报错）、调用回执 + 注入前脱敏；内置 mock 执行器（`mock://` 固件目录），真实 sql/openapi 执行器属部署侧适配（M6） |
| 7. 对账器 settle | ✅ 剧集完成后对 objective 型对象自动对账（受限形式 `settle: {uses: CON-xxx.接口名}`，SPEC v0.4 §6）：decision vs reality 落 `kind: outcome` 的 case（编号顺延、交蒸馏队列），不消耗剧集；自由文本 settle 保守不执行留痕。「闭店后」定时对账需部署侧营业日历，参考实现在剧集完成后立即对账 |

已可演示：Host 起停、包装载 / 注销、定时布防（status 可见 next_fire）、人工发射 event、
precondition 经代理真求值（有 binding 放行唤醒 / 无 binding 保守拦截）、审批挑战批/驳（挂起 → 批准 → 一次性放行）、
**唤醒 → 装配 → 沿 pipeline 出草稿**（`episodes` / `episode EP-xxxx` 可见步骤留痕、回执、tokens、草稿）、
对账落 outcome case；**三级停三级全可演示**：剧集停（pipeline 完成 / budget 硬顶 / 步骤失败）、
触发器停（disable 单 Aware）、包停（unload）；kill switch 触发时装载可、唤醒与调用全拒。
单条判断回放见 cli 的 `osca replay`（发布凭据第三样）。

## 用法

```bash
cd host && uv sync

# 前台起 Host：启动即装载样例包；--deployments 声明控制通道可装载的部署清单
uv run osca-host run --load ../examples/oper-diagnosis.osca \
  --deployments ../examples/deployments.example.yaml

# 另开终端：注册表快照 / 装载（只收部署 ID，路径由 Host 侧清单解析）/ 包停 / 关停
uv run osca-host status
uv run osca-host load demo
uv run osca-host unload demo-group-oper-diagnosis
uv run osca-host stop

# 三级停之「触发器停」＋ 操作者人工触发（对应样例 T3）
uv run osca-host disable demo-group-oper-diagnosis AW-001
uv run osca-host enable demo-group-oper-diagnosis AW-001
uv run osca-host fire demo-group-oper-diagnosis AW-001/T3

# 剧集台账：唤醒装配 + 执行留痕（状态 / 步骤 / 回执 / tokens / 草稿）
uv run osca-host episodes
uv run osca-host episode EP-0001

# 审批挑战（M4-W3）：高危写被审批门拦截时挂起 pending 挑战 → approver 列待批清单、
# 批/驳一张具体挑战（绑 challenge_id；principal 名须与 policy 指定审批人相符）。
# admin/operator 无审批面；--token-file 是全局参数，带 approver 自己的 0600 token。
uv run osca-host --token-file approver.token challenges demo-group-oper-diagnosis
uv run osca-host --token-file approver.token approve demo-group-oper-diagnosis CH-xxxxxxxxxxxxxxxx
uv run osca-host --token-file approver.token deny demo-group-oper-diagnosis CH-xxxxxxxxxxxxxxxx
```

## 控制通道的权限面（M4-W0.2 安全内核）

控制通道是本机 unix socket（默认 `~/.osca/host.sock`，`--socket` 可改）。运行目录
从 `/` 起逐级以 `openat`/`dir_fd + O_DIRECTORY + O_NOFOLLOW` 打开，只允许最后一级
由 Host 创建；最终目录 fd 持有到 ControlServer 完全关闭。token、principals、lock
全部相对该 fd 操作。Python 的 Unix socket bind 没有 `dir_fd`，因此 bind 前后都复核
父目录 inode；生产模式另要求每级祖先由 root/Host UID 持有、不可由 group/other
改名且允许目标 group 遍历（root/Host 所有的 sticky 临时目录可用）。路径被换时拒绝
启动，异常与 shutdown 只按保存的 socket inode 清理。
协议 v1 另有读/写超时、64 KiB 行上限、响应上限、连接上限和统一错误响应。

**信任模型两档（诚实标注）：**
- **开发模式**（不传 `--control-group`）：运行目录/socket 为 `0700/0600`，全部
  进程同 OS uid。token 只防误操作和角色越权，**不抵抗同 uid 失陷进程**；同 uid
  本来就能读取彼此文件和内存。
- **生产模式**（显式传 `run --control-group GROUP`）：运行目录必须由部署者预置为
  Host owner、目标 group、`0710`，socket 为该 group 的 `0660`。group 只提供目录
  遍历与连接可达性，不绕过 kernel peer UID、principal token、UID 绑定或角色检查。
  group 不存在、祖先不可安全遍历、目录 owner/group/mode 不符、chown/chmod 失败均
  拒绝启动，不降级。

生产示例（账号/group 名按部署环境替换；自定义路径必须写真实无符号链接的绝对路径，
macOS 的 `/tmp` 是系统链接，需写 `/private/tmp`）：

```bash
sudo install -d -o osca-host -g osca-control -m 0710 /run/oscaware
sudo -u osca-host uv run osca-host --socket /run/oscaware/host.sock \
  run --control-group osca-control --deployments /etc/osca/deployments.yaml
```

进程级身份靠 token。Host 生成的 admin token 仍在 `<socket>.token`（0600，绑定
Host uid，开发 CLI 默认读取）。生产 principals 文件只保存客户端 token 的 SHA-256
摘要，不保存明文；明文由对应客户端 UID 自己持有在 0600 文件中，并以全局参数
`--token-file` 传给 CLI：

```bash
openssl rand -hex 32 | tr -d '\n' > operator.token  # 至少 32 字节随机数；不要手工编 token
chmod 0600 operator.token
shasum -a 256 operator.token           # 将摘要写入 Host 侧 principals 文件
```

```yaml
# <socket>.principals.yaml（0600，Host 所有）
- name: operator-console
  role: operator
  uid: 30001
  token_sha256: 6f...共 64 位十六进制...
```

凭据读取从同一 fd 最多取 `MAX+1` 字节，再验 UTF-8；不依赖可竞态的预读
`st_size`。轮换 = 客户端换明文、部署者换摘要后重启 Host；principal token 在线撤销
仍为换文件重启（诚实标注）；挑战级撤销 `ChallengeStore.revoke` 状态机已备、控制通道
命令未接线（撤销权归 approver 本人还是 host_admin 应急面——矩阵归属待定后再接）。
角色能力矩阵（`osca_host.authz`，测试钉住）：

| 角色 | 允许 | 明确禁止 |
|---|---|---|
| `host_admin` | status / load / unload / enable / disable / fire / episodes / episode / stop | 审批面（approve/deny/challenges——admin 管生命周期但不可伪造业务审批） |
| `operator` | status / enable / disable / fire / episodes（摘要；脱敏 DTO 属 W2，当前与 admin 同构——勿授予不可信进程） | load、审批面、完整 episode、stop |
| `approver` | challenges / approve / deny（M4-W3：绑 challenge_id 批/驳一张具体挑战；principal 名须与挑战指定审批人相符——冒名/越权/一次性/过期由挑战状态机 fail-closed） | 其余全部（无生命周期/快照/启停/剧集面） |
| `expert` | episodes / episode（M4-W1 专家端只读交付面——draft 即交付物；episodes 摘要当前覆盖 Host 上全部包，per-principal 包域收窄未做，勿在多租户 Host 上授予） | 其余全部 |

### M4-W3 审批挑战（诚实标注：机制完成，闭环待 M5/M6）

W3 落地的是**机制**：绑定挑战状态机（approver / episode / payload 摘要 / 过期 +
一次性 consume，冒名/重放/偷梁换柱/跨剧集/过期各有测试钉住；`consume_or_raise`
单锁原子，无「消费失败与挂起之间恰好获批 → 同绑定双份放行额度」竞态窗）+ 控制通道
`challenges/approve/deny` + IM 审批卡桥接（私仓 oscapipe W3.2）。

**「批准 → 放行一次真写」的端到端闭环当前不可达**：真写执行未接入（connector 写路径
的 params 未从剧集传入，payload 摘要恒为空串摘要）；审批门拦截即步骤 failed，runner
没有剧集内挂起等批——而挑战绑定 episode_id，重跑是新剧集，已批挑战永远等不到
consume，到 TTL 过期作废。M5/M6 接入真写时须一并落地：①剧集内挂起等批后重试消费；
②审批卡带脱敏后的人类可读 payload（只给摘要 = 让审批人对哈希拍板）；③TTL 按 IM
人审时延重估（现值 5 分钟是机制口径）。

`load` 只收 `deployment_id`：包路径、bindings、解压目录一律由 Host 侧
`--deployments` 清单解析（相对路径按清单文件所在目录解析），绝不从连接者
透传（confused-deputy 面收口）。load 准备在线程中按 deployment 单飞，不同
deployment 可并行；发布段才进入短锁并复核 lifecycle/generation/tombstone。
`STARTING → RUNNING → DRAINING → STOPPED` 保证 stop/unload 胜过迟到 load，同时
慢 load 期间 status 仍可快速返回。

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

## 开发

```bash
cd host
uv sync
uv run pytest        # 测试（含控制通道端到端）
uv run ruff check .  # 代码检查
```
