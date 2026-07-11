"""Host 进程：确定性常驻，无 LLM（架构 §4）。

W1 形态：注册表 + 控制通道 + 干净关停。
关停即全体包停——逐包注销 watcher，符合「启停永远是注册表操作」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from osca_host import __version__
from osca_host.control import ControlServer
from osca_host.loader import load_for_host
from osca_host.registry import Registry, RegistryError

log = logging.getLogger("osca-host")


class Host:
    def __init__(self, socket_path: Path):
        self.registry = Registry()
        self.control = ControlServer(socket_path, self.handle)
        self._stop = asyncio.Event()

    # ── 控制命令（注册表操作，全部同步且快） ──────────────────────────

    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        try:
            if cmd == "status":
                return {"ok": True, "version": __version__, **self.registry.status()}
            if cmd == "load":
                return self._load(request)
            if cmd == "unload":
                lines = self.registry.unregister(str(request.get("package_id")))
                for line in lines:
                    log.info(line)
                return {"ok": True, "detail": lines}
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
        for line in lines:
            log.info(line)
        return {"ok": True, "package_id": loaded.package_id, "detail": lines}

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

        # 关停 = 全体包停：逐包注销 watcher（三级停之「包停」的机制复用）
        for package_id in list(self.registry.packages):
            for line in self.registry.unregister(package_id):
                log.info(line)
        await self.control.close()
        log.info("osca-host 已退出")
        return 1 if failed else 0


def run_host(socket_path: Path, initial_packs: list[dict] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(Host(socket_path).run(initial_packs))
