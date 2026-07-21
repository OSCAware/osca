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


def _opener(fn):
    """把 (request, timeout) 形状的 fake 包装成 opener——生产代码经 _OPENER.open 发请求（不跟随重定向）。"""

    class _O:
        def open(self, request, timeout=None):
            return fn(request, timeout)

    return _O()


def test_openai_compat_request_and_parse(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {"choices": [{"message": {"content": "回答"}}], "usage": {"total_tokens": 42}, "model": "gw-model"}
        )

    monkeypatch.setattr(llm_mod, "_OPENER", _opener(fake_urlopen))
    client = OpenAICompatLLM("https://gateway.example/v1", "some-model", "secret")
    reply = client.complete("你是谁", "你好", tag="t")

    assert seen["url"] == "https://gateway.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["model"] == "some-model"
    assert seen["body"]["temperature"] == 0  # 可复现性优先
    assert reply.text == "回答" and reply.tokens == 42 and reply.model == "gw-model"


def test_openai_compat_bad_shape(monkeypatch):
    monkeypatch.setattr(llm_mod, "_OPENER", _opener(lambda r, timeout: _FakeResponse({"error": "x"})))
    with pytest.raises(LLMError, match="不是 chat/completions 形状"):
        OpenAICompatLLM("https://g/v1", "m").complete("s", "u", tag="t")


def test_openai_compat_illegal_usage_report_falls_back_to_estimate(monkeypatch):
    """GPT Review P1 预算绕过：网关自报 total_tokens 是不可信输入（预算硬顶的记账源）——
    负数会冲减已用额度、非整数会炸记账；0/负数/bool/非整数/形状错乱一律回落字符估算（恒正）。"""
    for bad_usage in (
        {"total_tokens": -500},
        {"total_tokens": 0},
        {"total_tokens": "42"},
        {"total_tokens": True},
        {"total_tokens": 3.5},
        {},
        ["usage 形状错乱"],
        None,
    ):

        def fake(r, timeout, u=bad_usage):
            return _FakeResponse({"choices": [{"message": {"content": "回答"}}], "usage": u})

        monkeypatch.setattr(llm_mod, "_OPENER", _opener(fake))
        reply = OpenAICompatLLM("https://g/v1", "m").complete("s", "u", tag="t")
        assert isinstance(reply.tokens, int) and reply.tokens > 0  # 非法上报不进记账，估算兜底


def test_mock_tag_path_escape_rejected(tmp_path):
    """GPT Review 路径越界同口径：tag 含包内声明成分（步骤名）——`../` 把固件读引出固件目录 → 拒绝；
    合法 tag 本就带子目录（episode/成文），约束按目录包含而非禁分隔符。"""
    fixture_dir = tmp_path / "fx"
    fixture_dir.mkdir()
    (tmp_path / "leak.md").write_text("包外内容", encoding="utf-8")
    with pytest.raises(LLMError, match="越界"):
        MockLLM(fixture_dir).complete("s", "u", tag="../leak")


def test_openai_compat_deadline_bounds_urlopen_timeout(monkeypatch):
    """GPT Review 复审 P2：调用方剩余时间预算（timeout 参数）须传导为 urlopen 超时——
    只剩 3s 不许再吊默认 120s；缺省仍用 TIMEOUT_SECONDS，超默认取默认。"""
    seen = {}

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        return _FakeResponse({"choices": [{"message": {"content": "回答"}}], "usage": {"total_tokens": 7}})

    monkeypatch.setattr(llm_mod, "_OPENER", _opener(fake_urlopen))
    client = OpenAICompatLLM("https://g/v1", "m")
    client.complete("s", "u", tag="t", timeout=3.0)
    assert seen["timeout"] == 3.0  # 剩余预算生效
    client.complete("s", "u", tag="t")
    assert seen["timeout"] == llm_mod.TIMEOUT_SECONDS  # 缺省默认
    client.complete("s", "u", tag="t", timeout=999.0)
    assert seen["timeout"] == llm_mod.TIMEOUT_SECONDS  # 超默认取默认（不放大）


def test_mock_symlink_loop_raises_llm_error_not_runtime(tmp_path):
    """GPT 三审 P2：固件目录内符号链接环——resolve_in_root 收敛为 LLMError（越界/缺失），
    不许 RuntimeError 穿透成非 LLMError。"""
    fixture_dir = tmp_path / "fx"
    fixture_dir.mkdir()
    loop = fixture_dir / "loop.md"
    loop.symlink_to(loop.name)  # 自指链接环
    with pytest.raises(LLMError):
        MockLLM(fixture_dir).complete("s", "u", tag="loop")


# ── API key 传输安全（P1）：明文 http 拒发、重定向不跟随 ──


def test_api_key_over_plain_http_refused():
    """携带 API key 却走非 https 非回环 → 发起前拒绝（凭据明文外发风险）,不产生任何网络流量。"""
    client = OpenAICompatLLM("http://gateway.example/v1", "m", api_key="TOP-SECRET")
    with pytest.raises(LLMError, match="非 https"):
        client.complete("s", "u", tag="t")


def test_api_key_over_https_and_loopback_http_allowed(monkeypatch):
    """https 正常放行;本地回环显式豁免（开发面）——两者都应走到网络层（用 fake opener 断言到达）。"""
    sent = []

    class _Opener:
        def open(self, request, timeout=None):
            sent.append(request)
            return _FakeResponse({"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}})

    monkeypatch.setattr(llm_mod, "_OPENER", _Opener())
    for url in ("https://gateway.example/v1", "http://127.0.0.1:8080/v1", "http://localhost:8080/v1"):
        reply = OpenAICompatLLM(url, "m", api_key="KEY").complete("s", "u", tag="t")
        assert reply.text == "ok"
    assert len(sent) == 3
    assert all(req.get_header("Authorization") == "Bearer KEY" for req in sent)


def test_redirect_not_followed_with_authorization():
    """网关 302 → 不跟随（Authorization 绝不被带去别的 origin）:重定向按调用失败报错,目标端点零请求。"""
    import http.server
    import threading

    hits = {"redirect": 0, "target": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path.startswith("/target"):
                hits["target"] += 1
                self.send_response(200)
                self.end_headers()
                return
            hits["redirect"] += 1
            self.send_response(302)
            self.send_header("Location", "/target/chat/completions")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    try:
        client = OpenAICompatLLM(f"http://{host}:{port}", "m", api_key="KEY")  # 回环:允许发起
        with pytest.raises(LLMError, match="调用失败"):
            client.complete("s", "u", tag="t")
    finally:
        srv.shutdown()
        srv.server_close()
    assert hits["redirect"] == 1 and hits["target"] == 0  # 重定向未被跟随,授权头未到第二个 origin
