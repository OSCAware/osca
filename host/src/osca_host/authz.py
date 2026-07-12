"""控制通道的身份与授权：Principal + Authorizer + 命令 schema（M4-W0 安全内核）。

传输层（osca_host.control）只证明「本机同 uid 进程」；进程级身份靠 token——
Host 启动时生成 host_admin token（0600 文件），其余 principal 由部署者在
principals 文件签发（M4 三界面各持自己的 token，绝不共享 admin token）。
授权在进入命令实现前裁决：角色 → 能力集，不在集合内一律拒绝（fail-closed）。

M4 权限矩阵（Review M4 首轮拍板）：host_admin 管生命周期但**不可授予业务审批**
——审批是业务裁决，属 approver（审批卡界面）；operator 只有脱敏快照、启停触发
与剧集摘要，拿不到 load 路径与完整剧集；expert 的命令随 M4-W1 专家端落地。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml

PROTOCOL_VERSION = 1
MIN_TOKEN_LEN = 16

# 角色 → 命令能力集。改矩阵是拍板级决策：新命令必须显式归入角色，不存在默认继承。
ROLE_CAPS: dict[str, frozenset[str]] = {
    "host_admin": frozenset({"status", "load", "unload", "enable", "disable", "fire", "episodes", "episode", "stop"}),
    "operator": frozenset({"status", "enable", "disable", "fire", "episodes"}),
    "approver": frozenset({"approve"}),
    "expert": frozenset(),  # M4-W1：查看分配稿件/Diff 的命令落地时归入
}

# 命令 → 参数字段（全部必填、非空 str）。请求顶层除 v/cmd/token 与这些字段外不许有别的键
# ——load 只收部署 ID：包路径/binding/解压目录一律服务端解析，不给 confused-deputy 留面。
COMMAND_FIELDS: dict[str, tuple[str, ...]] = {
    "status": (),
    "load": ("deployment_id",),
    "unload": ("package_id",),
    "enable": ("package_id", "aware_id"),
    "disable": ("package_id", "aware_id"),
    "fire": ("package_id", "trigger_id"),
    "approve": ("package_id", "action"),
    "episodes": (),
    "episode": ("episode_id",),
    "stop": (),
}


@dataclass(frozen=True)
class Principal:
    """经 token 认证的调用方身份。"""

    name: str
    role: str


def validate_request(request) -> str | None:
    """命令 schema 裁决：合法返回 None，否则返回人话拒因。顶层必须是 mapping。"""
    if not isinstance(request, dict):
        return "请求必须是 JSON 对象"
    if request.get("v") != PROTOCOL_VERSION:
        return f"协议版本不符：需要 v={PROTOCOL_VERSION}"
    cmd = request.get("cmd")
    if not isinstance(cmd, str) or cmd not in COMMAND_FIELDS:
        return f"未知命令：{cmd!r}"
    fields = COMMAND_FIELDS[cmd]
    extra = sorted(set(request) - {"v", "cmd", "token", *fields})
    if extra:
        return f"命令 {cmd} 不接受字段：{'、'.join(extra)}"
    for field in fields:
        if not isinstance(request.get(field), str) or not request[field]:
            return f"命令 {cmd} 缺少字段（须为非空字符串）：{field}"
    return None


class Authorizer:
    """token → Principal 认证与逐命令能力裁决。token 以 sha256 摘要存表，不留明文。"""

    def __init__(self):
        self._by_digest: dict[str, Principal] = {}

    def register(self, token: str, principal: Principal) -> None:
        if principal.role not in ROLE_CAPS:
            raise ValueError(f"未知角色：{principal.role}（可选：{'/'.join(sorted(ROLE_CAPS))}）")
        if not isinstance(token, str) or len(token) < MIN_TOKEN_LEN:
            raise ValueError(f"token 过短（须 ≥{MIN_TOKEN_LEN} 字符）：principal {principal.name}")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if digest in self._by_digest:
            raise ValueError(f"token 重复（一 token 一 principal）：principal {principal.name}")
        self._by_digest[digest] = principal

    def identify(self, token) -> Principal | None:
        if not isinstance(token, str) or not token:
            return None
        return self._by_digest.get(hashlib.sha256(token.encode()).hexdigest())

    def authorize(self, principal: Principal, cmd: str) -> bool:
        return cmd in ROLE_CAPS.get(principal.role, frozenset())


def ensure_admin_token(path: Path) -> str:
    """host_admin token：已存在（普通文件）即复用，否则原子生成 0600 新文件。"""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError(f"token 文件不是普通文件（符号链接一律拒绝）：{path}") from None
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < MIN_TOKEN_LEN:
            raise OSError(f"token 文件内容过短——删除后重启 Host 重新生成：{path}") from None
        return token
    token = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def load_principals(path: Path, authorizer: Authorizer) -> int:
    """部署者签发的 principals 文件：yaml 列表 [{name, role, token}]，权限须 0600。

    缺文件 = 只有 admin（合法的单用户形态）；文件存在但形态/权限/条目非法一律
    抛错拒绝启动——签发面配置错误必须响，不许静默降级成「谁都进不来」或「谁都进得来」。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"principals 文件不是普通文件（符号链接一律拒绝）：{path}")
    if st.st_mode & 0o077:
        raise OSError(f"principals 文件权限过宽（须 0600，内含 token）：{path}")
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(entries, list):
        raise ValueError(f"principals 文件须是列表 [{{name, role, token}}]：{path}")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"name", "role", "token"}:
            raise ValueError(f"principals 第 {i + 1} 条须恰含 name/role/token 三键：{path}")
        authorizer.register(str(entry["token"]), Principal(str(entry["name"]), str(entry["role"])))
    return len(entries)
