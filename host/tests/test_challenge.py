"""审批挑战状态机测试（M4-W3.1）——把每种攻击都钉成断言：重放 / 偷梁换柱 / 跨剧集 / 冒名 / 过期。

时钟注入（Clock）控时；store 是纯状态机，无 socket / 无网络。
"""

from __future__ import annotations

from osca_host.challenge import (
    APPROVED,
    CONSUMED,
    DENIED,
    EXPIRED,
    PENDING,
    REVOKED,
    ChallengeStore,
    payload_digest,
)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        self.t += d


def _raise(store: ChallengeStore, **kw):
    d = dict(package_id="pkg", action="折扣<5折", approver="店长", episode_id="EP-1", payload_digest="dig-A")
    d.update(kw)
    return store.raise_or_get(**d)


def _consume(store: ChallengeStore, **kw):
    d = dict(package_id="pkg", action="折扣<5折", episode_id="EP-1", payload_digest="dig-A")
    d.update(kw)
    return store.consume(**d)


def test_raise_creates_pending_and_is_idempotent():
    store = ChallengeStore(clock=Clock())
    a = _raise(store)
    assert a.state == PENDING
    assert _raise(store).challenge_id == a.challenge_id  # 同绑定复用，不刷屏
    assert _raise(store, payload_digest="dig-B").challenge_id != a.challenge_id  # 不同 payload → 新挑战


def test_decide_requires_approver_role_and_matching_name():
    store = ChallengeStore(clock=Clock())
    ch = _raise(store)
    assert not store.decide(ch.challenge_id, by_name="店长", by_role="operator", approve=True)[0]  # 错角色
    assert not store.decide(ch.challenge_id, by_name="路人", by_role="approver", approve=True)[0]  # 冒名
    assert store.get(ch.challenge_id).state == PENDING  # 前两次都没改状态
    ok, _ = store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)
    assert ok and store.get(ch.challenge_id).state == APPROVED


def test_consume_once_then_replay_denied():
    store = ChallengeStore(clock=Clock())
    ch = _raise(store)
    store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)
    assert _consume(store)[0] and store.get(ch.challenge_id).state == CONSUMED
    assert not _consume(store)[0]  # 重放被拒（一次性）


def test_consume_binding_mismatch_denied():
    store = ChallengeStore(clock=Clock())
    ch = _raise(store)
    store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)
    assert not _consume(store, payload_digest="dig-B")[0]  # 偷梁换柱
    assert not _consume(store, episode_id="EP-2")[0]  # 跨剧集
    assert not _consume(store, action="别的动作")[0]  # 动作不符
    assert _consume(store)[0]  # 绑定全符才放行


def test_consume_pending_not_approved_denied():
    store = ChallengeStore(clock=Clock())
    _raise(store)  # 只挂起，未批
    assert not _consume(store)[0]


def test_deny_blocks_consume_and_is_terminal():
    store = ChallengeStore(clock=Clock())
    ch = _raise(store)
    ok, _ = store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=False)
    assert ok and store.get(ch.challenge_id).state == DENIED
    assert not _consume(store)[0]
    assert not store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)[0]  # 已裁决不可再裁


def test_expiry_pending_cannot_be_decided():
    clk = Clock()
    store = ChallengeStore(clock=clk, ttl_seconds=100)
    ch = _raise(store)
    clk.advance(101)
    assert store.list_pending() == []
    assert not store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)[0]


def test_expiry_approved_cannot_be_consumed():
    clk = Clock()
    store = ChallengeStore(clock=clk, ttl_seconds=100)
    ch = _raise(store)
    store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)
    clk.advance(101)
    assert not _consume(store)[0]  # 陈旧授权作废


def test_revoke_blocks_consume_but_not_after_consumed():
    store = ChallengeStore(clock=Clock())
    ch = _raise(store)
    ok, _ = store.revoke(ch.challenge_id)
    assert ok and store.get(ch.challenge_id).state == REVOKED
    assert not _consume(store)[0]
    # 已消费不可撤
    ch2 = _raise(store, episode_id="EP-9")
    store.decide(ch2.challenge_id, by_name="店长", by_role="approver", approve=True)
    assert _consume(store, episode_id="EP-9")[0]
    assert not store.revoke(ch2.challenge_id)[0]


