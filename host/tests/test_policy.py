"""Policy 拦截器：白名单默认拒绝、预算硬顶、审批门、脱敏、kill switch，全程审计。"""

from __future__ import annotations

import pytest

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
    """绑定挑战审批门：首次拦并挂 pending 挑战 → approver 批 → 同绑定放行一次 → 一次性再拦。"""
    p = make()
    action = "终稿发送管理层"
    ok, detail = p.require_approval(action, episode_id="EP-1", payload={"x": 1})
    assert not ok and "审批门拦截" in detail  # 未批 → 拦，同时挂 pending 挑战
    [ch] = p.pending_challenges()
    # 幽灵字段回归：nonce 已删（装饰性防线，协议从未校验）
    assert ch["action"] == action and ch["approver"] == "专家" and "nonce" not in ch
    ok, _ = p.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert ok
    assert p.require_approval(action, episode_id="EP-1", payload={"x": 1})[0]  # 同绑定放行一次
    assert not p.require_approval(action, episode_id="EP-1", payload={"x": 1})[0]  # 一次性：consume 后再拦
    assert p.require_approval("普通动作", episode_id="EP-1", payload={})[0]  # 不在清单的动作不设门


def test_approval_binds_payload_no_swap():
    """偷梁换柱防线（端到端接线）：批「4.5 折」的挑战不得放行「1 折」的写。"""
    p = make()
    action = "终稿发送管理层"
    p.require_approval(action, episode_id="EP-1", payload={"折扣": "4.5"})  # 挂挑战 A（绑 4.5 折摘要）
    [ch] = p.pending_challenges()
    p.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert not p.require_approval(action, episode_id="EP-1", payload={"折扣": "1"})[0]  # 换 payload → 摘要不符，拒
    assert p.require_approval(action, episode_id="EP-1", payload={"折扣": "4.5"})[0]  # 原 payload 仍放行一次


def test_approval_deny_blocks_consume():
    """驳回：approver deny 后该挑战不可放行；同绑定重试会挂一张新 pending。"""
    p = make()
    action = "终稿发送管理层"
    p.require_approval(action, episode_id="EP-1", payload={})
    [ch] = p.pending_challenges()
    ok, _ = p.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=False)
    assert ok
    assert not p.require_approval(action, episode_id="EP-1", payload={})[0]  # 已驳回：consume 不中，另挂新 pending
    [ch2] = p.pending_challenges()
    assert ch2["challenge_id"] != ch["challenge_id"]


def test_approval_imposter_and_wrong_role_cannot_decide():
    """冒名/越权批不动：decide 须 role=approver 且 by_name 与挑战指定审批人相符（fail-closed）。"""
    p = make()
    p.require_approval("终稿发送管理层", episode_id="EP-1", payload={})
    [ch] = p.pending_challenges()
    assert not p.decide_challenge(ch["challenge_id"], by_name="冒名", by_role="approver", approve=True)[0]  # 名不符
    assert not p.decide_challenge(ch["challenge_id"], by_name="专家", by_role="operator", approve=True)[0]  # 角色不符
    assert not p.require_approval("终稿发送管理层", episode_id="EP-1", payload={})[0]  # 仍未获批 → 拦


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
    assert not p2.require_approval("终稿发送管理层", episode_id="EP-1", payload={})[0]  # 审批配置非法 → 一律拒绝
    assert not p2.require_approval("任意动作", episode_id="EP-1", payload={})[0]  # 「不在清单放行」的口子也关死

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
    """写动作默认拒绝：不在 approvals 清单的写接口没有合法路径；批准后一次性消费。"""
    p = make()
    ref = "CON-009.回写工单"
    ok, reason = p.require_write_approval(ref, episode_id="EP-1", payload="")
    assert not ok and "默认拒绝" in reason
    p.approvals[ref] = "专家"
    # 在清单但被写内容为空 → 仍 fail-closed（不对空摘要拍板），不挂挑战
    ok, detail = p.require_write_approval(ref, episode_id="EP-1", payload="")
    assert not ok and "被写内容" in detail and p.pending_challenges() == []
    payload = {"工单": "WO-1", "结论": "已处理"}
    ok, detail = p.require_write_approval(ref, episode_id="EP-1", payload=payload)
    assert not ok and "审批门拦截" in detail  # 在清单、有内容、未批 → 拦（挂 pending 挑战）
    [ch] = p.pending_challenges()
    p.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert p.require_write_approval(ref, episode_id="EP-1", payload=payload)[0]  # 批后同内容放行一次
    assert not p.require_write_approval(ref, episode_id="EP-1", payload=payload)[0]  # 一次性：consume 后再拦


