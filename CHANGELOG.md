# 变更记录

## [SPEC v0.2] - 2026-07
- 初始公开草案：包格式与目录树、双平面运行契约、Object/Connector/Aware 三型规范、
  Policy 笼子、判断检索契约（两段式）、Cases 存储契约、账本纪律（不变量五条）
- 附完整脱敏样例包 `examples/oper-diagnosis.osca`

## [SPEC v0.3] - 2026-07-11
- 补齐 v0.2 缺失的四类文件规范：osca.yaml 身份证（§1）、structure（§5）、judgment 五段解剖（§9）、case（§10）
- 定稿五处规范与样例分歧：Aware 触发写法（triggers 列表 + gate 闸门）、debounce 归 gate、
  中间产物归 artifact（medium: payload + delivery: internal）、structure 衔接宽松制、URL 铁律精确化（§13）
- 修正：connector binding_ref 必填；cases 大报文外置指针改逻辑形态 {content_hash, store, key}
- 新增工具链契约（§14）
- 变更依据：lint 实现与样例包互证暴露的 9 条问题清单

## [CLI 0.1] - 2026-07-11
- `osca lint`：21 条规则——包结构、ID 与引用、账本纪律（出生证据 / supersedes 链 / trust 计数 / 回放断言）、
  零密钥铁律。规则清单：docs/OSCA-LINT-RULES.md
- `osca pack`：lint 门禁 + 真实 bindings 拦截 + 完整性清单 + 可复现打包（同内容同哈希）
- `osca load`：完整性校验（防篡改）→ lint → binding 比对 → 重建签名表索引
- 样例包修复（lint 抓出）：C-0102 YAML 语法错误、CON-001 缺 binding_ref、OBJ-001/002 缺 kind、sql impl 占位

## [Host 0.1 · M2 七组件齐] - 2026-07-11
- 运行框架 Host 参考实现（架构 §4）：Loader（复用 cli 装载核心）、触发表（定时器/轮询器，
  哈希去重共享）、闸门（combine/debounce/precondition 真求值）、剧集装配器（一次性上下文 +
  签名表检索 top3–7 带 case）、Policy 拦截器（步骤白名单默认拒绝 / 审批门 / 预算硬顶
  tool_calls + tokens 止损顶 / 脱敏 / kill switch）、Connector 代理（manifest 契约校验 +
  binding 部署注入 + mock 固件执行器）、对账器 settle（objective 型自动落 outcome case）
- 剧集执行器（认知平面）：performer 受限集 connector / agent / optimizer（初版贪心）/
  human（飞轮采集点）/ runtime（移交对账）；三级停三级全可演示，剧集台账全程留痕
- LLM 通道：抽象接口 + 环境变量配置（OSCA_LLM_URL / MODEL / API_KEY，OpenAI-compatible
  线协议，温度恒 0），不锁定厂商、配置永不进包；mock:// 固件执行器供测试与演练
- `osca replay`：单条判断 A/B 体检——发布凭据第三样「可回放」的完整体
- SPEC v0.4 草案增补：触发原语受限语法、组合语义、运行时求值参考语义、剧集执行参考语义、
  settle 受限形式、回放机器判据

## [Review 修复 · M2 收口] - 2026-07-11
- SPEC v0.4 §8：Object 第五型 `kind: objective` 收编词表——修复 optimizer/settle 对合法包
  不可达的规范矛盾（此前 objective 包必被 lint 拒绝）；样例包新增 OBJ-003（带 settle 对账声明），
  闭环对账当场可演
- 归属契约（M2→M3 口径）：剧集提示词要求依据命中判断的段落在段末标注判断 ID（SPEC v0.4 §5）——
  采集器按段落去留计 confirmed/overruled 由此有了输入，trust 才升得上去
- Loader：runtime 契约校验——format_version 支持集 + requires.runtime 受限形式 `>=x.y[.z]`，
  不满足或不可解析即拒绝装载
- 并发落账：新增 `osca_cli.ledger`（case 编号 O_EXCL 原子分配 + 包级写锁 flock）——
  对账器 / 采集器 / 拍板并发写账绝不同号覆盖
- Policy/Connector 笼子收口：写接口审批门接线（默认拒绝、token 一次性消费、step=None 内部调用
  不豁免）；binding 按包隔离、卸载即清理；包停触达在途剧集（步间取消点 + 调用全拒）；
  kill switch 每次唤醒前按现账本重算；max_tool_calls 受限记法解析；剧集执行异常兜底终态、
  不再永久 running；enable 幂等（不重复布防）
- lint 收紧：OSCA030 证据限定包内存在的 C-xxxx；OSCA031 自指与成环取代链报错；
  OSCA040 objective 必填 optimize
- osca pack/load 安全：符号链接拒绝进包（防宿主机文件泄入交付件）；zip 解压三重上限
  （成员数 / 单成员 / 总解压量，zip bomb 防护）
- CI 与 pre-commit：脱敏内容扫描加 `-i`——大小写变体不再绕过门禁
- 文档：README（中英）、CONTRIBUTING 状态修正（M2 七组件齐 + replay，SPEC v0.3 定稿）

## [Review 复核 · 二轮] - 2026-07-11
- Host 原子发布：policy/proxy/gate 全部构建成功才进注册表——运行时构建失败不再留半注册包；
  kill switch 阈值伪数字（如 `.`）按不可求值处理，不炸装载
- 长跑 Host 账本以磁盘为准：每次唤醒前刷新包内容 + 重建签名表 + 重算 kill switch——
  M3 拍板的新判断不重启即入检索
- lint：OSCA031 增加取代分叉检查（同一旧判断被多条判断取代即报错）
- osca pack：输出路径落在包内直接拒绝（防交付件被下次打包吞进自身、哈希漂移）

## [Review 复核 · 三轮] - 2026-07-11
- Host 热刷新入账本锁协议：唤醒前的快照刷新持账本写锁（非阻塞）——写入者事务进行中
  或磁盘账本不合规（lint 红灯）即拒绝本次唤醒、保留旧快照；读取/校验/重建索引/统计
  全部成功后才原子替换 `loaded.pack`（不用半截账本装配剧集）
- `osca_cli.ledger`：`ledger_lock` 增加非阻塞模式（`blocking=False` → `LedgerLockBusy`）；
  `rebuild_index` 可复用已解析包
- 发布与布防同生共死：`_load` 布防任一条失败即补偿回滚（撤 watcher + 清笼子/闸门/binding
  + 注销），不留半装载包；`TriggerTable.subscribe` 在 `_arm` 失败时撤掉空 watcher
- 签名表缓存形状校验：合法 YAML 但形状不对（顶层 list / entry 非 mapping）同样视为
  缓存损坏——oscapipe 检索重建不炸，Host 装配退化空桶（包才是真理）

## [Review 复核 · 四轮] - 2026-07-11
- 刷新安全边界收口：磁盘满/权限/索引重建失败等普通异常不再穿透 trigger 回调
  （穿透会杀死共享 watcher 循环）——记完整异常、拒绝本次唤醒，故障修复后自然恢复；
  TriggerTable 派发对订阅方异常各自隔离（`_fire` 逐个 try，人工发射转人话错误）
- 装配签名表与快照同源：`signature_entries` 上移公仓（rebuild_index 与 Host 共用），
  装配不再读磁盘缓存——坏缓存不可能把判断静默清空（fail-open），TOCTOU 窗口消除
- enable 补偿回滚：全部订阅成功才置 enabled，半路失败撤已布防部分、保持停用可重试
  ——不再留下「显示启用、实际半布防」且被幂等挡住的死角
- oscapipe 签名表形状校验补严：先取原值再验 `isinstance(list)`——
  `judgments: {}/""/0/false/null` 等 falsy 变体不再被吞成合法空表

## [Review 复核 · 五轮] - 2026-07-11
- lint 总函数化：包解析边界面对不可信 YAML 只报错、绝不崩溃——OSCA040 补齐全部
  嵌套 mapping/list 形状约束（examples/permissions/budget/gate/triggers/meta/replay/
  policy 各段/pipeline 步骤项），各规则自带类型防御（OSCA021/022/023/024/031/032/033
  不再假设别的规则先执行），`run_all` 兜底把规则异常转 ERROR finding；
  新增 YAML 类型变异矩阵测试（23 字段 × 5 形状断言不抛异常）
