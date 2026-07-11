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
import logging
import signal
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import yaml
from osca_cli.findings import Severity
from osca_cli.ledger import LedgerLockBusy, ledger_lock
from osca_cli.package import load_package
from osca_cli.packer import rebuild_index
from osca_cli.rules import run_all

from osca_host import __version__
from osca_host.connector import ConnectorProxy
from osca_host.control import ControlServer
from osca_host.episode import Episode, assemble
from osca_host.expr import parse_precondition
from osca_host.gate import Gate
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats
from osca_host.registry import Registry, RegistryError
from osca_host.runner import run_episode
from osca_host.settle import settle_episode
from osca_host.triggers import Subscription, TriggerTable

log = logging.getLogger("osca-host")

EPISODE_LEDGER_CAP = 100  # 剧集台账只留近期；持久归档随 M3 采集器/账本落地


class Host:
    def __init__(self, socket_path: Path):
        self.registry = Registry()
        self.table = TriggerTable(poller=self._poll)
        self.gates: dict[tuple[str, str], Gate] = {}  # (package_id, aware_id) → Gate
        self.policies: dict[str, PolicyInterceptor] = {}
        self.proxies: dict[str, ConnectorProxy] = {}
        self.bindings: dict[str, dict] = {}  # package_id → 部署注入的 binding 表（按包隔离，永不进包）
        self.episodes: OrderedDict[str, Episode] = OrderedDict()  # 剧集台账（近期）
        self._episode_seq = 0
        self._episode_tasks: set[asyncio.Task] = set()  # 在跑剧集（认知平面，独立线程）
        self.control = ControlServer(socket_path, self.handle)
        self._stop = asyncio.Event()

    # ── 控制命令（注册表操作，全部同步且快） ──────────────────────────

    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        try:
            if cmd == "status":
                snapshot = self.registry.status()
                for pkg in snapshot["packages"]:
                    pkg["gates"] = [
                        gate.snapshot() for (pid, _), gate in sorted(self.gates.items()) if pid == pkg["package_id"]
                    ]
                    policy = self.policies.get(pkg["package_id"])
                    pkg["policy"] = policy.snapshot() if policy else None
                return {"ok": True, "version": __version__, **snapshot, "triggers": self.table.status()}
            if cmd == "approve":
                policy = self.policies.get(str(request.get("package_id")))
                if policy is None:
                    return {"ok": False, "detail": f"包未注册：{request.get('package_id')}"}
                ok, detail = policy.grant_approval(str(request.get("action")))
                log.info(detail)
                return {"ok": ok, "detail": detail}
            if cmd == "load":
                return self._load(request)
            if cmd == "unload":
                return self._unload(str(request.get("package_id")))
            if cmd in ("enable", "disable"):
                return self._set_aware(str(request.get("package_id")), str(request.get("aware_id")), cmd == "enable")
            if cmd == "fire":
                return self._fire(str(request.get("package_id")), str(request.get("trigger_id")))
            if cmd == "episodes":
                return {"ok": True, "episodes": [ep.summary() for ep in self.episodes.values()]}
            if cmd == "episode":
                episode = self.episodes.get(str(request.get("episode_id")))
                if episode is None:
                    return {"ok": False, "detail": f"剧集不存在（台账只留近期 {EPISODE_LEDGER_CAP} 条）"}
                return {"ok": True, "episode": episode.dump()}
            if cmd == "stop":
                log.info("收到 stop 命令，开始关停")
                self._stop.set()
                return {"ok": True, "detail": "Host 关停中"}
            return {"ok": False, "detail": f"未知命令：{cmd}"}
        except RegistryError as e:
            return {"ok": False, "detail": str(e)}

    def _load(self, request: dict) -> dict:
        result, loaded = load_for_host(
            str(request.get("path")),
            dest=request.get("dest"),
            bindings=request.get("bindings"),
        )
        if loaded is None:
            return {"ok": False, "detail": result.lines}
        # binding 按包隔离：本次 --bindings 只归本包——同名 binding 不跨包串线、卸载即清理
        pkg_bindings: dict = {}
        if request.get("bindings"):
            pkg_bindings = yaml.safe_load(Path(str(request["bindings"])).read_text(encoding="utf-8")) or {}

        # 先在局部构建全部运行时对象——任何一步失败都不触碰注册表（原子发布，杜绝半注册包）
        pid = loaded.package_id
        try:
            policy_file = loaded.pack.yaml_files.get("policy.yaml")
            policy = PolicyInterceptor(pid, policy_file.mapping if policy_file else {}, ledger_stats(loaded.pack))
            proxy = ConnectorProxy(loaded, pkg_bindings, policy)
            gates = {
                aware.aware_id: Gate(pid, aware, precondition_eval=lambda text, p=pid: self._eval_precondition(p, text))
                for aware in loaded.awares
            }
        except Exception as e:
            detail = f"✗ 运行时构建失败：{e}——包未注册（原子发布：构建不全不触碰注册表）"
            log.error(detail)
            return {"ok": False, "detail": [*result.lines, detail]}

        # 发布点：注册表 + 笼子 + 闸门一起可见
        lines = result.lines + self.registry.register(loaded)
        self.bindings[pid] = pkg_bindings
        self.policies[pid] = policy
        self.proxies[pid] = proxy
        for aware_id, gate in gates.items():
            self.gates[(pid, aware_id)] = gate
        if policy.kill_tripped:
            lines.append(f"⚠ {policy.kill_reason}——包已装载但唤醒与调用全部拒绝（三级停语义，公理 A10）")

        # 布防：enabled 的 Aware 逐条触发原语进触发表；任一条失败即补偿回滚——不留半装载包
        armed = 0
        try:
            for aware in loaded.awares:
                if aware.enabled:
                    for t in aware.triggers:
                        self.table.subscribe(
                            t.kind,
                            t.spec,
                            Subscription(pid, aware.aware_id, t.trigger_id, self._make_deliver(pid, aware.aware_id)),
                        )
                        armed += 1
        except Exception as e:
            self._unload(pid)  # 补偿回滚：撤已布防 watcher + 清笼子/闸门/binding + 注销
            detail = f"✗ 布防失败：{e}——已补偿回滚（发布与布防同生共死），包未装载"
            log.error(detail)
            return {"ok": False, "detail": [*result.lines, detail]}
        self._sync_slots(pid)
        lines.append(f"触发表布防 {armed} 条（schedule/watch 挂 watcher，event 待人工发射）")
        for line in lines:
            log.info(line)
        return {"ok": True, "package_id": loaded.package_id, "detail": lines}

    def _unload(self, package_id: str) -> dict:
        removed = self.table.unsubscribe(package_id)
        for key in [k for k in self.gates if k[0] == package_id]:
            del self.gates[key]
        policy = self.policies.pop(package_id, None)
        if policy is not None:
            # 包停触达认知平面：在途剧集持有此 policy 引用——步间即停、后续调用全拒
            policy.revoke("unload 包停")
        self.proxies.pop(package_id, None)
        self.bindings.pop(package_id, None)  # binding 随包清理，不留给后来者
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
            # 全部订阅成功才置 enabled：任一条失败即补偿回滚——不留「显示启用、实际半布防」的 Aware
            try:
                for t in aware.triggers:
                    self.table.subscribe(
                        t.kind,
                        t.spec,
                        Subscription(package_id, aware_id, t.trigger_id, self._make_deliver(package_id, aware_id)),
                    )
            except Exception as e:
                self.table.unsubscribe(package_id, aware_id)  # 撤已布防的部分
                self._sync_slots(package_id)
                detail = f"触发器启失败：{e}——已补偿回滚（撤已布防部分），{aware_id} 保持停用、可重试"
                log.error(detail)
                return {"ok": False, "detail": detail}
            gate.enabled = True
            detail = f"触发器启：{aware_id} 重新布防 {len(aware.triggers)} 条"
        else:
            gate.enabled = False
            removed = self.table.unsubscribe(package_id, aware_id)
            detail = f"触发器停：{aware_id} 撤防 {len(removed)} 条（三级停之二）"
        self._sync_slots(package_id)
        log.info(detail)
        return {"ok": True, "detail": detail}

    def _fire(self, package_id: str, trigger_id: str) -> dict:
        error = self.table.fire_manual(package_id, trigger_id)
        if error:
            return {"ok": False, "detail": error}
        return {"ok": True, "detail": f"已人工发射 {trigger_id}（裁决见 Host 日志与 status.gates）"}

    def _make_deliver(self, package_id: str, aware_id: str):
        def deliver(trigger_id: str) -> None:
            gate = self.gates.get((package_id, aware_id))
            if gate is None:
                return
            policy = self.policies.get(package_id)
            loaded = self.registry.packages.get(package_id)
            if policy and loaded and not self._refresh_ledger(loaded, policy):
                log.warning(f"[{package_id}/{aware_id}] {trigger_id} 命中 → 账本刷新失败，本次唤醒拒绝（保留旧快照）")
                return
            if policy and policy.kill_tripped:
                log.warning(f"[{package_id}/{aware_id}] {trigger_id} 命中 → 拒绝唤醒：{policy.kill_reason}")
                return
            woke, verdict = gate.on_trigger(trigger_id)
            log.info(f"[{package_id}/{aware_id}] {trigger_id} 命中 → {verdict}")
            if woke:
                self._assemble_episode(package_id, aware_id, trigger_id)

        return deliver

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

    # ── 运行时内部取数（precondition / watch 轮询，经 Connector 代理 + Policy） ──

    def _poll(self, package_id: str, uses: str):
        proxy = self.proxies.get(package_id)
        if proxy is None:
            return None
        receipt = proxy.call(uses, step=None)
        if not receipt.ok:
            log.warning(f"[{package_id}] 轮询取数失败：{receipt.error}")
            return None
        payload = receipt.payload
        return payload if isinstance(payload, dict) else {"value": payload}

    def _eval_precondition(self, package_id: str, text: str) -> tuple[bool | None, str]:
        parsed = parse_precondition(text)
        if parsed is None:
            return None, "不可求值（受限形式：CON-xxx.接口(参数) 返回非空），默认放行"
        connector_id, interface, params = parsed
        proxy = self.proxies.get(package_id)
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
        while len(self.episodes) > EPISODE_LEDGER_CAP:
            self.episodes.popitem(last=False)
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

    async def _execute_episode(self, episode: Episode, loaded, proxy, policy) -> None:
        try:
            await asyncio.to_thread(run_episode, episode, loaded, proxy, policy)
        except Exception:
            # 执行器内部错误不许让剧集永远停在 running——终态入台账，异常进日志
            episode.status = "failed"
            episode.stop_reason = "执行器内部错误（见 Host 日志）"
            episode.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            log.exception(f"剧集 {episode.episode_id} 执行器内部错误")
        tail = f"（{episode.stop_reason}）" if episode.stop_reason else ""
        log.info(f"剧集 {episode.episode_id} 终态 {episode.status}{tail}：tokens {episode.tokens_used}")
        if episode.status != "completed":
            return
        # 对账器（组件 7）：objective 型对象自动落 outcome case，不消耗剧集
        try:
            for entry in await asyncio.to_thread(settle_episode, loaded, proxy, episode):
                if entry["settled"]:
                    log.info(f"对账落账：{entry['object']} → {entry['case']}（现实是第二位专家，公理 A2）")
                else:
                    log.info(f"对账未执行：{entry['object']}——{entry['note']}")
        except Exception:
            log.exception(f"剧集 {episode.episode_id} 对账器内部错误（剧集本身已 completed）")

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

    async def run(self, initial_packs: list[dict] | None = None) -> int:
        await self.control.start()
        log.info(f"osca-host {__version__} 就绪，控制通道：{self.control.socket_path}")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        failed = 0
        for pack in initial_packs or []:
            response = self._load({"cmd": "load", **pack})
            if not response["ok"]:
                failed += 1
                for line in response["detail"]:
                    log.error(line)
                log.error(f"启动装载失败：{pack['path']}")

        await self._stop.wait()

        # 先等在跑剧集收尾（剧集短命，正常秒级；网关卡死时 60s 兜底放弃等待，线程随进程消亡）
        if self._episode_tasks:
            log.info(f"关停前等待 {len(self._episode_tasks)} 个在跑剧集收尾")
            _, pending = await asyncio.wait(list(self._episode_tasks), timeout=60)
            if pending:
                log.warning(f"{len(pending)} 个剧集未在 60s 内收尾，放弃等待")

        # 关停 = 全体包停：逐包撤防注销（三级停之「包停」的机制复用）
        for package_id in list(self.registry.packages):
            for line in self._unload(package_id)["detail"]:
                log.info(line)
        self.table.shutdown()
        await self.control.close()
        log.info("osca-host 已退出")
        return 1 if failed else 0


def run_host(socket_path: Path, initial_packs: list[dict] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(Host(socket_path).run(initial_packs))
