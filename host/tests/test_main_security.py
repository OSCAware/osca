from __future__ import annotations

import pytest

from osca_host.main import _client, _load_deployments


@pytest.mark.parametrize(
    "text",
    [
        "demo:\n  path: null\n",
        "demo:\n  path: /tmp/demo\n  bindings: null\n",
        "demo:\n  path: /tmp/demo\n  dest: null\n",
        'demo:\n  path: "bad\\npath"\n',
    ],
)
def test_deployments_reject_null_and_control_character_paths(tmp_path, text):
    path = tmp_path / "deployments.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        _load_deployments(str(path))


@pytest.mark.parametrize("text", ["[]\n", "false\n", "0\n", "null\n", '""\n'])
def test_deployments_reject_falsy_non_mapping_top_level(tmp_path, text):
    """已有清单必须真是 mapping；falsy 值不能伪装成合法空清单。"""
    path = tmp_path / "deployments.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        _load_deployments(str(path))


def test_client_rejects_lax_principal_token_file_before_connecting(tmp_path, monkeypatch):
    token_file = tmp_path / "operator.token"
    token_file.write_text("operator-owned-token", encoding="utf-8")
    token_file.chmod(0o644)
    called = False

    def forbidden_connect(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应进入连接")

    monkeypatch.setattr("osca_host.main.send_command", forbidden_connect)
    assert _client({"cmd": "status"}, tmp_path / "missing.sock", token_file) == 1
    assert not called
