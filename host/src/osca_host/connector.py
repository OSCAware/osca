"""Host 组件 6：Connector 代理 —— 确定性取数/执行（架构 §4）。

模型只能按名调用，永不写 SQL、永不猜数。代理做三件事：
1. manifest 契约校验：调用未声明的接口 = 接口漂移，当场爆炸（不猜、不兜底）；
2. binding/secret 解析：binding 由部署环境注入（永不进包），secret 只解析名字、
   值留在部署环境的 secret manager；
3. 调用与回执：每次调用产出一张回执（谁调的、走哪个 binding、结果如何），
   结果注入剧集前先过 Policy 脱敏。

执行器按 kind 可插拔。参考实现内置 mock 执行器（endpoint 以 mock:// 开头，
从目录读 <接口名>.yaml 固件）——用于测试与全链路演练；真实 sql_readonly/openapi
执行器属部署侧适配（M6 对接约定）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from osca_host.loader import LoadedPackage
from osca_host.policy import PolicyInterceptor

ENDPOINT_HOST = re.compile(r"^[a-z+]+://([^/:@]+@)?([A-Za-z0-9.-]+)")


@dataclass
class Receipt:
    """一次 Connector 调用的回执。"""

    ok: bool
    interface: str
    binding_ref: str | None = None
    payload: object = None
    redacted: int = 0
    error: str | None = None
    at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))


class ConnectorProxy:
    def __init__(self, loaded: LoadedPackage, bindings: dict, policy: PolicyInterceptor):
        self.package_id = loaded.package_id
        self.bindings = bindings  # 部署环境注入的 binding 表（永不进包）
        self.policy = policy
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
        self, interface_ref: str, params: str = "", *, step: str | None = None, episode_id: str | None = None
    ) -> Receipt:
        """按名调用。step=None 为运行时内部调用（precondition/watch 轮询）。"""
        ok, reason = self.policy.authorize_tool(step, interface_ref, episode_id)
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
        if (connector.get("permissions") or {}).get("write") != "forbidden":
            # 写路径的审批门对内对外一视同仁——step=None 的运行时内部调用（watch/precondition/settle）
            # 也不豁免。不在 approvals 清单的写接口默认拒绝；在清单则挂/复用绑定挑战，批准后一次性 consume。
            # 挑战绑到本次 episode + 被写 payload（params 摘要）——防跨剧集串用与偷梁换柱。
            ok, reason = self.policy.require_write_approval(interface_ref, episode_id=episode_id, payload=params)
            if not ok:
                return Receipt(ok=False, interface=interface_ref, error=reason)

        binding_ref = connector.get("binding_ref")
        binding = self.bindings.get(binding_ref) if binding_ref else None
        if binding is None:
            return Receipt(
                ok=False,
                interface=interface_ref,
                binding_ref=binding_ref,
                error=f"部署环境未注入 binding {binding_ref}（binding 永不进包，缺失即报错）",
            )

        endpoint = str(binding.get("endpoint", ""))
        if endpoint.startswith("mock://"):
            payload, error = self._execute_mock(endpoint, interface_ref, itf)
        else:
            payload, error = self._execute_real(endpoint)
        if error:
            return Receipt(ok=False, interface=interface_ref, binding_ref=binding_ref, error=error)

        payload, hits = self.policy.redact(payload)  # 注入剧集前脱敏
        return Receipt(ok=True, interface=interface_ref, binding_ref=binding_ref, payload=payload, redacted=hits)

    # ── 执行器 ────────────────────────────────────────────────────────

    def _execute_mock(self, endpoint: str, interface_ref: str, itf: dict) -> tuple[object, str | None]:
        fixture = Path(endpoint.removeprefix("mock://")) / f"{interface_ref.split('.', 1)[1]}.yaml"
        if not fixture.is_file():
            return None, f"mock 固件缺失：{fixture}"
        return yaml.safe_load(fixture.read_text(encoding="utf-8")), None

    def _execute_real(self, endpoint: str) -> tuple[object, str | None]:
        m = ENDPOINT_HOST.match(endpoint)
        host = m.group(2) if m else endpoint
        ok, reason = self.policy.authorize_egress(host)
        if not ok:
            return None, reason
        # secret 解析只到名字为止；真实执行器（sql_readonly/openapi/mcp）属部署侧适配（M6 对接约定）
        return None, f"真实执行器未接入：endpoint 主机 {host} 已过 egress，执行适配随 M6 对接约定落地"
