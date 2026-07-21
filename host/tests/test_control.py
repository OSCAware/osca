"""控制通道端到端：起 Host → status/load/unload/stop 全走一遍 unix socket。

这就是 W1 验收路径：进程起得来、装载/注销样例包、包停可演示。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy

import pytest

from osca_host.authz import Principal
from osca_host.control import send_command
from osca_host.episode import Episode, assemble
from osca_host.host import Host
from osca_host.runner import run_episode


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


def _stub_bindings(path) -> str:
    """装载门禁（P1）后 Host 路径必须注入 required bindings——无真实部署环境的测试给 mock stub。"""
    import yaml as _yaml

    target = path.parent / f"{path.name}-stub-bindings.yaml"
    target.write_text(
        _yaml.safe_dump({"FINANCE_DB": {"endpoint": "mock:///nonexistent-fixtures"}}),
        encoding="utf-8",
    )
    return str(target)


async def _load_pack(host, path, bindings=None, did="t-pack"):
    """装载走部署 ID（M4-W0）：路径类参数只住服务端部署清单，控制通道只收 ID。"""
    host.deployments[did] = {"path": str(path), "bindings": str(bindings) if bindings else _stub_bindings(path)}
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


async def test_w3_approval_challenge_control_flow(running_host, sample_pack, deploy):
    """W3 审批 challenge 控制通道接线：challenges 列待批 → approve（绑 challenge_id，名须相符）→ 一次性 consume。

    绑定挑战（pending→approved|denied→consumed，绑 approver/episode/payload digest/expiry
    + 一次性 consume）替换旧无绑定 set[action]：批一张具体挑战、只放行同绑定的那一次写。
    """
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    policy = host.policies[pid]
    action = "终稿发送管理层"  # sample pack approvals：approver=专家

    # 模拟一次被审批门拦下的写：挂起一张 pending 挑战（绑 episode + payload 摘要）
    ok, detail = policy.require_write_approval(action, episode_id="EP-0001", payload={"折扣": "4.5"})
    assert not ok and "审批门拦截" in detail

    host.authorizer.register("approver-token-0001", Principal("专家", "approver"))  # name 须与 policy「专家」相符
    host.authorizer.register("imposter-token-01", Principal("冒名", "approver"))

    # admin（默认 token）无审批面：challenges/approve/deny 都不在 host_admin 能力集
    assert (await _send({"cmd": "challenges", "package_id": pid}, host))["error"] == "forbidden"

    # approver 拉待批清单（挑战 DTO；裁决痕迹不外泄——形状钉在 test_challenge）
    resp = await _send({"cmd": "challenges", "package_id": pid}, host, token="approver-token-0001")
    assert resp["ok"] and len(resp["challenges"]) == 1
    ch = resp["challenges"][0]
    assert ch["action"] == action and ch["approver"] == "专家" and "nonce" not in ch

    # 冒名审批人批不动（by_name 不符 → fail-closed）
    bad_id = ch["challenge_id"]
    bad = await _send({"cmd": "approve", "package_id": pid, "challenge_id": bad_id}, host, token="imposter-token-01")
    assert not bad["ok"] and "审批人不符" in bad["detail"]

    # 正主批准
    good = await _send({"cmd": "approve", "package_id": pid, "challenge_id": bad_id}, host, token="approver-token-0001")
    assert good["ok"]

    # 同一绑定的写放行一次（consume），再写即拦（一次性）
    assert policy.require_write_approval(action, episode_id="EP-0001", payload={"折扣": "4.5"})[0]
    ok2, d2 = policy.require_write_approval(action, episode_id="EP-0001", payload={"折扣": "4.5"})
    assert not ok2 and "审批门拦截" in d2

    # status 诚实展示 action→指定审批人
    st = await _send({"cmd": "status"}, host)
    assert st["packages"][0]["policy"]["approvals"][action] == "专家"


async def test_w3_approval_deny_over_control_channel(running_host, sample_pack, deploy):
    """W3 deny：approver 经控制通道驳回一张挑战后，该绑定不可放行。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    policy = host.policies[pid]
    action = "终稿发送管理层"
    policy.require_write_approval(action, episode_id="EP-1", payload={})  # 挂挑战
    host.authorizer.register("approver-token-0001", Principal("专家", "approver"))

    resp = await _send({"cmd": "challenges", "package_id": pid}, host, token="approver-token-0001")
    cid = resp["challenges"][0]["challenge_id"]
    denied = await _send({"cmd": "deny", "package_id": pid, "challenge_id": cid}, host, token="approver-token-0001")
    assert denied["ok"] and "驳回" in denied["detail"]
    assert not policy.require_write_approval(action, episode_id="EP-1", payload={})[0]  # 驳回后不可放行


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


# ── D2a 可恢复剧集：Host 编排（挂起登记 / approve·deny 触发恢复 / 登记侧自愈 / 清扫 / 免淘汰）──


def _setup_write_episode(host, pid, write_ref="CON-001.拉取费用明细"):
    """在已装载包上装 [agent 产草稿 → 写步(input=草稿)] 可恢复剧集，配写门白名单/approvals；
    LLM 由 deploy_w5 的 mock 通道供给。返回执行所需句柄。"""
    loaded = host.registry.packages[pid]
    proxy = host.proxies[pid]
    policy = host.policies[pid]
    proxy.connectors["CON-001"].setdefault("permissions", {})["write"] = "allowed_with_approval"
    policy.permissions["下发"] = {write_ref}
    policy.approvals[write_ref] = "专家"
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    episode = assemble("EP-0001", loaded, aware, "AW-001/T3")
    episode.context = copy.deepcopy(episode.context)
    episode.context["structure"]["pipeline"] = [
        {"step": "生成报警候选", "performer": "agent", "produces": "草稿"},
        {"step": "下发", "performer": "connector", "uses": write_ref, "input": "草稿"},
    ]
    host.episodes[episode.episode_id] = episode
    return episode, loaded, proxy, policy


async def _await_status(episode, target="completed", tries=300):
    for _ in range(tries):
        if episode.status == target:
            return
        await asyncio.sleep(0.01)


