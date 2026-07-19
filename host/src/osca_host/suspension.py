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

log = logging.getLogger("osca-host")

PREFIX = "susp-"
SUFFIX = ".json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SuspensionStore:
    """运行目录 fd 下的挂起快照存储。dir_fd 由 Host 从安全运行目录**借用**（不持有、不关闭——
    fd 归 ControlServer 的 RuntimeDirectory，随 Host 关停一并释放）。"""

    def __init__(self, dir_fd: int):
        self._fd = dir_fd

    def _name(self, operation_id: str) -> str:
        return f"{PREFIX}{operation_id}{SUFFIX}"

    def persist(self, operation_id: str, record: dict) -> bool:
        """原子写盘（temp + fsync + rename，均相对 dir_fd）。非 JSON 可序列化 → 跳过并告警，返回 False
        （该剧集退回 L1）；成功返回 True。文件名按 operation_id 键化。"""
        try:
            blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            log.warning(f"挂起快照非 JSON 可序列化，跳过持久化（该剧集退回 L1、不活过重载/重启）：{e}")
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
