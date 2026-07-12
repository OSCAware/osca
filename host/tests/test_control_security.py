"""M4-W0 安全内核（Review M4 首轮）：传输层权限、实例锁、授权矩阵、协议加固。

对应首轮探针：umask(0) 下 socket 0777、双实例互删 socket、非对象 JSON /
超长行空响应、全连接者共享管理员能力、load 透传文件系统面。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket as socket_mod
import stat

import pytest
import yaml

from osca_host.authz import Principal
from osca_host.control import admin_token_path, principals_path, send_command
from osca_host.host import Host


@pytest.fixture
async def running_host(sock_path):
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if host.control.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    yield host
    host._stop.set()
    await asyncio.wait_for(task, timeout=5)


async def _send(request, host, token=None):
    return await asyncio.to_thread(send_command, request, host.control.socket_path, 30.0, token)


def _raw(sock_path, payload: bytes) -> bytes:
    """绕过客户端封装直发字节——协议加固的探针入口。"""
    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(str(sock_path))
        s.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return buf


# ── 传输层：私有目录 / socket 权限 / 对端凭据 ──────────────────────────


async def test_socket_and_dir_private_under_umask0(sock_path):
    """探针曾实测 umask(0) 下 socket 0777——目录 0700 + socket/token 0600 须与 umask 无关。"""
    old = os.umask(0)
    try:
        host = Host(sock_path)
        task = asyncio.create_task(host.run())
        for _ in range(100):
            if sock_path.exists():
                break
            await asyncio.sleep(0.01)
        assert stat.S_IMODE(os.lstat(sock_path.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(sock_path).st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(admin_token_path(sock_path)).st_mode) == 0o600
        host._stop.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        os.umask(old)


async def test_peer_uid_checked_fail_closed(running_host, monkeypatch):
    """对端 uid ≠ 本进程 uid，或凭据取不到 → 一律拒绝（fail-closed）。"""
    import osca_host.control as control_mod

    monkeypatch.setattr(control_mod, "_peer_uid", lambda sock: os.getuid() + 1)
    response = await _send({"cmd": "status"}, running_host)
    assert not response["ok"] and response["error"] == "unauthorized"

    monkeypatch.setattr(control_mod, "_peer_uid", lambda sock: None)
    response = await _send({"cmd": "status"}, running_host)
    assert not response["ok"] and response["error"] == "unauthorized"


# ── 实例锁与 inode 所有权 ──────────────────────────────────────────────


async def test_second_instance_refused_first_socket_intact(running_host):
    """探针曾实测第二实例接管活 socket——实例 flock 后第二实例干净退出，第一实例通道原样。"""
    host = running_host
    second = Host(host.control.socket_path)
    assert await asyncio.wait_for(second.run(), timeout=5) == 1  # 实例锁拒绝
    response = await _send({"cmd": "status"}, host)
    assert response["ok"]  # 第一实例的入口没有被删除或重绑


async def test_close_spares_replaced_socket_path(sock_path):
    """关闭只删本实例创建的 inode——路径被换过就不动（不误删后来者的入口）。"""
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    sock_path.unlink()
    sock_path.write_text("后来者的入口占位", encoding="utf-8")
    host._stop.set()
    assert await asyncio.wait_for(task, timeout=5) == 0
    assert sock_path.read_text(encoding="utf-8") == "后来者的入口占位"


async def test_start_refuses_non_socket_at_path(sock_path):
    """路径被非 socket 占用 → 拒绝清理、拒绝启动（不无条件 unlink）。"""
    sock_path.write_text("不是 socket", encoding="utf-8")
    host = Host(sock_path)
    assert await asyncio.wait_for(host.run(), timeout=5) == 1
    assert sock_path.read_text(encoding="utf-8") == "不是 socket"


# ── 身份与逐命令授权 ──────────────────────────────────────────────────


async def test_role_capability_matrix_enforced(running_host):
    """全连接者共享管理员能力已终结：token → Principal，角色出界即 forbidden。"""
    host = running_host
    operator, approver, expert = "operator-token-01", "approver-token-01", "expert-token-0001"
    host.authorizer.register(operator, Principal("运营台", "operator"))
    host.authorizer.register(approver, Principal("审批卡", "approver"))
    host.authorizer.register(expert, Principal("专家端", "expert"))

    # 无 token / 未知 token → unauthorized
    assert (await _send({"cmd": "status", "token": ""}, host))["error"] == "unauthorized"
    assert (await _send({"cmd": "status"}, host, token="not-a-registered-token"))["error"] == "unauthorized"

    # operator：快照（脱敏 DTO 属 W2）/ 启停 / 发射 / 剧集摘要
    assert (await _send({"cmd": "status"}, host, token=operator))["ok"]
    assert (await _send({"cmd": "episodes"}, host, token=operator))["ok"]
    # operator 明确禁止：load 面 / 审批 / 完整剧集 / 生命周期
    for request in (
        {"cmd": "load", "deployment_id": "x"},
        {"cmd": "approve", "package_id": "p", "action": "a"},
        {"cmd": "episode", "episode_id": "EP-0001"},
        {"cmd": "unload", "package_id": "p"},
        {"cmd": "stop"},
    ):
        response = await _send(request, host, token=operator)
        assert response["error"] == "forbidden", request

    # 审批 RPC 在 W3 challenge 前对全角色关闭：admin 不可伪造业务审批，approver 也是空集
    assert (await _send({"cmd": "approve", "package_id": "p", "action": "a"}, host))["error"] == "forbidden"
    approve_req = {"cmd": "approve", "package_id": "p", "action": "a"}
    assert (await _send(approve_req, host, token=approver))["error"] == "forbidden"
    assert (await _send({"cmd": "status"}, host, token=approver))["error"] == "forbidden"
    assert (await _send({"cmd": "status"}, host, token=expert))["error"] == "forbidden"


async def test_principals_file_issues_roles(sock_path):
    """部署者签发面：principals 文件（0600）启动时注册；权限过宽拒绝启动。"""
    pfile = principals_path(sock_path)
    pfile.write_text(
        yaml.safe_dump([{"name": "运营台", "role": "operator", "token": "operator-token-01"}], allow_unicode=True),
        encoding="utf-8",
    )
    os.chmod(pfile, 0o600)
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    assert (await _send({"cmd": "status"}, host, token="operator-token-01"))["ok"]
    host._stop.set()
    await asyncio.wait_for(task, timeout=5)

    os.chmod(pfile, 0o644)  # 签发面配置错误必须响，不许静默降级
    assert await asyncio.wait_for(Host(sock_path).run(), timeout=5) == 1


async def test_load_only_accepts_server_side_deployment_id(running_host, sample_pack):
    """load 的文件系统代理面已关：path 字段死于 schema，未配置 ID 人话拒绝。"""
    response = await _send({"cmd": "load", "path": str(sample_pack)}, running_host)
    assert response["error"] == "bad_request"
    response = await _send({"cmd": "load", "deployment_id": "nope"}, running_host)
    assert not response["ok"] and "未配置的部署 ID" in response["detail"]


async def test_uid_bound_principal_blocks_stolen_tokens(running_host, monkeypatch):
    """W0.1 P1-1 生产信任模型：token 与对端 uid 双绑定——偷来的 token 换了进程身份即失效。"""
    import osca_host.control as control_mod

    host = running_host
    bot_uid = os.getuid() + 1000
    host.authorizer.register("bot-operator-token", Principal("飞书Bot", "operator", bot_uid))

    # Host 同 uid 的进程拿 bot 的 token：token 绑定 bot_uid → 拒
    response = await _send({"cmd": "status"}, host, token="bot-operator-token")
    assert response["error"] == "unauthorized" and "不符" in response["detail"]

    # bot uid 的进程（传输允许名单放行）持自己的 token → 正常
    monkeypatch.setattr(control_mod, "_peer_uid", lambda sock: bot_uid)
    assert (await _send({"cmd": "status"}, host, token="bot-operator-token"))["ok"]

    # 被攻陷的 bot 偷 admin token（绑定 Host uid）→ 拒——stolen_admin_reached_handler=False
    admin_token = admin_token_path(host.control.socket_path).read_text(encoding="utf-8").strip()
    response = await _send({"cmd": "status"}, host, token=admin_token)
    assert response["error"] == "unauthorized"
    # 不在允许名单的第三方 uid：传输层直接拒
    monkeypatch.setattr(control_mod, "_peer_uid", lambda sock: bot_uid + 1)
    assert (await _send({"cmd": "status"}, host, token=admin_token))["error"] == "unauthorized"


async def test_run_dir_symlink_escape_refused():
    """W0.1 P1-2：运行目录被预置成外部目录链接 → 拒绝启动——外部目录权限不被改、零写入。"""
    import shutil
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp(prefix="oscah-", dir="/tmp"))
    try:
        outside = base / "outside"
        outside.mkdir()
        os.chmod(outside, 0o755)
        (base / "run").symlink_to(outside)
        host = Host(base / "run" / "h.sock")
        assert await asyncio.wait_for(host.run(), timeout=5) == 1  # O_NOFOLLOW 拒链接目录
        assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o755  # 外部目录权限没被改成 0700
        assert list(outside.iterdir()) == []  # socket/token/lock 零落入
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def test_start_failure_leaves_no_unlocked_listener(sock_path, monkeypatch):
    """W0.1 P1-3：bind 之后权限步失败 → 关监听器、删自己 socket、放锁——不留无锁监听器。"""
    real_chmod = os.chmod

    def flaky_chmod(path, mode, **kwargs):
        if str(path) == str(sock_path):
            raise OSError("chmod 失败（测试注入）")
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", flaky_chmod)
    assert await asyncio.wait_for(Host(sock_path).run(), timeout=5) == 1
    assert not sock_path.exists()  # 自己的 socket 已删——没有还在 accept 的孤儿监听器
    monkeypatch.undo()

    host = Host(sock_path)  # 实例锁已释放：新实例能干净起停
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    assert (await _send({"cmd": "status"}, host))["ok"]
    host._stop.set()
    assert await asyncio.wait_for(task, timeout=5) == 0


async def test_lax_admin_token_perms_refuse_start(sock_path):
    """凭据读取协议：已存在的 admin token 权限过宽（0644）→ 拒绝启动。"""
    token_file = admin_token_path(sock_path)
    token_file.write_text("a" * 64 + "\n", encoding="utf-8")
    os.chmod(token_file, 0o644)
    assert await asyncio.wait_for(Host(sock_path).run(), timeout=5) == 1


# ── 协议加固：schema / 超长 / 超时 / 并发上限 ──────────────────────────


async def test_malformed_requests_get_unified_error_lines(running_host):
    """探针曾实测 [] / null / "x" / 1 → AttributeError 空响应——现在一律一行 bad_request。"""
    path = running_host.control.socket_path
    for payload in (b"[]\n", b"null\n", b'"x"\n', b"1\n", b"{oops\n"):
        response = json.loads(await asyncio.to_thread(_raw, path, payload))
        assert response["ok"] is False and response["error"] == "bad_request", payload

    for request in (
        {"v": 2, "cmd": "status"},  # 版本不符
        {"v": 1, "cmd": "sudo"},  # 未知命令
        {"v": 1, "cmd": "fire", "package_id": "p"},  # 缺字段
    ):
        payload = (json.dumps(request) + "\n").encode()
        response = json.loads(await asyncio.to_thread(_raw, path, payload))
        assert response["ok"] is False and response["error"] == "bad_request", request


async def test_oversize_request_rejected_with_response(running_host):
    """探针曾实测 70 KiB 请求 → ValueError 空响应——现在回一行「请求超长」。"""
    big = b'{"v": 1, "cmd": "status", "pad": "' + b"x" * (70 * 1024) + b'"}\n'
    response = json.loads(await asyncio.to_thread(_raw, running_host.control.socket_path, big))
    assert response["ok"] is False and "超长" in response["detail"]


async def test_idle_connection_times_out(running_host):
    """连接不许无限等一行：超时回错误行并关闭。"""
    running_host.control.read_timeout = 0.2

    def probe() -> bytes:
        with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(running_host.control.socket_path))
            return s.recv(65536)  # 一字节不发——等服务端超时关闭

    buf = await asyncio.to_thread(probe)
    assert b"bad_request" in buf and "超时" in buf.decode()


async def test_slow_load_does_not_block_status(running_host, sample_pack, monkeypatch):
    """W0.1 P2：慢 load 期间 status 仍即时响应——重活在锁外线程，_cmd_lock 只罩发布段。"""
    import time

    import osca_host.host as host_mod

    host = running_host
    real = host_mod.load_for_host

    def slow(*args, **kwargs):
        time.sleep(1.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", slow)
    host.deployments["slow"] = {"path": str(sample_pack)}
    load_task = asyncio.create_task(_send({"cmd": "load", "deployment_id": "slow"}, host))
    await asyncio.sleep(0.3)  # load 已进线程
    started = time.monotonic()
    response = await _send({"cmd": "status"}, host)
    assert response["ok"] and time.monotonic() - started < 1.0  # status 没排在慢 load 后面
    assert (await asyncio.wait_for(load_task, timeout=10))["ok"]


async def test_connection_cap_returns_busy(running_host):
    host = running_host
    host.control.max_connections = 1
    hold = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    hold.connect(str(host.control.socket_path))
    try:
        await asyncio.sleep(0.1)  # 让占位连接先被 accept
        response = await _send({"cmd": "status"}, host)
        assert response["error"] == "busy"
    finally:
        hold.close()
