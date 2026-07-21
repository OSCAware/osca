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
        token = s.begin_persist(opid)
        assert s.persist(opid, {"operation_id": opid}, token=token) is True
        s.delete(opid)
    assert s._ops == {}  # 全部回收


def test_delete_generation_survives_inflight_begin(store):
    """删除世代 tombstone 须活过在途 persist：begin 后 delete，迟到 persist 令牌失配弃写——
    即便中途无其他持票者，条目也不得被提前回收导致世代归零误放行。"""
    s, tmp = store
    token = s.begin_persist("EO-race")
    s.delete("EO-race")  # begin 与 persist 之间作废
    assert s.persist("EO-race", {"operation_id": "EO-race"}, token=token) is False  # 弃写
    assert not (tmp / "susp-EO-race.json").exists()
    assert s._ops == {}  # persist 归还凭据后回收


def test_abandon_persist_releases_credit(store):
    """begin 后未走到 persist（如指纹计算失败）→ abandon 归还凭据，注册表不泄漏。"""
    s, _ = store
    s.begin_persist("EO-abandon")
    s.abandon_persist("EO-abandon")
    assert s._ops == {}