def test_list_pending_excludes_decided():
    store = ChallengeStore(clock=Clock())
    a = _raise(store)
    b = _raise(store, episode_id="EP-2")
    store.decide(a.challenge_id, by_name="店长", by_role="approver", approve=True)
    assert [c.challenge_id for c in store.list_pending()] == [b.challenge_id]


def test_payload_digest_stable_order_independent_and_collision_free():
    assert payload_digest({"a": 1, "b": 2}) == payload_digest({"b": 2, "a": 1})
    assert payload_digest({"price": 4.5}) != payload_digest({"price": 6.0})


def test_public_dto_shape_pinned():
    """DTO 字段钉死（审批卡契约）：裁决痕迹（decided_by/decided_at/consumed_at）不外泄，也没有幽灵字段。"""
    ch = _raise(ChallengeStore(clock=Clock()))
    dto = ch.public()
    assert set(dto) == {
        "challenge_id", "package_id", "action", "approver",
        "episode_id", "payload_digest", "state", "created_at", "expires_at",
    }
    assert dto["state"] == PENDING and dto["challenge_id"] == ch.challenge_id


# ── consume_or_raise：单锁原子（Review W3 收口）───────────────────


def _consume_or_raise(store: ChallengeStore, **kw):
    d = dict(package_id="pkg", action="折扣<5折", approver="店长", episode_id="EP-1", payload_digest="dig-A")
    d.update(kw)
    return store.consume_or_raise(**d)


def test_consume_or_raise_consumes_approved_without_new_pending():
    """已批挑战被消费放行，且**不**另开新 pending——竞态回归：分步 consume→raise 会在此长出第二张。"""
    store = ChallengeStore(clock=Clock())
    ch = _raise(store)
    store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)
    ok, detail, pending = _consume_or_raise(store)
    assert ok and pending is None and ch.challenge_id in detail
    assert store.get(ch.challenge_id).state == CONSUMED
    assert store.list_pending() == []  # 同绑定没有第二张待批


def test_consume_or_raise_reuses_pending_then_creates():
    store = ChallengeStore(clock=Clock())
    ok, _, first = _consume_or_raise(store)
    assert not ok and first.state == PENDING
    ok, _, again = _consume_or_raise(store)
    assert not ok and again.challenge_id == first.challenge_id  # 幂等复用，不刷屏
    ok, _, other = _consume_or_raise(store, payload_digest="dig-B")
    assert not ok and other.challenge_id != first.challenge_id  # 不同绑定 → 新挑战


# ── 终态清出：store 不无限增长（Review W3 收口）───────────────────


def test_terminal_challenges_evicted_after_retention():
    clk = Clock()
    store = ChallengeStore(clock=clk, ttl_seconds=100, terminal_retention_seconds=1000)
    ch = _raise(store)
    store.decide(ch.challenge_id, by_name="店长", by_role="approver", approve=True)
    assert _consume(store)[0]  # → consumed（终态）
    clk.advance(999)
    assert store.get(ch.challenge_id) is not None  # 保留期内可查（排查用）
    clk.advance(2)
    assert store.get(ch.challenge_id) is None  # 超保留期清出


def test_expired_challenges_evicted_after_retention():
    clk = Clock()
    store = ChallengeStore(clock=clk, ttl_seconds=100, terminal_retention_seconds=1000)
    ch = _raise(store)
    clk.advance(101)  # pending → expired（终态计时从 expires_at 起）
    assert store.get(ch.challenge_id).state == EXPIRED
    clk.advance(1000)
    assert store.get(ch.challenge_id) is None
    denied = _raise(store, episode_id="EP-2")
    store.decide(denied.challenge_id, by_name="店长", by_role="approver", approve=False)
    clk.advance(999)
    assert store.get(denied.challenge_id) is not None
    clk.advance(2)
    assert store.get(denied.challenge_id) is None  # denied 同样清出
