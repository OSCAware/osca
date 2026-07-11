"""剧集装配器：一次性上下文 = AGENT.md + structure + discretion + objects + 判断 top3-7 带 case。"""

from __future__ import annotations

import pytest

from osca_host.episode import assemble, retrieve_judgments
from osca_host.loader import load_for_host


@pytest.fixture
def loaded(sample_pack):
    _, pkg = load_for_host(sample_pack)  # 装载五步含签名表重建
    return pkg


@pytest.fixture
def episode(loaded):
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    return assemble("EP-0001", loaded, aware, "AW-001/T3")


def test_retrieval_active_only_ranked(loaded):
    judgments = retrieve_judgments(loaded, "AW-001", {"OBJ-002"})
    ids = [j["judgment_id"] for j in judgments]
    assert "J-0405" not in ids  # superseded 不入剧集
    assert ids[0] == "J-0417"  # trust=high 排最前
    assert ids == ["J-0417", "J-0423"]


def test_representative_case_is_latest_evidence(loaded):
    judgments = {j["judgment_id"]: j for j in retrieve_judgments(loaded, "AW-001", set())}
    # J-0417 evidence = [C-0091, C-0094] → 代表 case 取编号最新的 C-0094
    assert judgments["J-0417"]["case"]["case_id"] == "C-0094"


def test_context_sections(episode):
    ctx = episode.context
    assert "身份" in ctx["agent"] or len(ctx["agent"]) > 0  # AGENT.md 全文
    assert ctx["structure"]["structure_id"] == "STR-001"
    assert "计划外唤醒" in ctx["discretion"]  # 命中 Aware 的 discretion 原文
    assert set(ctx["objects"]) == {"OBJ-001", "OBJ-002", "OBJ-003"}  # structure 引用 ∪ 判断签名指向
    assert [j["judgment_id"] for j in ctx["judgments"]] == ["J-0417", "J-0423"]


def test_policy_never_in_context(episode):
    """公理 A5：policy.yaml 是笼子，模型永不读——上下文里不得出现。"""
    assert "policy" not in episode.context
    assert "kill_switch" not in str(episode.context)


def test_episode_metadata(episode):
    assert episode.then == "STR-001"
    assert episode.budget.get("max_steps") == 40
    assert episode.fired_trigger == "AW-001/T3"
    summary = episode.summary()
    assert summary["judgments"] == ["J-0417", "J-0423"]
    assert summary["objects"] == ["OBJ-001", "OBJ-002", "OBJ-003"]


def test_assembly_is_deterministic(loaded):
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    a = assemble("EP-0001", loaded, aware, "AW-001/T3")
    b = assemble("EP-0002", loaded, aware, "AW-001/T3")
    assert a.context == b.context  # 同包同 Aware → 同上下文（纯确定性）
