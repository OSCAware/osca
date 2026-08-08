# osca-host（M2 七组件 ＋ M4 控制通道安全内核·审批挑战）

OSCA 运行框架 Host 参考实现——**控制平面确定性常驻，本体无 LLM**；
LLM 只活在短命的剧集（认知平面）里。把 `.osca` 包从静态资产变成能自己醒来干活的 agent。

## 当前进度（诚实标注）

M2 七组件之上，M4 已落：控制通道安全内核（W0/W0.1/W0.2：传输层权限 + Principal/角色矩阵 +
运行目录锚定）、专家只读交付面（W1：expert 角色 episodes/episode）、审批挑战状态机与
approver 命令面（W3：绑定挑战 + challenges/approve/deny，**闭环限定见「M4-W3 审批挑战」节**）。
运营控制台（M4-W2）与 IM 审批卡/Creator（M4-W3.2/M5）在私仓 oscapipe。
**M6 真写全接通（机制完成·诚实标注）：** 传写 params（W5-D1）+ 可恢复剧集（W5-D2a/b，写命中审批门 →
挂起 → approve 恢复兑现 / deny 回落，L2 持久活过重载/重启）+ **真实执行器**（W6-3，sql_readonly/openapi）
+ secret 三不（W6-2）+ 人类可读脱敏 payload（W6-4）+ TTL 可配（W6-1）；W7 把真实执行器**端到端串起来**
（真实 sql_readonly 读 fake sqlite + 真实 openapi 写打 fake http.server，独立集成工程演练）。
**口径：真写全接通 = 审批闭环 + 真实执行器机制通（测 fake 后端）——非真实系统写验证**（生产库/生产 API
真连通归部署侧 1.1）。详见「M4-W3 审批挑战 + M6 真写全接通」节。

