<div align="center">
  <img src="https://avatars.githubusercontent.com/OSCAware" width="88" alt="OSCA" />
  <h1>OSCA</h1>
  <p><b>An open specification for AI cognitive workflows — defined in plain text, evolving through human feedback.</b></p>
  <p><sub><b>English</b> · <a href="README.zh-CN.md">简体中文</a></sub></p>
</div>

---

OSCA is an open specification for defining AI cognitive workflows in plain text — workflows that run
deterministically, keep humans at the decision points, and evolve through human feedback recorded in an
attributable **contribution ledger**.

## What Oscaware is

> **Oscaware is a definition for AI cognitive workflows (OSCA), a contribution ledger continuously supplied by human experts, and a distillation mechanism that turns human feedback into workflow adjustments at runtime.**

Formula: **Oscaware ＝ OSCA definition ＋ Contribution ledger ＋ Distillation**

## Four words first

| Word | Meaning |
|---|---|
| **Feedback** | A correction or confirmation a person gives at a key point. E.g. a store manager says "these two go back to the supplier." This is the human's part. |
| **Contribution ledger** | The reusable entries that feedback settles into. This is the asset left behind — every entry can be attributed. |
| **Judgment** | The ledger entry's name in the technical spec (`J-xxxx` files). |
| **Evolve** | A neutral word: no promise it gets smarter or better, only that it stays a better fit — always aligned with the environment as it is now. |

In one line: **feedback is what a person does, the ledger is the asset left behind, distillation is the step in between.**

## One example to grasp the ledger

Supermarket fresh food: this month 10 new SKUs arrive; by default the system routes them into near-expiry
discounting and past-expiry write-off, and confirms with the store manager. The manager says: no, 2 of these
should go back to the supplier. Once that feedback is recorded, next month those 2 SKUs auto-route to supplier
return.

**One piece of feedback changed the workflow itself** — a knowledge base is something you look up; a ledger acts on its own.

A ledger entry looks like this (from the [sample pack](examples/oper-diagnosis.osca/judgments/J-0417.yaml)):

```yaml
judgment_id: J-0417
signature:
  object: OBJ-002                  # what it applies to
  guard: "费用科目 == 差旅费 && 环比涨幅 > 30 && 检修期上下文 != null"
body: |
  差旅费异动若与该单位检修计划期重叠，视为正常波动，正文不报——
  除非涨幅同时超过该单位近三年检修期同科目峰值。
evidence: [C-0091, C-0094]         # origin evidence: the expert's original edit diff
meta: {author: 王工, confirmed: 6, overruled: 0, trust: high}
```

## The four questions of OSCA

| Letter | Question |
|---|---|
| **O** — Object | What is this job trying to achieve? |
| **S** — Structure | How many steps? |
| **C** — Connector | Where does the data come from, and who gets the result? |
| **A** — Aware | When does it act? |

On top of those four answers sits a ledger that vets them. One agent ＝ one `.osca` folder ＝ one git repo, pure
Markdown + YAML — printable, signable, deliverable.

## Repository layout

```
osca/
├── docs/OSCA-SPEC-v0.3.md         # the specification (CC BY 4.0); v0.4 draft alongside, v0.2 kept for history
├── docs/OSCA-LINT-RULES.md        # lint rule catalogue (ledger discipline, machine-enforced)
├── examples/oper-diagnosis.osca/  # a full de-identified sample pack (with supersedes chains and spoken-language cases)
├── cli/                           # osca lint / pack / load (all three working)
├── host/                          # runtime host reference implementation (in progress: six of seven components done, settle/replay next)
├── site/                          # oscaware.com single-page source
├── CONTRIBUTING.md                # how to take part, pre-1.0
└── CHANGELOG.md
```

## Status & roadmap

- Now: **SPEC v0.3** + a full sample pack + the CLI trio (`lint / pack / load`).
  The sample pack passes all 22 lint rules; deliverables pack reproducibly and verify integrity.
- In progress: the runtime host (reference implementation) — loading, triggers, gates, episode assembly, policy enforcement.
- The bar for 1.0: spec + reference implementation + one replayable de-identified sample ledger — mechanism complete, verifiable on the spot.

## Taking part

For spec discussion, open an issue (framed as "scenario → expectation → relevant spec section"). PRs are on
hold for now; see [CONTRIBUTING](CONTRIBUTING.md).

## License & trademark

- Code & samples: [Apache-2.0](LICENSE)
- Spec text (`docs/`): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- The names and marks "Oscaware" and "OSCA" are not covered by the licenses above; describing compatibility (e.g. "OSCA-compatible") is fair use — other uses require separate permission.

---

<div align="center">Website: <a href="https://oscaware.com">oscaware.com</a></div>
