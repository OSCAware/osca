# OSCA Post-Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate FIFO capture hangs, make policy audit logs self-describing, and close the three implementation splits found by post-merge review.

**Architecture:** Keep the immutable snapshot and Episode lifecycle boundaries introduced by the preceding architecture work. Harden the snapshot file-open boundary with non-blocking descriptors and precise exception layering; mirror the existing audit record as JSON in the normal log message; make Host consumers reuse the already validated package generation and lifecycle module.

**Tech Stack:** Python 3.10+, pytest, PyYAML, standard-library `os`, `stat`, `json`, `logging`, asyncio Host tests, uv, ruff.

## Global Constraints

- Preserve existing CLI and Host public interfaces, CLI human-readable behavior, and the `.osca` package format.
- Keep symlink, non-regular-file, size-limit, binding, budget, suspension, and L2 behavior fail-closed.
- Do not change policy audit fields, retention (`1000`), tail size (`20`), Episode status names, or suspension persistence order.
- Do not add dependencies or deployment configuration.
- Work in `.worktrees/codex/osca-post-review-hardening` on branch `codex/osca-post-review-hardening`.
- Follow strict RED → GREEN → REFACTOR order; do not batch production changes before observing their focused tests fail.

---

## File Map

- `cli/src/osca_cli/package.py`: immutable directory capture and stable `SnapshotError` adaptation.
- `cli/tests/test_pack_load.py`: FIFO non-blocking capture regression.
- `cli/src/osca_cli/packer.py`: remove obsolete `symlink_entries()` and update the package-file comment.
- `host/src/osca_host/policy.py`: bounded in-memory audit plus JSON log mirror.
- `host/tests/test_policy.py`: assert normal message and structured `osca_audit` carry the same record.
- `host/src/osca_host/host.py`: reuse `loaded.pack` for bindings and route suspension registration through `EpisodeLifecycle`.
- `host/tests/test_control.py`: prove one snapshot capture for a bindings-enabled Host load and protect suspension behavior.
- `docs/superpowers/specs/2026-07-26-osca-post-review-hardening-design.md`: change status to implemented after verification.
- `docs/superpowers/plans/2026-07-26-osca-post-review-hardening-plan.md`: mark executed steps complete.

---

### Task 1: Make Snapshot Capture Reject FIFO Without Blocking

**Files:**
- Modify: `cli/tests/test_pack_load.py`
- Modify: `cli/src/osca_cli/package.py:105-124`

**Interfaces:**
- Consumes: `PackageSnapshot.capture(root: Path | str, *, max_member_bytes: int, max_total_bytes: int) -> PackageSnapshot`.
- Preserves: `SnapshotError` public type and the normal-file snapshot/fingerprint contract.
- Produces: stable `SnapshotError("包内不是普通文件：<relpath>")` for FIFO and other opened non-regular entries.

- [x] **Step 1: Add a failing FIFO subprocess regression**

Add imports:

```python
import os
import subprocess
import sys
```

Add the test near the existing snapshot size-limit test:

```python
def test_snapshot_rejects_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    script = (
        "from pathlib import Path; "
        "from osca_cli.package import PackageSnapshot, SnapshotError; "
        "import sys; "
        "\ntry:\n"
        " PackageSnapshot.capture(Path(sys.argv[1]))\n"
        "except SnapshotError as exc:\n"
        " print(str(exc))\n"
        "else:\n"
        " raise SystemExit('FIFO was accepted')\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "包内不是普通文件：blocked.fifo"
```

This test catches removal of `O_NONBLOCK`, removal of the post-open regular-file check, and accidental re-wrapping of `SnapshotError`.

- [x] **Step 2: Run the test and confirm RED**

Run:

```bash
cd cli
uv run pytest tests/test_pack_load.py::test_snapshot_rejects_fifo_without_blocking -q
```

Expected: FAIL after approximately one second with `subprocess.TimeoutExpired`, because the child blocks in `os.open()`.