| 组件（架构 §4） | 状态 |
|---|---|
| 1. Loader + Linter | ✅ 复用 cli 装载核心（完整性 / lint / binding 比对 / 索引重建）+ 运行时结构解析 |
| 2. 触发表 | ✅ 定时器 / 轮询器编译布防，哈希去重共享（引用计数；watch 按包隔离）；轮询经 Connector 代理取数，emit_when 真比对（SPEC v0.4 §4），首轮基线、无 emit_when 按状态变化发射；event 由控制通道人工发射 |
| 3. 闸门 gate | ✅ combine（any/all/sequence）+ debounce + enabled + **precondition 真求值**（经代理取数，返回空/取数失败即拦截并复述 on_fail；不可求值保守放行留痕）。编译期矛盾检查在 lint（OSCA041）与装载时共用 `osca_cli.triggers` |
| 4. 剧集装配器 + 执行器 | ✅ 唤醒 → 一次性上下文（AGENT.md + structure + discretion + 引用 objects + 判断 top3–7 各带代表 case）进剧集台账；检索 = 签名表硬过滤 + trust/confirmed 排序（语义排序归 M3 索引器）；policy.yaml 刻意不入上下文（公理 A5）。装配后即交剧集执行器（认知平面，独立线程）沿 pipeline 出草稿：performer 受限集 connector / agent / optimizer（初版贪心）/ human（飞轮采集点，机器到此为止）/ runtime（移交对账器）——SPEC v0.4 §5。步骤衔接（M8-T3）：agent 步可声明 `produces.as: json` 出**结构化产出**（人话 draft 与结构化数据**并存**，须可溯源上游产物，解析失败一律 fail-closed 不退回文本；**「合法 JSON」按 RFC 8259 判、不按 Python `json` 的默认宽容度**——`NaN`/`Infinity`/`-Infinity` 字面量与溢出成 ±inf 的数值（`1e999`）、同一对象里的重复键（默认静默取最后一个＝猜）、超 `MAX_STRUCTURED_DEPTH`（32）层的嵌套，三口一律在**解析这一次**上拒；深度上限按下游倒推——实测最浅的下游消费者 330 层即 `RecursionError`，取其 1/10 留一个数量级余量；这把闸提在 `osca_host.jsongate`，**与执行器解析后端响应体共用同一份实现**，M8-T4）；下游步骤可用 `input.from` 把上游 `{接口ref: 回执}` 字典收窄到**那一格**（不写即取整份，既有包一字不变）——lint OSCA042/043 同判据静态咬。**结构化产出的脱敏是「一份原文、一份台账副本」**（M8-T4）：交下游写步的产物是原文（`payload_digest` 绑它、挂起快照存它），进剧集台账的那一份过 `policy.redact`——与连接器回执同权，台账里不留绕过脱敏的字段。**执行器边界**（M8-T4）：未预期异常一律在 `run_episode` 边界收束成 `failed` ＋ 人话停因，台账不留 `running` 僵尸；堆栈进 Host 日志、台账只记异常**类型名**（内文可能带连接串/密钥），关停信号（`KeyboardInterrupt`/`SystemExit`）照旧穿透 |
| 5. Policy 拦截器 | ✅ 按步骤工具白名单（默认拒绝）、审批门（M4-W3 绑定挑战：pending → 批/驳 → 一次性 consume；**闭环限定见「M4-W3 审批挑战」节**）、预算硬顶（per-episode tool_calls + **tokens 止损顶**，`200k` 数量记法）、egress 默认全禁、数据脱敏（身份证号/手机号，agent 产出同样过脱敏）、kill switch（公理 A10，两种可求值形式：现役账本 overruled/confirmed 比率；回放红灯率 > X%）。两种条件均为 Tripped / Clear / Unavailable 三态：ratio 的 0/0 = Unavailable、overruled>0 且 confirmed=0 = Tripped；回放档案 `indexes/replay-health.json` 需通过完整 schema 校验并绑定当前 `ledger_tree`。阈值采用整数精确比较；Unavailable 保留既有 Kill 状态，不清除 Tripped，也不把既有 Clear 新触发为停机；首次装载或重启没有既有状态时保持未触发并告警。LLM、Tool、预算与审批授权均在统一授权锁内复核——全程审计留痕 |
| 6. Connector 代理 | ✅ manifest 契约校验（接口漂移当场爆炸）、binding/secret 解析（binding 永不进包，缺失即报错）、调用回执 + 注入前脱敏；执行器按 endpoint scheme 分派——内置 mock 执行器（`mock://` 固件）+ **真实参考执行器**（sql_readonly/openapi，W6-3，测 fake 后端）；生产驱动（生产库/生产网关）由部署侧按 `Executor` 协议注入。HTTP 执行器解析后端**响应体**时过**与模型产出同一把 JSON 闸**（`osca_host.jsongate`，同一份实现、同一个深度上限 32，M8-T4）——读回执是下游写步 body 的原料，还要进台账、上审批卡、进挂起快照、原样上 wire；过不了闸 ＝ 取数失败 ＝ 剧集失败（与「非 2xx」「响应截断」同一条路），不半解析、不把原文当字符串回执、不回空回执 |
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
| `approver` | challenges / approve / deny（M4-W3：绑 challenge_id 批/驳一张具体挑战；principal 名须与挑战指定审批人相符——冒名/越权/一次性/过期由挑战状态机 fail-closed。**名绑定是全局的、无包域**：同名审批人可批任何指定其名的包，challenges 覆盖任意包全部待批项、不按审批人过滤；per-principal 包域收窄归 T1/T2，之前勿在多租户 Host 上授予——与 expert 同款告示） | 其余全部（无生命周期/快照/启停/剧集面） |
| `expert` | episodes / episode（M4-W1 专家端只读交付面——draft 即交付物；episodes 摘要当前覆盖 Host 上全部包，per-principal 包域收窄未做，勿在多租户 Host 上授予） | 其余全部 |
| `requester` | fire / status（M8-T2 员工触发桥的最小身份：只发射与看快照；剧集结果读取归桥自己的 expert token，不给本角色） | 其余全部（无剧集读面/装卸/启停/审批） |
| `deployer` | load / unload / status（M8 App 服务层装载面的最小身份；界面进程永不持 host_admin——信任模型 M4-W0.1 不破。**装与卸是同一件事的两半**：只给 load 不给 unload，App 层撤不掉自己装错的部署、只能请 host_admin 出手，反而逼出更高的权限） | 其余全部（无启停/发射/剧集面/审批/stop——运行期启停与整机停机属 operator/host_admin 的面；装载面只管「装着什么」，不管「此刻跑不跑」） |

### M4-W3 审批挑战 + M6 真写全接通（诚实标注：审批闭环 + 真实执行器机制完成，测 fake 后端 ≠ 生产验证）

W3 落地的是**机制**：绑定挑战状态机（approver / episode / payload 摘要 / 过期 +
一次性 consume，冒名/重放/偷梁换柱/跨剧集/过期各有测试钉住；`consume_or_raise`
单锁原子，无「消费失败与挂起之间恰好获批 → 同绑定双份放行额度」竞态窗）+ 控制通道
`challenges/approve/deny` + IM 审批卡桥接（私仓 oscapipe W3.2）。

