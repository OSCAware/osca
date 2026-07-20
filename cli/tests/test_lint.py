"""osca lint 规则的正反用例。每条规则：最小包应通过，构造违规应命中该规则。"""

from pathlib import Path

from osca_cli.lint import lint_package

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "oper-diagnosis.osca"


def rules_hit(result) -> set[str]:
    return {f.rule for f in result.findings}


# ── 基线 ──


def test_minimal_package_passes(make_pkg, base):
    result = lint_package(make_pkg(base))
    assert result.ok, [f.format() for f in result.findings]
    assert result.warnings == 0


def test_example_package_passes():
    result = lint_package(EXAMPLE)
    assert result.ok, [f.format() for f in result.findings]
    assert result.warnings == 0


def test_missing_dir_is_error():
    result = lint_package("/不存在的路径/x.osca")
    assert not result.ok
    assert "OSCA000" in rules_hit(result)


# ── 包结构 ──


def test_osca001_missing_required_file(make_pkg, base):
    del base["policy.yaml"]
    assert "OSCA001" in rules_hit(lint_package(make_pkg(base)))


def test_osca002_missing_typed_dir_warns(make_pkg, base):
    base = {k: v for k, v in base.items() if not k.startswith("cases/")}
    base["judgments/J-0001.yaml"]["evidence"] = None  # 避免连带 OSCA030 干扰断言
    result = lint_package(make_pkg(base))
    assert "OSCA002" in rules_hit(result)


def test_osca003_broken_yaml(make_pkg, base):
    base["objects/OBJ-001-报告.yaml"] = "object_id: OBJ-001\nname: 报告\n  错误缩进: x\n"
    assert "OSCA003" in rules_hit(lint_package(make_pkg(base)))


def test_osca004_manifest_fields(make_pkg, base):
    base["osca.yaml"] = {"format": "别的", "entry": "不存在.md"}
    result = lint_package(make_pkg(base))
    messages = [f.message for f in result.findings if f.rule == "OSCA004"]
    assert any("format 必须为 osca" in m for m in messages)
    assert any("package_id" in m for m in messages)
    assert any("entry" in m for m in messages)


# ── 命名与 ID ──


def test_osca010_filename_prefix_mismatch(make_pkg, base):
    base["objects/CON-001-错放.yaml"] = {"object_id": "OBJ-009"}
    assert "OSCA010" in rules_hit(lint_package(make_pkg(base)))


def test_osca011_id_field_mismatch(make_pkg, base):
    base["cases/C-0001.yaml"]["case_id"] = "C-0002"
    assert "OSCA011" in rules_hit(lint_package(make_pkg(base)))


def test_osca012_duplicate_id(make_pkg, base):
    base["objects/OBJ-001-重复.yaml"] = dict(base["objects/OBJ-001-报告.yaml"])
    assert "OSCA012" in rules_hit(lint_package(make_pkg(base)))


# ── 引用 ──


def test_osca020_dangling_reference(make_pkg, base):
    base["judgments/J-0001.yaml"]["body"] = "见 J-9999 的裁决。"
    assert "OSCA020" in rules_hit(lint_package(make_pkg(base)))


def test_osca021_missing_binding_ref(make_pkg, base):
    del base["connectors/CON-001-数据源.yaml"]["binding_ref"]
    assert "OSCA021" in rules_hit(lint_package(make_pkg(base)))


def test_osca021_binding_ref_without_template_key(make_pkg, base):
    base["connectors/CON-001-数据源.yaml"]["binding_ref"] = "OTHER_DB"
    assert "OSCA021" in rules_hit(lint_package(make_pkg(base)))


def test_osca022_requires_bindings_mismatch(make_pkg, base):
    base["osca.yaml"]["requires"] = {"bindings": []}
    result = lint_package(make_pkg(base))
    assert "OSCA022" in rules_hit(result)
    assert result.ok  # 警告不挡通过


