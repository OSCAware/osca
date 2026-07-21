"""注册表：注册 / 重复拒绝 / 包停（注销全部 watcher）。"""

from __future__ import annotations

import pytest

from osca_host.loader import load_for_host
from osca_host.registry import Registry, RegistryError


@pytest.fixture
def registry_with_pack(sample_pack):
    registry = Registry()
    _, loaded = load_for_host(sample_pack, require_bindings=False)
    registry.register(loaded)
    return registry, loaded


def test_register_creates_watcher_slots(registry_with_pack):
    registry, loaded = registry_with_pack
    slots = registry.watchers[loaded.package_id]
    assert [s.trigger_id for s in slots] == ["AW-001/T1", "AW-001/T2", "AW-001/T3"]
    assert all(s.state == "declared" for s in slots)  # W2 布防后变 armed


def test_duplicate_register_rejected(registry_with_pack, sample_pack):
    registry, _ = registry_with_pack
    _, again = load_for_host(sample_pack, require_bindings=False)
    with pytest.raises(RegistryError, match="已注册"):
        registry.register(again)


def test_unregister_is_package_stop(registry_with_pack):
    registry, loaded = registry_with_pack
    lines = registry.unregister(loaded.package_id)
    assert registry.packages == {}
    assert registry.watchers == {}  # 包停 = 全部 watcher 槽位释放
    assert any("watcher 注销 3 个" in line for line in lines)


def test_unregister_unknown_package(registry_with_pack):
    registry, _ = registry_with_pack
    with pytest.raises(RegistryError, match="未注册"):
        registry.unregister("no-such-package")


def test_status_snapshot(registry_with_pack):
    registry, loaded = registry_with_pack
    snapshot = registry.status()
    (pkg,) = snapshot["packages"]
    assert pkg["package_id"] == loaded.package_id
    assert [w["state"] for w in pkg["watchers"]] == ["declared"] * 3
