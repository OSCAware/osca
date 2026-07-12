"""osca-host 命令入口。

run 之外的子命令都是控制通道客户端：对运行中的 Host 发注册表操作。
身份即 token（M4-W0）：默认读 Host 生成的 admin token（socket 旁 0600 文件）；
非 admin 界面进程用 --token-file 带自己的 principal token——角色能力见
osca_host.authz 的权限矩阵（admin 不可授予业务审批；approve 在 W3 审批
challenge 落地前对全角色关闭）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from osca_host import __version__
from osca_host.control import DEFAULT_SOCKET, send_command

EXIT_OK = 0
EXIT_FAILURE = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osca-host", description="OSCA 运行框架 Host（参考实现）")
    parser.add_argument("--version", action="version", version=f"osca-host {__version__}")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET, help=f"控制通道路径（默认 {DEFAULT_SOCKET}）")
    parser.add_argument(
        "--token-file",
        type=Path,
        help="principal token 文件（默认读 Host 生成的 admin token：<socket>.token）",
    )

    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="前台启动 Host（常驻进程；Ctrl-C / SIGTERM 干净关停）")
    p_run.add_argument(
        "--load", action="append", default=[], metavar="PACK", help="启动时装载的包（目录或 zip），可多次"
    )
    p_run.add_argument("--bindings", help="部署环境 bindings.yaml，装载时比对")
    p_run.add_argument(
        "--deployments",
        help="部署清单 deployments.yaml：deployment_id → {path[, bindings, dest]}——控制通道 load 只收 ID",
    )

    sub.add_parser("status", help="注册表快照：已装载的包 / Aware / watcher 槽位")

    p_load = sub.add_parser("load", help="向运行中的 Host 装载一个部署条目（路径由 Host 侧 --deployments 解析）")
    p_load.add_argument("deployment_id", help="Host 启动时 --deployments 清单里的部署 ID")

    p_unload = sub.add_parser("unload", help="包停：注销全部 watcher 并移除包")
    p_unload.add_argument("package_id")

    p_disable = sub.add_parser("disable", help="触发器停：撤防单个 Aware 的全部触发原语（三级停之二）")
    p_disable.add_argument("package_id")
    p_disable.add_argument("aware_id")

    p_enable = sub.add_parser("enable", help="触发器启：重新布防单个 Aware")
    p_enable.add_argument("package_id")
    p_enable.add_argument("aware_id")

    p_fire = sub.add_parser("fire", help="人工发射一个 event 触发原语（操作者控制台）")
    p_fire.add_argument("package_id")
    p_fire.add_argument("trigger_id", help="全局触发 ID，如 AW-001/T3")

    p_approve = sub.add_parser(
        "approve", help="审批门（W3 审批 challenge 落地前经控制通道禁用——旧 set[action] 授予不再暴露）"
    )
    p_approve.add_argument("package_id")
    p_approve.add_argument("action", help="policy.yaml approvals 里声明的动作名")

    sub.add_parser("episodes", help="剧集台账：近期唤醒装配的剧集摘要")

    p_episode = sub.add_parser("episode", help="导出一个剧集的完整一次性上下文")
    p_episode.add_argument("episode_id", help="剧集 ID，如 EP-0001")

    sub.add_parser("stop", help="关停 Host（等价于全体包停后退出）")

    return parser


def _load_deployments(path: str) -> dict[str, dict]:
    """部署清单严格验型：ID 与路径都须非空字符串（限长、拒控制字符），不收其他键；
    相对路径按**清单文件所在目录**解析（不随 Host 进程 cwd 漂移）。"""
    from osca_host.authz import clean_text

    base = Path(path).resolve().parent
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("部署清单必须是 mapping：deployment_id → {path[, bindings, dest]}")
    deployments: dict[str, dict] = {}
    for did, spec in data.items():
        did = clean_text(did, f"部署 ID {did!r}", max_len=200)
        if not isinstance(spec, dict) or "path" not in spec or set(spec) - {"path", "bindings", "dest"}:
            raise ValueError(f"部署 {did} 须是 {{path[, bindings, dest]}}（path 必填，不收其他键）")
        clean: dict = {}
        for key in ("path", "bindings", "dest"):
            if spec.get(key) is None:
                continue
            value = Path(clean_text(spec[key], f"部署 {did} 的 {key}"))
            clean[key] = str(value if value.is_absolute() else base / value)
        deployments[did] = clean
    return deployments


def _client(request: dict, socket_path: Path, token_file: Path | None) -> int:
    token = None
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"读不到 token 文件：{e}")
            return EXIT_FAILURE
    response = send_command(request, socket_path, token=token)
    if request["cmd"] in ("status", "episodes", "episode") and response.get("ok"):
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        detail = response.get("detail", "")
        for line in detail if isinstance(detail, list) else [str(detail)]:
            print(line)
    return EXIT_OK if response.get("ok") else EXIT_FAILURE


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    if args.command == "run":
        from osca_host.host import run_host

        deployments = None
        if args.deployments:
            try:
                deployments = _load_deployments(args.deployments)
            except (OSError, ValueError, yaml.YAMLError) as e:
                print(f"部署清单不可用：{e}")
                return EXIT_FAILURE
        packs = [{"path": p, "bindings": args.bindings} for p in args.load]
        return run_host(args.socket, packs, deployments)

    client = {
        "status": lambda: {"cmd": "status"},
        "load": lambda: {"cmd": "load", "deployment_id": args.deployment_id},
        "unload": lambda: {"cmd": "unload", "package_id": args.package_id},
        "enable": lambda: {"cmd": "enable", "package_id": args.package_id, "aware_id": args.aware_id},
        "disable": lambda: {"cmd": "disable", "package_id": args.package_id, "aware_id": args.aware_id},
        "fire": lambda: {"cmd": "fire", "package_id": args.package_id, "trigger_id": args.trigger_id},
        "approve": lambda: {"cmd": "approve", "package_id": args.package_id, "action": args.action},
        "episodes": lambda: {"cmd": "episodes"},
        "episode": lambda: {"cmd": "episode", "episode_id": args.episode_id},
        "stop": lambda: {"cmd": "stop"},
    }
    if args.command in client:
        return _client(client[args.command](), args.socket, args.token_file)

    parser.error(f"未知命令：{args.command}")
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
