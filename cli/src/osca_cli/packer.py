"""osca pack / load —— 开发态（git 仓库）↔ 交付态（zip）。

pack 的三条纪律：
1. lint 不过，不打包——交付件必须是合规资产（账本纪律的机器化延伸）
2. 真实 bindings 永不进包（SPEC §4 层2）；indexes/ 是缓存不进包（公理 A4），
   但 pack 会生成 indexes/checksums.txt 作为完整性清单（osca.yaml integrity 所指）
3. 打包可复现：同样内容 → 同样字节 → 同样哈希，交付件才可签名、可比对

load 的四步：解压（或原地）→ 完整性校验（防篡改）→ lint → binding 比对，
最后重建 indexes/judgments.index.yaml 签名表（判断检索契约 §7 的硬过滤输入）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from osca_cli import __version__
from osca_cli.ledger import open_ledger_dir, publish_file_in_dir
from osca_cli.lint import lint_package
from osca_cli.package import load_package

CHECKSUMS_REL = "indexes/checksums.txt"
EXCLUDE_TOP_DIRS = {"indexes", ".git"}
EXCLUDE_NAMES = {".DS_Store"}
FORBIDDEN_NAMES = {"bindings.yaml"}  # 真实 binding，永不进包
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # 固定时间戳 → 可复现打包

# zip bomb 防护上限（.osca 包是纯文本 Markdown/YAML，正常包远小于这些数）
MAX_ZIP_MEMBERS = 2000
MAX_MEMBER_BYTES = 50 * 1024 * 1024  # 单成员解压上限
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 总解压量上限


@dataclass
class OpResult:
    """pack/load 的执行结果：逐步骤的人可读记录 + 总判定。"""

    lines: list[str] = field(default_factory=list)
    ok: bool = True

    def step(self, message: str) -> None:
        self.lines.append(f"✓ {message}")

    def fail(self, message: str) -> None:
        self.lines.append(f"✗ {message}")
        self.ok = False

    def info(self, message: str) -> None:
        self.lines.append(f"· {message}")

    def render(self, title: str) -> str:
        verdict = "✓ 完成" if self.ok else "✗ 失败"
        return "\n".join([title, *self.lines, verdict])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def package_files(root: Path) -> list[str]:
    """进包文件清单（排除缓存、版本库、系统垃圾文件），排序保证确定性。

    符号链接一律不入清单——跟随链接会把包外（宿主机）文件当成包内容；
    pack 对链接直接拒绝（symlink_entries），load 侧按不存在处理。
    """
    rels = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.split("/", 1)[0] in EXCLUDE_TOP_DIRS or p.name in EXCLUDE_NAMES:
            continue
        rels.append(rel)
    return rels


def symlink_entries(root: Path) -> list[str]:
    """包内（排除目录之外）的符号链接清单——pack 的拒绝对象。"""
    links = []
    for p in sorted(root.rglob("*")):
        if not p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.split("/", 1)[0] not in EXCLUDE_TOP_DIRS:
            links.append(rel)
    return links


def load_symlink_entries(root: Path) -> list[str]:
    """装载门禁的符号链接清单：包内**全部**符号链接（含 indexes/，仅豁免 .git 版本库内部）。

    与 pack 侧 symlink_entries 的差别：load 连排除目录也不放过——`indexes` 被换成包外目录
    链接时 rebuild_index 会把索引写出包根；`AGENT.md`/YAML 是链接时包外文件会被读进
    Episode/LLM 上下文。os.walk 不跟随目录链接：链接本身按条目拒绝，不进入其目标扫描。
    """
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root and ".git" in dirnames:
            dirnames.remove(".git")
        for name in (*dirnames, *filenames):
            p = Path(dirpath, name)
            if p.is_symlink():
                links.append(p.relative_to(root).as_posix())
    return sorted(links)


def checksums_text(root: Path, rels: list[str]) -> str:
    lines = [f"sha256:{_sha256(root / rel)}  {rel}" for rel in sorted(rels)]
    return "\n".join(lines) + "\n"


# ───────────────────────── pack ─────────────────────────


def pack_package(path: str | Path, output: str | Path | None = None) -> tuple[OpResult, Path | None]:
    root = Path(path)
    result = OpResult()

    # 1. lint 门禁
    lint_result = lint_package(root)
    if not lint_result.ok:
        for f in lint_result.findings:
            result.info(f.format())
        result.fail(f"lint 未通过（{lint_result.errors} 错误）——交付件必须先合规，拒绝打包")
        return result, None
    result.step(f"lint 通过（{lint_result.warnings} 警告）")

    # 2. 真实 bindings 拦截
    for name in FORBIDDEN_NAMES:
        if (root / name).exists():
            result.fail(f"检测到 {name}（真实部署绑定）——铁律：真实 binding 永不进包，请移除后再打包")
            return result, None
    result.step("零真实 binding 确认")

    # 3. 符号链接拦截：跟随链接会把宿主机文件打进交付件
    links = symlink_entries(root)
    if links:
        shown = "、".join(links[:3]) + ("…" if len(links) > 3 else "")
        result.fail(f"检测到符号链接：{shown}——交付件不收符号链接（防止把包外文件打进包），请替换为真实文件")
        return result, None
    result.step("零符号链接确认")

    # 4. 清单与校验和
    rels = package_files(root)
    checksums = checksums_text(root, rels)
    result.step(f"进包文件 {len(rels)} 个，已生成校验和清单")

    # 5. 确定性写 zip
    manifest = load_package(root).yaml_files.get("osca.yaml")
    package_id = manifest.mapping.get("package_id", root.name) if manifest else root.name
    zip_path = Path(output) if output else Path.cwd() / f"{package_id}.osca.zip"
    if zip_path.resolve().is_relative_to(root.resolve()):
        result.fail(f"输出路径在包内：{zip_path}——下次打包会把交付件吞进自身、连续打包哈希漂移，请输出到包外")
        return result, None
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rels:
            info = zipfile.ZipInfo(rel, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (root / rel).read_bytes())
        info = zipfile.ZipInfo(CHECKSUMS_REL, date_time=ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, checksums)

    result.step(f"交付件已生成：{zip_path}（可复现打包，同内容同哈希）")
    result.info(f"交付件 sha256：{_sha256(zip_path)}")
    return result, zip_path


# ───────────────────────── load ─────────────────────────


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """防 zip-slip 与 zip bomb：路径越界、成员数、单成员与总解压量超限一律拒绝。"""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError(f"zip 成员数 {len(infos)} 超上限 {MAX_ZIP_MEMBERS}——拒绝解压（zip bomb 防护）")
    total = 0
    for info in infos:
        target = (dest / info.filename).resolve()
        if not target.is_relative_to(dest.resolve()):
            raise ValueError(f"zip 成员路径越界：{info.filename}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(
                f"zip 成员 {info.filename} 解压后 {info.file_size} 字节超单成员上限——拒绝解压（zip bomb 防护）"
            )
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"zip 总解压量 {total} 字节超上限 {MAX_TOTAL_BYTES}——拒绝解压（zip bomb 防护）")
    zf.extractall(dest)


def verify_checksums(root: Path, result: OpResult) -> bool:
    checks_path = root / CHECKSUMS_REL
    expected: dict[str, str] = {}
    for line in checks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        expected[rel] = digest.removeprefix("sha256:")

    actual = set(package_files(root))
    ok = True
    for rel, digest in sorted(expected.items()):
        if rel not in actual:
            result.fail(f"完整性校验失败：清单中的 {rel} 缺失")
            ok = False
        elif _sha256(root / rel) != digest:
            result.fail(f"完整性校验失败：{rel} 内容与校验和不符（疑似被篡改）")
            ok = False
    for rel in sorted(actual - set(expected)):
        result.fail(f"完整性校验失败：{rel} 不在校验和清单中（多出的文件）")
        ok = False
    if ok:
        result.step(f"完整性校验通过（{len(expected)} 个文件与清单一致）")
    return ok


def signature_entries(pkg) -> list[dict]:
    """判断签名表条目（检索硬过滤的输入形状）——rebuild_index 与 Host 装配共用。

    Host 装配直接调用本函数从已校验的包快照生成，不读磁盘缓存：坏缓存不可能把
    判断静默清空（fail-open），也没有「刷新完成 → 装配读盘」的 TOCTOU 窗口。
    """
    entries = []
    for f in pkg.typed_files("judgments"):
        sig = f.mapping.get("signature") or {}
        meta = f.mapping.get("meta") or {}
        entries.append(
            {
                "judgment_id": f.mapping.get("judgment_id"),
                "status": f.mapping.get("status"),
                "object": sig.get("object"),
                "aware": sig.get("aware"),
                "guard": sig.get("guard"),
                "trust": meta.get("trust"),
            }
        )
    return sorted(entries, key=lambda e: e["judgment_id"] or "")


def rebuild_index(root: Path, pkg=None) -> Path:
    """重建判断签名表（检索契约 §7 第 1 段的硬过滤输入）。索引是缓存，坏了随时重建（公理 A4）。

    pkg 可传入已解析的 OscaPackage 复用（调用方刚解析过时省一次全包解析）。
    """
    pkg = pkg if pkg is not None else load_package(root)
    index = {
        "generated_by": f"osca load {__version__}",
        "note": "机器生成的缓存，人不手写；坏了删掉重建（公理 A4）",
        "judgments": signature_entries(pkg),
    }
    data = yaml.safe_dump(index, allow_unicode=True, sort_keys=False).encode("utf-8")
    # 安全目录发布（与 settle 落账同一机制）：fd 锚定包根 + O_NOFOLLOW 打开 indexes/——
    # indexes 或索引文件被预置成符号链接时在此拒绝，索引写入永远出不了包根
    with open_ledger_dir(root, "indexes") as fd:
        publish_file_in_dir(fd, "judgments.index.yaml", data, overwrite=True)
    return root / "indexes" / "judgments.index.yaml"


def deployment_binding_errors(env: object, required: set[str]) -> list[str]:
    """部署 bindings 的装载门禁判据（CLI load 与 Host 装载共用，单一真理源）：
    顶层必须是 mapping[str, mapping]；required binding 必须存在且带非空字符串 endpoint；
    secret_ref 键存在时必须是非空字符串。任何一条不过都不得称为部署装载成功。"""
    if not isinstance(env, dict):
        return [f"bindings 顶层必须是 mapping[str, mapping]（现为 {type(env).__name__}）——拒绝装载"]
    errors: list[str] = []
    for key, value in env.items():
        if not isinstance(key, str):
            errors.append(f"binding 键必须是字符串（现为 {type(key).__name__}: {key!r}）")
        elif not isinstance(value, dict):
            errors.append(f"binding「{key}」的值必须是 mapping（endpoint[, secret_ref]），现为 {type(value).__name__}")
    missing = sorted(required - {k for k in env if isinstance(k, str)})
    if missing:
        errors.append(f"部署环境缺少 binding：{', '.join(missing)}（loader 装载时缺失即报错）")
    for ref in sorted(required):
        value = env.get(ref)
        if not isinstance(value, dict):
            continue  # 缺失/形状错误已在上面报
        endpoint = value.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            errors.append(f"required binding「{ref}」缺非空 endpoint——装载门禁，首次调用才炸是 fail-open")
        if "secret_ref" in value and (not isinstance(value["secret_ref"], str) or not value["secret_ref"]):
            errors.append(f"binding「{ref}」的 secret_ref 须为非空字符串（无凭据应删该字段，不留空/非法值）")
    return sorted(errors)


def required_bindings(root: Path) -> set[str]:
    pkg = load_package(root)
    required: set[str] = set()
    manifest = pkg.yaml_files.get("osca.yaml")
    if manifest:
        requires = manifest.mapping.get("requires") or {}
        if isinstance(requires, dict):
            required |= set(requires.get("bindings") or [])
    for f in pkg.typed_files("connectors"):
        ref = f.mapping.get("binding_ref")
        if isinstance(ref, str):
            required.add(ref)
    return required


def _extract_to_dest(source: Path, root: Path) -> str | None:
    """zip → 目标目录：版本化临时目录解压 + 原子切换（同一 dest 可重启 Host / unload+load 重复装载）。

    目标已存在且非空时只接管**既往 osca 交付解压目录**（osca.yaml + indexes/checksums.txt 痕迹）——
    绝不清理来历不明的用户目录；切换失败回滚旧目录。返回错误串或 None（成功）。
    """
    if root.is_symlink():
        return f"解压目标是符号链接：{root}——拒绝（解压不得跟随链接写出目标外）"
    if root.exists():
        if not root.is_dir():
            return f"解压目标已存在且不是目录：{root}"
        if any(root.iterdir()) and not ((root / "osca.yaml").is_file() and (root / CHECKSUMS_REL).is_file()):
            return f"目标目录非空且不是既往 osca 交付解压目录：{root}——拒绝清理未知目录（用 --dest 指定其他目录）"
    tmp = root.parent / f".{root.name}.osca-tmp-{os.getpid()}-{time.monotonic_ns()}"
    old: Path | None = None
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(exist_ok=False)
        with zipfile.ZipFile(source) as zf:
            _safe_extract(zf, tmp)
        if root.exists():
            old = root.parent / f".{root.name}.osca-old-{os.getpid()}-{time.monotonic_ns()}"
            os.rename(root, old)
        os.rename(tmp, root)
    except (OSError, ValueError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        if old is not None and old.exists() and not root.exists():
            os.rename(old, root)  # 回滚：旧交付目录归位，装载失败不留半切换状态
        return str(e) if isinstance(e, ValueError) else f"解压/切换失败：{e}"
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return None


def load_osca(
    archive: str | Path,
    dest: str | Path | None = None,
    bindings: str | Path | None = None,
    *,
    require_bindings: bool = False,
) -> tuple[OpResult, Path | None]:
    """装载校验。require_bindings=True（Host 部署装载）：包声明了 required bindings 却未注入
    部署环境即失败——「无环境只校验包」是 CLI 的显式校验模式，不得称为部署装载成功。"""
    source = Path(archive)
    result = OpResult()

    # 1. 解压 or 原地
    if source.is_dir():
        root = source
        from_zip = False
        result.info(f"输入为目录，原地装载校验：{root}")
    elif source.is_file() and zipfile.is_zipfile(source):
        root = Path(dest) if dest else Path.cwd() / source.name.removesuffix(".zip")
        error = _extract_to_dest(source, root)
        if error:
            result.fail(error)
            return result, None
        from_zip = True
        result.step(f"已解压到 {root}（版本化临时目录 + 原子切换，同 dest 可重启/重载）")
    else:
        result.fail(f"输入既不是目录也不是 zip：{archive}")
        return result, None

    # 1.5 符号链接门禁（读取/lint 之前）：链接可把包外文件读进 Episode/LLM 上下文（AGENT.md/YAML），
    # 或把 indexes 写引出包根（rebuild_index 覆盖包外文件）。zip 解压不产生链接，此检查两态统一兜底。
    links = load_symlink_entries(root)
    if links:
        shown = "、".join(links[:3]) + ("…" if len(links) > 3 else "")
        result.fail(f"包内检测到符号链接：{shown}——装载拒绝（链接可读写包外文件；交付件不收符号链接）")
        return result, None

    # 2. 完整性校验（交付件必须带清单；开发态目录可豁免）
    if (root / CHECKSUMS_REL).is_file():
        if not verify_checksums(root, result):
            return result, None
    elif from_zip:
        result.fail(f"交付件缺少 {CHECKSUMS_REL}——不是 osca pack 产出的合规交付件")
        return result, None
    else:
        result.info("开发态目录无校验和清单，跳过完整性校验（交付件不可跳过）")

    # 3. lint
    lint_result = lint_package(root)
    if not lint_result.ok:
        for f in lint_result.findings:
            result.info(f.format())
        result.fail(f"lint 未通过（{lint_result.errors} 错误），拒绝装载")
        return result, None
    result.step(f"lint 通过（{lint_result.warnings} 警告）")

    # 4. binding 与部署环境比对（SPEC §4 层2；缺失/形状非法即报错——装载门禁，不留到首次调用才炸）
    required = required_bindings(root)
    if bindings is not None:
        try:
            env = yaml.safe_load(Path(bindings).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            result.fail(f"bindings 文件读取/解析失败：{e}")
            return result, None
        errors = deployment_binding_errors({} if env is None else env, required)
        if errors:
            for error in errors:
                result.fail(error)
            return result, None
        result.step(f"binding 门禁通过（required {len(required)} 个均已注入且形状合法）")
    elif required and require_bindings:
        result.fail(
            f"部署装载必须注入 bindings：本包 required binding {', '.join(sorted(required))} 缺失"
            "（装载门禁 fail-closed；「无环境只校验包」请走 CLI 校验模式）"
        )
        return result, None
    elif required:
        result.info(
            f"未提供 --bindings：仅完成包校验（**非部署装载**，binding 门禁未执行）；"
            f"部署时必须注入：{', '.join(sorted(required))}"
        )

    # 5. 重建索引
    index_path = rebuild_index(root)
    result.step(f"签名表已重建：{index_path.relative_to(root).as_posix()}")
    return result, root
