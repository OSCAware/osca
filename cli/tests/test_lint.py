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