- kill switch 评估/发布分离：评估（纯计算）在刷新事务保护区内、发布（纯赋值）与
  `loaded.pack` 替换配对执行——pack 与 policy 同进退，评估异常保留旧快照旧状态，
  不存在半发布
- episode 模块文档同步四轮架构边界：装配签名表源自 loaded.pack，磁盘缓存只服务
  检索器与人工查看

## [Review 复核 · 六轮] - 2026-07-11
- Policy 叶子 schema：data.redact / egress.allow_domains / permissions[].allow 必须是
  字符串列表，permissions[] / approvals[] / kill_switch[] 元素必须是 mapping——
  `data.redact: 身份证号`（字符串）此前会**静默关闭脱敏**，现 lint 即 ERROR；
  PolicyInterceptor 自身同步自防（形状错误留审计警告、绝不静默改语义，不依赖 lint 先行）
- 序列元素与布尔计数：triggers[]/replay[]/negative[]/pipeline[] 非 mapping 元素即 ERROR
  （此前 `triggers: ["oops"]` 造出「显示启用、实际永不触发」的包）；meta 计数排除 bool
  （bool 是 int 子类，true/false 会污染 trust 与 kill switch 计数），ledger_stats 同步
- 诊断可定位：OSCA004/022/023/031 对 requires.bindings 非法形状、不可哈希 step/
  judgment_id 本地验型——报对应文件的正常 finding，不再退化为 run_all 兜底的「.」；
  变异矩阵按 GPT 建议收紧（叶子/元素/布尔各有必须 ERROR 的断言）

## [Review 复核 · 七轮] - 2026-07-12
- Policy fail-closed 定稿：安全段配置非法时保守默认朝安全侧倒——脱敏配置非法
  （形状/混合元素/未知类别）→ 启用全部已知脱敏类别（宁可多脱不可泄露）；预算非法或
  不可解析 → 额度撤销（0），不是无限额；kill_switch 形状非法（非 list/项非 mapping/
  when 非字符串）→ 配置错误即停机；审批配置非法 → 审批门一律拒绝（「不在清单放行」
  的口子同时关死）；混合列表不部分接受、整叶保守。SPEC v0.4 §4/§5 同步定稿
- lint 值级校验（OSCA040）：data.redact 限受支持类别枚举；kill_switch[].when 必须
  非空字符串；policy per_episode 与 aware budget 的预算键必须合数量记法（正整数，
  0/负数/unlimited 全拒）；permissions[].allow 必填（空列表也要显式）；pipeline
  步骤必须有非空字符串 step
- parse_quantity 上移 osca_cli.triggers（lint 与 Host 同源），并收紧为「正整数」

## [Host 0.1 · M3-W4 配套] - 2026-07-11
- kill switch 第二可求值形式「回放红灯率 > X%」（SPEC v0.4 §4）：数据源为回放器
  （M3 私仓 checkup）生成的健康档案缓存 `indexes/replay-health.json`（契约入规范）；
  档案缺失/损坏/越界 → 条件不生效留痕——数据可用性缺口走保守默认，与配置形状非法
  （fail-closed 停机）是两回事。样例包 policy 的「回放红灯率 > 20%」自此可真裁决

## [Review 复核 · 八轮] - 2026-07-12
- 脱敏正则边界修正：`\b` 在中文紧邻数字处无边界（中文与数字同属 \w），
  「手机号13812345678」会整条漏掉——改用数字负向断言，fail-closed 全开时不再漏
- 预算键按运行时真实契约拆分：Aware（max_steps/max_minutes/max_tokens）与
  Policy（max_tool_calls/max_tokens）各自受限、未知键报错——「声明了没人执行的
  硬顶」不再 0 错通过
- tokens 额度预检：零额度（含配置错误撤销的）在 llm.complete 之前拒绝——
  「额度撤销、任何调用即拒」真正成立；止损顶只管合法正数预算的超顶；
  runner 对绕过 lint 的非法 aware 预算同样撤销自防
- 自防与 lint 对齐：data 父段非法不再压成 {}（与「未声明」混同）→ 保守全开；
  kill_switch 空白 when 与 lint 同谓词 → 配置错误停机
- 审批配置损坏时 grant_approval 拒绝授予、status 明示 config_error/deny_all
  ——不再展示永不生效的 granted

## [Review 复核 · 九轮] - 2026-07-12
- authorize_llm 统一闸：每次 llm.complete 前查包停 + kill switch + tokens 额度——
  在途剧集对执行中途新触发的 kill switch 不再有豁免
- 健康档案作为安全信号的可信度收口（M3-W4 配套）：Host 校验完整契约
  （generated_by/at/model/ledger_head、非负整数计数、total 自洽、judgments 数量、
  red_rate 派生一致），任一不过按档案不可用；可判 0（green+red==0）= unavailable，
  不作 0% 红灯；git 根上 ledger_head ≠ 当前 HEAD 的旧档案不采信；
  判定改整数计数交叉相乘——四位小数派生值不再翻转严格 > 判定
- 预算键常量上移 osca_cli.triggers（lint 与 Host Policy/Runner 单一真理源）；
  运行时自防补齐：per_episode 出现跨层/未知键 → 额度撤销，aware.budget 出现
  跨层/未知键 → 拒绝执行
- 脱敏类别双份常量增加一致性锚测试（cli 枚举 vs host 正则表，漂移即红灯）
- SPEC v0.4 §4 健康档案契约同步定稿（ledger_head/unavailable/交叉相乘/原子发布）

## [Review 复核 · 十轮 · Host 0.2.0] - 2026-07-12
- 账本版本戳协议升级：`ledger_stamp`（包内容 git tree OID，子目录包不被无关提交
  作废）+ `ledger_dirty`（包范围干净区）上移 osca_cli.ledger——健康档案生产端与
  Host 消费端同一协议；档案字段 ledger_head → **ledger_tree**
- kill switch 三态（TRIPPED/CLEAR/UNAVAILABLE）：unavailable 保留既有安全状态
  ——已触发的红灯不被可用性缺口（档案缺失/账本前进/网关故障）清除，有可判数据
  证明健康才解除；重启即重评（持久化停机名单归部署侧，诚实标注）
- 健康档案消费端全 schema：judgments/red_rate 必填、逐项 light 枚举 + 非负整数
  assertions、灯色汇总与顶层计数对账、red_rate 有限且与计数一致；
  **非 git / git 失败 = 无法验证版本归属 → 不可用**（无法验证 ≠ 可以采信）
- Decimal 精确算术：阈值十进制 + 整数交叉相乘——18.4%×375 的二进制浮点误触发根除
- budgets 外层未知段（如 per_epiosde 拼写错误）：lint ERROR + 运行时额度撤销
- authorize_llm 三检入授权锁（与 revoke/kill 发布同一线性化边界）；permit 成功留审计痕
- Host 版本 0.1.0 → 0.2.0（replay_health 返回键与档案契约为破坏性变更）

## [Review 复核 · 十一轮] - 2026-07-12
- 体检快照真读：checkup 从 tree OID 导出不可变快照**全程回放快照**——盖章内容 =
  读取内容，「中途改写、事后恢复」的瞬时内容无从进入健康档案
- ledger_dirty 三漏修复：`--ignored=matching` 把 untracked/gitignored 全部纳入
  （loader 会读它们）；豁免收窄到**包根** indexes/（judgments/indexes/ 不豁免）；
  所有调用点显式区分 None（不可判定 = 不可信）与空（干净）
- settle 入账本锁协议 + mkstemp 原子落位——对账不再能在 checkup 锁内终检后偷写
- 锁文件移至 git common dir（按包路径哈希）：不随缓存目录删除重建产生第二个
  inode；O_NOFOLLOW 且不截断——预置符号链接即报错，不覆写链接目标
- ratio 条件三态：0/0 = unavailable（不解除既有红灯）；overruled>0 而 confirmed=0
  = 保守停机；lint 拒绝负计数
- authorize_tool/charge/precheck/approvals 全部进授权锁——工具决策与预算预留和
  revoke/kill 发布同一线性化边界，并发预算预留不超发
