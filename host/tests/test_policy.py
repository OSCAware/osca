"""Policy 拦截器：白名单默认拒绝、预算硬顶、审批门、脱敏、kill switch，全程审计。"""

from __future__ import annotations

from osca_host.policy import PolicyInterceptor

POLICY = {
    "policy_version": 1,
    "permissions": [
        {"step": "取数", "allow": ["CON-001.拉取费用明细"]},
        {"step": "成文", "allow": []},
    ],
    "budgets": {"per_episode": {"max_tool_calls": 2}},
    "data": {"redact": ["身份证号", "手机号"]},
    "approvals": [{"action": "终稿发送管理层", "approver": "专家"}],
    "kill_switch": [{"when": "近30天 overruled/confirmed > 0.3"}],
}

HEALTHY = {"confirmed": 10, "overruled": 1}


def make(policy=POLICY, stats=HEALTHY) -> PolicyInterceptor:
    return PolicyInterceptor("demo", policy, stats)


def test_whitelist_allow_and_deny():
    p = make()
    assert p.authorize_tool("取数", "CON-001.拉取费用明细")[0]
    ok, reason = p.authorize_tool("成文", "CON-001.拉取费用明细")
    assert not ok and "越权" in reason  # 成文步骤白名单为空——调不动任何 Connector


def test_unknown_step_default_deny():
    ok, reason = make().authorize_tool("未知步骤", "CON-001.拉取费用明细")
    assert not ok and "默认拒绝" in reason


def test_internal_call_bypasses_step_whitelist():
    assert make().authorize_tool(None, "CON-001.拉取费用明细")[0]  # precondition/轮询是运行时调用


def test_budget_hard_cap_per_episode():
    p = make()
    assert p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-1")[0]
    assert p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-1")[0]
    ok, reason = p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-1")
    assert not ok and "预算硬顶" in reason
    assert p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-2")[0]  # 按剧集独立计


def test_kill_switch_trips_on_unhealthy_ledger():
    p = make(stats={"confirmed": 10, "overruled": 4})  # 0.4 > 0.3
    assert p.kill_tripped
    ok, reason = p.authorize_tool("取数", "CON-001.拉取费用明细")
    assert not ok and "kill switch" in reason


def test_kill_switch_unparsable_condition_warns_not_trips():
    p = make(policy={**POLICY, "kill_switch": [{"when": "月亮是蓝色的"}]}, stats={"confirmed": 1, "overruled": 100})
    assert not p.kill_tripped
    assert any("不可机器求值" in a["reason"] for a in p.audit)


def test_approval_gate_one_shot():
    p = make()
    assert not p.require_approval("终稿发送管理层")[0]  # 未审批 → 拦
    ok, detail = p.grant_approval("终稿发送管理层")
    assert ok
    assert p.require_approval("终稿发送管理层")[0]  # 授予后放行一次
    assert not p.require_approval("终稿发送管理层")[0]  # 一次性：再次即拦
    assert p.require_approval("普通动作")[0]  # 不在清单的动作不设门
    assert not p.grant_approval("不存在的动作")[0]


def test_revoke_stops_all_calls():
    """包停触达认知平面：撤销后模型调用与运行时内部调用全部拒绝。"""
    p = make()
    assert p.authorize_tool("取数", "CON-001.拉取费用明细")[0]
    p.revoke("unload 包停")
    ok, reason = p.authorize_tool("取数", "CON-001.拉取费用明细")
    assert not ok and "包已停" in reason
    assert not p.authorize_tool(None, "CON-001.拉取费用明细")[0]  # 内部调用同样全拒


def test_kill_switch_refreshes_with_ledger():
    """账本健康度即安全信号（公理 A10）：M3 落账后计数恶化，Host 不重启也要看见。"""
    p = make()
    assert not p.kill_tripped  # 装载时健康
    p.refresh_kill_switch({"confirmed": 10, "overruled": 4})  # 0.4 > 0.3
    assert p.kill_tripped and "kill switch" in p.kill_reason
    p.refresh_kill_switch({"confirmed": 10, "overruled": 1})  # 账本自愈（推翻→重审→新判断）即恢复
    assert not p.kill_tripped


def test_unparsable_max_tool_calls_warns_and_disables_cap():
    p = make(policy={**POLICY, "budgets": {"per_episode": {"max_tool_calls": "十次"}}})
    assert p.max_tool_calls is None
    assert any("max_tool_calls 不可解析" in a["reason"] for a in p.audit)
    assert p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-1")[0]  # 顶不生效但不误伤调用


def test_write_approval_defaults_to_deny():
    """写动作默认拒绝：不在 approvals 清单的写接口没有合法路径；token 一次性消费。"""
    p = make()
    ok, reason = p.require_write_approval("CON-009.回写工单")
    assert not ok and "默认拒绝" in reason
    p.approvals["CON-009.回写工单"] = "专家"
    assert not p.require_write_approval("CON-009.回写工单")[0]  # 在清单但未授予 → 拦
    p.grant_approval("CON-009.回写工单")
    assert p.require_write_approval("CON-009.回写工单")[0]  # 授予后放行一次
    assert not p.require_write_approval("CON-009.回写工单")[0]  # token 已消费


def test_redaction():
    p = make()
    payload = {"rows": [{"经办": "张三 13812345678", "证件": "110101199001011234"}]}
    redacted, hits = p.redact(payload)
    assert hits == 2
    text = str(redacted)
    assert "13812345678" not in text and "110101199001011234" not in text
    assert "手机号已脱敏" in text and "身份证号已脱敏" in text


def test_audit_trail_records_decisions():
    p = make()
    p.authorize_tool("成文", "CON-001.拉取费用明细")
    denies = [a for a in p.audit if a["decision"] == "deny"]
    assert denies and denies[-1]["step"] == "成文"