async def test_d2a_suspend_approve_resume_lands_through_host(running_host, sample_pack, deploy_w5):
    """可恢复剧集端到端（async Host）：写步挂起 → 登记 → approver 经控制通道 approve → 恢复 → mock 写落地。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)

    await host._execute_episode(episode, loaded, proxy, policy)  # 首跑 → 挂起 + 登记
    assert episode.status == "suspended_pending_approval"
    [ch] = policy.pending_challenges()
    assert host._suspensions.get(ch["challenge_id"]) == episode.episode_id

    host.authorizer.register("approver-token-0001", Principal("专家", "approver"))
    good = await _send(
        {"cmd": "approve", "package_id": pid, "challenge_id": ch["challenge_id"]}, host, token="approver-token-0001"
    )
    assert good["ok"]

    await _await_status(episode, "completed")
    assert episode.status == "completed"
    step = next(s for s in episode.steps if s["step"] == "下发")
    assert step["status"] == "done"
    assert host._suspensions == {}  # 恢复后清出登记


async def test_d2a_deny_through_host_falls_back(running_host, sample_pack, deploy_w5):
    """deny 经控制通道 → 触发恢复 → 回落保守默认（不写）：剧集 completed（非 failed），写步记 denied。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)
    [ch] = policy.pending_challenges()
    host.authorizer.register("approver-token-0001", Principal("专家", "approver"))
    await _send(
        {"cmd": "deny", "package_id": pid, "challenge_id": ch["challenge_id"]}, host, token="approver-token-0001"
    )
    await _await_status(episode, "completed")
    assert episode.status == "completed"
    assert next(s for s in episode.steps if s["step"] == "下发")["status"] == "denied"


async def test_d2a_lost_wakeup_self_heals_on_registration(running_host, sample_pack, deploy_w5):
    """丢唤醒窗（§3.5 blocker）：审批在登记之前到达 → _register_suspension 复查发现已批 → 就地自愈恢复兑现。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)

    # 直接跑到挂起（绕过 _execute_episode 的登记，模拟「决定先到、登记后到」的窗）
    await asyncio.to_thread(run_episode, episode, loaded, proxy, policy)
    assert episode.status == "suspended_pending_approval"
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)  # 登记前批准
    assert host._suspensions == {}  # 尚未登记

    host._register_suspension(episode, loaded, proxy, policy)  # 登记侧自愈应就地恢复
    await _await_status(episode, "completed")
    assert episode.status == "completed"  # 自愈：登记时发现已批 → 恢复兑现（丢唤醒窗被堵）


async def test_d2a_sweep_resumes_decided_suspension(running_host, sample_pack, deploy_w5):
    """惰性清扫（§5.4）：挂起剧集的挑战已离开 pending 但恢复未被触发（漏触发/无决定超时）→ 清扫兜底恢复。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)  # 挂起 + 登记（pending）
    [ch] = policy.pending_challenges()
    # 直接裁决、不走控制通道 approve（故不触发 _maybe_resume）——模拟漏触发/无决定超时
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert episode.status == "suspended_pending_approval"  # 仍挂起（未触发恢复）

    host._sweep_suspensions()  # 清扫兜底
    await _await_status(episode, "completed")
    assert episode.status == "completed"


async def test_d2a_evict_excludes_suspended_episodes(sock_path, monkeypatch):
    """挂起剧集免 FIFO 淘汰（对抗审查 major-2）——否则已批写随剧集淘汰静默丢弃、击穿 INV-2。"""
    from osca_host import host as host_mod

    monkeypatch.setattr(host_mod, "EPISODE_LEDGER_CAP", 2)
    h = Host(sock_path)

    def ep(eid, status):
        e = Episode(eid, "p", "AW", "t", "", None, {}, {})
        e.status = status
        return e

    h.episodes["EP-1"] = ep("EP-1", "suspended_pending_approval")  # 最旧且挂起
    h.episodes["EP-2"] = ep("EP-2", "completed")
    h.episodes["EP-3"] = ep("EP-3", "completed")
    h._evict_old_episodes()
    assert "EP-1" in h.episodes  # 挂起免淘汰
    assert "EP-2" not in h.episodes and len(h.episodes) == 2  # 最旧的终态被淘汰


async def test_d2a_evict_excludes_inflight_write_episode(sock_path, monkeypatch):
    """在途（running/assembled）写剧集也免 FIFO 淘汰（对抗审查 major-B）——否则挂起前被淘汰、已批写永不兑现无报错。"""
    from osca_host import host as host_mod

    monkeypatch.setattr(host_mod, "EPISODE_LEDGER_CAP", 2)
    h = Host(sock_path)

    def ep(eid, status):
        e = Episode(eid, "p", "AW", "t", "", None, {}, {})
        e.status = status
        return e

    h.episodes["S1"] = ep("S1", "suspended_pending_approval")
    h.episodes["S2"] = ep("S2", "suspended_pending_approval")
    h.episodes["R3"] = ep("R3", "running")  # 在途写剧集，台账已满 2 条挂起
    h._evict_old_episodes()
    assert set(h.episodes) == {"S1", "S2", "R3"}  # 无终态可淘汰 → 全留（宁可超顶，不丢在途/挂起）


# ── D2b 可恢复剧集 L2 持久：磁盘落盘 + 装载重挂（活过包重载/Host 重启）──────────


def _reapply_write_config(host, pid, write_ref="CON-001.拉取费用明细"):
    """模拟包 policy.yaml 里本就声明的写审批配置——reload 后新 policy/proxy 也须有（真实包由 policy.yaml 承载，
    此处 monkeypatch 等价于该声明）。"""
    proxy, policy = host.proxies[pid], host.policies[pid]
    proxy.connectors["CON-001"].setdefault("permissions", {})["write"] = "allowed_with_approval"
    policy.permissions["下发"] = {write_ref}
    policy.approvals[write_ref] = "专家"


