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

import os
import re
from datetime import datetime

import yaml
from osca_cli.ledger import CASE_NUM, ledger_lock, open_ledger_dir, publish_file_in_dir

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

    def note(entry: dict) -> None:
        # 逐项即时记账（P2）：多 objective 中途异常时，已落盘的 case 必须已在 episode.settlements
        # 里可见——「case 在账本、settlements 为空」的静默 partial 不允许存在
        results.append(entry)
        episode.settlements.append(entry)

    for object_id, spec in sorted((episode.context.get("objects") or {}).items()):
        if not isinstance(spec, dict) or spec.get("kind") != "objective":
            continue
        declared = spec.get("settle")
        uses = str(declared.get("uses", "")) if isinstance(declared, dict) else ""
        if not INTERFACE_REF.fullmatch(uses):
            note(
                {
                    "object": object_id,
                    "settled": False,
                    "note": "settle 非受限形式（settle: {uses: CON-xxx.接口名}），保守不执行留痕",
                }
            )
            continue
        receipt = proxy.call(uses, step=None)  # 运行时内部调用，不走模型白名单
        if not receipt.ok:
            note({"object": object_id, "settled": False, "note": f"对账取数失败：{receipt.error}"})
            continue

        # 入账本锁协议（Review 十一轮）+ 安全目录发布（十三轮）：目录经 lstat/O_NOFOLLOW
        # 校验后以 dir_fd 全程操作——cases/ 被换成符号链接也写不出包根；内容写满临时
        # inode + fsync 再 link 无覆盖落名，外部读者看不到零字节 C-xxxx.yaml
        with ledger_lock(loaded.root), open_ledger_dir(loaded.root, "cases") as cases_fd:
            names = os.listdir(cases_fd)
            taken = [int(m.group(1)) for nm in names if nm.endswith(".yaml") and (m := CASE_NUM.match(nm[:-5]))]
            n = max(taken, default=0) + 1
            case_id = f"C-{n:04d}"
            case = {
                "case_id": case_id,
                "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "capture_source": "对账器 settle（剧集完成后自动对账）",
                "kind": "outcome",
                "report": episode.episode_id,
                "input": {
                    "objective": object_id,
                    "episode": episode.episode_id,
                    # 机器唯一身份（P2）：EP-xxxx 是跨重启可复用的展示号——outcome case 必须
                    # 同时持久化 operation_id，事后归因才不会把两次重启的同号剧集混为一谈
                    "operation_id": episode.operation_id,
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
            while True:
                case["case_id"] = case_id
                payload = yaml.safe_dump(case, allow_unicode=True, sort_keys=False).encode("utf-8")
                if publish_file_in_dir(cases_fd, f"{case_id}.yaml", payload, overwrite=False):
                    break
                n += 1  # 无覆盖发布：撞号顺移重试（编号随内容重写保持一致），绝不截断他人内容
                case_id = f"C-{n:04d}"
        note(
            {
                "object": object_id,
                "settled": True,
                "case": case_id,
                "path": str(loaded.root / "cases" / f"{case_id}.yaml"),
            }
        )

    return results
