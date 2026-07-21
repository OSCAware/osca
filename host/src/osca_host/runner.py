"""剧集执行器 —— 认知平面的宿主（架构 §5）。

Host 本体（控制平面）确定性、无 LLM；剧集是短命的认知平面，LLM 只活在这里：
由 Host 唤醒时拉起，沿 structure.pipeline 走完即死。跨剧集记忆只存在于
账本与 git，不存在于模型。LLM 通道复用 osca_cli.llm（环境变量配置，不锁定厂商）。

performer 分工（架构 §5，受限集——不可识别的 performer 直接拒绝，不猜）：
- connector：确定性取数，经 Connector 代理（模型只能按名调用），回执入档；
- agent（含 agent + judgments）：LLM 依一次性上下文出草稿，产出注入前过 Policy 脱敏；
- optimizer：确定性算法寻优——初版贪心（架构原文「初版贪心即可」）：
  候选受限形式 list[dict{value: 数值}]，按 objective 方向排序取最优；缺数值即拒不猜；
- human：审批门与终审——飞轮采集点，机器的流水线到此为止（界面归 M4）；
- runtime：对账步，移交对账器 settle（剧集完成后运行，不消耗剧集）。

三级停之「剧集停」在此落地：pipeline 走完 / budget 硬顶 / 步骤失败，三种终态
都进台账留痕。预算双重：aware.budget（max_steps / max_minutes / max_tokens）
由本执行器裁决，policy per_episode（tool_calls / tokens）由拦截器裁决——笼子优先。
"""

from __future__ import annotations

import inspect
import math
import time
from dataclasses import asdict
from datetime import datetime

import yaml
from osca_cli.llm import LLMError, estimate_tokens, resolve_llm
from osca_cli.triggers import AWARE_BUDGET_KEYS, PERFORMERS, parse_performer

from osca_host.connector import ConnectorProxy
from osca_host.episode import Episode
from osca_host.loader import LoadedPackage
from osca_host.policy import PolicyInterceptor, parse_quantity


def _yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()


def _llm_supports_timeout(llm) -> tuple[bool, str]:
    """LLM 通道是否提供 timeout 有界执行契约（显式 timeout 参数或 **kwargs 兜收）。

    max_minutes 声明为硬顶时这是**强制契约**（GPT 三审 P2）：不支持的适配器 fail-closed 拒绝发起
    ——「只剩数秒仍无限外呼」是把运行时硬预算做成 fail-open。签名不可内省（C 扩展/怪 callable）
    同判不支持（fail-closed，不炸穿 runner）。"""
    try:
        params = inspect.signature(llm.complete).parameters
    except (TypeError, ValueError):
        return False, "complete 签名不可内省"
    if "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True, ""
    return False, "complete 未声明 timeout 参数（也无 **kwargs）"


def render_system_prompt(episode: Episode) -> str:
    """一次性上下文 → 模型可读文本。policy.yaml 在装配时已刻意缺席（公理 A5）。"""
    ctx = episode.context
    parts = [str(ctx.get("agent", "")).strip()]
    if ctx.get("discretion"):
        parts.append("## 本次唤醒的裁量说明（discretion）\n\n" + str(ctx["discretion"]).strip())
    parts.append("## 组合骨架（structure）\n\n```yaml\n" + _yaml(ctx.get("structure") or {}) + "\n```")
    if ctx.get("objects"):
        parts.append("## 对象定义（objects）\n\n```yaml\n" + _yaml(ctx["objects"]) + "\n```")
    if ctx.get("judgments"):
        parts.append(
            "## 候选判断（依签名 object×aware 硬过滤检索，各带 1 个代表 case；guard 未判定）\n\n```yaml\n"
            + _yaml(ctx["judgments"])
            + "\n```"
        )
        parts.append(
            "## 判断应用纪律（guard 逐条判定，SPEC §11）\n\n"
            "上列判断只经 object×aware 确定性硬过滤，`signature.guard` **尚未判定**。应用任何一条之前，"
            "先按本次情境（输入产物、取数结果）逐条判定其 guard 是否命中："
            "guard 不命中、或依据不足无法判断的，一律**不得应用、不得标注其判断 ID**；"
            "只有 guard 命中的判断才依其裁决，并按下述归属纪律标注。\n\n"
            "## 归属纪律（飞轮口径）\n\n"
            "草稿中凡依据某条 guard 命中判断裁决或成文的段落，须在该段落末尾标注其判断 ID（如（J-0417））；"
            "未依据判断的段落不标。段落级标注是采集器归属计数的唯一依据——"
            "标注随草稿进专家终审：专家整段保留即 confirmed，整段删除即 overruled。"
        )
    parts.append(
        f"## 剧集\n\n本剧集 {episode.episode_id} 由 {episode.fired_trigger} 触发。"
        "剧集短命无状态：只做本次 pipeline 的事，产出交由人终审。"
    )
    return "\n\n".join(p for p in parts if p)


