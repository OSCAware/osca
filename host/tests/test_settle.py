"""对账器 settle：objective 型自动落 outcome case——现实是第二位专家（公理 A2）。"""

from __future__ import annotations

import shutil

import pytest
import yaml

from osca_host.connector import ConnectorProxy
from osca_host.episode import Episode
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats
from osca_host.settle import settle_episode

OBJECTIVE = {
    "object_id": "OBJ-009",
    "kind": "objective",
    "optimize": "maximize",
    "settle": {"uses": "CON-001.拉取费用明细", "when": "闭店后"},
}


@pytest.fixture
def loaded(sample_pack, tmp_path):
    """样例包的 tmp 副本——对账会往 cases/ 追加账本，不许写回仓库。"""
    work = tmp_path / "pack"
    shutil.copytree(sample_pack, work)
    _, pkg = load_for_host(work)
    return pkg


@pytest.fixture
def proxy(loaded, tmp_path):
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))
    fixtures = tmp_path / "con-fixtures"
    fixtures.mkdir()
    (fixtures / "拉取费用明细.yaml").write_text(
        yaml.safe_dump({"实际售罄率": 0.97, "折后毛利": 8123}, allow_unicode=True), encoding="utf-8"
    )
    bindings = {"FINANCE_DB": {"endpoint": f"mock://{fixtures}", "secret_ref": "FINANCE_DB_RO_KEY"}}
    return ConnectorProxy(loaded, bindings, policy)


def _episode(objects: dict) -> Episode:
    return Episode(
        episode_id="EP-0001",
        package_id="demo-group-oper-diagnosis",
        aware_id="AW-001",
        fired_trigger="AW-001/T3",
        assembled_at="2026-07-11T09:00:00+08:00",
        then="STR-001",
        budget={},
        context={"objects": objects, "judgments": [{"judgment_id": "J-0417"}]},
        status="completed",
        steps=[{"step": "寻优", "performer": "optimizer", "status": "done", "output": {"selected": {"方案": "B"}}}],
    )


def test_objective_settles_into_outcome_case(loaded, proxy):
    episode = _episode({"OBJ-009": OBJECTIVE})
    (result,) = settle_episode(loaded, proxy, episode)

    # 样例包 cases 到 C-0102 为止 → 顺延 C-0103（账本只追加）
    assert result["settled"] is True and result["case"] == "C-0103"
    case = yaml.safe_load((loaded.root / "cases" / "C-0103.yaml").read_text(encoding="utf-8"))
    assert case["kind"] == "outcome"
    assert case["capture_source"].startswith("对账器")
    assert case["input"]["当时生效判断集"] == ["J-0417"]
    assert case["outcome"]["decision"] == {"selected": {"方案": "B"}}  # 剧集的决策产出
    assert case["outcome"]["reality"]["实际售罄率"] == 0.97  # 经代理取的现实数据
    assert case["outcome"]["when_declared"] == "闭店后"
    assert case["distillation"]["status"] == "pending"  # 交蒸馏管道（M3）
    assert episode.settlements == [result]


def test_case_numbering_appends(loaded, proxy):
    settle_episode(loaded, proxy, _episode({"OBJ-009": OBJECTIVE}))
    (second,) = settle_episode(loaded, proxy, _episode({"OBJ-009": OBJECTIVE}))
    assert second["case"] == "C-0104"


def test_free_text_settle_is_conservative(loaded, proxy):
    free_text = dict(OBJECTIVE, settle="闭店后经 CON-001 对账")  # 自由文本，非受限形式
    (result,) = settle_episode(loaded, proxy, _episode({"OBJ-009": free_text}))
    assert result["settled"] is False and "保守不执行" in result["note"]
    assert not (loaded.root / "cases" / "C-0103.yaml").exists()


def test_non_objective_objects_are_skipped(loaded, proxy):
    results = settle_episode(loaded, proxy, _episode({"OBJ-001": {"object_id": "OBJ-001", "kind": "artifact"}}))
    assert results == []  # 对账只属闭环场景


def test_fetch_failure_leaves_trace_without_case(loaded):
    policy = PolicyInterceptor(loaded.package_id, {}, {"confirmed": 0, "overruled": 0})
    proxy = ConnectorProxy(loaded, {}, policy)  # 无 binding → 取数必败
    (result,) = settle_episode(loaded, proxy, _episode({"OBJ-009": OBJECTIVE}))
    assert result["settled"] is False and "对账取数失败" in result["note"]
    assert not (loaded.root / "cases" / "C-0103.yaml").exists()


def test_settle_no_overwrite_on_number_collision(loaded, proxy):
    """无覆盖发布：编号被对手先占（完整文件）→ 顺移下一号，绝不截断他人内容。"""
    rival = loaded.root / "cases" / "C-0103.yaml"
    rival.write_text("case_id: C-0103\n# 对手的完整内容\n", encoding="utf-8")
    (result,) = settle_episode(loaded, proxy, _episode({"OBJ-009": OBJECTIVE}))
    assert result["case"] == "C-0104"  # 撞号顺移
    assert "对手的完整内容" in rival.read_text(encoding="utf-8")  # 原文件原样
    published = yaml.safe_load((loaded.root / "cases" / "C-0104.yaml").read_text(encoding="utf-8"))
    assert published["case_id"] == "C-0104"  # 内容里的编号与文件名一致（重试后重写）


def test_settle_collision_after_scan_hits_retry_branch(loaded, proxy, monkeypatch):
    """撞号重试真分支（十四轮）：对手在编号扫描后、首次 link 前落 C-0103——
    首次发布必须返回占用（False），对手原样，重试落 C-0104 且 YAML 编号一致。"""
    from osca_host import settle as settle_mod

    real = settle_mod.publish_file_in_dir
    rival = loaded.root / "cases" / "C-0103.yaml"
    outcomes: list[tuple[str, bool]] = []

    def racing(dir_fd, filename, data, *, overwrite):
        if not outcomes:
            rival.write_text("case_id: C-0103\n# 对手的完整内容\n", encoding="utf-8")
        ok = real(dir_fd, filename, data, overwrite=overwrite)
        outcomes.append((filename, ok))
        return ok

    monkeypatch.setattr(settle_mod, "publish_file_in_dir", racing)
    (result,) = settle_episode(loaded, proxy, _episode({"OBJ-009": OBJECTIVE}))
    assert outcomes[0] == ("C-0103.yaml", False)  # 首次发布：占用，无覆盖
    assert outcomes[-1] == ("C-0104.yaml", True)  # 顺移重试成功
    assert result["case"] == "C-0104"
    assert "对手的完整内容" in rival.read_text(encoding="utf-8")  # 对手文件原样
    published = yaml.safe_load((loaded.root / "cases" / "C-0104.yaml").read_text(encoding="utf-8"))
    assert published["case_id"] == "C-0104"  # 内容编号与文件名一致（重试后重写）


def test_settle_refuses_symlinked_cases_dir(loaded, proxy, tmp_path):
    """cases/ 被换成外部目录链接 → 拒绝写入，包外零文件（十三轮：无覆盖 link 保完整不保在账本内）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    cases = loaded.root / "cases"
    shutil.rmtree(cases)
    cases.symlink_to(outside)
    with pytest.raises(OSError):
        settle_episode(loaded, proxy, _episode({"OBJ-009": OBJECTIVE}))
    assert list(outside.iterdir()) == []
