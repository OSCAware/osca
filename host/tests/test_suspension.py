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


# ── 目录 fsync（P1）：rename/unlink 后同步目录,断电不回滚落名/删除 ──


def test_persist_fsyncs_directory_after_rename(store, monkeypatch):
    s, _ = store
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    assert s.persist("EO-dsync", {"operation_id": "EO-dsync"}) is True
    assert s._fd in synced  # rename 后目录已 fsync（否则断电 rename 可丢失）


def test_delete_fsyncs_directory_after_unlink(store, monkeypatch):
    s, _ = store
    s.persist("EO-del", {"operation_id": "EO-del"})
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    s.delete("EO-del")
    assert s._fd in synced  # unlink 后目录已 fsync（否则断电删除回滚 → 旧快照复活重批重复写）


def test_persist_dir_fsync_failure_raises_and_releases_ticket(store, monkeypatch):
    """目录 fsync 失败 → OSError 上抛（调用方按落盘失败退回 L1,不假报持久）;凭据须归还、注册表回收。"""
    s, _ = store
    real_fsync = os.fsync

    def fail_on_dir(fd):
        if fd == s._fd:
            raise OSError("模拟目录 fsync 失败")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_on_dir)
    with pytest.raises(OSError):
        s.persist("EO-fsf", {"operation_id": "EO-fsf"})
    assert s._ops == {}  # finally 已归还凭据,无泄漏条目


def test_delete_dir_fsync_failure_raises_and_releases_ticket(store, monkeypatch):
    """delete 的目录 fsync 失败 → OSError 上抛——调用方保留挂起态待重试,不带着「删没删成不确定」推进真写。"""
    s, _ = store
    s.persist("EO-dff", {"operation_id": "EO-dff"})
    real_fsync = os.fsync

    def fail_on_dir(fd):
        if fd == s._fd:
            raise OSError("模拟盘故障")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_on_dir)
    with pytest.raises(OSError):
        s.delete("EO-dff")
    assert s._ops == {}


def test_persist_dir_fsync_failure_reclaims_renamed_snapshot(store, monkeypatch):
    """复核 P2（durability-unknown）：rename 成功、目录 fsync 失败——已落名快照必须被收回，
    否则「退回 L1、不活过重启」是谎话（load_all 会把它当可恢复快照重挂）。"""
    s, tmp = store
    calls = {"dir": 0}
    real_fsync = os.fsync

    def fail_first_dir_fsync(fd):
        if fd == s._fd:
            calls["dir"] += 1
            if calls["dir"] == 1:
                raise OSError("模拟目录 fsync 失败（rename 已成）")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_dir_fsync)
    with pytest.raises(OSError):
        s.persist("EO-du", {"operation_id": "EO-du"})
    assert not (tmp / "susp-EO-du.json").exists()  # 已落名文件被收回
    assert s.load_all() == []  # 下一次读盘不会复活该快照
    assert s._ops == {}  # 凭据已归还、注册表回收


def test_persist_persistent_storage_fault_still_no_resurrection(store, monkeypatch):
    """收回 unlink 成功、但删除耐久性未知（收回后的目录 fsync 也失败）：崩溃后快照可复活——
    进入显式存储故障态（三轮复核 P2）,不得按 L1 继续运行。"""
    s, tmp = store
    calls = {"dir": 0}
    real_fsync = os.fsync

    def fail_from_second_dir_fsync(fd):
        if fd == s._fd:
            calls["dir"] += 1
            if calls["dir"] >= 2:  # intent 建立成功;post-rename 与收回后的 fsync 持续失败
                raise OSError("持续存储故障")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_from_second_dir_fsync)
    with pytest.raises(OSError):
        s.persist("EO-df", {"operation_id": "EO-df"})
    assert not (tmp / "susp-EO-df.json").exists()  # unlink 成功即收回（fsync 失败不阻止收回）
    assert (tmp / "susp-EO-df.json.intent").exists()  # intent 保留——崩溃复活也按可疑跳过（五轮 P1）
    assert s.storage_fault is not None and "耐久性未知" in s.storage_fault  # 显式降级,非静默 L1
    monkeypatch.undo()
    assert s.load_all() == []  # 故障态禁止重挂
    assert s._ops == {}


