<div align="center">
  <img src="https://avatars.githubusercontent.com/OSCAware" width="88" alt="OSCA" />
  <h1>OSCA</h1>
  <p><b>用文本定义、随人的反馈进化的 AI 认知工作流程规范。</b></p>
  <p><sub><a href="README.md">English</a> · <b>简体中文</b> · <a href="README.ja.md">日本語</a></sub></p>
</div>

---

OSCA 是一套用纯文本定义 AI 认知工作流程的开放规范——控制平面确定性运行，在关键决策点保留人类判断，并随人的反馈不断进化；反馈被记入一本可署名的**贡献账本**。

📘 **白皮书：** [English](docs/OSCA-WHITEPAPER-v1.1.en.md) ·
[简体中文](docs/OSCA-WHITEPAPER-v1.1.zh-CN.md) · [日本語](docs/OSCA-WHITEPAPER-v1.1.ja.md) ·
[English PDF](docs/OSCA-WHITEPAPER-v1.1.en.pdf) · [中文 PDF 下载](docs/OSCA-WHITEPAPER-v1.1.zh-CN.pdf) ·
[日本語 PDF](docs/OSCA-WHITEPAPER-v1.1.ja.pdf)——覆盖设计动机、O/S/C/A/J、Runtime、反馈飞轮，
以及如何开始实现自己的 OSCA Agent。白皮书 v1.1 是文档版本，不等于软件 1.0 已发布。

## OSCA 与 Oscaware

> **OSCA 是开放规范；Oscaware 是它的参考工具、Runtime 与第一方反馈飞轮实现。**

第三方可以只按 OSCA 创建 `.osca` 包，也可以实现以所声明 OSCA Profile 为目标的 Runtime
和反馈飞轮；当前兼容性属于自声明，不是认证。采用规范不要求依赖 Oscaware 的私有组件。

## 先认识四个词

| 词 | 意思 |
|---|---|
| **反馈** | 人在关键位置给出的纠正或确认。例：店长说「这两个品要退回供货商」。这是人做的事。 |
| **贡献账本** | 反馈被整理后沉淀下来的一条条可复用条目。这是留下来的资产，每条可署名。 |
| **判断** | 账本条目在技术规范里的名字（judgment，`J-xxxx` 文件）。 |
| **进化** | 中性词：不承诺越用越好，只承诺越用越顺手——始终贴合当下的环境。 |

一句话记：**反馈是人做的事，账本是留下的资产，蒸馏是中间那道工序。**

可观测 Outcome 是第二条证据通道：它可以形成 Case，但不能自动立法。

## 一个例子看懂账本

超市生鲜：这个月新上 10 个品，系统按常规把它们排进临期打折和过期报废流程。店长说：不对，
其中 2 个要退回供货商。反馈先成为 Case；相似 Case 被 AI 归纳成 Candidate；有权专家 Confirm
Candidate 后才形成正式 Judgment。下次满足相同条件的品才进入退供流程。

**一条反馈，改了流程本身**——知识库是 Agent 去查它；判断账本在适用时主动进入本次运行。

账本条目长这样（摘自[样例包](examples/oper-diagnosis.osca/judgments/J-0417.yaml)）：

```yaml
judgment_id: J-0417
signature:
  object: OBJ-002                  # 对什么生效
  guard: "费用科目 == 差旅费 && 环比涨幅 > 30 && 检修期上下文 != null"
body: |
  差旅费异动若与该单位检修计划期重叠，视为正常波动，正文不报——
  除非涨幅同时超过该单位近三年检修期同科目峰值。
evidence: [C-0091, C-0094]         # 演示出生证据：合成专家改稿 diff
meta: {author: 王工, confirmed: 6, overruled: 0, trust: high}
```

上面是使用化名标签的合成演示条目。Case/Judgment 文件只是格式和机制夹具，不属于真实业务
验证（P0）证据；Lint 能检查引用纪律，不能证明证据真实。

## OSCA Agent 的五层问题

