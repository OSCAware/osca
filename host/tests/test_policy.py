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


def test_kill_switch_garbage_threshold_does_not_crash():
    """正则容忍 '.' 这类伪数字——按不可求值处理，绝不许炸装载（半注册包的病根之一）。"""
    p = make(
        policy={**POLICY, "kill_switch": [{"when": "overruled/confirmed > ."}]},
        stats={"confirmed": 1, "overruled": 100},
    )
    assert not p.kill_tripped
    assert any("阈值不可解析" in a["reason"] for a in p.audit)


def test_interceptor_fails_closed_on_broken_safety_config():
    """fail-closed（七轮定稿）：安全段配置非法时保守默认朝安全侧倒——「有警告」不能替代安全效果。"""
    from osca_host.policy import REDACTORS

    p = make(
        policy={
            **POLICY,
            "data": {"redact": "身份证号"},  # 字符串会被逐字符遍历——此前脱敏被静默关闭
            "egress": {"allow_domains": "oscaware.com"},
        }
    )
    assert set(p.redact_categories) == set(REDACTORS)  # 脱敏配置非法 → 保守全开（宁可多脱不可泄露）
    assert p.redact("经办电话 13812345678")[1] == 1  # 真的在脱，不只是留警告
    assert p.egress_allow == set() and not p.authorize_egress("oscaware.com")[0]  # 默认全禁成立

    p2 = make(
        policy={
            **POLICY,
            "permissions": [{"step": "取数", "allow": ["CON-001.拉取费用明细", 42]}],  # 混合列表
            "approvals": ["oops"],
            "kill_switch": 42,
            "budgets": {"per_episode": ["oops"]},
        }
    )
    assert p2.permissions["取数"] == set()  # 混合列表不部分接受——整叶空白名单（默认拒绝）
    assert p2.kill_tripped and "配置错误" in p2.kill_reason  # kill switch 形状非法 → 停机
    assert p2.max_tool_calls == 0 and p2.max_tokens == 0  # 预算非法 → 额度撤销
    assert not p2.require_approval("终稿发送管理层")[0]  # 审批配置非法 → 一律拒绝
    assert not p2.require_approval("任意动作")[0]  # 「不在清单放行」的口子也关死

    p3 = make(policy={**POLICY, "data": {"redact": ["身份证"]}})  # 合法形状、未知类别
    assert set(p3.redact_categories) == set(REDACTORS)  # 未知类别同样保守全开
    p4 = make(policy={**POLICY, "kill_switch": [{"when": ["not", "string"]}]})
    assert p4.kill_tripped and "配置错误" in p4.kill_reason  # when 非字符串 → 停机
    p5 = make(policy={**POLICY, "data": "oops"})  # 父段本身非法——不得压成 {} 与「未声明」混同
    assert set(p5.redact_categories) == set(REDACTORS)
    p6 = make(policy={**POLICY, "kill_switch": [{"when": "   "}]})  # 空白 when 与 lint 谓词对齐
    assert p6.kill_tripped and "配置错误" in p6.kill_reason


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


def test_unparsable_budget_revokes_quota_not_unlimited():
    """错误预算不是无限额（fail-closed）：不可解析 → 额度撤销（0），任何调用即拒。"""
    p = make(policy={**POLICY, "budgets": {"per_episode": {"max_tool_calls": "unlimited"}}})
    assert p.max_tool_calls == 0
    ok, reason = p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-1")
    assert not ok and "预算硬顶" in reason
    assert any("额度撤销" in a["reason"] for a in p.audit)
    # 未声明 ≠ 非法：不写 max_tokens 是合法的「无硬顶」选择
    assert p.max_tokens is None


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