async def test_d2b_reattach_survives_reload_then_approve_lands(running_host, sample_pack, deploy_w5):
    """L2 活过包重载：写步挂起 → 快照落盘 → unload（快照留盘）→ reload → 重挂（真键 operation_id，重编展示号）
    → approve → 在同一剧集恢复兑现 mock 写落地；恢复调度即删盘；per_episode 计数不清零（INV-7）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)  # 已配写门
    await host._execute_episode(episode, loaded, proxy, policy)  # 首跑 → 挂起 + 落盘
    assert episode.status == "suspended_pending_approval"
    opid = episode.operation_id
    [ch] = policy.pending_challenges()
    cid = ch["challenge_id"]
    used_before = policy.episode_budget_used(episode.episode_id)
    assert used_before[1] > 0  # agent 步已计 tokens（INV-7 待验的计数）
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid]  # 快照已落盘

    await _send({"cmd": "unload", "package_id": pid}, host)
    assert episode.status == "stopped"  # 内存副本迁 stopped（L2 快照留盘）
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid]  # 快照仍在盘

    await _load_pack(host, sample_pack, deploy_w5)  # reload → _reattach 重挂
    _reapply_write_config(host, pid)  # reload 后新 policy 须有写配置（真实包由 policy.yaml 承载）

    reattached = next((e for e in host.episodes.values() if e.operation_id == opid), None)
    assert reattached is not None and reattached.status == "suspended_pending_approval"  # 真键 operation_id 定位
    new_policy = host.policies[pid]
    assert new_policy.get_challenge(cid) is not None  # 挑战注回新 store（challenge_id 不变）
    assert host._suspensions.get(cid) == reattached.episode_id
    assert new_policy.episode_budget_used(reattached.episode_id) == used_before  # INV-7：计数跨重挂不清零

    host.authorizer.register("approver-token-0001", Principal("专家", "approver"))
    good = await _send({"cmd": "approve", "package_id": pid, "challenge_id": cid}, host, token="approver-token-0001")
    assert good["ok"]
    await _await_status(reattached, "completed")
    assert reattached.status == "completed"
    assert next(s for s in reattached.steps if s["step"] == "下发")["status"] == "done"  # 兑现落地
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid] == []  # 恢复调度即删盘


async def test_d2b_reattach_discards_on_version_mismatch(running_host, sample_pack, deploy_w5):
    """包版本漂移：挂起后改包源文件（含**未提交**改动——版本戳按实际字节内容指纹，不靠 git tree OID）→
    重挂时戳不符 → 丢弃不兑现 + 删盘（fail-closed，§2.4 / GPT 外审 P1）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)
    opid = episode.operation_id
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid]  # 已落盘

    # 改包源文件（不 commit，只加 YAML 注释）——模拟挂起后运行语义变更：指纹变、旧快照不可安全兑现
    pf = sample_pack / "policy.yaml"
    pf.write_text(pf.read_text(encoding="utf-8") + "\n# drift（未提交改动）\n", encoding="utf-8")

    await _send({"cmd": "unload", "package_id": pid}, host)
    await _load_pack(host, sample_pack, deploy_w5)  # reload → 内容指纹不符 → 丢弃
    _reapply_write_config(host, pid)

    assert not any(e.operation_id == opid and e.status == "suspended_pending_approval" for e in host.episodes.values())
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid] == []  # 快照被丢弃删除


async def test_d2b_reattach_same_display_id_no_collision(running_host, sample_pack, deploy_w5):
    """blocker：两条持久快照展示号相同（EP-0001，跨会话/跨包复用低号）但 operation_id 不同 → 重挂各得独立
    展示号，两条都在、两挑战各指自己剧集（不静默顶掉一条活挂起写、不错接挑战，击穿 INV-2 的路径已堵）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    loaded = host.registry.packages[pid]
    aware = next(a for a in loaded.awares if a.aware_id == "AW-001")
    stamp = host._pack_stamp(loaded)
    store = host._suspension_store

    def _record(opid, cid):
        ep = assemble("EP-0001", loaded, aware, "AW-001/T3")  # 两条都铸同一展示号
        ep.operation_id = opid
        ep.status = "suspended_pending_approval"
        ep.resume = {
            "step_index": 1,
            "ref_index": 0,
            "payloads": {},
            "receipts": [],
            "write_params": {"x": 1},
            "artifacts": {},
            "challenge_id": cid,
        }
        ch = {
            "challenge_id": cid,
            "package_id": pid,
            "action": "CON-001.拉取费用明细",
            "approver": "专家",
            "episode_id": "EP-0001",
            "payload_digest": "d",
            "created_at": 1.0,
            "expires_at": 1e12,
            "state": "pending",
            "decided_by": None,
            "decided_at": None,
            "consumed_at": None,
        }
        return {
            "operation_id": opid,
            "package_id": pid,
            "episode": ep.dump(),
            "challenge": ch,
            "tool_calls": 0,
            "tokens": 0,
            "version_stamp": stamp,
        }

    store.persist("EO-aaa", _record("EO-aaa", "CH-a"))
    store.persist("EO-bbb", _record("EO-bbb", "CH-b"))
    await host._reattach_suspensions(pid)

    suspended = [e for e in host.episodes.values() if e.status == "suspended_pending_approval"]
    assert {e.operation_id for e in suspended} == {"EO-aaa", "EO-bbb"}  # 两条都在，未互相顶掉
    assert len({e.episode_id for e in suspended}) == 2  # 各得独立展示号（无冲突）
    assert host._suspensions["CH-a"] != host._suspensions["CH-b"]  # 两挑战各指自己的剧集（未错接）


async def test_d2b_delete_failure_keeps_suspended_not_fake_running(running_host, sample_pack, deploy_w5, monkeypatch):
    """删快照失败（磁盘/权限）→ 保留挂起态、不推进成永卡的假 running（GPT 外审 P1）：_suspensions 映射仍在、可重试。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)  # 挂起 + 落盘
    [ch] = policy.pending_challenges()
    cid = ch["challenge_id"]

    def _boom(_opid):
        raise OSError("模拟删盘失败（权限/磁盘）")

    monkeypatch.setattr(host._suspension_store, "delete", _boom)
    host._schedule_resume(episode.episode_id, loaded, proxy, policy)  # 删盘失败 → 不推进
    for _ in range(300):  # 恢复状态机异步（删盘在线程）——等它退场再断言
        if episode.episode_id not in host._resuming:
            break
        await asyncio.sleep(0.01)

    assert episode.status == "suspended_pending_approval"  # 仍挂起（未变假 running）
    assert host._suspensions.get(cid) == episode.episode_id  # 映射保留，可重试


async def test_d2b_survives_host_restart(sock_path, sample_pack, deploy_w5):
    """活过 Host 重启（真实进程边界回归，GPT 外审 P2）：Host A 挂起写剧集 + 退出（关 runtime fd / socket）→
    Host B 复用同一运行目录、_episode_seq 归零、全新 policy/authorizer → 装载重挂 → 重新 approve → 兑现。"""

    async def _ready(h):
        for _ in range(200):
            if h.control.socket_path.exists():
                return
            await asyncio.sleep(0.01)

    pid = "demo-group-oper-diagnosis"
    # ── Host A：起、装载、挂起、退出 ──
    host_a = Host(sock_path)
    task_a = asyncio.create_task(host_a.run())
    await _ready(host_a)
    await _load_pack(host_a, sample_pack, deploy_w5)
    ep_a, loaded, proxy, policy = _setup_write_episode(host_a, pid)
    await host_a._execute_episode(ep_a, loaded, proxy, policy)
    assert ep_a.status == "suspended_pending_approval"
    opid = ep_a.operation_id
    host_a._stop.set()
    await asyncio.wait_for(task_a, timeout=5)  # A 退出：关 socket/runtime fd，susp 快照留盘

    # ── Host B：同 sock_path 复用运行目录，全新对象，装载重挂 ──
    host_b = Host(sock_path)
    task_b = asyncio.create_task(host_b.run())
    try:
        await _ready(host_b)
        assert host_b._episode_seq == 0  # 新进程序号归零（真键 operation_id）
        await _load_pack(host_b, sample_pack, deploy_w5)  # → _reattach 读 A 的快照
        _reapply_write_config(host_b, pid)
        reattached = next((e for e in host_b.episodes.values() if e.operation_id == opid), None)
        assert reattached is not None and reattached.status == "suspended_pending_approval"
        cid = host_b.policies[pid].pending_challenges()[0]["challenge_id"]
        host_b.authorizer.register("approver-token-0001", Principal("专家", "approver"))
        good = await _send(
            {"cmd": "approve", "package_id": pid, "challenge_id": cid}, host_b, token="approver-token-0001"
        )
        assert good["ok"]
        await _await_status(reattached, "completed")
        assert reattached.status == "completed"  # 跨重启兑现
        assert next(s for s in reattached.steps if s["step"] == "下发")["status"] == "done"
    finally:
        host_b._stop.set()
        await asyncio.wait_for(task_b, timeout=5)