- 纯整数交叉相乘（as_integer_ratio）：28 位 Decimal 上下文的乘法舍入不再翻转
  严格 > 判定；red_rate 数百位大整数不再炸读取器（总函数契约）
- Host 装载提示部署契约：zip 形态（非 git 账本）下回放红灯率条件永远 unavailable
- 兼容：CheckupReport.ledger_head 弃用别名保留；oscapipe 版本 0.2.0；
  README/样例注释同步

## [Review 复核 · 十二轮] - 2026-07-12
- 快照物化协议定稿：弃用 git archive（嵌套包子树空 tar / .gitattributes
  export-ignore 可抹掉盖章文件 / 3.10-3.11 无保护解包），改 `git ls-tree -rz
  --full-tree` + `cat-file blob` 精确物化——只收普通 blob，符号链接/submodule/
  越界路径一律拒绝；快照内容恰是 tree 内容，不多、不少、不被解释
- None 显式拒绝补齐：checkup 锁内终检与 Host replay_health 消费端不再走
  truthiness——git index 损坏（dirty=None）不是「干净」；_git_out 捕获 OSError
- ledger_dirty 前缀归一化（-z + repo 相对路径）：嵌套包的包根 indexes/ 正确豁免
- 锁身份跨 worktree 稳定：哈希「git common dir 实路径 + 包 repo 相对路径」
- settle 无覆盖发布：临时 inode + fsync + os.link 落名、撞号顺移、目录 fsync
  ——零字节 C-xxxx.yaml 不再可见，绝不截断他人内容
- oscapipe __version__ 从包 metadata 派生（0.2.0），加一致性测试

## [Review 复核 · 十三轮] - 2026-07-12
- 安全目录发布助手（osca_cli.ledger）：`open_ledger_dir`（lstat 拒符号链接 +
  O_DIRECTORY|O_NOFOLLOW 持目录 fd）+ `publish_file_in_dir`（唯一临时名 O_EXCL →
  写满 fsync → link 无覆盖 / replace 覆盖 → 目录 fsync，全程 dir_fd）——
  `indexes/`/`cases/` 被换成外部目录链接（dirty 豁免包根缓存曾使其通过全部版本
  检查）也写不出包根；检查后目录项被替换只作用于已持有的真实 inode
- settle 与 checkup 发布路径接入同一助手；settle 编号扫描改 dir_fd listdir
- Host replay_health：stamp → dirty → stamp 三明治——dirty 检查期间 HEAD 原子
  前进到干净 tree 的竞态窗口封堵（两次戳必须一致）
- ledger_dirty：porcelain -z 的 rename/copy 双路径成对消费——根缓存内部 rename
  不再误报脏（fail-closed 可用性问题），任一段出豁免区仍算脏

## [Review 复核 · 十四轮] - 2026-07-12
- 安全目录发布助手包根 fd 锚定：`open_ledger_dir` 改两层 fd——先 O_DIRECTORY|
  O_NOFOLLOW 打开**包根**拿 root_fd（包根被换成符号链接在此即拒），再经
  dir_fd=root_fd 创建/打开发布目录；name 限单一目录名（路径分隔符与 ./.. 拒绝）。
  单层 O_NOFOLLOW 只护路径最后一段——包根这类祖先在检查后被换成外链仍可把发布
  导出包根（十四轮确定性交错探针）；checkup/settle 经同一助手一并继承。回归：
  包根预置外链拒绝 + 包根持 fd 后被替换写入仍落原 inode、包外零写入
- `publish_file_in_dir` 目录 fsync 移到临时名清理之后（占用/异常路径同样覆盖）
  ——崩溃恢复不再可能残留点号临时文件把账本判脏
- settle 撞号重试真分支回归：对手在编号扫描后、首次 link 前落号——首次发布
  返回占用、对手文件原样、顺移 C-0104 且 YAML 内 case_id 一致

## [M4-W0 · 控制通道安全内核] - 2026-07-12
Review M4 首轮（权限面）No-Go 四项 P1 + 协议加固收口；专家端/运营台/审批卡
（W1–W3）在安全内核复核通过后再开：
- 传输层：私有运行目录 0700 + socket 0600（umask 无关）、对端 uid 校验
  （SO_PEERCRED / LOCAL_PEERCRED，取不到凭据 fail-closed 拒绝）
- 实例 flock：同一 socket 路径只有一个 Host——活 socket 不可被第二实例接管；
  残留 socket 只在持锁后清理且必须真是 socket；关闭只删本实例创建的 inode
  （lstat 比对），不误删后来者入口
- Principal + Authorizer + CommandSchema（osca_host.authz）：token → Principal
  认证（sha256 存表；admin token 启动生成 0600，其余 principal 走部署者签发的
  principals 文件，权限过宽拒绝启动），角色能力矩阵在进入命令实现前裁决——
  host_admin 管生命周期但不可授予业务审批（approve 归 approver）；operator 只有
  脱敏快照/启停/发射/剧集摘要；expert 命令随 M4-W1 落地。矩阵以测试钉住
- load 的 confused-deputy 面收口：控制通道只收 deployment_id，包路径/bindings/
  解压目录一律由 Host 侧 --deployments 清单解析，请求内 path 类字段死于 schema
- 协议 v1 加固：顶层必须 mapping、字段白名单（多余/缺失即拒）、读超时、单行
  64 KiB 上限、并发连接上限、统一异常边界（error 码 + 人话 detail，不再有
  AttributeError/ValueError 空响应）；load 重活进线程、命令经锁串行，事件循环
  保持确定性响应
- 后续按序：W1 专家端 → W2 运营台 → W3 审批卡（持久化审批 challenge：绑定
  approver/episode/payload digest/expiry/nonce/幂等键）→ append-only 审计与
  shutdown draining

## [M4-W0.1 · 安全内核复核收口] - 2026-07-12
Review M4-W0 复核三条新 P1 + 审批面暂闭 + 凭据协议收紧：
- 信任模型两档诚实标注：开发模式（principal 无 uid，同 uid 可信，token 只防
  误用）/ 生产模式（principals 条目写 uid，principal 绑定 expected_uid + role +
  token 摘要；传输允许名单 = Host uid + 各 principal uid）——偷来的 token 换了
  进程身份即失效，被攻陷界面进程偷到 admin token 也当不了 admin
- 运行目录锚定：mkdir/chmod 跟随链接的面收口——os.mkdir 不跟随 + O_NOFOLLOW
  打开后对 fd fstat（属主校验）/fchmod；目录被换成外链即拒绝启动，外部目录
  权限零改动、零写入
- 启动 fail-closed 回滚：bind 后任一步失败 → 关监听器、删自己的 socket、再放
  实例锁——不留「无锁监听器」与后来实例并存
- 审批 RPC 暂闭：W3 challenge（pending→approved|denied→consumed，绑定
  approver/episode/payload digest/expiry/nonce）落地前 ROLE_CAPS["approver"]
  空集——旧 set[action] 无绑定授予面不从控制通道暴露；M2 语义留在 policy 内部
  接口，W3 以 challenge 状态机替换后再接审批卡
- 凭据读取协议：O_NOFOLLOW 打开 → 同一 fd fstat 验属主/普通文件/0600 以内/
  限长 → 从该 fd 读——无检查-读取替换窗口；已存在 admin token 权限过宽拒绝
  启动；轮换 = 换文件重启（诚实标注），在线撤销随 W3
- 锁粒度：load 全部重活（读盘/解压/lint/binding 读取/git 戳探测）锁外线程执行，
  _cmd_lock 只罩发布段——慢 load 不再压住 status/stop（回归钉住）
- 连接计数覆盖完整连接生命周期（含响应序列化与 drain）+ 响应大小上限 + 写超时
- 部署/principals 严格验型：字段须非空字符串（拒静默 str() 转换）、限长、拒
  控制字符；部署清单相对路径按清单文件所在目录解析；operator 快照未脱敏的
  现状在 README 诚实标注（脱敏 DTO 属 W2）

## [M4-W0.2 · 防御性安全修复] - 2026-07-12
- 显式双模式：开发 `0700/0600`；生产以 `--control-group` 验证专用 group 和既有
  `0710` 运行目录、发布 `0660` socket。group 只提供内核可达性，peer UID、token、
  expected_uid、role 继续逐层裁决；配置/权限错误 fail-closed，不自动降级
