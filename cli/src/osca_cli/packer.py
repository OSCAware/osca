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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from osca_cli import __version__
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


def rebuild_index(root: Path, pkg=None) -> Path:
    """重建判断签名表（检索契约 §7 第 1 段的硬过滤输入）。索引是缓存，坏了随时重建（公理 A4）。

    pkg 可传入已解析的 OscaPackage 复用（调用方刚解析过时省一次全包解析）。
    """
    pkg = pkg if pkg is not None else load_package(root)
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
    index = {
        "generated_by": f"osca load {__version__}",
        "note": "机器生成的缓存，人不手写；坏了删掉重建（公理 A4）",
        "judgments": sorted(entries, key=lambda e: e["judgment_id"] or ""),
    }
    index_path = root / "indexes" / "judgments.index.yaml"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(yaml.safe_dump(index, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return index_path


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


def load_osca(
    archive: str | Path,
    dest: str | Path | None = None,
    bindings: str | Path | None = None,
) -> tuple[OpResult, Path | None]:
    source = Path(archive)
    result = OpResult()

    # 1. 解压 or 原地
    if source.is_dir():
        root = source
        from_zip = False
        result.info(f"输入为目录，原地装载校验：{root}")
    elif source.is_file() and zipfile.is_zipfile(source):
        root = Path(dest) if dest else Path.cwd() / source.name.removesuffix(".zip")
        if root.exists() and any(root.iterdir()):
            result.fail(f"目标目录已存在且非空：{root}（用 --dest 指定其他目录）")
            return result, None
        root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(source) as zf:
                _safe_extract(zf, root)
        except ValueError as e:
            result.fail(str(e))
            return result, None
        from_zip = True
        result.step(f"已解压到 {root}")
    else:
        result.fail(f"输入既不是目录也不是 zip：{archive}")
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

    # 4. binding 与部署环境比对（SPEC §4 层2；缺失即报错）
    required = required_bindings(root)
    if bindings is not None:
        env = yaml.safe_load(Path(bindings).read_text(encoding="utf-8")) or {}
        missing = sorted(required - set(env))
        if missing:
            result.fail(f"部署环境缺少 binding：{', '.join(missing)}（loader 装载时缺失即报错）")
            return result, None
        result.step(f"binding 比对通过（{len(required)} 个均已在部署环境注入）")
    elif required:
        result.info(f"未提供 --bindings，跳过环境比对；本包部署时需要注入：{', '.join(sorted(required))}")

    # 5. 重建索引
    index_path = rebuild_index(root)
    result.step(f"签名表已重建：{index_path.relative_to(root).as_posix()}")
    return result, root