# ── GPT Review 复审：跨代发布 / persist-决定竞态 / fire 出全局锁（可控屏障时序测试）──────────


def _slow_refresh(monkeypatch):
    """把 _refresh_ledger 换成可控屏障版：entered 置位 = 已进慢刷新线程；release 放行后走原逻辑。"""
    import threading as _threading

    entered, release = _threading.Event(), _threading.Event()
    original = Host._refresh_ledger

    def slow(self, loaded, policy):
        entered.set()
        release.wait(timeout=10)
        return original(self, loaded, policy)

    monkeypatch.setattr(Host, "_refresh_ledger", slow)
    return entered, release


def _slow_stamp(monkeypatch):
    """把 _pack_stamp 换成可控屏障版（persist / reattach 的线程重活都会经过它）。"""
    import threading as _threading

    entered, release = _threading.Event(), _threading.Event()
    original = Host._pack_stamp

    def slow(loaded):
        entered.set()
        release.wait(timeout=10)
        return original(loaded)

    monkeypatch.setattr(Host, "_pack_stamp", staticmethod(slow))
    return entered, release


async def test_stale_delivery_not_published_after_unload_reload(running_host, sample_pack, deploy, monkeypatch):
    """跨代投递（复审 P1）：旧投递慢刷新期间 unload + 同 id reload → 线程返回后代际 CAS 失配，
    旧触发不得唤醒新包（不发布、无剧集）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    deliver = next(sub.deliver for w in host.table.watchers.values() for sub in w.subs if sub.trigger_id == "AW-001/T3")
    entered, release = _slow_refresh(monkeypatch)

    task = asyncio.create_task(deliver("AW-001/T3"))  # 旧 generation 投递
    assert await asyncio.to_thread(entered.wait, 10)  # 已进慢刷新线程
    await _send({"cmd": "unload", "package_id": pid}, host)
    monkeypatch.undo()  # 新代不再慢（在途线程仍持旧慢函数引用）
    await _load_pack(host, sample_pack, deploy)  # 同 package_id 新一代
    release.set()
    await asyncio.wait_for(task, timeout=10)

    response = await _send({"cmd": "status"}, host)
    (gate,) = response["packages"][0]["gates"]
    assert gate["wakes"] == 0  # 新代闸门未被旧触发唤醒
    assert (await _send({"cmd": "episodes"}, host))["episodes"] == []  # 未装配任何剧集


async def test_persist_race_with_approve_leaves_no_stale_snapshot(running_host, sample_pack, deploy_w5, monkeypatch):
    """persist-决定竞态（复审 P1）：快照线程落盘前决定已到、恢复真写——删除世代令牌令迟到落盘弃写，
    磁盘最终态**无快照**（此刻崩溃也不会重挂重批重复写）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    entered, release = _slow_stamp(monkeypatch)

    exec_task = asyncio.create_task(host._execute_episode(episode, loaded, proxy, policy))
    assert await asyncio.to_thread(entered.wait, 10)  # 挂起已登记，persist 线程正算指纹（文件未落）
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    host._sweep_suspensions()  # 触发恢复：delete（此刻文件不存在）→ 世代 +1 → 真写
    release.set()
    await asyncio.wait_for(exec_task, timeout=10)  # persist 完成：令牌失配 → 弃写
    await _await_status(episode, "completed")
    assert episode.status == "completed"
    assert next(s for s in episode.steps if s["step"] == "下发")["status"] == "done"  # 写已兑现
    # 关键断言：迟到快照未落盘——「兑现后崩溃 → 重挂旧 pending → 重批重复写」的窗不存在
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == episode.operation_id] == []


async def test_persist_during_unload_retains_snapshot(running_host, sample_pack, deploy_w5, monkeypatch):
    """persist-unload 竞态（复审 P1）：落盘线程在途时 unload（挂起迁 stopped）——unload **不作废**快照，
    迟到落盘照常保留（活过包重载语义不取决于时序），重载后可重挂。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    entered, release = _slow_stamp(monkeypatch)

    exec_task = asyncio.create_task(host._execute_episode(episode, loaded, proxy, policy))
    assert await asyncio.to_thread(entered.wait, 10)  # persist 线程在途（文件未落）
    await _send({"cmd": "unload", "package_id": pid}, host)  # 挂起迁 stopped；快照语义 = 留盘
    assert episode.status == "stopped"
    monkeypatch.undo()
    release.set()
    await asyncio.wait_for(exec_task, timeout=10)
    # 迟到落盘保留（unload 不 delete）——重载后 _reattach 可重挂兑现
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == episode.operation_id]
    await _load_pack(host, sample_pack, deploy_w5)
    _reapply_write_config(host, pid)
    opid = episode.operation_id
    reattached = next(
        (e for e in host.episodes.values() if e.operation_id == opid and e.status == "suspended_pending_approval"),
        None,
    )
    assert reattached is not None


async def test_reattach_aborts_when_unloaded_during_disk_scan(running_host, sample_pack, deploy_w5, monkeypatch):
    """异步重挂跨代（复审 P1）：重挂读盘/指纹期间包被 unload → 线程返回后代际 CAS 失配 →
    整体放弃，不向台账/_suspensions/挑战库写入任何状态；快照留盘未消费。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)  # 挂起 + 落盘
    opid = episode.operation_id
    await _send({"cmd": "unload", "package_id": pid}, host)  # 快照留盘

    entered, release = _slow_stamp(monkeypatch)
    load_task = asyncio.create_task(_load_pack(host, sample_pack, deploy_w5))  # reload → 重挂读盘慢
    assert await asyncio.to_thread(entered.wait, 10)  # 重挂线程在途
    await _send({"cmd": "unload", "package_id": pid}, host)  # 读盘期间再 unload（注销新一代）
    release.set()
    await asyncio.wait_for(load_task, timeout=10)

    # 未发布孤儿挂起剧集（首轮 unload 留下的 stopped 旧条目照旧，不算发布）
    assert not any(e.operation_id == opid and e.status == "suspended_pending_approval" for e in host.episodes.values())
    assert host._suspensions == {}
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid]  # 快照未消费，留待下次重挂


