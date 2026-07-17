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

import time
from dataclasses import asdict
from datetime import datetime

import yaml
from osca_cli.llm import LLMError, resolve_llm
from osca_cli.triggers import AWARE_BUDGET_KEYS

from osca_host.connector import ConnectorProxy
from osca_host.episode import Episode
from osca_host.loader import LoadedPackage
from osca_host.policy import PolicyInterceptor, parse_quantity

PERFORMERS = ("human", "connector", "optimizer", "agent", "runtime")  # 分发优先序


def _yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()


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
        parts.append("## 命中判断（依签名检索，各带 1 个代表 case）\n\n```yaml\n" + _yaml(ctx["judgments"]) + "\n```")
        parts.append(
            "## 归属纪律（飞轮口径）\n\n"
            "草稿中凡依据某条命中判断裁决或成文的段落，须在该段落末尾标注其判断 ID（如（J-0417））；"
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
        "只输出本步骤产出物的内容本身，不要输出解释性前后缀；依据命中判断的段落保留段末判断 ID 标注（归属纪律）。"
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
        ranked = sorted(candidates, key=lambda c: float(c["value"]), reverse=direction == "maximize")
    except (TypeError, ValueError, KeyError):
        return None, "候选缺少数值 value 字段——optimizer 不猜数，直接拒绝"
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
    artifacts: dict[str, object] = {}

    for index, spec in enumerate(pipeline):
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

        kind = next((k for k in PERFORMERS if k in performer), None)

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
            payloads: dict[str, object] = {}
            receipts: list[dict] = []
            for ref in refs:
                # 当前 connector-performer 是取数（读）步，不传 params。审批门（require_write_approval）
                # 已按 episode_id + payload(params) 摘要挂绑定挑战，但真写执行未接入（_execute_real 返回未接入）：
                # 待 M5/M6 真写落地时须在此传入模型给出的写 params（否则 payload_digest 恒为空串摘要、绑不住被写内容），
                # 且审批门拦下的写应在**本剧集内**挂起等批后重试消费（而非当场 failed）——否则 episode_id 绑定不可兑现。
                receipt = proxy.call(ref, step=step_name, episode_id=episode.episode_id)
                receipts.append(asdict(receipt))
                if not receipt.ok:
                    # 取数失败即剧集失败：没有取数支撑的草稿是编造（AGENT.md 边界#3）
                    _record(episode, step_name, performer, "failed", receipt.error, receipts=receipts)
                    return _finish(episode, "failed", f"取数失败：{receipt.error}")
                payloads[ref] = receipt.payload
            artifacts[_produces_key(spec, step_name)] = payloads
            _record(episode, step_name, performer, "done", f"取数 {len(refs)} 接口", receipts=receipts)
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
            try:
                llm = llm or resolve_llm()
                reply = llm.complete(
                    system_prompt,
                    _step_user_prompt(spec, step_name, input_key, artifacts.get(input_key)),
                    tag=f"episode/{step_name}",
                )
            except LLMError as e:
                _record(episode, step_name, performer, "failed", str(e))
                return _finish(episode, "failed", str(e))
            text, redacted = policy.redact(reply.text)  # 产出注入剧集台账前脱敏
            episode.tokens_used += reply.tokens
            artifacts[_produces_key(spec, step_name)] = text
            episode.draft = text
            _record(
                episode,
                step_name,
                performer,
                "done",
                f"LLM 产出 {len(text)} 字",
                output=text,
                tokens=reply.tokens,
                redacted=redacted,
            )
            ok, reason = policy.charge_tokens(episode.episode_id, reply.tokens)  # 笼子的止损顶
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

    return _finish(episode, "completed")
