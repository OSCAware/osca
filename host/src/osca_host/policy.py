"""Host 组件 5：Policy 拦截器 —— 笼子的强制执行（架构 §4）。

权限硬管，不靠 AI 自觉：policy.yaml 由运行时读取执行，模型永不读（公理 A5）。
覆盖：按步骤工具白名单（默认拒绝）、审批门、预算硬顶（tool_calls + tokens）、
数据脱敏、kill switch（账本健康度即安全信号，公理 A10）。
每次裁决记审计日志——越权调用直接拒绝并留痕。

tokens 硬顶语义（W5，剧集执行接 LLM 后生效）：用量在调用后由网关回报，
记账后超顶即拒——超顶的那次调用已经发生，剧集就地停；这是止损顶，不是预扣顶。
数量记法受限形式 `<正整数>[k]`（如 200k）。

fail-closed 纪律（Review 七轮定稿）：安全段配置非法时，保守默认必须朝安全侧倒——
「有警告」不能替代安全效果。脱敏配置非法 → 启用全部已知类别（宁可多脱不可泄露）；
预算非法/不可解析 → 额度撤销（0），不是无限额；kill_switch 形状非法 → 按配置错误
停机；审批配置非法 → 一律拒绝。自由文本 kill 条件不可求值仍记警告不生效——那是
SPEC v0.4 §4 给声明性文本的保守默认，与形状非法是两回事。

kill_switch 可求值形式两种（SPEC v0.4 §4）：
- 「overruled/confirmed > X」——按包内现役判断的计数合计近似
  （「近30天」窗口需要蒸馏管道的时间账，后续收紧）；
- 「回放红灯率 > X%」——数据源是回放器（M3 私仓 checkup）生成的健康档案
  `indexes/replay-health.json`（缓存契约，公理 A4）。档案缺失/损坏/越界 →
  条件不生效留痕：这是数据可用性缺口，走声明性文本的保守默认，不是配置错误；
  档案新鲜度未校验（重跑体检由部署侧钩子/巡检保证），诚实标注。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from osca_cli.triggers import parse_quantity  # 数量记法单一真理源（SPEC v0.4 §5，lint OSCA040 同源）

__all__ = ["PolicyInterceptor", "REDACTORS", "ledger_stats", "parse_quantity", "replay_health"]

REDACTORS: dict[str, re.Pattern[str]] = {
    "身份证号": re.compile(r"\b\d{17}[\dXx]\b"),
    "手机号": re.compile(r"\b1[3-9]\d{9}\b"),
}

KILL_RATIO = re.compile(r".*overruled\s*/\s*confirmed\s*>\s*([\d.]+).*")
REPLAY_RED = re.compile(r".*回放红灯率\s*>\s*([\d.]+)\s*%.*")
UNEVALUABLE = "条件不可机器求值，不生效（受限形式：overruled/confirmed > X ｜ 回放红灯率 > X%）"

AUDIT_TAIL = 20  # status 里只带最近这些条


class PolicyInterceptor:
    def __init__(self, package_id: str, policy: dict, ledger_stats: dict[str, int]):
        self.package_id = package_id
        self.audit: list[dict] = []

        # ── 笼子自防：形状错误绝不静默改语义（如关闭脱敏）——留审计警告，lint 之外的第二道闸 ──
        def shape_warn(section: str, value, expected: str) -> None:
            self._record(
                "warn", section, str(value), f"{section} 形状错误（须为 {expected}）——该段配置未生效，请修 policy"
            )

        def as_dict(section: str, value) -> dict:
            if value is None or isinstance(value, dict):
                return value or {}
            shape_warn(section, value, "mapping")
            return {}

        def as_list(section: str, value) -> list:
            if value is None or isinstance(value, list):
                return value or []
            shape_warn(section, value, "list")
            return []

        self.permissions: dict[str, set[str]] = {}
        for p in as_list("permissions", policy.get("permissions")):
            if not isinstance(p, dict) or not isinstance(p.get("step"), str):
                shape_warn("permissions", p, "含 step 字符串的 mapping")
                continue
            allow = p.get("allow")
            if allow is not None and (not isinstance(allow, list) or any(not isinstance(a, str) for a in allow)):
                # 混合/非法列表不部分接受：整个白名单按空处理——默认拒绝（fail-closed）
                shape_warn(f"permissions[{p['step']}].allow", allow, "字符串列表")
                allow = []
            self.permissions[p["step"]] = set(allow or [])

        # 预算：配置非法/不可解析 = 额度撤销（0）——错误预算不是无限额（fail-closed）
        budgets_raw = policy.get("budgets")
        per_raw = budgets_raw.get("per_episode") if isinstance(budgets_raw, dict) else None
        budgets_broken = (budgets_raw is not None and not isinstance(budgets_raw, dict)) or (
            per_raw is not None and not isinstance(per_raw, dict)
        )
        per_episode = per_raw if isinstance(per_raw, dict) else {}
        if budgets_broken:
            self._record(
                "deny",
                "budgets",
                str(budgets_raw),
                "预算配置非法——额度撤销（tool_calls/tokens = 0），修好 policy 再放行",
            )
            self.max_tool_calls: int | None = 0
            self.max_tokens: int | None = 0
        else:

            def quantity_or_revoke(key: str) -> int | None:
                if key not in per_episode:
                    return None  # 未声明 = 无硬顶（合法的显式选择）
                value = parse_quantity(per_episode[key])
                if value is None:
                    detail = f"{key} 不合数量记法（<正整数>[k]）——额度撤销（0），不是无限额"
                    self._record("deny", "budgets", str(per_episode[key]), detail)
                    return 0
                return value

            self.max_tool_calls = quantity_or_revoke("max_tool_calls")
            self.max_tokens = quantity_or_revoke("max_tokens")
        self._tool_calls: dict[str, int] = {}  # episode_id → 已用
        self._tokens: dict[str, int] = {}  # episode_id → 已用

        domains = as_dict("egress", policy.get("egress")).get("allow_domains")
        if domains is not None and (not isinstance(domains, list) or any(not isinstance(d, str) for d in domains)):
            # 混合/非法列表整叶弃用——默认全禁成立（fail-closed）
            shape_warn("egress.allow_domains", domains, "字符串列表")
            domains = []
        self.egress_allow: set[str] = set(domains or [])

        # 脱敏：配置非法（形状/混合元素/未知类别）→ 保守启用全部已知类别——宁可多脱，不可泄露
        redact = as_dict("data", policy.get("data")).get("redact")
        redact_broken = redact is not None and (
            not isinstance(redact, list) or any(not isinstance(c, str) or c not in REDACTORS for c in redact)
        )
        if redact_broken:
            detail = "脱敏配置非法（须为受支持类别的字符串列表）——保守启用全部已知脱敏类别（fail-closed）"
            self._record("deny", "data.redact", str(redact), detail)
            self.redact_categories = list(REDACTORS)
        else:
            self.redact_categories = list(redact or [])

        # 审批：配置非法 → 审批门一律拒绝——清空后按「不在清单」放行是 fail-open，不允许
        approvals_raw = policy.get("approvals")
        self.approvals: dict[str, str] = {}
        self._approvals_broken = approvals_raw is not None and not isinstance(approvals_raw, list)
        for a in approvals_raw if isinstance(approvals_raw, list) else []:
            if isinstance(a, dict) and isinstance(a.get("action"), str) and isinstance(a.get("approver"), str):
                self.approvals[a["action"]] = a["approver"]
            else:
                shape_warn("approvals", a, "含 action/approver 字符串的 mapping")
                self._approvals_broken = True
        if self._approvals_broken:
            self._record(
                "deny",
                "approvals",
                str(approvals_raw),
                "审批配置非法——审批门一律拒绝（fail-closed），修好 policy 再放行",
            )
        self._granted: set[str] = set()
        self.revoked = ""  # 非空即包停/撤销原因——在途剧集在步间与每次调用点看它（三级停之「包停」触达认知平面）
        self._policy = policy  # kill switch 重算用（账本计数变了，条件不变）
        self._warned_conditions: set[str] = set()  # 不可求值条件只警告一次，重算不刷屏
        self.kill_tripped, self.kill_reason = self._eval_kill_switch(policy, ledger_stats)

    # ── kill switch（公理 A10：账本健康度即安全信号） ──────────────────

    def _config_error_kill(self, subject: str, why: str) -> tuple[bool, str]:
        """kill switch 形状非法 = 配置错误即停机（fail-closed）——「不生效」是给自由文本条件的，不给坏形状。"""
        reason = f"kill switch 配置错误（{why}）——配置错误即停机，修好 policy 再启用"
        if reason not in self._warned_conditions:
            self._warned_conditions.add(reason)
            self._record("deny", "kill_switch", subject, reason)
        return True, reason

    def _warn_once(self, condition: str, detail: str) -> None:
        if condition not in self._warned_conditions:
            self._warned_conditions.add(condition)
            self._record("warn", "kill_switch", condition, detail)

    def _eval_kill_switch(self, policy: dict, stats: dict) -> tuple[bool, str]:
        entries = policy.get("kill_switch")
        if entries is not None and not isinstance(entries, list):
            return self._config_error_kill(str(entries), "必须是 list")
        for entry in entries or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("when"), str):
                return self._config_error_kill(str(entry), "每项必须是含 when 字符串的 mapping")
            condition = entry["when"]
            ratio = KILL_RATIO.fullmatch(condition)
            replay = REPLAY_RED.fullmatch(condition) if ratio is None else None
            if ratio is None and replay is None:
                self._warn_once(condition, UNEVALUABLE)
                continue
            try:
                threshold = float((ratio or replay).group(1))
            except ValueError:  # 正则容忍 "." 这类伪数字——按不可求值处理，绝不许炸装载
                self._warn_once(condition, "阈值不可解析，不生效（" + UNEVALUABLE.split("（", 1)[1])
                continue

            if ratio is not None:
                confirmed, overruled = stats.get("confirmed", 0), stats.get("overruled", 0)
                if confirmed > 0 and overruled / confirmed > threshold:
                    reason = f"kill switch 触发：overruled/confirmed = {overruled}/{confirmed} > {ratio.group(1)}"
                    self._record("deny", "kill_switch", condition, reason)
                    return True, reason
                continue

            # 回放红灯率：数据源是回放器生成的健康档案缓存（SPEC v0.4 §4 契约）。
            # 档案缺失/损坏 = 数据可用性缺口 → 保守不生效留痕，不是配置错误不停机。
            rate = stats.get("replay_red_rate")
            if rate is None:
                self._warn_once(
                    condition,
                    "回放健康档案缺失或不可读（indexes/replay-health.json，由回放器 checkup 生成）——条件不生效",
                )
                continue
            if rate * 100 > threshold:
                stamp = stats.get("replay_at") or "时间未知"
                reason = f"kill switch 触发：回放红灯率 {rate:.0%} > {replay.group(1)}%（健康档案 {stamp}）"
                self._record("deny", "kill_switch", condition, reason)
                return True, reason
        return False, ""

    def evaluate_kill_switch(self, ledger_stats: dict[str, int]) -> tuple[bool, str]:
        """按现账本纯计算 kill switch 状态，不改自身（仅审计留痕）——在刷新事务的保护区内调用。"""
        return self._eval_kill_switch(self._policy, ledger_stats)

    def publish_kill_switch(self, tripped: bool, reason: str) -> None:
        """纯赋值发布——与 loaded.pack 的替换配对执行，pack 与 policy 同进退、不存在半发布。"""
        self.kill_tripped, self.kill_reason = tripped, reason

    def refresh_kill_switch(self, ledger_stats: dict[str, int]) -> None:
        """账本计数变了（M3 采集器落账）Host 不重启也要看见——每次唤醒前用现账本重算。"""
        self.publish_kill_switch(*self.evaluate_kill_switch(ledger_stats))

    # ── 包停触达认知平面（三级停之三：撤销后在途剧集步间即停、调用全拒） ──

    def revoke(self, reason: str) -> None:
        self.revoked = reason
        self._record("deny", None, self.package_id, f"包停/撤销：{reason}——在途剧集步间即停，后续调用全部拒绝")

    # ── 工具白名单（默认拒绝） ─────────────────────────────────────────

    def authorize_tool(self, step: str | None, tool: str, episode_id: str | None = None) -> tuple[bool, str]:
        """step=None 表示运行时内部调用（precondition/watch 轮询），不走模型白名单。"""
        if self.revoked:
            return self._deny(step, tool, f"包已停：{self.revoked}")
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
        if self._approvals_broken:
            return self._deny(None, action, "approvals 配置非法——审批门一律拒绝（fail-closed），修好 policy 再放行")
        approver = self.approvals.get(action)
        if approver is None:
            return True, "动作不在审批清单，放行"
        if action in self._granted:
            self._granted.discard(action)
            return self._allow(None, action, f"审批已获（{approver}），一次性放行")
        return self._deny(None, action, f"审批门拦截：动作「{action}」需 {approver} 审批")

    def require_write_approval(self, interface_ref: str) -> tuple[bool, str]:
        """写接口的审批门（Connector 代理写路径必经，内部调用不豁免）。

        与 require_approval 的缺省放行相反：写动作**默认拒绝**——不在审批清单的
        写接口连审批的机会都没有（policy 没给名分的写动作不存在合法路径）。
        """
        if interface_ref not in self.approvals:
            return self._deny(
                None, interface_ref, f"写接口「{interface_ref}」不在 policy approvals 清单——写动作默认拒绝"
            )
        return self.require_approval(interface_ref)

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


def ledger_stats(pack) -> dict:
    """kill switch 的两个可求值输入：现役账本计数合计 + 回放健康档案。

    计数只计 status=active：被取代判断的计数随取代冻结成历史——推翻 → 重审 → 蒸馏出
    新判断正是账本自愈，健康度看现役账本，不让已了结的争议永远压着 kill switch。
    """
    confirmed = overruled = 0
    for f in pack.typed_files("judgments"):
        if f.mapping.get("status") != "active":
            continue
        meta = f.mapping.get("meta")
        if not isinstance(meta, dict):
            continue  # 形状缺陷由 lint 挡；统计自身对任意形状保持总函数
        confirmed += meta.get("confirmed") if type(meta.get("confirmed")) is int else 0  # bool 是 int 子类，不算数
        overruled += meta.get("overruled") if type(meta.get("overruled")) is int else 0
    return {"confirmed": confirmed, "overruled": overruled, **replay_health(pack.root)}


def replay_health(root: Path | str) -> dict:
    """回放健康档案 indexes/replay-health.json（回放器生成的缓存，契约见 SPEC v0.4 §4）。

    缺失/损坏/形状越界（red_rate 须为 [0,1] 数值）→ rate 为 None：条件不生效留痕
    （公理 A4：缓存坏了可重建；安全条件的输入缺口走保守默认，绝不许炸装载）。
    """
    path = Path(root) / "indexes" / "replay-health.json"
    absent = {"replay_red_rate": None, "replay_at": None}
    if not path.is_file():
        return absent
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return absent
    if not isinstance(data, dict):
        return absent
    rate = data.get("red_rate")
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not 0 <= rate <= 1:
        return absent
    stamp = data.get("at")
    return {"replay_red_rate": float(rate), "replay_at": str(stamp) if stamp else None}
