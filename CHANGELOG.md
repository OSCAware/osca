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

## [Unreleased]
- 运行框架（参考实现）：装载、触发表、闸门、剧集装配器、Policy 拦截器
- Phase 0 内容线：首个真实场景 ≥20 条账本条目，反哺 SPEC
