"""SecretResolver 参考实现（W6-2）：env-var 取值 + 三不纪律的第一道保证（不缓存、不记录）。"""

from __future__ import annotations

from osca_host.secret_resolver import EnvVarSecretResolver


def test_envvar_resolver_reads_env(monkeypatch):
    monkeypatch.setenv("FINANCE_DB_RO_KEY", "s3cr3t-conn-str")
    assert EnvVarSecretResolver().resolve("FINANCE_DB_RO_KEY") == "s3cr3t-conn-str"


def test_envvar_resolver_missing_returns_none(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert EnvVarSecretResolver().resolve("NOPE_KEY") is None  # 未设 → None（调用方 fail-closed）


def test_envvar_resolver_empty_value_is_none(monkeypatch):
    monkeypatch.setenv("EMPTY_KEY", "")  # 部署把变量设成空串 = 没给凭据
    assert EnvVarSecretResolver().resolve("EMPTY_KEY") is None  # 空串 fail-closed，不拿空串去建连接


def test_envvar_resolver_empty_ref_is_none():
    assert EnvVarSecretResolver().resolve("") is None  # 空名 → None，不去读 os.environ[""]
