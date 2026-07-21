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

import fcntl
import hashlib
import os
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
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


class LoadAborted(Exception):
    """装载作废令牌在写边界命中（四轮复核 P1）——调用方转稳定装载失败，不是异常路径。"""


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

    # 4. 清单与校验和——同一次读取的同一份字节既算摘要又进归档（P2 关 TOCTOU 窗：
    # 摘要后二次读盘写 zip，并发修改会产出自校验必败的交付件）
    rels = package_files(root)
    blobs: dict[str, bytes] = {}
    lines = []
    for rel in sorted(rels):
        data = (root / rel).read_bytes()
        blobs[rel] = data
        lines.append(f"sha256:{hashlib.sha256(data).hexdigest()}  {rel}")
    checksums = "\n".join(lines) + "\n"
    result.step(f"进包文件 {len(rels)} 个，已生成校验和清单")

    # 5. 确定性写 zip。package_id 从**同一字节快照**解析（P3 口径收口）：重读实时 osca.yaml 会留
    # 「输出文件名与归档内 manifest 并发漂移」的窗——交付件名与归档内容必须出自同一份字节。
    try:
        manifest_data = yaml.safe_load(blobs["osca.yaml"].decode("utf-8")) if "osca.yaml" in blobs else None
    except (yaml.YAMLError, UnicodeDecodeError):
        manifest_data = None  # lint 已过；防御性兜底（快照间隙不可达，仍不许炸）
    package_id = manifest_data.get("package_id", root.name) if isinstance(manifest_data, dict) else root.name
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
            zf.writestr(info, blobs[rel])  # 与摘要同一份字节快照（不二次读盘）
        info = zipfile.ZipInfo(CHECKSUMS_REL, date_time=ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, checksums)

    result.step(f"交付件已生成：{zip_path}（可复现打包，同内容同哈希）")
    result.info(f"交付件 sha256：{_sha256(zip_path)}")
    return result, zip_path


# ───────────────────────── load ─────────────────────────


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """防 zip-slip 与 zip bomb：路径越界、成员数、单成员与总解压量超限一律拒绝。

    排除目录成员**不解压**（P2）：indexes/、.git/ 不进校验和清单——zip 可夹带不受校验的
    `indexes/replay-health.json`（伪健康档案）/向量缓存/钩子。归档缓存一律忽略，缓存只由
    装载后 rebuild_index 按已校验内容重建；唯一例外是清单自身 indexes/checksums.txt。
    """
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError(f"zip 成员数 {len(infos)} 超上限 {MAX_ZIP_MEMBERS}——拒绝解压（zip bomb 防护）")
    total = 0
    members: list[str] = []
    for info in infos:
        target = (dest / info.filename).resolve()
        if not target.is_relative_to(dest.resolve()):
            raise ValueError(f"zip 成员路径越界：{info.filename}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(
                f"zip 成员 {info.filename} 解压后 {info.file_size} 字节超单成员上限——拒绝解压（zip bomb 防护）"
            )
        total += info.file_size
        top = info.filename.split("/", 1)[0]
        if top in EXCLUDE_TOP_DIRS and info.filename != CHECKSUMS_REL:
            continue  # 归档缓存/版本库夹带：不解压（清单外内容不落地）
        members.append(info.filename)
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"zip 总解压量 {total} 字节超上限 {MAX_TOTAL_BYTES}——拒绝解压（zip bomb 防护）")
    zf.extractall(dest, members=members)


CHECKSUM_LINE = re.compile(r"sha256:([0-9a-f]{64})  (.+)")


def verify_checksums(root: Path, result: OpResult) -> bool:
    """校验和比对。清单自身损坏（不可读/非 UTF-8/行格式非法）→ 稳定装载失败（P2），
    不许 ValueError/traceback 穿透——清单是完整性根基，坏了不猜、不兜底。"""
    checks_path = root / CHECKSUMS_REL
    try:
        text = checks_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        result.fail(f"校验和清单读取失败（{type(e).__name__}）——清单损坏，装载失败")
        return False
    expected: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        m = CHECKSUM_LINE.fullmatch(line)
        if m is None:
            result.fail(f"校验和清单第 {lineno} 行格式非法——清单损坏，装载失败（格式：sha256:<hex64>␣␣<相对路径>）")
            return False
        expected[m.group(2)] = m.group(1)

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


