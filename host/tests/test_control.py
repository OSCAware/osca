"""控制通道端到端：起 Host → status/load/unload/stop 全走一遍 unix socket。

这就是 W1 验收路径：进程起得来、装载/注销样例包、包停可演示。
"""

from __future__ import annotations

import asyncio

import pytest

from osca_host.control import send_command
from osca_host.host import Host


@pytest.fixture
async def running_host(sock_path):
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):  # 等控制通道就绪
        if host.control.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    yield host
    host._stop.set()
    await asyncio.wait_for(task, timeout=5)


async def _send(request, host):
    return await asyncio.to_thread(send_command, request, host.control.socket_path)


async def test_full_lifecycle(running_host, sample_pack):
    host = running_host

    # 空注册表
    response = await _send({"cmd": "status"}, host)
    assert response["ok"] and response["packages"] == []

    # 装载样例包
    response = await _send({"cmd": "load", "path": str(sample_pack)}, host)
    assert response["ok"]
    assert response["package_id"] == "demo-group-oper-diagnosis"

    # 快照可见包与 watcher 槽位
    response = await _send({"cmd": "status"}, host)
    (pkg,) = response["packages"]
    assert len(pkg["watchers"]) == 3

    # 包停
    response = await _send({"cmd": "unload", "package_id": "demo-group-oper-diagnosis"}, host)
    assert response["ok"]
    response = await _send({"cmd": "status"}, host)
    assert response["packages"] == []


async def test_load_invalid_pack_rejected(running_host, tmp_path):
    bad = tmp_path / "bad.osca"
    bad.mkdir()
    (bad / "osca.yaml").write_text("format: osca\n", encoding="utf-8")
    response = await _send({"cmd": "load", "path": str(bad)}, running_host)
    assert not response["ok"]


async def test_unload_unknown_package(running_host):
    response = await _send({"cmd": "unload", "package_id": "ghost"}, running_host)
    assert not response["ok"]
    assert "未注册" in response["detail"]


async def test_stop_command(sock_path):
    host = Host(sock_path)
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if host.control.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    response = await asyncio.to_thread(send_command, {"cmd": "stop"}, host.control.socket_path)
    assert response["ok"]
    assert await asyncio.wait_for(task, timeout=5) == 0
    assert not host.control.socket_path.exists()  # 通道已清理


def test_client_without_host(tmp_path):
    response = send_command({"cmd": "status"}, tmp_path / "nowhere.sock")
    assert not response["ok"]
    assert "未运行" in response["detail"]
