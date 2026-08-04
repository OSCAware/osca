"""JSON 进门闸 —— **一份实现，两个进口共用**。

Host 里有两处「把不可信 JSON 文本收成 Python 对象」的进口，收进来之后流向**同一批下游**：

- `runner._parse_structured`：**模型产出**的结构化信封（agent 步 `produces.as: json`）；
- `executor.OpenapiExecutor.execute`：**读/写后端的响应体**——读回执是下游写 body 的原料，
  写回执进台账给人看。

四把闸（三把挂在解析这一次上、一把在解析之后）：

1. `parse_constant`：`NaN` / `Infinity` / `-Infinity` 一律拒——它们不是 JSON（RFC 8259 的数字文法里没有）；
2. `parse_float`：语法合法但**溢出成 ±inf** 的数值同拒（如 `1e999`）——parse_constant 只管字面量；
3. `object_pairs_hook`：同一对象里的**重复键**——默认行为是静默取最后一个，「取哪一份」是猜；
4. 解析后的**嵌套深度**上限——解析器自己能收近万层，下游最浅 330 层就炸栈（见 MAX_JSON_DEPTH）。

**为什么必须是一份实现**：这四把闸原本只长在模型产出那个进口上。两个进口各写一份的那天起，松的
那个就是没堵的洞——而两个进口的落点一字不差（审批卡、L2 挂起快照、wire body）。闸的全部价值在
「同一把」这三个字上，故本模块只被 import、绝不被复制。

本模块**只解析、不判形状**：「顶层该是信封 / 该是行数组」是各进口自己的契约，不进这把闸。
"""

from __future__ import annotations

import json
import math

# 解析后的**嵌套深度**上限。取值不是随手挑的，是按**下游真正扛得住的层数**倒推的（实测，
# CPython 3.12 / sys.getrecursionlimit()=1000，从顶层起算，二分找「不炸的最大层」）：
#
#   9997  json.loads（解析这一次自己）          9997  json.dumps（payload_digest / 快照落盘 / wire body）
#    996  connector._scrub_secret（反射清洗）    995  policy.redact（注入剧集前脱敏 / 审批卡 display）
#    497  dataclasses.asdict（回执入 steps 台账）
#    330  yaml.safe_dump（下游 agent 步渲染输入，runner._step_user_prompt）  ← **最浅**
#
# 两个进口共用同一个数，理由是**下游是同一批消费者**：模型产出与后端响应体都要过脱敏、进恢复快照、
# 渲染给下游步骤、上 wire；后端响应体还多两个（`_scrub_secret` 与 connector 内即刻的 `redact`），
# 只多不少，最浅的那个仍是 330。给响应体单开一个数，等于给同一个 330 配两个要同步维护的旋钮。
#
# 上限取 32 ≈ 最浅下游 330 的 1/10，留一个数量级余量，理由是那 330 **不是常量**：
# ① 它随调用栈**已用**深度浮动——实测垫 60 帧 → 310 层，垫 200 帧 → 263 层（yaml 约 3 帧/层）；
#    而两个进口都不在顶层跑：executor 那个解析点实测在 `run_episode` 之上 4 帧
#    （run_episode → proxy.call → _execute_real → executor.execute → json.loads），
#    真跑时剧集还压在事件循环 + 工作线程的深栈上；
# ② 它随部署侧 sys.getrecursionlimit() 变；
# ③ 下游消费者还会增加，每个消费者的「帧/层」比各不相同（yaml 3 帧、asdict 2 帧、redact 1 帧）。
# 32 层对真实内容绰绰有余（一行待写数据、一份取数回执的自然深度是 2–4 层）。
#
# 深到把解析器自己也炸栈的那一档（约 9997 层），RecursionError 由 run_episode 的边界兜成 failed
# ——但兜底不是许可：边界只报得出「内部错误：RecursionError」这句笼统话，本闸报的是「嵌套超 32 层」，
# 人看得懂才改得了包 / 查得动后端。
MAX_JSON_DEPTH = 32


