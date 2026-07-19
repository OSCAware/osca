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


def test_retrieval_is_conjunctive(loaded):
    """签名硬过滤是合取（签名 = object × aware，SPEC §11）：单维命中不注入。
    析取时代的两个误注入面（GPT 复审 P1 负向用例）：错误 Aware + 正确 Object、
    正确 Aware + 错误 Object——都不得命中，否则判断被照办到错误场景。"""
    assert retrieve_judgments(loaded, "AW-999", {"OBJ-002"}) == []
    assert retrieve_judgments(loaded, "AW-001", {"OBJ-999"}) == []


def test_representative_case_is_latest_evidence(loaded):
    judgments = {j["judgment_id"]: j for j in retrieve_judgments(loaded, "AW-001", {"OBJ-002"})}
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
    assert summary["operation_id"] == episode.operation_id


def test_episode_operation_id_is_unique_beyond_display_sequence(loaded):
    """EP 展示编号可在 Host 重启后复用；不可变 operation_id 才是跨进程身份。"""
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    first = assemble("EP-0001", loaded, aware, "AW-001/T3")
    restarted = assemble("EP-0001", loaded, aware, "AW-001/T3")
    assert first.operation_id.startswith("EO-")
    assert restarted.operation_id.startswith("EO-")
    assert first.operation_id != restarted.operation_id


def test_assembly_reads_pack_not_disk_cache(loaded):
    """签名表与已校验快照同源：磁盘缓存写坏/清空都不影响装配——绝不 fail-open 静默清空判断。"""
    (loaded.root / "indexes" / "judgments.index.yaml").write_text("- not-a-table\n", encoding="utf-8")
    judgments = retrieve_judgments(loaded, "AW-001", {"OBJ-002"})
    assert [j["judgment_id"] for j in judgments] == ["J-0417", "J-0423"]


def test_assembly_is_deterministic(loaded):
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    a = assemble("EP-0001", loaded, aware, "AW-001/T3")
    b = assemble("EP-0002", loaded, aware, "AW-001/T3")
    assert a.context == b.context  # 同包同 Aware → 同上下文（纯确定性）
