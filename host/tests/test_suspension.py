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