class JsonGateRejected(ValueError):
    """过闸时的定点拒绝（规范外字面量 / 溢出 / 重复键 / 深嵌套）——带人话理由，与 JSONDecodeError 区分开。

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
    raise JsonGateRejected(f"含 JSON 规范外的字面量 {name}（RFC 8259 只有数字，没有 NaN/Infinity）")


def _reject_nonfinite(text: str) -> float:
    """`json.loads` 的 parse_float 闸：语法合法但**溢出成 ±inf** 的数值同拒（如 `1e999`）。

    parse_constant 只管字面量，拦不住溢出——而 `1e999` 落进产物后 `json.dumps` 照样吐 `Infinity`，
    落盘/上 wire 的后果与写 `Infinity` 字面量一字不差。同一条纪律，两个进口都得堵。
    """
    value = float(text)
    if not math.isfinite(value):
        raise JsonGateRejected(f"数值 {text} 溢出成非有限浮点数（{value}）——落盘/上 wire 时它就是非法 JSON")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """`json.loads` 的 object_pairs_hook 闸：同一对象里出现重复键即拒。

    默认行为是**静默取最后一个**：`{"说明":"A","说明":"B"}` 只剩 B，`{"金额":100,"金额":999999}`
    只剩 999999——「取哪一份」是猜，猜错即写错内容，且台账里谁也看不出还有过第一份。

    取舍：外层与内层**共用这一把闸**（hook 天然作用于文档里的每一个 JSON 对象），不为两层写两套判据。
    理由是两层的后果同一个——内层的重复键照样进被写内容、进审批卡、进 wire body；真要分层区别对待，
    就得先回答「哪一层的猜是可接受的猜」，而这个问题没有可接受的答案。
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise JsonGateRejected(f"同一 JSON 对象里出现重复键「{key}」——取哪一份都是猜，直接拒绝")
        seen.add(key)
    return dict(pairs)


def exceeds_depth(value: object, limit: int = MAX_JSON_DEPTH) -> bool:
    """value 的嵌套是否超过 limit 层。**迭代**实现（显式栈）——用递归量深度，量到一半自己先炸栈，
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


def loads_guarded(raw: str | bytes | bytearray, *, max_depth: int | None = MAX_JSON_DEPTH) -> object:
    """过闸解析：三把解析期闸 + 解析后的深度闸。**唯一**的 `json.loads` 调用点。

    抛出（调用方一律 fail-closed，绝不「解析不了就当文本用」）：
    - `JsonGateRejected`：定点拒绝，`str(e)` 是人话理由（指名道姓说是哪个字面量/哪个重复键/超几层）；
    - `ValueError`：`json.JSONDecodeError`（语法错）与 `UnicodeDecodeError`（bytes 非 UTF-8）都属它；
    - `RecursionError`：深到把解析器自己炸栈时（深度闸在解析**之后**才量得着，量不到这一档）。
      它**不是** ValueError——调用方必须显式捕获，漏掉即炸穿本次调用。

    max_depth 留成参数（默认即上限，fail-closed）而非写死：深度闸量的是**哪一段**因进口而异——
    执行器量整份响应体，runner 量信封里的「数据」那一格（信封的两格是协议外壳，不该占数据的层数预算）。
    量法与上限仍是同一份（`exceeds_depth` + `MAX_JSON_DEPTH`），传 None 只是把「量哪一段」交回调用方。
    """
    value = json.loads(
        raw,
        parse_constant=_reject_constant,
        parse_float=_reject_nonfinite,
        object_pairs_hook=_no_duplicate_keys,
    )
    if max_depth is not None and exceeds_depth(value, max_depth):
        raise JsonGateRejected(
            f"嵌套超过 {max_depth} 层——深嵌套会在下游炸栈（脱敏、入档、渲染给下游步骤、落盘），"
            "而真实内容的自然深度是 2–4 层"
        )
    return value