def _input_key(spec: dict) -> str | None:
    declared = spec.get("input")
    if declared is None:
        return None
    return str(declared.get("ref")) if isinstance(declared, dict) else str(declared)


def _produces_key(spec: dict, step_name: str) -> str:
    produced = spec.get("produces")
    if isinstance(produced, dict):
        return str(produced.get("ref") or step_name)
    return str(produced) if produced else step_name


def _step_user_prompt(spec: dict, step_name: str, input_key: str | None, input_value) -> str:
    parts = [f"当前执行 pipeline 步骤「{step_name}」。步骤声明：\n```yaml\n{_yaml(spec)}\n```"]
    if input_key is not None:
        rendered = input_value if isinstance(input_value, str) else _yaml(input_value)
        parts.append(f"输入产物「{input_key}」：\n\n{rendered}")
    parts.append(
        "只输出本步骤产出物的内容本身，不要输出解释性前后缀；"
        "依据 guard 命中判断的段落保留段末判断 ID 标注（归属纪律；guard 不命中或无法判断的判断不得应用）。"
    )
    return "\n\n".join(parts)


def _interface_refs(uses, proxy: ConnectorProxy) -> tuple[list[str], str | None]:
    """步骤 uses → 接口引用列表。裸 Connector ID 展开为 manifest 声明的全部接口。"""
    refs: list[str] = []
    for item in uses if isinstance(uses, list) else [uses]:
        ref = str(item)
        if "." in ref:
            refs.append(ref)
            continue
        declared = sorted(k for k in proxy.interfaces if k.startswith(ref + "."))
        if not declared:
            return [], f"Connector {ref} 在 manifest 中没有声明任何接口"
        refs.extend(declared)
    return refs, None


def _run_optimizer(spec: dict, artifacts: dict, objects: dict) -> tuple[dict | None, str]:
    """初版贪心：按 objective 方向对候选排序取最优。数值缺失即拒——optimizer 不猜数。"""
    key = _input_key(spec)
    candidates = artifacts.get(key) if key else None
    if not isinstance(candidates, list) or not candidates:
        return None, f"optimizer 输入「{key}」不是非空候选列表（受限形式：list[dict{{value: 数值}}]）"
    objectives = [o for o in objects.values() if isinstance(o, dict) and o.get("kind") == "objective"]
    if spec.get("objective"):
        objectives = [o for o in objectives if o.get("object_id") == spec["objective"]]
    if len(objectives) != 1:
        return None, "optimizer 需要恰好一个 objective 型对象作寻优目标（步骤可用 objective: OBJ-xxx 指定）"
    objective = objectives[0]
    direction = str(objective.get("optimize", "maximize"))
    try:
        scored = [(float(c["value"]), c) for c in candidates]
    except (TypeError, ValueError, KeyError):
        return None, "候选缺少数值 value 字段——optimizer 不猜数，直接拒绝"
    if any(not math.isfinite(v) for v, _ in scored):
        # NaN 不触发 float() 异常却会毒化排序（NaN 候选可被选为 selected）；Infinity 同拒（GPT Review P2）
        return None, "候选 value 含非有限数（NaN/Infinity）——optimizer 不猜数，直接拒绝"
    ranked = [c for _, c in sorted(scored, key=lambda t: t[0], reverse=direction == "maximize")]
    plan = {
        "objective": objective.get("object_id"),
        "optimize": direction,
        "impl": str(spec.get("impl", "greedy_v0")),
        "ranked": ranked,
        "selected": ranked[0],
        # 数值约束求解与 bandit 属部署侧演进；约束声明留档给人审
        "constraints": objective.get("constraints"),
    }
    return plan, f"贪心寻优完成：{len(ranked)} 个候选按 {direction} 排序，选中首位"


