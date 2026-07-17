"""身份与授权单元：命令 schema、角色能力矩阵、token 签发面（M4-W0 安全内核）。"""

from __future__ import annotations

import hashlib
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
    read_private_file,
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
    # W3 审批 challenge（pending→approved|denied→consumed，绑定 approver/episode/digest/
    # expiry/nonce）：approver 批/驳（绑 challenge_id）+ 看待批清单——绑定挑战替换旧无绑定 set[action]
    assert ROLE_CAPS["approver"] == {"approve", "deny", "challenges"}
    assert "approve" not in ROLE_CAPS["host_admin"]  # admin 管生命周期但不可伪造业务审批（deny/challenges 同理）
    for cap in ("deny", "challenges"):
        assert cap not in ROLE_CAPS["host_admin"] and cap not in ROLE_CAPS["operator"]
    # approver 只有审批面，无生命周期/快照/启停
    for denied in ("status", "load", "unload", "enable", "disable", "fire", "episodes", "episode", "stop"):
        assert denied not in ROLE_CAPS["approver"]
    # M4-W1 专家端：只读交付面——episodes 摘要 + episode 全量（draft 即交付物）；写命令一律不给
    assert ROLE_CAPS["expert"] == {"episodes", "episode"}


def test_expert_role_readonly_delivery_surface():
    """expert 只读：能取剧集（交付面），任何状态变更/审批命令一律被拒。"""
    az = Authorizer()
    expert = Principal("专家桥", "expert")
    assert az.authorize(expert, "episodes")
    assert az.authorize(expert, "episode")
    for denied in ("status", "load", "unload", "enable", "disable", "fire", "approve", "deny", "challenges", "stop"):
        assert not az.authorize(expert, denied)


def test_approver_role_challenge_surface_only():
    """approver：只有审批 challenge 面（approve/deny/challenges），生命周期/快照/启停一律被拒。"""
    az = Authorizer()
    approver = Principal("审批人", "approver")
    for allowed in ("approve", "deny", "challenges"):
        assert az.authorize(approver, allowed)
    for denied in ("status", "load", "unload", "enable", "disable", "fire", "episodes", "episode", "stop"):
        assert not az.authorize(approver, denied)


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


def test_private_file_read_is_bounded_even_if_fstat_size_is_stale(tmp_path, monkeypatch):
    """文件可在 fstat 后增长；读取协议本身必须以 MAX+1 为硬边界。"""
    import osca_host.authz as authz_mod

    path = tmp_path / "growing.token"
    path.write_bytes(b"x" * (authz_mod.MAX_CRED_FILE + 1))
    os.chmod(path, 0o600)
    real_fstat = os.fstat

    def stale_size(fd):
        st = real_fstat(fd)
        values = list(st)
        values[6] = 0  # st_size：模拟检查后增长
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", stale_size)
    with pytest.raises(OSError, match="超长"):
        read_private_file(path)


def test_principals_yaml_error_is_normalized_without_source_excerpt(tmp_path):
    """解析器错误不能把可能含 token 的 YAML 行原文带进启动日志。"""
    secret = "super-secret-token-value"
    path = tmp_path / "p.yaml"
    path.write_text(f"- name: x\n  role: operator\n  token: {secret}\n  broken: [\n", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ValueError) as exc:
        load_principals(path, Authorizer())
    assert secret not in str(exc.value)
    assert "YAML" in str(exc.value)


def test_production_principal_accepts_digest_without_plaintext_token(tmp_path):
    """生产签发文件只保存 token 摘要；客户端明文不落 Host 配置。"""
    token = "client-owned-operator-token"
    path = tmp_path / "p.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "运营台",
                    "role": "operator",
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "uid": os.getuid(),
                }
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    az = Authorizer()
    assert load_principals(path, az, production=True) == 1
    assert az.identify(token) == Principal("运营台", "operator", os.getuid())
    assert token not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["{}\n", "false\n", "0\n", "null\n", '""\n'])
def test_existing_principals_file_rejects_falsy_non_list_shapes(tmp_path, text):
    """存在的签发文件必须真是列表；falsy 值不能伪装成“没有 principal”。"""
    path = tmp_path / "p.yaml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="列表"):
        load_principals(path, Authorizer())


def test_production_principal_requires_non_null_uid(tmp_path):
    """生产 principal 的 UID 是认证绑定，不允许用 null 悄悄退化成 Host UID。"""
    path = tmp_path / "p.yaml"
    path.write_text(
        yaml.safe_dump(
            [{"name": "运营台", "role": "operator", "uid": None, "token_sha256": "a" * 64}],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="uid"):
        load_principals(path, Authorizer(), production=True)