def test_osca023_policy_step_not_in_structure(make_pkg, base):
    base["policy.yaml"]["permissions"].append({"step": "幽灵步骤", "allow": []})
    assert "OSCA023" in rules_hit(lint_package(make_pkg(base)))


def test_osca024_impl_path_missing(make_pkg, base):
    base["connectors/CON-001-数据源.yaml"]["interfaces"][0]["impl"] = "sql/不存在.sql"
    assert "OSCA024" in rules_hit(lint_package(make_pkg(base)))


# ── 账本纪律 ──


def test_osca030_judgment_without_evidence(make_pkg, base):
    base["judgments/J-0001.yaml"]["evidence"] = []
    assert "OSCA030" in rules_hit(lint_package(make_pkg(base)))


def test_osca030_evidence_dangling(make_pkg, base):
    base["judgments/J-0001.yaml"]["evidence"] = ["C-0404"]
    assert "OSCA030" in rules_hit(lint_package(make_pkg(base)))


def _second_judgment(base, **overrides):
    j = {
        "judgment_id": "J-0002",
        "status": "active",
        "supersedes": "J-0001",
        "signature": {"object": "OBJ-001", "aware": "AW-001", "guard": "金额 > 50"},
        "body": "取代 J-0001。",
        "evidence": ["C-0001"],
        "meta": {"author": "张工", "confirmed": 0, "overruled": 0, "trust": "provisional"},
        "expiry": ["口径变更"],
        "replay": [{"given": "C-0001.input", "with_this_judgment": "改判"}],
    }
    j.update(overrides)
    base["judgments/J-0002.yaml"] = j
    return base


def test_osca031_superseded_status_not_updated(make_pkg, base):
    base = _second_judgment(base)  # J-0001 仍是 active
    assert "OSCA031" in rules_hit(lint_package(make_pkg(base)))


def test_osca031_orphan_superseded(make_pkg, base):
    base["judgments/J-0001.yaml"]["status"] = "superseded"
    assert "OSCA031" in rules_hit(lint_package(make_pkg(base)))


def test_osca031_valid_chain_passes(make_pkg, base):
    base = _second_judgment(base)
    base["judgments/J-0001.yaml"]["status"] = "superseded"
    result = lint_package(make_pkg(base))
    assert "OSCA031" not in rules_hit(result)


def test_osca030_rejects_non_case_evidence(make_pkg, base):
    """证据物种只有 case——引用碰巧存在的别类 ID（对象/判断）不算出生证据。"""
    base["judgments/J-0001.yaml"]["evidence"] = ["OBJ-001"]
    assert "OSCA030" in rules_hit(lint_package(make_pkg(base)))


def test_osca031_self_supersedes_rejected(make_pkg, base):
    base["judgments/J-0001.yaml"]["supersedes"] = "J-0001"
    base["judgments/J-0001.yaml"]["status"] = "superseded"
    result = lint_package(make_pkg(base))
    assert any(f.rule == "OSCA031" and "指向自身" in f.message for f in result.findings)


def test_osca031_supersedes_cycle_rejected(make_pkg, base):
    """互相取代是账本悖论：没有一条现役判断，回放无锚点。"""
    base = _second_judgment(base, status="superseded")
    base["judgments/J-0001.yaml"]["supersedes"] = "J-0002"
    base["judgments/J-0001.yaml"]["status"] = "superseded"
    result = lint_package(make_pkg(base))
    assert any(f.rule == "OSCA031" and "成环" in f.message for f in result.findings)


def test_osca031_fork_supersedes_rejected(make_pkg, base):
    """两条 active 判断同时取代同一旧判断 = 取代分叉——账本只认一条现役后继。"""
    base = _second_judgment(base)  # J-0002 supersedes J-0001
    base["judgments/J-0003.yaml"] = dict(base["judgments/J-0002.yaml"], judgment_id="J-0003")
    base["judgments/J-0001.yaml"]["status"] = "superseded"
    result = lint_package(make_pkg(base))
    assert any(f.rule == "OSCA031" and "分叉" in f.message for f in result.findings)


