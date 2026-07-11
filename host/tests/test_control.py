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


async def test_w2_trigger_stop_and_manual_fire(running_host, sample_pack):
    """W2 验收路径:布防 → 触发器停/启 → 人工发射穿透闸门唤醒。"""
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack)}, host)
    pid = "demo-group-oper-diagnosis"

    # 布防:3 条订阅,槽位 armed;schedule watcher 已排好下次触发
    response = await _send({"cmd": "status"}, host)
    (pkg,) = response["packages"]
    assert [w["state"] for w in pkg["watchers"]] == ["armed"] * 3
    kinds = {t["kind"]: t for t in response["triggers"]}
    assert kinds["schedule"]["next_fire"] is not None

    # 触发器停(三级停之二):撤防但包仍在
    response = await _send({"cmd": "disable", "package_id": pid, "aware_id": "AW-001"}, host)
    assert response["ok"]
    response = await _send({"cmd": "status"}, host)
    (pkg,) = response["packages"]
    assert [w["state"] for w in pkg["watchers"]] == ["disabled"] * 3
    assert response["triggers"] == []  # 引用归零,watcher 全拆

    # 停时人工发射被拒(未布防)
    response = await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert not response["ok"]

    # 触发器启 → 人工发射 event → 闸门放行唤醒
    await _send({"cmd": "enable", "package_id": pid, "aware_id": "AW-001"}, host)
    response = await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert response["ok"]
    response = await _send({"cmd": "status"}, host)
    (gate,) = response["packages"][0]["gates"]
    assert gate["wakes"] == 1 and gate["last_wake"] is not None

    # 非 event 不可人工发射(纪律)
    response = await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T1"}, host)
    assert not response["ok"]


async def test_w3_wake_assembles_episode(running_host, sample_pack):
    """W3 验收路径:发射 → 唤醒 → 剧集装配进台账 → 完整上下文可导出。"""
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack)}, host)
    pid = "demo-group-oper-diagnosis"

    response = await _send({"cmd": "episodes"}, host)
    assert response["ok"] and response["episodes"] == []  # 唤醒前台账为空

    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    response = await _send({"cmd": "episodes"}, host)
    (summary,) = response["episodes"]
    assert summary["episode_id"] == "EP-0001"
    assert summary["fired_trigger"] == "AW-001/T3"
    assert summary["judgments"] == ["J-0417", "J-0423"]

    response = await _send({"cmd": "episode", "episode_id": "EP-0001"}, host)
    ctx = response["episode"]["context"]
    assert ctx["structure"]["structure_id"] == "STR-001"
    assert "policy" not in ctx  # 公理 A5:模型不读笼子

    response = await _send({"cmd": "episode", "episode_id": "EP-9999"}, host)
    assert not response["ok"]
