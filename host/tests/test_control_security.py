"""M4-W0 安全内核（Review M4 首轮）：传输层权限、实例锁、授权矩阵、协议加固。

对应首轮探针：umask(0) 下 socket 0777、双实例互删 socket、非对象 JSON /
超长行空响应、全连接者共享管理员能力、load 透传文件系统面。
"""

from __future__ import annotations

import asyncio
import grp
import hashlib
import json
import os
import shutil
import socket as socket_mod
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
import yaml

from osca_host.authz import Principal
from osca_host.control import RuntimeDirectory, admin_token_path, principals_path, send_command
from osca_host.host import Host, HostState


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


async def test_start_refuses_insecure_existing_lock_file(sock_path):
    """lock 也是安全文件：必须相对 runtime fd 打开并校验属主、类型和 0600 权限。"""
    lock = sock_path.with_name(sock_path.name + ".lock")
    lock.write_text("preexisting", encoding="utf-8")
    os.chmod(lock, 0o644)
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if task.done() or sock_path.exists():
            break
        await asyncio.sleep(0.01)
    if not task.done():
        host._stop.set()
    assert await asyncio.wait_for(task, timeout=5) == 1
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o644


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


async def test_corrupt_principals_yaml_is_safe_host_error(sock_path, caplog):
    """损坏 YAML 不穿透 Host.run，日志不含 token 行原文。"""
    secret = "must-not-appear-in-host-log"
    pfile = principals_path(sock_path)
    pfile.write_text(f"- name: x\n  role: operator\n  token: {secret}\n  broken: [\n", encoding="utf-8")
    os.chmod(pfile, 0o600)
    assert await asyncio.wait_for(Host(sock_path).run(), timeout=5) == 1
    assert "principals YAML 无法解析" in caplog.text
    assert secret not in caplog.text


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

    base = Path(tempfile.mkdtemp(prefix="oscah-", dir="/tmp")).resolve()
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


async def test_ancestor_symlink_is_refused_without_external_writes():
    """祖先任一级是链接都必须拒绝；不能由 mkdir(parents=True) 跟到外部。"""
    base = Path(tempfile.mkdtemp(prefix="oscah-", dir="/tmp")).resolve()
    try:
        outside = base / "outside"
        outside.mkdir()
        (base / "link").symlink_to(outside, target_is_directory=True)
        host = Host(base / "link" / "run" / "h.sock")
        task = asyncio.create_task(host.run())
        for _ in range(100):
            if task.done() or (outside / "run" / "h.sock").exists():
                break
            await asyncio.sleep(0.01)
        if not task.done():  # 旧实现会成功启动到外部；让复现测试可以收尾
            host._stop.set()
        assert await asyncio.wait_for(task, timeout=5) == 1
        assert list(outside.iterdir()) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def test_runtime_dir_rename_swap_stays_on_anchored_inode(monkeypatch):
    """验证后换名并在原路径放外链：凭据/锁/socket 仍只落原 inode。"""
    import osca_host.host as host_mod

    base = Path(tempfile.mkdtemp(prefix="oscah-", dir="/tmp")).resolve()
    run_dir, held, outside = base / "run", base / "held", base / "outside"
    run_dir.mkdir()
    os.chmod(run_dir, 0o700)
    outside.mkdir()
    pfile = run_dir / "h.sock.principals.yaml"
    pfile.write_text(
        yaml.safe_dump([{"name": "运营台", "role": "operator", "token": "operator-token-01"}]),
        encoding="utf-8",
    )
    os.chmod(pfile, 0o600)
    real = host_mod.ensure_admin_token
    swapped = False

    def swap_then_create(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            run_dir.rename(held)
            run_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "ensure_admin_token", swap_then_create)
    host = Host(run_dir / "h.sock")
    task = asyncio.create_task(host.run())
    saw_socket = False
    try:
        for _ in range(200):
            if (held / "h.sock").exists():
                saw_socket = True
                break
            if task.done():
                break
            await asyncio.sleep(0.01)
        if saw_socket:
            host._stop.set()
        rc = await asyncio.wait_for(task, timeout=5)
        assert rc == 1  # 普通路径 bind 无 dir_fd：父 inode 不再匹配时安全拒绝
        assert not saw_socket
        assert host.authorizer.identify("operator-token-01") is not None
        assert {p.name for p in held.iterdir()} >= {
            "h.sock.token",
            "h.sock.principals.yaml",
            "h.sock.lock",
        }
        assert list(outside.iterdir()) == []
        assert run_dir.is_symlink()  # shutdown 不删除攻击者替换的目录项
    finally:
        host._stop.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5)
        shutil.rmtree(base, ignore_errors=True)


