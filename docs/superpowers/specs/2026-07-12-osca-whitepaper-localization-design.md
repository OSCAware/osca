# OSCA 白皮书三语本地化设计

## 目标

以 `docs/OSCA-WHITEPAPER-v1.0.zh-CN.md` 为唯一内容母版，发布语义一致的中文、英文和日文
白皮书，并让 GitHub 根 README 同样具备中、英、日三种版本与互相可发现的入口。

## 交付文件

| 语言 | README | 白皮书 |
|---|---|---|
| 简体中文 | `README.zh-CN.md` | `docs/OSCA-WHITEPAPER-v1.0.zh-CN.md` |
| English | `README.md` | `docs/OSCA-WHITEPAPER-v1.0.en.md` |
| 日本語 | `README.ja.md` | `docs/OSCA-WHITEPAPER-v1.0.ja.md` |

三份 README 顶部互链，并各自提供三种白皮书链接。仓库结构和状态段同步列出三语文档，
不让任一语言版本暗示软件 1.0、P0 或第一方私有能力已经完成真实验证。

## 翻译规则

1. 中文 v1.0 是事实与章节结构的唯一母版；英日版本不得自行增加产品承诺。
2. OSCA、Oscaware、Object、Structure、Connector、Aware、Judgment、Case、Candidate、
   Runtime、Episode、Policy、Replay、Checkup、Confirm、Supersedes 等规范术语保留英文原名，
   首次出现时用当地语言解释。
3. 文件名、命令、环境变量、ID、YAML 字段、Git 提交号和 Mermaid 节点语义保持不变；示例
   中的中文业务字段不强行改写，以免与公开样例包失去对应。
4. 保留中文母版的证据边界：合成演示不算 P0，公开 CLI/Host 与私有 `oscapipe` 分开，
   白皮书 1.0 不等于软件 1.0。
5. 日文采用技术文档常用的简洁书面语，避免逐字直译；英文采用开放规范/工程白皮书语气。

## README 结构

三种 README 使用相同信息顺序：

1. 语言切换；
2. OSCA 一句话定义；
3. 三语白皮书入口；
4. OSCA 与 Oscaware；
5. 核心术语和示例；
6. O/S/C/A/J；
7. 仓库结构；
8. 当前状态、参与、许可。

`README.md` 继续作为 GitHub 默认首页；`README.zh-CN.md` 和 `README.ja.md` 由顶部链接进入。

## 验证与发布

- 对三份白皮书检查 12 章结构、Mermaid/代码块数量、关键术语和本地链接；
- 对三份 README 检查语言互链、白皮书互链和状态口径；
- 运行 `git diff --check`、公开样例 Lint，以及现有 CLI/Host 测试；
- 明确暂存本次本地化文件，提交到 `codex/osca-whitepaper`；
- 使用 GitHub HTTPS 地址单独推送该分支，避免 `origin` 的第二个 push URL 同步到 ECS；
- 向 GitHub `main` 创建单一 Draft PR，不直接合并。

## 成功标准

GitHub PR 中可以从任一 README 一步切换到另两种 README，也可以一步打开任一语言白皮书；
三份白皮书的核心主张、章节、状态快照和证据边界一致，自动检查通过，且没有向 GitHub 之外
的远端执行推送。