# ── 审批授权 TTL 配置（W6-1：包级默认 default_ttl_seconds + 每动作 ttl_seconds 覆盖） ──


def _pending_ttl(p, action="终稿发送管理层", payload=None):
    """挂一张 pending 挑战并返回其 TTL = expires_at − created_at（二者同一 now，相减即配置窗口）。"""
    p.require_approval(action, episode_id="EP-1", payload=payload if payload is not None else {"x": 1})
    dto = next(c for c in p.pending_challenges() if c["action"] == action)
    return dto["expires_at"] - dto["created_at"]


def test_ttl_defaults_to_mechanism_300_when_unconfigured():
    assert _pending_ttl(make()) == pytest.approx(300.0)  # 未配 → 机制默认 DEFAULT_TTL_SECONDS


def test_ttl_package_default_applies():
    assert _pending_ttl(make({**POLICY, "default_ttl_seconds": 900})) == pytest.approx(900.0)


def test_ttl_per_action_overrides_package_default():
    policy = {
        **POLICY,
        "default_ttl_seconds": 900,
        "approvals": [{"action": "终稿发送管理层", "approver": "专家", "ttl_seconds": 1800}],
    }
    assert _pending_ttl(make(policy)) == pytest.approx(1800.0)  # 每动作覆盖包默认


def test_ttl_illegal_default_falls_back_to_300_not_no_expiry():
    """fail-closed：非法包默认一律回落机制默认 300s（绝不 fail-open 成无过期/inf），且留审计警告。"""
    for bad in (-5, 0, "banana", float("inf"), float("nan"), True, 10**400):
        p = make({**POLICY, "default_ttl_seconds": bad})
        assert _pending_ttl(p) == pytest.approx(300.0), f"非法 default_ttl_seconds={bad!r} 未回落 300s"
        assert any(a["decision"] == "warn" and a["step"] == "default_ttl_seconds" for a in p.audit)


def test_ttl_illegal_per_action_falls_back_to_package_default_and_keeps_gate():
    """非法每动作 TTL：只警告 + 回落包默认；action/approver 合法 → 审批门**不** broken（与项形状错区分）。"""
    policy = {
        **POLICY,
        "default_ttl_seconds": 600,
        "approvals": [{"action": "终稿发送管理层", "approver": "专家", "ttl_seconds": "oops"}],
    }
    p = make(policy)
    assert _pending_ttl(p) == pytest.approx(600.0)  # 非法每动作 TTL → 回落包默认，不回落到 300
    assert any(a["decision"] == "warn" and "ttl_seconds" in str(a["step"]) for a in p.audit)
    assert p.snapshot()["approvals"] == {"终稿发送管理层": "专家"}  # 门仍在，未 fail-closed 一律拒
    # 端到端仍可批可放行（非法 TTL 没关门）
    [ch] = p.pending_challenges()
    assert p.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)[0]
    assert p.require_approval("终稿发送管理层", episode_id="EP-1", payload={"x": 1})[0]