- 运行目录从根逐级 `openat`/`dir_fd + O_DIRECTORY + O_NOFOLLOW` 打开，最终 fd
  持有到完全关闭；token/principals/lock/清理均相对 fd。socket bind 前后复核父
  inode；生产路径祖先必须由 root/Host UID 持有、不可被 group/other 改名且允许目标
  group 遍历。启动失败和 shutdown 只删本实例保存的 socket inode
- 生产 principals 只收 `token_sha256 + uid`，客户端明文由对应 UID 的 0600 文件
  持有；凭据读取最多 `MAX+1`，principals YAML 错误归一化且不回显可能含 token 的行
- Host 生命周期显式化为 `STARTING/RUNNING/DRAINING/STOPPED`；load 按 deployment
  单飞并以 generation + package tombstone 线性化，stop/unload 胜过迟到发布，不同
  deployment 仍并行，status 不被慢准备阻塞；shutdown 跟踪并清退控制连接，启动
  阶段取消同样释放 runtime fd；取消判定保持 Python 3.10 兼容
- 部署清单拒绝必填 path 缺失/null/空串/控制字符，显式 null 的可选路径字段同样拒绝；
  deployments/principals 的 falsy 非容器顶层不再伪装成空配置，生产 principal 的 uid
  必须是非负整数且不可为 null

## [M4-W3.1 · 审批挑战状态机 + Review 收口] - 2026-07-18
- 绑定挑战替换旧无绑定 `set[action]` 授予：每次高危动作一台一次性状态机
  `pending → approved|denied → consumed`，绑定 approver（指定审批人且名相符）/
  episode_id（防跨剧集串用）/ payload sha256 摘要（防偷梁换柱）/ expiry（防陈旧
  授权），consume 即 consumed（防重放）——冒名/重放/偷梁换柱/跨剧集/过期各有测试钉住
- 控制通道接线：ROLE_CAPS["approver"] = {approve, deny, challenges}（绑
  challenge_id 批/驳一张具体挑战 + 看待批清单）；admin/operator/expert 均无审批面
  （矩阵双向断言）；policy.require_approval / require_write_approval 改带
  episode_id + payload，connector 写路径传入
- Review W3 收口：`consume_or_raise` 单锁原子——封死「consume 失败与 raise 之间
  恰好获批 → 同绑定长出第二张 pending → 双倍一次性放行额度」的竞态窗；终态挑战
  （consumed/denied/expired/revoked）超保留期（1h）惰性清出，store 不无限增长
  （审计真相在 policy.audit）；删除装饰性 nonce 字段（生成、存储但协议从未校验——
  防重放由状态机独担，文档与代码一致）；挑战级 revoke 状态机预留、控制通道命令
  待矩阵归属定夺后接线
- 交付限定（诚实标注，README「M4-W3 审批挑战」节）：机制完成，「批准 → 放行一次
  真写」端到端闭环待 M5/M6——真写执行未接入（payload 摘要恒空串摘要）、runner 无
  剧集内挂起等批（挑战绑 episode_id，重跑即新剧集，已批挑战等不到 consume）；
  接通时须一并落地剧集内等批重试、审批卡带人类可读脱敏 payload、TTL 按人审时延重估
- 外部审查补漏（GPT review）：approver 名绑定是全局的、无包域——同名审批人可批任何
  指定其名的包、challenges 不按审批人过滤；README 矩阵补多租户告示（与 expert 同款，
  包域收窄归 T1/T2）；挑战存储进程内随 Policy 同寿（包重载即清空 pending）同段明示

## [M4 · 三种界面 收官] - 2026-07-18
- 公仓侧全景：W0/W0.1/W0.2 控制通道安全内核（见上各条）→ W1 专家只读交付面
  （expert 角色 episodes 摘要 + episode 全量导出——draft 即交付物；episode 身份
  随交付面收口）→ W3.1/W3.1b 审批挑战状态机与 approver 命令面（见上条）
- 私仓侧（oscapipe，随行记录）：W1 双 IM 专家桥接（飞书卡片/企微文本，持久幂等）、
  W2 运营控制台 `oscapipe-console`（operator 一脸 + 管理层报告区A/C）、W3.2 IM 审批卡
  桥接（approver_im_id ≠ expert_im_id 职责分离）
- 收官口径（诚实标注）：**三种界面机制完成**；「批准 → 放行一次真写」闭环、审批卡
  人类可读 payload、TTL 人审时延重估三债归 M6 真写接通时一并落（host/README
  「M4-W3 审批挑战」节）；approver/expert 的 per-principal 包域收窄归 T1/T2 多租户

## [Unreleased]
- SPEC v0.4-draft §9：判断分层命名空间（commons 行业公共层 / org 企业私有层）与权属
  三字段（scope / provenance / classification）——权属血统无法事后重建，出生即标；
  洁净室规则（client-derived 永不静默进 commons、commons 必须无密级）；跨包限定引用
  语法 `<package_id>/<judgment_id>` 定稿（judgment_id 保持包内局部，ID 语法与
  OSCA010/011 纪律不动）；overrides（跨层遮蔽，≠supersedes）/ dependencies（判断库包
  锁版本+哈希）/ 条目级无内容遥测 属规划，仅钉语法
- lint 新增 OSCA060（共 23 条）：三字段缺失 warn（存量过渡）、枚举/形状非法 error、
  洁净室与无密级约束 error；样例包与测试黄金模板补三字段，23+6 用例
- 官网：开放段新增白皮书下载行（三语 PDF 自托管 `site/downloads/`，为国内受众可达性
  不走 GitHub 直链；**PDF 是 docs/ 拷贝，白皮书更新时须同步**）；信任段增第五条
  「方法有出处」（Klein CDM，五十年自然决策研究背书）
- 白皮书 v1.1 增补「方法论出处」小节（第 7 章，三语 md 同步）：Klein CDM/RPD 作为
  采集设计的认知科学出处——专家判断是模式识别、说不出规则（故归纳给 AI 拍板给人）、
  判断只在例外处显形（故只采纠错 Diff）、交付流里关键事件自然发生（故采集嵌交付而非
  回溯访谈）；五段式 ↔ CDM 认知成分对照表（预期是断言、案例是门禁）；结尾诚实标注
  「设计出处非效果证明，待 P0 检验」。**PDF 仍为 v1.1 审阅版快照未随行**（生成脚本
  不在仓内），下次导出时更新
- 发布 OSCA 开放规范白皮书 v1.0：以 OSCA 为核心、Oscaware 为参考实现，覆盖 O/S/C/A/J、
  双平面 Runtime、判断飞轮、采用路径、兼容与证据边界；历史 v0.1 扩展稿留档
- 白皮书 v1.0 新增 English / 日本語 完整译本；GitHub README 新增日本語版本，中英日三语
  README 与白皮书互相链接
- 白皮书 v1.1（最终审阅版）：对照公仓 CLI/Host、私仓蒸馏管道与内部构想逐条核对后收口——
  修正英日译本语义错误与漏译（Confirm 后的 J-0417 误标 Candidate 等）；五段式中生命周期
  状态归位为文件顶层字段；Reject 审计口径收紧为「尚未落地」；包布局图补 `indexes/` 缓存
  目录；补回「包与账本是源代码，模型与 Runtime 是可更换的编译器」核心类比；P0 判定条件
  改为清单式；文件更名 v1.0 → v1.1，三语 README 同步
- pre-commit 违禁词表移出公开脚本：此前拆词内嵌的词人工可还原（保护机制泄露保护对象），
  且只覆盖单一来源；现公开脚本只携带机制，词表读取本地未跟踪 `Core_docs/redlist.txt`
  （# 注释/空行忽略，缺文件跳过词检），路径拦截（Core_docs/、key.md、redlist.txt）始终生效
- Phase 0 内容线：P0-A 在高频真实场景形成 ≥20 条经专家 Confirm 入账的 Judgment，并观察
  后续独立使用；P0-B 慢场景单独报告，反哺 SPEC

## [SPEC v0.4 · M6] - 2026-07-19
- 定稿全文（并入 v0.3，§0–§14 + 附录 A/B/C/D），`format_version` 升 "0.4"，v0.4-draft 退休
- §4 Object 第五型 `kind: objective`；§7 Aware 受限触发语法（时长/schedule 结构化字段/watch/event，
  废止自由文本 schedule）+ 闸门编译期矛盾检查 + 组合语义定稿
