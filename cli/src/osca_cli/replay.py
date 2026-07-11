"""回放器（M2 先行版）—— 单条判断体检：`osca replay <包> J-xxxx`。

架构 §6：单判断 A/B（注入 / 不注入本判断），看输出是否**从改前移向改后**。
三用途：判断有效性证明、换模型迁移审计、账本健康仪表盘（红灯率 → kill switch）。
整本体检单与仪表盘归 M3 回放器；这里先把「单条可回放」立起来——发布凭据第三样。

机器判据（确定性，模型无关）：
    score(产出) = 相似度(产出, expert_final) − 相似度(产出, agent_draft)
    绿灯 ⇔ score(注入) > score(不注入)
这是「从改前移向改后」的字面落地：既奖励靠近专家改后，也奖励离开机器改前——
删除类判断（expert_final 为删除占位）靠后一项仍可判。判断 ID 是否被引用
作为提示信号一并报告，不作硬判据（负判断的期望产出可以完全不出现该 ID）。

断言里的 with/without_this_judgment 是给人读的期望声明，机器不解析——
自然语言断言进判据的那天，判据本身就不可信了。

A/B 两臂共享 case 的「当时生效判断集」（回放复现的地基，SPEC §10），
差异只有一个变量：本判断在不在场。LLM 通道同 osca_cli.llm（温度 0，可 mock）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from osca_cli.llm import LLMError, resolve_llm
from osca_cli.package import OscaPackage, load_package


class ReplayError(Exception):
    """包/判断/断言形状不对——回放没得跑，人话报错。"""


@dataclass
class AssertionVerdict:
    given: str
    case_id: str
    status: str  # green | red | error
    detail: str
    expected_with: str | None = None
    expected_without: str | None = None
    cited: bool | None = None  # 注入臂产出是否引用了判断 ID（提示信号，非判据）
    score_with: float | None = None
    score_without: float | None = None
    output_with: str | None = None
    output_without: str | None = None


@dataclass
class ReplayReport:
    judgment_id: str
    package_id: str
    model: str
    verdicts: list[AssertionVerdict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.verdicts) and all(v.status == "green" for v in self.verdicts)


def _yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()


def _find_by_id(pkg: OscaPackage, dirname: str, id_field: str, wanted: str) -> dict | None:
    for f in pkg.typed_files(dirname):
        if f.mapping.get(id_field) == wanted:
            return f.mapping
    return None


def _system_prompt(pkg: OscaPackage, active_judgments: list[dict]) -> str:
    agent_md = pkg.root / "AGENT.md"
    parts = [agent_md.read_text(encoding="utf-8").strip() if agent_md.is_file() else ""]
    objects = [f.mapping for f in pkg.typed_files("objects") if f.mapping]
    if objects:
        parts.append("## 对象定义（objects）\n\n```yaml\n" + _yaml(objects) + "\n```")
    if active_judgments:
        parts.append("## 当前生效判断集\n\n```yaml\n" + _yaml(active_judgments) + "\n```")
    else:
        parts.append("## 当前生效判断集\n\n（空——无既有判断可依，保守默认并如实标注。）")
    return "\n\n".join(p for p in parts if p)


def _user_prompt(case_id: str, case_input: dict) -> str:
    shown = {k: v for k, v in case_input.items() if k != "当时生效判断集"}
    return (
        f"回放情境（case {case_id} 采集时的触发上下文）：\n```yaml\n{_yaml(shown)}\n```\n\n"
        "任务：对上述候选依当前生效判断集做裁决，并按对象定义的 quality_bar 成文该条目；"
        "判断未覆盖时保守默认并如实标注。只输出条目内容本身，不要输出解释性前后缀。"
    )


def _movement(text: str, draft: str, final: str) -> float:
    """「从改前移向改后」的分值：靠近 expert_final 加分，离开 agent_draft 也加分。"""
    return SequenceMatcher(None, text, final).ratio() - SequenceMatcher(None, text, draft).ratio()


def _judgment_brief(j: dict) -> dict:
    return {"judgment_id": j.get("judgment_id"), "signature": j.get("signature"), "body": j.get("body")}


def replay_judgment(package: str | Path, judgment_id: str, llm=None) -> ReplayReport:
    """对一条判断跑全部回放断言。llm 未注入时按环境变量解析（OSCA_LLM_URL，可 mock://）。"""
    root = Path(package)
    if not root.is_dir():
        raise ReplayError(f"包目录不存在：{root}")
    pkg = load_package(root)
    judgment = _find_by_id(pkg, "judgments", "judgment_id", judgment_id)
    if judgment is None:
        raise ReplayError(f"判断不存在：{judgment_id}（包 {root} 的 judgments/ 里没有）")
    assertions = [a for a in judgment.get("replay") or [] if isinstance(a, dict)]
    if not assertions:
        raise ReplayError(f"{judgment_id} 没有 replay 断言——无断言不可体检（SPEC §9：回放断言≥1）")

    llm = llm or resolve_llm()
    manifest = pkg.yaml_files.get("osca.yaml")
    report = ReplayReport(
        judgment_id=judgment_id,
        package_id=str(manifest.mapping.get("package_id", root.name)) if manifest else root.name,
        model=getattr(llm, "model", "?"),
    )

    for assertion in assertions:
        given = str(assertion.get("given", ""))
        case_id = given.split(".", 1)[0]
        verdict = AssertionVerdict(
            given=given,
            case_id=case_id,
            status="error",
            detail="",
            expected_with=assertion.get("with_this_judgment"),
            expected_without=assertion.get("without_this_judgment"),
        )
        report.verdicts.append(verdict)

        case = _find_by_id(pkg, "cases", "case_id", case_id)
        if case is None:
            verdict.detail = f"断言引用的 case 不存在：{case_id}"
            continue
        case_input = case.get("input")
        draft, final = case.get("agent_draft"), case.get("expert_final")
        if not isinstance(case_input, dict) or not isinstance(draft, str) or not isinstance(final, str):
            verdict.detail = f"{case_id} 缺 input / agent_draft / expert_final——非 diff 物种证据，不可 A/B 回放"
            continue

        # 复现采集时的判断集；两臂唯一差异 = 本判断在不在场
        base_ids = [str(i) for i in case_input.get("当时生效判断集") or [] if str(i) != judgment_id]
        base = [b for i in base_ids if (b := _find_by_id(pkg, "judgments", "judgment_id", i)) is not None]
        arms = {
            "without": [_judgment_brief(j) for j in base],
            "with": [_judgment_brief(j) for j in [*base, judgment]],
        }
        user = _user_prompt(case_id, case_input)
        try:
            outputs = {
                arm: llm.complete(_system_prompt(pkg, active), user, tag=f"replay/{judgment_id}/{case_id}/{arm}").text
                for arm, active in arms.items()
            }
        except LLMError as e:
            verdict.detail = str(e)
            continue

        verdict.output_with, verdict.output_without = outputs["with"], outputs["without"]
        verdict.score_with = round(_movement(outputs["with"], draft, final), 4)
        verdict.score_without = round(_movement(outputs["without"], draft, final), 4)
        verdict.cited = judgment_id in outputs["with"]
        moved = verdict.score_with > verdict.score_without
        verdict.status = "green" if moved else "red"
        verdict.detail = f"score(注入)={verdict.score_with} vs score(不注入)={verdict.score_without} → " + (
            "输出从改前移向改后" if moved else "注入未使输出向专家改后移动"
        )
    return report


LIGHT = {"green": "🟢", "red": "🔴", "error": "⚠"}


def format_report(report: ReplayReport) -> str:
    lines = [f"osca replay {report.judgment_id}（包 {report.package_id}，模型 {report.model}）", ""]
    for v in report.verdicts:
        lines.append(f"{LIGHT[v.status]} {v.given}：{v.detail}")
        if v.expected_without:
            lines.append(f"   断言（不注入）：{v.expected_without}")
        if v.expected_with:
            lines.append(f"   断言（注入）　：{v.expected_with}")
        if v.cited is not None:
            lines.append(f"   提示信号：注入臂{'已' if v.cited else '未'}引用 {report.judgment_id}（非判据）")
    lines.append("")
    green = sum(1 for v in report.verdicts if v.status == "green")
    conclusion = "✅ 判断可回放，仍在起作用" if report.ok else "❌ 红灯——该判断进蒸馏队列重审"
    lines.append(f"体检结论：{green}/{len(report.verdicts)} 绿灯 → {conclusion}")
    return "\n".join(lines)