async def test_shutdown_after_parent_swap_spares_attacker_socket():
    """启动后父目录被换：shutdown 只清原 fd 内的 listener，不碰外链目标的 socket。"""
    base = Path(tempfile.mkdtemp(prefix="oscah-", dir="/tmp")).resolve()
    run_dir, held, outside = base / "run", base / "held", base / "outside"
    run_dir.mkdir(mode=0o700)
    os.chmod(run_dir, 0o700)
    outside.mkdir()
    host = Host(run_dir / "h.sock")
    task = asyncio.create_task(host.run())
    attacker = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    try:
        for _ in range(100):
            if (run_dir / "h.sock").exists():
                break
            await asyncio.sleep(0.01)
        run_dir.rename(held)
        run_dir.symlink_to(outside, target_is_directory=True)
        attacker.bind(str(outside / "h.sock"))
        attacker_st = os.lstat(outside / "h.sock")
        host._stop.set()
        assert await asyncio.wait_for(task, timeout=5) == 0
        after = os.lstat(outside / "h.sock")
        assert (after.st_dev, after.st_ino) == (attacker_st.st_dev, attacker_st.st_ino)
        assert run_dir.is_symlink()
        assert not (held / "h.sock").exists()
    finally:
        attacker.close()
        host._stop.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5)
        shutil.rmtree(base, ignore_errors=True)