def test_reclaim_unlink_failure_enters_storage_fault(store, monkeypatch):
    """三轮复核 P2：目录 fsync 失败且收回 unlink 也失败——盘上留孤本,不得再宣称「退回 L1」：
    进入显式存储故障态,persist/重挂全停（fail-closed,要求运维修复）。"""
    s, tmp = store
    calls = {"dir": 0}
    real_fsync, real_unlink = os.fsync, os.unlink

    def fail_dir_fsync(fd):
        if fd == s._fd:
            calls["dir"] += 1
            if calls["dir"] >= 2:  # intent 建立成功;post-rename 起持续失败
                raise OSError("目录 fsync 失败")
        return real_fsync(fd)

    def fail_susp_unlink(name, *args, **kwargs):
        if str(name).startswith("susp-") and str(name).endswith(".json"):
            raise OSError("unlink 失败（收回不了）")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)
    monkeypatch.setattr(os, "unlink", fail_susp_unlink)
    with pytest.raises(OSError):
        s.persist("EO-sf", {"operation_id": "EO-sf"})
    assert s.storage_fault is not None and "收回失败" in s.storage_fault  # 显式故障态
    assert (tmp / "susp-EO-sf.json").exists()  # 孤本确实在盘上——正因如此必须降级
    monkeypatch.undo()
    assert s.load_all() == []  # 故障态禁止重挂:孤本不复活
    assert s.persist("EO-next", {"operation_id": "EO-next"}) is False  # 故障态拒绝新持久化
    assert not (tmp / "susp-EO-next.json").exists()
    assert s._ops == {}  # 凭据仍 exactly-once 归还


def test_storage_fault_marker_survives_restart(store, monkeypatch, tmp_path):
    """四轮复核 P2：故障态落盘 FAULT_MARKER——重启（同目录新建 store）后故障态仍在,孤本不复活;
    运维删除标记后方可恢复。"""
    from osca_host.suspension import FAULT_MARKER, SuspensionStore

    s, tmp = store
    calls = {"dir": 0}
    real_fsync, real_unlink = os.fsync, os.unlink

    def fail_dir_fsync(fd):
        if fd == s._fd:
            calls["dir"] += 1
            if calls["dir"] >= 2:  # intent 建立成功;post-rename 起持续失败
                raise OSError("目录 fsync 失败")
        return real_fsync(fd)

    def fail_susp_unlink(name, *args, **kwargs):
        if str(name).startswith("susp-") and str(name).endswith(".json"):
            raise OSError("unlink 失败")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)
    monkeypatch.setattr(os, "unlink", fail_susp_unlink)
    with pytest.raises(OSError):
        s.persist("EO-orphan", {"operation_id": "EO-orphan"})
    monkeypatch.undo()
    assert s.storage_fault is not None
    assert (tmp / "susp-EO-orphan.json").exists()  # 孤本在盘
    assert (tmp / FAULT_MARKER).exists()  # 故障标记已落盘

    fd2 = os.open(str(tmp), os.O_RDONLY | os.O_DIRECTORY)  # 等价进程重启:同目录新建 store
    try:
        fresh = SuspensionStore(fd2)
        assert fresh.storage_fault is not None and "遗留存储故障标记" in fresh.storage_fault
        assert fresh.load_all() == []  # 重启不洗白:孤本不复活
        assert fresh.persist("EO-again", {"operation_id": "EO-again"}) is False
    finally:
        os.close(fd2)

    (tmp / FAULT_MARKER).unlink()  # 运维修复存储并显式清除标记
    fd3 = os.open(str(tmp), os.O_RDONLY | os.O_DIRECTORY)
    try:
        healthy = SuspensionStore(fd3)
        assert healthy.storage_fault is None  # 显式确认后方可恢复
        # 五轮复核 P1：只删 marker **不会**复活孤本——孤本带未了结的 write-ahead intent,按可疑跳过。
        # 崩溃安全由 rename 前耐久建立的 intent 承担,不再依赖 marker 自身的耐久性。
        assert (tmp / "susp-EO-orphan.json").exists() and (tmp / "susp-EO-orphan.json.intent").exists()
        assert healthy.load_all() == []
    finally:
        os.close(fd3)


# ── write-ahead intent 联锁（五轮复核 P1/P2/P3） ──


def test_normal_persist_leaves_no_intent(store):
    s, tmp = store
    assert s.persist("EO-clean", {"operation_id": "EO-clean"}) is True
    assert not (tmp / "susp-EO-clean.json.intent").exists()  # 生命周期证毕:intent 已耐久移除
    assert s.load_all() == [{"operation_id": "EO-clean"}]
    s.delete("EO-clean")
    assert not any(p.name.endswith(".intent") for p in tmp.iterdir())


