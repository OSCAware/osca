# OSCA Whitepaper Localization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish semantically aligned Chinese, English, and Japanese OSCA whitepapers and README entry points, then push one reviewed branch to GitHub as a Draft PR.

**Architecture:** Treat the Chinese v1.0 whitepaper as the only factual source. English and Japanese are complete editorial translations with identical chapter structure, code, commands, evidence boundaries, and status snapshot. The three README files share the same information architecture and cross-link all README and whitepaper languages.

**Tech Stack:** Markdown, Mermaid, Git, GitHub CLI, existing Python/uv verification commands.

## Global Constraints

- Keep `docs/OSCA-WHITEPAPER-v1.0.zh-CN.md` as the canonical source.
- Preserve canonical OSCA technical terms in English in every language.
- Do not translate code, commands, paths, IDs, YAML keys, environment variables, or Git field names.
- Do not upgrade synthetic evidence to real validation or private M3 implementation to public reproducibility.
- Whitepaper v1.0 must never be described as software 1.0.
- Push only to `https://github.com/OSCAware/osca.git`; do not use the second `origin` push URL.

---

### Task 1: English whitepaper

**Files:**
- Consume: `docs/OSCA-WHITEPAPER-v1.0.zh-CN.md`
- Create: `docs/OSCA-WHITEPAPER-v1.0.en.md`

**Interfaces:**
- Consumes: all 12 chapters, three Mermaid diagrams, code blocks, tables, links, status and evidence language from the Chinese source.
- Produces: a complete English whitepaper with the same heading hierarchy and local-link targets.

- [ ] **Step 1: Translate the full document**

Use concise open-specification English. Preserve OSCA/Oscaware, O/S/C/A/J, Case/Candidate/Judgment, Confirm/`confirmed`, Runtime/Episode/Policy, Replay/Checkup, `ledger_tree`, P0-A/P0-B, L0–L4, filenames, commands, YAML and Mermaid semantics.

- [ ] **Step 2: Run structural checks**

Run:

```bash
test "$(rg -c '^### Chapter [0-9]+:' docs/OSCA-WHITEPAPER-v1.0.en.md)" -eq 12
test "$(rg -c '^```mermaid$' docs/OSCA-WHITEPAPER-v1.0.en.md)" -eq 3
rg -n 'Whitepaper version|not.*software 1.0|synthetic|private.*oscapipe|P0-A|P0-B|Unavailable' docs/OSCA-WHITEPAPER-v1.0.en.md
```

Expected: 12 chapters, three Mermaid blocks, and all evidence-boundary terms present.

### Task 2: Japanese whitepaper

**Files:**
- Consume: `docs/OSCA-WHITEPAPER-v1.0.zh-CN.md`
- Create: `docs/OSCA-WHITEPAPER-v1.0.ja.md`

**Interfaces:**
- Consumes: the same canonical source as Task 1, not the English translation.
- Produces: a complete Japanese technical whitepaper with identical structure and evidence boundaries.

- [ ] **Step 1: Translate the full document**

Use concise Japanese technical-document style. Keep canonical terms in English and add natural Japanese explanation around them. Preserve Chinese sample field values where they correspond directly to files in `examples/oper-diagnosis.osca`.

- [ ] **Step 2: Run structural checks**

Run:

```bash
test "$(rg -c '^### 第[0-9]+章：' docs/OSCA-WHITEPAPER-v1.0.ja.md)" -eq 12
test "$(rg -c '^```mermaid$' docs/OSCA-WHITEPAPER-v1.0.ja.md)" -eq 3
rg -n 'ホワイトペーパー版|ソフトウェア 1.0|合成|非公開.*oscapipe|P0-A|P0-B|Unavailable' docs/OSCA-WHITEPAPER-v1.0.ja.md
```

Expected: 12 chapters, three Mermaid blocks, and all evidence-boundary terms present.

### Task 3: Tri-language README navigation

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Create: `README.ja.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the three whitepaper paths from Tasks 1–2 and the existing English/Chinese README structure.
- Produces: three mutually linked repository entry points with three whitepaper links each.

- [ ] **Step 1: Add language and whitepaper navigation**

Use this language order everywhere: English · 简体中文 · 日本語. In the active-language README, render that language in bold and the others as links.

Add a compact whitepaper line containing:

```text
Whitepaper: English · 简体中文 · 日本語
```

localized for each README, with links to the exact three whitepaper filenames.

- [ ] **Step 2: Create the Japanese README**

Translate the full English/Chinese README information structure: definition, OSCA/Oscaware distinction, four terms, synthetic example, O/S/C/A/J, repository layout, status, participation, license and trademark. Preserve the same public/private/P0/software-1.0 limits.

- [ ] **Step 3: Update repository layout and changelog**

List all three whitepaper files in every README repository tree. Record the English/Japanese whitepaper and Japanese README publication under `CHANGELOG.md` Unreleased.

- [ ] **Step 4: Check all navigation targets**

Run `test -e` for all three README and all three whitepaper paths, then verify each README contains links to the other two README files and all three whitepapers.

### Task 4: Verification, commit and GitHub publication

**Files:**
- Verify: all files changed in Tasks 1–3
- Publish: branch `codex/osca-whitepaper`

**Interfaces:**
- Consumes: the complete localized document set.
- Produces: one Git commit, one GitHub branch, and one Draft PR targeting `main`.

- [ ] **Step 1: Run documentation gates**

Run `git diff --check`; compare chapter, Mermaid and fenced-code-block counts across the three whitepapers; validate every relative Markdown link; scan for stale two-language navigation and conflicting evidence claims.

- [ ] **Step 2: Run project gates**

Run CLI pytest/Ruff/sample Lint and Host pytest/Ruff. Expected: existing test counts pass, Ruff clean, sample 0 errors and 0 warnings.

- [ ] **Step 3: Stage and commit only localization files**

Stage the two new whitepapers, three README files, changelog and this plan. Commit with:

```bash
git commit -m "docs: publish English and Japanese whitepapers"
```

- [ ] **Step 4: Push only to GitHub**

Verify `gh auth status`, then push explicitly:

```bash
git push https://github.com/OSCAware/osca.git codex/osca-whitepaper
```

Do not run `git push origin`, because `origin` has an ECS push URL in addition to GitHub.

- [ ] **Step 5: Open one Draft PR**

Create a Draft PR from `codex/osca-whitepaper` to `main` summarizing the three-language publication, evidence boundaries and verification results.
