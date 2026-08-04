"""剧集执行器：performer 分发沿 pipeline 出草稿；三级停之「剧集停」。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from osca_cli.llm import MockLLM

from osca_host.connector import ConnectorProxy
from osca_host.episode import assemble
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats, parse_quantity
from osca_host.runner import _parse_structured, _run_optimizer, _step_user_prompt, render_system_prompt, run_episode


@pytest.fixture
def loaded(sample_pack):
    _, pkg = load_for_host(sample_pack, require_bindings=False)
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


def test_prompt_carries_guard_application_contract(episode):
    """guard 提示词契约（SPEC §11 定稿，GPT 四审 P1）：硬过滤只有 object×aware，guard 由模型
    逐条判定——提示词必须明示「guard 未判定」并规定不命中/无法判断即不得应用、不得标注 ID，
    否则错误场景照办与归属计数污染两个失败面都开着。"""
    system = render_system_prompt(episode)
    assert "guard 未判定" in system or "尚未判定" in system
    assert "不得应用" in system and "逐条判定" in system
    user = _step_user_prompt({"step": "成文"}, "成文", None, None)
    assert "guard" in user


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


def test_kill_switch_mid_episode_blocks_next_llm_call(episode, loaded, proxy, policy, llm):
    """在途剧集对新触发的 kill switch 无豁免：第一个 agent 步后触发，第二个 agent 步零调用。"""

    class TripAfterFirst:
        model = "mock"

        def complete(self, system, user, tag=None, timeout=None):
            reply = llm.complete(system, user, tag=tag)
            policy.publish_kill_switch("tripped", "kill switch 触发：测试注入")
            return reply

    run_episode(episode, loaded, proxy, policy, llm=TripAfterFirst())
    assert episode.status == "stopped" and "拒绝发起 LLM 调用" in episode.stop_reason
    assert llm.calls == ["episode/生成报警候选"]  # 第二个 agent 步（裁决）一次都没调


def test_cross_section_budget_key_refused(episode, loaded, proxy, policy, llm):
    """aware.budget 里出现 Policy 层的键（如 max_tool_calls）——运行时自防拒绝执行。"""
    episode.budget = {"max_tool_calls": 1}
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "failed" and "不执行的键" in episode.stop_reason
    assert llm.calls == []


def test_zero_token_budget_blocks_llm_call_entirely(episode, loaded, proxy, llm):
    """「额度撤销、任何调用即拒」：零额度在 llm.complete 之前预检拒绝——LLM 一次都不调。"""
    policy = PolicyInterceptor(
        loaded.package_id,
        {
            "permissions": [{"step": "取数", "allow": ["CON-001.拉取费用明细", "CON-001.拉取检修计划期"]}],
            "budgets": {"per_episode": {"max_tokens": "unlimited"}},  # 记法非法 → 额度撤销（0）
        },
        {"confirmed": 0, "overruled": 0},
    )
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "stopped" and "拒绝发起" in episode.stop_reason
    assert llm.calls == []  # 不是调用后止损——一次都没发起


def test_unparsable_aware_budget_revokes_not_unlimited(episode, loaded, proxy, policy, llm):
    """绕过 lint 的非法 aware 预算不得退化成无硬顶——runner 自防：额度撤销即停。"""
    episode.budget = {"max_steps": "很多步"}
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "stopped" and "max_steps" in episode.stop_reason
    assert llm.calls == []

    episode2 = copy.deepcopy(episode)
    episode2.status = "assembled"
    episode2.budget = ["oops"]
    run_episode(episode2, loaded, proxy, policy, llm=llm)
    assert episode2.status == "failed" and "形状非法" in episode2.stop_reason


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


def _write_pipeline(episode, proxy, policy, write_ref="CON-001.拉取费用明细"):
    """装一条 [agent 产草稿 → connector 写步(input=草稿)] 的可恢复剧集管线；写接口进白名单+approvals。"""
    episode.context = copy.deepcopy(episode.context)  # structure 与包共享引用，改前先拷贝
    proxy.connectors["CON-001"].setdefault("permissions", {})["write"] = "allowed_with_approval"
    policy.permissions["下发"] = {write_ref}  # 写步过工具白名单，才真正触达写门（否则被白名单提前拦下）
    policy.approvals[write_ref] = "专家"
    episode.context["structure"]["pipeline"] = [
        {"step": "生成报警候选", "performer": "agent", "produces": "草稿"},
        {"step": "下发", "performer": "connector", "uses": write_ref, "input": "草稿"},
    ]
    return write_ref


def _write_step(episode):
    return next(s for s in episode.steps if s["step"] == "下发")


def _landed(step):
    return [r for r in step.get("receipts", []) if isinstance(r.get("payload"), dict) and r["payload"].get("landed")]


def test_write_step_suspends_on_approval_gate(episode, loaded, proxy, policy, llm):
    """写命中审批门 → 剧集**挂起**（非 failed）：status=suspended_pending_approval + resume 快照 + 挂 pending
    挑战，摘要绑真实被写内容（=上游草稿）、绑本剧集。断言真触达写门：删掉写门挂不出挑战、本测试会红。"""
    from osca_host.challenge import payload_digest

    _write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "suspended_pending_approval" and episode.finished_at is None
    assert episode.resume is not None and episode.resume["step_index"] == 1
    [ch] = policy.pending_challenges()
    assert episode.resume["challenge_id"] == ch["challenge_id"]
    assert ch["payload_digest"] == payload_digest(episode.draft)  # 绑真实被写内容=上游草稿（穿透成立）
    assert ch["payload_digest"] != payload_digest("") and ch["episode_id"] == episode.episode_id


def test_suspend_then_approve_resumes_and_lands(episode, loaded, proxy, policy, llm):
    """挂起 → approve → 恢复（consume-only 兑现）→ mock 写落地；剧集 completed，写回执落地=被批准内容。"""
    _write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "suspended_pending_approval"
    draft = episode.draft
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)

    run_episode(episode, loaded, proxy, policy, llm=llm)  # 恢复（读 episode.resume）
    assert episode.status == "completed" and episode.resume is None
    step = _write_step(episode)
    assert step["status"] == "done"
    [landed] = _landed(step)
    assert landed["ok"] and landed["payload"]["applied"] == draft  # 落地=被批准的被写内容
    assert policy.pending_challenges() == []  # 一次性：挑战已 consumed


def test_suspend_then_deny_resumes_to_fallback(episode, loaded, proxy, policy, llm):
    """挂起 → deny → 恢复 → 回落保守默认（不写）：剧集 completed（**非 failed**），写步记 denied、无落地。"""
    _write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=False)

    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "completed" and episode.resume is None
    step = _write_step(episode)
    assert step["status"] == "denied" and _landed(step) == []  # 回落=不写


def test_suspend_then_expired_approval_falls_back(loaded, llm, tmp_path):
    """approve 后 TTL 前未及恢复消费 → approved 也过期（防陈旧授权翻用）→ 恢复按 EXPIRED 回落（不写）。"""
    from osca_host.challenge import ChallengeStore

    clock = {"t": 1000.0}
    store = ChallengeStore(clock=lambda: clock["t"], ttl_seconds=300.0)
    pf = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, pf.mapping, ledger_stats(loaded.pack), challenges=store)
    write_ref = "CON-001.拉取费用明细"
    policy.permissions["下发"] = {write_ref}
    policy.approvals[write_ref] = "专家"
    fixtures = tmp_path / "f"
    fixtures.mkdir()
    proxy = ConnectorProxy(loaded, {"FINANCE_DB": {"endpoint": f"mock://{fixtures}"}}, policy)
    proxy.connectors["CON-001"].setdefault("permissions", {})["write"] = "allowed_with_approval"
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    episode = assemble("EP-0009", loaded, aware, "AW-001/T3")
    episode.context = copy.deepcopy(episode.context)
    episode.context["structure"]["pipeline"] = [
        {"step": "生成报警候选", "performer": "agent", "produces": "草稿"},
        {"step": "下发", "performer": "connector", "uses": write_ref, "input": "草稿"},
    ]
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "suspended_pending_approval"
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    clock["t"] += 400.0  # 越过 TTL：approved 也被 _gc_locked 迁 expired

    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "completed"
    assert _write_step(episode)["status"] == "denied"  # 过期未兑现 → 回落（不写）


def test_resume_after_consumed_challenge_falls_back_no_double_write(episode, loaded, proxy, policy, llm):
    """一次性兜底（INV-4 运行时侧）：恢复消费后挑战 consumed，再以旧快照恢复 → 见 consumed → 回落，不第二次写。"""
    _write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    snap = copy.deepcopy(episode.resume)  # 保存挂起快照（深拷贝：浅拷会与恢复共享 receipts 列表）
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    run_episode(episode, loaded, proxy, policy, llm=llm)  # 恢复 → 兑现（consume）
    assert episode.status == "completed" and len(_landed(_write_step(episode))) == 1

    episode.resume = snap  # 强行以旧快照再恢复一次（Host CAS 正常挡；这里验运行时一次性兜底）
    episode.status = "suspended_pending_approval"
    episode.steps = []
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "completed"
    step = _write_step(episode)
    assert step["status"] == "denied" and _landed(step) == []  # consumed → 回落，不第二次写


def test_resume_executor_error_fails_not_fallback(episode, loaded, proxy, policy, llm):
    """恢复兑现时写执行器/binding 真错误（挑战**已** consume）→ 剧集 failed（非回落 completed），不掩盖系统错、
    不谎报审批回落（对抗审查 major-A）。一次性授权已烧，诚实：真错误不退还额度。"""
    _write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    proxy.bindings.pop("FINANCE_DB", None)  # 兑现前抽掉 binding → consume 成功后写执行失败（disposition!=denied）

    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "failed" and "恢复写执行失败" in episode.stop_reason
    assert policy.get_challenge(ch["challenge_id"]).state == "consumed"  # 一次性授权已烧（真错误不退还）


def test_connector_step_missing_input_artifact_fails(episode, loaded, proxy, policy, llm):
    """连接器步声明 input 但上游产物缺失 = 流水线声明与执行不符，直接拒绝（与 agent 步同口径）。"""
    episode.context = copy.deepcopy(episode.context)
    episode.context["structure"]["pipeline"] = [
        {"step": "下发", "performer": "connector", "uses": "CON-001.拉取费用明细", "input": "并不存在的产物"},
    ]
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "failed" and "并不存在的产物" in episode.stop_reason


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


def test_agent_step_estimates_on_illegal_token_report(episode, loaded, proxy, policy):
    """GPT Review 复审 P1 预算绕过（aware 硬顶侧）：可插拔注入的 llm 误报负/零 tokens **不得按 0 记账**
    （零成本无限过顶）也不得冲减——runner 看得见 prompt/产出，回落字符估算（与 OpenAICompatLLM 同口径）。"""
    from osca_cli.llm import LLMReply

    class NegLLM:
        def complete(self, system, user, *, tag, timeout=None):
            return LLMReply(text="草稿" * 50, tokens=-100, model="fake")

    run_episode(episode, loaded, proxy, policy, llm=NegLLM())
    assert episode.tokens_used > 0  # 非法上报按估算记账——既不冲减、也不白嫖
    assert all((s.get("tokens") or 0) >= 0 for s in episode.steps)


def test_agent_step_zero_report_still_depletes_budget(episode, loaded, proxy, policy):
    """最小复现反转：tokens=0 自报 + max_tokens 小额度 → 调用不能零成本反复通过，估算记账终会触顶。"""
    from osca_cli.llm import LLMReply

    class ZeroLLM:
        def complete(self, system, user, *, tag, timeout=None):
            return LLMReply(text="产出" * 200, tokens=0, model="fake")

    episode.budget = dict(episode.budget, max_tokens=1)  # aware 硬顶收到 1 token
    run_episode(episode, loaded, proxy, policy, llm=ZeroLLM())
    assert episode.status == "stopped" and "预算硬顶" in episode.stop_reason  # 估算记账 → 触顶即停
    assert episode.tokens_used > 1


def test_optimizer_rejects_nan_and_infinity(loaded):
    """GPT Review 复审 P2：NaN 不触发 float() 异常却毒化排序（NaN 候选可被选为 selected）——
    非有限数一律拒绝，不进排序。"""
    spec = {"step": "寻优", "performer": "optimizer", "input": "候选"}
    objects = {"OBJ-9": {"object_id": "OBJ-9", "kind": "objective", "optimize": "maximize"}}
    for poison in (float("nan"), float("inf"), "-Infinity", "NaN"):
        artifacts = {"候选": [{"value": 1}, {"value": poison}]}
        plan, detail = _run_optimizer(spec, artifacts, objects)
        assert plan is None and "非有限数" in detail, poison


def test_agent_step_passes_remaining_deadline_as_llm_timeout(episode, loaded, proxy, policy):
    """GPT Review 复审 P2：max_minutes 剩余时间须传导为单次 LLM 调用超时——
    只剩数秒时不许再吊默认 120s 外呼继续烧外部成本。"""
    from osca_cli.llm import LLMReply

    seen = {}

    class TimeoutProbe:
        def complete(self, system, user, *, tag, timeout=None):
            seen["timeout"] = timeout
            return LLMReply(text="草稿", tokens=5, model="fake")

    episode.budget = dict(episode.budget, max_minutes=1)  # 剩余 ≤60s
    run_episode(episode, loaded, proxy, policy, llm=TimeoutProbe())
    assert seen["timeout"] is not None and 0 < seen["timeout"] <= 60


def test_performer_substring_no_longer_matches(episode, loaded, proxy, policy):
    """GPT Review 复审 P2：performer 受限语法（parse_performer，lint 同源）——
    `not-a-connector` 不得再按子串识别成 connector，步骤直接拒绝。"""
    episode.context = copy.deepcopy(episode.context)
    episode.context["structure"]["pipeline"] = [{"step": "怪步", "performer": "not-a-connector"}]
    run_episode(episode, loaded, proxy, policy)
    assert episode.status == "failed" and "不可识别" in episode.stop_reason


def test_max_minutes_with_non_timeout_llm_fails_closed(episode, loaded, proxy, policy):
    """GPT 三审 P2：max_minutes 声明为硬顶时，timeout 是强制契约——不支持的可插拔 LLM
    fail-closed 拒绝发起（否则「只剩数秒仍无限外呼」把运行时硬预算做成 fail-open）。"""
    from osca_cli.llm import LLMReply

    class NoTimeoutLLM:
        def complete(self, system, user, *, tag):  # 无 timeout、无 **kwargs
            return LLMReply(text="草稿", tokens=5, model="fake")

    episode.budget = dict(episode.budget, max_minutes=1)
    run_episode(episode, loaded, proxy, policy, llm=NoTimeoutLLM())
    assert episode.status == "failed" and "timeout" in episode.stop_reason


def test_var_keyword_llm_receives_timeout(episode, loaded, proxy, policy):
    """GPT 三审 P2：**kwargs 兜收的适配器同样满足契约、且真实收到 timeout。"""
    from osca_cli.llm import LLMReply

    seen = {}

    class KwLLM:
        def complete(self, system, user, **kwargs):
            seen.update(kwargs)
            return LLMReply(text="草稿", tokens=5, model="fake")

    episode.budget = dict(episode.budget, max_minutes=1)
    run_episode(episode, loaded, proxy, policy, llm=KwLLM())
    assert "timeout" in seen and 0 < seen["timeout"] <= 60


def test_no_max_minutes_no_timeout_requirement(episode, loaded, proxy, policy):
    """未声明 max_minutes 时不要求 timeout 契约——无时间硬顶即无强制传导（不误伤旧适配器）。"""
    from osca_cli.llm import LLMReply

    class NoTimeoutLLM:
        def complete(self, system, user, *, tag):
            return LLMReply(text="草稿", tokens=5, model="fake")

    episode.budget = {k: v for k, v in episode.budget.items() if k != "max_minutes"}
    run_episode(episode, loaded, proxy, policy, llm=NoTimeoutLLM())
    assert episode.status == "completed"


def test_max_minutes_enforced_after_slow_last_connector_step(loaded, policy, proxy, monkeypatch):
    """P2：max_minutes 只在步前查——慢连接器作为末步超时后仍标 completed。fake clock 复现：
    连接器调用吃掉 2 分钟（预算 1 分钟）→ 剧集必须 stopped，不许「超时的完成」进对账。"""
    import osca_host.runner as runner_mod
    from osca_host.episode import Episode

    class FakeTime:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

    fake = FakeTime()
    monkeypatch.setattr(runner_mod, "time", fake)
    real_call = proxy.call

    def slow_call(*args, **kwargs):
        fake.now += 120.0  # 慢连接器：一次调用走掉 2 分钟墙钟
        return real_call(*args, **kwargs)

    monkeypatch.setattr(proxy, "call", slow_call)
    episode = Episode(
        episode_id="EP-0042",
        package_id=loaded.package_id,
        aware_id="AW-001",
        fired_trigger="AW-001/T3",
        assembled_at="2026-07-21T09:00:00+08:00",
        then="STR-001",
        budget={"max_minutes": 1},
        context={
            "structure": {"pipeline": [{"step": "取数", "performer": "connector", "uses": "CON-001.拉取费用明细"}]},
            "objects": {},
            "judgments": [],
        },
    )
    result = run_episode(episode, loaded, proxy, policy)
    assert result.status == "stopped", result.stop_reason
    assert "max_minutes" in result.stop_reason
    assert episode.steps and episode.steps[-1]["status"] == "done"  # 步已执行留痕，只是不算 completed


def test_max_minutes_blocks_second_ref_within_connector_step(loaded, policy, proxy, monkeypatch):
    """复核 P2：一步展开多接口时,首接口耗尽预算后**第二个外呼一次都不许发起**（调用次数为 0）,
    不是只在收尾改判 stopped。"""
    import osca_host.runner as runner_mod
    from osca_host.episode import Episode

    class FakeTime:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

    fake = FakeTime()
    monkeypatch.setattr(runner_mod, "time", fake)
    calls: list[str] = []
    real_call = proxy.call

    def slow_call(ref, *args, **kwargs):
        calls.append(ref)
        fake.now += 120.0  # 首接口吃掉 2 分钟（预算 1 分钟）
        return real_call(ref, *args, **kwargs)

    monkeypatch.setattr(proxy, "call", slow_call)
    episode = Episode(
        episode_id="EP-0043",
        package_id=loaded.package_id,
        aware_id="AW-001",
        fired_trigger="AW-001/T3",
        assembled_at="2026-07-21T09:00:00+08:00",
        then="STR-001",
        budget={"max_minutes": 1},
        context={
            "structure": {
                "pipeline": [{"step": "取数", "performer": "connector", "uses": "CON-001"}]
            },  # 裸 ID 展开 2 接口
            "objects": {},
            "judgments": [],
        },
    )
    result = run_episode(episode, loaded, proxy, policy)
    assert result.status == "stopped" and "不再发起下一外呼" in result.stop_reason
    assert len(calls) == 1, calls  # 第二接口调用次数为 0
    assert episode.steps[-1]["status"] == "stopped" and len(episode.steps[-1]["receipts"]) == 1


def test_connector_call_receives_remaining_budget_as_timeout(loaded, policy, proxy, monkeypatch):
    """复核 P2：可阻塞 connector 接收剩余 timeout——runner 把 max_minutes 剩余秒数传给 proxy.call。"""
    import osca_host.runner as runner_mod
    from osca_host.episode import Episode

    class FakeTime:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

    monkeypatch.setattr(runner_mod, "time", FakeTime())
    seen: list[object] = []
    real_call = proxy.call

    def spying_call(ref, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real_call(ref, *args, **kwargs)

    monkeypatch.setattr(proxy, "call", spying_call)
    episode = Episode(
        episode_id="EP-0044",
        package_id=loaded.package_id,
        aware_id="AW-001",
        fired_trigger="AW-001/T3",
        assembled_at="2026-07-21T09:00:00+08:00",
        then="STR-001",
        budget={"max_minutes": 2},
        context={
            "structure": {"pipeline": [{"step": "取数", "performer": "connector", "uses": "CON-001.拉取费用明细"}]},
            "objects": {},
            "judgments": [],
        },
    )
    result = run_episode(episode, loaded, proxy, policy)
    assert result.status == "completed", result.stop_reason
    assert seen == [120.0]  # 剩余预算（2 分钟整,fake clock 未走动）已传导


# ── M8-T3-c：写步 input.from —— 从上游产物字典里取哪一格 ──────────────

READ_REF = "CON-009.查待办"
TODO_ROWS = {"待办": [{"单位": "甲厂", "动作": "限额审批", "经办人手机": "13812345678"}]}


def _add_read_connector(proxy, policy):
    """测试内追加一个**只读**连接器（映射形状与 loader 产出同构：connectors/interfaces/bindings 三处）。

    样例包只有 CON-001，而写测试把它整只标成写连接器（is_write 是连接器级），
    「取数 → 写」的上游取数步必须挂在另一个 write: forbidden 的连接器上。
    """
    fixtures = Path(proxy.bindings["FINANCE_DB"]["endpoint"].removeprefix("mock://"))
    (fixtures / "查待办.yaml").write_text(yaml.safe_dump(TODO_ROWS, allow_unicode=True), encoding="utf-8")
    proxy.connectors["CON-009"] = {
        "connector_id": "CON-009",
        "binding_ref": "FINANCE_DB",
        "permissions": {"write": "forbidden"},
        "interfaces": [{"name": "查待办"}],
    }
    proxy.interfaces[READ_REF] = {"name": "查待办"}
    policy.permissions["取数"] = {READ_REF}


def _read_then_write_pipeline(episode, proxy, policy, write_input, write_ref="CON-001.拉取费用明细"):
    """[connector 取数 → connector 写步] 管线：上游产物是真实的 `{接口ref: 回执}` 字典。"""
    episode.context = copy.deepcopy(episode.context)  # structure 与包共享引用，改前先拷贝
    _add_read_connector(proxy, policy)
    proxy.connectors["CON-001"].setdefault("permissions", {})["write"] = "allowed_with_approval"
    policy.permissions["下发"] = {write_ref}
    policy.approvals[write_ref] = "专家"
    episode.context["structure"]["pipeline"] = [
        {"step": "取数", "performer": "connector", "uses": READ_REF, "produces": "取数结果"},
        {"step": "下发", "performer": "connector", "uses": write_ref, "input": write_input},
    ]
    return write_ref


def _redacted_rows(policy):
    """上游回执注入剧集前已脱敏——被写内容的期望值以脱敏后为准（与真实取数路径同口径）。"""
    value, _ = policy.redact(TODO_ROWS)
    return value


def test_write_input_without_from_takes_whole_upstream_dict(episode, loaded, proxy, policy, llm):
    """不写 from = 行为一字不变：写 params ＝ 上游**整份** `{接口ref: 回执}` 字典（既有包的既有契约，
    与 test_write_e2e 的逐字断言同源）。本条是 T3-c 的向后兼容证据。"""
    from osca_host.challenge import payload_digest

    _read_then_write_pipeline(episode, proxy, policy, {"ref": "取数结果"})
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "suspended_pending_approval"
    [ch] = policy.pending_challenges()
    assert ch["payload_digest"] == payload_digest({READ_REF: _redacted_rows(policy)})


def test_write_input_from_narrows_to_single_upstream_ref(episode, loaded, proxy, policy, llm):
    """写 from = 收窄到字典里的那一格：写 params ＝ 该接口回执本身，不再是 `{接口ref: 回执}` 包装。"""
    from osca_host.challenge import payload_digest

    _read_then_write_pipeline(episode, proxy, policy, {"ref": "取数结果", "from": READ_REF})
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "suspended_pending_approval"
    [ch] = policy.pending_challenges()
    rows = _redacted_rows(policy)
    assert ch["payload_digest"] == payload_digest(rows)
    assert ch["payload_digest"] != payload_digest({READ_REF: rows})  # 收窄真的发生了
    assert ch["payload_display"] == rows  # W6-4：审批卡呈的是那一格，不是一坨回执字典


def test_write_input_dangling_from_fails_closed(episode, loaded, proxy, policy, llm):
    """from 指向的 ref 不在上游产物里 → fail-closed（剧集失败 + 人话列出可选 ref），
    绝不回落成「取整份」——回落等于把一坨回执字典当被写内容送上 wire。零写：一张挑战都没挂。"""
    _read_then_write_pipeline(episode, proxy, policy, {"ref": "取数结果", "from": "CON-009.查空闲"})
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "failed"
    assert "CON-009.查空闲" in episode.stop_reason and "可选 ref" in episode.stop_reason
    assert READ_REF in episode.stop_reason  # 人话说清有哪些 ref 可选
    assert policy.pending_challenges() == []


def test_input_from_on_non_dict_artifact_fails_closed(episode, loaded, proxy, policy, llm):
    """上游产物不是字典却写了 from → fail-closed（不猜「大概是想要整份」）。"""
    _write_pipeline(episode, proxy, policy)  # 上游是 agent 文本草稿（str）
    episode.context["structure"]["pipeline"][1]["input"] = {"ref": "草稿", "from": READ_REF}
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "failed" and "不是可按 ref 取格的字典" in episode.stop_reason
    assert policy.pending_challenges() == []


def test_resolve_input_units():
    """取参口径的单元判据（_resolve_input 是写步/agent 步共用的唯一入口）。"""
    from osca_host.runner import _resolve_input

    artifacts = {"取数结果": {READ_REF: [1, 2]}, "草稿": "一段人话"}
    assert _resolve_input({"step": "x"}, artifacts) == (None, None, "")  # 无 input
    assert _resolve_input({"input": "草稿"}, artifacts) == ("草稿", "一段人话", "")  # 裸字符串
    assert _resolve_input({"input": {"ref": "取数结果"}}, artifacts)[1] == {READ_REF: [1, 2]}  # 整份
    assert _resolve_input({"input": {"ref": "取数结果", "from": READ_REF}}, artifacts)[1] == [1, 2]  # 收窄
    _, value, error = _resolve_input({"input": {"ref": "缺的"}}, artifacts)
    assert value is None and "缺失" in error


# ── M8-T3-b：agent 步结构化产出（人话 draft 与结构化产物并存） ──────────

SHAPED_ROW = {"工单标题": "压降差旅费", "目标单位": "甲厂", "处置动作": "限额审批", "经办人手机": "13812345678"}
# 台账副本（M8-T4 ④）：steps[*].structured_output 存的是 policy.redact 后的副本——被写内容仍是上面的原文。
SHAPED_ROW_REDACTED = {**SHAPED_ROW, "经办人手机": "***手机号已脱敏***"}
ENVELOPE = json.dumps(
    {"说明": "甲厂差旅费超阈值，建议下发限额审批（J-0417）。", "数据": SHAPED_ROW}, ensure_ascii=False
)


def _structured_llm(tmp_path, output: str, step: str = "整形") -> MockLLM:
    """MockLLM（与真实 LLM 通道同一 complete 契约、同一调用路径）喂定制产出。"""
    d = tmp_path / f"structured-{abs(hash(output)) % 10**8}" / "episode"
    d.mkdir(parents=True)
    (d / f"{step}.md").write_text(output, encoding="utf-8")
    return MockLLM(d.parent)


def _shape_then_write_pipeline(episode, proxy, policy, *, produces=None, write_ref="CON-001.拉取费用明细"):
    """端到端管线：读（真回执字典）→ agent 结构化整形（收窄到那一格）→ 写（拿到一行待写数据）。"""
    _read_then_write_pipeline(episode, proxy, policy, {"ref": "待写行"}, write_ref=write_ref)
    episode.context["structure"]["pipeline"].insert(
        1,
        {
            "step": "整形",
            "performer": "agent",
            "input": {"ref": "取数结果", "from": READ_REF},
            "produces": produces if produces is not None else {"ref": "待写行", "as": "json"},
        },
    )
    return write_ref


def test_structured_agent_step_feeds_pipeline_and_keeps_human_draft(episode, loaded, proxy, policy, tmp_path):
    """T3-b 主干：结构化产物进 artifacts（dict，不是 JSON 字符串），**人话 draft 并存**（仍非空、仍是人话），
    产物可溯源到上游产物（机器可查），写步拿到的是**一行真正要写的数据**。"""
    from osca_host.challenge import payload_digest

    _shape_then_write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=_structured_llm(tmp_path, ENVELOPE))

    assert episode.status == "suspended_pending_approval"
    shaped = next(s for s in episode.steps if s["step"] == "整形")
    # ① 结构化产物是 dict/list，不是文本（台账里那份是脱敏副本——M8-T4 ④，形状与类型不变）
    assert shaped["structured_output"] == SHAPED_ROW_REDACTED and isinstance(shaped["structured_output"], dict)
    # ② draft 并存：人话仍在 episode.draft（capture/frontdesk/控制台快照的消费口径不被夺走），且不是 JSON
    assert episode.draft == "甲厂差旅费超阈值，建议下发限额审批（J-0417）。"
    assert not episode.draft.lstrip().startswith("{") and episode.summary()["draft_ready"] is True
    # ③ 可溯源（机器可查）：产物 ← 哪个上游产物的哪一格
    assert shaped["produced"] == "待写行" and shaped["produced_as"] == "json"
    assert shaped["derived_from"] == {"input": "取数结果", "from": READ_REF}
    # ④ 写步拿到的是一行待写数据本身（不是 {接口ref: …} 包装、也不是文本）
    [ch] = policy.pending_challenges()
    assert ch["payload_digest"] == payload_digest(SHAPED_ROW)
    assert ch["payload_display"]["目标单位"] == "甲厂"  # W6-4：审批人看得懂自己在批什么
    assert ch["payload_display"]["经办人手机"] == "***手机号已脱敏***"  # 显示脱敏（digest 仍绑原文）


def test_structured_write_lands_the_row_end_to_end(episode, loaded, proxy, policy, tmp_path):
    """端到端收口：读 → 整形 → 批准 → 恢复消费 → 写落地的就是那一行（被写内容 = 结构化产物原文，
    未被显示脱敏改写——脱敏只脱显示，不动被写内容）。"""
    _shape_then_write_pipeline(episode, proxy, policy)
    llm = _structured_llm(tmp_path, ENVELOPE)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)

    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "completed"
    [landed] = _landed(_write_step(episode))
    # mock 写回执回显被批准的被写内容；回执入档前过脱敏（故手机号在回执里是标记，被写内容仍是原文）
    assert landed["payload"]["applied"]["目标单位"] == "甲厂"
    assert "CON-009.查待办" not in landed["payload"]["applied"]  # 不是一坨读回执字典
    assert episode.draft and "甲厂" in episode.draft  # 人话 draft 全程并存


def test_structured_step_requires_input_for_traceability(episode, loaded, proxy, policy, tmp_path):
    """可溯源纪律（A6 边界的守护）：结构化产出的 agent 步无 input = 凭空造数 → 拒绝**发起调用**。"""
    _shape_then_write_pipeline(episode, proxy, policy)
    del episode.context["structure"]["pipeline"][1]["input"]
    llm = _structured_llm(tmp_path, ENVELOPE)
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "failed" and "可溯源" in episode.stop_reason
    assert llm.calls == [] and policy.pending_challenges() == []  # 一次外呼都没发起、零写


def test_unknown_produces_as_rejected(episode, loaded, proxy, policy, tmp_path):
    """produces.as 是受限词表：词表外的形态直接拒绝，不猜（与 performer 受限集同纪律）。"""
    _shape_then_write_pipeline(episode, proxy, policy, produces={"ref": "待写行", "as": "yaml"})
    llm = _structured_llm(tmp_path, ENVELOPE)
    run_episode(episode, loaded, proxy, policy, llm=llm)

    assert episode.status == "failed" and "produces.as「yaml」不可识别" in episode.stop_reason
    assert llm.calls == []


def _nested(depth: int, *, envelope: bool = True) -> str:
    """depth 层纯嵌套数组的信封（或裸数据）。层数是**数据**的嵌套层数。"""
    data = "[" * depth + "]" * depth
    return '{"说明": "人话", "数据": ' + data + "}" if envelope else data


ILLEGAL_OUTPUTS = {
    "纯文本": "甲厂差旅费超阈值，建议下发限额审批。",
    "半截 JSON": '{"说明": "人话", "数据": {"目标单位": "甲',
    "markdown 围栏": f"```json\n{ENVELOPE}\n```",
    "顶层数组": '[{"目标单位": "甲厂"}]',
    "顶层标量": '"就一个字符串"',
    "缺说明": '{"数据": {"目标单位": "甲厂"}}',
    "说明空白": '{"说明": "   ", "数据": {"目标单位": "甲厂"}}',
    "缺数据": '{"说明": "人话"}',
    "数据非结构": '{"说明": "人话", "数据": "甲厂 限额审批"}',
    "夹带未知键": '{"说明": "人话", "数据": {"a": 1}, "备注": "顺手夹带"}',
    "空产出": "",
    "超长": '{"说明": "人话", "数据": {"备注": "' + "长" * 100_001 + '"}}',
    "深嵌套": '{"说明": "人话", "数据": ' + "[" * 20_000 + "]" * 20_000 + "}",
    # ── 漏网口 ①：JSON 规范外的字面量（RFC 8259 无 NaN/Infinity，Python json 默认收） ──
    "NaN 字面量": '{"说明": "人话", "数据": {"金额": NaN}}',
    "Infinity 字面量": '{"说明": "人话", "数据": {"上限": Infinity}}',
    "负 Infinity 字面量": '{"说明": "人话", "数据": {"下限": -Infinity}}',
    "数组里的 NaN": '{"说明": "人话", "数据": [1, NaN, 3]}',
    "深层嵌套的 NaN": '{"说明": "人话", "数据": {"行": [{"金额": NaN}]}}',
    "说明位的 NaN": '{"说明": NaN, "数据": {"a": 1}}',
    # 同一条纪律的另一条进口：合法 JSON 数字**溢出**成 inf（parse_constant 拦不住），
    # 落盘/上 wire 时 json.dumps 照样吐 `Infinity` —— 后果与字面量完全一致
    "浮点溢出成 inf": '{"说明": "人话", "数据": {"金额": 1e999}}',
    "浮点溢出成 -inf": '{"说明": "人话", "数据": {"金额": -1e999}}',
    # ── 漏网口 ②：重复键（json.loads 默认静默取最后一个） ──
    "信封重复键": '{"说明": "A 版本", "说明": "B 版本", "数据": {"a": 1}}',
    "信封重复数据键": '{"说明": "人话", "数据": {"金额": 1}, "数据": {"金额": 999}}',
    "数据内层重复键": '{"说明": "人话", "数据": {"金额": 100, "金额": 999999}}',
    "数据深层重复键": '{"说明": "人话", "数据": {"行": [{"单位": "甲厂", "单位": "乙厂"}]}}',
    # ── 漏网口 ③：深嵌套（解析放行 9996 层，下游 330 层即炸栈） ──
    "深嵌套-越上限一层": _nested(33),
    "深嵌套-下游炸栈档": _nested(331),
    "深嵌套-解析上限档": _nested(9996),
}


@pytest.mark.parametrize("name", sorted(ILLEGAL_OUTPUTS))
def test_illegal_structured_output_always_fails_closed(name, episode, loaded, proxy, policy, tmp_path):
    """非法 JSON **一律 fail-closed，绝不退回文本**——退回文本 = 把方案 3 悄悄退化回今天的洞。
    判据四条：剧集 failed / 人话报错 / draft 不被非法产出污染 / 下游写步零触达（无挑战、无落地）。"""
    _shape_then_write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=_structured_llm(tmp_path, ILLEGAL_OUTPUTS[name]))

    assert episode.status == "failed", name
    assert episode.stop_reason and "结构化" not in episode.status
    assert episode.draft is None, name  # 没有「解析不了就当草稿用」
    assert policy.pending_challenges() == []  # 写步一步没跑：零挑战、零写
    assert [s["step"] for s in episode.steps] == ["取数", "整形"] and episode.steps[-1]["status"] == "failed"


# ── 三个漏网口的定点判据（上面的矩阵证「四条判据成立」，下面证「拦在哪、话说的是什么」） ──


@pytest.mark.parametrize(
    "raw,keyword",
    [
        ('{"说明": "人话", "数据": {"金额": NaN}}', "NaN"),
        ('{"说明": "人话", "数据": {"上限": Infinity}}', "Infinity"),
        ('{"说明": "人话", "数据": {"下限": -Infinity}}', "-Infinity"),
        ('{"说明": "人话", "数据": {"金额": 1e999}}', "1e999"),
    ],
)
def test_json_extension_literals_are_rejected_by_name(raw, keyword):
    """NaN/Infinity 不是 JSON（RFC 8259），Python 的 json 却默认收：一路放行会让审批人对着 nan 拍板、
    让 L2 快照落一份非 Python 读者解不开的非法 JSON、让 wire body 带 `NaN` 发给写后端。
    报错须指名道姓说是哪个字面量——人看得懂才改得了提示词。"""
    parsed, error = _parse_structured(raw)
    assert parsed is None
    assert keyword in error and "JSON" in error


def test_finite_numbers_still_pass():
    """闸只咬非有限数：正常浮点/整数/科学计数照旧放行（别把闸修成把合法数值一起拒了）。"""
    parsed, error = _parse_structured('{"说明": "人话", "数据": {"金额": 1.5, "件数": 3, "占比": 1e30}}')
    assert error == "" and parsed["data"] == {"金额": 1.5, "件数": 3, "占比": 1e30}


def test_structured_artifact_is_always_legal_json():
    """放行的结构化产物**必是合法 JSON**——它要进 L2 快照（落盘）、上审批卡、原样上 wire。
    allow_nan=False 就是 RFC 8259 的口径：这条断言是三处下游的共同前置。"""
    parsed, _ = _parse_structured(ENVELOPE)
    json.dumps(parsed["data"], allow_nan=False)  # 不抛即合法


@pytest.mark.parametrize(
    "raw,dup",
    [
        ('{"说明": "A 版本", "说明": "B 版本", "数据": {"a": 1}}', "说明"),
        ('{"说明": "人话", "数据": {"a": 1}, "数据": {"a": 2}}', "数据"),
        ('{"说明": "人话", "数据": {"金额": 100, "金额": 999999}}', "金额"),
        ('{"说明": "人话", "数据": {"行": [{"单位": "甲厂", "单位": "乙厂"}]}}', "单位"),
    ],
)
def test_duplicate_keys_rejected_at_every_level(raw, dup):
    """重复键：json.loads 默认静默取**最后一个**——「取哪一份」是猜，猜错即写错内容。
    信封层与「数据」内层同一把闸（一个 object_pairs_hook 覆盖所有层级）：两层的后果是同一个
    ——被写内容/审批卡/wire body 都只剩最后一份，谁也看不出还有过第一份。报错须点名重复的那个键。"""
    parsed, error = _parse_structured(raw)
    assert parsed is None
    assert dup in error and "重复键" in error


def test_structured_depth_boundary():
    """深度上限按**下游真正扛得住的数**定（见 runner.MAX_STRUCTURED_DEPTH 注释里的实测）：
    上限层放行、上限 + 1 层拒绝，且报错说清是嵌套超限（不是笼统「不是合法 JSON」）。"""
    from osca_host.runner import MAX_STRUCTURED_DEPTH

    parsed, error = _parse_structured(_nested(MAX_STRUCTURED_DEPTH))
    assert error == "" and parsed is not None  # 恰好上限：放行
    parsed, error = _parse_structured(_nested(MAX_STRUCTURED_DEPTH + 1))
    assert parsed is None and "嵌套" in error and str(MAX_STRUCTURED_DEPTH) in error
    # 上限本身须**远低于**实测下游上限（下游最浅 330 层即炸栈，且随调用栈已用深度浮动）
    assert MAX_STRUCTURED_DEPTH * 8 <= 330


def _shape_then_agent_then_write_pipeline(episode, proxy, policy):
    """整形（结构化）→ **下游 agent 步吃它**（_step_user_prompt 走 yaml.safe_dump，3 帧/层，
    是下游最浅的一道）→ 写步。深嵌套的炸栈就发生在这条管线上。"""
    _shape_then_write_pipeline(episode, proxy, policy)
    episode.context["structure"]["pipeline"].insert(
        2, {"step": "成文", "performer": "agent", "input": {"ref": "待写行"}, "produces": "文稿"}
    )


def test_deep_nesting_fails_closed_instead_of_crashing_downstream(episode, loaded, proxy, policy, tmp_path):
    """修前实测：331 层解析放行 → 下游 agent 步的 yaml.safe_dump 炸 RecursionError，**未捕获冲出
    run_episode**，剧集停在 status=running（既非 failed 也无 stop_reason，台账里是一具活死人）。
    修后：解析处即 fail-closed，剧集 failed、下游一步没跑。"""
    _shape_then_agent_then_write_pipeline(episode, proxy, policy)
    d = tmp_path / "deep" / "episode"
    d.mkdir(parents=True)
    (d / "整形.md").write_text(_nested(331), encoding="utf-8")
    (d / "成文.md").write_text("一段人话文稿。", encoding="utf-8")

    run_episode(episode, loaded, proxy, policy, llm=MockLLM(d.parent))  # 修前此处抛 RecursionError

    assert episode.status == "failed" and "嵌套" in episode.stop_reason
    assert [s["step"] for s in episode.steps] == ["取数", "整形"]  # 下游 agent 步、写步都没跑
    assert episode.draft is None and policy.pending_challenges() == []


def test_illegal_structured_output_still_charges_tokens(episode, loaded, proxy, policy, tmp_path):
    """解析失败不白嫖额度：外呼已发生，tokens 照记账（aware 侧累加 + 笼子侧计费），
    否则「产出非法就免费重试」是零成本绕过预算硬顶的口子。"""
    _shape_then_write_pipeline(episode, proxy, policy)
    run_episode(episode, loaded, proxy, policy, llm=_structured_llm(tmp_path, "不是 JSON"))

    assert episode.status == "failed" and episode.tokens_used > 0
    assert episode.steps[-1]["tokens"] > 0
    assert any("tokens 记账" in a["reason"] for a in policy.audit)


def test_structured_output_ledger_copy_is_redacted_but_write_content_is_not(episode, loaded, proxy, policy, tmp_path):
    """脱敏边界（M8-T4 ④）：结构化产出**分两份走**，三条须同时成立——

    ① 台账副本已脱：steps[*].structured_output 是 policy.redact 后的副本，与 connector 回执同权
       （回执一进台账就脱；结构化产出此前是台账里唯一不脱的字段 = PII 绕过脱敏进台账的旁路）；
    ② artifacts 原文未变：被写内容仍是原文——脱敏改写它等于把真实待写值写坏（预约行里的联系电话
       本就是合法字段），下游写步吃到的是原文；
    ③ digest 仍绑原文：附录 B.4 的既有对偶（digest 绑原文、display 只脱显示）一字不动。
    三条中任一条被改掉，本测试即红：②③ 咬的是同一份原文（digest 由 params 现算），①咬台账那份。"""
    from osca_host.challenge import payload_digest

    raw_row = {"目标单位": "甲厂", "经办人手机": "13812345678"}
    redacted_row = {"目标单位": "甲厂", "经办人手机": "***手机号已脱敏***"}
    envelope = json.dumps({"说明": "经办人手机13812345678，请核。", "数据": raw_row}, ensure_ascii=False)
    _shape_then_write_pipeline(episode, proxy, policy)
    llm = _structured_llm(tmp_path, envelope)
    run_episode(episode, loaded, proxy, policy, llm=llm)

    shaped = next(s for s in episode.steps if s["step"] == "整形")
    assert episode.draft == "经办人手机***手机号已脱敏***，请核。"  # 人话过脱敏（既有口径不变）
    # ① 台账副本已脱（形状/类型不变，只是那一格成了标记）
    assert shaped["structured_output"] == redacted_row
    assert shaped["structured_output"]["经办人手机"] == "***手机号已脱敏***"
    # redacted 计的是**进台账的两份**（人话草稿 + 结构化副本）合计命中数，与 connector 回执同口径
    assert shaped["redacted"] == 2
    # ②③ 被写内容仍是原文：digest 绑原文、且**不等于**脱敏副本的摘要（副本若流去当被写内容，此处即红）
    [ch] = policy.pending_challenges()
    assert ch["payload_digest"] == payload_digest(raw_row)
    assert ch["payload_digest"] != payload_digest(redacted_row)
    assert ch["payload_display"]["经办人手机"] == "***手机号已脱敏***"  # display 只脱显示

    # ② 的端到端硬证据：批准后恢复，consume 按**当刻现算的 params 摘要**匹配——artifacts 若被脱敏改写过，
    # 摘要对不上，写必回落成「未写」。写真落地 = artifacts 里那份自始至终是原文。
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "completed"
    [landed] = _landed(_write_step(episode))
    assert landed["payload"]["applied"]["目标单位"] == "甲厂"
    assert not any(s.get("status") == "denied" for s in episode.steps)  # 没有回落成保守默认


def test_structured_prompt_carries_envelope_and_a6_contract(episode):
    """提示词契约：结构化步须明示信封两格、禁围栏、A6 边界（只整形输入里已有的取数结果、不得凭空造数），
    且归属纪律（判断 ID 标注）不因换形态而丢。"""
    spec = {"step": "整形", "performer": "agent", "produces": {"ref": "待写行", "as": "json"}}
    user = _step_user_prompt(spec, "整形", "取数结果", {"CON-009.查待办": [1]}, structured=True)
    assert '"说明"' in user and '"数据"' in user and "markdown" in user
    assert "不得凭空造数" in user and "判断 ID 标注" in user
    # 文本步一字不变（既有契约）
    assert "只输出本步骤产出物的内容本身" in _step_user_prompt({"step": "成文"}, "成文", None, None)


# ── M8-T4 ⑥：run_episode 的边界捕获（台账不留 running 僵尸） ──────────────


class _Boom:
    """可插拔注入的 LLM 通道在 complete 里炸——**非解析路径**的未预期异常。
    带一句含敏感内文的报错，用来咬「异常内文不进台账」。"""

    model = "mock"
    SECRET = "postgres://osca:S3CR3T-TOKEN@10.0.0.9:5432/prod"

    def __init__(self, exc_type):
        self.exc_type = exc_type

    def complete(self, system, user, tag=None, timeout=None):
        raise self.exc_type(f"连不上写后端 {self.SECRET}")


def _deeply_nested_objects(episode):
    """把剧集上下文的 objects 塞成极深嵌套——render_system_prompt 的 yaml.safe_dump 会在这里
    真炸 RecursionError（**不是** _parse_structured 那条解析路径，也没有 mock 任何东西）。
    深度按当前递归上限取：yaml 每层吃约 3 帧，取上限层数必越界，不依赖 1000 这个具体数。"""
    import sys

    episode.context = copy.deepcopy(episode.context)
    nested: object = []
    for _ in range(sys.getrecursionlimit()):
        nested = [nested]
    episode.context["objects"] = {"OBJ-DEEP": nested}


def test_recursion_error_outside_parsing_lands_failed_not_running_zombie(episode, loaded, proxy, policy, llm):
    """⑥ 的主判据：从**非解析路径**冒出的 RecursionError（渲染一次性上下文时 yaml 炸栈）不再未捕获
    冲出 run_episode。剧集须落 failed + 人话 stop_reason + finished_at，**绝不是 running**——
    status 是所有下游的分派键，running 的语义是「还在跑，别碰它」，僵尸永远没人收、没人报。"""
    _deeply_nested_objects(episode)

    result = run_episode(episode, loaded, proxy, policy, llm=llm)  # 修前此处抛 RecursionError

    assert result is episode
    assert episode.status == "failed" and episode.status != "running"
    assert episode.stop_reason and "RecursionError" in episode.stop_reason  # 人话说清是什么把它打断的
    assert "内部错误" in episode.stop_reason and "running" in episode.stop_reason
    assert episode.finished_at is not None  # 终态该有的都有，台账收得干净
    assert episode.steps == [] and llm.calls == []  # 炸在第一步之前：一次外呼都没发起


@pytest.mark.parametrize("exc_type", [RecursionError, AttributeError, RuntimeError, MemoryError])
def test_boundary_records_type_but_never_leaks_exception_text(exc_type, episode, loaded, proxy, policy):
    """捕获范围取 Exception（不只 RecursionError）：僵尸的危害与异常类型无关。
    但「宽」不等于「吞」——台账写下**类型名**（AttributeError 一眼看得出是编程错误、不是业务失败），
    终态是 failed 而非 completed/stopped。且**异常内文绝不进台账**：报错消息可能带连接串/令牌，
    而台账要进控制台快照、日志与归档。"""
    run_episode(episode, loaded, proxy, policy, llm=_Boom(exc_type))

    assert episode.status == "failed"
    assert exc_type.__name__ in episode.stop_reason
    assert _Boom.SECRET not in episode.stop_reason and "S3CR3T-TOKEN" not in episode.stop_reason
    # 整份台账（含逐步留痕）都不许带内文——步里也不许留一句「连不上写后端 postgres://…」
    assert "S3CR3T-TOKEN" not in json.dumps(episode.dump(), ensure_ascii=False, default=str)


def test_boundary_does_not_swallow_shutdown_signals(episode, loaded, proxy, policy):
    """**不捕 BaseException**：KeyboardInterrupt/SystemExit 是关停信号、不是剧集失败。
    吞掉它们会把 Ctrl-C 变成「剧集失败」、并挡住 P1 关停语义——关停必须能穿透执行器。"""
    for signal_type in (KeyboardInterrupt, SystemExit):
        with pytest.raises(signal_type):
            run_episode(episode, loaded, proxy, policy, llm=_Boom(signal_type))


def test_boundary_logs_full_traceback_to_host_log(episode, loaded, proxy, policy, caplog):
    """内文不进台账 ≠ 内文消失：完整堆栈照打进 Host 日志（与 host.py 既有边界同一行为，
    不是新增静默）——否则「宽捕获」就真成了吞掉编程错误。"""
    with caplog.at_level("ERROR", logger="osca-host"):
        run_episode(episode, loaded, proxy, policy, llm=_Boom(AttributeError))

    [record] = [r for r in caplog.records if "执行器内部错误" in r.getMessage()]
    assert record.exc_info is not None and record.exc_info[0] is AttributeError
    assert episode.episode_id in record.getMessage()  # 按剧集号查得到
