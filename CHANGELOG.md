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

## [Unreleased]
- Phase 0 内容线：首个真实场景 ≥20 条账本条目，反哺 SPEC
