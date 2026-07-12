"""账本并发纪律：case 编号原子分配（O_EXCL 占位）+ 包级写锁。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from osca_cli.ledger import LedgerLockBusy, allocate_case_path, ledger_lock


def test_allocate_appends_after_existing(tmp_path):
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "C-0007.yaml").write_text("case_id: C-0007\n", encoding="utf-8")
    case_id, path = allocate_case_path(tmp_path)
    assert case_id == "C-0008"
    assert path.exists()  # 分配即占位


def test_allocate_starts_at_one_and_creates_dir(tmp_path):
    case_id, path = allocate_case_path(tmp_path)
    assert case_id == "C-0001"
    assert path.parent == tmp_path / "cases"


def test_allocate_skips_placeholder_of_rival(tmp_path):
    """对手已占位（内容未落盘）→ 顺移下一号，绝不同号覆盖。"""
    allocate_case_path(tmp_path)  # C-0001 占位
    case_id, _ = allocate_case_path(tmp_path)
    assert case_id == "C-0002"


def test_concurrent_allocation_never_collides(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: allocate_case_path(tmp_path)[0], range(24)))
    assert len(set(ids)) == 24  # 全部唯一
    assert sorted(ids)[-1] == "C-0024"


def test_nonblocking_lock_raises_when_busy(tmp_path):
    """不该等的读方（Host 唤醒前刷新）：写入者持锁时立刻收到 LedgerLockBusy，不阻塞。"""
    with ledger_lock(tmp_path):
        with pytest.raises(LedgerLockBusy), ledger_lock(tmp_path, blocking=False):
            pass  # pragma: no cover——不该走到这里
    with ledger_lock(tmp_path, blocking=False):
        pass  # 锁释放后非阻塞获取照常


def test_ledger_lock_serializes_critical_section(tmp_path):
    log: list[int] = []

    def critical(i: int):
        with ledger_lock(tmp_path):
            log.append(i)
            log.append(i)  # 锁内两次写入必须相邻——被打断即说明没锁住

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(critical, range(8)))
    assert all(log[i] == log[i + 1] for i in range(0, len(log), 2))
    assert not (tmp_path / "indexes" / ".ledger.lock").is_dir()  # 锁文件是文件不是目录


def _git(root, *args):
    import subprocess

    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_lock_lives_in_git_common_dir(tmp_path):
    """锁住 git common dir（按包路径哈希）——不随缓存目录删除重建产生第二个 inode。"""
    _git(tmp_path, "init", "-q")
    with ledger_lock(tmp_path):
        pass
    assert not (tmp_path / "indexes" / ".ledger.lock").exists()
    assert list((tmp_path / ".git").glob("osca-ledger-*.lock"))


def test_lock_refuses_symlinked_lock_file(tmp_path):
    """非 git 退回路径上的锁文件被预置成符号链接 → O_NOFOLLOW 报错，不截断链接目标。"""
    victim = tmp_path / "victim.txt"
    victim.write_text("不许动的数据", encoding="utf-8")
    (tmp_path / "indexes").mkdir()
    (tmp_path / "indexes" / ".ledger.lock").symlink_to(victim)
    with pytest.raises(OSError):
        with ledger_lock(tmp_path):
            pass
    assert victim.read_text(encoding="utf-8") == "不许动的数据"


def test_ledger_dirty_sees_ignored_and_inner_indexes(tmp_path):
    """脏区三漏（十一轮）：gitignored 的判断、内层 indexes/ 都算脏；仅包根 indexes/ 豁免。"""
    from osca_cli.ledger import ledger_dirty

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "测试")
    (tmp_path / ".gitignore").write_text("indexes/\njudgments/J-0005.yaml\n", encoding="utf-8")
    (tmp_path / "judgments").mkdir()
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "1")
    assert ledger_dirty(tmp_path) == []  # 干净

    (tmp_path / "indexes").mkdir()
    (tmp_path / "indexes" / "cache.json").write_text("{}", encoding="utf-8")
    assert ledger_dirty(tmp_path) == []  # 包根缓存豁免

    (tmp_path / "judgments" / "J-0005.yaml").write_text("judgment_id: J-0005\n", encoding="utf-8")
    dirty = ledger_dirty(tmp_path)
    assert dirty and any("J-0005" in line for line in dirty)  # gitignored 判断也算脏——loader 会读它
    (tmp_path / "judgments" / "J-0005.yaml").unlink()

    (tmp_path / "judgments" / "indexes").mkdir()
    (tmp_path / "judgments" / "indexes" / "x.yaml").write_text("x: 1", encoding="utf-8")
    assert ledger_dirty(tmp_path)  # 内层 indexes/ 不豁免——loader 读 judgments/**/*.yaml


def test_nested_package_root_indexes_exempt(tmp_path):
    """嵌套包：porcelain 路径带 repo 前缀（pkg/indexes/…）——归一化后包根缓存仍豁免。"""
    from osca_cli.ledger import ledger_dirty

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "测试")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "1")
    assert ledger_dirty(pkg) == []

    (pkg / "indexes").mkdir()
    (pkg / "indexes" / "cache.json").write_text("{}", encoding="utf-8")
    assert ledger_dirty(pkg) == []  # 嵌套包的包根缓存不再被永久判脏

    (pkg / "b.txt").write_text("2", encoding="utf-8")
    assert ledger_dirty(pkg)  # 真脏照常可见


def test_lock_shared_across_linked_worktrees(tmp_path):
    """linked worktree 的同一个包必须拿到同一把锁——哈希仓库稳定身份 + repo 相对路径。"""
    import subprocess

    from osca_cli.ledger import _lock_path

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "测试")
    (main / "a.txt").write_text("1", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "1")
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True, capture_output=True)

    assert _lock_path(main) == _lock_path(wt)  # 两个 checkout，同一把锁


def test_open_ledger_dir_refuses_symlink(tmp_path):
    """发布目录是符号链接（dirty 豁免包根缓存目录，链接可通过全部版本检查）→ 拒绝，包外零写入。"""
    from osca_cli.ledger import open_ledger_dir

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "pack"
    root.mkdir()
    (root / "indexes").symlink_to(outside)
    with pytest.raises(OSError):
        with open_ledger_dir(root, "indexes"):
            pass
    assert list(outside.iterdir()) == []


def test_open_ledger_dir_refuses_symlinked_root(tmp_path):
    """包根本身是符号链接（十四轮：单层 O_NOFOLLOW 只护最后一段）→ 拒绝，包外零写入。"""
    from osca_cli.ledger import open_ledger_dir

    outside = tmp_path / "outside"
    (outside / "indexes").mkdir(parents=True)
    (tmp_path / "pack").symlink_to(outside)
    with pytest.raises(OSError):
        with open_ledger_dir(tmp_path / "pack", "indexes"):
            pass
    assert list((outside / "indexes").iterdir()) == []


def test_open_ledger_dir_anchors_root_inode_after_swap(tmp_path):
    """包根在检查后被替换（原包根改名保存 + 原位放外链、外部有真实 indexes/）——
    十四轮确定性交错：写入必须仍落在已持有的原包根 inode，包外零写入。"""
    from osca_cli.ledger import open_ledger_dir, publish_file_in_dir

    outside = tmp_path / "outside"
    (outside / "indexes").mkdir(parents=True)
    root = tmp_path / "pack"
    root.mkdir()
    with open_ledger_dir(root, "indexes") as dfd:
        root.rename(tmp_path / "pack-moved")  # 原包根改名保存
        (tmp_path / "pack").symlink_to(outside)  # 原位放置指向外部目录的链接
        assert publish_file_in_dir(dfd, "replay-health.json", b"{}", overwrite=True)
    assert list((outside / "indexes").iterdir()) == []  # outside_written 必须为 False
    assert (tmp_path / "pack-moved" / "indexes" / "replay-health.json").read_bytes() == b"{}"


def test_open_ledger_dir_requires_single_basename(tmp_path):
    """name 限单一目录名——路径分隔符与 ./.. 一律拒绝，不给相对路径逃出包根的机会。"""
    from osca_cli.ledger import open_ledger_dir

    for bad in ("a/b", "../escape", ".", "..", ""):
        with pytest.raises(OSError):
            with open_ledger_dir(tmp_path, bad):
                pass


def test_publish_file_in_dir_protocol(tmp_path):
    """dir_fd 发布协议：无覆盖 link 占用返回 False；覆盖 replace 生效；临时件不残留。"""
    from osca_cli.ledger import open_ledger_dir, publish_file_in_dir

    with open_ledger_dir(tmp_path, "cases") as dfd:
        assert publish_file_in_dir(dfd, "C-0001.yaml", b"first", overwrite=False)
        assert not publish_file_in_dir(dfd, "C-0001.yaml", b"rival", overwrite=False)  # 无覆盖
        assert (tmp_path / "cases" / "C-0001.yaml").read_bytes() == b"first"
        assert publish_file_in_dir(dfd, "C-0001.yaml", b"newer", overwrite=True)  # 覆盖模式
        assert (tmp_path / "cases" / "C-0001.yaml").read_bytes() == b"newer"
    assert not list((tmp_path / "cases").glob(".*.tmp"))


def test_rename_within_root_indexes_stays_clean(tmp_path):
    """-z rename 成对消费：根缓存内部 rename 不误报脏（第二段曾被切头三字符误判）。"""
    from osca_cli.ledger import ledger_dirty

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "测试")
    pkg = tmp_path / "pkg"
    (pkg / "indexes").mkdir(parents=True)
    (pkg / "indexes" / "a.json").write_text("{}", encoding="utf-8")
    (pkg / "x.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "1")
    _git(tmp_path, "mv", "pkg/indexes/a.json", "pkg/indexes/b.json")
    assert ledger_dirty(pkg) == []  # 缓存内部 rename——两段都在豁免区

    _git(tmp_path, "mv", "pkg/indexes/b.json", "pkg/escaped.json")
    assert ledger_dirty(pkg)  # rename 出缓存区——任一段出界即脏