def _record(episode: Episode, step: str, performer: str, status: str, detail: str, **extra) -> None:
    episode.steps.append({"step": step, "performer": performer, "status": status, "detail": detail, **extra})


def _finish(episode: Episode, status: str, reason: str | None = None) -> Episode:
    episode.status = status
    episode.stop_reason = reason
    episode.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return episode


def _suspend_episode(
    episode: Episode,
    index: int,
    ref_i: int,
    payloads: dict,
    receipts: list,
    write_params: object,
    artifacts: dict,
    challenge_id: str | None,
) -> Episode:
    """挂起等批（可恢复剧集）：写最小恢复快照 + 置**非终态** + 释放线程（返回，不落终态）。

    INV-1：挂起不持线程——返回即归还线程池，等批期间零 LLM/零线程。挂起是事件而非流水线步，
    刻意不记入 steps（max_steps 只计已执行步）；挂起态由 status + resume 快照对外可见。
    """
    episode.resume = {
        "step_index": index,
        "ref_index": ref_i,
        "payloads": payloads,
        "receipts": receipts,
        "write_params": write_params,
        "artifacts": artifacts,
        "challenge_id": challenge_id,
    }
    episode.status = "suspended_pending_approval"
    episode.stop_reason = None
    return episode


def _resume_write_ref(policy: PolicyInterceptor, challenge_id: str | None) -> str:
    """恢复重入挂起的写 ref：按快照 challenge_id 查**当前**挑战态分派（§5.2）。

    approved → 兑现（consume-only 执行写）；pending → 仍待决，保持挂起（幂等）；
    其余（denied/expired/revoked/consumed/None 已清出）→ 回落保守默认（不写）。
    绝不在此重入 consume_or_raise——那会对已终态挑战静默新挂一张 pending（§5.2 坑）。
    """
    ch = policy.get_challenge(challenge_id) if challenge_id else None
    state = ch.state if ch is not None else None
    if state == "approved":
        return "approved"
    if state == "pending":
        return "suspend"
    return "fallback"


def _fallback_marker(ref: str, reason: str) -> dict:
    """写审批驳回/过期/撤销时的保守默认标记（**不写**）——机制层只「不写 + distinct 记账 + 续跑上报」；
    「沿用昨日档位」类业务 fallback 归包 pipeline/agent 设计（W5 设计 §5.3 边界）。"""
    return {"interface": ref, "written": False, "fallback": True, "reason": reason}