async def test_status_not_blocked_behind_slow_fire(running_host, sample_pack, deploy, monkeypatch):
    """fire 出全局命令锁（复审 P2）：慢投递（刷新在线程屏障上挂着）进行中，status 立即返回——
    控制通道不再整体排队在慢 fire 之后。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    entered, release = _slow_refresh(monkeypatch)

    fire_task = asyncio.create_task(_send({"cmd": "fire", "package_id": pid, "trigger_id": "AW-001/T3"}, host))
    assert await asyncio.to_thread(entered.wait, 10)  # fire 已进慢投递
    status = await asyncio.wait_for(_send({"cmd": "status"}, host), timeout=5)  # 不被 fire 压住
    assert status["ok"]
    assert not fire_task.done()  # fire 仍在等投递完成（响应语义保持）
    release.set()
    response = await asyncio.wait_for(fire_task, timeout=10)
    assert response["ok"]
    assert len((await _send({"cmd": "episodes"}, host))["episodes"]) == 1  # 投递最终照常发布


# ── GPT Review 三审：fire/stop 生命周期 TOCTOU / 跨代外呼 / 恢复删盘不压事件循环 ──────────


async def test_delivery_aborts_after_draining_and_manual_fire_fails(running_host, sample_pack, deploy, monkeypatch):
    """三审 P1：慢投递期间 stop 到达（DRAINING）→ 投递返回后生命周期 CAS 失效 → 零发布；
    人工 fire 如实报失败（不假报成功）——「任何迟到发布都会看到 tombstone」由此成立。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    calls = []
    proxy = host.proxies[pid]
    original_call = proxy.call
    monkeypatch.setattr(proxy, "call", lambda *a, **k: (calls.append(a), original_call(*a, **k))[1])
    entered, release = _slow_refresh(monkeypatch)

    fire_task = asyncio.create_task(host._fire(pid, "AW-001/T3"))  # 直接调 Host（socket 会随关停拆掉）
    assert await asyncio.to_thread(entered.wait, 10)  # 已进慢刷新线程
    host._begin_draining()  # stop 到达：进入 DRAINING
    release.set()
    response = await asyncio.wait_for(fire_task, timeout=10)

    assert not response["ok"] and "不发布" in response["detail"]  # 失效的人工 fire 报失败
    assert host.episodes == {}  # DRAINING 后零剧集发布
    assert calls == []  # 零 Connector 调用（precondition 从未被求值）


async def test_stale_precondition_uses_own_generation_proxy(running_host, sample_pack, deploy, monkeypatch):
    """三审 P1：旧投递在 precondition 外呼中挂起，期间 unload + 同 id reload——恢复后旧投递只走
    **本代（旧）proxy**（unload 已 revoke，授权层必拒），绝不触碰新代 Connector/binding。"""
    import threading as _threading

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    old_proxy = host.proxies[pid]
    deliver = next(sub.deliver for w in host.table.watchers.values() for sub in w.subs if sub.trigger_id == "AW-001/T3")
    entered, release = _threading.Event(), _threading.Event()
    original_call = old_proxy.call

    def slow_call(*a, **k):  # 旧代 precondition 外呼在此挂起
        entered.set()
        release.wait(timeout=10)
        return original_call(*a, **k)

    monkeypatch.setattr(old_proxy, "call", slow_call)
    task = asyncio.create_task(deliver("AW-001/T3"))
    assert await asyncio.to_thread(entered.wait, 10)  # 旧投递已在 precondition 外呼中
    await _send({"cmd": "unload", "package_id": pid}, host)
    await _load_pack(host, sample_pack, deploy)  # 同 id 新一代
    new_calls = []
    new_proxy = host.proxies[pid]
    new_original = new_proxy.call
    monkeypatch.setattr(new_proxy, "call", lambda *a, **k: (new_calls.append(a), new_original(*a, **k))[1])
    release.set()
    await asyncio.wait_for(task, timeout=10)

    assert new_calls == []  # 旧投递未触碰新代 Connector（本代恒本代，跨代不外呼）
    assert (await _send({"cmd": "episodes"}, host))["episodes"] == []  # 且未发布任何剧集


