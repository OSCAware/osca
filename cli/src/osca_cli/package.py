"""OSCA 包的装载与索引：读文件、解析 YAML、收集 ID。

这里只负责「把包读进内存」，所有规则判断在 rules.py。
indexes/ 是机器生成的缓存（设计公理 A4），装载时跳过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ID 语法：类型前缀 + 包内自增（SPEC §2）
ID_TOKEN = re.compile(r"\b(?:OBJ|STR|CON|AW|J|C)-\d{3,4}\b")

# 目录 → 该目录下文件应有的 ID 前缀与 ID 字段名
TYPED_DIRS: dict[str, tuple[str, str]] = {
    "objects": ("OBJ", "object_id"),
    "connectors": ("CON", "connector_id"),
    "aware": ("AW", "aware_id"),
    "judgments": ("J", "judgment_id"),
    "cases": ("C", "case_id"),
}

REQUIRED_FILES = ["osca.yaml", "AGENT.md", "policy.yaml", "structure.yaml"]
SKIP_DIRS = {"indexes", ".git"}


def resolve_in_root(root: Path | str, declared: str) -> Path | None:
    """包内受限路径判据（单一真理源：lint OSCA024、Host SQL 执行器、Connector mock 固件、
    MockLLM 固件**全部**经此判定，不各写第二份）。

    包内声明（impl / 固件名等）是不可信输入：resolve 后必须留在 root 内——绝对路径、`../` 与
    符号链接逃逸一律返回 None（GPT Review：开发期与执行期同一判据，lint 放行的包不得在运行时
    才被拒；反之 lint 也要拦运行时必拒的越界声明）。符号链接**环**在 ≤3.12 的 Path.resolve 抛
    RuntimeError（3.13 起并入 OSError）——两者都按越界拒，不许 traceback 穿透 lint/执行器
    （GPT 三审 P2）。"""
    try:
        base = Path(root).resolve()
        target = (base / declared).resolve()
    except (OSError, RuntimeError, ValueError):  # 链接环/深度爆炸/非法字符等病态路径——按越界拒
        return None
    return target if target.is_relative_to(base) else None


@dataclass
class YamlFile:
    relpath: str
    data: object | None
    parse_error: str | None = None

    @property
    def mapping(self) -> dict:
        """顶层 mapping；解析失败或非 mapping 时返回空 dict，规则侧不必判空。"""
        return self.data if isinstance(self.data, dict) else {}


@dataclass
class OscaPackage:
    root: Path
    yaml_files: dict[str, YamlFile] = field(default_factory=dict)  # relpath → YamlFile
    declared_ids: dict[str, str] = field(default_factory=dict)  # ID → 首个声明它的 relpath

    def exists(self, relpath: str) -> bool:
        return (self.root / relpath).is_file()

    def typed_files(self, dirname: str) -> list[YamlFile]:
        """某类型目录下的全部 YAML（含 >200 条后的子目录分层）。"""
        prefix = dirname + "/"
        return [f for rel, f in sorted(self.yaml_files.items()) if rel.startswith(prefix)]

    def id_field_of(self, f: YamlFile) -> tuple[str, str | None]:
        """返回 (该文件应有的 ID 字段名, 实际值)。structure.yaml 特殊处理。"""
        if f.relpath == "structure.yaml":
            return "structure_id", f.mapping.get("structure_id")
        top = f.relpath.split("/", 1)[0]
        if top in TYPED_DIRS:
            field_name = TYPED_DIRS[top][1]
            return field_name, f.mapping.get(field_name)
        return "", None


def _iter_strings(node: object):
    """递归遍历 YAML 数据里的全部字符串值（键不遍历——键是字段名，不是引用）。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_strings(v)


def referenced_ids(f: YamlFile) -> set[str]:
    """一个 YAML 文件正文中出现的全部 ID 形状的 token。"""
    ids: set[str] = set()
    for s in _iter_strings(f.data):
        ids.update(ID_TOKEN.findall(s))
    return ids


def load_package(root: Path) -> OscaPackage:
    pkg = OscaPackage(root=root)

    for path in sorted(root.rglob("*.yaml")):
        rel = path.relative_to(root).as_posix()
        if rel.split("/", 1)[0] in SKIP_DIRS:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            pkg.yaml_files[rel] = YamlFile(relpath=rel, data=data)
        except yaml.YAMLError as e:
            pkg.yaml_files[rel] = YamlFile(relpath=rel, data=None, parse_error=str(e))
        except (UnicodeDecodeError, OSError) as e:
            # 二进制伪装 .yaml（如 PNG 改名）/ 读取失败：不许 traceback 穿透 lint/pack/load——
            # 一律转 parse_error，由 OSCA003 稳定报告并拒绝（GPT Review P2）
            error = f"文件读取/解码失败（{type(e).__name__}）"
            pkg.yaml_files[rel] = YamlFile(relpath=rel, data=None, parse_error=error)

    # 收集声明的 ID（文件内 ID 字段优先；用于引用解析与唯一性检查）
    for rel, f in pkg.yaml_files.items():
        _, value = pkg.id_field_of(f)
        if isinstance(value, str) and value not in pkg.declared_ids:
            pkg.declared_ids[value] = rel

    return pkg
