"""osca 命令入口。

三个子命令对应《OSCA-SPEC》的工具链约定：
- lint  账本纪律的机器化（规则清单：docs/OSCA-LINT-RULES.md）
- pack  开发态（git 仓库）→ 交付态（zip）：lint 门禁 + 校验和清单 + 可复现打包
- load  装载校验：完整性（防篡改）→ lint → binding 比对 → 重建签名表索引
"""

from __future__ import annotations

import argparse
import sys

from osca_cli import __version__

EXIT_OK = 0
EXIT_FAILURE = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osca",
        description="OSCA 包工具：lint / pack / load",
    )
    parser.add_argument("--version", action="version", version=f"osca {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_lint = sub.add_parser("lint", help="校验一个 .osca 包是否符合规范与账本纪律")
    p_lint.add_argument("package", help=".osca 包目录路径")

    p_pack = sub.add_parser("pack", help="把开发态 .osca 目录打包为交付态 zip（lint 不过不打包）")
    p_pack.add_argument("package", help=".osca 包目录路径")
    p_pack.add_argument("-o", "--output", help="输出 zip 路径（默认 ./<package_id>.osca.zip）")

    p_load = sub.add_parser("load", help="装载校验：完整性 + lint + binding 比对 + 重建索引")
    p_load.add_argument("archive", help="交付态 zip，或开发态 .osca 目录（原地校验）")
    p_load.add_argument("--dest", help="zip 解压目标目录（默认 ./<zip 文件名去 .zip>/）")
    p_load.add_argument("--bindings", help="部署环境 bindings.yaml 路径，用于比对 binding 是否齐备")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    if args.command == "lint":
        from osca_cli.lint import format_report, lint_package

        result = lint_package(args.package)
        print(format_report(result))
        return EXIT_OK if result.ok else EXIT_FAILURE

    if args.command == "pack":
        from osca_cli.packer import pack_package

        result, _ = pack_package(args.package, args.output)
        print(result.render(f"osca pack {args.package}"))
        return EXIT_OK if result.ok else EXIT_FAILURE

    if args.command == "load":
        from osca_cli.packer import load_osca

        result, _ = load_osca(args.archive, dest=args.dest, bindings=args.bindings)
        print(result.render(f"osca load {args.archive}"))
        return EXIT_OK if result.ok else EXIT_FAILURE

    parser.error(f"未知命令：{args.command}")
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
