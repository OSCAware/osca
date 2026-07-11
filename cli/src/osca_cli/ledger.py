"""账本写入的并发纪律 —— 包级写锁 + case 编号原子分配。

账本是只追加的共享资产，写入者却不止一个：Host 对账器（settle）、采集器
（capture）、拍板入账（confirm）可能同时落笔。两道防线：

- allocate_case_path：O_EXCL 独占创建，编号分配即占位——并发分配者绝不同号，
  「扫最大号 + 1 再普通写入」的后写覆盖先写由此根除；
- ledger_lock：包级 flock（indexes/.ledger.lock，跨进程），计数回写 / J-ID 分配
  等多文件临界区互斥。锁文件住 indexes/——缓存目录，不进交付件、不进账本扫描。

私仓 oscapipe 与本模块共用同一协议（同一锁文件路径、同一占位语义）；
本模块是协议的单一真理源。
"""

from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from pathlib import Path

CASE_NUM = re.compile(r"C-(\d+)")


class LedgerLockBusy(Exception):
    """非阻塞获取账本写锁失败——另一写入者的事务正在进行。"""


def allocate_case_path(root: Path) -> tuple[str, Path]:
    """原子分配下一个 case 编号并独占占位（空文件）。

    编号顺延现有最大号（账本只追加）；O_EXCL 创建失败（并发抢号）即顺移重试。
    调用方写入内容失败时必须 unlink 占位文件——不留空壳进账本。
    """
    cases = root / "cases"
    cases.mkdir(exist_ok=True)
    taken = [int(m.group(1)) for p in cases.glob("*.yaml") if (m := CASE_NUM.match(p.stem)) is not None]
    n = max(taken, default=0) + 1
    while True:
        case_id = f"C-{n:04d}"
        path = cases / f"{case_id}.yaml"
        try:
            path.touch(exist_ok=False)
            return case_id, path
        except FileExistsError:
            n += 1  # 并发抢号：顺移下一号


@contextmanager
def ledger_lock(root: Path, *, blocking: bool = True):
    """包级账本写锁（flock，跨进程互斥）。锁不住单文件时序的场合用它包住整个临界区。

    blocking=False 用于「不该等的读方」（如 Host 唤醒前刷新快照）：写入者事务
    进行中即抛 LedgerLockBusy——宁可拒绝本次唤醒，不可读半截账本。
    """
    lock_dir = root / "indexes"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / ".ledger.lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise LedgerLockBusy(f"账本写锁被占用：{root}（写入者事务进行中）") from e
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