def test_intent_fsync_failure_aborts_before_any_snapshot(store, monkeypatch):
    """intent 自身建不耐久（第 1 次目录 fsync 失败）→ rename 之前干净中止:零快照、零残留、不降级。"""
    s, tmp = store
    calls = {"dir": 0}
    real_fsync = os.fsync

    def fail_first_dir_fsync(fd):
        if fd == s._fd:
            calls["dir"] += 1
            if calls["dir"] == 1:
                raise OSError("intent 目录 fsync 失败")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_dir_fsync)
    with pytest.raises(OSError):
        s.persist("EO-ia", {"operation_id": "EO-ia"})
    assert not (tmp / "susp-EO-ia.json").exists()  # rename 未发生
    assert not (tmp / "susp-EO-ia.json.intent").exists()  # 干净中止,intent 一并撤
    assert s.storage_fault is None
    assert s._ops == {}


def test_orphan_with_intent_skipped_even_without_marker(tmp_path):
    """五轮复核 P1 核心：孤本压制**不依赖 marker 耐久性**——盘上只有孤本+intent（marker 因存储
    故障没写成/丢失）,新 store 照样按可疑跳过,不重挂。"""
    (tmp_path / "susp-EO-o.json").write_text('{"operation_id": "EO-o"}', encoding="utf-8")
    (tmp_path / "susp-EO-o.json.intent").write_text("pending", encoding="utf-8")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fresh = SuspensionStore(fd)
        assert fresh.storage_fault is None  # 无 marker——但联锁不靠它
        assert fresh.load_all() == []  # intent 尚存 → 可疑跳过
        assert (tmp_path / "susp-EO-o.json").exists()  # 只跳过不删——留给运维核对
    finally:
        os.close(fd)


def test_stale_intent_without_snapshot_cleaned(store):
    """崩于 rename 之前:盘上只剩 intent——load_all 清理且不炸、不影响正常快照。"""
    s, tmp = store
    (tmp / "susp-EO-ghost.json.intent").write_text("pending", encoding="utf-8")
    s.persist("EO-live", {"operation_id": "EO-live"})
    assert s.load_all() == [{"operation_id": "EO-live"}]
    assert not (tmp / "susp-EO-ghost.json.intent").exists()  # stale intent 已清理


def test_persist_refused_when_fault_entered_during_file_ops(store, monkeypatch):
    """五轮复核 P2：文件操作期间另一线程进故障态——写后复核收回快照（intent 保留）,不返回 True。"""
    s, tmp = store
    real_rename = os.rename

    def rename_then_fault(src, dst, **kwargs):
        result = real_rename(src, dst, **kwargs)
        if str(dst).startswith("susp-EO-race"):
            s._enter_storage_fault("并发操作故障（测试注入:rename 后、复核前进故障态）")
        return result

    monkeypatch.setattr(os, "rename", rename_then_fault)
    assert s.persist("EO-race", {"operation_id": "EO-race"}) is False  # 不得宣称持久成功
    monkeypatch.undo()
    assert not (tmp / "susp-EO-race.json").exists()  # 已收回
    assert (tmp / "susp-EO-race.json.intent").exists()  # intent 保留(重启按可疑)
    assert s.load_all() == []  # 故障态禁止重挂
    assert s._ops == {}


def test_load_all_invalidated_when_fault_enters_during_scan(store, monkeypatch):
    """五轮复核 P2：扫描期间进故障态——本次结果整体作废,不半截可信。"""
    s, tmp = store
    s.persist("EO-scan", {"operation_id": "EO-scan"})
    real_open = os.open

    def open_then_fault(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if isinstance(path, str) and path == "susp-EO-scan.json":
            s._enter_storage_fault("扫描期间故障（测试注入）")
        return fd

    monkeypatch.setattr(os, "open", open_then_fault)
    assert s.load_all() == []  # 扫描后复核:整体作废
    monkeypatch.undo()


def test_binary_fault_marker_enters_fault_not_crash(tmp_path):
    """六项复核 P3：marker 内容非 UTF-8——构造器不许炸(UnicodeDecodeError),按不可读标记进故障态。"""
    from osca_host.suspension import FAULT_MARKER

    (tmp_path / FAULT_MARKER).write_bytes(b"\xff\xfe\x00binary")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fresh = SuspensionStore(fd)  # 不抛异常
        assert fresh.storage_fault is not None and "读取失败" in fresh.storage_fault
        assert fresh.load_all() == []
    finally:
        os.close(fd)