async def test_stale_watch_poll_does_not_call_new_generation(running_host, sample_pack, deploy, monkeypatch):
    """三审 P1（watch 侧）：旧代 watcher 的 poll 捕获本代 proxy——unload+reload 后调用旧 poll，
    新代 Connector 零调用；旧代已下线，poll 返回 None（本轮不发射）。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    old_poll = next(s.poll for w in host.table.watchers.values() if w.kind == "watch" for s in w.subs)
    assert old_poll is not None

    await _send({"cmd": "unload", "package_id": pid}, host)
    await _load_pack(host, sample_pack, deploy)  # 新一代
    new_calls = []
    new_proxy = host.proxies[pid]
    new_original = new_proxy.call
    monkeypatch.setattr(new_proxy, "call", lambda *a, **k: (new_calls.append(a), new_original(*a, **k))[1])

    result = await asyncio.to_thread(old_poll, "CON-001.拉取费用明细")  # 模拟旧 watcher 在途 tick
    assert result is None  # 旧代已下线：不发射
    assert new_calls == []  # 新代 Connector 零调用（陈旧外呼被堵死）


async def test_resume_delete_runs_off_event_loop(running_host, sample_pack, deploy_w5, monkeypatch):
    """三审 P2：恢复的删盘可能等一次 fsync（与在途 persist 争锁）——现在跑在线程里，
    _sweep_suspensions 同步返回不再携带删盘时长（事件循环不被压住）。"""
    import time as _time

    from osca_host.suspension import SuspensionStore

    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)  # 挂起 + 落盘
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)

    original_delete = SuspensionStore.delete

    def slow_delete(self, opid):  # 模拟存储慢（fsync 争锁/坏盘）
        _time.sleep(0.35)
        original_delete(self, opid)

    monkeypatch.setattr(SuspensionStore, "delete", slow_delete)
    t0 = _time.monotonic()
    host._sweep_suspensions()  # 触发恢复——删盘应已下线程
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.2, f"删盘仍在事件循环上同步执行（sweep 耗时 {elapsed:.3f}s）"
    await _await_status(episode, "completed")
    assert episode.status == "completed"  # 恢复照常兑现


# ── GPT Review 四审：DRAINING 期间恢复不启动写执行 / 重挂清理删盘不压事件循环 ──────────


async def test_resume_aborts_when_draining_during_delete(running_host, sample_pack, deploy_w5, monkeypatch):
    """四审 P1：删盘线程期间进入 DRAINING → 删盘返回后生命周期 CAS 失效 → 不起写执行；
    恢复任务内联 await（自身在 _episode_tasks）+ shutdown 循环重拍快照——STOPPED 时零存活剧集任务。"""
    import threading as _threading

    from osca_host.host import HostState
    from osca_host.suspension import SuspensionStore

    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)  # 挂起 + 落盘
    [ch] = policy.pending_challenges()
    policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)

    entered, release = _threading.Event(), _threading.Event()
    original_delete = SuspensionStore.delete

    def slow_delete(self, opid):
        entered.set()
        release.wait(timeout=10)
        original_delete(self, opid)

    monkeypatch.setattr(SuspensionStore, "delete", slow_delete)
    host._sweep_suspensions()  # 恢复调度：删盘在线程挂起
    assert await asyncio.to_thread(entered.wait, 10)
    host._begin_draining()  # stop 到达（DRAINING）——「进入 DRAINING 后不再启动新工作」
    release.set()
    for _ in range(500):  # 等 fixture 的 run 任务走完 _shutdown
        if host.state is HostState.STOPPED:
            break
        await asyncio.sleep(0.01)

    assert host.state is HostState.STOPPED
    assert host._episode_tasks == set()  # 无逃逸子任务（Host 报 STOPPED 时不许还有活执行）
    assert episode.status == "stopped"  # 未推进 running（关停收尾迁 stopped）
    assert not any(s.get("step") == "下发" for s in episode.steps)  # 零恢复执行、零写


async def test_reattach_cleanup_delete_off_event_loop(running_host, sample_pack, deploy_w5, monkeypatch):
    """四审 P2：重挂丢弃项（过期/损坏/版本失配）的删盘**线程批量**执行——慢 delete（与旧代在途
    persist 争 fsync）期间事件循环心跳照常，status/stop/审批不再被压住。"""
    import threading as _threading

    from osca_host.suspension import SuspensionStore

    host = running_host
    await _load_pack(host, sample_pack, deploy_w5)
    pid = "demo-group-oper-diagnosis"
    episode, loaded, proxy, policy = _setup_write_episode(host, pid)
    await host._execute_episode(episode, loaded, proxy, policy)  # 挂起 + 落盘
    opid = episode.operation_id
    pf = sample_pack / "policy.yaml"  # 未提交改动 → 内容指纹漂移 → 重挂走丢弃清理路径
    pf.write_text(pf.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    await _send({"cmd": "unload", "package_id": pid}, host)

    entered, release = _threading.Event(), _threading.Event()
    original_delete = SuspensionStore.delete

    def slow_delete(self, o):
        entered.set()
        release.wait(timeout=10)
        original_delete(self, o)

    monkeypatch.setattr(SuspensionStore, "delete", slow_delete)
    load_task = asyncio.create_task(_load_pack(host, sample_pack, deploy_w5))  # reload → 重挂 → 清理慢
    assert await asyncio.to_thread(entered.wait, 10)  # 清理删盘已在线程中挂起
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.sleep(0.01)  # 心跳：慢 delete 不压事件循环（旧实现此处死锁/被压 350ms+）
    assert loop.time() - t0 < 0.2
    release.set()
    await asyncio.wait_for(load_task, timeout=15)
    assert [r for r in host._suspension_store.load_all() if r["operation_id"] == opid] == []  # 废快照已清
    assert not any(
        e.operation_id == opid and e.status == "suspended_pending_approval" for e in host.episodes.values()
    )  # 失配快照未被重挂


# ── P1：Aware disable→enable 代际隔离 / binding 装载门禁 / 关停有界退出 ──


async def test_disable_enable_kills_stale_delivery(running_host, sample_pack, deploy, monkeypatch):
    """旧触发进入慢账本刷新后 disable→enable：旧投递返回时必须永久失效,不创建新 Episode（P1）。"""
    import threading

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    real_refresh = host._refresh_ledger

    def slow_refresh(loaded, policy):
        loop.call_soon_threadsafe(entered.set)
        release.wait(10)
        return real_refresh(loaded, policy)

    monkeypatch.setattr(host, "_refresh_ledger", slow_refresh)
    deliver = host._make_deliver(pid, "AW-001")
    task = asyncio.create_task(deliver("AW-001/T3"))
    await asyncio.wait_for(entered.wait(), timeout=5)  # 旧投递已卡在慢刷新（线程）
    host._set_aware(pid, "AW-001", False)
    host._set_aware(pid, "AW-001", True)  # gate/policy/loaded 对象身份全不变——旧 CAS 关不住
    release.set()
    why = await asyncio.wait_for(task, timeout=5)
    assert why is not None and "停用" in why  # 旧代投递按代际失效放弃
    assert host.episodes == {}  # 未创建任何剧集


async def test_disable_clears_partial_gate_progress(running_host, sample_pack, deploy):
    """disable 边界清除 all/sequence 半程状态（P1）——旧代命中不残留到重新启用之后。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    gate = host.gates[(pid, "AW-001")]
    gate._seen.add("AW-001/T3")
    gate._seq = 1
    host._set_aware(pid, "AW-001", False)
    assert gate._seen == set() and gate._seq == 0


async def test_load_without_required_bindings_rejected(running_host, sample_pack):
    """Host 部署装载：包声明 required bindings 却未注入 → 装载失败（P1 装载门禁,不留到首次调用才炸）。"""
    host = running_host
    host.deployments["no-bindings"] = {"path": str(sample_pack), "bindings": None}
    response = await _send({"cmd": "load", "deployment_id": "no-bindings"}, host)
    assert not response["ok"]
    assert any("部署装载必须注入 bindings" in line for line in response["detail"])
    assert host.registry.packages == {}


async def test_load_with_malformed_bindings_rejected(running_host, sample_pack, tmp_path):
    """bindings 顶层非 mapping / 值非 mapping / 缺 endpoint——装载门禁一律拒绝（P1）。"""
    host = running_host
    cases = [
        ("- 1\n- 2\n", "顶层必须是 mapping"),
        ("FINANCE_DB: 一条连接串\n", "值必须是 mapping"),
        ("FINANCE_DB:\n  secret_ref: KEY\n", "缺非空 endpoint"),
    ]
    for i, (content, expect) in enumerate(cases):
        bad = tmp_path / f"bad-bindings-{i}.yaml"
        bad.write_text(content, encoding="utf-8")
        response = await _load_pack(host, sample_pack, bad, did=f"bad-{i}")
        assert not response["ok"], content
        assert any(expect in line for line in response["detail"]), (content, response["detail"])
    assert host.registry.packages == {}