def test_approval_display_redacts_pii_but_digest_binds_original():
    """W6-4：审批卡 payload_display 脱敏（PII 抹），但 payload_digest 仍绑**原始** params——写执行器写原文、
    防偷梁换柱不变。审批人看脱敏视图、批的是这个动作，不批 PII。"""
    from osca_host.challenge import payload_digest as _digest

    p = make()  # POLICY: data.redact=[身份证号,手机号] + approvals 终稿发送管理层
    action = "终稿发送管理层"
    original = {"结论": "同意", "经办手机": "13812345678"}
    p.require_approval(action, episode_id="EP-1", payload=original)
    [dto] = p.pending_challenges()
    # display 脱敏：手机号被抹，非 PII 原样可读
    assert "13812345678" not in str(dto["payload_display"])
    assert "已脱敏" in str(dto["payload_display"])
    assert dto["payload_display"]["结论"] == "同意"
    # digest 仍绑**原始**（未脱敏）params——防偷梁换柱、写执行器写原文
    assert dto["payload_digest"] == _digest(original)
    assert dto["payload_digest"] != _digest({"结论": "同意", "经办手机": "***手机号已脱敏***"})
    # 原始 params 仍能一次性放行（脱敏没动被写内容、digest 未变）
    p.decide_challenge(dto["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert p.require_approval(action, episode_id="EP-1", payload=original)[0]


def test_approval_display_redacts_pii_in_dict_keys():
    """W6-4 对抗审查捉：PII 藏进 dict **键**（如 {经办手机138…: 值}）也须脱进 display，不漏进审批卡；
    digest 仍绑**原始**（未脱）键——写原文、防偷梁换柱不变。"""
    from osca_host.challenge import payload_digest as _digest

    p = make()
    action = "终稿发送管理层"
    original = {"经办手机13812345678": "同意", "嵌套": {"办事人身份证11010119900307461X": "张三"}}
    p.require_approval(action, episode_id="EP-1", payload=original)
    [dto] = p.pending_challenges()
    assert "13812345678" not in str(dto["payload_display"])  # 顶层键 PII 脱
    assert "11010119900307461X" not in str(dto["payload_display"])  # 嵌套键 PII 脱
    assert dto["payload_digest"] == _digest(original)  # digest 仍绑原始键（未脱）


def test_redact_key_collision_keeps_all_fields():
    """GPT 外审：两个不同 PII 键脱成同一标记 → 不静默塌成一个（读回执/审批展示丢字段）→ 后缀消歧、保全全字段。"""
    p = make()  # POLICY: data.redact=[身份证号,手机号]
    out, _ = p.redact({"手机13800138000": "A", "手机13900139000": "B"})
    assert len(out) == 2 and set(out.values()) == {"A", "B"}  # 两字段都在，未塌成一个


def test_duplicate_action_does_not_inherit_prev_ttl():
    """GPT 外审：重复 action 覆盖 approver 时**清旧 TTL 覆盖**——后一条非法/缺省 TTL 回落包默认，不继承前一条。"""
    policy = {
        **POLICY,
        "default_ttl_seconds": 600,
        "approvals": [
            {"action": "终稿发送管理层", "approver": "甲", "ttl_seconds": 1800},
            {"action": "终稿发送管理层", "approver": "乙", "ttl_seconds": "非法"},
        ],
    }
    p = make(policy)
    assert p.approvals["终稿发送管理层"] == "乙"  # 后一条 approver 生效
    assert _pending_ttl(p) == pytest.approx(600.0)  # TTL 回落包默认 600，不继承前一条 1800


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


def test_status_honest_when_approvals_broken():
    """P2：配置损坏时审批门一律拒绝，status 明示 config_error/deny_all（控制面不撒谎）。"""
    p = make(policy={**POLICY, "approvals": ["oops"]})
    assert not p.require_approval("终稿发送管理层", episode_id="EP-1", payload={})[0]  # 一律拒绝
    assert p.snapshot()["approvals"] == "config_error/deny_all"


def test_audit_trail_records_decisions():
    p = make()
    p.authorize_tool("成文", "CON-001.拉取费用明细")
    denies = [a for a in p.audit if a["decision"] == "deny"]
    assert denies and denies[-1]["step"] == "成文"


# ── kill switch 第二可求值形式：回放红灯率 > X%（数据源 = 回放器健康档案，M3-W4） ──

REPLAY_POLICY = {**POLICY, "kill_switch": [{"when": "回放红灯率 > 20%"}]}


def test_replay_red_rate_trips_kill_switch():
    p = make(
        policy=REPLAY_POLICY, stats={**HEALTHY, "replay_green": 1, "replay_red": 1, "replay_at": "2026-07-11T10:00:00"}
    )
    assert p.kill_tripped
    assert "回放红灯率 1/2" in p.kill_reason and "2026-07-11" in p.kill_reason


def test_replay_red_rate_below_threshold_stays_quiet():
    p = make(policy=REPLAY_POLICY, stats={**HEALTHY, "replay_green": 9, "replay_red": 1})
    assert not p.kill_tripped


def test_replay_threshold_no_float_rounding():
    """1/3 = 33.333…% > 33.33%：四位小数派生值会翻转严格 > 判定——整数计数交叉相乘不受舍入影响。"""
    p = make(
        policy={**POLICY, "kill_switch": [{"when": "回放红灯率 > 33.33%"}]},
        stats={**HEALTHY, "replay_green": 2, "replay_red": 1},
    )
    assert p.kill_tripped


def test_replay_health_missing_is_availability_gap_not_config_error():
    """档案缺失 → 条件不生效留痕（保守默认）——与形状非法的停机是两回事。"""
    p = make(policy=REPLAY_POLICY, stats=HEALTHY)  # 无 replay_red_rate
    assert not p.kill_tripped
    assert any("回放健康档案缺失" in a["reason"] for a in p.audit if a["decision"] == "warn")


def test_replay_health_reader_full_contract(tmp_path):
    """完整契约：字段/计数/逐项灯色对账/派生率/版本归属——任一不过按档案不可用（fail-closed）。"""
    import json
    import subprocess

    from osca_cli.ledger import ledger_stamp

    from osca_host.policy import replay_health

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "测试")
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "1")
    stamp = ledger_stamp(tmp_path)

    assert replay_health(tmp_path)["replay_green"] is None  # 档案不存在

    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    ok_doc = {
        "generated_by": "oscapipe checkup",
        "at": "2026-07-12T10:00:00",
        "model": "mock",
        "ledger_tree": stamp,
        "total": 3,
        "green": 2,
        "red": 1,
        "error": 0,
        "red_rate": 0.3333,
        "judgments": {
            "J-1": {"light": "green", "assertions": 1},
            "J-2": {"light": "green", "assertions": 2},
            "J-3": {"light": "red", "assertions": 1},
        },
    }
    health.write_text(json.dumps(ok_doc), encoding="utf-8")
    stats = replay_health(tmp_path)
    assert stats["replay_green"] == 2 and stats["replay_red"] == 1 and "2026-07-12" in stats["replay_at"]

    bad_docs = [
        {"red_rate": 0},  # 九轮病灶：最小合法 JSON 曾被采信
        {**ok_doc, "ledger_tree": ""},
        {k: v for k, v in ok_doc.items() if k != "ledger_tree"},
        {**ok_doc, "total": 9},  # 计数不自洽
        {**ok_doc, "red_rate": 0.25},  # 派生率与计数矛盾
        {**ok_doc, "red_rate": float("nan")},  # 非有限数值
        {k: v for k, v in ok_doc.items() if k != "red_rate"},  # red_rate 必填
        {k: v for k, v in ok_doc.items() if k != "judgments"},  # judgments 必填
        {**ok_doc, "green": True, "total": 2},  # bool 不是计数
        {**ok_doc, "judgments": {"J-1": {}}},  # 数量与 total 不符
        {**ok_doc, "judgments": {**ok_doc["judgments"], "J-3": {"light": "green", "assertions": 1}}},  # 灯色不对账
        {**ok_doc, "judgments": {**ok_doc["judgments"], "J-3": {"light": "紫灯", "assertions": 1}}},  # 未知枚举
        {**ok_doc, "judgments": {**ok_doc["judgments"], "J-3": {"light": "red", "assertions": -1}}},  # 负断言数
        {**ok_doc, "judgments": {**ok_doc["judgments"], "J-3": "不是 mapping"}},
        {
            **ok_doc,
            "green": 0,
            "red": 0,
            "error": 3,
            "red_rate": 0.0,
            "judgments": {k: {"light": "error", "assertions": 0} for k in ("J-1", "J-2", "J-3")},
        },  # 0/0 = unavailable
        {**ok_doc, "ledger_tree": "f" * 40},  # 版本不符
    ]
    for doc in bad_docs:
        health.write_text(json.dumps(doc), encoding="utf-8")
        assert replay_health(tmp_path)["replay_green"] is None, doc
    for raw in ("不是 JSON{{{", '["形状不对"]'):
        health.write_text(raw, encoding="utf-8")
        assert replay_health(tmp_path)["replay_green"] is None, raw

    # 干净区：未提交的判断修改让戳无从证明内容——档案不可用
    health.write_text(json.dumps(ok_doc), encoding="utf-8")
    (tmp_path / "a.txt").write_text("改动未提交", encoding="utf-8")
    assert replay_health(tmp_path)["replay_green"] is None
    git("checkout", "--", "a.txt")
    assert replay_health(tmp_path)["replay_green"] == 2  # 恢复干净即采信


