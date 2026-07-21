"""挂起剧集的磁盘持久层（M6-W5-D2b · L2）：让可恢复剧集活过包重载与 Host 重启。

写命中审批门挂起时，把 `{episode 快照 + 关联挑战 + per_episode 计数 + 账本版本戳}` 原子写盘
（temp+rename，均相对**运行目录 fd**——与 socket/token 同住那个从 `/` 逐级 `O_NOFOLLOW` 打开、
fd 锚定的私有运行目录，开发 0700 / 生产 group 受限 0660）；package 装载时重挂（读盘 → 重建剧集 +
挑战 + 计数 → 加回台账 → 等 approve/清扫恢复）。包重载与 Host 重启走**同一条**重挂路径（重启即逐包重装）。

**删盘时机（关双写窗，改善设计 §2.4 的悲观结论）：** 恢复被调度、即写执行发生**之前**就删快照
（Host `_schedule_resume` 里，晚于 CAS、早于起写线程）。于是崩溃于「写已落地、终态未记」不会重挂重批
重写（快照早没了）；崩溃于「恢复已调度、写未完成」则写丢失（fail-closed，安全侧）。残留只剩真·硬件
半写（写到一半崩），归 W6 写执行器幂等键（§8-5）。approve 决定不活过重启（decide 是内存态）——但持久
快照里的挑战恒为 **pending**（决定一到即 `_schedule_resume` 删盘并恢复，盘上永不留已决挑战），故重挂后
仍等审批人重发/清扫，不会凭陈旧「已批」自行放行。

存储：文件名以 `operation_id`（`EO-<hex>`，跨重启唯一）键化；读时按 `package_id` 过滤 + 版本戳严格比对
（git tree OID 或非 git 源文件指纹，任一漂移 fail-closed 丢弃）。**整份快照（含 `episode.dump()` 的
context、上游产物 artifacts）须 JSON 可序列化**——任一字段非序列化（如 YAML 原生 date 混进
objects/judgments/读产物）即持久化**跳过并告警**（该剧集退回 L1、不活过重载/重启），不静默改数、不炸
（诚实标注）。（写 params 本身恒可序列化——写门 D1 已 fail-closed 挡非序列化。）
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading

log = logging.getLogger("osca-host")

PREFIX = "susp-"
SUFFIX = ".json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SuspensionStore:
    """运行目录 fd 下的挂起快照存储。dir_fd 由 Host 从安全运行目录**借用**（不持有、不关闭——
    fd 归 ControlServer 的 RuntimeDirectory，随 Host 关停一并释放）。

    **persist/delete 按 operation_id 串行 + 删除世代令牌（GPT Review 复审 P1）：** persist 下线程后，
    「delete 找不到文件（persist 未落）→ 恢复真写 → persist 迟到落盘 → 崩溃」会把已兑现的 pending
    快照留在盘上、重启重挂重批**重复写**。堵法：delete 在每-operation 锁内先把删除世代 +1 再 unlink；
    persist 发起时（事件循环侧 `begin_persist`）取当时世代作令牌，线程落盘在同一锁内**复核令牌**——
    晚于 delete 的落盘直接作废（不写文件），无论 delete 时文件存不存在。于是「决定竞态 + 崩溃」窗关死；
    unload **不 delete**（快照留盘待重载重挂），在途 persist 照常落地——保留与作废由调用方语义区分。"""

    def __init__(self, dir_fd: int):
        self._fd = dir_fd
        self._registry = threading.Lock()  # guards/世代表的注册锁（粗粒度、纯内存，纳秒级）
        self._guards: dict[str, threading.Lock] = {}  # operation_id → persist/delete 互斥锁
        # operation_id → 删除世代（进程内单调；重启归零无碍——重启后不存在在途 persist）
        self._delete_gen: dict[str, int] = {}

    def _name(self, operation_id: str) -> str:
        return f"{PREFIX}{operation_id}{SUFFIX}"

    def _guard(self, operation_id: str) -> threading.Lock:
        with self._registry:
            return self._guards.setdefault(operation_id, threading.Lock())

    def begin_persist(self, operation_id: str) -> int:
        """persist 发起时（事件循环侧、与挂起登记同临界区）取当前删除世代作令牌。"""
        with self._registry:
            return self._delete_gen.get(operation_id, 0)

    def persist(self, operation_id: str, record: dict, token: int | None = None) -> bool:
        """原子写盘（temp + fsync + rename，均相对 dir_fd）。非 JSON 可序列化 → 跳过并告警，返回 False
        （该剧集退回 L1）；token 给定且与当前删除世代不符（落盘前已被 delete 作废）→ 不写、返回 False；
        成功返回 True。文件名按 operation_id 键化。"""
        try:
            blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            log.warning(f"挂起快照非 JSON 可序列化，跳过持久化（该剧集退回 L1、不活过重载/重启）：{e}")
            return False
        with self._guard(operation_id):
            if token is not None:
                with self._registry:
                    if self._delete_gen.get(operation_id, 0) != token:
                        log.info(f"挂起快照落盘前已被恢复作废（决定竞态），放弃写盘：{operation_id}")
                        return False
            name = self._name(operation_id)
            tmp = name + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600, dir_fd=self._fd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(blob)
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)  # 原子替换
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=self._fd)
                raise
        return True

    def delete(self, operation_id: str) -> None:
        """作废并删除快照：世代 +1（在途 persist 令牌失配即弃写）再 unlink。锁竞争至多一次 fsync 时长
        （仅决定恰与落盘并发的罕见时序），可接受。"""
        with self._guard(operation_id):
            with self._registry:
                self._delete_gen[operation_id] = self._delete_gen.get(operation_id, 0) + 1
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._name(operation_id), dir_fd=self._fd)

    def load_all(self) -> list[dict]:
        """读运行目录下全部挂起快照（坏文件跳过留痕）。调用方按 package_id 过滤 + 版本戳/过期校验。"""
        out: list[dict] = []
        try:
            names = os.listdir(self._fd)
        except OSError:
            return out
        for name in names:
            if not name.startswith(PREFIX) or not name.endswith(SUFFIX):
                continue  # tmp（.json.tmp）与 socket/token/lock 一并排除
            try:
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=self._fd)
                with os.fdopen(fd, "rb") as f:
                    obj = json.loads(f.read())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
                log.warning(f"挂起快照读取失败，跳过：{name}（{e}）")
                continue
            if not isinstance(obj, dict):  # 合法 JSON 但非 mapping（null/[..]/str/number）——跳过，防 reattach 崩
                log.warning(f"挂起快照非 mapping，跳过：{name}")
                continue
            out.append(obj)
        return out
