# OSCA Correctness and Architecture Deepening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复整体 Code Review 的七项缺陷，并以不可变包快照和统一剧集生命周期两个深层 Module 消除其重复根因。

**Architecture:** `PackageSnapshot` 一次捕获包字节，lint、pack、Host load、账本刷新和挂起指纹共享该代际；Episode 在装配时钉住快照指纹。`EpisodeLifecycle` 统一终态写入和 Policy 临时资源释放；TriggerTable 用每订阅单槽派发 lane 隔离慢订阅。

**Tech Stack:** Python 3.10+、PyYAML、asyncio、pytest/pytest-asyncio、ruff、uv

## Global Constraints

- 保持现有 CLI、Host 对外行为与 `.osca` 包格式兼容。
- `osca lint` 对符号链接稳定失败是唯一有意的 CLI 安全收紧。
- 不引入新的第三方运行时依赖。
- Package Snapshot 单文件上限 50 MiB、总字节上限 200 MiB，与 zip 防护一致。
- 根 `.git/`、根 `indexes/` 和 `.DS_Store` 不进入包快照；嵌套 `indexes/` 是业务内容，必须进入。
- 挂起指纹必须钉在 Episode 装配代际，不能在 persist 时读取可换代的 `loaded.pack`。
- 所有生产改动先由失败测试证明，再写最小实现。

---

### Task 1: Package Snapshot Core and Lint Adapter

**Files:**
- Modify: `cli/src/osca_cli/package.py`
- Modify: `cli/src/osca_cli/lint.py`
- Modify: `cli/src/osca_cli/rules.py`
- Test: `cli/tests/test_lint.py`
- Test: `cli/tests/test_rules.py`
- Test: `cli/tests/test_pack_load.py`

**Interfaces:**
- Produces: `SnapshotError`, `PackageSnapshot.capture(root, *, max_member_bytes, max_total_bytes)`, `load_package(root, snapshot=None)`, `lint_snapshot(snapshot, package: str | None = None)`.
- Produces: `PackageSnapshot.with_root(root) -> PackageSnapshot`, preserving bytes, directories and fingerprint while rebinding the root after a zip directory rename.
- Produces: `OscaPackage.exists()`, `OscaPackage.has_directory()`, `OscaPackage.is_file()`, `OscaPackage.iter_text_files()` backed by snapshot bytes.
- Consumes: existing YAML bounds, `REQUIRED_FILES`, `SKIP_DIRS`, lint `run_all`.

- [ ] **Step 1: Add failing snapshot tests**

```python
def test_lint_rejects_symlink_with_stable_finding(make_pkg, base):
    pkg = make_pkg(base)
    (pkg / "linked.yaml").symlink_to(pkg / "policy.yaml")
    result = lint_package(pkg)
    assert not result.ok
    assert any(f.rule == "OSCA000" and "符号链接" in f.message for f in result.findings)


def test_snapshot_rejects_member_and_total_size_limits(make_pkg, base):
    pkg = make_pkg(base)
    (pkg / "large.txt").write_bytes(b"x" * 65)
    with pytest.raises(SnapshotError, match="单文件"):
        PackageSnapshot.capture(pkg, max_member_bytes=64, max_total_bytes=256)
    with pytest.raises(SnapshotError, match="总字节"):
        PackageSnapshot.capture(pkg, max_member_bytes=128, max_total_bytes=64)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `cd cli && uv run pytest tests/test_lint.py tests/test_rules.py tests/test_pack_load.py -q`

Expected: new imports or assertions fail because snapshot interfaces do not exist.

- [ ] **Step 3: Implement immutable capture**

```python
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024


class SnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class PackageSnapshot:
    root: Path
    files: Mapping[str, bytes]
    directories: frozenset[str]
    fingerprint: str

    @classmethod
    def capture(
        cls,
        root: Path,
        *,
        max_member_bytes: int = MAX_MEMBER_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> "PackageSnapshot":
        base = Path(root)
        if not base.is_dir():
            raise SnapshotError(f"包目录不存在：{base}")
        blobs: dict[str, bytes] = {}
        directories: set[str] = set()
        links: list[str] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            current = Path(dirpath)
            if current == base and ".git" in dirnames:
                dirnames.remove(".git")
            for name in list(dirnames):
                path = current / name
                rel = path.relative_to(base).as_posix()
                if path.is_symlink():
                    links.append(rel)
                else:
                    directories.add(rel)
            for name in filenames:
                path = current / name
                rel = path.relative_to(base).as_posix()
                if path.is_symlink():
                    links.append(rel)
                    continue
                if rel.split("/", 1)[0] == "indexes" or name == ".DS_Store":
                    continue
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise SnapshotError(f"包内不是普通文件：{rel}")
                        data = stream.read(max_member_bytes + 1)
                except OSError as exc:
                    raise SnapshotError(f"包文件读取失败：{rel}（{type(exc).__name__}）") from exc
                if len(data) > max_member_bytes:
                    raise SnapshotError(f"单文件 {rel} 超上限 {max_member_bytes} 字节")
                total += len(data)
                if total > max_total_bytes:
                    raise SnapshotError(f"包总字节超上限 {max_total_bytes}")
                blobs[rel] = data
        if links:
            raise SnapshotError(f"检测到符号链接：{'、'.join(sorted(links)[:3])}——包内容不跟随链接")
        digest = hashlib.sha256()
        for rel, data in sorted(blobs.items()):
            digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
        return cls(base, MappingProxyType(blobs), frozenset(directories), f"fp:{digest.hexdigest()}")
```

Use `MappingProxyType` for `files`. Compute `fingerprint` from sorted `relative_path + NUL + bytes + NUL`. Do not suppress `OSError`.

- [ ] **Step 4: Move lint rules to snapshot queries**

Replace direct `pkg.root` reads in OSCA001, OSCA002, OSCA004, OSCA024 and OSCA050 with the `OscaPackage` snapshot Interface. Keep filesystem `resolve_in_root()` for runtime callers, but lint path declarations use normalized relative-path checks plus `pkg.is_file()`.

- [ ] **Step 5: Add `lint_snapshot` and stable error adaptation**

```python
def lint_snapshot(snapshot: PackageSnapshot, *, package: str | None = None) -> LintResult:
    pkg = load_package(snapshot.root, snapshot=snapshot)
    return LintResult(package=package or str(snapshot.root), findings=run_all(pkg), files_checked=len(pkg.yaml_files))


def lint_package(path):
    try:
        snapshot = PackageSnapshot.capture(Path(path))
    except SnapshotError as exc:
        return LintResult(str(path), [Finding("OSCA000", Severity.ERROR, ".", str(exc))], 0)
    return lint_snapshot(snapshot, package=str(path))
```

- [ ] **Step 6: Run focused CLI tests and confirm GREEN**

Run: `cd cli && uv run pytest tests/test_lint.py tests/test_rules.py tests/test_pack_load.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cli/src/osca_cli/package.py cli/src/osca_cli/lint.py cli/src/osca_cli/rules.py cli/tests
git commit -m "feat(cli): add immutable package snapshots"
```

### Task 2: Make pack Consume One Snapshot

**Files:**
- Modify: `cli/src/osca_cli/packer.py`
- Test: `cli/tests/test_pack_load.py`

**Interfaces:**
- Consumes: `PackageSnapshot.capture`, `lint_snapshot`.
- Produces: unchanged `pack_package(path, output) -> tuple[OpResult, Path | None]`.

- [ ] **Step 1: Add the lint-to-archive race regression**

```python
def test_pack_archives_exact_snapshot_that_passed_lint(make_pkg, base, tmp_path, monkeypatch):
    pkg = make_pkg(base)
    original = packer.lint_snapshot

    def lint_then_mutate(snapshot, **kwargs):
        result = original(snapshot, **kwargs)
        (snapshot.root / "AGENT.md").write_text("sk-live-secret", encoding="utf-8")
        return result

    monkeypatch.setattr(packer, "lint_snapshot", lint_then_mutate)
    result, archive = pack_package(pkg, tmp_path / "out.osca.zip")
    assert result.ok
    with ZipFile(archive) as zf:
        assert b"sk-live-secret" not in zf.read("AGENT.md")
```

- [ ] **Step 2: Run the regression and confirm RED**

Run: `cd cli && uv run pytest tests/test_pack_load.py::test_pack_archives_exact_snapshot_that_passed_lint -q`

Expected: archive contains the post-lint mutation.

- [ ] **Step 3: Replace pack rereads with snapshot bytes**

Capture before lint. Run `lint_snapshot(snapshot)`. Use `snapshot.files` for forbidden-name checks, checksum lines, package id parsing and zip entries. Keep deterministic zip metadata and output-path safety unchanged.

- [ ] **Step 4: Run pack/load tests and confirm GREEN**

Run: `cd cli && uv run pytest tests/test_pack_load.py -q`

Expected: PASS, including deterministic archive tests.

- [ ] **Step 5: Commit**

```bash
git add cli/src/osca_cli/packer.py cli/tests/test_pack_load.py
git commit -m "fix(cli): pack the exact linted snapshot"
```

### Task 3: Carry the Validated Snapshot Through Host Load

**Files:**
- Modify: `cli/src/osca_cli/packer.py`
- Modify: `host/src/osca_host/loader.py`
- Modify: `host/src/osca_host/host.py`
- Modify: `host/src/osca_host/episode.py`
- Test: `cli/tests/test_pack_load.py`
- Test: `host/tests/test_loader.py`
- Test: `host/tests/test_control.py`

**Interfaces:**
- Produces: internal `load_osca_snapshot(archive, dest=None, bindings=None, *, require_bindings=False, abort=None) -> tuple[OpResult, Path | None, PackageSnapshot | None]`.
- Preserves: public `load_osca(archive, dest=None, bindings=None, *, require_bindings=False, abort=None) -> tuple[OpResult, Path | None]`.
- Produces: `Episode.package_fingerprint: str`.
- Consumes: `LoadedPackage.pack.snapshot.fingerprint`.

- [ ] **Step 1: Add directory and zip handoff regressions**

```python
def test_load_for_host_parses_the_validated_directory_snapshot(sample_pack, monkeypatch):
    original = loader.load_osca_snapshot

    def mutate_after_gate(*args, **kwargs):
        result, root, snapshot = original(*args, **kwargs)
        manifest = yaml.safe_load((root / "osca.yaml").read_text(encoding="utf-8"))
        manifest["package_id"] = "post-gate-mutation"
        (root / "osca.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        return result, root, snapshot

    monkeypatch.setattr(loader, "load_osca_snapshot", mutate_after_gate)
    result, loaded = load_for_host(sample_pack, require_bindings=False)
    assert result.ok
    assert loaded.package_id != "post-gate-mutation"


def test_zip_snapshot_is_captured_before_dest_switch(make_pkg, base, tmp_path, monkeypatch):
    archive = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "deploy"
    original_swap = packer._swap_into_dest

    def mutate_after_swap(tmp, root, abort=None):
        error = original_swap(tmp, root, abort=abort)
        if error is None:
            (root / "AGENT.md").write_text("post-swap", encoding="utf-8")
        return error

    monkeypatch.setattr(packer, "_swap_into_dest", mutate_after_swap)
    result, root, snapshot = packer.load_osca_snapshot(archive, dest=dest)
    assert result.ok and root == dest
    assert snapshot.root == dest
    assert snapshot.read_text("AGENT.md") != "post-swap"
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `cd host && uv run pytest tests/test_loader.py tests/test_control.py -q`

Expected: directory test observes the second read; zip handoff Interface is missing.

- [ ] **Step 3: Add an internal snapshot-returning load path**

Refactor `_validate_package_root` to accept or return the exact captured snapshot used by integrity, lint and binding checks. For zip, capture the extracted temporary directory and carry that object across `_swap_into_dest`; never recapture `dest`. Implement `load_osca` as a two-value compatibility Adapter over `load_osca_snapshot`.

After a successful zip swap, call `snapshot.with_root(dest)` before returning it. Snapshot consumers use only relative-path byte/directory Interfaces; `load_package(dest, snapshot=snapshot)` supplies the live deployment root for mutable ledger paths. No consumer may perform I/O through the dead pre-rename temporary path.

- [ ] **Step 4: Parse Host structures from the returned snapshot**

`load_for_host` must call `load_osca_snapshot`, then `load_package(root, snapshot=snapshot)`. Remove the historical second live-directory parse. Assert in the zip regression that `snapshot.root == dest`.

- [ ] **Step 5: Pin the package generation during Episode assembly**

```python
@dataclass
class Episode:
    episode_id: str
    package_id: str
    aware_id: str
    fired_trigger: str
    assembled_at: str
    then: str | None
    budget: dict
    context: dict = field(repr=False)
    package_fingerprint: str = ""


return Episode(
    episode_id=episode_id,
    operation_id=f"EO-{uuid.uuid4().hex}",
    package_id=loaded.package_id,
    aware_id=aware.aware_id,
    fired_trigger=fired_trigger,
    assembled_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    then=aware.then,
    budget=aware.budget,
    context=context,
    package_fingerprint=loaded.pack.snapshot.fingerprint,
)
```

Keep the field in `dump()` for L2 persistence; `summary()` need not expose it.

The dataclass snippet shows field placement only. Add exactly `package_fingerprint: str = ""` to the existing Episode; do not rewrite or delete `operation_id`, `status`, `steps`, `resume`, `tokens_used`, settlements or any other field. The empty default preserves deserialization of old L2 Episode dictionaries.

- [ ] **Step 6: Replace `_pack_stamp` disk hashing**

`Host._pack_stamp(loaded)` returns `loaded.pack.snapshot.fingerprint`. `_persist_suspension` writes `episode.package_fingerprint`, not the current registry package. Reattach compares the record stamp with the newly loaded snapshot stamp and logs a precise generation mismatch.

- [ ] **Step 7: Add fingerprint and G1/G2 persistence tests**

Cover root `indexes/` exclusion, nested `judgments/indexes/` inclusion, read-failure rejection at capture, and:

```python
g1_fingerprint = loaded.pack.snapshot.fingerprint
episode = assemble("EP-0001", loaded, loaded.awares[0], loaded.awares[0].triggers[0].trigger_id)
loaded.pack = g2
await host._persist_suspension(episode, policy)
assert stored["version_stamp"] == g1_fingerprint
```

- [ ] **Step 8: Migrate ledger refresh to the snapshot generation**

Inside `_refresh_ledger` and its existing `ledger_lock`, call `PackageSnapshot.capture(loaded.root)`, then `load_package(loaded.root, snapshot=fresh_snapshot)`. Run lint, rebuild the generated index and evaluate kill-switch input from that same `fresh` package. Only after every step succeeds assign `loaded.pack = fresh` and publish the paired kill-switch state; on any failure retain the old package generation.

Add this regression:

```python
def test_refresh_publishes_snapshot_generation_used_by_next_episode(host, loaded, policy):
    old_fingerprint = loaded.pack.snapshot.fingerprint
    (loaded.root / "AGENT.md").write_text("refreshed generation", encoding="utf-8")
    assert host._refresh_ledger(loaded, policy)
    episode = assemble("EP-refresh", loaded, loaded.awares[0], loaded.awares[0].triggers[0].trigger_id)
    assert episode.package_fingerprint == loaded.pack.snapshot.fingerprint
    assert episode.package_fingerprint != old_fingerprint
```

- [ ] **Step 9: Run CLI and Host load/suspension tests**

Run: `cd cli && uv run pytest tests/test_pack_load.py -q`

Run: `cd host && uv run pytest tests/test_loader.py tests/test_episode.py tests/test_control.py tests/test_suspension.py -q`

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add cli/src/osca_cli/packer.py host/src/osca_host/loader.py host/src/osca_host/host.py host/src/osca_host/episode.py cli/tests host/tests
git commit -m "fix(host): bind loads and suspensions to package generations"
```

### Task 4: Centralize Episode Terminal Lifecycle

**Files:**
- Create: `host/src/osca_host/lifecycle.py`
- Modify: `host/src/osca_host/runner.py`
- Modify: `host/src/osca_host/host.py`
- Modify: `host/src/osca_host/policy.py`
- Test: `host/tests/test_lifecycle.py`
- Test: `host/tests/test_policy.py`
- Test: `host/tests/test_control.py`
- Test: `host/tests/test_runner.py`

**Interfaces:**
- Produces: `finish_episode_state(episode, status, reason=None) -> Episode`.
- Produces: `EpisodeLifecycle.suspend(episode, challenge_id) -> Episode`.
- Produces: `EpisodeLifecycle.finish(episode, status=None, reason=None, policy=None) -> Episode`.
- Produces: `EpisodeLifecycle.evict(episode_id, policy=None) -> Episode | None`.
- Produces: `PolicyInterceptor.release_episode(episode_id) -> None`.

- [ ] **Step 1: Add lifecycle cleanup failures**

```python
def test_terminal_transition_releases_policy_budget():
    lifecycle.finish(episode, "completed", policy=policy)
    assert policy.episode_budget_used(episode.episode_id) == (0, 0)


def test_suspended_episode_retains_budget():
    lifecycle.suspend(episode, challenge_id)
    assert policy.episode_budget_used(episode.episode_id) == (1, 80)


def test_finish_and_evict_are_idempotent():
    lifecycle.finish(episode, "failed", policy=policy)
    lifecycle.finish(episode, "failed", policy=policy)
    assert lifecycle.evict(episode.episode_id, policy=policy) is episode
```

- [ ] **Step 2: Run focused lifecycle tests and confirm RED**

Run: `cd host && uv run pytest tests/test_lifecycle.py tests/test_policy.py -q`

Expected: lifecycle module and `release_episode` do not exist.

- [ ] **Step 3: Implement lifecycle Module**

`finish_episode_state` owns the legal terminal-state assertion and timestamp write. `EpisodeLifecycle` receives the Host episode and suspension dictionaries, removes every `challenge_id -> episode_id` entry for a terminal episode, and calls `policy.release_episode()` exactly once semantically (method itself is idempotent).

- [ ] **Step 4: Route Runner and Host terminal paths through the Module**

Replace Runner `_finish` with `finish_episode_state`. In Host, create one `EpisodeLifecycle` over `self.episodes` and `self._suspensions`; finalize Runner terminal returns, execution exceptions, suspension registration failures, unload/shutdown stops and ledger eviction through it. Do not release suspended budgets.

- [ ] **Step 5: Run runner/control tests and confirm GREEN**

Run: `cd host && uv run pytest tests/test_lifecycle.py tests/test_policy.py tests/test_runner.py tests/test_control.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add host/src/osca_host/lifecycle.py host/src/osca_host/runner.py host/src/osca_host/host.py host/src/osca_host/policy.py host/tests
git commit -m "refactor(host): centralize episode terminal lifecycle"
```

### Task 5: Bound Policy Audit Memory

**Files:**
- Modify: `host/src/osca_host/policy.py`
- Test: `host/tests/test_policy.py`

**Interfaces:**
- Produces: `AUDIT_RETENTION = 1000`.
- Preserves: `policy.audit` as a list-compatible value and `snapshot()["audit_tail"]` at 20 entries.
- Produces: structured logger `osca-host.audit`.

- [ ] **Step 1: Add the retention regression**

```python
def test_audit_is_bounded_and_keeps_newest_entries():
    policy = make_policy()
    for index in range(AUDIT_RETENTION + 25):
        policy._record("allow", "step", str(index), "test")
    assert len(policy.audit) == AUDIT_RETENTION
    assert policy.audit[0]["subject"] == "25"
    assert policy.snapshot()["audit_tail"] == policy.audit[-20:]
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `cd host && uv run pytest tests/test_policy.py::test_audit_is_bounded_and_keeps_newest_entries -q`

Expected: audit length exceeds retention.

- [ ] **Step 3: Cap the list and emit the structured record**

Append the record, delete the oldest overflow slice, then emit it through `logging.getLogger("osca-host.audit")` using `extra={"osca_audit": record}`. Do not include any field not already present in the in-memory audit record.

- [ ] **Step 4: Run policy tests and confirm GREEN**

Run: `cd host && uv run pytest tests/test_policy.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add host/src/osca_host/policy.py host/tests/test_policy.py
git commit -m "fix(host): bound policy runtime state"
```

### Task 6: Isolate Automatic Watcher Dispatch

**Files:**
- Modify: `host/src/osca_host/triggers.py`
- Test: `host/tests/test_table.py`

**Interfaces:**
- Produces: private `_DispatchLane` with `task`, `pending`, `coalesced`.
- Preserves: `TriggerTable._fire(watcher)` awaitable API and synchronous `fire_manual` completion semantics.

- [ ] **Step 1: Add dispatch isolation failures**

```python
@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_fast_or_next_fire():
    release = asyncio.Event()
    fast_called = asyncio.Event()

    async def slow(trigger_id):
        await release.wait()

    async def fast(trigger_id):
        fast_called.set()

    table = TriggerTable()
    watcher = table.subscribe(
        "event", {"source": "op"}, Subscription("pkg-slow", "AW-1", "AW-1/T1", slow)
    )
    table.subscribe("event", {"source": "op"}, Subscription("pkg-fast", "AW-2", "AW-2/T1", fast))
    await table._fire(watcher)
    await asyncio.wait_for(fast_called.wait(), timeout=0.1)
    await asyncio.wait_for(table._fire(watcher), timeout=0.1)
    slow_lane = table._lanes[(watcher.key, "pkg-slow", "AW-1", "AW-1/T1")]
    assert slow_lane.pending is True
    release.set()


@pytest.mark.asyncio
async def test_coalescing_is_bounded_and_logged(caplog):
    release = asyncio.Event()

    async def blocked(trigger_id):
        await release.wait()

    table = TriggerTable()
    watcher = table.subscribe(
        "event", {"source": "op"}, Subscription("pkg", "AW-1", "AW-1/T1", blocked)
    )
    await table._fire(watcher)
    await table._fire(watcher)
    await table._fire(watcher)
    await table._fire(watcher)
    lane = next(iter(table._lanes.values()))
    assert lane.pending is True
    assert lane.coalesced == 2
    assert watcher.key in caplog.text and "合并 2" in caplog.text
    release.set()
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `cd host && uv run pytest tests/test_table.py -q`

Expected: current `_fire` blocks on the slow subscriber.

- [ ] **Step 3: Implement per-subscription lanes**

Key lanes by `(watcher.key, package_id, aware_id, trigger_id)`. `_fire` increments `watcher.fires`, submits every current subscription and returns without awaiting delivery completion. A lane drain awaits its subscription serially; when busy, one tick sets `pending=True`, later ticks increment `coalesced` and log. Exceptions are caught inside the lane.

- [ ] **Step 4: Clean lanes on unsubscribe and shutdown**

Cancel and remove lanes for dropped subscriptions. Shutdown cancels watcher tasks and all active lane tasks before clearing both dictionaries.

- [ ] **Step 5: Run trigger and control tests**

Run: `cd host && uv run pytest tests/test_table.py tests/test_control.py -q`

Expected: PASS; manual fire still waits and reports delivery errors.

- [ ] **Step 6: Commit**

```bash
git add host/src/osca_host/triggers.py host/tests/test_table.py
git commit -m "fix(host): isolate shared watcher subscribers"
```

### Task 7: Schedule Type, Documentation, and Migration Note

**Files:**
- Modify: `cli/src/osca_cli/triggers.py`
- Modify: `cli/tests/test_triggers.py`
- Modify: `cli/README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Preserves: existing `parse_schedule` return shape.
- Changes: monthly `day` accepts only non-bool integers in 1–31.

- [ ] **Step 1: Add the bool-day regression**

```python
@pytest.mark.parametrize("day", [True, False])
def test_monthly_schedule_rejects_boolean_day(day):
    schedule, errors = parse_schedule({"every": "month", "day": day, "time": "09:00"})
    assert schedule is None
    assert errors
```

- [ ] **Step 2: Run it and confirm RED**

Run: `cd cli && uv run pytest tests/test_triggers.py -q`

Expected: `True` is accepted as day 1.

- [ ] **Step 3: Require exact integer semantics**

Use `type(day) is int and 1 <= day <= 31`, preserving all other parser behavior.

- [ ] **Step 4: Correct documentation**

Change both CLI README rule counts from 22 to 25. Add a CHANGELOG entry covering immutable package snapshots, Episode-generation suspension stamps, intentional rejection of incompatible stored L2 snapshots, bounded lifecycle state and isolated watcher dispatch. Explicitly state both symlink tightenings: standalone lint now rejects package symlinks, and root `indexes/` symlinks are no longer exempt in lint/pack.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `cd cli && uv run pytest tests/test_triggers.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cli/src/osca_cli/triggers.py cli/tests/test_triggers.py cli/README.md CHANGELOG.md
git commit -m "fix(cli): reject boolean monthly schedule days"
```

### Task 8: Full Verification and Compatibility Audit

**Files:**
- Verify: all files changed by Tasks 1–7
- Modify only if a verification failure exposes a defect covered by the approved design.

**Interfaces:**
- Consumes: all previous task outputs.
- Produces: evidence that CLI, Host, lint samples, formatting and dependency locks remain compatible.

- [ ] **Step 1: Run all CLI tests**

Run: `cd cli && uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run all Host tests**

Run: `cd host && uv run pytest -q`

Expected: all tests pass; the existing root-only cross-UID probe may remain skipped when not running as root.

- [ ] **Step 3: Run lint and format checks**

Run: `cd cli && uv run ruff check . && uv run ruff format --check .`

Run: `cd host && uv run ruff check . && uv run ruff format --check .`

Expected: all commands exit 0.

- [ ] **Step 4: Lint official sample packages**

Run `uv run osca lint` from `cli/` for each repository sample package discovered under `examples/`.

Expected: 25 rules, zero errors for every official sample.

- [ ] **Step 5: Refresh Host lock against local CLI**

Run: `cd host && uv lock --upgrade-package osca-cli`

Expected: lock resolves successfully without unrelated dependency upgrades.

- [ ] **Step 6: Inspect the final diff**

Run: `git diff --check && git status --short && git diff --stat main...HEAD`

Expected: no whitespace errors; only approved source, tests and docs are changed.

- [ ] **Step 7: Commit any verification-only lock update**

```bash
git add host/uv.lock
git commit -m "chore(host): refresh osca-cli lock"
```

Skip this commit when `host/uv.lock` is unchanged.
