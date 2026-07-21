"""触发表：哈希去重共享（引用计数）、schedule/watch 布防、人工发射纪律。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from osca_host.triggers import Subscription, TriggerTable

SCHEDULE_SPEC = {"schedule": {"every": "month", "day": 9, "time": "09:00"}}


def sub(package_id: str, aware_id: str, tid: str, hits: list[str]) -> Subscription:
    return Subscription(package_id, aware_id, tid, hits.append)


async def test_fire_isolates_subscriber_exceptions():
    """订阅方异常各自隔离：一个包的派发故障不许杀掉共享 watcher、不许殃及同伴。"""
    table, hits = TriggerTable(), []

    def bad(trigger_id):
        raise RuntimeError("订阅方故障（测试注入）")

    watcher = table.subscribe("event", {"source": "op"}, Subscription("p1", "AW-001", "AW-001/T3", bad))
    table.subscribe("event", {"source": "op"}, Subscription("p2", "AW-001", "AW-001/T3", hits.append))
    await table._fire(watcher)
    assert hits == ["AW-001/T3"]  # 同伴照常收到派发
    assert table.watchers  # watcher 存活

    error = await table.fire_manual("p1", "AW-001/T3")  # 人工发射路径：异常转人话错误，不穿透控制通道
    assert error is not None and "派发异常" in error
    table.shutdown()


async def test_arm_failure_leaves_no_empty_watcher(monkeypatch):
    """_arm 失败必须撤掉刚建的 watcher——零订阅的僵尸槽位会永久占住去重键。"""
    import pytest

    table = TriggerTable()

    def boom(watcher):
        raise RuntimeError("arm 失败（测试注入）")

    monkeypatch.setattr(table, "_arm", boom)
    with pytest.raises(RuntimeError):
        table.subscribe("schedule", SCHEDULE_SPEC, sub("p1", "AW-001", "AW-001/T1", []))
    assert table.watchers == {}  # 无空 watcher 残留
    table.shutdown()


async def test_dedup_shares_watcher():
    table, hits = TriggerTable(), []
    table.subscribe("schedule", SCHEDULE_SPEC, sub("p1", "AW-001", "AW-001/T1", hits))
    table.subscribe("schedule", SCHEDULE_SPEC, sub("p2", "AW-002", "AW-002/T1", hits))
    assert len(table.watchers) == 1  # 相同 (kind, spec) 去重共享
    (watcher,) = table.watchers.values()
    assert len(watcher.subs) == 2  # 引用计数 = 2
    table.shutdown()


async def test_unsubscribe_refcounts_down_to_teardown():
    table, hits = TriggerTable(), []
    table.subscribe("schedule", SCHEDULE_SPEC, sub("p1", "AW-001", "AW-001/T1", hits))
    table.subscribe("schedule", SCHEDULE_SPEC, sub("p2", "AW-002", "AW-002/T1", hits))
    (watcher,) = table.watchers.values()
    task = watcher.task

    assert table.unsubscribe("p1") == ["AW-001/T1"]
    assert len(table.watchers) == 1  # 还有引用，watcher 保留
    assert table.unsubscribe("p2") == ["AW-002/T1"]
    assert table.watchers == {}  # 引用归零 → 拆除
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


async def test_schedule_watcher_plans_next_fire():
    table, hits = TriggerTable(), []
    watcher = table.subscribe("schedule", SCHEDULE_SPEC, sub("p1", "AW-001", "AW-001/T1", hits))
    await asyncio.sleep(0.05)  # 让 loop 计算 next_fire
    assert watcher.next_fire is not None
    assert watcher.next_fire > datetime.now().astimezone()
    table.shutdown()


async def test_watch_ticks_but_never_fires():
    table, hits = TriggerTable(), []
    watcher = table.subscribe("watch", {"uses": "CON-001.取数", "every": "1s"}, sub("p1", "AW-001", "AW-001/T2", hits))
    await asyncio.sleep(1.2)
    assert watcher.ticks >= 1  # 轮询在走
    assert hits == []  # emit_when 求值待 W4：只计 tick 不发射
    table.shutdown()


async def test_fire_manual_event_only():
    table, hits = TriggerTable(), []
    table.subscribe("event", {"source": "控制台"}, sub("p1", "AW-001", "AW-001/T3", hits))
    table.subscribe("schedule", SCHEDULE_SPEC, sub("p1", "AW-001", "AW-001/T1", hits))

    assert await table.fire_manual("p1", "AW-001/T3") is None
    assert hits == ["AW-001/T3"]

    error = await table.fire_manual("p1", "AW-001/T1")
    assert error and "仅 event 可人工发射" in error
    assert await table.fire_manual("p1", "AW-001/T9") is not None  # 未布防
    table.shutdown()


async def test_watch_emits_on_state_transition():
    states = iter([{"已关账": False}, {"已关账": False}, {"已关账": True}, {"已关账": True}])
    table = TriggerTable(poller=lambda scope, uses: next(states, {"已关账": True}))
    hits = []
    spec = {"uses": "CON-001.拉取费用明细", "every": "1s", "emit_when": "old.已关账 == false && new.已关账 == true"}
    watcher = table.subscribe("watch", spec, sub("p1", "AW-001", "AW-001/T2", hits))
    await asyncio.sleep(4.6)  # 基线 → 无变化 → 转变发射 → 已关账保持不再发射
    assert hits == ["AW-001/T2"]
    assert watcher.fires == 1
    table.shutdown()


async def test_watch_scoped_per_package():
    table, hits = TriggerTable(), []
    spec = {"uses": "CON-001.取数", "every": "1s"}
    table.subscribe("watch", spec, sub("p1", "AW-001", "AW-001/T2", hits))
    table.subscribe("watch", spec, sub("p2", "AW-001", "AW-001/T2", hits))
    assert len(table.watchers) == 2  # 数据绑定在包上:同 spec 不同包不共享
    table.shutdown()


# ── 轮询异常边界 + state_key（P1）：可控节拍驱动（sleep 换队列,无任意 sleep） ──


def _gated_ticks(monkeypatch):
    """把 _poll_loop 的节拍 sleep 换成可控队列：每 put 一次放行一轮。返回 (队列, 真 sleep)。"""
    import osca_host.triggers as trig_mod

    real_sleep = asyncio.sleep
    ticks: asyncio.Queue = asyncio.Queue()

    async def gated_sleep(seconds):
        await ticks.get()

    monkeypatch.setattr(trig_mod.asyncio, "sleep", gated_sleep)
    return ticks, real_sleep


async def _until(real_sleep, cond, what=""):
    for _ in range(500):
        if cond():
            return
        await real_sleep(0.01)
    raise AssertionError(f"条件未达成：{what}")


async def test_poll_exception_recovers_next_round(monkeypatch):
    """P1：单轮 poll 异常不许永久杀死 watch 循环——记录后继续,恢复轮照常建基线并发射。"""
    ticks, real_sleep = _gated_ticks(monkeypatch)
    states: list[object] = [RuntimeError("瞬时故障"), {"状态": "ok"}, {"状态": "changed"}]

    def poller(scope, uses):
        s = states.pop(0)
        if isinstance(s, Exception):
            raise s
        return s

    table, hits = TriggerTable(poller=poller), []
    watcher = table.subscribe("watch", {"uses": "CON-001.取数", "every": "1s"}, sub("p1", "AW-001", "AW-001/T2", hits))

    ticks.put_nowait(None)  # 第 1 轮：poll 抛错
    await _until(real_sleep, lambda: watcher.ticks == 1, "第 1 轮 tick")
    await real_sleep(0.05)  # 让异常路径走完
    assert not watcher.task.done()  # 修复前：一次异常即结束整个 _poll_loop
    ticks.put_nowait(None)  # 第 2 轮：恢复 → 建基线
    await _until(real_sleep, lambda: watcher.ticks == 2, "第 2 轮 tick")
    ticks.put_nowait(None)  # 第 3 轮：状态变化 → 发射
    await _until(real_sleep, lambda: len(hits) == 1, "恢复后发射")
    assert hits == ["AW-001/T2"] and watcher.fires == 1
    table.shutdown()


async def test_cancellation_still_propagates(monkeypatch):
    """异常边界不许吞 CancelledError：撤防/关停照常拆循环。"""
    table, hits = TriggerTable(poller=lambda scope, uses: {"x": 1}), []
    watcher = table.subscribe("watch", {"uses": "CON-001.取数", "every": "1s"}, sub("p1", "AW-001", "AW-001/T2", hits))
    task = watcher.task
    table.shutdown()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


async def test_state_key_only_target_field_compared(monkeypatch):
    """P1：声明 state_key 后只比较目标字段——无关字段变化不误唤醒;字段缺失 fail-closed 不发射、基线不动。"""
    ticks, real_sleep = _gated_ticks(monkeypatch)
    states = [
        {"状态": "运行", "心跳": 1},  # 基线
        {"状态": "运行", "心跳": 2},  # 无关字段变化 → 不发射（修复前整包比较会误唤醒）
        {"心跳": 3},  # 目标字段缺失 → fail-closed 不发射、基线不动
        {"状态": "停机", "心跳": 4},  # 目标字段变化 → 发射
    ]
    table, hits = TriggerTable(poller=lambda scope, uses: states.pop(0)), []
    spec = {"uses": "CON-001.取状态", "every": "1s", "state_key": "状态"}
    watcher = table.subscribe("watch", spec, sub("p1", "AW-001", "AW-001/T2", hits))

    for round_no in (1, 2, 3):
        ticks.put_nowait(None)
        await _until(real_sleep, lambda n=round_no: watcher.ticks == n, f"第 {round_no} 轮 tick")
        await real_sleep(0.05)
        assert hits == [], f"第 {round_no} 轮不应发射"
    ticks.put_nowait(None)
    await _until(real_sleep, lambda: len(hits) == 1, "目标字段变化发射")
    assert watcher.fires == 1
    assert watcher.state == {"状态": "停机"}  # 缓存的是提取后的目标状态
    table.shutdown()


async def test_state_key_with_emit_when_on_target_field(monkeypatch):
    """state_key + emit_when：emit_when 在提取后的目标状态域上求值。"""
    ticks, real_sleep = _gated_ticks(monkeypatch)
    states = [
        {"状态": "运行", "噪音": 1},
        {"状态": "运行", "噪音": 2},  # emit_when 不命中
        {"状态": "停机", "噪音": 3},  # 命中 → 发射
    ]
    table, hits = TriggerTable(poller=lambda scope, uses: states.pop(0)), []
    spec = {
        "uses": "CON-001.取状态",
        "every": "1s",
        "state_key": "状态",
        "emit_when": "old.状态 != 停机 && new.状态 == 停机",
    }
    watcher = table.subscribe("watch", spec, sub("p1", "AW-001", "AW-001/T2", hits))
    for _ in range(2):
        ticks.put_nowait(None)
    await _until(real_sleep, lambda: watcher.ticks == 2, "前两轮")
    await real_sleep(0.05)
    assert hits == []
    ticks.put_nowait(None)
    await _until(real_sleep, lambda: len(hits) == 1, "emit_when 命中发射")
    table.shutdown()
