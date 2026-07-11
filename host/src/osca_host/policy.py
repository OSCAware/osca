"""Host 组件 5：Policy 拦截器 —— 笼子的强制执行（架构 §4）。

权限硬管，不靠 AI 自觉：policy.yaml 由运行时读取执行，模型永不读（公理 A5）。
覆盖：按步骤工具白名单（默认拒绝）、审批门、预算硬顶（tool_calls + tokens）、
数据脱敏、kill switch（账本健康度即安全信号，公理 A10）。
每次裁决记审计日志——越权调用直接拒绝并留痕。

tokens 硬顶语义（W5，剧集执行接 LLM 后生效）：用量在调用后由网关回报，
记账后超顶即拒——超顶的那次调用已经发生，剧集就地停；这是止损顶，不是预扣顶。
数量记法受限形式 `<正整数>[k]`（如 200k），不可解析的记警告、硬顶不生效。

W4 边界（诚实标注）：kill_switch 条件的可求值形式为「overruled/confirmed > X」，
按包内全部判断的计数合计近似（「近30天」窗口需要蒸馏管道的时间账，M3 后收紧）；
不可求值的条件记警告、不生效。
"""

from __future__ import annotations

import re
from datetime import datetime

REDACTORS: dict[str, re.Pattern[str]] = {
    "身份证号": re.compile(r"\b\d{17}[\dXx]\b"),
    "手机号": re.compile(r"\b1[3-9]\d{9}\b"),
}

KILL_RATIO = re.compile(r".*overruled\s*/\s*confirmed\s*>\s*([\d.]+).*")
QUANTITY = re.compile(r"(\d+)\s*([kK]?)")

AUDIT_TAIL = 20  # status 里只带最近这些条


