"""Host 组件 6：Connector 代理 —— 确定性取数/执行（架构 §4）。

模型只能按名调用，永不写 SQL、永不猜数。代理做三件事：
1. manifest 契约校验：调用未声明的接口 = 接口漂移，当场爆炸（不猜、不兜底）；
2. binding/secret 解析：binding 由部署环境注入（永不进包）；secret 按 secret_ref 经可插拔
   SecretResolver（默认 env-var）取值交执行器，值三不（不进包/日志/剧集）、取不到即 fail-closed；
3. 调用与回执：每次调用产出一张回执（谁调的、走哪个 binding、结果如何），
   结果注入剧集前先过 Policy 脱敏。

执行器按 endpoint scheme 分派、可插拔（W6-3）。内置 mock 执行器（endpoint 以 mock:// 开头，
从目录读 <接口名>.yaml 固件，供测试与演练）+ 真实参考适配器（sql_readonly=sqlite ro / openapi=urllib，
见 executor.py，测 fake 后端）；生产驱动（postgres/mysql/生产网关）由部署侧按 Executor 协议注入。
未注册 scheme fail-closed（不猜、不兜底）；mcp 预留不实现。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from osca_cli.package import resolve_in_root

from osca_host.executor import Executor, default_executors
from osca_host.loader import LoadedPackage
from osca_host.policy import PolicyInterceptor
from osca_host.secret_resolver import EnvVarSecretResolver, SecretResolver

# scheme 允许 `_`（如 sql_readonly://）——否则含下划线的 scheme 主机名抽取落空、host=整串、egress 永远拒
ENDPOINT_HOST = re.compile(r"^[a-z+_]+://([^/:@]+@)?([A-Za-z0-9.-]+)")


def _executor_supports_timeout(executor: Executor) -> bool:
    """执行器是否接收 timeout（剩余预算传导，复核 P2）。老驱动无参不传——不炸不改契约；
    逐接口 deadline 仍由调用方（runner）强制，timeout 只是把在途单次外呼也收紧。"""
    try:
        params = inspect.signature(executor.execute).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _scrub_secret(node: object, secret: str) -> object:
    """递归把 payload/error 里出现的 secret 值抹成标记——防反射型 API（回显 Authorization/token）把凭据带进
    回执/剧集/日志。只在 connector 层（持有本次 secret 值）做。str/dict/list/tuple 全覆盖；键与值同抹。"""
    if isinstance(node, str):
        return node.replace(secret, "***secret已脱敏***") if secret in node else node
    if isinstance(node, dict):
        # 键**与值同抹**（secret 可被回显成 JSON 键）；抹后键**碰撞消歧**（GPT 复审：`TOKEN` 与已存在的标记键塌成
        # 一个会丢字段）——碰撞加稳定后缀，与 policy.redact 同口径，保序保全字段。
        out: dict = {}
        for k, v in node.items():
            rk = _scrub_secret(k, secret)
            if rk in out and isinstance(rk, str):
                i = 2
                while f"{rk}#{i}" in out:
                    i += 1
                rk = f"{rk}#{i}"
            out[rk] = _scrub_secret(v, secret)
        return out
    if isinstance(node, list):
        return [_scrub_secret(v, secret) for v in node]
    if isinstance(node, tuple):  # 可插拔执行器可能回 tuple（GPT 复审：tuple 内 secret 原漏）
        return tuple(_scrub_secret(v, secret) for v in node)
    return node


@dataclass
class Receipt:
    """一次 Connector 调用的回执。"""

    ok: bool
    interface: str
    binding_ref: str | None = None
    payload: object = None
    redacted: int = 0
    error: str | None = None
    # 写路径三态（D2a 可恢复剧集）：granted 放行落地 / pending 命中审批门待批（剧集须挂起） /
    # denied 拿不到授权（首次=配置/内容拒绝→剧集失败；恢复=驳回/过期/撤销→回落保守默认）。读回执为 None。
    disposition: str | None = None
    challenge_id: str | None = None  # pending 时的待批挑战 id（剧集挂起绑定它，恢复时按它查态兑现）
    at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))


class ConnectorProxy:
    def __init__(
        self,
        loaded: LoadedPackage,
        bindings: dict,
        policy: PolicyInterceptor,
        *,
        secret_resolver: SecretResolver | None = None,
        executors: dict[str, Executor] | None = None,
    ):
        self.package_id = loaded.package_id
        self.bindings = bindings  # 部署环境注入的 binding 表（永不进包）
        self.policy = policy
        # secret 解析器（W6-2）：默认 env-var 参考实现；部署侧可按 SecretResolver 协议注入 file/vault/callable。
        self.secret_resolver = secret_resolver if secret_resolver is not None else EnvVarSecretResolver()
        # 真实执行器注册表（W6-3，scheme → Executor）：默认内置参考适配器（sqlite ro / urllib openapi）；
        # 部署侧可按 Executor 协议注入生产驱动覆盖。未注册的 scheme 由 _execute_real fail-closed。
        self.executors = executors if executors is not None else default_executors()
        # manifest 编译：接口按「CON-xxx.名字」扁平注册；漂移在这里当场暴露
        self.interfaces: dict[str, dict] = {}
        self.connectors: dict[str, dict] = {}
        for f in loaded.pack.typed_files("connectors"):
            cid = f.mapping.get("connector_id")
            if not cid:
                continue
            self.connectors[cid] = f.mapping
            for itf in f.mapping.get("interfaces") or []:
                if isinstance(itf, dict) and itf.get("name"):
                    self.interfaces[f"{cid}.{itf['name']}"] = itf
        self.pack_root = loaded.root

    def call(
        self,
        interface_ref: str,
        params: object = "",
        *,
        step: str | None = None,
        episode_id: str | None = None,
        resume: bool = False,
        timeout: float | None = None,
    ) -> Receipt:
        """按名调用。step=None 为运行时内部调用（precondition/watch 轮询）。

        timeout：调用方剩余时间预算（秒，复核 P2）——转交支持 timeout 的执行器收紧单次外呼；
        不支持的执行器照旧（deadline 由调用方逐接口强制）。

        params：读接口的过滤参数（str）或**写接口被写内容**（结构体，D1 params 穿透）。写接口经审批门时
        以 params 的 sha256 摘要绑定挑战（防偷梁换柱）；读执行器忽略 params、也不过写审批门。

        resume=True（D2a 恢复重入挂起的写步）：写放行走 **consume-only**（只消费已批挑战、不新建），
        授权复核走 recheck_only（不重复计额度）；消费不到已批授权即 disposition=denied，由 runner 回落。
        """
        ok, reason = self.policy.authorize_tool(step, interface_ref, episode_id, recheck_only=resume)
        if not ok:
            return Receipt(ok=False, interface=interface_ref, error=reason)

        itf = self.interfaces.get(interface_ref)
        if itf is None:
            declared = ", ".join(sorted(self.interfaces)) or "（无）"
            return Receipt(
                ok=False,
                interface=interface_ref,
                error=f"接口漂移：{interface_ref} 未在 manifest 声明（已声明：{declared}）——契约校验直接爆炸，不猜",
            )

        cid = interface_ref.split(".", 1)[0]
        connector = self.connectors[cid]
        is_write = (connector.get("permissions") or {}).get("write") != "forbidden"
        if is_write:
            # 写路径的审批门对内对外一视同仁——step=None 的运行时内部调用（watch/precondition/settle）也不豁免。
            # 挑战绑本次 episode + 被写 payload（params 摘要，真实被写内容）——防跨剧集串用与偷梁换柱。
            if resume:
                # 恢复重入：只消费已批挑战、不新建（§5.2）——消费不到（过期/驳回/撤销/竞态）即 disposition=denied 回落
                ok, reason = self.policy.consume_write_approval(interface_ref, episode_id=episode_id, payload=params)
                if not ok:
                    return Receipt(ok=False, interface=interface_ref, error=reason, disposition="denied")
            else:
                # 首次命中：不在 approvals 默认拒绝；在清单则挂/复用绑定挑战供审批人裁决
                ok, reason = self.policy.require_write_approval(interface_ref, episode_id=episode_id, payload=params)
                if not ok:
                    # 微窗（fail-closed，实践几乎不可达）：raise pending 与此处 find 是两次取锁，其间若这张刚生成的
                    # challenge 恰被 approve，find 落空→判 denied→剧集失败（合法写被判败，可重跑）。审批人此刻尚不知
                    # 微秒前的 challenge_id，故不可达；彻底消窗须 require_write_approval 三元返回挑战，随后续清理。
                    ch = self.policy.find_pending_challenge(interface_ref, episode_id=episode_id, payload=params)
                    if ch is not None:  # 挂了 pending → 剧集须挂起等批（不是失败）
                        return Receipt(
                            ok=False,
                            interface=interface_ref,
                            error=reason,
                            disposition="pending",
                            challenge_id=ch.challenge_id,
                        )
                    # 无 pending：配置/内容拒绝（不在清单/空/非序列化）——硬拒绝（disposition=denied，首次即失败）
                    return Receipt(ok=False, interface=interface_ref, error=reason, disposition="denied")

        binding_ref = connector.get("binding_ref")
        binding = self.bindings.get(binding_ref) if binding_ref else None
        if not isinstance(binding, dict):
            # 装载门禁（deployment_binding_errors）应已拦下缺失/非 mapping——此处是运行时 fail-closed
            # 第二道闸（不猜、不兜底），绝不对着 list/scalar/null binding 继续取 endpoint
            return Receipt(
                ok=False,
                interface=interface_ref,
                binding_ref=binding_ref,
                error=f"部署环境未注入 binding {binding_ref} 或形状非法（须为 mapping；binding 永不进包，缺失即报错）",
            )

        endpoint = binding.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            return Receipt(
                ok=False,
                interface=interface_ref,
                binding_ref=binding_ref,
                error=f"binding {binding_ref} 缺非空 endpoint——装载门禁判据同源，运行时 fail-closed",
            )
        if endpoint.startswith("mock://"):
            # 写接口走 mock 写执行器（落地被批准的 params）；读接口走 mock 固件执行器
            payload, error = (
                self._execute_mock_write(interface_ref, params)
                if is_write
                else self._execute_mock(endpoint, interface_ref, itf)
            )
        else:
            payload, error = self._execute_real(endpoint, binding, itf, params, is_write, timeout=timeout)
        if error:
            return Receipt(ok=False, interface=interface_ref, binding_ref=binding_ref, error=error)

        payload, hits = self.policy.redact(payload)  # 注入剧集前脱敏
        return Receipt(
            ok=True,
            interface=interface_ref,
            binding_ref=binding_ref,
            payload=payload,
            redacted=hits,
            disposition="granted" if is_write else None,  # granted 只标写授权放行；读回执为 None（与字段注释一致）
        )

    # ── 执行器 ────────────────────────────────────────────────────────

    def _execute_mock(self, endpoint: str, interface_ref: str, itf: dict) -> tuple[object, str | None]:
        # 接口名来自包内 manifest（不可信输入）：带 `../`/绝对段/链接环会把固件读引出固件目录或炸穿。
        # 判据与 lint/执行器**同一 helper**（resolve_in_root，GPT 三审 P2：真共用，不手写第二份）。
        base = Path(endpoint.removeprefix("mock://"))
        fixture = resolve_in_root(base, f"{interface_ref.split('.', 1)[1]}.yaml")
        if fixture is None:
            return None, f"mock 固件路径越界：{interface_ref}——接口名不得把固件读引出固件目录，拒绝"
        if not fixture.is_file():
            return None, f"mock 固件缺失：{fixture}"
        return yaml.safe_load(fixture.read_text(encoding="utf-8")), None

    def _execute_mock_write(self, interface_ref: str, params: object) -> tuple[object, str | None]:
        """mock 写执行器（测试与全链路演练）：不落真实系统，回一张确定性 mock 写回执，回显被批准的
        被写内容（params）+ 落地标记。走通此路 = 审批闭环机制通，**非真实系统写验证**——真实写
        走真实执行器（openapi 写 / 生产 sql 写驱动，`_execute_real` 分派），本 mock 写执行器仅供演练。"""
        return {"mock_write": interface_ref, "applied": params, "landed": True}, None

    def _execute_real(
        self, endpoint: str, binding: dict, itf: dict, params: object, is_write: bool, timeout: float | None = None
    ) -> tuple[object, str | None]:
        """真实执行器分派（W6-3）：egress → secret 前置 → 按 endpoint scheme 分派执行器。

        顺序即防御纵深：egress 拒则不解析凭据、不外呼；secret 取不到则不分派执行器。secret 值解析后
        **只传给执行器**（建连接/鉴权），绝不进回执/日志/剧集。"""
        # authority 含 userinfo（@）即拒（GPT 外审 blocker 收口）：egress 正则抽 `allowed@evil` 得 allowed、urllib 实连
        # evil——校验主机 ≠ 实连主机、secret 送错家。真实 endpoint 无 userinfo（凭据走 secret_ref 不入 URL）。
        authority = endpoint.split("://", 1)[1].split("/", 1)[0] if "://" in endpoint else ""
        if "@" in authority:
            return (
                None,
                "endpoint authority 含 userinfo（@）——拒绝：egress 校验主机与实连主机不一致风险；凭据走 secret_ref",
            )
        m = ENDPOINT_HOST.match(endpoint)
        host = m.group(2) if m else endpoint
        ok, reason = self.policy.authorize_egress(host)
        if not ok:
            return None, reason
        # secret 前置（W6-2，三不：值不进包/日志/剧集）：binding **声明了** secret_ref（键存在）→ 必须是合法非空字符串、
        # 且解析出非空值，否则 fail-closed（错误只带名）。区分「键不存在=无需凭据」与「键存在但空/非法=误配→拒」
        # （GPT 外审收口：旧 `if secret_ref` 让 secret_ref: "" / 0 / false 按无凭据放行）。值绑局部、只传给执行器。
        secret: str | None = None
        if "secret_ref" in binding:
            secret_ref = binding["secret_ref"]
            if not isinstance(secret_ref, str) or not secret_ref:
                return None, "binding 声明 secret_ref 但为空/非字符串——fail-closed（无凭据应删该字段，不留空/非法值）"
            try:
                secret = self.secret_resolver.resolve(secret_ref)
            except Exception:
                # resolver 抛错（vault 超时/鉴权失败等）即 fail-closed（宁可拒不可炸，call() 恒回 Receipt）；错误串
                # **绝不带异常内文**——异常消息/栈可能含连接串或 token，带进来即踩穿「值永不进日志」。
                return (
                    None,
                    f"secret「{secret_ref}」解析出错——fail-closed（凭据取值失败即拒；值永不进包/日志/剧集）",
                )
            # 强制点自持 fail-closed，不信任 pluggable resolver 自律：None 或空串一律拒（契约 B.3「空串=没给凭据」
            # 落在强制点，非只靠参考 resolver 的 `or None`）；`not` 用真值判定——真实凭据从不为空串。
            if not secret:
                return (
                    None,
                    f"secret「{secret_ref}」未在部署环境解析——fail-closed（凭据缺失即拒；值永不进包/日志/剧集）",
                )
        # 分派：按 endpoint scheme 选执行器（不按 manifest kind——SPEC B.3）。mcp 预留不实现；未注册即 fail-closed。
        scheme = endpoint.split("://", 1)[0] if "://" in endpoint else ""
        if scheme == "mcp":
            return None, "mcp 执行器 W6 预留未实现（更大集成推后，不在 W6 范围）——fail-closed，不猜、不兜底"
        executor = self.executors.get(scheme)
        if executor is None:
            return None, f"不识别的 endpoint scheme「{scheme}」——fail-closed（无对应执行器；生产驱动由部署侧注入）"
        kwargs = {}
        if timeout is not None and _executor_supports_timeout(executor):
            kwargs["timeout"] = timeout  # 剩余预算传导（复核 P2）；老驱动无参不传、不炸
        try:
            payload, error = executor.execute(
                endpoint=endpoint,
                interface=itf,
                params=params,
                secret=secret,
                is_write=is_write,
                pack_root=self.pack_root,
                **kwargs,
            )
        except Exception:
            # 执行器不许炸穿 call()（契约：call() 恒回 Receipt）——任何执行器异常（含可插拔生产驱动的意外异常、
            # sqlite3.Warning 多语句、http.client 截断响应、MemoryError 巨响应体）统一转 fail-closed 回执。
            # 错误串**绝不带异常内文**——异常消息/栈可能含连接串或 secret，带进来即踩穿「值永不进日志」。
            return None, f"「{scheme}」执行器执行异常——fail-closed（执行器不许炸穿；异常内文不外泄）"
        # secret 反射清洗（GPT 外审收口）：反射型 API 回显 Authorization/token → secret 值进 payload/error → 进回执/
        # 剧集/日志，踩穿「值永不进剧集/回执」（脱敏只认 PII 正则、认不出 secret）。用**本次** secret 值抹掉。
        # error 也抹（GPT 复审：可插拔执行器 error 串可能含凭据）——error 为 None 时 _scrub_secret 原样返回 None。
        if secret:
            payload = _scrub_secret(payload, secret) if payload is not None else payload
            error = _scrub_secret(error, secret)
        return payload, error