def test_osca032_trust_should_be_high(make_pkg, base):
    base["judgments/J-0001.yaml"]["meta"] |= {"confirmed": 6, "overruled": 0}
    assert "OSCA032" in rules_hit(lint_package(make_pkg(base)))


def test_osca032_trust_high_not_earned(make_pkg, base):
    base["judgments/J-0001.yaml"]["meta"] |= {"confirmed": 2, "trust": "high"}
    assert "OSCA032" in rules_hit(lint_package(make_pkg(base)))


def test_osca033_invalid_status(make_pkg, base):
    base["judgments/J-0001.yaml"]["status"] = "deleted"
    assert "OSCA033" in rules_hit(lint_package(make_pkg(base)))


def test_osca034_missing_replay(make_pkg, base):
    del base["judgments/J-0001.yaml"]["replay"]
    assert "OSCA034" in rules_hit(lint_package(make_pkg(base)))


def test_osca035_missing_expiry_warns(make_pkg, base):
    del base["judgments/J-0001.yaml"]["expiry"]
    result = lint_package(make_pkg(base))
    assert "OSCA035" in rules_hit(result)
    assert result.ok


def test_osca036_case_missing_effective_set(make_pkg, base):
    base["cases/C-0001.yaml"]["input"] = {"单位": "X"}
    assert "OSCA036" in rules_hit(lint_package(make_pkg(base)))


# ── 必填字段 ──


def test_osca040_object_missing_kind(make_pkg, base):
    del base["objects/OBJ-001-报告.yaml"]["kind"]
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_negative_example_without_why(make_pkg, base):
    base["objects/OBJ-001-报告.yaml"]["examples"]["negative"] = [{"摘录": "坏"}]
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_objective_kind_accepted(make_pkg, base):
    """objective 是合法第五型（SPEC v0.4 §8）——此前被词表拒绝导致 optimizer/settle 不可达。"""
    base["objects/OBJ-002-目标.yaml"] = {
        "object_id": "OBJ-002",
        "name": "示例寻优目标",
        "kind": "objective",
        "version": 1,
        "definition": "演示用寻优目标",
        "optimize": "minimize",
    }
    assert "OSCA040" not in rules_hit(lint_package(make_pkg(base)))


def test_osca040_objective_requires_optimize(make_pkg, base):
    base["objects/OBJ-002-目标.yaml"] = {
        "object_id": "OBJ-002",
        "name": "示例寻优目标",
        "kind": "objective",
        "version": 1,
        "definition": "演示用寻优目标",
    }
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_connector_write_permission(make_pkg, base):
    base["connectors/CON-001-数据源.yaml"]["permissions"] = {"write": "随便写"}
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_aware_enabled_not_bool(make_pkg, base):
    base["aware/AW-001-定时.yaml"]["enabled"] = "yes 吧"
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_trigger_kind_invalid(make_pkg, base):
    base["aware/AW-001-定时.yaml"]["triggers"][0]["kind"] = "魔法"
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_judgment_signature_incomplete(make_pkg, base):
    del base["judgments/J-0001.yaml"]["signature"]["guard"]
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


# ── 审批授权 TTL（W6-1：default_ttl_seconds 顶层 + approvals 每项 ttl_seconds，须正有限数秒） ──


def test_osca040_default_ttl_seconds_rejects_illegal(make_pkg, base):
    """形状错误在装载前挡（policy 是笼子）——与 host policy._parse_ttl 的合法判定一致。"""
    for bad in (-5, 0, "banana", float("inf"), float("nan"), True, 10**400):
        base["policy.yaml"]["default_ttl_seconds"] = bad
        assert "OSCA040" in rules_hit(lint_package(make_pkg(base))), f"未拒绝非法 default_ttl_seconds={bad!r}"


def test_osca040_default_ttl_seconds_valid_passes(make_pkg, base):
    base["policy.yaml"]["default_ttl_seconds"] = 900
    assert "OSCA040" not in rules_hit(lint_package(make_pkg(base)))


