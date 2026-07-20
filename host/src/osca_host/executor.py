"""真实执行器适配器（W6-3）：按 endpoint scheme 分派，跑真实取数/写路径。

契约（SPEC B.3/B.4）：
- **只读强制**（sql_readonly）：靠**连接模式**（sqlite `mode=ro` / 生产只读角色），**非关键字黑名单**——
  黑名单脆弱可绕，不采。写连接器不走 sql_readonly（写走写执行器 + 审批门，B.4）。
- **SQL 不由模型生成**：sql_readonly 跑**包内固化 impl SQL**（公理 A6，模型只按名调用），params 作
  **参数化命名绑定**（防注入）。impl 缺失即报错（OSCA024）。
- **egress**：真实执行器发起外呼前须过 Policy egress 白名单——**已在 connector `_execute_real` 分派前置**，
  本模块不重复（openapi 参考适配器额外**不跟随重定向**，防 SSRF 绕 egress）。
- **secret 三不**：secret 值由 connector 解析后传入，**只在建连接/带鉴权时活着**——绝不进回执/日志/剧集；
  本模块的 error 串一律**不带异常内文**（异常消息/栈可能含连接串或 token）。

**立身口径（诚实标注）：** 内置参考适配器（sqlite ro / urllib openapi）测 **fake 后端**（内存/本地 sqlite 文件、
本地 http.server）；生产 postgres/mysql 只读角色驱动、生产 API 网关驱动由**部署侧**按 `Executor` 协议注入。
本模块**不假装已对生产系统验证过**——真系统连通与写落地属部署验收（1.1/部署侧）。
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

_MAX_BODY = 16 << 20  # openapi 响应体读上限 16 MiB——巨响应体不触发 OOM（DoS 面 + 守 call() 恒回 Receipt）


def _split_endpoint(endpoint: str) -> tuple[str, str, str]:
    """endpoint `scheme://host[/path]` → (scheme, host, path)。**不用 urllib.parse**——URI 规范禁止 scheme
    含下划线，urlparse 对 `sql_readonly://…` 会静默把整串当 path（host/path 全落空）。手工切保稳。"""
    scheme, sep, rest = endpoint.partition("://")
    if not sep:
        return "", "", endpoint
    idx = rest.find("/")
    return (scheme, rest, "") if idx == -1 else (scheme, rest[:idx], rest[idx:])


class Executor(Protocol):
    """真实执行器协议（可插拔）。secret 是 connector 解析出的凭据值（或 None）——只用于建连接/鉴权，
    实现**绝不**把它放进回执 payload 或 error 串。返回 (payload, error)：error 非空即失败。"""

    def execute(
        self,
        *,
        endpoint: str,
        interface: dict,
        params: object,
        secret: str | None,
        is_write: bool,
        pack_root: Path,
    ) -> tuple[object, str | None]: ...


class SqlReadonlyExecutor:
    """sql_readonly 参考适配器（sqlite）：只读连接（`mode=ro`）跑包内固化 impl SQL，params 作参数化命名绑定。

    生产 postgres/mysql 只读角色驱动由部署侧按 `Executor` 协议注入（用 secret 建只读连接）。参考适配器读
    本地 sqlite 文件（endpoint 的 path 部分），本地无鉴权、不用 secret。只读强制靠 `mode=ro` 连接——
    任何写 SQL 被 sqlite 天然拒（不靠黑名单）。"""

    def execute(self, *, endpoint, interface, params, secret, is_write, pack_root):
        if is_write:
            # 写连接器不走 sql_readonly（只读契约）——写走写执行器 + 审批门（B.4）
            return None, "sql_readonly 执行器只读——写路径不走只读执行器（写走写执行器 + 审批门，契约 B.4）"
        impl = interface.get("impl")
        if not isinstance(impl, str) or not impl:
            return None, "sql_readonly 接口缺 impl 固化查询（OSCA024）——不接受模型即席 SQL（公理 A6）"
        sql_path = pack_root / impl
        if not sql_path.is_file():
            return None, f"impl SQL 缺失：{impl}（OSCA024，声明即必须存在）"
        try:
            sql = sql_path.read_text(encoding="utf-8")
        except OSError:
            return None, f"impl SQL 读取失败：{impl}"
        db_path = _split_endpoint(endpoint)[2]  # 参考适配器：endpoint path = sqlite 文件；生产走网络连接串 + secret
        if not db_path:
            return None, "sql_readonly endpoint 缺 sqlite 文件路径（参考适配器；生产 DB 走部署侧注入驱动）"
        # 命名绑定：dict → 缺失的命名参数默认 None（可选参数省略即 NULL）；非 dict → 全 None（无注入面）
        bind = defaultdict(lambda: None, params) if isinstance(params, dict) else defaultdict(lambda: None)
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读连接（连接模式强制，非关键字过滤）
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(sql, bind).fetchall()]  # 参数化绑定（防注入）
            return rows, None
        except (sqlite3.Error, sqlite3.Warning) as e:
            # 只读强制靠 mode=ro：写 SQL/写连接一律 OperationalError；多语句 impl 触发 sqlite3.Warning（Error 的兄弟，
            # 须一并捕获）。只带异常**类型名**、不带内文，守「不带异常内文」纪律（connector 分派处另有兜底 guard）。
            return None, f"sql_readonly 执行失败（{type(e).__name__}）——只读连接（mode=ro）；单语句固化查询"
        finally:
            if conn is not None:
                conn.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向——防服务器 302 到内网/未授权 host 绕过 egress 白名单（SSRF 面）。3xx 作非 2xx 处理。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class OpenapiExecutor:
    """openapi 参考适配器（urllib，无三方依赖）：method + path + params 从接口 manifest 取，secret 作
    `Authorization: Bearer` 头。参考适配器按 endpoint scheme 走 http（openapi://）/ https（https://）；
    生产 API 网关驱动由部署侧注入。egress 已在 connector 分派前置；本适配器额外不跟随重定向（防 SSRF）。"""

    def execute(self, *, endpoint, interface, params, secret, is_write, pack_root):
        method = interface.get("method")
        if not isinstance(method, str) or not method:
            method = "POST" if is_write else "GET"  # 未声明 method：写默认 POST，读默认 GET
        method = method.upper()
        ep_scheme, netloc, _ = _split_endpoint(endpoint)
        scheme = "https" if ep_scheme == "https" else "http"  # openapi:// 参考适配器映射 http；https:// 直用
        # path **强制以 / 开头**——否则 manifest path（如 ".evil.com/x" / "evil/x"）会向右延展 netloc、把真实连接
        # host 引到 egress 从未校验的主机、并把 secret Bearer 送过去（对抗审查 blocker）。锚定后 path 不注入 authority。
        raw_path = interface.get("path")
        path = "/" + (raw_path if isinstance(raw_path, str) else "").lstrip("/")
        url = f"{scheme}://{netloc}{path}"
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"  # 值只在请求头（发给预期接收方），绝不回执/日志
        data = None
        if method == "GET":
            if isinstance(params, dict) and params:
                url = f"{url}?{urlencode(params)}"
        else:
            body = params if isinstance(params, dict | list) else {}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=10) as resp:
                # read(size) 读上限：巨响应体不触发 OOM（DoS + call() 恒回 Receipt）。注意带 size 参数**不**会对截断响应
                # 抛 IncompleteRead，故截断由下方 Content-Length 比对显式 fail-closed（不静默把半截数据当取数结果）。
                raw, status, declared = resp.read(_MAX_BODY + 1), resp.status, resp.getheader("Content-Length")
        except urllib.error.HTTPError as e:
            return None, f"openapi {method} 非 2xx：HTTP {e.code}"  # 只带状态码，不带响应体（可能含数据）
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            # 不带异常内文——URLError 消息可能含 URL；连接层错误（含畸形响应 HTTPException）统一按调用失败 fail-closed
            return None, f"openapi {method} 调用失败（连接层错误）"
        if len(raw) > _MAX_BODY:
            return None, f"openapi {method} 响应体超限（>{_MAX_BODY}B）——fail-closed"
        if declared is not None and declared.isdigit() and int(declared) != len(raw):
            # 截断/不完整响应——不把半截数据当取数结果（取数不完整即失败，不编造，公理 A6）
            return None, f"openapi {method} 响应截断（Content-Length 不符）——fail-closed"
        if not (200 <= status < 300):
            return None, f"openapi {method} 非 2xx：HTTP {status}"
        if not raw:
            return None, None
        try:
            return json.loads(raw), None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, f"openapi {method} 响应非 JSON（HTTP {status}）"


def default_executors() -> dict[str, Executor]:
    """内置参考适配器注册表（scheme → 执行器）。生产驱动（postgres/mysql/生产网关）由部署侧按 `Executor`
    协议注入覆盖；未注册的 scheme 由 connector fail-closed。mcp 刻意不注册（W6 预留不实现）。"""
    openapi = OpenapiExecutor()
    return {"sql_readonly": SqlReadonlyExecutor(), "openapi": openapi, "https": openapi}
