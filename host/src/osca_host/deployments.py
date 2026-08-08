"""部署清单（--deployments）解析：deployment_id → {path[, bindings, dest, egress_extra]}。

单独成模块的原因（M8-T2）：清单既在启动期由 CLI 解析（fail-fast），也在 load 命令
前由 Host 热重读（新发布的部署条目免重启即可装载）——两处消费同一解析器，
清单来源永远是 Host 侧文件、服务端解析，控制通道只收 deployment_id（confused-deputy
收口，M4 首轮 P1）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from osca_host.authz import clean_text


def load_deployments(path: str) -> dict[str, dict]:
    """部署清单严格验型：ID 与路径都须非空字符串（限长、拒控制字符），不收其他键；
    相对路径按**清单文件所在目录**解析（不随 Host 进程 cwd 漂移）。"""
    base = Path(path).resolve().parent
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("部署清单必须是 mapping：deployment_id → {path[, bindings, dest]}")
    deployments: dict[str, dict] = {}
    for did, spec in data.items():
        did = clean_text(did, f"部署 ID {did!r}", max_len=200)
        allowed = {"path", "bindings", "dest", "egress_extra", "autoload"}
        if not isinstance(spec, dict) or "path" not in spec or set(spec) - allowed:
            raise ValueError(
                f"部署 {did} 须是 {{path[, bindings, dest, egress_extra, autoload]}}（path 必填，不收其他键）"
            )
        clean: dict = {}
        for key in ("path", "bindings", "dest"):
            if key not in spec:
                continue
            if spec[key] is None:
                raise ValueError(f"部署 {did} 的 {key} 不接受 null；不使用可选字段时请省略")
            value = Path(clean_text(spec[key], f"部署 {did} 的 {key}"))
            clean[key] = str(value if value.is_absolute() else base / value)
        # egress_extra（M7-W4）：部署侧注入的真实 egress host 列表（并入 policy egress_allow）——须非空字符串列表，
        # 恶形硬拒（同 path/bindings/dest 严格验型，deploy 清单错即拒启动 fail-closed）；host 非路径，不按 base 解析。
        if "egress_extra" in spec:
            raw = spec["egress_extra"]
            if not isinstance(raw, list) or any(not isinstance(h, str) or not h.strip() for h in raw):
                raise ValueError(f"部署 {did} 的 egress_extra 须是非空字符串列表（egress 允许的 host）")
            clean["egress_extra"] = [h.strip() for h in raw]
        # autoload（M8-T6）：**这一条声明的是期望态**——「这台机器上这个部署应当是装着的」，
        # Host 启动时自动装载它。只收真 bool：YAML 里 `"true"`/`yes`/`1` 这类近似值一律拒，
        # 因为「看着像 true 的字符串」被静默当真是这条线上反复吃过亏的那类猜（宁拒不猜）。
        if "autoload" in spec:
            raw = spec["autoload"]
            if not isinstance(raw, bool):
                raise ValueError(f"部署 {did} 的 autoload 须是布尔值 true/false（不接受字符串或数字的近似写法）")
            clean["autoload"] = raw
        deployments[did] = clean
    return deployments