def test_osca040_approval_ttl_seconds_rejects_illegal(make_pkg, base):
    base["policy.yaml"]["approvals"] = [{"action": "改价", "approver": "店长", "ttl_seconds": 0}]
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_approval_ttl_seconds_valid_passes(make_pkg, base):
    base["policy.yaml"]["approvals"] = [{"action": "改价", "approver": "店长", "ttl_seconds": 1800}]
    assert "OSCA040" not in rules_hit(lint_package(make_pkg(base)))


def test_osca040_duplicate_approval_action_rejected(make_pkg, base):
    """GPT 外审：重复 action 致 approver/TTL 覆盖歧义——lint 挡下（运行时也清旧覆盖）。"""
    base["policy.yaml"]["approvals"] = [{"action": "改价", "approver": "店长"}, {"action": "改价", "approver": "老板"}]
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_forbidden_connector_rejects_write_method(make_pkg, base):
    """GPT 外审 blocker：write: forbidden 连接器接口声明写 method（POST…）→ lint 挡（否则绕审批门真写）。"""
    base["connectors/CON-001-数据源.yaml"]["interfaces"][0]["method"] = "POST"
    assert "OSCA040" in rules_hit(lint_package(make_pkg(base)))


def test_osca040_forbidden_connector_get_method_ok(make_pkg, base):
    base["connectors/CON-001-数据源.yaml"]["interfaces"][0]["method"] = "GET"
    assert "OSCA040" not in rules_hit(lint_package(make_pkg(base)))


# ── 总函数纪律：任意 YAML 形状只报错、不崩溃 ──

SHAPE_MUTATIONS = [
    ("osca.yaml", "requires"),
    ("policy.yaml", "permissions"),
    ("policy.yaml", "budgets"),
    ("policy.yaml", "approvals"),
    ("policy.yaml", "egress"),
    ("policy.yaml", "data"),
    ("policy.yaml", "kill_switch"),
    ("structure.yaml", "pipeline"),
    ("objects/OBJ-001-报告.yaml", "examples"),
    ("objects/OBJ-001-报告.yaml", "kind"),
    ("connectors/CON-001-数据源.yaml", "interfaces"),
    ("connectors/CON-001-数据源.yaml", "permissions"),
    ("connectors/CON-001-数据源.yaml", "binding_ref"),
    ("aware/AW-001-定时.yaml", "triggers"),
    ("aware/AW-001-定时.yaml", "gate"),
    ("aware/AW-001-定时.yaml", "budget"),
    ("judgments/J-0001.yaml", "meta"),
    ("judgments/J-0001.yaml", "signature"),
    ("judgments/J-0001.yaml", "evidence"),
    ("judgments/J-0001.yaml", "replay"),
    ("judgments/J-0001.yaml", "supersedes"),
    ("judgments/J-0001.yaml", "status"),
    ("cases/C-0001.yaml", "input"),
]


def test_lint_total_over_arbitrary_shapes(make_pkg, base):
    """lint 是总函数（YAML 类型变异矩阵）：任意字段形状只产出 findings，绝不抛异常——
    包解析边界面对不可信 YAML 不许崩，Host 首次 load 的控制请求也就不会断。"""
    import copy

    for relpath, fieldname in SHAPE_MUTATIONS:
        for bad in ([1, 2], {"意外": 1}, "文本", 42, True):
            mutated = copy.deepcopy(base)
            mutated[relpath][fieldname] = bad
            lint_package(make_pkg(mutated))  # 不抛异常即通过（多数形状同时应报错，见下条）


def test_runtime_critical_shape_errors_reported(make_pkg, base):
    """运行时按键取值的字段，形状错误必须在 lint 就挡下——不能等剧集线程里炸。"""
    import copy

    critical = [
        ("judgments/J-0001.yaml", "meta"),
        ("objects/OBJ-001-报告.yaml", "examples"),
        ("connectors/CON-001-数据源.yaml", "permissions"),
        ("aware/AW-001-定时.yaml", "budget"),
        ("policy.yaml", "budgets"),
        ("structure.yaml", "pipeline"),
    ]
    for relpath, fieldname in critical:
        mutated = copy.deepcopy(base)
        mutated[relpath][fieldname] = ["列表不是这里该有的形状"]
        result = lint_package(make_pkg(mutated))
        assert not result.ok, f"{relpath} 的 {fieldname}=list 应报 ERROR"


