"""控制通道：unix socket 上的 JSON-lines 协议（v1）。

启停是运行时对注册表的操作——操作入口就是这条通道。每行一个 JSON 请求
{"v": 1, "cmd": ..., "token": ...}，回一行 JSON 响应 {"ok": ...}。
协议保持傻：本地管控，不是对外 API。

M4-W0 安全内核（Review M4 首轮）：
- 私有运行目录 0700 + socket 0600 + 对端 uid 校验（fail-closed）——传输层先证明
  「本机同用户进程」；进程级身份再由 token → Principal 证明（osca_host.authz），
  schema 与逐命令授权都在进入命令实现前裁决；
- 实例 flock：同一 socket 路径同时只有一个 Host——活 socket 不可被第二实例接管；
  残留 socket 只在持锁后清理且必须真是 socket；关闭只删本实例创建的 inode
  （lstat 比对），绝不误删后来者的入口；
- 协议加固：读超时、单行 64 KiB 上限、并发连接上限、统一异常边界——任何请求
  形态都得到一行 JSON 回应（带 error 码），不许异常穿透留空响应。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import socket
import stat
import struct
from pathlib import Path

from osca_host.authz import PROTOCOL_VERSION, validate_request

log = logging.getLogger("osca-host")

DEFAULT_SOCKET = Path.home() / ".osca" / "host.sock"
MAX_LINE = 64 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
READ_TIMEOUT = 10.0
WRITE_TIMEOUT = 10.0
MAX_CONNECTIONS = 16


def secure_run_dir(run_dir: Path) -> None:
    """私有运行目录：不跟随符号链接地创建并收紧 0700（M4-W0.1 P1-2）。

    mkdir/chmod 按路径操作都会跟随链接——运行目录被预置成指向外部目录的符号链接
    时，socket/token/lock 会建到外面、外部目录权限还被改掉。这里 os.mkdir 不跟随、
    O_NOFOLLOW 打开后对 fd fchmod/fstat：目录是链接即拒绝启动。
    """
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(run_dir)
    fd = os.open(run_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise OSError(f"运行目录属主不是当前用户——拒绝使用：{run_dir}")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def admin_token_path(socket_path: Path) -> Path:
    """host_admin token 文件：与 socket 同住私有运行目录。"""
    return socket_path.with_name(socket_path.name + ".token")


def principals_path(socket_path: Path) -> Path:
    """部署者签发的 principals 文件（可选）：与 socket 同住私有运行目录。"""
    return socket_path.with_name(socket_path.name + ".principals.yaml")


def _error(code: str, detail: str) -> dict:
    """统一错误响应：ok=False + 机器可判的 error 码 + 人话 detail。"""
    return {"ok": False, "error": code, "detail": detail}


def _peer_uid(sock: socket.socket) -> int | None:
    """unix socket 对端进程的 uid；取不到返回 None（调用方 fail-closed 拒绝）。"""
    try:
        if hasattr(socket, "SO_PEERCRED"):  # Linux：struct ucred {pid, uid, gid}
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", data)[1]
        # macOS/BSD：LOCAL_PEERCRED（level SOL_LOCAL=0）→ struct xucred；版本不符不硬猜
        data = sock.getsockopt(0, 0x0001, struct.calcsize("2i") + 4 + 16 * 4)
        version, uid = struct.unpack("2i", data[:8])
        return uid if version == 0 else None  # XUCRED_VERSION == 0
    except (OSError, struct.error):
        return None


class ControlServer:
    """挂在 Host 事件循环上的控制端。handler 由 Host 注入：async (request, principal) → dict。"""

    def __init__(self, socket_path: Path, handler, authorizer):
        self.socket_path = socket_path
        self.handler = handler
        self.authorizer = authorizer
        self.read_timeout = READ_TIMEOUT
        self.write_timeout = WRITE_TIMEOUT
        self.max_connections = MAX_CONNECTIONS
        self._connections = 0
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._bound: tuple[int, int] | None = None  # 本实例创建的 socket inode（st_dev, st_ino）

    async def start(self) -> None:
        secure_run_dir(self.socket_path.parent)  # 私有目录：bind 与 chmod 之间的权限窗口也被目录挡住
        lock_path = self.socket_path.with_name(self.socket_path.name + ".lock")
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise OSError(f"另一个 Host 实例正在此控制通道上运行：{self.socket_path}") from e
            # 持有实例锁 ⇒ 此路径不可能有活 Host——存在的只能是残留 socket；其他类型拒绝清理
            with contextlib.suppress(FileNotFoundError):
                if not stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                    raise OSError(f"控制通道路径被非 socket 占用（拒绝清理，请人工排查）：{self.socket_path}")
                self.socket_path.unlink()
            self._server = await asyncio.start_unix_server(self._serve, path=str(self.socket_path), limit=MAX_LINE)
            os.chmod(self.socket_path, 0o600)
            st = os.lstat(self.socket_path)
            self._bound = (st.st_dev, st.st_ino)
        except BaseException:
            # fail-closed：权限面没立起来就不许开门——bind 之后任一步失败都要关监听器、
            # 删自己的 socket，再放实例锁；不留「无锁监听器」给后来的实例并存（W0.1 P1-3）
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                with contextlib.suppress(OSError):
                    if stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                        self.socket_path.unlink()  # 持锁期间路径上的 socket 只能是自己刚 bind 的
            os.close(self._lock_fd)  # 锁文件保留：unlink 锁文件才是竞态（同 ledger 锁纪律）
            self._lock_fd = None
            raise

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # 只删本实例创建的 inode——路径上是别人的东西就不动（不误删后来者的入口）
        if self._bound is not None:
            with contextlib.suppress(FileNotFoundError):
                st = os.lstat(self.socket_path)
                if (st.st_dev, st.st_ino) == self._bound:
                    self.socket_path.unlink()
            self._bound = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # 关闭即释放实例锁
            self._lock_fd = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 计数覆盖整个连接生命周期（含响应序列化与 drain）——不读响应的慢客户端
        # 也占并发额度，不许积累成不受上限约束的阻塞任务（W0.1 P2）
        over = self._connections >= self.max_connections
        self._connections += 1
        try:
            try:
                if over:
                    response = _error("busy", f"控制通道并发连接已达上限（{self.max_connections}）")
                else:
                    response = await self._respond(reader, writer)
                if response is not None:
                    await self._write_response(writer, response)
            except Exception:
                # 统一异常边界：命令实现/序列化的意外一律回一行 internal，不许空响应
                log.exception("控制连接处理异常")
                with contextlib.suppress(Exception):
                    await self._write_response(writer, _error("internal", "内部错误（见 Host 日志）"))
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        finally:
            self._connections -= 1

    async def _write_response(self, writer: asyncio.StreamWriter, response: dict) -> None:
        data = json.dumps(response, ensure_ascii=False).encode() + b"\n"
        if len(data) > MAX_RESPONSE:
            log.error(f"控制响应超出大小上限（{len(data)} > {MAX_RESPONSE} 字节）——已替换为错误响应")
            oversize = _error("internal", "响应超出大小上限（见 Host 日志）")
            data = json.dumps(oversize, ensure_ascii=False).encode() + b"\n"
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout=self.write_timeout)  # 写超时：不给不收响应的对端挂住

    async def _respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict | None:
        uid = _peer_uid(writer.get_extra_info("socket"))
        # 传输层允许名单：Host 同 uid + 生产模式 principals 声明的 uid；凭据取不到 fail-closed
        if uid is None or (uid != os.getuid() and uid not in self.authorizer.peer_uids):
            log.warning(f"控制连接对端 uid 校验失败（peer uid={uid}）——拒绝")
            return _error("unauthorized", "对端凭据校验失败：对端 uid 不在控制通道允许名单")
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=self.read_timeout)
        except asyncio.TimeoutError:
            return _error("bad_request", f"读取请求超时（{self.read_timeout}s 内未收到完整一行）")
        except (ValueError, asyncio.LimitOverrunError):
            return _error("bad_request", f"请求超长（单行上限 {MAX_LINE} 字节）")
        if not line:
            return None  # 对端未发一字节即断开
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return _error("bad_request", "请求不是合法 JSON")
        problem = validate_request(request)
        if problem is not None:
            return _error("bad_request", problem)
        principal = self.authorizer.identify(request.get("token"))
        if principal is None:
            return _error("unauthorized", "token 缺失或不可识别——控制命令必须携带有效 principal token")
        # token 与对端 uid 双绑定：绑定了 uid 的 principal 只在自己的 uid 上有效；
        # 开发模式条目（无 uid）只在 Host 同 uid 上有效——偷来的 token 换了进程身份即失效
        required_uid = principal.uid if principal.uid is not None else os.getuid()
        if uid != required_uid:
            log.warning(f"token 与对端 uid 不符：principal {principal.name} 绑定 {required_uid}，对端 {uid}——拒绝")
            return _error("unauthorized", "token 与对端进程身份不符——该 token 不属于这个 uid（疑似被窃）")
        if not self.authorizer.authorize(principal, request["cmd"]):
            log.warning(f"授权拒绝：{principal.name}（{principal.role}）→ {request['cmd']}")
            return _error("forbidden", f"角色 {principal.role} 无权执行 {request['cmd']}")
        return await self.handler(request, principal)


def send_command(
    request: dict, socket_path: Path = DEFAULT_SOCKET, timeout: float = 30.0, token: str | None = None
) -> dict:
    """客户端：发一条命令，等一行响应。自动补协议版本；token 未给时读 admin token 文件。

    Host 未运行时给人话报错。M4 界面进程各持自己的 principal token（token 参数），
    不读 admin token 文件。
    """
    if not socket_path.exists():
        return {"ok": False, "detail": f"Host 未运行（控制通道不存在：{socket_path}）"}
    request = {"v": PROTOCOL_VERSION, **request}
    if "token" not in request:
        if token is None:
            token_file = admin_token_path(socket_path)
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError:
                return {"ok": False, "detail": f"读不到控制 token（{token_file}）——非 admin 请用自己的 principal token"}
        request["token"] = token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf) if buf.strip() else {"ok": False, "detail": "Host 未响应"}