- §9 判断分层命名空间与权属三字段（scope/provenance/classification）+ 洁净室规则 + 限定引用语法
  （`<package_id>/<judgment_id>`）；§10 case kind 词表收编 `引用`（公共标准编纂类判断的天然出生证据）
- 附录 A 运行时求值参考语义（precondition/emit_when/kill_switch 可求值形式 + 健康档案契约、
  performer 受限集 + 预算记法 + 剧集停三终态、settle 受限形式、回放机器判据）
- 附录 B 企业系统对接约定（Manifest/Binding/Impl 三层职责 + 执行器分派 + 真实执行器契约 +
  read-only enforcement + secret 解析 + 写路径挂起-等批-恢复消费语义）
- 附录 C 判断库包变体规范（`package_kind: library` / 库包免 pipeline / 抽象签名再绑定 rebind /
  Manifest dependencies 锁版本+完整性哈希 / 合并索引 layer 列 / overrides 跨层遮蔽 /
  无宿主 replay 退化判据）——**规范语义定稿，cli/host 实现推 Phase 1**

## [CLI · lint 24 条 + replay] - 2026-07-19
- OSCA060（判断分层权属三字段 + 洁净室机器布防，SPEC §9）、OSCA061（osca.yaml 包级 layering
  默认段校验，SPEC §1/§9，与 OSCA060 共用枚举/形状/洁净室判据）。共 24 条规则
- `osca replay <包> <J-id>`：单判断 A/B 体检——同一 case 情境跑注入/不注入两臂，机器判据
  `score = 相似度(产出, 改后) − 相似度(产出, 改前)`（模型无关、确定性）；样例 `osca replay J-0417` 2/2 绿灯

## [v1.0] - 2026-07-19 — 发布凭据三样齐
1.0 = **机制可验**（措辞纪律：机制口径，非效果证明；飞轮收敛曲线进 1.x 叙事，曲线出现前不写「已证明」）：
- **规范**：OSCA-SPEC v0.4 定稿全文（`docs/OSCA-SPEC-v0.4.md`，CC BY 4.0）
- **参考实现**：运行框架 Host（M2 七组件 + M4 控制通道安全内核 / 审批挑战机制，各带诚实限定）
  + CLI（lint / pack / load / replay，Apache-2.0）
- **可回放脱敏样例**：`examples/oper-diagnosis.osca`；`osca replay J-0417` 单条体检 2/2 绿灯
  （输出从改前移向改后）
- **限定（诚实标注）**：蒸馏管道 / Creator / 交互层闭源；判断库包变体实现推 Phase 1；真写全接通
  （真实 sql_readonly/openapi 执行器 + 审批闭环三债 + 可恢复剧集）作 M6-cont → v1.1；八步全链路
  演练走通的是**机制链路**（mock 连接器 + mock LLM），非业务效果

## [v1.0.1] - 2026-07-19 — GPT 外审收口（5 P1 + 2 P2）
- **检索硬过滤析取→合取（P1，跨仓同步）**：Host `retrieve_judgments` 与私仓检索器的签名硬过滤
  原为「aware 命中 或 object 命中」——错误 Aware + 正确 Object（或反之）也会注入，判断被照办
  到错误场景。收紧为合取（签名 = object × aware × guard，SPEC §11；调用方未给的维度作通配），
  补两个负向用例
- **OSCA060/061 分层枚举判定类型防御**：不可哈希叶子（list/mapping）不再退化成不指字段的
  「规则执行异常」（d370996，v1.0 后落）
- **文档同步（P2）**：SPEC v0.4 状态行去掉过时的「W1 / 附录 C 待 W3」；三语 README 目录树与
  状态节 v0.3+draft/22 条 → v0.4 定稿/24 条；CHANGELOG 去掉 Unreleased 里与已发布节重复的
  M6-W1 条目；三语白皮书里程碑表按 M6 (b) 裁决对齐（M4/M5 机制完成·私有，M6 机制集成完成，
  软件 v1.0=机制可验发布、真实内容门槛整体移 1.x 不删）。**白皮书 PDF/站点副本仍为旧快照**
  （生成脚本不在仓内，随行纪律见前），下次导出时更新
- **CI 门禁实绿**：`ruff format` 全仓收口（cli/host 此前 3 文件漂移）
- 私仓/集成工程随行（另仓提交）：capture 聚类键改锚裁决工件、Creator policy 白名单接缝修复、
  CDM boundary 企微生产路径接入、Creator→pack→load 真实接缝测试、联调基线 BASELINE.txt

## [GPT 三审收口] - 2026-07-19
- **SPEC §11 guard 契约精确化（P1 裁定）**：硬过滤定稿为 **object × aware 合取**（确定性）；
  guard 明确**不参与硬过滤**——自由文本「可求值风格」无受限求值语法（附录 A 显式不含），其变量
  （连接器数据）在装配时刻尚未绑定；随判断注入后由模型应用、回放判据事后体检。guard 受限语法与
  检索前确定性求值属后续版本（机器布防不了的语法不进确定性契约）。Host/私仓检索器 docstring 同步
- **三语白皮书正文全面清扫（P2）**：头部规范基线 v0.3+draft→v0.4 定稿、参考实现状态刷新
  （Host M2+M4 / M3–M5 机制完成·私有 / v1.0 机制可验）、速览表与附录导航 v0.4 为当前定稿、
  第 10 章 22→24 条、第 11 章「v0.4 仍是草案」→已定稿；里程碑历史行（M1 交付 v0.3/22 条）保留。
  PDF/站点副本仍为旧快照（生成脚本不在仓内），下次导出更新
- 私仓/联调仓随行（另仓提交）：否决记忆封蒸馏死循环、裁决工件忠于 episode 快照、
  联调仓 format 实绿 + BASELINE 脏树标记

## [GPT 四审收口] - 2026-07-19
- **guard 裁定接进提示词契约（P1）**：装配注入的判断改称「候选判断（guard 未判定）」，提示词
  新增「判断应用纪律」——模型应用前须按本次情境逐条判定 guard，不命中/无法判断即不得应用、
  不得标注 ID（归属计数不被未判定注入污染）；步骤提示同步。SPEC §11 补提示词契约句，测试锁行为
- **文档口径终扫（P2）**：SPEC §9 guard「可求值谓词」→「可求值风格」自由文本（无受限语法，
  不参与硬过滤）；lint 清单「可求值性留给 M2」→ §11 定稿口径；episode 模块头「Aware 或 Object」
  → 合取；英文白皮书导航 v0.3 Stable → v0.4 定稿为当前（上轮替换串不匹配静默漏改，本轮验证命中）
- 私仓/联调仓随行（另仓提交）：否决记忆改内容指纹＋原子写＋锁＋unreject、快照缺 ref 不读磁盘、
  BASELINE 升级为门禁（脏树失败 + verify-baseline 前后复核）

## [GPT 五审收口] - 2026-07-19（私仓/联调随行记录）
- 私仓：否决记忆补**包域**（侧车条目带 package_id；rejected_digests/unreject 按包过滤——多包
  共享队列不串台）；reject 补归属与证据验证（跨包候选拒绝处理；指纹对不上当前账本的 stale 候选
  出队不记忆——伪造摘要进不了封簇面）；confirm 入账前查否决 tombstone（reject 崩在「记忆已写、
  候选未删」窗口时残留候选不得入账——专家否决不可静默反转，误否走 unreject 显式解除）；
  record_rejection 幂等（崩溃重试不重复记）；快照存在性按**字段是否携带**判定（"structure": null/
  坏形状同样不回读磁盘）。公仓本轮无代码变更

## [GPT 六审收口] - 2026-07-19（私仓随行记录）
- 私仓：reject 与 confirm **同持包级账本锁**（候选装载均在锁内）——封「confirm 查完 tombstone、
  reject 插队否决、confirm 继续立法」的 TOCTOU（入账+否决同真）；confirm 抢先后 reject 报
  「候选不存在」，绝不给已立法候选补 tombstone。tombstone 身份键统一为
  **(package_id, candidate_id, cases_sha256) 三元组**——蒸馏跳过/幂等写入/confirm 拒绝三处同语义：
  --batch 复用产同名新内容候选时，新内容的否决必被记录（旧口径按 id 幂等会静默吞掉）、
  旧 tombstone 不误拦新内容入账（内容变化即解封契约贯通）。公仓本轮无代码变更