def test_policy_leaf_shape_errors_reported(make_pkg, base):
    """Policy 运行时消费的叶子字段：形状错误必须 ERROR——脱敏/白名单不能被静默关闭。"""
    import copy

    leaves = [
        ("data", {"redact": "身份证号"}),  # 字符串会被逐字符遍历 → 脱敏静默关闭
        ("egress", {"allow_domains": "oscaware.com"}),
        ("permissions", [{"step": "取数", "allow": "CON-001.取数"}]),
        ("permissions", ["oops"]),
        ("approvals", ["oops"]),
        ("kill_switch", ["oops"]),
    ]
    for fieldname, bad in leaves:
        mutated = copy.deepcopy(base)
        mutated["policy.yaml"][fieldname] = bad
        result = lint_package(make_pkg(mutated))
        assert not result.ok, f"policy.{fieldname}={bad!r} 应报 ERROR"


def test_sequence_element_shape_errors_reported(make_pkg, base):
    """外层 list 合法不等于元素合法：非 mapping 元素会被运行时静默丢弃，必须 ERROR。"""
    import copy

    element_cases = [
        ("aware/AW-001-定时.yaml", "triggers", ["oops"]),  # 「显示启用、实际永不触发」
        ("judgments/J-0001.yaml", "replay", ["oops"]),
        ("objects/OBJ-001-报告.yaml", "examples", {"negative": ["oops"]}),
        ("structure.yaml", "pipeline", ["oops"]),
    ]
    for relpath, fieldname, bad in element_cases:
        mutated = copy.deepcopy(base)
        mutated[relpath][fieldname] = bad
        result = lint_package(make_pkg(mutated))
        assert not result.ok, f"{relpath} 的 {fieldname}={bad!r} 应报 ERROR"


def test_bool_counts_rejected(make_pkg, base):
    """bool 是 int 子类：true/false 混进计数会污染 trust 与 kill switch——必须 ERROR。"""
    import copy

    mutated = copy.deepcopy(base)
    mutated["judgments/J-0001.yaml"]["meta"]["confirmed"] = True
    result = lint_package(make_pkg(mutated))
    assert any(f.rule == "OSCA040" and "布尔" in f.message for f in result.findings)


def test_legal_shape_illegal_value_rejected(make_pkg, base):
    """合法形状、非法值同样必须 ERROR——否则不绕过 lint 也能削弱脱敏/预算/kill switch。"""
    import copy

    value_cases = [
        ("policy.yaml", "data", {"redact": ["身份证"]}),  # 未知类别 → 运行时过滤成空
        ("policy.yaml", "kill_switch", [{"when": ["not", "string"]}]),  # truthy 非字符串
        ("policy.yaml", "budgets", {"per_episode": {"max_tool_calls": "unlimited"}}),  # 记法非法 → 无限额
        ("policy.yaml", "budgets", {"per_episode": {"max_tokens": -5}}),
        ("policy.yaml", "budgets", {"per_epiosde": {"max_tokens": 1}}),  # 外层拼写错误 → 静默无限额
        ("aware/AW-001-定时.yaml", "budget", {"max_steps": "很多步"}),
        ("policy.yaml", "permissions", [{"step": "取数"}]),  # 缺 allow——白名单必须显式
        ("structure.yaml", "pipeline", [{"step": "", "performer": "agent"}]),  # 空 step
        ("structure.yaml", "pipeline", [{"performer": "agent"}]),  # 缺 step
    ]
    for relpath, fieldname, bad in value_cases:
        mutated = copy.deepcopy(base)
        mutated[relpath][fieldname] = bad
        result = lint_package(make_pkg(mutated))
        assert not result.ok, f"{relpath} 的 {fieldname}={bad!r} 应报 ERROR"


