"""控制通道的身份与授权：Principal + Authorizer + 命令 schema（M4-W0 安全内核）。

信任模型两档，诚实标注（M4-W0.1）：
- 开发模式（principal 不带 uid）：全部进程同 OS uid——token 防误用与角色越权，
  **不宣称抵抗同 uid 进程失陷**（同 uid 天然读得到 token 文件与彼此内存）；
- 生产模式（principals 条目写 uid）：各界面进程各自 OS uid/容器，principal 绑定
  expected_uid + role + token 摘要——偷来的 token 在别的 uid 上一律无效；
  admin token 绑定 Host 自己的 uid，被攻陷的界面进程偷到也当不了 admin。

授权在进入命令实现前裁决：角色 → 能力集，不在集合内一律拒绝（fail-closed）。
M4 权限矩阵：host_admin 管生命周期但**不可授予业务审批**；operator 只有快照、
启停触发与剧集摘要（脱敏 DTO 属 W2）；approver 在 W3 审批 challenge
（pending → approved|denied → consumed，绑定 approver/episode/payload digest/
expiry/nonce）落地前为空集——旧 set[action] 授予不从控制通道暴露；
expert 的命令随 M4-W1 专家端落地。
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
MAX_TEXT_LEN = 4096  # 配置文本字段统一上限；含控制字符一律拒绝
MAX_CRED_FILE = 64 * 1024  # 凭据文件大小上限

# 角色 → 命令能力集。改矩阵是拍板级决策：新命令必须显式归入角色，不存在默认继承。
ROLE_CAPS: dict[str, frozenset[str]] = {
    "host_admin": frozenset({"status", "load", "unload", "enable", "disable", "fire", "episodes", "episode", "stop"}),
    "operator": frozenset({"status", "enable", "disable", "fire", "episodes"}),
    "approver": frozenset(),  # W3 审批 challenge 前为空——旧 approve RPC 对全角色关闭，不留无绑定授予面
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
    """经 token 认证的调用方身份。uid 非空即生产模式绑定：token 只在该对端 uid 上有效。"""

    name: str
    role: str
    uid: int | None = None


def clean_text(value, what: str, max_len: int = MAX_TEXT_LEN) -> str:
    """配置文本字段的严格验型：非空 str（不做 str() 静默转换）、限长、拒控制字符。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} 须是非空字符串（不接受其他类型的静默转换）")
    if len(value) > max_len:
        raise ValueError(f"{what} 超长（上限 {max_len} 字符）")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{what} 含控制字符——拒绝")
    return value


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
    """token → Principal 认证与逐命令能力裁决。token 以 sha256 摘要存表，不留明文。

    peer_uids 是传输层允许名单的增量：生产模式下各界面进程各自 uid，
    绑定了 uid 的 principal 把自己的 uid 加进名单——除此之外只放行 Host 同 uid。
    """

    def __init__(self):
        self._by_digest: dict[str, Principal] = {}
        self.peer_uids: set[int] = set()

    def register(self, token: str, principal: Principal) -> None:
        if principal.role not in ROLE_CAPS:
            raise ValueError(f"未知角色：{principal.role}（可选：{'/'.join(sorted(ROLE_CAPS))}）")
        clean_text(principal.name, f"principal 名（{principal.role}）", max_len=200)
        clean_text(token, f"principal {principal.name} 的 token")
        if len(token) < MIN_TOKEN_LEN:
            raise ValueError(f"token 过短（须 ≥{MIN_TOKEN_LEN} 字符）：principal {principal.name}")
        if principal.uid is not None and (
            not isinstance(principal.uid, int) or isinstance(principal.uid, bool) or principal.uid < 0
        ):
            raise ValueError(f"principal {principal.name} 的 uid 须是非负整数")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if digest in self._by_digest:
            raise ValueError(f"token 重复（一 token 一 principal）：principal {principal.name}")
        self._by_digest[digest] = principal
        if principal.uid is not None:
            self.peer_uids.add(principal.uid)

    def identify(self, token) -> Principal | None:
        if not isinstance(token, str) or not token:
            return None
        return self._by_digest.get(hashlib.sha256(token.encode()).hexdigest())

    def authorize(self, principal: Principal, cmd: str) -> bool:
        return cmd in ROLE_CAPS.get(principal.role, frozenset())


def read_private_file(path: Path) -> str:
    """凭据文件读取协议：O_NOFOLLOW 打开 → 对**同一个 fd** fstat 验证（普通文件、
    属主是自己、权限 0600 以内、限长）→ 从该 fd 读——检查与读取之间无替换窗口。"""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"凭据文件不是普通文件：{path}")
        if st.st_uid != os.getuid():
            raise OSError(f"凭据文件属主不是当前用户——拒绝采信：{path}")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise OSError(f"凭据文件权限过宽（须 0600 以内，内含 token）：{path}")
        if st.st_size > MAX_CRED_FILE:
            raise OSError(f"凭据文件超长（上限 {MAX_CRED_FILE} 字节）：{path}")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1  # fdopen 接管关闭
            return fh.read()
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_admin_token(path: Path) -> str:
    """host_admin token：已存在即经凭据协议校验后复用，否则原子生成 0600 新文件。

    轮换 = 替换/删除文件后重启 Host（token 只在进程内存里生效，重启即旧 token 全体失效）；
    在线撤销随 W3 审批 challenge 的持久化状态机一起落地。
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        token = read_private_file(path).strip()
        if len(token) < MIN_TOKEN_LEN:
            raise OSError(f"token 文件内容过短——删除后重启 Host 重新生成：{path}") from None
        return token
    token = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def load_principals(path: Path, authorizer: Authorizer) -> int:
    """部署者签发的 principals 文件：yaml 列表 [{name, role, token[, uid]}]，权限须 0600。

    uid 即生产模式绑定（token 只在该对端 uid 上有效）；不写 uid 为开发模式条目
    （同 uid 可信）。缺文件 = 只有 admin（合法的单用户形态）；文件存在但形态/
    权限/条目非法一律抛错拒绝启动——签发面配置错误必须响，不许静默降级。
    """
    try:
        text = read_private_file(path)
    except FileNotFoundError:
        return 0
    entries = yaml.safe_load(text) or []
    if not isinstance(entries, list):
        raise ValueError(f"principals 文件须是列表 [{{name, role, token[, uid]}}]：{path}")
    for i, entry in enumerate(entries):
        if (
            not isinstance(entry, dict)
            or not {"name", "role", "token"} <= set(entry)
            or set(entry) - {"name", "role", "token", "uid"}
        ):
            raise ValueError(f"principals 第 {i + 1} 条须含 name/role/token（可选 uid），不收其他键：{path}")
        name = clean_text(entry["name"], f"principals 第 {i + 1} 条 name", max_len=200)
        role = clean_text(entry["role"], f"principals 第 {i + 1} 条 role", max_len=50)
        token = clean_text(entry["token"], f"principals 第 {i + 1} 条 token")
        uid = entry.get("uid")
        if uid is not None and (not isinstance(uid, int) or isinstance(uid, bool) or uid < 0):
            raise ValueError(f"principals 第 {i + 1} 条 uid 须是非负整数：{path}")
        authorizer.register(token, Principal(name, role, uid))
    return len(entries)
