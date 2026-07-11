"""回放器：单条判断 A/B 体检——机器判据 = 输出从改前移向改后。"""

from __future__ import annotations

import pytest

from osca_cli.llm import LLMReply, MockLLM
from osca_cli.main import main
from osca_cli.replay import ReplayError, format_report, replay_judgment

DRAFT = "【异动】甲单位差旅费环比+45%，待核实：是否存在超标准报销。建议核查。\n"
FINAL = "该项落附录：检修期常态波动，正文不报。\n"


@pytest.fixture
def replay_base(base):
    """最小包 + diff 物种 case + 回放断言。"""
    base["cases/C-0001.yaml"] = {
        "case_id": "C-0001",
        "captured_at": "2026-01-01 10:00",
        "capture_source": "报告终审界面 diff 监听",
        "input": {"单位名称": "甲单位", "环比涨幅": 45, "当时生效判断集": []},
        "agent_draft": DRAFT,
        "expert_final": FINAL,
    }
    base["judgments/J-0001.yaml"]["replay"] = [
        {
            "given": "C-0001.input",
            "without_this_judgment": "报警出现在正文",
            "with_this_judgment": "报警落附录并注明 J-0001",
        }
    ]
    return base


@pytest.fixture
def llm_dir(tmp_path):
    """绿灯固件：注入臂贴近专家改后并引用判断 ID，不注入臂复读机器改前。"""
    d = tmp_path / "llm-fixtures" / "replay" / "J-0001" / "C-0001"
    d.mkdir(parents=True)
    (d / "with.md").write_text("该项落附录：检修期常态波动（J-0001），正文不报。\n", encoding="utf-8")
    (d / "without.md").write_text(DRAFT, encoding="utf-8")
    return tmp_path / "llm-fixtures"


def test_green_when_output_moves_toward_expert(replay_base, make_pkg, llm_dir):
    report = replay_judgment(make_pkg(replay_base), "J-0001", llm=MockLLM(llm_dir))
    assert report.ok and report.package_id == "demo-pkg"
    (v,) = report.verdicts
    assert v.status == "green"
    assert v.score_with > v.score_without  # 从改前移向改后
    assert v.cited is True  # 提示信号：注入臂引用了判断 ID
    rendered = format_report(report)
    assert "🟢" in rendered and "可回放" in rendered


def test_red_when_injection_changes_nothing(replay_base, make_pkg, llm_dir):
    # 注入臂也复读改前 → 注入没让输出移动 → 红灯，进蒸馏队列重审
    (llm_dir / "replay" / "J-0001" / "C-0001" / "with.md").write_text(DRAFT, encoding="utf-8")
    report = replay_judgment(make_pkg(replay_base), "J-0001", llm=MockLLM(llm_dir))
    assert not report.ok
    (v,) = report.verdicts
    assert v.status == "red" and v.cited is False
    assert "❌" in format_report(report)


def test_ab_arms_differ_by_exactly_one_judgment(replay_base, make_pkg):
    """A/B 纪律：case 的「当时生效判断集」两臂共享，唯一差异 = 本判断在不在场。"""
    replay_base["judgments/J-0002.yaml"] = {
        "judgment_id": "J-0002",
        "status": "active",
        "signature": {"object": "OBJ-001", "aware": "AW-001", "guard": "金额 > 0"},
        "body": "历史在场判断——两臂都注入。",
        "evidence": ["C-0001"],
    }
    replay_base["cases/C-0001.yaml"]["input"]["当时生效判断集"] = ["J-0002"]

    class CaptureLLM:
        def __init__(self):
            self.systems: dict[str, str] = {}
            self.model = "capture"

        def complete(self, system, user, *, tag):
            self.systems[tag] = system
            return LLMReply(text="产出", tokens=1, model=self.model)

    llm = CaptureLLM()
    replay_judgment(make_pkg(replay_base), "J-0001", llm=llm)
    without = llm.systems["replay/J-0001/C-0001/without"]
    with_arm = llm.systems["replay/J-0001/C-0001/with"]
    assert "历史在场判断" in without and "历史在场判断" in with_arm
    assert "演示判断" not in without and "演示判断" in with_arm  # J-0001 的 body 只在注入臂


def test_unknown_judgment(replay_base, make_pkg, llm_dir):
    pkg = make_pkg(replay_base)
    with pytest.raises(ReplayError, match="判断不存在"):
        replay_judgment(pkg, "J-9999", llm=MockLLM(llm_dir))


def test_no_assertions_is_an_error(base, make_pkg, llm_dir):
    base["judgments/J-0001.yaml"].pop("replay")
    with pytest.raises(ReplayError, match="没有 replay 断言"):
        replay_judgment(make_pkg(base), "J-0001", llm=MockLLM(llm_dir))


def test_non_diff_case_cannot_replay(base, make_pkg, llm_dir):
    # conftest 的 C-0001 只有 input（口述物种）——没有改前改后，A/B 无从比对
    report = replay_judgment(make_pkg(base), "J-0001", llm=MockLLM(llm_dir))
    (v,) = report.verdicts
    assert v.status == "error" and "不可 A/B 回放" in v.detail
    assert not report.ok and "⚠" in format_report(report)


def test_missing_llm_fixture_surfaces_as_error(replay_base, make_pkg, tmp_path):
    empty = tmp_path / "no-fixtures"
    empty.mkdir()
    report = replay_judgment(make_pkg(replay_base), "J-0001", llm=MockLLM(empty))
    (v,) = report.verdicts
    assert v.status == "error" and "固件缺失" in v.detail


def test_cli_replay_exit_codes(replay_base, make_pkg, llm_dir, monkeypatch, capsys):
    pkg = make_pkg(replay_base)
    monkeypatch.setenv("OSCA_LLM_URL", f"mock://{llm_dir}")
    assert main(["replay", str(pkg), "J-0001"]) == 0
    assert "体检结论" in capsys.readouterr().out
    assert main(["replay", str(pkg), "J-9999"]) == 1  # 判断不存在 → 人话 + 非零退出


def test_cli_replay_without_llm_config(replay_base, make_pkg, monkeypatch, capsys):
    monkeypatch.delenv("OSCA_LLM_URL", raising=False)
    assert main(["replay", str(make_pkg(replay_base)), "J-0001"]) == 1
    assert "OSCA_LLM_URL" in capsys.readouterr().out
