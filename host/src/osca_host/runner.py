"""剧集执行器 —— 认知平面的宿主（架构 §5）。

Host 本体（控制平面）确定性、无 LLM；剧集是短命的认知平面，LLM 只活在这里：
由 Host 唤醒时拉起，沿 structure.pipeline 走完即死。跨剧集记忆只存在于
账本与 git，不存在于模型。LLM 通道复用 osca_cli.llm（环境变量配置，不锁定厂商）。

performer 分工（架构 §5，受限集——不可识别的 performer 直接拒绝，不猜）：
- connector：确定性取数，经 Connector 代理（模型只能按名调用），回执入档；
- agent（含 agent + judgments）：LLM 依一次性上下文出草稿，产出注入前过 Policy 脱敏；
  声明 `produces.as: json` 时改出**结构化产出**（M8-T3）：人话草稿与结构化数据**并存**——
  草稿仍进 episode.draft 给人看，结构化数据进 artifacts 给下游写步吃；解析失败一律 fail-closed；
- optimizer：确定性算法寻优——初版贪心（架构原文「初版贪心即可」）：
  候选受限形式 list[dict{value: 数值}]，按 objective 方向排序取最优；缺数值即拒不猜；
- human：审批门与终审——飞轮采集点，机器的流水线到此为止（界面归 M4）；
- runtime：对账步，移交对账器 settle（剧集完成后运行，不消耗剧集）。

三级停之「剧集停」在此落地：pipeline 走完 / budget 硬顶 / 步骤失败，三种终态
都进台账留痕。预算双重：aware.budget（max_steps / max_minutes / max_tokens）
由本执行器裁决，policy per_episode（tool_calls / tokens）由拦截器裁决——笼子优先。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict

import yaml
from osca_cli.llm import LLMError, estimate_tokens, resolve_llm
from osca_cli.triggers import (
    AWARE_BUDGET_KEYS,
    PERFORMERS,
    STRUCTURED_AS,
    parse_performer,
    parse_produces_as,
    step_input_from,
    step_input_key,
    step_produces_key,
)

from osca_host.connector import ConnectorProxy
from osca_host.episode import Episode
from osca_host.lifecycle import finish_episode_state
from osca_host.loader import LoadedPackage
from osca_host.policy import PolicyInterceptor, parse_quantity
from osca_host.timeouts import supports_keyword_timeout

# ── agent 步结构化产出（M8-T3）的信封 ──────────────────────
# 衔接声明的受限语法（`produces.as` 受限词表 STRUCTURED_AS、`input.ref`/`input.from` 取键口径）
# 与 lint 共用 osca_cli.triggers——两边不写第二份解析（lint 过 = Host 跑得动，OSCA042/043）。
# 信封两格：人话草稿给人看（capture 的 agent_draft / frontdesk「依据：」/ 控制台快照都消费 episode.draft），
# 结构化数据给管道吃（下游写步的 body）。**并存不是替换**——两者本就是两件事。
# 之所以要求模型一次输出**一份 JSON 信封**、而不是「人话段 + JSON 段」两段：两段输出必须靠分隔符/围栏
# 扫描切分，那是形状猜测器（猜错即写错内容），与本次要堵的洞同源；一份信封 = 一次 json.loads = 一个
# fail-closed 判据。
DRAFT_KEY = "说明"
DATA_KEY = "数据"
# 结构化产出的体积上限：结构化产物会进恢复快照（持久化）、上审批卡（人读）、原样上 wire——无界即把
# 无界体积灌进这三处。与 llm/openapi 执行器的响应体上限同一纪律（有界才敢解析）。
MAX_STRUCTURED_CHARS = 100_000
# 结构化产出的**嵌套深度**上限。取值不是随手挑的，是按**下游真正扛得住的层数**倒推的（实测，
# CPython 3.12 / sys.getrecursionlimit()=1000，从 run_episode 顶层起算）：
# - 下游 agent 步渲染输入 `_step_user_prompt` → `yaml.safe_dump`：**330 层 ok / 331 层 RecursionError**（最浅）；
# - 写步的 `policy.redact`（审批卡 payload_display / 回执脱敏）：993 层 ok / 994 层 RecursionError；
# - `dataclasses.asdict`（回执入档）≈496 层、`json.dumps`（payload_digest）≈9996 层。
# 而 `json.loads` 自己能收到 **9996 层**——「解析放行、下游炸栈」的落差有 30 倍，炸出来的
# RecursionError 还会**未捕获冲出 run_episode**，剧集停在 status=running（既非 failed 也无 stop_reason）。
# 上限取 32 ≈ 最浅下游上限 330 的 1/10，留一个数量级余量，理由是那 330 **不是常量**：
# ① 它随调用栈**已用**深度浮动——实测垫 200 帧后 329 层就炸（Host 真跑时剧集在事件循环 + 工作线程的
#    深栈上，不是脚本顶层）；② 它随部署侧 sys.getrecursionlimit() 变；③ 下游消费者还会增加，
# 每个消费者的「帧/层」比各不相同（yaml 3 帧、asdict 2 帧、redact 1 帧）。
# 32 层对真实被写内容绰绰有余（一行待写数据的自然深度是 2–4 层）。
MAX_STRUCTURED_DEPTH = 32


def _yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()


def _llm_supports_timeout(llm) -> tuple[bool, str]:
    """LLM 通道是否提供 timeout 有界执行契约。判据与 Connector 执行器**同一 helper**
    （osca_host.timeouts.supports_keyword_timeout，复核 P3）：只认可按关键字传递的 timeout
    形参或 **kwargs；positional-only 的 timeout 判不支持（传 timeout= 必 TypeError）。

    max_minutes 声明为硬顶时这是**强制契约**（GPT 三审 P2）：不支持的适配器 fail-closed 拒绝发起
    ——「只剩数秒仍无限外呼」是把运行时硬预算做成 fail-open。"""
    supported, why = supports_keyword_timeout(llm.complete)
    return (True, "") if supported else (False, f"complete {why}")


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


def _resolve_input(spec: dict, artifacts: dict) -> tuple[str | None, object, str]:
    """取上游产物：`input.ref` 取整份产物，再有 `input.from` 则收窄到字典里的那一格。

    `input.from` 收窄的是「取哪个」，不是「怎么取」——不引入任何条件语法（想写 if 的那一刻，
    它是一条该进 judgments/ 的判断）。取键口径与 lint 共用（osca_cli.triggers）。

    返回 (input_key, 取到的值, 错误人话)；错误非空即 fail-closed（缺产物、产物不是字典、from 悬空）。
    悬空的 from 一律拒绝并列出可选 ref——绝不回落成「取整份」，那会把一坨回执字典当被写内容送上 wire。
    """
    key = step_input_key(spec)
    if key is None:
        return None, None, ""
    if key not in artifacts:
        return key, None, f"上游产物「{key}」缺失——流水线声明与执行不符，直接拒绝"
    value = artifacts[key]
    picked = step_input_from(spec)
    if picked is None:
        return key, value, ""
    if not isinstance(value, dict):
        detail = (
            f"input.from 声明取「{picked}」，但上游产物「{key}」是 {type(value).__name__}、"
            "不是可按 ref 取格的字典——直接拒绝"
        )
        return key, None, detail
    if picked not in value:
        available = "、".join(str(k) for k in value) or "（空）"
        return key, None, f"input.from 指向的「{picked}」不在上游产物「{key}」里——可选 ref：{available}"
    return key, value[picked], ""


class _EnvelopeRejected(ValueError):
    """信封解析期间的定点拒绝（JSON 规范外字面量 / 重复键）——带人话理由，与 JSONDecodeError 区分开。

    继承 ValueError 是刻意的：即便调用方漏了本类的 except，也仍落进既有的 ValueError 分支 fail-closed
    （只是报错人话退成笼统版），绝不会漏成放行。
    """


def _reject_constant(name: str):
    """`json.loads` 的 parse_constant 闸：NaN / Infinity / -Infinity 一律拒。

    这三个**不是 JSON**（RFC 8259 的数字文法里没有它们），Python 的 json 出于历史原因默认收。
    收下去的后果实测过一整条：审批卡显示 `{'金额': nan}`（审批人对着 nan 拍板）、L2 挂起快照落盘成
    `{"artifacts": {"金额": NaN}}`（一份非 Python 读者解不开的非法 JSON 文件）、executor 拼的 wire body
    也是 `{"金额": NaN}` 直接发给写后端。故在**进门处**拒绝，不在下游一处处打补丁。
    """
    raise _EnvelopeRejected(f"含 JSON 规范外的字面量 {name}（RFC 8259 只有数字，没有 NaN/Infinity）")


def _reject_nonfinite(text: str) -> float:
    """`json.loads` 的 parse_float 闸：语法合法但**溢出成 ±inf** 的数值同拒（如 `1e999`）。

    parse_constant 只管字面量，拦不住溢出——而 `1e999` 落进产物后 `json.dumps` 照样吐 `Infinity`，
    落盘/上 wire 的后果与写 `Infinity` 字面量一字不差。同一条纪律，两个进口都得堵。
    """
    value = float(text)
    if not math.isfinite(value):
        raise _EnvelopeRejected(f"数值 {text} 溢出成非有限浮点数（{value}）——落盘/上 wire 时它就是非法 JSON")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """`json.loads` 的 object_pairs_hook 闸：同一对象里出现重复键即拒。

    默认行为是**静默取最后一个**：`{"说明":"A","说明":"B"}` 只剩 B，`{"金额":100,"金额":999999}`
    只剩 999999——「取哪一份」是猜，猜错即写错内容，且台账里谁也看不出还有过第一份。

    取舍：信封层与「数据」内层**共用这一把闸**（hook 天然作用于文档里的每一个 JSON 对象），不为两层
    写两套判据。理由是两层的后果同一个——数据内层的重复键照样进被写内容、进审批卡、进 wire body；
    真要分层区别对待，就得先回答「哪一层的猜是可接受的猜」，而这个问题没有可接受的答案。
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _EnvelopeRejected(f"同一 JSON 对象里出现重复键「{key}」——取哪一份都是猜，直接拒绝")
        seen.add(key)
    return dict(pairs)


