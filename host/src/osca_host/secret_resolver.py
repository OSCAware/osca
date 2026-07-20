"""Host secret 解析（W6-2）：`binding.secret_ref` → 值，交真实执行器建连接 / 带鉴权头。

三不纪律（SPEC B.3，凭据面硬契约）：secret 值——
- **永不进包**：binding 只声明名字（`secret_ref`），值留在部署环境 secret manager；
- **永不进日志**：error/audit 只带 `secret_ref` 名、绝不带值（fail-closed 错误也只带名）；
- **永不进剧集上下文/回执**：值只在执行器内活到发起连接为止，不落回执、不进台账。

resolver 取不到值即 **fail-closed**（凭据缺失即拒，错误只带名）——由调用方（connector `_execute_real`）落。

可插拔：`SecretResolver` 协议（结构化类型，任何带 `resolve(str) -> str | None` 的对象都可注入）。
参考实现 `EnvVarSecretResolver`（`secret_ref` 作环境变量名，如 `FINANCE_DB_RO_KEY`）；部署侧可按同协议
注入 file / vault / callable resolver。默认 env-var。参考实现**不缓存、不记录**——三不的第一道保证。
"""

from __future__ import annotations

import os
from typing import Protocol


class SecretResolver(Protocol):
    """按名解析 secret 值。取不到返回 None（调用方 fail-closed，错误只带名、绝不带值）。"""

    def resolve(self, secret_ref: str) -> str | None: ...


class EnvVarSecretResolver:
    """参考实现：`secret_ref` 作环境变量名取值。空名 / 未设 / 空串一律按「没给凭据」返回 None
    （部署把变量设成空串 = 没给，返回空串去建连接不如 fail-closed 安全）。不缓存、不记录。"""

    def resolve(self, secret_ref: str) -> str | None:
        if not secret_ref:
            return None
        return os.environ.get(secret_ref) or None