- [x] **Step 3: Implement non-blocking open and exception layering**

Change the open flags and exception order:

```python
fd = os.open(
    path,
    os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
)
with os.fdopen(fd, "rb") as stream:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise SnapshotError(f"包内不是普通文件：{rel}")
    data = stream.read(max_member_bytes + 1)
```

Immediately before the existing `except OSError as exc`, add:

```python
except SnapshotError:
    raise
```

Do not add an `lstat()`-only precheck: the opened descriptor remains the authoritative object identity and `O_NONBLOCK` closes the precheck/open FIFO race.

- [x] **Step 4: Run focused snapshot, lint, and pack tests and confirm GREEN**

Run:

```bash
cd cli
uv run pytest tests/test_pack_load.py tests/test_lint.py -q
```

Expected: PASS, including the FIFO test, snapshot size limits, symlink rejection, lint snapshot use, and deterministic packaging.

- [x] **Step 5: Commit Task 1**

```bash
git add cli/src/osca_cli/package.py cli/tests/test_pack_load.py
git commit -m "fix(cli): reject FIFO snapshots without blocking"
```

---

### Task 2: Put Policy Audit Fields in the Normal Log Message

**Files:**
- Modify: `host/tests/test_policy.py`
- Modify: `host/src/osca_host/policy.py:707-719`

**Interfaces:**
- Consumes: the existing audit record mapping with `at`, `decision`, `step`, `subject`, and `reason`.
- Preserves: `policy.audit`, `snapshot()["audit_tail"]`, logger name `osca-host.audit`, and `LogRecord.osca_audit`.
- Produces: `LogRecord.getMessage()` containing one JSON object equivalent to `LogRecord.osca_audit`.

- [x] **Step 1: Add the failing audit-message regression**

Add imports:

```python
import json
import logging
```

Add:

```python
def test_audit_log_message_contains_the_structured_record(caplog):
    policy = PolicyInterceptor("pkg", {}, {})

    with caplog.at_level(logging.INFO, logger="osca-host.audit"):
        policy._record("allow", "step", "subject", "reason")

    record = caplog.records[-1]
    expected = policy.audit[-1]
    assert json.loads(record.getMessage()) == expected
    assert record.osca_audit == expected
```

The literal record inputs make the expectation independent of the logging implementation. The two assertions protect the default formatter and existing structured formatter contract.

- [x] **Step 2: Run the test and confirm RED**

Run:

```bash
cd host
uv run pytest tests/test_policy.py::test_audit_log_message_contains_the_structured_record -q
```

Expected: FAIL with `json.decoder.JSONDecodeError`, because the message is currently `policy audit`.

- [x] **Step 3: Emit JSON while retaining the structured extra**

Replace the audit log call with:

```python
audit_log.info(
    json.dumps(record, ensure_ascii=False),
    extra={"osca_audit": record},
)
```

Do not add fields or log the policy configuration, inputs, bindings, or secrets.

- [x] **Step 4: Run policy tests and confirm GREEN**

Run:

```bash
cd host
uv run pytest tests/test_policy.py -q
```

Expected: PASS; bounded retention remains `1000`, the status tail remains the newest `20`, and the new message is parseable JSON.

- [x] **Step 5: Commit Task 2**

```bash
git add host/src/osca_host/policy.py host/tests/test_policy.py
git commit -m "fix(host): include policy audit fields in logs"
```

---

### Task 3: Reuse the Validated Host Package Generation

**Files:**
- Modify: `host/tests/test_control.py`
- Modify: `host/src/osca_host/host.py:315-322`

**Interfaces:**
- Consumes: `required_bindings(root: Path, pkg: OscaPackage | None = None) -> set[str]`.
- Consumes: `LoadedPackage.pack`, already parsed from the validation snapshot.
- Preserves: deployment binding validation errors and Host control response shape.
- Produces: exactly one `PackageSnapshot.capture()` during a normal directory Host load with bindings.

- [x] **Step 1: Add a failing single-capture Host regression**