def test_budget_keys_split_by_runtime_contract(make_pkg, base):
    """预算键按运行时真实契约拆分：声明了没人执行的硬顶 = fail-open，必须 ERROR。"""
    import copy

    cases_ = [
        ("aware/AW-001-定时.yaml", "budget", {"max_tool_calls": 1}),  # 剧集执行器不执行它
        ("aware/AW-001-定时.yaml", "budget", {"banana": 1}),  # 未知键
        ("policy.yaml", "budgets", {"per_episode": {"max_steps": 1}}),  # Policy 拦截器不执行它
    ]
    for relpath, fieldname, bad in cases_:
        mutated = copy.deepcopy(base)
        mutated[relpath][fieldname] = bad
        result = lint_package(make_pkg(mutated))
        assert not result.ok, f"{relpath} 的 {fieldname}={bad!r} 应报 ERROR"


def test_shape_findings_are_locatable_not_backstop(make_pkg, base):
    """诊断可定位：形状错误报在对应文件的正常 finding，不退化为 run_all 兜底的「.」。"""
    import copy

    probes = [
        ("osca.yaml", "requires", {"bindings": 42}),
        ("structure.yaml", "pipeline", [{"step": ["不可哈希"], "performer": "agent"}]),
        ("judgments/J-0001.yaml", "judgment_id", ["J-0001"]),
    ]
    for relpath, fieldname, bad in probes:
        mutated = copy.deepcopy(base)
        mutated[relpath][fieldname] = bad
        result = lint_package(make_pkg(mutated))
        assert not any("规则执行异常" in f.message for f in result.findings), f"{relpath}.{fieldname} 走了兜底"
        assert not result.ok, f"{relpath}.{fieldname}={bad!r} 应报 ERROR"


# ── 安全铁律 ──


def test_osca050_url_in_package(make_pkg, base):
    base["connectors/CON-001-数据源.yaml"]["rationale"] = "详见 https://internal.example.com/doc"
    assert "OSCA050" in rules_hit(lint_package(make_pkg(base)))


def test_osca050_whitelisted_doc_link_allowed(make_pkg, base):
    base["AGENT.md"] = "# 演示\n规范出处：https://oscaware.com 与 https://creativecommons.org/licenses/by/4.0/\n"
    result = lint_package(make_pkg(base))
    assert "OSCA050" not in rules_hit(result)


def test_osca050_plain_http_forbidden_even_whitelisted(make_pkg, base):
    base["AGENT.md"] = "# 演示\n参见 http://oscaware.com\n"
    result = lint_package(make_pkg(base))
    messages = [f.message for f in result.findings if f.rule == "OSCA050"]
    assert any("明文 http" in m for m in messages)


def test_osca050_scans_markdown_and_sql(make_pkg, base):
    base["AGENT.md"] = "# 演示\n内部系统：https://erp.internal.corp/login\n"
    base["sql/query.sql"] = "-- jdbc:oracle:thin:@db-host:1521/PROD\nSELECT 1;\n"
    result = lint_package(make_pkg(base))
    hits = {f.path for f in result.findings if f.rule == "OSCA050"}
    assert hits == {"AGENT.md", "sql/query.sql"}


def test_osca050_connection_string(make_pkg, base):
    base["bindings.example.yaml"]["DEMO_DB"]["endpoint"] = "mysql://root:pass@10.0.0.1/db"
    assert "OSCA050" in rules_hit(lint_package(make_pkg(base)))


def test_osca050_private_key(make_pkg, base):
    base["cases/C-0001.yaml"]["input"]["附件"] = "-----BEGIN RSA PRIVATE KEY-----"
    assert "OSCA050" in rules_hit(lint_package(make_pkg(base)))


# ── 触发原语与闸门受限语法（SPEC v0.4 草案 §5） ──


def test_osca041_free_text_schedule(make_pkg, base):
    base["aware/AW-001-定时.yaml"]["triggers"] = [{"id": "T1", "kind": "schedule", "schedule": "每月9日 09:00"}]
    result = lint_package(make_pkg(base))
    assert "OSCA041" in rules_hit(result)
    assert any("自由文本已废止" in f.message for f in result.findings)


