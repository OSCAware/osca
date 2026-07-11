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
