"""Connector 代理：manifest 契约、binding 解析、mock 执行、egress、脱敏回执。"""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from osca_host.connector import ConnectorProxy
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats


@pytest.fixture
def mock_dir(tmp_path):
    d = tmp_path / "fixtures"
    d.mkdir()
    (d / "拉取费用明细.yaml").write_text(
        yaml.safe_dump(
            {"已关账": True, "rows": [{"科目": "差旅费", "金额": 45, "经办电话": "13812345678"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def proxy(sample_pack, mock_dir):
    _, loaded = load_for_host(sample_pack)
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))
    bindings = {"FINANCE_DB": {"endpoint": f"mock://{mock_dir}", "secret_ref": "FINANCE_DB_RO_KEY"}}
    return ConnectorProxy(loaded, bindings, policy)


def test_call_ok_with_receipt_and_redaction(proxy):
    receipt = proxy.call("CON-001.拉取费用明细", "2026-07", step="取数")
    assert receipt.ok
    assert receipt.binding_ref == "FINANCE_DB"
    assert receipt.payload["已关账"] is True
    assert "13812345678" not in str(receipt.payload)  # policy.data.redact 注入前脱敏
    assert receipt.redacted == 1


def test_interface_drift_explodes(proxy):
    receipt = proxy.call("CON-001.不存在的接口", step=None)
    assert not receipt.ok and "接口漂移" in receipt.error


def test_step_whitelist_enforced_via_proxy(proxy):
    receipt = proxy.call("CON-001.拉取费用明细", step="成文")
    assert not receipt.ok and "越权" in receipt.error


def test_missing_binding(sample_pack, mock_dir):
    _, loaded = load_for_host(sample_pack)
    policy = PolicyInterceptor(loaded.package_id, {}, {"confirmed": 0, "overruled": 0})
    proxy = ConnectorProxy(loaded, {}, policy)  # 部署环境没注入 FINANCE_DB
    receipt = proxy.call("CON-001.拉取费用明细", step=None)
    assert not receipt.ok and "binding" in receipt.error


def test_egress_default_deny_for_real_endpoint(sample_pack):
    _, loaded = load_for_host(sample_pack)
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))
    bindings = {"FINANCE_DB": {"endpoint": "mysql://db.internal.example:3306/finance"}}
    proxy = ConnectorProxy(loaded, bindings, policy)
    receipt = proxy.call("CON-001.拉取费用明细", step=None)
    assert not receipt.ok and "egress 默认全禁" in receipt.error  # allow_domains 为空


def test_write_path_gated_by_approval_even_for_internal_calls(proxy):
    """写接口审批门（绑定挑战）：step=None 内部调用不豁免；不在清单默认拒绝；批准后一次性消费。"""
    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"

    receipt = proxy.call(ref, step=None)
    assert not receipt.ok and "默认拒绝" in receipt.error  # 不在 approvals 清单——内部调用也没有旁路

    proxy.policy.approvals[ref] = "专家"
    receipt = proxy.call(ref, "2026-07", step="取数", episode_id="EP-1")
    assert not receipt.ok and "审批门拦截" in receipt.error  # 在清单但未批 → 挂 pending 挑战

    # 审批人批准该挑战（挑战绑到本次 episode + params 摘要）
    [ch] = proxy.policy.pending_challenges()
    proxy.policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)
    assert proxy.call(ref, "2026-07", step="取数", episode_id="EP-1").ok  # 同绑定放行一次（mock 执行）
    receipt = proxy.call(ref, "2026-07", step="取数", episode_id="EP-1")
    assert not receipt.ok and "审批门拦截" in receipt.error  # 一次性：consume 后再调即拦


def test_mock_fixture_missing(proxy, sample_pack):
    receipt = proxy.call("CON-001.拉取检修计划期", step=None)  # 固件目录里没有这个接口的文件
    assert not receipt.ok and "mock 固件缺失" in receipt.error


