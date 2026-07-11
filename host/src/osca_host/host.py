"""Host 进程：确定性常驻，无 LLM（架构 §4）。

W2 形态：注册表 + 触发表（定时器/轮询器布防）+ 闸门 + 控制通道。
三级停已可演示两级：包停（unload）、触发器停（disable 单 Aware）。
剧集停（budget 硬顶）随 W3 剧集执行落地。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from osca_host import __version__
from osca_host.control import ControlServer
from osca_host.gate import Gate
from osca_host.loader import load_for_host
from osca_host.registry import Registry, RegistryError
from osca_host.triggers import Subscription, TriggerTable

log = logging.getLogger("osca-host")


class Host:
    def __init__(self, socket_path: Path):
        self.registry = Registry()
        self.table = TriggerTable()
        self.gates: dict[tuple[str, str], Gate] = {}  # (package_id, aware_id) → Gate
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
                return {"ok": True, "version": __version__, **snapshot, "triggers": self.table.status()}
            if cmd == "load":
                return self._load(request)
            if cmd == "unload":
                return self._unload(str(request.get("package_id")))
            if cmd in ("enable", "disable"):
                return self._set_aware(str(request.get("package_id")), str(request.get("aware_id")), cmd == "enable")
            if cmd == "fire":
                return self._fire(str(request.get("package_id")), str(request.get("trigger_id")))
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
        lines = result.lines + self.registry.register(loaded)

        # 布防：enabled 的 Aware 逐条触发原语进触发表；闸门每 Aware 一个
        armed = 0
        for aware in loaded.awares:
            self.gates[(loaded.package_id, aware.aware_id)] = Gate(loaded.package_id, aware)
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
            woke, verdict = gate.on_trigger(trigger_id)
            log.info(f"[{package_id}/{aware_id}] {trigger_id} 命中 → {verdict}")

        return deliver

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