async def test_development_and_production_modes_have_real_kernel_permissions(sock_path):
    """dev=0700/0600；prod=0710/0660 且 group 由部署者显式指定。"""
    # 开发模式沿用 fixture 的 0700 目录。
    dev = Host(sock_path)
    dev_task = asyncio.create_task(dev.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    assert stat.S_IMODE(os.stat(sock_path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600
    dev._stop.set()
    await asyncio.wait_for(dev_task, timeout=5)

    os.chmod(sock_path.parent, 0o710)
    os.chown(sock_path.parent, -1, os.getgid())
    group = grp.getgrgid(os.getgid()).gr_name
    prod = Host(sock_path, control_group=group)
    prod_task = asyncio.create_task(prod.run())
    for _ in range(100):
        if sock_path.exists() or prod_task.done():
            break
        await asyncio.sleep(0.01)
    assert not prod_task.done()
    assert os.stat(sock_path.parent).st_gid == os.getgid()
    assert stat.S_IMODE(os.stat(sock_path.parent).st_mode) == 0o710
    assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o660
    prod._stop.set()
    await asyncio.wait_for(prod_task, timeout=5)


async def test_production_mode_rejects_bad_group_or_misprovisioned_directory(sock_path):
    """生产权限配置错误 fail closed，且不得顺手 chmod 掩盖部署错误。"""
    assert await Host(sock_path, control_group="group-that-must-not-exist-oscaware").run() == 1
    group = grp.getgrgid(os.getgid()).gr_name
    os.chmod(sock_path.parent, 0o700)
    assert await Host(sock_path, control_group=group).run() == 1
    assert stat.S_IMODE(os.stat(sock_path.parent).st_mode) == 0o700


async def test_production_mode_rejects_group_writable_runtime_ancestor():
    """control group 若能改名 runtime 的祖先目录，precheck→bind 窗口仍可把 socket 引到外部。"""
    base = Path(tempfile.mkdtemp(prefix="oscah-parent-", dir="/tmp")).resolve()
    os.chmod(base, 0o711)  # 先保证外层可遍历，本测试只命中下面的可改名祖先
    unsafe_parent = base / "group-writable"
    run_dir = unsafe_parent / "run"
    unsafe_parent.mkdir(mode=0o770)
    os.chown(unsafe_parent, -1, os.getgid())
    os.chmod(unsafe_parent, 0o770)
    run_dir.mkdir(mode=0o710)
    os.chown(run_dir, -1, os.getgid())
    os.chmod(run_dir, 0o710)
    group = grp.getgrgid(os.getgid()).gr_name
    host = Host(run_dir / "h.sock", control_group=group)
    task = asyncio.create_task(host.run())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.3)
        if not done:
            host._stop.set()
            await asyncio.wait_for(task, timeout=5)
            pytest.fail("生产模式接受了 control group 可改名的 runtime 祖先")
        assert task.result() == 1
        assert not (run_dir / "h.sock").exists()
    finally:
        host._stop.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5)
        shutil.rmtree(base, ignore_errors=True)


def test_production_mode_rejects_untraversable_runtime_ancestor():
    """最终 0710 不够：目标 group 无法穿过任一级 0700 祖先时必须拒绝启动。"""
    base = Path(tempfile.mkdtemp(prefix="oscah-traverse-", dir="/tmp")).resolve()  # 默认 0700
    run_dir = base / "run"
    run_dir.mkdir(mode=0o710)
    os.chown(run_dir, -1, os.getgid())
    os.chmod(run_dir, 0o710)
    runtime = None
    try:
        with pytest.raises(OSError, match="遍历"):
            runtime = RuntimeDirectory(run_dir, grp.getgrgid(os.getgid()).gr_name)
    finally:
        if runtime is not None:
            runtime.close()
        shutil.rmtree(base, ignore_errors=True)


async def test_start_failure_leaves_no_unlocked_listener(sock_path, monkeypatch):
    """W0.1 P1-3：bind 之后权限步失败 → 关监听器、删自己 socket、放锁——不留无锁监听器。"""
    real_chmod = os.chmod

    def flaky_chmod(path, mode, **kwargs):
        if Path(path).name == sock_path.name:
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


async def test_start_failure_spares_socket_replacement_inode(sock_path, monkeypatch):
    """bind 后立刻记 listener inode；chmod 前被换上的 socket 绝不能被异常清理删除。"""
    real_chmod = os.chmod
    attacker = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    attacker_inode = None

    def replace_then_fail(path, mode, **kwargs):
        nonlocal attacker_inode
        if Path(path).name == sock_path.name:
            sock_path.unlink()
            attacker.bind(str(sock_path))
            st = os.lstat(sock_path)
            attacker_inode = (st.st_dev, st.st_ino)
            raise OSError("chmod 失败（替换竞态注入）")
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", replace_then_fail)
    try:
        assert await asyncio.wait_for(Host(sock_path).run(), timeout=5) == 1
        st = os.lstat(sock_path)
        assert (st.st_dev, st.st_ino) == attacker_inode
    finally:
        attacker.close()


async def test_unexpected_start_exception_releases_runtime_fd(sock_path, monkeypatch):
    """非 OSError 的普通启动异常也必须规范化返回并释放持久 runtime fd。"""
    host = Host(sock_path)

    async def boom():
        raise RuntimeError("injected startup failure")

    monkeypatch.setattr(host.control, "start", boom)
    assert await host.run() == 1
    assert host.control._runtime is None
    assert host.control._lock_fd is None


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
    entered = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        time.sleep(1.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", slow)
    host.deployments["slow"] = {"path": str(sample_pack)}
    load_task = asyncio.create_task(_send({"cmd": "load", "deployment_id": "slow"}, host))
    assert await asyncio.to_thread(entered.wait, 5)  # 确认慢准备分支已经进入
    started = time.monotonic()
    response = await _send({"cmd": "status"}, host)
    assert response["ok"] and time.monotonic() - started < 1.0  # status 没排在慢 load 后面
    assert (await asyncio.wait_for(load_task, timeout=10))["ok"]


async def test_slow_load_cannot_publish_after_stop(sock_path, sample_pack, monkeypatch):
    """慢准备先开始，stop 先线性化：迟到 load 必须失败且退出后运行时图为空。"""
    import osca_host.host as host_mod

    entered, release = threading.Event(), threading.Event()
    real = host_mod.load_for_host

    def slow(*args, **kwargs):
        entered.set()
        release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", slow)
    host = Host(sock_path, {"slow": {"path": str(sample_pack)}})
    run_task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    load_task = asyncio.create_task(_send({"cmd": "load", "deployment_id": "slow"}, host))
    assert await asyncio.to_thread(entered.wait, 5)
    stop = await _send({"cmd": "stop"}, host)
    assert stop["ok"]
    release.set()
    load_response = await asyncio.wait_for(load_task, timeout=10)
    assert not load_response.get("ok")
    assert "关停" in load_response["detail"] and "取消" in load_response["detail"]
    assert await asyncio.wait_for(run_task, timeout=10) == 0
    assert host.registry.packages == {}
    assert host.registry.watchers == {}
    assert host.gates == {}
    assert host.policies == {}
    assert host.proxies == {}
    assert host.bindings == {}
    assert host.table.status() == []


async def test_stop_during_slow_initial_load_reaches_stopped_without_waiting_for_worker(
    sock_path, sample_pack, monkeypatch, caplog
):
    """初始 --load 也必须被 draining 立即取消，不能卡在进入主等待循环之前。"""
    import osca_host.host as host_mod

    entered, release = threading.Event(), threading.Event()
    real = host_mod.load_for_host

    def slow(*args, **kwargs):
        entered.set()
        release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", slow)
    host = Host(sock_path)
    run_task = asyncio.create_task(host.run([{"path": str(sample_pack)}]))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stop = await _send({"cmd": "stop"}, host)
        assert stop["ok"]
        done, _ = await asyncio.wait({run_task}, timeout=0.5)
        assert done == {run_task}
        assert run_task.result() == 1  # 初始装载被 stop 取消，启动批次未完整成功
        assert host.state is HostState.STOPPED
        assert any("Host 正在关停，load 已取消且不会发布" in record.message for record in caplog.records)
    finally:
        release.set()
        if not run_task.done():
            await asyncio.wait_for(run_task, timeout=10)


async def test_same_deployment_concurrent_loads_share_one_preparation(running_host, sample_pack, monkeypatch):
    """相同 deployment 的并发请求不得并发解压/索引，也不应互相覆盖。"""
    import osca_host.host as host_mod

    host = running_host
    host.deployments["same"] = {"path": str(sample_pack)}
    real = host_mod.load_for_host
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    calls = 0

    def slow(*args, **kwargs):
        nonlocal calls
        with guard:
            calls += 1
        entered.set()
        release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", slow)
    first = asyncio.create_task(_send({"cmd": "load", "deployment_id": "same"}, host))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(_send({"cmd": "load", "deployment_id": "same"}, host))
    await asyncio.sleep(0.2)
    release.set()
    one, two = await asyncio.gather(first, second)
    assert calls == 1
    assert one["ok"] and two["ok"]


async def test_different_deployments_prepare_in_parallel(running_host, sample_pack, tmp_path, monkeypatch):
    """按 deployment 分片单飞，不得退化成所有 load 共用一把重活锁。"""
    import osca_host.host as host_mod

    second_pack = tmp_path / "second.osca"
    shutil.copytree(sample_pack, second_pack)
    manifest = second_pack / "osca.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "package_id: demo-group-oper-diagnosis",
            "package_id: demo-group-oper-diagnosis-second",
        ),
        encoding="utf-8",
    )
    host = running_host
    host.deployments.update({"one": {"path": str(sample_pack)}, "two": {"path": str(second_pack)}})
    real = host_mod.load_for_host
    both_entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = 0

    def barrier(*args, **kwargs):
        nonlocal active
        with guard:
            active += 1
            if active == 2:
                both_entered.set()
        release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", barrier)
    one = asyncio.create_task(_send({"cmd": "load", "deployment_id": "one"}, host))
    two = asyncio.create_task(_send({"cmd": "load", "deployment_id": "two"}, host))
    try:
        assert await asyncio.to_thread(both_entered.wait, 5)
    finally:
        release.set()
    results = await asyncio.gather(one, two)
    assert all(result["ok"] for result in results)


