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
import subprocess
from contextlib import contextmanager
from pathlib import Path

CASE_NUM = re.compile(r"C-(\d+)")


def _git_out(root: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def ledger_stamp(root: Path | str) -> str | None:
    """包内容的版本戳：HEAD 下该包目录的 git tree OID。

    绑定包内容而非整仓 HEAD——子目录包不被无关提交作废；配合干净区检查
    （ledger_dirty），戳相同 ⇔ 账本文件内容相同。非 git / git 失败 → None，
    调用方一律按「版本不可证」处理（fail-closed），不许当「非 git 照常接受」。
    """
    root = Path(root).resolve()
    toplevel = _git_out(root, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    try:
        rel = root.relative_to(Path(toplevel).resolve())
    except ValueError:
        return None
    spec = "HEAD^{tree}" if rel == Path(".") else f"HEAD:{rel.as_posix()}"
    return _git_out(root, "rev-parse", spec)


def ledger_dirty(root: Path | str) -> list[str] | None:
    """包范围未提交改动清单（indexes/ 缓存除外）；非 git / git 失败 → None（不可判定）。

    健康档案等版本敏感产物的生产端与消费端共用：未提交的判断/case 不在版本快照里，
    干净区不成立时戳不能证明内容。
    """
    root = Path(root)
    porcelain = _git_out(root, "status", "--porcelain", "--", ".")
    if porcelain is None:
        return None
    cache = re.compile(r"(^|/)indexes/")  # 任意层级的 indexes/ 都是缓存
    return [line for line in porcelain.splitlines() if line.strip() and not cache.search(line[3:].strip().strip('"'))]


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