def test_replay_health_unverifiable_is_unavailable(tmp_path):
    """非 git / git 失败 = 无法验证版本归属——不可用，不是「非 Git 照常接受」（fail-closed）。"""
    import json

    from osca_host.policy import replay_health

    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    doc = {
        "generated_by": "oscapipe checkup",
        "at": "2026-07-12T10:00:00",
        "model": "mock",
        "ledger_tree": "a" * 40,
        "total": 1,
        "green": 1,
        "red": 0,
        "error": 0,
        "red_rate": 0.0,
        "judgments": {"J-1": {"light": "green", "assertions": 1}},
    }
    health.write_text(json.dumps(doc), encoding="utf-8")
    assert replay_health(tmp_path)["replay_green"] is None  # tmp_path 非 git 根


def test_replay_health_rejected_when_ledger_advances(tmp_path):
    """git 根：ledger_head ≠ 当前 HEAD → 账本已前进，旧档案不作安全信号。"""
    import json
    import subprocess

    from osca_host.policy import replay_health

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "测试")
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "1")
    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    from osca_cli.ledger import ledger_stamp

    doc = {
        "generated_by": "oscapipe checkup",
        "at": "2026-07-12T10:00:00",
        "model": "mock",
        "ledger_tree": ledger_stamp(tmp_path),
        "total": 1,
        "green": 1,
        "red": 0,
        "error": 0,
        "red_rate": 0.0,
        "judgments": {"J-1": {"light": "green", "assertions": 1}},
    }
    health.write_text(json.dumps(doc), encoding="utf-8")
    assert replay_health(tmp_path)["replay_green"] == 1  # 内容戳匹配 → 采信

    (tmp_path / "a.txt").write_text("2", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "2")
    assert replay_health(tmp_path)["replay_green"] is None  # 账本前进 → 不采信