def test_write_params_bind_real_content_and_mock_write_lands(proxy):
    """D1 params 穿透：写摘要绑**真实被写内容**（非空串摘要），批准后 mock 写执行器落地并回显被写内容；
    换 params 即换绑定——偷梁换柱防线用真实内容成立（旧批准消费不到新内容）。"""
    from osca_host.challenge import payload_digest

    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"
    proxy.policy.approvals[ref] = "专家"
    params = {"品类": "浆果", "折扣": 4.5, "起始": "16:30"}

    r = proxy.call(ref, params, step="取数", episode_id="EP-1")  # 挂 pending：摘要绑真实 params
    assert not r.ok and "审批门拦截" in r.error
    [ch] = proxy.policy.pending_challenges()
    assert ch["payload_digest"] == payload_digest(params)
    assert ch["payload_digest"] != payload_digest("")  # 不再是「恒空串摘要」的旧病灶

    proxy.policy.decide_challenge(ch["challenge_id"], by_name="专家", by_role="approver", approve=True)

    # 偷梁换柱（在消费 A 之前独立验）：换内容 B 消费不到 A 的批准 → 另挂一张 pending
    rb = proxy.call(ref, {"品类": "浆果", "折扣": 3.0}, step="取数", episode_id="EP-1")
    assert not rb.ok and "审批门拦截" in rb.error

    # 用回原内容 A → A 的批准一次性消费 → mock 写落地并回显被批准内容
    landed = proxy.call(ref, params, step="取数", episode_id="EP-1")
    assert landed.ok and landed.payload["landed"] is True and landed.payload["mock_write"] == ref
    assert landed.payload["applied"] == params  # 回执回显被批准的被写内容


def test_write_empty_params_rejected_no_empty_digest(proxy):
    """写步未提供被写内容（params 空）→ fail-closed，不生成空串摘要绑定（不对空摘要拍板）。"""
    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"
    proxy.policy.approvals[ref] = "专家"
    r = proxy.call(ref, "", step="取数", episode_id="EP-1")  # 空 params
    assert not r.ok and "被写内容" in r.error
    assert proxy.policy.pending_challenges() == []  # 没挂任何挑战——不生成空串摘要


def test_write_non_json_serializable_params_fail_closed(proxy):
    """写 params 非 JSON 可序列化（如 YAML 原生 date）→ fail-closed 回执，不抛未捕获异常（宁可拒绝不可炸）。"""
    import datetime

    ref = "CON-001.拉取费用明细"
    proxy.connectors["CON-001"]["permissions"]["write"] = "allowed_with_approval"
    proxy.policy.approvals[ref] = "专家"
    r = proxy.call(ref, {"关账日": datetime.date(2026, 7, 8)}, step="取数", episode_id="EP-1")
    assert not r.ok and "JSON 可序列化" in r.error
    assert proxy.policy.pending_challenges() == []  # 坏输入不生成绑定


def test_read_path_ignores_params_no_regression(proxy):
    """读接口（permissions.write=forbidden）不过写审批门、执行器忽略 params——传 params 也不改读回执。"""
    a = proxy.call("CON-001.拉取费用明细", "2026-07", step="取数")
    b = proxy.call("CON-001.拉取费用明细", {"任意": "结构体"}, step="取数")
    assert a.ok and b.ok and a.payload == b.payload  # 读回执与 params 无关


# ── secret 解析（W6-2：可插拔 resolver + fail-closed + 三不：值不进包/日志/剧集） ──

SQL_RO_EP = "sql_readonly://db.internal.example/finance"


