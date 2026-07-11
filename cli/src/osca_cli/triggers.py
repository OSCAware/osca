"""触发原语与闸门的受限语法（SPEC v0.4 草案 §5）。

单一真理源：lint 的开发期校验（OSCA041）与 Host 触发表的装载编译期布防
共用本模块——语法只在这里定义一次，两边不写第二份解析。

schedule 受限语法（取代 v0.3 样例中的自由文本「每月9日 09:00」）：
    schedule: {every: month, day: 9, time: "09:00"}           # 每月 9 日 09:00
    schedule: {every: week, day: mon, time: "08:30"}          # 每周一 08:30
    schedule: {every: day, time: "07:00", tz: Asia/Shanghai}  # 每天 07:00（显式时区）

时长语法（watch.every 与 gate.debounce 共用）：<整数><单位>，单位 s/m/h/d，如 24h、72h、30m。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DURATION = re.compile(r"(\d+)([smhd])")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
TIME_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SCHEDULE_KEYS = {"every", "day", "time", "tz"}
TRIGGER_KEYS = {
    "schedule": {"id", "kind", "schedule", "note"},
    "watch": {"id", "kind", "uses", "every", "state_key", "emit_when", "note"},
    "event": {"id", "kind", "source", "note"},
}
GATE_KEYS = {"combine", "precondition", "debounce", "on_fail"}
COMBINE_MODES = {"any", "all", "sequence"}


def parse_duration(value: object) -> timedelta | None:
    """时长受限语法：<整数><单位>，单位 s/m/h/d；0 值与其他写法一律非法。"""
    if not isinstance(value, str):
        return None
    m = DURATION.fullmatch(value.strip())
    if not m or int(m.group(1)) == 0:
        return None
    return timedelta(seconds=int(m.group(1)) * UNIT_SECONDS[m.group(2)])


@dataclass(frozen=True)
class Schedule:
    every: str  # day | week | month
    time: str  # "HH:MM"，24 小时制
    day: int | str | None = None  # month: 1..31；week: mon..sun；day: 不得给
    tz: str | None = None  # IANA 时区名；缺省取 Host 部署环境时区

    def next_fire(self, after: datetime) -> datetime:
        """after 之后的下一次触发时刻。day 超出当月天数时取当月最后一天。"""
        base = after.astimezone(ZoneInfo(self.tz)) if self.tz else after
        hour, minute = (int(x) for x in self.time.split(":"))

        def at(d: date) -> datetime:
            return datetime(d.year, d.month, d.day, hour, minute, tzinfo=base.tzinfo)

        if self.every == "day":
            candidate = at(base.date())
            return candidate if candidate > base else at(base.date() + timedelta(days=1))

        if self.every == "week":
            ahead = (WEEKDAYS.index(self.day) - base.weekday()) % 7
            candidate = at(base.date() + timedelta(days=ahead))
            return candidate if candidate > base else at(candidate.date() + timedelta(days=7))

        # every == "month"
        def monthly(year: int, month: int) -> datetime:
            last = calendar.monthrange(year, month)[1]
            return at(date(year, month, min(int(self.day), last)))

        candidate = monthly(base.year, base.month)
        if candidate > base:
            return candidate
        year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
        return monthly(year, month)


def parse_schedule(spec: object) -> tuple[Schedule | None, list[str]]:
    """解析 schedule 结构化字段；返回 (Schedule 或 None, 人可读错误列表)。"""
    if not isinstance(spec, dict):
        return None, ["schedule 必须是结构化字段 {every, day, time[, tz]}——自由文本已废止（SPEC v0.4 草案 §5）"]
    errors = [f"schedule 含未知字段 {k}（受限语法）" for k in sorted(set(spec) - SCHEDULE_KEYS)]

    every = spec.get("every")
    if every not in ("day", "week", "month"):
        errors.append(f"schedule.every={every} 不在 {{day, week, month}} 中")

    time_ = spec.get("time")
    if not (isinstance(time_, str) and TIME_HHMM.fullmatch(time_)):
        errors.append(f'schedule.time={time_} 须为 24 小时制 HH:MM（如 "09:00"）')

    day = spec.get("day")
    if every == "month" and not (isinstance(day, int) and 1 <= day <= 31):
        errors.append(f"schedule.day={day} 须为 1..31 的整数（every: month）")
    elif every == "week" and day not in WEEKDAYS:
        errors.append(f"schedule.day={day} 须为 {'/'.join(WEEKDAYS)} 之一（every: week）")
    elif every == "day" and day is not None:
        errors.append("every: day 不得给 day 字段")

    tz = spec.get("tz")
    if tz is not None:
        try:
            ZoneInfo(str(tz))
        except Exception:
            errors.append(f"schedule.tz={tz} 不是合法 IANA 时区名（如 Asia/Shanghai）")

    if errors:
        return None, errors
    return Schedule(every=every, time=time_, day=day, tz=tz), []


def validate_trigger(t: dict) -> list[str]:
    """一条触发原语的受限语法校验。kind 枚举本身由 OSCA040 检查，这里查 kind 内语法。"""
    tid = t.get("id", "?")
    kind = t.get("kind")
    allowed = TRIGGER_KEYS.get(kind)
    if allowed is None:
        return []  # 非法 kind 由 OSCA040 报
    errors = [f"[{tid}] 含 {kind} 触发不识别的字段 {k}（受限语法）" for k in sorted(set(t) - allowed)]

    if kind == "schedule":
        _, errs = parse_schedule(t.get("schedule"))
        errors.extend(f"[{tid}] {e}" for e in errs)
    elif kind == "watch":
        if not isinstance(t.get("uses"), str) or not t.get("uses"):
            errors.append(f"[{tid}] watch 触发缺少 uses（CON-xxx.接口名）")
        if parse_duration(t.get("every")) is None:
            errors.append(f"[{tid}] watch.every={t.get('every')} 须为时长语法 <整数><s|m|h|d>（如 24h）")
    elif kind == "event":
        if not t.get("source"):
            errors.append(f"[{tid}] event 触发缺少 source（触发来源说明）")
    return errors


def validate_gate(gate: dict, trigger_count: int) -> list[str]:
    """闸门的编译期矛盾检查（装载时执行；lint 提前到开发期）。"""
    errors = [
        f"gate 含未知字段 {k}（受限语法：combine/precondition/debounce/on_fail）" for k in sorted(set(gate) - GATE_KEYS)
    ]
    combine = gate.get("combine", "any")
    if combine not in COMBINE_MODES:
        errors.append(f"gate.combine={combine} 不在 {sorted(COMBINE_MODES)} 中")
    elif combine in ("all", "sequence") and trigger_count < 2:
        errors.append(f"编译期矛盾：combine={combine} 要求 ≥2 条触发原语，当前只有 {trigger_count} 条")
    if "debounce" in gate and parse_duration(gate.get("debounce")) is None:
        errors.append(f"gate.debounce={gate.get('debounce')} 须为时长语法 <整数><s|m|h|d>（如 72h）")
    return errors
