"""osca pack / load 的行为测试：门禁、确定性、防篡改、binding 比对、索引重建。"""

import zipfile

import yaml

from osca_cli import packer
from osca_cli.packer import (
    CHECKSUMS_REL,
    load_osca,
    pack_package,
    rebuild_index,
)

# ── pack ──


def test_pack_creates_zip_with_checksums(make_pkg, base, tmp_path):
    pkg = make_pkg(base)
    result, zip_path = pack_package(pkg, tmp_path / "out.zip")
    assert result.ok, result.render("pack")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert CHECKSUMS_REL in names
        assert "osca.yaml" in names
        checks = zf.read(CHECKSUMS_REL).decode()
    # 校验和清单覆盖包内全部文件（清单自身除外）
    listed = {line.split("  ", 1)[1] for line in checks.strip().splitlines()}
    assert listed == names - {CHECKSUMS_REL}


def test_pack_is_reproducible(make_pkg, base, tmp_path):
    pkg = make_pkg(base)
    _, zip1 = pack_package(pkg, tmp_path / "a.zip")
    _, zip2 = pack_package(pkg, tmp_path / "b.zip")
    assert zip1.read_bytes() == zip2.read_bytes()


def test_pack_refuses_lint_errors(make_pkg, base, tmp_path):
    del base["judgments/J-0001.yaml"]["replay"]  # 违反 OSCA034
    result, zip_path = pack_package(make_pkg(base), tmp_path / "out.zip")
    assert not result.ok
    assert zip_path is None


def test_pack_refuses_real_bindings(make_pkg, base, tmp_path):
    base["bindings.yaml"] = {"DEMO_DB": {"endpoint": "占位而已"}}
    result, zip_path = pack_package(make_pkg(base), tmp_path / "out.zip")
    assert not result.ok
    assert zip_path is None
    assert any("真实 binding" in line for line in result.lines)


