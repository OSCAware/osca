"""osca lint 引擎：装载包 → 跑全部规则 → 人可读报告。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from osca_cli.findings import Finding, Severity
from osca_cli.package import PackageSnapshot, SnapshotError, load_package
from osca_cli.rules import RULES, run_all


@dataclass
class LintResult:
    package: str
    findings: list[Finding]
    files_checked: int

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        return self.errors == 0


def lint_package(path: str | Path) -> LintResult:
    root = Path(path)
    try:
        snapshot = PackageSnapshot.capture(root)
    except SnapshotError as exc:
        return LintResult(
            package=str(path),
            findings=[Finding("OSCA000", Severity.ERROR, ".", str(exc))],
            files_checked=0,
        )
    return lint_snapshot(snapshot, package=str(path))


def lint_snapshot(snapshot: PackageSnapshot, *, package: str | None = None) -> LintResult:
    pkg = load_package(snapshot.root, snapshot=snapshot)
    return LintResult(
        package=package or str(snapshot.root),
        findings=run_all(pkg),
        files_checked=len(pkg.yaml_files),
    )


def format_report(result: LintResult) -> str:
    lines = [f"osca lint {result.package}"]
    lines.extend(f.format() for f in result.findings)
    verdict = "✓ 通过" if result.ok else "✗ 未通过"
    lines.append(
        f"{verdict} · {result.errors} 错误, {result.warnings} 警告"
        f" · 检查 YAML {result.files_checked} 个 · 规则 {len(RULES)} 条"
    )
    return "\n".join(lines)
