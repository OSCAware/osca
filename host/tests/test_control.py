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


async def test_w2_trigger_stop_and_manual_fire(running_host, sample_pack, deploy):
    """W2 验收路径:布防 → 触发器停/启 → 人工发射穿透闸门唤醒。"""
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack), "bindings": str(deploy)}, host)
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


async def test_w3_wake_assembles_episode(running_host, sample_pack, deploy):
    """W3 验收路径:发射 → 唤醒 → 剧集装配进台账 → 完整上下文可导出。"""
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack), "bindings": str(deploy)}, host)
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


@pytest.fixture
def deploy(tmp_path):
    """部署环境:mock 固件目录 + bindings.yaml(binding 永不进包,这里模拟运维注入)。"""
    import yaml as _yaml

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "拉取费用明细.yaml").write_text(
        _yaml.safe_dump({"已关账": True, "rows": [{"科目": "差旅费", "金额": 45}]}, allow_unicode=True),
        encoding="utf-8",
    )
    bindings = tmp_path / "bindings.yaml"
    bindings.write_text(
        _yaml.safe_dump({"FINANCE_DB": {"endpoint": f"mock://{fixtures}", "secret_ref": "FINANCE_DB_RO_KEY"}}),
        encoding="utf-8",
    )
    return bindings


async def test_w4_precondition_evaluated_through_proxy(running_host, sample_pack, deploy):
    """W4 验收路径:装载带 binding → 发射 → precondition 经代理真求值 → 唤醒装配。"""
    host = running_host
    response = await _send({"cmd": "load", "path": str(sample_pack), "bindings": str(deploy)}, host)
    assert response["ok"]
    pid = "demo-group-oper-diagnosis"

    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    response = await _send({"cmd": "status"}, host)
    (pkg,) = response["packages"]
    (gate,) = pkg["gates"]
    assert gate["wakes"] == 1 and gate["precondition_blocked"] == 0  # 真求值通过,非默认放行

    policy = pkg["policy"]
    assert policy["kill_switch_tripped"] is False
    assert policy["max_tool_calls"] == 30
    assert "终稿发送管理层" in policy["approvals"]
    # precondition 的取数在政策审计里留了「运行时内部调用」放行痕
    assert any(a["subject"] == "CON-001.拉取费用明细" and a["decision"] == "allow" for a in policy["audit_tail"])

    response = await _send({"cmd": "episodes"}, host)
    assert len(response["episodes"]) == 1


async def test_w4_approval_gate_via_control(running_host, sample_pack, deploy):
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack), "bindings": str(deploy)}, host)
    pid = "demo-group-oper-diagnosis"

    response = await _send({"cmd": "approve", "package_id": pid, "action": "终稿发送管理层"}, host)
    assert response["ok"]
    response = await _send({"cmd": "status"}, host)
    assert response["packages"][0]["policy"]["approvals"]["终稿发送管理层"] == "granted"

    response = await _send({"cmd": "approve", "package_id": pid, "action": "不存在的动作"}, host)
    assert not response["ok"]


async def test_w4_precondition_blocks_without_bindings(running_host, sample_pack):
    """未注入部署 binding → precondition 取数失败 → 保守拦截唤醒(不装配)。"""
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack)}, host)
    pid = "demo-group-oper-diagnosis"
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    response = await _send({"cmd": "status"}, host)
    (gate,) = response["packages"][0]["gates"]
    assert gate["wakes"] == 0 and gate["precondition_blocked"] == 1
    response = await _send({"cmd": "episodes"}, host)
    assert response["episodes"] == []


@pytest.fixture
def deploy_w5(tmp_path, monkeypatch):
    """W5 部署环境:双接口 mock 固件 + bindings + LLM mock 通道(环境变量注入,CI 不联网)。"""
    import yaml as _yaml

    fixtures = tmp_path / "con-fixtures"
    fixtures.mkdir()
    (fixtures / "拉取费用明细.yaml").write_text(
        _yaml.safe_dump({"已关账": True, "rows": [{"科目": "差旅费", "环比涨幅": 45}]}, allow_unicode=True),
        encoding="utf-8",
    )
    (fixtures / "拉取检修计划期.yaml").write_text(
        _yaml.safe_dump({"处于检修期": True, "近三年检修期峰值涨幅": 60}, allow_unicode=True), encoding="utf-8"
    )
    llm = tmp_path / "llm-fixtures" / "episode"
    llm.mkdir(parents=True)
    (llm / "生成报警候选.md").write_text("- 甲单位 差旅费 +45%（检修期内）\n", encoding="utf-8")
    (llm / "裁决.md").write_text("- 甲单位 差旅费 +45% → 正常波动（J-0417），落附录\n", encoding="utf-8")
    (llm / "成文.md").write_text(
        "草稿正文：（无异动进正文）\n附录：甲单位差旅费 +45%，检修期常态波动（J-0417）。\n", encoding="utf-8"
    )
    monkeypatch.setenv("OSCA_LLM_URL", f"mock://{tmp_path / 'llm-fixtures'}")

    bindings = tmp_path / "bindings.yaml"
    bindings.write_text(
        _yaml.safe_dump({"FINANCE_DB": {"endpoint": f"mock://{fixtures}", "secret_ref": "FINANCE_DB_RO_KEY"}}),
        encoding="utf-8",
    )
    return bindings


async def test_w5_fire_runs_pipeline_to_draft(running_host, sample_pack, deploy_w5):
    """W5 验收路径(发布凭据第三样的运行侧):装载 → 发射 → 跑完 pipeline 出草稿 → 台账可导出。"""
    host = running_host
    await _send({"cmd": "load", "path": str(sample_pack), "bindings": str(deploy_w5)}, host)
    pid = "demo-group-oper-diagnosis"
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)

    # 剧集在认知平面线程执行;轮询台账等终态
    response = await _send({"cmd": "episodes"}, host)
    for _ in range(250):
        response = await _send({"cmd": "episodes"}, host)
        if response["episodes"] and response["episodes"][0]["status"] not in ("assembled", "running"):
            break
        await asyncio.sleep(0.02)

    (summary,) = response["episodes"]
    assert summary["status"] == "completed"
    assert summary["draft_ready"] is True and summary["tokens_used"] > 0

    response = await _send({"cmd": "episode", "episode_id": summary["episode_id"]}, host)
    episode = response["episode"]
    assert [s["status"] for s in episode["steps"]] == ["done", "done", "done", "done", "handoff"]
    assert "检修期常态波动" in episode["draft"]  # 机器侧交付物:草稿待专家终审
    assert episode["settlements"] == []  # 主观场景无 objective 对象——对账只属闭环场景
