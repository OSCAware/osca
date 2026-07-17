"""控制通道的身份与授权：Principal + Authorizer + 命令 schema（M4-W0 安全内核）。

信任模型两档，诚实标注（M4-W0.1）：
- 开发模式（principal 不带 uid）：全部进程同 OS uid——token 防误用与角色越权，
  **不宣称抵抗同 uid 进程失陷**（同 uid 天然读得到 token 文件与彼此内存）；
- 生产模式（principals 条目写 uid）：各界面进程各自 OS uid/容器，principal 绑定
  expected_uid + role + token 摘要——偷来的 token 在别的 uid 上一律无效；
  admin token 绑定 Host 自己的 uid，被攻陷的界面进程偷到也当不了 admin。

授权在进入命令实现前裁决：角色 → 能力集，不在集合内一律拒绝（fail-closed）。
M4 权限矩阵：host_admin 管生命周期但**不可授予业务审批**；operator 只有快照、
启停触发与剧集摘要（脱敏 DTO 属 W2）；approver 经 W3 审批 challenge
（pending → approved|denied → consumed，绑定 approver/episode/payload digest/
expiry + 一次性 consume）批/驳（绑 challenge_id）与看待批清单——绑定挑战替换旧
set[action] 无绑定授予；expert 的命令随 M4-W1 专家端落地。
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
    # W3 审批 challenge：approve/deny 绑 challenge_id、challenges 看待批清单——绑定挑战替换旧无绑定 set[action]
    "approver": frozenset({"approve", "deny", "challenges"}),
    "expert": frozenset({"episodes", "episode"}),  # M4-W1 专家端：只读交付面（摘要 + 全量导出——draft 正是要交付之物）
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
    "approve": ("package_id", "challenge_id"),  # W3：批一张具体挑战（绑 challenge_id），非旧的按 action 授予
    "deny": ("package_id", "challenge_id"),
    "challenges": ("package_id",),  # 待审批挑战清单（approver 拉取，IM 审批卡输入）
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
        self.register_digest(digest, principal)

    def register_digest(self, digest: str, principal: Principal) -> None:
        """注册部署者保存的 SHA-256 摘要，不要求 Host 接触客户端明文 token。"""
        if principal.role not in ROLE_CAPS:
            raise ValueError(f"未知角色：{principal.role}（可选：{'/'.join(sorted(ROLE_CAPS))}）")
        clean_text(principal.name, f"principal 名（{principal.role}）", max_len=200)
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ValueError(f"principal {principal.name} 的 token_sha256 须是 64 位十六进制摘要")
        if principal.uid is not None and (
            not isinstance(principal.uid, int) or isinstance(principal.uid, bool) or principal.uid < 0
        ):
            raise ValueError(f"principal {principal.name} 的 uid 须是非负整数")
        digest = digest.lower()
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


def read_private_file(path: Path | str, *, dir_fd: int | None = None) -> str:
    """凭据文件读取协议：O_NOFOLLOW 打开 → 对**同一个 fd** fstat 验证（普通文件、
    属主是自己、权限 0600 以内）→ 最多读 MAX+1——无替换窗口或 st_size 增长绕过。"""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
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
        chunks: list[bytes] = []
        remaining = MAX_CRED_FILE + 1
        while remaining:
            chunk = os.read(fd, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_CRED_FILE:
            raise OSError(f"凭据文件超长（上限 {MAX_CRED_FILE} 字节）：{path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise OSError(f"凭据文件不是合法 UTF-8：{path}") from e
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_admin_token(path: Path | str, *, dir_fd: int | None = None) -> str:
    """host_admin token：已存在即经凭据协议校验后复用，否则原子生成 0600 新文件。

    轮换 = 替换/删除文件后重启 Host（token 只在进程内存里生效，重启即旧 token 全体失效）；
    在线撤销随 W3 审批 challenge 的持久化状态机一起落地。
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    except FileExistsError:
        token = read_private_file(path, dir_fd=dir_fd).strip()
        if len(token) < MIN_TOKEN_LEN:
            raise OSError(f"token 文件内容过短——删除后重启 Host 重新生成：{path}") from None
        return token
    token = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def load_principals(
    path: Path | str, authorizer: Authorizer, *, dir_fd: int | None = None, production: bool = False
) -> int:
    """部署者签发的 principals 文件，权限须 0600。

    开发模式兼容 [{name, role, token[, uid]}]；生产模式只收
    [{name, role, uid, token_sha256}]，Host 配置不保存客户端明文。缺文件 = 只有
    admin；存在但形态/权限/条目非法一律拒绝启动，不静默降级。
    """
    try:
        text = read_private_file(path, dir_fd=dir_fd)
    except FileNotFoundError:
        return 0
    try:
        entries = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"principals YAML 无法解析：{path}（为避免泄露凭据，未显示原文）") from e
    if not isinstance(entries, list):
        raise ValueError(f"principals 文件须是列表：{path}")
    for i, entry in enumerate(entries):
        required = {"name", "role", "uid", "token_sha256"} if production else {"name", "role", "token"}
        allowed = required if production else {"name", "role", "token", "uid"}
        if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - allowed:
            shape = "name/role/uid/token_sha256" if production else "name/role/token（可选 uid）"
            raise ValueError(f"principals 第 {i + 1} 条须含 {shape}，不收其他键：{path}")
        name = clean_text(entry["name"], f"principals 第 {i + 1} 条 name", max_len=200)
        role = clean_text(entry["role"], f"principals 第 {i + 1} 条 role", max_len=50)
        uid = entry.get("uid")
        if production and (type(uid) is not int or uid < 0):
            raise ValueError(f"principals 第 {i + 1} 条 uid 须是非负整数且不可为 null：{path}")
        if not production and uid is not None and (type(uid) is not int or uid < 0):
            raise ValueError(f"principals 第 {i + 1} 条 uid 须是非负整数：{path}")
        principal = Principal(name, role, uid)
        if production:
            authorizer.register_digest(entry["token_sha256"], principal)
        else:
            token = clean_text(entry["token"], f"principals 第 {i + 1} 条 token")
            authorizer.register(token, principal)
    return len(entries)
