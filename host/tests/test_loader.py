"""Loader：装载校验复用 cli 核心，增量解析出运行时声明结构。"""

from __future__ import annotations

from osca_host.loader import load_for_host


def test_load_sample_pack(sample_pack):
    result, loaded = load_for_host(sample_pack)
    assert result.ok
    assert loaded is not None
    assert loaded.package_id == "demo-group-oper-diagnosis"
    assert loaded.format_version == "0.3"
    assert loaded.root == sample_pack


def test_awares_parsed(sample_pack):
    _, loaded = load_for_host(sample_pack)
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    assert aware.enabled
    assert aware.then == "STR-001"
    assert [t.kind for t in aware.triggers] == ["schedule", "watch", "event"]
    assert [t.trigger_id for t in aware.triggers] == ["AW-001/T1", "AW-001/T2", "AW-001/T3"]
    # 闸门四要素原样保留，W2 编译输入
    assert aware.gate["combine"] == "any"
    assert aware.gate["debounce"] == "72h"


def test_reject_invalid_pack(tmp_path):
    (tmp_path / "osca.yaml").write_text("format: osca\n", encoding="utf-8")
    result, loaded = load_for_host(tmp_path)
    assert not result.ok
    assert loaded is None


# ── runtime 契约校验（format_version 支持集 + requires.runtime 受限形式） ──


def _pack_with_manifest(sample_pack, tmp_path, **overrides):
    """样例包副本 + 改写 osca.yaml 字段（校验链其余环节保持绿灯）。"""
    import shutil

    import yaml

    root = tmp_path / "pack"
    shutil.copytree(sample_pack, root, ignore=shutil.ignore_patterns("indexes"))
    manifest = yaml.safe_load((root / "osca.yaml").read_text(encoding="utf-8"))
    manifest.update(overrides)
    (root / "osca.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return root


def test_reject_unsupported_format_version(sample_pack, tmp_path):
    root = _pack_with_manifest(sample_pack, tmp_path, format_version="0.2")
    result, loaded = load_for_host(root)
    assert loaded is None
    assert any("format_version 0.2 不受支持" in line for line in result.lines)


def test_reject_unmet_runtime_requirement(sample_pack, tmp_path):
    root = _pack_with_manifest(sample_pack, tmp_path, requires={"runtime": ">=9.9", "bindings": ["FINANCE_DB"]})
    result, loaded = load_for_host(root)
    assert loaded is None
    assert any("requires.runtime" in line or "包要求 runtime" in line for line in result.lines)


def test_reject_unparseable_runtime_requirement(sample_pack, tmp_path):
    root = _pack_with_manifest(sample_pack, tmp_path, requires={"runtime": "latest", "bindings": ["FINANCE_DB"]})
    result, loaded = load_for_host(root)
    assert loaded is None
    assert any("不可解析" in line for line in result.lines)


def test_sample_pack_runtime_requirement_satisfied(sample_pack):
    result, loaded = load_for_host(sample_pack)
    assert loaded is not None
    assert any("runtime 契约校验通过" in line for line in result.lines)