async def test_run_in_daemon_thread_daemon_flag_and_result(sock_path):
    """P1 关停语义：剧集执行线程必须是守护线程（随进程消亡）,结果/异常原样回传。"""
    import threading

    host = Host(sock_path)
    seen: dict[str, bool] = {}

    def probe():
        seen["daemon"] = threading.current_thread().daemon
        return 42

    assert await host._run_in_daemon_thread(probe) == 42
    assert seen["daemon"] is True

    def boom():
        raise RuntimeError("剧集内部错")

    with pytest.raises(RuntimeError, match="剧集内部错"):
        await host._run_in_daemon_thread(boom)


async def test_shutdown_bounded_with_hung_episode_thread(sock_path, caplog):
    """P1：剧集线程卡死时关停仍有界完成——守护线程不阻塞进程退出,STOPPED 不再是假报。"""
    import threading
    import time as time_mod

    host = Host(sock_path)
    host._episode_shutdown_timeout = 0.3
    task = asyncio.create_task(host.run())
    for _ in range(100):
        if host.control.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    release = threading.Event()
    hung = asyncio.create_task(host._run_in_daemon_thread(release.wait, 30))
    host._episode_tasks.add(hung)
    hung.add_done_callback(host._episode_tasks.discard)
    await asyncio.sleep(0)  # 让 hung 任务起线程
    started = time_mod.monotonic()
    host._stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert host.state.name == "STOPPED"
    assert time_mod.monotonic() - started < 5  # 上限 0.3s + 收尾,远小于挂死线程的 30s
    assert "守护线程随进程退出终止" in caplog.text  # 诚实口径:超时留痕
    release.set()  # 释放线程,测试进程干净退出
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(hung, timeout=5)