def test_osca041_gate_contradiction(make_pkg, base):
    base["aware/AW-001-定时.yaml"]["gate"] = {"combine": "sequence"}
    result = lint_package(make_pkg(base))
    assert any(f.rule == "OSCA041" and "矛盾" in f.message for f in result.findings)


def test_osca041_watch_duration(make_pkg, base):
    base["aware/AW-001-定时.yaml"]["triggers"] = [
        {"id": "T2", "kind": "watch", "uses": "CON-001.取数", "every": "一天"}
    ]
    result = lint_package(make_pkg(base))
    assert any(f.rule == "OSCA041" and "every=一天" in f.message for f in result.findings)


# ── 分层与权属（OSCA060）──


def test_osca060_missing_trio_warns_not_blocks(make_pkg, base):
    for key in ("scope", "provenance", "classification"):
        del base["judgments/J-0001.yaml"][key]
    result = lint_package(make_pkg(base))
    assert "OSCA060" in rules_hit(result)
    assert result.ok  # 存量过渡期：warn 不拦（新生判断必填由蒸馏/Creator 出生时保证）


def test_osca060_bad_enums_are_errors(make_pkg, base):
    j = base["judgments/J-0001.yaml"]
    j["scope"] = "global"
    j["provenance"]["origin"] = "找不到出处"
    j["classification"] = "secret"
    result = lint_package(make_pkg(base))
    assert not result.ok
    messages = [f.message for f in result.findings if f.rule == "OSCA060"]
    assert any("scope=" in m for m in messages)
    assert any("provenance.origin=" in m for m in messages)
    assert any("classification=" in m for m in messages)


def test_osca060_unhashable_values_report_precise_field(make_pkg, base):
    """不可哈希叶子（list/mapping）不得让规则崩进 run_all 兜底——报错须精确到字段、
    且同规则其余 findings 不被吞（此前 set 成员测试直接 TypeError）。"""
    j = base["judgments/J-0001.yaml"]
    j["scope"] = ["commons"]  # YAML 手滑写成列表
    j["provenance"]["origin"] = {"kind": "own-ops"}
    j["classification"] = ["public"]
    result = lint_package(make_pkg(base))
    assert not result.ok
    messages = [f.message for f in result.findings if f.rule == "OSCA060"]
    assert any("scope=" in m for m in messages)
    assert any("provenance.origin=" in m for m in messages)
    assert any("classification=" in m for m in messages)
    assert not any("规则执行异常" in f.message for f in result.findings)


def test_osca060_provenance_shape_and_missing_keys(make_pkg, base):
    base["judgments/J-0001.yaml"]["provenance"] = "京郊某处"  # 非 mapping
    result = lint_package(make_pkg(base))
    assert not result.ok and any(
        "provenance 必须是 mapping" in f.message for f in result.findings if f.rule == "OSCA060"
    )
    base["judgments/J-0001.yaml"]["provenance"] = {"origin": "own-ops"}  # 缺 source/rights
    result = lint_package(make_pkg(base))
    assert not result.ok
    messages = [f.message for f in result.findings if f.rule == "OSCA060"]
    assert any("缺 source" in m for m in messages) and any("缺 rights" in m for m in messages)


def test_osca060_cleanroom_client_derived_cannot_enter_commons(make_pkg, base):
    j = base["judgments/J-0001.yaml"]
    j["scope"] = "commons"  # provenance.origin 仍是 client-derived
    j["classification"] = "public"
    result = lint_package(make_pkg(base))
    assert not result.ok
    assert any("洁净室" in f.message for f in result.findings if f.rule == "OSCA060")


def test_osca060_commons_requires_public_classification(make_pkg, base):
    j = base["judgments/J-0001.yaml"]
    j["scope"] = "commons"
    j["provenance"] = {"origin": "own-ops", "source": "自营外呼", "rights": "vendor-owned"}
    j["classification"] = "internal"
    result = lint_package(make_pkg(base))
    assert not result.ok
    assert any("无密级" in f.message for f in result.findings if f.rule == "OSCA060")