def test_policy_budget_cross_or_unknown_keys_revoke_quota():
    """跨层/未知预算键（max_steps/banana 落在 per_episode）——运行时自防同样撤额，不依赖 lint。"""
    p = make(policy={**POLICY, "budgets": {"per_episode": {"max_steps": 0, "banana": 0}}})
    assert p.max_tool_calls == 0 and p.max_tokens == 0
    assert not p.precheck_tokens("EP-1")[0]
    assert any("不执行的键" in a["reason"] for a in p.audit)


def test_redact_enum_synced_with_lint():
    """脱敏类别双份常量的一致性锚（cli 枚举 vs host 正则表）——漂移即红灯，后续上移单一真理源。"""
    from osca_cli.rules import REDACT_CATEGORIES

    from osca_host.policy import REDACTORS

    assert set(REDACT_CATEGORIES) == set(REDACTORS)


def test_sample_pack_replay_condition_now_evaluable(sample_pack):
    """样例包 policy 的「回放红灯率 > 20%」从 W4 起可求值：健康档案在场即真裁决。"""
    import yaml as _yaml

    policy_doc = _yaml.safe_load((sample_pack / "policy.yaml").read_text(encoding="utf-8"))
    p = PolicyInterceptor("demo", policy_doc, {"confirmed": 9, "overruled": 0, "replay_green": 1, "replay_red": 1})
    assert p.kill_tripped and "回放红灯率" in p.kill_reason


