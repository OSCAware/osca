"""Host 的注册表：装载的包与它们的 watcher 槽位。

启停模型（架构 §4）：启停永远是运行时对注册表的操作，不是模型的决定。
三级停中的「包停」在这里落地——注销一个包 = 释放它全部 watcher 槽位。
W1 槽位只登记（state=declared），W2 触发表编译后变为 armed。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from osca_host.loader import LoadedPackage


@dataclass
class WatcherSlot:
    """触发表里的一个槽位。W1 只声明，W2 挂真 watcher。"""

    trigger_id: str
    kind: str
    state: str = "declared"  # declared → armed(W2) → disabled


class RegistryError(Exception):
    pass


@dataclass
class Registry:
    packages: dict[str, LoadedPackage] = field(default_factory=dict)
    watchers: dict[str, list[WatcherSlot]] = field(default_factory=dict)  # package_id → slots

    def register(self, pkg: LoadedPackage) -> list[str]:
        """注册包并登记 watcher 槽位；返回人可读日志行。"""
        if pkg.package_id in self.packages:
            raise RegistryError(f"包已注册：{pkg.package_id}（同 ID 重复装载需先注销）")
        slots = [
            WatcherSlot(trigger_id=t.trigger_id, kind=t.kind)
            for aware in pkg.awares
            if aware.enabled
            for t in aware.triggers
        ]
        self.packages[pkg.package_id] = pkg
        self.watchers[pkg.package_id] = slots
        return [
            f"包已注册：{pkg.package_id}（{pkg.name}）",
            f"watcher 槽位登记 {len(slots)} 个：" + ", ".join(s.trigger_id for s in slots),
        ]

    def unregister(self, package_id: str) -> list[str]:
        """包停：注销全部 watcher 槽位，再移除包。"""
        if package_id not in self.packages:
            raise RegistryError(f"包未注册：{package_id}")
        slots = self.watchers.pop(package_id, [])
        self.packages.pop(package_id)
        return [
            f"watcher 注销 {len(slots)} 个：" + ", ".join(s.trigger_id for s in slots),
            f"包已停止并移除：{package_id}",
        ]

    def status(self) -> dict:
        """注册表快照（控制通道 status 命令的返回体）。"""
        return {
            "packages": [
                {
                    "package_id": pkg.package_id,
                    "name": pkg.name,
                    "format_version": pkg.format_version,
                    "root": str(pkg.root),
                    "awares": [{"aware_id": a.aware_id, "name": a.name, "enabled": a.enabled} for a in pkg.awares],
                    "watchers": [
                        {"trigger_id": s.trigger_id, "kind": s.kind, "state": s.state}
                        for s in self.watchers.get(pkg.package_id, [])
                    ],
                }
                for pkg in self.packages.values()
            ],
        }
