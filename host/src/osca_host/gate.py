"""Host 组件 3：闸门 —— 触发 ≠ 唤醒，闸门裁决（架构 §4）。

组合语义按 SPEC v0.4 草案 §3。precondition 求值器由 Host 注入（走 Connector 代理，
可求值形式见 SPEC v0.4 草案 §4）：求值 False 拦截唤醒并复述 on_fail 声明；
不可求值（None）保守默认放行、留痕。on_fail 的顺延重试执行归对账/重试机制（W5 后）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from osca_cli.triggers import parse_duration

from osca_host.loader import AwareDecl

# precondition_eval(声明文本) → (True 放行 / False 拦截 / None 不可求值, 人可读说明)
PreconditionEval = Callable[[str], "tuple[bool | None, str]"]


class Gate:
    def __init__(self, package_id: str, aware: AwareDecl, precondition_eval: PreconditionEval | None = None):
        self.package_id = package_id
        self.aware = aware
        self.trigger_ids = [t.trigger_id for t in aware.triggers]
        self.combine = aware.gate.get("combine", "any")
        self.debounce = parse_duration(aware.gate.get("debounce")) if aware.gate.get("debounce") else None
        self.precondition = aware.gate.get("precondition")
        self.precondition_eval = precondition_eval
        self.precondition_blocked = 0
        self.enabled = aware.enabled
        self.wakes = 0
        self.debounced = 0
        self.last_wake: datetime | None = None
        self._seen: set[str] = set()  # combine=all 的已命中集合
        self._seq = 0  # combine=sequence 的推进指针

    def reset_progress(self) -> None:
        """清除组合闸门的部分推进状态（all 已见集合 / sequence 指针）——触发器停（disable）边界调用：
        半程状态不得跨越「停用→重新启用」残留，否则旧代命中 + 新代命中会拼成一次假唤醒（P1）。"""
        self._seen.clear()
        self._seq = 0

    def on_trigger(self, trigger_id: str) -> tuple[bool, str]:
        """触发命中 → (是否唤醒, 人可读裁决说明)。"""
        if not self.enabled:
            return False, "抑制：Aware 已停（触发器停）"

        if self.combine == "all":
            self._seen.add(trigger_id)
            if not self._seen.issuperset(self.trigger_ids):
                return False, f"闸门等待：all 已见 {len(self._seen)}/{len(self.trigger_ids)}"
            self._seen.clear()
        elif self.combine == "sequence":
            if trigger_id == self.trigger_ids[self._seq]:
                self._seq += 1
                if self._seq < len(self.trigger_ids):
                    return False, f"闸门推进：sequence {self._seq}/{len(self.trigger_ids)}"
                self._seq = 0
            else:
                # 乱序即重置；乱序命中的恰是首位则视为新序列开始（SPEC v0.4 §3）
                self._seq = 1 if trigger_id == self.trigger_ids[0] else 0
                return False, "闸门重置：sequence 乱序"

        now = datetime.now().astimezone()
        if self.debounce and self.last_wake and now - self.last_wake < self.debounce:
            self.debounced += 1
            return False, f"debounce 抑制（窗口 {self.aware.gate.get('debounce')}，第 {self.debounced} 次）"

        note = ""
        if self.precondition:
            if self.precondition_eval is None:
                note = "precondition 未求值（评估器未注入），默认放行；"
            else:
                verdict, detail = self.precondition_eval(self.precondition)
                if verdict is False:
                    self.precondition_blocked += 1
                    on_fail = self.aware.gate.get("on_fail", "（无 on_fail 声明）")
                    return False, f"precondition 拦截：{detail}。on_fail 声明：{on_fail}"
                note = f"precondition {detail}；"
        self.wakes += 1
        self.last_wake = now
        return True, f"{note}唤醒 → 装配 {self.aware.then}"

    def snapshot(self) -> dict:
        return {
            "aware_id": self.aware.aware_id,
            "enabled": self.enabled,
            "combine": self.combine,
            "wakes": self.wakes,
            "debounced": self.debounced,
            "precondition_blocked": self.precondition_blocked,
            "last_wake": self.last_wake.isoformat(timespec="seconds") if self.last_wake else None,
        }