def run_episode(
    episode: Episode,
    loaded: LoadedPackage,
    proxy: ConnectorProxy,
    policy: PolicyInterceptor,
    llm=None,
) -> Episode:
    """沿 pipeline 执行剧集。llm 未注入时按环境变量解析（osca_cli.llm）。"""
    episode.status = "running"
    started = time.monotonic()
    if episode.budget is not None and not isinstance(episode.budget, dict):
        return _finish(episode, "failed", "aware.budget 形状非法（须为 mapping）——宁可拒绝，不可无硬顶执行")
    budget = episode.budget or {}
    if unknown := sorted(k for k in budget if k not in AWARE_BUDGET_KEYS):
        # 跨层/未知键 = 声明了没人执行的硬顶——lint 应拦，运行时自防拒绝执行（fail-closed）
        detail = f"aware.budget 含运行时不执行的键 {unknown}（只认 {list(AWARE_BUDGET_KEYS)}）——拒绝执行"
        return _finish(episode, "failed", detail)

    def budget_cap(key: str) -> int | None:
        """声明了却不可解析 = 额度撤销（0）——绕过 lint 也不许退化成无硬顶（fail-closed 自防）。"""
        if key not in budget:
            return None
        value = parse_quantity(budget[key])
        return value if value is not None else 0

    max_steps = budget_cap("max_steps")
    max_minutes = budget_cap("max_minutes")
    max_tokens = budget_cap("max_tokens")

    pipeline = (episode.context.get("structure") or {}).get("pipeline") or []
    if not pipeline:
        return _finish(episode, "failed", "structure 无 pipeline，无事可执行")
    system_prompt = render_system_prompt(episode)
    # 恢复（可恢复剧集）：从挂起快照回灌已产出 artifacts、快进到挂起步——不重跑上游、不重记步。
    # 诚实标注：max_minutes 墙钟基线在恢复时重置（挂起期间不计活跃运行时）。
    resume_state = episode.resume
    resume_step = resume_state["step_index"] if resume_state is not None else None
    artifacts: dict[str, object] = dict(resume_state["artifacts"]) if resume_state is not None else {}

    for index, spec in enumerate(pipeline):
        if resume_step is not None and index < resume_step:
            continue  # 已跑过、artifacts 已回灌——恢复只从挂起步续（不重跑不重记，绕过预算/包停复检）
        if not isinstance(spec, dict):
            return _finish(episode, "failed", f"pipeline 第 {index + 1} 项不是步骤声明——宁可拒绝，不可猜测")
        step_name = str(spec.get("step", f"步骤{index + 1}"))
        performer = str(spec.get("performer", ""))

        # ── 包停触达（三级停之三）：unload 撤销后在途剧集步间即停，不再发起任何调用 ──
        if policy.revoked:
            return _finish(episode, "stopped", f"包已停：{policy.revoked}（在途剧集步间即停）")

        # ── 预算裁决（aware.budget；剧集停之 budget 硬顶） ──
        if max_steps is not None and len(episode.steps) >= max_steps:
            return _finish(episode, "stopped", f"预算硬顶：max_steps {max_steps} 用满（剧集停）")
        if max_minutes is not None and time.monotonic() - started > max_minutes * 60:
            return _finish(episode, "stopped", f"预算硬顶：max_minutes {max_minutes} 用满（剧集停）")

        # 受限语法解析（lint OSCA040 同源，单一真理源）——子串匹配已废：`not-a-connector` 不当 connector，
        # 多关键词不再依赖枚举顺序（GPT Review P2）
        kind = parse_performer(performer)

        if kind == "human":
            remaining = len(pipeline) - index - 1
            detail = "飞轮采集点：草稿待专家终审（采集器归 M3，界面归 M4）"
            if remaining:
                detail += f"；其后 {remaining} 步待人工环节回执，机器侧不自动续跑"
            _record(episode, step_name, performer, "handoff", detail)
            return _finish(episode, "completed")

        if kind == "connector":
            refs, error = _interface_refs(spec.get("uses"), proxy)
            if error:
                _record(episode, step_name, performer, "failed", error)
                return _finish(episode, "failed", error)
            # 写步取上游产物作**写 params**（params 穿透）：写接口经审批门以其摘要绑被写内容（防偷梁换柱）；
            # 读接口执行器忽略 params、也不过写审批门（取数步无 input）。写命中审批门 → **挂起等批**（可恢复剧集）。
            resuming = resume_state is not None and index == resume_step
            if resuming:
                rs = resume_state
                payloads, receipts = rs["payloads"], rs["receipts"]
                write_params, start_ref, pending_cid = rs["write_params"], rs["ref_index"], rs["challenge_id"]
                episode.resume = None  # 快照已回灌（若仍待决，下方 _suspend_episode 重写）
                resume_state = None
            else:
                write_params, start_ref, pending_cid = "", 0, None
                input_key = _input_key(spec)
                if input_key is not None:
                    if input_key not in artifacts:
                        detail = f"上游产物「{input_key}」缺失——连接器步声明与执行不符，直接拒绝"
                        _record(episode, step_name, performer, "failed", detail)
                        return _finish(episode, "failed", detail)
                    write_params = artifacts[input_key]
                payloads, receipts = {}, []

            fell_back = False
            for ref_i in range(start_ref, len(refs)):
                ref = refs[ref_i]
                if resuming and ref_i == start_ref:
                    # 恢复重入挂起的写 ref：按 challenge_id 当前态分派（§5.2）
                    verdict = _resume_write_ref(policy, pending_cid)
                    if verdict == "suspend":  # 仍待决 → 保持挂起（幂等）
                        return _suspend_episode(
                            episode, index, ref_i, payloads, receipts, write_params, artifacts, pending_cid
                        )
                    if verdict == "fallback":  # 驳回/过期/撤销/已清出 → 回落保守默认（不写）
                        payloads[ref] = _fallback_marker(ref, f"挑战 {pending_cid} 驳回/过期/撤销，未兑现")
                        fell_back = True
                        break
                    receipt = proxy.call(ref, write_params, step=step_name, episode_id=episode.episode_id, resume=True)
                    if not receipt.ok and receipt.disposition == "denied":
                        # consume **未命中**（驳回/过期/撤销/竞态过期，挑战未被消费）→ 回落保守默认（不写）
                        receipts.append(asdict(receipt))
                        payloads[ref] = _fallback_marker(ref, f"审批未兑现（consume 未命中）：{receipt.error}")
                        fell_back = True
                        break
                    if not receipt.ok:
                        # disposition 非 denied：binding/写执行器报错（挑战**已** consume）或 recheck 命中 kill/包停——
                        # 是真错误、不是审批回落；剧集失败（与首次路径同口径，不吞成 completed 掩盖系统错/烧掉的授权）
                        receipts.append(asdict(receipt))
                        _record(episode, step_name, performer, "failed", receipt.error, receipts=receipts)
                        return _finish(episode, "failed", f"恢复写执行失败：{receipt.error}")
                    receipts.append(asdict(receipt))
                    payloads[ref] = receipt.payload
                    continue

                receipt = proxy.call(ref, write_params, step=step_name, episode_id=episode.episode_id)
                if receipt.disposition == "pending":  # 首次命中审批门 → 挂起等批（非失败）
                    return _suspend_episode(
                        episode, index, ref_i, payloads, receipts, write_params, artifacts, receipt.challenge_id
                    )
                receipts.append(asdict(receipt))
                if not receipt.ok:
                    # 取数真失败 / 写配置拒绝（不在清单/空/非序列化）/ 真实写执行器未接入 → 剧集失败
                    _record(episode, step_name, performer, "failed", receipt.error, receipts=receipts)
                    return _finish(episode, "failed", f"取数失败：{receipt.error}")
                payloads[ref] = receipt.payload

            artifacts[_produces_key(spec, step_name)] = payloads
            if fell_back:
                _record(
                    episode,
                    step_name,
                    performer,
                    "denied",
                    "写审批驳回/过期→回落保守默认（不写）+ 上报",
                    receipts=receipts,
                )
            else:
                _record(episode, step_name, performer, "done", f"取数/写 {len(refs)} 接口", receipts=receipts)
            continue

        if kind == "optimizer":
            plan, detail = _run_optimizer(spec, artifacts, episode.context.get("objects") or {})
            if plan is None:
                _record(episode, step_name, performer, "failed", detail)
                return _finish(episode, "failed", detail)
            artifacts[_produces_key(spec, step_name)] = plan
            _record(episode, step_name, performer, "done", detail, output=plan)
            continue

        if kind == "agent":
            input_key = _input_key(spec)
            if input_key is not None and input_key not in artifacts:
                detail = f"上游产物「{input_key}」缺失——流水线声明与执行不符，直接拒绝"
                _record(episode, step_name, performer, "failed", detail)
                return _finish(episode, "failed", detail)
            # 统一闸（每次 LLM 调用前）：包停 / kill switch / tokens 额度——
            # 在途剧集对新触发的 kill switch 无豁免；零额度一次都不发起，止损顶只管超顶
            ok, reason = policy.authorize_llm(episode.episode_id)
            if not ok:
                return _finish(episode, "stopped", f"{reason}（剧集停）")
            if max_tokens is not None and episode.tokens_used >= max_tokens:
                return _finish(
                    episode,
                    "stopped",
                    f"预算硬顶：aware tokens 额度已尽（{episode.tokens_used}/{max_tokens}），拒绝发起调用（剧集停）",
                )
            user_prompt = _step_user_prompt(spec, step_name, input_key, artifacts.get(input_key))
            # 时间预算传导为单次调用硬顶（GPT Review P2）：max_minutes 只剩数秒时不许再吊默认 120s
            # 外呼继续烧外部成本。timeout 是**强制契约**（三审收口）：max_minutes 在而通道不支持
            # timeout（无参数、无 **kwargs、签名不可内省）→ fail-closed 拒绝发起，绝不 fail-open 无界外呼。
            deadline: float | None = None
            if max_minutes is not None:
                remaining = max_minutes * 60 - (time.monotonic() - started)
                if remaining <= 0:
                    return _finish(episode, "stopped", f"预算硬顶：max_minutes {max_minutes} 用满（剧集停）")
                deadline = remaining
            try:
                llm = llm or resolve_llm()
                kwargs = {}
                if deadline is not None:
                    supported, why = _llm_supports_timeout(llm)
                    if not supported:
                        detail = (
                            f"aware.budget max_minutes 是运行时硬顶，但注入的 LLM 通道无 timeout 有界执行契约"
                            f"（{why}）——fail-closed 拒绝发起调用（宁可拒绝，不可无硬顶外呼）"
                        )
                        _record(episode, step_name, performer, "failed", detail)
                        return _finish(episode, "failed", detail)
                    kwargs["timeout"] = deadline
                reply = llm.complete(system_prompt, user_prompt, tag=f"episode/{step_name}", **kwargs)
            except LLMError as e:
                _record(episode, step_name, performer, "failed", str(e))
                return _finish(episode, "failed", str(e))
            text, redacted = policy.redact(reply.text)  # 产出注入剧集台账前脱敏
            # 用量自报是不可信输入（源头 osca_cli.llm 已清洗；可插拔注入的 llm 走本处兜底）：
            # 非法上报**不得按 0 记账**（零成本无限过顶）也不得冲减硬顶——runner 看得见 prompt/产出，
            # 与 OpenAICompatLLM 同口径回落字符估算（GPT Review 复审 P1：按 0 计 = 免费绕过 max_tokens）
            tokens = (
                reply.tokens
                if type(reply.tokens) is int and reply.tokens > 0
                else estimate_tokens(system_prompt, user_prompt, text)
            )
            episode.tokens_used += tokens
            artifacts[_produces_key(spec, step_name)] = text
            episode.draft = text
            _record(
                episode,
                step_name,
                performer,
                "done",
                f"LLM 产出 {len(text)} 字",
                output=text,
                tokens=tokens,
                redacted=redacted,
            )
            ok, reason = policy.charge_tokens(episode.episode_id, tokens)  # 笼子的止损顶
            if not ok:
                return _finish(episode, "stopped", f"{reason}（剧集停）")
            if max_tokens is not None and episode.tokens_used > max_tokens:
                over = f"预算硬顶：tokens 已用 {episode.tokens_used} > {max_tokens}（剧集停）"
                return _finish(episode, "stopped", over)
            continue

        if kind == "runtime":
            _record(episode, step_name, performer, "handoff", "对账步：移交对账器 settle（剧集完成后运行，不消耗剧集）")
            continue

        detail = f"performer「{performer}」不可识别（受限集：{'/'.join(PERFORMERS)}）——宁可拒绝，不可猜测"
        _record(episode, step_name, performer, "failed", detail)
        return _finish(episode, "failed", detail)

    # 收尾复检（P2）：max_minutes 只在步前查——最后一步是慢连接器时会超时后仍标 completed。
    # 硬顶是硬顶：超时的剧集按 stopped 收束（已执行步与回执留痕），不许「超时的完成」混进对账。
    if max_minutes is not None and time.monotonic() - started > max_minutes * 60:
        return _finish(episode, "stopped", f"预算硬顶：max_minutes {max_minutes} 用满（末步超时，剧集停）")
    return _finish(episode, "completed")
