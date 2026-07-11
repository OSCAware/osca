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
