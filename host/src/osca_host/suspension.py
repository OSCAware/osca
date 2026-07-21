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
FAULT_MARKER = "storage-fault.marker"  # 持久故障标记（四轮复核 P2）：活过重启，运维修复后手工删除
INTENT_SUFFIX = ".intent"  # write-ahead 意向标记（五轮复核 P1）：rename 前耐久建立、生命周期证毕后耐久移除
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class _OpState:
    """单个 operation 的在途状态：persist/delete 互斥锁 + 删除世代 + 在途凭据计数（归零回收）。"""

    __slots__ = ("lock", "delete_gen", "pending")

    def __init__(self):
        self.lock = threading.Lock()
        self.delete_gen = 0
        self.pending = 0


# PersistTicket 生命周期（GPT 五审 P2：一次性**使用**也要强制，不只释放幂等）：
# PENDING（begin 发出，可 abandon）→ CLAIMED（persist 在 registry 锁内原子认领，abandon 变 no-op）
# → RELEASED（persist finally / abandon 归还，终态）。已 RELEASED/CLAIMED、跨 store、脱离注册表的
# 票在 persist 认领时一律拒绝且不建文件——否则「abandon 后旧票仍持旧状态与旧世代（0）」：delete 用
# 新状态递增世代并回收后，拿旧票 persist 查的是旧状态 delete_gen==token==0，会在注册表全空时复活快照。
_PENDING, _CLAIMED, _RELEASED = "pending", "claimed", "released"


class PersistTicket:
    """begin_persist 发的一次性在途凭据（GPT 四审 + 五审 P2）：绑定**具体状态对象、当时删除世代与
    所属 store**，使用（claim）与释放都 exactly-once。裸整数令牌 + 按 operation_id 释放会 ABA（偷走
    并发 delete 的票、世代归零复活快照）；只幂等释放不锁使用，abandon 过的旧票仍能拿旧状态里的
    旧世代通过校验重新落盘——状态机 + 原子 claim 把两条路都封死。"""

    __slots__ = ("operation_id", "state", "token", "status", "store")

    def __init__(self, operation_id: str, state: _OpState, token: int, store: SuspensionStore):
        self.operation_id = operation_id
        self.state = state
        self.token = token
        self.status = _PENDING
        self.store = store


