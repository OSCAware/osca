"""Host 进程：确定性常驻，无 LLM（架构 §4）。

W4 形态：注册表 + 触发表 + 闸门 + 剧集装配器 + Policy 拦截器 + Connector 代理 + 控制通道。
笼子已硬：按步骤白名单（默认拒绝）、审批门、预算硬顶、脱敏、kill switch，全程审计。
W2 的两笔债已还：precondition 经代理真求值、watch 的 emit_when 真比对。
三级停可演示两级：包停（unload）、触发器停（disable）；剧集停随 W5 剧集执行落地。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections import OrderedDict
from pathlib import Path

import yaml

from osca_host import __version__
from osca_host.connector import ConnectorProxy
from osca_host.control import ControlServer
from osca_host.episode import Episode, assemble
from osca_host.expr import parse_precondition
from osca_host.gate import Gate
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats
from osca_host.registry import Registry, RegistryError
from osca_host.triggers import Subscription, TriggerTable

log = logging.getLogger("osca-host")

EPISODE_LEDGER_CAP = 100  # 剧集台账只留近期；持久归档随 W5 对账器落地


class Host:
    def __init__(self, socket_path: Path):
        self.registry = Registry()
        self.table = TriggerTable(poller=self._poll)
        self.gates: dict[tuple[str, str], Gate] = {}  # (package_id, aware_id) → Gate
        self.policies: dict[str, PolicyInterceptor] = {}
        self.proxies: dict[str, ConnectorProxy] = {}
        self.bindings: dict = {}  # 部署环境注入的 binding 表（--bindings，永不进包）
        self.episodes: OrderedDict[str, Episode] = OrderedDict()  # 剧集台账（近期）
        self._episode_seq = 0
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
        if request.get("bindings"):  # 部署 binding 进 Host 运行时表（Connector 代理解析用）
            self.bindings.update(yaml.safe_load(Path(str(request["bindings"])).read_text(encoding="utf-8")) or {})
        lines = result.lines + self.registry.register(loaded)

        # 笼子先立起来：policy 拦截器 + connector 代理，然后才布防
        policy_file = loaded.pack.yaml_files.get("policy.yaml")
        policy = PolicyInterceptor(
            loaded.package_id,
            policy_file.mapping if policy_file else {},
            ledger_stats(loaded.pack),
        )
        self.policies[loaded.package_id] = policy
        self.proxies[loaded.package_id] = ConnectorProxy(loaded, self.bindings, policy)
        if policy.kill_tripped:
            lines.append(f"⚠ {policy.kill_reason}——包已装载但唤醒与调用全部拒绝（三级停语义，公理 A10）")

        # 布防：enabled 的 Aware 逐条触发原语进触发表；闸门每 Aware 一个
        armed = 0
        pid = loaded.package_id
        for aware in loaded.awares:
            self.gates[(pid, aware.aware_id)] = Gate(
                pid, aware, precondition_eval=lambda text, p=pid: self._eval_precondition(p, text)
            )
            if aware.enabled:
                for t in aware.triggers:
                    self.table.subscribe(
                        t.kind,
                        t.spec,
                        Subscription(
                            loaded.package_id,
                            aware.aware_id,
                            t.trigger_id,
                            self._make_deliver(loaded.package_id, aware.aware_id),
                        ),
                    )
                    armed += 1
        self._sync_slots(loaded.package_id)
        lines.append(f"触发表布防 {armed} 条（schedule/watch 挂 watcher，event 待人工发射）")
        for line in lines:
            log.info(line)
        return {"ok": True, "package_id": loaded.package_id, "detail": lines}

    def _unload(self, package_id: str) -> dict:
        removed = self.table.unsubscribe(package_id)
        for key in [k for k in self.gates if k[0] == package_id]:
            del self.gates[key]
        self.policies.pop(package_id, None)
        self.proxies.pop(package_id, None)
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
        gate.enabled = enabled
        if enabled:
            for t in aware.triggers:
                self.table.subscribe(
                    t.kind,
                    t.spec,
                    Subscription(package_id, aware_id, t.trigger_id, self._make_deliver(package_id, aware_id)),
                )
            detail = f"触发器启：{aware_id} 重新布防 {len(aware.triggers)} 条"
        else:
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
            if policy and policy.kill_tripped:
                log.warning(f"[{package_id}/{aware_id}] {trigger_id} 命中 → 拒绝唤醒：{policy.kill_reason}")
                return
            woke, verdict = gate.on_trigger(trigger_id)
            log.info(f"[{package_id}/{aware_id}] {trigger_id} 命中 → {verdict}")
            if woke:
                self._assemble_episode(package_id, aware_id, trigger_id)

        return deliver

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
        if loaded is None:
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
            f" / 对象 {len(s['objects'])} 个 / 预算 {episode.budget}（执行属 W5，本周只装配）"
        )

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