def rebuild_index(root: Path, pkg=None, *, abort: Callable[[], str | None] | None = None) -> Path:
    """重建判断签名表（检索契约 §7 第 1 段的硬过滤输入）。索引是缓存，坏了随时重建（公理 A4）。

    pkg 可传入已解析的 OscaPackage 复用（调用方刚解析过时省一次全包解析）。
    abort（四轮复核 P1）：作废令牌在**写入边界内**（目录 fd 已开、发布之前）复核——
    「检查通过后、写入之前作废」的窗由此关死；命中抛 LoadAborted，零发布。
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
        if abort is not None and (why := abort()):
            raise LoadAborted(why)
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


def _dest_error(root: Path) -> str | None:
    """zip 发布目标的可接管性判据（**只查不动**）：目标非空时只接管既往 osca 交付解压目录
    （osca.yaml + indexes/checksums.txt 痕迹）——绝不清理来历不明的用户目录。"""
    if root.is_symlink():
        return f"解压目标是符号链接：{root}——拒绝（解压不得跟随链接写出目标外）"
    if root.exists():
        if not root.is_dir():
            return f"解压目标已存在且不是目录：{root}"
        if any(root.iterdir()) and not ((root / "osca.yaml").is_file() and (root / CHECKSUMS_REL).is_file()):
            return f"目标目录非空且不是既往 osca 交付解压目录：{root}——拒绝清理未知目录（用 --dest 指定其他目录）"
    return None


@contextmanager
def _swap_lock(root: Path):
    """dest 的**跨进程**切换互斥（复核 P1）：flock 锁文件住 dest 旁——两个部署进程对同一 dest
    的「挪旧/上位/删旧」序列串行化；进程内 asyncio 锁关不住跨进程并发。"""
    lock_path = root.parent / f".{root.name}.osca-swap.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _swap_into_dest(tmp: Path, root: Path, abort: Callable[[], str | None] | None = None) -> str | None:
    """**已通过全部校验**的临时目录原子上位（P1 升级安全）：跨进程 flock 内挪旧 → 上位 → 删旧。

    上位失败的恢复纪律（复核 P1）：若 root 已被锁外闯入者占用，先把未知内容**隔离**到
    quarantine 再把旧部署归位——绝不静默把错误内容留在 dest、也绝不丢弃合法旧部署。
    abort（四轮复核 P1）：flock 等待可以无限长——锁前的作废检查关不住「等锁期间 STOPPED、
    释放后迟到切换」；**取得锁之后、第一次 rename 之前**必须复核。
    """
    old: Path | None = None
    try:
        with _swap_lock(root):
            if abort is not None and (why := abort()):
                shutil.rmtree(tmp, ignore_errors=True)
                return f"装载已作废：{why}——取得切换锁后复核止步，dest 未被触碰（迟到切换零副作用）"
            # 锁内重查可接管性：_dest_error 首查与拿锁之间 dest 可能被并发换成用户内容——
            # 锁内不过判据就放弃，绝不把来历不明的目录挪走再删掉
            error = _dest_error(root)
            if error:
                shutil.rmtree(tmp, ignore_errors=True)
                return f"{error}（切换前锁内复核）"
            if root.exists():
                old = root.parent / f".{root.name}.osca-old-{os.getpid()}-{time.monotonic_ns()}"
                os.rename(root, old)
            try:
                os.rename(tmp, root)
            except OSError as e:
                shutil.rmtree(tmp, ignore_errors=True)
                if old is None:
                    return f"发布切换失败：{e}"
                notes = []
                try:
                    if root.exists() or root.is_symlink():
                        quarantine = root.parent / f".{root.name}.osca-quarantine-{os.getpid()}-{time.monotonic_ns()}"
                        os.rename(root, quarantine)
                        notes.append(f"锁外并发占用内容已隔离到 {quarantine}")
                    os.rename(old, root)
                    notes.append("旧部署已恢复原位")
                except OSError as restore_error:
                    notes.append(f"恢复失败（旧部署暂存于 {old}，请人工归位）：{restore_error}")
                return f"发布切换失败：{e}（{'；'.join(notes)}）"
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return f"发布切换失败（swap 跨进程锁）：{e}"
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return None


def _validate_package_root(
    root: Path,
    result: OpResult,
    *,
    from_zip: bool,
    bindings: str | Path | None,
    require_bindings: bool,
    abort: Callable[[], str | None] | None = None,
) -> bool:
    """装载校验流水线（符号链接 → 完整性 → lint → binding 门禁 → 重建索引），全过才 True。

    zip 模式在**临时目录**上整段执行（P1 升级安全）：任何一步失败时 dest 上的旧部署一字节不动。
    """
    # 符号链接门禁（读取/lint 之前）：链接可把包外文件读进 Episode/LLM 上下文（AGENT.md/YAML），
    # 或把 indexes 写引出包根（rebuild_index 覆盖包外文件）。zip 解压不产生链接，此检查两态统一兜底。
    links = load_symlink_entries(root)
    if links:
        shown = "、".join(links[:3]) + ("…" if len(links) > 3 else "")
        result.fail(f"包内检测到符号链接：{shown}——装载拒绝（链接可读写包外文件；交付件不收符号链接）")
        return False

    # 完整性校验（交付件必须带清单；开发态目录可豁免）
    if (root / CHECKSUMS_REL).is_file():
        if not verify_checksums(root, result):
            return False
    elif from_zip:
        result.fail(f"交付件缺少 {CHECKSUMS_REL}——不是 osca pack 产出的合规交付件")
        return False
    else:
        result.info("开发态目录无校验和清单，跳过完整性校验（交付件不可跳过）")

    # lint
    lint_result = lint_package(root)
    if not lint_result.ok:
        for f in lint_result.findings:
            result.info(f.format())
        result.fail(f"lint 未通过（{lint_result.errors} 错误），拒绝装载")
        return False
    result.step(f"lint 通过（{lint_result.warnings} 警告）")

    # binding 与部署环境比对（SPEC §4 层2；缺失/形状非法即报错——装载门禁，不留到首次调用才炸）
    required = required_bindings(root)
    if bindings is not None:
        try:
            env = yaml.safe_load(Path(bindings).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            result.fail(f"bindings 文件读取/解析失败：{e}")
            return False
        errors = deployment_binding_errors({} if env is None else env, required)
        if errors:
            for error in errors:
                result.fail(error)
            return False
        result.step(f"binding 门禁通过（required {len(required)} 个均已注入且形状合法）")
    elif required and require_bindings:
        result.fail(
            f"部署装载必须注入 bindings：本包 required binding {', '.join(sorted(required))} 缺失"
            "（装载门禁 fail-closed；「无环境只校验包」请走 CLI 校验模式）"
        )
        return False
    elif required:
        result.info(
            f"未提供 --bindings：仅完成包校验（**非部署装载**，binding 门禁未执行）；"
            f"部署时必须注入：{', '.join(sorted(required))}"
        )

    # 装载作废令牌（复核 P1）：rebuild_index 是本流水线里**第一处磁盘写**（目录模式直接写真实
    # 包目录）——迟到 load worker 在写之前复核；写入边界内（fd 已开、发布前）由 rebuild_index
    # 再复核一次（四轮复核 P1：检查与写入之间无线性化屏障的窗关死），命中即 LoadAborted 零发布
    if abort is not None and (why := abort()):
        result.fail(f"装载已作废：{why}——校验止步，不写入索引（迟到 load 零磁盘副作用）")
        return False

    # 重建索引（zip 模式建在临时目录里，随原子切换一并上位）
    try:
        index_path = rebuild_index(root, abort=abort)
    except LoadAborted as e:
        result.fail(f"装载已作废：{e}——索引写入边界复核止步（零发布）")
        return False
    result.step(f"签名表已重建：{index_path.relative_to(root).as_posix()}")
    return True


def load_osca(
    archive: str | Path,
    dest: str | Path | None = None,
    bindings: str | Path | None = None,
    *,
    require_bindings: bool = False,
    abort: Callable[[], str | None] | None = None,
) -> tuple[OpResult, Path | None]:
    """装载校验。require_bindings=True（Host 部署装载）：包声明了 required bindings 却未注入
    部署环境即失败——「无环境只校验包」是 CLI 的显式校验模式，不得称为部署装载成功。

    zip 模式**先验后切换**（P1 升级安全）：全部校验（符号链接/完整性/lint/binding/索引）在
    版本化临时目录完成，全过才原子切换 dest——失败的升级绝不销毁上一版部署。

    abort（复核 P1 作废令牌）：调用方（Host）注入的线程安全检查，返回非 None 即装载已作废
    （关停/换代）。在**每处磁盘写副作用之前**复核（目录模式的 rebuild_index、zip 模式的
    dest 切换）——被取消的迟到 load worker 不许在 STOPPED 之后修改部署目录。
    """
    source = Path(archive)
    result = OpResult()

    if source.is_dir():
        result.info(f"输入为目录，原地装载校验：{source}")
        ok = _validate_package_root(
            source, result, from_zip=False, bindings=bindings, require_bindings=require_bindings, abort=abort
        )
        return (result, source) if ok else (result, None)

    if not (source.is_file() and zipfile.is_zipfile(source)):
        result.fail(f"输入既不是目录也不是 zip：{archive}")
        return result, None

    root = Path(dest) if dest else Path.cwd() / source.name.removesuffix(".zip")
    error = _dest_error(root)
    if error:
        result.fail(error)
        return result, None
    tmp = root.parent / f".{root.name}.osca-tmp-{os.getpid()}-{time.monotonic_ns()}"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(exist_ok=False)
        with zipfile.ZipFile(source) as zf:
            _safe_extract(zf, tmp)
    except (OSError, ValueError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        result.fail(str(e) if isinstance(e, ValueError) else f"解压失败：{e}")
        return result, None
    result.step("已解压到临时目录（先验后切换：全部校验过关才动 dest）")
    if not _validate_package_root(
        tmp, result, from_zip=True, bindings=bindings, require_bindings=require_bindings, abort=abort
    ):
        shutil.rmtree(tmp, ignore_errors=True)  # 校验失败：只清临时目录，dest 上的旧部署一字节不动
        return result, None
    # 切换前复核作废令牌（复核 P1，快路径——真正的屏障在 _swap_into_dest 取得 flock 之后）
    if abort is not None and (why := abort()):
        shutil.rmtree(tmp, ignore_errors=True)
        result.fail(f"装载已作废：{why}——切换取消，dest 未被触碰（迟到 load 零磁盘副作用）")
        return result, None
    error = _swap_into_dest(tmp, root, abort=abort)
    if error:
        result.fail(error)
        return result, None
    result.step(f"已发布到 {root}（原子切换；同 dest 可重启/重载，升级失败不销毁旧部署）")
    return result, root