async def test_unload_tombstone_beats_old_load_generation(running_host, sample_pack, monkeypatch):
    """load(old) / unload / load(new)：旧 generation 不得迟到覆盖新 generation。"""
    import osca_host.host as host_mod

    host = running_host
    did = "generation"
    pid = "demo-group-oper-diagnosis"
    host.deployments[did] = {"path": str(sample_pack)}

    real = host_mod.load_for_host
    old_entered, release_old, new_entered = threading.Event(), threading.Event(), threading.Event()
    guard = threading.Lock()
    calls = 0

    def interleaved(*args, **kwargs):
        nonlocal calls
        with guard:
            calls += 1
            call = calls
        if call == 1:
            old_entered.set()
            release_old.wait(timeout=10)
        else:
            new_entered.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(host_mod, "load_for_host", interleaved)
    old = asyncio.create_task(_send({"cmd": "load", "deployment_id": did}, host))
    assert await asyncio.to_thread(old_entered.wait, 5)
    old_generation = host._load_slots[did][0]
    unload = await _send({"cmd": "unload", "package_id": pid}, host)
    assert not unload["ok"] and "未注册" in unload["detail"]  # 未发布也仍写下停止 tombstone
    new = asyncio.create_task(_send({"cmd": "load", "deployment_id": did}, host))
    for _ in range(100):
        slot = host._load_slots.get(did)
        if slot is not None and slot[0] != old_generation:
            break
        await asyncio.sleep(0.01)
    assert host._load_slots[did][0] != old_generation  # 新请求已确实进入并在 per-deployment lock 外等待
    new_started_early = new_entered.is_set()
    release_old.set()
    old_result, new_result = await asyncio.gather(old, new)
    assert not new_started_early  # 同 deployment 的重活保持单飞
    assert not old_result.get("ok")
    assert new_result["ok"]
    assert new_entered.is_set()
    assert list(host.registry.packages) == [pid]


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


