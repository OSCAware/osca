"""Host 组件 3：闸门 —— 触发 ≠ 唤醒，闸门裁决（架构 §4）。

组合语义按 SPEC v0.4 草案 §3。W2 边界（诚实标注）：
- precondition 求值需要 Connector 代理（W4），本周记录声明、默认放行并打日志；
- 过闸后的「唤醒」是 W3 剧集装配器的活，本周唤醒 = 计数 + 日志。
"""

from __future__ import annotations

from datetime import datetime

from osca_cli.triggers import parse_duration

from osca_host.loader import AwareDecl


class Gate:
    def __init__(self, package_id: str, aware: AwareDecl):
        self.package_id = package_id
        self.aware = aware
        self.trigger_ids = [t.trigger_id for t in aware.triggers]
        self.combine = aware.gate.get("combine", "any")
        self.debounce = parse_duration(aware.gate.get("debounce")) if aware.gate.get("debounce") else None
        self.precondition = aware.gate.get("precondition")
        self.enabled = aware.enabled
        self.wakes = 0
        self.debounced = 0
        self.last_wake: datetime | None = None
        self._seen: set[str] = set()  # combine=all 的已命中集合
        self._seq = 0  # combine=sequence 的推进指针

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

        note = "precondition 未求值（W4 Connector 后接管），默认放行；" if self.precondition else ""
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
            "last_wake": self.last_wake.isoformat(timespec="seconds") if self.last_wake else None,
        }
