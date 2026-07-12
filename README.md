<div align="center">
  <img src="https://avatars.githubusercontent.com/OSCAware" width="88" alt="OSCA" />
  <h1>OSCA</h1>
  <p><b>An open specification for AI cognitive workflows — defined in plain text, evolving through human feedback.</b></p>
  <p><sub><b>English</b> · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a></sub></p>
</div>

---

OSCA is an open specification for defining AI cognitive workflows in plain text — workflows whose control
plane runs deterministically, that keep humans at the decision points, and that evolve through human feedback
recorded in an attributable **contribution ledger**.

📘 **Whitepaper:** [English](docs/OSCA-WHITEPAPER-v1.1.en.md) ·
[简体中文](docs/OSCA-WHITEPAPER-v1.1.zh-CN.md) · [日本語](docs/OSCA-WHITEPAPER-v1.1.ja.md) — the design
rationale, O/S/C/A/J model, Runtime, feedback flywheel, and how to begin implementing your own OSCA Agent.
Whitepaper v1.0 is not a software 1.0 release.

## OSCA and Oscaware

> **OSCA is the open specification; Oscaware is its reference tooling, runtime, and first-party feedback-flywheel implementation.**

Third parties can create `.osca` packages from the specification alone, or build a runtime and feedback flywheel
against a declared OSCA Profile. Compatibility is currently self-declared, not certified. Adopting OSCA does
not require Oscaware's private components.

## Four words first

| Word | Meaning |
|---|---|
| **Feedback** | A correction or confirmation a person gives at a key point. E.g. a store manager says "these two go back to the supplier." This is the human's part. |
| **Contribution ledger** | The reusable entries that feedback settles into. This is the asset left behind — every entry can be attributed. |
| **Judgment** | The ledger entry's name in the technical spec (`J-xxxx` files). |
| **Evolve** | A neutral word: no promise it gets smarter or better, only that it stays a better fit — always aligned with the environment as it is now. |

In one line: **feedback is what a person does, the ledger is the asset left behind, distillation is the step in between.**

An observable Outcome is the second evidence channel: it may become a Case, but it cannot legislate automatically.

## One example to grasp the ledger

Supermarket fresh food: this month 10 new SKUs arrive; by default the system routes them into near-expiry
discounting and past-expiry write-off. The manager says: no, 2 should go back to the supplier. The feedback
first becomes a Case; AI distills similar Cases into a Candidate; only after an authorized expert Confirms that
Candidate does it become a formal Judgment. Future SKUs under the same conditions then enter the return path.

**One piece of feedback changed the workflow itself** — an Agent looks up a knowledge base; an applicable Judgment is actively brought into the current run.

A ledger entry looks like this (from the [sample pack](examples/oper-diagnosis.osca/judgments/J-0417.yaml)):

```yaml
judgment_id: J-0417
signature:
  object: OBJ-002                  # what it applies to
  guard: "费用科目 == 差旅费 && 环比涨幅 > 30 && 检修期上下文 != null"
body: |
  差旅费异动若与该单位检修计划期重叠，视为正常波动，正文不报——
  除非涨幅同时超过该单位近三年检修期同科目峰值。
evidence: [C-0091, C-0094]         # demo origin evidence: synthetic expert-edit diffs
meta: {author: 王工, confirmed: 6, overruled: 0, trust: high}
```

The entry above is a synthetic demonstration using pseudonymous labels. Its Case/Judgment files are format and
mechanism fixtures, not real-business P0 evidence; Lint can check reference discipline, not prove evidence is real.

## The five layers of an OSCA Agent

| Letter | Question |
|---|---|
| **O** — Object | What is this job trying to achieve? |
| **S** — Structure | How many steps? |
| **C** — Connector | Where does the data come from, and who gets the result? |
| **A** — Aware | When does it act? |
| **J** — Judgment | When does a decision entered through expert Confirm apply? |

O/S/C/A define the stable skeleton; J stores decisions that change with the operating context. One Agent is a
mostly Markdown + YAML `.osca` folder that Git can manage, machines can validate, and tooling can checksum and deliver.

## Repository layout

```
osca/
├── docs/OSCA-WHITEPAPER-v1.1.en.md    # open-spec whitepaper: English
├── docs/OSCA-WHITEPAPER-v1.1.zh-CN.md # open-spec whitepaper: Simplified Chinese
├── docs/OSCA-WHITEPAPER-v1.1.ja.md    # open-spec whitepaper: Japanese
├── docs/OSCA-SPEC-v0.3.md         # the specification (CC BY 4.0); v0.4 draft alongside, v0.2 kept for history
├── docs/OSCA-LINT-RULES.md        # lint rule catalogue (ledger discipline, machine-enforced)
├── examples/oper-diagnosis.osca/  # a full synthetic demo pack (with supersedes chains and spoken-language cases)
├── cli/                           # osca lint / pack / load / replay
├── host/                          # runtime host reference implementation (M2 complete: all seven components + episode runner)
├── site/                          # oscaware.com single-page source
├── CONTRIBUTING.md                # how to take part, pre-1.0
└── CHANGELOG.md
```

## Status & roadmap

- **Public implementation/tests are reproducible**: SPEC v0.3 (+ v0.4 draft), the synthetic demo, CLI, and Host
  are public; a fresh clone can reproduce Lint/Pack/Load and the automated tests. Actual Replay needs an LLM/mock;
  a full Episode also needs Bindings, a Connector fixture/Executor, and an LLM. The demo passes all 22 lint rules;
  that does not mean an enterprise environment is connected.
- **First-party feedback flywheel engineering loop (M3)**: Capture → Distill → Candidate Queue →
  Confirm/Reject → Git Judgment Ledger → Index → Retrieve → Checkup is complete in the private implementation
  and supported by synthetic fixtures and tests. The public cannot independently inspect it, and this is not
  real-world validation.
- **Next content gate**: one high-frequency real scenario must produce ≥20 Judgments entered through expert
  Confirm, with some later reapplying and receiving support in independent batches. The monthly slow scenario
  is reported separately and never pooled. Public fixtures do not count; product interfaces, Creator, and
  production integration follow.
- **Software 1.0 release bar**: complete that real-content evidence, product interfaces, Creator, and production
  integration, then ship the specification, reference implementation, and a replayable controlled real sample
  ledger. Customer raw ledgers need not be public.
  “Whitepaper v1.0” above is only a document version.

## Taking part

For spec discussion, open an issue (framed as "scenario → expectation → relevant spec section"). PRs are on
hold for now; see [CONTRIBUTING](CONTRIBUTING.md).

## License & trademark

- Code & samples: [Apache-2.0](LICENSE)
- Spec text (`docs/`): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- The names and marks "Oscaware" and "OSCA" are not covered by the licenses above; describing compatibility (e.g. "OSCA-compatible") is fair use — other uses require separate permission.

---

<div align="center">Website: <a href="https://oscaware.com">oscaware.com</a></div>
