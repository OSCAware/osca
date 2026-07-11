"""lint 结果的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """一条 lint 发现：哪条规则、什么级别、哪个文件、什么问题。"""

    rule: str  # 规则 ID，如 OSCA020
    severity: Severity
    path: str  # 包内相对路径；包级问题用 "."
    message: str

    def format(self) -> str:
        mark = "✗" if self.severity is Severity.ERROR else "⚠"
        level = "ERROR" if self.severity is Severity.ERROR else "WARN "
        return f"{mark} {level} [{self.rule}] {self.path}: {self.message}"
