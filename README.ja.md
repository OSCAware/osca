<div align="center">
  <img src="https://avatars.githubusercontent.com/OSCAware" width="88" alt="OSCA" />
  <h1>OSCA</h1>
  <p><b>Plain Text で定義し、人のフィードバックを通じて進化する AI 認知ワークフロー仕様。</b></p>
  <p><sub><a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a> · <b>日本語</b></sub></p>
</div>

---

OSCA は AI 認知ワークフローを Plain Text で定義するオープン仕様である。Control plane は
決定論的に動作し、重要な判断点には人を残す。人のフィードバックは、帰属可能な
**Contribution Ledger** に記録され、Workflow の進化に使われる。

📘 **ホワイトペーパー：** [English](docs/OSCA-WHITEPAPER-v1.1.en.md) ·
[简体中文](docs/OSCA-WHITEPAPER-v1.1.zh-CN.md) · [日本語](docs/OSCA-WHITEPAPER-v1.1.ja.md) ·
[English PDF](docs/OSCA-WHITEPAPER-v1.1.en.pdf) · [中文 PDF](docs/OSCA-WHITEPAPER-v1.1.zh-CN.pdf) ·
[日本語 PDF](docs/OSCA-WHITEPAPER-v1.1.ja.pdf) —
設計思想、O/S/C/A/J、Runtime、フィードバック・フライホイール、自分の OSCA Agent を
実装し始める方法を説明する。ホワイトペーパー v1.1 は文書版。ソフトウェアは独立採番で現在 **v1.1**（メカニズム
検証可能なリリース：仕様 v0.4 ＋ 参照 Runtime ＋ Replay 可能な匿名化 Sample、さらに真書き込み経路の
エンドツーエンド接続：承認ループ ＋ 実 sql_readonly/openapi Executor、fake バックエンドで検証）を `v1.1` tag で
付与、実業務・本番書き込み検証は 1.x の道のり。

## OSCA と Oscaware

> **OSCA はオープン仕様であり、Oscaware は参照 Tool、Runtime、ファーストパーティのフィードバック・フライホイール実装である。**

第三者は OSCA 仕様だけで `.osca` Package を作成でき、宣言した OSCA Profile に基づく
Runtime やフライホイールを独自実装できる。現時点の互換性は自己宣言であり、認証ではない。
OSCA の採用に Oscaware の非公開 Component は必須ではない。

## 最初に四つの言葉

| 用語 | 意味 |
|---|---|
| **Feedback** | 重要な位置で人が行う修正または確認。例：「この 2 商品は Supplier Return にする」。 |
| **Contribution Ledger** | Feedback から整理された再利用可能な Entry。帰属可能な組織資産。 |
| **Judgment** | Ledger Entry の仕様上の名称。`J-xxxx` File。 |
| **Evolution** | 中立語。使うほど優秀になるとは約束せず、現在の環境へ合わせ続ける能力を表す。 |

一言でいえば、**Feedback は人の行為、Ledger は残る資産、Distillation はその間の工程**である。

観測可能な Outcome は第二の Evidence 経路である。Case にはなれるが、自動的に立法はしない。

## Ledger を理解する例

生鮮売場に今月 10 商品が追加され、System は通常どおり値引き・廃棄工程へ送った。店長は
「2 商品は Supplier Return にする」と修正した。Feedback はまず Case になり、AI が類似
Case を Candidate に Distill する。権限を持つ専門家が Candidate を Confirm した後でだけ
正式 Judgment となり、次回、同条件の商品が Return 経路へ進む。

**一件の Feedback が Workflow 自体を変えた。** Knowledge Base は Agent が検索するが、
適用可能な Judgment は実行へ入ってくる。

Ledger Entry の例（[公開 Sample](examples/oper-diagnosis.osca/judgments/J-0417.yaml)）：

```yaml
judgment_id: J-0417
signature:
  object: OBJ-002                  # 適用対象
  guard: "費用科目 == 差旅費 && 环比涨幅 > 30 && 检修期上下文 != null"
body: |
  差旅费异动若与该单位检修计划期重叠，视为正常波动，正文不报——
  除非涨幅同时超过该单位近三年检修期同科目峰值。
evidence: [C-0091, C-0094]         # 合成 Expert Edit の Demo Evidence
meta: {author: 王工, confirmed: 6, overruled: 0, trust: high}
```

これは仮名 Label を使った合成 Demo である。Case/Judgment File は Format と Mechanism の
Fixture であり、実業務検証（P0）の Evidence ではない。Lint は参照規律を検査できるが、
Evidence が実在することまでは証明できない。

