"""SuspensionStore：挂起快照的原子读写删（fd 锚定）+ 非序列化跳过 + 坏文件跳过（M6-W5-D2b·L2）。"""

from __future__ import annotations

import datetime
import json
import os

import pytest

from osca_host.suspension import SuspensionStore


@pytest.fixture
def store(tmp_path):
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield SuspensionStore(fd), tmp_path
    finally:
        os.close(fd)


def test_persist_load_delete_roundtrip(store):
    s, tmp = store
    rec = {"operation_id": "EO-abc", "package_id": "pkg", "episode": {"episode_id": "EP-0001"}}
    assert s.persist("EO-abc", rec) is True
    assert (tmp / "susp-EO-abc.json").is_file()
    assert s.load_all() == [rec]
    s.delete("EO-abc")
    assert s.load_all() == [] and not (tmp / "susp-EO-abc.json").exists()


def test_persist_atomic_no_tmp_left(store):
    s, tmp = store
    s.persist("EO-x", {"a": 1})
    assert not any(p.name.endswith(".tmp") for p in tmp.iterdir())  # temp 已 rename，无残留


def test_non_serializable_skipped_not_raised(store):
    s, _ = store
    ok = s.persist("EO-date", {"d": datetime.date(2026, 7, 8)})  # 非 JSON 可序列化（YAML 原生 date）
    assert ok is False and s.load_all() == []  # 跳过、不炸、不落盘（该剧集退回 L1）


def test_bad_file_skipped(store):
    s, tmp = store
    (tmp / "susp-EO-bad.json").write_text("{not json", encoding="utf-8")
    (tmp / "susp-EO-ok.json").write_text(json.dumps({"operation_id": "EO-ok"}), encoding="utf-8")
    assert s.load_all() == [{"operation_id": "EO-ok"}]  # 坏文件跳过，好文件读到


def test_non_dict_json_skipped(store):
    """合法 JSON 但非 mapping（null/list/str/number）——跳过，不让 reattach 的 record.get() 崩启动（major-1）。"""
    s, tmp = store
    (tmp / "susp-EO-null.json").write_text("null", encoding="utf-8")
    (tmp / "susp-EO-list.json").write_text("[1, 2]", encoding="utf-8")
    (tmp / "susp-EO-ok.json").write_text(json.dumps({"operation_id": "EO-ok"}), encoding="utf-8")
    assert s.load_all() == [{"operation_id": "EO-ok"}]


def test_ignores_non_suspension_neighbours(store):
    s, tmp = store
    (tmp / "host.sock").write_text("x")  # socket/token/lock 类邻居——不得当快照读
    (tmp / "host.sock.token").write_text("y")
    s.persist("EO-1", {"operation_id": "EO-1"})
    assert [r["operation_id"] for r in s.load_all()] == ["EO-1"]  # 只认 susp-*.json


def test_op_registry_bounded_after_terminal_operations(store):
    """GPT 三审 P2：per-operation 状态（锁/删除世代）以在途凭据引用计数——大量终态 operation 后
    注册表清零，常驻进程无无界增长。"""
    s, _ = store
    for i in range(200):
        opid = f"EO-{i:04x}"
        ticket = s.begin_persist(opid)
        assert s.persist(opid, {"operation_id": opid}, ticket=ticket) is True
        s.delete(opid)
    assert s._ops == {}  # 全部回收


def test_delete_generation_survives_inflight_begin(store):
    """删除世代 tombstone 须活过在途 persist：begin 后 delete，迟到 persist 令牌失配弃写——
    即便中途无其他持票者，条目也不得被提前回收导致世代归零误放行。"""
    s, tmp = store
    ticket = s.begin_persist("EO-race")
    s.delete("EO-race")  # begin 与 persist 之间作废
    assert s.persist("EO-race", {"operation_id": "EO-race"}, ticket=ticket) is False  # 弃写
    assert not (tmp / "susp-EO-race.json").exists()
    assert s._ops == {}  # persist 归还凭据后回收


def test_abandon_persist_releases_credit(store):
    """begin 后未走到 persist（如指纹计算失败）→ abandon 归还凭据，注册表不泄漏。"""
    s, _ = store
    ticket = s.begin_persist("EO-abandon")
    s.abandon_persist(ticket)
    assert s._ops == {}


def test_double_release_cannot_steal_inflight_credit(store):
    """GPT 四审 P2（ABA）：凭据释放幂等——abandon 双调 / persist 自归还后再 abandon，
    绝不偷走并发在途者的票、不提前回收条目、不重置删除世代。"""
    s, tmp = store
    t1 = s.begin_persist("EO-aba")
    s.abandon_persist(t1)
    s.abandon_persist(t1)  # 双 abandon：第二次 no-op
    t2 = s.begin_persist("EO-aba")  # 新在途凭据
    assert s._ops  # t2 的条目还在——没被 t1 的双释放偷走
    s.delete("EO-aba")  # 作废 t2 的令牌
    assert s.persist("EO-aba", {"x": 1}, ticket=t2) is False  # 世代未被重置——迟到 persist 弃写
    assert not (tmp / "susp-EO-aba.json").exists()
    assert s._ops == {}


