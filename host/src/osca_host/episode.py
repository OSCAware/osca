"""Host 组件 4：剧集装配器 —— 唤醒时组装一次性上下文（架构 §4）。

一次性上下文 = AGENT.md + structure + 命中 Aware 的 discretion + 引用 objects
             + 检索命中的判断（top3–7，各带 1 个代表 case）。

判断检索（架构 §6 检索器的 M2 先行版）：签名表（indexes/judgments.index.yaml，
装载时重建）硬过滤 → active 且签名命中本 Aware 或本剧集引用的 object →
按 trust（high 优先）+ confirmed 降序取 top 7。语义排序（向量）是 M3 索引器的事。

纪律（公理 A5）：policy.yaml 是笼子，运行时读、模型永不读——**不入上下文**。
剧集短命无状态：装配产物只进 Host 的剧集台账，执行属 W5。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

import yaml
from osca_cli.package import referenced_ids

from osca_host.loader import AwareDecl, LoadedPackage

TOP_JUDGMENTS = 7  # 检索上限（架构：top3–7）
CASE_NUM = re.compile(r"C-(\d+)")


@dataclass
class Episode:
    """一次唤醒装配出的一次性上下文。执行（W5）前它就是剧集的全部。"""

    episode_id: str
    package_id: str
    aware_id: str
    fired_trigger: str
    assembled_at: str
    then: str | None
    budget: dict
    context: dict = field(repr=False)

    def summary(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "package_id": self.package_id,
            "aware_id": self.aware_id,
            "fired_trigger": self.fired_trigger,
            "assembled_at": self.assembled_at,
            "then": self.then,
            "judgments": [j["judgment_id"] for j in self.context["judgments"]],
            "objects": sorted(self.context["objects"]),
        }

    def dump(self) -> dict:
        return asdict(self)


def _signature_table(loaded: LoadedPackage) -> list[dict]:
    """签名表：装载五步的最后一步重建，是判断检索的硬过滤输入（indexes/ 属缓存，不在 pack 内）。"""
    index_path = loaded.root / "indexes" / "judgments.index.yaml"
    index = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    return index.get("judgments") or []


def _by_id(loaded: LoadedPackage, dirname: str) -> dict[str, dict]:
    files = loaded.pack.typed_files(dirname)
    field_name = {"objects": "object_id", "judgments": "judgment_id", "cases": "case_id"}[dirname]
    return {f.mapping[field_name]: f.mapping for f in files if f.mapping.get(field_name)}


def _representative_case(judgment: dict, cases: dict[str, dict]) -> dict | None:
    """代表 case = 出生证据里编号最新的一条（最近的专家动作最能代表判断的活用法）。"""
    evidence = [e for e in judgment.get("evidence") or [] if isinstance(e, str) and CASE_NUM.fullmatch(e)]
    if not evidence:
        return None
    latest = max(evidence, key=lambda e: int(CASE_NUM.fullmatch(e).group(1)))
    return cases.get(latest)


def retrieve_judgments(loaded: LoadedPackage, aware_id: str, object_ids: set[str]) -> list[dict]:
    """签名表硬过滤 + trust/confirmed 排序，top 7，各带 1 个代表 case。"""
    hits = [
        e
        for e in _signature_table(loaded)
        if e.get("status") == "active" and (e.get("aware") == aware_id or e.get("object") in object_ids)
    ]
    judgments = _by_id(loaded, "judgments")
    cases = _by_id(loaded, "cases")

    hydrated = []
    for entry in hits:
        j = judgments.get(entry.get("judgment_id"))
        if j is None:  # 签名表是缓存，包才是真理；不一致时以包为准跳过
            continue
        meta = j.get("meta") or {}
        hydrated.append(
            {
                "judgment_id": j.get("judgment_id"),
                "signature": j.get("signature"),
                "body": j.get("body"),
                "trust": meta.get("trust"),
                "confirmed": meta.get("confirmed", 0),
                "case": _representative_case(j, cases),
            }
        )
    hydrated.sort(key=lambda j: (j["trust"] != "high", -(j["confirmed"] or 0), j["judgment_id"]))
    return hydrated[:TOP_JUDGMENTS]


def assemble(episode_id: str, loaded: LoadedPackage, aware: AwareDecl, fired_trigger: str) -> Episode:
    """唤醒 → 一次性上下文。纯确定性：同一包同一 Aware 装配出同样的上下文。"""
    structure = loaded.pack.yaml_files.get("structure.yaml")
    structure_map = structure.mapping if structure else {}

    # 引用 objects = structure 正文引用的 OBJ-* ∪ 命中判断签名指向的 object
    object_ids = {i for i in referenced_ids(structure) if i.startswith("OBJ-")} if structure else set()
    judgments = retrieve_judgments(loaded, aware.aware_id, object_ids)
    object_ids |= {j["signature"].get("object") for j in judgments if isinstance(j.get("signature"), dict)}
    objects = {oid: spec for oid, spec in _by_id(loaded, "objects").items() if oid in object_ids}

    context = {
        "agent": (loaded.root / "AGENT.md").read_text(encoding="utf-8"),
        "structure": structure_map,
        "discretion": aware.discretion,
        "objects": objects,
        "judgments": judgments,
        # policy.yaml 刻意缺席：笼子归 Policy 拦截器（W4）强制执行，模型不读（公理 A5）
    }
    return Episode(
        episode_id=episode_id,
        package_id=loaded.package_id,
        aware_id=aware.aware_id,
        fired_trigger=fired_trigger,
        assembled_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        then=aware.then,
        budget=aware.budget,
        context=context,
    )