## [GPT 七审收口] - 2026-07-19（私仓随行记录）
- 私仓：候选发布补两条终局纪律——①**原子发布**（tmp+fsync+replace，对齐否决侧车/BridgeStore）：
  桥接轮询与 queue 读方绝不见半截候选 YAML，崩在 replace 前只留 .tmp、重跑蒸馏即补发；
  ②**候选不可变身份**：同名候选仍在队列（未拍板）时不覆盖——确认卡按 id 指路、专家按当时内容
  拍板，静默换内容等于让专家批没看过的东西；跳过留痕，拍板/否决出队后同名方可再发布。
  两条各配崩溃窗口/覆盖拒绝回归测试。公仓本轮无代码变更

## [GPT 八审收口] - 2026-07-19（私仓随行记录）
- 私仓：候选不可变身份补齐**全生命周期**——①桥接身份对账：确认卡记录与判断 meta 各带
  cases_sha256（meta 新增 candidate_digest），轮询去重键改 (id, 指纹)：出队终局后同名**新内容**
  候选重新发卡（旧终局是旧身份的终局）；handle_confirm/reject 先对账「卡片身份 vs 队列身份」，
  不符即拒（专家批的必须是看过的那份）；账本对账恢复分支同样按指纹——旧判断不再把同名新候选
  误报「已入账」；②双发布者竞争：_publish_candidate 改进程唯一 tmp + os.link 独占占位——
  exists 预检只是快路径、link 才是裁决，同 ID 并发发布只有一个赢、目标绝无混写。公仓无代码变更

## [GPT 九审收口] - 2026-07-19（私仓随行记录）
- 私仓：候选身份升级 **candidate_revision**（哈希整份候选：问句/草案/证据清单/notes，规范化 dump）
  ——证据指纹 cases_sha256 区分不了「同 id 同证据、不同归纳」的两份候选（LLM 重归纳），旧卡能批
  到专家没看过的判断文本。桥接去重/身份守卫/账本对账（判断 meta.candidate_revision）/confirm
  tombstone 全部改按 (package_id, candidate_id, revision)；cases_sha256 只留给蒸馏封簇（同证据
  不再骚扰专家——那是对证据的裁决）。旧格式候选以 cases_sha256 作代理身份（surrogate）。
  公仓本轮无代码变更

## [GPT 十审收口] - 2026-07-19（私仓随行记录）
- 私仓：revision 补齐**动作链路与消费端**两个缺口——①消费端不信自报摘要：revision_of 一律按
  内容**重算**（文件里的 candidate_revision 字段只是给人看的落款）——改内容不改落款/伪造落款
  都改变不了身份判定；②动作携带版本：确认卡按钮值带 candidate_revision（专家点的是这张卡），
  daemon→handler→confirm/reject 核心逐层透传 expected_revision，与队列当前重算 revision 不符
  即拒（轮询重发卡后专家点旧卡的窗口由此关死——记录守卫在该窗口会误放行）；CLI 补 --revision、
  queue 展示版本。公仓本轮无代码变更

## [GPT 十一审收口] - 2026-07-19（私仓随行记录）
- 私仓：无 revision 的生产入口与升级兼容全收口——①企微文本入口带版本：卡片指令含 @rev
  （「回复：确认 D-x@<rev>」照卡片原样回复即携带身份），路由解析进动作 value，与按钮同走
  版本守卫（@rev 只随确认/否）；②CLI confirm/reject 的 --revision 改**必填**（生产动作必须
  钉版本，queue 展示版本供取用）；③升级兼容不破不变量：无版本的存量记录（升级前旧卡）在
  候选在队时一律拒动作、等轮询按新版补发；给定 revision 对账时无落款的存量判断不再豁免匹配
  （不借升级短路成「已入账」）；升级前否决条目（无 revision）按 cases_sha256 兼容拦截
  （否决不可反转不因字段升级失效）。公仓本轮无代码变更

## [GPT 十二审收口] - 2026-07-19（私仓随行记录）
- 私仓：企微最后一个无版本状态转换入口关死——裸「确认/否 D-x」不再派发动作，人话回「需带
  卡片版本，照确认卡指令原样回复（确认 D-x@<版本>）」；与 CLI --revision 必填同律。HELP 与
  卡片边界提示同步 @版本；访谈中裸确认/否仍逃逸捕获（回人话而非被折成钩子答案）。
  公仓本轮无代码变更

## [v1.0.2] - 2026-07-19 — GPT 外审十轮收口补丁（v1.0.1 后全部修复）
自 v1.0.1 起经 GPT 三审至十三审共十轮对抗审查，全部 P1 收口（外审确认「无 P1 + 全门禁绿」）：
- **公仓**：SPEC §11 guard 契约定稿（硬过滤=object×aware 合取，guard 注入后模型逐条判定 +
  回放判据体检；提示词契约入 SPEC 并落 Host 提示词与测试）；文档口径全同步（SPEC §9/lint
  清单/episode 模块头/三语 README 与白皮书正文/导航）
- **私仓（随行记录，代码在 oscapipe）**：候选身份体系从零建到闭合——candidate_revision
  完整候选内容哈希、消费端一律重算、三入口（飞书按钮/企微 @rev/CLI --revision）强制携带、
  confirm/reject 同持账本锁串行终局并锁内验版、否决记忆（包域+内容指纹+tombstone 三元组+
  原子侧车+unreject 救济）、候选原子发布（os.link 单赢家）、出队后同名新候选必重发卡、
  升级兼容一律 fail-closed；capture 聚类键忠于剧集快照；蒸馏死循环封死
- **联调仓**：Creator→pack→load 真实接缝（钓出 policy 白名单接缝洞并修复）、BASELINE
  门禁 + verify-baseline 前后复核
- **留债（P2，明标不冒充已解）**：①同 scope 无关主题的**拆簇机制**（否决记忆只防重复骚扰，
  不产生更细的簇）——归 1.x；②白皮书 **PDF/站点副本为旧快照**（生成脚本不在仓内，正文
  md 已同步）——下次导出时更新

## [Host M6-W5 · 真写机制地基] - 2026-07-20
真写全接通（v1.1 块）的地基三片，每片对抗审查（合计逼出 blocker×3 + major×8，全真跑复现→修→补测）
+ pytest / ruff（check + format）全绿后推公仓：
- **D1 传写 params**（`0e8c521`）：connector 写路径把模型写 params 传入，payload 摘要绑**真实被写内容**
  （终结空串摘要）；写门两条 fail-closed（空内容 / 非 JSON 可序列化拒）；内置 mock 写执行器（演练用，
  真实 `_execute_real` 仍桩）。
- **D2a 可恢复剧集 L1**（`8df9333`）：写命中审批门 → 剧集**挂起**（`suspended_pending_approval`，释放线程）
  → approve/deny 经控制通道触发**恢复**（同一剧集内消费兑现 / 回落保守默认）；「决定先到、登记后到」丢唤醒
  窗由登记侧复查自愈 + 惰性清扫双堵；恢复用 consume-only（防过期竞态再挂新卡）；在途/挂起剧集免台账淘汰。
- **D2b 可恢复剧集 L2**（`7e38f88`）：挂起快照原子写盘（fd 锚定运行目录）+ 装载时重挂——活过**包重载**且
  活过 **Host 重启**（同一路径，operation_id 真键 + 重挂重编展示号）；版本戳按源文件内容指纹（捕获未提交
  改动，漂移即 fail-closed 丢弃）；删盘早于写执行 → 关 reload/restart 双写窗，残留真·硬件半写归 W6 幂等键。
- **措辞纪律**：mock 写执行器落地 = 审批闭环**机制通、非真实系统写验证**。真实 sql_readonly/openapi 执行器
  + secret 解析 + 审批卡人类可读脱敏 payload + TTL 按人审重估 = **W6**；真实写连接器样例 + 端到端演练收口 +
  tag v1.1 = **W7**。GPT 外审 4×P1 + 2×P2 收口（版本戳内容指纹防未提交漂移 / 删盘失败保挂起不假 running /
  真重启回归测试 / 文档同步）。

