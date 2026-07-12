"""身份与授权单元：命令 schema、角色能力矩阵、token 签发面（M4-W0 安全内核）。"""

from __future__ import annotations

import os
import stat

import pytest
import yaml

from osca_host.authz import (
    ROLE_CAPS,
    Authorizer,
    Principal,
    ensure_admin_token,
    load_principals,
    validate_request,
)


def test_validate_request_top_level_must_be_mapping():
    """非对象 JSON（[] / null / 字符串 / 数字）曾以 AttributeError 穿透留空响应。"""
    for bad in ([], None, "x", 1):
        assert validate_request(bad) == "请求必须是 JSON 对象"


def test_validate_request_version_cmd_and_fields():
    assert "协议版本" in validate_request({"cmd": "status"})
    assert "未知命令" in validate_request({"v": 1, "cmd": "sudo"})
    # path 类字段透传死于 schema——load 只收 deployment_id（confused-deputy 面，M4 首轮 P1）
    assert "不接受字段" in validate_request({"v": 1, "cmd": "load", "deployment_id": "d", "path": "/etc"})
    assert "缺少字段" in validate_request({"v": 1, "cmd": "fire", "package_id": "p"})
    assert "缺少字段" in validate_request({"v": 1, "cmd": "fire", "package_id": "p", "trigger_id": ""})
    assert validate_request({"v": 1, "cmd": "status", "token": "t"}) is None


def test_role_caps_matrix_pinned():
    """权限矩阵是拍板级决策——任何变更必须显式改这条测试。"""
    assert "approve" not in ROLE_CAPS["host_admin"]  # admin 不可伪造业务审批
    assert ROLE_CAPS["operator"] == {"status", "enable", "disable", "fire", "episodes"}
    assert "episode" not in ROLE_CAPS["operator"]  # 剧集摘要可看，全量导出不给
    # W3 审批 challenge（pending→approved|denied→consumed，绑定 approver/episode/
    # digest/expiry/nonce）落地前，旧 set[action] 授予不从控制通道暴露——approver 空集
    assert ROLE_CAPS["approver"] == frozenset()
    assert ROLE_CAPS["expert"] == frozenset()  # M4-W1 专家端命令落地时显式归入


def test_authorizer_register_identify_authorize():
    az = Authorizer()
    az.register("a" * 16, Principal("管理员", "host_admin"))
    assert az.identify("a" * 16).role == "host_admin"
    assert az.identify("b" * 16) is None
    assert az.identify(None) is None and az.identify("") is None and az.identify(123) is None
    assert az.authorize(Principal("x", "operator"), "fire")
    assert not az.authorize(Principal("x", "operator"), "stop")
    assert not az.authorize(Principal("x", "没有的角色"), "status")  # 未知角色零能力

    with pytest.raises(ValueError):
        az.register("short", Principal("短", "operator"))  # token 过短
    with pytest.raises(ValueError):
        az.register("c" * 16, Principal("坏", "root"))  # 未知角色
    with pytest.raises(ValueError):
        az.register("a" * 16, Principal("重", "operator"))  # token 重复（一 token 一 principal）
    with pytest.raises(ValueError):
        az.register(12345678901234567890, Principal("非字符串", "operator"))  # 不做 str() 静默转换
    with pytest.raises(ValueError):
        az.register("d" * 16, Principal("控制\x00字符", "operator"))  # name 拒控制字符


def test_authorizer_uid_binding_registers_peer_allowlist():
    """生产模式：principal 绑 uid → 进传输层允许名单；uid 验型拒 bool/负数/非整数。"""
    az = Authorizer()
    az.register("e" * 16, Principal("飞书Bot", "operator", 30001))
    assert az.peer_uids == {30001}
    assert az.identify("e" * 16).uid == 30001
    for bad_uid in (True, -1, "30001"):
        with pytest.raises(ValueError):
            az.register("f" * 16, Principal("坏uid", "operator", bad_uid))


def test_ensure_admin_token_creates_0600_and_reuses(tmp_path):
    path = tmp_path / "h.sock.token"
    token = ensure_admin_token(path)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert len(token) >= 32
    assert ensure_admin_token(path) == token  # 重启复用，不轮换


def test_ensure_admin_token_refuses_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("x" * 64, encoding="utf-8")
    link = tmp_path / "h.sock.token"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        ensure_admin_token(link)


def test_ensure_admin_token_refuses_lax_permissions(tmp_path):
    """已存在的 token 文件权限过宽（如 0644）→ 拒绝复用（凭据读取协议对 fd fstat 验证）。"""
    path = tmp_path / "h.sock.token"
    path.write_text("a" * 64 + "\n", encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(OSError):
        ensure_admin_token(path)
    os.chmod(path, 0o600)
    assert ensure_admin_token(path) == "a" * 64  # 收紧后照常复用


def test_load_principals_validates(tmp_path):
    az = Authorizer()
    path = tmp_path / "p.yaml"
    assert load_principals(path, az) == 0  # 缺文件 = 只有 admin（单用户形态合法）

    path.write_text(
        yaml.safe_dump([{"name": "运营台", "role": "operator", "token": "operator-token-01"}], allow_unicode=True),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    assert load_principals(path, az) == 1
    assert az.identify("operator-token-01").name == "运营台"

    os.chmod(path, 0o640)
    with pytest.raises(OSError):
        load_principals(path, Authorizer())  # 权限过宽（内含 token）

    os.chmod(path, 0o600)
    path.write_text("name: 不是列表\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_principals(path, Authorizer())

    path.write_text(yaml.safe_dump([{"name": "缺角色", "token": "t" * 16}]), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ValueError):
        load_principals(path, Authorizer())


def test_load_principals_uid_binding_and_strict_types(tmp_path):
    """uid 条目进允许名单；token 非字符串不做 str() 静默转换，一律拒绝。"""
    path = tmp_path / "p.yaml"
    path.write_text(
        yaml.safe_dump(
            [{"name": "飞书Bot", "role": "operator", "token": "bot-operator-token", "uid": 30001}],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    az = Authorizer()
    assert load_principals(path, az) == 1
    assert az.identify("bot-operator-token").uid == 30001
    assert az.peer_uids == {30001}

    for bad in (
        [{"name": "整数token", "role": "operator", "token": 12345678901234567890}],
        [{"name": "坏uid", "role": "operator", "token": "x" * 16, "uid": "30001"}],
        [{"name": "多余键", "role": "operator", "token": "x" * 16, "extra": 1}],
    ):
        path.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
        os.chmod(path, 0o600)
        with pytest.raises(ValueError):
            load_principals(path, Authorizer())
