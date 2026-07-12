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
    assert ROLE_CAPS["approver"] == {"approve"}
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