## [Host M6-W6-1 · 审批授权 TTL 可配] - 2026-07-20
真写「变真」块（W6）第一片 TTL 重估（配置面，强测试 + 自审；W6-D0 六决策全过红笔按推荐）：
- **TTL 从硬编码变 policy 可配**：授权过期窗口不再固定 `DEFAULT_TTL_SECONDS=300`——policy.yaml 顶层
  `default_ttl_seconds`（包级默认）+ 每 `approvals` 项 `ttl_seconds`（每动作覆盖）。装载时包级默认进
  `ChallengeStore`，每动作覆盖在挂挑战时按调用传入（`consume_or_raise` 既有 per-call ttl）。
- **fail-closed（不 fail-open 成无过期）**：缺省/非法（非数 / bool / 非有限 / ≤0 / 巨值溢出 float）一律
  回落机制默认 300s 并记审计警告；非法每动作 TTL 只警告 + 回落包默认，**不** broken 审批门（与 approvals
  项本身形状错区分）。`float(巨整数)` 溢出被拦——绝不让 `now + inf` 退化成永不过期。
- **公仓 osca-cli lint 校验新字段**（决策⑥）：OSCA040 认 `default_ttl_seconds` 与每项 `ttl_seconds` 须正
  有限数（秒），形状错误在装载前挡（policy 是笼子）；lint 合法判定与 host `policy._parse_ttl` 逐条一致。
  样例包 `oper-diagnosis.osca` 增 TTL 配置示例（default 900s + 该动作 1800s）。
- **诚实标注**：参考默认 300s 只是占位口径——**真实人审节奏仍由部署侧按 IM 实况设**。
- 门禁全绿：host 261 passed + cli 146 passed，ruff check + format 双绿。红笔纠偏：`default_ttl_seconds`
  **不能**塞进 approvals list（list 且 lint/host 要求每项含 action+approver，塞进去会触发审批门 fail-closed
  全拒）——故包级默认另起顶层键。W6 余片：secret 解析（W6-2）/ 真实执行器（W6-3）/ 人类可读 payload（W6-4）。

## [Host M6-W6-2 · secret 解析（凭据面）] - 2026-07-20
真写「变真」块（W6）第二片 secret 解析（高危凭据面，六 lens 对抗审查 → 逐条真跑核实 → 收口）：
- **可插拔 SecretResolver**：新模块 `secret_resolver.py`——`SecretResolver` 协议 + 参考实现 `EnvVarSecretResolver`
  （`secret_ref` 作环境变量名）；`ConnectorProxy` 构造可注入（默认 env-var，部署侧可换 file/vault/callable）。
- **接线**：`_execute_real` 在 egress 之后做 secret 前置——binding 声明了 `secret_ref` 就必须解析出非空值，
  否则 fail-closed；resolver 返回值即用即弃（真实执行器随 W6-3 接入才把值传入建连接）。修 `ENDPOINT_HOST`
  正则 `[a-z+]→[a-z+_]`：否则 `sql_readonly://`（含下划线）主机名抽取落空、egress 永远拒、secret 前置不可达。
- **三不（SPEC B.3）**：secret 值永不进包（binding 只有名字）/ 永不进日志（error/audit 只带 secret_ref 名）/
  永不进剧集上下文·回执；哨兵值回归测试锁死「值不进回执/审计」。
- **对抗审查捉 2 真缺陷（各由 2 个 lens 独立命中、真跑复现后修）**：① fail-closed 强制点原只判 `is None`，
  但协议允许返回空串——注入型 resolver 返回 `""` 会绕过闸门（W6-3 拿空串建连接=fail-open）；改 `not resolve(...)`，
  空串与 None 同 fail-closed，不信任 resolver 自律归一。② resolver 抛异常未捕获 → `call()` 崩溃（破坏「恒回
  Receipt」），且异常内文可能含连接串被 host `log.exception` 写进日志（踩穿三不）；加 try/except → fail-closed，
  错误串**绝不带异常内文**。另 3 lens（egress 顺序 / 正则安全 / 注入路径）真跑后无真问题。
- **立身口径**：W6-2 建 resolver + 契约强制，测 fake（env 注入 / 桩 resolver）；真实 secret manager 取值连通
  归部署侧（1.1/部署验收）。门禁全绿：host 273 passed（+2 对抗审查回归）+ ruff check/format 双绿。真实执行器（W6-3）。

## [Host M6-W6-3 · 真实执行器] - 2026-07-20
真写「变真」块（W6）第三片真实执行器（最高危真读/写路径 + 凭据；七 lens 对抗审查 → 逐条真跑核实 → 收口）：
- **可插拔 Executor 分派**：新 `executor.py`——`Executor` 协议 + 内置参考适配器 `SqlReadonlyExecutor`（sqlite）
  / `OpenapiExecutor`（urllib）+ `default_executors()`。`_execute_real` 按 endpoint scheme 分派（egress → secret
  前置 → 执行器）；`ConnectorProxy` 可注入执行器注册表（部署侧覆盖生产驱动）。未注册 scheme / mcp（预留不实现）
  一律 fail-closed，不猜、不兜底。
- **sql_readonly**：只读靠**连接模式**（`mode=ro`，非关键字黑名单）；跑包内固化 impl SQL、params 参数化命名
  绑定（防注入）；impl 缺失/写路径/多语句一律拒。**openapi**：method+path+params，secret 作 Bearer 头，**不跟随
  重定向**（防 SSRF 绕 egress），响应非 2xx/非 JSON/截断/超限 fail-closed。
- **对抗审查捉 7 缺陷（11 raw→7 confirmed，各真跑复现后修），收敛为 2 类根因：**
  ① **执行器炸穿 call()**（sqlite3.Warning 多语句 / http.client 截断响应 / MemoryError 巨响应体 / 注入驱动意外
  异常）——违反「call() 恒回 Receipt」。修：`_execute_real` 分派处统一 try/except 兜成 fail-closed 回执（异常内文
  绝不入串，守三不）+ 执行器各自补捕获 + 响应体读上限。
  ② **BLOCKER openapi SSRF + secret 外泄**——URL=netloc+manifest path，path 无前导 `/` 时向右延展 netloc、把真实
  连接引到 egress 从未校验的主机并送 secret Bearer。修：path 强制锚定 `/`（`"/"+lstrip("/")`），host 永不被 path
  污染（7 种逃逸 path 真跑复核全落在 netloc host）。另修 `urlparse` 对含下划线 scheme（sql_readonly）静默失效
  → 自持 `_split_endpoint` 手工切。
- **立身口径**：内置参考适配器测 **fake 后端**（本地 sqlite / 本地 http.server）；生产库/生产 API 真系统验证归
  部署侧（1.1/部署验收）。门禁全绿：host 295 passed（+对抗审查回归：多语句/注入异常无泄漏/SSRF 锚定/截断/超限）
  + ruff check/format 双绿。W6 末片：人类可读 payload（W6-4，跨仓 host+oscapipe）。

## [M6-W6-4 · 审批卡人类可读脱敏 payload（跨仓 host + oscapipe）] - 2026-07-20
真写「变真」块（W6）末片，还清审批卡橡皮图章设计债（五 lens 对抗审查 → 逐条真跑核实 → 收口）：
- **host（公仓）**：`Challenge` 新增 `payload_display` = `policy.redact(原始 params)`（PII 已抹的脱敏视图），
  随 `public()` DTO + L2 快照（asdict/重挂）跟随；`payload_digest` 仍绑**原始** params（防偷梁换柱、写执行器
  写原文，不变）。`redact` 递归**含 dict 键**（键位 PII 不漏进人审卡面）；数字型标量刻意不脱（对裸整数脱敏会
  误伤合法大额金额、既污染读回执又对审批人隐藏真实写值——PII 规范以字符串承载）。
- **oscapipe（私仓）**：`notices.approval_notice` 呈现脱敏写内容原文供人拍板（digest 降为技术核对小字）；渲染
  叠显示安全——键/值**同包 inline code span** 中和 markdown（假批准链接/伪权威粗体）注入、总长截断**丢整行**
  （不按字符切断、绝不留半截 code span）+ 字段/总长上限。