**「批准 → 放行一次真写」的端到端闭环 M6 已通（含 L2 磁盘持久，活过包重载 / Host 重启）：**
- **W5-D1 传写 params**：connector 写路径把模型给出的写 params 传入，payload 摘要绑
  **真实被写内容**（不再是空串摘要）；写门两条 fail-closed（空内容拒 / 非 JSON 可序列化拒）。
- **W5-D2a 可恢复剧集**：写命中审批门 → 剧集**挂起**（`suspended_pending_approval`，释放
  线程）→ approve 事件到达 → 从审批步**恢复重试消费**、写执行器落地；deny / 过期 → 回落
  保守默认（不写）+ 上报。挑战绑 episode_id，恢复在**同一剧集**内兑现（重跑=新剧集不认）。
  「审批决定先到、挂起登记后到」的丢唤醒窗由登记侧复查自愈 + 惰性清扫双重堵死。
- **W6-3 真实执行器落地**：`_execute_real` 已按 endpoint scheme 分派**真实参考执行器**（不再是桩）——
  写走 openapi（urllib POST，见下「真实执行器」条），读走 sql_readonly；`mock://` 固件执行器仅供演练。
- **W7 端到端演练**：独立集成工程把真实执行器**端到端串起来**——真实 sql_readonly 读 fake sqlite +
  真实 openapi 写打本地 fake http.server，走完挂起-approve-恢复-真写落地；对抗审查证被写内容真落
  fake 后端 / secret 反射清洗 / egress-SSRF / 恢复路径偷梁换柱拒绝。
- **措辞纪律**：真写全接通 = **审批闭环 + 真实执行器机制通（测 fake 后端：本地 sqlite / http.server）——
  非真实系统写验证**。生产库/生产 API/生产 secret manager 的真连通与真写落地仍归部署侧（1.1/部署验收）。

**真写全接通四件事已落（W6/W7·机制完成·诚实标注；真·待续见文末两条）：**
- **secret 解析 ✅**（W6-2）：可插拔 `SecretResolver`（默认 env-var，部署侧可注入 file/vault/callable）取值
  交执行器，值**三不**（不进包/日志/剧集）；取不到 / 空串 / resolver 抛错一律 fail-closed（错误只带 secret_ref
  名、绝不带值）；secret 前置在 egress **之后**（egress 拒则不解析凭据）。真系统 secret manager 取值归部署侧。
- **真实执行器 ✅**（W6-3）：`_execute_real` 按 endpoint scheme 分派可插拔执行器——内置参考适配器
  sql_readonly（sqlite `mode=ro` 只读强制、包内固化 impl SQL 参数化命名绑定防注入）/ openapi（urllib，
  method+path+params、secret 作 Bearer 头、**不跟随重定向**防 SSRF、**URL path = endpoint 的 path 段（部署侧
  挂载前缀）+ interface 的 path 段（包内相对路由）**，两段各自强制锚定 `/` 防 host 混淆、缝上归一斜杠（不出 `//`）、
  拼完含上跳段（`..`，含 `%2e%2e` 等编码变体与反斜杠）即 fail-closed 不归一放行、响应体
  读上限 + 截断/超限 fail-closed）；生产驱动（postgres/mysql/生产网关）由部署侧按 `Executor` 协议注入，未注册
  scheme / mcp 一律 fail-closed；执行器异常统一兜成 fail-closed 回执（`call()` 恒回 Receipt）。**诚实标注：测
  fake 后端（本地 sqlite / 本地 http.server）——生产库/生产 API 的真系统验证仍归部署侧（1.1/部署验收）。**
- **审批卡人类可读脱敏 payload ✅**（W6-4，跨仓 host + oscapipe）：`Challenge` 新增 `payload_display`
  = `policy.redact(原始 params)`（PII 已抹的脱敏视图，含 dict 键），随 DTO / L2 快照跟随；`payload_digest`
  仍绑**原始** params（防偷梁换柱、写执行器写原文，不变）。审批卡（oscapipe notices）呈现脱敏写内容原文供人
  拍板（不再是橡皮图章哈希，digest 降为技术核对小字），渲染叠**防注入**（键/值同包 code span 中和 markdown、
  截断丢整行不切断 span）+ **防超长**（字段/总长截断标注）。redact 只脱**显示**、不动被写内容（批动作不批 PII）。