class SuspensionStore:
    """运行目录 fd 下的挂起快照存储。dir_fd 由 Host 从安全运行目录**借用**（不持有、不关闭——
    fd 归 ControlServer 的 RuntimeDirectory，随 Host 关停一并释放）。

    **persist/delete 按 operation_id 串行 + 删除世代令牌（GPT Review 复审 P1）：** persist 下线程后，
    「delete 找不到文件（persist 未落）→ 恢复真写 → persist 迟到落盘 → 崩溃」会把已兑现的 pending
    快照留在盘上、重启重挂重批**重复写**。堵法：delete 在每-operation 状态锁内先把删除世代 +1 再 unlink；
    persist 发起时（事件循环侧 `begin_persist`）取当时世代作令牌，线程落盘在同一锁内**复核令牌**——
    晚于 delete 的落盘直接作废（不写文件），无论 delete 时文件存不存在。于是「决定竞态 + 崩溃」窗关死；
    unload **不 delete**（快照留盘待重载重挂），在途 persist 照常落地——保留与作废由调用方语义区分。

    **注册表有界（GPT Review 三审 P2）：** per-operation 状态（锁 + 删除世代）以**在途凭据**引用计数——
    begin_persist/persist/delete 各在操作期间持一票，归零即整条回收。删除世代 tombstone 只须活过在途
    persist（begin 持票保证），历史 operation 不留任何条目——常驻进程无无界增长。"""

    def __init__(self, dir_fd: int):
        self._fd = dir_fd
        self._registry = threading.Lock()  # per-op 状态注册锁（粗粒度、纯内存，纳秒级）
        self._ops: dict[str, _OpState] = {}  # 仅在途 operation 有条目（凭据归零即回收）
        # 显式存储故障态（复核 P2）：非 None 即持久层降级——「退回 L1」的承诺（快照不活过重启）
        # 已无法兑现（收回失败=盘上留孤本 / 删除耐久性未知=崩溃后可复活）。降级后 persist 拒绝、
        # load_all 拒绝重挂。读写经 _registry 锁（fault 迁移与 persist/load_all 共享同一存储级
        # gate，无并发穿越）；**落盘 FAULT_MARKER 活过重启**（四轮复核 P2）——运维修复存储并
        # 删除标记后方可恢复重挂，重启不洗白孤本。
        self.storage_fault: str | None = None
        try:
            fd = os.open(FAULT_MARKER, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd)
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                reason = f.read().strip() or "（原因未记录）"
            self.storage_fault = f"上次运行遗留存储故障标记：{reason}（运维修复存储并删除 {FAULT_MARKER} 后方可恢复）"
            log.error(f"挂起持久层带故障标记启动——persist/重挂停用：{self.storage_fault}")
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError, ValueError) as e:
            # 标记在但读不了/内容损坏（六项复核 P3：非 UTF-8 曾抛 UnicodeDecodeError 炸构造器）——
            # 一律按故障态恢复（fail-closed），不许中断 Store/Host 初始化
            self.storage_fault = f"存储故障标记读取失败（{type(e).__name__}）——按故障态处理"
            log.error(self.storage_fault)

    def _fault(self) -> str | None:
        with self._registry:
            return self.storage_fault

    def _enter_storage_fault(self, why: str) -> None:
        with self._registry:
            if self.storage_fault is not None:
                return
            self.storage_fault = why
        log.error(
            f"挂起持久层进入存储故障态（fail-closed 降级）：{why}——后续持久化与重挂全部停用，"
            f"请修复存储并删除 {FAULT_MARKER} 后重启 Host"
        )
        # 标记落盘（best-effort——存储本身在坏，写不进也已有内存态兜住本进程；写得进则活过重启，
        # 关掉「重启新建 store 洗白孤本」的窗）。锁外执行：故障盘上的 I/O 不许攥着注册锁。
        try:
            fd = os.open(FAULT_MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600, dir_fd=self._fd)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(why)
                f.flush()
                with contextlib.suppress(OSError):
                    os.fsync(f.fileno())
            with contextlib.suppress(OSError):
                os.fsync(self._fd)
        except OSError:
            log.error("存储故障标记落盘失败——标记仅本进程内存生效（存储已坏到写不进标记；重启前务必修复存储）")

    def _name(self, operation_id: str) -> str:
        return f"{PREFIX}{operation_id}{SUFFIX}"

    def _acquire_ticket(self, operation_id: str) -> PersistTicket:
        with self._registry:
            st = self._ops.setdefault(operation_id, _OpState())
            st.pending += 1
            return PersistTicket(operation_id, st, st.delete_gen, self)

    def _release_ticket(self, ticket: PersistTicket, *, allowed: str) -> None:
        """exactly-once 释放（registry 锁内）：只从 allowed 态迁 RELEASED，只释放**本票绑定的状态
        对象**的那一份计数——双释放/跨代释放/错态释放全 no-op（GPT 四审 P2：裸释放 + 钳制会偷走
        并发 delete 的票、ABA 复活快照）。"""
        with self._registry:
            if ticket.status is not allowed:
                return
            ticket.status = _RELEASED
            st = self._ops.get(ticket.operation_id)
            if st is not ticket.state:
                return  # 条目已被替换（持票期间不可能；防御性：绝不动别代的状态）
            st.pending -= 1
            if st.pending <= 0:
                self._ops.pop(ticket.operation_id, None)  # tombstone 只须活过在途凭据——归零即回收（有界性）

    def begin_persist(self, operation_id: str) -> PersistTicket:
        """persist 发起时（事件循环侧、与挂起登记同临界区）领一次性在途凭据（含当时删除世代）。
        对应的 persist() 在认领后于 finally 归还；发起后未走到 persist（如指纹计算失败）由
        abandon_persist 归还——abandon 只作用于 PENDING 票，persist 已认领/已归还时是 no-op。"""
        return self._acquire_ticket(operation_id)

    def abandon_persist(self, ticket: PersistTicket) -> None:
        """begin_persist 后未走到 persist 的归还口。只归还 **PENDING** 票（GPT 五审 P2）：
        CLAIMED（persist 使用中，其 finally 自归还）与 RELEASED 一律 no-op——绝不偷走其他
        在途者（并发 delete/新一代 begin）的票，也绝不把用过的票洗回可用。"""
        self._release_ticket(ticket, allowed=_PENDING)

    def persist(self, operation_id: str, record: dict, ticket: PersistTicket | None = None) -> bool:
        """原子写盘（temp + fsync + rename，均相对 dir_fd）。非 JSON 可序列化 → 跳过并告警，返回 False
        （该剧集退回 L1）；ticket 的删除世代与当前不符（落盘前已被 delete 作废）→ 不写、返回 False；
        成功返回 True。文件名按 operation_id 键化。

        **一次性使用强制**（GPT 五审 P2）：开写前在 registry 锁内原子校验并认领（PENDING→CLAIMED）——
        已 abandon/已用过（含并发双用）、跨 store、脱离注册表的票一律拒绝且不建文件。abandon 后的旧票
        持旧状态与旧世代，不拦会在 delete 回收后借「旧状态 delete_gen==旧 token」复活快照。
        认领后 finally 归还；无票直调（测试/工具路径）自持一票新领即认领。"""
        if ticket is None:
            ticket = self._acquire_ticket(operation_id)
        elif ticket.operation_id != operation_id:
            raise ValueError(f"persist 凭据不属于 {operation_id}（属 {ticket.operation_id}）——拒绝错票落盘")
        with self._registry:  # 原子校验并认领：一次性凭据的「使用」也 exactly-once
            if (
                ticket.store is not self
                or ticket.status is not _PENDING
                or self._ops.get(operation_id) is not ticket.state
            ):
                log.warning(f"persist 凭据无效（已用过/已归还/跨 store/已脱离注册表），拒绝落盘：{operation_id}")
                return False
            ticket.status = _CLAIMED
        st = ticket.state
        try:
            if self._fault():
                log.warning(f"挂起持久层处于存储故障态，拒绝持久化（不再谎称退回 L1）：{operation_id}")
                return False
            try:
                blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as e:
                log.warning(f"挂起快照非 JSON 可序列化，跳过持久化（该剧集退回 L1、不活过重载/重启）：{e}")
                return False
            with st.lock:
                with self._registry:
                    invalidated = st.delete_gen != ticket.token
                    fault = self.storage_fault  # 与 fault 迁移共享存储级 gate（四轮复核 P2）
                if fault:
                    log.warning(f"挂起持久层已进入存储故障态（写前复核），拒绝持久化：{operation_id}")
                    return False
                if invalidated:
                    log.info(f"挂起快照落盘前已被恢复作废（决定竞态），放弃写盘：{operation_id}")
                    return False
                name = self._name(operation_id)
                intent = name + INTENT_SUFFIX
                tmp = name + ".tmp"
                renamed = False
                try:
                    # ① write-ahead intent（五轮复核 P1）：rename **之前**耐久建立意向标记——此后任何
                    # 失败/崩溃，快照都因 intent 尚存被 load_all 按可疑跳过（不复活）；intent 自身建
                    # 不耐久则在 rename 前就中止（零快照，安全侧）。崩溃恢复的正确性由此**不再依赖**
                    # 故障 marker 的耐久性（marker 只是运维 UX 的带子）。
                    ifd = os.open(intent, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600, dir_fd=self._fd)
                    with os.fdopen(ifd, "wb") as f:
                        f.write(b"pending\n")
                        f.flush()
                        os.fsync(f.fileno())
                    os.fsync(self._fd)  # intent 目录项耐久——失败即抛，rename 不会发生
                    # ② 快照本体
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600, dir_fd=self._fd)
                    with os.fdopen(fd, "wb") as f:
                        f.write(blob)
                        f.flush()
                        os.fsync(f.fileno())
                    os.rename(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)  # 原子替换
                    renamed = True
                    try:
                        # rename 后同步目录（P1）：断电可把已落名快照回滚成不存在（rename 丢失）。
                        os.fsync(self._fd)
                    except OSError:
                        # durability-unknown（四轮复核 P2）：收回刚落名的快照；收回失败/收回耐久未知
                        # → 显式存储故障态。intent **保留**——即便孤本/复活，重启也按可疑跳过（五轮 P1）。
                        try:
                            os.unlink(name, dir_fd=self._fd)
                        except OSError as unlink_error:
                            self._enter_storage_fault(f"快照收回失败（{unlink_error}）——盘上留有不可承诺状态的孤本")
                        else:
                            try:
                                os.fsync(self._fd)
                            except OSError:
                                self._enter_storage_fault("快照收回后的目录 fsync 失败——删除耐久性未知，崩溃后可复活")
                        raise
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp, dir_fd=self._fd)
                    if not renamed:
                        # rename 未发生：干净中止——intent 一并撤（不留待清理的悬疑标记）
                        with contextlib.suppress(OSError):
                            os.unlink(intent, dir_fd=self._fd)
                    with contextlib.suppress(OSError):
                        os.fsync(self._fd)  # 清理同样耐久（best-effort，不掩盖原异常）
                    raise
                # ③ 写后故障复核（五轮复核 P2）：文件操作期间另一线程可能已进故障态——已落名快照按
                # 可疑收回（intent 保留），不得返回 True（不对故障存储宣称持久成功）
                with self._registry:
                    faulted = self.storage_fault
                if faulted:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=self._fd)
                        os.fsync(self._fd)
                    log.warning(f"存储故障态期间完成的快照已收回（intent 保留，重启按可疑跳过）：{operation_id}")
                    return False
                # ④ 生命周期证毕：耐久移除 intent——移除失败按 OSError 上抛（快照留着 intent，
                # 重启按可疑跳过，fail-closed：persist 失败 = 不作持久承诺）
                os.unlink(intent, dir_fd=self._fd)
                os.fsync(self._fd)
            return True
        finally:
            self._release_ticket(ticket, allowed=_CLAIMED)  # 认领过的票由此 exactly-once 归还

    def delete(self, operation_id: str) -> None:
        """作废并删除快照：世代 +1（在途 persist 令牌失配即弃写）再 unlink。**本方法可能等一次 fsync**
        （与在途 persist 争同一状态锁）——调用方须在线程里调（Host._resume_after_delete / 重挂批量清理），
        不上事件循环。自持一票凭据护住文件操作期间的状态条目与锁对象（内部票不外流、原地归还）。"""
        with self._registry:
            st = self._ops.setdefault(operation_id, _OpState())
            st.delete_gen += 1
            st.pending += 1
        ticket = PersistTicket(operation_id, st, st.delete_gen, self)  # 仅作归还凭据（token 不参与判定）
        try:
            with st.lock:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._name(operation_id), dir_fd=self._fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._name(operation_id) + INTENT_SUFFIX, dir_fd=self._fd)  # 意向标记随快照清理
                # unlink 后同步目录（P1）：断电可把删除回滚——已兑现写入的旧快照复活会被重挂重批
                # **重复写**。同步失败 OSError 上抛：调用方保留挂起态待清扫重试，绝不带着「删没删成
                # 不确定」推进真写。
                os.fsync(self._fd)
        finally:
            self._release_ticket(ticket, allowed=_PENDING)

    def load_all(self) -> list[dict]:
        """读运行目录下全部挂起快照（坏文件跳过留痕）。调用方按 package_id 过滤 + 版本戳/过期校验。

        存储故障态下**拒绝重挂**（复核 P2）：故障期间盘上内容不可信（收回失败的孤本/删除耐久性
        未知的残影），读回重挂可能兑现本应作废的写——fail-closed 空表，要求运维修复。
        """
        if fault := self._fault():
            log.error(f"挂起持久层存储故障态，拒绝重挂：{fault}")
            return []
        out: list[dict] = []
        try:
            names = os.listdir(self._fd)
        except OSError:
            return out
        name_set = set(names)
        for name in names:
            if not name.startswith(PREFIX) or not name.endswith(SUFFIX):
                continue  # tmp（.json.tmp）/intent 与 socket/token/lock 一并排除
            if name + INTENT_SUFFIX in name_set:
                # write-ahead intent 尚存（五轮复核 P1）：该快照的生命周期未证完成（崩于目录 fsync/
                # 收回失败的孤本/删除耐久性未知后的复活）——按可疑跳过不重挂，请运维核对后清理
                log.warning(f"挂起快照带未了结的 write-ahead intent——按可疑跳过不重挂，请运维核对：{name}")
                continue
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
        # 清理无快照的 stale intent（崩于 rename 之前）——**在途 persist 的 intent 勿动**（其
        # operation 仍在注册表；动了会拆掉正在建立的联锁）
        for name in names:
            if not name.endswith(INTENT_SUFFIX) or name[: -len(INTENT_SUFFIX)] in name_set:
                continue
            opid = name[len(PREFIX) : -len(SUFFIX + INTENT_SUFFIX)] if name.startswith(PREFIX) else ""
            with self._registry:
                inflight = opid in self._ops
            if not inflight:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=self._fd)
        # 扫描后复核（五轮复核 P2）：扫描期间进故障态 → 结果整体作废（不半截可信）
        if self._fault():
            log.error("挂起持久层在扫描期间进入存储故障态——本次重挂结果整体作废")
            return []
        return out