def test_unavailable_replay_data_does_not_clear_existing_trip():
    """三态语义（十轮）：可用性缺口不清除已触发的红灯；有可判数据证明恢复才解除。"""
    p = make(policy=REPLAY_POLICY, stats={**HEALTHY, "replay_green": 1, "replay_red": 1})
    assert p.kill_tripped  # 1/2 > 20% → 触发
    p.refresh_kill_switch(dict(HEALTHY))  # 档案不可用（HEAD 前进/损坏/网关故障）
    assert p.kill_tripped and "回放红灯率" in p.kill_reason  # 缺口不洗红灯
    p.refresh_kill_switch({**HEALTHY, "replay_green": 9, "replay_red": 0})  # 新可判档案证明健康
    assert not p.kill_tripped


def test_replay_threshold_exact_equality_not_tripped():
    """69/375 = 18.4% 精确相等——严格 > 不触发（二进制浮点 18.4×375 == 6899.999… 会误触发）。"""
    p = make(
        policy={**POLICY, "kill_switch": [{"when": "回放红灯率 > 18.4%"}]},
        stats={**HEALTHY, "replay_green": 306, "replay_red": 69},
    )
    assert not p.kill_tripped


def test_budgets_outer_typo_revokes_quota():
    """budgets 外层拼写错误（per_epiosde）= 声明了没人执行的预算段——额度撤销，不是无限额。"""
    p = make(policy={**POLICY, "budgets": {"per_epiosde": {"max_tokens": 1}}})
    assert p.max_tool_calls == 0 and p.max_tokens == 0
    assert not p.authorize_llm("EP-1")[0]
    assert any("预算配置非法" in a["reason"] for a in p.audit)


def test_llm_permit_leaves_audit_trace():
    """permit 成功也留痕：LLM 随后失败时，审计里看得到这次授权尝试。"""
    p = make()
    ok, _ = p.authorize_llm("EP-1")
    assert ok
    assert any("LLM 调用授权" in a["reason"] and a["decision"] == "allow" for a in p.audit)


def test_ratio_zero_denominator_three_state():
    """比值条件的零分母（十一轮）：0/0 不可判不解除既有红灯；有推翻零确认保守停机。"""
    p = make(stats={"confirmed": 1, "overruled": 1})  # 1/1 > 0.3 → 触发
    assert p.kill_tripped
    p.refresh_kill_switch({"confirmed": 0, "overruled": 0})  # 刷成 0/0——可用性缺口不洗红灯
    assert p.kill_tripped
    p2 = make(stats={"confirmed": 0, "overruled": 1})  # 有推翻却零确认 → fail-closed
    assert p2.kill_tripped and "分母缺失" in p2.kill_reason
    p3 = make(stats={"confirmed": 0, "overruled": 0})  # 初始 0/0 → 未触发 + 警告留痕
    assert not p3.kill_tripped
    assert any("0/0" in a["reason"] for a in p3.audit)


def test_threshold_no_decimal_context_rounding():
    """29 个 9 的阈值：28 位 Decimal 上下文乘法会舍成 1 而漏触发——纯整数交叉相乘不舍入。"""
    nines = "0." + "9" * 29
    p = make(
        policy={**POLICY, "kill_switch": [{"when": f"overruled/confirmed > {nines}"}]},
        stats={"confirmed": 1, "overruled": 1},
    )
    assert p.kill_tripped  # 1/1 = 1 > 0.99…9


