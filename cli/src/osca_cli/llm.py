"""LLM 通道 —— 剧集与回放调用模型的唯一入口（认知平面的执行器）。

开源仓只放抽象接口 + 环境变量配置，不锁定厂商：
- OSCA_LLM_URL      OpenAI-compatible 网关地址（如 https://…/v1），或 mock://<固件目录>
- OSCA_LLM_MODEL    模型名（真实网关必填；mock 不需要）
- OSCA_LLM_API_KEY  网关密钥——由部署环境注入，永不进包（与 binding 同一纪律）

线协议取 OpenAI-compatible chat/completions：事实上的网关通用标准，
DashScope / OpenRouter / LiteLLM / vLLM / Ollama 皆可直连，换厂商只换环境变量。
mock 执行器与 Connector 代理的 mock 同一手法：按调用 tag 读 <目录>/<tag>.md 固件，
缺失即报错不猜——测试与全链路演练不联网。

温度恒为 0：参考实现把可复现性置于创造性之上（回放判据依赖它）。
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from osca_cli.package import resolve_in_root

ENV_URL = "OSCA_LLM_URL"
ENV_MODEL = "OSCA_LLM_MODEL"
ENV_KEY = "OSCA_LLM_API_KEY"

TIMEOUT_SECONDS = 120
MAX_RESPONSE_BYTES = 16 << 20  # 响应体读上限 16 MiB——巨响应体不触发 OOM（与 Host openapi 执行器同顶）

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}  # API key 走明文 http 仅限本地回环（开发面）


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向——Authorization 头绝不许被 3xx 带去别的 origin。网关地址是显式部署配置，
    重定向即异常；3xx 按 HTTPError 走统一失败路径。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class LLMError(Exception):
    """配置缺失、网关失败、固件缺失——统一人话报错。"""


@dataclass
class LLMReply:
    text: str
    tokens: int  # 网关回报的 total_tokens；mock 与缺报时按字符数/4 估算
    model: str


def estimate_tokens(*texts: str) -> int:
    """字符估算 tokens（恒正）。网关缺报/误报时的记账回落——预算硬顶的强制点绝不收非法上报；
    Host runner 对可插拔 LLM 的非法上报同口径复用（单一真理源）。"""
    return max(1, sum(len(t) for t in texts) // 4)


class OpenAICompatLLM:
    """OpenAI-compatible 网关客户端（stdlib urllib，零新依赖）。"""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def complete(self, system: str, user: str, *, tag: str, timeout: float | None = None) -> LLMReply:
        """timeout：调用方剩余时间预算（秒）——剧集 max_minutes 只剩数秒时不许再吊 120s 外呼
        （GPT Review：时间预算须传导为单次调用硬顶）。缺省/超默认 → 用 TIMEOUT_SECONDS。"""
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # API key 强制 HTTPS（P1）：明文 http 会把 Bearer 凭据裸奔外发——仅本地回环豁免（开发面）。
            split = urllib.parse.urlsplit(self.base_url)
            host = (split.hostname or "").lower()
            if split.scheme != "https" and host not in _LOOPBACK_HOSTS:
                raise LLMError(
                    f"LLM 网关携带 API key 却走非 https（{split.scheme or '无 scheme'}://{host or '?'}）——"
                    f"拒绝发起：凭据明文外发风险；本地开发仅允许回环地址走 http（{tag}）"
                )
            headers["Authorization"] = f"Bearer {self.api_key}"
        effective = TIMEOUT_SECONDS if timeout is None else min(TIMEOUT_SECONDS, max(0.001, timeout))
        try:
            # Request 构造也在边界内（P2）：非法 URL 在构造期就抛 ValueError（unknown url type 等）
            request = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers)
            with _OPENER.open(request, timeout=effective) as resp:  # noqa: S310 —— 网关地址来自部署环境；不跟随重定向
                raw = resp.read(MAX_RESPONSE_BYTES + 1)  # 读上限（P2）：巨响应体不吃光内存
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as e:
            # ValueError 罩非法 URL、HTTPException 罩畸形响应——不许 traceback 穿透
            raise LLMError(f"LLM 网关调用失败（{tag}）：{e}") from e
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LLMError(f"LLM 网关响应体超限（>{MAX_RESPONSE_BYTES}B，{tag}）——拒绝解析")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise LLMError(f"LLM 网关响应非 UTF-8 JSON（{tag}）：{type(e).__name__}") from e
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            # 错误只带形状摘要（P2）：完整 payload 回显会把网关返回的数据（可能含敏感内容）灌进日志
            shape = f"顶层键 {list(payload)[:8]}" if isinstance(payload, dict) else f"顶层类型 {type(payload).__name__}"
            raise LLMError(f"LLM 网关响应不是 chat/completions 形状（{tag}；{shape}，payload 不回显）") from e
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise LLMError(f"LLM 网关 content 非字符串（{tag}；类型 {type(content).__name__}）——拒绝，不猜")
        text = content
        # 用量自报是不可信输入（预算硬顶的记账源）：负数会冲减已用额度、非整数会炸记账——
        # 缺失/0/负数/bool/非整数一律回落字符估算，绝不把非法上报放进硬预算（GPT Review P1 预算绕过）
        usage = payload.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            tokens = estimate_tokens(system, user, text)
        return LLMReply(text=text, tokens=tokens, model=str(payload.get("model", self.model)))


class MockLLM:
    """mock 固件执行器：按 tag 读 <目录>/<tag>.md，缺失即报错（不猜，同 Connector mock）。"""

    def __init__(self, fixture_dir: Path):
        self.fixture_dir = fixture_dir
        self.model = "mock"
        self.calls: list[str] = []  # 调用过的 tag，测试断言用

    def complete(self, system: str, user: str, *, tag: str, timeout: float | None = None) -> LLMReply:
        self.calls.append(tag)
        # tag 含包内声明的步骤名等成分（不可信输入）：`../` 会把固件读引出固件目录——包内受限路径
        # 判据（package.resolve_in_root，与 lint/Host 同源）强制留在目录内（合法 tag 本就带子目录分层）。
        # timeout 在 mock 里无实义（本地读文件），收下参数保持 complete 契约一致。
        fixture = resolve_in_root(self.fixture_dir, f"{tag}.md")
        if fixture is None:
            raise LLMError(f"mock LLM 固件路径越界：{tag}——tag 不得把固件读引出固件目录")
        if not fixture.is_file():
            raise LLMError(f"mock LLM 固件缺失：{self.fixture_dir / f'{tag}.md'}")
        text = fixture.read_text(encoding="utf-8")
        return LLMReply(text=text, tokens=estimate_tokens(system, user, text), model="mock")


def resolve_llm(env: Mapping[str, str] | None = None) -> OpenAICompatLLM | MockLLM:
    """按环境变量解析 LLM 通道；未配置给人话报错（配置属部署环境，永不进包）。"""
    env = os.environ if env is None else env
    url = str(env.get(ENV_URL, "")).strip()
    if not url:
        raise LLMError(f"LLM 未配置：请设 {ENV_URL}（OpenAI-compatible 网关地址，或 mock://<固件目录>）")
    if url.startswith("mock://"):
        return MockLLM(Path(url.removeprefix("mock://")))
    model = str(env.get(ENV_MODEL, "")).strip()
    if not model:
        raise LLMError(f"LLM 模型未配置：请设 {ENV_MODEL}（真实网关必填；mock:// 不需要）")
    return OpenAICompatLLM(url, model, str(env.get(ENV_KEY, "")).strip())
