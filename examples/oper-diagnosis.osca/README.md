# 样例包：示例集团 · 经营诊断（oper-diagnosis.osca）

一个完整、可读、脱敏的 `.osca` 包。人物与单位均为化名，数据为演示值。

## 这个包演示了什么

- **双面 Aware**：`aware/AW-001` 的 trigger 段给运行时编译，discretion 段给剧集注入
- **薄 Structure**：`structure.yaml` 只描述「什么喂给什么」，不写 if/else——想写的那一刻，它就是一条该进账本的判断
- **确定性 Connector**：`connectors/CON-001` 的「拉取检修计划期」接口带 `born_reason`——接口是被判断层倒逼长出来的
- **正判断**：`judgments/J-0417`（trust: high 的来路：confirmed ≥5 且 0 推翻）
- **负判断**：`judgments/J-0423`（压制噪音的判断与正判断同权）
- **supersedes 链**：`J-0405（superseded）→ J-0423`——推翻不删除，留档可回放
- **证据两物种与口述 case**：`cases/C-0079`（kind: 口述）与 C-0088 / C-0091 / C-0094（专家 diff）
- **回放断言**：每条判断自带单元测试（`replay` 段）

## 建议阅读顺序

`AGENT.md` → `structure.yaml` → `aware/` → `objects/` → `judgments/`（沿 J-0405 → J-0423 的取代链读）→ `cases/`

## 备注

- `indexes/` 由 commit 钩子生成，不入库（`.gitignore` 已排除）
- 真实 binding 永不进包；见 `bindings.example.yaml` 的占位模板
