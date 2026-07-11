"""lint 规则第一批（v0.1）——账本纪律与包规范的机器化。

每条规则一个函数，签名统一：(pkg: OscaPackage) -> list[Finding]。
规则依据以注释标注：SPEC §x / 账本纪律第 n 条 / 开仓铁律。
规则清单文档：docs/OSCA-LINT-RULES.md（与本文件一一对应）。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable

from osca_cli.findings import Finding, Severity
from osca_cli.package import (
    REQUIRED_FILES,
    TYPED_DIRS,
    OscaPackage,
    YamlFile,
    referenced_ids,
)
from osca_cli.triggers import (
    AWARE_BUDGET_KEYS,
    POLICY_BUDGET_KEYS,
    parse_quantity,
    validate_gate,
    validate_trigger,
)

Rule = Callable[[OscaPackage], list[Finding]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def _err(rule_id: str, path: str, msg: str) -> Finding:
    return Finding(rule_id, Severity.ERROR, path, msg)


def _warn(rule_id: str, path: str, msg: str) -> Finding:
    return Finding(rule_id, Severity.WARNING, path, msg)


# ───────────────────────── 包结构 ─────────────────────────


@rule
def osca001_required_files(pkg: OscaPackage) -> list[Finding]:
    """OSCA001 必备文件存在（SPEC §0 + osca.yaml 身份证）。"""
    return [_err("OSCA001", ".", f"缺少必备文件 {name}") for name in REQUIRED_FILES if not pkg.exists(name)]


@rule
def osca002_layout(pkg: OscaPackage) -> list[Finding]:
    """OSCA002 标准目录布局（SPEC §0；空目录 git 不保留，缺失记警告）。"""
    findings = []
    for dirname in TYPED_DIRS:
        if not (pkg.root / dirname).is_dir():
            findings.append(_warn("OSCA002", ".", f"缺少标准目录 {dirname}/"))
    return findings


@rule
def osca003_yaml_parse(pkg: OscaPackage) -> list[Finding]:
    """OSCA003 所有 YAML 必须可解析。"""
    return [
        _err("OSCA003", f.relpath, f"YAML 解析失败：{f.parse_error.splitlines()[0]}")
        for f in pkg.yaml_files.values()
        if f.parse_error
    ]


@rule
def osca004_manifest(pkg: OscaPackage) -> list[Finding]:
    """OSCA004 osca.yaml 身份证完整；entry 指向的文件存在。"""
    f = pkg.yaml_files.get("osca.yaml")
    if f is None or f.parse_error:
        return []  # 缺失/解析失败由 OSCA001/003 报
    findings = []
    m = f.mapping
    if m.get("format") != "osca":
        findings.append(_err("OSCA004", "osca.yaml", "format 必须为 osca"))
    for key in ("format_version", "package_id", "name"):
        if not m.get(key):
            findings.append(_err("OSCA004", "osca.yaml", f"缺少必填字段 {key}"))
    entry = m.get("entry", "AGENT.md")
    if isinstance(entry, str) and not pkg.exists(entry):
        findings.append(_err("OSCA004", "osca.yaml", f"entry 指向的文件不存在：{entry}"))
    requires = m.get("requires")
    if requires is not None and not isinstance(requires, dict):
        findings.append(
            _err("OSCA004", "osca.yaml", f"requires 必须是 mapping（runtime/bindings，现为 {type(requires).__name__}）")
        )
    elif isinstance(requires, dict):
        bindings_decl = requires.get("bindings")
        if bindings_decl is not None and not (
            isinstance(bindings_decl, list) and all(isinstance(x, str) for x in bindings_decl)
        ):
            findings.append(_err("OSCA004", "osca.yaml", "requires.bindings 必须是字符串列表（binding 名）"))
    pid = m.get("package_id")
    if isinstance(pid, str) and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", pid):
        findings.append(_warn("OSCA004", "osca.yaml", "package_id 建议仅用小写字母、数字、连字符"))
    return findings


# ───────────────────────── 命名与 ID（SPEC §2） ─────────────────────────


@rule
def osca010_filename(pkg: OscaPackage) -> list[Finding]:
    """OSCA010 类型目录下文件名 = <ID>[-<名>].yaml，前缀与目录匹配。"""
    findings = []
    for dirname, (prefix, _) in TYPED_DIRS.items():
        for f in pkg.typed_files(dirname):
            stem = f.relpath.rsplit("/", 1)[-1].removesuffix(".yaml")
            if not re.fullmatch(rf"{prefix}-\d{{3,4}}(-[^/]+)?", stem):
                findings.append(
                    _err(
                        "OSCA010",
                        f.relpath,
                        f"文件名须为 {prefix}-<编号>[-<中文名>].yaml（前缀与目录 {dirname}/ 匹配）",
                    )
                )
    return findings


@rule
def osca011_id_matches_filename(pkg: OscaPackage) -> list[Finding]:
    """OSCA011 文件内 ID 字段存在，且与文件名中的 ID 一致。"""
    findings = []
    for dirname, (prefix, field_name) in TYPED_DIRS.items():
        for f in pkg.typed_files(dirname):
            if f.parse_error:
                continue
            value = f.mapping.get(field_name)
            if not value:
                findings.append(_err("OSCA011", f.relpath, f"缺少 ID 字段 {field_name}"))
                continue
            stem = f.relpath.rsplit("/", 1)[-1].removesuffix(".yaml")
            m = re.match(rf"({prefix}-\d{{3,4}})", stem)
            if m and value != m.group(1):
                findings.append(_err("OSCA011", f.relpath, f"{field_name}={value} 与文件名 ID {m.group(1)} 不一致"))
    return findings


@rule
def osca012_id_unique(pkg: OscaPackage) -> list[Finding]:
    """OSCA012 ID 包内唯一，永不复用（SPEC §2）。"""
    seen: dict[str, list[str]] = defaultdict(list)
    for rel, f in pkg.yaml_files.items():
        _, value = pkg.id_field_of(f)
        if isinstance(value, str):
            seen[value].append(rel)
    return [
        _err("OSCA012", ", ".join(paths), f"ID {id_} 被声明了 {len(paths)} 次")
        for id_, paths in seen.items()
        if len(paths) > 1
    ]


# ───────────────────────── 引用（SPEC §2：只许用 ID 引用） ─────────────────────────


@rule
def osca020_refs_resolve(pkg: OscaPackage) -> list[Finding]:
    """OSCA020 文件正文出现的每个 ID 形状 token 必须在包内可解析。"""
    known = set(pkg.declared_ids)
    findings = []
    for rel, f in sorted(pkg.yaml_files.items()):
        for id_ in sorted(referenced_ids(f) - known):
            findings.append(_err("OSCA020", rel, f"引用的 {id_} 在包内不存在"))
    return findings


@rule
def osca021_binding_ref(pkg: OscaPackage) -> list[Finding]:
    """OSCA021 connector 必有 binding_ref，且在 bindings.example.yaml 有同名键（SPEC §4）。"""
    findings = []
    bindings = pkg.yaml_files.get("bindings.example.yaml")
    binding_keys = set(bindings.mapping) if bindings else set()
    connectors = pkg.typed_files("connectors")
    if connectors and bindings is None:
        findings.append(_err("OSCA021", ".", "有 connector 但缺少 bindings.example.yaml 模板"))
    for f in connectors:
        if f.parse_error:
            continue
        ref = f.mapping.get("binding_ref")
        if not ref:
            findings.append(_err("OSCA021", f.relpath, "缺少 binding_ref（manifest 必填，SPEC §4）"))
        elif not isinstance(ref, str):
            findings.append(_err("OSCA021", f.relpath, f"binding_ref 必须是字符串（现为 {type(ref).__name__}）"))
        elif binding_keys and ref not in binding_keys:
            findings.append(_err("OSCA021", f.relpath, f"binding_ref={ref} 在 bindings.example.yaml 中无对应键"))
    return findings


@rule
def osca022_requires_bindings(pkg: OscaPackage) -> list[Finding]:
    """OSCA022 osca.yaml requires.bindings 与各 connector 的 binding_ref 集合一致。"""
    manifest = pkg.yaml_files.get("osca.yaml")
    if manifest is None:
        return []
    requires = manifest.mapping.get("requires") or {}
    raw_declared = requires.get("bindings") if isinstance(requires, dict) else None
    declared = {x for x in raw_declared if isinstance(x, str)} if isinstance(raw_declared, list) else set()
    actual = {
        f.mapping.get("binding_ref")
        for f in pkg.typed_files("connectors")
        if isinstance(f.mapping.get("binding_ref"), str)
    }
    findings = []
    for missing in sorted(actual - declared):
        findings.append(_warn("OSCA022", "osca.yaml", f"connector 用到 binding {missing}，但 requires.bindings 未声明"))
    for extra in sorted(declared - actual):
        findings.append(_warn("OSCA022", "osca.yaml", f"requires.bindings 声明了 {extra}，但没有 connector 使用它"))
    return findings


@rule
def osca023_policy_steps(pkg: OscaPackage) -> list[Finding]:
    """OSCA023 policy 权限表的 step 名必须存在于 structure pipeline。"""
    policy = pkg.yaml_files.get("policy.yaml")
    structure = pkg.yaml_files.get("structure.yaml")
    if policy is None or structure is None:
        return []
    pipeline = structure.mapping.get("pipeline")
    if not isinstance(pipeline, list):
        pipeline = []  # 形状缺陷由 OSCA040 报，这里不迭代非序列
    steps = {s.get("step") for s in pipeline if isinstance(s, dict) and isinstance(s.get("step"), str)}
    findings = []
    permissions = policy.mapping.get("permissions")
    for perm in permissions if isinstance(permissions, list) else []:
        if not isinstance(perm, dict):
            continue  # 元素形状由 OSCA040 报
        step = perm.get("step")
        if not isinstance(step, str) or step not in steps:
            findings.append(_warn("OSCA023", "policy.yaml", f"权限表 step「{step}」在 structure pipeline 中不存在"))
    return findings


@rule
def osca024_impl_paths(pkg: OscaPackage) -> list[Finding]:
    """OSCA024 connector 接口声明的 impl 路径必须真实存在（SPEC §4 层3）。"""
    findings = []
    for f in pkg.typed_files("connectors"):
        itfs = f.mapping.get("interfaces")
        for itf in itfs if isinstance(itfs, list) else []:
            impl = itf.get("impl") if isinstance(itf, dict) else None
            if isinstance(impl, str) and not pkg.exists(impl):
                findings.append(_warn("OSCA024", f.relpath, f"impl 指向的文件不存在：{impl}"))
    return findings


# ───────────────────────── 账本纪律（SPEC §9 + 架构 §2） ─────────────────────────


def _judgments(pkg: OscaPackage) -> list[YamlFile]:
    return [f for f in pkg.typed_files("judgments") if not f.parse_error]


CASE_ID = re.compile(r"C-\d{3,4}")


@rule
def osca030_evidence(pkg: OscaPackage) -> list[Finding]:
    """OSCA030 每条判断 ≥1 条出生证据，且必须是包内存在的 case（C-xxxx，纪律第 2 条）。

    只认 C- 前缀：证据物种只有 case——引用别的 ID（对象/判断）碰巧存在也不算证据。
    """
    findings = []
    for f in _judgments(pkg):
        evidence = f.mapping.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            findings.append(_err("OSCA030", f.relpath, "无出生证据的判断不准入账（evidence 至少 1 条）"))
            continue
        for ev in evidence:
            if not (isinstance(ev, str) and CASE_ID.fullmatch(ev) and ev in pkg.declared_ids):
                findings.append(_err("OSCA030", f.relpath, f"evidence 必须是包内存在的 case（C-xxxx）：{ev}"))
    return findings


@rule
def osca031_supersedes(pkg: OscaPackage) -> list[Finding]:
    """OSCA031 supersedes 链双向一致且无环：推翻不删除（纪律第 1 条）。

    自指与环是「互相取代」的账本悖论——没有一条现役判断，回放无锚点，一律报错。
    """
    findings = []
    judgments = _judgments(pkg)
    by_id = {f.mapping.get("judgment_id"): f for f in judgments if isinstance(f.mapping.get("judgment_id"), str)}
    superseded_by: dict[str, list[str]] = defaultdict(list)
    chain: dict[str, str] = {}  # jid → 它取代的 jid（环检测输入）
    for f in judgments:
        target = f.mapping.get("supersedes")
        if target is None:
            continue
        jid = f.mapping.get("judgment_id")
        jid = jid if isinstance(jid, str) else f.relpath  # ID 形状缺陷由 OSCA011 报；这里保持可定位
        if not isinstance(target, str):
            findings.append(
                _err("OSCA031", f.relpath, f"supersedes 必须是判断 ID 字符串（现为 {type(target).__name__}）")
            )
            continue
        if target == jid:
            findings.append(_err("OSCA031", f.relpath, "supersedes 指向自身——取代链必须指向别的判断"))
            continue
        old = by_id.get(target)
        if old is None:
            findings.append(_err("OSCA031", f.relpath, f"supersedes 指向的 {target} 不存在"))
        else:
            chain[jid] = target
            superseded_by[target].append(jid)
            if old.mapping.get("status") != "superseded":
                findings.append(
                    _err("OSCA031", old.relpath, f"被 {f.mapping.get('judgment_id')} 取代，status 必须改为 superseded")
                )
    # 取代分叉：同一旧判断被多条判断取代——账本只认一条现役后继，分叉即口径分裂
    for target, successors in sorted(superseded_by.items()):
        if len(successors) > 1:
            findings.append(
                _err(
                    "OSCA031",
                    by_id[target].relpath,
                    f"{target} 被多条判断取代（{'、'.join(sorted(successors))}）——取代链分叉，须并成一条",
                )
            )
    # 环检测：沿取代链走，回到走过的节点即环（每个环只报一次，以环内最小 ID 为代表）
    for start in sorted(chain):
        path: list[str] = []
        cur = start
        while cur in chain and cur not in path:
            path.append(cur)
            cur = chain[cur]
        if cur in path:
            cycle = path[path.index(cur) :]
            if start == min(cycle):
                loop = " → ".join([*cycle, cur])
                findings.append(_err("OSCA031", by_id[start].relpath, f"supersedes 成环：{loop}——取代链必须无环"))
    for f in judgments:
        jid = f.mapping.get("judgment_id")
        if f.mapping.get("status") == "superseded" and (not isinstance(jid, str) or jid not in superseded_by):
            findings.append(_err("OSCA031", f.relpath, "status=superseded 但没有任何判断通过 supersedes 指向它"))
    return findings


@rule
def osca032_trust(pkg: OscaPackage) -> list[Finding]:
    """OSCA032 trust 由计数自动驱动，人不手改（纪律第 4 条）：
    active 判断中 confirmed≥5 且 overruled==0 ⇔ trust=high。superseded 冻结不查。"""
    findings = []
    for f in _judgments(pkg):
        if f.mapping.get("status") != "active":
            continue
        meta = f.mapping.get("meta")
        if not isinstance(meta, dict):
            continue  # 形状缺陷由 OSCA040 报——本规则自身对任意形状保持总函数
        confirmed, overruled = meta.get("confirmed"), meta.get("overruled")
        trust = meta.get("trust")
        if type(confirmed) is not int or type(overruled) is not int:
            continue  # 字段缺失/形状缺陷（含 bool——Python 里 bool 是 int 子类）由 OSCA040 报
        earned_high = confirmed >= 5 and overruled == 0
        if earned_high and trust != "high":
            findings.append(
                _err("OSCA032", f.relpath, f"confirmed={confirmed} 且 overruled=0，trust 应为 high（现为 {trust}）")
            )
        if not earned_high and trust == "high":
            findings.append(
                _err("OSCA032", f.relpath, f"trust=high 但计数不够格（confirmed={confirmed}, overruled={overruled}）")
            )
    return findings


@rule
def osca033_status(pkg: OscaPackage) -> list[Finding]:
    """OSCA033 status 合法值：active | superseded | review。"""
    valid = {"active", "superseded", "review"}
    return [
        _err("OSCA033", f.relpath, f"status={f.mapping.get('status')} 不在合法值 {sorted(valid)} 中")
        for f in _judgments(pkg)
        if not (isinstance(f.mapping.get("status"), str) and f.mapping.get("status") in valid)
    ]


@rule
def osca034_replay(pkg: OscaPackage) -> list[Finding]:
    """OSCA034 每条判断自带回放断言＝单元测试（纪律第 4 条·架构版）。"""
    return [
        _err("OSCA034", f.relpath, "缺少 replay 回放断言（每条判断必须自带单元测试）")
        for f in _judgments(pkg)
        if not f.mapping.get("replay")
    ]


@rule
def osca035_expiry(pkg: OscaPackage) -> list[Finding]:
    """OSCA035 判断应声明失效条件（防腐烂）。"""
    return [
        _warn("OSCA035", f.relpath, "建议补 expiry 失效条件（防止判断悄悄腐烂）")
        for f in _judgments(pkg)
        if not f.mapping.get("expiry")
    ]


@rule
def osca036_case_effective_set(pkg: OscaPackage) -> list[Finding]:
    """OSCA036 case 必存「当时生效判断集」，无此字段回放不可信（SPEC §8）。"""
    findings = []
    for f in pkg.typed_files("cases"):
        if f.parse_error:
            continue
        input_ = f.mapping.get("input")
        if not isinstance(input_, dict) or "当时生效判断集" not in input_:
            findings.append(_err("OSCA036", f.relpath, "input 缺少必存字段「当时生效判断集」（回放不可信）"))
    return findings


# ───────────────────────── 各类文件必填字段 ─────────────────────────

OBJECT_KINDS = {"entity", "artifact", "metric", "composite", "objective"}
CONNECTOR_KINDS = {"mcp", "openapi", "sql_readonly", "code"}
TRIGGER_KINDS = {"schedule", "event", "watch"}
# 受支持的脱敏类别枚举——与参考实现 Host 的 REDACTORS 同步（合法形状但未知类别 = 脱敏静默失效）
REDACT_CATEGORIES = {"身份证号", "手机号"}


@rule
def osca040_required_fields(pkg: OscaPackage) -> list[Finding]:
    """OSCA040 各类文件必填字段（SPEC §3–§6；judgment/case 依样例包定稿稿）。"""
    findings = []

    def need(f: YamlFile, *keys: str, where: dict | None = None, prefix: str = ""):
        source = f.mapping if where is None else where
        for key in keys:
            if source.get(key) in (None, "", [], {}):
                findings.append(_err("OSCA040", f.relpath, f"缺少必填字段 {prefix}{key}"))

    def bad_shape(f: YamlFile, field_name: str, value, expected: str) -> None:
        findings.append(
            _err(
                "OSCA040",
                f.relpath,
                f"{field_name} 必须是 {expected}（现为 {type(value).__name__}）——运行时按此形状取值",
            )
        )

    for f in pkg.typed_files("objects"):
        if f.parse_error:
            continue
        need(f, "name", "kind", "version", "definition")
        kind = f.mapping.get("kind")
        if kind is not None and not (isinstance(kind, str) and kind in OBJECT_KINDS):
            findings.append(_err("OSCA040", f.relpath, f"kind={kind} 不在 {sorted(OBJECT_KINDS)} 中"))
        if kind == "objective" and f.mapping.get("optimize") not in ("maximize", "minimize"):
            findings.append(
                _err("OSCA040", f.relpath, "objective 必填 optimize: maximize | minimize（寻优方向，SPEC v0.4 §8）")
            )
        examples = f.mapping.get("examples")
        if examples is not None and not isinstance(examples, dict):
            bad_shape(f, "examples", examples, "mapping（positive/negative 列表）")
            examples = None
        negatives = (examples or {}).get("negative")
        if negatives is not None and not isinstance(negatives, list):
            bad_shape(f, "examples.negative", negatives, "list")
            negatives = None
        for i, neg in enumerate(negatives or []):
            if not isinstance(neg, dict):
                findings.append(_err("OSCA040", f.relpath, f"负样例第 {i + 1} 条必须是 mapping（摘录 + why）"))
            elif not neg.get("why"):
                findings.append(
                    _err("OSCA040", f.relpath, f"负样例第 {i + 1} 条缺少 why（每条负样例必须带 why，SPEC §3）")
                )

    for f in pkg.typed_files("connectors"):
        if f.parse_error:
            continue
        need(f, "name", "kind", "interfaces")
        kind = f.mapping.get("kind")
        if kind is not None and not (isinstance(kind, str) and kind in CONNECTOR_KINDS):
            findings.append(_err("OSCA040", f.relpath, f"kind={kind} 不在 {sorted(CONNECTOR_KINDS)} 中"))
        itfs = f.mapping.get("interfaces")
        if itfs is not None and not isinstance(itfs, list):
            bad_shape(f, "interfaces", itfs, "list（接口声明序列）")
            itfs = None
        for i, itf in enumerate(itfs or []):
            if not isinstance(itf, dict) or not (itf.get("name") and itf.get("returns")):
                findings.append(_err("OSCA040", f.relpath, f"接口第 {i + 1} 条缺少 name 或 returns"))
        perms = f.mapping.get("permissions")
        if perms is not None and not isinstance(perms, dict):
            bad_shape(f, "permissions", perms, "mapping（write 权限声明）")
        elif (perms or {}).get("write") not in ("forbidden", "allowed_with_approval"):
            findings.append(_err("OSCA040", f.relpath, "permissions.write 必须是 forbidden 或 allowed_with_approval"))

    for f in pkg.typed_files("aware"):
        if f.parse_error:
            continue
        need(f, "name", "then", "budget")
        if not isinstance(f.mapping.get("enabled"), bool):
            findings.append(_err("OSCA040", f.relpath, "enabled 必须是布尔值（三级停的触发器停靠它）"))
        budget = f.mapping.get("budget")
        if budget is not None and not isinstance(budget, dict):
            bad_shape(f, "budget", budget, "mapping（max_steps/max_minutes/max_tokens）")
        elif isinstance(budget, dict):
            for key, value in sorted(budget.items()):
                if key not in AWARE_BUDGET_KEYS:
                    findings.append(
                        _err(
                            "OSCA040",
                            f.relpath,
                            f"budget 含运行时不执行的键「{key}」（Aware 预算只认 {list(AWARE_BUDGET_KEYS)}）"
                            "——声明了没人执行的硬顶是 fail-open",
                        )
                    )
                elif parse_quantity(value) is None:
                    findings.append(
                        _err(
                            "OSCA040",
                            f.relpath,
                            f"budget.{key}={value!r} 不合数量记法（<正整数>[k]）——错误预算不得放行",
                        )
                    )
        gate = f.mapping.get("gate")
        if gate is not None and not isinstance(gate, dict):
            bad_shape(f, "gate", gate, "mapping（combine/precondition/debounce/on_fail）")
        raw_triggers = f.mapping.get("triggers")
        if raw_triggers is not None and not isinstance(raw_triggers, list):
            bad_shape(f, "triggers", raw_triggers, "list（触发原语序列）")
            raw_triggers = None
        triggers = raw_triggers or ([f.mapping["trigger"]] if f.mapping.get("trigger") else [])
        if not triggers:
            findings.append(_err("OSCA040", f.relpath, "至少需要 1 个触发原语（triggers 或 trigger）"))
        for i, t in enumerate(triggers):
            if not isinstance(t, dict):
                # 非 mapping 元素会被运行时静默丢弃 → 「显示启用、实际永不触发」的包，必须在此挡下
                findings.append(_err("OSCA040", f.relpath, f"触发原语第 {i + 1} 条必须是 mapping（id/kind/…）"))
                continue
            kind = t.get("kind")
            if not (isinstance(kind, str) and kind in TRIGGER_KINDS):
                findings.append(
                    _err("OSCA040", f.relpath, f"触发原语第 {i + 1} 条 kind 不在 {sorted(TRIGGER_KINDS)} 中")
                )

    for f in _judgments(pkg):
        need(f, "status", "body", "meta")
        sig = f.mapping.get("signature")
        if not isinstance(sig, dict):
            findings.append(_err("OSCA040", f.relpath, "缺少必填字段 signature"))
        else:
            need(f, "object", "aware", "guard", where=sig, prefix="signature.")
        meta = f.mapping.get("meta")
        if isinstance(meta, dict):
            need(f, "author", "trust", where=meta, prefix="meta.")
            for key in ("confirmed", "overruled"):
                if type(meta.get(key)) is not int:  # bool 是 int 子类——true/false 混进计数会污染 trust 与 kill switch
                    findings.append(_err("OSCA040", f.relpath, f"meta.{key} 必须是整数计数（布尔值不算）"))
        elif meta is not None:
            bad_shape(f, "meta", meta, "mapping（机器管账的计数与 trust）")
        replay = f.mapping.get("replay")
        if replay is not None and not isinstance(replay, list):
            bad_shape(f, "replay", replay, "list（回放断言序列）")
            replay = None
        for i, assertion in enumerate(replay or []):
            if not isinstance(assertion, dict):
                findings.append(
                    _err("OSCA040", f.relpath, f"replay 第 {i + 1} 条必须是 mapping（given/with_this_judgment）")
                )

    for f in pkg.typed_files("cases"):
        if f.parse_error:
            continue
        need(f, "captured_at", "capture_source", "input")

    policy = pkg.yaml_files.get("policy.yaml")
    if policy and not policy.parse_error:
        m = policy.mapping
        if not m.get("policy_version"):
            findings.append(_err("OSCA040", "policy.yaml", "缺少必填字段 policy_version"))
        # policy 是笼子：形状错误在装载前就要挡下——运行时构造器按这些形状取值
        for key in ("permissions", "approvals", "kill_switch"):
            v = m.get(key)
            if v is not None and not isinstance(v, list):
                findings.append(_err("OSCA040", "policy.yaml", f"{key} 必须是 list（现为 {type(v).__name__}）"))
        for key in ("budgets", "egress", "data"):
            v = m.get(key)
            if v is not None and not isinstance(v, dict):
                findings.append(_err("OSCA040", "policy.yaml", f"{key} 必须是 mapping（现为 {type(v).__name__}）"))
        budgets = m.get("budgets")
        if isinstance(budgets, dict):
            per = budgets.get("per_episode")
            if per is not None and not isinstance(per, dict):
                findings.append(_err("OSCA040", "policy.yaml", "budgets.per_episode 必须是 mapping"))
            elif isinstance(per, dict):
                for key, value in sorted(per.items()):
                    if key not in POLICY_BUDGET_KEYS:
                        findings.append(
                            _err(
                                "OSCA040",
                                "policy.yaml",
                                f"per_episode 含运行时不执行的键「{key}」（Policy 预算只认 {list(POLICY_BUDGET_KEYS)}）"
                                "——声明了没人执行的硬顶是 fail-open",
                            )
                        )
                    elif parse_quantity(value) is None:
                        findings.append(
                            _err(
                                "OSCA040",
                                "policy.yaml",
                                f"per_episode.{key}={value!r} 不合数量记法（<正整数>[k]）——错误预算不得放行",
                            )
                        )
        # 运行时消费的叶子字段：形状错误会静默改变笼子语义（如关闭脱敏），必须逐项验型
        permissions = m.get("permissions")
        for i, p in enumerate(permissions if isinstance(permissions, list) else []):
            if not isinstance(p, dict) or not isinstance(p.get("step"), str):
                findings.append(
                    _err("OSCA040", "policy.yaml", f"permissions 第 {i + 1} 项必须是含 step 字符串的 mapping")
                )
                continue
            allow = p.get("allow")
            if allow is None:
                findings.append(
                    _err(
                        "OSCA040",
                        "policy.yaml",
                        f"permissions「{p.get('step')}」缺少 allow——白名单必须显式声明（空列表也要写）",
                    )
                )
            elif not (isinstance(allow, list) and all(isinstance(a, str) for a in allow)):
                findings.append(
                    _err(
                        "OSCA040",
                        "policy.yaml",
                        f"permissions「{p.get('step')}」的 allow 必须是字符串列表（工具白名单）",
                    )
                )
        approvals = m.get("approvals")
        for i, a in enumerate(approvals if isinstance(approvals, list) else []):
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("action"), str)
                or not isinstance(a.get("approver"), str)
            ):
                findings.append(
                    _err("OSCA040", "policy.yaml", f"approvals 第 {i + 1} 项必须是含 action/approver 字符串的 mapping")
                )
        kill_switch = m.get("kill_switch")
        for i, k in enumerate(kill_switch if isinstance(kill_switch, list) else []):
            if not isinstance(k, dict) or not isinstance(k.get("when"), str) or not k.get("when").strip():
                findings.append(
                    _err("OSCA040", "policy.yaml", f"kill_switch 第 {i + 1} 项必须是含 when 非空字符串的 mapping")
                )
        data = m.get("data")
        if isinstance(data, dict):
            redact = data.get("redact")
            if redact is not None and not (isinstance(redact, list) and all(isinstance(c, str) for c in redact)):
                findings.append(
                    _err("OSCA040", "policy.yaml", "data.redact 必须是字符串列表（脱敏类别）——形状错误会静默关闭脱敏")
                )
            elif isinstance(redact, list):
                for c in redact:
                    if c not in REDACT_CATEGORIES:
                        findings.append(
                            _err(
                                "OSCA040",
                                "policy.yaml",
                                f"data.redact 含未知类别「{c}」（支持 {sorted(REDACT_CATEGORIES)}）——未知类别不生效",
                            )
                        )
        egress = m.get("egress")
        if isinstance(egress, dict):
            domains = egress.get("allow_domains")
            if domains is not None and not (isinstance(domains, list) and all(isinstance(d, str) for d in domains)):
                findings.append(_err("OSCA040", "policy.yaml", "egress.allow_domains 必须是字符串列表（出网白名单）"))

    structure = pkg.yaml_files.get("structure.yaml")
    if structure and not structure.parse_error:
        pipeline = structure.mapping.get("pipeline")
        if pipeline is not None and not isinstance(pipeline, list):
            findings.append(
                _err(
                    "OSCA040", "structure.yaml", f"pipeline 必须是 list（步骤声明序列，现为 {type(pipeline).__name__}）"
                )
            )
        for i, step in enumerate(pipeline if isinstance(pipeline, list) else []):
            if not isinstance(step, dict):
                findings.append(
                    _err(
                        "OSCA040",
                        "structure.yaml",
                        f"pipeline 第 {i + 1} 项必须是步骤声明 mapping（现为 {type(step).__name__}）",
                    )
                )
            elif not isinstance(step.get("step"), str) or not step.get("step").strip():
                findings.append(
                    _err(
                        "OSCA040",
                        "structure.yaml",
                        f"pipeline 第 {i + 1} 项必须有非空字符串 step（policy 权限表以它为键）",
                    )
                )

    return findings


@rule
def osca041_trigger_gate_syntax(pkg: OscaPackage) -> list[Finding]:
    """OSCA041 触发原语与闸门的受限语法（SPEC v0.4 草案 §5）。
    解析器与运行框架 Host 共用（osca_cli.triggers）——lint 过 = Host 编译期能布防。"""
    findings = []
    for f in pkg.typed_files("aware"):
        if f.parse_error:
            continue
        triggers = [t for t in (f.mapping.get("triggers") or []) if isinstance(t, dict)]
        for t in triggers:
            findings.extend(_err("OSCA041", f.relpath, msg) for msg in validate_trigger(t))
        gate = f.mapping.get("gate")
        if isinstance(gate, dict):
            findings.extend(_err("OSCA041", f.relpath, msg) for msg in validate_gate(gate, len(triggers)))
    return findings


# ───────────────────────── 安全（铁律，SPEC v0.3 §13） ─────────────────────────

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:ssh|ftp|redis|amqp|mongodb(?:\+srv)?|mysql|postgres(?:ql)?)://"), "连接串"),
    (re.compile(r"\bjdbc:"), "JDBC 连接串"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS AccessKey"),
    (re.compile(r"\bLTAI[A-Za-z0-9]{12,24}\b"), "阿里云 AccessKey"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
]

# 有限允许：指向公开文档的 https 链接（SPEC v0.3 §13）。白名单之外一律报错。
ALLOWED_LINK_DOMAINS = {
    "creativecommons.org",
    "apache.org",
    "www.apache.org",
    "opensource.org",
    "oscaware.com",
    "www.oscaware.com",
    "github.com",
}

HTTP_URL = re.compile(r"\b(https?)://([A-Za-z0-9.-]+)")
SCAN_SKIP_DIRS = {"indexes", ".git"}
SCAN_SKIP_NAMES = {".DS_Store"}


def _domain_allowed(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in ALLOWED_LINK_DOMAINS)


@rule
def osca050_secrets(pkg: OscaPackage) -> list[Finding]:
    """OSCA050 零密钥、零连接串；https 文档链接仅白名单域放行（铁律；扫描包内全部文本文件）。"""
    findings = []
    for path in sorted(pkg.root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(pkg.root).as_posix()
        if rel.split("/", 1)[0] in SCAN_SKIP_DIRS or path.name in SCAN_SKIP_NAMES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(_err("OSCA050", rel, f"第 {lineno} 行疑似{label}（铁律：连接串与密钥绝对禁止）"))
            for scheme, host in HTTP_URL.findall(line):
                if scheme == "http":
                    findings.append(_err("OSCA050", rel, f"第 {lineno} 行含明文 http 链接（{host}）——一律禁止"))
                elif not _domain_allowed(host):
                    findings.append(
                        _err("OSCA050", rel, f"第 {lineno} 行链接域名 {host} 不在公开文档白名单（SPEC §13）")
                    )
    return findings


def run_all(pkg: OscaPackage) -> list[Finding]:
    """跑全部规则。lint 必须是总函数：面对不可信 YAML 只产出 findings、绝不抛异常——
    各规则自带类型防御，这里再兜最后一层（规则异常转 ERROR，CLI/Host 装载永不断掉）。"""
    findings: list[Finding] = []
    for r in RULES:
        try:
            findings.extend(r(pkg))
        except Exception as e:
            rule_id = r.__name__.split("_", 1)[0].upper()
            findings.append(
                Finding(
                    rule_id,
                    Severity.ERROR,
                    ".",
                    f"规则执行异常（{type(e).__name__}: {e}）——包形状超出规则假设，按错误拒绝",
                )
            )
    return sorted(findings, key=lambda x: (x.path, x.rule, x.message))
