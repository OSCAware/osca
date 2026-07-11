"""剧集执行器：performer 分发沿 pipeline 出草稿；三级停之「剧集停」。"""

from __future__ import annotations

import copy

import pytest
import yaml
from osca_cli.llm import MockLLM

from osca_host.connector import ConnectorProxy
from osca_host.episode import assemble
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats, parse_quantity
from osca_host.runner import _run_optimizer, _step_user_prompt, render_system_prompt, run_episode


@pytest.fixture
def loaded(sample_pack):
    _, pkg = load_for_host(sample_pack)
    return pkg


@pytest.fixture
def policy(loaded):
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    return PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))


@pytest.fixture
def proxy(loaded, policy, tmp_path):
    fixtures = tmp_path / "con-fixtures"
    fixtures.mkdir()
    (fixtures / "拉取费用明细.yaml").write_text(
        yaml.safe_dump({"已关账": True, "rows": [{"科目": "差旅费", "环比涨幅": 45}]}, allow_unicode=True),
        encoding="utf-8",
    )
    (fixtures / "拉取检修计划期.yaml").write_text(
        yaml.safe_dump({"处于检修期": True, "近三年检修期峰值涨幅": 60}, allow_unicode=True), encoding="utf-8"
    )
    bindings = {"FINANCE_DB": {"endpoint": f"mock://{fixtures}", "secret_ref": "FINANCE_DB_RO_KEY"}}
    return ConnectorProxy(loaded, bindings, policy)


@pytest.fixture
def llm(tmp_path):
    d = tmp_path / "llm-fixtures" / "episode"
    d.mkdir(parents=True)
    (d / "生成报警候选.md").write_text("- 甲单位 差旅费 +45%（检修期内）\n", encoding="utf-8")
    (d / "裁决.md").write_text("- 甲单位 差旅费 +45% → 正常波动（J-0417），落附录\n", encoding="utf-8")
    (d / "成文.md").write_text("正文：（无）\n附录：甲单位差旅费 +45%，检修期常态波动（J-0417）。\n", encoding="utf-8")
    return MockLLM(tmp_path / "llm-fixtures")


@pytest.fixture
def episode(loaded):
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    return assemble("EP-0001", loaded, aware, "AW-001/T3")


def test_prompt_carries_attribution_contract(episode):
    """归属契约（M2→M3 口径）：命中判断在场时，提示词必须要求段末标注判断 ID——
    否则草稿全记 uncited，confirmed/overruled 永不累积，trust 无从升级。"""
    system = render_system_prompt(episode)
    assert "归属纪律" in system and "段落末尾标注" in system
    user = _step_user_prompt({"step": "成文"}, "成文", None, None)
    assert "判断 ID 标注" in user


def test_full_pipeline_produces_draft(episode, loaded, proxy, policy, llm):
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "completed" and episode.stop_reason is None
    assert [s["step"] for s in episode.steps] == ["取数", "生成报警候选", "裁决", "成文", "专家终审"]
    assert [s["status"] for s in episode.steps] == ["done", "done", "done", "done", "handoff"]
    # 取数：裸 CON-001 展开为 manifest 全部接口，回执入档
    receipts = episode.steps[0]["receipts"]
    assert {r["interface"] for r in receipts} == {"CON-001.拉取费用明细", "CON-001.拉取检修计划期"}
    assert all(r["ok"] for r in receipts)
    # 机器侧交付物 = 最后一个 agent 步的产出；专家终审是飞轮采集点
    assert episode.draft is not None and "检修期常态波动" in episode.draft
    assert "采集点" in episode.steps[-1]["detail"]
    assert episode.tokens_used > 0 and episode.finished_at is not None
    assert llm.calls == ["episode/生成报警候选", "episode/裁决", "episode/成文"]
    # 笼子的 tokens 记账留了审计痕
    assert any("tokens 记账" in a["reason"] for a in policy.audit if a["decision"] == "allow")


def test_revoked_package_stops_inflight_episode(episode, loaded, proxy, policy, llm):
    """包停触达在途剧集：撤销后步间即停，一次调用都不再发起。"""
    policy.revoke("unload 包停")
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "stopped" and "包已停" in episode.stop_reason
    assert episode.steps == []  # 取消点在第一步发起之前


def test_budget_tokens_hard_stop(episode, loaded, proxy, policy, llm):
    episode.budget = dict(episode.budget, max_tokens=1)  # aware 预算收到 1 token
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "stopped"
    assert "预算硬顶" in episode.stop_reason and "剧集停" in episode.stop_reason
    # 止损顶：超顶那步的产物已留档，其后步骤没跑
    assert [s["step"] for s in episode.steps] == ["取数", "生成报警候选"]


