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