- **TTL 按人审时延重估 ✅**（W6-1）：授权过期窗口从硬编码 300s 变 **policy 可配**——包级
  `default_ttl_seconds` + 每 action `ttl_seconds` 覆盖；缺省/非法一律 fail-closed 回落机制默认 300s
  （绝不 fail-open 成无过期），公仓 osca-cli lint 校验字段形状（正有限数秒）。诚实标注：**真实人审
  节奏仍由部署侧按 IM 实况设**——参考默认只是占位口径。
- **挑战 + 挂起态持久化 ✅**（W5-D2b · L2）：挂起快照原子写盘（fd 锚定运行目录）+ 装载时重挂——
  活过**包重载**且活过 **Host 重启**（同一重挂路径；版本戳按源文件内容指纹，漂移即 fail-closed 丢弃）。
  诚实标注：approve 决定不活过重启（盘上挑战恒 pending，须重发）；跨崩溃 exactly-once「写重复侧」靠 W6
  写执行器幂等键（删盘早于写执行已关 reload/restart 双写窗，残留只真·硬件半写）。
- **approver 名绑定无包域**（见上表告示）：包域收窄归 T1/T2 多租户。

`load` 只收 `deployment_id`：包路径、bindings、解压目录一律由 Host 侧
`--deployments` 清单解析（相对路径按清单文件所在目录解析），绝不从连接者
透传（confused-deputy 面收口）。清单条目可标 **`autoload: true`（M8-T6）**：
Host **启动时自动装载**它——注册表是内存态，进程一重启装着的包就全没了，
而重启是日常（部署脚本重启服务／机器重启／崩溃拉起），在这条之前每次重启
都得有人手工补 `load`，没人补就是「服务活着、包一个没有」的空跑。语义与
systemd 同构、两层别混：**`autoload: true` ≈ `enable`**（期望态，声明「这台
机器上它应当装着」）；**控制通道 `unload` ≈ `stop`**（运行期动作，临时停一个
包，下次启动还会回来——要永久移除请改清单，别指望 unload 记住）。一条装不上
不拦别人（逐条独立，失败记 error 并计入退出码，不阻塞 Host 起来）；`autoload`
只收真 bool，`"true"`/`yes`/`1` 这类近似写法一律拒。清单在每次 `load` 前**热重读**（M8-T2）：
新增的部署条目免重启即可装载；重读失败时新的 `load` **fail-closed**，拒因
原样带回重读失败原因。已经装载的实例继续按内存运行态服务、不受影响；上次有效
清单缓存只保这些已经跑着的实例，不授权新的变更。load 准备在线程中按 deployment 单飞，不同
deployment 可并行；发布段才进入短锁并复核 lifecycle/generation/tombstone。
`STARTING → RUNNING → DRAINING → STOPPED` 保证 stop/unload 胜过迟到 load，同时
慢 load 期间 status 仍可快速返回。

`fire` 的响应在 `{ok, detail}` 之上带两个**可选**字段 `episode_id` 与 `operation_id`
（M8-T3-a/T3-b，契约见 SPEC 附录 A.7）：**当且仅当这一发真的装配出剧集时才有这两个字段**，
值即台账里的那一条（`episodes` 摘要同名字段查得到）。两者**同源同生同灭**——都取自这一发
装配出的那条剧集，要么都有、要么都没有。闸门未唤醒 / 账本刷新失败 / kill switch 拒唤醒时
`ok=true` 但**两个字段都不带**（detail 如实写明「未装配剧集」）；未布防 / 跨代失效 / Aware
停用 / Host 关停仍是 `ok=false` + 拒因。调用方（员工触发桥等 `requester` 身份）据此直接绑定
自己这一发的剧集——按时间窗反查台账只作兜底，命中多条即放弃绑定并报错，不许猜。
**绑定与轮询按 `operation_id`**：`EP-xxxx` 是**进程内**短展示编号（重启从 `EP-0001` 重新计号，
同一个号在重启前后是两条不同剧集），`operation_id`（`EO-<uuid>`）才是跨重启唯一的机器身份。

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
目录里放 `<接口名>.yaml` 固件。**真实参考执行器**（sql_readonly / openapi，W6-3）测 fake 后端
（本地 sqlite / http.server，端到端演练见独立集成工程）；**生产驱动**（生产库 / 生产网关）由部署侧按
`Executor` 协议注入（M6 对接约定）。

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
