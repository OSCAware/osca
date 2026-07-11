"""运行框架的受限求值语义（SPEC v0.4 草案 §4：precondition / emit_when）。

规范说这两处的求值语义由运行框架定义——这里就是定义。原则与触发语法一致：
宁可拒绝（返回「不可求值」交给保守默认），不可猜测。

precondition 可求值形式（其余文本视为不可求值 → 默认放行并留痕）：
    CON-xxx.接口名(参数?) 返回非空

emit_when 可求值形式：以 && 连接的比较子句，字段取自 old.* / new.*：
    old.已关账 == false && new.已关账 == true
比较符 == / !=；字面量 true/false/null、数字、其余按字符串比对。
"""

from __future__ import annotations

import re

PRECONDITION = re.compile(r"(CON-\d{3,4})\.(\S+?)\((.*?)\)\s*返回非空")
CLAUSE = re.compile(r"(old|new)\.([^\s=!]+)\s*(==|!=)\s*(\S+)")

_LITERALS = {"true": True, "false": False, "null": None, "none": None}


def parse_precondition(text: str) -> tuple[str, str, str] | None:
    """返回 (connector_id, 接口名, 参数原文)；不可求值形式返回 None。"""
    m = PRECONDITION.fullmatch(text.strip())
    return (m.group(1), m.group(2), m.group(3).strip()) if m else None


def _literal(token: str):
    lowered = token.strip().strip("'\"")
    if lowered.lower() in _LITERALS:
        return _LITERALS[lowered.lower()]
    try:
        return int(lowered)
    except ValueError:
        try:
            return float(lowered)
        except ValueError:
            return lowered


def _coerce(value):
    """把 YAML/JSON 里的值折到与字面量同一比较域。"""
    return value if not isinstance(value, str) else _literal(value)


def evaluate_emit_when(expr: str, old: dict, new: dict) -> bool | None:
    """求值 emit_when；表达式不合受限形式或字段缺失时返回 None（不发射，留痕）。"""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return None
    result = True
    consumed = 0
    for part in expr.split("&&"):
        m = CLAUSE.fullmatch(part.strip())
        if m is None:
            return None
        side, field, op, literal = m.groups()
        source = old if side == "old" else new
        if field not in source:
            return None
        left, right = _coerce(source[field]), _literal(literal)
        ok = (left == right) if op == "==" else (left != right)
        result = result and ok
        consumed += 1
    return result if consumed else None