class _StaticResolver:
    """测试用可注入 resolver：记录被问的名字、按表返回值（值只在这里，绝不该出现在回执/审计）。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.asked: list[str] = []

    def resolve(self, secret_ref):
        self.asked.append(secret_ref)
        return self.mapping.get(secret_ref)


class _RecordingExecutor:
    """测试用可注入执行器：记录收到的 secret（证明值传给了执行器），回一份 fake payload。
    secret 值只该在这里出现，绝不该到回执/审计——三不的注入型验证锚点。"""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"fake_row": 1}
        self.received_secret = "UNSET"
        self.received_is_write = None

    def execute(self, *, secret, is_write, **kw):
        self.received_secret = secret
        self.received_is_write = is_write
        return self.payload, None


def _real_proxy(sample_pack, bindings, *, resolver=None, allow="db.internal.example", executors=None):
    """egress 放行 allow 的真实执行器代理（非 mock endpoint）——secret 前置由此可达。"""
    _, loaded = load_for_host(sample_pack)
    policy = PolicyInterceptor(
        loaded.package_id, {"policy_version": 1, "egress": {"allow_domains": [allow]}}, ledger_stats(loaded.pack)
    )
    return ConnectorProxy(loaded, bindings, policy, secret_resolver=resolver, executors=executors)


def test_secret_ref_unresolved_fails_closed_name_only(sample_pack, monkeypatch):
    """部署环境没设 → env-var resolver 取不到 → fail-closed；错误只带名、不带值（值本就不存在）。"""
    monkeypatch.delenv("FINANCE_DB_RO_KEY", raising=False)
    proxy = _real_proxy(sample_pack, {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "FINANCE_DB_RO_KEY"}})
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert not r.ok
    assert "未在部署环境解析" in r.error and "FINANCE_DB_RO_KEY" in r.error  # 名可出现


def test_secret_value_passed_to_executor_but_never_leaks(sample_pack):
    """三不 + 交接：解析出的 secret **值传给执行器**（建连接/鉴权），但绝不进回执任何字段/审计日志。"""
    sentinel = "S3CR3T-CONN-STRING-must-never-appear"
    resolver = _StaticResolver({"FINANCE_DB_RO_KEY": sentinel})
    ex = _RecordingExecutor()
    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "FINANCE_DB_RO_KEY"}},
        resolver=resolver,
        executors={"sql_readonly": ex},
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert r.ok  # fake 执行器成功
    assert resolver.asked == ["FINANCE_DB_RO_KEY"]  # 按名解析过
    assert ex.received_secret == sentinel  # 值确实传给了执行器（建连接用）
    blob = repr(r.__dict__) + repr(proxy.policy.audit)
    assert sentinel not in blob  # 但值不在回执/审计任何角落


def test_secret_resolver_is_pluggable(sample_pack):
    resolver = _StaticResolver({"K": "v"})
    ex = _RecordingExecutor()
    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "K"}},
        resolver=resolver,
        executors={"sql_readonly": ex},
    )
    proxy.call("CON-001.拉取费用明细", step=None)
    assert resolver.asked == ["K"] and ex.received_secret == "v"  # 注入的 resolver 被调用、值传给执行器


def test_no_secret_ref_skips_resolution(sample_pack):
    resolver = _StaticResolver({})
    ex = _RecordingExecutor()
    proxy = _real_proxy(
        sample_pack, {"FINANCE_DB": {"endpoint": SQL_RO_EP}}, resolver=resolver, executors={"sql_readonly": ex}
    )  # binding 无 secret_ref
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert resolver.asked == []  # 不问 resolver
    assert r.ok and ex.received_secret is None  # 无凭据需求 → 执行器收到 secret=None


def test_sql_readonly_scheme_host_extracted_for_egress(sample_pack):
    """回归：sql_readonly:// 下划线 scheme 主机名可被抽取（否则 egress 永远拒、secret 前置不可达）。"""
    resolver = _StaticResolver({"K": "v"})
    ex = _RecordingExecutor()
    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "K"}},
        resolver=resolver,
        executors={"sql_readonly": ex},
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert r.ok  # 过了 egress + secret 前置 → 到执行器（host 正确抽取为 db.internal.example）
    assert resolver.asked == ["K"] and ex.received_secret == "v"