def test_budget_max_steps_hard_stop(episode, loaded, proxy, policy, llm):
    episode.budget = dict(episode.budget, max_steps=1)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "stopped" and "max_steps" in episode.stop_reason
    assert len(episode.steps) == 1  # 只跑了取数


def test_policy_tokens_cage_stops_episode(episode, loaded, proxy, llm):
    caged_rules = {
        "budgets": {"per_episode": {"max_tokens": 1}},
        "permissions": [{"step": "取数", "allow": ["CON-001.拉取费用明细", "CON-001.拉取检修计划期"]}],
    }
    caged = PolicyInterceptor(loaded.package_id, caged_rules, {"confirmed": 0, "overruled": 0})
    proxy.policy = caged
    episode.budget = {}  # aware 无预算，笼子仍在
    run_episode(episode, loaded, proxy, caged, llm=llm)
    assert episode.status == "stopped" and "tokens 已用" in episode.stop_reason
    assert any(a["decision"] == "deny" for a in caged.audit)


def test_connector_failure_fails_episode(episode, loaded, policy):
    proxy = ConnectorProxy(loaded, {}, policy)  # 部署环境没注入 binding
    run_episode(episode, loaded, proxy, policy, llm=None)
    assert episode.status == "failed"
    assert "取数失败" in episode.stop_reason and "binding" in episode.stop_reason


def test_llm_unconfigured_fails_with_plain_words(episode, loaded, proxy, policy, monkeypatch):
    monkeypatch.delenv("OSCA_LLM_URL", raising=False)
    run_episode(episode, loaded, proxy, policy, llm=None)
    assert episode.status == "failed" and "OSCA_LLM_URL" in episode.stop_reason
    assert episode.steps[0]["status"] == "done"  # 取数不需要 LLM，先跑完了


def test_unknown_performer_rejected(episode, loaded, proxy, policy, llm):
    episode.context = copy.deepcopy(episode.context)  # structure 与包共享引用，改前先拷贝
    episode.context["structure"]["pipeline"].insert(0, {"step": "巫术", "performer": "wizard"})
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "failed" and "不可识别" in episode.stop_reason


def test_summary_carries_execution_state(episode, loaded, proxy, policy, llm):
    run_episode(episode, loaded, proxy, policy, llm=llm)
    s = episode.summary()
    assert s["status"] == "completed" and s["draft_ready"] is True and s["tokens_used"] > 0


# ── optimizer：初版贪心（确定性，LLM 不参与数值寻优——公理 A6） ──────────

OBJECTIVE = {"object_id": "OBJ-009", "kind": "objective", "optimize": "maximize", "constraints": ["售罄率 ≥ 95%"]}


def test_optimizer_greedy_ranks_by_objective_direction():
    spec = {"step": "寻优", "performer": "optimizer", "input": "候选", "impl": "greedy_grid_v1"}
    artifacts = {"候选": [{"方案": "A", "value": 3}, {"方案": "B", "value": 9}, {"方案": "C", "value": 5}]}
    plan, detail = _run_optimizer(spec, artifacts, {"OBJ-009": OBJECTIVE})
    assert plan["selected"]["方案"] == "B" and [c["方案"] for c in plan["ranked"]] == ["B", "C", "A"]
    assert plan["objective"] == "OBJ-009" and "贪心" in detail

    minimize = dict(OBJECTIVE, optimize="minimize")
    plan, _ = _run_optimizer(spec, artifacts, {"OBJ-009": minimize})
    assert plan["selected"]["方案"] == "A"


def test_optimizer_refuses_to_guess():
    spec = {"step": "寻优", "performer": "optimizer", "input": "候选"}
    plan, detail = _run_optimizer(spec, {"候选": [{"方案": "A"}]}, {"OBJ-009": OBJECTIVE})
    assert plan is None and "不猜数" in detail  # 候选缺数值 value

    plan, detail = _run_optimizer(spec, {"候选": [{"value": 1}]}, {})
    assert plan is None and "objective" in detail  # 没有寻优目标

    plan, detail = _run_optimizer(spec, {}, {"OBJ-009": OBJECTIVE})
    assert plan is None and "候选列表" in detail  # 输入缺失


def test_parse_quantity_restricted_form():
    assert parse_quantity("200k") == 200_000
    assert parse_quantity(30) == 30
    assert parse_quantity("40") == 40
    assert parse_quantity("1h") is None and parse_quantity(True) is None and parse_quantity(None) is None
