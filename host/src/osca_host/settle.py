"""Host 组件 7：对账器 settle —— 现实是第二位专家（公理 A2）。

objective 型场景（闭环）在剧集完成后自动对账：decision（剧集产出）vs reality
（经 Connector 代理取数），落一条带 outcome 的 case 进包内 cases/——飞轮的
第二个证据物种，与专家 diff 同一口径入账。对账不消耗剧集：无 LLM、无唤醒，
纯确定性运行时动作。

可求值受限形式（与 precondition / emit_when 同一纪律，SPEC v0.4 草案 §4）：
    settle: {uses: CON-xxx.接口名, when: <自由文本注释，机器不读>}
自由文本 settle 声明不报错、不执行——保守默认，留痕。
「闭店后/收盘后」的时刻语义需要部署侧的营业日历，M2 参考实现在剧集完成后
立即对账并把 when 声明留档；定时对账随后续版本落地。
"""

from __future__ import annotations

import re
from datetime import datetime

import yaml
from osca_cli.ledger import allocate_case_path

from osca_host.connector import ConnectorProxy
from osca_host.episode import Episode
from osca_host.loader import LoadedPackage

INTERFACE_REF = re.compile(r"CON-\d{3,4}\.\S+")


def _decision(episode: Episode) -> object:
    """剧集的决策产出：最后一个 done 步骤的产物（agent 草稿或 optimizer 方案）。"""
    for record in reversed(episode.steps):
        if record.get("status") == "done" and record.get("output") is not None:
            return record["output"]
    return episode.draft


def settle_episode(loaded: LoadedPackage, proxy: ConnectorProxy, episode: Episode) -> list[dict]:
    """对剧集上下文中的 objective 型对象逐个对账；返回落账记录（同时写进 episode.settlements）。"""
    results: list[dict] = []
    for object_id, spec in sorted((episode.context.get("objects") or {}).items()):
        if not isinstance(spec, dict) or spec.get("kind") != "objective":
            continue
        declared = spec.get("settle")
        uses = str(declared.get("uses", "")) if isinstance(declared, dict) else ""
        if not INTERFACE_REF.fullmatch(uses):
            results.append(
                {
                    "object": object_id,
                    "settled": False,
                    "note": "settle 非受限形式（settle: {uses: CON-xxx.接口名}），保守不执行留痕",
                }
            )
            continue
        receipt = proxy.call(uses, step=None)  # 运行时内部调用，不走模型白名单
        if not receipt.ok:
            results.append({"object": object_id, "settled": False, "note": f"对账取数失败：{receipt.error}"})
            continue

        # 编号分配即占位（O_EXCL）：并发剧集/采集器同时落账也绝不同号覆盖
        case_id, path = allocate_case_path(loaded.root)
        case = {
            "case_id": case_id,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "capture_source": "对账器 settle（剧集完成后自动对账）",
            "kind": "outcome",
            "report": episode.episode_id,
            "input": {
                "objective": object_id,
                "episode": episode.episode_id,
                "fired_trigger": episode.fired_trigger,
                "当时生效判断集": [j["judgment_id"] for j in episode.context.get("judgments") or []],
            },
            "outcome": {
                "decision": _decision(episode),
                "reality": receipt.payload,  # 已过代理脱敏
                "settled_via": uses,
                "when_declared": str(declared.get("when", "")) or None,
            },
            "distillation": {"status": "pending"},
        }
        try:
            path.write_text(yaml.safe_dump(case, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except OSError:
            path.unlink(missing_ok=True)  # 写入失败不留空壳占位进账本
            raise
        results.append({"object": object_id, "settled": True, "case": case_id, "path": str(path)})

    episode.settlements.extend(results)
    return results
