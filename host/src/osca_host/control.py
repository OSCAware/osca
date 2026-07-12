"""控制通道：unix socket 上的 JSON-lines 协议（v1）。

启停是运行时对注册表的操作——操作入口就是这条通道。每行一个 JSON 请求
{"v": 1, "cmd": ..., "token": ...}，回一行 JSON 响应 {"ok": ...}。
协议保持傻：本地管控，不是对外 API。

M4-W0.2 安全内核：
- 开发模式 0700/0600；生产模式以部署者指定 group 提供 0710/0660 可达性，对端
  kernel uid + token/expected_uid/role 仍逐层 fail-closed；
- 运行目录从根逐级 O_NOFOLLOW 打开并持 fd；凭据/lock/清理均走 dir_fd，socket
  bind 前后复核父 inode；
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
import grp
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
CONNECTION_DRAIN_TIMEOUT = 0.5
DEV_DIR_MODE = 0o700
DEV_SOCKET_MODE = 0o600
PROD_DIR_MODE = 0o710
PROD_SOCKET_MODE = 0o660


class RuntimeDirectory:
    """从 `/` 逐级 O_NOFOLLOW 打开的运行目录，并持有最终 inode 的 fd。"""

    def __init__(self, path: Path, control_group: str | None = None):
        if not path.is_absolute():
            raise ValueError(f"运行目录必须是绝对路径：{path}")
        self.path = path
        self.production = control_group is not None
        self.gid = self._resolve_group(control_group) if control_group is not None else os.getgid()
        self.dir_mode = PROD_DIR_MODE if self.production else DEV_DIR_MODE
        self.socket_mode = PROD_SOCKET_MODE if self.production else DEV_SOCKET_MODE
        self.fd, created = self._open(create_final=True)
        try:
            st = os.fstat(self.fd)
            if st.st_uid != os.getuid():
                raise OSError(f"运行目录属主不是当前用户——拒绝使用：{path}")
            if created:
                if self.production:
                    os.fchown(self.fd, -1, self.gid)
                os.fchmod(self.fd, self.dir_mode)
                st = os.fstat(self.fd)
            mode = stat.S_IMODE(st.st_mode)
            if mode != self.dir_mode:
                raise OSError(
                    f"运行目录权限不符合{'生产' if self.production else '开发'}模式"
                    f"（需要 {self.dir_mode:04o}，实际 {mode:04o}）：{path}"
                )
            if self.production and st.st_gid != self.gid:
                raise OSError(f"运行目录 group 不符合生产配置（需要 gid {self.gid}，实际 {st.st_gid}）：{path}")
            self.inode = (st.st_dev, st.st_ino)
        except BaseException:
            os.close(self.fd)
            raise

    @staticmethod
    def _resolve_group(name: str) -> int:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("生产模式 control group 须是非空 Unix group 名")
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError as e:
            raise ValueError(f"生产模式 control group 不存在：{name}") from e

    def _validate_trusted_ancestor(self, fd: int, path: Path) -> None:
        """生产路径祖先须不可由 control group/其他 UID 改名。

        root 或 Host UID 所有的普通不可写目录可信；sticky 目录（如 /tmp）也可信，
        因为非目录/条目 owner 不能改名 Host 所有的下一层。其他 UID 所有的祖先，
        或无 sticky 的 group/world 可写祖先，会重新打开 precheck→bind 逃逸窗口。
        """
        st = os.fstat(fd)
        if st.st_uid not in (0, os.getuid()):
            raise OSError(f"生产运行目录祖先须由 root 或 Host UID 持有：{path}")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise OSError(f"生产运行目录祖先可被 group/other 改名且无 sticky 保护：{path}")
        if st.st_gid == self.gid:
            traversable = bool(mode & stat.S_IXGRP)
        else:
            traversable = bool(mode & stat.S_IXOTH)
        if not traversable:
            raise OSError(f"生产 control group 无法遍历运行目录祖先：{path}")

    def _open(self, *, create_final: bool) -> tuple[int, bool]:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parts = self.path.parts
        fd = os.open(parts[0], flags)
        created = False
        try:
            current_path = Path(parts[0])
            if self.production:
                self._validate_trusted_ancestor(fd, current_path)
            for index, part in enumerate(parts[1:], start=1):
                final = index == len(parts) - 1
                try:
                    next_fd = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not final or not create_final:
                        raise
                    os.mkdir(part, self.dir_mode, dir_fd=fd)
                    created = True
                    next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                current_path /= part
                if self.production and not final:
                    self._validate_trusted_ancestor(fd, current_path)
            return fd, created
        except BaseException:
            os.close(fd)
            raise

    def path_matches(self) -> bool:
        """普通 bind 路径仍必须解析到被持有 inode，且沿途不得出现链接。"""
        try:
            fd, _ = self._open(create_final=False)
        except OSError:
            return False
        try:
            st = os.fstat(fd)
            return (st.st_dev, st.st_ino) == self.inode
        finally:
            os.close(fd)

    def stat(self, name: str):
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def secure_run_dir(run_dir: Path) -> None:
    """兼容入口：按开发模式安全打开运行目录，验证后立即关闭 fd。"""
    runtime = RuntimeDirectory(run_dir)
    runtime.close()


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

    def __init__(self, socket_path: Path, handler, authorizer, control_group: str | None = None):
        self.socket_path = socket_path
        self.handler = handler
        self.authorizer = authorizer
        self.control_group = control_group
        self.read_timeout = READ_TIMEOUT
        self.write_timeout = WRITE_TIMEOUT
        self.max_connections = MAX_CONNECTIONS
        self._connections = 0
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._bound: tuple[int, int] | None = None  # 本实例创建的 socket inode（st_dev, st_ino）
        self._runtime: RuntimeDirectory | None = None
        self._client_tasks: set[asyncio.Task] = set()

    @property
    def runtime(self) -> RuntimeDirectory:
        if self._runtime is None:
            raise RuntimeError("运行目录尚未准备")
        return self._runtime

    def prepare_runtime(self) -> RuntimeDirectory:
        if self._runtime is None:
            self._runtime = RuntimeDirectory(self.socket_path.parent, self.control_group)
        return self._runtime

    def _entry_inode(self, name: str) -> tuple[int, int] | None:
        try:
            st = self.runtime.stat(name)
        except FileNotFoundError:
            return None
        return st.st_dev, st.st_ino

    def _unlink_bound(self) -> None:
        if self._bound is not None and self._entry_inode(self.socket_path.name) == self._bound:
            self.runtime.unlink(self.socket_path.name)

    async def start(self) -> None:
        runtime = self.prepare_runtime()
        lock_name = self.socket_path.name + ".lock"
        self._lock_fd = os.open(
            lock_name,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=runtime.fd,
        )
        try:
            lock_st = os.fstat(self._lock_fd)
            if not stat.S_ISREG(lock_st.st_mode):
                raise OSError(f"控制通道 lock 不是普通文件：{lock_name}")
            if lock_st.st_uid != os.getuid():
                raise OSError(f"控制通道 lock 属主不是当前用户：{lock_name}")
            if stat.S_IMODE(lock_st.st_mode) & 0o077:
                raise OSError(f"控制通道 lock 权限过宽（须 0600 以内）：{lock_name}")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise OSError(f"另一个 Host 实例正在此控制通道上运行：{self.socket_path}") from e
            # 持有实例锁 ⇒ 此路径不可能有活 Host——存在的只能是残留 socket；其他类型拒绝清理
            with contextlib.suppress(FileNotFoundError):
                if not stat.S_ISSOCK(runtime.stat(self.socket_path.name).st_mode):
                    raise OSError(f"控制通道路径被非 socket 占用（拒绝清理，请人工排查）：{self.socket_path}")
                runtime.unlink(self.socket_path.name)
            if not runtime.path_matches():
                raise OSError(f"运行目录路径在 bind 前已被替换——拒绝启动：{runtime.path}")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.setblocking(False)
                listener.bind(str(self.socket_path))
                st = runtime.stat(self.socket_path.name)
                self._bound = (st.st_dev, st.st_ino)  # chmod/asyncio 接管前立即记录路径 inode
                if not runtime.path_matches():
                    raise OSError(f"运行目录路径在 bind 后已被替换——拒绝启动：{runtime.path}")
                os.chmod(
                    self.socket_path.name,
                    runtime.socket_mode,
                    dir_fd=runtime.fd,
                    follow_symlinks=False,
                )
                if runtime.production:
                    os.chown(
                        self.socket_path.name,
                        -1,
                        runtime.gid,
                        dir_fd=runtime.fd,
                        follow_symlinks=False,
                    )
                self._server = await asyncio.start_unix_server(self._serve, sock=listener, limit=MAX_LINE)
                listener = None
            finally:
                if listener is not None:
                    listener.close()
        except BaseException:
            # fail-closed：权限面没立起来就不许开门——bind 之后任一步失败都要关监听器、
            # 删自己的 socket，再放实例锁；不留「无锁监听器」给后来的实例并存（W0.1 P1-3）
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            with contextlib.suppress(OSError):
                self._unlink_bound()
            self._bound = None
            os.close(self._lock_fd)  # 锁文件保留：unlink 锁文件才是竞态（同 ledger 锁纪律）
            self._lock_fd = None
            raise

    async def close(self) -> None:
        if self._server:
            server = self._server
            server.close()
            current = asyncio.current_task()
            tasks = {task for task in self._client_tasks if task is not current and not task.done()}
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=CONNECTION_DRAIN_TIMEOUT)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await server.wait_closed()
            self._server = None
        # 只删本实例创建的 inode——路径上是别人的东西就不动（不误删后来者的入口）
        if self._bound is not None:
            with contextlib.suppress(FileNotFoundError):
                self._unlink_bound()
            self._bound = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # 关闭即释放实例锁
            self._lock_fd = None
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._client_tasks.add(current)
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
            if current is not None:
                self._client_tasks.discard(current)

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
