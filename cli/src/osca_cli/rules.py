"""lint 规则第一批（v0.1）——账本纪律与包规范的机器化。

每条规则一个函数，签名统一：(pkg: OscaPackage) -> list[Finding]。
规则依据以注释标注：SPEC §x / 账本纪律第 n 条 / 开仓铁律。
规则清单文档：docs/OSCA-LINT-RULES.md（与本文件一一对应）。
"""

from __future__ import annotations

import math
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
    resolve_in_root,
)
from osca_cli.triggers import (
    AWARE_BUDGET_KEYS,
    PERFORMERS,
    POLICY_BUDGET_KEYS,
    parse_performer,
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
    """OSCA024 connector 接口声明的 impl 路径必须真实存在，且不得越出包根（SPEC §4 层3）。

    包内受限路径判据与 Host 执行器共用（package.resolve_in_root）——绝对路径、`../`、符号链接
    逃逸报 error（GPT Review：运行时必拒的越界声明不许 lint 全绿、pack 出必死交付件）。"""
    findings = []
    for f in pkg.typed_files("connectors"):
        itfs = f.mapping.get("interfaces")
        for itf in itfs if isinstance(itfs, list) else []:
            impl = itf.get("impl") if isinstance(itf, dict) else None
            if not isinstance(impl, str):
                continue
            resolved = resolve_in_root(pkg.root, impl)
            if resolved is None:
                findings.append(
                    _err("OSCA024", f.relpath, f"impl 路径越界：{impl}——包内声明只可指包内文件（运行时同判据必拒）")
                )
            elif not resolved.is_file():
                findings.append(_warn("OSCA024", f.relpath, f"impl 指向的文件不存在：{impl}"))
    return findings


@rule
def osca025_write_approval_binding(pkg: OscaPackage) -> list[Finding]:
    """OSCA025 写连接器（allowed_with_approval）每个写接口须在 policy.approvals 声明 approver（SPEC §6/B.4）。

    运行时写审批门 require_write_approval **按写接口 ref「CON-xxx.接口名」查 approver**（policy），不在
    approvals 清单即默认拒绝——写路径会静默死，而其余规则全绿。lint 无此对应则「一等写样例」极易做成
    lint 全绿、写却永远被拒的死包。本规则把「approvals[].action 逐字 == 写接口 ref」机器化（is_write 是
    连接器级：allowed_with_approval 连接器的每个接口都是写接口，逐个都要有名分）。
    """
    findings = []
    policy = pkg.yaml_files.get("policy.yaml")
    approvals = policy.mapping.get("approvals") if policy and not policy.parse_error else None
    actions = {a.get("action") for a in approvals if isinstance(a, dict)} if isinstance(approvals, list) else set()
    for f in pkg.typed_files("connectors"):
        if f.parse_error:
            continue
        perms = f.mapping.get("permissions")
        if not (isinstance(perms, dict) and perms.get("write") == "allowed_with_approval"):
            continue  # 只读连接器（forbidden）不进写门；形状非法由 OSCA040 报
        cid = f.mapping.get("connector_id")
        itfs = f.mapping.get("interfaces")
        for itf in itfs if isinstance(itfs, list) else []:
            name = itf.get("name") if isinstance(itf, dict) else None
            if not (isinstance(cid, str) and isinstance(name, str)):
                continue  # ID/接口名 形状缺陷由 OSCA011/040 报，这里不重复
            ref = f"{cid}.{name}"
            if ref not in actions:
                findings.append(
                    _err(
                        "OSCA025",
                        f.relpath,
                        f"写接口「{ref}」（write: allowed_with_approval）未在 policy.approvals 声明 approver"
                        "——运行时写审批按接口 ref 查 approver，缺失即默认拒绝（写路径静默死）",
                    )
                )
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
        # 读连接器（write: forbidden）接口不得声明写 method（GPT 外审：否则 method:POST 绕审批门真写）；运行时也拦
        if isinstance(perms, dict) and perms.get("write") == "forbidden":
            for i, itf in enumerate(itfs or []):
                method = itf.get("method") if isinstance(itf, dict) else None
                if isinstance(method, str) and method.upper() not in ("GET", "HEAD"):
                    findings.append(
                        _err(
                            "OSCA040",
                            f.relpath,
                            f"接口第 {i + 1} 条：write: forbidden 连接器不得用写 method {method}（绕审批门）",
                        )
                    )

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
                # bool 是 int 子类、负数会污染 kill switch 比值——都不是合法计数
                if type(meta.get(key)) is not int or meta.get(key) < 0:
                    findings.append(_err("OSCA040", f.relpath, f"meta.{key} 必须是非负整数计数（布尔/负数不算）"))
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

        def _bad_ttl(value: object) -> bool:
            """审批授权 TTL（W6-1）：须为正有限数（秒）。非数/bool/非有限/≤0/巨值溢出 float 皆非法——
            与 host policy._parse_ttl 的合法判定一致（形状错误在装载前挡，policy 是笼子）。"""
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return True
            try:
                f = float(value)
            except (OverflowError, ValueError):
                return True
            return not math.isfinite(f) or f <= 0

        dt = m.get("default_ttl_seconds")
        if dt is not None and _bad_ttl(dt):
            findings.append(_err("OSCA040", "policy.yaml", "default_ttl_seconds 必须是正数（审批授权过期秒数）"))
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
            for key in sorted(budgets):
                if key != "per_episode":
                    findings.append(
                        _err(
                            "OSCA040",
                            "policy.yaml",
                            f"budgets 含未知段「{key}」（只认 per_episode）——拼写错误会静默变成无限额",
                        )
                    )
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
        seen_actions: set[str] = set()
        for i, a in enumerate(approvals if isinstance(approvals, list) else []):
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("action"), str)
                or not isinstance(a.get("approver"), str)
            ):
                findings.append(
                    _err("OSCA040", "policy.yaml", f"approvals 第 {i + 1} 项必须是含 action/approver 字符串的 mapping")
                )
                continue
            if (ttl := a.get("ttl_seconds")) is not None and _bad_ttl(ttl):
                findings.append(
                    _err("OSCA040", "policy.yaml", f"approvals 第 {i + 1} 项 ttl_seconds 必须是正数（授权过期秒数）")
                )
            # action 须唯一（GPT 外审）：重复 action 会致 approver/TTL 覆盖歧义（后一条覆盖前一条）
            if a["action"] in seen_actions:
                findings.append(
                    _err("OSCA040", "policy.yaml", f"approvals 第 {i + 1} 项 action「{a['action']}」重复——须唯一")
                )
            seen_actions.add(a["action"])
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
            elif parse_performer(step.get("performer", "")) is None:
                # performer 受限语法与 Host runner 共用（parse_performer）——运行时不可识别即步骤失败，
                # lint 不拦则「拼错 performer 的包全绿、跑必败」（GPT Review：runner 子串匹配已废，改共用解析）
                findings.append(
                    _err(
                        "OSCA040",
                        "structure.yaml",
                        f"pipeline 第 {i + 1} 项 performer「{step.get('performer')}」不合受限语法"
                        f"（须以 {'/'.join(PERFORMERS)} 开头，可带 `+ 修饰`/括注）",
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

# 协议/连接串正则一律大小写不敏感（P1：`POSTGRES://` / `HTTP://` 大写变体曾漏检）；
# 端点形式按 SPEC §13 补全：裸 IP:端口、IPv6:端口、带 userinfo 的连接串同属禁止。
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:ssh|ftp|redis|amqp|mongodb(?:\+srv)?|mysql|postgres(?:ql)?)://", re.IGNORECASE), "连接串"),
    (re.compile(r"\bjdbc:", re.IGNORECASE), "JDBC 连接串"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS AccessKey"),
    (re.compile(r"\bLTAI[A-Za-z0-9]{12,24}\b"), "阿里云 AccessKey"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b"), "IP:端口 端点"),
    (re.compile(r"\[[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*\]:\d{1,5}"), "IPv6 端点"),
    (re.compile(r"://[^/\s@]{1,128}@"), "带 userinfo 的连接串"),
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

HTTP_URL = re.compile(r"\b(https?)://([A-Za-z0-9.-]+)", re.IGNORECASE)
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
                host = host.lower()  # 域名比对大小写不敏感（HTTP:// / Github.Com 变体不许漏）
                if scheme.lower() == "http":
                    findings.append(_err("OSCA050", rel, f"第 {lineno} 行含明文 http 链接（{host}）——一律禁止"))
                elif not _domain_allowed(host):
                    findings.append(
                        _err("OSCA050", rel, f"第 {lineno} 行链接域名 {host} 不在公开文档白名单（SPEC §13）")
                    )
    return findings


# ── 分层与权属（SPEC v0.4 §9：commons/org 命名空间 + 洁净室）──────────────────

JUDGMENT_SCOPES = {"commons", "org"}
PROVENANCE_ORIGINS = {"own-ops", "public-standard", "client-derived", "licensed"}
CLASSIFICATIONS = {"public", "internal", "restricted"}


def _layering_validity(rule_id: str, relpath: str, scope: object, prov: object, cls: object) -> list[Finding]:
    """分层三字段的枚举 / 形状 / 洁净室校验——judgment（OSCA060）与 osca.yaml 包级默认段
    （OSCA061）共用同一判据。只管「存在即合法」，缺失是否报由调用方按语境决定。

    枚举判定先过 isinstance(str)：不可信 YAML 里 scope/classification/origin 可能是 list/mapping
    （不可哈希），直接进 set 成员测试会 TypeError——run_all 兜底虽不炸，但报错退化成不指字段的
    「规则执行异常」且吞掉本规则其余 findings。类型防御在此，报错精确到字段。"""
    findings: list[Finding] = []
    if scope not in (None, "") and (not isinstance(scope, str) or scope not in JUDGMENT_SCOPES):
        findings.append(_err(rule_id, relpath, f"scope={scope!r} 不在 {sorted(JUDGMENT_SCOPES)} 中"))

    origin = None
    if prov not in (None, "", {}):
        if not isinstance(prov, dict):
            findings.append(
                _err(rule_id, relpath, f"provenance 必须是 mapping（origin/source/rights），现为 {type(prov).__name__}")
            )
        else:
            for key in ("origin", "source", "rights"):
                if prov.get(key) in (None, ""):
                    findings.append(_err(rule_id, relpath, f"provenance 缺 {key}——权属血统无法事后重建，出生即填"))
            origin = prov.get("origin")
            if origin not in (None, "") and (not isinstance(origin, str) or origin not in PROVENANCE_ORIGINS):
                findings.append(
                    _err(rule_id, relpath, f"provenance.origin={origin!r} 不在 {sorted(PROVENANCE_ORIGINS)} 中")
                )

    if cls not in (None, "") and (not isinstance(cls, str) or cls not in CLASSIFICATIONS):
        findings.append(_err(rule_id, relpath, f"classification={cls!r} 不在 {sorted(CLASSIFICATIONS)} 中"))

    if scope == "commons":
        if origin == "client-derived":
            findings.append(
                _err(
                    rule_id,
                    relpath,
                    "洁净室：origin=client-derived 的判断不得进 commons——合法入口只有 "
                    "own-ops / public-standard / licensed（客户判断出生在 org，永不静默迁移）",
                )
            )
        if cls != "public":
            findings.append(_err(rule_id, relpath, "commons 层定义=可迁移且无密级：classification 必须是 public"))
    return findings


@rule
def osca060_layering(pkg: OscaPackage) -> list[Finding]:
    """OSCA060 判断分层与权属三字段（SPEC v0.4 §9）：scope / provenance / classification。

    权属血统无法事后重建——client-derived 混进 commons 是不可逆污染，必须在出生时机器布防：
    三字段缺失记 warn（v0.4 起新生判断必填；存量包过渡期不硬拦）；枚举非法 / provenance
    形状缺陷记 error；洁净室与无密级约束（commons 不收 client-derived、commons 必须 public）
    记 error。
    """
    findings: list[Finding] = []
    for f in _judgments(pkg):
        scope = f.mapping.get("scope")
        prov = f.mapping.get("provenance")
        cls = f.mapping.get("classification")

        missing = [
            name
            for name, value in (("scope", scope), ("provenance", prov), ("classification", cls))
            if value in (None, "", {})
        ]
        if missing:
            findings.append(
                _warn(
                    "OSCA060",
                    f.relpath,
                    f"缺分层权属字段 {'/'.join(missing)}（SPEC v0.4 §9：新生判断必填；存量包过渡期警告）",
                )
            )
        findings.extend(_layering_validity("OSCA060", f.relpath, scope, prov, cls))
    return findings


@rule
def osca061_package_layering_default(pkg: OscaPackage) -> list[Finding]:
    """OSCA061 osca.yaml 包级分层默认段（SPEC v0.4 §1/§9）：`layering: {scope, provenance, classification}`。

    蒸馏 confirm 出生判断按此段填三字段（不填则新账本永远带 OSCA060 warn）。默认段可选、可部分；
    present 即按 OSCA060 同一判据校验枚举 / 形状 / 洁净室——错的默认会污染整包新生判断，在源头拦
    （osca.yaml）比逐条 judgment 报更早。缺段合法（judgment 缺字段自有 OSCA060 warn）。
    """
    f = pkg.yaml_files.get("osca.yaml")
    if f is None or f.parse_error:
        return []  # 缺失 / 解析失败由 OSCA001/003/004 报
    layering = f.mapping.get("layering")
    if layering in (None, "", {}):
        return []
    if not isinstance(layering, dict):
        return [
            _err(
                "OSCA061",
                "osca.yaml",
                f"layering 必须是 mapping（scope/provenance/classification），现为 {type(layering).__name__}",
            )
        ]
    return _layering_validity(
        "OSCA061", "osca.yaml", layering.get("scope"), layering.get("provenance"), layering.get("classification")
    )


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
