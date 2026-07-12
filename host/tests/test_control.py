"""控制通道端到端：起 Host → status/load/unload/stop 全走一遍 unix socket。

这就是 W1 验收路径：进程起得来、装载/注销样例包、包停可演示。
"""

from __future__ import annotations

import asyncio

import pytest

from osca_host.authz import Principal
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


async def _send(request, host, token=None):
    return await asyncio.to_thread(send_command, request, host.control.socket_path, 30.0, token)


async def _load_pack(host, path, bindings=None, did="t-pack"):
    """装载走部署 ID（M4-W0）：路径类参数只住服务端部署清单，控制通道只收 ID。"""
    host.deployments[did] = {"path": str(path), "bindings": str(bindings) if bindings else None}
    return await _send({"cmd": "load", "deployment_id": did}, host)


async def test_full_lifecycle(running_host, sample_pack):
    host = running_host

    # 空注册表
    response = await _send({"cmd": "status"}, host)
    assert response["ok"] and response["packages"] == []

    # 装载样例包
    response = await _load_pack(host, sample_pack)
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
    response = await _load_pack(running_host, bad)
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
    await _load_pack(host, sample_pack, deploy)
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
    await _load_pack(host, sample_pack, deploy)
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
    response = await _load_pack(host, sample_pack, deploy)
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


async def test_w4_approval_gate_closed_until_challenge(running_host, sample_pack, deploy):
    """审批 RPC 在 W3 challenge 落地前对全角色关闭——旧 set[action] 授予不从控制通道可达。

    M2 的审批门语义仍在（policy 内部授予接口，runner 消费）；W3 用持久化 challenge
    （pending→approved|denied→consumed，绑定 approver/episode/digest/expiry/nonce）替换它。
    """
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    approver = "approver-token-0001"
    host.authorizer.register(approver, Principal("审批卡", "approver"))
    for token in (None, approver):  # admin 与 approver 一视同仁：无绑定授予面不暴露
        response = await _send({"cmd": "approve", "package_id": pid, "action": "终稿发送管理层"}, host, token=token)
        assert not response["ok"] and response["error"] == "forbidden"

    ok, _ = host.policies[pid].grant_approval("终稿发送管理层")  # M2 语义（内部接口）未动
    assert ok
    response = await _send({"cmd": "status"}, host)
    assert response["packages"][0]["policy"]["approvals"]["终稿发送管理层"] == "granted"


async def test_w4_precondition_blocks_without_bindings(running_host, sample_pack):
    """未注入部署 binding → precondition 取数失败 → 保守拦截唤醒(不装配)。"""
    host = running_host
    await _load_pack(host, sample_pack)
    pid = "demo-group-oper-diagnosis"
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    response = await _send({"cmd": "status"}, host)
    (gate,) = response["packages"][0]["gates"]
    assert gate["wakes"] == 0 and gate["precondition_blocked"] == 1
    response = await _send({"cmd": "episodes"}, host)
    assert response["episodes"] == []


async def test_kill_switch_recomputed_at_wakeup(running_host, sample_pack, deploy):
    """账本健康度即安全信号：M3 采集器落账后计数恶化，Host 不重启、下次唤醒前重算即拒。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    response = await _send({"cmd": "episodes"}, host)
    assert len(response["episodes"]) == 1  # 装载时账本健康，唤醒成功

    # 模拟 M3 采集器落账：现役判断被大量推翻（9/7 > 0.3），Host 进程不重启。
    # trust 同步降 provisional——保持账本 lint 合规（唤醒前的刷新校验只放行合规账本）
    j = sample_pack / "judgments" / "J-0417.yaml"
    text = (
        j.read_text(encoding="utf-8")
        .replace("overruled: 0", "overruled: 9")
        .replace("trust: high", "trust: provisional")
    )
    j.write_text(text, encoding="utf-8")

    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    response = await _send({"cmd": "status"}, host)
    assert response["packages"][0]["policy"]["kill_switch_tripped"] is True  # 唤醒前已重算
    response = await _send({"cmd": "episodes"}, host)
    assert len(response["episodes"]) == 1  # 第二次唤醒被拒——没有新剧集


async def test_wakeup_sees_newly_distilled_judgment(running_host, sample_pack, deploy):
    """长跑 Host 的账本以磁盘为准：M3 拍板落账后，不 unload/load，下次唤醒即用新判断。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    # 模拟 M3 拍板落账：磁盘新增一条 active 判断（Host 进程不重启）
    (sample_pack / "judgments" / "J-0500.yaml").write_text(
        "judgment_id: J-0500\n"
        "status: active\n"
        "supersedes: null\n"
        'signature: {object: OBJ-002, aware: AW-001, guard: "费用科目 == 会议费"}\n'
        "body: |\n  新蒸馏的判断：会议费异动按季度滚动看。\n"
        "evidence: [C-0102]\n"
        "meta: {author: 王工, confirmed: 0, overruled: 0, trust: provisional}\n"
        "expiry: [口径变更]\n"
        "replay:\n  - {given: C-0102.input, with_this_judgment: 压制单月报警}\n",
        encoding="utf-8",
    )
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    (summary,) = (await _send({"cmd": "episodes"}, host))["episodes"]
    assert "J-0500" in summary["judgments"]  # 唤醒前刷新包内容 + 签名表——新判断即入检索


