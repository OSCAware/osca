"""LLM 通道：环境变量解析、OpenAI-compatible 客户端、mock 固件执行器。"""

from __future__ import annotations

import json

import pytest

from osca_cli import llm as llm_mod
from osca_cli.llm import ENV_KEY, ENV_MODEL, ENV_URL, LLMError, MockLLM, OpenAICompatLLM, resolve_llm


def test_resolve_requires_url():
    with pytest.raises(LLMError, match="OSCA_LLM_URL"):
        resolve_llm(env={})


def test_resolve_real_gateway_requires_model():
    with pytest.raises(LLMError, match="OSCA_LLM_MODEL"):
        resolve_llm(env={ENV_URL: "https://gateway.example/v1"})


def test_resolve_mock_and_real(tmp_path):
    mock = resolve_llm(env={ENV_URL: f"mock://{tmp_path}"})
    assert isinstance(mock, MockLLM) and mock.fixture_dir == tmp_path

    real = resolve_llm(env={ENV_URL: "https://gateway.example/v1/", ENV_MODEL: "some-model", ENV_KEY: "k"})
    assert isinstance(real, OpenAICompatLLM)
    assert real.base_url == "https://gateway.example/v1"  # 尾斜杠归一


def test_mock_reads_fixture_by_tag(tmp_path):
    (tmp_path / "episode").mkdir()
    (tmp_path / "episode" / "成文.md").write_text("草稿正文", encoding="utf-8")
    mock = MockLLM(tmp_path)
    reply = mock.complete("system", "user", tag="episode/成文")
    assert reply.text == "草稿正文" and reply.tokens > 0 and reply.model == "mock"
    assert mock.calls == ["episode/成文"]


def test_mock_missing_fixture_explodes(tmp_path):
    with pytest.raises(LLMError, match="固件缺失"):
        MockLLM(tmp_path).complete("s", "u", tag="episode/不存在")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_openai_compat_request_and_parse(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {"choices": [{"message": {"content": "回答"}}], "usage": {"total_tokens": 42}, "model": "gw-model"}
        )

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatLLM("https://gateway.example/v1", "some-model", "secret")
    reply = client.complete("你是谁", "你好", tag="t")

    assert seen["url"] == "https://gateway.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["model"] == "some-model"
    assert seen["body"]["temperature"] == 0  # 可复现性优先
    assert reply.text == "回答" and reply.tokens == 42 and reply.model == "gw-model"


def test_openai_compat_bad_shape(monkeypatch):
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", lambda r, timeout: _FakeResponse({"error": "x"}))
    with pytest.raises(LLMError, match="不是 chat/completions 形状"):
        OpenAICompatLLM("https://g/v1", "m").complete("s", "u", tag="t")
