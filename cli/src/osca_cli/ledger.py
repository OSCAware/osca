"""账本写入的并发纪律 —— 包级写锁 + case 编号原子分配。

账本是只追加的共享资产，写入者却不止一个：Host 对账器（settle）、采集器
（capture）、拍板入账（confirm）可能同时落笔。两道防线：

- allocate_case_path：O_EXCL 独占创建，编号分配即占位——并发分配者绝不同号，
  「扫最大号 + 1 再普通写入」的后写覆盖先写由此根除；
- ledger_lock：包级 flock（跨进程），计数回写 / J-ID 分配等多文件临界区互斥。
  锁文件住 git common dir（按包路径哈希命名）——不随缓存目录被删重建，
  也不给包内容留预置符号链接的机会；非 git 根退回 indexes/。

私仓 oscapipe 与本模块共用同一协议（同一锁文件路径、同一占位语义）；
本模块是协议的单一真理源。
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

CASE_NUM = re.compile(r"C-(\d+)")
JUNK_NAMES = {".DS_Store"}  # 系统垃圾文件：loader 永不读取，不作为脏区证据


def _git_out(root: Path, *args: str) -> str | None:
    """git 输出或 None——总函数：git 不存在/不可执行（OSError）与命令失败同样返回 None。"""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _repo_relative(root: Path) -> tuple[str, str] | None:
    """(git common dir 实路径, 包的 repo 相对路径)；非 git/失败 → None。"""
    root = Path(root).resolve()
    common = _git_out(root, "rev-parse", "--git-common-dir")
    toplevel = _git_out(root, "rev-parse", "--show-toplevel")
    if common is None or toplevel is None:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (root / common_path).resolve()
    try:
        rel = root.relative_to(Path(toplevel).resolve()).as_posix()
    except ValueError:
        return None
    return str(common_path.resolve()), ("" if rel == "." else rel)


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
    """包范围未提交/未跟踪/被忽略的改动清单；非 git / git 失败 → None（不可判定）。

    健康档案等版本敏感产物的生产端与消费端共用：不在版本快照里的内容（含 gitignored
    的判断/case——loader 照样会读它们）让戳无法证明内容。豁免只给**包根** `indexes/`
    （缓存目录，公理 A4）与系统垃圾文件；`judgments/indexes/` 这类内层目录不豁免。
    调用方必须显式区分 None（不可判定 → 按不可信处理）与 []（干净）。
    """
    root = Path(root).resolve()
    ident = _repo_relative(root)
    if ident is None:
        return None
    prefix = ident[1] + "/" if ident[1] else ""
    # -z：NUL 分隔、路径不转义——porcelain 路径相对 repo 根，嵌套包须按包前缀归一化后再豁免
    porcelain = _git_out(root, "status", "--porcelain", "--ignored=matching", "-z", "--", ".")
    if porcelain is None:
        return None

    def exempt(path: str) -> bool:
        rel = path[len(prefix) :] if prefix and path.startswith(prefix) else path
        if rel == "indexes" or rel.startswith("indexes/"):
            return True  # 仅包根缓存目录豁免（嵌套包的 pkg/indexes/ 归一化后同样豁免）
        return rel.rsplit("/", 1)[-1] in JUNK_NAMES

    entries = []
    records = porcelain.split("\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record.strip():
            continue
        paths = [record[3:] if len(record) > 3 else record]
        # porcelain -z 协议：rename/copy 的**原路径**跟在下一个 NUL 段、无状态前缀——必须成对消费，
        # 否则第二段被切头三个字符、当成新记录误判（根缓存内部 rename 曾被误报脏）
        if len(record) > 3 and (record[0] in "RC" or record[1] in "RC") and i < len(records) and records[i]:
            paths.append(records[i])
            i += 1
        if all(exempt(p) for p in paths):
            continue  # 两段都在豁免区（如缓存内部 rename）才豁免；任一段出界即脏
        entries.append(record)
    return entries


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


def _lock_path(root: Path) -> Path:
    """锁文件位置：git common dir 下按包路径哈希命名——不住可丢弃的 indexes/
    （删除重建缓存目录会造出第二个锁 inode），也不给包内容留预置符号链接的机会。
    非 git 根（测试/临时目录）退回 indexes/，但拒绝符号链接目录。
    """
    ident = _repo_relative(root)
    if ident is not None:
        common_path, rel = ident
        # 哈希「仓库稳定身份 + 包的 repo 相对路径」——linked worktree 的同一个包拿到同一把锁
        digest = hashlib.sha256(f"{common_path}:{rel}".encode()).hexdigest()[:16]
        return Path(common_path) / f"osca-ledger-{digest}.lock"
    lock_dir = root / "indexes"
    if lock_dir.is_symlink():
        raise OSError(f"{lock_dir} 是符号链接——拒绝在链接目录建账本锁")
    lock_dir.mkdir(exist_ok=True)
    return lock_dir / ".ledger.lock"


@contextmanager
def open_ledger_dir(root: Path, name: str):
    """安全打开包内发布目录（cases/、indexes/）——发布路径不许逃出包根（Review 十三/十四轮）。

    两层 fd 锚定：先 O_DIRECTORY|O_NOFOLLOW 打开**包根**拿 root_fd（包根本身被换成
    符号链接在此即拒），再经 dir_fd=root_fd 创建/打开发布目录（最后一段 O_NOFOLLOW
    拒符号链接）。单层 O_NOFOLLOW 只保护路径最后一段——包根这类祖先在检查后被换成
    外部目录链接，仍可把发布导出包根（十四轮探针）；持有 fd 后目录项再被替换，
    只作用于已持有的真实目录 inode。name 限单一目录名：路径分隔符与 ./.. 一律拒绝。
    """
    if name != os.path.basename(name) or name in ("", ".", ".."):
        raise OSError(f"发布目录名必须是单一目录名（不含路径分隔符与 ./..）：{name!r}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    try:
        try:
            os.mkdir(name, dir_fd=root_fd)
        except FileExistsError:
            pass  # 已存在真实目录照常打开；预置符号链接由下面 O_NOFOLLOW 拒绝
        fd = os.open(name, flags, dir_fd=root_fd)
        try:
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def publish_file_in_dir(dir_fd: int, filename: str, data: bytes, *, overwrite: bool) -> bool:
    """在已安全打开的目录 fd 内原子发布文件：唯一临时名（O_EXCL、不跟随链接）→ 写满 +
    fsync → link（无覆盖，占用返回 False 由调用方顺移）或 replace（覆盖）→ 清理临时名
    → 目录 fsync。目录 fsync 放在临时名清理之后、占用/异常路径同样覆盖（十四轮）——
    否则崩溃恢复可能残留点号临时文件把账本判脏。"""
    tmp_name = f".{filename}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            try:
                os.link(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except FileExistsError:
                return False
        return True
    finally:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass  # replace 已把临时名挪走
        os.fsync(dir_fd)  # link/replace 落名 + 临时名删除，一并耐久


@contextmanager
def ledger_lock(root: Path, *, blocking: bool = True):
    """包级账本写锁（flock，跨进程互斥）。锁不住单文件时序的场合用它包住整个临界区。

    blocking=False 用于「不该等的读方」（如 Host 唤醒前刷新快照）：写入者事务
    进行中即抛 LedgerLockBusy——宁可拒绝本次唤醒，不可读半截账本。
    打开方式 O_NOFOLLOW 且不截断：锁文件被预置成符号链接即报错，不覆写链接目标。
    """
    path = _lock_path(Path(root))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise LedgerLockBusy(f"账本写锁被占用：{root}（写入者事务进行中）") from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
