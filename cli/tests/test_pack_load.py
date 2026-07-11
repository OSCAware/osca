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
    assert any("binding 比对通过" in line for line in result.lines)


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