def test_pack_excludes_indexes_and_junk(make_pkg, base, tmp_path):
    pkg = make_pkg(base)
    (pkg / "indexes").mkdir()
    (pkg / "indexes" / "judgments.index.yaml").write_text("旧缓存", encoding="utf-8")
    (pkg / ".DS_Store").write_bytes(b"junk")
    _, zip_path = pack_package(pkg, tmp_path / "out.zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert names & {"indexes/judgments.index.yaml", ".DS_Store"} == set()
    assert CHECKSUMS_REL in names  # 缓存不进包，但校验和清单进


def test_pack_refuses_output_inside_package(make_pkg, base, tmp_path):
    """输出落在包内 → 下次打包吞进自身、哈希漂移——破坏可复现承诺，直接拒绝。"""
    pkg = make_pkg(base)
    result, zip_path = pack_package(pkg, pkg / "build.zip")
    assert not result.ok and zip_path is None
    assert any("输出路径在包内" in line for line in result.lines)
    assert not (pkg / "build.zip").exists()


def test_pack_refuses_symlinks(make_pkg, base, tmp_path):
    """符号链接会把宿主机文件打进交付件——pack 直接拒绝。"""
    pkg = make_pkg(base)
    outside = tmp_path / "宿主机文件.txt"
    outside.write_text("不该进包的内容", encoding="utf-8")
    (pkg / "sql").mkdir(exist_ok=True)
    (pkg / "sql" / "泄露.sql").symlink_to(outside)
    result, zip_path = pack_package(pkg, tmp_path / "out.zip")
    assert not result.ok and zip_path is None
    assert any("符号链接" in line for line in result.lines)


# ── load ──


def _packed(make_pkg, base, tmp_path):
    _, zip_path = pack_package(make_pkg(base), tmp_path / "pkg.osca.zip")
    return zip_path


def test_load_zip_roundtrip(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    result, root = load_osca(zip_path, dest=tmp_path / "deploy")
    assert result.ok, result.render("load")
    index = yaml.safe_load((root / "indexes" / "judgments.index.yaml").read_text(encoding="utf-8"))
    assert index["judgments"][0]["judgment_id"] == "J-0001"
    assert index["judgments"][0]["trust"] == "provisional"


def test_load_detects_tampering(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "deploy"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    j = dest / "judgments" / "J-0001.yaml"
    j.write_text(j.read_text(encoding="utf-8").replace("金额 > 20", "金额 > 9999"), encoding="utf-8")
    result, _ = load_osca(dest)
    assert not result.ok
    assert any("篡改" in line for line in result.lines)


def test_load_detects_extra_file(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "deploy"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    (dest / "cases" / "C-0999.yaml").write_text(
        "case_id: C-0999\ncaptured_at: x\ncapture_source: x\ninput:\n  当时生效判断集: []\n",
        encoding="utf-8",
    )
    result, _ = load_osca(dest)
    assert not result.ok
    assert any("不在校验和清单" in line for line in result.lines)


def test_load_zip_without_checksums_rejected(make_pkg, base, tmp_path):
    bad_zip = tmp_path / "bad.zip"
    pkg = make_pkg(base)
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.write(pkg / "osca.yaml", "osca.yaml")
    result, _ = load_osca(bad_zip, dest=tmp_path / "deploy")
    assert not result.ok
    assert any("合规交付件" in line for line in result.lines)


def test_load_dev_directory_skips_integrity(make_pkg, base):
    result, root = load_osca(make_pkg(base))
    assert result.ok
    assert any("跳过完整性校验" in line for line in result.lines)
    assert (root / "indexes" / "judgments.index.yaml").exists()


def test_load_bindings_missing_key(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    env = tmp_path / "bindings.yaml"
    env.write_text("OTHER_DB:\n  endpoint: x\n", encoding="utf-8")
    result, _ = load_osca(zip_path, dest=tmp_path / "deploy", bindings=env)
    assert not result.ok
    assert any("DEMO_DB" in line for line in result.lines)


def test_load_bindings_complete(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    env = tmp_path / "bindings.yaml"
    env.write_text("DEMO_DB:\n  endpoint: x\n  secret_ref: KEY\n", encoding="utf-8")
    result, _ = load_osca(zip_path, dest=tmp_path / "deploy", bindings=env)
    assert result.ok, result.render("load")
    assert any("binding 门禁通过" in line for line in result.lines)


def test_load_refuses_nonempty_dest(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "已有文件.txt").write_text("x", encoding="utf-8")
    result, _ = load_osca(zip_path, dest=dest)
    assert not result.ok


def test_load_rejects_zip_with_too_many_members(make_pkg, base, tmp_path, monkeypatch):
    monkeypatch.setattr(packer, "MAX_ZIP_MEMBERS", 3)
    zip_path = _packed(make_pkg, base, tmp_path)  # 正常包成员数 > 3
    result, root = load_osca(zip_path, dest=tmp_path / "deploy")
    assert not result.ok and root is None
    assert any("zip bomb" in line for line in result.lines)


def test_load_rejects_oversized_member(make_pkg, base, tmp_path, monkeypatch):
    monkeypatch.setattr(packer, "MAX_MEMBER_BYTES", 64)
    zip_path = _packed(make_pkg, base, tmp_path)  # AGENT.md 等远超 64 字节
    result, root = load_osca(zip_path, dest=tmp_path / "deploy")
    assert not result.ok and root is None
    assert any("单成员上限" in line for line in result.lines)


def test_load_rejects_oversized_total(make_pkg, base, tmp_path, monkeypatch):
    monkeypatch.setattr(packer, "MAX_TOTAL_BYTES", 256)
    zip_path = _packed(make_pkg, base, tmp_path)
    result, root = load_osca(zip_path, dest=tmp_path / "deploy")
    assert not result.ok and root is None
    assert any("总解压量" in line for line in result.lines)


# ── 索引重建 ──


def test_rebuild_index_is_regenerable(make_pkg, base):
    pkg = make_pkg(base)
    path = rebuild_index(pkg)
    first = path.read_text(encoding="utf-8")
    path.write_text("被破坏的缓存", encoding="utf-8")
    rebuild_index(pkg)  # 公理 A4：索引坏了删掉重建
    assert path.read_text(encoding="utf-8") == first


# ── 符号链接装载门禁（P1）：目录模式在读取/lint 前拒绝包内任何链接 ──


def test_load_dir_rejects_agent_symlink(make_pkg, base, tmp_path):
    """AGENT.md 是链接 → 包外敏感文件会进 Episode/LLM system prompt——装载必须失败。"""
    pkg = make_pkg(base)
    secret = tmp_path / "宿主机敏感文件.txt"
    secret.write_text("SENSITIVE", encoding="utf-8")
    (pkg / "AGENT.md").unlink()
    (pkg / "AGENT.md").symlink_to(secret)
    result, root = load_osca(pkg)
    assert not result.ok and root is None
    assert any("符号链接" in line for line in result.lines)


def test_load_dir_rejects_yaml_symlink(make_pkg, base, tmp_path):
    pkg = make_pkg(base)
    outside = tmp_path / "外部.yaml"
    outside.write_text("judgment_id: J-0001\n", encoding="utf-8")
    (pkg / "judgments" / "J-0001.yaml").unlink()
    (pkg / "judgments" / "J-0001.yaml").symlink_to(outside)
    result, root = load_osca(pkg)
    assert not result.ok and root is None
    assert any("符号链接" in line for line in result.lines)


def test_load_dir_rejects_indexes_dir_symlink_and_never_writes_outside(make_pkg, base, tmp_path):
    """indexes → 包外目录：修复前 rebuild_index 会覆盖包外 judgments.index.yaml——现在装载失败且包外文件原样。"""
    pkg = make_pkg(base)
    outside = tmp_path / "外部索引目录"
    outside.mkdir()
    victim = outside / "judgments.index.yaml"
    victim.write_text("包外原内容", encoding="utf-8")
    (pkg / "indexes").symlink_to(outside)
    result, root = load_osca(pkg)
    assert not result.ok and root is None
    assert any("符号链接" in line for line in result.lines)
    assert victim.read_text(encoding="utf-8") == "包外原内容"  # 包外文件未被触碰


def test_load_dir_rejects_index_file_symlink(make_pkg, base, tmp_path):
    pkg = make_pkg(base)
    victim = tmp_path / "包外文件.yaml"
    victim.write_text("包外原内容", encoding="utf-8")
    (pkg / "indexes").mkdir()
    (pkg / "indexes" / "judgments.index.yaml").symlink_to(victim)
    result, root = load_osca(pkg)
    assert not result.ok and root is None
    assert victim.read_text(encoding="utf-8") == "包外原内容"


def test_rebuild_index_refuses_symlinked_indexes(make_pkg, base, tmp_path):
    """rebuild_index 自身的安全目录发布：indexes 被换成链接时 O_NOFOLLOW 拒绝，绝不写出包根。"""
    import pytest

    pkg = make_pkg(base)
    outside = tmp_path / "外部索引"
    outside.mkdir()
    victim = outside / "judgments.index.yaml"
    victim.write_text("包外原内容", encoding="utf-8")
    (pkg / "indexes").symlink_to(outside)
    with pytest.raises(OSError):
        rebuild_index(pkg)
    assert victim.read_text(encoding="utf-8") == "包外原内容"


# ── ZIP 部署可重启/重载（P1）：同一 dest 连续装载 ──


def test_load_zip_same_dest_reload_and_restart(make_pkg, base, tmp_path):
    """同一 zip + 同一 dest：连续装载（unload/load、Host 重启同型）必须都成功。"""
    zip_path = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "deploy"
    for attempt in range(3):
        result, root = load_osca(zip_path, dest=dest)
        assert result.ok, f"第 {attempt + 1} 次装载失败：{result.render('load')}"
        assert (root / "osca.yaml").is_file()
        assert (root / "indexes" / "judgments.index.yaml").is_file()


def test_load_zip_same_dest_picks_up_new_content(make_pkg, base, tmp_path):
    """同 dest 重载新版交付件：内容必须是新版（原子切换,不残留旧文件混装）。"""
    zip1 = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "deploy"
    result, _ = load_osca(zip1, dest=dest)
    assert result.ok
    base["AGENT.md"] = "# 演示 Agent v2\n新版身份。\n"
    _, zip2 = pack_package(make_pkg(base), tmp_path / "v2.osca.zip")
    result, root = load_osca(zip2, dest=dest)
    assert result.ok, result.render("load")
    assert "v2" in (root / "AGENT.md").read_text(encoding="utf-8")


def test_load_zip_refuses_unknown_nonempty_dest(make_pkg, base, tmp_path):
    """dest 非空且不是既往 osca 交付解压目录——拒绝清理用户未知目录。"""
    zip_path = _packed(make_pkg, base, tmp_path)
    dest = tmp_path / "user-data"
    dest.mkdir()
    (dest / "用户文件.txt").write_text("重要数据", encoding="utf-8")
    result, root = load_osca(zip_path, dest=dest)
    assert not result.ok and root is None
    assert any("拒绝清理未知目录" in line for line in result.lines)
    assert (dest / "用户文件.txt").read_text(encoding="utf-8") == "重要数据"  # 用户目录未被动


# ── binding 装载门禁（P1）：形状与 required 完整性 ──


def test_load_bindings_shape_gate(make_pkg, base, tmp_path):
    pkg = make_pkg(base)
    cases = [
        ("- a\n- b\n", "顶层必须是 mapping"),
        ("DEMO_DB: 连接串占位\n", "值必须是 mapping"),
        ("DEMO_DB:\n  secret_ref: KEY\n", "缺非空 endpoint"),
        ("DEMO_DB:\n  endpoint: x\n  secret_ref: ''\n", "secret_ref 须为非空字符串"),
        ("DEMO_DB:\n  endpoint: ''\n", "缺非空 endpoint"),
    ]
    for i, (content, expect) in enumerate(cases):
        env = tmp_path / f"bindings-{i}.yaml"
        env.write_text(content, encoding="utf-8")
        result, root = load_osca(pkg, bindings=env)
        assert not result.ok and root is None, f"应拒绝：{content!r}"
        assert any(expect in line for line in result.lines), f"{content!r} 未报「{expect}」：{result.lines}"


def test_load_require_bindings_fails_without_env(make_pkg, base):
    """部署装载模式（Host 路径）：包声明 required bindings 却未注入 → 装载失败（fail-closed）。"""
    result, root = load_osca(make_pkg(base), require_bindings=True)
    assert not result.ok and root is None
    assert any("部署装载必须注入 bindings" in line for line in result.lines)


def test_load_without_bindings_is_explicit_non_deployment(make_pkg, base):
    """CLI 校验模式保留,但必须显式区分——不能称为部署装载成功。"""
    result, root = load_osca(make_pkg(base))
    assert result.ok and root is not None
    assert any("非部署装载" in line for line in result.lines)


# ── 校验和清单损坏（P2）：稳定装载失败，不许 traceback 穿透 ──


def test_load_corrupted_checksums_stable_failure(make_pkg, base):
    pkg = make_pkg(base)
    (pkg / "indexes").mkdir()
    (pkg / CHECKSUMS_REL).write_text("这一行没有双空格分隔也没有sha256前缀\n", encoding="utf-8")
    result, root = load_osca(pkg)
    assert not result.ok and root is None
    assert any("清单" in line and "格式非法" in line for line in result.lines)


def test_load_binary_checksums_stable_failure(make_pkg, base):
    pkg = make_pkg(base)
    (pkg / "indexes").mkdir()
    (pkg / CHECKSUMS_REL).write_bytes(b"\xff\xfe\x00garbage")
    result, root = load_osca(pkg)
    assert not result.ok and root is None
    assert any("清单读取失败" in line for line in result.lines)


# ── zip 夹带缓存（P2）：未列入 checksum 的 indexes/.git 成员一律不解压 ──


def test_load_zip_ignores_smuggled_cache_members(make_pkg, base, tmp_path):
    zip_path = _packed(make_pkg, base, tmp_path)
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("indexes/replay-health.json", '{"green": 999, "red": 0}')  # 伪健康档案
        zf.writestr("indexes/vectors.bin", "fake-vector-cache")
        zf.writestr(".git/hooks/evil", "#!/bin/sh\n")
    dest = tmp_path / "deploy"
    result, root = load_osca(zip_path, dest=dest)
    assert result.ok, result.render("load")
    assert not (root / "indexes" / "replay-health.json").exists()  # 未经校验的健康档案不落地
    assert not (root / "indexes" / "vectors.bin").exists()
    assert not (root / ".git").exists()
    assert (root / "indexes" / "judgments.index.yaml").is_file()  # 受支持缓存按已校验内容重建
