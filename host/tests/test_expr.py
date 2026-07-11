"""受限求值语义（SPEC v0.4 草案 §4）：precondition 形式与 emit_when 子句。"""

from __future__ import annotations

from osca_host.expr import evaluate_emit_when, parse_precondition


def test_parse_precondition():
    assert parse_precondition("CON-001.拉取费用明细(当月) 返回非空") == ("CON-001", "拉取费用明细", "当月")
    assert parse_precondition("CON-001.取数() 返回非空") == ("CON-001", "取数", "")


def test_precondition_free_text_unevaluable():
    assert parse_precondition("数据看起来差不多就行") is None
    assert parse_precondition("CON-001.取数(当月) 返回一大堆") is None


def test_emit_when_transition():
    expr = "old.已关账 == false && new.已关账 == true"
    assert evaluate_emit_when(expr, {"已关账": False}, {"已关账": True}) is True
    assert evaluate_emit_when(expr, {"已关账": True}, {"已关账": True}) is False
    assert evaluate_emit_when(expr, {"已关账": False}, {"已关账": False}) is False


def test_emit_when_operators_and_literals():
    assert evaluate_emit_when("new.状态 != null", {}, {"状态": "x"}) is True  # 子句只引用 new，old 空不碍事
    assert evaluate_emit_when("new.计数 == 3", {"计数": 1}, {"计数": 3}) is True
    assert evaluate_emit_when("new.名称 == 张三", {"名称": ""}, {"名称": "张三"}) is True


def test_emit_when_unevaluable_returns_none():
    assert evaluate_emit_when("new.金额 > 100", {}, {"金额": 200}) is None  # > 不在受限形式
    assert evaluate_emit_when("看情况", {}, {}) is None
    assert evaluate_emit_when("old.缺失 == 1 && new.缺失 == 2", {"别的": 1}, {"别的": 2}) is None  # 字段缺失