| 字母 | 问题 |
|---|---|
| **O** — Object 目标 | 这件活要达成什么？ |
| **S** — Structure 步骤 | 分几步走？ |
| **C** — Connector 接口 | 数据从哪来、结果给谁？ |
| **A** — Aware 时机 | 什么时候动手？ |
| **J** — Judgment 判断 | 经专家 Confirm 入账的裁决何时生效？ |

O/S/C/A 定义稳定骨架，J 保存随现场变化的裁决。一个 Agent 是一个以 Markdown + YAML 为主的 `.osca` 文件夹，可由 Git 管理、机器校验、生成稳定校验和并交付。

## 仓库结构

```
osca/
├── docs/OSCA-WHITEPAPER-v1.1.en.md    # 开放规范白皮书：English
├── docs/OSCA-WHITEPAPER-v1.1.zh-CN.md # 开放规范白皮书：简体中文
├── docs/OSCA-WHITEPAPER-v1.1.ja.md    # 开放规范白皮书：日本語
├── docs/OSCA-WHITEPAPER-v1.1.en.pdf    # 英文白皮书 PDF 下载版
├── docs/OSCA-WHITEPAPER-v1.1.zh-CN.pdf # 中文白皮书 PDF 下载版
├── docs/OSCA-WHITEPAPER-v1.1.ja.pdf    # 日文白皮书 PDF 下载版
├── docs/OSCA-SPEC-v0.3.md        # 规范正文（CC BY 4.0）；v0.4 草案与历史版本 v0.2 同目录
├── docs/OSCA-LINT-RULES.md       # lint 规则清单（账本纪律的机器化）
├── examples/oper-diagnosis.osca/  # 完整合成演示包（含 supersedes 链与口述 case）
├── cli/                           # osca lint / pack / load / replay
├── host/                          # 运行框架 Host 参考实现（M2 收官：七组件齐 + 剧集执行器）
├── site/                          # oscaware.com 单页源文件
├── CONTRIBUTING.md                # pre-1.0 参与方式
└── CHANGELOG.md
```

## 状态与路线

- **公开实现/测试可复现**：SPEC v0.3（＋ v0.4 草案）、合成演示包、CLI 和 Host 均已公开；
  fresh clone 可复现 Lint/Pack/Load 与自动测试。实际 Replay 需要 LLM/mock；完整 Episode 还
  需要 Binding、Connector fixture/Executor 与 LLM。样例通过全部 22 条 lint 规则；这不等于
  企业环境已经接通。
- **第一方反馈飞轮工程闭环（M3）**：Capture → Distill → Candidate Queue → Confirm/Reject → Git
  Judgment Ledger → Index → Retrieve → Checkup 已在私有实现中完成，并由合成夹具与自动测试
  支持；公众目前不能独立复核，也不等于真实业务验证。
- **下一道内容闸门**：同一高频真实场景形成 ≥20 条经专家 Confirm 入账的 Judgment，并观察
  其中一部分在后续独立批次再次适用、得到支持；月度慢场景单独报告、不得混池。公开样例与
  合成夹具不计数，其后再推进产品界面、Creator 与生产集成。
- **软件 1.0 发布门槛**：完成上述真实内容证据、产品界面、Creator 与生产集成，并交付规范、
  参考实现和一个可回放的受控真实样例账本；不要求公开客户原始账本。上面的“白皮书 v1.1”
  只是文档版本。

## 参与

规范讨论请提 issue（建议按「场景 → 期望 → 对应规范章节」描述）；PR 暂缓，详见 [CONTRIBUTING](CONTRIBUTING.md)。

## 许可与商标

- 代码与样例：[Apache-2.0](LICENSE)
- 规范文本（`docs/`）：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh)
- 「Oscaware」与「OSCA」的名称及标识不在上述许可范围内；描述兼容性（如「兼容 OSCA 规范」）属合理使用，其余使用需另行授权。

---

<div align="center">官网：<a href="https://oscaware.com">oscaware.com</a></div>
