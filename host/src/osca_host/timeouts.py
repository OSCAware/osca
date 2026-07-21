"""timeout 有界执行契约的共用签名判据（复核 P3）：LLM 通道与 Connector 执行器同一 helper。

判据：只有 timeout 形参可**按关键字**传递（POSITIONAL_OR_KEYWORD / KEYWORD_ONLY），或
**kwargs 兜收，才判定支持——positional-only 的 `timeout` 传 `timeout=` 会 TypeError，
误判成支持等于把 fail-closed 契约做成运行期炸弹。签名不可内省（C 扩展/怪 callable）
同判不支持（fail-closed）。
"""

from __future__ import annotations

import inspect


def supports_keyword_timeout(fn) -> tuple[bool, str]:
    """fn 能否以 `timeout=` 关键字接收剩余时间预算。返回 (支持?, 不支持的人话原因)。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False, "签名不可内省"
    p = params.get("timeout")
    if p is not None and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
        return True, ""
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return True, ""
    if p is not None:  # 形参名叫 timeout 但 positional-only——按关键字传必 TypeError，判不支持
        return False, "timeout 形参为 positional-only（不可按关键字传递）"
    return False, "未声明 timeout 参数（也无 **kwargs）"