def test_tool_budget_reservation_is_atomic():
    """预算预留在授权锁内：并发工具授权不超发（十一轮：锁外 read-modify-write 会超发）。"""
    from concurrent.futures import ThreadPoolExecutor

    p = make(policy={**POLICY, "budgets": {"per_episode": {"max_tool_calls": 5}}})
    with ThreadPoolExecutor(max_workers=8) as pool:
        grants = list(
            pool.map(lambda _: p.authorize_tool("取数", "CON-001.拉取费用明细", episode_id="EP-1")[0], range(32))
        )
    assert sum(grants) == 5


def test_replay_health_huge_int_rate_degrades_not_crashes(tmp_path):
    """数百位 JSON 大整数 red_rate：读取器退化不崩（math.isfinite/float() 会 OverflowError）。"""
    import json

    from osca_host.policy import replay_health

    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    doc = {
        "generated_by": "oscapipe checkup",
        "at": "t",
        "model": "m",
        "ledger_tree": "a" * 40,
        "total": 1,
        "green": 1,
        "red": 0,
        "error": 0,
        "red_rate": 10**400,
        "judgments": {"J-1": {"light": "green", "assertions": 1}},
    }
    health.write_text(json.dumps(doc), encoding="utf-8")
    assert replay_health(tmp_path)["replay_green"] is None  # 不炸、不采信


def test_replay_health_broken_git_index_is_unavailable(tmp_path):
    """git index 损坏 → dirty=None（不可判定）——显式拒绝，不当「干净」采信（十二轮）。"""
    import json
    import subprocess

    from osca_cli.ledger import ledger_stamp

    from osca_host.policy import replay_health

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "测试")
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "1")
    doc = {
        "generated_by": "oscapipe checkup",
        "at": "2026-07-12T10:00:00",
        "model": "mock",
        "ledger_tree": ledger_stamp(tmp_path),
        "total": 1,
        "green": 1,
        "red": 0,
        "error": 0,
        "red_rate": 0.0,
        "judgments": {"J-1": {"light": "green", "assertions": 1}},
    }
    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    health.write_text(json.dumps(doc), encoding="utf-8")
    assert replay_health(tmp_path)["replay_green"] == 1  # 完好时采信

    (tmp_path / ".git" / "index").write_bytes(b"garbage-not-an-index")
    assert replay_health(tmp_path)["replay_green"] is None  # 不可判定 ≠ 干净


def test_replay_health_stamp_dirty_stamp_race_rejected(tmp_path, monkeypatch):
    """stamp→dirty→stamp 三明治：dirty 检查期间 HEAD 原子前进到干净 tree B——A 的旧档案不得蒙混。"""
    import json

    import osca_host.policy as policy_mod

    doc = {
        "generated_by": "oscapipe checkup",
        "at": "2026-07-12T10:00:00",
        "model": "mock",
        "ledger_tree": "a" * 40,
        "total": 1,
        "green": 1,
        "red": 0,
        "error": 0,
        "red_rate": 0.0,
        "judgments": {"J-1": {"light": "green", "assertions": 1}},
    }
    health = tmp_path / "indexes" / "replay-health.json"
    health.parent.mkdir()
    health.write_text(json.dumps(doc), encoding="utf-8")

    stamps = iter(["a" * 40, "b" * 40])  # 第一次读到 A，dirty 后已是 B
    monkeypatch.setattr(policy_mod, "ledger_stamp", lambda root: next(stamps))
    monkeypatch.setattr(policy_mod, "ledger_dirty", lambda root: [])
    assert policy_mod.replay_health(tmp_path)["replay_green"] is None  # 两次戳不一致 → 拒信

    monkeypatch.setattr(policy_mod, "ledger_stamp", lambda root: "a" * 40)
    assert policy_mod.replay_health(tmp_path)["replay_green"] == 1  # 稳定一致才采信