async def test_wakeup_refused_while_ledger_locked(running_host, sample_pack, deploy):
    """写入者事务进行中（持账本写锁）→ 唤醒拒绝、保留旧快照——不读半截账本。"""
    from osca_cli.ledger import ledger_lock

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    with ledger_lock(sample_pack):  # 模拟 oscapipe capture/confirm 正持锁写账
        await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
        assert (await _send({"cmd": "episodes"}, host))["episodes"] == []  # 拒绝唤醒

    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert len((await _send({"cmd": "episodes"}, host))["episodes"]) == 1  # 锁释放后照常


async def test_wakeup_refused_on_broken_ledger(running_host, sample_pack, deploy):
    """磁盘账本不合规（如写入中断留下不可解析判断）→ 唤醒拒绝、保留旧快照。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    (sample_pack / "judgments" / "J-0999.yaml").write_text("judgment_id: [未闭合", encoding="utf-8")
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert (await _send({"cmd": "episodes"}, host))["episodes"] == []  # 半截账本不装配


async def test_refresh_exception_refuses_wakeup_and_callback_survives(running_host, sample_pack, deploy, monkeypatch):
    """刷新是安全边界：磁盘满等普通异常不许穿透 trigger 回调——拒绝本次唤醒，故障修复后照常。"""
    import osca_host.host as host_mod

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    def disk_full(root, pkg=None):
        raise OSError("simulated disk full")

    monkeypatch.setattr(host_mod, "rebuild_index", disk_full)
    response = await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert response["ok"]  # 发射本身干净返回——异常没有穿透控制通道
    assert (await _send({"cmd": "episodes"}, host))["episodes"] == []  # 唤醒被拒，旧快照保留

    monkeypatch.undo()  # 「修好磁盘」后无需任何干预即自然恢复
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert len((await _send({"cmd": "episodes"}, host))["episodes"]) == 1


async def test_policy_publish_inside_refresh_transaction(running_host, sample_pack, deploy, monkeypatch):
    """pack 与 policy 同进退：kill switch 评估在保护区内纯计算，评估异常 → 旧快照原样保留。"""
    from osca_host.policy import PolicyInterceptor

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    old_pack = host.registry.packages[pid].pack

    def boom(self, stats):
        raise RuntimeError("评估失败（测试注入）")

    monkeypatch.setattr(PolicyInterceptor, "evaluate_kill_switch", boom)
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert (await _send({"cmd": "episodes"}, host))["episodes"] == []  # 唤醒拒绝
    assert host.registry.packages[pid].pack is old_pack  # 新 pack 未发布——不存在半发布状态

    monkeypatch.undo()
    await _send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host)
    assert len((await _send({"cmd": "episodes"}, host))["episodes"]) == 1


async def test_enable_failure_rolls_back_and_stays_retryable(running_host, sample_pack, deploy, monkeypatch):
    """enable 全部订阅成功才置位：半路失败即补偿回滚，不留「显示启用、实际半布防」，且可重试修复。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    await _send({"cmd": "disable", "package_id": pid, "aware_id": "AW-001"}, host)

    original = host.table.subscribe
    calls = {"n": 0}

    def flaky(kind, spec, sub):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("布防失败（测试注入）")
        return original(kind, spec, sub)

    monkeypatch.setattr(host.table, "subscribe", flaky)
    response = await _send({"cmd": "enable", "package_id": pid, "aware_id": "AW-001"}, host)
    assert not response["ok"] and "补偿回滚" in response["detail"]
    status = await _send({"cmd": "status"}, host)
    assert status["triggers"] == []  # 已布防的第一条也撤了
    assert [w["state"] for w in status["packages"][0]["watchers"]] == ["disabled"] * 3

    monkeypatch.undo()  # 「修复故障」后重试即成——不会被幂等挡回
    response = await _send({"cmd": "enable", "package_id": pid, "aware_id": "AW-001"}, host)
    assert response["ok"] and "重新布防 3 条" in response["detail"]