def test_persist_oserror_then_abandon_does_not_reset_delete_generation(store, monkeypatch):
    """GPT 四审 P2 复现反转：persist 内部 OSError（自归还）→ Host 兜底 abandon（分不清异常位置）→
    并发 delete 持票 + 新一代 begin——旧票的第二次释放不得移除/修改新状态，迟到 persist 不得复活快照。"""
    import osca_host.suspension as susp_mod

    s, tmp = store
    t1 = s.begin_persist("EO-x")
    real_open = os.open

    def boom(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("susp-EO-x"):
            raise OSError("disk full（测试注入）")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(susp_mod.os, "open", boom)
    with pytest.raises(OSError):
        s.persist("EO-x", {"a": 1}, ticket=t1)  # persist 内部炸——finally 已自归还
    monkeypatch.undo()
    s.abandon_persist(t1)  # Host 的兜底调用——幂等 no-op，不偷票

    t2 = s.begin_persist("EO-x")  # 新一代在途凭据
    assert s._ops  # 条目未被旧票双释放提前回收
    s.delete("EO-x")  # delete 持票 + 世代 +1
    assert s.persist("EO-x", {"a": 2}, ticket=t2) is False  # 世代未归零——迟到 persist 弃写，快照不复活
    assert not (tmp / "susp-EO-x.json").exists()
    assert s._ops == {}


def test_released_ticket_cannot_resurrect_snapshot(store):
    """GPT 五审 P2（复现反转）：begin → abandon → delete（新状态世代 +1 后回收）→ 拿旧票 persist——
    旧票持旧状态旧世代（0），不锁使用会在注册表全空时复活快照。须拒绝且零快照。"""
    s, tmp = store
    t1 = s.begin_persist("EO-old")
    s.abandon_persist(t1)  # 旧票 RELEASED
    s.delete("EO-old")  # 新状态：世代 +1 → 回收 → 注册表空
    assert s._ops == {}
    assert s.persist("EO-old", {"a": 1}, ticket=t1) is False  # 已归还的票拒绝使用
    assert not (tmp / "susp-EO-old.json").exists()  # 不建文件——快照未复活
    assert s._ops == {}


def test_same_ticket_cannot_persist_twice(store):
    """GPT 五审 P2：同一票串行用两次——第二次必须拒绝（使用也 exactly-once，不只释放幂等）。"""
    s, tmp = store
    t = s.begin_persist("EO-twice")
    assert s.persist("EO-twice", {"n": 1}, ticket=t) is True
    assert s.persist("EO-twice", {"n": 2}, ticket=t) is False  # 已用过的票拒绝重用
    assert json.loads((tmp / "susp-EO-twice.json").read_text(encoding="utf-8")) == {"n": 1}  # 内容未被第二次覆写
    s.delete("EO-twice")
    assert s._ops == {}


def test_cross_store_ticket_rejected(store, tmp_path):
    """GPT 五审 P2：Store A 的票传给 Store B（同 operation_id）——跨 store 拒绝且不建文件。"""
    s_a, _ = store
    other_dir = tmp_path / "other-store"
    other_dir.mkdir()
    fd = os.open(str(other_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        s_b = SuspensionStore(fd)
        t_a = s_a.begin_persist("EO-cross")
        assert s_b.persist("EO-cross", {"a": 1}, ticket=t_a) is False  # 错店的票不认
        assert not (other_dir / "susp-EO-cross.json").exists()
        assert s_b._ops == {}  # B 店零残留
        s_a.abandon_persist(t_a)  # A 店的票仍可正常归还
        assert s_a._ops == {}
    finally:
        os.close(fd)


def test_abandon_is_noop_on_claimed_ticket(store, monkeypatch):
    """abandon 只作用于 PENDING：persist 认领后（含内部异常自归还后）abandon 一律 no-op——
    用过的票不被洗回可用，也不双计释放。"""
    import osca_host.suspension as susp_mod

    s, tmp = store
    t = s.begin_persist("EO-claimed")
    real_open = os.open

    def boom(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("susp-EO-claimed"):
            raise OSError("disk full（测试注入）")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(susp_mod.os, "open", boom)
    with pytest.raises(OSError):
        s.persist("EO-claimed", {"a": 1}, ticket=t)  # 认领后炸——finally 已归还（CLAIMED→RELEASED）
    monkeypatch.undo()
    s.abandon_persist(t)  # no-op
    assert s.persist("EO-claimed", {"a": 2}, ticket=t) is False  # 终态票不可再用
    assert not (tmp / "susp-EO-claimed.json").exists()
    assert s._ops == {}
