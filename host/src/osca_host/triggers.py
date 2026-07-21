"""Host 组件 2：触发表 —— trigger 原语扁平注册，哈希分发，跨 Aware 去重共享（架构 §4）。

语法解析复用 osca_cli.triggers（单一真理源）。布防语义：
- schedule：定时器，按 next_fire 睡到点发射；纯时间，可跨包去重共享；
- watch：轮询器，按 every 经 poller（Connector 代理）取数，emit_when 命中才发射；
  数据绑定在包上，去重共享只在包内（不同包的 CON-001 可能是不同系统）；
  无 emit_when 时按「状态变化」发射；emit_when 不可求值或取数失败只计 tick 留痕；
  state_key 声明时按该键提取目标状态（缓存/比较/emit_when 求值域都只看它），字段缺失 fail-closed 不发射；
- event：登记不布防，由控制通道人工发射（对应样例 T3「操作者控制台按钮」）。
去重：相同 (kind, spec[, 包域]) 只建一个 watcher，多个订阅共享（引用计数 = 订阅数）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from osca_cli.triggers import parse_duration, parse_schedule

from osca_host.expr import evaluate_emit_when

log = logging.getLogger("osca-host")

# poller(package_id, 接口引用) → 状态负载（dict）或 None（取数失败）。
# 轮询走 Connector 代理可能做真实网络取数——发射循环经 to_thread 调它，绝不在事件循环上阻塞（GPT Review P1）
Poller = Callable[[str, str], object]


@dataclass
class Subscription:
    package_id: str
    aware_id: str
    trigger_id: str  # 全局 ID，如 AW-001/T1
    # 发射回调：deliver(trigger_id) → 闸门裁决。可回协程（Host 的投递把账本刷新/precondition
    # 取数下线程，事件循环不承载阻塞 IO）；同步回调（测试裸用）也接受。协程可回「未发布原因」
    # 字符串（生命周期失效等）——人工 fire 据此如实报失败；watcher 自动发射只记日志。
    deliver: Callable[[str], Awaitable[str | None] | None]
    # watch 轮询回调（Host 注入，捕获**本代** Connector 代理——跨代不外呼，GPT Review 三审 P1）。
    # None 时轮询回落表级 poller（测试裸用）；仍无则只计 tick。
    poll: Callable[[str], object] | None = None


@dataclass
class Watcher:
    key: str
    kind: str
    spec: dict
    scope: str = ""  # watch 的包域（数据绑定在包上）；schedule/event 为空
    subs: list[Subscription] = field(default_factory=list)
    task: asyncio.Task | None = None
    fires: int = 0  # 发射次数
    ticks: int = 0  # watch 轮询 tick 数
    state: object = None  # watch 的上一轮状态（old）
    next_fire: datetime | None = None


def _canonical_key(kind: str, spec: dict, scope: str) -> str:
    payload = json.dumps({"kind": kind, "spec": spec, "scope": scope}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


class TriggerTable:
    def __init__(self, poller: Poller | None = None) -> None:
        self.watchers: dict[str, Watcher] = {}
        self.poller = poller  # 未注入时轮询只计 tick（W4 前的行为，测试仍可裸用）

    # ── 布防与撤防 ────────────────────────────────────────────────────

    def subscribe(self, kind: str, spec: dict, sub: Subscription) -> Watcher:
        scope = sub.package_id if kind == "watch" else ""
        key = _canonical_key(kind, spec, scope)
        watcher = self.watchers.get(key)
        if watcher is None:
            watcher = Watcher(key=key, kind=kind, spec=spec, scope=scope)
            self.watchers[key] = watcher
            try:
                self._arm(watcher)
            except Exception:
                del self.watchers[key]  # 布防失败不留空 watcher（零订阅的僵尸槽位）
                raise
        else:
            log.info(f"触发共享：{sub.trigger_id} 复用 watcher {key}（引用 {len(watcher.subs) + 1}）")
        watcher.subs.append(sub)
        return watcher

    def unsubscribe(self, package_id: str, aware_id: str | None = None) -> list[str]:
        """撤销订阅：包停（aware_id=None）或触发器停（指定 Aware）。引用归零即拆 watcher。"""
        removed: list[str] = []
        for key in list(self.watchers):
            watcher = self.watchers[key]
            keep, drop = [], []
            for s in watcher.subs:
                match = s.package_id == package_id and (aware_id is None or s.aware_id == aware_id)
                (drop if match else keep).append(s)
            if not drop:
                continue
            watcher.subs = keep
            removed.extend(s.trigger_id for s in drop)
            if not keep:
                if watcher.task:
                    watcher.task.cancel()
                del self.watchers[key]
        return removed

    def shutdown(self) -> None:
        for watcher in self.watchers.values():
            if watcher.task:
                watcher.task.cancel()
        self.watchers.clear()

    # ── 发射 ──────────────────────────────────────────────────────────

    async def fire_manual(self, package_id: str, trigger_id: str) -> str | None:
        """人工发射（操作者通道）。仅 event 触发原语可人工发射；返回错误消息或 None。
        投递等到裁决/装配完成才返回——fire 命令的响应语义保持确定（发射即可查台账）。
        投递回「未发布原因」（关停/跨代失效）时如实转错误——失效的人工 fire 不许假报成功（GPT 三审 P1）。"""
        for watcher in self.watchers.values():
            for sub in watcher.subs:
                if sub.package_id == package_id and sub.trigger_id == trigger_id:
                    if watcher.kind != "event":
                        return f"{trigger_id} 是 {watcher.kind} 触发，仅 event 可人工发射"
                    watcher.fires += 1
                    try:
                        result = sub.deliver(sub.trigger_id)
                        if inspect.isawaitable(result):
                            result = await result
                    except Exception as e:
                        log.exception(f"人工发射派发异常：{trigger_id}")
                        return f"发射派发异常：{e}（watcher 存活，详见 Host 日志）"
                    if isinstance(result, str) and result:
                        return f"发射未发布：{result}"
                    return None
        return f"触发原语未布防：{package_id} 的 {trigger_id}"

    async def _fire(self, watcher: Watcher) -> None:
        watcher.fires += 1
        for sub in list(watcher.subs):
            try:
                result = sub.deliver(sub.trigger_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # 订阅方异常各自隔离——一个包的故障不许杀掉共享 watcher 的循环任务、不许殃及同伴
                log.exception(f"派发异常：{sub.trigger_id}（watcher {watcher.key} 继续存活）")

    # ── watcher 编译（装载时；语法已过 lint，此处解析必须成功） ──────

    def _arm(self, watcher: Watcher) -> None:
        if watcher.kind == "schedule":
            watcher.task = asyncio.get_running_loop().create_task(self._schedule_loop(watcher))
        elif watcher.kind == "watch":
            watcher.task = asyncio.get_running_loop().create_task(self._poll_loop(watcher))
        # event：不布防，等人工发射

    async def _schedule_loop(self, watcher: Watcher) -> None:
        schedule, errors = parse_schedule(watcher.spec.get("schedule"))
        if schedule is None:  # lint 已挡；防御性兜底
            log.error(f"watcher {watcher.key} schedule 编译失败：{'; '.join(errors)}")
            return
        while True:
            now = datetime.now().astimezone()
            watcher.next_fire = schedule.next_fire(now)
            log.info(f"定时器 {watcher.key} 下次触发：{watcher.next_fire.isoformat(timespec='seconds')}")
            await asyncio.sleep(max(0.0, (watcher.next_fire - now).total_seconds()))
            await self._fire(watcher)

    async def _poll_loop(self, watcher: Watcher) -> None:
        every = parse_duration(watcher.spec.get("every"))
        if every is None:
            log.error(f"watcher {watcher.key} every 编译失败：{watcher.spec.get('every')}")
            return
        uses = str(watcher.spec.get("uses"))
        emit_when = watcher.spec.get("emit_when")
        state_key = watcher.spec.get("state_key")
        while True:
            await asyncio.sleep(every.total_seconds())
            watcher.ticks += 1
            # 每 tick 从订阅取**本代** poll（Host 注入，捕获本代 proxy——跨代不外呼，GPT 三审 P1）；
            # 无 sub.poll 回落表级 poller（测试裸用）；仍无则只计 tick
            poll = next((s.poll for s in list(watcher.subs) if s.poll is not None), None)
            if poll is None and self.poller is not None:
                poll = lambda u, scope=watcher.scope: self.poller(scope, u)  # noqa: E731
            if poll is None:
                log.info(f"轮询 tick {watcher.key}（{uses}）：poller 未注入，只计 tick（第 {watcher.ticks} 次）")
                continue
            # 取数下线程（GPT Review P1）：poll 经 Connector 代理可能做真实网络取数（urllib timeout 10s）——
            # 在事件循环上同步调它会压住控制通道（status/stop/审批）整整一次外呼的时长。
            # 逐轮异常边界（P1）：一次瞬时异常（网络抖动/代理内部错）不许永久杀死共享 watcher 的循环
            # 任务（watcher 显示存在、实际已死）——记录后继续下一轮；CancelledError（撤防/关停）照常传播。
            try:
                new_state = await asyncio.to_thread(poll, uses)
            except Exception:
                log.exception(f"轮询 {watcher.key}（{uses}）本轮异常，跳过继续（第 {watcher.ticks} 次；watcher 存活）")
                continue
            if new_state is None:
                log.warning(f"轮询 {watcher.key}（{uses}）取数失败，本轮不发射（第 {watcher.ticks} 次）")
                continue
            if state_key is not None:
                # state_key（P1）：按声明键提取目标状态——缓存与比较（含 emit_when 求值域）只看目标
                # 字段，无关字段变化不再误唤醒。键非法/负载非 mapping/字段缺失 → fail-closed 留痕、
                # 不发射、基线不动（不许拿缺字段的负载把状态洗掉）。
                if not isinstance(state_key, str) or not state_key:
                    log.warning(f"轮询 {watcher.key} state_key 非法（{state_key!r}，须非空字符串）——fail-closed 不发射")
                    continue
                if not isinstance(new_state, dict) or state_key not in new_state:
                    log.warning(
                        f"轮询 {watcher.key}（{uses}）state_key「{state_key}」在负载中缺失——"
                        f"fail-closed 不发射（第 {watcher.ticks} 次）"
                    )
                    continue
                new_state = {state_key: new_state[state_key]}
            old_state, watcher.state = watcher.state, new_state
            if old_state is None:
                log.info(f"轮询 {watcher.key}（{uses}）首轮建立基线，不发射")
                continue
            if emit_when is not None:
                verdict = evaluate_emit_when(str(emit_when), old_state, new_state)
                if verdict is None:
                    log.warning(
                        f"轮询 {watcher.key} emit_when 不可求值或字段缺失，本轮不发射（受限形式见 SPEC v0.4 §4）"
                    )
                    continue
                should_fire = verdict
            else:
                should_fire = old_state != new_state  # 无 emit_when：状态变化即发射
            if should_fire:
                log.info(f"轮询 {watcher.key}（{uses}）emit 条件命中，发射")
                await self._fire(watcher)

    # ── 快照 ──────────────────────────────────────────────────────────

    def status(self) -> list[dict]:
        return [
            {
                "key": w.key,
                "kind": w.kind,
                "refs": len(w.subs),
                "fires": w.fires,
                "ticks": w.ticks,
                "next_fire": w.next_fire.isoformat(timespec="seconds") if w.next_fire else None,
                "subscribers": [s.trigger_id for s in w.subs],
            }
            for w in self.watchers.values()
        ]