async def test_arming_failure_rolls_back_registration(running_host, sample_pack, monkeypatch):
    """发布与布防同生共死：第二条订阅失败 → 补偿回滚，注册表/笼子/闸门/watcher 零残留。"""
    host = running_host
    original = host.table.subscribe
    calls = {"n": 0}

    def flaky(kind, spec, sub):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("布防失败（测试注入）")
        return original(kind, spec, sub)

    monkeypatch.setattr(host.table, "subscribe", flaky)
    response = await _load_pack(host, sample_pack)
    assert not response["ok"]
    assert any("补偿回滚" in line for line in response["detail"])

    status = await _send({"cmd": "status"}, host)
    assert status["packages"] == [] and status["triggers"] == []  # 第一条 watcher 也已撤
    assert host.policies == {} and host.proxies == {} and host.gates == {} and host.bindings == {}


async def test_load_failure_leaves_no_half_registered_package(running_host, sample_pack, monkeypatch):
    """原子发布：运行时构建（policy/proxy/gate）任一失败，注册表不得留下半注册包。"""
    import osca_host.host as host_mod

    def boom(*args, **kwargs):
        raise RuntimeError("构造失败（测试注入）")

    monkeypatch.setattr(host_mod, "PolicyInterceptor", boom)
    response = await _load_pack(running_host, sample_pack)
    assert not response["ok"]
    assert any("包未注册" in line for line in response["detail"])

    response = await _send({"cmd": "status"}, running_host)
    assert response["packages"] == []  # 无半注册包
    assert response["triggers"] == []  # 无残留布防


async def test_enable_is_idempotent(running_host, sample_pack, deploy):
    """对已启用 Aware 重复 enable 不得重复订阅（否则一次触发双份唤醒）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    response = await _send({"cmd": "enable", "package_id": pid, "aware_id": "AW-001"}, host)
    assert response["ok"] and "幂等" in response["detail"]
    # disable → enable 的正常路径不受影响
    await _send({"cmd": "disable", "package_id": pid, "aware_id": "AW-001"}, host)
    response = await _send({"cmd": "enable", "package_id": pid, "aware_id": "AW-001"}, host)
    assert response["ok"] and "重新布防 3 条" in response["detail"]


async def test_bindings_isolated_per_package(running_host, sample_pack, deploy, tmp_path):
    """同名 binding 不跨包串线：后装包不改先装包的连接目标，卸载即清理。"""
    import shutil

    import yaml as _yaml

    host = running_host
    await _load_pack(host, sample_pack, deploy)

    pack_b = tmp_path / "pack-b.osca"
    shutil.copytree(sample_pack, pack_b, ignore=shutil.ignore_patterns("indexes"))
    manifest = pack_b / "osca.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "package_id: demo-group-oper-diagnosis", "package_id: demo-group-oper-diagnosis-b"
        ),
        encoding="utf-8",
    )
    fixtures_b = tmp_path / "fixtures-b"
    fixtures_b.mkdir()
    bindings_b = tmp_path / "bindings-b.yaml"
    bindings_b.write_text(
        _yaml.safe_dump({"FINANCE_DB": {"endpoint": f"mock://{fixtures_b}", "secret_ref": "K"}}), encoding="utf-8"
    )
    response = await _load_pack(host, pack_b, bindings_b, did="t-pack-b")
    assert response["ok"]

    a = host.proxies["demo-group-oper-diagnosis"].bindings["FINANCE_DB"]["endpoint"]
    b = host.proxies["demo-group-oper-diagnosis-b"].bindings["FINANCE_DB"]["endpoint"]
    assert a != b and str(fixtures_b) in b  # 各连各的库

    await _send({"cmd": "unload", "package_id": "demo-group-oper-diagnosis-b"}, host)
    assert "demo-group-oper-diagnosis-b" not in host.bindings  # binding 随包清理


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
    await _load_pack(host, sample_pack, deploy_w5)
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
    # 样例包现为闭环场景(OBJ-003 objective):剧集完成后对账器自动落 outcome case。
    # 对账在剧集终态之后的线程里运行——轮询等它落账。
    for _ in range(250):
        if episode["settlements"]:
            break
        await asyncio.sleep(0.02)
        episode = (await _send({"cmd": "episode", "episode_id": summary["episode_id"]}, host))["episode"]
    (settlement,) = episode["settlements"]
    assert settlement["settled"] is True and settlement["case"] == "C-0103"
