"""审批挑战状态机（M4-W3）：用**绑定挑战**替换旧的无绑定 `set[action]` 授予。

旧机制（policy._granted: set[action]）按动作名授予，任何能发 approve 的调用方批一个动作名，
就放行该动作的**任意**一次执行——confused-deputy（批 A 用于 B）+ 重放（批一次用多次）两大风险源。

本状态机把每次高危动作变成一台**一次性**状态机 `pending → approved | denied → consumed`，
五重绑定，任一不符即 fail-closed：
- **approver**：只有 policy 指定的那个审批人（role=approver 且 name 相符）能批——防冒名/越权批；
- **episode_id**：绑到具体剧集——防跨剧集串用（A 剧集批的授权不能拿去 B 剧集）；
- **payload_digest**：绑到被审批 payload 的 sha256 摘要——防偷梁换柱（批「临期奶 4.5 折」不能应用到别的改价）；
- **expires_at**：过期作废——防陈旧授权被翻出来用；
- **nonce**：一次性随机数 + consume 后即 `consumed`——防重放（同一挑战批准后只能放行一次）。

授权是一次性的：`consume` 成功即 `consumed`，再也用不了；过期/撤销/驳回一律拒绝放行。
本模块是纯状态机（时钟可注入），Host 侧的接线（authz approver 能力集 / 控制通道 approve·deny·challenges /
policy.require_approval 接 consume / connector 传 episode+payload）属 W3.1b。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import time as _wall_clock

# 状态机字面量。pending/approved/denied/consumed 是 §7 契约四态；expired/revoked 是终止旁支。
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
CONSUMED = "consumed"
EXPIRED = "expired"
REVOKED = "revoked"

DEFAULT_TTL_SECONDS = 300.0


def payload_digest(payload: object) -> str:
    """被审批 payload 的稳定 sha256 摘要（防偷梁换柱）。

    规范化 JSON（sort_keys + 紧凑分隔 + 保留非 ASCII）保证同一逻辑 payload → 同一摘要，
    与键序/空白无关。payload 须是 JSON 可序列化的（改价参数这类结构本就该是）。
    """
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Challenge:
    """一台一次性审批状态机。frozen——每次状态迁移产出新对象（可审计、无就地改）。"""

    challenge_id: str
    package_id: str
    action: str
    approver: str
    episode_id: str
    payload_digest: str
    nonce: str
    created_at: float
    expires_at: float
    state: str = PENDING
    decided_by: str | None = None
    decided_at: float | None = None
    consumed_at: float | None = None

    @property
    def binding(self) -> tuple[str, str, str, str]:
        """放行绑定键：package/action/episode/payload 四元组全符才允许 consume。approver 不入放行键——
        approver 决定谁能批，不决定谁能用；用的是运行时按同一绑定找已批挑战。"""
        return (self.package_id, self.action, self.episode_id, self.payload_digest)

    def public(self) -> dict:
        """给控制通道 challenges 命令的脱敏 DTO：不含 nonce（防重放的秘密不外泄）。"""
        return {
            "challenge_id": self.challenge_id,
            "package_id": self.package_id,
            "action": self.action,
            "approver": self.approver,
            "episode_id": self.episode_id,
            "payload_digest": self.payload_digest,
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ChallengeStore:
    """绑定挑战的进程内存储与状态机。线程安全：raise/consume 在剧集线程、decide 在控制通道线程，同一锁。

    时钟可注入（测试控时）。过期是惰性的：每次操作前把到点的 pending/approved 迁到 expired。
    """

    def __init__(self, *, clock: Callable[[], float] = _wall_clock, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self._by_id: dict[str, Challenge] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._ttl = ttl_seconds

    # ── 剧集线程：命中审批门时挂起 / 放行 ──────────────────────────

    def raise_or_get(
        self,
        *,
        package_id: str,
        action: str,
        approver: str,
        episode_id: str,
        payload_digest: str,
        ttl_seconds: float | None = None,
    ) -> Challenge:
        """剧集步命中审批门时挂起一张挑战。同一绑定已有未过期 pending → 复用（幂等，不刷屏）；否则新建。"""
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            key = (package_id, action, episode_id, payload_digest)
            for ch in self._by_id.values():
                if ch.state == PENDING and ch.binding == key:
                    return ch
            ttl = self._ttl if ttl_seconds is None else ttl_seconds
            ch = Challenge(
                challenge_id="CH-" + secrets.token_hex(8),
                package_id=package_id,
                action=action,
                approver=approver,
                episode_id=episode_id,
                payload_digest=payload_digest,
                nonce=secrets.token_hex(16),
                created_at=now,
                expires_at=now + ttl,
            )
            self._by_id[ch.challenge_id] = ch
            return ch

    def consume(self, *, package_id: str, action: str, episode_id: str, payload_digest: str) -> tuple[bool, str]:
        """放行路径（policy.require_approval 调）：找绑定全符的 approved 挑战 → 一次性 consume。

        无匹配（还在 pending / 绑定不符 / 已过期 / 已 consume）一律拒绝——fail-closed。防重放：consume 后即 consumed。
        """
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            key = (package_id, action, episode_id, payload_digest)
            for cid, ch in self._by_id.items():
                if ch.state == APPROVED and ch.binding == key:
                    self._by_id[cid] = replace(ch, state=CONSUMED, consumed_at=now)
                    return True, f"审批已获（{ch.approver}），一次性放行（{cid}）"
            return False, "无匹配的已批准挑战——等待审批人批准，或绑定不符/已过期/已用过（fail-closed）"

    # ── 控制通道线程：审批人批 / 驳 / 撤销 ─────────────────────────

    def decide(self, challenge_id: str, *, by_name: str, by_role: str, approve: bool) -> tuple[bool, str]:
        """approver 经控制通道批/驳。fail-closed：角色须 approver、姓名须与挑战审批人相符、状态须 pending 且未过期。"""
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            ch = self._by_id.get(challenge_id)
            if ch is None:
                return False, f"挑战不存在或已过期：{challenge_id}"
            if by_role != "approver":
                return False, f"角色「{by_role}」无审批权（需 approver）"
            if ch.state != PENDING:
                return False, f"挑战状态为 {ch.state}，不可再裁决（一次性状态机）"
            if by_name != ch.approver:
                return False, f"审批人不符：本挑战指定「{ch.approver}」，来者「{by_name}」——拒绝"
            new_state = APPROVED if approve else DENIED
            self._by_id[challenge_id] = replace(ch, state=new_state, decided_by=by_name, decided_at=now)
            return True, f"挑战 {challenge_id} 已{'批准' if approve else '驳回'}（{by_name}）"

    def revoke(self, challenge_id: str) -> tuple[bool, str]:
        """在线撤销（§7）：pending/approved 未消费的挑战可撤销为 revoked（此后不可放行）。已 consumed 不可撤。"""
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            ch = self._by_id.get(challenge_id)
            if ch is None:
                return False, f"挑战不存在或已过期：{challenge_id}"
            if ch.state == CONSUMED:
                return False, f"挑战 {challenge_id} 已消费——放行已发生，无法撤销"
            if ch.state in (DENIED, REVOKED, EXPIRED):
                return False, f"挑战 {challenge_id} 已是 {ch.state}——无需撤销"
            self._by_id[challenge_id] = replace(ch, state=REVOKED, decided_at=now)
            return True, f"挑战 {challenge_id} 已撤销"

    # ── 只读 ────────────────────────────────────────────────────

    def list_pending(self) -> list[Challenge]:
        """待审批清单（IM 审批卡轮询的输入）：仅 pending 且未过期。"""
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            return [ch for ch in self._by_id.values() if ch.state == PENDING]

    def get(self, challenge_id: str) -> Challenge | None:
        with self._lock:
            self._expire_locked(self._clock())
            return self._by_id.get(challenge_id)

    # ── 内部 ────────────────────────────────────────────────────

    def _expire_locked(self, now: float) -> None:
        """惰性过期：到点的 pending/approved 迁 expired。approved 也会过期——防陈旧授权被翻出来用。"""
        for cid, ch in list(self._by_id.items()):
            if ch.state in (PENDING, APPROVED) and now >= ch.expires_at:
                self._by_id[cid] = replace(ch, state=EXPIRED)
