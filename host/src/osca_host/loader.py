"""Host 组件 1：Loader —— 把合规的 .osca 包读成运行时结构。

装载校验五步（解压 → 完整性 → lint → binding 比对 → 重建索引）直接复用
cli 的 `load_osca`：交付态与运行态用同一套校验，不写第二份真理。
本模块只做增量：把校验通过的包解析成注册表需要的声明结构
（Aware / 触发原语 / 闸门），供触发表在 W2 编译布防。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from osca_cli.package import OscaPackage, load_package
from osca_cli.packer import OpResult, load_osca


@dataclass
class TriggerDecl:
    """一条触发原语的声明。W1 只登记，W2 编译为 watcher。"""

    trigger_id: str  # 全局唯一：<aware_id>/<包内 id>，如 AW-001/T1
    kind: str  # schedule | watch | event
    spec: dict  # 原始字段（schedule/every/uses/source…），编译期再解释


@dataclass
class AwareDecl:
    aware_id: str
    name: str
    enabled: bool
    triggers: list[TriggerDecl]
    gate: dict  # combine/precondition/debounce/on_fail，W2 闸门编译输入
    then: str | None  # 唤醒后装配的 structure 引用
    discretion: str = ""  # 有界主动的裁量说明，进剧集上下文
    budget: dict = field(default_factory=dict)  # 剧集预算（max_steps/max_minutes/max_tokens）


@dataclass
class LoadedPackage:
    """一个装载完成、可注册进 Host 的包。"""

    package_id: str
    name: str
    format_version: str
    root: Path
    awares: list[AwareDecl] = field(default_factory=list)
    pack: OscaPackage | None = field(default=None, repr=False)  # 装载时解析的包内容（装载态即运行态）

    @property
    def trigger_count(self) -> int:
        return sum(len(a.triggers) for a in self.awares)


def _parse_awares(pkg: OscaPackage) -> list[AwareDecl]:
    awares: list[AwareDecl] = []
    for f in pkg.typed_files("aware"):
        aware_id = f.mapping.get("aware_id") or f.relpath
        triggers = []
        for t in f.mapping.get("triggers") or []:
            if not isinstance(t, dict):
                continue
            local_id = str(t.get("id", f"T{len(triggers) + 1}"))
            spec = {k: v for k, v in t.items() if k not in ("id", "kind")}
            triggers.append(
                TriggerDecl(
                    trigger_id=f"{aware_id}/{local_id}",
                    kind=str(t.get("kind", "")),
                    spec=spec,
                )
            )
        awares.append(
            AwareDecl(
                aware_id=aware_id,
                name=str(f.mapping.get("name", "")),
                enabled=bool(f.mapping.get("enabled", True)),
                triggers=triggers,
                gate=f.mapping.get("gate") or {},
                then=f.mapping.get("then"),
                discretion=str(f.mapping.get("discretion", "")),
                budget=f.mapping.get("budget") or {},
            )
        )
    return awares


def load_for_host(
    source: str | Path,
    dest: str | Path | None = None,
    bindings: str | Path | None = None,
) -> tuple[OpResult, LoadedPackage | None]:
    """装载一个包（开发态目录或交付态 zip）为运行时结构。

    校验不过 → (失败的 OpResult, None)；Host 拒绝注册不合规资产。
    """
    result, root = load_osca(source, dest=dest, bindings=bindings)
    if not result.ok or root is None:
        return result, None

    pack = load_package(root)
    manifest = pack.yaml_files.get("osca.yaml")
    m = manifest.mapping if manifest else {}
    loaded = LoadedPackage(
        package_id=str(m.get("package_id", root.name)),
        name=str(m.get("name", "")),
        format_version=str(m.get("format_version", "")),
        root=root,
        awares=_parse_awares(pack),
        pack=pack,
    )
    result.step(f"运行时结构解析完成：Aware {len(loaded.awares)} 个，触发原语 {loaded.trigger_count} 条（W2 编译布防）")
    return result, loaded