def test_secret_not_resolved_before_egress_passes(sample_pack):
    """防御纵深：secret 前置在 egress **之后**——egress 拒时根本不问 resolver（凭据不解析、不外呼）。"""
    resolver = _StaticResolver({"K": "v"})
    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "K"}},
        resolver=resolver,
        allow="other.example",
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert not r.ok and "egress 默认全禁" in r.error
    assert resolver.asked == []  # egress 未过 → resolver 一次都没问


def test_secret_empty_string_from_resolver_fails_closed(sample_pack):
    """对抗审查捉·凭据面：pluggable resolver 返回空串（协议合法 str）→ 强制点须 fail-closed，
    不信任 resolver 自律归一（契约 B.3「空串=没给凭据」落在强制点）。否则 W6-3 拿空串建连接=fail-open。"""
    resolver = _StaticResolver({"K": ""})  # 存储里该密钥值为空串（file/vault 常见），依协议返回 ""
    proxy = _real_proxy(sample_pack, {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "K"}}, resolver=resolver)
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert not r.ok and "未在部署环境解析" in r.error  # 空串 fail-closed，不落执行器桩
    assert "未接入" not in r.error


def test_secret_resolver_exception_fails_closed_no_leak(sample_pack):
    """对抗审查捉·凭据面：resolver 取值抛异常（vault 超时/鉴权失败）→ call() 恒回 fail-closed Receipt（不崩）、
    错误只带名，且异常消息里的值绝不外泄进回执/审计（否则 host log.exception 会把值写进日志）。"""

    sentinel = "postgres://user:S3CR3T-CONN@db"  # 异常消息里携带的连接串（模拟 vault 客户端把值塞进异常）

    class _RaisingResolver:
        def resolve(self, secret_ref):
            raise ConnectionError(f"vault 取值失败：{sentinel}")

    proxy = _real_proxy(
        sample_pack, {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "K"}}, resolver=_RaisingResolver()
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)  # 不抛——恒回 Receipt
    assert not r.ok and "解析出错" in r.error and "K" in r.error  # fail-closed，名可出现
    blob = repr(r.__dict__) + repr(proxy.policy.audit)
    assert sentinel not in blob  # 异常内文（含值）绝不进回执/审计


# ── 执行器分派（W6-3：按 endpoint scheme 选执行器；未注册 / mcp 预留 fail-closed） ──