- **对抗审查捉 9 缺陷（10 raw→9 confirmed，各真跑复现后修），收敛为 3 类根因：** ① dict **键**未包 code span
  → 键内 markdown 假链接/粗体注入误导审批人（4 lens 独立命中）；② 总长截断按字符切断可切掉值 code span 闭反引号
  → 重新激活注入；③ `redact` 不脱 dict 键 → 键位 PII 漏进卡片。修：键值同包 span + 截断丢整行 + redact 脱键。
- **立身口径**：脱敏在 host 侧、桥接只呈现再叠显示安全；诚实标注。门禁全绿：host 299 passed + oscapipe 332 passed
  （+对抗审查回归：键注入中和/截断不断 span/键位 PII 脱敏）+ 两仓 ruff check/format 双绿。**W6 四片全收（TTL /
  secret / 真实执行器 / 人类可读 payload）；真实写连接器语义化样例 + 端到端演练 + tag v1.1 属 W7。**

## [M6-W6 · GPT 复审收口（原 8 条已闭合，补 3 残留）] - 2026-07-20
GPT 复审确认原 8 条在内置执行器主路径已闭合，对抗复测捉 3 残留（均在上一轮我的修复里），真跑复现后修 + 回归（host 310 绿）：
- **[major] secret 清洗漏 executor error 与 tuple payload**：`_execute_real` 只抹 payload——可插拔驱动回 `error=
  "...TOKEN..."` 时 TOKEN 进 `Receipt.error`；payload 里的 tuple 也没递归。修：error 也抹 + `_scrub_secret` 支持 tuple。
- **[general] secret 抹成键时再次碰撞丢字段**：`{"TOKEN":"A","***secret已脱敏***":"B"}` 抹后塌成一个。修：`_scrub_secret`
  dict 分支加碰撞后缀消歧（与 `policy.redact` 同口径），保序保全字段。
- **[general] SQLite 授权器误拒合法递归 CTE**：`WITH RECURSIVE … SELECT` 实测 DatabaseError（普通 SELECT/CTE/UNION/
  子查询/窗口/聚合均通过）。修：授权器放行 `SQLITE_RECURSIVE`（只读、不开写；VACUUM/ATTACH/写仍拒，已回归复核）。
- 门禁全绿：host 310 passed（+error 抹/tuple/键碰撞/递归 CTE 回归）+ ruff check/format 双绿。两 blocker 在默认执行链确认已关。

## [M6-W6 · GPT 外审收口（2 blocker + 6 major + 1 general 全修）] - 2026-07-20
GPT 外审（范围 `e48c42c..a0a02f0`）判「暂不建议发布」，8 条全部真跑复现确认后逐条修 + 回归（host 308 + cli 149 绿）：
- **[blocker] 写审批被 HTTP method 绕过**：读连接器（`write: forbidden`，无审批门）+ 接口 `method: POST/DELETE` →
  openapi 执行器真写、绕审批门。修：执行器强制读路径只允 GET/HEAD（写 method fail-closed）+ cli lint 挡 forbidden
  连接器的写 method。
- **[blocker] egress 校验主机 ≠ 实连主机**：`openapi://allowed@evil/x` 正则校验 allowed、urllib 实连 evil、secret
  送错家。修：endpoint authority 含 userinfo（`@`）一律拒（凭据走 secret_ref 不入 URL；egress 与实连主机一致）。
- **[major] `mode=ro` 非完整只读沙箱**：单条 `VACUUM INTO` / `ATTACH DATABASE` 实测能建新文件。修：加 SQLite
  **authorizer**，执行面收窄到 SELECT/READ/FUNCTION，ATTACH/VACUUM/写/PRAGMA 一律 DENY（授权器 + 连接模式双闸）。
- **[major] 空 `secret_ref` 绕 fail-closed**：`secret_ref: "" / 0 / false` 按「无需凭据」放行。修：区分「键不存在=
  无凭据」与「键存在但空/非法=误配→拒」。
- **[major] 反射型 API 回显 secret 进回执/剧集**：远端回显 Authorization/token → secret 进 payload（PII 脱敏认不出）。
  修：connector 层用**本次** secret 值抹回执（键与值同抹——自审补漏：secret 可被回显成 JSON 键）。
- **[major] `openapi://` 明文发 Bearer**：修：携带 secret 走非 https 且非本地回环 → fail-closed（生产须 https）。
- **[major] 脱敏后 dict 键碰撞静默丢字段**：两不同 PII 键脱成同一标记 → 塌成一个（读回执/审批展示丢字段）。修：
  碰撞加稳定后缀，保序保全字段（此前 W6-4 曾「接受碰撞」，现按外审properly 修）。
- **[general] 重复 action 残留上一条 TTL**：修：解析每项前先清旧 TTL 覆盖（非法/缺省回落包默认）+ cli lint 禁重复 action。
- **诚实标注**：均测 fake 后端（本地 sqlite/http.server/桩），生产真系统验证仍归部署侧（1.1）。门禁全绿：host 308
  passed + cli 149 passed（+8 条外审回归 + 授权器/method/userinfo/scrub 自审探测）+ 两仓 ruff check/format 双绿。

## [Host M6-W7 · 真写全接通收口（一等写样例 + 端到端演练 + 文档/基线）] - 2026-07-21
真写「变真」块收官：把 W6 真实执行器**端到端串起来**演练、写路径整备成包自带一等样例、文档/基线同步——
真写全接通**机制完成**（tag v1.1 = 发布门，先过目再推，非本轮）：
- **W7-1 真实写连接器语义化样例**（公仓 `180316d`）：新增 `examples/oper-dispatch.osca`（示例经营处置下发，
  通用名）——CON-202 `openapi` / `write: allowed_with_approval`、写接口 `method: POST` + `path`；policy 写审批
  `action` **逐字 == 写接口 ref** + approver + TTL；bindings 只占位 + secret_ref 名；param-less 固化 SQL（绕开
  runner 读步命名 params 穿透历史缺口）。写路径成**包自带一等样例**，不再演练里运行时 patch。
- **新 lint OSCA025**：写连接器（allowed_with_approval）每个写接口 ref 必须在 policy.approvals 声明 approver——
  运行时写审批按 ref 查 approver，缺失即默认拒绝（写路径静默死而其余规则全绿）。把这条 lint 无覆盖的头号坑机器化。
- **W7-2 真写审批端到端演练**（独立集成工程）：`test_write_approval.py` 从 mock 写执行器升级到**真实执行器打
  fake 后端**——真实 sql_readonly 读本地 fake sqlite + 真实 openapi 写打本地 fake http.server，走完挂起-approve
  （底层入口）-恢复-真写落地；被写内容验证搬 fake server 侧捕获（真实执行器回执无 mock 的 landed/applied 键）。
  **对抗审查（6 lens mutation 实证）**：核心安全属性全 load-bearing、非虚假通过；补洞含**恢复路径偷梁换柱拒绝**
  （批 A、恢复前换 B → consume digest 不符回落不写 + 授权未被消费，两仓此前均未覆盖）+ deny 零请求 / secret
  反射清洗 / egress-SSRF 外呼前拦。
- **W7-3 文档 + 基线同步**：host/README 修**头号硬矛盾**（旧「`_execute_real` 返回未接入/是桩」——W6-3 早已落，
  改为「真实执行器落地」）+「真实系统写待续」节改题「真写全接通机制完成」；集成工程 README 加真写端到端步、
  `BASELINE.txt` 刷 pre-W6 → post-W7（verify-baseline 前后绿）。SPEC 零改——B.3/B.4 机制语义 W6 已成文，格式
  规范不载状态。
- **立身口径（写死 harness 断言 + 本条）**：真写全接通 = **审批闭环 + 真实执行器机制通（测 fake 后端：本地
  sqlite / http.server）——非真实系统写验证**。生产库 / 生产 API / 生产 secret manager 的真连通与真写落地仍归
  **部署侧（1.1 / 部署验收）**。tag v1.1 = 真写全接通**机制**完成里程碑、非生产发布验收；发布 = 外部动作，先过目再推。
- 门禁全绿：公仓 cli 151 passed + host 310 passed；独立集成工程 9 passed（含对抗审查补洞）；三处 ruff check/format 全绿。