Change the test import to:

```python
from osca_cli.package import PackageSnapshot, load_package
```

Add near the load lifecycle tests:

```python
async def test_load_with_bindings_reuses_the_validated_snapshot(running_host, sample_pack, monkeypatch):
    original = PackageSnapshot.capture.__func__
    captures = 0

    def counted_capture(cls, root, **kwargs):
        nonlocal captures
        captures += 1
        return original(cls, root, **kwargs)

    monkeypatch.setattr(PackageSnapshot, "capture", classmethod(counted_capture))

    response = await _load_pack(running_host, sample_pack)

    assert response["ok"], response
    assert captures == 1
```

The test exercises the real Host load path and counts the package boundary operation rather than mocking `required_bindings()`.

- [x] **Step 2: Run the test and confirm RED**

Run:

```bash
cd host
uv run pytest tests/test_control.py::test_load_with_bindings_reuses_the_validated_snapshot -q
```

Expected: FAIL with `assert 2 == 1`, because Host recaptures through `required_bindings(loaded.root)`.

- [x] **Step 3: Pass the validated package to `required_bindings`**

Change:

```python
errors = deployment_binding_errors(pkg_bindings, required_bindings(loaded.root))
```

to:

```python
errors = deployment_binding_errors(pkg_bindings, required_bindings(loaded.root, loaded.pack))
```

- [x] **Step 4: Run focused Host load tests and confirm GREEN**

Run:

```bash
cd host
uv run pytest tests/test_control.py::test_load_with_bindings_reuses_the_validated_snapshot tests/test_loader.py -q
```

Expected: PASS with one capture and unchanged loader compatibility.

- [x] **Step 5: Commit Task 3**

```bash
git add host/src/osca_host/host.py host/tests/test_control.py
git commit -m "fix(host): reuse validated package for bindings"
```

---

### Task 4: Finish the Lifecycle and Snapshot Cleanup

**Files:**
- Modify: `host/src/osca_host/host.py:709-738`
- Modify: `cli/src/osca_cli/packer.py:91-122`

**Interfaces:**
- Consumes: `EpisodeLifecycle.suspend(episode: Episode, challenge_id: str) -> Episode`.
- Preserves: `_register_suspension(...) -> bool`, challenge decision recheck, resume scheduling, budget retention, and L2 persistence decision.
- Removes: private, unreferenced `symlink_entries(root: Path) -> list[str]`.

- [x] **Step 1: Establish the green refactor baseline**

Run:

```bash
cd host
uv run pytest tests/test_lifecycle.py tests/test_control.py -q
cd ../cli
uv run pytest tests/test_pack_load.py -q
```

Expected: PASS before the refactor.

- [x] **Step 2: Route suspension registration through the lifecycle module**

In `Host._register_suspension()`, replace:

```python
self._suspensions[cid] = episode.episode_id
```

with:

```python
self._episode_lifecycle.suspend(episode, cid)
```

Keep both validation branches before this call and keep the challenge state recheck after it.

- [x] **Step 3: Remove obsolete symlink scanning code**

Delete `symlink_entries()` from `cli/src/osca_cli/packer.py`. In `package_files()` change the comment:

```python
pack 对链接直接拒绝（symlink_entries），load 侧按不存在处理。
```

to:

```python
快照捕获与 load 门禁都会拒绝链接，文件清单自身不跟随链接。
```

Do not change `load_symlink_entries()`, `PackageSnapshot.capture()`, or their root `indexes/` semantics.

- [x] **Step 4: Run lifecycle, control, and pack regression suites**

Run:

```bash
cd host
uv run pytest tests/test_lifecycle.py tests/test_control.py -q
cd ../cli
uv run pytest tests/test_pack_load.py -q
```

Expected: PASS; suspend/approve/resume, lost-wakeup self-healing, budget retention, symlink rejection, and deterministic pack behavior remain unchanged.

- [x] **Step 5: Confirm the dead symbol is gone**

