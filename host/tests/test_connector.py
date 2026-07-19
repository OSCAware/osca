"""Connector 代理：manifest 契约、binding 解析、mock 执行、egress、脱敏回执。"""

from __future__ import annotations

import pytest
import yaml

from osca_host.connector import ConnectorProxy
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats


@pytest.fixture
def mock_dir(tmp_path):
    d = tmp_path / "fixtures"
    d.mkdir()
    (d / "拉取费用明细.yaml").write_text(
        yaml.safe_dump(
            {"已关账": True, "rows": [{"科目": "差旅费", "金额": 45, "经办电话": "13812345678"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def proxy(sample_pack, mock_dir):
    _, loaded = load_for_host(sample_pack)
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))
    bindings = {"FINANCE_DB": {"endpoint": f"mock://{mock_dir}", "secret_ref": "FINANCE_DB_RO_KEY"}}
    return ConnectorProxy(loaded, bindings, policy)


def test_call_ok_with_receipt_and_redaction(proxy):
    receipt = proxy.call("CON-001.拉取费用明细", "2026-07", step="取数")
    assert receipt.ok
    assert receipt.binding_ref == "FINANCE_DB"
    assert receipt.payload["已关账"] is True
    assert "13812345678" not in str(receipt.payload)  # policy.data.redact 注入前脱敏
    assert receipt.redacted == 1


def test_interface_drift_explodes(proxy):
    receipt = proxy.call("CON-001.不存在的接口", step=None)
    assert not receipt.ok and "接口漂移" in receipt.error


def test_step_whitelist_enforced_via_proxy(proxy):
    receipt = proxy.call("CON-001.拉取费用明细", step="成文")
    assert not receipt.ok and "越权" in receipt.error


def test_missing_binding(sample_pack, mock_dir):
    _, loaded = load_for_host(sample_pack)
    policy = PolicyInterceptor(loaded.package_id, {}, {"confirmed": 0, "overruled": 0})
    proxy = ConnectorProxy(loaded, {}, policy)  # 部署环境没注入 FINANCE_DB
    receipt = proxy.call("CON-001.拉取费用明细", step=None)
    assert not receipt.ok and "binding" in receipt.error


def test_egress_default_deny_for_real_endpoint(sample_pack):
    _, loaded = load_for_host(sample_pack)
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))
    bindings = {"FINANCE_DB": {"endpoint": "mysql://db.internal.example:3306/finance"}}
    proxy = ConnectorProxy(loaded, bindings, policy)
    receipt = proxy.call("CON-001.拉取费用明细", step=None)
    assert not receipt.ok and "egress 默认全禁" in receipt.error  # allow_domains 为空


def test_write_path_gated_by_approval_even_for_internal_calls(proxy):
    """写接口审批门（绑定挑战）：step=None 内部调用不豁免；不在清单默认拒绝；批准后一次性消费。"""
    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"

    receipt = proxy.call(ref, step=None)
    assert not receipt.ok and "默认拒绝" in receipt.error  # 不在 approvals 清单——内部调用也没有旁路

    proxy.policy.approvals[ref] = "专家"
    receipt = proxy.call(ref, "2026-07", step="取数", episode_id="EP-1")
    assert not receipt.ok and "审批门拦截" in receipt.error  # 在清单但未批 → 挂 pending 挑战

    # 审批人批准该挑战（挑战绑到本次 episode + params 摘要）
    [ch] = proxy.policy.pending_challenges()
    proxy.policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert proxy.call(ref, "2026-07", step="取数", episode_id="EP-1").ok  # 同绑定放行一次（mock 执行）
    receipt = proxy.call(ref, "2026-07", step="取数", episode_id="EP-1")
    assert not receipt.ok and "审批门拦截" in receipt.error  # 一次性：consume 后再调即拦


def test_mock_fixture_missing(proxy, sample_pack):
    receipt = proxy.call("CON-001.拉取检修计划期", step=None)  # 固件目录里没有这个接口的文件
    assert not receipt.ok and "mock 固件缺失" in receipt.error


def test_write_params_bind_real_content_and_mock_write_lands(proxy):
    """D1 params 穿透：写摘要绑**真实被写内容**（非空串摘要），批准后 mock 写执行器落地并回显被写内容；
    换 params 即换绑定——偷梁换柱防线用真实内容成立（旧批准消费不到新内容）。"""
    from osca_host.challenge import payload_digest

    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"
    proxy.policy.approvals[ref] = "专家"
    params = {"品类": "浆果", "折扣": 4.5, "起始": "16:30"}

    r = proxy.call(ref, params, step="取数", episode_id="EP-1")  # 挂 pending：摘要绑真实 params
    assert not r.ok and "审批门拦截" in r.error
    [ch] = proxy.policy.pending_challenges()
    assert ch["payload_digest"] == payload_digest(params)
    assert ch["payload_digest"] != payload_digest("")  # 不再是「恒空串摘要」的旧病灶

    proxy.policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)

    # 偷梁换柱（在消费 A 之前独立验）：换内容 B 消费不到 A 的批准 → 另挂一张 pending
    rb = proxy.call(ref, {"品类": "浆果", "折扣": 3.0}, step="取数", episode_id="EP-1")
    assert not rb.ok and "审批门拦截" in rb.error

    # 用回原内容 A → A 的批准一次性消费 → mock 写落地并回显被批准内容
    landed = proxy.call(ref, params, step="取数", episode_id="EP-1")
    assert landed.ok and landed.payload["landed"] is True and landed.payload["mock_write"] == ref
    assert landed.payload["applied"] == params  # 回执回显被批准的被写内容


def test_write_empty_params_rejected_no_empty_digest(proxy):
    """写步未提供被写内容（params 空）→ fail-closed，不生成空串摘要绑定（不对空摘要拍板）。"""
    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"
    proxy.policy.approvals[ref] = "专家"
    r = proxy.call(ref, "", step="取数", episode_id="EP-1")  # 空 params
    assert not r.ok and "被写内容" in r.error
    assert proxy.policy.pending_challenges() == []  # 没挂任何挑战——不生成空串摘要


def test_write_non_json_serializable_params_fail_closed(proxy):
    """写 params 非 JSON 可序列化（如 YAML 原生 date）→ fail-closed 回执，不抛未捕获异常（宁可拒绝不可炸）。"""
    import datetime

    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"
    proxy.policy.approvals[ref] = "专家"
    r = proxy.call(ref, {"关账日": datetime.date(2026, 7, 8)}, step="取数", episode_id="EP-1")
    assert not r.ok and "JSON 可序列化" in r.error
    assert proxy.policy.pending_challenges() == []  # 坏输入不生成绑定


def test_read_path_ignores_params_no_regression(proxy):
    """读接口（permissions.write=forbidden）不过写审批门、执行器忽略 params——传 params 也不改读回执。"""
    a = proxy.call("CON-001.拉取费用明细", "2026-07", step="取数")
    b = proxy.call("CON-001.拉取费用明细", {"任意": "结构体"}, step="取数")
    assert a.ok and b.ok and a.payload == b.payload  # 读回执与 params 无关