def test_redaction_matches_numbers_adjacent_to_chinese():
    """中文与数字同属正则 \\w——「手机号13812345678」在 \\b 边界下会整条漏掉（八轮实测病灶）。"""
    p = make()
    text, hits = p.redact("手机号13812345678；身份证号11010519491231002X")
    assert hits == 2
    assert "13812345678" not in text and "11010519491231002X" not in text
    # 长数字串里的片段不是完整号码——数字负向断言不误伤
    _, none_hits = p.redact("订单号 913812345678901")
    assert none_hits == 0


def test_zero_token_budget_denies_before_any_call():
    """额度撤销后「任何调用即拒」：预检在 LLM 调用之前拦，不是调用之后止损。"""
    p = make(policy={**POLICY, "budgets": {"per_episode": {"max_tokens": "unlimited"}}})
    assert p.max_tokens == 0
    ok, reason = p.precheck_tokens("EP-1")
    assert not ok and "拒绝发起" in reason


def test_grant_refused_and_status_honest_when_approvals_broken():
    """P2：配置损坏时授予必须失败——授出永不生效的 token、status 显示 granted 都是控制面撒谎。"""
    p = make(policy={**POLICY, "approvals": ["oops"]})
    ok, reason = p.grant_approval("终稿发送管理层")
    assert not ok and "配置非法" in reason
    assert p.snapshot()["approvals"] == "config_error/deny_all"


def test_audit_trail_records_decisions():
    p = make()
    p.authorize_tool("成文", "CON-001.拉取费用明细")
    denies = [a for a in p.audit if a["decision"] == "deny"]
    assert denies and denies[-1]["step"] == "成文"


# ── kill switch 第二可求值形式：回放红灯率 > X%（数据源 = 回放器健康档案，M3-W4） ──

REPLAY_POLICY = {**POLICY, "kill_switch": [{"when": "回放红灯率 > 20%"}]}


def test_replay_red_rate_trips_kill_switch():
    p = make(policy=REPLAY_POLICY, stats={**HEALTHY, "replay_red_rate": 0.5, "replay_at": "2026-07-11T10:00:00"})
    assert p.kill_tripped
    assert "回放红灯率 50%" in p.kill_reason and "2026-07-11" in p.kill_reason


def test_replay_red_rate_below_threshold_stays_quiet():
    p = make(policy=REPLAY_POLICY, stats={**HEALTHY, "replay_red_rate": 0.1})
    assert not p.kill_tripped


def test_replay_health_missing_is_availability_gap_not_config_error():
    """档案缺失 → 条件不生效留痕（保守默认）——与形状非法的停机是两回事。"""
    p = make(policy=REPLAY_POLICY, stats=HEALTHY)  # 无 replay_red_rate
    assert not p.kill_tripped
    assert any("回放健康档案缺失" in a["reason"] for a in p.audit if a["decision"] == "warn")


def test_replay_health_reader_conservative(tmp_path):
    from osca_host.policy import replay_health

    assert replay_health(tmp_path)["replay_red_rate"] is None  # 档案不存在

    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    health.write_text('{"red_rate": 0.25, "at": "2026-07-11T10:00:00"}', encoding="utf-8")
    stats = replay_health(tmp_path)
    assert stats["replay_red_rate"] == 0.25 and "2026-07-11" in stats["replay_at"]

    for bad in ("不是 JSON{{{", '{"red_rate": "多"}', '{"red_rate": 1.5}', '{"red_rate": true}', '["形状不对"]'):
        health.write_text(bad, encoding="utf-8")
        assert replay_health(tmp_path)["replay_red_rate"] is None, bad  # 损坏/越界一律保守


def test_sample_pack_replay_condition_now_evaluable(sample_pack):
    """样例包 policy 的「回放红灯率 > 20%」从 W4 起可求值：健康档案在场即真裁决。"""
    import yaml as _yaml

    policy_doc = _yaml.safe_load((sample_pack / "policy.yaml").read_text(encoding="utf-8"))
    p = PolicyInterceptor("demo", policy_doc, {"confirmed": 9, "overruled": 0, "replay_red_rate": 0.5})
    assert p.kill_tripped and "回放红灯率" in p.kill_reason