Run:

```bash
rg -n "symlink_entries" cli host
```

Expected: only `load_symlink_entries` references remain; there is no standalone `def symlink_entries`.

- [x] **Step 6: Commit Task 4**

```bash
git add host/src/osca_host/host.py cli/src/osca_cli/packer.py
git commit -m "refactor: finish snapshot and lifecycle consolidation"
```

---

### Task 5: Full Compatibility Verification and Completion Records

**Files:**
- Modify: `docs/superpowers/specs/2026-07-26-osca-post-review-hardening-design.md`
- Modify: `docs/superpowers/plans/2026-07-26-osca-post-review-hardening-plan.md`
- Conditionally modify downstream: `/Users/lay/Documents/Git/oscapipe/uv.lock`

**Interfaces:**
- Consumes: all previous task commits.
- Produces: fresh evidence for CLI, Host, samples, downstream lock compatibility, and synchronized Git refs.

- [x] **Step 1: Run all CLI tests and quality checks**

Run:

```bash
cd cli
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Expected: `212 + 1` or more tests pass after adding the FIFO regression; ruff exits `0`.

- [x] **Step 2: Run all Host tests and quality checks**

Run:

```bash
cd host
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Expected: `428 + 2` or more tests pass after adding audit and single-capture regressions; the existing root-only cross-UID probe may remain skipped; ruff exits `0`.

- [x] **Step 3: Lint both official samples**

Run:

```bash
cd cli
uv run osca lint ../examples/oper-dispatch.osca
uv run osca lint ../examples/oper-diagnosis.osca
```

Expected: each reports 25 rules, 0 errors, and 0 warnings.

- [x] **Step 4: Mark the design and plan complete**

Change the design status to:

```markdown
**状态：** 已实施并验证
```

Mark every executed plan checkbox `[x]`. Do not alter technical requirements while recording completion.

- [x] **Step 5: Commit completion records**

```bash
git add docs/superpowers/specs/2026-07-26-osca-post-review-hardening-design.md docs/superpowers/plans/2026-07-26-osca-post-review-hardening-plan.md
git commit -m "docs: mark post-review hardening complete"
```

- [ ] **Step 6: Merge the isolated branch into local `main`**

From the primary checkout:

```bash
git merge --no-ff codex/osca-post-review-hardening
```

Expected: a merge commit with only the approved source, tests, and completion records.

- [ ] **Step 7: Push OSCA `main` and verify GitHub/ECS refs**

Run:

```bash
git push origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
git ls-remote ecs refs/heads/main
```

Expected: all three SHA values are identical and the ECS deployment hook finishes successfully.

- [ ] **Step 8: Refresh and verify downstream oscapipe**

From `/Users/lay/Documents/Git/oscapipe`, require a clean `main`, then run:

```bash
uv lock --upgrade-package osca-cli
uv run pytest -q
uv run ruff check .
```

Expected: `uv.lock` points at the new OSCA SHA, all 380 or more tests pass, and ruff check exits `0`. Do not include the two known unrelated formatter drifts in `mockups/m8_appliance/app.py` and `src/oscapipe/creator/coldstart.py`.

- [ ] **Step 9: Commit and synchronize oscapipe only if the lock changed**

```bash
git add uv.lock
git commit -m "chore: update osca-cli lock revision"
git push origin main
git push ecs main
git rev-parse HEAD
git ls-remote origin refs/heads/main
git ls-remote ecs refs/heads/main
```

Expected: the three oscapipe SHA values are identical; ECS reports the OSCA lock revision is consistent and its smoke checks pass.

- [ ] **Step 10: Remove the worktree and verify both primary repositories are clean**

Run from the OSCA primary checkout:

```bash
git worktree remove .worktrees/codex/osca-post-review-hardening
git branch -d codex/osca-post-review-hardening
git status -sb
git -C /Users/lay/Documents/Git/oscapipe status -sb
```

Expected: both repositories are on clean `main` tracking their GitHub remotes.