async def test_shutdown_cancels_tracked_idle_control_request(sock_path):
    """shutdown 不得被未发完整请求的连接拖到读超时；连接任务必须可追踪、可清退。"""
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    idle = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    idle.connect(str(sock_path))
    try:
        await asyncio.sleep(0.1)
        host._stop.set()
        assert await asyncio.wait_for(task, timeout=2) == 0
    finally:
        idle.close()


async def test_host_task_cancellation_releases_socket_lock_and_runtime_fd(sock_path):
    """嵌入式调用取消 Host.run 也必须走幂等 shutdown，不能留下 listener/flock/fd。"""
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sock_path.exists()
    assert host.control._lock_fd is None
    assert host.control._runtime is None

    retry = Host(sock_path)
    retry_task = asyncio.create_task(retry.run())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    assert not retry_task.done()
    retry._stop.set()
    await asyncio.wait_for(retry_task, timeout=5)


async def test_host_cancellation_during_control_start_releases_runtime_fd(sock_path, monkeypatch):
    """启动尚未发布 listener 时取消，也必须关闭已锚定的 runtime fd 并进入 STOPPED。"""
    host = Host(sock_path)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def stalled_start():
        entered.set()
        await blocked.wait()

    monkeypatch.setattr(host.control, "start", stalled_start)
    task = asyncio.create_task(host.run())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.control._runtime is None
    assert host.control._lock_fd is None
    assert host.state is HostState.STOPPED


@pytest.mark.skipif(os.geteuid() != 0, reason="需要 root 才能让子进程切换真实 uid")
async def test_real_different_uid_kernel_probe(sock_path):
    """真实 setuid 探针：group 只解决可达性，UID/token/role 仍逐层裁决。"""
    client_uid, stranger_uid = 65534, 65533
    group = grp.getgrgid(os.getgid()).gr_name
    os.chmod(sock_path.parent, 0o710)
    os.chown(sock_path.parent, -1, os.getgid())
    token = "different-uid-operator-token"
    pfile = principals_path(sock_path)
    pfile.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "uid-client",
                    "role": "operator",
                    "uid": client_uid,
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(pfile, 0o600)
    host = Host(sock_path, control_group=group)
    reached_handler = 0
    real_handler = host.control.handler

    async def counted(request, principal):
        nonlocal reached_handler
        reached_handler += 1
        return await real_handler(request, principal)

    host.control.handler = counted
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if sock_path.exists() or task.done():
            break
        await asyncio.sleep(0.01)
    assert not task.done()
    admin = admin_token_path(sock_path).read_text(encoding="utf-8").strip()
    code = (
        "import json,socket,sys;"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.connect(sys.argv[1]);"
        "s.sendall((json.dumps({'v':1,'cmd':'status','token':sys.argv[2]})+'\\n').encode());"
        "b=b'';"
        "\nwhile not b.endswith(b'\\n'):\n c=s.recv(65536); b+=c\n"
        "print(b.decode())"
    )
    # root 集成任务常把 pytest 装在 0700 临时 venv；降权子进程无法执行其中的
    # sys.executable。探针本身只用标准库，优先走所有 UID 可执行的系统 Python。
    probe_python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable

    def probe(uid: int, probe_token: str) -> dict:
        def demote():
            os.setgroups([os.getgid()])
            os.setgid(os.getgid())
            os.setuid(uid)

        completed = subprocess.run(
            [probe_python, "-c", code, str(sock_path), probe_token],
            capture_output=True,
            text=True,
            timeout=5,
            preexec_fn=demote,
            check=True,
        )
        return json.loads(completed.stdout)

    try:
        assert (await asyncio.to_thread(probe, client_uid, token))["ok"]
        assert reached_handler == 1
        assert (await asyncio.to_thread(probe, client_uid, admin))["error"] == "unauthorized"
        assert reached_handler == 1  # 错 UID + 被盗 admin token 未进入 Host handler
        assert (await asyncio.to_thread(probe, stranger_uid, token))["error"] == "unauthorized"
        assert reached_handler == 1  # 未授权 UID 在读取/处理命令前被拒
    finally:
        host._stop.set()
        await asyncio.wait_for(task, timeout=5)