def parse_quantity(value) -> int | None:
    """数量记法受限形式：整数或 `<正整数>[k]`（200k → 200000）；其余不可解析返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and (m := QUANTITY.fullmatch(value.strip())):
        return int(m.group(1)) * (1000 if m.group(2) else 1)
    return None


class PolicyInterceptor:
    def __init__(self, package_id: str, policy: dict, ledger_stats: dict[str, int]):
        self.package_id = package_id
        self.audit: list[dict] = []
        self.permissions: dict[str, set[str]] = {
            str(p.get("step")): set(p.get("allow") or [])
            for p in policy.get("permissions") or []
            if isinstance(p, dict)
        }
        budgets = policy.get("budgets") or {}
        per_episode = budgets.get("per_episode") or {}
        self.max_tool_calls = per_episode.get("max_tool_calls")
        self._tool_calls: dict[str, int] = {}  # episode_id → 已用
        self.max_tokens = parse_quantity(per_episode.get("max_tokens")) if "max_tokens" in per_episode else None
        if "max_tokens" in per_episode and self.max_tokens is None:
            detail = "max_tokens 不可解析（受限形式：<正整数>[k]），硬顶不生效"
            self._record("warn", "budgets", str(per_episode["max_tokens"]), detail)
        self._tokens: dict[str, int] = {}  # episode_id → 已用
        self.egress_allow: set[str] = set((policy.get("egress") or {}).get("allow_domains") or [])
        self.redact_categories = [c for c in (policy.get("data") or {}).get("redact") or [] if c in REDACTORS]
        self.approvals: dict[str, str] = {
            str(a.get("action")): str(a.get("approver")) for a in policy.get("approvals") or [] if isinstance(a, dict)
        }
        self._granted: set[str] = set()
        self.kill_tripped, self.kill_reason = self._eval_kill_switch(policy, ledger_stats)

    # ── kill switch（公理 A10：账本健康度即安全信号） ──────────────────

    def _eval_kill_switch(self, policy: dict, stats: dict[str, int]) -> tuple[bool, str]:
        for entry in policy.get("kill_switch") or []:
            condition = str(entry.get("when", "")) if isinstance(entry, dict) else str(entry)
            m = KILL_RATIO.fullmatch(condition)
            if m is None:
                self._record(
                    "warn", "kill_switch", condition, "条件不可机器求值，不生效（受限形式：overruled/confirmed > X）"
                )
                continue
            confirmed, overruled = stats.get("confirmed", 0), stats.get("overruled", 0)
            if confirmed > 0 and overruled / confirmed > float(m.group(1)):
                reason = f"kill switch 触发：overruled/confirmed = {overruled}/{confirmed} > {m.group(1)}"
                self._record("deny", "kill_switch", condition, reason)
                return True, reason
        return False, ""

    # ── 工具白名单（默认拒绝） ─────────────────────────────────────────

    def authorize_tool(self, step: str | None, tool: str, episode_id: str | None = None) -> tuple[bool, str]:
        """step=None 表示运行时内部调用（precondition/watch 轮询），不走模型白名单。"""
        if self.kill_tripped:
            return self._deny(step, tool, self.kill_reason)
        if episode_id is not None and self.max_tool_calls is not None:
            used = self._tool_calls.get(episode_id, 0)
            if used >= self.max_tool_calls:
                return self._deny(step, tool, f"预算硬顶：本剧集 tool_calls 已用满 {self.max_tool_calls}")
            self._tool_calls[episode_id] = used + 1
        if step is None:
            return self._allow(step, tool, "运行时内部调用")
        allowed = self.permissions.get(step)
        if allowed is None:
            return self._deny(step, tool, f"步骤「{step}」不在权限表中（默认拒绝）")
        if tool not in allowed:
            return self._deny(step, tool, f"步骤「{step}」白名单不含 {tool}（模型越权，直接拒绝）")
        return self._allow(step, tool, "白名单放行")

    def charge_tokens(self, episode_id: str, tokens: int) -> tuple[bool, str]:
        """LLM 用量记账（网关调用后回报）。超顶即拒——止损顶：超顶那次调用已发生，剧集就地停。"""
        used = self._tokens.get(episode_id, 0) + tokens
        self._tokens[episode_id] = used
        if self.max_tokens is not None and used > self.max_tokens:
            return self._deny(None, episode_id, f"预算硬顶：本剧集 tokens 已用 {used} > {self.max_tokens}")
        cap = f"/{self.max_tokens}" if self.max_tokens is not None else "（无 tokens 硬顶）"
        return self._allow(None, episode_id, f"tokens 记账：{used}{cap}")

    # ── egress（默认全禁，白名单放行） ────────────────────────────────

    def authorize_egress(self, host: str) -> tuple[bool, str]:
        if any(host == d or host.endswith("." + d) for d in self.egress_allow):
            return self._allow(None, host, "egress 白名单放行")
        return self._deny(None, host, f"egress 默认全禁：{host} 不在 allow_domains")

    # ── 审批门（授予一次用一次；授予入口是控制通道，M4 换审批卡界面） ──

    def require_approval(self, action: str) -> tuple[bool, str]:
        approver = self.approvals.get(action)
        if approver is None:
            return True, "动作不在审批清单，放行"
        if action in self._granted:
            self._granted.discard(action)
            return self._allow(None, action, f"审批已获（{approver}），一次性放行")
        return self._deny(None, action, f"审批门拦截：动作「{action}」需 {approver} 审批")

    def grant_approval(self, action: str) -> tuple[bool, str]:
        if action not in self.approvals:
            return False, f"动作「{action}」不在审批清单中"
        self._granted.add(action)
        self._record("allow", "approval", action, "操作者授予一次性审批")
        return True, f"已授予一次性审批：{action}（审批人应为 {self.approvals[action]}）"

    # ── 脱敏（注入剧集前执行） ─────────────────────────────────────────

    def redact(self, value):
        """递归脱敏字符串值；返回 (脱敏后值, 命中次数)。"""
        hits = 0

        def walk(node):
            nonlocal hits
            if isinstance(node, str):
                for category in self.redact_categories:
                    node, n = REDACTORS[category].subn(f"***{category}已脱敏***", node)
                    hits += n
                return node
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v) for v in node]
            return node

        return walk(value), hits

    # ── 审计 ──────────────────────────────────────────────────────────

    def _allow(self, step, subject, reason) -> tuple[bool, str]:
        self._record("allow", step, subject, reason)
        return True, reason

    def _deny(self, step, subject, reason) -> tuple[bool, str]:
        self._record("deny", step, subject, reason)
        return False, reason

    def _record(self, decision, step, subject, reason) -> None:
        self.audit.append(
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "decision": decision,
                "step": step,
                "subject": subject,
                "reason": reason,
            }
        )

    def snapshot(self) -> dict:
        return {
            "kill_switch_tripped": self.kill_tripped,
            "kill_reason": self.kill_reason or None,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "approvals": {a: ("granted" if a in self._granted else "pending") for a in self.approvals},
            "redact": self.redact_categories,
            "audit_tail": self.audit[-AUDIT_TAIL:],
        }


def ledger_stats(pack) -> dict[str, int]:
    """现役账本计数合计（kill switch 的近似输入；时间窗归 M3 蒸馏管道）。

    只计 status=active：被取代判断的计数随取代冻结成历史——推翻 → 重审 → 蒸馏出
    新判断正是账本自愈，健康度看现役账本，不让已了结的争议永远压着 kill switch。
    """
    confirmed = overruled = 0
    for f in pack.typed_files("judgments"):
        if f.mapping.get("status") != "active":
            continue
        meta = f.mapping.get("meta") or {}
        confirmed += meta.get("confirmed") or 0
        overruled += meta.get("overruled") or 0
    return {"confirmed": confirmed, "overruled": overruled}
