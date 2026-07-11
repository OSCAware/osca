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

## [Unreleased]
- Phase 0 内容线：首个真实场景 ≥20 条账本条目，反哺 SPEC
