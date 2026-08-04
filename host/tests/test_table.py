"""触发表：哈希去重共享（引用计数）、schedule/watch 布防、人工发射纪律。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from osca_host.triggers import Delivery, Subscription, TriggerTable, as_delivery

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

    delivery = await table.fire_manual("p1", "AW-001/T3")  # 人工发射路径：异常转人话错误，不穿透控制通道
    assert delivery.reason is not None and "派发异常" in delivery.reason
    assert delivery.episode_id is None  # 没装配就是没有
    table.shutdown()


async def test_slow_subscriber_does_not_block_fast_or_next_fire():
    release = asyncio.Event()
    fast_called = asyncio.Event()

    async def slow(trigger_id):
        await release.wait()

    async def fast(trigger_id):
        fast_called.set()

    table = TriggerTable()
    watcher = table.subscribe("event", {"source": "op"}, Subscription("slow", "AW-1", "AW-1/T1", slow))
    table.subscribe("event", {"source": "op"}, Subscription("fast", "AW-2", "AW-2/T1", fast))

    await asyncio.wait_for(table._fire(watcher), timeout=0.1)
    await asyncio.wait_for(fast_called.wait(), timeout=0.1)
    await asyncio.wait_for(table._fire(watcher), timeout=0.1)

    release.set()
    table.shutdown()


async def test_busy_lane_coalesces_repeated_ticks_with_log(caplog):
    release = asyncio.Event()

    async def blocked(trigger_id):
        await release.wait()

    table = TriggerTable()
    watcher = table.subscribe("event", {"source": "op"}, Subscription("pkg", "AW-1", "AW-1/T1", blocked))
    with caplog.at_level("INFO", logger="osca-host"):
        await table._fire(watcher)
        await table._fire(watcher)
        await table._fire(watcher)

    lane = next(iter(table._lanes.values()))
    assert lane.pending is True and lane.coalesced == 1
    assert watcher.key in caplog.text and "累计合并 1" in caplog.text
    release.set()
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

    assert await table.fire_manual("p1", "AW-001/T3") == Delivery()  # 裸回调：正常投递、无剧集号
    assert hits == ["AW-001/T3"]

    delivery = await table.fire_manual("p1", "AW-001/T1")
    assert delivery.reason and "仅 event 可人工发射" in delivery.reason
    assert (await table.fire_manual("p1", "AW-001/T9")).reason  # 未布防
    table.shutdown()


async def test_fire_manual_carries_episode_id_and_drops_it_when_unpublished():
    """M8-T3-a 触发表侧契约：deliver 回的 Delivery 带 episode_id 就原样带出；
    回未发布原因时**丢弃 episode_id**（没发布就没有剧集，绝不给假 id）。"""
    table = TriggerTable()

    async def with_episode(trigger_id):
        return Delivery(episode_id="EP-0007")

    async def unpublished(trigger_id):
        # 防御性：即便回调同时给了原因和 id，未发布也一律不许带 id 出去
        return Delivery(reason="包已卸载/重载——跨代投递不发布", episode_id="EP-0009")

    table.subscribe("event", {"source": "op"}, Subscription("p1", "AW-001", "AW-001/T3", with_episode))
    table.subscribe("event", {"source": "op2"}, Subscription("p2", "AW-001", "AW-001/T3", unpublished))

    assert await table.fire_manual("p1", "AW-001/T3") == Delivery(episode_id="EP-0007")
    denied = await table.fire_manual("p2", "AW-001/T3")
    assert denied.episode_id is None and "跨代投递不发布" in denied.reason
    table.shutdown()


async def test_fire_manual_carries_operation_id_alongside_episode_id():
    """M8-T3-b 触发表侧契约：`operation_id` 与 `episode_id` **并列**原样带出（触发表只做管道，
    不重编不补全）；未发布时**两个一起丢弃**——没发布就没有剧集，两个身份都不许漏出去。"""
    table = TriggerTable()

    async def with_both(trigger_id):
        return Delivery(episode_id="EP-0007", operation_id="EO-deadbeef")

    async def unpublished(trigger_id):
        # 防御性：即便回调同时给了原因和两个 id，未发布也一律不许带 id 出去
        return Delivery(reason="包已卸载/重载——跨代投递不发布", episode_id="EP-0009", operation_id="EO-cafe")

    table.subscribe("event", {"source": "op"}, Subscription("p1", "AW-001", "AW-001/T3", with_both))
    table.subscribe("event", {"source": "op2"}, Subscription("p2", "AW-001", "AW-001/T3", unpublished))

    assert await table.fire_manual("p1", "AW-001/T3") == Delivery(episode_id="EP-0007", operation_id="EO-deadbeef")
    denied = await table.fire_manual("p2", "AW-001/T3")
    assert denied.episode_id is None and denied.operation_id is None
    table.shutdown()


async def test_as_delivery_keeps_the_legacy_string_contract():
    """旧契约兼容：非空字符串 = 未发布原因；None/空串 = 正常投递、无剧集号。"""
    assert as_delivery("包已卸载") == Delivery(reason="包已卸载")
    assert as_delivery(None) == Delivery()
    assert as_delivery("") == Delivery()  # 空串不是原因，也不是 id
    assert as_delivery(Delivery(episode_id="EP-0001")) == Delivery(episode_id="EP-0001")
    assert as_delivery("包已卸载").operation_id is None  # 旧形态回值不会凭空长出机器身份


async def test_watcher_auto_fire_ignores_delivery_return_value():
    """回归：watcher 自动发射那条路不看回值——回 Delivery、回字符串、回 None 一视同仁，
    照常派发、watcher 照常存活、fires 照常计数（改动前后逐字同行为）。"""
    table, seen = TriggerTable(), []

    async def returns_delivery(trigger_id):
        seen.append(("delivery", trigger_id))
        return Delivery(reason="包已卸载/重载——跨代投递不发布", episode_id="EP-0001")

    async def returns_str(trigger_id):
        seen.append(("str", trigger_id))
        return "Host 已 DRAINING——迟到投递不发布"

    watcher = table.subscribe("event", {"source": "op"}, Subscription("p1", "AW-001", "AW-001/T3", returns_delivery))
    table.subscribe("event", {"source": "op"}, Subscription("p2", "AW-001", "AW-001/T3", returns_str))

    await table._fire(watcher)
    await asyncio.sleep(0.05)
    assert sorted(seen) == [("delivery", "AW-001/T3"), ("str", "AW-001/T3")]
    assert watcher.fires == 1 and table.watchers  # watcher 存活，未发布原因不影响自动发射循环

    await table._fire(watcher)  # 再来一发照旧
    await asyncio.sleep(0.05)
    assert len(seen) == 4 and watcher.fires == 2
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
