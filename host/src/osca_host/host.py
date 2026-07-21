"""Host 进程：控制平面确定性常驻，本体无 LLM（架构 §4）。

W5 形态（M2 七组件齐）：注册表 + 触发表 + 闸门 + 剧集装配器 + Policy 拦截器
+ Connector 代理 + 对账器 + 控制通道。唤醒 → 装配 → 执行 → （objective 型）对账。
LLM 只活在剧集执行器（认知平面，osca_host.runner）里，跑在独立线程，
Host 事件循环保持确定性响应；三级停三级全可演示：剧集停（pipeline 完成 /
budget 硬顶 / 步骤失败）、触发器停（disable）、包停（unload）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import signal
import time
from collections import OrderedDict
from dataclasses import asdict, replace
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import yaml
from osca_cli.findings import Severity
from osca_cli.ledger import LedgerLockBusy, ledger_lock, ledger_stamp
from osca_cli.package import load_package
from osca_cli.packer import deployment_binding_errors, rebuild_index, required_bindings
from osca_cli.rules import run_all

from osca_host import __version__
from osca_host.authz import Authorizer, Principal, ensure_admin_token, load_principals
from osca_host.challenge import TERMINAL_RETENTION_SECONDS, Challenge
from osca_host.connector import ConnectorProxy
from osca_host.control import ControlServer, admin_token_path, principals_path
from osca_host.episode import Episode, assemble
from osca_host.expr import parse_precondition
from osca_host.gate import Gate
from osca_host.loader import load_for_host
from osca_host.policy import REPLAY_RED, PolicyInterceptor, ledger_stats
from osca_host.registry import Registry, RegistryError
from osca_host.runner import run_episode
from osca_host.settle import settle_episode
from osca_host.suspension import SuspensionStore
from osca_host.threads import run_in_daemon_thread
from osca_host.triggers import Subscription, TriggerTable

log = logging.getLogger("osca-host")

EPISODE_LEDGER_CAP = 100  # 剧集台账只留近期；持久归档随 M3 采集器/账本落地


class HostState(Enum):
    STARTING = auto()
    RUNNING = auto()
    DRAINING = auto()
    STOPPED = auto()


class Host:
    def __init__(
        self,
        socket_path: Path,
        deployments: dict[str, dict] | None = None,
        control_group: str | None = None,
    ):
        self.registry = Registry()
        # 触发表不注入表级 poller：watch 轮询走 Subscription 携带的本代 poll（_make_poll 捕获本代 proxy，
        # 跨代不外呼——GPT Review 三审 P1）；表级 poller 仅测试裸用
        self.table = TriggerTable()
        self.gates: dict[tuple[str, str], Gate] = {}  # (package_id, aware_id) → Gate
        self.policies: dict[str, PolicyInterceptor] = {}
        self.proxies: dict[str, ConnectorProxy] = {}
        self.bindings: dict[str, dict] = {}  # package_id → 部署注入的 binding 表（按包隔离，永不进包）
        # 部署清单（服务端解析）：deployment_id → {path[, bindings, dest]}——控制通道只收 ID，
        # 路径类参数绝不从连接者透传（confused-deputy 文件读写面，M4 首轮 P1）
        self.deployments: dict[str, dict] = dict(deployments or {})
        self.episodes: OrderedDict[str, Episode] = OrderedDict()  # 剧集台账（近期）
        self._episode_seq = 0
        self._episode_tasks: set[asyncio.Task] = set()  # 在跑剧集（认知平面，独立线程）
        self._suspensions: dict[str, str] = {}  # challenge_id → episode_id：挂起等批的写剧集（可恢复剧集，D2a）
        self._resuming: set[str] = set()  # episode_id：恢复在途（删盘在线程，防同剧集双恢复；GPT 三审 P2）
        # 挂起快照磁盘持久层（D2b·L2）：run() 里锚定运行目录后建；未建（如单元测试直连）时挂起仅 L1（进程内）
        self._suspension_store: SuspensionStore | None = None
        self.authorizer = Authorizer()
        self.control = ControlServer(socket_path, self.handle, self.authorizer, control_group)
        self._cmd_lock = asyncio.Lock()  # 命令串行；load 的重活在线程里跑，事件循环保持响应
        # 同包触发投递串行（GPT Review P1 事件循环阻塞的修复随行）：账本刷新/precondition 取数下线程后，
        # 同包并发投递会在账本 flock 上互踩（第二个拿不到非阻塞锁被误拒唤醒）——按包一把 asyncio 锁保旧序
        self._deliver_locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self.state = HostState.STARTING
        # Aware 代际（P1）：disable 递增——停用前已进入慢投递（账本刷新/闸门裁决在线程）的旧代
        # 投递，跨越「停用→重新启用」边界返回时按代际失配永久失效，不再创建剧集
        self._aware_generations: dict[tuple[str, str], int] = {}
        self._deployment_generations: dict[str, int] = {}
        self._deployment_locks: dict[str, asyncio.Lock] = {}
        self._load_slots: dict[str, tuple[int, int, asyncio.Task]] = {}
        self._package_deployments: dict[str, str] = {}
        self._package_tombstones: dict[str, int] = {}
        self._operation_seq = 0
        self._last_unload_operation = 0
        self._control_tasks: set[asyncio.Task] = set()
        self._episode_shutdown_timeout = 60.0  # 关停等在跑剧集的上限（秒）；测试可注入缩短

    # ── 控制命令（schema 与授权已在 ControlServer 裁决后才进到这里） ──────

    async def handle(self, request: dict, principal: Principal) -> dict:
        current = asyncio.current_task()
        if current is not None:
            self._control_tasks.add(current)
        cmd = request["cmd"]
        if cmd not in ("status", "episodes", "episode"):  # 只读命令不刷屏；变更命令留操作者身份痕
            log.info(f"[control] {principal.name}（{principal.role}）→ {cmd}")
        try:
            if cmd == "load":
                # 重活（读盘/解压/lint）在锁外线程执行——慢 load 不许压住 status/stop（W0.1 P2）
                spec = self.deployments.get(request["deployment_id"])
                if spec is None:
                    detail = f"未配置的部署 ID：{request['deployment_id']}（部署清单归 Host 侧管理）"
                    return {"ok": False, "detail": detail}
                return await self._request_load(request["deployment_id"], spec)
            if cmd == "fire":
                # fire 同 load 出全局命令锁（GPT Review 复审 P2）：投递含线程化的账本刷新/precondition
                # 取数，持 _cmd_lock 等它会把 status/stop/approve 全排在慢投递之后。短锁只查状态；
                # 投递自身按包锁串行 + 代际 CAS（与 watcher 自动发射同一路径、同一防护）。
                async with self._cmd_lock:
                    if self.state is not HostState.RUNNING:
                        return {"ok": False, "detail": f"Host 当前为 {self.state.name}，拒绝新的变更命令"}
                return await self._fire(request["package_id"], request["trigger_id"])
            async with self._cmd_lock:
                if self.state is not HostState.RUNNING and cmd not in ("status", "episodes", "episode", "stop"):
                    return {"ok": False, "detail": f"Host 当前为 {self.state.name}，拒绝新的变更命令"}
                return await self._dispatch(cmd, request, principal)
        except RegistryError as e:
            return {"ok": False, "detail": str(e)}
        finally:
            if current is not None:
                self._control_tasks.discard(current)

    async def _dispatch(self, cmd: str, request: dict, principal: Principal) -> dict:
        if cmd == "status":
            self._sweep_suspensions()  # 轮询顺带清扫过期/已决挂起（§5.4：无决定超时 + 丢唤醒窗第二保险）
            snapshot = self.registry.status()
            for pkg in snapshot["packages"]:
                pkg["gates"] = [
                    gate.snapshot() for (pid, _), gate in sorted(self.gates.items()) if pid == pkg["package_id"]
                ]
                policy = self.policies.get(pkg["package_id"])
                pkg["policy"] = policy.snapshot() if policy else None
            return {"ok": True, "version": __version__, **snapshot, "triggers": self.table.status()}
        if cmd in ("approve", "deny"):
            # 审批人经控制通道批/驳一张具体挑战（绑 challenge_id）。principal.name 必与挑战指定审批人相符
            # （ChallengeStore.decide 强制），冒名/越权/一次性全在状态机 fail-closed。
            policy = self.policies.get(request["package_id"])
            if policy is None:
                return {"ok": False, "detail": f"包未注册：{request['package_id']}"}
            ok, detail = policy.decide_challenge(
                request["challenge_id"], by_name=principal.name, by_role=principal.role, approve=(cmd == "approve")
            )
            log.info(detail)
            if ok:  # 裁决成功 → 触发挂起剧集恢复（approve 兑现 / deny 回落，均从审批步重入本剧集，§3）
                self._maybe_resume_for_challenge(request["challenge_id"], request["package_id"])
            return {"ok": ok, "detail": detail}
        if cmd == "challenges":
            policy = self.policies.get(request["package_id"])
            if policy is None:
                return {"ok": False, "detail": f"包未注册：{request['package_id']}"}
            return {"ok": True, "challenges": policy.pending_challenges()}
        if cmd == "unload":
            return self._unload(request["package_id"])
        if cmd in ("enable", "disable"):
            return self._set_aware(request["package_id"], request["aware_id"], cmd == "enable")
        if cmd == "episodes":
            return {"ok": True, "episodes": [ep.summary() for ep in self.episodes.values()]}
        if cmd == "episode":
            episode = self.episodes.get(request["episode_id"])
            if episode is None:
                return {"ok": False, "detail": f"剧集不存在（台账只留近期 {EPISODE_LEDGER_CAP} 条）"}
            return {"ok": True, "episode": episode.dump()}
        if cmd == "stop":
            log.info("收到 stop 命令，开始关停")
            self._begin_draining()
            return {"ok": True, "detail": "Host 关停中"}
        return {"ok": False, "detail": f"未知命令：{cmd}"}  # schema 先裁过，此行是防御兜底

    def _begin_draining(self) -> None:
        """单个事件循环原子写入生命周期 tombstone；任何迟到发布都会看到它。"""
        if self.state in (HostState.DRAINING, HostState.STOPPED):
            self._stop.set()
            return
        self.state = HostState.DRAINING
        for deployment_id in set(self._deployment_generations) | set(self._load_slots):
            self._deployment_generations[deployment_id] = self._deployment_generations.get(deployment_id, 0) + 1
        # 初始 --load 发生在 run() 进入 _stop.wait() 之前；若只置事件，stop 会等慢
        # worker 返回后才有机会进入 _shutdown，形成循环等待。这里立即取消协程任务；
        # asyncio.to_thread 的系统线程可自行收尾，但 generation 已失效，绝不能迟到发布。
        for _, _, task in self._load_slots.values():
            if not task.done():
                task.cancel()
        self._stop.set()

    async def _request_load(self, deployment_id: str, spec: dict) -> dict:
        """同 deployment 共享同一 generation 的在途任务；tombstone 后创建新 generation。"""
        async with self._cmd_lock:
            if self.state is not HostState.RUNNING:
                return {"ok": False, "detail": f"Host 当前为 {self.state.name}，拒绝开始 load"}
            current_generation = self._deployment_generations.get(deployment_id, 0)
            slot = self._load_slots.get(deployment_id)
            if (
                slot is not None
                and slot[0] == current_generation
                and slot[1] > self._last_unload_operation
                and not slot[2].done()
            ):
                task = slot[2]
            else:
                generation = current_generation + 1
                self._operation_seq += 1
                operation = self._operation_seq
                self._deployment_generations[deployment_id] = generation
                task = asyncio.create_task(self._load_generation(deployment_id, generation, operation, spec))
                self._load_slots[deployment_id] = (generation, operation, task)

                def clear_slot(done: asyncio.Task, did=deployment_id, gen=generation, op=operation) -> None:
                    if self._load_slots.get(did) == (gen, op, done):
                        self._load_slots.pop(did, None)

                task.add_done_callback(clear_slot)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # shield 区分两种取消：共享 load 自身被 shutdown 取消时 task.cancelled() 为真，
            # 应给请求方稳定业务响应；若只是当前请求被取消，底层共享 load 仍活着，继续
            # 向外传播。不要用 Task.cancelling()——它是 3.11+，项目支持下限为 3.10。
            if task.cancelled():
                return {"ok": False, "detail": "Host 正在关停，load 已取消且不会发布"}
            raise

    async def _load_generation(self, deployment_id: str, generation: int, operation: int, spec: dict) -> dict:
        lock = self._deployment_locks.setdefault(deployment_id, asyncio.Lock())
        async with lock:
            async with self._cmd_lock:
                if self.state is not HostState.RUNNING or self._deployment_generations.get(deployment_id) != generation:
                    return {"ok": False, "detail": "load generation 已失效，未开始准备"}
            return await self._load(spec, deployment_id, generation, operation)

    async def _load(self, spec: dict, deployment_id: str, generation: int, operation: int) -> dict:
        """装载一个部署条目：{path[, bindings, dest]}——路径只来自服务端部署清单。

        重活（读盘/解压/lint/索引重建、binding 读取、git 戳探测）整段进线程，
        **锁外**执行——慢 load 不许压住 status/stop；_cmd_lock 只罩短暂的发布段
        （注册表 + 笼子 + 闸门 + 布防，纯状态变更，事件循环上原子）。
        """

        def _abort() -> str | None:
            """线程安全的装载作废令牌（复核 P1）：load worker 在守护线程里跑——被取消的 coroutine
            拦不住线程继续执行,STOPPED/换代之后的迟到 worker 必须在磁盘写副作用（索引重建/dest
            切换）之前自行止步。GIL 下读这些字段是原子的;stop/unload 都先推进它们再走后续。
            unload tombstone 一并纳入（四轮复核 P1）：worker 在解析出 package_id 之前不知道
            自己属谁——本次装载开始后发生的**任何** unload 都保守作废（fail-closed:宁可让无关
            装载重试,不让被 tombstone 的包把磁盘副作用做完）。"""
            if self.state is not HostState.RUNNING:
                return f"Host 已 {self.state.name}"
            if self._deployment_generations.get(deployment_id) != generation:
                return "load generation 已失效（unload/关停/新一代 load）"
            if self._last_unload_operation > operation:
                return "本次装载开始后已有 unload tombstone（保守作废,可重试）"
            return None

        def _build():
            result, loaded = load_for_host(
                str(spec.get("path")), dest=spec.get("dest"), bindings=spec.get("bindings"), abort=_abort
            )
            pkg_bindings: dict = {}
            replay_kill_unprovable = False
            if loaded is not None:
                # binding 按包隔离：本次注入只归本包——同名 binding 不跨包串线、卸载即清理。
                # 重读后再过一遍装载门禁（P1）：load_osca 校验与此处重读是两次 I/O，文件在其间被
                # 换成非法形状不得静默带病发布（与 CLI 同一判据，单一真理源）
                if spec.get("bindings"):
                    pkg_bindings = yaml.safe_load(Path(str(spec["bindings"])).read_text(encoding="utf-8")) or {}
                    errors = deployment_binding_errors(pkg_bindings, required_bindings(loaded.root))
                    if errors:
                        for line in errors:
                            result.fail(line)
                        return result, None, {}, False
                policy_file = loaded.pack.yaml_files.get("policy.yaml")
                kill_entries = (policy_file.mapping.get("kill_switch") if policy_file else None) or []
                has_replay_condition = any(
                    isinstance(e, dict) and isinstance(e.get("when"), str) and REPLAY_RED.fullmatch(e["when"])
                    for e in kill_entries
                )
                replay_kill_unprovable = has_replay_condition and ledger_stamp(loaded.root) is None
            return result, loaded, pkg_bindings, replay_kill_unprovable

        result, loaded, pkg_bindings, replay_kill_unprovable = await run_in_daemon_thread(_build, name="osca-load")
        if loaded is None:
            return {"ok": False, "detail": result.lines}

        # 先在局部构建全部运行时对象——任何一步失败都不触碰注册表（原子发布，杜绝半注册包）
        pid = loaded.package_id
        try:
            policy_file = loaded.pack.yaml_files.get("policy.yaml")
            policy = PolicyInterceptor(pid, policy_file.mapping if policy_file else {}, ledger_stats(loaded.pack))
            proxy = ConnectorProxy(loaded, pkg_bindings, policy)
            # precondition 绑**本代** proxy（跨代不外呼，GPT 三审 P1）——旧 Gate 的求值恒走旧代理，
            # unload 后旧 policy 已 revoke，授权层必拒
            gates = {
                aware.aware_id: Gate(
                    pid, aware, precondition_eval=lambda text, pr=proxy: self._eval_precondition(pr, text)
                )
                for aware in loaded.awares
            }
        except Exception as e:
            detail = f"✗ 运行时构建失败：{e}——包未注册（原子发布：构建不全不触碰注册表）"
            log.error(detail)
            return {"ok": False, "detail": [*result.lines, detail]}

        # 发布段进命令锁（短暂、纯状态变更）：注册表 + 笼子 + 闸门一起可见
        async with self._cmd_lock:
            if self.state is not HostState.RUNNING or self._deployment_generations.get(deployment_id) != generation:
                return {"ok": False, "detail": "load 准备完成时 generation 已失效，拒绝迟到发布"}
            if operation < self._package_tombstones.get(pid, 0):
                return {"ok": False, "detail": f"{pid} 已被更新的 unload tombstone 停止，拒绝旧 load 发布"}
            lines = result.lines + self.registry.register(loaded)
            if operation >= self._package_tombstones.get(pid, 0):
                self._package_tombstones.pop(pid, None)
            self._package_deployments[pid] = deployment_id
            self.bindings[pid] = pkg_bindings
            self.policies[pid] = policy
            self.proxies[pid] = proxy
            for aware_id, gate in gates.items():
                self.gates[(pid, aware_id)] = gate
            if policy.kill_tripped:
                lines.append(f"⚠ {policy.kill_reason}——包已装载但唤醒与调用全部拒绝（三级停语义，公理 A10）")
            # 部署契约提示（M4 前拍板）：zip 解压目录无 git 账本 → 回放红灯率条件永远 unavailable（默认不触发）
            if replay_kill_unprovable:
                lines.append(
                    "⚠ policy 声明了「回放红灯率」kill 条件，但包根不是 git 账本（zip 部署形态）——"
                    "该条件永远不可求值（unavailable 默认不触发）。生产账本建议以 git 目录部署并定期 checkup"
                )

            # 布防：enabled 的 Aware 逐条触发原语进触发表；任一条失败即补偿回滚——不留半装载包
            armed = 0
            try:
                for aware in loaded.awares:
                    if aware.enabled:
                        for t in aware.triggers:
                            self.table.subscribe(
                                t.kind,
                                t.spec,
                                Subscription(
                                    pid,
                                    aware.aware_id,
                                    t.trigger_id,
                                    self._make_deliver(pid, aware.aware_id),
                                    poll=self._make_poll(pid, proxy),
                                ),
                            )
                            armed += 1
            except Exception as e:
                self._unload(pid)  # 补偿回滚：撤已布防 watcher + 清笼子/闸门/binding + 注销
                detail = f"✗ 布防失败：{e}——已补偿回滚（发布与布防同生共死），包未装载"
                log.error(detail)
                return {"ok": False, "detail": [*result.lines, detail]}
            self._sync_slots(pid)
            lines.append(f"触发表布防 {armed} 条（schedule/watch 挂 watcher，event 待人工发射）")
        await self._reattach_suspensions(loaded.package_id)  # 装载后重挂持久化的挂起剧集（L2 活过重载/重启）
        for line in lines:
            log.info(line)
        return {"ok": True, "package_id": loaded.package_id, "detail": lines}

    def _unload(self, package_id: str) -> dict:
        self._operation_seq += 1
        self._last_unload_operation = self._operation_seq
        self._package_tombstones[package_id] = self._operation_seq
        deployment_id = self._package_deployments.pop(package_id, None)
        if deployment_id is not None:
            self._deployment_generations[deployment_id] = self._deployment_generations.get(deployment_id, 0) + 1
        removed = self.table.unsubscribe(package_id)
        for key in [k for k in self.gates if k[0] == package_id]:
            del self.gates[key]
        policy = self.policies.pop(package_id, None)
        if policy is not None:
            # 包停触达认知平面：在途剧集持有此 policy 引用——步间即停、后续调用全拒
            policy.revoke("unload 包停")
        # 挂起等批的剧集不在跑循环、看不到 revoked——显式迁 stopped（内存）并清 _suspensions 登记。
        # L2 磁盘快照**留盘不删**：同包重载时 _reattach 会重挂兑现（活过包重载）；L1（无持久层）则不恢复。
        self._stop_suspended_episodes(package_id, "包停（unload）：挂起迁 stopped；L2 快照留盘待重载重挂，L1 不恢复")
        self.proxies.pop(package_id, None)
        self.bindings.pop(package_id, None)  # binding 随包清理，不留给后来者
        # _deliver_locks 刻意**不随包清理**（GPT Review 复审 P1）：pop 后同 package_id 重载会造第二把锁，
        # 旧 generation 在途投递与新代不再互斥。锁按 package_id 常驻（有界：历史包 ID 个数），
        # 旧持锁者按代际 CAS 失配快速退出，新代投递等它归还同一把锁——互斥恒成立。
        lines = [f"触发表撤防 {len(removed)} 条"] + self.registry.unregister(package_id)
        for line in lines:
            log.info(line)
        return {"ok": True, "detail": lines}

    def _set_aware(self, package_id: str, aware_id: str, enabled: bool) -> dict:
        gate = self.gates.get((package_id, aware_id))
        pkg = self.registry.packages.get(package_id)
        if gate is None or pkg is None:
            return {"ok": False, "detail": f"未找到 {package_id} 的 {aware_id}"}
        aware = next(a for a in pkg.awares if a.aware_id == aware_id)
        if enabled and gate.enabled:
            return {"ok": True, "detail": f"{aware_id} 已是启用状态——幂等，不重复布防（防止双份订阅双份唤醒）"}
        if enabled:
            # 全部订阅成功才置 enabled：任一条失败即补偿回滚——不留「显示启用、实际半布防」的 Aware。
            # poll 绑当前在册（本代）proxy——enable 恒发生在本代存续期内
            try:
                for t in aware.triggers:
                    self.table.subscribe(
                        t.kind,
                        t.spec,
                        Subscription(
                            package_id,
                            aware_id,
                            t.trigger_id,
                            self._make_deliver(package_id, aware_id),
                            poll=self._make_poll(package_id, self.proxies.get(package_id)),
                        ),
                    )
            except Exception as e:
                self.table.unsubscribe(package_id, aware_id)  # 撤已布防的部分
                self._sync_slots(package_id)
                detail = f"触发器启失败：{e}——已补偿回滚（撤已布防部分），{aware_id} 保持停用、可重试"
                log.error(detail)
                return {"ok": False, "detail": detail}
            gate.enabled = True
            aware.enabled = True  # 声明结构跟运行态走（P2）：status 的 awares.enabled 不许与 Gate 自相矛盾
            detail = f"触发器启：{aware_id} 重新布防 {len(aware.triggers)} 条"
        else:
            gate.enabled = False
            aware.enabled = False  # 声明结构跟运行态走（P2）
            # disable 边界（P1）：代际递增使所有旧代在途投递永久失效（撑过 disable→enable 也不发布）；
            # 组合闸门的部分推进状态（all 已见/sequence 指针）一并清除——半程状态不跨启停边界
            key = (package_id, aware_id)
            self._aware_generations[key] = self._aware_generations.get(key, 0) + 1
            gate.reset_progress()
            removed = self.table.unsubscribe(package_id, aware_id)
            detail = f"触发器停：{aware_id} 撤防 {len(removed)} 条（三级停之二）"
        self._sync_slots(package_id)
        log.info(detail)
        return {"ok": True, "detail": detail}

    async def _fire(self, package_id: str, trigger_id: str) -> dict:
        error = await self.table.fire_manual(package_id, trigger_id)
        if error:
            return {"ok": False, "detail": error}
        return {"ok": True, "detail": f"已人工发射 {trigger_id}（裁决见 Host 日志与 status.gates）"}

    def _make_deliver(self, package_id: str, aware_id: str):
        # Aware 代际**固化于订阅闭包创建时**（复核 P1）：若在 deliver 开始执行时才读当前代际，
        # 共享 watcher 的派发快照里排队的旧订阅回调可能在 disable→enable **之后**才开始执行——
        # 那时读到的已是新代际，CAS 形同虚设。闭包创建即持永久 token：enable 重建的新订阅拿
        # 新代际，旧订阅（无论何时才轮到执行）恒持旧代际、恒被拒。
        subscription_generation = self._aware_generations.get((package_id, aware_id), 0)

        async def deliver(trigger_id: str) -> str | None:
            # 投递的两段重活（GPT Review P1 事件循环阻塞）都下线程：账本刷新是磁盘重活（全量 lint +
            # 索引重建），闸门裁决内含 precondition 真取数（经**本代** Connector 代理走真实网络）——原先
            # 直接跑在事件循环上，一次外呼/慢盘就压住控制通道（status/stop/审批）。同包投递按包锁串行：
            # 保住账本非阻塞 flock 的旧序语义（并发刷新会互踩误拒唤醒），闸门状态机也因此无并发写。
            #
            # 生命周期 CAS（GPT Review 复审 P1 + 三审 P1）：重活下线程后，unload/同 id reload/**stop**
            # 可在任一 await 期间发生——旧 generation 投递若继续裁决、再从注册表取到新包装配，或在
            # DRAINING 后发布新剧集，都违反「迟到发布必见 tombstone」。故进锁后按对象身份取齐三件套，
            # **进锁即查 + 每个 await 返回后复核**（HostState 一并纳入），失效即整体放弃、绝不发布。
            # 返回值 = 未发布原因（None=正常投递完成）——人工 fire 据此如实报失败。
            async with self._deliver_locks.setdefault(package_id, asyncio.Lock()):
                gate = self.gates.get((package_id, aware_id))
                policy = self.policies.get(package_id)
                loaded = self.registry.packages.get(package_id)
                if gate is None:
                    return f"{package_id}/{aware_id} 未布防或已卸载——投递不发布"

                def stale() -> str | None:
                    if self.state is not HostState.RUNNING:
                        return f"Host 已 {self.state.name}——迟到投递不发布（tombstone 生效）"
                    if (
                        self.gates.get((package_id, aware_id)) is not gate
                        or self.policies.get(package_id) is not policy
                        or self.registry.packages.get(package_id) is not loaded
                    ):
                        return "包已卸载/重载——跨代投递不发布"
                    # 订阅创建时固化的代际 vs 当前代际：disable 递增后旧订阅永久失效——
                    # 即便旧回调排队到 enable 之后才开始执行也拿不到新代际（复核 P1）
                    if self._aware_generations.get((package_id, aware_id), 0) != subscription_generation:
                        return "Aware 已停用（disable）——旧代订阅永久失效，不发布"
                    return None

                if why := stale():  # 进锁即查：fire 短锁状态检查与投递之间的 stop/unload TOCTOU
                    log.info(f"[{package_id}/{aware_id}] {trigger_id} 投递放弃：{why}")
                    return why
                if (
                    policy
                    and loaded
                    and not await run_in_daemon_thread(self._refresh_ledger, loaded, policy, name="osca-ledger")
                ):
                    log.warning(
                        f"[{package_id}/{aware_id}] {trigger_id} 命中 → 账本刷新失败，本次唤醒拒绝（保留旧快照）"
                    )
                    return None
                if why := stale():
                    log.info(f"[{package_id}/{aware_id}] {trigger_id} 投递放弃：{why}")
                    return why
                if policy and policy.kill_tripped:
                    log.warning(f"[{package_id}/{aware_id}] {trigger_id} 命中 → 拒绝唤醒：{policy.kill_reason}")
                    return None
                woke, verdict = await run_in_daemon_thread(gate.on_trigger, trigger_id, name="osca-gate")
                if why := stale():
                    log.info(f"[{package_id}/{aware_id}] {trigger_id} 投递放弃：{why}")
                    return why
                log.info(f"[{package_id}/{aware_id}] {trigger_id} 命中 → {verdict}")
                if woke:
                    self._assemble_episode(package_id, aware_id, trigger_id)
                return None

        return deliver

    def _make_poll(self, package_id: str, proxy):
        """watch 轮询回调——**捕获本代 proxy**（GPT Review 三审 P1）：旧 watcher 的在途轮询绝不动态取
        `self.proxies[package_id]`（unload+reload 后那是**新代**代理，会对新 binding 产生陈旧外呼）。
        unload 时本代 policy 已 revoke，本代 proxy.call 在授权层被拒——外呼前的复核由授权强制点承担
        （身份快查只是省一次必拒调用），fail-closed 无竞态窗。"""

        def poll(uses: str):
            if proxy is None or self.proxies.get(package_id) is not proxy:
                return None  # 本代已下线：快路径不再发起（即便发起，本代 revoked policy 也必拒）
            receipt = proxy.call(uses, step=None)
            if not receipt.ok:
                log.warning(f"[{package_id}] 轮询取数失败：{receipt.error}")
                return None
            payload = receipt.payload
            return payload if isinstance(payload, dict) else {"value": payload}

        return poll

    def _refresh_ledger(self, loaded, policy: PolicyInterceptor) -> bool:
        """唤醒前把账本刷成磁盘现状（持账本写锁，与 oscapipe 写入者互斥）。

        读取 → lint 校验 → 重建签名表 → 算 kill switch 输入，全部成功才原子替换
        loaded.pack；锁忙（写入者事务进行中）或账本不合规 → 保留旧快照并拒绝本次
        唤醒——宁可拒绝，不可用半截账本装配剧集。触发命中才刷新，成本与唤醒同频。
        """
        try:
            with ledger_lock(loaded.root, blocking=False):
                fresh = load_package(loaded.root)
                errors = [f for f in run_all(fresh) if f.severity is Severity.ERROR]
                if errors:
                    head = "；".join(f"{f.rule} {f.message}" for f in errors[:3])
                    log.warning(f"[{loaded.package_id}] 账本刷新失败（lint {len(errors)} 错误：{head}）——保留旧快照")
                    return False
                rebuild_index(loaded.root, fresh)
                # kill switch 在保护区内纯计算（三态）——评估异常也走下面的兜底，旧 pack/旧 policy 原样
                kill_state, kill_reason = policy.evaluate_kill_switch(ledger_stats(fresh))
        except LedgerLockBusy:
            log.warning(f"[{loaded.package_id}] 账本写锁被占用（写入者事务进行中）——保留旧快照")
            return False
        except Exception:
            # 刷新是安全边界：磁盘满/权限/索引重建失败等一律不许穿透——穿透会杀死
            # 共享 watcher 的循环任务，修好磁盘也不会自然再试。留完整异常，拒绝本次唤醒。
            log.exception(f"[{loaded.package_id}] 账本刷新异常——保留旧快照，本次唤醒拒绝")
            return False
        # 发布：pack 替换与三态发布配对生效——unavailable 保留既有安全状态（缺口不洗红灯）
        loaded.pack = fresh
        policy.publish_kill_switch(kill_state, kill_reason)
        return True

    # ── 运行时内部取数（precondition，经**本代** Connector 代理 + Policy） ──

    def _eval_precondition(self, proxy, text: str) -> tuple[bool | None, str]:
        """precondition 求值——proxy 是 Gate 构建时**捕获的本代代理**（GPT Review 三审 P1）：
        不动态取 self.proxies[package_id]（unload+reload 后那是新代，旧投递会对新 binding 陈旧外呼、
        甚至在新 Policy 里长出无主审批挑战）。unload 即 revoke 本代 policy，旧代外呼在授权层必拒。"""
        parsed = parse_precondition(text)
        if parsed is None:
            return None, "不可求值（受限形式：CON-xxx.接口(参数) 返回非空），默认放行"
        connector_id, interface, params = parsed
        if proxy is None:
            return None, "Connector 代理未就绪，默认放行"
        receipt = proxy.call(f"{connector_id}.{interface}", params, step=None)
        if not receipt.ok:
            return False, f"取数失败（{receipt.error}）"
        payload = receipt.payload
        empty = payload is None or (hasattr(payload, "__len__") and len(payload) == 0)
        if empty:
            return False, f"{connector_id}.{interface}({params}) 返回为空"
        return True, f"求值通过（{connector_id}.{interface}({params}) 返回非空）"

    def _assemble_episode(self, package_id: str, aware_id: str, trigger_id: str) -> None:
        loaded = self.registry.packages.get(package_id)
        proxy = self.proxies.get(package_id)
        policy = self.policies.get(package_id)
        if loaded is None or proxy is None or policy is None:
            return
        aware = next(a for a in loaded.awares if a.aware_id == aware_id)
        self._episode_seq += 1
        episode = assemble(f"EP-{self._episode_seq:04d}", loaded, aware, trigger_id)
        self.episodes[episode.episode_id] = episode
        self._evict_old_episodes()  # 淘汰最旧终态剧集；挂起等批剧集免淘汰（否则已批写随淘汰静默丢弃）
        self._sweep_suspensions()  # 每次唤醒顺带清扫过期/已决挂起（§5.4）
        s = episode.summary()
        log.info(
            f"剧集 {episode.episode_id} 装配完成：判断 {len(s['judgments'])} 条（{', '.join(s['judgments'])}）"
            f" / 对象 {len(s['objects'])} 个 / 预算 {episode.budget}，开始执行"
        )
        # 认知平面在独立线程执行（LLM 阻塞调用），Host 事件循环保持确定性响应。
        # loaded/proxy/policy 在此刻捕获——执行中途包停也不半路丢引用。
        task = asyncio.create_task(self._execute_episode(episode, loaded, proxy, policy))
        self._episode_tasks.add(task)
        task.add_done_callback(self._episode_tasks.discard)

    async def _run_in_daemon_thread(self, fn, *args):
        """认知平面重活（run_episode/settle）跑在**守护线程**（P1 关停语义）：统一有界执行模型见
        osca_host.threads——默认执行器会让卡死线程阻塞 asyncio.run 收尾与进程退出。「STOPPED 后
        无迟到副作用」由副作用强制点保证：关停逐包 revoke，迟到线程的外呼/LLM/落账在授权层全拒。"""
        return await run_in_daemon_thread(fn, *args, name="osca-episode")

    async def _execute_episode(self, episode: Episode, loaded, proxy, policy) -> None:
        try:
            await self._run_in_daemon_thread(run_episode, episode, loaded, proxy, policy)
        except Exception:
            # 执行器内部错误不许让剧集永远停在 running——终态入台账，异常进日志
            episode.status = "failed"
            episode.stop_reason = "执行器内部错误（见 Host 日志）"
            episode.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            log.exception(f"剧集 {episode.episode_id} 执行器内部错误")
        if episode.status == "suspended_pending_approval":
            # 挂起等批（可恢复剧集）：登记 + 登记侧自愈（§3.5）；**不落终态、不对账**——等 approve/deny/清扫触发恢复。
            # 登记（状态变更）留在事件循环；L2 持久的磁盘重活（全包指纹 + fsync）由 _persist_suspension 下线程
            if self._register_suspension(episode, loaded, proxy, policy):
                await self._persist_suspension(episode, policy)
            return
        tail = f"（{episode.stop_reason}）" if episode.stop_reason else ""
        log.info(f"剧集 {episode.episode_id} 终态 {episode.status}{tail}：tokens {episode.tokens_used}")
        if episode.status != "completed":
            return
        if any(s.get("status") == "denied" for s in episode.steps):
            # 写审批驳回/过期 → 回落保守默认（未写）：保守态不对账，不落 outcome case（§3.7，回落不误采集）
            log.info(f"剧集 {episode.episode_id} 含写回落（保守默认未写）——不对账，不落 outcome case")
            return
        # 对账器（组件 7）：objective 型对象自动落 outcome case，不消耗剧集
        try:
            for entry in await self._run_in_daemon_thread(settle_episode, loaded, proxy, episode):
                if entry["settled"]:
                    log.info(f"对账落账：{entry['object']} → {entry['case']}（现实是第二位专家，公理 A2）")
                else:
                    log.info(f"对账未执行：{entry['object']}——{entry['note']}")
        except Exception:
            log.exception(f"剧集 {episode.episode_id} 对账器内部错误（剧集本身已 completed）")

    # ── 可恢复剧集编排（D2a）：挂起登记 / 恢复调度 / 惰性清扫 ──────────────

    def _register_suspension(self, episode: Episode, loaded, proxy, policy) -> bool:
        """登记挂起剧集（challenge_id → episode_id）+ **登记侧自愈**（§3.5 blocker）：登记同一事件循环临界区内
        立即复查挑战当前态，若已 approve/deny/expire（决定先到、登记后到的丢唤醒窗），就地调度一次恢复。
        返回 True = 挑战仍 pending、已登记，调用方应做 L2 持久（_persist_suspension，磁盘重活不上事件循环）。"""
        cid = episode.resume.get("challenge_id") if episode.resume else None
        if cid is None:  # 挂起却无 challenge_id——内部不一致，防御性收尾成 failed（不静默悬挂）
            episode.status = "failed"
            episode.stop_reason = "挂起态缺 challenge_id（内部不一致）"
            episode.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            return False
        if self.episodes.get(episode.episode_id) is not episode:
            # 剧集已不在台账（被淘汰/替换）——登记也无从恢复（_schedule_resume 找不到它）。收尾成 failed，
            # 不留悬空 _suspensions（否则已批写永不兑现且无报错）。有 _evict 免淘汰在途/挂起后此路应不可达，留作兜底。
            episode.status = "failed"
            episode.stop_reason = "剧集已不在台账，挂起无法登记恢复（避免悬空、不静默丢已批写）"
            episode.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            log.error(f"剧集 {episode.episode_id} 挂起时已不在台账——拒绝登记恢复")
            return False
        self._suspensions[cid] = episode.episode_id
        log.info(f"剧集 {episode.episode_id} 挂起等批（挑战 {cid}）")
        ch = policy.get_challenge(cid)
        if ch is None or ch.state != "pending":  # 决定已先到（丢唤醒窗）→ 就地自愈恢复（马上恢复，不落盘）
            self._schedule_resume(episode.episode_id, loaded, proxy, policy)
            return False
        return True  # 挑战仍 pending → 调用方做 L2 持久（活过包重载 / Host 重启）

    def _schedule_resume(self, episode_id: str, loaded, proxy, policy) -> None:
        """调度一次恢复（幂等）：真正的「删盘 → CAS → 起写线程」在异步状态机 _resume_after_delete 里——
        删盘可能等一次 fsync（与在途 persist 争 operation 锁），**不许上事件循环**（GPT 三审 P2：同步
        delete 会把审批/status/stop 一起压住 fsync 时长，存储故障时无上界）。_resuming 防同剧集双恢复。"""
        episode = self.episodes.get(episode_id)
        if episode is None or episode.status != "suspended_pending_approval":
            return  # 已淘汰/已恢复/已终态——放弃（幂等）
        if episode_id in self._resuming:
            return  # 已有恢复在途（删盘在线程中）——放弃（幂等，防双恢复）
        self._resuming.add(episode_id)
        task = asyncio.create_task(self._resume_after_delete(episode_id, loaded, proxy, policy))
        self._episode_tasks.add(task)
        task.add_done_callback(self._episode_tasks.discard)

    async def _resume_after_delete(self, episode_id: str, loaded, proxy, policy) -> None:
        """恢复状态机：生命周期 CAS → 删盘（线程）→ **再** CAS → suspended→running（事件循环）→
        内联 await 写执行。崩溃安全序不变（delete-before-write）：① 崩溃于「写已落地、终态未记」不会
        重挂重批重写（快照早没了，§2.4）；② 删盘失败（磁盘/权限）保留挂起态、不提交 running（假
        running 永卡，GPT 外审 P1），待清扫重试。

        生命周期 CAS（GPT 四审 P1）：只查 episode.status 关不住 DRAINING——_shutdown 先等 _episode_tasks
        再 _stop_suspended_episodes，删盘期间进入 DRAINING 时剧集仍 suspended，旧 CAS 会成功推进 running
        并**派生逃出 shutdown 快照的子任务**（Host 已报 STOPPED 而写执行还活着）。故删盘前后都复核
        HostState + loaded/proxy/policy 代际身份 + policy 未 revoke；失效即放弃、绝不起执行。写执行改
        **内联 await**（不再 create_task）——本恢复任务自身在 _episode_tasks 里，shutdown 等到的就是
        全程（无子任务逃逸面）。删盘后失效 = 快照已删、写不执行（fail-closed 丢写，审计可见——与旧序
        「CAS 后 unload、恢复被 revoked 拒绝」同向）；删盘前失效 = 快照保留，重载后可重挂。"""

        def lifecycle_stale() -> str | None:
            if self.state is not HostState.RUNNING:
                return f"Host 已 {self.state.name}（DRAINING 后不再启动新工作）"
            episode = self.episodes.get(episode_id)
            if episode is None:
                return "剧集已不在台账"
            pid = episode.package_id
            if (
                self.registry.packages.get(pid) is not loaded
                or self.policies.get(pid) is not policy
                or self.proxies.get(pid) is not proxy
            ):
                return "包已卸载/重载（跨代不恢复）"
            if policy.revoked:
                return f"包已停：{policy.revoked}"
            return None

        try:
            episode = self.episodes.get(episode_id)
            if episode is None or episode.status != "suspended_pending_approval":
                return
            if why := lifecycle_stale():
                log.info(f"剧集 {episode_id} 恢复放弃（删盘前）：{why}——快照保留，重载后可重挂")
                return
            if self._suspension_store is not None:
                try:
                    await run_in_daemon_thread(self._suspension_store.delete, episode.operation_id, name="osca-susp")
                except OSError as e:
                    log.warning(f"删除挂起快照失败，保留挂起态待清扫重试（不推进假 running）：{episode_id}（{e}）")
                    return
            if episode.status != "suspended_pending_approval":  # 删盘线程期间 unload/关停已迁 stopped
                log.warning(f"剧集 {episode_id} 恢复放弃：删盘期间已离开挂起态（{episode.status}）")
                return
            if why := lifecycle_stale():
                log.warning(f"剧集 {episode_id} 恢复放弃（删盘后失效）：{why}——快照已删、写不执行（fail-closed）")
                return
            episode.status = "running"  # CAS：删盘成功且生命周期有效才提交（与 pop 间无 await，循环上原子）
            cid = episode.resume.get("challenge_id") if episode.resume else None
            if cid is not None:
                self._suspensions.pop(cid, None)
        finally:
            self._resuming.discard(episode_id)
        await self._execute_episode(episode, loaded, proxy, policy)

    async def _persist_suspension(self, episode: Episode, policy: PolicyInterceptor) -> None:
        """把挂起剧集快照原子写盘（L2）：{episode 全 dump + 关联挑战全字段 + per_episode 计数 + 版本戳}。
        运行目录未就绪（单元测试直连）/ 整份快照（含 context）非 JSON 可序列化 / 落盘 I/O 失败 → 静默退回
        L1（不炸、不改数）。

        重活下线程（GPT Review P2 异步隔离）：版本戳要哈希全包源文件、persist 要 fsync——都不上事件循环。
        快照内容（dump/计数）仍在事件循环取（与登记同临界区一致）。落盘期间决定已到的竞态由 store 的
        **删除世代令牌**关死（GPT Review 复审 P1）：发起时取令牌，_schedule_resume 的 delete 令世代 +1，
        迟到落盘在存储层锁内复核令牌失配即弃写——「delete 未见文件 → 真写落地 → 迟到快照落盘 → 崩溃 →
        重挂重批重复写」的窗不复存在（作废发生在写盘**之前**，不靠事后补删）。unload 不 delete：
        快照照常落地留盘待重载重挂（保留与作废按调用方语义区分，不再看 episode.status 猜）。"""
        store = self._suspension_store
        cid = episode.resume.get("challenge_id") if episode.resume else None
        ch = policy.get_challenge(cid) if cid else None
        if store is None or ch is None:
            return
        loaded = self.registry.packages.get(episode.package_id)
        tool_calls, tokens = policy.episode_budget_used(episode.episode_id)
        record = {
            "operation_id": episode.operation_id,
            "package_id": episode.package_id,
            "episode": episode.dump(),
            "challenge": asdict(ch),
            "tool_calls": tool_calls,
            "tokens": tokens,
            "version_stamp": None,  # 线程内补算（全包指纹是磁盘重活）
        }
        ticket = store.begin_persist(episode.operation_id)  # 事件循环侧领票——先于任何可能的 delete 观察点

        def _stamp_and_persist() -> None:
            record["version_stamp"] = self._pack_stamp(loaded) if loaded is not None else None
            store.persist(episode.operation_id, record, ticket=ticket)

        try:
            await run_in_daemon_thread(_stamp_and_persist, name="osca-susp")
        except OSError as e:  # 磁盘满/权限/fd 已关等——真正退回 L1（对抗审查 minor-1：不炸成未处理 task 异常）
            log.warning(f"挂起快照落盘失败（该剧集退回 L1、不活过重载/重启）：{e}")
            # 异常可能发生在 persist 之前（指纹计算）——兜底归还在途凭据。凭据释放**幂等**（GPT 四审
            # P2）：persist 已跑过则它自己已归还、本调用 no-op，绝不偷走并发 delete/新一代的票（ABA 已封）
            store.abandon_persist(ticket)

    @staticmethod
    def _pack_stamp(loaded) -> str:
        """包版本戳 = **源文件内容指纹**（sha256 of 排序后的「相对路径 + 字节」），排除 `.git/`（版本控制内部）
        与 `indexes/`（装载重建的缓存，与包身份无关）。

        为何不用 git tree OID：OID 只反映 **已提交** 内容——直接改 policy.yaml/pipeline/connector 而不提交，
        HEAD tree 不变，旧快照会重挂到新运行语义（GPT 外审 P1）。内容指纹按**实际工作树字节**计，未提交改动
        照样变戳。重挂时严格比对，任一漂移即 fail-closed 丢弃（§2.4）。持久/重挂皆低频（挂起时 / 装载时），
        小包成本可忽略。"""
        root = loaded.root
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            parts = p.relative_to(root).parts
            if p.is_dir() or ".git" in parts or "indexes" in parts:
                continue
            h.update(p.relative_to(root).as_posix().encode("utf-8") + b"\0")
            with contextlib.suppress(OSError):
                h.update(p.read_bytes())
            h.update(b"\0")
        return "fp:" + h.hexdigest()

    async def _reattach_suspensions(self, package_id: str) -> None:
        """package 装载后重挂其持久化的挂起剧集（L2）：读盘 → 版本戳/结构校验 → 重建剧集 + 挑战 + 计数 →
        **重编无冲突展示号**（真键 operation_id）→ 加回台账与 _suspensions → sweep（已决/过期的立即兑现/回落）。
        包重载与 Host 重启同此一条路径。快照里的挑战恒 pending（决定一到即删盘），故重挂后仍等审批人重发/清扫。
        读盘 + 全包指纹是磁盘重活，下线程（GPT Review P2 异步隔离）；台账/授权状态机的变更回事件循环单线程做。
        线程返回后**代际 CAS**（GPT Review 复审 P1）：读盘期间 unload/reload/关停可已发生——loaded/policy/store
        对象身份任一失配即整体放弃、不向 episodes/_suspensions/ChallengeStore 写入任何状态（跨代不发布）。"""
        store = self._suspension_store
        loaded = self.registry.packages.get(package_id)
        policy = self.policies.get(package_id)
        if store is None or loaded is None or policy is None:
            return
        records, current_stamp = await run_in_daemon_thread(
            lambda: (store.load_all(), self._pack_stamp(loaded)), name="osca-susp"
        )

        def lifecycle_stale() -> bool:
            return (
                self.state is not HostState.RUNNING
                or self.registry.packages.get(package_id) is not loaded
                or self.policies.get(package_id) is not policy
                or self._suspension_store is not store
            )

        if lifecycle_stale():
            log.info(f"[{package_id}] 重挂放弃：读盘期间包已卸载/重载或 Host 关停（跨代不发布）")
            return
        # 三段式（GPT 四审 P2）：① 事件循环上纯裁决（零文件操作）→ ② 丢弃项**线程批量删**
        # （delete 可能等在途 persist 的 fsync，同步调会压住整条控制通道）→ ③ 删后再代际 CAS、发布重挂。
        now = time.time()
        to_delete: list[str] = []
        keep: list[tuple[Episode, Challenge, int, int]] = []
        for record in records:
            if not isinstance(record, dict):
                continue  # 合法 JSON 但非 mapping——坏文件跳过，不崩启动（major-1；load_all 已滤，双保险）
            opid = str(record.get("operation_id", ""))
            # GC：任意包的挑战过期超保留期 → 顺带清盘（防永不重载包的孤儿快照无限堆积，minor-2）
            ch_data = record.get("challenge")
            expires = ch_data.get("expires_at") if isinstance(ch_data, dict) else None
            stale = isinstance(expires, (int, float)) and not isinstance(expires, bool)
            if stale and now > expires + TERMINAL_RETENTION_SECONDS:
                to_delete.append(opid)
                continue
            if record.get("package_id") != package_id:
                continue
            try:
                episode = Episode(**record["episode"])
                challenge = Challenge(**record["challenge"])
            except (TypeError, KeyError) as e:
                log.warning(f"挂起快照结构不符，丢弃：{opid}（{e}）")
                to_delete.append(opid)
                continue
            if record.get("version_stamp") != current_stamp:
                # 版本戳不符（含旧快照 None vs 现非 None）→ 包已改版 / 不可证同版 → fail-closed 丢弃（§2.4）
                log.warning(f"挂起快照与当前包版本不符（包已改版），丢弃不重挂：{opid}")
                to_delete.append(opid)
                continue
            raw_tc, raw_tk = record.get("tool_calls", 0), record.get("tokens", 0)
            if type(raw_tc) is not int or raw_tc < 0 or type(raw_tk) is not int or raw_tk < 0:
                # 计数字段损坏（非非负整数）→ 快照不可信，与结构不符同口径丢弃（fail-closed：
                # 按 0 回灌会把 INV-7 的硬顶在重挂边界洗掉，硬转 int 又会炸掉整个装载）
                log.warning(f"挂起快照 per_episode 计数损坏，丢弃：{opid}")
                to_delete.append(opid)
                continue
            keep.append((episode, challenge, raw_tc, raw_tk))
        if to_delete:
            try:
                await run_in_daemon_thread(lambda: [store.delete(o) for o in to_delete], name="osca-susp")
            except OSError as e:
                # delete 现在含目录 fsync（P1）：存储故障时批量清理可失败——放弃本次重挂（快照仍在盘，
                # 下次装载再试），绝不带着「删没删成不确定」发布重挂
                log.warning(f"[{package_id}] 废弃挂起快照清理失败，本次重挂放弃（下次装载重试）：{e}")
                return
            if lifecycle_stale():
                # 丢弃清理已做（本就是废快照，删了不冤）；重挂发布整体放弃——保留项快照仍在盘，下次重挂再续
                log.info(f"[{package_id}] 重挂放弃：清理期间包已卸载/重载或 Host 关停（跨代不发布）")
                return
        for episode, challenge, raw_tc, raw_tk in keep:
            # 重编无冲突展示号（blocker）：EP 号只是展示、跨重启复用低号会与另一 operation_id 相撞（静默顶掉
            # 活挂起写、错接挑战）。operation_id 才是真键。挑战仍 pending（未决），故按新号重绑其 episode_id——
            # payload_digest 不变、恢复用新号消费仍命中；challenge_id 不变（审批人按它批）。
            self._episode_seq += 1
            new_id = f"EP-{self._episode_seq:04d}"
            episode.episode_id = new_id
            challenge = replace(challenge, episode_id=new_id)
            policy.restore_challenge(challenge)  # 注回授权状态机（过期由既有 gc 迁 EXPIRED、恢复走回落）
            policy.restore_episode_budget(new_id, raw_tc, raw_tk)
            episode.status = "suspended_pending_approval"  # 快照必为 pending 挑战——重挂即挂起态
            self.episodes[new_id] = episode
            self._suspensions[challenge.challenge_id] = new_id
        if keep:
            log.info(f"[{package_id}] 重挂 {len(keep)} 条挂起剧集（L2 活过重载/重启）")
            self._sweep_suspensions()  # 已决（含丢唤醒窗）/ 过期的立即恢复：兑现或回落

    def _maybe_resume_for_challenge(self, challenge_id: str, package_id: str) -> None:
        """approve/deny 裁决成功后：有挂起剧集在等这张挑战即调度恢复（approve 兑现 / deny 回落）。"""
        episode_id = self._suspensions.get(challenge_id)
        if episode_id is None:
            return  # 无挂起剧集等它（或登记侧自愈已处理，或已淘汰）
        loaded = self.registry.packages.get(package_id)
        proxy = self.proxies.get(package_id)
        policy = self.policies.get(package_id)
        if loaded is not None and proxy is not None and policy is not None:
            self._schedule_resume(episode_id, loaded, proxy, policy)

    def _evict_old_episodes(self) -> None:
        """台账超顶只淘汰最旧的**终态**（completed/stopped/failed）剧集；在途/挂起（assembled/running/
        suspended_pending_approval）一律**免淘汰**（对抗审查 major-2）——否则在途或挂起的写剧集被 FIFO 淘汰出
        台账后，其已批高危写永不兑现且无报错（击穿 INV-2）。backlog 时台账暂时超顶，可接受。"""
        while len(self.episodes) > EPISODE_LEDGER_CAP:
            victim = next(
                (eid for eid, ep in self.episodes.items() if ep.status in ("completed", "stopped", "failed")), None
            )
            if victim is None:
                return  # 无终态可淘汰（全在途/挂起）——宁可超顶，不丢在途/已批写
            del self.episodes[victim]

    def _sweep_suspensions(self) -> None:
        """惰性清扫（§5.4）：凡挂起剧集其挑战已离开 pending（approved/denied/expired/revoked/已清出）→ 调度一次
        恢复（走 §5.2 分派）。堵「无决定超时（TTL 过期无事件）」+ 丢唤醒窗第二重保险。"""
        for cid, episode_id in list(self._suspensions.items()):
            episode = self.episodes.get(episode_id)
            if episode is None or episode.status != "suspended_pending_approval":
                self._suspensions.pop(cid, None)  # 已淘汰/已恢复/已终态——清出登记
                continue
            policy = self.policies.get(episode.package_id)
            loaded = self.registry.packages.get(episode.package_id)
            proxy = self.proxies.get(episode.package_id)
            if policy is None or loaded is None or proxy is None:
                continue
            ch = policy.get_challenge(cid)
            if ch is None or ch.state != "pending":
                self._schedule_resume(episode_id, loaded, proxy, policy)

    def _stop_suspended_episodes(self, package_id: str | None, reason: str) -> None:
        """把挂起等批的剧集迁 stopped 并清出 _suspensions（§3.4 包停/关停）——package_id=None 为全体（关停）。
        诚实标注：L1 不持久，挂起态随包/进程消亡、重启不恢复（活过重载/重启是 L2 的事）。"""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for episode in self.episodes.values():
            if episode.status != "suspended_pending_approval":
                continue
            if package_id is not None and episode.package_id != package_id:
                continue
            episode.status = "stopped"
            episode.stop_reason = reason
            episode.finished_at = now
            cid = episode.resume.get("challenge_id") if episode.resume else None
            if cid is not None:
                self._suspensions.pop(cid, None)

    def _sync_slots(self, package_id: str) -> None:
        """注册表槽位状态跟随布防事实：armed（已挂 watcher/待人工发射）或 disabled。"""
        pkg = self.registry.packages.get(package_id)
        if pkg is None:
            return
        enabled_by_aware = {(a.aware_id): self.gates[(package_id, a.aware_id)].enabled for a in pkg.awares}
        for slot in self.registry.watchers.get(package_id, []):
            aware_id = slot.trigger_id.split("/", 1)[0]
            slot.state = "armed" if enabled_by_aware.get(aware_id) else "disabled"

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        """幂等关停：先封发布，再清退 load/剧集/控制请求，最后释放 socket/lock/runtime fd。"""
        if self.state is HostState.STOPPED:
            return
        async with self._cmd_lock:
            self._begin_draining()
        load_tasks = {slot[2] for slot in self._load_slots.values() if not slot[2].done()}
        for task in load_tasks:
            task.cancel()
        if load_tasks:
            await asyncio.gather(*load_tasks, return_exceptions=True)

        # 先等在跑剧集收尾（剧集短命，正常秒级；卡死时按上限放弃等待）。
        # **循环重拍快照直到集合为空**（GPT 四审 P1）：一次性快照会漏等等待期间新登记的任务
        # （恢复状态机已内联 await 消除子任务派生，这里是第二道保险——迟到任务不逃出关停）
        deadline = time.monotonic() + self._episode_shutdown_timeout
        while self._episode_tasks and time.monotonic() < deadline:
            log.info(f"关停前等待 {len(self._episode_tasks)} 个在跑剧集收尾")
            await asyncio.wait(list(self._episode_tasks), timeout=max(0.1, deadline - time.monotonic()))
        if self._episode_tasks:
            # 超时诚实口径（P1）：剧集线程是守护线程，随本次进程退出被终止——STOPPED 报出后进程
            # **真的**会退出（asyncio.run 不再被不可取消线程卡住），不存在「socket 已删、进程仍活、
            # 卡死写还在跑」的假关停。残余半写属硬件半写同类，归写幂等键界定（W6 §8-5）。
            log.warning(
                f"{len(self._episode_tasks)} 个剧集未在 {self._episode_shutdown_timeout:.0f}s 内收尾——"
                "放弃等待；其守护线程随进程退出终止，不阻塞关停"
            )
        # 挂起等批的剧集无 task（INV-1 不持线程）→ 不会被上面等到；显式迁 stopped（L1 不持久，重启不恢复）
        self._stop_suspended_episodes(None, "Host 关停，挂起等批未兑现（L1 不持久，重启不恢复）")

        # 关停 = 全体包停：逐包撤防注销（三级停之「包停」的机制复用）
        for package_id in list(self.registry.packages):
            for line in self._unload(package_id)["detail"]:
                log.info(line)
        self.table.shutdown()
        await self.control.close()
        self._load_slots.clear()
        self._deployment_locks.clear()
        self._deployment_generations.clear()
        self._package_deployments.clear()
        self._package_tombstones.clear()
        self._control_tasks.clear()
        self.state = HostState.STOPPED
        log.info("osca-host 已退出")

    async def run(self, initial_packs: list[dict] | None = None) -> int:
        # 安全内核先立：私有运行目录（不跟随符号链接）→ principal 签发面 → 传输
        # （实例锁 + dev 0600 / prod 0660 socket）。任一步非法都拒绝启动，不降级。
        try:
            runtime = self.control.prepare_runtime()
            self._suspension_store = SuspensionStore(runtime.fd)  # 挂起快照落这个 fd 锚定的运行目录（L2）
            token_file = admin_token_path(self.control.socket_path)
            # admin token 绑定 Host 自己的 uid：生产模式下别的 uid 偷到也当不了 admin
            admin = Principal("local-admin", "host_admin", os.getuid())
            self.authorizer.register(
                ensure_admin_token(token_file.name, dir_fd=runtime.fd),
                admin,
            )
            issued = load_principals(
                principals_path(self.control.socket_path).name,
                self.authorizer,
                dir_fd=runtime.fd,
                production=runtime.production,
            )
            await self.control.start()
        except asyncio.CancelledError:
            # run() 可能在控制通道完全发布前被嵌入方取消；此时还没进入下面的
            # 主循环 finally，必须在这里释放已锚定的 runtime fd / listener / flock。
            await asyncio.shield(self.control.close())
            self.state = HostState.STOPPED
            raise
        except Exception as e:
            log.error(f"控制通道启动失败：{e}")
            await self.control.close()
            self.state = HostState.STOPPED
            return 1
        self.state = HostState.RUNNING
        log.info(
            f"osca-host {__version__} 就绪，控制通道：{self.control.socket_path}"
            f"（admin token：{token_file}；部署签发 principal {issued} 个；部署清单 {len(self.deployments)} 条）"
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._begin_draining)

        try:
            failed = 0
            for index, pack in enumerate(initial_packs or []):
                try:
                    response = await self._request_load(f"__initial__:{index}", pack)
                except RegistryError as e:
                    response = {"ok": False, "detail": [str(e)]}
                if not response["ok"]:
                    failed += 1
                    detail = response["detail"]
                    for line in detail if isinstance(detail, list) else [str(detail)]:
                        log.error(line)
                    log.error(f"启动装载失败：{pack['path']}")

            await self._stop.wait()
            return 1 if failed else 0
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(sig)
            await asyncio.shield(self._shutdown())


def run_host(
    socket_path: Path,
    initial_packs: list[dict] | None = None,
    deployments: dict[str, dict] | None = None,
    control_group: str | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(Host(socket_path, deployments, control_group).run(initial_packs))