def test_unknown_scheme_fails_closed(sample_pack):
    """未注册 scheme（如 postgresql://，生产驱动由部署侧注入）→ fail-closed，不猜、不兜底。"""
    proxy = _real_proxy(
        sample_pack, {"FINANCE_DB": {"endpoint": "postgresql://db.internal.example/x"}}, allow="db.internal.example"
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert not r.ok and "不识别的 endpoint scheme" in r.error


def test_mcp_scheme_reserved_not_implemented(sample_pack):
    """mcp:// 预留不实现（W6 范围外）→ fail-closed（与「未识别 scheme」区分，给明确的预留口径）。"""
    proxy = _real_proxy(
        sample_pack, {"FINANCE_DB": {"endpoint": "mcp://srv.internal.example/x"}}, allow="srv.internal.example"
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert not r.ok and "mcp" in r.error and "预留" in r.error


def test_real_sql_readonly_read_end_to_end(sample_pack, tmp_path):
    """端到端 connector → SqlReadonlyExecutor（真 sqlite 只读）：命名参数化查询回结果、经脱敏注入回执。"""
    db = tmp_path / "fin.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE 合并报表_费用明细(单位名称,费用科目,统计周期,环比涨幅,绝对金额)")
    conn.executemany(
        "INSERT INTO 合并报表_费用明细 VALUES(?,?,?,?,?)",
        [("甲厂", "差旅费", "2026-07", 0.3, 45), ("乙厂", "差旅费", "2026-06", 0.1, 20)],
    )
    conn.commit()
    conn.close()
    proxy = _real_proxy(sample_pack, {"FINANCE_DB": {"endpoint": f"sql_readonly://localhost{db}"}}, allow="localhost")
    # 直接传命名参数 dict 验执行器契约（runner→执行器的 params 穿透属 W7 端到端；此处验适配器真跑）
    r = proxy.call("CON-001.拉取费用明细", {"统计周期": "2026-07", "费用科目": None}, step=None)
    assert r.ok, r.error
    assert r.payload == [
        {"单位名称": "甲厂", "费用科目": "差旅费", "统计周期": "2026-07", "环比涨幅": 0.3, "绝对金额": 45}
    ]  # 只回 2026-07 的甲厂（命名参数过滤生效），乙厂 2026-06 被过滤


def test_userinfo_in_endpoint_rejected(sample_pack):
    """GPT 外审 blocker：endpoint authority 含 userinfo（@）→ 拒（egress 抽 allowed、urllib 实连 evil）。"""
    resolver = _StaticResolver({"K": "v"})
    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": "openapi://allowed.example@evil.example/x", "secret_ref": "K"}},
        resolver=resolver,
        allow="allowed.example",
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert not r.ok and "userinfo" in r.error
    assert resolver.asked == []  # 在 egress/secret 之前即拒——不解析凭据


def test_empty_or_illegal_secret_ref_fails_closed(sample_pack):
    """GPT 外审：binding 声明 secret_ref 但为空/非字符串（"" / 0 / false）→ fail-closed（区分「键不存在」）。"""
    for bad in ("", 0, False, 123):
        ex = _RecordingExecutor()
        proxy = _real_proxy(
            sample_pack, {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": bad}}, executors={"sql_readonly": ex}
        )
        r = proxy.call("CON-001.拉取费用明细", step=None)
        assert not r.ok and "secret_ref" in r.error, repr(bad)
        assert ex.received_secret == "UNSET"  # fail-closed，从未分派执行器


def test_reflected_secret_scrubbed_from_receipt(sample_pack):
    """GPT 外审：反射型 API 回显 secret（Bearer/token）→ connector 用**本次** secret 值抹掉，不进回执/剧集/审计。"""
    sentinel = "TKN-reflected-secret-xyz"

    class _ReflectExecutor:
        def execute(self, *, secret, **kw):
            # 远端回显凭据到值、嵌套、列表，甚至 JSON **键**
            return {"echo_auth": f"Bearer {secret}", "nest": {"k": [f"x{secret}y"]}, secret: "in-key"}, None

    resolver = _StaticResolver({"K": sentinel})
    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": SQL_RO_EP, "secret_ref": "K"}},
        resolver=resolver,
        executors={"sql_readonly": _ReflectExecutor()},
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)
    assert r.ok
    blob = repr(r.__dict__) + repr(proxy.policy.audit)
    assert sentinel not in blob  # 反射的 secret 被抹，不进回执/审计
    assert "***secret已脱敏***" in repr(r.payload)  # 抹成标记（证明确实清洗了、非碰巧不含）


def test_executor_exception_never_crashes_call_no_leak(sample_pack):
    """对抗审查捉·契约：执行器抛任意异常绝不炸穿 call()（恒回 Receipt）；异常内文（可能含连接串/secret）不外泄。
    覆盖 sqlite3.Warning（多语句）/ http.client 截断响应 / MemoryError / 可插拔生产驱动的意外异常。"""
    sentinel = "postgres://u:LEAK-TOKEN@db.internal"

    class _BoomExecutor:
        def execute(self, **kw):
            raise RuntimeError(f"驱动内部炸了，连接串：{sentinel}")

    proxy = _real_proxy(
        sample_pack,
        {"FINANCE_DB": {"endpoint": SQL_RO_EP}},
        executors={"sql_readonly": _BoomExecutor()},
    )
    r = proxy.call("CON-001.拉取费用明细", step=None)  # 不抛
    assert not r.ok and "执行器执行异常" in r.error  # 统一 fail-closed 回执
    blob = repr(r.__dict__) + repr(proxy.policy.audit)
    assert sentinel not in blob  # 异常内文（含连接串）绝不进回执/审计