def test_charge_tokens_illegal_report_fails_closed_not_zero():
    """GPT Review 复审 P1 预算绕过：负数/零/bool/非整数用量上报在强制点**直接拒绝**——按 0 计账仍是
    绕过（max_tokens=1 时可零成本无限过顶）；强制点看不见 prompt/产出、无法估算，唯 fail-closed。
    合法路径恒为正整数（osca_cli.llm 源头清洗 + runner 估算兜底）。"""
    p = make({**POLICY, "budgets": {"per_episode": {"max_tokens": 100}}})
    assert p.charge_tokens("EP-1", 80)[0]
    for bad in (-1000, 0, True, 3.5, "42"):
        ok, reason = p.charge_tokens("EP-1", bad)
        assert not ok and "用量上报非法" in reason, bad  # fail-closed 拒绝，剧集就地停
    assert p.episode_budget_used("EP-1") == (0, 80)  # 已用量既未冲减也未白嫖
    ok, reason = p.charge_tokens("EP-1", 30)  # 真用量照记，超顶即拒（止损顶语义不变）
    assert not ok and "预算硬顶" in reason


def test_charge_tokens_repeated_illegal_reports_never_pass_cap():
    """GPT 最小复现反转：max_tokens=1 时连续 5 次 authorize_llm → charge(-1)，charge 必须 5 次全拒——
    不存在「全部允许、累计恒 0」的零成本循环。"""
    p = make({**POLICY, "budgets": {"per_episode": {"max_tokens": 1}}})
    for _ in range(5):
        assert p.authorize_llm("EP-1")[0]  # 额度未被合法消耗，授权仍过——止损靠 charge 侧拒绝
        ok, reason = p.charge_tokens("EP-1", -1)
        assert not ok and "用量上报非法" in reason  # 每次非法上报当场拒绝（剧集停），循环在第一次即断


def test_redact_recurses_into_tuple():
    """P2：tuple 内的手机号/身份证不许漏进审批展示与快照——redact 必须递归 tuple。"""
    policy = PolicyInterceptor("p", {"data": {"redact": ["手机号"]}}, {})
    value = {"联系": ({"手机": "13812345678"}, ["13898765432"], "备注 13811112222")}
    redacted, hits = policy.redact(value)
    assert hits == 3
    assert isinstance(redacted["联系"], tuple)  # 形状保留
    flat = str(redacted)
    assert "13812345678" not in flat and "13898765432" not in flat and "13811112222" not in flat


def test_revoke_bounded_when_final_commit_hangs():
    """四轮复核 P1：终局提交悬挂于存储时 revoke 必须**有界**返回（短事务预约:I/O 在锁外,
    revoke 只有界等待在途计数）——不许把 revoke/关停一起卡死。"""
    import time as time_mod

    policy = PolicyInterceptor("p", {}, {})
    policy.final_commit_grace = 0.2
    ok, _ = policy.begin_final_commit()  # 模拟:预约后发布 I/O 永久卡死(不 end)
    assert ok
    started = time_mod.monotonic()
    policy.revoke("关停（测试:提交悬挂）")
    elapsed = time_mod.monotonic() - started
    assert policy.revoked and elapsed < 2.0  # grace 0.2s + 调度余量,绝非无界
    assert any("悬挂" in a["reason"] for a in policy.audit)  # 悬挂明标,不静默
    ok, deny = policy.begin_final_commit()  # revoke 返回后零新提交开始
    assert not ok and "包已停" in deny
    policy.end_final_commit()  # 悬挂线程迟到归还:不炸、计数不为负


def test_revoke_waits_briefly_for_inflight_commit_to_finish():
    """在途提交在 grace 内收尾:revoke 等到归零才返回——「先于 revoke 返回完成」的正常路径。"""
    import threading
    import time as time_mod

    policy = PolicyInterceptor("p", {}, {})
    policy.final_commit_grace = 5.0
    ok, _ = policy.begin_final_commit()
    assert ok
    threading.Timer(0.1, policy.end_final_commit).start()
    started = time_mod.monotonic()
    policy.revoke("关停（测试:提交及时收尾）")
    elapsed = time_mod.monotonic() - started
    assert 0.05 < elapsed < 3.0  # 等到了 end(≈0.1s),而非立即返回或吊满 grace
    assert not any("悬挂" in a["reason"] for a in policy.audit)
