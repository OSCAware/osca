<div align="center">
  <img src="https://avatars.githubusercontent.com/OSCAware" width="88" alt="OSCA" />
  <h1>OSCA</h1>
  <p><b>用文本定义、随人的反馈进化的 AI 认知工作流程规范。</b></p>
  <p><sub><a href="README.md">English</a> · <b>简体中文</b></sub></p>
</div>

---

OSCA 是一套用纯文本定义 AI 认知工作流程的开放规范——这些流程确定性地运行，在关键决策点保留人类判断，并随人的反馈不断进化；反馈被记入一本可署名的**贡献账本**。

## Oscaware 是什么

> **Oscaware 是一套 AI 认知工作流程的定义（OSCA）、一本由人类专家持续供给的贡献账本，和一套在运行中把人的反馈变成流程调整的蒸馏机制。**

公式版：**Oscaware ＝ OSCA 定义 ＋ 贡献账本 ＋ 蒸馏机制**

## 先认识四个词

| 词 | 意思 |
|---|---|
| **反馈** | 人在关键位置给出的纠正或确认。例：店长说「这两个品要退回供货商」。这是人做的事。 |
| **贡献账本** | 反馈被整理后沉淀下来的一条条可复用条目。这是留下来的资产，每条可署名。 |
| **判断** | 账本条目在技术规范里的名字（judgment，`J-xxxx` 文件）。 |
| **进化** | 中性词：不承诺越用越好，只承诺越用越顺手——始终贴合当下的环境。 |

一句话记：**反馈是人做的事，账本是留下的资产，蒸馏是中间那道工序。**

## 一个例子看懂账本

超市生鲜：这个月新上 10 个品，系统按常规把它们排进临期打折和过期报废流程，并向店长确认。店长说：不对，其中 2 个要退回供货商。这条反馈入账之后，下个月这 2 个品自动走退供流程。

**一条反馈，改了流程本身**——知识库是你去查它；账本是它自己生效。

账本条目长这样（摘自[样例包](examples/oper-diagnosis.osca/judgments/J-0417.yaml)）：

```yaml
judgment_id: J-0417
signature:
  object: OBJ-002                  # 对什么生效
  guard: "费用科目 == 差旅费 && 环比涨幅 > 30 && 检修期上下文 != null"
body: |
  差旅费异动若与该单位检修计划期重叠，视为正常波动，正文不报——
  除非涨幅同时超过该单位近三年检修期同科目峰值。
evidence: [C-0091, C-0094]         # 出生证据：专家改稿的原始 diff
meta: {author: 王工, confirmed: 6, overruled: 0, trust: high}
```

## OSCA 的四个问题

| 字母 | 问题 |
|---|---|
| **O** — Object 目标 | 这件活要达成什么？ |
| **S** — Structure 步骤 | 分几步走？ |
| **C** — Connector 接口 | 数据从哪来、结果给谁？ |
| **A** — Aware 时机 | 什么时候动手？ |

四个答案之上，压一层账本来把关。一个 agent ＝ 一个 `.osca` 文件夹 ＝ 一个 git 仓库，纯 Markdown + YAML，可打印、可签名、可交付。

## 仓库结构

```
osca/
├── docs/OSCA-SPEC-v0.3.md        # 规范正文（CC BY 4.0）；v0.4 草案与历史版本 v0.2 同目录
├── docs/OSCA-LINT-RULES.md       # lint 规则清单（账本纪律的机器化）
├── examples/oper-diagnosis.osca/  # 完整脱敏样例包（含 supersedes 链与口述 case）
├── cli/                           # osca lint / pack / load（三件套可用）
├── host/                          # 运行框架 Host 参考实现（进行中：装载/触发表/闸门/剧集装配已通，Policy 拦截在路上）
├── site/                          # oscaware.com 单页源文件
├── CONTRIBUTING.md                # pre-1.0 参与方式
└── CHANGELOG.md
```

## 状态与路线

- 当前：**SPEC v0.3** ＋ 完整样例包 ＋ CLI 三件套（`lint / pack / load`）。
  样例包通过全部 22 条 lint 规则；交付件可复现打包、可校验完整性。
- 进行中：运行框架（参考实现）——装载、触发表、闸门、剧集装配、Policy 拦截。
- 1.0 的发布凭据：规范 ＋ 参考实现 ＋ 一个可回放的脱敏样例账本——机制完整、当场可验。

## 参与

规范讨论请提 issue（建议按「场景 → 期望 → 对应规范章节」描述）；PR 暂缓，详见 [CONTRIBUTING](CONTRIBUTING.md)。

## 许可与商标

- 代码与样例：[Apache-2.0](LICENSE)
- 规范文本（`docs/`）：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh)
- 「Oscaware」与「OSCA」的名称及标识不在上述许可范围内；描述兼容性（如「兼容 OSCA 规范」）属合理使用，其余使用需另行授权。

---

<div align="center">官网：<a href="https://oscaware.com">oscaware.com</a></div>
