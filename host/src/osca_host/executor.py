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
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlencode

from osca_cli.package import resolve_in_root

from osca_host.jsongate import MAX_JSON_DEPTH, JsonGateRejected, loads_guarded

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
    实现**绝不**把它放进回执 payload 或 error 串。返回 (payload, error)：error 非空即失败。

    timeout（可选，复核 P2）：调用方剩余时间预算（秒）——支持的实现按它收紧单次外呼上限；
    connector 分派用签名探测传参，老驱动不声明也不破约（deadline 由调用方逐接口强制）。"""

    def execute(
        self,
        *,
        endpoint: str,
        interface: dict,
        params: object,
        secret: str | None,
        is_write: bool,
        pack_root: Path,
        timeout: float | None = None,
    ) -> tuple[object, str | None]: ...


# 只读授权器（GPT 外审收口）：`mode=ro` 只护**主库**——VACUUM INTO / ATTACH DATABASE / 写 PRAGMA 仍能建新文件、
# 改 schema（已实测）。授权器把执行面收窄到 SELECT / READ / FUNCTION，其余（ATTACH/写/PRAGMA/VACUUM…）一律 DENY——
# 只读靠**授权器 + 连接模式双闸**，非关键字黑名单。授权器动作码走 sqlite3 常量（缺失回退稳定 ABI 整数）。
_RO_ALLOWED = frozenset(
    {
        getattr(sqlite3, "SQLITE_SELECT", 21),
        getattr(sqlite3, "SQLITE_READ", 20),
        getattr(sqlite3, "SQLITE_FUNCTION", 31),
        getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # 合法 WITH RECURSIVE CTE（只读，不开写；GPT 复审误拒收口）
    }
)


def _readonly_authorizer(action, _arg1, _arg2, _dbname, _source):
    return sqlite3.SQLITE_OK if action in _RO_ALLOWED else sqlite3.SQLITE_DENY


class SqlReadonlyExecutor:
    """sql_readonly 参考适配器（sqlite）：只读连接（`mode=ro` + 授权器）跑包内固化 impl SQL，params 参数化命名绑定。

    生产 postgres/mysql 只读角色驱动由部署侧按 `Executor` 协议注入（用 secret 建只读连接）。参考适配器读
    本地 sqlite 文件（endpoint 的 path 部分），本地无鉴权、不用 secret。只读强制靠 **`mode=ro` 连接 + 授权器
    双闸**——写 SQL / ATTACH / VACUUM / 写 PRAGMA 一律拒（mode=ro 单独只护主库，不够；不靠关键字黑名单）。"""

    def execute(self, *, endpoint, interface, params, secret, is_write, pack_root, timeout=None):
        if is_write:
            # 写连接器不走 sql_readonly（只读契约）——写走写执行器 + 审批门（B.4）
            return None, "sql_readonly 执行器只读——写路径不走只读执行器（写走写执行器 + 审批门，契约 B.4）"
        impl = interface.get("impl")
        if not isinstance(impl, str) or not impl:
            return None, "sql_readonly 接口缺 impl 固化查询（OSCA024）——不接受模型即席 SQL（公理 A6）"
        # impl 是包内 manifest 声明（不可信输入）：绝对路径 / `../` / 符号链接（含链接环）都能把读引出
        # 包根或炸穿执行器。判据与 lint OSCA024 **同一 helper**（resolve_in_root，GPT 三审 P2：真共用）。
        sql_path = resolve_in_root(pack_root, impl)
        if sql_path is None:
            return None, f"impl 路径越界：{impl}——包内声明只可指包内文件，拒绝（不可信输入不出包根）"
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
        # 剩余预算传导（复核 P2）：sqlite 的 connect timeout 只是**锁等待**上限——长查询本身要靠
        # progress handler 按**绝对 deadline** 中断（每 ~5000 条 VM 指令检查一次，超时回非零即中止,
        # 触发 OperationalError → fail-closed 错误回执）。
        busy_timeout = 5.0 if timeout is None else max(0.001, min(5.0, timeout))
        deadline = None if timeout is None else time.monotonic() + max(0.001, timeout)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=busy_timeout)  # 只读连接
            if deadline is not None:
                conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 5000)
            conn.set_authorizer(_readonly_authorizer)  # 第二闸：拒 ATTACH/VACUUM/写 PRAGMA（mode=ro 只护主库不够）
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

_READ_METHODS = frozenset({"GET", "HEAD"})  # 读路径只允许这些 HTTP method（其余属写，须过审批门）
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})  # 本地回环——secret 走明文 http 仅限于此（参考适配器测试）


def _host_only(netloc: str) -> str:
    """netloc → host（去 port / IPv6 括号；userinfo 已在 connector 拒，此处无 @）。用于回环判定。"""
    if netloc.startswith("["):  # IPv6：[::1] 或 [::1]:port
        return netloc[1 : netloc.index("]")] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def _anchor_path(raw: str) -> str:
    """单段 path **锚定闸**：非空段一律强制以单个 `/` 开头（多余前导 `/` 塌成一个），空段回 ""。

    这是 authority 注入闸（对抗审查 blocker）：一段 path 若是 `.evil.com/x` / `evil/x`，直接贴在 netloc
    右边会**向右延展主机名**——连接被引到 egress 从未校验的主机、secret Bearer 顺手送过去；而前导 `//`
    会让 `//evil.com/x` 被当成 protocol-relative authority（同样换主机）。锚定后这段只能是路径，注入不了 authority。

    **endpoint 的 path 段与 interface 的 path 段都过这道闸**：闸不认来源、只认形状。endpoint 段虽由
    `_split_endpoint` 切出来时就带前导 `/`，仍走同一函数——不给「这段来自部署侧所以可信」留隐含前提，
    也不让日后换切法时悄悄漏掉一段。
    """
    return "/" + raw.lstrip("/") if raw else ""


def _seg_normalizations(seg: str) -> set[str]:
    """一段 path 在**各家后端**手里可能被归一成什么——判据只问「有没有一种归一让这段等于 `..`」。

    写成通用形式（枚举归一，不是一个变体打一个补丁）：每多一种「后端在比对路径前会先抹掉某种噪声」的
    事实，就在这里加一条归一规则，`_has_traversal` 与它的调用方都不用动。当前三条：

    - **`;` path-parameter（RFC 3986 §3.3）**：Tomcat/Jetty/Spring 一类在**归一化之前**剥掉 `;param`，
      `..;` / `..;jsessionid=x` 剥完就变回 `..`（编码形 `%3b` 已在解码那步还原成 `;`）。
    - **NUL**：按 C 字符串语义**截断**的后端把 `..%00x` 读成 `..`；也有实现是直接**抹除**控制字符，
      那 `.%00.` 会变回 `..`。两种读法都算进来（宁可多拒）。
    - **首尾空白**：Windows 文件语义吃掉尾随空格，`..%20` / `%09..` 落到那类后端就是 `..`。

    考虑过但**不**收进来的：`...`（三点及以上纯点段）——RFC 与各家 HTTP 路由都不把它当上跳，只有
    Win32 文件层吃尾随点，而它吃掉的是**全部**尾随点（`...` → 空，不是 `..`）；收进来纯属误伤。
    `%c0%ae` 一类 overlong UTF-8（远古 IIS 会解成 `.`）也不收：Python 的 unquote 按 UTF-8 解得到替换字符，
    要覆盖得另铺一套字节层解码，而本适配器打的是声明为 http/https 的数据台，不是 IIS 4/5。
    """
    cands = {seg, seg.split(";", 1)[0]}  # `;` 及其后的 path-parameter：剥 / 不剥
    cands |= {c.split("\x00", 1)[0] for c in cands}  # NUL 截断（C 字符串语义）
    cands |= {c.replace("\x00", "") for c in cands}  # NUL 抹除（另一种实现）
    cands |= {c.strip() for c in cands}  # 首尾空白被吃掉
    return cands


def _has_traversal(path: str) -> bool:
    """path 里有没有上跳段——判据是「**段规范化后等于 `..`**」，不是逐字等于 `..`。

    三步：① 反复百分号解码到不动点（挡 `%2e%2e` / `%2E%2E` / `.%2e` / `..%2f` / `..%5c`，以及
    `%252e%252e` 这类双重编码——带 decode 的代理/框架会把它还原成 `..`）；② 把 `\\` 也当分隔符
    （部分服务器/代理按 Windows 语义等同 `/`）；③ 逐段过 `_seg_normalizations`（`;` path-parameter、
    NUL 截断/抹除、首尾空白），任一归一形等于 `..` 即判上跳。

    判据故意比「某台服务器实际怎么解析」更宽：宁可多拒一条畸形路径，不可漏放一次上跳。
    已覆盖变体（每条都有参数化测试）：`..`、`%2e%2e`、`%2E%2E`、`.%2e`、`%252e%252e`、`..\\`、
    `..%2f`、`..%5c`、`..;`、`..;jsessionid=x`、`..%3b`、`..%00`、`..%00x`、`.%00.`、`..%2500`、
    `..%20`、`%09..`，以及它们的叠用形。

    **只判、不归一**：归一后放行 = 替不可信输入把上跳兑现掉，攻击者只要找到一台归一方式与我们不同的
    后端，缺口就重开；fail-closed 才收敛。
    """
    probe = path
    for _ in range(3):  # 有界解码到不动点：够覆盖单/双重编码，又不给畸形串留无限循环
        nxt = unquote(probe)
        if nxt == probe:
            break
        probe = nxt
    return any(".." in _seg_normalizations(seg) for seg in probe.replace("\\", "/").split("/"))


class OpenapiExecutor:
    """openapi 参考适配器（urllib，无三方依赖）：method + path + params 从接口 manifest 取，secret 作
    `Authorization: Bearer` 头。参考适配器按 endpoint scheme 走 http（openapi://）/ https（https://）；
    生产 API 网关驱动由部署侧注入。egress 已在 connector 分派前置；本适配器额外不跟随重定向（防 SSRF）。

    **URL path = endpoint 的 path 段（部署侧挂载前缀）+ interface 的 path 段（包内相对路由）**：
    `openapi://<主机>/datastore/<公司>` + 接口 `path: /booking` → `/datastore/<公司>/booking`。
    endpoint 不带 path 时（既有形态）URL 与老行为逐字一致。两段都过锚定闸（`_anchor_path`），
    缝上归一斜杠，拼完含上跳段（`..`）即 fail-closed（`_has_traversal`）。"""

    def execute(self, *, endpoint, interface, params, secret, is_write, pack_root, timeout=None):
        method = interface.get("method")
        if not isinstance(method, str) or not method:
            method = "POST" if is_write else "GET"  # 未声明 method：写默认 POST，读默认 GET
        method = method.upper()
        # method 与写权限一致性（GPT 外审 blocker 收口）：读连接器（is_write=False，无审批门）**不得**用写 method——
        # 否则 `write: forbidden` + `method: POST/DELETE` 绕过审批门真实写。写须走写连接器 + 审批门（B.4）。
        if not is_write and method not in _READ_METHODS:
            return (
                None,
                f"读路径（write: forbidden）不得用写 method {method}——绕过审批门；写须走写连接器 + 审批门（B.4）",
            )
        ep_scheme, netloc, ep_path = _split_endpoint(endpoint)
        scheme = "https" if ep_scheme == "https" else "http"  # openapi:// 参考适配器映射 http；https:// 直用
        # 携带 secret 却非 https 且非本地回环（GPT 外审收口）→ fail-closed：明文外发凭据风险，生产用 https://。
        if secret and scheme != "https" and _host_only(netloc) not in _LOOPBACK:
            return (
                None,
                "openapi 携带 secret 却走非 https（且非本地回环）——fail-closed：凭据明文外发风险，生产须 https://",
            )
        # URL 的 path = **endpoint 的 path 段（部署侧挂载前缀）** + **interface 的 path 段（包内相对路由）**。
        # 分工是刻意的：主机、公司代号、挂载点只活在部署侧 endpoint 真值里（本来就该在那），包只声明自己
        # 表内的相对段（如 `/booking`）——包与登记表都不带 host / 公司代号 / 凭据。
        # 两段各自过 `_anchor_path` 的 authority 闸（见该函数），再在**缝上**归一斜杠。
        raw_path = interface.get("path")
        itf_raw = raw_path if isinstance(raw_path, str) else ""
        ep_seg, itf_seg = _anchor_path(ep_path), _anchor_path(itf_raw)
        if ep_seg and itf_seg:
            # 缝上恰好一个 `/`：左段去尾斜杠、右段已锚定以 `/` 开头。不许拼出 `//`——某些服务器把 `//x`
            # 当另一个资源（前缀路由/鉴权中间件按段匹配时尤其危险），路径开头的 `//` 更会被读成 authority。
            path = ep_seg.rstrip("/") + itf_seg
        else:
            # 空段塌缩：只有一段就用那段；两段皆空 → `/`（与老形状「endpoint 无 path + 接口无 path」逐字一致）
            path = ep_seg or itf_seg or "/"
        if _has_traversal(path):
            # interface.path 来自**包**（不可信输入），endpoint 的 path 段来自部署侧。拼接一旦允许上跳，
            # 包就能从自己的挂载前缀（如 /datastore/<公司>）跳到同一台服务上别的 API——越权、串到别家公司的
            # 表，而 egress 白名单只看主机、根本拦不住。故 **fail-closed 拒绝调用**，不做归一化后放行。
            # error 只回显包内声明的接口 path（包已在手），不回显部署侧前缀（挂载点属部署侧真值，不进剧集）。
            return (
                None,
                f"openapi 拼接后 path 含上跳段（..）——fail-closed：包不得跳出部署侧挂载前缀"
                f"（接口 path：{itf_raw}；此段无异常则查部署侧 endpoint 的 path 段）",
            )
        url = f"{scheme}://{netloc}{path}"
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"  # 值只在请求头（发给预期接收方），绝不回执/日志
        data = None
        if method == "GET":
            if isinstance(params, dict) and params:
                url = f"{url}?{urlencode(params)}"
        else:
            # 审批过什么就发什么（P1）：原始 JSON 值原样上 wire——标量（str/num/bool/null）不得静默
            # 改写成 {}，否则「审批展示、摘要、实际落地内容一致」被击穿。非 JSON 可序列化在审批门已
            # fail-closed 挡下；此处兜底显式拒绝，绝不静默改写被批内容。
            try:
                data = json.dumps(params, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                return None, f"openapi {method} 写 params 非 JSON 可序列化——fail-closed（不静默改写被批内容）"
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        # 总 deadline（复核 P2）：socket timeout 只钳**单次阻塞操作**——慢滴漏响应可把总时长拖到
        # 任意长。故双闸：单次 op 上限 = min(默认 10s, 剩余预算)，且响应体按 read1 分块读、块间按
        # **绝对 deadline** 复核（慢滴漏在 deadline 处被截停，fail-closed）。
        per_op = 10.0 if timeout is None else max(0.001, min(10.0, timeout))
        deadline = None if timeout is None else time.monotonic() + max(0.001, timeout)
        try:
            with _OPENER.open(req, timeout=per_op) as resp:
                status, declared = resp.status, resp.getheader("Content-Length")
                # 底层 socket（CPython：HTTPResponse.fp 是 socket makefile 的 BufferedReader，
                # raw 是 SocketIO）——每次 read 前把 socket timeout 收紧到 remaining（四轮复核 P2）：
                # 只在块间查 deadline 时，deadline 前启动的一次 read 仍可吊满旧 per-op timeout。
                sock = getattr(getattr(resp, "fp", None), "raw", None)
                sock = getattr(sock, "_sock", None)
                if deadline is not None and sock is None:
                    # fail-closed（六项复核 P2）：拿不到底层连接就无法实施绝对 deadline——声明了
                    # max_minutes 时**拒绝无界读**，不静默退回「单次 op timeout 可越过 deadline」
                    return None, f"openapi {method} 无法获取底层连接以实施绝对 deadline——fail-closed（不做无界读）"
                # 分块读 + 读上限：巨响应体不触发 OOM（DoS + call() 恒回 Receipt）。截断不在此判——
                # 由下方 Content-Length 比对显式 fail-closed（不静默把半截数据当取数结果）。
                chunks: list[bytes] = []
                got = 0
                while got <= _MAX_BODY:
                    is_closed = getattr(resp, "isclosed", None)
                    if is_closed is not None and is_closed():
                        # 响应已读尽并关闭（3.12 起读满 Content-Length 即关连接）——收束，
                        # 不再对已关 socket 设超时/发起下一次 read（误判成 fail-closed）
                        break
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return None, f"openapi {method} 总 deadline 用尽（响应读取中止）——fail-closed"
                        try:
                            sock.settimeout(max(0.001, min(per_op, remaining)))
                        except OSError:
                            # 收不紧读超时同样 fail-closed（六项复核 P2）——不许静默降级成旧 per-op timeout
                            return None, f"openapi {method} 无法把读超时收紧到剩余预算——fail-closed"
                    chunk = resp.read1(min(65536, _MAX_BODY + 1 - got))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
                raw = b"".join(chunks)
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
            # 后端响应体过**同一把 JSON 闸**（osca_host.jsongate，与 runner 解析模型产出共用一份实现）：
            # NaN/Infinity、溢出成 ±inf 的数值、重复键、深嵌套——从这个进口进来的，落点与从模型那个进口
            # 进来的一字不差（**读回执是下游写 body 的原料**，还要进台账、上审批卡、进 L2 挂起快照）。
            # 同一类漏网口不留第二个进口；闸只有一份实现，复制一份即是给漂移开门。
            return loads_guarded(raw, max_depth=MAX_JSON_DEPTH), None
        except JsonGateRejected as e:
            # 定点拒绝：说清是哪把闸拦的（人看得懂才查得动后端）。理由取自**响应体内容**（重复的那个键名、
            # 溢出的那个数值），不是驱动异常的内文——「不带异常内文」防的是连接串/凭据从异常消息漏进回执，
            # 而反射型 API 把 token 回显进响应体的情形另有 connector 层 `_scrub_secret` 兜底（回执与 error 同抹）。
            return None, f"openapi {method} 响应体过不了 JSON 闸（HTTP {status}）：{e}"
        except (ValueError, RecursionError):
            # JSONDecodeError / UnicodeDecodeError 都是 ValueError；深到把解析器自己炸栈时是 RecursionError
            # ——它**不是** ValueError，漏掉即炸穿 execute（退到 connector 的兜底 except，报错退成笼统版）。
            return None, f"openapi {method} 响应非 JSON（HTTP {status}）"


def default_executors() -> dict[str, Executor]:
    """内置参考适配器注册表（scheme → 执行器）。生产驱动（postgres/mysql/生产网关）由部署侧按 `Executor`
    协议注入覆盖；未注册的 scheme 由 connector fail-closed。mcp 刻意不注册（W6 预留不实现）。"""
    openapi = OpenapiExecutor()
    return {"sql_readonly": SqlReadonlyExecutor(), "openapi": openapi, "https": openapi}
