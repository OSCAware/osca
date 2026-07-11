"""触发原语受限语法（SPEC v0.4 草案 §5）：时长、schedule、next_fire、闸门矛盾检查。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from osca_cli.triggers import parse_duration, parse_schedule, validate_gate, validate_trigger

# ── 时长语法 ──


def test_duration_valid():
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("72h") == timedelta(hours=72)
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("45s") == timedelta(seconds=45)


def test_duration_invalid():
    for bad in ("0h", "h", "24", "1.5h", "每天", 24, None, "24H", "1w"):
        assert parse_duration(bad) is None, bad


# ── schedule 解析 ──


def test_schedule_monthly():
    sched, errors = parse_schedule({"every": "month", "day": 9, "time": "09:00"})
    assert errors == []
    assert (sched.every, sched.day, sched.time) == ("month", 9, "09:00")


def test_schedule_free_text_rejected():
    _, errors = parse_schedule("每月9日 09:00")
    assert any("自由文本已废止" in e for e in errors)


def test_schedule_field_errors():
    _, errors = parse_schedule({"every": "year", "day": 0, "time": "9:00", "cron": "x"})
    joined = "\n".join(errors)
    assert "every=year" in joined
    assert "HH:MM" in joined
    assert "未知字段 cron" in joined


def test_schedule_day_field_rules():
    _, errors = parse_schedule({"every": "month", "time": "09:00"})  # month 缺 day
    assert any("1..31" in e for e in errors)
    _, errors = parse_schedule({"every": "week", "day": "monday", "time": "09:00"})  # 须缩写
    assert any("mon/tue" in e for e in errors)
    _, errors = parse_schedule({"every": "day", "day": 3, "time": "09:00"})  # day 不得给
    assert any("不得给 day" in e for e in errors)


def test_schedule_bad_tz():
    _, errors = parse_schedule({"every": "day", "time": "09:00", "tz": "Mars/Olympus"})
    assert any("IANA" in e for e in errors)


# ── next_fire ──


def test_next_fire_monthly():
    sched, _ = parse_schedule({"every": "month", "day": 9, "time": "09:00"})
    assert sched.next_fire(datetime(2026, 7, 11, 12, 0)) == datetime(2026, 8, 9, 9, 0)
    assert sched.next_fire(datetime(2026, 8, 9, 8, 59)) == datetime(2026, 8, 9, 9, 0)  # 当天未到点
    assert sched.next_fire(datetime(2026, 12, 20, 0, 0)) == datetime(2027, 1, 9, 9, 0)  # 跨年


def test_next_fire_month_end_clamp():
    sched, _ = parse_schedule({"every": "month", "day": 31, "time": "09:00"})
    assert sched.next_fire(datetime(2026, 2, 1, 0, 0)) == datetime(2026, 2, 28, 9, 0)  # 2026 非闰年


def test_next_fire_weekly():
    sched, _ = parse_schedule({"every": "week", "day": "mon", "time": "08:30"})
    # 2026-07-11 是周六 → 下周一 07-13
    assert sched.next_fire(datetime(2026, 7, 11, 12, 0)) == datetime(2026, 7, 13, 8, 30)


def test_next_fire_daily():
    sched, _ = parse_schedule({"every": "day", "time": "09:00"})
    assert sched.next_fire(datetime(2026, 7, 11, 10, 0)) == datetime(2026, 7, 12, 9, 0)
    assert sched.next_fire(datetime(2026, 7, 11, 8, 0)) == datetime(2026, 7, 11, 9, 0)


def test_next_fire_with_tz():
    sched, _ = parse_schedule({"every": "day", "time": "09:00", "tz": "Asia/Shanghai"})
    after = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    fire = sched.next_fire(after)
    assert fire == datetime(2026, 7, 12, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert fire.utcoffset() == timedelta(hours=8)


# ── 触发原语校验 ──


def test_trigger_watch_syntax():
    assert validate_trigger({"id": "T2", "kind": "watch", "uses": "CON-001.取数", "every": "24h"}) == []
    errors = validate_trigger({"id": "T2", "kind": "watch", "every": "一天"})
    joined = "\n".join(errors)
    assert "缺少 uses" in joined and "every=一天" in joined


def test_trigger_event_syntax():
    assert validate_trigger({"id": "T3", "kind": "event", "source": "操作者控制台"}) == []
    assert any("缺少 source" in e for e in validate_trigger({"id": "T3", "kind": "event"}))


def test_trigger_unknown_field():
    errors = validate_trigger(
        {"id": "T1", "kind": "schedule", "schedule": {"every": "day", "time": "09:00"}, "cron": "x"}
    )
    assert any("不识别的字段 cron" in e for e in errors)


# ── 闸门编译期矛盾检查 ──


def test_gate_valid():
    gate = {"combine": "any", "precondition": "非空", "debounce": "72h", "on_fail": "顺延重试"}
    assert validate_gate(gate, 3) == []


def test_gate_contradictions():
    assert any("矛盾" in e for e in validate_gate({"combine": "sequence"}, 1))
    assert any("矛盾" in e for e in validate_gate({"combine": "all"}, 1))
    assert any("combine=both" in e for e in validate_gate({"combine": "both"}, 2))
    assert any("debounce" in e for e in validate_gate({"debounce": "三天"}, 2))
    assert any("未知字段" in e for e in validate_gate({"cooldown": "1h"}, 2))
