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

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_URL = "OSCA_LLM_URL"
ENV_MODEL = "OSCA_LLM_MODEL"
ENV_KEY = "OSCA_LLM_API_KEY"

TIMEOUT_SECONDS = 120


class LLMError(Exception):
    """配置缺失、网关失败、固件缺失——统一人话报错。"""


@dataclass
class LLMReply:
    text: str
    tokens: int  # 网关回报的 total_tokens；mock 与缺报时按字符数/4 估算
    model: str


def _estimate_tokens(*texts: str) -> int:
    return max(1, sum(len(t) for t in texts) // 4)


class OpenAICompatLLM:
    """OpenAI-compatible 网关客户端（stdlib urllib，零新依赖）。"""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def complete(self, system: str, user: str, *, tag: str) -> LLMReply:
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
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 —— 网关地址来自部署环境
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise LLMError(f"LLM 网关调用失败（{tag}）：{e}") from e
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 网关响应不是 chat/completions 形状（{tag}）：{payload}") from e
        # 用量自报是不可信输入（预算硬顶的记账源）：负数会冲减已用额度、非整数会炸记账——
        # 缺失/0/负数/bool/非整数一律回落字符估算，绝不把非法上报放进硬预算（GPT Review P1 预算绕过）
        usage = payload.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            tokens = _estimate_tokens(system, user, text)
        return LLMReply(text=text, tokens=tokens, model=str(payload.get("model", self.model)))


class MockLLM:
    """mock 固件执行器：按 tag 读 <目录>/<tag>.md，缺失即报错（不猜，同 Connector mock）。"""

    def __init__(self, fixture_dir: Path):
        self.fixture_dir = fixture_dir
        self.model = "mock"
        self.calls: list[str] = []  # 调用过的 tag，测试断言用

    def complete(self, system: str, user: str, *, tag: str) -> LLMReply:
        self.calls.append(tag)
        # tag 含包内声明的步骤名等成分（不可信输入）：`../` 会把固件读引出固件目录——resolve 后
        # 强制留在目录内（合法 tag 本就带子目录分层，约束按目录包含而非禁分隔符）
        base = self.fixture_dir.resolve()
        fixture = (base / f"{tag}.md").resolve()
        if not fixture.is_relative_to(base):
            raise LLMError(f"mock LLM 固件路径越界：{tag}——tag 不得把固件读引出固件目录")
        if not fixture.is_file():
            raise LLMError(f"mock LLM 固件缺失：{fixture}")
        text = fixture.read_text(encoding="utf-8")
        return LLMReply(text=text, tokens=_estimate_tokens(system, user, text), model="mock")


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
