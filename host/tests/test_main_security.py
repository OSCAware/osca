from __future__ import annotations

import pytest

from osca_host import main as m
from osca_host.authz import PROTOCOL_VERSION, validate_request
from osca_host.main import _client, _load_deployments


@pytest.mark.parametrize(
    ("argv", "expect"),
    [
        (["challenges", "pkg"], {"cmd": "challenges", "package_id": "pkg"}),
        (["approve", "pkg", "CH-1"], {"cmd": "approve", "package_id": "pkg", "challenge_id": "CH-1"}),
        (["deny", "pkg", "CH-1"], {"cmd": "deny", "package_id": "pkg", "challenge_id": "CH-1"}),
    ],
)
def test_cli_builds_schema_valid_w3_approval_requests(argv, expect, monkeypatch):
    """CLI 客户端为 W3 审批命令构造的请求必须与 COMMAND_FIELDS 契约对齐（防 approve/deny/challenges 契约漂移回归）。"""
    captured: dict = {}

    def fake_send(request, socket_path, token=None):
        captured["req"] = request
        return {"ok": True, "detail": "ok", "challenges": []}

    monkeypatch.setattr(m, "send_command", fake_send)
    assert m.main(argv) == 0
    assert captured["req"] == expect
    # 关键回归：请求过 schema（不再是旧的 {cmd:approve, action:...}，否则 validate_request 拒）
    assert validate_request({"v": PROTOCOL_VERSION, **captured["req"]}) is None


@pytest.mark.parametrize(
    "text",
    [
        "demo:\n  path: null\n",
        "demo:\n  path: /tmp/demo\n  bindings: null\n",
        "demo:\n  path: /tmp/demo\n  dest: null\n",
        'demo:\n  path: "bad\\npath"\n',
    ],
)
def test_deployments_reject_null_and_control_character_paths(tmp_path, text):
    path = tmp_path / "deployments.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        _load_deployments(str(path))


@pytest.mark.parametrize("text", ["[]\n", "false\n", "0\n", "null\n", '""\n'])
def test_deployments_reject_falsy_non_mapping_top_level(tmp_path, text):
    """已有清单必须真是 mapping；falsy 值不能伪装成合法空清单。"""
    path = tmp_path / "deployments.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        _load_deployments(str(path))


def test_deployments_egress_extra_valid_parsed(tmp_path):
    """M7-W4：egress_extra 合法（非空字符串列表）→ 解析并入 clean（host 不按 base 解析路径）。"""
    path = tmp_path / "deployments.yaml"
    path.write_text(
        'demo:\n  path: /var/lib/packs/demo.osca\n  egress_extra: ["127.0.0.1", "gw.internal"]\n',
        encoding="utf-8",
    )
    d = _load_deployments(str(path))
    assert d["demo"]["egress_extra"] == ["127.0.0.1", "gw.internal"]
    assert d["demo"]["path"].endswith("/var/lib/packs/demo.osca")


@pytest.mark.parametrize(
    "bad",
    [
        'd:\n  path: /p\n  egress_extra: "127.0.0.1"\n',  # 非 list
        "d:\n  path: /p\n  egress_extra: [1, 2]\n",  # 含非字符串
        'd:\n  path: /p\n  egress_extra: [""]\n',  # 空串
        "d:\n  path: /p\n  egress_extra: []\n  other: 1\n",  # 未知键（回归：白名单仍拒 egress_extra 以外的其它键）
    ],
)
def test_deployments_reject_malformed_egress_extra_or_unknown_key(tmp_path, bad):
    path = tmp_path / "deployments.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        _load_deployments(str(path))


def test_client_rejects_lax_principal_token_file_before_connecting(tmp_path, monkeypatch):
    token_file = tmp_path / "operator.token"
    token_file.write_text("operator-owned-token", encoding="utf-8")
    token_file.chmod(0o644)
    called = False

    def forbidden_connect(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应进入连接")

    monkeypatch.setattr("osca_host.main.send_command", forbidden_connect)
    assert _client({"cmd": "status"}, tmp_path / "missing.sock", token_file) == 1
    assert not called
