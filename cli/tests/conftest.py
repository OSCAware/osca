"""测试夹具：可按需破坏的最小合法 .osca 包。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def minimal_package() -> dict[str, object]:
    """一个应当零错误通过 lint 的最小包。测试通过增删改条目来构造违规。"""
    return {
        "osca.yaml": {
            "format": "osca",
            "format_version": "0.2",
            "package_id": "demo-pkg",
            "name": "演示包",
            "entry": "AGENT.md",
            "requires": {"bindings": ["DEMO_DB"]},
        },
        "AGENT.md": "# 演示 Agent\n身份、目标与边界。\n",
        "policy.yaml": {
            "policy_version": 1,
            "permissions": [{"step": "取数", "allow": ["CON-001.取数"]}],
        },
        "structure.yaml": {
            "structure_id": "STR-001",
            "pipeline": [
                {"step": "取数", "performer": "connector", "uses": "CON-001"},
                {"step": "成文", "performer": "agent", "produces": {"ref": "OBJ-001"}},
            ],
        },
        "bindings.example.yaml": {"DEMO_DB": {"endpoint": "<数据源连接串（占位）>", "secret_ref": "DEMO_DB_RO_KEY"}},
        "objects/OBJ-001-报告.yaml": {
            "object_id": "OBJ-001",
            "name": "报告",
            "kind": "artifact",
            "version": 1,
            "definition": "演示产出物。",
            "examples": {
                "positive": [{"摘录": "好样例"}],
                "negative": [{"摘录": "坏样例", "why": "因为含糊"}],
            },
        },
        "connectors/CON-001-数据源.yaml": {
            "connector_id": "CON-001",
            "name": "数据源",
            "kind": "sql_readonly",
            "binding_ref": "DEMO_DB",
            "interfaces": [{"name": "取数", "returns": "数据集"}],
            "permissions": {"write": "forbidden"},
        },
        "aware/AW-001-定时.yaml": {
            "aware_id": "AW-001",
            "name": "定时",
            "enabled": True,
            "triggers": [{"id": "T1", "kind": "schedule", "schedule": "每月9日 09:00"}],
            "then": "STR-001",
            "budget": {"max_steps": 10, "max_minutes": 5},
        },
        "judgments/J-0001.yaml": {
            "judgment_id": "J-0001",
            "status": "active",
            "signature": {"object": "OBJ-001", "aware": "AW-001", "guard": "金额 > 20"},
            "body": "演示判断。",
            "evidence": ["C-0001"],
            "meta": {"author": "张工", "confirmed": 1, "overruled": 0, "trust": "provisional"},
            "expiry": ["口径变更"],
            "replay": [{"given": "C-0001.input", "with_this_judgment": "压下"}],
        },
        "cases/C-0001.yaml": {
            "case_id": "C-0001",
            "captured_at": "2026-01-01 10:00",
            "capture_source": "口述",
            "input": {"当时生效判断集": []},
        },
    }


def build(root: Path, files: dict[str, object]) -> Path:
    pkg = root / "demo.osca"
    for rel, content in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return pkg


@pytest.fixture
def base() -> dict[str, object]:
    return minimal_package()


@pytest.fixture
def make_pkg(tmp_path):
    def _make(files: dict[str, object]) -> Path:
        return build(tmp_path, files)

    return _make