## OSCA Agent の五つの層

| 文字 | 問い |
|---|---|
| **O** — Object | 何を対象に、何を達成するか。 |
| **S** — Structure | どの Step で進めるか。 |
| **C** — Connector | Data はどこから来て、結果はどこへ行くか。 |
| **A** — Aware | いつ起動するか。 |
| **J** — Judgment | 専門家の Confirm で登録された判断がいつ適用されるか。 |

O/S/C/A は安定した骨格を定義し、J は現場に応じて変わる判断を保存する。一つの Agent は
主に Markdown + YAML からなる `.osca` Folder であり、Git で管理し、機械検証し、安定した
Checksum を生成して引き渡せる。

## Repository 構成

```text
osca/
├── docs/OSCA-WHITEPAPER-v1.1.en.md    # Open-spec whitepaper: English
├── docs/OSCA-WHITEPAPER-v1.1.zh-CN.md # Open-spec whitepaper: 简体中文
├── docs/OSCA-WHITEPAPER-v1.1.ja.md    # Open-spec whitepaper: 日本語
├── docs/OSCA-WHITEPAPER-v1.1.en.pdf    # 英語ホワイトペーパー PDF
├── docs/OSCA-WHITEPAPER-v1.1.zh-CN.pdf # 中国語ホワイトペーパー PDF
├── docs/OSCA-WHITEPAPER-v1.1.ja.pdf    # 日本語ホワイトペーパー PDF
├── docs/OSCA-SPEC-v0.4.md             # 確定仕様。v0.3 / v0.2 History も同 Directory
├── docs/OSCA-LINT-RULES.md             # Lint Rule Catalog
├── examples/oper-diagnosis.osca/      # 完全な合成 Demo Package
├── cli/                               # osca lint / pack / load / replay
├── host/                              # Runtime Host 参照実装
├── site/                              # oscaware.com Source
├── CONTRIBUTING.md
└── CHANGELOG.md
```

## 状態と Roadmap

- **公開実装/Test は再現可能：** 確定版 SPEC v0.4、合成 Demo、CLI、Host は公開済み。
  Fresh Clone で Lint/Pack/Load と自動 Test を再現できる。実 Replay には LLM/mock、Full Episode
  には Binding、Connector Fixture/Executor、LLM が必要であり、企業環境接続済みという意味ではない。
- **ファーストパーティ Feedback Flywheel（M3）：** Capture → Distill → Candidate Queue →
  Confirm/Reject → Git Judgment Ledger → Index → Retrieve → Checkup は非公開実装で完了し、
  合成 Fixture/Test がある。公開読者は独立検査できず、実業務検証でもない。
- **次の Content Gate：** 一つの高頻度実シナリオで、専門家 Confirm により 20 件以上の
  Judgment を登録し、その一部が後続 Independent Batch で再適用・支持されることを観測する。
  月次の低頻度シナリオは別集計とし、公開 Fixture は数えない。
- **Software v1.1 — メカニズム検証可能なリリース（達成済）：** 仕様（v0.4）、参照実装（Runtime ＋ CLI）、
  `osca replay` で判断が出力を編集前から編集後へ動かすのを観られる Replay 可能な匿名化 Sample Ledger、
  および真書き込み経路のエンドツーエンド接続（承認ループ ＋ 実 sql_readonly/openapi Executor ＋ 回復可能
  Episode、fake バックエンドで検証）。これが `v1.1` tag の指すもの——実業務の効果や本番書き込み検証の
  主張では**ない**（後者は 1.x／デプロイ側）。
- **1.x — 製品成熟度の検証（進行中）：** 上記の実 Content Evidence（実シナリオで専門家 Confirm 20 件以上、
  一部は Independent Batch で再適用）、製品 UI、Creator、本番統合；収束曲線は 1.x の物語。顧客 Raw Ledger の公開は不要。

## 参加

仕様議論は Issue で「Scenario → Expected Behavior → Relevant SPEC Section」の形式を推奨する。
PR は現在一時停止中。詳細は [CONTRIBUTING](CONTRIBUTING.md) を参照。

## License と Trademark

- Code と Sample：[Apache-2.0](LICENSE)
- 仕様 Text（`docs/`）：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 「Oscaware」「OSCA」の名称・Mark は上記 License に含まれない。「OSCA-compatible」など
  互換性の記述は Fair Use だが、その他の利用には別途許可が必要。

---

<div align="center">Website: <a href="https://oscaware.com">oscaware.com</a></div>
