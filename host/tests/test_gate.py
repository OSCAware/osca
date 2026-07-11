"""闸门组合语义（SPEC v0.4 草案 §3）：any / all / sequence / debounce / enabled。"""

from __future__ import annotations

from osca_host.gate import Gate
from osca_host.loader import AwareDecl, TriggerDecl


def make_gate(gate_spec: dict, n_triggers: int = 2, enabled: bool = True) -> Gate:
    triggers = [TriggerDecl(f"AW-001/T{i + 1}", "event", {"source": "测试"}) for i in range(n_triggers)]
    aware = AwareDecl(
        aware_id="AW-001", name="测试", enabled=enabled, triggers=triggers, gate=gate_spec, then="STR-001"
    )
    return Gate("demo", aware)


def test_any_wakes_on_first_hit():
    gate = make_gate({"combine": "any"})
    woke, verdict = gate.on_trigger("AW-001/T1")
    assert woke and "唤醒" in verdict
    assert gate.wakes == 1


def test_all_requires_every_trigger():
    gate = make_gate({"combine": "all"})
    woke, verdict = gate.on_trigger("AW-001/T1")
    assert not woke and "all 已见 1/2" in verdict
    woke, _ = gate.on_trigger("AW-001/T2")
    assert woke
    # 唤醒后重置：单发不再过
    woke, _ = gate.on_trigger("AW-001/T1")
    assert not woke


def test_sequence_in_order():
    gate = make_gate({"combine": "sequence"})
    assert not gate.on_trigger("AW-001/T1")[0]
    assert gate.on_trigger("AW-001/T2")[0]


def test_sequence_out_of_order_resets():
    gate = make_gate({"combine": "sequence"})
    woke, verdict = gate.on_trigger("AW-001/T2")  # 乱序
    assert not woke and "乱序" in verdict
    assert not gate.on_trigger("AW-001/T1")[0]  # 重新开始
    assert gate.on_trigger("AW-001/T2")[0]


def test_sequence_restart_on_first():
    gate = make_gate({"combine": "sequence"})
    gate.on_trigger("AW-001/T1")
    woke, _ = gate.on_trigger("AW-001/T1")  # 乱序但是首位 → 视为新序列开始
    assert not woke
    assert gate.on_trigger("AW-001/T2")[0]


def test_debounce_suppresses_second_wake():
    gate = make_gate({"combine": "any", "debounce": "1h"})
    assert gate.on_trigger("AW-001/T1")[0]
    woke, verdict = gate.on_trigger("AW-001/T2")
    assert not woke and "debounce 抑制" in verdict
    assert (gate.wakes, gate.debounced) == (1, 1)


def test_disabled_gate_suppresses():
    gate = make_gate({"combine": "any"}, enabled=False)
    woke, verdict = gate.on_trigger("AW-001/T1")
    assert not woke and "触发器停" in verdict
    assert gate.wakes == 0


def test_precondition_noted_in_verdict():
    gate = make_gate({"combine": "any", "precondition": "取数非空"})
    _, verdict = gate.on_trigger("AW-001/T1")
    assert "precondition 未求值" in verdict


def test_precondition_evaluator_blocks_wake():
    gate = make_gate({"combine": "any", "precondition": "CON-001.取数(当月) 返回非空", "on_fail": "顺延24小时重试"})
    gate.precondition_eval = lambda text: (False, "CON-001.取数(当月) 返回为空")
    woke, verdict = gate.on_trigger("AW-001/T1")
    assert not woke and "precondition 拦截" in verdict and "顺延24小时重试" in verdict
    assert gate.wakes == 0 and gate.precondition_blocked == 1
    assert gate.last_wake is None  # 拦截不消耗 debounce 窗口


def test_precondition_evaluator_pass_and_unevaluable():
    gate = make_gate({"combine": "any", "precondition": "CON-001.取数(当月) 返回非空"})
    gate.precondition_eval = lambda text: (True, "求值通过（返回非空）")
    woke, verdict = gate.on_trigger("AW-001/T1")
    assert woke and "求值通过" in verdict

    gate2 = make_gate({"combine": "any", "precondition": "看情况"})
    gate2.precondition_eval = lambda text: (None, "不可求值，默认放行")
    woke, verdict = gate2.on_trigger("AW-001/T1")
    assert woke and "不可求值" in verdict
