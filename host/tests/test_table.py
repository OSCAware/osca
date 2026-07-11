"""触发表：哈希去重共享（引用计数）、schedule/watch 布防、人工发射纪律。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from osca_host.triggers import Subscription, TriggerTable

SCHEDULE_SPEC = {"schedule": {"every": "month", "day": 9, "time": "09:00"}}


def sub(package_id: str, aware_id: str, tid: str, hits: list[str]) -> Subscription:
    return Subscription(package_id, aware_id, tid, hits.append)


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

    assert table.fire_manual("p1", "AW-001/T3") is None
    assert hits == ["AW-001/T3"]

    error = table.fire_manual("p1", "AW-001/T1")
    assert error and "仅 event 可人工发射" in error
    assert table.fire_manual("p1", "AW-001/T9") is not None  # 未布防
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