def _exceeds_depth(value: object, limit: int) -> bool:
    """结构化数据的嵌套是否超过 limit 层。**迭代**实现（显式栈）——用递归量深度，量到一半自己先炸栈，
    这道闸就成了它要防的那个 bug。"""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: object = node.values()
        elif isinstance(node, (list, tuple)):
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _parse_structured(raw: object) -> tuple[dict | None, str]:
    """结构化 agent 产出的**唯一**解析口径：一份 JSON 信封 `{说明: 人话, 数据: 对象/数组}`。

    解析失败 = 本步失败（调用方 fail-closed 收剧集），**绝不**做任何宽容修复：不剥 markdown 围栏、
    不截半补全、不「解析不了就当文本用」——退回文本等于把「写步 body ＝ 一行真正要写的数据」
    悄悄退化回「body ＝ 一坨读回执」，那正是本次要堵的洞。

    返回 ({"draft": 人话, "data": 结构化数据}, "") 或 (None, 错误人话)。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "模型产出为空——结构化产出解析失败（不退回文本）"
    if len(raw) > MAX_STRUCTURED_CHARS:
        detail = (
            f"模型产出 {len(raw)} 字超过结构化产出上限 {MAX_STRUCTURED_CHARS} 字——拒绝解析"
            "（结构化产物会进恢复快照、上审批卡、原样上 wire，体积须有界）"
        )
        return None, detail
    try:
        # 三把闸都挂在**解析这一次**上（不在下游一处处补）：规范外字面量、溢出成 ±inf 的数值、重复键。
        envelope = json.loads(
            raw,
            parse_constant=_reject_constant,
            parse_float=_reject_nonfinite,
            object_pairs_hook=_no_duplicate_keys,
        )
    except _EnvelopeRejected as e:  # 定点拒绝：报错指名道姓（人看得懂才改得了提示词/包）
        return None, f"模型产出不是合法 JSON：{e}。结构化产出一律 fail-closed，不退回文本"
    except (ValueError, RecursionError) as e:  # JSONDecodeError 属 ValueError；深嵌套触 RecursionError
        return None, f"模型产出不是合法 JSON（{type(e).__name__}）——结构化产出一律 fail-closed，不退回文本"
    if not isinstance(envelope, dict):
        detail = (
            f"模型产出 JSON 顶层是 {type(envelope).__name__}，不是信封对象——"
            f'须为 {{"{DRAFT_KEY}": 人话, "{DATA_KEY}": 结构化数据}}'
        )
        return None, detail
    if unknown := sorted(str(k) for k in envelope if k not in (DRAFT_KEY, DATA_KEY)):
        return None, f"信封含未知键 {unknown}（只认「{DRAFT_KEY}」「{DATA_KEY}」）——宁可拒绝，不可猜测"
    draft = envelope.get(DRAFT_KEY)
    if not isinstance(draft, str) or not draft.strip():
        return None, f"信封的「{DRAFT_KEY}」缺失或不是非空文本——人话草稿与结构化产物**并存**，缺一即拒"
    if DATA_KEY not in envelope:
        return None, f"信封缺「{DATA_KEY}」——结构化产出没有数据可交给下游步骤"
    data = envelope[DATA_KEY]
    if not isinstance(data, (dict, list)):
        return None, f"信封的「{DATA_KEY}」是 {type(data).__name__}，不是对象/数组——下游写步的 body 须是结构化数据"
    if _exceeds_depth(data, MAX_STRUCTURED_DEPTH):
        # 解析器自己能收近万层，下游最浅 330 层就炸栈（见 MAX_STRUCTURED_DEPTH 注释的实测）——
        # 「解析放行、下游炸栈」的落差必须在这里合上，否则 RecursionError 会未捕获冲出 run_episode。
        detail = (
            f"信封的「{DATA_KEY}」嵌套超过 {MAX_STRUCTURED_DEPTH} 层——拒绝解析"
            "（结构化产物要过脱敏、进恢复快照、渲染给下游步骤，深嵌套会在下游炸栈；"
            "一行待写数据的自然深度是 2–4 层）"
        )
        return None, detail
    return {"draft": draft, "data": data}, ""


def _step_user_prompt(spec: dict, step_name: str, input_key: str | None, input_value, structured: bool = False) -> str:
    parts = [f"当前执行 pipeline 步骤「{step_name}」。步骤声明：\n```yaml\n{_yaml(spec)}\n```"]
    if input_key is not None:
        rendered = input_value if isinstance(input_value, str) else _yaml(input_value)
        parts.append(f"输入产物「{input_key}」：\n\n{rendered}")
    if structured:
        # 结构化产出契约（M8-T3）：一份 JSON 信封、两格并存。A6 边界写进提示词——整形的是上游确定性
        # 取数的结果，不是凭空造数；解析不了即本步失败，模型没有「退回文本」这条退路。
        parts.append(
            f"本步骤产出**结构化数据**（produces.as: {'/'.join(STRUCTURED_AS)}）。只输出**一份 JSON 对象**，"
            "前后不要任何解释性文字、不要 markdown 代码围栏：\n\n"
            f'{{"{DRAFT_KEY}": "给人看的人话说明", "{DATA_KEY}": 交给下游步骤的结构化数据（对象或数组）}}\n\n'
            f"「{DRAFT_KEY}」是给人读的草稿，依据 guard 命中判断的段落保留段末判断 ID 标注"
            "（归属纪律；guard 不命中或无法判断的判断不得应用）；"
            f"「{DATA_KEY}」只允许整形上述输入产物里已有的取数结果——**不得凭空造数、"
            "不得补全输入里没有的字段**（公理 A6）。"
            "产出不是合法信封 JSON 时本步骤直接判失败，不会退回当文本使用。"
        )
        return "\n\n".join(parts)
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
    key = step_input_key(spec)
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
    return finish_episode_state(episode, status, reason)


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

    def minutes_remaining() -> float | None:
        """max_minutes 的剩余秒数（未声明 → None）。接口循环内逐个外呼前必查（复核 P2）。"""
        if max_minutes is None:
            return None
        return max_minutes * 60 - (time.monotonic() - started)

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
            # `input.from` 在此收窄取值（M8-T3）：从整份 `{接口ref: 回执}` 收到字典里的那一格。
            resuming = resume_state is not None and index == resume_step
            if resuming:
                rs = resume_state
                payloads, receipts = rs["payloads"], rs["receipts"]
                write_params, start_ref, pending_cid = rs["write_params"], rs["ref_index"], rs["challenge_id"]
                episode.resume = None  # 快照已回灌（若仍待决，下方 _suspend_episode 重写）
                resume_state = None
            else:
                write_params, start_ref, pending_cid = "", 0, None
                input_key, input_value, error = _resolve_input(spec, artifacts)
                if error:
                    _record(episode, step_name, performer, "failed", error)
                    return _finish(episode, "failed", error)
                if input_key is not None:
                    write_params = input_value
                payloads, receipts = {}, []

            fell_back = False
            for ref_i in range(start_ref, len(refs)):
                ref = refs[ref_i]
                # 逐接口 deadline（复核 P2）：一步展开多个 ref 时，首个接口耗尽预算后**不得再发起**
                # 下一个外呼——只在 pipeline 收尾改判 stopped 仍会把后续外部调用真实打出去。
                remaining = minutes_remaining()
                if remaining is not None and remaining <= 0:
                    detail = f"预算硬顶：max_minutes {max_minutes} 用满——接口循环内止步，不再发起下一外呼（剧集停）"
                    _record(episode, step_name, performer, "stopped", detail, receipts=receipts)
                    return _finish(episode, "stopped", detail)
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
                    receipt = proxy.call(
                        ref, write_params, step=step_name, episode_id=episode.episode_id, resume=True, timeout=remaining
                    )
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

                # 剩余预算传导给可阻塞 connector（复核 P2）：支持 timeout 的执行器按剩余秒数收紧外呼上限
                receipt = proxy.call(
                    ref, write_params, step=step_name, episode_id=episode.episode_id, timeout=remaining
                )
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

            artifacts[step_produces_key(spec, step_name)] = payloads
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
            artifacts[step_produces_key(spec, step_name)] = plan
            _record(episode, step_name, performer, "done", detail, output=plan)
            continue

        if kind == "agent":
            input_key, input_value, error = _resolve_input(spec, artifacts)
            if error:
                _record(episode, step_name, performer, "failed", error)
                return _finish(episode, "failed", error)
            structured, error = parse_produces_as(spec)
            if error:
                _record(episode, step_name, performer, "failed", error)
                return _finish(episode, "failed", error)
            if structured and input_key is None:
                # 可溯源纪律（A6 边界的守护）：结构化产出必须有上游产物可整形——无 input 即凭空造数，
                # 拒绝发起调用（lint 亦静态咬同一判据）。
                detail = (
                    f"结构化产出（produces.as: {'/'.join(STRUCTURED_AS)}）的 agent 步必须声明 input——"
                    "结构化产物须可溯源上游产物，无上游产物可整形即凭空造数（公理 A6），拒绝发起调用"
                )
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
            user_prompt = _step_user_prompt(spec, step_name, input_key, input_value, structured=bool(structured))
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
            # 用量自报是不可信输入（源头 osca_cli.llm 已清洗；可插拔注入的 llm 走本处兜底）：
            # 非法上报**不得按 0 记账**（零成本无限过顶）也不得冲减硬顶——runner 看得见 prompt/产出，
            # 与 OpenAICompatLLM 同口径回落字符估算（GPT Review 复审 P1：按 0 计 = 免费绕过 max_tokens）。
            # 估算口径按**原始产出**：外呼已经按原文计费，脱敏/解析都是事后处理，不改变已烧的成本。
            tokens = (
                reply.tokens
                if type(reply.tokens) is int and reply.tokens > 0
                else estimate_tokens(system_prompt, user_prompt, reply.text)
            )
            episode.tokens_used += tokens
            produces_key = step_produces_key(spec, step_name)
            if structured:
                # 结构化产出（M8-T3）：按信封解析原始产出，解析失败即 fail-closed 收剧集——绝不退回文本。
                parsed, error = _parse_structured(reply.text)
                if parsed is None:
                    _record(episode, step_name, performer, "failed", error, tokens=tokens)
                    policy.charge_tokens(episode.episode_id, tokens)  # 外呼已发生：解析失败照记成本，不白嫖额度
                    return _finish(episode, "failed", error)
                # 人话草稿照旧过脱敏（进台账、给人看）；**结构化数据不脱**——它是待写内容，显示脱敏不得改写
                # 被写内容（与 payload_digest 绑原文、payload_display 只脱显示同一纪律，附录 B.4），且它整形自
                # 已在 connector 回执处脱过敏的上游产物。
                text, redacted = policy.redact(parsed["draft"])
                artifacts[produces_key] = parsed["data"]
                episode.draft = text  # 并存：人话草稿仍是 draft，结构化数据另存 artifacts 给下游吃
                _record(
                    episode,
                    step_name,
                    performer,
                    "done",
                    f"LLM 结构化产出：人话草稿 {len(text)} 字 + 「{DATA_KEY}」{type(parsed['data']).__name__}",
                    output=text,
                    structured_output=parsed["data"],
                    produced=produces_key,
                    produced_as=structured,
                    # 可溯源（机器可查）：这份结构化数据整形自哪个上游产物、收窄到哪一格
                    derived_from={"input": input_key, "from": step_input_from(spec)},
                    tokens=tokens,
                    redacted=redacted,
                )
            else:
                text, redacted = policy.redact(reply.text)  # 产出注入剧集台账前脱敏
                artifacts[produces_key] = text
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
