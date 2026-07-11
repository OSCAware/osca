"""osca-host 命令入口。

run 之外的子命令都是控制通道客户端：对运行中的 Host 发注册表操作。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from osca_host import __version__
from osca_host.control import DEFAULT_SOCKET, send_command

EXIT_OK = 0
EXIT_FAILURE = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osca-host", description="OSCA 运行框架 Host（参考实现）")
    parser.add_argument("--version", action="version", version=f"osca-host {__version__}")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET, help=f"控制通道路径（默认 {DEFAULT_SOCKET}）")

    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="前台启动 Host（常驻进程；Ctrl-C / SIGTERM 干净关停）")
    p_run.add_argument(
        "--load", action="append", default=[], metavar="PACK", help="启动时装载的包（目录或 zip），可多次"
    )
    p_run.add_argument("--bindings", help="部署环境 bindings.yaml，装载时比对")

    sub.add_parser("status", help="注册表快照：已装载的包 / Aware / watcher 槽位")

    p_load = sub.add_parser("load", help="向运行中的 Host 装载一个包")
    p_load.add_argument("package", help=".osca 包目录或交付态 zip")
    p_load.add_argument("--bindings", help="部署环境 bindings.yaml")
    p_load.add_argument("--dest", help="zip 解压目标目录")

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

    sub.add_parser("stop", help="关停 Host（等价于全体包停后退出）")

    return parser


def _client(request: dict, socket_path: Path) -> int:
    response = send_command(request, socket_path)
    if request["cmd"] == "status" and response.get("ok"):
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

        packs = [{"path": p, "bindings": args.bindings} for p in args.load]
        return run_host(args.socket, packs)

    if args.command == "status":
        return _client({"cmd": "status"}, args.socket)
    if args.command == "load":
        return _client({"cmd": "load", "path": args.package, "bindings": args.bindings, "dest": args.dest}, args.socket)
    if args.command == "unload":
        return _client({"cmd": "unload", "package_id": args.package_id}, args.socket)
    if args.command in ("enable", "disable"):
        return _client({"cmd": args.command, "package_id": args.package_id, "aware_id": args.aware_id}, args.socket)
    if args.command == "fire":
        return _client({"cmd": "fire", "package_id": args.package_id, "trigger_id": args.trigger_id}, args.socket)
    if args.command == "stop":
        return _client({"cmd": "stop"}, args.socket)

    parser.error(f"未知命令：{args.command}")
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