def test_osca060_valid_commons_entry_passes(make_pkg, base):
    j = base["judgments/J-0001.yaml"]
    j["scope"] = "commons"
    j["provenance"] = {"origin": "public-standard", "source": "GB/T 9704", "rights": "vendor-owned"}
    j["classification"] = "public"
    result = lint_package(make_pkg(base))
    assert result.ok, [f.format() for f in result.findings]
    assert result.warnings == 0


# ── osca.yaml 包级分层默认段（OSCA061）──


def test_osca061_absent_layering_ok(make_pkg, base):
    # 无 layering 默认段 = 合法（judgment 缺字段自有 OSCA060 warn，不由 061 报）
    assert "layering" not in base["osca.yaml"]
    result = lint_package(make_pkg(base))
    assert "OSCA061" not in rules_hit(result)


def test_osca061_valid_default_passes(make_pkg, base):
    base["osca.yaml"]["layering"] = {
        "scope": "org",
        "provenance": {"origin": "client-derived", "source": "demo-group", "rights": "client-owned"},
        "classification": "internal",
    }
    result = lint_package(make_pkg(base))
    assert result.ok, [f.format() for f in result.findings]
    assert "OSCA061" not in rules_hit(result)


def test_osca061_partial_default_ok(make_pkg, base):
    # 可部分声明：只给 scope + classification（provenance 缺整段），不报（缺整段 ≠ 形状缺陷）
    base["osca.yaml"]["layering"] = {"scope": "org", "classification": "internal"}
    result = lint_package(make_pkg(base))
    assert "OSCA061" not in rules_hit(result)


def test_osca061_bad_enum_is_error(make_pkg, base):
    base["osca.yaml"]["layering"] = {"scope": "global", "classification": "secret"}
    result = lint_package(make_pkg(base))
    assert not result.ok
    messages = [f.message for f in result.findings if f.rule == "OSCA061"]
    assert any("scope=" in m for m in messages)
    assert any("classification=" in m for m in messages)


def test_osca061_incomplete_provenance_is_error(make_pkg, base):
    # provenance present 即须完整（缺 source/rights）——错默认会污染整包新生判断，源头拦
    base["osca.yaml"]["layering"] = {"provenance": {"origin": "own-ops"}}
    result = lint_package(make_pkg(base))
    assert not result.ok
    messages = [f.message for f in result.findings if f.rule == "OSCA061"]
    assert any("缺 source" in m for m in messages) and any("缺 rights" in m for m in messages)


def test_osca061_cleanroom_violation_is_error(make_pkg, base):
    # 洁净室在 osca.yaml 源头就拦：commons + client-derived 默认会给整包新生判断洗成 commons
    base["osca.yaml"]["layering"] = {
        "scope": "commons",
        "provenance": {"origin": "client-derived", "source": "demo-group", "rights": "client-owned"},
        "classification": "public",
    }
    result = lint_package(make_pkg(base))
    assert not result.ok
    assert any("洁净室" in f.message for f in result.findings if f.rule == "OSCA061")


def test_osca061_unhashable_values_report_precise_field(make_pkg, base):
    base["osca.yaml"]["layering"] = {"scope": ["org"], "classification": {"level": "internal"}}
    result = lint_package(make_pkg(base))
    assert not result.ok
    messages = [f.message for f in result.findings if f.rule == "OSCA061"]
    assert any("scope=" in m for m in messages) and any("classification=" in m for m in messages)
    assert not any("规则执行异常" in f.message for f in result.findings)


def test_osca061_non_mapping_is_error(make_pkg, base):
    base["osca.yaml"]["layering"] = "org"  # 非 mapping
    result = lint_package(make_pkg(base))
    assert not result.ok
    assert any("layering 必须是 mapping" in f.message for f in result.findings if f.rule == "OSCA061")
