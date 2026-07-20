"""审批挑战状态机（M4-W3）：用**绑定挑战**替换旧的无绑定 `set[action]` 授予。

旧机制（policy._granted: set[action]）按动作名授予，任何能发 approve 的调用方批一个动作名，
就放行该动作的**任意**一次执行——confused-deputy（批 A 用于 B）+ 重放（批一次用多次）两大风险源。

本状态机把每次高危动作变成一台**一次性**状态机 `pending → approved | denied → consumed`，
四重绑定 + 一次性消费，任一不符即 fail-closed：
- **approver**：只有 policy 指定的那个审批人（role=approver 且 name 相符）能批——防冒名/越权批；
- **episode_id**：绑到具体剧集——防跨剧集串用（A 剧集批的授权不能拿去 B 剧集）；
- **payload_digest**：绑到被审批 payload 的 sha256 摘要——防偷梁换柱（批「临期奶 4.5 折」不能应用到别的改价）；
- **expires_at**：过期作废——防陈旧授权被翻出来用；
- **一次性**：`consume` 成功即 `consumed`，同一挑战再也放行不了第二次——防重放由状态机独担
  （挑战 id 本身即高熵随机，不可预测不可枚举）。曾有独立 nonce 字段，但协议无任何一处校验它，
  装饰性防线已删——安全模块的文档必须与代码一致（Review W3 收口）。

本模块是纯状态机（时钟可注入），Host 侧的接线（authz approver 能力集 / 控制通道 approve·deny·challenges /
policy.require_approval 接 consume_or_raise / connector 传 episode+payload）属 W3.1b。
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

# 机制口径的缺省 TTL。M5/M6 审批闭环接通（真写 + IM 人审 + 剧集内挂起等批）时须按人审时延
# 重估——5 分钟对「人在 IM 上看到卡片再拍板」偏短，届时调大或按包/按动作配置。
DEFAULT_TTL_SECONDS = 300.0

# 终态挑战（consumed/denied/expired/revoked）的保留时长：留一段供在场排查，之后惰性清出——
# store 不无限增长（长驻包的 Host 可运行数月）。裁决/放行的审计真相在 policy.audit
# （decide_challenge/_allow/_deny 都 _record），不靠本 store 留史。
TERMINAL_RETENTION_SECONDS = 3600.0

_TERMINAL = (CONSUMED, DENIED, EXPIRED, REVOKED)


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
    created_at: float
    expires_at: float
    state: str = PENDING
    decided_by: str | None = None
    decided_at: float | None = None
    consumed_at: float | None = None
    # 人类可读**脱敏**写内容（W6-4）：= policy.redact(原始 params)，给审批卡呈现供人拍板。与 payload_digest 分离——
    # digest 仍绑**原始** params（防偷梁换柱、写执行器写原文），display 只脱**显示**、不动被写内容（批动作不批 PII）。
    # 默认 None：兼容旧 L2 快照重挂（缺字段回落 None）与不含写内容的挑战。须 JSON 可序列化（随 L2 快照持久）。
    payload_display: object = None

    @property
    def binding(self) -> tuple[str, str, str, str]:
        """放行绑定键：package/action/episode/payload 四元组全符才允许 consume。approver 不入放行键——
        approver 决定谁能批，不决定谁能用；用的是运行时按同一绑定找已批挑战。"""
        return (self.package_id, self.action, self.episode_id, self.payload_digest)

    def public(self) -> dict:
        """给控制通道 challenges 命令的 DTO（IM 审批卡的输入）。"""
        return {
            "challenge_id": self.challenge_id,
            "package_id": self.package_id,
            "action": self.action,
            "approver": self.approver,
            "episode_id": self.episode_id,
            "payload_digest": self.payload_digest,
            "payload_display": self.payload_display,  # 脱敏人类可读写内容（W6-4，审批卡呈现）
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ChallengeStore:
    """绑定挑战的进程内存储与状态机。线程安全：raise/consume 在剧集线程、decide 在控制通道线程，同一锁。

    时钟可注入（测试控时）。过期与终态清出都是惰性的：每次操作前把到点的 pending/approved 迁
    expired、把超保留期的终态挑战清出（_gc_locked）。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = _wall_clock,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        terminal_retention_seconds: float = TERMINAL_RETENTION_SECONDS,
    ):
        self._by_id: dict[str, Challenge] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._ttl = ttl_seconds
        self._retention = terminal_retention_seconds

    # ── 剧集线程：命中审批门时挂起 / 放行 ──────────────────────────

    def consume_or_raise(
        self,
        *,
        package_id: str,
        action: str,
        approver: str,
        episode_id: str,
        payload_digest: str,
        ttl_seconds: float | None = None,
        payload_display: object = None,
    ) -> tuple[bool, str, Challenge | None]:
        """放行或挂起，**单锁原子**——policy.require_approval 的唯一入口。

        先 consume（一次取锁）失败再 raise_or_get（另一次取锁）会留竞态窗：窗内 approver 恰好
        批准了那张 pending，raise 侧只认 PENDING、看不见 APPROVED，会另开一张新挑战——同一绑定
        同时存在已批 + 待批两张，审批人把第二张也批掉，同一逻辑动作就有两次一次性放行额度。
        单锁内先试消费、再复用/挂起，窗口不存在。

        返回 (True, detail, None) = 已批挑战被一次性消费放行；
        (False, detail, challenge) = 无可消费挑战，已复用/挂起一张 pending 供审批人裁决。
        """
        now = self._clock()
        key = (package_id, action, episode_id, payload_digest)
        with self._lock:
            self._gc_locked(now)
            ok, detail = self._consume_locked(key, now)
            if ok:
                return True, detail, None
            existing = self._find_locked(PENDING, key)
            if existing is not None:
                return False, detail, existing
            ch = self._raise_locked(
                package_id=package_id,
                action=action,
                approver=approver,
                episode_id=episode_id,
                payload_digest=payload_digest,
                ttl=self._ttl if ttl_seconds is None else ttl_seconds,
                now=now,
                payload_display=payload_display,
            )
            return False, detail, ch

    def raise_or_get(
        self,
        *,
        package_id: str,
        action: str,
        approver: str,
        episode_id: str,
        payload_digest: str,
        ttl_seconds: float | None = None,
        payload_display: object = None,
    ) -> Challenge:
        """剧集步命中审批门时挂起一张挑战。同一绑定已有未过期 pending → 复用（幂等，不刷屏）；否则新建。"""
        now = self._clock()
        with self._lock:
            self._gc_locked(now)
            existing = self._find_locked(PENDING, (package_id, action, episode_id, payload_digest))
            if existing is not None:
                return existing
            return self._raise_locked(
                package_id=package_id,
                action=action,
                approver=approver,
                episode_id=episode_id,
                payload_digest=payload_digest,
                ttl=self._ttl if ttl_seconds is None else ttl_seconds,
                now=now,
                payload_display=payload_display,
            )

    def consume(self, *, package_id: str, action: str, episode_id: str, payload_digest: str) -> tuple[bool, str]:
        """放行路径：找绑定全符的 approved 挑战 → 一次性 consume。

        无匹配（还在 pending / 绑定不符 / 已过期 / 已 consume）一律拒绝——fail-closed。防重放：consume 后即 consumed。
        """
        now = self._clock()
        with self._lock:
            self._gc_locked(now)
            return self._consume_locked((package_id, action, episode_id, payload_digest), now)

    # ── 控制通道线程：审批人批 / 驳 / 撤销 ─────────────────────────

    def decide(self, challenge_id: str, *, by_name: str, by_role: str, approve: bool) -> tuple[bool, str]:
        """approver 经控制通道批/驳。fail-closed：角色须 approver、姓名须与挑战审批人相符、状态须 pending 且未过期。"""
        now = self._clock()
        with self._lock:
            self._gc_locked(now)
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
        """在线撤销（§7）：pending/approved 未消费的挑战可撤销为 revoked（此后不可放行）。已 consumed 不可撤。

        **预留待接线**：控制通道尚无 revoke 命令、当前零调用方——接线前须先定权限矩阵归属
        （撤销权给 approver 本人、还是 host_admin 应急面，authz.ROLE_CAPS 同步）。状态机先备好。
        """
        now = self._clock()
        with self._lock:
            self._gc_locked(now)
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
            self._gc_locked(now)
            return [ch for ch in self._by_id.values() if ch.state == PENDING]

    def get(self, challenge_id: str) -> Challenge | None:
        with self._lock:
            self._gc_locked(self._clock())
            return self._by_id.get(challenge_id)

    def restore(self, challenge: Challenge) -> None:
        """L2 重挂（M6-W5-D2b）：把持久化的挑战注回 store（重载/重启后恢复挂起写的授权状态机）。同 id 覆盖。
        过期由既有惰性 gc 处理——wall-clock `expires_at` 跨重启仍有效，过期即迁 EXPIRED、恢复走回落。"""
        with self._lock:
            self._by_id[challenge.challenge_id] = challenge

    # ── 内部（全部须持锁调用）────────────────────────────────────

    def _find_locked(self, state: str, key: tuple[str, str, str, str]) -> Challenge | None:
        for ch in self._by_id.values():
            if ch.state == state and ch.binding == key:
                return ch
        return None

    def _consume_locked(self, key: tuple[str, str, str, str], now: float) -> tuple[bool, str]:
        ch = self._find_locked(APPROVED, key)
        if ch is not None:
            self._by_id[ch.challenge_id] = replace(ch, state=CONSUMED, consumed_at=now)
            return True, f"审批已获（{ch.approver}），一次性放行（{ch.challenge_id}）"
        return False, "无匹配的已批准挑战——等待审批人批准，或绑定不符/已过期/已用过（fail-closed）"

    def _raise_locked(
        self,
        *,
        package_id: str,
        action: str,
        approver: str,
        episode_id: str,
        payload_digest: str,
        ttl: float,
        now: float,
        payload_display: object = None,
    ) -> Challenge:
        ch = Challenge(
            challenge_id="CH-" + secrets.token_hex(8),
            package_id=package_id,
            action=action,
            approver=approver,
            episode_id=episode_id,
            payload_digest=payload_digest,
            payload_display=payload_display,
            created_at=now,
            expires_at=now + ttl,
        )
        self._by_id[ch.challenge_id] = ch
        return ch

    def _gc_locked(self, now: float) -> None:
        """惰性过期 + 终态清出：到点的 pending/approved 迁 expired（approved 也过期——防陈旧授权
        被翻出来用）；终态挑战超保留期清出（store 不无限增长；审计真相在 policy.audit）。"""
        for cid, ch in list(self._by_id.items()):
            if ch.state in (PENDING, APPROVED) and now >= ch.expires_at:
                ch = replace(ch, state=EXPIRED)
                self._by_id[cid] = ch
            if ch.state in _TERMINAL:
                settled_at = ch.consumed_at or ch.decided_at or ch.expires_at
                if now - settled_at >= self._retention:
                    del self._by_id[cid]