async def test_enable_disable_status_not_self_contradictory(running_host, sample_pack, deploy):
    """P2：disable/enable 后 status 三处一致——Gate 运行态、AwareDecl.enabled、watcher 槽位不许互相矛盾。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"

    await _send({"cmd": "disable", "package_id": pid, "aware_id": "AW-001"}, host)
    response = await _send({"cmd": "status"}, host)
    (pkg,) = response["packages"]
    (aware,) = pkg["awares"]
    (gate,) = pkg["gates"]
    assert aware["enabled"] is False and gate["enabled"] is False
    assert [w["state"] for w in pkg["watchers"]] == ["disabled"] * 3

    await _send({"cmd": "enable", "package_id": pid, "aware_id": "AW-001"}, host)
    response = await _send({"cmd": "status"}, host)
    (pkg,) = response["packages"]
    (aware,) = pkg["awares"]
    (gate,) = pkg["gates"]
    assert aware["enabled"] is True and gate["enabled"] is True
    assert [w["state"] for w in pkg["watchers"]] == ["armed"] * 3


# ── 复核 P1：订阅代际固化 / STOPPED 后零迟到副作用 / 卡死重活下进程限期退出 ──


async def test_old_subscription_closure_holds_old_generation(running_host, sample_pack, deploy):
    """旧订阅闭包（disable 前创建）在 disable→enable 之后才执行——代际固化于创建时,永久失效。"""
    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    old_deliver = host._make_deliver(pid, "AW-001")  # 装载期创建的旧订阅闭包
    host._set_aware(pid, "AW-001", False)
    host._set_aware(pid, "AW-001", True)
    why = await old_deliver("AW-001/T3")  # 旧闭包此刻才开始执行——已拿不到新代际
    assert why is not None and "永久失效" in why
    assert host.episodes == {}


async def test_queued_old_subscription_after_disable_enable_zero_publish(running_host, sample_pack, deploy):
    """复核 P1 场景复刻：共享 watcher 派发快照里,A 订阅阻塞、B 旧订阅排队;期间 disable→enable;
    旧 B 回调随后才开始执行——必须零发布（代际在订阅创建时固化,不在回调开始时读取）。"""
    from osca_host.triggers import Subscription

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    watcher = next(w for w in host.table.watchers.values() if any(s.trigger_id == "AW-001/T3" for s in w.subs))
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking_deliver(trigger_id):
        entered.set()
        await release.wait()

    watcher.subs.insert(0, Subscription(pid, "AW-000", "AW-000/TX", blocking_deliver))
    fire_task = asyncio.create_task(host.table._fire(watcher))
    await asyncio.wait_for(entered.wait(), timeout=5)  # A 阻塞;B 旧订阅仍在派发快照里排队
    host._set_aware(pid, "AW-001", False)
    host._set_aware(pid, "AW-001", True)  # 新订阅换新代际;旧订阅永久持旧代际
    release.set()
    await asyncio.wait_for(fire_task, timeout=5)  # 旧 B 回调此刻才开始执行
    assert host.episodes == {}  # 零发布
    assert host.gates[(pid, "AW-001")].wakes == 0


async def test_no_late_settle_write_after_stopped(running_host, sample_pack, deploy):
    """复核 P1：STOPPED 后迟到副作用绝不发生——卡在对账之前的线程于 STOPPED 后才继续,零落账。"""
    import threading

    from osca_host.episode import Episode
    from osca_host.settle import settle_episode

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    loaded, proxy = host.registry.packages[pid], host.proxies[pid]
    episode = Episode(
        episode_id="EP-0777",
        package_id=pid,
        aware_id="AW-001",
        fired_trigger="AW-001/T3",
        assembled_at="2026-07-21T09:00:00+08:00",
        then="STR-001",
        budget={},
        context={
            "objects": {
                "OBJ-009": {
                    "object_id": "OBJ-009",
                    "kind": "objective",
                    "optimize": "maximize",
                    "settle": {"uses": "CON-001.拉取费用明细"},
                }
            },
            "judgments": [],
        },
        status="completed",
        steps=[{"step": "寻优", "performer": "optimizer", "status": "done", "output": {"x": 1}}],
    )
    release = threading.Event()

    def late_settle():
        release.wait(10)  # 卡到 STOPPED 之后才继续
        return settle_episode(loaded, proxy, episode)

    late = asyncio.create_task(host._run_in_daemon_thread(late_settle))
    host._episode_tasks.add(late)
    late.add_done_callback(host._episode_tasks.discard)
    await asyncio.sleep(0)
    host._episode_shutdown_timeout = 0.2
    host._stop.set()
    for _ in range(200):
        if host.state.name == "STOPPED":
            break
        await asyncio.sleep(0.05)
    assert host.state.name == "STOPPED"
    cases_before = sorted(p.name for p in (loaded.root / "cases").glob("*.yaml"))
    release.set()  # STOPPED 之后线程才继续跑到对账
    results = await asyncio.wait_for(late, timeout=5)
    assert results and all(not r["settled"] for r in results)  # 迟到对账零落账
    assert sorted(p.name for p in (loaded.root / "cases").glob("*.yaml")) == cases_before
    assert all("包已停" in r["note"] for r in results)  # 授权层/落账门按 revoke 拒绝留痕


async def test_settle_dispatched_on_daemon_thread(running_host, sample_pack, deploy, monkeypatch):
    """复核 P1：settle 不进默认 executor——经统一守护线程模型执行（卡死不阻塞进程退出）。"""
    import threading

    import osca_host.host as host_mod
    from osca_host.episode import Episode

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    loaded, proxy, policy = host.registry.packages[pid], host.proxies[pid], host.policies[pid]
    seen: dict[str, bool] = {}

    def fake_run_episode(episode, *args, **kwargs):
        episode.status = "completed"
        return episode

    def fake_settle(loaded, proxy, episode):
        seen["daemon"] = threading.current_thread().daemon
        return []

    monkeypatch.setattr(host_mod, "run_episode", fake_run_episode)
    monkeypatch.setattr(host_mod, "settle_episode", fake_settle)
    episode = Episode(
        episode_id="EP-0778",
        package_id=pid,
        aware_id="AW-001",
        fired_trigger="AW-001/T3",
        assembled_at="2026-07-21T09:00:00+08:00",
        then="STR-001",
        budget={},
        context={"objects": {}, "judgments": []},
    )
    await host._execute_episode(episode, loaded, proxy, policy)
    assert seen["daemon"] is True


def test_process_exits_within_deadline_with_hung_worker(tmp_path):
    """复核 P1（子进程真实验证）：settle 类重活永久卡死时,进程仍在期限内退出——统一守护线程
    模型下 asyncio.run 收尾不再等默认 executor;若该重活仍在默认 executor,本用例会超时失败。"""
    import shutil as shutil_mod
    import subprocess
    import sys
    import tempfile
    import textwrap
    import time as time_mod

    workdir = tempfile.mkdtemp(prefix="oscah-", dir="/tmp")
    sock = f"{workdir}/h.sock"
    script = textwrap.dedent(
        f"""
        import asyncio, threading
        from pathlib import Path
        from osca_host.host import Host

        async def main():
            host = Host(Path({sock!r}))
            host._episode_shutdown_timeout = 0.2
            task = asyncio.create_task(host.run())
            for _ in range(100):
                if host.control.socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            hung = asyncio.create_task(host._run_in_daemon_thread(threading.Event().wait))  # 永久卡死的重活
            host._episode_tasks.add(hung)
            hung.add_done_callback(host._episode_tasks.discard)
            await asyncio.sleep(0)
            host._stop.set()
            await task
            print("STOPPED", flush=True)

        asyncio.run(main())
        """
    )
    started = time_mod.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    finally:
        shutil_mod.rmtree(workdir, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    assert "STOPPED" in proc.stdout
    assert time_mod.monotonic() - started < 15  # 进程限期退出（卡死线程随进程消亡）


async def test_late_load_worker_never_mutates_dest_after_stopped(sock_path, sample_pack, tmp_path, monkeypatch):
    """三轮复核 P1：load worker 卡在切换前,Host STOPPED 后才释放——作废令牌使其在磁盘写
    副作用（索引/切换）之前止步,dest 必须完全不存在/不变。"""
    import threading

    from osca_cli import packer as packer_mod
    from osca_cli.packer import pack_package

    import osca_host.host as host_mod

    _, zip_path = pack_package(sample_pack, tmp_path / "pack.osca.zip")
    assert zip_path is not None
    dest = tmp_path / "deploy-dest"
    entered, release, worker_done = threading.Event(), threading.Event(), threading.Event()
    real_rebuild = packer_mod.rebuild_index

    def gated_rebuild(root, pkg=None, **kwargs):
        entered.set()
        release.wait(10)  # 卡在校验流水线末步（切换之前）
        return real_rebuild(root, pkg, **kwargs)

    real_lfh = host_mod.load_for_host

    def tracked_lfh(*args, **kwargs):
        try:
            return real_lfh(*args, **kwargs)
        finally:
            worker_done.set()

    monkeypatch.setattr(packer_mod, "rebuild_index", gated_rebuild)
    monkeypatch.setattr(host_mod, "load_for_host", tracked_lfh)
    host = Host(sock_path)
    host.deployments["z"] = {"path": str(zip_path), "dest": str(dest), "bindings": _stub_bindings(sample_pack)}
    run_task = asyncio.create_task(host.run())
    for _ in range(100):
        if host.control.socket_path.exists():
            break
        await asyncio.sleep(0.01)
    load_task = asyncio.create_task(_send({"cmd": "load", "deployment_id": "z"}, host))
    assert await asyncio.to_thread(entered.wait, 5)  # worker 已卡在切换前
    host._stop.set()
    assert await asyncio.wait_for(run_task, timeout=10) == 0
    assert host.state.name == "STOPPED"
    assert not dest.exists()  # STOPPED 时 dest 未被创建
    release.set()  # STOPPED 之后 worker 才继续
    assert await asyncio.to_thread(worker_done.wait, 10)
    assert not dest.exists()  # 迟到 worker 被作废令牌止步:dest 完全不变
    assert not list(tmp_path.glob(f".{dest.name}.osca-tmp-*"))  # 临时目录亦清理
    with contextlib.suppress(Exception):
        await asyncio.wait_for(load_task, timeout=5)


async def test_stopped_within_bound_when_final_commit_hangs(running_host, sample_pack, deploy):
    """四轮复核 P1：settle 的发布 I/O 永久悬挂(在途终局提交不归还)时,Host 仍在承诺时限内
    到达 STOPPED——revoke 不再持锁做文件 I/O,只有界等待在途提交。"""
    import time as time_mod

    host = running_host
    await _load_pack(host, sample_pack, deploy)
    pid = "demo-group-oper-diagnosis"
    policy = host.policies[pid]
    policy.final_commit_grace = 0.2
    ok, _ = policy.begin_final_commit()  # 模拟:对账发布卡死在 fsync/link(永不归还)
    assert ok
    host._episode_shutdown_timeout = 0.2
    started = time_mod.monotonic()
    host._stop.set()
    for _ in range(200):
        if host.state.name == "STOPPED":
            break
        await asyncio.sleep(0.05)
    elapsed = time_mod.monotonic() - started
    assert host.state.name == "STOPPED"
    assert elapsed < 8.0  # 有界关停:悬挂提交明标后放行,不无界等待
    policy.end_final_commit()  # 收尾,不留悬挂计数
