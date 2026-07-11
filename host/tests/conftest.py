from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

SAMPLE_PACK = Path(__file__).resolve().parents[2] / "examples" / "oper-diagnosis.osca"


@pytest.fixture
def sample_pack(tmp_path) -> Path:
    """样例包的 tmp 副本——运行时会往包内写（settle 落账、索引重建），不许写回仓库。"""
    assert SAMPLE_PACK.is_dir(), f"样例包缺失：{SAMPLE_PACK}"
    root = tmp_path / SAMPLE_PACK.name
    shutil.copytree(SAMPLE_PACK, root, ignore=shutil.ignore_patterns("indexes"))
    return root


@pytest.fixture
def sock_path():
    """unix socket 路径有 ~104 字符上限（macOS），tmp_path 太深，用 /tmp 短路径。"""
    d = Path(tempfile.mkdtemp(prefix="oscah-", dir="/tmp"))
    yield d / "h.sock"
    shutil.rmtree(d, ignore_errors=True)
