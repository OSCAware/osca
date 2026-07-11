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
    """写接口审批门：step=None 的内部调用不豁免；不在清单默认拒绝；token 一次性消费。"""
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"

    receipt = proxy.call("CON-001.拉取费用明细", step=None)
    assert not receipt.ok and "默认拒绝" in receipt.error  # 不在 approvals 清单——内部调用也没有旁路

    proxy.policy.approvals["CON-001.拉取费用明细"] = "专家"
    receipt = proxy.call("CON-001.拉取费用明细", step="取数")
    assert not receipt.ok and "审批门拦截" in receipt.error  # 在清单但未授予

    proxy.policy.grant_approval("CON-001.拉取费用明细")
    assert proxy.call("CON-001.拉取费用明细", step="取数").ok  # 授予后放行一次（mock 执行）
    receipt = proxy.call("CON-001.拉取费用明细", step="取数")
    assert not receipt.ok and "审批门拦截" in receipt.error  # token 已消费


def test_mock_fixture_missing(proxy, sample_pack):
    receipt = proxy.call("CON-001.拉取检修计划期", step=None)  # 固件目录里没有这个接口的文件
    assert not receipt.ok and "mock 固件缺失" in receipt.error
